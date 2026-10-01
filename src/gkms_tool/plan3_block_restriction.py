"""Exact native contract for Plan 3/Common BlockRestriction effects.

Android v3.2.3 maps ProduceExamEffectType 18 to
``BlockRestrictionEffectExecutor``.  The executor reads only ``EffectTurn``;
its add path is first offered to the AntiDebuff gate, then either merges the
turn count into the active same-type status or installs a fresh status.  A
successful add emits a before/after status difference.  Unknown rows and
contexts fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .plan3_anti_debuff import (
    AntiDebuffBlockResult,
    AntiDebuffRuntime,
    ExamStatusEffectTargetType,
    try_block_status_addition,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamBlockRestriction"
EFFECT_TYPE_VALUE = 18
EFFECT_GROUP_ID = "effect_group-visible-exam_block_restriction-000"
DIRECT_EFFECT_ID = "e_effect-exam_block_restriction-02"
GIMMICK_EFFECT_ID = "e_effect-exam_block_restriction-01"


class EffectContext(str, Enum):
    DIRECT_CARD = "direct-card"
    TURN_START_GIMMICK = "turn-start-gimmick"


@dataclass(frozen=True, slots=True)
class EffectRow:
    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    effect_group_ids: tuple[str, ...]
    context: EffectContext


GIMMICK_EFFECT = EffectRow(
    GIMMICK_EFFECT_ID,
    EFFECT_TYPE,
    0,
    0,
    0,
    1,
    (EFFECT_GROUP_ID,),
    EffectContext.TURN_START_GIMMICK,
)
DIRECT_EFFECT = EffectRow(
    DIRECT_EFFECT_ID,
    EFFECT_TYPE,
    0,
    0,
    0,
    2,
    (EFFECT_GROUP_ID,),
    EffectContext.DIRECT_CARD,
)
EFFECT_ROWS: tuple[EffectRow, ...] = (GIMMICK_EFFECT, DIRECT_EFFECT)
EFFECT_BY_ID: Mapping[str, EffectRow] = MappingProxyType(
    {row.id: row for row in EFFECT_ROWS}
)


@dataclass(frozen=True, slots=True)
class CardVersion:
    card_id: str
    upgrade: int
    ordered_effect_ids: tuple[str, ...]

    @property
    def version(self) -> str:
        return f"{self.card_id}+{self.upgrade}"


AFFECTED_CARD_VERSIONS: tuple[CardVersion, ...] = (
    CardVersion(
        "p_card-00-men-2_014",
        0,
        (
            "e_effect-exam_block-0011",
            DIRECT_EFFECT_ID,
            "e_effect-exam_stamina_consumption_down-03",
        ),
    ),
    CardVersion(
        "p_card-00-men-2_014",
        1,
        (
            "e_effect-exam_block-0013",
            DIRECT_EFFECT_ID,
            "e_effect-exam_stamina_consumption_down-04",
        ),
    ),
    CardVersion(
        "p_card-00-men-2_014",
        2,
        (
            "e_effect-exam_block-0015",
            DIRECT_EFFECT_ID,
            "e_effect-exam_stamina_consumption_down-04",
        ),
    ),
    CardVersion(
        "p_card-00-men-2_014",
        3,
        (
            "e_effect-exam_block-0017",
            DIRECT_EFFECT_ID,
            "e_effect-exam_stamina_consumption_down-04",
        ),
    ),
)


def matches_effect_contract(effect: object, context: EffectContext) -> bool:
    """Match every effect column represented at the scalar boundary."""

    if not isinstance(context, EffectContext):
        return False
    effect_id = getattr(effect, "id", None)
    row = EFFECT_BY_ID.get(effect_id) if isinstance(effect_id, str) else None
    if row is None or row.context is not context:
        return False
    return (
        getattr(effect, "effect_type", None) == row.effect_type
        and getattr(effect, "value1", None) == row.value1
        and getattr(effect, "value2", None) == row.value2
        and getattr(effect, "effect_count", None) == row.effect_count
        and getattr(effect, "effect_turn", None) == row.effect_turn
        and not getattr(effect, "status_enchant_id", "")
        and getattr(effect, "status_enchant", None) is None
        and not getattr(effect, "chain_effect_id", "")
        and not tuple(getattr(effect, "chain_effect_ids", ()))
        and getattr(effect, "chain_effect", None) is None
        and getattr(effect, "trigger", None) is None
        and getattr(effect, "card_move_rule", None) is None
    )


@dataclass(frozen=True, slots=True)
class BlockRestrictionRuntime:
    turns: int = 0
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        if type(self.turns) is not int or self.turns < 0:
            raise ValueError("block restriction turns must be non-negative")
        if not isinstance(self.is_passing_turn_start, bool):
            raise TypeError("block restriction passing marker must be boolean")
        if self.turns == 0 and self.is_passing_turn_start:
            raise ValueError("an absent status cannot be passing")

    @property
    def status_present(self) -> bool:
        return self.turns > 0

    def mark_passing_turn_start(self) -> "BlockRestrictionRuntime":
        return (
            BlockRestrictionRuntime(self.turns, True)
            if self.status_present
            else self
        )

    def spend_turn_boundary(self) -> "BlockRestrictionRuntime":
        """Spend a passing status; a fresh one is only marked passing."""

        if not self.status_present:
            return self
        if not self.is_passing_turn_start:
            return self.mark_passing_turn_start()
        remaining = self.turns - 1
        return BlockRestrictionRuntime(
            remaining,
            is_passing_turn_start=remaining > 0,
        )


@dataclass(frozen=True, slots=True)
class BlockRestrictionDifference:
    before: int
    after: int
    effect_type: str = EFFECT_TYPE
    effect_type_value: int = EFFECT_TYPE_VALUE


@dataclass(frozen=True, slots=True)
class Execution:
    executable: bool
    effect_id: str
    context: EffectContext
    before: BlockRestrictionRuntime
    after: BlockRestrictionRuntime
    anti_debuff_gate: AntiDebuffBlockResult | None
    operation: str
    difference: BlockRestrictionDifference | None
    add_status_callback_fired: bool
    difference_appended: bool
    trace: tuple[str, ...]
    reasons: tuple[str, ...] = ()


def execute_effect(
    effect: object,
    state: BlockRestrictionRuntime,
    anti_debuff: AntiDebuffRuntime,
    *,
    context: EffectContext,
) -> Execution:
    """Execute one exact row through gate, merge/install and difference."""

    effect_id = str(getattr(effect, "id", "<unknown>"))
    if not isinstance(state, BlockRestrictionRuntime):
        raise TypeError("state must be BlockRestrictionRuntime")
    if not isinstance(anti_debuff, AntiDebuffRuntime):
        raise TypeError("anti_debuff must be AntiDebuffRuntime")
    if not matches_effect_contract(effect, context):
        return Execution(
            False,
            effect_id,
            context,
            state,
            state,
            None,
            "unresolved",
            None,
            False,
            False,
            (),
            ("exact-effect-context-contract",),
        )

    gate = try_block_status_addition(
        anti_debuff,
        incoming_effect_type_value=EFFECT_TYPE_VALUE,
        incoming_target_type=ExamStatusEffectTargetType.DEBUFF,
    )
    if gate.blocked:
        return Execution(
            True,
            effect_id,
            context,
            state,
            state,
            gate,
            "blocked-by-anti-debuff",
            None,
            False,
            False,
            tuple((*gate.trace, "skip-block-restriction-add-and-difference")),
        )

    row = EFFECT_BY_ID[effect_id]
    installed = not state.status_present
    after = BlockRestrictionRuntime(
        state.turns + row.effect_turn,
        # AddTurn on an existing status preserves its passing marker.  A new
        # ExamStatusEffectBase starts fresh until the turn-start marker runs.
        False if installed else state.is_passing_turn_start,
    )
    difference = BlockRestrictionDifference(state.turns, after.turns)
    operation = "installed" if installed else "merged"
    return Execution(
        True,
        effect_id,
        context,
        state,
        after,
        gate,
        operation,
        difference,
        installed,
        True,
        (
            *gate.trace,
            "get-block-restriction-turn:before:is-use=false",
            f"{operation}-block-restriction-turn:{row.effect_turn}",
            "get-block-restriction-turn:after:is-use=false",
            "set-status-difference:18",
            "append-effect-difference",
        ),
    )


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "BlockRestrictionDifference",
    "BlockRestrictionRuntime",
    "CardVersion",
    "DIRECT_EFFECT",
    "DIRECT_EFFECT_ID",
    "EFFECT_BY_ID",
    "EFFECT_GROUP_ID",
    "EFFECT_ROWS",
    "EFFECT_TYPE",
    "EFFECT_TYPE_VALUE",
    "EffectContext",
    "EffectRow",
    "Execution",
    "GIMMICK_EFFECT",
    "GIMMICK_EFFECT_ID",
    "execute_effect",
    "matches_effect_contract",
]
