"""Standalone native contract for Plan2 ``BlockPerUserCardCount``.

The Android v3.2.3 executor is a direct card-play effect, not a status
installer.  It reads the current global ``ExamCardPlayCount`` and computes
``effectValue1 + effectValue2 * ExamCardPlayCount`` before the card-play
counter is incremented.  This module deliberately owns a tiny immutable
runtime instead of extending :mod:`gkms_tool.plan2_state`.

Only the current Master shape proved by the local native audit is accepted.
Unsupported fields fail closed in the loader and constructor.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    AddBlockSettings,
    AddBlockStatus,
    calculate_add_block,
)


BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamBlockPerUseCardCount"
)
BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE = 3


ANDROID_BLOCK_PER_USE_CARD_COUNT_EVIDENCE = {
    "factory": {
        "symbol": "CreateExamEffectExecutor",
        "enum_value": 133,
        "executor": "Campus.InGame.Exam.BlockPerUserCardCountEffectExecutor",
        "ctor_va": "0x7E73080",
        "execute_va": "0x7E73198",
        "fact": "constructor copies effectValue1/effectValue2 into _value/_value2",
    },
    "formula": {
        "execute_va": "0x7E73198",
        "count_getter_va": "0x7EB82FC",
        "fact": (
            "madd(value1, value2, ExamParameterModel.get_ExamCardPlayCount) "
            "then CalculateAddBlock(..., additionalMultipleAggressiveRate=1)"
        ),
    },
    "block_calculation": {
        "calculate_add_block_va": "0x7E5E6E4",
        "calculate_add_block_wrapper_va": "0x7E5E6CC",
        "add_block_fix_va": "0x7E5EA94",
        "fact": (
            "restriction short-circuits CalculateAddBlock to zero; then the "
            "native aggressive/add-down/fixed reductions run in order and "
            "AddBlockFix clamps the final block floor to zero"
        ),
    },
    "card_play_order": {
        "play_card_count_increment_va": "0x7EBB6C8",
        "exam_card_play_count_getter_va": "0x7EB82FC",
        "turn_card_play_count_getter_va": "0x7EB8208",
        "fact": (
            "PlayCardCountIncrement increments ExamCardPlayCount and then "
            "TurnCardPlayCount after the card effect command batch"
        ),
    },
    "difference": {
        "block_difference_va": "0x7E56818",
        "status_difference_va": "0x7E56AE4",
        "effect_difference_create_va": "0x7E56020",
        "fact": (
            "AddBlockFix appends a block difference before SetBlock; the "
            "executor then appends status-effect type 3 difference after "
            "reading the resulting block"
        ),
    },
}


class Plan2BlockPerUseCardCountContractError(ValueError):
    """A Master row or runtime shape is outside the proven native contract."""


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2BlockPerUseCardCountContractError(
            f"{label} must be an integer"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2BlockPerUseCardCountContractError(
            f"{label} is outside Int32"
        )
    return value


def _non_negative_int(value: object, label: str) -> int:
    value = _plain_int(value, label)
    if value < 0:
        raise Plan2BlockPerUseCardCountContractError(
            f"{label} must be non-negative"
        )
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(raw_json: object, row_id: str) -> dict[str, object]:
    try:
        raw = json.loads(str(raw_json))
    except json.JSONDecodeError as error:
        raise Plan2BlockPerUseCardCountContractError(
            f"{row_id}: raw_json is invalid"
        ) from error
    if not isinstance(raw, dict):
        raise Plan2BlockPerUseCardCountContractError(
            f"{row_id}: raw_json must be an object"
        )
    return raw


def _require_equal(
    raw: dict[str, object], key: str, expected: object, row_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2BlockPerUseCardCountContractError(
            f"{row_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


@dataclass(frozen=True, slots=True)
class Plan2BlockPerUseCardCountEffect:
    """The executable fields copied by the native target constructor."""

    effect_id: str
    value1: int
    value2: int
    effect_count: int = 0
    effect_turn: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise Plan2BlockPerUseCardCountContractError(
                "effect_id must be a non-empty string"
            )
        _plain_int(self.value1, "value1")
        _plain_int(self.value2, "value2")
        effect_count = _plain_int(self.effect_count, "effect_count")
        effect_turn = _plain_int(self.effect_turn, "effect_turn")
        if effect_count != 0:
            raise Plan2BlockPerUseCardCountContractError(
                "effect_count is not part of the proven direct-effect shape"
            )
        if effect_turn != 0:
            raise Plan2BlockPerUseCardCountContractError(
                "effect_turn is not part of the proven direct-effect shape"
            )

    @property
    def effect_value1(self) -> int:
        return self.value1

    @property
    def effect_value2(self) -> int:
        return self.value2

    @property
    def installs_status(self) -> bool:
        return False

    @property
    def lifetime(self) -> Literal["direct"]:
        return "direct"


@dataclass(frozen=True, slots=True)
class Plan2BlockPerUseCardCountRuntime:
    """Minimal immutable state needed by this one direct native executor."""

    block: int = 0
    exam_card_play_count: int = 0
    turn_card_play_count: int = 0
    turn_index: int = 0
    block_consumption_sum_count: int = 0
    block_restriction: bool = False
    aggressive: int = 0
    block_add_down: bool = False
    block_add_down_restriction_for_ratio: bool = False
    block_add_down_restriction_for_fix: bool = False
    block_add_down_permille: int = 1000
    block_add_down_fix_peek: int = 0
    block_add_down_fix_consume: int = 0

    def __post_init__(self) -> None:
        _non_negative_int(self.block, "block")
        _non_negative_int(
            self.exam_card_play_count, "exam_card_play_count"
        )
        _non_negative_int(
            self.turn_card_play_count, "turn_card_play_count"
        )
        _non_negative_int(self.turn_index, "turn_index")
        _non_negative_int(
            self.block_consumption_sum_count, "block_consumption_sum_count"
        )
        _plain_int(self.aggressive, "aggressive")
        _plain_int(self.block_add_down_permille, "block_add_down_permille")
        _plain_int(self.block_add_down_fix_peek, "block_add_down_fix_peek")
        _plain_int(
            self.block_add_down_fix_consume, "block_add_down_fix_consume"
        )
        for label, value in (
            ("block_restriction", self.block_restriction),
            ("block_add_down", self.block_add_down),
            (
                "block_add_down_restriction_for_ratio",
                self.block_add_down_restriction_for_ratio,
            ),
            (
                "block_add_down_restriction_for_fix",
                self.block_add_down_restriction_for_fix,
            ),
        ):
            if not isinstance(value, bool):
                raise Plan2BlockPerUseCardCountContractError(
                    f"{label} must be boolean"
                )

    def start_turn(self, turn_index: int | None = None) -> "Plan2BlockPerUseCardCountRuntime":
        """Reset only the native per-turn counter; global count continues."""

        next_turn = self.turn_index + 1 if turn_index is None else turn_index
        next_turn = _non_negative_int(next_turn, "turn_index")
        if next_turn <= self.turn_index:
            raise Plan2BlockPerUseCardCountContractError(
                "start_turn must advance turn_index"
            )
        return replace(self, turn_index=next_turn, turn_card_play_count=0)


DifferenceKind = Literal["block", "effect"]


@dataclass(frozen=True, slots=True)
class Plan2BlockPerUseCardCountDifference:
    """One item appended to the native effect-difference list."""

    kind: DifferenceKind
    preview: int
    current: int
    block_consumption_sum_count: int | None
    status_effect_type: int | None = None
    is_consumption: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ("block", "effect"):
            raise Plan2BlockPerUseCardCountContractError(
                "difference kind is unknown"
            )
        _plain_int(self.preview, "difference.preview")
        _plain_int(self.current, "difference.current")
        if self.kind == "block":
            if self.status_effect_type is not None:
                raise Plan2BlockPerUseCardCountContractError(
                    "block difference must not carry a status-effect type"
                )
            _non_negative_int(
                self.block_consumption_sum_count,
                "difference.block_consumption_sum_count",
            )
        else:
            if self.status_effect_type != BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE:
                raise Plan2BlockPerUseCardCountContractError(
                    "effect difference must carry status-effect type 3"
                )
            if self.block_consumption_sum_count is not None:
                raise Plan2BlockPerUseCardCountContractError(
                    "effect difference must not carry block-consumption data"
                )
        if not isinstance(self.is_consumption, bool):
            raise Plan2BlockPerUseCardCountContractError(
                "difference.is_consumption must be boolean"
            )


@dataclass(frozen=True, slots=True)
class Plan2BlockPerUseCardCountTransition:
    """Pure result of one direct execution or one complete card play."""

    before: Plan2BlockPerUseCardCountRuntime
    after: Plan2BlockPerUseCardCountRuntime
    effect: Plan2BlockPerUseCardCountEffect
    count_at_execution: int
    requested_value: int
    calculated_value: int
    block_fix_value: int
    block_before: int
    block_after: int
    differences: tuple[Plan2BlockPerUseCardCountDifference, ...]
    removed_status_uids: tuple[int, ...]
    event_trace: tuple[str, ...]

    @property
    def applied_block(self) -> int:
        return self.block_after - self.block_before

    @property
    def callbacks(self) -> tuple[str, ...]:
        """No direct callback is invoked by this executor."""

        return ()

    @property
    def status_layers(self) -> tuple[()]:
        """The direct executor never installs, merges, or expires a status."""

        return ()


def _calculate_requested_value(
    effect: Plan2BlockPerUseCardCountEffect,
    runtime: Plan2BlockPerUseCardCountRuntime,
) -> int:
    # Native ``madd w0,w0,w22,w20`` is signed 32-bit arithmetic.
    return _i32(effect.value1 + effect.value2 * runtime.exam_card_play_count)


def _add_block_fix(block_before: int, calculated_value: int) -> tuple[int, int]:
    """Reconstruct ``AddBlockFix`` for the normal finite Int32 domain.

    Native first computes ``Math.Max(-currentBlock, value)`` and then calls
    ``SetBlock(currentBlock + effective, isConsumption=false)``.  The
    CalculateAddBlock contract supplies a non-negative ``value`` here, so
    this is the same as a non-negative floor on the resulting block.
    """

    effective = max(-block_before, calculated_value)
    effective = _i32(effective)
    after = max(0, _i32(block_before + effective))
    return effective, after


def evaluate_plan2_block_per_use_card_count(
    runtime: Plan2BlockPerUseCardCountRuntime,
    effect: Plan2BlockPerUseCardCountEffect,
) -> Plan2BlockPerUseCardCountTransition:
    """Execute only the native direct effect; card counters are not advanced.

    The caller can use :func:`simulate_plan2_block_per_use_card_play` when the
    surrounding card-play transaction, including its post-effect counter
    increment, should also be projected.
    """

    if not isinstance(runtime, Plan2BlockPerUseCardCountRuntime):
        raise TypeError("runtime must be Plan2BlockPerUseCardCountRuntime")
    if not isinstance(effect, Plan2BlockPerUseCardCountEffect):
        raise TypeError("effect must be Plan2BlockPerUseCardCountEffect")

    count = runtime.exam_card_play_count
    requested = _calculate_requested_value(effect, runtime)
    calculated = calculate_add_block(
        requested,
        is_buff_active=True,
        status=AddBlockStatus(
            block_restriction=runtime.block_restriction,
            aggressive=runtime.aggressive,
            block_add_down=runtime.block_add_down,
            block_add_down_restriction_for_ratio=(
                runtime.block_add_down_restriction_for_ratio
            ),
            block_add_down_restriction_for_fix=(
                runtime.block_add_down_restriction_for_fix
            ),
            block_add_down_fix_peek=runtime.block_add_down_fix_peek,
            block_add_down_fix_consume=runtime.block_add_down_fix_consume,
        ),
        settings=AddBlockSettings(
            block_add_down_permille=runtime.block_add_down_permille
        ),
        additional_multiple_aggressive_rate=1.0,
    )

    block_before = runtime.block
    block_fix_value, block_after = _add_block_fix(block_before, calculated)
    block_difference = Plan2BlockPerUseCardCountDifference(
        kind="block",
        preview=block_before,
        current=block_after,
        block_consumption_sum_count=runtime.block_consumption_sum_count,
    )
    after_runtime = replace(runtime, block=block_after)
    effect_difference = Plan2BlockPerUseCardCountDifference(
        kind="effect",
        preview=block_before,
        current=block_after,
        block_consumption_sum_count=None,
        status_effect_type=BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE,
    )
    return Plan2BlockPerUseCardCountTransition(
        before=runtime,
        after=after_runtime,
        effect=effect,
        count_at_execution=count,
        requested_value=requested,
        calculated_value=calculated,
        block_fix_value=block_fix_value,
        block_before=block_before,
        block_after=block_after,
        differences=(block_difference, effect_difference),
        removed_status_uids=(),
        event_trace=(
            "executor:snapshot:block",
            "executor:read:ExamCardPlayCount",
            "executor:compute:value1+value2*ExamCardPlayCount",
            "executor:CalculateAddBlock(isBuffActive=true,rate=1.0)",
            "AddBlockFix:append:block-difference",
            "AddBlockFix:SetBlock(isConsumption=false)",
            "executor:snapshot:block-after",
            "executor:append:effect-difference(statusEffectType=3,isConsumption=false)",
            "executor:no-status-install-or-remove",
        ),
    )


def simulate_plan2_block_per_use_card_play(
    runtime: Plan2BlockPerUseCardCountRuntime,
    effect: Plan2BlockPerUseCardCountEffect,
) -> Plan2BlockPerUseCardCountTransition:
    """Execute the direct effect, then perform native post-effect count updates."""

    transition = evaluate_plan2_block_per_use_card_count(runtime, effect)
    if transition.after.exam_card_play_count == INT32_MAX:
        raise OverflowError("ExamCardPlayCount cannot be incremented past Int32")
    after = replace(
        transition.after,
        exam_card_play_count=transition.after.exam_card_play_count + 1,
        turn_card_play_count=transition.after.turn_card_play_count + 1,
    )
    return replace(
        transition,
        after=after,
        event_trace=transition.event_trace
        + (
            "card-play:PlayCardCountIncrement:ExamCardPlayCount",
            "card-play:PlayCardCountIncrement:TurnCardPlayCount",
            "card-play:no-target-status-play-count-callback",
        ),
    )


def load_plan2_block_per_use_card_count_effect(
    effect_id: str, database: Path = DEFAULT_DATABASE
) -> Plan2BlockPerUseCardCountEffect:
    """Load and strictly validate one current-Master target effect row."""

    if not isinstance(effect_id, str) or not effect_id:
        raise Plan2BlockPerUseCardCountContractError(
            "effect_id must be a non-empty string"
        )
    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown Master effect: {effect_id}")

    if str(row["effect_type"]) != BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE:
        raise Plan2BlockPerUseCardCountContractError(
            f"{effect_id}: expected {BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE}"
        )
    value1 = _plain_int(row["value1"], f"{effect_id}.value1")
    value2 = _plain_int(row["value2"], f"{effect_id}.value2")
    effect_count = _plain_int(row["effect_count"], f"{effect_id}.effect_count")
    effect_turn = _plain_int(row["effect_turn"], f"{effect_id}.effect_turn")
    if row["status_enchant_id"] != "" or row["chain_effect_id"] != "":
        raise Plan2BlockPerUseCardCountContractError(
            f"{effect_id}: status/chain fields are unsupported"
        )

    raw = _json_object(row["raw_json"], effect_id)
    neutral_fields = (
        ("id", effect_id),
        ("effectType", BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE),
        ("effectValue1", value1),
        ("effectValue2", value2),
        ("effectCount", effect_count),
        ("effectTurn", effect_turn),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        ("produceCardSearchId", ""),
        ("movePositionType", "ProduceCardMovePositionType_Unknown"),
        ("pickRangeType", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", "ProducePickCountType_Unknown"),
        ("pickCountMin", 0),
        ("pickCountMax", 0),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", "ProducePickCountType_Unknown"),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", ""),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
    )
    for key, expected in neutral_fields:
        _require_equal(raw, key, expected, effect_id)

    return Plan2BlockPerUseCardCountEffect(
        effect_id=effect_id,
        value1=value1,
        value2=value2,
        effect_count=effect_count,
        effect_turn=effect_turn,
    )


__all__ = [
    "ANDROID_BLOCK_PER_USE_CARD_COUNT_EVIDENCE",
    "BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE",
    "BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE",
    "Plan2BlockPerUseCardCountContractError",
    "Plan2BlockPerUseCardCountDifference",
    "Plan2BlockPerUseCardCountEffect",
    "Plan2BlockPerUseCardCountRuntime",
    "Plan2BlockPerUseCardCountTransition",
    "evaluate_plan2_block_per_use_card_count",
    "load_plan2_block_per_use_card_count_effect",
    "simulate_plan2_block_per_use_card_play",
]
