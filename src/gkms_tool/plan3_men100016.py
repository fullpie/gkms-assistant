"""Exact Android v3.2.3 contract for ``p_card-03-men-100_016#0``.

This is deliberately a one-card program, not a generic ExtraTurn or
StanceChangeFullPower implementation.  It owns the six ordered card slots,
the permanent three-use listener, and its finite 1500-permille child.  The
small standalone runtime exposes the native scheduling boundaries without a
dependency from this module back into the Plan 3 core's mutable workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .nia_lesson_value_multiple import (
    LessonParameterMultipleState,
    install_lesson_value_multiple,
    matches_lesson_value_multiple_effect_shape,
)
from .plan3_engine import (
    CATEGORY_MENTAL,
    COST_STAMINA,
    EFFECT_CARD_DRAW,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_STATUS_ENCHANT,
    FIELD_REMAINING_TURN,
    LESSON_UNKNOWN,
    MOVE_LOST,
    MOVE_UNKNOWN,
    PHASE_NONE,
    PLAN3,
    STANCE_FULL_POWER,
    Plan3Card,
    Plan3Effect,
    Plan3StatusEnchantRule,
    Plan3Trigger,
    load_plan3_card,
)


CARD_ID = "p_card-03-men-100_016"
CARD_UPGRADE = 0
CARD_VERSION = (CARD_ID, CARD_UPGRADE)

EFFECT_EXTRA_TURN = "ProduceExamEffectType_ExamExtraTurn"
EFFECT_LESSON_VALUE_MULTIPLE = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
PHASE_STANCE_CHANGE_FULL_POWER = (
    "ProduceExamPhaseType_ExamStanceChangeFullPower"
)

PLAYABLE_EFFECT_ID = "e_effect-exam_playable_value_add-01"
DRAW_EFFECT_ID = "e_effect-exam_card_draw-0002"
EXTRA_TURN_EFFECT_ID = "e_effect-exam_extra_turn"
INSTALLER_EFFECT_ID = (
    "e_effect-exam_status_enchant-03-inf-"
    "enchant-p_card-03-men-100_016-enc01"
)
STATUS_ENCHANT_ID = "enchant-p_card-03-men-100_016-enc01"
TRIGGER_ID = "e_trigger-exam_stance_change_full_power-remaining_turn-3"
CHILD_EFFECT_ID = "e_effect-exam_lesson_value_multiple-1500-01"

EXTRA_TURN_TRIGGER_IDS = (
    "e_trigger-none-preservation_change_count_up-2",
    "e_trigger-none-concentration_change_count_up-2",
    "e_trigger-none-full_power_change_count_up-2",
)
ORDERED_EFFECT_IDS = (
    PLAYABLE_EFFECT_ID,
    DRAW_EFFECT_ID,
    EXTRA_TURN_EFFECT_ID,
    EXTRA_TURN_EFFECT_ID,
    EXTRA_TURN_EFFECT_ID,
    INSTALLER_EFFECT_ID,
)
CARD_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_card_draw-000",
    "effect_group-visible-exam_playable_value_add-000",
    "effect_group-visible-exam_extra_turn-000",
    "effect_group-visible-exam_lesson_value_multiple-000",
)

FIELD_PRESERVATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_PreservationChangeCountUp"
)
FIELD_CONCENTRATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_ConcentrationChangeCountUp"
)
FIELD_FULL_POWER_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_FullPowerChangeCountUp"
)
EXTRA_TRIGGER_FIELDS = (
    FIELD_PRESERVATION_CHANGE_COUNT_UP,
    FIELD_CONCENTRATION_CHANGE_COUNT_UP,
    FIELD_FULL_POWER_CHANGE_COUNT_UP,
)

LISTENER_MAX_USES = 3
LISTENER_TURN = -1
CHILD_PERMIL = 1500
CHILD_TURN = 1
REMAINING_TURN_THRESHOLD = 3
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

NATIVE_EXTRA_TURN_ORDER = (
    "read-remain-turn-before",
    "read-extra-turn-before",
    "set-remain-turn-before-plus-one",
    "add-extra-turn-one",
    "create-after-minus-before-difference",
    "ordinary-end-boundary-decrements-remain-turn-once-later",
)
NATIVE_FULL_POWER_ORDER = (
    "consume-full-power-point",
    "commit-full-power-stance-and-change-counts",
    "select-exam-stance-change-full-power-phase-listeners",
    "evaluate-current-signed-remaining-turn-less-or-equal-three",
    "spend-finite-listener-use",
    "install-fresh-one-turn-lesson-value-multiple",
)


def _plain_effect(effect: Plan3Effect) -> bool:
    return bool(
        not effect.status_enchant_id
        and effect.status_enchant is None
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
        and effect.card_move_rule is None
        and not effect.once
    )


def matches_extra_turn_trigger(trigger: object, *, slot: int | None = None) -> bool:
    if not isinstance(trigger, Plan3Trigger):
        return False
    if slot is None:
        candidates = tuple(zip(EXTRA_TURN_TRIGGER_IDS, EXTRA_TRIGGER_FIELDS))
    elif slot in (2, 3, 4):
        candidates = ((EXTRA_TURN_TRIGGER_IDS[slot - 2], EXTRA_TRIGGER_FIELDS[slot - 2]),)
    else:
        return False
    return any(
        trigger.id == trigger_id
        and trigger.phase_types == (PHASE_NONE,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and trigger.field_types == (field,)
        and trigger.field_values == (2,)
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == LESSON_UNKNOWN
        for trigger_id, field in candidates
    )


def matches_extra_turn_effect(effect: object, *, slot: int | None = None) -> bool:
    return bool(
        isinstance(effect, Plan3Effect)
        and effect.id == EXTRA_TURN_EFFECT_ID
        and effect.effect_type == EFFECT_EXTRA_TURN
        and effect.value1 == 0
        and effect.value2 == 0
        and effect.effect_count == 0
        and effect.effect_turn == 0
        and _plain_effect(replace(effect, trigger=None))
        and matches_extra_turn_trigger(effect.trigger, slot=slot)
    )


def matches_full_power_trigger(trigger: object) -> bool:
    return bool(
        isinstance(trigger, Plan3Trigger)
        and trigger.id == TRIGGER_ID
        and trigger.phase_types == (PHASE_STANCE_CHANGE_FULL_POWER,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and trigger.field_types == (FIELD_REMAINING_TURN,)
        and trigger.field_values == (REMAINING_TURN_THRESHOLD,)
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def matches_lesson_value_multiple_child(effect: object) -> bool:
    return bool(
        matches_lesson_value_multiple_effect_shape(effect)
        and isinstance(effect, Plan3Effect)
        and effect.id == CHILD_EFFECT_ID
        and effect.value1 == CHILD_PERMIL
        and effect.effect_turn == CHILD_TURN
    )


def matches_status_rule(rule: object) -> bool:
    return bool(
        isinstance(rule, Plan3StatusEnchantRule)
        and rule.id == STATUS_ENCHANT_ID
        and matches_full_power_trigger(rule.trigger)
        and len(rule.effects) == 1
        and matches_lesson_value_multiple_child(rule.effects[0])
    )


def matches_installer(effect: object) -> bool:
    return bool(
        isinstance(effect, Plan3Effect)
        and effect.id == INSTALLER_EFFECT_ID
        and effect.effect_type == EFFECT_STATUS_ENCHANT
        and effect.value1 == 0
        and effect.value2 == 0
        and effect.effect_count == LISTENER_MAX_USES
        and effect.effect_turn == LISTENER_TURN
        and effect.status_enchant_id == STATUS_ENCHANT_ID
        and matches_status_rule(effect.status_enchant)
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
        and effect.trigger is None
        and not effect.once
        and effect.card_move_rule is None
    )


def matches_card(card: object) -> bool:
    if not (
        isinstance(card, Plan3Card)
        and (card.id, card.upgrade) == CARD_VERSION
        and card.plan_type == PLAN3
        and card.category == CATEGORY_MENTAL
        and card.stamina_cost == 7
        and card.force_stamina_cost == 0
        and card.cost_type == COST_STAMINA
        and card.cost_value == 0
        and card.play_trigger is None
        and card.move_position_type == MOVE_LOST
        and card.effect_group_ids == CARD_EFFECT_GROUP_IDS
        and tuple(effect.id for effect in card.effects) == ORDERED_EFFECT_IDS
        and tuple(effect.once for effect in card.effects) == (False,) * 6
    ):
        return False
    playable, draw, *rest = card.effects
    return bool(
        playable.effect_type == EFFECT_PLAYABLE_VALUE_ADD
        and playable.value1 == playable.value2 == playable.effect_turn == 0
        and playable.effect_count == 1
        and playable.trigger is None
        and _plain_effect(playable)
        and draw.effect_type == EFFECT_CARD_DRAW
        and draw.value1 == 2
        and draw.value2 == draw.effect_count == draw.effect_turn == 0
        and draw.trigger is None
        and _plain_effect(draw)
        and all(
            matches_extra_turn_effect(card.effects[slot], slot=slot)
            for slot in (2, 3, 4)
        )
        and matches_installer(rest[-1])
    )


@dataclass(frozen=True, slots=True)
class Men100016Program:
    card: Plan3Card

    def __post_init__(self) -> None:
        if not matches_card(self.card):
            raise ValueError("exact p_card-03-men-100_016#0 shape is required")

    @property
    def installer(self) -> Plan3Effect:
        return self.card.effects[5]

    @property
    def child(self) -> Plan3Effect:
        assert self.installer.status_enchant is not None
        return self.installer.status_enchant.effects[0]


def load_program(database: Path = DEFAULT_DATABASE) -> Men100016Program:
    return Men100016Program(load_plan3_card(CARD_ID, CARD_UPGRADE, database))


def signed_remaining_turn_gate(remaining_turn: int) -> bool:
    if isinstance(remaining_turn, bool) or not isinstance(remaining_turn, int):
        raise TypeError("remaining_turn must be an integer")
    if not INT32_MIN <= remaining_turn <= INT32_MAX:
        raise ValueError("remaining_turn must be an Int32")
    return remaining_turn <= REMAINING_TURN_THRESHOLD


@dataclass(frozen=True, slots=True)
class Men100016Runtime:
    turns_remaining: int
    extra_turn: int = 0
    listener_uses: tuple[int, ...] = ()
    lesson_multiple: LessonParameterMultipleState = LessonParameterMultipleState()

    def __post_init__(self) -> None:
        for label, value in (
            ("turns_remaining", self.turns_remaining),
            ("extra_turn", self.extra_turn),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        uses = tuple(self.listener_uses)
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value < LISTENER_MAX_USES
            for value in uses
        ):
            raise ValueError("listener uses must be active values from 0 through 2")
        if not isinstance(self.lesson_multiple, LessonParameterMultipleState):
            raise TypeError("lesson_multiple must be a typed multiplier state")
        object.__setattr__(self, "listener_uses", uses)


@dataclass(frozen=True, slots=True)
class Men100016CardExecution:
    before: Men100016Runtime
    after: Men100016Runtime
    fired_extra_turn_slots: tuple[int, ...]
    extra_turns_added: int
    playable_value_added: int = 1
    draw_requested: int = 2
    listener_installed: bool = True
    order: tuple[str, ...] = NATIVE_EXTRA_TURN_ORDER


def execute_card(
    program: Men100016Program,
    state: Men100016Runtime,
    *,
    preservation_change_count: int,
    concentration_change_count: int,
    full_power_change_count: int,
) -> Men100016CardExecution:
    if not isinstance(program, Men100016Program) or not matches_card(program.card):
        raise ValueError("exact program is required")
    if not isinstance(state, Men100016Runtime):
        raise TypeError("state must be Men100016Runtime")
    counters = (
        preservation_change_count,
        concentration_change_count,
        full_power_change_count,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counters):
        raise ValueError("change counters must be non-negative integers")
    fired = tuple(slot for slot, value in zip((2, 3, 4), counters) if value >= 2)
    added = len(fired)
    after = replace(
        state,
        turns_remaining=state.turns_remaining + added,
        extra_turn=state.extra_turn + added,
        listener_uses=(*state.listener_uses, 0),
    )
    return Men100016CardExecution(state, after, fired, added)


def ordinary_end_turn_scheduler(state: Men100016Runtime) -> Men100016Runtime:
    if not isinstance(state, Men100016Runtime):
        raise TypeError("state must be Men100016Runtime")
    if state.turns_remaining == 0:
        raise ValueError("cannot spend a completed exam turn")
    return replace(state, turns_remaining=state.turns_remaining - 1)


@dataclass(frozen=True, slots=True)
class Men100016FullPowerExecution:
    before: Men100016Runtime
    after: Men100016Runtime
    phase_selected_post_commit: bool
    trigger_matched: bool
    fired_listener_count: int
    order: tuple[str, ...] = NATIVE_FULL_POWER_ORDER


def execute_full_power_phase(
    program: Men100016Program,
    state: Men100016Runtime,
    *,
    post_commit_stance: str,
) -> Men100016FullPowerExecution:
    if not isinstance(program, Men100016Program) or not matches_card(program.card):
        raise ValueError("exact program is required")
    if not isinstance(state, Men100016Runtime):
        raise TypeError("state must be Men100016Runtime")
    selected = post_commit_stance == STANCE_FULL_POWER
    matched = selected and signed_remaining_turn_gate(state.turns_remaining)
    if not matched:
        return Men100016FullPowerExecution(state, state, selected, False, 0)

    multiplier = state.lesson_multiple
    surviving_uses: list[int] = []
    fired = 0
    for uses in state.listener_uses:
        fired += 1
        multiplier = install_lesson_value_multiple(
            multiplier, permil=CHILD_PERMIL, turn=CHILD_TURN
        ).state
        if uses + 1 < LISTENER_MAX_USES:
            surviving_uses.append(uses + 1)
    after = replace(
        state,
        listener_uses=tuple(surviving_uses),
        lesson_multiple=multiplier,
    )
    return Men100016FullPowerExecution(state, after, True, True, fired)
