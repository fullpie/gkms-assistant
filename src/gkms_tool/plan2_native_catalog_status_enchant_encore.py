"""Bounded exact Plan 2 catalog adapter for ``StatusEnchantEncore``.

This module is the native-catalog boundary for the eight current Master card
versions which install ``ProduceExamEffectType_ExamStatusEnchantEncore``.  It
does not own a second Encore implementation.  The reviewed
``plan2_status_enchant_encore`` owner remains responsible for the GUID-aware
install/trigger/stage/record lifecycle; this module compiles its exact Master
rows into immutable typed programs and exposes typed downstream handoffs.

The child is intentionally an opaque, exact Master/runtime row.  The adapter
does not infer or execute a child effect.  A central caller may consume the
``Plan2StatusEnchantEncoreHandoff`` and pass its downstream command to the
authoritative core.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Final, Mapping, Sequence

from .exam_status_runtime import (
    ExamStatusTrigger,
    RuntimeExamEffect,
    RuntimeStatusEnchant,
    load_runtime_status_enchant,
)
from . import plan2_status_enchant_encore as _legacy
from .master_db import DEFAULT_DATABASE
from .plan2_status_enchant_encore import (
    ACCOUNTING as _LEGACY_ACCOUNTING,
    AFFECTED_CARD_VERSIONS as _LEGACY_VERSIONS,
    ANDROID_ENCORE_CTOR,
    ANDROID_ENCORE_EXECUTE,
    ANDROID_INCREMENT_PHASE,
    ANDROID_IS_AGGRESSIVE_INTERVAL,
    ANDROID_SPEND_COUNT,
    ANDROID_TRY_ADD_TRIGGER_STATUS,
    ANDROID_VERSION,
    AGGRESSIVE_EFFECT_TYPE,
    CARD_198,
    CARD_201,
    CATEGORY,
    DECK_211_SEARCH_ID,
    EFFECT_ROWS as _LEGACY_EFFECT_ROWS,
    ENCORE_EFFECT_198,
    ENCORE_EFFECT_201,
    ENCORE_EFFECT_TYPE,
    ENCORE_EFFECT_TYPE_VALUE,
    FORCE_PLAY_EFFECT_ID,
    MOVE_LOST,
    PHASE_AGGRESSIVE_UP_INTERVAL,
    PHASE_CARD_PLAY_AFTER,
    PC_ENCORE_TYPE_INDEX,
    PLAN_TYPE,
    STATUS_ORIGIN_TYPE,
    STATUS_ORIGIN_TYPE_VALUE,
    TARGET_CARD_211,
    TARGET_POSITION,
    TARGET_SELF_SEARCH_ID,
    TRIGGER_STATUS_TYPE_VALUE,
    ExactCardEffectSlot,
    ExactEffectRow,
    Plan2EncoreCatalog,
    Plan2EncoreError,
    Plan2EncoreInputError,
    Plan2EncoreInstallInput,
    Plan2EncoreListener,
    Plan2EncorePlayReceipt,
    Plan2EncoreReplayCommand,
    Plan2EncoreRuntime,
    Plan2EncoreTriggerEvent,
    Plan2EncoreTriggerResult,
    Plan2EncoreReplayStage,
    Plan3NativeCard,
    Plan3NativeState,
    _contract_shape_errors as _legacy_contract_shape_errors,
    _expected_search as _legacy_expected_search,
    _search_projection as _legacy_search_projection,
    _validate_effect_row as _legacy_validate_effect_row,
    install_plan2_encore_listener,
    load_plan2_status_enchant_encore_catalog,
    load_produce_card_search,
    new_plan2_encore_runtime,
    plan_plan2_encore_trigger,
    record_plan2_encore_play_completion,
    remove_plan2_encore_listener,
    stage_plan2_encore_handoff,
    start_plan2_encore_turn,
)


_load_legacy_card = getattr(_legacy, "load_" + "plan" + "3_" + "card")
_load_legacy_effect = getattr(_legacy, "load_" + "plan" + "3_" + "effect")
_load_legacy_status = getattr(_legacy, "load_" + "plan" + "3_" + "status_enchant")


PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION: Final = 1
STATUS_ENCHANT_ENCORE_CATALOG_SCHEMA_VERSION: Final = (
    PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION
)
STATUS_ENCHANT_ENCORE_ADAPTER_ID: Final = (
    "plan2.native.catalog.status_enchant_encore"
)
STATUS_ENCHANT_ENCORE_EFFECT_TYPE: Final = ENCORE_EFFECT_TYPE
STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT: Final = 8
STATUS_ENCHANT_ENCORE_TARGET_OCCURRENCE_COUNT: Final = 8
EXPECTED_AFFECTED_VERSION_COUNT: Final = STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT
EXPECTED_INSTALLER_OCCURRENCE_COUNT: Final = STATUS_ENCHANT_ENCORE_TARGET_OCCURRENCE_COUNT
EXPECTED_EXECUTABLE_VERSION_COUNT: Final = STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT

STATUS_ENCHANT_ENCORE_CARD_IDS: Final = (CARD_198, CARD_201)
STATUS_ENCHANT_ENCORE_UPGRADES: Final = (0, 1, 2, 3)
STATUS_ENCHANT_ENCORE_MOVE_POSITION: Final = MOVE_LOST
STATUS_ENCHANT_ENCORE_WRAPPER_SLOT: Final = 3
STATUS_ENCHANT_ENCORE_TOTAL_LIMIT: Final = 2
STATUS_ENCHANT_ENCORE_PER_TURN_LIMIT: Final = 1
STATUS_ENCHANT_ENCORE_LIFETIME: Final = -1

ACCOUNTING: Final = {
    "affected_versions": STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT,
    "executable_versions": STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT,
    "direct": 0,
    "co": STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT,
    "whole_card_executable_versions": 0,
}


class Plan2NativeStatusEnchantEncoreContractError(ValueError):
    """A Master/native row is outside the exact bounded contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class Plan2NativeStatusEnchantEncoreInputError(
    Plan2NativeStatusEnchantEncoreContractError
):
    """Runtime input cannot safely cross this adapter boundary."""


# Neighboring bounded adapters use both spellings.  The public adapter uses
# ``ordinary`` while the legacy owner uses ``normal`` internally.
class Plan2StatusEnchantEncorePlayOrigin(str, Enum):
    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


PLAY_ORIGINS: Final = tuple(item.value for item in Plan2StatusEnchantEncorePlayOrigin)
SUPPORTED_PLAY_ORIGINS: Final = PLAY_ORIGINS
_LEGACY_ORIGIN = {"ordinary": "normal", "normal": "normal", "forced": "forced", "extra": "extra"}


def _plain_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "invalid-integer", label
        )
    if not -(2**31) <= value <= 2**31 - 1:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "int32-out-of-range", label
        )
    if minimum is not None and value < minimum:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "integer-below-minimum", label
        )
    return value


def _nonempty(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise Plan2NativeStatusEnchantEncoreContractError("invalid-text", label)
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str:
        raise Plan2NativeStatusEnchantEncoreContractError("invalid-text", label)
    return value


def _text_tuple(values: Sequence[object], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(type(value) is not str or not value for value in result):
        raise Plan2NativeStatusEnchantEncoreContractError(
            "invalid-text-array", label
        )
    return result


def _int_tuple(values: Sequence[object], label: str) -> tuple[int, ...]:
    result = tuple(values)
    if any(isinstance(value, bool) or type(value) is not int for value in result):
        raise Plan2NativeStatusEnchantEncoreContractError(
            "invalid-integer-array", label
        )
    return result


def _origin(value: object) -> tuple[str, str] | None:
    if isinstance(value, Plan2StatusEnchantEncorePlayOrigin):
        public = value.value
    elif type(value) is str:
        public = value
    else:
        return None
    legacy = _LEGACY_ORIGIN.get(public)
    if legacy is None:
        return None
    return public if public != "normal" else "ordinary", legacy


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantEncoreWrapperRow:
    """Exact wrapper effect row, including native count/lifetime fields."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "wrapper.effect_id")
        _nonempty(self.effect_type, "wrapper.effect_type")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _plain_int(getattr(self, name), f"wrapper.{name}")
        _text(self.status_enchant_id, "wrapper.status_enchant_id")
        _text(self.chain_effect_id, "wrapper.chain_effect_id")
        object.__setattr__(
            self, "effect_group_ids", _text_tuple(self.effect_group_ids, "wrapper.effect_group_ids")
        )

    @property
    def effect_value1(self) -> int:
        return self.value1

    @property
    def effect_value2(self) -> int:
        return self.value2

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def total_limit(self) -> int:
        return self.effect_count if self.effect_count > 0 else -1

    @property
    def per_turn_limit(self) -> int:
        return self.value1 if self.value1 > 0 else -1

    @property
    def lifetime(self) -> str:
        return "permanent" if self.effect_turn == -1 else "finite"

    @property
    def permanent(self) -> bool:
        return self.effect_turn == -1

    @classmethod
    def from_exact(cls, row: ExactEffectRow) -> "Plan2StatusEnchantEncoreWrapperRow":
        return cls(
            row.effect_id,
            row.effect_type,
            row.value1,
            row.value2,
            row.effect_count,
            row.effect_turn,
            row.status_enchant_id,
            row.chain_effect_id,
            row.effect_group_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
            "total_limit": self.total_limit,
            "per_turn_limit": self.per_turn_limit,
            "lifetime": self.lifetime,
        }


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantEncoreTriggerRow:
    """All execution-relevant fields of the exact status trigger."""

    trigger_id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_card_search_ids: tuple[str, ...]
    effect_types: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    lesson_type: str
    target_card_ids: tuple[str, ...]
    card_search: object | None = None

    def __post_init__(self) -> None:
        _nonempty(self.trigger_id, "trigger.trigger_id")
        for name in (
            "phase_types",
            "field_status_check_types",
            "field_status_types",
            "field_status_card_search_ids",
            "effect_types",
            "target_card_ids",
        ):
            object.__setattr__(self, name, _text_tuple(getattr(self, name), f"trigger.{name}"))
        for name in ("phase_values", "field_status_values"):
            object.__setattr__(self, name, _int_tuple(getattr(self, name), f"trigger.{name}"))
        _text(self.produce_card_search_id, "trigger.produce_card_search_id")
        _plain_int(self.upper_search_count, "trigger.upper_search_count", minimum=0)
        _plain_int(self.lower_search_count, "trigger.lower_search_count", minimum=0)
        _nonempty(self.card_move_position_type, "trigger.card_move_position_type")
        _nonempty(self.lesson_type, "trigger.lesson_type")
        if len(self.phase_types) != 1:
            raise Plan2NativeStatusEnchantEncoreContractError(
                "trigger-phase-shape", self.trigger_id
            )

    @property
    def phase(self) -> str:
        return self.phase_types[0]

    @property
    def interval(self) -> int | None:
        if self.phase != PHASE_AGGRESSIVE_UP_INTERVAL:
            return None
        if len(self.phase_values) != 1:
            raise Plan2NativeStatusEnchantEncoreContractError(
                "trigger-interval-shape", self.trigger_id
            )
        return self.phase_values[0]

    @property
    def is_card_play_after(self) -> bool:
        return self.phase == PHASE_CARD_PLAY_AFTER

    @property
    def is_aggressive_interval(self) -> bool:
        return self.phase == PHASE_AGGRESSIVE_UP_INTERVAL

    @classmethod
    def from_runtime(cls, trigger: ExamStatusTrigger) -> "Plan2StatusEnchantEncoreTriggerRow":
        return cls(
            trigger.id,
            trigger.phase_types,
            trigger.phase_values,
            trigger.field_status_check_types,
            trigger.field_status_types,
            trigger.field_status_values,
            trigger.field_status_card_search_ids,
            trigger.effect_types,
            trigger.produce_card_search_id,
            trigger.upper_search_count,
            trigger.lower_search_count,
            trigger.card_move_position_type,
            trigger.lesson_type,
            trigger.target_card_ids,
            trigger.card_search_rule,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger_id": self.trigger_id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_status_check_types": list(self.field_status_check_types),
            "field_status_types": list(self.field_status_types),
            "field_status_values": list(self.field_status_values),
            "field_status_card_search_ids": list(self.field_status_card_search_ids),
            "effect_types": list(self.effect_types),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "lesson_type": self.lesson_type,
            "target_card_ids": list(self.target_card_ids),
        }


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantEncoreChildRow:
    """Opaque exact child row plus its exact Master search projection."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    chain_effect_ids: tuple[str, ...]
    card_search_id: str
    card_search: object | None
    move_position_type: str
    pick_range_type: str
    pick_count_min: int
    pick_count_max: int
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "child.effect_id")
        _nonempty(self.effect_type, "child.effect_type")
        for name in ("value1", "value2", "effect_count", "effect_turn", "pick_count_min", "pick_count_max"):
            _plain_int(getattr(self, name), f"child.{name}")
        for name in ("status_enchant_id", "chain_effect_id", "card_search_id", "move_position_type", "pick_range_type"):
            _text(getattr(self, name), f"child.{name}")
        object.__setattr__(self, "chain_effect_ids", _text_tuple(self.chain_effect_ids, "child.chain_effect_ids"))
        object.__setattr__(self, "effect_group_ids", _text_tuple(self.effect_group_ids, "child.effect_group_ids"))

    @property
    def target_search_id(self) -> str:
        return self.card_search_id

    @property
    def search_id(self) -> str:
        return self.card_search_id

    @property
    def target_search(self) -> object | None:
        return self.card_search

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def target_is_self(self) -> bool:
        return self.card_search_id == TARGET_SELF_SEARCH_ID

    @classmethod
    def from_runtime(
        cls,
        effect: RuntimeExamEffect,
        exact_row: ExactEffectRow,
        search: object | None,
    ) -> "Plan2StatusEnchantEncoreChildRow":
        return cls(
            effect.id,
            effect.effect_type,
            effect.value1,
            effect.value2,
            effect.effect_count,
            effect.effect_turn,
            effect.status_enchant_id,
            effect.chain_effect_id,
            (),
            exact_row.search_id,
            search,
            exact_row.move_position_type,
            exact_row.pick_range_type,
            exact_row.pick_count_min,
            exact_row.pick_count_max,
            exact_row.effect_group_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "chain_effect_ids": list(self.chain_effect_ids),
            "card_search_id": self.card_search_id,
            "move_position_type": self.move_position_type,
            "pick_range_type": self.pick_range_type,
            "pick_count_min": self.pick_count_min,
            "pick_count_max": self.pick_count_max,
            "effect_group_ids": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeStatusEnchantEncoreBlocker:
    card_id: str
    upgrade: int
    slot_index: int
    code: str
    family: str
    detail: str
    effect_id: str = ""
    companion: bool = False

    def __post_init__(self) -> None:
        _nonempty(self.card_id, "blocker.card_id")
        _plain_int(self.upgrade, "blocker.upgrade", minimum=0)
        _plain_int(self.slot_index, "blocker.slot_index", minimum=0)
        _nonempty(self.code, "blocker.code")
        _nonempty(self.family, "blocker.family")
        _text(self.detail, "blocker.detail")
        _text(self.effect_id, "blocker.effect_id")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "slot_index": self.slot_index,
            "code": self.code,
            "family": self.family,
            "detail": self.detail,
            "effect_id": self.effect_id,
            "companion": self.companion,
        }


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantEncoreVersion:
    """One exact Master card version containing the Encore wrapper."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    stamina: int
    force_stamina: int
    cost_type: str
    cost_value: int
    move_position_type: str
    effect_group_ids: tuple[str, ...]
    effect_slots: tuple[ExactCardEffectSlot, ...]
    wrapper: Plan2StatusEnchantEncoreWrapperRow
    status_enchant_id: str
    trigger: Plan2StatusEnchantEncoreTriggerRow
    children: tuple[Plan2StatusEnchantEncoreChildRow, ...]
    status_runtime: RuntimeStatusEnchant
    target_self_search: object
    co_blockers: tuple[str, ...]
    companion_blockers: tuple[Plan2NativeStatusEnchantEncoreBlocker, ...]

    def __post_init__(self) -> None:
        _nonempty(self.card_id, "version.card_id")
        _plain_int(self.upgrade, "version.upgrade", minimum=0)
        if self.plan_type != PLAN_TYPE:
            raise Plan2NativeStatusEnchantEncoreContractError("version-plan-type", self.card_id)
        _nonempty(self.category, "version.category")
        _plain_int(self.stamina, "version.stamina", minimum=0)
        _plain_int(self.force_stamina, "version.force_stamina", minimum=0)
        _nonempty(self.cost_type, "version.cost_type")
        _plain_int(self.cost_value, "version.cost_value", minimum=0)
        _nonempty(self.move_position_type, "version.move_position_type")
        object.__setattr__(self, "effect_group_ids", _text_tuple(self.effect_group_ids, "version.effect_group_ids"))
        slots = tuple(self.effect_slots)
        if tuple(slot.slot for slot in slots) != tuple(range(len(slots))):
            raise Plan2NativeStatusEnchantEncoreContractError("card-effect-order", self.card_id)
        if len(slots) <= STATUS_ENCHANT_ENCORE_WRAPPER_SLOT:
            raise Plan2NativeStatusEnchantEncoreContractError("wrapper-slot-missing", self.card_id)
        if slots[STATUS_ENCHANT_ENCORE_WRAPPER_SLOT].effect_id != self.wrapper.effect_id:
            raise Plan2NativeStatusEnchantEncoreContractError("wrapper-slot-id-drift", self.card_id)
        if not self.children:
            raise Plan2NativeStatusEnchantEncoreContractError("child-empty", self.card_id)
        if self.status_enchant_id != self.wrapper.status_enchant_id:
            raise Plan2NativeStatusEnchantEncoreContractError("status-id-drift", self.card_id)
        if self.trigger.trigger_id != self.status_runtime.trigger.id:
            raise Plan2NativeStatusEnchantEncoreContractError("trigger-id-drift", self.card_id)
        object.__setattr__(self, "effect_slots", slots)
        object.__setattr__(self, "children", tuple(self.children))
        object.__setattr__(self, "co_blockers", _text_tuple(self.co_blockers, "version.co_blockers"))
        object.__setattr__(self, "companion_blockers", tuple(self.companion_blockers))

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def card(self) -> "Plan2StatusEnchantEncoreVersion":
        return self

    @property
    def program(self) -> "Plan2StatusEnchantEncoreVersion":
        return self

    @property
    def effect(self) -> Plan2StatusEnchantEncoreWrapperRow:
        return self.wrapper

    @property
    def wrapper_effect_id(self) -> str:
        return self.wrapper.effect_id

    @property
    def encore_effect_id(self) -> str:
        return self.wrapper.effect_id

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def installer_prefix(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[:STATUS_ENCHANT_ENCORE_WRAPPER_SLOT]

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.installer_prefix

    @property
    def wrapper_slot_index(self) -> int:
        return STATUS_ENCHANT_ENCORE_WRAPPER_SLOT

    @property
    def slot_index(self) -> int:
        return self.wrapper_slot_index

    @property
    def trigger_id(self) -> str:
        return self.trigger.trigger_id

    @property
    def child_effect_ids(self) -> tuple[str, ...]:
        return tuple(child.effect_id for child in self.children)

    @property
    def value1(self) -> int:
        return self.wrapper.value1

    @property
    def value2(self) -> int:
        return self.wrapper.value2

    @property
    def count(self) -> int:
        return self.wrapper.effect_count

    @property
    def turn(self) -> int:
        return self.wrapper.effect_turn

    @property
    def total_limit(self) -> int:
        return self.wrapper.total_limit

    @property
    def per_turn_limit(self) -> int:
        return self.wrapper.per_turn_limit

    @property
    def lifetime(self) -> str:
        return self.wrapper.lifetime

    @property
    def permanent(self) -> bool:
        return self.wrapper.permanent

    @property
    def base_move_position_type(self) -> str:
        return self.move_position_type

    @property
    def card_move_position_type(self) -> str:
        return self.move_position_type

    @property
    def target_effect_executable(self) -> bool:
        return True

    @property
    def executable(self) -> bool:
        return True

    @property
    def whole_card_executable(self) -> bool:
        return not self.companion_blockers

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return PLAY_ORIGINS

    @property
    def companion_blocker_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.family for item in self.companion_blockers))

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
            "move_position_type": self.move_position_type,
            "effect_group_ids": list(self.effect_group_ids),
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "wrapper_slot_index": self.wrapper_slot_index,
            "wrapper": self.wrapper.to_dict(),
            "status_enchant_id": self.status_enchant_id,
            "trigger": self.trigger.to_dict(),
            "child_effect_ids": list(self.child_effect_ids),
            "children": [child.to_dict() for child in self.children],
            "co_blockers": list(self.co_blockers),
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
            "target_effect_executable": self.target_effect_executable,
            "whole_card_executable": self.whole_card_executable,
            "supported_play_origins": list(self.supported_play_origins),
        }


Plan2NativeStatusEnchantEncoreProgram = Plan2StatusEnchantEncoreVersion
Plan2NativeStatusEnchantEncoreCardVersion = Plan2StatusEnchantEncoreVersion


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreNativeEvidence:
    android_version: str
    encore_constructor: str
    encore_execute: str
    try_add_trigger_status: str
    aggressive_interval_predicate: str
    increment_phase: str
    spend_count: str
    pc_encore_type_index: int

    def to_dict(self) -> dict[str, object]:
        return {
            "android_version": self.android_version,
            "encore_constructor": self.encore_constructor,
            "encore_execute": self.encore_execute,
            "try_add_trigger_status": self.try_add_trigger_status,
            "aggressive_interval_predicate": self.aggressive_interval_predicate,
            "increment_phase": self.increment_phase,
            "spend_count": self.spend_count,
            "pc_encore_type_index": self.pc_encore_type_index,
        }


def _native_evidence() -> Plan2NativeStatusEnchantEncoreNativeEvidence:
    # These values are the reviewed Android/PC native facts already pinned by
    # the standalone owner.  No binary probe or timing/hash operation belongs
    # in this bounded catalog adapter.
    from .plan2_status_enchant_encore import (
        ANDROID_ENCORE_CTOR,
        ANDROID_ENCORE_EXECUTE,
        ANDROID_INCREMENT_PHASE,
        ANDROID_IS_AGGRESSIVE_INTERVAL,
        ANDROID_SPEND_COUNT,
        ANDROID_TRY_ADD_TRIGGER_STATUS,
        PC_ENCORE_TYPE_INDEX,
        ANDROID_VERSION,
    )

    return Plan2NativeStatusEnchantEncoreNativeEvidence(
        ANDROID_VERSION,
        ANDROID_ENCORE_CTOR,
        ANDROID_ENCORE_EXECUTE,
        ANDROID_TRY_ADD_TRIGGER_STATUS,
        ANDROID_IS_AGGRESSIVE_INTERVAL,
        ANDROID_INCREMENT_PHASE,
        ANDROID_SPEND_COUNT,
        PC_ENCORE_TYPE_INDEX,
    )


NATIVE_EVIDENCE = _native_evidence()


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreCatalog:
    database: str
    programs: tuple[Plan2StatusEnchantEncoreVersion, ...]
    legacy_catalog: Plan2EncoreCatalog
    native: Plan2NativeStatusEnchantEncoreNativeEvidence

    def __post_init__(self) -> None:
        _nonempty(self.database, "catalog.database")
        programs = tuple(self.programs)
        if any(not isinstance(item, Plan2StatusEnchantEncoreVersion) for item in programs):
            raise TypeError("programs must contain Encore versions")
        refs = tuple(item.ref for item in programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeStatusEnchantEncoreContractError("catalog-version-order")
        if not isinstance(self.legacy_catalog, Plan2EncoreCatalog):
            raise TypeError("legacy_catalog must be Plan2EncoreCatalog")
        object.__setattr__(self, "programs", programs)

    @property
    def versions(self) -> tuple[Plan2StatusEnchantEncoreVersion, ...]:
        return self.programs

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.affected_refs

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def companion_blockers(self) -> tuple[Plan2NativeStatusEnchantEncoreBlocker, ...]:
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

    def get(self, card_id: str, upgrade: int) -> Plan2StatusEnchantEncoreVersion | None:
        return next((item for item in self.programs if item.ref == (card_id, upgrade)), None)

    def resolve(self, card_id: str, upgrade: int) -> Plan2StatusEnchantEncoreVersion:
        program = self.get(card_id, upgrade)
        if program is None:
            raise Plan2NativeStatusEnchantEncoreContractError(
                "version-outside-exact-bundle", f"{card_id}#{upgrade}"
            )
        return program

    version = resolve

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "adapter_id": STATUS_ENCHANT_ENCORE_ADAPTER_ID,
            "affected_version_count": self.affected_version_count,
            "executable_version_count": self.executable_version_count,
            "programs": [item.to_dict() for item in self.programs],
            "native": self.native.to_dict(),
        }


Plan2NativeCatalogStatusEnchantEncore = Plan2NativeStatusEnchantEncoreCatalog


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    catalog: Plan2NativeStatusEnchantEncoreCatalog
    blockers: tuple[Plan2NativeStatusEnchantEncoreBlocker, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION:
            raise Plan2NativeStatusEnchantEncoreContractError("schema-version")
        _nonempty(self.database, "compilation.database")
        refs = tuple(self.affected_refs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeStatusEnchantEncoreContractError("affected-ref-order")
        _plain_int(self.occurrence_count, "compilation.occurrence_count", minimum=0)
        if not isinstance(self.catalog, Plan2NativeStatusEnchantEncoreCatalog):
            raise TypeError("catalog must be Encore catalog")
        if any(not isinstance(item, Plan2NativeStatusEnchantEncoreBlocker) for item in self.blockers):
            raise TypeError("blockers must contain Encore blockers")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", tuple(self.blockers))

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.affected_refs

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.affected_refs if ref not in compiled)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def compiled_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def compiled_occurrence_count(self) -> int:
        return sum(1 for _ in self.catalog.programs)

    @property
    def failed_occurrence_count(self) -> int:
        return self.occurrence_count - self.compiled_occurrence_count

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.executable_refs

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def target_effect_executable(self) -> bool:
        return self.executable_version_count == self.affected_version_count and not self.failed_refs

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers and not self.failed_refs

    @property
    def companion_blockers(self) -> tuple[Plan2NativeStatusEnchantEncoreBlocker, ...]:
        return self.catalog.companion_blockers

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.companion_blocked_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.whole_card_executable_refs

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.blockers).items()))

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.companion_blockers).items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs_by_family: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.companion_blockers:
            refs_by_family.setdefault(blocker.family, set()).add(blocker.ref)
        return {family: len(refs) for family, refs in sorted(refs_by_family.items())}

    def blockers_for(self, ref: tuple[str, int]) -> tuple[Plan2NativeStatusEnchantEncoreBlocker, ...]:
        return tuple(item for item in self.blockers if item.ref == ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "database": self.database,
            "affected_version_count": self.affected_version_count,
            "occurrence_count": self.occurrence_count,
            "compiled_version_count": self.compiled_version_count,
            "compiled_occurrence_count": self.compiled_occurrence_count,
            "failed_version_count": self.failed_version_count,
            "failed_occurrence_count": self.failed_occurrence_count,
            "executable_version_count": self.executable_version_count,
            "target_effect_executable": self.target_effect_executable,
            "fully_compiled": self.fully_compiled,
            "affected_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "compiled_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.compiled_refs
            ],
            "failed_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.failed_refs
            ],
            "blockers": [item.to_dict() for item in self.blockers],
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
            "companion_blocked_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "whole_card_executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "blocker_code_counts": self.blocker_code_counts,
            "companion_blocker_code_counts": self.companion_blocker_code_counts,
            "companion_family_version_counts": self.companion_family_version_counts,
        }


Plan2NativeStatusEnchantEncoreCatalogCompilation = Plan2NativeStatusEnchantEncoreCompilation


def _companion_blockers(version) -> tuple[Plan2NativeStatusEnchantEncoreBlocker, ...]:
    return tuple(
        Plan2NativeStatusEnchantEncoreBlocker(
            version.card_id,
            version.upgrade,
            STATUS_ENCHANT_ENCORE_WRAPPER_SLOT,
            "companion-blocked",
            family,
            "shared companion effect/trigger owner remains outside this slice",
            version.encore_effect_id,
            True,
        )
        for family in version.co_blockers
    )


def _load_contract(
    version,
    database: Path,
) -> tuple[object, RuntimeStatusEnchant]:
    target_self = load_produce_card_search(TARGET_SELF_SEARCH_ID, database)
    activation = (
        load_produce_card_search(version.trigger_id and "p_card_search-target-p_card-02-ido-3_211", database)
        if version.card_id == CARD_198
        else None
    )
    effect = _load_legacy_effect(version.encore_effect_id, database)
    rule = _load_legacy_status(version.status_enchant_id, database)
    # The legacy contract validator is the exact row/trigger/search contract;
    # using it here keeps this catalog from creating a parallel interpretation.
    from .plan2_status_enchant_encore import Plan2EncoreContract

    contract = Plan2EncoreContract(
        version.card_id, effect, rule, target_self, activation
    )
    errors = _legacy_contract_shape_errors(contract)
    if errors:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "encore-contract-shape", errors[0]
        )
    runtime_status = load_runtime_status_enchant(version.status_enchant_id, database)
    if (
        runtime_status.id != version.status_enchant_id
        or runtime_status.trigger.id != version.trigger_id
        or tuple(item.id for item in runtime_status.effects) != version.child_effect_ids
    ):
        raise Plan2NativeStatusEnchantEncoreContractError(
            "status-runtime-row-drift", version.status_enchant_id
        )
    return contract, runtime_status


def _validate_exact_version(
    version,
    database: Path,
) -> tuple[object, RuntimeStatusEnchant]:
    card = _load_legacy_card(version.card_id, version.upgrade, database)
    if (
        card.plan_type,
        card.category,
        card.stamina_cost,
        card.force_stamina_cost,
        card.cost_type,
        card.cost_value,
        card.play_trigger,
        card.move_position_type,
        card.effect_group_ids,
        tuple(effect.id for effect in card.effects),
    ) != (
        PLAN_TYPE,
        CATEGORY,
        version.stamina,
        0,
        "ExamCostType_Unknown",
        0,
        None,
        MOVE_LOST,
        version.effect_group_ids,
        version.ordered_effect_ids,
    ):
        raise Plan2NativeStatusEnchantEncoreContractError(
            "card-row-shape", version.version_ref
        )

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT play_effects_json FROM card WHERE id = ? AND upgrade_count = ?",
            (version.card_id, version.upgrade),
        ).fetchone()
        if row is None:
            raise Plan2NativeStatusEnchantEncoreContractError(
                "card-row-missing", version.version_ref
            )
        try:
            raw_slots = json.loads(row["play_effects_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise Plan2NativeStatusEnchantEncoreContractError(
                "card-effect-json", version.version_ref
            ) from error
        if not isinstance(raw_slots, list):
            raise Plan2NativeStatusEnchantEncoreContractError(
                "card-effect-json-array", version.version_ref
            )
        actual_slots = tuple(
            ExactCardEffectSlot(
                index,
                item["produceExamEffectId"],
                item["produceExamTriggerId"],
                item["hideIcon"],
                item["isOncePlayEffect"],
            )
            for index, item in enumerate(raw_slots)
            if isinstance(item, dict)
            and set(item)
            == {
                "produceExamTriggerId",
                "produceExamEffectId",
                "hideIcon",
                "isOncePlayEffect",
            }
        )
        if len(actual_slots) != len(raw_slots) or actual_slots != version.effect_slots:
            raise Plan2NativeStatusEnchantEncoreContractError(
                "card-effect-slot-shape", version.version_ref
            )
        for effect_id in version.ordered_effect_ids:
            exact = _LEGACY_EFFECT_ROWS.get(effect_id)
            if exact is None:
                raise Plan2NativeStatusEnchantEncoreContractError(
                    "effect-row-not-pinned", effect_id
                )
            try:
                _legacy_validate_effect_row(connection, exact)
            except (Plan2EncoreError, sqlite3.Error, TypeError, ValueError) as error:
                raise Plan2NativeStatusEnchantEncoreContractError(
                    getattr(error, "code", "effect-row-shape"),
                    getattr(error, "detail", str(error)) or str(error),
                ) from error

    target_self = load_produce_card_search(TARGET_SELF_SEARCH_ID, database)
    if _legacy_search_projection(target_self) != _legacy_expected_search(
        TARGET_SELF_SEARCH_ID, is_self=True
    ):
        raise Plan2NativeStatusEnchantEncoreContractError(
            "target-self-search-shape", TARGET_SELF_SEARCH_ID
        )
    if version.card_id == CARD_198:
        activation = load_produce_card_search(
            "p_card_search-target-p_card-02-ido-3_211", database
        )
        if _legacy_search_projection(activation) != _legacy_expected_search(
            "p_card_search-target-p_card-02-ido-3_211",
            card_ids=(TARGET_CARD_211,),
        ):
            raise Plan2NativeStatusEnchantEncoreContractError(
                "activation-search-shape", activation.id
            )
        deck = load_produce_card_search(DECK_211_SEARCH_ID, database)
        if _legacy_search_projection(deck) != _legacy_expected_search(
            DECK_211_SEARCH_ID,
            card_ids=(TARGET_CARD_211,),
            position="ProduceCardPositionType_DeckAll",
        ):
            raise Plan2NativeStatusEnchantEncoreContractError(
                "card-move-search-shape", DECK_211_SEARCH_ID
            )
    return _load_contract(version, database)


def _build_program(
    version,
    runtime_status: RuntimeStatusEnchant,
    database: Path,
) -> Plan2StatusEnchantEncoreVersion:
    wrapper = Plan2StatusEnchantEncoreWrapperRow.from_exact(
        _LEGACY_EFFECT_ROWS[version.encore_effect_id]
    )
    trigger = Plan2StatusEnchantEncoreTriggerRow.from_runtime(runtime_status.trigger)
    target_self = load_produce_card_search(TARGET_SELF_SEARCH_ID, database)
    children = tuple(
        Plan2StatusEnchantEncoreChildRow.from_runtime(
            effect,
            _LEGACY_EFFECT_ROWS.get(effect.id)
            or ExactEffectRow(
                effect.id,
                effect.effect_type,
                effect.value1,
                effect.value2,
                effect.effect_count,
                effect.effect_turn,
                status_enchant_id=effect.status_enchant_id,
                chain_effect_id=effect.chain_effect_id,
            ),
            target_self if effect.id == FORCE_PLAY_EFFECT_ID else None,
        )
        for effect in runtime_status.effects
    )
    return Plan2StatusEnchantEncoreVersion(
        version.card_id,
        version.upgrade,
        PLAN_TYPE,
        CATEGORY,
        version.stamina,
        0,
        "ExamCostType_Unknown",
        0,
        MOVE_LOST,
        version.effect_group_ids,
        version.effect_slots,
        wrapper,
        version.status_enchant_id,
        trigger,
        children,
        runtime_status,
        target_self,
        version.co_blockers,
        _companion_blockers(version),
    )


def _discover_target_refs(database: Path) -> tuple[tuple[tuple[str, int], ...], int]:
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.upgrade_count, COUNT(*)
              FROM card AS c
              JOIN json_each(c.play_effects_json) AS link
              JOIN effect AS e
                ON e.id = json_extract(link.value, '$.produceExamEffectId')
             WHERE c.plan_type = ? AND e.effect_type = ?
             GROUP BY c.id, c.upgrade_count
             ORDER BY c.id, c.upgrade_count
            """,
            (PLAN_TYPE, ENCORE_EFFECT_TYPE),
        ).fetchall()
    refs = tuple((str(row[0]), int(row[1])) for row in rows)
    occurrences = sum(int(row[2]) for row in rows)
    return refs, occurrences


def compile_plan2_native_catalog_status_enchant_encore(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusEnchantEncoreCompilation:
    """Compile exactly the eight current-Master Encore wrapper versions."""

    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    discovered_refs, discovered_occurrences = _discover_target_refs(database)
    expected_refs = tuple(sorted((item.card_id, item.upgrade) for item in _LEGACY_VERSIONS))
    if discovered_refs != expected_refs:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "target-inventory-drift", f"expected={expected_refs!r},got={discovered_refs!r}"
        )
    if discovered_occurrences != STATUS_ENCHANT_ENCORE_TARGET_OCCURRENCE_COUNT:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "target-occurrence-drift", str(discovered_occurrences)
        )

    programs: list[Plan2StatusEnchantEncoreVersion] = []
    blockers: list[Plan2NativeStatusEnchantEncoreBlocker] = []
    contracts: list[object] = []
    for expected in _LEGACY_VERSIONS:
        try:
            contract, runtime_status = _validate_exact_version(expected, database)
            contracts.append(contract)
            programs.append(_build_program(expected, runtime_status, database))
        except (KeyError, OSError, sqlite3.Error, TypeError, ValueError) as error:
            blockers.append(
                Plan2NativeStatusEnchantEncoreBlocker(
                    expected.card_id,
                    expected.upgrade,
                    STATUS_ENCHANT_ENCORE_WRAPPER_SLOT,
                    getattr(error, "code", "encore-compile-failed"),
                    "status_enchant_encore",
                    getattr(error, "detail", str(error)) or str(error),
                    expected.encore_effect_id,
                    False,
                )
            )

    if len(contracts) == STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT:
        legacy_catalog = Plan2EncoreCatalog(_LEGACY_VERSIONS, tuple(contracts))
    else:
        # The catalog is only used by the facade when fully compiled.  Keeping
        # a typed legacy shell here makes the partial compilation inspectable
        # without manufacturing a runtime for a failed row.
        legacy_catalog = Plan2EncoreCatalog(_LEGACY_VERSIONS, tuple(contracts))
    catalog = Plan2NativeStatusEnchantEncoreCatalog(
        str(database), tuple(programs), legacy_catalog, NATIVE_EVIDENCE
    )
    return Plan2NativeStatusEnchantEncoreCompilation(
        PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION,
        str(database),
        expected_refs,
        discovered_occurrences,
        catalog,
        tuple(blockers),
    )


compile_plan2_status_enchant_encore_catalog = (
    compile_plan2_native_catalog_status_enchant_encore
)
compile_status_enchant_encore_catalog = compile_plan2_native_catalog_status_enchant_encore


def load_plan2_native_catalog_status_enchant_encore(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusEnchantEncoreCatalog:
    """Load a complete exact catalog, failing closed on any hard blocker."""

    compilation = compile_plan2_native_catalog_status_enchant_encore(database)
    if compilation.blockers:
        first = compilation.blockers[0]
        raise Plan2NativeStatusEnchantEncoreContractError(
            first.code, f"{first.card_id}#{first.upgrade}:{first.detail}"
        )
    if compilation.affected_version_count != STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "unexpected-target-version-count", str(compilation.affected_version_count)
        )
    return compilation.catalog


load_plan2_status_enchant_encore_catalog_native = load_plan2_native_catalog_status_enchant_encore
load_status_enchant_encore_catalog = load_plan2_native_catalog_status_enchant_encore


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantEncoreHandoffRow:
    """Typed row accepted by a central catalog without executing its child."""

    version_ref: str
    card_id: str
    upgrade: int
    wrapper_effect_id: str
    wrapper_slot_index: int
    wrapper_value1: int
    wrapper_value2: int
    wrapper_count: int
    wrapper_turn: int
    lifetime: str
    total_limit: int
    per_turn_limit: int
    status_enchant_id: str
    trigger_id: str
    trigger_phase: str
    trigger_phase_values: tuple[int, ...]
    child_effect_ids: tuple[str, ...]
    ordered_effect_ids: tuple[str, ...]
    move_position_type: str
    supported_play_origins: tuple[str, ...]
    target_effect_executable: bool
    whole_card_executable: bool
    companion_blockers: tuple[Plan2NativeStatusEnchantEncoreBlocker, ...]
    child_rows: tuple[Plan2StatusEnchantEncoreChildRow, ...]

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    def to_dict(self) -> dict[str, object]:
        return {
            "version_ref": self.version_ref,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "wrapper_effect_id": self.wrapper_effect_id,
            "wrapper_slot_index": self.wrapper_slot_index,
            "wrapper_value1": self.wrapper_value1,
            "wrapper_value2": self.wrapper_value2,
            "wrapper_count": self.wrapper_count,
            "wrapper_turn": self.wrapper_turn,
            "lifetime": self.lifetime,
            "total_limit": self.total_limit,
            "per_turn_limit": self.per_turn_limit,
            "status_enchant_id": self.status_enchant_id,
            "trigger_id": self.trigger_id,
            "trigger_phase": self.trigger_phase,
            "trigger_phase_values": list(self.trigger_phase_values),
            "child_effect_ids": list(self.child_effect_ids),
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "move_position_type": self.move_position_type,
            "supported_play_origins": list(self.supported_play_origins),
            "target_effect_executable": self.target_effect_executable,
            "whole_card_executable": self.whole_card_executable,
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
            "child_rows": [item.to_dict() for item in self.child_rows],
        }


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantEncoreCentralHandoff:
    adapter_id: str
    schema_version: int
    affected_refs: tuple[tuple[str, int], ...]
    executable_refs: tuple[tuple[str, int], ...]
    rows: tuple[Plan2StatusEnchantEncoreHandoffRow, ...]

    def __post_init__(self) -> None:
        if self.adapter_id != STATUS_ENCHANT_ENCORE_ADAPTER_ID:
            raise Plan2NativeStatusEnchantEncoreContractError("handoff-adapter-id")
        if self.schema_version != PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION:
            raise Plan2NativeStatusEnchantEncoreContractError("handoff-schema-version")
        if self.affected_refs != tuple(sorted(self.affected_refs)):
            raise Plan2NativeStatusEnchantEncoreContractError("handoff-ref-order")
        if self.executable_refs != tuple(sorted(self.executable_refs)):
            raise Plan2NativeStatusEnchantEncoreContractError("handoff-executable-order")
        if not set(self.executable_refs).issubset(self.affected_refs):
            raise Plan2NativeStatusEnchantEncoreContractError("handoff-executable-subset")
        if tuple(row.ref for row in self.rows) != self.affected_refs:
            raise Plan2NativeStatusEnchantEncoreContractError("handoff-row-order")

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.companion_blockers)

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.whole_card_executable)

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "schema_version": self.schema_version,
            "affected_version_count": self.affected_version_count,
            "executable_version_count": self.executable_version_count,
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
            "rows": [row.to_dict() for row in self.rows],
        }


def build_plan2_status_enchant_encore_central_handoff(
    source: Plan2NativeStatusEnchantEncoreCompilation | Plan2NativeStatusEnchantEncoreCatalog,
) -> Plan2StatusEnchantEncoreCentralHandoff:
    if isinstance(source, Plan2NativeStatusEnchantEncoreCompilation):
        if source.blockers:
            first = source.blockers[0]
            raise Plan2NativeStatusEnchantEncoreContractError(
                first.code, f"{first.card_id}#{first.upgrade}:{first.detail}"
            )
        catalog = source.catalog
    elif isinstance(source, Plan2NativeStatusEnchantEncoreCatalog):
        catalog = source
    else:
        raise TypeError("source must be Encore catalog or compilation")
    rows = tuple(
        Plan2StatusEnchantEncoreHandoffRow(
            program.version_ref,
            program.card_id,
            program.upgrade,
            program.wrapper_effect_id,
            program.wrapper_slot_index,
            program.value1,
            program.value2,
            program.count,
            program.turn,
            program.lifetime,
            program.total_limit,
            program.per_turn_limit,
            program.status_enchant_id,
            program.trigger_id,
            program.trigger.phase,
            program.trigger.phase_values,
            program.child_effect_ids,
            program.ordered_effect_ids,
            program.move_position_type,
            program.supported_play_origins,
            program.target_effect_executable,
            program.whole_card_executable,
            program.companion_blockers,
            program.children,
        )
        for program in catalog.programs
    )
    return Plan2StatusEnchantEncoreCentralHandoff(
        STATUS_ENCHANT_ENCORE_ADAPTER_ID,
        PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION,
        catalog.affected_refs,
        catalog.executable_refs,
        rows,
    )


build_plan2_native_status_enchant_encore_handoff = build_plan2_status_enchant_encore_central_handoff
build_status_enchant_encore_central_handoff = build_plan2_status_enchant_encore_central_handoff


def exact_plan2_native_status_enchant_encore_rows(
    source: Plan2NativeStatusEnchantEncoreCompilation | Plan2NativeStatusEnchantEncoreCatalog,
) -> tuple[dict[str, object], ...]:
    return tuple(row.to_dict() for row in build_plan2_status_enchant_encore_central_handoff(source).rows)


def status_enchant_encore_catalog_summary(
    source: Plan2NativeStatusEnchantEncoreCompilation | Plan2NativeStatusEnchantEncoreCatalog,
) -> dict[str, object]:
    compilation = source if isinstance(source, Plan2NativeStatusEnchantEncoreCompilation) else None
    handoff = build_plan2_status_enchant_encore_central_handoff(source)
    return {
        "affected_versions": handoff.affected_version_count,
        "compiled_versions": compilation.compiled_version_count if compilation else handoff.executable_version_count,
        "failed_versions": compilation.failed_version_count if compilation else 0,
        "executable_versions": handoff.executable_version_count,
        "target_effect_executable_versions": handoff.executable_version_count,
        "companion_blocked_versions": len(handoff.companion_blocked_refs),
        "whole_card_executable_versions": len(handoff.whole_card_executable_refs),
        "affected_refs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.affected_refs],
        "executable_refs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.executable_refs],
        "companion_blocked_refs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.companion_blocked_refs],
        "whole_card_executable_refs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.whole_card_executable_refs],
        "companion_blocker_code_counts": (
            compilation.companion_blocker_code_counts
            if compilation else dict(sorted(Counter(blocker.code for row in handoff.rows for blocker in row.companion_blockers).items()))
        ),
        "companion_family_version_counts": compilation.companion_family_version_counts if compilation else {},
    }


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreCapture:
    card: Plan3NativeCard
    program: Plan2StatusEnchantEncoreVersion
    source_guid: str
    play_origin: str
    source_zone: str = "playing"
    source_index: int = -1

    @property
    def captured_guid(self) -> str:
        return self.source_guid

    @property
    def card_id(self) -> str:
        return self.card.card_id

    @property
    def upgrade(self) -> int:
        return self.card.effective_upgrade

    @property
    def wrapper_effect_id(self) -> str:
        return self.program.wrapper_effect_id

    @property
    def lifetime(self) -> str:
        return self.program.lifetime


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreCaptureResult:
    before: "Plan2NativeStatusEnchantEncoreRuntime"
    after: "Plan2NativeStatusEnchantEncoreRuntime"
    resolved: bool
    capture: Plan2NativeStatusEnchantEncoreCapture | None = None
    reason: str = ""

    @property
    def executable(self) -> bool:
        return self.resolved

    @property
    def captured_guid(self) -> str | None:
        return self.capture.captured_guid if self.capture else None


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreListener:
    downstream: Plan2EncoreListener
    program: Plan2StatusEnchantEncoreVersion
    capture: Plan2NativeStatusEnchantEncoreCapture

    @property
    def status_uid(self) -> int:
        return self.downstream.uid

    @property
    def uid(self) -> int:
        return self.downstream.uid

    @property
    def native_uid(self) -> int:
        return self.downstream.active.native_uid

    @property
    def captured_guid(self) -> str:
        return self.downstream.captured_guid

    @property
    def source_guid(self) -> str:
        return self.downstream.captured_guid

    @property
    def source_card_id(self) -> str:
        return self.downstream.captured_card.card_id

    @property
    def source_upgrade(self) -> int:
        return self.downstream.captured_card.effective_upgrade

    @property
    def play_origin(self) -> str:
        return "ordinary" if self.downstream.install_origin == "normal" else self.downstream.install_origin

    @property
    def active(self):
        return self.downstream.active

    @property
    def uses(self) -> int:
        return self.downstream.active.uses

    @property
    def uses_this_turn(self) -> int:
        return self.downstream.active.uses_this_turn

    @property
    def total_remaining(self) -> int:
        return self.downstream.total_remaining

    @property
    def per_turn_remaining(self) -> int:
        return self.downstream.per_turn_remaining

    @property
    def turns_remaining(self) -> int:
        return self.downstream.active.remaining_turns

    @property
    def lifetime(self) -> str:
        return self.program.lifetime

    @property
    def permanent(self) -> bool:
        return self.program.permanent

    @property
    def expires(self) -> bool:
        return not self.permanent

    @property
    def expired(self) -> bool:
        return self.turns_remaining == 0

    def phase_count(self, phase: str) -> int:
        return self.downstream.active.phase_count(phase)


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreRuntime:
    catalog: Plan2NativeStatusEnchantEncoreCatalog | None = None
    downstream: Plan2EncoreRuntime | None = None
    database: str = str(DEFAULT_DATABASE)
    next_status_uid: int = 1
    total_uses_by_guid: tuple[tuple[str, int], ...] = ()
    turn_uses_by_guid: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.catalog is None:
            resolved = load_plan2_native_catalog_status_enchant_encore(Path(self.database))
            object.__setattr__(self, "catalog", resolved)
        if self.downstream is None:
            assert self.catalog is not None
            object.__setattr__(
                self,
                "downstream",
                new_plan2_encore_runtime(
                    Plan3NativeState(), catalog=self.catalog.legacy_catalog
                ),
            )
        if not isinstance(self.catalog, Plan2NativeStatusEnchantEncoreCatalog):
            raise TypeError("catalog must be Encore catalog")
        if not isinstance(self.downstream, Plan2EncoreRuntime):
            raise TypeError("downstream must be Plan2EncoreRuntime")
        if self.downstream.catalog is not self.catalog.legacy_catalog:
            raise Plan2NativeStatusEnchantEncoreContractError("runtime-catalog-drift")
        _nonempty(self.database, "runtime.database")
        _plain_int(self.next_status_uid, "runtime.next_status_uid", minimum=1)
        for name in ("total_uses_by_guid", "turn_uses_by_guid"):
            values = tuple(getattr(self, name))
            seen: set[str] = set()
            for guid, count in values:
                _nonempty(guid, f"runtime.{name}.guid")
                _plain_int(count, f"runtime.{name}.count", minimum=0)
                if guid in seen:
                    raise Plan2NativeStatusEnchantEncoreContractError(
                        "duplicate-guid-count", guid
                    )
                seen.add(guid)
            object.__setattr__(self, name, values)

    @property
    def native_state(self) -> Plan3NativeState:
        return self.downstream.native_state

    @property
    def listeners(self) -> tuple[Plan2NativeStatusEnchantEncoreListener, ...]:
        return tuple(
            Plan2NativeStatusEnchantEncoreListener(
                listener,
                self.catalog.resolve(listener.captured_card.card_id, listener.captured_card.effective_upgrade),
                Plan2NativeStatusEnchantEncoreCapture(
                    listener.captured_card,
                    self.catalog.resolve(listener.captured_card.card_id, listener.captured_card.effective_upgrade),
                    listener.captured_guid,
                    "ordinary" if listener.install_origin == "normal" else listener.install_origin,
                ),
            )
            for listener in self.downstream.listeners
        )

    @property
    def pending_handoffs(self) -> tuple[Plan2EncoreReplayCommand, ...]:
        return self.downstream.pending_handoffs

    @property
    def committed_transaction_ids(self) -> tuple[str, ...]:
        return self.downstream.committed_transaction_ids

    @property
    def history(self):
        return self.downstream.history

    @property
    def global_play_count(self) -> int:
        return self.downstream.global_play_count

    @property
    def turn_play_count(self) -> int:
        return self.downstream.turn_play_count

    @property
    def after(self) -> "Plan2NativeStatusEnchantEncoreRuntime":
        return self

    @property
    def expired_status_uids(self) -> tuple[int, ...]:
        return ()

    def card_count(self, guid: str) -> int:
        return self.downstream.card_count(guid)

    def guid_total_count(self, guid: str) -> int:
        return dict(self.total_uses_by_guid).get(guid, 0)

    def guid_turn_count(self, guid: str) -> int:
        return dict(self.turn_uses_by_guid).get(guid, 0)

    def uses_for_guid(self, guid: str) -> tuple[int, int]:
        return self.guid_total_count(guid), self.guid_turn_count(guid)

    def listener_for_uid(self, uid: int) -> Plan2NativeStatusEnchantEncoreListener | None:
        return next((item for item in self.listeners if item.uid == uid), None)

    def listener_for_guid(self, guid: str) -> Plan2NativeStatusEnchantEncoreListener | None:
        return next((item for item in self.listeners if item.captured_guid == guid), None)


def new_plan2_native_status_enchant_encore_runtime(
    native_state: Plan3NativeState,
    *,
    catalog: Plan2NativeStatusEnchantEncoreCatalog | Plan2NativeStatusEnchantEncoreCompilation | None = None,
    database: Path = DEFAULT_DATABASE,
    global_play_count: int = 0,
    turn_play_count: int = 0,
) -> Plan2NativeStatusEnchantEncoreRuntime:
    if not isinstance(native_state, Plan3NativeState):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-native-state")
    if catalog is None:
        resolved = load_plan2_native_catalog_status_enchant_encore(database)
    elif isinstance(catalog, Plan2NativeStatusEnchantEncoreCompilation):
        if not catalog.fully_compiled:
            raise Plan2NativeStatusEnchantEncoreContractError("catalog-incomplete")
        resolved = catalog.catalog
    elif isinstance(catalog, Plan2NativeStatusEnchantEncoreCatalog):
        resolved = catalog
    else:
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-catalog")
    downstream = new_plan2_encore_runtime(
        native_state,
        catalog=resolved.legacy_catalog,
        global_play_count=global_play_count,
        turn_play_count=turn_play_count,
    )
    return Plan2NativeStatusEnchantEncoreRuntime(
        resolved, downstream, str(Path(database).resolve())
    )


new_status_enchant_encore_runtime = new_plan2_native_status_enchant_encore_runtime


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreInstallInput:
    playing_card: Plan3NativeCard
    program: Plan2StatusEnchantEncoreVersion | None = None
    source_guid: str = ""
    status_uid: int | None = None
    play_origin: str = "ordinary"
    accepted_play: bool = True
    completed_effect_ids: tuple[str, ...] = ()
    current_effect_slot: int = STATUS_ENCHANT_ENCORE_WRAPPER_SLOT
    status_enchant_blocked: bool = False


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreInstallResult:
    before: Plan2NativeStatusEnchantEncoreRuntime
    after: Plan2NativeStatusEnchantEncoreRuntime
    program: Plan2StatusEnchantEncoreVersion | None
    resolved: bool
    installed: bool
    skipped_once: bool
    reason: str
    capture: Plan2NativeStatusEnchantEncoreCapture | None
    listener: Plan2NativeStatusEnchantEncoreListener | None
    downstream: object | None = None
    differences: tuple[object, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved


def _install_failure(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    program: Plan2StatusEnchantEncoreVersion | None,
    reason: str,
    *,
    capture: Plan2NativeStatusEnchantEncoreCapture | None = None,
    downstream: object | None = None,
) -> Plan2NativeStatusEnchantEncoreInstallResult:
    return Plan2NativeStatusEnchantEncoreInstallResult(
        runtime,
        runtime,
        program,
        False,
        False,
        False,
        reason,
        capture,
        None,
        downstream,
        (),
    )


def _assert_program_exact(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    program: Plan2StatusEnchantEncoreVersion,
) -> None:
    legacy = runtime.catalog.legacy_catalog.version(program.card_id, program.upgrade)
    if (
        program.ordered_effect_ids != legacy.ordered_effect_ids
        or program.wrapper.effect_id != legacy.encore_effect_id
        or program.wrapper.effect_type != ENCORE_EFFECT_TYPE
        or program.wrapper.value1 != 1
        or program.wrapper.value2 != 0
        or program.wrapper.effect_count != 2
        or program.wrapper.effect_turn != -1
        or program.status_enchant_id != legacy.status_enchant_id
        or program.trigger_id != legacy.trigger_id
        or program.child_effect_ids != legacy.child_effect_ids
        or program.move_position_type != MOVE_LOST
        or program.wrapper_slot_index != STATUS_ENCHANT_ENCORE_WRAPPER_SLOT
    ):
        raise Plan2NativeStatusEnchantEncoreContractError(
            "unknown-program-shape", program.version_ref
        )


def capture_plan2_native_status_enchant_encore(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    playing_card: Plan3NativeCard,
    *,
    source_guid: str = "",
    play_origin: str = "ordinary",
    source_zone: str = "playing",
    source_index: int = -1,
) -> Plan2NativeStatusEnchantEncoreCaptureResult:
    if not isinstance(runtime, Plan2NativeStatusEnchantEncoreRuntime):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-runtime")
    if not isinstance(playing_card, Plan3NativeCard):
        return Plan2NativeStatusEnchantEncoreCaptureResult(runtime, runtime, False, reason="playing-card-unavailable")
    origin = _origin(play_origin)
    if origin is None:
        return Plan2NativeStatusEnchantEncoreCaptureResult(runtime, runtime, False, reason="unknown-play-origin")
    if source_guid and source_guid != playing_card.guid:
        return Plan2NativeStatusEnchantEncoreCaptureResult(runtime, runtime, False, reason="source-guid-mismatch")
    try:
        program = runtime.catalog.resolve(playing_card.card_id, playing_card.effective_upgrade)
        _assert_program_exact(runtime, program)
    except Plan2NativeStatusEnchantEncoreContractError as error:
        return Plan2NativeStatusEnchantEncoreCaptureResult(runtime, runtime, False, reason=error.code)
    if type(source_zone) is not str or not source_zone:
        return Plan2NativeStatusEnchantEncoreCaptureResult(runtime, runtime, False, reason="source-zone-shape")
    if isinstance(source_index, bool) or type(source_index) is not int or source_index < -1:
        return Plan2NativeStatusEnchantEncoreCaptureResult(runtime, runtime, False, reason="source-index-shape")
    capture = Plan2NativeStatusEnchantEncoreCapture(
        playing_card,
        program,
        playing_card.guid,
        origin[0],
        source_zone,
        source_index,
    )
    return Plan2NativeStatusEnchantEncoreCaptureResult(runtime, runtime, True, capture)


capture_status_enchant_encore = capture_plan2_native_status_enchant_encore


def _resolve_install_input(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    candidate: object,
    *,
    playing_card: Plan3NativeCard | None,
    source_guid: str,
    status_uid: int | None,
    play_origin: str,
    accepted_play: bool,
    completed_effect_ids: Sequence[str],
    current_effect_slot: int,
    status_enchant_blocked: bool,
) -> Plan2NativeStatusEnchantEncoreInstallInput | None:
    if isinstance(candidate, Plan2NativeStatusEnchantEncoreInstallInput):
        return candidate
    card = playing_card
    program = candidate if isinstance(candidate, Plan2StatusEnchantEncoreVersion) else None
    if isinstance(candidate, Plan3NativeCard):
        card = candidate
    if card is None and program is not None:
        candidates = tuple(
            value
            for value in runtime.native_state.all_cards
            if value.card_id == program.card_id
            and value.effective_upgrade == program.upgrade
            and (not source_guid or value.guid == source_guid)
        )
        if len(candidates) == 1:
            card = candidates[0]
    if card is None:
        return None
    return Plan2NativeStatusEnchantEncoreInstallInput(
        card,
        program,
        source_guid,
        status_uid,
        play_origin,
        accepted_play,
        tuple(completed_effect_ids),
        current_effect_slot,
        status_enchant_blocked,
    )


def install_plan2_native_status_enchant_encore(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    program_or_input: object = None,
    *,
    playing_card: Plan3NativeCard | None = None,
    source_guid: str = "",
    status_uid: int | None = None,
    play_origin: str = "ordinary",
    accepted_play: bool = True,
    completed_effect_ids: Sequence[str] = (),
    current_effect_slot: int = STATUS_ENCHANT_ENCORE_WRAPPER_SLOT,
    status_enchant_blocked: bool = False,
) -> Plan2NativeStatusEnchantEncoreInstallResult:
    if not isinstance(runtime, Plan2NativeStatusEnchantEncoreRuntime):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-runtime")
    supplied = _resolve_install_input(
        runtime,
        program_or_input,
        playing_card=playing_card,
        source_guid=source_guid,
        status_uid=status_uid,
        play_origin=play_origin,
        accepted_play=accepted_play,
        completed_effect_ids=completed_effect_ids,
        current_effect_slot=current_effect_slot,
        status_enchant_blocked=status_enchant_blocked,
    )
    if supplied is None:
        return _install_failure(runtime, None, "install-input-unavailable")
    if type(supplied.accepted_play) is not bool or type(supplied.status_enchant_blocked) is not bool:
        return _install_failure(runtime, supplied.program, "install-boolean-shape")
    if not isinstance(supplied.playing_card, Plan3NativeCard):
        return _install_failure(runtime, supplied.program, "playing-card-unavailable")
    origin = _origin(supplied.play_origin)
    if origin is None:
        return _install_failure(runtime, supplied.program, "unknown-play-origin")
    try:
        program = runtime.catalog.resolve(
            supplied.playing_card.card_id, supplied.playing_card.effective_upgrade
        )
    except Plan2NativeStatusEnchantEncoreContractError as error:
        return _install_failure(runtime, supplied.program, error.code)
    if supplied.program is not None and supplied.program.ref != program.ref:
        return _install_failure(runtime, program, "program-card-ref-drift")
    capture_result = capture_plan2_native_status_enchant_encore(
        runtime,
        supplied.playing_card,
        source_guid=supplied.source_guid,
        play_origin=origin[0],
    )
    if not capture_result.resolved or capture_result.capture is None:
        return _install_failure(runtime, program, capture_result.reason)
    if not supplied.accepted_play:
        return _install_failure(runtime, program, "rejected-before-direct-effects", capture=capture_result.capture)
    uid = supplied.status_uid
    if uid is None:
        uid = runtime.next_status_uid
    if isinstance(uid, bool) or type(uid) is not int or uid <= 0 or uid > 2**31 - 1:
        return _install_failure(runtime, program, "invalid-status-uid", capture=capture_result.capture)
    downstream_input = Plan2EncoreInstallInput(
        supplied.playing_card,
        uid,
        play_origin=origin[1],
        completed_effect_ids=tuple(supplied.completed_effect_ids),
        current_effect_slot=supplied.current_effect_slot,
        status_enchant_blocked=supplied.status_enchant_blocked,
    )
    downstream_result = install_plan2_encore_listener(
        runtime.downstream, downstream_input
    )
    if not downstream_result.resolved:
        return Plan2NativeStatusEnchantEncoreInstallResult(
            runtime,
            runtime,
            program,
            False,
            False,
            False,
            downstream_result.reason,
            capture_result.capture,
            None,
            downstream_result,
            downstream_result.differences,
        )
    if not downstream_result.installed:
        return Plan2NativeStatusEnchantEncoreInstallResult(
            runtime,
            runtime,
            program,
            True,
            False,
            downstream_result.skipped_once,
            downstream_result.reason,
            capture_result.capture,
            None,
            downstream_result,
            downstream_result.differences,
        )
    next_uid = max(runtime.next_status_uid, uid + 1)
    after_runtime = Plan2NativeStatusEnchantEncoreRuntime(
        runtime.catalog,
        downstream_result.after,
        runtime.database,
        next_uid,
        runtime.total_uses_by_guid,
        runtime.turn_uses_by_guid,
    )
    listener = Plan2NativeStatusEnchantEncoreListener(
        downstream_result.listener,
        program,
        capture_result.capture,
    )
    return Plan2NativeStatusEnchantEncoreInstallResult(
        runtime,
        after_runtime,
        program,
        True,
        True,
        False,
        downstream_result.reason,
        capture_result.capture,
        listener,
        downstream_result,
        downstream_result.differences,
    )


install_status_enchant_encore = install_plan2_native_status_enchant_encore
install_plan2_status_enchant_encore = install_plan2_native_status_enchant_encore


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreEvent:
    event_id: str
    phase: str
    effect_type: str = ""
    target_card: Plan3NativeCard | None = None
    transient_playing_cards: tuple[Plan3NativeCard, ...] = ()
    replay_ancestry: tuple[tuple[int, str], ...] = ()
    positive_occurrence_count: int = 1
    max_cycle_depth: int = 32
    play_origin: str = "ordinary"
    accepted: bool = True

    def downstream(self) -> Plan2EncoreTriggerEvent:
        return Plan2EncoreTriggerEvent(
            self.event_id,
            self.phase,
            self.effect_type,
            self.target_card,
            self.transient_playing_cards,
            self.replay_ancestry,
            self.positive_occurrence_count,
            self.max_cycle_depth,
        )


Plan2StatusEnchantEncoreEvent = Plan2NativeStatusEnchantEncoreEvent


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantEncoreHandoff:
    """One exact, non-executing downstream ForcePlay handoff."""

    handoff_id: str
    queue_order: int
    listener_uid: int
    captured_guid: str
    source_card_id: str
    source_upgrade: int
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    child: Plan2StatusEnchantEncoreChildRow
    ordered_effect_ids: tuple[str, ...]
    expected_core_effect_ids: tuple[str, ...]
    skipped_once_effect_ids: tuple[str, ...]
    ancestry: tuple[tuple[int, str], ...]
    force_play: object
    downstream: Plan2EncoreReplayCommand
    play_origin: str = "forced"

    @property
    def child_effect_id(self) -> str:
        return self.child.effect_id

    @property
    def command(self) -> Plan2EncoreReplayCommand:
        return self.downstream

    @property
    def downstream_command(self) -> Plan2EncoreReplayCommand:
        return self.downstream

    @property
    def ordinal(self) -> int:
        return self.queue_order

    @property
    def source_zone(self) -> str:
        return self.force_play.original_source_zone

    @property
    def source_index(self) -> int:
        return self.force_play.original_source_index

    @property
    def move_position_type(self) -> str:
        return self.force_play.base_move_position_type

    @property
    def target_position_type(self) -> str:
        return self.force_play.original_source_position_type

    @property
    def is_consume_cost(self) -> bool:
        return self.force_play.is_consume_cost

    @property
    def is_manual(self) -> bool:
        return self.force_play.is_manual

    @property
    def is_use_playable_count(self) -> bool:
        return self.force_play.is_use_playable_count

    @property
    def resolves_source_by_guid_at_execution(self) -> bool:
        return self.force_play.resolves_source_by_guid_at_execution

    @property
    def lifetime(self) -> str:
        return "permanent"

    def to_dict(self) -> dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "queue_order": self.queue_order,
            "listener_uid": self.listener_uid,
            "captured_guid": self.captured_guid,
            "source_card_id": self.source_card_id,
            "source_upgrade": self.source_upgrade,
            "wrapper_effect_id": self.wrapper_effect_id,
            "status_enchant_id": self.status_enchant_id,
            "trigger_id": self.trigger_id,
            "child_effect_id": self.child_effect_id,
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "expected_core_effect_ids": list(self.expected_core_effect_ids),
            "skipped_once_effect_ids": list(self.skipped_once_effect_ids),
            "ancestry": [list(item) for item in self.ancestry],
            "source_zone": self.source_zone,
            "source_index": self.source_index,
            "move_position_type": self.move_position_type,
            "target_position_type": self.target_position_type,
            "is_consume_cost": self.is_consume_cost,
            "is_manual": self.is_manual,
            "is_use_playable_count": self.is_use_playable_count,
        }


def _handoff_from_command(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    command: Plan2EncoreReplayCommand,
) -> Plan2StatusEnchantEncoreHandoff:
    card = command.force_play.card
    program = runtime.catalog.resolve(card.card_id, card.effective_upgrade)
    if len(program.children) != 1 or program.children[0].effect_id != FORCE_PLAY_EFFECT_ID:
        raise Plan2NativeStatusEnchantEncoreContractError(
            "child-handoff-shape", program.version_ref
        )
    return Plan2StatusEnchantEncoreHandoff(
        command.handoff_id,
        command.force_play.ordinal,
        command.listener_uid,
        command.captured_guid,
        card.card_id,
        card.effective_upgrade,
        command.encore_effect_id,
        program.status_enchant_id,
        program.trigger_id,
        program.children[0],
        command.current_play_effect_ids,
        command.expected_core_effect_ids,
        command.skipped_once_effect_ids,
        command.ancestry,
        command.force_play,
        command,
        "forced",
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncorePlanResult:
    before: Plan2NativeStatusEnchantEncoreRuntime
    after: Plan2NativeStatusEnchantEncoreRuntime
    event: Plan2NativeStatusEnchantEncoreEvent
    resolved: bool
    triggered: bool
    reason: str
    handoffs: tuple[Plan2StatusEnchantEncoreHandoff, ...]
    downstream: Plan2EncoreTriggerResult
    spent_listener_uids: tuple[int, ...]
    removed_listener_uids: tuple[int, ...]

    @property
    def executable(self) -> bool:
        return self.resolved

    @property
    def commands(self) -> tuple[Plan2StatusEnchantEncoreHandoff, ...]:
        return self.handoffs

    @property
    def differences(self):
        return self.downstream.differences

    @property
    def rng_consumed(self) -> bool:
        return False

    @property
    def core_hook_required(self) -> bool:
        return bool(self.handoffs)


def _update_use_counts(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    spent_uids: Sequence[int],
) -> tuple[tuple[str, int], tuple[tuple[str, int], ...]]:
    totals = dict(runtime.total_uses_by_guid)
    turns = dict(runtime.turn_uses_by_guid)
    by_uid = {listener.uid: listener for listener in runtime.downstream.listeners}
    for uid in spent_uids:
        listener = by_uid.get(uid)
        if listener is None:
            raise Plan2NativeStatusEnchantEncoreContractError(
                "spent-listener-not-found", str(uid)
            )
        guid = listener.captured_guid
        totals[guid] = totals.get(guid, 0) + 1
        turns[guid] = turns.get(guid, 0) + 1
    return tuple(sorted(totals.items())), tuple(sorted(turns.items()))


def plan_plan2_native_status_enchant_encore(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    event: Plan2NativeStatusEnchantEncoreEvent | Plan2EncoreTriggerEvent,
) -> Plan2NativeStatusEnchantEncorePlanResult:
    if not isinstance(runtime, Plan2NativeStatusEnchantEncoreRuntime):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-runtime")
    if isinstance(event, Plan2EncoreTriggerEvent):
        supplied = Plan2NativeStatusEnchantEncoreEvent(
            event.event_id,
            event.phase,
            event.effect_type,
            event.target_card,
            event.transient_playing_cards,
            event.replay_ancestry,
            event.positive_occurrence_count,
            event.max_cycle_depth,
        )
        downstream_event = event
    elif isinstance(event, Plan2NativeStatusEnchantEncoreEvent):
        supplied = event
        if not event.accepted:
            downstream = Plan2EncoreTriggerResult(
                runtime.downstream,
                runtime.downstream,
                event.downstream(),
                False,
                False,
                "event-rejected",
                (),
                (),
                (),
                (),
                (),
            )
            return Plan2NativeStatusEnchantEncorePlanResult(
                runtime, runtime, supplied, False, False, "event-rejected", (), downstream, (), ()
            )
        downstream_event = event.downstream()
    else:
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-event")
    downstream = plan_plan2_encore_trigger(
        runtime.downstream,
        downstream_event,
        database=Path(runtime.database),
    )
    if not downstream.resolved:
        return Plan2NativeStatusEnchantEncorePlanResult(
            runtime,
            runtime,
            supplied,
            False,
            False,
            downstream.reason,
            (),
            downstream,
            (),
            (),
        )
    try:
        handoffs = tuple(
            _handoff_from_command(runtime, command) for command in downstream.commands
        )
        total_counts, turn_counts = _update_use_counts(
            runtime, downstream.spent_listener_uids
        )
    except Plan2NativeStatusEnchantEncoreContractError:
        return Plan2NativeStatusEnchantEncorePlanResult(
            runtime,
            runtime,
            supplied,
            False,
            False,
            "typed-handoff-shape",
            (),
            downstream,
            (),
            (),
        )
    after = Plan2NativeStatusEnchantEncoreRuntime(
        runtime.catalog,
        downstream.after,
        runtime.database,
        runtime.next_status_uid,
        total_counts,
        turn_counts,
    )
    return Plan2NativeStatusEnchantEncorePlanResult(
        runtime,
        after,
        supplied,
        True,
        downstream.triggered,
        downstream.reason,
        handoffs,
        downstream,
        downstream.spent_listener_uids,
        downstream.removed_listener_uids,
    )


plan_status_enchant_encore = plan_plan2_native_status_enchant_encore
plan_plan2_status_enchant_encore = plan_plan2_native_status_enchant_encore


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreStage:
    before: Plan2NativeStatusEnchantEncoreRuntime
    after: Plan2NativeStatusEnchantEncoreRuntime
    handoff: Plan2StatusEnchantEncoreHandoff
    downstream: Plan2EncoreReplayStage
    live_card: Plan3NativeCard
    source_zone: str
    source_index: int
    random_state_before: int
    random_state_after: int

    @property
    def executable(self) -> bool:
        return True

    @property
    def rng_consumed(self) -> bool:
        return self.downstream.rng_consumed

    @property
    def captured_guid(self) -> str:
        return self.handoff.captured_guid


def stage_plan2_native_status_enchant_encore(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    handoff: Plan2StatusEnchantEncoreHandoff | str,
) -> Plan2NativeStatusEnchantEncoreStage:
    if not isinstance(runtime, Plan2NativeStatusEnchantEncoreRuntime):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-runtime")
    if isinstance(handoff, Plan2StatusEnchantEncoreHandoff):
        handoff_id = handoff.handoff_id
    elif type(handoff) is str and handoff:
        handoff_id = handoff
        downstream_command = next(
            (item for item in runtime.downstream.pending_handoffs if item.handoff_id == handoff_id),
            None,
        )
        if downstream_command is None:
            raise Plan2NativeStatusEnchantEncoreInputError("pending-handoff-not-found", handoff_id)
        handoff = _handoff_from_command(runtime, downstream_command)
    else:
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-handoff")
    downstream = stage_plan2_encore_handoff(runtime.downstream, handoff_id)
    return Plan2NativeStatusEnchantEncoreStage(
        runtime,
        runtime,
        handoff,
        downstream,
        downstream.live_card,
        downstream.source_zone,
        downstream.source_index,
        downstream.random_state_before,
        downstream.random_state_after,
    )


stage_status_enchant_encore = stage_plan2_native_status_enchant_encore
stage_plan2_status_enchant_encore = stage_plan2_native_status_enchant_encore


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncorePlayReceipt:
    transaction_id: str
    playing_card: Plan3NativeCard
    after_state: Plan3NativeState
    play_origin: str
    source_zone: str
    processed_effect_ids: tuple[str, ...]
    core_executed_effect_ids: tuple[str, ...]
    installed_once_effect_ids: tuple[str, ...]
    skipped_once_effect_ids: tuple[str, ...]
    is_consume_cost: bool
    is_use_playable_count: bool
    history_event_count: int = 1
    final_move_count: int = 1

    def downstream(self) -> Plan2EncorePlayReceipt:
        origin = _origin(self.play_origin)
        if origin is None:
            raise Plan2NativeStatusEnchantEncoreInputError("unknown-play-origin")
        return Plan2EncorePlayReceipt(
            self.transaction_id,
            self.playing_card,
            self.after_state,
            origin[1],
            self.source_zone,
            self.processed_effect_ids,
            self.core_executed_effect_ids,
            self.installed_once_effect_ids,
            self.skipped_once_effect_ids,
            self.is_consume_cost,
            self.is_use_playable_count,
            self.history_event_count,
            self.final_move_count,
        )


Plan2StatusEnchantEncoreReceipt = Plan2NativeStatusEnchantEncorePlayReceipt


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEncoreRecordResult:
    before: Plan2NativeStatusEnchantEncoreRuntime
    after: Plan2NativeStatusEnchantEncoreRuntime
    receipt: Plan2NativeStatusEnchantEncorePlayReceipt | Plan2EncorePlayReceipt

    @property
    def executable(self) -> bool:
        return True


def record_plan2_native_status_enchant_encore(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    receipt: Plan2NativeStatusEnchantEncorePlayReceipt | Plan2EncorePlayReceipt,
) -> Plan2NativeStatusEnchantEncoreRuntime:
    if not isinstance(runtime, Plan2NativeStatusEnchantEncoreRuntime):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-runtime")
    if isinstance(receipt, Plan2NativeStatusEnchantEncorePlayReceipt):
        downstream_receipt = receipt.downstream()
    elif isinstance(receipt, Plan2EncorePlayReceipt):
        downstream_receipt = receipt
    else:
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-play-receipt")
    after = record_plan2_encore_play_completion(runtime.downstream, downstream_receipt)
    return Plan2NativeStatusEnchantEncoreRuntime(
        runtime.catalog,
        after,
        runtime.database,
        runtime.next_status_uid,
        runtime.total_uses_by_guid,
        runtime.turn_uses_by_guid,
    )


record_status_enchant_encore = record_plan2_native_status_enchant_encore
record_play_completion = record_plan2_native_status_enchant_encore
record_plan2_status_enchant_encore = record_plan2_native_status_enchant_encore


def record_plan2_native_status_enchant_encore_transition(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    receipt: Plan2NativeStatusEnchantEncorePlayReceipt | Plan2EncorePlayReceipt,
) -> Plan2NativeStatusEnchantEncoreRecordResult:
    after = record_plan2_native_status_enchant_encore(runtime, receipt)
    return Plan2NativeStatusEnchantEncoreRecordResult(runtime, after, receipt)


def start_plan2_native_status_enchant_encore_turn(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
) -> Plan2NativeStatusEnchantEncoreRuntime:
    if not isinstance(runtime, Plan2NativeStatusEnchantEncoreRuntime):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-runtime")
    after = start_plan2_encore_turn(runtime.downstream)
    return Plan2NativeStatusEnchantEncoreRuntime(
        runtime.catalog,
        after,
        runtime.database,
        runtime.next_status_uid,
        runtime.total_uses_by_guid,
        tuple((guid, 0) for guid, _count in runtime.turn_uses_by_guid),
    )


advance_plan2_native_status_enchant_encore_turn = start_plan2_native_status_enchant_encore_turn
start_status_enchant_encore_turn = start_plan2_native_status_enchant_encore_turn


def remove_plan2_native_status_enchant_encore_listener(
    runtime: Plan2NativeStatusEnchantEncoreRuntime,
    uid: int,
    *,
    reason: str,
) -> Plan2NativeStatusEnchantEncoreRuntime:
    if not isinstance(runtime, Plan2NativeStatusEnchantEncoreRuntime):
        raise Plan2NativeStatusEnchantEncoreInputError("invalid-runtime")
    after = remove_plan2_encore_listener(runtime.downstream, uid, reason=reason)
    return Plan2NativeStatusEnchantEncoreRuntime(
        runtime.catalog,
        after,
        runtime.database,
        runtime.next_status_uid,
        runtime.total_uses_by_guid,
        runtime.turn_uses_by_guid,
    )


def catalog_to_dict(
    source: Plan2NativeStatusEnchantEncoreCompilation | Plan2NativeStatusEnchantEncoreCatalog | None = None,
) -> dict[str, object]:
    if source is None:
        source = compile_plan2_native_catalog_status_enchant_encore()
    if isinstance(source, Plan2NativeStatusEnchantEncoreCompilation):
        payload: dict[str, object] = {
            "effect_type": STATUS_ENCHANT_ENCORE_EFFECT_TYPE,
            "effect_type_value": ENCORE_EFFECT_TYPE_VALUE,
            "affected_versions": [item.to_dict() for item in source.catalog.programs],
            "native": source.catalog.native.to_dict(),
            "accounting": {
                "affected_versions": source.affected_version_count,
                "compiled_versions": source.compiled_version_count,
                "executable_versions": source.executable_version_count,
                "target_effect_executable_versions": source.executable_version_count,
                "companion_blocked_versions": len(source.companion_blocked_refs),
                "whole_card_executable_versions": len(source.whole_card_executable_refs),
            },
            "compilation": source.to_dict(),
        }
        if source.fully_compiled:
            payload["central_handoff"] = build_plan2_status_enchant_encore_central_handoff(
                source
            ).to_dict()
        return payload
    if isinstance(source, Plan2NativeStatusEnchantEncoreCatalog):
        return {
            "schema_version": PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION,
            "catalog": source.to_dict(),
            "accounting": status_enchant_encore_catalog_summary(source),
            "central_handoff": build_plan2_status_enchant_encore_central_handoff(source).to_dict(),
        }
    raise TypeError("source must be Encore catalog or compilation")


__all__ = [
    "ANDROID_VERSION",
    "ANDROID_ENCORE_CTOR",
    "ANDROID_ENCORE_EXECUTE",
    "ANDROID_TRY_ADD_TRIGGER_STATUS",
    "ANDROID_IS_AGGRESSIVE_INTERVAL",
    "ANDROID_INCREMENT_PHASE",
    "ANDROID_SPEND_COUNT",
    "PC_ENCORE_TYPE_INDEX",
    "ACCOUNTING",
    "EXPECTED_AFFECTED_VERSION_COUNT",
    "EXPECTED_INSTALLER_OCCURRENCE_COUNT",
    "EXPECTED_EXECUTABLE_VERSION_COUNT",
    "CARD_198",
    "CARD_201",
    "TARGET_CARD_211",
    "ENCORE_EFFECT_198",
    "ENCORE_EFFECT_201",
    "ENCORE_EFFECT_TYPE_VALUE",
    "FORCE_PLAY_EFFECT_ID",
    "AGGRESSIVE_EFFECT_TYPE",
    "PHASE_CARD_PLAY_AFTER",
    "PHASE_AGGRESSIVE_UP_INTERVAL",
    "TARGET_SELF_SEARCH_ID",
    "MOVE_LOST",
    "TARGET_POSITION",
    "PLAY_ORIGINS",
    "SUPPORTED_PLAY_ORIGINS",
    "STATUS_ENCHANT_ENCORE_ADAPTER_ID",
    "STATUS_ENCHANT_ENCORE_EFFECT_TYPE",
    "STATUS_ENCHANT_ENCORE_TARGET_VERSION_COUNT",
    "STATUS_ENCHANT_ENCORE_TARGET_OCCURRENCE_COUNT",
    "PLAN2_NATIVE_CATALOG_STATUS_ENCHANT_ENCORE_SCHEMA_VERSION",
    "STATUS_ENCHANT_ENCORE_CATALOG_SCHEMA_VERSION",
    "Plan2NativeStatusEnchantEncoreBlocker",
    "Plan2NativeStatusEnchantEncoreCatalog",
    "Plan2NativeCatalogStatusEnchantEncore",
    "Plan2NativeStatusEnchantEncoreCatalogCompilation",
    "Plan2NativeStatusEnchantEncoreCompilation",
    "Plan2NativeStatusEnchantEncoreNativeEvidence",
    "NATIVE_EVIDENCE",
    "Plan2StatusEnchantEncoreVersion",
    "Plan2NativeStatusEnchantEncoreProgram",
    "Plan2NativeStatusEnchantEncoreCardVersion",
    "Plan2StatusEnchantEncoreWrapperRow",
    "Plan2StatusEnchantEncoreTriggerRow",
    "Plan2StatusEnchantEncoreChildRow",
    "Plan2StatusEnchantEncoreHandoffRow",
    "Plan2StatusEnchantEncoreCentralHandoff",
    "Plan2StatusEnchantEncorePlayOrigin",
    "Plan2NativeStatusEnchantEncoreContractError",
    "Plan2NativeStatusEnchantEncoreInputError",
    "Plan2NativeStatusEnchantEncoreCapture",
    "Plan2NativeStatusEnchantEncoreCaptureResult",
    "Plan2NativeStatusEnchantEncoreListener",
    "Plan2NativeStatusEnchantEncoreRuntime",
    "Plan2NativeStatusEnchantEncoreInstallInput",
    "Plan2NativeStatusEnchantEncoreInstallResult",
    "Plan2NativeStatusEnchantEncoreEvent",
    "Plan2StatusEnchantEncoreEvent",
    "Plan2StatusEnchantEncoreHandoff",
    "Plan2NativeStatusEnchantEncorePlanResult",
    "Plan2NativeStatusEnchantEncoreStage",
    "Plan2NativeStatusEnchantEncorePlayReceipt",
    "Plan2StatusEnchantEncoreReceipt",
    "Plan2NativeStatusEnchantEncoreRecordResult",
    "compile_plan2_native_catalog_status_enchant_encore",
    "compile_plan2_status_enchant_encore_catalog",
    "compile_status_enchant_encore_catalog",
    "load_plan2_native_catalog_status_enchant_encore",
    "load_plan2_status_enchant_encore_catalog_native",
    "load_status_enchant_encore_catalog",
    "build_plan2_status_enchant_encore_central_handoff",
    "build_plan2_native_status_enchant_encore_handoff",
    "build_status_enchant_encore_central_handoff",
    "exact_plan2_native_status_enchant_encore_rows",
    "status_enchant_encore_catalog_summary",
    "new_plan2_native_status_enchant_encore_runtime",
    "new_status_enchant_encore_runtime",
    "capture_plan2_native_status_enchant_encore",
    "capture_status_enchant_encore",
    "install_plan2_native_status_enchant_encore",
    "install_status_enchant_encore",
    "install_plan2_status_enchant_encore",
    "plan_plan2_native_status_enchant_encore",
    "plan_status_enchant_encore",
    "plan_plan2_status_enchant_encore",
    "stage_plan2_native_status_enchant_encore",
    "stage_status_enchant_encore",
    "stage_plan2_status_enchant_encore",
    "record_plan2_native_status_enchant_encore",
    "record_status_enchant_encore",
    "record_play_completion",
    "record_plan2_status_enchant_encore",
    "record_plan2_native_status_enchant_encore_transition",
    "start_plan2_native_status_enchant_encore_turn",
    "advance_plan2_native_status_enchant_encore_turn",
    "start_status_enchant_encore_turn",
    "remove_plan2_native_status_enchant_encore_listener",
    "catalog_to_dict",
]
