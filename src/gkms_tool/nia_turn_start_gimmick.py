"""Standalone Android v3.2.3 NIA turn-start gimmick adapter.

The adapter intentionally does not depend on ``plan3_engine`` or screen
recognition.  It joins the current NIA Master turn schedule to the native
field-condition semantics and executes the reconstructed
``ExamLessonValueMultiple``, ``ExamAddGrowEffect`` and ``ExamStatusEnchant``
primitives.  Unsupported runtime shapes remain visible in the result instead
of being silently ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence

from .master_db import DEFAULT_DATABASE
from .nia_add_grow_effect import (
    DEFAULT_MASTER_DIR,
    EFFECT_TYPE as ADD_GROW_EFFECT_TYPE,
    NiaAddGrowMutation,
    NiaAddGrowProgram,
    NiaDeckAllState,
    execute_nia_add_grow_program,
    load_nia_add_grow_program,
)
from .nia_lesson_value_multiple import (
    EFFECT_TYPE as LESSON_VALUE_MULTIPLE_EFFECT_TYPE,
    LessonParameterMultipleState,
    LessonValueMultipleMasterEffect,
    execute_master_effect,
)
from .nia_static_adapter import NiaGimmickStep

if TYPE_CHECKING:
    from .nia_status_enchant import NiaStatusEnchantProgram
    from .plan3_engine import ActivePlan3StatusEnchant


FIELD_UNKNOWN = "ProduceExamFieldStatusType_Unknown"
FIELD_FULL_POWER_POINT_GET_SUM_UP = (
    "ProduceExamFieldStatusType_FullPowerPointGetSumUp"
)
FIELD_CONCENTRATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_ConcentrationChangeCountUp"
)
FIELD_PRESERVATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_PreservationChangeCountUp"
)
CHECK_UNKNOWN = "ProduceExamTriggerCheckType_Unknown"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
BLOCK_EFFECT_TYPE = "ProduceExamEffectType_ExamBlock"

SUPPORTED_COUNTER_FIELDS = frozenset(
    (
        FIELD_FULL_POWER_POINT_GET_SUM_UP,
        FIELD_CONCENTRATION_CHANGE_COUNT_UP,
        FIELD_PRESERVATION_CHANGE_COUNT_UP,
    )
)


class NiaTurnStartContractError(ValueError):
    """An input is outside the statically proven NIA v3.2.3 contract."""


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not -(2**31) <= value <= (2**31) - 1:
        raise NiaTurnStartContractError(f"{label} is outside Int32: {value}")
    return value


@dataclass(frozen=True, slots=True)
class NiaTurnStartCounters:
    """Runtime counters read by the three NIA-used native field cases."""

    full_power_point_get_sum: int = 0
    concentration_change_count: int = 0
    preservation_change_count: int = 0

    def __post_init__(self) -> None:
        _integer(self.full_power_point_get_sum, "full_power_point_get_sum")
        _integer(self.concentration_change_count, "concentration_change_count")
        _integer(self.preservation_change_count, "preservation_change_count")

    def value_for(self, field_status_type: str) -> int:
        if field_status_type == FIELD_FULL_POWER_POINT_GET_SUM_UP:
            return self.full_power_point_get_sum
        if field_status_type == FIELD_CONCENTRATION_CHANGE_COUNT_UP:
            return self.concentration_change_count
        if field_status_type == FIELD_PRESERVATION_CHANGE_COUNT_UP:
            return self.preservation_change_count
        raise NiaTurnStartContractError(
            f"unsupported NIA field status: {field_status_type!r}"
        )


@dataclass(frozen=True, slots=True)
class NiaTurnStartMasterEffect:
    """Narrow Master projection needed by this adapter."""

    effect_id: str
    effect_type: str
    value1: int
    effect_turn: int
    status_enchant_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise TypeError("effect_id must be non-empty text")
        if not isinstance(self.effect_type, str) or not self.effect_type:
            raise TypeError("effect_type must be non-empty text")
        _integer(self.value1, "value1")
        _integer(self.effect_turn, "effect_turn")
        if not isinstance(self.status_enchant_id, str):
            raise TypeError("status_enchant_id must be text")

    def lesson_value_multiple(self) -> LessonValueMultipleMasterEffect:
        if self.effect_type != LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
            raise NiaTurnStartContractError(
                "effect is not ExamLessonValueMultiple: "
                f"{self.effect_type!r}"
            )
        return LessonValueMultipleMasterEffect(
            effect_id=self.effect_id,
            permil=self.value1,
            turn=self.effect_turn,
        )


@dataclass(frozen=True, slots=True)
class NiaTurnStartDecision:
    """One scheduled Master step and its observable adapter outcome."""

    step: NiaGimmickStep
    condition_met: bool
    effect_type: str | None
    handled: bool
    installed: bool
    reason: str
    listener_executable: bool | None = None
    listener_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NiaTurnStartExecution:
    multiplier_state: LessonParameterMultipleState
    decisions: tuple[NiaTurnStartDecision, ...]
    status_enchants: tuple[ActivePlan3StatusEnchant, ...] = ()
    deck_all_state: NiaDeckAllState | None = None
    add_grow_mutations: tuple[NiaAddGrowMutation, ...] = ()
    next_status_uid: int | None = None

    @property
    def applied(self) -> tuple[NiaTurnStartDecision, ...]:
        return tuple(item for item in self.decisions if item.installed)

    @property
    def unsupported(self) -> tuple[NiaTurnStartDecision, ...]:
        return tuple(
            item
            for item in self.decisions
            if item.condition_met and not item.handled
        )


@dataclass(frozen=True, slots=True)
class NiaTurnStartStepCapability:
    """Static executable-support result for one NIA gimmick row."""

    step: NiaGimmickStep
    effect_type: str | None
    status_enchant_id: str | None
    executable: bool
    listener_backend: str | None
    blocker_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NiaTurnStartProfileCapability:
    """Effect-layer gate; this is deliberately not a whole-audition claim."""

    total_steps: int
    executable_steps: int
    effect_layer_executable: bool
    blocker_codes: tuple[str, ...]
    steps: tuple[NiaTurnStartStepCapability, ...]


def validate_nia_turn_start_condition_shape(step: NiaGimmickStep) -> None:
    """Fail closed unless a row uses one of the proven NIA condition shapes."""

    if not isinstance(step, NiaGimmickStep):
        raise TypeError("step must be NiaGimmickStep")
    if step.field_status_card_search_id:
        raise NiaTurnStartContractError(
            "field-status card-search condition is not supported"
        )
    if step.field_status_check_type not in (CHECK_UNKNOWN, CHECK_NOT):
        raise NiaTurnStartContractError(
            f"unsupported trigger check: {step.field_status_check_type!r}"
        )
    if step.field_status_type == FIELD_UNKNOWN:
        if step.field_status_value != 0:
            raise NiaTurnStartContractError(
                "Unknown field status is proven only for value zero"
            )
        return
    if step.field_status_type not in SUPPORTED_COUNTER_FIELDS:
        raise NiaTurnStartContractError(
            f"unsupported NIA field status: {step.field_status_type!r}"
        )
    _integer(step.field_status_value, "field_status_value")


def evaluate_nia_turn_start_condition(
    step: NiaGimmickStep,
    counters: NiaTurnStartCounters,
) -> bool:
    """Evaluate the exact NIA-used field/check combinations.

    Native ``IsFieldStatusTriggerStatusEffect`` evaluates all three counter
    cases as ``current >= threshold``.  Its caller continues when that result
    differs from ``check == Not``: Unknown therefore keeps ``>=`` and Not
    inverts it to ``<``.

    The current NIA Master also uses an empty Unknown/0/Unknown condition as
    an unconditional trigger.  Card-search conditions are outside this
    adapter and fail closed.
    """

    if not isinstance(step, NiaGimmickStep):
        raise TypeError("step must be NiaGimmickStep")
    if not isinstance(counters, NiaTurnStartCounters):
        raise TypeError("counters must be NiaTurnStartCounters")
    validate_nia_turn_start_condition_shape(step)

    if step.field_status_type == FIELD_UNKNOWN:
        base_result = True
    else:
        current = counters.value_for(step.field_status_type)
        threshold = _integer(step.field_status_value, "field_status_value")
        base_result = current >= threshold

    return (
        not base_result
        if step.field_status_check_type == CHECK_NOT
        else base_result
    )


def scheduled_nia_turn_start_steps(
    steps: Sequence[NiaGimmickStep], current_turn: int
) -> tuple[NiaGimmickStep, ...]:
    """Return current-turn Master rows in stable priority order.

    The current NIA rows have ``remainingTurnPermil == remainingTurn == 0``;
    those rows use their explicit ``startTurn``.  Other scheduling shapes are
    rejected rather than approximated.
    """

    current_turn = _integer(current_turn, "current_turn")
    indexed: list[tuple[int, int, NiaGimmickStep]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, NiaGimmickStep):
            raise TypeError("steps must contain only NiaGimmickStep")
        if step.remaining_turn_permille != 0 or step.remaining_turn != 0:
            raise NiaTurnStartContractError(
                "remaining-turn gimmick scheduling is outside the current "
                "NIA exact profile"
            )
        if step.start_turn == current_turn:
            indexed.append((step.priority, index, step))
    return tuple(item[2] for item in sorted(indexed))


def execute_nia_turn_start_gimmicks(
    steps: Sequence[NiaGimmickStep],
    *,
    current_turn: int,
    counters: NiaTurnStartCounters,
    effects: Mapping[str, NiaTurnStartMasterEffect],
    multiplier_state: LessonParameterMultipleState | None = None,
    deck_all_state: NiaDeckAllState | None = None,
    add_grow_programs: Mapping[str, NiaAddGrowProgram] | None = None,
    status_enchant_programs: Mapping[str, NiaStatusEnchantProgram] | None = None,
    active_status_enchants: Sequence[ActivePlan3StatusEnchant] = (),
    block_add_status: bool = False,
    next_status_uid: int | None = None,
    scalar_effect_handler: Callable[..., None] | None = None,
) -> NiaTurnStartExecution:
    """Evaluate and execute current-turn NIA gimmicks.

    The three exact NIA effect families mutate independent returned states.
    Direct DeckAll growth requires a complete ``NiaDeckAllState``; absence of
    that runtime state is an explicit fail-closed result.
    """

    if not isinstance(counters, NiaTurnStartCounters):
        raise TypeError("counters must be NiaTurnStartCounters")
    if not isinstance(block_add_status, bool):
        raise TypeError("block_add_status must be bool")
    if next_status_uid is not None and (
        _integer(next_status_uid, "next_status_uid") < 1
    ):
        raise NiaTurnStartContractError(
            "next_status_uid must be a positive Int32"
        )
    status_uid_cursor = next_status_uid
    state = multiplier_state or LessonParameterMultipleState()
    if not isinstance(state, LessonParameterMultipleState):
        raise TypeError("multiplier_state must be LessonParameterMultipleState")
    status_programs = status_enchant_programs or {}
    if not isinstance(status_programs, Mapping):
        raise TypeError("status_enchant_programs must be a mapping")
    statuses = tuple(active_status_enchants)
    grow_programs = add_grow_programs or {}
    if not isinstance(grow_programs, Mapping):
        raise TypeError("add_grow_programs must be a mapping")
    if deck_all_state is not None and not isinstance(deck_all_state, NiaDeckAllState):
        raise TypeError("deck_all_state must be NiaDeckAllState or None")
    deck_state = deck_all_state
    grow_mutations: list[NiaAddGrowMutation] = []

    decisions: list[NiaTurnStartDecision] = []
    for step in scheduled_nia_turn_start_steps(steps, current_turn):
        condition_met = evaluate_nia_turn_start_condition(step, counters)
        if not condition_met:
            decisions.append(
                NiaTurnStartDecision(
                    step, False, None, True, False, "condition-not-met"
                )
            )
            continue

        effect = effects.get(step.effect_id)
        if effect is None:
            decisions.append(
                NiaTurnStartDecision(
                    step, True, None, False, False, "master-effect-missing"
                )
            )
            continue
        if effect.effect_id != step.effect_id:
            raise NiaTurnStartContractError(
                f"effect mapping key/id mismatch: {step.effect_id!r}"
            )
        if effect.effect_type == BLOCK_EFFECT_TYPE:
            if scalar_effect_handler is None:
                decisions.append(NiaTurnStartDecision(step, True, effect.effect_type, False, False,
                                                       "shared-scalar-effect-handler-missing"))
            else:
                scalar_effect_handler(step, effect, statuses, state, status_uid_cursor)
                decisions.append(NiaTurnStartDecision(step, True, effect.effect_type, True, True,
                                                       "applied-shared-scalar-effect"))
            continue
        if effect.effect_type == STATUS_ENCHANT_EFFECT_TYPE:
            program = status_programs.get(effect.effect_id)
            if program is None:
                decisions.append(
                    NiaTurnStartDecision(
                        step,
                        True,
                        effect.effect_type,
                        False,
                        False,
                        "effect-primitive-not-implemented",
                    )
                )
                continue
            if effect.status_enchant_id and (
                program.effect.status_enchant_id != effect.status_enchant_id
            ):
                raise NiaTurnStartContractError(
                    f"status enchant mapping mismatch: {effect.effect_id!r}"
                )
            from .nia_status_enchant import install_nia_status_enchant_programs

            install = install_nia_status_enchant_programs(
                statuses,
                (program,),
                instance_prefix=(
                    f"nia-turn:{current_turn}:priority:{step.priority}"
                ),
                block_add_status=block_add_status,
            )
            record = install.installations[0]
            statuses = install.active
            if record.installed and status_uid_cursor is not None:
                if status_uid_cursor >= 2**31 - 1:
                    raise NiaTurnStartContractError(
                        "status UID cursor overflow"
                    )
                assert record.instance_id is not None
                statuses = tuple(
                    replace(status, native_uid=status_uid_cursor)
                    if status.instance_id == record.instance_id
                    else status
                    for status in statuses
                )
                status_uid_cursor += 1
            decisions.append(
                NiaTurnStartDecision(
                    step,
                    True,
                    effect.effect_type,
                    True,
                    record.installed,
                    (
                        "installed-listener-executable"
                        if record.installed and record.listener_executable
                        else "installed-listener-pending-trigger-support"
                        if record.installed
                        else "blocked-add-status"
                    ),
                    record.listener_executable,
                    record.listener_errors,
                )
            )
            continue
        if effect.effect_type == ADD_GROW_EFFECT_TYPE:
            program = grow_programs.get(effect.effect_id)
            if program is None:
                decisions.append(
                    NiaTurnStartDecision(
                        step,
                        True,
                        effect.effect_type,
                        False,
                        False,
                        "effect-primitive-not-implemented",
                    )
                )
                continue
            if program.effect.effect_id != effect.effect_id:
                raise NiaTurnStartContractError(
                    f"AddGrow mapping mismatch: {effect.effect_id!r}"
                )
            if deck_state is None:
                decisions.append(
                    NiaTurnStartDecision(
                        step,
                        True,
                        effect.effect_type,
                        False,
                        False,
                        "deck-all-state-missing",
                    )
                )
                continue
            mutation = execute_nia_add_grow_program(deck_state, program)
            deck_state = mutation.state
            grow_mutations.extend(mutation.mutations)
            decisions.append(
                NiaTurnStartDecision(
                    step,
                    True,
                    effect.effect_type,
                    True,
                    True,
                    "applied",
                )
            )
            continue
        if effect.effect_type != LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
            decisions.append(
                NiaTurnStartDecision(
                    step,
                    True,
                    effect.effect_type,
                    False,
                    False,
                    "effect-primitive-not-implemented",
                )
            )
            continue

        mutation = execute_master_effect(
            state,
            effect.lesson_value_multiple(),
            block_add_status=block_add_status,
            new_status_uid=status_uid_cursor,
        )
        if (
            status_uid_cursor is not None
            and mutation.installed
            and mutation.merged_index is None
        ):
            if status_uid_cursor >= 2**31 - 1:
                raise NiaTurnStartContractError("status UID cursor overflow")
            status_uid_cursor += 1
        state = mutation.state
        decisions.append(
            NiaTurnStartDecision(
                step,
                True,
                effect.effect_type,
                True,
                mutation.installed,
                "installed" if mutation.installed else "blocked-add-status",
            )
        )
    return NiaTurnStartExecution(
        state,
        tuple(decisions),
        statuses,
        deck_state,
        tuple(grow_mutations),
        status_uid_cursor,
    )


def inspect_nia_turn_start_profile(
    steps: Sequence[NiaGimmickStep],
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaTurnStartProfileCapability:
    """Validate static support for every row without claiming search coverage."""

    normalized = tuple(steps)
    effects: dict[str, NiaTurnStartMasterEffect] = {}
    effect_load_errors: dict[str, str] = {}
    for effect_id in dict.fromkeys(step.effect_id for step in normalized):
        try:
            effects.update(
                load_nia_turn_start_master_effects(
                    (effect_id,), database=database
                )
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            effect_load_errors[effect_id] = type(error).__name__

    results: list[NiaTurnStartStepCapability] = []
    for step in normalized:
        blockers: list[str] = []
        if (
            step.remaining_turn_permille != 0
            or step.remaining_turn != 0
            or step.start_turn <= 0
        ):
            blockers.append("schedule-shape")
        try:
            validate_nia_turn_start_condition_shape(step)
        except (TypeError, ValueError):
            blockers.append("condition-shape")

        effect = effects.get(step.effect_id)
        effect_type = None if effect is None else effect.effect_type
        listener_backend: str | None = None
        if effect is None:
            blockers.append(
                "master-effect:" + effect_load_errors.get(step.effect_id, "missing")
            )
        else:
            try:
                if effect.effect_type == LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
                    effect.lesson_value_multiple()
                elif effect.effect_type == BLOCK_EFFECT_TYPE:
                    from .plan3_engine import _effect_shape_errors, load_plan3_effect
                    full_effect = load_plan3_effect(effect.effect_id, Path(database))
                    errors = _effect_shape_errors(full_effect)
                    if errors:
                        blockers.extend("shared-effect:" + error for error in errors)
                elif effect.effect_type == ADD_GROW_EFFECT_TYPE:
                    load_nia_add_grow_program(
                        effect.effect_id,
                        database=database,
                        master_dir=master_dir,
                    )
                elif effect.effect_type == STATUS_ENCHANT_EFFECT_TYPE:
                    from .nia_status_enchant import load_nia_status_enchant_program

                    program = load_nia_status_enchant_program(
                        effect.effect_id,
                        database=database,
                        master_dir=master_dir,
                    )
                    listener_backend = program.listener_backend
                    if not program.listener_executable:
                        blockers.extend(
                            f"listener:{code}" for code in program.listener_errors
                        )
                else:
                    blockers.append(f"effect-type:{effect.effect_type}")
            except (KeyError, OSError, TypeError, ValueError) as error:
                blockers.append(f"effect-program:{type(error).__name__}")
        results.append(
            NiaTurnStartStepCapability(
                step=step,
                effect_type=effect_type,
                status_enchant_id=(
                    None
                    if effect is None or not effect.status_enchant_id
                    else effect.status_enchant_id
                ),
                executable=not blockers,
                listener_backend=listener_backend,
                blocker_codes=tuple(dict.fromkeys(blockers)),
            )
        )

    blocker_codes = tuple(
        dict.fromkeys(code for result in results for code in result.blocker_codes)
    )
    executable_steps = sum(result.executable for result in results)
    return NiaTurnStartProfileCapability(
        total_steps=len(results),
        executable_steps=executable_steps,
        effect_layer_executable=(executable_steps == len(results)),
        blocker_codes=blocker_codes,
        steps=tuple(results),
    )


def load_nia_turn_start_master_effects(
    effect_ids: Iterable[str],
    *,
    database: Path = DEFAULT_DATABASE,
) -> dict[str, NiaTurnStartMasterEffect]:
    """Load an exact, duplicate-free effect mapping from imported Master."""

    ids = tuple(dict.fromkeys(effect_ids))
    if any(not isinstance(item, str) or not item for item in ids):
        raise TypeError("effect_ids must contain non-empty text")
    if not ids:
        return {}

    uri = f"file:{Path(database).resolve().as_posix()}?mode=ro"
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(uri, uri=True) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(effect)")
        }
        status_column = (
            "status_enchant_id"
            if "status_enchant_id" in columns
            else "'' AS status_enchant_id"
        )
        rows = connection.execute(
            f"""
            SELECT id, effect_type, value1, effect_turn, {status_column}
            FROM effect
            WHERE id IN ({placeholders})
            """,
            ids,
        ).fetchall()
    result = {
        row_id: NiaTurnStartMasterEffect(
            effect_id=row_id,
            effect_type=effect_type,
            value1=value1,
            effect_turn=effect_turn,
            status_enchant_id=status_enchant_id,
        )
        for row_id, effect_type, value1, effect_turn, status_enchant_id in rows
    }
    missing = tuple(item for item in ids if item not in result)
    if missing:
        raise KeyError(f"Master effects not found: {missing!r}")
    return result


def execute_nia_turn_start_from_master(
    steps: Sequence[NiaGimmickStep],
    *,
    current_turn: int,
    counters: NiaTurnStartCounters,
    multiplier_state: LessonParameterMultipleState | None = None,
    deck_all_state: NiaDeckAllState | None = None,
    active_status_enchants: Sequence[ActivePlan3StatusEnchant] = (),
    block_add_status: bool = False,
    next_status_uid: int | None = None,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
    scalar_effect_handler: Callable[..., None] | None = None,
) -> NiaTurnStartExecution:
    """Convenience join from scheduled steps to the local Master database."""

    scheduled = scheduled_nia_turn_start_steps(steps, current_turn)
    effects = load_nia_turn_start_master_effects(
        (step.effect_id for step in scheduled), database=database
    )
    from .nia_status_enchant import load_nia_status_enchant_program

    status_programs = {
        effect_id: load_nia_status_enchant_program(
            effect_id, database=database, master_dir=master_dir
        )
        for effect_id, effect in effects.items()
        if effect.effect_type == STATUS_ENCHANT_EFFECT_TYPE
    }
    grow_programs = {
        effect_id: load_nia_add_grow_program(
            effect_id, database=database, master_dir=master_dir
        )
        for effect_id, effect in effects.items()
        if effect.effect_type == ADD_GROW_EFFECT_TYPE
    }
    return execute_nia_turn_start_gimmicks(
        scheduled,
        current_turn=current_turn,
        counters=counters,
        effects=effects,
        multiplier_state=multiplier_state,
        deck_all_state=deck_all_state,
        add_grow_programs=grow_programs,
        status_enchant_programs=status_programs,
        active_status_enchants=active_status_enchants,
        block_add_status=block_add_status,
        next_status_uid=next_status_uid,
        scalar_effect_handler=scalar_effect_handler,
    )


__all__ = [
    "CHECK_NOT",
    "CHECK_UNKNOWN",
    "ADD_GROW_EFFECT_TYPE",
    "FIELD_CONCENTRATION_CHANGE_COUNT_UP",
    "FIELD_FULL_POWER_POINT_GET_SUM_UP",
    "FIELD_PRESERVATION_CHANGE_COUNT_UP",
    "FIELD_UNKNOWN",
    "NiaTurnStartContractError",
    "NiaTurnStartCounters",
    "NiaTurnStartDecision",
    "NiaTurnStartExecution",
    "NiaTurnStartMasterEffect",
    "NiaTurnStartProfileCapability",
    "NiaTurnStartStepCapability",
    "STATUS_ENCHANT_EFFECT_TYPE",
    "SUPPORTED_COUNTER_FIELDS",
    "evaluate_nia_turn_start_condition",
    "execute_nia_turn_start_from_master",
    "execute_nia_turn_start_gimmicks",
    "inspect_nia_turn_start_profile",
    "load_nia_turn_start_master_effects",
    "scheduled_nia_turn_start_steps",
    "validate_nia_turn_start_condition_shape",
]
