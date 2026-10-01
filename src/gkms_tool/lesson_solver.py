"""Conservative, explainable ranking for the verified Plan 2 rule slice."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable, Iterable

from .auto_play_rules import auto_play_card_evaluation
from .item_rules import (
    FIELD_MOTIVATION_UP,
    PHASE_CARD_PLAY_AFTER,
    EquippedItemRule,
    resolve_item_end_turn,
)
from .logic_engine import (
    COST_STAMINA,
    EFFECT_LESSON_BY_MOTIVATION,
    EFFECT_MOTIVATION,
    PLAN_COMMON,
    LogicExamState,
    LogicTransition,
    MasterCard,
    apply_logic_card,
    apply_logic_turn_skip,
)
from .lesson_gimmick import (
    LessonGimmickProfile,
    load_lesson_gimmick_profile,
    resolve_lesson_turn_start,
)


_PASS_CARD = MasterCard(
    id="gkms_tool-pass",
    upgrade=0,
    name="後續無新增效果",
    plan_type=PLAN_COMMON,
    category="",
    stamina_cost=0,
    cost_type=COST_STAMINA,
    cost_value=0,
    play_trigger_id="",
    move_position_type="ProduceCardMovePositionType_Grave",
    effects=(),
)


@dataclass(frozen=True, slots=True)
class LessonCardRecommendation:
    hand_index: int
    card: MasterCard
    transition: LogicTransition
    current_item_effect_ids: tuple[str, ...]
    current_item_enchantment_ids: tuple[str, ...]
    carry_score_gain: int
    carry_end_state: LogicExamState
    unsupported_rules: tuple[str, ...]
    setup_value: int = 0
    strategic_value: int = 0
    auto_play_evaluation: int = 0
    completes_lesson: bool = False
    clear_remaining_after: int = 0

    @property
    def is_turn_skip(self) -> bool:
        return self.hand_index < 0

    @property
    def fully_supported(self) -> bool:
        return self.transition.legal and not self.unsupported_rules

    def to_dict(self) -> dict[str, object]:
        return {
            "hand_index": self.hand_index,
            "card_id": self.card.id,
            "upgrade": self.card.upgrade,
            "name": self.card.name,
            "legal": self.transition.legal,
            "fully_supported": self.fully_supported,
            "score_gain": self.transition.total_score_gain,
            "stamina_paid": self.transition.stamina_paid,
            "force_stamina_paid": self.transition.force_stamina_paid,
            "good_impression_paid": self.transition.good_impression_paid,
            "motivation_paid": self.transition.motivation_paid,
            "block_paid": self.transition.block_paid,
            "end_turn_stamina_paid": self.transition.end_turn_stamina_paid,
            "turn_ended": self.transition.turn_ended,
            "runtime_status_enchant_ids": list(
                self.transition.runtime_status_enchant_ids
            ),
            "runtime_status_effect_ids": list(
                self.transition.runtime_status_effect_ids
            ),
            "runtime_status_added_ids": list(
                self.transition.runtime_status_added_ids
            ),
            "runtime_status_draw_count": (
                self.transition.runtime_status_draw_count
            ),
            "carry_score_gain": self.carry_score_gain,
            "setup_value": self.setup_value,
            "strategic_value": self.strategic_value,
            "auto_play_evaluation": self.auto_play_evaluation,
            "completes_lesson": self.completes_lesson,
            "clear_remaining_after": self.clear_remaining_after,
            "after": asdict(self.transition.after),
            "carry_end_state": asdict(self.carry_end_state),
            "item_effect_ids": list(self.current_item_effect_ids),
            "item_enchantment_ids": list(self.current_item_enchantment_ids),
            "unsupported_rules": list(self.unsupported_rules),
        }


def _carry_without_future_cards_or_items(
    transition: LogicTransition,
    gimmick_profile: LessonGimmickProfile | None = None,
    lesson_type: str = "ProduceStepLessonType_Unknown",
) -> tuple[int, LogicExamState, tuple[str, ...]]:
    """Project only already-earned persistent effects through the lesson.

    Future hand effects and future item activations are intentionally omitted.
    This is a stable comparison signal, not a claim of full-run optimal play.
    """

    start_score = transition.before.score
    current = transition.after
    unsupported: list[str] = []
    at_turn_start = transition.turn_ended
    while current.turns_remaining > 0:
        if at_turn_start and gimmick_profile is not None:
            gimmick = resolve_lesson_turn_start(gimmick_profile, current)
            current = gimmick.after
            unsupported.extend(gimmick.unsupported_rules)
        idle = apply_logic_card(current, _PASS_CARD, lesson_type=lesson_type)
        unsupported.extend(idle.unsupported_rules)
        if not idle.legal:
            break
        current = idle.after
        at_turn_start = idle.turn_ended
    return current.score - start_score, current, tuple(unsupported)


def _motivation_setup_value(
    state: LogicExamState,
    card: MasterCard,
    transition: LogicTransition,
) -> int:
    """Value early やる気/元気 setup without spending the late burst card."""

    if state.turns_remaining < 3:
        return 0
    future_turns = state.turns_remaining - 1
    gained_motivation = sum(
        max(0, effect.value1)
        for effect in card.effects
        if effect.effect_type == EFFECT_MOTIVATION
    )
    gained_block = max(0, transition.after.block - state.block)
    extra_play_value = 4 if not transition.turn_ended else 0
    existing_enchants = {enchant.id for enchant in state.active_status_enchants}
    expected_skill_plays = future_turns + (1 if not transition.turn_ended else 0)
    persistent_score = 0
    for enchant in transition.after.active_status_enchants:
        if enchant.id in existing_enchants:
            continue
        prefix = "e_trigger-exam_play_count_interval-"
        if not enchant.trigger_id.startswith(prefix):
            continue
        try:
            interval = int(enchant.trigger_id.removeprefix(prefix))
        except ValueError:
            continue
        if interval < 1:
            continue
        persistent_score += (
            expected_skill_plays // interval
            * sum(max(0, effect.value1) for effect in enchant.effects)
        )
    deck_thinning = (
        2
        if future_turns >= 2
        and card.move_position_type == "ProduceCardMovePositionType_Lost"
        else 0
    )
    return (
        gained_motivation * max(1, future_turns * 2)
        + gained_block
        + extra_play_value
        + persistent_score
        + deck_thinning
    )


def _item_readiness_setup_value(
    state: LogicExamState,
    transition: LogicTransition,
    item: EquippedItemRule,
    fired_enchantment_ids: tuple[str, ...],
) -> int:
    """Value crossing a verified after-card item threshold.

    Future hand order is unknown, so this counts at most one later activation.
    That is enough to distinguish a real item unlock from a small immediate
    score while remaining deliberately conservative.
    """

    if not transition.legal or state.turns_remaining < 2:
        return 0
    already_fired = set(fired_enchantment_ids)
    used = dict(state.item_enchantment_uses)
    value = 0
    for enchantment in item.enchantments:
        trigger = enchantment.trigger
        if enchantment.id in already_fired:
            continue
        if (
            trigger.phase_types != (PHASE_CARD_PLAY_AFTER,)
            or trigger.field_types != (FIELD_MOTIVATION_UP,)
            or len(trigger.field_values) != 1
        ):
            continue
        threshold = trigger.field_values[0]
        if not state.motivation < threshold <= transition.after.motivation:
            continue
        if (
            enchantment.max_uses > 0
            and used.get(enchantment.id, 0) >= enchantment.max_uses
        ):
            continue
        for effect in enchantment.effects:
            if effect.effect_type != EFFECT_LESSON_BY_MOTIVATION:
                continue
            repetitions = max(1, effect.effect_count)
            value += (
                (transition.after.motivation * effect.value1 + 999) // 1000
            ) * repetitions
    return value


def recommend_logic_hand(
    state: LogicExamState,
    cards: Iterable[MasterCard],
    item: EquippedItemRule,
    *,
    clear_target: int = 0,
    gimmick_group_id: str | None = None,
    gimmick_profile: LessonGimmickProfile | None = None,
    lesson_type: str = "ProduceStepLessonType_Unknown",
    timing_evaluator: Callable[[str, int], int] = auto_play_card_evaluation,
) -> tuple[LessonCardRecommendation, ...]:
    """Rank cards by verified current effects and conservative carry score.

    ``state`` is the already-observed card-selection state; callers must not
    reapply its current turn-start gimmick.  A supplied profile is applied
    exactly once at each *future* turn boundary in the carry projection.
    """

    gimmick_load_unsupported: tuple[str, ...] = ()
    if gimmick_profile is None and gimmick_group_id:
        try:
            gimmick_profile = load_lesson_gimmick_profile(gimmick_group_id)
        except (KeyError, ValueError, FileNotFoundError) as error:
            gimmick_load_unsupported = (f"gimmick-profile:{error}",)

    recommendations: list[LessonCardRecommendation] = []
    for hand_index, card in enumerate(cards):
        item_result = resolve_item_end_turn(item, state, card)
        transition = apply_logic_card(
            state,
            card,
            post_card_effects=item_result.post_card_effects,
            end_turn_effects=item_result.end_turn_effects,
            status_change_item_enchantments=item_result.status_change_enchantments,
            fired_item_enchantment_ids=item_result.fired_enchantment_ids,
            lesson_type=lesson_type,
        )
        carry_score, carry_end, carry_unsupported = (
            _carry_without_future_cards_or_items(
                transition, gimmick_profile, lesson_type
            )
            if transition.legal
            else (0, state, ())
        )
        unsupported = tuple(
            dict.fromkeys(
                (
                    *item_result.unsupported_rules,
                    *transition.unsupported_rules,
                    *carry_unsupported,
                    *gimmick_load_unsupported,
                )
            )
        )
        setup_value = _motivation_setup_value(state, card, transition)
        setup_value += _item_readiness_setup_value(
            state,
            transition,
            item,
            item_result.fired_enchantment_ids,
        )
        completes_lesson = (
            clear_target > 0 and transition.after.score >= clear_target
        )
        if completes_lesson:
            setup_value = 0
        clear_remaining_after = (
            max(0, clear_target - transition.after.score)
            if clear_target > 0
            else 0
        )
        urgency_weight = max(0, 4 - state.turns_remaining)
        urgent_progress = transition.total_score_gain * urgency_weight
        strategic_value = carry_score + setup_value + urgent_progress
        official_timing = timing_evaluator(card.id, state.turns_remaining)
        recommendations.append(
            LessonCardRecommendation(
                hand_index=hand_index,
                card=card,
                transition=transition,
                current_item_effect_ids=tuple(
                    effect.id for effect in item_result.effects
                ),
                current_item_enchantment_ids=(
                    item_result.fired_enchantment_ids
                ),
                carry_score_gain=carry_score,
                carry_end_state=carry_end,
                unsupported_rules=unsupported,
                setup_value=setup_value,
                strategic_value=strategic_value,
                auto_play_evaluation=official_timing,
                completes_lesson=completes_lesson,
                clear_remaining_after=clear_remaining_after,
            )
        )

    # Manual TurnEnd is a first-class action.  It may be selected even with a
    # usable hand, but only after the item's end-turn resolver has supplied a
    # complete effect list and the state carries a verified max-stamina cap.
    skip_card = MasterCard(
        id="gkms_tool-turn-skip", upgrade=0, name="SKIP", plan_type=PLAN_COMMON,
        category="", stamina_cost=0, cost_type=COST_STAMINA, cost_value=0,
        play_trigger_id="", move_position_type="ProduceCardMovePositionType_Unknown",
        effects=(),
    )
    skip_item = resolve_item_end_turn(item, state, skip_card)
    skip = apply_logic_turn_skip(
        state, end_turn_effects=skip_item.end_turn_effects, lesson_type=lesson_type
    )
    skip_unsupported = tuple(dict.fromkeys(
        (*skip_item.unsupported_rules, *skip.unsupported_rules)
    ))
    skip_carry, skip_end, skip_carry_unsupported = (
        _carry_without_future_cards_or_items(skip, gimmick_profile, lesson_type)
        if skip.legal else (0, state, ())
    )
    skip_unsupported = tuple(dict.fromkeys((*skip_unsupported, *skip_carry_unsupported)))
    recommendations.append(LessonCardRecommendation(
        hand_index=-1, card=skip_card, transition=skip,
        current_item_effect_ids=tuple(effect.id for effect in skip_item.effects),
        current_item_enchantment_ids=skip_item.fired_enchantment_ids,
        carry_score_gain=skip_carry, carry_end_state=skip_end,
        unsupported_rules=skip_unsupported, strategic_value=skip_carry,
        auto_play_evaluation=-999,
        clear_remaining_after=(max(0, clear_target - skip.after.score) if clear_target else 0),
    ))

    return tuple(
        sorted(
            recommendations,
            key=lambda result: (
                result.fully_supported,
                result.transition.legal,
                result.completes_lesson,
                result.auto_play_evaluation >= 0,
                result.strategic_value,
                result.auto_play_evaluation,
                -result.clear_remaining_after,
                result.carry_score_gain,
                result.transition.after.motivation,
                result.transition.after.block,
                result.transition.after.stamina,
            ),
            reverse=True,
        )
    )
