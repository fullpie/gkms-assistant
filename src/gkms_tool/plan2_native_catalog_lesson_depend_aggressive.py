"""Bounded native catalog adapter for LessonDependExamCardPlayAggressive.

This module deliberately owns only the ``ExamLessonDependExamCardPlayAggressive``
family.  It does not register itself with the central Plan2 catalog or runtime;
the returned immutable objects are the typed seam those surfaces can consume.

The implementation is contract-first.  Master rows and their raw native-shaped
JSON are checked against the 24 known card versions before a version is exposed
as compiled.  Runtime execution uses the already audited scalar helpers in
``native_exam_formula`` and captures CardPlayAggressive once at handoff time.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import Any, Final, Literal, Mapping, Sequence

from .logic_engine import EFFECT_LESSON_BY_MOTIVATION
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    F32_NEGATIVE_EPSILON,
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    NativeFormulaDomainError,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_adding_parameter,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)


PLAN2: Final = "ProducePlanType_Plan2"
ACTIVE_SKILL: Final = "ProduceCardCategory_ActiveSkill"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
TARGET_EFFECT_TYPE: Final = EFFECT_LESSON_BY_MOTIVATION
PLAY_ORIGINS: Final = ("normal", "forced", "extra")
TARGET_VERSION_COUNT: Final = 24
SCHEMA_VERSION: Final = 1

PlayOrigin = Literal["normal", "forced", "extra"]

_EFFECT_BLOCK: Final = "ProduceExamEffectType_ExamBlock"
_EFFECT_BLOCK_ADD_MULTIPLE: Final = "ProduceExamEffectType_ExamBlockAddMultipleAggressive"
_EFFECT_CARD_PLAY_AGGRESSIVE: Final = "ProduceExamEffectType_ExamCardPlayAggressive"
_EFFECT_LESSON_DEPEND_BLOCK: Final = "ProduceExamEffectType_ExamLessonDependBlock"
_UNKNOWN: Final = "ProduceExamEffectType_Unknown"

_TARGET_GROUPS: Final = (
    "effect_group-visible-exam_lesson_depend_block-000",
    "effect_group-visible-exam_lesson-000",
)
_BLOCK_GROUPS: Final = ("effect_group-visible-exam_block-000",)
_CARD_PLAY_GROUPS: Final = ("effect_group-visible-exam_card_play_aggressive-000",)

_RAW_EFFECT_KEYS: Final = frozenset(
    {
        "chainProduceExamEffectId",
        "chainProduceExamEffectIds",
        "customizeProduceDescriptions",
        "effectCount",
        "effectGroupIds",
        "effectTurn",
        "effectType",
        "effectValue1",
        "effectValue2",
        "id",
        "movePositionType",
        "pickCountMax",
        "pickCountMax2",
        "pickCountMin",
        "pickCountMin2",
        "pickCountReferenceProduceCardSearchId",
        "pickCountReferenceProduceCardSearchId2",
        "pickCountType",
        "pickCountType2",
        "pickRangeType",
        "pickRangeType2",
        "produceCardGrowEffectIds",
        "produceCardSearchId",
        "produceCardSearchId2",
        "produceCardStatusEnchantId",
        "produceDescriptions",
        "produceExamStatusEnchantId",
        "targetExamEffectType",
        "targetProduceCardId",
        "targetUpgradeCount",
    }
)

_RAW_CARD_KEYS: Final = frozenset(
    {
        "assetId",
        "category",
        "costType",
        "costValue",
        "effectGroupIds",
        "evaluation",
        "forceStamina",
        "id",
        "isCharacterAsset",
        "isConversion",
        "isEndTurnLost",
        "isInitial",
        "isInitialDeckProduceCard",
        "isLimited",
        "isRestrict",
        "isReward",
        "libraryHidden",
        "maxCustomizeCount",
        "moveEffectTriggerType",
        "moveProduceExamEffectIds",
        "moveProduceExamTriggerIds",
        "name",
        "noDeckDuplication",
        "order",
        "originCharacterId",
        "originIdolCardId",
        "originPrimaStellaIdolCardId",
        "originSupportCardId",
        "planType",
        "playEffects",
        "playMovePositionType",
        "playProduceExamTriggerId",
        "produceCardCustomizeIds",
        "produceCardStatusEnchantId",
        "produceDescriptions",
        "rarity",
        "rentalUnlockProducerLevel",
        "searchTag",
        "stamina",
        "unlockProducerLevel",
        "upgradeCount",
        "viewStartTime",
        "voiceAssetId",
    }
)


class Plan2LessonDependAggressiveContractError(ValueError):
    """A Master/native shape or runtime value is outside the bounded contract."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# Short public name for callers that do not need the Plan2 namespace prefix.
LessonDependAggressiveContractError = Plan2LessonDependAggressiveContractError


def _contract(code: str, detail: str) -> Plan2LessonDependAggressiveContractError:
    return Plan2LessonDependAggressiveContractError(code, detail)


def _i32(value: Any, label: str) -> int:
    if type(value) is not int or value < -(2**31) or value > 2**31 - 1:
        raise _contract("i32-domain", f"{label}={value!r}")
    return value


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise _contract("boolean-shape", f"{label}={value!r}")
    return value


def _string(value: Any, label: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str) or (non_empty and not value):
        raise _contract("string-shape", f"{label}={value!r}")
    return value


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise _contract("json-shape", f"{label}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _contract("json-shape", f"{label} is not an object")
    return parsed


def _json_array(value: Any, label: str) -> list[Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as exc:
        raise _contract("json-shape", f"{label}: {exc}") from exc
    if not isinstance(parsed, list):
        raise _contract("json-shape", f"{label} is not an array")
    return parsed


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveSlot:
    effect_id: str
    effect_type: str
    value1: int
    value2: int = 0
    count: int = 0
    turn: int = 0
    trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False

    def __post_init__(self) -> None:
        _string(self.effect_id, "effect_id", non_empty=True)
        _string(self.effect_type, "effect_type", non_empty=True)
        _string(self.trigger_id, "trigger_id")
        _i32(self.value1, f"{self.effect_id}.value1")
        _i32(self.value2, f"{self.effect_id}.value2")
        _i32(self.count, f"{self.effect_id}.count")
        _i32(self.turn, f"{self.effect_id}.turn")
        _bool(self.hide_icon, f"{self.effect_id}.hide_icon")
        _bool(self.is_once_play_effect, f"{self.effect_id}.is_once_play_effect")


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.effect_group_ids) is not tuple or any(not isinstance(item, str) for item in self.effect_group_ids):
            raise _contract("target-effect-groups", f"{self.effect_id}: groups must be an immutable string tuple")
        if self.effect_type != TARGET_EFFECT_TYPE:
            raise _contract("target-effect-type", f"{self.effect_id}: {self.effect_type!r}")
        if self.effect_group_ids != _TARGET_GROUPS:
            raise _contract("target-effect-groups", f"{self.effect_id}: {self.effect_group_ids!r}")
        _i32(self.value1, f"{self.effect_id}.value1")
        _i32(self.value2, f"{self.effect_id}.value2")
        _i32(self.count, f"{self.effect_id}.count")
        _i32(self.turn, f"{self.effect_id}.turn")
        if self.value2 != 0 or self.count != 1 or self.turn != 0:
            raise _contract(
                "target-effect-shape",
                f"{self.effect_id}: value2/count/turn must be 0/1/0",
            )

    @property
    def raw_ratio_permille(self) -> int:
        return self.value1


@dataclass(frozen=True, slots=True)
class _ExpectedVersion:
    card_id: str
    upgrade_count: int
    name: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    raw_effect_group_ids: tuple[str, ...]
    asset_id: str
    rarity: str
    search_tag: str
    is_initial_deck_produce_card: bool
    max_customize_count: int
    ordered_slots: tuple[LessonDependAggressiveSlot, ...]
    target_slot_index: int

    @property
    def ref(self) -> tuple[str, int]:
        return (self.card_id, self.upgrade_count)


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveVersion:
    card_id: str
    upgrade_count: int
    name: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    ordered_effects: tuple[LessonDependAggressiveSlot, ...]
    target_slot_index: int
    target_effect: LessonDependAggressiveEffect
    asset_id: str
    rarity: str
    search_tag: str
    is_initial_deck_produce_card: bool
    max_customize_count: int

    def __post_init__(self) -> None:
        _string(self.card_id, "card_id", non_empty=True)
        _i32(self.upgrade_count, "upgrade_count")
        _string(self.name, "name", non_empty=True)
        _i32(self.stamina, "stamina")
        _string(self.cost_type, "cost_type", non_empty=True)
        _i32(self.cost_value, "cost_value")
        _string(self.play_trigger_id, "play_trigger_id")
        if type(self.ordered_effects) is not tuple or not self.ordered_effects:
            raise _contract("version-order", f"{self.card_id}@{self.upgrade_count}: ordered effects must be a non-empty tuple")
        if type(self.target_slot_index) is not int or not 0 <= self.target_slot_index < len(self.ordered_effects):
            raise _contract("target-slot-index", f"{self.card_id}@{self.upgrade_count}: {self.target_slot_index!r}")
        target_slot = self.ordered_effects[self.target_slot_index]
        if target_slot.effect_type != TARGET_EFFECT_TYPE or not isinstance(self.target_effect, LessonDependAggressiveEffect):
            raise _contract("target-effect-type", f"{self.card_id}@{self.upgrade_count}: target does not match slot")
        if (
            self.target_effect.effect_id,
            self.target_effect.value1,
            self.target_effect.value2,
            self.target_effect.count,
            self.target_effect.turn,
        ) != (
            target_slot.effect_id,
            target_slot.value1,
            target_slot.value2,
            target_slot.count,
            target_slot.turn,
        ):
            raise _contract("target-effect-shape", f"{self.card_id}@{self.upgrade_count}: target does not match ordered slot")
        _string(self.asset_id, "asset_id", non_empty=True)
        _string(self.rarity, "rarity", non_empty=True)
        _string(self.search_tag, "search_tag")
        _bool(self.is_initial_deck_produce_card, "is_initial_deck_produce_card")
        _i32(self.max_customize_count, "max_customize_count")

    @property
    def ref(self) -> tuple[str, int]:
        return (self.card_id, self.upgrade_count)

    @property
    def ref_text(self) -> str:
        return f"{self.card_id}@{self.upgrade_count}"

    @property
    def companion_effects(self) -> tuple[LessonDependAggressiveSlot, ...]:
        return tuple(slot for i, slot in enumerate(self.ordered_effects) if i != self.target_slot_index)


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveBlocker:
    code: str
    card_id: str
    upgrade_count: int
    detail: str

    def __post_init__(self) -> None:
        _string(self.code, "blocker.code", non_empty=True)
        _string(self.card_id, "blocker.card_id", non_empty=True)
        _i32(self.upgrade_count, "blocker.upgrade_count")
        _string(self.detail, "blocker.detail")

    @property
    def ref(self) -> tuple[str, int]:
        return (self.card_id, self.upgrade_count)

    @property
    def ref_text(self) -> str:
        return f"{self.card_id}@{self.upgrade_count}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveAccounting:
    universe_version_count: int
    compiled_version_count: int
    failed_version_count: int
    family_executable_version_count: int
    full_card_executable_version_count: int
    companion_gap_version_count: int
    companion_blocker_count: int
    family_blocker_count: int
    blocker_code_counts: tuple[tuple[str, int], ...]

    @property
    def blocker_code_map(self) -> dict[str, int]:
        return dict(self.blocker_code_counts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_version_count": self.universe_version_count,
            "compiled_version_count": self.compiled_version_count,
            "failed_version_count": self.failed_version_count,
            "family_executable_version_count": self.family_executable_version_count,
            "full_card_executable_version_count": self.full_card_executable_version_count,
            "companion_gap_version_count": self.companion_gap_version_count,
            "companion_blocker_count": self.companion_blocker_count,
            "family_blocker_count": self.family_blocker_count,
            "blocker_code_counts": self.blocker_code_map,
        }


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveCatalog:
    database: str
    versions: tuple[LessonDependAggressiveVersion, ...]
    blockers: tuple[LessonDependAggressiveBlocker, ...]
    universe_refs: tuple[tuple[str, int], ...]
    failed_refs: tuple[tuple[str, int], ...]
    accounting: LessonDependAggressiveAccounting

    def version(self, card_id: str, upgrade_count: int) -> LessonDependAggressiveVersion:
        ref = (card_id, upgrade_count)
        for version in self.versions:
            if version.ref == ref:
                return version
        raise KeyError(f"uncompiled LessonDependAggressive version: {card_id}@{upgrade_count}")

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(version.ref for version in self.versions)

    def blockers_for(self, card_id: str, upgrade_count: int) -> tuple[LessonDependAggressiveBlocker, ...]:
        ref = (card_id, upgrade_count)
        return tuple(blocker for blocker in self.blockers if blocker.ref == ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "versions": [_version_to_dict(version) for version in self.versions],
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "universe_refs": [_ref_to_dict(ref) for ref in self.universe_refs],
            "failed_refs": [_ref_to_dict(ref) for ref in self.failed_refs],
            "accounting": self.accounting.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveCompilation:
    schema_version: int
    catalog: LessonDependAggressiveCatalog
    blockers: tuple[LessonDependAggressiveBlocker, ...]

    @property
    def versions(self) -> tuple[LessonDependAggressiveVersion, ...]:
        return self.catalog.versions

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.compiled_refs

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.failed_refs

    @property
    def accounting(self) -> LessonDependAggressiveAccounting:
        return self.catalog.accounting

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(Counter(blocker.code for blocker in self.blockers))

    @property
    def fully_compiled(self) -> bool:
        return not self.failed_refs and self.accounting.family_blocker_count == 0

    def blockers_for(self, card_id: str, upgrade_count: int) -> tuple[LessonDependAggressiveBlocker, ...]:
        return self.catalog.blockers_for(card_id, upgrade_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog": self.catalog.to_dict(),
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveRuntime:
    current_card_play_aggressive: int
    is_buff_active: bool = True
    adding_status: AddingParameterStatus = AddingParameterStatus()
    adding_settings: AddingParameterSettings = AddingParameterSettings()
    additional: AddingParameterAdditionalData | None = None
    application_status: ParameterApplicationStatus = ParameterApplicationStatus(judge_parameter=0)

    def __post_init__(self) -> None:
        _i32(self.current_card_play_aggressive, "current_card_play_aggressive")
        _bool(self.is_buff_active, "is_buff_active")
        if not isinstance(self.adding_status, AddingParameterStatus):
            raise _contract("runtime-shape", "adding_status has the wrong type")
        if not isinstance(self.adding_settings, AddingParameterSettings):
            raise _contract("runtime-shape", "adding_settings has the wrong type")
        if self.additional is not None and not isinstance(self.additional, AddingParameterAdditionalData):
            raise _contract("runtime-shape", "additional has the wrong type")
        if not isinstance(self.application_status, ParameterApplicationStatus):
            raise _contract("runtime-shape", "application_status has the wrong type")


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveHandoff:
    version: LessonDependAggressiveVersion
    source_guid: str
    play_origin: PlayOrigin
    current_card_play_aggressive_snapshot: int
    target_effect: LessonDependAggressiveEffect
    order: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.version, LessonDependAggressiveVersion):
            raise _contract("handoff-version", "version has the wrong type")
        if type(self.order) is not tuple or any(not isinstance(item, str) for item in self.order):
            raise _contract("handoff-order", "execution order must be an immutable string tuple")
        _string(self.source_guid, "source_guid", non_empty=True)
        if not self.source_guid:
            raise _contract("handoff-source", "source_guid must be non-empty")
        if self.play_origin not in PLAY_ORIGINS:
            raise _contract("play-origin", repr(self.play_origin))
        _i32(self.current_card_play_aggressive_snapshot, "current_card_play_aggressive_snapshot")
        if self.target_effect != self.version.target_effect:
            raise _contract("handoff-target", "target effect is not the compiled version target")
        if not self.order:
            raise _contract("handoff-order", "execution order is empty")


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveEffectHandoff:
    """One statically validated target effect added at runtime."""

    source_guid: str
    play_origin: PlayOrigin
    current_card_play_aggressive_snapshot: int
    target_effect: LessonDependAggressiveEffect
    order: tuple[str, ...]

    def __post_init__(self) -> None:
        _string(self.source_guid, "source_guid", non_empty=True)
        if self.play_origin not in PLAY_ORIGINS:
            raise _contract("play-origin", repr(self.play_origin))
        _i32(
            self.current_card_play_aggressive_snapshot,
            "current_card_play_aggressive_snapshot",
        )
        if not isinstance(self.target_effect, LessonDependAggressiveEffect):
            raise _contract("handoff-target", "target effect is not typed")
        if type(self.order) is not tuple or any(
            not isinstance(item, str) or not item for item in self.order
        ):
            raise _contract(
                "handoff-order",
                "execution order must contain non-empty text",
            )


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveHit:
    index: int
    card_play_aggressive_snapshot: int
    requested: int
    calculated: int
    application: ParameterApplication


@dataclass(frozen=True, slots=True)
class LessonDependAggressiveExecution:
    handoff: (
        LessonDependAggressiveHandoff
        | LessonDependAggressiveEffectHandoff
    )
    before: LessonDependAggressiveRuntime
    after: LessonDependAggressiveRuntime
    hits: tuple[LessonDependAggressiveHit, ...]
    actual_lesson_parameter_added: int
    card_play_aggressive_after: int
    trace: tuple[str, ...]
    companion_blockers: tuple[LessonDependAggressiveBlocker, ...]

    @property
    def score_before(self) -> int:
        return self.before.application_status.judge_parameter

    @property
    def score_after(self) -> int:
        return self.after.application_status.judge_parameter

    @property
    def play_origin(self) -> PlayOrigin:
        return self.handoff.play_origin


def _target_id(value1: int) -> str:
    return f"e_effect-exam_lesson_depend_exam_card_play_aggressive-{value1:04d}-01"


def _slot(effect_id: str, effect_type: str, value1: int, value2: int = 0, count: int = 0, turn: int = 0) -> LessonDependAggressiveSlot:
    return LessonDependAggressiveSlot(effect_id, effect_type, value1, value2, count, turn)


def _target(value1: int) -> LessonDependAggressiveSlot:
    return _slot(_target_id(value1), TARGET_EFFECT_TYPE, value1, 0, 1, 0)


def _groups_for_effect_type(effect_type: str) -> tuple[str, ...]:
    if effect_type == TARGET_EFFECT_TYPE:
        return _TARGET_GROUPS
    if effect_type in (_EFFECT_BLOCK, _EFFECT_BLOCK_ADD_MULTIPLE, _EFFECT_LESSON_DEPEND_BLOCK):
        return _BLOCK_GROUPS if effect_type != _EFFECT_LESSON_DEPEND_BLOCK else _TARGET_GROUPS
    if effect_type == _EFFECT_CARD_PLAY_AGGRESSIVE:
        return _CARD_PLAY_GROUPS
    raise _contract("unknown-effect-type", effect_type)


def _expected_version(
    card_id: str,
    upgrade_count: int,
    name: str,
    stamina: int,
    cost_type: str,
    cost_value: int,
    play_trigger_id: str,
    asset_id: str,
    rarity: str,
    search_tag: str,
    is_initial_deck_produce_card: bool,
    max_customize_count: int,
    ordered_slots: tuple[LessonDependAggressiveSlot, ...],
    target_slot_index: int,
    raw_effect_group_ids: tuple[str, ...] | None = None,
) -> _ExpectedVersion:
    return _ExpectedVersion(
        card_id,
        upgrade_count,
        name,
        stamina,
        cost_type,
        cost_value,
        play_trigger_id,
        raw_effect_group_ids
        or ("effect_group-visible-exam_lesson_depend_block-000", "effect_group-visible-exam_lesson-000", "effect_group-visible-exam_block-000"),
        asset_id,
        rarity,
        search_tag,
        is_initial_deck_produce_card,
        max_customize_count,
        ordered_slots,
        target_slot_index,
    )


def _versions() -> tuple[_ExpectedVersion, ...]:
    versions: list[_ExpectedVersion] = []

    act0 = "p_card-02-act-0_038"
    for up, name, stamina, block_id, block_value, target_value in (
        (0, "仕草の基本", 2, "e_effect-exam_block-0003", 3, 1200),
        (1, "仕草の基本+", 2, "e_effect-exam_block-0006", 6, 1500),
        (2, "仕草の基本++", 1, "e_effect-exam_block-0006", 6, 1500),
        (3, "仕草の基本+++", 0, "e_effect-exam_block-0006", 6, 1500),
    ):
        versions.append(
            _expected_version(
                act0,
                up,
                name,
                stamina,
                "ExamCostType_Unknown",
                0,
                "",
                "img_general_skillcard_act-0_038",
                "ProduceCardRarity_N",
                "starter",
                True,
                0,
                (_slot(block_id, _EFFECT_BLOCK, block_value), _target(target_value)),
                1,
            )
        )

    act3 = "p_card-02-act-3_038"
    for up, name, play_id, play_value, target_value, max_customize in (
        (0, "開花", "e_effect-exam_card_play_aggressive-0006", 6, 2000, 0),
        (1, "開花+", "e_effect-exam_card_play_aggressive-0008", 8, 3000, 1),
        (2, "開花++", "e_effect-exam_card_play_aggressive-0009", 9, 3000, 1),
        (3, "開花+++", "e_effect-exam_card_play_aggressive-0011", 11, 3000, 1),
    ):
        versions.append(
            _expected_version(
                act3,
                up,
                name,
                5,
                "ExamCostType_Unknown",
                0,
                "",
                "img_general_skillcard_act-3_038",
                "ProduceCardRarity_Ssr",
                "",
                False,
                max_customize,
                (_slot(play_id, _EFFECT_CARD_PLAY_AGGRESSIVE, play_value), _target(target_value)),
                1,
                _TARGET_GROUPS + _CARD_PLAY_GROUPS,
            )
        )

    act045 = "p_card-02-act-3_045"
    for up, name, block_id, block_value, add_id, add_value, depend_id, depend_value, target_value in (
        (0, "あのときの約束", "e_effect-exam_block-0014", 14, None, 0, "e_effect-exam_lesson_depend_block-1400-01", 1400, 2000),
        (1, "あのときの約束+", "", 0, "e_effect-exam_block_add_multiple_aggressive-0020-0500-01", 500, "e_effect-exam_lesson_depend_block-1500-01", 1500, 2500),
        (2, "あのときの約束++", "", 0, "e_effect-exam_block_add_multiple_aggressive-0020-0500-01", 500, "e_effect-exam_lesson_depend_block-1700-01", 1700, 3000),
        (3, "あのときの約束+++", "", 0, "e_effect-exam_block_add_multiple_aggressive-0020-1000-01", 1000, "e_effect-exam_lesson_depend_block-1900-01", 1900, 3000),
    ):
        first = _slot(block_id, _EFFECT_BLOCK, block_value) if block_id else _slot(add_id or "", _EFFECT_BLOCK_ADD_MULTIPLE, 20, add_value, 1, 0)
        versions.append(
            _expected_version(
                act045,
                up,
                name,
                0,
                "ExamCostType_ExamReview",
                3,
                "e_trigger-none-card_play_aggressive_up-3",
                "img_general_skillcard_act-3_045",
                "ProduceCardRarity_Ssr",
                "",
                False,
                0 if up == 0 else 1,
                (first, _slot(depend_id, _EFFECT_LESSON_DEPEND_BLOCK, depend_value, 0, 1, 0), _target(target_value)),
                2,
            )
        )

    ido2 = "p_card-02-ido-2_021"
    for up, name, block_id, block_value, target_value in (
        (0, "苦しいのが好き", "e_effect-exam_block-0006", 6, 2500),
        (1, "苦しいのが好き+", "e_effect-exam_block-0007", 7, 3500),
        (2, "苦しいのが好き++", "e_effect-exam_block-0011", 11, 3500),
        (3, "苦しいのが好き+++", "e_effect-exam_block-0014", 14, 3500),
    ):
        versions.append(
            _expected_version(
                ido2,
                up,
                name,
                4,
                "ExamCostType_Unknown",
                0,
                "",
                "img_general_skillcard_ido-2_021",
                "ProduceCardRarity_Sr",
                "idol-unique",
                False,
                0,
                (_slot(block_id, _EFFECT_BLOCK, block_value), _target(target_value)),
                1,
            )
        )

    ido3 = "p_card-02-ido-3_086"
    for up, name, stamina, add_id, add_v1, add_v2, target_value in (
        (0, "愛を込めて", 6, "e_effect-exam_block_add_multiple_aggressive-0004-0800-01", 4, 800, 3000),
        (1, "愛を込めて+", 6, "e_effect-exam_block_add_multiple_aggressive-0006-1000-01", 6, 1000, 4000),
        (2, "愛を込めて++", 5, "e_effect-exam_block_add_multiple_aggressive-0006-1000-01", 6, 1000, 4000),
        (3, "愛を込めて+++", 4, "e_effect-exam_block_add_multiple_aggressive-0006-1000-01", 6, 1000, 4000),
    ):
        versions.append(
            _expected_version(
                ido3,
                up,
                name,
                stamina,
                "ExamCostType_Unknown",
                0,
                "",
                "img_general_skillcard_ido-3_086",
                "ProduceCardRarity_Ssr",
                "idol-unique",
                False,
                0,
                (_slot(add_id, _EFFECT_BLOCK_ADD_MULTIPLE, add_v1, add_v2, 1, 0), _target(target_value)),
                1,
            )
        )

    sup2 = "p_card-02-sup-2_094"
    for up, name, block_id, block_value, target_value in (
        (0, "ディテールが肝心", "e_effect-exam_block-0004", 4, 2700),
        (1, "ディテールが肝心+", "e_effect-exam_block-0007", 7, 3400),
        (2, "ディテールが肝心++", "e_effect-exam_block-0010", 10, 3400),
        (3, "ディテールが肝心+++", "e_effect-exam_block-0010", 10, 4000),
    ):
        versions.append(
            _expected_version(
                sup2,
                up,
                name,
                0,
                "ExamCostType_Unknown",
                0,
                "",
                "img_general_skillcard_sup-2_094",
                "ProduceCardRarity_Sr",
                "",
                False,
                0 if up == 0 else 2,
                (_slot(block_id, _EFFECT_BLOCK, block_value), _target(target_value)),
                1,
            )
        )
    return tuple(versions)


_EXPECTED_VERSIONS: Final = _versions()
_EXPECTED_REFS: Final = tuple(expected.ref for expected in _EXPECTED_VERSIONS)


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise _contract("master-schema", f"missing column {key!r}") from exc


def _fetch_one(connection: sqlite3.Connection, query: str, params: Sequence[Any], label: str) -> sqlite3.Row:
    try:
        row = connection.execute(query, params).fetchone()
    except sqlite3.Error as exc:
        raise _contract("master-schema", f"{label}: {exc}") from exc
    if row is None:
        raise _contract("master-missing", label)
    return row


def _validate_effect_raw(raw: Mapping[str, Any], slot: LessonDependAggressiveSlot) -> tuple[str, ...]:
    if frozenset(raw) != _RAW_EFFECT_KEYS:
        raise _contract("effect-raw-shape", f"{slot.effect_id}: key set differs")
    if raw.get("id") != slot.effect_id or raw.get("effectType") != slot.effect_type:
        raise _contract("effect-raw-core", f"{slot.effect_id}: id/type mismatch")
    for key, expected in (
        ("effectValue1", slot.value1),
        ("effectValue2", slot.value2),
        ("effectCount", slot.count),
        ("effectTurn", slot.turn),
    ):
        if raw.get(key) != expected:
            raise _contract("effect-raw-core", f"{slot.effect_id}: {key}={raw.get(key)!r}, expected {expected!r}")
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or tuple(groups) != _groups_for_effect_type(slot.effect_type):
        raise _contract("effect-raw-groups", f"{slot.effect_id}: {groups!r}")
    neutral = {
        "movePositionType": "ProduceCardMovePositionType_Unknown",
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": _UNKNOWN,
        "produceCardSearchId": "",
        "produceCardSearchId2": "",
        "pickRangeType": "ProducePickRangeType_Unknown",
        "pickRangeType2": "ProducePickRangeType_Unknown",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountType2": "ProducePickCountType_Unknown",
        "pickCountMin": 0,
        "pickCountMin2": 0,
        "pickCountMax": 0,
        "pickCountMax2": 0,
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountReferenceProduceCardSearchId2": "",
        "produceCardGrowEffectIds": [],
        "produceCardStatusEnchantId": "",
        "produceExamStatusEnchantId": "",
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
    }
    for key, expected in neutral.items():
        if raw.get(key) != expected:
            raise _contract("effect-raw-neutral", f"{slot.effect_id}: {key}={raw.get(key)!r}")
    descriptions = raw.get("produceDescriptions")
    custom = raw.get("customizeProduceDescriptions")
    if not isinstance(descriptions, list) or not isinstance(custom, list):
        raise _contract("effect-description-shape", f"{slot.effect_id}: descriptions are not arrays")
    if len(custom) != len(descriptions) + 1 or custom[1:] != descriptions:
        raise _contract("effect-description-shape", f"{slot.effect_id}: customize descriptions do not contain the native style prefix")
    first_custom = custom[0]
    if not isinstance(first_custom, dict) or (
        first_custom.get("produceDescriptionType") != "ProduceDescriptionType_ProduceDescriptionName"
        or first_custom.get("targetId") != "Label_StyleDot"
        or first_custom.get("text") != ""
    ):
        raise _contract("effect-description-shape", f"{slot.effect_id}: invalid customize style prefix")
    if slot.effect_type == TARGET_EFFECT_TYPE:
        _validate_target_descriptions(descriptions, slot.value1, slot.effect_id)
    else:
        _validate_companion_descriptions(descriptions, slot)
    return tuple(groups)


def _description_signature(description: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        description.get("produceDescriptionType"),
        description.get("examDescriptionType"),
        description.get("examEffectType"),
        description.get("effectValue1"),
        description.get("effectValue2"),
        description.get("effectCount"),
        description.get("turn"),
        description.get("targetId"),
        description.get("text"),
    )


def _validate_target_descriptions(descriptions: list[Any], value1: int, effect_id: str) -> None:
    if len(descriptions) != 7 or any(not isinstance(item, dict) for item in descriptions):
        raise _contract("effect-description-shape", f"{effect_id}: expected seven native description rows")
    expected = (
        ("ProduceDescriptionType_ProduceExamEffectType", "ExamDescriptionType_Unknown", _EFFECT_CARD_PLAY_AGGRESSIVE, 0, 0, 0, 0, "Label_ExamCardPlayAggressive_Produce", "やる気"),
        ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "の<nobr>"),
        ("ProduceDescriptionType_Exam", "ExamDescriptionType_CustomizeEffectValuePercent1", _UNKNOWN, value1, 0, 0, 0, "", str(value1 // 10)),
        ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "%</nobr>分"),
        ("ProduceDescriptionType_ProduceExamEffectType", "ExamDescriptionType_Unknown", "ProduceExamEffectType_ExamLesson", 0, 0, 0, 0, "Label_ExamLesson", "パラメータ"),
        ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "上昇"),
        ("ProduceDescriptionType_Exam", "ExamDescriptionType_CustomizeLessonCountAdd", _UNKNOWN, 0, 0, 0, 0, "Description_LessonCountAdd_CountSection", ""),
    )
    actual = tuple(_description_signature(item) for item in descriptions)
    if actual != expected:
        raise _contract("effect-description-shape", f"{effect_id}: description signature differs")


def _validate_companion_descriptions(descriptions: list[Any], slot: LessonDependAggressiveSlot) -> None:
    if slot.effect_type in (_EFFECT_BLOCK, _EFFECT_CARD_PLAY_AGGRESSIVE):
        label = "Label_ExamBlock" if slot.effect_type == _EFFECT_BLOCK else "Label_ExamCardPlayAggressive_Produce"
        effect_label = _EFFECT_BLOCK if slot.effect_type == _EFFECT_BLOCK else _EFFECT_CARD_PLAY_AGGRESSIVE
        expected = (
            ("ProduceDescriptionType_ProduceExamEffectType", "ExamDescriptionType_Unknown", effect_label, 0, 0, 0, 0, label, "元気" if slot.effect_type == _EFFECT_BLOCK else "やる気"),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "<nobr>+"),
            ("ProduceDescriptionType_Exam", "ExamDescriptionType_CustomizeEffectValue1", _UNKNOWN, slot.value1, 0, 0, 0, "", str(slot.value1)),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "</nobr>"),
        )
    elif slot.effect_type == _EFFECT_BLOCK_ADD_MULTIPLE:
        multiplier = f"{1 + slot.value2 / 1000:g}"
        expected = (
            ("ProduceDescriptionType_ProduceExamEffectType", "ExamDescriptionType_Unknown", _EFFECT_BLOCK, 0, 0, 0, 0, "Label_ExamBlock", "元気"),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "<nobr>+"),
            ("ProduceDescriptionType_Exam", "ExamDescriptionType_CustomizeEffectValue1", _UNKNOWN, slot.value1, 0, 0, 0, "", str(slot.value1)),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "</nobr>（"),
            ("ProduceDescriptionType_ProduceExamEffectType", "ExamDescriptionType_Unknown", _EFFECT_CARD_PLAY_AGGRESSIVE, 0, 0, 0, 0, "Label_ExamCardPlayAggressive_Produce", "やる気"),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", f"効果を<nobr>{multiplier}倍</nobr>適用）"),
        )
    elif slot.effect_type == _EFFECT_LESSON_DEPEND_BLOCK:
        expected = (
            ("ProduceDescriptionType_ProduceExamEffectType", "ExamDescriptionType_Unknown", _EFFECT_BLOCK, 0, 0, 0, 0, "Label_ExamBlock", "元気"),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "の<nobr>"),
            ("ProduceDescriptionType_Exam", "ExamDescriptionType_CustomizeEffectValuePercent1", _UNKNOWN, slot.value1, 0, 0, 0, "", str(slot.value1 // 10)),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "%</nobr>分"),
            ("ProduceDescriptionType_ProduceExamEffectType", "ExamDescriptionType_Unknown", "ProduceExamEffectType_ExamLesson", 0, 0, 0, 0, "Label_ExamLesson", "パラメータ"),
            ("ProduceDescriptionType_PlainText", "ExamDescriptionType_Unknown", _UNKNOWN, 0, 0, 0, 0, "", "上昇"),
            ("ProduceDescriptionType_Exam", "ExamDescriptionType_CustomizeLessonCountAdd", _UNKNOWN, 0, 0, 0, 0, "Description_LessonCountAdd_CountSection", ""),
        )
    else:  # pragma: no cover - guarded by _groups_for_effect_type
        raise _contract("unknown-effect-type", slot.effect_type)
    if len(descriptions) != len(expected) or tuple(_description_signature(item) for item in descriptions) != expected:
        raise _contract("effect-description-shape", f"{slot.effect_id}: companion description signature differs")


def _validate_effect(connection: sqlite3.Connection, slot: LessonDependAggressiveSlot) -> tuple[str, ...]:
    row = _fetch_one(
        connection,
        """
        SELECT id, effect_type, value1 AS effect_value1, value2 AS effect_value2, effect_count,
               effect_turn, status_enchant_id, chain_effect_id, raw_json
        FROM effect WHERE id = ?
        """,
        (slot.effect_id,),
        f"effect {slot.effect_id}",
    )
    values = {
        "effect_type": _row_value(row, "effect_type"),
        "effect_value1": _row_value(row, "effect_value1"),
        "effect_value2": _row_value(row, "effect_value2"),
        "effect_count": _row_value(row, "effect_count"),
        "effect_turn": _row_value(row, "effect_turn"),
        "status_enchant_id": _row_value(row, "status_enchant_id"),
        "chain_effect_id": _row_value(row, "chain_effect_id"),
    }
    expected = {
        "effect_type": slot.effect_type,
        "effect_value1": slot.value1,
        "effect_value2": slot.value2,
        "effect_count": slot.count,
        "effect_turn": slot.turn,
        "status_enchant_id": "",
        "chain_effect_id": "",
    }
    if values != expected:
        raise _contract("effect-normalized-shape", f"{slot.effect_id}: {values!r} != {expected!r}")
    raw = _json_object(_row_value(row, "raw_json"), f"effect {slot.effect_id}.raw_json")
    groups = _validate_effect_raw(raw, slot)
    return groups


@lru_cache(maxsize=None)
def load_plan2_native_lesson_depend_aggressive_effect(
    effect_id: str,
    *,
    database: str | Path = DEFAULT_DATABASE,
) -> LessonDependAggressiveEffect:
    """Load one target leaf through the catalog's existing strict validator."""

    _string(effect_id, "effect_id", non_empty=True)
    database_path = Path(database).resolve()
    connection = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        row = _fetch_one(
            connection,
            """
            SELECT id, effect_type, value1, value2, effect_count, effect_turn
              FROM effect
             WHERE id = ?
            """,
            (effect_id,),
            f"effect {effect_id}",
        )
        slot = LessonDependAggressiveSlot(
            effect_id=str(_row_value(row, "id")),
            effect_type=str(_row_value(row, "effect_type")),
            value1=_i32(_row_value(row, "value1"), f"{effect_id}.value1"),
            value2=_i32(_row_value(row, "value2"), f"{effect_id}.value2"),
            count=_i32(
                _row_value(row, "effect_count"),
                f"{effect_id}.effect_count",
            ),
            turn=_i32(
                _row_value(row, "effect_turn"),
                f"{effect_id}.effect_turn",
            ),
        )
        groups = _validate_effect(connection, slot)
        return LessonDependAggressiveEffect(
            effect_id=slot.effect_id,
            effect_type=slot.effect_type,
            value1=slot.value1,
            value2=slot.value2,
            count=slot.count,
            turn=slot.turn,
            effect_group_ids=groups,
        )
    finally:
        connection.close()


def _validate_card(connection: sqlite3.Connection, expected: _ExpectedVersion) -> LessonDependAggressiveVersion:
    row = _fetch_one(
        connection,
        """
        SELECT id, upgrade_count, name, plan_type, category, stamina, cost_type,
               cost_value, move_position_type AS play_move_position_type,
               play_trigger_id AS play_produce_exam_trigger_id,
               play_effects_json, raw_json
        FROM card WHERE id = ? AND upgrade_count = ?
        """,
        (expected.card_id, expected.upgrade_count),
        f"card {expected.card_id}@{expected.upgrade_count}",
    )
    normalized = {
        "id": _row_value(row, "id"),
        "upgrade_count": _row_value(row, "upgrade_count"),
        "name": _row_value(row, "name"),
        "plan_type": _row_value(row, "plan_type"),
        "category": _row_value(row, "category"),
        "stamina": _row_value(row, "stamina"),
        "cost_type": _row_value(row, "cost_type"),
        "cost_value": _row_value(row, "cost_value"),
        "play_move_position_type": _row_value(row, "play_move_position_type"),
        "play_produce_exam_trigger_id": _row_value(row, "play_produce_exam_trigger_id"),
    }
    expected_normalized = {
        "id": expected.card_id,
        "upgrade_count": expected.upgrade_count,
        "name": expected.name,
        "plan_type": PLAN2,
        "category": ACTIVE_SKILL,
        "stamina": expected.stamina,
        "cost_type": expected.cost_type,
        "cost_value": expected.cost_value,
        "play_move_position_type": MOVE_LOST,
        "play_produce_exam_trigger_id": expected.play_trigger_id,
    }
    if normalized != expected_normalized:
        raise _contract("card-normalized-shape", f"{expected.card_id}@{expected.upgrade_count}: {normalized!r}")

    raw = _json_object(_row_value(row, "raw_json"), f"card {expected.card_id}@{expected.upgrade_count}.raw_json")
    if frozenset(raw) != _RAW_CARD_KEYS:
        raise _contract("card-raw-shape", f"{expected.card_id}@{expected.upgrade_count}: key set differs")
    raw_core = {
        "id": raw.get("id"),
        "upgradeCount": raw.get("upgradeCount"),
        "name": raw.get("name"),
        "planType": raw.get("planType"),
        "category": raw.get("category"),
        "stamina": raw.get("stamina"),
        "costType": raw.get("costType"),
        "costValue": raw.get("costValue"),
        "playMovePositionType": raw.get("playMovePositionType"),
        "playProduceExamTriggerId": raw.get("playProduceExamTriggerId"),
        "assetId": raw.get("assetId"),
        "rarity": raw.get("rarity"),
        "searchTag": raw.get("searchTag"),
        "isInitialDeckProduceCard": raw.get("isInitialDeckProduceCard"),
        "maxCustomizeCount": raw.get("maxCustomizeCount"),
        "moveEffectTriggerType": raw.get("moveEffectTriggerType"),
        "effectGroupIds": raw.get("effectGroupIds"),
    }
    expected_core = {
        "id": expected.card_id,
        "upgradeCount": expected.upgrade_count,
        "name": expected.name,
        "planType": PLAN2,
        "category": ACTIVE_SKILL,
        "stamina": expected.stamina,
        "costType": expected.cost_type,
        "costValue": expected.cost_value,
        "playMovePositionType": MOVE_LOST,
        "playProduceExamTriggerId": expected.play_trigger_id,
        "assetId": expected.asset_id,
        "rarity": expected.rarity,
        "searchTag": expected.search_tag,
        "isInitialDeckProduceCard": expected.is_initial_deck_produce_card,
        "maxCustomizeCount": expected.max_customize_count,
        "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
        "effectGroupIds": list(expected.raw_effect_group_ids),
    }
    if raw_core != expected_core:
        raise _contract("card-raw-core", f"{expected.card_id}@{expected.upgrade_count}: {raw_core!r}")

    raw_play_effects = _json_array(_row_value(row, "play_effects_json"), f"card {expected.card_id}.play_effects_json")
    raw_play_effects_from_raw = raw.get("playEffects")
    if raw_play_effects_from_raw != raw_play_effects:
        raise _contract("card-play-effects-shape", f"{expected.card_id}@{expected.upgrade_count}: normalized/raw mismatch")
    expected_play_effects = [
        {
            "produceExamTriggerId": slot.trigger_id,
            "produceExamEffectId": slot.effect_id,
            "hideIcon": slot.hide_icon,
            "isOncePlayEffect": slot.is_once_play_effect,
        }
        for slot in expected.ordered_slots
    ]
    if raw_play_effects != expected_play_effects:
        raise _contract("card-play-effects-shape", f"{expected.card_id}@{expected.upgrade_count}: order/rows differ")

    effects: list[LessonDependAggressiveSlot] = []
    for slot in expected.ordered_slots:
        _validate_effect(connection, slot)
        effects.append(slot)
        if slot.effect_type == TARGET_EFFECT_TYPE and slot != expected.ordered_slots[expected.target_slot_index]:
            raise _contract("target-cardinality", f"{expected.card_id}@{expected.upgrade_count}: unexpected target slot")
    target_slots = [index for index, slot in enumerate(effects) if slot.effect_type == TARGET_EFFECT_TYPE]
    if target_slots != [expected.target_slot_index]:
        raise _contract("target-cardinality", f"{expected.card_id}@{expected.upgrade_count}: target slots={target_slots!r}")
    target_slot = expected.ordered_slots[expected.target_slot_index]
    target_groups = _validate_effect(connection, target_slot)
    target_effect = LessonDependAggressiveEffect(
        target_slot.effect_id,
        target_slot.effect_type,
        target_slot.value1,
        target_slot.value2,
        target_slot.count,
        target_slot.turn,
        target_groups,
    )
    return LessonDependAggressiveVersion(
        expected.card_id,
        expected.upgrade_count,
        expected.name,
        expected.stamina,
        expected.cost_type,
        expected.cost_value,
        expected.play_trigger_id,
        tuple(effects),
        expected.target_slot_index,
        target_effect,
        expected.asset_id,
        expected.rarity,
        expected.search_tag,
        expected.is_initial_deck_produce_card,
        expected.max_customize_count,
    )


def _discover_target_refs(connection: sqlite3.Connection) -> tuple[tuple[str, int], ...]:
    try:
        rows = connection.execute(
            "SELECT id, upgrade_count, play_effects_json FROM card WHERE plan_type = ?",
            (PLAN2,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise _contract("master-schema", f"card universe query: {exc}") from exc
    discovered: list[tuple[str, int]] = []
    for row in rows:
        card_id = _row_value(row, "id")
        upgrade_count = _row_value(row, "upgrade_count")
        play_effects = _json_array(_row_value(row, "play_effects_json"), f"card {card_id}.play_effects_json")
        effect_ids = []
        for item in play_effects:
            if not isinstance(item, dict) or not isinstance(item.get("produceExamEffectId"), str):
                raise _contract("card-play-effects-shape", f"{card_id}@{upgrade_count}: unknown play effect row")
            effect_ids.append(item["produceExamEffectId"])
        if not effect_ids:
            continue
        placeholders = ",".join("?" for _ in effect_ids)
        effect_rows = connection.execute(
            f"SELECT id, effect_type FROM effect WHERE id IN ({placeholders})",  # noqa: S608 - placeholders are generated, values are bound
            effect_ids,
        ).fetchall()
        effect_types = {row["id"]: row["effect_type"] for row in effect_rows}
        if any(effect_id not in effect_types for effect_id in effect_ids):
            raise _contract("master-missing", f"{card_id}@{upgrade_count}: play effect is absent")
        if TARGET_EFFECT_TYPE in (effect_types[effect_id] for effect_id in effect_ids):
            discovered.append((card_id, upgrade_count))
    return tuple(sorted(discovered))


def _companion_blockers(version: LessonDependAggressiveVersion) -> tuple[LessonDependAggressiveBlocker, ...]:
    blockers = [
        LessonDependAggressiveBlocker(
            "companion-effect-unbound",
            version.card_id,
            version.upgrade_count,
            f"slot={index};effect_id={slot.effect_id};effect_type={slot.effect_type}",
        )
        for index, slot in enumerate(version.ordered_effects)
        if index != version.target_slot_index
    ]
    if version.play_trigger_id:
        blockers.append(
            LessonDependAggressiveBlocker(
                "external-card-play-trigger",
                version.card_id,
                version.upgrade_count,
                version.play_trigger_id,
            )
        )
    if version.cost_type != "ExamCostType_Unknown" or version.cost_value != 0:
        blockers.append(
            LessonDependAggressiveBlocker(
                "external-card-cost-policy",
                version.card_id,
                version.upgrade_count,
                f"{version.cost_type}:{version.cost_value}",
            )
        )
    return tuple(blockers)


def _make_accounting(
    universe_refs: tuple[tuple[str, int], ...],
    versions: tuple[LessonDependAggressiveVersion, ...],
    blockers: tuple[LessonDependAggressiveBlocker, ...],
    failed_refs: tuple[tuple[str, int], ...],
) -> LessonDependAggressiveAccounting:
    companion_blockers = tuple(blocker for blocker in blockers if blocker.code.startswith("companion-") or blocker.code.startswith("external-"))
    family_blockers = tuple(blocker for blocker in blockers if blocker not in companion_blockers)
    version_refs = {version.ref for version in versions}
    companion_gap_refs = {blocker.ref for blocker in companion_blockers}
    code_counts = Counter(blocker.code for blocker in blockers)
    return LessonDependAggressiveAccounting(
        universe_version_count=len(universe_refs),
        compiled_version_count=len(versions),
        failed_version_count=len(failed_refs),
        family_executable_version_count=len(versions),
        full_card_executable_version_count=len(version_refs - companion_gap_refs),
        companion_gap_version_count=len(version_refs & companion_gap_refs),
        companion_blocker_count=len(companion_blockers),
        family_blocker_count=len(family_blockers),
        blocker_code_counts=tuple(sorted(code_counts.items())),
    )


def compile_plan2_native_lesson_depend_aggressive_catalog(
    database: str | Path = DEFAULT_DATABASE,
) -> LessonDependAggressiveCompilation:
    """Compile the bounded 24-version Master contract without central registration."""

    database_path = str(database)
    try:
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise _contract("master-open", f"{database_path}: {exc}") from exc
    try:
        discovered_refs = _discover_target_refs(connection)
        if discovered_refs != tuple(sorted(_EXPECTED_REFS)):
            raise _contract(
                "catalog-universe-drift",
                f"target refs={discovered_refs!r}, expected={tuple(sorted(_EXPECTED_REFS))!r}",
            )
        compiled: list[LessonDependAggressiveVersion] = []
        blockers: list[LessonDependAggressiveBlocker] = []
        failed: list[tuple[str, int]] = []
        for expected in _EXPECTED_VERSIONS:
            try:
                version = _validate_card(connection, expected)
            except Plan2LessonDependAggressiveContractError as exc:
                failed.append(expected.ref)
                blockers.append(
                    LessonDependAggressiveBlocker(
                        f"family-compile-{exc.code}",
                        expected.card_id,
                        expected.upgrade_count,
                        exc.detail,
                    )
                )
                continue
            compiled.append(version)
            blockers.extend(_companion_blockers(version))
        version_tuple = tuple(compiled)
        blocker_tuple = tuple(blockers)
        accounting = _make_accounting(tuple(_EXPECTED_REFS), version_tuple, blocker_tuple, tuple(failed))
        catalog = LessonDependAggressiveCatalog(
            database_path,
            version_tuple,
            blocker_tuple,
            tuple(_EXPECTED_REFS),
            tuple(failed),
            accounting,
        )
        return LessonDependAggressiveCompilation(SCHEMA_VERSION, catalog, blocker_tuple)
    finally:
        connection.close()


load_plan2_native_lesson_depend_aggressive_catalog = compile_plan2_native_lesson_depend_aggressive_catalog


def _resolve_version(
    catalog_or_version: LessonDependAggressiveCompilation | LessonDependAggressiveCatalog | LessonDependAggressiveVersion,
    card_id: str | None = None,
    upgrade_count: int | None = None,
) -> LessonDependAggressiveVersion:
    if isinstance(catalog_or_version, LessonDependAggressiveVersion):
        return catalog_or_version
    if card_id is None or upgrade_count is None:
        raise _contract("version-ref", "card_id and upgrade_count are required for a catalog handoff")
    catalog = catalog_or_version.catalog if isinstance(catalog_or_version, LessonDependAggressiveCompilation) else catalog_or_version
    return catalog.version(card_id, upgrade_count)


def build_plan2_native_lesson_depend_aggressive_handoff(
    catalog_or_version: LessonDependAggressiveCompilation | LessonDependAggressiveCatalog | LessonDependAggressiveVersion,
    source_guid: str,
    play_origin: PlayOrigin,
    current_card_play_aggressive: int,
    *,
    card_id: str | None = None,
    upgrade_count: int | None = None,
) -> LessonDependAggressiveHandoff:
    """Capture the execution-time CardPlayAggressive snapshot exactly once."""

    version = _resolve_version(catalog_or_version, card_id, upgrade_count)
    snapshot = _i32(current_card_play_aggressive, "current_card_play_aggressive")
    return LessonDependAggressiveHandoff(
        version=version,
        source_guid=source_guid,
        play_origin=play_origin,
        current_card_play_aggressive_snapshot=snapshot,
        target_effect=version.target_effect,
        order=(
            "bind-card-version",
            "capture-execution-time-card-play-aggressive-once",
            "target-effect",
        ),
    )


def build_plan2_native_lesson_depend_aggressive_effect_handoff(
    effect: LessonDependAggressiveEffect,
    source_guid: str,
    play_origin: PlayOrigin,
    current_card_play_aggressive: int,
) -> LessonDependAggressiveEffectHandoff:
    """Bind one runtime-added target leaf to the shared native executor."""

    if not isinstance(effect, LessonDependAggressiveEffect):
        raise _contract("handoff-target", "target effect is not typed")
    return LessonDependAggressiveEffectHandoff(
        source_guid=source_guid,
        play_origin=play_origin,
        current_card_play_aggressive_snapshot=_i32(
            current_card_play_aggressive,
            "current_card_play_aggressive",
        ),
        target_effect=effect,
        order=(
            "bind-runtime-added-target-effect",
            "capture-execution-time-card-play-aggressive-once",
            "target-effect",
        ),
    )


def _native_ceil_ratio(value: int, permille: int) -> int:
    try:
        ratio = permille_to_f32(_i32(permille, "permille"))
        product = f32(ratio * f32(_i32(value, "value")))
        return ceil_f32_to_i32(f32(product + F32_NEGATIVE_EPSILON))
    except NativeFormulaDomainError as exc:
        raise _contract("native-arithmetic-domain", str(exc)) from exc


def _native_retained_aggressive(value: int, retention_permille: int) -> int:
    try:
        retention = f32(f32(1.0) - permille_to_f32(_i32(retention_permille, "retention_permille")))
        return ceil_f32_to_i32(f32(retention * f32(_i32(value, "value"))))
    except NativeFormulaDomainError as exc:
        raise _contract("native-arithmetic-domain", str(exc)) from exc


def execute_plan2_native_lesson_depend_aggressive(
    handoff: (
        LessonDependAggressiveHandoff
        | LessonDependAggressiveEffectHandoff
    ),
    runtime: LessonDependAggressiveRuntime | None = None,
) -> LessonDependAggressiveExecution:
    """Execute the target leaf in native order against an immutable handoff."""

    if runtime is None:
        runtime = LessonDependAggressiveRuntime(handoff.current_card_play_aggressive_snapshot)
    if runtime.current_card_play_aggressive != handoff.current_card_play_aggressive_snapshot:
        raise _contract(
            "snapshot-mismatch",
            f"runtime={runtime.current_card_play_aggressive}, handoff={handoff.current_card_play_aggressive_snapshot}",
        )
    effect = handoff.target_effect
    bound_adding_status = replace(
        runtime.adding_status,
        aggressive=handoff.current_card_play_aggressive_snapshot,
    )
    trace = list(handoff.order)
    trace.extend(
        (
            "target:FloatFromPermil(value1)-binary32",
            "target:FMUL(snapshot)-binary32",
            "target:F32_NEGATIVE_EPSILON+FRINTP+FCVTPS",
        )
    )
    requested = _native_ceil_ratio(
        handoff.current_card_play_aggressive_snapshot,
        effect.value1,
    )
    current_application_status = runtime.application_status
    hits: list[LessonDependAggressiveHit] = []
    trace.append("target:CalculateAddingParameter(isBuffActive/additive/restriction/cap)")
    for index in range(effect.count):
        try:
            calculated = calculate_adding_parameter(
                requested,
                is_buff_active=runtime.is_buff_active,
                status=bound_adding_status,
                settings=runtime.adding_settings,
                additional=runtime.additional,
            )
            application = apply_parameter_add(calculated, status=current_application_status)
        except NativeFormulaDomainError as exc:
            raise _contract("native-arithmetic-domain", str(exc)) from exc
        hits.append(
            LessonDependAggressiveHit(
                index=index,
                card_play_aggressive_snapshot=handoff.current_card_play_aggressive_snapshot,
                requested=requested,
                calculated=calculated,
                application=application,
            )
        )
        current_application_status = replace(
            current_application_status,
            judge_parameter=application.after,
            current_turn_total_add_parameter=application.current_turn_total_add_parameter,
            judge_parameter_vocal=application.judge_parameter_vocal,
            judge_parameter_dance=application.judge_parameter_dance,
            judge_parameter_visual=application.judge_parameter_visual,
        )
        trace.extend(("target:AddParameter-per-hit", "target:commit-score-after-each-AddParameter"))
    trace.append("target:value2-retention-after-score-hits")
    aggressive_after = _native_retained_aggressive(
        handoff.current_card_play_aggressive_snapshot,
        effect.value2,
    )
    after = replace(
        runtime,
        current_card_play_aggressive=aggressive_after,
        adding_status=replace(runtime.adding_status, aggressive=aggressive_after),
        application_status=current_application_status,
    )
    trace.append("handoff:companion-effects-and-card-settlement-external")
    blockers = (
        _companion_blockers(handoff.version)
        if isinstance(handoff, LessonDependAggressiveHandoff)
        else ()
    )
    return LessonDependAggressiveExecution(
        handoff=handoff,
        before=runtime,
        after=after,
        hits=tuple(hits),
        actual_lesson_parameter_added=sum(hit.application.actual_parameter for hit in hits),
        card_play_aggressive_after=aggressive_after,
        trace=tuple(trace),
        companion_blockers=blockers,
    )


def _ref_to_dict(ref: tuple[str, int]) -> dict[str, Any]:
    return {"card_id": ref[0], "upgrade_count": ref[1]}


def _slot_to_dict(slot: LessonDependAggressiveSlot) -> dict[str, Any]:
    return {
        "effect_id": slot.effect_id,
        "effect_type": slot.effect_type,
        "value1": slot.value1,
        "value2": slot.value2,
        "count": slot.count,
        "turn": slot.turn,
        "trigger_id": slot.trigger_id,
        "hide_icon": slot.hide_icon,
        "is_once_play_effect": slot.is_once_play_effect,
    }


def _version_to_dict(version: LessonDependAggressiveVersion) -> dict[str, Any]:
    return {
        "card_id": version.card_id,
        "upgrade_count": version.upgrade_count,
        "name": version.name,
        "stamina": version.stamina,
        "cost_type": version.cost_type,
        "cost_value": version.cost_value,
        "play_trigger_id": version.play_trigger_id,
        "target_slot_index": version.target_slot_index,
        "ordered_effects": [_slot_to_dict(slot) for slot in version.ordered_effects],
        "target_effect": {
            "effect_id": version.target_effect.effect_id,
            "value1": version.target_effect.value1,
            "value2": version.target_effect.value2,
            "count": version.target_effect.count,
            "turn": version.target_effect.turn,
        },
        "asset_id": version.asset_id,
        "rarity": version.rarity,
        "search_tag": version.search_tag,
        "is_initial_deck_produce_card": version.is_initial_deck_produce_card,
        "max_customize_count": version.max_customize_count,
    }


def build_plan2_native_lesson_depend_aggressive_audit(
    compilation: LessonDependAggressiveCompilation | None = None,
) -> dict[str, Any]:
    """Return the JSON-serializable audit payload used by the checked-in report."""

    compilation = compilation or compile_plan2_native_lesson_depend_aggressive_catalog()
    version_accounting = []
    for ref in compilation.catalog.universe_refs:
        blockers = compilation.blockers_for(*ref)
        version_accounting.append(
            {
                "ref": f"{ref[0]}@{ref[1]}",
                "compiled": ref in compilation.compiled_refs,
                "family_blocker_count": sum(blocker.code.startswith("family-") for blocker in blockers),
                "companion_blocker_count": sum(not blocker.code.startswith("family-") for blocker in blockers),
                "blocker_codes": sorted(blocker.code for blocker in blockers),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "target_effect_type": TARGET_EFFECT_TYPE,
        "target_version_count": TARGET_VERSION_COUNT,
        "catalog": compilation.catalog.to_dict(),
        "compiled_version_rows": [_version_to_dict(version) for version in compilation.versions],
        "version_accounting": version_accounting,
        "accounting": compilation.accounting.to_dict(),
        "play_origin_consistency": {
            "origins": list(PLAY_ORIGINS),
            "same_target_leaf_order": True,
            "same_snapshot_rule": True,
            "payment_trigger_and_companion_effects": "external blockers remain explicit",
        },
        "boundary_policy": {
            "zero": "native FloatFromPermil/binary32 path is executed; no special zero shortcut",
            "signed_i32": "inputs are checked as signed i32; non-representable native ceil conversion fails closed",
            "retention": "value2 retention is committed after score hits, even though Master target value2 is zero",
        },
        "unknown_or_altered_shape": "strict key/core/order/group/description checks fail closed into family blockers",
        "companion_gap_statement": "24 target family versions compile; companion effects and card play settlement are not claimed complete",
        "forbidden_surfaces": [
            "src/gkms_tool/plan2_native_program_catalog.py",
            "src/gkms_tool/plan2_native_horizon.py",
            "src/gkms_tool/plan2_core_runtime.py",
            "formal coverage",
            "Plan3",
            "GUI/controller",
        ],
        "runtime_calls_agent": False,
        "verification_scope": "focused adapter tests only; full/security/hash/time runs not performed",
    }


__all__ = [
    "ACTIVE_SKILL",
    "LessonDependAggressiveAccounting",
    "LessonDependAggressiveBlocker",
    "LessonDependAggressiveCatalog",
    "LessonDependAggressiveCompilation",
    "LessonDependAggressiveContractError",
    "LessonDependAggressiveEffect",
    "LessonDependAggressiveExecution",
    "LessonDependAggressiveEffectHandoff",
    "LessonDependAggressiveHandoff",
    "LessonDependAggressiveHit",
    "LessonDependAggressiveRuntime",
    "LessonDependAggressiveSlot",
    "LessonDependAggressiveVersion",
    "MOVE_LOST",
    "PLAN2",
    "PLAY_ORIGINS",
    "TARGET_EFFECT_TYPE",
    "TARGET_VERSION_COUNT",
    "build_plan2_native_lesson_depend_aggressive_audit",
    "build_plan2_native_lesson_depend_aggressive_effect_handoff",
    "build_plan2_native_lesson_depend_aggressive_handoff",
    "compile_plan2_native_lesson_depend_aggressive_catalog",
    "execute_plan2_native_lesson_depend_aggressive",
    "load_plan2_native_lesson_depend_aggressive_catalog",
    "load_plan2_native_lesson_depend_aggressive_effect",
]
