"""Exact Android v3.2.3 ``ExamLessonValueMultiple`` primitive.

This module deliberately has no dependency on ``plan3_engine`` or screen
recognition.  It models the native status installed by
``LessonValueMultipleEffectExecutor`` and provides a narrow adapter to the
already reconstructed per-hit score kernel.

Native names use both *LessonValueMultiple* (Master/executor) and
*LessonParameterMultiple* (runtime status collection).  They are the same
effect path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import sqlite3
from pathlib import Path
from typing import Mapping, Sequence

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_adding_parameter,
    f32,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamLessonValueMultiple"
STATUS_TYPE = 39
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1


class LessonValueMultipleContractError(ValueError):
    """A Master/runtime input does not match the proven native contract."""


def _int32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise LessonValueMultipleContractError(
            f"{label} is outside Int32: {value}"
        )
    return value


def _wrapped_i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _f32_add(left: float, right: float) -> float:
    return f32(f32(left) + f32(right))


def _permil_term(permil: int) -> float:
    # Native GetLessonParameterMultiple emits SCVTF, FDIV by 1000.0f, then
    # FADD into an S-register accumulator.
    return f32(f32(permil) / f32(1000.0))


def matches_lesson_value_multiple_effect_shape(effect: object) -> bool:
    """Return whether an object has the plain native type-39 effect shape.

    Plan 3 uses the same ``ExamLessonValueMultiple`` child as NIA's direct
    turn-start gimmicks.  The old Plan 3 dispatcher recognized only the
    ``p_card-03-men-100_016`` child, which made otherwise ordinary NIA rows
    (for example the 100 and 150 permille children) look unsupported.  Keep
    this check structural and card-independent: Master supplies the numeric
    value and duration, while this helper only admits the fields that the
    native executor actually consumes.

    A finite duration is positive; ``-1`` is the native unlimited marker.
    Zero is not a Master/runtime duration shape for this effect.
    """

    if getattr(effect, "effect_type", None) != EFFECT_TYPE:
        return False
    try:
        _int32(getattr(effect, "value1"), "effect value1")
        value2 = getattr(effect, "value2")
        effect_count = getattr(effect, "effect_count")
        effect_turn = _int32(getattr(effect, "effect_turn"), "effect turn")
    except (AttributeError, TypeError, LessonValueMultipleContractError):
        return False
    # ``value1`` is intentionally not whitelisted by value.  The native
    # Int32 field is the source of truth and the shared installer handles
    # signed wrapping/merge semantics.
    return bool(
        value2 == 0
        and effect_count == 0
        and (effect_turn == -1 or effect_turn > 0)
        and not getattr(effect, "status_enchant_id", "")
        and getattr(effect, "status_enchant", None) is None
        and not getattr(effect, "chain_effect_id", "")
        and not getattr(effect, "chain_effect_ids", ())
        and getattr(effect, "chain_effect", None) is None
        and getattr(effect, "trigger", None) is None
        and not getattr(effect, "once", False)
        and getattr(effect, "card_move_rule", None) is None
    )


@dataclass(frozen=True, slots=True)
class LessonParameterMultipleStatus:
    """One active native ``LessonParameterMultipleStatusEffect`` instance."""

    permil: int
    turn: int
    passing_turn_start: bool = False
    is_turn_limited: bool | None = None
    # Native status objects also carry a monotonically allocated UID.  The
    # scalar lesson multiplier does not use it for arithmetic, but retaining
    # it when a status is restored from ExamSaveData keeps the reference-graph
    # identity available to diagnostics and future native continuation code.
    uid: int | None = None

    def __post_init__(self) -> None:
        _int32(self.permil, "permil")
        _int32(self.turn, "turn")
        if not isinstance(self.passing_turn_start, bool):
            raise TypeError("passing_turn_start must be bool")
        if self.is_turn_limited is None:
            # The native constructor starts limited, then clears the flag for
            # any negative initial turn value.  Keep the resulting flag
            # explicit: SpendTurn can later take a finite counter below zero
            # without changing _isTurnLimited.
            object.__setattr__(self, "is_turn_limited", self.turn >= 0)
        elif not isinstance(self.is_turn_limited, bool):
            raise TypeError("is_turn_limited must be bool or None")
        if self.uid is not None and (
            isinstance(self.uid, bool)
            or not isinstance(self.uid, int)
            or self.uid <= 0
        ):
            raise ValueError("uid must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class LessonParameterMultipleState:
    """Ordered active-list projection used by the native getter."""

    statuses: tuple[LessonParameterMultipleStatus, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.statuses, tuple):
            raise TypeError("statuses must be a tuple")
        if not all(
            isinstance(item, LessonParameterMultipleStatus)
            for item in self.statuses
        ):
            raise TypeError("statuses contain an unsupported value")

    def multiplier(self) -> float:
        """Return native ``1.0f + sum(status.permil / 1000.0f)``.

        Statuses are accumulated in active-list order with a binary32
        rounding point after every division and addition.  This is additive,
        not a product of per-status multipliers.
        """

        result = f32(1.0)
        for status in self.statuses:
            result = _f32_add(result, _permil_term(status.permil))
        return result

    def mark_passing_turn_start(self) -> "LessonParameterMultipleState":
        """Mirror the post-TurnStart checkpoint for surviving statuses."""

        return LessonParameterMultipleState(
            tuple(
                replace(status, passing_turn_start=True)
                for status in self.statuses
            )
        )

    def spend_turn_start(self) -> "LessonParameterMultipleState":
        """Apply the shared native finite-status TurnStart lifecycle.

        A finite status is decremented only after its passing flag was set.
        Every finite status at turn zero or below is then removed.  Negative
        turns are unlimited and are neither decremented nor removed.
        """

        survivors: list[LessonParameterMultipleStatus] = []
        for status in self.statuses:
            current = status
            if status.is_turn_limited and status.passing_turn_start:
                current = replace(status, turn=_wrapped_i32(status.turn - 1))
            if current.is_turn_limited and current.turn <= 0:
                continue
            survivors.append(current)
        return LessonParameterMultipleState(tuple(survivors))


@dataclass(frozen=True, slots=True)
class LessonValueMultipleMasterEffect:
    effect_id: str
    permil: int
    turn: int

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise TypeError("effect_id must be non-empty text")
        _int32(self.permil, "effect permil")
        _int32(self.turn, "effect turn")

    @classmethod
    def from_mapping(
        cls, row: Mapping[str, object]
    ) -> "LessonValueMultipleMasterEffect":
        effect_type = row.get("effectType", row.get("effect_type"))
        if effect_type != EFFECT_TYPE:
            raise LessonValueMultipleContractError(
                f"expected {EFFECT_TYPE}, found {effect_type!r}"
            )
        effect_id = row.get("id")
        permil = row.get("effectValue1", row.get("value1"))
        turn = row.get("effectTurn", row.get("effect_turn"))
        if not isinstance(effect_id, str) or not effect_id:
            raise LessonValueMultipleContractError("effect id is missing")
        return cls(
            effect_id=effect_id,
            permil=_int32(permil, "effectValue1"),
            turn=_int32(turn, "effectTurn"),
        )


@dataclass(frozen=True, slots=True)
class LessonValueMultipleExecution:
    state: LessonParameterMultipleState
    installed: bool
    merged_index: int | None


def install_lesson_value_multiple(
    state: LessonParameterMultipleState,
    *,
    permil: int,
    turn: int,
    block_add_status: bool = False,
    new_status_uid: int | None = None,
) -> LessonValueMultipleExecution:
    """Execute native ``TryAddLessonParameterMultipleStatus``.

    The last active instance with the same ``turn`` is incremented.  Its
    signed Int32 addition wraps exactly as ARM64 ``ADD W`` and a negative
    result is clamped to zero.  If no turn matches, a fresh status is appended.
    ``block_add_status`` represents the collection's preceding type-39 block
    gate; a blocked call returns false and leaves state untouched.
    """

    if not isinstance(state, LessonParameterMultipleState):
        raise TypeError("state must be LessonParameterMultipleState")
    permil = _int32(permil, "permil")
    turn = _int32(turn, "turn")
    if not isinstance(block_add_status, bool):
        raise TypeError("block_add_status must be bool")
    if new_status_uid is not None and (
        isinstance(new_status_uid, bool)
        or not isinstance(new_status_uid, int)
        or not 1 <= new_status_uid <= INT32_MAX
    ):
        raise ValueError("new_status_uid must be a positive Int32 or None")
    if block_add_status:
        return LessonValueMultipleExecution(state, False, None)

    statuses = list(state.statuses)
    for index in range(len(statuses) - 1, -1, -1):
        current = statuses[index]
        if current.turn != turn:
            continue
        merged = _wrapped_i32(current.permil + permil)
        statuses[index] = replace(current, permil=max(0, merged))
        return LessonValueMultipleExecution(
            LessonParameterMultipleState(tuple(statuses)), True, index
        )

    statuses.append(
        LessonParameterMultipleStatus(
            permil=permil,
            turn=turn,
            uid=new_status_uid,
        )
    )
    return LessonValueMultipleExecution(
        LessonParameterMultipleState(tuple(statuses)), True, None
    )


def execute_master_effect(
    state: LessonParameterMultipleState,
    effect: LessonValueMultipleMasterEffect,
    *,
    block_add_status: bool = False,
    new_status_uid: int | None = None,
) -> LessonValueMultipleExecution:
    if not isinstance(effect, LessonValueMultipleMasterEffect):
        raise TypeError("effect must be LessonValueMultipleMasterEffect")
    return install_lesson_value_multiple(
        state,
        permil=effect.permil,
        turn=effect.turn,
        block_add_status=block_add_status,
        new_status_uid=new_status_uid,
    )


def load_master_effect(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> LessonValueMultipleMasterEffect:
    """Load one exact effect row from the local imported Master database."""

    if not isinstance(effect_id, str) or not effect_id:
        raise TypeError("effect_id must be non-empty text")
    uri = f"file:{Path(database).resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT id, effect_type, value1, effect_turn
            FROM effect
            WHERE id = ?
            """,
            (effect_id,),
        ).fetchall()
    if len(rows) != 1:
        raise KeyError(
            f"effect must resolve exactly once, found {len(rows)}: {effect_id}"
        )
    row_id, effect_type, value1, effect_turn = rows[0]
    return LessonValueMultipleMasterEffect.from_mapping(
        {
            "id": row_id,
            "effect_type": effect_type,
            "value1": value1,
            "effect_turn": effect_turn,
        }
    )


def bind_adding_parameter_status(
    status: AddingParameterStatus,
    state: LessonParameterMultipleState,
) -> AddingParameterStatus:
    """Bind the native getter result into ``CalculateAddingParameter`` input."""

    if not isinstance(status, AddingParameterStatus):
        raise TypeError("status must be AddingParameterStatus")
    if not isinstance(state, LessonParameterMultipleState):
        raise TypeError("state must be LessonParameterMultipleState")
    return replace(status, lesson_parameter_multiple=state.multiplier())


@dataclass(frozen=True, slots=True)
class LessonHitMutation:
    """One native Lesson loop iteration: calculate, then mutate score."""

    calculated_parameter: int
    application: ParameterApplication
    next_application_status: ParameterApplicationStatus


def apply_lesson_hit(
    value: int,
    *,
    multiplier_state: LessonParameterMultipleState,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    application_status: ParameterApplicationStatus,
    is_buff_active: bool = True,
    additional: AddingParameterAdditionalData | None = None,
) -> LessonHitMutation:
    """Run one proven native Lesson score mutation in exact call order.

    ``LessonEffectExecutor`` calls ``CalculateAddingParameter`` and then
    ``AddParameter`` inside every loop iteration.  Call this helper once per
    hit and pass its ``next_application_status`` into the following hit; do
    not multiply an already-summed multi-hit result.
    """

    bound = bind_adding_parameter_status(adding_status, multiplier_state)
    calculated = calculate_adding_parameter(
        value,
        is_buff_active=is_buff_active,
        status=bound,
        settings=settings,
        additional=additional,
    )
    application = apply_parameter_add(calculated, status=application_status)
    next_status = replace(
        application_status,
        judge_parameter=application.after,
        current_turn_total_add_parameter=(
            application.current_turn_total_add_parameter
        ),
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )
    return LessonHitMutation(calculated, application, next_status)


def apply_lesson_hits(
    value: int,
    count: int,
    *,
    multiplier_state: LessonParameterMultipleState,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    application_status: ParameterApplicationStatus,
    is_buff_active: bool = True,
    additional: AddingParameterAdditionalData | None = None,
) -> tuple[LessonHitMutation, ...]:
    """Repeat the exact per-hit call pair for an explicit positive count."""

    count = _int32(count, "count")
    if count < 1:
        return ()
    result: list[LessonHitMutation] = []
    current = application_status
    for _ in range(count):
        hit = apply_lesson_hit(
            value,
            multiplier_state=multiplier_state,
            adding_status=adding_status,
            settings=settings,
            application_status=current,
            is_buff_active=is_buff_active,
            additional=additional,
        )
        result.append(hit)
        current = hit.next_application_status
    return tuple(result)


def multiplier_from_statuses(
    statuses: Sequence[LessonParameterMultipleStatus],
) -> float:
    """Convenience wrapper that still preserves caller-provided order."""

    return LessonParameterMultipleState(tuple(statuses)).multiplier()


__all__ = [
    "EFFECT_TYPE",
    "STATUS_TYPE",
    "LessonHitMutation",
    "LessonParameterMultipleState",
    "LessonParameterMultipleStatus",
    "LessonValueMultipleContractError",
    "LessonValueMultipleExecution",
    "LessonValueMultipleMasterEffect",
    "apply_lesson_hit",
    "apply_lesson_hits",
    "bind_adding_parameter_status",
    "execute_master_effect",
    "install_lesson_value_multiple",
    "load_master_effect",
    "matches_lesson_value_multiple_effect_shape",
    "multiplier_from_statuses",
]
