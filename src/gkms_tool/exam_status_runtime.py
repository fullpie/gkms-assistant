"""Table-driven runtime for persistent exam status enchants.

This module interprets only explicitly whitelisted trigger/effect shapes.  It
is shared by memory/support passives and the later Plan3 engine; unknown Master
rows are returned as blocking diagnostics rather than treated as no-ops.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_exact_target_card_search,
)
from .master_db import DEFAULT_DATABASE


PHASE_TURN_TIMER = "ProduceExamPhaseType_ExamTurnTimer"
PHASE_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
PHASE_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"

EFFECT_STATUS_ENCHANT = "ProduceExamEffectType_ExamStatusEnchant"
EFFECT_PLAYABLE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_CARD_DRAW = "ProduceExamEffectType_ExamCardDraw"
EFFECT_MOTIVATION = "ProduceExamEffectType_ExamCardPlayAggressive"
EFFECT_GOOD_IMPRESSION = "ProduceExamEffectType_ExamReview"
EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
EFFECT_STAMINA_DOWN = "ProduceExamEffectType_ExamStaminaConsumptionDown"
EFFECT_GOOD_IMPRESSION_GAIN = "ProduceExamEffectType_ExamReviewAdditive"
EFFECT_MOTIVATION_GAIN = "ProduceExamEffectType_ExamAggressiveAdditive"
EFFECT_CARD_MOVE = "ProduceExamEffectType_ExamCardMove"

SLEEP_CARD_MOVE_TO_LOST_PREFIX = (
    "e_effect-exam_card_move-p_card_search-deck_grave-"
    "p_card-00-acc-0_002-lost-random-"
)


@dataclass(frozen=True, slots=True)
class ExamStatusTrigger:
    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_card_search_ids: tuple[str, ...]
    effect_types: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    lesson_type: str
    target_card_ids: tuple[str, ...]
    card_search_rule: ProduceCardSearchRule | None = None


@dataclass(frozen=True, slots=True)
class RuntimeExamEffect:
    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str


@dataclass(frozen=True, slots=True)
class RuntimeStatusEnchant:
    id: str
    trigger: ExamStatusTrigger
    effects: tuple[RuntimeExamEffect, ...]


@dataclass(frozen=True, slots=True)
class StatusRuntimeResolution:
    fired_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()
    added_enchants: tuple[tuple[str, int], ...] = ()
    use_counts: tuple[tuple[str, int], ...] = ()
    playable_add: int = 0
    draw_count: int = 0
    motivation_add: int = 0
    good_impression_add: int = 0
    block_base_add: int = 0
    block_effect_count: int = 0
    stamina_consumption_down_turns: int = 0
    good_impression_gain_layers: tuple[tuple[int, int], ...] = ()
    motivation_gain_layers: tuple[tuple[int, int], ...] = ()
    trouble_cards_moved_to_lost: int = 0
    unsupported_rules: tuple[str, ...] = ()

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported_rules


def _string_tuple(raw: str) -> tuple[str, ...]:
    values = json.loads(raw)
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("Master trigger string array is invalid")
    return tuple(values)


def _integer_tuple(raw: str) -> tuple[int, ...]:
    values = json.loads(raw)
    if not isinstance(values, list) or not all(isinstance(item, int) for item in values):
        raise ValueError("Master trigger integer array is invalid")
    return tuple(values)


@lru_cache(maxsize=1024)
def load_exam_status_trigger(
    trigger_id: str,
    database: Path = DEFAULT_DATABASE,
) -> ExamStatusTrigger:
    with closing(sqlite3.connect(Path(database))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"Master exam trigger not found: {trigger_id}")
    search_id = str(row["produce_card_search_id"])
    search_rule = (
        load_produce_card_search(search_id, Path(database))
        if search_id
        else None
    )
    return ExamStatusTrigger(
        id=str(row["id"]),
        phase_types=_string_tuple(row["phase_types_json"]),
        phase_values=_integer_tuple(row["phase_values_json"]),
        field_status_check_types=_string_tuple(
            row["field_status_check_types_json"]
        ),
        field_status_types=_string_tuple(row["field_status_types_json"]),
        field_status_values=_integer_tuple(row["field_status_values_json"]),
        field_status_card_search_ids=_string_tuple(
            row["field_status_produce_card_search_ids_json"]
        ),
        effect_types=_string_tuple(row["effect_types_json"]),
        produce_card_search_id=search_id,
        upper_search_count=int(row["upper_search_count"]),
        lower_search_count=int(row["lower_search_count"]),
        card_move_position_type=str(row["card_move_position_type"]),
        lesson_type=str(row["lesson_type"]),
        target_card_ids=(search_rule.produce_card_ids if search_rule else ()),
        card_search_rule=search_rule,
    )


@lru_cache(maxsize=1024)
def load_runtime_status_enchant(
    enchant_id: str,
    database: Path = DEFAULT_DATABASE,
) -> RuntimeStatusEnchant:
    with closing(sqlite3.connect(Path(database))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """SELECT produce_exam_trigger_id, produce_exam_effect_ids_json
                 FROM produce_exam_status_enchant WHERE id = ?""",
            (enchant_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Master status enchant not found: {enchant_id}")
        effect_ids = json.loads(row["produce_exam_effect_ids_json"])
        if not isinstance(effect_ids, list):
            raise ValueError(f"status enchant effect list is invalid: {enchant_id}")
        effects = []
        for effect_id in effect_ids:
            effect = connection.execute(
                """SELECT id, effect_type, value1, value2, effect_count,
                          effect_turn, status_enchant_id, chain_effect_id
                     FROM effect WHERE id = ?""",
                (effect_id,),
            ).fetchone()
            if effect is None:
                raise KeyError(f"Master status effect not found: {effect_id}")
            effects.append(RuntimeExamEffect(*tuple(effect)))
    return RuntimeStatusEnchant(
        id=enchant_id,
        trigger=load_exam_status_trigger(str(row["produce_exam_trigger_id"]), Path(database)),
        effects=tuple(effects),
    )


def _trigger_matches(
    trigger: ExamStatusTrigger,
    *,
    phase: str,
    round_number: int,
    played_card_id: str | None,
    played_card_upgrade: int | None,
    changed_effect_type: str | None = None,
    lesson_type: str = "ProduceStepLessonType_Unknown",
) -> tuple[bool, str | None]:
    if phase not in trigger.phase_types:
        return False, None
    unsupported_fields = bool(
        trigger.field_status_check_types
        or trigger.field_status_types
        or trigger.field_status_values
        or trigger.field_status_card_search_ids
        or (trigger.effect_types and phase != PHASE_STATUS_CHANGE)
        or trigger.card_move_position_type
        != "ProduceCardMovePositionType_Unknown"
        or (trigger.lesson_type != "ProduceStepLessonType_Unknown"
            and phase != PHASE_STATUS_CHANGE)
    )
    if unsupported_fields:
        return False, f"status-trigger-conditions:{trigger.id}"
    if phase == PHASE_TURN_TIMER:
        if len(trigger.phase_values) != 1:
            return False, f"status-trigger-timer:{trigger.id}"
        return round_number == trigger.phase_values[0], None
    if phase == PHASE_START_TURN:
        if trigger.phase_values or trigger.produce_card_search_id:
            return False, f"status-trigger-start-turn:{trigger.id}"
        return True, None
    if phase == PHASE_CARD_PLAY_AFTER:
        if trigger.phase_values or trigger.card_search_rule is None:
            return False, f"status-trigger-card-target:{trigger.id}"
        if trigger.lower_search_count != 1 or trigger.upper_search_count != 0:
            return False, f"status-trigger-card-count:{trigger.id}"
        if played_card_id is None:
            return False, f"status-trigger-card-missing:{trigger.id}"
        if played_card_upgrade is None:
            if trigger.card_search_rule.upgrade_counts:
                return False, f"status-trigger-card-upgrade-unknown:{trigger.id}"
            played_card_upgrade = 0
        matched, issue = match_exact_target_card_search(
            trigger.card_search_rule,
            played_card_id,
            played_card_upgrade,
        )
        if issue is not None:
            return False, f"status-trigger-{issue}"
        return matched, None
    if phase == PHASE_STATUS_CHANGE:
        if trigger.phase_values or not trigger.effect_types:
            return False, f"status-trigger-change-shape:{trigger.id}"
        if changed_effect_type is None:
            return False, f"status-trigger-change-effect-missing:{trigger.id}"
        if changed_effect_type not in trigger.effect_types:
            return False, None
        if trigger.lesson_type != "ProduceStepLessonType_Unknown":
            if lesson_type == "ProduceStepLessonType_Unknown":
                return False, f"status-trigger-change-lesson-unknown:{trigger.id}"
            if trigger.lesson_type != lesson_type:
                return False, None
        return True, None
    return False, f"status-trigger-phase:{trigger.id}:{phase}"


def resolve_status_enchants(
    enchant_ids: Sequence[str],
    *,
    phase: str,
    round_number: int,
    played_card_id: str | None = None,
    played_card_upgrade: int | None = None,
    changed_effect_type: str | None = None,
    lesson_type: str = "ProduceStepLessonType_Unknown",
    limits: Mapping[str, int] | None = None,
    uses: Mapping[str, int] | None = None,
    database: Path = DEFAULT_DATABASE,
) -> StatusRuntimeResolution:
    """Resolve one exam phase for installed enchants.

    A limit of zero or a missing entry means unlimited.  The returned use map
    is complete and can be written back into the shadow state.
    """

    limit_map = {str(key): int(value) for key, value in (limits or {}).items()}
    use_map = {str(key): int(value) for key, value in (uses or {}).items()}
    fired_enchants: list[str] = []
    fired_effects: list[str] = []
    added_enchants: list[tuple[str, int]] = []
    unsupported: list[str] = []
    playable_add = draw_count = motivation_add = good_impression_add = 0
    block_base_add = block_effect_count = stamina_down = trouble_moved = 0
    good_gain: list[tuple[int, int]] = []
    motivation_gain: list[tuple[int, int]] = []

    for enchant_id in enchant_ids:
        limit = limit_map.get(enchant_id, 0)
        used = use_map.get(enchant_id, 0)
        if limit > 0 and used >= limit:
            continue
        enchant = load_runtime_status_enchant(enchant_id, Path(database))
        matches, trigger_issue = _trigger_matches(
            enchant.trigger,
            phase=phase,
            round_number=round_number,
            played_card_id=played_card_id,
            played_card_upgrade=played_card_upgrade,
            changed_effect_type=changed_effect_type,
            lesson_type=lesson_type,
        )
        if trigger_issue is not None:
            unsupported.append(trigger_issue)
            continue
        if not matches:
            continue
        local_unsupported: list[str] = []
        for effect in enchant.effects:
            if effect.chain_effect_id:
                local_unsupported.append(f"status-chain:{effect.chain_effect_id}")
                continue
            if effect.effect_type == EFFECT_STATUS_ENCHANT:
                if not effect.status_enchant_id or effect.effect_turn != -1:
                    local_unsupported.append(f"status-install:{effect.id}")
                else:
                    added_enchants.append(
                        (effect.status_enchant_id, max(0, effect.effect_count))
                    )
            elif effect.status_enchant_id:
                local_unsupported.append(f"status-nested:{effect.id}")
            elif effect.effect_type == EFFECT_PLAYABLE_ADD:
                playable_add += max(1, effect.value1, effect.effect_count)
            elif effect.effect_type == EFFECT_CARD_DRAW:
                draw_count += max(1, effect.value1, effect.effect_count)
            elif effect.effect_type == EFFECT_MOTIVATION:
                motivation_add += effect.value1 * max(1, effect.effect_count)
            elif effect.effect_type == EFFECT_GOOD_IMPRESSION:
                good_impression_add += effect.value1 * max(1, effect.effect_count)
            elif effect.effect_type == EFFECT_BLOCK:
                block_base_add += effect.value1 * max(1, effect.effect_count)
                block_effect_count += max(1, effect.effect_count)
            elif effect.effect_type == EFFECT_STAMINA_DOWN:
                if effect.effect_turn <= 0:
                    local_unsupported.append(f"status-duration:{effect.id}")
                else:
                    stamina_down += effect.effect_turn
            elif effect.effect_type == EFFECT_GOOD_IMPRESSION_GAIN:
                if effect.value1 <= 0 or effect.effect_turn <= 0:
                    local_unsupported.append(f"status-duration:{effect.id}")
                else:
                    good_gain.append((effect.value1, effect.effect_turn))
            elif effect.effect_type == EFFECT_MOTIVATION_GAIN:
                if effect.value1 <= 0 or effect.effect_turn <= 0:
                    local_unsupported.append(f"status-duration:{effect.id}")
                else:
                    motivation_gain.append((effect.value1, effect.effect_turn))
            elif effect.effect_type == EFFECT_CARD_MOVE:
                if not effect.id.startswith(SLEEP_CARD_MOVE_TO_LOST_PREFIX):
                    local_unsupported.append(f"status-card-move:{effect.id}")
                else:
                    trouble_moved += 1
            else:
                local_unsupported.append(
                    f"status-effect:{effect.effect_type}:{effect.id}"
                )
        fired_enchants.append(enchant_id)
        fired_effects.extend(effect.id for effect in enchant.effects)
        use_map[enchant_id] = used + 1
        unsupported.extend(local_unsupported)

    return StatusRuntimeResolution(
        fired_enchant_ids=tuple(fired_enchants),
        fired_effect_ids=tuple(fired_effects),
        added_enchants=tuple(added_enchants),
        use_counts=tuple(sorted(use_map.items())),
        playable_add=playable_add,
        draw_count=draw_count,
        motivation_add=motivation_add,
        good_impression_add=good_impression_add,
        block_base_add=block_base_add,
        block_effect_count=block_effect_count,
        stamina_consumption_down_turns=stamina_down,
        good_impression_gain_layers=tuple(good_gain),
        motivation_gain_layers=tuple(motivation_gain),
        trouble_cards_moved_to_lost=trouble_moved,
        unsupported_rules=tuple(dict.fromkeys(unsupported)),
    )
