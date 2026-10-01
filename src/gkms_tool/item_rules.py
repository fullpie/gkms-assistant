"""Read and evaluate the verified item -> enchant -> trigger master chain."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_exact_target_card_search,
)
from .logic_engine import (
    EFFECT_MOTIVATION,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_GOOD_IMPRESSION_ADDITIVE,
    EFFECT_MOTIVATION_ADDITIVE,
    ItemStatusChangeEnchant,
    LogicExamState,
    MasterCard,
    MasterCardEffect,
    apply_logic_card,
    load_master_effect,
    scale_good_impression_gain,
)
from .master_db import DEFAULT_DATABASE


ITEM_EFFECT_STATUS_ENCHANT = "ProduceItemEffectType_ExamStatusEnchant"
PHASE_END_TURN_INTERVAL = "ProduceExamPhaseType_ExamEndTurnInterval"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
PHASE_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
PHASE_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
FIELD_CARD_SEARCH_COUNT_UP = "ProduceExamFieldStatusType_CardSearchCountUp"
FIELD_MOTIVATION_UP = "ProduceExamFieldStatusType_CardPlayAggressiveUp"
SEARCH_LOST = "p_card_search-lost"
SEARCH_ACTIVE_SKILL = "p_card_search-active_skill"
CARD_MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
LESSON_DANCE = "ProduceStepLessonType_LessonDance"


@dataclass(frozen=True, slots=True)
class ExamTriggerRule:
    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_check_types: tuple[str, ...]
    field_types: tuple[str, ...]
    field_values: tuple[int, ...]
    field_card_search_ids: tuple[str, ...]
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = CARD_MOVE_UNKNOWN
    effect_types: tuple[str, ...] = ()
    lesson_type: str = LESSON_UNKNOWN
    card_search_rule: ProduceCardSearchRule | None = None


@dataclass(frozen=True, slots=True)
class ItemEnchantment:
    id: str
    trigger: ExamTriggerRule
    effects: tuple[MasterCardEffect, ...]
    max_uses: int = 0


@dataclass(frozen=True, slots=True)
class EquippedItemRule:
    id: str
    name: str
    enchantments: tuple[ItemEnchantment, ...]
    unsupported_rules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedItemEffects:
    effects: tuple[MasterCardEffect, ...]
    fired_enchantment_ids: tuple[str, ...]
    unsupported_rules: tuple[str, ...]
    # Keep phase-specific effects separate.  Treating every item activation as
    # an end-of-turn effect loses real ExamCardPlayAfter activations whenever a
    # card grants another play in the same turn.
    post_card_effects: tuple[MasterCardEffect, ...] = ()
    end_turn_effects: tuple[MasterCardEffect, ...] = ()
    # These are supplied to the direct-effect status-change phase, not to
    # ExamCardPlayAfter.  They remain equipped-item sources with their own
    # persisted usage limits.
    status_change_enchantments: tuple[ItemStatusChangeEnchant, ...] = ()

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported_rules


def merge_equipped_item_rules(
    items: Sequence[EquippedItemRule],
    *,
    id: str = "equipped-item-merge",
    name: str = "Equipped items",
) -> EquippedItemRule:
    """Combine independently equipped item sources without losing limits.

    The returned rule deliberately preserves source and enchantment order.
    An enchant ID is also the persisted usage key, so collisions would merge
    two otherwise independent items in a later transition; reject them rather
    than guessing which source owns the use count.
    """

    if not items:
        raise ValueError("Cannot merge an empty item collection")
    if not id or not name:
        raise ValueError("Merged item id and name must be non-empty")

    item_ids: set[str] = set()
    enchantment_ids: set[str] = set()
    enchantments: list[ItemEnchantment] = []
    unsupported: list[str] = []
    for item in items:
        if item.id in item_ids:
            raise ValueError(f"Duplicate equipped item id: {item.id}")
        item_ids.add(item.id)
        for enchantment in item.enchantments:
            if enchantment.id in enchantment_ids:
                raise ValueError(
                    f"Conflicting equipped item enchantment id: {enchantment.id}"
                )
            enchantment_ids.add(enchantment.id)
            enchantments.append(enchantment)
        unsupported.extend(item.unsupported_rules)
    return EquippedItemRule(
        id=id,
        name=name,
        enchantments=tuple(enchantments),
        unsupported_rules=tuple(unsupported),
    )


@dataclass(frozen=True, slots=True)
class ItemTurnStartResolution:
    before: LogicExamState
    after: LogicExamState
    effects: tuple[MasterCardEffect, ...]
    fired_enchantment_ids: tuple[str, ...]
    unsupported_rules: tuple[str, ...]

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported_rules


def _json_list(value: str, label: str) -> list[Any]:
    payload = json.loads(value)
    if not isinstance(payload, list):
        raise ValueError(f"{label} must be a JSON array")
    return payload


def _str_tuple(value: str, label: str) -> tuple[str, ...]:
    payload = _json_list(value, label)
    if not all(isinstance(item, str) for item in payload):
        raise ValueError(f"{label} entries must be strings")
    return tuple(payload)


def _int_tuple(value: str, label: str) -> tuple[int, ...]:
    payload = _json_list(value, label)
    if not all(isinstance(item, int) and not isinstance(item, bool) for item in payload):
        raise ValueError(f"{label} entries must be integers")
    return tuple(payload)


def load_idol_item_id(
    idol_card_id: str,
    *,
    upgraded: bool = False,
    database: Path = DEFAULT_DATABASE,
) -> str:
    """Resolve the before/after item stored in an idol card's raw master row."""

    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            "SELECT raw_json FROM idol_card WHERE id = ?", (idol_card_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"Master 資料庫找不到偶像卡：{idol_card_id}")
    payload = json.loads(row[0])
    key = "afterProduceItemId" if upgraded else "beforeProduceItemId"
    item_id = payload.get(key)
    if not isinstance(item_id, str) or not item_id:
        raise KeyError(f"偶像卡沒有 {key}：{idol_card_id}")
    return item_id


def load_item_rule(
    item_id: str, database: Path = DEFAULT_DATABASE
) -> EquippedItemRule:
    """Load an item's ordered enchantments and their exam effects."""

    unsupported: list[str] = []
    pending: list[tuple[str, ExamTriggerRule, tuple[str, ...], int]] = []
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        item = connection.execute(
            """
            SELECT id, name, produce_item_effect_ids_json
              FROM produce_item
             WHERE id = ?
            """,
            (item_id,),
        ).fetchone()
        if item is None:
            raise KeyError(f"Master 資料庫找不到 P 道具：{item_id}")

        for item_effect_id in _str_tuple(
            item["produce_item_effect_ids_json"], "item.effect_ids"
        ):
            item_effect = connection.execute(
                """
                SELECT effect_type, effect_turn, effect_count,
                       produce_exam_status_enchant_id, produce_effect_id
                  FROM produce_item_effect
                 WHERE id = ?
                """,
                (item_effect_id,),
            ).fetchone()
            if item_effect is None:
                raise KeyError(f"Master 資料庫找不到 P 道具效果：{item_effect_id}")
            if item_effect["effect_type"] != ITEM_EFFECT_STATUS_ENCHANT:
                unsupported.append(
                    f"item-effect:{item_effect['effect_type']}:{item_effect_id}"
                )
                continue
            effect_turn = int(item_effect["effect_turn"])
            effect_count = int(item_effect["effect_count"])
            if effect_turn != -1 or effect_count < 0:
                unsupported.append(
                    f"item-effect-lifecycle:{item_effect_id}:"
                    f"turn={effect_turn}:count={effect_count}"
                )

            enchant_id = item_effect["produce_exam_status_enchant_id"]
            enchant = connection.execute(
                """
                SELECT produce_exam_trigger_id, produce_exam_effect_ids_json
                  FROM produce_exam_status_enchant
                 WHERE id = ?
                """,
                (enchant_id,),
            ).fetchone()
            if enchant is None:
                raise KeyError(f"Master 資料庫找不到狀態附魔：{enchant_id}")
            trigger_id = enchant["produce_exam_trigger_id"]
            trigger = connection.execute(
                "SELECT * FROM produce_exam_trigger WHERE id = ?",
                (trigger_id,),
            ).fetchone()
            if trigger is None:
                raise KeyError(f"Master 資料庫找不到考試觸發器：{trigger_id}")
            rule = ExamTriggerRule(
                id=trigger_id,
                phase_types=_str_tuple(
                    trigger["phase_types_json"], "trigger.phase_types"
                ),
                phase_values=_int_tuple(
                    trigger["phase_values_json"], "trigger.phase_values"
                ),
                field_check_types=_str_tuple(
                    trigger["field_status_check_types_json"],
                    "trigger.field_checks",
                ),
                field_types=_str_tuple(
                    trigger["field_status_types_json"], "trigger.field_types"
                ),
                field_values=_int_tuple(
                    trigger["field_status_values_json"], "trigger.field_values"
                ),
                field_card_search_ids=_str_tuple(
                    trigger["field_status_produce_card_search_ids_json"],
                    "trigger.field_card_search_ids",
                ),
                produce_card_search_id=str(trigger["produce_card_search_id"]),
                upper_search_count=int(trigger["upper_search_count"]),
                lower_search_count=int(trigger["lower_search_count"]),
                card_move_position_type=str(trigger["card_move_position_type"]),
                effect_types=_str_tuple(
                    trigger["effect_types_json"], "trigger.effect_types"
                ),
                lesson_type=str(trigger["lesson_type"]),
                card_search_rule=(
                    load_produce_card_search(
                        str(trigger["produce_card_search_id"]), database
                    )
                    if str(trigger["produce_card_search_id"])
                    else None
                ),
            )
            pending.append(
                (
                    enchant_id,
                    rule,
                    _str_tuple(enchant["produce_exam_effect_ids_json"], "enchant.effects"),
                    max(0, effect_count),
                )
            )

    enchantments = tuple(
        ItemEnchantment(
            enchant_id,
            trigger,
            tuple(load_master_effect(effect_id, database) for effect_id in effect_ids),
            max_uses,
        )
        for enchant_id, trigger, effect_ids, max_uses in pending
    )
    return EquippedItemRule(
        id=item["id"],
        name=item["name"],
        enchantments=enchantments,
        unsupported_rules=tuple(unsupported),
    )


def load_idol_item_rule(
    idol_card_id: str,
    *,
    upgraded: bool = False,
    database: Path = DEFAULT_DATABASE,
) -> EquippedItemRule:
    return load_item_rule(
        load_idol_item_id(idol_card_id, upgraded=upgraded, database=database),
        database,
    )


def _trigger_fires(
    trigger: ExamTriggerRule,
    state: LogicExamState,
    card: MasterCard | None = None,
) -> tuple[bool, tuple[str, ...]]:
    unsupported: list[str] = []
    if trigger.phase_types == (PHASE_CARD_PLAY_AFTER,):
        if card is None:
            return False, (f"trigger-card-missing:{trigger.id}",)
        if trigger.phase_values:
            return False, (f"trigger-phase-values:{trigger.id}",)
        common_shape = (
            trigger.card_move_position_type == CARD_MOVE_UNKNOWN
            and not trigger.effect_types
            and trigger.lesson_type == LESSON_UNKNOWN
            and trigger.upper_search_count == 0
            and trigger.lower_search_count == 1
        )
        if (
            common_shape
            and trigger.field_types == (FIELD_MOTIVATION_UP,)
            and len(trigger.field_values) == 1
            and trigger.field_values[0] >= 0
            and not trigger.field_check_types
            and not trigger.field_card_search_ids
            and trigger.produce_card_search_id == f"{SEARCH_ACTIVE_SKILL}-target"
        ):
            if card.category != "ProduceCardCategory_ActiveSkill":
                return False, ()
            preview = apply_logic_card(state, card)
            if not preview.legal or preview.unsupported_rules:
                return False, tuple(
                    f"trigger-preview:{rule}" for rule in preview.unsupported_rules
                )
            return preview.after.motivation >= trigger.field_values[0], ()

        if (
            common_shape
            and not trigger.field_types
            and not trigger.field_values
            and not trigger.field_check_types
            and not trigger.field_card_search_ids
            and trigger.produce_card_search_id
            and trigger.card_search_rule is not None
        ):
            matched, issue = match_exact_target_card_search(
                trigger.card_search_rule,
                card.id,
                card.upgrade,
            )
            if issue is not None:
                return False, (f"trigger-{issue}",)
            return matched, ()

        return False, (f"trigger-field:{trigger.id}",)

    if (
        trigger.phase_types != (PHASE_END_TURN_INTERVAL,)
        or len(trigger.phase_values) != 1
        or trigger.phase_values[0] <= 0
    ):
        unsupported.append(f"trigger-phase:{trigger.id}")
    elif state.round_number % trigger.phase_values[0] != 0:
        return False, ()

    widths = {
        len(trigger.field_check_types),
        len(trigger.field_types),
        len(trigger.field_values),
        len(trigger.field_card_search_ids),
    }
    if widths == {0}:
        return (not unsupported), tuple(unsupported)
    if len(widths) != 1:
        unsupported.append(f"trigger-field-shape:{trigger.id}")
        return False, tuple(unsupported)

    for check, field, value, search_id in zip(
        trigger.field_check_types,
        trigger.field_types,
        trigger.field_values,
        trigger.field_card_search_ids,
        strict=True,
    ):
        if (
            check == CHECK_NOT
            and field == FIELD_CARD_SEARCH_COUNT_UP
            and search_id == SEARCH_LOST
        ):
            if state.lost_card_count >= value:
                return False, tuple(unsupported)
        else:
            unsupported.append(
                f"trigger-field:{check}:{field}:{search_id}:{trigger.id}"
            )

    return (not unsupported), tuple(unsupported)


def _status_change_item_enchantment(
    enchantment: ItemEnchantment,
) -> tuple[ItemStatusChangeEnchant | None, tuple[str, ...]]:
    """Admit only the verified Dance + motivation P-item reaction shape."""

    trigger = enchantment.trigger
    expected_effects = (
        (EFFECT_GOOD_IMPRESSION_ADDITIVE, 500, 0, 0, 2),
        (EFFECT_MOTIVATION_ADDITIVE, 500, 0, 0, 2),
    )
    actual_effects = tuple(
        (
            effect.effect_type,
            effect.value1,
            effect.value2,
            effect.effect_count,
            effect.effect_turn,
        )
        for effect in enchantment.effects
    )
    supported = (
        trigger.phase_types == (PHASE_STATUS_CHANGE,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and not trigger.field_types
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == CARD_MOVE_UNKNOWN
        and trigger.effect_types == (EFFECT_MOTIVATION,)
        and trigger.lesson_type == LESSON_DANCE
        and enchantment.max_uses > 0
        and actual_effects == expected_effects
        and all(
            not effect.trigger_id
            and not effect.status_enchant_id
            and not effect.chain_effect_id
            for effect in enchantment.effects
        )
    )
    if not supported:
        return None, (f"status-change-item-shape:{enchantment.id}",)
    return ItemStatusChangeEnchant(enchantment.id, enchantment.max_uses), ()


def resolve_item_end_turn(
    item: EquippedItemRule,
    state: LogicExamState,
    card: MasterCard | None = None,
) -> ResolvedItemEffects:
    trigger_state = state
    if (
        card is not None
        and card.move_position_type == "ProduceCardMovePositionType_Lost"
    ):
        trigger_state = replace(
            state,
            lost_card_count=state.lost_card_count + 1,
        )
    effects: list[MasterCardEffect] = []
    post_card_effects: list[MasterCardEffect] = []
    end_turn_effects: list[MasterCardEffect] = []
    status_change_enchantments: list[ItemStatusChangeEnchant] = []
    fired: list[str] = []
    unsupported = list(item.unsupported_rules)
    for enchantment in item.enchantments:
        used = dict(state.item_enchantment_uses).get(enchantment.id, 0)
        if enchantment.max_uses > 0 and used >= enchantment.max_uses:
            continue
        if enchantment.trigger.phase_types == (PHASE_STATUS_CHANGE,):
            status_change, trigger_unsupported = _status_change_item_enchantment(
                enchantment
            )
            unsupported.extend(trigger_unsupported)
            if status_change is not None:
                status_change_enchantments.append(status_change)
            continue
        does_fire, trigger_unsupported = _trigger_fires(
            enchantment.trigger, trigger_state, card
        )
        unsupported.extend(trigger_unsupported)
        if does_fire:
            fired.append(enchantment.id)
            effects.extend(enchantment.effects)
            if enchantment.trigger.phase_types == (PHASE_CARD_PLAY_AFTER,):
                post_card_effects.extend(enchantment.effects)
            elif enchantment.trigger.phase_types == (PHASE_END_TURN_INTERVAL,):
                end_turn_effects.extend(enchantment.effects)
    return ResolvedItemEffects(
        effects=tuple(effects),
        fired_enchantment_ids=tuple(fired),
        unsupported_rules=tuple(unsupported),
        post_card_effects=tuple(post_card_effects),
        end_turn_effects=tuple(end_turn_effects),
        status_change_enchantments=tuple(status_change_enchantments),
    )


def _normalized_lesson_type(value: str) -> str | None:
    lowered = value.strip().lower()
    aliases = {
        "vo": "vocal",
        "vocal": "vocal",
        "da": "dance",
        "dance": "dance",
        "vi": "visual",
        "visual": "visual",
    }
    if lowered in aliases:
        return aliases[lowered]
    for token, normalized in (
        ("vocal", "vocal"),
        ("dance", "dance"),
        ("visual", "visual"),
    ):
        if token in lowered:
            return normalized
    return None


def _start_turn_trigger_fires(
    trigger: ExamTriggerRule,
    state: LogicExamState,
    lesson_type: str,
) -> tuple[bool, tuple[str, ...]]:
    """Evaluate the verified start-turn trigger shapes from local Master."""

    if trigger.phase_types != (PHASE_START_TURN,):
        return False, ()
    if trigger.phase_values or trigger.field_check_types or trigger.field_card_search_ids:
        return False, (f"start-turn-trigger-shape:{trigger.id}",)

    lesson_match = re.search(r"lesson_(vocal|dance|visual)(?:$|-)", trigger.id)
    if lesson_match is not None:
        if trigger.field_types or trigger.field_values:
            return False, (f"start-turn-trigger-field:{trigger.id}",)
        normalized = _normalized_lesson_type(lesson_type)
        if normalized is None:
            return False, (f"start-turn-lesson-type:{lesson_type}",)
        return normalized == lesson_match.group(1), ()

    if (
        trigger.field_types == (FIELD_MOTIVATION_UP,)
        and len(trigger.field_values) == 1
        and trigger.field_values[0] >= 0
        and "card_play_aggressive_up" in trigger.id
    ):
        return state.motivation >= trigger.field_values[0], ()
    return False, (f"start-turn-trigger:{trigger.id}",)


def resolve_item_turn_start(
    item: EquippedItemRule,
    state: LogicExamState,
    *,
    lesson_type: str,
) -> ItemTurnStartResolution:
    """Apply one equipped item's verified ExamStartTurn enchantments.

    This supports the local-Master shapes used by attribute-specific +play
    items and the motivation-threshold mascot.  Other phases are deliberately
    ignored here because they are resolved by ``resolve_item_end_turn``.
    """

    current = state
    effects: list[MasterCardEffect] = []
    fired: list[str] = []
    unsupported = list(item.unsupported_rules)
    uses = dict(state.item_enchantment_uses)
    for enchantment in item.enchantments:
        if enchantment.trigger.phase_types != (PHASE_START_TURN,):
            continue
        if enchantment.max_uses > 0 and uses.get(enchantment.id, 0) >= enchantment.max_uses:
            continue
        does_fire, trigger_unsupported = _start_turn_trigger_fires(
            enchantment.trigger,
            current,
            lesson_type,
        )
        unsupported.extend(trigger_unsupported)
        if not does_fire:
            continue

        plays_remaining = current.plays_remaining
        motivation = current.motivation
        effect_unsupported: list[str] = []
        for effect in enchantment.effects:
            if effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
                plays_remaining += max(1, effect.value1, effect.effect_count)
            elif effect.effect_type == EFFECT_MOTIVATION:
                motivation_gain_bonus = sum(
                    value
                    for value, _turns in current.motivation_gain_bonus_layers
                )
                motivation += scale_good_impression_gain(
                    effect.value1,
                    motivation_gain_bonus,
                )
            else:
                effect_unsupported.append(
                    f"start-turn-effect:{effect.effect_type}:{effect.id}"
                )
        unsupported.extend(effect_unsupported)
        if effect_unsupported:
            continue
        uses[enchantment.id] = uses.get(enchantment.id, 0) + 1
        current = replace(
            current,
            plays_remaining=plays_remaining,
            motivation=motivation,
            item_enchantment_uses=tuple(sorted(uses.items())),
        )
        effects.extend(enchantment.effects)
        fired.append(enchantment.id)

    return ItemTurnStartResolution(
        before=state,
        after=current,
        effects=tuple(effects),
        fired_enchantment_ids=tuple(fired),
        unsupported_rules=tuple(unsupported),
    )
