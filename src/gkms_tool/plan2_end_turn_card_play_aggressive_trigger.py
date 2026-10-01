"""Exact Plan2 ``ExamEndTurn`` / ``CardPlayAggressiveUp >= 6`` adapter.

This module owns one narrow status-trigger gate only:

* ``e_trigger-exam_end_turn-card_play_aggressive_up-6``;
* the target ``ExamStatusEnchant`` wrapper;
* its one ``ExamLessonDependExamReview(800)`` child.

The predicate reads the signed *current* Aggressive status value at the
``ExamEndTurn`` phase.  It does not invent a per-turn accumulator, an event
delta, or a second listener runtime.  Installation delegates to the existing
``plan2_end_turn_trigger`` listener adapter; unknown trigger, wrapper, child,
or group shapes fail closed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import (
    AggressiveTriggerRow,
    CHECK_NOT,
    FIELD_CARD_PLAY_AGGRESSIVE_UP,
    LESSON_UNKNOWN,
    MOVE_UNKNOWN,
)
from .plan2_end_turn_trigger import (
    END_TURN_PHASE_TYPE,
    STATUS_ENCHANT_EFFECT_TYPE,
    Plan2EndTurnContractError,
    Plan2EndTurnInstallTransition,
    Plan2EndTurnProgram,
    _json_array,
    _json_object,
    _ordered_string_array,
    _validate_neutral_effect_fields,
    simulate_install_plan2_end_turn_listener,
)
from .plan2_state import Plan2EndTurnEffect, Plan2State


TARGET_TRIGGER_ID = "e_trigger-exam_end_turn-card_play_aggressive_up-6"
TARGET_THRESHOLD = 6
TARGET_CARD_ID = "p_card-02-ido-2_107"
TARGET_CARD_UPGRADES = (0, 1, 2, 3)
TARGET_STATUS_ENCHANT_ID = "enchant-p_card-02-ido-2_107-enc01"
TARGET_WRAPPER_EFFECT_ID = (
    "e_effect-exam_status_enchant-04-inf-enchant-"
    "p_card-02-ido-2_107-enc01"
)
TARGET_CHILD_EFFECT_ID = "e_effect-exam_lesson_depend_exam_review-0800-01"
TARGET_CHILD_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)

TARGET_CHILD_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_lesson_depend_exam_review-000",
    "effect_group-visible-exam_lesson-000",
)
TARGET_WRAPPER_EFFECT_GROUP_IDS = (
    *TARGET_CHILD_EFFECT_GROUP_IDS,
    "effect_group-visible-exam_status_enchant-000",
)


class Plan2EndTurnCardPlayAggressiveContractError(Plan2EndTurnContractError):
    """A Master row is outside the exact target contract."""


class EndTurnEvaluationStage(str, Enum):
    """Native ordering points relevant to this field-status gate."""

    FIELD_GATE = "exam-end-turn-field-gate"
    INTERVAL = "exam-end-turn-interval"
    TIMER = "exam-end-turn-timer"
    TURN_CHECK_REVIEW = "turn-check-review"
    TURN_START_DECREMENT = "turn-start-decrement"


EXACT_TARGET_TRIGGER = AggressiveTriggerRow(
    id=TARGET_TRIGGER_ID,
    phase_types=(END_TURN_PHASE_TYPE,),
    phase_values=(),
    field_status_check_types=(),
    field_status_types=(FIELD_CARD_PLAY_AGGRESSIVE_UP,),
    field_status_values=(TARGET_THRESHOLD,),
    field_status_card_search_ids=(),
    produce_card_search_id="",
    upper_search_count=0,
    lower_search_count=0,
    card_move_position_type=MOVE_UNKNOWN,
    effect_types=(),
    lesson_type=LESSON_UNKNOWN,
)


ANDROID_END_TURN_CARD_PLAY_AGGRESSIVE_EVIDENCE = {
    "field": {
        "enum_symbol": "ProduceExamFieldStatusType.CardPlayAggressiveUp",
        "enum_value": 42,
        "predicate_symbol": "ExamExtensions.IsEffectTriggerFieldValid",
        "predicate_rva": "0x68072F4",
        "status_helper_symbol": "ExamExtensions.IsFieldStatusTriggerStatusEffect",
        "status_helper_rva": "0x68082D4",
        "getter_symbol": "ExamStatusEffectCollection.GetAggressive",
        "getter_rva": "0x7E99350",
        "fact": "read the current signed Aggressive status value",
        "comparison": "signed current_value >= 6; inclusive equality",
        "not_shape": "the exact target has no Not check",
    },
    "install": {
        "executor_symbol": "StatusEnchantEffectExecutor.ExecuteEffect",
        "executor_rva": "0x7E8FA08",
        "listener_symbol": "ExamStatusEffectCollection.TryAddTriggerEffectStatus",
        "listener_rva": "0x7E8FEE0",
        "constructor_rva": "0x7EADF04",
        "fact": "one fresh TriggerEffectStatusEffect is appended per wrapper execution",
    },
    "listener": {
        "filter_symbol": "ExamStatusEffectCollection.GetTriggerEffectList",
        "filter_rva": "0x7EA1D74",
        "count_symbol": "TriggerEffectStatusEffect.SpendCount",
        "count_rva": "0x7EAE70C",
        "remove_symbol": "ExamStatusEffectCollection.RemoveCountLimitTriggeredStatus",
        "remove_rva": "0x7EA52B0",
        "fact": "children run in active/child order, then one count spend; total zero removes",
    },
    "phase": {
        "loop_symbol": "ExamSequence.<ExamLoopTaskAsync>d__94.MoveNext",
        "loop_rva": "0x7EDCE78",
        "fact": (
            "ExamEndTurn field evaluation precedes EndTurnInterval, "
            "EndTurnTimer, TurnCheck Review, and later TurnStart spending"
        ),
    },
    "child": {
        "effect_type": TARGET_CHILD_EFFECT_TYPE,
        "executor_symbol": "LessonDependExamReviewEffectExecutor",
        "executor_rva": "0x7E82DD4",
        "factory_rva": "0x7E5D8FC",
        "fact": "the child uses the current Review value at execution time",
    },
}


def _strict_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{label} must be a plain integer"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{label} is outside Int32"
        )
    return value


def _strict_equal(
    actual: object, expected: object, label: str
) -> None:
    """Compare JSON values without Python bool/int coercion."""

    if type(actual) is not type(expected):
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )
    if isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{label} must be {expected!r}, got {actual!r}"
            )
        for index, (item, wanted) in enumerate(zip(actual, expected)):
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if actual != expected:
        raise Plan2EndTurnCardPlayAggressiveContractError(
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
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{row_id}: missing raw field {key}"
            )
        _strict_equal(raw[key], value, f"{row_id}.{key}")


def _unsupported_evaluation(
    trigger_id: str,
    inputs: "Plan2EndTurnCardPlayAggressiveEvaluationInput",
    reasons: tuple[str, ...],
) -> "Plan2EndTurnCardPlayAggressiveEvaluation":
    return Plan2EndTurnCardPlayAggressiveEvaluation(
        trigger_id=trigger_id,
        supported=False,
        fires=None,
        aggressive_status_value=None,
        threshold=None,
        comparison=None,
        check_not=None,
        phase=inputs.phase,
        evaluation_stage=inputs.evaluation_stage,
        reasons=reasons,
    )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnCardPlayAggressiveEvaluationInput:
    """Read-only native inputs; counters/deltas/origin are intentionally inert."""

    aggressive_status_value: int
    phase: str = END_TURN_PHASE_TYPE
    evaluation_stage: EndTurnEvaluationStage = EndTurnEvaluationStage.FIELD_GATE
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    observed_aggressive_delta: int | None = None
    card_origin: Literal["normal", "forced", "extra"] = "normal"

    def __post_init__(self) -> None:
        _strict_int(self.aggressive_status_value, "aggressive_status_value")
        for label in ("global_card_play_count", "turn_card_play_count"):
            value = _strict_int(getattr(self, label), label)
            if value < 0:
                raise Plan2EndTurnCardPlayAggressiveContractError(
                    f"{label} must be non-negative"
                )
        if self.observed_aggressive_delta is not None:
            _strict_int(self.observed_aggressive_delta, "observed_aggressive_delta")
        if not isinstance(self.phase, str) or not self.phase:
            raise TypeError("phase must be a non-empty string")
        if not isinstance(self.evaluation_stage, EndTurnEvaluationStage):
            object.__setattr__(
                self,
                "evaluation_stage",
                EndTurnEvaluationStage(self.evaluation_stage),
            )
        if self.card_origin not in ("normal", "forced", "extra"):
            raise Plan2EndTurnCardPlayAggressiveContractError(
                "card_origin must be normal, forced, or extra"
            )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnCardPlayAggressiveEvaluation:
    """Result of the exact signed field predicate."""

    trigger_id: str
    supported: bool
    fires: bool | None
    aggressive_status_value: int | None
    threshold: int | None
    comparison: Literal["signed_greater_equal"] | None
    check_not: bool | None
    phase: str
    evaluation_stage: EndTurnEvaluationStage
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def result_expression(self) -> str | None:
        if self.threshold is None or self.check_not is None:
            return None
        expression = f"signed_aggressive_value >= {self.threshold}"
        return f"not ({expression})" if self.check_not else expression

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "aggressiveStatusValue": self.aggressive_status_value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "checkNot": self.check_not,
            "resultExpression": self.result_expression,
            "phase": self.phase,
            "evaluationStage": self.evaluation_stage.value,
            "reasons": list(self.reasons),
        }


def evaluate_plan2_end_turn_card_play_aggressive_trigger(
    inputs: Plan2EndTurnCardPlayAggressiveEvaluationInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
) -> Plan2EndTurnCardPlayAggressiveEvaluation:
    """Evaluate ``signed current Aggressive >= 6`` before EndTurn follow-ups."""

    if not isinstance(inputs, Plan2EndTurnCardPlayAggressiveEvaluationInput):
        raise TypeError(
            "inputs must be Plan2EndTurnCardPlayAggressiveEvaluationInput"
        )
    if not isinstance(trigger, AggressiveTriggerRow):
        raise TypeError("trigger must be AggressiveTriggerRow")
    if trigger != EXACT_TARGET_TRIGGER:
        return _unsupported_evaluation(
            trigger.id,
            inputs,
            ("only-exact-end-turn-card-play-aggressive-up-6-is-supported",),
        )
    reasons: list[str] = []
    if inputs.phase != END_TURN_PHASE_TYPE:
        reasons.append("runtime-phase-is-not-exam-end-turn")
    if inputs.evaluation_stage is not EndTurnEvaluationStage.FIELD_GATE:
        reasons.append("field-gate-is-only-evaluated-at-exam-end-turn")
    if reasons:
        return Plan2EndTurnCardPlayAggressiveEvaluation(
            trigger_id=TARGET_TRIGGER_ID,
            supported=False,
            fires=None,
            aggressive_status_value=None,
            threshold=TARGET_THRESHOLD,
            comparison="signed_greater_equal",
            check_not=False,
            phase=inputs.phase,
            evaluation_stage=inputs.evaluation_stage,
            reasons=tuple(reasons),
        )
    current = inputs.aggressive_status_value
    return Plan2EndTurnCardPlayAggressiveEvaluation(
        trigger_id=TARGET_TRIGGER_ID,
        supported=True,
        fires=current >= TARGET_THRESHOLD,
        aggressive_status_value=current,
        threshold=TARGET_THRESHOLD,
        comparison="signed_greater_equal",
        check_not=False,
        phase=inputs.phase,
        evaluation_stage=inputs.evaluation_stage,
    )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnCardPlayAggressiveProgram:
    """Exact trigger plus a program for the existing EndTurn listener."""

    trigger: AggressiveTriggerRow
    listener_program: Plan2EndTurnProgram

    def __post_init__(self) -> None:
        if self.trigger != EXACT_TARGET_TRIGGER:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                "only the exact target trigger is supported"
            )
        if not isinstance(self.listener_program, Plan2EndTurnProgram):
            raise TypeError("listener_program must be Plan2EndTurnProgram")
        program = self.listener_program
        if (
            program.wrapper_effect_id != TARGET_WRAPPER_EFFECT_ID
            or program.status_enchant_id != TARGET_STATUS_ENCHANT_ID
            or program.trigger_id != TARGET_TRIGGER_ID
            or program.phase_type != END_TURN_PHASE_TYPE
            or program.phase_values
            or program.turn != -1
            or program.effect_count != 4
            or program.effect_value1 != 0
            or len(program.effects) != 1
        ):
            raise Plan2EndTurnCardPlayAggressiveContractError(
                "listener program is outside the exact target shape"
            )
        if program.wrapper_effect_group_ids != TARGET_WRAPPER_EFFECT_GROUP_IDS:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                "wrapper effectGroupIds are outside the exact target shape"
            )
        child = program.effects[0]
        if (
            child.effect_id != TARGET_CHILD_EFFECT_ID
            or child.effect_type != TARGET_CHILD_EFFECT_TYPE
            or child.value1 != 800
            or child.value2 != 0
            or child.count != 1
            or child.turn != 0
            or child.effect_group_ids != TARGET_CHILD_EFFECT_GROUP_IDS
        ):
            raise Plan2EndTurnCardPlayAggressiveContractError(
                "child effect is outside the exact target shape"
            )

    @property
    def wrapper_effect_id(self) -> str:
        return self.listener_program.wrapper_effect_id

    @property
    def status_enchant_id(self) -> str:
        return self.listener_program.status_enchant_id

    @property
    def effects(self) -> tuple[Plan2EndTurnEffect, ...]:
        return self.listener_program.effects

    @property
    def native_total_limit(self) -> int:
        return self.listener_program.native_limit_count

    @property
    def native_per_turn_limit(self) -> int:
        return self.listener_program.native_limit_count_in_turn


def _validate_target_trigger(row: sqlite3.Row) -> AggressiveTriggerRow:
    trigger_id = str(row["id"])
    if trigger_id != TARGET_TRIGGER_ID:
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{trigger_id}: expected exact target trigger"
        )
    arrays = {
        "phase_types_json": _strict_array(
            row["phase_types_json"], [END_TURN_PHASE_TYPE], trigger_id
        ),
        "phase_values_json": _strict_array(
            row["phase_values_json"], [], trigger_id
        ),
        "field_status_check_types_json": _strict_array(
            row["field_status_check_types_json"], [], trigger_id
        ),
        "field_status_types_json": _strict_array(
            row["field_status_types_json"], [FIELD_CARD_PLAY_AGGRESSIVE_UP], trigger_id
        ),
        "field_status_values_json": _strict_array(
            row["field_status_values_json"], [TARGET_THRESHOLD], trigger_id
        ),
        "field_status_produce_card_search_ids_json": _strict_array(
            row["field_status_produce_card_search_ids_json"], [], trigger_id
        ),
        "effect_types_json": _strict_array(
            row["effect_types_json"], [], trigger_id
        ),
    }
    produce_card_search_id = str(row["produce_card_search_id"])
    upper_search_count = _strict_int(
        row["upper_search_count"], f"{trigger_id}.upper_search_count"
    )
    lower_search_count = _strict_int(
        row["lower_search_count"], f"{trigger_id}.lower_search_count"
    )
    card_move_position_type = str(row["card_move_position_type"])
    lesson_type = str(row["lesson_type"])
    if (
        produce_card_search_id != ""
        or upper_search_count != 0
        or lower_search_count != 0
        or card_move_position_type != MOVE_UNKNOWN
        or lesson_type != LESSON_UNKNOWN
    ):
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{trigger_id}: unsupported search/filter shape"
        )
    raw = _json_object(row["raw_json"], trigger_id)
    _strict_raw_fields(
        raw,
        {
            "id": TARGET_TRIGGER_ID,
            "phaseTypes": [END_TURN_PHASE_TYPE],
            "phaseValues": [],
            "fieldStatusCheckTypes": [],
            "fieldStatusTypes": [FIELD_CARD_PLAY_AGGRESSIVE_UP],
            "fieldStatusValues": [TARGET_THRESHOLD],
            "fieldStatusProduceCardSearchIds": [],
            "produceCardSearchId": "",
            "upperSearchCount": 0,
            "lowerSearchCount": 0,
            "cardMovePositionType": MOVE_UNKNOWN,
            "effectTypes": [],
            "lessonType": LESSON_UNKNOWN,
        },
        trigger_id,
    )
    candidate = AggressiveTriggerRow(
        id=trigger_id,
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
        field_status_card_search_ids=tuple(
            str(value)
            for value in arrays["field_status_produce_card_search_ids_json"]
        ),
        produce_card_search_id=produce_card_search_id,
        upper_search_count=upper_search_count,
        lower_search_count=lower_search_count,
        card_move_position_type=card_move_position_type,
        effect_types=tuple(str(value) for value in arrays["effect_types_json"]),
        lesson_type=lesson_type,
    )
    if candidate != EXACT_TARGET_TRIGGER:
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{trigger_id}: target trigger does not match the typed exact row"
        )
    return candidate


def _validate_target_effect_row(
    row: sqlite3.Row,
    *,
    expected_effect_id: str,
    expected_effect_type: str,
    expected_value1: int,
    expected_value2: int,
    expected_count: int,
    expected_turn: int,
    expected_status_id: str,
    expected_groups: tuple[str, ...],
) -> Plan2EndTurnEffect:
    effect_id = str(row["id"])
    if effect_id != expected_effect_id:
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{effect_id}: expected exact target effect {expected_effect_id}"
        )
    raw = _json_object(row["raw_json"], effect_id)
    _validate_neutral_effect_fields(row, raw)
    expected = {
        "id": effect_id,
        "effectType": expected_effect_type,
        "effectValue1": expected_value1,
        "effectValue2": expected_value2,
        "effectCount": expected_count,
        "effectTurn": expected_turn,
        "produceExamStatusEnchantId": expected_status_id,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "effectGroupIds": list(expected_groups),
    }
    _strict_raw_fields(raw, expected, effect_id)
    groups = _ordered_string_array(raw, "effectGroupIds", effect_id)
    if groups != expected_groups:
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{effect_id}: effectGroupIds order is not exact"
        )
    values = {
        "value1": _strict_int(row["value1"], f"{effect_id}.value1"),
        "value2": _strict_int(row["value2"], f"{effect_id}.value2"),
        "effect_count": _strict_int(row["effect_count"], f"{effect_id}.effect_count"),
        "effect_turn": _strict_int(row["effect_turn"], f"{effect_id}.effect_turn"),
    }
    if (
        str(row["effect_type"]) != expected_effect_type
        or values["value1"] != expected_value1
        or values["value2"] != expected_value2
        or values["effect_count"] != expected_count
        or values["effect_turn"] != expected_turn
        or str(row["chain_effect_id"]) != ""
    ):
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"{effect_id}: scalar effect shape is not exact"
        )
    return Plan2EndTurnEffect(
        effect_id=effect_id,
        effect_type=expected_effect_type,
        value1=expected_value1,
        value2=expected_value2,
        count=expected_count,
        turn=expected_turn,
        effect_group_ids=expected_groups,
    )


def load_plan2_end_turn_card_play_aggressive_program(
    wrapper_effect_id: str = TARGET_WRAPPER_EFFECT_ID,
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2EndTurnCardPlayAggressiveProgram:
    """Load and validate only the target wrapper/status/trigger/child chain."""

    if wrapper_effect_id != TARGET_WRAPPER_EFFECT_ID:
        raise Plan2EndTurnCardPlayAggressiveContractError(
            f"unsupported target wrapper: {wrapper_effect_id}"
        )
    path = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        wrapper = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (wrapper_effect_id,)
        ).fetchone()
        if wrapper is None:
            raise KeyError(f"unknown Master effect: {wrapper_effect_id}")
        if str(wrapper["effect_type"]) != STATUS_ENCHANT_EFFECT_TYPE:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{wrapper_effect_id}: expected {STATUS_ENCHANT_EFFECT_TYPE}"
            )
        status_id = str(wrapper["status_enchant_id"])
        if status_id != TARGET_STATUS_ENCHANT_ID:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{wrapper_effect_id}: unexpected status {status_id}"
            )
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (status_id,)
        ).fetchone()
        if status is None:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{wrapper_effect_id}: missing status {status_id}"
            )
        trigger_id = str(status["produce_exam_trigger_id"])
        if trigger_id != TARGET_TRIGGER_ID:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{status_id}: unexpected trigger {trigger_id}"
            )
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if trigger_row is None:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{status_id}: missing trigger {trigger_id}"
            )
        trigger = _validate_target_trigger(trigger_row)
        child_ids = tuple(
            str(value)
            for value in _json_array(status["produce_exam_effect_ids_json"], status_id)
        )
        if child_ids != (TARGET_CHILD_EFFECT_ID,):
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{status_id}: child order/identity is not exact"
            )
        child_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (TARGET_CHILD_EFFECT_ID,)
        ).fetchone()
        if child_row is None:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                f"{status_id}: missing child effect {TARGET_CHILD_EFFECT_ID}"
            )
        child = _validate_target_effect_row(
            child_row,
            expected_effect_id=TARGET_CHILD_EFFECT_ID,
            expected_effect_type=TARGET_CHILD_EFFECT_TYPE,
            expected_value1=800,
            expected_value2=0,
            expected_count=1,
            expected_turn=0,
            expected_status_id="",
            expected_groups=TARGET_CHILD_EFFECT_GROUP_IDS,
        )
        wrapper_raw = _json_object(wrapper["raw_json"], wrapper_effect_id)
        wrapper_from_row = _validate_target_effect_row(
            wrapper,
            expected_effect_id=TARGET_WRAPPER_EFFECT_ID,
            expected_effect_type=STATUS_ENCHANT_EFFECT_TYPE,
            expected_value1=0,
            expected_value2=0,
            expected_count=4,
            expected_turn=-1,
            expected_status_id=TARGET_STATUS_ENCHANT_ID,
            expected_groups=TARGET_WRAPPER_EFFECT_GROUP_IDS,
        )
        _strict_raw_fields(
            wrapper_raw,
            {
                "produceExamStatusEnchantId": TARGET_STATUS_ENCHANT_ID,
                "chainProduceExamEffectId": "",
                "chainProduceExamEffectIds": [],
            },
            wrapper_effect_id,
        )
        status_raw = _json_object(status["raw_json"], status_id)
        _strict_raw_fields(
            status_raw,
            {
                "id": TARGET_STATUS_ENCHANT_ID,
                "assetId": str(status["asset_id"]),
                "produceExamTriggerId": TARGET_TRIGGER_ID,
                "produceExamEffectIds": [TARGET_CHILD_EFFECT_ID],
            },
            status_id,
        )
        if wrapper_from_row.effect_type != STATUS_ENCHANT_EFFECT_TYPE:
            raise Plan2EndTurnCardPlayAggressiveContractError(
                "wrapper effect did not retain its exact type"
            )

        listener_program = Plan2EndTurnProgram(
            wrapper_effect_id=TARGET_WRAPPER_EFFECT_ID,
            status_enchant_id=TARGET_STATUS_ENCHANT_ID,
            trigger_id=TARGET_TRIGGER_ID,
            phase_type=END_TURN_PHASE_TYPE,
            phase_values=(),
            effects=(child,),
            wrapper_effect_group_ids=TARGET_WRAPPER_EFFECT_GROUP_IDS,
            turn=-1,
            effect_count=4,
            effect_value1=0,
        )
    return Plan2EndTurnCardPlayAggressiveProgram(trigger, listener_program)


def install_plan2_end_turn_card_play_aggressive_listener(
    state: Plan2State,
    program: Plan2EndTurnCardPlayAggressiveProgram,
) -> Plan2EndTurnInstallTransition:
    """Delegate installation to the existing EndTurn listener primitive."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnCardPlayAggressiveProgram):
        raise TypeError(
            "program must be Plan2EndTurnCardPlayAggressiveProgram"
        )
    return simulate_install_plan2_end_turn_listener(
        state, program.listener_program
    )


__all__ = [
    "ANDROID_END_TURN_CARD_PLAY_AGGRESSIVE_EVIDENCE",
    "END_TURN_PHASE_TYPE",
    "EndTurnEvaluationStage",
    "EXACT_TARGET_TRIGGER",
    "TARGET_CARD_ID",
    "TARGET_CARD_UPGRADES",
    "TARGET_CHILD_EFFECT_GROUP_IDS",
    "TARGET_CHILD_EFFECT_ID",
    "TARGET_CHILD_EFFECT_TYPE",
    "TARGET_STATUS_ENCHANT_ID",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "TARGET_WRAPPER_EFFECT_GROUP_IDS",
    "TARGET_WRAPPER_EFFECT_ID",
    "Plan2EndTurnCardPlayAggressiveContractError",
    "Plan2EndTurnCardPlayAggressiveEvaluation",
    "Plan2EndTurnCardPlayAggressiveEvaluationInput",
    "Plan2EndTurnCardPlayAggressiveProgram",
    "evaluate_plan2_end_turn_card_play_aggressive_trigger",
    "install_plan2_end_turn_card_play_aggressive_listener",
    "load_plan2_end_turn_card_play_aggressive_program",
]
