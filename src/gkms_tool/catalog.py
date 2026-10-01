from __future__ import annotations

from .models import CardDefinition


# Exact upgrade-0 values from gakumasu-diff/ProduceCard.yaml (2026-07-21).
# This intentionally small bootstrap catalog covers the Plan1 REGULAR initial
# deck plus the sample idol card.  A generated full catalog will replace it.
BOOTSTRAP_CARDS: tuple[CardDefinition, ...] = (
    CardDefinition(
        id="p_card-00-act-0_001",
        name="アピールの基本",
        stamina_cost=4,
        parameter=9,
        source_note="參數 +9",
    ),
    CardDefinition(
        id="p_card-00-act-0_002",
        name="ポーズの基本",
        stamina_cost=3,
        parameter=2,
        block=2,
        source_note="參數 +2、元氣 +2",
    ),
    CardDefinition(
        id="p_card-00-men-0_003",
        name="表現の基本",
        stamina_cost=0,
        block=4,
        once_per_exam=True,
        source_note="元氣 +4；考試中一次",
    ),
    CardDefinition(
        id="p_card-01-men-0_007",
        name="振る舞いの基本",
        stamina_cost=1,
        block=1,
        favorable_turns=2,
        source_note="元氣 +1、好調 2 回合",
    ),
    CardDefinition(
        id="p_card-01-act-0_005",
        name="挑戦",
        stamina_cost=7,
        parameter=25,
        requires_favorable=True,
        once_per_exam=True,
        source_note="好調時可用；參數 +25；考試中一次",
    ),
    CardDefinition(
        id="p_card-01-ido-1_013",
        name="リトル・プリンス",
        stamina_cost=3,
        parameter=8,
        conditional_parameter=3,
        once_per_exam=True,
        source_note="參數 +8；好調時再 +3；考試中一次",
    ),
)

CARD_BY_ID = {card.id: card for card in BOOTSTRAP_CARDS}

# Plan1 initial deck for produce-001 / ExamParameterBuff.
INITIAL_DECK_IDS: tuple[str, ...] = (
    "p_card-00-act-0_001",
    "p_card-00-act-0_001",
    "p_card-00-act-0_002",
    "p_card-00-men-0_003",
    "p_card-00-men-0_003",
    "p_card-01-men-0_007",
    "p_card-01-men-0_007",
    "p_card-01-act-0_005",
)


def card_choices() -> tuple[CardDefinition, ...]:
    return BOOTSTRAP_CARDS


def get_card(card_id: str) -> CardDefinition:
    try:
        return CARD_BY_ID[card_id]
    except KeyError as exc:
        raise KeyError(f"啟動資料尚未收錄卡牌：{card_id}") from exc

