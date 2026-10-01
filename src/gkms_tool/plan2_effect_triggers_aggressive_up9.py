"""Bounded Plan2 leaf for the two Aggressive-up-9 effect triggers.

This module owns only the four ``p_card-02-ido-3_165`` versions of
``夢と現の境界線``.  It reads the immutable Master catalog in read-only mode,
reuses the already audited typed ``CardPlayAggressiveUp`` predicate, and
models the *effect-slot* call site separately from the card-admission call
site.  It deliberately does not import the Plan2 runtime, the central
coverage builder, native-search tooling, Plan3, or GUI/controller code.

The native distinction is important:

* ``IsPlayable`` evaluates a card-owned ``playTriggerId``;
* ``ExecuteCardCommandImpl`` evaluates each non-null
  ``PlayEffectTriggerList[i]`` while building direct effect candidates;
* both call ``IsEffectTriggerFieldValid`` and therefore the current signed
  ``ExamStatusEffectCollection.GetAggressive(true)`` for this field;
* the effect-slot candidate is made before payment, and the direct card path
  uses the one-argument ``CreatePlayEffectCommand(effect)`` overload, so the
  original slot trigger is not re-run by the command runner.

The exact direct-effect sub-phase is therefore not silently equated with the
card-admission sub-phase.  For this target, the relevant Aggressive scalar is
the same pre-payment build snapshot; unrelated context fields (for example
``RemainCanPlayCardCount``) are not admitted into this leaf's predicate.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import (
    AggressiveTriggerRow,
    CHECK_NOT,
    DIRECT_DISPATCH_PHASE,
    FIELD_CARD_PLAY_AGGRESSIVE_UP,
    LESSON_UNKNOWN,
    MOVE_UNKNOWN,
    PHASE_NONE,
    PLAN2,
    resolve_aggressive_trigger,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NATIVE_AUDIT_ARTIFACT: Final = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_effect_triggers_aggressive_up9_native_audit.json"
)

TARGET_CARD_ID: Final = "p_card-02-ido-3_165"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_CARD_LEVEL_REFS: Final = tuple(
    (TARGET_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES
)
TARGET_TRIGGER_NOT_ID: Final = (
    "e_trigger-none-not-card_play_aggressive_up-9"
)
TARGET_TRIGGER_POSITIVE_ID: Final = "e_trigger-none-card_play_aggressive_up-9"
TARGET_TRIGGER_IDS: Final = (
    TARGET_TRIGGER_NOT_ID,
    TARGET_TRIGGER_POSITIVE_ID,
)
TARGET_THRESHOLD: Final = 9

CARD_NAME_BY_UPGRADE: Final = {
    0: "夢と現の境界線",
    1: "夢と現の境界線+",
    2: "夢と現の境界線++",
    3: "夢と現の境界線+++",
}
LESSON_EFFECT_BY_UPGRADE: Final = {
    0: "e_effect-exam_lesson_depend_exam_review-1400-01",
    1: "e_effect-exam_lesson_depend_exam_review-2500-01",
    2: "e_effect-exam_lesson_depend_exam_review-3000-01",
    3: "e_effect-exam_lesson_depend_exam_review-3500-01",
}
LESSON_VALUE_BY_UPGRADE: Final = {0: 1400, 1: 2500, 2: 3000, 3: 3500}
PLAYABLE_VALUE_ADD_EFFECT_ID: Final = "e_effect-exam_playable_value_add-01"
REVIEW_VALUE_MULTIPLE_EFFECT_ID: Final = (
    "e_effect-exam_review_value_multiple-0100"
)
KNOWN_EXECUTABLE_EFFECT_TYPES: Final = frozenset(
    {
        "ProduceExamEffectType_ExamPlayableValueAdd",
        "ProduceExamEffectType_ExamReviewValueMultiple",
        "ProduceExamEffectType_ExamLessonDependExamReview",
    }
)

CATEGORY_ACTIVE_SKILL: Final = "ProduceCardCategory_ActiveSkill"
COST_TYPE_UNKNOWN: Final = "ExamCostType_Unknown"
MOVE_GRAVE: Final = "ProduceCardMovePositionType_Grave"

CARD_ORIGINS: Final = ("normal", "forced", "extra")
DIRECT_EFFECT_BUILD_BOUNDARY: Final = "pre-payment-direct-effect-build"
CARD_ADMISSION_BOUNDARY: Final = "pre-payment-card-admission"


class Plan2EffectTriggerAggressiveUp9ContractError(ValueError):
    """A Master row or runtime input is outside this exact standalone leaf."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}:{detail}"
        super().__init__(message)


class EffectTriggerBoundary(str, Enum):
    """Only the native direct-effect candidate-build boundary is supported."""

    PRE_PAYMENT_DIRECT_EFFECT_BUILD = DIRECT_EFFECT_BUILD_BOUNDARY


@dataclass(frozen=True, slots=True)
class _ExpectedEffect:
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]


_EXPECTED_EFFECTS: Final[dict[str, _ExpectedEffect]] = {
    PLAYABLE_VALUE_ADD_EFFECT_ID: _ExpectedEffect(
        "ProduceExamEffectType_ExamPlayableValueAdd",
        0,
        0,
        1,
        0,
        ("effect_group-visible-exam_playable_value_add-000",),
    ),
    REVIEW_VALUE_MULTIPLE_EFFECT_ID: _ExpectedEffect(
        "ProduceExamEffectType_ExamReviewValueMultiple",
        100,
        0,
        0,
        0,
        ("effect_group-visible-exam_review-000",),
    ),
    LESSON_EFFECT_BY_UPGRADE[0]: _ExpectedEffect(
        "ProduceExamEffectType_ExamLessonDependExamReview",
        LESSON_VALUE_BY_UPGRADE[0],
        0,
        1,
        0,
        (
            "effect_group-visible-exam_lesson_depend_exam_review-000",
            "effect_group-visible-exam_lesson-000",
        ),
    ),
    LESSON_EFFECT_BY_UPGRADE[1]: _ExpectedEffect(
        "ProduceExamEffectType_ExamLessonDependExamReview",
        LESSON_VALUE_BY_UPGRADE[1],
        0,
        1,
        0,
        (
            "effect_group-visible-exam_lesson_depend_exam_review-000",
            "effect_group-visible-exam_lesson-000",
        ),
    ),
    LESSON_EFFECT_BY_UPGRADE[2]: _ExpectedEffect(
        "ProduceExamEffectType_ExamLessonDependExamReview",
        LESSON_VALUE_BY_UPGRADE[2],
        0,
        1,
        0,
        (
            "effect_group-visible-exam_lesson_depend_exam_review-000",
            "effect_group-visible-exam_lesson-000",
        ),
    ),
    LESSON_EFFECT_BY_UPGRADE[3]: _ExpectedEffect(
        "ProduceExamEffectType_ExamLessonDependExamReview",
        LESSON_VALUE_BY_UPGRADE[3],
        0,
        1,
        0,
        (
            "effect_group-visible-exam_lesson_depend_exam_review-000",
            "effect_group-visible-exam_lesson-000",
        ),
    ),
}

_RAW_TRIGGER_KEYS: Final = frozenset(
    {
        "id",
        "phaseTypes",
        "phaseValues",
        "fieldStatusCheckTypes",
        "fieldStatusTypes",
        "fieldStatusValues",
        "fieldStatusProduceCardSearchIds",
        "produceCardSearchId",
        "upperSearchCount",
        "lowerSearchCount",
        "cardMovePositionType",
        "effectTypes",
        "lessonType",
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    }
)
_RAW_CARD_KEYS: Final = frozenset(
    {
        "assetId",
        "category",
        "costType",
        "costValue",
        "effectGroupIds",
        "evaluation",
        "forceStamina",
        "id",
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
        "moveEffectTriggerType",
        "moveProduceExamEffectIds",
        "moveProduceExamTriggerIds",
        "name",
        "noDeckDuplication",
        "order",
        "originCharacterId",
        "originIdolCardId",
        "originPrimaStellaIdolCardId",
        "originSupportCardId",
        "planType",
        "playEffects",
        "playMovePositionType",
        "playProduceExamTriggerId",
        "produceCardCustomizeIds",
        "produceCardStatusEnchantId",
        "produceDescriptions",
        "rarity",
        "rentalUnlockProducerLevel",
        "searchTag",
        "stamina",
        "unlockProducerLevel",
        "upgradeCount",
        "viewStartTime",
        "voiceAssetId",
    }
)
_RAW_EFFECT_KEYS: Final = frozenset(
    {
        "chainProduceExamEffectId",
        "chainProduceExamEffectIds",
        "customizeProduceDescriptions",
        "effectCount",
        "effectGroupIds",
        "effectTurn",
        "effectType",
        "effectValue1",
        "effectValue2",
        "id",
        "movePositionType",
        "pickCountMax",
        "pickCountMax2",
        "pickCountMin",
        "pickCountMin2",
        "pickCountReferenceProduceCardSearchId",
        "pickCountReferenceProduceCardSearchId2",
        "pickCountType",
        "pickCountType2",
        "pickRangeType",
        "pickRangeType2",
        "produceCardGrowEffectIds",
        "produceCardSearchId",
        "produceCardSearchId2",
        "produceCardStatusEnchantId",
        "produceDescriptions",
        "produceExamStatusEnchantId",
        "targetExamEffectType",
        "targetProduceCardId",
        "targetUpgradeCount",
    }
)


def _plain_int(value: object, label: str, *, signed: bool = True) -> int:
    if type(value) is not int:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-integer", label
        )
    if signed and not INT32_MIN <= value <= INT32_MAX:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "integer-out-of-range", label
        )
    if not signed and value < 0:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "negative-integer", label
        )
    return value


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise Plan2EffectTriggerAggressiveUp9ContractError("invalid-text", label)
    return value


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-json-object", label
        ) from error
    if not isinstance(parsed, dict):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-json-object", label
        )
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-json-array", label
        ) from error
    if not isinstance(parsed, list):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-json-array", label
        )
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not str for item in parsed):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-string-array", label
        )
    return tuple(parsed)  # type: ignore[arg-type]


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not int for item in parsed):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-integer-array", label
        )
    return tuple(parsed)  # type: ignore[arg-type]


def _row_value(row: Mapping[str, object] | sqlite3.Row, *names: str) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
    else:
        available = set(row.keys())
        for name in names:
            if name in available:
                return row[name]
    raise Plan2EffectTriggerAggressiveUp9ContractError(
        "missing-master-field", names[0]
    )


def _validate_exact_keys(
    raw: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if frozenset(raw) != expected:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "raw-shape", label
        )


def _expected_trigger_row(trigger_id: str) -> AggressiveTriggerRow:
    check_types = (CHECK_NOT,) if trigger_id == TARGET_TRIGGER_NOT_ID else ()
    return AggressiveTriggerRow(
        id=trigger_id,
        phase_types=(PHASE_NONE,),
        phase_values=(),
        field_status_check_types=check_types,
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


EXPECTED_TRIGGER_ROWS: Final = {
    trigger_id: _expected_trigger_row(trigger_id)
    for trigger_id in TARGET_TRIGGER_IDS
}


def _validate_raw_trigger(
    raw: Mapping[str, object], expected: AggressiveTriggerRow
) -> None:
    _validate_exact_keys(raw, _RAW_TRIGGER_KEYS, expected.id)
    exact = expected.to_dict()
    for key, wanted in exact.items():
        actual = raw.get(key)
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "trigger-raw-value", f"{expected.id}:{key}"
            )
    for key in (
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    ):
        value = raw[key]
        if type(value) is not list or any(type(item) is not dict for item in value):
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "trigger-description-shape", f"{expected.id}:{key}"
            )
        if len(value) < 2:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "trigger-description-shape", f"{expected.id}:{key}:count"
            )
    suffix = "8以下" if expected.id == TARGET_TRIGGER_NOT_ID else "9以上"
    for key in ("produceDescriptions", "playProduceDescriptions"):
        second = raw[key][1]
        text = second.get("text")
        if text not in (f"が<nobr>{suffix}</nobr>の場合、", f"が<nobr>{suffix}</nobr>の場合、使用可"):
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "trigger-boundary-description", f"{expected.id}:{key}"
            )


def _parse_trigger_row(row: sqlite3.Row) -> AggressiveTriggerRow:
    trigger_id = _required_text(row["id"], "trigger.id")
    if trigger_id not in EXPECTED_TRIGGER_ROWS:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "unknown-trigger", trigger_id
        )
    parsed = AggressiveTriggerRow(
        id=trigger_id,
        phase_types=_string_tuple(row["phase_types_json"], f"{trigger_id}:phaseTypes"),
        phase_values=_int_tuple(row["phase_values_json"], f"{trigger_id}:phaseValues"),
        field_status_check_types=_string_tuple(
            row["field_status_check_types_json"],
            f"{trigger_id}:fieldStatusCheckTypes",
        ),
        field_status_types=_string_tuple(
            row["field_status_types_json"], f"{trigger_id}:fieldStatusTypes"
        ),
        field_status_values=_int_tuple(
            row["field_status_values_json"], f"{trigger_id}:fieldStatusValues"
        ),
        field_status_card_search_ids=_string_tuple(
            row["field_status_produce_card_search_ids_json"],
            f"{trigger_id}:fieldStatusProduceCardSearchIds",
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_plain_int(
            row["upper_search_count"], f"{trigger_id}:upperSearchCount", signed=False
        ),
        lower_search_count=_plain_int(
            row["lower_search_count"], f"{trigger_id}:lowerSearchCount", signed=False
        ),
        card_move_position_type=_required_text(
            row["card_move_position_type"], f"{trigger_id}:cardMovePositionType"
        ),
        effect_types=_string_tuple(row["effect_types_json"], f"{trigger_id}:effectTypes"),
        lesson_type=_required_text(row["lesson_type"], f"{trigger_id}:lessonType"),
    )
    expected = EXPECTED_TRIGGER_ROWS[trigger_id]
    if parsed != expected:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "trigger-shape", trigger_id
        )
    raw = _json_object(row["raw_json"], f"{trigger_id}:raw_json")
    _validate_raw_trigger(raw, expected)
    return parsed


@dataclass(frozen=True, slots=True)
class EffectTriggerContract:
    """The exact predicate for one of the two target effect triggers."""

    row: AggressiveTriggerRow
    threshold: int = TARGET_THRESHOLD
    comparison: str = "signed_greater_equal"
    value_source: str = (
        "signed current ExamStatusEffectCollection.GetAggressive(true)"
    )
    counter_scope: str = "none"
    counter_source: str = "global/per-turn card-play counts are not read"
    status_delta_source: str = "status delta is not read"
    trigger_count_source: str = "not a TriggerEffectStatusEffect listener"
    evaluation_boundary: str = DIRECT_EFFECT_BUILD_BOUNDARY
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    listener_kind: str = "card-owned direct effect-slot trigger"
    same_card_reread: bool = False

    @property
    def check_not(self) -> bool:
        return self.row.field_status_check_types == (CHECK_NOT,)

    @property
    def result_expression(self) -> str:
        base = f"signed_aggressive_value >= {self.threshold}"
        return f"not ({base})" if self.check_not else base

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "phase": self.row.phase_types[0],
            "threshold": self.threshold,
            "checkNot": self.check_not,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "valueSource": self.value_source,
            "counterScope": self.counter_scope,
            "counterSource": self.counter_source,
            "statusDeltaSource": self.status_delta_source,
            "triggerCountSource": self.trigger_count_source,
            "evaluationBoundary": self.evaluation_boundary,
            "dispatchPhase": self.dispatch_phase,
            "listenerKind": self.listener_kind,
            "sameCardReread": self.same_card_reread,
        }


@dataclass(frozen=True, slots=True)
class EffectTriggerResolution:
    supported: bool
    contract: EffectTriggerContract | None
    reasons: tuple[str, ...] = ()


def resolve_aggressive_effect_trigger(
    row: AggressiveTriggerRow,
) -> EffectTriggerResolution:
    """Resolve only the two exact target rows; all altered shapes pause."""

    expected = EXPECTED_TRIGGER_ROWS.get(row.id)
    if expected is None:
        return EffectTriggerResolution(False, None, ("unknown-target-trigger",))

    # This is the shared, previously audited native predicate resolver.  The
    # effect leaf adds an exact target-row check and its own call-site boundary
    # instead of assuming that a card-admission evaluator is interchangeable.
    shared = resolve_aggressive_trigger(row)
    reasons = list(shared.reasons)
    if row != expected:
        reasons.append("trigger-row-is-not-exact-target-shape")
    if reasons:
        return EffectTriggerResolution(False, None, tuple(dict.fromkeys(reasons)))
    return EffectTriggerResolution(
        True,
        EffectTriggerContract(row=row, threshold=TARGET_THRESHOLD),
    )


@dataclass(frozen=True, slots=True)
class EffectTriggerEvaluationInput:
    """Inputs made explicit to demonstrate which native fields are inert."""

    aggressive_status_value: int
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    observed_aggressive_status_delta: int = 0
    phase: str = PHASE_NONE
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: EffectTriggerBoundary = EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD
    card_origin: str = "normal"
    is_use_playable_count: bool = True
    later_same_card_aggressive_status_value: int | None = None

    def __post_init__(self) -> None:
        _plain_int(self.aggressive_status_value, "aggressive_status_value")
        _plain_int(self.global_card_play_count, "global_card_play_count", signed=False)
        _plain_int(self.turn_card_play_count, "turn_card_play_count", signed=False)
        _plain_int(
            self.observed_aggressive_status_delta,
            "observed_aggressive_status_delta",
        )
        if self.later_same_card_aggressive_status_value is not None:
            _plain_int(
                self.later_same_card_aggressive_status_value,
                "later_same_card_aggressive_status_value",
            )
        if type(self.is_use_playable_count) is not bool:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "invalid-boolean", "is_use_playable_count"
            )
        if self.card_origin not in CARD_ORIGINS:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "unsupported-card-origin", str(self.card_origin)
            )
        if type(self.phase) is not str or not self.phase:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "invalid-phase", "phase"
            )
        if type(self.dispatch_phase) is not str or not self.dispatch_phase:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "invalid-dispatch-phase", "dispatch_phase"
            )
        if not isinstance(self.boundary, EffectTriggerBoundary):
            try:
                object.__setattr__(
                    self, "boundary", EffectTriggerBoundary(self.boundary)
                )
            except ValueError as error:
                raise Plan2EffectTriggerAggressiveUp9ContractError(
                    "invalid-boundary", str(self.boundary)
                ) from error


AggressiveEffectTriggerInput = EffectTriggerEvaluationInput


@dataclass(frozen=True, slots=True)
class EffectTriggerEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    aggressive_status_value: int | None
    threshold: int | None
    check_not: bool | None
    phase: str
    dispatch_phase: str
    boundary: EffectTriggerBoundary
    card_origin: str
    read_fields: tuple[str, ...] = ("current_aggressive_status_value",)
    ignored_fields: tuple[str, ...] = (
        "global_card_play_count",
        "turn_card_play_count",
        "observed_aggressive_status_delta",
        "is_use_playable_count",
        "later_same_card_aggressive_status_value",
    )
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "aggressiveStatusValue": self.aggressive_status_value,
            "threshold": self.threshold,
            "checkNot": self.check_not,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "cardOrigin": self.card_origin,
            "readFields": list(self.read_fields),
            "ignoredFields": list(self.ignored_fields),
            "reasons": list(self.reasons),
        }


def evaluate_aggressive_effect_trigger(
    inputs: EffectTriggerEvaluationInput,
    trigger: AggressiveTriggerRow,
) -> EffectTriggerEvaluation:
    """Evaluate one target effect trigger without reading or mutating state."""

    resolution = resolve_aggressive_effect_trigger(trigger)
    if not resolution.supported:
        return EffectTriggerEvaluation(
            trigger_id=trigger.id,
            supported=False,
            fires=None,
            aggressive_status_value=None,
            threshold=None,
            check_not=None,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            card_origin=inputs.card_origin,
            reasons=resolution.reasons,
        )
    contract = resolution.contract
    assert contract is not None
    reasons: list[str] = []
    if inputs.phase != PHASE_NONE:
        reasons.append("runtime-phase-is-not-none")
    if inputs.dispatch_phase != DIRECT_DISPATCH_PHASE:
        reasons.append("dispatch-phase-is-not-play-effect")
    if inputs.boundary is not EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD:
        reasons.append("runtime-boundary-is-not-direct-effect-build")
    if reasons:
        return EffectTriggerEvaluation(
            trigger_id=trigger.id,
            supported=False,
            fires=None,
            aggressive_status_value=None,
            threshold=contract.threshold,
            check_not=contract.check_not,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            card_origin=inputs.card_origin,
            reasons=tuple(reasons),
        )
    base_match = inputs.aggressive_status_value >= contract.threshold
    fires = not base_match if contract.check_not else base_match
    return EffectTriggerEvaluation(
        trigger_id=trigger.id,
        supported=True,
        fires=fires,
        aggressive_status_value=inputs.aggressive_status_value,
        threshold=contract.threshold,
        check_not=contract.check_not,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        card_origin=inputs.card_origin,
    )


@dataclass(frozen=True, slots=True)
class MasterEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]
    status_enchant_id: str = ""
    chain_effect_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "effectId": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "effectGroupIds": list(self.effect_group_ids),
            "statusEnchantId": self.status_enchant_id,
            "chainEffectId": self.chain_effect_id,
        }


@dataclass(frozen=True, slots=True)
class EffectTriggerCardSlot:
    card_id: str
    upgrade: int
    slot_index: int
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot_index,
            "effectId": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
            "effectGroupIds": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True)
class TargetCardVersion:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    effect_slots: tuple[EffectTriggerCardSlot, ...]

    @property
    def ref(self) -> tuple[str, int]:
        return (self.card_id, self.upgrade)

    @property
    def effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def trigger_ids(self) -> tuple[str, ...]:
        return tuple(slot.trigger_id for slot in self.effect_slots)

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "movePositionType": self.move_position_type,
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
        }


def _expected_slot_payload(upgrade: int) -> tuple[dict[str, object], ...]:
    return (
        {
            "produceExamTriggerId": "",
            "produceExamEffectId": PLAYABLE_VALUE_ADD_EFFECT_ID,
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
        {
            "produceExamTriggerId": TARGET_TRIGGER_NOT_ID,
            "produceExamEffectId": REVIEW_VALUE_MULTIPLE_EFFECT_ID,
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
        {
            "produceExamTriggerId": TARGET_TRIGGER_POSITIVE_ID,
            "produceExamEffectId": LESSON_EFFECT_BY_UPGRADE[upgrade],
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
    )


def _validate_raw_effect(
    raw: Mapping[str, object], effect_id: str, expected: _ExpectedEffect
) -> None:
    _validate_exact_keys(raw, _RAW_EFFECT_KEYS, effect_id)
    exact: dict[str, object] = {
        "id": effect_id,
        "effectType": expected.effect_type,
        "effectValue1": expected.value1,
        "effectValue2": expected.value2,
        "effectCount": expected.count,
        "effectTurn": expected.turn,
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
        "effectGroupIds": list(expected.effect_group_ids),
    }
    for key, wanted in exact.items():
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "effect-raw-value", f"{effect_id}:{key}"
            )
    for key in ("produceDescriptions", "customizeProduceDescriptions"):
        value = raw[key]
        if type(value) is not list or any(type(item) is not dict for item in value):
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "effect-description-shape", f"{effect_id}:{key}"
            )


def _parse_effect_row(
    row: sqlite3.Row, expected_id: str
) -> MasterEffect:
    effect_id = _required_text(row["id"], "effect.id")
    if effect_id != expected_id or effect_id not in _EXPECTED_EFFECTS:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "effect-id-mismatch", f"{effect_id}:{expected_id}"
        )
    expected = _EXPECTED_EFFECTS[effect_id]
    values = (
        _required_text(row["effect_type"], f"{effect_id}:effect_type"),
        _plain_int(row["value1"], f"{effect_id}:value1"),
        _plain_int(row["value2"], f"{effect_id}:value2"),
        _plain_int(row["effect_count"], f"{effect_id}:effect_count"),
        _plain_int(row["effect_turn"], f"{effect_id}:effect_turn"),
    )
    if values != (
        expected.effect_type,
        expected.value1,
        expected.value2,
        expected.count,
        expected.turn,
    ):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "effect-value-shape", effect_id
        )
    status = row["status_enchant_id"]
    chain = row["chain_effect_id"]
    if status != "" or chain != "":
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "effect-links", effect_id
        )
    raw = _json_object(row["raw_json"], f"{effect_id}:raw_json")
    _validate_raw_effect(raw, effect_id, expected)
    return MasterEffect(
        effect_id=effect_id,
        effect_type=values[0],
        value1=values[1],
        value2=values[2],
        count=values[3],
        turn=values[4],
        effect_group_ids=expected.effect_group_ids,
    )


def _validate_raw_card(
    raw: Mapping[str, object],
    *,
    card_id: str,
    upgrade: int,
    name: str,
    expected_payload: Sequence[Mapping[str, object]],
) -> None:
    _validate_exact_keys(raw, _RAW_CARD_KEYS, f"{card_id}:{upgrade}")
    exact: dict[str, object] = {
        "id": card_id,
        "upgradeCount": upgrade,
        "name": name,
        "planType": PLAN2,
        "category": CATEGORY_ACTIVE_SKILL,
        "stamina": 4,
        "costType": COST_TYPE_UNKNOWN,
        "costValue": 0,
        "playProduceExamTriggerId": "",
        "playEffects": [dict(item) for item in expected_payload],
        "playMovePositionType": MOVE_GRAVE,
    }
    for key, wanted in exact.items():
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "card-raw-value", f"{card_id}:{upgrade}:{key}"
            )
    for key in (
        "produceDescriptions",
        "effectGroupIds",
        "produceCardCustomizeIds",
        "moveProduceExamEffectIds",
        "moveProduceExamTriggerIds",
    ):
        if type(raw[key]) is not list:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "card-raw-array", f"{card_id}:{upgrade}:{key}"
            )


def _parse_slot(
    payload: object,
    *,
    card_id: str,
    upgrade: int,
    slot_index: int,
    expected: Mapping[str, object],
    effects: Mapping[str, MasterEffect],
) -> EffectTriggerCardSlot:
    if not isinstance(payload, Mapping):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-shape", f"{card_id}:{upgrade}:{slot_index}"
        )
    if set(payload) != {
        "produceExamTriggerId",
        "produceExamEffectId",
        "hideIcon",
        "isOncePlayEffect",
    }:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-shape", f"{card_id}:{upgrade}:{slot_index}"
        )
    if type(payload["produceExamTriggerId"]) is not str:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-trigger-type", f"{card_id}:{upgrade}:{slot_index}"
        )
    if type(payload["produceExamEffectId"]) is not str:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-effect-type", f"{card_id}:{upgrade}:{slot_index}"
        )
    if type(payload["hideIcon"]) is not bool or type(payload["isOncePlayEffect"]) is not bool:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-boolean-type", f"{card_id}:{upgrade}:{slot_index}"
        )
    if payload != expected:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-value", f"{card_id}:{upgrade}:{slot_index}"
        )
    effect_id = expected["produceExamEffectId"]
    assert isinstance(effect_id, str)
    effect = effects.get(effect_id)
    if effect is None:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "unknown-slot-effect", f"{card_id}:{upgrade}:{slot_index}:{effect_id}"
        )
    return EffectTriggerCardSlot(
        card_id=card_id,
        upgrade=upgrade,
        slot_index=slot_index,
        effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        count=effect.count,
        turn=effect.turn,
        trigger_id=str(expected["produceExamTriggerId"]),
        hide_icon=bool(expected["hideIcon"]),
        is_once_play_effect=bool(expected["isOncePlayEffect"]),
        effect_group_ids=effect.effect_group_ids,
    )


def validate_target_card_version(card: TargetCardVersion) -> None:
    """Validate an already-normalized target card; malformed shapes raise."""

    ref = card.ref
    if (
        type(card.card_id) is not str
        or type(card.upgrade) is not int
        or isinstance(card.upgrade, bool)
        or ref not in TARGET_CARD_LEVEL_REFS
    ):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "unknown-card", repr(ref)
        )
    upgrade = card.upgrade
    if type(card.effect_slots) is not tuple:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-container", repr(ref)
        )
    if card.name != CARD_NAME_BY_UPGRADE[upgrade]:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-name", repr(ref)
        )
    if (
        card.plan_type != PLAN2
        or card.category != CATEGORY_ACTIVE_SKILL
        or card.stamina != 4
        or card.cost_type != COST_TYPE_UNKNOWN
        or card.cost_value != 0
        or card.play_trigger_id != ""
        or card.move_position_type != MOVE_GRAVE
    ):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-admission-or-cost-or-move-shape", repr(ref)
        )
    expected_payload = _expected_slot_payload(upgrade)
    if len(card.effect_slots) != len(expected_payload):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-count", repr(ref)
        )
    for index, (slot, expected) in enumerate(
        zip(card.effect_slots, expected_payload)
    ):
        expected_effect_id = expected["produceExamEffectId"]
        expected_trigger_id = expected["produceExamTriggerId"]
        if (
            slot.card_id != TARGET_CARD_ID
            or slot.upgrade != upgrade
            or slot.slot_index != index
            or slot.effect_id != expected_effect_id
            or slot.trigger_id != expected_trigger_id
            or slot.hide_icon is not False
            or slot.is_once_play_effect is not False
        ):
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "card-slot-order-or-gate", f"{ref}:{index}"
            )
        effect = _EXPECTED_EFFECTS[slot.effect_id]
        if (
            slot.effect_type != effect.effect_type
            or slot.value1 != effect.value1
            or slot.value2 != effect.value2
            or slot.count != effect.count
            or slot.turn != effect.turn
            or slot.effect_group_ids != effect.effect_group_ids
        ):
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "card-slot-effect-shape", f"{ref}:{index}"
            )


def _parse_card_row(
    row: sqlite3.Row, effects: Mapping[str, MasterEffect]
) -> TargetCardVersion:
    card_id = _required_text(row["id"], "card.id")
    upgrade = _plain_int(row["upgrade_count"], "card.upgrade_count", signed=False)
    if (card_id, upgrade) not in TARGET_CARD_LEVEL_REFS:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "unknown-card", f"{card_id}:{upgrade}"
        )
    name = _required_text(row["name"], f"{card_id}:{upgrade}:name")
    if name != CARD_NAME_BY_UPGRADE[upgrade]:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-name", f"{card_id}:{upgrade}"
        )
    if row["plan_type"] != PLAN2 or row["category"] != CATEGORY_ACTIVE_SKILL:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-plan-or-category", f"{card_id}:{upgrade}"
        )
    stamina = _plain_int(row["stamina"], f"{card_id}:{upgrade}:stamina")
    cost_value = _plain_int(
        row["cost_value"], f"{card_id}:{upgrade}:cost_value", signed=False
    )
    if (
        stamina != 4
        or row["cost_type"] != COST_TYPE_UNKNOWN
        or cost_value != 0
        or row["play_trigger_id"] != ""
        or row["move_position_type"] != MOVE_GRAVE
    ):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-admission-or-cost-or-move-shape", f"{card_id}:{upgrade}"
        )
    payload = _json_array(row["play_effects_json"], f"{card_id}:{upgrade}:playEffects")
    expected_payload = _expected_slot_payload(upgrade)
    if len(payload) != len(expected_payload):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "card-slot-count", f"{card_id}:{upgrade}"
        )
    raw = _json_object(row["raw_json"], f"{card_id}:{upgrade}:raw_json")
    _validate_raw_card(
        raw,
        card_id=card_id,
        upgrade=upgrade,
        name=name,
        expected_payload=expected_payload,
    )
    slots = tuple(
        _parse_slot(
            item,
            card_id=card_id,
            upgrade=upgrade,
            slot_index=index,
            expected=expected,
            effects=effects,
        )
        for index, (item, expected) in enumerate(zip(payload, expected_payload))
    )
    card = TargetCardVersion(
        card_id=card_id,
        upgrade=upgrade,
        name=name,
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina=stamina,
        cost_type=str(row["cost_type"]),
        cost_value=cost_value,
        play_trigger_id=str(row["play_trigger_id"]),
        move_position_type=str(row["move_position_type"]),
        effect_slots=slots,
    )
    validate_target_card_version(card)
    return card


def _read_effect_rows(
    connection: sqlite3.Connection, effect_ids: Sequence[str]
) -> dict[str, sqlite3.Row]:
    if not effect_ids or len(set(effect_ids)) != len(effect_ids):
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-effect-list", repr(tuple(effect_ids))
        )
    placeholders = ",".join("?" for _ in effect_ids)
    rows = connection.execute(
        f"SELECT * FROM effect WHERE id IN ({placeholders})", tuple(effect_ids)
    ).fetchall()
    result = {str(row["id"]): row for row in rows}
    if set(result) != set(effect_ids) or len(rows) != len(effect_ids):
        missing = sorted(set(effect_ids) - set(result))
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "effect-catalog", ",".join(missing)
        )
    return result


@dataclass(frozen=True, slots=True)
class TriggerFamilyAccounting:
    trigger_id: str
    affected_versions: tuple[tuple[str, int], ...]
    direct_versions: tuple[tuple[str, int], ...]
    co_blocked_versions: tuple[tuple[str, int], ...]

    @property
    def affected_count(self) -> int:
        return len(self.affected_versions)

    @property
    def direct_count(self) -> int:
        return len(self.direct_versions)

    @property
    def co_blocked_count(self) -> int:
        return len(self.co_blocked_versions)

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "affected": self.affected_count,
            "direct": self.direct_count,
            "coBlocked": self.co_blocked_count,
            "affectedRefs": [list(ref) for ref in self.affected_versions],
            "directRefs": [list(ref) for ref in self.direct_versions],
            "coBlockedRefs": [list(ref) for ref in self.co_blocked_versions],
        }


@dataclass(frozen=True, slots=True)
class Plan2EffectTriggerAggressiveUp9Catalog:
    database: str
    trigger_rows: tuple[AggressiveTriggerRow, ...]
    card_versions: tuple[TargetCardVersion, ...]
    effect_rows: tuple[MasterEffect, ...]

    def trigger(self, trigger_id: str) -> AggressiveTriggerRow:
        for row in self.trigger_rows:
            if row.id == trigger_id:
                return row
        raise KeyError(trigger_id)

    def card(self, upgrade: int) -> TargetCardVersion:
        for card in self.card_versions:
            if card.upgrade == upgrade:
                return card
        raise KeyError(upgrade)

    @property
    def version_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(card.ref for card in self.card_versions)

    @property
    def executable_effect_types(self) -> tuple[str, ...]:
        return tuple(sorted(KNOWN_EXECUTABLE_EFFECT_TYPES))

    def _unlocked_with(self, card: TargetCardVersion, resolved: frozenset[str]) -> bool:
        validate_target_card_version(card)
        return all(
            slot.trigger_id == "" or slot.trigger_id in resolved
            for slot in card.effect_slots
        ) and all(
            slot.effect_type in KNOWN_EXECUTABLE_EFFECT_TYPES
            for slot in card.effect_slots
        )

    def family_accounting(self, trigger_id: str) -> TriggerFamilyAccounting:
        if trigger_id not in TARGET_TRIGGER_IDS:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "unknown-target-trigger", trigger_id
            )
        affected = tuple(
            card for card in self.card_versions if trigger_id in card.trigger_ids
        )
        resolved = frozenset({trigger_id})
        direct = tuple(
            card for card in affected if self._unlocked_with(card, resolved)
        )
        co_blocked = tuple(card for card in affected if card not in direct)
        return TriggerFamilyAccounting(
            trigger_id=trigger_id,
            affected_versions=tuple(card.ref for card in affected),
            direct_versions=tuple(card.ref for card in direct),
            co_blocked_versions=tuple(card.ref for card in co_blocked),
        )

    def batch_accounting(self) -> TriggerFamilyAccounting:
        resolved = frozenset(TARGET_TRIGGER_IDS)
        direct = tuple(
            card for card in self.card_versions if self._unlocked_with(card, resolved)
        )
        return TriggerFamilyAccounting(
            trigger_id="both-target-effect-triggers",
            affected_versions=self.version_refs,
            direct_versions=tuple(card.ref for card in direct),
            co_blocked_versions=tuple(
                card.ref for card in self.card_versions if card not in direct
            ),
        )


def load_plan2_effect_triggers_aggressive_up9_catalog(
    database: Path = DEFAULT_DATABASE,
) -> Plan2EffectTriggerAggressiveUp9Catalog:
    """Read exactly the two trigger rows, four cards, and six effect rows."""

    path = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            trigger_rows_raw = connection.execute(
                """
                SELECT * FROM produce_exam_trigger
                 WHERE id IN (?, ?)
                 ORDER BY CASE id WHEN ? THEN 0 WHEN ? THEN 1 END
                """,
                (
                    TARGET_TRIGGER_NOT_ID,
                    TARGET_TRIGGER_POSITIVE_ID,
                    TARGET_TRIGGER_NOT_ID,
                    TARGET_TRIGGER_POSITIVE_ID,
                ),
            ).fetchall()
            actual_trigger_ids = tuple(str(row["id"]) for row in trigger_rows_raw)
            if actual_trigger_ids != TARGET_TRIGGER_IDS:
                raise Plan2EffectTriggerAggressiveUp9ContractError(
                    "trigger-catalog", repr(actual_trigger_ids)
                )
            trigger_rows = tuple(_parse_trigger_row(row) for row in trigger_rows_raw)

            effect_ids = tuple(
                dict.fromkeys(
                    [PLAYABLE_VALUE_ADD_EFFECT_ID, REVIEW_VALUE_MULTIPLE_EFFECT_ID]
                    + [LESSON_EFFECT_BY_UPGRADE[upgrade] for upgrade in TARGET_UPGRADES]
                )
            )
            raw_effect_rows = _read_effect_rows(connection, effect_ids)
            effects = {
                effect_id: _parse_effect_row(raw_effect_rows[effect_id], effect_id)
                for effect_id in effect_ids
            }

            rows = connection.execute(
                """
                SELECT * FROM card
                 WHERE id = ? AND upgrade_count BETWEEN 0 AND 3
                 ORDER BY upgrade_count
                """,
                (TARGET_CARD_ID,),
            ).fetchall()
            actual_refs = tuple((str(row["id"]), int(row["upgrade_count"])) for row in rows)
            if actual_refs != TARGET_CARD_LEVEL_REFS:
                raise Plan2EffectTriggerAggressiveUp9ContractError(
                    "card-catalog", repr(actual_refs)
                )
            card_versions = tuple(_parse_card_row(row, effects) for row in rows)
    except sqlite3.Error as error:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "master-read-failed", str(error)
        ) from error
    return Plan2EffectTriggerAggressiveUp9Catalog(
        database=str(path),
        trigger_rows=trigger_rows,
        card_versions=card_versions,
        effect_rows=tuple(effects[effect_id] for effect_id in effect_ids),
    )


load_catalog = load_plan2_effect_triggers_aggressive_up9_catalog


@dataclass(frozen=True, slots=True)
class EffectTriggerBuildProbe:
    card_ref: tuple[str, int]
    snapshot_aggressive_status_value: int
    snapshot_boundary: EffectTriggerBoundary
    snapshot_rule: str
    slot_evaluations: tuple[tuple[int, EffectTriggerEvaluation], ...]
    included_slot_indexes: tuple[int, ...]
    included_effect_ids: tuple[str, ...]
    excluded_slot_indexes: tuple[int, ...]
    later_same_card_aggressive_status_value: int | None
    same_card_trigger_reread: bool = False
    command_factory: str = "ExamPlayCommand.CreatePlayEffectCommand(effect)"

    def to_dict(self) -> dict[str, object]:
        return {
            "cardRef": list(self.card_ref),
            "snapshotAggressiveStatusValue": self.snapshot_aggressive_status_value,
            "snapshotBoundary": self.snapshot_boundary.value,
            "snapshotRule": self.snapshot_rule,
            "slotEvaluations": [
                {"slot": index, "evaluation": evaluation.to_dict()}
                for index, evaluation in self.slot_evaluations
            ],
            "includedSlotIndexes": list(self.included_slot_indexes),
            "includedEffectIds": list(self.included_effect_ids),
            "excludedSlotIndexes": list(self.excluded_slot_indexes),
            "laterSameCardAggressiveStatusValue": self.later_same_card_aggressive_status_value,
            "sameCardTriggerReread": self.same_card_trigger_reread,
            "commandFactory": self.command_factory,
        }


def simulate_effect_trigger_build(
    card: TargetCardVersion,
    inputs: EffectTriggerEvaluationInput,
    triggers: Mapping[str, AggressiveTriggerRow],
) -> EffectTriggerBuildProbe:
    """Build the direct candidate list from one pre-payment Aggressive snapshot."""

    validate_target_card_version(card)
    for trigger_id in TARGET_TRIGGER_IDS:
        if trigger_id not in triggers:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "missing-trigger-row", trigger_id
            )
    snapshot = inputs.aggressive_status_value
    snapshot_inputs = replace(
        inputs,
        aggressive_status_value=snapshot,
        later_same_card_aggressive_status_value=None,
    )
    evaluations: list[tuple[int, EffectTriggerEvaluation]] = []
    included: list[int] = []
    excluded: list[int] = []
    for slot in card.effect_slots:
        if slot.trigger_id == "":
            included.append(slot.slot_index)
            continue
        evaluation = evaluate_aggressive_effect_trigger(
            snapshot_inputs, triggers[slot.trigger_id]
        )
        evaluations.append((slot.slot_index, evaluation))
        if not evaluation.supported:
            raise Plan2EffectTriggerAggressiveUp9ContractError(
                "effect-trigger-fail-closed",
                f"{card.ref}:{slot.slot_index}:{evaluation.reasons}",
            )
        if evaluation.fires:
            included.append(slot.slot_index)
        else:
            excluded.append(slot.slot_index)
    included.sort()
    excluded.sort()
    slots_by_index = {slot.slot_index: slot for slot in card.effect_slots}
    return EffectTriggerBuildProbe(
        card_ref=card.ref,
        snapshot_aggressive_status_value=snapshot,
        snapshot_boundary=EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
        snapshot_rule=(
            "capture current Aggressive during direct-effect candidate build; "
            "later same-card effects do not rewrite the already-built list"
        ),
        slot_evaluations=tuple(evaluations),
        included_slot_indexes=tuple(included),
        included_effect_ids=tuple(
            slots_by_index[index].effect_id for index in included
        ),
        excluded_slot_indexes=tuple(excluded),
        later_same_card_aggressive_status_value=(
            inputs.later_same_card_aggressive_status_value
        ),
    )


ANDROID_NATIVE_EVIDENCE: Final[dict[str, object]] = {
    "version": "Android v3.2.3",
    "fieldEnum": {
        "source": "_research/android/game-v3.2.3/il2cppdumper/dump.cs:697025-697026",
        "symbol": "ProduceExamFieldStatusType.CardPlayAggressiveUp",
        "value": 42,
    },
    "fieldPredicate": {
        "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
        "symbol": "ExamExtensions.IsEffectTriggerFieldValid",
        "rva": "0x68072F4",
        "helperSymbol": "ExamExtensions.IsFieldStatusTriggerStatusEffect",
        "helperRva": "0x68082D4",
        "getterSymbol": "ExamStatusEffectCollection.GetAggressive",
        "getterRva": "0x7E99350",
        "getterParameter": "isUse=true",
        "comparison": "current signed Aggressive >= fieldStatusValue; Not inverts",
    },
    "cardAdmissionCallSite": {
        "symbol": "ExamExtensions.IsPlayable",
        "sourceLines": "ExamExtensions.txt ISIL 027-043",
        "call": "ExamCardData.get_PlayTrigger -> IsEffectTriggerFieldValid(trigger, context)",
        "boundary": CARD_ADMISSION_BOUNDARY,
    },
    "effectSlotCallSite": {
        "symbol": "ExamSequence.ExecuteCardCommandImpl",
        "rva": "0x7ECE628",
        "sourceLines": "ExamSequence.txt ISIL 563-610",
        "call": "ExamCardData.get_PlayEffectTriggerList -> IsEffectTriggerFieldValid(trigger, context)",
        "boundary": DIRECT_EFFECT_BUILD_BOUNDARY,
        "after": "UsePlayCountBuff, PlayProduceExamEffectList and RemainCanPlayCardCount setup",
        "before": "ConsumeCardCost/payment and direct PlayEffect dispatch",
    },
    "commandFactory": {
        "used": "ExamPlayCommand.CreatePlayEffectCommand(effect)",
        "usedRva": "0x7EC35E0",
        "notUsed": "ExamPlayCommand.CreatePlayEffectCommand(effect, trigger)",
        "notUsedRva": "0x7EC392C",
        "effectSlotTriggerReread": False,
    },
    "statusListenerExclusion": {
        "captureSymbol": "ExamStatusEffectCollection.GetCardPlayValidEffectList",
        "captureRva": "0x7EA2318",
        "countSymbol": "TriggerEffectStatusEffect.SpendCount",
        "countRva": "0x7EAE70C",
        "appliesTo": "captured status-listener children, not target direct card effect slots",
        "targetReadsTriggerCount": False,
    },
}

PC_METADATA_EVIDENCE: Final[dict[str, object]] = {
    "source": "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/targeted-metadata-index.json",
    "symbols": {
        "IsEffectTriggerFieldValid": {"parameterCount": 2},
        "CreatePlayEffectCommand": {"overloads": [1, 2]},
        "ExecuteCardCommandImpl": {"parameterCount": 4},
        "GetAggressive": {"parameterCount": 1},
        "GetCardPlayValidEffectList": {"parameterCount": 2},
    },
    "role": "structural cross-check; Android native instruction evidence remains authoritative for this leaf",
}


def build_plan2_effect_triggers_aggressive_up9_audit(
    database: Path = DEFAULT_DATABASE,
    *,
    focused_test_count: int = 0,
) -> dict[str, object]:
    """Return the committed native audit payload without touching central JSON."""

    if type(focused_test_count) is not int or focused_test_count < 0:
        raise Plan2EffectTriggerAggressiveUp9ContractError(
            "invalid-focused-test-count"
        )
    catalog = load_plan2_effect_triggers_aggressive_up9_catalog(database)
    family_accounting = {
        trigger_id: catalog.family_accounting(trigger_id).to_dict()
        for trigger_id in TARGET_TRIGGER_IDS
    }
    batch = catalog.batch_accounting()
    probes = {
        "below9": simulate_effect_trigger_build(
            catalog.card(0),
            EffectTriggerEvaluationInput(
                aggressive_status_value=8,
                later_same_card_aggressive_status_value=12,
            ),
            {row.id: row for row in catalog.trigger_rows},
        ).to_dict(),
        "at9": simulate_effect_trigger_build(
            catalog.card(0),
            EffectTriggerEvaluationInput(
                aggressive_status_value=9,
                later_same_card_aggressive_status_value=0,
            ),
            {row.id: row for row in catalog.trigger_rows},
        ).to_dict(),
    }
    return {
        "schemaVersion": 1,
        "scope": "Plan2 standalone target effect-trigger leaf",
        "target": {
            "cardId": TARGET_CARD_ID,
            "name": "夢と現の境界線",
            "upgrades": list(TARGET_UPGRADES),
            "triggerIds": list(TARGET_TRIGGER_IDS),
            "threshold": TARGET_THRESHOLD,
            "versionRefs": [list(ref) for ref in TARGET_CARD_LEVEL_REFS],
        },
        "master": {
            "triggerRows": [row.to_dict() for row in catalog.trigger_rows],
            "orderedVersions": [card.to_dict() for card in catalog.card_versions],
            "effectRows": [effect.to_dict() for effect in catalog.effect_rows],
        },
        "predicate": {
            "field": "ProduceExamFieldStatusType_CardPlayAggressiveUp",
            "signedSnapshot": True,
            "boundary": DIRECT_EFFECT_BUILD_BOUNDARY,
            "positive": "signed_aggressive_value >= 9",
            "not": "not (signed_aggressive_value >= 9)",
            "complementary": True,
            "equality": {"at8": {"not": True, "positive": False}, "at9": {"not": False, "positive": True}},
            "readFields": ["current Aggressive scalar"],
            "ignoredFields": [
                "global card-play count",
                "per-turn card-play count",
                "Aggressive/status delta",
                "TriggerEffectStatusEffect.SpendCount",
            ],
            "sameCardReread": False,
        },
        "snapshotComparison": {
            "predicateImplementation": "same IsEffectTriggerFieldValid -> IsFieldStatusTriggerStatusEffect -> GetAggressive(true)",
            "cardTriggerCallSite": CARD_ADMISSION_BOUNDARY,
            "effectTriggerCallSite": DIRECT_EFFECT_BUILD_BOUNDARY,
            "broadPhase": "both before payment; direct effect dispatch is later",
            "precisePhase": "different call-site sub-phases; effect leaf does not assume identity",
            "targetScalarConclusion": "same current Aggressive snapshot for this field because candidate commands are built before queued effects/payment mutate state",
        },
        "transactionOrder": {
            "ordered": [
                "card admission: IsPlayable/playTrigger check",
                "direct candidate build: PlayProduceExamEffectList and effect-slot trigger checks in Master order",
                "build-side card.PlayCardCount",
                "ordinary cost/payment and cost difference reactives",
                "captured card-play-valid status-listener children",
                "direct PlayEffect slots in Master order",
                "UserCardAfterCheck",
                "MovePlayCard(position,isUsePlayableCount,card)",
                "resolved-card PlayCardCount",
                "ExamParameterModel.PlayCardCountIncrement",
                "CardPlayCountAdd/Status.UpdatePlayCardCount/history update",
            ],
            "admission": "target card playTriggerId is empty; only effect-slot gates are in Master",
            "cost": "ConsumeCardCost follows ExecuteCardCommandImpl candidate construction; cost is outside this leaf",
            "direct": "slot candidates are built first, then direct type-5 PlayEffect commands dispatch after payment",
            "count": "count/history writes occur after direct effects; isOncePlayEffect=false rebuilds slots per play",
            "move": "MovePlayCard follows UserCardAfterCheck and precedes resolved-card count/history updates",
            "normalForcedExtra": "same predicate/input field set; isUsePlayableCount changes batch/move mode, not Aggressive comparison",
        },
        "accounting": {
            "familySemantics": "direct means end-to-end target card unlocked after resolving that family alone; coBlocked means the other target trigger still blocks",
            "families": family_accounting,
            "batch": batch.to_dict(),
            "expectedProbe": "four versions unlock only when both target families are resolved",
        },
        "nativeEvidence": {
            "android": ANDROID_NATIVE_EVIDENCE,
            "pcMetadata": PC_METADATA_EVIDENCE,
        },
        "validation": {
            "focusedTests": focused_test_count,
            "hashVerification": "not_performed",
            "securityVerification": "not_performed",
            "clockVerification": "not_performed",
            "runtimeAgentCalls": False,
        },
        "scopeGuard": {
            "centralPlan2CoreRuntimeModified": False,
            "nativeSearchModified": False,
            "formalCoverageModified": False,
            "plan3Modified": False,
            "guiOrControllerModified": False,
            "unknownOrAlteredTriggerCardSlot": "fail_closed",
        },
    }


__all__ = [
    "AggressiveEffectTriggerInput",
    "ANDROID_NATIVE_EVIDENCE",
    "CARD_NAME_BY_UPGRADE",
    "CARD_ORIGINS",
    "DIRECT_EFFECT_BUILD_BOUNDARY",
    "EffectTriggerBoundary",
    "EffectTriggerBuildProbe",
    "EffectTriggerCardSlot",
    "EffectTriggerContract",
    "EffectTriggerEvaluation",
    "EffectTriggerEvaluationInput",
    "EffectTriggerResolution",
    "EXPECTED_TRIGGER_ROWS",
    "KNOWN_EXECUTABLE_EFFECT_TYPES",
    "LESSON_EFFECT_BY_UPGRADE",
    "NATIVE_AUDIT_ARTIFACT",
    "PC_METADATA_EVIDENCE",
    "Plan2EffectTriggerAggressiveUp9Catalog",
    "Plan2EffectTriggerAggressiveUp9ContractError",
    "REVIEW_VALUE_MULTIPLE_EFFECT_ID",
    "TARGET_CARD_ID",
    "TARGET_CARD_LEVEL_REFS",
    "TARGET_TRIGGER_IDS",
    "TARGET_TRIGGER_NOT_ID",
    "TARGET_TRIGGER_POSITIVE_ID",
    "TARGET_THRESHOLD",
    "TargetCardVersion",
    "TriggerFamilyAccounting",
    "build_plan2_effect_triggers_aggressive_up9_audit",
    "evaluate_aggressive_effect_trigger",
    "load_catalog",
    "load_plan2_effect_triggers_aggressive_up9_catalog",
    "resolve_aggressive_effect_trigger",
    "simulate_effect_trigger_build",
    "validate_target_card_version",
]
