"""Authoritative Master compiler for the agent-free Plan2 native horizon.

This module deliberately keeps compilation separate from formal coverage.
Every emitted program is built from the imported Master rows and every
effect in that program is bound to an executable Plan2 operation.  A version
whose complete ordered transaction cannot be represented by the current
horizon fails closed; a coverage label is never converted into a program.

The integrated slices include the complete current-Master initial deck for
``i_card-fktn-3-007`` plus exact direct CardPlayAggressive, PlayableValueAdd,
LessonDependBlock, CardDraw, HandGraveDraw, HandAll CardUpgrade, and the
proven DeckGrave random and DeckAll-to-DeckFirst CardMove shapes.  Mixed zone
cards replay their Master effect list at the Playing boundary, before
played-card settlement, so draw recycling and RNG cannot accidentally include
the played card.  The compiler inventories and emits complete typed programs
for the Common+Plan2 inventory in the supplied Master version.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence

from .audition_native_ordered_zones import (
    NativeOrderedCardInstance,
    NativeOrderedZoneError,
    NativeOrderedZoneState,
    _runtime_state_digest,
)
from .audition_local_save_state import (
    LocalSaveExamCardRuntimeState,
    empty_local_save_exam_card_runtime_state,
)
from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_plan3_card_move_search,
    validate_plan3_card_move_search,
)
from .exam_native_rng import next_int32, next_range
from .logic_engine import PLAN_COMMON, PLAN_LOGIC
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT64_MAX,
    AddBlockSettings,
    AddBlockStatus,
    AddingParameterSettings,
    AddingParameterStatus,
    calculate_add_block,
    ceil_f32_to_i32,
    f32,
    get_ratio_effect_int_value,
    ParameterApplicationStatus,
    permille_to_f32,
)
from .plan2_core_runtime import install_plan2_end_turn_remaining_three_exact_listener
from .plan2_card_move import (
    MOVE_LOST,
    PICK_RANDOM,
    Plan2CardMoveContract,
    resolve_plan2_card_move_contract,
)
from .plan2_card_upgrade import (
    Plan2CardUpgradeContract,
    resolve_plan2_card_upgrade,
)
from .plan2_end_turn_remaining_three import (
    DIRECT_TARGETS as END_TURN_REMAINING_THREE_DIRECT_TARGETS,
    Plan2EndTurnRemainingThreeProgram,
    load_plan2_end_turn_remaining_three_program,
)
from .plan2_native_horizon import (
    Plan2NativeCardProgram,
    Plan2NativeHorizonState,
    Plan2NativePlayingEffectResult,
    Plan2NativeProgramCatalog,
    Plan2NativeQueuedAction,
    Plan2NativeGeneratedAllocatorInput,
    _plan3_card_from_ordered,
    _apply_generated_hand_arrivals,
    dispatch_plan2_native_status_enchant_event as _dispatch_plan2_native_status_enchant_event,
)
from .plan2_native_item_scalar_execution import ItemScalarContext, execute_item_scalar_effect
from .plan2_native_item_runtime import (
    EFFECT_AGGRESSIVE as ITEM_EFFECT_AGGRESSIVE,
    ITEM_PHASE_STATUS_CHANGE,
    Plan2NativeItemEvent,
    Plan2NativeItemRuntime,
    dispatch_plan2_native_item_event,
)
from .plan2_pitem_effect_graph import (
    Plan2PItemCatalog,
    Plan2PItemEffectGraphError,
    Plan2PItemEvaluation,
    Plan2PItemOperation,
    Plan2PItemVariant,
    evaluate_effect_graph,
    load_plan2_pitem_catalog,
)
from .plan2_pitem_event_dispatch import (
    PItemDispatchEvaluation,
    PItemEventDispatch,
    PItemEventDispatchError,
    PItemEventPhase,
    PItemPredicateContext,
    parse_plan2_pitem_event_dispatch,
)
from .plan2_core_runtime import apply_plan2_stamina_recover_fix
from .plan2_stamina_recover_fix import StaminaRecoverFixContract
from .plan2_native_catalog_generated_cards import (
    CARD_CREATE_ID,
    CARD_CREATE_SEARCH,
    FORCE_PLAY_SEARCH,
    PLAY_COUNT_BUFF,
    GeneratedBuffForceInput,
    GeneratedCardCatalog,
    GeneratedCardCreateInput,
    GeneratedCardProgram,
    GeneratedHandMoveProgram,
    GeneratedStatusUidInput,
    compile_plan2_native_catalog_generated_cards,
    execute_plan2_generated_buff_force_chain,
    execute_plan2_generated_card_create,
)
from .plan2_native_catalog_status_children import (
    Plan2NativeStatusChildCatalog,
    Plan2NativeStatusChildCompilation,
    compile_plan2_native_catalog_status_children,
)
from .plan2_native_catalog_aggressive_additive import (
    AGGRESSIVE_EFFECT_TYPE as AGGRESSIVE_ADDITIVE_EVENT_EFFECT_TYPE,
    Plan2AggressiveAdditiveEvent,
    Plan2AggressiveAdditiveRuntime,
    Plan2NativeAggressiveAdditiveCatalog,
    Plan2NativeAggressiveAdditiveProgram,
    apply_plan2_native_aggressive_additive_event,
    apply_plan2_native_bound_aggressive_additive_event,
    compile_plan2_native_catalog_aggressive_additive,
    install_plan2_native_aggressive_additive,
)
from .plan2_native_catalog_block_fix_restriction import (
    Plan2NativeBlockFixRestrictionCatalog,
    Plan2NativeBlockFixRestrictionRuntime,
    Plan2NativeBlockFixRestrictionVersion,
    compile_plan2_native_catalog_block_fix_restriction,
    execute_plan2_native_block_add,
    execute_plan2_native_block_fix_restriction,
)
from .plan2_native_catalog_extra_turn import (
    Plan2NativeExtraTurnCatalog,
    Plan2NativeExtraTurnRuntime,
    Plan2NativeExtraTurnVersion,
    compile_plan2_native_catalog_extra_turn,
    execute_plan2_native_extra_turn,
)
from .plan2_native_catalog_stamina_recover_multiple import (
    Plan2NativeStaminaRecoverMultipleCatalog,
    Plan2NativeStaminaRecoverMultipleProgram,
    Plan2NativeStaminaRecoverMultipleSnapshot,
    compile_plan2_native_catalog_stamina_recover_multiple,
    execute_plan2_native_stamina_recover_multiple,
)
from .plan2_native_catalog_lesson_depend_block_retention import (
    LessonDependBlockRetentionCatalog,
    LessonDependBlockRetentionRuntime,
    LessonDependBlockRetentionVersion,
    build_plan2_native_lesson_depend_block_retention_handoff,
    compile_plan2_native_lesson_depend_block_retention_catalog,
    execute_plan2_native_lesson_depend_block_retention,
)
from .plan2_card_search_play_count_buff import (
    PlayCountBuffRuntime,
    use_matching_play_count_buff,
)
from .plan3_native_state import (
    PLAN2_NATIVE_CUSTOMIZATION_GROW_TYPES,
    Plan3NativeRuntimeCustomization,
    Plan3NativeRuntimeCustomizationEffect,
    Plan3NativeState,
    Plan3NativeStateError,
    parse_plan2_native_runtime_customization,
)
from .plan2_native_catalog_status_enchant_encore import (
    AGGRESSIVE_EFFECT_TYPE as ENCORE_AGGRESSIVE_EFFECT_TYPE,
    PHASE_AGGRESSIVE_UP_INTERVAL as ENCORE_PHASE_AGGRESSIVE_UP_INTERVAL,
    STATUS_ENCHANT_ENCORE_EFFECT_TYPE,
    Plan2NativeStatusEnchantEncoreEvent,
    Plan2NativeStatusEnchantEncoreCatalog,
    Plan2NativeStatusEnchantEncoreRuntime,
    Plan2StatusEnchantEncoreVersion,
    compile_plan2_native_catalog_status_enchant_encore,
    install_plan2_native_status_enchant_encore,
    plan_plan2_native_status_enchant_encore,
)
from .plan2_native_catalog_card_triggers import (
    CardPlayOrigin,
    Plan2NativeCardTriggerCatalog,
    Plan2NativeCardTriggerHandoff,
    Plan2NativeCardTriggerSnapshot,
    compile_plan2_native_catalog_card_triggers,
    evaluate_plan2_native_card_trigger,
)
from .plan2_native_catalog_block_dynamic import (
    BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE,
    BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE,
    Plan2NativeDynamicBlockCatalog,
    Plan2NativeDynamicBlockExecutionInput,
    Plan2NativeDynamicBlockProgram,
    Plan2NativeDynamicBlockRuntime,
    compile_plan2_native_catalog_block_dynamic,
    execute_plan2_native_dynamic_block,
)
from .plan2_native_catalog_lesson_depend_aggressive import (
    TARGET_EFFECT_TYPE as LESSON_DEPEND_AGGRESSIVE_EFFECT_TYPE,
    LessonDependAggressiveEffect,
    LessonDependAggressiveCatalog,
    LessonDependAggressiveRuntime,
    LessonDependAggressiveVersion,
    build_plan2_native_lesson_depend_aggressive_effect_handoff,
    build_plan2_native_lesson_depend_aggressive_handoff,
    compile_plan2_native_lesson_depend_aggressive_catalog,
    execute_plan2_native_lesson_depend_aggressive,
    load_plan2_native_lesson_depend_aggressive_effect,
)
from .plan2_native_catalog_debuff import (
    EFFECT_ANTI_DEBUFF,
    EFFECT_DEBUFF_RECOVER,
    Plan2NativeCatalogDebuff,
    Plan2NativeDebuffEffectHandoff,
    Plan2NativeDebuffRegistry,
    PlayOrigin as Plan2DebuffPlayOrigin,
    compile_plan2_native_catalog_debuff,
    execute_plan2_native_anti_debuff_handoff,
    execute_plan2_native_debuff_recover_handoff,
    gate_plan2_native_status_addition,
)
from .plan2_native_catalog_effect_chains import (
    EFFECT_TIMER,
    Plan2NativeEffectChainCatalog,
    Plan2NativeEffectChainInstallInput,
    Plan2NativeEffectChainProgram,
    Plan2NativeEffectChainRuntime,
    compile_plan2_native_catalog_effect_chains,
    install_plan2_native_effect_chain,
)
from .plan2_native_catalog_effect_triggers import (
    EffectTriggerBoundary,
    Plan2NativeEffectTriggerCatalog,
    Plan2NativeEffectTriggerHandoff,
    Plan2NativeEffectTriggerSnapshot,
    compile_plan2_native_catalog_effect_triggers,
    evaluate_plan2_native_effect_trigger,
)
from .plan2_native_catalog_effect_triggers_remaining import (
    Plan2RemainingEffectTriggerCatalog,
    Plan2RemainingEffectTriggerHandoff,
    Plan2RemainingEffectTriggerSnapshot,
    Plan2RemainingSearchCardSnapshot,
    Plan2RemainingSearchZoneSnapshot,
    compile_plan2_native_catalog_effect_triggers_remaining,
    evaluate_plan2_native_catalog_effect_trigger_remaining,
)
from .plan2_native_catalog_stamina import (
    BUFF_COST_TYPES,
    DIRECT_EFFECT_OWNER,
    Plan2CostResources,
    Plan2NativeCardCost,
    Plan2NativeCatalogStamina,
    Plan2NativeStaminaEffectHandoff,
    Plan2StaminaModifierRuntime,
    compile_plan2_native_catalog_stamina,
    execute_plan2_native_stamina_handoff,
)
from .plan2_native_catalog_status_enchant import (
    PHASE_STATUS_CHANGE,
    Plan2NativeStatusEnchantCatalog,
    Plan2NativeStatusEnchantEvent,
    Plan2NativeStatusEnchantRuntime,
    Plan2StatusEnchantInstallProgram,
    compile_plan2_native_catalog_status_enchant,
    install_plan2_native_status_enchant,
)
from .plan2_native_catalog_triggered_effect_executors import (
    Plan2NativeTriggeredEffectExecutionInput,
    Plan2NativeTriggeredEffectExecutorCatalog,
    Plan2NativeTriggeredEffectExecutorHandoff,
    TriggeredEffectExecutorFamily,
    compile_plan2_native_catalog_triggered_effect_executors,
    execute_plan2_native_triggered_effect,
)
from .plan2_native_catalog_status_enchant_triggered import (
    Plan2TriggeredReviewStatusInstallInput,
    Plan2TriggeredReviewStatusRuntime,
    Plan2TriggeredStatusEnchantOwnershipAudit,
    Plan2TriggeredStatusEnchantProgram,
    compile_plan2_native_catalog_status_enchant_triggered,
    install_plan2_triggered_review_status,
)
from .plan2_native_catalog_card_move_other import (
    Plan2NativeCatalogCardMoveOtherCatalog,
    Plan2NativeCatalogCardMoveOtherVersion,
    compile_plan2_native_catalog_card_move_other,
    execute_plan2_native_catalog_card_move_other,
)
from .plan2_card_move_remaining import RemainingCardMoveState
from .plan2_start_play_trigger import (
    Plan2StartPlayListener,
    Plan2StartPlayStatusProgram,
    load_plan2_start_play_trigger_catalog,
    simulate_install_plan2_start_play_listener,
)
from .plan2_end_turn_trigger import (
    Plan2EndTurnProgram,
    load_plan2_end_turn_program,
    simulate_install_plan2_end_turn_listener,
)
from .plan2_native_catalog_review_multiple import (
    Plan2NativeReviewMultipleCatalog,
    Plan2NativeReviewMultipleProgram,
    Plan2ReviewMultiplePlayOrigin,
    compile_plan2_native_catalog_review_multiple,
    install_plan2_native_review_multiple,
)
from .plan2_native_catalog_review_dynamic import (
    LAYER_REVIEW_ADDITIVE,
    OP_INSTALL_LESSON_DEPENDENT,
    OP_INSTALL_LESSON_MULTIPLE,
    OP_INSTALL_REVIEW_ADDITIVE,
    OP_REVIEW_PER_SEARCH_COUNT,
    OP_REVIEW_VALUE_MULTIPLE,
    TARGET_EFFECT_TYPES as REVIEW_DYNAMIC_EFFECT_TYPES,
    Plan2NativeReviewDynamicCatalog,
    Plan2NativeReviewDynamicEventInput,
    Plan2NativeReviewDynamicInstallInput,
    Plan2NativeReviewDynamicLessonRequest,
    Plan2NativeReviewDynamicProgram,
    Plan2NativeReviewDynamicRuntime,
    compile_plan2_native_catalog_review_dynamic,
    execute_plan2_native_review_dynamic_event,
    handoff_plan2_native_review_dynamic_lesson,
    install_plan2_native_review_dynamic,
)
from .plan2_hand_grave_draw import (
    HandGraveDrawExecutable,
    HandGraveDrawUnresolved,
    resolve_plan2_hand_grave_draw,
)
from .plan2_exam_save_battle_scoring import (
    Plan2ScoreIntegrationPoint,
    Plan2ScoreKind,
)
from .plan2_state import (
    Plan2EndTurnEffect,
    Plan2State,
    apply_plan2_state_score,
    carry_plan2_state_application_status,
)
from .plan3_engine import Plan3Effect
from .plan2_search_play_card_stamina_consumption_change import (
    SearchPlayCardStaminaRuntime,
)
from .plan2_add_grow_effect import (
    Plan2AddGrowCardInstance,
    Plan2AddGrowDeckAllState,
    Plan2AddGrowProgram,
)
from .plan2_lesson_depend_block_consumption_final import FinalCardRuntime
from .plan2_native_catalog_misc_exact import (
    Plan2MiscExactAddGrowRequest,
    Plan2MiscExactAggressiveRequest,
    Plan2MiscExactExecutionContext,
    Plan2MiscExactFamily,
    Plan2MiscExactLessonBlockSumRequest,
    Plan2MiscExactSearchStaminaRequest,
    Plan2NativeCatalogMiscExactCatalog,
    Plan2NativeCatalogMiscExactHandoff,
    execute_plan2_native_catalog_misc_exact,
    load_plan2_native_catalog_misc_exact,
)


PLAN2_NATIVE_PROGRAM_CATALOG_SCHEMA_VERSION = 1
# Historical baseline for pinned regression reports; never an update gate.
PLAN2_MASTER_VERSION_TARGET = 709
_TRIGGERED_TIMER_OVERLAY_DEPTH = 0

_COST_STAMINA = "ExamCostType_Unknown"
_MOVE_GRAVE = "ProduceCardMovePositionType_Grave"
_MOVE_LOST = "ProduceCardMovePositionType_Lost"
_MOVE_UNKNOWN = "ProduceCardMoveEffectTriggerType_Unknown"

_PLAY_COUNT_BUFF_RARITIES = frozenset(
    {
        "ProduceCardRarity_N",
        "ProduceCardRarity_R",
        "ProduceCardRarity_Sr",
        "ProduceCardRarity_Ssr",
    }
)
_PLAY_COUNT_BUFF_ALL_SEARCH_ID = "p_card_search-n-r-sr-ssr-playing"
_PLAY_COUNT_BUFF_ACTIVE_SEARCH_ID = (
    "p_card_search-active_skill-n-r-sr-ssr-playing"
)
_ACTIVE_SKILL_CATEGORY = "ProduceCardCategory_ActiveSkill"

_EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
_EFFECT_REVIEW = "ProduceExamEffectType_ExamReview"
_EFFECT_LESSON = "ProduceExamEffectType_ExamLesson"
_EFFECT_LESSON_DEPEND_REVIEW = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
_EFFECT_AGGRESSIVE = "ProduceExamEffectType_ExamCardPlayAggressive"
_EFFECT_PLAYABLE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
_EFFECT_LESSON_DEPEND_BLOCK = "ProduceExamEffectType_ExamLessonDependBlock"
_EFFECT_LESSON_DEPEND_AGGRESSIVE = LESSON_DEPEND_AGGRESSIVE_EFFECT_TYPE
_EFFECT_CARD_DRAW = "ProduceExamEffectType_ExamCardDraw"
_EFFECT_HAND_GRAVE_DRAW = (
    "ProduceExamEffectType_ExamHandGraveCountCardDraw"
)
_EFFECT_CARD_MOVE = "ProduceExamEffectType_ExamCardMove"
_EFFECT_CARD_UPGRADE = "ProduceExamEffectType_ExamCardUpgrade"
_EFFECT_STATUS_ENCHANT = "ProduceExamEffectType_ExamStatusEnchant"
_EFFECT_REVIEW_MULTIPLE = "ProduceExamEffectType_ExamReviewMultiple"
_EFFECT_STAMINA_TYPES = frozenset(
    {
        "ProduceExamEffectType_ExamStaminaConsumptionDown",
        "ProduceExamEffectType_ExamStaminaConsumptionDownFix",
        "ProduceExamEffectType_ExamStaminaConsumptionAdd",
        "ProduceExamEffectType_ExamStaminaRecoverFix",
    }
)
_EXECUTABLE_STATUS_CHILD_TYPES = frozenset(
    {
        _EFFECT_LESSON_DEPEND_BLOCK,
        _EFFECT_LESSON_DEPEND_REVIEW,
        _EFFECT_REVIEW,
        _EFFECT_BLOCK,
        _EFFECT_LESSON,
        _EFFECT_AGGRESSIVE,
    }
)

_EXACT_DECK_GRAVE_LOST_RANDOM_EFFECT_ID = (
    "e_effect-exam_card_move-p_card_search-deck_grave-"
    "p_card-00-acc-0_002-lost-random-1_1"
)

_TRIGGER_PHASE_NONE = "ProduceExamPhaseType_None"
_TRIGGER_REVIEW_UP = "ProduceExamFieldStatusType_ReviewUp"
_TRIGGER_MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
_TRIGGER_LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"


def _plain_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{label} must contain a JSON object")
    return parsed


def _json_array(value: object, label: str) -> tuple[object, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise ValueError(f"{label} must contain a JSON array")
    return tuple(parsed)


def _checked_nonnegative_add(left: int, right: int, label: str) -> int:
    result = left + right
    if not 0 <= result <= INT32_MAX:
        raise OverflowError(f"{label} result is outside non-negative Int32")
    return result


def _apply_review_dynamic_lesson(
    scalar: Plan2State,
    runtime: Plan2NativeReviewDynamicRuntime,
    *,
    value: int,
    count: int,
    play_origin: str,
) -> tuple[Plan2State, tuple[str, ...]]:
    if runtime.review != scalar.review:
        raise ValueError("review-dynamic-scalar-snapshot-mismatch")
    handoff = handoff_plan2_native_review_dynamic_lesson(
        runtime,
        Plan2NativeReviewDynamicLessonRequest(
            value=value,
            count=count,
            aggressive_at_execution=scalar.card_play_aggressive,
            adding_status=AddingParameterStatus(
                aggressive=scalar.card_play_aggressive
            ),
            settings=AddingParameterSettings(),
            application_status=scalar.parameter_application_status(slump=False),
            play_origin=play_origin,
        ),
    )
    if not handoff.executable:
        raise ValueError(
            "review-dynamic-lesson-failed-closed:"
            + ",".join(handoff.unresolved)
        )
    return (
        carry_plan2_state_application_status(
            scalar,
            handoff.final_application_status,
        ),
        handoff.trace,
    )


def _apply_review_dynamic_review_gain(
    scalar: Plan2State,
    runtime: Plan2NativeReviewDynamicRuntime,
    *,
    value: int,
    count: int,
) -> Plan2State:
    """Apply a direct Review gain through ordered active additive layers."""

    if runtime.review != scalar.review:
        raise ValueError("review-dynamic-scalar-snapshot-mismatch")
    _plain_int(value, "review_gain", minimum=0)
    _plain_int(count, "review_gain_count", minimum=1)
    after = scalar
    for _ in range(count):
        multiple = f32(1.0)
        for layer in runtime.layers:
            if layer.kind == LAYER_REVIEW_ADDITIVE and (
                not layer.is_turn_limited or layer.turn > 0
            ):
                multiple = f32(
                    multiple + permille_to_f32(layer.permille)
                )
        delta = ceil_f32_to_i32(f32(f32(value) * multiple))
        after = replace(
            after,
            review=_checked_nonnegative_add(after.review, delta, "review"),
        )
    return after


def _sync_review_dynamic_scalar(
    runtime: Plan2NativeReviewDynamicRuntime,
    review: int,
) -> Plan2NativeReviewDynamicRuntime:
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


_RUNTIME_CUSTOMIZATION_REVIEW_ADD = (
    "ProduceCardGrowEffectType_ReviewAdd"
)
_RUNTIME_CUSTOMIZATION_LESSON_ADD = (
    "ProduceCardGrowEffectType_LessonAdd"
)
_RUNTIME_CUSTOMIZATION_LESSON_COUNT_ADD = (
    "ProduceCardGrowEffectType_LessonCountAdd"
)
_RUNTIME_CUSTOMIZATION_LESSON_DEPEND_REVIEW_ADD = (
    "ProduceCardGrowEffectType_LessonDependExamReviewAdd"
)
_RUNTIME_CUSTOMIZATION_EFFECT_ADD = "ProduceCardGrowEffectType_EffectAdd"
_RUNTIME_CUSTOMIZATION_INITIAL_ADD = "ProduceCardGrowEffectType_InitialAdd"
_RUNTIME_CUSTOMIZATION_BLOCK_ADD = "ProduceCardGrowEffectType_BlockAdd"
_RUNTIME_CUSTOMIZATION_AGGRESSIVE_ADD = (
    "ProduceCardGrowEffectType_AggressiveAdd"
)
_RUNTIME_CUSTOMIZATION_COST_REDUCE = (
    "ProduceCardGrowEffectType_CostReduce"
)
_PLAN2_RUNTIME_CUSTOMIZATION_GROW_TYPES = (
    PLAN2_NATIVE_CUSTOMIZATION_GROW_TYPES
)


def _compile_runtime_customization_effect(
    effect: Plan3Effect,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2MasterPlayingOperation:
    """Compile one direct Master child of a CardCustomize ``EffectAdd``.

    CardCustomize grows are resolved at Playing time because the active
    counts live on the card instance.  The child itself still follows the
    same typed scalar/queue grammar as the static card compiler; all other
    effect families remain fail-closed until they have an executor that can
    carry their catalog context.
    """

    if not isinstance(effect, Plan3Effect):
        raise TypeError("runtime customization child must be Plan3Effect")
    if (
        effect.chain_effect_id
        or effect.chain_effect_ids
        or effect.status_enchant_id
        or effect.status_enchant is not None
        or effect.trigger is not None
        or effect.card_move_rule is not None
    ):
        raise ValueError(
            f"runtime-customization-added-effect-shape-unsupported:{effect.id}"
        )
    if effect.effect_type in {
        _EFFECT_BLOCK,
        _EFFECT_REVIEW,
        _EFFECT_LESSON,
        _EFFECT_LESSON_DEPEND_REVIEW,
        _EFFECT_AGGRESSIVE,
        _EFFECT_LESSON_DEPEND_BLOCK,
    }:
        if (
            effect.value1 < 0
            or effect.value2 != 0
            or effect.effect_count < 0
            or effect.effect_turn != 0
        ):
            raise ValueError(
                f"runtime-customization-added-scalar-shape-unsupported:{effect.id}"
            )
        return Plan2MasterScalarOperation(
            effect.id,
            effect.effect_type,
            effect.value1,
            effect.value2,
            effect.effect_count,
            effect.effect_turn,
        )
    if effect.effect_type == _EFFECT_PLAYABLE_ADD:
        if (
            effect.value1 != 0
            or effect.value2 != 0
            or effect.effect_turn != 0
            or effect.effect_count < 1
        ):
            raise ValueError(
                f"runtime-customization-added-playable-shape-unsupported:{effect.id}"
            )
        return Plan2NativeQueuedAction("add_playable", value=effect.effect_count)
    if effect.effect_type == _EFFECT_CARD_DRAW:
        if (
            effect.value1 < 1
            or effect.value2 != 0
            or effect.effect_turn != 0
            or effect.effect_count != 0
        ):
            raise ValueError(
                f"runtime-customization-added-draw-shape-unsupported:{effect.id}"
            )
        return Plan2NativeQueuedAction("draw", value=effect.value1)
    if effect.effect_type == _EFFECT_LESSON_DEPEND_AGGRESSIVE:
        target = load_plan2_native_lesson_depend_aggressive_effect(
            effect.id,
            database=database,
        )
        if (
            target.effect_type,
            target.value1,
            target.value2,
            target.count,
            target.turn,
        ) != (
            effect.effect_type,
            effect.value1,
            effect.value2,
            effect.effect_count,
            effect.effect_turn,
        ):
            raise ValueError(
                "runtime-customization-added-effect-lineage-mismatch:"
                f"{effect.id}"
            )
        return Plan2MasterRuntimeLessonDependAggressiveOperation(target)
    raise ValueError(
        "runtime-customization-added-effect-unbound:"
        f"{effect.effect_type}:{effect.id}"
    )


def _runtime_customization_operation_effect_type(
    operation: Plan2MasterPlayingOperation,
) -> str | None:
    if isinstance(operation, Plan2MasterTriggeredOperation):
        operation = operation.operation
    if isinstance(operation, Plan2MasterScalarOperation):
        return operation.effect_type
    return None


def _runtime_customization_replace_scalar(
    operation: Plan2MasterPlayingOperation,
    scalar: Plan2MasterScalarOperation,
) -> Plan2MasterPlayingOperation:
    if isinstance(operation, Plan2MasterTriggeredOperation):
        return Plan2MasterTriggeredOperation(scalar, operation.handoff)
    return scalar


def apply_plan2_runtime_customization_operations(
    operations: tuple[Plan2MasterPlayingOperation, ...],
    customization: Plan3NativeRuntimeCustomization,
    *,
    database: Path = DEFAULT_DATABASE,
) -> tuple[tuple[Plan2MasterPlayingOperation, ...], tuple[str, ...]]:
    """Apply the canonical ordered Plan2 CardCustomize contract to Playing.

    This is the single operation-materialization boundary shared by live
    Playing execution and retained command-history replay.  Callers must
    obtain ``customization`` from
    :func:`parse_plan2_native_runtime_customization`; no grow-family parsing
    belongs in an individual execution consumer.
    """

    if not isinstance(customization, Plan3NativeRuntimeCustomization):
        raise TypeError("customization must be Plan3NativeRuntimeCustomization")
    effective = list(operations)
    trace: list[str] = []
    for entry in customization.effects:
        if not isinstance(entry, Plan3NativeRuntimeCustomizationEffect):
            raise TypeError("invalid runtime customization entry")
        if entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_INITIAL_ADD:
            # InitialAdd changes the card-definition initial marker.  At the
            # Playing boundary the card already exists as an ordered runtime
            # instance, so there is no direct operation to schedule.
            trace.append(
                "playing-customization-initial-add:"
                f"{entry.customize_id}:{entry.grow_effect_id}"
            )
            continue
        if entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_EFFECT_ADD:
            if entry.added_effect is None:
                raise ValueError(
                    "runtime-customization-added-effect-missing:"
                    f"{entry.grow_effect_id}"
                )
            effective.append(
                _compile_runtime_customization_effect(
                    entry.added_effect,
                    database=database,
                )
            )
            trace.append(
                "playing-customization-effect-add:"
                f"{entry.customize_id}:{entry.grow_effect_id}:"
                f"{entry.added_effect.id}"
            )
            continue
        if entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_COST_REDUCE:
            # CostReduce is consumed by the shared playability/payment
            # boundary before Playing effects execute.  Keep its ordered
            # provenance here without applying the cost a second time.
            trace.append(
                "playing-customization-cost-already-applied:"
                f"{entry.customize_id}:{entry.grow_effect_id}:-{entry.value}"
            )
            continue
        if entry.grow_effect_type in {
            _RUNTIME_CUSTOMIZATION_REVIEW_ADD,
            _RUNTIME_CUSTOMIZATION_LESSON_ADD,
            _RUNTIME_CUSTOMIZATION_LESSON_COUNT_ADD,
            _RUNTIME_CUSTOMIZATION_LESSON_DEPEND_REVIEW_ADD,
            _RUNTIME_CUSTOMIZATION_BLOCK_ADD,
            _RUNTIME_CUSTOMIZATION_AGGRESSIVE_ADD,
        }:
            if entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_REVIEW_ADD:
                target_type = _EFFECT_REVIEW
                field = "value1"
            elif entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_LESSON_ADD:
                target_type = _EFFECT_LESSON
                field = "value1"
            elif entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_LESSON_COUNT_ADD:
                target_type = _EFFECT_LESSON
                field = "count"
            elif entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_BLOCK_ADD:
                target_type = _EFFECT_BLOCK
                field = "value1"
            elif entry.grow_effect_type == _RUNTIME_CUSTOMIZATION_AGGRESSIVE_ADD:
                target_type = _EFFECT_AGGRESSIVE
                field = "value1"
            else:
                target_type = _EFFECT_LESSON_DEPEND_REVIEW
                field = "value1"
            matching_indexes = tuple(
                index
                for index, operation in enumerate(effective)
                if _runtime_customization_operation_effect_type(operation)
                == target_type
            )
            if (
                entry.grow_effect_type
                in {
                    _RUNTIME_CUSTOMIZATION_BLOCK_ADD,
                    _RUNTIME_CUSTOMIZATION_AGGRESSIVE_ADD,
                }
                and len(matching_indexes) != 1
            ):
                raise ValueError(
                    "runtime-customization-target-effect-count:"
                    f"{entry.grow_effect_id}:{target_type}:"
                    f"expected=1:actual={len(matching_indexes)}"
                )
            for index in matching_indexes:
                operation = effective[index]
                unwrapped = (
                    operation.operation
                    if isinstance(operation, Plan2MasterTriggeredOperation)
                    else operation
                )
                assert isinstance(unwrapped, Plan2MasterScalarOperation)
                if field == "value1":
                    updated = replace(
                        unwrapped,
                        value1=_checked_nonnegative_add(
                            unwrapped.value1,
                            entry.value,
                            "runtime_customization_value1",
                        ),
                    )
                else:
                    updated = replace(
                        unwrapped,
                        count=_checked_nonnegative_add(
                            unwrapped.count,
                            entry.value,
                            "runtime_customization_count",
                        ),
                    )
                effective[index] = _runtime_customization_replace_scalar(
                    operation,
                    updated,
                )
            if not matching_indexes:
                raise ValueError(
                    "runtime-customization-target-effect-missing:"
                    f"{entry.grow_effect_id}:{target_type}"
                )
            trace.append(
                "playing-customization-delta:"
                f"{entry.customize_id}:{entry.grow_effect_id}:+{entry.value}"
            )
            continue
        raise ValueError(
            "runtime-customization-grow-unbound:"
            f"{entry.grow_effect_id}:{entry.grow_effect_type}"
        )
    return tuple(effective), tuple(trace)


# Compatibility import for focused tests and older diagnostics.  This is an
# alias to the single public materializer, not a second execution path.
_apply_runtime_customization_operations = (
    apply_plan2_runtime_customization_operations
)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeCatalogBlocker:
    code: str
    card_id: str
    upgrade: int
    detail: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.code, "blocker code")
        _nonempty(self.card_id, "blocker card_id")
        _plain_int(self.upgrade, "blocker upgrade", minimum=0)
        if not isinstance(self.detail, str):
            raise TypeError("blocker detail must be text")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Plan2MasterScalarOperation:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "effect_id")
        _nonempty(self.effect_type, "effect_type")
        for name in ("value1", "value2", "count", "turn"):
            _plain_int(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class Plan2MasterRemainingThreeInstallOperation:
    effect_id: str
    program: Plan2EndTurnRemainingThreeProgram

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "effect_id")
        if not isinstance(self.program, Plan2EndTurnRemainingThreeProgram):
            raise TypeError("program must be Plan2EndTurnRemainingThreeProgram")
        if self.program.wrapper_effect_id != self.effect_id:
            raise ValueError("remaining-three program/effect identity mismatch")


Plan2MasterDirectOperation = (
    Plan2MasterScalarOperation | Plan2MasterRemainingThreeInstallOperation
)


@dataclass(frozen=True, slots=True)
class Plan2MasterHandGraveDrawOperation:
    effect_id: str
    executable: HandGraveDrawExecutable

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "effect_id")
        if not isinstance(self.executable, HandGraveDrawExecutable):
            raise TypeError("executable must be HandGraveDrawExecutable")
        if self.executable.contract.effect_id != self.effect_id:
            raise ValueError("hand/grave draw effect identity mismatch")


@dataclass(frozen=True, slots=True)
class Plan2MasterCardMoveOperation:
    effect_id: str
    contract: Plan2CardMoveContract
    search: ProduceCardSearchRule

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "effect_id")
        if not isinstance(self.contract, Plan2CardMoveContract):
            raise TypeError("contract must be Plan2CardMoveContract")
        if not isinstance(self.search, ProduceCardSearchRule):
            raise TypeError("search must be ProduceCardSearchRule")
        if self.contract.effect_id != self.effect_id:
            raise ValueError("CardMove effect identity mismatch")
        if self.search.id != self.contract.search_id:
            raise ValueError("CardMove search identity mismatch")
        if self.effect_id != _EXACT_DECK_GRAVE_LOST_RANDOM_EFFECT_ID:
            raise ValueError("unsupported native-zone CardMove effect")
        if (
            self.contract.destination != MOVE_LOST
            or self.contract.pick_range_type != PICK_RANDOM
            or self.contract.count_range != (1, 1)
        ):
            raise ValueError("unsupported native-zone CardMove contract")
        reason = validate_plan3_card_move_search(self.search)
        if reason is not None:
            raise ValueError(reason)
        if self.search.card_position_type != "ProduceCardPositionType_DeckGrave":
            raise ValueError("CardMove search must target ordered DeckGrave")


@dataclass(frozen=True, slots=True)
class Plan2MasterCardUpgradeOperation:
    effect_id: str
    contract: Plan2CardUpgradeContract
    master_card_refs: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _nonempty(self.effect_id, "effect_id")
        if not isinstance(self.contract, Plan2CardUpgradeContract):
            raise TypeError("contract must be Plan2CardUpgradeContract")
        if self.contract.effect_id != self.effect_id:
            raise ValueError("CardUpgrade effect identity mismatch")
        if (
            self.contract.search.card_position_type
            != "ProduceCardPositionType_Hand"
            or self.contract.pick_range_type != "ProducePickRangeType_All"
            or self.contract.pick_count_min != 0
            or self.contract.pick_count_max != 0
            or self.contract.search_support_pause
        ):
            raise ValueError("unsupported native-zone CardUpgrade contract")
        refs = tuple(self.master_card_refs)
        if refs != tuple(sorted(set(refs))):
            raise ValueError("master_card_refs must be sorted and unique")
        object.__setattr__(self, "master_card_refs", refs)


Plan2MasterZoneOperation = (
    Plan2MasterHandGraveDrawOperation
    | Plan2MasterCardMoveOperation
    | Plan2MasterCardUpgradeOperation
)


@dataclass(frozen=True, slots=True)
class Plan2MasterStaminaOperation:
    handoff: Plan2NativeStaminaEffectHandoff

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, Plan2NativeStaminaEffectHandoff):
            raise TypeError("handoff must be Plan2NativeStaminaEffectHandoff")
        if not self.handoff.direct or self.handoff.owner != DIRECT_EFFECT_OWNER:
            raise ValueError("Playing stamina operation must be a direct handoff")

    @property
    def effect_id(self) -> str:
        return self.handoff.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterStatusEnchantOperation:
    program: Plan2StatusEnchantInstallProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2StatusEnchantInstallProgram):
            raise TypeError("program must be Plan2StatusEnchantInstallProgram")

    @property
    def effect_id(self) -> str:
        return self.program.installer_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterReviewMultipleOperation:
    program: Plan2NativeReviewMultipleProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeReviewMultipleProgram):
            raise TypeError("program must be Plan2NativeReviewMultipleProgram")

    @property
    def effect_id(self) -> str:
        return self.program.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterDynamicBlockOperation:
    program: Plan2NativeDynamicBlockProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeDynamicBlockProgram):
            raise TypeError("program must be Plan2NativeDynamicBlockProgram")

    @property
    def effect_id(self) -> str:
        return self.program.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterLessonDependAggressiveOperation:
    version: LessonDependAggressiveVersion

    def __post_init__(self) -> None:
        if not isinstance(self.version, LessonDependAggressiveVersion):
            raise TypeError("version must be LessonDependAggressiveVersion")

    @property
    def effect_id(self) -> str:
        return self.version.target_effect.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterRuntimeLessonDependAggressiveOperation:
    effect: LessonDependAggressiveEffect

    def __post_init__(self) -> None:
        if not isinstance(self.effect, LessonDependAggressiveEffect):
            raise TypeError("effect must be a typed lesson/aggressive target")

    @property
    def effect_id(self) -> str:
        return self.effect.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterReviewDynamicOperation:
    program: Plan2NativeReviewDynamicProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeReviewDynamicProgram):
            raise TypeError("program must be Plan2NativeReviewDynamicProgram")

    @property
    def effect_id(self) -> str:
        return self.program.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterDebuffOperation:
    handoff: Plan2NativeDebuffEffectHandoff

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, Plan2NativeDebuffEffectHandoff):
            raise TypeError("handoff must be Plan2NativeDebuffEffectHandoff")

    @property
    def effect_id(self) -> str:
        return self.handoff.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterStatusEnchantEncoreOperation:
    program: Plan2StatusEnchantEncoreVersion

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2StatusEnchantEncoreVersion):
            raise TypeError("program must be Plan2StatusEnchantEncoreVersion")

    @property
    def effect_id(self) -> str:
        return self.program.wrapper_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterEffectChainOperation:
    program: Plan2NativeEffectChainProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeEffectChainProgram):
            raise TypeError("program must be Plan2NativeEffectChainProgram")
        if not self.program.executable:
            raise ValueError("effect-chain child runtime must be executable")

    @property
    def effect_id(self) -> str:
        return self.program.timer_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterTriggeredOperation:
    operation: object
    handoff: Plan2NativeEffectTriggerHandoff | Plan2RemainingEffectTriggerHandoff

    def __post_init__(self) -> None:
        if not isinstance(
            self.handoff,
            (Plan2NativeEffectTriggerHandoff, Plan2RemainingEffectTriggerHandoff),
        ):
            raise TypeError("handoff must be a typed effect-trigger occurrence")

    @property
    def effect_id(self) -> str:
        return self.handoff.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterGeneratedCreateOperation:
    program: GeneratedCardProgram

    @property
    def effect_id(self) -> str:
        return self.program.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterGeneratedBuffForceOperation:
    buff_program: GeneratedCardProgram
    force_program: GeneratedCardProgram

    def __post_init__(self) -> None:
        if self.buff_program.operation != "play-count-buff":
            raise ValueError("generated buff program mismatch")
        if self.force_program.operation != "force-play-search":
            raise ValueError("generated force program mismatch")
        if self.buff_program.ref != self.force_program.ref:
            raise ValueError("generated buff/force version mismatch")

    @property
    def effect_id(self) -> str:
        return self.buff_program.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterGeneratedForceSettlementOperation:
    program: GeneratedCardProgram

    @property
    def effect_id(self) -> str:
        return self.program.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterAggressiveAdditiveOperation:
    program: Plan2NativeAggressiveAdditiveProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeAggressiveAdditiveProgram):
            raise TypeError("program must be a typed aggressive-additive program")

    @property
    def effect_id(self) -> str:
        return self.program.target_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterBlockFixRestrictionOperation:
    program: Plan2NativeBlockFixRestrictionVersion

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeBlockFixRestrictionVersion):
            raise TypeError("program must be a typed block fix/restriction program")

    @property
    def effect_id(self) -> str:
        return self.program.target_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterExtraTurnOperation:
    program: Plan2NativeExtraTurnVersion

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeExtraTurnVersion):
            raise TypeError("program must be a typed extra-turn program")

    @property
    def effect_id(self) -> str:
        return self.program.target_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterStaminaRecoverMultipleOperation:
    program: Plan2NativeStaminaRecoverMultipleProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeStaminaRecoverMultipleProgram):
            raise TypeError("program must be a typed stamina-recover-multiple program")

    @property
    def effect_id(self) -> str:
        return self.program.target_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterLessonDependBlockRetentionOperation:
    version: LessonDependBlockRetentionVersion

    def __post_init__(self) -> None:
        if not isinstance(self.version, LessonDependBlockRetentionVersion):
            raise TypeError("version must be a typed lesson/block-retention version")

    @property
    def effect_id(self) -> str:
        return self.version.target_effect.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterMiscExactOperation:
    handoff: Plan2NativeCatalogMiscExactHandoff

    def __post_init__(self) -> None:
        if not isinstance(self.handoff, Plan2NativeCatalogMiscExactHandoff):
            raise TypeError("handoff must be a typed misc-exact occurrence")

    @property
    def effect_id(self) -> str:
        return self.handoff.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterTriggeredEffectExecutorOperation:
    handoff: Plan2NativeTriggeredEffectExecutorHandoff

    def __post_init__(self) -> None:
        if not isinstance(
            self.handoff, Plan2NativeTriggeredEffectExecutorHandoff
        ):
            raise TypeError("handoff must be a typed triggered-effect executor")

    @property
    def effect_id(self) -> str:
        return self.handoff.effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterTriggeredStatusEnchantOperation:
    program: Plan2TriggeredStatusEnchantProgram
    start_play_program: Plan2StartPlayStatusProgram | None = None
    end_turn_program: Plan2EndTurnProgram | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2TriggeredStatusEnchantProgram):
            raise TypeError("program must be a typed triggered StatusEnchant program")
        routes = sum(
            value is not None
            for value in (self.start_play_program, self.end_turn_program)
        )
        expected = 0 if self.program.owner_kind == "local-start-turn-review-value-multiple" else 1
        if routes != expected:
            raise ValueError("triggered StatusEnchant owner route mismatch")
        if (
            self.start_play_program is not None
            and self.program.owner_kind != "start-play-card-draw"
        ):
            raise ValueError("unexpected StartPlay owner")
        if (
            self.end_turn_program is not None
            and self.program.owner_kind != "end-turn-review"
        ):
            raise ValueError("unexpected EndTurn owner")

    @property
    def effect_id(self) -> str:
        return self.program.installer_effect_id


@dataclass(frozen=True, slots=True)
class Plan2MasterCardMoveOtherOperation:
    program: Plan2NativeCatalogCardMoveOtherVersion

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeCatalogCardMoveOtherVersion):
            raise TypeError("program must be a typed CardMove-other version")

    @property
    def effect_id(self) -> str:
        return self.program.target_effect_id


def _replace_universe_runtimes(
    state: NativeOrderedZoneState,
    updates: Mapping[str, NativeOrderedCardInstance],
) -> tuple[NativeOrderedCardInstance, ...]:
    unknown = set(updates) - {card.guid for card in state.card_universe}
    if unknown:
        raise NativeOrderedZoneError(
            "zone-runtime-update-guid-missing",
            sorted(unknown)[0],
        )
    return tuple(updates.get(card.guid, card) for card in state.card_universe)


def _set_temporary_upgrade_one(
    card: NativeOrderedCardInstance,
) -> NativeOrderedCardInstance:
    target_effective = card.effective_upgrade + 1
    if target_effective > 3:
        raise NativeOrderedZoneError(
            "native-upgrade-cap-unresolved",
            card.guid,
        )
    digest = _runtime_state_digest(
        base_upgrade=card.base_upgrade,
        temporary_upgrade=1,
        effective_upgrade=target_effective,
        support_upgrade_ids=card.support_upgrade_ids,
        fixed_deck_order=card.fixed_deck_order,
        runtime_state=card.runtime_state,
    )
    return replace(
        card,
        temporary_upgrade=1,
        effective_upgrade=target_effective,
        runtime_state_digest=digest,
    )


@dataclass(frozen=True, slots=True)
class Plan2MasterZoneExecutor:
    """Ordered exact effects at Playing, before played-card settlement."""

    card_id: str
    upgrade: int
    operations: tuple[Plan2MasterZoneOperation, ...]

    def __post_init__(self) -> None:
        _nonempty(self.card_id, "card_id")
        _plain_int(self.upgrade, "upgrade", minimum=0)
        operations = tuple(self.operations)
        if not operations or any(
            not isinstance(
                operation,
                (
                    Plan2MasterHandGraveDrawOperation,
                    Plan2MasterCardMoveOperation,
                    Plan2MasterCardUpgradeOperation,
                ),
            )
            for operation in operations
        ):
            raise TypeError("operations contain an unsupported zone operation")
        object.__setattr__(self, "operations", operations)

    def __call__(
        self,
        state: NativeOrderedZoneState,
        hand_limit: int,
        *,
        candidate_snapshot: NativeOrderedZoneState | None = None,
    ) -> tuple[NativeOrderedZoneState, tuple[str, ...]]:
        if not isinstance(state, NativeOrderedZoneState):
            raise TypeError("state must be NativeOrderedZoneState")
        if candidate_snapshot is not None and not isinstance(
            candidate_snapshot, NativeOrderedZoneState
        ):
            raise TypeError(
                "candidate_snapshot must be NativeOrderedZoneState or None"
            )
        _plain_int(hand_limit, "hand_limit", minimum=1)
        if state.pending_played is None:
            raise NativeOrderedZoneError(
                "native-zone-playing-boundary-missing",
                f"{self.card_id}@{self.upgrade}",
            )
        if len(state.hand) > hand_limit:
            raise NativeOrderedZoneError(
                "hand-limit-invariant",
                f"hand={len(state.hand)};limit={hand_limit}",
            )

        after = state
        trace: list[str] = []
        for operation in self.operations:
            if isinstance(operation, Plan2MasterHandGraveDrawOperation):
                snapshot = after.hand
                discarded = tuple(card.reset_support_upgrade() for card in snapshot)
                updates = {card.guid: card for card in discarded}
                after_discard = replace(
                    after,
                    card_universe=_replace_universe_runtimes(after, updates),
                    hand=(),
                    grave=(*after.grave, *discarded),
                )
                draw = after_discard.draw_to_hand(len(snapshot))
                after = draw.state
                trace.extend(
                    (
                        f"hand-grave-draw:discard:{','.join(card.guid for card in snapshot)}",
                        f"hand-grave-draw:draw:{','.join(draw.drawn_guids)}",
                        f"hand-grave-draw:shuffle-grave:{draw.recycled_grave_count}",
                        f"hand-grave-draw:rng:{draw.random_state_before}->{after.random_state}",
                    )
                )
                continue

            if isinstance(operation, Plan2MasterCardMoveOperation):
                candidates: list[tuple[str, int, NativeOrderedCardInstance]] = []
                for zone_name, zone in (("deck", after.deck), ("grave", after.grave)):
                    for index, card in enumerate(zone):
                        matches, reason = match_plan3_card_move_search(
                            operation.search,
                            card.card_id,
                            card.effective_upgrade,
                        )
                        if reason is not None:
                            raise NativeOrderedZoneError(reason, operation.search.id)
                        if matches:
                            candidates.append((zone_name, index, card))
                # Native CardMove does not enter its random count/key path
                # when the collection search is empty.  Keeping the cursor
                # untouched here is important because the simulator's
                # unconditional count roll would otherwise desynchronize all
                # following random operations.
                random_state = after.random_state
                if candidates:
                    count, random_state = next_range(
                        random_state,
                        operation.contract.count_min,
                        operation.contract.count_max + 1,
                    )
                    keyed: list[
                        tuple[int, int, tuple[str, int, NativeOrderedCardInstance]]
                    ] = []
                    for index, candidate in enumerate(candidates):
                        key, random_state = next_int32(random_state)
                        keyed.append((key, index, candidate))
                    picked = sorted(keyed, key=lambda value: (value[0], value[1]))[
                        : min(count, len(keyed))
                    ]
                    selected_indices = {index for _key, index, _candidate in picked}
                    selected = tuple(
                        candidate
                        for index, candidate in enumerate(candidates)
                        if index in selected_indices
                    )
                else:
                    selected = ()
                selected_guids = {card.guid for _zone, _index, card in selected}
                if any(card.support_upgrade_ids for _zone, _index, card in selected):
                    raise NativeOrderedZoneError(
                        "card-move-support-upgrade-outside-hand-hold",
                        selected[0][2].guid,
                    )
                moved = tuple(card for _zone, _index, card in selected)
                after = replace(
                    after,
                    random_state=random_state,
                    deck=tuple(
                        card for card in after.deck if card.guid not in selected_guids
                    ),
                    grave=tuple(
                        card for card in after.grave if card.guid not in selected_guids
                    ),
                    lost=(*after.lost, *moved),
                )
                trace.extend(
                    (
                        f"card-move:lost:{','.join(card.guid for card in moved)}",
                        f"card-move:rng:{state.random_state}->{random_state}",
                    )
                )
                continue

            master_refs = set(operation.master_card_refs)
            search_state = after if candidate_snapshot is None else candidate_snapshot
            candidates: list[NativeOrderedCardInstance] = []
            for card in search_state.hand:
                if (card.card_id, card.effective_upgrade) not in master_refs:
                    raise NativeOrderedZoneError(
                        "card-master-missing",
                        f"{card.card_id}@{card.effective_upgrade}",
                    )
                # The native search list is materialized before upgrade
                # application and contains every Hand search match.  The
                # supported contract is PickRangeType_All: it preserves this
                # order and does not commit candidate keys to the shared exam
                # RNG.  Random selectors use their separate proven paths.
                candidates.append(card)
            candidate_guids = {card.guid for card in candidates}
            updates: dict[str, NativeOrderedCardInstance] = {}
            for card in after.hand:
                if (card.card_id, card.effective_upgrade) not in master_refs:
                    raise NativeOrderedZoneError(
                        "card-master-missing",
                        f"{card.card_id}@{card.effective_upgrade}",
                    )
                if card.guid not in candidate_guids:
                    continue
                # Native IsUpgradableRaw admits only raw base+temporary zero
                # and only when the +1 Master exists.  Support layers remain
                # attached and therefore contribute to target effective.
                if card.base_upgrade + card.temporary_upgrade != 0:
                    continue
                if (card.card_id, 1) not in master_refs:
                    continue
                target_effective = card.effective_upgrade + 1
                if (card.card_id, target_effective) not in master_refs:
                    raise NativeOrderedZoneError(
                        "target-master-or-lineage-unresolved",
                        f"{card.card_id}@{target_effective}",
                    )
                updates[card.guid] = _set_temporary_upgrade_one(card)
            random_state_before = after.random_state
            if updates:
                after = replace(
                    after,
                    card_universe=_replace_universe_runtimes(after, updates),
                    hand=tuple(updates.get(card.guid, card) for card in after.hand),
                )
            trace.append("card-upgrade:hand-all:" + ",".join(updates))
            trace.append(
                f"card-upgrade:rng:{random_state_before}->{after.random_state}"
            )
        return after, tuple(trace)


Plan2MasterPlayingOperation = (
    Plan2MasterDirectOperation
    | Plan2MasterZoneOperation
    | Plan2MasterStaminaOperation
    | Plan2MasterStatusEnchantOperation
    | Plan2MasterReviewMultipleOperation
    | Plan2MasterDynamicBlockOperation
    | Plan2MasterLessonDependAggressiveOperation
    | Plan2MasterRuntimeLessonDependAggressiveOperation
    | Plan2MasterReviewDynamicOperation
    | Plan2MasterDebuffOperation
    | Plan2MasterStatusEnchantEncoreOperation
    | Plan2MasterEffectChainOperation
    | Plan2MasterTriggeredOperation
    | Plan2MasterGeneratedCreateOperation
    | Plan2MasterGeneratedBuffForceOperation
    | Plan2MasterGeneratedForceSettlementOperation
    | Plan2MasterAggressiveAdditiveOperation
    | Plan2MasterBlockFixRestrictionOperation
    | Plan2MasterExtraTurnOperation
    | Plan2MasterStaminaRecoverMultipleOperation
    | Plan2MasterLessonDependBlockRetentionOperation
    | Plan2MasterMiscExactOperation
    | Plan2MasterTriggeredEffectExecutorOperation
    | Plan2MasterTriggeredStatusEnchantOperation
    | Plan2MasterCardMoveOtherOperation
    | Plan2NativeQueuedAction
)


def _effect_trigger_search_snapshot(
    zones: NativeOrderedZoneState,
    categories: Mapping[tuple[str, int], str],
) -> Plan2RemainingSearchZoneSnapshot:
    def cards(values: Sequence[NativeOrderedCardInstance]) -> tuple[Plan2RemainingSearchCardSnapshot, ...]:
        result: list[Plan2RemainingSearchCardSnapshot] = []
        for value in values:
            category = categories.get((value.card_id, value.effective_upgrade))
            if category is None:
                raise ValueError(
                    "effect-trigger-card-category-missing:"
                    f"{value.card_id}@{value.effective_upgrade}"
                )
            result.append(Plan2RemainingSearchCardSnapshot(value.guid, category))
        return tuple(result)

    return Plan2RemainingSearchZoneSnapshot(
        hand=cards(zones.hand),
        deck=cards(zones.deck),
        grave=cards(zones.grave),
        lost=cards(zones.lost),
        hold=(),
        playing=(
            None
            if zones.pending_played is None
            else cards((zones.pending_played,))[0]
        ),
        future_decks=(),
        past_decks=(),
        complete=True,
    )


def _plan3_state_at_playing(
    zones: NativeOrderedZoneState,
    *,
    include_pending_in_hand: bool = False,
) -> Plan3NativeState:
    pending = (
        ()
        if not include_pending_in_hand or zones.pending_played is None
        else (_plan3_card_from_ordered(zones.pending_played),)
    )
    return Plan3NativeState(
        hand=(
            *(tuple(_plan3_card_from_ordered(value) for value in zones.hand)),
            *pending,
        ),
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


def _add_grow_state_at_playing(
    zones: NativeOrderedZoneState,
    source_program: Plan2AddGrowProgram,
    direct_review_refs: frozenset[tuple[str, int]],
    previous: Plan2AddGrowDeckAllState | None,
) -> Plan2AddGrowDeckAllState:
    if zones.pending_played is None:
        raise ValueError("add-grow requires the Playing card")
    prior_by_guid = (
        {}
        if previous is None
        else {value.card.guid: value.card for value in previous.native_locations}
    )

    def card(value: NativeOrderedCardInstance, *, playing: bool = False) -> Plan2AddGrowCardInstance:
        prior = prior_by_guid.get(value.guid)
        if playing:
            effects = source_program.play_effects
        elif (value.card_id, value.effective_upgrade) in direct_review_refs:
            effects = (
                Plan2EndTurnEffect(
                    effect_id=f"central-review-marker:{value.card_id}@{value.effective_upgrade}",
                    effect_type=_EFFECT_REVIEW,
                    value1=0,
                    value2=0,
                    count=0,
                    turn=0,
                ),
            )
        else:
            effects = ()
        return Plan2AddGrowCardInstance(
            guid=value.guid,
            card_id=value.card_id,
            play_effects=effects,
            review_grow=None if prior is None else prior.review_grow,
            applied_grow_effect_ids=(
                () if prior is None else prior.applied_grow_effect_ids
            ),
            cache_reset_count=0 if prior is None else prior.cache_reset_count,
            on_change_count=0 if prior is None else prior.on_change_count,
        )

    return Plan2AddGrowDeckAllState(
        hand=tuple(card(value) for value in zones.hand),
        deck=tuple(card(value) for value in zones.deck),
        grave=tuple(card(value) for value in zones.grave),
        lost=tuple(card(value) for value in zones.lost),
        playing=card(zones.pending_played, playing=True),
    )
def _ordered_generated_card(card: object) -> NativeOrderedCardInstance:
    runtime = replace(
        empty_local_save_exam_card_runtime_state(),
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


def _ordered_after_generated_create(
    zones: NativeOrderedZoneState,
    native: Plan3NativeState,
) -> NativeOrderedZoneState:
    by_guid = {value.guid: value for value in zones.card_universe}

    def restore(values: Sequence[object]) -> tuple[NativeOrderedCardInstance, ...]:
        result: list[NativeOrderedCardInstance] = []
        for value in values:
            current = by_guid.get(value.guid)
            result.append(current if current is not None else _ordered_generated_card(value))
        return tuple(result)

    hand = restore(native.hand)
    deck = restore(native.deck)
    grave = restore(native.grave)
    lost = restore(native.lost)
    universe = tuple(
        sorted(
            (*hand, *deck, *grave, *lost, zones.pending_played),
            key=lambda value: value.guid,
        )
    )
    return replace(
        zones,
        random_state=native.random_state,
        card_universe=universe,
        hand=hand,
        deck=deck,
        grave=grave,
        lost=lost,
    )


def _remaining_card_move_state_at_playing(
    zones: NativeOrderedZoneState,
) -> RemainingCardMoveState:
    return RemainingCardMoveState(
        hand=tuple(_plan3_card_from_ordered(value) for value in zones.hand),
        deck=tuple(_plan3_card_from_ordered(value) for value in zones.deck),
        grave=tuple(_plan3_card_from_ordered(value) for value in zones.grave),
        lost=tuple(_plan3_card_from_ordered(value) for value in zones.lost),
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


def _ordered_after_remaining_card_move(
    zones: NativeOrderedZoneState,
    remaining: RemainingCardMoveState,
) -> NativeOrderedZoneState:
    if remaining.hold or remaining.future_decks or remaining.past_decks:
        raise ValueError("card-move-other-extended-zone-unbound")
    by_guid = {value.guid: value for value in zones.card_universe}
    after_cards = remaining.all_cards
    if {value.guid for value in after_cards} != set(by_guid):
        raise ValueError("card-move-other-card-universe-drift")

    def restore(values: Sequence[object]) -> tuple[NativeOrderedCardInstance, ...]:
        try:
            return tuple(by_guid[value.guid] for value in values)
        except KeyError as error:
            raise ValueError("card-move-other-guid-rematch-failed") from error

    pending = (
        None
        if remaining.playing is None
        else by_guid.get(remaining.playing.guid)
    )
    if pending != zones.pending_played:
        raise ValueError("card-move-other-playing-boundary-changed")
    return replace(
        zones,
        hand=restore(remaining.hand),
        deck=restore(remaining.deck),
        grave=restore(remaining.grave),
        lost=restore(remaining.lost),
        pending_played=pending,
        random_state=remaining.random_state,
    )


def _evaluate_effect_trigger_at_build(
    handoff: Plan2NativeEffectTriggerHandoff | Plan2RemainingEffectTriggerHandoff,
    scalar: Plan2State,
    zones: NativeOrderedZoneState,
    *,
    play_origin: str,
    categories: Mapping[tuple[str, int], str],
) -> bool:
    origin = "normal" if play_origin == "normal" else play_origin
    if isinstance(handoff, Plan2RemainingEffectTriggerHandoff):
        snapshot = Plan2RemainingEffectTriggerSnapshot(
            phase=handoff.base.trigger.phase_types[0],
            boundary=handoff.supported_boundaries[0],
            origin=origin,
            aggressive=scalar.card_play_aggressive,
            review=scalar.review,
            block=scalar.block,
            search_zones=_effect_trigger_search_snapshot(zones, categories),
            payment_committed_before_build=False,
            direct_effects_executed_before_build=0,
        )
        evaluated = evaluate_plan2_native_catalog_effect_trigger_remaining(
            handoff,
            snapshot,
        )
    else:
        preferred = (
            EffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT,
            EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
            EffectTriggerBoundary.DIRECT_EFFECT_SEQUENCE_BUILD,
            EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD,
        )
        boundary = next(
            (value for value in preferred if value in handoff.supported_boundaries),
            None,
        )
        if boundary is None:
            raise ValueError(
                "effect-trigger-boundary-unbound:"
                f"{handoff.trigger.trigger_id}"
            )
        snapshot = Plan2NativeEffectTriggerSnapshot(
            phase=handoff.trigger.phase_types[0],
            boundary=boundary,
            origin=origin,
            aggressive=scalar.card_play_aggressive,
            current_stamina=scalar.stamina,
            max_stamina=scalar.max_stamina,
            review=scalar.review,
            block=scalar.block,
            global_card_play_count=scalar.exam_card_play_count,
            turn_card_play_count=scalar.turn_card_play_count,
            payment_committed_before_build=(
                boundary is EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD
            ),
            direct_effects_executed_before_build=0,
        )
        evaluated = evaluate_plan2_native_effect_trigger(handoff, snapshot)
    if evaluated.fail_closed or type(evaluated.fires) is not bool:
        raise ValueError(
            "effect-trigger-failed-closed:"
            f"{handoff.effect_id}:{','.join(evaluated.reasons)}"
        )
    return evaluated.fires


@dataclass(frozen=True, slots=True)
class Plan2MasterPlayingExecutor:
    """Replay one mixed Master effect list in exact order at Playing."""

    card_id: str
    upgrade: int
    operations: tuple[Plan2MasterPlayingOperation, ...]
    stamina_catalog: Plan2NativeCatalogStamina | None = None
    debuff_catalog: Plan2NativeCatalogDebuff | None = None
    encore_catalog: Plan2NativeStatusEnchantEncoreCatalog | None = None
    effect_chain_catalog: Plan2NativeEffectChainCatalog | None = None
    aggressive_additive_catalog: Plan2NativeAggressiveAdditiveCatalog | None = None
    block_fix_restriction_catalog: Plan2NativeBlockFixRestrictionCatalog | None = None
    extra_turn_catalog: Plan2NativeExtraTurnCatalog | None = None
    stamina_recover_multiple_catalog: Plan2NativeStaminaRecoverMultipleCatalog | None = None
    misc_exact_catalog: Plan2NativeCatalogMiscExactCatalog | None = None
    status_child_compilation: Plan2NativeStatusChildCompilation | None = None
    master_card_categories: tuple[tuple[str, int, str], ...] = ()
    direct_review_refs: tuple[tuple[str, int], ...] = ()
    operation_effect_ids: tuple[str, ...] = ()
    operation_once_flags: tuple[bool, ...] = ()
    play_count_buff_search_ids: tuple[str, ...] = ()
    runtime_customization_database: str = ""
    runtime_customization_contract: Plan3NativeRuntimeCustomization | None = None
    runtime_customization_trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.card_id, "card_id")
        _plain_int(self.upgrade, "upgrade", minimum=0)
        operations = tuple(self.operations)
        if not operations or any(
            not isinstance(
                operation,
                (
                    Plan2MasterScalarOperation,
                    Plan2MasterRemainingThreeInstallOperation,
                    Plan2MasterHandGraveDrawOperation,
                    Plan2MasterCardMoveOperation,
                    Plan2MasterCardUpgradeOperation,
                    Plan2MasterStaminaOperation,
                    Plan2MasterStatusEnchantOperation,
                    Plan2MasterReviewMultipleOperation,
                    Plan2MasterDynamicBlockOperation,
                    Plan2MasterLessonDependAggressiveOperation,
                    Plan2MasterRuntimeLessonDependAggressiveOperation,
                    Plan2MasterReviewDynamicOperation,
                    Plan2MasterDebuffOperation,
                    Plan2MasterStatusEnchantEncoreOperation,
                    Plan2MasterEffectChainOperation,
                    Plan2MasterTriggeredOperation,
                    Plan2MasterGeneratedCreateOperation,
                    Plan2MasterGeneratedBuffForceOperation,
                    Plan2MasterGeneratedForceSettlementOperation,
                    Plan2MasterAggressiveAdditiveOperation,
                    Plan2MasterBlockFixRestrictionOperation,
                    Plan2MasterExtraTurnOperation,
                    Plan2MasterStaminaRecoverMultipleOperation,
                    Plan2MasterLessonDependBlockRetentionOperation,
                    Plan2MasterMiscExactOperation,
                    Plan2MasterTriggeredEffectExecutorOperation,
                    Plan2MasterTriggeredStatusEnchantOperation,
                    Plan2MasterCardMoveOtherOperation,
                    Plan2NativeQueuedAction,
                ),
            )
            for operation in operations
        ):
            raise TypeError("operations contain an unsupported Playing operation")
        for operation in operations:
            checked = (
                operation.operation
                if isinstance(operation, Plan2MasterTriggeredOperation)
                else operation
            )
            if isinstance(checked, Plan2NativeQueuedAction) and checked.kind not in {
                "draw",
                "add_playable",
            }:
                raise ValueError(
                    f"unsupported Playing queued operation: {checked.kind}"
                )
        object.__setattr__(self, "operations", operations)
        categories = tuple(self.master_card_categories)
        if categories != tuple(sorted(set(categories))):
            raise ValueError("master_card_categories must be sorted and unique")
        object.__setattr__(self, "master_card_categories", categories)
        direct_review_refs = tuple(self.direct_review_refs)
        if direct_review_refs != tuple(sorted(set(direct_review_refs))):
            raise ValueError("direct_review_refs must be sorted and unique")
        object.__setattr__(self, "direct_review_refs", direct_review_refs)
        effect_ids = tuple(self.operation_effect_ids)
        if effect_ids and (
            len(effect_ids) != len(operations)
            or any(not isinstance(value, str) or not value for value in effect_ids)
        ):
            raise ValueError("operation_effect_ids must align with operations")
        object.__setattr__(self, "operation_effect_ids", effect_ids)
        once_flags = tuple(self.operation_once_flags)
        if once_flags and (
            len(once_flags) != len(operations)
            or any(type(value) is not bool for value in once_flags)
        ):
            raise ValueError("operation_once_flags must align with operations")
        object.__setattr__(self, "operation_once_flags", once_flags)
        search_ids = tuple(self.play_count_buff_search_ids)
        if (
            any(not isinstance(value, str) or not value for value in search_ids)
            or len(set(search_ids)) != len(search_ids)
        ):
            raise ValueError(
                "play_count_buff_search_ids must be unique non-empty text"
            )
        object.__setattr__(self, "play_count_buff_search_ids", search_ids)
        if not isinstance(self.runtime_customization_database, str):
            raise TypeError("runtime_customization_database must be text")
        if self.runtime_customization_contract is not None and not isinstance(
            self.runtime_customization_contract,
            Plan3NativeRuntimeCustomization,
        ):
            raise TypeError(
                "runtime_customization_contract must be typed or None"
            )
        customization_trace = tuple(self.runtime_customization_trace)
        if any(
            not isinstance(value, str) or not value
            for value in customization_trace
        ):
            raise ValueError(
                "runtime_customization_trace must contain non-empty text"
            )
        if self.runtime_customization_contract is None and customization_trace:
            raise ValueError(
                "runtime customization trace requires a bound contract"
            )
        object.__setattr__(self, "runtime_customization_trace", customization_trace)
        if self.stamina_catalog is not None and not isinstance(
            self.stamina_catalog, Plan2NativeCatalogStamina
        ):
            raise TypeError("stamina_catalog must be Plan2NativeCatalogStamina or None")
        unwrapped = tuple(
            value.operation if isinstance(value, Plan2MasterTriggeredOperation) else value
            for value in operations
        )
        if any(isinstance(value, Plan2MasterStaminaOperation) for value in unwrapped):
            if self.stamina_catalog is None:
                raise ValueError("stamina operations require a stamina catalog")
        if any(isinstance(value, Plan2MasterDebuffOperation) for value in unwrapped):
            if self.debuff_catalog is None:
                raise ValueError("debuff operations require a debuff catalog")
        if any(
            isinstance(value, Plan2MasterStatusEnchantEncoreOperation)
            for value in unwrapped
        ):
            if self.encore_catalog is None:
                raise ValueError("Encore operations require an Encore catalog")
        if any(isinstance(value, Plan2MasterEffectChainOperation) for value in unwrapped):
            if self.effect_chain_catalog is None:
                raise ValueError("effect-chain operations require an effect-chain catalog")
        if any(
            isinstance(value, Plan2MasterAggressiveAdditiveOperation)
            for value in unwrapped
        ):
            if self.aggressive_additive_catalog is None:
                raise ValueError(
                    "aggressive-additive operations require an aggressive-additive catalog"
                )

        if any(isinstance(value, Plan2MasterMiscExactOperation) for value in unwrapped):
            if self.misc_exact_catalog is None:
                raise ValueError("misc-exact operations require their catalog")
        if any(isinstance(value, Plan2MasterExtraTurnOperation) for value in unwrapped):
            if self.extra_turn_catalog is None:
                raise ValueError("extra-turn operations require an extra-turn catalog")
        if any(
            isinstance(value, Plan2MasterStaminaRecoverMultipleOperation)
            for value in unwrapped
        ):
            if self.stamina_recover_multiple_catalog is None:
                raise ValueError(
                    "stamina-recover-multiple operations require their catalog"
                )
        if any(
            isinstance(value, Plan2MasterBlockFixRestrictionOperation)
            for value in unwrapped
        ):
            if self.block_fix_restriction_catalog is None:
                raise ValueError(
                    "block fix/restriction operations require their catalog"
                )

    def materialize_runtime_customization(
        self,
        runtime: LocalSaveExamCardRuntimeState,
        *,
        database: Path | None = None,
    ) -> "Plan2MasterPlayingExecutor":
        """Bind one card instance's complete customization before legality.

        The returned executor owns the exact effective operation sequence,
        effect identity and trace.  Playability, payment, Playing execution,
        and retained-history recovery therefore validate the same contract.
        """

        if not isinstance(runtime, LocalSaveExamCardRuntimeState):
            raise TypeError("runtime must be LocalSaveExamCardRuntimeState")
        if self.runtime_customization_contract is not None:
            return self
        database_path = (
            Path(database)
            if database is not None
            else Path(self.runtime_customization_database)
            if self.runtime_customization_database
            else DEFAULT_DATABASE
        )
        customization = parse_plan2_native_runtime_customization(
            self.card_id,
            self.upgrade,
            runtime,
            database=database_path,
        )
        operations, trace = apply_plan2_runtime_customization_operations(
            self.operations,
            customization,
            database=database_path,
        )
        added_effect_ids = tuple(
            entry.added_effect.id
            for entry in customization.effects
            if entry.added_effect is not None
        )
        effect_ids = (
            *(self.operation_effect_ids or tuple(
                getattr(value, "effect_id", f"operation-{index}")
                for index, value in enumerate(self.operations)
            )),
            *added_effect_ids,
        )
        once_flags = (
            *(self.operation_once_flags or tuple(False for _ in self.operations)),
            *(False for _ in added_effect_ids),
        )
        return replace(
            self,
            operations=operations,
            operation_effect_ids=effect_ids,
            operation_once_flags=once_flags,
            runtime_customization_contract=customization,
            runtime_customization_trace=trace,
        )

    def __call__(
        self,
        scalar: Plan2State,
        zones: NativeOrderedZoneState,
        hand_limit: int,
        stamina_modifiers: Plan2StaminaModifierRuntime,
        status_enchant: Plan2NativeStatusEnchantRuntime,
        review_dynamic: Plan2NativeReviewDynamicRuntime,
        debuff_registry: Plan2NativeDebuffRegistry,
        effect_chains: Plan2NativeEffectChainRuntime,
        trigger_snapshot_scalar: Plan2State,
        generated_runtime: PlayCountBuffRuntime,
        generated_input: Plan2NativeGeneratedAllocatorInput | None,
        encore_runtime: Plan2NativeStatusEnchantEncoreRuntime | None,
        review_consumption_sum: int,
        block_consumption_sum_count: int,
        source_guid: str,
        play_origin: str,
        remaining_turns: int,
        replay_ancestry: tuple[tuple[int, str], ...],
        status_child_context: object | None = None,
        aggressive_additive_runtime: Plan2AggressiveAdditiveRuntime | None = None,
        block_fix_restriction_runtime: Plan2NativeBlockFixRestrictionRuntime | None = None,
        extra_turn_runtime: Plan2NativeExtraTurnRuntime | None = None,
        stamina_recover_restricted: bool = False,
        stamina_recover_add_permil: int | None = None,
        search_play_card_stamina_runtime: SearchPlayCardStaminaRuntime = SearchPlayCardStaminaRuntime(),
        add_grow_runtime: Plan2AddGrowDeckAllState | None = None,
        start_play_listeners: tuple[Plan2StartPlayListener, ...] = (),
        triggered_review_status_runtime: Plan2TriggeredReviewStatusRuntime = Plan2TriggeredReviewStatusRuntime(),
        item_runtime: Plan2NativeItemRuntime = Plan2NativeItemRuntime(),
        item_lesson_type: str = "ProduceStepLessonType_Unknown",
    ) -> Plan2NativePlayingEffectResult:
        if not isinstance(scalar, Plan2State):
            raise TypeError("scalar must be Plan2State")
        if not isinstance(zones, NativeOrderedZoneState):
            raise TypeError("zones must be NativeOrderedZoneState")
        if zones.pending_played is None:
            raise NativeOrderedZoneError(
                "native-zone-playing-boundary-missing",
                f"{self.card_id}@{self.upgrade}",
            )
        _plain_int(hand_limit, "hand_limit", minimum=1)
        if not isinstance(stamina_modifiers, Plan2StaminaModifierRuntime):
            raise TypeError("stamina_modifiers must be Plan2StaminaModifierRuntime")
        if not isinstance(status_enchant, Plan2NativeStatusEnchantRuntime):
            raise TypeError("status_enchant must be Plan2NativeStatusEnchantRuntime")
        if not isinstance(review_dynamic, Plan2NativeReviewDynamicRuntime):
            raise TypeError("review_dynamic must be Plan2NativeReviewDynamicRuntime")
        if not isinstance(debuff_registry, Plan2NativeDebuffRegistry):
            raise TypeError("debuff_registry must be Plan2NativeDebuffRegistry")
        if not isinstance(effect_chains, Plan2NativeEffectChainRuntime):
            raise TypeError("effect_chains must be Plan2NativeEffectChainRuntime")
        if not isinstance(trigger_snapshot_scalar, Plan2State):
            raise TypeError("trigger_snapshot_scalar must be Plan2State")
        if not isinstance(generated_runtime, PlayCountBuffRuntime):
            raise TypeError("generated_runtime must be PlayCountBuffRuntime")
        if generated_input is not None and not isinstance(
            generated_input,
            Plan2NativeGeneratedAllocatorInput,
        ):
            raise TypeError("generated_input must be typed or None")
        if encore_runtime is not None and not isinstance(
            encore_runtime,
            Plan2NativeStatusEnchantEncoreRuntime,
        ):
            raise TypeError("encore_runtime must be Encore runtime or None")
        if aggressive_additive_runtime is not None and not isinstance(
            aggressive_additive_runtime,
            Plan2AggressiveAdditiveRuntime,
        ):
            raise TypeError("aggressive_additive_runtime must be typed or None")
        if block_fix_restriction_runtime is not None and not isinstance(
            block_fix_restriction_runtime,
            Plan2NativeBlockFixRestrictionRuntime,
        ):
            raise TypeError("block_fix_restriction_runtime must be typed or None")
        if extra_turn_runtime is not None and not isinstance(
            extra_turn_runtime,
            Plan2NativeExtraTurnRuntime,
        ):
            raise TypeError("extra_turn_runtime must be typed or None")
        if not isinstance(stamina_recover_restricted, bool):
            raise TypeError("stamina_recover_restricted must be boolean")
        if stamina_recover_add_permil is not None:
            _plain_int(stamina_recover_add_permil, "stamina_recover_add_permil")
        if not isinstance(
            search_play_card_stamina_runtime,
            SearchPlayCardStaminaRuntime,
        ):
            raise TypeError("search_play_card_stamina_runtime must be typed")
        if add_grow_runtime is not None and not isinstance(
            add_grow_runtime,
            Plan2AddGrowDeckAllState,
        ):
            raise TypeError("add_grow_runtime must be typed or None")
        start_play_listeners = tuple(start_play_listeners)
        if any(
            not isinstance(value, Plan2StartPlayListener)
            for value in start_play_listeners
        ):
            raise TypeError("start_play_listeners must contain typed listeners")
        if not isinstance(
            triggered_review_status_runtime,
            Plan2TriggeredReviewStatusRuntime,
        ):
            raise TypeError("triggered_review_status_runtime must be typed")
        if not isinstance(item_runtime, Plan2NativeItemRuntime):
            raise TypeError("item_runtime must be Plan2NativeItemRuntime")
        if not isinstance(item_lesson_type, str) or not item_lesson_type:
            raise TypeError("item_lesson_type must be non-empty text")
        _plain_int(review_consumption_sum, "review_consumption_sum", minimum=0)
        _plain_int(
            block_consumption_sum_count,
            "block_consumption_sum_count",
            minimum=0,
        )
        _nonempty(source_guid, "source_guid")
        if play_origin not in {"normal", "forced", "extra"}:
            raise ValueError("unsupported play_origin")
        _plain_int(remaining_turns, "remaining_turns", minimum=0)
        if not isinstance(replay_ancestry, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or isinstance(item[0], bool)
            or not isinstance(item[0], int)
            or not isinstance(item[1], str)
            or not item[1]
            for item in replay_ancestry
        ):
            raise TypeError("replay_ancestry has an invalid shape")

        after_scalar = scalar
        after_zones = zones
        after_stamina = stamina_modifiers
        after_review_dynamic = review_dynamic
        after_debuff_registry = debuff_registry
        after_effect_chains = effect_chains
        after_generated_runtime = generated_runtime
        after_encore_runtime = encore_runtime
        after_aggressive_additive_runtime = aggressive_additive_runtime
        def dispatch_plan2_native_status_enchant_event(*args, **kwargs):
            # Status children and direct card operations share the current
            # additive owner, including a layer installed earlier in this
            # same ordered Playing transaction.
            nonlocal after_aggressive_additive_runtime
            context = kwargs.get("status_child_context")
            if context is not None:
                context.aggressive_additive_runtime = after_aggressive_additive_runtime
            result = _dispatch_plan2_native_status_enchant_event(*args, **kwargs)
            if context is not None and context.aggressive_additive_runtime is not None:
                after_aggressive_additive_runtime = replace(context.aggressive_additive_runtime,
                    aggressive=result[0].card_play_aggressive)
            return result
        after_block_fix_restriction_runtime = block_fix_restriction_runtime
        after_extra_turn_runtime = extra_turn_runtime
        after_search_play_card_stamina_runtime = search_play_card_stamina_runtime
        after_add_grow_runtime = add_grow_runtime
        after_start_play_listeners = start_play_listeners
        after_triggered_review_status_runtime = triggered_review_status_runtime
        after_item_runtime = item_runtime
        try:
            self = self.materialize_runtime_customization(
                zones.pending_played.runtime_state,
                database=(
                    Path(self.runtime_customization_database)
                    if self.runtime_customization_database
                    else DEFAULT_DATABASE
                ),
            )
        except Plan3NativeStateError as error:
            detail = f"{error.code}:{error.detail}".rstrip(":")
            raise ValueError(
                f"runtime-customization-failed-closed:{detail}"
            ) from error
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"runtime-customization-failed-closed:{error}"
            ) from error
        customization = self.runtime_customization_contract
        assert customization is not None
        operations = self.operations
        runtime_customization_trace = self.runtime_customization_trace
        runtime_customization_materialized = bool(customization.customize_ids)
        owns_aggressive_additive_operation = any(
            isinstance(
                (
                    operation.operation
                    if isinstance(operation, Plan2MasterTriggeredOperation)
                    else operation
                ),
                Plan2MasterAggressiveAdditiveOperation,
            )
            for operation in operations
        )
        if self.aggressive_additive_catalog is not None and (
            after_aggressive_additive_runtime is not None
            or owns_aggressive_additive_operation
        ):
            if after_aggressive_additive_runtime is None:
                after_aggressive_additive_runtime = Plan2AggressiveAdditiveRuntime.empty(
                    self.aggressive_additive_catalog,
                    aggressive=scalar.card_play_aggressive,
                )
            elif (
                after_aggressive_additive_runtime.catalog
                is not self.aggressive_additive_catalog
            ):
                raise ValueError("aggressive-additive-catalog-mismatch")
            elif (
                after_aggressive_additive_runtime.aggressive
                != scalar.card_play_aggressive
            ):
                after_aggressive_additive_runtime = replace(
                    after_aggressive_additive_runtime,
                    aggressive=scalar.card_play_aggressive,
                )
        if self.block_fix_restriction_catalog is not None:
            if after_block_fix_restriction_runtime is None:
                after_block_fix_restriction_runtime = (
                    Plan2NativeBlockFixRestrictionRuntime(
                        block=scalar.block,
                        block_consumption_sum_count=block_consumption_sum_count,
                    )
                )
            elif (
                after_block_fix_restriction_runtime.block != scalar.block
                or after_block_fix_restriction_runtime.block_consumption_sum_count
                != block_consumption_sum_count
            ):
                after_block_fix_restriction_runtime = replace(
                    after_block_fix_restriction_runtime,
                    block=scalar.block,
                    block_consumption_sum_count=block_consumption_sum_count,
                )
        if self.extra_turn_catalog is not None:
            current_extra_turn = (
                0
                if after_extra_turn_runtime is None
                else after_extra_turn_runtime.extra_turn
            )
            after_extra_turn_runtime = Plan2NativeExtraTurnRuntime(
                remaining_turn=remaining_turns,
                extra_turn=current_extra_turn,
                current_turn=scalar.current_turn,
        )
        trigger_zones = zones
        # A Hand-All CardUpgrade normally uses the Playing-boundary search
        # snapshot.  For an uncustomized card, a preceding child in this same
        # ordered effect list (for example Draw followed by Upgrade) exposes
        # a live Hand search.  A typed runtime customization is a native
        # materialization boundary, so its Playing snapshot remains the
        # authority.  This is provenance/ordering, not a card-specific branch;
        # standalone Plan2MasterZoneExecutor callers retain their explicit
        # candidate_snapshot semantics.
        zone_snapshot_changed = False
        zone_search_materialization = (
            "playing-boundary"
            if runtime_customization_materialized
            else "live-after-child"
        )
        trigger_categories = {
            (card_id, upgrade): category
            for card_id, upgrade, category in self.master_card_categories
        }
        if after_review_dynamic.review != after_scalar.review:
            raise ValueError("review-dynamic-scalar-snapshot-mismatch")
        if (
            after_triggered_review_status_runtime.review_runtime
            != after_review_dynamic
        ):
            after_triggered_review_status_runtime = replace(
                after_triggered_review_status_runtime,
                review_runtime=after_review_dynamic,
            )
        after_block_consumption_sum_count = block_consumption_sum_count
        playable_delta = 0
        queued_actions: list[Plan2NativeQueuedAction] = []
        trace: list[str] = []
        trace.extend(runtime_customization_trace)
        trace.append(
            f"zone-search-materialization:{zone_search_materialization}"
        )
        completed_effect_ids: list[str] = []

        def append_debuff_queue(queue: object) -> None:
            differences = tuple(getattr(queue, "differences", ()))
            callbacks = tuple(getattr(queue, "callback_events", ()))
            trace.extend(tuple(getattr(queue, "trace", ())))
            trace.extend(f"debuff-difference:{value!r}" for value in differences)
            trace.extend(f"debuff-callback:{value}" for value in callbacks)

        def gate_status_addition(effect_type: str) -> bool:
            nonlocal after_debuff_registry
            if after_debuff_registry.anti_debuff_count == 0:
                return False
            gate = gate_plan2_native_status_addition(
                after_debuff_registry,
                effect_type,
            )
            if not gate.resolved:
                raise ValueError(
                    "anti-debuff-gate-failed-closed:"
                    f"{effect_type}:{gate.reason}"
                )
            after_debuff_registry = gate.registry_after
            append_debuff_queue(gate.queue_handoff)
            return gate.blocked

        def active_status_uids() -> tuple[int, ...]:
            status_child_review = getattr(
                status_child_context,
                "review_state",
                None,
            )
            return (
                *(value.status_uid for value in after_scalar.review_multiple_layers),
                *(value.status_uid for value in after_scalar.end_turn_listeners),
                *(value.status_uid for value in after_scalar.card_play_listeners),
                *(
                    (after_scalar.stamina_consumption_add_status.status_uid,)
                    if after_scalar.stamina_consumption_add_status is not None
                    else ()
                ),
                *(value.status_uid for value in after_stamina.down_fix_layers),
                *(
                    (after_stamina.down.status_uid,)
                    if after_stamina.down is not None
                    else ()
                ),
                *(
                    (after_stamina.add.layer.status_uid,)
                    if after_stamina.add.layer is not None
                    else ()
                ),
                *(value.status_uid for value in status_enchant.listeners),
                *(value.status_uid for value in after_review_dynamic.layers),
                *(value.uid for value in after_debuff_registry.statuses),
                *(value.status_uid for value in after_effect_chains.queue),
                *(value.native_uid for value in after_generated_runtime.statuses),
                *(
                    (value.status_uid for value in status_child_review.active)
                    if status_child_review is not None
                    else ()
                ),
                *(
                    (
                        value.status_uid
                        for value in status_child_review.review_count_add_layers
                    )
                    if status_child_review is not None
                    else ()
                ),
                *(
                    (value.status_uid for value in after_encore_runtime.listeners)
                    if after_encore_runtime is not None
                    else ()
                ),
                *(
                    after_aggressive_additive_runtime.status_uids
                    if after_aggressive_additive_runtime is not None
                    else ()
                ),
                *(
                    after_block_fix_restriction_runtime.active_status_uids
                    if after_block_fix_restriction_runtime is not None
                    else ()
                ),
                *(
                    value.status_uid
                    for value in after_search_play_card_stamina_runtime.statuses
                ),
                *(value.status_uid for value in after_start_play_listeners),
                *(
                    value.status_uid
                    for value in after_triggered_review_status_runtime.listeners
                ),
                *after_item_runtime.active_status_uids,
            )

        def global_next_status_uid(*floors: int) -> int:
            status_child_review = getattr(
                status_child_context,
                "review_state",
                None,
            )
            active_uids = active_status_uids()
            return max(
                1,
                *floors,
                after_scalar.next_status_uid,
                after_stamina.next_status_uid,
                status_enchant.next_status_uid,
                after_review_dynamic.next_status_uid,
                after_debuff_registry.next_uid,
                after_effect_chains.next_status_uid,
                (
                    status_child_review.plan2_state.next_status_uid
                    if status_child_review is not None
                    else 1
                ),
                (
                    after_encore_runtime.next_status_uid
                    if after_encore_runtime is not None
                    else 1
                ),
                (
                    after_aggressive_additive_runtime.next_status_uid
                    if after_aggressive_additive_runtime is not None
                    else 1
                ),
                (
                    after_block_fix_restriction_runtime.next_status_uid
                    if after_block_fix_restriction_runtime is not None
                    else 1
                ),
                after_search_play_card_stamina_runtime.next_status_uid,
                after_triggered_review_status_runtime.next_status_uid,
                after_item_runtime.next_status_uid,
                *(value + 1 for value in active_uids),
            )

        def dispatch_item_status_change(
            *,
            changed_effect_type: str,
            difference: int,
        ) -> None:
            """Resolve exact equipped-item children at the native effect slot.

            StatusChange items fire immediately after the committed scalar
            change, before the next card effect.  Their child status order is
            therefore observable by later effects on the same card.
            """

            nonlocal after_scalar
            nonlocal after_item_runtime
            nonlocal after_review_dynamic
            nonlocal after_triggered_review_status_runtime
            nonlocal after_aggressive_additive_runtime
            nonlocal after_debuff_registry

            if difference <= 0 or not after_item_runtime.listeners:
                return
            after_review_dynamic = _sync_review_dynamic_scalar(
                after_review_dynamic,
                after_scalar.review,
            )
            before_item = after_item_runtime
            dispatched = dispatch_plan2_native_item_event(
                before_item,
                Plan2NativeItemEvent(
                    phase=ITEM_PHASE_STATUS_CHANGE,
                    round_number=after_scalar.current_turn,
                    changed_effect_type=changed_effect_type,
                    status_difference=difference,
                    status_change_committed=True,
                    lesson_type=item_lesson_type,
                    block=after_scalar.block,
                ),
            )
            after_item_runtime = dispatched.after
            trace.extend(dispatched.trace)
            if not dispatched.effects:
                return

            fired_ids = set(dispatched.fired_enchantment_ids)
            owned_effects = tuple(
                (listener.source_item_id, effect)
                for listener in before_item.listeners
                if listener.enchantment_id in fired_ids
                for effect in listener.effects
            )
            if tuple(effect for _source, effect in owned_effects) != dispatched.effects:
                raise ValueError("item-status-change-effect-order-mismatch")

            for source_item_id, effect in owned_effects:
                context = ItemScalarContext(
                    scalar=after_scalar, review_dynamic=after_review_dynamic,
                    aggressive_runtime=after_aggressive_additive_runtime,
                    aggressive_catalog=self.aggressive_additive_catalog,
                    debuff_registry=after_debuff_registry,
                    next_status_uid=global_next_status_uid(after_item_runtime.next_status_uid),
                    source_item_id=source_item_id, play_origin=play_origin,
                    stamina_recover_restricted=stamina_recover_restricted,
                    stamina_recover_add_permil=stamina_recover_add_permil,
                )
                result, child_trace = execute_item_scalar_effect(context, effect)
                after_scalar = result.scalar
                after_review_dynamic = result.review_dynamic
                after_aggressive_additive_runtime = result.aggressive_runtime
                after_debuff_registry = result.debuff_registry
                trace.extend(child_trace)

            next_uid = global_next_status_uid()
            after_scalar = replace(after_scalar, next_status_uid=next_uid)
            after_item_runtime = replace(
                after_item_runtime,
                next_status_uid=next_uid,
            )
            after_review_dynamic = replace(
                after_review_dynamic,
                next_status_uid=next_uid,
            )
            if after_aggressive_additive_runtime is not None:
                after_aggressive_additive_runtime = replace(
                    after_aggressive_additive_runtime,
                    next_status_uid=next_uid,
                )
            after_triggered_review_status_runtime = replace(
                after_triggered_review_status_runtime,
                review_runtime=after_review_dynamic,
                next_status_uid=next_uid,
            )

        effect_ids = self.operation_effect_ids or tuple(
            getattr(value, "effect_id", f"operation-{index}")
            for index, value in enumerate(self.operations)
        )
        if len(effect_ids) != len(operations):
            raise ValueError("runtime-customization-operation-id-mismatch")
        once_flags = self.operation_once_flags or tuple(
            False for _ in self.operations
        )
        if len(once_flags) != len(operations):
            raise ValueError("runtime-customization-operation-once-mismatch")
        buff_use = use_matching_play_count_buff(
            after_generated_runtime,
            matching_search_ids=self.play_count_buff_search_ids,
        )
        if buff_use.unresolved:
            raise ValueError(
                "play-count-buff-use-failed-closed:"
                + ",".join(buff_use.unresolved)
            )
        after_generated_runtime = buff_use.after
        if buff_use.consumed:
            trace.append(
                "play-count-buff-use:"
                f"index={buff_use.selected_status_index}:"
                f"repeat={buff_use.repeat_count}"
            )
        # ``_execute_play`` has already materialized Android's first
        # build-side PlayCardCount on the pending Playing instance.  Once
        # predicates still need the pre-play count, so remove exactly that
        # transaction-local increment when deciding whether an is-once slot
        # was previously used.  The horizon owns this boundary; no card ID
        # or effect-specific exception belongs here.
        prior_play_count = max(
            after_zones.pending_played.runtime_state.play_count - 1,
            0,
        )
        scheduled_operations: list[tuple[str, Plan2MasterPlayingOperation]] = []
        for pass_index in range(buff_use.repeat_count + 1):
            for operation_effect_id, operation, is_once in zip(
                effect_ids,
                operations,
                once_flags,
                strict=True,
            ):
                if is_once and (pass_index > 0 or prior_play_count > 0):
                    trace.append(
                        "direct-effect-once-skip:"
                        f"pass={pass_index}:{operation_effect_id}"
                    )
                    continue
                scheduled_operations.append((operation_effect_id, operation))
        for scheduled_index, (operation_effect_id, operation) in enumerate(
            scheduled_operations
        ):
            if isinstance(operation, Plan2MasterTriggeredOperation):
                fires = _evaluate_effect_trigger_at_build(
                    operation.handoff,
                    trigger_snapshot_scalar,
                    trigger_zones,
                    play_origin=play_origin,
                    categories=trigger_categories,
                )
                trace.append(
                    f"effect-trigger:{operation.handoff.trigger_id if isinstance(operation.handoff, Plan2RemainingEffectTriggerHandoff) else operation.handoff.trigger.trigger_id}:"
                    + ("fire" if fires else "skip")
                )
                if not fires:
                    completed_effect_ids.append(operation_effect_id)
                    continue
                operation = operation.operation
            if isinstance(operation, Plan2MasterTriggeredEffectExecutorOperation):
                handoff = operation.handoff
                fires = _evaluate_effect_trigger_at_build(
                    (
                        handoff.remaining_trigger
                        if handoff.remaining_trigger is not None
                        else handoff.base_trigger
                    ),
                    trigger_snapshot_scalar,
                    trigger_zones,
                    play_origin=play_origin,
                    categories=trigger_categories,
                )
                if not fires:
                    trace.append(f"triggered-effect:{operation.effect_id}:skip")
                    completed_effect_ids.append(operation_effect_id)
                    continue
                if handoff.remaining_trigger is not None:
                    boundary = handoff.remaining_trigger.supported_boundaries[0]
                else:
                    preferred = (
                        EffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT,
                        EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
                        EffectTriggerBoundary.DIRECT_EFFECT_SEQUENCE_BUILD,
                        EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD,
                    )
                    boundary = next(
                        value
                        for value in preferred
                        if value in handoff.base_trigger.supported_boundaries
                    )
                blocked = gate_status_addition(handoff.base_trigger.effect_type)
                next_uid = global_next_status_uid()
                if handoff.family is TriggeredEffectExecutorFamily.STAMINA_DOWN_FIX:
                    runtime: object = replace(
                        after_stamina,
                        next_status_uid=next_uid,
                    )
                else:
                    runtime = replace(
                        after_review_dynamic,
                        next_status_uid=next_uid,
                    )
                exact = execute_plan2_native_triggered_effect(
                    runtime,
                    handoff,
                    Plan2NativeTriggeredEffectExecutionInput(
                        source_guid=source_guid,
                        phase=handoff.base_trigger.trigger.phase_types[0],
                        boundary=boundary,
                        origin=play_origin,
                        aggressive_snapshot=(
                            trigger_snapshot_scalar.card_play_aggressive
                        ),
                        current_stamina_snapshot=trigger_snapshot_scalar.stamina,
                        max_stamina_snapshot=trigger_snapshot_scalar.max_stamina,
                        review_snapshot=trigger_snapshot_scalar.review,
                        later_same_card_review=after_scalar.review,
                        payment_committed_before_build=False,
                        direct_effects_executed_before_build=0,
                        status_addition_blocked=blocked,
                    ),
                )
                if not exact.supported:
                    raise ValueError(
                        "triggered-effect-executor-failed-closed:"
                        f"{operation.effect_id}:{','.join(exact.unresolved)}"
                    )
                if handoff.family is TriggeredEffectExecutorFamily.STAMINA_DOWN_FIX:
                    assert isinstance(exact.after, Plan2StaminaModifierRuntime)
                    after_stamina = exact.after
                else:
                    assert isinstance(exact.after, Plan2NativeReviewDynamicRuntime)
                    after_review_dynamic = exact.after
                    after_scalar = replace(
                        after_scalar,
                        review=after_review_dynamic.review,
                    )
                    after_triggered_review_status_runtime = replace(
                        after_triggered_review_status_runtime,
                        review_runtime=after_review_dynamic,
                    )
                trace.extend(exact.trace)
                trace.append(f"triggered-effect:{operation.effect_id}:execute")
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterTriggeredStatusEnchantOperation):
                next_uid = global_next_status_uid()
                blocked = gate_status_addition(_EFFECT_STATUS_ENCHANT)
                owner = operation.program.owner_kind
                if owner == "local-start-turn-review-value-multiple":
                    runtime = replace(
                        after_triggered_review_status_runtime,
                        review_runtime=after_review_dynamic,
                        next_status_uid=next_uid,
                    )
                    installed = install_plan2_triggered_review_status(
                        runtime,
                        operation.program,
                        Plan2TriggeredReviewStatusInstallInput(
                            source_guid,
                            trigger_snapshot_scalar.review,
                            tuple(completed_effect_ids),
                            play_origin=play_origin,
                            status_addition_blocked=blocked,
                        ),
                    )
                    if not installed.executable:
                        raise ValueError(
                            "triggered-review-status-install-failed-closed:"
                            f"{operation.effect_id}:{','.join(installed.unresolved)}"
                        )
                    after_triggered_review_status_runtime = installed.after
                    after_review_dynamic = installed.after.review_runtime
                    trace.append(
                        f"triggered-status:{operation.effect_id}:"
                        + ("installed" if installed.installed else "blocked")
                    )
                elif owner == "start-play-card-draw":
                    assert operation.start_play_program is not None
                    if not blocked:
                        after_start_play_listeners = (
                            *after_start_play_listeners,
                            simulate_install_plan2_start_play_listener(
                                next_uid,
                                operation.start_play_program,
                            ),
                        )
                        after_scalar = replace(
                            after_scalar,
                            next_status_uid=next_uid + 1,
                        )
                    trace.append(
                        f"triggered-status:{operation.effect_id}:"
                        + ("blocked" if blocked else "installed")
                    )
                else:
                    assert operation.end_turn_program is not None
                    if not blocked:
                        if after_scalar.next_status_uid != next_uid:
                            after_scalar = replace(
                                after_scalar,
                                next_status_uid=next_uid,
                            )
                        installed_end = simulate_install_plan2_end_turn_listener(
                            after_scalar,
                            operation.end_turn_program,
                        )
                        after_scalar = installed_end.after
                    trace.append(
                        f"triggered-status:{operation.effect_id}:"
                        + ("blocked" if blocked else "installed")
                    )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterAggressiveAdditiveOperation):
                assert after_aggressive_additive_runtime is not None
                next_uid = global_next_status_uid()
                if after_aggressive_additive_runtime.next_status_uid != next_uid:
                    after_aggressive_additive_runtime = replace(
                        after_aggressive_additive_runtime,
                        next_status_uid=next_uid,
                    )
                blocked = gate_status_addition(operation.program.target_effect_type)
                installed = install_plan2_native_aggressive_additive(
                    after_aggressive_additive_runtime,
                    operation.program,
                    play_origin=(
                        "ordinary" if play_origin == "normal" else play_origin
                    ),
                    completed_effect_ids=tuple(completed_effect_ids),
                    blocked=blocked,
                )
                if blocked:
                    trace.append("aggressive-additive-install-blocked")
                elif not installed.executable:
                    raise ValueError(
                        "aggressive-additive-install-failed-closed:"
                        f"{operation.effect_id}:{installed.reason}"
                    )
                else:
                    after_aggressive_additive_runtime = installed.after
                    trace.extend(installed.operations)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterBlockFixRestrictionOperation):
                assert after_block_fix_restriction_runtime is not None
                next_uid = global_next_status_uid()
                if after_block_fix_restriction_runtime.next_status_uid != next_uid:
                    after_block_fix_restriction_runtime = replace(
                        after_block_fix_restriction_runtime,
                        next_status_uid=next_uid,
                    )
                before_block = after_scalar.block
                execution = execute_plan2_native_block_fix_restriction(
                    after_block_fix_restriction_runtime,
                    operation.program,
                    play_origin=play_origin,
                    completed_effect_ids=tuple(completed_effect_ids),
                )
                if not execution.executable:
                    raise ValueError(
                        "block-fix-restriction-failed-closed:"
                        f"{operation.effect_id}:{execution.reason}"
                    )
                after_block_fix_restriction_runtime = execution.after
                after_scalar = replace(
                    after_scalar,
                    block=execution.after.block,
                )
                after_block_consumption_sum_count = (
                    execution.after.block_consumption_sum_count
                )
                trace.extend(execution.operations)
                difference = after_scalar.block - before_block
                if difference:
                    after_scalar, status_enchant, status_trace = (
                        dispatch_plan2_native_status_enchant_event(
                            after_scalar,
                            status_enchant,
                            Plan2NativeStatusEnchantEvent(
                                phase=PHASE_STATUS_CHANGE,
                                play_origin=play_origin,
                                round_number=after_scalar.current_turn,
                                changed_effect_type=_EFFECT_BLOCK,
                                status_difference=difference,
                                status_change_committed=True,
                            ),
                            remaining_turns=remaining_turns,
                            review_dynamic=after_review_dynamic,
                            status_child_compilation=self.status_child_compilation,
                            status_child_context=status_child_context,
                        )
                    )
                    trace.extend(status_trace)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterExtraTurnOperation):
                assert after_extra_turn_runtime is not None
                execution = execute_plan2_native_extra_turn(
                    after_extra_turn_runtime,
                    operation.program,
                    play_origin=play_origin,
                )
                if not execution.executable:
                    raise ValueError(
                        "extra-turn-failed-closed:"
                        f"{operation.effect_id}:{execution.reason}"
                    )
                after_extra_turn_runtime = execution.after
                trace.extend(execution.operations)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterStaminaRecoverMultipleOperation):
                execution = execute_plan2_native_stamina_recover_multiple(
                    operation.program,
                    Plan2NativeStaminaRecoverMultipleSnapshot(
                        current_stamina=after_scalar.stamina,
                        max_stamina=after_scalar.max_stamina,
                        stamina_recover_restricted=stamina_recover_restricted,
                        stamina_recover_add_permil=stamina_recover_add_permil,
                        block=after_scalar.block,
                    ),
                    play_origin=play_origin,
                )
                if not execution.executable:
                    raise ValueError(
                        "stamina-recover-multiple-failed-closed:"
                        f"{operation.effect_id}:{execution.reason}"
                    )
                after_scalar = replace(
                    after_scalar,
                    stamina=execution.runtime_after.stamina,
                    block=execution.runtime_after.block,
                )
                trace.extend(execution.event_trace)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterLessonDependBlockRetentionOperation):
                handoff = build_plan2_native_lesson_depend_block_retention_handoff(
                    operation.version,
                    source_guid,
                    play_origin,
                    after_scalar.block,
                )
                exact = execute_plan2_native_lesson_depend_block_retention(
                    handoff,
                    LessonDependBlockRetentionRuntime(
                        block=after_scalar.block,
                        block_consumption_sum_count=(
                            after_block_consumption_sum_count
                        ),
                        adding_status=AddingParameterStatus(
                            aggressive=after_scalar.card_play_aggressive
                        ),
                        adding_settings=AddingParameterSettings(),
                        application_status=ParameterApplicationStatus(
                            judge_parameter=after_scalar.score
                        ),
                    ),
                )
                if not exact.hits:
                    raise ValueError(
                        "lesson-block-retention-produced-no-native-hits"
                    )
                scored, lesson_trace = _apply_review_dynamic_lesson(
                    after_scalar,
                    after_review_dynamic,
                    value=exact.hits[0].requested,
                    count=len(exact.hits),
                    play_origin=play_origin,
                )
                before_block = after_scalar.block
                after_scalar = replace(scored, block=exact.after.block)
                after_block_consumption_sum_count = (
                    exact.after.block_consumption_sum_count
                )
                if after_block_fix_restriction_runtime is not None:
                    after_block_fix_restriction_runtime = replace(
                        after_block_fix_restriction_runtime,
                        block=after_scalar.block,
                        block_consumption_sum_count=(
                            after_block_consumption_sum_count
                        ),
                    )
                trace.extend(exact.trace)
                trace.extend(lesson_trace)
                difference = after_scalar.block - before_block
                if difference:
                    after_scalar, status_enchant, status_trace = (
                        dispatch_plan2_native_status_enchant_event(
                            after_scalar,
                            status_enchant,
                            Plan2NativeStatusEnchantEvent(
                                phase=PHASE_STATUS_CHANGE,
                                play_origin=play_origin,
                                round_number=after_scalar.current_turn,
                                changed_effect_type=_EFFECT_BLOCK,
                                status_difference=difference,
                                status_change_committed=True,
                            ),
                            remaining_turns=remaining_turns,
                            review_dynamic=after_review_dynamic,
                            status_child_compilation=self.status_child_compilation,
                            status_child_context=status_child_context,
                        )
                    )
                    trace.extend(status_trace)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterMiscExactOperation):
                handoff = operation.handoff
                context = Plan2MiscExactExecutionContext(
                    origin=play_origin,
                    source_guid=source_guid,
                    cost_policy_resolved=True,
                    payment_committed_before_target=(play_origin == "normal"),
                    completed_slot_indexes=tuple(range(handoff.slot_index)),
                )
                before_aggressive = after_scalar.card_play_aggressive
                if handoff.family is Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE:
                    request = Plan2MiscExactAggressiveRequest(
                        context,
                        after_scalar.card_play_aggressive,
                        gate_status_addition(handoff.effect_type),
                    )
                elif handoff.family is Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE:
                    next_uid = global_next_status_uid()
                    if after_search_play_card_stamina_runtime.next_status_uid != next_uid:
                        after_search_play_card_stamina_runtime = replace(
                            after_search_play_card_stamina_runtime,
                            next_status_uid=next_uid,
                        )
                    request = Plan2MiscExactSearchStaminaRequest(
                        context,
                        after_search_play_card_stamina_runtime,
                        _plan3_state_at_playing(after_zones),
                        _plan3_card_from_ordered(after_zones.pending_played),
                        gate_status_addition(handoff.effect_type),
                    )
                elif handoff.family is Plan2MiscExactFamily.ADD_GROW_EFFECT:
                    if not isinstance(handoff.owner_contract, Plan2AddGrowProgram):
                        raise ValueError("misc-add-grow-owner-contract-mismatch")
                    grow_state = _add_grow_state_at_playing(
                        after_zones,
                        handoff.owner_contract,
                        frozenset(self.direct_review_refs),
                        after_add_grow_runtime,
                    )
                    request = Plan2MiscExactAddGrowRequest(context, grow_state)
                else:
                    request = Plan2MiscExactLessonBlockSumRequest(
                        context,
                        FinalCardRuntime(
                            native_state=_plan3_state_at_playing(after_zones),
                            stamina=after_scalar.stamina,
                            block=after_scalar.block,
                            block_consumption_sum_count=(
                                after_block_consumption_sum_count
                            ),
                            adding_status=AddingParameterStatus(
                                aggressive=after_scalar.card_play_aggressive
                            ),
                            adding_settings=AddingParameterSettings(),
                            application_status=(
                                after_scalar.parameter_application_status(
                                    slump=False
                                )
                            ),
                        ),
                    )
                exact = execute_plan2_native_catalog_misc_exact(handoff, request)
                if not exact.supported or not exact.target_effect_executed:
                    raise ValueError(
                        "misc-exact-failed-closed:"
                        f"{operation.effect_id}:{','.join(exact.reasons)}"
                    )
                if handoff.family is Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE:
                    after_scalar = replace(
                        after_scalar,
                        card_play_aggressive=int(exact.persistent_after),
                    )
                    if after_aggressive_additive_runtime is not None:
                        after_aggressive_additive_runtime = replace(
                            after_aggressive_additive_runtime,
                            aggressive=after_scalar.card_play_aggressive,
                        )
                elif handoff.family is Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE:
                    assert isinstance(exact.persistent_after, SearchPlayCardStaminaRuntime)
                    after_search_play_card_stamina_runtime = exact.persistent_after
                elif handoff.family is Plan2MiscExactFamily.ADD_GROW_EFFECT:
                    assert isinstance(exact.persistent_after, Plan2AddGrowDeckAllState)
                    after_add_grow_runtime = exact.persistent_after
                else:
                    assert isinstance(exact.persistent_after, FinalCardRuntime)
                    after_scalar = carry_plan2_state_application_status(
                        after_scalar,
                        exact.persistent_after.application_status,
                    )
                aggressive_difference = (
                    after_scalar.card_play_aggressive - before_aggressive
                )
                if aggressive_difference:
                    after_scalar, status_enchant, status_trace = (
                        dispatch_plan2_native_status_enchant_event(
                            after_scalar,
                            status_enchant,
                            Plan2NativeStatusEnchantEvent(
                                phase=PHASE_STATUS_CHANGE,
                                play_origin=play_origin,
                                round_number=after_scalar.current_turn,
                                changed_effect_type=_EFFECT_AGGRESSIVE,
                                status_difference=aggressive_difference,
                                status_change_committed=True,
                            ),
                            remaining_turns=remaining_turns,
                            review_dynamic=after_review_dynamic,
                            status_child_compilation=self.status_child_compilation,
                            status_child_context=status_child_context,
                        )
                    )
                    trace.extend(status_trace)
                trace.append(f"misc-exact:{handoff.family.value}:{handoff.effect_id}")
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterGeneratedCreateOperation):
                if generated_input is None:
                    raise ValueError("generated-card-input-missing")
                exact = execute_plan2_generated_card_create(
                    _plan3_state_at_playing(after_zones),
                    operation.program,
                    GeneratedCardCreateInput(
                        completed_effect_ids=tuple(completed_effect_ids),
                        guid_tokens=generated_input.guid_tokens,
                        plan_ignore_card_ids=generated_input.plan_ignore_card_ids,
                        hand_limit=hand_limit,
                    ),
                )
                if not exact.executable:
                    raise ValueError(
                        "generated-card-create-failed-closed:"
                        f"{operation.effect_id}:{','.join(exact.unresolved)}"
                    )
                before_create_zones = after_zones
                after_zones = _ordered_after_generated_create(
                    after_zones,
                    exact.after,
                )
                if status_child_context is not None:
                    after_scalar, after_zones, hand_move_trace = (
                        _apply_generated_hand_arrivals(
                            after_scalar,
                            before_create_zones,
                            after_zones,
                            status_child_context,
                            reason="hand-add",
                        )
                    )
                    trace.extend(hand_move_trace)
                    status_child_context.zones = after_zones
                trace.append(
                    "generated-card-create:"
                    + ",".join(value.guid for value in exact.created_cards)
                )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterGeneratedBuffForceOperation):
                if generated_input is None:
                    raise ValueError("generated-card-input-missing")
                blocked = gate_status_addition(PLAY_COUNT_BUFF)
                supplied_uid = generated_input.native_status_uid
                if not blocked:
                    active_uids = set(active_status_uids())
                    if supplied_uid is None or supplied_uid in active_uids:
                        raise ValueError("generated-native-status-uid-missing-or-collision")
                exact = execute_plan2_generated_buff_force_chain(
                    after_scalar,
                    _plan3_state_at_playing(
                        after_zones,
                        include_pending_in_hand=True,
                    ),
                    after_generated_runtime,
                    operation.buff_program,
                    operation.force_program,
                    GeneratedBuffForceInput(
                        parent_guid=source_guid,
                        selected_guid=generated_input.force_selected_guid,
                        status_uid=GeneratedStatusUidInput(supplied_uid),
                        native_status_add_blocked=blocked,
                        enchant_effect_uid=generated_input.enchant_effect_uid,
                    ),
                )
                if not exact.executable or exact.force_plan is None:
                    raise ValueError(
                        "generated-force-play-failed-closed:"
                        + ",".join(exact.unresolved)
                    )
                after_generated_runtime = exact.after_runtime
                if not blocked:
                    assert supplied_uid is not None
                    next_uid = global_next_status_uid(supplied_uid + 1)
                    after_scalar = replace(after_scalar, next_status_uid=next_uid)
                    after_stamina = replace(after_stamina, next_status_uid=next_uid)
                    status_enchant = replace(status_enchant, next_status_uid=next_uid)
                    after_review_dynamic = replace(
                        after_review_dynamic,
                        next_status_uid=next_uid,
                    )
                    after_debuff_registry = replace(
                        after_debuff_registry,
                        next_uid=next_uid,
                    )
                    after_effect_chains = replace(
                        after_effect_chains,
                        next_status_uid=next_uid,
                    )
                    if after_encore_runtime is not None:
                        after_encore_runtime = replace(
                            after_encore_runtime,
                            next_status_uid=next_uid,
                        )
                if exact.force_plan.branch.selected_guids != (
                    generated_input.force_selected_guid,
                ):
                    raise ValueError("generated-force-selection-drift")
                playable_delta = _checked_nonnegative_add(
                    playable_delta,
                    0,
                    "playable_delta",
                )
                trace.append(
                    f"generated-force-queue:{generated_input.force_selected_guid}"
                )
                queued_actions.append(
                    Plan2NativeQueuedAction(
                        "force_play",
                        card_guid=generated_input.force_selected_guid or "",
                        pay_cost=False,
                        spend_play=False,
                    )
                )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterGeneratedForceSettlementOperation):
                if generated_input is None or generated_input.force_selected_guid is None:
                    raise ValueError("generated-force-selection-missing")
                # The previous slot planned and consumed the exact search/buff
                # queue; central settlement runs only after the parent card moves.
                trace.append(
                    f"generated-force-settlement:{generated_input.force_selected_guid}"
                )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterEffectChainOperation):
                next_uid = global_next_status_uid()
                if after_effect_chains.next_status_uid != next_uid:
                    after_effect_chains = replace(
                        after_effect_chains,
                        next_status_uid=next_uid,
                    )
                installed = install_plan2_native_effect_chain(
                    after_effect_chains,
                    operation.program,
                    Plan2NativeEffectChainInstallInput(
                        source_guid=source_guid,
                        play_origin=play_origin,
                        completed_effect_ids=tuple(completed_effect_ids),
                        accepted=True,
                        install_phase_known=True,
                    ),
                )
                if not installed.executable or installed.installed is None:
                    raise ValueError(
                        "effect-chain-install-failed-closed:"
                        f"{operation.effect_id}:{','.join(installed.unresolved)}"
                    )
                after_effect_chains = installed.after
                trace.extend(installed.trace)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterStatusEnchantEncoreOperation):
                if after_encore_runtime is None:
                    raise ValueError("encore-runtime-unavailable")
                next_uid = global_next_status_uid()
                if after_encore_runtime.next_status_uid != next_uid:
                    after_encore_runtime = replace(
                        after_encore_runtime,
                        next_status_uid=next_uid,
                    )
                installed = install_plan2_native_status_enchant_encore(
                    after_encore_runtime,
                    operation.program,
                    playing_card=_plan3_card_from_ordered(
                        after_zones.pending_played
                    ),
                    source_guid=source_guid,
                    play_origin=(
                        "ordinary" if play_origin == "normal" else play_origin
                    ),
                    completed_effect_ids=tuple(completed_effect_ids),
                    current_effect_slot=operation.program.slot_index,
                    status_enchant_blocked=gate_status_addition(
                        _EFFECT_STATUS_ENCHANT
                    ),
                )
                if not installed.executable:
                    raise ValueError(
                        "encore-install-failed-closed:"
                        f"{operation.effect_id}:{installed.reason}"
                    )
                after_encore_runtime = installed.after
                trace.extend(
                    f"encore-difference:{value!r}"
                    for value in installed.differences
                )
                trace.append(
                    "encore-install:"
                    + ("installed" if installed.installed else installed.reason)
                )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterDebuffOperation):
                assert self.debuff_catalog is not None
                next_uid = global_next_status_uid()
                if after_debuff_registry.next_uid != next_uid:
                    after_debuff_registry = replace(
                        after_debuff_registry,
                        next_uid=next_uid,
                    )
                origin = Plan2DebuffPlayOrigin(play_origin)
                if operation.handoff.effect_type == EFFECT_ANTI_DEBUFF:
                    execution = execute_plan2_native_anti_debuff_handoff(
                        self.debuff_catalog,
                        operation.handoff,
                        after_debuff_registry,
                        origin=origin,
                    )
                    if not execution.executable:
                        raise ValueError(
                            "anti-debuff-failed-closed:"
                            f"{operation.effect_id}:{execution.reason}"
                        )
                    after_debuff_registry = execution.registry_after
                    append_debuff_queue(execution.queue_handoff)
                elif operation.handoff.effect_type == EFFECT_DEBUFF_RECOVER:
                    execution = execute_plan2_native_debuff_recover_handoff(
                        self.debuff_catalog,
                        operation.handoff,
                        after_debuff_registry,
                        origin=origin,
                    )
                    if not execution.executable:
                        raise ValueError(
                            "debuff-recover-failed-closed:"
                            f"{operation.effect_id}:{execution.reason}"
                        )
                    after_debuff_registry = execution.registry_after
                    append_debuff_queue(execution.queue_handoff)
                else:  # pragma: no cover - catalog constructor validates
                    raise ValueError(
                        f"debuff-operation-family-unbound:{operation.effect_id}"
                    )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterReviewDynamicOperation):
                next_uid = global_next_status_uid()
                if after_review_dynamic.next_status_uid != next_uid:
                    after_review_dynamic = replace(
                        after_review_dynamic,
                        next_status_uid=next_uid,
                    )
                if operation.program.operation_kind in {
                    OP_INSTALL_REVIEW_ADDITIVE,
                    OP_INSTALL_LESSON_MULTIPLE,
                    OP_INSTALL_LESSON_DEPENDENT,
                }:
                    installed = install_plan2_native_review_dynamic(
                        after_review_dynamic,
                        operation.program,
                        Plan2NativeReviewDynamicInstallInput(
                            source_guid=source_guid,
                            play_origin=play_origin,
                            completed_effect_ids=tuple(completed_effect_ids),
                            status_addition_blocked=gate_status_addition(
                                operation.program.effect_type
                            ),
                        ),
                    )
                    if not installed.executable or not installed.installed:
                        detail = ",".join(installed.unresolved) or "status-add-blocked"
                        raise ValueError(
                            "review-dynamic-install-failed-closed:"
                            f"{operation.effect_id}:{detail}"
                        )
                    after_review_dynamic = installed.after
                    trace.extend(installed.trace)
                elif operation.program.operation_kind in {
                    OP_REVIEW_VALUE_MULTIPLE,
                    OP_REVIEW_PER_SEARCH_COUNT,
                }:
                    transition = execute_plan2_native_review_dynamic_event(
                        after_review_dynamic,
                        operation.program,
                        Plan2NativeReviewDynamicEventInput(
                            source_guid=source_guid,
                            play_origin=play_origin,
                            completed_effect_ids=tuple(completed_effect_ids),
                            deck_search_count=len(after_zones.deck),
                            grave_search_count=len(after_zones.grave),
                            review_additive_fix=0,
                            status_addition_blocked=gate_status_addition(
                                _EFFECT_REVIEW
                            ),
                        ),
                    )
                    if not transition.executable:
                        raise ValueError(
                            "review-dynamic-event-failed-closed:"
                            f"{operation.effect_id}:"
                            f"{','.join(transition.unresolved)}"
                        )
                    difference = transition.after.review - after_scalar.review
                    after_review_dynamic = transition.after
                    after_scalar = replace(
                        after_scalar,
                        review=after_review_dynamic.review,
                    )
                    trace.extend(transition.trace)
                    if difference:
                        after_scalar, status_enchant, status_trace = (
                            dispatch_plan2_native_status_enchant_event(
                                after_scalar,
                                status_enchant,
                                Plan2NativeStatusEnchantEvent(
                                    phase=PHASE_STATUS_CHANGE,
                                    play_origin=play_origin,
                                    round_number=after_scalar.current_turn,
                                    changed_effect_type=_EFFECT_REVIEW,
                                    status_difference=difference,
                                    status_change_committed=True,
                                ),
                                remaining_turns=remaining_turns,
                                review_dynamic=after_review_dynamic,
                                status_child_compilation=self.status_child_compilation,
                                status_child_context=status_child_context,
                            )
                        )
                        trace.extend(status_trace)
                        if status_child_context is not None:
                            after_zones = status_child_context.zones
                            after_block_consumption_sum_count = (
                                status_child_context.block_consumption_sum_count
                            )
                        after_review_dynamic = _sync_review_dynamic_scalar(
                            after_review_dynamic,
                            after_scalar.review,
                        )
                else:  # pragma: no cover - leaf program validates this
                    raise ValueError(
                        f"review-dynamic-operation-unbound:{operation.effect_id}"
                    )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(
                operation,
                (
                    Plan2MasterLessonDependAggressiveOperation,
                    Plan2MasterRuntimeLessonDependAggressiveOperation,
                ),
            ):
                handoff = (
                    build_plan2_native_lesson_depend_aggressive_handoff(
                        operation.version,
                        source_guid,
                        play_origin,
                        after_scalar.card_play_aggressive,
                    )
                    if isinstance(
                        operation,
                        Plan2MasterLessonDependAggressiveOperation,
                    )
                    else build_plan2_native_lesson_depend_aggressive_effect_handoff(
                        operation.effect,
                        source_guid,
                        play_origin,
                        after_scalar.card_play_aggressive,
                    )
                )
                execution = execute_plan2_native_lesson_depend_aggressive(
                    handoff,
                    LessonDependAggressiveRuntime(
                        current_card_play_aggressive=(
                            after_scalar.card_play_aggressive
                        ),
                        application_status=ParameterApplicationStatus(
                            judge_parameter=after_scalar.score
                        ),
                    ),
                )
                if not execution.hits:
                    raise ValueError(
                        "lesson-depend-aggressive-produced-no-native-hits"
                    )
                scored, lesson_trace = _apply_review_dynamic_lesson(
                    after_scalar,
                    after_review_dynamic,
                    value=execution.hits[0].requested,
                    count=len(execution.hits),
                    play_origin=play_origin,
                )
                after_scalar = replace(
                    scored,
                    card_play_aggressive=(
                        execution.card_play_aggressive_after
                    ),
                )
                trace.extend(execution.trace)
                trace.extend(lesson_trace)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterDynamicBlockOperation):
                transition = execute_plan2_native_dynamic_block(
                    Plan2NativeDynamicBlockRuntime(
                        block=after_scalar.block,
                        aggressive=after_scalar.card_play_aggressive,
                        source_card_use_count=(
                            after_zones.pending_played.runtime_state.play_count
                        ),
                        exam_card_play_count=after_scalar.exam_card_play_count,
                        turn_card_play_count=after_scalar.turn_card_play_count,
                        block_consumption_sum_count=(
                            after_block_consumption_sum_count
                        ),
                    ),
                    operation.program,
                    Plan2NativeDynamicBlockExecutionInput(
                        source_guid=source_guid,
                        play_origin=play_origin,
                        completed_effect_ids=tuple(completed_effect_ids),
                    ),
                )
                if not transition.executable:
                    raise ValueError(
                        "dynamic-block-failed-closed:"
                        f"{operation.effect_id}:{','.join(transition.unresolved)}"
                    )
                difference = transition.after.block - after_scalar.block
                after_scalar = replace(after_scalar, block=transition.after.block)
                after_block_consumption_sum_count = (
                    transition.after.block_consumption_sum_count
                )
                trace.extend(transition.trace)
                if difference:
                    after_scalar, status_enchant, status_trace = (
                        dispatch_plan2_native_status_enchant_event(
                            after_scalar,
                            status_enchant,
                            Plan2NativeStatusEnchantEvent(
                                phase=PHASE_STATUS_CHANGE,
                                play_origin=play_origin,
                                round_number=after_scalar.current_turn,
                                changed_effect_type=_EFFECT_BLOCK,
                                status_difference=difference,
                                status_change_committed=True,
                            ),
                            remaining_turns=remaining_turns,
                            review_dynamic=after_review_dynamic,
                            status_child_compilation=self.status_child_compilation,
                            status_child_context=status_child_context,
                        )
                    )
                    trace.extend(status_trace)
                    if status_child_context is not None:
                        after_zones = status_child_context.zones
                        after_block_consumption_sum_count = (
                            status_child_context.block_consumption_sum_count
                        )
                    after_review_dynamic = _sync_review_dynamic_scalar(
                        after_review_dynamic,
                        after_scalar.review,
                    )
                    if (
                        after_encore_runtime is not None
                        and operation.effect_type == _EFFECT_AGGRESSIVE
                        and difference > 0
                    ):
                        planned = plan_plan2_native_status_enchant_encore(
                            after_encore_runtime,
                            Plan2NativeStatusEnchantEncoreEvent(
                                event_id=(
                                    f"{source_guid}:{operation.effect_id}:"
                                    "aggressive-up"
                                ),
                                phase=ENCORE_PHASE_AGGRESSIVE_UP_INTERVAL,
                                effect_type=ENCORE_AGGRESSIVE_EFFECT_TYPE,
                                replay_ancestry=replay_ancestry,
                                positive_occurrence_count=max(
                                    1,
                                    operation.count,
                                ),
                                play_origin=(
                                    "ordinary"
                                    if play_origin == "normal"
                                    else play_origin
                                ),
                            ),
                        )
                        if not planned.executable:
                            raise ValueError(
                                "encore-aggressive-event-failed-closed:"
                                + planned.reason
                            )
                        after_encore_runtime = planned.after
                        trace.extend(
                            f"encore-difference:{value!r}"
                            for value in planned.differences
                        )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterReviewMultipleOperation):
                next_uid = global_next_status_uid()
                if after_scalar.next_status_uid != next_uid:
                    after_scalar = replace(after_scalar, next_status_uid=next_uid)
                if gate_status_addition(_EFFECT_REVIEW_MULTIPLE):
                    trace.append("review-multiple-install-blocked")
                    completed_effect_ids.append(operation_effect_id)
                    continue
                installed = install_plan2_native_review_multiple(
                    after_scalar,
                    operation.program,
                    play_origin=(
                        Plan2ReviewMultiplePlayOrigin.ORDINARY
                        if play_origin == "normal"
                        else Plan2ReviewMultiplePlayOrigin(play_origin)
                    ),
                )
                after_scalar = installed.after
                trace.extend(installed.event_trace)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterStatusEnchantOperation):
                next_uid = global_next_status_uid()
                if status_enchant.next_status_uid != next_uid:
                    status_enchant = replace(status_enchant, next_status_uid=next_uid)
                installed = install_plan2_native_status_enchant(
                    status_enchant,
                    operation.program,
                    source_guid=source_guid,
                    play_origin=play_origin,
                    completed_effect_ids=tuple(completed_effect_ids),
                    status_add_blocked=gate_status_addition(
                        _EFFECT_STATUS_ENCHANT
                    ),
                )
                if not installed.executable or not installed.installed:
                    detail = ",".join(installed.unresolved) or "status-add-blocked"
                    raise ValueError(
                        f"status-enchant-install-failed-closed:{operation.effect_id}:{detail}"
                    )
                status_enchant = installed.after
                trace.extend(installed.trace)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterStaminaOperation):
                assert self.stamina_catalog is not None
                next_uid = global_next_status_uid()
                if after_stamina.next_status_uid != next_uid:
                    after_stamina = replace(after_stamina, next_status_uid=next_uid)
                resources = Plan2CostResources(
                    stamina=after_scalar.stamina,
                    max_stamina=after_scalar.max_stamina,
                    block=after_scalar.block,
                    review=after_scalar.review,
                    motivation=after_scalar.card_play_aggressive,
                    review_exists=after_scalar.review > 0,
                    review_consumption_sum=review_consumption_sum,
                )
                execution = execute_plan2_native_stamina_handoff(
                    self.stamina_catalog,
                    operation.handoff,
                    modifiers=after_stamina,
                    resources=resources,
                    block_add_status=gate_status_addition(
                        operation.handoff.effect_type
                    ),
                )
                if not execution.executable:
                    raise ValueError(
                        "stamina-handoff-failed-closed:"
                        f"{operation.effect_id}:{execution.reason}"
                    )
                after_stamina = execution.modifier_after
                after_scalar = replace(
                    after_scalar,
                    stamina=execution.resources_after.stamina,
                    block=execution.resources_after.block,
                    review=execution.resources_after.review,
                    card_play_aggressive=execution.resources_after.motivation,
                )
                after_review_dynamic = _sync_review_dynamic_scalar(
                    after_review_dynamic,
                    after_scalar.review,
                )
                trace.append(f"playing-stamina:{operation.effect_id}")
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(
                operation,
                (Plan2MasterScalarOperation, Plan2MasterRemainingThreeInstallOperation),
            ):
                before_scalar = after_scalar
                if (
                    isinstance(operation, Plan2MasterScalarOperation)
                    and operation.effect_type == _EFFECT_REVIEW
                ):
                    after_scalar = _apply_review_dynamic_review_gain(
                        after_scalar,
                        after_review_dynamic,
                        value=operation.value1,
                        count=max(1, operation.count),
                    )
                elif (
                    isinstance(operation, Plan2MasterScalarOperation)
                    and operation.effect_type == _EFFECT_AGGRESSIVE
                    and after_aggressive_additive_runtime is not None
                ):
                    aggressive = apply_plan2_native_bound_aggressive_additive_event(
                        after_aggressive_additive_runtime,
                        Plan2AggressiveAdditiveEvent(
                            base_value=operation.value1,
                            play_origin=(
                                "ordinary" if play_origin == "normal" else play_origin
                            ),
                            transaction_id=f"{source_guid}:{operation.effect_id}",
                            effect_id=operation.effect_id,
                            effect_type=AGGRESSIVE_ADDITIVE_EVENT_EFFECT_TYPE,
                        ),
                    )
                    if not aggressive.executable:
                        raise ValueError(
                            "aggressive-additive-event-failed-closed:"
                            f"{operation.effect_id}:{aggressive.reason}"
                        )
                    after_aggressive_additive_runtime = aggressive.after
                    after_scalar = replace(
                        after_scalar,
                        card_play_aggressive=aggressive.aggressive_after,
                    )
                    trace.extend(aggressive.operations)
                elif (
                    isinstance(operation, Plan2MasterScalarOperation)
                    and operation.effect_type == _EFFECT_BLOCK
                    and after_block_fix_restriction_runtime is not None
                ):
                    block_add = execute_plan2_native_block_add(
                        after_block_fix_restriction_runtime,
                        operation.value1,
                    )
                    if not block_add.executable:
                        raise ValueError(
                            "block-add-failed-closed:"
                            f"{operation.effect_id}:{block_add.reason}"
                        )
                    after_block_fix_restriction_runtime = block_add.after
                    after_scalar = replace(
                        after_scalar,
                        block=block_add.after.block,
                    )
                    trace.extend(block_add.operations)
                elif (
                    isinstance(operation, Plan2MasterScalarOperation)
                    and operation.effect_type
                    in {
                        _EFFECT_LESSON,
                        _EFFECT_LESSON_DEPEND_REVIEW,
                        _EFFECT_LESSON_DEPEND_BLOCK,
                    }
                ):
                    repetitions = max(1, operation.count)
                    if operation.effect_type == _EFFECT_LESSON:
                        requested = operation.value1
                    elif operation.effect_type == _EFFECT_LESSON_DEPEND_REVIEW:
                        requested = get_ratio_effect_int_value(
                            after_scalar.review,
                            operation.value1,
                            is_ceil=True,
                        )
                    else:
                        requested = get_ratio_effect_int_value(
                            after_scalar.block,
                            operation.value1,
                            is_ceil=True,
                        )
                    after_scalar, lesson_trace = _apply_review_dynamic_lesson(
                        after_scalar,
                        after_review_dynamic,
                        value=requested,
                        count=repetitions,
                        play_origin=play_origin,
                    )
                    trace.extend(lesson_trace)
                else:
                    after_scalar = Plan2MasterDirectExecutor(
                        self.card_id,
                        self.upgrade,
                        (operation,),
                    )(after_scalar)
                    if after_aggressive_additive_runtime is not None:
                        after_aggressive_additive_runtime = replace(
                            after_aggressive_additive_runtime,
                            aggressive=after_scalar.card_play_aggressive,
                        )
                    if after_block_fix_restriction_runtime is not None:
                        after_block_fix_restriction_runtime = replace(
                            after_block_fix_restriction_runtime,
                            block=after_scalar.block,
                            block_consumption_sum_count=(
                                after_block_consumption_sum_count
                            ),
                        )
                # A direct Review operation can be scaled by a restored
                # ReviewAdditive layer before its StatusChange callbacks run.
                # Keep the dynamic mirror synchronized for those callbacks;
                # otherwise a listener child sees the pre-operation Review
                # snapshot and fails closed.
                after_review_dynamic = _sync_review_dynamic_scalar(
                    after_review_dynamic,
                    after_scalar.review,
                )
                trace.append(f"playing-direct:{operation.effect_id}")
                if isinstance(operation, Plan2MasterScalarOperation):
                    differences = {
                        _EFFECT_REVIEW: after_scalar.review - before_scalar.review,
                        _EFFECT_BLOCK: after_scalar.block - before_scalar.block,
                        _EFFECT_AGGRESSIVE: (
                            after_scalar.card_play_aggressive
                            - before_scalar.card_play_aggressive
                        ),
                    }
                    difference = differences.get(operation.effect_type, 0)
                    if difference:
                        after_scalar, status_enchant, status_trace = (
                            dispatch_plan2_native_status_enchant_event(
                                after_scalar,
                                status_enchant,
                                Plan2NativeStatusEnchantEvent(
                                    phase=PHASE_STATUS_CHANGE,
                                    play_origin=play_origin,
                                    round_number=after_scalar.current_turn,
                                    changed_effect_type=operation.effect_type,
                                    status_difference=difference,
                                    status_change_committed=True,
                                ),
                                remaining_turns=remaining_turns,
                                review_dynamic=after_review_dynamic,
                                status_child_compilation=self.status_child_compilation,
                                status_child_context=status_child_context,
                            )
                        )
                        trace.extend(status_trace)
                        if status_child_context is not None:
                            after_zones = status_child_context.zones
                            after_block_consumption_sum_count = (
                                status_child_context.block_consumption_sum_count
                            )
                        # Status-enchant children may apply a Review gain
                        # through the active ReviewAdditive stack.  Refresh
                        # the dynamic scalar mirror before an item
                        # StatusChange callback observes this same direct
                        # operation; otherwise the callback sees stale
                        # Review and fails the snapshot contract.
                        after_review_dynamic = _sync_review_dynamic_scalar(
                            after_review_dynamic,
                            after_scalar.review,
                        )
                        if operation.effect_type in {
                            ITEM_EFFECT_AGGRESSIVE,
                            _EFFECT_REVIEW,
                        }:
                            dispatch_item_status_change(
                                changed_effect_type=operation.effect_type,
                                difference=difference,
                            )
                    after_review_dynamic = _sync_review_dynamic_scalar(
                        after_review_dynamic,
                        after_scalar.review,
                    )
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(operation, Plan2MasterCardMoveOtherOperation):
                before_move_zones = after_zones
                exact_move = execute_plan2_native_catalog_card_move_other(
                    _remaining_card_move_state_at_playing(after_zones),
                    operation.program,
                    hand_limit=hand_limit,
                    play_origin=play_origin,
                )
                after_zones = _ordered_after_remaining_card_move(
                    after_zones,
                    exact_move.after,
                )
                if status_child_context is not None:
                    after_scalar, after_zones, hand_move_trace = (
                        _apply_generated_hand_arrivals(
                            after_scalar,
                            before_move_zones,
                            after_zones,
                            status_child_context,
                            reason="card-move",
                        )
                    )
                    trace.extend(hand_move_trace)
                    status_child_context.zones = after_zones
                trace.extend(exact_move.phases)
                completed_effect_ids.append(operation_effect_id)
                continue
            if isinstance(
                operation,
                (
                    Plan2MasterHandGraveDrawOperation,
                    Plan2MasterCardMoveOperation,
                    Plan2MasterCardUpgradeOperation,
                ),
            ):
                before_move_zones = after_zones
                # The typed runtime-customization provenance above selects
                # which native child boundary materialized this Hand search.
                # Uncustomized ordered children can expose a preceding zone
                # mutation; a materialized customized batch retains its
                # Playing-boundary snapshot.
                candidate_snapshot = trigger_zones
                if (
                    zone_snapshot_changed
                    and zone_search_materialization == "live-after-child"
                ):
                    candidate_snapshot = after_zones
                after_zones, zone_trace = Plan2MasterZoneExecutor(
                    self.card_id,
                    self.upgrade,
                    (operation,),
                )(
                    after_zones,
                    hand_limit,
                    candidate_snapshot=candidate_snapshot,
                )
                if status_child_context is not None:
                    after_scalar, after_zones, hand_move_trace = (
                        _apply_generated_hand_arrivals(
                            after_scalar,
                            before_move_zones,
                            after_zones,
                            status_child_context,
                            reason="card-move",
                        )
                    )
                    trace.extend(hand_move_trace)
                    status_child_context.zones = after_zones
                trace.extend(zone_trace)
                completed_effect_ids.append(operation_effect_id)
                zone_snapshot_changed = True
                continue
            if operation.kind == "add_playable":
                playable_delta = _checked_nonnegative_add(
                    playable_delta,
                    operation.value,
                    "playable_delta",
                )
                trace.append(f"playing:add-playable:+{operation.value}")
                trace.append(f"nested:add-playable:+{operation.value}")
                completed_effect_ids.append(operation_effect_id)
                continue
            capacity = max(hand_limit - len(after_zones.hand), 0)
            available = len(after_zones.deck) + len(after_zones.grave)
            actual = min(operation.value, capacity, available)
            if actual == 0:
                trace.append("playing:draw:0")
                trace.append("nested:draw")
                completed_effect_ids.append(operation_effect_id)
                continue
            before_draw_zones = after_zones
            draw = after_zones.draw_to_hand(actual)
            after_zones = draw.state
            zone_snapshot_changed = True
            if status_child_context is not None:
                # Every card materialized into Hand reaches the same HandAdd
                # support boundary before the next ordered effect.  A failed
                # support roll still advances the shared RNG even though it
                # adds no support ID and changes no card lineage.
                after_scalar, after_zones, hand_move_trace = (
                    _apply_generated_hand_arrivals(
                        after_scalar,
                        before_draw_zones,
                        after_zones,
                        status_child_context,
                        reason="draw",
                    )
                )
                trace.extend(hand_move_trace)
                status_child_context.zones = after_zones
            trace.extend(
                (
                    "nested:draw",
                    f"playing:draw:{','.join(draw.drawn_guids)}",
                    f"playing:shuffle-grave:{draw.recycled_grave_count}",
                    f"playing:draw-rng:{draw.random_state_before}->{after_zones.random_state}",
                    f"shuffle-grave:{draw.recycled_grave_count}",
                    f"rng:{draw.random_state_before}->{after_zones.random_state}",
                )
            )
            completed_effect_ids.append(operation_effect_id)
        return Plan2NativePlayingEffectResult(
            scalar=after_scalar,
            zones=after_zones,
            stamina_modifiers=after_stamina,
            status_enchant=status_enchant,
            review_consumption_sum=review_consumption_sum,
            block_consumption_sum_count=after_block_consumption_sum_count,
            review_dynamic=after_review_dynamic,
            debuff_registry=after_debuff_registry,
            effect_chains=after_effect_chains,
            generated_runtime=after_generated_runtime,
            item_runtime=after_item_runtime,
            encore_runtime=after_encore_runtime,
            aggressive_additive_runtime=after_aggressive_additive_runtime,
            block_fix_restriction_runtime=after_block_fix_restriction_runtime,
            extra_turn_runtime=after_extra_turn_runtime,
            stamina_recover_restricted=stamina_recover_restricted,
            stamina_recover_add_permil=stamina_recover_add_permil,
            search_play_card_stamina_runtime=(
                after_search_play_card_stamina_runtime
            ),
            add_grow_runtime=after_add_grow_runtime,
            start_play_listeners=after_start_play_listeners,
            triggered_review_status_runtime=(
                after_triggered_review_status_runtime
            ),
            playable_delta=playable_delta,
            queued_actions=tuple(queued_actions),
            trace=tuple(trace),
        )


@dataclass(frozen=True, slots=True)
class Plan2MasterDirectExecutor:
    """Ordered pure executor over only operations represented by Plan2State."""

    card_id: str
    upgrade: int
    operations: tuple[Plan2MasterDirectOperation, ...]

    def __post_init__(self) -> None:
        _nonempty(self.card_id, "card_id")
        _plain_int(self.upgrade, "upgrade", minimum=0)
        operations = tuple(self.operations)
        if any(
            not isinstance(
                operation,
                (Plan2MasterScalarOperation, Plan2MasterRemainingThreeInstallOperation),
            )
            for operation in operations
        ):
            raise TypeError("operations contain an unsupported operation type")
        object.__setattr__(self, "operations", operations)

    def __call__(self, state: Plan2State) -> Plan2State:
        if not isinstance(state, Plan2State):
            raise TypeError("state must be Plan2State")
        after = state
        for operation in self.operations:
            if isinstance(operation, Plan2MasterRemainingThreeInstallOperation):
                after = install_plan2_end_turn_remaining_three_exact_listener(
                    after,
                    operation.program,
                ).after
                continue

            effect_type = operation.effect_type
            repetitions = max(1, operation.count)
            if effect_type == _EFFECT_REVIEW:
                if operation.value1 < 0 or operation.value2 or operation.turn:
                    raise ValueError(f"unsupported Review shape: {operation.effect_id}")
                for _ in range(repetitions):
                    after = replace(
                        after,
                        review=_checked_nonnegative_add(
                            after.review,
                            operation.value1,
                            "review",
                        ),
                    )
                continue
            if effect_type == _EFFECT_BLOCK:
                if operation.value1 < 0 or operation.value2 or operation.turn:
                    raise ValueError(f"unsupported Block shape: {operation.effect_id}")
                for _ in range(repetitions):
                    delta = calculate_add_block(
                        operation.value1,
                        is_buff_active=True,
                        status=AddBlockStatus(
                            aggressive=after.card_play_aggressive,
                        ),
                        settings=AddBlockSettings(),
                    )
                    after = replace(
                        after,
                        block=_checked_nonnegative_add(after.block, delta, "block"),
                    )
                continue
            if effect_type == _EFFECT_AGGRESSIVE:
                if operation.value1 < 0 or operation.value2 or operation.turn:
                    raise ValueError(
                        f"unsupported CardPlayAggressive shape: {operation.effect_id}"
                    )
                for _ in range(repetitions):
                    after = replace(
                        after,
                        card_play_aggressive=_checked_nonnegative_add(
                            after.card_play_aggressive,
                            operation.value1,
                            "card_play_aggressive",
                        ),
                    )
                continue
            if effect_type == _EFFECT_LESSON:
                if operation.value1 < 0 or operation.value2 or operation.turn:
                    raise ValueError(f"unsupported Lesson shape: {operation.effect_id}")
                delta = operation.value1 * repetitions
            elif effect_type == _EFFECT_LESSON_DEPEND_REVIEW:
                if operation.value1 < 0 or operation.value2 or operation.turn:
                    raise ValueError(
                        f"unsupported LessonDependReview shape: {operation.effect_id}"
                    )
                one_hit = get_ratio_effect_int_value(
                    after.review,
                    operation.value1,
                    is_ceil=True,
                )
                delta = one_hit * repetitions
            elif effect_type == _EFFECT_LESSON_DEPEND_BLOCK:
                if operation.value1 < 0 or operation.value2 or operation.turn:
                    raise ValueError(
                        f"unsupported LessonDependBlock shape: {operation.effect_id}"
                    )
                one_hit = get_ratio_effect_int_value(
                    after.block,
                    operation.value1,
                    is_ceil=True,
                )
                delta = one_hit * repetitions
            else:  # pragma: no cover - constructor is private to the compiler
                raise ValueError(f"unbound operation: {operation.effect_id}")
            one_hit = delta // repetitions
            for _ in range(repetitions):
                after = apply_plan2_state_score(
                    after,
                    one_hit,
                    score_kind=Plan2ScoreKind.LESSON,
                    integration_point=Plan2ScoreIntegrationPoint.SCALAR_DIRECT,
                    slump=False,
                ).after
        return after


@dataclass(frozen=True, slots=True)
class Plan2MasterReviewUpPredicate:
    trigger_id: str
    threshold: int

    def __post_init__(self) -> None:
        _nonempty(self.trigger_id, "trigger_id")
        _plain_int(self.threshold, "threshold", minimum=0)

    def __call__(self, state: Plan2State) -> bool:
        if not isinstance(state, Plan2State):
            raise TypeError("state must be Plan2State")
        return state.review >= self.threshold


@dataclass(frozen=True, slots=True)
class Plan2MasterCardTriggerPredicate:
    """Typed pre-payment adapter for one exact card-owned gate."""

    handoff: Plan2NativeCardTriggerHandoff

    @property
    def trigger_id(self) -> str:
        return self.handoff.predicate.trigger_id

    def __call__(self, state: Plan2NativeHorizonState, origin: str) -> bool:
        if not isinstance(state, Plan2NativeHorizonState):
            raise TypeError("state must be Plan2NativeHorizonState")
        try:
            card_origin = CardPlayOrigin(origin)
        except (TypeError, ValueError) as error:
            raise ValueError(f"unknown card origin: {origin}") from error
        predicate = self.handoff.predicate
        evaluation = evaluate_plan2_native_card_trigger(
            self.handoff,
            Plan2NativeCardTriggerSnapshot(
                phase=predicate.phase,
                boundary=predicate.boundary,
                origin=card_origin,
                aggressive=state.scalar.card_play_aggressive,
                block=state.scalar.block,
                current_stamina=state.scalar.stamina,
                max_stamina=state.scalar.max_stamina,
                current_turn=state.scalar.current_turn,
                remaining_turn=state.remaining_turns,
                review=state.scalar.review,
                judge_parameter=state.scalar.score,
                clear_border=state.clear_border,
                global_card_play_count=state.scalar.exam_card_play_count,
                turn_card_play_count=state.scalar.turn_card_play_count,
            ),
        )
        if not evaluation.supported or type(evaluation.fires) is not bool:
            detail = ",".join(evaluation.reasons) or "unknown-trigger-result"
            raise ValueError(f"card-trigger-failed-closed:{self.trigger_id}:{detail}")
        return evaluation.fires


@dataclass(frozen=True, slots=True)
class Plan2NativeProgramCatalogCompilation:
    schema_version: int
    master_database: str
    universe_refs: tuple[tuple[str, int], ...]
    catalog: Plan2NativeProgramCatalog
    blockers: tuple[Plan2NativeCatalogBlocker, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_PROGRAM_CATALOG_SCHEMA_VERSION:
            raise ValueError("unsupported program-catalog schema version")
        _nonempty(self.master_database, "master_database")
        refs = tuple(self.universe_refs)
        if len(refs) != len(set(refs)):
            raise ValueError("universe_refs contains duplicates")
        if refs != tuple(sorted(refs)):
            raise ValueError("universe_refs must be sorted")
        if not isinstance(self.catalog, Plan2NativeProgramCatalog):
            raise TypeError("catalog must be Plan2NativeProgramCatalog")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2NativeCatalogBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan2NativeCatalogBlocker")
        object.__setattr__(self, "universe_refs", refs)
        object.__setattr__(self, "blockers", blockers)

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(program.ref for program in self.catalog.programs)

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.universe_refs if ref not in compiled)

    @property
    def universe_version_count(self) -> int:
        return len(self.universe_refs)

    @property
    def compiled_version_count(self) -> int:
        return len(self.catalog.programs)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def fully_compiled(self) -> bool:
        return (
            bool(self.universe_refs)
            and set(self.compiled_refs) == set(self.universe_refs)
            and not self.blockers
        )

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(value.code for value in self.blockers).items()))

    @property
    def unbound_effect_family_counts(self) -> dict[str, dict[str, int]]:
        occurrences: Counter[str] = Counter()
        refs: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.blockers:
            if blocker.code != "effect-runtime-unbound":
                continue
            effect_type = blocker.detail.split(":", 1)[0]
            occurrences[effect_type] += 1
            refs.setdefault(effect_type, set()).add(blocker.ref)
        ordered = sorted(
            occurrences,
            key=lambda effect_type: (-occurrences[effect_type], effect_type),
        )
        return {
            effect_type: {
                "occurrences": occurrences[effect_type],
                "affected_versions": len(refs[effect_type]),
            }
            for effect_type in ordered
        }

    def blockers_for(self, ref: tuple[str, int]) -> tuple[Plan2NativeCatalogBlocker, ...]:
        return tuple(value for value in self.blockers if value.ref == ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "master_database": self.master_database,
            "universe_version_count": self.universe_version_count,
            "target_version_count": self.universe_version_count,
            "compiled_version_count": self.compiled_version_count,
            "failed_version_count": self.failed_version_count,
            "fully_compiled": self.fully_compiled,
            "blocker_code_counts": self.blocker_code_counts,
            "unbound_effect_family_counts": self.unbound_effect_family_counts,
            "compiled_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.compiled_refs
            ],
            "failed_refs": [
                {
                    "card_id": card_id,
                    "upgrade": upgrade,
                    "blockers": [
                        blocker.to_dict() for blocker in self.blockers_for((card_id, upgrade))
                    ],
                }
                for card_id, upgrade in self.failed_refs
            ],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeMandatoryPItemResidual:
    """One exact runtime boundary retained by the static P-item graph.

    The effect graph proves Master parsing and order, but its operations are
    deliberately marked ``runtime_required``.  Reporting those operations as
    a residual matrix keeps a catalog from claiming native execution that has
    not been wired to live state.
    """

    operation: str
    effect_type: str
    occurrences: int
    variant_refs: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        _nonempty(self.operation, "residual operation")
        _nonempty(self.effect_type, "residual effect_type")
        _plain_int(self.occurrences, "residual occurrences", minimum=1)
        refs = tuple(self.variant_refs)
        if refs != tuple(sorted(set(refs))):
            raise ValueError("residual variant_refs must be sorted and unique")
        if not refs:
            raise ValueError("residual variant_refs must be non-empty")
        object.__setattr__(self, "variant_refs", refs)

    @property
    def affected_variants(self) -> int:
        return len(self.variant_refs)

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "effect_type": self.effect_type,
            "occurrences": self.occurrences,
            "affected_variants": self.affected_variants,
            "variant_refs": [
                {"idol_card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.variant_refs
            ],
        }


def _walk_pitem_operations(
    operations: Sequence[Plan2PItemOperation],
) -> tuple[Plan2PItemOperation, ...]:
    result: list[Plan2PItemOperation] = []

    def visit(operation: Plan2PItemOperation) -> None:
        result.append(operation)
        for child in operation.children:
            visit(child)

    for operation in operations:
        visit(operation)
    return tuple(result)


def _parse_mandatory_pitem_dispatch(
    trigger: object,
) -> PItemEventDispatch:
    """Parse a graph trigger while retaining the native CardPlay marker.

    Two legacy ReviewUp listeners encode the card boundary as
    ``lowerSearchCount=1`` without a search ID.  That marker is part of the
    exact Master shape but is not a card-search predicate; represent it as a
    typed dispatch with no search reference instead of dropping the row or
    changing its serialized values.
    """

    if not hasattr(trigger, "to_dict"):
        raise TypeError("mandatory P-item trigger must be a CompositeTriggerSpec")
    raw = trigger.to_dict()
    try:
        return parse_plan2_pitem_event_dispatch(
            raw,
            card_search_rule=getattr(trigger, "card_search_rule", None),
        )
    except PItemEventDispatchError as error:
        if error.code != "search-count-without-reference":
            raise
        return PItemEventDispatch(
            trigger=trigger,
            phase=PItemEventPhase(trigger.phase),
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeMandatoryPItemVariant:
    """One idol-card before/after mandatory item with typed graph contracts."""

    graph: Plan2PItemVariant
    dispatches: tuple[PItemEventDispatch, ...]
    evaluation: Plan2PItemEvaluation
    blockers: tuple[Plan2NativeCatalogBlocker, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.graph, Plan2PItemVariant):
            raise TypeError("graph must be Plan2PItemVariant")
        dispatches = tuple(self.dispatches)
        if any(not isinstance(value, PItemEventDispatch) for value in dispatches):
            raise TypeError("dispatches must contain PItemEventDispatch values")
        if len(dispatches) != len(self.graph.enchantments):
            raise ValueError("dispatch/enchantment count mismatch")
        if tuple(value.trigger_id for value in dispatches) != self.graph.trigger_ids:
            raise ValueError("dispatch/enchantment order mismatch")
        if not isinstance(self.evaluation, Plan2PItemEvaluation):
            raise TypeError("evaluation must be Plan2PItemEvaluation")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2NativeCatalogBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan2NativeCatalogBlocker values")
        object.__setattr__(self, "dispatches", dispatches)
        object.__setattr__(self, "blockers", blockers)

    @property
    def idol_card_id(self) -> str:
        return self.graph.idol_card_id

    @property
    def item_id(self) -> str:
        return self.graph.item_id

    @property
    def upgrade(self) -> int:
        return self.graph.upgrade

    @property
    def operations(self) -> tuple[Plan2PItemOperation, ...]:
        return self.evaluation.operations

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return self.graph.ordered_effect_ids

    @property
    def supported(self) -> bool:
        return not self.blockers and self.evaluation.supported

    @property
    def runtime_complete(self) -> bool:
        return self.supported and self.evaluation.runtime_complete

    def evaluate(
        self,
        context: PItemPredicateContext,
        enchantment_index: int = 0,
    ) -> PItemDispatchEvaluation:
        if not isinstance(context, PItemPredicateContext):
            raise TypeError("context must be PItemPredicateContext")
        if (
            isinstance(enchantment_index, bool)
            or not isinstance(enchantment_index, int)
            or enchantment_index < 0
            or enchantment_index >= len(self.dispatches)
        ):
            raise IndexError("enchantment_index out of range")
        from .plan2_pitem_event_dispatch import evaluate_plan2_pitem_event_dispatch

        return evaluate_plan2_pitem_event_dispatch(
            self.dispatches[enchantment_index], context
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeMandatoryPItemCatalog:
    """All current Plan2 idol-card mandatory P-item variants in Master order."""

    variants: tuple[Plan2NativeMandatoryPItemVariant, ...]

    def __post_init__(self) -> None:
        variants = tuple(self.variants)
        if any(not isinstance(value, Plan2NativeMandatoryPItemVariant) for value in variants):
            raise TypeError("variants must contain Plan2NativeMandatoryPItemVariant values")
        keys = [(value.idol_card_id, value.upgrade) for value in variants]
        if len(keys) != len(set(keys)):
            raise ValueError("mandatory P-item variants contain duplicates")
        if keys != sorted(keys):
            raise ValueError("mandatory P-item variants must be sorted")
        object.__setattr__(self, "variants", variants)

    @property
    def supported(self) -> bool:
        return len(self.variants) == 118 and all(value.supported for value in self.variants)

    @property
    def runtime_complete(self) -> bool:
        return bool(self.variants) and all(value.runtime_complete for value in self.variants)

    @property
    def blockers(self) -> tuple[Plan2NativeCatalogBlocker, ...]:
        return tuple(
            blocker
            for variant in self.variants
            for blocker in variant.blockers
        )

    def variant(self, idol_card_id: str, upgrade: int = 0) -> Plan2NativeMandatoryPItemVariant:
        matches = tuple(
            value
            for value in self.variants
            if value.idol_card_id == idol_card_id and value.upgrade == upgrade
        )
        if len(matches) != 1:
            raise KeyError(f"mandatory P-item variant resolves {len(matches)} times")
        return matches[0]

    @property
    def residual_matrix(self) -> tuple[Plan2NativeMandatoryPItemResidual, ...]:
        counts: Counter[tuple[str, str]] = Counter()
        refs: dict[tuple[str, str], set[tuple[str, int]]] = {}
        for variant in self.variants:
            for operation in _walk_pitem_operations(variant.operations):
                if not operation.runtime_required:
                    continue
                key = (operation.operation, operation.effect_type)
                counts[key] += 1
                refs.setdefault(key, set()).add((variant.idol_card_id, variant.upgrade))
        keys = sorted(counts, key=lambda value: (value[0], value[1]))
        return tuple(
            Plan2NativeMandatoryPItemResidual(
                operation=operation,
                effect_type=effect_type,
                occurrences=counts[(operation, effect_type)],
                variant_refs=tuple(sorted(refs[(operation, effect_type)])),
            )
            for operation, effect_type in keys
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "variant_count": len(self.variants),
            "idol_card_count": len({value.idol_card_id for value in self.variants}),
            "supported": self.supported,
            "runtime_complete": self.runtime_complete,
            "blockers": [value.to_dict() for value in self.blockers],
            "residual_matrix": [value.to_dict() for value in self.residual_matrix],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeMandatoryPItemCatalogCompilation:
    database: str
    catalog: Plan2NativeMandatoryPItemCatalog | None
    blockers: tuple[Plan2NativeCatalogBlocker, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.database, "mandatory P-item database")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2NativeCatalogBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan2NativeCatalogBlocker values")
        object.__setattr__(self, "blockers", blockers)

    @property
    def supported(self) -> bool:
        return self.catalog is not None and self.catalog.supported and not self.blockers

    @property
    def runtime_complete(self) -> bool:
        return self.catalog is not None and self.catalog.runtime_complete

    @property
    def variant_count(self) -> int:
        return 0 if self.catalog is None else len(self.catalog.variants)

    @property
    def residual_matrix(self) -> tuple[Plan2NativeMandatoryPItemResidual, ...]:
        return () if self.catalog is None else self.catalog.residual_matrix

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "supported": self.supported,
            "runtime_complete": self.runtime_complete,
            "variant_count": self.variant_count,
            "blockers": [value.to_dict() for value in self.blockers],
            "residual_matrix": [value.to_dict() for value in self.residual_matrix],
        }


def compile_plan2_native_mandatory_item_catalog(
    *,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeMandatoryPItemCatalogCompilation:
    """Compile all 59x2 mandatory items without item-ID execution branches."""

    database_path = Path(database).resolve()
    graph_catalog = load_plan2_pitem_catalog(database_path)
    variants: list[Plan2NativeMandatoryPItemVariant] = []
    for graph_variant in graph_catalog.variants:
        blockers: list[Plan2NativeCatalogBlocker] = []
        dispatches: list[PItemEventDispatch] = []
        for enchantment in graph_variant.enchantments:
            try:
                dispatches.append(_parse_mandatory_pitem_dispatch(enchantment.trigger))
            except (PItemEventDispatchError, TypeError, ValueError) as error:
                code = getattr(error, "code", "event-dispatch-unsupported")
                blockers.append(
                    Plan2NativeCatalogBlocker(
                        "pitem-event-dispatch-unsupported",
                        graph_variant.idol_card_id,
                        graph_variant.upgrade,
                        f"{code}:{error}",
                    )
                )
        try:
            evaluation = evaluate_effect_graph(graph_variant)
        except (Plan2PItemEffectGraphError, TypeError, ValueError) as error:
            evaluation = Plan2PItemEvaluation(False, (), (f"evaluation-error:{error}",))
        if not graph_variant.supported:
            blockers.extend(
                Plan2NativeCatalogBlocker(
                    "pitem-effect-graph-unsupported",
                    graph_variant.idol_card_id,
                    graph_variant.upgrade,
                    detail,
                )
                for detail in graph_variant.unsupported
            )
        if not evaluation.supported:
            blockers.extend(
                Plan2NativeCatalogBlocker(
                    "pitem-effect-evaluation-unsupported",
                    graph_variant.idol_card_id,
                    graph_variant.upgrade,
                    detail,
                )
                for detail in evaluation.blockers
            )
        variants.append(
            Plan2NativeMandatoryPItemVariant(
                graph=graph_variant,
                dispatches=tuple(dispatches),
                evaluation=evaluation,
                blockers=tuple(dict.fromkeys(blockers)),
            )
        )
    catalog = Plan2NativeMandatoryPItemCatalog(tuple(variants))
    return Plan2NativeMandatoryPItemCatalogCompilation(
        database=str(database_path),
        catalog=catalog,
        blockers=catalog.blockers,
    )


# Friendly aliases used by audit callers that spell the layer as "mandatory
# items" or "P-item catalog".  All aliases preserve the same static/runtime
# boundary and therefore cannot accidentally claim a live executor.
Plan2NativeMandatoryItemVariant = Plan2NativeMandatoryPItemVariant
Plan2NativeMandatoryItemCatalog = Plan2NativeMandatoryPItemCatalog
Plan2NativeMandatoryItemCatalogCompilation = Plan2NativeMandatoryPItemCatalogCompilation
compile_plan2_native_mandatory_pitem_catalog = compile_plan2_native_mandatory_item_catalog
compile_plan2_native_catalog_mandatory_items = compile_plan2_native_mandatory_item_catalog


def _review_up_predicate(
    trigger_id: str,
    triggers: Mapping[str, Mapping[str, object]],
) -> Plan2MasterReviewUpPredicate | None:
    row = triggers.get(trigger_id)
    if row is None:
        return None
    if not (
        _json_array(row["phase_types_json"], f"{trigger_id}.phaseTypes")
        == (_TRIGGER_PHASE_NONE,)
        and _json_array(row["phase_values_json"], f"{trigger_id}.phaseValues") == ()
        and _json_array(
            row["field_status_check_types_json"],
            f"{trigger_id}.fieldStatusCheckTypes",
        )
        == ()
        and _json_array(
            row["field_status_types_json"], f"{trigger_id}.fieldStatusTypes"
        )
        == (_TRIGGER_REVIEW_UP,)
        and _json_array(
            row["field_status_produce_card_search_ids_json"],
            f"{trigger_id}.fieldStatusProduceCardSearchIds",
        )
        == ()
        and str(row["produce_card_search_id"]) == ""
        and int(row["upper_search_count"]) == 0
        and int(row["lower_search_count"]) == 0
        and str(row["card_move_position_type"]) == _TRIGGER_MOVE_UNKNOWN
        and _json_array(row["effect_types_json"], f"{trigger_id}.effectTypes") == ()
        and str(row["lesson_type"]) == _TRIGGER_LESSON_UNKNOWN
    ):
        return None
    values = _json_array(
        row["field_status_values_json"], f"{trigger_id}.fieldStatusValues"
    )
    if len(values) != 1:
        return None
    threshold = _plain_int(values[0], f"{trigger_id}.threshold", minimum=0)
    return Plan2MasterReviewUpPredicate(trigger_id, threshold)


def _compile_card(
    card: Mapping[str, object],
    *,
    effects: Mapping[str, Mapping[str, object]],
    triggers: Mapping[str, Mapping[str, object]],
    master_card_refs: tuple[tuple[str, int], ...],
    database: Path,
    stamina_catalog: Plan2NativeCatalogStamina,
    status_catalog: Plan2NativeStatusEnchantCatalog,
    review_multiple_catalog: Plan2NativeReviewMultipleCatalog,
    card_trigger_catalog: Plan2NativeCardTriggerCatalog,
    dynamic_block_catalog: Plan2NativeDynamicBlockCatalog,
    lesson_aggressive_catalog: LessonDependAggressiveCatalog,
    review_dynamic_catalog: Plan2NativeReviewDynamicCatalog,
    debuff_catalog: Plan2NativeCatalogDebuff,
    encore_catalog: Plan2NativeStatusEnchantEncoreCatalog,
    effect_chain_catalog: Plan2NativeEffectChainCatalog,
    effect_trigger_catalog: Plan2NativeEffectTriggerCatalog,
    remaining_effect_trigger_catalog: Plan2RemainingEffectTriggerCatalog,
    master_card_categories: tuple[tuple[str, int, str], ...],
    generated_card_catalog: GeneratedCardCatalog,
    status_child_compilation: Plan2NativeStatusChildCompilation,
    status_child_catalog: Plan2NativeStatusChildCatalog,
    triggered_timer_catalog: object | None,
    aggressive_additive_catalog: Plan2NativeAggressiveAdditiveCatalog,
    block_fix_restriction_catalog: Plan2NativeBlockFixRestrictionCatalog,
    extra_turn_catalog: Plan2NativeExtraTurnCatalog,
    stamina_recover_multiple_catalog: Plan2NativeStaminaRecoverMultipleCatalog,
    lesson_block_retention_catalog: LessonDependBlockRetentionCatalog,
    misc_exact_catalog: Plan2NativeCatalogMiscExactCatalog,
    triggered_effect_executor_catalog: Plan2NativeTriggeredEffectExecutorCatalog,
    triggered_status_catalog: Plan2TriggeredStatusEnchantOwnershipAudit,
    triggered_start_play_programs: Mapping[str, Plan2StartPlayStatusProgram],
    triggered_end_turn_programs: Mapping[str, Plan2EndTurnProgram],
    card_move_other_catalog: Plan2NativeCatalogCardMoveOtherCatalog,
    direct_review_refs: tuple[tuple[str, int], ...],
) -> tuple[Plan2NativeCardProgram | None, tuple[Plan2NativeCatalogBlocker, ...]]:
    card_id = str(card["id"])
    upgrade = int(card["upgrade_count"])
    blockers: list[Plan2NativeCatalogBlocker] = []

    def block(code: str, detail: str = "") -> None:
        blockers.append(Plan2NativeCatalogBlocker(code, card_id, upgrade, detail))

    raw = _json_object(card["raw_json"], f"{card_id}@{upgrade}.raw_json")
    cost_type = str(card["cost_type"])
    stamina = int(card["stamina"])
    force_stamina = int(raw.get("forceStamina", 0))
    native_cost = stamina_catalog.cost_by_ref.get((card_id, upgrade))
    if cost_type == _COST_STAMINA:
        if stamina < 0 or force_stamina < 0 or (stamina and force_stamina):
            block(
                "cost-shape-unsupported",
                f"stamina={stamina};forceStamina={force_stamina}",
            )
        else:
            native_cost = Plan2NativeCardCost(
                card_id=card_id,
                upgrade=upgrade,
                cost_type=_COST_STAMINA,
                base_cost=force_stamina or stamina,
                stamina=stamina,
                force_stamina=force_stamina,
                penetrate=force_stamina > 0,
            )
    elif cost_type not in BUFF_COST_TYPES or native_cost is None:
        block("cost-runtime-unbound", cost_type)

    move = str(card["move_position_type"])
    if move == _MOVE_GRAVE:
        move_destination = "grave"
    elif move == _MOVE_LOST:
        move_destination = "lost"
    else:
        move_destination = "grave"
        block("play-destination-unbound", move)

    try:
        hand_move_program = generated_card_catalog.hand_move(card_id, upgrade)
    except StopIteration:
        hand_move_program = None
    raw_move_trigger_type = raw.get("moveEffectTriggerType", _MOVE_UNKNOWN)
    if (
        raw_move_trigger_type != _MOVE_UNKNOWN
        and hand_move_program is None
    ):
        block("move-trigger-type-unbound", str(raw_move_trigger_type))
    for field, code in (
        ("moveProduceExamEffectIds", "move-effect-runtime-unbound"),
        ("moveProduceExamTriggerIds", "move-trigger-runtime-unbound"),
    ):
        values = raw.get(field, [])
        if not isinstance(values, list):
            block("master-card-shape-invalid", field)
        elif values and not (
            hand_move_program is not None
            and (
                (field == "moveProduceExamEffectIds" and tuple(values) == hand_move_program.move_effect_ids)
                or (field == "moveProduceExamTriggerIds" and not values)
            )
        ):
            block(code, ",".join(str(value) for value in values))
    card_status = raw.get("produceCardStatusEnchantId", "")
    if card_status:
        block("card-status-runtime-unbound", str(card_status))

    predicate: Plan2MasterReviewUpPredicate | None = None
    horizon_predicate: Plan2MasterCardTriggerPredicate | None = None
    play_trigger_id = str(card["play_trigger_id"])
    if play_trigger_id:
        try:
            trigger_handoff = card_trigger_catalog.resolve(card_id, upgrade)
        except KeyError:
            trigger_handoff = None
        if (
            trigger_handoff is not None
            and trigger_handoff.predicate.trigger_id == play_trigger_id
        ):
            horizon_predicate = Plan2MasterCardTriggerPredicate(trigger_handoff)
        else:
            predicate = _review_up_predicate(play_trigger_id, triggers)
            if predicate is None:
                block("card-trigger-runtime-unbound", play_trigger_id)

    raw_groups = raw.get("effectGroupIds", [])
    if not isinstance(raw_groups, list) or any(
        not isinstance(group, str) or not group for group in raw_groups
    ):
        block("master-card-shape-invalid", "effectGroupIds")
        effect_group_ids: tuple[str, ...] = ()
    else:
        effect_group_ids = tuple(raw_groups)

    operations: list[Plan2MasterDirectOperation] = []
    zone_operations: list[Plan2MasterZoneOperation] = []
    playing_operations: list[Plan2MasterPlayingOperation] = []
    commands: list[Plan2NativeQueuedAction] = []
    ordered_effect_ids: list[str] = []
    play_effects = _json_array(
        card["play_effects_json"], f"{card_id}@{upgrade}.play_effects_json"
    )
    operation_once_flags: list[bool] = []
    for index, raw_link in enumerate(play_effects):
        if not isinstance(raw_link, Mapping):
            block("master-card-shape-invalid", f"playEffects[{index}]")
            operation_once_flags.append(False)
            continue
        is_once = raw_link.get("isOncePlayEffect", False)
        if type(is_once) is not bool:
            block(
                "master-card-shape-invalid",
                f"playEffects[{index}].isOncePlayEffect",
            )
            operation_once_flags.append(False)
            continue
        operation_once_flags.append(is_once)
    rarity = raw.get("rarity", "")
    play_count_buff_search_ids: list[str] = []
    if rarity in _PLAY_COUNT_BUFF_RARITIES:
        play_count_buff_search_ids.append(_PLAY_COUNT_BUFF_ALL_SEARCH_ID)
        if str(card["category"]) == _ACTIVE_SKILL_CATEGORY:
            play_count_buff_search_ids.append(
                _PLAY_COUNT_BUFF_ACTIVE_SEARCH_ID
            )
    stamina_handoffs = {
        value.slot_index: value
        for value in stamina_catalog.handoffs_for((card_id, upgrade))
        if value.direct
    }
    try:
        status_version = status_catalog.version(card_id, upgrade)
    except KeyError:
        status_version = None
    aggressive_additive_program = aggressive_additive_catalog.get(
        card_id,
        upgrade,
    )
    block_fix_restriction_program = block_fix_restriction_catalog.get(
        card_id,
        upgrade,
    )
    extra_turn_program = extra_turn_catalog.get(card_id, upgrade)
    stamina_recover_multiple_program = stamina_recover_multiple_catalog.get(
        card_id,
        upgrade,
    )
    try:
        lesson_block_retention_version = lesson_block_retention_catalog.version(
            card_id,
            upgrade,
        )
    except KeyError:
        lesson_block_retention_version = None
    try:
        triggered_status_program = triggered_status_catalog.program(
            card_id,
            upgrade,
        )
    except StopIteration:
        triggered_status_program = None
    try:
        card_move_other_program = card_move_other_catalog.resolve(card_id, upgrade)
    except KeyError:
        card_move_other_program = None
    review_multiple_program = review_multiple_catalog.get(card_id, upgrade)
    try:
        dynamic_block_program = dynamic_block_catalog.version(card_id, upgrade)
    except KeyError:
        dynamic_block_program = None
    try:
        lesson_aggressive_version = lesson_aggressive_catalog.version(
            card_id,
            upgrade,
        )
    except KeyError:
        lesson_aggressive_version = None
    try:
        review_dynamic_program = review_dynamic_catalog.version(card_id, upgrade)
    except KeyError:
        review_dynamic_program = None
    encore_program = encore_catalog.get(card_id, upgrade)
    try:
        effect_chain_version = effect_chain_catalog.version(card_id, upgrade)
    except KeyError:
        effect_chain_version = None
    for index, raw_link in enumerate(play_effects):
        if not isinstance(raw_link, Mapping):
            block("master-play-effect-link-invalid", str(index))
            continue
        effect_id = str(raw_link.get("produceExamEffectId", ""))
        if not effect_id or effect_id not in effects:
            block("master-effect-missing", effect_id or f"index={index}")
            continue
        link_trigger = str(raw_link.get("produceExamTriggerId", ""))
        effect = effects[effect_id]
        effect_type = str(effect["effect_type"])
        value1 = int(effect["value1"])
        value2 = int(effect["value2"])
        count = int(effect["effect_count"])
        turn = int(effect["effect_turn"])
        chain_id = str(effect["chain_effect_id"])
        effect_raw = _json_object(effect["raw_json"], f"effect:{effect_id}")
        chain_ids = effect_raw.get("chainProduceExamEffectIds", [])
        try:
            misc_exact_handoff = misc_exact_catalog.occurrence(
                card_id,
                upgrade,
                index,
            )
        except KeyError:
            misc_exact_handoff = None
        try:
            triggered_effect_handoff = triggered_effect_executor_catalog.occurrence(
                card_id,
                upgrade,
                index,
            )
        except KeyError:
            triggered_effect_handoff = None
        if effect_type == EFFECT_TIMER:
            timer_program = next(
                (
                    value
                    for value in (
                        effect_chain_version.timers
                        if effect_chain_version is not None
                        else ()
                    )
                    if value.slot_index == index
                    and value.timer_effect_id == effect_id
                ),
                None,
            )
            if timer_program is None and triggered_timer_catalog is not None:
                try:
                    timer_program = triggered_timer_catalog.occurrence(
                        card_id,
                        upgrade,
                        index,
                    ).timer_program
                except KeyError:
                    timer_program = None
            if timer_program is None or not timer_program.executable:
                block("effect-chain-runtime-unbound", effect_id)
                continue
            operation = Plan2MasterEffectChainOperation(timer_program)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if chain_id or chain_ids:
            detail = ",".join(
                value
                for value in (chain_id, *(str(item) for item in chain_ids))
                if value
            )
            block("effect-chain-runtime-unbound", f"{effect_id}:{detail}")
            continue
        if (
            misc_exact_handoff is not None
            and misc_exact_handoff.effect_id == effect_id
        ):
            operation = Plan2MasterMiscExactOperation(misc_exact_handoff)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            triggered_effect_handoff is not None
            and triggered_effect_handoff.effect_id == effect_id
        ):
            operation = Plan2MasterTriggeredEffectExecutorOperation(
                triggered_effect_handoff
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            triggered_status_program is not None
            and triggered_status_program.slot_index == index
            and triggered_status_program.installer_effect_id == effect_id
        ):
            operation = Plan2MasterTriggeredStatusEnchantOperation(
                triggered_status_program,
                start_play_program=triggered_start_play_programs.get(
                    triggered_status_program.status_enchant_id
                ),
                end_turn_program=triggered_end_turn_programs.get(effect_id),
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type in {CARD_CREATE_ID, CARD_CREATE_SEARCH}:
            try:
                generated_program = generated_card_catalog.program(
                    card_id,
                    upgrade,
                    effect_id,
                )
            except KeyError:
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterGeneratedCreateOperation(generated_program)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == PLAY_COUNT_BUFF:
            try:
                buff_program = generated_card_catalog.program(
                    card_id,
                    upgrade,
                    effect_id,
                )
                following_id = str(
                    play_effects[index + 1]["produceExamEffectId"]
                )
                force_program = generated_card_catalog.program(
                    card_id,
                    upgrade,
                    following_id,
                )
            except (IndexError, KeyError, TypeError):
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterGeneratedBuffForceOperation(
                buff_program,
                force_program,
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == FORCE_PLAY_SEARCH:
            try:
                force_program = generated_card_catalog.program(
                    card_id,
                    upgrade,
                    effect_id,
                )
            except KeyError:
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterGeneratedForceSettlementOperation(force_program)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type in _EFFECT_STAMINA_TYPES:
            handoff = stamina_handoffs.get(index)
            if handoff is None or handoff.effect_id != effect_id:
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterStaminaOperation(handoff)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            aggressive_additive_program is not None
            and aggressive_additive_program.target_slot_index == index
            and aggressive_additive_program.target_effect_id == effect_id
        ):
            operation = Plan2MasterAggressiveAdditiveOperation(
                aggressive_additive_program
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            lesson_block_retention_version is not None
            and lesson_block_retention_version.target_slot_index == index
            and lesson_block_retention_version.target_effect.effect_id == effect_id
        ):
            operation = Plan2MasterLessonDependBlockRetentionOperation(
                lesson_block_retention_version
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            stamina_recover_multiple_program is not None
            and stamina_recover_multiple_program.target_slot_index == index
            and stamina_recover_multiple_program.target_effect_id == effect_id
        ):
            operation = Plan2MasterStaminaRecoverMultipleOperation(
                stamina_recover_multiple_program
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            extra_turn_program is not None
            and extra_turn_program.target_slot_index == index
            and extra_turn_program.target_effect_id == effect_id
        ):
            operation = Plan2MasterExtraTurnOperation(extra_turn_program)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            block_fix_restriction_program is not None
            and block_fix_restriction_program.target_slot_index == index
            and block_fix_restriction_program.target_effect_id == effect_id
        ):
            operation = Plan2MasterBlockFixRestrictionOperation(
                block_fix_restriction_program
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type in {EFFECT_ANTI_DEBUFF, EFFECT_DEBUFF_RECOVER}:
            debuff_handoff = debuff_catalog.handoff_for(
                (card_id, upgrade),
                effect_type=effect_type,
            )
            if (
                debuff_handoff is None
                or debuff_handoff.slot_index != index
                or debuff_handoff.effect_id != effect_id
            ):
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterDebuffOperation(debuff_handoff)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == STATUS_ENCHANT_ENCORE_EFFECT_TYPE:
            if (
                encore_program is None
                or encore_program.slot_index != index
                or encore_program.wrapper_effect_id != effect_id
            ):
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterStatusEnchantEncoreOperation(encore_program)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type in {
            BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE,
            BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE,
        }:
            if (
                dynamic_block_program is None
                or dynamic_block_program.slot_index != index
                or dynamic_block_program.effect_id != effect_id
            ):
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterDynamicBlockOperation(dynamic_block_program)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_LESSON_DEPEND_AGGRESSIVE:
            if (
                lesson_aggressive_version is None
                or lesson_aggressive_version.target_slot_index != index
                or lesson_aggressive_version.target_effect.effect_id != effect_id
            ):
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterLessonDependAggressiveOperation(
                lesson_aggressive_version
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_REVIEW_MULTIPLE:
            if (
                review_multiple_program is None
                or review_multiple_program.slot_index != index
                or review_multiple_program.effect_id != effect_id
            ):
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterReviewMultipleOperation(
                review_multiple_program
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type in REVIEW_DYNAMIC_EFFECT_TYPES:
            if (
                review_dynamic_program is None
                or review_dynamic_program.slot_index != index
                or review_dynamic_program.effect_id != effect_id
            ):
                block("effect-runtime-unbound", f"{effect_type}:{effect_id}")
                continue
            operation = Plan2MasterReviewDynamicOperation(
                review_dynamic_program
            )
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type in {
            _EFFECT_BLOCK,
            _EFFECT_REVIEW,
            _EFFECT_LESSON,
            _EFFECT_LESSON_DEPEND_REVIEW,
            _EFFECT_AGGRESSIVE,
            _EFFECT_LESSON_DEPEND_BLOCK,
        }:
            if value1 < 0 or value2 != 0 or turn != 0 or count < 0:
                block(
                    "scalar-effect-shape-unsupported",
                    f"{effect_id}:{value1}:{value2}:{count}:{turn}",
                )
                continue
            operation = Plan2MasterScalarOperation(
                effect_id,
                effect_type,
                value1,
                value2,
                count,
                turn,
            )
            operations.append(operation)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_PLAYABLE_ADD:
            if value1 != 0 or value2 != 0 or turn != 0 or count < 1:
                block(
                    "playable-add-shape-unsupported",
                    f"{effect_id}:{value1}:{value2}:{count}:{turn}",
                )
                continue
            command = Plan2NativeQueuedAction("add_playable", value=count)
            commands.append(command)
            playing_operations.append(command)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_CARD_DRAW:
            if value1 < 1 or value2 != 0 or turn != 0 or count != 0:
                block(
                    "card-draw-shape-unsupported",
                    f"{effect_id}:{value1}:{value2}:{count}:{turn}",
                )
                continue
            command = Plan2NativeQueuedAction("draw", value=value1)
            commands.append(command)
            playing_operations.append(command)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_HAND_GRAVE_DRAW:
            resolution = resolve_plan2_hand_grave_draw(
                effect_raw,
                plan_type=PLAN_LOGIC,
            )
            if isinstance(resolution, HandGraveDrawUnresolved):
                block(
                    "exact-adapter-shape-unsupported",
                    f"{effect_id}:{resolution.blocker_code}:{resolution.detail}",
                )
                continue
            if not isinstance(resolution, HandGraveDrawExecutable):
                block("exact-adapter-shape-unsupported", effect_id)
                continue
            operation = Plan2MasterHandGraveDrawOperation(effect_id, resolution)
            zone_operations.append(operation)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_CARD_MOVE:
            if (
                card_move_other_program is not None
                and card_move_other_program.target_slot_index == index
                and card_move_other_program.target_effect_id == effect_id
            ):
                operation = Plan2MasterCardMoveOtherOperation(
                    card_move_other_program
                )
                playing_operations.append(operation)
                ordered_effect_ids.append(effect_id)
                continue
            try:
                contract = resolve_plan2_card_move_contract(
                    effect,
                    plan_type=PLAN_LOGIC,
                )
                search = load_produce_card_search(contract.search_id, database)
                operation = Plan2MasterCardMoveOperation(
                    effect_id,
                    contract,
                    search,
                )
            except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
                block(
                    "exact-adapter-shape-unsupported",
                    f"{effect_id}:{type(error).__name__}:{error}",
                )
                continue
            zone_operations.append(operation)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_CARD_UPGRADE:
            try:
                resolution = resolve_plan2_card_upgrade(
                    effect,
                    database=database,
                    plan_type=PLAN_LOGIC,
                )
                if resolution.contract is None:
                    assert resolution.pause is not None
                    raise ValueError(
                        f"{resolution.pause.code}:{resolution.pause.detail}"
                    )
                operation = Plan2MasterCardUpgradeOperation(
                    effect_id,
                    resolution.contract,
                    master_card_refs,
                )
            except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
                block(
                    "exact-adapter-shape-unsupported",
                    f"{effect_id}:{type(error).__name__}:{error}",
                )
                continue
            zone_operations.append(operation)
            playing_operations.append(operation)
            ordered_effect_ids.append(effect_id)
            continue
        if (
            effect_type == _EFFECT_STATUS_ENCHANT
            and effect_id in END_TURN_REMAINING_THREE_DIRECT_TARGETS
        ):
            try:
                status_program = load_plan2_end_turn_remaining_three_program(
                    effect_id,
                    database,
                )
            except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
                block(
                    "exact-adapter-load-failed",
                    f"{effect_id}:{type(error).__name__}:{error}",
                )
            else:
                operation = Plan2MasterRemainingThreeInstallOperation(
                    effect_id,
                    status_program,
                )
                operations.append(operation)
                playing_operations.append(operation)
                ordered_effect_ids.append(effect_id)
            continue
        if effect_type == _EFFECT_STATUS_ENCHANT and status_version is not None:
            try:
                status_program = status_version.install_at(index)
            except KeyError:
                status_program = None
            if status_program is not None and status_program.installer_effect_id == effect_id:
                child_types = {
                    child.effect_type for child in status_program.children
                }
                if not child_types <= _EXECUTABLE_STATUS_CHILD_TYPES:
                    bindings = status_child_catalog.for_install(
                        card_id,
                        upgrade,
                        effect_id,
                    )
                    if (
                        len(bindings) != len(status_program.children)
                        or any(
                            binding.child_index != child_index
                            or binding.child != status_program.children[child_index]
                            for child_index, binding in enumerate(bindings)
                        )
                    ):
                        unsupported = ",".join(
                            sorted(child_types - _EXECUTABLE_STATUS_CHILD_TYPES)
                        )
                        block(
                            "status-enchant-child-runtime-unbound",
                            f"{effect_id}:{unsupported}",
                        )
                        continue
                operation = Plan2MasterStatusEnchantOperation(status_program)
                playing_operations.append(operation)
                ordered_effect_ids.append(effect_id)
                continue
        block("effect-runtime-unbound", f"{effect_type}:{effect_id}")

    if not blockers and len(playing_operations) == len(play_effects):
        triggered_operations: list[Plan2MasterPlayingOperation] = []
        for index, (raw_link, operation) in enumerate(
            zip(play_effects, playing_operations, strict=True)
        ):
            assert isinstance(raw_link, Mapping)
            trigger_id = str(raw_link.get("produceExamTriggerId", ""))
            if not trigger_id:
                triggered_operations.append(operation)
                continue
            if isinstance(operation, Plan2MasterTriggeredEffectExecutorOperation):
                triggered_operations.append(operation)
                continue
            try:
                base_handoff = effect_trigger_catalog.occurrence(
                    card_id,
                    upgrade,
                    index,
                )
                if base_handoff.trigger.trigger_id != trigger_id:
                    raise KeyError((card_id, upgrade, index, trigger_id))
                if base_handoff.target_predicate_executable:
                    handoff: Plan2NativeEffectTriggerHandoff | Plan2RemainingEffectTriggerHandoff = base_handoff
                else:
                    handoff = remaining_effect_trigger_catalog.occurrence(
                        card_id,
                        upgrade,
                        index,
                    )
                    if handoff.trigger_id != trigger_id:
                        raise KeyError((card_id, upgrade, index, trigger_id))
            except KeyError:
                block(
                    "effect-trigger-runtime-unbound",
                    f"{getattr(operation, 'effect_id', '')}:{trigger_id}",
                )
                triggered_operations.append(operation)
                continue
            triggered_operations.append(
                Plan2MasterTriggeredOperation(operation, handoff)
            )
        playing_operations = triggered_operations

    if blockers:
        return None, tuple(dict.fromkeys(blockers))
    unwrapped_playing_operations = tuple(
        value.operation if isinstance(value, Plan2MasterTriggeredOperation) else value
        for value in playing_operations
    )
    has_stamina_operation = any(
        isinstance(value, Plan2MasterStaminaOperation)
        for value in unwrapped_playing_operations
    )
    has_debuff_operation = any(
        isinstance(value, Plan2MasterDebuffOperation)
        for value in unwrapped_playing_operations
    )
    has_encore_operation = any(
        isinstance(value, Plan2MasterStatusEnchantEncoreOperation)
        for value in unwrapped_playing_operations
    )
    has_effect_chain_operation = any(
        isinstance(value, Plan2MasterEffectChainOperation)
        for value in unwrapped_playing_operations
    )
    has_aggressive_additive_operation = any(
        isinstance(value, Plan2MasterAggressiveAdditiveOperation)
        for value in unwrapped_playing_operations
    )
    has_block_fix_restriction_operation = any(
        isinstance(value, Plan2MasterBlockFixRestrictionOperation)
        for value in unwrapped_playing_operations
    )
    has_extra_turn_operation = any(
        isinstance(value, Plan2MasterExtraTurnOperation)
        for value in unwrapped_playing_operations
    )
    has_stamina_recover_multiple_operation = any(
        isinstance(value, Plan2MasterStaminaRecoverMultipleOperation)
        for value in unwrapped_playing_operations
    )
    has_misc_exact_operation = any(
        isinstance(value, Plan2MasterMiscExactOperation)
        for value in unwrapped_playing_operations
    )
    playing_executor = (
        Plan2MasterPlayingExecutor(
            card_id=card_id,
            upgrade=upgrade,
            operations=tuple(playing_operations),
            stamina_catalog=(
                stamina_catalog if has_stamina_operation else None
            ),
            debuff_catalog=(debuff_catalog if has_debuff_operation else None),
            encore_catalog=(encore_catalog if has_encore_operation else None),
            effect_chain_catalog=(
                effect_chain_catalog if has_effect_chain_operation else None
            ),
            aggressive_additive_catalog=(
                aggressive_additive_catalog
            ),
            block_fix_restriction_catalog=(
                block_fix_restriction_catalog
                if has_block_fix_restriction_operation
                else None
            ),
            extra_turn_catalog=(
                extra_turn_catalog if has_extra_turn_operation else None
            ),
            stamina_recover_multiple_catalog=(
                stamina_recover_multiple_catalog
                if has_stamina_recover_multiple_operation
                else None
            ),
            misc_exact_catalog=(
                misc_exact_catalog if has_misc_exact_operation else None
            ),
            status_child_compilation=status_child_compilation,
            master_card_categories=master_card_categories,
            direct_review_refs=direct_review_refs,
            operation_effect_ids=tuple(ordered_effect_ids),
            operation_once_flags=tuple(operation_once_flags),
            play_count_buff_search_ids=tuple(play_count_buff_search_ids),
            runtime_customization_database=str(database),
        )
        if playing_operations
        else None
    )
    executor = Plan2MasterDirectExecutor(
        card_id,
        upgrade,
        tuple(operations),
    )
    executor_id = "master-ordered:" + ",".join(ordered_effect_ids)
    return (
        Plan2NativeCardProgram(
            card_id=card_id,
            upgrade=upgrade,
            category=str(card["category"]),
            effect_group_ids=effect_group_ids,
            cost_type=(
                "native"
                if native_cost is not None and native_cost.cost_type in BUFF_COST_TYPES
                else "none"
                if native_cost is None or native_cost.base_cost == 0
                else "stamina"
            ),
            cost_value=0 if native_cost is None else native_cost.base_cost,
            native_cost=native_cost,
            move_destination=move_destination,
            is_end_turn_lost=bool(raw.get("isEndTurnLost", False)),
            commands=() if playing_executor is not None else tuple(commands),
            native_play_predicate_id=(
                predicate.trigger_id if predicate is not None else ""
            ),
            native_play_predicate=predicate,
            native_horizon_predicate_id=(
                horizon_predicate.trigger_id if horizon_predicate is not None else ""
            ),
            native_horizon_predicate=horizon_predicate,
            native_direct_executor_id=executor_id,
            native_direct_executor=executor,
            native_playing_executor_id=(
                "master-playing-ordered:" + ",".join(ordered_effect_ids)
                if playing_executor is not None
                else ""
            ),
            native_playing_executor=playing_executor,
        ),
        (),
    )


def compile_plan2_native_program_catalog(
    *,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeProgramCatalogCompilation:
    """Compile every current Common+Plan2 version, failing closed per version."""

    global _TRIGGERED_TIMER_OVERLAY_DEPTH
    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        card_rows = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT id, upgrade_count, plan_type, category, stamina,
                       cost_type, cost_value, play_trigger_id,
                       move_position_type, play_effects_json, raw_json
                  FROM card
                 WHERE plan_type IN (?, ?)
                 ORDER BY id, upgrade_count
                """,
                (PLAN_COMMON, PLAN_LOGIC),
            )
        )
        effects = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM effect")
        }
        triggers = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM produce_exam_trigger")
        }
        master_card_refs = tuple(
            (str(row["id"]), int(row["upgrade_count"]))
            for row in connection.execute(
                "SELECT id, upgrade_count FROM card ORDER BY id, upgrade_count"
            )
        )
    master_card_categories = tuple(
        (str(row["id"]), int(row["upgrade_count"]), str(row["category"]))
        for row in card_rows
    )
    direct_review_refs = tuple(
        sorted(
            {
                (str(row["id"]), int(row["upgrade_count"]))
                for row in card_rows
                if any(
                    isinstance(link, Mapping)
                    and str(
                        effects.get(
                            str(link.get("produceExamEffectId", "")),
                            {},
                        ).get("effect_type", "")
                    )
                    == _EFFECT_REVIEW
                    for link in _json_array(
                        row["play_effects_json"],
                        f"{row['id']}@{row['upgrade_count']}.play_effects_json",
                    )
                )
            }
        )
    )

    refs = tuple((str(row["id"]), int(row["upgrade_count"])) for row in card_rows)
    if not refs:
        raise ValueError("Common+Plan2 Master inventory is empty")
    programs: list[Plan2NativeCardProgram] = []
    blockers: list[Plan2NativeCatalogBlocker] = []
    stamina_catalog = compile_plan2_native_catalog_stamina(database=database_path)
    status_compilation = compile_plan2_native_catalog_status_enchant(
        database=database_path
    )
    status_catalog = status_compilation.catalog
    review_multiple_compilation = compile_plan2_native_catalog_review_multiple(
        database=database_path
    )
    review_multiple_catalog = review_multiple_compilation.catalog
    card_trigger_catalog = compile_plan2_native_catalog_card_triggers(
        database=database_path
    )
    dynamic_block_compilation = compile_plan2_native_catalog_block_dynamic(
        database=database_path
    )
    dynamic_block_catalog = dynamic_block_compilation.catalog
    lesson_aggressive_compilation = (
        compile_plan2_native_lesson_depend_aggressive_catalog(
            database=database_path
        )
    )
    lesson_aggressive_catalog = lesson_aggressive_compilation.catalog
    review_dynamic_compilation = compile_plan2_native_catalog_review_dynamic(
        database=database_path
    )
    review_dynamic_catalog = review_dynamic_compilation.catalog
    debuff_catalog = compile_plan2_native_catalog_debuff(
        database=database_path
    )
    encore_compilation = compile_plan2_native_catalog_status_enchant_encore(
        database=database_path
    )
    encore_catalog = encore_compilation.catalog
    effect_chain_compilation = compile_plan2_native_catalog_effect_chains(
        database=database_path
    )
    effect_chain_catalog = effect_chain_compilation.catalog
    effect_trigger_catalog = compile_plan2_native_catalog_effect_triggers(
        database_path
    )
    remaining_effect_trigger_catalog = (
        compile_plan2_native_catalog_effect_triggers_remaining(database_path)
    )
    generated_card_compilation = compile_plan2_native_catalog_generated_cards(
        database_path
    )
    generated_card_catalog = generated_card_compilation.catalog
    status_child_compilation = compile_plan2_native_catalog_status_children(
        database_path
    )
    status_child_catalog = status_child_compilation.catalog
    aggressive_additive_compilation = (
        compile_plan2_native_catalog_aggressive_additive(database_path)
    )
    aggressive_additive_catalog = aggressive_additive_compilation.catalog
    block_fix_restriction_compilation = (
        compile_plan2_native_catalog_block_fix_restriction(database_path)
    )
    block_fix_restriction_catalog = block_fix_restriction_compilation.catalog
    extra_turn_compilation = compile_plan2_native_catalog_extra_turn(database_path)
    extra_turn_catalog = extra_turn_compilation.catalog
    stamina_recover_multiple_compilation = (
        compile_plan2_native_catalog_stamina_recover_multiple(
            database=database_path
        )
    )
    stamina_recover_multiple_catalog = stamina_recover_multiple_compilation.catalog
    lesson_block_retention_compilation = (
        compile_plan2_native_lesson_depend_block_retention_catalog(
            database_path
        )
    )
    lesson_block_retention_catalog = lesson_block_retention_compilation.catalog
    misc_exact_catalog = load_plan2_native_catalog_misc_exact(database_path)
    triggered_effect_executor_catalog = (
        compile_plan2_native_catalog_triggered_effect_executors(database_path)
    )
    triggered_status_catalog = (
        compile_plan2_native_catalog_status_enchant_triggered(database_path)
    )
    start_play_catalog = load_plan2_start_play_trigger_catalog(database_path)
    triggered_start_play_programs = {
        program.status.id: program
        for program in start_play_catalog.programs
        if program.status.id
        in {
            row.status_enchant_id
            for row in triggered_status_catalog.programs
            if row.owner_kind == "start-play-card-draw"
        }
    }
    triggered_end_turn_programs = {
        row.installer_effect_id: load_plan2_end_turn_program(
            row.installer_effect_id,
            database_path,
        )
        for row in triggered_status_catalog.programs
        if row.owner_kind == "end-turn-review"
    }
    card_move_other_compilation = compile_plan2_native_catalog_card_move_other(
        database_path
    )
    if not card_move_other_compilation.fully_compiled:
        raise ValueError("CardMove-other leaf is not fully compiled")
    card_move_other_catalog = card_move_other_compilation.catalog
    triggered_timer_catalog: object | None = None
    if _TRIGGERED_TIMER_OVERLAY_DEPTH == 0:
        _TRIGGERED_TIMER_OVERLAY_DEPTH += 1
        try:
            from .plan2_native_catalog_triggered_timers import (
                compile_plan2_native_catalog_triggered_timers,
            )

            triggered_timer_catalog = compile_plan2_native_catalog_triggered_timers(
                database_path
            )
        finally:
            _TRIGGERED_TIMER_OVERLAY_DEPTH -= 1
    for row in card_rows:
        program, failed = _compile_card(
            row,
            effects=effects,
            triggers=triggers,
            master_card_refs=master_card_refs,
            database=database_path,
            stamina_catalog=stamina_catalog,
            status_catalog=status_catalog,
            review_multiple_catalog=review_multiple_catalog,
            card_trigger_catalog=card_trigger_catalog,
            dynamic_block_catalog=dynamic_block_catalog,
            lesson_aggressive_catalog=lesson_aggressive_catalog,
            review_dynamic_catalog=review_dynamic_catalog,
            debuff_catalog=debuff_catalog,
            encore_catalog=encore_catalog,
            effect_chain_catalog=effect_chain_catalog,
            effect_trigger_catalog=effect_trigger_catalog,
            remaining_effect_trigger_catalog=remaining_effect_trigger_catalog,
            master_card_categories=master_card_categories,
            generated_card_catalog=generated_card_catalog,
            status_child_compilation=status_child_compilation,
            status_child_catalog=status_child_catalog,
            triggered_timer_catalog=triggered_timer_catalog,
            aggressive_additive_catalog=aggressive_additive_catalog,
            block_fix_restriction_catalog=block_fix_restriction_catalog,
            extra_turn_catalog=extra_turn_catalog,
            stamina_recover_multiple_catalog=stamina_recover_multiple_catalog,
            lesson_block_retention_catalog=lesson_block_retention_catalog,
            misc_exact_catalog=misc_exact_catalog,
            triggered_effect_executor_catalog=triggered_effect_executor_catalog,
            triggered_status_catalog=triggered_status_catalog,
            triggered_start_play_programs=triggered_start_play_programs,
            triggered_end_turn_programs=triggered_end_turn_programs,
            card_move_other_catalog=card_move_other_catalog,
            direct_review_refs=direct_review_refs,
        )
        if program is not None:
            programs.append(program)
        blockers.extend(failed)
    return Plan2NativeProgramCatalogCompilation(
        schema_version=PLAN2_NATIVE_PROGRAM_CATALOG_SCHEMA_VERSION,
        master_database=str(database_path),
        universe_refs=refs,
        catalog=Plan2NativeProgramCatalog(
            tuple(programs),
            master_card_refs,
            status_child_compilation,
            generated_card_catalog.hand_move_programs,
        ),
        blockers=tuple(blockers),
    )


__all__ = [
    "PLAN2_MASTER_VERSION_TARGET",
    "PLAN2_NATIVE_PROGRAM_CATALOG_SCHEMA_VERSION",
    "Plan2MasterDirectExecutor",
    "Plan2MasterCardMoveOperation",
    "Plan2MasterCardUpgradeOperation",
    "Plan2MasterHandGraveDrawOperation",
    "Plan2MasterRemainingThreeInstallOperation",
    "Plan2MasterReviewUpPredicate",
    "Plan2MasterScalarOperation",
    "Plan2MasterTriggeredOperation",
    "Plan2MasterZoneExecutor",
    "Plan2NativeCatalogBlocker",
    "Plan2NativeMandatoryPItemResidual",
    "Plan2NativeMandatoryPItemVariant",
    "Plan2NativeMandatoryPItemCatalog",
    "Plan2NativeMandatoryPItemCatalogCompilation",
    "Plan2NativeMandatoryItemVariant",
    "Plan2NativeMandatoryItemCatalog",
    "Plan2NativeMandatoryItemCatalogCompilation",
    "Plan2NativeProgramCatalogCompilation",
    "compile_plan2_native_mandatory_item_catalog",
    "apply_plan2_runtime_customization_operations",
    "compile_plan2_native_mandatory_pitem_catalog",
    "compile_plan2_native_catalog_mandatory_items",
    "compile_plan2_native_program_catalog",
]
