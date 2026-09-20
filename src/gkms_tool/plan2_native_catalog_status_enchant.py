"""Exact Plan2 owner for native-catalog ``ExamStatusEnchant`` installers.

The native program catalog currently leaves 100 direct installer occurrences
across 99 Common/Plan2 card versions unbound.  This module compiles that slice
directly from Master without importing the catalog or coverage layers.  It
owns only listener installation and lifecycle; nested effects remain ordered,
immutable handoffs for their downstream Plan2 owners.

Android v3.2.3 ``StatusEnchantEffectExecutor`` semantics represented here:

* every successful install appends a distinct listener/UID;
* ``effectCount > 0`` is the total fire limit, otherwise unlimited;
* ``effectValue1 > 0`` is the per-turn fire limit, otherwise unlimited;
* finite turns are fresh at install and are spent at later StartTurn bounds;
* listener-local interval counters advance only for matching accepted cards;
* count is spent when ordered child commands are queued, before execution;
* exhausted card-direct listeners and expired finite listeners are removed.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .exam_status_runtime import (
    ExamStatusTrigger,
    RuntimeExamEffect,
    load_runtime_status_enchant,
)
from .logic_engine import PLAN_COMMON, PLAN_LOGIC
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX
from .plan2_end_turn_remaining_three import (
    DIRECT_TARGETS as END_TURN_REMAINING_THREE_DIRECT_TARGETS,
)


PLAN2_NATIVE_STATUS_ENCHANT_SCHEMA_VERSION = 1
EXPECTED_AFFECTED_VERSION_COUNT = 99
EXPECTED_INSTALLER_OCCURRENCE_COUNT = 100
EXPECTED_INSTALLER_EFFECT_COUNT = 39
EXPECTED_STATUS_ENCHANT_COUNT = 36
EXPECTED_TRIGGER_COUNT = 20

EFFECT_STATUS_ENCHANT = "ProduceExamEffectType_ExamStatusEnchant"
EFFECT_LESSON_DEPEND_AGGRESSIVE = (
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive"
)

PHASE_END_TURN = "ProduceExamPhaseType_ExamEndTurn"
PHASE_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
PHASE_START_PLAY = "ProduceExamPhaseType_StartPlay"
PHASE_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
PHASE_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"
PHASE_PLAY_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
PHASE_TURN_TIMER = "ProduceExamPhaseType_ExamTurnTimer"
PHASE_STAMINA_REDUCE_CARD = "ProduceExamPhaseType_ExamStaminaReduceCard"

SUPPORTED_PHASES = frozenset(
    {
        PHASE_END_TURN,
        PHASE_START_TURN,
        PHASE_START_PLAY,
        PHASE_CARD_PLAY,
        PHASE_CARD_PLAY_AFTER,
        PHASE_STATUS_CHANGE,
        PHASE_PLAY_COUNT_INTERVAL,
        PHASE_STAMINA_REDUCE_CARD,
    }
)
RUNTIME_SUPPORTED_PHASES = frozenset((*SUPPORTED_PHASES, PHASE_TURN_TIMER))
INTERVAL_PHASES = frozenset(
    {PHASE_PLAY_COUNT_INTERVAL, PHASE_TURN_TIMER}
)
PLAY_PHASES = frozenset(
    {PHASE_CARD_PLAY, PHASE_CARD_PLAY_AFTER, PHASE_PLAY_COUNT_INTERVAL}
)
PLAY_ORIGINS = frozenset({"normal", "forced", "extra"})

FIELD_REVIEW_UP = "ProduceExamFieldStatusType_ReviewUp"
FIELD_AGGRESSIVE_UP = "ProduceExamFieldStatusType_CardPlayAggressiveUp"
FIELD_REMAINING_TURN = "ProduceExamFieldStatusType_RemainingTurn"
FIELD_NO_BLOCK = "ProduceExamFieldStatusType_NoBlock"
FIELD_STAMINA_LESS_MULTIPLE = (
    "ProduceExamFieldStatusType_StaminaLessMultiple"
)
SUPPORTED_FIELD_TYPES = frozenset(
    {
        FIELD_REVIEW_UP,
        FIELD_AGGRESSIVE_UP,
        FIELD_REMAINING_TURN,
        FIELD_NO_BLOCK,
        FIELD_STAMINA_LESS_MULTIPLE,
    }
)

POSITION_TARGET = "ProduceCardPositionType_Target"
POSITION_PLAYING = "ProduceCardPositionType_Playing"
POSITION_UNKNOWN = "ProduceCardMovePositionType_Unknown"
PLAN_UNKNOWN = "ProducePlanType_Unknown"
STATUS_UNKNOWN = "ProduceCardSearchStatusType_Unknown"
ORDER_UNKNOWN = "ProduceCardOrderType_Unknown"
MIN_MAX_UNKNOWN = "ConditionMinMaxType_Unknown"
EXAM_EFFECT_UNKNOWN = "ProduceExamEffectType_Unknown"
COST_UNKNOWN = "ExamCostType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"

SUPPORTED_CHILD_EFFECT_TYPES = frozenset(
    {
        "ProduceExamEffectType_ExamLessonDependBlock",
        "ProduceExamEffectType_ExamLessonDependExamReview",
        "ProduceExamEffectType_ExamReview",
        "ProduceExamEffectType_ExamBlock",
        "ProduceExamEffectType_ExamStaminaRecoverMultiple",
        "ProduceExamEffectType_ExamBlockDependBlockConsumptionSum",
        "ProduceExamEffectType_ExamCardMove",
        "ProduceExamEffectType_ExamCardDraw",
        "ProduceExamEffectType_ExamLesson",
        "ProduceExamEffectType_ExamCardCreateId",
        "ProduceExamEffectType_ExamReviewCountAdd",
        "ProduceExamEffectType_ExamForcePlayCardSearch",
        "ProduceExamEffectType_ExamBlockAddMultipleAggressive",
        "ProduceExamEffectType_ExamCardPlayAggressive",
        EFFECT_LESSON_DEPEND_AGGRESSIVE,
    }
)


class Plan2NativeStatusEnchantError(ValueError):
    """Fail-closed contract/runtime error with a stable diagnostic code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise Plan2NativeStatusEnchantError("invalid-text", label)
    return value


def _i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2NativeStatusEnchantError("invalid-int32", label)
    if not -(2**31) <= value <= INT32_MAX:
        raise Plan2NativeStatusEnchantError("int32-out-of-range", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    result = _i32(value, label)
    if result < 0:
        raise Plan2NativeStatusEnchantError("negative-value", label)
    return result


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeStatusEnchantError("invalid-json", label) from error
    if not isinstance(result, Mapping):
        raise Plan2NativeStatusEnchantError("invalid-json-object", label)
    return result


def _json_array(value: object, label: str) -> tuple[object, ...]:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeStatusEnchantError("invalid-json", label) from error
    if not isinstance(result, list):
        raise Plan2NativeStatusEnchantError("invalid-json-array", label)
    return tuple(result)


def _string_array(value: object, label: str) -> tuple[str, ...]:
    result = _json_array(value, label)
    if any(not isinstance(item, str) or not item for item in result):
        raise Plan2NativeStatusEnchantError("invalid-string-array", label)
    return tuple(result)  # type: ignore[return-value]


def _integer_array(value: object, label: str) -> tuple[int, ...]:
    result = _json_array(value, label)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in result):
        raise Plan2NativeStatusEnchantError("invalid-integer-array", label)
    return tuple(result)  # type: ignore[return-value]


def _raw_equal(
    raw: Mapping[str, object], expected: Mapping[str, object], label: str
) -> None:
    for key, value in expected.items():
        if key not in raw or raw[key] != value:
            raise Plan2NativeStatusEnchantError(
                "master-raw-drift", f"{label}.{key}"
            )


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeStatusEnchantBlocker:
    card_id: str
    upgrade: int
    slot_index: int
    effect_id: str
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.card_id, "blocker.card_id")
        _nonnegative(self.upgrade, "blocker.upgrade")
        _nonnegative(self.slot_index, "blocker.slot_index")
        _text(self.effect_id, "blocker.effect_id", empty=True)
        _text(self.code, "blocker.code")
        _text(self.detail, "blocker.detail", empty=True)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantChildProgram:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    status_enchant_id: str
    chain_effect_id: str
    chain_effect_ids: tuple[str, ...]
    target_card_id: str
    target_upgrade: int
    target_effect_type: str
    card_search_id: str
    move_position_type: str
    card_search_id2: str
    grow_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.effect_id, "child.effect_id")
        _text(self.effect_type, "child.effect_type")
        for name in ("value1", "value2", "count", "turn", "target_upgrade"):
            _i32(getattr(self, name), f"child.{name}")
        for name in (
            "status_enchant_id",
            "chain_effect_id",
            "target_card_id",
            "target_effect_type",
            "card_search_id",
            "move_position_type",
            "card_search_id2",
        ):
            _text(getattr(self, name), f"child.{name}", empty=True)
        for name in ("chain_effect_ids", "grow_effect_ids", "effect_group_ids"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values):
                raise Plan2NativeStatusEnchantError(
                    "invalid-string-array", f"child.{name}"
                )
            object.__setattr__(self, name, values)
        if self.effect_type not in SUPPORTED_CHILD_EFFECT_TYPES:
            raise Plan2NativeStatusEnchantError(
                "child-effect-type-unbound", self.effect_type
            )
        if self.status_enchant_id or self.chain_effect_id or self.chain_effect_ids:
            raise Plan2NativeStatusEnchantError(
                "nested-child-runtime-unbound", self.effect_id
            )


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantTriggerProgram:
    trigger_id: str
    phase: str
    phase_values: tuple[int, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    effect_types: tuple[str, ...]
    card_search: ProduceCardSearchRule | None

    def __post_init__(self) -> None:
        _text(self.trigger_id, "trigger_id")
        if self.phase not in RUNTIME_SUPPORTED_PHASES:
            raise Plan2NativeStatusEnchantError(
                "trigger-phase-unbound", self.phase
            )
        for name in ("phase_values", "field_status_values"):
            values = tuple(getattr(self, name))
            for index, value in enumerate(values):
                _i32(value, f"trigger.{name}[{index}]")
            object.__setattr__(self, name, values)
        for name in ("field_status_types", "effect_types"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values):
                raise Plan2NativeStatusEnchantError(
                    "invalid-string-array", f"trigger.{name}"
                )
            object.__setattr__(self, name, values)
        if any(value not in SUPPORTED_FIELD_TYPES for value in self.field_status_types):
            raise Plan2NativeStatusEnchantError(
                "trigger-field-status-unbound", self.trigger_id
            )
        if len(self.field_status_types) > 1:
            raise Plan2NativeStatusEnchantError(
                "trigger-multiple-field-status-unbound", self.trigger_id
            )
        expected_values = 0 if self.field_status_types == (FIELD_NO_BLOCK,) else len(
            self.field_status_types
        )
        if len(self.field_status_values) != expected_values:
            raise Plan2NativeStatusEnchantError(
                "trigger-field-value-shape", self.trigger_id
            )
        if self.phase in INTERVAL_PHASES:
            if len(self.phase_values) != 1 or self.phase_values[0] <= 0:
                raise Plan2NativeStatusEnchantError(
                    "trigger-interval-shape", self.trigger_id
                )
        elif self.phase == PHASE_STATUS_CHANGE:
            if len(self.phase_values) > 1 or (
                self.phase_values and self.phase_values[0] <= 0
            ):
                raise Plan2NativeStatusEnchantError(
                    "trigger-status-change-threshold-shape", self.trigger_id
                )
        elif self.phase_values:
            raise Plan2NativeStatusEnchantError(
                "trigger-phase-values-unbound", self.trigger_id
            )
        if self.phase == PHASE_STATUS_CHANGE:
            if len(self.effect_types) != 1:
                raise Plan2NativeStatusEnchantError(
                    "trigger-status-change-effect-shape", self.trigger_id
                )
        elif self.effect_types:
            raise Plan2NativeStatusEnchantError(
                "trigger-effect-filter-unbound", self.trigger_id
            )
        if self.card_search is not None and self.phase not in PLAY_PHASES:
            raise Plan2NativeStatusEnchantError(
                "trigger-search-phase-unbound", self.trigger_id
            )

    @property
    def interval(self) -> int | None:
        if self.phase not in INTERVAL_PHASES:
            return None
        return self.phase_values[0]


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantInstallProgram:
    card_id: str
    upgrade: int
    slot_index: int
    ordered_card_effect_ids: tuple[str, ...]
    installer_effect_id: str
    status_enchant_id: str
    turn: int
    total_limit: int
    per_turn_limit: int
    trigger: Plan2StatusEnchantTriggerProgram
    children: tuple[Plan2StatusEnchantChildProgram, ...]

    def __post_init__(self) -> None:
        _text(self.card_id, "program.card_id")
        _nonnegative(self.upgrade, "program.upgrade")
        _nonnegative(self.slot_index, "program.slot_index")
        _text(self.installer_effect_id, "program.installer_effect_id")
        _text(self.status_enchant_id, "program.status_enchant_id")
        _i32(self.turn, "program.turn")
        _i32(self.total_limit, "program.total_limit")
        _i32(self.per_turn_limit, "program.per_turn_limit")
        if self.turn == 0 or self.turn < -1:
            raise Plan2NativeStatusEnchantError(
                "installer-turn-shape", self.installer_effect_id
            )
        if self.total_limit == 0 or self.total_limit < -1:
            raise Plan2NativeStatusEnchantError(
                "installer-total-limit-shape", self.installer_effect_id
            )
        if self.per_turn_limit == 0 or self.per_turn_limit < -1:
            raise Plan2NativeStatusEnchantError(
                "installer-per-turn-limit-shape", self.installer_effect_id
            )
        ordered = tuple(self.ordered_card_effect_ids)
        if self.slot_index >= len(ordered) or ordered[self.slot_index] != self.installer_effect_id:
            raise Plan2NativeStatusEnchantError(
                "installer-card-order-drift", self.installer_effect_id
            )
        if not isinstance(self.trigger, Plan2StatusEnchantTriggerProgram):
            raise TypeError("trigger must be Plan2StatusEnchantTriggerProgram")
        children = tuple(self.children)
        if not children or any(
            not isinstance(child, Plan2StatusEnchantChildProgram)
            for child in children
        ):
            raise Plan2NativeStatusEnchantError(
                "status-children-shape", self.status_enchant_id
            )
        object.__setattr__(self, "ordered_card_effect_ids", ordered)
        object.__setattr__(self, "children", children)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_card_effect_ids[: self.slot_index]

    @property
    def permanent(self) -> bool:
        return self.turn == -1


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantVersionProgram:
    card_id: str
    upgrade: int
    ordered_card_effect_ids: tuple[str, ...]
    installs: tuple[Plan2StatusEnchantInstallProgram, ...]

    def __post_init__(self) -> None:
        _text(self.card_id, "version.card_id")
        _nonnegative(self.upgrade, "version.upgrade")
        ordered = tuple(self.ordered_card_effect_ids)
        installs = tuple(self.installs)
        if not installs or any(
            not isinstance(item, Plan2StatusEnchantInstallProgram)
            for item in installs
        ):
            raise Plan2NativeStatusEnchantError(
                "version-installs-empty", f"{self.card_id}#{self.upgrade}"
            )
        if any(item.ref != self.ref for item in installs):
            raise Plan2NativeStatusEnchantError(
                "version-install-ref-drift", f"{self.card_id}#{self.upgrade}"
            )
        if tuple(sorted(item.slot_index for item in installs)) != tuple(
            item.slot_index for item in installs
        ):
            raise Plan2NativeStatusEnchantError(
                "version-install-order-drift", f"{self.card_id}#{self.upgrade}"
            )
        object.__setattr__(self, "ordered_card_effect_ids", ordered)
        object.__setattr__(self, "installs", installs)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    def install_at(self, slot_index: int) -> Plan2StatusEnchantInstallProgram:
        for item in self.installs:
            if item.slot_index == slot_index:
                return item
        raise KeyError(f"status installer not found: {self.card_id}#{self.upgrade}@{slot_index}")


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantCatalog:
    programs: tuple[Plan2NativeStatusEnchantVersionProgram, ...]

    def __post_init__(self) -> None:
        programs = tuple(self.programs)
        refs = tuple(item.ref for item in programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeStatusEnchantError("catalog-version-order")
        object.__setattr__(self, "programs", programs)

    def version(
        self, card_id: str, upgrade: int
    ) -> Plan2NativeStatusEnchantVersionProgram:
        for program in self.programs:
            if program.ref == (card_id, upgrade):
                return program
        raise KeyError(f"status-enchant version not found: {card_id}#{upgrade}")


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    catalog: Plan2NativeStatusEnchantCatalog
    blockers: tuple[Plan2NativeStatusEnchantBlocker, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_STATUS_ENCHANT_SCHEMA_VERSION:
            raise Plan2NativeStatusEnchantError("schema-version")
        _text(self.database, "compilation.database")
        refs = tuple(self.affected_refs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeStatusEnchantError("affected-ref-order")
        _nonnegative(self.occurrence_count, "compilation.occurrence_count")
        if not isinstance(self.catalog, Plan2NativeStatusEnchantCatalog):
            raise TypeError("catalog must be Plan2NativeStatusEnchantCatalog")
        blockers = tuple(self.blockers)
        if any(not isinstance(item, Plan2NativeStatusEnchantBlocker) for item in blockers):
            raise TypeError("blockers contain the wrong type")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", blockers)

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.catalog.programs)

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.affected_refs if ref not in compiled)

    @property
    def compiled_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def compiled_occurrence_count(self) -> int:
        return sum(
            len(program.installs) for program in self.catalog.programs
        )

    @property
    def failed_occurrence_count(self) -> int:
        return self.occurrence_count - self.compiled_occurrence_count

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers and not self.failed_refs

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.blockers).items()))

    def blockers_for(
        self, ref: tuple[str, int]
    ) -> tuple[Plan2NativeStatusEnchantBlocker, ...]:
        return tuple(item for item in self.blockers if item.ref == ref)


def _validate_parent_effect(
    row: Mapping[str, object], effect_id: str
) -> tuple[str, int, int, int]:
    effect_type = str(row["effect_type"])
    value1 = _i32(row["value1"], f"{effect_id}.value1")
    value2 = _i32(row["value2"], f"{effect_id}.value2")
    count = _i32(row["effect_count"], f"{effect_id}.count")
    turn = _i32(row["effect_turn"], f"{effect_id}.turn")
    status_id = str(row["status_enchant_id"])
    chain_id = str(row["chain_effect_id"])
    if effect_type != EFFECT_STATUS_ENCHANT:
        raise Plan2NativeStatusEnchantError(
            "installer-effect-type-drift", effect_id
        )
    if value2 != 0 or count < 0 or value1 < 0 or turn == 0 or turn < -1:
        raise Plan2NativeStatusEnchantError(
            "installer-value-shape", effect_id
        )
    if not status_id or chain_id:
        raise Plan2NativeStatusEnchantError(
            "installer-reference-shape", effect_id
        )
    raw = _json_object(row["raw_json"], effect_id)
    _raw_equal(
        raw,
        {
            "id": effect_id,
            "effectType": EFFECT_STATUS_ENCHANT,
            "effectValue1": value1,
            "effectValue2": value2,
            "effectCount": count,
            "effectTurn": turn,
            "targetProduceCardId": "",
            "targetUpgradeCount": 0,
            "targetExamEffectType": EXAM_EFFECT_UNKNOWN,
            "produceCardSearchId": "",
            "movePositionType": POSITION_UNKNOWN,
            "pickRangeType": "ProducePickRangeType_Unknown",
            "pickCountReferenceProduceCardSearchId": "",
            "pickCountType": "ProducePickCountType_Unknown",
            "produceCardSearchId2": "",
            "pickRangeType2": "ProducePickRangeType_Unknown",
            "pickCountReferenceProduceCardSearchId2": "",
            "pickCountType2": "ProducePickCountType_Unknown",
            "pickCountMin2": 0,
            "pickCountMax2": 0,
            "chainProduceExamEffectId": "",
            "chainProduceExamEffectIds": [],
            "produceExamStatusEnchantId": status_id,
            "produceCardStatusEnchantId": "",
            "produceCardGrowEffectIds": [],
        },
        effect_id,
    )
    # These two fields are presentation/customization metadata on eight
    # otherwise identical installers in this exact slice.  They do not select
    # a runtime target for ExamStatusEnchant, but their known Master shapes are
    # still pinned so drift fails closed.
    if (raw.get("pickCountMin"), raw.get("pickCountMax")) not in {
        (0, 0),
        (1, 1),
    }:
        raise Plan2NativeStatusEnchantError(
            "installer-pick-count-shape", effect_id
        )
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or any(
        not isinstance(value, str) or not value for value in groups
    ):
        raise Plan2NativeStatusEnchantError(
            "installer-effect-groups-shape", effect_id
        )
    return status_id, turn, count if count > 0 else -1, value1 if value1 > 0 else -1


def _validate_trigger(
    runtime_trigger: ExamStatusTrigger,
    row: Mapping[str, object],
    *,
    database: Path,
) -> Plan2StatusEnchantTriggerProgram:
    trigger_id = runtime_trigger.id
    phase_types = _string_array(row["phase_types_json"], f"{trigger_id}.phases")
    phase_values = _integer_array(row["phase_values_json"], f"{trigger_id}.values")
    checks = _string_array(
        row["field_status_check_types_json"], f"{trigger_id}.checks"
    )
    field_types = _string_array(
        row["field_status_types_json"], f"{trigger_id}.field_types"
    )
    field_values = _integer_array(
        row["field_status_values_json"], f"{trigger_id}.field_values"
    )
    field_searches = _string_array(
        row["field_status_produce_card_search_ids_json"],
        f"{trigger_id}.field_searches",
    )
    effect_types = _string_array(
        row["effect_types_json"], f"{trigger_id}.effect_types"
    )
    search_id = str(row["produce_card_search_id"])
    upper = _i32(row["upper_search_count"], f"{trigger_id}.upper")
    lower = _i32(row["lower_search_count"], f"{trigger_id}.lower")
    move = str(row["card_move_position_type"])
    lesson = str(row["lesson_type"])
    if len(phase_types) != 1 or checks or field_searches:
        raise Plan2NativeStatusEnchantError(
            "trigger-base-shape", trigger_id
        )
    phase = phase_types[0]
    if move != POSITION_UNKNOWN or lesson != LESSON_UNKNOWN:
        raise Plan2NativeStatusEnchantError(
            "trigger-move-lesson-shape", trigger_id
        )
    if search_id:
        if phase in {PHASE_CARD_PLAY, PHASE_CARD_PLAY_AFTER}:
            if (lower, upper) != (1, 0):
                raise Plan2NativeStatusEnchantError(
                    "trigger-card-search-count-shape", trigger_id
                )
        elif phase == PHASE_PLAY_COUNT_INTERVAL:
            if (lower, upper) != (0, 0):
                raise Plan2NativeStatusEnchantError(
                    "trigger-interval-search-count-shape", trigger_id
                )
        else:
            raise Plan2NativeStatusEnchantError(
                "trigger-search-phase-unbound", trigger_id
            )
        card_search = load_produce_card_search(search_id, database)
        if runtime_trigger.card_search_rule != card_search:
            raise Plan2NativeStatusEnchantError(
                "trigger-runtime-search-drift", trigger_id
            )
    else:
        if upper != 0 or lower != 0:
            raise Plan2NativeStatusEnchantError(
                "trigger-empty-search-count-shape", trigger_id
            )
        card_search = None
    raw = _json_object(row["raw_json"], trigger_id)
    _raw_equal(
        raw,
        {
            "id": trigger_id,
            "phaseTypes": list(phase_types),
            "phaseValues": list(phase_values),
            "fieldStatusCheckTypes": list(checks),
            "fieldStatusTypes": list(field_types),
            "fieldStatusValues": list(field_values),
            "fieldStatusProduceCardSearchIds": list(field_searches),
            "produceCardSearchId": search_id,
            "upperSearchCount": upper,
            "lowerSearchCount": lower,
            "cardMovePositionType": move,
            "effectTypes": list(effect_types),
            "lessonType": lesson,
        },
        trigger_id,
    )
    if (
        runtime_trigger.phase_types != phase_types
        or runtime_trigger.phase_values != phase_values
        or runtime_trigger.field_status_check_types != checks
        or runtime_trigger.field_status_types != field_types
        or runtime_trigger.field_status_values != field_values
        or runtime_trigger.field_status_card_search_ids != field_searches
        or runtime_trigger.effect_types != effect_types
        or runtime_trigger.produce_card_search_id != search_id
        or runtime_trigger.upper_search_count != upper
        or runtime_trigger.lower_search_count != lower
        or runtime_trigger.card_move_position_type != move
        or runtime_trigger.lesson_type != lesson
    ):
        raise Plan2NativeStatusEnchantError(
            "trigger-runtime-row-drift", trigger_id
        )
    return Plan2StatusEnchantTriggerProgram(
        trigger_id,
        phase,
        phase_values,
        field_types,
        field_values,
        effect_types,
        card_search,
    )


def _child_from_rows(
    runtime_effect: RuntimeExamEffect,
    row: Mapping[str, object],
) -> Plan2StatusEnchantChildProgram:
    effect_id = runtime_effect.id
    raw = _json_object(row["raw_json"], effect_id)
    chain_ids = raw.get("chainProduceExamEffectIds", [])
    grow_ids = raw.get("produceCardGrowEffectIds", [])
    groups = raw.get("effectGroupIds", [])
    for label, values in (
        ("chainProduceExamEffectIds", chain_ids),
        ("produceCardGrowEffectIds", grow_ids),
        ("effectGroupIds", groups),
    ):
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value for value in values
        ):
            raise Plan2NativeStatusEnchantError(
                "child-array-shape", f"{effect_id}.{label}"
            )
    _raw_equal(
        raw,
        {
            "id": effect_id,
            "effectType": runtime_effect.effect_type,
            "effectValue1": runtime_effect.value1,
            "effectValue2": runtime_effect.value2,
            "effectCount": runtime_effect.effect_count,
            "effectTurn": runtime_effect.effect_turn,
            "chainProduceExamEffectId": runtime_effect.chain_effect_id,
            "chainProduceExamEffectIds": chain_ids,
            "produceExamStatusEnchantId": runtime_effect.status_enchant_id,
        },
        effect_id,
    )
    normalized = (
        str(row["id"]),
        str(row["effect_type"]),
        int(row["value1"]),
        int(row["value2"]),
        int(row["effect_count"]),
        int(row["effect_turn"]),
        str(row["status_enchant_id"]),
        str(row["chain_effect_id"]),
    )
    runtime_normalized = (
        runtime_effect.id,
        runtime_effect.effect_type,
        runtime_effect.value1,
        runtime_effect.value2,
        runtime_effect.effect_count,
        runtime_effect.effect_turn,
        runtime_effect.status_enchant_id,
        runtime_effect.chain_effect_id,
    )
    if normalized != runtime_normalized:
        raise Plan2NativeStatusEnchantError(
            "child-runtime-row-drift", effect_id
        )
    return Plan2StatusEnchantChildProgram(
        effect_id=effect_id,
        effect_type=runtime_effect.effect_type,
        value1=runtime_effect.value1,
        value2=runtime_effect.value2,
        count=runtime_effect.effect_count,
        turn=runtime_effect.effect_turn,
        status_enchant_id=runtime_effect.status_enchant_id,
        chain_effect_id=runtime_effect.chain_effect_id,
        chain_effect_ids=tuple(chain_ids),
        target_card_id=str(raw.get("targetProduceCardId", "")),
        target_upgrade=_i32(
            raw.get("targetUpgradeCount", 0), f"{effect_id}.targetUpgradeCount"
        ),
        target_effect_type=str(
            raw.get("targetExamEffectType", EXAM_EFFECT_UNKNOWN)
        ),
        card_search_id=str(raw.get("produceCardSearchId", "")),
        move_position_type=str(raw.get("movePositionType", POSITION_UNKNOWN)),
        card_search_id2=str(raw.get("produceCardSearchId2", "")),
        grow_effect_ids=tuple(grow_ids),
        effect_group_ids=tuple(groups),
    )


def _compile_install(
    *,
    card_id: str,
    upgrade: int,
    slot_index: int,
    ordered_effect_ids: tuple[str, ...],
    effect: Mapping[str, object],
    statuses: Mapping[str, Mapping[str, object]],
    triggers: Mapping[str, Mapping[str, object]],
    effects: Mapping[str, Mapping[str, object]],
    database: Path,
) -> Plan2StatusEnchantInstallProgram:
    effect_id = str(effect["id"])
    status_id, turn, total_limit, per_turn_limit = _validate_parent_effect(
        effect, effect_id
    )
    status_row = statuses.get(status_id)
    if status_row is None:
        raise Plan2NativeStatusEnchantError("status-row-missing", status_id)
    runtime = load_runtime_status_enchant(status_id, database)
    trigger_id = str(status_row["produce_exam_trigger_id"])
    child_ids = _string_array(
        status_row["produce_exam_effect_ids_json"], f"{status_id}.effects"
    )
    raw_status = _json_object(status_row["raw_json"], status_id)
    _raw_equal(
        raw_status,
        {
            "id": status_id,
            "produceExamTriggerId": trigger_id,
            "produceExamEffectIds": list(child_ids),
        },
        status_id,
    )
    if (
        runtime.id != status_id
        or runtime.trigger.id != trigger_id
        or tuple(item.id for item in runtime.effects) != child_ids
    ):
        raise Plan2NativeStatusEnchantError(
            "status-runtime-row-drift", status_id
        )
    trigger_row = triggers.get(trigger_id)
    if trigger_row is None:
        raise Plan2NativeStatusEnchantError("trigger-row-missing", trigger_id)
    trigger = _validate_trigger(runtime.trigger, trigger_row, database=database)
    children: list[Plan2StatusEnchantChildProgram] = []
    for runtime_child in runtime.effects:
        child_row = effects.get(runtime_child.id)
        if child_row is None:
            raise Plan2NativeStatusEnchantError(
                "child-effect-row-missing", runtime_child.id
            )
        children.append(_child_from_rows(runtime_child, child_row))
    return Plan2StatusEnchantInstallProgram(
        card_id=card_id,
        upgrade=upgrade,
        slot_index=slot_index,
        ordered_card_effect_ids=ordered_effect_ids,
        installer_effect_id=effect_id,
        status_enchant_id=status_id,
        turn=turn,
        total_limit=total_limit,
        per_turn_limit=per_turn_limit,
        trigger=trigger,
        children=tuple(children),
    )


def compile_plan2_native_catalog_status_enchant(
    *,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusEnchantCompilation:
    """Compile the exact 99-version/100-occurrence unbound installer slice."""

    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        cards = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT id, upgrade_count, play_effects_json FROM card "
                "WHERE plan_type IN (?, ?) ORDER BY id, upgrade_count",
                (PLAN_COMMON, PLAN_LOGIC),
            )
        )
        effects = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM effect")
        }
        statuses = {
            str(row["id"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM produce_exam_status_enchant"
            )
        }
        triggers = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM produce_exam_trigger")
        }

    candidates: dict[
        tuple[str, int],
        tuple[tuple[str, ...], list[tuple[int, Mapping[str, object]]]],
    ] = {}
    occurrence_count = 0
    for card in cards:
        card_id = str(card["id"])
        upgrade = int(card["upgrade_count"])
        links = _json_array(
            card["play_effects_json"], f"{card_id}#{upgrade}.play_effects"
        )
        ordered: list[str] = []
        found: list[tuple[int, Mapping[str, object]]] = []
        for index, raw_link in enumerate(links):
            if not isinstance(raw_link, Mapping):
                raise Plan2NativeStatusEnchantError(
                    "card-effect-link-shape", f"{card_id}#{upgrade}@{index}"
                )
            effect_id = str(raw_link.get("produceExamEffectId", ""))
            ordered.append(effect_id)
            effect = effects.get(effect_id)
            if effect is None:
                continue
            if (
                str(effect["effect_type"]) == EFFECT_STATUS_ENCHANT
                and not str(raw_link.get("produceExamTriggerId", ""))
                and effect_id not in END_TURN_REMAINING_THREE_DIRECT_TARGETS
            ):
                found.append((index, effect))
                occurrence_count += 1
        if found:
            candidates[(card_id, upgrade)] = (tuple(ordered), found)

    affected_refs = tuple(sorted(candidates))
    if (
        len(affected_refs) != EXPECTED_AFFECTED_VERSION_COUNT
        or occurrence_count != EXPECTED_INSTALLER_OCCURRENCE_COUNT
    ):
        raise Plan2NativeStatusEnchantError(
            "target-slice-cardinality-drift",
            f"versions={len(affected_refs)},occurrences={occurrence_count}",
        )

    programs: list[Plan2NativeStatusEnchantVersionProgram] = []
    blockers: list[Plan2NativeStatusEnchantBlocker] = []
    for (card_id, upgrade), (ordered, found) in sorted(candidates.items()):
        installs: list[Plan2StatusEnchantInstallProgram] = []
        for slot_index, effect in found:
            effect_id = str(effect["id"])
            try:
                installs.append(
                    _compile_install(
                        card_id=card_id,
                        upgrade=upgrade,
                        slot_index=slot_index,
                        ordered_effect_ids=ordered,
                        effect=effect,
                        statuses=statuses,
                        triggers=triggers,
                        effects=effects,
                        database=database_path,
                    )
                )
            except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
                blockers.append(
                    Plan2NativeStatusEnchantBlocker(
                        card_id,
                        upgrade,
                        slot_index,
                        effect_id,
                        getattr(error, "code", "status-enchant-compile-failed"),
                        getattr(error, "detail", str(error)) or str(error),
                    )
                )
        if len(installs) == len(found):
            programs.append(
                Plan2NativeStatusEnchantVersionProgram(
                    card_id, upgrade, ordered, tuple(installs)
                )
            )

    compilation = Plan2NativeStatusEnchantCompilation(
        PLAN2_NATIVE_STATUS_ENCHANT_SCHEMA_VERSION,
        str(database_path),
        affected_refs,
        occurrence_count,
        Plan2NativeStatusEnchantCatalog(tuple(programs)),
        tuple(blockers),
    )
    if compilation.fully_compiled:
        installer_ids = {
            install.installer_effect_id
            for version in compilation.catalog.programs
            for install in version.installs
        }
        status_ids = {
            install.status_enchant_id
            for version in compilation.catalog.programs
            for install in version.installs
        }
        trigger_ids = {
            install.trigger.trigger_id
            for version in compilation.catalog.programs
            for install in version.installs
        }
        if (
            len(installer_ids) != EXPECTED_INSTALLER_EFFECT_COUNT
            or len(status_ids) != EXPECTED_STATUS_ENCHANT_COUNT
            or len(trigger_ids) != EXPECTED_TRIGGER_COUNT
        ):
            raise Plan2NativeStatusEnchantError(
                "target-owner-cardinality-drift",
                f"effects={len(installer_ids)},statuses={len(status_ids)},"
                f"triggers={len(trigger_ids)}",
            )
    return compilation


def load_plan2_native_status_enchant_installer_effect(
    effect_id: str,
    *,
    source_id: str,
    source_upgrade: int = 0,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2StatusEnchantInstallProgram:
    """Compile one exact Master ``ExamStatusEnchant`` installer by identity.

    Scheduled gimmicks are not card rows, so they cannot appear in the normal
    card-owned catalog slice above.  They nevertheless use the same native
    installer/status/trigger/child graph.  This narrow entry point reuses that
    graph compiler without inventing card ownership or accepting an unknown
    effect shape.
    """

    _text(effect_id, "effect_id")
    _text(source_id, "source_id")
    _nonnegative(source_upgrade, "source_upgrade")
    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        parent_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
        if parent_row is None:
            raise Plan2NativeStatusEnchantError(
                "installer-effect-row-missing", effect_id
            )
        parent = dict(parent_row)
        status_id = str(parent.get("status_enchant_id", ""))
        status_row = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (status_id,),
        ).fetchone()
        if status_row is None:
            raise Plan2NativeStatusEnchantError("status-row-missing", status_id)
        status = dict(status_row)
        trigger_id = str(status.get("produce_exam_trigger_id", ""))
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if trigger_row is None:
            raise Plan2NativeStatusEnchantError("trigger-row-missing", trigger_id)
        child_ids = _string_array(
            status.get("produce_exam_effect_ids_json"), f"{status_id}.effects"
        )
        child_rows: dict[str, Mapping[str, object]] = {}
        for child_id in child_ids:
            child_row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (child_id,)
            ).fetchone()
            if child_row is None:
                raise Plan2NativeStatusEnchantError(
                    "child-effect-row-missing", child_id
                )
            child_rows[child_id] = dict(child_row)

    return _compile_install(
        card_id=source_id,
        upgrade=source_upgrade,
        slot_index=0,
        ordered_effect_ids=(effect_id,),
        effect=parent,
        statuses={status_id: status},
        triggers={trigger_id: dict(trigger_row)},
        effects=child_rows,
        database=database_path,
    )


def load_plan2_native_bare_start_status_enchant(
    status_enchant_id: str,
    *,
    source_id: str,
    source_upgrade: int = 0,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2StatusEnchantInstallProgram:
    """Compile one ``IExamStartStatusEnchantData`` Master status.

    Start-status rows carry only origin metadata plus the status ID.  Native
    ``TryAddTriggerEffectStatus`` supplies ``turn=-1``, ``limitCount=-1`` and
    ``limitCountInTurn=-1`` at this boundary, so a synthetic installer wrapper
    is the exact typed analogue of that overload rather than a guessed card
    effect.
    """

    _text(status_enchant_id, "status_enchant_id")
    _text(source_id, "source_id")
    _nonnegative(source_upgrade, "source_upgrade")
    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        status_row = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (status_enchant_id,),
        ).fetchone()
        if status_row is None:
            raise Plan2NativeStatusEnchantError(
                "status-row-missing", status_enchant_id
            )
        status = dict(status_row)
        trigger_id = str(status.get("produce_exam_trigger_id", ""))
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (trigger_id,),
        ).fetchone()
        if trigger_row is None:
            raise Plan2NativeStatusEnchantError(
                "trigger-row-missing", trigger_id
            )
        child_ids = _string_array(
            status.get("produce_exam_effect_ids_json"),
            f"{status_enchant_id}.effects",
        )
        child_rows: dict[str, Mapping[str, object]] = {}
        for child_id in child_ids:
            child_row = connection.execute(
                "SELECT * FROM effect WHERE id = ?",
                (child_id,),
            ).fetchone()
            if child_row is None:
                raise Plan2NativeStatusEnchantError(
                    "child-effect-row-missing", child_id
                )
            child_rows[child_id] = dict(child_row)

    installer_effect_id = f"exam-start-status:{status_enchant_id}"
    raw_effect = {
        "id": installer_effect_id,
        "effectType": EFFECT_STATUS_ENCHANT,
        "effectValue1": 0,
        "effectValue2": 0,
        "effectCount": 0,
        "effectTurn": -1,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": EXAM_EFFECT_UNKNOWN,
        "produceCardSearchId": "",
        "movePositionType": POSITION_UNKNOWN,
        "pickRangeType": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": "ProducePickCountType_Unknown",
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": status_enchant_id,
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": [],
    }
    synthetic_effect: Mapping[str, object] = {
        "id": installer_effect_id,
        "effect_type": EFFECT_STATUS_ENCHANT,
        "value1": 0,
        "value2": 0,
        "effect_count": 0,
        "effect_turn": -1,
        "status_enchant_id": status_enchant_id,
        "chain_effect_id": "",
        "raw_json": json.dumps(
            raw_effect,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    return _compile_install(
        card_id=source_id,
        upgrade=source_upgrade,
        slot_index=0,
        ordered_effect_ids=(installer_effect_id,),
        effect=synthetic_effect,
        statuses={status_enchant_id: status},
        triggers={trigger_id: dict(trigger_row)},
        effects=child_rows,
        database=database_path,
    )


load_plan2_native_catalog_status_enchant = (
    compile_plan2_native_catalog_status_enchant
)


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantListener:
    """One independently stacked native status-enchant listener."""

    status_uid: int
    source_guid: str
    source_card_id: str
    source_upgrade: int
    play_origin: str
    program: Plan2StatusEnchantInstallProgram
    turns_remaining: int
    total_remaining: int
    per_turn_remaining: int
    passing_turn_start: bool = False
    phase_counts: tuple[tuple[str, int], ...] = ()
    turn_count: int = 0

    def __post_init__(self) -> None:
        if _nonnegative(self.status_uid, "listener.status_uid") == 0:
            raise Plan2NativeStatusEnchantError("status-uid-zero")
        _text(self.source_guid, "listener.source_guid")
        _text(self.source_card_id, "listener.source_card_id")
        _nonnegative(self.source_upgrade, "listener.source_upgrade")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeStatusEnchantError(
                "play-origin-unbound", self.play_origin
            )
        if not isinstance(self.program, Plan2StatusEnchantInstallProgram):
            raise TypeError("program must be Plan2StatusEnchantInstallProgram")
        if (self.source_card_id, self.source_upgrade) != self.program.ref:
            raise Plan2NativeStatusEnchantError(
                "listener-source-program-drift", self.source_guid
            )
        turns = _i32(self.turns_remaining, "listener.turns_remaining")
        total = _i32(self.total_remaining, "listener.total_remaining")
        per_turn = _i32(
            self.per_turn_remaining, "listener.per_turn_remaining"
        )
        if self.program.turn == -1:
            if turns != -1:
                raise Plan2NativeStatusEnchantError(
                    "listener-unlimited-turn-drift", str(self.status_uid)
                )
        elif turns < 1:
            raise Plan2NativeStatusEnchantError(
                "inactive-listener-retained", str(self.status_uid)
            )
        if self.program.total_limit == -1:
            if total != -1:
                raise Plan2NativeStatusEnchantError(
                    "listener-unlimited-total-drift", str(self.status_uid)
                )
        elif not 1 <= total <= self.program.total_limit:
            raise Plan2NativeStatusEnchantError(
                "listener-total-range", str(self.status_uid)
            )
        if self.program.per_turn_limit == -1:
            if per_turn != -1:
                raise Plan2NativeStatusEnchantError(
                    "listener-unlimited-per-turn-drift", str(self.status_uid)
                )
        elif not 0 <= per_turn <= self.program.per_turn_limit:
            raise Plan2NativeStatusEnchantError(
                "listener-per-turn-range", str(self.status_uid)
            )
        if type(self.passing_turn_start) is not bool:
            raise TypeError("passing_turn_start must be bool")
        _nonnegative(self.turn_count, "listener.turn_count")
        counts = tuple(self.phase_counts)
        if tuple(sorted(counts)) != counts or len({key for key, _ in counts}) != len(counts):
            raise Plan2NativeStatusEnchantError(
                "listener-phase-count-order", str(self.status_uid)
            )
        for key, value in counts:
            _text(key, "listener.phase_count.phase")
            _nonnegative(value, "listener.phase_count.value")
        object.__setattr__(self, "phase_counts", counts)

    @property
    def status_enchant_id(self) -> str:
        return self.program.status_enchant_id

    @property
    def installer_effect_id(self) -> str:
        return self.program.installer_effect_id

    def phase_count(self, phase: str) -> int:
        return dict(self.phase_counts).get(phase, 0)


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantRuntime:
    listeners: tuple[Plan2NativeStatusEnchantListener, ...] = ()
    next_status_uid: int = 1
    event_sequence: int = 0
    history: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        listeners = tuple(self.listeners)
        if any(
            not isinstance(item, Plan2NativeStatusEnchantListener)
            for item in listeners
        ):
            raise TypeError("listeners contain the wrong type")
        uids = tuple(item.status_uid for item in listeners)
        if len(uids) != len(set(uids)):
            raise Plan2NativeStatusEnchantError("duplicate-status-uid")
        if _nonnegative(self.next_status_uid, "runtime.next_status_uid") == 0:
            raise Plan2NativeStatusEnchantError("next-status-uid-zero")
        if uids and self.next_status_uid <= max(uids):
            raise Plan2NativeStatusEnchantError("next-status-uid-not-fresh")
        _nonnegative(self.event_sequence, "runtime.event_sequence")
        history = tuple(self.history)
        if any(not isinstance(item, str) or not item for item in history):
            raise Plan2NativeStatusEnchantError("runtime-history-shape")
        object.__setattr__(self, "listeners", listeners)
        object.__setattr__(self, "history", history)


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantInstallResult:
    before: Plan2NativeStatusEnchantRuntime
    after: Plan2NativeStatusEnchantRuntime
    program: Plan2StatusEnchantInstallProgram
    installed: bool
    blocked: bool = False
    unresolved: tuple[str, ...] = ()
    listener: Plan2NativeStatusEnchantListener | None = None
    trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantEvent:
    phase: str
    accepted: bool = True
    play_origin: str = "normal"
    round_number: int = 1
    card_id: str = ""
    card_upgrade: int = 0
    card_category: str = ""
    card_effect_group_ids: tuple[str, ...] = ()
    card_position: str = ""
    review: int | None = None
    aggressive: int | None = None
    remaining_turns: int | None = None
    block: int | None = None
    stamina: int | None = None
    max_stamina: int | None = None
    changed_effect_type: str = ""
    status_difference: int | None = None
    status_change_committed: bool | None = None
    lesson_type: str = LESSON_UNKNOWN
    suppressed_status_uids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _text(self.phase, "event.phase")
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be bool")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeStatusEnchantError(
                "play-origin-unbound", self.play_origin
            )
        if _nonnegative(self.round_number, "event.round_number") == 0:
            raise Plan2NativeStatusEnchantError("round-number-zero")
        _text(self.card_id, "event.card_id", empty=True)
        _nonnegative(self.card_upgrade, "event.card_upgrade")
        _text(self.card_category, "event.card_category", empty=True)
        _text(self.card_position, "event.card_position", empty=True)
        _text(self.changed_effect_type, "event.changed_effect_type", empty=True)
        _text(self.lesson_type, "event.lesson_type", empty=True)
        groups = tuple(self.card_effect_group_ids)
        if any(not isinstance(item, str) or not item for item in groups):
            raise Plan2NativeStatusEnchantError("event-effect-groups-shape")
        object.__setattr__(self, "card_effect_group_ids", groups)
        for name in (
            "review",
            "aggressive",
            "remaining_turns",
            "block",
            "stamina",
            "max_stamina",
            "status_difference",
        ):
            value = getattr(self, name)
            if value is not None:
                _i32(value, f"event.{name}")
        if self.status_change_committed is not None and type(
            self.status_change_committed
        ) is not bool:
            raise TypeError("status_change_committed must be bool or None")
        suppressed = tuple(self.suppressed_status_uids)
        if (
            any(type(value) is not int or value <= 0 for value in suppressed)
            or len(suppressed) != len(set(suppressed))
        ):
            raise Plan2NativeStatusEnchantError(
                "event-suppressed-status-uids-shape"
            )
        object.__setattr__(self, "suppressed_status_uids", suppressed)


@dataclass(frozen=True, slots=True)
class Plan2StatusEnchantChildCommand:
    event_sequence: int
    listener_sequence: int
    child_sequence: int
    status_uid: int
    source_guid: str
    source_card_id: str
    source_upgrade: int
    play_origin: str
    installer_effect_id: str
    status_enchant_id: str
    trigger_id: str
    phase: str
    child: Plan2StatusEnchantChildProgram


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusEnchantTransition:
    before: Plan2NativeStatusEnchantRuntime
    after: Plan2NativeStatusEnchantRuntime
    event: Plan2NativeStatusEnchantEvent
    commands: tuple[Plan2StatusEnchantChildCommand, ...] = ()
    fired_status_uids: tuple[int, ...] = ()
    expired_status_uids: tuple[int, ...] = ()
    unresolved: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved


def install_plan2_native_status_enchant(
    runtime: Plan2NativeStatusEnchantRuntime,
    program: Plan2StatusEnchantInstallProgram,
    *,
    source_guid: str,
    play_origin: str = "normal",
    accepted_play: bool = True,
    completed_effect_ids: Sequence[str] = (),
    status_add_blocked: bool = False,
) -> Plan2NativeStatusEnchantInstallResult:
    """Append one fresh UID after exact earlier card effects have completed."""

    if not isinstance(runtime, Plan2NativeStatusEnchantRuntime):
        raise TypeError("runtime must be Plan2NativeStatusEnchantRuntime")
    if not isinstance(program, Plan2StatusEnchantInstallProgram):
        raise TypeError("program must be Plan2StatusEnchantInstallProgram")
    if play_origin not in PLAY_ORIGINS:
        return Plan2NativeStatusEnchantInstallResult(
            runtime,
            runtime,
            program,
            False,
            unresolved=(f"play-origin-unbound:{play_origin}",),
            trace=("fail-closed-before-status-install",),
        )
    if type(accepted_play) is not bool or type(status_add_blocked) is not bool:
        return Plan2NativeStatusEnchantInstallResult(
            runtime,
            runtime,
            program,
            False,
            unresolved=("install-boolean-shape",),
            trace=("fail-closed-before-status-install",),
        )
    completed = tuple(completed_effect_ids)
    if any(not isinstance(item, str) or not item for item in completed):
        reason = "completed-effect-ids-shape"
    elif completed != program.prior_effect_ids:
        reason = "prior-card-effect-order-unproven"
    else:
        reason = ""
    if reason:
        return Plan2NativeStatusEnchantInstallResult(
            runtime,
            runtime,
            program,
            False,
            unresolved=(reason,),
            trace=("fail-closed-before-status-install",),
        )
    if not accepted_play:
        return Plan2NativeStatusEnchantInstallResult(
            runtime,
            runtime,
            program,
            False,
            trace=("rejected-before-direct-card-effects",),
        )
    if status_add_blocked:
        return Plan2NativeStatusEnchantInstallResult(
            runtime,
            runtime,
            program,
            False,
            blocked=True,
            trace=("status-add-blocked",),
        )
    if runtime.next_status_uid == INT32_MAX:
        return Plan2NativeStatusEnchantInstallResult(
            runtime,
            runtime,
            program,
            False,
            unresolved=("status-uid-overflow",),
            trace=("fail-closed-before-status-install",),
        )
    listener = Plan2NativeStatusEnchantListener(
        status_uid=runtime.next_status_uid,
        source_guid=source_guid,
        source_card_id=program.card_id,
        source_upgrade=program.upgrade,
        play_origin=play_origin,
        program=program,
        turns_remaining=program.turn,
        total_remaining=program.total_limit,
        per_turn_remaining=program.per_turn_limit,
    )
    marker = (
        f"install:{listener.status_uid}:{program.installer_effect_id}:"
        f"{play_origin}"
    )
    after = replace(
        runtime,
        listeners=runtime.listeners + (listener,),
        next_status_uid=runtime.next_status_uid + 1,
        history=runtime.history + (marker,),
    )
    return Plan2NativeStatusEnchantInstallResult(
        runtime,
        after,
        program,
        True,
        listener=listener,
        trace=("ordered-prior-card-effects-complete", marker),
    )


def _replace_phase_count(
    listener: Plan2NativeStatusEnchantListener, phase: str, value: int
) -> Plan2NativeStatusEnchantListener:
    counts = dict(listener.phase_counts)
    counts[phase] = value
    return replace(listener, phase_counts=tuple(sorted(counts.items())))


def _match_card_search(
    rule: ProduceCardSearchRule,
    event: Plan2NativeStatusEnchantEvent,
) -> tuple[bool, str | None]:
    neutral = (
        not rule.card_rarities
        and not rule.produce_card_ids
        and not rule.upgrade_counts
        and rule.plan_type == PLAN_UNKNOWN
        and rule.card_status_type == STATUS_UNKNOWN
        and rule.order_type == ORDER_UNKNOWN
        and not rule.card_search_tag
        and not rule.produce_card_random_pool_id
        and rule.limit_count == 0
        and rule.stamina_min_max_type == MIN_MAX_UNKNOWN
        and rule.stamina_min == 0
        and rule.stamina_max == 0
        and rule.exam_effect_type == EXAM_EFFECT_UNKNOWN
        and not rule.is_self
        and not rule.produce_card_pool_id
        and rule.cost_type == COST_UNKNOWN
        and not rule.is_customized
    )
    if not neutral:
        return False, f"card-search-shape:{rule.id}"
    if rule.card_position_type not in {POSITION_PLAYING, POSITION_TARGET}:
        return False, f"card-search-position-shape:{rule.id}"
    if event.card_position != rule.card_position_type:
        if not event.card_position:
            return False, f"card-position-snapshot-missing:{rule.id}"
        return False, None
    if rule.card_categories:
        if not event.card_category:
            return False, f"card-category-snapshot-missing:{rule.id}"
        if event.card_category not in rule.card_categories:
            return False, None
    if rule.effect_group_ids and not all(
        value in event.card_effect_group_ids for value in rule.effect_group_ids
    ):
        return False, None
    return True, None


def _field_gate(
    trigger: Plan2StatusEnchantTriggerProgram,
    event: Plan2NativeStatusEnchantEvent,
) -> tuple[bool, str | None]:
    if not trigger.field_status_types:
        return True, None
    field_type = trigger.field_status_types[0]
    if field_type == FIELD_NO_BLOCK:
        if event.block is None:
            return False, "block-snapshot-missing"
        return event.block <= 0, None
    if field_type == FIELD_REVIEW_UP:
        if event.review is None:
            return False, "review-snapshot-missing"
        return event.review >= trigger.field_status_values[0], None
    if field_type == FIELD_AGGRESSIVE_UP:
        if event.aggressive is None:
            return False, "aggressive-snapshot-missing"
        return event.aggressive >= trigger.field_status_values[0], None
    if field_type == FIELD_REMAINING_TURN:
        if event.remaining_turns is None:
            return False, "remaining-turn-snapshot-missing"
        return event.remaining_turns <= trigger.field_status_values[0], None
    if field_type == FIELD_STAMINA_LESS_MULTIPLE:
        if event.stamina is None or event.max_stamina is None:
            return False, "stamina-snapshot-missing"
        if trigger.field_status_values != (500,):
            return False, f"stamina-threshold-unbound:{trigger.trigger_id}"
        # Reuse the exact standalone float32/native boundary proof for the one
        # StartTurn stamina trigger present in this catalog slice.
        from .plan2_start_turn_stamina_less_recover import (
            StaminaLessEvaluationInput,
            evaluate_stamina_less_trigger,
        )

        try:
            evaluation = evaluate_stamina_less_trigger(
                StaminaLessEvaluationInput(event.stamina, event.max_stamina),
                trigger.trigger_id,
            )
        except (TypeError, ValueError) as error:
            return False, f"stamina-predicate-input:{error}"
        if not evaluation.supported or evaluation.fires is None:
            detail = evaluation.reasons[0] if evaluation.reasons else "unknown"
            return False, f"stamina-predicate-unresolved:{detail}"
        return evaluation.fires, None
    return False, f"field-status-unbound:{field_type}"


def _phase_gate(
    trigger: Plan2StatusEnchantTriggerProgram,
    event: Plan2NativeStatusEnchantEvent,
) -> tuple[bool, str | None]:
    if trigger.phase != PHASE_STATUS_CHANGE:
        return True, None
    if event.status_change_committed is None:
        return False, "status-change-commit-snapshot-missing"
    if not event.status_change_committed:
        return False, None
    if not event.changed_effect_type:
        return False, "changed-effect-type-snapshot-missing"
    if event.status_difference is None:
        return False, "status-difference-snapshot-missing"
    if event.changed_effect_type not in trigger.effect_types:
        return False, None
    if event.status_difference == 0:
        return False, None
    if trigger.phase_values:
        return event.status_difference >= trigger.phase_values[0], None
    return True, None


def _resolve_status_enchant_event(
    runtime: Plan2NativeStatusEnchantRuntime,
    event: Plan2NativeStatusEnchantEvent,
) -> Plan2NativeStatusEnchantTransition:
    if event.phase not in RUNTIME_SUPPORTED_PHASES:
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            unresolved=(f"event-phase-unbound:{event.phase}",),
            trace=("fail-closed-before-listener-dispatch",),
        )
    if event.phase in PLAY_PHASES and not event.accepted:
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            trace=("rejected-before-listener-dispatch",),
        )

    proposed = list(runtime.listeners)
    eligible: list[tuple[int, Plan2NativeStatusEnchantListener]] = []
    errors: list[str] = []
    trace: list[str] = [f"dispatch:{event.phase}:{event.play_origin}"]
    for sequence, original in enumerate(runtime.listeners):
        if original.status_uid in event.suppressed_status_uids:
            trace.append(f"listener:{original.status_uid}:same-chain-suppressed")
            continue
        trigger = original.program.trigger
        if trigger.phase != event.phase:
            continue
        listener = proposed[sequence]
        if trigger.card_search is not None:
            matches, reason = _match_card_search(trigger.card_search, event)
            if reason is not None:
                errors.append(f"listener:{listener.status_uid}:{reason}")
                continue
            if not matches:
                trace.append(f"listener:{listener.status_uid}:search-non-match")
                continue
        if trigger.phase in INTERVAL_PHASES:
            count = listener.phase_count(trigger.phase)
            if count == INT32_MAX:
                errors.append(f"phase-count-overflow:{listener.status_uid}")
                continue
            listener = _replace_phase_count(listener, trigger.phase, count + 1)
            proposed[sequence] = listener
            trace.append(
                f"listener:{listener.status_uid}:phase-count:{count}->{count + 1}"
            )
            assert trigger.interval is not None
            if (count + 1) % trigger.interval:
                continue
        field_matches, field_reason = _field_gate(trigger, event)
        if field_reason is not None:
            errors.append(f"listener:{listener.status_uid}:{field_reason}")
            continue
        phase_matches, phase_reason = _phase_gate(trigger, event)
        if phase_reason is not None:
            errors.append(f"listener:{listener.status_uid}:{phase_reason}")
            continue
        if not field_matches or not phase_matches:
            trace.append(f"listener:{listener.status_uid}:predicate-false")
            continue
        if listener.total_remaining == 0 or listener.per_turn_remaining == 0:
            continue
        eligible.append((sequence, listener))

    if errors:
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            unresolved=tuple(dict.fromkeys(errors)),
            trace=("fail-closed-before-listener-state-change",),
        )

    commands: list[Plan2StatusEnchantChildCommand] = []
    fired: list[int] = []
    removed: set[int] = set()
    next_event_sequence = runtime.event_sequence + 1
    if next_event_sequence > INT32_MAX:
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            unresolved=("event-sequence-overflow",),
            trace=("fail-closed-before-listener-state-change",),
        )
    for sequence, captured in eligible:
        current = proposed[sequence]
        total = current.total_remaining
        per_turn = current.per_turn_remaining
        if total > 0:
            total -= 1
        if per_turn > 0:
            per_turn -= 1
        fired.append(current.status_uid)
        for child_sequence, child in enumerate(current.program.children):
            commands.append(
                Plan2StatusEnchantChildCommand(
                    event_sequence=next_event_sequence,
                    listener_sequence=sequence,
                    child_sequence=child_sequence,
                    status_uid=current.status_uid,
                    source_guid=current.source_guid,
                    source_card_id=current.source_card_id,
                    source_upgrade=current.source_upgrade,
                    play_origin=event.play_origin,
                    installer_effect_id=current.installer_effect_id,
                    status_enchant_id=current.status_enchant_id,
                    trigger_id=current.program.trigger.trigger_id,
                    phase=event.phase,
                    child=child,
                )
            )
        trace.append(
            f"listener:{current.status_uid}:spend-before-children:"
            f"total={total}:turn={per_turn}"
        )
        if total == 0:
            removed.add(current.status_uid)
        else:
            proposed[sequence] = replace(
                current,
                total_remaining=total,
                per_turn_remaining=per_turn,
            )
    active = tuple(item for item in proposed if item.status_uid not in removed)
    marker = f"event:{next_event_sequence}:{event.phase}:fires={len(fired)}"
    after = replace(
        runtime,
        listeners=active,
        event_sequence=next_event_sequence,
        history=runtime.history + (marker,),
    )
    return Plan2NativeStatusEnchantTransition(
        runtime,
        after,
        event,
        tuple(commands),
        tuple(fired),
        trace=tuple(trace + [marker]),
    )


def start_plan2_native_status_enchant_turn(
    runtime: Plan2NativeStatusEnchantRuntime,
    event: Plan2NativeStatusEnchantEvent,
) -> Plan2NativeStatusEnchantTransition:
    """Spend finite duration, reset turn limits, then dispatch StartTurn."""

    if event.phase != PHASE_START_TURN:
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            unresolved=("start-turn-api-phase-mismatch",),
            trace=("fail-closed-before-turn-boundary",),
        )
    if any(listener.turn_count == INT32_MAX for listener in runtime.listeners):
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            unresolved=("listener-turn-count-overflow",),
            trace=("fail-closed-before-turn-boundary",),
        )
    prepared: list[Plan2NativeStatusEnchantListener] = []
    expired: list[int] = []
    for listener in runtime.listeners:
        turns = listener.turns_remaining
        if turns > 0 and listener.passing_turn_start:
            turns -= 1
        if turns == 0:
            expired.append(listener.status_uid)
            continue
        prepared.append(
            replace(
                listener,
                turns_remaining=turns,
                per_turn_remaining=listener.program.per_turn_limit,
                turn_count=listener.turn_count + 1,
            )
        )
    prepared_runtime = replace(runtime, listeners=tuple(prepared))
    transition = _resolve_status_enchant_event(prepared_runtime, event)
    if transition.unresolved:
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            unresolved=transition.unresolved,
            trace=("fail-closed-before-turn-boundary",),
        )
    marked = tuple(
        replace(listener, passing_turn_start=True)
        for listener in transition.after.listeners
    )
    after = replace(transition.after, listeners=marked)
    return Plan2NativeStatusEnchantTransition(
        runtime,
        after,
        event,
        transition.commands,
        transition.fired_status_uids,
        tuple(expired),
        trace=(
            *(f"expire-before-start-turn:{uid}" for uid in expired),
            *transition.trace,
            "mark-passing-start-turn",
        ),
    )


def end_plan2_native_status_enchant_turn(
    runtime: Plan2NativeStatusEnchantRuntime,
    event: Plan2NativeStatusEnchantEvent,
) -> Plan2NativeStatusEnchantTransition:
    if event.phase != PHASE_END_TURN:
        return Plan2NativeStatusEnchantTransition(
            runtime,
            runtime,
            event,
            unresolved=("end-turn-api-phase-mismatch",),
            trace=("fail-closed-before-end-turn",),
        )
    return _resolve_status_enchant_event(runtime, event)


def resolve_plan2_native_status_enchant_event(
    runtime: Plan2NativeStatusEnchantRuntime,
    event: Plan2NativeStatusEnchantEvent,
) -> Plan2NativeStatusEnchantTransition:
    """Dispatch one immutable lifecycle event; unknown shapes are atomic no-ops."""

    if not isinstance(runtime, Plan2NativeStatusEnchantRuntime):
        raise TypeError("runtime must be Plan2NativeStatusEnchantRuntime")
    if not isinstance(event, Plan2NativeStatusEnchantEvent):
        raise TypeError("event must be Plan2NativeStatusEnchantEvent")
    if event.phase == PHASE_START_TURN:
        return start_plan2_native_status_enchant_turn(runtime, event)
    if event.phase == PHASE_END_TURN:
        return end_plan2_native_status_enchant_turn(runtime, event)
    return _resolve_status_enchant_event(runtime, event)


execute_plan2_native_status_enchant_event = (
    resolve_plan2_native_status_enchant_event
)
