"""Exact Plan2 ``ExamEndTurn`` / ``RemainingTurn <= 3`` adapter.

This is the narrow Plan2/Common companion to
``plan2_end_turn_remaining_one``.  The field predicate is evaluated against
the signed current remaining-turn value before the exam-loop decrement.  The
normal StatusEnchant installation/lifetime boundary is delegated to the
existing EndTurn listener runtime; this module only adds the exact threshold,
Master chain, and the one direct Plan2 child family.

The target child is ``ExamLessonDependExamReview``.  Its typed child formula
is exposed as a pure adapter, while the existing generic EndTurn evaluator is
left unchanged and continues to reject unknown child types.  Encore/item
rows sharing the trigger are catalog-only and never resolve through this
module.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    NativeFormulaDomainError,
    get_ratio_effect_int_value,
)
from .plan2_end_turn_remaining_one import REMAINING_TURN_FIELD_TYPE
from .plan2_end_turn_trigger import (
    END_TURN_PHASE_TYPE,
    Plan2EndTurnContractError,
    Plan2EndTurnInstallTransition,
    Plan2EndTurnProgram,
    STATUS_ENCHANT_EFFECT_TYPE,
    _json_array,
    _json_object,
    _validate_neutral_effect_fields,
    simulate_install_plan2_end_turn_listener,
)
from .plan2_state import Plan2EndTurnEffect, Plan2State


TARGET_CARD_ID = "p_card-02-ido-3_100"
TARGET_UPGRADES = (0, 1, 2, 3)
REMAINING_THREE_TRIGGER_ID = "e_trigger-exam_end_turn-remaining_turn-3"
REMAINING_THREE_THRESHOLD = 3
LESSON_DEPEND_EXAM_REVIEW_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)

REMAINING_THREE_CHILD_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_lesson_depend_exam_review-000",
    "effect_group-visible-exam_lesson-000",
)
REMAINING_THREE_WRAPPER_EFFECT_GROUP_IDS = (
    *REMAINING_THREE_CHILD_EFFECT_GROUP_IDS,
    "effect_group-visible-exam_status_enchant-000",
)

# This is the only Plan2 direct bundle.  The same trigger has one Encore
# wrapper and six item status rows in the local Master; those are intentionally
# not included in the resolver set.
DIRECT_TARGETS: dict[str, tuple[int, str, str, int]] = {
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_100-enc01": (
        0,
        "enchant-p_card-02-ido-3_100-enc01",
        "e_effect-exam_lesson_depend_exam_review-1400-01",
        1400,
    ),
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_100-enc02": (
        1,
        "enchant-p_card-02-ido-3_100-enc02",
        "e_effect-exam_lesson_depend_exam_review-1800-01",
        1800,
    ),
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_100-enc03": (
        2,
        "enchant-p_card-02-ido-3_100-enc03",
        "e_effect-exam_lesson_depend_exam_review-2100-01",
        2100,
    ),
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_100-enc04": (
        3,
        "enchant-p_card-02-ido-3_100-enc04",
        "e_effect-exam_lesson_depend_exam_review-2300-01",
        2300,
    ),
}
DIRECT_WRAPPER_EFFECT_IDS = tuple(DIRECT_TARGETS)
DIRECT_UPGRADES = TARGET_UPGRADES
DIRECT_CHILD_EFFECT_IDS = tuple(item[2] for item in DIRECT_TARGETS.values())
DIRECT_CHILD_VALUES = tuple(item[3] for item in DIRECT_TARGETS.values())

# The decrement is a later ExamLoop continuation: the target child and the
# listener count spend finish first, then Interval/Timer are queued, followed
# by TurnCheck.Review.  This records the native order without reimplementing
# the generic EndTurn evaluator.
NATIVE_REMAINING_THREE_ORDER = (
    END_TURN_PHASE_TYPE,
    "child:ProduceExamEffectType_ExamLessonDependExamReview",
    "listener-count:spend-after-child",
    "remaining-turn:decrement",
    "ProduceExamPhaseType_ExamEndTurnInterval",
    "ProduceExamPhaseType_ExamEndTurnTimer",
    "ExamPlayCommandType_TurnCheck.Review",
    "next:ProduceExamPhaseType_ExamStartTurn",
)
END_TURN_REMAINING_THREE_ORDER = NATIVE_REMAINING_THREE_ORDER


class Plan2EndTurnRemainingThreeContractError(Plan2EndTurnContractError):
    """A Master row is outside the exact remaining-three contract."""


def _strict_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise Plan2EndTurnRemainingThreeContractError(
            f"{label} must be a plain integer"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2EndTurnRemainingThreeContractError(
            f"{label} is outside Int32"
        )
    return value


def _strict_equal(actual: object, expected: object, label: str) -> None:
    """Compare JSON values without Python bool/int coercion."""

    if type(actual) is not type(expected):
        raise Plan2EndTurnRemainingThreeContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )
    if isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2EndTurnRemainingThreeContractError(
                f"{label} must be {expected!r}, got {actual!r}"
            )
        for index, (item, wanted) in enumerate(zip(actual, expected)):
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if actual != expected:
        raise Plan2EndTurnRemainingThreeContractError(
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
            raise Plan2EndTurnRemainingThreeContractError(
                f"{row_id}: missing raw field {key}"
            )
        _strict_equal(raw[key], value, f"{row_id}.{key}")


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingThreeTrigger:
    """The fully typed, exact Master trigger shape for threshold three."""

    trigger_id: str = REMAINING_THREE_TRIGGER_ID
    phase_types: tuple[str, ...] = (END_TURN_PHASE_TYPE,)
    phase_values: tuple[int, ...] = ()
    field_status_check_types: tuple[str, ...] = ()
    field_status_types: tuple[str, ...] = (REMAINING_TURN_FIELD_TYPE,)
    field_status_values: tuple[int, ...] = (REMAINING_THREE_THRESHOLD,)
    field_status_produce_card_search_ids: tuple[str, ...] = ()
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = "ProduceCardMovePositionType_Unknown"
    effect_types: tuple[str, ...] = ()
    lesson_type: str = "ProduceStepLessonType_Unknown"

    def __post_init__(self) -> None:
        if self.trigger_id != REMAINING_THREE_TRIGGER_ID:
            raise Plan2EndTurnRemainingThreeContractError(
                "trigger_id must be e_trigger-exam_end_turn-remaining_turn-3"
            )
        exact = (
            ("phase_types", (END_TURN_PHASE_TYPE,)),
            ("phase_values", ()),
            ("field_status_check_types", ()),
            ("field_status_types", (REMAINING_TURN_FIELD_TYPE,)),
            ("field_status_values", (REMAINING_THREE_THRESHOLD,)),
            ("field_status_produce_card_search_ids", ()),
            ("effect_types", ()),
        )
        for label, expected in exact:
            if getattr(self, label) != expected:
                raise Plan2EndTurnRemainingThreeContractError(
                    f"{label} has unknown shape: {getattr(self, label)!r}"
                )
        if self.produce_card_search_id != "":
            raise Plan2EndTurnRemainingThreeContractError(
                "produce_card_search_id must be empty"
            )
        if self.card_move_position_type != "ProduceCardMovePositionType_Unknown":
            raise Plan2EndTurnRemainingThreeContractError(
                "card_move_position_type must be Unknown"
            )
        if self.lesson_type != "ProduceStepLessonType_Unknown":
            raise Plan2EndTurnRemainingThreeContractError(
                "lesson_type must be Unknown"
            )
        _strict_int(self.upper_search_count, "upper_search_count")
        _strict_int(self.lower_search_count, "lower_search_count")
        if self.upper_search_count != 0 or self.lower_search_count != 0:
            raise Plan2EndTurnRemainingThreeContractError(
                "search counts must be zero"
            )


EXACT_REMAINING_THREE_TRIGGER = Plan2EndTurnRemainingThreeTrigger()


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingThreeEvaluation:
    """Result of the native signed threshold predicate."""

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
        if self.threshold != REMAINING_THREE_THRESHOLD:
            raise Plan2EndTurnRemainingThreeContractError(
                "threshold must be three"
            )
        if type(self.triggered) is not bool:
            raise TypeError("triggered must be a boolean")
        if self.comparison != "signed_less_equal":
            raise Plan2EndTurnRemainingThreeContractError(
                "comparison must be signed_less_equal"
            )
        if self.evaluation_point != "before_exam_end_turn_decrement":
            raise Plan2EndTurnRemainingThreeContractError(
                "evaluation point must be before decrement"
            )


def evaluate_plan2_end_turn_remaining_three(
    remaining_turn: int,
    trigger: Plan2EndTurnRemainingThreeTrigger = EXACT_REMAINING_THREE_TRIGGER,
) -> Plan2EndTurnRemainingThreeEvaluation:
    """Evaluate ``signed_remaining_turn <= 3`` without mutating state."""

    if not isinstance(trigger, Plan2EndTurnRemainingThreeTrigger):
        raise TypeError("trigger must be Plan2EndTurnRemainingThreeTrigger")
    if trigger != EXACT_REMAINING_THREE_TRIGGER:
        raise Plan2EndTurnRemainingThreeContractError(
            "only the exact remaining-three trigger is supported"
        )
    current = _strict_int(remaining_turn, "remaining_turn")
    return Plan2EndTurnRemainingThreeEvaluation(
        remaining_turn=current,
        threshold=REMAINING_THREE_THRESHOLD,
        triggered=current <= REMAINING_THREE_THRESHOLD,
    )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingThreeEvaluationTransition:
    """Plan2State-neutral core-facing evaluation boundary."""

    before: Plan2State
    after: Plan2State
    standalone: Plan2EndTurnRemainingThreeEvaluation
    event_trace: tuple[str, ...]


def evaluate_plan2_end_turn_remaining_three_before_decrement(
    state: Plan2State,
    remaining_turn: int,
    trigger: object = EXACT_REMAINING_THREE_TRIGGER,
) -> Plan2EndTurnRemainingThreeEvaluationTransition:
    """Evaluate only the exact threshold gate at the native read point."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if (
        not isinstance(trigger, Plan2EndTurnRemainingThreeTrigger)
        or trigger != EXACT_REMAINING_THREE_TRIGGER
    ):
        raise ValueError(
            "unintegrated-end-turn-remaining-three-trigger:"
            f"{getattr(trigger, 'trigger_id', trigger)}"
        )
    standalone = evaluate_plan2_end_turn_remaining_three(
        remaining_turn,
        trigger,
    )
    return Plan2EndTurnRemainingThreeEvaluationTransition(
        state,
        state,
        standalone,
        (
            "ExamEndTurn:before-decrement:RemainingTurn:read",
            f"ExamEndTurn:RemainingTurn<=3:fires={standalone.triggered}",
        ),
    )


def _validate_direct_child(
    child: Plan2EndTurnEffect,
) -> None:
    if not isinstance(child, Plan2EndTurnEffect):
        raise Plan2EndTurnRemainingThreeContractError(
            "child is not a Plan2EndTurnEffect"
        )
    expected = next(
        (
            (child_id, value)
            for child_id, value in zip(DIRECT_CHILD_EFFECT_IDS, DIRECT_CHILD_VALUES)
            if child.effect_id == child_id
        ),
        None,
    )
    if expected is None:
        raise Plan2EndTurnRemainingThreeContractError(
            f"unknown direct child effect {child.effect_id}"
        )
    if (
        child.effect_type != LESSON_DEPEND_EXAM_REVIEW_EFFECT_TYPE
        or child.value1 != expected[1]
        or child.value2 != 0
        or child.count != 1
        or child.turn != 0
        or child.effect_group_ids != REMAINING_THREE_CHILD_EFFECT_GROUP_IDS
    ):
        raise Plan2EndTurnRemainingThreeContractError(
            "child effect is outside exact remaining-three shape"
        )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingThreeChildEvaluation:
    """Pure score projection for one exact target child."""

    effect: Plan2EndTurnEffect
    review_before: int
    raw_score: int
    score_occurrences: tuple[int, ...]
    evaluation_point: Literal["before_turn_check_review"] = (
        "before_turn_check_review"
    )

    def __post_init__(self) -> None:
        _validate_direct_child(self.effect)
        _strict_int(self.review_before, "review_before")
        if self.review_before < 0:
            raise Plan2EndTurnRemainingThreeContractError(
                "review_before must be non-negative"
            )
        _strict_int(self.raw_score, "raw_score")
        if self.raw_score < 0:
            raise Plan2EndTurnRemainingThreeContractError(
                "raw_score must be non-negative"
            )
        if self.score_occurrences != (self.raw_score,):
            raise Plan2EndTurnRemainingThreeContractError(
                "exact child requires one score occurrence"
            )
        if self.evaluation_point != "before_turn_check_review":
            raise Plan2EndTurnRemainingThreeContractError(
                "child must execute before TurnCheck.Review"
            )


def evaluate_plan2_end_turn_remaining_three_child(
    review: int,
    child: Plan2EndTurnEffect,
) -> Plan2EndTurnRemainingThreeChildEvaluation:
    """Evaluate the exact Review-dependent child without mutating state."""

    _validate_direct_child(child)
    current_review = _strict_int(review, "review")
    if current_review < 0:
        raise Plan2EndTurnRemainingThreeContractError(
            "review must be non-negative"
        )
    try:
        raw_score = get_ratio_effect_int_value(
            current_review,
            child.value1,
            is_ceil=True,
        )
    except (NativeFormulaDomainError, OverflowError, ValueError) as error:
        raise Plan2EndTurnRemainingThreeContractError(
            "child ratio is outside the native Int32 domain"
        ) from error
    return Plan2EndTurnRemainingThreeChildEvaluation(
        effect=child,
        review_before=current_review,
        raw_score=raw_score,
        score_occurrences=(raw_score,),
    )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingThreeProgram:
    """Exact target trigger plus the reused native EndTurn listener program."""

    trigger: Plan2EndTurnRemainingThreeTrigger
    listener_program: Plan2EndTurnProgram

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, Plan2EndTurnRemainingThreeTrigger):
            raise TypeError(
                "trigger must be Plan2EndTurnRemainingThreeTrigger"
            )
        if not isinstance(self.listener_program, Plan2EndTurnProgram):
            raise TypeError("listener_program must be Plan2EndTurnProgram")
        program = self.listener_program
        if program.wrapper_effect_id not in DIRECT_TARGETS:
            raise Plan2EndTurnRemainingThreeContractError(
                "wrapper is outside direct p_card-02-ido-3_100 target"
            )
        _, status_id, child_id, child_value = DIRECT_TARGETS[
            program.wrapper_effect_id
        ]
        if (
            program.status_enchant_id != status_id
            or program.trigger_id != REMAINING_THREE_TRIGGER_ID
            or program.phase_type != END_TURN_PHASE_TYPE
            or program.phase_values
            or program.turn != -1
            or program.effect_count != 0
            or program.effect_value1 != 0
            or len(program.effects) != 1
        ):
            raise Plan2EndTurnRemainingThreeContractError(
                "listener program is outside exact remaining-three shape"
            )
        if (
            program.wrapper_effect_group_ids
            != REMAINING_THREE_WRAPPER_EFFECT_GROUP_IDS
        ):
            raise Plan2EndTurnRemainingThreeContractError(
                "wrapper effectGroupIds are outside exact remaining-three shape"
            )
        child = program.effects[0]
        _validate_direct_child(child)
        if child.effect_id != child_id or child.value1 != child_value:
            raise Plan2EndTurnRemainingThreeContractError(
                "child does not match the exact wrapper target"
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


def _validate_remaining_three_trigger(
    row: sqlite3.Row,
) -> Plan2EndTurnRemainingThreeTrigger:
    trigger_id = str(row["id"])
    if trigger_id != REMAINING_THREE_TRIGGER_ID:
        raise Plan2EndTurnRemainingThreeContractError(
            f"{trigger_id}: expected exact remaining-three trigger"
        )
    array_expectations = (
        ("phase_types_json", [END_TURN_PHASE_TYPE]),
        ("phase_values_json", []),
        ("field_status_check_types_json", []),
        ("field_status_types_json", [REMAINING_TURN_FIELD_TYPE]),
        ("field_status_values_json", [REMAINING_THREE_THRESHOLD]),
        ("field_status_produce_card_search_ids_json", []),
        ("effect_types_json", []),
    )
    arrays: dict[str, tuple[object, ...]] = {}
    for column, expected in array_expectations:
        arrays[column] = _strict_array(row[column], expected, trigger_id)
    if (
        str(row["produce_card_search_id"]) != ""
        or _strict_int(row["upper_search_count"], f"{trigger_id}.upperSearchCount")
        != 0
        or _strict_int(row["lower_search_count"], f"{trigger_id}.lowerSearchCount")
        != 0
        or str(row["card_move_position_type"])
        != "ProduceCardMovePositionType_Unknown"
        or str(row["lesson_type"]) != "ProduceStepLessonType_Unknown"
    ):
        raise Plan2EndTurnRemainingThreeContractError(
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
            "fieldStatusValues": [REMAINING_THREE_THRESHOLD],
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
    return Plan2EndTurnRemainingThreeTrigger(
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
            row["upper_search_count"], f"{trigger_id}.upperSearchCount"
        ),
        lower_search_count=_strict_int(
            row["lower_search_count"], f"{trigger_id}.lowerSearchCount"
        ),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=tuple(str(value) for value in arrays["effect_types_json"]),
        lesson_type=str(row["lesson_type"]),
    )


def _validate_target_child_row(row: sqlite3.Row, expected_child_id: str) -> None:
    effect_id = str(row["id"])
    raw = _json_object(row["raw_json"], effect_id)
    _validate_neutral_effect_fields(row, raw)
    if str(row["status_enchant_id"]) or str(row["chain_effect_id"]):
        raise Plan2EndTurnRemainingThreeContractError(
            f"{effect_id}: child status/chain fields must be empty"
        )
    expected_value = next(
        value
        for child_id, value in zip(DIRECT_CHILD_EFFECT_IDS, DIRECT_CHILD_VALUES)
        if child_id == expected_child_id
    )
    _strict_raw_fields(
        raw,
        {
            "id": effect_id,
            "effectType": LESSON_DEPEND_EXAM_REVIEW_EFFECT_TYPE,
            "effectValue1": expected_value,
            "effectValue2": 0,
            "effectCount": 1,
            "effectTurn": 0,
            "produceExamStatusEnchantId": "",
            "effectGroupIds": list(REMAINING_THREE_CHILD_EFFECT_GROUP_IDS),
        },
        effect_id,
    )
    if (
        str(row["effect_type"]) != LESSON_DEPEND_EXAM_REVIEW_EFFECT_TYPE
        or _strict_int(row["value1"], f"{effect_id}.value1") != expected_value
        or _strict_int(row["value2"], f"{effect_id}.value2") != 0
        or _strict_int(row["effect_count"], f"{effect_id}.effectCount") != 1
        or _strict_int(row["effect_turn"], f"{effect_id}.effectTurn") != 0
    ):
        raise Plan2EndTurnRemainingThreeContractError(
            f"{effect_id}: child fields are outside exact shape"
        )
    groups = raw.get("effectGroupIds")
    _strict_equal(
        groups,
        list(REMAINING_THREE_CHILD_EFFECT_GROUP_IDS),
        f"{effect_id}.effectGroupIds",
    )


def _validate_target_wrapper_row(
    row: sqlite3.Row,
    *,
    expected_status_id: str,
) -> None:
    effect_id = str(row["id"])
    raw = _json_object(row["raw_json"], effect_id)
    _validate_neutral_effect_fields(row, raw)
    if str(row["status_enchant_id"]) != expected_status_id:
        raise Plan2EndTurnRemainingThreeContractError(
            f"{effect_id}: status_enchant_id does not match status row"
        )
    if str(row["chain_effect_id"]):
        raise Plan2EndTurnRemainingThreeContractError(
            f"{effect_id}: chain_effect_id must be empty"
        )
    _strict_raw_fields(
        raw,
        {
            "id": effect_id,
            "effectType": STATUS_ENCHANT_EFFECT_TYPE,
            "effectValue1": 0,
            "effectValue2": 0,
            "effectCount": 0,
            "effectTurn": -1,
            "produceExamStatusEnchantId": expected_status_id,
            "chainProduceExamEffectId": "",
            "chainProduceExamEffectIds": [],
            "effectGroupIds": list(REMAINING_THREE_WRAPPER_EFFECT_GROUP_IDS),
        },
        effect_id,
    )
    if (
        str(row["effect_type"]) != STATUS_ENCHANT_EFFECT_TYPE
        or _strict_int(row["value1"], f"{effect_id}.value1") != 0
        or _strict_int(row["value2"], f"{effect_id}.value2") != 0
        or _strict_int(row["effect_count"], f"{effect_id}.effectCount") != 0
        or _strict_int(row["effect_turn"], f"{effect_id}.effectTurn") != -1
    ):
        raise Plan2EndTurnRemainingThreeContractError(
            f"{effect_id}: wrapper fields are outside exact shape"
        )


def load_plan2_end_turn_remaining_three_program(
    wrapper_effect_id: str,
    database: Path = DEFAULT_DATABASE,
) -> Plan2EndTurnRemainingThreeProgram:
    """Load one of the four exact direct target StatusEnchant chains."""

    if type(wrapper_effect_id) is not str:
        raise TypeError("wrapper_effect_id must be a string")
    target = DIRECT_TARGETS.get(wrapper_effect_id)
    if target is None:
        raise Plan2EndTurnRemainingThreeContractError(
            f"wrapper is outside direct target: {wrapper_effect_id}"
        )
    _, expected_status_id, expected_child_id, expected_child_value = target
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
        _validate_target_wrapper_row(
            wrapper,
            expected_status_id=expected_status_id,
        )
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (expected_status_id,),
        ).fetchone()
        if status is None:
            raise Plan2EndTurnRemainingThreeContractError(
                f"{wrapper_effect_id}: missing status {expected_status_id}"
            )
        trigger_id = str(status["produce_exam_trigger_id"])
        if trigger_id != REMAINING_THREE_TRIGGER_ID:
            raise Plan2EndTurnRemainingThreeContractError(
                f"{expected_status_id}: expected {REMAINING_THREE_TRIGGER_ID}, "
                f"got {trigger_id}"
            )
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if trigger is None:
            raise Plan2EndTurnRemainingThreeContractError(
                f"{expected_status_id}: missing trigger {trigger_id}"
            )
        typed_trigger = _validate_remaining_three_trigger(trigger)
        child_ids = tuple(
            str(value)
            for value in _json_array(
                status["produce_exam_effect_ids_json"], expected_status_id
            )
        )
        if child_ids != (expected_child_id,):
            raise Plan2EndTurnRemainingThreeContractError(
                f"{expected_status_id}: child list is outside exact target"
            )
        child_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (expected_child_id,)
        ).fetchone()
        if child_row is None:
            raise Plan2EndTurnRemainingThreeContractError(
                f"{expected_status_id}: missing child effect {expected_child_id}"
            )
        _validate_target_child_row(child_row, expected_child_id)
        child = Plan2EndTurnEffect(
            effect_id=expected_child_id,
            effect_type=str(child_row["effect_type"]),
            value1=_strict_int(child_row["value1"], f"{expected_child_id}.value1"),
            value2=_strict_int(child_row["value2"], f"{expected_child_id}.value2"),
            count=_strict_int(
                child_row["effect_count"], f"{expected_child_id}.effectCount"
            ),
            turn=_strict_int(
                child_row["effect_turn"], f"{expected_child_id}.effectTurn"
            ),
            effect_group_ids=REMAINING_THREE_CHILD_EFFECT_GROUP_IDS,
        )
        status_raw = _json_object(status["raw_json"], expected_status_id)
        _strict_raw_fields(
            status_raw,
            {
                "id": expected_status_id,
                "assetId": str(status["asset_id"]),
                "produceExamTriggerId": trigger_id,
                "produceExamEffectIds": [expected_child_id],
            },
            expected_status_id,
        )
    listener_program = Plan2EndTurnProgram(
        wrapper_effect_id=wrapper_effect_id,
        status_enchant_id=expected_status_id,
        trigger_id=trigger_id,
        phase_type=END_TURN_PHASE_TYPE,
        phase_values=typed_trigger.phase_values,
        effects=(child,),
        wrapper_effect_group_ids=REMAINING_THREE_WRAPPER_EFFECT_GROUP_IDS,
        turn=-1,
        effect_count=0,
        effect_value1=0,
    )
    return Plan2EndTurnRemainingThreeProgram(typed_trigger, listener_program)


def resolve_plan2_end_turn_remaining_three_program(
    wrapper_effect_id: str,
    database: Path = DEFAULT_DATABASE,
) -> Plan2EndTurnRemainingThreeProgram:
    """Resolver alias for callers that distinguish loading from resolution."""

    return load_plan2_end_turn_remaining_three_program(wrapper_effect_id, database)


def install_plan2_end_turn_remaining_three_listener(
    state: Plan2State,
    program: Plan2EndTurnRemainingThreeProgram,
) -> Plan2EndTurnInstallTransition:
    """Delegate normal-card listener installation to the existing runtime."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnRemainingThreeProgram):
        raise TypeError("program must be Plan2EndTurnRemainingThreeProgram")
    return simulate_install_plan2_end_turn_listener(
        state,
        program.listener_program,
    )


__all__ = [
    "DIRECT_CHILD_EFFECT_IDS",
    "DIRECT_CHILD_VALUES",
    "DIRECT_TARGETS",
    "DIRECT_UPGRADES",
    "DIRECT_WRAPPER_EFFECT_IDS",
    "END_TURN_REMAINING_THREE_ORDER",
    "EXACT_REMAINING_THREE_TRIGGER",
    "LESSON_DEPEND_EXAM_REVIEW_EFFECT_TYPE",
    "NATIVE_REMAINING_THREE_ORDER",
    "Plan2EndTurnRemainingThreeChildEvaluation",
    "Plan2EndTurnRemainingThreeContractError",
    "Plan2EndTurnRemainingThreeEvaluation",
    "Plan2EndTurnRemainingThreeEvaluationTransition",
    "Plan2EndTurnRemainingThreeProgram",
    "Plan2EndTurnRemainingThreeTrigger",
    "REMAINING_THREE_CHILD_EFFECT_GROUP_IDS",
    "REMAINING_THREE_THRESHOLD",
    "REMAINING_THREE_TRIGGER_ID",
    "REMAINING_THREE_WRAPPER_EFFECT_GROUP_IDS",
    "REMAINING_TURN_FIELD_TYPE",
    "TARGET_CARD_ID",
    "TARGET_UPGRADES",
    "evaluate_plan2_end_turn_remaining_three",
    "evaluate_plan2_end_turn_remaining_three_before_decrement",
    "evaluate_plan2_end_turn_remaining_three_child",
    "install_plan2_end_turn_remaining_three_listener",
    "load_plan2_end_turn_remaining_three_program",
    "resolve_plan2_end_turn_remaining_three_program",
]
