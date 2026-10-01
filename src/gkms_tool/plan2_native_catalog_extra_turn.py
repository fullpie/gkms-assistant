"""Bounded, exact Plan2 catalog adapter for ``ExamExtraTurn``.

This module is intentionally an integration leaf.  It reads the four current
Plan2 Master versions which contain ``e_effect-exam_extra_turn``, preserves
their ordered effect links, and exposes the direct native counter operation
without importing the central Plan2 catalog, horizon, core runtime, or any
Plan3 module.

The Android v3.2.3 native executor is unusually small but its order matters:
the executor increments ``RemainTurn`` by one, then calls
``ExamParameterModel.AddExtraTurn(1)``.  ``AddExtraTurn`` reads and adds to the
separate ``ExtraTurn`` property.  Neither operation reads or writes
``CurrentTurn``.  The ordinary end-turn scheduler decrements ``RemainTurn``
later, and only then decides whether another StartTurn exists.

The adapter therefore owns only this typed target effect.  Other card slots
remain exact data and are reported as companion blockers; no companion effect,
card payment, or card settlement is executed here.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Final, Mapping, Sequence

from .master_db import DEFAULT_DATABASE


PLAN2_NATIVE_CATALOG_EXTRA_TURN_SCHEMA_VERSION: Final = 1
EXTRA_TURN_CATALOG_SCHEMA_VERSION: Final = (
    PLAN2_NATIVE_CATALOG_EXTRA_TURN_SCHEMA_VERSION
)
EXTRA_TURN_ADAPTER_ID: Final = "plan2.native.catalog.extra_turn"

EXTRA_TURN_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamExtraTurn"
EFFECT_EXTRA_TURN: Final = EXTRA_TURN_EFFECT_TYPE
EXTRA_TURN_EFFECT_ID: Final = "e_effect-exam_extra_turn"
EXTRA_TURN_EFFECT_GROUP_ID: Final = "effect_group-visible-exam_extra_turn-000"

PLAN2: Final = "ProducePlanType_Plan2"
CATEGORY_MENTAL_SKILL: Final = "ProduceCardCategory_MentalSkill"
COST_REVIEW: Final = "ExamCostType_ExamReview"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
CARD_MOVE_TRIGGER_UNKNOWN: Final = "ProduceCardMoveEffectTriggerType_Unknown"
EFFECT_UNKNOWN: Final = "ProduceExamEffectType_Unknown"

PLAYABLE_VALUE_ADD_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamPlayableValueAdd"
)
CARD_DRAW_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamCardDraw"
BLOCK_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlock"
PLAYABLE_VALUE_ADD_EFFECT_ID: Final = "e_effect-exam_playable_value_add-01"
CARD_DRAW_EFFECT_ID: Final = "e_effect-exam_card_draw-0001"
BLOCK_EFFECT_IDS: Final = frozenset(
    {"e_effect-exam_block-0001", "e_effect-exam_block-0005"}
)

TARGET_CARD_ID: Final = "p_card-02-men-3_002"
EXTRA_TURN_CARD_ID: Final = TARGET_CARD_ID
TARGET_UPGRADES: Final = (0, 1, 2, 3)
EXTRA_TURN_TARGET_UPGRADES: Final = TARGET_UPGRADES
TARGET_VERSION_COUNT: Final = 4
EXTRA_TURN_TARGET_VERSION_COUNT: Final = TARGET_VERSION_COUNT
TARGET_OCCURRENCE_COUNT: Final = 4
EXTRA_TURN_TARGET_OCCURRENCE_COUNT: Final = TARGET_OCCURRENCE_COUNT
EXPECTED_AFFECTED_VERSION_COUNT: Final = TARGET_VERSION_COUNT
EXPECTED_INSTALLER_OCCURRENCE_COUNT: Final = TARGET_OCCURRENCE_COUNT
EXPECTED_EXECUTABLE_VERSION_COUNT: Final = TARGET_VERSION_COUNT

EXPECTED_TARGET_REFS: Final = tuple(
    (TARGET_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES
)
EXPECTED_ORDERED_EFFECT_IDS: Final = {
    (TARGET_CARD_ID, 0): (
        EXTRA_TURN_EFFECT_ID,
        PLAYABLE_VALUE_ADD_EFFECT_ID,
    ),
    (TARGET_CARD_ID, 1): (
        EXTRA_TURN_EFFECT_ID,
        PLAYABLE_VALUE_ADD_EFFECT_ID,
        CARD_DRAW_EFFECT_ID,
    ),
    (TARGET_CARD_ID, 2): (
        "e_effect-exam_block-0001",
        EXTRA_TURN_EFFECT_ID,
        PLAYABLE_VALUE_ADD_EFFECT_ID,
        CARD_DRAW_EFFECT_ID,
    ),
    (TARGET_CARD_ID, 3): (
        "e_effect-exam_block-0005",
        EXTRA_TURN_EFFECT_ID,
        PLAYABLE_VALUE_ADD_EFFECT_ID,
        CARD_DRAW_EFFECT_ID,
    ),
}
EXPECTED_COST_VALUES: Final = {0: 2, 1: 1, 2: 1, 3: 1}

PLAY_ORIGINS: Final = ("normal", "forced", "extra")
SUPPORTED_PLAY_ORIGINS: Final = PLAY_ORIGINS

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1

# Android v3.2.3 native named points.  The PC input is metadata parity, not a
# second guessed implementation.
ANDROID_VERSION: Final = "Android v3.2.3"
ANDROID_EXTRA_TURN_CONSTRUCTOR: Final = "0x7E794E0"
ANDROID_EXTRA_TURN_EXECUTE: Final = "0x7E794E8"
ANDROID_ADD_EXTRA_TURN: Final = "0x7EBB384"
ANDROID_SET_REMAIN_TURN: Final = "0x7EBB368"
ANDROID_END_TURN_MOVE_NEXT: Final = "0x7EDCE78"
ANDROID_EXTRA_TURN_EXECUTE_TOKEN: Final = "0x0600482B"
ANDROID_ADD_EXTRA_TURN_TOKEN: Final = "0x06004F00"
PC_METADATA_INDEX: Final = (
    "_research/il2cpp/"
    "B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
    "targeted-metadata-index.json"
)

NATIVE_EXTRA_TURN_ORDER: Final = (
    "read-remain-turn-before",
    "set-remain-turn-before-plus-one",
    "read-extra-turn-before",
    "add-extra-turn-one",
    "create-after-minus-before-difference",
    "ordinary-end-boundary-decrements-remain-turn-once-later",
)


class Plan2NativeExtraTurnCatalogError(ValueError):
    """A Master/native row or immutable input is outside this contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class Plan2NativeExtraTurnInputError(Plan2NativeExtraTurnCatalogError):
    """A caller supplied a runtime/program shape that cannot be executed."""


class Plan2NativeExtraTurnPlayOrigin(str, Enum):
    """All three card-play paths reach the same native direct executor."""

    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"


PlayOrigin = Plan2NativeExtraTurnPlayOrigin
ExtraTurnPlayOrigin = Plan2NativeExtraTurnPlayOrigin


def _i32(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise Plan2NativeExtraTurnCatalogError("invalid-i32", label)
    if value < INT32_MIN or value > INT32_MAX:
        raise Plan2NativeExtraTurnCatalogError("i32-out-of-range", label)
    if minimum is not None and value < minimum:
        raise Plan2NativeExtraTurnCatalogError("negative-value", label)
    return value


def _nonnegative_i32(value: object, label: str) -> int:
    return _i32(value, label, minimum=0)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise Plan2NativeExtraTurnCatalogError(
            "invalid-text", label if empty else f"{label}:empty"
        )
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise Plan2NativeExtraTurnCatalogError("invalid-bool", label)
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    if type(value) is not str:
        raise Plan2NativeExtraTurnCatalogError("invalid-json", label)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeExtraTurnCatalogError("invalid-json", label) from error
    if not isinstance(parsed, Mapping):
        raise Plan2NativeExtraTurnCatalogError("invalid-json-object", label)
    return parsed


def _json_array(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not str:
        raise Plan2NativeExtraTurnCatalogError("invalid-json", label)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeExtraTurnCatalogError("invalid-json", label) from error
    if not isinstance(parsed, list):
        raise Plan2NativeExtraTurnCatalogError("invalid-json-array", label)
    return tuple(parsed)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise Plan2NativeExtraTurnCatalogError("invalid-string-array", label)
    result = tuple(value)
    if any(type(item) is not str or not item for item in result):
        raise Plan2NativeExtraTurnCatalogError("invalid-string-array", label)
    return result


def _raw_equal(
    raw: Mapping[str, object], key: str, expected: object, label: str
) -> None:
    if key not in raw or raw[key] != expected:
        raise Plan2NativeExtraTurnCatalogError(
            "master-raw-drift", f"{label}.{key}"
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnNativeEvidence:
    """Pinned native/metadata facts carried with the standalone catalog."""

    android_version: str
    android_constructor: str
    android_execute: str
    android_add_extra_turn: str
    android_end_turn: str
    pc_metadata_index: str
    pc_types: tuple[str, ...]
    pc_method_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.android_version, "native.android_version")
        for name in (
            "android_constructor",
            "android_execute",
            "android_add_extra_turn",
            "android_end_turn",
            "pc_metadata_index",
        ):
            _text(getattr(self, name), f"native.{name}")
        object.__setattr__(self, "pc_types", _string_tuple(self.pc_types, "native.pc_types"))
        object.__setattr__(
            self,
            "pc_method_names",
            _string_tuple(self.pc_method_names, "native.pc_method_names"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "android_version": self.android_version,
            "android_constructor": self.android_constructor,
            "android_execute": self.android_execute,
            "android_add_extra_turn": self.android_add_extra_turn,
            "android_end_turn": self.android_end_turn,
            "pc_metadata_index": self.pc_metadata_index,
            "pc_types": list(self.pc_types),
            "pc_method_names": list(self.pc_method_names),
        }


NATIVE_EVIDENCE: Final = Plan2NativeExtraTurnNativeEvidence(
    android_version=ANDROID_VERSION,
    android_constructor=ANDROID_EXTRA_TURN_CONSTRUCTOR,
    android_execute=ANDROID_EXTRA_TURN_EXECUTE,
    android_add_extra_turn=ANDROID_ADD_EXTRA_TURN,
    android_end_turn=ANDROID_END_TURN_MOVE_NEXT,
    pc_metadata_index=PC_METADATA_INDEX,
    pc_types=(
        "Campus.InGame.Exam.ExtraTurnEffectExecutor",
        "Campus.InGame.Exam.ExamParameterModel",
        "Campus.InGame.Exam.ExamSequence",
    ),
    pc_method_names=(
        "ExtraTurnEffectExecutor.ExecuteEffect",
        "ExamParameterModel.get_ExtraTurn",
        "ExamParameterModel.set_ExtraTurn",
        "ExamParameterModel.AddExtraTurn",
        "ExamParameterModel.get_RemainTurn",
        "ExamParameterModel.set_RemainTurn",
        "ExamParameterModel.get_CurrentTurn",
        "ExamSequence.<ExamLoopTaskAsync>d__94.MoveNext",
    ),
)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeExtraTurnEffectRow:
    """One normalized Master effect row, target or exact companion."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str = ""
    chain_effect_id: str = ""
    effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect.effect_id")
        _text(self.effect_type, "effect.effect_type")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _i32(getattr(self, name), f"effect.{name}")
        _text(self.status_enchant_id, "effect.status_enchant_id", empty=True)
        _text(self.chain_effect_id, "effect.chain_effect_id", empty=True)
        groups = _string_tuple(self.effect_group_ids, "effect.effect_group_ids")
        if not groups:
            raise Plan2NativeExtraTurnCatalogError(
                "missing-effect-group", self.effect_id
            )
        object.__setattr__(self, "effect_group_ids", groups)

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.effect_count,
            "turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
        }


Plan2NativeExtraTurnEffect = Plan2NativeExtraTurnEffectRow


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeExtraTurnEffectSlot:
    """One ordered ``card.playEffects`` link and its effect row."""

    slot_index: int
    effect: Plan2NativeExtraTurnEffectRow
    trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False

    def __post_init__(self) -> None:
        _nonnegative_i32(self.slot_index, "slot.slot_index")
        if not isinstance(self.effect, Plan2NativeExtraTurnEffectRow):
            raise TypeError("slot.effect must be Plan2NativeExtraTurnEffectRow")
        _text(self.trigger_id, "slot.trigger_id", empty=True)
        _bool(self.hide_icon, "slot.hide_icon")
        _bool(self.is_once_play_effect, "slot.is_once_play_effect")

    @property
    def effect_id(self) -> str:
        return self.effect.effect_id

    @property
    def effect_type(self) -> str:
        return self.effect.effect_type

    @property
    def value1(self) -> int:
        return self.effect.value1

    @property
    def value2(self) -> int:
        return self.effect.value2

    @property
    def effect_count(self) -> int:
        return self.effect.effect_count

    @property
    def effect_turn(self) -> int:
        return self.effect.effect_turn

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "effect_group_ids": list(self.effect.effect_group_ids),
        }


Plan2NativeExtraTurnSlot = Plan2NativeExtraTurnEffectSlot


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeExtraTurnBlocker:
    """Exact companion or hard compile blocker for one target version."""

    card_id: str
    upgrade: int
    slot_index: int
    code: str
    family: str
    detail: str
    effect_id: str = ""
    companion: bool = True

    def __post_init__(self) -> None:
        _text(self.card_id, "blocker.card_id")
        _nonnegative_i32(self.upgrade, "blocker.upgrade")
        slot_index = _i32(self.slot_index, "blocker.slot_index")
        if slot_index < -1:
            raise Plan2NativeExtraTurnCatalogError(
                "invalid-slot-index", "blocker.slot_index"
            )
        _text(self.code, "blocker.code")
        _text(self.family, "blocker.family")
        _text(self.detail, "blocker.detail")
        _text(self.effect_id, "blocker.effect_id", empty=True)
        _bool(self.companion, "blocker.companion")
        if self.slot_index >= 0 and not self.effect_id:
            raise Plan2NativeExtraTurnCatalogError(
                "slot-blocker-missing-effect", self.version_ref
            )

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "version_ref": self.version_ref,
            "slot_index": self.slot_index,
            "code": self.code,
            "family": self.family,
            "effect_id": self.effect_id,
            "detail": self.detail,
            "companion": self.companion,
        }


Plan2NativeExtraTurnCompanionBlocker = Plan2NativeExtraTurnBlocker


def _target_shape_reason(effect: object) -> str | None:
    if not isinstance(effect, Plan2NativeExtraTurnEffectRow):
        return "target-effect-row-type"
    if effect.effect_id != EXTRA_TURN_EFFECT_ID:
        return "target-effect-id"
    if effect.effect_type != EXTRA_TURN_EFFECT_TYPE:
        return "target-effect-type"
    if (
        effect.value1,
        effect.value2,
        effect.effect_count,
        effect.effect_turn,
    ) != (0, 0, 0, 0):
        return "target-effect-zero-shape"
    if effect.status_enchant_id or effect.chain_effect_id:
        return "target-effect-nested-shape"
    if effect.effect_group_ids != (EXTRA_TURN_EFFECT_GROUP_ID,):
        return "target-effect-group-shape"
    return None


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnVersion:
    """One exact Plan2 card version containing the target slot."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    stamina: int
    force_stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    effect_slots: tuple[Plan2NativeExtraTurnEffectSlot, ...]
    target_slot_index: int
    companion_blockers: tuple[Plan2NativeExtraTurnBlocker, ...] = ()

    def __post_init__(self) -> None:
        _text(self.card_id, "version.card_id")
        _nonnegative_i32(self.upgrade, "version.upgrade")
        if self.plan_type != PLAN2:
            raise Plan2NativeExtraTurnCatalogError("version-plan-type", self.version_ref)
        _text(self.category, "version.category")
        for name in ("stamina", "force_stamina", "cost_value"):
            _nonnegative_i32(getattr(self, name), f"version.{name}")
        _text(self.cost_type, "version.cost_type")
        _text(self.play_trigger_id, "version.play_trigger_id", empty=True)
        _text(self.move_position_type, "version.move_position_type")
        slots = tuple(self.effect_slots)
        if not slots or any(
            not isinstance(item, Plan2NativeExtraTurnEffectSlot) for item in slots
        ):
            raise TypeError("effect_slots must contain typed slots")
        if tuple(item.slot_index for item in slots) != tuple(range(len(slots))):
            raise Plan2NativeExtraTurnCatalogError(
                "card-effect-order", self.version_ref
            )
        object.__setattr__(self, "effect_slots", slots)
        _nonnegative_i32(self.target_slot_index, "version.target_slot_index")
        if self.target_slot_index >= len(slots):
            raise Plan2NativeExtraTurnCatalogError(
                "target-slot-range", self.version_ref
            )
        target = slots[self.target_slot_index]
        if target.effect_type != EXTRA_TURN_EFFECT_TYPE:
            raise Plan2NativeExtraTurnCatalogError(
                "target-slot-type", self.version_ref
            )
        if target.effect_id != EXTRA_TURN_EFFECT_ID:
            raise Plan2NativeExtraTurnCatalogError(
                "target-slot-id", self.version_ref
            )
        blockers = tuple(self.companion_blockers)
        if any(
            not isinstance(item, Plan2NativeExtraTurnBlocker) for item in blockers
        ):
            raise TypeError("companion_blockers must contain typed blockers")
        if any(item.ref != self.ref for item in blockers):
            raise Plan2NativeExtraTurnCatalogError(
                "blocker-version-mismatch", self.version_ref
            )
        object.__setattr__(self, "companion_blockers", blockers)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(item.effect_id for item in self.effect_slots)

    @property
    def target_slot(self) -> Plan2NativeExtraTurnEffectSlot:
        return self.effect_slots[self.target_slot_index]

    @property
    def target_effect(self) -> Plan2NativeExtraTurnEffectRow:
        return self.target_slot.effect

    @property
    def target_effect_id(self) -> str:
        return self.target_effect.effect_id

    @property
    def effect(self) -> Plan2NativeExtraTurnEffectRow:
        return self.target_effect

    @property
    def value1(self) -> int:
        return self.target_effect.value1

    @property
    def value2(self) -> int:
        return self.target_effect.value2

    @property
    def effect_count(self) -> int:
        return self.target_effect.effect_count

    @property
    def effect_turn(self) -> int:
        return self.target_effect.effect_turn

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.target_slot_index]

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return PLAY_ORIGINS

    @property
    def target_effect_executable(self) -> bool:
        return _target_shape_reason(self.target_effect) is None

    @property
    def executable(self) -> bool:
        return self.target_effect_executable

    @property
    def whole_card_executable(self) -> bool:
        return self.target_effect_executable and not self.companion_blockers

    @property
    def companion_blocker_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.family for item in self.companion_blockers))

    @property
    def companion_effect_ids(self) -> tuple[str, ...]:
        return tuple(
            item.effect_id
            for item in self.effect_slots
            if item.slot_index != self.target_slot_index
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "version_ref": self.version_ref,
            "plan_type": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "force_stamina": self.force_stamina,
            "cost_type": self.cost_type,
            "cost_value": self.cost_value,
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "target_slot_index": self.target_slot_index,
            "target_effect": self.target_effect.to_dict(),
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "effect_slots": [item.to_dict() for item in self.effect_slots],
            "companion_effect_ids": list(self.companion_effect_ids),
            "supported_play_origins": list(self.supported_play_origins),
            "target_effect_executable": self.target_effect_executable,
            "whole_card_executable": self.whole_card_executable,
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
        }


Plan2NativeExtraTurnProgram = Plan2NativeExtraTurnVersion
Plan2ExtraTurnProgram = Plan2NativeExtraTurnVersion


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnCatalog:
    """Immutable target-family catalog suitable for a central handoff."""

    database: str
    effect: Plan2NativeExtraTurnEffectRow
    programs: tuple[Plan2NativeExtraTurnVersion, ...]
    native: Plan2NativeExtraTurnNativeEvidence = NATIVE_EVIDENCE

    def __post_init__(self) -> None:
        _text(self.database, "catalog.database")
        if not isinstance(self.effect, Plan2NativeExtraTurnEffectRow):
            raise TypeError("catalog.effect must be a typed effect row")
        programs = tuple(self.programs)
        if any(
            not isinstance(item, Plan2NativeExtraTurnVersion) for item in programs
        ):
            raise TypeError("catalog.programs must contain typed versions")
        refs = tuple(item.ref for item in programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeExtraTurnCatalogError("catalog-version-order")
        if not isinstance(self.native, Plan2NativeExtraTurnNativeEvidence):
            raise TypeError("catalog.native must be native evidence")
        object.__setattr__(self, "programs", programs)

    @property
    def versions(self) -> tuple[Plan2NativeExtraTurnVersion, ...]:
        return self.programs

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs if item.target_effect_executable)

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def target_effect_executable(self) -> bool:
        return self.executable_version_count == self.affected_version_count == TARGET_VERSION_COUNT

    @property
    def companion_blockers(self) -> tuple[Plan2NativeExtraTurnBlocker, ...]:
        return tuple(
            blocker
            for program in self.programs
            for blocker in program.companion_blockers
        )

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            program.ref for program in self.programs if program.companion_blockers
        )

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            program.ref for program in self.programs if program.whole_card_executable
        )

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.companion_blockers).items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs_by_family: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.companion_blockers:
            refs_by_family.setdefault(blocker.family, set()).add(blocker.ref)
        return {
            family: len(refs)
            for family, refs in sorted(refs_by_family.items())
        }

    def get(self, card_id: str, upgrade: int) -> Plan2NativeExtraTurnVersion | None:
        return next((item for item in self.programs if item.ref == (card_id, upgrade)), None)

    def resolve(self, card_id: str, upgrade: int) -> Plan2NativeExtraTurnVersion:
        program = self.get(card_id, upgrade)
        if program is None:
            raise KeyError(f"uncompiled ExtraTurn version: {card_id}#{upgrade}")
        return program

    version = resolve

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter_id": EXTRA_TURN_ADAPTER_ID,
            "schema_version": PLAN2_NATIVE_CATALOG_EXTRA_TURN_SCHEMA_VERSION,
            "database": self.database,
            "affected_version_count": self.affected_version_count,
            "executable_version_count": self.executable_version_count,
            "target_effect_executable": self.target_effect_executable,
            "companion_blocked_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "whole_card_executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "native": self.native.to_dict(),
            "programs": [item.to_dict() for item in self.programs],
        }


Plan2NativeExtraTurnCatalogProgram = Plan2NativeExtraTurnVersion


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnCompilation:
    """Per-version compile result; hard blockers never become programs."""

    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    catalog: Plan2NativeExtraTurnCatalog
    blockers: tuple[Plan2NativeExtraTurnBlocker, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_CATALOG_EXTRA_TURN_SCHEMA_VERSION:
            raise Plan2NativeExtraTurnCatalogError("unsupported-schema-version")
        _text(self.database, "compilation.database")
        refs = tuple(self.affected_refs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeExtraTurnCatalogError("affected-ref-order")
        _nonnegative_i32(self.occurrence_count, "compilation.occurrence_count")
        if not isinstance(self.catalog, Plan2NativeExtraTurnCatalog):
            raise TypeError("compilation.catalog must be typed")
        if any(item.ref not in refs for item in self.catalog.programs):
            raise Plan2NativeExtraTurnCatalogError("compiled-ref-outside-target")
        blockers = tuple(self.blockers)
        if any(
            not isinstance(item, Plan2NativeExtraTurnBlocker) for item in blockers
        ):
            raise TypeError("compilation.blockers must be typed")
        if any(item.companion for item in blockers):
            raise Plan2NativeExtraTurnCatalogError("hard-blocker-marked-companion")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", blockers)

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.affected_refs

    @property
    def compiled_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def compiled_occurrence_count(self) -> int:
        return len(self.catalog.programs)

    @property
    def failed_occurrence_count(self) -> int:
        return self.occurrence_count - self.compiled_occurrence_count

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.affected_refs if ref not in compiled)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def executable_version_count(self) -> int:
        return self.catalog.executable_version_count

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.executable_refs

    @property
    def target_effect_executable(self) -> bool:
        return (
            self.affected_version_count == TARGET_VERSION_COUNT
            and self.compiled_version_count == TARGET_VERSION_COUNT
            and self.executable_version_count == TARGET_VERSION_COUNT
            and not self.blockers
        )

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers and not self.failed_refs

    @property
    def companion_blockers(self) -> tuple[Plan2NativeExtraTurnBlocker, ...]:
        return self.catalog.companion_blockers

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.companion_blocked_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.whole_card_executable_refs

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return self.catalog.companion_blocker_code_counts

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.blockers).items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        return self.catalog.companion_family_version_counts

    def blockers_for(
        self, ref: tuple[str, int]
    ) -> tuple[Plan2NativeExtraTurnBlocker, ...]:
        return tuple(item for item in self.blockers if item.ref == ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "database": self.database,
            "affected_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "occurrence_count": self.occurrence_count,
            "compiled_version_count": self.compiled_version_count,
            "compiled_occurrence_count": self.compiled_occurrence_count,
            "failed_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.failed_refs
            ],
            "failed_occurrence_count": self.failed_occurrence_count,
            "target_effect_executable": self.target_effect_executable,
            "fully_compiled": self.fully_compiled,
            "executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.executable_refs
            ],
            "blocker_code_counts": self.blocker_code_counts,
            "blockers": [item.to_dict() for item in self.blockers],
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
            "catalog": self.catalog.to_dict(),
        }


Plan2NativeExtraTurnCatalogCompilation = Plan2NativeExtraTurnCompilation


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnHandoffRow:
    """One immutable row accepted by a central catalog caller."""

    version_ref: str
    card_id: str
    upgrade: int
    target_effect_id: str
    target_effect_type: str
    target_slot_index: int
    value1: int
    value2: int
    count: int
    turn: int
    ordered_effect_ids: tuple[str, ...]
    supported_play_origins: tuple[str, ...]
    target_effect_executable: bool
    whole_card_executable: bool
    companion_blockers: tuple[Plan2NativeExtraTurnBlocker, ...]

    def __post_init__(self) -> None:
        _text(self.version_ref, "handoff.version_ref")
        _text(self.card_id, "handoff.card_id")
        _nonnegative_i32(self.upgrade, "handoff.upgrade")
        if self.version_ref != f"{self.card_id}#{self.upgrade}":
            raise Plan2NativeExtraTurnCatalogError("handoff-version-ref")
        _text(self.target_effect_id, "handoff.target_effect_id")
        if self.target_effect_id != EXTRA_TURN_EFFECT_ID:
            raise Plan2NativeExtraTurnCatalogError("handoff-target-effect-id")
        if self.target_effect_type != EXTRA_TURN_EFFECT_TYPE:
            raise Plan2NativeExtraTurnCatalogError("handoff-target-effect-type")
        _nonnegative_i32(self.target_slot_index, "handoff.target_slot_index")
        for name in ("value1", "value2", "count", "turn"):
            _i32(getattr(self, name), f"handoff.{name}")
        object.__setattr__(
            self, "ordered_effect_ids", _string_tuple(self.ordered_effect_ids, "handoff.ordered_effect_ids")
        )
        origins = _string_tuple(self.supported_play_origins, "handoff.supported_play_origins")
        if origins != PLAY_ORIGINS:
            raise Plan2NativeExtraTurnCatalogError("handoff-play-origins")
        object.__setattr__(self, "supported_play_origins", origins)
        if (
            self.target_slot_index >= len(self.ordered_effect_ids)
            or self.ordered_effect_ids[self.target_slot_index] != EXTRA_TURN_EFFECT_ID
            or self.ordered_effect_ids.count(EXTRA_TURN_EFFECT_ID) != 1
        ):
            raise Plan2NativeExtraTurnCatalogError("handoff-target-slot")
        if self.target_effect_executable and (
            self.value1,
            self.value2,
            self.count,
            self.turn,
        ) != (0, 0, 0, 0):
            raise Plan2NativeExtraTurnCatalogError("handoff-target-zero-shape")
        _bool(self.target_effect_executable, "handoff.target_effect_executable")
        _bool(self.whole_card_executable, "handoff.whole_card_executable")
        blockers = tuple(self.companion_blockers)
        if any(not isinstance(item, Plan2NativeExtraTurnBlocker) for item in blockers):
            raise TypeError("handoff.companion_blockers must be typed")
        object.__setattr__(self, "companion_blockers", blockers)
        if self.whole_card_executable and blockers:
            raise Plan2NativeExtraTurnCatalogError("handoff-whole-card-blocked")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    def to_dict(self) -> dict[str, object]:
        return {
            "version_ref": self.version_ref,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "target_effect_id": self.target_effect_id,
            "target_effect_type": self.target_effect_type,
            "target_slot_index": self.target_slot_index,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "supported_play_origins": list(self.supported_play_origins),
            "target_effect_executable": self.target_effect_executable,
            "whole_card_executable": self.whole_card_executable,
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnCentralHandoff:
    """Immutable aggregate handoff; it never invokes central runtime code."""

    adapter_id: str
    schema_version: int
    affected_refs: tuple[tuple[str, int], ...]
    executable_refs: tuple[tuple[str, int], ...]
    rows: tuple[Plan2NativeExtraTurnHandoffRow, ...]

    def __post_init__(self) -> None:
        _text(self.adapter_id, "handoff.adapter_id")
        if self.schema_version != PLAN2_NATIVE_CATALOG_EXTRA_TURN_SCHEMA_VERSION:
            raise Plan2NativeExtraTurnCatalogError("handoff-schema-version")
        affected = tuple(self.affected_refs)
        executable = tuple(self.executable_refs)
        rows = tuple(self.rows)
        if affected != tuple(sorted(affected)) or len(affected) != len(set(affected)):
            raise Plan2NativeExtraTurnCatalogError("handoff-affected-order")
        if executable != tuple(sorted(executable)) or set(executable) - set(affected):
            raise Plan2NativeExtraTurnCatalogError("handoff-executable-order")
        if tuple(row.ref for row in rows) != affected:
            raise Plan2NativeExtraTurnCatalogError("handoff-row-order")
        object.__setattr__(self, "affected_refs", affected)
        object.__setattr__(self, "executable_refs", executable)
        object.__setattr__(self, "rows", rows)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def target_effect_executable(self) -> bool:
        return self.executable_version_count == self.affected_version_count == TARGET_VERSION_COUNT

    @property
    def companion_blockers(self) -> tuple[Plan2NativeExtraTurnBlocker, ...]:
        return tuple(item for row in self.rows for item in row.companion_blockers)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.companion_blockers)

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.whole_card_executable)

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.companion_blockers).items()))

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "schema_version": self.schema_version,
            "affected_version_count": self.affected_version_count,
            "executable_version_count": self.executable_version_count,
            "target_effect_executable": self.target_effect_executable,
            "affected_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.executable_refs
            ],
            "companion_blocked_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "whole_card_executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "rows": [item.to_dict() for item in self.rows],
        }


Plan2NativeExtraTurnHandoff = Plan2NativeExtraTurnCentralHandoff


def _load_effect_row(row: sqlite3.Row) -> Plan2NativeExtraTurnEffectRow:
    effect_id = _text(row["id"], "effect.id")
    raw = _json_object(row["raw_json"], f"{effect_id}.raw_json")
    normalized = {
        "id": effect_id,
        "effectType": row["effect_type"],
        "effectValue1": row["value1"],
        "effectValue2": row["value2"],
        "effectCount": row["effect_count"],
        "effectTurn": row["effect_turn"],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": EFFECT_UNKNOWN,
        "produceCardSearchId": "",
        "produceCardSearchId2": "",
        "movePositionType": MOVE_UNKNOWN,
        "produceCardGrowEffectIds": [],
    }
    for key, expected in normalized.items():
        _raw_equal(raw, key, expected, effect_id)
    groups = _string_tuple(raw.get("effectGroupIds"), f"{effect_id}.effectGroupIds")
    return Plan2NativeExtraTurnEffectRow(
        effect_id=effect_id,
        effect_type=_text(row["effect_type"], f"{effect_id}.effect_type"),
        value1=_i32(row["value1"], f"{effect_id}.value1"),
        value2=_i32(row["value2"], f"{effect_id}.value2"),
        effect_count=_i32(row["effect_count"], f"{effect_id}.effect_count"),
        effect_turn=_i32(row["effect_turn"], f"{effect_id}.effect_turn"),
        status_enchant_id=_text(row["status_enchant_id"], f"{effect_id}.status_enchant_id", empty=True),
        chain_effect_id=_text(row["chain_effect_id"], f"{effect_id}.chain_effect_id", empty=True),
        effect_group_ids=groups,
    )


_LINK_KEYS: Final = frozenset(
    {"produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"}
)


def _load_slot(
    connection: sqlite3.Connection,
    index: int,
    link: object,
    version_ref: str,
) -> Plan2NativeExtraTurnEffectSlot:
    if not isinstance(link, Mapping) or set(link) != _LINK_KEYS:
        raise Plan2NativeExtraTurnCatalogError(
            "card-effect-link-shape", f"{version_ref}.playEffects[{index}]"
        )
    effect_id = _text(link.get("produceExamEffectId"), f"{version_ref}.effect[{index}].id")
    trigger_id = _text(
        link.get("produceExamTriggerId"),
        f"{version_ref}.effect[{index}].trigger",
        empty=True,
    )
    effect_row = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (effect_id,)
    ).fetchone()
    if effect_row is None:
        raise Plan2NativeExtraTurnCatalogError(
            "missing-effect-row", f"{version_ref}:{effect_id}"
        )
    return Plan2NativeExtraTurnEffectSlot(
        slot_index=index,
        effect=_load_effect_row(effect_row),
        trigger_id=trigger_id,
        hide_icon=_bool(link.get("hideIcon"), f"{version_ref}.effect[{index}].hideIcon"),
        is_once_play_effect=_bool(
            link.get("isOncePlayEffect"),
            f"{version_ref}.effect[{index}].isOncePlayEffect",
        ),
    )


def _validate_card_raw(
    row: sqlite3.Row,
    links: tuple[object, ...],
    version_ref: str,
) -> Mapping[str, object]:
    raw = _json_object(row["raw_json"], f"{version_ref}.raw_json")
    expected = {
        "id": row["id"],
        "upgradeCount": row["upgrade_count"],
        "planType": row["plan_type"],
        "category": row["category"],
        "stamina": row["stamina"],
        "forceStamina": 0,
        "costType": row["cost_type"],
        "costValue": row["cost_value"],
        "playProduceExamTriggerId": row["play_trigger_id"],
        "playEffects": list(links),
        "playMovePositionType": row["move_position_type"],
        "moveEffectTriggerType": CARD_MOVE_TRIGGER_UNKNOWN,
        "moveProduceExamEffectIds": [],
        "isEndTurnLost": False,
        "produceCardStatusEnchantId": "",
    }
    for key, value in expected.items():
        _raw_equal(raw, key, value, version_ref)
    return raw


def _build_companion_blockers(
    version_ref: str,
    row: sqlite3.Row,
    slots: Sequence[Plan2NativeExtraTurnEffectSlot],
    target_index: int,
) -> tuple[Plan2NativeExtraTurnBlocker, ...]:
    blockers: list[Plan2NativeExtraTurnBlocker] = []
    # Payment is deliberately outside the effect-family executor.  A central
    # caller must compose the exact Review cost before consuming this handoff.
    blockers.append(
        Plan2NativeExtraTurnBlocker(
            card_id=str(row["id"]),
            upgrade=int(row["upgrade_count"]),
            slot_index=-1,
            code="companion-adapter-pending",
            family="plan2_cost",
            detail=(
                f"{row['cost_type']}:{row['cost_value']} card payment is outside "
                "the ExtraTurn target executor"
            ),
        )
    )
    for slot in slots:
        if slot.slot_index == target_index:
            continue
        # These two scalar rows already have independent central-compatible
        # owners.  Preserve them in ordered_effect_ids without double-claiming
        # their execution here.
        if slot.effect_type in {PLAYABLE_VALUE_ADD_EFFECT_TYPE, BLOCK_EFFECT_TYPE}:
            continue
        if slot.effect_type == CARD_DRAW_EFFECT_TYPE:
            family = "card_draw"
        else:
            family = "unknown_companion"
        blockers.append(
            Plan2NativeExtraTurnBlocker(
                card_id=str(row["id"]),
                upgrade=int(row["upgrade_count"]),
                slot_index=slot.slot_index,
                code="companion-adapter-pending",
                family=family,
                detail=(
                    f"ordered companion slot {slot.slot_index}: "
                    f"{slot.effect_id}:{slot.effect_type} is not executed by "
                    "the ExtraTurn target adapter"
                ),
                effect_id=slot.effect_id,
            )
        )
    return tuple(blockers)


def _compile_version(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> Plan2NativeExtraTurnVersion:
    card_id = _text(row["id"], "card.id")
    upgrade = _nonnegative_i32(row["upgrade_count"], f"{card_id}.upgrade")
    version_ref = f"{card_id}#{upgrade}"
    if row["plan_type"] != PLAN2:
        raise Plan2NativeExtraTurnCatalogError("version-plan-type", version_ref)
    links = _json_array(row["play_effects_json"], f"{version_ref}.play_effects_json")
    _validate_card_raw(row, links, version_ref)
    slots = tuple(
        _load_slot(connection, index, link, version_ref)
        for index, link in enumerate(links)
    )
    target_indices = tuple(
        slot.slot_index
        for slot in slots
        if slot.effect_id == EXTRA_TURN_EFFECT_ID
    )
    if target_indices != (EXPECTED_ORDERED_EFFECT_IDS[(card_id, upgrade)].index(EXTRA_TURN_EFFECT_ID),):
        raise Plan2NativeExtraTurnCatalogError(
            "target-occurrence-in-version", version_ref
        )
    target_index = target_indices[0]
    if tuple(slot.effect_id for slot in slots) != EXPECTED_ORDERED_EFFECT_IDS[(card_id, upgrade)]:
        raise Plan2NativeExtraTurnCatalogError("card-effect-order", version_ref)
    target = slots[target_index]
    target_reason = _target_shape_reason(target.effect)
    if target_reason is not None:
        raise Plan2NativeExtraTurnCatalogError(target_reason, version_ref)
    if target.trigger_id or target.hide_icon or target.is_once_play_effect:
        raise Plan2NativeExtraTurnCatalogError("target-link-shape", version_ref)
    if row["play_trigger_id"] != "":
        raise Plan2NativeExtraTurnCatalogError("card-play-trigger-shape", version_ref)
    if row["category"] != CATEGORY_MENTAL_SKILL:
        raise Plan2NativeExtraTurnCatalogError("card-category-shape", version_ref)
    if int(row["stamina"]) != 0 or int(_validate_card_raw(row, links, version_ref)["forceStamina"]) != 0:
        raise Plan2NativeExtraTurnCatalogError("card-stamina-shape", version_ref)
    if row["cost_type"] != COST_REVIEW or int(row["cost_value"]) != EXPECTED_COST_VALUES[upgrade]:
        raise Plan2NativeExtraTurnCatalogError("card-cost-shape", version_ref)
    if row["move_position_type"] != MOVE_LOST:
        raise Plan2NativeExtraTurnCatalogError("card-move-shape", version_ref)
    blockers = _build_companion_blockers(version_ref, row, slots, target_index)
    return Plan2NativeExtraTurnVersion(
        card_id=card_id,
        upgrade=upgrade,
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina=_nonnegative_i32(row["stamina"], f"{version_ref}.stamina"),
        force_stamina=0,
        cost_type=str(row["cost_type"]),
        cost_value=_nonnegative_i32(row["cost_value"], f"{version_ref}.cost_value"),
        play_trigger_id=str(row["play_trigger_id"]),
        move_position_type=str(row["move_position_type"]),
        effect_slots=slots,
        target_slot_index=target_index,
        companion_blockers=blockers,
    )


def _target_effect_from_database(connection: sqlite3.Connection) -> Plan2NativeExtraTurnEffectRow:
    row = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (EXTRA_TURN_EFFECT_ID,)
    ).fetchone()
    if row is None:
        raise Plan2NativeExtraTurnCatalogError("target-effect-row-missing", EXTRA_TURN_EFFECT_ID)
    return _load_effect_row(row)


def compile_plan2_native_catalog_extra_turn(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeExtraTurnCompilation:
    """Compile exactly the four current Plan2 target versions read-only."""

    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        inventory = connection.execute(
            """
            SELECT c.id, c.upgrade_count, COUNT(*) AS occurrence_count
              FROM card AS c
              JOIN json_each(c.play_effects_json) AS link
                ON json_extract(link.value, '$.produceExamEffectId') = ?
             WHERE c.plan_type = ?
             GROUP BY c.id, c.upgrade_count
             ORDER BY c.id, c.upgrade_count
            """,
            (EXTRA_TURN_EFFECT_ID, PLAN2),
        ).fetchall()
        discovered_refs = tuple(
            (str(row["id"]), int(row["upgrade_count"])) for row in inventory
        )
        occurrence_count = sum(int(row["occurrence_count"]) for row in inventory)
        if discovered_refs != EXPECTED_TARGET_REFS:
            raise Plan2NativeExtraTurnCatalogError(
                "target-inventory-drift",
                f"expected={EXPECTED_TARGET_REFS!r},got={discovered_refs!r}",
            )
        if occurrence_count != TARGET_OCCURRENCE_COUNT:
            raise Plan2NativeExtraTurnCatalogError(
                "target-occurrence-drift", str(occurrence_count)
            )
        target_effect = _target_effect_from_database(connection)
        hard_blockers: list[Plan2NativeExtraTurnBlocker] = []
        programs: list[Plan2NativeExtraTurnVersion] = []
        for card_id, upgrade in EXPECTED_TARGET_REFS:
            row = connection.execute(
                "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
            if row is None:
                hard_blockers.append(
                    Plan2NativeExtraTurnBlocker(
                        card_id,
                        upgrade,
                        -1,
                        "target-card-missing",
                        "target_card",
                        "exact Master card version is missing",
                        companion=False,
                    )
                )
                continue
            try:
                program = _compile_version(connection, row)
            except (KeyError, IndexError, sqlite3.Error, TypeError, ValueError) as error:
                hard_blockers.append(
                    Plan2NativeExtraTurnBlocker(
                        card_id,
                        upgrade,
                        -1,
                        getattr(error, "code", "target-compile-failed"),
                        "target_effect",
                        getattr(error, "detail", str(error)) or str(error),
                        EXTRA_TURN_EFFECT_ID,
                        False,
                    )
                )
            else:
                programs.append(program)
        catalog = Plan2NativeExtraTurnCatalog(
            database=str(database_path),
            effect=target_effect,
            programs=tuple(programs),
            native=NATIVE_EVIDENCE,
        )
    return Plan2NativeExtraTurnCompilation(
        schema_version=PLAN2_NATIVE_CATALOG_EXTRA_TURN_SCHEMA_VERSION,
        database=str(database_path),
        affected_refs=EXPECTED_TARGET_REFS,
        occurrence_count=occurrence_count,
        catalog=catalog,
        blockers=tuple(hard_blockers),
    )


compile_extra_turn_catalog = compile_plan2_native_catalog_extra_turn
compile_plan2_extra_turn_catalog = compile_plan2_native_catalog_extra_turn


def load_plan2_native_catalog_extra_turn(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeExtraTurnCatalog:
    """Load a complete exact catalog or fail closed on its first hard blocker."""

    compilation = compile_plan2_native_catalog_extra_turn(database)
    if compilation.blockers or compilation.failed_refs:
        if compilation.blockers:
            first = compilation.blockers[0]
            raise Plan2NativeExtraTurnCatalogError(
                first.code, f"{first.version_ref}:{first.detail}"
            )
        raise Plan2NativeExtraTurnCatalogError(
            "incomplete-target-catalog", repr(compilation.failed_refs)
        )
    return compilation.catalog


load_extra_turn_catalog = load_plan2_native_catalog_extra_turn
load_plan2_extra_turn_catalog = load_plan2_native_catalog_extra_turn


def build_plan2_native_extra_turn_central_handoff(
    source: Plan2NativeExtraTurnCompilation | Plan2NativeExtraTurnCatalog,
) -> Plan2NativeExtraTurnCentralHandoff:
    """Build rows for a central catalog without importing or invoking it."""

    if isinstance(source, Plan2NativeExtraTurnCompilation):
        if source.blockers or source.failed_refs:
            first = source.blockers[0] if source.blockers else None
            if first is not None:
                raise Plan2NativeExtraTurnCatalogError(
                    "handoff-incomplete-catalog", f"{first.version_ref}:{first.detail}"
                )
            raise Plan2NativeExtraTurnCatalogError(
                "handoff-incomplete-catalog", repr(source.failed_refs)
            )
        catalog = source.catalog
    elif isinstance(source, Plan2NativeExtraTurnCatalog):
        catalog = source
    else:
        raise TypeError("source must be ExtraTurn catalog or compilation")
    rows = tuple(
        Plan2NativeExtraTurnHandoffRow(
            version_ref=program.version_ref,
            card_id=program.card_id,
            upgrade=program.upgrade,
            target_effect_id=program.target_effect_id,
            target_effect_type=program.target_effect.effect_type,
            target_slot_index=program.target_slot_index,
            value1=program.value1,
            value2=program.value2,
            count=program.count,
            turn=program.turn,
            ordered_effect_ids=program.ordered_effect_ids,
            supported_play_origins=program.supported_play_origins,
            target_effect_executable=program.target_effect_executable,
            whole_card_executable=program.whole_card_executable,
            companion_blockers=program.companion_blockers,
        )
        for program in catalog.programs
    )
    return Plan2NativeExtraTurnCentralHandoff(
        adapter_id=EXTRA_TURN_ADAPTER_ID,
        schema_version=PLAN2_NATIVE_CATALOG_EXTRA_TURN_SCHEMA_VERSION,
        affected_refs=catalog.affected_refs,
        executable_refs=catalog.executable_refs,
        rows=rows,
    )


build_plan2_extra_turn_central_handoff = build_plan2_native_extra_turn_central_handoff
build_extra_turn_central_handoff = build_plan2_native_extra_turn_central_handoff
build_plan2_native_extra_turn_handoff = build_plan2_native_extra_turn_central_handoff


def exact_plan2_native_extra_turn_rows(
    source: Plan2NativeExtraTurnCompilation | Plan2NativeExtraTurnCatalog,
) -> tuple[dict[str, object], ...]:
    return tuple(
        row.to_dict()
        for row in build_plan2_native_extra_turn_central_handoff(source).rows
    )


def extra_turn_catalog_summary(
    source: Plan2NativeExtraTurnCompilation | Plan2NativeExtraTurnCatalog,
) -> dict[str, object]:
    compilation = source if isinstance(source, Plan2NativeExtraTurnCompilation) else None
    handoff = build_plan2_native_extra_turn_central_handoff(source)
    return {
        "affected_versions": handoff.affected_version_count,
        "compiled_versions": compilation.compiled_version_count if compilation else handoff.executable_version_count,
        "failed_versions": compilation.failed_version_count if compilation else 0,
        "target_effect_executable_versions": handoff.executable_version_count,
        "companion_blocked_versions": len(handoff.companion_blocked_refs),
        "whole_card_executable_versions": len(handoff.whole_card_executable_refs),
        "companion_blocker_occurrences": len(handoff.companion_blockers),
        "companion_blocker_code_counts": handoff.companion_blocker_code_counts,
        "companion_blocked_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in handoff.companion_blocked_refs
        ],
    }


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnRuntime:
    """Minimal immutable native counter projection.

    ``current_turn=0`` represents the pre-first active turn.  Active turns are
    one-based.  ``remaining_turn`` is the scheduler's signed counter, while
    ``extra_turn`` is the separate native accumulator used for reporting and
    persistence; ExtraTurn itself does not rewrite CurrentTurn.
    """

    remaining_turn: int
    extra_turn: int = 0
    current_turn: int = 0

    def __post_init__(self) -> None:
        for name in ("remaining_turn", "extra_turn", "current_turn"):
            _nonnegative_i32(getattr(self, name), f"runtime.{name}")

    @property
    def terminal(self) -> bool:
        return self.remaining_turn == 0

    @property
    def is_terminal(self) -> bool:
        return self.terminal


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnExecution:
    """Immutable direct target-effect execution result."""

    before: Plan2NativeExtraTurnRuntime
    after: Plan2NativeExtraTurnRuntime
    program: Plan2NativeExtraTurnVersion
    play_origin: str
    resolved: bool
    committed: bool
    remaining_turn_before: int
    remaining_turn_after: int
    extra_turn_before: int
    extra_turn_after: int
    current_turn_before: int
    current_turn_after: int
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def target_effect_executable(self) -> bool:
        return self.resolved

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before

    @property
    def remaining_turn_delta(self) -> int:
        return self.remaining_turn_after - self.remaining_turn_before

    @property
    def extra_turn_delta(self) -> int:
        return self.extra_turn_after - self.extra_turn_before

    @property
    def current_turn_delta(self) -> int:
        return self.current_turn_after - self.current_turn_before


def _normalize_origin(value: object) -> str | None:
    if isinstance(value, Plan2NativeExtraTurnPlayOrigin):
        return value.value
    if type(value) is str and value in PLAY_ORIGINS:
        return value
    return None


def _failed_execution(
    runtime: Plan2NativeExtraTurnRuntime,
    program: Plan2NativeExtraTurnVersion,
    play_origin: str,
    reason: str,
    operations: tuple[str, ...] = (),
) -> Plan2NativeExtraTurnExecution:
    return Plan2NativeExtraTurnExecution(
        before=runtime,
        after=runtime,
        program=program,
        play_origin=play_origin,
        resolved=False,
        committed=False,
        remaining_turn_before=runtime.remaining_turn,
        remaining_turn_after=runtime.remaining_turn,
        extra_turn_before=runtime.extra_turn,
        extra_turn_after=runtime.extra_turn,
        current_turn_before=runtime.current_turn,
        current_turn_after=runtime.current_turn,
        reason=reason,
        operations=operations,
    )


def execute_plan2_native_extra_turn(
    runtime: Plan2NativeExtraTurnRuntime,
    program: Plan2NativeExtraTurnVersion,
    *,
    play_origin: Plan2NativeExtraTurnPlayOrigin | str = Plan2NativeExtraTurnPlayOrigin.NORMAL,
) -> Plan2NativeExtraTurnExecution:
    """Execute only the exact target effect, atomically and immutably."""

    if not isinstance(runtime, Plan2NativeExtraTurnRuntime):
        raise TypeError("runtime must be Plan2NativeExtraTurnRuntime")
    if not isinstance(program, Plan2NativeExtraTurnVersion):
        raise TypeError("program must be Plan2NativeExtraTurnVersion")
    origin = _normalize_origin(play_origin)
    origin_text = origin if origin is not None else str(play_origin)
    if origin is None:
        return _failed_execution(runtime, program, origin_text, "unknown-play-origin")
    target_reason = _target_shape_reason(program.target_effect)
    if target_reason is not None:
        return _failed_execution(runtime, program, origin, target_reason)
    if program.target_slot.trigger_id or program.target_slot.hide_icon or program.target_slot.is_once_play_effect:
        return _failed_execution(runtime, program, origin, "target-link-shape")
    if runtime.terminal:
        return _failed_execution(
            runtime,
            program,
            origin,
            "terminal-boundary",
            ("reject-after-remain-turn-zero",),
        )
    if runtime.remaining_turn == INT32_MAX:
        return _failed_execution(
            runtime,
            program,
            origin,
            "remaining-turn-i32-overflow",
            ("reject-before-set-remain-turn",),
        )
    if runtime.extra_turn == INT32_MAX:
        return _failed_execution(
            runtime,
            program,
            origin,
            "extra-turn-i32-overflow",
            ("reject-before-add-extra-turn",),
        )
    after = replace(
        runtime,
        remaining_turn=runtime.remaining_turn + 1,
        extra_turn=runtime.extra_turn + 1,
    )
    return Plan2NativeExtraTurnExecution(
        before=runtime,
        after=after,
        program=program,
        play_origin=origin,
        resolved=True,
        committed=True,
        remaining_turn_before=runtime.remaining_turn,
        remaining_turn_after=after.remaining_turn,
        extra_turn_before=runtime.extra_turn,
        extra_turn_after=after.extra_turn,
        current_turn_before=runtime.current_turn,
        current_turn_after=after.current_turn,
        operations=NATIVE_EXTRA_TURN_ORDER,
    )


execute_plan2_native_extra_turn_effect = execute_plan2_native_extra_turn
execute_extra_turn = execute_plan2_native_extra_turn


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnEndTurnTransition:
    """Immutable ordinary EndTurn boundary, including terminal decision."""

    before: Plan2NativeExtraTurnRuntime
    after: Plan2NativeExtraTurnRuntime
    resolved: bool
    committed: bool
    terminal: bool
    next_start_turn_available: bool
    remaining_turn_before: int
    remaining_turn_after: int
    extra_turn_before: int
    extra_turn_after: int
    current_turn_before: int
    current_turn_after: int
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before


def advance_plan2_native_extra_turn_end_turn(
    runtime: Plan2NativeExtraTurnRuntime,
) -> Plan2NativeExtraTurnEndTurnTransition:
    """Apply the later ordinary EndTurn decrement and terminal boundary."""

    if not isinstance(runtime, Plan2NativeExtraTurnRuntime):
        raise TypeError("runtime must be Plan2NativeExtraTurnRuntime")
    if runtime.terminal:
        return Plan2NativeExtraTurnEndTurnTransition(
            runtime,
            runtime,
            False,
            False,
            True,
            False,
            runtime.remaining_turn,
            runtime.remaining_turn,
            runtime.extra_turn,
            runtime.extra_turn,
            runtime.current_turn,
            runtime.current_turn,
            "terminal-boundary",
            ("reject-already-terminal",),
        )
    after = replace(runtime, remaining_turn=runtime.remaining_turn - 1)
    terminal = after.remaining_turn == 0
    return Plan2NativeExtraTurnEndTurnTransition(
        before=runtime,
        after=after,
        resolved=True,
        committed=True,
        terminal=terminal,
        next_start_turn_available=not terminal,
        remaining_turn_before=runtime.remaining_turn,
        remaining_turn_after=after.remaining_turn,
        extra_turn_before=runtime.extra_turn,
        extra_turn_after=after.extra_turn,
        current_turn_before=runtime.current_turn,
        current_turn_after=after.current_turn,
        operations=(
            "phase:ProduceExamPhaseType_ExamEndTurn",
            "get-remain-turn",
            "set-remain-turn:-1",
            "terminal:exam-end-complete" if terminal else "next:start-turn",
        ),
    )


end_plan2_native_extra_turn = advance_plan2_native_extra_turn_end_turn
advance_extra_turn_end_turn = advance_plan2_native_extra_turn_end_turn


@dataclass(frozen=True, slots=True)
class Plan2NativeExtraTurnStartTurnTransition:
    """Immutable next-active-turn CurrentTurn increment."""

    before: Plan2NativeExtraTurnRuntime
    after: Plan2NativeExtraTurnRuntime
    resolved: bool
    committed: bool
    current_turn_before: int
    current_turn_after: int
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before


def advance_plan2_native_extra_turn_start_turn(
    runtime: Plan2NativeExtraTurnRuntime,
) -> Plan2NativeExtraTurnStartTurnTransition:
    """Enter the next active turn without changing ExtraTurn."""

    if not isinstance(runtime, Plan2NativeExtraTurnRuntime):
        raise TypeError("runtime must be Plan2NativeExtraTurnRuntime")
    if runtime.terminal:
        return Plan2NativeExtraTurnStartTurnTransition(
            runtime,
            runtime,
            False,
            False,
            runtime.current_turn,
            runtime.current_turn,
            "terminal-boundary",
            ("reject-no-following-start-turn",),
        )
    if runtime.current_turn == INT32_MAX:
        return Plan2NativeExtraTurnStartTurnTransition(
            runtime,
            runtime,
            False,
            False,
            runtime.current_turn,
            runtime.current_turn,
            "current-turn-i32-overflow",
            ("reject-before-current-turn-increment",),
        )
    after = replace(runtime, current_turn=runtime.current_turn + 1)
    return Plan2NativeExtraTurnStartTurnTransition(
        runtime,
        after,
        True,
        True,
        runtime.current_turn,
        after.current_turn,
        operations=("next:ProduceExamPhaseType_ExamStartTurn", "set-current-turn:+1"),
    )


start_plan2_native_extra_turn = advance_plan2_native_extra_turn_start_turn
advance_extra_turn_start_turn = advance_plan2_native_extra_turn_start_turn


def _program_for_handoff(
    source: Plan2NativeExtraTurnCatalog | Plan2NativeExtraTurnCompilation,
    row: Plan2NativeExtraTurnHandoffRow | Plan2NativeExtraTurnVersion,
) -> Plan2NativeExtraTurnVersion:
    if isinstance(source, Plan2NativeExtraTurnCompilation):
        if source.blockers or source.failed_refs:
            raise Plan2NativeExtraTurnCatalogError("handoff-source-incomplete")
        catalog = source.catalog
    elif isinstance(source, Plan2NativeExtraTurnCatalog):
        catalog = source
    else:
        raise TypeError("source must be ExtraTurn catalog or compilation")
    if isinstance(row, Plan2NativeExtraTurnVersion):
        return catalog.resolve(row.card_id, row.upgrade)
    if not isinstance(row, Plan2NativeExtraTurnHandoffRow):
        raise TypeError("handoff row must be typed")
    program = catalog.resolve(row.card_id, row.upgrade)
    if (
        row.version_ref != program.version_ref
        or row.target_effect_id != program.target_effect_id
        or row.target_effect_type != program.target_effect.effect_type
        or row.target_slot_index != program.target_slot_index
        or (row.value1, row.value2, row.count, row.turn)
        != (program.value1, program.value2, program.count, program.turn)
        or row.ordered_effect_ids != program.ordered_effect_ids
        or row.supported_play_origins != program.supported_play_origins
        or row.target_effect_executable != program.target_effect_executable
        or row.whole_card_executable != program.whole_card_executable
        or row.companion_blockers != program.companion_blockers
    ):
        raise Plan2NativeExtraTurnCatalogError("handoff-row-drift", row.version_ref)
    return program


def execute_plan2_native_extra_turn_handoff(
    source: Plan2NativeExtraTurnRuntime
    | Plan2NativeExtraTurnCatalog
    | Plan2NativeExtraTurnCompilation,
    handoff_or_program: Plan2NativeExtraTurnVersion
    | Plan2NativeExtraTurnHandoffRow
    | Plan2NativeExtraTurnRuntime,
    runtime: Plan2NativeExtraTurnRuntime | None = None,
    *,
    play_origin: Plan2NativeExtraTurnPlayOrigin | str = Plan2NativeExtraTurnPlayOrigin.NORMAL,
) -> Plan2NativeExtraTurnExecution:
    """Execute a typed program directly or resolve one typed handoff row.

    Supported call forms are ``(runtime, program)``, ``(catalog, program,
    runtime)``, and ``(catalog, handoff_row, runtime)``.  No form calls a
    central registry or runtime.
    """

    if isinstance(source, Plan2NativeExtraTurnRuntime):
        if runtime is not None:
            raise TypeError("runtime must be omitted when source is runtime")
        before = source
        candidate = handoff_or_program
        if not isinstance(candidate, Plan2NativeExtraTurnVersion):
            raise TypeError("direct runtime form requires a typed program")
        program = candidate
    else:
        if runtime is None:
            if isinstance(handoff_or_program, Plan2NativeExtraTurnRuntime):
                raise TypeError("catalog form requires a handoff/program and runtime")
            raise TypeError("catalog form requires runtime")
        before = runtime
        program = _program_for_handoff(source, handoff_or_program)
    return execute_plan2_native_extra_turn(before, program, play_origin=play_origin)


execute_extra_turn_handoff = execute_plan2_native_extra_turn_handoff


__all__ = [
    "ANDROID_ADD_EXTRA_TURN",
    "ANDROID_ADD_EXTRA_TURN_TOKEN",
    "ANDROID_END_TURN_MOVE_NEXT",
    "ANDROID_EXTRA_TURN_CONSTRUCTOR",
    "ANDROID_EXTRA_TURN_EXECUTE",
    "ANDROID_EXTRA_TURN_EXECUTE_TOKEN",
    "ANDROID_VERSION",
    "BLOCK_EFFECT_TYPE",
    "CARD_DRAW_EFFECT_TYPE",
    "CATEGORY_MENTAL_SKILL",
    "COST_REVIEW",
    "EFFECT_EXTRA_TURN",
    "EXTRA_TURN_ADAPTER_ID",
    "EXTRA_TURN_CARD_ID",
    "EXTRA_TURN_CATALOG_SCHEMA_VERSION",
    "EXTRA_TURN_EFFECT_GROUP_ID",
    "EXTRA_TURN_EFFECT_ID",
    "EXTRA_TURN_EFFECT_TYPE",
    "EXTRA_TURN_TARGET_OCCURRENCE_COUNT",
    "EXTRA_TURN_TARGET_UPGRADES",
    "EXTRA_TURN_TARGET_VERSION_COUNT",
    "EXPECTED_AFFECTED_VERSION_COUNT",
    "EXPECTED_EXECUTABLE_VERSION_COUNT",
    "EXPECTED_ORDERED_EFFECT_IDS",
    "EXPECTED_TARGET_REFS",
    "EXPECTED_INSTALLER_OCCURRENCE_COUNT",
    "INT32_MAX",
    "INT32_MIN",
    "MOVE_LOST",
    "NATIVE_EVIDENCE",
    "NATIVE_EXTRA_TURN_ORDER",
    "PC_METADATA_INDEX",
    "PLAY_ORIGINS",
    "SUPPORTED_PLAY_ORIGINS",
    "Plan2NativeExtraTurnBlocker",
    "Plan2NativeExtraTurnCatalog",
    "Plan2NativeExtraTurnCatalogCompilation",
    "Plan2NativeExtraTurnCatalogError",
    "Plan2NativeExtraTurnCentralHandoff",
    "Plan2NativeExtraTurnCompilation",
    "Plan2NativeExtraTurnEffect",
    "Plan2NativeExtraTurnEffectRow",
    "Plan2NativeExtraTurnEffectSlot",
    "Plan2NativeExtraTurnEndTurnTransition",
    "Plan2NativeExtraTurnExecution",
    "Plan2NativeExtraTurnHandoff",
    "Plan2NativeExtraTurnHandoffRow",
    "Plan2NativeExtraTurnInputError",
    "Plan2NativeExtraTurnNativeEvidence",
    "Plan2NativeExtraTurnPlayOrigin",
    "Plan2NativeExtraTurnProgram",
    "Plan2NativeExtraTurnRuntime",
    "Plan2NativeExtraTurnSlot",
    "Plan2NativeExtraTurnStartTurnTransition",
    "Plan2NativeExtraTurnVersion",
    "PlayOrigin",
    "advance_extra_turn_end_turn",
    "advance_extra_turn_start_turn",
    "advance_plan2_native_extra_turn_end_turn",
    "advance_plan2_native_extra_turn_start_turn",
    "build_extra_turn_central_handoff",
    "build_plan2_extra_turn_central_handoff",
    "build_plan2_native_extra_turn_central_handoff",
    "build_plan2_native_extra_turn_handoff",
    "compile_extra_turn_catalog",
    "compile_plan2_extra_turn_catalog",
    "compile_plan2_native_catalog_extra_turn",
    "exact_plan2_native_extra_turn_rows",
    "execute_extra_turn",
    "execute_extra_turn_handoff",
    "execute_plan2_native_extra_turn",
    "execute_plan2_native_extra_turn_effect",
    "execute_plan2_native_extra_turn_handoff",
    "extra_turn_catalog_summary",
    "load_extra_turn_catalog",
    "load_plan2_extra_turn_catalog",
    "load_plan2_native_catalog_extra_turn",
    "end_plan2_native_extra_turn",
    "start_plan2_native_extra_turn",
]
