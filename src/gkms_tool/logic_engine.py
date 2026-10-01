"""Strict, data-driven one-turn rules for Plan 2 (Logic / good impression).

The master database tells us which effects a card owns and their numeric
values.  This module applies only effects whose behaviour has been verified
from the in-game tutorial and a live transition.  Unknown triggers, cost types,
and effects are returned explicitly and are never treated as no-ops.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

from .exam_status_runtime import (
    PHASE_CARD_PLAY_AFTER as STATUS_PHASE_CARD_PLAY_AFTER,
    PHASE_STATUS_CHANGE as STATUS_PHASE_STATUS_CHANGE,
    PHASE_START_TURN as STATUS_PHASE_START_TURN,
    PHASE_TURN_TIMER as STATUS_PHASE_TURN_TIMER,
    StatusRuntimeResolution,
    resolve_status_enchants,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import ceil_f32_to_i32, f32


PLAN_COMMON = "ProducePlanType_Common"
PLAN_LOGIC = "ProducePlanType_Plan2"
COST_STAMINA = "ExamCostType_Unknown"
COST_GOOD_IMPRESSION = "ExamCostType_ExamReview"
COST_MOTIVATION = "ExamCostType_ExamCardPlayAggressive"

EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
EFFECT_BLOCK_MULTIPLE_MOTIVATION = (
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive"
)
EFFECT_GOOD_IMPRESSION = "ProduceExamEffectType_ExamReview"
EFFECT_MOTIVATION = "ProduceExamEffectType_ExamCardPlayAggressive"
EFFECT_GOOD_IMPRESSION_MULTIPLE = (
    "ProduceExamEffectType_ExamReviewValueMultiple"
)
EFFECT_GOOD_IMPRESSION_BONUS = "ProduceExamEffectType_ExamReviewMultiple"
EFFECT_LESSON = "ProduceExamEffectType_ExamLesson"
EFFECT_LESSON_BY_GOOD_IMPRESSION = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
EFFECT_LESSON_BY_BLOCK = "ProduceExamEffectType_ExamLessonDependBlock"
EFFECT_LESSON_BY_MOTIVATION = (
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive"
)
EFFECT_STAMINA_REDUCE_FIX = "ProduceExamEffectType_ExamStaminaReduceFix"
EFFECT_EXTRA_TURN = "ProduceExamEffectType_ExamExtraTurn"
EFFECT_PLAYABLE_VALUE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_CARD_DRAW = "ProduceExamEffectType_ExamCardDraw"
EFFECT_CARD_CREATE_ID = "ProduceExamEffectType_ExamCardCreateId"
EFFECT_CARD_MOVE = "ProduceExamEffectType_ExamCardMove"
EFFECT_GOOD_IMPRESSION_ADDITIVE = "ProduceExamEffectType_ExamReviewAdditive"
EFFECT_MOTIVATION_ADDITIVE = "ProduceExamEffectType_ExamAggressiveAdditive"
EFFECT_STAMINA_CONSUMPTION_DOWN = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_STAMINA_CONSUMPTION_DOWN_FIXED = (
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix"
)
PHASE_END_TURN = "ProduceExamPhaseType_ExamEndTurn"
FIELD_REMAINING_TURN = "ProduceExamFieldStatusType_RemainingTurn"
CARD_MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
LESSON_SKIP_BASE_RECOVERY = 2
SUPPORTED_DIRECT_EFFECT_TYPES = frozenset(
    {
        EFFECT_BLOCK,
        EFFECT_BLOCK_MULTIPLE_MOTIVATION,
        EFFECT_GOOD_IMPRESSION,
        EFFECT_MOTIVATION,
        EFFECT_GOOD_IMPRESSION_MULTIPLE,
        EFFECT_LESSON,
        EFFECT_LESSON_BY_GOOD_IMPRESSION,
        EFFECT_LESSON_BY_BLOCK,
        EFFECT_LESSON_BY_MOTIVATION,
        EFFECT_EXTRA_TURN,
        EFFECT_PLAYABLE_VALUE_ADD,
        EFFECT_CARD_DRAW,
        EFFECT_CARD_CREATE_ID,
        EFFECT_GOOD_IMPRESSION_ADDITIVE,
        EFFECT_MOTIVATION_ADDITIVE,
        EFFECT_STAMINA_CONSUMPTION_DOWN,
        EFFECT_STAMINA_CONSUMPTION_DOWN_FIXED,
    }
)
SUPPORTED_END_TURN_EFFECT_TYPES = frozenset(
    {
        EFFECT_GOOD_IMPRESSION_BONUS,
        EFFECT_STAMINA_REDUCE_FIX,
    }
)
MOTIVATION_TRIGGER_PREFIX = "e_trigger-exam_card_play-card_play_aggressive_up-"
REVIEW_TRIGGER_PREFIX = "e_trigger-none-review_up-"
BLOCK_TRIGGER_PREFIX = "e_trigger-none-block_up-"
CARD_PLAY_REVIEW_TRIGGER_PREFIX = "e_trigger-exam_card_play-review_up-"
TROUBLE_SEARCH_TRIGGER_PREFIX = "e_trigger-none-card_search_count_up-"
TROUBLE_NOT_LOST_SEARCH_ID = "p_card_search-trouble-not_lost"
END_TURN_REVIEW_TRIGGER_PREFIX = "e_trigger-exam_end_turn-review_up-"
SLEEP_TROUBLE_CARD_ID = "p_card-00-acc-0_002"
CARD_CREATE_ID_PATTERN = re.compile(
    r"^e_effect-exam_card_create_id-(p_card-.+)-(\d+)-"
    r"(deck_first|deck_random|grave|hand)-(\d+)_(\d+)$"
)
SLEEP_CARD_MOVE_TO_LOST_PATTERN = re.compile(
    r"^e_effect-exam_card_move-p_card_search-deck_grave-"
    r"p_card-00-acc-0_002-lost-random-1_1$"
)
PLAY_COUNT_TRIGGER_PREFIX = "e_trigger-exam_play_count_interval-"
SKILL_CARD_CATEGORIES = frozenset(
    {
        "ProduceCardCategory_ActiveSkill",
        "ProduceCardCategory_MentalSkill",
    }
)
_REVIEW_LIFECYCLE_UNSET = object()


@dataclass(frozen=True, slots=True)
class MasterCardEffect:
    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    trigger_id: str = ""
    hide_icon: bool = False
    once: bool = False
    status_enchant_rule: MasterStatusEnchant | None = None


@dataclass(frozen=True, slots=True)
class MasterStatusEnchant:
    """One persistent in-exam rule resolved from Master metadata."""

    id: str
    trigger_id: str
    effects: tuple[MasterCardEffect, ...]
    # Set only when the complete Master trigger row has the exact native
    # ExamEndTurn + RemainingTurn threshold shape.  ``None`` means this
    # trigger has not been proven safe for that interpreter.
    end_turn_remaining_turn_max: int | None = None


@dataclass(frozen=True, slots=True)
class MasterCard:
    id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina_cost: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    effects: tuple[MasterCardEffect, ...]
    force_stamina_cost: int = 0
    rarity: str = ""
    evaluation: int = 0
    observed_stamina_cost: int | None = None
    observed_force_stamina_cost: int | None = None
    # Native ordered replay needs the card's end-of-turn and movement
    # metadata as well as its ordinary played-card destination.  The legacy
    # numerical engine does not consume these fields, so defaults preserve
    # existing callers while strict Master loading supplies the real values.
    is_end_turn_lost: bool = False
    move_effect_trigger_type: str = ""
    move_effect_ids: tuple[str, ...] = ()
    move_trigger_ids: tuple[str, ...] = ()
    produce_card_status_enchant_id: str = ""


@dataclass(frozen=True, slots=True)
class ActiveRuntimeStatusEnchant:
    """Installed Master status rule with source-specific use tracking.

    ``instance_id`` deliberately differs from ``enchant_id``: two selected
    memories or support cards can install the same Master enchant, and those
    copies must retain independent activation limits.
    """

    instance_id: str
    enchant_id: str
    max_uses: int = 0
    uses: int = 0


@dataclass(frozen=True, slots=True)
class ItemStatusChangeEnchant:
    """An equipped item's verified reactive status source.

    Unlike an installed runtime status this is supplied for the current card
    transition only.  Its use count remains in ``item_enchantment_uses`` so a
    P item cannot be replayed as an extra card or survive after unequipping.
    """

    id: str
    max_uses: int


@dataclass(frozen=True, slots=True)
class LogicExamState:
    """Observed state at the moment a card can be selected.

    Review is an object-backed scalar in the native client. Its integer value
    therefore cannot describe its lifetime on its own: an active zero-valued
    status differs from no status, and a fresh status differs from one that has
    already passed a TurnStart. The two optional fields at the end of this
    dataclass store those three states explicitly.

    Older checkpoints omit both fields. For compatibility, a positive legacy
    value is treated as an ordinary Main-phase ``present + passing`` status;
    zero is treated as absent. Explicit ``None`` is rejected as unknown rather
    than silently granted the no-decay benefit of ``fresh``.
    """

    turns_remaining: int
    stamina: int
    score: int = 0
    block: int = 0
    good_impression: int = 0
    motivation: int = 0
    round_number: int = 1
    good_impression_bonus_permille: int = 0
    good_impression_bonus_turns: int = 0
    lost_card_count: int = 0
    good_impression_bonus_layers: tuple[tuple[int, int], ...] = ()
    last_resolved_turn_start_round: int = 0
    score_multiplier_permille: int = 1000
    max_stamina: int = 0
    turn_end_stamina_recovery: int = 0
    plays_remaining: int = 1
    good_impression_gain_bonus_permille: int = 0
    good_impression_gain_bonus_turns: int = 0
    good_impression_gain_bonus_layers: tuple[tuple[int, int], ...] = ()
    good_impression_gain_bonus_fresh_count: int = 0
    motivation_gain_bonus_permille: int = 0
    motivation_gain_bonus_turns: int = 0
    motivation_gain_bonus_layers: tuple[tuple[int, int], ...] = ()
    motivation_gain_bonus_fresh_count: int = 0
    trouble_card_count: int = 0
    pending_extra_turns: int = 0
    stamina_consumption_down_turns: int = 0
    stamina_consumption_down_fresh: bool = False
    stamina_consumption_down_fixed: int = 0
    active_status_enchants: tuple[MasterStatusEnchant, ...] = ()
    active_status_enchant_progress: tuple[tuple[str, int], ...] = ()
    runtime_status_enchants: tuple[ActiveRuntimeStatusEnchant, ...] = ()
    last_resolved_runtime_status_round: int = 0
    created_cards: tuple[tuple[str, int, str, int], ...] = ()
    item_enchantment_uses: tuple[tuple[str, int], ...] = ()
    good_impression_exists: bool | None = (  # type: ignore[assignment]
        _REVIEW_LIFECYCLE_UNSET
    )
    good_impression_passing_turn_start: bool | None = (  # type: ignore[assignment]
        _REVIEW_LIFECYCLE_UNSET
    )

    def __post_init__(self) -> None:
        review_exists = self.good_impression_exists
        review_passing = self.good_impression_passing_turn_start
        exists_unset = review_exists is _REVIEW_LIFECYCLE_UNSET
        passing_unset = review_passing is _REVIEW_LIFECYCLE_UNSET
        if exists_unset and passing_unset:
            # Legacy/Main checkpoint compatibility. Unknown positive Review
            # must not silently become fresh, because that would skip a native
            # TurnStart decrement and make the state optimistically safe.
            review_exists = self.good_impression > 0
            review_passing = review_exists
        elif exists_unset or passing_unset:
            raise ValueError(
                "Review lifecycle must provide both exists and passing flags"
            )
        elif review_exists is None or review_passing is None:
            raise ValueError(
                "unknown Review lifecycle cannot be simulated safely"
            )
        object.__setattr__(self, "good_impression_exists", review_exists)
        object.__setattr__(
            self,
            "good_impression_passing_turn_start",
            review_passing,
        )

        enchants: list[MasterStatusEnchant] = []
        for raw_enchant in self.active_status_enchants:
            if isinstance(raw_enchant, MasterStatusEnchant):
                enchants.append(raw_enchant)
                continue
            if not isinstance(raw_enchant, dict):
                raise TypeError("active_status_enchants entries must be mappings")
            raw_effects = raw_enchant.get("effects", ())
            if not isinstance(raw_effects, (list, tuple)):
                raise TypeError("status enchant effects must be a sequence")
            effects = tuple(
                effect
                if isinstance(effect, MasterCardEffect)
                else MasterCardEffect(**effect)
                for effect in raw_effects
            )
            enchants.append(
                MasterStatusEnchant(
                    id=str(raw_enchant.get("id", "")),
                    trigger_id=str(raw_enchant.get("trigger_id", "")),
                    effects=effects,
                    end_turn_remaining_turn_max=(
                        None
                        if raw_enchant.get("end_turn_remaining_turn_max") is None
                        else int(raw_enchant["end_turn_remaining_turn_max"])
                    ),
                )
            )
        object.__setattr__(self, "active_status_enchants", tuple(enchants))
        runtime_enchants: list[ActiveRuntimeStatusEnchant] = []
        for index, raw_enchant in enumerate(self.runtime_status_enchants):
            if isinstance(raw_enchant, ActiveRuntimeStatusEnchant):
                runtime_enchants.append(raw_enchant)
            elif isinstance(raw_enchant, str):
                runtime_enchants.append(
                    ActiveRuntimeStatusEnchant(
                        instance_id=f"legacy:{index}:{raw_enchant}",
                        enchant_id=raw_enchant,
                    )
                )
            elif isinstance(raw_enchant, dict):
                runtime_enchants.append(
                    ActiveRuntimeStatusEnchant(**raw_enchant)
                )
            else:
                raise TypeError(
                    "runtime_status_enchants entries must be strings, mappings, "
                    "or ActiveRuntimeStatusEnchant values"
                )
        object.__setattr__(
            self,
            "runtime_status_enchants",
            tuple(runtime_enchants),
        )
        object.__setattr__(
            self,
            "active_status_enchant_progress",
            tuple(
                (str(enchantment_id), int(progress))
                for enchantment_id, progress in self.active_status_enchant_progress
            ),
        )
        object.__setattr__(
            self,
            "created_cards",
            tuple(
                (
                    str(card_id),
                    int(upgrade),
                    str(position),
                    int(count),
                )
                for card_id, upgrade, position, count in self.created_cards
            ),
        )
        object.__setattr__(
            self,
            "item_enchantment_uses",
            tuple(
                (str(enchantment_id), int(count))
                for enchantment_id, count in self.item_enchantment_uses
            ),
        )
        layers = tuple(
            (int(layer[0]), int(layer[1]))
            for layer in self.good_impression_bonus_layers
        )
        if not layers and self.good_impression_bonus_permille > 0:
            layers = ((
                int(self.good_impression_bonus_permille),
                int(self.good_impression_bonus_turns),
            ),)
        object.__setattr__(self, "good_impression_bonus_layers", layers)
        if layers:
            object.__setattr__(
                self,
                "good_impression_bonus_permille",
                sum(value for value, _turns in layers),
            )
            object.__setattr__(
                self,
                "good_impression_bonus_turns",
                min(turns for _value, turns in layers),
            )
        gain_layers = tuple(
            (int(layer[0]), int(layer[1]))
            for layer in self.good_impression_gain_bonus_layers
        )
        if not gain_layers and self.good_impression_gain_bonus_permille > 0:
            gain_layers = ((
                int(self.good_impression_gain_bonus_permille),
                int(self.good_impression_gain_bonus_turns),
            ),)
        object.__setattr__(
            self, "good_impression_gain_bonus_layers", gain_layers
        )
        if gain_layers:
            object.__setattr__(
                self,
                "good_impression_gain_bonus_permille",
                sum(value for value, _turns in gain_layers),
            )
            object.__setattr__(
                self,
                "good_impression_gain_bonus_turns",
                min(turns for _value, turns in gain_layers),
            )
        motivation_gain_layers = tuple(
            (int(layer[0]), int(layer[1]))
            for layer in self.motivation_gain_bonus_layers
        )
        if (
            not motivation_gain_layers
            and self.motivation_gain_bonus_permille > 0
        ):
            motivation_gain_layers = ((
                int(self.motivation_gain_bonus_permille),
                int(self.motivation_gain_bonus_turns),
            ),)
        object.__setattr__(
            self, "motivation_gain_bonus_layers", motivation_gain_layers
        )
        if motivation_gain_layers:
            object.__setattr__(
                self,
                "motivation_gain_bonus_permille",
                sum(value for value, _turns in motivation_gain_layers),
            )
            object.__setattr__(
                self,
                "motivation_gain_bonus_turns",
                min(turns for _value, turns in motivation_gain_layers),
            )

    def validate(self, *, allow_completed: bool = False) -> None:
        if self.turns_remaining < 0 or (
            self.turns_remaining == 0 and not allow_completed
        ):
            raise ValueError("剩餘回合必須至少為 1。")
        if self.round_number < 1:
            raise ValueError("目前回合序號必須至少為 1。")
        if self.plays_remaining < 1:
            raise ValueError("可出牌次數必須至少為 1。")
        if min(
            self.stamina,
            self.score,
            self.block,
            self.good_impression,
            self.motivation,
            self.good_impression_bonus_permille,
            self.good_impression_bonus_turns,
            self.lost_card_count,
            self.last_resolved_turn_start_round,
            self.max_stamina,
            self.turn_end_stamina_recovery,
            self.good_impression_gain_bonus_permille,
            self.good_impression_gain_bonus_turns,
            self.good_impression_gain_bonus_fresh_count,
            self.motivation_gain_bonus_permille,
            self.motivation_gain_bonus_turns,
            self.motivation_gain_bonus_fresh_count,
            self.trouble_card_count,
            self.pending_extra_turns,
            self.stamina_consumption_down_turns,
            self.stamina_consumption_down_fixed,
            self.last_resolved_runtime_status_round,
        ) < 0:
            raise ValueError("體力、分數、元氣與好印象不可為負數。")
        if self.score_multiplier_permille <= 0:
            raise ValueError("分數倍率必須大於零。")
        if type(self.good_impression_exists) is not bool or type(
            self.good_impression_passing_turn_start
        ) is not bool:
            raise ValueError("Review lifecycle flags must be booleans")
        if (
            self.good_impression_passing_turn_start
            and not self.good_impression_exists
        ):
            raise ValueError("absent Review cannot be passing TurnStart")
        if not self.good_impression_exists and self.good_impression != 0:
            raise ValueError("nonzero Review requires an active status")
        if self.max_stamina and self.stamina > self.max_stamina:
            raise ValueError("目前體力不可高於體力上限。")
        if any(value <= 0 or turns <= 0 for value, turns in self.good_impression_bonus_layers):
            raise ValueError("好印象加成分層的數值與回合必須為正數。")
        if any(
            value <= 0 or turns <= 0
            for value, turns in self.good_impression_gain_bonus_layers
        ):
            raise ValueError("好印象增加量加成的數值與回合必須為正數。")
        if any(
            value <= 0 or turns <= 0
            for value, turns in self.motivation_gain_bonus_layers
        ):
            raise ValueError(
                "motivation gain-bonus values and turns must be positive"
            )
        if any(
            not card_id or upgrade < 0 or not position or count <= 0
            for card_id, upgrade, position, count in self.created_cards
        ):
            raise ValueError("created card entries must be positive and complete")
        if any(
            not enchantment_id or count < 0
            for enchantment_id, count in self.item_enchantment_uses
        ):
            raise ValueError("item enchantment use entries must be non-negative")
        if any(
            not enchantment_id or progress < 0
            for enchantment_id, progress in self.active_status_enchant_progress
        ):
            raise ValueError("status enchantment progress entries must be non-negative")
        runtime_instance_ids = [
            enchantment.instance_id
            for enchantment in self.runtime_status_enchants
        ]
        if len(runtime_instance_ids) != len(set(runtime_instance_ids)):
            raise ValueError("runtime status enchant instance ids must be unique")
        if any(
            not enchantment.instance_id
            or not enchantment.enchant_id
            or enchantment.max_uses < 0
            or enchantment.uses < 0
            or (
                enchantment.max_uses > 0
                and enchantment.uses > enchantment.max_uses
            )
            for enchantment in self.runtime_status_enchants
        ):
            raise ValueError("runtime status enchant entries are invalid")
        if self.good_impression_gain_bonus_fresh_count > len(
            self.good_impression_gain_bonus_layers
        ):
            raise ValueError("fresh gain-bonus layer count exceeds active layers")
        if self.motivation_gain_bonus_fresh_count > len(
            self.motivation_gain_bonus_layers
        ):
            raise ValueError(
                "fresh motivation-gain layer count exceeds active layers"
            )
        if self.last_resolved_runtime_status_round > self.round_number:
            raise ValueError(
                "last resolved runtime-status round cannot exceed current round"
            )
        if self.last_resolved_turn_start_round > self.round_number:
            raise ValueError("已處理的回合開始序號不可超過目前回合。")
    @property
    def good_impression_lifecycle(self) -> str:
        """Return the explicit three-state Review lifetime."""

        if not self.good_impression_exists:
            return "ABSENT"
        if self.good_impression_passing_turn_start:
            return "PRESENT_PASSING"
        return "PRESENT_FRESH"


@dataclass(frozen=True, slots=True)
class LogicTransition:
    before: LogicExamState
    after: LogicExamState
    card: MasterCard
    legal: bool
    stamina_paid: int
    force_stamina_paid: int
    block_paid: int
    end_turn_stamina_paid: int
    immediate_score_gain: int
    end_turn_score_gain: int
    total_score_gain: int
    unsupported_rules: tuple[str, ...] = ()
    immediate_raw_score_gain: int = 0
    end_turn_raw_score_gain: int = 0
    end_turn_stamina_recovered: int = 0
    good_impression_paid: int = 0
    motivation_paid: int = 0
    turn_ended: bool = True
    runtime_status_enchant_ids: tuple[str, ...] = ()
    runtime_status_effect_ids: tuple[str, ...] = ()
    runtime_status_added_ids: tuple[str, ...] = ()
    runtime_status_draw_count: int = 0

    @property
    def fully_supported(self) -> bool:
        return self.legal and not self.unsupported_rules


def _effect_from_row(
    row: sqlite3.Row,
    *,
    trigger_id: str,
    hide_icon: bool,
    once: bool,
) -> MasterCardEffect:
    return MasterCardEffect(
        id=row["id"],
        effect_type=row["effect_type"],
        value1=row["value1"],
        value2=row["value2"],
        effect_count=row["effect_count"],
        effect_turn=row["effect_turn"],
        status_enchant_id=row["status_enchant_id"],
        chain_effect_id=row["chain_effect_id"],
        trigger_id=trigger_id,
        hide_icon=hide_icon,
        once=once,
    )


def _exact_end_turn_remaining_turn_max(
    connection: sqlite3.Connection,
    trigger_id: str,
) -> int | None:
    """Return the native ``RemainingTurn <= value`` gate for one exact shape.

    The field predicate is deliberately derived from every relevant Master
    column instead of from the human-readable trigger ID.  A row with any
    additional condition stays unsupported and cannot silently install an
    optimistic status rule.
    """

    row = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?",
        (trigger_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Master exam trigger not found: {trigger_id}")
    required = {
        "phase_types_json",
        "phase_values_json",
        "field_status_check_types_json",
        "field_status_types_json",
        "field_status_values_json",
        "field_status_produce_card_search_ids_json",
        "produce_card_search_id",
        "upper_search_count",
        "lower_search_count",
        "card_move_position_type",
        "effect_types_json",
        "lesson_type",
    }
    if not required.issubset(row.keys()):
        return None

    def array(name: str) -> list[object]:
        value = json.loads(row[name])
        if not isinstance(value, list):
            raise ValueError(f"Master trigger field is not an array: {trigger_id}:{name}")
        return value

    phase_types = array("phase_types_json")
    phase_values = array("phase_values_json")
    field_checks = array("field_status_check_types_json")
    field_types = array("field_status_types_json")
    field_values = array("field_status_values_json")
    field_searches = array("field_status_produce_card_search_ids_json")
    effect_types = array("effect_types_json")
    if not (
        phase_types == [PHASE_END_TURN]
        and phase_values == []
        and field_checks == []
        and field_types == [FIELD_REMAINING_TURN]
        and len(field_values) == 1
        and isinstance(field_values[0], int)
        and not isinstance(field_values[0], bool)
        and field_values[0] >= 0
        and field_searches == []
        and row["produce_card_search_id"] == ""
        and int(row["upper_search_count"]) == 0
        and int(row["lower_search_count"]) == 0
        and row["card_move_position_type"] == CARD_MOVE_UNKNOWN
        and effect_types == []
        and row["lesson_type"] == LESSON_UNKNOWN
    ):
        return None
    return int(field_values[0])


def _load_status_enchant(
    connection: sqlite3.Connection,
    enchant_id: str,
) -> MasterStatusEnchant:
    row = connection.execute(
        """
        SELECT produce_exam_trigger_id, produce_exam_effect_ids_json
          FROM produce_exam_status_enchant
         WHERE id = ?
        """,
        (enchant_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Master status enchant not found: {enchant_id}")
    effect_ids = json.loads(row["produce_exam_effect_ids_json"])
    if not isinstance(effect_ids, list) or not all(
        isinstance(effect_id, str) and effect_id for effect_id in effect_ids
    ):
        raise ValueError(f"Invalid status enchant effects: {enchant_id}")
    effects: list[MasterCardEffect] = []
    for effect_id in effect_ids:
        effect_row = connection.execute(
            """
            SELECT id, effect_type, value1, value2, effect_count,
                   effect_turn, status_enchant_id, chain_effect_id
              FROM effect
             WHERE id = ?
            """,
            (effect_id,),
        ).fetchone()
        if effect_row is None:
            raise KeyError(f"Master enchant effect not found: {effect_id}")
        effects.append(
            _effect_from_row(
                effect_row,
                trigger_id="",
                hide_icon=False,
                once=False,
            )
        )
    return MasterStatusEnchant(
        id=enchant_id,
        trigger_id=str(row["produce_exam_trigger_id"]),
        effects=tuple(effects),
        end_turn_remaining_turn_max=_exact_end_turn_remaining_turn_max(
            connection,
            str(row["produce_exam_trigger_id"]),
        ),
    )


def load_master_card(
    card_id: str,
    upgrade: int = 0,
    database: Path = DEFAULT_DATABASE,
) -> MasterCard:
    """Load one card and its ordered effects from the imported master DB."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        card_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(card)")
        }
        raw_column = ", raw_json" if "raw_json" in card_columns else ""
        card = connection.execute(
            f"""
            SELECT id, upgrade_count, name, plan_type, category, stamina,
                   cost_type, cost_value, play_trigger_id, move_position_type,
                   play_effects_json{raw_column}
              FROM card
             WHERE id = ? AND upgrade_count = ?
            """,
            (card_id, upgrade),
        ).fetchone()
        if card is None:
            raise KeyError(f"Master 資料庫找不到卡牌：{card_id} +{upgrade}")

        payload = json.loads(card["play_effects_json"])
        if not isinstance(payload, list):
            raise ValueError(f"卡牌效果不是陣列：{card_id} +{upgrade}")

        effects: list[MasterCardEffect] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                raise ValueError(f"卡牌效果 #{index} 不是物件：{card_id} +{upgrade}")
            effect_id = item.get("produceExamEffectId")
            if not isinstance(effect_id, str) or not effect_id:
                raise ValueError(f"卡牌效果 #{index} 缺少 effect id：{card_id} +{upgrade}")
            effect = connection.execute(
                """
                SELECT id, effect_type, value1, value2, effect_count,
                       effect_turn, status_enchant_id, chain_effect_id
                  FROM effect
                 WHERE id = ?
                """,
                (effect_id,),
            ).fetchone()
            if effect is None:
                raise KeyError(f"Master 資料庫找不到效果：{effect_id}")
            resolved = _effect_from_row(
                effect,
                trigger_id=str(item.get("produceExamTriggerId", "")),
                hide_icon=bool(item.get("hideIcon", False)),
                once=bool(item.get("isOncePlayEffect", False)),
            )
            if resolved.status_enchant_id:
                resolved = replace(
                    resolved,
                    status_enchant_rule=_load_status_enchant(
                        connection, resolved.status_enchant_id
                    ),
                )
            effects.append(resolved)

        raw = (
            json.loads(card["raw_json"])
            if "raw_json" in card.keys()
            else {}
        )
        if not isinstance(raw, dict):
            raise ValueError(f"卡牌原始資料不是物件：{card_id} +{upgrade}")

        def raw_string_tuple(field: str) -> tuple[str, ...]:
            value = raw.get(field, [])
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item for item in value
            ):
                raise ValueError(
                    f"Invalid Master card {field}: {card_id} +{upgrade}"
                )
            return tuple(value)

        raw_end_turn_lost = raw.get("isEndTurnLost", False)
        if not isinstance(raw_end_turn_lost, bool):
            raise ValueError(
                f"Invalid Master card isEndTurnLost: {card_id} +{upgrade}"
            )
        raw_move_effect_trigger_type = raw.get("moveEffectTriggerType", "")
        raw_status_enchant_id = raw.get("produceCardStatusEnchantId", "")
        if not isinstance(raw_move_effect_trigger_type, str) or not isinstance(
            raw_status_enchant_id, str
        ):
            raise ValueError(
                f"Invalid Master card movement/status metadata: {card_id} +{upgrade}"
            )
        raw_move_effect_ids = raw_string_tuple("moveProduceExamEffectIds")
        raw_move_trigger_ids = raw_string_tuple("moveProduceExamTriggerIds")

    return MasterCard(
        id=card["id"],
        upgrade=card["upgrade_count"],
        name=card["name"],
        plan_type=card["plan_type"],
        category=card["category"],
        stamina_cost=card["stamina"],
        cost_type=card["cost_type"],
        cost_value=card["cost_value"],
        play_trigger_id=card["play_trigger_id"],
        move_position_type=card["move_position_type"],
        effects=tuple(effects),
        force_stamina_cost=int(raw.get("forceStamina", 0)),
        rarity=str(raw.get("rarity", "")),
        evaluation=int(raw.get("evaluation", 0)),
        is_end_turn_lost=raw_end_turn_lost,
        move_effect_trigger_type=raw_move_effect_trigger_type,
        move_effect_ids=raw_move_effect_ids,
        move_trigger_ids=raw_move_trigger_ids,
        produce_card_status_enchant_id=raw_status_enchant_id,
    )


def load_master_effect(
    effect_id: str, database: Path = DEFAULT_DATABASE
) -> MasterCardEffect:
    """Load one standalone exam effect, for example an item end-turn effect."""

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, effect_type, value1, value2, effect_count,
                   effect_turn, status_enchant_id, chain_effect_id
              FROM effect
             WHERE id = ?
            """,
            (effect_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Master 資料庫找不到效果：{effect_id}")
        return _effect_from_row(
            row, trigger_id="", hide_icon=False, once=False
        )


def _round_up_ratio(value: int, numerator: int, denominator: int = 1000) -> int:
    if value <= 0 or numerator <= 0:
        return 0
    # Native converts numerator and denominator independently to float32,
    # divides in float32, multiplies in float32, then applies FRINTP/FCVTPS.
    multiplier = f32(f32(numerator) / f32(denominator))
    product = f32(f32(value) * multiplier)
    return ceil_f32_to_i32(product)


def scale_good_impression_gain(value: int, bonus_permille: int) -> int:
    """Apply the active 好印象 increase bonus using the game's ceiling rule."""

    return _round_up_ratio(value, 1000 + bonus_permille)


def _gain_review_scalar(
    value: int,
    exists: bool,
    passing_turn_start: bool,
    gain: int,
) -> tuple[int, bool, bool]:
    """Apply native TryAddReviewStatus identity/freshness semantics."""

    if exists:
        return value + gain, True, passing_turn_start
    # TryAddReviewStatus allocates even when the resulting gain is zero. The
    # new managed instance starts with IsPassingTurnStart == false.
    return value + gain, True, False


def _consume_review_scalar(
    value: int,
    exists: bool,
    passing_turn_start: bool,
    paid: int,
) -> tuple[int, bool, bool]:
    """Apply ConsumeReview, including synchronous removal at exactly zero."""

    if not exists or paid <= 0:
        return value, exists, passing_turn_start
    remaining = max(0, value - paid)
    if remaining == 0:
        return 0, False, False
    return remaining, True, passing_turn_start


def _review_multiple_from_layers(
    layers: tuple[tuple[int, int], ...] | list[tuple[int, int]],
) -> float:
    """Native ordered Review multiplier accumulation in binary32."""

    multiple = f32(1.0)
    for value, _turns in layers:
        term = f32(f32(value) / f32(1000.0))
        multiple = f32(multiple + term)
    return multiple


def _scale_review_with_layers(
    value: int,
    layers: tuple[tuple[int, int], ...] | list[tuple[int, int]],
) -> int:
    if value <= 0:
        return 0
    return ceil_f32_to_i32(f32(f32(value) * _review_multiple_from_layers(layers)))


def _merge_review_multiple_layer(
    active: list[tuple[int, int]],
    fresh: list[tuple[int, int]],
    *,
    value: int,
    turns: int,
) -> None:
    """Merge equal-Turn ReviewMultiple while retaining existing freshness."""

    for target in (active, fresh):
        for index, (current, current_turns) in enumerate(target):
            if current_turns == turns:
                target[index] = (max(0, current + value), current_turns)
                return
    fresh.append((value, turns))


def _end_turn_review_enchant_threshold(
    enchant: MasterStatusEnchant,
) -> int | None:
    """Return the verified review threshold, or None for an unknown rule."""

    if not enchant.trigger_id.startswith(END_TURN_REVIEW_TRIGGER_PREFIX):
        return None
    try:
        threshold = int(
            enchant.trigger_id.removeprefix(END_TURN_REVIEW_TRIGGER_PREFIX)
        )
    except ValueError:
        return None
    if threshold < 0 or not enchant.effects:
        return None
    if any(
        effect.effect_type != EFFECT_GOOD_IMPRESSION
        or effect.value1 <= 0
        or effect.trigger_id
        or effect.status_enchant_id
        or effect.chain_effect_id
        for effect in enchant.effects
    ):
        return None
    return threshold


def _end_turn_remaining_review_score_limit(
    enchant: MasterStatusEnchant,
) -> int | None:
    """Validate a persistent EndTurn ``RemainingTurn <= N`` score rule."""

    limit = enchant.end_turn_remaining_turn_max
    if limit is None or limit < 0 or not enchant.effects:
        return None
    if any(
        effect.effect_type != EFFECT_LESSON_BY_GOOD_IMPRESSION
        or effect.value1 <= 0
        or effect.value2 != 0
        or effect.effect_count <= 0
        or effect.effect_turn != 0
        or effect.trigger_id
        or effect.status_enchant_id
        or effect.chain_effect_id
        or effect.hide_icon
        or effect.once
        for effect in enchant.effects
    ):
        return None
    return limit


def _play_count_enchant_interval(
    enchant: MasterStatusEnchant,
) -> int | None:
    if not enchant.trigger_id.startswith(PLAY_COUNT_TRIGGER_PREFIX):
        return None
    try:
        interval = int(
            enchant.trigger_id.removeprefix(PLAY_COUNT_TRIGGER_PREFIX)
        )
    except ValueError:
        return None
    if interval < 1 or not enchant.effects:
        return None
    if any(
        effect.effect_type != EFFECT_LESSON
        or effect.value1 <= 0
        or effect.trigger_id
        or effect.status_enchant_id
        or effect.chain_effect_id
        for effect in enchant.effects
    ):
        return None
    return interval


@dataclass(frozen=True, slots=True)
class LogicStatusTurnStartResolution:
    """A source-preserving runtime-status update before card selection."""

    before: LogicExamState
    after: LogicExamState
    fired_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()
    added_enchant_ids: tuple[str, ...] = ()
    draw_count: int = 0
    unsupported_rules: tuple[str, ...] = ()

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported_rules


@dataclass(frozen=True, slots=True)
class _InstalledStatusPhase:
    enchantments: tuple[ActiveRuntimeStatusEnchant, ...]
    resolution: StatusRuntimeResolution
    added_enchant_ids: tuple[str, ...] = ()


def _resolve_installed_runtime_statuses(
    enchantments: tuple[ActiveRuntimeStatusEnchant, ...],
    *,
    phase: str,
    round_number: int,
    played_card_id: str | None = None,
    played_card_upgrade: int | None = None,
    changed_effect_type: str | None = None,
    lesson_type: str = LESSON_UNKNOWN,
) -> _InstalledStatusPhase:
    """Resolve duplicate Master enchant IDs as independent source instances."""

    updated: list[ActiveRuntimeStatusEnchant] = []
    added: list[ActiveRuntimeStatusEnchant] = []
    existing_instance_ids = {entry.instance_id for entry in enchantments}
    fired_enchants: list[str] = []
    fired_effects: list[str] = []
    added_enchant_ids: list[str] = []
    unsupported: list[str] = []
    playable_add = draw_count = motivation_add = good_impression_add = 0
    block_base_add = block_effect_count = stamina_down = trouble_moved = 0
    good_gain: list[tuple[int, int]] = []
    motivation_gain: list[tuple[int, int]] = []

    for entry in enchantments:
        try:
            result = resolve_status_enchants(
                (entry.enchant_id,),
                phase=phase,
                round_number=round_number,
                played_card_id=played_card_id,
                played_card_upgrade=played_card_upgrade,
                changed_effect_type=changed_effect_type,
                lesson_type=lesson_type,
                limits={entry.enchant_id: entry.max_uses},
                uses={entry.enchant_id: entry.uses},
            )
        except (KeyError, ValueError, sqlite3.Error) as error:
            updated.append(entry)
            unsupported.append(
                f"runtime-status-load:{entry.instance_id}:{entry.enchant_id}:"
                f"{type(error).__name__}"
            )
            continue

        fired_enchants.extend(result.fired_enchant_ids)
        fired_effects.extend(result.fired_effect_ids)
        if result.unsupported_rules:
            updated.append(entry)
            unsupported.extend(
                f"runtime-status:{entry.instance_id}:{rule}"
                for rule in result.unsupported_rules
            )
            continue

        new_uses = dict(result.use_counts).get(entry.enchant_id, entry.uses)
        updated_entry = replace(entry, uses=new_uses)
        updated.append(updated_entry)
        playable_add += result.playable_add
        draw_count += result.draw_count
        motivation_add += result.motivation_add
        good_impression_add += result.good_impression_add
        block_base_add += result.block_base_add
        block_effect_count += result.block_effect_count
        stamina_down += result.stamina_consumption_down_turns
        trouble_moved += result.trouble_cards_moved_to_lost
        good_gain.extend(result.good_impression_gain_layers)
        motivation_gain.extend(result.motivation_gain_layers)

        for child_index, (enchant_id, max_uses) in enumerate(
            result.added_enchants
        ):
            base_instance_id = (
                f"{entry.instance_id}>{enchant_id}@{new_uses}:{child_index}"
            )
            instance_id = base_instance_id
            suffix = 1
            while instance_id in existing_instance_ids:
                suffix += 1
                instance_id = f"{base_instance_id}:{suffix}"
            existing_instance_ids.add(instance_id)
            added.append(
                ActiveRuntimeStatusEnchant(
                    instance_id=instance_id,
                    enchant_id=enchant_id,
                    max_uses=max_uses,
                )
            )
            added_enchant_ids.append(enchant_id)

    aggregate = StatusRuntimeResolution(
        fired_enchant_ids=tuple(fired_enchants),
        fired_effect_ids=tuple(fired_effects),
        added_enchants=tuple(
            (entry.enchant_id, entry.max_uses) for entry in added
        ),
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
    return _InstalledStatusPhase(
        enchantments=tuple((*updated, *added)),
        resolution=aggregate,
        added_enchant_ids=tuple(added_enchant_ids),
    )


def _apply_supported_status_phase(
    state: LogicExamState,
    phase: _InstalledStatusPhase,
) -> LogicExamState:
    """Apply a fully supported aggregate without guessing hidden outcomes."""

    result = phase.resolution
    if result.unsupported_rules:
        return state
    good_gain_layers = tuple(
        (*state.good_impression_gain_bonus_layers,
         *result.good_impression_gain_layers)
    )
    motivation_gain_layers = tuple(
        (*state.motivation_gain_bonus_layers,
         *result.motivation_gain_layers)
    )
    good_gain_bonus = sum(value for value, _turns in good_gain_layers)
    motivation_gain_bonus = sum(
        value for value, _turns in motivation_gain_layers
    )
    motivation = state.motivation + scale_good_impression_gain(
        result.motivation_add,
        motivation_gain_bonus,
    )
    good_impression = state.good_impression
    good_impression_exists = state.good_impression_exists
    good_impression_passing = state.good_impression_passing_turn_start
    if result.good_impression_add != 0:
        (
            good_impression,
            good_impression_exists,
            good_impression_passing,
        ) = _gain_review_scalar(
            good_impression,
            good_impression_exists,
            good_impression_passing,
            scale_good_impression_gain(
                result.good_impression_add,
                good_gain_bonus,
            ),
        )
    return replace(
        state,
        plays_remaining=state.plays_remaining + result.playable_add,
        motivation=motivation,
        good_impression=good_impression,
        good_impression_exists=good_impression_exists,
        good_impression_passing_turn_start=good_impression_passing,
        block=(
            state.block
            + result.block_base_add
            + result.block_effect_count * motivation
        ),
        good_impression_gain_bonus_layers=good_gain_layers,
        motivation_gain_bonus_layers=motivation_gain_layers,
        stamina_consumption_down_turns=(
            state.stamina_consumption_down_turns
            + result.stamina_consumption_down_turns
        ),
        lost_card_count=(
            state.lost_card_count + result.trouble_cards_moved_to_lost
        ),
        trouble_card_count=max(
            0,
            state.trouble_card_count
            - result.trouble_cards_moved_to_lost,
        ),
        runtime_status_enchants=phase.enchantments,
    )


def resolve_logic_status_turn_start(
    state: LogicExamState,
) -> LogicStatusTurnStartResolution:
    """Resolve Timer and StartTurn passives exactly once for this round."""

    state.validate()
    if (
        not state.runtime_status_enchants
        or state.last_resolved_runtime_status_round == state.round_number
    ):
        return LogicStatusTurnStartResolution(before=state, after=state)

    timer = _resolve_installed_runtime_statuses(
        state.runtime_status_enchants,
        phase=STATUS_PHASE_TURN_TIMER,
        round_number=state.round_number,
    )
    after_timer = _apply_supported_status_phase(state, timer)
    start = _resolve_installed_runtime_statuses(
        after_timer.runtime_status_enchants,
        phase=STATUS_PHASE_START_TURN,
        round_number=state.round_number,
    )
    unsupported = tuple(
        dict.fromkeys(
            (*timer.resolution.unsupported_rules,
             *start.resolution.unsupported_rules)
        )
    )
    if unsupported:
        return LogicStatusTurnStartResolution(
            before=state,
            after=state,
            fired_enchant_ids=tuple(
                (*timer.resolution.fired_enchant_ids,
                 *start.resolution.fired_enchant_ids)
            ),
            fired_effect_ids=tuple(
                (*timer.resolution.fired_effect_ids,
                 *start.resolution.fired_effect_ids)
            ),
            added_enchant_ids=tuple(
                (*timer.added_enchant_ids, *start.added_enchant_ids)
            ),
            draw_count=timer.resolution.draw_count + start.resolution.draw_count,
            unsupported_rules=unsupported,
        )
    after_start = _apply_supported_status_phase(after_timer, start)
    after = replace(
        after_start,
        last_resolved_runtime_status_round=state.round_number,
        # Native phase 5 enumerates every surviving active status and calls
        # SetPassingTurnStart immediately before entering Main. A Review
        # created by TurnTimer/StartTurn therefore skipped this turn's spend,
        # but is already passing by the time the solver can select a card.
        good_impression_passing_turn_start=(
            after_start.good_impression_exists
        ),
    )
    return LogicStatusTurnStartResolution(
        before=state,
        after=after,
        fired_enchant_ids=tuple(
            (*timer.resolution.fired_enchant_ids,
             *start.resolution.fired_enchant_ids)
        ),
        fired_effect_ids=tuple(
            (*timer.resolution.fired_effect_ids,
             *start.resolution.fired_effect_ids)
        ),
        added_enchant_ids=tuple(
            (*timer.added_enchant_ids, *start.added_enchant_ids)
        ),
        draw_count=timer.resolution.draw_count + start.resolution.draw_count,
    )


def apply_logic_card(
    state: LogicExamState,
    card: MasterCard,
    *,
    post_card_effects: tuple[MasterCardEffect, ...] = (),
    end_turn_effects: tuple[MasterCardEffect, ...] = (),
    status_change_item_enchantments: tuple[ItemStatusChangeEnchant, ...] = (),
    fired_item_enchantment_ids: tuple[str, ...] = (),
    lesson_type: str = LESSON_UNKNOWN,
) -> LogicTransition:
    """Apply one card, including status costs and same-turn extra plays."""

    state.validate()
    turn_start_status = resolve_logic_status_turn_start(state)
    state = turn_start_status.after
    unsupported: list[str] = list(turn_start_status.unsupported_rules)

    def illegal(rules: tuple[str, ...] = ()) -> LogicTransition:
        return LogicTransition(
            before=state,
            after=state,
            card=card,
            legal=False,
            stamina_paid=0,
            force_stamina_paid=0,
            block_paid=0,
            end_turn_stamina_paid=0,
            immediate_score_gain=0,
            end_turn_score_gain=0,
            total_score_gain=0,
            unsupported_rules=rules,
            turn_ended=False,
            runtime_status_enchant_ids=(
                turn_start_status.fired_enchant_ids
            ),
            runtime_status_effect_ids=turn_start_status.fired_effect_ids,
            runtime_status_added_ids=turn_start_status.added_enchant_ids,
            runtime_status_draw_count=turn_start_status.draw_count,
        )

    if card.plan_type not in (PLAN_COMMON, PLAN_LOGIC):
        unsupported.append(f"plan:{card.plan_type}")
    if card.cost_type not in {
        COST_STAMINA,
        COST_GOOD_IMPRESSION,
        COST_MOTIVATION,
    }:
        unsupported.append(f"cost:{card.cost_type}")
    card_review_threshold: int | None = None
    card_block_threshold: int | None = None
    if card.play_trigger_id:
        if card.play_trigger_id.startswith(REVIEW_TRIGGER_PREFIX):
            try:
                card_review_threshold = int(
                    card.play_trigger_id.removeprefix(REVIEW_TRIGGER_PREFIX)
                )
            except ValueError:
                unsupported.append(f"card-trigger:{card.play_trigger_id}")
        elif card.play_trigger_id.startswith(BLOCK_TRIGGER_PREFIX):
            try:
                card_block_threshold = int(
                    card.play_trigger_id.removeprefix(BLOCK_TRIGGER_PREFIX)
                )
            except ValueError:
                unsupported.append(f"card-trigger:{card.play_trigger_id}")
        else:
            unsupported.append(f"card-trigger:{card.play_trigger_id}")
    if card.force_stamina_cost < 0:
        unsupported.append(f"force-stamina:{card.force_stamina_cost}")
    if card.observed_stamina_cost is not None and card.observed_stamina_cost < 0:
        unsupported.append(f"observed-stamina:{card.observed_stamina_cost}")
    if (
        card.observed_force_stamina_cost is not None
        and card.observed_force_stamina_cost < 0
    ):
        unsupported.append(
            f"observed-force-stamina:{card.observed_force_stamina_cost}"
        )
    if unsupported:
        return illegal(tuple(unsupported))
    if (
        card_review_threshold is not None
        and state.good_impression < card_review_threshold
    ):
        return illegal()
    if card_block_threshold is not None and state.block < card_block_threshold:
        return illegal()
    if (
        any(
            effect.effect_type == EFFECT_CARD_MOVE
            and SLEEP_CARD_MOVE_TO_LOST_PATTERN.fullmatch(effect.id)
            is not None
            for effect in card.effects
        )
        and state.trouble_card_count < 1
    ):
        # The client opens a no-target warning and does not apply the card's
        # later +play/enchantment effects when no Sleep trouble card exists.
        return illegal()

    block = state.block
    good_impression = state.good_impression
    good_impression_exists = state.good_impression_exists
    good_impression_passing = state.good_impression_passing_turn_start
    motivation = state.motivation
    block_paid = 0
    stamina_paid = 0
    good_impression_paid = 0
    motivation_paid = 0
    if card.cost_type == COST_STAMINA:
        effective_stamina_cost = (
            card.observed_stamina_cost
            if card.observed_stamina_cost is not None
            else card.stamina_cost
        )
        if (
            card.observed_stamina_cost is None
            and state.stamina_consumption_down_turns > 0
        ):
            effective_stamina_cost = _round_up_ratio(
                effective_stamina_cost, 500
            )
        if card.observed_stamina_cost is None:
            effective_force_before_fixed = card.force_stamina_cost
            if state.stamina_consumption_down_turns > 0:
                effective_force_before_fixed = _round_up_ratio(
                    effective_force_before_fixed, 500
                )
            remaining_fixed_reduction = max(
                0,
                state.stamina_consumption_down_fixed
                - effective_force_before_fixed,
            )
            effective_stamina_cost = max(
                0, effective_stamina_cost - remaining_fixed_reduction
            )
        block_paid = min(block, effective_stamina_cost)
        stamina_paid = effective_stamina_cost - block_paid
        block -= block_paid
    elif card.cost_type == COST_GOOD_IMPRESSION:
        good_impression_paid = card.cost_value
        if good_impression < good_impression_paid:
            return illegal()
        (
            good_impression,
            good_impression_exists,
            good_impression_passing,
        ) = _consume_review_scalar(
            good_impression,
            good_impression_exists,
            good_impression_passing,
            good_impression_paid,
        )
    else:
        motivation_paid = card.cost_value
        if motivation < motivation_paid:
            return illegal()
        motivation -= motivation_paid

    if card.observed_force_stamina_cost is not None:
        # The live card face is authoritative after every active reduction.
        # Keep its unavoidable portion separate so block still cannot absorb
        # it, but do not apply the discounts a second time.
        force_stamina_paid = card.observed_force_stamina_cost
    else:
        force_stamina_paid = card.force_stamina_cost
        if state.stamina_consumption_down_turns > 0:
            force_stamina_paid = _round_up_ratio(force_stamina_paid, 500)
        force_stamina_paid = max(
            0, force_stamina_paid - state.stamina_consumption_down_fixed
        )
    if stamina_paid + force_stamina_paid > state.stamina:
        return illegal()

    stamina_after_card = state.stamina - stamina_paid - force_stamina_paid
    trigger_good_impression = good_impression
    trigger_motivation = motivation
    immediate_raw_score = 0
    plays_after_card = state.plays_remaining - 1
    extra_turns = 0
    active_gain_layers = list(state.good_impression_gain_bonus_layers)
    new_gain_layers: list[tuple[int, int]] = []
    active_motivation_gain_layers = list(state.motivation_gain_bonus_layers)
    new_motivation_gain_layers: list[tuple[int, int]] = []
    new_stamina_consumption_down_turns = 0
    new_stamina_consumption_down_fixed = 0
    active_status_enchants = list(state.active_status_enchants)
    runtime_status_enchants = state.runtime_status_enchants
    status_change_fired_enchant_ids: list[str] = []
    status_change_fired_effect_ids: list[str] = []
    fired_item_status_change_enchantment_ids: list[str] = []
    item_status_change_uses = dict(state.item_enchantment_uses)
    new_status_enchants: list[MasterStatusEnchant] = []
    new_created_cards: list[tuple[str, int, str, int]] = []
    new_trouble_cards = 0
    moved_trouble_cards = 0
    preactivated_gain_effect_ids: set[str] = set()
    preactivated_motivation_gain_effect_ids: set[str] = set()
    for effect in card.effects:
        if effect.effect_type not in {
            EFFECT_GOOD_IMPRESSION_ADDITIVE,
            EFFECT_MOTIVATION_ADDITIVE,
        }:
            continue
        eligible = not effect.trigger_id
        if effect.trigger_id.startswith(REVIEW_TRIGGER_PREFIX):
            try:
                threshold = int(
                    effect.trigger_id.removeprefix(REVIEW_TRIGGER_PREFIX)
                )
            except ValueError:
                threshold = -1
            eligible = threshold >= 0 and good_impression >= threshold
        if not eligible:
            continue
        if effect.value1 <= 0 or effect.effect_turn <= 0:
            unsupported.append(f"effect-duration:{effect.id}")
            continue
        if effect.effect_type == EFFECT_GOOD_IMPRESSION_ADDITIVE:
            new_gain_layers.append((effect.value1, effect.effect_turn))
            preactivated_gain_effect_ids.add(effect.id)
        else:
            new_motivation_gain_layers.append(
                (effect.value1, effect.effect_turn)
            )
            preactivated_motivation_gain_effect_ids.add(effect.id)

    for effect in card.effects:
        if effect.trigger_id:
            threshold_prefix: str | None = None
            observed_value = 0
            if effect.trigger_id.startswith(MOTIVATION_TRIGGER_PREFIX):
                threshold_prefix = MOTIVATION_TRIGGER_PREFIX
                observed_value = trigger_motivation
            elif effect.trigger_id.startswith(CARD_PLAY_REVIEW_TRIGGER_PREFIX):
                threshold_prefix = CARD_PLAY_REVIEW_TRIGGER_PREFIX
                observed_value = trigger_good_impression
            elif effect.trigger_id.startswith(REVIEW_TRIGGER_PREFIX):
                threshold_prefix = REVIEW_TRIGGER_PREFIX
                observed_value = trigger_good_impression
            elif effect.trigger_id.startswith(TROUBLE_SEARCH_TRIGGER_PREFIX):
                raw_condition = effect.trigger_id.removeprefix(
                    TROUBLE_SEARCH_TRIGGER_PREFIX
                )
                raw_threshold, separator, search_id = raw_condition.partition("-")
                if separator and search_id == TROUBLE_NOT_LOST_SEARCH_ID:
                    try:
                        threshold = int(raw_threshold)
                    except ValueError:
                        unsupported.append(f"effect-trigger:{effect.trigger_id}")
                        continue
                    if state.trouble_card_count < threshold:
                        continue
                    threshold_prefix = ""
                else:
                    unsupported.append(f"effect-trigger:{effect.trigger_id}")
                    continue
            if threshold_prefix is None:
                unsupported.append(f"effect-trigger:{effect.trigger_id}")
                continue
            if threshold_prefix:
                try:
                    threshold = int(
                        effect.trigger_id.removeprefix(threshold_prefix)
                    )
                except ValueError:
                    unsupported.append(f"effect-trigger:{effect.trigger_id}")
                    continue
                if observed_value < threshold:
                    continue
        if effect.status_enchant_id:
            enchant = effect.status_enchant_rule
            if (
                enchant is None
                or enchant.id != effect.status_enchant_id
                or (
                    _end_turn_review_enchant_threshold(enchant) is None
                    and _end_turn_remaining_review_score_limit(enchant) is None
                    and _play_count_enchant_interval(enchant) is None
                )
            ):
                unsupported.append(f"status-enchant:{effect.status_enchant_id}")
            else:
                new_status_enchants.append(enchant)
            continue
        if effect.chain_effect_id:
            unsupported.append(f"chain:{effect.chain_effect_id}")
            continue

        if effect.effect_type == EFFECT_BLOCK:
            block += effect.value1 + motivation
        elif effect.effect_type == EFFECT_BLOCK_MULTIPLE_MOTIVATION:
            block += effect.value1 + _round_up_ratio(
                motivation, 1000 + effect.value2
            )
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION:
            (
                good_impression,
                good_impression_exists,
                good_impression_passing,
            ) = _gain_review_scalar(
                good_impression,
                good_impression_exists,
                good_impression_passing,
                _scale_review_with_layers(
                    effect.value1,
                    [*active_gain_layers, *new_gain_layers],
                ),
            )
        elif effect.effect_type == EFFECT_MOTIVATION:
            motivation_gain_bonus = sum(
                value
                for value, _turns in (
                    *active_motivation_gain_layers,
                    *new_motivation_gain_layers,
                )
            )
            motivation += scale_good_impression_gain(
                effect.value1, motivation_gain_bonus
            )
            # A status-change enchant resolves immediately after the direct
            # gain.  This is earlier than ExamCardPlayAfter item effects.
            changed = _resolve_installed_runtime_statuses(
                runtime_status_enchants,
                phase=STATUS_PHASE_STATUS_CHANGE,
                round_number=state.round_number,
                changed_effect_type=EFFECT_MOTIVATION,
                lesson_type=lesson_type,
            )
            unsupported.extend(changed.resolution.unsupported_rules)
            if changed.resolution.fully_supported:
                runtime_status_enchants = changed.enchantments
                status_change_fired_enchant_ids.extend(
                    changed.resolution.fired_enchant_ids
                )
                status_change_fired_effect_ids.extend(
                    changed.resolution.fired_effect_ids
                )
                new_gain_layers.extend(
                    changed.resolution.good_impression_gain_layers
                )
                new_motivation_gain_layers.extend(
                    changed.resolution.motivation_gain_layers
                )
            # Equipped P-item reactions share this exact native phase, but
            # are transient sources rather than card-play-after effects.
            # Keep their limits in item state and apply only all-supported
            # Master resolutions.
            for item_enchantment in status_change_item_enchantments:
                used = item_status_change_uses.get(item_enchantment.id, 0)
                if (
                    item_enchantment.max_uses > 0
                    and used >= item_enchantment.max_uses
                ):
                    continue
                try:
                    item_changed = resolve_status_enchants(
                        (item_enchantment.id,),
                        phase=STATUS_PHASE_STATUS_CHANGE,
                        round_number=state.round_number,
                        changed_effect_type=EFFECT_MOTIVATION,
                        lesson_type=lesson_type,
                        limits={
                            item_enchantment.id: item_enchantment.max_uses
                        },
                        uses={item_enchantment.id: used},
                    )
                except (KeyError, ValueError, sqlite3.Error) as error:
                    unsupported.append(
                        "item-status-change-load:"
                        f"{item_enchantment.id}:{type(error).__name__}"
                    )
                    continue
                if item_changed.unsupported_rules:
                    unsupported.extend(
                        f"item-status-change:{item_enchantment.id}:{rule}"
                        for rule in item_changed.unsupported_rules
                    )
                    continue
                if not item_changed.fired_enchant_ids:
                    continue
                fired_item_status_change_enchantment_ids.extend(
                    item_changed.fired_enchant_ids
                )
                item_status_change_uses[item_enchantment.id] = (
                    dict(item_changed.use_counts).get(
                        item_enchantment.id, used + 1
                    )
                )
                status_change_fired_enchant_ids.extend(
                    item_changed.fired_enchant_ids
                )
                status_change_fired_effect_ids.extend(
                    item_changed.fired_effect_ids
                )
                new_gain_layers.extend(
                    item_changed.good_impression_gain_layers
                )
                new_motivation_gain_layers.extend(
                    item_changed.motivation_gain_layers
                )
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION_MULTIPLE:
            (
                good_impression,
                good_impression_exists,
                good_impression_passing,
            ) = _gain_review_scalar(
                good_impression,
                good_impression_exists,
                good_impression_passing,
                _round_up_ratio(good_impression, effect.value1),
            )
        elif effect.effect_type == EFFECT_EXTRA_TURN:
            extra_turns += max(1, effect.value1, effect.effect_count)
        elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
            plays_after_card += max(1, effect.value1, effect.effect_count)
        elif effect.effect_type == EFFECT_CARD_DRAW:
            # The next live hand is read from the screen, so draw order does
            # not need to be guessed in the numerical shadow state.
            pass
        elif effect.effect_type == EFFECT_CARD_CREATE_ID:
            match = CARD_CREATE_ID_PATTERN.fullmatch(effect.id)
            if match is None:
                unsupported.append(f"card-create:{effect.id}")
            else:
                card_id, upgrade, position, count_min, count_max = (
                    match.groups()
                )
                if count_min != count_max:
                    unsupported.append(f"card-create-range:{effect.id}")
                else:
                    count = int(count_min)
                    new_created_cards.append(
                        (card_id, int(upgrade), position, count)
                    )
                    if card_id == SLEEP_TROUBLE_CARD_ID:
                        new_trouble_cards += count
        elif effect.effect_type == EFFECT_CARD_MOVE:
            if SLEEP_CARD_MOVE_TO_LOST_PATTERN.fullmatch(effect.id) is None:
                unsupported.append(f"card-move:{effect.id}")
            elif state.trouble_card_count > moved_trouble_cards:
                moved_trouble_cards += 1
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION_ADDITIVE:
            if effect.id in preactivated_gain_effect_ids:
                pass
            elif effect.value1 <= 0 or effect.effect_turn <= 0:
                unsupported.append(f"effect-duration:{effect.id}")
            else:
                new_gain_layers.append((effect.value1, effect.effect_turn))
        elif effect.effect_type == EFFECT_MOTIVATION_ADDITIVE:
            if effect.id in preactivated_motivation_gain_effect_ids:
                pass
            elif effect.value1 <= 0 or effect.effect_turn <= 0:
                unsupported.append(f"effect-duration:{effect.id}")
            else:
                new_motivation_gain_layers.append(
                    (effect.value1, effect.effect_turn)
                )
        elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
            if effect.effect_turn <= 0:
                unsupported.append(f"effect-duration:{effect.id}")
            else:
                new_stamina_consumption_down_turns += effect.effect_turn
        elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIXED:
            if effect.value1 <= 0 or effect.effect_turn != -1:
                unsupported.append(f"effect-duration:{effect.id}")
            else:
                new_stamina_consumption_down_fixed += effect.value1
        elif effect.effect_type == EFFECT_LESSON:
            repetitions = max(1, effect.effect_count)
            immediate_raw_score += effect.value1 * repetitions
        elif effect.effect_type == EFFECT_LESSON_BY_GOOD_IMPRESSION:
            repetitions = max(1, effect.effect_count)
            immediate_raw_score += _round_up_ratio(
                good_impression, effect.value1
            ) * repetitions
        elif effect.effect_type == EFFECT_LESSON_BY_BLOCK:
            repetitions = max(1, effect.effect_count)
            immediate_raw_score += _round_up_ratio(
                block, effect.value1
            ) * repetitions
        elif effect.effect_type == EFFECT_LESSON_BY_MOTIVATION:
            repetitions = max(1, effect.effect_count)
            immediate_raw_score += _round_up_ratio(
                motivation, effect.value1
            ) * repetitions
        else:
            unsupported.append(f"effect:{effect.effect_type}:{effect.id}")

    enchant_progress = dict(state.active_status_enchant_progress)
    if card.category in SKILL_CARD_CATEGORIES:
        for enchant in active_status_enchants:
            interval = _play_count_enchant_interval(enchant)
            if interval is None:
                continue
            progress = enchant_progress.get(enchant.id, 0) + 1
            if progress >= interval:
                for effect in enchant.effects:
                    repetitions = max(1, effect.effect_count)
                    immediate_raw_score += effect.value1 * repetitions
                progress %= interval
            enchant_progress[enchant.id] = progress
    for enchant in new_status_enchants:
        enchant_progress.setdefault(enchant.id, 0)

    # ExamCardPlayAfter item effects resolve after the selected card's own
    # effects, even when that card leaves another play available.  This is
    # distinct from ExamEndTurnInterval and therefore must happen before the
    # ``turn_ended`` branch below.
    additional_stamina_paid = 0
    for effect in post_card_effects:
        if effect.trigger_id or effect.status_enchant_id or effect.chain_effect_id:
            unsupported.append(f"post-card-effect-shape:{effect.id}")
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION:
            if (
                effect.value1 < 0
                or effect.value2 != 0
                or effect.effect_count != 0
                or effect.effect_turn != 0
            ):
                unsupported.append(f"post-card-effect-shape:{effect.id}")
                continue
            (
                good_impression,
                good_impression_exists,
                good_impression_passing,
            ) = _gain_review_scalar(
                good_impression,
                good_impression_exists,
                good_impression_passing,
                _scale_review_with_layers(
                    effect.value1,
                    [*active_gain_layers, *new_gain_layers],
                ),
            )
        elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
            if (
                effect.value1 != 0
                or effect.value2 != 0
                or effect.effect_count <= 0
                or effect.effect_turn != 0
            ):
                unsupported.append(f"post-card-effect-shape:{effect.id}")
                continue
            plays_after_card += effect.effect_count
        elif effect.effect_type == EFFECT_STAMINA_REDUCE_FIX:
            if (
                effect.value1 < 0
                or effect.value2 != 0
                or effect.effect_count != 0
                or effect.effect_turn != 0
            ):
                unsupported.append(f"post-card-effect-shape:{effect.id}")
                continue
            paid = min(stamina_after_card, effect.value1)
            stamina_after_card -= paid
            additional_stamina_paid += paid
        elif effect.effect_type == EFFECT_LESSON_BY_MOTIVATION:
            repetitions = max(1, effect.effect_count)
            immediate_raw_score += _round_up_ratio(
                motivation, effect.value1
            ) * repetitions
        elif effect.effect_type == EFFECT_BLOCK:
            block += effect.value1 + motivation
        elif effect.effect_type == EFFECT_BLOCK_MULTIPLE_MOTIVATION:
            block += effect.value1 + _round_up_ratio(
                motivation, 1000 + effect.value2
            )
        else:
            unsupported.append(
                f"post-card-effect:{effect.effect_type}:{effect.id}"
            )

    runtime_after_card = _resolve_installed_runtime_statuses(
        runtime_status_enchants,
        phase=STATUS_PHASE_CARD_PLAY_AFTER,
        round_number=state.round_number,
        played_card_id=card.id,
        played_card_upgrade=card.upgrade,
    )
    runtime_after_result = runtime_after_card.resolution
    unsupported.extend(runtime_after_result.unsupported_rules)
    if runtime_after_result.fully_supported:
        runtime_status_enchants = runtime_after_card.enchantments
        new_gain_layers.extend(
            runtime_after_result.good_impression_gain_layers
        )
        new_motivation_gain_layers.extend(
            runtime_after_result.motivation_gain_layers
        )
        motivation_gain_bonus = sum(
            value
            for value, _turns in (
                *active_motivation_gain_layers,
                *new_motivation_gain_layers,
            )
        )
        motivation += scale_good_impression_gain(
            runtime_after_result.motivation_add,
            motivation_gain_bonus,
        )
        gain_bonus = sum(
            value
            for value, _turns in (*active_gain_layers, *new_gain_layers)
        )
        if runtime_after_result.good_impression_add != 0:
            (
                good_impression,
                good_impression_exists,
                good_impression_passing,
            ) = _gain_review_scalar(
                good_impression,
                good_impression_exists,
                good_impression_passing,
                scale_good_impression_gain(
                    runtime_after_result.good_impression_add,
                    gain_bonus,
                ),
            )
        block += (
            runtime_after_result.block_base_add
            + runtime_after_result.block_effect_count * motivation
        )
        plays_after_card += runtime_after_result.playable_add
        new_stamina_consumption_down_turns += (
            runtime_after_result.stamina_consumption_down_turns
        )
        requested_trouble_moves = (
            runtime_after_result.trouble_cards_moved_to_lost
        )
        available_trouble = max(
            0,
            state.trouble_card_count
            + new_trouble_cards
            - moved_trouble_cards,
        )
        if requested_trouble_moves > available_trouble:
            unsupported.append("runtime-status-card-move-target-missing")
        moved_trouble_cards += min(
            requested_trouble_moves,
            available_trouble,
        )

    turn_ended = plays_after_card == 0
    if plays_after_card < 0:
        unsupported.append("playable-value-negative")

    # ``LogicTransition.end_turn_stamina_paid`` predates phase-specific item
    # support.  Preserve the serialized field while reporting all fixed item
    # stamina paid after the card, including ExamCardPlayAfter activations.
    end_turn_stamina_paid = additional_stamina_paid
    active_bonus_layers = list(state.good_impression_bonus_layers)
    new_bonus_layers: list[tuple[int, int]] = []
    end_turn_direct_raw_score = 0
    end_turn_direct_score = 0
    if turn_ended:
        for enchant in (*active_status_enchants, *new_status_enchants):
            threshold = _end_turn_review_enchant_threshold(enchant)
            if threshold is not None:
                if good_impression < threshold:
                    continue
                for effect in enchant.effects:
                    (
                        good_impression,
                        good_impression_exists,
                        good_impression_passing,
                    ) = _gain_review_scalar(
                        good_impression,
                        good_impression_exists,
                        good_impression_passing,
                        _scale_review_with_layers(
                            effect.value1,
                            [*active_gain_layers, *new_gain_layers],
                        ),
                    )
                continue

            remaining_turn_limit = _end_turn_remaining_review_score_limit(
                enchant
            )
            if remaining_turn_limit is not None:
                # Native field status comparison is current RemainingTurn <= N
                # and occurs before this function decrements turns_remaining.
                if state.turns_remaining > remaining_turn_limit:
                    continue
                for effect in enchant.effects:
                    raw = _round_up_ratio(good_impression, effect.value1)
                    for _ in range(effect.effect_count):
                        end_turn_direct_raw_score += raw
                        # CalculateAddingParameter is invoked per hit, so keep
                        # each float32 ceiling separate instead of scaling one
                        # aggregated raw total.
                        end_turn_direct_score += _round_up_ratio(
                            raw,
                            state.score_multiplier_permille,
                        )
                continue

            if _play_count_enchant_interval(enchant) is not None:
                continue
            unsupported.append(f"active-status-enchant:{enchant.id}")
        for effect in end_turn_effects:
            if effect.effect_type == EFFECT_GOOD_IMPRESSION_BONUS:
                if effect.value1 <= 0 or effect.effect_turn <= 0:
                    unsupported.append(f"end-turn-effect-duration:{effect.id}")
                else:
                    _merge_review_multiple_layer(
                        active_bonus_layers,
                        new_bonus_layers,
                        value=effect.value1,
                        turns=effect.effect_turn,
                    )
            elif effect.effect_type == EFFECT_STAMINA_REDUCE_FIX:
                paid = min(stamina_after_card, effect.value1)
                stamina_after_card -= paid
                end_turn_stamina_paid += paid
            elif effect.effect_type == EFFECT_LESSON_BY_MOTIVATION:
                repetitions = max(1, effect.effect_count)
                immediate_raw_score += _round_up_ratio(
                    motivation, effect.value1
                ) * repetitions
            elif effect.effect_type == EFFECT_BLOCK:
                block += effect.value1 + motivation
            elif effect.effect_type == EFFECT_BLOCK_MULTIPLE_MOTIVATION:
                block += effect.value1 + _round_up_ratio(
                    motivation, 1000 + effect.value2
                )
            else:
                unsupported.append(
                    f"end-turn-effect:{effect.effect_type}:{effect.id}"
                )

    end_turn_review_raw_score = (
        _scale_review_with_layers(
            good_impression,
            [*active_bonus_layers, *new_bonus_layers],
        )
        if turn_ended
        else 0
    )
    end_turn_raw_score = end_turn_direct_raw_score + end_turn_review_raw_score
    immediate_score = _round_up_ratio(
        immediate_raw_score, state.score_multiplier_permille
    )
    end_turn_score = end_turn_direct_score + _round_up_ratio(
        end_turn_review_raw_score,
        state.score_multiplier_permille,
    )
    total_score = immediate_score + end_turn_score

    if turn_ended:
        # The native timer extension keys decay to the active instance's
        # IsPassingTurnStart flag, not to a scalar snapshot. It then removes
        # every limited Review instance whose Turn is <= 0. SpendInitialTurn
        # does not select Review, so round one marks survivors as passing
        # without decrementing them.
        next_good_impression = good_impression
        next_good_impression_exists = good_impression_exists
        next_good_impression_passing = good_impression_passing
        if state.round_number > 1 and next_good_impression_exists:
            if next_good_impression_passing:
                next_good_impression -= 1
            if next_good_impression <= 0:
                next_good_impression = 0
                next_good_impression_exists = False
                next_good_impression_passing = False
        if next_good_impression_exists:
            # ExamLoopTaskAsync marks every surviving active status after the
            # current TurnStart snapshot has been processed.
            next_good_impression_passing = True
        next_bonus_layers = tuple(
            (value, turns - 1)
            for value, turns in active_bonus_layers
            if turns > 1
        ) + tuple(new_bonus_layers)
        fresh_gain_start = (
            len(active_gain_layers)
            - state.good_impression_gain_bonus_fresh_count
        )
        next_gain_layers = tuple(
            (value, turns - 1)
            for value, turns in active_gain_layers[:fresh_gain_start]
            if turns > 1
        ) + tuple(active_gain_layers[fresh_gain_start:]) + tuple(
            new_gain_layers
        )
        next_gain_fresh_count = 0
        fresh_motivation_gain_start = (
            len(active_motivation_gain_layers)
            - state.motivation_gain_bonus_fresh_count
        )
        next_motivation_gain_layers = tuple(
            (value, turns - 1)
            for value, turns in active_motivation_gain_layers[
                :fresh_motivation_gain_start
            ]
            if turns > 1
        ) + tuple(
            active_motivation_gain_layers[fresh_motivation_gain_start:]
        ) + tuple(new_motivation_gain_layers)
        next_motivation_gain_fresh_count = 0
        next_round = state.round_number + 1
        next_turns = max(0, state.turns_remaining - 1)
        next_pending_extra_turns = (
            state.pending_extra_turns + extra_turns
        )
        if next_turns == 0 and next_pending_extra_turns > 0:
            next_turns = next_pending_extra_turns
            next_pending_extra_turns = 0
        stamina_down_total = (
            state.stamina_consumption_down_turns
            + new_stamina_consumption_down_turns
        )
        stamina_down_fresh = (
            state.stamina_consumption_down_fresh
            or (
                state.stamina_consumption_down_turns == 0
                and new_stamina_consumption_down_turns > 0
            )
        )
        next_stamina_consumption_down_turns = (
            stamina_down_total
            if stamina_down_fresh
            else max(0, stamina_down_total - 1)
        )
        next_stamina_consumption_down_fresh = False
        next_plays = 1
    else:
        next_good_impression = good_impression
        next_good_impression_exists = good_impression_exists
        next_good_impression_passing = good_impression_passing
        next_bonus_layers = tuple(active_bonus_layers)
        next_gain_layers = tuple((*active_gain_layers, *new_gain_layers))
        next_gain_fresh_count = (
            state.good_impression_gain_bonus_fresh_count
            + len(new_gain_layers)
        )
        next_motivation_gain_layers = tuple(
            (*active_motivation_gain_layers, *new_motivation_gain_layers)
        )
        next_motivation_gain_fresh_count = (
            state.motivation_gain_bonus_fresh_count
            + len(new_motivation_gain_layers)
        )
        next_round = state.round_number
        next_turns = state.turns_remaining
        next_pending_extra_turns = state.pending_extra_turns + extra_turns
        next_stamina_consumption_down_turns = (
            state.stamina_consumption_down_turns
            + new_stamina_consumption_down_turns
        )
        next_stamina_consumption_down_fresh = (
            state.stamina_consumption_down_fresh
            or (
                state.stamina_consumption_down_turns == 0
                and new_stamina_consumption_down_turns > 0
            )
        )
        next_plays = plays_after_card

    next_bonus = sum(value for value, _turns in next_bonus_layers)
    next_bonus_turns = min(
        (turns for _value, turns in next_bonus_layers), default=0
    )
    next_gain_bonus = sum(value for value, _turns in next_gain_layers)
    next_gain_turns = min(
        (turns for _value, turns in next_gain_layers), default=0
    )
    next_motivation_gain_bonus = sum(
        value for value, _turns in next_motivation_gain_layers
    )
    next_motivation_gain_turns = min(
        (turns for _value, turns in next_motivation_gain_layers), default=0
    )
    after = replace(
        state,
        turns_remaining=next_turns,
        stamina=stamina_after_card,
        score=state.score + total_score,
        block=block,
        good_impression=next_good_impression,
        good_impression_exists=next_good_impression_exists,
        good_impression_passing_turn_start=next_good_impression_passing,
        motivation=motivation,
        round_number=next_round,
        good_impression_bonus_permille=next_bonus,
        good_impression_bonus_turns=next_bonus_turns,
        good_impression_bonus_layers=next_bonus_layers,
        good_impression_gain_bonus_permille=next_gain_bonus,
        good_impression_gain_bonus_turns=next_gain_turns,
        good_impression_gain_bonus_layers=next_gain_layers,
        good_impression_gain_bonus_fresh_count=next_gain_fresh_count,
        motivation_gain_bonus_permille=next_motivation_gain_bonus,
        motivation_gain_bonus_turns=next_motivation_gain_turns,
        motivation_gain_bonus_layers=next_motivation_gain_layers,
        motivation_gain_bonus_fresh_count=(
            next_motivation_gain_fresh_count
        ),
        plays_remaining=next_plays,
        lost_card_count=(
            state.lost_card_count + 1
            if card.move_position_type == "ProduceCardMovePositionType_Lost"
            else state.lost_card_count
        ) + moved_trouble_cards,
        trouble_card_count=max(
            0,
            state.trouble_card_count
            + new_trouble_cards
            - moved_trouble_cards
            - (
                1
                if card.category == "ProduceCardCategory_Trouble"
                and card.move_position_type
                == "ProduceCardMovePositionType_Lost"
                else 0
            ),
        ),
        created_cards=tuple((*state.created_cards, *new_created_cards)),
        pending_extra_turns=next_pending_extra_turns,
        stamina_consumption_down_turns=(
            next_stamina_consumption_down_turns
        ),
        stamina_consumption_down_fresh=(
            next_stamina_consumption_down_fresh
        ),
        stamina_consumption_down_fixed=(
            state.stamina_consumption_down_fixed
            + new_stamina_consumption_down_fixed
        ),
        active_status_enchants=tuple(
            (*active_status_enchants, *new_status_enchants)
        ),
        active_status_enchant_progress=tuple(sorted(enchant_progress.items())),
        runtime_status_enchants=runtime_status_enchants,
        item_enchantment_uses=tuple(
            sorted(
                {
                    **item_status_change_uses,
                    **{
                        enchantment_id: (
                            item_status_change_uses.get(enchantment_id, 0)
                            + fired_item_enchantment_ids.count(enchantment_id)
                        )
                        for enchantment_id in fired_item_enchantment_ids
                    },
                }.items()
            )
        ),
    )
    return LogicTransition(
        before=state,
        after=after,
        card=card,
        legal=True,
        stamina_paid=stamina_paid,
        force_stamina_paid=force_stamina_paid,
        block_paid=block_paid,
        end_turn_stamina_paid=end_turn_stamina_paid,
        immediate_score_gain=immediate_score,
        end_turn_score_gain=end_turn_score,
        total_score_gain=total_score,
        unsupported_rules=tuple(unsupported),
        immediate_raw_score_gain=immediate_raw_score,
        end_turn_raw_score_gain=end_turn_raw_score,
        end_turn_stamina_recovered=0,
        good_impression_paid=good_impression_paid,
        motivation_paid=motivation_paid,
        turn_ended=turn_ended,
        runtime_status_enchant_ids=tuple(
            (*turn_start_status.fired_enchant_ids,
             *status_change_fired_enchant_ids,
             *runtime_after_result.fired_enchant_ids)
        ),
        runtime_status_effect_ids=tuple(
            (*turn_start_status.fired_effect_ids,
             *status_change_fired_effect_ids,
             *runtime_after_result.fired_effect_ids)
        ),
        runtime_status_added_ids=tuple(
            (*turn_start_status.added_enchant_ids,
             *runtime_after_card.added_enchant_ids)
        ),
        runtime_status_draw_count=(
            turn_start_status.draw_count + runtime_after_result.draw_count
        ),
    )


_TURN_SKIP_CARD = MasterCard(
    id="gkms_tool-turn-skip",
    upgrade=0,
    name="SKIP",
    plan_type=PLAN_COMMON,
    category="",
    stamina_cost=0,
    cost_type=COST_STAMINA,
    cost_value=0,
    play_trigger_id="",
    move_position_type=CARD_MOVE_UNKNOWN,
    effects=(),
)


def apply_logic_turn_skip(
    state: LogicExamState,
    *,
    end_turn_effects: tuple[MasterCardEffect, ...] = (),
    lesson_type: str = LESSON_UNKNOWN,
) -> LogicTransition:
    """Resolve the manual ExamTurnSkip command without pretending to play a card.

    Native applies the fixed recovery before ExamTurnSkip/TurnEnd.  A maximum
    stamina observation is therefore mandatory; callers otherwise receive a
    blocking diagnostic rather than an optimistic recovery estimate.
    """
    if state.max_stamina <= 0:
        return LogicTransition(
            before=state, after=state, card=_TURN_SKIP_CARD, legal=False,
            stamina_paid=0, force_stamina_paid=0, block_paid=0,
            end_turn_stamina_paid=0, immediate_score_gain=0,
            end_turn_score_gain=0, total_score_gain=0,
            unsupported_rules=("turn-skip-max-stamina-unknown",),
            turn_ended=False,
        )
    recovered = min(
        state.max_stamina,
        state.stamina + LESSON_SKIP_BASE_RECOVERY,
    ) - state.stamina
    # TurnEnd consumes the turn regardless of remaining playable-card count.
    recovered_state = replace(
        state, stamina=state.stamina + recovered, plays_remaining=1
    )
    transition = apply_logic_card(
        recovered_state,
        _TURN_SKIP_CARD,
        end_turn_effects=end_turn_effects,
        lesson_type=lesson_type,
    )
    return replace(
        transition,
        before=state,
        end_turn_stamina_recovered=recovered,
    )


def complete_audition_if_forced(
    transition: LogicTransition,
    force_end_score: int,
) -> LogicTransition:
    """Apply ExamSetting recovery when an audition ends with unused turns."""

    if force_end_score <= 0:
        raise ValueError("force_end_score must be positive")
    after = transition.after
    if not transition.legal or after.score < force_end_score:
        return transition

    score_room = max(0, force_end_score - transition.before.score)
    realized_immediate = min(transition.immediate_score_gain, score_room)
    realized_end = min(
        transition.end_turn_score_gain,
        max(0, score_room - realized_immediate),
    )
    # The client awards recovery from the number shown when the winning card
    # was selected.  A live 2-turn finish confirmed 18 - 1 + (2 * 2) == 21.
    recovery = (
        transition.before.turns_remaining
        * after.turn_end_stamina_recovery
    )
    if after.max_stamina > 0:
        recovery = min(recovery, max(0, after.max_stamina - after.stamina))
    completed = replace(
        after,
        turns_remaining=0,
        stamina=after.stamina + recovery,
        score=force_end_score,
    )
    completed.validate(allow_completed=True)
    return replace(
        transition,
        after=completed,
        immediate_score_gain=realized_immediate,
        end_turn_score_gain=realized_end,
        total_score_gain=realized_immediate + realized_end,
        end_turn_stamina_recovered=(
            transition.end_turn_stamina_recovered + recovery
        ),
    )
