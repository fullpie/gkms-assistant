"""Bounded standalone proof for ``p_card-02-ido-3_081``.

The card has one common StartTurn blocker and one common effect blocker:

* ``e_trigger-exam_start_turn-review_up-1`` reads the signed current Review
  status at the settled StartTurn card gate; and
* ``ExamStaminaRecoverMultiple`` computes a fixed recovery from MaxStamina,
  then delegates to the already proven Plan2 Fix recovery primitive.

This module is intentionally a card-local adapter.  It reads the local Master
database read-only, validates the four exact card rows and the two blocker
shapes, and returns pure evaluations.  It does not import or mutate the
central Plan2/Plan3 engine, formal coverage, GUI, or card-move runtime.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from .master_db import DEFAULT_DATABASE
from .plan2_aggressive_card_trigger import EvaluationBoundary
from .plan2_stamina_recover_fix import (
    StaminaDifference,
    StaminaRecoverFixContract,
    StaminaRecoverFixEvaluation,
    StaminaRecoverFixRuntime,
    evaluate_stamina_recover_fix,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_start_turn_review_stamina_recover_native_audit.json"
)

PHASE_EXAM_START_TURN: Final = "ProduceExamPhaseType_ExamStartTurn"
DIRECT_DISPATCH_PHASE: Final = "PlayEffect"
FIELD_REVIEW_UP: Final = "ProduceExamFieldStatusType_ReviewUp"
CHECK_NOT: Final = "ProduceExamTriggerCheckType_Not"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"
PLAN2: Final = "ProducePlanType_Plan2"
MENTAL_SKILL: Final = "ProduceCardCategory_MentalSkill"
REVIEW_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamReview"
BLOCK_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlock"
PLAYABLE_VALUE_ADD_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamPlayableValueAdd"
)
MULTIPLE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamStaminaRecoverMultiple"
)

TARGET_CARD_ID: Final = "p_card-02-ido-3_081"
TARGET_CARD_NAME: Final = "また、明日"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_TRIGGER_ID: Final = "e_trigger-exam_start_turn-review_up-1"
TARGET_THRESHOLD: Final = 1

TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE: Final = {
    0: "e_effect-exam_stamina_recover_multiple-0100",
    1: "e_effect-exam_stamina_recover_multiple-0100",
    2: "e_effect-exam_stamina_recover_multiple-0100",
    3: "e_effect-exam_stamina_recover_multiple-0150",
}
TARGET_EFFECT_IDS_BY_UPGRADE: Final = {
    0: (
        "e_effect-exam_stamina_recover_multiple-0100",
        "e_effect-exam_review-0003",
        "e_effect-exam_block-0005",
        "e_effect-exam_playable_value_add-01",
    ),
    1: (
        "e_effect-exam_stamina_recover_multiple-0100",
        "e_effect-exam_review-0005",
        "e_effect-exam_block-0010",
        "e_effect-exam_playable_value_add-01",
    ),
    2: (
        "e_effect-exam_stamina_recover_multiple-0100",
        "e_effect-exam_review-0005",
        "e_effect-exam_block-0010",
        "e_effect-exam_playable_value_add-01",
    ),
    3: (
        "e_effect-exam_stamina_recover_multiple-0150",
        "e_effect-exam_review-0005",
        "e_effect-exam_block-0014",
        "e_effect-exam_playable_value_add-01",
    ),
}
TARGET_CARD_STAMINA_BY_UPGRADE: Final = {0: 6, 1: 6, 2: 5, 3: 5}
TARGET_CARD_NAMES_BY_UPGRADE: Final = {
    0: TARGET_CARD_NAME,
    1: f"{TARGET_CARD_NAME}+",
    2: f"{TARGET_CARD_NAME}++",
    3: f"{TARGET_CARD_NAME}+++",
}

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1
_F32_THOUSAND: Final = 1000.0

ANDROID_V323_NATIVE_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/lib/arm64-v8a/"
    "libil2cpp.so"
)
ANDROID_V323_METADATA_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/"
    "global-metadata.decrypted.dat"
)
ANDROID_V323_TARGET_METADATA_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/target-metadata.json"
)
ANDROID_V323_EXECUTOR_MAPPING_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json"
)
ANDROID_V323_ISIL_EXTENSIONS_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/ExamExtensions.txt"
)
ANDROID_V323_ISIL_SEQUENCE_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/Exam/ExamSequence.txt"
)
ANDROID_V323_ISIL_LOOP_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/Exam/"
    "ExamSequence_NestedType__ExamLoopTaskAsync_d__94.txt"
)
PC_METADATA_SOURCE: Final = (
    "_research/il2cpp/"
    "B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
    "targeted-metadata-index.json"
)
PC_NATIVE_SCAN_SOURCE: Final = (
    "_research/il2cpp/"
    "B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
    "native-method-table-scan.json"
)


class ReviewStaminaContractError(ValueError):
    """A Master/native row is outside this exact standalone contract."""


class StartTurnSnapshotBoundary(str, Enum):
    """The transaction boundary visible to a direct StartTurn card gate."""

    SETTLED_PRE_CARD_GATE = "settled-start-turn-pre-card-gate"
    POST_PAYMENT = "post-payment"
    POST_DIRECT_EFFECTS = "post-direct-effects"
    POST_CARD_MOVE = "post-card-move"


class EvaluationStage(str, Enum):
    """The card transaction stage at which the trigger is evaluated."""

    PRE_CARD_COST = "pre-card-cost"
    POST_CARD_COST = "post-card-cost"
    POST_DIRECT_EFFECTS = "post-direct-effects"
    POST_CARD_MOVE = "post-card-move"


class CardExecutionKind(str, Enum):
    """Only ordinary direct card play is proven for this target."""

    ORDINARY = "ordinary-card-play"
    FORCED = "forced-card-play"
    EXTRA = "extra-card-play"


ExecutionMode = CardExecutionKind


def _i32(value: object, label: str) -> int:
    if type(value) is not int:
        raise ReviewStaminaContractError(f"{label} must be a signed Int32")
    if not INT32_MIN <= value <= INT32_MAX:
        raise ReviewStaminaContractError(f"{label} is outside signed Int32")
    return value


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        adjective = "text" if empty else "non-empty text"
        raise ReviewStaminaContractError(f"{label} must be {adjective}")
    return value


def _json_value(value: object, label: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReviewStaminaContractError(f"{label} is invalid JSON") from exc
    return value


def _json_array(value: object, label: str) -> tuple[object, ...]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise ReviewStaminaContractError(f"{label} must be a JSON array")
    return tuple(parsed)


def _json_object(value: object, label: str) -> dict[str, object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, dict):
        raise ReviewStaminaContractError(f"{label} must be a JSON object")
    return parsed


def _strict_equal(actual: object, expected: object, label: str) -> None:
    """Compare JSON shape without bool/int coercion or unordered arrays."""

    if type(actual) is not type(expected):
        raise ReviewStaminaContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )
    if isinstance(expected, dict):
        if set(actual) != set(expected):  # type: ignore[arg-type]
            raise ReviewStaminaContractError(
                f"{label} keys are not exact: {sorted(actual)!r}"  # type: ignore[arg-type]
            )
        for key, wanted in expected.items():
            _strict_equal(actual[key], wanted, f"{label}.{key}")  # type: ignore[index]
        return
    if isinstance(expected, (list, tuple)):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise ReviewStaminaContractError(
                f"{label} length is not exact: {actual!r}"
            )
        for index, (item, wanted) in enumerate(zip(actual, expected, strict=True)):  # type: ignore[arg-type]
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if actual != expected:
        raise ReviewStaminaContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )


def _strict_subset(
    actual: object,
    expected: Mapping[str, object],
    label: str,
    *,
    allowed_extra_keys: Sequence[str] = (),
) -> None:
    """Validate executable keys while allowing only named metadata keys."""

    if type(actual) is not dict:
        raise ReviewStaminaContractError(f"{label} must be a JSON object")
    actual_dict = actual  # type: ignore[assignment]
    missing = set(expected) - set(actual_dict)
    if missing:
        raise ReviewStaminaContractError(
            f"{label} is missing keys: {sorted(missing)!r}"
        )
    extra = set(actual_dict) - set(expected)
    allowed = set(allowed_extra_keys)
    if not extra <= allowed:
        raise ReviewStaminaContractError(
            f"{label} has unknown keys: {sorted(extra - allowed)!r}"
        )
    for key, wanted in expected.items():
        _strict_equal(actual_dict[key], wanted, f"{label}.{key}")


@dataclass(frozen=True, slots=True)
class ReviewUp1Trigger:
    """The complete normalized target ``produce_exam_trigger`` row."""

    id: str = TARGET_TRIGGER_ID
    phase_types: tuple[str, ...] = (PHASE_EXAM_START_TURN,)
    phase_values: tuple[int, ...] = ()
    field_status_check_types: tuple[str, ...] = ()
    field_status_types: tuple[str, ...] = (FIELD_REVIEW_UP,)
    field_status_values: tuple[int, ...] = (TARGET_THRESHOLD,)
    field_status_card_search_ids: tuple[str, ...] = ()
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = MOVE_UNKNOWN
    effect_types: tuple[str, ...] = ()
    lesson_type: str = LESSON_UNKNOWN

    def __post_init__(self) -> None:
        expected = (
            ("id", TARGET_TRIGGER_ID),
            ("phase_types", (PHASE_EXAM_START_TURN,)),
            ("phase_values", ()),
            ("field_status_check_types", ()),
            ("field_status_types", (FIELD_REVIEW_UP,)),
            ("field_status_values", (TARGET_THRESHOLD,)),
            ("field_status_card_search_ids", ()),
            ("produce_card_search_id", ""),
            ("upper_search_count", 0),
            ("lower_search_count", 0),
            ("card_move_position_type", MOVE_UNKNOWN),
            ("effect_types", ()),
            ("lesson_type", LESSON_UNKNOWN),
        )
        for label, wanted in expected:
            if getattr(self, label) != wanted:
                raise ReviewStaminaContractError(
                    f"{label} has unknown target shape: {getattr(self, label)!r}"
                )
        for name in ("upper_search_count", "lower_search_count"):
            _i32(getattr(self, name), name)

    @property
    def trigger_id(self) -> str:
        return self.id

    def master_contract(self) -> dict[str, object]:
        return {
            "id": self.id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_check_types": list(self.field_status_check_types),
            "field_types": list(self.field_status_types),
            "field_values": list(self.field_status_values),
            "field_card_search_ids": list(self.field_status_card_search_ids),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "effect_types": list(self.effect_types),
            "lesson_type": self.lesson_type,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "phaseTypes": list(self.phase_types),
            "phaseValues": list(self.phase_values),
            "fieldStatusCheckTypes": list(self.field_status_check_types),
            "fieldStatusTypes": list(self.field_status_types),
            "fieldStatusValues": list(self.field_status_values),
            "fieldStatusProduceCardSearchIds": list(
                self.field_status_card_search_ids
            ),
            "produceCardSearchId": self.produce_card_search_id,
            "upperSearchCount": self.upper_search_count,
            "lowerSearchCount": self.lower_search_count,
            "cardMovePositionType": self.card_move_position_type,
            "effectTypes": list(self.effect_types),
            "lessonType": self.lesson_type,
        }


EXACT_TARGET_TRIGGER: Final = ReviewUp1Trigger()
REVIEW_UP1_TRIGGER: Final = EXACT_TARGET_TRIGGER


def _trigger_raw_shape(trigger: ReviewUp1Trigger) -> dict[str, object]:
    return {
        "id": trigger.id,
        "phaseTypes": list(trigger.phase_types),
        "phaseValues": list(trigger.phase_values),
        "fieldStatusCheckTypes": list(trigger.field_status_check_types),
        "fieldStatusTypes": list(trigger.field_status_types),
        "fieldStatusValues": list(trigger.field_status_values),
        "fieldStatusProduceCardSearchIds": list(
            trigger.field_status_card_search_ids
        ),
        "produceCardSearchId": trigger.produce_card_search_id,
        "upperSearchCount": trigger.upper_search_count,
        "lowerSearchCount": trigger.lower_search_count,
        "cardMovePositionType": trigger.card_move_position_type,
        "effectTypes": list(trigger.effect_types),
        "lessonType": trigger.lesson_type,
    }


def _trigger_from_row(row: sqlite3.Row) -> ReviewUp1Trigger:
    trigger_id = _text(row["id"], "trigger.id")
    if trigger_id != TARGET_TRIGGER_ID:
        raise ReviewStaminaContractError("unexpected target trigger id")
    arrays = {
        "phase_types": tuple(
            _text(item, "trigger.phaseTypes")
            for item in _json_array(row["phase_types_json"], "trigger.phaseTypes")
        ),
        "phase_values": tuple(
            _i32(item, "trigger.phaseValues")
            for item in _json_array(row["phase_values_json"], "trigger.phaseValues")
        ),
        "field_status_check_types": tuple(
            _text(item, "trigger.fieldStatusCheckTypes")
            for item in _json_array(
                row["field_status_check_types_json"],
                "trigger.fieldStatusCheckTypes",
            )
        ),
        "field_status_types": tuple(
            _text(item, "trigger.fieldStatusTypes")
            for item in _json_array(row["field_status_types_json"], "trigger.fieldStatusTypes")
        ),
        "field_status_values": tuple(
            _i32(item, "trigger.fieldStatusValues")
            for item in _json_array(
                row["field_status_values_json"], "trigger.fieldStatusValues"
            )
        ),
        "field_status_card_search_ids": tuple(
            _text(item, "trigger.fieldStatusProduceCardSearchIds", empty=True)
            for item in _json_array(
                row["field_status_produce_card_search_ids_json"],
                "trigger.fieldStatusProduceCardSearchIds",
            )
        ),
        "effect_types": tuple(
            _text(item, "trigger.effectTypes")
            for item in _json_array(row["effect_types_json"], "trigger.effectTypes")
        ),
    }
    candidate = ReviewUp1Trigger(
        id=trigger_id,
        **arrays,
        produce_card_search_id=_text(
            row["produce_card_search_id"], "trigger.produceCardSearchId", empty=True
        ),
        upper_search_count=_i32(row["upper_search_count"], "trigger.upperSearchCount"),
        lower_search_count=_i32(row["lower_search_count"], "trigger.lowerSearchCount"),
        card_move_position_type=_text(
            row["card_move_position_type"], "trigger.cardMovePositionType", empty=True
        ),
        lesson_type=_text(row["lesson_type"], "trigger.lessonType", empty=True),
    )
    raw = _json_object(row["raw_json"], TARGET_TRIGGER_ID)
    _strict_subset(
        raw,
        _trigger_raw_shape(EXACT_TARGET_TRIGGER),
        "trigger.raw",
        allowed_extra_keys=(
            "playEffectProduceDescriptions",
            "playProduceDescriptions",
            "produceDescriptions",
        ),
    )
    return candidate


@dataclass(frozen=True, slots=True)
class ContractResolution:
    supported: bool
    contract: ReviewUp1Trigger | None
    reasons: tuple[str, ...] = ()


def resolve_review_up1_trigger(source: object) -> ContractResolution:
    """Resolve only the exact typed trigger; unknown input stays unresolved."""

    if isinstance(source, str):
        if source == TARGET_TRIGGER_ID:
            return ContractResolution(True, EXACT_TARGET_TRIGGER)
        return ContractResolution(False, None, ("unknown-trigger-id",))
    if not isinstance(source, ReviewUp1Trigger):
        return ContractResolution(False, None, ("trigger-record-type-is-unknown",))
    if source != EXACT_TARGET_TRIGGER:
        return ContractResolution(False, None, ("trigger-shape-is-not-exact",))
    return ContractResolution(True, source)


@dataclass(frozen=True, slots=True)
class ReviewUp1EvaluationInput:
    """Values visible at the settled direct card gate.

    ``current_review`` is the only predicate input.  Delta/count fields are
    provenance probes and deliberately inert.  The settlement and transaction
    flags make the gate/payment/effect order explicit and fail closed if a
    caller supplies a snapshot from another point in the native queue.
    """

    current_review: int
    phase: str = PHASE_EXAM_START_TURN
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: EvaluationBoundary = EvaluationBoundary.PRE_PAYMENT_BUILD
    stage: EvaluationStage = EvaluationStage.PRE_CARD_COST
    snapshot: StartTurnSnapshotBoundary = (
        StartTurnSnapshotBoundary.SETTLED_PRE_CARD_GATE
    )
    execution_kind: CardExecutionKind = CardExecutionKind.ORDINARY
    observed_review_delta: int | None = None
    review_count_add: int = 0
    repeat_index: int = 0
    start_turn_settled: bool = True
    full_power_settled: bool = True
    gimmick_settled: bool = True
    draw_settled: bool = True
    recovery_settled: bool = True
    consumption_settled: bool = True
    status_turn_spend_settled: bool = True
    ordered_start_turn_effects_settled: bool = True
    card_cost_paid: bool = False
    direct_effects_applied: bool = False
    timer_installed: bool = False
    card_move_applied: bool = False
    play_count_incremented: bool = False

    def __post_init__(self) -> None:
        _i32(self.current_review, "current_review")
        if self.observed_review_delta is not None:
            _i32(self.observed_review_delta, "observed_review_delta")
        count_add = _i32(self.review_count_add, "review_count_add")
        if count_add < 0:
            raise ReviewStaminaContractError("review_count_add must be non-negative")
        if type(self.repeat_index) is not int or self.repeat_index < 0:
            raise ReviewStaminaContractError("repeat_index must be non-negative")
        if type(self.phase) is not str or not self.phase:
            raise ReviewStaminaContractError("phase must be non-empty text")
        if type(self.dispatch_phase) is not str or not self.dispatch_phase:
            raise ReviewStaminaContractError("dispatch_phase must be non-empty text")
        for name, enum_type in (
            ("boundary", EvaluationBoundary),
            ("stage", EvaluationStage),
            ("snapshot", StartTurnSnapshotBoundary),
            ("execution_kind", CardExecutionKind),
        ):
            value = getattr(self, name)
            if not isinstance(value, enum_type):
                object.__setattr__(self, name, enum_type(value))
        for name in (
            "start_turn_settled",
            "full_power_settled",
            "gimmick_settled",
            "draw_settled",
            "recovery_settled",
            "consumption_settled",
            "status_turn_spend_settled",
            "ordered_start_turn_effects_settled",
            "card_cost_paid",
            "direct_effects_applied",
            "timer_installed",
            "card_move_applied",
            "play_count_incremented",
        ):
            if type(getattr(self, name)) is not bool:
                raise ReviewStaminaContractError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class ReviewUp1Evaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    current_review: int | None
    threshold: int | None
    comparison: str | None
    phase: str
    dispatch_phase: str
    boundary: EvaluationBoundary
    stage: EvaluationStage
    snapshot: StartTurnSnapshotBoundary
    execution_kind: CardExecutionKind
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def allows_card_invocation(self) -> bool:
        return self.supported and self.fires is True

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
            "allowsCardInvocation": self.allows_card_invocation,
            "currentReview": self.current_review,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "stage": self.stage.value,
            "snapshot": self.snapshot.value,
            "executionKind": self.execution_kind.value,
            "reasons": list(self.reasons),
        }


def _unsupported_review(
    trigger_id: str,
    inputs: ReviewUp1EvaluationInput,
    reasons: Sequence[str],
) -> ReviewUp1Evaluation:
    return ReviewUp1Evaluation(
        trigger_id=trigger_id,
        supported=False,
        fires=None,
        current_review=None,
        threshold=TARGET_THRESHOLD,
        comparison="signed_greater_equal",
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_kind=inputs.execution_kind,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_review_up1_trigger(
    inputs: ReviewUp1EvaluationInput,
    trigger: object = EXACT_TARGET_TRIGGER,
) -> ReviewUp1Evaluation:
    """Evaluate signed current Review ``>= 1`` at the pre-payment gate."""

    if not isinstance(inputs, ReviewUp1EvaluationInput):
        raise TypeError("inputs must be ReviewUp1EvaluationInput")
    resolution = resolve_review_up1_trigger(trigger)
    if not resolution.supported:
        return _unsupported_review(
            str(getattr(trigger, "id", trigger)), inputs, resolution.reasons
        )
    reasons: list[str] = []
    if inputs.phase != PHASE_EXAM_START_TURN:
        reasons.append("runtime-phase-is-not-exam-start-turn")
    if inputs.dispatch_phase != DIRECT_DISPATCH_PHASE:
        reasons.append("dispatch-phase-is-not-direct-play-effect")
    if inputs.boundary is not EvaluationBoundary.PRE_PAYMENT_BUILD:
        reasons.append("card-gate-is-not-pre-payment-build")
    if inputs.stage is not EvaluationStage.PRE_CARD_COST:
        reasons.append("card-gate-is-not-pre-card-cost")
    if inputs.snapshot is not StartTurnSnapshotBoundary.SETTLED_PRE_CARD_GATE:
        reasons.append("start-turn-snapshot-is-not-settled-pre-card-gate")
    for name, reason in (
        ("start_turn_settled", "start-turn-settlement-not-proven"),
        ("full_power_settled", "full-power-settlement-not-proven"),
        ("gimmick_settled", "gimmick-settlement-not-proven"),
        ("draw_settled", "draw-settlement-not-proven"),
        ("recovery_settled", "start-turn-recovery-not-settled"),
        ("consumption_settled", "stamina-consumption-not-settled"),
        ("status_turn_spend_settled", "status-turn-spend-not-settled"),
        (
            "ordered_start_turn_effects_settled",
            "ordered-start-turn-effects-not-settled",
        ),
    ):
        if not getattr(inputs, name):
            reasons.append(reason)
    if inputs.execution_kind is not CardExecutionKind.ORDINARY:
        reasons.append("forced-or-extra-execution-unproven")
    if any(
        (
            inputs.card_cost_paid,
            inputs.direct_effects_applied,
            inputs.timer_installed,
            inputs.card_move_applied,
            inputs.play_count_incremented,
        )
    ):
        reasons.append("snapshot-is-after-payment-effect-move-or-count-step")
    if reasons:
        return _unsupported_review(TARGET_TRIGGER_ID, inputs, reasons)
    return ReviewUp1Evaluation(
        trigger_id=TARGET_TRIGGER_ID,
        supported=True,
        fires=inputs.current_review >= TARGET_THRESHOLD,
        current_review=inputs.current_review,
        threshold=TARGET_THRESHOLD,
        comparison="signed_greater_equal",
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_kind=inputs.execution_kind,
    )


evaluate_review_up1 = evaluate_review_up1_trigger
evaluate_plan2_start_turn_review_up1 = evaluate_review_up1_trigger


_EFFECT_GROUPS_BY_TYPE: Final = {
    MULTIPLE_EFFECT_TYPE: ("effect_group-visible-stamina_recover_fix-000",),
    REVIEW_EFFECT_TYPE: ("effect_group-visible-exam_review-000",),
    BLOCK_EFFECT_TYPE: ("effect_group-visible-exam_block-000",),
    PLAYABLE_VALUE_ADD_EFFECT_TYPE: (
        "effect_group-visible-exam_playable_value_add-000",
    ),
}
_EXPECTED_EFFECT_SCALARS: Final = {
    "e_effect-exam_stamina_recover_multiple-0100": (
        MULTIPLE_EFFECT_TYPE,
        100,
        0,
        0,
        0,
    ),
    "e_effect-exam_stamina_recover_multiple-0150": (
        MULTIPLE_EFFECT_TYPE,
        150,
        0,
        0,
        0,
    ),
    "e_effect-exam_review-0003": (REVIEW_EFFECT_TYPE, 3, 0, 0, 0),
    "e_effect-exam_review-0005": (REVIEW_EFFECT_TYPE, 5, 0, 0, 0),
    "e_effect-exam_block-0005": (BLOCK_EFFECT_TYPE, 5, 0, 0, 0),
    "e_effect-exam_block-0010": (BLOCK_EFFECT_TYPE, 10, 0, 0, 0),
    "e_effect-exam_block-0014": (BLOCK_EFFECT_TYPE, 14, 0, 0, 0),
    "e_effect-exam_playable_value_add-01": (
        PLAYABLE_VALUE_ADD_EFFECT_TYPE,
        0,
        0,
        1,
        0,
    ),
}


def _neutral_effect_raw(effect_id: str, effect_type: str, value1: int, value2: int, count: int, turn: int) -> dict[str, object]:
    return {
        "id": effect_id,
        "effectType": effect_type,
        "effectValue1": value1,
        "effectValue2": value2,
        "effectCount": count,
        "effectTurn": turn,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": "",
        "movePositionType": MOVE_UNKNOWN,
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
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": list(_EFFECT_GROUPS_BY_TYPE[effect_type]),
    }


@dataclass(frozen=True, slots=True)
class MultipleEffectContract:
    """One exact ``ExamStaminaRecoverMultiple`` effect row."""

    effect_id: str
    effect_value1: int
    effect_value2: int = 0
    effect_count: int = 0
    effect_turn: int = 0
    effect_type: str = MULTIPLE_EFFECT_TYPE
    status_enchant_id: str = ""
    chain_effect_id: str = ""
    effect_group_ids: tuple[str, ...] = (
        "effect_group-visible-stamina_recover_fix-000",
    )

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _i32(self.effect_value1, "effect_value1")
        _i32(self.effect_value2, "effect_value2")
        _i32(self.effect_count, "effect_count")
        _i32(self.effect_turn, "effect_turn")
        if self.effect_type != MULTIPLE_EFFECT_TYPE:
            raise ReviewStaminaContractError("effect type is not Multiple")
        if self.effect_value2 != 0 or self.effect_count != 0 or self.effect_turn != 0:
            raise ReviewStaminaContractError("Multiple scalar shape is not exact")
        if self.status_enchant_id or self.chain_effect_id:
            raise ReviewStaminaContractError("nested Multiple shape is not exact")
        if self.effect_group_ids != _EFFECT_GROUPS_BY_TYPE[MULTIPLE_EFFECT_TYPE]:
            raise ReviewStaminaContractError("Multiple effectGroupIds are not exact")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> "MultipleEffectContract":
        if not isinstance(raw, Mapping):
            raise ReviewStaminaContractError("effect payload must be a mapping")

        allowed_keys = {
            "id",
            "effect_id",
            "effectType",
            "effect_type",
            "effectValue1",
            "value1",
            "effect_value1",
            "effectValue2",
            "value2",
            "effect_value2",
            "effectCount",
            "effect_count",
            "effectTurn",
            "effect_turn",
            "effectGroupIds",
            "effect_group_ids",
            "produceExamStatusEnchantId",
            "status_enchant_id",
            "chainProduceExamEffectId",
            "chain_effect_id",
            "produceCardStatusEnchantId",
            "produceExamTriggerId",
            "produceCardSearchId",
            "targetProduceCardId",
            "targetUpgradeCount",
            "targetExamEffectType",
            "movePositionType",
            "pickRangeType",
            "pickCountReferenceProduceCardSearchId",
            "pickCountType",
            "pickCountMin",
            "pickCountMax",
            "produceCardSearchId2",
            "pickRangeType2",
            "pickCountReferenceProduceCardSearchId2",
            "pickCountType2",
            "pickCountMin2",
            "pickCountMax2",
            "chainProduceExamEffectIds",
            "chain_effect_ids",
            "produceCardGrowEffectIds",
            "produceDescriptions",
            "customizeProduceDescriptions",
        }
        unknown_keys = set(raw) - allowed_keys
        if unknown_keys:
            raise ReviewStaminaContractError(
                f"unknown Multiple effect keys: {sorted(unknown_keys)!r}"
            )

        def pick(*names: str, required: bool = True) -> object:
            for name in names:
                if name in raw:
                    return raw[name]
            if required:
                raise ReviewStaminaContractError(f"missing effect field: {names[0]}")
            return ""

        for name in (
            "produceExamStatusEnchantId",
            "status_enchant_id",
            "chainProduceExamEffectId",
            "chain_effect_id",
            "produceCardStatusEnchantId",
            "produceExamTriggerId",
            "produceCardSearchId",
            "targetProduceCardId",
        ):
            if name in raw and raw[name] not in ("", None):
                raise ReviewStaminaContractError(f"unsupported nested effect field: {name}")
        for name in ("chainProduceExamEffectIds", "chain_effect_ids"):
            if name in raw and raw[name] not in (None, "", [], ()):
                raise ReviewStaminaContractError("unsupported nested chain effect list")
        groups = pick("effectGroupIds", "effect_group_ids")
        if type(groups) is tuple:
            groups = list(groups)
        if groups != list(_EFFECT_GROUPS_BY_TYPE[MULTIPLE_EFFECT_TYPE]):
            raise ReviewStaminaContractError("effectGroupIds are not the proven shape")
        neutral_fields = {
            "targetUpgradeCount": 0,
            "targetExamEffectType": "ProduceExamEffectType_Unknown",
            "movePositionType": MOVE_UNKNOWN,
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
            "produceCardGrowEffectIds": [],
            "produceExamTriggerId": "",
        }
        for key, wanted in neutral_fields.items():
            if key in raw:
                _strict_equal(raw[key], wanted, f"effect.{key}")
        return cls(
            effect_id=_text(pick("id", "effect_id"), "effect_id"),
            effect_type=_text(pick("effectType", "effect_type"), "effect_type"),
            effect_value1=_i32(
                pick("effectValue1", "value1", "effect_value1"), "effect_value1"
            ),
            effect_value2=_i32(
                pick("effectValue2", "value2", "effect_value2"), "effect_value2"
            ),
            effect_count=_i32(
                pick("effectCount", "effect_count"), "effect_count"
            ),
            effect_turn=_i32(pick("effectTurn", "effect_turn"), "effect_turn"),
            status_enchant_id=_text(
                pick("produceExamStatusEnchantId", "status_enchant_id", required=False),
                "status_enchant_id",
                empty=True,
            ),
            chain_effect_id=_text(
                pick("chainProduceExamEffectId", "chain_effect_id", required=False),
                "chain_effect_id",
                empty=True,
            ),
            effect_group_ids=tuple(str(item) for item in groups),
        )


def try_parse_stamina_recover_multiple(
    raw: Mapping[str, object],
) -> MultipleEffectContract | None:
    try:
        return MultipleEffectContract.from_mapping(raw)
    except (KeyError, TypeError, ValueError, ReviewStaminaContractError):
        return None


@dataclass(frozen=True, slots=True)
class CardEffectShape:
    """Exact scalar/raw-neutral shape for one target card effect row."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.effect_id,
            "effect_type": self.effect_type,
            "effect_value1": self.value1,
            "effect_value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
        }


def _card_effect_shape_from_row(
    row: sqlite3.Row,
    expected_id: str,
    issues: list[str],
) -> CardEffectShape | None:
    label = f"effect:{expected_id}"
    try:
        effect_id = _text(row["id"], f"{label}.id")
        expected = _EXPECTED_EFFECT_SCALARS[expected_id]
        effect_type, value1, value2, count, turn = expected
        if effect_id != expected_id:
            raise ReviewStaminaContractError("effect id mismatch")
        normalized = (
            effect_type,
            _i32(row["value1"], f"{label}.value1"),
            _i32(row["value2"], f"{label}.value2"),
            _i32(row["effect_count"], f"{label}.effect_count"),
            _i32(row["effect_turn"], f"{label}.effect_turn"),
        )
        if normalized != expected:
            raise ReviewStaminaContractError(
                f"scalar mismatch: expected {expected!r}, got {normalized!r}"
            )
        status_id = _text(row["status_enchant_id"], f"{label}.status_enchant_id", empty=True)
        chain_id = _text(row["chain_effect_id"], f"{label}.chain_effect_id", empty=True)
        if status_id or chain_id:
            raise ReviewStaminaContractError("nested status/chain is not empty")
        raw_expected = _neutral_effect_raw(
            expected_id, effect_type, value1, value2, count, turn
        )
        raw = _json_object(row["raw_json"], label)
        _strict_subset(
            raw,
            raw_expected,
            f"{label}.raw",
            allowed_extra_keys=(
                "customizeProduceDescriptions",
                "produceDescriptions",
            ),
        )
        return CardEffectShape(
            effect_id=effect_id,
            effect_type=effect_type,
            value1=value1,
            value2=value2,
            effect_count=count,
            effect_turn=turn,
            status_enchant_id=status_id,
            chain_effect_id=chain_id,
            effect_group_ids=tuple(_EFFECT_GROUPS_BY_TYPE[effect_type]),
        )
    except (KeyError, TypeError, ValueError, ReviewStaminaContractError) as exc:
        issues.append(f"{label}:{exc}")
        return None


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class TargetCardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    effect_slots: tuple[CardEffectSlot, ...]

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def multiple_effect_id(self) -> str:
        return self.ordered_effect_ids[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "name": self.name,
            "plan_type": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "cost_type": self.cost_type,
            "cost_value": self.cost_value,
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "move_effect_trigger_type": self.move_effect_trigger_type,
            "move_effect_ids": list(self.move_effect_ids),
            "effect_slots": [slot.to_dict() for slot in self.effect_slots],
            "ordered_effect_ids": list(self.ordered_effect_ids),
        }


def _card_raw_expected(
    row: TargetCardVersion,
) -> dict[str, object]:
    return {
        "id": row.card_id,
        "upgradeCount": row.upgrade_count,
        "name": row.name,
        "planType": row.plan_type,
        "category": row.category,
        "stamina": row.stamina,
        "forceStamina": 0,
        "costType": row.cost_type,
        "costValue": row.cost_value,
        "playProduceExamTriggerId": TARGET_TRIGGER_ID,
        "playEffects": [
            {
                "produceExamTriggerId": slot.trigger_id,
                "produceExamEffectId": slot.effect_id,
                "hideIcon": slot.hide_icon,
                "isOncePlayEffect": slot.is_once_play_effect,
            }
            for slot in row.effect_slots
        ],
        "moveProduceExamTriggerIds": [],
        "playMovePositionType": MOVE_LOST,
        "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
        "moveProduceExamEffectIds": [],
        "produceCardCustomizeIds": [],
        "produceCardStatusEnchantId": "",
        "effectGroupIds": [
            "effect_group-visible-exam_block-000",
            "effect_group-visible-exam_playable_value_add-000",
            "effect_group-visible-exam_review-000",
            "effect_group-visible-stamina_recover_fix-000",
        ],
    }


def _card_from_row(row: sqlite3.Row, issues: list[str]) -> TargetCardVersion | None:
    label = f"card:{row['id']}#{row['upgrade_count']}"
    try:
        card_id = _text(row["id"], f"{label}.id")
        upgrade = _i32(row["upgrade_count"], f"{label}.upgrade_count")
        raw = _json_object(row["raw_json"], f"{label}.raw")
        raw_slots = _json_array(row["play_effects_json"], f"{label}.play_effects")
        expected_ids = TARGET_EFFECT_IDS_BY_UPGRADE.get(upgrade, ())
        if len(raw_slots) != 4 or tuple(
            item.get("produceExamEffectId")
            for item in raw_slots
            if isinstance(item, Mapping)
        ) != expected_ids:
            raise ReviewStaminaContractError("ordered play effect shape is not exact")
        slots: list[CardEffectSlot] = []
        if len(raw_slots) != 4:
            raise ReviewStaminaContractError("target card must have four effect slots")
        for index, item in enumerate(raw_slots):
            if not isinstance(item, Mapping):
                raise ReviewStaminaContractError(f"effect slot {index} is not an object")
            slot = CardEffectSlot(
                slot_index=index,
                effect_id=_text(item.get("produceExamEffectId"), f"{label}.slot{index}.id"),
                trigger_id=_text(
                    item.get("produceExamTriggerId"),
                    f"{label}.slot{index}.trigger",
                    empty=True,
                ),
                hide_icon=item.get("hideIcon"),
                is_once_play_effect=item.get("isOncePlayEffect"),
            )
            if slot.trigger_id or slot.hide_icon is not False or slot.is_once_play_effect is not False:
                raise ReviewStaminaContractError(f"slot {index} flags are not exact")
            slots.append(slot)
        card = TargetCardVersion(
            card_id=card_id,
            upgrade_count=upgrade,
            name=_text(row["name"], f"{label}.name"),
            plan_type=_text(row["plan_type"], f"{label}.plan_type"),
            category=_text(row["category"], f"{label}.category"),
            stamina=_i32(row["stamina"], f"{label}.stamina"),
            cost_type=_text(row["cost_type"], f"{label}.cost_type", empty=True),
            cost_value=_i32(row["cost_value"], f"{label}.cost_value"),
            play_trigger_id=_text(row["play_trigger_id"], f"{label}.play_trigger_id"),
            move_position_type=_text(
                row["move_position_type"], f"{label}.move_position_type"
            ),
            move_effect_trigger_type=_text(
                raw.get("moveEffectTriggerType"),
                f"{label}.moveEffectTriggerType",
                empty=True,
            ),
            move_effect_ids=tuple(
                _text(item, f"{label}.moveProduceExamEffectIds", empty=True)
                for item in _json_array(
                    raw.get("moveProduceExamEffectIds", []),
                    f"{label}.moveProduceExamEffectIds",
                )
            ),
            effect_slots=tuple(slots),
        )
        if card.card_id != TARGET_CARD_ID:
            raise ReviewStaminaContractError("card id is outside target scope")
        if card.upgrade_count not in TARGET_UPGRADES:
            raise ReviewStaminaContractError("upgrade is outside target scope")
        if card.name != TARGET_CARD_NAMES_BY_UPGRADE[card.upgrade_count]:
            raise ReviewStaminaContractError("card name shape is not exact")
        if card.plan_type != PLAN2 or card.category != MENTAL_SKILL:
            raise ReviewStaminaContractError("card plan/category shape is not exact")
        if card.stamina != TARGET_CARD_STAMINA_BY_UPGRADE[card.upgrade_count]:
            raise ReviewStaminaContractError("card stamina shape is not exact")
        if card.cost_type != "ExamCostType_Unknown" or card.cost_value != 0:
            raise ReviewStaminaContractError("card cost shape is not exact")
        if card.play_trigger_id != TARGET_TRIGGER_ID:
            raise ReviewStaminaContractError("card trigger reference is not exact")
        if card.move_position_type != MOVE_LOST:
            raise ReviewStaminaContractError("card move shape is not exact")
        if card.move_effect_trigger_type != "ProduceCardMoveEffectTriggerType_Unknown" or card.move_effect_ids:
            raise ReviewStaminaContractError("card move effect shape is not exact")
        _strict_subset(
            raw,
            _card_raw_expected(card),
            f"{label}.raw",
            allowed_extra_keys=(
                "assetId",
                "evaluation",
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
                "noDeckDuplication",
                "order",
                "originCharacterId",
                "originIdolCardId",
                "originPrimaStellaIdolCardId",
                "originSupportCardId",
                "produceDescriptions",
                "rarity",
                "rentalUnlockProducerLevel",
                "searchTag",
                "unlockProducerLevel",
                "viewStartTime",
                "voiceAssetId",
            ),
        )
        return card
    except (KeyError, TypeError, ValueError, ReviewStaminaContractError) as exc:
        issues.append(f"{label}:{exc}")
        return None


@dataclass(frozen=True, slots=True)
class ReviewStaminaCatalog:
    database: str
    trigger: ReviewUp1Trigger | None
    card_versions: tuple[TargetCardVersion, ...]
    effect_shapes: tuple[CardEffectShape, ...]
    shape_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_versions", tuple(self.card_versions))
        object.__setattr__(self, "effect_shapes", tuple(self.effect_shapes))
        object.__setattr__(self, "shape_issues", tuple(dict.fromkeys(self.shape_issues)))

    @property
    def affected_card_versions(self) -> tuple[TargetCardVersion, ...]:
        return self.card_versions

    @property
    def exact_shape_supported(self) -> bool:
        return (
            self.trigger == EXACT_TARGET_TRIGGER
            and not self.shape_issues
            and tuple(row.upgrade_count for row in self.card_versions)
            == TARGET_UPGRADES
            and len(self.effect_shapes) == len(_EXPECTED_EFFECT_SCALARS)
        )

    @property
    def direct_card_versions(self) -> tuple[TargetCardVersion, ...]:
        return self.card_versions if self.exact_shape_supported else ()

    @property
    def effect_shapes_by_id(self) -> dict[str, CardEffectShape]:
        return {row.effect_id: row for row in self.effect_shapes}

    def card_version(self, upgrade: int) -> TargetCardVersion:
        for row in self.card_versions:
            if row.upgrade_count == upgrade:
                return row
        raise KeyError(upgrade)

    def multiple_effect(self, upgrade: int) -> MultipleEffectContract:
        card = self.card_version(upgrade)
        shape = self.effect_shapes_by_id[card.multiple_effect_id]
        return MultipleEffectContract(
            effect_id=shape.effect_id,
            effect_type=shape.effect_type,
            effect_value1=shape.value1,
            effect_value2=shape.value2,
            effect_count=shape.effect_count,
            effect_turn=shape.effect_turn,
            status_enchant_id=shape.status_enchant_id,
            chain_effect_id=shape.chain_effect_id,
            effect_group_ids=shape.effect_group_ids,
        )

    def blocker_summary(self) -> dict[str, dict[str, object]]:
        affected = len(self.affected_card_versions)
        exact = self.exact_shape_supported
        return {
            "card_trigger": {
                "id": TARGET_TRIGGER_ID,
                "affected_card_versions": affected,
                "direct_card_versions": 0,
                "co_blocked_card_versions": affected,
                "proof": exact,
            },
            "stamina_recover_multiple": {
                "id": MULTIPLE_EFFECT_TYPE,
                "affected_card_versions": affected,
                "direct_card_versions": 0,
                "co_blocked_card_versions": affected,
                "proof": exact,
            },
            "combined": {
                "affected_card_versions": affected,
                "direct_card_versions": len(self.direct_card_versions),
                "co_blocked_card_versions": (
                    affected - len(self.direct_card_versions)
                ),
                "proof": exact,
            },
        }

    def summary(self) -> dict[str, object]:
        combined = self.blocker_summary()["combined"]
        return {
            "affected_card_versions": len(self.affected_card_versions),
            "direct_card_versions": combined["direct_card_versions"],
            "co_blocked_card_versions": combined["co_blocked_card_versions"],
            "directly_unlocked_if_fixed_alone": 0,
            "directly_unlocked_if_both_blockers_fixed": combined[
                "direct_card_versions"
            ],
            "blockers": self.blocker_summary(),
            "exact_shape_supported": self.exact_shape_supported,
            "shape_issues": list(self.shape_issues),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "trigger": None if self.trigger is None else self.trigger.to_dict(),
            "card_versions": [row.to_dict() for row in self.card_versions],
            "effect_shapes": [row.to_dict() for row in self.effect_shapes],
            "summary": self.summary(),
        }


def load_plan2_start_turn_review_stamina_recover_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> ReviewStaminaCatalog:
    """Read the exact target card/effect slice from SQLite in read-only mode."""

    path = Path(database)
    if not path.is_file():
        raise ReviewStaminaContractError(f"Master database not found: {path}")
    issues: list[str] = []
    trigger: ReviewUp1Trigger | None = None
    cards: list[TargetCardVersion] = []
    shapes: list[CardEffectShape] = []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        if trigger_row is None:
            issues.append("missing-target-trigger")
        else:
            try:
                trigger = _trigger_from_row(trigger_row)
            except (KeyError, TypeError, ValueError, ReviewStaminaContractError) as exc:
                issues.append(f"trigger:{exc}")

        rows = connection.execute(
            "SELECT id, upgrade_count, name, plan_type, category, stamina, "
            "cost_type, cost_value, play_trigger_id, move_position_type, "
            "play_effects_json, raw_json FROM card WHERE id = ? "
            "ORDER BY upgrade_count",
            (TARGET_CARD_ID,),
        ).fetchall()
        for row in rows:
            parsed = _card_from_row(row, issues)
            if parsed is not None:
                cards.append(parsed)
        if tuple(row.upgrade_count for row in cards) != TARGET_UPGRADES:
            issues.append("target-upgrades-are-not-exact-0-1-2-3")
        if len(cards) != 4:
            issues.append(f"target-card-version-count:{len(cards)}")

        expected_effect_ids = tuple(_EXPECTED_EFFECT_SCALARS)
        for effect_id in expected_effect_ids:
            row = connection.execute(
                "SELECT id, effect_type, value1, value2, effect_count, effect_turn, "
                "status_enchant_id, chain_effect_id, raw_json FROM effect WHERE id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                issues.append(f"missing-target-effect:{effect_id}")
                continue
            shape = _card_effect_shape_from_row(row, effect_id, issues)
            if shape is not None:
                shapes.append(shape)

    return ReviewStaminaCatalog(
        database=str(path),
        trigger=trigger,
        card_versions=tuple(cards),
        effect_shapes=tuple(shapes),
        shape_issues=tuple(issues),
    )


load_catalog = load_plan2_start_turn_review_stamina_recover_catalog


def _f32(value: int | float) -> float:
    try:
        result = struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except OverflowError:
        # Native SCVTF/arithmetics produce an IEEE-754 infinity on overflow.
        result = math.copysign(math.inf, float(value))
    except struct.error as exc:
        raise ReviewStaminaContractError(f"value is outside float32: {value!r}") from exc
    if math.isnan(result):
        raise ReviewStaminaContractError(f"value is NaN float32: {value!r}")
    return result


def _native_multiple_request(
    max_stamina: int,
    effect_value1: int,
) -> tuple[float, int]:
    """Reproduce Multiple's MaxStamina * permille binary32/ceil path."""

    maximum = _f32(max_stamina)
    value = _f32(effect_value1)
    product = _f32(maximum * value)
    scaled = _f32(product / _f32(_F32_THOUSAND))
    if math.isinf(scaled):
        # ARM FRINTP/FCVTPS uses the Int32 indefinite sentinel for +Infinity.
        return scaled, INT32_MIN
    rounded = math.ceil(scaled)
    if not INT32_MIN <= rounded <= INT32_MAX:
        # FCVTPS also yields the signed Int32 indefinite result out of range.
        return scaled, INT32_MIN
    return scaled, rounded


StaminaRecoverMultipleRuntime = StaminaRecoverFixRuntime


@dataclass(frozen=True, slots=True)
class StaminaRecoverMultipleEvaluation:
    contract: MultipleEffectContract | None
    runtime_before: StaminaRecoverMultipleRuntime
    runtime_after: StaminaRecoverMultipleRuntime
    executable: bool
    simulated: bool
    reason: str | None
    base_max_stamina: int | None
    scaled_float32: float | None
    requested_value: int | None
    adjusted_recovery_value: int | None
    calculated_recovery: int | None
    predicted_stamina: int | None
    difference: StaminaDifference | None
    trace: tuple[str, ...]
    fix_evaluation: StaminaRecoverFixEvaluation | None = None

    @property
    def actual_stamina(self) -> int:
        return self.runtime_after.stamina

    @property
    def fail_closed(self) -> bool:
        return not self.executable

    def to_dict(self) -> dict[str, object]:
        return {
            "effectId": None if self.contract is None else self.contract.effect_id,
            "effectType": (
                None if self.contract is None else self.contract.effect_type
            ),
            "executable": self.executable,
            "simulated": self.simulated,
            "reason": self.reason,
            "baseMaxStamina": self.base_max_stamina,
            "scaledFloat32": self.scaled_float32,
            "requestedValue": self.requested_value,
            "adjustedRecoveryValue": self.adjusted_recovery_value,
            "calculatedRecovery": self.calculated_recovery,
            "predictedStamina": self.predicted_stamina,
            "actualStamina": self.actual_stamina,
            "difference": None
            if self.difference is None
            else {
                "beforeStamina": self.difference.before_stamina,
                "afterStamina": self.difference.after_stamina,
                "beforeMaxStamina": self.difference.before_max_stamina,
                "afterMaxStamina": self.difference.after_max_stamina,
                "currentBlock": self.difference.current_block,
            },
            "trace": list(self.trace),
        }


def _unresolved_multiple(
    runtime: StaminaRecoverMultipleRuntime,
    *,
    contract: MultipleEffectContract | None,
    simulated: bool,
    reason: str,
    trace: Sequence[str],
    base: int | None = None,
    scaled: float | None = None,
    requested: int | None = None,
    fix_evaluation: StaminaRecoverFixEvaluation | None = None,
) -> StaminaRecoverMultipleEvaluation:
    return StaminaRecoverMultipleEvaluation(
        contract=contract,
        runtime_before=runtime,
        runtime_after=runtime,
        executable=False,
        simulated=simulated,
        reason=reason,
        base_max_stamina=base,
        scaled_float32=scaled,
        requested_value=requested,
        adjusted_recovery_value=None,
        calculated_recovery=None,
        predicted_stamina=None,
        difference=None,
        trace=tuple(trace),
        fix_evaluation=fix_evaluation,
    )


def evaluate_stamina_recover_multiple(
    effect: MultipleEffectContract | Mapping[str, object],
    runtime: StaminaRecoverMultipleRuntime,
    *,
    simulate: bool = False,
) -> StaminaRecoverMultipleEvaluation:
    """Evaluate Multiple, then reuse the proven Fix recovery/cap primitive."""

    if not isinstance(runtime, StaminaRecoverMultipleRuntime):
        raise TypeError("runtime must be StaminaRecoverMultipleRuntime")
    if type(simulate) is not bool:
        raise TypeError("simulate must be bool")
    contract: MultipleEffectContract | None
    if isinstance(effect, MultipleEffectContract):
        contract = effect
    elif isinstance(effect, Mapping):
        contract = try_parse_stamina_recover_multiple(effect)
    else:
        contract = None
    if contract is None:
        return _unresolved_multiple(
            runtime,
            contract=None,
            simulated=simulate,
            reason="unknown or unsupported ExamStaminaRecoverMultiple shape",
            trace=("contract:unresolved",),
        )
    if runtime.max_stamina < 0:
        return _unresolved_multiple(
            runtime,
            contract=contract,
            simulated=simulate,
            reason="negative max_stamina is outside the proven native runtime shape",
            trace=("runtime:unresolved",),
        )
    if runtime.stamina < 0:
        return _unresolved_multiple(
            runtime,
            contract=contract,
            simulated=simulate,
            reason="negative stamina is outside the proven native runtime shape",
            trace=("runtime:unresolved",),
        )
    if runtime.stamina > runtime.max_stamina:
        return _unresolved_multiple(
            runtime,
            contract=contract,
            simulated=simulate,
            reason="stamina exceeds max_stamina",
            trace=("runtime:unresolved",),
        )
    try:
        scaled, requested = _native_multiple_request(
            runtime.max_stamina, contract.effect_value1
        )
    except (OverflowError, struct.error, ValueError, ReviewStaminaContractError) as exc:
        return _unresolved_multiple(
            runtime,
            contract=contract,
            simulated=simulate,
            reason=f"Multiple float32 conversion is unproven: {exc}",
            trace=("multiple:float32-unresolved",),
            base=runtime.max_stamina,
        )
    fix_contract = StaminaRecoverFixContract(
        effect_id=f"{contract.effect_id}:fixed-request",
        effect_value1=requested,
    )
    fix_result = evaluate_stamina_recover_fix(
        fix_contract, runtime, simulate=simulate
    )
    trace = (
        "multiple:read_max_stamina",
        "multiple:read_effect_value1",
        "multiple:float32_mul",
        "multiple:float32_div_1000_permille",
        "multiple:frintp_fcvtps_ceil",
        *fix_result.trace,
    )
    if not fix_result.executable:
        return _unresolved_multiple(
            runtime,
            contract=contract,
            simulated=simulate,
            reason=fix_result.reason or "shared recovery primitive unresolved",
            trace=trace,
            base=runtime.max_stamina,
            scaled=scaled,
            requested=requested,
            fix_evaluation=fix_result,
        )
    return StaminaRecoverMultipleEvaluation(
        contract=contract,
        runtime_before=runtime,
        runtime_after=fix_result.runtime_after,
        executable=True,
        simulated=simulate,
        reason=None,
        base_max_stamina=runtime.max_stamina,
        scaled_float32=scaled,
        requested_value=requested,
        adjusted_recovery_value=fix_result.adjusted_recovery_value,
        calculated_recovery=fix_result.calculated_recovery,
        predicted_stamina=fix_result.predicted_stamina,
        difference=fix_result.difference,
        trace=trace,
        fix_evaluation=fix_result,
    )


evaluate_multiple = evaluate_stamina_recover_multiple


def evaluate_target_card_blockers(
    catalog: ReviewStaminaCatalog,
    upgrade: int,
    review_inputs: ReviewUp1EvaluationInput,
    runtime: StaminaRecoverMultipleRuntime,
    *,
    simulate_effect: bool = False,
) -> tuple[ReviewUp1Evaluation, StaminaRecoverMultipleEvaluation]:
    """Evaluate both proven blocker surfaces for one exact card version."""

    if not catalog.exact_shape_supported:
        review = _unsupported_review(
            TARGET_TRIGGER_ID,
            review_inputs,
            ("target-card-catalog-shape-unresolved",),
        )
        effect = _unresolved_multiple(
            runtime,
            contract=None,
            simulated=simulate_effect,
            reason="target-card-catalog-shape-unresolved",
            trace=("catalog:unresolved",),
        )
        return review, effect
    card = catalog.card_version(upgrade)
    review = evaluate_review_up1_trigger(review_inputs, catalog.trigger)
    effect = evaluate_stamina_recover_multiple(
        catalog.multiple_effect(card.upgrade_count), runtime, simulate=simulate_effect
    )
    return review, effect


NATIVE_EVIDENCE: Final[dict[str, object]] = {
    "review_up": {
        "android_method": "Campus.InGame.ExamExtensions.IsFieldStatusTriggerStatusEffect",
        "android_method_va": "0x68082D4",
        "field_enum_value": 35,
        "field_status_type": FIELD_REVIEW_UP,
        "getter": "ExamStatusEffectCollection.GetReview",
        "getter_locator": f"{ANDROID_V323_ISIL_EXTENSIONS_SOURCE}:15917",
        "value_source": "current signed Review scalar/status",
        "delta_source": "not read; observed delta and ReviewCountAdd are inert",
        "comparison": "signed current_review >= 1",
        "inclusive": True,
        "boundary": {"review_0": False, "review_1": True},
    },
    "start_turn": {
        "snapshot": "settled-start-turn-post-draw",
        "gate": "pre-payment direct PlayEffect card gate",
        "ordinary": True,
        "forced": False,
        "extra": False,
        "order": [
            "current turn/status/full-power/gimmick/draw settlement",
            "ExamStartTurn settled snapshot",
            "ReviewUp current-status gate",
            "ExamSequence.ConsumeCardCost",
            "ordered card PlayEffect slots",
            "card move",
        ],
        "forced_extra_policy": "unresolved; no target-specific native proof",
    },
    "stamina_recover_multiple": {
        "executor": "Campus.InGame.Exam.StaminaRecoverMultipleEffectExecutor",
        "constructor_va": "0x7E8D78C",
        "execute_va": "0x7E8D844",
        "calculate_va": "0x7E5EC2C",
        "add_fix_va": "0x7E5EDE0",
        "base": "MaxStamina",
        "not_base": ["current stamina", "missing amount"],
        "unit": "permille",
        "formula": "ceil_f32(f32(f32(MaxStamina)*f32(effectValue1))/f32(1000.0f))",
        "rounding": [
            "binary32 MaxStamina/effectValue1 conversion",
            "binary32 multiply",
            "binary32 divide by 1000.0f",
            "FRINTP/FCVTPS ceiling; no -0.0001f epsilon in Multiple",
            "ARM positive-infinity/out-of-range Int32 indefinite sentinel",
        ],
        "post_formula_order": [
            "CalculateStaminaRecover",
            "active StaminaRecoverRestriction returns 0",
            "active StaminaRecoverAdd adjusts by ceil_f32 ratio term",
            "first cap: min(MaxStamina, int32(current+adjusted))-current",
            "AddStaminaFix",
            "second cap: max(0, min(int32(current+calculated), MaxStamina))",
            "difference create/set/append",
            "ExamParameterModel.SetStamina",
            "EffectDifferenceExecuted callback",
        ],
        "bounds": {
            "zero_max_zero_current": "supported no-op: request/recovery/final are 0",
            "negative_max": "fail-closed; native runtime/setter domain is not proven",
            "negative_current": "fail-closed",
            "over_cap": "fail-closed",
            "effect_value_le_zero": "known native value<1 path produces 0 and never decreases stamina",
        },
    },
}


def build_native_audit(
    catalog: ReviewStaminaCatalog | None = None,
) -> dict[str, object]:
    """Build the standalone audit payload without touching formal coverage."""

    catalog = catalog or load_catalog()
    return {
        "audit_id": "plan2_start_turn_review_stamina_recover_native_audit",
        "scope": {
            "plan": "Plan2",
            "target_card": TARGET_CARD_ID,
            "target_card_name": TARGET_CARD_NAME,
            "target_upgrades": list(TARGET_UPGRADES),
            "bounded_standalone_whole_card": True,
            "central_coverage_rebuilt": False,
            "central_coverage_integrated": False,
            "central_core_modified": False,
            "plan3_modified": False,
            "gui_modified": False,
            "existing_files_modified": False,
            "formal_coverage_artifact_modified": False,
            "excluded": [
                "central core",
                "formal coverage artifact",
                "Plan3",
                "GUI",
                "security/hash validation",
                "clock validation",
            ],
        },
        "summary": catalog.summary(),
        "master": {
            "database": "var/master.sqlite3",
            "trigger": None if catalog.trigger is None else catalog.trigger.to_dict(),
            "card_versions": [row.to_dict() for row in catalog.card_versions],
            "effect_shapes": [row.to_dict() for row in catalog.effect_shapes],
        },
        "native_semantics": NATIVE_EVIDENCE,
        "allowed_sources": {
            "android_native": [ANDROID_V323_NATIVE_SOURCE],
            "android_metadata": [
                ANDROID_V323_METADATA_SOURCE,
                ANDROID_V323_TARGET_METADATA_SOURCE,
                ANDROID_V323_EXECUTOR_MAPPING_SOURCE,
                ANDROID_V323_ISIL_EXTENSIONS_SOURCE,
                ANDROID_V323_ISIL_SEQUENCE_SOURCE,
                ANDROID_V323_ISIL_LOOP_SOURCE,
            ],
            "pc_metadata": [PC_METADATA_SOURCE, PC_NATIVE_SCAN_SOURCE],
            "existing_plan2_primitives": [
                "src/gkms_tool/plan2_end_turn_review_up3.py",
                "src/gkms_tool/plan2_stamina_recover_fix.py",
            ],
            "plan3_reference_only": "src/gkms_tool/plan3_engine.py",
        },
        "card_effect_execution": {
            "this_module_executes": [
                "ReviewUp1 predicate",
                "ExamStaminaRecoverMultiple pure prediction",
            ],
            "this_module_does_not_execute": [
                "ExamReview",
                "ExamBlock",
                "ExamPlayableValueAdd",
                "card payment",
                "card move",
            ],
            "order_claim": "shape/order only; existing Plan2 primitives own other effects",
        },
        "verification": {
            "test_file": "tests/test_plan2_start_turn_review_stamina_recover.py",
            "allowed_adjacent_focused_files": 3,
            "central_coverage_updated": False,
        },
    }


def catalog_row_dict(row: TargetCardVersion) -> dict[str, object]:
    return row.to_dict()


__all__ = [
    "ANDROID_V323_EXECUTOR_MAPPING_SOURCE",
    "ANDROID_V323_ISIL_EXTENSIONS_SOURCE",
    "ANDROID_V323_ISIL_LOOP_SOURCE",
    "ANDROID_V323_ISIL_SEQUENCE_SOURCE",
    "ANDROID_V323_METADATA_SOURCE",
    "ANDROID_V323_NATIVE_SOURCE",
    "ANDROID_V323_TARGET_METADATA_SOURCE",
    "BLOCK_EFFECT_TYPE",
    "CardEffectShape",
    "CardEffectSlot",
    "CardExecutionKind",
    "ContractResolution",
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_DATABASE",
    "DIRECT_DISPATCH_PHASE",
    "EvaluationBoundary",
    "EvaluationStage",
    "EXACT_TARGET_TRIGGER",
    "ExecutionMode",
    "FIELD_REVIEW_UP",
    "INT32_MAX",
    "INT32_MIN",
    "LESSON_UNKNOWN",
    "MENTAL_SKILL",
    "MOVE_LOST",
    "MOVE_UNKNOWN",
    "MULTIPLE_EFFECT_TYPE",
    "MultipleEffectContract",
    "NATIVE_EVIDENCE",
    "PC_METADATA_SOURCE",
    "PC_NATIVE_SCAN_SOURCE",
    "PHASE_EXAM_START_TURN",
    "PLAN2",
    "PLAYABLE_VALUE_ADD_EFFECT_TYPE",
    "REVIEW_EFFECT_TYPE",
    "REVIEW_UP1_TRIGGER",
    "ReviewStaminaCatalog",
    "ReviewStaminaContractError",
    "ReviewUp1Evaluation",
    "ReviewUp1EvaluationInput",
    "ReviewUp1Trigger",
    "StartTurnSnapshotBoundary",
    "StaminaDifference",
    "StaminaRecoverFixEvaluation",
    "StaminaRecoverMultipleEvaluation",
    "StaminaRecoverMultipleRuntime",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_CARD_NAMES_BY_UPGRADE",
    "TARGET_CARD_STAMINA_BY_UPGRADE",
    "TARGET_EFFECT_IDS_BY_UPGRADE",
    "TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "TargetCardVersion",
    "build_native_audit",
    "catalog_row_dict",
    "evaluate_multiple",
    "evaluate_plan2_start_turn_review_up1",
    "evaluate_review_up1",
    "evaluate_review_up1_trigger",
    "evaluate_stamina_recover_multiple",
    "evaluate_target_card_blockers",
    "load_catalog",
    "load_plan2_start_turn_review_stamina_recover_catalog",
    "resolve_review_up1_trigger",
    "try_parse_stamina_recover_multiple",
]
