"""Exact Plan2 ``ExamEndTurn`` / ``ReviewUp >= 3`` support.

This is the deliberately narrow adapter for
``e_trigger-exam_end_turn-review_up-3`` on
``p_card-02-men-3_042``.  The field predicate reads the signed *current*
Review status at the ExamEndTurn field-gate; it does not use a cumulative
counter, a Review delta, or the later TurnCheck score.

Master installation and child execution are delegated to the existing
``plan2_end_turn_trigger`` listener runtime.  That keeps the native active
list, child order, SpendCount, removal, interval/timer, TurnCheck, and
TurnStart lifetime boundaries in one proven implementation.  Unknown
trigger, wrapper, status, child, scalar, chain, or group shapes fail closed
at the loader/evaluator boundary.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_end_turn_card_play_aggressive_trigger import EndTurnEvaluationStage
from .plan2_end_turn_trigger import (
    END_TURN_PHASE_TYPE,
    NATIVE_END_TURN_PHASE_ORDER,
    REVIEW_EFFECT_TYPE,
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


TARGET_CARD_ID = "p_card-02-men-3_042"
TARGET_CARD_UPGRADES = (0, 1, 2, 3)
TARGET_TRIGGER_ID = "e_trigger-exam_end_turn-review_up-3"
TARGET_THRESHOLD = 3
TARGET_FIELD_STATUS_TYPE = "ProduceExamFieldStatusType_ReviewUp"
TARGET_STATUS_ENCHANT_ID = "enchant-p_card-02-men-3_042-enc01"
TARGET_WRAPPER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-enchant-"
    "p_card-02-men-3_042-enc01"
)
TARGET_CHILD_EFFECT_ID = "e_effect-exam_review-0003"
TARGET_CHILD_EFFECT_TYPE = REVIEW_EFFECT_TYPE

TARGET_CHILD_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_review-000",
)
TARGET_WRAPPER_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_review-000",
)

# Useful stable aliases for callers that name the slice rather than the
# generic target constants used by the adjacent aggressive adapter.
REVIEW_UP3_TRIGGER_ID = TARGET_TRIGGER_ID
REVIEW_UP3_THRESHOLD = TARGET_THRESHOLD
REVIEW_UP3_FIELD_STATUS_TYPE = TARGET_FIELD_STATUS_TYPE
REVIEW_UP3_STATUS_ENCHANT_ID = TARGET_STATUS_ENCHANT_ID
REVIEW_UP3_WRAPPER_EFFECT_ID = TARGET_WRAPPER_EFFECT_ID
REVIEW_UP3_CHILD_EFFECT_ID = TARGET_CHILD_EFFECT_ID

ANDROID_V323_NATIVE_SOURCE = (
    "_research/android/game-v3.2.3/native-analysis/lib/arm64-v8a/"
    "libil2cpp.so"
)
ANDROID_V323_METADATA_SOURCE = (
    "_research/android/game-v3.2.3/native-analysis/"
    "global-metadata.decrypted.dat"
)
ANDROID_V323_DUMP_SOURCE = "_research/android/game-v3.2.3/il2cppdumper/dump.cs"
ANDROID_V323_ISIL_SOURCE = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/ExamExtensions.txt"
)


ANDROID_END_TURN_REVIEW_UP3_EVIDENCE = {
    "version": "Android v3.2.3 arm64-v8a",
    "native_source": ANDROID_V323_NATIVE_SOURCE,
    "metadata_source": ANDROID_V323_METADATA_SOURCE,
    "field": {
        "enum_symbol": TARGET_FIELD_STATUS_TYPE,
        "enum_value": 35,
        "enum_source": f"{ANDROID_V323_DUMP_SOURCE}:696971-697012",
        "predicate_symbol": "ExamExtensions.IsFieldStatusTriggerStatusEffect",
        "predicate_rva": "0x68082D4",
        "predicate_source": f"{ANDROID_V323_ISIL_SOURCE}:14735-15476",
        "getter_symbol": "ExamStatusEffectCollection.GetReview",
        "getter_source": f"{ANDROID_V323_ISIL_SOURCE}:15917",
        "fact": "the field path reads the current signed Review status",
        "comparison": "signed current_review >= 3; inclusive equality",
        "not_shape": "fieldStatusCheckTypes=[]; no Not inversion",
        "not_cumulative": (
            "Review deltas, ReviewCountAdd, and TurnCheck score occurrences "
            "are not predicate inputs"
        ),
    },
    "install": {
        "symbol": "StatusEnchantEffectExecutor.ExecuteEffect",
        "va": "0x7E8FA08",
        "fact": "one fresh TriggerEffectStatusEffect is appended per wrapper execution",
    },
    "listener": {
        "symbol": "ExamStatusEffectCollection.TryAddTriggerEffectStatus",
        "va": "0x7E8FEE0",
        "constructor_va": "0x7EADF04",
        "filter_va": "0x7EA1D74",
        "spend_count_symbol": "TriggerEffectStatusEffect.SpendCount",
        "spend_count_va": "0x7EAE70C",
        "remove_symbol": "ExamStatusEffectCollection.RemoveCountLimitTriggeredStatus",
        "remove_va": "0x7EA52B0",
        "fact": "children execute in stable order, then one SpendCount occurs",
    },
    "child": {
        "effect_type": TARGET_CHILD_EFFECT_TYPE,
        "executor_symbol": "ReviewEffectExecutor.ExecuteEffect",
        "executor_va": "0x7E8A61C",
        "fact": "ExamReview adds value1 to the current Review status",
    },
    "phase": {
        "symbol": "ExamSequence.<ExamLoopTaskAsync>d__94.MoveNext",
        "va": "0x7EDCE78",
        "fact": (
            "ExamEndTurn field evaluation and child execution precede "
            "EndTurnInterval, EndTurnTimer, TurnCheck.Review, and later "
            "TurnStart lifetime spending"
        ),
    },
}


class Plan2EndTurnReviewUp3ContractError(Plan2EndTurnContractError):
    """A Master/native row is outside the exact ReviewUp-3 contract."""


def _strict_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise Plan2EndTurnReviewUp3ContractError(
            f"{label} must be a plain integer"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2EndTurnReviewUp3ContractError(
            f"{label} is outside Int32"
        )
    return value


def _strict_equal(actual: object, expected: object, label: str) -> None:
    """Compare JSON values without Python bool/int coercion."""

    if type(actual) is not type(expected):
        raise Plan2EndTurnReviewUp3ContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )
    if isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2EndTurnReviewUp3ContractError(
                f"{label} must be {expected!r}, got {actual!r}"
            )
        for index, (item, wanted) in enumerate(zip(actual, expected)):
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if actual != expected:
        raise Plan2EndTurnReviewUp3ContractError(
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
            raise Plan2EndTurnReviewUp3ContractError(
                f"{row_id}: missing raw field {key}"
            )
        _strict_equal(raw[key], value, f"{row_id}.{key}")


def _validate_neutral_raw(
    row: sqlite3.Row, raw: dict[str, object], row_id: str
) -> None:
    """Pin all executable neutral controls while allowing display metadata."""

    _validate_neutral_effect_fields(row, raw)
    _strict_raw_fields(
        raw,
        {
            "targetProduceCardId": "",
            "targetUpgradeCount": 0,
            "targetExamEffectType": "ProduceExamEffectType_Unknown",
            "produceCardSearchId": "",
            "movePositionType": "ProduceCardMovePositionType_Unknown",
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
            "produceCardStatusEnchantId": "",
            "produceCardGrowEffectIds": [],
        },
        row_id,
    )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnReviewUp3Trigger:
    """The fully typed, exact target trigger row."""

    id: str = TARGET_TRIGGER_ID
    phase_types: tuple[str, ...] = (END_TURN_PHASE_TYPE,)
    phase_values: tuple[int, ...] = ()
    field_status_check_types: tuple[str, ...] = ()
    field_status_types: tuple[str, ...] = (TARGET_FIELD_STATUS_TYPE,)
    field_status_values: tuple[int, ...] = (TARGET_THRESHOLD,)
    field_status_produce_card_search_ids: tuple[str, ...] = ()
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = "ProduceCardMovePositionType_Unknown"
    effect_types: tuple[str, ...] = ()
    lesson_type: str = "ProduceStepLessonType_Unknown"

    def __post_init__(self) -> None:
        exact = (
            ("id", TARGET_TRIGGER_ID),
            ("phase_types", (END_TURN_PHASE_TYPE,)),
            ("phase_values", ()),
            ("field_status_check_types", ()),
            ("field_status_types", (TARGET_FIELD_STATUS_TYPE,)),
            ("field_status_values", (TARGET_THRESHOLD,)),
            ("field_status_produce_card_search_ids", ()),
            ("effect_types", ()),
            ("produce_card_search_id", ""),
            ("card_move_position_type", "ProduceCardMovePositionType_Unknown"),
            ("lesson_type", "ProduceStepLessonType_Unknown"),
        )
        for label, expected in exact:
            if getattr(self, label) != expected:
                raise Plan2EndTurnReviewUp3ContractError(
                    f"{label} has unknown shape: {getattr(self, label)!r}"
                )
        _strict_int(self.upper_search_count, "upper_search_count")
        _strict_int(self.lower_search_count, "lower_search_count")
        if self.upper_search_count != 0 or self.lower_search_count != 0:
            raise Plan2EndTurnReviewUp3ContractError(
                "search counts must be zero"
            )

    @property
    def trigger_id(self) -> str:
        return self.id

    @property
    def field_status_card_search_ids(self) -> tuple[str, ...]:
        return self.field_status_produce_card_search_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
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


EXACT_TARGET_TRIGGER = Plan2EndTurnReviewUp3Trigger()
EXACT_REVIEW_UP3_TRIGGER = EXACT_TARGET_TRIGGER


@dataclass(frozen=True, slots=True)
class Plan2EndTurnReviewUp3EvaluationInput:
    """Native inputs for the field gate.

    ``review_status_value`` is the only value used by the target predicate.
    The optional observed delta and later ReviewCountAdd-like value are kept
    solely to make accidental counter/delta implementations testable.
    """

    review_status_value: int
    phase: str = END_TURN_PHASE_TYPE
    evaluation_stage: EndTurnEvaluationStage = EndTurnEvaluationStage.FIELD_GATE
    observed_review_delta: int | None = None
    review_count_add: int = 0

    def __post_init__(self) -> None:
        _strict_int(self.review_status_value, "review_status_value")
        if not isinstance(self.phase, str) or not self.phase:
            raise TypeError("phase must be a non-empty string")
        if not isinstance(self.evaluation_stage, EndTurnEvaluationStage):
            object.__setattr__(
                self,
                "evaluation_stage",
                EndTurnEvaluationStage(self.evaluation_stage),
            )
        if self.observed_review_delta is not None:
            _strict_int(self.observed_review_delta, "observed_review_delta")
        count_add = _strict_int(self.review_count_add, "review_count_add")
        if count_add < 0:
            raise Plan2EndTurnReviewUp3ContractError(
                "review_count_add must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class Plan2EndTurnReviewUp3Evaluation:
    """Result of the exact signed current-value predicate."""

    trigger_id: str
    supported: bool
    fires: bool | None
    review_status_value: int | None
    threshold: int | None
    comparison: Literal["signed_greater_equal"] | None
    phase: str
    evaluation_stage: EndTurnEvaluationStage
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.review_status_value is not None:
            _strict_int(self.review_status_value, "review_status_value")
        if self.threshold is not None:
            _strict_int(self.threshold, "threshold")
        if type(self.fires) not in (bool, type(None)):
            raise TypeError("fires must be a boolean or None")

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def current_review(self) -> int | None:
        return self.review_status_value

    @property
    def review_value(self) -> int | None:
        return self.review_status_value

    @property
    def result_expression(self) -> str | None:
        if self.threshold is None:
            return None
        return f"signed_current_review >= {self.threshold}"

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "reviewStatusValue": self.review_status_value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "phase": self.phase,
            "evaluationStage": self.evaluation_stage.value,
            "reasons": list(self.reasons),
        }


def _unsupported_evaluation(
    trigger_id: str,
    inputs: Plan2EndTurnReviewUp3EvaluationInput,
    reasons: tuple[str, ...],
) -> Plan2EndTurnReviewUp3Evaluation:
    return Plan2EndTurnReviewUp3Evaluation(
        trigger_id=trigger_id,
        supported=False,
        fires=None,
        review_status_value=None,
        threshold=None,
        comparison=None,
        phase=inputs.phase,
        evaluation_stage=inputs.evaluation_stage,
        reasons=reasons,
    )


def evaluate_plan2_end_turn_review_up3_trigger(
    inputs: Plan2EndTurnReviewUp3EvaluationInput,
    trigger: Plan2EndTurnReviewUp3Trigger = EXACT_TARGET_TRIGGER,
) -> Plan2EndTurnReviewUp3Evaluation:
    """Evaluate signed current Review ``>= 3`` before EndTurn follow-ups."""

    if not isinstance(inputs, Plan2EndTurnReviewUp3EvaluationInput):
        raise TypeError(
            "inputs must be Plan2EndTurnReviewUp3EvaluationInput"
        )
    if not isinstance(trigger, Plan2EndTurnReviewUp3Trigger):
        return _unsupported_evaluation(
            TARGET_TRIGGER_ID,
            inputs,
            ("unknown-review-up3-trigger-shape",),
        )
    if trigger != EXACT_TARGET_TRIGGER:
        return _unsupported_evaluation(
            trigger.id,
            inputs,
            ("only-exact-end-turn-review-up-3-is-supported",),
        )
    reasons: list[str] = []
    if inputs.phase != END_TURN_PHASE_TYPE:
        reasons.append("runtime-phase-is-not-exam-end-turn")
    if inputs.evaluation_stage is not EndTurnEvaluationStage.FIELD_GATE:
        reasons.append("field-gate-is-only-evaluated-at-exam-end-turn")
    if reasons:
        return Plan2EndTurnReviewUp3Evaluation(
            trigger_id=TARGET_TRIGGER_ID,
            supported=False,
            fires=None,
            review_status_value=None,
            threshold=TARGET_THRESHOLD,
            comparison="signed_greater_equal",
            phase=inputs.phase,
            evaluation_stage=inputs.evaluation_stage,
            reasons=tuple(reasons),
        )
    return Plan2EndTurnReviewUp3Evaluation(
        trigger_id=TARGET_TRIGGER_ID,
        supported=True,
        fires=inputs.review_status_value >= TARGET_THRESHOLD,
        review_status_value=inputs.review_status_value,
        threshold=TARGET_THRESHOLD,
        comparison="signed_greater_equal",
        phase=inputs.phase,
        evaluation_stage=inputs.evaluation_stage,
    )


# Short alias matching the slice name used by some callers.
evaluate_plan2_end_turn_review_up3 = evaluate_plan2_end_turn_review_up3_trigger


@dataclass(frozen=True, slots=True)
class Plan2EndTurnReviewUp3Program:
    """Exact trigger plus the existing native EndTurn listener program."""

    trigger: Plan2EndTurnReviewUp3Trigger
    listener_program: Plan2EndTurnProgram

    def __post_init__(self) -> None:
        if self.trigger != EXACT_TARGET_TRIGGER:
            raise Plan2EndTurnReviewUp3ContractError(
                "only the exact ReviewUp-3 trigger is supported"
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
            or program.effect_count != 0
            or program.effect_value1 != 0
            or len(program.effects) != 1
        ):
            raise Plan2EndTurnReviewUp3ContractError(
                "listener program is outside the exact ReviewUp-3 shape"
            )
        if program.wrapper_effect_group_ids != TARGET_WRAPPER_EFFECT_GROUP_IDS:
            raise Plan2EndTurnReviewUp3ContractError(
                "wrapper effectGroupIds are outside the exact target shape"
            )
        child = program.effects[0]
        if (
            child.effect_id != TARGET_CHILD_EFFECT_ID
            or child.effect_type != TARGET_CHILD_EFFECT_TYPE
            or child.value1 != TARGET_THRESHOLD
            or child.value2 != 0
            or child.count != 0
            or child.turn != 0
            or child.effect_group_ids != TARGET_CHILD_EFFECT_GROUP_IDS
        ):
            raise Plan2EndTurnReviewUp3ContractError(
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


def _validate_target_trigger(row: sqlite3.Row) -> Plan2EndTurnReviewUp3Trigger:
    trigger_id = str(row["id"])
    if trigger_id != TARGET_TRIGGER_ID:
        raise Plan2EndTurnReviewUp3ContractError(
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
            row["field_status_types_json"], [TARGET_FIELD_STATUS_TYPE], trigger_id
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
        or card_move_position_type != "ProduceCardMovePositionType_Unknown"
        or lesson_type != "ProduceStepLessonType_Unknown"
    ):
        raise Plan2EndTurnReviewUp3ContractError(
            f"{trigger_id}: unsupported search/filter shape"
        )
    raw = _json_object(row["raw_json"], trigger_id)
    _strict_raw_fields(raw, EXACT_TARGET_TRIGGER.to_dict(), trigger_id)
    candidate = Plan2EndTurnReviewUp3Trigger(
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
        field_status_produce_card_search_ids=tuple(
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
        raise Plan2EndTurnReviewUp3ContractError(
            f"{trigger_id}: target trigger does not match exact typed row"
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
        raise Plan2EndTurnReviewUp3ContractError(
            f"{effect_id}: expected exact target effect {expected_effect_id}"
        )
    raw = _json_object(row["raw_json"], effect_id)
    _validate_neutral_raw(row, raw, effect_id)
    _strict_raw_fields(
        raw,
        {
            "id": expected_effect_id,
            "effectType": expected_effect_type,
            "effectValue1": expected_value1,
            "effectValue2": expected_value2,
            "effectCount": expected_count,
            "effectTurn": expected_turn,
            "produceExamStatusEnchantId": expected_status_id,
            "chainProduceExamEffectId": "",
            "chainProduceExamEffectIds": [],
            "effectGroupIds": list(expected_groups),
        },
        effect_id,
    )
    groups = _ordered_string_array(raw, "effectGroupIds", effect_id)
    if groups != expected_groups:
        raise Plan2EndTurnReviewUp3ContractError(
            f"{effect_id}: effectGroupIds order is not exact"
        )
    values = {
        "value1": _strict_int(row["value1"], f"{effect_id}.value1"),
        "value2": _strict_int(row["value2"], f"{effect_id}.value2"),
        "effect_count": _strict_int(
            row["effect_count"], f"{effect_id}.effect_count"
        ),
        "effect_turn": _strict_int(
            row["effect_turn"], f"{effect_id}.effect_turn"
        ),
    }
    if (
        str(row["effect_type"]) != expected_effect_type
        or values["value1"] != expected_value1
        or values["value2"] != expected_value2
        or values["effect_count"] != expected_count
        or values["effect_turn"] != expected_turn
        or str(row["status_enchant_id"]) != expected_status_id
        or str(row["chain_effect_id"]) != ""
    ):
        raise Plan2EndTurnReviewUp3ContractError(
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


def load_plan2_end_turn_review_up3_program(
    wrapper_effect_id: str = TARGET_WRAPPER_EFFECT_ID,
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2EndTurnReviewUp3Program:
    """Load only the exact target wrapper/status/trigger/child chain."""

    if wrapper_effect_id != TARGET_WRAPPER_EFFECT_ID:
        raise Plan2EndTurnReviewUp3ContractError(
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
            raise Plan2EndTurnReviewUp3ContractError(
                f"{wrapper_effect_id}: expected {STATUS_ENCHANT_EFFECT_TYPE}"
            )
        status_id = str(wrapper["status_enchant_id"])
        if status_id != TARGET_STATUS_ENCHANT_ID:
            raise Plan2EndTurnReviewUp3ContractError(
                f"{wrapper_effect_id}: unexpected status {status_id}"
            )
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (status_id,),
        ).fetchone()
        if status is None:
            raise Plan2EndTurnReviewUp3ContractError(
                f"{wrapper_effect_id}: missing status {status_id}"
            )
        trigger_id = str(status["produce_exam_trigger_id"])
        if trigger_id != TARGET_TRIGGER_ID:
            raise Plan2EndTurnReviewUp3ContractError(
                f"{status_id}: unexpected trigger {trigger_id}"
            )
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if trigger_row is None:
            raise Plan2EndTurnReviewUp3ContractError(
                f"{status_id}: missing trigger {trigger_id}"
            )
        trigger = _validate_target_trigger(trigger_row)
        child_ids = tuple(
            str(value)
            for value in _json_array(status["produce_exam_effect_ids_json"], status_id)
        )
        if child_ids != (TARGET_CHILD_EFFECT_ID,):
            raise Plan2EndTurnReviewUp3ContractError(
                f"{status_id}: child order/identity is not exact"
            )
        child_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (TARGET_CHILD_EFFECT_ID,)
        ).fetchone()
        if child_row is None:
            raise Plan2EndTurnReviewUp3ContractError(
                f"{status_id}: missing child effect {TARGET_CHILD_EFFECT_ID}"
            )
        child = _validate_target_effect_row(
            child_row,
            expected_effect_id=TARGET_CHILD_EFFECT_ID,
            expected_effect_type=TARGET_CHILD_EFFECT_TYPE,
            expected_value1=TARGET_THRESHOLD,
            expected_value2=0,
            expected_count=0,
            expected_turn=0,
            expected_status_id="",
            expected_groups=TARGET_CHILD_EFFECT_GROUP_IDS,
        )

        wrapper_raw = _json_object(wrapper["raw_json"], wrapper_effect_id)
        _validate_target_effect_row(
            wrapper,
            expected_effect_id=TARGET_WRAPPER_EFFECT_ID,
            expected_effect_type=STATUS_ENCHANT_EFFECT_TYPE,
            expected_value1=0,
            expected_value2=0,
            expected_count=0,
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

        listener_program = Plan2EndTurnProgram(
            wrapper_effect_id=TARGET_WRAPPER_EFFECT_ID,
            status_enchant_id=TARGET_STATUS_ENCHANT_ID,
            trigger_id=TARGET_TRIGGER_ID,
            phase_type=END_TURN_PHASE_TYPE,
            phase_values=(),
            effects=(child,),
            wrapper_effect_group_ids=TARGET_WRAPPER_EFFECT_GROUP_IDS,
            turn=-1,
            effect_count=0,
            effect_value1=0,
        )
    return Plan2EndTurnReviewUp3Program(trigger, listener_program)


def install_plan2_end_turn_review_up3_listener(
    state: Plan2State, program: Plan2EndTurnReviewUp3Program
) -> Plan2EndTurnInstallTransition:
    """Delegate installation to the existing native EndTurn primitive."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnReviewUp3Program):
        raise TypeError("program must be Plan2EndTurnReviewUp3Program")
    return simulate_install_plan2_end_turn_listener(
        state, program.listener_program
    )


__all__ = [
    "ANDROID_END_TURN_REVIEW_UP3_EVIDENCE",
    "ANDROID_V323_DUMP_SOURCE",
    "ANDROID_V323_ISIL_SOURCE",
    "ANDROID_V323_METADATA_SOURCE",
    "ANDROID_V323_NATIVE_SOURCE",
    "END_TURN_PHASE_TYPE",
    "EXACT_REVIEW_UP3_TRIGGER",
    "EXACT_TARGET_TRIGGER",
    "NATIVE_END_TURN_PHASE_ORDER",
    "REVIEW_EFFECT_TYPE",
    "REVIEW_UP3_CHILD_EFFECT_ID",
    "REVIEW_UP3_FIELD_STATUS_TYPE",
    "REVIEW_UP3_STATUS_ENCHANT_ID",
    "REVIEW_UP3_THRESHOLD",
    "REVIEW_UP3_TRIGGER_ID",
    "REVIEW_UP3_WRAPPER_EFFECT_ID",
    "TARGET_CARD_ID",
    "TARGET_CARD_UPGRADES",
    "TARGET_CHILD_EFFECT_GROUP_IDS",
    "TARGET_CHILD_EFFECT_ID",
    "TARGET_CHILD_EFFECT_TYPE",
    "TARGET_FIELD_STATUS_TYPE",
    "TARGET_STATUS_ENCHANT_ID",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "TARGET_WRAPPER_EFFECT_GROUP_IDS",
    "TARGET_WRAPPER_EFFECT_ID",
    "EndTurnEvaluationStage",
    "Plan2EndTurnReviewUp3ContractError",
    "Plan2EndTurnReviewUp3Evaluation",
    "Plan2EndTurnReviewUp3EvaluationInput",
    "Plan2EndTurnReviewUp3Program",
    "Plan2EndTurnReviewUp3Trigger",
    "evaluate_plan2_end_turn_review_up3",
    "evaluate_plan2_end_turn_review_up3_trigger",
    "install_plan2_end_turn_review_up3_listener",
    "load_plan2_end_turn_review_up3_program",
]
