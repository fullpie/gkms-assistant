"""Exact standalone resolver for the Plan 3 interval-five lesson listener.

The adapter owns only ``p_card-00-sup-3_152`` and its permanent
``ExamPlayCountInterval(5)`` status enchant.  It reuses the engine's typed
status-enchant instance, but deliberately does not register the phase or
effect with the central Plan 3 executor.

Android v3.2.3 increments each listener's phase counter after the accepted
card becomes ``PlayingCard`` and before direct card effects.  The same native
card entry point is used by accepted ordinary, forced, and extra plays; the
``isUsePlayableCount`` flag does not guard the phase-count increment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    CATEGORY_ACTIVE,
    COST_STAMINA,
    EFFECT_LESSON,
    EFFECT_STATUS_ENCHANT,
    LESSON_UNKNOWN,
    MOVE_LOST,
    MOVE_UNKNOWN,
    PLAN_COMMON,
    ActivePlan3StatusEnchant,
    Plan3Effect,
    Plan3StatusEnchantRule,
    Plan3Trigger,
    load_plan3_effect,
)


CARD_ID = "p_card-00-sup-3_152"
CARD_UPGRADES = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple((CARD_ID, upgrade) for upgrade in CARD_UPGRADES)
AFFECTED_CARD_VERSION_COUNT = 4
SOLE_UNLOCK_CARD_VERSION_COUNT = 4
CO_BLOCKED_CARD_VERSION_COUNT = 0

TRIGGER_ID = "e_trigger-exam_play_count_interval-5"
PHASE_PLAY_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
PHASE_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
PHASE_PLAY_COUNT_INTERVAL_AFTER = (
    "ProduceExamPhaseType_ExamPlayCountIntervalAfter"
)
INTERVAL = 5
AUDITED_UNFILTERED_INTERVALS = frozenset((3, 5))

INSTALLER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-00-sup-3_152-enc01"
)
STATUS_ENCHANT_ID = "enchant-p_card-00-sup-3_152-enc01"
CHILD_EFFECT_ID = "e_effect-exam_lesson-0004-01"
CHILD_LESSON_VALUE = 4

RANDOM_MOVE_EFFECT_ID = (
    "e_effect-exam_card_move-p_card_search-deck_grave-"
    "p_card-00-acc-0_002-lost-random-1_1"
)
PLAYABLE_VALUE_EFFECT_ID = "e_effect-exam_playable_value_add-01"
ORDERED_CARD_EFFECT_IDS = (
    RANDOM_MOVE_EFFECT_ID,
    PLAYABLE_VALUE_EFFECT_ID,
    INSTALLER_EFFECT_ID,
)
CARD_STAMINA_BY_UPGRADE = (7, 4, 3, 2)

EFFECT_GROUP_LESSON = "effect_group-visible-exam_lesson-000"
EFFECT_GROUP_STATUS_ENCHANT = "effect_group-visible-exam_status_enchant-000"

INT32_MAX = 2**31 - 1

# GetCardPlayAfterValidEffectList is one stable list for phase 3 and phase 44;
# no relative order between those two predicates is invented here.
NATIVE_CARD_TRANSACTION_ORDER = (
    "set-playing-card",
    "increment-listener-phase-counts-23-44-25",
    "exam-card-play-listeners",
    "exam-play-count-interval-listeners",
    "ordered-direct-card-effects",
    "final-zone-move-settled",
    "card-play-after-and-interval-after-listeners",
)


class PlaySource(str, Enum):
    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


@dataclass(frozen=True, slots=True)
class AcceptedPlay:
    source: PlaySource
    accepted: bool = True
    playing_card_present: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.source, PlaySource):
            raise TypeError("source must be PlaySource")
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be bool")
        if not isinstance(self.playing_card_present, bool):
            raise TypeError("playing_card_present must be bool")


@dataclass(frozen=True, slots=True)
class PlayCountPhaseCounter:
    instance_id: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.instance_id, str) or not self.instance_id:
            raise TypeError("instance_id must be non-empty text")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or not 0 <= self.count <= INT32_MAX
        ):
            raise ValueError("count must be a non-negative Int32")


@dataclass(frozen=True, slots=True)
class IntervalFiveProgram:
    installer: Plan3Effect

    @property
    def rule(self):
        return self.installer.status_enchant

    @property
    def child(self) -> Plan3Effect:
        assert self.rule is not None
        return self.rule.effects[0]


@dataclass(frozen=True, slots=True)
class IntervalFiveState:
    lesson_parameter: int
    active: tuple[ActivePlan3StatusEnchant, ...]
    phase_counts: tuple[PlayCountPhaseCounter, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.lesson_parameter, bool)
            or not isinstance(self.lesson_parameter, int)
            or not 0 <= self.lesson_parameter <= INT32_MAX
        ):
            raise ValueError("lesson_parameter must be a non-negative Int32")
        active = tuple(self.active)
        counters = tuple(self.phase_counts)
        if not all(isinstance(item, ActivePlan3StatusEnchant) for item in active):
            raise TypeError("active must contain ActivePlan3StatusEnchant values")
        if not all(isinstance(item, PlayCountPhaseCounter) for item in counters):
            raise TypeError("phase_counts must contain PlayCountPhaseCounter values")
        active_ids = tuple(item.instance_id for item in active)
        counter_ids = tuple(item.instance_id for item in counters)
        if (
            any(not item for item in active_ids)
            or len(active_ids) != len(set(active_ids))
        ):
            raise ValueError("active listener instance IDs must be unique")
        if len(counter_ids) != len(set(counter_ids)):
            raise ValueError("phase counter instance IDs must be unique")
        if set(counter_ids).difference(active_ids):
            raise ValueError("phase counters must reference active listeners")
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "phase_counts", counters)


@dataclass(frozen=True, slots=True)
class IntervalFiveFire:
    listener_sequence: int
    instance_id: str
    count_before: int
    count_after: int
    child_sequence: int
    child_effect_id: str
    lesson_before: int
    lesson_after: int


@dataclass(frozen=True, slots=True)
class IntervalFiveExecution:
    before: IntervalFiveState
    after: IntervalFiveState
    event: AcceptedPlay
    fires: tuple[IntervalFiveFire, ...] = ()
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = NATIVE_CARD_TRANSACTION_ORDER

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def matches_interval_five_trigger(trigger: object) -> bool:
    """Match only the audited phase-23 interval-five trigger row."""

    return bool(
        matches_unfiltered_interval_trigger(trigger)
        and trigger.id == TRIGGER_ID
        and trigger.phase_values == (INTERVAL,)
    )


def matches_unfiltered_interval_trigger(trigger: object) -> bool:
    """Match the two native NIA/Common unfiltered interval rows (3 or 5).

    This is only a trigger-shape gate.  Ownership and nested effect execution
    remain with the exact Common listener or the NIA accepted-play extension.
    """

    return bool(
        isinstance(trigger, Plan3Trigger)
        and len(trigger.phase_values) == 1
        and trigger.phase_values[0] in AUDITED_UNFILTERED_INTERVALS
        and trigger.id
        == f"e_trigger-exam_play_count_interval-{trigger.phase_values[0]}"
        and trigger.phase_types == (PHASE_PLAY_COUNT_INTERVAL,)
        and not trigger.field_check_types
        and not trigger.field_types
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def matches_interval_five_rule(rule: object) -> bool:
    """Match the exact listener identity and its single Lesson +4 child."""

    if not (
        isinstance(rule, Plan3StatusEnchantRule)
        and rule.id == STATUS_ENCHANT_ID
        and matches_interval_five_trigger(rule.trigger)
        and len(rule.effects) == 1
    ):
        return False
    child = rule.effects[0]
    return bool(
        child.id == CHILD_EFFECT_ID
        and child.effect_type == EFFECT_LESSON
        and child.value1 == CHILD_LESSON_VALUE
        and child.value2 == 0
        and child.effect_count == 1
        and child.effect_turn == 0
        and not child.status_enchant_id
        and child.status_enchant is None
        and not child.chain_effect_id
        and not child.chain_effect_ids
        and child.chain_effect is None
        and child.trigger is None
        and not child.once
        and child.card_move_rule is None
    )


def _program_errors(installer: object) -> tuple[str, ...]:
    if not isinstance(installer, Plan3Effect):
        return ("typed-installer-required",)
    errors: list[str] = []
    if installer.id != INSTALLER_EFFECT_ID:
        errors.append("installer-id")
    if (
        installer.effect_type != EFFECT_STATUS_ENCHANT
        or installer.value1 != 0
        or installer.value2 != 0
        or installer.effect_count != 0
        or installer.effect_turn != -1
        or installer.status_enchant_id != STATUS_ENCHANT_ID
        or installer.chain_effect_id
        or installer.chain_effect_ids
        or installer.chain_effect is not None
        or installer.trigger is not None
        or installer.once
        or installer.card_move_rule is not None
    ):
        errors.append("installer-shape")
    rule = installer.status_enchant
    if rule is None or rule.id != STATUS_ENCHANT_ID:
        errors.append("status-enchant")
        return tuple(errors)
    if not matches_interval_five_trigger(rule.trigger):
        errors.append("trigger-shape")
    if len(rule.effects) != 1:
        errors.append("child-count")
        return tuple(errors)
    if not matches_interval_five_rule(rule):
        errors.append("child-shape")
    return tuple(errors)


def load_interval_five_program(
    database: Path = DEFAULT_DATABASE,
) -> IntervalFiveProgram:
    installer = load_plan3_effect(INSTALLER_EFFECT_ID, database)
    errors = _program_errors(installer)
    if errors:
        raise ValueError(f"unsupported interval-five program: {errors!r}")
    return IntervalFiveProgram(installer)


def matches_interval_five_installer(effect: object) -> bool:
    return isinstance(effect, Plan3Effect) and not _program_errors(effect)


def install_interval_five_listener(
    program: IntervalFiveProgram,
    instance_id: str,
    *,
    native_uid: int = 0,
) -> ActivePlan3StatusEnchant:
    if not isinstance(program, IntervalFiveProgram) or _program_errors(
        program.installer
    ):
        raise ValueError("exact interval-five program is required")
    if not isinstance(instance_id, str) or not instance_id:
        raise TypeError("instance_id must be non-empty text")
    if isinstance(native_uid, bool) or not isinstance(native_uid, int) or native_uid < 0:
        raise ValueError("native_uid must be a non-negative integer")
    assert program.rule is not None
    return ActivePlan3StatusEnchant(
        instance_id=instance_id,
        source_id=program.installer.id,
        rule=program.rule,
        max_uses=0,
        uses=0,
        max_uses_per_turn=0,
        uses_this_turn=0,
        remaining_turns=-1,
        passing_turn_start=False,
        is_item_direct=False,
        turn_count=0,
        native_uid=native_uid,
    )


def _listener_errors(
    listener: ActivePlan3StatusEnchant, program: IntervalFiveProgram
) -> tuple[str, ...]:
    errors: list[str] = []
    if listener.source_id != INSTALLER_EFFECT_ID or listener.rule != program.rule:
        errors.append(f"listener-program:{listener.instance_id}")
    if not (
        listener.max_uses == 0
        and listener.uses == 0
        and listener.max_uses_per_turn == 0
        and listener.uses_this_turn == 0
        and listener.remaining_turns == -1
        and listener.passing_turn_start is False
        and listener.is_item_direct is False
        and listener.turn_count == 0
        and not listener.captured_card_guid
        and listener.is_encore_enchant is False
    ):
        errors.append(f"listener-lifecycle:{listener.instance_id}")
    return tuple(errors)


def execute_interval_five_card_start(
    state: IntervalFiveState,
    event: AcceptedPlay,
    program: IntervalFiveProgram,
) -> IntervalFiveExecution:
    """Resolve the native pre-direct-effect boundary for one card attempt.

    A rejected play is an exact no-op because it never reaches
    ``ExecuteCardCommandImpl``.  An accepted event without a current
    ``PlayingCard`` or any unknown phase-23 listener fails closed and returns
    the original state by identity.
    """

    if not isinstance(state, IntervalFiveState):
        raise TypeError("state must be IntervalFiveState")
    if not isinstance(event, AcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    if not isinstance(program, IntervalFiveProgram):
        raise TypeError("program must be IntervalFiveProgram")
    errors = list(_program_errors(program.installer))
    if not event.accepted:
        return IntervalFiveExecution(state, state, event, unresolved=tuple(errors))
    if not event.playing_card_present:
        errors.append("accepted-play-has-no-playing-card")

    counters_before = {item.instance_id: item.count for item in state.phase_counts}
    interval_listeners: list[tuple[int, ActivePlan3StatusEnchant]] = []
    for sequence, listener in enumerate(state.active):
        has_phase = PHASE_PLAY_COUNT_INTERVAL in listener.rule.trigger.phase_types
        if not has_phase:
            if listener.instance_id in counters_before:
                errors.append(f"counter-for-non-interval:{listener.instance_id}")
            continue
        if listener.rule.trigger.phase_types != (PHASE_PLAY_COUNT_INTERVAL,):
            errors.append(f"unknown-interval-phase-shape:{listener.instance_id}")
            continue
        errors.extend(_listener_errors(listener, program))
        interval_listeners.append((sequence, listener))

    exact_ids = {listener.instance_id for _, listener in interval_listeners}
    for instance_id in counters_before:
        if instance_id not in exact_ids:
            errors.append(f"counter-for-unknown-listener:{instance_id}")
    if errors:
        return IntervalFiveExecution(
            state, state, event, unresolved=tuple(dict.fromkeys(errors))
        )

    lesson = state.lesson_parameter
    counters: list[PlayCountPhaseCounter] = []
    fires: list[IntervalFiveFire] = []
    for sequence, listener in interval_listeners:
        count_before = counters_before.get(listener.instance_id, 0)
        if count_before == INT32_MAX:
            return IntervalFiveExecution(
                state,
                state,
                event,
                unresolved=(f"phase-count-overflow:{listener.instance_id}",),
            )
        count_after = count_before + 1
        counters.append(PlayCountPhaseCounter(listener.instance_id, count_after))
        # Native rejects count zero and increments first, so occurrence five,
        # not occurrence zero, is the first firing.
        if count_after == 0 or count_after % INTERVAL:
            continue
        if lesson > INT32_MAX - CHILD_LESSON_VALUE:
            return IntervalFiveExecution(
                state, state, event, unresolved=("lesson-parameter-overflow",)
            )
        lesson_before = lesson
        lesson += CHILD_LESSON_VALUE
        fires.append(
            IntervalFiveFire(
                listener_sequence=sequence,
                instance_id=listener.instance_id,
                count_before=count_before,
                count_after=count_after,
                child_sequence=0,
                child_effect_id=CHILD_EFFECT_ID,
                lesson_before=lesson_before,
                lesson_after=lesson,
            )
        )

    if not interval_listeners:
        return IntervalFiveExecution(state, state, event)
    after = IntervalFiveState(lesson, state.active, tuple(counters))
    return IntervalFiveExecution(state, after, event, tuple(fires))


def matches_interval_card(card: object) -> bool:
    """Return true only for one exact affected Master card version."""

    upgrade = getattr(card, "upgrade", None)
    if isinstance(upgrade, bool) or not isinstance(upgrade, int):
        return False
    return bool(
        getattr(card, "id", None) == CARD_ID
        and upgrade in CARD_UPGRADES
        and getattr(card, "plan_type", None) == PLAN_COMMON
        and getattr(card, "category", None) == CATEGORY_ACTIVE
        and getattr(card, "stamina_cost", None)
        == CARD_STAMINA_BY_UPGRADE[upgrade]
        and getattr(card, "force_stamina_cost", None) == 0
        and getattr(card, "cost_type", None) == COST_STAMINA
        and getattr(card, "cost_value", None) == 0
        and getattr(card, "play_trigger", None) is None
        and getattr(card, "move_position_type", None) == MOVE_LOST
        and tuple(effect.id for effect in getattr(card, "effects", ()))
        == ORDERED_CARD_EFFECT_IDS
    )


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "AUDITED_UNFILTERED_INTERVALS",
    "AcceptedPlay",
    "CARD_ID",
    "CARD_STAMINA_BY_UPGRADE",
    "CARD_UPGRADES",
    "CHILD_EFFECT_ID",
    "CHILD_LESSON_VALUE",
    "CO_BLOCKED_CARD_VERSION_COUNT",
    "INSTALLER_EFFECT_ID",
    "INTERVAL",
    "IntervalFiveExecution",
    "IntervalFiveFire",
    "IntervalFiveProgram",
    "IntervalFiveState",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "ORDERED_CARD_EFFECT_IDS",
    "PHASE_PLAY_COUNT_INTERVAL",
    "PHASE_PLAY_COUNT_INTERVAL_AFTER",
    "PlayCountPhaseCounter",
    "PlaySource",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "STATUS_ENCHANT_ID",
    "TRIGGER_ID",
    "execute_interval_five_card_start",
    "install_interval_five_listener",
    "load_interval_five_program",
    "matches_interval_card",
    "matches_interval_five_rule",
    "matches_interval_five_trigger",
    "matches_interval_five_installer",
    "matches_unfiltered_interval_trigger",
]
