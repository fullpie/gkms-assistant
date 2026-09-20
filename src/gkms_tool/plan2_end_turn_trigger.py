"""Exact Plan2 runtime for the native ``ExamEndTurn`` listener family.

The executable contract is intentionally narrow: a normal card
``ExamStatusEnchant`` installs one ``TriggerEffectStatusEffect`` whose trigger
is exactly ``ExamEndTurn`` with no interval or secondary filter.  The current
Master children used by this family are ``ExamReview``,
``ExamLessonDependBlock``, and direct ``ExamCardPlayAggressive``.  Unknown
shapes fail closed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    F32_NEGATIVE_EPSILON,
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    ceil_f32_to_i32,
    f32,
)
from .plan2_review_multiple import (
    Plan2ReviewScoreTransition,
    Plan2ScoreApplication,
    simulate_end_turn_review_score,
)
from .plan2_state import (
    PERMANENT_TURN,
    Plan2EndTurnEffect,
    Plan2EndTurnListener,
    Plan2State,
)


STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
END_TURN_TRIGGER_ID = "e_trigger-exam_end_turn"
END_TURN_PHASE_TYPE = "ProduceExamPhaseType_ExamEndTurn"
REVIEW_EFFECT_TYPE = "ProduceExamEffectType_ExamReview"
LESSON_DEPEND_BLOCK_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamLessonDependBlock"
)
CARD_PLAY_AGGRESSIVE_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamCardPlayAggressive"
)
TURN_CHECK_REVIEW_SOURCE = "native:ExamTurnCheck.Review"

NATIVE_END_TURN_PHASE_ORDER = (
    END_TURN_PHASE_TYPE,
    "ProduceExamPhaseType_ExamEndTurnInterval",
    "ProduceExamPhaseType_ExamEndTurnTimer",
    "ExamPlayCommandType_TurnCheck.Review",
    "next:ProduceExamPhaseType_ExamStartTurn",
)

ANDROID_END_TURN_TRIGGER_EVIDENCE = {
    "install": {
        "symbol": "StatusEnchantEffectExecutor.ExecuteEffect",
        "va": "0x7E8FA08",
        "fact": (
            "Select(...).ToList preserves child order; effectCount/value1 "
            "map to total/per-turn limits and non-positive values become -1"
        ),
    },
    "listener": {
        "symbol": "ExamStatusEffectCollection.TryAddTriggerEffectStatus",
        "va": "0x7E8FEE0",
        "constructor_va": "0x7EADF04",
        "fact": "normal card installs a distinct active-list listener instance",
    },
    "filter": {
        "symbol": "ExamStatusEffectCollection.GetTriggerEffectList",
        "va": "0x7EA1D74",
        "predicate_va": "0x7EA9298",
        "fact": "stable filter rejects exhausted total/per-turn counts",
    },
    "count": {
        "symbol": "TriggerEffectStatusEffect.SpendCount",
        "va": "0x7EAE70C",
        "fact": "one decrement per listener activation, after ordered children",
    },
    "remove": {
        "symbol": "ExamStatusEffectCollection.RemoveCountLimitTriggeredStatus",
        "va": "0x7EA52B0",
        "fact": "a limited listener is removed after its total count reaches zero",
    },
    "phase": {
        "symbol": "ExamSequence.<ExamLoopTaskAsync>d__94.MoveNext",
        "va": "0x7EDCE78",
        "isil_lines": [8094, 8096, 8144, 8244, 8275, 8282],
        "fact": (
            "ExamEndTurn, EndTurnInterval, and EndTurnTimer commands precede "
            "TurnCheck Review scoring; TurnStart lifetime spending is later"
        ),
    },
}


class Plan2EndTurnContractError(ValueError):
    """A Master row is outside the native end-turn shape proved here."""


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2EndTurnContractError(f"{label} must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2EndTurnContractError(f"{label} is outside Int32")
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(raw_json: object, row_id: str) -> dict[str, object]:
    try:
        raw = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise Plan2EndTurnContractError(f"{row_id}: invalid raw_json") from error
    if not isinstance(raw, dict):
        raise Plan2EndTurnContractError(f"{row_id}: raw_json must be an object")
    return raw


def _json_array(raw_json: object, label: str) -> list[object]:
    try:
        value = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise Plan2EndTurnContractError(f"{label}: invalid JSON array") from error
    if not isinstance(value, list):
        raise Plan2EndTurnContractError(f"{label} must be an array")
    return value


def _require_equal(
    raw: dict[str, object], key: str, expected: object, row_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2EndTurnContractError(
            f"{row_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


def _ordered_string_array(
    raw: dict[str, object], key: str, row_id: str
) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise Plan2EndTurnContractError(
            f"{row_id}: {key} must be an array of non-empty strings"
        )
    return tuple(value)


@dataclass(frozen=True, slots=True)
class Plan2EndTurnProgram:
    """Validated native installation program for one wrapper effect."""

    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    phase_type: str
    phase_values: tuple[int, ...]
    effects: tuple[Plan2EndTurnEffect, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int
    effect_count: int
    effect_value1: int

    def __post_init__(self) -> None:
        if self.phase_type != END_TURN_PHASE_TYPE:
            raise Plan2EndTurnContractError("program phase must be ExamEndTurn")
        if tuple(self.phase_values):
            raise Plan2EndTurnContractError(
                "ExamEndTurn program must not have interval phase values"
            )
        if not self.effects:
            raise Plan2EndTurnContractError("program must contain child effects")
        turn = _plain_int(self.turn, "turn")
        if turn != PERMANENT_TURN and turn < 1:
            raise Plan2EndTurnContractError(
                "turn must be -1 (permanent) or positive"
            )
        if _plain_int(self.effect_count, "effect_count") < 0:
            raise Plan2EndTurnContractError("effect_count must be non-negative")
        if _plain_int(self.effect_value1, "effect_value1") < 0:
            raise Plan2EndTurnContractError("effect_value1 must be non-negative")

    @property
    def interval(self) -> None:
        """This exact phase trigger has no interval modulo gate."""

        return None

    @property
    def native_limit_count(self) -> int:
        return self.effect_count if self.effect_count > 0 else -1

    @property
    def native_limit_count_in_turn(self) -> int:
        return self.effect_value1 if self.effect_value1 > 0 else -1


@dataclass(frozen=True, slots=True)
class Plan2EndTurnInstallTransition:
    before: Plan2State
    after: Plan2State
    program: Plan2EndTurnProgram
    created_status_uid: int


@dataclass(frozen=True, slots=True)
class Plan2TriggeredScoreOccurrence:
    index: int
    raw_score: int
    applied_score: int


@dataclass(frozen=True, slots=True)
class Plan2EndTurnEffectOccurrence:
    listener_status_uid: int
    effect_index: int
    effect_id: str
    effect_type: str
    effect_group_ids: tuple[str, ...]
    score_occurrences: tuple[Plan2TriggeredScoreOccurrence, ...]
    review_before: int
    review_after: int


@dataclass(frozen=True, slots=True)
class Plan2EndTurnActivation:
    listener_before: Plan2EndTurnListener
    listener_after: Plan2EndTurnListener | None
    effects: tuple[Plan2EndTurnEffectOccurrence, ...]


@dataclass(frozen=True, slots=True)
class Plan2EndTurnTransition:
    before: Plan2State
    after_exact_end_turn: Plan2State
    after: Plan2State
    activations: tuple[Plan2EndTurnActivation, ...]
    removed_status_uids: tuple[int, ...]
    review_score: Plan2ReviewScoreTransition
    native_phase_order: tuple[str, ...]
    event_trace: tuple[str, ...]


Plan2EndTurnScoreApplicator = Callable[
    [Plan2State, int, str, int], Plan2ScoreApplication
]


def _apply_raw_score(
    state: Plan2State, raw_score: int, _source_id: str, _index: int
) -> Plan2ScoreApplication:
    next_score = state.score + raw_score
    if not 0 <= next_score <= INT64_MAX:
        raise OverflowError("score result is outside non-negative Int64")
    return Plan2ScoreApplication(
        after=replace(state, score=next_score),
        applied_score=raw_score,
    )


def _validate_neutral_effect_fields(
    row: sqlite3.Row, raw: dict[str, object]
) -> None:
    effect_id = str(row["id"])
    for key, expected in (
        ("id", effect_id),
        ("effectType", str(row["effect_type"])),
        ("effectValue1", int(row["value1"])),
        ("effectValue2", int(row["value2"])),
        ("effectCount", int(row["effect_count"])),
        ("effectTurn", int(row["effect_turn"])),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        ("produceCardSearchId", ""),
        ("movePositionType", "ProduceCardMovePositionType_Unknown"),
        ("produceCardSearchId2", ""),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
    ):
        _require_equal(raw, key, expected, effect_id)


def _load_child(row: sqlite3.Row) -> Plan2EndTurnEffect:
    effect_id = str(row["id"])
    effect_type = str(row["effect_type"])
    raw = _json_object(row["raw_json"], effect_id)
    _validate_neutral_effect_fields(row, raw)
    _require_equal(raw, "produceExamStatusEnchantId", "", effect_id)
    value1 = _plain_int(int(row["value1"]), f"{effect_id}.value1")
    value2 = _plain_int(int(row["value2"]), f"{effect_id}.value2")
    count = _plain_int(int(row["effect_count"]), f"{effect_id}.count")
    turn = _plain_int(int(row["effect_turn"]), f"{effect_id}.turn")
    if effect_type == REVIEW_EFFECT_TYPE:
        if value1 <= 0 or value2 != 0 or count != 0 or turn != 0:
            raise Plan2EndTurnContractError(
                f"{effect_id}: unsupported ExamReview child shape"
            )
    elif effect_type == LESSON_DEPEND_BLOCK_EFFECT_TYPE:
        if value1 <= 0 or value2 != 0 or count < 1 or turn != 0:
            raise Plan2EndTurnContractError(
                f"{effect_id}: unsupported ExamLessonDependBlock child shape"
            )
    elif effect_type == CARD_PLAY_AGGRESSIVE_EFFECT_TYPE:
        if value1 <= 0 or value2 != 0 or count != 0 or turn != 0:
            raise Plan2EndTurnContractError(
                f"{effect_id}: unsupported ExamCardPlayAggressive child shape"
            )
    else:
        raise Plan2EndTurnContractError(
            f"{effect_id}: unsupported end-turn child type {effect_type}"
        )
    return Plan2EndTurnEffect(
        effect_id=effect_id,
        effect_type=effect_type,
        value1=value1,
        value2=value2,
        count=count,
        turn=turn,
        effect_group_ids=_ordered_string_array(raw, "effectGroupIds", effect_id),
    )


def _validate_trigger(row: sqlite3.Row) -> tuple[int, ...]:
    trigger_id = str(row["id"])
    phase_types = _json_array(row["phase_types_json"], trigger_id)
    phase_values = _json_array(row["phase_values_json"], trigger_id)
    expected_columns = (
        ("field_status_check_types_json", []),
        ("field_status_types_json", []),
        ("field_status_values_json", []),
        ("field_status_produce_card_search_ids_json", []),
        ("effect_types_json", []),
    )
    if phase_types != [END_TURN_PHASE_TYPE] or phase_values:
        raise Plan2EndTurnContractError(
            f"{trigger_id}: expected exact ExamEndTurn with no interval"
        )
    for column, expected in expected_columns:
        if _json_array(row[column], trigger_id) != expected:
            raise Plan2EndTurnContractError(
                f"{trigger_id}: unsupported trigger field {column}"
            )
    if (
        str(row["produce_card_search_id"])
        or int(row["upper_search_count"]) != 0
        or int(row["lower_search_count"]) != 0
        or str(row["card_move_position_type"])
        != "ProduceCardMovePositionType_Unknown"
        or str(row["lesson_type"]) != "ProduceStepLessonType_Unknown"
    ):
        raise Plan2EndTurnContractError(
            f"{trigger_id}: unsupported trigger search/filter shape"
        )
    raw = _json_object(row["raw_json"], trigger_id)
    for key, expected in (
        ("id", trigger_id),
        ("phaseTypes", [END_TURN_PHASE_TYPE]),
        ("phaseValues", []),
        ("fieldStatusCheckTypes", []),
        ("fieldStatusTypes", []),
        ("fieldStatusValues", []),
        ("fieldStatusProduceCardSearchIds", []),
        ("produceCardSearchId", ""),
        ("upperSearchCount", 0),
        ("lowerSearchCount", 0),
        ("cardMovePositionType", "ProduceCardMovePositionType_Unknown"),
        ("effectTypes", []),
        ("lessonType", "ProduceStepLessonType_Unknown"),
    ):
        _require_equal(raw, key, expected, trigger_id)
    return tuple(_plain_int(int(value), f"{trigger_id}.phaseValue") for value in phase_values)


def load_plan2_end_turn_program(
    wrapper_effect_id: str, database: Path = DEFAULT_DATABASE
) -> Plan2EndTurnProgram:
    """Load one wrapper/status/trigger/children chain and validate it strictly."""

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
            raise Plan2EndTurnContractError(
                f"{wrapper_effect_id}: expected {STATUS_ENCHANT_EFFECT_TYPE}"
            )
        status_id = str(wrapper["status_enchant_id"])
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (status_id,)
        ).fetchone()
        if status is None:
            raise Plan2EndTurnContractError(
                f"{wrapper_effect_id}: missing status {status_id}"
            )
        trigger_id = str(status["produce_exam_trigger_id"])
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if trigger is None:
            raise Plan2EndTurnContractError(
                f"{status_id}: missing trigger {trigger_id}"
            )
        child_ids = tuple(
            str(value)
            for value in _json_array(
                status["produce_exam_effect_ids_json"], status_id
            )
        )
        if not child_ids or any(not child_id for child_id in child_ids):
            raise Plan2EndTurnContractError(
                f"{status_id}: child effect IDs must be non-empty"
            )
        children: list[Plan2EndTurnEffect] = []
        for child_id in child_ids:
            child = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (child_id,)
            ).fetchone()
            if child is None:
                raise Plan2EndTurnContractError(
                    f"{status_id}: missing child effect {child_id}"
                )
            children.append(_load_child(child))

    wrapper_raw = _json_object(wrapper["raw_json"], wrapper_effect_id)
    _validate_neutral_effect_fields(wrapper, wrapper_raw)
    _require_equal(
        wrapper_raw,
        "produceExamStatusEnchantId",
        status_id,
        wrapper_effect_id,
    )
    if (
        int(wrapper["value2"]) != 0
        or str(wrapper["chain_effect_id"])
        or int(wrapper["effect_count"]) < 0
        or int(wrapper["value1"]) < 0
    ):
        raise Plan2EndTurnContractError(
            f"{wrapper_effect_id}: unsupported StatusEnchant count/value shape"
        )
    status_raw = _json_object(status["raw_json"], status_id)
    for key, expected in (
        ("id", status_id),
        ("assetId", str(status["asset_id"])),
        ("produceExamTriggerId", trigger_id),
        ("produceExamEffectIds", list(child_ids)),
    ):
        _require_equal(status_raw, key, expected, status_id)
    phase_values = _validate_trigger(trigger)
    return Plan2EndTurnProgram(
        wrapper_effect_id=wrapper_effect_id,
        status_enchant_id=status_id,
        trigger_id=trigger_id,
        phase_type=END_TURN_PHASE_TYPE,
        phase_values=phase_values,
        effects=tuple(children),
        wrapper_effect_group_ids=_ordered_string_array(
            wrapper_raw, "effectGroupIds", wrapper_effect_id
        ),
        turn=_plain_int(int(wrapper["effect_turn"]), "effect_turn"),
        effect_count=_plain_int(int(wrapper["effect_count"]), "effect_count"),
        effect_value1=_plain_int(int(wrapper["value1"]), "effect_value1"),
    )


def simulate_install_plan2_end_turn_listener(
    state: Plan2State, program: Plan2EndTurnProgram
) -> Plan2EndTurnInstallTransition:
    """Project native normal-card StatusEnchant installation."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnProgram):
        raise TypeError("program must be Plan2EndTurnProgram")
    uid = state.next_status_uid
    listener = Plan2EndTurnListener(
        status_uid=uid,
        wrapper_effect_id=program.wrapper_effect_id,
        status_enchant_id=program.status_enchant_id,
        trigger_id=program.trigger_id,
        effects=program.effects,
        wrapper_effect_group_ids=program.wrapper_effect_group_ids,
        turn=program.turn,
        limit_count=program.native_limit_count,
        limit_count_in_turn=program.native_limit_count_in_turn,
        limit_count_in_turn_remaining=program.native_limit_count_in_turn,
        turn_count=0,
        is_passing_turn_start=False,
    )
    after = replace(
        state,
        end_turn_listeners=state.end_turn_listeners + (listener,),
        next_status_uid=uid + 1,
    )
    return Plan2EndTurnInstallTransition(state, after, program, uid)


def _lesson_depend_block_raw_score(block: int, permil: int) -> int:
    ratio = f32(f32(permil) / f32(1000.0))
    product = f32(ratio * f32(block))
    return ceil_f32_to_i32(f32(product + F32_NEGATIVE_EPSILON))


def _execute_child(
    state: Plan2State,
    listener: Plan2EndTurnListener,
    effect: Plan2EndTurnEffect,
    effect_index: int,
    apply_score: Plan2EndTurnScoreApplicator,
) -> tuple[Plan2State, Plan2EndTurnEffectOccurrence]:
    before_review = state.review
    score_rows: list[Plan2TriggeredScoreOccurrence] = []
    after = state
    if effect.effect_type == REVIEW_EFFECT_TYPE:
        value = _i32(after.review + effect.value1)
        if value < 0:
            raise OverflowError("native Review addition wrapped negative")
        after = replace(after, review=value)
    elif effect.effect_type == LESSON_DEPEND_BLOCK_EFFECT_TYPE:
        raw_score = _lesson_depend_block_raw_score(after.block, effect.value1)
        for index in range(effect.count):
            application = apply_score(after, raw_score, effect.effect_id, index)
            if not isinstance(application, Plan2ScoreApplication):
                raise TypeError("apply_score must return Plan2ScoreApplication")
            if not isinstance(application.after, Plan2State):
                raise TypeError("score application after must be Plan2State")
            applied = _plain_int(application.applied_score, "applied_score")
            after = application.after
            score_rows.append(
                Plan2TriggeredScoreOccurrence(index, raw_score, applied)
            )
    elif effect.effect_type == CARD_PLAY_AGGRESSIVE_EFFECT_TYPE:
        value = _i32(after.card_play_aggressive + effect.value1)
        if value < 0:
            raise OverflowError("native Aggressive addition wrapped negative")
        after = replace(after, card_play_aggressive=value)
    else:  # pragma: no cover - loader/state constructor boundary
        raise Plan2EndTurnContractError(
            f"unsupported runtime child type: {effect.effect_type}"
        )
    return after, Plan2EndTurnEffectOccurrence(
        listener_status_uid=listener.status_uid,
        effect_index=effect_index,
        effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        effect_group_ids=effect.effect_group_ids,
        score_occurrences=tuple(score_rows),
        review_before=before_review,
        review_after=after.review,
    )


def _replace_or_remove_listener(
    state: Plan2State,
    uid: int,
    replacement: Plan2EndTurnListener | None,
) -> Plan2State:
    listeners: list[Plan2EndTurnListener] = []
    found = False
    for listener in state.end_turn_listeners:
        if listener.status_uid != uid:
            listeners.append(listener)
            continue
        found = True
        if replacement is not None:
            listeners.append(replacement)
    if not found:
        raise RuntimeError(f"active end-turn listener disappeared: {uid}")
    return replace(state, end_turn_listeners=tuple(listeners))


def simulate_plan2_exam_end_turn(
    state: Plan2State,
    *,
    apply_score: Plan2EndTurnScoreApplicator = _apply_raw_score,
) -> Plan2EndTurnTransition:
    """Project ExamEndTurn listeners and the later same-turn Review score.

    The active-list snapshot is stable and each eligible listener executes its
    children in Master order.  Count spending happens once after all children;
    total-count removal is committed before the later TurnCheck Review score.
    Interval and timer phases are recorded in their native position but are
    deliberately not implemented by this exact-phase module.
    """

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    after = state
    activations: list[Plan2EndTurnActivation] = []
    removed: list[int] = []
    trace: list[str] = [f"phase:{END_TURN_PHASE_TYPE}"]
    for snapshot_listener in state.end_turn_listeners:
        current = next(
            (
                listener
                for listener in after.end_turn_listeners
                if listener.status_uid == snapshot_listener.status_uid
            ),
            None,
        )
        if current is None or not current.can_trigger:
            continue
        effect_rows: list[Plan2EndTurnEffectOccurrence] = []
        for effect_index, effect in enumerate(current.effects):
            after, occurrence = _execute_child(
                after, current, effect, effect_index, apply_score
            )
            effect_rows.append(occurrence)
            trace.append(
                f"listener:{current.status_uid}:effect:{effect_index}:{effect.effect_id}"
            )
        total = current.limit_count
        if total >= 0:
            total = _i32(total - 1)
        remaining = current.limit_count_in_turn_remaining
        if current.limit_count_in_turn >= 0:
            remaining = _i32(remaining - 1)
        listener_after = replace(
            current,
            limit_count=total,
            limit_count_in_turn_remaining=remaining,
        )
        if current.limit_count >= 0 and total <= 0:
            listener_after = None
            removed.append(current.status_uid)
        after = _replace_or_remove_listener(
            after, current.status_uid, listener_after
        )
        activations.append(
            Plan2EndTurnActivation(current, listener_after, tuple(effect_rows))
        )

    after_exact = after
    trace.extend(
        (
            "phase:ProduceExamPhaseType_ExamEndTurnInterval",
            "phase:ProduceExamPhaseType_ExamEndTurnTimer",
            "phase:ExamPlayCommandType_TurnCheck.Review",
        )
    )

    def apply_review_score(
        current: Plan2State, raw_score: int, index: int
    ) -> Plan2ScoreApplication:
        application = apply_score(
            current, raw_score, TURN_CHECK_REVIEW_SOURCE, index
        )
        trace.append(f"turn-check:review-score:{index}")
        return application

    review_score = simulate_end_turn_review_score(
        after_exact, apply_score=apply_review_score
    )
    trace.append("next:ProduceExamPhaseType_ExamStartTurn")
    return Plan2EndTurnTransition(
        before=state,
        after_exact_end_turn=after_exact,
        after=review_score.after,
        activations=tuple(activations),
        removed_status_uids=tuple(removed),
        review_score=review_score,
        native_phase_order=NATIVE_END_TURN_PHASE_ORDER,
        event_trace=tuple(trace),
    )


__all__ = [
    "ANDROID_END_TURN_TRIGGER_EVIDENCE",
    "CARD_PLAY_AGGRESSIVE_EFFECT_TYPE",
    "END_TURN_PHASE_TYPE",
    "END_TURN_TRIGGER_ID",
    "LESSON_DEPEND_BLOCK_EFFECT_TYPE",
    "NATIVE_END_TURN_PHASE_ORDER",
    "Plan2EndTurnActivation",
    "Plan2EndTurnContractError",
    "Plan2EndTurnEffectOccurrence",
    "Plan2EndTurnInstallTransition",
    "Plan2EndTurnProgram",
    "Plan2EndTurnScoreApplicator",
    "Plan2EndTurnTransition",
    "Plan2TriggeredScoreOccurrence",
    "REVIEW_EFFECT_TYPE",
    "STATUS_ENCHANT_EFFECT_TYPE",
    "TURN_CHECK_REVIEW_SOURCE",
    "load_plan2_end_turn_program",
    "simulate_install_plan2_end_turn_listener",
    "simulate_plan2_exam_end_turn",
]
