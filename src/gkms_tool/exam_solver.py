"""Conservative audition ranking for the verified Plan 2 rule slice."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Callable, Iterable

from .auto_play_rules import auto_play_card_evaluation
from .item_rules import EquippedItemRule, resolve_item_end_turn
from .logic_engine import (
    COST_STAMINA,
    PLAN_COMMON,
    EFFECT_MOTIVATION,
    LESSON_UNKNOWN,
    LogicExamState,
    LogicTransition,
    MasterCard,
    apply_logic_card,
    complete_audition_if_forced,
)


_PASS_CARD = MasterCard(
    id="gkms_tool-exam-pass",
    upgrade=0,
    name="不出牌",
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
class AuditionCardRecommendation:
    hand_index: int
    card: MasterCard
    transition: LogicTransition
    passive_raw_carry: int
    setup_value: int
    strategic_value: int
    auto_play_evaluation: int
    completes_audition: bool
    overkill_score: int
    item_effect_ids: tuple[str, ...]
    item_enchantment_ids: tuple[str, ...]
    unsupported_rules: tuple[str, ...]
    same_turn_follow_up_card_ids: tuple[str, ...] = ()

    @property
    def fully_supported(self) -> bool:
        return self.transition.legal and not self.unsupported_rules

    def to_dict(self) -> dict[str, object]:
        return {
            "hand_index": self.hand_index,
            "card_id": self.card.id,
            "upgrade": self.card.upgrade,
            "name": self.card.name,
            "evaluation": self.card.evaluation,
            "legal": self.transition.legal,
            "fully_supported": self.fully_supported,
            "score_gain": self.transition.total_score_gain,
            "raw_score_gain": (
                self.transition.immediate_raw_score_gain
                + self.transition.end_turn_raw_score_gain
            ),
            "immediate_score_gain": self.transition.immediate_score_gain,
            "end_turn_score_gain": self.transition.end_turn_score_gain,
            "stamina_paid": self.transition.stamina_paid,
            "observed_stamina_cost": self.card.observed_stamina_cost,
            "force_stamina_paid": self.transition.force_stamina_paid,
            "block_paid": self.transition.block_paid,
            "end_turn_stamina_paid": self.transition.end_turn_stamina_paid,
            "end_turn_stamina_recovered": (
                self.transition.end_turn_stamina_recovered
            ),
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
            "passive_raw_carry": self.passive_raw_carry,
            "setup_value": self.setup_value,
            "strategic_value": self.strategic_value,
            "auto_play_evaluation": self.auto_play_evaluation,
            "completes_audition": self.completes_audition,
            "overkill_score": self.overkill_score,
            "after": asdict(self.transition.after),
            "item_effect_ids": list(self.item_effect_ids),
            "item_enchantment_ids": list(self.item_enchantment_ids),
            "unsupported_rules": list(self.unsupported_rules),
            "same_turn_follow_up_card_ids": list(
                self.same_turn_follow_up_card_ids
            ),
        }


def _motivation_gain(card: MasterCard) -> int:
    return sum(
        max(0, effect.value1)
        for effect in card.effects
        if effect.effect_type == EFFECT_MOTIVATION and not effect.trigger_id
    )


def _passive_raw_carry(
    state: LogicExamState,
    card: MasterCard,
    item: EquippedItemRule,
    lesson_type: str,
) -> tuple[int, tuple[str, ...]]:
    """Project only the card's earned persistent effects in raw score units."""

    raw_state = replace(state, score=0, score_multiplier_permille=1000)
    item_result = resolve_item_end_turn(item, raw_state, card)
    transition = apply_logic_card(
        raw_state,
        card,
        post_card_effects=item_result.post_card_effects,
        end_turn_effects=item_result.end_turn_effects,
        status_change_item_enchantments=item_result.status_change_enchantments,
        fired_item_enchantment_ids=item_result.fired_enchantment_ids,
        lesson_type=lesson_type,
    )
    unsupported = [
        *item_result.unsupported_rules,
        *transition.unsupported_rules,
    ]
    current = transition.after
    while transition.legal and current.turns_remaining > 0:
        idle_item_result = resolve_item_end_turn(item, current, _PASS_CARD)
        unsupported.extend(idle_item_result.unsupported_rules)
        idle = apply_logic_card(
            current,
            _PASS_CARD,
            post_card_effects=idle_item_result.post_card_effects,
            end_turn_effects=idle_item_result.end_turn_effects,
            fired_item_enchantment_ids=(
                idle_item_result.fired_enchantment_ids
            ),
            lesson_type=lesson_type,
        )
        unsupported.extend(idle.unsupported_rules)
        if not idle.legal:
            break
        current = idle.after
    return current.score, tuple(dict.fromkeys(unsupported))


def _setup_value(
    state: LogicExamState,
    card: MasterCard,
    transition: LogicTransition,
) -> int:
    future_turns = max(0, state.turns_remaining - 1)
    if future_turns == 0:
        # Block, motivation and deck thinning have no setup value after the
        # final turn.  Keeping them in the heuristic can make a zero-score
        # card beat a card that raises the actual final score.
        return 0
    motivation_weight = (
        max(1, future_turns * 2) if state.turns_remaining >= 3 else 0
    )
    gained_motivation = _motivation_gain(card)
    gained_block = max(0, transition.after.block - state.block)
    deck_thinning = (
        2
        if future_turns >= 2
        and card.move_position_type == "ProduceCardMovePositionType_Lost"
        else 0
    )
    added_trouble = max(
        0, transition.after.trouble_card_count - state.trouble_card_count
    )
    trouble_draw_risk = added_trouble * max(4, future_turns * 2)
    return (
        gained_motivation * motivation_weight
        + gained_block
        + deck_thinning
        - trouble_draw_risk
    )


def _best_same_turn_follow_up(
    state: LogicExamState,
    cards: tuple[MasterCard, ...],
    item: EquippedItemRule,
    timing_evaluator: Callable[[str, int], int],
    lesson_type: str,
) -> tuple[int, tuple[str, ...]]:
    """Choose a fully supported continuation before the current turn ends.

    Cards that add another play must be valued together with the card they
    enable.  The returned value stays in the solver's conservative raw-score
    utility space, and never assumes a future draw.
    """

    best_key: tuple[int, int, int] | None = None
    best_value = 0
    best_plan: tuple[str, ...] = ()
    terminal_turn = state.turns_remaining == 1
    for index, card in enumerate(cards):
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
        passive_raw, carry_unsupported = _passive_raw_carry(
            state, card, item, lesson_type
        )
        unsupported = tuple(
            dict.fromkeys(
                (
                    *item_result.unsupported_rules,
                    *transition.unsupported_rules,
                    *carry_unsupported,
                )
            )
        )
        if not transition.legal or unsupported:
            continue

        value = (
            transition.total_score_gain
            if terminal_turn
            else passive_raw + _setup_value(state, card, transition)
        )
        plan = (f"{card.id}@{card.upgrade}",)
        if not transition.turn_ended and len(cards) > 1:
            remaining = cards[:index] + cards[index + 1 :]
            continuation_value, continuation_plan = _best_same_turn_follow_up(
                transition.after,
                remaining,
                item,
                timing_evaluator,
                lesson_type,
            )
            if continuation_plan:
                deck_change_value = (
                    2
                    if state.turns_remaining >= 3
                    and card.move_position_type
                    == "ProduceCardMovePositionType_Lost"
                    else 0
                )
                added_trouble = max(
                    0,
                    transition.after.trouble_card_count
                    - state.trouble_card_count,
                )
                deck_change_value -= added_trouble * max(
                    4, (state.turns_remaining - 1) * 2
                )
                chain_value = (
                    transition.total_score_gain + continuation_value
                    if terminal_turn
                    else transition.immediate_raw_score_gain
                    + continuation_value
                    + deck_change_value
                )
                if chain_value >= value:
                    value = chain_value
                    plan += continuation_plan

        official_timing = timing_evaluator(card.id, state.turns_remaining)
        key = (
            (value, transition.after.stamina, official_timing)
            if terminal_turn
            else (official_timing, value, transition.after.stamina)
        )
        if best_key is None or key > best_key:
            best_key = key
            best_value = value
            best_plan = plan
    return best_value, best_plan


def recommend_audition_hand(
    state: LogicExamState,
    cards: Iterable[MasterCard],
    item: EquippedItemRule,
    *,
    timing_evaluator: Callable[[str, int], int] = auto_play_card_evaluation,
    force_end_score: int = 0,
    lesson_type: str = LESSON_UNKNOWN,
) -> tuple[AuditionCardRecommendation, ...]:
    """Rank a visible hand without pretending the unseen deck is known.

    The current-turn transition is exact for supported effects.  The longer
    signal is intentionally conservative: existing good impression naturally
    decays with no future draw assumptions.  Early motivation receives a small
    setup value because it increases block gained by later block cards.
    """

    recommendations: list[AuditionCardRecommendation] = []
    visible_cards = tuple(cards)
    terminal_turn = state.turns_remaining == 1
    for hand_index, card in enumerate(visible_cards):
        item_result = resolve_item_end_turn(item, state, card)
        uncapped_transition = apply_logic_card(
            state,
            card,
            post_card_effects=item_result.post_card_effects,
            end_turn_effects=item_result.end_turn_effects,
            status_change_item_enchantments=item_result.status_change_enchantments,
            fired_item_enchantment_ids=item_result.fired_enchantment_ids,
            lesson_type=lesson_type,
        )
        transition = uncapped_transition
        overkill_score = 0
        if force_end_score > 0:
            overkill_score = max(
                0,
                uncapped_transition.after.score - force_end_score,
            )
            transition = complete_audition_if_forced(
                transition,
                force_end_score,
            )
        completes_audition = (
            force_end_score > 0
            and transition.after.turns_remaining == 0
            and transition.after.score >= force_end_score
        )
        passive_raw, carry_unsupported = _passive_raw_carry(
            state, card, item, lesson_type
        )
        setup_value = _setup_value(state, card, transition)
        if completes_audition:
            setup_value = 0
        strategic_value = (
            transition.total_score_gain
            if completes_audition or terminal_turn
            else passive_raw + setup_value
        )
        follow_up_card_ids: tuple[str, ...] = ()
        if (
            force_end_score <= 0
            and transition.legal
            and not transition.unsupported_rules
            and not transition.turn_ended
            and len(visible_cards) > 1
        ):
            remaining_cards = (
                visible_cards[:hand_index] + visible_cards[hand_index + 1 :]
            )
            continuation_value, continuation_plan = _best_same_turn_follow_up(
                transition.after,
                remaining_cards,
                item,
                timing_evaluator,
                lesson_type,
            )
            if continuation_plan:
                deck_change_value = (
                    2
                    if state.turns_remaining >= 3
                    and card.move_position_type
                    == "ProduceCardMovePositionType_Lost"
                    else 0
                )
                added_trouble = max(
                    0,
                    transition.after.trouble_card_count
                    - state.trouble_card_count,
                )
                deck_change_value -= added_trouble * max(
                    4, (state.turns_remaining - 1) * 2
                )
                chain_value = (
                    transition.total_score_gain + continuation_value
                    if terminal_turn
                    else transition.immediate_raw_score_gain
                    + continuation_value
                    + deck_change_value
                )
                if chain_value >= strategic_value:
                    strategic_value = chain_value
                    follow_up_card_ids = continuation_plan
        official_timing = timing_evaluator(card.id, state.turns_remaining)
        unsupported = tuple(
            dict.fromkeys(
                (
                    *item_result.unsupported_rules,
                    *transition.unsupported_rules,
                    *carry_unsupported,
                )
            )
        )
        recommendations.append(
            AuditionCardRecommendation(
                hand_index=hand_index,
                card=card,
                transition=transition,
                passive_raw_carry=passive_raw,
                setup_value=setup_value,
                strategic_value=strategic_value,
                auto_play_evaluation=official_timing,
                completes_audition=completes_audition,
                overkill_score=overkill_score,
                item_effect_ids=tuple(
                    effect.id for effect in item_result.effects
                ),
                item_enchantment_ids=item_result.fired_enchantment_ids,
                unsupported_rules=unsupported,
                same_turn_follow_up_card_ids=follow_up_card_ids,
            )
        )

    if terminal_turn:
        # There is no later draw on the final turn.  Rank the exact supported
        # score (including a visible same-turn continuation) before the game's
        # generic auto-play timing table.  The table remains a tie-breaker.
        key = lambda result: (  # noqa: E731 - compact local sort policy
            result.fully_supported,
            result.transition.legal,
            result.completes_audition,
            result.strategic_value,
            -result.overkill_score,
            result.transition.total_score_gain,
            result.transition.after.stamina,
            result.auto_play_evaluation,
        )
    else:
        key = lambda result: (  # noqa: E731 - compact local sort policy
            result.fully_supported,
            result.transition.legal,
            result.completes_audition,
            result.auto_play_evaluation >= 0,
            (
                result.transition.after.stamina
                if result.completes_audition
                else -1
            ),
            -result.overkill_score,
            result.strategic_value,
            result.auto_play_evaluation,
            result.transition.total_score_gain,
            result.transition.after.stamina,
        )
    return tuple(sorted(recommendations, key=key, reverse=True))
