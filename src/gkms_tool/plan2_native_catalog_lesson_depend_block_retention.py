"""Bounded native adapter for the eight LessonDependBlock retention cards.

This leaf owns only the eight Plan2 ActiveSkill versions whose native
``ExamLessonDependBlock`` effect has ``value2`` 500 or 1000.  It validates the
Master rows and raw native-shaped JSON before exposing immutable typed values.
The execution seam captures Block once, applies the lesson score, commits
each score hit, then performs the value2 Block retention/consumption commit.

The module intentionally has no registration side effect and does not import
the central program catalog, horizon, core/formal coverage, Plan3, or GUI
surfaces.  Runtime execution is pure over an immutable handoff and runtime;
it never calls an agent or performs process-memory access.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Any, Final, Literal, Mapping, Sequence

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
TARGET_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamLessonDependBlock"
PLAY_ORIGINS: Final = ("normal", "forced", "extra")
TARGET_VERSION_COUNT: Final = 8
SCHEMA_VERSION: Final = 1

PlayOrigin = Literal["normal", "forced", "extra"]

_UNKNOWN: Final = "ProduceExamEffectType_Unknown"
_TARGET_GROUPS: Final = (
    "effect_group-visible-exam_lesson_depend_block-000",
    "effect_group-visible-exam_lesson-000",
)
_MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
_UNKNOWN_PICK_RANGE: Final = "ProducePickRangeType_Unknown"
_UNKNOWN_PICK_COUNT: Final = "ProducePickCountType_Unknown"
_MOVE_EFFECT_TRIGGER_UNKNOWN: Final = "ProduceCardMoveEffectTriggerType_Unknown"
_UNKNOWN_COST: Final = "ExamCostType_Unknown"

HANDOFF_ORDER: Final = (
    "bind-card-version",
    "capture-execution-time-block-once",
    "target-effect",
)

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

_DESCRIPTION_KEYS: Final = frozenset(
    {
        "produceDescriptionType",
        "examDescriptionType",
        "examEffectType",
        "produceCardGrowEffectType",
        "produceCardCategory",
        "produceCardMovePositionType",
        "produceStepType",
        "produceStepBusinessType",
        "text",
        "targetId",
        "targetLevel",
        "effectValue1",
        "effectValue2",
        "effectCount",
        "turn",
        "costValue",
        "produceDescriptionSwapId",
        "originProduceExamTriggerId",
        "originProduceExamEffectId",
        "originProduceCardStatusEnchantId",
        "isCost",
        "isOnlyOutGame",
        "changeColor",
    }
)


class Plan2NativeCatalogLessonDependBlockRetentionError(ValueError):
    """A bounded Master/native shape or runtime value is unsupported."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


LessonDependBlockRetentionContractError = (
    Plan2NativeCatalogLessonDependBlockRetentionError
)
Plan2NativeLessonDependBlockRetentionContractError = (
    Plan2NativeCatalogLessonDependBlockRetentionError
)


def _contract(
    code: str, detail: str
) -> Plan2NativeCatalogLessonDependBlockRetentionError:
    return Plan2NativeCatalogLessonDependBlockRetentionError(code, detail)


def _i32(value: Any, label: str) -> int:
    if type(value) is not int or value < -(2**31) or value > 2**31 - 1:
        raise _contract("i32-domain", f"{label}={value!r}")
    return value


def _i32_wrap(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


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
class LessonDependBlockRetentionSlot:
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
        _i32(self.value1, f"{self.effect_id}.value1")
        _i32(self.value2, f"{self.effect_id}.value2")
        _i32(self.count, f"{self.effect_id}.count")
        _i32(self.turn, f"{self.effect_id}.turn")
        _string(self.trigger_id, "trigger_id")
        _bool(self.hide_icon, "hide_icon")
        _bool(self.is_once_play_effect, "is_once_play_effect")


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _string(self.effect_id, "target.effect_id", non_empty=True)
        if self.effect_type != TARGET_EFFECT_TYPE:
            raise _contract("target-effect-type", self.effect_type)
        if type(self.effect_group_ids) is not tuple or any(
            type(item) is not str or not item for item in self.effect_group_ids
        ):
            raise _contract("target-effect-groups", self.effect_id)
        if self.effect_group_ids != _TARGET_GROUPS:
            raise _contract("target-effect-groups", f"{self.effect_id}: {self.effect_group_ids!r}")
        _i32(self.value1, f"{self.effect_id}.value1")
        _i32(self.value2, f"{self.effect_id}.value2")
        _i32(self.count, f"{self.effect_id}.count")
        _i32(self.turn, f"{self.effect_id}.turn")
        if self.value2 not in (500, 1000) or self.count != 1 or self.turn != 0:
            raise _contract(
                "target-effect-shape",
                f"{self.effect_id}: value2/count/turn={self.value2}/{self.count}/{self.turn}",
            )

    @property
    def raw_score_permille(self) -> int:
        return self.value1

    @property
    def retention_permille(self) -> int:
        return self.value2


@dataclass(frozen=True, slots=True)
class _ExpectedVersion:
    card_id: str
    upgrade_count: int
    name: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    asset_id: str
    rarity: str
    search_tag: str
    is_initial_deck_produce_card: bool
    max_customize_count: int
    force_stamina: int
    is_character_asset: bool
    no_deck_duplication: bool
    ordered_slots: tuple[LessonDependBlockRetentionSlot, ...]
    target_slot_index: int
    raw_effect_group_ids: tuple[str, ...] = _TARGET_GROUPS

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade_count


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionVersion:
    card_id: str
    upgrade_count: int
    name: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    ordered_effects: tuple[LessonDependBlockRetentionSlot, ...]
    target_slot_index: int
    target_effect: LessonDependBlockRetentionEffect
    asset_id: str
    rarity: str
    search_tag: str
    is_initial_deck_produce_card: bool
    max_customize_count: int
    force_stamina: int
    is_character_asset: bool
    no_deck_duplication: bool

    def __post_init__(self) -> None:
        _string(self.card_id, "card_id", non_empty=True)
        _i32(self.upgrade_count, "upgrade_count")
        _string(self.name, "name", non_empty=True)
        _i32(self.stamina, "stamina")
        _string(self.cost_type, "cost_type", non_empty=True)
        _i32(self.cost_value, "cost_value")
        _string(self.play_trigger_id, "play_trigger_id")
        _string(self.asset_id, "asset_id", non_empty=True)
        _string(self.rarity, "rarity", non_empty=True)
        _string(self.search_tag, "search_tag")
        _bool(self.is_initial_deck_produce_card, "is_initial_deck_produce_card")
        _i32(self.max_customize_count, "max_customize_count")
        _i32(self.force_stamina, "force_stamina")
        _bool(self.is_character_asset, "is_character_asset")
        _bool(self.no_deck_duplication, "no_deck_duplication")
        if type(self.ordered_effects) is not tuple or not self.ordered_effects:
            raise _contract("version-order", f"{self.card_id}@{self.upgrade_count}")
        if type(self.target_slot_index) is not int or not (
            0 <= self.target_slot_index < len(self.ordered_effects)
        ):
            raise _contract("target-slot-index", f"{self.card_id}@{self.upgrade_count}")
        target_slot = self.ordered_effects[self.target_slot_index]
        if target_slot.effect_type != TARGET_EFFECT_TYPE:
            raise _contract("target-effect-type", f"{self.card_id}@{self.upgrade_count}")
        if (
            target_slot.effect_id,
            target_slot.value1,
            target_slot.value2,
            target_slot.count,
            target_slot.turn,
        ) != (
            self.target_effect.effect_id,
            self.target_effect.value1,
            self.target_effect.value2,
            self.target_effect.count,
            self.target_effect.turn,
        ):
            raise _contract("target-effect-shape", f"{self.card_id}@{self.upgrade_count}")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade_count

    @property
    def ref_text(self) -> str:
        return f"{self.card_id}@{self.upgrade_count}"

    @property
    def companion_effects(self) -> tuple[LessonDependBlockRetentionSlot, ...]:
        return tuple(
            slot for index, slot in enumerate(self.ordered_effects)
            if index != self.target_slot_index
        )

    @property
    def target_effect_executable(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionBlocker:
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
        return self.card_id, self.upgrade_count

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
class LessonDependBlockRetentionAccounting:
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
    def target_effect_executable_version_count(self) -> int:
        return self.family_executable_version_count

    @property
    def blocker_code_map(self) -> dict[str, int]:
        return dict(self.blocker_code_counts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "universe_version_count": self.universe_version_count,
            "compiled_version_count": self.compiled_version_count,
            "failed_version_count": self.failed_version_count,
            "family_executable_version_count": self.family_executable_version_count,
            "target_effect_executable_version_count": self.target_effect_executable_version_count,
            "full_card_executable_version_count": self.full_card_executable_version_count,
            "companion_gap_version_count": self.companion_gap_version_count,
            "companion_blocker_count": self.companion_blocker_count,
            "family_blocker_count": self.family_blocker_count,
            "blocker_code_counts": self.blocker_code_map,
        }


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionCatalog:
    database: str
    versions: tuple[LessonDependBlockRetentionVersion, ...]
    blockers: tuple[LessonDependBlockRetentionBlocker, ...]
    universe_refs: tuple[tuple[str, int], ...]
    failed_refs: tuple[tuple[str, int], ...]
    accounting: LessonDependBlockRetentionAccounting

    def version(self, card_id: str, upgrade_count: int) -> LessonDependBlockRetentionVersion:
        ref = (card_id, upgrade_count)
        for version in self.versions:
            if version.ref == ref:
                return version
        raise KeyError(f"uncompiled LessonDependBlockRetention version: {card_id}@{upgrade_count}")

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(version.ref for version in self.versions)

    @property
    def target_effect_executable_versions(self) -> tuple[LessonDependBlockRetentionVersion, ...]:
        return self.versions

    def blockers_for(self, card_id: str, upgrade_count: int) -> tuple[LessonDependBlockRetentionBlocker, ...]:
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
class LessonDependBlockRetentionCompilation:
    schema_version: int
    catalog: LessonDependBlockRetentionCatalog
    blockers: tuple[LessonDependBlockRetentionBlocker, ...]

    @property
    def versions(self) -> tuple[LessonDependBlockRetentionVersion, ...]:
        return self.catalog.versions

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.compiled_refs

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.failed_refs

    @property
    def accounting(self) -> LessonDependBlockRetentionAccounting:
        return self.catalog.accounting

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(Counter(blocker.code for blocker in self.blockers))

    @property
    def fully_compiled(self) -> bool:
        return not self.failed_refs and self.accounting.family_blocker_count == 0

    def blockers_for(self, card_id: str, upgrade_count: int) -> tuple[LessonDependBlockRetentionBlocker, ...]:
        return self.catalog.blockers_for(card_id, upgrade_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog": self.catalog.to_dict(),
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionRuntime:
    block: int
    block_consumption_sum_count: int = 0
    is_buff_active: bool = True
    adding_status: AddingParameterStatus = AddingParameterStatus()
    adding_settings: AddingParameterSettings = AddingParameterSettings()
    additional: AddingParameterAdditionalData | None = None
    application_status: ParameterApplicationStatus = ParameterApplicationStatus(
        judge_parameter=0
    )

    def __post_init__(self) -> None:
        _i32(self.block, "block")
        _i32(self.block_consumption_sum_count, "block_consumption_sum_count")
        _bool(self.is_buff_active, "is_buff_active")
        if not isinstance(self.adding_status, AddingParameterStatus):
            raise _contract("runtime-shape", "adding_status has the wrong type")
        if not isinstance(self.adding_settings, AddingParameterSettings):
            raise _contract("runtime-shape", "adding_settings has the wrong type")
        if self.additional is not None and not isinstance(
            self.additional, AddingParameterAdditionalData
        ):
            raise _contract("runtime-shape", "additional has the wrong type")
        if not isinstance(self.application_status, ParameterApplicationStatus):
            raise _contract("runtime-shape", "application_status has the wrong type")

    @property
    def block_snapshot(self) -> int:
        return self.block

    @property
    def current_block(self) -> int:
        return self.block

    @property
    def block_consumption_sum(self) -> int:
        return self.block_consumption_sum_count


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionHandoff:
    version: LessonDependBlockRetentionVersion
    source_guid: str
    play_origin: PlayOrigin
    block_snapshot: int
    target_effect: LessonDependBlockRetentionEffect
    order: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.version, LessonDependBlockRetentionVersion):
            raise _contract("handoff-version", "version has the wrong type")
        _string(self.source_guid, "source_guid", non_empty=True)
        if self.play_origin not in PLAY_ORIGINS:
            raise _contract("play-origin", repr(self.play_origin))
        _i32(self.block_snapshot, "block_snapshot")
        if self.target_effect != self.version.target_effect:
            raise _contract("handoff-target", "target effect is not the compiled version target")
        if type(self.order) is not tuple or any(
            type(item) is not str or not item for item in self.order
        ):
            raise _contract("handoff-order", "execution order must be an immutable string tuple")
        if self.order != HANDOFF_ORDER:
            raise _contract("handoff-order", f"expected {HANDOFF_ORDER!r}, got {self.order!r}")

    @property
    def execution_time_block_snapshot(self) -> int:
        return self.block_snapshot


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionHit:
    index: int
    block_snapshot: int
    requested: int
    calculated: int
    application: ParameterApplication

    @property
    def score_before(self) -> int:
        return self.application.before

    @property
    def score_after(self) -> int:
        return self.application.after

    @property
    def raw_value(self) -> int:
        return self.requested

    @property
    def calculated_value(self) -> int:
        return self.calculated


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionBlockCommit:
    block_before: int
    block_after: int
    is_consumption: bool
    actual_delta_i32: int
    actual_consumption: int
    block_consumption_sum_before: int
    block_consumption_sum_after: int

    def __post_init__(self) -> None:
        _i32(self.block_before, "block_commit.block_before")
        _i32(self.block_after, "block_commit.block_after")
        _bool(self.is_consumption, "block_commit.is_consumption")
        _i32(self.actual_delta_i32, "block_commit.actual_delta_i32")
        _i32(self.actual_consumption, "block_commit.actual_consumption")
        _i32(self.block_consumption_sum_before, "block_commit.sum_before")
        _i32(self.block_consumption_sum_after, "block_commit.sum_after")
        if not self.is_consumption:
            raise _contract("block-commit-shape", "retention commit must be consumption")

    @property
    def requested_block(self) -> int:
        return self.block_after

    @property
    def actual_delta(self) -> int:
        return self.actual_delta_i32

    @property
    def consumption_delta(self) -> int:
        return self.actual_consumption

    @property
    def sum_before(self) -> int:
        return self.block_consumption_sum_before

    @property
    def sum_after(self) -> int:
        return self.block_consumption_sum_after


@dataclass(frozen=True, slots=True)
class LessonDependBlockRetentionExecution:
    handoff: LessonDependBlockRetentionHandoff
    before: LessonDependBlockRetentionRuntime
    after: LessonDependBlockRetentionRuntime
    hits: tuple[LessonDependBlockRetentionHit, ...]
    block_commit: LessonDependBlockRetentionBlockCommit | None
    retained_block: int
    actual_lesson_parameter_added: int
    trace: tuple[str, ...]
    companion_blockers: tuple[LessonDependBlockRetentionBlocker, ...]

    @property
    def score_before(self) -> int:
        return self.before.application_status.judge_parameter

    @property
    def score_after(self) -> int:
        return self.after.application_status.judge_parameter

    @property
    def block_before(self) -> int:
        return self.before.block

    @property
    def block_snapshot(self) -> int:
        return self.handoff.block_snapshot

    @property
    def block_after(self) -> int:
        return self.after.block

    @property
    def block_consumption_sum_before(self) -> int:
        return self.before.block_consumption_sum_count

    @property
    def block_consumption_sum_after(self) -> int:
        return self.after.block_consumption_sum_count

    @property
    def actual_block_delta(self) -> int:
        return _i32_wrap(self.after.block - self.before.block)

    @property
    def actual_delta_i32(self) -> int:
        return self.actual_block_delta

    @property
    def actual_block_consumption(self) -> int:
        if self.block_commit is None:
            return 0
        return self.block_commit.actual_consumption

    @property
    def actual_delta(self) -> int:
        return self.actual_block_delta

    @property
    def value1_permille(self) -> int:
        return self.handoff.target_effect.value1

    @property
    def value2_retention_permille(self) -> int:
        return self.handoff.target_effect.value2

    @property
    def value2_target_block(self) -> int:
        return self.retained_block

    @property
    def set_block(self) -> LessonDependBlockRetentionBlockCommit | None:
        return self.block_commit

    @property
    def event_trace(self) -> tuple[str, ...]:
        return self.trace

    @property
    def play_origin(self) -> PlayOrigin:
        return self.handoff.play_origin

    @property
    def executable(self) -> bool:
        return True


def _slot(
    effect_id: str,
    value1: int,
    value2: int,
    *,
    count: int = 1,
    turn: int = 0,
) -> LessonDependBlockRetentionSlot:
    return LessonDependBlockRetentionSlot(
        effect_id,
        TARGET_EFFECT_TYPE,
        value1,
        value2,
        count,
        turn,
    )


def _target_id(value1: int, value2: int) -> str:
    return f"e_effect-exam_lesson_depend_block-{value1:04d}-{value2:04d}-01"


def _expected_version(
    card_id: str,
    upgrade_count: int,
    name: str,
    value1: int,
    value2: int,
    *,
    play_trigger_id: str,
    asset_id: str,
    rarity: str,
    max_customize_count: int,
    force_stamina: int,
    no_deck_duplication: bool,
) -> _ExpectedVersion:
    return _ExpectedVersion(
        card_id=card_id,
        upgrade_count=upgrade_count,
        name=name,
        stamina=0,
        cost_type=_UNKNOWN_COST,
        cost_value=0,
        play_trigger_id=play_trigger_id,
        asset_id=asset_id,
        rarity=rarity,
        search_tag="",
        is_initial_deck_produce_card=False,
        max_customize_count=max_customize_count,
        force_stamina=force_stamina,
        is_character_asset=True,
        no_deck_duplication=no_deck_duplication,
        ordered_slots=(_slot(_target_id(value1, value2), value1, value2),),
        target_slot_index=0,
    )


def _versions() -> tuple[_ExpectedVersion, ...]:
    act2 = "p_card-02-act-2_045"
    act3 = "p_card-02-act-3_039"
    act2_name = "\u30cf\u30fc\u30c8\u306e\u5408\u56f3"
    act3_name = "\u5c4a\u3044\u3066\uff01"
    values: list[_ExpectedVersion] = []
    for upgrade_count, value1, max_customize_count in (
        (0, 1300, 0),
        (1, 1800, 2),
        (2, 2000, 2),
        (3, 2300, 2),
    ):
        values.append(
            _expected_version(
                act2,
                upgrade_count,
                act2_name + "+" * upgrade_count,
                value1,
                500,
                play_trigger_id="",
                asset_id="img_general_skillcard_act-2_045",
                rarity="ProduceCardRarity_Sr",
                max_customize_count=max_customize_count,
                force_stamina=3,
                no_deck_duplication=False,
            )
        )
    for upgrade_count, value1, max_customize_count in (
        (0, 3200, 0),
        (1, 4000, 1),
        (2, 4200, 1),
        (3, 4600, 1),
    ):
        values.append(
            _expected_version(
                act3,
                upgrade_count,
                act3_name + "+" * upgrade_count,
                value1,
                1000,
                play_trigger_id="e_trigger-none-block_up-7",
                asset_id="img_general_skillcard_act-3_039",
                rarity="ProduceCardRarity_Ssr",
                max_customize_count=max_customize_count,
                force_stamina=1,
                no_deck_duplication=True,
            )
        )
    return tuple(values)


_EXPECTED_VERSIONS: Final = _versions()
_EXPECTED_REFS: Final = tuple(expected.ref for expected in _EXPECTED_VERSIONS)


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise _contract("master-schema", f"missing column {key!r}") from exc


def _fetch_one(
    connection: sqlite3.Connection,
    query: str,
    params: Sequence[Any],
    label: str,
) -> sqlite3.Row:
    try:
        row = connection.execute(query, params).fetchone()
    except sqlite3.Error as exc:
        raise _contract("master-schema", f"{label}: {exc}") from exc
    if row is None:
        raise _contract("master-missing", label)
    return row


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


def _validate_target_descriptions(
    descriptions: list[Any], slot: LessonDependBlockRetentionSlot
) -> None:
    if len(descriptions) != 10 or any(
        not isinstance(item, dict) or frozenset(item) != _DESCRIPTION_KEYS
        for item in descriptions
    ):
        raise _contract("effect-description-shape", f"{slot.effect_id}: expected ten rows")
    final_text = "\u3092\u534a\u5206\u306b\u3059\u308b" if slot.value2 == 500 else "\u30920\u306b\u3059\u308b"
    expected = (
        (
            "ProduceDescriptionType_ProduceExamEffectType",
            "ExamDescriptionType_Unknown",
            "ProduceExamEffectType_ExamBlock",
            0,
            0,
            0,
            0,
            "Label_ExamBlock",
            "\u5143\u6c17",
        ),
        (
            "ProduceDescriptionType_PlainText",
            "ExamDescriptionType_Unknown",
            _UNKNOWN,
            0,
            0,
            0,
            0,
            "",
            "\u306e<nobr>",
        ),
        (
            "ProduceDescriptionType_Exam",
            "ExamDescriptionType_CustomizeEffectValuePercent1",
            _UNKNOWN,
            slot.value1,
            0,
            0,
            0,
            "",
            str(slot.value1 // 10),
        ),
        (
            "ProduceDescriptionType_PlainText",
            "ExamDescriptionType_Unknown",
            _UNKNOWN,
            0,
            0,
            0,
            0,
            "",
            "%</nobr>\u5206",
        ),
        (
            "ProduceDescriptionType_ProduceExamEffectType",
            "ExamDescriptionType_Unknown",
            "ProduceExamEffectType_ExamLesson",
            0,
            0,
            0,
            0,
            "Label_ExamLesson",
            "\u30d1\u30e9\u30e1\u30fc\u30bf",
        ),
        (
            "ProduceDescriptionType_PlainText",
            "ExamDescriptionType_Unknown",
            _UNKNOWN,
            0,
            0,
            0,
            0,
            "",
            "\u4e0a\u6607",
        ),
        (
            "ProduceDescriptionType_Exam",
            "ExamDescriptionType_CustomizeLessonCountAdd",
            _UNKNOWN,
            0,
            0,
            0,
            0,
            "Description_LessonCountAdd_CountSection",
            "",
        ),
        (
            "ProduceDescriptionType_PlainText",
            "ExamDescriptionType_Unknown",
            _UNKNOWN,
            0,
            0,
            0,
            0,
            "",
            "\u3055\u305b\u3001",
        ),
        (
            "ProduceDescriptionType_ProduceExamEffectType",
            "ExamDescriptionType_Unknown",
            "ProduceExamEffectType_ExamBlock",
            0,
            0,
            0,
            0,
            "Label_ExamBlock",
            "\u5143\u6c17",
        ),
        (
            "ProduceDescriptionType_PlainText",
            "ExamDescriptionType_Unknown",
            _UNKNOWN,
            0,
            0,
            0,
            0,
            "",
            final_text,
        ),
    )
    actual = tuple(_description_signature(item) for item in descriptions)
    if actual != expected:
        raise _contract("effect-description-shape", f"{slot.effect_id}: signature differs")


def _validate_effect_raw(
    raw: Mapping[str, Any], slot: LessonDependBlockRetentionSlot
) -> tuple[str, ...]:
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
            raise _contract(
                "effect-raw-core",
                f"{slot.effect_id}: {key}={raw.get(key)!r}, expected {expected!r}",
            )
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or tuple(groups) != _TARGET_GROUPS:
        raise _contract("effect-raw-groups", f"{slot.effect_id}: {groups!r}")
    neutral = {
        "movePositionType": _MOVE_UNKNOWN,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": _UNKNOWN,
        "produceCardSearchId": "",
        "produceCardSearchId2": "",
        "pickRangeType": _UNKNOWN_PICK_RANGE,
        "pickRangeType2": _UNKNOWN_PICK_RANGE,
        "pickCountType": _UNKNOWN_PICK_COUNT,
        "pickCountType2": _UNKNOWN_PICK_COUNT,
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
            raise _contract("effect-raw-neutral", f"{slot.effect_id}: {key}")
    descriptions = raw.get("produceDescriptions")
    custom = raw.get("customizeProduceDescriptions")
    if not isinstance(descriptions, list) or not isinstance(custom, list):
        raise _contract("effect-description-shape", f"{slot.effect_id}: descriptions are not arrays")
    if len(custom) != len(descriptions) + 1 or custom[1:] != descriptions:
        raise _contract("effect-description-shape", f"{slot.effect_id}: custom rows differ")
    if any(
        not isinstance(item, dict) or frozenset(item) != _DESCRIPTION_KEYS
        for item in custom
    ):
        raise _contract("effect-description-shape", f"{slot.effect_id}: description keys differ")
    first_custom = custom[0]
    if (
        first_custom.get("produceDescriptionType")
        != "ProduceDescriptionType_ProduceDescriptionName"
        or first_custom.get("targetId") != "Label_StyleDot"
        or first_custom.get("text") != ""
    ):
        raise _contract("effect-description-shape", f"{slot.effect_id}: style prefix differs")
    _validate_target_descriptions(descriptions, slot)
    return tuple(groups)


def _validate_effect(
    connection: sqlite3.Connection, slot: LessonDependBlockRetentionSlot
) -> tuple[str, ...]:
    row = _fetch_one(
        connection,
        """
        SELECT id, effect_type, value1 AS effect_value1, value2 AS effect_value2,
               effect_count, effect_turn, status_enchant_id, chain_effect_id, raw_json
        FROM effect WHERE id = ?
        """,
        (slot.effect_id,),
        f"effect {slot.effect_id}",
    )
    actual = {
        "id": _row_value(row, "id"),
        "effect_type": _row_value(row, "effect_type"),
        "effect_value1": _row_value(row, "effect_value1"),
        "effect_value2": _row_value(row, "effect_value2"),
        "effect_count": _row_value(row, "effect_count"),
        "effect_turn": _row_value(row, "effect_turn"),
        "status_enchant_id": _row_value(row, "status_enchant_id"),
        "chain_effect_id": _row_value(row, "chain_effect_id"),
    }
    expected = {
        "id": slot.effect_id,
        "effect_type": slot.effect_type,
        "effect_value1": slot.value1,
        "effect_value2": slot.value2,
        "effect_count": slot.count,
        "effect_turn": slot.turn,
        "status_enchant_id": "",
        "chain_effect_id": "",
    }
    if actual != expected:
        raise _contract("effect-normalized-shape", f"{slot.effect_id}: {actual!r}")
    return _validate_effect_raw(
        _json_object(_row_value(row, "raw_json"), f"effect {slot.effect_id}.raw_json"),
        slot,
    )


def _validate_card(
    connection: sqlite3.Connection, expected: _ExpectedVersion
) -> LessonDependBlockRetentionVersion:
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
        raise _contract("card-normalized-shape", f"{expected.ref!r}: {normalized!r}")

    raw = _json_object(_row_value(row, "raw_json"), f"card {expected.ref!r}.raw_json")
    if frozenset(raw) != _RAW_CARD_KEYS:
        raise _contract("card-raw-shape", f"{expected.ref!r}: key set differs")
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
        "forceStamina": raw.get("forceStamina"),
        "isCharacterAsset": raw.get("isCharacterAsset"),
        "noDeckDuplication": raw.get("noDeckDuplication"),
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
        "forceStamina": expected.force_stamina,
        "isCharacterAsset": expected.is_character_asset,
        "noDeckDuplication": expected.no_deck_duplication,
        "moveEffectTriggerType": _MOVE_EFFECT_TRIGGER_UNKNOWN,
        "effectGroupIds": list(expected.raw_effect_group_ids),
    }
    if raw_core != expected_core:
        raise _contract("card-raw-core", f"{expected.ref!r}: {raw_core!r}")

    play_effects = _json_array(
        _row_value(row, "play_effects_json"),
        f"card {expected.ref!r}.play_effects_json",
    )
    if raw.get("playEffects") != play_effects:
        raise _contract("card-play-effects-shape", f"{expected.ref!r}: raw/normalized mismatch")
    expected_play_effects = [
        {
            "produceExamTriggerId": slot.trigger_id,
            "produceExamEffectId": slot.effect_id,
            "hideIcon": slot.hide_icon,
            "isOncePlayEffect": slot.is_once_play_effect,
        }
        for slot in expected.ordered_slots
    ]
    if play_effects != expected_play_effects:
        raise _contract("card-play-effects-shape", f"{expected.ref!r}: order/rows differ")

    target_slots = [
        index
        for index, slot in enumerate(expected.ordered_slots)
        if slot.effect_type == TARGET_EFFECT_TYPE
    ]
    if target_slots != [expected.target_slot_index]:
        raise _contract("target-cardinality", f"{expected.ref!r}: {target_slots!r}")
    for slot in expected.ordered_slots:
        _validate_effect(connection, slot)
    target_slot = expected.ordered_slots[expected.target_slot_index]
    target_groups = _validate_effect(connection, target_slot)
    target_effect = LessonDependBlockRetentionEffect(
        target_slot.effect_id,
        target_slot.effect_type,
        target_slot.value1,
        target_slot.value2,
        target_slot.count,
        target_slot.turn,
        target_groups,
    )
    return LessonDependBlockRetentionVersion(
        expected.card_id,
        expected.upgrade_count,
        expected.name,
        expected.stamina,
        expected.cost_type,
        expected.cost_value,
        expected.play_trigger_id,
        expected.ordered_slots,
        expected.target_slot_index,
        target_effect,
        expected.asset_id,
        expected.rarity,
        expected.search_tag,
        expected.is_initial_deck_produce_card,
        expected.max_customize_count,
        expected.force_stamina,
        expected.is_character_asset,
        expected.no_deck_duplication,
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
        play_effects = _json_array(
            _row_value(row, "play_effects_json"),
            f"card {card_id}@{upgrade_count}.play_effects_json",
        )
        effect_ids: list[str] = []
        for item in play_effects:
            if not isinstance(item, dict) or not isinstance(
                item.get("produceExamEffectId"), str
            ):
                raise _contract("card-play-effects-shape", f"{card_id}@{upgrade_count}")
            effect_ids.append(item["produceExamEffectId"])
        if not effect_ids:
            continue
        placeholders = ",".join("?" for _ in effect_ids)
        try:
            effect_rows = connection.execute(
                f"SELECT id, effect_type, value2 FROM effect WHERE id IN ({placeholders})",
                effect_ids,
            ).fetchall()
        except sqlite3.Error as exc:
            raise _contract("master-schema", f"effect universe query: {exc}") from exc
        effect_types = {
            row["id"]: (row["effect_type"], row["value2"])
            for row in effect_rows
        }
        if any(effect_id not in effect_types for effect_id in effect_ids):
            raise _contract("master-missing", f"{card_id}@{upgrade_count}: play effect")
        if any(
            effect_types[effect_id][0] == TARGET_EFFECT_TYPE
            and effect_types[effect_id][1] in (500, 1000)
            for effect_id in effect_ids
        ):
            discovered.append((card_id, upgrade_count))
    return tuple(sorted(discovered))


def _companion_blockers(
    version: LessonDependBlockRetentionVersion,
) -> tuple[LessonDependBlockRetentionBlocker, ...]:
    blockers: list[LessonDependBlockRetentionBlocker] = []
    for index, slot in enumerate(version.companion_effects, start=1):
        blockers.append(
            LessonDependBlockRetentionBlocker(
                "companion-effect-unbound",
                version.card_id,
                version.upgrade_count,
                f"slot={index};effect_id={slot.effect_id};effect_type={slot.effect_type}",
            )
        )
    if version.play_trigger_id:
        blockers.append(
            LessonDependBlockRetentionBlocker(
                "external-card-play-trigger",
                version.card_id,
                version.upgrade_count,
                version.play_trigger_id,
            )
        )
    if version.cost_type != _UNKNOWN_COST or version.cost_value != 0:
        blockers.append(
            LessonDependBlockRetentionBlocker(
                "external-card-cost-policy",
                version.card_id,
                version.upgrade_count,
                f"{version.cost_type}:{version.cost_value}",
            )
        )
    return tuple(blockers)


def _make_accounting(
    universe_refs: tuple[tuple[str, int], ...],
    versions: tuple[LessonDependBlockRetentionVersion, ...],
    blockers: tuple[LessonDependBlockRetentionBlocker, ...],
    failed_refs: tuple[tuple[str, int], ...],
) -> LessonDependBlockRetentionAccounting:
    companion_blockers = tuple(
        blocker
        for blocker in blockers
        if blocker.code.startswith("companion-") or blocker.code.startswith("external-")
    )
    family_blockers = tuple(
        blocker for blocker in blockers if blocker not in companion_blockers
    )
    version_refs = {version.ref for version in versions}
    companion_gap_refs = {blocker.ref for blocker in companion_blockers}
    code_counts = Counter(blocker.code for blocker in blockers)
    return LessonDependBlockRetentionAccounting(
        universe_version_count=len(universe_refs),
        compiled_version_count=len(versions),
        failed_version_count=len(failed_refs),
        family_executable_version_count=len(versions),
        full_card_executable_version_count=len(
            version_refs - companion_gap_refs
        ),
        companion_gap_version_count=len(version_refs & companion_gap_refs),
        companion_blocker_count=len(companion_blockers),
        family_blocker_count=len(family_blockers),
        blocker_code_counts=tuple(sorted(code_counts.items())),
    )


def compile_plan2_native_lesson_depend_block_retention_catalog(
    database: str | Path = DEFAULT_DATABASE,
) -> LessonDependBlockRetentionCompilation:
    """Compile only the exact eight Master versions in this leaf."""

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
        compiled: list[LessonDependBlockRetentionVersion] = []
        blockers: list[LessonDependBlockRetentionBlocker] = []
        failed: list[tuple[str, int]] = []
        for expected in _EXPECTED_VERSIONS:
            try:
                version = _validate_card(connection, expected)
            except Plan2NativeCatalogLessonDependBlockRetentionError as exc:
                failed.append(expected.ref)
                blockers.append(
                    LessonDependBlockRetentionBlocker(
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
        accounting = _make_accounting(
            tuple(_EXPECTED_REFS), version_tuple, blocker_tuple, tuple(failed)
        )
        catalog = LessonDependBlockRetentionCatalog(
            database_path,
            version_tuple,
            blocker_tuple,
            tuple(_EXPECTED_REFS),
            tuple(failed),
            accounting,
        )
        return LessonDependBlockRetentionCompilation(
            SCHEMA_VERSION, catalog, blocker_tuple
        )
    finally:
        connection.close()


compile_plan2_native_catalog_lesson_depend_block_retention = (
    compile_plan2_native_lesson_depend_block_retention_catalog
)
compile_plan2_native_catalog_lesson_depend_block_retention_catalog = (
    compile_plan2_native_lesson_depend_block_retention_catalog
)
load_plan2_native_lesson_depend_block_retention_catalog = (
    compile_plan2_native_lesson_depend_block_retention_catalog
)
load_plan2_native_catalog_lesson_depend_block_retention = (
    compile_plan2_native_lesson_depend_block_retention_catalog
)


def _resolve_version(
    catalog_or_version: LessonDependBlockRetentionCompilation
    | LessonDependBlockRetentionCatalog
    | LessonDependBlockRetentionVersion,
    card_id: str | None,
    upgrade_count: int | None,
) -> LessonDependBlockRetentionVersion:
    if isinstance(catalog_or_version, LessonDependBlockRetentionVersion):
        return catalog_or_version
    if card_id is None or upgrade_count is None:
        raise _contract("version-ref", "card_id and upgrade_count are required")
    if isinstance(catalog_or_version, LessonDependBlockRetentionCompilation):
        catalog = catalog_or_version.catalog
    elif isinstance(catalog_or_version, LessonDependBlockRetentionCatalog):
        catalog = catalog_or_version
    else:
        raise _contract("version-ref", "catalog_or_version has the wrong type")
    try:
        return catalog.version(card_id, upgrade_count)
    except KeyError as exc:
        raise _contract("version-ref", str(exc)) from exc


def build_plan2_native_lesson_depend_block_retention_handoff(
    catalog_or_version: LessonDependBlockRetentionCompilation
    | LessonDependBlockRetentionCatalog
    | LessonDependBlockRetentionVersion,
    source_guid: str,
    play_origin: PlayOrigin,
    block_snapshot: int | None = None,
    *,
    card_id: str | None = None,
    upgrade_count: int | None = None,
    block: int | None = None,
) -> LessonDependBlockRetentionHandoff:
    """Capture the execution-time Block exactly once."""

    if block_snapshot is None:
        block_snapshot = block
    elif block is not None:
        raise _contract("snapshot-shape", "block and block_snapshot were both supplied")
    if block_snapshot is None:
        raise _contract("snapshot-shape", "block_snapshot is required")
    version = _resolve_version(catalog_or_version, card_id, upgrade_count)
    snapshot = _i32(block_snapshot, "block_snapshot")
    return LessonDependBlockRetentionHandoff(
        version=version,
        source_guid=source_guid,
        play_origin=play_origin,
        block_snapshot=snapshot,
        target_effect=version.target_effect,
        order=HANDOFF_ORDER,
    )


build_plan2_native_catalog_lesson_depend_block_retention_handoff = (
    build_plan2_native_lesson_depend_block_retention_handoff
)


def _native_ceil_ratio(value: int, permille: int) -> int:
    """Native FloatFromPermil -> FMUL -> epsilon -> FRINTP -> FCVTPS."""

    value = _i32(value, "block_snapshot")
    permille = _i32(permille, "value1 permille")
    try:
        ratio = permille_to_f32(permille)
        product = f32(ratio * f32(value))
        return ceil_f32_to_i32(f32(product + F32_NEGATIVE_EPSILON))
    except (NativeFormulaDomainError, OverflowError, ValueError, TypeError) as exc:
        raise _contract("native-arithmetic-domain", str(exc)) from exc


def _native_retained_block(value: int, retention_permille: int) -> int:
    """Native binary32 retention: ceil((1 - value2/1000) * Block)."""

    value = _i32(value, "block_snapshot")
    retention_permille = _i32(retention_permille, "value2 retention permille")
    try:
        retention = f32(f32(1.0) - permille_to_f32(retention_permille))
        product = f32(retention * f32(value))
        return ceil_f32_to_i32(product)
    except (NativeFormulaDomainError, OverflowError, ValueError, TypeError) as exc:
        raise _contract("native-arithmetic-domain", str(exc)) from exc


def execute_plan2_native_lesson_depend_block_retention(
    handoff: LessonDependBlockRetentionHandoff,
    runtime: LessonDependBlockRetentionRuntime | None = None,
) -> LessonDependBlockRetentionExecution:
    """Execute the target in native order over immutable input values."""

    if not isinstance(handoff, LessonDependBlockRetentionHandoff):
        raise _contract("handoff-shape", "handoff has the wrong type")
    if runtime is None:
        runtime = LessonDependBlockRetentionRuntime(handoff.block_snapshot)
    if not isinstance(runtime, LessonDependBlockRetentionRuntime):
        raise _contract("runtime-shape", "runtime has the wrong type")
    if runtime.block != handoff.block_snapshot:
        raise _contract(
            "snapshot-mismatch",
            f"runtime={runtime.block}, handoff={handoff.block_snapshot}",
        )

    effect = handoff.target_effect
    trace = list(handoff.order)
    trace.extend(
        (
            "target:read-execution-time-block-once",
            "target:FloatFromPermil(value1)-binary32",
            "target:FMUL(block-snapshot)-binary32",
            "target:F32_NEGATIVE_EPSILON+FRINTP+FCVTPS",
        )
    )
    requested = _native_ceil_ratio(handoff.block_snapshot, effect.value1)
    current_application_status = runtime.application_status
    hits: list[LessonDependBlockRetentionHit] = []
    trace.append(
        "target:CalculateAddingParameter(isBuffActive/lesson-modifier/cap)"
    )
    for index in range(effect.count):
        try:
            calculated = calculate_adding_parameter(
                requested,
                is_buff_active=runtime.is_buff_active,
                status=runtime.adding_status,
                settings=runtime.adding_settings,
                additional=runtime.additional,
            )
            application = apply_parameter_add(
                calculated, status=current_application_status
            )
        except (NativeFormulaDomainError, OverflowError, ValueError, TypeError) as exc:
            raise _contract("native-arithmetic-domain", str(exc)) from exc
        hits.append(
            LessonDependBlockRetentionHit(
                index=index,
                block_snapshot=handoff.block_snapshot,
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
        trace.extend(
            (
                "target:AddParameter-per-hit",
                "target:commit-score-after-each-AddParameter",
            )
        )

    trace.append("target:value2-retention-after-score-hits")
    retained_block = _native_retained_block(
        handoff.block_snapshot, effect.value2
    )
    sum_before = runtime.block_consumption_sum_count
    block_commit: LessonDependBlockRetentionBlockCommit | None = None
    sum_after = sum_before
    if retained_block != handoff.block_snapshot:
        actual_consumption = max(
            _i32_wrap(handoff.block_snapshot - retained_block), 0
        )
        sum_after = _i32_wrap(sum_before + actual_consumption)
        block_commit = LessonDependBlockRetentionBlockCommit(
            block_before=handoff.block_snapshot,
            block_after=retained_block,
            is_consumption=True,
            actual_delta_i32=_i32_wrap(retained_block - handoff.block_snapshot),
            actual_consumption=actual_consumption,
            block_consumption_sum_before=sum_before,
            block_consumption_sum_after=sum_after,
        )
        trace.extend(
            (
                "target:retention=ceil_f32((1-value2/1000)*block-snapshot)",
                "target:SetBlock(isConsumption=true)",
                "target:actual-consumption=max(i32(oldBlock-newBlock),0)",
                "target:BlockConsumptionSumCount=i32(sum+actual-consumption)",
            )
        )
    else:
        trace.extend(
            (
                "target:retention=ceil_f32((1-value2/1000)*block-snapshot)",
                "target:value2-retention:no-block-change",
            )
        )

    after = replace(
        runtime,
        block=retained_block,
        block_consumption_sum_count=sum_after,
        application_status=current_application_status,
    )
    trace.append("handoff:companions-and-card-settlement-external")
    return LessonDependBlockRetentionExecution(
        handoff=handoff,
        before=runtime,
        after=after,
        hits=tuple(hits),
        block_commit=block_commit,
        retained_block=retained_block,
        actual_lesson_parameter_added=_i32_wrap(
            sum(hit.application.actual_parameter for hit in hits)
        ),
        trace=tuple(trace),
        companion_blockers=_companion_blockers(handoff.version),
    )


execute_plan2_native_catalog_lesson_depend_block_retention = (
    execute_plan2_native_lesson_depend_block_retention
)


def _ref_to_dict(ref: tuple[str, int]) -> dict[str, Any]:
    return {"card_id": ref[0], "upgrade_count": ref[1]}


def _slot_to_dict(slot: LessonDependBlockRetentionSlot) -> dict[str, Any]:
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


def _version_to_dict(version: LessonDependBlockRetentionVersion) -> dict[str, Any]:
    return {
        "card_id": version.card_id,
        "upgrade_count": version.upgrade_count,
        "ref": version.ref_text,
        "name": version.name,
        "stamina": version.stamina,
        "cost_type": version.cost_type,
        "cost_value": version.cost_value,
        "play_trigger_id": version.play_trigger_id,
        "target_slot_index": version.target_slot_index,
        "ordered_effects": [_slot_to_dict(slot) for slot in version.ordered_effects],
        "companions": [_slot_to_dict(slot) for slot in version.companion_effects],
        "target_effect_executable": version.target_effect_executable,
        "target_effect": {
            "effect_id": version.target_effect.effect_id,
            "effect_type": version.target_effect.effect_type,
            "value1_permille": version.target_effect.value1,
            "value2_retention_permille": version.target_effect.value2,
            "count": version.target_effect.count,
            "turn": version.target_effect.turn,
            "effect_group_ids": list(version.target_effect.effect_group_ids),
        },
        "asset_id": version.asset_id,
        "rarity": version.rarity,
        "search_tag": version.search_tag,
        "is_initial_deck_produce_card": version.is_initial_deck_produce_card,
        "max_customize_count": version.max_customize_count,
        "force_stamina": version.force_stamina,
        "is_character_asset": version.is_character_asset,
        "no_deck_duplication": version.no_deck_duplication,
    }


ANDROID_NATIVE_EVIDENCE: Final = {
    "binary": "Android libil2cpp v3.2.3",
    "executor": {
        "type": "Campus.InGame.Exam.LessonDependBlockEffectExecutor",
        "type_def_index": 3716,
        "ctor_va": "0x7E82290",
        "execute_va": "0x7E82408",
        "fields": {"_value": "0x10", "_value2": "0x14", "_count": "0x18"},
    },
    "formula": {
        "float_from_permille_va": "0x701A940",
        "calculate_adding_parameter_callsite": "0x7F0819C",
        "add_parameter_callsite": "0x808BFCC",
        "score_order": [
            "read execution-time Block once",
            "FloatFromPermil(value1)",
            "binary32 multiply by Block snapshot",
            "add F32_NEGATIVE_EPSILON",
            "FRINTP/FCVTPS ceil to signed i32",
            "CalculateAddingParameter",
            "AddParameter per effect count",
        ],
    },
    "block_commit": {
        "get_block_va": "0x7EB7DE4",
        "get_block_consumption_sum_count_va": "0x7EB8AD8",
        "set_block_va": "0x7EBB524",
        "add_block_consumption_sum_count_va": "0x7EBB5C8",
        "formula": "actual=max(i32(oldBlock-newBlock),0); sum=i32(sum+actual)",
    },
}

PC_NATIVE_METADATA: Final = {
    "LessonDependBlockEffectExecutor": {
        "type_index": 3599,
        "ctor": {
            "method_index": 17966,
            "token": 100681263,
            "token_hex": "0x0600462F",
        },
        "ExecuteEffect": {
            "method_index": 17967,
            "token": 100681264,
            "token_hex": "0x06004630",
        },
        "fields": {"_value": "_value", "_value2": "_value2", "_count": "_count"},
    }
}


def build_plan2_native_lesson_depend_block_retention_audit(
    compilation: LessonDependBlockRetentionCompilation | None = None,
) -> dict[str, Any]:
    """Return the focused JSON audit for this standalone adapter."""

    compilation = compilation or compile_plan2_native_lesson_depend_block_retention_catalog()
    version_accounting = []
    for ref in compilation.catalog.universe_refs:
        blockers = compilation.blockers_for(*ref)
        version = next(
            (item for item in compilation.versions if item.ref == ref), None
        )
        version_accounting.append(
            {
                "ref": f"{ref[0]}@{ref[1]}",
                "compiled": version is not None,
                "target_effect_executable": version is not None,
                "target_effect_id": (
                    version.target_effect.effect_id if version is not None else None
                ),
                "companions": (
                    [_slot_to_dict(slot) for slot in version.companion_effects]
                    if version is not None
                    else []
                ),
                "external_companions": [
                    {
                        "code": blocker.code,
                        "detail": blocker.detail,
                    }
                    for blocker in blockers
                    if blocker.code.startswith("external-")
                ],
                "family_blocker_count": sum(
                    blocker.code.startswith("family-") for blocker in blockers
                ),
                "companion_blocker_count": sum(
                    not blocker.code.startswith("family-") for blocker in blockers
                ),
                "blocker_codes": sorted(blocker.code for blocker in blockers),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter": "plan2_native_catalog_lesson_depend_block_retention",
        "target_effect_type": TARGET_EFFECT_TYPE,
        "target_version_count": TARGET_VERSION_COUNT,
        "exact_versions": [
            _version_to_dict(version) for version in compilation.versions
        ],
        "catalog": compilation.catalog.to_dict(),
        "compiled_version_rows": [
            _version_to_dict(version) for version in compilation.versions
        ],
        "version_accounting": version_accounting,
        "accounting": compilation.accounting.to_dict(),
        "nativeEvidence": {
            "android": ANDROID_NATIVE_EVIDENCE,
            "pc": PC_NATIVE_METADATA,
        },
        "runtimeContract": {
            "immutable_compile_execute_handoff": True,
            "execution_time_block_snapshot": "handoff captures Block once; execution rejects a different runtime Block",
            "value1": "permille -> binary32 ratio * Block snapshot -> F32_NEGATIVE_EPSILON -> ceil signed i32",
            "lesson_modifier_and_cap": "CalculateAddingParameter then AddParameter/limit cap per hit",
            "value2": "retention is evaluated after score commits: ceil_f32((1 - value2/1000) * original Block snapshot)",
            "block_commit": "SetBlock(newBlock, isConsumption=true); actual=max(i32(old-new),0); BlockConsumptionSumCount=i32(sum+actual)",
            "orders": {
                "normal": list(HANDOFF_ORDER),
                "forced": list(HANDOFF_ORDER),
                "extra": list(HANDOFF_ORDER),
            },
        },
        "boundary_policy": {
            "zero": "zero Block and zero score execute through the native float32 path; no shortcut",
            "signed_i32": "Block, value1/value2, score, and accumulator inputs are signed i32",
            "overflow": "non-representable binary32 ceil or formula domain raises native-arithmetic-domain",
            "unknown": "unknown card/effect/raw key/group/description/order shape fails closed",
        },
        "companion_gap_statement": "all eight target effects compile; four act-3 cards retain the external e_trigger-none-block_up-7 companion and are not claimed full-card executable",
        "forbidden_surfaces": [
            "src/gkms_tool/plan2_native_program_catalog.py",
            "src/gkms_tool/plan2_native_horizon.py",
            "src/gkms_tool/plan2_core_runtime.py",
            "formal coverage",
            "Plan3",
            "GUI/controller",
        ],
        "runtime_calls_agent": False,
        "verification_scope": {
            "focused_only": True,
            "full": False,
            "security": False,
            "hashes": False,
            "time": False,
        },
    }


build_plan2_native_catalog_lesson_depend_block_retention_audit = (
    build_plan2_native_lesson_depend_block_retention_audit
)


__all__ = [
    "ACTIVE_SKILL",
    "ANDROID_NATIVE_EVIDENCE",
    "HANDOFF_ORDER",
    "LessonDependBlockRetentionAccounting",
    "LessonDependBlockRetentionBlocker",
    "LessonDependBlockRetentionCatalog",
    "LessonDependBlockRetentionCompilation",
    "LessonDependBlockRetentionContractError",
    "LessonDependBlockRetentionEffect",
    "LessonDependBlockRetentionExecution",
    "LessonDependBlockRetentionHandoff",
    "LessonDependBlockRetentionHit",
    "LessonDependBlockRetentionBlockCommit",
    "LessonDependBlockRetentionRuntime",
    "LessonDependBlockRetentionSlot",
    "LessonDependBlockRetentionVersion",
    "MOVE_LOST",
    "PC_NATIVE_METADATA",
    "PLAN2",
    "PLAY_ORIGINS",
    "Plan2NativeCatalogLessonDependBlockRetentionError",
    "Plan2NativeLessonDependBlockRetentionContractError",
    "TARGET_EFFECT_TYPE",
    "TARGET_VERSION_COUNT",
    "build_plan2_native_catalog_lesson_depend_block_retention_audit",
    "build_plan2_native_catalog_lesson_depend_block_retention_handoff",
    "build_plan2_native_lesson_depend_block_retention_audit",
    "build_plan2_native_lesson_depend_block_retention_handoff",
    "compile_plan2_native_catalog_lesson_depend_block_retention",
    "compile_plan2_native_catalog_lesson_depend_block_retention_catalog",
    "compile_plan2_native_lesson_depend_block_retention_catalog",
    "execute_plan2_native_catalog_lesson_depend_block_retention",
    "execute_plan2_native_lesson_depend_block_retention",
    "load_plan2_native_catalog_lesson_depend_block_retention",
    "load_plan2_native_lesson_depend_block_retention_catalog",
]
