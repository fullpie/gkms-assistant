"""Strict completed-drink command-history replay for Plan 3 LocalSave.

The visible GUID zones and drink inventory are already final in this LocalSave
boundary.  Only scalar numeric effects remain encoded in the terminal native
``commandList``.  This module validates that history against ordered drink
Master and folds those effects into the scalar state shared by lesson and
audition advisors.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .master_db import DEFAULT_DATABASE
from .plan3_drink import (
    EFFECT_CONCENTRATION,
    EFFECT_FULL_POWER_POINT,
    EFFECT_PRESERVATION,
    Plan3DrinkNumericEffect,
    Plan3DrinkSelectedCardMove,
    load_plan3_drink,
)
from .plan3_drink_adapter import apply_plan3_drink_numeric_effect
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    Plan3ExamSettings,
    Plan3State,
)


# Android v3.2.3 ProduceExamEffectType values used by the supported terminal
# histories.  Preservation=46 is present in the real Green Tea LocalSave;
# Concentration=45 and FullPowerPoint=49 are fixed by the native enum/executor
# table.  Other numeric command kinds remain deliberately unsupported here.
_COMMAND_EFFECT_TYPE_BY_MASTER = {
    EFFECT_CONCENTRATION: 45,
    EFFECT_PRESERVATION: 46,
    EFFECT_FULL_POWER_POINT: 49,
}


@dataclass(frozen=True, slots=True)
class Plan3CompletedDrinkReplay:
    """One terminal Master-matched drink history folded into scalar state."""

    drink_id: str
    effect_ids: tuple[str, ...]
    after: Plan3State

    def __post_init__(self) -> None:
        if not isinstance(self.drink_id, str) or not self.drink_id:
            raise ValueError("drink_id must be non-empty text")
        if not self.effect_ids or any(
            not isinstance(value, str) or not value for value in self.effect_ids
        ):
            raise ValueError("effect_ids must contain non-empty text")
        if not isinstance(self.after, Plan3State):
            raise TypeError("after must be Plan3State")


def _empty_exam_card(playing: object) -> bool:
    if not isinstance(playing, Mapping) or playing.get("_guid") != "":
        return False
    card_data = playing.get("_cardData")
    return isinstance(card_data, Mapping) and card_data.get("_id") == ""


def _empty_playing_card(command: Mapping[str, object]) -> bool:
    return _empty_exam_card(command.get("_playingCard"))


def _command_drink_id(command: Mapping[str, object]) -> str:
    playing = command.get("_playingDrink")
    if not isinstance(playing, Mapping):
        return ""
    drink_id = playing.get("_id")
    return drink_id if isinstance(drink_id, str) else ""


def _serialized_effect_matches(
    serialized: Mapping[str, object],
    effect: Plan3DrinkNumericEffect,
) -> bool:
    effect_type = _COMMAND_EFFECT_TYPE_BY_MASTER.get(effect.effect_type)
    if effect_type is None:
        return False
    return bool(
        serialized.get("_id") == effect.id
        and serialized.get("_effectType") == effect_type
        and serialized.get("_effectValue1") == effect.value1
        and serialized.get("_effectValue2") == effect.value2
        and serialized.get("_effectCount") == effect.effect_count
        and serialized.get("_effectTurn") == effect.effect_turn
        and serialized.get("_chainEffectId", "") == effect.chain_effect_id
        and serialized.get("_statusEnchantId", "")
        == effect.status_enchant_id
        and serialized.get("_chainEffectIdList", []) == []
        and serialized.get("_cardGrowEffectIdList", []) == []
    )


def replay_completed_plan3_drink_history(
    state: Plan3State,
    raw_exam_save: Mapping[str, object],
    *,
    settings: Plan3ExamSettings,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> Plan3CompletedDrinkReplay | None:
    """Fold one exact ``effects -> consume -> terminal`` drink history.

    Static drink Master determines both effect identity and order.  A single
    selected-card move is allowed because its GUID-zone mutation is already
    present in the settled LocalSave; only the remaining numeric commands are
    replayed.  Unsupported effect kinds, reordered commands, incomplete
    histories, transition markers, or any unmatched serialized value return
    ``None`` rather than inferring state.
    """

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(raw_exam_save, Mapping):
        raise TypeError("raw_exam_save must be a mapping")
    if not isinstance(settings, Plan3ExamSettings):
        raise TypeError("settings must be Plan3ExamSettings")
    raw_commands = raw_exam_save.get("commandList")
    if not isinstance(raw_commands, list) or len(raw_commands) < 3:
        return None
    if not all(isinstance(value, Mapping) for value in raw_commands):
        return None
    commands = tuple(raw_commands)
    if not all(_empty_playing_card(command) for command in commands):
        return None
    if any(
        command.get("_isCardSelect") is not False
        or command.get("_isCardSelect2") is not False
        or command.get("_isPlayingMoveCardEffect") is not False
        for command in commands
    ):
        return None

    terminal = commands[-1]
    if terminal.get("_playType") != 9 or _command_drink_id(terminal):
        return None
    consumes = [
        command for command in commands[:-1] if command.get("_playType") == 10
    ]
    if len(consumes) != 1:
        return None
    consume_index = commands.index(consumes[0])
    if consume_index != len(commands) - 2:
        return None
    drink_id = _command_drink_id(consumes[0])
    if not drink_id:
        return None
    effect_commands = commands[:consume_index]
    if not effect_commands or any(
        command.get("_playType") != 5
        or _command_drink_id(command) != drink_id
        for command in effect_commands
    ):
        return None

    if raw_exam_save.get("drawCardGuidList") != []:
        return None
    if raw_exam_save.get("isTurnCardPlayEnd") is not False:
        return None
    if raw_exam_save.get("isTurnCardGrave") is not False:
        return None
    if raw_exam_save.get("isTurnCardLost") is not False:
        return None
    if raw_exam_save.get("isExamEndComplete") is not False:
        return None
    if raw_exam_save.get("removedCardList") != []:
        return None
    if not _empty_exam_card(raw_exam_save.get("playingCard")):
        return None

    drink = load_plan3_drink(
        drink_id,
        master_dir=Path(master_dir),
        database=Path(database),
    )
    if sum(
        isinstance(effect, Plan3DrinkSelectedCardMove)
        for effect in drink.effects
    ) > 1:
        return None
    if any(
        not isinstance(effect, (Plan3DrinkNumericEffect, Plan3DrinkSelectedCardMove))
        for effect in drink.effects
    ):
        return None
    numeric_effects = tuple(
        effect
        for effect in drink.effects
        if isinstance(effect, Plan3DrinkNumericEffect)
    )
    if len(numeric_effects) != len(effect_commands):
        return None

    serialized_effects: list[Mapping[str, object]] = []
    for command in effect_commands:
        value = command.get("_playEffect")
        if not isinstance(value, Mapping):
            return None
        serialized_effects.append(value)
    if not all(
        _serialized_effect_matches(serialized, effect)
        for serialized, effect in zip(
            serialized_effects,
            numeric_effects,
            strict=True,
        )
    ):
        return None

    working = state
    for effect in numeric_effects:
        working = apply_plan3_drink_numeric_effect(
            working,
            effect,
            settings=settings,
        ).after
    return Plan3CompletedDrinkReplay(
        drink_id=drink_id,
        effect_ids=tuple(effect.id for effect in numeric_effects),
        after=working,
    )


__all__ = [
    "Plan3CompletedDrinkReplay",
    "replay_completed_plan3_drink_history",
]
