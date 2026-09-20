"""NIA v3.2.3 turn-start status-enchant installation adapter.

Installation and later listener execution are deliberately separate.  All
current NIA rows have an exact infinite stacking installation shape.  Two
listener shapes use the Plan 3 trigger kernel; the three exact play-count
interval shapes use the standalone NIA native adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .master_db import DEFAULT_DATABASE
from .nia_add_grow_effect import (
    DEFAULT_MASTER_DIR,
    NiaAddGrowProgram,
    load_nia_add_grow_program,
)
from .plan3_engine import (
    EFFECT_STATUS_ENCHANT,
    LESSON_UNKNOWN,
    MOVE_UNKNOWN,
    ActivePlan3StatusEnchant,
    Plan3Effect,
    Plan3StatusEnchantRule,
    load_plan3_effect,
    validate_plan3_status_enchant,
)


PHASE_PLAY_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
NIA_PLAY_COUNT_INTERVALS = frozenset((3, 5))


class NiaStatusEnchantContractError(ValueError):
    """A Master/runtime input is outside the exact NIA install profile."""


@dataclass(frozen=True, slots=True)
class NiaStatusEnchantProgram:
    """One exact outer installer and its ordered nested DeckAll programs."""

    effect: Plan3Effect
    nested_add_grow_programs: tuple[NiaAddGrowProgram, ...]
    listener_errors: tuple[str, ...]
    plan3_listener_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.effect, Plan3Effect):
            raise TypeError("effect must be Plan3Effect")
        nested = tuple(self.nested_add_grow_programs)
        if not all(isinstance(value, NiaAddGrowProgram) for value in nested):
            raise TypeError(
                "nested_add_grow_programs must contain NiaAddGrowProgram values"
            )
        errors = tuple(self.listener_errors)
        if any(not isinstance(value, str) or not value for value in errors):
            raise TypeError("listener_errors must contain non-empty text")
        plan3_errors = tuple(self.plan3_listener_errors)
        if any(not isinstance(value, str) or not value for value in plan3_errors):
            raise TypeError("plan3_listener_errors must contain non-empty text")
        object.__setattr__(self, "nested_add_grow_programs", nested)
        object.__setattr__(self, "listener_errors", errors)
        object.__setattr__(self, "plan3_listener_errors", plan3_errors)
        self.assert_exact_install_shape()

    @property
    def listener_executable(self) -> bool:
        return not self.listener_errors

    @property
    def listener_backend(self) -> str:
        if self.listener_errors:
            return "unsupported"
        rule = self.effect.status_enchant
        if (
            rule is not None
            and rule.trigger.phase_types == (PHASE_PLAY_COUNT_INTERVAL,)
        ):
            # The shared scalar validator may understand the trigger shape,
            # but NIA owns the GUID DeckAll mutation and phase counter at the
            # accepted-play boundary for both interval 3 and interval 5.
            return "nia-play-count-interval"
        if not self.plan3_listener_errors:
            return "plan3"
        return "nia-play-count-interval"

    def assert_exact_install_shape(self) -> None:
        effect = self.effect
        if not (
            effect.effect_type == EFFECT_STATUS_ENCHANT
            and effect.value1 == 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == -1
            and effect.status_enchant_id
            and effect.status_enchant is not None
            and effect.status_enchant.id == effect.status_enchant_id
            and not effect.chain_effect_id
            and effect.trigger is None
            and not effect.once
            and effect.card_move_rule is None
            and effect.status_enchant.effects
            and tuple(
                program.effect.effect_id
                for program in self.nested_add_grow_programs
            )
            == tuple(item.id for item in effect.status_enchant.effects)
        ):
            raise NiaStatusEnchantContractError(
                f"unsupported NIA status install shape: {effect.id}"
            )


@dataclass(frozen=True, slots=True)
class NiaStatusEnchantInstallation:
    effect_id: str
    status_enchant_id: str
    instance_id: str | None
    installed: bool
    listener_executable: bool
    listener_errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NiaStatusEnchantInstallExecution:
    active: tuple[ActivePlan3StatusEnchant, ...]
    installations: tuple[NiaStatusEnchantInstallation, ...]


def nia_play_count_interval_listener_errors(
    rule: Plan3StatusEnchantRule,
) -> tuple[str, ...]:
    """Validate the exact unfiltered NIA v3.2.3 interval-listener shape."""

    if not isinstance(rule, Plan3StatusEnchantRule):
        raise TypeError("rule must be Plan3StatusEnchantRule")
    trigger = rule.trigger
    errors: list[str] = []
    if trigger.phase_types != (PHASE_PLAY_COUNT_INTERVAL,):
        errors.append(f"interval-phase:{trigger.id}")
    if (
        len(trigger.phase_values) != 1
        or trigger.phase_values[0] not in NIA_PLAY_COUNT_INTERVALS
    ):
        errors.append(f"interval-value:{trigger.id}")
    if (
        trigger.field_check_types
        or trigger.field_types
        or trigger.field_values
        or trigger.field_card_search_ids
    ):
        errors.append(f"interval-field-filter:{trigger.id}")
    if trigger.produce_card_search_id:
        errors.append(f"interval-card-search:{trigger.id}")
    if trigger.upper_search_count or trigger.lower_search_count:
        errors.append(f"interval-search-count:{trigger.id}")
    if trigger.card_move_position_type != MOVE_UNKNOWN:
        errors.append(f"interval-move-position:{trigger.id}")
    if trigger.effect_types:
        errors.append(f"interval-effect-filter:{trigger.id}")
    if trigger.lesson_type != LESSON_UNKNOWN:
        errors.append(f"interval-lesson-type:{trigger.id}")
    return tuple(errors)


def load_nia_status_enchant_program(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaStatusEnchantProgram:
    """Resolve one NIA installer, listener, and ordered nested AddGrow rows."""

    if not isinstance(effect_id, str) or not effect_id:
        raise TypeError("effect_id must be non-empty text")
    effect = load_plan3_effect(effect_id, database=database)
    if effect.status_enchant is None:
        raise NiaStatusEnchantContractError(
            f"effect has no resolved status enchant: {effect_id}"
        )
    nested = tuple(
        load_nia_add_grow_program(
            item.id, database=database, master_dir=master_dir
        )
        for item in effect.status_enchant.effects
    )
    plan3_errors = validate_plan3_status_enchant(
        effect.status_enchant, allow_native_grow=True
    )
    interval_errors = nia_play_count_interval_listener_errors(
        effect.status_enchant
    )
    errors = () if not interval_errors else plan3_errors
    return NiaStatusEnchantProgram(effect, nested, errors, plan3_errors)


def install_nia_status_enchant_programs(
    active: Iterable[ActivePlan3StatusEnchant],
    programs: Iterable[NiaStatusEnchantProgram],
    *,
    instance_prefix: str = "nia-gimmick",
    block_add_status: bool = False,
) -> NiaStatusEnchantInstallExecution:
    """Append caller/priority-ordered non-unique native listener instances."""

    if isinstance(active, (str, bytes)):
        raise TypeError("active must be an iterable of status instances")
    if isinstance(programs, (str, bytes)):
        raise TypeError("programs must be an iterable of status programs")
    if not isinstance(instance_prefix, str) or not instance_prefix:
        raise TypeError("instance_prefix must be non-empty text")
    if not isinstance(block_add_status, bool):
        raise TypeError("block_add_status must be bool")
    current = tuple(active)
    if not all(isinstance(value, ActivePlan3StatusEnchant) for value in current):
        raise TypeError("active must contain ActivePlan3StatusEnchant values")
    ids = [value.instance_id for value in current]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise NiaStatusEnchantContractError(
            "active status instance IDs must be non-empty and unique"
        )
    existing = set(ids)
    installed = list(current)
    records: list[NiaStatusEnchantInstallation] = []
    for program in tuple(programs):
        if not isinstance(program, NiaStatusEnchantProgram):
            raise TypeError("programs must contain NiaStatusEnchantProgram values")
        effect = program.effect
        if block_add_status:
            records.append(
                NiaStatusEnchantInstallation(
                    effect.id,
                    effect.status_enchant_id,
                    None,
                    False,
                    program.listener_executable,
                    program.listener_errors,
                )
            )
            continue

        base = f"{instance_prefix}:{effect.id}:{effect.status_enchant_id}"
        instance_id = base
        suffix = 1
        while instance_id in existing:
            suffix += 1
            instance_id = f"{base}:{suffix}"
        existing.add(instance_id)
        assert effect.status_enchant is not None
        installed.append(
            ActivePlan3StatusEnchant(
                instance_id=instance_id,
                source_id=effect.id,
                rule=effect.status_enchant,
                # Plan3 represents native -1 unlimited limits as zero.
                max_uses=0,
                max_uses_per_turn=0,
                remaining_turns=-1,
                passing_turn_start=False,
                is_item_direct=False,
            )
        )
        records.append(
            NiaStatusEnchantInstallation(
                effect.id,
                effect.status_enchant_id,
                instance_id,
                True,
                program.listener_executable,
                program.listener_errors,
            )
        )
    return NiaStatusEnchantInstallExecution(
        tuple(installed), tuple(records)
    )


__all__ = [
    "NiaStatusEnchantContractError",
    "NiaStatusEnchantInstallExecution",
    "NiaStatusEnchantInstallation",
    "NiaStatusEnchantProgram",
    "NIA_PLAY_COUNT_INTERVALS",
    "PHASE_PLAY_COUNT_INTERVAL",
    "install_nia_status_enchant_programs",
    "load_nia_status_enchant_program",
    "nia_play_count_interval_listener_errors",
]
