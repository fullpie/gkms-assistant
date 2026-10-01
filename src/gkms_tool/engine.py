from __future__ import annotations

from dataclasses import replace
from math import floor
from typing import Iterable

from .models import (
    CardDefinition,
    ExamState,
    ExamTransition,
    Recommendation,
    RunRecommendation,
    RunState,
)


def apply_card(state: ExamState, card: CardDefinition) -> ExamTransition:
    """Apply the verified bootstrap effects for one card.

    This is deliberately strict: future unknown effects must be surfaced in
    ``unsupported_effects`` instead of being treated as a silent no-op.
    """

    state.validate()
    if card.requires_favorable and state.favorable_turns <= 0:
        return ExamTransition(
            before=state,
            after=state,
            card=card,
            score_gain=0,
            stamina_paid=0,
            block_paid=0,
            legal=False,
        )

    block_paid = min(state.block, card.stamina_cost)
    stamina_paid = card.stamina_cost - block_paid
    if stamina_paid > state.stamina:
        return ExamTransition(
            before=state,
            after=state,
            card=card,
            score_gain=0,
            stamina_paid=0,
            block_paid=0,
            legal=False,
        )

    raw_parameter = card.parameter
    if state.favorable_turns > 0:
        raw_parameter += card.conditional_parameter
        score_gain = floor(raw_parameter * 1.5)
    else:
        score_gain = raw_parameter

    after = replace(
        state,
        turns_remaining=max(0, state.turns_remaining - 1),
        stamina=state.stamina - stamina_paid,
        block=state.block - block_paid + card.block,
        favorable_turns=max(0, state.favorable_turns - 1) + card.favorable_turns,
        score=state.score + score_gain,
    )
    return ExamTransition(
        before=state,
        after=after,
        card=card,
        score_gain=score_gain,
        stamina_paid=stamina_paid,
        block_paid=block_paid,
        legal=True,
    )


def _utility(transition: ExamTransition) -> float:
    if not transition.legal:
        return float("-inf")
    after = transition.after
    card = transition.card
    survival_value = card.block * 0.45 - transition.stamina_paid * 0.30
    future_favorable = card.favorable_turns * min(after.turns_remaining, 3) * 2.5
    conservation = 1.0 if transition.stamina_paid == 0 else 0.0
    return transition.score_gain + survival_value + future_favorable + conservation


def recommend_cards(
    state: ExamState, cards: Iterable[CardDefinition]
) -> list[Recommendation]:
    recommendations: list[Recommendation] = []
    for card in cards:
        transition = apply_card(state, card)
        reasons: list[str] = []
        if not transition.legal:
            if card.requires_favorable and state.favorable_turns <= 0:
                reasons.append("目前沒有好調，這張牌不能使用。")
            else:
                reasons.append("體力與元氣不足，這張牌不能使用。")
        else:
            if transition.score_gain:
                reasons.append(f"立即增加 {transition.score_gain} 分。")
            if card.block:
                reasons.append(f"獲得 {card.block} 元氣，降低後續體力風險。")
            if card.favorable_turns:
                reasons.append(f"建立 {card.favorable_turns} 回合好調，提升後續輸出。")
            if transition.stamina_paid:
                reasons.append(f"實際消耗 {transition.stamina_paid} 體力。")
            elif card.stamina_cost:
                reasons.append("成本完全由目前元氣吸收。")
            if card.once_per_exam:
                reasons.append("此牌為考試中一次；初版尚未追蹤是否已使用。")
        if transition.unsupported_effects:
            reasons.append("尚未支援：" + "、".join(transition.unsupported_effects))
        recommendations.append(
            Recommendation(
                card=card,
                transition=transition,
                utility=_utility(transition),
                reasons=tuple(reasons),
            )
        )
    return sorted(recommendations, key=lambda item: item.utility, reverse=True)


def recommend_run_actions(state: RunState) -> list[RunRecommendation]:
    """Return transparent MVP heuristics for the 13-step run tracker."""

    state.validate()
    stamina_ratio = state.stamina / state.max_stamina
    stats = {"Vocal": state.vocal, "Dance": state.dance, "Visual": state.visual}
    growth_rates = {
        "Vocal": state.vocal_growth,
        "Dance": state.dance_growth,
        "Visual": state.visual_growth,
    }
    actions: list[RunRecommendation] = []

    if stamina_ratio <= 0.35:
        actions.append(
            RunRecommendation(
                action="休息／回復體力",
                utility=120.0 + (0.35 - stamina_ratio) * 100,
                reasons=(
                    f"目前體力只有 {state.stamina}/{state.max_stamina}。",
                    "先避免後續課程失敗；精確回復量會在事件規則接入後替換。",
                ),
            )
        )

    average = sum(stats.values()) / 3
    for label, current in stats.items():
        growth = growth_rates[label]
        deficit = max(0.0, average - current)
        utility = growth / 10 + deficit * 0.45 + stamina_ratio * 10
        actions.append(
            RunRecommendation(
                action=f"{label} 課程",
                utility=utility,
                reasons=(
                    f"目前 {label} 為 {current}，角色成長率為 {growth}‰。",
                    "這是流程骨架的方向分；尚未加入支援卡、SP 課程與當格事件。",
                ),
            )
        )

    actions.append(
        RunRecommendation(
            action="外出／其他事件",
            utility=18.0 + (1.0 - stamina_ratio) * 20,
            reasons=(
                "保留作為畫面出現特殊選項時的候選。",
                "事件成功率與實際結果尚待 Master event executor 接入。",
            ),
        )
    )
    return sorted(actions, key=lambda item: item.utility, reverse=True)
