"""Bounded exact Plan2 catalog adapter for ``ReviewMultiple``.

This module owns only the current-Master card versions which contain
``ProduceExamEffectType_ExamReviewMultiple``.  The scalar/status primitive and
the immutable lifecycle are deliberately delegated to :mod:`plan2_review_multiple`
and :mod:`plan2_state`; this file is the typed catalog boundary around those
primitives.

The catalog is an effect-family catalog, not a claim that every complete card
transaction is already owned by the central horizon.  A ReviewMultiple slot
can therefore be compiled for all 26 affected versions while exact companion
rows remain visible as per-version handoff blockers.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Final, Mapping, Sequence

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_review_multiple import (
    ANDROID_REVIEW_MULTIPLE_EVIDENCE,
    Plan2ReviewMultipleContractError,
    Plan2ReviewMultipleEffect,
    Plan2ReviewMultipleTransition,
    Plan2ReviewScoreTransition,
    Plan2ScoreApplicator,
    REVIEW_MULTIPLE_EFFECT_TYPE as _REVIEW_MULTIPLE_EFFECT_TYPE,
    load_plan2_review_multiple_effect,
    simulate_end_turn_review_score,
    simulate_review_multiple,
)
from .plan2_state import (
    PERMANENT_TURN,
    Plan2State,
    Plan2TurnStartTransition,
    simulate_plan2_turn_start,
)


PLAN2_NATIVE_CATALOG_REVIEW_MULTIPLE_SCHEMA_VERSION: Final = 1
REVIEW_MULTIPLE_CATALOG_SCHEMA_VERSION: Final = (
    PLAN2_NATIVE_CATALOG_REVIEW_MULTIPLE_SCHEMA_VERSION
)
REVIEW_MULTIPLE_TARGET_VERSION_COUNT: Final = 26
REVIEW_MULTIPLE_EFFECT_TYPE: Final = _REVIEW_MULTIPLE_EFFECT_TYPE
REVIEW_MULTIPLE_ADAPTER_ID: Final = "plan2.native.catalog.review_multiple"

_PLAN2 = "ProducePlanType_Plan2"
_PHASE_NONE = "ProduceExamPhaseType_None"
_PHASE_START_PLAY = "ProduceExamPhaseType_StartPlay"
_PHASE_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"
_FIELD_REVIEW_UP = "ProduceExamFieldStatusType_ReviewUp"
_MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
_LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
_CARD_MOVE_TRIGGER_UNKNOWN = "ProduceCardMoveEffectTriggerType_Unknown"

_EFFECT_REVIEW = "ProduceExamEffectType_ExamReview"
_EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
_EFFECT_PLAYABLE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
_EFFECT_STAMINA_ADD = "ProduceExamEffectType_ExamStaminaConsumptionAdd"
_EFFECT_ADD_GROW = "ProduceExamEffectType_ExamAddGrowEffect"
_EFFECT_STATUS_ENCHANT = "ProduceExamEffectType_ExamStatusEnchant"


class Plan2NativeCatalogReviewMultipleContractError(ValueError):
    """A Master row is outside the exact bounded adapter contract."""


# Friendly aliases used by neighboring bounded adapters and by central callers.
Plan2ReviewMultipleCatalogContractError = (
    Plan2NativeCatalogReviewMultipleContractError
)
Plan2NativeReviewMultipleContractError = (
    Plan2NativeCatalogReviewMultipleContractError
)


class Plan2ReviewMultiplePlayOrigin(str, Enum):
    """Card-play origins sharing the native direct-effect path."""

    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


PLAY_ORIGINS: Final = tuple(item.value for item in Plan2ReviewMultiplePlayOrigin)


def _plain_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} must be a plain integer"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} is outside Int32"
        )
    if minimum is not None and value < minimum:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} must be >= {minimum}"
        )
    return value


def _nonempty(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} must be non-empty text"
        )
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} must be text"
        )
    return value


def _json_value(value: object, label: str) -> object:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} is not valid JSON"
        ) from error


def _json_list(value: object, label: str) -> tuple[object, ...]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} must be a JSON array"
        )
    return tuple(parsed)


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_list(value, label)
    if any(type(item) is not str for item in parsed):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} must contain only strings"
        )
    return tuple(parsed)  # type: ignore[return-value]


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_list(value, label)
    if any(isinstance(item, bool) or type(item) is not int for item in parsed):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label} must contain only integers"
        )
    return tuple(parsed)  # type: ignore[return-value]


def _required_raw_equal(
    raw: Mapping[str, object], key: str, expected: object, label: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{label}: {key} changed; expected {expected!r}, got {raw.get(key)!r}"
        )


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleTriggerRow:
    """Normalized non-description fields of one Master trigger row."""

    trigger_id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_produce_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str

    def __post_init__(self) -> None:
        _nonempty(self.trigger_id, "trigger_id")
        for name in (
            "phase_types",
            "field_status_check_types",
            "field_status_types",
            "field_status_produce_card_search_ids",
            "effect_types",
        ):
            values = tuple(getattr(self, name))
            if any(type(value) is not str for value in values):
                raise TypeError(f"{name} must contain strings")
            object.__setattr__(self, name, values)
        for name in ("phase_values", "field_status_values"):
            values = tuple(getattr(self, name))
            if any(isinstance(value, bool) or type(value) is not int for value in values):
                raise TypeError(f"{name} must contain integers")
            object.__setattr__(self, name, values)
        _text(self.produce_card_search_id, "produce_card_search_id")
        _plain_int(self.upper_search_count, "upper_search_count", minimum=0)
        _plain_int(self.lower_search_count, "lower_search_count", minimum=0)
        _nonempty(self.card_move_position_type, "card_move_position_type")
        _nonempty(self.lesson_type, "lesson_type")

    @property
    def is_unconditional(self) -> bool:
        return (
            self.phase_types == (_PHASE_NONE,)
            and not self.phase_values
            and not self.field_status_check_types
            and not self.field_status_types
            and not self.field_status_values
            and not self.field_status_produce_card_search_ids
            and self.produce_card_search_id == ""
            and self.upper_search_count == 0
            and self.lower_search_count == 0
            and self.card_move_position_type == _MOVE_UNKNOWN
            and not self.effect_types
            and self.lesson_type == _LESSON_UNKNOWN
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.trigger_id,
            "phaseTypes": list(self.phase_types),
            "phaseValues": list(self.phase_values),
            "fieldStatusCheckTypes": list(self.field_status_check_types),
            "fieldStatusTypes": list(self.field_status_types),
            "fieldStatusValues": list(self.field_status_values),
            "fieldStatusProduceCardSearchIds": list(
                self.field_status_produce_card_search_ids
            ),
            "produceCardSearchId": self.produce_card_search_id,
            "upperSearchCount": self.upper_search_count,
            "lowerSearchCount": self.lower_search_count,
            "cardMovePositionType": self.card_move_position_type,
            "effectTypes": list(self.effect_types),
            "lessonType": self.lesson_type,
        }


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleEffectRow:
    """Exact normalized Master effect payload retained in slot/order form."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "effect_id")
        _nonempty(self.effect_type, "effect_type")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _plain_int(getattr(self, name), name)
        if self.effect_count < 0:
            raise ValueError("effect_count must be non-negative")
        _text(self.status_enchant_id, "status_enchant_id")
        _text(self.chain_effect_id, "chain_effect_id")

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def value(self) -> int:
        return self.value1

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.effect_count,
            "turn": self.effect_turn,
            "statusEnchantId": self.status_enchant_id,
            "chainEffectId": self.chain_effect_id,
        }


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleEffectSlot:
    """One zero-based direct play-effect slot in Master order."""

    slot_index: int
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool
    row: Plan2ReviewMultipleEffectRow

    def __post_init__(self) -> None:
        _plain_int(self.slot_index, "slot_index", minimum=0)
        _text(self.trigger_id, "effect slot trigger_id")
        if type(self.hide_icon) is not bool:
            raise TypeError("hide_icon must be boolean")
        if type(self.is_once_play_effect) is not bool:
            raise TypeError("is_once_play_effect must be boolean")
        if not isinstance(self.row, Plan2ReviewMultipleEffectRow):
            raise TypeError("row must be Plan2ReviewMultipleEffectRow")

    @property
    def effect_id(self) -> str:
        return self.row.effect_id

    @property
    def effect_type(self) -> str:
        return self.row.effect_type

    @property
    def value1(self) -> int:
        return self.row.value1

    @property
    def value2(self) -> int:
        return self.row.value2

    @property
    def effect_count(self) -> int:
        return self.row.effect_count

    @property
    def effect_turn(self) -> int:
        return self.row.effect_turn

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot_index,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
            "effect": self.row.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleStatusRow:
    """Exact status-enchant row and its ordered child effect rows."""

    status_enchant_id: str
    wrapper_effect_id: str
    wrapper_slot_index: int
    wrapper_value1: int
    wrapper_value2: int
    wrapper_count: int
    wrapper_turn: int
    trigger: Plan2ReviewMultipleTriggerRow
    child_effect_ids: tuple[str, ...]
    child_effects: tuple[Plan2ReviewMultipleEffectRow, ...]

    def __post_init__(self) -> None:
        _nonempty(self.status_enchant_id, "status_enchant_id")
        _nonempty(self.wrapper_effect_id, "wrapper_effect_id")
        _plain_int(self.wrapper_slot_index, "wrapper_slot_index", minimum=0)
        for name in ("wrapper_value1", "wrapper_value2", "wrapper_count", "wrapper_turn"):
            _plain_int(getattr(self, name), name)
        if self.wrapper_count < 0:
            raise ValueError("wrapper_count must be non-negative")
        if not isinstance(self.trigger, Plan2ReviewMultipleTriggerRow):
            raise TypeError("trigger must be Plan2ReviewMultipleTriggerRow")
        ids = tuple(self.child_effect_ids)
        effects = tuple(self.child_effects)
        if not ids or len(ids) != len(effects):
            raise ValueError("status child IDs/effects must be non-empty and aligned")
        if any(type(value) is not str or not value for value in ids):
            raise TypeError("child_effect_ids must contain non-empty text")
        if any(not isinstance(value, Plan2ReviewMultipleEffectRow) for value in effects):
            raise TypeError("child_effects must contain effect rows")
        if tuple(value.effect_id for value in effects) != ids:
            raise ValueError("status child order changed")
        object.__setattr__(self, "child_effect_ids", ids)
        object.__setattr__(self, "child_effects", effects)

    @property
    def effect_turn(self) -> int:
        return self.wrapper_turn

    @property
    def is_permanent(self) -> bool:
        return self.wrapper_turn == PERMANENT_TURN

    def to_dict(self) -> dict[str, object]:
        return {
            "statusId": self.status_enchant_id,
            "wrapperEffectId": self.wrapper_effect_id,
            "wrapperSlot": self.wrapper_slot_index,
            "wrapperValue1": self.wrapper_value1,
            "wrapperValue2": self.wrapper_value2,
            "wrapperCount": self.wrapper_count,
            "wrapperTurn": self.wrapper_turn,
            "trigger": self.trigger.to_dict(),
            "childEffectIds": list(self.child_effect_ids),
            "childEffects": [value.to_dict() for value in self.child_effects],
        }


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleCardVersion:
    """One exact current-Master card version containing the target slot."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    play_trigger: Plan2ReviewMultipleTriggerRow | None
    move_position_type: str
    effect_group_ids: tuple[str, ...]
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    move_trigger_ids: tuple[str, ...]
    card_status_enchant_id: str
    effect_slots: tuple[Plan2ReviewMultipleEffectSlot, ...]
    review_multiple_slot_index: int
    review_multiple_effect: Plan2ReviewMultipleEffect
    status_rows: tuple[Plan2ReviewMultipleStatusRow, ...]
    companion_blockers: tuple["Plan2ReviewMultipleBlocker", ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.card_id, "card_id")
        _plain_int(self.upgrade, "upgrade", minimum=0)
        if self.plan_type != _PLAN2:
            raise ValueError("card version must be Plan2")
        _nonempty(self.category, "category")
        _plain_int(self.stamina, "stamina", minimum=0)
        _nonempty(self.cost_type, "cost_type")
        _plain_int(self.cost_value, "cost_value", minimum=0)
        _text(self.play_trigger_id, "play_trigger_id")
        if self.play_trigger is not None and not isinstance(
            self.play_trigger, Plan2ReviewMultipleTriggerRow
        ):
            raise TypeError("play_trigger must be a trigger row or None")
        _nonempty(self.move_position_type, "move_position_type")
        for name in (
            "effect_group_ids",
            "move_effect_ids",
            "move_trigger_ids",
        ):
            values = tuple(getattr(self, name))
            if any(type(value) is not str or not value for value in values):
                raise TypeError(f"{name} must contain non-empty text")
            object.__setattr__(self, name, values)
        _nonempty(self.move_effect_trigger_type, "move_effect_trigger_type")
        _text(self.card_status_enchant_id, "card_status_enchant_id")
        slots = tuple(self.effect_slots)
        if not slots or any(not isinstance(value, Plan2ReviewMultipleEffectSlot) for value in slots):
            raise TypeError("effect_slots must contain effect slots")
        if tuple(value.slot_index for value in slots) != tuple(range(len(slots))):
            raise ValueError("effect_slots must preserve contiguous Master order")
        object.__setattr__(self, "effect_slots", slots)
        _plain_int(self.review_multiple_slot_index, "review_multiple_slot_index", minimum=0)
        if self.review_multiple_slot_index >= len(slots):
            raise ValueError("review_multiple_slot_index is outside effect_slots")
        target = slots[self.review_multiple_slot_index]
        if target.effect_type != REVIEW_MULTIPLE_EFFECT_TYPE:
            raise ValueError("review_multiple slot is not ReviewMultiple")
        if target.effect_id != self.review_multiple_effect.effect_id:
            raise ValueError("ReviewMultiple effect identity mismatch")
        statuses = tuple(self.status_rows)
        if any(not isinstance(value, Plan2ReviewMultipleStatusRow) for value in statuses):
            raise TypeError("status_rows must contain Plan2ReviewMultipleStatusRow")
        object.__setattr__(self, "status_rows", statuses)
        blockers = tuple(self.companion_blockers)
        if any(not isinstance(value, Plan2ReviewMultipleBlocker) for value in blockers):
            raise TypeError("companion_blockers must contain blockers")
        object.__setattr__(self, "companion_blockers", blockers)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(value.effect_id for value in self.effect_slots)

    @property
    def review_multiple_slot(self) -> Plan2ReviewMultipleEffectSlot:
        return self.effect_slots[self.review_multiple_slot_index]

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return PLAY_ORIGINS

    @property
    def target_executable(self) -> bool:
        return True

    @property
    def whole_card_executable(self) -> bool:
        return not self.companion_blockers

    @property
    def companion_blocker_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.family for value in self.companion_blockers))

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "versionRef": self.version_ref,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "playTrigger": None
            if self.play_trigger is None
            else self.play_trigger.to_dict(),
            "movePositionType": self.move_position_type,
            "effectGroupIds": list(self.effect_group_ids),
            "moveEffectTriggerType": self.move_effect_trigger_type,
            "moveEffectIds": list(self.move_effect_ids),
            "moveTriggerIds": list(self.move_trigger_ids),
            "cardStatusEnchantId": self.card_status_enchant_id,
            "orderedEffectIds": list(self.ordered_effect_ids),
            "reviewMultipleSlot": self.review_multiple_slot_index,
            "reviewMultiple": {
                "effectId": self.review_multiple_effect.effect_id,
                "permil": self.review_multiple_effect.permil,
                "count": self.review_multiple_slot.effect_count,
                "turn": self.review_multiple_effect.turn,
                "lifetime": (
                    "permanent"
                    if self.review_multiple_effect.is_permanent
                    else "finite"
                ),
            },
            "effectSlots": [value.to_dict() for value in self.effect_slots],
            "statusRows": [value.to_dict() for value in self.status_rows],
            "companionBlockers": [value.to_dict() for value in self.companion_blockers],
            "targetExecutable": self.target_executable,
            "wholeCardExecutable": self.whole_card_executable,
        }


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleBlocker:
    """Exact per-version blocker; never represents a partially compiled card."""

    code: str
    version_ref: str
    family: str
    detail: str
    effect_id: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.code, "blocker code")
        _nonempty(self.version_ref, "blocker version_ref")
        _nonempty(self.family, "blocker family")
        if type(self.detail) is not str:
            raise TypeError("blocker detail must be text")
        if type(self.effect_id) is not str:
            raise TypeError("blocker effect_id must be text")

    @property
    def ref(self) -> tuple[str, int]:
        card_id, separator, upgrade = self.version_ref.rpartition("#")
        if not separator or not upgrade.isdigit():
            raise ValueError(f"invalid blocker version_ref: {self.version_ref}")
        return card_id, int(upgrade)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "versionRef": self.version_ref,
            "family": self.family,
            "effectId": self.effect_id,
            "detail": self.detail,
        }


Plan2NativeReviewMultipleBlocker = Plan2ReviewMultipleBlocker
Plan2NativeCatalogReviewMultipleBlocker = Plan2ReviewMultipleBlocker


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewMultipleProgram:
    """Compiled target-family program for one card version."""

    card: Plan2ReviewMultipleCardVersion

    def __post_init__(self) -> None:
        if not isinstance(self.card, Plan2ReviewMultipleCardVersion):
            raise TypeError("card must be Plan2ReviewMultipleCardVersion")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card.ref

    @property
    def card_id(self) -> str:
        return self.card.card_id

    @property
    def upgrade(self) -> int:
        return self.card.upgrade

    @property
    def effect(self) -> Plan2ReviewMultipleEffect:
        return self.card.review_multiple_effect

    @property
    def effect_id(self) -> str:
        return self.effect.effect_id

    @property
    def slot_index(self) -> int:
        return self.card.review_multiple_slot_index

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return self.card.ordered_effect_ids

    @property
    def play_origins(self) -> tuple[str, ...]:
        return self.card.supported_play_origins

    def to_dict(self) -> dict[str, object]:
        return self.card.to_dict()


Plan2ReviewMultipleCompiledProgram = Plan2NativeReviewMultipleProgram


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewMultipleCatalog:
    """Immutable catalog handed to a central catalog/horizon."""

    database: str
    programs: tuple[Plan2NativeReviewMultipleProgram, ...]

    def __post_init__(self) -> None:
        _nonempty(self.database, "database")
        programs = tuple(self.programs)
        if any(not isinstance(value, Plan2NativeReviewMultipleProgram) for value in programs):
            raise TypeError("programs must contain target-family programs")
        refs = tuple(value.ref for value in programs)
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate target-family card version")
        if refs != tuple(sorted(refs)):
            raise ValueError("programs must be sorted by card version")
        object.__setattr__(self, "programs", programs)

    @property
    def versions(self) -> tuple[Plan2NativeReviewMultipleProgram, ...]:
        return self.programs

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(value.ref for value in self.programs)

    @property
    def affected_version_count(self) -> int:
        return len(self.programs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.affected_refs

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    def get(self, card_id: str, upgrade: int) -> Plan2NativeReviewMultipleProgram | None:
        return next((value for value in self.programs if value.ref == (card_id, upgrade)), None)

    def resolve(self, card_id: str, upgrade: int) -> Plan2NativeReviewMultipleProgram:
        program = self.get(card_id, upgrade)
        if program is None:
            raise KeyError(f"uncompiled ReviewMultiple version: {card_id}#{upgrade}")
        return program

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "affectedVersionCount": self.affected_version_count,
            "executableVersionCount": self.executable_version_count,
            "programs": [value.to_dict() for value in self.programs],
        }


Plan2ReviewMultipleCatalog = Plan2NativeReviewMultipleCatalog


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewMultipleCatalogCompilation:
    """Per-version compile result, including hard fail-closed blockers."""

    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    catalog: Plan2NativeReviewMultipleCatalog
    blockers: tuple[Plan2ReviewMultipleBlocker, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_CATALOG_REVIEW_MULTIPLE_SCHEMA_VERSION:
            raise ValueError("unsupported ReviewMultiple catalog schema version")
        refs = tuple(self.affected_refs)
        if len(refs) != len(set(refs)):
            raise ValueError("affected_refs contains duplicates")
        if refs != tuple(sorted(refs)):
            raise ValueError("affected_refs must be sorted")
        if not isinstance(self.catalog, Plan2NativeReviewMultipleCatalog):
            raise TypeError("catalog must be Plan2NativeReviewMultipleCatalog")
        if any(value not in refs for value in self.catalog.affected_refs):
            raise ValueError("catalog contains a version outside affected_refs")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2ReviewMultipleBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan2ReviewMultipleBlocker")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", blockers)

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
    def executable_version_count(self) -> int:
        return self.catalog.executable_version_count

    @property
    def fully_compiled(self) -> bool:
        return not self.failed_refs and not self.blockers

    @property
    def target_effect_executable(self) -> bool:
        return (
            self.affected_version_count == REVIEW_MULTIPLE_TARGET_VERSION_COUNT
            and self.executable_version_count == self.affected_version_count
            and not self.failed_refs
        )

    @property
    def companion_blockers(self) -> tuple[Plan2ReviewMultipleBlocker, ...]:
        return tuple(
            blocker
            for program in self.catalog.programs
            for blocker in program.card.companion_blockers
        )

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            program.ref
            for program in self.catalog.programs
            if program.card.companion_blockers
        )

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            program.ref
            for program in self.catalog.programs
            if program.card.whole_card_executable
        )

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(value.code for value in self.companion_blockers).items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs_by_family: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.companion_blockers:
            refs_by_family.setdefault(blocker.family, set()).add(blocker.ref)
        return {
            family: len(refs)
            for family, refs in sorted(refs_by_family.items())
        }

    def blockers_for(self, ref: tuple[str, int]) -> tuple[Plan2ReviewMultipleBlocker, ...]:
        return tuple(value for value in self.blockers if value.ref == ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "database": self.database,
            "affectedVersionCount": self.affected_version_count,
            "compiledVersionCount": self.compiled_version_count,
            "failedVersionCount": self.failed_version_count,
            "executableVersionCount": self.executable_version_count,
            "targetEffectExecutable": self.target_effect_executable,
            "fullyCompiled": self.fully_compiled,
            "affectedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "compiledRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.compiled_refs
            ],
            "failedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.failed_refs
            ],
            "blockers": [value.to_dict() for value in self.blockers],
            "companionBlockers": [value.to_dict() for value in self.companion_blockers],
            "companionBlockedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "wholeCardExecutableRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "companionBlockerCodeCounts": self.companion_blocker_code_counts,
            "companionFamilyVersionCounts": self.companion_family_version_counts,
        }


Plan2ReviewMultipleCatalogCompilation = Plan2NativeReviewMultipleCatalogCompilation


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleInstallTransition:
    """Immutable direct-effect installation result with native status identity."""

    before: Plan2State
    after: Plan2State
    program: Plan2NativeReviewMultipleProgram
    play_origin: Plan2ReviewMultiplePlayOrigin
    native: Plan2ReviewMultipleTransition
    active_status_uids_before: tuple[int, ...]
    active_status_uids_after: tuple[int, ...]
    event_trace: tuple[str, ...]

    @property
    def created_status_uid(self) -> int | None:
        return self.native.created_status_uid

    @property
    def merged_status_uid(self) -> int | None:
        return self.native.merged_status_uid

    @property
    def installed_status_uid(self) -> int:
        uid = self.created_status_uid or self.merged_status_uid
        if uid is None:
            raise AssertionError("native install did not create or merge a status")
        return uid

    @property
    def status_uid(self) -> int:
        return self.installed_status_uid

    @property
    def stack_order_after(self) -> tuple[int, ...]:
        return self.active_status_uids_after

    @property
    def multiplier_after(self) -> float:
        return self.native.multiplier_after


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleTurnStartTransition:
    """Target-family projection of shared TurnStart freshness/expiry."""

    before: Plan2State
    after: Plan2State
    native: Plan2TurnStartTransition
    target_status_uids_before: tuple[int, ...]
    target_status_uids_after: tuple[int, ...]
    fresh_status_uids: tuple[int, ...]
    spent_status_uids: tuple[int, ...]
    expired_status_uids: tuple[int, ...]
    permanent_status_uids: tuple[int, ...]

    @property
    def stack_order_after(self) -> tuple[int, ...]:
        return self.target_status_uids_after


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleTurnEndTransition:
    """Target-family projection of native end-turn Review score order."""

    before: Plan2State
    after: Plan2State
    native: Plan2ReviewScoreTransition

    @property
    def occurrences(self) -> tuple[object, ...]:
        return self.native.occurrences

    @property
    def raw_score_per_occurrence(self) -> int:
        return self.native.raw_score_per_occurrence

    @property
    def review_multiple(self) -> float:
        return self.native.review_multiple


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleHandoffRow:
    """Central-consumable row; card ownership is explicit and conservative."""

    version_ref: str
    card_id: str
    upgrade: int
    target_effect_id: str
    target_slot_index: int
    value1_permille: int
    value2: int
    count: int
    turn: int
    lifetime: str
    ordered_effect_ids: tuple[str, ...]
    status_enchant_ids: tuple[str, ...]
    supported_play_origins: tuple[str, ...]
    target_effect_executable: bool
    whole_card_executable: bool
    companion_blockers: tuple[Plan2ReviewMultipleBlocker, ...]

    def __post_init__(self) -> None:
        _nonempty(self.version_ref, "version_ref")
        _nonempty(self.card_id, "card_id")
        _plain_int(self.upgrade, "upgrade", minimum=0)
        _nonempty(self.target_effect_id, "target_effect_id")
        _plain_int(self.target_slot_index, "target_slot_index", minimum=0)
        _plain_int(self.value1_permille, "value1_permille")
        _plain_int(self.value2, "value2")
        _plain_int(self.count, "count", minimum=0)
        _plain_int(self.turn, "turn")
        if self.turn != PERMANENT_TURN and self.turn < 1:
            raise ValueError("finite target turn must be positive")
        if self.lifetime not in {"finite", "permanent"}:
            raise ValueError("unsupported lifetime")
        if self.lifetime == "permanent" and self.turn != PERMANENT_TURN:
            raise ValueError("permanent row must use -1 turn")
        if self.lifetime == "finite" and self.turn == PERMANENT_TURN:
            raise ValueError("finite row cannot use permanent turn")
        object.__setattr__(self, "ordered_effect_ids", tuple(self.ordered_effect_ids))
        object.__setattr__(self, "status_enchant_ids", tuple(self.status_enchant_ids))
        object.__setattr__(self, "supported_play_origins", tuple(self.supported_play_origins))
        if any(not isinstance(value, Plan2ReviewMultipleBlocker) for value in self.companion_blockers):
            raise TypeError("companion_blockers must contain blockers")
        object.__setattr__(self, "companion_blockers", tuple(self.companion_blockers))

    def to_dict(self) -> dict[str, object]:
        return {
            "versionRef": self.version_ref,
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "targetEffectId": self.target_effect_id,
            "targetSlotIndex": self.target_slot_index,
            "value1Permille": self.value1_permille,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "lifetime": self.lifetime,
            "orderedEffectIds": list(self.ordered_effect_ids),
            "statusEnchantIds": list(self.status_enchant_ids),
            "supportedPlayOrigins": list(self.supported_play_origins),
            "targetEffectExecutable": self.target_effect_executable,
            "wholeCardExecutable": self.whole_card_executable,
            "companionBlockers": [value.to_dict() for value in self.companion_blockers],
        }


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleCentralHandoff:
    """Typed handoff without importing or invoking the central horizon."""

    adapter_id: str
    schema_version: int
    affected_refs: tuple[tuple[str, int], ...]
    executable_refs: tuple[tuple[str, int], ...]
    rows: tuple[Plan2ReviewMultipleHandoffRow, ...]

    def __post_init__(self) -> None:
        _nonempty(self.adapter_id, "adapter_id")
        if self.schema_version != PLAN2_NATIVE_CATALOG_REVIEW_MULTIPLE_SCHEMA_VERSION:
            raise ValueError("unsupported handoff schema version")
        affected = tuple(self.affected_refs)
        executable = tuple(self.executable_refs)
        rows = tuple(self.rows)
        if affected != tuple(sorted(affected)) or len(affected) != len(set(affected)):
            raise ValueError("handoff affected refs must be unique and sorted")
        if executable != tuple(sorted(executable)) or set(executable) - set(affected):
            raise ValueError("handoff executable refs must be a sorted subset")
        if tuple((row.card_id, row.upgrade) for row in rows) != affected:
            raise ValueError("handoff rows must preserve affected version order")
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
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (row.card_id, row.upgrade)
            for row in self.rows
            if row.companion_blockers
        )

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (row.card_id, row.upgrade)
            for row in self.rows
            if row.whole_card_executable
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "adapterId": self.adapter_id,
            "schemaVersion": self.schema_version,
            "affectedVersionCount": self.affected_version_count,
            "executableVersionCount": self.executable_version_count,
            "affectedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "executableRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.executable_refs
            ],
            "companionBlockedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "wholeCardExecutableRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "rows": [value.to_dict() for value in self.rows],
        }


# These are a change detector, not a source of effect semantics.  All payloads
# below are still read and validated from Master on every compile.
EXPECTED_REVIEW_MULTIPLE_VERSION_EFFECTS: Final[dict[tuple[str, int], str]] = {
    ("p_card-02-act-3_187", 0): "e_effect-exam_review_multiple-2000-03",
    ("p_card-02-act-3_187", 1): "e_effect-exam_review_multiple-2000-04",
    ("p_card-02-act-3_187", 2): "e_effect-exam_review_multiple-2700-04",
    ("p_card-02-act-3_187", 3): "e_effect-exam_review_multiple-2700-04",
    ("p_card-02-ido-100_037", 0): "e_effect-exam_review_multiple-0300-inf",
    ("p_card-02-ido-3_148", 0): "e_effect-exam_review_multiple-0200-inf",
    ("p_card-02-ido-3_148", 1): "e_effect-exam_review_multiple-0200-inf",
    ("p_card-02-ido-3_148", 2): "e_effect-exam_review_multiple-0250-inf",
    ("p_card-02-ido-3_148", 3): "e_effect-exam_review_multiple-0250-inf",
    ("p_card-02-ido-3_171", 0): "e_effect-exam_review_multiple-0500-05",
    ("p_card-02-ido-3_171", 1): "e_effect-exam_review_multiple-0500-05",
    ("p_card-02-ido-3_171", 2): "e_effect-exam_review_multiple-0500-05",
    ("p_card-02-ido-3_171", 3): "e_effect-exam_review_multiple-0500-05",
    ("p_card-02-ido-3_232", 0): "e_effect-exam_review_multiple-0200-03",
    ("p_card-02-ido-3_232", 1): "e_effect-exam_review_multiple-0200-04",
    ("p_card-02-ido-3_232", 2): "e_effect-exam_review_multiple-0200-04",
    ("p_card-02-ido-3_232", 3): "e_effect-exam_review_multiple-0200-04",
    ("p_card-02-men-100_008", 0): "e_effect-exam_review_multiple-1000-inf",
    ("p_card-02-men-1_076", 0): "e_effect-exam_review_multiple-0100-inf",
    ("p_card-02-men-1_076", 1): "e_effect-exam_review_multiple-0100-inf",
    ("p_card-02-men-1_076", 2): "e_effect-exam_review_multiple-0100-inf",
    ("p_card-02-men-1_076", 3): "e_effect-exam_review_multiple-0100-inf",
    ("p_card-02-sup-3_156", 0): "e_effect-exam_review_multiple-1000-03",
    ("p_card-02-sup-3_156", 1): "e_effect-exam_review_multiple-1000-03",
    ("p_card-02-sup-3_156", 2): "e_effect-exam_review_multiple-1000-03",
    ("p_card-02-sup-3_156", 3): "e_effect-exam_review_multiple-1000-03",
}


def _row_value(row: sqlite3.Row, key: str) -> object:
    try:
        return row[key]
    except (IndexError, KeyError) as error:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"Master row is missing column {key}"
        ) from error


def _effect_row_from_db(row: sqlite3.Row) -> Plan2ReviewMultipleEffectRow:
    effect_id = _nonempty(_row_value(row, "id"), "effect.id")
    raw = _json_value(_row_value(row, "raw_json"), f"effect:{effect_id}.raw_json")
    if not isinstance(raw, Mapping):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"effect:{effect_id}.raw_json must be an object"
        )
    effect_type = _nonempty(_row_value(row, "effect_type"), f"effect:{effect_id}.type")
    value1 = _plain_int(_row_value(row, "value1"), f"effect:{effect_id}.value1")
    value2 = _plain_int(_row_value(row, "value2"), f"effect:{effect_id}.value2")
    count = _plain_int(_row_value(row, "effect_count"), f"effect:{effect_id}.count")
    turn = _plain_int(_row_value(row, "effect_turn"), f"effect:{effect_id}.turn")
    status_id = _text(_row_value(row, "status_enchant_id"), f"effect:{effect_id}.status")
    chain_id = _text(_row_value(row, "chain_effect_id"), f"effect:{effect_id}.chain")
    for key, expected in (
        ("id", effect_id),
        ("effectType", effect_type),
        ("effectValue1", value1),
        ("effectValue2", value2),
        ("effectCount", count),
        ("effectTurn", turn),
        ("produceExamStatusEnchantId", status_id),
        ("chainProduceExamEffectId", chain_id),
    ):
        _required_raw_equal(raw, key, expected, f"effect:{effect_id}.raw_json")
    chain_ids = raw.get("chainProduceExamEffectIds")
    if chain_ids is None or not isinstance(chain_ids, list):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"effect:{effect_id}: chainProduceExamEffectIds must be an array"
        )
    if any(type(value) is not str for value in chain_ids):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"effect:{effect_id}: chainProduceExamEffectIds must contain strings"
        )
    return Plan2ReviewMultipleEffectRow(
        effect_id,
        effect_type,
        value1,
        value2,
        count,
        turn,
        status_id,
        chain_id,
    )


def _trigger_row_from_db(row: sqlite3.Row) -> Plan2ReviewMultipleTriggerRow:
    trigger_id = _nonempty(_row_value(row, "id"), "trigger.id")
    raw = _json_value(_row_value(row, "raw_json"), f"trigger:{trigger_id}.raw_json")
    if not isinstance(raw, Mapping):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"trigger:{trigger_id}.raw_json must be an object"
        )
    phase_types = _string_tuple(_row_value(row, "phase_types_json"), f"trigger:{trigger_id}.phase_types")
    phase_values = _int_tuple(_row_value(row, "phase_values_json"), f"trigger:{trigger_id}.phase_values")
    field_checks = _string_tuple(
        _row_value(row, "field_status_check_types_json"), f"trigger:{trigger_id}.field_checks"
    )
    field_types = _string_tuple(
        _row_value(row, "field_status_types_json"), f"trigger:{trigger_id}.field_types"
    )
    field_values = _int_tuple(
        _row_value(row, "field_status_values_json"), f"trigger:{trigger_id}.field_values"
    )
    field_searches = _string_tuple(
        _row_value(row, "field_status_produce_card_search_ids_json"),
        f"trigger:{trigger_id}.field_searches",
    )
    search_id = _text(_row_value(row, "produce_card_search_id"), f"trigger:{trigger_id}.search_id")
    upper = _plain_int(_row_value(row, "upper_search_count"), f"trigger:{trigger_id}.upper", minimum=0)
    lower = _plain_int(_row_value(row, "lower_search_count"), f"trigger:{trigger_id}.lower", minimum=0)
    move = _nonempty(_row_value(row, "card_move_position_type"), f"trigger:{trigger_id}.move")
    effect_types = _string_tuple(_row_value(row, "effect_types_json"), f"trigger:{trigger_id}.effect_types")
    lesson = _nonempty(_row_value(row, "lesson_type"), f"trigger:{trigger_id}.lesson")
    for key, expected in (
        ("id", trigger_id),
        ("phaseTypes", list(phase_types)),
        ("phaseValues", list(phase_values)),
        ("fieldStatusCheckTypes", list(field_checks)),
        ("fieldStatusTypes", list(field_types)),
        ("fieldStatusValues", list(field_values)),
        ("fieldStatusProduceCardSearchIds", list(field_searches)),
        ("produceCardSearchId", search_id),
        ("upperSearchCount", upper),
        ("lowerSearchCount", lower),
        ("cardMovePositionType", move),
        ("effectTypes", list(effect_types)),
        ("lessonType", lesson),
    ):
        _required_raw_equal(raw, key, expected, f"trigger:{trigger_id}.raw_json")
    return Plan2ReviewMultipleTriggerRow(
        trigger_id,
        phase_types,
        phase_values,
        field_checks,
        field_types,
        field_values,
        field_searches,
        search_id,
        upper,
        lower,
        move,
        effect_types,
        lesson,
    )


def _load_trigger(
    connection: sqlite3.Connection, trigger_id: str
) -> Plan2ReviewMultipleTriggerRow:
    if not trigger_id:
        raise Plan2NativeCatalogReviewMultipleContractError("trigger_id cannot be empty")
    row = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
    ).fetchone()
    if row is None:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"unknown Master trigger: {trigger_id}"
        )
    return _trigger_row_from_db(row)


def _native_review_effect(
    effect_id: str, database: Path
) -> tuple[Plan2ReviewMultipleEffectRow, Plan2ReviewMultipleEffect]:
    database = Path(database).resolve()
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM effect WHERE id = ?", (effect_id,)).fetchone()
        if row is None:
            raise Plan2NativeCatalogReviewMultipleContractError(
                f"unknown Master effect: {effect_id}"
            )
        normalized = _effect_row_from_db(row)
    try:
        native = load_plan2_review_multiple_effect(effect_id, database)
    except (Plan2ReviewMultipleContractError, KeyError) as error:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{effect_id}: existing ReviewMultiple primitive rejected row"
        ) from error
    if normalized.effect_type != REVIEW_MULTIPLE_EFFECT_TYPE:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{effect_id}: target effect type changed"
        )
    if normalized.effect_count != 0:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{effect_id}: target effect count must be zero"
        )
    return normalized, native


def _effect_slot_from_link(
    connection: sqlite3.Connection,
    index: int,
    link: object,
) -> Plan2ReviewMultipleEffectSlot:
    if not isinstance(link, Mapping):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"play effect slot {index} must be an object"
        )
    required = {"produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"}
    if set(link) != required:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"play effect slot {index} shape changed: {sorted(link)}"
        )
    trigger_id = _text(link["produceExamTriggerId"], f"slot {index}.trigger_id")
    effect_id = _nonempty(link["produceExamEffectId"], f"slot {index}.effect_id")
    if type(link["hideIcon"]) is not bool or type(link["isOncePlayEffect"]) is not bool:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"play effect slot {index} boolean shape changed"
        )
    row = connection.execute("SELECT * FROM effect WHERE id = ?", (effect_id,)).fetchone()
    if row is None:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"slot {index}: unknown effect {effect_id}"
        )
    return Plan2ReviewMultipleEffectSlot(
        index,
        trigger_id,
        link["hideIcon"],
        link["isOncePlayEffect"],
        _effect_row_from_db(row),
    )


def _status_row_from_slot(
    connection: sqlite3.Connection,
    slot: Plan2ReviewMultipleEffectSlot,
) -> Plan2ReviewMultipleStatusRow:
    wrapper = slot.row
    if wrapper.effect_type != _EFFECT_STATUS_ENCHANT or not wrapper.status_enchant_id:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{wrapper.effect_id}: status wrapper shape is incomplete"
        )
    status = connection.execute(
        "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
        (wrapper.status_enchant_id,),
    ).fetchone()
    if status is None:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{wrapper.effect_id}: unknown status {wrapper.status_enchant_id}"
        )
    status_raw = _json_value(status["raw_json"], f"status:{status['id']}.raw_json")
    if not isinstance(status_raw, Mapping):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"status:{status['id']}.raw_json must be an object"
        )
    child_ids = _string_tuple(status["produce_exam_effect_ids_json"], f"status:{status['id']}.children")
    trigger_id = _nonempty(status["produce_exam_trigger_id"], f"status:{status['id']}.trigger_id")
    _required_raw_equal(status_raw, "id", status["id"], f"status:{status['id']}.raw_json")
    _required_raw_equal(status_raw, "assetId", status["asset_id"], f"status:{status['id']}.raw_json")
    _required_raw_equal(status_raw, "produceExamTriggerId", trigger_id, f"status:{status['id']}.raw_json")
    _required_raw_equal(status_raw, "produceExamEffectIds", list(child_ids), f"status:{status['id']}.raw_json")
    trigger = _load_trigger(connection, trigger_id)
    children: list[Plan2ReviewMultipleEffectRow] = []
    for child_id in child_ids:
        child = connection.execute("SELECT * FROM effect WHERE id = ?", (child_id,)).fetchone()
        if child is None:
            raise Plan2NativeCatalogReviewMultipleContractError(
                f"status:{status['id']}: unknown child effect {child_id}"
            )
        children.append(_effect_row_from_db(child))
    return Plan2ReviewMultipleStatusRow(
        status_enchant_id=str(status["id"]),
        wrapper_effect_id=wrapper.effect_id,
        wrapper_slot_index=slot.slot_index,
        wrapper_value1=wrapper.value1,
        wrapper_value2=wrapper.value2,
        wrapper_count=wrapper.effect_count,
        wrapper_turn=wrapper.effect_turn,
        trigger=trigger,
        child_effect_ids=child_ids,
        child_effects=tuple(children),
    )


def _card_raw_fields(row: sqlite3.Row, card_id: str) -> tuple[tuple[str, ...], str, tuple[str, ...], tuple[str, ...], str]:
    raw = _json_value(row["raw_json"], f"card:{card_id}.raw_json")
    if not isinstance(raw, Mapping):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"card:{card_id}.raw_json must be an object"
        )
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or any(type(value) is not str or not value for value in groups):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"card:{card_id}: effectGroupIds shape changed"
        )
    move_trigger = raw.get("moveEffectTriggerType")
    move_effects = raw.get("moveProduceExamEffectIds")
    move_triggers = raw.get("moveProduceExamTriggerIds")
    status_id = raw.get("produceCardStatusEnchantId")
    if type(move_trigger) is not str or not move_trigger:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"card:{card_id}: moveEffectTriggerType shape changed"
        )
    if not isinstance(move_effects, list) or any(type(value) is not str for value in move_effects):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"card:{card_id}: moveProduceExamEffectIds shape changed"
        )
    if not isinstance(move_triggers, list) or any(type(value) is not str for value in move_triggers):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"card:{card_id}: moveProduceExamTriggerIds shape changed"
        )
    if type(status_id) is not str:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"card:{card_id}: produceCardStatusEnchantId shape changed"
        )
    _required_raw_equal(raw, "id", card_id, f"card:{card_id}.raw_json")
    return tuple(groups), move_trigger, tuple(move_effects), tuple(move_triggers), status_id


def _play_trigger_from_card(
    connection: sqlite3.Connection, trigger_id: str
) -> Plan2ReviewMultipleTriggerRow | None:
    if not trigger_id:
        return None
    trigger = _load_trigger(connection, trigger_id)
    # This is the exact only current ReviewMultiple card-level gate.  The
    # adapter does not infer a gate from text or from a missing trigger row.
    if not (
        trigger.phase_types == (_PHASE_NONE,)
        and not trigger.phase_values
        and not trigger.field_status_check_types
        and trigger.field_status_types == (_FIELD_REVIEW_UP,)
        and trigger.field_status_values in {(6,), (10,)}
        and not trigger.field_status_produce_card_search_ids
        and trigger.produce_card_search_id == ""
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == _MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == _LESSON_UNKNOWN
    ):
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"card play trigger {trigger_id} is outside exact ReviewMultiple gate shape"
        )
    return trigger


def _companion_blockers(
    card_ref: str,
    slots: Sequence[Plan2ReviewMultipleEffectSlot],
    statuses: Sequence[Plan2ReviewMultipleStatusRow],
    target_index: int,
) -> tuple[Plan2ReviewMultipleBlocker, ...]:
    status_by_wrapper = {value.wrapper_effect_id: value for value in statuses}
    blockers: list[Plan2ReviewMultipleBlocker] = []
    for slot in slots:
        if slot.slot_index == target_index:
            continue
        row = slot.row
        detail = (
            f"slot={slot.slot_index};effect={row.effect_id};type={row.effect_type};"
            f"value1={row.value1};value2={row.value2};count={row.effect_count};turn={row.effect_turn}"
        )
        if row.effect_type in {_EFFECT_REVIEW, _EFFECT_BLOCK, _EFFECT_PLAYABLE_ADD}:
            # These are already represented by the shared scalar/direct core
            # boundary; retain their exact rows but do not duplicate ownership.
            continue
        if row.effect_type == _EFFECT_STAMINA_ADD:
            blockers.append(
                Plan2ReviewMultipleBlocker(
                    "companion-adapter-pending",
                    card_ref,
                    "stamina_consumption_add",
                    detail,
                    row.effect_id,
                )
            )
            continue
        if row.effect_type == _EFFECT_ADD_GROW:
            blockers.append(
                Plan2ReviewMultipleBlocker(
                    "companion-adapter-pending",
                    card_ref,
                    "add_grow_effect",
                    detail,
                    row.effect_id,
                )
            )
            continue
        if row.effect_type == _EFFECT_STATUS_ENCHANT:
            status = status_by_wrapper.get(row.effect_id)
            if status is None:
                blockers.append(
                    Plan2ReviewMultipleBlocker(
                        "companion-status-shape-invalid",
                        card_ref,
                        "status_enchant",
                        detail,
                        row.effect_id,
                    )
                )
                continue
            family = (
                "start_play_trigger"
                if status.trigger.trigger_id == "e_trigger-start_play"
                else "status_change_review"
                if status.trigger.trigger_id == "e_trigger-exam_status_change-exam_review"
                else "status_enchant"
            )
            blockers.append(
                Plan2ReviewMultipleBlocker(
                    "companion-adapter-pending",
                    card_ref,
                    family,
                    detail
                    + f";status={status.status_enchant_id};trigger={status.trigger.trigger_id};"
                    + f"children={','.join(status.child_effect_ids)}",
                    row.effect_id,
                )
            )
            continue
        blockers.append(
            Plan2ReviewMultipleBlocker(
                "companion-effect-unbound",
                card_ref,
                "unknown_companion",
                detail,
                row.effect_id,
            )
        )
    return tuple(blockers)


def _compile_card(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    database: Path,
) -> Plan2NativeReviewMultipleProgram:
    card_id = _nonempty(row["id"], "card.id")
    upgrade = _plain_int(row["upgrade_count"], f"{card_id}.upgrade", minimum=0)
    ref = f"{card_id}#{upgrade}"
    if row["plan_type"] != _PLAN2:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{ref}: target card is not Plan2"
        )
    groups, move_trigger, move_effects, move_triggers, card_status = _card_raw_fields(row, card_id)
    links = _json_list(row["play_effects_json"], f"{ref}.play_effects_json")
    slots = tuple(
        _effect_slot_from_link(connection, index, link)
        for index, link in enumerate(links)
    )
    target_indices = tuple(
        slot.slot_index for slot in slots if slot.effect_type == REVIEW_MULTIPLE_EFFECT_TYPE
    )
    if len(target_indices) != 1:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{ref}: expected exactly one ReviewMultiple slot, got {target_indices}"
        )
    target_index = target_indices[0]
    expected_effect_id = EXPECTED_REVIEW_MULTIPLE_VERSION_EFFECTS.get((card_id, upgrade))
    if expected_effect_id is None:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{ref}: unexpected ReviewMultiple affected version"
        )
    target_slot = slots[target_index]
    if target_slot.effect_id != expected_effect_id:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{ref}: target effect changed; expected {expected_effect_id}, got {target_slot.effect_id}"
        )
    if target_slot.trigger_id or target_slot.hide_icon or target_slot.is_once_play_effect:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{ref}: target slot link shape changed"
        )
    normalized_target, native_target = _native_review_effect(expected_effect_id, database)
    if normalized_target.effect_id != target_slot.effect_id:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{ref}: target normalized effect mismatch"
        )
    statuses = tuple(
        _status_row_from_slot(connection, slot)
        for slot in slots
        if slot.effect_type == _EFFECT_STATUS_ENCHANT
    )
    blockers = _companion_blockers(ref, slots, statuses, target_index)
    play_trigger_id = _text(row["play_trigger_id"], f"{ref}.play_trigger_id")
    play_trigger = _play_trigger_from_card(connection, play_trigger_id)
    return Plan2NativeReviewMultipleProgram(
        Plan2ReviewMultipleCardVersion(
            card_id=card_id,
            upgrade=upgrade,
            plan_type=str(row["plan_type"]),
            category=_nonempty(row["category"], f"{ref}.category"),
            stamina=_plain_int(row["stamina"], f"{ref}.stamina", minimum=0),
            cost_type=_nonempty(row["cost_type"], f"{ref}.cost_type"),
            cost_value=_plain_int(row["cost_value"], f"{ref}.cost_value", minimum=0),
            play_trigger_id=play_trigger_id,
            play_trigger=play_trigger,
            move_position_type=_nonempty(row["move_position_type"], f"{ref}.move_position_type"),
            effect_group_ids=groups,
            move_effect_trigger_type=move_trigger,
            move_effect_ids=move_effects,
            move_trigger_ids=move_triggers,
            card_status_enchant_id=card_status,
            effect_slots=slots,
            review_multiple_slot_index=target_index,
            review_multiple_effect=native_target,
            status_rows=statuses,
            companion_blockers=blockers,
        )
    )


def compile_plan2_native_catalog_review_multiple(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeReviewMultipleCatalogCompilation:
    """Compile exactly the current-Master ReviewMultiple affected versions.

    Hard target-shape errors are recorded against their version and never emit
    a partial program.  Companion rows are retained exactly and reported as
    handoff blockers on an otherwise executable target-family program.
    """

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT DISTINCT c.*
            FROM card AS c
            JOIN json_each(c.play_effects_json) AS link
            JOIN effect AS e
              ON e.id = json_extract(link.value, '$.produceExamEffectId')
            WHERE c.plan_type = ?
              AND e.effect_type = ?
            ORDER BY c.id, c.upgrade_count
            """,
            (_PLAN2, REVIEW_MULTIPLE_EFFECT_TYPE),
        ).fetchall()
        discovered_refs = tuple((str(row["id"]), int(row["upgrade_count"])) for row in rows)
        expected_refs = tuple(sorted(EXPECTED_REVIEW_MULTIPLE_VERSION_EFFECTS))
        if discovered_refs != expected_refs:
            raise Plan2NativeCatalogReviewMultipleContractError(
                "ReviewMultiple affected version inventory changed: "
                f"expected {expected_refs!r}, got {discovered_refs!r}"
            )
        programs: list[Plan2NativeReviewMultipleProgram] = []
        blockers: list[Plan2ReviewMultipleBlocker] = []
        for row in rows:
            ref = f"{row['id']}#{row['upgrade_count']}"
            try:
                program = _compile_card(connection, row, database)
            except (Plan2NativeCatalogReviewMultipleContractError, Plan2ReviewMultipleContractError) as error:
                blockers.append(
                    Plan2ReviewMultipleBlocker(
                        "review-multiple-shape-invalid",
                        ref,
                        "review_multiple",
                        f"{type(error).__name__}: {error}",
                    )
                )
            else:
                programs.append(program)
    catalog = Plan2NativeReviewMultipleCatalog(str(database), tuple(programs))
    return Plan2NativeReviewMultipleCatalogCompilation(
        PLAN2_NATIVE_CATALOG_REVIEW_MULTIPLE_SCHEMA_VERSION,
        str(database),
        expected_refs,
        catalog,
        tuple(blockers),
    )


compile_plan2_review_multiple_catalog = compile_plan2_native_catalog_review_multiple
compile_review_multiple_catalog = compile_plan2_native_catalog_review_multiple


def load_plan2_native_catalog_review_multiple(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeReviewMultipleCatalog:
    """Load a complete exact catalog or fail closed on any hard row blocker."""

    compilation = compile_plan2_native_catalog_review_multiple(database)
    if compilation.blockers:
        first = compilation.blockers[0]
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"{first.version_ref}: {first.detail}"
        )
    if compilation.affected_version_count != REVIEW_MULTIPLE_TARGET_VERSION_COUNT:
        raise Plan2NativeCatalogReviewMultipleContractError(
            "unexpected ReviewMultiple target version count"
        )
    return compilation.catalog


load_plan2_review_multiple_catalog = load_plan2_native_catalog_review_multiple


def install_plan2_native_review_multiple(
    state: Plan2State,
    program: Plan2NativeReviewMultipleProgram,
    *,
    play_origin: Plan2ReviewMultiplePlayOrigin | str = Plan2ReviewMultiplePlayOrigin.ORDINARY,
) -> Plan2ReviewMultipleInstallTransition:
    """Install one compiled direct effect through the proven immutable primitive."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2NativeReviewMultipleProgram):
        raise TypeError("program must be Plan2NativeReviewMultipleProgram")
    try:
        origin = Plan2ReviewMultiplePlayOrigin(play_origin)
    except (TypeError, ValueError) as error:
        raise Plan2NativeCatalogReviewMultipleContractError(
            f"unsupported play origin: {play_origin!r}"
        ) from error
    native = simulate_review_multiple(state, program.effect)
    before_uids = tuple(value.status_uid for value in state.review_multiple_layers)
    after_uids = tuple(value.status_uid for value in native.after.review_multiple_layers)
    uid = native.created_status_uid or native.merged_status_uid
    if uid is None:
        raise AssertionError("ReviewMultiple install did not create or merge a status")
    trace = (
        f"card-play:{origin.value}:direct-slot:{program.slot_index}:ReviewMultiple:execute",
        f"status:ReviewMultiple:uid={uid}:turn={program.effect.turn}:permil={program.effect.permil}",
        f"status-stack:before={before_uids}:after={after_uids}",
    )
    return Plan2ReviewMultipleInstallTransition(
        state,
        native.after,
        program,
        origin,
        native,
        before_uids,
        after_uids,
        trace,
    )


install_plan2_review_multiple = install_plan2_native_review_multiple
install_review_multiple_program = install_plan2_native_review_multiple


def simulate_plan2_native_review_multiple_turn_start(
    state: Plan2State,
) -> Plan2ReviewMultipleTurnStartTransition:
    """Advance shared TurnStart and expose only ReviewMultiple lifecycle IDs."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    native = simulate_plan2_turn_start(state)
    before = tuple(value.status_uid for value in state.review_multiple_layers)
    after = tuple(value.status_uid for value in native.after.review_multiple_layers)
    target = set(before) | set(after)
    return Plan2ReviewMultipleTurnStartTransition(
        state,
        native.after,
        native,
        before,
        after,
        tuple(uid for uid in native.fresh_status_uids if uid in target),
        tuple(uid for uid in native.spent_status_uids if uid in target),
        tuple(uid for uid in native.expired_status_uids if uid in target),
        tuple(uid for uid in native.permanent_status_uids if uid in target),
    )


simulate_review_multiple_turn_start = simulate_plan2_native_review_multiple_turn_start
advance_plan2_review_multiple_turn_start = simulate_plan2_native_review_multiple_turn_start


def simulate_plan2_native_review_multiple_turn_end(
    state: Plan2State,
    *,
    apply_score: Plan2ScoreApplicator | None = None,
) -> Plan2ReviewMultipleTurnEndTransition:
    """Run native end-turn Review scoring with one typed score bridge."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    kwargs: dict[str, object] = {}
    if apply_score is not None:
        kwargs["apply_score"] = apply_score
    native = simulate_end_turn_review_score(state, **kwargs)  # type: ignore[arg-type]
    return Plan2ReviewMultipleTurnEndTransition(state, native.after, native)


simulate_review_multiple_turn_end = simulate_plan2_native_review_multiple_turn_end
advance_plan2_review_multiple_turn_end = simulate_plan2_native_review_multiple_turn_end


def build_plan2_review_multiple_central_handoff(
    source: Plan2NativeReviewMultipleCatalogCompilation | Plan2NativeReviewMultipleCatalog,
) -> Plan2ReviewMultipleCentralHandoff:
    """Build an exact central handoff without calling central runtime code."""

    if isinstance(source, Plan2NativeReviewMultipleCatalogCompilation):
        if source.blockers:
            first = source.blockers[0]
            raise Plan2NativeCatalogReviewMultipleContractError(
                f"cannot hand off incomplete ReviewMultiple catalog: {first.version_ref}: {first.detail}"
            )
        catalog = source.catalog
    elif isinstance(source, Plan2NativeReviewMultipleCatalog):
        catalog = source
    else:
        raise TypeError("source must be ReviewMultiple catalog or compilation")
    rows = tuple(
        Plan2ReviewMultipleHandoffRow(
            version_ref=program.card.version_ref,
            card_id=program.card.card_id,
            upgrade=program.card.upgrade,
            target_effect_id=program.effect.effect_id,
            target_slot_index=program.slot_index,
            value1_permille=program.effect.permil,
            value2=program.card.review_multiple_slot.value2,
            count=program.card.review_multiple_slot.effect_count,
            turn=program.effect.turn,
            lifetime="permanent" if program.effect.is_permanent else "finite",
            ordered_effect_ids=program.ordered_effect_ids,
            status_enchant_ids=tuple(value.status_enchant_id for value in program.card.status_rows),
            supported_play_origins=program.play_origins,
            target_effect_executable=program.card.target_executable,
            whole_card_executable=program.card.whole_card_executable,
            companion_blockers=program.card.companion_blockers,
        )
        for program in catalog.programs
    )
    return Plan2ReviewMultipleCentralHandoff(
        REVIEW_MULTIPLE_ADAPTER_ID,
        PLAN2_NATIVE_CATALOG_REVIEW_MULTIPLE_SCHEMA_VERSION,
        catalog.affected_refs,
        catalog.executable_refs,
        rows,
    )


build_plan2_native_review_multiple_handoff = build_plan2_review_multiple_central_handoff
build_review_multiple_central_handoff = build_plan2_review_multiple_central_handoff


def exact_plan2_native_review_multiple_rows(
    source: Plan2NativeReviewMultipleCatalogCompilation | Plan2NativeReviewMultipleCatalog,
) -> tuple[dict[str, object], ...]:
    """Return immutable catalog rows suitable for audit/central ingestion."""

    handoff = build_plan2_review_multiple_central_handoff(source)
    return tuple(row.to_dict() for row in handoff.rows)


def review_multiple_catalog_summary(
    source: Plan2NativeReviewMultipleCatalogCompilation | Plan2NativeReviewMultipleCatalog,
) -> dict[str, object]:
    compilation = (
        source
        if isinstance(source, Plan2NativeReviewMultipleCatalogCompilation)
        else None
    )
    handoff = build_plan2_review_multiple_central_handoff(source)
    return {
        "affected_versions": handoff.affected_version_count,
        "compiled_versions": (
            compilation.compiled_version_count
            if compilation is not None
            else handoff.executable_version_count
        ),
        "failed_versions": compilation.failed_version_count if compilation is not None else 0,
        "target_effect_executable_versions": handoff.executable_version_count,
        "companion_blocked_versions": len(handoff.companion_blocked_refs),
        "whole_card_executable_versions": len(handoff.whole_card_executable_refs),
        "affected_refs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.affected_refs],
        "target_effect_executable_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in handoff.executable_refs
        ],
        "companion_blocked_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in handoff.companion_blocked_refs
        ],
        "whole_card_executable_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in handoff.whole_card_executable_refs
        ],
        "companion_blocker_code_counts": (
            compilation.companion_blocker_code_counts
            if compilation is not None
            else dict(
                sorted(
                    Counter(
                        blocker.code
                        for row in handoff.rows
                        for blocker in row.companion_blockers
                    ).items()
                )
            )
        ),
        "companion_family_version_counts": (
            compilation.companion_family_version_counts
            if compilation is not None
            else {}
        ),
    }


__all__ = [
    "ANDROID_REVIEW_MULTIPLE_EVIDENCE",
    "EXPECTED_REVIEW_MULTIPLE_VERSION_EFFECTS",
    "PLAY_ORIGINS",
    "Plan2NativeCatalogReviewMultipleBlocker",
    "Plan2NativeCatalogReviewMultipleContractError",
    "Plan2NativeReviewMultipleBlocker",
    "Plan2NativeReviewMultipleCatalog",
    "Plan2NativeReviewMultipleCatalogCompilation",
    "Plan2NativeReviewMultipleContractError",
    "Plan2NativeReviewMultipleProgram",
    "Plan2ReviewMultipleCardVersion",
    "Plan2ReviewMultipleCatalog",
    "Plan2ReviewMultipleCatalogCompilation",
    "Plan2ReviewMultipleCentralHandoff",
    "Plan2ReviewMultipleEffectRow",
    "Plan2ReviewMultipleEffectSlot",
    "Plan2ReviewMultipleHandoffRow",
    "Plan2ReviewMultipleInstallTransition",
    "Plan2ReviewMultiplePlayOrigin",
    "Plan2ReviewMultipleStatusRow",
    "Plan2ReviewMultipleTriggerRow",
    "Plan2ReviewMultipleTurnEndTransition",
    "Plan2ReviewMultipleTurnStartTransition",
    "PLAN2_NATIVE_CATALOG_REVIEW_MULTIPLE_SCHEMA_VERSION",
    "REVIEW_MULTIPLE_ADAPTER_ID",
    "REVIEW_MULTIPLE_CATALOG_SCHEMA_VERSION",
    "REVIEW_MULTIPLE_EFFECT_TYPE",
    "REVIEW_MULTIPLE_TARGET_VERSION_COUNT",
    "advance_plan2_review_multiple_turn_end",
    "advance_plan2_review_multiple_turn_start",
    "build_plan2_native_review_multiple_handoff",
    "build_plan2_review_multiple_central_handoff",
    "build_review_multiple_central_handoff",
    "compile_plan2_native_catalog_review_multiple",
    "compile_plan2_review_multiple_catalog",
    "compile_review_multiple_catalog",
    "exact_plan2_native_review_multiple_rows",
    "install_plan2_native_review_multiple",
    "install_plan2_review_multiple",
    "install_review_multiple_program",
    "load_plan2_native_catalog_review_multiple",
    "load_plan2_review_multiple_catalog",
    "review_multiple_catalog_summary",
    "simulate_plan2_native_review_multiple_turn_end",
    "simulate_plan2_native_review_multiple_turn_start",
    "simulate_review_multiple_turn_end",
    "simulate_review_multiple_turn_start",
]
