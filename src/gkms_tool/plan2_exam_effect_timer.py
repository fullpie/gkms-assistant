"""Exact Plan2/Common admission for the proven ``ExamEffectTimer`` rows.

The relative-turn scheduler remains owned by :mod:`gkms_tool.plan3_engine`.
This module only names the six Master rows whose direct child is already an
executable Plan2/Common primitive.  Every other timer or child shape stops at
this boundary instead of inheriting the broader Plan3 timer catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    EFFECT_CARD_DRAW,
    EFFECT_CARD_UPGRADE,
    EFFECT_TIMER,
    Plan3Effect,
    load_plan3_effect,
)


DRAW_ONE_EFFECT_ID = "e_effect-exam_card_draw-0001"
DRAW_TWO_EFFECT_ID = "e_effect-exam_card_draw-0002"
HAND_ALL_UPGRADE_EFFECT_ID = (
    "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"
)

TIMER_DRAW_ONE_DELAY_1 = (
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001"
)
TIMER_DRAW_ONE_DELAY_2 = (
    "e_effect-exam_effect_timer-0002-01-e_effect-exam_card_draw-0001"
)
TIMER_DRAW_ONE_DELAY_3 = (
    "e_effect-exam_effect_timer-0003-01-e_effect-exam_card_draw-0001"
)
TIMER_DRAW_TWO_DELAY_1 = (
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0002"
)
TIMER_HAND_ALL_UPGRADE_DELAY_1 = (
    "e_effect-exam_effect_timer-0001-01-"
    "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"
)
TIMER_HAND_ALL_UPGRADE_DELAY_2 = (
    "e_effect-exam_effect_timer-0002-01-"
    "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"
)

DRAW_ONE_TIMER_EFFECT_IDS = frozenset(
    {
        TIMER_DRAW_ONE_DELAY_1,
        TIMER_DRAW_ONE_DELAY_2,
        TIMER_DRAW_ONE_DELAY_3,
    }
)
DRAW_TWO_TIMER_EFFECT_IDS = frozenset({TIMER_DRAW_TWO_DELAY_1})
HAND_ALL_UPGRADE_TIMER_EFFECT_IDS = frozenset(
    {
        TIMER_HAND_ALL_UPGRADE_DELAY_1,
        TIMER_HAND_ALL_UPGRADE_DELAY_2,
    }
)
TARGET_EFFECT_IDS = frozenset(
    {
        *DRAW_ONE_TIMER_EFFECT_IDS,
        *DRAW_TWO_TIMER_EFFECT_IDS,
        *HAND_ALL_UPGRADE_TIMER_EFFECT_IDS,
    }
)

_EXPECTED_ROWS = {
    TIMER_DRAW_ONE_DELAY_1: (1, DRAW_ONE_EFFECT_ID, EFFECT_CARD_DRAW, 1),
    TIMER_DRAW_ONE_DELAY_2: (2, DRAW_ONE_EFFECT_ID, EFFECT_CARD_DRAW, 1),
    TIMER_DRAW_ONE_DELAY_3: (3, DRAW_ONE_EFFECT_ID, EFFECT_CARD_DRAW, 1),
    TIMER_DRAW_TWO_DELAY_1: (1, DRAW_TWO_EFFECT_ID, EFFECT_CARD_DRAW, 2),
    TIMER_HAND_ALL_UPGRADE_DELAY_1: (
        1,
        HAND_ALL_UPGRADE_EFFECT_ID,
        EFFECT_CARD_UPGRADE,
        0,
    ),
    TIMER_HAND_ALL_UPGRADE_DELAY_2: (
        2,
        HAND_ALL_UPGRADE_EFFECT_ID,
        EFFECT_CARD_UPGRADE,
        0,
    ),
}


class Plan2ExamEffectTimerContractError(ValueError):
    """Stable fail-closed error for an unproven timer or child shape."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectTimerContract:
    effect: Plan3Effect
    delay: int
    child_effect_id: str
    child_effect_type: str
    child_value: int

    @property
    def effect_id(self) -> str:
        return self.effect.id


def resolve_plan2_exam_effect_timer(
    effect: str | Plan3Effect,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ExamEffectTimerContract:
    """Resolve only the six exact Master timer/child chains."""

    if isinstance(effect, str):
        effect_id = effect
        if effect_id not in TARGET_EFFECT_IDS:
            raise Plan2ExamEffectTimerContractError(
                "unintegrated-exam-effect-timer-row", effect_id
            )
        try:
            row = load_plan3_effect(effect_id, database)
        except KeyError as error:
            raise Plan2ExamEffectTimerContractError(
                "exam-effect-timer-master-missing", effect_id
            ) from error
    elif isinstance(effect, Plan3Effect):
        row = effect
        effect_id = row.id
        if effect_id not in TARGET_EFFECT_IDS:
            raise Plan2ExamEffectTimerContractError(
                "unintegrated-exam-effect-timer-row", effect_id
            )
    else:
        raise TypeError("effect must be an effect ID or Plan3Effect")

    delay, child_id, child_type, child_value = _EXPECTED_ROWS[effect_id]
    if not (
        row.effect_type == EFFECT_TIMER
        and row.value1 == delay
        and row.value2 == 0
        and row.effect_count == 1
        and row.effect_turn == 0
        and not row.status_enchant_id
        and row.status_enchant is None
        and row.chain_effect_id == child_id
        and not row.chain_effect_ids
        and row.chain_effect is not None
        and row.chain_effect.id == child_id
        and row.chain_effect.effect_type == child_type
        and row.chain_effect.value1 == child_value
        and row.chain_effect.value2 == 0
        and row.chain_effect.effect_count == 0
        and row.chain_effect.effect_turn == 0
        and not row.chain_effect.status_enchant_id
        and row.chain_effect.status_enchant is None
        and not row.chain_effect.chain_effect_id
        and not row.chain_effect.chain_effect_ids
        and row.chain_effect.chain_effect is None
        and row.chain_effect.trigger is None
        and not row.chain_effect.once
        and row.chain_effect.card_move_rule is None
    ):
        raise Plan2ExamEffectTimerContractError(
            "unintegrated-exam-effect-timer-shape", effect_id
        )
    return Plan2ExamEffectTimerContract(
        row,
        delay,
        child_id,
        child_type,
        child_value,
    )


def load_plan2_exam_effect_timer_catalog(
    *, database: Path = DEFAULT_DATABASE
) -> tuple[Plan2ExamEffectTimerContract, ...]:
    """Load all six exact rows in deterministic delay/child order."""

    return tuple(
        resolve_plan2_exam_effect_timer(effect_id, database=database)
        for effect_id in (
            TIMER_DRAW_ONE_DELAY_1,
            TIMER_DRAW_ONE_DELAY_2,
            TIMER_DRAW_ONE_DELAY_3,
            TIMER_DRAW_TWO_DELAY_1,
            TIMER_HAND_ALL_UPGRADE_DELAY_1,
            TIMER_HAND_ALL_UPGRADE_DELAY_2,
        )
    )


__all__ = [
    "DRAW_ONE_EFFECT_ID",
    "DRAW_ONE_TIMER_EFFECT_IDS",
    "DRAW_TWO_EFFECT_ID",
    "DRAW_TWO_TIMER_EFFECT_IDS",
    "HAND_ALL_UPGRADE_EFFECT_ID",
    "HAND_ALL_UPGRADE_TIMER_EFFECT_IDS",
    "Plan2ExamEffectTimerContract",
    "Plan2ExamEffectTimerContractError",
    "TARGET_EFFECT_IDS",
    "TIMER_DRAW_ONE_DELAY_1",
    "TIMER_DRAW_ONE_DELAY_2",
    "TIMER_DRAW_ONE_DELAY_3",
    "TIMER_DRAW_TWO_DELAY_1",
    "TIMER_HAND_ALL_UPGRADE_DELAY_1",
    "TIMER_HAND_ALL_UPGRADE_DELAY_2",
    "load_plan2_exam_effect_timer_catalog",
    "resolve_plan2_exam_effect_timer",
]
