"""Standalone Plan2 card-admission gate for Aggressive >= 9.

This leaf owns only the ``playTriggerId`` use of
``e_trigger-none-card_play_aggressive_up-9``.  It deliberately does not
install an executor, import the Plan2 core, or claim that the five cards are
playable end-to-end: every affected card still has one independent coverage
gap.

The Android v3.2.3 call chain makes ``ProduceExamPhaseType_None`` a literal
phase value for this card-owned predicate.  The value is read while the card
is Playing, before payment and before any direct PlayEffect command runs.
The predicate is the signed current Aggressive value compared inclusively
with nine.  Counts, deltas, and a later same-card effect state are not inputs
to this admission gate.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import (
    AggressiveTriggerRow,
    CardEffectSlot,
    CHECK_NOT,
    DIRECT_DISPATCH_PHASE,
    FIELD_CARD_PLAY_AGGRESSIVE_UP,
    LESSON_UNKNOWN,
    MOVE_UNKNOWN,
    PHASE_NONE,
    PLAN2,
    resolve_aggressive_trigger,
)
from .plan2_card_and_effect_aggressive_up6 import AggressiveGateBoundary


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COVERAGE_ARTIFACT = PROJECT_ROOT / "var" / "coverage" / "plan2_card_executable_coverage.json"
NATIVE_AUDIT_ARTIFACT = PROJECT_ROOT / "var" / "coverage" / "plan2_card_trigger_aggressive_up9_native_audit.json"

TARGET_TRIGGER_ID: Final = "e_trigger-none-card_play_aggressive_up-9"
TARGET_THRESHOLD: Final = 9
TARGET_CARD_LEVEL_REFS: Final = (
    ("p_card-02-ido-3_140", 0),
    ("p_card-02-ido-3_140", 1),
    ("p_card-02-ido-3_140", 2),
    ("p_card-02-ido-3_140", 3),
    ("p_card-02-men-100_011", 0),
)
TARGET_AFFECTED_REFS: Final = TARGET_CARD_LEVEL_REFS
SUPPORTED_PLAN_TYPES: Final = (PLAN2,)
CARD_ORIGINS: Final = ("normal", "forced", "extra")

CARD_IDO_140: Final = "p_card-02-ido-3_140"
CARD_MEN_100_011: Final = "p_card-02-men-100_011"
MENTAL_SKILL_CATEGORY: Final = "ProduceCardCategory_MentalSkill"
COST_TYPE_UNKNOWN: Final = "ExamCostType_Unknown"
CARD_MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"

TARGET_NAMES_BY_REF: Final = {
    (CARD_IDO_140, 0): "私は、決して",
    (CARD_IDO_140, 1): "私は、決して+",
    (CARD_IDO_140, 2): "私は、決して++",
    (CARD_IDO_140, 3): "私は、決して+++",
    (CARD_MEN_100_011, 0): "最強パフォーマー",
}
TARGET_STAMINA_BY_REF: Final = {
    (CARD_IDO_140, 0): 6,
    (CARD_IDO_140, 1): 4,
    (CARD_IDO_140, 2): 4,
    (CARD_IDO_140, 3): 4,
    (CARD_MEN_100_011, 0): 8,
}
TARGET_EFFECT_IDS_BY_REF: Final = {
    (CARD_IDO_140, 0): (
        "e_effect-exam_lesson_value_multiple_depend_review_or_aggressive-05",
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_review-0008",
        "e_effect-exam_card_draw-0001",
    ),
    (CARD_IDO_140, 1): (
        "e_effect-exam_lesson_value_multiple_depend_review_or_aggressive-05",
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_review-0009",
        "e_effect-exam_card_draw-0001",
    ),
    (CARD_IDO_140, 2): (
        "e_effect-exam_lesson_value_multiple_depend_review_or_aggressive-05",
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_review-0011",
        "e_effect-exam_card_draw-0001",
    ),
    (CARD_IDO_140, 3): (
        "e_effect-exam_lesson_value_multiple_depend_review_or_aggressive-05",
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_review-0013",
        "e_effect-exam_card_draw-0001",
    ),
    (CARD_MEN_100_011, 0): (
        "e_effect-exam_playable_value_add-01",
        "e_effect-exam_card_play_aggressive-0005",
        "e_effect-exam_status_enchant-inf-enchant-p_card-02-men-100_011-enc01",
    ),
}
TARGET_BLOCKER_GAPS_BY_REF: Final = {
    (CARD_IDO_140, 0): (
        "C:effect:ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive",
    ),
    (CARD_IDO_140, 1): (
        "C:effect:ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive",
    ),
    (CARD_IDO_140, 2): (
        "C:effect:ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive",
    ),
    (CARD_IDO_140, 3): (
        "C:effect:ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive",
    ),
    (CARD_MEN_100_011, 0): (
        "C:status-trigger:e_trigger-exam_stamina_reduce_card",
    ),
}


class AggressiveUp9CatalogError(ValueError):
    """The local Master or coverage readback is outside this exact leaf."""


@dataclass(frozen=True, slots=True)
class ExactAggressiveUp9Contract:
    """The one supported trigger row and its native scalar predicate."""

    row: AggressiveTriggerRow
    threshold: int = TARGET_THRESHOLD
    comparison: str = "signed_greater_equal"
    value_source: str = "signed current ExamStatusEffectCollection.GetAggressive(true)"
    evaluation_boundary: str = "Playing/card-admission-before-payment"

    @property
    def result_expression(self) -> str:
        return f"signed_aggressive_value >= {self.threshold}"

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "phase": self.row.phase_types[0],
            "threshold": self.threshold,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "valueSource": self.value_source,
            "evaluationBoundary": self.evaluation_boundary,
            "checkNot": False,
        }


@dataclass(frozen=True, slots=True)
class ExactContractResolution:
    supported: bool
    contract: ExactAggressiveUp9Contract | None
    reasons: tuple[str, ...] = ()


EXACT_TARGET_TRIGGER = AggressiveTriggerRow(
    id=TARGET_TRIGGER_ID,
    phase_types=(PHASE_NONE,),
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


def _strict_int(value: object, label: str, *, signed: bool = True) -> int:
    if type(value) is not int:
        raise AggressiveUp9CatalogError(f"{label} must be a plain integer")
    if signed and not INT32_MIN <= value <= INT32_MAX:
        raise AggressiveUp9CatalogError(f"{label} is outside signed Int32")
    if not signed and value < 0:
        raise AggressiveUp9CatalogError(f"{label} must be non-negative")
    return value


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(value if isinstance(value, str) else str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise AggressiveUp9CatalogError(f"{label}: invalid JSON array") from error
    if not isinstance(parsed, list):
        raise AggressiveUp9CatalogError(f"{label}: expected JSON array")
    return parsed


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value if isinstance(value, str) else str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise AggressiveUp9CatalogError(f"{label}: invalid JSON object") from error
    if not isinstance(parsed, dict):
        raise AggressiveUp9CatalogError(f"{label}: expected JSON object")
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not str for item in parsed):
        raise AggressiveUp9CatalogError(f"{label}: expected string entries")
    return tuple(parsed)  # type: ignore[arg-type]


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not int for item in parsed):
        raise AggressiveUp9CatalogError(f"{label}: expected integer entries")
    return tuple(parsed)  # type: ignore[arg-type]


def _strict_raw_contract(
    raw: Mapping[str, object], expected: Mapping[str, object], label: str
) -> None:
    for key, wanted in expected.items():
        if key not in raw:
            raise AggressiveUp9CatalogError(f"{label}: missing raw field {key}")
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise AggressiveUp9CatalogError(
                f"{label}: raw field {key} is {actual!r}, expected {wanted!r}"
            )


def _trigger_from_db_row(row: sqlite3.Row) -> AggressiveTriggerRow:
    trigger_id = str(row["id"])
    typed = AggressiveTriggerRow(
        id=trigger_id,
        phase_types=_string_tuple(row["phase_types_json"], f"{trigger_id}.phaseTypes"),
        phase_values=_int_tuple(row["phase_values_json"], f"{trigger_id}.phaseValues"),
        field_status_check_types=_string_tuple(
            row["field_status_check_types_json"],
            f"{trigger_id}.fieldStatusCheckTypes",
        ),
        field_status_types=_string_tuple(
            row["field_status_types_json"], f"{trigger_id}.fieldStatusTypes"
        ),
        field_status_values=_int_tuple(
            row["field_status_values_json"], f"{trigger_id}.fieldStatusValues"
        ),
        field_status_card_search_ids=_string_tuple(
            row["field_status_produce_card_search_ids_json"],
            f"{trigger_id}.fieldStatusProduceCardSearchIds",
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_strict_int(
            row["upper_search_count"], f"{trigger_id}.upperSearchCount", signed=False
        ),
        lower_search_count=_strict_int(
            row["lower_search_count"], f"{trigger_id}.lowerSearchCount", signed=False
        ),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=_string_tuple(row["effect_types_json"], f"{trigger_id}.effectTypes"),
        lesson_type=str(row["lesson_type"]),
    )
    raw = _json_object(row["raw_json"], trigger_id)
    _strict_raw_contract(
        raw,
        {
            "id": TARGET_TRIGGER_ID,
            "phaseTypes": [PHASE_NONE],
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
    return typed


def resolve_exact_aggressive_up9_trigger(
    row: AggressiveTriggerRow,
) -> ExactContractResolution:
    """Resolve only the exact positive None/Up9 row; all drift fails closed."""

    if not isinstance(row, AggressiveTriggerRow):
        return ExactContractResolution(
            False, None, ("trigger-row-type-is-unsupported",)
        )

    reasons: list[str] = []
    generic = resolve_aggressive_trigger(row)
    if not generic.supported:
        reasons.extend(generic.reasons)
    if row.id != TARGET_TRIGGER_ID:
        reasons.append("trigger-id-is-not-exact-target")
    if row.phase_types != (PHASE_NONE,):
        reasons.append("trigger-phase-is-not-none")
    if row.phase_values:
        reasons.append("trigger-phase-values-must-be-empty")
    if row.field_status_check_types:
        if CHECK_NOT in row.field_status_check_types:
            reasons.append("trigger-not-check-unsupported")
        else:
            reasons.append("trigger-field-check-shape-unsupported")
    if row.field_status_types != (FIELD_CARD_PLAY_AGGRESSIVE_UP,):
        reasons.append("trigger-field-is-not-card-play-aggressive-up")
    if row.field_status_values != (TARGET_THRESHOLD,):
        reasons.append("trigger-value-shape-is-not-exact-nine")
    if row.field_status_card_search_ids:
        reasons.append("trigger-field-search-is-unsupported")
    if row.produce_card_search_id:
        reasons.append("trigger-card-search-is-unsupported")
    if row.upper_search_count != 0 or row.lower_search_count != 0:
        reasons.append("trigger-search-count-shape-is-unsupported")
    if row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("trigger-card-move-shape-is-unsupported")
    if row.effect_types:
        reasons.append("trigger-effect-type-shape-is-unsupported")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("trigger-lesson-shape-is-unsupported")
    unique = tuple(dict.fromkeys(reasons))
    if unique:
        return ExactContractResolution(False, None, unique)
    return ExactContractResolution(True, ExactAggressiveUp9Contract(row), ())


@dataclass(frozen=True, slots=True)
class AggressiveUp9GateInput:
    """Read-only inputs captured at the Playing/card-admission boundary."""

    aggressive_status_value: int
    phase: str = PHASE_NONE
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: AggressiveGateBoundary = AggressiveGateBoundary.CARD_PLAY_BEFORE_PAYMENT
    card_origin: Literal["normal", "forced", "extra"] = "normal"
    is_use_playable_count: bool = True
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    observed_aggressive_delta: int | None = None

    def __post_init__(self) -> None:
        _strict_int(self.aggressive_status_value, "aggressive_status_value")
        for name in ("global_card_play_count", "turn_card_play_count"):
            _strict_int(getattr(self, name), name, signed=False)
        if self.observed_aggressive_delta is not None:
            _strict_int(self.observed_aggressive_delta, "observed_aggressive_delta")
        if type(self.is_use_playable_count) is not bool:
            raise TypeError("is_use_playable_count must be bool")
        if type(self.phase) is not str or not self.phase:
            raise TypeError("phase must be a non-empty string")
        if type(self.dispatch_phase) is not str or not self.dispatch_phase:
            raise TypeError("dispatch_phase must be a non-empty string")
        try:
            object.__setattr__(self, "boundary", AggressiveGateBoundary(self.boundary))
        except (TypeError, ValueError) as error:
            raise TypeError("boundary must be an AggressiveGateBoundary") from error
        if self.card_origin not in CARD_ORIGINS:
            raise ValueError("card_origin must be normal, forced, or extra")


@dataclass(frozen=True, slots=True)
class AggressiveUp9GateEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    aggressive_status_value: int | None
    threshold: int | None
    comparison: str | None
    check_not: bool | None
    phase: str
    dispatch_phase: str
    boundary: AggressiveGateBoundary
    card_origin: str
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def value_source(self) -> str:
        return "signed current ExamStatusEffectCollection.GetAggressive(true)"

    @property
    def snapshot_rule(self) -> str:
        return (
            "Playing/card-admission snapshot before payment and direct effects; "
            "same-card prior effects are not read"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "aggressiveStatusValue": self.aggressive_status_value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "checkNot": self.check_not,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "cardOrigin": self.card_origin,
            "valueSource": self.value_source,
            "snapshotRule": self.snapshot_rule,
            "reasons": list(self.reasons),
        }


AggressiveGateInput = AggressiveUp9GateInput
AggressiveGateEvaluation = AggressiveUp9GateEvaluation


def _trigger_id(trigger: object) -> str:
    return trigger.id if isinstance(trigger, AggressiveTriggerRow) else "<unknown-trigger>"


def _unsupported_gate(
    trigger: object,
    inputs: AggressiveUp9GateInput,
    reasons: Sequence[str],
    *,
    threshold: int | None = None,
    comparison: str | None = None,
    check_not: bool | None = None,
) -> AggressiveUp9GateEvaluation:
    return AggressiveUp9GateEvaluation(
        trigger_id=_trigger_id(trigger),
        supported=False,
        fires=None,
        aggressive_status_value=None,
        threshold=threshold,
        comparison=comparison,
        check_not=check_not,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        card_origin=inputs.card_origin,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_card_play_aggressive_up9_trigger(
    inputs: AggressiveUp9GateInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
) -> AggressiveUp9GateEvaluation:
    """Evaluate the exact card-level gate before payment and direct effects."""

    if not isinstance(inputs, AggressiveUp9GateInput):
        raise TypeError("inputs must be AggressiveUp9GateInput")
    resolution = resolve_exact_aggressive_up9_trigger(trigger)
    if not resolution.supported:
        return _unsupported_gate(trigger, inputs, resolution.reasons)
    contract = resolution.contract
    assert contract is not None
    reasons: list[str] = []
    if inputs.phase != PHASE_NONE:
        reasons.append("runtime-phase-is-not-none")
    if inputs.dispatch_phase != DIRECT_DISPATCH_PHASE:
        reasons.append("dispatch-phase-is-not-play-effect")
    if inputs.boundary is not AggressiveGateBoundary.CARD_PLAY_BEFORE_PAYMENT:
        reasons.append("runtime-boundary-is-not-card-play-before-payment")
    if reasons:
        return _unsupported_gate(
            trigger,
            inputs,
            reasons,
            threshold=contract.threshold,
            comparison=contract.comparison,
            check_not=False,
        )
    return AggressiveUp9GateEvaluation(
        trigger_id=trigger.id,
        supported=True,
        fires=inputs.aggressive_status_value >= contract.threshold,
        aggressive_status_value=inputs.aggressive_status_value,
        threshold=contract.threshold,
        comparison=contract.comparison,
        check_not=False,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        card_origin=inputs.card_origin,
    )


evaluate_card_play_gate = evaluate_card_play_aggressive_up9_trigger
evaluate_card_trigger = evaluate_card_play_aggressive_up9_trigger


@dataclass(frozen=True, slots=True)
class CardAdmissionSimulation:
    """Repeated pure probes over one supplied pre-payment snapshot."""

    snapshot_aggressive_status_value: int
    evaluations: tuple[AggressiveUp9GateEvaluation, ...]

    @property
    def supported(self) -> bool:
        return bool(self.evaluations) and all(item.supported for item in self.evaluations)

    @property
    def fires(self) -> bool | None:
        if not self.evaluations or not self.supported:
            return None
        return all(item.fires is True for item in self.evaluations)

    @property
    def snapshot_rule(self) -> str:
        return (
            "one signed current Aggressive read while Playing before payment; "
            "later same-card effects do not rewrite this admission snapshot"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "snapshotAggressiveStatusValue": self.snapshot_aggressive_status_value,
            "evaluations": [item.to_dict() for item in self.evaluations],
            "fires": self.fires,
            "snapshotRule": self.snapshot_rule,
        }


def simulate_card_admission(
    inputs: AggressiveUp9GateInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
    *,
    repetitions: int = 1,
) -> CardAdmissionSimulation:
    """Evaluate repeated card invocations without consulting later effects."""

    if not isinstance(inputs, AggressiveUp9GateInput):
        raise TypeError("inputs must be AggressiveUp9GateInput")
    if type(repetitions) is not int:
        raise TypeError("repetitions must be an integer")
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    return CardAdmissionSimulation(
        snapshot_aggressive_status_value=inputs.aggressive_status_value,
        evaluations=tuple(
            evaluate_card_play_aggressive_up9_trigger(inputs, trigger)
            for _ in range(repetitions)
        ),
    )


simulate_aggressive_card_play = simulate_card_admission


@dataclass(frozen=True, slots=True)
class Plan2CardTriggerAggressiveUp9CardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    move_position_type: str
    play_trigger_id: str
    effect_slots: tuple[CardEffectSlot, ...]

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade_count

    @property
    def effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "movePositionType": self.move_position_type,
            "playTriggerId": self.play_trigger_id,
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
        }


AggressiveUp9CardVersion = Plan2CardTriggerAggressiveUp9CardVersion


@dataclass(frozen=True, slots=True)
class CardTriggerCoverageReadback:
    source: str
    classification: str
    instances: int
    card_gap_refs: tuple[tuple[str, int], ...]
    direct_refs: tuple[tuple[str, int], ...]
    co_blocked_refs: tuple[tuple[str, int], ...]
    remaining_gaps: tuple[tuple[tuple[str, int], tuple[str, ...]], ...]

    @property
    def affected_count(self) -> int:
        return len(self.card_gap_refs)

    @property
    def direct_count(self) -> int:
        return len(self.direct_refs)

    @property
    def co_blocked_count(self) -> int:
        return len(self.co_blocked_refs)

    @property
    def card_trigger_classification(self) -> str:
        return self.classification

    @property
    def card_trigger_instances(self) -> int:
        return self.instances

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "scope": "card-level playTriggerId only; effect-trigger inventory excluded",
            "slotInventory": {
                "cardTrigger": {
                    "classification": self.classification,
                    "instances": self.instances,
                }
            },
            "cardGapRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.card_gap_refs
            ],
            "directRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.direct_refs
            ],
            "coBlockedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.co_blocked_refs
            ],
            "remainingGaps": [
                {"cardId": card_id, "upgrade": upgrade, "gaps": list(gaps)}
                for (card_id, upgrade), gaps in self.remaining_gaps
            ],
            "affected": self.affected_count,
            "direct": self.direct_count,
            "coBlocked": self.co_blocked_count,
        }


CoverageReadback = CardTriggerCoverageReadback


def _coverage_ref(value: object, label: str) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise AggressiveUp9CatalogError(f"{label}: expected object")
    card_id = value.get("card_id")
    upgrade = value.get("upgrade")
    if type(card_id) is not str or type(upgrade) is not int:
        raise AggressiveUp9CatalogError(f"{label}: malformed card ref")
    return card_id, upgrade


def load_card_trigger_coverage_readback(
    artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
) -> CardTriggerCoverageReadback:
    """Read the formal artifact without rebuilding or modifying it."""

    path = Path(artifact)
    if not path.is_file():
        raise AggressiveUp9CatalogError(f"coverage artifact not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AggressiveUp9CatalogError(f"cannot read coverage artifact: {path}") from error
    if not isinstance(data, dict):
        raise AggressiveUp9CatalogError("coverage artifact must be a JSON object")

    inventory = data.get("slot_inventory")
    if not isinstance(inventory, list):
        raise AggressiveUp9CatalogError("coverage slot_inventory is missing")
    card_inventory = [
        row
        for row in inventory
        if isinstance(row, dict)
        and row.get("kind") == "card-trigger"
        and row.get("id") == TARGET_TRIGGER_ID
    ]
    if len(card_inventory) != 1:
        raise AggressiveUp9CatalogError("target card-trigger inventory row is not unique")
    card_row = card_inventory[0]
    integrated = card_row.get("classification") == "B"
    if card_row.get("classification") not in {"B", "C"}:
        raise AggressiveUp9CatalogError(
            "target card-trigger is outside the standalone/integrated contract"
        )
    if type(card_row.get("instances")) is not int:
        raise AggressiveUp9CatalogError("target card-trigger instances are malformed")
    if card_row["instances"] != len(TARGET_CARD_LEVEL_REFS):
        raise AggressiveUp9CatalogError("target card-trigger instance count changed")

    cards = data.get("cards")
    if not isinstance(cards, list):
        raise AggressiveUp9CatalogError("coverage cards section is missing")
    target_gap = f"C:card-trigger:{TARGET_TRIGGER_ID}"
    expected_rows: dict[tuple[str, int], tuple[str, ...]] = {}
    for index, row in enumerate(cards):
        if not isinstance(row, dict):
            continue
        ref = _coverage_ref(row, f"coverage cards[{index}]")
        if ref not in TARGET_CARD_LEVEL_REFS:
            continue
        if ref in expected_rows:
            raise AggressiveUp9CatalogError(f"duplicate target coverage row: {ref!r}")
        gaps = row.get("gaps")
        if not isinstance(gaps, list) or any(type(gap) is not str for gap in gaps):
            raise AggressiveUp9CatalogError(f"coverage cards[{index}].gaps is malformed")
        expected = (target_gap, *TARGET_BLOCKER_GAPS_BY_REF[ref])
        observed = tuple(gaps)
        if integrated:
            batch = row.get("plan2_aggressive_stamina_batch_overlay")
            family = row.get("card_trigger_aggressive_up9_overlay")
            if not isinstance(batch, dict) or not isinstance(family, dict):
                raise AggressiveUp9CatalogError(
                    f"{ref!r}: integrated coverage overlays are missing"
                )
            previous_gaps = batch.get("previous_gaps")
            if not isinstance(previous_gaps, list) or any(
                type(gap) is not str for gap in previous_gaps
            ):
                raise AggressiveUp9CatalogError(
                    f"{ref!r}: integrated previous gaps are malformed"
                )
            if family.get("slots_executable") is not True:
                raise AggressiveUp9CatalogError(
                    f"{ref!r}: integrated card gate is not executable"
                )
            observed = tuple(previous_gaps)
        if observed != expected:
            raise AggressiveUp9CatalogError(
                f"{ref!r}: remaining gap shape changed: {observed!r}"
            )
        expected_rows[ref] = observed
    if tuple(expected_rows) != TARGET_CARD_LEVEL_REFS:
        raise AggressiveUp9CatalogError(
            f"target coverage refs changed: {tuple(expected_rows)!r}"
        )

    remaining = tuple((ref, tuple(gaps[1:])) for ref, gaps in expected_rows.items())
    direct_refs = tuple(ref for ref, gaps in remaining if not gaps)
    co_blocked_refs = tuple(ref for ref, gaps in remaining if gaps)
    return CardTriggerCoverageReadback(
        source=str(path),
        classification="C",
        instances=int(card_row["instances"]),
        card_gap_refs=TARGET_CARD_LEVEL_REFS,
        direct_refs=direct_refs,
        co_blocked_refs=co_blocked_refs,
        remaining_gaps=remaining,
    )


@dataclass(frozen=True, slots=True)
class Plan2CardTriggerAggressiveUp9Catalog:
    database: str
    trigger: AggressiveTriggerRow
    card_level_versions: tuple[Plan2CardTriggerAggressiveUp9CardVersion, ...]
    affected_versions: tuple[Plan2CardTriggerAggressiveUp9CardVersion, ...]
    direct_versions: tuple[Plan2CardTriggerAggressiveUp9CardVersion, ...]
    co_blocked_versions: tuple[Plan2CardTriggerAggressiveUp9CardVersion, ...]
    coverage: CardTriggerCoverageReadback

    @property
    def affected_count(self) -> int:
        return len(self.affected_versions)

    @property
    def direct_count(self) -> int:
        return len(self.direct_versions)

    @property
    def co_blocked_count(self) -> int:
        return len(self.co_blocked_versions)

    @property
    def executable(self) -> bool:
        return False

    def version_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(card.ref for card in self.card_level_versions)

    def summary(self) -> dict[str, object]:
        return {
            "triggerId": TARGET_TRIGGER_ID,
            "cardLevelPlayGateVersions": len(self.card_level_versions),
            "affected": self.affected_count,
            "direct": self.direct_count,
            "coBlocked": self.co_blocked_count,
            "executable": self.executable,
            "cardLevelRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.version_refs()
            ],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "trigger": self.trigger.to_dict(),
            "summary": self.summary(),
            "cardLevelVersions": [card.to_dict() for card in self.card_level_versions],
            "coverage": self.coverage.to_dict(),
        }


def _strict_card_slot(
    card_id: str,
    upgrade: int,
    index: int,
    raw: object,
    expected_effect_id: str,
) -> CardEffectSlot:
    if not isinstance(raw, dict):
        raise AggressiveUp9CatalogError(f"{card_id}#{upgrade}: slot {index} is not an object")
    if set(raw) != {
        "produceExamTriggerId",
        "produceExamEffectId",
        "hideIcon",
        "isOncePlayEffect",
    }:
        raise AggressiveUp9CatalogError(f"{card_id}#{upgrade}: slot {index} shape changed")
    if raw["produceExamTriggerId"] != "":
        raise AggressiveUp9CatalogError(
            f"{card_id}#{upgrade}: slot {index} has an unexpected trigger"
        )
    if raw["produceExamEffectId"] != expected_effect_id:
        raise AggressiveUp9CatalogError(
            f"{card_id}#{upgrade}: slot {index} effect shape changed"
        )
    if type(raw["hideIcon"]) is not bool or raw["hideIcon"] is not False:
        raise AggressiveUp9CatalogError(f"{card_id}#{upgrade}: slot {index} hideIcon changed")
    if type(raw["isOncePlayEffect"]) is not bool or raw["isOncePlayEffect"] is not False:
        raise AggressiveUp9CatalogError(
            f"{card_id}#{upgrade}: slot {index} once-play shape changed"
        )
    return CardEffectSlot(
        card_id=card_id,
        upgrade_count=upgrade,
        slot_index=index,
        effect_id=expected_effect_id,
        trigger_id="",
        is_once_play_effect=False,
        effect_group_ids=(),
    )


def load_plan2_card_trigger_aggressive_up9_catalog(
    database: Path | str = DEFAULT_DATABASE,
    coverage_artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
) -> Plan2CardTriggerAggressiveUp9Catalog:
    """Load only the fixed five Plan2 card versions from read-only Master."""

    coverage = load_card_trigger_coverage_readback(coverage_artifact)
    path = Path(database)
    if not path.is_file():
        raise AggressiveUp9CatalogError(f"Master database not found: {path}")
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise AggressiveUp9CatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        if trigger_row is None:
            raise AggressiveUp9CatalogError(f"missing target trigger: {TARGET_TRIGGER_ID}")
        trigger = _trigger_from_db_row(trigger_row)
        resolution = resolve_exact_aggressive_up9_trigger(trigger)
        if not resolution.supported:
            raise AggressiveUp9CatalogError(
                f"target trigger is outside exact shape: {resolution.reasons}"
            )

        card_rows = connection.execute(
            """
            SELECT id, upgrade_count, name, plan_type, category, stamina,
                   cost_type, cost_value, play_trigger_id, move_position_type,
                   play_effects_json
              FROM card
             WHERE (id = ? AND upgrade_count BETWEEN 0 AND 3)
                OR (id = ? AND upgrade_count = 0)
             ORDER BY id, upgrade_count
            """,
            (CARD_IDO_140, CARD_MEN_100_011),
        ).fetchall()
        actual_refs = tuple(
            (str(row["id"]), _strict_int(row["upgrade_count"], "card.upgradeCount", signed=False))
            for row in card_rows
        )
        expected_query_refs = (
            (CARD_IDO_140, 0),
            (CARD_IDO_140, 1),
            (CARD_IDO_140, 2),
            (CARD_IDO_140, 3),
            (CARD_MEN_100_011, 0),
        )
        if actual_refs != expected_query_refs:
            raise AggressiveUp9CatalogError("fixed target card refs are missing or changed")

        parsed: dict[tuple[str, int], Plan2CardTriggerAggressiveUp9CardVersion] = {}
        for row in card_rows:
            card_id = str(row["id"])
            upgrade = _strict_int(row["upgrade_count"], f"{card_id}.upgradeCount", signed=False)
            ref = (card_id, upgrade)
            expected_name = TARGET_NAMES_BY_REF.get(ref)
            expected_effect_ids = TARGET_EFFECT_IDS_BY_REF.get(ref)
            if expected_name is None or expected_effect_ids is None:
                raise AggressiveUp9CatalogError(f"card ref is outside fixed target slice: {ref!r}")
            if type(row["name"]) is not str or row["name"] != expected_name:
                raise AggressiveUp9CatalogError(f"{ref!r}: name shape changed")
            if type(row["plan_type"]) is not str or row["plan_type"] not in SUPPORTED_PLAN_TYPES:
                raise AggressiveUp9CatalogError(f"{ref!r}: plan type changed")
            if type(row["category"]) is not str or row["category"] != MENTAL_SKILL_CATEGORY:
                raise AggressiveUp9CatalogError(f"{ref!r}: category shape changed")
            if _strict_int(row["stamina"], f"{ref!r}.stamina", signed=False) != TARGET_STAMINA_BY_REF[ref]:
                raise AggressiveUp9CatalogError(f"{ref!r}: stamina shape changed")
            if type(row["cost_type"]) is not str or row["cost_type"] != COST_TYPE_UNKNOWN:
                raise AggressiveUp9CatalogError(f"{ref!r}: cost type shape changed")
            if _strict_int(row["cost_value"], f"{ref!r}.costValue", signed=False) != 0:
                raise AggressiveUp9CatalogError(f"{ref!r}: cost value shape changed")
            if type(row["play_trigger_id"]) is not str or row["play_trigger_id"] != TARGET_TRIGGER_ID:
                raise AggressiveUp9CatalogError(f"{ref!r}: card admission trigger changed")
            if type(row["move_position_type"]) is not str or row["move_position_type"] != CARD_MOVE_LOST:
                raise AggressiveUp9CatalogError(f"{ref!r}: move position shape changed")
            raw_effects = _json_array(row["play_effects_json"], f"{ref!r}.playEffects")
            if len(raw_effects) != len(expected_effect_ids):
                raise AggressiveUp9CatalogError(f"{ref!r}: slot count changed")
            slots = tuple(
                _strict_card_slot(card_id, upgrade, index, raw, expected_effect_id)
                for index, (raw, expected_effect_id) in enumerate(
                    zip(raw_effects, expected_effect_ids)
                )
            )
            parsed[ref] = Plan2CardTriggerAggressiveUp9CardVersion(
                card_id=card_id,
                upgrade_count=upgrade,
                name=expected_name,
                plan_type=str(row["plan_type"]),
                category=str(row["category"]),
                stamina=int(row["stamina"]),
                cost_type=str(row["cost_type"]),
                cost_value=int(row["cost_value"]),
                move_position_type=str(row["move_position_type"]),
                play_trigger_id=str(row["play_trigger_id"]),
                effect_slots=slots,
            )

        effect_ids = sorted(
            {effect_id for card in parsed.values() for effect_id in card.effect_ids}
        )
        placeholders = ",".join("?" for _ in effect_ids)
        existing_effect_ids = {
            str(row["id"])
            for row in connection.execute(
                f"SELECT id FROM effect WHERE id IN ({placeholders})", tuple(effect_ids)
            ).fetchall()
        }
        missing_effects = sorted(set(effect_ids) - existing_effect_ids)
        if missing_effects:
            raise AggressiveUp9CatalogError(f"target card effects are missing: {missing_effects}")

    card_level = tuple(parsed[ref] for ref in TARGET_CARD_LEVEL_REFS)
    if coverage.card_gap_refs != TARGET_CARD_LEVEL_REFS:
        raise AggressiveUp9CatalogError("coverage card refs do not match Master refs")
    if coverage.affected_count != 5 or coverage.direct_count != 0 or coverage.co_blocked_count != 5:
        raise AggressiveUp9CatalogError("coverage is not the required affected/direct/co 5/0/5")
    return Plan2CardTriggerAggressiveUp9Catalog(
        database=str(path),
        trigger=trigger,
        card_level_versions=card_level,
        affected_versions=card_level,
        direct_versions=(),
        co_blocked_versions=card_level,
        coverage=coverage,
    )


load_catalog = load_plan2_card_trigger_aggressive_up9_catalog
load_coverage_readback = load_card_trigger_coverage_readback


ANDROID_V323_PC_METADATA_EVIDENCE: Final = {
    "android_v3_2_3": {
        "field_predicate": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
            "symbol": "ExamExtensions.IsEffectTriggerFieldValid",
            "rva": "0x68072F4",
            "getter": "ExamStatusEffectCollection.GetAggressive(true)",
            "getter_rva": "0x7E99350",
            "enum_value": 42,
            "fact": "reads signed current Aggressive and compares inclusively with the Master value",
        },
        "card_admission": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence.txt",
            "symbol": "ExamSequence.ExecuteCardCommandImpl",
            "calls": [
                "SetPlayingCard",
                "get_PlayEffectTriggerList",
                "IsEffectTriggerFieldValid",
                "CreatePlayEffectCommand(effect)",
            ],
            "fact": "the card-owned predicate is captured while Playing, before payment and direct effects",
        },
        "metadata": {
            "dump": "_research/android/game-v3.2.3/il2cppdumper/dump.cs",
            "decrypted_global_metadata": "_research/android/game-v3.2.3/native-analysis/global-metadata.decrypted.dat",
            "executor_mapping": "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json",
        },
    },
    "pc_named_queries": {
        "trigger_source": "_research/gakumasu-diff/ProduceExamTrigger.yaml:e_trigger-none-card_play_aggressive_up-9",
        "card_source": "_research/gakumasu-diff/ProduceCard.yaml:p_card-02-ido-3_140,p_card-02-men-100_011",
        "structural_source": "var/coverage/android_pc_exam_metadata_compatibility.json",
        "fact": "PC metadata names the same trigger/card linkage; Android v3.2.3 owns timing and predicate semantics",
    },
}


def build_plan2_card_trigger_aggressive_up9_audit(
    database: Path | str = DEFAULT_DATABASE,
    coverage_artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
    *,
    focused_test_count: int | None = None,
) -> dict[str, object]:
    """Build the leaf audit payload without hashes, clocks, or central writes."""

    catalog = load_plan2_card_trigger_aggressive_up9_catalog(database, coverage_artifact)
    return {
        "schema_version": 1,
        "scope": "Plan2 exact standalone card-level playTriggerId gate: e_trigger-none-card_play_aggressive_up-9",
        "validation": {
            "internet": "not_used",
            "hash_verification": "not_performed",
            "security_verification": "not_performed",
            "clock_verification": "not_performed",
            "master_access": "read_only",
            "central_coverage": "read_only_reference; not rebuilt or modified",
        },
        "target": {
            "trigger_id": TARGET_TRIGGER_ID,
            "threshold": TARGET_THRESHOLD,
            "card_level_play_gate": {
                "version_count": len(catalog.card_level_versions),
                "version_refs": catalog.summary()["cardLevelRefs"],
                "classification": "C",
                "executable": False,
            },
            "affected_direct_co_blocked": {
                "affected": catalog.affected_count,
                "direct": catalog.direct_count,
                "co_blocked": catalog.co_blocked_count,
            },
        },
        "master_snapshot": {
            "database": str(database),
            "trigger": catalog.trigger.to_dict(),
            "card_level_versions": [card.to_dict() for card in catalog.card_level_versions],
        },
        "coverage_readback": catalog.coverage.to_dict(),
        "native_sources": ANDROID_V323_PC_METADATA_EVIDENCE,
        "exact_semantics": {
            "phase": "ProduceExamPhaseType_None is literal; this card gate is evaluated at Playing/card admission",
            "card_admission": "pre-payment and before any direct PlayEffect executes",
            "field_status": "signed current Aggressive >= 9; equality at 9 fires",
            "snapshot": "use the Playing/pre-payment snapshot; do not reread post-payment or same-card prior-effect state",
            "ordinary_forced_extra": "normal (ordinary), forced, and extra share the same admission predicate",
            "counters_and_delta": "global/per-turn play counts, playable-count flag, and observed Aggressive delta are inert",
            "shape": "fail closed outside exact None/no-Not/no-search/[9]/unknown-move/unknown-lesson and the two fixed card families",
            "co_blockers": {
                "p_card-02-ido-3_140": "C:effect:ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive",
                "p_card-02-men-100_011#0": "C:status-trigger:e_trigger-exam_stamina_reduce_card",
            },
        },
        "scope_guard": {
            "new_runtime": "src/gkms_tool/plan2_card_trigger_aggressive_up9.py",
            "new_tests": ["tests/test_plan2_card_trigger_aggressive_up9.py"],
            "central_coverage_read_only": "var/coverage/plan2_card_executable_coverage.json",
            "central_coverage_rebuilt": False,
            "central_coverage_modified": False,
            "forbidden_layers_modified": [],
            "plan2_core_runtime_modified": False,
            "plan3_modified": False,
            "gui_or_outer_modified": False,
        },
        "focused_verification": {
            "command": ".venv\\Scripts\\python.exe -m pytest -q tests/test_plan2_card_trigger_aggressive_up9.py",
            "result": f"{focused_test_count} passed" if focused_test_count is not None else "not_recorded",
            "scope": "new file only; no full-suite or broad pytest run",
        },
    }


__all__ = [
    "ANDROID_V323_PC_METADATA_EVIDENCE",
    "AggressiveGateEvaluation",
    "AggressiveGateBoundary",
    "AggressiveGateInput",
    "AggressiveUp9CatalogError",
    "AggressiveUp9CardVersion",
    "AggressiveUp9GateEvaluation",
    "AggressiveUp9GateInput",
    "CARD_ORIGINS",
    "CARD_MOVE_LOST",
    "CardAdmissionSimulation",
    "CardEffectSlot",
    "CardTriggerCoverageReadback",
    "CoverageReadback",
    "DEFAULT_COVERAGE_ARTIFACT",
    "EXACT_TARGET_TRIGGER",
    "ExactAggressiveUp9Contract",
    "ExactContractResolution",
    "NATIVE_AUDIT_ARTIFACT",
    "Plan2CardTriggerAggressiveUp9CardVersion",
    "Plan2CardTriggerAggressiveUp9Catalog",
    "TARGET_AFFECTED_REFS",
    "TARGET_CARD_LEVEL_REFS",
    "TARGET_EFFECT_IDS_BY_REF",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "build_plan2_card_trigger_aggressive_up9_audit",
    "evaluate_card_play_aggressive_up9_trigger",
    "evaluate_card_play_gate",
    "evaluate_card_trigger",
    "load_card_trigger_coverage_readback",
    "load_coverage_readback",
    "load_catalog",
    "load_plan2_card_trigger_aggressive_up9_catalog",
    "resolve_exact_aggressive_up9_trigger",
    "simulate_aggressive_card_play",
    "simulate_card_admission",
]
