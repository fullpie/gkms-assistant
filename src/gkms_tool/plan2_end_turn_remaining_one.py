"""Exact ``ExamEndTurn`` / ``remaining_turn-1`` Plan2 support.

The native trigger is a field-status predicate, not a new ``Plan2State``
field.  Android v3.2.3 compares the signed current remaining-turn value with
the trigger value using ``<=`` while the end-turn command is being evaluated;
the exam loop decrements and stores the value in a later state-machine
continuation.  This module keeps that predicate as a small typed evaluator.

Status installation and end-turn child execution remain in
``plan2_end_turn_trigger``.  The loader below only validates this trigger's
extra Master shape and returns the existing ``Plan2EndTurnProgram`` through a
thin adapter.  Unknown trigger, wrapper, child, or group shapes fail closed.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_end_turn_trigger import (
    END_TURN_PHASE_TYPE,
    LESSON_DEPEND_BLOCK_EFFECT_TYPE,
    STATUS_ENCHANT_EFFECT_TYPE,
    Plan2EndTurnContractError,
    Plan2EndTurnInstallTransition,
    Plan2EndTurnProgram,
    _json_array,
    _json_object,
    _load_child,
    _ordered_string_array,
    _validate_neutral_effect_fields,
    simulate_install_plan2_end_turn_listener,
)
from .plan2_state import Plan2EndTurnEffect, Plan2State


REMAINING_ONE_TRIGGER_ID = "e_trigger-exam_end_turn-remaining_turn-1"
REMAINING_TURN_FIELD_TYPE = "ProduceExamFieldStatusType_RemainingTurn"
REMAINING_ONE_THRESHOLD = 1
REMAINING_ONE_CHILD_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_lesson_depend_block-000",
    "effect_group-visible-exam_lesson-000",
)
REMAINING_ONE_WRAPPER_EFFECT_GROUP_IDS = (
    *REMAINING_ONE_CHILD_EFFECT_GROUP_IDS,
    "effect_group-visible-exam_status_enchant-000",
)


class Plan2EndTurnRemainingOneContractError(Plan2EndTurnContractError):
    """A Master row is outside the exact remaining-one native contract."""


def _strict_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise Plan2EndTurnRemainingOneContractError(
            f"{label} must be a plain integer"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2EndTurnRemainingOneContractError(
            f"{label} is outside Int32"
        )
    return value


def _strict_equal(
    actual: object, expected: object, label: str
) -> None:
    """Compare JSON scalars/arrays without Python bool/int coercion."""

    if type(actual) is not type(expected):
        raise Plan2EndTurnRemainingOneContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )
    if isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2EndTurnRemainingOneContractError(
                f"{label} must be {expected!r}, got {actual!r}"
            )
        for index, (item, wanted) in enumerate(zip(actual, expected)):
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if actual != expected:
        raise Plan2EndTurnRemainingOneContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )


def _strict_array(
    raw_json: object, expected: list[object], label: str
) -> tuple[object, ...]:
    actual = _json_array(raw_json, label)
    _strict_equal(actual, expected, label)
    return tuple(actual)


def _strict_raw_fields(
    raw: dict[str, object], expected: dict[str, object], row_id: str
) -> None:
    for key, value in expected.items():
        if key not in raw:
            raise Plan2EndTurnRemainingOneContractError(
                f"{row_id}: missing raw field {key}"
            )
        _strict_equal(raw[key], value, f"{row_id}.{key}")


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingOneTrigger:
    """The fully typed, exact Master trigger shape for threshold one."""

    trigger_id: str = REMAINING_ONE_TRIGGER_ID
    phase_types: tuple[str, ...] = (END_TURN_PHASE_TYPE,)
    phase_values: tuple[int, ...] = ()
    field_status_check_types: tuple[str, ...] = ()
    field_status_types: tuple[str, ...] = (REMAINING_TURN_FIELD_TYPE,)
    field_status_values: tuple[int, ...] = (REMAINING_ONE_THRESHOLD,)
    field_status_produce_card_search_ids: tuple[str, ...] = ()
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = "ProduceCardMovePositionType_Unknown"
    effect_types: tuple[str, ...] = ()
    lesson_type: str = "ProduceStepLessonType_Unknown"

    def __post_init__(self) -> None:
        if self.trigger_id != REMAINING_ONE_TRIGGER_ID:
            raise Plan2EndTurnRemainingOneContractError(
                "trigger_id must be e_trigger-exam_end_turn-remaining_turn-1"
            )
        exact = (
            ("phase_types", (END_TURN_PHASE_TYPE,)),
            ("phase_values", ()),
            ("field_status_check_types", ()),
            ("field_status_types", (REMAINING_TURN_FIELD_TYPE,)),
            ("field_status_values", (REMAINING_ONE_THRESHOLD,)),
            ("field_status_produce_card_search_ids", ()),
            ("effect_types", ()),
        )
        for label, expected in exact:
            if getattr(self, label) != expected:
                raise Plan2EndTurnRemainingOneContractError(
                    f"{label} has unknown shape: {getattr(self, label)!r}"
                )
        if self.produce_card_search_id != "":
            raise Plan2EndTurnRemainingOneContractError(
                "produce_card_search_id must be empty"
            )
        if self.card_move_position_type != "ProduceCardMovePositionType_Unknown":
            raise Plan2EndTurnRemainingOneContractError(
                "card_move_position_type must be Unknown"
            )
        if self.lesson_type != "ProduceStepLessonType_Unknown":
            raise Plan2EndTurnRemainingOneContractError(
                "lesson_type must be Unknown"
            )
        _strict_int(self.upper_search_count, "upper_search_count")
        _strict_int(self.lower_search_count, "lower_search_count")
        if self.upper_search_count != 0 or self.lower_search_count != 0:
            raise Plan2EndTurnRemainingOneContractError(
                "search counts must be zero"
            )


EXACT_REMAINING_ONE_TRIGGER = Plan2EndTurnRemainingOneTrigger()


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingOneEvaluation:
    """Result of evaluating the native signed threshold predicate."""

    remaining_turn: int
    threshold: int
    triggered: bool
    comparison: Literal["signed_less_equal"] = "signed_less_equal"
    evaluation_point: Literal[
        "before_exam_end_turn_decrement"
    ] = "before_exam_end_turn_decrement"

    def __post_init__(self) -> None:
        _strict_int(self.remaining_turn, "remaining_turn")
        _strict_int(self.threshold, "threshold")
        if self.threshold != REMAINING_ONE_THRESHOLD:
            raise Plan2EndTurnRemainingOneContractError(
                "threshold must be one"
            )
        if type(self.triggered) is not bool:
            raise TypeError("triggered must be a boolean")
        if self.comparison != "signed_less_equal":
            raise Plan2EndTurnRemainingOneContractError(
                "comparison must be signed_less_equal"
            )
        if self.evaluation_point != "before_exam_end_turn_decrement":
            raise Plan2EndTurnRemainingOneContractError(
                "evaluation point must be before decrement"
            )


def evaluate_plan2_end_turn_remaining_one(
    remaining_turn: int,
    trigger: Plan2EndTurnRemainingOneTrigger = EXACT_REMAINING_ONE_TRIGGER,
) -> Plan2EndTurnRemainingOneEvaluation:
    """Evaluate ``signed_remaining_turn <= 1`` without mutating Plan2State."""

    if not isinstance(trigger, Plan2EndTurnRemainingOneTrigger):
        raise TypeError("trigger must be Plan2EndTurnRemainingOneTrigger")
    if trigger != EXACT_REMAINING_ONE_TRIGGER:
        raise Plan2EndTurnRemainingOneContractError(
            "only the exact remaining-one trigger is supported"
        )
    current = _strict_int(remaining_turn, "remaining_turn")
    return Plan2EndTurnRemainingOneEvaluation(
        remaining_turn=current,
        threshold=REMAINING_ONE_THRESHOLD,
        triggered=current <= REMAINING_ONE_THRESHOLD,
    )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingOneProgram:
    """Exact trigger plus an existing native EndTurn listener program."""

    trigger: Plan2EndTurnRemainingOneTrigger
    listener_program: Plan2EndTurnProgram

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, Plan2EndTurnRemainingOneTrigger):
            raise TypeError("trigger must be Plan2EndTurnRemainingOneTrigger")
        if not isinstance(self.listener_program, Plan2EndTurnProgram):
            raise TypeError("listener_program must be Plan2EndTurnProgram")
        program = self.listener_program
        if (
            program.trigger_id != REMAINING_ONE_TRIGGER_ID
            or program.phase_type != END_TURN_PHASE_TYPE
            or program.phase_values
            or program.turn != -1
            or program.effect_count != 0
            or program.effect_value1 != 0
            or len(program.effects) != 1
        ):
            raise Plan2EndTurnRemainingOneContractError(
                "listener program is outside exact remaining-one shape"
            )
        if program.wrapper_effect_group_ids != REMAINING_ONE_WRAPPER_EFFECT_GROUP_IDS:
            raise Plan2EndTurnRemainingOneContractError(
                "wrapper effectGroupIds are outside exact remaining-one shape"
            )
        child = program.effects[0]
        if not isinstance(child, Plan2EndTurnEffect):
            raise Plan2EndTurnRemainingOneContractError(
                "child is not a Plan2EndTurnEffect"
            )
        if (
            child.effect_type != LESSON_DEPEND_BLOCK_EFFECT_TYPE
            or child.value2 != 0
            or child.count != 1
            or child.turn != 0
            or child.effect_group_ids != REMAINING_ONE_CHILD_EFFECT_GROUP_IDS
        ):
            raise Plan2EndTurnRemainingOneContractError(
                "child effect is outside exact remaining-one shape"
            )

    @property
    def wrapper_effect_id(self) -> str:
        return self.listener_program.wrapper_effect_id

    @property
    def status_enchant_id(self) -> str:
        return self.listener_program.status_enchant_id

    @property
    def effects(self):
        return self.listener_program.effects


def _validate_remaining_one_trigger(row: sqlite3.Row) -> Plan2EndTurnRemainingOneTrigger:
    trigger_id = str(row["id"])
    if trigger_id != REMAINING_ONE_TRIGGER_ID:
        raise Plan2EndTurnRemainingOneContractError(
            f"{trigger_id}: expected exact remaining-one trigger"
        )
    array_expectations = (
        ("phase_types_json", [END_TURN_PHASE_TYPE]),
        ("phase_values_json", []),
        ("field_status_check_types_json", []),
        ("field_status_types_json", [REMAINING_TURN_FIELD_TYPE]),
        ("field_status_values_json", [REMAINING_ONE_THRESHOLD]),
        ("field_status_produce_card_search_ids_json", []),
        ("effect_types_json", []),
    )
    arrays: dict[str, tuple[object, ...]] = {}
    for column, expected in array_expectations:
        arrays[column] = _strict_array(row[column], expected, trigger_id)
    if (
        str(row["produce_card_search_id"]) != ""
        or int(row["upper_search_count"]) != 0
        or int(row["lower_search_count"]) != 0
        or str(row["card_move_position_type"])
        != "ProduceCardMovePositionType_Unknown"
        or str(row["lesson_type"]) != "ProduceStepLessonType_Unknown"
    ):
        raise Plan2EndTurnRemainingOneContractError(
            f"{trigger_id}: unsupported search/filter shape"
        )
    raw = _json_object(row["raw_json"], trigger_id)
    _strict_raw_fields(
        raw,
        {
            "id": trigger_id,
            "phaseTypes": [END_TURN_PHASE_TYPE],
            "phaseValues": [],
            "fieldStatusCheckTypes": [],
            "fieldStatusTypes": [REMAINING_TURN_FIELD_TYPE],
            "fieldStatusValues": [REMAINING_ONE_THRESHOLD],
            "fieldStatusProduceCardSearchIds": [],
            "produceCardSearchId": "",
            "upperSearchCount": 0,
            "lowerSearchCount": 0,
            "cardMovePositionType": "ProduceCardMovePositionType_Unknown",
            "effectTypes": [],
            "lessonType": "ProduceStepLessonType_Unknown",
        },
        trigger_id,
    )
    return Plan2EndTurnRemainingOneTrigger(
        trigger_id=trigger_id,
        phase_types=tuple(str(value) for value in arrays["phase_types_json"]),
        phase_values=tuple(
            _strict_int(value, f"{trigger_id}.phaseValue")
            for value in arrays["phase_values_json"]
        ),
        field_status_check_types=tuple(
            str(value) for value in arrays["field_status_check_types_json"]
        ),
        field_status_types=tuple(
            str(value) for value in arrays["field_status_types_json"]
        ),
        field_status_values=tuple(
            _strict_int(value, f"{trigger_id}.fieldStatusValue")
            for value in arrays["field_status_values_json"]
        ),
        field_status_produce_card_search_ids=tuple(
            str(value)
            for value in arrays["field_status_produce_card_search_ids_json"]
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_strict_int(
            int(row["upper_search_count"]), f"{trigger_id}.upperSearchCount"
        ),
        lower_search_count=_strict_int(
            int(row["lower_search_count"]), f"{trigger_id}.lowerSearchCount"
        ),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=tuple(str(value) for value in arrays["effect_types_json"]),
        lesson_type=str(row["lesson_type"]),
    )


def _validate_target_effect_row(
    row: sqlite3.Row,
    *,
    expected_status_id: str,
    expected_groups: tuple[str, ...],
) -> None:
    effect_id = str(row["id"])
    raw = _json_object(row["raw_json"], effect_id)
    _validate_neutral_effect_fields(row, raw)
    _strict_raw_fields(
        raw,
        {
            "id": effect_id,
            "effectType": str(row["effect_type"]),
            "effectValue1": int(row["value1"]),
            "effectValue2": int(row["value2"]),
            "effectCount": int(row["effect_count"]),
            "effectTurn": int(row["effect_turn"]),
            "produceExamStatusEnchantId": expected_status_id,
            "effectGroupIds": list(expected_groups),
        },
        effect_id,
    )
    _strict_equal(
        tuple(_ordered_string_array(raw, "effectGroupIds", effect_id)),
        expected_groups,
        f"{effect_id}.effectGroupIds",
    )


def load_plan2_end_turn_remaining_one_program(
    wrapper_effect_id: str, database: Path = DEFAULT_DATABASE
) -> Plan2EndTurnRemainingOneProgram:
    """Load and strictly validate one exact target StatusEnchant chain."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        wrapper = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (wrapper_effect_id,)
        ).fetchone()
        if wrapper is None:
            raise KeyError(f"unknown Master effect: {wrapper_effect_id}")
        if str(wrapper["effect_type"]) != STATUS_ENCHANT_EFFECT_TYPE:
            raise Plan2EndTurnRemainingOneContractError(
                f"{wrapper_effect_id}: expected {STATUS_ENCHANT_EFFECT_TYPE}"
            )
        status_id = str(wrapper["status_enchant_id"])
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (status_id,)
        ).fetchone()
        if status is None:
            raise Plan2EndTurnRemainingOneContractError(
                f"{wrapper_effect_id}: missing status {status_id}"
            )
        trigger_id = str(status["produce_exam_trigger_id"])
        if trigger_id != REMAINING_ONE_TRIGGER_ID:
            raise Plan2EndTurnRemainingOneContractError(
                f"{status_id}: expected {REMAINING_ONE_TRIGGER_ID}, got {trigger_id}"
            )
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if trigger is None:
            raise Plan2EndTurnRemainingOneContractError(
                f"{status_id}: missing trigger {trigger_id}"
            )
        typed_trigger = _validate_remaining_one_trigger(trigger)
        child_ids = tuple(
            str(value)
            for value in _json_array(
                status["produce_exam_effect_ids_json"], status_id
            )
        )
        if len(child_ids) != 1 or not child_ids[0]:
            raise Plan2EndTurnRemainingOneContractError(
                f"{status_id}: exact target requires one child effect"
            )
        child_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (child_ids[0],)
        ).fetchone()
        if child_row is None:
            raise Plan2EndTurnRemainingOneContractError(
                f"{status_id}: missing child effect {child_ids[0]}"
            )
        _validate_target_effect_row(
            child_row,
            expected_status_id="",
            expected_groups=REMAINING_ONE_CHILD_EFFECT_GROUP_IDS,
        )
        child = _load_child(child_row)

        wrapper_raw = _json_object(wrapper["raw_json"], wrapper_effect_id)
        _validate_target_effect_row(
            wrapper,
            expected_status_id=status_id,
            expected_groups=REMAINING_ONE_WRAPPER_EFFECT_GROUP_IDS,
        )
        if (
            int(wrapper["value1"]) != 0
            or int(wrapper["value2"]) != 0
            or int(wrapper["effect_count"]) != 0
            or int(wrapper["effect_turn"]) != -1
            or str(wrapper["chain_effect_id"]) != ""
        ):
            raise Plan2EndTurnRemainingOneContractError(
                f"{wrapper_effect_id}: unsupported wrapper count/value shape"
            )
        _strict_raw_fields(
            wrapper_raw,
            {
                "produceExamStatusEnchantId": status_id,
                "chainProduceExamEffectId": "",
                "chainProduceExamEffectIds": [],
            },
            wrapper_effect_id,
        )
        status_raw = _json_object(status["raw_json"], status_id)
        _strict_raw_fields(
            status_raw,
            {
                "id": status_id,
                "assetId": str(status["asset_id"]),
                "produceExamTriggerId": trigger_id,
                "produceExamEffectIds": list(child_ids),
            },
            status_id,
        )

    listener_program = Plan2EndTurnProgram(
        wrapper_effect_id=wrapper_effect_id,
        status_enchant_id=status_id,
        trigger_id=trigger_id,
        phase_type=END_TURN_PHASE_TYPE,
        phase_values=typed_trigger.phase_values,
        effects=(child,),
        wrapper_effect_group_ids=REMAINING_ONE_WRAPPER_EFFECT_GROUP_IDS,
        turn=-1,
        effect_count=0,
        effect_value1=0,
    )
    return Plan2EndTurnRemainingOneProgram(typed_trigger, listener_program)


def install_plan2_end_turn_remaining_one_listener(
    state: Plan2State, program: Plan2EndTurnRemainingOneProgram
) -> Plan2EndTurnInstallTransition:
    """Delegate installation to the existing native EndTurn listener runtime."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnRemainingOneProgram):
        raise TypeError("program must be Plan2EndTurnRemainingOneProgram")
    return simulate_install_plan2_end_turn_listener(
        state, program.listener_program
    )


__all__ = [
    "EXACT_REMAINING_ONE_TRIGGER",
    "REMAINING_ONE_CHILD_EFFECT_GROUP_IDS",
    "REMAINING_ONE_THRESHOLD",
    "REMAINING_ONE_TRIGGER_ID",
    "REMAINING_ONE_WRAPPER_EFFECT_GROUP_IDS",
    "REMAINING_TURN_FIELD_TYPE",
    "Plan2EndTurnRemainingOneContractError",
    "Plan2EndTurnRemainingOneEvaluation",
    "Plan2EndTurnRemainingOneProgram",
    "Plan2EndTurnRemainingOneTrigger",
    "evaluate_plan2_end_turn_remaining_one",
    "install_plan2_end_turn_remaining_one_listener",
    "load_plan2_end_turn_remaining_one_program",
]
