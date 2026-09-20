"""Native Plan2 ReviewMultiple loader, executor, and Review scoring bridge.

The implementation is derived from Android v3.2.3 symbols and instructions,
not from localized card text.  Current PC Master rows are accepted only when
their executable fields match the proven ReviewMultiple shape.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN, INT64_MAX
from .plan2_state import PERMANENT_TURN, Plan2ReviewMultipleLayer, Plan2State


REVIEW_MULTIPLE_EFFECT_TYPE = "ProduceExamEffectType_ExamReviewMultiple"

ANDROID_REVIEW_MULTIPLE_EVIDENCE = {
    "executor": {
        "symbol": "ReviewMultipleEffectExecutor.ExecuteEffect",
        "va": "0x7E8A8DC",
        "fact": "reads before, calls TryAdd(value, turn), then reads after",
    },
    "try_add": {
        "symbol": "ExamStatusEffectCollection.TryAddReviewMultipleStatus",
        "va": "0x7E9AE04",
        "predicate_va": "0x7EA81B4",
        "fact": "last layer with exactly equal Turn is merged; negative sum clamps to zero",
    },
    "constructor": {
        "symbol": "ReviewMultipleStatusEffect.ctor",
        "va": "0x7E9AFB4",
        "fact": "negative Turn clears IsTurnLimited",
    },
    "getter": {
        "symbol": "ExamStatusEffectCollection.GetReviewMultiple",
        "va": "0x7E9B004",
        "fact": "ordered binary32 accumulation of Value / 1000 from 1.0",
    },
    "score_sequence": {
        "symbol": "ExamSequence.<ExecuteCommandImplAsync>d__111",
        "isil_lines": [4427, 4439, 4448, 4505, 4506, 4507, 4508, 4598],
        "fact": (
            "GetReview, GetReviewMultiple, GetReviewCountAdd, ceil, then one "
            "normal score-effect command per occurrence"
        ),
    },
}


class Plan2ReviewMultipleContractError(ValueError):
    """A Master row is not the native ReviewMultiple shape proved here."""


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2ReviewMultipleContractError(f"{label} must be an integer")
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleEffect:
    """The native executable fields of one ReviewMultiple Master row."""

    effect_id: str
    permil: int
    turn: int

    def __post_init__(self) -> None:
        if not self.effect_id:
            raise Plan2ReviewMultipleContractError("effect_id must be non-empty")
        permil = _plain_int(self.permil, "permil")
        if not INT32_MIN <= permil <= INT32_MAX:
            raise Plan2ReviewMultipleContractError("permil is outside Int32")
        turn = _plain_int(self.turn, "turn")
        if not INT32_MIN <= turn <= INT32_MAX:
            raise Plan2ReviewMultipleContractError("turn is outside Int32")
        if turn != PERMANENT_TURN and turn < 1:
            raise Plan2ReviewMultipleContractError(
                "turn must be -1 (permanent) or positive"
            )

    @property
    def is_permanent(self) -> bool:
        return self.turn == PERMANENT_TURN


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleTransition:
    before: Plan2State
    after: Plan2State
    effect: Plan2ReviewMultipleEffect
    multiplier_before: float
    multiplier_after: float
    created_status_uid: int | None
    merged_status_uid: int | None


@dataclass(frozen=True, slots=True)
class Plan2ScoreApplication:
    """Typed result from the plan-neutral per-occurrence score bridge."""

    after: Plan2State
    applied_score: int


@dataclass(frozen=True, slots=True)
class Plan2ReviewScoreOccurrence:
    index: int
    raw_score: int
    applied_score: int


@dataclass(frozen=True, slots=True)
class Plan2ReviewScoreTransition:
    """One immutable projection of native end-turn Review scoring."""

    before: Plan2State
    after: Plan2State
    review_value: int
    review_multiple: float
    review_count_add: int
    raw_score_per_occurrence: int
    occurrences: tuple[Plan2ReviewScoreOccurrence, ...]

    @property
    def total_applied_score(self) -> int:
        return sum(row.applied_score for row in self.occurrences)


Plan2ScoreApplicator = Callable[[Plan2State, int, int], Plan2ScoreApplication]


def _require_raw_equal(
    raw: dict[str, object], key: str, expected: object, effect_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2ReviewMultipleContractError(
            f"{effect_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


def _effect_from_row(row: sqlite3.Row) -> Plan2ReviewMultipleEffect:
    effect_id = str(row["id"])
    if str(row["effect_type"]) != REVIEW_MULTIPLE_EFFECT_TYPE:
        raise Plan2ReviewMultipleContractError(
            f"{effect_id}: expected {REVIEW_MULTIPLE_EFFECT_TYPE}"
        )
    value1 = int(row["value1"])
    value2 = int(row["value2"])
    effect_count = int(row["effect_count"])
    effect_turn = int(row["effect_turn"])
    status_id = str(row["status_enchant_id"])
    chain_id = str(row["chain_effect_id"])
    if value1 <= 0:
        raise Plan2ReviewMultipleContractError(
            f"{effect_id}: current-Master permil must be positive"
        )
    if value2 != 0 or effect_count != 0 or status_id or chain_id:
        raise Plan2ReviewMultipleContractError(
            f"{effect_id}: unsupported value2/count/status/chain shape"
        )
    try:
        raw = json.loads(str(row["raw_json"]))
    except json.JSONDecodeError as error:
        raise Plan2ReviewMultipleContractError(
            f"{effect_id}: raw_json is invalid"
        ) from error
    if not isinstance(raw, dict):
        raise Plan2ReviewMultipleContractError(
            f"{effect_id}: raw_json must be an object"
        )
    for key, expected in (
        ("id", effect_id),
        ("effectType", REVIEW_MULTIPLE_EFFECT_TYPE),
        ("effectValue1", value1),
        ("effectValue2", 0),
        ("effectCount", 0),
        ("effectTurn", effect_turn),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        ("produceCardSearchId", ""),
        ("movePositionType", "ProduceCardMovePositionType_Unknown"),
        ("produceCardSearchId2", ""),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", ""),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
    ):
        _require_raw_equal(raw, key, expected, effect_id)
    return Plan2ReviewMultipleEffect(effect_id, value1, effect_turn)


def load_plan2_review_multiple_effect(
    effect_id: str, database: Path = DEFAULT_DATABASE
) -> Plan2ReviewMultipleEffect:
    """Load and strictly validate one current-Master ReviewMultiple effect."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown Master effect: {effect_id}")
    return _effect_from_row(row)


def simulate_review_multiple(
    state: Plan2State,
    effect: Plan2ReviewMultipleEffect,
) -> Plan2ReviewMultipleTransition:
    """Project native TryAddReviewMultipleStatus without committing state."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(effect, Plan2ReviewMultipleEffect):
        raise TypeError("effect must be Plan2ReviewMultipleEffect")
    before_multiple = state.get_review_multiple()
    layers = list(state.review_multiple_layers)
    match_index = next(
        (
            index
            for index in range(len(layers) - 1, -1, -1)
            if layers[index].turn == effect.turn
        ),
        None,
    )
    created_uid: int | None = None
    merged_uid: int | None = None
    next_uid = state.next_status_uid
    if match_index is None:
        created_uid = next_uid
        layers.append(
            Plan2ReviewMultipleLayer(
                status_uid=created_uid,
                permil=effect.permil,
                turn=effect.turn,
                is_passing_turn_start=False,
            )
        )
        next_uid += 1
    else:
        existing = layers[match_index]
        merged_uid = existing.status_uid
        merged_value = max(0, _i32(existing.permil + effect.permil))
        layers[match_index] = replace(existing, permil=merged_value)
    after = replace(
        state,
        review_multiple_layers=tuple(layers),
        next_status_uid=next_uid,
    )
    return Plan2ReviewMultipleTransition(
        before=state,
        after=after,
        effect=effect,
        multiplier_before=before_multiple,
        multiplier_after=after.get_review_multiple(),
        created_status_uid=created_uid,
        merged_status_uid=merged_uid,
    )


def _apply_raw_score(
    state: Plan2State, raw_score: int, _occurrence_index: int
) -> Plan2ScoreApplication:
    next_score = state.score + raw_score
    if not 0 <= next_score <= INT64_MAX:
        raise OverflowError("Review score result is outside non-negative Int64")
    return Plan2ScoreApplication(
        after=replace(state, score=next_score),
        applied_score=raw_score,
    )


def simulate_end_turn_review_score(
    state: Plan2State,
    *,
    apply_score: Plan2ScoreApplicator = _apply_raw_score,
) -> Plan2ReviewScoreTransition:
    """Project native Review scoring, preserving getter and hit order.

    Native reads Review, ReviewMultiple, and ReviewCountAdd once, computes one
    ceiled raw value, then sends ``count_add + 1`` separate score effects
    through the ordinary score path.  ``apply_score`` is the intentionally
    narrow typed bridge for that wider path; it is called once per occurrence
    and receives the state produced by the preceding occurrence.
    """

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    review = state.review
    multiple = state.get_review_multiple()
    count_add = state.review_count_add
    raw_score = state.get_review_raw_score()
    after = state
    occurrences: list[Plan2ReviewScoreOccurrence] = []
    if review > 0:
        for index in range(count_add + 1):
            application = apply_score(after, raw_score, index)
            if not isinstance(application, Plan2ScoreApplication):
                raise TypeError("apply_score must return Plan2ScoreApplication")
            if not isinstance(application.after, Plan2State):
                raise TypeError("score application after must be Plan2State")
            applied = _plain_int(application.applied_score, "applied_score")
            after = application.after
            occurrences.append(
                Plan2ReviewScoreOccurrence(index, raw_score, applied)
            )
    return Plan2ReviewScoreTransition(
        before=state,
        after=after,
        review_value=review,
        review_multiple=multiple,
        review_count_add=count_add,
        raw_score_per_occurrence=raw_score,
        occurrences=tuple(occurrences),
    )


__all__ = [
    "ANDROID_REVIEW_MULTIPLE_EVIDENCE",
    "Plan2ReviewMultipleContractError",
    "Plan2ReviewMultipleEffect",
    "Plan2ReviewMultipleTransition",
    "Plan2ScoreApplication",
    "Plan2ReviewScoreOccurrence",
    "Plan2ReviewScoreTransition",
    "REVIEW_MULTIPLE_EFFECT_TYPE",
    "load_plan2_review_multiple_effect",
    "simulate_end_turn_review_score",
    "simulate_review_multiple",
]
