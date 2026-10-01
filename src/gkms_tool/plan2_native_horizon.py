"""Fail-closed, agent-free Plan2 horizon over native ordered card zones.

This module composes the small exact Plan2 scalar kernel with the shared
GUID/order/RNG zone primitive.  It is deliberately a harness, not a claim
that every current-Master card transaction is compiled: callers supply an
explicit :class:`Plan2NativeCardProgram` for each supported card version and
unknown versions fail at the action boundary.

The coverage gate is independent from fixture execution.  A supported
fixture may therefore run to terminal while ``all_plan2_ready`` remains
false until the formal Plan2 coverage artifact reports 709/709.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Callable, Mapping, Sequence, TypeAlias

import yaml

from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    EXAM_SAVE_DATA_SOURCE_TYPE,
    AuditionLocalSaveStateEvidence,
    LocalSaveExamCard,
    LocalSaveExamState,
    LocalSaveExamZones,
    empty_local_save_exam_card_runtime_state,
    empty_local_save_exam_root_runtime_state,
)
from .audition_native_ordered_zones import (
    NativeEndTurnDisposition,
    NativeOrderedCardInstance,
    NativeOrderedZoneError,
    NativeOrderedZoneState,
    _runtime_state_digest,
    build_native_ordered_zone_state,
    canonical_runtime_digest_aggregate,
)
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .card_play_count_receipt import (
    CARD_PLAY_COUNT_BUILD_OWNER,
    CARD_PLAY_COUNT_INCREMENT_OPERATION,
    CARD_PLAY_COUNT_MOVE_OWNER,
    CARD_PLAY_COUNT_RECEIPT_FAMILY,
    PLAN2_CARD_PLAY_COUNT_RECEIPT_OWNERS,
    CardPlayCountOperationReceipt,
)
from .exam_native_rng import shuffle_unfixed_pool
from .native_exam_formula import (
    INT32_MAX,
    INT64_MAX,
    AddBlockSettings,
    AddBlockStatus,
    AddingParameterSettings,
    AddingParameterStatus,
    ParameterApplicationStatus,
    StaminaGrowEffect,
    StaminaGrowEffectType,
    calculate_add_block,
    f32,
    permille_to_f32,
    get_stamina_cost,
    get_ratio_effect_int_value,
)
from .item_rules import load_item_rule
from .master_db import DEFAULT_DATABASE as DEFAULT_MASTER_DATABASE
from .logic_engine import (
    COST_STAMINA,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_STAMINA_REDUCE_FIX,
    _consume_review_scalar,
)
from .plan2_native_item_runtime import (
    EFFECT_AGGRESSIVE as ITEM_EFFECT_AGGRESSIVE,
    EFFECT_BLOCK as ITEM_EFFECT_BLOCK,
    EFFECT_LESSON_DEPEND_BLOCK as ITEM_EFFECT_LESSON_DEPEND_BLOCK,
    EFFECT_REVIEW_REDUCE as ITEM_EFFECT_REVIEW_REDUCE,
    EFFECT_CARD_DRAW as ITEM_EFFECT_CARD_DRAW,
    EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE as ITEM_EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE,
    EFFECT_BLOCK_DEPEND_REVIEW as ITEM_EFFECT_BLOCK_DEPEND_REVIEW,
    EFFECT_BLOCK_FIX as ITEM_EFFECT_BLOCK_FIX,
    EFFECT_REVIEW as ITEM_EFFECT_REVIEW,
    EFFECT_REVIEW_MULTIPLE as ITEM_EFFECT_REVIEW_MULTIPLE,
    EFFECT_STAMINA_REDUCE as ITEM_EFFECT_STAMINA_REDUCE,
    EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE as ITEM_EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE,
    EFFECT_STAMINA_CONSUMPTION_DOWN as ITEM_EFFECT_STAMINA_CONSUMPTION_DOWN,
    EFFECT_LESSON_MULTIPLE as ITEM_EFFECT_LESSON_MULTIPLE,
    EFFECT_LESSON_MULTIPLE_DOWN as ITEM_EFFECT_LESSON_MULTIPLE_DOWN,
    EFFECT_TIMER as ITEM_EFFECT_TIMER,
    ITEM_PHASE_CARD_PLAY,
    ITEM_PHASE_CARD_PLAY_AFTER,
    ITEM_PHASE_END_TURN,
    ITEM_PHASE_PLAY_TURN_COUNT_INTERVAL,
    ITEM_PHASE_TURN_INTERVAL,
    ITEM_PHASE_TURN_START,
    Plan2NativeItemEvent,
    Plan2NativeItemRuntime,
    _has_legacy_runtime_shape,
    advance_plan2_native_item_turn_start,
    compile_plan2_native_item_rules,
    dispatch_plan2_native_item_event,
)
from .plan2_native_item_scalar_execution import (
    AGGRESSIVE_ADDITIVE as SHARED_ITEM_AGGRESSIVE_ADDITIVE,
    SHARED_ITEM_SCALAR_EFFECT_TYPES, ItemScalarContext, execute_item_scalar_effect,
)
from .plan2_card_play_trigger import (
    Plan2PlayingCard,
    simulate_plan2_exam_card_play,
)
from .plan2_core_runtime import (
    apply_plan2_stamina_recover_fix,
    complete_plan2_card_play_after_move,
)
from .plan2_end_turn_trigger import (
    Plan2EndTurnProgram,
    TURN_CHECK_REVIEW_SOURCE,
    simulate_install_plan2_end_turn_listener,
    simulate_plan2_exam_end_turn,
)
from .plan2_end_turn_remaining_three import (
    REMAINING_THREE_TRIGGER_ID,
    evaluate_plan2_end_turn_remaining_three_before_decrement,
    evaluate_plan2_end_turn_remaining_three_child,
)
from .plan2_review_multiple import (
    Plan2ReviewMultipleEffect,
    Plan2ScoreApplication,
    simulate_end_turn_review_score,
    simulate_review_multiple,
)
from .plan2_native_catalog_stamina import (
    Plan2CostResources,
    Plan2NativeCardCost,
    Plan2NativeCostRequest,
    Plan2StaminaModifierRuntime,
    PlayOrigin,
    StaminaConsumptionDownEffect,
    advance_plan2_stamina_turn_start,
    apply_plan2_stamina_modifier,
    pay_plan2_native_cost,
)
from .plan2_native_catalog_status_enchant import (
    EFFECT_LESSON_DEPEND_AGGRESSIVE,
    PHASE_CARD_PLAY,
    PHASE_CARD_PLAY_AFTER,
    PHASE_END_TURN as STATUS_PHASE_END_TURN,
    PHASE_PLAY_COUNT_INTERVAL,
    PHASE_START_TURN as STATUS_PHASE_START_TURN,
    PHASE_STAMINA_REDUCE_CARD,
    PHASE_STATUS_CHANGE,
    PHASE_TURN_TIMER,
    POSITION_PLAYING,
    POSITION_TARGET,
    Plan2NativeStatusEnchantEvent,
    Plan2NativeStatusEnchantRuntime,
    Plan2StatusEnchantChildProgram,
    Plan2StatusEnchantChildCommand,
    resolve_plan2_native_status_enchant_event,
)
from .plan2_native_catalog_lesson_depend_aggressive import (
    LessonDependAggressiveEffect,
    LessonDependAggressiveRuntime,
    build_plan2_native_lesson_depend_aggressive_effect_handoff,
    execute_plan2_native_lesson_depend_aggressive,
)
from .plan2_native_catalog_status_children import (
    Plan2NativeStatusChildCompilation,
    Plan2NativeStatusChildInput,
    Plan2NativeStatusChildState,
    execute_plan2_native_status_child_handoff,
)
from .plan2_block_depend_block_consumption_sum import (
    Plan2BlockDependConsumptionSumRuntime,
)
from .plan2_native_catalog_block_dynamic import Plan2NativeDynamicBlockRuntime
from .plan2_review_count_add_interval3 import Plan2ReviewCountAddInterval3State
from .plan2_native_catalog_review_dynamic import (
    LAYER_LESSON_MULTIPLE_DOWN,
    OP_INSTALL_LESSON_MULTIPLE,
    Plan2NativeReviewDynamicInstallInput,
    Plan2NativeReviewDynamicLayer,
    Plan2NativeReviewDynamicLessonRequest,
    Plan2NativeReviewDynamicProgram,
    Plan2NativeReviewDynamicRuntime,
    apply_plan2_native_review_dynamic_review_gain,
    handoff_plan2_native_review_dynamic_lesson,
    install_plan2_native_review_dynamic,
    start_plan2_native_review_dynamic_turn,
)
from .plan2_native_catalog_debuff import (
    Plan2NativeDebuffRegistry,
    advance_plan2_native_debuff_turn_start,
    gate_plan2_native_status_addition,
)
from .plan2_native_catalog_effect_chains import (
    EXECUTOR_BY_TYPE as EFFECT_CHAIN_EXECUTOR_BY_TYPE,
    EXECUTOR_CARD_MOVE as EFFECT_CHAIN_CARD_MOVE,
    EXECUTOR_LESSON_BLOCK as EFFECT_CHAIN_LESSON_BLOCK,
    EXECUTOR_LESSON_REVIEW as EFFECT_CHAIN_LESSON_REVIEW,
    EXECUTOR_LESSON_MULTIPLE as EFFECT_CHAIN_LESSON_MULTIPLE,
    Plan2NativeEffectChainInstallInput,
    Plan2NativeEffectChainProgram,
    Plan2NativeEffectChainChildInput,
    Plan2NativeEffectChainExecutionState,
    Plan2NativeEffectChainRuntime,
    Plan2NativeEffectChainStartTurnInput,
    execute_plan2_native_effect_chain_card_move,
    execute_plan2_native_effect_chain_child,
    install_plan2_native_effect_chain,
    prepare_plan2_native_effect_chain_start_turn_execution,
    start_plan2_native_effect_chain_turn,
    upgrade_plan2_native_hand_all_zones,
)
from .plan2_card_search_play_count_buff import (
    PlayCountBuffRuntime,
    PlayCountBuffStatus,
    advance_play_count_buff_turn_start,
)
from .plan2_native_catalog_generated_cards import (
    GeneratedHandMoveProgram,
    GeneratedHandMoveTurnState,
    execute_plan2_generated_hand_move_aggressive,
    execute_plan2_generated_hand_move_block,
    start_plan2_generated_hand_move_turn,
)
from .plan2_lesson_depend_block_consumption_final import FinalCardRuntime
from .plan2_native_catalog_lesson_depend_block_retention import (
    _native_ceil_ratio as native_lesson_block_ratio,
    _native_retained_block as native_lesson_retained_block,
)
from .plan2_search_play_card_stamina_consumption_change import (
    SearchPlayCardStaminaRuntime,
    advance_plan2_search_play_card_stamina_turn,
    resolve_plan2_search_play_card_stamina_payment,
)
from .plan2_add_grow_effect import Plan2AddGrowDeckAllState
from .plan2_move_card_play_aggressive import (
    HandMoveAggressiveRuntimeState,
    HandMoveCommitEvent,
)
from .plan2_card_move_remaining import (
    RemainingCardMoveExecutionContext,
    RemainingCardMoveState,
)
from .plan2_native_catalog_status_enchant_encore import (
    AGGRESSIVE_EFFECT_TYPE as ENCORE_AGGRESSIVE_EFFECT_TYPE,
    PHASE_AGGRESSIVE_UP_INTERVAL as ENCORE_PHASE_AGGRESSIVE_UP_INTERVAL,
    PHASE_CARD_PLAY_AFTER as ENCORE_PHASE_CARD_PLAY_AFTER,
    Plan2NativeStatusEnchantEncoreEvent,
    Plan2NativeStatusEnchantEncorePlayReceipt,
    Plan2NativeStatusEnchantEncoreRuntime,
    plan_plan2_native_status_enchant_encore,
    record_plan2_native_status_enchant_encore,
    stage_plan2_native_status_enchant_encore,
    start_plan2_native_status_enchant_encore_turn,
)
from .plan2_native_catalog_aggressive_additive import (
    Plan2AggressiveAdditiveRuntime,
    apply_plan2_native_bound_aggressive_gain,
    advance_plan2_native_aggressive_additive_turn,
)
from .plan2_native_catalog_block_fix_restriction import (
    Plan2NativeBlockFixRestrictionRuntime,
    advance_plan2_native_block_fix_restriction_turn_start,
)
from .plan2_native_catalog_extra_turn import (
    Plan2NativeExtraTurnRuntime,
    advance_plan2_native_extra_turn_end_turn,
    advance_plan2_native_extra_turn_start_turn,
)
from .plan2_native_catalog_status_enchant_triggered import (
    Plan2TriggeredReviewStatusRuntime,
    start_plan2_triggered_review_status_turn,
)
from .plan2_start_play_trigger import (
    CARD_DRAW_EFFECT_TYPE as START_PLAY_CARD_DRAW_EFFECT_TYPE,
    Plan2StartPlayListener,
    Plan2StartPlayStatusProgram,
    advance_plan2_start_play_listener_turn,
    capture_plan2_start_play,
    simulate_install_plan2_start_play_listener,
)
from .plan2_state import (
    Plan2State,
    apply_plan2_state_score,
    carry_plan2_state_application_status,
    simulate_plan2_turn_start,
)
from .plan2_stamina_recover_fix import StaminaRecoverFixContract
from .plan2_exam_save_battle_scoring import (
    Plan2BattleScoringError,
    Plan2ScoreIntegrationPoint,
    Plan2ScoreKind,
    close_plan2_exam_save_battle_turn,
    plan2_battle_scoring_context_from_exam_save,
)
from .plan2_exam_mode import (
    DEFAULT_PLAN2_AUDITION_EXAM_MODE,
    Plan2ExamMode,
    Plan2ExamModeError,
    Plan2ExamOutcome,
    Plan2ExamTerminalReason,
    Plan2ScheduledGimmick,
    Plan2ScheduledGimmickHook,
    Plan2ScheduledGimmickHookKey,
    Plan2ScheduledGimmickHookResult,
    compile_plan2_scheduled_gimmicks,
    evaluate_plan2_exam_outcome,
    resolve_plan2_exam_mode,
)
from .exam_context import CurrentTurnBoundary
from .plan3_engine import DEFAULT_MASTER_DIR
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeRuntimeCustomization,
    Plan3NativeState,
    Plan3NativeStateError,
    parse_plan2_native_runtime_customization,
)
from .plan3_card_create_search import (
    CardCreateSearchChanceInput,
    CardCreateSearchEffectContract,
    apply_card_create_search,
)
from .plan2_drink_force_play_random_pool import (
    Plan2DrinkForcePlayRandomPoolError,
    Plan2DrinkForcePlayRandomPoolProgram,
    select_plan2_drink_force_play_random_pool_card,
)
from .plan2_stamina_consumption_add import StaminaConsumptionAddEffect

if TYPE_CHECKING:
    from .plan2_native_hand_add_support import Plan2NativeHandAddSupportCatalog


PLAN2_NATIVE_HORIZON_SCHEMA_VERSION = 1
PLAN2_COVERAGE_TARGET = 709
PLAN2_NATIVE_COVERAGE_ARTIFACT = Path("var/coverage/plan2_card_executable_coverage.json")

PHASE_MAIN = "main"
PHASE_TERMINAL = "terminal"
ACTION_END_TURN = "END_TURN"

CARD_PLAY_COUNT_RECEIPT_OWNERS = PLAN2_CARD_PLAY_COUNT_RECEIPT_OWNERS

DRINK_HANDLER_LESSON = "lesson"
DRINK_HANDLER_LESSON_DEPEND_REVIEW = "lesson_depend_review"
DRINK_HANDLER_REVIEW = "review"
DRINK_HANDLER_BLOCK = "block"
DRINK_HANDLER_AGGRESSIVE = "aggressive"
DRINK_HANDLER_PLAYABLE_ADD = "playable_add"
DRINK_HANDLER_STAMINA_RECOVER_FIX = "stamina_recover_fix"
DRINK_HANDLER_CARD_UPGRADE_HAND_ALL = "card_upgrade_hand_all"
DRINK_HANDLER_END_TURN_STATUS = "end_turn_status"
DRINK_HANDLER_HAND_GRAVE_DRAW = "hand_grave_draw"
DRINK_HANDLER_LESSON_VALUE_MULTIPLE = "lesson_value_multiple"
DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN = "stamina_consumption_down"
DRINK_HANDLER_STAMINA_CONSUMPTION_ADD = "stamina_consumption_add"
DRINK_HANDLER_STAMINA_REDUCE_FIX = "stamina_reduce_fix"
DRINK_HANDLER_CARD_DRAW = "card_draw"
DRINK_HANDLER_START_PLAY_STATUS = "start_play_status"
DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND = "card_move_idol_unique_hand"
DRINK_HANDLER_CARD_CREATE_SEARCH = "card_create_search"
DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL = "force_play_random_pool"
DRINK_HANDLER_PLAY_COUNT_BUFF = "play_count_buff"

_DRINK_HANDLER_EFFECT_TYPES = {
    DRINK_HANDLER_LESSON: "ProduceExamEffectType_ExamLesson",
    DRINK_HANDLER_LESSON_DEPEND_REVIEW: (
        "ProduceExamEffectType_ExamLessonDependExamReview"
    ),
    DRINK_HANDLER_REVIEW: "ProduceExamEffectType_ExamReview",
    DRINK_HANDLER_BLOCK: "ProduceExamEffectType_ExamBlock",
    DRINK_HANDLER_AGGRESSIVE: (
        "ProduceExamEffectType_ExamCardPlayAggressive"
    ),
    DRINK_HANDLER_PLAYABLE_ADD: (
        "ProduceExamEffectType_ExamPlayableValueAdd"
    ),
    DRINK_HANDLER_STAMINA_RECOVER_FIX: (
        "ProduceExamEffectType_ExamStaminaRecoverFix"
    ),
    DRINK_HANDLER_CARD_UPGRADE_HAND_ALL: (
        "ProduceExamEffectType_ExamCardUpgrade"
    ),
    DRINK_HANDLER_END_TURN_STATUS: (
        "ProduceExamEffectType_ExamStatusEnchant"
    ),
    DRINK_HANDLER_HAND_GRAVE_DRAW: (
        "ProduceExamEffectType_ExamHandGraveCountCardDraw"
    ),
    DRINK_HANDLER_LESSON_VALUE_MULTIPLE: (
        "ProduceExamEffectType_ExamLessonValueMultiple"
    ),
    DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN: (
        "ProduceExamEffectType_ExamStaminaConsumptionDown"
    ),
    DRINK_HANDLER_STAMINA_CONSUMPTION_ADD: (
        "ProduceExamEffectType_ExamStaminaConsumptionAdd"
    ),
    DRINK_HANDLER_STAMINA_REDUCE_FIX: (
        "ProduceExamEffectType_ExamStaminaReduceFix"
    ),
    DRINK_HANDLER_CARD_DRAW: "ProduceExamEffectType_ExamCardDraw",
    DRINK_HANDLER_START_PLAY_STATUS: (
        "ProduceExamEffectType_ExamStatusEnchant"
    ),
    DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND: (
        "ProduceExamEffectType_ExamCardMove"
    ),
    DRINK_HANDLER_CARD_CREATE_SEARCH: (
        "ProduceExamEffectType_ExamCardCreateSearch"
    ),
    DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL: (
        "ProduceExamEffectType_ExamForcePlayCardSearch"
    ),
    DRINK_HANDLER_PLAY_COUNT_BUFF: (
        "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff"
    ),
}

_COST_TYPES = frozenset({"none", "stamina", "native"})
_MOVE_DESTINATIONS = frozenset({"grave", "lost"})
_TIMER_PHASES = frozenset({"start_turn", "end_turn"})
_COMMAND_KINDS = frozenset(
    {
        "add_review",
        "add_block",
        "add_aggressive",
        "add_playable",
        "draw",
        "extra_turn",
        "force_play",
    }
)
_PLAN2_LOCAL_SAVE_VALUE = 3
_REVIEW_STATUS = re.compile(r"^Review:\((?P<value>\d+)\)$")
_AGGRESSIVE_STATUS = re.compile(r"^Aggressive:\((?P<value>\d+)\)$")


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _i32_add(value: int, delta: int, label: str) -> int:
    result = value + delta
    if not 0 <= result <= INT32_MAX:
        raise OverflowError(f"{label} result is outside non-negative Int32")
    return result


def _sync_review_dynamic(
    runtime: Plan2NativeReviewDynamicRuntime,
    review: int,
) -> Plan2NativeReviewDynamicRuntime:
    """Mirror the scalar Review value while preserving known lifecycle state."""

    _plain_int(review, "review")
    if runtime.review == review:
        return runtime
    if review == 0:
        return replace(
            runtime,
            review=0,
            review_status_present=False,
            review_passing_turn_start=False,
        )
    return replace(
        runtime,
        review=review,
        review_status_present=True,
        review_passing_turn_start=(
            runtime.review_passing_turn_start
            if runtime.review_status_present
            else False
        ),
    )


def _plan3_card_from_ordered(
    card: NativeOrderedCardInstance,
    *,
    project_runtime_customization: bool = True,
) -> Plan3NativeCard:
    empty = empty_local_save_exam_card_runtime_state()
    runtime = card.runtime_state
    if replace(
        runtime,
        play_count=empty.play_count,
        is_move_produce_exam_effect_use_in_turn=(
            empty.is_move_produce_exam_effect_use_in_turn
        ),
        customize_count_list=empty.customize_count_list,
    ) != empty:
        raise Plan2NativeHorizonError(
            "encore-card-runtime-projection-unbound",
            card.guid,
        )
    customization = Plan3NativeRuntimeCustomization((), ())
    if project_runtime_customization:
        try:
            customization = parse_plan2_native_runtime_customization(
                card.card_id,
                card.effective_upgrade,
                runtime,
            )
        except Plan3NativeStateError as error:
            detail = f"{card.guid}:{error.code}"
            if error.detail:
                detail = f"{detail}:{error.detail}"
            raise Plan2NativeHorizonError(
                "encore-card-runtime-projection-unbound",
                detail,
            ) from error
    return Plan3NativeCard(
        guid=card.guid,
        card_id=card.card_id,
        base_upgrade=card.base_upgrade,
        temporary_upgrade=card.temporary_upgrade,
        effective_upgrade=card.effective_upgrade,
        support_upgrade_ids=card.support_upgrade_ids,
        fixed_deck_order=card.fixed_deck_order,
        play_count=runtime.play_count,
        runtime_customize_ids=customization.customize_ids,
        plan2_runtime_customization=customization,
        runtime_customize_lesson_add=customization.lesson_add,
        runtime_customize_lesson_depend_review_add=(
            customization.lesson_depend_review_add
        ),
        runtime_lesson_count_add=customization.lesson_count_add,
        move_effect_used_in_turn=(
            runtime.is_move_produce_exam_effect_use_in_turn
        ),
    )


def _plan3_state_from_ordered(zones: NativeOrderedZoneState) -> Plan3NativeState:
    if zones.pending_played is not None:
        raise Plan2NativeHorizonError("encore-state-requires-settled-zones")
    return Plan3NativeState(
        hand=tuple(_plan3_card_from_ordered(value) for value in zones.hand),
        deck=tuple(_plan3_card_from_ordered(value) for value in zones.deck),
        grave=tuple(_plan3_card_from_ordered(value) for value in zones.grave),
        lost=tuple(_plan3_card_from_ordered(value) for value in zones.lost),
        random_state=zones.random_state,
        turn_used_support_ids=tuple(
            dict.fromkeys(
                support
                for value in zones.hand
                for support in value.support_upgrade_ids
            )
        ),
    )


def _plan3_state_for_status_children(
    zones: NativeOrderedZoneState,
    *,
    total_effect_draw_card_count: int,
) -> Plan3NativeState:
    """Project stable zones while retaining the caller-owned Playing card."""

    return Plan3NativeState(
        hand=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.hand
        ),
        deck=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.deck
        ),
        grave=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.grave
        ),
        lost=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.lost
        ),
        random_state=zones.random_state,
        turn_used_support_ids=tuple(
            dict.fromkeys(
                support
                for value in zones.hand
                for support in value.support_upgrade_ids
            )
        ),
        total_effect_draw_card_count=total_effect_draw_card_count,
    )


def _ordered_card_from_plan3(
    card: Plan3NativeCard,
    current: NativeOrderedCardInstance | None,
) -> NativeOrderedCardInstance:
    runtime = (
        empty_local_save_exam_card_runtime_state()
        if current is None
        else current.runtime_state
    )
    runtime = replace(
        runtime,
        play_count=card.play_count,
        is_move_produce_exam_effect_use_in_turn=card.move_effect_used_in_turn,
    )
    digest = _runtime_state_digest(
        base_upgrade=card.base_upgrade,
        temporary_upgrade=card.temporary_upgrade,
        effective_upgrade=card.effective_upgrade,
        support_upgrade_ids=card.support_upgrade_ids,
        fixed_deck_order=card.fixed_deck_order,
        runtime_state=runtime,
    )
    if current is None:
        return NativeOrderedCardInstance(
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            support_upgrade_ids=card.support_upgrade_ids,
            fixed_deck_order=card.fixed_deck_order,
            runtime_state=runtime,
            runtime_state_digest=digest,
        )
    if (current.card_id, current.base_upgrade) != (card.card_id, card.base_upgrade):
        raise Plan2NativeHorizonError(
            "status-child-card-identity-drift",
            card.guid,
        )
    return replace(
        current,
        temporary_upgrade=card.temporary_upgrade,
        effective_upgrade=card.effective_upgrade,
        support_upgrade_ids=card.support_upgrade_ids,
        fixed_deck_order=card.fixed_deck_order,
        runtime_state=runtime,
        runtime_state_digest=digest,
    )


def _ordered_after_status_native(
    zones: NativeOrderedZoneState,
    native: Plan3NativeState,
) -> NativeOrderedZoneState:
    current = {value.guid: value for value in zones.card_universe}

    def restore(values: Sequence[Plan3NativeCard]) -> tuple[NativeOrderedCardInstance, ...]:
        return tuple(
            _ordered_card_from_plan3(value, current.get(value.guid)) for value in values
        )

    hand = restore(native.hand)
    deck = restore(native.deck)
    grave = restore(native.grave)
    lost = restore(native.lost)
    if native.hold:
        raise Plan2NativeHorizonError("status-child-hold-zone-unbound")
    pending = zones.pending_played
    stable = (*hand, *deck, *grave, *lost)
    universe = tuple(
        sorted((*stable, *((pending,) if pending is not None else ())), key=lambda value: value.guid)
    )
    if len({value.guid for value in universe}) != len(universe):
        raise Plan2NativeHorizonError("status-child-card-universe-duplicate")
    return replace(
        zones,
        random_state=native.random_state,
        card_universe=universe,
        hand=hand,
        deck=deck,
        grave=grave,
        lost=lost,
    )


def _remaining_state_for_status_children(
    zones: NativeOrderedZoneState,
) -> RemainingCardMoveState:
    return RemainingCardMoveState(
        hand=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.hand
        ),
        deck=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.deck
        ),
        grave=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.grave
        ),
        lost=tuple(
            _plan3_card_from_ordered(value)
            for value in zones.lost
        ),
        playing=(
            None
            if zones.pending_played is None
            else _plan3_card_from_ordered(zones.pending_played)
        ),
        random_state=zones.random_state,
        turn_used_support_ids=tuple(
            dict.fromkeys(
                support
                for value in zones.hand
                for support in value.support_upgrade_ids
            )
        ),
    )


def _ordered_after_status_remaining(
    zones: NativeOrderedZoneState,
    remaining: RemainingCardMoveState,
) -> NativeOrderedZoneState:
    if remaining.hold or remaining.future_decks or remaining.past_decks:
        raise Plan2NativeHorizonError("status-child-remaining-zone-unbound")
    native = Plan3NativeState(
        hand=remaining.hand,
        deck=remaining.deck,
        grave=remaining.grave,
        lost=remaining.lost,
        random_state=remaining.random_state,
        turn_used_support_ids=remaining.turn_used_support_ids,
    )
    after = _ordered_after_status_native(zones, native)
    expected_playing = (
        None
        if zones.pending_played is None
        else _plan3_card_from_ordered(zones.pending_played)
    )
    if remaining.playing != expected_playing:
        raise Plan2NativeHorizonError("status-child-playing-zone-drift")
    return after


def _sync_encore_runtime(
    runtime: Plan2NativeStatusEnchantEncoreRuntime | None,
    zones: NativeOrderedZoneState,
    scalar: Plan2State,
) -> Plan2NativeStatusEnchantEncoreRuntime | None:
    if runtime is None:
        return None
    native_state = _plan3_state_from_ordered(zones)
    counts = tuple(
        (card.guid, card.play_count) for card in native_state.all_cards
    )
    downstream = replace(
        runtime.downstream,
        native_state=native_state,
        card_play_counts=counts,
        global_play_count=scalar.exam_card_play_count,
        turn_play_count=scalar.turn_card_play_count,
    )
    return Plan2NativeStatusEnchantEncoreRuntime(
        runtime.catalog,
        downstream,
        runtime.database,
        runtime.next_status_uid,
        runtime.total_uses_by_guid,
        runtime.turn_uses_by_guid,
    )


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeBlocker:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.code, "blocker code")
        if not isinstance(self.detail, str):
            raise TypeError("blocker detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


class Plan2NativeHorizonError(ValueError):
    """Stable boundary error carrying one structured blocker."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.blocker = Plan2NativeBlocker(code, detail)
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan2NativeCoverageGate:
    artifact_path: str
    covered_versions: int
    target_versions: int = PLAN2_COVERAGE_TARGET

    def __post_init__(self) -> None:
        _nonempty(self.artifact_path, "artifact_path")
        _plain_int(self.covered_versions, "covered_versions")
        _plain_int(self.target_versions, "target_versions", minimum=1)
        if self.covered_versions > self.target_versions:
            raise ValueError("covered_versions cannot exceed target_versions")

    @property
    def remaining_versions(self) -> int:
        return self.target_versions - self.covered_versions

    @property
    def all_plan2_ready(self) -> bool:
        return self.remaining_versions == 0

    @property
    def blockers(self) -> tuple[Plan2NativeBlocker, ...]:
        if self.all_plan2_ready:
            return ()
        return (
            Plan2NativeBlocker(
                "plan2-card-coverage-gate",
                f"{self.covered_versions}/{self.target_versions};"
                f"remaining={self.remaining_versions}",
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_path": self.artifact_path,
            "covered_versions": self.covered_versions,
            "target_versions": self.target_versions,
            "remaining_versions": self.remaining_versions,
            "all_plan2_ready": self.all_plan2_ready,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


def load_plan2_native_coverage_gate(
    path: str | Path = PLAN2_NATIVE_COVERAGE_ARTIFACT,
) -> Plan2NativeCoverageGate:
    """Read the formal report without mutating or regenerating it."""

    artifact = Path(path)
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Plan2 coverage artifact must contain an object")
    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        raise ValueError("Plan2 coverage artifact has no summary object")
    target = summary.get("card_version_count")
    covered = summary.get(
        "true_executable_versions",
        summary.get("executable_foundation_ready_versions"),
    )
    target_value = _plain_int(target, "summary.card_version_count", minimum=1)
    if target_value != PLAN2_COVERAGE_TARGET:
        raise ValueError(
            f"unexpected Plan2 coverage target: {target_value};"
            f" expected {PLAN2_COVERAGE_TARGET}"
        )
    return Plan2NativeCoverageGate(
        artifact_path=str(artifact),
        covered_versions=_plain_int(covered, "summary.true_executable_versions"),
        target_versions=target_value,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeQueuedAction:
    """One explicit nested command; it never infers a Master rule."""

    kind: str
    value: int = 0
    card_guid: str = ""
    pay_cost: bool = False
    spend_play: bool = False

    def __post_init__(self) -> None:
        if self.kind not in _COMMAND_KINDS:
            raise ValueError(f"unsupported queued action kind: {self.kind}")
        _plain_int(self.value, "queued action value")
        if type(self.pay_cost) is not bool or type(self.spend_play) is not bool:
            raise TypeError("pay_cost and spend_play must be booleans")
        if self.kind == "force_play":
            _nonempty(self.card_guid, "forced card_guid")
        elif self.card_guid:
            raise ValueError("only force_play may carry card_guid")
        if self.kind not in {"draw", "extra_turn", "force_play"} and self.value < 1:
            raise ValueError(f"{self.kind} requires a positive value")
        if self.kind in {"draw", "extra_turn"} and self.value < 1:
            raise ValueError(f"{self.kind} requires a positive value")


@dataclass(frozen=True, slots=True)
class Plan2NativeGeneratedAllocatorInput:
    """Caller-observed identities/selections for one generated-card play."""

    source_guid: str
    guid_tokens: tuple[str, ...] | None = None
    plan_ignore_card_ids: tuple[str, ...] | None = None
    force_selected_guid: str | None = None
    native_status_uid: int | None = None
    enchant_effect_uid: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.source_guid, "source_guid")
        for name in ("guid_tokens", "plan_ignore_card_ids"):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not tuple
                or any(type(item) is not str or not item for item in value)
            ):
                raise TypeError(f"{name} must be an exact tuple of non-empty text")
        if self.force_selected_guid is not None:
            _nonempty(self.force_selected_guid, "force_selected_guid")
        if self.native_status_uid is not None:
            _plain_int(self.native_status_uid, "native_status_uid", minimum=1)
        _plain_int(self.enchant_effect_uid, "enchant_effect_uid")


@dataclass(frozen=True, slots=True)
class Plan2NativeTimer:
    """A bounded fixture timer with explicit phase and child commands."""

    timer_id: str
    phase: str
    boundaries_until_fire: int
    commands: tuple[Plan2NativeQueuedAction, ...]

    def __post_init__(self) -> None:
        _nonempty(self.timer_id, "timer_id")
        if self.phase not in _TIMER_PHASES:
            raise ValueError(f"unsupported timer phase: {self.phase}")
        _plain_int(
            self.boundaries_until_fire,
            "boundaries_until_fire",
            minimum=1,
        )
        commands = tuple(self.commands)
        if not commands or any(
            not isinstance(command, Plan2NativeQueuedAction) for command in commands
        ):
            raise TypeError("timer commands must contain queued actions")
        object.__setattr__(self, "commands", commands)


@dataclass(slots=True)
class Plan2NativeStatusChildContext:
    """Mutable transaction-local bridge for exact status-child adapters."""

    zones: NativeOrderedZoneState
    hand_limit: int
    master_card_refs: tuple[tuple[str, int], ...] = ()
    inputs: list[Plan2NativeStatusChildInput] = field(default_factory=list)
    review_state: Plan2ReviewCountAddInterval3State | None = None
    total_effect_draw_card_count: int = 0
    block_consumption_sum_count: int = 0
    playing_guid: str = ""
    hand_move_programs: tuple[GeneratedHandMoveProgram, ...] = ()
    hand_move_turn: GeneratedHandMoveTurnState = field(
        default_factory=lambda: GeneratedHandMoveTurnState(0)
    )
    hand_add_support_callback: Callable[
        [
            NativeOrderedZoneState,
            tuple[str, ...],
            tuple[str, ...],
        ],
        tuple[NativeOrderedZoneState, tuple[str, ...], tuple[str, ...]],
    ] | None = None
    turn_used_support_ids: tuple[str, ...] = ()
    aggressive_additive_runtime: Plan2AggressiveAdditiveRuntime | None = None


@dataclass(frozen=True, slots=True)
class Plan2NativePlayingEffectResult:
    """Exact direct-effect result while the played GUID remains Playing."""

    scalar: Plan2State
    zones: NativeOrderedZoneState
    stamina_modifiers: Plan2StaminaModifierRuntime = Plan2StaminaModifierRuntime()
    status_enchant: Plan2NativeStatusEnchantRuntime = Plan2NativeStatusEnchantRuntime()
    review_dynamic: Plan2NativeReviewDynamicRuntime = Plan2NativeReviewDynamicRuntime()
    debuff_registry: Plan2NativeDebuffRegistry = Plan2NativeDebuffRegistry()
    effect_chains: Plan2NativeEffectChainRuntime = Plan2NativeEffectChainRuntime()
    generated_runtime: PlayCountBuffRuntime = PlayCountBuffRuntime()
    item_runtime: Plan2NativeItemRuntime = Plan2NativeItemRuntime()
    encore_runtime: Plan2NativeStatusEnchantEncoreRuntime | None = None
    aggressive_additive_runtime: Plan2AggressiveAdditiveRuntime | None = None
    block_fix_restriction_runtime: Plan2NativeBlockFixRestrictionRuntime | None = None
    extra_turn_runtime: Plan2NativeExtraTurnRuntime | None = None
    stamina_recover_restricted: bool = False
    stamina_recover_add_permil: int | None = None
    search_play_card_stamina_runtime: SearchPlayCardStaminaRuntime = SearchPlayCardStaminaRuntime()
    add_grow_runtime: Plan2AddGrowDeckAllState | None = None
    start_play_listeners: tuple[Plan2StartPlayListener, ...] = ()
    triggered_review_status_runtime: Plan2TriggeredReviewStatusRuntime = Plan2TriggeredReviewStatusRuntime()
    review_consumption_sum: int = 0
    block_consumption_sum_count: int = 0
    playable_delta: int = 0
    queued_actions: tuple[Plan2NativeQueuedAction, ...] = ()
    trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scalar, Plan2State):
            raise TypeError("scalar must be Plan2State")
        if not isinstance(self.zones, NativeOrderedZoneState):
            raise TypeError("zones must be NativeOrderedZoneState")
        if not isinstance(self.stamina_modifiers, Plan2StaminaModifierRuntime):
            raise TypeError("stamina_modifiers must be Plan2StaminaModifierRuntime")
        if not isinstance(self.status_enchant, Plan2NativeStatusEnchantRuntime):
            raise TypeError("status_enchant must be Plan2NativeStatusEnchantRuntime")
        if not isinstance(self.review_dynamic, Plan2NativeReviewDynamicRuntime):
            raise TypeError("review_dynamic must be Plan2NativeReviewDynamicRuntime")
        if not isinstance(self.debuff_registry, Plan2NativeDebuffRegistry):
            raise TypeError("debuff_registry must be Plan2NativeDebuffRegistry")
        if not isinstance(self.effect_chains, Plan2NativeEffectChainRuntime):
            raise TypeError("effect_chains must be Plan2NativeEffectChainRuntime")
        if not isinstance(self.generated_runtime, PlayCountBuffRuntime):
            raise TypeError("generated_runtime must be PlayCountBuffRuntime")
        if not isinstance(self.item_runtime, Plan2NativeItemRuntime):
            raise TypeError("item_runtime must be Plan2NativeItemRuntime")
        if self.encore_runtime is not None and not isinstance(
            self.encore_runtime,
            Plan2NativeStatusEnchantEncoreRuntime,
        ):
            raise TypeError("encore_runtime must be Encore runtime or None")
        if self.aggressive_additive_runtime is not None and not isinstance(
            self.aggressive_additive_runtime,
            Plan2AggressiveAdditiveRuntime,
        ):
            raise TypeError("aggressive_additive_runtime must be typed or None")
        if self.block_fix_restriction_runtime is not None and not isinstance(
            self.block_fix_restriction_runtime,
            Plan2NativeBlockFixRestrictionRuntime,
        ):
            raise TypeError("block_fix_restriction_runtime must be typed or None")
        if self.extra_turn_runtime is not None and not isinstance(
            self.extra_turn_runtime,
            Plan2NativeExtraTurnRuntime,
        ):
            raise TypeError("extra_turn_runtime must be typed or None")
        if not isinstance(self.stamina_recover_restricted, bool):
            raise TypeError("stamina_recover_restricted must be boolean")
        if self.stamina_recover_add_permil is not None:
            _plain_int(
                self.stamina_recover_add_permil,
                "stamina_recover_add_permil",
            )
        if not isinstance(
            self.search_play_card_stamina_runtime,
            SearchPlayCardStaminaRuntime,
        ):
            raise TypeError("search_play_card_stamina_runtime must be typed")
        if self.add_grow_runtime is not None and not isinstance(
            self.add_grow_runtime,
            Plan2AddGrowDeckAllState,
        ):
            raise TypeError("add_grow_runtime must be typed or None")
        start_play_listeners = tuple(self.start_play_listeners)
        if any(
            not isinstance(value, Plan2StartPlayListener)
            for value in start_play_listeners
        ):
            raise TypeError("start_play_listeners must contain typed listeners")
        object.__setattr__(self, "start_play_listeners", start_play_listeners)
        if not isinstance(
            self.triggered_review_status_runtime,
            Plan2TriggeredReviewStatusRuntime,
        ):
            raise TypeError("triggered_review_status_runtime must be typed")
        _plain_int(self.review_consumption_sum, "review_consumption_sum")
        _plain_int(
            self.block_consumption_sum_count,
            "block_consumption_sum_count",
        )
        if self.zones.pending_played is None:
            raise ValueError("Playing effect result must retain pending_played")
        _plain_int(self.playable_delta, "playable_delta")
        queued_actions = tuple(self.queued_actions)
        if any(not isinstance(value, Plan2NativeQueuedAction) for value in queued_actions):
            raise TypeError("queued_actions must contain Plan2NativeQueuedAction")
        object.__setattr__(self, "queued_actions", queued_actions)
        trace = tuple(self.trace)
        if any(not isinstance(value, str) or not value for value in trace):
            raise ValueError("trace must contain non-empty text")
        object.__setattr__(self, "trace", trace)


@dataclass(frozen=True, slots=True)
class Plan2NativeCardProgram:
    """Explicit exact program for one supported fixture card version.

    Only the small scalar/direct subset below is modeled here.  Rich Master
    effects remain owned by their reviewed Plan2 adapters and must not be
    translated into this record by description or OCR inference.
    """

    card_id: str
    upgrade: int
    category: str
    effect_group_ids: tuple[str, ...] = ()
    cost_type: str = "none"
    cost_value: int = 0
    native_cost: Plan2NativeCardCost | None = None
    review_delta: int = 0
    block_delta: int = 0
    aggressive_delta: int = 0
    score_delta: int = 0
    stamina_recover: int = 0
    commands: tuple[Plan2NativeQueuedAction, ...] = ()
    move_destination: str = "grave"
    is_end_turn_lost: bool = False
    ends_turn: bool = False
    playing_search: ProduceCardSearchRule | None = None
    native_play_predicate_id: str = ""
    native_play_predicate: Callable[[Plan2State], bool] | None = None
    native_horizon_predicate_id: str = ""
    native_horizon_predicate: Callable[
        ["Plan2NativeHorizonState", str], bool
    ] | None = None
    native_direct_executor_id: str = ""
    native_direct_executor: Callable[[Plan2State], Plan2State] | None = None
    native_zone_executor_id: str = ""
    native_zone_executor: Callable[
        [NativeOrderedZoneState, int],
        tuple[NativeOrderedZoneState, tuple[str, ...]],
    ] | None = None
    native_playing_executor_id: str = ""
    native_playing_executor: Callable[
        [
            Plan2State,
            NativeOrderedZoneState,
            int,
            Plan2StaminaModifierRuntime,
            Plan2NativeStatusEnchantRuntime,
            Plan2NativeReviewDynamicRuntime,
            Plan2NativeDebuffRegistry,
            Plan2NativeEffectChainRuntime,
            Plan2State,
            PlayCountBuffRuntime,
            Plan2NativeGeneratedAllocatorInput | None,
            Plan2NativeStatusEnchantEncoreRuntime | None,
            int,
            int,
            str,
            str,
            int,
            tuple[tuple[int, str], ...],
            Plan2NativeStatusChildContext | None,
        ],
        Plan2NativePlayingEffectResult,
    ] | None = None

    def __post_init__(self) -> None:
        _nonempty(self.card_id, "card_id")
        _plain_int(self.upgrade, "upgrade")
        _nonempty(self.category, "category")
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(group, str) or not group for group in groups):
            raise ValueError("effect_group_ids must contain non-empty text")
        object.__setattr__(self, "effect_group_ids", groups)
        if self.cost_type not in _COST_TYPES:
            raise ValueError(f"unsupported cost_type: {self.cost_type}")
        _plain_int(self.cost_value, "cost_value")
        if self.cost_type == "none" and self.cost_value:
            raise ValueError("none cost cannot have a non-zero value")
        if self.native_cost is not None:
            if not isinstance(self.native_cost, Plan2NativeCardCost):
                raise TypeError("native_cost must be Plan2NativeCardCost or None")
            if self.native_cost.ref != self.ref:
                raise ValueError("native_cost ref must match the card program")
        for name in (
            "review_delta",
            "block_delta",
            "aggressive_delta",
            "score_delta",
            "stamina_recover",
        ):
            _plain_int(getattr(self, name), name)
        commands = tuple(self.commands)
        if any(not isinstance(command, Plan2NativeQueuedAction) for command in commands):
            raise TypeError("commands must contain Plan2NativeQueuedAction")
        object.__setattr__(self, "commands", commands)
        if self.move_destination not in _MOVE_DESTINATIONS:
            raise ValueError(f"unsupported move destination: {self.move_destination}")
        for name in ("is_end_turn_lost", "ends_turn"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be boolean")
        if self.playing_search is not None and not isinstance(
            self.playing_search, ProduceCardSearchRule
        ):
            raise TypeError("playing_search must be ProduceCardSearchRule or None")
        predicate_bound = self.native_play_predicate is not None
        if bool(self.native_play_predicate_id) != predicate_bound:
            raise ValueError(
                "native_play_predicate_id and native_play_predicate must be bound together"
            )
        if predicate_bound and not callable(self.native_play_predicate):
            raise TypeError("native_play_predicate must be callable")
        horizon_predicate_bound = self.native_horizon_predicate is not None
        if bool(self.native_horizon_predicate_id) != horizon_predicate_bound:
            raise ValueError(
                "native_horizon_predicate_id and native_horizon_predicate "
                "must be bound together"
            )
        if horizon_predicate_bound and not callable(self.native_horizon_predicate):
            raise TypeError("native_horizon_predicate must be callable")
        if predicate_bound and horizon_predicate_bound:
            raise ValueError("only one native play predicate runtime may be bound")
        executor_bound = self.native_direct_executor is not None
        if bool(self.native_direct_executor_id) != executor_bound:
            raise ValueError(
                "native_direct_executor_id and native_direct_executor must be bound together"
            )
        if executor_bound and not callable(self.native_direct_executor):
            raise TypeError("native_direct_executor must be callable")
        zone_executor_bound = self.native_zone_executor is not None
        if bool(self.native_zone_executor_id) != zone_executor_bound:
            raise ValueError(
                "native_zone_executor_id and native_zone_executor must be bound together"
            )
        if zone_executor_bound and not callable(self.native_zone_executor):
            raise TypeError("native_zone_executor must be callable")
        playing_executor_bound = self.native_playing_executor is not None
        if bool(self.native_playing_executor_id) != playing_executor_bound:
            raise ValueError(
                "native_playing_executor_id and native_playing_executor must be bound together"
            )
        if playing_executor_bound and not callable(self.native_playing_executor):
            raise TypeError("native_playing_executor must be callable")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2NativeProgramCatalog:
    programs: tuple[Plan2NativeCardProgram, ...]
    master_card_refs: tuple[tuple[str, int], ...] = ()
    status_child_compilation: Plan2NativeStatusChildCompilation | None = None
    hand_move_programs: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        programs = tuple(self.programs)
        if any(not isinstance(program, Plan2NativeCardProgram) for program in programs):
            raise TypeError("programs must contain Plan2NativeCardProgram")
        refs = tuple(program.ref for program in programs)
        if len(refs) != len(set(refs)):
            raise ValueError("program catalog contains duplicate card versions")
        object.__setattr__(self, "programs", programs)
        refs = tuple(self.master_card_refs)
        if refs and refs != tuple(sorted(set(refs))):
            raise ValueError("master_card_refs must be sorted and unique")
        object.__setattr__(self, "master_card_refs", refs)
        if self.status_child_compilation is not None and not isinstance(
            self.status_child_compilation,
            Plan2NativeStatusChildCompilation,
        ):
            raise TypeError("status_child_compilation must be typed or None")
        hand_move_programs = tuple(self.hand_move_programs)
        hand_move_refs = tuple(value.ref for value in hand_move_programs)
        if len(hand_move_refs) != len(set(hand_move_refs)):
            raise ValueError("hand_move_programs contain duplicate card versions")
        object.__setattr__(self, "hand_move_programs", hand_move_programs)

    def get(self, card: NativeOrderedCardInstance) -> Plan2NativeCardProgram | None:
        ref = (card.card_id, card.effective_upgrade)
        return next((program for program in self.programs if program.ref == ref), None)


@dataclass(frozen=True, slots=True)
class Plan2MasterInitialDeck:
    """Current-Master identity manifest; it does not compile card effects."""

    idol_card_id: str
    produce_id: str
    plan_type: str
    exam_effect_type: str
    produce_default_deck_id: str
    character_deck_id: str
    card_refs: tuple[tuple[str, int], ...]
    item_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "idol_card_id",
            "produce_id",
            "plan_type",
            "exam_effect_type",
            "produce_default_deck_id",
            "character_deck_id",
        ):
            _nonempty(getattr(self, name), name)
        if self.plan_type != "ProducePlanType_Plan2":
            raise ValueError("Master initial deck must belong to Plan2")
        refs = tuple(self.card_refs)
        if not refs:
            raise ValueError("Master initial deck cannot be empty")
        for card_id, upgrade in refs:
            _nonempty(card_id, "Master initial card_id")
            _plain_int(upgrade, "Master initial upgrade")
        object.__setattr__(self, "card_refs", refs)
        item_ids = tuple(self.item_ids)
        if any(not isinstance(value, str) or not value for value in item_ids):
            raise ValueError("Master item_ids must contain non-empty text")
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("Master item_ids must be unique")
        object.__setattr__(self, "item_ids", item_ids)


def _yaml_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(row, Mapping) for row in payload):
        raise ValueError(f"Master file must contain a row list: {path}")
    return tuple(payload)


def _deck_refs(row: Mapping[str, object]) -> tuple[tuple[str, int], ...]:
    raw_ids = row.get("produceCardIds", [])
    raw_upgrades = row.get("produceCardUpgradeCounts", [])
    if not isinstance(raw_ids, list) or not all(
        isinstance(card_id, str) and card_id for card_id in raw_ids
    ):
        raise ValueError(f"invalid Master deck card IDs: {row.get('id')!r}")
    if not isinstance(raw_upgrades, list) or not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in raw_upgrades
    ):
        raise ValueError(f"invalid Master deck upgrades: {row.get('id')!r}")
    upgrades = raw_upgrades or [0] * len(raw_ids)
    if len(upgrades) != len(raw_ids):
        raise ValueError(f"Master deck ID/upgrade length mismatch: {row.get('id')!r}")
    return tuple(zip(raw_ids, upgrades, strict=True))


def load_master_plan2_initial_deck(
    idol_card_id: str,
    *,
    produce_id: str = "produce-001",
    idol_card_upgrade: int = 0,
    master_dir: str | Path = DEFAULT_MASTER_DIR,
) -> Plan2MasterInitialDeck:
    """Load complete Plan2 start identities from current Master rows.

    The authoritative composition is the produce-default deck followed by the
    idol row's character deck and its producing card.  Runtime acquisition or
    potential upgrades must be supplied explicitly and are not inferred.
    """

    _nonempty(idol_card_id, "idol_card_id")
    _nonempty(produce_id, "produce_id")
    _plain_int(idol_card_upgrade, "idol_card_upgrade")
    directory = Path(master_dir)
    idol = next(
        (
            row
            for row in _yaml_rows(directory / "IdolCard.yaml")
            if row.get("id") == idol_card_id
        ),
        None,
    )
    if idol is None:
        raise KeyError(f"unknown IdolCard: {idol_card_id}")
    if idol.get("planType") != "ProducePlanType_Plan2":
        raise ValueError(f"IdolCard is not Plan2: {idol_card_id}")
    effect_type = _nonempty(idol.get("examEffectType"), "IdolCard.examEffectType")
    character_deck_id = _nonempty(idol.get("examInitialDeckId"), "IdolCard.examInitialDeckId")
    produce_card_id = _nonempty(idol.get("produceCardId"), "IdolCard.produceCardId")
    item_key = "beforeProduceItemId" if idol_card_upgrade == 0 else "afterProduceItemId"
    item_id = _nonempty(idol.get(item_key), f"IdolCard.{item_key}")
    default_map = next(
        (
            row
            for row in _yaml_rows(directory / "ProduceInitialDeck.yaml")
            if row.get("produceId") == produce_id
            and row.get("examEffectType") == effect_type
        ),
        None,
    )
    if default_map is None:
        raise KeyError(f"no ProduceInitialDeck row for {produce_id}/{effect_type}")
    default_deck_id = _nonempty(
        default_map.get("examInitialDeckId"),
        "ProduceInitialDeck.examInitialDeckId",
    )
    deck_rows = {
        str(row.get("id")): row
        for row in _yaml_rows(directory / "ExamInitialDeck.yaml")
        if row.get("id")
    }
    try:
        default_refs = _deck_refs(deck_rows[default_deck_id])
        character_refs = _deck_refs(deck_rows[character_deck_id])
    except KeyError as error:
        raise KeyError(f"missing ExamInitialDeck row: {error.args[0]}") from error
    return Plan2MasterInitialDeck(
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        plan_type="ProducePlanType_Plan2",
        exam_effect_type=effect_type,
        produce_default_deck_id=default_deck_id,
        character_deck_id=character_deck_id,
        card_refs=(*default_refs, *character_refs, (produce_card_id, idol_card_upgrade)),
        item_ids=(item_id,),
    )


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeDrinkCardMoveProgram:
    """Exact Master binding for the common idol-unique Deck/Grave drink.

    ``GetSearchCardList`` filters the native Deck then Grave collection,
    preserves that order for ``OrderType_Unknown``, and applies the search
    ``LimitCount`` before CardMove receives the list.  The compiler projects
    the Master ``searchTag`` membership into exact card-version references so
    the horizon never guesses from localized card names or descriptions.
    """

    effect_id: str
    search_id: str
    card_search_tag: str
    limit_count: int
    destination: str
    pick_range_type: str
    master_refs: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "drink CardMove effect_id")
        _nonempty(self.search_id, "drink CardMove search_id")
        _nonempty(self.card_search_tag, "drink CardMove search tag")
        _plain_int(self.limit_count, "drink CardMove limit_count", minimum=1)
        if self.destination != "ProduceCardMovePositionType_Hand":
            raise ValueError("drink CardMove destination must be Hand")
        if self.pick_range_type != "ProducePickRangeType_All":
            raise ValueError("drink CardMove pick range must be All")
        refs = tuple(self.master_refs)
        if not refs or refs != tuple(sorted(set(refs))):
            raise ValueError(
                "drink CardMove master refs must be non-empty, sorted, and unique"
            )
        if any(
            not isinstance(card_id, str)
            or not card_id
            or isinstance(upgrade, bool)
            or not isinstance(upgrade, int)
            or upgrade < 0
            for card_id, upgrade in refs
        ):
            raise TypeError("drink CardMove master refs have an invalid shape")
        object.__setattr__(self, "master_refs", refs)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeDrinkPlayCountBuffProgram:
    """Exact finite Playing-search buff installed by a drink."""

    effect_id: str
    search_id: str
    value1: int
    effect_count: int
    effect_turn: int
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "drink PlayCountBuff effect_id")
        _nonempty(self.search_id, "drink PlayCountBuff search_id")
        _plain_int(self.value1, "drink PlayCountBuff value1", minimum=1)
        _plain_int(
            self.effect_count,
            "drink PlayCountBuff effect_count",
            minimum=1,
        )
        _plain_int(
            self.effect_turn,
            "drink PlayCountBuff effect_turn",
            minimum=1,
        )
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(value, str) or not value for value in groups):
            raise ValueError(
                "drink PlayCountBuff effect_group_ids must contain text"
            )
        object.__setattr__(self, "effect_group_ids", groups)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeDrinkEffect:
    """One already-validated Master effect in native drink-list order."""

    effect_index: int
    drink_effect_id: str
    effect_id: str
    effect_type: str
    handler: str
    value1: int
    value2: int = 0
    count: int = 0
    turn: int = 0
    end_turn_program: Plan2EndTurnProgram | None = None
    review_dynamic_program: Plan2NativeReviewDynamicProgram | None = None
    stamina_down_effect: StaminaConsumptionDownEffect | None = None
    stamina_add_effect: StaminaConsumptionAddEffect | None = None
    start_play_program: Plan2StartPlayStatusProgram | None = None
    card_move_program: Plan2NativeDrinkCardMoveProgram | None = None
    card_create_program: CardCreateSearchEffectContract | None = None
    force_play_random_pool_program: Plan2DrinkForcePlayRandomPoolProgram | None = None
    play_count_buff_program: Plan2NativeDrinkPlayCountBuffProgram | None = None

    def __post_init__(self) -> None:
        _plain_int(self.effect_index, "drink effect_index")
        _nonempty(self.drink_effect_id, "drink_effect_id")
        _nonempty(self.effect_id, "drink effect_id")
        _nonempty(self.effect_type, "drink effect_type")
        expected_type = _DRINK_HANDLER_EFFECT_TYPES.get(self.handler)
        if expected_type is None:
            raise ValueError(f"unsupported drink handler: {self.handler}")
        if self.effect_type != expected_type:
            raise ValueError(
                "drink handler/effect type mismatch: "
                f"{self.handler}:{self.effect_type}"
            )
        for name in ("value1", "value2", "count", "turn"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"drink {name} must be an integer")
            minimum = -1 if name == "turn" else 0
            if not minimum <= value <= INT32_MAX:
                raise ValueError(f"drink {name} must fit supported Int32 range")
        if self.handler == DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL:
            program = self.force_play_random_pool_program
            if not isinstance(program, Plan2DrinkForcePlayRandomPoolProgram):
                raise TypeError(
                    "drink ForcePlay RandomPool requires a typed program"
                )
            if (
                self.effect_id != program.effect_id
                or self.value1 != 0
                or self.value2 != 0
                or self.count != 0
                or self.turn != 0
            ):
                raise ValueError(
                    "drink ForcePlay RandomPool program/effect mismatch"
                )
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.review_dynamic_program,
                    self.stamina_down_effect,
                    self.stamina_add_effect,
                    self.start_play_program,
                    self.card_move_program,
                    self.card_create_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError(
                    "drink ForcePlay RandomPool carries another typed program"
                )
            return
        if self.force_play_random_pool_program is not None:
            raise ValueError(
                "non-ForcePlay drink carries a ForcePlay RandomPool program"
            )
        if self.handler == DRINK_HANDLER_END_TURN_STATUS:
            program = self.end_turn_program
            if not isinstance(program, Plan2EndTurnProgram):
                raise TypeError("end-turn status drink requires a typed program")
            if (
                self.effect_id != program.wrapper_effect_id
                or self.value1 != program.effect_value1
                or self.value2 != 0
                or self.count != program.effect_count
                or self.turn != program.turn
            ):
                raise ValueError("drink end-turn program/effect shape mismatch")
            if any(
                value is not None
                for value in (
                    self.review_dynamic_program,
                    self.stamina_down_effect,
                    self.stamina_add_effect,
                    self.start_play_program,
                    self.card_move_program,
                    self.card_create_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError("end-turn drink carries another typed program")
            return
        if self.handler == DRINK_HANDLER_START_PLAY_STATUS:
            program = self.start_play_program
            if not isinstance(program, Plan2StartPlayStatusProgram):
                raise TypeError("StartPlay status drink requires a typed program")
            if (
                self.effect_id != program.wrapper.id
                or self.value1 != program.wrapper.value1
                or self.value2 != program.wrapper.value2
                or self.count != program.wrapper.effect_count
                or self.turn != program.wrapper.effect_turn
            ):
                raise ValueError("drink StartPlay program/effect shape mismatch")
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.review_dynamic_program,
                    self.stamina_down_effect,
                    self.stamina_add_effect,
                    self.card_move_program,
                    self.card_create_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError("StartPlay drink carries another typed program")
            return
        if self.handler == DRINK_HANDLER_LESSON_VALUE_MULTIPLE:
            program = self.review_dynamic_program
            if not isinstance(program, Plan2NativeReviewDynamicProgram):
                raise TypeError("lesson-multiple drink requires a typed program")
            if (
                program.operation_kind != OP_INSTALL_LESSON_MULTIPLE
                or self.effect_id != program.effect_id
                or self.value1 != program.value1
                or self.value2 != program.value2
                or self.count != program.effect_count
                or self.turn != program.effect_turn
            ):
                raise ValueError("drink lesson-multiple program/effect mismatch")
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.stamina_down_effect,
                    self.stamina_add_effect,
                    self.start_play_program,
                    self.card_move_program,
                    self.card_create_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError("lesson-multiple drink carries another program")
            return
        if self.handler == DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN:
            contract = self.stamina_down_effect
            if not isinstance(contract, StaminaConsumptionDownEffect):
                raise TypeError("stamina-down drink requires a typed contract")
            if (
                self.effect_id != contract.effect_id
                or self.value1 != contract.value1
                or self.value2 != contract.value2
                or self.count != contract.effect_count
                or self.turn != contract.effect_turn
            ):
                raise ValueError("drink stamina-down contract/effect mismatch")
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.review_dynamic_program,
                    self.stamina_add_effect,
                    self.start_play_program,
                    self.card_move_program,
                    self.card_create_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError("stamina-down drink carries another program")
            return
        if self.handler == DRINK_HANDLER_STAMINA_CONSUMPTION_ADD:
            contract = self.stamina_add_effect
            if not isinstance(contract, StaminaConsumptionAddEffect):
                raise TypeError("stamina-add drink requires a typed contract")
            if (
                self.effect_id != contract.effect_id
                or self.value1 != contract.value1
                or self.value2 != contract.value2
                or self.count != contract.effect_count
                or self.turn != contract.effect_turn
            ):
                raise ValueError("drink stamina-add contract/effect mismatch")
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.review_dynamic_program,
                    self.stamina_down_effect,
                    self.start_play_program,
                    self.card_move_program,
                    self.card_create_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError("stamina-add drink carries another program")
            return
        if self.handler == DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND:
            program = self.card_move_program
            if not isinstance(program, Plan2NativeDrinkCardMoveProgram):
                raise TypeError("drink CardMove requires a typed program")
            if (
                self.effect_id != program.effect_id
                or self.value1 != 0
                or self.value2 != 0
                or self.count != 0
                or self.turn != 0
            ):
                raise ValueError("drink CardMove program/effect shape mismatch")
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.review_dynamic_program,
                    self.stamina_down_effect,
                    self.stamina_add_effect,
                    self.start_play_program,
                    self.card_create_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError("drink CardMove carries another typed program")
            return
        if self.handler == DRINK_HANDLER_CARD_CREATE_SEARCH:
            program = self.card_create_program
            if not isinstance(program, CardCreateSearchEffectContract):
                raise TypeError("drink CardCreateSearch requires a typed program")
            if (
                self.effect_id != program.effect_id
                or self.value1 != program.effect_value1
                or self.value2 != program.effect_value2
                or self.count != program.effect_count
                or self.turn != program.effect_turn
            ):
                raise ValueError("drink CardCreateSearch program/effect mismatch")
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.review_dynamic_program,
                    self.stamina_down_effect,
                    self.stamina_add_effect,
                    self.start_play_program,
                    self.card_move_program,
                    self.play_count_buff_program,
                )
            ):
                raise ValueError(
                    "drink CardCreateSearch carries another typed program"
                )
            return
        if self.handler == DRINK_HANDLER_PLAY_COUNT_BUFF:
            program = self.play_count_buff_program
            if not isinstance(program, Plan2NativeDrinkPlayCountBuffProgram):
                raise TypeError(
                    "drink PlayCountBuff requires a typed program"
                )
            if (
                self.effect_id != program.effect_id
                or self.value1 != program.value1
                or self.value2 != 0
                or self.count != program.effect_count
                or self.turn != program.effect_turn
            ):
                raise ValueError(
                    "drink PlayCountBuff program/effect mismatch"
                )
            if any(
                value is not None
                for value in (
                    self.end_turn_program,
                    self.review_dynamic_program,
                    self.stamina_down_effect,
                    self.stamina_add_effect,
                    self.start_play_program,
                    self.card_move_program,
                    self.card_create_program,
                )
            ):
                raise ValueError(
                    "drink PlayCountBuff carries another typed program"
                )
            return
        if any(
            value is not None
            for value in (
                self.end_turn_program,
                self.review_dynamic_program,
                self.stamina_down_effect,
                self.stamina_add_effect,
                self.start_play_program,
                self.card_move_program,
                self.card_create_program,
                self.play_count_buff_program,
            )
        ):
            raise ValueError("ordinary drink effect must not carry a typed program")
        if self.value2 != 0 or self.turn != 0:
            raise ValueError("drink effect carries an unsupported value2/turn shape")
        if self.handler in {
            DRINK_HANDLER_LESSON,
            DRINK_HANDLER_LESSON_DEPEND_REVIEW,
        }:
            valid = self.value1 > 0 and self.count > 0
        elif self.handler == DRINK_HANDLER_PLAYABLE_ADD:
            valid = self.value1 == 0 and self.count > 0
        elif self.handler == DRINK_HANDLER_CARD_UPGRADE_HAND_ALL:
            valid = (
                self.effect_id
                == "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"
                and self.value1 == 0
                and self.count == 0
            )
        elif self.handler == DRINK_HANDLER_HAND_GRAVE_DRAW:
            valid = self.value1 == 0 and self.count == 0
        elif self.handler == DRINK_HANDLER_CARD_DRAW:
            valid = self.value1 > 0 and self.count == 0
        elif self.handler == DRINK_HANDLER_STAMINA_REDUCE_FIX:
            valid = self.value1 > 0 and self.count == 0
        else:
            valid = self.value1 > 0 and self.count == 0
        if not valid:
            raise ValueError(f"unsupported drink effect shape: {self.effect_id}")


@dataclass(frozen=True, slots=True)
class Plan2NativeExternalReviewEffect:
    """One validated non-card ``ExamReview`` effect at a native phase hook.

    Scheduled stage gimmicks use a stable source identity rather than a card
    GUID.  Execution still enters the ordinary status-child/status-change
    pipeline so Review mirrors and listeners observe the committed change.
    """

    source_id: str
    effect_id: str
    value: int
    sequence: int = 0
    apply_review_additive: bool = True
    suppressed_status_uids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.source_id, "external review source_id")
        _nonempty(self.effect_id, "external review effect_id")
        _plain_int(self.value, "external review value", minimum=1)
        _plain_int(self.sequence, "external review sequence")
        if self.value > INT32_MAX:
            raise ValueError("external review value must fit Int32")
        if type(self.apply_review_additive) is not bool:
            raise TypeError("apply_review_additive must be bool")
        suppressed = tuple(self.suppressed_status_uids)
        if (
            any(type(value) is not int or value <= 0 for value in suppressed)
            or len(suppressed) != len(set(suppressed))
        ):
            raise ValueError("suppressed_status_uids must be unique positive integers")
        object.__setattr__(self, "suppressed_status_uids", suppressed)


@dataclass(frozen=True, slots=True)
class Plan2NativeExternalBlockEffect:
    """One validated non-card ``ExamBlock`` effect at a native phase hook."""

    source_id: str
    effect_id: str
    value: int
    sequence: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.source_id, "external block source_id")
        _nonempty(self.effect_id, "external block effect_id")
        _plain_int(self.value, "external block value", minimum=1)
        _plain_int(self.sequence, "external block sequence")
        if self.value > INT32_MAX:
            raise ValueError("external block value must fit Int32")


@dataclass(frozen=True, slots=True)
class Plan2NativeExternalStaminaDamageEffect:
    """One validated direct stamina-damage gimmick effect."""

    source_id: str
    effect_id: str
    value: int
    sequence: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.source_id, "external stamina-damage source_id")
        _nonempty(self.effect_id, "external stamina-damage effect_id")
        _plain_int(self.value, "external stamina-damage value", minimum=1)
        _plain_int(self.sequence, "external stamina-damage sequence")
        if self.value > INT32_MAX:
            raise ValueError("external stamina-damage value must fit Int32")


@dataclass(frozen=True, slots=True)
class Plan2NativeExternalAggressiveEffect:
    """One validated non-card ``ExamCardPlayAggressive`` phase effect."""

    source_id: str
    effect_id: str
    value: int
    sequence: int = 0

    def __post_init__(self) -> None:
        _nonempty(self.source_id, "external aggressive source_id")
        _nonempty(self.effect_id, "external aggressive effect_id")
        _plain_int(self.value, "external aggressive value", minimum=1)
        _plain_int(self.sequence, "external aggressive sequence")
        if self.value > INT32_MAX:
            raise ValueError("external aggressive value must fit Int32")


@dataclass(frozen=True, slots=True)
class Plan2NativeDrinkInstance:
    """One stable outer/LocalSave slot and its complete ordered program."""

    instance_id: str
    drink_id: str
    plan_type: str
    effects: tuple[Plan2NativeDrinkEffect, ...]
    session_ref: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.instance_id, "drink instance_id")
        _nonempty(self.drink_id, "drink_id")
        _nonempty(self.plan_type, "drink plan_type")
        session_ref = self.session_ref or self.instance_id
        _nonempty(session_ref, "drink session_ref")
        effects = tuple(self.effects)
        if not effects or any(
            not isinstance(value, Plan2NativeDrinkEffect) for value in effects
        ):
            raise TypeError("drink effects must contain typed effects")
        if tuple(value.effect_index for value in effects) != tuple(
            range(len(effects))
        ):
            raise ValueError("drink effects must preserve contiguous Master order")
        object.__setattr__(self, "effects", effects)
        object.__setattr__(self, "session_ref", session_ref)


@dataclass(frozen=True, slots=True)
class Plan2NativeDrinkRuntime:
    """Ordered, consumable drink inventory owned by the offline horizon."""

    inventory: tuple[Plan2NativeDrinkInstance, ...] = ()

    def __post_init__(self) -> None:
        inventory = tuple(self.inventory)
        if any(
            not isinstance(value, Plan2NativeDrinkInstance)
            for value in inventory
        ):
            raise TypeError("drink inventory must contain typed instances")
        instance_ids = tuple(value.instance_id for value in inventory)
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("drink instance_id values must be unique")
        object.__setattr__(self, "inventory", inventory)

    @property
    def session_refs(self) -> tuple[str, ...]:
        return tuple(value.session_ref for value in self.inventory)

    def resolve(
        self, slot_index: int, *, instance_id: str, drink_id: str
    ) -> Plan2NativeDrinkInstance:
        _plain_int(slot_index, "drink slot_index")
        if slot_index >= len(self.inventory):
            raise Plan2NativeHorizonError(
                "drink-slot-out-of-range", str(slot_index)
            )
        selected = self.inventory[slot_index]
        if selected.instance_id != instance_id or selected.drink_id != drink_id:
            raise Plan2NativeHorizonError(
                "drink-slot-identity-mismatch",
                f"slot={slot_index};expected={selected.instance_id}:"
                f"{selected.drink_id};actual={instance_id}:{drink_id}",
            )
        return selected

    def consume(
        self, slot_index: int, *, instance_id: str, drink_id: str
    ) -> tuple["Plan2NativeDrinkRuntime", Plan2NativeDrinkInstance]:
        selected = self.resolve(
            slot_index,
            instance_id=instance_id,
            drink_id=drink_id,
        )
        return (
            Plan2NativeDrinkRuntime(
                self.inventory[:slot_index] + self.inventory[slot_index + 1 :]
            ),
            selected,
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeHorizonState:
    schema_version: int
    scalar: Plan2State
    zones: NativeOrderedZoneState
    limit_turn: int
    stamina_modifiers: Plan2StaminaModifierRuntime = Plan2StaminaModifierRuntime()
    status_enchant: Plan2NativeStatusEnchantRuntime = Plan2NativeStatusEnchantRuntime()
    review_dynamic: Plan2NativeReviewDynamicRuntime = Plan2NativeReviewDynamicRuntime()
    debuff_registry: Plan2NativeDebuffRegistry = Plan2NativeDebuffRegistry()
    effect_chains: Plan2NativeEffectChainRuntime = Plan2NativeEffectChainRuntime()
    generated_runtime: PlayCountBuffRuntime = PlayCountBuffRuntime()
    generated_inputs: tuple[Plan2NativeGeneratedAllocatorInput, ...] = ()
    status_child_inputs: tuple[Plan2NativeStatusChildInput, ...] = ()
    status_child_review_state: Plan2ReviewCountAddInterval3State | None = None
    total_effect_draw_card_count: int = 0
    generated_hand_move_turn: GeneratedHandMoveTurnState = field(
        default_factory=lambda: GeneratedHandMoveTurnState(0)
    )
    encore_runtime: Plan2NativeStatusEnchantEncoreRuntime | None = None
    aggressive_additive_runtime: Plan2AggressiveAdditiveRuntime | None = None
    block_fix_restriction_runtime: Plan2NativeBlockFixRestrictionRuntime | None = None
    extra_turn_runtime: Plan2NativeExtraTurnRuntime | None = None
    stamina_recover_restricted: bool = False
    stamina_recover_add_permil: int | None = None
    search_play_card_stamina_runtime: SearchPlayCardStaminaRuntime = SearchPlayCardStaminaRuntime()
    add_grow_runtime: Plan2AddGrowDeckAllState | None = None
    item_runtime: Plan2NativeItemRuntime = Plan2NativeItemRuntime()
    drink_runtime: Plan2NativeDrinkRuntime = Plan2NativeDrinkRuntime()
    start_play_listeners: tuple[Plan2StartPlayListener, ...] = ()
    triggered_review_status_runtime: Plan2TriggeredReviewStatusRuntime = Plan2TriggeredReviewStatusRuntime()
    review_consumption_sum: int = 0
    block_consumption_sum_count: int = 0
    judge_parameter: int | None = None
    clear_border: int | None = None
    extra_turn: int = 0
    draw_count: int = 3
    hand_limit: int = 5
    plays_remaining: int = 1
    phase: str = PHASE_MAIN
    timers: tuple[Plan2NativeTimer, ...] = ()
    command_queue: tuple[Plan2NativeQueuedAction, ...] = ()
    opaque_status_queue: tuple[str, ...] = ()
    source_kind: str = "fixture"
    exam_mode: Plan2ExamMode = DEFAULT_PLAN2_AUDITION_EXAM_MODE
    scheduled_gimmicks: tuple[Plan2ScheduledGimmick, ...] = ()
    terminal_reason: Plan2ExamTerminalReason | None = None
    # Optional caller-owned HandAdd runtime.  These are appended after all
    # legacy fields so existing positional horizon constructors remain valid.
    hand_add_support_catalog: "Plan2NativeHandAddSupportCatalog | None" = None
    hand_add_lesson_type: str | None = None
    turn_used_support_ids: tuple[str, ...] = ()
    unmapped_drink_slots: tuple[str, ...] = ()
    # ExamSetting.examTurnEndRecoveryStamina is applied at an explicit
    # EndTurn/early-finish boundary.  It is deliberately kept out of an
    # ordinary card's ``ends_turn`` path, whose automatic lifecycle does not
    # receive the ExamSetting recovery.
    turn_end_stamina_recovery: int = 0

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_HORIZON_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 native horizon schema")
        if not isinstance(self.scalar, Plan2State):
            raise TypeError("scalar must be Plan2State")
        if not isinstance(self.zones, NativeOrderedZoneState):
            raise TypeError("zones must be NativeOrderedZoneState")
        if self.hand_add_support_catalog is not None:
            # Local import avoids the adapter↔horizon module cycle.  The
            # optional runtime is a pure typed boundary and does not alter
            # legacy horizons that leave it unset.
            from .plan2_native_hand_add_support import (
                Plan2NativeHandAddSupportCatalog,
            )

            if not isinstance(
                self.hand_add_support_catalog,
                Plan2NativeHandAddSupportCatalog,
            ):
                raise TypeError(
                    "hand_add_support_catalog must be a typed support catalog"
                )
        if self.hand_add_lesson_type is not None and (
            not isinstance(self.hand_add_lesson_type, str)
            or not self.hand_add_lesson_type.strip()
        ):
            raise ValueError("hand_add_lesson_type must be non-empty text or None")
        used_support_ids = tuple(self.turn_used_support_ids)
        if any(
            not isinstance(value, str) or not value.strip()
            for value in used_support_ids
        ):
            raise ValueError("turn_used_support_ids must contain non-empty text")
        if len(used_support_ids) != len(set(used_support_ids)):
            raise ValueError("turn_used_support_ids must not contain duplicates")
        if self.hand_add_support_catalog is None and used_support_ids:
            raise ValueError(
                "turn_used_support_ids require hand_add_support_catalog"
            )
        if self.hand_add_support_catalog is not None:
            known_support_ids = {
                value.support_id
                for value in self.hand_add_support_catalog.support_upgrades
            }
            unknown_support_ids = tuple(
                value for value in used_support_ids if value not in known_support_ids
            )
            if unknown_support_ids:
                raise ValueError(
                    "turn_used_support_ids contain unknown support IDs: "
                    + ",".join(unknown_support_ids)
                )
        object.__setattr__(self, "turn_used_support_ids", used_support_ids)
        unmapped_drink_slots = tuple(self.unmapped_drink_slots)
        if any(
            not isinstance(value, str) or not value
            for value in unmapped_drink_slots
        ):
            raise ValueError("unmapped_drink_slots must contain non-empty text")
        object.__setattr__(self, "unmapped_drink_slots", unmapped_drink_slots)
        _plain_int(
            self.turn_end_stamina_recovery,
            "turn_end_stamina_recovery",
            minimum=0,
        )
        if self.turn_end_stamina_recovery > INT32_MAX:
            raise ValueError(
                "turn_end_stamina_recovery is outside non-negative Int32"
            )
        if not isinstance(self.exam_mode, Plan2ExamMode):
            raise TypeError("exam_mode must be Plan2ExamMode")
        if not isinstance(self.stamina_modifiers, Plan2StaminaModifierRuntime):
            raise TypeError("stamina_modifiers must be Plan2StaminaModifierRuntime")
        if not isinstance(self.status_enchant, Plan2NativeStatusEnchantRuntime):
            raise TypeError("status_enchant must be Plan2NativeStatusEnchantRuntime")
        if not isinstance(self.review_dynamic, Plan2NativeReviewDynamicRuntime):
            raise TypeError("review_dynamic must be Plan2NativeReviewDynamicRuntime")
        if not isinstance(self.debuff_registry, Plan2NativeDebuffRegistry):
            raise TypeError("debuff_registry must be Plan2NativeDebuffRegistry")
        if not isinstance(self.effect_chains, Plan2NativeEffectChainRuntime):
            raise TypeError("effect_chains must be Plan2NativeEffectChainRuntime")
        if not isinstance(self.generated_runtime, PlayCountBuffRuntime):
            raise TypeError("generated_runtime must be PlayCountBuffRuntime")
        generated_inputs = tuple(self.generated_inputs)
        if any(
            not isinstance(value, Plan2NativeGeneratedAllocatorInput)
            for value in generated_inputs
        ):
            raise TypeError("generated_inputs must contain typed allocator inputs")
        generated_source_guids = tuple(value.source_guid for value in generated_inputs)
        if len(generated_source_guids) != len(set(generated_source_guids)):
            raise ValueError("generated_inputs source_guid values must be unique")
        object.__setattr__(self, "generated_inputs", generated_inputs)
        status_child_inputs = tuple(self.status_child_inputs)
        if any(
            not isinstance(value, Plan2NativeStatusChildInput)
            for value in status_child_inputs
        ):
            raise TypeError("status_child_inputs must contain typed inputs")
        object.__setattr__(self, "status_child_inputs", status_child_inputs)
        if self.status_child_review_state is not None:
            if not isinstance(
                self.status_child_review_state,
                Plan2ReviewCountAddInterval3State,
            ):
                raise TypeError("status_child_review_state must be typed or None")
        _plain_int(
            self.total_effect_draw_card_count,
            "total_effect_draw_card_count",
        )
        if not isinstance(
            self.generated_hand_move_turn,
            GeneratedHandMoveTurnState,
        ):
            raise TypeError("generated_hand_move_turn must be typed")
        if self.generated_hand_move_turn.exam_turn > self.scalar.current_turn:
            raise ValueError("generated hand-move turn is ahead of scalar turn")
        if self.encore_runtime is not None and not isinstance(
            self.encore_runtime,
            Plan2NativeStatusEnchantEncoreRuntime,
        ):
            raise TypeError("encore_runtime must be Encore runtime or None")
        if self.aggressive_additive_runtime is not None and not isinstance(
            self.aggressive_additive_runtime,
            Plan2AggressiveAdditiveRuntime,
        ):
            raise TypeError("aggressive_additive_runtime must be typed or None")
        if self.block_fix_restriction_runtime is not None and not isinstance(
            self.block_fix_restriction_runtime,
            Plan2NativeBlockFixRestrictionRuntime,
        ):
            raise TypeError("block_fix_restriction_runtime must be typed or None")
        if self.extra_turn_runtime is not None and not isinstance(
            self.extra_turn_runtime,
            Plan2NativeExtraTurnRuntime,
        ):
            raise TypeError("extra_turn_runtime must be typed or None")
        if (
            self.extra_turn_runtime is not None
            and self.extra_turn_runtime.extra_turn != self.extra_turn
        ):
            raise ValueError("extra-turn runtime/scalar mismatch")
        if not isinstance(self.stamina_recover_restricted, bool):
            raise TypeError("stamina_recover_restricted must be boolean")
        if self.stamina_recover_add_permil is not None:
            _plain_int(
                self.stamina_recover_add_permil,
                "stamina_recover_add_permil",
            )
        if not isinstance(
            self.search_play_card_stamina_runtime,
            SearchPlayCardStaminaRuntime,
        ):
            raise TypeError("search_play_card_stamina_runtime must be typed")
        if self.add_grow_runtime is not None and not isinstance(
            self.add_grow_runtime,
            Plan2AddGrowDeckAllState,
        ):
            raise TypeError("add_grow_runtime must be typed or None")
        if not isinstance(self.item_runtime, Plan2NativeItemRuntime):
            raise TypeError("item_runtime must be Plan2NativeItemRuntime")
        if not isinstance(self.drink_runtime, Plan2NativeDrinkRuntime):
            raise TypeError("drink_runtime must be Plan2NativeDrinkRuntime")
        start_play_listeners = tuple(self.start_play_listeners)
        if any(
            not isinstance(value, Plan2StartPlayListener)
            for value in start_play_listeners
        ):
            raise TypeError("start_play_listeners must contain typed listeners")
        object.__setattr__(self, "start_play_listeners", start_play_listeners)
        if not isinstance(
            self.triggered_review_status_runtime,
            Plan2TriggeredReviewStatusRuntime,
        ):
            raise TypeError("triggered_review_status_runtime must be typed")
        if (
            self.triggered_review_status_runtime.review_runtime.review
            != self.review_dynamic.review
        ):
            raise ValueError("triggered-review/review-dynamic snapshot mismatch")
        _plain_int(self.review_consumption_sum, "review_consumption_sum")
        _plain_int(
            self.block_consumption_sum_count,
            "block_consumption_sum_count",
        )
        for name in ("judge_parameter", "clear_border"):
            value = getattr(self, name)
            if value is not None:
                _plain_int(value, name)
        if self.judge_parameter is not None:
            object.__setattr__(self, "judge_parameter", self.scalar.score)
        if self.scalar.battle_scoring is not None:
            if (
                self.scalar.score
                != self.scalar.battle_scoring.judge_parameter
            ):
                raise ValueError("battle-scoring-score-mismatch")
            if self.scalar.battle_scoring.exam_mode != self.exam_mode:
                raise ValueError("exam-mode-scoring-context-mismatch")
            object.__setattr__(
                self,
                "clear_border",
                self.scalar.battle_scoring.clear_border,
            )
        scalar_uids = [
            *(value.status_uid for value in self.scalar.review_multiple_layers),
            *(value.status_uid for value in self.scalar.end_turn_listeners),
            *(value.status_uid for value in self.scalar.card_play_listeners),
            *(
                (self.scalar.stamina_consumption_add_status.status_uid,)
                if self.scalar.stamina_consumption_add_status is not None
                else ()
            ),
        ]
        stamina_uids = [
            *(value.status_uid for value in self.stamina_modifiers.down_fix_layers),
            *(
                (self.stamina_modifiers.down.status_uid,)
                if self.stamina_modifiers.down is not None
                else ()
            ),
            *(
                (self.stamina_modifiers.add.layer.status_uid,)
                if self.stamina_modifiers.add.layer is not None
                else ()
            ),
        ]
        status_uids = [value.status_uid for value in self.status_enchant.listeners]
        review_dynamic_uids = [
            value.status_uid for value in self.review_dynamic.layers
        ]
        debuff_uids = [value.uid for value in self.debuff_registry.statuses]
        effect_chain_uids = [value.status_uid for value in self.effect_chains.queue]
        generated_uids = [value.native_uid for value in self.generated_runtime.statuses]
        status_child_review_uids = (
            [
                *(value.status_uid for value in self.status_child_review_state.active),
                *(
                    value.status_uid
                    for value in self.status_child_review_state.review_count_add_layers
                ),
            ]
            if self.status_child_review_state is not None
            else []
        )
        encore_uids = (
            [value.status_uid for value in self.encore_runtime.listeners]
            if self.encore_runtime is not None
            else []
        )
        aggressive_additive_uids = (
            list(self.aggressive_additive_runtime.status_uids)
            if self.aggressive_additive_runtime is not None
            else []
        )
        block_fix_restriction_uids = (
            list(self.block_fix_restriction_runtime.active_status_uids)
            if self.block_fix_restriction_runtime is not None
            else []
        )
        search_play_card_stamina_uids = [
            value.status_uid
            for value in self.search_play_card_stamina_runtime.statuses
        ]
        start_play_uids = [
            value.status_uid for value in self.start_play_listeners
        ]
        triggered_review_uids = [
            value.status_uid
            for value in self.triggered_review_status_runtime.listeners
        ]
        item_uids = list(self.item_runtime.active_status_uids)
        active_uids = (
            *scalar_uids,
            *stamina_uids,
            *status_uids,
            *review_dynamic_uids,
            *debuff_uids,
            *effect_chain_uids,
            *generated_uids,
            *status_child_review_uids,
            *encore_uids,
            *aggressive_additive_uids,
            *block_fix_restriction_uids,
            *search_play_card_stamina_uids,
            *start_play_uids,
            *triggered_review_uids,
            *item_uids,
        )
        if len(active_uids) != len(set(active_uids)):
            duplicates = tuple(
                sorted(
                    uid
                    for uid in set(active_uids)
                    if active_uids.count(uid) > 1
                )
            )
            raise ValueError(
                "cross-runtime active status_uid values must be unique:"
                + ",".join(str(uid) for uid in duplicates)
            )
        if self.zones.pending_played is not None:
            raise ValueError("horizon nodes must be settled")
        _plain_int(self.limit_turn, "limit_turn", minimum=1)
        _plain_int(self.extra_turn, "extra_turn")
        _plain_int(self.draw_count, "draw_count")
        _plain_int(self.hand_limit, "hand_limit", minimum=1)
        _plain_int(self.plays_remaining, "plays_remaining")
        if self.scalar.current_turn > self.limit_turn + self.extra_turn + 1:
            raise ValueError("current_turn is beyond the terminal boundary")
        if self.phase not in {PHASE_MAIN, PHASE_TERMINAL}:
            raise ValueError(f"unsupported horizon phase: {self.phase}")
        timers = tuple(self.timers)
        commands = tuple(self.command_queue)
        scheduled_gimmicks = tuple(self.scheduled_gimmicks)
        statuses = tuple(self.opaque_status_queue)
        if any(not isinstance(timer, Plan2NativeTimer) for timer in timers):
            raise TypeError("timers must contain Plan2NativeTimer")
        if any(not isinstance(command, Plan2NativeQueuedAction) for command in commands):
            raise TypeError("command_queue must contain Plan2NativeQueuedAction")
        if any(
            not isinstance(value, Plan2ScheduledGimmick)
            for value in scheduled_gimmicks
        ):
            raise TypeError(
                "scheduled_gimmicks must contain Plan2ScheduledGimmick"
            )
        scheduled_turns = tuple(value.turn for value in scheduled_gimmicks)
        if scheduled_turns != tuple(sorted(scheduled_turns)):
            raise ValueError("scheduled_gimmicks must retain native turn order")
        if any(
            turn > self.limit_turn + self.extra_turn
            for turn in scheduled_turns
        ):
            raise ValueError("scheduled gimmick turn exceeds exam boundary")
        if any(not isinstance(status, str) or not status for status in statuses):
            raise ValueError("opaque_status_queue must contain non-empty text")
        if len({timer.timer_id for timer in timers}) != len(timers):
            raise ValueError("timer IDs must be unique")
        object.__setattr__(self, "timers", timers)
        object.__setattr__(self, "command_queue", commands)
        object.__setattr__(self, "scheduled_gimmicks", scheduled_gimmicks)
        object.__setattr__(self, "opaque_status_queue", statuses)
        terminal_reason = self.terminal_reason
        if terminal_reason is not None:
            try:
                terminal_reason = Plan2ExamTerminalReason(terminal_reason)
            except (TypeError, ValueError) as error:
                raise ValueError("unsupported terminal_reason") from error
            object.__setattr__(self, "terminal_reason", terminal_reason)
        turn_exhausted = (
            self.scalar.current_turn > self.limit_turn + self.extra_turn
        )
        if self.phase == PHASE_MAIN:
            if turn_exhausted or terminal_reason is not None:
                raise ValueError(
                    "main phase disagrees with the terminal boundary"
                )
        elif terminal_reason is Plan2ExamTerminalReason.TURN_EXHAUSTED:
            # Native's last-boundary path records the active turn, then marks
            # ExamEndComplete; it does not enter a synthetic next turn.  Keep
            # accepting the historical post-boundary (+1) representation so
            # persisted/search fixtures remain readable, while allowing the
            # native-shaped terminal node at the limit itself.
            if self.scalar.current_turn < self.limit_turn + self.extra_turn:
                raise ValueError("turn-exhausted terminal precedes the final active turn")
        elif terminal_reason is Plan2ExamTerminalReason.LIMIT_BORDER:
            scoring = self.scalar.battle_scoring
            if (
                not self.exam_mode.is_lesson
                or turn_exhausted
                or scoring is None
                or scoring.limit_border < 0
                or self.scalar.score < scoring.limit_border
            ):
                raise ValueError("invalid limit-border terminal state")
        else:
            raise ValueError("terminal phase requires terminal_reason")
        _nonempty(self.source_kind, "source_kind")

    @property
    def remaining_turns(self) -> int:
        if self.terminal:
            return 0
        return max(self.limit_turn + self.extra_turn - self.scalar.current_turn + 1, 0)

    @property
    def terminal(self) -> bool:
        return self.phase == PHASE_TERMINAL

    @property
    def is_clear(self) -> bool:
        clear_border = self.clear_border
        return (
            clear_border is not None
            and clear_border >= 0
            and self.scalar.score >= clear_border
        )

    @property
    def outcome(self) -> Plan2ExamOutcome | None:
        if not self.terminal:
            return None
        return evaluate_plan2_exam_outcome(
            self.scalar.score,
            -1 if self.clear_border is None else self.clear_border,
        )


def _finish_plan2_exam(
    state: Plan2NativeHorizonState,
    reason: Plan2ExamTerminalReason,
) -> Plan2NativeHorizonState:
    """Close the central scoring context and construct one terminal node."""

    if state.terminal:
        return state
    scalar = state.scalar
    scoring = scalar.battle_scoring
    if scoring is not None and not scoring.exam_end_complete:
        closed = close_plan2_exam_save_battle_turn(
            scoring,
            CurrentTurnBoundary.EXAM_END,
        )
        scalar = replace(scalar, battle_scoring=closed.after_boundary)
    # EXAM_END is a terminal boundary on the current active turn.  Unlike the
    # NEXT_TURN path, native does not increment CurrentTurn after logging the
    # last turn; the completion flag is the boundary marker.  Keep the scalar
    # and battle-scoring contexts on that same turn.
    return replace(
        state,
        scalar=scalar,
        plays_remaining=0,
        phase=PHASE_TERMINAL,
        command_queue=(),
        scheduled_gimmicks=(),
        terminal_reason=reason,
    )


def _finalize_plan2_lesson_limit(
    state: Plan2NativeHorizonState,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """LimitBorder caps score upstream and ends a lesson at the next boundary."""

    if state.terminal or not state.exam_mode.is_lesson:
        return state, ()
    scoring = state.scalar.battle_scoring
    if scoring is None:
        raise Plan2NativeHorizonError("lesson-scoring-context-missing")
    if scoring.limit_border < 0 or state.scalar.score < scoring.limit_border:
        return state, ()
    after = _finish_plan2_exam(
        state,
        Plan2ExamTerminalReason.LIMIT_BORDER,
    )
    assert after.outcome is not None
    return after, (
        "terminal:limit-border",
        f"outcome:{after.outcome.value}",
    )


def _execute_plan2_scheduled_gimmicks(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Invoke registered StartTurn hooks in serialized order before draw."""

    turn = state.scalar.current_turn
    stale = tuple(
        value for value in state.scheduled_gimmicks if value.turn < turn
    )
    if stale:
        raise Plan2NativeHorizonError(
            "scheduled-gimmick-phase-missed",
            ",".join(value.gimmick_effect_id for value in stale),
        )
    due = tuple(
        value for value in state.scheduled_gimmicks if value.turn == turn
    )
    if not due:
        return state, ()
    future = tuple(
        value for value in state.scheduled_gimmicks if value.turn > turn
    )
    after = replace(state, scheduled_gimmicks=future)
    trace: list[str] = []
    for scheduled in due:
        try:
            result = scheduled.hook.executor(after, catalog)
        except Exception as error:
            raise Plan2NativeHorizonError(
                "scheduled-gimmick-hook-failed-closed",
                f"{scheduled.hook.executor_id}:"
                f"{type(error).__name__}:{error}",
            ) from error
        if not isinstance(result, Plan2ScheduledGimmickHookResult):
            raise Plan2NativeHorizonError(
                "scheduled-gimmick-hook-result-invalid",
                scheduled.hook.executor_id,
            )
        if not isinstance(result.state, Plan2NativeHorizonState):
            raise Plan2NativeHorizonError(
                "scheduled-gimmick-hook-state-invalid",
                scheduled.hook.executor_id,
            )
        hooked = result.state
        if (
            hooked.exam_mode != after.exam_mode
            or hooked.scalar.current_turn != turn
            or hooked.phase != PHASE_MAIN
            or hooked.scheduled_gimmicks != after.scheduled_gimmicks
        ):
            raise Plan2NativeHorizonError(
                "scheduled-gimmick-hook-boundary-changed",
                scheduled.hook.executor_id,
            )
        after = hooked
        trace.append(
            "scheduled-gimmick:"
            f"turn={turn}:group={scheduled.gimmick_group_id}:"
            f"effect={scheduled.gimmick_effect_id}:"
            f"hook={scheduled.hook.executor_id}"
        )
        trace.extend(result.trace)
    return after, tuple(trace)


def execute_plan2_native_current_turn_gimmicks(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Execute and retire the scheduled gimmicks for ``state.current_turn``."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    return _execute_plan2_scheduled_gimmicks(state, catalog)


@dataclass(frozen=True, slots=True)
class Plan2NativeBootstrap:
    state: Plan2NativeHorizonState | None
    blockers: tuple[Plan2NativeBlocker, ...] = ()

    @property
    def simulation_ready(self) -> bool:
        return self.state is not None and not self.blockers


def _current_scalar_status_values(statuses: Sequence[object]) -> tuple[int, int, tuple[str, ...]]:
    review = 0
    aggressive = 0
    unresolved: list[str] = []
    for raw in statuses:
        if not isinstance(raw, str) or not raw:
            unresolved.append(json.dumps(raw, ensure_ascii=False, sort_keys=True))
            continue
        match = _REVIEW_STATUS.fullmatch(raw)
        if match:
            review = _plain_int(int(match.group("value")), "Review status")
            continue
        match = _AGGRESSIVE_STATUS.fullmatch(raw)
        if match:
            aggressive = _plain_int(int(match.group("value")), "Aggressive status")
            continue
        unresolved.append(raw)
    return review, aggressive, tuple(unresolved)


def bootstrap_plan2_native_local_save(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    draw_count: int = 3,
    hand_limit: int = 5,
    gimmick_hooks: Mapping[
        Plan2ScheduledGimmickHookKey,
        Plan2ScheduledGimmickHook,
    ]
    | None = None,
) -> Plan2NativeBootstrap:
    """Preserve a real settled Plan2 LocalSave and fail closed on opaque queues."""

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    exam = evidence.state
    root = exam.root_runtime
    if root is None:
        return Plan2NativeBootstrap(None, (Plan2NativeBlocker("local-save-root-runtime-missing"),))
    opaque = root.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):  # constructor boundary, retained for clarity
        return Plan2NativeBootstrap(None, (Plan2NativeBlocker("local-save-root-runtime-invalid"),))
    if opaque.get("planType") != _PLAN2_LOCAL_SAVE_VALUE:
        return Plan2NativeBootstrap(
            None,
            (Plan2NativeBlocker("local-save-not-plan2", repr(opaque.get("planType"))),),
        )
    try:
        exam_mode = resolve_plan2_exam_mode(
            exam.exam_type,
            exam.step_type_value,
        )
        scheduled_gimmicks = compile_plan2_scheduled_gimmicks(
            opaque.get("gimmickList"),
            hooks=gimmick_hooks,
            maximum_turn=exam.limit_turn + exam.extra_turn,
            completed_through_turn=exam.current_turn,
        )
    except Plan2ExamModeError as error:
        return Plan2NativeBootstrap(
            None,
            (
                Plan2NativeBlocker(
                    "local-save-exam-mode-invalid",
                    str(error),
                ),
            ),
        )
    try:
        runtime_digest = canonical_runtime_digest_aggregate(
            tuple(
                NativeOrderedCardInstance.from_local_save(card)
                for card in exam.all_card_instances
            )
        )
        zones = build_native_ordered_zone_state(
            evidence,
            runtime_evidence_digest=runtime_digest,
        )
    except (NativeOrderedZoneError, TypeError, ValueError) as error:
        code = error.code if isinstance(error, NativeOrderedZoneError) else "local-save-zone-bootstrap-failed"
        return Plan2NativeBootstrap(None, (Plan2NativeBlocker(code, str(error)),))

    raw_statuses = opaque.get("currentTurnStartStatusList", [])
    if not isinstance(raw_statuses, list):
        return Plan2NativeBootstrap(None, (Plan2NativeBlocker("local-save-status-list-invalid"),))
    review, aggressive, unresolved_statuses = _current_scalar_status_values(raw_statuses)
    try:
        battle_scoring = plan2_battle_scoring_context_from_exam_save(exam)
    except Plan2BattleScoringError as error:
        return Plan2NativeBootstrap(
            None,
            (Plan2NativeBlocker("local-save-battle-scoring-invalid", str(error)),),
        )
    scalar = Plan2State(
        current_turn=exam.current_turn,
        review=review,
        score=exam.score,
        block=exam.block,
        card_play_aggressive=aggressive,
        stamina=exam.stamina,
        max_stamina=exam.max_stamina,
        exam_card_play_count=exam.exam_card_play_count,
        turn_card_play_count=exam.turn_card_play_count,
        battle_scoring=battle_scoring,
    )
    state = Plan2NativeHorizonState(
        schema_version=PLAN2_NATIVE_HORIZON_SCHEMA_VERSION,
        scalar=scalar,
        zones=zones,
        limit_turn=exam.limit_turn,
        exam_mode=exam_mode,
        extra_turn=exam.extra_turn,
        draw_count=draw_count,
        hand_limit=hand_limit,
        plays_remaining=0 if exam.is_turn_card_play_end else 1,
        judge_parameter=exam.score,
        clear_border=battle_scoring.clear_border,
        scheduled_gimmicks=scheduled_gimmicks,
        opaque_status_queue=unresolved_statuses,
        source_kind="plan2-local-save-schema-v5",
    )
    blockers: list[Plan2NativeBlocker] = []
    if not root.command_list_is_empty:
        blockers.append(Plan2NativeBlocker("local-save-command-queue-unsettled"))
    restore_details: list[str] = []
    if unresolved_statuses:
        restore_details.append("opaque_statuses=" + ",".join(unresolved_statuses))
    # Active reference-backed listeners cannot be reconstructed merely from
    # display strings.  Retain a single blocker even if their display entry
    # was absent from currentTurnStartStatusList.
    raw_status = opaque.get("status", {})
    if isinstance(raw_status, Mapping) and raw_status.get("_effectList"):
        restore_details.append("active_reference_listeners=true")
    if restore_details:
        blockers.append(
            Plan2NativeBlocker(
                "local-save-runtime-restore-unsupported",
                ";".join(restore_details),
            )
        )
    return Plan2NativeBootstrap(state, tuple(blockers))


def bootstrap_plan2_native_master_deck(
    manifest: Plan2MasterInitialDeck,
    *,
    random_state: int,
    limit_turn: int,
    stamina: int,
    max_stamina: int,
    draw_count: int = 3,
    hand_limit: int = 5,
    extra_turn: int = 0,
) -> Plan2NativeBootstrap:
    """Build a deterministic full-deck root from Master identities.

    The seed and exam scalars are explicit runtime inputs.  Card effects are
    still supplied separately through :class:`Plan2NativeProgramCatalog`.
    """

    if not isinstance(manifest, Plan2MasterInitialDeck):
        raise TypeError("manifest must be Plan2MasterInitialDeck")
    _plain_int(random_state, "random_state")
    if random_state > 0xFFFFFFFF:
        raise ValueError("random_state is outside UInt32")
    _plain_int(limit_turn, "limit_turn", minimum=1)
    _plain_int(stamina, "stamina")
    _plain_int(max_stamina, "max_stamina")
    _plain_int(draw_count, "draw_count")
    _plain_int(hand_limit, "hand_limit", minimum=1)
    _plain_int(extra_turn, "extra_turn")
    if stamina > max_stamina:
        raise ValueError("stamina cannot exceed max_stamina")
    if draw_count > hand_limit or draw_count > len(manifest.card_refs):
        raise ValueError("initial draw exceeds Hand or full-deck capacity")

    try:
        item_rules = tuple(load_item_rule(item_id) for item_id in manifest.item_ids)
        item_compilation = compile_plan2_native_item_rules(item_rules)
    except (KeyError, OSError, TypeError, ValueError) as error:
        return Plan2NativeBootstrap(
            None,
            (
                Plan2NativeBlocker(
                    "master-item-load-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    if not item_compilation.supported:
        return Plan2NativeBootstrap(
            None,
            tuple(
                Plan2NativeBlocker("master-item-runtime-unsupported", value)
                for value in item_compilation.blockers
            ),
        )
    assert item_compilation.runtime is not None
    item_runtime = item_compilation.runtime

    cards = tuple(
        LocalSaveExamCard(
            zone_order=index,
            guid=f"master-{index:03d}-{card_id}",
            card_id=card_id,
            base_upgrade=upgrade,
            temporary_upgrade=0,
            effective_upgrade=upgrade,
            support_upgrade_ids=(),
            fixed_deck_order=0,
            runtime_state=empty_local_save_exam_card_runtime_state(),
        )
        for index, (card_id, upgrade) in enumerate(manifest.card_refs)
    )
    shuffled, after_shuffle_state = shuffle_unfixed_pool(
        cards,
        random_state,
        fixed_deck_orders=(0,) * len(cards),
    )
    shuffled_cards = tuple(
        replace(card, zone_order=index)
        for index, card in enumerate(shuffled)
    )
    manifest_payload = {
        "idol_card_id": manifest.idol_card_id,
        "produce_id": manifest.produce_id,
        "produce_default_deck_id": manifest.produce_default_deck_id,
        "character_deck_id": manifest.character_deck_id,
        "card_refs": [list(ref) for ref in manifest.card_refs],
        "item_ids": list(manifest.item_ids),
        "random_state": random_state,
    }
    source_bytes = json.dumps(
        manifest_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    exam = LocalSaveExamState(
        character_id=manifest.idol_card_id.split("-")[1],
        setting_id="master-plan2-native-horizon-fixture",
        exam_type=1,
        step_type_value=16,
        phase=6,
        current_turn=1,
        limit_turn=limit_turn,
        remain_turn=limit_turn + extra_turn,
        extra_turn=extra_turn,
        score=0,
        stamina=stamina,
        max_stamina=max_stamina,
        block=0,
        vocal_bonus_permille=1000,
        dance_bonus_permille=1000,
        visual_bonus_permille=1000,
        turn_parameter_types=tuple((index % 3) + 1 for index in range(limit_turn)),
        turn_card_play_count=0,
        exam_card_play_count=0,
        is_turn_card_play_end=False,
        random_state=after_shuffle_state,
        turn_use_support_ids=(),
        zones=LocalSaveExamZones(
            hand=(),
            deck=shuffled_cards,
            grave=(),
            lost=(),
            hold=(),
        ),
        root_runtime=empty_local_save_exam_root_runtime_state(),
    )
    evidence = AuditionLocalSaveStateEvidence(
        schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id=f"master-fixture-{manifest.idol_card_id}",
        step_context_id=f"{manifest.produce_id}:master-plan2-initial-deck",
        step_context_digest=source_digest,
        session_transition_id="master-fixture-initial-shuffle",
        zone_checkpoint_digest=source_digest,
        source_path=(
            f"Master/IdolCard/{manifest.idol_card_id}/"
            f"{manifest.produce_default_deck_id}+{manifest.character_deck_id}"
        ),
        source_sha256=source_digest,
        source_size=len(source_bytes),
        source_type=EXAM_SAVE_DATA_SOURCE_TYPE,
        envelope_version=0,
        state=exam,
    )
    instances = tuple(
        NativeOrderedCardInstance.from_local_save(card)
        for card in exam.all_card_instances
    )
    zones = build_native_ordered_zone_state(
        evidence,
        runtime_evidence_digest=canonical_runtime_digest_aggregate(instances),
    )
    initial_draw = zones.draw_to_hand(draw_count)
    state = Plan2NativeHorizonState(
        schema_version=PLAN2_NATIVE_HORIZON_SCHEMA_VERSION,
        scalar=Plan2State(
            current_turn=1,
            stamina=stamina,
            max_stamina=max_stamina,
            next_status_uid=item_runtime.next_status_uid,
        ),
        zones=initial_draw.state,
        limit_turn=limit_turn,
        extra_turn=extra_turn,
        draw_count=draw_count,
        hand_limit=hand_limit,
        plays_remaining=1,
        item_runtime=item_runtime,
        source_kind=f"master-initial-deck:{manifest.idol_card_id}",
    )
    return Plan2NativeBootstrap(state)


@dataclass(frozen=True, slots=True)
class Plan2NativeAction:
    kind: str
    card_guid: str = ""

    def __post_init__(self) -> None:
        if self.kind not in {"play", "end_turn"}:
            raise ValueError(f"unsupported action kind: {self.kind}")
        if self.kind == "play":
            _nonempty(self.card_guid, "card_guid")
        elif self.card_guid:
            raise ValueError("end_turn cannot carry card_guid")

    @property
    def action_id(self) -> str:
        return ACTION_END_TURN if self.kind == "end_turn" else f"PLAY:{self.card_guid}"


@dataclass(frozen=True, order=True, slots=True)
class Plan2NativeDrinkAction:
    """Drink use bound to an ordered slot and stable runtime identity."""

    slot_index: int
    instance_id: str
    drink_id: str
    selected_card_guid: str = ""

    def __post_init__(self) -> None:
        _plain_int(self.slot_index, "drink action slot_index")
        _nonempty(self.instance_id, "drink action instance_id")
        _nonempty(self.drink_id, "drink action drink_id")
        if not isinstance(self.selected_card_guid, str):
            raise TypeError("selected_card_guid must be text")

    @property
    def kind(self) -> str:
        return "drink"

    @property
    def action_id(self) -> str:
        suffix = (
            ""
            if not self.selected_card_guid
            else f":SELECT:{self.selected_card_guid}"
        )
        return (
            f"DRINK:{self.slot_index}:{self.instance_id}:{self.drink_id}"
            f"{suffix}"
        )


Plan2NativeOfflineAction: TypeAlias = Plan2NativeAction | Plan2NativeDrinkAction


Plan2NativeOperationReceipt = CardPlayCountOperationReceipt

def _require_plan2_operation_receipt_scope(
    receipts: tuple[Plan2NativeOperationReceipt, ...],
    *,
    label: str,
) -> None:
    if any(
        value.owner not in PLAN2_CARD_PLAY_COUNT_RECEIPT_OWNERS
        for value in receipts
    ):
        raise ValueError(
            f"{label} must use Plan2 card play-count owners"
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeTransition:
    before: Plan2NativeHorizonState
    action: Plan2NativeOfflineAction
    after: Plan2NativeHorizonState | None
    trace: tuple[str, ...] = ()
    blockers: tuple[Plan2NativeBlocker, ...] = ()
    operation_receipts: tuple[Plan2NativeOperationReceipt, ...] = ()

    def __post_init__(self) -> None:
        receipts = tuple(self.operation_receipts)
        if any(
            not isinstance(value, Plan2NativeOperationReceipt)
            for value in receipts
        ):
            raise TypeError(
                "operation_receipts must contain Plan2NativeOperationReceipt"
            )
        _require_plan2_operation_receipt_scope(
            receipts,
            label="operation_receipts",
        )
        object.__setattr__(self, "operation_receipts", receipts)

    @property
    def supported(self) -> bool:
        return self.after is not None and not self.blockers


@dataclass(frozen=True, slots=True)
class Plan2NativePlayability:
    card_guid: str
    playable: bool
    effective_cost: int | None = None
    blockers: tuple[Plan2NativeBlocker, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.card_guid, "card_guid")
        if type(self.playable) is not bool:
            raise TypeError("playable must be boolean")
        if self.effective_cost is not None:
            _plain_int(self.effective_cost, "effective_cost")
        if self.playable == bool(self.blockers):
            raise ValueError("playable must be true exactly when blockers are empty")


def _program_for(
    catalog: Plan2NativeProgramCatalog,
    card: NativeOrderedCardInstance,
) -> Plan2NativeCardProgram:
    program = catalog.get(card)
    if program is None:
        raise Plan2NativeHorizonError(
            "card-program-unsupported",
            f"{card.card_id}@{card.effective_upgrade}",
        )
    return program


def materialize_plan2_native_card_program(
    catalog: Plan2NativeProgramCatalog,
    card: NativeOrderedCardInstance,
) -> Plan2NativeCardProgram:
    """Bind one GUID's complete customization before legality and execution."""

    program = _program_for(catalog, card)
    executor = program.native_playing_executor
    database_value = getattr(executor, "runtime_customization_database", "")
    database = (
        Path(database_value)
        if isinstance(database_value, str) and database_value
        else DEFAULT_MASTER_DATABASE
    )
    try:
        materializer = getattr(executor, "materialize_runtime_customization", None)
        if callable(materializer):
            executor = materializer(card.runtime_state, database=database)
            customization = getattr(
                executor,
                "runtime_customization_contract",
                None,
            )
            if not isinstance(customization, Plan3NativeRuntimeCustomization):
                raise TypeError(
                    "Playing materializer did not bind a customization contract"
                )
        else:
            customization = parse_plan2_native_runtime_customization(
                card.card_id,
                card.effective_upgrade,
                card.runtime_state,
                database=database,
            )
            if customization.customize_ids:
                raise ValueError("runtime-customization-playing-owner-missing")
    except (Plan3NativeStateError, TypeError, ValueError) as error:
        raise Plan2NativeHorizonError(
            "runtime-customization-materialization-failed-closed",
            f"{card.guid}:{type(error).__name__}:{error}",
        ) from error
    reduction = customization.cost_reduce
    if reduction == 0:
        return replace(program, native_playing_executor=executor)
    native_cost = program.native_cost
    if (
        native_cost is None
        or native_cost.cost_type != COST_STAMINA
        or native_cost.penetrate
        or native_cost.force_stamina > 0
    ):
        raise Plan2NativeHorizonError(
            "runtime-customization-cost-reduce-target-unsupported",
            (
                f"{card.guid}:{card.card_id}:"
                f"{None if native_cost is None else native_cost.cost_type}"
            ),
        )
    effective = get_stamina_cost(
        native_cost.base_cost,
        penetrate=False,
        master_grow=(
            StaminaGrowEffect(StaminaGrowEffectType.COST_REDUCE, reduction),
        ),
    )
    return replace(
        program,
        cost_value=effective,
        native_cost=replace(
            native_cost,
            base_cost=effective,
            stamina=effective,
        ),
        native_playing_executor=executor,
    )


def _pay_cost(
    state: Plan2State,
    program: Plan2NativeCardProgram,
    *,
    pay_cost: bool,
    modifiers: Plan2StaminaModifierRuntime,
    review_consumption_sum: int,
    origin: PlayOrigin = PlayOrigin.NORMAL,
    search_override: int | None = None,
) -> tuple[Plan2State, int, tuple[str, ...]]:
    if not pay_cost:
        return state, review_consumption_sum, (f"{origin.value}:cost-free",)
    native_cost = program.native_cost
    if native_cost is None:
        if program.cost_type == "none":
            return state, review_consumption_sum, ("cost:none",)
        native_cost = Plan2NativeCardCost(
            card_id=program.card_id,
            upgrade=program.upgrade,
            cost_type=COST_STAMINA,
            base_cost=program.cost_value,
            stamina=program.cost_value,
        )
    resources = Plan2CostResources(
        stamina=state.stamina,
        max_stamina=state.max_stamina,
        block=state.block,
        review=state.review,
        motivation=state.card_play_aggressive,
        review_exists=state.review > 0,
        review_consumption_sum=review_consumption_sum,
    )
    payment = pay_plan2_native_cost(
        Plan2NativeCostRequest(
            cost=native_cost,
            resources=resources,
            modifiers=modifiers,
            origin=origin,
            search_override=search_override,
        )
    )
    if not payment.committed:
        raise Plan2NativeHorizonError(
            "illegal-action-cost-rejected",
            payment.predicate.reason or native_cost.cost_type,
        )
    after_resources = payment.resources_after
    return (
        replace(
            state,
            stamina=after_resources.stamina,
            block=after_resources.block,
            review=after_resources.review,
            card_play_aggressive=after_resources.motivation,
        ),
        after_resources.review_consumption_sum,
        payment.trace,
    )


def _search_stamina_payment(program, card, runtime, *, simulate):
    # ExamSequence.ConsumeCardCost branches on the card's cost type before
    # querying this status (APK 0x7ED04BC, 0x7ED0620). Buff-cost cards use a
    # separate native status family and cannot consume the stamina override.
    cost_type = program.native_cost.cost_type if program.native_cost is not None else (
        COST_STAMINA if program.cost_type != "none" else None
    )
    if cost_type != COST_STAMINA or not runtime.statuses:
        return None
    return resolve_plan2_search_play_card_stamina_payment(
        runtime, _plan3_card_from_ordered(card), program.cost_value, simulate=simulate,
    )


def evaluate_plan2_native_playability(
    state: Plan2NativeHorizonState,
    card_guid: str,
    catalog: Plan2NativeProgramCatalog,
) -> Plan2NativePlayability:
    """Evaluate a normal Hand action using the same exact payment boundary."""

    _nonempty(card_guid, "card_guid")
    card = next((value for value in state.zones.hand if value.guid == card_guid), None)
    if state.terminal:
        return Plan2NativePlayability(
            card_guid,
            False,
            blockers=(Plan2NativeBlocker("terminal-state-has-no-actions"),),
        )
    if card is None:
        return Plan2NativePlayability(
            card_guid,
            False,
            blockers=(Plan2NativeBlocker("card-guid-not-in-hand", card_guid),),
        )
    if state.opaque_status_queue:
        return Plan2NativePlayability(
            card_guid,
            False,
            blockers=(
                Plan2NativeBlocker(
                    "opaque-status-queue-unresolved",
                    ",".join(state.opaque_status_queue),
                ),
            ),
        )
    try:
        program = materialize_plan2_native_card_program(catalog, card)
    except Plan2NativeHorizonError as error:
        return Plan2NativePlayability(
            card_guid,
            False,
            blockers=(error.blocker,),
        )
    if state.plays_remaining < 1:
        return Plan2NativePlayability(
            card_guid,
            False,
            blockers=(Plan2NativeBlocker("illegal-action-no-playable-count", card_guid),),
        )
    if program.native_horizon_predicate is not None:
        try:
            admitted = program.native_horizon_predicate(state, "ordinary")
        except (OverflowError, TypeError, ValueError) as error:
            return Plan2NativePlayability(
                card_guid,
                False,
                blockers=(
                    Plan2NativeBlocker(
                        "card-play-predicate-failed-closed",
                        f"{program.native_horizon_predicate_id}:"
                        f"{type(error).__name__}:{error}",
                    ),
                ),
            )
        if type(admitted) is not bool:
            return Plan2NativePlayability(
                card_guid,
                False,
                blockers=(
                    Plan2NativeBlocker(
                        "card-play-predicate-failed-closed",
                        f"{program.native_horizon_predicate_id}:non-boolean-result",
                    ),
                ),
            )
        if not admitted:
            return Plan2NativePlayability(
                card_guid,
                False,
                blockers=(
                    Plan2NativeBlocker(
                        "card-play-condition-not-met",
                        program.native_horizon_predicate_id,
                    ),
                ),
            )
    elif program.native_play_predicate is not None:
        try:
            admitted = program.native_play_predicate(state.scalar)
        except (OverflowError, TypeError, ValueError) as error:
            return Plan2NativePlayability(
                card_guid,
                False,
                blockers=(
                    Plan2NativeBlocker(
                        "card-play-predicate-failed-closed",
                        f"{program.native_play_predicate_id}:"
                        f"{type(error).__name__}:{error}",
                    ),
                ),
            )
        if type(admitted) is not bool:
            return Plan2NativePlayability(
                card_guid,
                False,
                blockers=(
                    Plan2NativeBlocker(
                        "card-play-predicate-failed-closed",
                        f"{program.native_play_predicate_id}:non-boolean-result",
                    ),
                ),
            )
        if not admitted:
            return Plan2NativePlayability(
                card_guid,
                False,
                blockers=(
                    Plan2NativeBlocker(
                        "card-play-condition-not-met",
                        program.native_play_predicate_id,
                    ),
                ),
            )
    try:
        search_payment = _search_stamina_payment(program, card,
            state.search_play_card_stamina_runtime, simulate=True)
        after, _review_consumption_sum, _trace = _pay_cost(
            state.scalar,
            program,
            pay_cost=True,
            modifiers=state.stamina_modifiers,
            review_consumption_sum=state.review_consumption_sum,
            search_override=(search_payment.selected_stamina_cost
                if search_payment is not None and search_payment.cost_source == "search-override" else None),
        )
    except Plan2NativeHorizonError as error:
        return Plan2NativePlayability(card_guid, False, blockers=(error.blocker,))
    paid = (
        state.scalar.stamina
        + state.scalar.block
        + state.scalar.review
        + state.scalar.card_play_aggressive
        - after.stamina
        - after.block
        - after.review
        - after.card_play_aggressive
    )
    return Plan2NativePlayability(card_guid, True, effective_cost=paid)


def _apply_scalar_program(state: Plan2State, program: Plan2NativeCardProgram, *, status_child_context=None) -> Plan2State:
    after = state
    if program.native_direct_executor is not None:
        after = program.native_direct_executor(after)
        if not isinstance(after, Plan2State):
            raise TypeError("native_direct_executor must return Plan2State")
    if (status_child_context is not None and status_child_context.aggressive_additive_runtime is not None
            and (program.aggressive_delta or after.card_play_aggressive > state.card_play_aggressive)):
        raise Plan2NativeHorizonError("aggressive-additive-legacy-direct-gain-unbound", program.card_id)
    if program.score_delta:
        after = apply_plan2_state_score(
            after,
            program.score_delta,
            score_kind=Plan2ScoreKind.LESSON,
            integration_point=Plan2ScoreIntegrationPoint.SCALAR_DIRECT,
            slump=False,
        ).after
    return replace(
        after,
        review=_i32_add(after.review, program.review_delta, "review"),
        block=_i32_add(after.block, program.block_delta, "block"),
        card_play_aggressive=_i32_add(
            after.card_play_aggressive,
            program.aggressive_delta,
            "card_play_aggressive",
        ),
        stamina=min(after.max_stamina, after.stamina + program.stamina_recover),
    )


_STATUS_CHILD_REVIEW = "ProduceExamEffectType_ExamReview"
_STATUS_CHILD_BLOCK = "ProduceExamEffectType_ExamBlock"
_STATUS_CHILD_AGGRESSIVE = "ProduceExamEffectType_ExamCardPlayAggressive"
_STATUS_CHILD_LESSON = "ProduceExamEffectType_ExamLesson"
_STATUS_CHILD_LESSON_REVIEW = "ProduceExamEffectType_ExamLessonDependExamReview"
_STATUS_CHILD_LESSON_BLOCK = "ProduceExamEffectType_ExamLessonDependBlock"
_STATUS_CHILD_LESSON_AGGRESSIVE = EFFECT_LESSON_DEPEND_AGGRESSIVE
_EXECUTABLE_STATUS_CHILD_TYPES = frozenset(
    {
        _STATUS_CHILD_REVIEW,
        _STATUS_CHILD_BLOCK,
        _STATUS_CHILD_AGGRESSIVE,
        _STATUS_CHILD_LESSON,
        _STATUS_CHILD_LESSON_REVIEW,
        _STATUS_CHILD_LESSON_BLOCK,
        _STATUS_CHILD_LESSON_AGGRESSIVE,
    }
)


def _status_event_with_scalar(
    event: Plan2NativeStatusEnchantEvent,
    scalar: Plan2State,
    *,
    remaining_turns: int,
) -> Plan2NativeStatusEnchantEvent:
    return replace(
        event,
        review=scalar.review,
        aggressive=scalar.card_play_aggressive,
        remaining_turns=remaining_turns,
        block=scalar.block,
        stamina=scalar.stamina,
        max_stamina=scalar.max_stamina,
    )


def _apply_aggressive_gain(scalar, *, value, count=1, effect_id, context=None, play_origin="normal"):
    runtime = None if context is None else context.aggressive_additive_runtime
    if runtime is None:
        return replace(scalar, card_play_aggressive=_i32_add(scalar.card_play_aggressive, value * count, "card_play_aggressive"))
    try:
        after = apply_plan2_native_bound_aggressive_gain(runtime, aggressive=scalar.card_play_aggressive,
            value=value, count=count, effect_id=effect_id, play_origin=play_origin)
    except (TypeError, ValueError) as error:
        raise Plan2NativeHorizonError("aggressive-additive-gain-unbound", f"{effect_id}:{error}") from error
    context.aggressive_additive_runtime = after
    return replace(scalar, card_play_aggressive=after.aggressive)


def _context_aggressive_runtime(context, scalar, fallback=None):
    runtime = context.aggressive_additive_runtime if context.aggressive_additive_runtime is not None else fallback
    return None if runtime is None else replace(runtime, aggressive=scalar.card_play_aggressive)


def _apply_status_enchant_child(
    scalar: Plan2State,
    command: Plan2StatusEnchantChildCommand,
    *,
    review_dynamic: Plan2NativeReviewDynamicRuntime | None = None,
    play_origin: str = "normal",
    status_child_context: Plan2NativeStatusChildContext | None = None,
) -> tuple[Plan2State, str, int]:
    child = command.child
    if child.effect_type not in _EXECUTABLE_STATUS_CHILD_TYPES:
        raise Plan2NativeHorizonError(
            "status-enchant-child-runtime-unbound",
            f"{child.effect_type}:{child.effect_id}",
        )
    if child.value1 < 0 or child.value2 != 0 or child.turn != 0 or child.count < 0:
        raise Plan2NativeHorizonError(
            "status-enchant-child-shape-unbound",
            child.effect_id,
        )
    repetitions = max(1, child.count)
    before = scalar
    if child.effect_type == _STATUS_CHILD_REVIEW:
        if review_dynamic is None:
            scalar = replace(
                scalar,
                review=_i32_add(
                    scalar.review,
                    child.value1 * repetitions,
                    "review",
                ),
            )
        else:
            if review_dynamic.review != scalar.review:
                raise Plan2NativeHorizonError(
                    "review-dynamic-scalar-snapshot-mismatch",
                    f"runtime={review_dynamic.review};scalar={scalar.review}",
                )
            scalar = replace(
                scalar,
                review=apply_plan2_native_review_dynamic_review_gain(
                    review_dynamic,
                    value=child.value1,
                    count=repetitions,
                ),
            )
        return scalar, child.effect_type, scalar.review - before.review
    if child.effect_type == _STATUS_CHILD_AGGRESSIVE:
        scalar = _apply_aggressive_gain(scalar, value=child.value1, count=repetitions,
            effect_id=child.effect_id, context=status_child_context, play_origin=play_origin)
        return (
            scalar,
            child.effect_type,
            scalar.card_play_aggressive - before.card_play_aggressive,
        )
    if child.effect_type == _STATUS_CHILD_BLOCK:
        for _ in range(repetitions):
            delta = calculate_add_block(
                child.value1,
                is_buff_active=True,
                status=AddBlockStatus(aggressive=scalar.card_play_aggressive),
                settings=AddBlockSettings(),
            )
            scalar = replace(scalar, block=_i32_add(scalar.block, delta, "block"))
        return scalar, child.effect_type, scalar.block - before.block
    aggressive_after = scalar.card_play_aggressive
    if child.effect_type == _STATUS_CHILD_LESSON:
        requested = child.value1
    elif child.effect_type == _STATUS_CHILD_LESSON_REVIEW:
        requested = get_ratio_effect_int_value(
            scalar.review, child.value1, is_ceil=True
        )
    elif child.effect_type == _STATUS_CHILD_LESSON_AGGRESSIVE:
        effect = LessonDependAggressiveEffect(
            effect_id=child.effect_id,
            effect_type=child.effect_type,
            value1=child.value1,
            value2=child.value2,
            count=child.count,
            turn=child.turn,
            effect_group_ids=child.effect_group_ids,
        )
        execution = execute_plan2_native_lesson_depend_aggressive(
            build_plan2_native_lesson_depend_aggressive_effect_handoff(
                effect,
                command.source_guid,
                play_origin,
                scalar.card_play_aggressive,
            ),
            LessonDependAggressiveRuntime(
                current_card_play_aggressive=scalar.card_play_aggressive,
                application_status=scalar.parameter_application_status(
                    slump=False
                ),
            ),
        )
        requested = execution.hits[0].requested
        aggressive_after = execution.card_play_aggressive_after
    else:
        requested = get_ratio_effect_int_value(
            scalar.block, child.value1, is_ceil=True
        )
    if review_dynamic is not None:
        review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
        lesson = handoff_plan2_native_review_dynamic_lesson(
            review_dynamic,
            Plan2NativeReviewDynamicLessonRequest(
                value=requested,
                count=repetitions,
                aggressive_at_execution=scalar.card_play_aggressive,
                adding_status=AddingParameterStatus(
                    aggressive=scalar.card_play_aggressive
                ),
                settings=AddingParameterSettings(),
                application_status=scalar.parameter_application_status(
                    slump=False
                ),
                play_origin=play_origin,
            ),
        )
        if not lesson.executable:
            raise Plan2NativeHorizonError(
                "review-dynamic-lesson-failed-closed",
                ",".join(lesson.unresolved),
            )
        return (
            replace(
                carry_plan2_state_application_status(
                    scalar,
                    lesson.final_application_status,
                ),
                card_play_aggressive=aggressive_after,
            ),
            "",
            0,
        )
    for _ in range(repetitions):
        scalar = apply_plan2_state_score(
            scalar,
            requested,
            score_kind=Plan2ScoreKind.LESSON,
            integration_point=Plan2ScoreIntegrationPoint.STATUS_CHILD,
            slump=False,
        ).after
    return replace(
        scalar,
        card_play_aggressive=aggressive_after,
    ), "", 0


def _apply_generated_hand_arrivals(
    scalar: Plan2State,
    before_zones: NativeOrderedZoneState,
    after_zones: NativeOrderedZoneState,
    context: Plan2NativeStatusChildContext,
    *,
    reason: str,
) -> tuple[Plan2State, NativeOrderedZoneState, tuple[str, ...]]:
    """Execute exact per-GUID Hand callbacks after an authoritative move."""

    before_hand = {value.guid for value in before_zones.hand}
    arrivals = tuple(
        value for value in after_zones.hand if value.guid not in before_hand
    )
    if not arrivals:
        return scalar, after_zones, ()
    programs = {value.ref: value for value in context.hand_move_programs}
    current_zones = after_zones
    trace: list[str] = []
    for arrived in arrivals:
        program = programs.get((arrived.card_id, arrived.effective_upgrade))
        if program is None:
            continue
        source = next(
            (
                zone_name
                for zone_name in ("deck", "grave", "lost")
                if any(
                    value.guid == arrived.guid
                    for value in getattr(before_zones, zone_name)
                )
            ),
            None,
        )
        if source not in {"deck", "grave"}:
            raise Plan2NativeHorizonError(
                "generated-hand-move-source-unbound",
                f"{arrived.guid}:{source or 'created'}",
            )
        native_after = _plan3_state_for_status_children(
            current_zones,
            total_effect_draw_card_count=context.total_effect_draw_card_count,
        )
        moved = native_after.card_by_guid(arrived.guid)
        native_before = replace(
            native_after,
            hand=tuple(
                value for value in native_after.hand if value.guid != arrived.guid
            ),
            **{
                source: (*getattr(native_after, source), moved),
            },
        )
        if program.executor == "block-five":
            exact = execute_plan2_generated_hand_move_block(
                program,
                FinalCardRuntime(
                    native_state=native_before,
                    stamina=scalar.stamina,
                    block=scalar.block,
                    block_consumption_sum_count=(
                        context.block_consumption_sum_count
                    ),
                    application_status=ParameterApplicationStatus(
                        judge_parameter=scalar.score,
                    ),
                    add_block_status=AddBlockStatus(
                        aggressive=scalar.card_play_aggressive,
                    ),
                    global_play_count=scalar.exam_card_play_count,
                    turn_play_count=scalar.turn_card_play_count,
                ),
                context.hand_move_turn,
                guid=arrived.guid,
                source_zone=source,
                reason=reason,
            )
            if not exact.executable:
                raise Plan2NativeHorizonError(
                    "generated-hand-move-failed-closed",
                    f"{arrived.guid}:{','.join(exact.unresolved)}",
                )
            context.hand_move_turn = exact.after_turn
            assert exact.exact is not None
            if (context.aggressive_additive_runtime is not None
                    and exact.exact.after.aggressive_value > scalar.card_play_aggressive):
                raise Plan2NativeHorizonError("aggressive-additive-generated-hand-move-gain-unbound", arrived.guid)
            scalar = replace(scalar, block=exact.exact.after.block)
            context.block_consumption_sum_count = (
                exact.exact.after.block_consumption_sum_count
            )
        else:
            exact = execute_plan2_generated_hand_move_aggressive(
                program,
                HandMoveCommitEvent(
                    native_before,
                    native_after,
                    arrived.guid,
                    source,
                    "hand",
                    reason,
                ),
                HandMoveAggressiveRuntimeState(
                    native_before,
                    aggressive_value=scalar.card_play_aggressive,
                    global_card_play_count=scalar.exam_card_play_count,
                    turn_card_play_count=scalar.turn_card_play_count,
                ),
                context.hand_move_turn,
            )
            if not exact.executable:
                raise Plan2NativeHorizonError(
                    "generated-hand-move-failed-closed",
                    f"{arrived.guid}:{','.join(exact.unresolved)}",
                )
            context.hand_move_turn = exact.after_turn
            assert exact.exact is not None
            scalar = replace(
                scalar,
                card_play_aggressive=exact.exact.after.aggressive_value,
            )
        if arrived.guid in context.hand_move_turn.used_guids:
            marked_native = replace(
                native_after,
                hand=tuple(
                    replace(value, move_effect_used_in_turn=True)
                    if value.guid == arrived.guid
                    else value
                    for value in native_after.hand
                ),
            )
            current_zones = _ordered_after_status_native(
                current_zones,
                marked_native,
            )
        trace.append(
            f"generated-hand-move:{arrived.guid}:{program.executor}:{source}->hand"
        )
    if context.hand_add_support_callback is not None:
        current_zones, used_support_ids, support_trace = (
            context.hand_add_support_callback(
                current_zones,
                tuple(value.guid for value in arrivals),
                context.turn_used_support_ids,
            )
        )
        context.turn_used_support_ids = used_support_ids
        trace.extend(support_trace)
    context.zones = current_zones
    return scalar, current_zones, tuple(trace)


def _reset_generated_hand_move_flags(
    zones: NativeOrderedZoneState,
) -> NativeOrderedZoneState:
    def reset(card: NativeOrderedCardInstance) -> NativeOrderedCardInstance:
        if not card.runtime_state.is_move_produce_exam_effect_use_in_turn:
            return card
        runtime = replace(
            card.runtime_state,
            is_move_produce_exam_effect_use_in_turn=False,
        )
        digest = _runtime_state_digest(
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            support_upgrade_ids=card.support_upgrade_ids,
            fixed_deck_order=card.fixed_deck_order,
            runtime_state=runtime,
        )
        return replace(card, runtime_state=runtime, runtime_state_digest=digest)

    hand = tuple(reset(value) for value in zones.hand)
    deck = tuple(reset(value) for value in zones.deck)
    grave = tuple(reset(value) for value in zones.grave)
    lost = tuple(reset(value) for value in zones.lost)
    pending = None if zones.pending_played is None else reset(zones.pending_played)
    universe_by_guid = {
        value.guid: value for value in (*hand, *deck, *grave, *lost)
    }
    if pending is not None:
        universe_by_guid[pending.guid] = pending
    return replace(
        zones,
        card_universe=tuple(
            sorted(universe_by_guid.values(), key=lambda value: value.guid)
        ),
        hand=hand,
        deck=deck,
        grave=grave,
        lost=lost,
        pending_played=pending,
    )


def dispatch_plan2_native_status_enchant_event(
    scalar: Plan2State,
    runtime: Plan2NativeStatusEnchantRuntime,
    event: Plan2NativeStatusEnchantEvent,
    *,
    remaining_turns: int,
    command_limit: int = 64,
    review_dynamic: Plan2NativeReviewDynamicRuntime | None = None,
    status_child_compilation: Plan2NativeStatusChildCompilation | None = None,
    status_child_context: Plan2NativeStatusChildContext | None = None,
    default_status_child_input: Plan2NativeStatusChildInput | None = None,
) -> tuple[Plan2State, Plan2NativeStatusEnchantRuntime, tuple[str, ...]]:
    """Commit listener counts, then execute supported children in queue order."""

    pending = [_status_event_with_scalar(event, scalar, remaining_turns=remaining_turns)]
    after_scalar = scalar
    after_runtime = runtime
    trace: list[str] = []
    executed = 0
    while pending:
        next_event = pending.pop(0)
        transition = resolve_plan2_native_status_enchant_event(
            after_runtime,
            _status_event_with_scalar(
                next_event,
                after_scalar,
                remaining_turns=remaining_turns,
            ),
        )
        if not transition.executable:
            raise Plan2NativeHorizonError(
                "status-enchant-event-unresolved",
                ",".join(transition.unresolved),
            )
        exact_bindings = ()
        ordered_bindings: tuple[object | None, ...] = ()
        if status_child_compilation is not None and transition.commands:
            resolved_bindings: list[object | None] = []
            for command in transition.commands:
                try:
                    binding = status_child_compilation.catalog.for_command(
                        command
                    )
                except (KeyError, ValueError):
                    binding = None
                resolved_bindings.append(binding)
            ordered_bindings = tuple(resolved_bindings)
            if all(binding is not None for binding in ordered_bindings):
                exact_bindings = tuple(ordered_bindings)
        mixed_bindings = bool(
            ordered_bindings
            and any(binding is None for binding in ordered_bindings)
            and any(binding is not None for binding in ordered_bindings)
        )
        if mixed_bindings:
            if status_child_context is None or status_child_compilation is None:
                raise Plan2NativeHorizonError(
                    "status-child-context-missing",
                    transition.commands[0].installer_effect_id,
                )
            zone_operations = {
                "card-draw",
                "card-move",
                "card-create-id",
                "force-play",
            }
            for command, binding in zip(
                transition.commands,
                ordered_bindings,
                strict=True,
            ):
                if binding is None:
                    catalog_matches = tuple(
                        candidate
                        for candidate in status_child_compilation.catalog.bindings
                        if candidate.child.effect_id == command.child.effect_id
                    )
                    if catalog_matches:
                        raise Plan2NativeHorizonError(
                            "status-child-binding-unbound",
                            command.child.effect_id,
                        )
                    continue
                if binding.operation in zone_operations:
                    raise Plan2NativeHorizonError(
                        "status-child-mixed-zone-operation-unbound",
                        f"{binding.operation}:{command.child.effect_id}",
                    )

            staged_context = replace(
                status_child_context,
                inputs=list(status_child_context.inputs),
            )
            staged_scalar = after_scalar
            staged_review_dynamic = review_dynamic
            staged_trace: list[str] = []
            staged_changes: list[
                tuple[Plan2StatusEnchantChildCommand, str, int]
            ] = []
            supplied = (
                staged_context.inputs[0]
                if staged_context.inputs
                else default_status_child_input
                or Plan2NativeStatusChildInput()
            )
            supplied = replace(
                supplied,
                hand_limit=(
                    staged_context.hand_limit
                    if supplied.hand_limit is None
                    else supplied.hand_limit
                ),
                playing_guid=(
                    staged_context.playing_guid
                    if supplied.playing_guid is None
                    and staged_context.playing_guid
                    else supplied.playing_guid
                ),
            )
            typed_executed = False
            for command, binding in zip(
                transition.commands,
                ordered_bindings,
                strict=True,
            ):
                before_command = staged_scalar
                if binding is None:
                    staged_scalar, changed_type, difference = (
                        _apply_status_enchant_child(
                            staged_scalar,
                            command,
                            review_dynamic=staged_review_dynamic,
                            play_origin=event.play_origin,
                            status_child_context=staged_context,
                        )
                    )
                    if staged_review_dynamic is not None:
                        staged_review_dynamic = _sync_review_dynamic(
                            staged_review_dynamic,
                            staged_scalar.review,
                        )
                    staged_trace.append(
                        "status-child-fallback:"
                        f"{command.status_uid}:{command.child.effect_id}"
                    )
                else:
                    review_state = staged_context.review_state
                    if review_state is None:
                        review_state = Plan2ReviewCountAddInterval3State(
                            plan2_state=staged_scalar,
                        )
                    else:
                        review_state = replace(
                            review_state,
                            plan2_state=staged_scalar,
                        )
                    native_before = _plan3_state_for_status_children(
                        staged_context.zones,
                        total_effect_draw_card_count=(
                            staged_context.total_effect_draw_card_count
                        ),
                    )
                    remaining_before = _remaining_state_for_status_children(
                        staged_context.zones
                    )
                    source_card = next(
                        (
                            value
                            for value in staged_context.zones.card_universe
                            if value.guid == command.source_guid
                        ),
                        None,
                    )
                    child_state = Plan2NativeStatusChildState(
                        plan2_state=staged_scalar,
                        native_state=native_before,
                        remaining_move_state=remaining_before,
                        review_state=review_state,
                        block_runtime=Plan2BlockDependConsumptionSumRuntime(
                            block=staged_scalar.block,
                            block_consumption_sum_count=(
                                staged_context.block_consumption_sum_count
                            ),
                        ),
                        dynamic_block_runtime=Plan2NativeDynamicBlockRuntime(
                            block=staged_scalar.block,
                            aggressive=staged_scalar.card_play_aggressive,
                            source_card_use_count=(
                                0
                                if source_card is None
                                else source_card.runtime_state.play_count
                            ),
                            exam_card_play_count=(
                                staged_scalar.exam_card_play_count
                            ),
                            turn_card_play_count=(
                                staged_scalar.turn_card_play_count
                            ),
                            block_consumption_sum_count=(
                                staged_context.block_consumption_sum_count
                            ),
                        ),
                        application_status=(
                            staged_scalar.parameter_application_status(
                                slump=False
                            )
                        ),
                    )
                    exact = execute_plan2_native_status_child_handoff(
                        replace(transition, commands=(command,)),
                        child_state,
                        supplied,
                        compilation=status_child_compilation,
                    )
                    if not exact.executable:
                        raise Plan2NativeHorizonError(
                            "status-child-execution-failed-closed",
                            ",".join(exact.unresolved),
                        )
                    if (
                        exact.after_state.native_state != native_before
                        or exact.after_state.remaining_move_state
                        != remaining_before
                    ):
                        raise Plan2NativeHorizonError(
                            "status-child-mixed-zone-operation-unbound",
                            command.child.effect_id,
                        )
                    if (staged_context.aggressive_additive_runtime is not None
                            and exact.after_state.plan2_state.card_play_aggressive > staged_scalar.card_play_aggressive):
                        raise Plan2NativeHorizonError("aggressive-additive-typed-status-child-gain-unbound", command.child.effect_id)
                    staged_scalar = exact.after_state.plan2_state
                    if exact.after_state.application_status is not None:
                        staged_scalar = carry_plan2_state_application_status(
                            staged_scalar,
                            exact.after_state.application_status,
                        )
                    if staged_review_dynamic is not None:
                        staged_review_dynamic = _sync_review_dynamic(
                            staged_review_dynamic,
                            staged_scalar.review,
                        )
                    staged_context.review_state = (
                        exact.after_state.review_state
                    )
                    if exact.after_state.block_runtime is not None:
                        staged_context.block_consumption_sum_count = (
                            exact.after_state.block_runtime.block_consumption_sum_count
                        )
                    if exact.after_state.dynamic_block_runtime is not None:
                        staged_context.block_consumption_sum_count = (
                            exact.after_state.dynamic_block_runtime.block_consumption_sum_count
                        )
                    staged_trace.extend(
                        (
                            *exact.trace,
                            "status-child-typed:"
                            f"{command.status_uid}:{command.child.effect_id}",
                        )
                    )
                    typed_executed = True
                    changed_type = ""
                    difference = 0
                    for candidate_type, candidate_difference in (
                        (
                            _STATUS_CHILD_REVIEW,
                            staged_scalar.review - before_command.review,
                        ),
                        (
                            _STATUS_CHILD_BLOCK,
                            staged_scalar.block - before_command.block,
                        ),
                        (
                            _STATUS_CHILD_AGGRESSIVE,
                            staged_scalar.card_play_aggressive
                            - before_command.card_play_aggressive,
                        ),
                    ):
                        if candidate_difference:
                            changed_type = candidate_type
                            difference = candidate_difference
                            break
                staged_changes.append((command, changed_type, difference))

            if typed_executed and staged_context.inputs:
                staged_context.inputs.pop(0)
            child_status_suppression = tuple(
                dict.fromkeys(
                    (
                        *(item.status_uid for item in transition.before.listeners),
                        *(item.status_uid for item in transition.after.listeners),
                    )
                )
            )
            for command, changed_type, difference in staged_changes:
                if changed_type and difference:
                    pending.append(
                        Plan2NativeStatusEnchantEvent(
                            phase=PHASE_STATUS_CHANGE,
                            play_origin=event.play_origin,
                            round_number=event.round_number,
                            changed_effect_type=changed_type,
                            status_difference=difference,
                            status_change_committed=True,
                            suppressed_status_uids=tuple(
                                dict.fromkeys(
                                    (
                                        *next_event.suppressed_status_uids,
                                        command.status_uid,
                                        *child_status_suppression,
                                    )
                                )
                            ),
                        )
                    )
            next_executed = executed + len(transition.commands)
            if next_executed > command_limit:
                raise Plan2NativeHorizonError(
                    "status-enchant-child-command-limit-exceeded",
                    str(command_limit),
                )
            status_child_context.zones = staged_context.zones
            status_child_context.inputs = staged_context.inputs
            status_child_context.review_state = staged_context.review_state
            status_child_context.total_effect_draw_card_count = (
                staged_context.total_effect_draw_card_count
            )
            status_child_context.block_consumption_sum_count = (
                staged_context.block_consumption_sum_count
            )
            status_child_context.hand_move_turn = (
                staged_context.hand_move_turn
            )
            status_child_context.turn_used_support_ids = (
                staged_context.turn_used_support_ids
            )
            status_child_context.aggressive_additive_runtime = staged_context.aggressive_additive_runtime
            after_scalar = staged_scalar
            review_dynamic = staged_review_dynamic
            after_runtime = transition.after
            trace.extend(staged_trace)
            executed = next_executed
            continue
        if exact_bindings and any(binding.target for binding in exact_bindings):
            if status_child_context is None:
                raise Plan2NativeHorizonError(
                    "status-child-context-missing",
                    transition.commands[0].installer_effect_id,
                )
            review_state = status_child_context.review_state
            if review_state is None:
                review_state = Plan2ReviewCountAddInterval3State(
                    plan2_state=after_scalar,
                )
            else:
                review_state = replace(review_state, plan2_state=after_scalar)
            native_before = _plan3_state_for_status_children(
                status_child_context.zones,
                total_effect_draw_card_count=(
                    status_child_context.total_effect_draw_card_count
                ),
            )
            remaining_before = _remaining_state_for_status_children(
                status_child_context.zones
            )
            source_card = next(
                (
                    value
                    for value in status_child_context.zones.card_universe
                    if value.guid == transition.commands[0].source_guid
                ),
                None,
            )
            dynamic_runtime = Plan2NativeDynamicBlockRuntime(
                block=after_scalar.block,
                aggressive=after_scalar.card_play_aggressive,
                source_card_use_count=(
                    0 if source_card is None else source_card.runtime_state.play_count
                ),
                exam_card_play_count=after_scalar.exam_card_play_count,
                turn_card_play_count=after_scalar.turn_card_play_count,
                block_consumption_sum_count=(
                    status_child_context.block_consumption_sum_count
                ),
            )
            child_state = Plan2NativeStatusChildState(
                plan2_state=after_scalar,
                native_state=native_before,
                remaining_move_state=remaining_before,
                review_state=review_state,
                block_runtime=Plan2BlockDependConsumptionSumRuntime(
                    block=after_scalar.block,
                    block_consumption_sum_count=(
                        status_child_context.block_consumption_sum_count
                    ),
                ),
                dynamic_block_runtime=dynamic_runtime,
                application_status=after_scalar.parameter_application_status(
                    slump=False
                ),
            )
            before_child_zones = status_child_context.zones
            supplied = (
                status_child_context.inputs[0]
                if status_child_context.inputs
                else default_status_child_input
                or Plan2NativeStatusChildInput()
            )
            supplied = replace(
                supplied,
                hand_limit=(
                    status_child_context.hand_limit
                    if supplied.hand_limit is None
                    else supplied.hand_limit
                ),
                playing_guid=(
                    status_child_context.playing_guid
                    if supplied.playing_guid is None
                    and status_child_context.playing_guid
                    else supplied.playing_guid
                ),
            )
            exact = execute_plan2_native_status_child_handoff(
                transition,
                child_state,
                supplied,
                compilation=status_child_compilation,
            )
            if not exact.executable:
                raise Plan2NativeHorizonError(
                    "status-child-execution-failed-closed",
                    ",".join(exact.unresolved),
                )
            if status_child_context.inputs:
                status_child_context.inputs.pop(0)
            if (status_child_context.aggressive_additive_runtime is not None
                    and exact.after_state.plan2_state.card_play_aggressive > after_scalar.card_play_aggressive):
                raise Plan2NativeHorizonError("aggressive-additive-typed-status-child-gain-unbound", transition.commands[0].child.effect_id)
            after_scalar = exact.after_state.plan2_state
            if exact.after_state.application_status is not None:
                after_scalar = carry_plan2_state_application_status(
                    after_scalar,
                    exact.after_state.application_status,
                )
            if review_dynamic is not None:
                review_dynamic = _sync_review_dynamic(
                    review_dynamic,
                    after_scalar.review,
                )
            after_runtime = exact.after_parent
            # A status-enchant child is already the result of a listener
            # dispatch.  Android does not re-enter sibling StatusChange
            # listeners for the scalar mutation emitted by that child (the
            # originating listener is the familiar same-chain case, while
            # the full active set also prevents a sibling gimmick from being
            # counted a second time).  Keep the suppression generic and
            # identity-free; direct card/item/drink mutations still enter
            # StatusChange through their unsuppressed outer event.
            child_status_suppression = tuple(
                dict.fromkeys(
                    (
                        *(item.status_uid for item in transition.before.listeners),
                        *(item.status_uid for item in after_runtime.listeners),
                    )
                )
            )
            if exact.after_state.native_state != native_before:
                assert exact.after_state.native_state is not None
                status_child_context.zones = _ordered_after_status_native(
                    status_child_context.zones,
                    exact.after_state.native_state,
                )
                status_child_context.total_effect_draw_card_count = (
                    exact.after_state.native_state.total_effect_draw_card_count
                )
            if exact.after_state.remaining_move_state != remaining_before:
                assert exact.after_state.remaining_move_state is not None
                status_child_context.zones = _ordered_after_status_remaining(
                    status_child_context.zones,
                    exact.after_state.remaining_move_state,
                )
            status_child_context.review_state = exact.after_state.review_state
            if exact.after_state.block_runtime is not None:
                status_child_context.block_consumption_sum_count = (
                    exact.after_state.block_runtime.block_consumption_sum_count
                )
            if exact.after_state.dynamic_block_runtime is not None:
                status_child_context.block_consumption_sum_count = (
                    exact.after_state.dynamic_block_runtime.block_consumption_sum_count
                )
            move_reason = next(
                (
                    {
                        "card-draw": "draw",
                        "card-move": "card-move",
                        "card-create-id": "hand-add",
                        "force-play": "hand-add",
                    }[binding.operation]
                    for binding in exact_bindings
                    if binding.operation
                    in {"card-draw", "card-move", "card-create-id", "force-play"}
                ),
                None,
            )
            if move_reason is not None:
                after_scalar, status_child_context.zones, hand_move_trace = (
                    _apply_generated_hand_arrivals(
                        after_scalar,
                        before_child_zones,
                        status_child_context.zones,
                        status_child_context,
                        reason=move_reason,
                    )
                )
                trace.extend(hand_move_trace)
            trace.extend((*exact.trace, *(f"status-child:{item.command.status_uid}:{item.command.child.effect_id}" for item in exact.executions)))
            for changed_type, difference in (
                (_STATUS_CHILD_REVIEW, after_scalar.review - child_state.plan2_state.review),
                (_STATUS_CHILD_BLOCK, after_scalar.block - child_state.plan2_state.block),
                (
                    _STATUS_CHILD_AGGRESSIVE,
                    after_scalar.card_play_aggressive
                    - child_state.plan2_state.card_play_aggressive,
                ),
            ):
                if changed_type and difference:
                    pending.append(
                        Plan2NativeStatusEnchantEvent(
                            phase=PHASE_STATUS_CHANGE,
                            play_origin=event.play_origin,
                            round_number=event.round_number,
                            changed_effect_type=changed_type,
                            status_difference=difference,
                            status_change_committed=True,
                            suppressed_status_uids=tuple(
                                dict.fromkeys(
                                    (
                                        *next_event.suppressed_status_uids,
                                        *(item.command.status_uid for item in exact.executions),
                                        *child_status_suppression,
                                    )
                                )
                            ),
                        )
                    )
            executed += len(transition.commands)
            if executed > command_limit:
                raise Plan2NativeHorizonError(
                    "status-enchant-child-command-limit-exceeded",
                    str(command_limit),
                )
            continue
        after_runtime = transition.after
        child_status_suppression = tuple(
            dict.fromkeys(
                (
                    *(item.status_uid for item in transition.before.listeners),
                    *(item.status_uid for item in after_runtime.listeners),
                )
            )
        )
        trace.extend(transition.trace)
        for command in transition.commands:
            executed += 1
            if executed > command_limit:
                raise Plan2NativeHorizonError(
                    "status-enchant-child-command-limit-exceeded",
                    str(command_limit),
                )
            after_scalar, changed_type, difference = _apply_status_enchant_child(
                after_scalar,
                command,
                review_dynamic=review_dynamic,
                play_origin=event.play_origin,
                status_child_context=status_child_context,
            )
            if review_dynamic is not None:
                review_dynamic = _sync_review_dynamic(
                    review_dynamic,
                    after_scalar.review,
                )
            trace.append(
                f"status-child:{command.status_uid}:{command.child.effect_id}"
            )
            if changed_type and difference:
                pending.append(
                    Plan2NativeStatusEnchantEvent(
                        phase=PHASE_STATUS_CHANGE,
                        play_origin=event.play_origin,
                        round_number=event.round_number,
                        changed_effect_type=changed_type,
                        status_difference=difference,
                        status_change_committed=True,
                        suppressed_status_uids=tuple(
                            dict.fromkeys(
                                (
                                    *next_event.suppressed_status_uids,
                                    command.status_uid,
                                    *child_status_suppression,
                                )
                            )
                        ),
                    )
                )
    return after_scalar, after_runtime, tuple(trace)


_HAND_ADD_LESSON_TYPES: dict[int, str] = {
    1: "ProduceStepLessonType_LessonVocal",
    2: "ProduceStepLessonType_LessonDance",
    3: "ProduceStepLessonType_LessonVisual",
}


def _item_status_change_lesson_type(
    state: Plan2NativeHorizonState,
) -> str:
    """Bind an item StatusChange event to the exact current attribute.

    Auditions own a per-turn parameter schedule in ``battle_scoring`` while
    lessons expose the same Vocal/Dance/Visual enum through ``exam_mode``.
    Unknown modes remain inapplicable instead of guessing a lesson family.
    """

    parameter = (
        state.scalar.battle_scoring.current_parameter_type
        if state.scalar.battle_scoring is not None
        else state.exam_mode.parameter_type
    )
    try:
        value = int(parameter)
    except (TypeError, ValueError):
        return "ProduceStepLessonType_Unknown"
    return _HAND_ADD_LESSON_TYPES.get(
        value,
        "ProduceStepLessonType_Unknown",
    )


_PITEM_FIELD_CARD_SEARCH_COUNT = "ProduceExamFieldStatusType_CardSearchCountUp"
_PITEM_FIELD_BLOCK = "ProduceExamFieldStatusType_BlockUp"


def _pitem_neutral_search_issue(search: ProduceCardSearchRule) -> str | None:
    """Validate the search subset countable from ordered native zones.

    A CardSearchCountUp field can be evaluated without guessing only when the
    Master search is a neutral collection search.  The collection position and
    the optional result cap remain meaningful; card identity/category/status
    filters would require another native search owner and therefore stay
    fail-closed.
    """

    expected: tuple[tuple[str, object], ...] = (
        ("card_rarities", ()),
        ("produce_card_ids", ()),
        ("upgrade_counts", ()),
        ("plan_type", "ProducePlanType_Unknown"),
        ("card_categories", ()),
        ("card_status_type", "ProduceCardSearchStatusType_Unknown"),
        ("order_type", "ProduceCardOrderType_Unknown"),
        ("card_search_tag", ""),
        ("produce_card_random_pool_id", ""),
        ("stamina_min_max_type", "ConditionMinMaxType_Unknown"),
        ("stamina_min", 0),
        ("stamina_max", 0),
        ("exam_effect_type", "ProduceExamEffectType_Unknown"),
        ("effect_group_ids", ()),
        ("is_self", False),
        ("produce_card_pool_id", ""),
        ("cost_type", "ExamCostType_Unknown"),
        ("is_customized", False),
    )
    for name, neutral in expected:
        value = getattr(search, name)
        if value != neutral:
            return f"non-neutral:{name}"
    if search.card_position_type not in {
        "ProduceCardPositionType_Hand",
        "ProduceCardPositionType_Deck",
        "ProduceCardPositionType_DeckAll",
        "ProduceCardPositionType_Grave",
        "ProduceCardPositionType_DeckGrave",
        "ProduceCardPositionType_Lost",
        "ProduceCardPositionType_NotLost",
        "ProduceCardPositionType_Hold",
        "ProduceCardPositionType_Playing",
    }:
        return f"position:{search.card_position_type}"
    if search.limit_count < 0:
        return "negative-limit"
    return None


def _pitem_search_zone_cards(
    zones: NativeOrderedZoneState,
    position: str,
) -> tuple[NativeOrderedCardInstance, ...] | None:
    """Return the ordered collection represented by one Master position."""

    pending = (
        () if zones.pending_played is None else (zones.pending_played,)
    )
    return {
        "ProduceCardPositionType_Hand": zones.hand,
        "ProduceCardPositionType_Deck": zones.deck,
        "ProduceCardPositionType_DeckAll": zones.deck,
        "ProduceCardPositionType_Grave": zones.grave,
        "ProduceCardPositionType_DeckGrave": (*zones.deck, *zones.grave),
        "ProduceCardPositionType_Lost": zones.lost,
        "ProduceCardPositionType_NotLost": (
            *zones.hand,
            *zones.deck,
            *zones.grave,
            *pending,
        ),
        # Hold is outside the typed zone model.  An empty tuple is still an
        # authoritative zero because projection rejects a populated Hold.
        "ProduceCardPositionType_Hold": (),
        "ProduceCardPositionType_Playing": pending,
    }.get(position)


def _native_pitem_field_search_evidence(
    runtime: Plan2NativeItemRuntime,
    zones: NativeOrderedZoneState,
) -> tuple[dict[tuple[str, str], int], tuple[str, ...]]:
    """Build typed CardSearchCountUp evidence from Master + ordered zones.

    P-item trigger fields carry a search ID, not a direct zone name.  Resolve
    that ID through Master and count only a neutral, typed collection in the
    exact ordered-zone snapshot.  No item/card ID is used to select behavior.
    """

    values: dict[tuple[str, str], int] = {}
    traces: list[str] = []
    for listener in runtime.listeners:
        dispatch = listener.trigger.event_dispatch
        if (
            dispatch is None
            or listener.trigger.phase != ITEM_PHASE_END_TURN
        ):
            continue
        for reference in dispatch.search_references:
            if not reference.is_field or reference.field_index is None:
                continue
            if reference.field_index >= len(dispatch.trigger.fields):
                traces.append(
                    "native-pitem-search-evidence-missing:"
                    f"{reference.search_id}:field-index"
                )
                continue
            predicate = dispatch.trigger.fields[reference.field_index]
            if predicate.field_type != _PITEM_FIELD_CARD_SEARCH_COUNT:
                traces.append(
                    "native-pitem-search-evidence-missing:"
                    f"{reference.search_id}:field-type:{predicate.field_type}"
                )
                continue
            if predicate.card_search_id != reference.search_id:
                traces.append(
                    "native-pitem-search-evidence-missing:"
                    f"{reference.search_id}:field-reference-mismatch"
                )
                continue
            search = reference.rule
            if search is None:
                try:
                    search = load_produce_card_search(
                        reference.search_id,
                        database=DEFAULT_MASTER_DATABASE,
                    )
                except (KeyError, OSError, TypeError, ValueError):
                    traces.append(
                        "native-pitem-search-evidence-missing:"
                        f"{reference.search_id}:master-unavailable"
                    )
                    continue
            issue = _pitem_neutral_search_issue(search)
            if issue is not None:
                traces.append(
                    "native-pitem-search-evidence-missing:"
                    f"{reference.search_id}:{issue}"
                )
                continue
            cards = _pitem_search_zone_cards(zones, search.card_position_type)
            if cards is None:
                traces.append(
                    "native-pitem-search-evidence-missing:"
                    f"{reference.search_id}:zone-unavailable"
                )
                continue
            count = len(cards)
            if search.limit_count > 0:
                count = min(count, search.limit_count)
            key = (predicate.field_type, reference.search_id)
            previous = values.get(key)
            if previous is not None and previous != count:
                # Conflicting occurrences cannot be resolved by choosing one
                # listener's snapshot; leave the key absent so dispatch fails
                # closed on missing evidence.
                values.pop(key, None)
                traces.append(
                    "native-pitem-search-evidence-conflict:"
                    f"{reference.search_id}:{previous}!={count}"
                )
                continue
            values[key] = count
            traces.append(
                "native-pitem-search-count:"
                f"{reference.search_id}:{search.card_position_type}:{count}"
            )
    return values, tuple(traces)


def _native_pitem_end_turn_phase_values(
    runtime: Plan2NativeItemRuntime,
) -> tuple[int, ...]:
    """Return a shared EndTurnInterval Master vector when one is provable."""

    vectors = tuple(
        listener.trigger.master_trigger.phase_values
        for listener in runtime.listeners
        if (
            listener.trigger.phase == ITEM_PHASE_END_TURN
            and listener.trigger.master_trigger is not None
            and not _has_legacy_runtime_shape(listener.trigger)
        )
    )
    if vectors and all(value == vectors[0] for value in vectors):
        return vectors[0]
    # A single event can carry one explicit vector.  If listeners have
    # different Master intervals, each generic listener normalizes an omitted
    # vector to its own contract and applies its own round gate.
    return ()


def _item_turn_start_parameter_value(
    state: Plan2NativeHorizonState,
) -> int:
    """Return the current native lesson/ audition attribute for StartTurn.

    The item serializer uses one ``lesson_*`` trigger grammar for both
    lessons and auditions.  Lessons bind the value through ``step_type``;
    auditions bind it through the per-turn battle schedule.  Keep the
    distinction here so a restored audition item can evaluate its Vocal /
    Dance / Visual predicate without pretending that the exam itself is a
    lesson.
    """

    if state.exam_mode.is_lesson:
        return state.exam_mode.step_type_value
    scoring = state.scalar.battle_scoring
    if scoring is None:
        return 0
    try:
        value = int(scoring.current_parameter_type)
    except (TypeError, ValueError):
        return 0
    # ``plan2_native_lesson_trigger_is_runtime_valid`` consumes the native
    # nine-way lesson step enum (Vocal 1..3, Dance 4..6, Visual 7..9), while
    # an audition schedule stores only the three parameter kinds (1..3).
    # Bind each audition parameter to its ordinary/normal lesson slot so a
    # Vocal trigger cannot accidentally match Visual value ``3`` as a Vocal
    # hard slot.
    return {1: 1, 2: 4, 3: 7}.get(value, 0)


def _hand_add_lesson_type(
    state: Plan2NativeHorizonState,
) -> str:
    # Audition HandAdd is parameter-scoped to the *draw's* turn, not the
    # transition-local checkpoint's original turn.  The battle-scoring
    # context owns the full native schedule and advances with EndTurn, so it
    # must take precedence over a checkpoint-time convenience value.
    if state.exam_mode.is_battle and state.scalar.battle_scoring is not None:
        try:
            parameter = int(
                state.scalar.battle_scoring.current_parameter_type
            )
        except (TypeError, ValueError):
            parameter = 0
        lesson_type = _HAND_ADD_LESSON_TYPES.get(parameter)
        if lesson_type is not None:
            return lesson_type
    if state.hand_add_lesson_type is not None:
        return state.hand_add_lesson_type
    parameter = int(state.exam_mode.parameter_type)
    lesson_type = _HAND_ADD_LESSON_TYPES.get(parameter)
    if lesson_type is None:
        raise Plan2NativeHorizonError(
            "hand-add-support-lesson-type-missing",
            f"exam_mode={state.exam_mode.exam_type}:{state.exam_mode.step_type_value}",
        )
    return lesson_type


def _apply_hand_add_support_to_zones(
    zones: NativeOrderedZoneState,
    drawn_guids: tuple[str, ...],
    *,
    catalog: "Plan2NativeHandAddSupportCatalog | None",
    lesson_type: str,
    used_support_ids: tuple[str, ...],
) -> tuple[NativeOrderedZoneState, tuple[str, ...], tuple[str, ...]]:
    if catalog is None:
        if used_support_ids:
            raise Plan2NativeHorizonError(
                "hand-add-support-catalog-missing",
                "turn-used support IDs were supplied without a catalog",
            )
        return zones, used_support_ids, ()
    if not drawn_guids:
        return zones, used_support_ids, ()
    from .plan2_native_hand_add_support import (
        Plan2NativeHandAddSupportInput,
        apply_plan2_native_hand_add_support_to_zones,
    )

    request = Plan2NativeHandAddSupportInput(
        drawn_guids=drawn_guids,
        lesson_type=lesson_type,
        used_support_ids=used_support_ids,
    )
    try:
        after_zones, native, _upgraded_guids = (
            apply_plan2_native_hand_add_support_to_zones(
                zones,
                request,
                catalog,
            )
        )
    except Exception as error:
        code = getattr(error, "code", "hand-add-support-execution-failed")
        detail = getattr(error, "detail", f"{type(error).__name__}:{error}")
        raise Plan2NativeHorizonError(str(code), str(detail)) from error
    upgraded = tuple(
        support_id
        for card in native.cards
        for support_id in card.added_support_ids
    )
    return after_zones, native.used_support_ids, (
        "hand-add-support:"
        + (";".join(upgraded) if upgraded else "none"),
        "hand-add-support-rng:"
        f"{native.initial_random_state}->{native.final_random_state}",
    )


def _apply_hand_add_support_after_draw(
    state: Plan2NativeHorizonState,
    drawn_guids: tuple[str, ...],
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Apply one exact native HandAdd event after the zone draw.

    The optional catalog is caller-owned.  With no catalog and an empty
    ledger this is a no-op for every legacy fixture.  A configured catalog is
    applied atomically: a blocker raises before this helper returns, so the
    caller never receives a partially drawn horizon.
    """

    zones, used_support_ids, support_trace = _apply_hand_add_support_to_zones(
        state.zones,
        drawn_guids,
        catalog=state.hand_add_support_catalog,
        lesson_type=(
            ""
            if state.hand_add_support_catalog is None
            else _hand_add_lesson_type(state)
        ),
        used_support_ids=state.turn_used_support_ids,
    )
    return replace(
        state,
        zones=zones,
        turn_used_support_ids=used_support_ids,
    ), support_trace


def _replace_with_draw(
    state: Plan2NativeHorizonState,
    count: int,
    catalog: Plan2NativeProgramCatalog,
    *,
    effect_draw: bool = False,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    capacity = max(state.hand_limit - len(state.zones.hand), 0)
    available = len(state.zones.deck) + len(state.zones.grave)
    actual = min(count, capacity, available)
    if actual == 0:
        return state, ("draw:0",)
    draw = state.zones.draw_to_hand(actual)
    context = _status_child_context_from_state(state, catalog)
    scalar, zones, hand_move_trace = _apply_generated_hand_arrivals(
        state.scalar,
        state.zones,
        draw.state,
        context,
        reason="draw",
    )
    drawn_state = replace(
        state,
        scalar=scalar,
        zones=zones,
        total_effect_draw_card_count=(
            _i32_add(
                state.total_effect_draw_card_count,
                actual,
                "total_effect_draw_card_count",
            )
            if effect_draw
            else state.total_effect_draw_card_count
        ),
        block_consumption_sum_count=context.block_consumption_sum_count,
        generated_hand_move_turn=context.hand_move_turn,
        turn_used_support_ids=context.turn_used_support_ids,
    )
    return drawn_state, (
        f"draw:{actual}",
        f"shuffle-grave:{draw.recycled_grave_count}",
        f"rng:{draw.random_state_before}->{draw.state.random_state}",
        *hand_move_trace,
    )


def _drain_encore_handoffs(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    queue_limit: int,
    skip_handoff_id: str = "",
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    after = state
    trace: list[str] = []
    executed = 0
    while after.encore_runtime is not None:
        pending = next(
            (
                value
                for value in after.encore_runtime.pending_handoffs
                if value.handoff_id != skip_handoff_id
            ),
            None,
        )
        if pending is None:
            break
        executed += 1
        if executed > queue_limit:
            raise Plan2NativeHorizonError(
                "encore-handoff-command-limit-exceeded",
                str(queue_limit),
            )
        staged = stage_plan2_native_status_enchant_encore(
            after.encore_runtime,
            pending.handoff_id,
        )
        if staged.source_zone != "lost" or staged.rng_consumed:
            raise Plan2NativeHorizonError(
                "encore-stage-boundary-unbound",
                pending.handoff_id,
            )
        replayed, replay_trace = _execute_play(
            after,
            staged.captured_guid,
            catalog,
            pay_cost=False,
            spend_play=False,
            forced=True,
            queue_limit=max(queue_limit - executed, 0),
            replay_ancestry=staged.handoff.ancestry,
            active_encore_handoff_id=pending.handoff_id,
        )
        if replayed.encore_runtime is None:
            raise Plan2NativeHorizonError(
                "encore-runtime-lost-during-replay",
                pending.handoff_id,
            )
        program = replayed.encore_runtime.catalog.resolve(
            staged.live_card.card_id,
            staged.live_card.effective_upgrade,
        )
        after_native = _plan3_state_from_ordered(replayed.zones)
        receipt = Plan2NativeStatusEnchantEncorePlayReceipt(
            pending.handoff_id,
            staged.live_card,
            after_native,
            "forced",
            staged.source_zone,
            program.ordered_effect_ids,
            program.prior_effect_ids,
            (),
            (program.wrapper_effect_id,),
            False,
            False,
        )
        recorded = record_plan2_native_status_enchant_encore(
            replayed.encore_runtime,
            receipt,
        )
        after = replace(replayed, encore_runtime=recorded)
        trace.extend(
            (
                f"encore-stage:{pending.handoff_id}:{staged.captured_guid}",
                *replay_trace,
                f"encore-record:{pending.handoff_id}",
            )
        )
    return after, tuple(trace)


def _status_child_context_from_state(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    playing_guid: str = "",
) -> Plan2NativeStatusChildContext:
    review_state = state.status_child_review_state
    if review_state is not None:
        review_state = replace(review_state, plan2_state=state.scalar)
    hand_move_turn = state.generated_hand_move_turn
    if hand_move_turn.exam_turn < state.scalar.current_turn:
        hand_move_turn = start_plan2_generated_hand_move_turn(
            hand_move_turn,
            state.scalar.current_turn,
        )
    def apply_hand_add_support(
        zones: NativeOrderedZoneState,
        drawn_guids: tuple[str, ...],
        used_support_ids: tuple[str, ...],
    ) -> tuple[NativeOrderedZoneState, tuple[str, ...], tuple[str, ...]]:
        return _apply_hand_add_support_to_zones(
            zones,
            drawn_guids,
            catalog=state.hand_add_support_catalog,
            lesson_type=(
                ""
                if state.hand_add_support_catalog is None
                else _hand_add_lesson_type(state)
            ),
            used_support_ids=used_support_ids,
        )

    return Plan2NativeStatusChildContext(
        zones=state.zones,
        hand_limit=state.hand_limit,
        master_card_refs=catalog.master_card_refs,
        inputs=list(state.status_child_inputs),
        review_state=review_state,
        total_effect_draw_card_count=state.total_effect_draw_card_count,
        block_consumption_sum_count=state.block_consumption_sum_count,
        playing_guid=playing_guid,
        hand_move_programs=tuple(catalog.hand_move_programs),
        hand_move_turn=hand_move_turn,
        hand_add_support_callback=apply_hand_add_support,
        turn_used_support_ids=state.turn_used_support_ids,
        aggressive_additive_runtime=state.aggressive_additive_runtime,
    )


def _state_after_status_child_context(
    state: Plan2NativeHorizonState,
    context: Plan2NativeStatusChildContext,
) -> Plan2NativeHorizonState:
    review_state = context.review_state
    if review_state is not None:
        review_state = replace(review_state, plan2_state=state.scalar)
    return replace(
        state,
        zones=context.zones,
        status_child_inputs=tuple(context.inputs),
        status_child_review_state=review_state,
        total_effect_draw_card_count=context.total_effect_draw_card_count,
        block_consumption_sum_count=context.block_consumption_sum_count,
        generated_hand_move_turn=context.hand_move_turn,
        turn_used_support_ids=context.turn_used_support_ids,
        aggressive_additive_runtime=_context_aggressive_runtime(context, state.scalar, state.aggressive_additive_runtime),
    )


def dispatch_plan2_native_turn_timer(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Run the native per-turn timer phase for installed start statuses."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not any(
        listener.program.trigger.phase == PHASE_TURN_TIMER
        for listener in state.status_enchant.listeners
    ):
        return state, ()
    context = _status_child_context_from_state(state, catalog)
    scalar, runtime, trace = dispatch_plan2_native_status_enchant_event(
        state.scalar,
        state.status_enchant,
        Plan2NativeStatusEnchantEvent(
            phase=PHASE_TURN_TIMER,
            round_number=state.scalar.current_turn,
        ),
        remaining_turns=state.remaining_turns,
        review_dynamic=state.review_dynamic,
        status_child_compilation=catalog.status_child_compilation,
        status_child_context=context,
    )
    review_dynamic = _sync_review_dynamic(state.review_dynamic, scalar.review)
    after = replace(
        state,
        scalar=scalar,
        status_enchant=runtime,
        review_dynamic=review_dynamic,
        triggered_review_status_runtime=replace(
            state.triggered_review_status_runtime,
            review_runtime=review_dynamic,
        ),
    )
    after = _state_after_status_child_context(after, context)
    return after, (f"turn-timer:{state.scalar.current_turn}", *trace)


def _external_review_child_command(
    effect: Plan2NativeExternalReviewEffect,
) -> Plan2StatusEnchantChildCommand:
    child = Plan2StatusEnchantChildProgram(
        effect_id=effect.effect_id,
        effect_type=_STATUS_CHILD_REVIEW,
        value1=effect.value,
        value2=0,
        count=0,
        turn=0,
        status_enchant_id="",
        chain_effect_id="",
        chain_effect_ids=(),
        target_card_id="",
        target_upgrade=0,
        target_effect_type="",
        card_search_id="",
        move_position_type="",
        card_search_id2="",
        grow_effect_ids=(),
        effect_group_ids=(),
    )
    return Plan2StatusEnchantChildCommand(
        event_sequence=effect.sequence,
        listener_sequence=0,
        child_sequence=effect.sequence,
        status_uid=0,
        source_guid=effect.source_id,
        source_card_id="",
        source_upgrade=0,
        play_origin="normal",
        installer_effect_id=effect.effect_id,
        status_enchant_id="",
        trigger_id="",
        phase=STATUS_PHASE_START_TURN,
        child=child,
    )


def apply_plan2_native_external_review_effect(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    effect: Plan2NativeExternalReviewEffect,
) -> Plan2ScheduledGimmickHookResult:
    """Apply one proven external ``ExamReview`` effect through native owners.

    The scalar change is produced by the same child executor used by cards,
    items, and drinks.  The committed Review change is then dispatched to
    status-enchant listeners and projected back into every Review mirror and
    status-child context before the scheduled hook returns.
    """

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(effect, Plan2NativeExternalReviewEffect):
        raise TypeError("effect must be Plan2NativeExternalReviewEffect")
    if state.terminal:
        raise Plan2NativeHorizonError("terminal-state-has-no-external-effects")
    if state.phase != PHASE_MAIN:
        raise Plan2NativeHorizonError(
            "external-effect-phase-unbound", state.phase
        )
    if state.opaque_status_queue:
        raise Plan2NativeHorizonError(
            "opaque-status-queue-unresolved",
            ",".join(state.opaque_status_queue),
        )

    review_dynamic = _sync_review_dynamic(
        state.review_dynamic,
        state.scalar.review,
    )
    context = _status_child_context_from_state(state, catalog)
    expected_review = (
        apply_plan2_native_review_dynamic_review_gain(
            review_dynamic,
            value=effect.value,
            count=1,
        )
        if effect.apply_review_additive
        else _i32_add(state.scalar.review, effect.value, "review")
    )
    scalar, changed_type, difference = _apply_status_enchant_child(
        state.scalar,
        _external_review_child_command(effect),
        review_dynamic=(
            review_dynamic if effect.apply_review_additive else None
        ),
        play_origin="normal",
    )
    if (
        changed_type != _STATUS_CHILD_REVIEW
        or scalar.review != expected_review
        or difference != expected_review - state.scalar.review
    ):
        raise Plan2NativeHorizonError(
            "external-review-effect-result-unbound",
            effect.effect_id,
        )
    review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
    scalar, status_enchant, status_trace = (
        dispatch_plan2_native_status_enchant_event(
            scalar,
            state.status_enchant,
            Plan2NativeStatusEnchantEvent(
                phase=PHASE_STATUS_CHANGE,
                play_origin="normal",
                round_number=scalar.current_turn,
                changed_effect_type=changed_type,
                status_difference=difference,
                status_change_committed=True,
                suppressed_status_uids=effect.suppressed_status_uids,
            ),
            remaining_turns=state.remaining_turns,
            review_dynamic=review_dynamic,
            status_child_compilation=catalog.status_child_compilation,
            status_child_context=context,
        )
    )
    review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
    after = replace(
        state,
        scalar=scalar,
        status_enchant=status_enchant,
        review_dynamic=review_dynamic,
        triggered_review_status_runtime=replace(
            state.triggered_review_status_runtime,
            review_runtime=review_dynamic,
        ),
    )
    after = _state_after_status_child_context(after, context)
    return Plan2ScheduledGimmickHookResult(
        after,
        (
            "external-review-effect:"
            f"{effect.source_id}:{effect.effect_id}:"
            f"{state.scalar.review}->{scalar.review}",
            *status_trace,
        ),
    )


def _external_block_child_command(
    effect: Plan2NativeExternalBlockEffect,
) -> Plan2StatusEnchantChildCommand:
    child = Plan2StatusEnchantChildProgram(
        effect_id=effect.effect_id,
        effect_type=_STATUS_CHILD_BLOCK,
        value1=effect.value,
        value2=0,
        count=0,
        turn=0,
        status_enchant_id="",
        chain_effect_id="",
        chain_effect_ids=(),
        target_card_id="",
        target_upgrade=0,
        target_effect_type="",
        card_search_id="",
        move_position_type="",
        card_search_id2="",
        grow_effect_ids=(),
        effect_group_ids=(),
    )
    return Plan2StatusEnchantChildCommand(
        event_sequence=effect.sequence,
        listener_sequence=0,
        child_sequence=effect.sequence,
        status_uid=0,
        source_guid=effect.source_id,
        source_card_id="",
        source_upgrade=0,
        play_origin="normal",
        installer_effect_id=effect.effect_id,
        status_enchant_id="",
        trigger_id="",
        phase=STATUS_PHASE_START_TURN,
        child=child,
    )


def apply_plan2_native_external_block_effect(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    effect: Plan2NativeExternalBlockEffect,
) -> Plan2ScheduledGimmickHookResult:
    """Apply one proven external ``ExamBlock`` through native owners."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(effect, Plan2NativeExternalBlockEffect):
        raise TypeError("effect must be Plan2NativeExternalBlockEffect")
    if state.terminal or state.phase != PHASE_MAIN:
        raise Plan2NativeHorizonError("external-effect-phase-unbound", state.phase)
    if state.opaque_status_queue:
        raise Plan2NativeHorizonError(
            "opaque-status-queue-unresolved", ",".join(state.opaque_status_queue)
        )

    review_dynamic = _sync_review_dynamic(state.review_dynamic, state.scalar.review)
    context = _status_child_context_from_state(state, catalog)
    scalar, changed_type, difference = _apply_status_enchant_child(
        state.scalar,
        _external_block_child_command(effect),
        review_dynamic=review_dynamic,
        play_origin="normal",
    )
    if changed_type != _STATUS_CHILD_BLOCK or difference < effect.value:
        raise Plan2NativeHorizonError(
            "external-block-effect-result-unbound", effect.effect_id
        )
    scalar, status_enchant, status_trace = dispatch_plan2_native_status_enchant_event(
        scalar,
        state.status_enchant,
        Plan2NativeStatusEnchantEvent(
            phase=PHASE_STATUS_CHANGE,
            play_origin="normal",
            round_number=scalar.current_turn,
            changed_effect_type=changed_type,
            status_difference=difference,
            status_change_committed=True,
        ),
        remaining_turns=state.remaining_turns,
        review_dynamic=review_dynamic,
        status_child_compilation=catalog.status_child_compilation,
        status_child_context=context,
    )
    after = replace(state, scalar=scalar, status_enchant=status_enchant)
    after = _state_after_status_child_context(after, context)
    return Plan2ScheduledGimmickHookResult(
        after,
        (
            f"external-block-effect:{effect.source_id}:{effect.effect_id}:"
            f"{state.scalar.block}->{scalar.block}",
            *status_trace,
        ),
    )


def apply_plan2_native_external_stamina_damage_effect(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    effect: Plan2NativeExternalStaminaDamageEffect,
) -> Plan2ScheduledGimmickHookResult:
    """Apply direct gimmick stamina damage, which bypasses Block."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(effect, Plan2NativeExternalStaminaDamageEffect):
        raise TypeError("effect must be Plan2NativeExternalStaminaDamageEffect")
    if state.terminal or state.phase != PHASE_MAIN:
        raise Plan2NativeHorizonError("external-effect-phase-unbound", state.phase)
    if state.opaque_status_queue:
        raise Plan2NativeHorizonError(
            "opaque-status-queue-unresolved", ",".join(state.opaque_status_queue)
        )
    before = state.scalar.stamina
    after_stamina = max(0, before - effect.value)
    after = replace(
        state,
        scalar=replace(state.scalar, stamina=after_stamina),
    )
    return Plan2ScheduledGimmickHookResult(
        after,
        (
            f"external-stamina-damage:{effect.source_id}:{effect.effect_id}:"
            f"{before}->{after_stamina}:block={state.scalar.block}",
        ),
    )


def _external_aggressive_child_command(
    effect: Plan2NativeExternalAggressiveEffect,
) -> Plan2StatusEnchantChildCommand:
    child = Plan2StatusEnchantChildProgram(
        effect_id=effect.effect_id,
        effect_type=_STATUS_CHILD_AGGRESSIVE,
        value1=effect.value,
        value2=0,
        count=0,
        turn=0,
        status_enchant_id="",
        chain_effect_id="",
        chain_effect_ids=(),
        target_card_id="",
        target_upgrade=0,
        target_effect_type="",
        card_search_id="",
        move_position_type="",
        card_search_id2="",
        grow_effect_ids=(),
        effect_group_ids=(),
    )
    return Plan2StatusEnchantChildCommand(
        event_sequence=effect.sequence,
        listener_sequence=0,
        child_sequence=effect.sequence,
        status_uid=0,
        source_guid=effect.source_id,
        source_card_id="",
        source_upgrade=0,
        play_origin="normal",
        installer_effect_id=effect.effect_id,
        status_enchant_id="",
        trigger_id="",
        phase=STATUS_PHASE_START_TURN,
        child=child,
    )


def apply_plan2_native_external_aggressive_effect(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    effect: Plan2NativeExternalAggressiveEffect,
) -> Plan2ScheduledGimmickHookResult:
    """Apply one proven external Aggressive effect through native owners."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(effect, Plan2NativeExternalAggressiveEffect):
        raise TypeError("effect must be Plan2NativeExternalAggressiveEffect")
    if state.terminal:
        raise Plan2NativeHorizonError("terminal-state-has-no-external-effects")
    if state.phase != PHASE_MAIN:
        raise Plan2NativeHorizonError(
            "external-effect-phase-unbound", state.phase
        )
    if state.opaque_status_queue:
        raise Plan2NativeHorizonError(
            "opaque-status-queue-unresolved",
            ",".join(state.opaque_status_queue),
        )

    review_dynamic = _sync_review_dynamic(
        state.review_dynamic,
        state.scalar.review,
    )
    context = _status_child_context_from_state(state, catalog)
    scalar, changed_type, difference = _apply_status_enchant_child(
        state.scalar,
        _external_aggressive_child_command(effect),
        review_dynamic=review_dynamic,
        play_origin="normal",
        status_child_context=context,
    )
    if changed_type != _STATUS_CHILD_AGGRESSIVE or (state.aggressive_additive_runtime is None and difference != effect.value):
        raise Plan2NativeHorizonError(
            "external-aggressive-effect-result-unbound",
            effect.effect_id,
        )
    scalar, status_enchant, status_trace = (
        dispatch_plan2_native_status_enchant_event(
            scalar,
            state.status_enchant,
            Plan2NativeStatusEnchantEvent(
                phase=PHASE_STATUS_CHANGE,
                play_origin="normal",
                round_number=scalar.current_turn,
                changed_effect_type=changed_type,
                status_difference=difference,
                status_change_committed=True,
            ),
            remaining_turns=state.remaining_turns,
            review_dynamic=review_dynamic,
            status_child_compilation=catalog.status_child_compilation,
            status_child_context=context,
        )
    )
    review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
    after = replace(
        state,
        scalar=scalar,
        status_enchant=status_enchant,
        review_dynamic=review_dynamic,
        triggered_review_status_runtime=replace(
            state.triggered_review_status_runtime,
            review_runtime=review_dynamic,
        ),
    )
    after = _state_after_status_child_context(after, context)
    return Plan2ScheduledGimmickHookResult(
        after,
        (
            "external-aggressive-effect:"
            f"{effect.source_id}:{effect.effect_id}:"
            f"{state.scalar.card_play_aggressive}->"
            f"{scalar.card_play_aggressive}",
            *status_trace,
        ),
    )


def _execute_item_phase(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    event: Plan2NativeItemEvent,
    *,
    card_program: Plan2NativeCardProgram | None = None,
    play_origin: str = "normal",
    playing_guid: str = "",
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Dispatch one item boundary and route children to existing owners."""

    try:
        dispatched = dispatch_plan2_native_item_event(state.item_runtime, event)
    except (TypeError, ValueError) as error:
        raise Plan2NativeHorizonError(
            "item-runtime-dispatch-failed-closed",
            f"{type(error).__name__}:{error}",
        ) from error
    if not dispatched.effects:
        return replace(state, item_runtime=dispatched.after), dispatched.trace

    scalar = state.scalar
    plays_remaining = state.plays_remaining
    review_consumption_sum = state.review_consumption_sum
    status_enchant = state.status_enchant
    stamina_modifiers = state.stamina_modifiers
    effect_chains = state.effect_chains
    debuff_registry = state.debuff_registry
    # ReviewMultiple is allocated by the scalar owner, while card-installed
    # listeners use the status-enchant owner.  A card status can therefore have
    # advanced the shared native UID floor without changing scalar.next_uid;
    # synchronize before an item ReviewMultiple effect creates its layer.
    status_uid_floor = _next_horizon_status_uid(state)
    if scalar.next_status_uid < status_uid_floor:
        scalar = replace(scalar, next_status_uid=status_uid_floor)
    # Native item effects are resolved inside the item boundary.  The
    # captured ExamSave stream shows that the same status-change gimmick is
    # not re-entered by those child effects (its trigger-item fire ledger is
    # unchanged), while ordinary card boundaries still dispatch it.  Carry a
    # suppression set through the child status dispatches so restoring an
    # item listener does not manufacture a second gimmick reaction.
    item_suppressed_status_uids = tuple(
        listener.status_uid for listener in status_enchant.listeners
    )
    review_dynamic = _sync_review_dynamic(state.review_dynamic, scalar.review)
    status_child_context = _status_child_context_from_state(
        state,
        catalog,
        playing_guid=playing_guid,
    )
    trace = list(dispatched.trace)
    event_base = dict(
        accepted=True,
        play_origin=play_origin,
        round_number=event.round_number,
        card_id=event.card_id,
        card_upgrade=event.card_upgrade,
        card_category="" if card_program is None else card_program.category,
        card_effect_group_ids=(
            () if card_program is None else card_program.effect_group_ids
        ),
        card_position=(
            POSITION_PLAYING
            if event.phase in (ITEM_PHASE_CARD_PLAY, ITEM_PHASE_CARD_PLAY_AFTER)
            else ""
        ),
    )
    fired_ids = set(dispatched.fired_enchantment_ids)
    owned_effects = tuple((listener.source_item_id, effect)
        for listener in state.item_runtime.listeners if listener.enchantment_id in fired_ids
        for effect in listener.effects)
    if tuple(effect for _, effect in owned_effects) != dispatched.effects:
        raise Plan2NativeHorizonError("item-effect-source-order-mismatch")
    for index, (source_item_id, effect) in enumerate(owned_effects):
        before = scalar
        if effect.effect_type in SHARED_ITEM_SCALAR_EFFECT_TYPES:
            aggressive = _context_aggressive_runtime(status_child_context, scalar, state.aggressive_additive_runtime)
            aggressive_catalog = aggressive.catalog if aggressive is not None else None
            if effect.effect_type == SHARED_ITEM_AGGRESSIVE_ADDITIVE and aggressive_catalog is None:
                catalogs = {id(value): value for program in catalog.programs
                    if (value := getattr(program.native_playing_executor, "aggressive_additive_catalog", None)) is not None}
                if len(catalogs) == 1:
                    aggressive_catalog = next(iter(catalogs.values()))
            context = ItemScalarContext(
                scalar=scalar, review_dynamic=review_dynamic, aggressive_runtime=aggressive,
                aggressive_catalog=aggressive_catalog, debuff_registry=debuff_registry,
                next_status_uid=max(status_uid_floor, scalar.next_status_uid, review_dynamic.next_status_uid,
                    status_enchant.next_status_uid, stamina_modifiers.next_status_uid, effect_chains.next_status_uid,
                    debuff_registry.next_uid, dispatched.after.next_status_uid,
                    aggressive.next_status_uid if aggressive is not None else 1),
                source_item_id=source_item_id, play_origin=play_origin,
                stamina_recover_restricted=state.stamina_recover_restricted,
                stamina_recover_add_permil=state.stamina_recover_add_permil,
            )
            result, child_trace = execute_item_scalar_effect(context, effect)
            scalar = replace(result.scalar, next_status_uid=max(result.scalar.next_status_uid, result.next_status_uid))
            review_dynamic = result.review_dynamic
            status_child_context.aggressive_additive_runtime = result.aggressive_runtime
            debuff_registry = result.debuff_registry
            trace.extend(child_trace)
        elif effect.effect_type == ITEM_EFFECT_REVIEW:
            # A preceding card/item effect may already have installed a
            # ReviewAdditive layer in this action. Resolve each item child
            # through the current shared binary32/ceil owner, just as card
            # and status-listener Review gains do.
            scalar = replace(
                scalar,
                review=apply_plan2_native_review_dynamic_review_gain(
                    review_dynamic, value=effect.amount, count=1,
                ),
            )
            review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
            scalar, status_enchant, status_trace = (
                dispatch_plan2_native_status_enchant_event(
                    scalar,
                    status_enchant,
                    Plan2NativeStatusEnchantEvent(
                        phase=PHASE_STATUS_CHANGE,
                        changed_effect_type=_STATUS_CHILD_REVIEW,
                        status_difference=scalar.review - before.review,
                        status_change_committed=True,
                        suppressed_status_uids=item_suppressed_status_uids,
                        **event_base,
                    ),
                    remaining_turns=state.remaining_turns,
                    review_dynamic=review_dynamic,
                    status_child_compilation=catalog.status_child_compilation,
                    status_child_context=status_child_context,
                )
            )
            review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
            trace.extend(status_trace)
        elif effect.effect_type == ITEM_EFFECT_REVIEW_MULTIPLE:
            installed = simulate_review_multiple(
                scalar,
                Plan2ReviewMultipleEffect(
                    effect.effect_id,
                    effect.value1,
                    effect.effect_turn,
                ),
            )
            scalar = installed.after
            trace.append(
                "item-review-multiple-installed:"
                f"{effect.effect_id}:permil={effect.value1}:"
                f"turn={effect.effect_turn}:"
                f"uid={installed.created_status_uid or installed.merged_status_uid}"
            )
        elif effect.effect_type in {ITEM_EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE, ITEM_EFFECT_LESSON_DEPEND_BLOCK}:
            # Ratio lessons snapshot their live source at this item boundary,
            # convert the Master permille with the existing native kernel, then run the
            # ordinary per-hit lesson/parameter pipeline.  It is intentionally
            # independent of the owning item/card identity.
            block_snapshot = scalar.block
            requested = (native_lesson_block_ratio(block_snapshot, effect.amount)
                if effect.effect_type == ITEM_EFFECT_LESSON_DEPEND_BLOCK else
                get_ratio_effect_int_value(scalar.card_play_aggressive, effect.amount, is_ceil=True))
            lesson = handoff_plan2_native_review_dynamic_lesson(
                review_dynamic,
                Plan2NativeReviewDynamicLessonRequest(
                    value=requested,
                    count=effect.effect_count,
                    aggressive_at_execution=scalar.card_play_aggressive,
                    adding_status=AddingParameterStatus(
                        aggressive=scalar.card_play_aggressive,
                    ),
                    settings=AddingParameterSettings(),
                    application_status=scalar.parameter_application_status(
                        slump=False,
                    ),
                    play_origin=play_origin,
                ),
            )
            if not lesson.executable:
                raise Plan2NativeHorizonError(
                    "item-lesson-ratio-failed-closed",
                    f"{effect.effect_id}:{','.join(lesson.unresolved)}",
                )
            scalar = carry_plan2_state_application_status(
                scalar,
                lesson.final_application_status,
            )
            review_dynamic = _sync_review_dynamic(
                review_dynamic,
                scalar.review,
            )
            trace.extend(lesson.trace)
            if effect.effect_type == ITEM_EFFECT_LESSON_DEPEND_BLOCK and effect.value2:
                retained = native_lesson_retained_block(block_snapshot, effect.value2)
                consumed = max(_i32_add(block_snapshot, -retained, "item-block-consumption"), 0)
                scalar = replace(scalar, block=retained)
                status_child_context.block_consumption_sum_count = _i32_add(
                    status_child_context.block_consumption_sum_count, consumed, "block_consumption_sum_count")
                trace.append(f"item-lesson-block:score-before-retention:{block_snapshot}->{retained}")
        elif effect.effect_type == ITEM_EFFECT_REVIEW_REDUCE:
            remaining, _, _ = _consume_review_scalar(scalar.review,
                review_dynamic.review_status_present, review_dynamic.review_passing_turn_start, effect.amount)
            review_consumption_sum = _i32_add(review_consumption_sum,
                max(scalar.review - remaining, 0), "review_consumption_sum")
            scalar = replace(scalar, review=remaining)
            review_dynamic = _sync_review_dynamic(review_dynamic, remaining)
            trace.append(f"item-review-consume:{before.review}->{remaining}")
        elif effect.effect_type == ITEM_EFFECT_CARD_DRAW:
            before_zones = status_child_context.zones
            count = min(effect.amount, max(state.hand_limit - len(before_zones.hand), 0),
                len(before_zones.deck) + len(before_zones.grave))
            if count:
                draw = before_zones.draw_to_hand(count)
                scalar, _, draw_trace = _apply_generated_hand_arrivals(
                    scalar, before_zones, draw.state, status_child_context, reason="draw")
                trace.extend(draw_trace)
            trace.append(f"item-card-draw:actual={count}")
        elif effect.effect_type == ITEM_EFFECT_BLOCK_DEPEND_REVIEW:
            requested = get_ratio_effect_int_value(
                scalar.review,
                effect.amount,
                is_ceil=True,
            )
            delta = calculate_add_block(
                requested,
                is_buff_active=True,
                status=AddBlockStatus(
                    aggressive=scalar.card_play_aggressive,
                ),
                settings=AddBlockSettings(),
            )
            scalar = replace(
                scalar,
                block=_i32_add(scalar.block, delta, "block"),
            )
            if delta:
                scalar, status_enchant, status_trace = (
                    dispatch_plan2_native_status_enchant_event(
                        scalar,
                        status_enchant,
                        Plan2NativeStatusEnchantEvent(
                            phase=PHASE_STATUS_CHANGE,
                            changed_effect_type=_STATUS_CHILD_BLOCK,
                            status_difference=delta,
                            status_change_committed=True,
                            suppressed_status_uids=item_suppressed_status_uids,
                            **event_base,
                        ),
                        remaining_turns=state.remaining_turns,
                        review_dynamic=review_dynamic,
                        status_child_compilation=catalog.status_child_compilation,
                        status_child_context=status_child_context,
                    )
                )
                trace.extend(status_trace)
        elif effect.effect_type in {ITEM_EFFECT_BLOCK, ITEM_EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE}:
            # Native ExamBlock passes through CalculateAddBlock, so an
            # Aggressive child earlier in this same item batch is already
            # visible here.  Suppress the firing item listeners only from the
            # resulting StatusChange recursion; all other native listeners
            # still receive the exact committed Block delta.
            delta = calculate_add_block(
                effect.amount,
                is_buff_active=True,
                status=AddBlockStatus(
                    aggressive=scalar.card_play_aggressive,
                ),
                settings=AddBlockSettings(),
                additional_multiple_aggressive_rate=(
                    f32(permille_to_f32(effect.value2) + f32(1.0))
                    if effect.effect_type == ITEM_EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE
                    else 1.0
                ),
            )
            scalar = replace(
                scalar,
                block=_i32_add(scalar.block, delta, "block"),
            )
            if delta:
                scalar, status_enchant, status_trace = (
                    dispatch_plan2_native_status_enchant_event(
                        scalar,
                        status_enchant,
                        Plan2NativeStatusEnchantEvent(
                            phase=PHASE_STATUS_CHANGE,
                            changed_effect_type=_STATUS_CHILD_BLOCK,
                            status_difference=delta,
                            status_change_committed=True,
                            suppressed_status_uids=item_suppressed_status_uids,
                            **event_base,
                        ),
                        remaining_turns=state.remaining_turns,
                        review_dynamic=review_dynamic,
                        status_child_compilation=(
                            catalog.status_child_compilation
                        ),
                        status_child_context=status_child_context,
                    )
                )
                trace.extend(status_trace)
        elif effect.effect_type == ITEM_EFFECT_BLOCK_FIX:
            # Android ExamBlockFix bypasses CalculateAddBlock and applies the
            # fixed positive amount directly when Block is non-negative.
            delta = effect.amount
            scalar = replace(
                scalar,
                block=_i32_add(scalar.block, delta, "block"),
            )
            scalar, status_enchant, status_trace = (
                dispatch_plan2_native_status_enchant_event(
                    scalar,
                    status_enchant,
                    Plan2NativeStatusEnchantEvent(
                        phase=PHASE_STATUS_CHANGE,
                        changed_effect_type=_STATUS_CHILD_BLOCK,
                        status_difference=delta,
                        status_change_committed=True,
                        suppressed_status_uids=item_suppressed_status_uids,
                        **event_base,
                    ),
                    remaining_turns=state.remaining_turns,
                    review_dynamic=review_dynamic,
                    status_child_compilation=catalog.status_child_compilation,
                    status_child_context=status_child_context,
                )
            )
            trace.extend(status_trace)
        elif effect.effect_type == ITEM_EFFECT_AGGRESSIVE:
            scalar = _apply_aggressive_gain(scalar, value=effect.value1, count=max(1, effect.effect_count),
                effect_id=effect.effect_id, context=status_child_context, play_origin=play_origin)
            scalar, status_enchant, status_trace = (
                dispatch_plan2_native_status_enchant_event(
                    scalar,
                    status_enchant,
                    Plan2NativeStatusEnchantEvent(
                        phase=PHASE_STATUS_CHANGE,
                        changed_effect_type=_STATUS_CHILD_AGGRESSIVE,
                        status_difference=(
                            scalar.card_play_aggressive
                            - before.card_play_aggressive
                        ),
                        status_change_committed=True,
                        suppressed_status_uids=item_suppressed_status_uids,
                        **event_base,
                    ),
                    remaining_turns=state.remaining_turns,
                    review_dynamic=review_dynamic,
                    status_child_compilation=catalog.status_child_compilation,
                    status_child_context=status_child_context,
                )
            )
            trace.extend(status_trace)
        elif effect.effect_type == ITEM_EFFECT_STAMINA_CONSUMPTION_DOWN:
            status_uid_floor = _next_horizon_status_uid(
                replace(
                    state,
                    scalar=scalar,
                    status_enchant=status_enchant,
                    review_dynamic=review_dynamic,
                    stamina_modifiers=stamina_modifiers,
                    effect_chains=effect_chains,
                )
            )
            modifier_runtime = replace(
                stamina_modifiers,
                next_status_uid=max(
                    stamina_modifiers.next_status_uid,
                    status_uid_floor,
                ),
            )
            installed = apply_plan2_stamina_modifier(
                modifier_runtime,
                StaminaConsumptionDownEffect(
                    effect_id=effect.effect_id,
                    effect_turn=effect.effect_turn,
                    value1=effect.value1,
                    value2=effect.value2,
                    effect_count=effect.effect_count,
                ),
            )
            if not installed.executable:
                raise Plan2NativeHorizonError(
                    "item-stamina-down-install-failed-closed",
                    effect.effect_id,
                )
            stamina_modifiers = installed.after
            trace.append(
                f"item-stamina-down:{effect.effect_id}:"
                + ("installed" if installed.installed else "merged")
            )
        elif effect.effect_type == ITEM_EFFECT_LESSON_MULTIPLE:
            # Item children install the same finite LessonParameterMultiple
            # status as the existing card/timer path. Keep UID/lifetime and
            # Master child order; do not fold it into a one-shot score delta.
            next_uid = _next_horizon_status_uid(replace(
                state, scalar=scalar, status_enchant=status_enchant,
                review_dynamic=review_dynamic, stamina_modifiers=stamina_modifiers,
                effect_chains=effect_chains, debuff_registry=debuff_registry,
            ))
            installed = install_plan2_native_review_dynamic(
                replace(review_dynamic, next_status_uid=max(review_dynamic.next_status_uid, next_uid)),
                Plan2NativeReviewDynamicProgram(
                    card_id="item-runtime", upgrade=0, slot_index=0,
                    ordered_effect_ids=(effect.effect_id,), effect_id=effect.effect_id,
                    effect_type=effect.effect_type, operation_kind=OP_INSTALL_LESSON_MULTIPLE,
                    value1=effect.value1, value2=effect.value2,
                    effect_count=effect.effect_count, effect_turn=effect.effect_turn,
                ),
                Plan2NativeReviewDynamicInstallInput(
                    source_guid=f"item:{effect.effect_id}", play_origin=play_origin,
                    completed_effect_ids=(),
                ),
            )
            if not installed.executable:
                raise Plan2NativeHorizonError(
                    "item-lesson-multiple-install-failed-closed",
                    f"{effect.effect_id}:{','.join(installed.unresolved)}",
                )
            review_dynamic = installed.after
            scalar = replace(scalar, next_status_uid=max(scalar.next_status_uid, review_dynamic.next_status_uid))
            trace.extend(installed.trace)
        elif effect.effect_type == ITEM_EFFECT_LESSON_MULTIPLE_DOWN:
            gate = gate_plan2_native_status_addition(
                debuff_registry,
                effect.effect_type,
            )
            if not gate.resolved:
                raise Plan2NativeHorizonError(
                    "item-lesson-multiple-down-gate-failed-closed",
                    f"{effect.effect_id}:{gate.reason or ''}",
                )
            debuff_registry = gate.registry_after
            trace.extend(gate.queue_handoff.trace)
            if gate.blocked:
                trace.append(
                    f"item-lesson-multiple-down-blocked:{effect.effect_id}"
                )
            else:
                matching = [
                    layer_index
                    for layer_index, layer in enumerate(review_dynamic.layers)
                    if layer.kind == LAYER_LESSON_MULTIPLE_DOWN
                    and layer.turn == effect.effect_turn
                ]
                layers = list(review_dynamic.layers)
                if matching:
                    layer_index = matching[-1]
                    current = layers[layer_index]
                    layers[layer_index] = replace(
                        current,
                        permille=_i32_add(
                            current.permille,
                            effect.value1,
                            "lesson_multiple_down",
                        ),
                    )
                    trace.append(
                        f"item-lesson-multiple-down-merged:{effect.effect_id}:"
                        f"uid={current.status_uid}"
                    )
                else:
                    next_uid = _next_horizon_status_uid(
                        replace(
                            state,
                            scalar=scalar,
                            status_enchant=status_enchant,
                            review_dynamic=review_dynamic,
                            stamina_modifiers=stamina_modifiers,
                            effect_chains=effect_chains,
                            debuff_registry=debuff_registry,
                        )
                    )
                    layers.append(
                        Plan2NativeReviewDynamicLayer(
                            status_uid=next_uid,
                            kind=LAYER_LESSON_MULTIPLE_DOWN,
                            source_guid=f"item:{effect.effect_id}",
                            source_card_id="item-runtime",
                            source_upgrade=0,
                            source_effect_id=effect.effect_id,
                            permille=effect.value1,
                            turn=effect.effect_turn,
                        )
                    )
                    trace.append(
                        f"item-lesson-multiple-down-installed:{effect.effect_id}:"
                        f"uid={next_uid}"
                    )
                review_dynamic = replace(
                    review_dynamic,
                    layers=tuple(layers),
                    next_status_uid=max(
                        review_dynamic.next_status_uid,
                        max(layer.status_uid for layer in layers) + 1,
                    ),
                )
        elif effect.effect_type == ITEM_EFFECT_TIMER:
            child = effect.timer_child
            if child is None:
                raise Plan2NativeHorizonError(
                    "item-timer-child-missing",
                    effect.effect_id,
                )
            ordered_ids = tuple(value.effect_id for value in dispatched.effects)
            program = Plan2NativeEffectChainProgram(
                card_id="item-runtime",
                upgrade=0,
                slot_index=index,
                ordered_effect_ids=ordered_ids,
                timer_effect_id=effect.effect_id,
                delay=effect.value1,
                effect_count=effect.effect_count,
                effect_turn=effect.effect_turn,
                child_effect_id=child.effect_id,
                child_effect_type=child.effect_type,
                child_value1=child.value1,
                child_value2=child.value2,
                child_count=child.effect_count,
                child_turn=child.effect_turn,
                child_executor=EFFECT_CHAIN_EXECUTOR_BY_TYPE[child.effect_type],
            )
            next_uid = _next_horizon_status_uid(
                replace(
                    state,
                    scalar=scalar,
                    status_enchant=status_enchant,
                    review_dynamic=review_dynamic,
                    stamina_modifiers=stamina_modifiers,
                    effect_chains=effect_chains,
                )
            )
            install_runtime = replace(
                effect_chains,
                next_status_uid=max(effect_chains.next_status_uid, next_uid),
            )
            installed = install_plan2_native_effect_chain(
                install_runtime,
                program,
                Plan2NativeEffectChainInstallInput(
                    source_guid=f"item:{effect.effect_id}",
                    completed_effect_ids=ordered_ids[:index],
                ),
            )
            if not installed.executable or installed.installed is None:
                raise Plan2NativeHorizonError(
                    "item-timer-install-failed-closed",
                    f"{effect.effect_id}:{','.join(installed.unresolved)}",
                )
            effect_chains = installed.after
            trace.extend(installed.trace)
        elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
            plays_remaining = _i32_add(
                plays_remaining,
                effect.amount,
                "plays_remaining",
            )
        elif effect.effect_type == EFFECT_STAMINA_REDUCE_FIX:
            scalar = replace(scalar, stamina=max(0, scalar.stamina - effect.amount))
        elif effect.effect_type == ITEM_EFFECT_STAMINA_REDUCE:
            reduced = get_ratio_effect_int_value(
                scalar.max_stamina,
                effect.amount,
                is_ceil=False,
            )
            scalar = replace(
                scalar,
                stamina=max(0, scalar.stamina - reduced),
            )
        else:  # Constructor/compile boundary should make this unreachable.
            raise Plan2NativeHorizonError(
                "item-effect-owner-unbound",
                f"{effect.effect_type}:{effect.effect_id}",
            )
        trace.append(
            f"item-effect:{index}:{effect.effect_id}:{effect.effect_type}:"
            f"amount={effect.amount}"
        )

    after = replace(
        state,
        scalar=scalar,
        plays_remaining=plays_remaining,
        review_consumption_sum=review_consumption_sum,
        item_runtime=dispatched.after,
        aggressive_additive_runtime=_context_aggressive_runtime(status_child_context, scalar, state.aggressive_additive_runtime),
        stamina_modifiers=stamina_modifiers,
        effect_chains=effect_chains,
        debuff_registry=debuff_registry,
        status_enchant=status_enchant,
        review_dynamic=review_dynamic,
        triggered_review_status_runtime=replace(
            state.triggered_review_status_runtime,
            review_runtime=review_dynamic,
        ),
        zones=status_child_context.zones,
        status_child_inputs=tuple(status_child_context.inputs),
        status_child_review_state=(
            None
            if status_child_context.review_state is None
            else replace(status_child_context.review_state, plan2_state=scalar)
        ),
        total_effect_draw_card_count=(
            status_child_context.total_effect_draw_card_count
        ),
        block_consumption_sum_count=(
            status_child_context.block_consumption_sum_count
        ),
        block_fix_restriction_runtime=(None if state.block_fix_restriction_runtime is None else
            replace(state.block_fix_restriction_runtime, block=scalar.block,
                block_consumption_sum_count=status_child_context.block_consumption_sum_count)),
        generated_hand_move_turn=status_child_context.hand_move_turn,
        turn_used_support_ids=status_child_context.turn_used_support_ids,
    )
    shared_next_status_uid = _next_horizon_status_uid(after)
    if after.scalar.next_status_uid < shared_next_status_uid:
        after = replace(
            after,
            scalar=replace(
                after.scalar,
                next_status_uid=shared_next_status_uid,
            ),
        )
    return after, tuple(trace)


def _execute_play(
    state: Plan2NativeHorizonState,
    card_guid: str,
    catalog: Plan2NativeProgramCatalog,
    *,
    pay_cost: bool = True,
    spend_play: bool = True,
    forced: bool = False,
    queue_limit: int = 64,
    replay_ancestry: tuple[tuple[int, str], ...] = (),
    active_encore_handoff_id: str = "",
    detached_card: NativeOrderedCardInstance | None = None,
    operation_receipts: list[Plan2NativeOperationReceipt] | None = None,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    if state.terminal:
        raise Plan2NativeHorizonError("terminal-state-has-no-actions")
    if state.opaque_status_queue:
        raise Plan2NativeHorizonError(
            "opaque-status-queue-unresolved",
            ",".join(state.opaque_status_queue),
        )
    forced_source_zone = "hand"
    forced_before_zones = state.zones
    is_detached = detached_card is not None
    if is_detached:
        assert detached_card is not None
        if not forced:
            raise Plan2NativeHorizonError(
                "detached-card-requires-forced-play",
                card_guid,
            )
        if detached_card.guid != card_guid:
            raise Plan2NativeHorizonError(
                "detached-card-guid-mismatch",
                card_guid,
            )
        if any(card.guid == card_guid for card in state.zones.card_universe):
            raise Plan2NativeHorizonError(
                "detached-card-guid-already-positioned",
                card_guid,
            )
        state = replace(
            state,
            zones=replace(
                state.zones,
                card_universe=tuple(
                    sorted(
                        (*state.zones.card_universe, detached_card),
                        key=lambda card: card.guid,
                    )
                ),
                hand=(*state.zones.hand, detached_card),
            ),
        )
        index = len(state.zones.hand) - 1
        forced_source_zone = "random_pool"
    else:
        index = next(
            (
                i
                for i, card in enumerate(state.zones.hand)
                if card.guid == card_guid
            ),
            None,
        )
    if index is None and forced:
        for zone_name in ("deck", "grave", "lost"):
            zone = getattr(state.zones, zone_name)
            zone_index = next(
                (i for i, value in enumerate(zone) if value.guid == card_guid),
                None,
            )
            if zone_index is None:
                continue
            forced_card = zone[zone_index]
            state = replace(
                state,
                zones=replace(
                    state.zones,
                    hand=(*state.zones.hand, forced_card),
                    **{
                        zone_name: (
                            *zone[:zone_index],
                            *zone[zone_index + 1 :],
                        )
                    },
                ),
            )
            index = len(state.zones.hand) - 1
            forced_source_zone = zone_name
            break
    if index is None:
        raise Plan2NativeHorizonError("card-guid-not-in-hand", card_guid)
    card = state.zones.hand[index]
    program = materialize_plan2_native_card_program(catalog, card)
    status_child_context = _status_child_context_from_state(
        state,
        catalog,
        playing_guid=card_guid,
    )
    forced_hand_move_trace: tuple[str, ...] = ()
    if forced_source_zone in {"deck", "grave"}:
        moved_scalar, moved_zones, forced_hand_move_trace = (
            _apply_generated_hand_arrivals(
                state.scalar,
                forced_before_zones,
                state.zones,
                status_child_context,
                reason="hand-add",
            )
        )
        state = replace(
            state,
            scalar=moved_scalar,
            zones=moved_zones,
            block_consumption_sum_count=(
                status_child_context.block_consumption_sum_count
            ),
            generated_hand_move_turn=status_child_context.hand_move_turn,
            turn_used_support_ids=status_child_context.turn_used_support_ids,
        )
    if spend_play and state.plays_remaining < 1:
        raise Plan2NativeHorizonError("illegal-action-no-playable-count", card_guid)
    if program.native_horizon_predicate is not None:
        admitted = program.native_horizon_predicate(
            state,
            "forced" if forced else "ordinary",
        )
        if type(admitted) is not bool:
            raise Plan2NativeHorizonError(
                "card-play-predicate-failed-closed",
                f"{program.native_horizon_predicate_id}:non-boolean-result",
            )
        if not admitted:
            if is_detached:
                return replace(state, zones=forced_before_zones), (
                    "forced-random-pool-invalid:"
                    f"{card.card_id}@{card.effective_upgrade}:"
                    f"{program.native_horizon_predicate_id}",
                )
            raise Plan2NativeHorizonError(
                "card-play-condition-not-met",
                program.native_horizon_predicate_id,
            )
    elif program.native_play_predicate is not None:
        admitted = program.native_play_predicate(state.scalar)
        if type(admitted) is not bool:
            raise Plan2NativeHorizonError(
                "card-play-predicate-failed-closed",
                f"{program.native_play_predicate_id}:non-boolean-result",
            )
        if not admitted:
            if is_detached:
                return replace(state, zones=forced_before_zones), (
                    "forced-random-pool-invalid:"
                    f"{card.card_id}@{card.effective_upgrade}:"
                    f"{program.native_play_predicate_id}",
                )
            raise Plan2NativeHorizonError(
                "card-play-condition-not-met",
                program.native_play_predicate_id,
            )

    # Android materializes the playing card's first ``PlayCardCount`` before
    # the cost/direct-effect transaction starts.  Keep that runtime update on
    # the ordered zone object so every effect executor sees the same Playing
    # instance (including its play count), while the previous count remains
    # recoverable by the once-effect scheduler.  This is a lifecycle update,
    # not a card-specific effect and applies to ordinary and forced plays.
    previous_card = next(
        value for value in state.zones.hand if value.guid == card_guid
    )
    card = previous_card.increment_play_count()
    if operation_receipts is not None:
        operation_receipts.append(
            Plan2NativeOperationReceipt(
                family=CARD_PLAY_COUNT_RECEIPT_FAMILY,
                operation=CARD_PLAY_COUNT_INCREMENT_OPERATION,
                subject_id=previous_card.guid,
                owner=CARD_PLAY_COUNT_BUILD_OWNER,
                before_value=previous_card.runtime_state.play_count,
                after_value=card.runtime_state.play_count,
            )
        )
    state = replace(
        state,
        zones=state.zones.replace_hand_runtime(
            previous_card,
            card,
        ),
    )
    # The context was created before this runtime-only update.  Keep its
    # transaction-local zones synchronized before any Playing callback or
    # effect search observes the card.
    status_child_context.zones = state.zones

    # Item listeners share the native status UID allocator.  The mixed
    # Playing executor does not own the item runtime, so carry its active UID
    # floor through the scalar allocator before any card-installed status is
    # created.  Invalid detached plays returned above remain a true no-op.
    item_uid_floor = max(
        (state.scalar.next_status_uid,)
        + tuple(value + 1 for value in state.item_runtime.active_status_uids)
    )
    if item_uid_floor != state.scalar.next_status_uid:
        state = replace(
            state,
            scalar=replace(state.scalar, next_status_uid=item_uid_floor),
        )
        status_child_context = _status_child_context_from_state(
            state,
            catalog,
            playing_guid=card_guid,
        )

    scalar = state.scalar
    stamina_modifiers = state.stamina_modifiers
    status_enchant = state.status_enchant
    review_dynamic = _sync_review_dynamic(state.review_dynamic, scalar.review)
    debuff_registry = state.debuff_registry
    effect_chains = state.effect_chains
    generated_runtime = state.generated_runtime
    item_runtime = state.item_runtime
    generated_input = next(
        (
            value
            for value in state.generated_inputs
            if value.source_guid == card_guid
        ),
        None,
    )
    encore_runtime = _sync_encore_runtime(
        state.encore_runtime,
        state.zones,
        scalar,
    )
    aggressive_additive_runtime = state.aggressive_additive_runtime
    block_fix_restriction_runtime = state.block_fix_restriction_runtime
    extra_turn_runtime = state.extra_turn_runtime
    extra_turn = state.extra_turn
    stamina_recover_restricted = state.stamina_recover_restricted
    stamina_recover_add_permil = state.stamina_recover_add_permil
    search_play_card_stamina_runtime = state.search_play_card_stamina_runtime
    search_payment = _search_stamina_payment(program, card,
        search_play_card_stamina_runtime, simulate=False) if pay_cost else None
    search_override = None
    if search_payment is not None and search_payment.cost_source == "search-override":
        search_override = search_payment.selected_stamina_cost
        search_play_card_stamina_runtime = search_payment.runtime_after
    add_grow_runtime = state.add_grow_runtime
    start_play_listeners = state.start_play_listeners
    triggered_review_status_runtime = replace(
        state.triggered_review_status_runtime,
        review_runtime=review_dynamic,
    )
    review_consumption_sum = state.review_consumption_sum
    block_consumption_sum_count = state.block_consumption_sum_count
    plays_remaining = state.plays_remaining
    cost_trace: tuple[str, ...] = ()
    item_play_trace: tuple[str, ...] = ()

    def dispatch_card_play_item_event(value: Plan2State) -> Plan2State:
        """Run the generic item CardPlay boundary before card direct effects."""

        nonlocal block_consumption_sum_count, item_play_trace, item_runtime
        nonlocal plays_remaining, review_consumption_sum, review_dynamic, scalar
        nonlocal status_child_context, status_enchant
        nonlocal stamina_modifiers, effect_chains
        nonlocal triggered_review_status_runtime
        nonlocal aggressive_additive_runtime
        event_review_dynamic = _sync_review_dynamic(review_dynamic, value.review)
        item_event_state = replace(
            state,
            scalar=value,
            aggressive_additive_runtime=_context_aggressive_runtime(status_child_context, value, aggressive_additive_runtime),
            zones=status_child_context.zones,
            status_enchant=status_enchant,
            review_dynamic=event_review_dynamic,
            triggered_review_status_runtime=replace(
                state.triggered_review_status_runtime,
                review_runtime=event_review_dynamic,
            ),
            item_runtime=item_runtime,
            search_play_card_stamina_runtime=search_play_card_stamina_runtime,
            stamina_modifiers=stamina_modifiers,
            effect_chains=effect_chains,
            plays_remaining=plays_remaining,
            review_consumption_sum=review_consumption_sum,
            block_consumption_sum_count=(
                status_child_context.block_consumption_sum_count
            ),
            status_child_inputs=tuple(status_child_context.inputs),
            status_child_review_state=(
                None
                if status_child_context.review_state is None
                else replace(
                    status_child_context.review_state,
                    plan2_state=value,
                )
            ),
            total_effect_draw_card_count=(
                status_child_context.total_effect_draw_card_count
            ),
            generated_hand_move_turn=status_child_context.hand_move_turn,
            turn_used_support_ids=status_child_context.turn_used_support_ids,
        )
        item_event_state, event_trace = _execute_item_phase(
            item_event_state,
            catalog,
            Plan2NativeItemEvent(
                ITEM_PHASE_CARD_PLAY,
                value.current_turn,
                card.card_id,
                card.effective_upgrade,
                lesson_type=_item_status_change_lesson_type(item_event_state),
                card_category=program.category,
                card_effect_group_ids=program.effect_group_ids,
                current_stamina=state.scalar.stamina,
                max_stamina=state.scalar.max_stamina,
            ),
            card_program=program,
            play_origin="forced" if forced else "normal",
            playing_guid=card.guid,
        )
        scalar = item_event_state.scalar
        aggressive_additive_runtime = item_event_state.aggressive_additive_runtime
        status_enchant = item_event_state.status_enchant
        review_dynamic = item_event_state.review_dynamic
        triggered_review_status_runtime = (
            item_event_state.triggered_review_status_runtime
        )
        item_runtime = item_event_state.item_runtime
        stamina_modifiers = item_event_state.stamina_modifiers
        effect_chains = item_event_state.effect_chains
        review_consumption_sum = item_event_state.review_consumption_sum
        block_consumption_sum_count = item_event_state.block_consumption_sum_count
        plays_remaining = item_event_state.plays_remaining
        status_child_context = _status_child_context_from_state(
            item_event_state,
            catalog,
            playing_guid=card.guid,
        )
        # Keep the established trace compact when no CardPlay item listener
        # fires, while retaining the complete provenance for a real dispatch.
        item_play_trace = (
            event_trace
            if any(value.startswith("item-fire:") for value in event_trace)
            else ()
        )
        return scalar

    if scalar.card_play_listeners:
        if status_enchant.listeners:
            raise Plan2NativeHorizonError(
                "mixed-card-play-listener-runtime-unbound",
                "legacy+native-status-enchant",
            )
        if program.native_playing_executor is not None:
            raise Plan2NativeHorizonError(
                "playing-executor-with-card-play-listener-unbound",
                program.native_playing_executor_id,
            )
        if program.playing_search is None:
            raise Plan2NativeHorizonError(
                "card-play-listener-search-unbound",
                f"{card.card_id}@{card.effective_upgrade}",
            )
        def pay_listener_cost(value: Plan2State) -> Plan2State:
            nonlocal review_consumption_sum, block_consumption_sum_count, cost_trace
            paid, review_consumption_sum, cost_trace = _pay_cost(
                value,
                program,
                pay_cost=pay_cost,
                modifiers=stamina_modifiers,
                review_consumption_sum=review_consumption_sum,
                origin=PlayOrigin.FORCED if forced else PlayOrigin.NORMAL,
                search_override=search_override,
            )
            block_consumption_sum_count = _i32_add(
                block_consumption_sum_count,
                max(value.block - paid.block, 0),
                "block_consumption_sum_count",
            )
            status_child_context.block_consumption_sum_count = (
                block_consumption_sum_count
            )
            return paid

        listener_transition = simulate_plan2_exam_card_play(
            scalar,
            state.zones,
            card.guid,
            Plan2PlayingCard(
                id=card.card_id,
                upgrade=card.effective_upgrade,
                category=program.category,
                effect_group_ids=program.effect_group_ids,
            ),
            program.playing_search,
            pay_cost=pay_listener_cost,
            apply_direct_effects=lambda value: _apply_scalar_program(
                dispatch_card_play_item_event(value),
                program,
                status_child_context=status_child_context,
            ),
        )
        scalar = listener_transition.after
        trace = [*listener_transition.event_trace, *cost_trace, *item_play_trace]
    else:
        scalar_before_cost = scalar
        scalar, review_consumption_sum, cost_trace = _pay_cost(
            scalar,
            program,
            pay_cost=pay_cost,
            modifiers=stamina_modifiers,
            review_consumption_sum=review_consumption_sum,
            origin=PlayOrigin.FORCED if forced else PlayOrigin.NORMAL,
            search_override=search_override,
        )
        block_consumption_sum_count = _i32_add(
            block_consumption_sum_count,
            max(scalar_before_cost.block - scalar.block, 0),
            "block_consumption_sum_count",
        )
        status_child_context.block_consumption_sum_count = (
            block_consumption_sum_count
        )
        review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
        play_origin = "forced" if forced else "normal"
        event_base = dict(
            accepted=True,
            play_origin=play_origin,
            round_number=scalar.current_turn,
            card_id=card.card_id,
            card_upgrade=card.effective_upgrade,
            card_category=program.category,
            card_effect_group_ids=program.effect_group_ids,
            card_position=POSITION_PLAYING,
        )
        status_trace: list[str] = []
        # Payment is not an AddStatusEffect event. Native buff payment queries
        # GetConsumeEffectTriggerEffectList (APK 0x7ED0598), not the StatusChange
        # listener family. Replaying negative resource deltas as StatusChange
        # incorrectly fired an on-motivation-change scoring listener on costs.
        if scalar.stamina < scalar_before_cost.stamina:
            scalar, status_enchant, dispatched = (
                dispatch_plan2_native_status_enchant_event(
                    scalar,
                    status_enchant,
                    Plan2NativeStatusEnchantEvent(
                        phase=PHASE_STAMINA_REDUCE_CARD,
                        **event_base,
                    ),
                    remaining_turns=state.remaining_turns,
                    review_dynamic=review_dynamic,
                    status_child_compilation=catalog.status_child_compilation,
                    status_child_context=status_child_context,
                )
            )
            status_trace.extend(dispatched)
            review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
        # The distinct CardPlay item boundary runs while the selected card is
        # Playing, after cost payment and before the card's direct effects.
        scalar = dispatch_card_play_item_event(scalar)
        scalar, status_enchant, dispatched = dispatch_plan2_native_status_enchant_event(
            scalar,
            status_enchant,
            Plan2NativeStatusEnchantEvent(phase=PHASE_CARD_PLAY, **event_base),
            remaining_turns=state.remaining_turns,
            review_dynamic=review_dynamic,
            status_child_compilation=catalog.status_child_compilation,
            status_child_context=status_child_context,
        )
        status_trace.extend(dispatched)
        # CardPlay children can add Review before the independent play-count
        # event. Pass that committed value into its shared additive owner;
        # retaining the pre-CardPlay mirror loses the first child's change.
        review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
        scalar, status_enchant, dispatched = (
            dispatch_plan2_native_status_enchant_event(
                scalar,
                status_enchant,
                Plan2NativeStatusEnchantEvent(
                    phase=PHASE_PLAY_COUNT_INTERVAL,
                    **{**event_base, "card_position": POSITION_TARGET},
                ),
                remaining_turns=state.remaining_turns,
                review_dynamic=review_dynamic,
                status_child_compilation=catalog.status_child_compilation,
                status_child_context=status_child_context,
            )
        )
        status_trace.extend(dispatched)
        review_dynamic = _sync_review_dynamic(
            review_dynamic,
            scalar.review,
        )
        if program.native_playing_executor is None:
            scalar = _apply_scalar_program(scalar, program, status_child_context=status_child_context)
        trace = [
            f"card:{card.card_id}@{card.effective_upgrade}:{'forced' if forced else 'normal'}",
            f"play-source:{forced_source_zone}",
            f"cost:{program.cost_type}:{program.cost_value if pay_cost else 0}",
            "direct-effects:ordered-explicit-program",
            *cost_trace,
            *status_trace,
            *item_play_trace,
            *forced_hand_move_trace,
        ]
        if program.native_direct_executor_id:
            trace.append(f"native-direct:{program.native_direct_executor_id}")

    # Native direct effects observe the played card at the Playing boundary:
    # it is no longer in Hand, but it has not yet been settled or had its play
    # count incremented.  Zone/search adapters run exactly at that boundary.
    live_index = next(
        (
            value_index
            for value_index, value in enumerate(status_child_context.zones.hand)
            if value.guid == card_guid
        ),
        None,
    )
    if live_index is None:
        raise Plan2NativeHorizonError(
            "status-child-moved-playing-card-before-playing-boundary",
            card_guid,
        )
    zones, pending = status_child_context.zones.remove_hand(live_index)
    status_child_context.zones = zones
    playable_delta = 0
    playing_queued_actions: tuple[Plan2NativeQueuedAction, ...] = ()
    if program.native_playing_executor is not None:
        review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
        try:
            playing_result = program.native_playing_executor(
                scalar,
                zones,
                state.hand_limit,
                stamina_modifiers,
                status_enchant,
                review_dynamic,
                debuff_registry,
                effect_chains,
                scalar_before_cost,
                generated_runtime,
                generated_input,
                encore_runtime,
                review_consumption_sum,
                block_consumption_sum_count,
                card.guid,
                "forced" if forced else "normal",
                state.remaining_turns,
                replay_ancestry,
                status_child_context,
                _context_aggressive_runtime(status_child_context, scalar, aggressive_additive_runtime),
                state.block_fix_restriction_runtime,
                state.extra_turn_runtime,
                state.stamina_recover_restricted,
                state.stamina_recover_add_permil,
                search_play_card_stamina_runtime,
                state.add_grow_runtime,
                start_play_listeners,
                triggered_review_status_runtime,
                item_runtime,
                _item_status_change_lesson_type(state),
            )
        except (TypeError, ValueError, OverflowError) as error:
            raise Plan2NativeHorizonError(
                "native-playing-executor-failed-closed",
                f"{program.native_playing_executor_id}:{error}",
            ) from error
        if not isinstance(playing_result, Plan2NativePlayingEffectResult):
            raise TypeError(
                "native_playing_executor must return Plan2NativePlayingEffectResult"
            )
        if playing_result.zones.pending_played != pending:
            raise Plan2NativeHorizonError(
                "native-playing-boundary-changed",
                program.native_playing_executor_id,
            )
        scalar = playing_result.scalar
        zones = playing_result.zones
        status_child_context.zones = zones
        stamina_modifiers = playing_result.stamina_modifiers
        status_enchant = playing_result.status_enchant
        review_dynamic = playing_result.review_dynamic
        debuff_registry = playing_result.debuff_registry
        effect_chains = playing_result.effect_chains
        generated_runtime = playing_result.generated_runtime
        item_runtime = playing_result.item_runtime
        encore_runtime = playing_result.encore_runtime
        aggressive_additive_runtime = playing_result.aggressive_additive_runtime
        status_child_context.aggressive_additive_runtime = aggressive_additive_runtime
        block_fix_restriction_runtime = (
            playing_result.block_fix_restriction_runtime
        )
        extra_turn_runtime = playing_result.extra_turn_runtime
        extra_turn = (
            state.extra_turn
            if extra_turn_runtime is None
            else extra_turn_runtime.extra_turn
        )
        # ExamExtraTurn is one native transaction.  The horizon boundary and
        # the score applicator both carry the resulting counter, so commit the
        # same value to both snapshots before any automatic EndTurn/TurnStart
        # suffix runs.  Leaving the score context at the pre-card value makes
        # a legal lesson extra turn look like an exhausted schedule.
        if (
            scalar.battle_scoring is not None
            and scalar.battle_scoring.extra_turn != extra_turn
        ):
            scalar = replace(
                scalar,
                battle_scoring=replace(
                    scalar.battle_scoring,
                    extra_turn=extra_turn,
                ),
            )
        stamina_recover_restricted = playing_result.stamina_recover_restricted
        stamina_recover_add_permil = playing_result.stamina_recover_add_permil
        search_play_card_stamina_runtime = (
            playing_result.search_play_card_stamina_runtime
        )
        add_grow_runtime = playing_result.add_grow_runtime
        start_play_listeners = playing_result.start_play_listeners
        triggered_review_status_runtime = (
            playing_result.triggered_review_status_runtime
        )
        review_consumption_sum = playing_result.review_consumption_sum
        block_consumption_sum_count = playing_result.block_consumption_sum_count
        status_child_context.block_consumption_sum_count = (
            block_consumption_sum_count
        )
        playable_delta = playing_result.playable_delta
        playing_queued_actions = playing_result.queued_actions
        trace.append(f"native-playing:{program.native_playing_executor_id}")
        trace.extend(playing_result.trace)
    if program.native_zone_executor is not None:
        zones, zone_trace = program.native_zone_executor(zones, state.hand_limit)
        status_child_context.zones = zones
        if not isinstance(zones, NativeOrderedZoneState):
            raise TypeError("native_zone_executor must return NativeOrderedZoneState")
        if (
            zones.pending_played is None
            or zones.pending_played.guid != pending.guid
        ):
            raise Plan2NativeHorizonError(
                "native-zone-playing-boundary-changed",
                program.native_zone_executor_id,
            )
        trace.append(f"native-zone:{program.native_zone_executor_id}")
        trace.extend(zone_trace)
        pending = zones.pending_played
    # The build-side PlayCardCount above is visible while the card is Playing.
    # Native MovePlayCard performs its second runtime increment only for the
    # ordinary Hand command that consumes a playable count.  Forced plays and
    # other non-playable-count commands settle with the build-side value.
    move_uses_playable_count = not forced and spend_play
    updated = (
        pending.increment_play_count()
        if move_uses_playable_count
        else pending
    )
    if operation_receipts is not None and move_uses_playable_count:
        operation_receipts.append(
            Plan2NativeOperationReceipt(
                family=CARD_PLAY_COUNT_RECEIPT_FAMILY,
                operation=CARD_PLAY_COUNT_INCREMENT_OPERATION,
                subject_id=pending.guid,
                owner=CARD_PLAY_COUNT_MOVE_OWNER,
                before_value=pending.runtime_state.play_count,
                after_value=updated.runtime_state.play_count,
            )
        )
    zones = zones.replace_pending_runtime(pending, updated)
    if is_detached:
        zones = replace(
            zones,
            card_universe=tuple(
                value
                for value in zones.card_universe
                if value.guid != updated.guid
            ),
            pending_played=None,
        )
    else:
        zones = (
            zones.append_played_to_lost(updated)
            if program.move_destination == "lost"
            else zones.append_played_to_grave(updated)
        )
    status_child_context.zones = zones
    completed = complete_plan2_card_play_after_move(
        scalar,
        card_move_completed=True,
    )
    event_base = dict(
        accepted=True,
        play_origin="forced" if forced else "normal",
        round_number=completed.after.current_turn,
        card_id=card.card_id,
        card_upgrade=card.effective_upgrade,
        card_category=program.category,
        card_effect_group_ids=program.effect_group_ids,
        card_position=POSITION_PLAYING,
    )
    scalar_after_events, status_enchant, status_trace = (
        dispatch_plan2_native_status_enchant_event(
            completed.after,
            status_enchant,
            Plan2NativeStatusEnchantEvent(
                phase=PHASE_CARD_PLAY_AFTER,
                **event_base,
            ),
            remaining_turns=state.remaining_turns,
            review_dynamic=review_dynamic,
            status_child_compilation=catalog.status_child_compilation,
            status_child_context=status_child_context,
        )
    )
    zones = status_child_context.zones
    after = replace(
        state,
        scalar=scalar_after_events,
        zones=zones,
        stamina_modifiers=stamina_modifiers,
        status_enchant=status_enchant,
        review_dynamic=_sync_review_dynamic(
            review_dynamic,
            scalar_after_events.review,
        ),
        debuff_registry=debuff_registry,
        effect_chains=effect_chains,
        generated_runtime=generated_runtime,
        item_runtime=item_runtime,
        generated_inputs=tuple(
            value for value in state.generated_inputs if value.source_guid != card_guid
        ),
        encore_runtime=encore_runtime,
        aggressive_additive_runtime=_context_aggressive_runtime(status_child_context, scalar, aggressive_additive_runtime),
        block_fix_restriction_runtime=block_fix_restriction_runtime,
        extra_turn_runtime=extra_turn_runtime,
        extra_turn=extra_turn,
        stamina_recover_restricted=stamina_recover_restricted,
        stamina_recover_add_permil=stamina_recover_add_permil,
        search_play_card_stamina_runtime=search_play_card_stamina_runtime,
        add_grow_runtime=add_grow_runtime,
        start_play_listeners=start_play_listeners,
        triggered_review_status_runtime=replace(
            triggered_review_status_runtime,
            review_runtime=_sync_review_dynamic(
                review_dynamic,
                scalar_after_events.review,
            ),
        ),
        review_consumption_sum=review_consumption_sum,
        block_consumption_sum_count=(
            status_child_context.block_consumption_sum_count
        ),
        plays_remaining=_i32_add(
            plays_remaining - (1 if spend_play else 0),
            playable_delta,
            "plays_remaining",
        ),
        command_queue=(
            *state.command_queue,
            *program.commands,
            *playing_queued_actions,
        ),
        status_child_inputs=tuple(status_child_context.inputs),
        status_child_review_state=(
            None
            if status_child_context.review_state is None
            else replace(
                status_child_context.review_state,
                plan2_state=scalar_after_events,
            )
        ),
        total_effect_draw_card_count=(
            status_child_context.total_effect_draw_card_count
        ),
        generated_hand_move_turn=status_child_context.hand_move_turn,
        turn_used_support_ids=status_child_context.turn_used_support_ids,
    )
    trace.extend(
        (
            (
                f"move:random_pool-drop:{card.guid}"
                if is_detached
                else f"move:{program.move_destination}:{card.guid}"
            ),
            (
                "card-play-counts:increment-after-move"
                if move_uses_playable_count
                else "card-play-counts:move-side-not-applicable"
            ),
            *status_trace,
        )
    )
    after, item_trace = _execute_item_phase(
        after,
        catalog,
        Plan2NativeItemEvent(
            ITEM_PHASE_CARD_PLAY_AFTER,
            after.scalar.current_turn,
            card.card_id,
            card.effective_upgrade,
            review=after.scalar.review,
            aggressive=after.scalar.card_play_aggressive,
            card_category=program.category,
            card_effect_group_ids=program.effect_group_ids,
        ),
        card_program=program,
        play_origin="forced" if forced else "normal",
    )
    trace.extend(item_trace)
    if any(
        listener.trigger.phase == ITEM_PHASE_PLAY_TURN_COUNT_INTERVAL
        and (listener.max_uses == 0 or listener.remaining_uses > 0)
        for listener in after.item_runtime.listeners
    ):
        play_turn_phase_values = (after.scalar.turn_card_play_count,)
        target_search_matches = {
            spec.produce_card_search_id: True
            for listener in after.item_runtime.listeners
            if listener.trigger.phase == ITEM_PHASE_PLAY_TURN_COUNT_INTERVAL
            and (spec := listener.trigger.master_trigger) is not None
            and spec.produce_card_search_id
            and spec.card_search_rule is not None
            and spec.card_search_rule.card_position_type
            == "ProduceCardPositionType_Target"
        }
        after, item_play_turn_trace = _execute_item_phase(
            after,
            catalog,
            Plan2NativeItemEvent(
                ITEM_PHASE_PLAY_TURN_COUNT_INTERVAL,
                after.scalar.current_turn,
                phase_values=play_turn_phase_values,
                card_search_matches=target_search_matches,
            ),
            card_program=program,
            play_origin="forced" if forced else "normal",
        )
        trace.extend(item_play_turn_trace)
    if encore_runtime is not None:
        encore_runtime = _sync_encore_runtime(
            encore_runtime,
            after.zones,
            after.scalar,
        )
        assert encore_runtime is not None
        after = replace(after, encore_runtime=encore_runtime)
        after, encore_direct_trace = _drain_encore_handoffs(
            after,
            catalog,
            queue_limit=queue_limit,
            skip_handoff_id=active_encore_handoff_id,
        )
        trace.extend(encore_direct_trace)
        assert after.encore_runtime is not None
        settled_card = (
            updated
            if is_detached
            else next(
                value
                for value in (*after.zones.grave, *after.zones.lost)
                if value.guid == card.guid
            )
        )
        planned = plan_plan2_native_status_enchant_encore(
            after.encore_runtime,
            Plan2NativeStatusEnchantEncoreEvent(
                event_id=f"{card.guid}:card-play-after",
                phase=ENCORE_PHASE_CARD_PLAY_AFTER,
                target_card=_plan3_card_from_ordered(settled_card),
                replay_ancestry=replay_ancestry,
                play_origin="forced" if forced else "ordinary",
            ),
        )
        if not planned.executable:
            raise Plan2NativeHorizonError(
                "encore-card-play-after-failed-closed",
                planned.reason,
            )
        after = replace(after, encore_runtime=planned.after)
        trace.extend(
            f"encore-difference:{value!r}" for value in planned.differences
        )
        after, encore_after_trace = _drain_encore_handoffs(
            after,
            catalog,
            queue_limit=queue_limit,
            skip_handoff_id=active_encore_handoff_id,
        )
        trace.extend(encore_after_trace)
        after = replace(
            after,
            encore_runtime=_sync_encore_runtime(
                after.encore_runtime,
                after.zones,
                after.scalar,
            ),
        )
    after, drained_trace = _drain_commands(after, catalog, queue_limit=queue_limit)
    trace.extend(drained_trace)
    after, limit_trace = _finalize_plan2_lesson_limit(after)
    trace.extend(limit_trace)
    if after.terminal:
        return after, tuple(trace)
    if program.ends_turn:
        after, end_trace = _execute_end_turn(
            after,
            catalog,
            queue_limit=queue_limit,
            # A card's own ends_turn flag enters the automatic lifecycle; it
            # is not the explicit player EndTurn/ExamTurnSkip command and
            # therefore does not receive ExamSetting recovery.
            apply_turn_end_recovery=False,
        )
        trace.extend(end_trace)
    return after, tuple(trace)


def _drain_commands(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    queue_limit: int,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    after = state
    trace: list[str] = []
    executed = 0
    while after.command_queue:
        if executed >= queue_limit:
            raise Plan2NativeHorizonError("nested-command-limit-exceeded", str(queue_limit))
        command = after.command_queue[0]
        after = replace(after, command_queue=after.command_queue[1:])
        executed += 1
        if command.kind == "force_play":
            after, child_trace = _execute_play(
                after,
                command.card_guid,
                catalog,
                pay_cost=command.pay_cost,
                spend_play=command.spend_play,
                forced=True,
                queue_limit=queue_limit - executed,
            )
            trace.append(f"nested:force-play:{command.card_guid}")
            trace.extend(child_trace)
            continue
        if command.kind == "draw":
            after, draw_trace = _replace_with_draw(
                after,
                command.value,
                catalog,
                effect_draw=True,
            )
            trace.append("nested:draw")
            trace.extend(draw_trace)
            continue
        if command.kind == "extra_turn":
            extra_turn = _i32_add(
                after.extra_turn,
                command.value,
                "extra_turn",
            )
            scalar = after.scalar
            if scalar.battle_scoring is not None:
                scalar = replace(
                    scalar,
                    battle_scoring=replace(
                        scalar.battle_scoring,
                        extra_turn=extra_turn,
                    ),
                )
            after = replace(after, scalar=scalar, extra_turn=extra_turn)
            trace.append(f"nested:extra-turn:+{command.value}")
            continue
        if command.kind == "add_playable":
            after = replace(
                after,
                plays_remaining=_i32_add(
                    after.plays_remaining,
                    command.value,
                    "plays_remaining",
                ),
            )
            trace.append(f"nested:add-playable:+{command.value}")
            continue
        scalar = after.scalar
        if command.kind == "add_review":
            scalar = replace(scalar, review=_i32_add(scalar.review, command.value, "review"))
        elif command.kind == "add_block":
            scalar = replace(scalar, block=_i32_add(scalar.block, command.value, "block"))
        elif command.kind == "add_aggressive":
            if after.aggressive_additive_runtime is not None:
                raise Plan2NativeHorizonError("aggressive-additive-queued-raw-gain-unbound", command.kind)
            scalar = replace(
                scalar,
                card_play_aggressive=_i32_add(
                    scalar.card_play_aggressive,
                    command.value,
                    "card_play_aggressive",
                ),
            )
        after = replace(after, scalar=scalar)
        trace.append(f"nested:{command.kind}:+{command.value}")
    return after, tuple(trace)


def _advance_timers(
    state: Plan2NativeHorizonState,
    phase: str,
    catalog: Plan2NativeProgramCatalog,
    *,
    queue_limit: int,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    timers: list[Plan2NativeTimer] = []
    commands: list[Plan2NativeQueuedAction] = []
    trace: list[str] = []
    for timer in state.timers:
        if timer.phase != phase:
            timers.append(timer)
        elif timer.boundaries_until_fire == 1:
            commands.extend(timer.commands)
            trace.append(f"timer:{timer.timer_id}:fire:{phase}")
        else:
            timers.append(
                replace(timer, boundaries_until_fire=timer.boundaries_until_fire - 1)
            )
            trace.append(f"timer:{timer.timer_id}:tick:{phase}")
    after = replace(
        state,
        timers=tuple(timers),
        command_queue=(*state.command_queue, *commands),
    )
    after, command_trace = _drain_commands(after, catalog, queue_limit=queue_limit)
    return after, (*trace, *command_trace)


def _ordered_after_effect_chain_card_move(
    zones: NativeOrderedZoneState,
    command: object,
    *,
    hand_limit: int,
) -> tuple[NativeOrderedZoneState, tuple[str, ...]]:
    before = RemainingCardMoveState(
        hand=tuple(_plan3_card_from_ordered(value) for value in zones.hand),
        deck=tuple(_plan3_card_from_ordered(value) for value in zones.deck),
        grave=tuple(_plan3_card_from_ordered(value) for value in zones.grave),
        lost=tuple(_plan3_card_from_ordered(value) for value in zones.lost),
        random_state=zones.random_state,
        turn_used_support_ids=tuple(
            dict.fromkeys(
                support
                for value in zones.hand
                for support in value.support_upgrade_ids
            )
        ),
    )
    exact = execute_plan2_native_effect_chain_card_move(
        command,
        before,
        RemainingCardMoveExecutionContext(hand_limit=hand_limit),
    )
    ordered_by_guid = {value.guid: value for value in zones.card_universe}
    if {value.guid for value in exact.after.all_cards} != set(ordered_by_guid):
        raise Plan2NativeHorizonError(
            "effect-chain-card-move-universe-changed",
            getattr(command, "queue_instance_id", "unknown"),
        )

    def restore(values: Sequence[Plan3NativeCard]) -> tuple[NativeOrderedCardInstance, ...]:
        return tuple(ordered_by_guid[value.guid] for value in values)

    return (
        replace(
            zones,
            hand=restore(exact.after.hand),
            deck=restore(exact.after.deck),
            grave=restore(exact.after.grave),
            lost=restore(exact.after.lost),
            random_state=exact.after.random_state,
        ),
        exact.phases,
    )


def _execute_effect_chain_start_turn(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    status_child_context = _status_child_context_from_state(state, catalog)
    transition = start_plan2_native_effect_chain_turn(
        state.effect_chains,
        Plan2NativeEffectChainStartTurnInput(
            phase_snapshot_known=True,
            terminal_before_start=False,
        ),
    )
    if not transition.executable:
        raise Plan2NativeHorizonError(
            "effect-chain-start-turn-failed-closed",
            ",".join(transition.unresolved),
        )
    after = replace(state, effect_chains=transition.after)
    trace = list(transition.trace)
    for command in transition.commands:
        program = command.program
        if program.child_executor == EFFECT_CHAIN_CARD_MOVE:
            zones, child_trace = _ordered_after_effect_chain_card_move(
                after.zones,
                command,
                hand_limit=after.hand_limit,
            )
            after = replace(after, zones=zones)
            status_child_context.zones = zones
            trace.extend(child_trace)
            continue
        if program.child_executor == EFFECT_CHAIN_LESSON_MULTIPLE:
            next_uid = _next_horizon_status_uid(after)
            install_runtime = replace(
                after.review_dynamic,
                next_status_uid=max(
                    after.review_dynamic.next_status_uid,
                    next_uid,
                ),
            )
            dynamic_program = Plan2NativeReviewDynamicProgram(
                card_id=program.card_id,
                upgrade=program.upgrade,
                slot_index=0,
                ordered_effect_ids=(program.child_effect_id,),
                effect_id=program.child_effect_id,
                effect_type=program.child_effect_type,
                operation_kind=OP_INSTALL_LESSON_MULTIPLE,
                value1=program.child_value1,
                value2=program.child_value2,
                effect_count=program.child_count,
                effect_turn=program.child_turn,
            )
            installed = install_plan2_native_review_dynamic(
                install_runtime,
                dynamic_program,
                Plan2NativeReviewDynamicInstallInput(
                    source_guid=command.source_guid,
                    play_origin=command.source_play_origin,
                    completed_effect_ids=(),
                ),
            )
            if not installed.executable or not installed.installed:
                raise Plan2NativeHorizonError(
                    "effect-chain-lesson-multiple-failed-closed",
                    f"{command.queue_instance_id}:{','.join(installed.unresolved)}",
                )
            after = replace(
                after,
                scalar=replace(
                    after.scalar,
                    next_status_uid=max(
                        after.scalar.next_status_uid,
                        installed.after.next_status_uid,
                    ),
                ),
                review_dynamic=installed.after,
                triggered_review_status_runtime=replace(
                    after.triggered_review_status_runtime,
                    review_runtime=installed.after,
                ),
            )
            trace.extend(installed.trace)
            continue
        execution_state = prepare_plan2_native_effect_chain_start_turn_execution(
            Plan2NativeEffectChainExecutionState(
                zones=after.zones,
                hand_limit=after.hand_limit,
                master_card_refs=catalog.master_card_refs,
                block=after.scalar.block,
                review=after.scalar.review,
                application_status=after.scalar.parameter_application_status(
                    slump=False
                ),
            )
        )
        exact = execute_plan2_native_effect_chain_child(
            execution_state,
            command,
            Plan2NativeEffectChainChildInput(
                snapshot_known=True,
                add_block_status=AddBlockStatus(
                    aggressive=after.scalar.card_play_aggressive,
                ),
            ),
        )
        if not exact.executable:
            raise Plan2NativeHorizonError(
                "effect-chain-child-failed-closed",
                f"{command.queue_instance_id}:{','.join(exact.unresolved)}",
            )
        scalar = after.scalar
        status_child_context.zones = exact.after.zones or after.zones
        if exact.drawn_guids:
            # Timer children use the same native hand-arrival boundary as
            # direct draws. Even failed support upgrades consume native RNG.
            scalar, drawn_zones, draw_trace = _apply_generated_hand_arrivals(
                scalar, after.zones, status_child_context.zones,
                status_child_context, reason="draw",
            )
            status_child_context.zones = drawn_zones
            trace.extend(draw_trace)
        status_enchant = after.status_enchant
        review_dynamic = after.review_dynamic
        if program.child_executor in {
            EFFECT_CHAIN_LESSON_BLOCK,
            EFFECT_CHAIN_LESSON_REVIEW,
        }:
            if exact.ratio_value is None:
                raise Plan2NativeHorizonError(
                    "effect-chain-lesson-ratio-missing",
                    command.queue_instance_id,
                )
            lesson = handoff_plan2_native_review_dynamic_lesson(
                review_dynamic,
                Plan2NativeReviewDynamicLessonRequest(
                    value=exact.ratio_value,
                    count=1,
                    aggressive_at_execution=scalar.card_play_aggressive,
                    adding_status=AddingParameterStatus(
                        aggressive=scalar.card_play_aggressive,
                    ),
                    settings=AddingParameterSettings(),
                    application_status=scalar.parameter_application_status(
                        slump=False
                    ),
                    play_origin=command.source_play_origin,
                ),
            )
            if not lesson.executable:
                raise Plan2NativeHorizonError(
                    "effect-chain-lesson-failed-closed",
                    f"{command.queue_instance_id}:{','.join(lesson.unresolved)}",
                )
            scalar = carry_plan2_state_application_status(
                scalar,
                lesson.final_application_status,
            )
            trace.extend(lesson.trace)
        elif exact.after.block != scalar.block:
            difference = exact.after.block - scalar.block
            scalar = replace(scalar, block=exact.after.block)
            scalar, status_enchant, status_trace = (
                dispatch_plan2_native_status_enchant_event(
                    scalar,
                    status_enchant,
                    Plan2NativeStatusEnchantEvent(
                        phase=PHASE_STATUS_CHANGE,
                        play_origin=command.source_play_origin,
                        round_number=scalar.current_turn,
                        changed_effect_type=_STATUS_CHILD_BLOCK,
                        status_difference=difference,
                        status_change_committed=True,
                    ),
                    remaining_turns=after.remaining_turns,
                    review_dynamic=review_dynamic,
                    status_child_compilation=catalog.status_child_compilation,
                    status_child_context=status_child_context,
                )
            )
            trace.extend(status_trace)
            review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
        after = replace(
            after,
            scalar=scalar,
            zones=status_child_context.zones,
            status_enchant=status_enchant,
            review_dynamic=review_dynamic,
            status_child_inputs=tuple(status_child_context.inputs),
            status_child_review_state=(
                None
                if status_child_context.review_state is None
                else replace(
                    status_child_context.review_state,
                    plan2_state=scalar,
                )
            ),
            total_effect_draw_card_count=(
                status_child_context.total_effect_draw_card_count
            ),
            block_consumption_sum_count=(
                status_child_context.block_consumption_sum_count
            ),
            generated_hand_move_turn=status_child_context.hand_move_turn,
            turn_used_support_ids=status_child_context.turn_used_support_ids,
        )
        status_child_context.zones = after.zones
        trace.extend(exact.trace)
    return after, tuple(trace)


def _execute_end_turn(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    queue_limit: int = 64,
    apply_turn_end_recovery: bool = True,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    if state.terminal:
        raise Plan2NativeHorizonError("terminal-state-has-no-actions")
    if state.command_queue:
        raise Plan2NativeHorizonError("end-turn-with-pending-command-queue")
    if state.opaque_status_queue:
        raise Plan2NativeHorizonError(
            "opaque-status-queue-unresolved",
            ",".join(state.opaque_status_queue),
        )
    if type(apply_turn_end_recovery) is not bool:
        raise TypeError("apply_turn_end_recovery must be boolean")
    # ExamSetting recovery belongs to the explicit ExamTurnSkip/EndTurn
    # transaction.  Native applies it before any EndTurn listener, interval,
    # timer, or TurnCheck.Review work can observe the scalar.  The automatic
    # card-ended lifecycle passes ``False`` below and therefore never enters
    # this branch.
    recovery_trace: tuple[str, ...] = ()
    if apply_turn_end_recovery:
        recovery = state.turn_end_stamina_recovery
        if recovery:
            before_stamina = state.scalar.stamina
            recovered_stamina = min(
                state.scalar.max_stamina,
                before_stamina + recovery,
            )
            if recovered_stamina != before_stamina:
                state = replace(
                    state,
                    scalar=replace(
                        state.scalar,
                        stamina=recovered_stamina,
                    ),
                )
                recovery_trace = (
                    f"turn-end-stamina-recovery:{before_stamina}"
                    f"->{recovered_stamina}",
                )
    status_child_context = _status_child_context_from_state(state, catalog)
    scalar, status_enchant, status_trace = dispatch_plan2_native_status_enchant_event(
        state.scalar,
        state.status_enchant,
        Plan2NativeStatusEnchantEvent(
            phase=STATUS_PHASE_END_TURN,
            round_number=state.scalar.current_turn,
        ),
        remaining_turns=state.remaining_turns,
        review_dynamic=state.review_dynamic,
        status_child_compilation=catalog.status_child_compilation,
        status_child_context=status_child_context,
    )
    state = replace(
        state,
        scalar=scalar,
        status_enchant=status_enchant,
        zones=status_child_context.zones,
        status_child_inputs=tuple(status_child_context.inputs),
        status_child_review_state=(
            None
            if status_child_context.review_state is None
            else replace(status_child_context.review_state, plan2_state=scalar)
        ),
        total_effect_draw_card_count=(
            status_child_context.total_effect_draw_card_count
        ),
        block_consumption_sum_count=(
            status_child_context.block_consumption_sum_count
        ),
        generated_hand_move_turn=status_child_context.hand_move_turn,
        turn_used_support_ids=status_child_context.turn_used_support_ids,
    )
    item_field_search_values, item_field_search_trace = (
        _native_pitem_field_search_evidence(
            state.item_runtime,
            state.zones,
        )
    )
    state, item_end_trace = _execute_item_phase(
        state,
        catalog,
        Plan2NativeItemEvent(
            ITEM_PHASE_END_TURN,
            state.scalar.current_turn,
            review=state.scalar.review,
            aggressive=state.scalar.card_play_aggressive,
            remaining_turn=state.remaining_turns,
            phase_values=_native_pitem_end_turn_phase_values(
                state.item_runtime,
            ),
            field_values={_PITEM_FIELD_BLOCK: state.scalar.block},
            field_search_values=item_field_search_values,
        ),
    )
    item_end_trace = (*item_field_search_trace, *item_end_trace)
    remaining_three = tuple(
        listener
        for listener in state.scalar.end_turn_listeners
        if listener.trigger_id == REMAINING_THREE_TRIGGER_ID
    )
    if remaining_three and len(remaining_three) != len(state.scalar.end_turn_listeners):
        raise Plan2NativeHorizonError(
            "mixed-end-turn-listener-runtime-unbound",
            ",".join(listener.trigger_id for listener in state.scalar.end_turn_listeners),
        )
    if remaining_three:
        scalar = state.scalar
        trace = [
            *recovery_trace,
            *status_trace,
            *item_end_trace,
            "phase:ProduceExamPhaseType_ExamEndTurn",
        ]
        for listener in remaining_three:
            evaluated = evaluate_plan2_end_turn_remaining_three_before_decrement(
                scalar,
                state.remaining_turns,
            )
            trace.extend(evaluated.event_trace)
            if not evaluated.standalone.triggered or not listener.can_trigger:
                continue
            for effect_index, effect in enumerate(listener.effects):
                child = evaluate_plan2_end_turn_remaining_three_child(
                    scalar.review,
                    effect,
                )
                for raw_score in child.score_occurrences:
                    lesson = handoff_plan2_native_review_dynamic_lesson(
                        _sync_review_dynamic(
                            state.review_dynamic,
                            scalar.review,
                        ),
                        Plan2NativeReviewDynamicLessonRequest(
                            value=raw_score,
                            count=1,
                            aggressive_at_execution=(
                                scalar.card_play_aggressive
                            ),
                            adding_status=AddingParameterStatus(
                                aggressive=scalar.card_play_aggressive
                            ),
                            settings=AddingParameterSettings(),
                            application_status=(
                                scalar.parameter_application_status(
                                    slump=False
                                )
                            ),
                            play_origin="normal",
                        ),
                    )
                    if not lesson.executable:
                        raise Plan2NativeHorizonError(
                            "remaining-three-lesson-failed-closed",
                            ",".join(lesson.unresolved),
                        )
                    scalar = carry_plan2_state_application_status(
                        scalar,
                        lesson.final_application_status,
                    )
                trace.append(
                    f"remaining-three:{listener.status_uid}:effect:"
                    f"{effect_index}:{effect.effect_id}"
                )
        def apply_remaining_review(
            current: Plan2State,
            raw_score: int,
            _index: int,
        ) -> Plan2ScoreApplication:
            applied = apply_plan2_state_score(
                current,
                raw_score,
                score_kind=Plan2ScoreKind.REVIEW,
                integration_point=Plan2ScoreIntegrationPoint.REVIEW_DYNAMIC,
                slump=False,
            )
            return Plan2ScoreApplication(
                after=applied.after,
                applied_score=applied.actual_parameter,
            )

        review_score = simulate_end_turn_review_score(
            scalar,
            apply_score=apply_remaining_review,
        )
        scalar = review_score.after
        trace.extend(
            (
                "phase:ProduceExamPhaseType_ExamEndTurnInterval",
                "phase:ProduceExamPhaseType_ExamEndTurnTimer",
                "phase:ExamPlayCommandType_TurnCheck.Review",
                "next:ProduceExamPhaseType_ExamStartTurn",
            )
        )
        after = replace(state, scalar=scalar)
    else:
        def apply_end_turn_score(
            current: Plan2State,
            raw_score: int,
            source: str,
            _index: int,
        ) -> Plan2ScoreApplication:
            if source == TURN_CHECK_REVIEW_SOURCE:
                lesson = handoff_plan2_native_review_dynamic_lesson(
                    _sync_review_dynamic(state.review_dynamic, current.review),
                    Plan2NativeReviewDynamicLessonRequest(
                        value=raw_score,
                        count=1,
                        aggressive_at_execution=current.card_play_aggressive,
                        adding_status=AddingParameterStatus(
                            aggressive=current.card_play_aggressive
                        ),
                        settings=AddingParameterSettings(),
                        application_status=current.parameter_application_status(
                            slump=False
                        ),
                        play_origin="normal",
                    ),
                )
                if not lesson.executable:
                    raise Plan2NativeHorizonError(
                        "end-turn-lesson-failed-closed",
                        ",".join(lesson.unresolved),
                    )
                after_score = carry_plan2_state_application_status(
                    current,
                    lesson.final_application_status,
                )
                return Plan2ScoreApplication(
                    after=after_score,
                    applied_score=(
                        0
                        if not lesson.hits
                        else lesson.hits[-1].application.actual_parameter
                    ),
                )
            lesson = handoff_plan2_native_review_dynamic_lesson(
                _sync_review_dynamic(state.review_dynamic, current.review),
                Plan2NativeReviewDynamicLessonRequest(
                    value=raw_score,
                    count=1,
                    aggressive_at_execution=current.card_play_aggressive,
                    adding_status=AddingParameterStatus(
                        aggressive=current.card_play_aggressive
                    ),
                    settings=AddingParameterSettings(),
                    application_status=current.parameter_application_status(
                        slump=False
                    ),
                    play_origin="normal",
                ),
            )
            if not lesson.executable:
                raise Plan2NativeHorizonError(
                    "end-turn-lesson-failed-closed",
                    ",".join(lesson.unresolved),
                )
            after_score = carry_plan2_state_application_status(
                current,
                lesson.final_application_status,
            )
            return Plan2ScoreApplication(
                after=after_score,
                applied_score=(
                    0
                    if not lesson.hits
                    else lesson.hits[-1].application.actual_parameter
                ),
            )

        end_transition = simulate_plan2_exam_end_turn(
            state.scalar,
            apply_score=apply_end_turn_score,
        )
        if (state.aggressive_additive_runtime is not None
                and end_transition.after.card_play_aggressive > state.scalar.card_play_aggressive):
            raise Plan2NativeHorizonError("aggressive-additive-legacy-end-turn-gain-unbound", str(state.scalar.current_turn))
        after = replace(state, scalar=end_transition.after)
        trace = [
            *recovery_trace,
            *status_trace,
            *item_end_trace,
            *end_transition.event_trace,
        ]
    after, timer_trace = _advance_timers(
        after,
        "end_turn",
        catalog,
        queue_limit=queue_limit,
    )
    trace.extend(timer_trace)
    after, limit_trace = _finalize_plan2_lesson_limit(after)
    trace.extend(limit_trace)
    if after.terminal:
        return after, tuple(trace)

    dispositions: dict[str, NativeEndTurnDisposition] = {}
    for card in after.zones.hand:
        program = _program_for(catalog, card)
        dispositions[card.guid] = (
            NativeEndTurnDisposition.LOST
            if program.is_end_turn_lost
            else NativeEndTurnDisposition.GRAVE
        )
    zones = after.zones.close_turn(dispositions)
    status_child_context.zones = zones
    trace.append(f"reset-hand:{len(dispositions)}")
    extra_turn_ended = (
        advance_plan2_native_extra_turn_end_turn(after.extra_turn_runtime)
        if after.extra_turn_runtime is not None
        else None
    )
    if extra_turn_ended is not None:
        if not extra_turn_ended.executable:
            raise Plan2NativeHorizonError(
                "extra-turn-end-turn-failed-closed",
                extra_turn_ended.reason,
            )
        after = replace(after, extra_turn_runtime=extra_turn_ended.after)
        trace.extend(extra_turn_ended.operations)
    if after.scalar.current_turn >= after.limit_turn + after.extra_turn:
        if extra_turn_ended is not None and not extra_turn_ended.terminal:
            raise Plan2NativeHorizonError("extra-turn-terminal-boundary-mismatch")
        terminal = _finish_plan2_exam(
            # There is no next TurnStart on the native last-boundary path, so
            # preserve the just-completed turn's support-use ledger in the
            # terminal snapshot.
            replace(after, zones=zones),
            Plan2ExamTerminalReason.TURN_EXHAUSTED,
        )
        assert terminal.outcome is not None
        return (
            terminal,
            (
                *trace,
                "terminal:exam-end-complete",
                f"outcome:{terminal.outcome.value}",
            ),
        )

    # Native EndTurn starts a fresh per-turn support-use ledger only when the
    # lifecycle proceeds to NEXT_TURN.  The next TurnStart draw therefore
    # evaluates HandAdd with an empty used-ID tuple; later draws in that same
    # turn receive the updated tuple from the wire.
    after = replace(after, turn_used_support_ids=())
    status_child_context.turn_used_support_ids = ()

    started = simulate_plan2_turn_start(after.scalar)
    stamina_started = advance_plan2_stamina_turn_start(after.stamina_modifiers)
    play_count_started = advance_play_count_buff_turn_start(
        after.generated_runtime
    )
    search_stamina_started = advance_plan2_search_play_card_stamina_turn(
        after.search_play_card_stamina_runtime
    )
    review_started = start_plan2_native_review_dynamic_turn(
        _sync_review_dynamic(after.review_dynamic, started.after.review)
    )
    review_spent_at_turn_start = (
        review_started.before.review - review_started.after.review
    )
    review_consumption_sum = _i32_add(
        after.review_consumption_sum,
        review_spent_at_turn_start,
        "review_consumption_sum",
    )
    advanced_start_play_listeners: list[Plan2StartPlayListener] = []
    expired_start_play_uids: list[int] = []
    for listener in after.start_play_listeners:
        advanced_listener = advance_plan2_start_play_listener_turn(listener)
        if advanced_listener.after is None:
            expired_start_play_uids.append(listener.status_uid)
        else:
            advanced_start_play_listeners.append(advanced_listener.after)
    triggered_review_started = start_plan2_triggered_review_status_turn(
        replace(
            after.triggered_review_status_runtime,
            review_runtime=review_started.after,
        )
    )
    if not triggered_review_started.executable:
        raise Plan2NativeHorizonError(
            "triggered-review-start-turn-failed-closed",
            ",".join(triggered_review_started.unresolved),
        )
    debuff_started = advance_plan2_native_debuff_turn_start(
        after.debuff_registry
    )
    encore_started = (
        start_plan2_native_status_enchant_encore_turn(after.encore_runtime)
        if after.encore_runtime is not None
        else None
    )
    aggressive_additive_started = (
        advance_plan2_native_aggressive_additive_turn(
            after.aggressive_additive_runtime
        )
        if after.aggressive_additive_runtime is not None
        else None
    )
    scalar_after_review_start = replace(
        started.after,
        review=triggered_review_started.after.review_runtime.review,
    )
    block_fix_restriction_started = (
        advance_plan2_native_block_fix_restriction_turn_start(
            replace(
                after.block_fix_restriction_runtime,
                block=scalar_after_review_start.block,
                block_consumption_sum_count=after.block_consumption_sum_count,
            )
        )
        if after.block_fix_restriction_runtime is not None
        else None
    )
    extra_turn_started = (
        advance_plan2_native_extra_turn_start_turn(extra_turn_ended.after)
        if extra_turn_ended is not None
        else None
    )
    if extra_turn_started is not None and not extra_turn_started.executable:
        raise Plan2NativeHorizonError(
            "extra-turn-start-turn-failed-closed",
            extra_turn_started.reason,
        )
    zones = _reset_generated_hand_move_flags(zones)
    status_child_context.zones = zones
    status_child_context.hand_move_turn = start_plan2_generated_hand_move_turn(
        status_child_context.hand_move_turn,
        scalar_after_review_start.current_turn,
    )
    status_child_context.playing_guid = ""
    status_child_context.block_consumption_sum_count = (
        after.block_consumption_sum_count
    )
    started_scalar, started_status, status_start_trace = (
        dispatch_plan2_native_status_enchant_event(
            scalar_after_review_start,
            after.status_enchant,
            Plan2NativeStatusEnchantEvent(
                phase=STATUS_PHASE_START_TURN,
                round_number=scalar_after_review_start.current_turn,
                review=scalar_after_review_start.review,
                aggressive=scalar_after_review_start.card_play_aggressive,
                remaining_turns=max(after.remaining_turns - 1, 0),
                block=scalar_after_review_start.block,
                stamina=scalar_after_review_start.stamina,
                max_stamina=scalar_after_review_start.max_stamina,
            ),
            remaining_turns=max(after.remaining_turns - 1, 0),
            review_dynamic=triggered_review_started.after.review_runtime,
            status_child_compilation=catalog.status_child_compilation,
            status_child_context=status_child_context,
            default_status_child_input=Plan2NativeStatusChildInput(
                stamina_recover_restricted=(
                    after.stamina_recover_restricted
                ),
                stamina_recover_add_permil=(
                    after.stamina_recover_add_permil
                ),
                stamina_recover_add_permil_known=True,
                lesson_inputs_known=True,
                adding_status=AddingParameterStatus(
                    aggressive=(
                        scalar_after_review_start.card_play_aggressive
                    ),
                ),
                adding_settings=AddingParameterSettings(),
            ),
        )
    )
    after = replace(
        after,
        scalar=started_scalar,
        stamina_modifiers=stamina_started.after,
        generated_runtime=play_count_started.after,
        search_play_card_stamina_runtime=search_stamina_started.runtime_after,
        status_enchant=started_status,
        review_dynamic=_sync_review_dynamic(
            triggered_review_started.after.review_runtime,
            started_scalar.review,
        ),
        start_play_listeners=tuple(advanced_start_play_listeners),
        triggered_review_status_runtime=replace(
            triggered_review_started.after,
            review_runtime=_sync_review_dynamic(
                triggered_review_started.after.review_runtime,
                started_scalar.review,
            ),
        ),
        debuff_registry=debuff_started.after,
        encore_runtime=encore_started,
        aggressive_additive_runtime=(
            aggressive_additive_started.after
            if aggressive_additive_started is not None
            else None
        ),
        block_fix_restriction_runtime=(
            block_fix_restriction_started.after
            if block_fix_restriction_started is not None
            else None
        ),
        extra_turn_runtime=(
            extra_turn_started.after if extra_turn_started is not None else None
        ),
        zones=status_child_context.zones,
        status_child_inputs=tuple(status_child_context.inputs),
        status_child_review_state=(
            None
            if status_child_context.review_state is None
            else replace(
                status_child_context.review_state,
                plan2_state=started_scalar,
            )
        ),
        total_effect_draw_card_count=(
            status_child_context.total_effect_draw_card_count
        ),
        block_consumption_sum_count=(
            status_child_context.block_consumption_sum_count
        ),
        generated_hand_move_turn=status_child_context.hand_move_turn,
        turn_used_support_ids=status_child_context.turn_used_support_ids,
        review_consumption_sum=review_consumption_sum,
        plays_remaining=1,
    )
    after, turn_timer_trace = dispatch_plan2_native_turn_timer(
        after,
        catalog,
    )
    after, gimmick_trace = _execute_plan2_scheduled_gimmicks(
        after,
        catalog,
    )
    trace.extend(
        (
            f"turn-start:{started.after.current_turn}",
            "status-expiry:" + ",".join(str(uid) for uid in started.expired_status_uids),
            "review-dynamic-expiry:"
            + ",".join(str(uid) for uid in review_started.expired_status_uids),
            "triggered-review-activation:"
            + ",".join(
                str(value.status_uid)
                for value in triggered_review_started.activations
            ),
            "start-play-status-expiry:"
            + ",".join(str(uid) for uid in expired_start_play_uids),
            "debuff-permanent:"
            + ",".join(str(uid) for uid in debuff_started.permanent_status_uids),
            "stamina-status-expiry:"
            + ",".join(str(uid) for uid in stamina_started.expired_status_uids),
            "play-count-buff-fresh:"
            + ",".join(
                str(uid) for uid in play_count_started.fresh_status_uids
            ),
            "play-count-buff-spent:"
            + ",".join(
                str(uid) for uid in play_count_started.spent_status_uids
            ),
            "play-count-buff-expiry:"
            + ",".join(
                str(uid) for uid in play_count_started.expired_status_uids
            ),
            "aggressive-additive-expiry:"
            + ",".join(
                str(uid)
                for uid in (
                    aggressive_additive_started.expired_status_uids
                    if aggressive_additive_started is not None
                    else ()
                )
            ),
            "block-restriction-expiry:"
            + ",".join(
                str(uid)
                for uid in (
                    block_fix_restriction_started.expired_status_uids
                    if block_fix_restriction_started is not None
                    else ()
                )
            ),
            *(
                extra_turn_started.operations
                if extra_turn_started is not None
                else ()
            ),
            *status_start_trace,
            *turn_timer_trace,
            *gimmick_trace,
        )
    )
    after, limit_trace = _finalize_plan2_lesson_limit(after)
    trace.extend(limit_trace)
    if after.terminal:
        return after, tuple(trace)
    try:
        item_runtime_at_turn_start = advance_plan2_native_item_turn_start(
            after.item_runtime
        )
    except (TypeError, ValueError) as error:
        raise Plan2NativeHorizonError(
            "item-turn-start-lifecycle-failed-closed",
            f"{type(error).__name__}:{error}",
        ) from error
    after = replace(after, item_runtime=item_runtime_at_turn_start)
    item_parameter_value = _item_turn_start_parameter_value(after)
    item_lesson_type = (
        "ProduceStepLessonType_LessonVocal"
        if item_parameter_value in {1, 2, 3}
        else "ProduceStepLessonType_LessonDance"
        if item_parameter_value in {4, 5, 6}
        else "ProduceStepLessonType_LessonVisual"
        if item_parameter_value in {7, 8, 9}
        else "ProduceStepLessonType_Unknown"
    )
    after, item_start_trace = _execute_item_phase(
        after,
        catalog,
        Plan2NativeItemEvent(
            ITEM_PHASE_TURN_START,
            after.scalar.current_turn,
            review=after.scalar.review,
            remaining_turn=after.remaining_turns,
            block=after.scalar.block,
            field_values={
                "ProduceExamFieldStatusType_BlockUp": after.scalar.block,
            },
            lesson_step_type_value=item_parameter_value,
            lesson_type=item_lesson_type,
            lesson_is_clear=(
                after.exam_mode.is_lesson
                and after.clear_border >= 0
                and after.scalar.score >= after.clear_border
            ),
            is_lesson=after.exam_mode.is_lesson,
        ),
    )
    trace.extend(item_start_trace)
    after, limit_trace = _finalize_plan2_lesson_limit(after)
    trace.extend(limit_trace)
    if after.terminal:
        return after, tuple(trace)
    after, draw_trace = _replace_with_draw(after, after.draw_count, catalog)
    trace.extend(draw_trace)
    start_play_capture = capture_plan2_start_play(after.start_play_listeners)
    after = replace(
        after,
        start_play_listeners=start_play_capture.after_count_spend,
    )
    trace.extend(start_play_capture.event_trace)
    for child in start_play_capture.child_effects_in_order:
        if (
            child.effect_type != START_PLAY_CARD_DRAW_EFFECT_TYPE
            or child.value1 < 1
            or child.value2 != 0
            or child.effect_count != 0
            or child.effect_turn != 0
        ):
            raise Plan2NativeHorizonError(
                "start-play-child-runtime-unbound",
                child.id,
            )
        after, child_draw_trace = _replace_with_draw(
            after,
            child.value1,
            catalog,
            effect_draw=True,
        )
        trace.append(f"start-play-child:{child.id}")
        trace.extend(child_draw_trace)
    after, effect_chain_trace = _execute_effect_chain_start_turn(after, catalog)
    trace.extend(effect_chain_trace)
    after, timer_trace = _advance_timers(
        after,
        "start_turn",
        catalog,
        queue_limit=queue_limit,
    )
    trace.extend(timer_trace)
    after, item_interval_trace = _execute_item_phase(
        after,
        catalog,
        Plan2NativeItemEvent(
            ITEM_PHASE_TURN_INTERVAL,
            after.scalar.current_turn,
            review=after.scalar.review,
        ),
    )
    trace.extend(item_interval_trace)
    after, limit_trace = _finalize_plan2_lesson_limit(after)
    trace.extend(limit_trace)
    if not after.terminal and after.aggressive_additive_runtime is not None:
        # Native MainStart marks every status, including any just installed
        # by StartTurn/timer/item effects after the boundary's SpendTurn pass.
        runtime = after.aggressive_additive_runtime
        after = replace(after, aggressive_additive_runtime=replace(runtime,
            additive_layers=tuple(replace(layer, passing_turn_start=True) for layer in runtime.additive_layers),
            additive_fix_layers=tuple(replace(layer, passing_turn_start=True) for layer in runtime.additive_fix_layers)))
    return after, tuple(trace)


def _drink_child_command(
    effect: Plan2NativeDrinkEffect,
    instance: Plan2NativeDrinkInstance,
) -> Plan2StatusEnchantChildCommand:
    child = Plan2StatusEnchantChildProgram(
        effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        count=effect.count,
        turn=effect.turn,
        status_enchant_id="",
        chain_effect_id="",
        chain_effect_ids=(),
        target_card_id="",
        target_upgrade=0,
        target_effect_type="",
        card_search_id="",
        move_position_type="",
        card_search_id2="",
        grow_effect_ids=(),
        effect_group_ids=(),
    )
    return Plan2StatusEnchantChildCommand(
        event_sequence=effect.effect_index,
        listener_sequence=0,
        child_sequence=effect.effect_index,
        status_uid=0,
        source_guid=instance.instance_id,
        source_card_id="",
        source_upgrade=0,
        play_origin="normal",
        installer_effect_id=effect.drink_effect_id,
        status_enchant_id="",
        trigger_id="",
        phase=PHASE_MAIN,
        child=child,
    )


def _next_horizon_status_uid(state: Plan2NativeHorizonState) -> int:
    """Return the shared native UID allocator floor for non-card sources."""

    uids = [
        *(value.status_uid for value in state.scalar.review_multiple_layers),
        *(value.status_uid for value in state.scalar.end_turn_listeners),
        *(value.status_uid for value in state.scalar.card_play_listeners),
        *((state.scalar.stamina_consumption_add_status.status_uid,)
          if state.scalar.stamina_consumption_add_status is not None else ()),
        *(value.status_uid for value in state.stamina_modifiers.down_fix_layers),
        *((state.stamina_modifiers.down.status_uid,)
          if state.stamina_modifiers.down is not None else ()),
        *((state.stamina_modifiers.add.layer.status_uid,)
          if state.stamina_modifiers.add.layer is not None else ()),
        *(value.status_uid for value in state.status_enchant.listeners),
        *(value.status_uid for value in state.review_dynamic.layers),
        *(value.uid for value in state.debuff_registry.statuses),
        *(value.status_uid for value in state.effect_chains.queue),
        *(value.native_uid for value in state.generated_runtime.statuses),
        *(value.status_uid for value in state.start_play_listeners),
        *(value.status_uid for value in state.triggered_review_status_runtime.listeners),
        *state.item_runtime.active_status_uids,
    ]
    if state.status_child_review_state is not None:
        uids.extend(
            value.status_uid for value in state.status_child_review_state.active
        )
        uids.extend(
            value.status_uid
            for value in state.status_child_review_state.review_count_add_layers
        )
    if state.encore_runtime is not None:
        uids.extend(value.status_uid for value in state.encore_runtime.listeners)
    if state.aggressive_additive_runtime is not None:
        uids.extend(state.aggressive_additive_runtime.status_uids)
    if state.block_fix_restriction_runtime is not None:
        uids.extend(state.block_fix_restriction_runtime.active_status_uids)
    uids.extend(
        value.status_uid for value in state.search_play_card_stamina_runtime.statuses
    )
    # Native UID allocation is monotonic across status runtimes. Expiry removes
    # an active listener/layer but deliberately keeps that runtime's cursor;
    # looking only at active UIDs would then reuse an already-consumed value.
    # Keep one shared floor from every typed runtime cursor as well as the
    # active inventory above.
    cursor_owners = (
        state.scalar,
        state.stamina_modifiers,
        state.status_enchant,
        state.review_dynamic,
        state.debuff_registry,
        state.effect_chains,
        state.generated_runtime,
        state.item_runtime,
        state.triggered_review_status_runtime,
        state.search_play_card_stamina_runtime,
        state.status_child_review_state,
        state.encore_runtime,
        state.aggressive_additive_runtime,
        state.block_fix_restriction_runtime,
    )
    cursors = tuple(
        value
        for owner in cursor_owners
        if owner is not None
        for value in (
            getattr(owner, "next_status_uid", None),
            getattr(owner, "next_uid", None),
        )
        if isinstance(value, int) and not isinstance(value, bool)
    )
    return max((1, *cursors, *(value + 1 for value in uids)))


def _execute_hand_grave_draw_drink(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Reuse the ordered-zone draw owner after the native Hand→Grave move."""

    snapshot = state.zones.hand
    discarded = tuple(card.reset_support_upgrade() for card in snapshot)
    updates = {card.guid: card for card in discarded}
    zones = replace(
        state.zones,
        card_universe=tuple(
            updates.get(card.guid, card) for card in state.zones.card_universe
        ),
        hand=(),
        grave=(*state.zones.grave, *discarded),
    )
    moved = replace(state, zones=zones)
    after, draw_trace = _replace_with_draw(
        moved,
        len(snapshot),
        catalog,
        effect_draw=True,
    )
    return after, (
        "drink-hand-grave-discard:"
        + ",".join(card.guid for card in snapshot),
        *draw_trace,
    )


def _execute_idol_unique_card_move_drink(
    state: Plan2NativeHorizonState,
    effect: Plan2NativeDrinkEffect,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Move the first exact tagged Deck/Grave card to Hand.

    Native ``OrderType_Unknown`` preserves the Deck→Grave collection order;
    the search's ``LimitCount`` is applied before the effect's ``All`` pick.
    Hand overflow follows the already-proved CardMove rule and is inserted at
    DeckFirst.  A successful Hand arrival then enters the shared HandAdd
    callback, including support upgrade RNG and generated hand-move effects.
    """

    program = effect.card_move_program
    if not isinstance(program, Plan2NativeDrinkCardMoveProgram):
        raise Plan2NativeHorizonError(
            "drink-card-move-program-missing", effect.effect_id
        )
    refs = set(program.master_refs)
    candidates = tuple(
        card
        for zone in (state.zones.deck, state.zones.grave)
        for card in zone
        if (card.card_id, card.effective_upgrade) in refs
    )
    moved = candidates[: program.limit_count]
    if not moved:
        return state, (
            f"drink-card-move-search:{program.search_id}:none",
            "drink-card-move-hand:none",
        )
    selected_guids = {card.guid for card in moved}
    remaining_deck = tuple(
        card for card in state.zones.deck if card.guid not in selected_guids
    )
    remaining_grave = tuple(
        card for card in state.zones.grave if card.guid not in selected_guids
    )
    capacity = max(state.hand_limit - len(state.zones.hand), 0)
    accepted = moved[:capacity]
    overflow = moved[capacity:]
    raw_zones = replace(
        state.zones,
        hand=(*state.zones.hand, *accepted),
        deck=(*overflow, *remaining_deck),
        grave=remaining_grave,
    )
    context = _status_child_context_from_state(state, catalog)
    scalar, zones, hand_move_trace = _apply_generated_hand_arrivals(
        state.scalar,
        state.zones,
        raw_zones,
        context,
        reason="card-move",
    )
    after = replace(
        state,
        scalar=scalar,
        zones=zones,
        block_consumption_sum_count=context.block_consumption_sum_count,
        generated_hand_move_turn=context.hand_move_turn,
        turn_used_support_ids=context.turn_used_support_ids,
    )
    return after, (
        f"drink-card-move-search:{program.search_id}:"
        + ",".join(card.guid for card in candidates),
        "drink-card-move-hand:" + ",".join(card.guid for card in accepted),
        "drink-card-move-overflow-deck-first:"
        + ",".join(card.guid for card in overflow),
        *hand_move_trace,
    )


def _execute_card_create_search_drink(
    state: Plan2NativeHorizonState,
    effect: Plan2NativeDrinkEffect,
    instance_id: str,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Execute the exact random-pool create pipeline with caller GUIDs."""

    program = effect.card_create_program
    if not isinstance(program, CardCreateSearchEffectContract):
        raise Plan2NativeHorizonError(
            "drink-card-create-program-missing",
            effect.effect_id,
        )
    supplied = next(
        (
            value
            for value in state.generated_inputs
            if value.source_guid == instance_id
        ),
        None,
    )
    if supplied is None:
        raise Plan2NativeHorizonError(
            "drink-card-create-input-missing",
            instance_id,
        )
    execution = apply_card_create_search(
        _plan3_state_from_ordered(state.zones),
        program,
        execution_input=CardCreateSearchChanceInput(
            plan_type="ProducePlanType_Plan2",
            plan_ignore_card_ids=supplied.plan_ignore_card_ids,
            hand_limit=state.hand_limit,
            guid_tokens=(() if supplied.guid_tokens is None else supplied.guid_tokens),
        ),
    )
    if not execution.resolved:
        detail = ",".join(
            f"{value.field}:{value.reason}"
            for value in execution.trace.unresolved_inputs
        )
        raise Plan2NativeHorizonError(
            "drink-card-create-input-unresolved",
            f"{effect.effect_id}:{detail}",
        )
    raw_zones = _ordered_after_status_native(state.zones, execution.after)
    context = _status_child_context_from_state(state, catalog)
    scalar, zones, hand_move_trace = _apply_generated_hand_arrivals(
        state.scalar,
        state.zones,
        raw_zones,
        context,
        reason="card-create-search",
    )
    after = replace(
        state,
        scalar=scalar,
        zones=zones,
        generated_inputs=tuple(
            value
            for value in state.generated_inputs
            if value.source_guid != instance_id
        ),
        block_consumption_sum_count=context.block_consumption_sum_count,
        generated_hand_move_turn=context.hand_move_turn,
        turn_used_support_ids=context.turn_used_support_ids,
    )
    return after, (
        f"drink-card-create-search:{effect.effect_id}:"
        + ",".join(execution.trace.selected_card_ids),
        "drink-card-create-guids:" + ",".join(execution.trace.guid_tokens),
        "drink-card-create-rng:"
        f"{execution.trace.random_state_before}->{execution.trace.random_state_after}:"
        f"calls={execution.trace.rng_call_count}",
        *hand_move_trace,
    )


def _execute_force_play_random_pool_drink(
    state: Plan2NativeHorizonState,
    effect: Plan2NativeDrinkEffect,
    instance_id: str,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Select, force-play, and discard one detached RandomPool card."""

    program = effect.force_play_random_pool_program
    if not isinstance(program, Plan2DrinkForcePlayRandomPoolProgram):
        raise Plan2NativeHorizonError(
            "drink-force-play-random-pool-program-missing",
            effect.effect_id,
        )
    supplied = next(
        (
            value
            for value in state.generated_inputs
            if value.source_guid == instance_id
        ),
        None,
    )
    if supplied is None:
        raise Plan2NativeHorizonError(
            "drink-force-play-random-pool-input-missing",
            instance_id,
        )
    if supplied.guid_tokens is None or len(supplied.guid_tokens) != 1:
        raise Plan2NativeHorizonError(
            "drink-force-play-random-pool-guid-count",
            (
                "unresolved"
                if supplied.guid_tokens is None
                else str(len(supplied.guid_tokens))
            ),
        )
    if supplied.plan_ignore_card_ids is None:
        raise Plan2NativeHorizonError(
            "drink-force-play-random-pool-whitelist-missing",
            instance_id,
        )
    try:
        selection = select_plan2_drink_force_play_random_pool_card(
            _plan3_state_from_ordered(state.zones),
            program,
            guid_token=supplied.guid_tokens[0],
            plan_ignore_card_ids=supplied.plan_ignore_card_ids,
            hand_limit=state.hand_limit,
        )
    except (Plan2DrinkForcePlayRandomPoolError, TypeError, ValueError) as error:
        raise Plan2NativeHorizonError(
            "drink-force-play-random-pool-selection-failed",
            f"{type(error).__name__}:{error}",
        ) from error

    selected = selection.card
    random_zones = replace(
        state.zones,
        random_state=selection.random_state_after,
    )
    prepared = replace(
        state,
        zones=random_zones,
        generated_inputs=tuple(
            value
            for value in state.generated_inputs
            if value.source_guid != instance_id
        ),
    )
    played, play_trace = _execute_play(
        prepared,
        selected.guid,
        catalog,
        pay_cost=False,
        spend_play=False,
        forced=True,
        detached_card=_ordered_card_from_plan3(selected, None),
    )
    return played, (
        f"drink-force-play-random-pool:{effect.effect_id}:"
        f"{selected.card_id}@{selected.effective_upgrade}:"
        f"ticket={selection.selected_ticket_index}",
        f"drink-force-play-random-pool-guid:{selected.guid}",
        "drink-force-play-random-pool-rng:"
        f"{selection.random_state_before}->{selection.random_state_after}:"
        f"calls={selection.rng_call_count}",
        *play_trace,
    )


def _execute_drink(
    state: Plan2NativeHorizonState,
    action: Plan2NativeDrinkAction,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, tuple[str, ...]]:
    """Consume one exact slot and execute every Master effect in list order."""

    if state.terminal:
        raise Plan2NativeHorizonError("terminal-state-has-no-actions")
    if state.phase != PHASE_MAIN:
        raise Plan2NativeHorizonError("drink-action-phase-unbound", state.phase)
    if state.opaque_status_queue:
        raise Plan2NativeHorizonError(
            "opaque-status-queue-unresolved",
            ",".join(state.opaque_status_queue),
        )
    if action.selected_card_guid:
        raise Plan2NativeHorizonError(
            "drink-selection-unexpected", action.selected_card_guid
        )
    instance = state.drink_runtime.resolve(
        action.slot_index,
        instance_id=action.instance_id,
        drink_id=action.drink_id,
    )
    if state.block_fix_restriction_runtime is not None and any(
        value.handler == DRINK_HANDLER_BLOCK for value in instance.effects
    ):
        raise Plan2NativeHorizonError(
            "drink-block-restriction-interaction-unbound", instance.drink_id
        )

    scalar = state.scalar
    stamina_modifiers = state.stamina_modifiers
    status_enchant = state.status_enchant
    review_dynamic = _sync_review_dynamic(state.review_dynamic, scalar.review)
    debuff_registry = state.debuff_registry
    generated_runtime = state.generated_runtime
    start_play_listeners = state.start_play_listeners
    generated_inputs = state.generated_inputs
    status_child_context = _status_child_context_from_state(state, catalog)
    plays_remaining = state.plays_remaining
    completed_effect_ids: list[str] = []
    trace: list[str] = [
        f"drink-use:{action.slot_index}:{instance.instance_id}:{instance.drink_id}",
    ]

    def working_state() -> Plan2NativeHorizonState:
        return replace(
            state,
            scalar=scalar,
            aggressive_additive_runtime=_context_aggressive_runtime(status_child_context, scalar, state.aggressive_additive_runtime),
            stamina_modifiers=stamina_modifiers,
            zones=status_child_context.zones,
            status_enchant=status_enchant,
            review_dynamic=review_dynamic,
            debuff_registry=debuff_registry,
            generated_runtime=generated_runtime,
            start_play_listeners=start_play_listeners,
            generated_inputs=generated_inputs,
            status_child_inputs=tuple(status_child_context.inputs),
            status_child_review_state=(
                None
                if status_child_context.review_state is None
                else replace(
                    status_child_context.review_state,
                    plan2_state=scalar,
                )
            ),
            total_effect_draw_card_count=(
                status_child_context.total_effect_draw_card_count
            ),
            block_consumption_sum_count=(
                status_child_context.block_consumption_sum_count
            ),
            generated_hand_move_turn=status_child_context.hand_move_turn,
            turn_used_support_ids=status_child_context.turn_used_support_ids,
            plays_remaining=plays_remaining,
        )

    for effect in instance.effects:
        scalar_before = scalar
        if effect.handler == DRINK_HANDLER_PLAYABLE_ADD:
            plays_remaining = _i32_add(
                plays_remaining,
                effect.count,
                "plays_remaining",
            )
            changed_type = ""
            difference = 0
        elif effect.handler == DRINK_HANDLER_CARD_UPGRADE_HAND_ALL:
            master_refs = tuple(program.ref for program in catalog.programs)
            upgraded_zones, upgraded_guids = upgrade_plan2_native_hand_all_zones(
                status_child_context.zones,
                master_refs,
            )
            status_child_context = replace(
                status_child_context,
                zones=upgraded_zones,
            )
            changed_type = ""
            difference = 0
            trace.append(
                "drink-card-upgrade-hand-all:"
                + ",".join(upgraded_guids)
            )
        elif effect.handler == DRINK_HANDLER_HAND_GRAVE_DRAW:
            drawn, draw_trace = _execute_hand_grave_draw_drink(
                working_state(),
                catalog,
            )
            scalar = drawn.scalar
            stamina_modifiers = drawn.stamina_modifiers
            status_enchant = drawn.status_enchant
            review_dynamic = drawn.review_dynamic
            debuff_registry = drawn.debuff_registry
            start_play_listeners = drawn.start_play_listeners
            status_child_context = _status_child_context_from_state(
                drawn,
                catalog,
            )
            changed_type = ""
            difference = 0
            trace.extend(draw_trace)
        elif effect.handler == DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND:
            moved, move_trace = _execute_idol_unique_card_move_drink(
                working_state(),
                effect,
                catalog,
            )
            scalar = moved.scalar
            stamina_modifiers = moved.stamina_modifiers
            status_enchant = moved.status_enchant
            review_dynamic = moved.review_dynamic
            debuff_registry = moved.debuff_registry
            start_play_listeners = moved.start_play_listeners
            status_child_context = _status_child_context_from_state(
                moved,
                catalog,
            )
            changed_type = ""
            difference = 0
            trace.extend(move_trace)
        elif effect.handler == DRINK_HANDLER_CARD_CREATE_SEARCH:
            created, create_trace = _execute_card_create_search_drink(
                working_state(),
                effect,
                instance.instance_id,
                catalog,
            )
            scalar = created.scalar
            stamina_modifiers = created.stamina_modifiers
            status_enchant = created.status_enchant
            review_dynamic = created.review_dynamic
            debuff_registry = created.debuff_registry
            start_play_listeners = created.start_play_listeners
            generated_inputs = created.generated_inputs
            status_child_context = _status_child_context_from_state(
                created,
                catalog,
            )
            changed_type = ""
            difference = 0
            trace.extend(create_trace)
        elif effect.handler == DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL:
            forced_played, force_trace = (
                _execute_force_play_random_pool_drink(
                    working_state(),
                    effect,
                    instance.instance_id,
                    catalog,
                )
            )
            # A forced card can touch every native horizon runtime.  Make its
            # complete result the new base, then refresh the loop-owned
            # mirrors used by any later Master effect in the same drink.
            state = forced_played
            scalar = forced_played.scalar
            stamina_modifiers = forced_played.stamina_modifiers
            status_enchant = forced_played.status_enchant
            review_dynamic = forced_played.review_dynamic
            debuff_registry = forced_played.debuff_registry
            generated_runtime = forced_played.generated_runtime
            start_play_listeners = forced_played.start_play_listeners
            generated_inputs = forced_played.generated_inputs
            plays_remaining = forced_played.plays_remaining
            status_child_context = _status_child_context_from_state(
                forced_played,
                catalog,
            )
            changed_type = ""
            difference = 0
            trace.extend(force_trace)
        elif effect.handler == DRINK_HANDLER_CARD_DRAW:
            drawn, draw_trace = _replace_with_draw(
                working_state(),
                effect.value1,
                catalog,
                effect_draw=True,
            )
            scalar = drawn.scalar
            stamina_modifiers = drawn.stamina_modifiers
            status_enchant = drawn.status_enchant
            review_dynamic = drawn.review_dynamic
            debuff_registry = drawn.debuff_registry
            start_play_listeners = drawn.start_play_listeners
            status_child_context = _status_child_context_from_state(
                drawn,
                catalog,
            )
            changed_type = ""
            difference = 0
            trace.extend(draw_trace)
        elif effect.handler == DRINK_HANDLER_LESSON_VALUE_MULTIPLE:
            assert effect.review_dynamic_program is not None
            next_uid = _next_horizon_status_uid(working_state())
            install_runtime = replace(
                review_dynamic,
                next_status_uid=max(review_dynamic.next_status_uid, next_uid),
            )
            installed = install_plan2_native_review_dynamic(
                install_runtime,
                effect.review_dynamic_program,
                Plan2NativeReviewDynamicInstallInput(
                    source_guid=instance.instance_id,
                    completed_effect_ids=tuple(completed_effect_ids),
                ),
            )
            if not installed.executable or not installed.installed:
                raise Plan2NativeHorizonError(
                    "drink-lesson-multiple-install-failed-closed",
                    f"{effect.effect_id}:{','.join(installed.unresolved)}",
                )
            review_dynamic = installed.after
            changed_type = ""
            difference = 0
            trace.extend(installed.trace)
        elif effect.handler == DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN:
            assert effect.stamina_down_effect is not None
            next_uid = _next_horizon_status_uid(working_state())
            modifier_runtime = replace(
                stamina_modifiers,
                next_status_uid=max(
                    stamina_modifiers.next_status_uid,
                    next_uid,
                ),
            )
            installed = apply_plan2_stamina_modifier(
                modifier_runtime,
                effect.stamina_down_effect,
            )
            if not installed.executable:
                raise Plan2NativeHorizonError(
                    "drink-stamina-down-install-failed-closed",
                    effect.effect_id,
                )
            stamina_modifiers = installed.after
            changed_type = ""
            difference = 0
            trace.append(
                f"drink-stamina-down:{effect.effect_id}:"
                + ("installed" if installed.installed else "merged")
            )
        elif effect.handler == DRINK_HANDLER_STAMINA_CONSUMPTION_ADD:
            assert effect.stamina_add_effect is not None
            gate = gate_plan2_native_status_addition(
                debuff_registry,
                effect.effect_type,
            )
            if not gate.resolved:
                raise Plan2NativeHorizonError(
                    "drink-stamina-add-gate-failed-closed",
                    f"{effect.effect_id}:{gate.reason or ''}",
                )
            debuff_registry = gate.registry_after
            if gate.blocked:
                trace.extend(gate.queue_handoff.trace)
                trace.append(
                    f"drink-stamina-add:{effect.effect_id}:blocked"
                )
            else:
                next_uid = _next_horizon_status_uid(working_state())
                modifier_runtime = replace(
                    stamina_modifiers,
                    next_status_uid=max(
                        stamina_modifiers.next_status_uid,
                        next_uid,
                    ),
                )
                installed = apply_plan2_stamina_modifier(
                    modifier_runtime,
                    effect.stamina_add_effect,
                )
                if not installed.executable:
                    raise Plan2NativeHorizonError(
                        "drink-stamina-add-install-failed-closed",
                        effect.effect_id,
                    )
                stamina_modifiers = installed.after
                trace.append(
                    f"drink-stamina-add:{effect.effect_id}:"
                    + ("installed" if installed.installed else "merged")
                )
            changed_type = ""
            difference = 0
        elif effect.handler == DRINK_HANDLER_PLAY_COUNT_BUFF:
            program = effect.play_count_buff_program
            assert isinstance(program, Plan2NativeDrinkPlayCountBuffProgram)
            next_uid = _next_horizon_status_uid(working_state())
            generated_runtime = PlayCountBuffRuntime(
                (
                    *generated_runtime.statuses,
                    PlayCountBuffStatus(
                        limit=program.value1,
                        remaining_count=program.effect_count,
                        search_id=program.search_id,
                        turn=program.effect_turn,
                        native_uid=next_uid,
                        is_passing_turn_start=False,
                    ),
                )
            )
            changed_type = ""
            difference = 0
            trace.append(
                f"drink-play-count-buff:{next_uid}:{program.search_id}:"
                f"count={program.effect_count}:turn={program.effect_turn}"
            )
        elif effect.handler == DRINK_HANDLER_STAMINA_REDUCE_FIX:
            scalar = replace(
                scalar,
                stamina=max(0, scalar.stamina - effect.value1),
            )
            changed_type = ""
            difference = 0
        elif effect.handler == DRINK_HANDLER_STAMINA_RECOVER_FIX:
            recovered = apply_plan2_stamina_recover_fix(
                scalar,
                StaminaRecoverFixContract(
                    effect_id=effect.effect_id,
                    effect_value1=effect.value1,
                    effect_value2=effect.value2,
                    effect_count=effect.count,
                    effect_turn=effect.turn,
                ),
                stamina_recover_restricted=state.stamina_recover_restricted,
                stamina_recover_add_permil=state.stamina_recover_add_permil,
            )
            if not recovered.standalone.executable:
                raise Plan2NativeHorizonError(
                    "drink-stamina-recovery-failed-closed",
                    f"{effect.effect_id}:{recovered.standalone.reason or ''}",
                )
            scalar = recovered.after
            changed_type = ""
            difference = 0
            trace.extend(recovered.event_trace)
        elif effect.handler == DRINK_HANDLER_END_TURN_STATUS:
            assert effect.end_turn_program is not None
            next_uid = _next_horizon_status_uid(working_state())
            if scalar.next_status_uid != next_uid:
                scalar = replace(scalar, next_status_uid=next_uid)
            installed = simulate_install_plan2_end_turn_listener(
                scalar,
                effect.end_turn_program,
            )
            scalar = installed.after
            changed_type = ""
            difference = 0
            trace.append(
                "drink-end-turn-listener:"
                f"{installed.created_status_uid}:{effect.effect_id}"
            )
        elif effect.handler == DRINK_HANDLER_START_PLAY_STATUS:
            assert effect.start_play_program is not None
            next_uid = _next_horizon_status_uid(working_state())
            listener = simulate_install_plan2_start_play_listener(
                next_uid,
                effect.start_play_program,
            )
            start_play_listeners = (*start_play_listeners, listener)
            changed_type = ""
            difference = 0
            trace.append(
                f"drink-start-play-listener:{next_uid}:{effect.effect_id}"
            )
        else:
            scalar, changed_type, difference = _apply_status_enchant_child(
                scalar,
                _drink_child_command(effect, instance),
                review_dynamic=review_dynamic,
                play_origin="normal",
                status_child_context=status_child_context,
            )
        trace.append(
            f"drink-effect:{effect.effect_index}:{effect.drink_effect_id}:"
            f"{effect.effect_id}:{effect.handler}"
        )
        if scalar.score != scalar_before.score:
            trace.append(
                f"drink-score:{scalar_before.score}->{scalar.score}"
            )
        if scalar.stamina != scalar_before.stamina:
            trace.append(
                f"drink-stamina:{scalar_before.stamina}->{scalar.stamina}"
            )
        review_dynamic = _sync_review_dynamic(review_dynamic, scalar.review)
        if changed_type and difference:
            scalar, status_enchant, status_trace = (
                dispatch_plan2_native_status_enchant_event(
                    scalar,
                    status_enchant,
                    Plan2NativeStatusEnchantEvent(
                        phase=PHASE_STATUS_CHANGE,
                        play_origin="normal",
                        round_number=scalar.current_turn,
                        changed_effect_type=changed_type,
                        status_difference=difference,
                        status_change_committed=True,
                    ),
                    remaining_turns=state.remaining_turns,
                    review_dynamic=review_dynamic,
                    status_child_compilation=catalog.status_child_compilation,
                    status_child_context=status_child_context,
                )
            )
            trace.extend(status_trace)
            review_dynamic = _sync_review_dynamic(
                review_dynamic,
                scalar.review,
            )
        completed_effect_ids.append(effect.effect_id)

    runtime_after, _consumed = state.drink_runtime.consume(
        action.slot_index,
        instance_id=action.instance_id,
        drink_id=action.drink_id,
    )
    trace.append(
        f"drink-consume:{action.slot_index}:"
        f"{instance.instance_id}:{instance.drink_id}"
    )

    after = replace(
        state,
        scalar=scalar,
        stamina_modifiers=stamina_modifiers,
        zones=status_child_context.zones,
        status_enchant=status_enchant,
        review_dynamic=review_dynamic,
        debuff_registry=debuff_registry,
        generated_runtime=generated_runtime,
        start_play_listeners=start_play_listeners,
        generated_inputs=generated_inputs,
        triggered_review_status_runtime=replace(
            state.triggered_review_status_runtime,
            review_runtime=review_dynamic,
        ),
        status_child_inputs=tuple(status_child_context.inputs),
        status_child_review_state=(
            None
            if status_child_context.review_state is None
            else replace(
                status_child_context.review_state,
                plan2_state=scalar,
            )
        ),
        total_effect_draw_card_count=(
            status_child_context.total_effect_draw_card_count
        ),
        block_consumption_sum_count=(
            status_child_context.block_consumption_sum_count
        ),
        generated_hand_move_turn=status_child_context.hand_move_turn,
        turn_used_support_ids=status_child_context.turn_used_support_ids,
        plays_remaining=plays_remaining,
        drink_runtime=runtime_after,
        aggressive_additive_runtime=_context_aggressive_runtime(status_child_context, scalar, state.aggressive_additive_runtime),
    )
    after, limit_trace = _finalize_plan2_lesson_limit(after)
    trace.extend(limit_trace)
    return after, tuple(trace)


def plan2_native_drink_transition_has_effect(
    state: Plan2NativeHorizonState,
    action: Plan2NativeDrinkAction,
    transition: Plan2NativeTransition,
) -> bool:
    """Compare one exact drink transition against ordered removal alone."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(action, Plan2NativeDrinkAction):
        raise TypeError("action must be Plan2NativeDrinkAction")
    if not isinstance(transition, Plan2NativeTransition):
        raise TypeError("transition must be Plan2NativeTransition")
    if not transition.supported or transition.after is None:
        raise Plan2NativeHorizonError(
            "drink-effect-transition-unavailable",
            action.action_id,
        )
    consumed_runtime, _ = state.drink_runtime.consume(
        action.slot_index,
        instance_id=action.instance_id,
        drink_id=action.drink_id,
    )
    return transition.after != replace(state, drink_runtime=consumed_runtime)


def enumerate_plan2_native_drink_actions(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog | None = None,
) -> tuple[Plan2NativeDrinkAction, ...]:
    """Return drink actions in current native inventory order.

    When a catalog is supplied, omit an exactly consumption-only action.  In
    particular, a pure fixed stamina recovery at full stamina must not win an
    expectimax tie merely because drink slots are enumerated before cards.
    The comparison is over the complete typed horizon after applying the
    exact ordered ``RemoveAt``; mixed drinks remain legal whenever any scalar,
    zone, status, RNG, generated-input, or other runtime field changes.

    A context-dependent unsupported transition is retained so its typed
    blocker remains visible to callers instead of being mistaken for a known
    no-op.
    """

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if state.terminal or state.unmapped_drink_slots:
        # LocalSave slots compact from left to right.  If even one bottle has
        # no exact native program, filtering it out would shift every later
        # UI slot and could drink the wrong item.  Card play remains usable;
        # only drink actions are withheld until the whole ordered inventory
        # is mapped.
        return ()
    actions = tuple(
        Plan2NativeDrinkAction(
            slot_index=index,
            instance_id=value.instance_id,
            drink_id=value.drink_id,
        )
        for index, value in enumerate(state.drink_runtime.inventory)
    )
    if catalog is None:
        return actions
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog or None")
    useful: list[Plan2NativeDrinkAction] = []
    for action in actions:
        transition = simulate_plan2_native_action_lifecycle(
            state,
            action,
            catalog,
        )
        if not transition.supported or transition.after is None:
            useful.append(action)
            continue
        if plan2_native_drink_transition_has_effect(state, action, transition):
            useful.append(action)
    return tuple(useful)


def simulate_plan2_native_action(
    state: Plan2NativeHorizonState,
    action: Plan2NativeOfflineAction,
    catalog: Plan2NativeProgramCatalog,
    *,
    apply_turn_end_recovery: bool = True,
) -> Plan2NativeTransition:
    """Simulate one explicit native action without any proxy/agent call.

    This low-level boundary deliberately leaves ``plays_remaining == 0``
    observable after an ordinary card transaction.  Production planning and
    UI execution must use :func:`simulate_plan2_native_action_lifecycle`,
    because the client immediately continues that boundary through EndTurn
    and TurnStart without offering another input target.
    """

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    if not isinstance(action, (Plan2NativeAction, Plan2NativeDrinkAction)):
        raise TypeError("action must be a typed Plan2 native offline action")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if type(apply_turn_end_recovery) is not bool:
        raise TypeError("apply_turn_end_recovery must be boolean")
    operation_receipts: list[Plan2NativeOperationReceipt] = []
    try:
        if isinstance(action, Plan2NativeDrinkAction):
            after, trace = _execute_drink(state, action, catalog)
        elif action.kind == "end_turn":
            after, trace = _execute_end_turn(
                state,
                catalog,
                apply_turn_end_recovery=apply_turn_end_recovery,
            )
        else:
            after, trace = _execute_play(
                state,
                action.card_guid,
                catalog,
                operation_receipts=operation_receipts,
            )
        return Plan2NativeTransition(
            state,
            action,
            after,
            trace,
            operation_receipts=tuple(operation_receipts),
        )
    except (Plan2NativeHorizonError, NativeOrderedZoneError) as error:
        blocker = (
            error.blocker
            if isinstance(error, Plan2NativeHorizonError)
            else Plan2NativeBlocker(error.code, str(error))
        )
        return Plan2NativeTransition(state, action, None, blockers=(blocker,))
    except (IndexError, OverflowError, TypeError, ValueError) as error:
        return Plan2NativeTransition(
            state,
            action,
            None,
            blockers=(
                Plan2NativeBlocker(
                    "plan2-native-transition-failed-closed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )


def simulate_plan2_native_action_lifecycle(
    state: Plan2NativeHorizonState,
    action: Plan2NativeOfflineAction,
    catalog: Plan2NativeProgramCatalog,
) -> Plan2NativeTransition:
    """Simulate one player input through its automatic durable lifecycle.

    Android's main-loop boundary does not wait for another player input when
    an ordinary PLAY consumes the last playable-card count.  After the card's
    effects and command queue settle, it runs the existing exact EndTurn,
    TurnCheck, TurnStart, hand-reset, draw, and StartPlay implementation.  A
    failure anywhere in that automatic suffix rejects the whole input and
    exposes no partial after-state.

    Explicit ``END_TURN`` remains available when the caller elects to end a
    turn while playable-card count is still positive.  Cards whose Master
    program already ends the turn are also left alone: their low-level
    transition has already crossed this lifecycle boundary.
    """

    transition = simulate_plan2_native_action(state, action, catalog)
    if (
        not transition.supported
        or transition.after is None
        or not isinstance(action, Plan2NativeAction)
        or action.kind != "play"
        or transition.after.terminal
        or transition.after.plays_remaining != 0
    ):
        return transition

    automatic = simulate_plan2_native_action(
        transition.after,
        Plan2NativeAction("end_turn"),
        catalog,
        # The automatic suffix is the card's zero-play lifecycle, not an
        # explicit EndTurn/early-finish command; ExamSetting recovery does not
        # apply to this path.
        apply_turn_end_recovery=False,
    )
    if not automatic.supported or automatic.after is None:
        return Plan2NativeTransition(
            before=state,
            action=action,
            after=None,
            trace=(
                *transition.trace,
                "automatic-lifecycle:zero-play:blocked",
                *automatic.trace,
            ),
            blockers=(
                Plan2NativeBlocker(
                    "automatic-zero-play-lifecycle-blocked",
                    action.action_id,
                ),
                *automatic.blockers,
            ),
            operation_receipts=transition.operation_receipts,
        )
    return Plan2NativeTransition(
        before=state,
        action=action,
        after=automatic.after,
        trace=(
            *transition.trace,
            "automatic-lifecycle:zero-play",
            *automatic.trace,
        ),
        operation_receipts=(
            *transition.operation_receipts,
            *automatic.operation_receipts,
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeSearchLimits:
    max_depth: int = 12
    max_nodes: int = 10_000

    def __post_init__(self) -> None:
        _plain_int(self.max_depth, "max_depth", minimum=1)
        _plain_int(self.max_nodes, "max_nodes", minimum=1)


@dataclass(frozen=True, slots=True)
class Plan2NativeSearchResult:
    root: Plan2NativeHorizonState
    coverage_gate: Plan2NativeCoverageGate
    nodes_expanded: int
    maximum_depth_reached: int
    maximum_turn_reached: int
    terminal_nodes: int
    terminal_path: tuple[str, ...]
    execution_blockers: tuple[Plan2NativeBlocker, ...]
    search_complete: bool

    @property
    def supported_terminal_found(self) -> bool:
        return self.terminal_nodes > 0

    @property
    def all_plan2_ready(self) -> bool:
        return self.coverage_gate.all_plan2_ready

    @property
    def readiness_blockers(self) -> tuple[Plan2NativeBlocker, ...]:
        return self.coverage_gate.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage_gate": self.coverage_gate.to_dict(),
            "nodes_expanded": self.nodes_expanded,
            "maximum_depth_reached": self.maximum_depth_reached,
            "maximum_turn_reached": self.maximum_turn_reached,
            "terminal_nodes": self.terminal_nodes,
            "terminal_path": list(self.terminal_path),
            "supported_terminal_found": self.supported_terminal_found,
            "execution_blockers": [value.to_dict() for value in self.execution_blockers],
            "readiness_blockers": [value.to_dict() for value in self.readiness_blockers],
            "search_complete": self.search_complete,
            "all_plan2_ready": self.all_plan2_ready,
        }


def _actions(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeOfflineAction, ...]:
    if state.terminal:
        return ()
    cards: list[Plan2NativeAction] = []
    for card in state.zones.hand:
        playability = evaluate_plan2_native_playability(state, card.guid, catalog)
        if playability.playable or any(
            blocker.code in {
                "card-program-unsupported",
                "opaque-status-queue-unresolved",
            }
            for blocker in playability.blockers
        ):
            cards.append(Plan2NativeAction("play", card.guid))
    return (
        *enumerate_plan2_native_drink_actions(state, catalog),
        *cards,
        Plan2NativeAction("end_turn"),
    )


def search_plan2_native_horizon(
    root: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    coverage_gate: Plan2NativeCoverageGate | None = None,
    limits: Plan2NativeSearchLimits = Plan2NativeSearchLimits(),
) -> Plan2NativeSearchResult:
    """Breadth-first exact search of the supplied supported transaction set."""

    if not isinstance(root, Plan2NativeHorizonState):
        raise TypeError("root must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(limits, Plan2NativeSearchLimits):
        raise TypeError("limits must be Plan2NativeSearchLimits")
    gate = load_plan2_native_coverage_gate() if coverage_gate is None else coverage_gate
    if not isinstance(gate, Plan2NativeCoverageGate):
        raise TypeError("coverage_gate must be Plan2NativeCoverageGate")

    frontier: list[tuple[Plan2NativeHorizonState, int, tuple[str, ...]]] = [(root, 0, ())]
    seen: set[Plan2NativeHorizonState] = {root}
    blockers: list[Plan2NativeBlocker] = []
    nodes = 0
    maximum_depth = 0
    maximum_turn = root.scalar.current_turn
    terminal_nodes = 0
    terminal_path: tuple[str, ...] = ()
    search_complete = True

    while frontier:
        state, depth, path = frontier.pop(0)
        nodes += 1
        maximum_depth = max(maximum_depth, depth)
        maximum_turn = max(maximum_turn, state.scalar.current_turn)
        if nodes >= limits.max_nodes and frontier:
            blockers.append(Plan2NativeBlocker("search-node-limit-exceeded", str(limits.max_nodes)))
            search_complete = False
            break
        if state.terminal:
            terminal_nodes += 1
            if not terminal_path:
                terminal_path = path
            continue
        if depth >= limits.max_depth:
            blockers.append(Plan2NativeBlocker("search-depth-limit-exceeded", str(limits.max_depth)))
            search_complete = False
            continue
        for action in _actions(state, catalog):
            transition = simulate_plan2_native_action_lifecycle(
                state, action, catalog
            )
            if not transition.supported:
                blockers.extend(transition.blockers)
                continue
            assert transition.after is not None
            child = transition.after
            if child not in seen:
                seen.add(child)
                frontier.append((child, depth + 1, (*path, action.action_id)))

    return Plan2NativeSearchResult(
        root=root,
        coverage_gate=gate,
        nodes_expanded=nodes,
        maximum_depth_reached=maximum_depth,
        maximum_turn_reached=maximum_turn,
        terminal_nodes=terminal_nodes,
        terminal_path=terminal_path,
        execution_blockers=tuple(dict.fromkeys(blockers)),
        search_complete=search_complete,
    )


__all__ = [
    "ACTION_END_TURN",
    "CARD_PLAY_COUNT_BUILD_OWNER",
    "CARD_PLAY_COUNT_INCREMENT_OPERATION",
    "CARD_PLAY_COUNT_MOVE_OWNER",
    "CARD_PLAY_COUNT_RECEIPT_FAMILY",
    "PHASE_MAIN",
    "PHASE_TERMINAL",
    "PLAN2_COVERAGE_TARGET",
    "PLAN2_NATIVE_COVERAGE_ARTIFACT",
    "PLAN2_NATIVE_HORIZON_SCHEMA_VERSION",
    "Plan2NativeAction",
    "Plan2NativeBlocker",
    "Plan2NativeBootstrap",
    "Plan2NativeCardProgram",
    "Plan2NativeCoverageGate",
    "Plan2NativeHorizonError",
    "Plan2NativeHorizonState",
    "Plan2NativeDrinkAction",
    "Plan2NativeDrinkEffect",
    "Plan2NativeDrinkInstance",
    "Plan2NativeDrinkRuntime",
    "Plan2NativeExternalAggressiveEffect",
    "Plan2NativeExternalBlockEffect",
    "Plan2NativeExternalReviewEffect",
    "Plan2NativeExternalStaminaDamageEffect",
    "Plan2NativeOfflineAction",
    "Plan2NativeOperationReceipt",
    "Plan2MasterInitialDeck",
    "Plan2NativeProgramCatalog",
    "Plan2NativePlayability",
    "Plan2NativeQueuedAction",
    "Plan2NativeSearchLimits",
    "Plan2NativeSearchResult",
    "Plan2NativeTimer",
    "Plan2NativeTransition",
    "bootstrap_plan2_native_local_save",
    "bootstrap_plan2_native_master_deck",
    "dispatch_plan2_native_turn_timer",
    "apply_plan2_native_external_aggressive_effect",
    "apply_plan2_native_external_block_effect",
    "apply_plan2_native_external_review_effect",
    "apply_plan2_native_external_stamina_damage_effect",
    "evaluate_plan2_native_playability",
    "execute_plan2_native_current_turn_gimmicks",
    "enumerate_plan2_native_drink_actions",
    "plan2_native_drink_transition_has_effect",
    "load_plan2_native_coverage_gate",
    "load_master_plan2_initial_deck",
    "materialize_plan2_native_card_program",
    "search_plan2_native_horizon",
    "simulate_plan2_native_action",
    "simulate_plan2_native_action_lifecycle",
]
