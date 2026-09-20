"""Exact standalone Plan2 adapter for ``ExamPlayCountInterval(5)``.

The Master graph and native matcher are owned by
``plan3_play_count_interval_trigger``.  This module only supplies the
Plan2-shaped state boundary that the existing Plan2 core does not have:
``Plan2State`` remains the base state, while this narrow wrapper carries the
phase-23 listener collection, listener-local counters, and the Lesson scalar.

The generic Plan2 listener record is reused from ``plan2_state``.  Its
``EndTurn`` name is historical; its fields are the same native
``TriggerEffectStatusEffect`` payload needed here and, unlike the CardPlay
record, it has no unrelated search predicate.  No central Plan2 core,
coverage builder, or GUI hook is installed by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TypeAlias

from . import plan3_play_count_interval_trigger as _plan3
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX
from .plan2_card_play_playing_search_trigger import PlaySource
from .plan2_state import (
    PERMANENT_TURN,
    Plan2EndTurnEffect,
    Plan2EndTurnListener,
    Plan2State,
    simulate_plan2_turn_start,
)
from .plan3_engine import Plan3Effect


# Reuse the completed exact Master/native contract verbatim.  These aliases
# intentionally avoid a second copy of the target card/status constants.
AFFECTED_CARD_VERSIONS = _plan3.AFFECTED_CARD_VERSIONS
AFFECTED_CARD_VERSION_COUNT = _plan3.AFFECTED_CARD_VERSION_COUNT
CARD_ID = _plan3.CARD_ID
CARD_STAMINA_BY_UPGRADE = _plan3.CARD_STAMINA_BY_UPGRADE
CARD_UPGRADES = _plan3.CARD_UPGRADES
CHILD_EFFECT_ID = _plan3.CHILD_EFFECT_ID
CHILD_LESSON_VALUE = _plan3.CHILD_LESSON_VALUE
CO_BLOCKED_CARD_VERSION_COUNT = _plan3.CO_BLOCKED_CARD_VERSION_COUNT
INSTALLER_EFFECT_ID = _plan3.INSTALLER_EFFECT_ID
INTERVAL = _plan3.INTERVAL
NATIVE_CARD_TRANSACTION_ORDER = _plan3.NATIVE_CARD_TRANSACTION_ORDER
ORDERED_CARD_EFFECT_IDS = _plan3.ORDERED_CARD_EFFECT_IDS
PHASE_CARD_PLAY = _plan3.PHASE_CARD_PLAY
PHASE_CARD_PLAY_AFTER = _plan3.PHASE_CARD_PLAY_AFTER
PHASE_PLAY_COUNT_INTERVAL = _plan3.PHASE_PLAY_COUNT_INTERVAL
PHASE_PLAY_COUNT_INTERVAL_AFTER = _plan3.PHASE_PLAY_COUNT_INTERVAL_AFTER
SOLE_UNLOCK_CARD_VERSION_COUNT = _plan3.SOLE_UNLOCK_CARD_VERSION_COUNT
STATUS_ENCHANT_ID = _plan3.STATUS_ENCHANT_ID
TRIGGER_ID = _plan3.TRIGGER_ID

# These are deliberately read from the existing exact module too; they are
# used only when adapting the typed child into the Plan2 listener record.
_LESSON_GROUP = _plan3.EFFECT_GROUP_LESSON
_STATUS_GROUP = _plan3.EFFECT_GROUP_STATUS_ENCHANT

DIRECT_CARD_VERSION_COUNT = SOLE_UNLOCK_CARD_VERSION_COUNT
TRIGGER_PHASE_BOUNDARY = "phase23-before-card-direct-effects"
SETTLEMENT_BOUNDARY = "after-final-zone-move"


class Plan2PlayCountIntervalContractError(ValueError):
    """A Plan2 adapter input is outside the exact audited shape."""


@dataclass(frozen=True, slots=True)
class AcceptedPlay:
    """One accepted/rejected Playing-card command at the phase-23 boundary."""

    source: PlaySource
    accepted: bool = True
    playing_card_present: bool = True
    is_use_playable_count: bool | None = None
    card: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source, PlaySource):
            raise TypeError("source must be Plan2 PlaySource")
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a boolean")
        if type(self.playing_card_present) is not bool:
            raise TypeError("playing_card_present must be a boolean")
        if self.is_use_playable_count is not None and type(
            self.is_use_playable_count
        ) is not bool:
            raise TypeError("is_use_playable_count must be bool or None")


Plan2AcceptedPlay: TypeAlias = AcceptedPlay
Plan2PlayCountIntervalProgram: TypeAlias = _plan3.IntervalFiveProgram
Plan2PlayCountIntervalListener: TypeAlias = Plan2EndTurnListener


@dataclass(frozen=True, slots=True)
class Plan2PlayCountPhaseCounter:
    """One listener-local native phase-23 counter."""

    status_uid: int
    count: int

    def __post_init__(self) -> None:
        if isinstance(self.status_uid, bool) or not isinstance(self.status_uid, int):
            raise TypeError("status_uid must be an integer")
        if self.status_uid < 1:
            raise ValueError("status_uid must be positive")
        if isinstance(self.count, bool) or not isinstance(self.count, int):
            raise TypeError("count must be an integer")
        if not 0 <= self.count <= INT32_MAX:
            raise ValueError("count must be a non-negative Int32")

    @property
    def instance_id(self) -> str:
        """Compatibility spelling for the shared listener-local concept."""

        return str(self.status_uid)


@dataclass(frozen=True, slots=True)
class Plan2PlayCountIntervalState:
    """Plan2 base state plus only the interval listener-local projection."""

    lesson_parameter: int
    active: tuple[Plan2PlayCountIntervalListener, ...]
    phase_counts: tuple[Plan2PlayCountPhaseCounter, ...] = ()
    plan2_state: Plan2State = field(default_factory=Plan2State)

    def __post_init__(self) -> None:
        if isinstance(self.lesson_parameter, bool) or not isinstance(
            self.lesson_parameter, int
        ):
            raise TypeError("lesson_parameter must be an integer")
        if not 0 <= self.lesson_parameter <= INT32_MAX:
            raise ValueError("lesson_parameter must be a non-negative Int32")
        if not isinstance(self.plan2_state, Plan2State):
            raise TypeError("plan2_state must be Plan2State")

        active = tuple(self.active)
        if not all(isinstance(item, Plan2EndTurnListener) for item in active):
            raise TypeError("active must contain Plan2 listener records")
        active_uids = tuple(item.status_uid for item in active)
        base_uids = {
            *(item.status_uid for item in self.plan2_state.end_turn_listeners),
            *(item.status_uid for item in self.plan2_state.card_play_listeners),
        }
        if len(active_uids) != len(set(active_uids)):
            raise ValueError("active interval listener status_uids must be unique")
        if set(active_uids).intersection(base_uids):
            raise ValueError("interval listener status_uids must be distinct")

        counters = tuple(self.phase_counts)
        if not all(
            isinstance(item, Plan2PlayCountPhaseCounter) for item in counters
        ):
            raise TypeError("phase_counts must contain Plan2 counters")
        counter_uids = tuple(item.status_uid for item in counters)
        if len(counter_uids) != len(set(counter_uids)):
            raise ValueError("phase counter status_uids must be unique")
        if set(counter_uids).difference(active_uids):
            raise ValueError("phase counters must reference active listeners")
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "phase_counts", counters)

    @property
    def base_state(self) -> Plan2State:
        """Existing Plan2 state, kept separate from interval-local state."""

        return self.plan2_state


@dataclass(frozen=True, slots=True)
class Plan2PlayCountIntervalInstallTransition:
    before: Plan2PlayCountIntervalState
    after: Plan2PlayCountIntervalState
    program: Plan2PlayCountIntervalProgram
    created_status_uid: int

    @property
    def listener(self) -> Plan2PlayCountIntervalListener:
        return self.after.active[-1]


@dataclass(frozen=True, slots=True)
class Plan2PlayCountIntervalFire:
    listener_sequence: int
    status_uid: int
    count_before: int
    count_after: int
    child_sequence: int
    child_effect_id: str
    child_effect_type: str
    child_effect_count: int
    lesson_before: int
    lesson_after: int
    spend_count_called: bool = True
    spend_count_delta: int = 0

    @property
    def delta(self) -> int:
        return self.lesson_after - self.lesson_before

    @property
    def source_effect_id(self) -> str:
        return INSTALLER_EFFECT_ID


@dataclass(frozen=True, slots=True)
class Plan2PlayCountIntervalExecution:
    before: Plan2PlayCountIntervalState
    after: Plan2PlayCountIntervalState
    event: AcceptedPlay
    fires: tuple[Plan2PlayCountIntervalFire, ...] = ()
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = NATIVE_CARD_TRANSACTION_ORDER
    event_trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after

    @property
    def lesson_delta(self) -> int:
        return self.after.lesson_parameter - self.before.lesson_parameter

    @property
    def trigger_boundary(self) -> str:
        return TRIGGER_PHASE_BOUNDARY

    @property
    def settlement_boundary(self) -> str:
        return SETTLEMENT_BOUNDARY


@dataclass(frozen=True, slots=True)
class Plan2PlayCountIntervalTurnStartTransition:
    before: Plan2PlayCountIntervalState
    after: Plan2PlayCountIntervalState
    spent_status_uids: tuple[int, ...]
    expired_status_uids: tuple[int, ...]
    fresh_status_uids: tuple[int, ...]
    permanent_status_uids: tuple[int, ...]


def matches_plan2_play_count_interval5_trigger(trigger: object) -> bool:
    """Delegate the exact trigger matcher to the completed Plan3 contract."""

    return _plan3.matches_interval_five_trigger(trigger)


def matches_plan2_play_count_interval5_rule(rule: object) -> bool:
    """Delegate the exact status/child matcher to the completed contract."""

    return _plan3.matches_interval_five_rule(rule)


def matches_plan2_play_count_interval5_installer(effect: object) -> bool:
    return _plan3.matches_interval_five_installer(effect)


# Short aliases keep the Plan3 naming available to callers that already use
# the shared exact contract.
matches_interval_five_trigger = matches_plan2_play_count_interval5_trigger
matches_interval_five_rule = matches_plan2_play_count_interval5_rule
matches_interval_five_installer = matches_plan2_play_count_interval5_installer
matches_interval_card = _plan3.matches_interval_card


def load_plan2_play_count_interval5_program(
    database: Path = DEFAULT_DATABASE,
) -> Plan2PlayCountIntervalProgram:
    """Load the exact wrapper/status/trigger/child graph through Plan3."""

    return _plan3.load_interval_five_program(database)


load_plan2_play_count_interval_program = load_plan2_play_count_interval5_program


def _require_program(
    program: Plan2PlayCountIntervalProgram,
) -> Plan2PlayCountIntervalProgram:
    if not isinstance(program, _plan3.IntervalFiveProgram):
        raise TypeError("program must be IntervalFiveProgram")
    if not matches_plan2_play_count_interval5_installer(program.installer):
        raise Plan2PlayCountIntervalContractError(
            "unsupported interval-five program"
        )
    return program


def _plan2_child(effect: Plan3Effect) -> Plan2EndTurnEffect:
    if not isinstance(effect, Plan3Effect):
        raise Plan2PlayCountIntervalContractError("typed child effect required")
    return Plan2EndTurnEffect(
        effect_id=effect.id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        count=effect.effect_count,
        turn=effect.effect_turn,
        effect_group_ids=(_LESSON_GROUP,),
    )


def _expected_listener(
    status_uid: int,
    program: Plan2PlayCountIntervalProgram,
) -> Plan2PlayCountIntervalListener:
    child = _plan2_child(program.child)
    return Plan2EndTurnListener(
        status_uid=status_uid,
        wrapper_effect_id=INSTALLER_EFFECT_ID,
        status_enchant_id=STATUS_ENCHANT_ID,
        trigger_id=TRIGGER_ID,
        effects=(child,),
        wrapper_effect_group_ids=(_LESSON_GROUP, _STATUS_GROUP),
        turn=PERMANENT_TURN,
        limit_count=-1,
        limit_count_in_turn=-1,
        limit_count_in_turn_remaining=-1,
        turn_count=0,
        is_passing_turn_start=False,
    )


def _listener_errors(
    listener: Plan2PlayCountIntervalListener,
    program: Plan2PlayCountIntervalProgram,
) -> tuple[str, ...]:
    expected = _expected_listener(listener.status_uid, program)
    errors: list[str] = []
    if listener.wrapper_effect_id != expected.wrapper_effect_id:
        errors.append(f"listener-wrapper:{listener.status_uid}")
    if listener.status_enchant_id != expected.status_enchant_id:
        errors.append(f"listener-status:{listener.status_uid}")
    if listener.trigger_id != expected.trigger_id:
        errors.append(f"listener-trigger:{listener.status_uid}")
    if listener.effects != expected.effects:
        errors.append(f"listener-child:{listener.status_uid}")
    if listener.wrapper_effect_group_ids != expected.wrapper_effect_group_ids:
        errors.append(f"listener-groups:{listener.status_uid}")
    if not (
        listener.turn == PERMANENT_TURN
        and listener.limit_count == -1
        and listener.limit_count_in_turn == -1
        and listener.limit_count_in_turn_remaining == -1
        and listener.turn_count >= 0
        and isinstance(listener.is_passing_turn_start, bool)
    ):
        errors.append(f"listener-lifecycle:{listener.status_uid}")
    return tuple(errors)


def install_plan2_play_count_interval5_listener(
    state: Plan2PlayCountIntervalState,
    program: Plan2PlayCountIntervalProgram,
) -> Plan2PlayCountIntervalInstallTransition:
    """Install one fresh permanent/unlimited listener without firing it."""

    if not isinstance(state, Plan2PlayCountIntervalState):
        raise TypeError("state must be Plan2PlayCountIntervalState")
    program = _require_program(program)
    uid = state.plan2_state.next_status_uid
    listener = _expected_listener(uid, program)
    after_plan2 = replace(state.plan2_state, next_status_uid=uid + 1)
    after = replace(
        state,
        plan2_state=after_plan2,
        active=state.active + (listener,),
    )
    return Plan2PlayCountIntervalInstallTransition(
        before=state,
        after=after,
        program=program,
        created_status_uid=uid,
    )


def install_plan2_play_count_interval5_listener_on_state(
    state: Plan2State,
    program: Plan2PlayCountIntervalProgram,
    *,
    lesson_parameter: int = 0,
) -> Plan2PlayCountIntervalInstallTransition:
    """Convenience bridge from the existing Plan2 state adapter."""

    return install_plan2_play_count_interval5_listener(
        Plan2PlayCountIntervalState(
            lesson_parameter=lesson_parameter,
            active=(),
            plan2_state=state,
        ),
        program,
    )


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def execute_plan2_play_count_interval5_card_start(
    state: Plan2PlayCountIntervalState,
    event: AcceptedPlay,
    program: Plan2PlayCountIntervalProgram,
) -> Plan2PlayCountIntervalExecution:
    """Resolve one accepted play at phase 23, before direct card effects.

    ``source`` and ``is_use_playable_count`` select no alternate counter:
    accepted ordinary, forced, and extra commands all use this same
    listener-local phase-23 count.  The final zone move and the combined
    phase-3/44 listener list are represented only in ``order``; they happen
    after this standalone transition.
    """

    if not isinstance(state, Plan2PlayCountIntervalState):
        raise TypeError("state must be Plan2PlayCountIntervalState")
    if not isinstance(event, AcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    if not isinstance(program, _plan3.IntervalFiveProgram):
        raise TypeError("program must be IntervalFiveProgram")

    errors: list[str] = []
    if not matches_plan2_play_count_interval5_installer(program.installer):
        errors.append("unsupported-interval-five-program")
    if not event.accepted:
        return Plan2PlayCountIntervalExecution(
            before=state,
            after=state,
            event=event,
            unresolved=tuple(errors),
            event_trace=("rejected-before-exam-card-play",),
        )
    if not event.playing_card_present:
        errors.append("accepted-play-has-no-playing-card")
    if event.card is not None and not matches_interval_card(event.card):
        errors.append("card-is-not-exact-common-interval-five-card")

    for listener in state.active:
        errors.extend(_listener_errors(listener, program))
    known_uids = {listener.status_uid for listener in state.active}
    for counter in state.phase_counts:
        if counter.status_uid not in known_uids:
            errors.append(f"counter-for-unknown-listener:{counter.status_uid}")
    if errors:
        return Plan2PlayCountIntervalExecution(
            before=state,
            after=state,
            event=event,
            unresolved=_unique(errors),
        )

    old_counts = {item.status_uid: item.count for item in state.phase_counts}
    counters: list[Plan2PlayCountPhaseCounter] = []
    fires: list[Plan2PlayCountIntervalFire] = []
    lesson = state.lesson_parameter
    trace: list[str] = [
        "set-playing-card",
        "increment-listener-phase-counts:23",
        "queue-exam-play-count-interval-listeners:phase23:pre-direct",
    ]
    for sequence, listener in enumerate(state.active):
        before = old_counts.get(listener.status_uid, 0)
        if before == INT32_MAX:
            return Plan2PlayCountIntervalExecution(
                before=state,
                after=state,
                event=event,
                unresolved=(f"phase-count-overflow:{listener.status_uid}",),
            )
        after = before + 1
        counters.append(Plan2PlayCountPhaseCounter(listener.status_uid, after))
        trace.append(
            f"listener:{listener.status_uid}:phase23-count:{before}->{after}"
        )
        if after < 1 or after % INTERVAL:
            continue
        if lesson > INT32_MAX - CHILD_LESSON_VALUE:
            return Plan2PlayCountIntervalExecution(
                before=state,
                after=state,
                event=event,
                unresolved=("lesson-parameter-overflow",),
            )
        lesson_before = lesson
        lesson += CHILD_LESSON_VALUE
        trace.append(
            f"spend-count:{listener.status_uid}:unlimited:no-delta"
        )
        trace.append(
            f"listener:{listener.status_uid}:effect:0:{CHILD_EFFECT_ID}:phase23:pre-direct"
        )
        fires.append(
            Plan2PlayCountIntervalFire(
                listener_sequence=sequence,
                status_uid=listener.status_uid,
                count_before=before,
                count_after=after,
                child_sequence=0,
                child_effect_id=CHILD_EFFECT_ID,
                child_effect_type="ProduceExamEffectType_ExamLesson",
                child_effect_count=1,
                lesson_before=lesson_before,
                lesson_after=lesson,
            )
        )

    if not state.active:
        return Plan2PlayCountIntervalExecution(
            before=state,
            after=state,
            event=event,
            event_trace=(),
        )
    trace.extend(
        (
            "ordered-direct-card-effects",
            "final-zone-move-settled",
            "card-play-after-and-interval-after-listeners",
        )
    )
    after_state = replace(state, lesson_parameter=lesson, phase_counts=tuple(counters))
    return Plan2PlayCountIntervalExecution(
        before=state,
        after=after_state,
        event=event,
        fires=tuple(fires),
        event_trace=tuple(trace),
    )


execute_plan2_play_count_interval_card_start = (
    execute_plan2_play_count_interval5_card_start
)


def simulate_plan2_play_count_interval5_turn_start(
    state: Plan2PlayCountIntervalState,
) -> Plan2PlayCountIntervalTurnStartTransition:
    """Reuse Plan2 TurnStart for the base state and preserve interval status."""

    if not isinstance(state, Plan2PlayCountIntervalState):
        raise TypeError("state must be Plan2PlayCountIntervalState")
    program = load_plan2_play_count_interval5_program()
    errors = [
        error
        for listener in state.active
        for error in _listener_errors(listener, program)
    ]
    if errors:
        raise Plan2PlayCountIntervalContractError(str(_unique(errors)))

    base_transition = simulate_plan2_turn_start(state.plan2_state)
    survivors: list[Plan2PlayCountIntervalListener] = []
    counters = {item.status_uid: item for item in state.phase_counts}
    spent = list(base_transition.spent_status_uids)
    expired = list(base_transition.expired_status_uids)
    fresh = list(base_transition.fresh_status_uids)
    permanent = list(base_transition.permanent_status_uids)
    for listener in state.active:
        if listener.turn_count == INT32_MAX:
            raise Plan2PlayCountIntervalContractError(
                f"listener turn_count overflow: {listener.status_uid}"
            )
        next_listener = replace(
            listener,
            turn_count=listener.turn_count + 1,
            limit_count_in_turn_remaining=listener.limit_count_in_turn,
            is_passing_turn_start=True,
        )
        if listener.turn == PERMANENT_TURN:
            permanent.append(listener.status_uid)
            survivors.append(next_listener)
            continue
        if not listener.is_passing_turn_start:
            fresh.append(listener.status_uid)
            survivors.append(next_listener)
            continue
        spent.append(listener.status_uid)
        remaining_turns = listener.turn - 1
        if remaining_turns <= 0:
            expired.append(listener.status_uid)
            counters.pop(listener.status_uid, None)
        else:
            survivors.append(replace(next_listener, turn=remaining_turns))

    after = Plan2PlayCountIntervalState(
        lesson_parameter=state.lesson_parameter,
        active=tuple(survivors),
        phase_counts=tuple(
            counters[listener.status_uid]
            for listener in survivors
            if listener.status_uid in counters
        ),
        plan2_state=base_transition.after,
    )
    return Plan2PlayCountIntervalTurnStartTransition(
        before=state,
        after=after,
        spent_status_uids=tuple(spent),
        expired_status_uids=tuple(expired),
        fresh_status_uids=tuple(fresh),
        permanent_status_uids=tuple(permanent),
    )


simulate_plan2_play_count_interval_turn_start = (
    simulate_plan2_play_count_interval5_turn_start
)


__all__ = [
    "AcceptedPlay",
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "CARD_ID",
    "CARD_STAMINA_BY_UPGRADE",
    "CARD_UPGRADES",
    "CHILD_EFFECT_ID",
    "CHILD_LESSON_VALUE",
    "CO_BLOCKED_CARD_VERSION_COUNT",
    "DIRECT_CARD_VERSION_COUNT",
    "INSTALLER_EFFECT_ID",
    "INTERVAL",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "ORDERED_CARD_EFFECT_IDS",
    "PERMANENT_TURN",
    "PHASE_CARD_PLAY",
    "PHASE_CARD_PLAY_AFTER",
    "PHASE_PLAY_COUNT_INTERVAL",
    "PHASE_PLAY_COUNT_INTERVAL_AFTER",
    "Plan2AcceptedPlay",
    "Plan2PlayCountIntervalContractError",
    "Plan2PlayCountIntervalExecution",
    "Plan2PlayCountIntervalFire",
    "Plan2PlayCountIntervalInstallTransition",
    "Plan2PlayCountIntervalListener",
    "Plan2PlayCountIntervalProgram",
    "Plan2PlayCountIntervalState",
    "Plan2PlayCountIntervalTurnStartTransition",
    "Plan2PlayCountPhaseCounter",
    "PlaySource",
    "SETTLEMENT_BOUNDARY",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "STATUS_ENCHANT_ID",
    "TRIGGER_ID",
    "TRIGGER_PHASE_BOUNDARY",
    "execute_plan2_play_count_interval5_card_start",
    "execute_plan2_play_count_interval_card_start",
    "install_plan2_play_count_interval5_listener",
    "install_plan2_play_count_interval5_listener_on_state",
    "load_plan2_play_count_interval5_program",
    "load_plan2_play_count_interval_program",
    "matches_interval_card",
    "matches_interval_five_installer",
    "matches_interval_five_rule",
    "matches_interval_five_trigger",
    "matches_plan2_play_count_interval5_installer",
    "matches_plan2_play_count_interval5_rule",
    "matches_plan2_play_count_interval5_trigger",
    "simulate_plan2_play_count_interval5_turn_start",
    "simulate_plan2_play_count_interval_turn_start",
]
