"""Minimal immutable Plan2 state for native Review/end-turn execution.

This module deliberately does not mirror the broad Plan3 state.  The only
status objects represented here are the layers read by native
``GetReviewMultiple``; Review and ReviewCountAdd are the two scalar getters
needed by the native end-turn scoring sequence.

Android v3.2.3 evidence (``libil2cpp.so`` / ``dump.cs``):

* ``ReviewMultipleStatusEffect.ctor`` at VA ``0x7E9AFB4`` stores Value and
  Turn, and makes a negative Turn permanent;
* ``ExamStatusEffectCollection.GetReviewMultiple`` at VA ``0x7E9B004``
  starts at binary32 1.0 and adds every active Value / binary32 1000.0 in
  active-list order;
* the generic turn-start lifetime uses ``IsPassingTurnStart``: a freshly
  installed layer survives its first boundary, while an already-passing
  finite layer spends one Turn and is removed at zero.

The two listener records intentionally store only installed programs, native
turn/count fields, and stable active-list identity.  Card zones and GUID
lookup remain in the shared playing-card identity primitive.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    INT64_MAX,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
    ParameterApplicationStatus,
)
from .exam_context import CurrentTurnBoundary
from .plan2_exam_save_battle_scoring import (
    Plan2CurrentTurnScoreApplication,
    Plan2CurrentTurnScoreRequest,
    Plan2ExamSaveBattleScoringContext,
    Plan2ScoreIntegrationPoint,
    Plan2ScoreKind,
    apply_plan2_current_turn_score,
    carry_plan2_battle_application_status,
    close_plan2_exam_save_battle_turn,
)
from .plan2_stamina_consumption_add import (
    StaminaConsumptionAddLayer,
    StaminaConsumptionAddRuntime,
    simulate_turn_start as simulate_stamina_consumption_add_turn_start,
)


PERMANENT_TURN = -1


def _require_plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _require_i32(value: object, label: str) -> int:
    result = _require_plain_int(value, label)
    if not INT32_MIN <= result <= INT32_MAX:
        raise ValueError(f"{label} is outside Int32: {result}")
    return result


@dataclass(frozen=True, slots=True)
class Plan2ReviewMultipleLayer:
    """One native ReviewMultiple status instance in active-list order."""

    status_uid: int
    permil: int
    turn: int
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        uid = _require_plain_int(self.status_uid, "status_uid")
        if uid < 1:
            raise ValueError("status_uid must be positive")
        _require_i32(self.permil, "permil")
        turn = _require_i32(self.turn, "turn")
        if turn != PERMANENT_TURN and turn < 1:
            raise ValueError("turn must be -1 (permanent) or positive")
        if type(self.is_passing_turn_start) is not bool:
            raise TypeError("is_passing_turn_start must be a boolean")

    @property
    def is_turn_limited(self) -> bool:
        return self.turn != PERMANENT_TURN


@dataclass(frozen=True, slots=True)
class Plan2EndTurnEffect:
    """One child effect retained in Master order by StatusEnchant."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise ValueError("effect_id must be non-empty")
        if not isinstance(self.effect_type, str) or not self.effect_type:
            raise ValueError("effect_type must be non-empty")
        _require_i32(self.value1, "value1")
        _require_i32(self.value2, "value2")
        count = _require_i32(self.count, "count")
        if count < 0:
            raise ValueError("count must be non-negative")
        _require_i32(self.turn, "turn")
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(group_id, str) or not group_id for group_id in groups):
            raise ValueError("effect_group_ids entries must be non-empty strings")
        object.__setattr__(self, "effect_group_ids", groups)


@dataclass(frozen=True, slots=True)
class Plan2EndTurnListener:
    """One native TriggerEffectStatusEffect in active-list order."""

    status_uid: int
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    effects: tuple[Plan2EndTurnEffect, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int
    limit_count: int
    limit_count_in_turn: int
    limit_count_in_turn_remaining: int
    turn_count: int = 0
    is_passing_turn_start: bool = False
    phase_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        uid = _require_plain_int(self.status_uid, "status_uid")
        if uid < 1:
            raise ValueError("status_uid must be positive")
        for label, value in (
            ("wrapper_effect_id", self.wrapper_effect_id),
            ("status_enchant_id", self.status_enchant_id),
            ("trigger_id", self.trigger_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be non-empty")
        effects = tuple(self.effects)
        if not effects or any(
            not isinstance(effect, Plan2EndTurnEffect) for effect in effects
        ):
            raise TypeError("effects must contain Plan2EndTurnEffect entries")
        object.__setattr__(self, "effects", effects)
        groups = tuple(self.wrapper_effect_group_ids)
        if any(not isinstance(group_id, str) or not group_id for group_id in groups):
            raise ValueError(
                "wrapper_effect_group_ids entries must be non-empty strings"
            )
        object.__setattr__(self, "wrapper_effect_group_ids", groups)
        turn = _require_i32(self.turn, "turn")
        if turn != PERMANENT_TURN and turn < 1:
            raise ValueError("turn must be -1 (permanent) or positive")
        total = _require_i32(self.limit_count, "limit_count")
        per_turn = _require_i32(self.limit_count_in_turn, "limit_count_in_turn")
        remaining = _require_i32(
            self.limit_count_in_turn_remaining,
            "limit_count_in_turn_remaining",
        )
        if total < PERMANENT_TURN:
            raise ValueError("limit_count must be -1 (unlimited) or non-negative")
        if per_turn < PERMANENT_TURN:
            raise ValueError(
                "limit_count_in_turn must be -1 (unlimited) or non-negative"
            )
        if remaining < PERMANENT_TURN:
            raise ValueError(
                "limit_count_in_turn_remaining must be -1 or non-negative"
            )
        if per_turn == PERMANENT_TURN and remaining != PERMANENT_TURN:
            raise ValueError("unlimited per-turn count must have remaining=-1")
        if per_turn >= 0 and not 0 <= remaining <= per_turn:
            raise ValueError("per-turn remaining must be within its limit")
        turn_count = _require_i32(self.turn_count, "turn_count")
        if turn_count < 0:
            raise ValueError("turn_count must be non-negative")
        if type(self.is_passing_turn_start) is not bool:
            raise TypeError("is_passing_turn_start must be a boolean")
        counts = tuple(self.phase_counts)
        if (
            tuple(sorted(counts)) != counts
            or len({key for key, _ in counts}) != len(counts)
        ):
            raise ValueError("phase_counts must be sorted with unique phases")
        for phase, current in counts:
            if not isinstance(phase, str) or not phase:
                raise ValueError("phase_counts phases must be non-empty strings")
            if _require_i32(current, "phase_count") < 0:
                raise ValueError("phase_count must be non-negative")
        object.__setattr__(self, "phase_counts", counts)

    @property
    def is_turn_limited(self) -> bool:
        return self.turn != PERMANENT_TURN

    @property
    def can_trigger(self) -> bool:
        return (self.limit_count < 0 or self.limit_count >= 1) and (
            self.limit_count_in_turn < 0
            or self.limit_count_in_turn_remaining >= 1
        )


@dataclass(frozen=True, slots=True)
class Plan2CardPlayEffect:
    """One supported child retained in CardPlay Master order."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise ValueError("effect_id must be non-empty")
        if not isinstance(self.effect_type, str) or not self.effect_type:
            raise ValueError("effect_type must be non-empty")
        _require_i32(self.value1, "value1")
        _require_i32(self.value2, "value2")
        count = _require_i32(self.count, "count")
        if count < 0:
            raise ValueError("count must be non-negative")
        _require_i32(self.turn, "turn")
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(group_id, str) or not group_id for group_id in groups):
            raise ValueError("effect_group_ids entries must be non-empty strings")
        object.__setattr__(self, "effect_group_ids", groups)


@dataclass(frozen=True, slots=True)
class Plan2CardPlayListener:
    """One exact ExamCardPlay TriggerEffectStatusEffect."""

    status_uid: int
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    search_id: str
    effects: tuple[Plan2CardPlayEffect, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int
    limit_count: int
    limit_count_in_turn: int
    limit_count_in_turn_remaining: int
    turn_count: int = 0
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        uid = _require_plain_int(self.status_uid, "status_uid")
        if uid < 1:
            raise ValueError("status_uid must be positive")
        for label, value in (
            ("wrapper_effect_id", self.wrapper_effect_id),
            ("status_enchant_id", self.status_enchant_id),
            ("trigger_id", self.trigger_id),
            ("search_id", self.search_id),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be non-empty")
        effects = tuple(self.effects)
        if not effects or any(
            not isinstance(effect, Plan2CardPlayEffect) for effect in effects
        ):
            raise TypeError("effects must contain Plan2CardPlayEffect entries")
        object.__setattr__(self, "effects", effects)
        groups = tuple(self.wrapper_effect_group_ids)
        if any(not isinstance(group_id, str) or not group_id for group_id in groups):
            raise ValueError(
                "wrapper_effect_group_ids entries must be non-empty strings"
            )
        object.__setattr__(self, "wrapper_effect_group_ids", groups)
        turn = _require_i32(self.turn, "turn")
        if turn != PERMANENT_TURN and turn < 1:
            raise ValueError("turn must be -1 (permanent) or positive")
        total = _require_i32(self.limit_count, "limit_count")
        per_turn = _require_i32(self.limit_count_in_turn, "limit_count_in_turn")
        remaining = _require_i32(
            self.limit_count_in_turn_remaining,
            "limit_count_in_turn_remaining",
        )
        if total < PERMANENT_TURN:
            raise ValueError("limit_count must be -1 (unlimited) or non-negative")
        if per_turn < PERMANENT_TURN:
            raise ValueError(
                "limit_count_in_turn must be -1 (unlimited) or non-negative"
            )
        if remaining < PERMANENT_TURN:
            raise ValueError(
                "limit_count_in_turn_remaining must be -1 or non-negative"
            )
        if per_turn == PERMANENT_TURN and remaining != PERMANENT_TURN:
            raise ValueError("unlimited per-turn count must have remaining=-1")
        if per_turn >= 0 and not 0 <= remaining <= per_turn:
            raise ValueError("per-turn remaining must be within its limit")
        turn_count = _require_i32(self.turn_count, "turn_count")
        if turn_count < 0:
            raise ValueError("turn_count must be non-negative")
        if type(self.is_passing_turn_start) is not bool:
            raise TypeError("is_passing_turn_start must be a boolean")

    @property
    def is_turn_limited(self) -> bool:
        return self.turn != PERMANENT_TURN

    @property
    def can_trigger(self) -> bool:
        return (self.limit_count < 0 or self.limit_count >= 1) and (
            self.limit_count_in_turn < 0
            or self.limit_count_in_turn_remaining >= 1
        )


@dataclass(frozen=True, slots=True)
class Plan2State:
    """Smallest Plan2 scalar/status state needed by the proven runtimes."""

    current_turn: int = 1
    review: int = 0
    score: int = 0
    review_count_add: int = 0
    block: int = 0
    card_play_aggressive: int = 0
    stamina: int = 0
    max_stamina: int = 0
    exam_card_play_count: int = 0
    turn_card_play_count: int = 0
    review_multiple_layers: tuple[Plan2ReviewMultipleLayer, ...] = ()
    end_turn_listeners: tuple[Plan2EndTurnListener, ...] = ()
    card_play_listeners: tuple[Plan2CardPlayListener, ...] = ()
    stamina_consumption_add_status: StaminaConsumptionAddLayer | None = None
    next_status_uid: int = 1
    battle_scoring: Plan2ExamSaveBattleScoringContext | None = None

    def __post_init__(self) -> None:
        current_turn = _require_plain_int(self.current_turn, "current_turn")
        if current_turn < 1:
            raise ValueError("current_turn must be positive")
        review = _require_i32(self.review, "review")
        if review < 0:
            raise ValueError("review must be non-negative")
        score = _require_plain_int(self.score, "score")
        if not 0 <= score <= INT64_MAX:
            raise ValueError("score must be a non-negative Int64")
        count_add = _require_i32(self.review_count_add, "review_count_add")
        if count_add < 0:
            raise ValueError("review_count_add must be non-negative")
        block = _require_i32(self.block, "block")
        if block < 0:
            raise ValueError("block must be non-negative")
        aggressive = _require_i32(
            self.card_play_aggressive, "card_play_aggressive"
        )
        if aggressive < 0:
            raise ValueError("card_play_aggressive must be non-negative")
        stamina = _require_i32(self.stamina, "stamina")
        max_stamina = _require_i32(self.max_stamina, "max_stamina")
        if stamina < 0 or max_stamina < 0 or stamina > max_stamina:
            raise ValueError("stamina must be within 0..max_stamina")
        exam_card_play_count = _require_i32(
            self.exam_card_play_count, "exam_card_play_count"
        )
        turn_card_play_count = _require_i32(
            self.turn_card_play_count, "turn_card_play_count"
        )
        if exam_card_play_count < 0 or turn_card_play_count < 0:
            raise ValueError("card-play counts must be non-negative")
        layers = tuple(self.review_multiple_layers)
        if any(not isinstance(layer, Plan2ReviewMultipleLayer) for layer in layers):
            raise TypeError(
                "review_multiple_layers entries must be Plan2ReviewMultipleLayer"
            )
        object.__setattr__(self, "review_multiple_layers", layers)
        listeners = tuple(self.end_turn_listeners)
        if any(
            not isinstance(listener, Plan2EndTurnListener)
            for listener in listeners
        ):
            raise TypeError(
                "end_turn_listeners entries must be Plan2EndTurnListener"
            )
        object.__setattr__(self, "end_turn_listeners", listeners)
        card_play_listeners = tuple(self.card_play_listeners)
        if any(
            not isinstance(listener, Plan2CardPlayListener)
            for listener in card_play_listeners
        ):
            raise TypeError(
                "card_play_listeners entries must be Plan2CardPlayListener"
            )
        object.__setattr__(self, "card_play_listeners", card_play_listeners)
        add_status = self.stamina_consumption_add_status
        if add_status is not None and not isinstance(
            add_status, StaminaConsumptionAddLayer
        ):
            raise TypeError(
                "stamina_consumption_add_status must be "
                "StaminaConsumptionAddLayer or None"
            )
        uids = [layer.status_uid for layer in layers]
        uids.extend(listener.status_uid for listener in listeners)
        uids.extend(listener.status_uid for listener in card_play_listeners)
        if add_status is not None:
            uids.append(add_status.status_uid)
        if len(uids) != len(set(uids)):
            raise ValueError("active status_uid values must be unique")
        next_uid = _require_plain_int(self.next_status_uid, "next_status_uid")
        if next_uid < 1 or (uids and next_uid <= max(uids)):
            raise ValueError("next_status_uid must be greater than every active uid")
        scoring = self.battle_scoring
        if scoring is not None:
            if not isinstance(scoring, Plan2ExamSaveBattleScoringContext):
                raise TypeError(
                    "battle_scoring must be "
                    "Plan2ExamSaveBattleScoringContext or None"
                )

    def get_review_multiple(self) -> float:
        """Return native ordered binary32 ``1 + sum(permil / 1000)``."""

        result = f32(1.0)
        for layer in self.review_multiple_layers:
            result = f32(result + permille_to_f32(layer.permil))
        return result

    def get_review_raw_score(self) -> int:
        """Read current Review, then multiply and ceil exactly as native."""

        if self.review == 0:
            return 0
        return ceil_f32_to_i32(
            f32(f32(self.review) * self.get_review_multiple())
        )

    def parameter_application_status(
        self, *, slump: bool
    ) -> ParameterApplicationStatus:
        """Project the one score status used by every native leaf executor."""

        if type(slump) is not bool:
            raise TypeError("slump must be a boolean")
        if self.battle_scoring is None:
            return ParameterApplicationStatus(
                judge_parameter=self.score,
                slump=slump,
            )
        return self.battle_scoring.application_status(slump=slump)


@dataclass(frozen=True, slots=True)
class Plan2StateScoreApplication:
    """Atomic scalar/context result for one already-calculated score hit."""

    before: Plan2State
    after: Plan2State
    actual_parameter: int
    battle_application: Plan2CurrentTurnScoreApplication | None


def apply_plan2_state_score(
    state: Plan2State,
    raw_parameter: int,
    *,
    score_kind: Plan2ScoreKind,
    integration_point: Plan2ScoreIntegrationPoint,
    slump: bool,
) -> Plan2StateScoreApplication:
    """Apply one score hit and keep ``score`` and battle context in lockstep."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if state.battle_scoring is None:
        after = replace(state, score=state.score + raw_parameter)
        return Plan2StateScoreApplication(
            before=state,
            after=after,
            actual_parameter=raw_parameter,
            battle_application=None,
        )
    application = apply_plan2_current_turn_score(
        state.battle_scoring,
        Plan2CurrentTurnScoreRequest(
            raw_parameter=raw_parameter,
            score_kind=score_kind,
            integration_point=integration_point,
            slump=slump,
        ),
    )
    after = replace(
        state,
        score=application.after.judge_parameter,
        battle_scoring=application.after,
    )
    return Plan2StateScoreApplication(
        before=state,
        after=after,
        actual_parameter=application.application.actual_parameter,
        battle_application=application,
    )


def carry_plan2_state_application_status(
    state: Plan2State,
    status: ParameterApplicationStatus,
) -> Plan2State:
    """Atomically adopt the score status returned by one native leaf loop."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(status, ParameterApplicationStatus):
        raise TypeError("status must be ParameterApplicationStatus")
    if state.battle_scoring is None:
        return replace(state, score=status.judge_parameter)
    context = carry_plan2_battle_application_status(
        state.battle_scoring,
        status,
    )
    return replace(
        state,
        score=status.judge_parameter,
        battle_scoring=context,
    )


@dataclass(frozen=True, slots=True)
class Plan2TurnStartTransition:
    """Pure projection of one native TurnStart lifetime boundary."""

    before: Plan2State
    after: Plan2State
    spent_status_uids: tuple[int, ...]
    expired_status_uids: tuple[int, ...]
    fresh_status_uids: tuple[int, ...]
    permanent_status_uids: tuple[int, ...]


def simulate_plan2_turn_start(state: Plan2State) -> Plan2TurnStartTransition:
    """Project one TurnStart without mutating or committing ``state``.

    The caller explicitly adopts ``transition.after`` if it wants to commit
    the projection.  This keeps preview/simulation and execution on one exact
    transition function instead of maintaining two drifting implementations.
    """

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    battle_scoring = state.battle_scoring
    next_exam_turn = state.current_turn + 1
    if battle_scoring is not None:
        boundary = close_plan2_exam_save_battle_turn(
            battle_scoring,
            CurrentTurnBoundary.NEXT_TURN,
        )
        battle_scoring = boundary.after_boundary
        next_exam_turn = battle_scoring.current_turn
    survivors: list[Plan2ReviewMultipleLayer] = []
    listener_survivors: list[Plan2EndTurnListener] = []
    card_play_listener_survivors: list[Plan2CardPlayListener] = []
    spent: list[int] = []
    expired: list[int] = []
    fresh: list[int] = []
    permanent: list[int] = []
    for layer in state.review_multiple_layers:
        if not layer.is_turn_limited:
            permanent.append(layer.status_uid)
            survivors.append(replace(layer, is_passing_turn_start=True))
            continue
        if not layer.is_passing_turn_start:
            fresh.append(layer.status_uid)
            survivors.append(replace(layer, is_passing_turn_start=True))
            continue
        spent.append(layer.status_uid)
        next_turn = layer.turn - 1
        if next_turn <= 0:
            expired.append(layer.status_uid)
        else:
            survivors.append(replace(layer, turn=next_turn))

    for listener in state.end_turn_listeners:
        next_listener = replace(
            listener,
            turn_count=_require_i32(listener.turn_count + 1, "turn_count"),
            limit_count_in_turn_remaining=listener.limit_count_in_turn,
            is_passing_turn_start=True,
        )
        if not listener.is_turn_limited:
            permanent.append(listener.status_uid)
            listener_survivors.append(next_listener)
            continue
        if not listener.is_passing_turn_start:
            fresh.append(listener.status_uid)
            listener_survivors.append(next_listener)
            continue
        spent.append(listener.status_uid)
        next_turn = listener.turn - 1
        if next_turn <= 0:
            expired.append(listener.status_uid)
        else:
            listener_survivors.append(replace(next_listener, turn=next_turn))

    for listener in state.card_play_listeners:
        next_listener = replace(
            listener,
            turn_count=_require_i32(listener.turn_count + 1, "turn_count"),
            limit_count_in_turn_remaining=listener.limit_count_in_turn,
            is_passing_turn_start=True,
        )
        if not listener.is_turn_limited:
            permanent.append(listener.status_uid)
            card_play_listener_survivors.append(next_listener)
            continue
        if not listener.is_passing_turn_start:
            fresh.append(listener.status_uid)
            card_play_listener_survivors.append(next_listener)
            continue
        spent.append(listener.status_uid)
        next_turn = listener.turn - 1
        if next_turn <= 0:
            expired.append(listener.status_uid)
        else:
            card_play_listener_survivors.append(
                replace(next_listener, turn=next_turn)
            )

    add_status = state.stamina_consumption_add_status
    next_add_status = add_status
    if add_status is not None:
        add_transition = simulate_stamina_consumption_add_turn_start(
            StaminaConsumptionAddRuntime(
                layers=(add_status,), next_status_uid=state.next_status_uid
            )
        )
        next_add_status = add_transition.after.layer
        spent.extend(add_transition.spent_status_uids)
        expired.extend(add_transition.expired_status_uids)
        fresh.extend(add_transition.fresh_status_uids)
        permanent.extend(add_transition.permanent_status_uids)

    after = replace(
        state,
        current_turn=next_exam_turn,
        review_multiple_layers=tuple(survivors),
        end_turn_listeners=tuple(listener_survivors),
        card_play_listeners=tuple(card_play_listener_survivors),
        stamina_consumption_add_status=next_add_status,
        turn_card_play_count=0,
        battle_scoring=battle_scoring,
    )
    return Plan2TurnStartTransition(
        before=state,
        after=after,
        spent_status_uids=tuple(spent),
        expired_status_uids=tuple(expired),
        fresh_status_uids=tuple(fresh),
        permanent_status_uids=tuple(permanent),
    )


__all__ = [
    "PERMANENT_TURN",
    "Plan2CardPlayEffect",
    "Plan2CardPlayListener",
    "Plan2EndTurnEffect",
    "Plan2EndTurnListener",
    "Plan2ReviewMultipleLayer",
    "Plan2State",
    "Plan2StateScoreApplication",
    "Plan2TurnStartTransition",
    "apply_plan2_state_score",
    "carry_plan2_state_application_status",
    "simulate_plan2_turn_start",
]
