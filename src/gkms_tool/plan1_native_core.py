"""Small fail-closed Plan 1 card core backed by local Master and Android 3.2.3.

This is deliberately a *single-card* kernel.  It does not own a deck shuffle,
turn lifecycle, general passive/item dispatch, or a search policy.  The native Hand
transaction recovered for Android 3.2.3 gives the ordering used here::

    play gate -> ordinary stamina cost -> captured ExamCardPlay listeners
    -> ordered playEffects -> MovePlayCard

``MovePlayCard`` increments the global play count and consumes one playable
value after the direct effects.  The narrow ``ExamEffectTimer`` slice compiled
here installs a typed one-shot timer through a stage-owned resolver; it never
executes the child in the installing card slot.  The only listener admitted is
the exact mandatory beforeProduce item for FKTN SSR Plan 1.  Other
difference-triggered commands, status enchants, timer child families, move
effects, and per-effect triggers remain fail closed.

The score, block, and stamina arithmetic is not duplicated here.  It bridges
to :mod:`gkms_tool.native_exam_formula`, whose helpers are reconstructed from
``ExamEffectUtility`` and ``ExamSequence.ConsumeCardCost``.

Raw Master terminology is retained.  In the Japanese UI
``ExamParameterBuff`` is 好調 and ``ExamLessonBuff`` is 集中.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Sequence

import yaml

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .plan2_search_play_card_stamina_consumption_change import (
    SearchPlayCardStaminaContract, SearchPlayCardStaminaRuntime,
    resolve_plan2_search_play_card_stamina_consumption_change_from_rows,
    resolve_plan2_search_play_card_stamina_payment, install_search_play_card_stamina_status,
)
from .nia_lesson_value_multiple import (
    LessonParameterMultipleState,
    LessonParameterMultipleStatus,
    install_lesson_value_multiple,
)
from .plan2_card_move import (
    CARD_MOVE_EFFECT_TYPE as EFFECT_CARD_MOVE,
    CardMoveResolutionError,
    Plan2CardMoveContract,
    assert_plan2_card_move_lost_random_direct_contract,
    assert_plan2_card_move_lost_random_direct_search,
    resolve_plan2_card_move_contract,
)
from .plan3_card_create_id import (
    CardCreateContract,
    CardCreateUnresolvedInput,
    try_resolve_card_create_contract,
)
from .plan3_card_upgrade import (
    EFFECT_EXAM_CARD_UPGRADE as EFFECT_CARD_UPGRADE,
    Plan3CardUpgradeBranch,
    Plan3CardUpgradeContract,
    resolve_plan3_card_upgrade,
)
from .plan3_hand_grave_draw import (
    HandGraveDrawContract,
    HandGraveDrawContractError,
)
from .plan3_stamina_multiple_trigger import (
    PHASE_CARD_PLAY,
    STAMINA_MULTIPLE_TRIGGER_BY_ID,
    StaminaSnapshot,
    evaluate_trigger as evaluate_stamina_multiple_trigger,
)
from .native_exam_formula import (
    F32_NEGATIVE_EPSILON,
    INT32_MAX,
    AddBlockSettings,
    AddBlockStatus,
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    NativeFormulaDomainError,
    ParameterApplication,
    ParameterApplicationStatus,
    ProduceParameterType,
    StaminaPaymentSettings,
    StaminaPaymentStatus,
    StaminaGrowEffect,
    StaminaGrowEffectType,
    apply_parameter_add,
    calculate_add_block,
    calculate_adding_parameter,
    calculate_buff_cost,
    calculate_effective_stamina_cost,
    ceil_f32_to_i32,
    f32,
    get_stamina_cost,
    permille_to_f32,
    split_stamina_payment,
)


PLAN_COMMON = "ProducePlanType_Common"
PLAN1 = "ProducePlanType_Plan1"
PLAN1_ARCHETYPE = "ProduceExamEffectType_ExamParameterBuff"

EFFECT_UNKNOWN = "ProduceExamEffectType_Unknown"
EFFECT_LESSON = "ProduceExamEffectType_ExamLesson"
EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
EFFECT_BLOCK_FIX = "ProduceExamEffectType_ExamBlockFix"
EFFECT_PARAMETER_BUFF = "ProduceExamEffectType_ExamParameterBuff"
EFFECT_LESSON_BUFF = "ProduceExamEffectType_ExamLessonBuff"
EFFECT_LESSON_BUFF_ADDITIVE = (
    "ProduceExamEffectType_ExamLessonBuffAdditive"
)
EFFECT_LESSON_VALUE_MULTIPLE = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
EFFECT_PLAYABLE_VALUE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_CARD_DRAW = "ProduceExamEffectType_ExamCardDraw"
EFFECT_CARD_CREATE_ID = "ProduceExamEffectType_ExamCardCreateId"
EFFECT_STATUS_ENCHANT = "ProduceExamEffectType_ExamStatusEnchant"
EFFECT_TIMER = "ProduceExamEffectType_ExamEffectTimer"
EFFECT_HAND_GRAVE_COUNT_CARD_DRAW = (
    "ProduceExamEffectType_ExamHandGraveCountCardDraw"
)
EFFECT_STAMINA_RECOVER_FIX = "ProduceExamEffectType_ExamStaminaRecoverFix"
EFFECT_STAMINA_REDUCE_FIX = "ProduceExamEffectType_ExamStaminaReduceFix"
EFFECT_SEARCH_STAMINA_CHANGE = "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange"
EFFECT_STAMINA_RECOVER_MULTIPLE = (
    "ProduceExamEffectType_ExamStaminaRecoverMultiple"
)
EFFECT_ANTI_DEBUFF = "ProduceExamEffectType_ExamAntiDebuff"
EFFECT_STAMINA_CONSUMPTION_DOWN = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_STAMINA_CONSUMPTION_ADD = (
    "ProduceExamEffectType_ExamStaminaConsumptionAdd"
)
EFFECT_STAMINA_CONSUMPTION_DOWN_FIX = (
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix"
)
EFFECT_MULTIPLE_LESSON_BUFF_LESSON = (
    "ProduceExamEffectType_ExamMultipleLessonBuffLesson"
)
EFFECT_LESSON_DEPEND_PARAMETER_BUFF = (
    "ProduceExamEffectType_ExamLessonDependParameterBuff"
)
# These two effect kinds are used by the FKTN SSR Plan 1 initial deck.  Their
# Android executors are small wrappers around the same native lesson/status
# helpers used by the direct effects above (see the native addresses called
# out in the module docstring below).
EFFECT_LESSON_ADD_MULTIPLE_PARAMETER_BUFF = (
    "ProduceExamEffectType_ExamLessonAddMultipleParameterBuff"
)
EFFECT_PARAMETER_BUFF_MULTIPLE_PER_TURN = (
    "ProduceExamEffectType_ExamParameterBuffMultiplePerTurn"
)

# This is deliberately a closed minimum slice.  These children already have
# exact Plan1 scalar/ordered-zone executors; every other Timer child remains a
# typed compiler blocker even when its direct effect may be supported later.
PLAN1_TIMER_CHILD_EFFECT_TYPES = frozenset(
    {
        EFFECT_CARD_DRAW,
        EFFECT_CARD_UPGRADE,
        EFFECT_LESSON,
        EFFECT_LESSON_DEPEND_PARAMETER_BUFF,
    }
)

# The only per-effect trigger admitted by this slice.  Its exact PC Master
# row and Android pre-payment binary32 predicate are validated below/reused
# from the shared native evidence helper.
PLAN1_TIMER_STAMINA_TRIGGER_ID = (
    "e_trigger-exam_card_play-stamina_up_multiple-500"
)
# The direct ExamCardPlay status slot uses the same native trigger row as the
# timer family above, but is intentionally scoped to the one Master card whose
# effect shape is proven in this Plan1 slice.  The timer path remains available
# to its existing Plan1 cards below.
PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_TRIGGER_ID = (
    "e_trigger-exam_card_play-stamina_up_multiple-500"
)
PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_CARD_ID = "p_card-00-men-1_007"

# Exact per-effect ExamCardPlay field-status predicates recovered from the
# Android ``IsCardPlayValidTrigger`` path.  These are effect-slot links (the
# card itself remains un-gated); the live pre-payment scalar is sampled before
# the ordinary cost/direct-effect sequence.  All other effect-slot triggers
# remain fail-closed.
PLAN1_CARD_PLAY_PARAMETER_BUFF_TRIGGER_ID = (
    "e_trigger-exam_card_play-parameter_buff"
)
PLAN1_CARD_PLAY_PARAMETER_BUFF_GATE_CARD_ID = "p_card-01-ido-3_074"
PLAN1_CARD_PLAY_LESSON_BUFF_GATE_TRIGGER_ID = (
    "e_trigger-none-lesson_buff_up-3"
)
PLAN1_CARD_PLAY_LESSON_BUFF_GATE_CARD_ID = "p_card-01-act-0_028"
PLAN1_CARD_PLAY_LESSON_BUFF_UP_3_TRIGGER_ID = (
    "e_trigger-exam_card_play-lesson_buff_up-3"
)
PLAN1_CARD_PLAY_LESSON_BUFF_UP_6_TRIGGER_ID = (
    "e_trigger-exam_card_play-lesson_buff_up-6"
)
PLAN1_CARD_PLAY_STAMINA_LESS_MULTIPLE_500_TRIGGER_ID = (
    "e_trigger-exam_card_play-stamina_less_multiple-500"
)
PLAN1_EXACT_EFFECT_TRIGGER_IDS = frozenset(
    {
        PLAN1_CARD_PLAY_PARAMETER_BUFF_TRIGGER_ID,
        PLAN1_CARD_PLAY_LESSON_BUFF_UP_3_TRIGGER_ID,
        PLAN1_CARD_PLAY_LESSON_BUFF_UP_6_TRIGGER_ID,
        PLAN1_CARD_PLAY_STAMINA_LESS_MULTIPLE_500_TRIGGER_ID,
        PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_TRIGGER_ID,
    }
)

PLAN1_HAND_GRAVE_DRAW_CARD_IDS = frozenset(
    {
        "p_card-00-men-3_005",
        "p_card-01-sup-3_109",
    }
)

COST_STAMINA = "ExamCostType_Unknown"
COST_LESSON_BUFF = "ExamCostType_ExamLessonBuff"
COST_PARAMETER_BUFF = "ExamCostType_ExamParameterBuff"
COST_PARAMETER_BUFF_MULTIPLE_PER_TURN = "ExamCostType_ExamParameterBuffMultiplePerTurn"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
MOVE_GRAVE = "ProduceCardMovePositionType_Grave"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
MOVE_DECK_RANDOM = "ProduceCardMovePositionType_DeckRandom"
MOVE_EFFECT_UNKNOWN = "ProduceCardMoveEffectTriggerType_Unknown"
CATEGORY_ACTIVE = "ProduceCardCategory_ActiveSkill"
CATEGORY_MENTAL = "ProduceCardCategory_MentalSkill"

# ConsumeCardCost -> ConsumeBuffCost dispatches by enum and Master value,
# not card ID. These are the Plan1 resources already represented by scalar
# state; other cost enums still need their own state/consumption owner.
# Android call sites: 0x7E60088 (Lesson), 0x7E600B0 (Parameter),
# 0x7E60060 (ParameterMultiplePerTurn), after shared binary32 rounding.
_PLAN1_BUFF_COST_RESOURCES = {
    COST_LESSON_BUFF: "lesson_buff",
    COST_PARAMETER_BUFF: "parameter_buff_turns",
    COST_PARAMETER_BUFF_MULTIPLE_PER_TURN: "parameter_buff_multiple_per_turn_turns",
}


def _supported_plan1_cost(cost_type: str, value: int) -> bool:
    return (type(value) is int and 0 <= value <= INT32_MAX
            and (cost_type in _PLAN1_BUFF_COST_RESOURCES or cost_type == COST_STAMINA and value == 0))

FKTN_SSR_PLAN1_IDOL_CARD_ID = "i_card-fktn-3-001"
FKTN_SSR_PLAN1_UNIQUE_CARD_ID = "p_card-01-ido-3_066"
FKTN_SSR_PLAN1_BEFORE_ITEM_ID = "pitem_01-3-030-0"
FKTN_SSR_PLAN1_BEFORE_ITEM_EFFECT_ID = (
    "p_item_effect-exam_status_enchant-inf-01-"
    "enchant-pitem_01-3-030-0-enc01"
)
FKTN_SSR_PLAN1_BEFORE_ITEM_STATUS_ID = (
    "enchant-p_item_effect_01-3-030-0-enc01"
)
FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID = (
    "e_trigger-exam_card_play-p_card_search-playing-"
    "p_card-01-ido-3_066-0_1"
)
FKTN_SSR_PLAN1_BEFORE_ITEM_SEARCH_ID = (
    "p_card_search-playing-p_card-01-ido-3_066"
)
FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS = (
    "e_effect-exam_parameter_buff-03",
    "e_effect-exam_block_fix-0005",
)
FKTN_SSR_PLAN1_IDOL_FRAGMENT_ID = "initial_deck-parameter_buff"
FKTN_SSR_PLAN1_MODE_DECK_ID = "initial_deck-produce_default-parameter_buff"
FKTN_SSR_PLAN1_MODE_CARD_REFS = (
    ("p_card-00-act-0_001", 0),
    ("p_card-00-act-0_001", 0),
    ("p_card-00-act-0_002", 0),
    ("p_card-00-men-0_003", 0),
    ("p_card-00-men-0_003", 0),
    ("p_card-01-men-0_007", 0),
    ("p_card-01-men-0_007", 0),
    ("p_card-01-act-0_005", 0),
)
FKTN_SSR_PLAN1_CHARACTER_CARD_REFS = (
    ("p_card-01-men-0_007", 0),
    ("p_card-01-act-0_022", 0),
)
INITIAL_REGULAR_PRODUCE_ID = "produce-001"
DEFAULT_EXAM_SETTING_ID = "p_exam_setting-1"
DEFAULT_MASTER_DIR = Path(__file__).resolve().parents[2] / "_research/gakumasu-diff"

# Android v3.2.3 ExecuteCardCommandImpl / PlayEffect / MovePlayCard order for
# this deliberately non-reactive slice.  The compiler rejects the trigger,
# status, timer, and move-effect shapes that would insert extra commands.
NATIVE_CARD_TRANSACTION_ORDER = (
    "accepted-play-gate",
    "ordinary-stamina-cost",
    "fktn-before-produce-item-exam-card-play-effects",
    "direct-effects-in-master-order",
    "move-play-card-consume-playable-and-increment-count",
)

_SUPPORTED_EFFECT_TYPES = frozenset(
    {
        EFFECT_LESSON,
        EFFECT_BLOCK,
        EFFECT_BLOCK_FIX,
        EFFECT_PARAMETER_BUFF,
        EFFECT_LESSON_BUFF,
        EFFECT_LESSON_VALUE_MULTIPLE,
        EFFECT_PLAYABLE_VALUE_ADD,
        EFFECT_CARD_DRAW,
        EFFECT_CARD_UPGRADE,
        EFFECT_CARD_MOVE,
        EFFECT_CARD_CREATE_ID,
        EFFECT_TIMER,
        EFFECT_HAND_GRAVE_COUNT_CARD_DRAW,
        EFFECT_STAMINA_RECOVER_FIX,
        EFFECT_STAMINA_REDUCE_FIX,
        EFFECT_SEARCH_STAMINA_CHANGE,
        EFFECT_STAMINA_RECOVER_MULTIPLE,
        EFFECT_ANTI_DEBUFF,
        EFFECT_STAMINA_CONSUMPTION_DOWN,
        EFFECT_STAMINA_CONSUMPTION_ADD,
        EFFECT_STAMINA_CONSUMPTION_DOWN_FIX,
        EFFECT_MULTIPLE_LESSON_BUFF_LESSON,
        EFFECT_LESSON_DEPEND_PARAMETER_BUFF,
        EFFECT_LESSON_ADD_MULTIPLE_PARAMETER_BUFF,
        EFFECT_PARAMETER_BUFF_MULTIPLE_PER_TURN,
    }
)


class Plan1NativeContractError(ValueError):
    """The pinned acceptance Master projection is missing or contradictory."""


@dataclass(frozen=True, slots=True, order=True)
class Plan1Blocker:
    """One stable reason why a program cannot be executed exactly."""

    code: str
    source_id: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.source_id:
            raise ValueError("blocker code and source_id must be non-empty")


class Plan1DeckRole(StrEnum):
    COMMON = "common"
    PLAN1_STARTER = "plan1-starter"
    CHARACTER = "character"
    IDOL_EXCLUSIVE = "idol-exclusive"


@dataclass(frozen=True, slots=True)
class Plan1DeckEntry:
    card_id: str
    upgrade: int
    role: Plan1DeckRole

    def __post_init__(self) -> None:
        if not self.card_id:
            raise ValueError("card_id must be non-empty")
        if isinstance(self.upgrade, bool) or not isinstance(self.upgrade, int):
            raise TypeError("upgrade must be an integer")
        if self.upgrade < 0:
            raise ValueError("upgrade must be non-negative")


@dataclass(frozen=True, slots=True)
class Plan1RequiredItem:
    """Exact beforeProduce item graph retained as a manifest-only boundary."""

    item_id: str
    item_effect_ids: tuple[str, ...]
    status_enchant_ids: tuple[str, ...]
    trigger_ids: tuple[str, ...]
    nested_effect_ids: tuple[str, ...]
    execution_blocker: Plan1Blocker


@dataclass(frozen=True, slots=True)
class Plan1InitialDeckManifest:
    idol_card_id: str
    produce_id: str
    exam_effect_type: str
    mode_deck_id: str
    entries: tuple[Plan1DeckEntry, ...]
    idol_fragment_id: str
    idol_fragment_entries: tuple[Plan1DeckEntry, ...]
    idol_fragment_in_effective_deck: bool
    before_produce_item: Plan1RequiredItem

    @property
    def card_count(self) -> int:
        return len(self.entries)

    @property
    def card_counts(self) -> Counter[tuple[str, int]]:
        return Counter((entry.card_id, entry.upgrade) for entry in self.entries)


@dataclass(frozen=True, slots=True)
class Plan1NativeSettings:
    exam_setting_id: str
    adding_parameter: AddingParameterSettings
    add_block: AddBlockSettings
    stamina_payment: StaminaPaymentSettings
    hand_limit: int
    # ConsumeBuffCost's global status multipliers.  Defaults preserve the
    # historical positional constructor while the Master projection below
    # binds the exact p_exam_setting-1 values.
    buff_consumption_down_permille: int = 0
    buff_consumption_add_permille: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.hand_limit, bool)
            or not isinstance(self.hand_limit, int)
            or not 1 <= self.hand_limit <= INT32_MAX
        ):
            raise ValueError("hand_limit must be a positive Int32")
        for name in (
            "buff_consumption_down_permille",
            "buff_consumption_add_permille",
        ):
            _plain_i32(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Plan1CompiledEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    blockers: tuple[Plan1Blocker, ...] = ()
    # Structural CardCreateId payload.  These stay neutral for every other
    # effect kind.  Keeping them typed avoids recovering behavior from the
    # effect-id string at runtime.
    target_card_id: str = ""
    target_upgrade: int = 0
    move_position_type: str = MOVE_UNKNOWN
    pick_count_min: int = 0
    pick_count_max: int = 0
    # ExamEffectTimer owns exactly one ordered child in the supported slice.
    # Keeping the compiled child here avoids recovering behavior from the
    # parent id and lets runtime code reject hand-built/nested shapes.
    timer_child: Plan1CompiledEffect | None = None
    # Card play-effect slot predicate.  Only the exact pre-payment stamina
    # threshold used by the Timer whitelist can populate this field.
    activation_trigger_id: str = ""
    # Persistent status installed by ExamStatusEnchant.  Transition-local
    # replay projection observes the installed runtime in the next captured
    # state; this field proves the no-immediate-scalar installer itself.
    status_enchant_id: str = ""
    # Typed proof of the sole value1-zero HandGraveCountCardDraw row.
    hand_grave_draw_contract: HandGraveDrawContract | None = None
    # Shared plan-neutral CardMove contract.  Execution remains stage-owned
    # because it mutates ordered GUID zones and native RNG, not scalar state.
    card_move_contract: Plan2CardMoveContract | None = None
    # The exact search row is compiled from the caller's versioned database.
    # Runtime must not reopen DEFAULT_DATABASE and silently mix Master builds.
    card_move_search: ProduceCardSearchRule | None = None
    # Shared plan-neutral Hand-All CardUpgrade contract.  Plan1 retains its
    # own ordered-zone runtime and delegates only Master/search semantics.
    card_upgrade_contract: Plan3CardUpgradeContract | None = None
    search_stamina_contract: SearchPlayCardStaminaContract | None = None

    @property
    def executable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class Plan1EquippedItemRuntime:
    """One stage-local use counter for the exact FKTN SSR mandatory item.

    This is intentionally not a general P-item DTO.  Static identities and
    ordered child effects are retained so the stage can validate the complete
    Master binding before it dispatches the sole whitelisted listener.
    """

    item_id: str
    item_effect_id: str
    status_enchant_id: str
    trigger_id: str
    phase_type: str
    target_card_id: str
    effects: tuple[Plan1CompiledEffect, ...]
    remaining_uses: int
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "item_id",
            "item_effect_id",
            "status_enchant_id",
            "trigger_id",
            "phase_type",
            "target_card_id",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be non-empty text")
        effects = tuple(self.effects)
        if any(not isinstance(value, Plan1CompiledEffect) for value in effects):
            raise TypeError("effects must contain Plan1CompiledEffect values")
        if (
            isinstance(self.remaining_uses, bool)
            or not isinstance(self.remaining_uses, int)
            or self.remaining_uses < 0
        ):
            raise ValueError("remaining_uses must be a non-negative integer")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1Blocker) for value in blockers):
            raise TypeError("blockers must contain Plan1Blocker values")
        object.__setattr__(self, "effects", effects)
        object.__setattr__(self, "blockers", blockers)

    @property
    def executable(self) -> bool:
        return not validate_fktn_ssr_plan1_equipped_item_runtime(self)

    @property
    def contract_identity(self) -> tuple[object, ...]:
        return (
            self.item_id,
            self.item_effect_id,
            self.status_enchant_id,
            self.trigger_id,
            self.phase_type,
            self.target_card_id,
            self.effects,
        )

    def consume(self) -> "Plan1EquippedItemRuntime":
        if self.remaining_uses < 1:
            raise ValueError("equipped item has no remaining use")
        return replace(self, remaining_uses=self.remaining_uses - 1)


class Plan1CardGate(StrEnum):
    ALWAYS = "always"
    PARAMETER_BUFF_ACTIVE = "parameter-buff-active"
    LESSON_BUFF_ACTIVE = "lesson-buff-active"


@dataclass(frozen=True, slots=True)
class Plan1PaymentModifier:
    """One instance-owned payment change compiled from a CardCustomize row.

    ``Plan1CompiledCard.cost_value`` remains the normalized value from the
    card Master row.  Runtime customization is an instance property, so the
    source grow and its reduction are retained separately instead of being
    inferred from a card id or folded into the static card catalog.  The
    payment executor applies all matching reductions in their Master order.
    """

    customize_id: str
    grow_effect_id: str
    cost_type: str
    reduction: int

    def __post_init__(self) -> None:
        for name in ("customize_id", "grow_effect_id", "cost_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        if (
            isinstance(self.reduction, bool)
            or not isinstance(self.reduction, int)
            or self.reduction <= 0
        ):
            raise ValueError("reduction must be a positive integer")

    @property
    def value(self) -> int:
        """Compatibility alias for callers that use Master ``value``."""

        return self.reduction

    @property
    def amount(self) -> int:
        return self.reduction

    @property
    def source_id(self) -> str:
        return self.grow_effect_id

    @property
    def grow_effect_type(self) -> str:
        """Master grow family represented by this payment modifier."""

        if self.cost_type == COST_STAMINA:
            return "ProduceCardGrowEffectType_CostReduce"
        if self.cost_type == COST_PARAMETER_BUFF:
            return "ProduceCardGrowEffectType_CostParameterBuffReduce"
        if self.cost_type == COST_LESSON_BUFF:
            return "ProduceCardGrowEffectType_CostLessonBuffReduce"
        return ""


@dataclass(frozen=True, slots=True)
class Plan1EffectModifier:
    """Typed placeholder for an instance-owned effect customization.

    The first Plan 1 customization slice only admits payment reducers.  The
    type is nevertheless public so a future effect compiler can extend the
    same card-instance contract without changing the bridge API.  Unsupported
    effect rows are represented by a Plan1 blocker and never emitted as a
    partially interpreted modifier.
    """

    customize_id: str
    grow_effect_id: str
    effect_type: str
    value: int

    def __post_init__(self) -> None:
        for name in ("customize_id", "grow_effect_id", "effect_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int)
            or self.value < 0
        ):
            raise ValueError("value must be a non-negative integer")


# Descriptive aliases for callers that include the card scope in their type
# names.  The compact names remain the canonical dataclass names.
Plan1CardPaymentModifier = Plan1PaymentModifier
Plan1CardEffectModifier = Plan1EffectModifier


@dataclass(frozen=True, slots=True)
class Plan1CompiledCard:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina_cost: int
    gate: Plan1CardGate
    play_trigger_id: str
    move_position_type: str
    effects: tuple[Plan1CompiledEffect, ...]
    blockers: tuple[Plan1Blocker, ...] = ()
    # ``ParameterBuffUp`` trigger values are lower bounds (the native trigger
    # text for the FKTN character card says "2 turns or more").  Keeping this
    # at the end preserves the old positional constructor shape.
    parameter_buff_min_turns: int = 1
    # ``LessonBuffUp`` card-admission gates use the same pre-payment scalar;
    # this field is appended for positional-constructor compatibility.
    lesson_buff_min: int = 0
    # Master cost fields are retained separately from the stamina scalar so
    # the closed buff-cost payment branch can consume the matching status
    # resource without changing existing positional fixtures.
    cost_type: str = COST_STAMINA
    cost_value: int = 0
    force_stamina_cost: int = 0
    # Runtime CardCustomize lineage is appended to preserve the historical
    # positional constructor used by the static catalog/tests.
    customize_ids: tuple[str, ...] = ()
    customize_count_list: tuple[int, ...] = ()
    payment_modifiers: tuple[Plan1PaymentModifier, ...] = ()
    effect_modifiers: tuple[Plan1EffectModifier, ...] = ()
    effect_group_ids: tuple[str, ...] = ()
    effect_group_ids_known: bool = False
    # Optional binding for programs compiled from an observed card instance.
    # The Master card ID remains unchanged; equal IDs can have distinct grows.
    instance_guid: str | None = None

    def __post_init__(self) -> None:
        if self.instance_guid is not None and (not isinstance(self.instance_guid, str) or not self.instance_guid):
            raise ValueError("compiled card instance_guid must be non-empty text or None")
        customize_ids = tuple(self.customize_ids)
        if any(not isinstance(value, str) or not value for value in customize_ids):
            raise ValueError("customize_ids must contain non-empty text")
        if len(set(customize_ids)) != len(customize_ids):
            raise ValueError("customize_ids must not contain duplicates")
        counts = tuple(self.customize_count_list)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise ValueError("customize_count_list must contain non-negative integers")
        if counts and len(counts) != len(customize_ids):
            raise ValueError(
                "customize_count_list must align with customize_ids"
            )
        payment_modifiers = tuple(self.payment_modifiers)
        if any(
            not isinstance(value, Plan1PaymentModifier)
            for value in payment_modifiers
        ):
            raise TypeError(
                "payment_modifiers must contain Plan1PaymentModifier values"
            )
        effect_modifiers = tuple(self.effect_modifiers)
        if any(
            not isinstance(value, Plan1EffectModifier)
            for value in effect_modifiers
        ):
            raise TypeError(
                "effect_modifiers must contain Plan1EffectModifier values"
            )
        object.__setattr__(self, "customize_ids", customize_ids)
        object.__setattr__(self, "customize_count_list", counts)
        object.__setattr__(self, "payment_modifiers", payment_modifiers)
        object.__setattr__(self, "effect_modifiers", effect_modifiers)
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(value, str) or not value for value in groups):
            raise ValueError("effect_group_ids must contain non-empty text")
        if len(groups) != len(set(groups)):
            raise ValueError("effect_group_ids must not contain duplicates")
        if not isinstance(self.effect_group_ids_known, bool):
            raise TypeError("effect_group_ids_known must be bool")
        object.__setattr__(self, "effect_group_ids", groups)

    @property
    def force_stamina(self) -> int:
        """Raw Master ``forceStamina`` alias retained for catalog callers."""

        return self.force_stamina_cost

    @property
    def customize_counts(self) -> tuple[int, ...]:
        """Alias matching the short spelling used by captured card DTOs."""

        return self.customize_count_list

    @property
    def payment_cost_reduction(self) -> int:
        """Total reduction for this card's normalized cost family."""

        return sum(value.reduction for value in self.payment_modifiers)

    @property
    def effective_cost_value(self) -> int:
        """Cost after instance reducers, before global status multipliers."""

        base = (
            self.stamina_cost
            if self.cost_type == COST_STAMINA
            else self.cost_value
        )
        return max(base - self.payment_cost_reduction, 0)

    @property
    def executable(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class Plan1DeckCompilation:
    manifest: Plan1InitialDeckManifest
    programs: tuple[Plan1CompiledCard, ...]
    equipped_item_runtime: Plan1EquippedItemRuntime | None = None

    def __post_init__(self) -> None:
        if len(self.programs) != len(self.manifest.entries):
            raise ValueError("one card program is required for every manifest entry")
        for entry, program in zip(self.manifest.entries, self.programs, strict=True):
            if (entry.card_id, entry.upgrade) != (program.card_id, program.upgrade):
                raise ValueError("program order must match the manifest")
        if self.equipped_item_runtime is not None and not isinstance(
            self.equipped_item_runtime, Plan1EquippedItemRuntime
        ):
            raise TypeError(
                "equipped_item_runtime must be Plan1EquippedItemRuntime or None"
            )

    @property
    def executable_instance_count(self) -> int:
        return sum(program.executable for program in self.programs)

    @property
    def blocked_instance_count(self) -> int:
        return len(self.programs) - self.executable_instance_count

    @property
    def unique_programs(self) -> tuple[Plan1CompiledCard, ...]:
        seen: set[tuple[str, int]] = set()
        result: list[Plan1CompiledCard] = []
        for program in self.programs:
            key = (program.card_id, program.upgrade)
            if key not in seen:
                seen.add(key)
                result.append(program)
        return tuple(result)

    @property
    def executable_unique_count(self) -> int:
        return sum(program.executable for program in self.unique_programs)

    @property
    def blocked_unique_count(self) -> int:
        return len(self.unique_programs) - self.executable_unique_count


def _plain_i32(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not minimum <= value <= INT32_MAX:
        raise ValueError(f"{label} must be in [{minimum}, {INT32_MAX}]")
    return value


@dataclass(frozen=True, slots=True)
class Plan1LessonBuffAdditiveStatus:
    """One finite/unlimited native LessonBuff additive status instance."""

    uid: int
    value_permille: int
    turns: int
    passing_turn_start: bool

    def __post_init__(self) -> None:
        _plain_i32(self.uid, "lesson_buff_additive.uid", minimum=1)
        _plain_i32(
            self.value_permille,
            "lesson_buff_additive.value_permille",
            minimum=1,
        )
        if (
            isinstance(self.turns, bool)
            or not isinstance(self.turns, int)
            or (self.turns != -1 and self.turns < 1)
        ):
            raise ValueError(
                "lesson_buff_additive.turns must be -1 or positive"
            )
        if not isinstance(self.passing_turn_start, bool):
            raise TypeError(
                "lesson_buff_additive.passing_turn_start must be bool"
            )


@dataclass(frozen=True, slots=True)
class Plan1PlayCountIntervalListenerStatus:
    """Minimal persisted progress for one captured item interval listener."""

    uid: int
    source_id: str
    rule_id: str
    phase_count: int
    remaining_uses: int
    turn_count: int

    def __post_init__(self) -> None:
        _plain_i32(self.uid, "interval_listener.uid", minimum=1)
        for name in ("source_id", "rule_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"interval_listener.{name} must be text")
        _plain_i32(self.phase_count, "interval_listener.phase_count")
        if (
            isinstance(self.remaining_uses, bool)
            or not isinstance(self.remaining_uses, int)
            or self.remaining_uses < -1
        ):
            raise ValueError(
                "interval_listener.remaining_uses must be -1 or non-negative"
            )
        _plain_i32(self.turn_count, "interval_listener.turn_count")


@dataclass(frozen=True, slots=True)
class Plan1ScalarState:
    """Closed-world scalar state for one direct card transaction.

    ``parameter_buff_turns`` is Master ``ExamParameterBuff`` (好調), while
    ``lesson_buff`` is Master ``ExamLessonBuff`` (集中).  The optional score
    context at the end of this DTO carries the native audition battle layer.
    Keeping that context on the immutable scalar makes every score hit use the
    same state-before schedule/bonus projection without changing card or turn
    dispatch.
    """

    score: int
    stamina: int
    max_stamina: int
    block: int
    parameter_buff_turns: int
    lesson_buff: int
    turn: int
    plays_remaining: int
    play_count: int
    limit_border: int = -1
    # Native ParameterBuffMultiplePerTurnStatusEffect keeps its own turn
    # counter, separate from the ordinary ParameterBuff status.  The field is
    # optional so existing scalar fixtures remain source-compatible.
    parameter_buff_multiple_per_turn_turns: int = 0
    parameter_buff_multiple_per_turn_fresh: bool = False
    # ``ParameterBuffStatusEffect`` follows the same first-boundary lifetime
    # rule as the native status object: a status installed during the current
    # turn is marked as passing the next TurnStart and must not be spent at
    # the immediately following EndTurn.  Keeping this bit beside the scalar
    # counter prevents a one-step executor from silently shortening a freshly
    # applied N.I.A. good-condition status.
    parameter_buff_fresh: bool = False
    # Native StaminaConsumptionDownStatusEffect is an additive duration.  -1
    # is the permanent sentinel; ``fresh`` preserves a newly installed layer
    # across its first end-turn SpendTurn boundary.
    stamina_consumption_down_turns: int = 0
    stamina_consumption_down_fresh: bool = False
    # Native StaminaConsumptionAddStatusEffect is one same-type status.  A
    # finite re-install uses AddTurn on that status; a permanent status is
    # not extended.  ``fresh`` preserves the first-boundary exemption.
    stamina_consumption_add_turns: int = 0
    stamina_consumption_add_fresh: bool = False
    stamina_consumption_down_fixed: int = 0
    # Exact scalar supplied by GetLessonBuffMultiple.  Current plain Plan1
    # checkpoints use 1000 (=1.0); keeping it explicit avoids assuming that
    # support/passive multiplier layers can never be present.
    lesson_buff_multiple_permille: int = 1000
    # LessonBuffAdditive is a separate multiplier used while acquiring 集中;
    # it is not GetLessonBuffMultiple and must not scale the score term twice.
    lesson_buff_gain_multiple_permille: int = 1000
    # ExamLessonValueMultiple installs ordered LessonParameterMultiple status
    # instances.  Keep this separate from LessonBuffAdditive: the former
    # scales lesson score, while the latter scales the 集中 contribution/gain.
    lesson_parameter_multiple_statuses: tuple[
        LessonParameterMultipleStatus, ...
    ] = ()
    lesson_buff_additive_statuses: tuple[
        Plan1LessonBuffAdditiveStatus, ...
    ] = ()
    next_status_uid: int = 1
    play_count_interval_listeners: tuple[
        Plan1PlayCountIntervalListenerStatus, ...
    ] = ()
    # Android AntiDebuffStatusEffect is one merged permanent status whose
    # count is a pool of preventive charges.  Installing the direct effect
    # adds effectCount; it does not enumerate or cleanse existing debuffs.
    anti_debuff_count: int = 0
    # DrawEffectExecutor adds the *actual* returned DrawCard count, not the
    # requested value.  Ordinary turn draws do not touch this native counter.
    total_effect_draw_card_count: int = 0
    # ConsumeBuffCost samples these two native status predicates before the
    # final binary32 ceil.  They default to false because the plain Plan1
    # scalar fixture has no buff-consumption modifier layer.
    buff_consumption_down: bool = False
    buff_consumption_add: bool = False
    # Audition score application context.  These defaults preserve the
    # historical non-battle Plan 1 scalar fixtures; a runtime projection sets
    # them from state-before's schedule and battle bonus permils.
    is_battle: bool = False
    current_parameter_type: ProduceParameterType = ProduceParameterType.UNKNOWN
    battle_bonus_permille_vocal: int = 1000
    battle_bonus_permille_dance: int = 1000
    battle_bonus_permille_visual: int = 1000
    clear_border: int = -1
    current_turn_total_add_parameter: int = 0
    judge_parameter_vocal: int = 0
    judge_parameter_dance: int = 0
    judge_parameter_visual: int = 0
    search_play_card_stamina_runtime: SearchPlayCardStaminaRuntime = field(default_factory=SearchPlayCardStaminaRuntime)

    def __post_init__(self) -> None:
        if not isinstance(self.search_play_card_stamina_runtime, SearchPlayCardStaminaRuntime):
            raise TypeError("search_play_card_stamina_runtime must be typed runtime")
        for name in (
            "score",
            "stamina",
            "max_stamina",
            "block",
            "parameter_buff_turns",
            "parameter_buff_multiple_per_turn_turns",
            "lesson_buff",
            "plays_remaining",
            "play_count",
            "stamina_consumption_down_fixed",
            "lesson_buff_multiple_permille",
            "lesson_buff_gain_multiple_permille",
            "anti_debuff_count",
            "total_effect_draw_card_count",
        ):
            _plain_i32(getattr(self, name), name)
        if (
            isinstance(self.stamina_consumption_down_turns, bool)
            or not isinstance(self.stamina_consumption_down_turns, int)
            or self.stamina_consumption_down_turns < -1
        ):
            raise ValueError(
                "stamina_consumption_down_turns must be -1 or non-negative"
            )
        if not isinstance(self.stamina_consumption_down_fresh, bool):
            raise TypeError("stamina_consumption_down_fresh must be bool")
        if not isinstance(self.parameter_buff_fresh, bool):
            raise TypeError("parameter_buff_fresh must be bool")
        if not isinstance(self.parameter_buff_multiple_per_turn_fresh, bool):
            raise TypeError(
                "parameter_buff_multiple_per_turn_fresh must be bool"
            )
        if (
            isinstance(self.stamina_consumption_add_turns, bool)
            or not isinstance(self.stamina_consumption_add_turns, int)
            or self.stamina_consumption_add_turns < -1
        ):
            raise ValueError(
                "stamina_consumption_add_turns must be -1 or non-negative"
            )
        if not isinstance(self.stamina_consumption_add_fresh, bool):
            raise TypeError("stamina_consumption_add_fresh must be bool")
        if not isinstance(self.buff_consumption_down, bool):
            raise TypeError("buff_consumption_down must be bool")
        if not isinstance(self.buff_consumption_add, bool):
            raise TypeError("buff_consumption_add must be bool")
        lesson_parameter = tuple(self.lesson_parameter_multiple_statuses)
        if any(
            not isinstance(value, LessonParameterMultipleStatus)
            for value in lesson_parameter
        ):
            raise TypeError(
                "lesson_parameter_multiple_statuses must contain typed statuses"
            )
        lesson_parameter_uids = tuple(
            value.uid for value in lesson_parameter if value.uid is not None
        )
        if len(lesson_parameter_uids) != len(set(lesson_parameter_uids)):
            raise ValueError(
                "lesson_parameter_multiple status UIDs must be unique"
            )
        object.__setattr__(
            self,
            "lesson_parameter_multiple_statuses",
            lesson_parameter,
        )
        additive = tuple(self.lesson_buff_additive_statuses)
        if any(
            not isinstance(value, Plan1LessonBuffAdditiveStatus)
            for value in additive
        ):
            raise TypeError(
                "lesson_buff_additive_statuses must contain typed statuses"
            )
        additive_uids = tuple(value.uid for value in additive)
        if len(additive_uids) != len(set(additive_uids)):
            raise ValueError("lesson_buff_additive status UIDs must be unique")
        all_status_uids = (*lesson_parameter_uids, *additive_uids)
        if len(all_status_uids) != len(set(all_status_uids)):
            raise ValueError("Plan1 active status UIDs must be unique")
        if sum(value.value_permille for value in additive) > (
            self.lesson_buff_gain_multiple_permille
        ):
            raise ValueError(
                "lesson_buff additive aggregate exceeds multiplier"
            )
        object.__setattr__(
            self,
            "lesson_buff_additive_statuses",
            additive,
        )
        _plain_i32(self.next_status_uid, "next_status_uid", minimum=1)
        if all_status_uids and self.next_status_uid <= max(all_status_uids):
            raise ValueError("next_status_uid must follow active status UIDs")
        interval_listeners = tuple(self.play_count_interval_listeners)
        if any(
            not isinstance(value, Plan1PlayCountIntervalListenerStatus)
            for value in interval_listeners
        ):
            raise TypeError(
                "play_count_interval_listeners must contain typed statuses"
            )
        interval_uids = tuple(value.uid for value in interval_listeners)
        if len(interval_uids) != len(set(interval_uids)):
            raise ValueError("interval listener UIDs must be unique")
        object.__setattr__(
            self,
            "play_count_interval_listeners",
            interval_listeners,
        )
        if not isinstance(self.is_battle, bool):
            raise TypeError("is_battle must be bool")
        if isinstance(self.current_parameter_type, bool):
            raise TypeError("current_parameter_type must be a ProduceParameterType")
        try:
            parameter_type = ProduceParameterType(self.current_parameter_type)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "current_parameter_type must be a ProduceParameterType"
            ) from error
        if self.is_battle and parameter_type is ProduceParameterType.UNKNOWN:
            raise ValueError(
                "battle scalar requires a Vocal/Dance/Visual parameter type"
            )
        object.__setattr__(self, "current_parameter_type", parameter_type)
        for name in (
            "battle_bonus_permille_vocal",
            "battle_bonus_permille_dance",
            "battle_bonus_permille_visual",
            "judge_parameter_vocal",
            "judge_parameter_dance",
            "judge_parameter_visual",
        ):
            _plain_i32(getattr(self, name), name)
        if (
            isinstance(self.clear_border, bool)
            or not isinstance(self.clear_border, int)
            or not -1 <= self.clear_border <= INT32_MAX
        ):
            raise ValueError(
                "clear_border must be -1 or a non-negative Int32"
            )
        _plain_i32(
            self.current_turn_total_add_parameter,
            "current_turn_total_add_parameter",
        )
        _plain_i32(self.turn, "turn", minimum=1)
        if self.stamina > self.max_stamina:
            raise ValueError("stamina must not exceed max_stamina")
        if (
            isinstance(self.limit_border, bool)
            or not isinstance(self.limit_border, int)
            or not -1 <= self.limit_border <= INT32_MAX
        ):
            raise ValueError("limit_border must be -1 or a non-negative Int32")
        if self.limit_border >= 0 and self.score > self.limit_border:
            raise ValueError("score must not exceed an enabled limit_border")


class Plan1TraceStage(StrEnum):
    COST = "cost"
    EFFECT_TRIGGER = "effect-trigger"
    EFFECT = "effect"
    MOVE_PLAY_CARD = "move-play-card"


@dataclass(frozen=True, slots=True)
class Plan1TraceEntry:
    ordinal: int
    stage: Plan1TraceStage
    source_id: str
    before: Plan1ScalarState
    after: Plan1ScalarState
    effect_type: str = ""
    effect_index: int | None = None
    hit_index: int | None = None
    requested: int = 0
    actual: int = 0


@dataclass(frozen=True, slots=True)
class Plan1EffectExecution:
    before: Plan1ScalarState
    after: Plan1ScalarState
    trace: tuple[Plan1TraceEntry, ...]
    blockers: tuple[Plan1Blocker, ...] = ()

    @property
    def applied(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class Plan1CardDrawResolution:
    """Scalar half of one externally staged native CardDraw command.

    Ordered zones live in :mod:`plan1_native_stage`; the core only accepts a
    resolver result that increments ``total_effect_draw_card_count`` by the
    actual draw count and changes no other scalar.  A blocker is an atomic
    no-op and lets the stage discard its immutable zone preview.
    """

    before: Plan1ScalarState
    after: Plan1ScalarState
    requested_count: int
    actual_count: int
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        for name in ("requested_count", "actual_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.actual_count > self.requested_count:
            raise ValueError("actual_count must not exceed requested_count")
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.blockers:
            if self.after is not self.before:
                raise ValueError("blocked CardDraw must preserve scalar identity")
            return
        expected_count = self.before.total_effect_draw_card_count + self.actual_count
        if expected_count > INT32_MAX:
            raise ValueError("total_effect_draw_card_count is outside Int32")
        if self.after != replace(
            self.before,
            total_effect_draw_card_count=expected_count,
        ):
            raise ValueError("CardDraw may only add its actual count")


Plan1CardDrawResolver = Callable[
    [Plan1ScalarState, Plan1CompiledEffect, int],
    Plan1CardDrawResolution,
]


@dataclass(frozen=True, slots=True)
class Plan1HandGraveDrawResolution:
    """Scalar half of the stage-owned Hand snapshot/grave/draw command."""

    before: Plan1ScalarState
    after: Plan1ScalarState
    requested_count: int
    actual_count: int
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        for name in ("requested_count", "actual_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.actual_count > self.requested_count:
            raise ValueError("actual_count must not exceed requested_count")
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.blockers:
            if self.after is not self.before:
                raise ValueError(
                    "blocked HandGraveCountCardDraw must preserve scalar identity"
                )
            return
        expected_count = self.before.total_effect_draw_card_count + self.actual_count
        if expected_count > INT32_MAX:
            raise ValueError("total_effect_draw_card_count is outside Int32")
        if self.after != replace(
            self.before,
            total_effect_draw_card_count=expected_count,
        ):
            raise ValueError(
                "HandGraveCountCardDraw may only add its actual draw count"
            )


Plan1HandGraveDrawResolver = Callable[
    [Plan1ScalarState, Plan1CompiledEffect, int],
    Plan1HandGraveDrawResolution,
]


@dataclass(frozen=True, slots=True)
class Plan1CardMoveResolution:
    """Scalar-neutral acknowledgement of one native CardMove command."""

    before: Plan1ScalarState
    after: Plan1ScalarState
    candidate_count: int
    moved_count: int
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        for name in ("candidate_count", "moved_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.moved_count > self.candidate_count:
            raise ValueError("moved_count must not exceed candidate_count")
        if self.after is not self.before:
            raise ValueError("CardMove must preserve scalar identity")
        object.__setattr__(self, "blockers", tuple(self.blockers))


Plan1CardMoveResolver = Callable[
    [Plan1ScalarState, Plan1CompiledEffect, int],
    Plan1CardMoveResolution,
]


@dataclass(frozen=True, slots=True)
class Plan1CardUpgradeResolution:
    """Scalar-neutral acknowledgement of one ordered CardUpgrade command."""

    before: Plan1ScalarState
    after: Plan1ScalarState
    candidate_count: int
    upgraded_count: int
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        for name in ("candidate_count", "upgraded_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.upgraded_count > self.candidate_count:
            raise ValueError("upgraded_count must not exceed candidate_count")
        if self.after is not self.before:
            raise ValueError("CardUpgrade must preserve scalar identity")
        object.__setattr__(self, "blockers", tuple(self.blockers))


Plan1CardUpgradeResolver = Callable[
    [Plan1ScalarState, Plan1CompiledEffect, int],
    Plan1CardUpgradeResolution,
]


@dataclass(frozen=True, slots=True)
class Plan1CardCreateResolution:
    """Scalar half of one externally staged native CardCreateId command.

    Card creation mutates ordered zones and the exam RNG, never this scalar
    state.  The stage resolver must resolve the full fixed count or return a
    typed blocker; partial creation is not a native result.
    """

    before: Plan1ScalarState
    after: Plan1ScalarState
    requested_count: int
    actual_count: int
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        for name in ("requested_count", "actual_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.actual_count > self.requested_count:
            raise ValueError("actual_count must not exceed requested_count")
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.after is not self.before:
            raise ValueError("CardCreateId must preserve scalar identity")
        if not self.blockers and self.actual_count != self.requested_count:
            raise ValueError("resolved CardCreateId must create the fixed count")


Plan1CardCreateResolver = Callable[
    [Plan1ScalarState, Plan1CompiledEffect, int],
    Plan1CardCreateResolution,
]


@dataclass(frozen=True, slots=True)
class Plan1EffectTimerResolution:
    """Scalar-neutral acknowledgement of one stage-owned Timer install."""

    before: Plan1ScalarState
    after: Plan1ScalarState
    delay_turns: int
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.delay_turns, bool)
            or not isinstance(self.delay_turns, int)
            or self.delay_turns < 1
        ):
            raise ValueError("delay_turns must be a positive integer")
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.after is not self.before:
            raise ValueError("EffectTimer installation must preserve scalar identity")


Plan1EffectTimerResolver = Callable[
    [Plan1ScalarState, Plan1CompiledEffect, int],
    Plan1EffectTimerResolution,
]


class Plan1TransitionStatus(StrEnum):
    APPLIED = "applied"
    INELIGIBLE = "ineligible"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class Plan1CardTransition:
    card: Plan1CompiledCard
    before: Plan1ScalarState
    after: Plan1ScalarState
    trace: tuple[Plan1TraceEntry, ...]
    status: Plan1TransitionStatus
    blockers: tuple[Plan1Blocker, ...] = ()
    ineligible_reason: str = ""

    @property
    def applied(self) -> bool:
        return self.status is Plan1TransitionStatus.APPLIED


def _decode_array(raw: str, label: str) -> tuple[Any, ...]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise Plan1NativeContractError(f"invalid JSON array: {label}") from error
    if not isinstance(value, list):
        raise Plan1NativeContractError(f"expected JSON array: {label}")
    return tuple(value)


def _decode_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise Plan1NativeContractError(f"invalid JSON object: {label}") from error
    if not isinstance(value, dict):
        raise Plan1NativeContractError(f"expected JSON object: {label}")
    return value


def _deck_entries(row: sqlite3.Row, label: str) -> tuple[tuple[str, int], ...]:
    card_ids = _decode_array(str(row["card_ids_json"]), f"{label}:cards")
    upgrades = _decode_array(str(row["upgrade_counts_json"]), f"{label}:upgrades")
    if not all(isinstance(card_id, str) and card_id for card_id in card_ids):
        raise Plan1NativeContractError(f"invalid card identity in {label}")
    if upgrades and len(upgrades) != len(card_ids):
        raise Plan1NativeContractError(f"card/upgrade length mismatch in {label}")
    if not upgrades:
        upgrades = (0,) * len(card_ids)
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in upgrades
    ):
        raise Plan1NativeContractError(f"invalid upgrade in {label}")
    return tuple(zip(card_ids, upgrades, strict=True))


def _require_exact(value: object, expected: object, label: str) -> None:
    if value != expected:
        raise Plan1NativeContractError(
            f"{label} changed: expected {expected!r}, got {value!r}"
        )


def load_fktn_ssr_plan1_manifest(
    *, database: Path = DEFAULT_DATABASE
) -> Plan1InitialDeckManifest:
    """Load the pinned produce-001 + FKTN SSR base-potential manifest.

    ``ProduceInitialDeck`` selects the eight-card mode/archetype deck.  Native
    bootstrap then appends the IdolCard's two-card ``examInitialDeckId``
    character deck and its exclusive ``produceCardId``, in that order.
    """

    database = Path(database)
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        idol = connection.execute(
            "SELECT * FROM idol_card WHERE id = ?",
            (FKTN_SSR_PLAN1_IDOL_CARD_ID,),
        ).fetchone()
        if idol is None:
            raise Plan1NativeContractError("FKTN SSR IdolCard is missing")
        _require_exact(idol["plan_type"], PLAN1, "IdolCard.planType")
        _require_exact(idol["exam_effect_type"], PLAN1_ARCHETYPE, "IdolCard.examEffectType")
        _require_exact(idol["produce_card_id"], FKTN_SSR_PLAN1_UNIQUE_CARD_ID, "IdolCard.produceCardId")
        _require_exact(idol["exam_initial_deck_id"], FKTN_SSR_PLAN1_IDOL_FRAGMENT_ID, "IdolCard.examInitialDeckId")
        idol_raw = _decode_object(str(idol["raw_json"]), str(idol["id"]))
        _require_exact(idol_raw.get("beforeProduceItemId"), FKTN_SSR_PLAN1_BEFORE_ITEM_ID, "IdolCard.beforeProduceItemId")

        mode = connection.execute(
            """
            SELECT d.*
              FROM initial_deck_map AS m
              JOIN initial_deck AS d ON d.id = m.exam_initial_deck_id
             WHERE m.produce_id = ? AND m.exam_effect_type = ?
            """,
            (INITIAL_REGULAR_PRODUCE_ID, PLAN1_ARCHETYPE),
        ).fetchone()
        if mode is None:
            raise Plan1NativeContractError("produce-001 Plan1 deck is missing")
        _require_exact(mode["id"], FKTN_SSR_PLAN1_MODE_DECK_ID, "ProduceInitialDeck.examInitialDeckId")
        mode_rows = _deck_entries(mode, str(mode["id"]))
        _require_exact(
            mode_rows,
            FKTN_SSR_PLAN1_MODE_CARD_REFS,
            "produce-001 Plan1 default deck",
        )

        entries: list[Plan1DeckEntry] = []
        for card_id, upgrade in mode_rows:
            card = connection.execute(
                "SELECT plan_type FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
            if card is None:
                raise Plan1NativeContractError(f"initial card row missing: {card_id}@{upgrade}")
            if card["plan_type"] == PLAN_COMMON:
                role = Plan1DeckRole.COMMON
            elif card["plan_type"] == PLAN1:
                role = Plan1DeckRole.PLAN1_STARTER
            else:
                raise Plan1NativeContractError(f"unexpected initial card plan: {card_id}:{card['plan_type']}")
            entries.append(Plan1DeckEntry(card_id, upgrade, role))

        fragment = connection.execute(
            "SELECT * FROM initial_deck WHERE id = ?",
            (FKTN_SSR_PLAN1_IDOL_FRAGMENT_ID,),
        ).fetchone()
        if fragment is None:
            raise Plan1NativeContractError("IdolCard legacy deck fragment is missing")
        fragment_rows = _deck_entries(fragment, str(fragment["id"]))
        _require_exact(
            fragment_rows,
            FKTN_SSR_PLAN1_CHARACTER_CARD_REFS,
            "FKTN SSR Plan1 character deck",
        )
        fragment_entries = tuple(
            Plan1DeckEntry(card_id, upgrade, Plan1DeckRole.CHARACTER)
            for card_id, upgrade in fragment_rows
        )
        for entry in fragment_entries:
            card = connection.execute(
                "SELECT plan_type FROM card WHERE id = ? AND upgrade_count = ?",
                (entry.card_id, entry.upgrade),
            ).fetchone()
            if card is None or card["plan_type"] != PLAN1:
                raise Plan1NativeContractError(
                    "character initial card is missing or not Plan1: "
                    f"{entry.card_id}@{entry.upgrade}"
                )
        entries.extend(fragment_entries)

        unique = connection.execute(
            "SELECT plan_type FROM card WHERE id = ? AND upgrade_count = 0",
            (FKTN_SSR_PLAN1_UNIQUE_CARD_ID,),
        ).fetchone()
        if unique is None or unique["plan_type"] != PLAN1:
            raise Plan1NativeContractError("idol-exclusive base card is missing or not Plan1")
        entries.append(
            Plan1DeckEntry(
                FKTN_SSR_PLAN1_UNIQUE_CARD_ID,
                0,
                Plan1DeckRole.IDOL_EXCLUSIVE,
            )
        )

        item = connection.execute(
            "SELECT * FROM produce_item WHERE id = ?",
            (FKTN_SSR_PLAN1_BEFORE_ITEM_ID,),
        ).fetchone()
        if item is None:
            raise Plan1NativeContractError("mandatory beforeProduce item is missing")
        _require_exact(item["plan_type"], PLAN1, "ProduceItem.planType")
        _require_exact(item["is_exam_effect"], 1, "ProduceItem.isExamEffect")
        _require_exact(item["fire_limit"], 0, "ProduceItem.fireLimit")
        _require_exact(item["fire_interval"], 0, "ProduceItem.fireInterval")
        _require_exact(item["produce_trigger_id"], "", "ProduceItem.produceTriggerId")
        item_effect_ids = tuple(
            str(value)
            for value in _decode_array(
                str(item["produce_item_effect_ids_json"]),
                "ProduceItem.produceItemEffectIds",
            )
        )
        if not item_effect_ids:
            raise Plan1NativeContractError("mandatory item has no item effect")
        _require_exact(
            item_effect_ids,
            (FKTN_SSR_PLAN1_BEFORE_ITEM_EFFECT_ID,),
            "ProduceItem.produceItemEffectIds",
        )
        status_ids: list[str] = []
        trigger_ids: list[str] = []
        nested_effect_ids: list[str] = []
        for item_effect_id in item_effect_ids:
            item_effect = connection.execute(
                "SELECT * FROM produce_item_effect WHERE id = ?",
                (item_effect_id,),
            ).fetchone()
            if item_effect is None:
                raise Plan1NativeContractError(f"item effect missing: {item_effect_id}")
            _require_exact(item_effect["effect_type"], "ProduceItemEffectType_ExamStatusEnchant", f"{item_effect_id}.effectType")
            _require_exact(item_effect["effect_turn"], -1, f"{item_effect_id}.effectTurn")
            _require_exact(item_effect["effect_count"], 1, f"{item_effect_id}.effectCount")
            status_id = str(item_effect["produce_exam_status_enchant_id"])
            if not status_id:
                raise Plan1NativeContractError(f"item status enchant missing: {item_effect_id}")
            status = connection.execute(
                "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
                (status_id,),
            ).fetchone()
            if status is None:
                raise Plan1NativeContractError(f"status enchant missing: {status_id}")
            status_ids.append(status_id)
            trigger_ids.append(str(status["produce_exam_trigger_id"]))
            nested_effect_ids.extend(
                str(value)
                for value in _decode_array(
                    str(status["produce_exam_effect_ids_json"]),
                    f"{status_id}.produceExamEffectIds",
                )
            )
        _require_exact(
            tuple(status_ids),
            (FKTN_SSR_PLAN1_BEFORE_ITEM_STATUS_ID,),
            "ProduceItem.statusEnchantIds",
        )
        _require_exact(
            tuple(trigger_ids),
            (FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID,),
            "ProduceItem.triggerIds",
        )
        _require_exact(
            tuple(nested_effect_ids),
            FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS,
            "ProduceItem.nestedEffectIds",
        )
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID,),
        ).fetchone()
        if trigger is None:
            raise Plan1NativeContractError("mandatory item trigger is missing")
        _require_exact(
            _decode_array(
                str(trigger["phase_types_json"]),
                "mandatory item trigger phases",
            ),
            (PHASE_CARD_PLAY,),
            "ProduceExamTrigger.phaseTypes",
        )
        for column in (
            "phase_values_json",
            "field_status_check_types_json",
            "field_status_types_json",
            "field_status_values_json",
            "field_status_produce_card_search_ids_json",
            "effect_types_json",
        ):
            _require_exact(
                _decode_array(str(trigger[column]), column),
                (),
                f"ProduceExamTrigger.{column}",
            )
        _require_exact(
            trigger["produce_card_search_id"],
            FKTN_SSR_PLAN1_BEFORE_ITEM_SEARCH_ID,
            "ProduceExamTrigger.produceCardSearchId",
        )
        _require_exact(trigger["upper_search_count"], 0, "ProduceExamTrigger.upperSearchCount")
        _require_exact(trigger["lower_search_count"], 1, "ProduceExamTrigger.lowerSearchCount")
        _require_exact(
            trigger["card_move_position_type"],
            "ProduceCardMovePositionType_Unknown",
            "ProduceExamTrigger.cardMovePositionType",
        )
        _require_exact(
            trigger["lesson_type"],
            "ProduceStepLessonType_Unknown",
            "ProduceExamTrigger.lessonType",
        )
        search = connection.execute(
            "SELECT * FROM produce_card_search WHERE id = ?",
            (FKTN_SSR_PLAN1_BEFORE_ITEM_SEARCH_ID,),
        ).fetchone()
        if search is None:
            raise Plan1NativeContractError("mandatory item card search is missing")
        _require_exact(
            _decode_array(str(search["produce_card_ids_json"]), "item search cards"),
            (FKTN_SSR_PLAN1_UNIQUE_CARD_ID,),
            "ProduceCardSearch.produceCardIds",
        )
        _require_exact(
            _decode_array(str(search["upgrade_counts_json"]), "item search upgrades"),
            (),
            "ProduceCardSearch.upgradeCounts",
        )
        _require_exact(
            search["card_position_type"],
            "ProduceCardPositionType_Playing",
            "ProduceCardSearch.cardPositionType",
        )

    return Plan1InitialDeckManifest(
        idol_card_id=FKTN_SSR_PLAN1_IDOL_CARD_ID,
        produce_id=INITIAL_REGULAR_PRODUCE_ID,
        exam_effect_type=PLAN1_ARCHETYPE,
        mode_deck_id=FKTN_SSR_PLAN1_MODE_DECK_ID,
        entries=tuple(entries),
        idol_fragment_id=FKTN_SSR_PLAN1_IDOL_FRAGMENT_ID,
        idol_fragment_entries=fragment_entries,
        idol_fragment_in_effective_deck=True,
        before_produce_item=Plan1RequiredItem(
            item_id=FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
            item_effect_ids=item_effect_ids,
            status_enchant_ids=tuple(status_ids),
            trigger_ids=tuple(trigger_ids),
            nested_effect_ids=tuple(nested_effect_ids),
            execution_blocker=Plan1Blocker(
                "before-produce-item-runtime-required",
                FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
                "compile and bind the exact stage-local remaining-use runtime",
            ),
        ),
    )


@lru_cache(maxsize=8)
def load_plan1_native_settings(
    setting_id: str = DEFAULT_EXAM_SETTING_ID,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1NativeSettings:
    path = Path(master_dir) / "ExamSetting.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise Plan1NativeContractError("ExamSetting.yaml must contain an array")
    matches = [row for row in payload if isinstance(row, dict) and row.get("id") == setting_id]
    if len(matches) != 1:
        raise Plan1NativeContractError(f"ExamSetting must resolve exactly once: {setting_id}")
    row = matches[0]

    def number(name: str) -> int:
        value = row.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise Plan1NativeContractError(f"invalid ExamSetting field: {setting_id}:{name}")
        return value

    return Plan1NativeSettings(
        exam_setting_id=setting_id,
        adding_parameter=AddingParameterSettings(
            parameter_buff_permille=number("examParameterBuffPermil"),
            parameter_buff_multiple_per_turn_permille=number("examParameterBuffMultiplePerTurnPermil"),
            gimmick_parameter_debuff_permille=number("examGimmickParameterDebuffPermil"),
            lesson_depend_review_aggressive_permille=number("examLessonValueMultipleDependReviewOrAggressiveMultiplePermil"),
            lesson_depend_review_aggressive_max_permille=number("examLessonValueMultipleDependReviewOrAggressiveMaxPermil"),
            concentration_lesson_permille=(
                number("examConcentrationLessonValueMultiplePermil1"),
                number("examConcentrationLessonValueMultiplePermil2"),
            ),
            preservation_lesson_permille=(
                number("examPreservationLessonValueMultiplePermil1"),
                number("examPreservationLessonValueMultiplePermil2"),
            ),
            full_power_lesson_permille=number("examFullPowerLessonValueMultiplePermil"),
            over_preservation_lesson_permille=number("examOverPreservationLessonValueMultiplePermil"),
        ),
        add_block=AddBlockSettings(
            block_add_down_permille=number("examBlockAddDownPermil")
        ),
        stamina_payment=StaminaPaymentSettings(
            concentration_permille=(
                number("examConcentrationStaminaMultiplePermil1"),
                number("examConcentrationStaminaMultiplePermil2"),
            ),
            preservation_permille=(
                number("examPreservationStaminaMultiplePermil1"),
                number("examPreservationStaminaMultiplePermil2"),
            ),
            over_preservation_permille=number("examOverPreservationStaminaMultiplePermil"),
            consumption_down_permille=number("examStaminaConsumptionDownPermil"),
            consumption_down_add_permille=number("examStaminaConsumptionDownAddPermil"),
            consumption_add_permille=number("examStaminaConsumptionAddPermil"),
            consumption_add_down_permille=number("examStaminaConsumptionAddDownPermil"),
            reduce_change_value=number("examStaminaReduceChange"),
        ),
        hand_limit=number("handLimit"),
        buff_consumption_down_permille=number("examBuffConsumptionDownPermil"),
        buff_consumption_add_permille=number("examBuffConsumptionAddPermil"),
    )


def _effect_link_blockers(
    raw: dict[str, Any],
    effect_id: str,
    effect_type: str,
) -> list[Plan1Blocker]:
    blockers: list[Plan1Blocker] = []
    nested_fields = (
        "chainProduceExamEffectIds",
        "produceExamStatusEnchantId",
        "produceCardStatusEnchantId",
        "produceCardGrowEffectIds",
    )
    # The supported Timer contract permits one singular child link.  Every
    # other effect kind, including a Timer child, still requires it neutral.
    if effect_type != EFFECT_TIMER:
        nested_fields = ("chainProduceExamEffectId", *nested_fields)
    if effect_type == EFFECT_STATUS_ENCHANT:
        nested_fields = tuple(
            name for name in nested_fields
            if name != "produceExamStatusEnchantId"
        )
    for name in nested_fields:
        value = raw.get(name)
        if value not in (None, "", []):
            blockers.append(Plan1Blocker("effect-nested-shape-unsupported", effect_id, name))
    neutral_fields = {
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": EFFECT_UNKNOWN,
        "produceCardSearchId": "",
        "movePositionType": MOVE_UNKNOWN,
        "pickRangeType": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": "ProducePickCountType_Unknown",
        "pickCountMin2": 0,
        "pickCountMax2": 0,
    }
    if effect_type == EFFECT_CARD_CREATE_ID:
        # These five fields are the direct structural payload validated by
        # the shared native CardCreateId contract below.  Search/range fields
        # and every nested link remain required-neutral for this Plan1 slice.
        for name in (
            "targetProduceCardId",
            "targetUpgradeCount",
            "movePositionType",
            "pickCountMin",
            "pickCountMax",
        ):
            neutral_fields.pop(name)
    elif effect_type == EFFECT_CARD_MOVE:
        for name in (
            "produceCardSearchId",
            "movePositionType",
            "pickRangeType",
            "pickCountMin",
            "pickCountMax",
        ):
            neutral_fields.pop(name)
    elif effect_type == EFFECT_CARD_UPGRADE:
        for name in (
            "produceCardSearchId",
            "pickRangeType",
            "pickCountMin",
            "pickCountMax",
        ):
            neutral_fields.pop(name)
    if effect_type == EFFECT_SEARCH_STAMINA_CHANGE:
        neutral_fields.pop("produceCardSearchId")
        neutral_fields.pop("pickRangeType")
    for name, expected in neutral_fields.items():
        if raw.get(name) != expected:
            blockers.append(Plan1Blocker("effect-target-shape-unsupported", effect_id, name))
    return blockers


def _compile_effect_row(
    row: sqlite3.Row | None,
    effect_id: str,
    *,
    database: Path,
    connection: sqlite3.Connection | None = None,
    compile_stack: tuple[str, ...] = (),
) -> Plan1CompiledEffect:
    if row is None:
        return Plan1CompiledEffect(
            effect_id,
            EFFECT_UNKNOWN,
            0,
            0,
            0,
            0,
            (Plan1Blocker("master-effect-missing", effect_id),),
        )
    raw = _decode_object(str(row["raw_json"]), effect_id)
    effect_type = str(row["effect_type"])
    value1 = int(row["value1"])
    value2 = int(row["value2"])
    effect_count = int(row["effect_count"])
    effect_turn = int(row["effect_turn"])
    blockers = _effect_link_blockers(raw, effect_id, effect_type)
    card_create_contract: CardCreateContract | None = None
    hand_grave_draw_contract: HandGraveDrawContract | None = None
    card_move_contract: Plan2CardMoveContract | None = None
    card_move_search: ProduceCardSearchRule | None = None
    card_upgrade_contract: Plan3CardUpgradeContract | None = None
    timer_child: Plan1CompiledEffect | None = None
    status_enchant_id = ""
    search_stamina_contract = None
    if effect_type == EFFECT_CARD_CREATE_ID:
        resolved = try_resolve_card_create_contract(row)
        if isinstance(resolved, CardCreateUnresolvedInput):
            blockers.append(
                Plan1Blocker(
                    "effect-card-create-contract-unresolved",
                    effect_id,
                    f"{resolved.field}:{resolved.reason}",
                )
            )
        else:
            card_create_contract = resolved
    elif effect_type == EFFECT_HAND_GRAVE_COUNT_CARD_DRAW:
        try:
            hand_grave_draw_contract = HandGraveDrawContract.from_master_row(raw)
        except (HandGraveDrawContractError, TypeError, ValueError) as error:
            blockers.append(
                Plan1Blocker(
                    "effect-hand-grave-draw-contract-unresolved",
                    effect_id,
                    str(error) or type(error).__name__,
                )
            )
    elif effect_type == EFFECT_CARD_MOVE:
        try:
            card_move_contract = resolve_plan2_card_move_contract(
                row,
                plan_type=PLAN_COMMON,
            )
            card_move_search = load_produce_card_search(
                card_move_contract.search_id,
                database,
            )
            if card_move_contract.destination == "ProduceCardMovePositionType_Hand":
                # The shared native move owner also serves these two complete
                # drink shapes. Stage callers without that owner still fail
                # at their ordered-zone resolver, rather than invent a move.
                from .plan2_card_move import _assert_proven_contract
                from .card_search import validate_plan3_card_move_search
                _assert_proven_contract(card_move_contract)
                search = card_move_search
                allowed = ((card_move_contract.pick_range_type == "ProducePickRangeType_All"
                            and card_move_contract.count_range == (0, 0) and search.card_search_tag == "idol-unique" and search.limit_count == 1)
                           or (card_move_contract.pick_range_type == "ProducePickRangeType_Select"
                               and card_move_contract.count_range == (1, 1) and not search.card_search_tag and search.limit_count == 0))
                if (not allowed or search.card_position_type != "ProduceCardPositionType_DeckGrave"
                        or validate_plan3_card_move_search(replace(search, card_search_tag="")) is not None
                        or search.card_rarities or search.produce_card_ids or search.upgrade_counts or search.card_categories
                        or search.plan_type != "ProducePlanType_Unknown"):
                    raise ValueError("unintegrated-card-move-hand-search-shape")
            else:
                assert_plan2_card_move_lost_random_direct_contract(card_move_contract)
                assert_plan2_card_move_lost_random_direct_search(card_move_contract, card_move_search)
        except (
            CardMoveResolutionError,
            KeyError,
            OSError,
            sqlite3.Error,
            TypeError,
            ValueError,
        ) as error:
            blockers.append(
                Plan1Blocker(
                    "effect-card-move-contract-unresolved",
                    effect_id,
                    str(error) or type(error).__name__,
                )
            )
    elif effect_type == EFFECT_CARD_UPGRADE:
        resolution = resolve_plan3_card_upgrade(
            effect_id,
            database=Path(database),
        )
        if resolution.pause is not None:
            blockers.append(
                Plan1Blocker(
                    "effect-card-upgrade-contract-unresolved",
                    effect_id,
                    f"{resolution.pause.code}:{resolution.pause.detail}".rstrip(":"),
                )
            )
        else:
            contract = resolution.contract
            assert contract is not None
            search = contract.search
            hand_all = (
                contract.branch is Plan3CardUpgradeBranch.ALL
                and search.card_position_type
                == "ProduceCardPositionType_Hand"
                and not search.card_rarities
                and not search.produce_card_ids
                and not search.upgrade_counts
                and search.plan_type == "ProducePlanType_Unknown"
                and not search.card_categories
                and search.card_status_type
                == "ProduceCardSearchStatusType_Unknown"
                and search.order_type == "ProduceCardOrderType_Unknown"
                and not search.card_search_tag
                and not search.produce_card_random_pool_id
                and search.limit_count == 0
                and not search.effect_group_ids
                and not search.is_self
                and not search.produce_card_pool_id
                and search.cost_type == "ExamCostType_Unknown"
                and not search.is_customized
                and not contract.search_support_pause
            )
            if hand_all:
                card_upgrade_contract = contract
            else:
                blockers.append(
                    Plan1Blocker(
                        "effect-card-upgrade-contract-unresolved",
                        effect_id,
                        "hand-all-search-required",
                    )
                )
    elif effect_type == EFFECT_TIMER:
        child_id = raw.get("chainProduceExamEffectId")
        if not isinstance(child_id, str) or not child_id:
            blockers.append(
                Plan1Blocker("effect-timer-child-missing", effect_id)
            )
        elif child_id == effect_id or child_id in compile_stack:
            blockers.append(
                Plan1Blocker("effect-timer-child-nested", effect_id, child_id)
            )
        elif connection is None:
            blockers.append(
                Plan1Blocker(
                    "effect-timer-master-context-required",
                    effect_id,
                    child_id,
                )
            )
        else:
            child_row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (child_id,)
            ).fetchone()
            child_type = (
                EFFECT_UNKNOWN
                if child_row is None
                else str(child_row["effect_type"])
            )
            if child_type == EFFECT_TIMER:
                blockers.append(
                    Plan1Blocker(
                        "effect-timer-child-nested", effect_id, child_id
                    )
                )
            else:
                compiled_child = _compile_effect_row(
                    child_row,
                    child_id,
                    database=database,
                    connection=connection,
                    compile_stack=(*compile_stack, effect_id),
                )
                if child_type not in PLAN1_TIMER_CHILD_EFFECT_TYPES:
                    blockers.append(
                        Plan1Blocker(
                            "effect-timer-child-type-unsupported",
                            effect_id,
                            child_type,
                        )
                    )
                elif compiled_child.blockers:
                    blockers.append(
                        Plan1Blocker(
                            "effect-timer-child-blocked", effect_id, child_id
                        )
                    )
                    blockers.extend(compiled_child.blockers)
                else:
                    timer_child = compiled_child
    elif effect_type == EFFECT_SEARCH_STAMINA_CHANGE:
        try:
            search = load_produce_card_search(raw.get("produceCardSearchId", ""), database)
            resolved = resolve_plan2_search_play_card_stamina_consumption_change_from_rows(raw, search)
            if resolved.contract is None or resolved.gaps:
                blockers.extend(Plan1Blocker(gap.code, effect_id, gap.detail) for gap in resolved.gaps)
            else:
                search_stamina_contract = resolved.contract
        except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
            blockers.append(Plan1Blocker("effect-search-stamina-contract-unresolved", effect_id, str(error)))
    elif effect_type == EFFECT_STATUS_ENCHANT:
        raw_status_id = raw.get("produceExamStatusEnchantId")
        if not isinstance(raw_status_id, str) or not raw_status_id:
            blockers.append(
                Plan1Blocker(
                    "effect-status-enchant-id-missing",
                    effect_id,
                )
            )
        else:
            status_enchant_id = raw_status_id
    normalized = {
        "id": effect_id,
        "effectType": effect_type,
        "effectValue1": value1,
        "effectValue2": value2,
        "effectCount": effect_count,
        "effectTurn": effect_turn,
    }
    for name, expected in normalized.items():
        if raw.get(name) != expected:
            blockers.append(Plan1Blocker("master-effect-normalization-mismatch", effect_id, name))
    if effect_type not in _SUPPORTED_EFFECT_TYPES:
        blockers.append(Plan1Blocker("effect-type-unsupported", effect_id, effect_type))
    else:
        shape_valid = {
            EFFECT_LESSON: value1 >= 0 and value2 == 0 and effect_count > 0 and effect_turn == 0,
            EFFECT_BLOCK: value1 >= 0 and value2 == effect_count == effect_turn == 0,
            EFFECT_BLOCK_FIX: value1 >= 0 and value2 == effect_count == effect_turn == 0,
            EFFECT_PARAMETER_BUFF: value1 == value2 == effect_count == 0 and effect_turn > 0,
            EFFECT_LESSON_BUFF: value1 > 0 and value2 == effect_count == effect_turn == 0,
            # The shared type-39 installer/lifecycle also owns finite positive
            # durations. Equal-turn instances merge; different ones remain
            # ordered and additive. A card/drink ID does not change this rule.
            EFFECT_LESSON_VALUE_MULTIPLE: (
                value1 > 0
                and value2 == 0
                and effect_count == 0
                and (effect_turn == -1 or effect_turn > 0)
            ),
            EFFECT_PLAYABLE_VALUE_ADD: value1 == value2 == effect_turn == 0 and effect_count > 0,
            # Android DrawEffectExecutor stores value1 as the requested count;
            # DrawCard later clamps it by Deck+Grave availability and Hand
            # space.  The other scalar fields are unused on all Master rows.
            EFFECT_CARD_DRAW: (
                value1 > 0 and value2 == effect_count == effect_turn == 0
            ),
            EFFECT_CARD_MOVE: (
                card_move_contract is not None
                and value1 == value2 == effect_count == effect_turn == 0
            ),
            EFFECT_CARD_UPGRADE: card_upgrade_contract is not None,
            # The shared Android-native contract proves all CardCreateId
            # destinations/count rules.  This Plan1 stage slice deliberately
            # opens only fixed-count DeckRandom: it can be expressed by the
            # existing atomic ordered-zone hook without any search/runtime
            # inputs beyond caller-authoritative GUID tokens.
            EFFECT_CARD_CREATE_ID: (
                card_create_contract is not None
                and value1 == value2 == effect_count == effect_turn == 0
                and card_create_contract.destination == "deck_random"
                and card_create_contract.count_min
                == card_create_contract.count_max
                and card_create_contract.count_min > 0
                and card_create_contract.pick_count_type
                == "ProducePickCountType_Unknown"
                and not card_create_contract.search2_id
            ),
            EFFECT_HAND_GRAVE_COUNT_CARD_DRAW: (
                hand_grave_draw_contract is not None
                and value1 == value2 == effect_count == effect_turn == 0
            ),
            # Android EffectTimerEffectExecutor installs one ExamTurnTimer
            # with this relative delay and one ordered child.  The child is
            # not executed until the stage-owned later-turn boundary.
            EFFECT_TIMER: (
                value1 > 0
                and value2 == 0
                and effect_count == 1
                and effect_turn == 0
                and timer_child is not None
            ),
            # Android ExamStaminaRecoverFix calls AddStamina(value1) directly.
            # The authoritative max-stamina cap is applied by the status
            # collection; value2/count/turn are unused.
            EFFECT_STAMINA_RECOVER_FIX: (
                value1 > 0 and value2 == effect_count == effect_turn == 0
            ),
            EFFECT_STAMINA_REDUCE_FIX: (
                value1 >= 0 and value2 == effect_count == effect_turn == 0
            ),
            EFFECT_SEARCH_STAMINA_CHANGE: search_stamina_contract is not None,
            # Android StaminaRecoverMultipleEffectExecutor reads MaxStamina,
            # computes its value1 permille through an ordered binary32
            # multiply/divide/ceil path, then delegates to AddStaminaFix.
            # All four current Master rows are positive pure scalars.
            EFFECT_STAMINA_RECOVER_MULTIPLE: (
                value1 > 0 and value2 == effect_count == effect_turn == 0
            ),
            # Android AntiDebuffEffectExecutor installs/merges one permanent
            # AntiDebuffStatusEffect and adds effectCount to its charge pool.
            # The current Master rows carry counts 1/2/3/5 and no scalars.
            EFFECT_ANTI_DEBUFF: (
                value1 == value2 == effect_turn == 0 and effect_count > 0
            ),
            # Android TryAddStaminaConsumptionDownStatus adds this duration
            # to the active status.  -1 is its permanent lifetime sentinel.
            EFFECT_STAMINA_CONSUMPTION_DOWN: (
                value1 == value2 == effect_count == 0
                and (effect_turn == -1 or effect_turn > 0)
            ),
            # Android StaminaConsumptionAddEffectExecutor stores only
            # ``effectTurn`` and calls TryAddStaminaConsumptionAddStatus.
            # The value fields are not a fixed cost; the percentage is read
            # from ExamSetting at the later stamina-payment boundary.
            EFFECT_STAMINA_CONSUMPTION_ADD: (
                value1 == value2 == effect_count == 0
                and (effect_turn == -1 or effect_turn > 0)
            ),
            # All current direct Master rows use the native permanent fixed
            # reduction layer (-1 turn).  Multiple layers add together.
            EFFECT_STAMINA_CONSUMPTION_DOWN_FIX: (
                value1 > 0
                and value2 == effect_count == 0
                and effect_turn == -1
            ),
            EFFECT_MULTIPLE_LESSON_BUFF_LESSON: (
                value1 >= 0
                and value2 > 0
                and effect_count > 0
                and effect_turn == 0
            ),
            EFFECT_LESSON_DEPEND_PARAMETER_BUFF: (
                value1 >= 0
                and 0 <= value2 <= 1000
                and effect_count > 0
                and effect_turn == 0
            ),
            # Android LessonAddMultipleParameterBuffEffectExecutor builds an
            # additional-data multiplier from value2 and applies value1 once
            # per effectCount.  The executor has no turn payload.
            EFFECT_LESSON_ADD_MULTIPLE_PARAMETER_BUFF: (
                value1 >= 0 and value2 >= 0 and effect_count > 0 and effect_turn == 0
            ),
            # ParameterBuffMultiplePerTurnEffectExecutor adds its turn payload
            # to the dedicated status collection; value1/value2/count are
            # unused for this effect kind.
            EFFECT_PARAMETER_BUFF_MULTIPLE_PER_TURN: (
                value1 == value2 == effect_count == 0 and effect_turn > 0
            ),
        }[effect_type]
        if not shape_valid:
            blockers.append(Plan1Blocker("effect-shape-unsupported", effect_id, effect_type))
    return Plan1CompiledEffect(
        effect_id,
        effect_type,
        value1,
        value2,
        effect_count,
        effect_turn,
        tuple(dict.fromkeys(blockers)),
        target_card_id=(
            "" if card_create_contract is None else card_create_contract.card_id
        ),
        target_upgrade=(
            0 if card_create_contract is None else card_create_contract.upgrade
        ),
        move_position_type=(
            MOVE_UNKNOWN
            if card_create_contract is None
            else card_create_contract.raw_move_position_type
        ),
        pick_count_min=(
            0 if card_create_contract is None else card_create_contract.count_min
        ),
        pick_count_max=(
            0 if card_create_contract is None else card_create_contract.count_max
        ),
        timer_child=timer_child,
        status_enchant_id=status_enchant_id,
        hand_grave_draw_contract=hand_grave_draw_contract,
        card_move_contract=card_move_contract,
        card_move_search=card_move_search,
        card_upgrade_contract=card_upgrade_contract,
        search_stamina_contract=search_stamina_contract,
    )


def compile_plan1_effect(
    effect_id: str, *, database: Path = DEFAULT_DATABASE
) -> Plan1CompiledEffect:
    if not effect_id:
        raise ValueError("effect_id must be non-empty")
    with closing(sqlite3.connect(Path(database))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM effect WHERE id = ?", (effect_id,)).fetchone()
        return _compile_effect_row(
            row,
            effect_id,
            database=Path(database),
            connection=connection,
        )


def validate_fktn_ssr_plan1_equipped_item_runtime(
    runtime: Plan1EquippedItemRuntime,
) -> tuple[Plan1Blocker, ...]:
    """Validate the sole equipped-item runtime admitted by the Plan 1 slice."""

    if not isinstance(runtime, Plan1EquippedItemRuntime):
        raise TypeError("runtime must be Plan1EquippedItemRuntime")
    blockers = list(runtime.blockers)
    expected_fields = {
        "item_id": FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
        "item_effect_id": FKTN_SSR_PLAN1_BEFORE_ITEM_EFFECT_ID,
        "status_enchant_id": FKTN_SSR_PLAN1_BEFORE_ITEM_STATUS_ID,
        "trigger_id": FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID,
        "phase_type": PHASE_CARD_PLAY,
        "target_card_id": FKTN_SSR_PLAN1_UNIQUE_CARD_ID,
    }
    for name, expected in expected_fields.items():
        if getattr(runtime, name) != expected:
            blockers.append(
                Plan1Blocker(
                    "equipped-item-contract-mismatch",
                    runtime.item_id,
                    name,
                )
            )
    if runtime.remaining_uses not in (0, 1):
        blockers.append(
            Plan1Blocker(
                "equipped-item-use-count-unsupported",
                runtime.item_id,
                str(runtime.remaining_uses),
            )
        )
    expected_effects = (
        (
            FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS[0],
            EFFECT_PARAMETER_BUFF,
            0,
            0,
            0,
            3,
        ),
        (
            FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS[1],
            EFFECT_BLOCK_FIX,
            5,
            0,
            0,
            0,
        ),
    )
    observed_effects = tuple(
        (
            effect.effect_id,
            effect.effect_type,
            effect.value1,
            effect.value2,
            effect.effect_count,
            effect.effect_turn,
        )
        for effect in runtime.effects
    )
    if observed_effects != expected_effects:
        blockers.append(
            Plan1Blocker(
                "equipped-item-effect-order-unsupported",
                runtime.item_id,
                ",".join(effect.effect_id for effect in runtime.effects),
            )
        )
    blockers.extend(
        blocker for effect in runtime.effects for blocker in effect.blockers
    )
    return tuple(dict.fromkeys(blockers))


def compile_fktn_ssr_plan1_equipped_item_runtime(
    *,
    manifest: Plan1InitialDeckManifest | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan1EquippedItemRuntime:
    """Compile the exact one-use FKTN SSR beforeProduce item from Master."""

    source = load_fktn_ssr_plan1_manifest(database=database) if manifest is None else manifest
    if not isinstance(source, Plan1InitialDeckManifest):
        raise TypeError("manifest must be Plan1InitialDeckManifest or None")
    item = source.before_produce_item
    blockers: list[Plan1Blocker] = []
    expected_manifest = (
        ("item_id", item.item_id, FKTN_SSR_PLAN1_BEFORE_ITEM_ID),
        (
            "item_effect_ids",
            item.item_effect_ids,
            (FKTN_SSR_PLAN1_BEFORE_ITEM_EFFECT_ID,),
        ),
        (
            "status_enchant_ids",
            item.status_enchant_ids,
            (FKTN_SSR_PLAN1_BEFORE_ITEM_STATUS_ID,),
        ),
        (
            "trigger_ids",
            item.trigger_ids,
            (FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID,),
        ),
        (
            "nested_effect_ids",
            item.nested_effect_ids,
            FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS,
        ),
    )
    for field_name, observed, expected in expected_manifest:
        if observed != expected:
            blockers.append(
                Plan1Blocker(
                    "equipped-item-manifest-mismatch",
                    FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
                    field_name,
                )
            )

    parameter_buff = compile_plan1_effect(
        FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS[0], database=database
    )
    block_fix = compile_plan1_effect(
        FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS[1], database=database
    )
    expected_unsupported = Plan1Blocker(
        "effect-type-unsupported",
        FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS[1],
        EFFECT_BLOCK_FIX,
    )
    block_fix_shape = (
        block_fix.effect_id,
        block_fix.effect_type,
        block_fix.value1,
        block_fix.value2,
        block_fix.effect_count,
        block_fix.effect_turn,
    )
    if block_fix_shape == (
        FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS[1],
        EFFECT_BLOCK_FIX,
        5,
        0,
        0,
        0,
    ) and block_fix.blockers == (expected_unsupported,):
        # The global card catalog deliberately keeps ExamBlockFix closed.  Only
        # this exact Master row is promoted inside the equipped-item contract.
        block_fix = replace(block_fix, blockers=())

    runtime = Plan1EquippedItemRuntime(
        item_id=FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
        item_effect_id=FKTN_SSR_PLAN1_BEFORE_ITEM_EFFECT_ID,
        status_enchant_id=FKTN_SSR_PLAN1_BEFORE_ITEM_STATUS_ID,
        trigger_id=FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID,
        phase_type=PHASE_CARD_PLAY,
        target_card_id=FKTN_SSR_PLAN1_UNIQUE_CARD_ID,
        effects=(parameter_buff, block_fix),
        remaining_uses=1,
        blockers=tuple(blockers),
    )
    return replace(
        runtime,
        blockers=validate_fktn_ssr_plan1_equipped_item_runtime(runtime),
    )


def _compile_card_gate(
    connection: sqlite3.Connection,
    trigger_id: str,
    card_id: str,
) -> tuple[Plan1CardGate, int, tuple[Plan1Blocker, ...]]:
    if not trigger_id:
        return Plan1CardGate.ALWAYS, 0, ()
    trigger = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
    ).fetchone()
    if trigger is None:
        return (
            Plan1CardGate.ALWAYS,
            0,
            (Plan1Blocker("card-trigger-missing", card_id, trigger_id),),
        )

    phase_types = _decode_array(trigger["phase_types_json"], f"{trigger_id}:phase")
    phase_values = _decode_array(
        trigger["phase_values_json"], f"{trigger_id}:phase-values"
    )
    field_checks = _decode_array(
        trigger["field_status_check_types_json"], f"{trigger_id}:checks"
    )
    field_types = _decode_array(
        trigger["field_status_types_json"], f"{trigger_id}:field"
    )
    field_values = _decode_array(
        trigger["field_status_values_json"], f"{trigger_id}:values"
    )
    field_searches = _decode_array(
        trigger["field_status_produce_card_search_ids_json"],
        f"{trigger_id}:field-search",
    )
    neutral = (
        phase_values == ()
        and field_checks == ()
        and field_searches == ()
        and trigger["produce_card_search_id"] == ""
        and trigger["upper_search_count"] == 0
        and trigger["lower_search_count"] == 0
        and trigger["card_move_position_type"] == MOVE_UNKNOWN
        and _decode_array(trigger["effect_types_json"], f"{trigger_id}:effects") == ()
        and trigger["lesson_type"] == "ProduceStepLessonType_Unknown"
    )

    # ``e_trigger-exam_start_turn-parameter_buff`` is the ordinary
    # parameter-buff gate (any positive remaining turn).  The character card
    # uses the newer ``ParameterBuffUp`` trigger, whose value is an explicit
    # lower bound and whose native UI text says "2 turns or more".
    if (
        neutral
        and phase_types == ("ProduceExamPhaseType_ExamStartTurn",)
        and field_types == ("ProduceExamFieldStatusType_ParameterBuff",)
        and field_values == ()
    ):
        return Plan1CardGate.PARAMETER_BUFF_ACTIVE, 1, ()
    # Android's IsCardPlayValidTrigger samples the same pre-payment
    # ParameterBuff scalar for this card-level gate.  Keep the admission
    # scoped to the one Master card family whose complete row/effect shape is
    # proven; the effect-slot use of the same trigger remains a separate
    # whitelist above.
    if (
        card_id == PLAN1_CARD_PLAY_PARAMETER_BUFF_GATE_CARD_ID
        and trigger_id == PLAN1_CARD_PLAY_PARAMETER_BUFF_TRIGGER_ID
        and neutral
        and phase_types == (PHASE_CARD_PLAY,)
        and field_types == ("ProduceExamFieldStatusType_ParameterBuff",)
        and field_values == ()
    ):
        raw = _decode_object(str(trigger["raw_json"]), trigger_id)
        expected_raw = {
            "id": trigger_id,
            "phaseTypes": ["ProduceExamPhaseType_ExamCardPlay"],
            "phaseValues": [],
            "fieldStatusCheckTypes": [],
            "fieldStatusTypes": ["ProduceExamFieldStatusType_ParameterBuff"],
            "fieldStatusValues": [],
            "fieldStatusProduceCardSearchIds": [],
            "produceCardSearchId": "",
            "upperSearchCount": 0,
            "lowerSearchCount": 0,
            "cardMovePositionType": MOVE_UNKNOWN,
            "effectTypes": [],
            "lessonType": "ProduceStepLessonType_Unknown",
        }
        if all(raw.get(name) == value for name, value in expected_raw.items()):
            return Plan1CardGate.PARAMETER_BUFF_ACTIVE, 1, ()
        return (
            Plan1CardGate.ALWAYS,
            0,
            (Plan1Blocker("card-trigger-shape-unsupported", card_id, trigger_id),),
        )
    # ``GetCardPlayValidEffectList(card, context)`` invokes the native
    # ``IsCardPlayValidTrigger`` validator before ``ConsumeCardCost``.  A
    # ``ProduceExamPhaseType_None`` row is still a card-owned admission
    # predicate here; it is not the later phase-dispatch ``None=999`` pass.
    # Keep the promotion scoped to this exact Master card/trigger pair until
    # more phase-none families have equivalent native evidence.
    if (
        card_id == PLAN1_CARD_PLAY_LESSON_BUFF_GATE_CARD_ID
        and trigger_id == PLAN1_CARD_PLAY_LESSON_BUFF_GATE_TRIGGER_ID
        and neutral
        and phase_types == ("ProduceExamPhaseType_None",)
        and field_types == ("ProduceExamFieldStatusType_LessonBuffUp",)
        and field_values == (3,)
    ):
        raw = _decode_object(str(trigger["raw_json"]), trigger_id)
        expected_raw = {
            "id": trigger_id,
            "phaseTypes": ["ProduceExamPhaseType_None"],
            "phaseValues": [],
            "fieldStatusCheckTypes": [],
            "fieldStatusTypes": ["ProduceExamFieldStatusType_LessonBuffUp"],
            "fieldStatusValues": [3],
            "fieldStatusProduceCardSearchIds": [],
            "produceCardSearchId": "",
            "upperSearchCount": 0,
            "lowerSearchCount": 0,
            "cardMovePositionType": MOVE_UNKNOWN,
            "effectTypes": [],
            "lessonType": "ProduceStepLessonType_Unknown",
        }
        if all(raw.get(name) == value for name, value in expected_raw.items()):
            return Plan1CardGate.LESSON_BUFF_ACTIVE, 3, ()
        return (
            Plan1CardGate.ALWAYS,
            0,
            (Plan1Blocker("card-trigger-shape-unsupported", card_id, trigger_id),),
        )
    if (
        neutral
        and phase_types == ("ProduceExamPhaseType_None",)
        and field_types == ("ProduceExamFieldStatusType_ParameterBuffUp",)
        and len(field_values) == 1
        and isinstance(field_values[0], int)
        and not isinstance(field_values[0], bool)
        and field_values[0] > 0
    ):
        return Plan1CardGate.PARAMETER_BUFF_ACTIVE, int(field_values[0]), ()

    if not neutral:
        return (
            Plan1CardGate.ALWAYS,
            0,
            (Plan1Blocker("card-trigger-shape-unsupported", card_id, trigger_id),),
        )
    return (
        Plan1CardGate.ALWAYS,
        0,
        (Plan1Blocker("card-trigger-unsupported", card_id, trigger_id),),
    )


def _timer_slot_trigger_blockers(
    connection: sqlite3.Connection,
    trigger_id: str,
    effect: Plan1CompiledEffect,
) -> tuple[Plan1Blocker, ...]:
    """Validate exact per-slot predicates admitted by the native slice.

    The Timer stamina predicate is retained as its own contract.  The three
    ExamCardPlay field-status rows below are ordinary effect-slot predicates;
    their shape is checked column-by-column before the trigger id is carried
    into runtime.  Similar-looking rows never become executable implicitly.
    """

    if effect.effect_type != EFFECT_TIMER and trigger_id in PLAN1_EXACT_EFFECT_TRIGGER_IDS:
        row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if row is None:
            return (Plan1Blocker("master-trigger-missing", trigger_id),)
        raw = _decode_object(str(row["raw_json"]), trigger_id)
        expected_by_trigger = {
            PLAN1_CARD_PLAY_PARAMETER_BUFF_TRIGGER_ID: {
                "phaseTypes": (PHASE_CARD_PLAY,),
                "phaseValues": (),
                "fieldStatusCheckTypes": (),
                "fieldStatusTypes": (
                    "ProduceExamFieldStatusType_ParameterBuff",
                ),
                "fieldStatusValues": (),
                "fieldStatusProduceCardSearchIds": (),
                "produceCardSearchId": "",
                "upperSearchCount": 0,
                "lowerSearchCount": 0,
                "cardMovePositionType": MOVE_UNKNOWN,
                "effectTypes": (),
                "lessonType": "ProduceStepLessonType_Unknown",
            },
            PLAN1_CARD_PLAY_LESSON_BUFF_UP_3_TRIGGER_ID: {
                "phaseTypes": (PHASE_CARD_PLAY,),
                "phaseValues": (),
                "fieldStatusCheckTypes": (),
                "fieldStatusTypes": (
                    "ProduceExamFieldStatusType_LessonBuffUp",
                ),
                "fieldStatusValues": (3,),
                "fieldStatusProduceCardSearchIds": (),
                "produceCardSearchId": "",
                "upperSearchCount": 0,
                # Master carries this lower-search slot for the explicit
                # ``>=`` field threshold; retain it as part of the whitelist.
                "lowerSearchCount": 1,
                "cardMovePositionType": MOVE_UNKNOWN,
                "effectTypes": (),
                "lessonType": "ProduceStepLessonType_Unknown",
            },
            PLAN1_CARD_PLAY_LESSON_BUFF_UP_6_TRIGGER_ID: {
                "phaseTypes": (PHASE_CARD_PLAY,),
                "phaseValues": (),
                "fieldStatusCheckTypes": (),
                "fieldStatusTypes": (
                    "ProduceExamFieldStatusType_LessonBuffUp",
                ),
                "fieldStatusValues": (6,),
                "fieldStatusProduceCardSearchIds": (),
                "produceCardSearchId": "",
                "upperSearchCount": 0,
                "lowerSearchCount": 1,
                "cardMovePositionType": MOVE_UNKNOWN,
                "effectTypes": (),
                "lessonType": "ProduceStepLessonType_Unknown",
            },
            PLAN1_CARD_PLAY_STAMINA_LESS_MULTIPLE_500_TRIGGER_ID: {
                "phaseTypes": (PHASE_CARD_PLAY,),
                "phaseValues": (),
                "fieldStatusCheckTypes": (),
                "fieldStatusTypes": (
                    "ProduceExamFieldStatusType_StaminaLessMultiple",
                ),
                "fieldStatusValues": (500,),
                "fieldStatusProduceCardSearchIds": (),
                "produceCardSearchId": "",
                "upperSearchCount": 0,
                "lowerSearchCount": 0,
                "cardMovePositionType": MOVE_UNKNOWN,
                "effectTypes": (),
                "lessonType": "ProduceStepLessonType_Unknown",
            },
            PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_TRIGGER_ID: {
                "phaseTypes": (PHASE_CARD_PLAY,),
                "phaseValues": (),
                "fieldStatusCheckTypes": (),
                "fieldStatusTypes": (
                    "ProduceExamFieldStatusType_StaminaUpMultiple",
                ),
                "fieldStatusValues": (500,),
                "fieldStatusProduceCardSearchIds": (),
                "produceCardSearchId": "",
                "upperSearchCount": 0,
                "lowerSearchCount": 0,
                "cardMovePositionType": MOVE_UNKNOWN,
                "effectTypes": (),
                "lessonType": "ProduceStepLessonType_Unknown",
            },
        }
        expected = expected_by_trigger[trigger_id]
        row_values: dict[str, object] = {
            "phaseTypes": _decode_array(
                str(row["phase_types_json"]), trigger_id
            ),
            "phaseValues": _decode_array(
                str(row["phase_values_json"]), trigger_id
            ),
            "fieldStatusCheckTypes": _decode_array(
                str(row["field_status_check_types_json"]), trigger_id
            ),
            "fieldStatusTypes": _decode_array(
                str(row["field_status_types_json"]), trigger_id
            ),
            "fieldStatusValues": _decode_array(
                str(row["field_status_values_json"]), trigger_id
            ),
            "fieldStatusProduceCardSearchIds": _decode_array(
                str(row["field_status_produce_card_search_ids_json"]),
                trigger_id,
            ),
            "produceCardSearchId": str(row["produce_card_search_id"]),
            "upperSearchCount": int(row["upper_search_count"]),
            "lowerSearchCount": int(row["lower_search_count"]),
            "cardMovePositionType": str(row["card_move_position_type"]),
            "effectTypes": _decode_array(
                str(row["effect_types_json"]), trigger_id
            ),
            "lessonType": str(row["lesson_type"]),
        }
        blockers: list[Plan1Blocker] = []
        for name, expected_value in expected.items():
            actual = row_values[name]
            raw_actual = raw.get(name)
            if isinstance(expected_value, tuple):
                raw_sequence = (
                    raw_actual
                    if isinstance(raw_actual, (list, tuple))
                    else None
                )
                valid = (
                    actual == expected_value
                    and raw_sequence is not None
                    and tuple(raw_sequence) == expected_value
                )
            else:
                valid = actual == expected_value and raw_actual == expected_value
            if not valid:
                blockers.append(
                    Plan1Blocker("effect-trigger-shape-unsupported", trigger_id, name)
                )
        return tuple(blockers)

    if effect.effect_type != EFFECT_TIMER or trigger_id != PLAN1_TIMER_STAMINA_TRIGGER_ID:
        return (
            Plan1Blocker("effect-trigger-unsupported", effect.effect_id, trigger_id),
        )
    row = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
    ).fetchone()
    if row is None:
        return (Plan1Blocker("master-trigger-missing", trigger_id),)
    raw = _decode_object(str(row["raw_json"]), trigger_id)
    expected = {
        "phaseTypes": (PHASE_CARD_PLAY,),
        "phaseValues": (),
        "fieldStatusCheckTypes": (),
        "fieldStatusTypes": (
            "ProduceExamFieldStatusType_StaminaUpMultiple",
        ),
        "fieldStatusValues": (500,),
        "fieldStatusProduceCardSearchIds": (),
        "produceCardSearchId": "",
        "upperSearchCount": 0,
        "lowerSearchCount": 0,
        "cardMovePositionType": MOVE_UNKNOWN,
        "effectTypes": (),
        "lessonType": "ProduceStepLessonType_Unknown",
    }
    row_values: dict[str, object] = {
        "phaseTypes": _decode_array(str(row["phase_types_json"]), trigger_id),
        "phaseValues": _decode_array(str(row["phase_values_json"]), trigger_id),
        "fieldStatusCheckTypes": _decode_array(
            str(row["field_status_check_types_json"]), trigger_id
        ),
        "fieldStatusTypes": _decode_array(
            str(row["field_status_types_json"]), trigger_id
        ),
        "fieldStatusValues": _decode_array(
            str(row["field_status_values_json"]), trigger_id
        ),
        "fieldStatusProduceCardSearchIds": _decode_array(
            str(row["field_status_produce_card_search_ids_json"]), trigger_id
        ),
        "produceCardSearchId": str(row["produce_card_search_id"]),
        "upperSearchCount": int(row["upper_search_count"]),
        "lowerSearchCount": int(row["lower_search_count"]),
        "cardMovePositionType": str(row["card_move_position_type"]),
        "effectTypes": _decode_array(str(row["effect_types_json"]), trigger_id),
        "lessonType": str(row["lesson_type"]),
    }
    blockers: list[Plan1Blocker] = []
    for name, expected_value in expected.items():
        if row_values[name] != expected_value or (
            tuple(raw.get(name, ())) != expected_value
            if isinstance(expected_value, tuple)
            else raw.get(name) != expected_value
        ):
            blockers.append(
                Plan1Blocker("effect-trigger-shape-unsupported", trigger_id, name)
            )
    return tuple(blockers)


def compile_plan1_card(
    card_id: str,
    upgrade: int = 0,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan1CompiledCard:
    if not card_id:
        raise ValueError("card_id must be non-empty")
    if isinstance(upgrade, bool) or not isinstance(upgrade, int) or upgrade < 0:
        raise ValueError("upgrade must be a non-negative integer")
    with closing(sqlite3.connect(Path(database))) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
            (card_id, upgrade),
        ).fetchone()
        if row is None:
            return Plan1CompiledCard(
                card_id=card_id,
                upgrade=upgrade,
                name="",
                plan_type="",
                category="",
                stamina_cost=0,
                gate=Plan1CardGate.ALWAYS,
                play_trigger_id="",
                move_position_type=MOVE_UNKNOWN,
                effects=(),
                blockers=(Plan1Blocker("master-card-missing", card_id, str(upgrade)),),
                parameter_buff_min_turns=1,
                lesson_buff_min=0,
            )
        raw = _decode_object(str(row["raw_json"]), f"{card_id}@{upgrade}")
        blockers: list[Plan1Blocker] = []
        normalized_card = {
            "id": row["id"],
            "upgradeCount": row["upgrade_count"],
            "name": row["name"],
            "planType": row["plan_type"],
            "category": row["category"],
            "stamina": row["stamina"],
            "costType": row["cost_type"],
            "costValue": row["cost_value"],
            "playProduceExamTriggerId": row["play_trigger_id"],
            "playMovePositionType": row["move_position_type"],
        }
        for name, expected in normalized_card.items():
            if raw.get(name) != expected:
                blockers.append(
                    Plan1Blocker(
                        "master-card-normalization-mismatch", card_id, name
                    )
                )
        if row["plan_type"] not in (PLAN_COMMON, PLAN1):
            blockers.append(Plan1Blocker("card-plan-unsupported", card_id, str(row["plan_type"])))
        if row["category"] not in (CATEGORY_ACTIVE, CATEGORY_MENTAL):
            try:
                # The native empty-effect Trouble transaction is Plan-neutral:
                # IsPlayable, consume one play, increment count, move to Lost.
                from .plan3_sleepy_trouble import load_sleepy_trouble_contract
                load_sleepy_trouble_contract(card_id, upgrade, Path(database))
            except (KeyError, OSError, TypeError, ValueError) as error:
                blockers.append(Plan1Blocker("card-category-unsupported", card_id,
                                             str(row["category"]) + ":" + str(error)))
        if not _supported_plan1_cost(str(row["cost_type"]), raw.get("costValue")):
            blockers.append(
                Plan1Blocker(
                    "card-cost-type-unsupported",
                    card_id,
                    f"{row['cost_type']}:{row['cost_value']}",
                )
            )
        if not isinstance(row["stamina"], int) or row["stamina"] < 0:
            blockers.append(Plan1Blocker("card-stamina-shape-unsupported", card_id, str(row["stamina"])))
        raw_force_stamina = raw.get("forceStamina")
        valid_force_shape = (
            type(raw_force_stamina) is int
            and raw_force_stamina >= 0
            and (
                raw_force_stamina == 0
                or (
                    row["cost_type"] == COST_STAMINA
                    and row["cost_value"] == 0
                    and row["stamina"] == 0
                )
            )
        )
        if not valid_force_shape:
            blockers.append(
                Plan1Blocker(
                    "card-force-stamina-unsupported",
                    card_id,
                    str(raw_force_stamina),
                )
            )
        if raw.get("produceCardStatusEnchantId") not in (None, ""):
            blockers.append(Plan1Blocker("card-status-enchant-unsupported", card_id, str(raw.get("produceCardStatusEnchantId"))))
        if raw.get("moveEffectTriggerType") != MOVE_EFFECT_UNKNOWN or raw.get("moveProduceExamTriggerIds") not in (None, []) or raw.get("moveProduceExamEffectIds") not in (None, []):
            blockers.append(Plan1Blocker("card-move-effect-unsupported", card_id))
        if row["move_position_type"] not in (MOVE_GRAVE, MOVE_LOST):
            blockers.append(Plan1Blocker("card-move-position-unsupported", card_id, str(row["move_position_type"])))
        if raw.get("isEndTurnLost") is not False or raw.get("isRestrict") is not False:
            blockers.append(Plan1Blocker("card-lifecycle-shape-unsupported", card_id))
        raw_effect_group_ids = raw.get("effectGroupIds")
        if not isinstance(raw_effect_group_ids, list) or any(
            not isinstance(value, str) or not value
            for value in raw_effect_group_ids
        ) or len(raw_effect_group_ids) != len(set(raw_effect_group_ids)):
            blockers.append(
                Plan1Blocker(
                    "card-effect-group-shape-unsupported",
                    card_id,
                )
            )
            effect_group_ids: tuple[str, ...] = ()
            effect_group_ids_known = False
        else:
            effect_group_ids = tuple(raw_effect_group_ids)
            effect_group_ids_known = True

        gate, gate_min_turns, gate_blockers = _compile_card_gate(
            connection, str(row["play_trigger_id"]), card_id
        )
        blockers.extend(gate_blockers)

        play_effects = _decode_array(str(row["play_effects_json"]), f"{card_id}:playEffects")
        if tuple(raw.get("playEffects", ())) != play_effects:
            blockers.append(Plan1Blocker("master-card-normalization-mismatch", card_id, "playEffects"))
        effects: list[Plan1CompiledEffect] = []
        for index, value in enumerate(play_effects):
            if not isinstance(value, dict):
                blockers.append(Plan1Blocker("card-play-effect-shape-unsupported", card_id, str(index)))
                continue
            effect_id = value.get("produceExamEffectId")
            if not isinstance(effect_id, str) or not effect_id:
                blockers.append(Plan1Blocker("card-play-effect-id-missing", card_id, str(index)))
                continue
            slot_trigger_id = value.get("produceExamTriggerId")
            if value.get("isOncePlayEffect") is not False:
                blockers.append(Plan1Blocker("effect-once-unsupported", effect_id))
            effect_row = connection.execute("SELECT * FROM effect WHERE id = ?", (effect_id,)).fetchone()
            effect = _compile_effect_row(
                effect_row,
                effect_id,
                database=Path(database),
                connection=connection,
            )
            if (
                effect.effect_type == EFFECT_HAND_GRAVE_COUNT_CARD_DRAW
                and (card_id not in PLAN1_HAND_GRAVE_DRAW_CARD_IDS or upgrade > 3)
            ):
                blockers.append(
                    Plan1Blocker(
                        "card-hand-grave-draw-scope-unsupported",
                        card_id,
                        str(upgrade),
                    )
                )
            if slot_trigger_id not in (None, ""):
                trigger_blockers = _timer_slot_trigger_blockers(
                    connection,
                    str(slot_trigger_id),
                    effect,
                )
                # The shared trigger row also gates a timer card.  For a
                # direct ExamCardPlay effect, only the exact Master card
                # below has a proven ordered status-consumption contract;
                # similarly shaped rows stay fail-closed rather than being
                # opened by trigger-id spelling alone.
                if (
                    not trigger_blockers
                    and str(slot_trigger_id)
                    == PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_TRIGGER_ID
                    and effect.effect_type != EFFECT_TIMER
                    and card_id != PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_CARD_ID
                ):
                    trigger_blockers = (
                        Plan1Blocker(
                            "effect-trigger-unsupported",
                            effect.effect_id,
                            str(slot_trigger_id),
                        ),
                    )
                blockers.extend(trigger_blockers)
                if not trigger_blockers:
                    effect = replace(
                        effect, activation_trigger_id=str(slot_trigger_id)
                    )
            effects.append(effect)
            blockers.extend(effect.blockers)

    return Plan1CompiledCard(
        card_id=card_id,
        upgrade=upgrade,
        name=str(row["name"]),
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina_cost=int(row["stamina"]),
        gate=gate,
        play_trigger_id=str(row["play_trigger_id"]),
        move_position_type=str(row["move_position_type"]),
        effects=tuple(effects),
        blockers=tuple(dict.fromkeys(blockers)),
        parameter_buff_min_turns=gate_min_turns,
        lesson_buff_min=gate_min_turns if gate is Plan1CardGate.LESSON_BUFF_ACTIVE else 0,
        cost_type=str(row["cost_type"]),
        cost_value=int(row["cost_value"]),
        force_stamina_cost=int(raw_force_stamina)
        if type(raw_force_stamina) is int and raw_force_stamina >= 0
        else 0,
        effect_group_ids=effect_group_ids,
        effect_group_ids_known=effect_group_ids_known,
    )


def compile_fktn_ssr_plan1_initial_deck(
    *, database: Path = DEFAULT_DATABASE
) -> Plan1DeckCompilation:
    manifest = load_fktn_ssr_plan1_manifest(database=database)
    programs = tuple(
        compile_plan1_card(entry.card_id, entry.upgrade, database=database)
        for entry in manifest.entries
    )
    equipped_item_runtime = compile_fktn_ssr_plan1_equipped_item_runtime(
        manifest=manifest,
        database=database,
    )
    return Plan1DeckCompilation(manifest, programs, equipped_item_runtime)


def compile_plan1_card_customization(
    card_id: str,
    upgrade: int,
    runtime: object,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> object:
    """Lazy public bridge to the instance-customization compiler.

    Keeping this import lazy avoids a core/customization import cycle while
    allowing callers that already use :mod:`plan1_native_core` to discover the
    new runtime-aware helper from the same public module.
    """

    from .plan1_runtime_customization import compile_plan1_card_customization as _compile

    return _compile(
        card_id,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
    )


def compile_plan1_card_instance(
    instance: object,
    upgrade: int | None = None,
    runtime: object | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1CompiledCard:
    """Lazy public bridge for compiling a captured card instance."""

    from .plan1_runtime_customization import compile_plan1_card_instance as _compile

    return _compile(
        instance,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
    )


def compile_plan1_card_for_instance(
    instance: object,
    upgrade: int | None = None,
    runtime: object | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1CompiledCard:
    """Readable alias for :func:`compile_plan1_card_instance`."""

    from .plan1_runtime_customization import compile_plan1_card_for_instance as _compile

    return _compile(
        instance,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
    )


def compile_plan1_card_from_local_save(
    card: object,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1CompiledCard:
    """Lazy public bridge for a captured LocalSave card instance."""

    from .plan1_runtime_customization import compile_plan1_card_from_local_save as _compile

    return _compile(card, database=database, master_dir=master_dir)


def parse_plan1_runtime_customization_ordered(
    card_id: str,
    upgrade: int,
    runtime: object,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> object:
    """Lazy strict parser for callers rooted at the Plan 1 core module."""

    from .plan1_runtime_customization import parse_plan1_runtime_customization_ordered as _parse

    return _parse(
        card_id,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
    )


def _runtime_blocker(code: str, source_id: str, detail: str = "") -> Plan1Blocker:
    return Plan1Blocker(code, source_id, detail)


def _checked_add(left: int, right: int, source_id: str, field_name: str) -> int:
    result = left + right
    if not 0 <= result <= INT32_MAX:
        raise NativeFormulaDomainError(f"{source_id}:{field_name} addition is outside Int32")
    return result


def _stamina_recover_multiple_request(max_stamina: int, permille: int) -> int:
    """Reproduce Android's ordered binary32 MaxStamina permille request."""

    product = f32(f32(max_stamina) * f32(permille))
    scaled = f32(product / f32(1000.0))
    return ceil_f32_to_i32(scaled)


def _evaluate_card_play_field_status_trigger(
    trigger_id: str, state: Plan1ScalarState
) -> tuple[bool, int, int]:
    """Evaluate one whitelisted ExamCardPlay effect-slot predicate.

    The native predicate reads the pre-payment ``ExamParameterModel``.  The
    caller therefore supplies ``effect_trigger_state`` separately from the
    mutable post-cost ``working`` scalar.  The return tuple is
    ``(fires, requested_threshold, observed_boolean)`` for trace purposes.
    """

    if trigger_id == PLAN1_CARD_PLAY_PARAMETER_BUFF_TRIGGER_ID:
        return state.parameter_buff_turns > 0, 1, int(state.parameter_buff_turns > 0)
    if trigger_id == PLAN1_CARD_PLAY_LESSON_BUFF_UP_3_TRIGGER_ID:
        return state.lesson_buff >= 3, 3, int(state.lesson_buff >= 3)
    if trigger_id == PLAN1_CARD_PLAY_LESSON_BUFF_UP_6_TRIGGER_ID:
        return state.lesson_buff >= 6, 6, int(state.lesson_buff >= 6)
    raise ValueError(f"unsupported Plan1 field-status trigger: {trigger_id}")


def _parameter_application_status_from_scalar(
    state: Plan1ScalarState,
) -> ParameterApplicationStatus:
    """Project the score fields consumed by native ``AddParameter``.

    The original Plan 1 scalar only modeled lesson arithmetic.  Audition
    ``ExamParameterModel`` applies the current schedule target's battle bonus
    after ``CalculateAddingParameter`` for *each* hit, so keeping this mapping
    in one helper prevents one effect family from silently taking the old
    identity path.
    """

    return ParameterApplicationStatus(
        judge_parameter=state.score,
        limit_border=state.limit_border,
        clear_border=state.clear_border,
        current_turn_total_add_parameter=state.current_turn_total_add_parameter,
        is_battle=state.is_battle,
        current_parameter_type=state.current_parameter_type,
        battle_bonus_permille_vocal=state.battle_bonus_permille_vocal,
        battle_bonus_permille_dance=state.battle_bonus_permille_dance,
        battle_bonus_permille_visual=state.battle_bonus_permille_visual,
        judge_parameter_vocal=state.judge_parameter_vocal,
        judge_parameter_dance=state.judge_parameter_dance,
        judge_parameter_visual=state.judge_parameter_visual,
    )


def _adding_parameter_status_from_scalar(
    state: Plan1ScalarState,
) -> AddingParameterStatus:
    """Bind the native LessonParameterMultiple score status once."""

    return AddingParameterStatus(
        parameter_buff=state.parameter_buff_turns > 0,
        parameter_buff_multiple_per_turn=(
            state.parameter_buff_multiple_per_turn_turns > 0
        ),
        parameter_buff_turn=state.parameter_buff_turns,
        lesson_buff=state.lesson_buff,
        lesson_parameter_multiple=LessonParameterMultipleState(
            state.lesson_parameter_multiple_statuses
        ).multiplier(),
        lesson_buff_multiple=permille_to_f32(
            state.lesson_buff_multiple_permille
        ),
    )


def _apply_parameter_application_to_scalar(
    state: Plan1ScalarState,
    application: ParameterApplication,
) -> Plan1ScalarState:
    """Carry native score totals when a runtime battle context is present.

    Legacy lesson fixtures intentionally keep these audition-only totals at
    their historical defaults.  A projected battle scalar, however, must
    retain the per-turn and per-attribute counters that ``AddParameter``
    mutates alongside ``JudgeParameter``.
    """

    if not isinstance(application, ParameterApplication):
        raise TypeError("application must be ParameterApplication")
    if not state.is_battle:
        return replace(state, score=application.after)
    return replace(
        state,
        score=application.after,
        current_turn_total_add_parameter=(
            application.current_turn_total_add_parameter
        ),
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )


def execute_plan1_effects(
    state: Plan1ScalarState,
    effects: Sequence[Plan1CompiledEffect],
    *,
    settings: Plan1NativeSettings | None = None,
    card_draw_resolver: Plan1CardDrawResolver | None = None,
    hand_grave_draw_resolver: Plan1HandGraveDrawResolver | None = None,
    card_move_resolver: Plan1CardMoveResolver | None = None,
    card_upgrade_resolver: Plan1CardUpgradeResolver | None = None,
    card_create_resolver: Plan1CardCreateResolver | None = None,
    effect_timer_resolver: Plan1EffectTimerResolver | None = None,
    effect_trigger_state: Plan1ScalarState | None = None,
    parameter_buff_status_change_effects: Sequence[Plan1CompiledEffect] = (),
) -> Plan1EffectExecution:
    """Execute an already ordered, all-direct effect list atomically."""

    effects = tuple(effects)
    parameter_buff_status_change_effects = tuple(
        parameter_buff_status_change_effects
    )
    if any(
        not isinstance(value, Plan1CompiledEffect)
        for value in parameter_buff_status_change_effects
    ):
        return Plan1EffectExecution(
            state,
            state,
            (),
            (
                _runtime_blocker(
                    "parameter-buff-listener-invalid",
                    "effect-list",
                    "listener effects must be Plan1CompiledEffect values",
                ),
            ),
        )
    if any(
        value.effect_type not in {EFFECT_LESSON_BUFF, EFFECT_PARAMETER_BUFF}
        for value in parameter_buff_status_change_effects
    ):
        return Plan1EffectExecution(
            state,
            state,
            (),
            (
                _runtime_blocker(
                    "parameter-buff-listener-effect-unsupported",
                    "effect-list",
                ),
            ),
        )
    compile_blockers = tuple(
        blocker for effect in effects for blocker in effect.blockers
    )
    if compile_blockers:
        return Plan1EffectExecution(state, state, (), tuple(dict.fromkeys(compile_blockers)))
    if settings is None:
        settings = load_plan1_native_settings()
    working = state
    trace: list[Plan1TraceEntry] = []
    current_effect_id = "effect-list"
    parameter_buff_applied = False
    try:
        for effect_index, effect in enumerate(effects):
            current_effect_id = effect.effect_id
            if effect.activation_trigger_id:
                trigger_id = effect.activation_trigger_id
                if trigger_id in PLAN1_EXACT_EFFECT_TRIGGER_IDS:
                    if effect_trigger_state is None:
                        return Plan1EffectExecution(
                            state,
                            state,
                            (),
                            (
                                _runtime_blocker(
                                    "effect-trigger-context-required",
                                    trigger_id,
                                    "pre-payment ExamCardPlay scalar is required",
                                ),
                            ),
                        )
                    if trigger_id in {
                        PLAN1_CARD_PLAY_STAMINA_LESS_MULTIPLE_500_TRIGGER_ID,
                        PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_TRIGGER_ID,
                    }:
                        evaluation = evaluate_stamina_multiple_trigger(
                            STAMINA_MULTIPLE_TRIGGER_BY_ID[trigger_id],
                            StaminaSnapshot(
                                current_stamina=effect_trigger_state.stamina,
                                max_stamina=effect_trigger_state.max_stamina,
                            ),
                            event_phase=PHASE_CARD_PLAY,
                        )
                        if evaluation.fires is None:
                            return Plan1EffectExecution(
                                state,
                                state,
                                (),
                                (
                                    _runtime_blocker(
                                        "effect-trigger-unresolved",
                                        trigger_id,
                                        ",".join(evaluation.reasons),
                                    ),
                                ),
                            )
                        fires = evaluation.fires
                        requested = STAMINA_MULTIPLE_TRIGGER_BY_ID[trigger_id].field_values[0]
                        actual = int(fires)
                    else:
                        fires, requested, actual = _evaluate_card_play_field_status_trigger(
                            trigger_id, effect_trigger_state
                        )
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT_TRIGGER,
                            trigger_id,
                            working,
                            working,
                            effect.effect_type,
                            effect_index,
                            None,
                            requested,
                            actual,
                        )
                    )
                    if not fires:
                        continue
                elif trigger_id != PLAN1_TIMER_STAMINA_TRIGGER_ID:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-trigger-unsupported",
                                effect.effect_id,
                                trigger_id,
                            ),
                        ),
                    )
                elif trigger_id == PLAN1_TIMER_STAMINA_TRIGGER_ID:
                    if effect_trigger_state is None:
                        return Plan1EffectExecution(
                            state,
                            state,
                            (),
                            (
                                _runtime_blocker(
                                    "effect-trigger-context-required",
                                    trigger_id,
                                    "pre-payment ExamCardPlay scalar is required",
                                ),
                            ),
                        )
                    evaluation = evaluate_stamina_multiple_trigger(
                        trigger_id,
                        StaminaSnapshot(
                            current_stamina=effect_trigger_state.stamina,
                            max_stamina=effect_trigger_state.max_stamina,
                        ),
                        event_phase=PHASE_CARD_PLAY,
                    )
                    if evaluation.fires is None:
                        return Plan1EffectExecution(
                            state,
                            state,
                            (),
                            (
                                _runtime_blocker(
                                    "effect-trigger-unresolved",
                                    trigger_id,
                                    ",".join(evaluation.reasons),
                                ),
                            ),
                        )
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT_TRIGGER,
                            trigger_id,
                            working,
                            working,
                            effect.effect_type,
                            effect_index,
                            None,
                            500,
                            int(evaluation.fires),
                        )
                    )
                    if not evaluation.fires:
                        continue
            if effect.effect_type == EFFECT_LESSON:
                for hit_index in range(effect.effect_count):
                    before = working
                    requested = calculate_adding_parameter(
                        effect.value1,
                        is_buff_active=True,
                        status=_adding_parameter_status_from_scalar(working),
                        settings=settings.adding_parameter,
                    )
                    application = apply_parameter_add(
                        requested,
                        status=_parameter_application_status_from_scalar(working),
                    )
                    working = _apply_parameter_application_to_scalar(
                        working, application
                    )
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT,
                            effect.effect_id,
                            before,
                            working,
                            effect.effect_type,
                            effect_index,
                            hit_index,
                            requested,
                            application.actual_parameter,
                        )
                    )
            elif effect.effect_type == EFFECT_LESSON_ADD_MULTIPLE_PARAMETER_BUFF:
                # LessonAddMultipleParameterBuffEffectExecutor sets
                # MultipleParameterBuffRate to
                # 1 + FloatFromPermil(effectValue2), then runs the normal
                # CalculateAddingParameter/AddParameter pair effectCount
                # times.  Keeping the pair in this loop preserves native
                # per-hit limit and difference semantics.
                additional = AddingParameterAdditionalData(
                    multiple_parameter_buff_rate=f32(
                        1.0 + permille_to_f32(effect.value2)
                    )
                )
                for hit_index in range(effect.effect_count):
                    before = working
                    requested = calculate_adding_parameter(
                        effect.value1,
                        is_buff_active=True,
                        status=_adding_parameter_status_from_scalar(working),
                        settings=settings.adding_parameter,
                        additional=additional,
                    )
                    application = apply_parameter_add(
                        requested,
                        status=_parameter_application_status_from_scalar(working),
                    )
                    working = _apply_parameter_application_to_scalar(
                        working, application
                    )
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT,
                            effect.effect_id,
                            before,
                            working,
                            effect.effect_type,
                            effect_index,
                            hit_index,
                            requested,
                            application.actual_parameter,
                        )
                    )
            elif effect.effect_type == EFFECT_BLOCK:
                before = working
                actual = calculate_add_block(
                    effect.value1,
                    is_buff_active=True,
                    status=AddBlockStatus(),
                    settings=settings.add_block,
                )
                working = replace(
                    working,
                    block=_checked_add(working.block, actual, effect.effect_id, "block"),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace), Plan1TraceStage.EFFECT, effect.effect_id,
                        before, working, effect.effect_type, effect_index, None,
                        effect.value1, actual,
                    )
                )
            elif effect.effect_type == EFFECT_BLOCK_FIX:
                if (
                    effect.value1 < 0
                    or effect.value2 != 0
                    or effect.effect_count != 0
                    or effect.effect_turn != 0
                ):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-block-fix-scope-unsupported",
                                effect.effect_id,
                                effect.effect_type,
                            ),
                        ),
                    )
                before = working
                working = replace(
                    working,
                    block=_checked_add(
                        working.block,
                        effect.value1,
                        effect.effect_id,
                        "block",
                    ),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.value1,
                        effect.value1,
                    )
                )
            elif effect.effect_type == EFFECT_PARAMETER_BUFF:
                if (
                    effect.value1 != 0
                    or effect.value2 != 0
                    or effect.effect_count != 0
                    or effect.effect_turn <= 0
                ):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-parameter-buff-scope-unsupported",
                                effect.effect_id,
                                effect.effect_type,
                            ),
                        ),
                    )
                before = working
                working = replace(
                    working,
                    parameter_buff_turns=_checked_add(
                        working.parameter_buff_turns,
                        effect.effect_turn,
                        effect.effect_id,
                        "parameter_buff_turns",
                    ),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace), Plan1TraceStage.EFFECT, effect.effect_id,
                        before, working, effect.effect_type, effect_index, None,
                        effect.effect_turn, effect.effect_turn,
                    )
                )
                parameter_buff_applied = True
            elif effect.effect_type == EFFECT_PARAMETER_BUFF_MULTIPLE_PER_TURN:
                # Native TryAddParameterBuffMultiplePerTurnStatus adds the
                # requested turns to an existing status (or creates it when
                # absent).  Turn-end reduction is handled by the explicit
                # advance_plan1_turn helper below.
                before = working
                was_absent = (
                    working.parameter_buff_multiple_per_turn_turns == 0
                )
                working = replace(
                    working,
                    parameter_buff_multiple_per_turn_turns=_checked_add(
                        working.parameter_buff_multiple_per_turn_turns,
                        effect.effect_turn,
                        effect.effect_id,
                        "parameter_buff_multiple_per_turn_turns",
                    ),
                    parameter_buff_multiple_per_turn_fresh=(
                        working.parameter_buff_multiple_per_turn_fresh
                        or was_absent
                    ),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.effect_turn,
                        effect.effect_turn,
                    )
                )
            elif effect.effect_type == EFFECT_LESSON_BUFF:
                before = working
                multiple = permille_to_f32(
                    working.lesson_buff_gain_multiple_permille
                )
                actual = ceil_f32_to_i32(
                    f32(f32(effect.value1) * multiple)
                )
                working = replace(
                    working,
                    lesson_buff=_checked_add(
                        working.lesson_buff,
                        actual,
                        effect.effect_id,
                        "lesson_buff",
                    ),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace), Plan1TraceStage.EFFECT, effect.effect_id,
                        before, working, effect.effect_type, effect_index, None,
                        effect.value1, actual,
                    )
                )
            elif effect.effect_type == EFFECT_LESSON_VALUE_MULTIPLE:
                before = working
                installed = install_lesson_value_multiple(
                    LessonParameterMultipleState(
                        working.lesson_parameter_multiple_statuses
                    ),
                    permil=effect.value1,
                    turn=effect.effect_turn,
                    new_status_uid=working.next_status_uid,
                )
                if not installed.installed:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-lesson-value-multiple-install-blocked",
                                effect.effect_id,
                            ),
                        ),
                    )
                working = replace(
                    working,
                    lesson_parameter_multiple_statuses=(
                        installed.state.statuses
                    ),
                    next_status_uid=(
                        working.next_status_uid + 1
                        if installed.merged_index is None
                        else working.next_status_uid
                    ),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.value1,
                        effect.value1,
                    )
                )
            elif effect.effect_type == EFFECT_LESSON_BUFF_ADDITIVE:
                # This status installer is admitted only by the runtime
                # interval dispatcher after it validates the captured
                # listener/effect/lifecycle contract.  The static card
                # catalog keeps the family blocked because it does not own
                # finite status persistence.
                if (
                    effect.value1 <= 0
                    or effect.value2 != 0
                    or effect.effect_count != 0
                    or effect.effect_turn <= 0
                ):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-lesson-buff-additive-shape-unsupported",
                                effect.effect_id,
                            ),
                        ),
                    )
                before = working
                if working.next_status_uid >= INT32_MAX:
                    raise NativeFormulaDomainError(
                        f"{effect.effect_id}:status UID overflow"
                    )
                status = Plan1LessonBuffAdditiveStatus(
                    uid=working.next_status_uid,
                    value_permille=effect.value1,
                    turns=effect.effect_turn,
                    passing_turn_start=False,
                )
                working = replace(
                    working,
                    lesson_buff_gain_multiple_permille=_checked_add(
                        working.lesson_buff_gain_multiple_permille,
                        effect.value1,
                        effect.effect_id,
                        "lesson_buff_gain_multiple_permille",
                    ),
                    lesson_buff_additive_statuses=(
                        *working.lesson_buff_additive_statuses,
                        status,
                    ),
                    next_status_uid=working.next_status_uid + 1,
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.value1,
                        effect.value1,
                    )
                )
            elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
                before = working
                working = replace(
                    working,
                    plays_remaining=_checked_add(
                        working.plays_remaining,
                        effect.effect_count,
                        effect.effect_id,
                        "plays_remaining",
                    ),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace), Plan1TraceStage.EFFECT, effect.effect_id,
                        before, working, effect.effect_type, effect_index, None,
                        effect.effect_count, effect.effect_count,
                    )
                )
            elif effect.effect_type == EFFECT_STATUS_ENCHANT:
                # Installing a persistent listener has no immediate scalar or
                # ordered-zone mutation.  Transition-local replay projection
                # restores that listener from the next native state_before.
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        working,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        1,
                        1,
                    )
                )
            elif effect.effect_type == EFFECT_CARD_DRAW:
                if card_draw_resolver is None:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-context-required",
                                effect.effect_id,
                                "CardDraw requires ordered stage zones",
                            ),
                        ),
                    )
                resolution = card_draw_resolver(
                    working,
                    effect,
                    effect_index,
                )
                if not isinstance(resolution, Plan1CardDrawResolution):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-invalid",
                                effect.effect_id,
                                type(resolution).__name__,
                            ),
                        ),
                    )
                if resolution.before != working:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-stale",
                                effect.effect_id,
                            ),
                        ),
                    )
                if resolution.requested_count != effect.value1:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-invalid",
                                effect.effect_id,
                                "requested count changed",
                            ),
                        ),
                    )
                if resolution.blockers:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        tuple(dict.fromkeys(resolution.blockers)),
                    )
                before = working
                working = resolution.after
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        resolution.requested_count,
                        resolution.actual_count,
                    )
                )
            elif effect.effect_type == EFFECT_CARD_UPGRADE:
                if card_upgrade_resolver is None:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-context-required",
                                effect.effect_id,
                                "CardUpgrade requires ordered stage zones",
                            ),
                        ),
                    )
                resolution = card_upgrade_resolver(
                    working,
                    effect,
                    effect_index,
                )
                if not isinstance(resolution, Plan1CardUpgradeResolution):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-invalid",
                                effect.effect_id,
                                type(resolution).__name__,
                            ),
                        ),
                    )
                if resolution.before is not working or resolution.blockers:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        tuple(resolution.blockers) or (
                            _runtime_blocker(
                                "effect-zone-resolution-stale",
                                effect.effect_id,
                            ),
                        ),
                    )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        working,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        resolution.candidate_count,
                        resolution.upgraded_count,
                    )
                )
            elif effect.effect_type == EFFECT_CARD_MOVE:
                if (
                    effect.card_move_contract is None
                    or effect.value1 != 0
                    or effect.value2 != 0
                    or effect.effect_count != 0
                    or effect.effect_turn != 0
                ):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-card-move-contract-invalid",
                                effect.effect_id,
                            ),
                        ),
                    )
                if card_move_resolver is None:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-context-required",
                                effect.effect_id,
                                "CardMove requires ordered stage zones and native RNG",
                            ),
                        ),
                    )
                resolution = card_move_resolver(
                    working,
                    effect,
                    effect_index,
                )
                if not isinstance(resolution, Plan1CardMoveResolution):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-invalid",
                                effect.effect_id,
                                type(resolution).__name__,
                            ),
                        ),
                    )
                if resolution.before is not working:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-stale",
                                effect.effect_id,
                            ),
                        ),
                    )
                if resolution.blockers:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        tuple(dict.fromkeys(resolution.blockers)),
                    )
                before = working
                working = resolution.after
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        resolution.candidate_count,
                        resolution.moved_count,
                    )
                )
            elif effect.effect_type == EFFECT_HAND_GRAVE_COUNT_CARD_DRAW:
                if (
                    effect.hand_grave_draw_contract is None
                    or effect.value1 != 0
                    or effect.value2 != 0
                    or effect.effect_count != 0
                    or effect.effect_turn != 0
                ):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-hand-grave-draw-contract-invalid",
                                effect.effect_id,
                            ),
                        ),
                    )
                if hand_grave_draw_resolver is None:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-context-required",
                                effect.effect_id,
                                "HandGraveCountCardDraw requires ordered zones and HandAdd runtime",
                            ),
                        ),
                    )
                resolution = hand_grave_draw_resolver(
                    working,
                    effect,
                    effect_index,
                )
                if not isinstance(resolution, Plan1HandGraveDrawResolution):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-invalid",
                                effect.effect_id,
                                type(resolution).__name__,
                            ),
                        ),
                    )
                if resolution.before is not working:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-stale",
                                effect.effect_id,
                            ),
                        ),
                    )
                if resolution.blockers:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        tuple(dict.fromkeys(resolution.blockers)),
                    )
                before = working
                working = resolution.after
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        resolution.requested_count,
                        resolution.actual_count,
                    )
                )
            elif effect.effect_type == EFFECT_CARD_CREATE_ID:
                if card_create_resolver is None:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-context-required",
                                effect.effect_id,
                                "CardCreateId requires ordered stage zones and caller GUIDs",
                            ),
                        ),
                    )
                resolution = card_create_resolver(
                    working,
                    effect,
                    effect_index,
                )
                if not isinstance(resolution, Plan1CardCreateResolution):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-invalid",
                                effect.effect_id,
                                type(resolution).__name__,
                            ),
                        ),
                    )
                if resolution.before is not working:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-stale",
                                effect.effect_id,
                            ),
                        ),
                    )
                if resolution.requested_count != effect.pick_count_min:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-zone-resolution-invalid",
                                effect.effect_id,
                                "requested count changed",
                            ),
                        ),
                    )
                if resolution.blockers:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        tuple(dict.fromkeys(resolution.blockers)),
                    )
                before = working
                working = resolution.after
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        resolution.requested_count,
                        resolution.actual_count,
                    )
                )
            elif effect.effect_type == EFFECT_TIMER:
                child = effect.timer_child
                if (
                    effect.value1 < 1
                    or effect.value2 != 0
                    or effect.effect_count != 1
                    or effect.effect_turn != 0
                    or child is None
                    or child.effect_type not in PLAN1_TIMER_CHILD_EFFECT_TYPES
                    or child.effect_type == EFFECT_TIMER
                    or not child.executable
                ):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-timer-contract-invalid",
                                effect.effect_id,
                            ),
                        ),
                    )
                if effect_timer_resolver is None:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-timer-context-required",
                                effect.effect_id,
                                "EffectTimer requires stage-owned lifecycle state",
                            ),
                        ),
                    )
                resolution = effect_timer_resolver(
                    working,
                    effect,
                    effect_index,
                )
                if not isinstance(resolution, Plan1EffectTimerResolution):
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-timer-resolution-invalid",
                                effect.effect_id,
                                type(resolution).__name__,
                            ),
                        ),
                    )
                if resolution.before is not working:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-timer-resolution-stale",
                                effect.effect_id,
                            ),
                        ),
                    )
                if resolution.delay_turns != effect.value1:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        (
                            _runtime_blocker(
                                "effect-timer-resolution-invalid",
                                effect.effect_id,
                                "delay changed",
                            ),
                        ),
                    )
                if resolution.blockers:
                    return Plan1EffectExecution(
                        state,
                        state,
                        (),
                        tuple(dict.fromkeys(resolution.blockers)),
                    )
                before = working
                working = resolution.after
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.value1,
                        effect.value1,
                    )
                )
            elif effect.effect_type == EFFECT_SEARCH_STAMINA_CHANGE:
                if effect.search_stamina_contract is None:
                    raise NativeFormulaDomainError("search stamina installer has no typed Master contract")
                before = working
                runtime = replace(working.search_play_card_stamina_runtime, next_status_uid=working.next_status_uid)
                installed, _status, _prior, _created = install_search_play_card_stamina_status(runtime, effect.search_stamina_contract)
                working = replace(working, search_play_card_stamina_runtime=installed, next_status_uid=installed.next_status_uid)
                trace.append(Plan1TraceEntry(len(trace), Plan1TraceStage.EFFECT, effect.effect_id,
                    before, working, effect.effect_type, effect_index, None, effect.effect_count, effect.effect_count))
            elif effect.effect_type == EFFECT_STAMINA_REDUCE_FIX:
                # ReduceStaminaFix (0x7E5EFF8) directly clamps HP. It does not
                # use block, ordinary cost modifiers or their payment counters.
                before = working
                working = replace(working, stamina=max(0, min(working.max_stamina, working.stamina - effect.value1)))
                trace.append(Plan1TraceEntry(
                    len(trace), Plan1TraceStage.EFFECT, effect.effect_id,
                    before, working, effect.effect_type, effect_index, None,
                    effect.value1, before.stamina - working.stamina,
                ))
            elif effect.effect_type == EFFECT_STAMINA_RECOVER_FIX:
                before = working
                actual = min(effect.value1, working.max_stamina - working.stamina)
                working = replace(working, stamina=working.stamina + actual)
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.value1,
                        actual,
                    )
                )
            elif effect.effect_type == EFFECT_STAMINA_RECOVER_MULTIPLE:
                before = working
                # Android 3.2.3 StaminaRecoverMultipleEffectExecutor
                # (Execute 0x7E8D844) snapshots MaxStamina, performs
                # f32(f32(MaxStamina * value1) / 1000.0f), applies an exact
                # FRINTP/FCVTPS ceiling, and only then enters the same fixed
                # recovery/cap path used above.  This scalar state has no
                # StaminaRecoverRestriction/Add layers, matching the closed
                # direct-card slice documented by this module.
                requested = _stamina_recover_multiple_request(
                    working.max_stamina,
                    effect.value1,
                )
                actual = min(requested, working.max_stamina - working.stamina)
                working = replace(working, stamina=working.stamina + actual)
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        requested,
                        actual,
                    )
                )
            elif effect.effect_type == EFFECT_ANTI_DEBUFF:
                before = working
                # Android 3.2.3 AntiDebuffEffectExecutor.ExecuteEffect
                # (0x7E714E0) calls the native add path with effectCount.
                # The status is permanent (turn=-1) and an existing status
                # merges by AddCount, so this projection is one exact scalar.
                after_count = _checked_add(
                    working.anti_debuff_count,
                    effect.effect_count,
                    effect.effect_id,
                    "anti_debuff_count",
                )
                working = replace(working, anti_debuff_count=after_count)
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.effect_count,
                        effect.effect_count,
                    )
                )
            elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
                before = working
                current = working.stamina_consumption_down_turns
                turns = (
                    -1
                    if current == -1 or effect.effect_turn == -1
                    else _checked_add(
                        current,
                        effect.effect_turn,
                        effect.effect_id,
                        "stamina_consumption_down_turns",
                    )
                )
                working = replace(
                    working,
                    stamina_consumption_down_turns=turns,
                    stamina_consumption_down_fresh=True,
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.effect_turn,
                        effect.effect_turn,
                    )
                )
            elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_ADD:
                before = working
                current = working.stamina_consumption_add_turns
                # The native collection keeps one same-type Add status and
                # extends finite instances with AddTurn.  A permanent
                # instance cannot be extended; the executor reports no
                # mutation for that case.  A fresh install is exempt from the
                # first turn-boundary spend, while a merge preserves the
                # existing freshness flag.
                if current == -1:
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT,
                            effect.effect_id,
                            before,
                            working,
                            effect.effect_type,
                            effect_index,
                            None,
                            effect.effect_turn,
                            0,
                        )
                    )
                    continue
                if current == 0:
                    turns = effect.effect_turn
                    fresh = True
                else:
                    turns = _checked_add(
                        current,
                        effect.effect_turn,
                        effect.effect_id,
                        "stamina_consumption_add_turns",
                    )
                    fresh = working.stamina_consumption_add_fresh
                working = replace(
                    working,
                    stamina_consumption_add_turns=turns,
                    stamina_consumption_add_fresh=fresh,
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.effect_turn,
                        effect.effect_turn if current != -1 else 0,
                    )
                )
            elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX:
                before = working
                working = replace(
                    working,
                    stamina_consumption_down_fixed=_checked_add(
                        working.stamina_consumption_down_fixed,
                        effect.value1,
                        effect.effect_id,
                        "stamina_consumption_down_fixed",
                    ),
                )
                trace.append(
                    Plan1TraceEntry(
                        len(trace),
                        Plan1TraceStage.EFFECT,
                        effect.effect_id,
                        before,
                        working,
                        effect.effect_type,
                        effect_index,
                        None,
                        effect.value1,
                        effect.value1,
                    )
                )
            elif effect.effect_type == EFFECT_MULTIPLE_LESSON_BUFF_LESSON:
                # Native 0x7E869D8 derives the dependent base from the raw
                # current LessonBuff.  The active LessonBuff multiplier is
                # applied later by CalculateAddingParameter; applying it in
                # both places double-counts additive focus statuses.
                product = effect.value2 * working.lesson_buff
                if not 0 <= product <= INT32_MAX:
                    raise NativeFormulaDomainError(
                        f"{effect.effect_id}:lesson-buff product is outside Int32"
                    )
                dependent = ceil_f32_to_i32(
                    f32(f32(product) / f32(1000.0))
                )
                base_value = _checked_add(
                    effect.value1,
                    dependent,
                    effect.effect_id,
                    "multiple_lesson_buff_lesson",
                )
                for hit_index in range(effect.effect_count):
                    before = working
                    requested = calculate_adding_parameter(
                        base_value,
                        is_buff_active=True,
                        status=_adding_parameter_status_from_scalar(working),
                        settings=settings.adding_parameter,
                    )
                    application = apply_parameter_add(
                        requested,
                        status=_parameter_application_status_from_scalar(working),
                    )
                    working = _apply_parameter_application_to_scalar(
                        working, application
                    )
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT,
                            effect.effect_id,
                            before,
                            working,
                            effect.effect_type,
                            effect_index,
                            hit_index,
                            requested,
                            application.actual_parameter,
                        )
                    )
            elif effect.effect_type == EFFECT_LESSON_DEPEND_PARAMETER_BUFF:
                # Native 0x7E83378: ceil(value1/1000 * current good-condition
                # turns - epsilon), then ordinary score calculation.  value2
                # optionally consumes a proportional share of those turns.
                source_turns = working.parameter_buff_turns
                coefficient = permille_to_f32(effect.value1)
                dependent = ceil_f32_to_i32(
                    f32(
                        f32(coefficient * f32(source_turns))
                        + F32_NEGATIVE_EPSILON
                    )
                )
                for hit_index in range(effect.effect_count):
                    before = working
                    requested = calculate_adding_parameter(
                        dependent,
                        is_buff_active=True,
                        status=_adding_parameter_status_from_scalar(working),
                        settings=settings.adding_parameter,
                    )
                    application = apply_parameter_add(
                        requested,
                        status=_parameter_application_status_from_scalar(working),
                    )
                    working = _apply_parameter_application_to_scalar(
                        working, application
                    )
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT,
                            effect.effect_id,
                            before,
                            working,
                            effect.effect_type,
                            effect_index,
                            hit_index,
                            requested,
                            application.actual_parameter,
                        )
                    )
                remaining_rate = f32(
                    f32(1.0) - permille_to_f32(effect.value2)
                )
                remaining_turns = ceil_f32_to_i32(
                    f32(remaining_rate * f32(source_turns))
                )
                if remaining_turns != working.parameter_buff_turns:
                    before = working
                    working = replace(
                        working,
                        parameter_buff_turns=remaining_turns,
                    )
                    trace.append(
                        Plan1TraceEntry(
                            len(trace),
                            Plan1TraceStage.EFFECT,
                            effect.effect_id,
                            before,
                            working,
                            effect.effect_type,
                            effect_index,
                            None,
                            source_turns,
                            remaining_turns,
                        )
                    )
            else:  # Defensive: callers can construct the public DTO manually.
                return Plan1EffectExecution(
                    state,
                    state,
                    (),
                    (_runtime_blocker("effect-type-unsupported", effect.effect_id, effect.effect_type),),
                )
    except (NativeFormulaDomainError, ValueError, OverflowError) as error:
        return Plan1EffectExecution(
            state,
            state,
            (),
            (
                _runtime_blocker(
                    "effect-runtime-domain", current_effect_id, str(error)
                ),
            ),
        )
    # The native card command exposes one status-change dispatch after the
    # ordered card effect list has committed.  Keep the listener conditional
    # on an actually-applied ParameterBuff effect so a gated slot cannot fire
    # merely because a card program contains that effect family.
    if parameter_buff_applied and parameter_buff_status_change_effects:
        listener_execution = execute_plan1_effects(
            working,
            parameter_buff_status_change_effects,
            settings=settings,
            effect_trigger_state=effect_trigger_state,
        )
        if listener_execution.blockers:
            return Plan1EffectExecution(
                state,
                state,
                (),
                tuple(dict.fromkeys(listener_execution.blockers)),
            )
        working = listener_execution.after
        trace.extend(listener_execution.trace)
    return Plan1EffectExecution(state, working, tuple(trace))


def advance_plan1_turn(
    state: Plan1ScalarState, turns: int = 1
) -> Plan1ScalarState:
    """Advance only the status/turn fields proven by native status code.

    ``ExamStatusEffectCollection.SpendTurn`` reduces ordinary
    ``ParameterBuffStatusEffect`` and ``ParameterBuffMultiplePerTurnStatusEffect``
    counters by one at the turn boundary.  This helper mirrors those scalar
    mutations and increments ``turn``.  It intentionally does *not* synthesize
    a new hand, playable-value reset, card draw, or turn-start trigger: those
    require live ``ExamEffectCalculateContext`` inputs outside this card core.
    """

    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
        raise ValueError("turns must be a positive integer")
    stamina_down = state.stamina_consumption_down_turns
    down_spend = max(
        turns - (1 if state.stamina_consumption_down_fresh else 0),
        0,
    )
    if stamina_down == -1:
        next_stamina_down = stamina_down
    else:
        next_stamina_down = max(stamina_down - down_spend, 0)
    stamina_add = state.stamina_consumption_add_turns
    add_spend = max(
        turns - (1 if state.stamina_consumption_add_fresh else 0),
        0,
    )
    if stamina_add == -1:
        next_stamina_add = stamina_add
    else:
        next_stamina_add = max(stamina_add - add_spend, 0)
    parameter_spend = max(
        turns - (1 if state.parameter_buff_fresh else 0),
        0,
    )
    multiple_parameter_spend = max(
        turns
        - (1 if state.parameter_buff_multiple_per_turn_fresh else 0),
        0,
    )
    additive_statuses: list[Plan1LessonBuffAdditiveStatus] = []
    removed_additive = 0
    for status in state.lesson_buff_additive_statuses:
        spend = max(
            turns - (0 if status.passing_turn_start else 1),
            0,
        )
        if status.turns == -1:
            additive_statuses.append(
                replace(status, passing_turn_start=True)
            )
            continue
        remaining = status.turns - spend
        if remaining < 1:
            removed_additive = _checked_add(
                removed_additive,
                status.value_permille,
                "lesson-buff-additive",
                "removed_additive",
            )
        else:
            additive_statuses.append(
                replace(
                    status,
                    turns=remaining,
                    passing_turn_start=True,
                )
            )
    interval_listeners = tuple(
        replace(
            listener,
            turn_count=_checked_add(
                listener.turn_count,
                turns,
                listener.rule_id,
                "turn_count",
            ),
        )
        for listener in state.play_count_interval_listeners
    )
    lesson_parameter_state = LessonParameterMultipleState(
        state.lesson_parameter_multiple_statuses
    )
    for _ in range(turns):
        lesson_parameter_state = (
            lesson_parameter_state
            .spend_turn_start()
            .mark_passing_turn_start()
        )
    lesson_parameter_statuses = lesson_parameter_state.statuses
    return replace(
        state,
        turn=_checked_add(state.turn, turns, "turn", "turn"),
        parameter_buff_turns=(
            state.parameter_buff_turns
            if state.parameter_buff_turns == -1
            else max(state.parameter_buff_turns - parameter_spend, 0)
        ),
        parameter_buff_multiple_per_turn_turns=(
            state.parameter_buff_multiple_per_turn_turns
            if state.parameter_buff_multiple_per_turn_turns == -1
            else max(
                state.parameter_buff_multiple_per_turn_turns
                - multiple_parameter_spend,
                0,
            )
        ),
        parameter_buff_multiple_per_turn_fresh=False,
        parameter_buff_fresh=False,
        stamina_consumption_down_turns=next_stamina_down,
        stamina_consumption_down_fresh=False,
        stamina_consumption_add_turns=next_stamina_add,
        stamina_consumption_add_fresh=False,
        lesson_buff_gain_multiple_permille=(
            state.lesson_buff_gain_multiple_permille - removed_additive
        ),
        lesson_parameter_multiple_statuses=lesson_parameter_statuses,
        lesson_buff_additive_statuses=tuple(additive_statuses),
        play_count_interval_listeners=interval_listeners,
    )


# The native name is SpendTurn; keep a descriptive alias for callers that
# model a single-card run one turn at a time.
spend_plan1_turn = advance_plan1_turn


def _paused_transition(
    card: Plan1CompiledCard,
    state: Plan1ScalarState,
    blockers: Iterable[Plan1Blocker],
) -> Plan1CardTransition:
    return Plan1CardTransition(
        card,
        state,
        state,
        (),
        Plan1TransitionStatus.PAUSED,
        tuple(dict.fromkeys(blockers)),
    )


def _ineligible_transition(
    card: Plan1CompiledCard, state: Plan1ScalarState, reason: str
) -> Plan1CardTransition:
    return Plan1CardTransition(
        card,
        state,
        state,
        (),
        Plan1TransitionStatus.INELIGIBLE,
        ineligible_reason=reason,
    )


def _plan1_effective_buff_cost_value(card: Plan1CompiledCard) -> int:
    """Return an instance-customized buff cost after typed reductions.

    The static Master contract is checked separately by
    :func:`execute_plan1_card`; this helper only applies the already compiled
    instance-owned reducers.  A malformed hand-built modifier is rejected at
    the execution boundary as a runtime blocker rather than being silently
    ignored.
    """

    if card.cost_type not in _PLAN1_BUFF_COST_RESOURCES:
        return card.cost_value
    reduction = 0
    for modifier in card.payment_modifiers:
        if not isinstance(modifier, Plan1PaymentModifier):
            raise ValueError("payment modifier has an invalid type")
        if modifier.cost_type != card.cost_type:
            raise ValueError(
                "payment modifier cost family does not match card cost"
            )
        reduction = _checked_add(
            reduction,
            modifier.reduction,
            card.card_id,
            "customization_cost_reduction",
        )
    return max(card.cost_value - reduction, 0)


def _plan1_stamina_customization_grow_effects(
    card: Plan1CompiledCard,
) -> tuple[StaminaGrowEffect, ...]:
    """Compile ordered CostReduce modifiers for one ordinary stamina target."""

    modifiers = tuple(card.payment_modifiers)
    stamina_modifiers = tuple(
        modifier
        for modifier in modifiers
        if isinstance(modifier, Plan1PaymentModifier)
        and modifier.cost_type == COST_STAMINA
    )
    if not stamina_modifiers:
        if modifiers:
            raise ValueError(
                "payment modifier cost family does not match card cost"
            )
        return ()
    if (
        card.cost_type != COST_STAMINA
        or card.stamina_cost <= 0
        or card.force_stamina_cost != 0
        or len(stamina_modifiers) != len(modifiers)
    ):
        raise ValueError(
            "stamina CostReduce requires one ordinary stamina target"
        )
    effects: list[StaminaGrowEffect] = []
    for modifier in stamina_modifiers:
        if modifier.grow_effect_type != (
            "ProduceCardGrowEffectType_CostReduce"
        ):
            raise ValueError(
                "payment modifier grow family does not match CostReduce"
            )
        effects.append(
            StaminaGrowEffect(
                StaminaGrowEffectType.COST_REDUCE,
                modifier.reduction,
            )
        )
    return tuple(effects)


def execute_plan1_card(
    state: Plan1ScalarState,
    card: Plan1CompiledCard,
    *,
    settings: Plan1NativeSettings | None = None,
    card_draw_resolver: Plan1CardDrawResolver | None = None,
    hand_grave_draw_resolver: Plan1HandGraveDrawResolver | None = None,
    card_move_resolver: Plan1CardMoveResolver | None = None,
    card_upgrade_resolver: Plan1CardUpgradeResolver | None = None,
    card_create_resolver: Plan1CardCreateResolver | None = None,
    effect_timer_resolver: Plan1EffectTimerResolver | None = None,
    before_produce_item_effects: Sequence[Plan1CompiledEffect] = (),
    parameter_buff_status_change_effects: Sequence[Plan1CompiledEffect] = (),
) -> Plan1CardTransition:
    """Apply one ordinary Hand card transaction or return a typed no-op."""

    listener_effects = tuple(before_produce_item_effects)
    if listener_effects:
        listener_runtime = Plan1EquippedItemRuntime(
            item_id=FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
            item_effect_id=FKTN_SSR_PLAN1_BEFORE_ITEM_EFFECT_ID,
            status_enchant_id=FKTN_SSR_PLAN1_BEFORE_ITEM_STATUS_ID,
            trigger_id=FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID,
            phase_type=PHASE_CARD_PLAY,
            target_card_id=FKTN_SSR_PLAN1_UNIQUE_CARD_ID,
            effects=listener_effects,
            remaining_uses=1,
        )
        listener_blockers = validate_fktn_ssr_plan1_equipped_item_runtime(
            listener_runtime
        )
        if listener_blockers:
            return _paused_transition(card, state, listener_blockers)
        if card.card_id != FKTN_SSR_PLAN1_UNIQUE_CARD_ID:
            return _paused_transition(
                card,
                state,
                (
                    _runtime_blocker(
                        "equipped-item-target-card-mismatch",
                        FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
                        card.card_id,
                    ),
                ),
            )
    if any(
        effect.effect_type == EFFECT_HAND_GRAVE_COUNT_CARD_DRAW
        for effect in card.effects
    ) and (
        card.card_id not in PLAN1_HAND_GRAVE_DRAW_CARD_IDS
        or not 0 <= card.upgrade <= 3
    ):
        return _paused_transition(
            card,
            state,
            (
                _runtime_blocker(
                    "card-hand-grave-draw-scope-unsupported",
                    card.card_id,
                    str(card.upgrade),
                ),
            ),
        )
    if card.blockers:
        return _paused_transition(card, state, card.blockers)
    if state.plays_remaining < 1:
        return _ineligible_transition(card, state, "no-playable-value")
    if (
        card.gate is Plan1CardGate.PARAMETER_BUFF_ACTIVE
        and state.parameter_buff_turns < card.parameter_buff_min_turns
    ):
        return _ineligible_transition(card, state, "parameter-buff-inactive")
    if (
        card.gate is Plan1CardGate.LESSON_BUFF_ACTIVE
        and state.lesson_buff < card.lesson_buff_min
    ):
        return _ineligible_transition(card, state, "lesson-buff-inactive")
    if settings is None:
        settings = load_plan1_native_settings()

    force_value_valid = (
        type(card.force_stamina_cost) is int
        and card.force_stamina_cost >= 0
    )
    force_penetrating = (
        force_value_valid and card.force_stamina_cost > 0
    )
    if (
        not force_value_valid
        or (
            force_penetrating
            and (
                card.cost_type != COST_STAMINA
                or card.cost_value != 0
                or card.stamina_cost != 0
            )
        )
    ):
        return _paused_transition(
            card,
            state,
            (
                _runtime_blocker(
                    "card-force-stamina-runtime-domain",
                    card.card_id,
                    str(card.force_stamina_cost),
                ),
            ),
        )

    if card.cost_type == COST_STAMINA:
        try:
            def card_cost():
                return get_stamina_cost(card.force_stamina_cost if force_penetrating else card.stamina_cost,
                    penetrate=force_penetrating, master_grow=_plan1_stamina_customization_grow_effects(card),
                    runtime_grow=(), temporary_specify=())
            search_runtime = state.search_play_card_stamina_runtime
            if search_runtime.statuses:
                if not card.instance_guid:
                    raise NativeFormulaDomainError("search stamina payment requires the current card instance GUID")
                from .plan3_native_state import Plan3NativeCard
                identity = Plan3NativeCard(card.instance_guid, card.card_id, card.upgrade, 0, card.upgrade)
                search_payment = resolve_plan2_search_play_card_stamina_payment(search_runtime, identity, card_cost)
                raw_cost = search_payment.selected_stamina_cost
                search_runtime = search_payment.runtime_after
            else:
                raw_cost = card_cost()
            effective_cost = calculate_effective_stamina_cost(
                raw_cost,
                status=StaminaPaymentStatus(
                    consumption_down=(state.stamina_consumption_down_turns != 0),
                    consumption_add=(state.stamina_consumption_add_turns != 0),
                    consumption_down_fix=state.stamina_consumption_down_fixed,
                ),
                settings=settings.stamina_payment,
            )
            stamina_loss = max(
                effective_cost
                - (0 if force_penetrating else state.block),
                0,
            )
            if stamina_loss > state.stamina:
                return _ineligible_transition(
                    card,
                    state,
                    (
                        "insufficient-force-stamina"
                        if force_penetrating
                        else "insufficient-stamina"
                    ),
                )
            payment = split_stamina_payment(
                effective_cost,
                penetrate=force_penetrating,
                current_stamina=state.stamina,
                max_stamina=state.max_stamina,
                current_block=state.block,
            )
        except (NativeFormulaDomainError, ValueError, OverflowError) as error:
            return _paused_transition(
                card,
                state,
                (_runtime_blocker("card-cost-runtime-domain", card.card_id, str(error)),),
            )

        after_cost = replace(
            state,
            stamina=payment.new_stamina,
            block=payment.new_block,
            search_play_card_stamina_runtime=search_runtime,
        )
        cost_trace = Plan1TraceEntry(
            0,
            Plan1TraceStage.COST,
            card.card_id,
            state,
            after_cost,
            requested=raw_cost,
            actual=payment.stamina_loss,
        )
    elif card.cost_type in _PLAN1_BUFF_COST_RESOURCES:
        if not _supported_plan1_cost(card.cost_type, card.cost_value):
            return _paused_transition(
                card,
                state,
                (
                    _runtime_blocker(
                        "card-cost-contract-unsupported",
                        card.card_id,
                        f"{card.cost_type}:{card.cost_value}",
                    ),
                ),
            )
        try:
            customized_cost = _plan1_effective_buff_cost_value(card)
            effective_cost = calculate_buff_cost(
                customized_cost,
                consumption_down=state.buff_consumption_down,
                consumption_down_permille=settings.buff_consumption_down_permille,
                consumption_add=state.buff_consumption_add,
                consumption_add_permille=settings.buff_consumption_add_permille,
            )
        except (NativeFormulaDomainError, ValueError, OverflowError) as error:
            return _paused_transition(
                card,
                state,
                (_runtime_blocker("card-cost-runtime-domain", card.card_id, str(error)),),
            )
        resource_name = _PLAN1_BUFF_COST_RESOURCES[card.cost_type]
        available = getattr(state, resource_name)
        if effective_cost > available:
            return _ineligible_transition(
                card,
                state,
                f"insufficient-{resource_name.replace('_', '-')}",
            )
        after_cost = replace(
            state,
            **{resource_name: available - effective_cost},
        )
        cost_trace = Plan1TraceEntry(
            0,
            Plan1TraceStage.COST,
            card.card_id,
            state,
            after_cost,
            requested=customized_cost,
            actual=effective_cost,
        )
    else:
        return _paused_transition(
            card,
            state,
            (
                _runtime_blocker(
                    "card-cost-type-runtime-unsupported",
                    card.card_id,
                    card.cost_type,
                ),
            ),
        )

    listener_execution = execute_plan1_effects(
        after_cost,
        listener_effects,
        settings=settings,
        card_move_resolver=card_move_resolver,
        card_upgrade_resolver=card_upgrade_resolver,
        effect_trigger_state=state,
        parameter_buff_status_change_effects=parameter_buff_status_change_effects,
    )
    if listener_execution.blockers:
        return _paused_transition(card, state, listener_execution.blockers)
    effect_execution = execute_plan1_effects(
        listener_execution.after,
        card.effects,
        settings=settings,
        card_draw_resolver=card_draw_resolver,
        hand_grave_draw_resolver=hand_grave_draw_resolver,
        card_move_resolver=card_move_resolver,
        card_upgrade_resolver=card_upgrade_resolver,
        card_create_resolver=card_create_resolver,
        effect_timer_resolver=effect_timer_resolver,
        effect_trigger_state=state,
        parameter_buff_status_change_effects=parameter_buff_status_change_effects,
    )
    if effect_execution.blockers:
        return _paused_transition(card, state, effect_execution.blockers)

    before_move = effect_execution.after
    try:
        after_move = replace(
            before_move,
            plays_remaining=before_move.plays_remaining - 1,
            play_count=_checked_add(
                before_move.play_count, 1, card.card_id, "play_count"
            ),
        )
    except (NativeFormulaDomainError, ValueError) as error:
        return _paused_transition(
            card,
            state,
            (_runtime_blocker("move-play-card-runtime-domain", card.card_id, str(error)),),
        )
    combined_effect_trace = (*listener_execution.trace, *effect_execution.trace)
    payment_trace_count = 1
    effect_trace = tuple(
        replace(entry, ordinal=index + payment_trace_count)
        for index, entry in enumerate(combined_effect_trace)
    )
    move_trace = Plan1TraceEntry(
        len(effect_trace) + payment_trace_count,
        Plan1TraceStage.MOVE_PLAY_CARD,
        card.card_id,
        before_move,
        after_move,
        requested=1,
        actual=1,
    )
    return Plan1CardTransition(
        card,
        state,
        after_move,
        (
            cost_trace,
            *effect_trace,
            move_trace,
        ),
        Plan1TransitionStatus.APPLIED,
    )


__all__ = [
    "DEFAULT_EXAM_SETTING_ID",
    "DEFAULT_MASTER_DIR",
    "EFFECT_ANTI_DEBUFF",
    "EFFECT_BLOCK",
    "EFFECT_BLOCK_FIX",
    "EFFECT_CARD_DRAW",
    "EFFECT_CARD_MOVE",
    "EFFECT_CARD_CREATE_ID",
    "EFFECT_HAND_GRAVE_COUNT_CARD_DRAW",
    "EFFECT_TIMER",
    "EFFECT_LESSON",
    "EFFECT_LESSON_ADD_MULTIPLE_PARAMETER_BUFF",
    "EFFECT_LESSON_BUFF",
    "EFFECT_LESSON_BUFF_ADDITIVE",
    "EFFECT_LESSON_VALUE_MULTIPLE",
    "EFFECT_PARAMETER_BUFF",
    "EFFECT_PARAMETER_BUFF_MULTIPLE_PER_TURN",
    "EFFECT_PLAYABLE_VALUE_ADD",
    "EFFECT_STAMINA_RECOVER_FIX",
    "EFFECT_STAMINA_REDUCE_FIX",
    "EFFECT_SEARCH_STAMINA_CHANGE",
    "EFFECT_STAMINA_RECOVER_MULTIPLE",
    "EFFECT_STATUS_ENCHANT",
    "EFFECT_STAMINA_CONSUMPTION_DOWN",
    "EFFECT_STAMINA_CONSUMPTION_ADD",
    "EFFECT_STAMINA_CONSUMPTION_DOWN_FIX",
    "EFFECT_MULTIPLE_LESSON_BUFF_LESSON",
    "EFFECT_LESSON_DEPEND_PARAMETER_BUFF",
    "COST_STAMINA",
    "COST_LESSON_BUFF",
    "COST_PARAMETER_BUFF",
    "COST_PARAMETER_BUFF_MULTIPLE_PER_TURN",
    "FKTN_SSR_PLAN1_BEFORE_ITEM_ID",
    "FKTN_SSR_PLAN1_BEFORE_ITEM_EFFECT_ID",
    "FKTN_SSR_PLAN1_BEFORE_ITEM_NESTED_EFFECT_IDS",
    "FKTN_SSR_PLAN1_BEFORE_ITEM_SEARCH_ID",
    "FKTN_SSR_PLAN1_BEFORE_ITEM_STATUS_ID",
    "FKTN_SSR_PLAN1_BEFORE_ITEM_TRIGGER_ID",
    "FKTN_SSR_PLAN1_CHARACTER_CARD_REFS",
    "FKTN_SSR_PLAN1_IDOL_CARD_ID",
    "FKTN_SSR_PLAN1_IDOL_FRAGMENT_ID",
    "FKTN_SSR_PLAN1_MODE_CARD_REFS",
    "FKTN_SSR_PLAN1_MODE_DECK_ID",
    "FKTN_SSR_PLAN1_UNIQUE_CARD_ID",
    "INITIAL_REGULAR_PRODUCE_ID",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "PLAN1_TIMER_CHILD_EFFECT_TYPES",
    "PLAN1_TIMER_STAMINA_TRIGGER_ID",
    "PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_TRIGGER_ID",
    "PLAN1_CARD_PLAY_STAMINA_UP_MULTIPLE_500_CARD_ID",
    "PLAN1_CARD_PLAY_PARAMETER_BUFF_TRIGGER_ID",
    "PLAN1_CARD_PLAY_PARAMETER_BUFF_GATE_CARD_ID",
    "PLAN1_CARD_PLAY_LESSON_BUFF_GATE_TRIGGER_ID",
    "PLAN1_CARD_PLAY_LESSON_BUFF_GATE_CARD_ID",
    "PLAN1_CARD_PLAY_LESSON_BUFF_UP_3_TRIGGER_ID",
    "PLAN1_CARD_PLAY_LESSON_BUFF_UP_6_TRIGGER_ID",
    "PLAN1_CARD_PLAY_STAMINA_LESS_MULTIPLE_500_TRIGGER_ID",
    "PLAN1_EXACT_EFFECT_TRIGGER_IDS",
    "PLAN1_HAND_GRAVE_DRAW_CARD_IDS",
    "Plan1Blocker",
    "Plan1CardDrawResolution",
    "Plan1CardDrawResolver",
    "Plan1CardMoveResolution",
    "Plan1CardMoveResolver",
    "Plan1CardCreateResolution",
    "Plan1CardCreateResolver",
    "Plan1CardGate",
    "Plan1CardTransition",
    "Plan1CompiledCard",
    "Plan1CompiledEffect",
    "Plan1DeckCompilation",
    "Plan1DeckEntry",
    "Plan1DeckRole",
    "Plan1EffectExecution",
    "Plan1EffectTimerResolution",
    "Plan1EffectTimerResolver",
    "Plan1EquippedItemRuntime",
    "Plan1HandGraveDrawResolution",
    "Plan1HandGraveDrawResolver",
    "Plan1LessonBuffAdditiveStatus",
    "Plan1PlayCountIntervalListenerStatus",
    "Plan1InitialDeckManifest",
    "Plan1NativeContractError",
    "Plan1NativeSettings",
    "Plan1PaymentModifier",
    "Plan1EffectModifier",
    "Plan1CardPaymentModifier",
    "Plan1CardEffectModifier",
    "Plan1RequiredItem",
    "Plan1ScalarState",
    "Plan1TraceEntry",
    "Plan1TraceStage",
    "Plan1TransitionStatus",
    "compile_fktn_ssr_plan1_initial_deck",
    "compile_fktn_ssr_plan1_equipped_item_runtime",
    "compile_plan1_card_customization",
    "compile_plan1_card_for_instance",
    "compile_plan1_card_from_local_save",
    "compile_plan1_card_instance",
    "compile_plan1_card",
    "compile_plan1_effect",
    "advance_plan1_turn",
    "execute_plan1_card",
    "execute_plan1_effects",
    "load_fktn_ssr_plan1_manifest",
    "load_plan1_native_settings",
    "parse_plan1_runtime_customization_ordered",
    "spend_plan1_turn",
    "validate_fktn_ssr_plan1_equipped_item_runtime",
]
