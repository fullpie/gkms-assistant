"""Minimal central Plan2 adapters for integrated direct families.

The standalone modules remain the formula/lifecycle owners.  This module only
maps their immutable boundaries onto :class:`Plan2State`, preserving the card
transaction order: existing stamina modifiers are read during pre-card
payment; direct effects run next; external plan-neutral card settlement is
confirmed; global/per-turn play counts increment last.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from .plan2_force_play_search import (
        ForcePlayEffectContract,
        ForcePlayExecutionInput,
        Plan2ForcePlayPlanResult,
        Plan2ForcePlayQueueExecution,
        Plan2ForcePlayQueuedCommand,
    )

from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX
from .plan2_aggressive_card_trigger import (
    TARGET_TRIGGER_ID as PLAN2_AGGRESSIVE_CARD_TRIGGER_ID,
    AggressiveEvaluation,
    AggressiveEvaluationInput,
    AggressiveCardVersion,
    AggressiveTriggerRow,
    evaluate_aggressive_trigger,
)
from .plan2_anti_debuff import (
    AntiDebuffBlockResult,
    AntiDebuffEffectRow,
    AntiDebuffExecution,
    AntiDebuffRuntime,
    Plan2AntiDebuffUnresolvedBlock,
    UnresolvedExecution,
    execute_plan2_anti_debuff,
    try_block_plan2_status_addition,
)
from .plan2_block_per_use_card_count import (
    Plan2BlockPerUseCardCountEffect,
    Plan2BlockPerUseCardCountRuntime,
    Plan2BlockPerUseCardCountTransition,
    evaluate_plan2_block_per_use_card_count,
)
from .plan2_block_restriction import (
    PLAN_COMMON as PLAN2_BLOCK_RESTRICTION_COMMON_PLAN,
    Plan2BlockRestrictionEffectRow,
    Plan2BlockRestrictionExecution,
    Plan2BlockRestrictionResolutionError,
    Plan2BlockRestrictionRuntime,
    execute_plan2_block_restriction,
)
from .plan2_block_depend_block_consumption_sum import (
    Plan2BlockDependCaptureTransition,
    Plan2BlockDependConsumptionSumProgram,
    Plan2BlockDependConsumptionSumRuntime,
    Plan2BlockDependExecutionTransition,
    Plan2BlockDependInstallTransition,
    Plan2BlockDependScheduler,
    Plan2NoBlockAdmission,
    capture_plan2_block_depend_start_turn_commands,
    execute_captured_plan2_block_depend_commands,
    install_plan2_block_depend_consumption_sum_listener,
)
from .plan2_card_move import (
    PLAN2_CARD_MOVE_LOST_RANDOM_EFFECT_ID,
    PLAN2_CARD_MOVE_LOST_RANDOM_SEARCH_ID,
    CardMoveResolutionError,
    Plan2CardMoveContract,
    Plan2CardMoveResult,
    apply_plan2_card_move,
    assert_plan2_card_move_lost_random_direct_contract,
    assert_plan2_card_move_lost_random_direct_search,
)
from .plan2_card_move_remaining import (
    FORMAL_AFFECTED_VERSION_REFS as PLAN2_REMAINING_CARD_MOVE_REFS,
    RemainingCardMoveContract,
    RemainingCardMoveExecutionContext,
    RemainingCardMoveHandoff,
    RemainingCardMoveResult,
    RemainingCardMoveState,
    simulate_remaining_card_move,
)
from .plan2_card_create_search import (
    CardCreateSearchChanceInput,
    Plan2CardCreateSearchContract,
    Plan2CardCreateSearchResult,
    Plan2CardCreateSearchState,
    apply_plan2_card_create_search,
)
from .plan2_add_grow_effect import (
    Plan2AddGrowDeckAllState,
    Plan2AddGrowProgram,
    Plan2TargetAddGrowStageTransition,
    simulate_plan2_target_add_grow_stage,
)
from .plan2_card_and_effect_aggressive_up6 import (
    AggressiveGateBoundary,
    AggressiveGateEvaluation as AggressiveUp6GateEvaluation,
    AggressiveGateInput as AggressiveUp6GateInput,
    DirectEffectSequenceEvaluation as AggressiveUp6SequenceEvaluation,
    EXACT_TARGET_TRIGGER as PLAN2_AGGRESSIVE_UP6_TRIGGER,
    evaluate_card_play_aggressive_up6_trigger,
    simulate_direct_effect_sequence as simulate_aggressive_up6_direct_effect_sequence,
)
from .plan2_card_trigger_aggressive_up9 import (
    AggressiveUp9GateEvaluation,
    AggressiveUp9GateInput,
    evaluate_card_play_aggressive_up9_trigger,
)
from .plan2_card_upgrade import (
    Plan2CardUpgradeContract,
    Plan2CardUpgradeResolutionError,
    Plan2CardUpgradeResult,
    execute_plan2_card_upgrade,
    resolve_plan2_card_upgrade,
)
from .plan2_card_play_stamina_trigger import (
    DIRECT_EFFECT_ID as PLAN2_CARD_PLAY_STAMINA_EFFECT_ID,
    TARGET_TRIGGER_SHAPE as PLAN2_CARD_PLAY_STAMINA_TRIGGER_SHAPE,
    Plan2CardPlayStaminaEvaluation,
    Plan2CardPlayTriggerShape,
    evaluate_plan2_card_play_stamina_trigger,
)
from .plan2_card_play_playing_search_trigger import (
    CARD_PLAY_TRIGGER_ID as PLAN2_PLAYING_SEARCH_TRIGGER_ID,
    PLAYING_SEARCH_ID as PLAN2_PLAYING_SEARCH_ID,
    Plan2PlayingSearchContract,
    Plan2PlayingSearchProgram,
    Plan2PlayingSearchRequest,
    Plan2PlayingSearchTransition,
    evaluate_plan2_card_play_playing_search,
    install_plan2_card_play_playing_search_listener,
)
from .plan2_end_turn_card_play_aggressive_trigger import (
    Plan2EndTurnCardPlayAggressiveProgram,
    install_plan2_end_turn_card_play_aggressive_listener,
)
from .plan2_end_turn_remaining_one import (
    EXACT_REMAINING_ONE_TRIGGER,
    REMAINING_ONE_TRIGGER_ID as PLAN2_END_TURN_REMAINING_ONE_TRIGGER_ID,
    Plan2EndTurnRemainingOneEvaluation,
    Plan2EndTurnRemainingOneTrigger,
    evaluate_plan2_end_turn_remaining_one,
)
from .plan2_end_turn_remaining_three import (
    Plan2EndTurnRemainingThreeProgram,
    install_plan2_end_turn_remaining_three_listener,
)
from .plan2_end_turn_review_up3 import (
    Plan2EndTurnReviewUp3Program,
    install_plan2_end_turn_review_up3_listener,
)
from .plan2_end_turn_trigger import Plan2EndTurnInstallTransition
from .plan2_exam_effect_timer import (
    Plan2ExamEffectTimerContract,
    resolve_plan2_exam_effect_timer,
)
from .plan2_hand_grave_draw import (
    Plan2HandGraveDrawExecutable,
    Plan2HandGraveDrawTransition,
    execute_plan2_hand_grave_draw,
)
from .plan2_lesson_value_multiple import (
    TARGET_EFFECT_IDS as PLAN2_LESSON_VALUE_MULTIPLE_EFFECT_IDS,
    Plan2LessonParameterMultipleState,
    Plan2LessonValueMultipleContractError,
    Plan2LessonValueMultipleTransition,
    execute_plan2_lesson_value_multiple,
    try_resolve_plan2_lesson_value_multiple,
)
from .plan2_lesson_multiple_depend_review_aggressive import (
    TARGET_CARD_ID as PLAN2_LESSON_DEPEND_REVIEW_AGGRESSIVE_CARD_ID,
    TARGET_EFFECT_ID as PLAN2_LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_ID,
    TARGET_UPGRADES as PLAN2_LESSON_DEPEND_REVIEW_AGGRESSIVE_UPGRADES,
    LessonParameterMultipleDependReviewOrAggressiveState,
    Plan2CardGateSnapshot,
    Plan2LessonMultipleDependReviewAggressiveCatalog,
    Plan2LessonMultipleDependReviewOrAggressiveExecution,
    execute_plan2_lesson_multiple_depend_review_or_aggressive,
    load_plan2_lesson_multiple_depend_review_aggressive_effect,
)
from .plan2_interval2_aggressive_active import (
    AcceptedPlay as Plan2Interval2AggressiveActiveAcceptedPlay,
    Plan2Interval2AggressiveActiveContract,
    Plan2Interval2AggressiveActiveExecution,
    Plan2Interval2AggressiveActiveInstallTransition,
    Plan2Interval2AggressiveActiveProgram,
    Plan2Interval2AggressiveActiveState,
    execute_plan2_interval2_aggressive_active_card_start,
    install_plan2_interval2_aggressive_active_listener,
)
from .plan2_interval2_target_block_group import (
    Plan2Interval2Execution as Plan2Interval2TargetBlockGroupExecution,
    Plan2Interval2InstallTransition as Plan2Interval2TargetBlockGroupInstallTransition,
    Plan2Interval2TargetBlockGroupProgram,
    Plan2Interval2TargetBlockGroupState,
    execute_plan2_interval2_target_block_group_card_start,
    install_plan2_interval2_target_block_group_listener,
)
from .plan2_play_count_interval5 import (
    AcceptedPlay as Plan2PlayCountIntervalAcceptedPlay,
    Plan2PlayCountIntervalExecution,
    Plan2PlayCountIntervalInstallTransition,
    Plan2PlayCountIntervalProgram,
    Plan2PlayCountIntervalState,
    execute_plan2_play_count_interval5_card_start,
    install_plan2_play_count_interval5_listener,
)
from .plan2_move_card_play_aggressive import (
    HandMoveAggressiveRuntimeResult,
    HandMoveAggressiveRuntimeState,
    HandMoveCommitEvent,
    execute_plan2_move_card_play_aggressive,
)
from .plan2_review_per_search_count import (
    Plan2ReviewPerSearchCountContract,
    Plan2ReviewPerSearchCountRequest,
    Plan2ReviewPerSearchCountTransition,
    apply_plan2_review_per_search_count,
)
from .plan2_search_play_card_stamina_consumption_change import (
    SearchPlayCardStaminaApplication,
    SearchPlayCardStaminaContract,
    SearchPlayCardStaminaRuntime,
    apply_plan2_search_play_card_stamina_change,
)
from .plan2_stamina_consumption_add import (
    StaminaConsumptionAddEffect,
    StaminaConsumptionAddExecution,
    StaminaConsumptionAddPaymentContext,
    StaminaConsumptionAddPaymentResult,
    StaminaConsumptionAddRuntime,
    StaminaConsumptionAddSettings,
    evaluate_stamina_payment,
    install_stamina_consumption_add,
)
from .plan2_stamina_recover_fix import (
    StaminaRecoverFixContract,
    StaminaRecoverFixEvaluation,
    StaminaRecoverFixRuntime,
    evaluate_stamina_recover_fix,
)
from .plan2_stamina_up500_block_fix import (
    TARGET_CARD_ID as PLAN2_STAMINA_UP500_CARD_ID,
    TARGET_UPGRADES as PLAN2_STAMINA_UP500_UPGRADES,
    CardExecutionKind as StaminaUp500CardExecutionKind,
    Plan2CardPlayTransition as Plan2StaminaUp500CardTransition,
    Plan2CardRuntime as Plan2StaminaUp500CardRuntime,
    Plan2StaminaUp500BlockFixCatalog,
    execute_plan2_target_card as execute_stamina_up500_target_card,
)
from .plan2_stamina_reduce_card_trigger import (
    Plan2StaminaReduceCardCaptureTransition,
    Plan2StaminaReduceCardEvent,
    Plan2StaminaReduceCardExecutionTransition,
    Plan2StaminaReduceCardInstallTransition,
    Plan2StaminaReduceCardProgram,
    Plan2StaminaReduceCardScheduler,
    StaminaReduceCardSource,
    capture_plan2_stamina_reduce_card_commands,
    execute_captured_plan2_stamina_reduce_card_commands,
    install_plan2_stamina_reduce_card_listener,
)
from .plan2_start_play_trigger import (
    TARGET_TRIGGER_ID as PLAN2_START_PLAY_TRIGGER_ID,
    TARGET_TRIGGER_ROW as PLAN2_START_PLAY_TRIGGER_ROW,
    Plan2StartPlayListener,
    Plan2StartPlayTriggerRow,
    StartPlayCapture,
    capture_plan2_start_play,
)
from .plan2_start_turn_condition_threshold_down import (
    ConditionThresholdDownEvaluation,
    ConditionThresholdDownEvaluationInput,
    ConditionThresholdDownTriggerRow,
    ExecutionMode as ConditionThresholdDownExecutionMode,
    evaluate_condition_threshold_down,
)
from .plan2_start_turn_no_block import (
    TARGET_TRIGGER_ROW as PLAN2_START_TURN_NO_BLOCK_TRIGGER,
    NoBlockEvaluation,
    Plan2StartTurnNoBlockListener,
    PlayOrigin as StartTurnNoBlockPlayOrigin,
    StartTurnCapture as StartTurnNoBlockCapture,
    StartTurnEvaluationInput as StartTurnNoBlockEvaluationInput,
    StartTurnNoBlockTriggerRow,
    capture_plan2_start_turn_no_block,
    evaluate_card_no_block_gate,
    evaluate_status_no_block_listener,
)
from .plan2_start_turn_stamina_up_multiple import (
    CardExecutionKind as StartTurnStaminaExecutionKind,
    Plan2StartTurnSnapshot as StartTurnStaminaSnapshot,
    Plan2StartTurnStaminaEvaluation,
    TARGET_TRIGGER_ROW as PLAN2_START_TURN_STAMINA_UP_MULTIPLE_TRIGGER,
    evaluate_plan2_start_turn_stamina_up_multiple,
)
from .plan2_start_turn_stamina_less_recover import (
    TARGET_CARD_ID as PLAN2_START_TURN_STAMINA_LESS_CARD_ID,
    TARGET_UPGRADES as PLAN2_START_TURN_STAMINA_LESS_UPGRADES,
    StaminaLessEvaluation,
    StaminaLessEvaluationInput,
    StaminaLessRecoverCatalog,
    evaluate_stamina_less_trigger,
    evaluate_stamina_recover_multiple as evaluate_stamina_less_recover_multiple,
)
from .plan2_start_turn_turn_progress_up2 import (
    ExecutionMode as StartTurnProgressUp2ExecutionMode,
    TARGET_TRIGGER_ID as PLAN2_START_TURN_PROGRESS_UP2_TRIGGER_ID,
    TurnProgressUp2Evaluation,
    TurnProgressUp2EvaluationInput,
    evaluate_turn_progress_up2,
)
from .plan2_start_turn_review_stamina_recover import (
    ReviewStaminaCatalog,
    ReviewUp1Evaluation,
    ReviewUp1EvaluationInput,
    StaminaRecoverMultipleEvaluation,
    StaminaRecoverMultipleRuntime,
    evaluate_review_up1_trigger,
    evaluate_stamina_recover_multiple,
)
from .plan2_state import Plan2State
from .plan2_turn_progress_trigger import (
    TARGET_TRIGGER_ID as PLAN2_TURN_PROGRESS_TRIGGER_ID,
    TurnProgressEvaluation,
    TurnProgressEvaluationInput,
    TurnProgressTriggerRow,
    evaluate_turn_progress_trigger,
)
from .plan2_timer_lesson_depend_review import (
    TARGET_VERSION_REFS as PLAN2_TIMER_LESSON_REVIEW_REFS,
    Plan2TimerReviewCardVersion,
    Plan2TimerReviewEnqueue,
    Plan2TimerReviewRuntime,
    Plan2TimerReviewSearchResult,
    Plan2TimerReviewTurnInput,
    Plan2TimerReviewTurnTransition,
    advance_plan2_timer_review_turn,
    enqueue_plan2_timer_review_card,
    search_plan2_timer_review_native_horizon,
)
from .plan3_engine import (
    EFFECT_CARD_DRAW,
    EFFECT_CARD_UPGRADE,
    PHASE_TURN_TIMER,
    Plan3RuntimeEffectEvent,
    Plan3State,
    Plan3TurnStart,
    _install_effect_timer,
    end_plan3_turn,
    load_plan3_exam_settings,
    start_plan3_turn,
)
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeDrawTransition,
    Plan3NativeState,
)


PLAN2_CARD_UPGRADE_HAND_ALL_EFFECT_ID = (
    "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"
)


@dataclass(frozen=True, slots=True)
class Plan2StaminaConsumptionAddInstall:
    before: Plan2State
    after: Plan2State
    standalone: StaminaConsumptionAddExecution
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StaminaPaymentTransition:
    before: Plan2State
    after: Plan2State
    standalone: StaminaConsumptionAddPaymentResult
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StaminaRecoverFixTransition:
    before: Plan2State
    after: Plan2State
    standalone: StaminaRecoverFixEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2BlockPerUseCardCountDirectTransition:
    before: Plan2State
    after: Plan2State
    standalone: Plan2BlockPerUseCardCountTransition
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardPlayCompletion:
    before: Plan2State
    after: Plan2State
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2HandGraveDrawDirectTransition:
    before: Plan3NativeState
    after: Plan3NativeState
    standalone: Plan2HandGraveDrawTransition
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2AntiDebuffInstallTransition:
    before: Plan3NativeState
    after: Plan3NativeState
    standalone: AntiDebuffExecution | UnresolvedExecution[AntiDebuffRuntime]
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2AntiDebuffGateTransition:
    before: Plan3NativeState
    after: Plan3NativeState
    standalone: AntiDebuffBlockResult | Plan2AntiDebuffUnresolvedBlock
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardMoveDirectTransition:
    before: Plan3NativeState
    after: Plan3NativeState
    standalone: Plan2CardMoveResult
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2AggressiveCardTriggerEvaluation:
    before: Plan2State
    after: Plan2State
    standalone: AggressiveEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardPlayAggressiveUp6Evaluation:
    before: Plan2State
    after: Plan2State
    standalone: AggressiveUp6GateEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardPlayAggressiveUp9Evaluation:
    before: Plan2State
    after: Plan2State
    standalone: AggressiveUp9GateEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2AggressiveUp9StaminaReduceCardTransition:
    before: Plan2State
    after: Plan2State
    scheduler_before: Plan2StaminaReduceCardScheduler
    scheduler_after: Plan2StaminaReduceCardScheduler
    gate: Plan2CardPlayAggressiveUp9Evaluation
    capture: Plan2StaminaReduceCardCaptureTransition | None
    execution: Plan2StaminaReduceCardExecutionTransition | None
    install: Plan2StaminaReduceCardInstallTransition | None
    completion: Plan2CardPlayCompletion | None
    resolved: bool
    admitted: bool
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2DirectEffectAggressiveUp6Evaluation:
    before: Plan2State
    after: Plan2State
    standalone: AggressiveUp6SequenceEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardUpgradeDirectTransition:
    before: Plan3NativeState
    after: Plan3NativeState
    standalone: Plan2CardUpgradeResult
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2EndTurnRemainingOneEvaluationTransition:
    before: Plan2State
    after: Plan2State
    standalone: Plan2EndTurnRemainingOneEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StartPlayCaptureTransition:
    before: tuple[Plan2StartPlayListener, ...]
    after: tuple[Plan2StartPlayListener, ...]
    standalone: StartPlayCapture
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StartTurnConditionThresholdDownEvaluation:
    standalone: ConditionThresholdDownEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StartTurnNoBlockCardGateEvaluation:
    before: Plan2State
    after: Plan2State
    standalone: NoBlockEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StartTurnNoBlockCaptureTransition:
    before: Plan2State
    after: Plan2State
    standalone: StartTurnNoBlockCapture
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2BlockDependStartTurnTransition:
    before: Plan2State
    after: Plan2State
    runtime_before: Plan2BlockDependConsumptionSumRuntime
    runtime_after: Plan2BlockDependConsumptionSumRuntime
    scheduler_before: Plan2BlockDependScheduler
    scheduler_after: Plan2BlockDependScheduler
    no_block: NoBlockEvaluation
    capture: Plan2BlockDependCaptureTransition
    execution: Plan2BlockDependExecutionTransition
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StartTurnStaminaUpMultipleEvaluation:
    before: Plan2State
    after: Plan2State
    standalone: Plan2StartTurnStaminaEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StartTurnProgressUp2Evaluation:
    before: Plan2State
    after: Plan2State
    standalone: TurnProgressUp2Evaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2StartTurnReviewStaminaRecoverTransition:
    catalog: ReviewStaminaCatalog
    upgrade: int
    runtime_before: StaminaRecoverMultipleRuntime
    runtime_after: StaminaRecoverMultipleRuntime
    gate: ReviewUp1Evaluation
    recovery: StaminaRecoverMultipleEvaluation | None
    ordered_effect_ids: tuple[str, ...]
    downstream_effect_ids: tuple[str, ...]
    executed: bool
    resolved: bool
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2TurnProgressCardTriggerEvaluation:
    before: Plan2State
    after: Plan2State
    standalone: TurnProgressEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardPlayStaminaTriggerEvaluation:
    before: Plan2State
    after: Plan2State
    standalone: Plan2CardPlayStaminaEvaluation
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleDirectTransition:
    before: Plan2LessonParameterMultipleState
    after: Plan2LessonParameterMultipleState
    standalone: Plan2LessonValueMultipleTransition
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2ReviewPerSearchCountDirectTransition:
    before: Plan2State
    after: Plan2State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    standalone: Plan2ReviewPerSearchCountTransition
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2SearchPlayCardStaminaChangeDirectTransition:
    before: SearchPlayCardStaminaRuntime
    after: SearchPlayCardStaminaRuntime
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    standalone: SearchPlayCardStaminaApplication
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2BlockRestrictionDirectTransition:
    before: Plan2BlockRestrictionRuntime
    after: Plan2BlockRestrictionRuntime
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    standalone: Plan2BlockRestrictionExecution
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectTimerRuntime:
    """Plan2-owned composition of the reused Plan3 timer and native zones."""

    scheduler: Plan3State
    native: Plan3NativeState
    relative_turn: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scheduler, Plan3State):
            raise TypeError("scheduler must be Plan3State")
        if not isinstance(self.native, Plan3NativeState):
            raise TypeError("native must be Plan3NativeState")
        if (
            not isinstance(self.relative_turn, int)
            or isinstance(self.relative_turn, bool)
            or self.relative_turn < 0
        ):
            raise ValueError("relative_turn must be a non-negative integer")
        self.native.assert_plan3_projection(self.scheduler)


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectTimerInstallDifference:
    before_listener_count: int
    after_listener_count: int
    installed_instance_id: str


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectTimerInstallTransition:
    before: Plan2ExamEffectTimerRuntime
    after: Plan2ExamEffectTimerRuntime
    contract: Plan2ExamEffectTimerContract
    difference: Plan2ExamEffectTimerInstallDifference
    child_callback_fired: bool
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectTimerEndTurnTransition:
    before: Plan2ExamEffectTimerRuntime
    after: Plan2ExamEffectTimerRuntime
    turn_counts_before: tuple[int, ...]
    turn_counts_after: tuple[int, ...]
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectTimerChildExecution:
    event: Plan3RuntimeEffectEvent
    timer_effect_id: str
    hand_snapshot_guids: tuple[str, ...]
    before: Plan3NativeState
    after: Plan3NativeState
    draw: Plan3NativeDrawTransition | None
    upgrade: Plan2CardUpgradeDirectTransition | None
    changed_guids: tuple[str, ...]
    callback_fired: bool


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectTimerStartTurnTransition:
    before: Plan2ExamEffectTimerRuntime
    after: Plan2ExamEffectTimerRuntime
    ordinary_draw: Plan3NativeDrawTransition
    scheduler_transition: Plan3TurnStart
    children: tuple[Plan2ExamEffectTimerChildExecution, ...]
    event_trace: tuple[str, ...]


def _add_runtime(state: Plan2State) -> StaminaConsumptionAddRuntime:
    layer = state.stamina_consumption_add_status
    return StaminaConsumptionAddRuntime(
        layers=() if layer is None else (layer,),
        next_status_uid=state.next_status_uid,
    )


def install_plan2_stamina_consumption_add(
    state: Plan2State,
    effect: StaminaConsumptionAddEffect,
    *,
    block_add_status: bool = False,
) -> Plan2StaminaConsumptionAddInstall:
    """Install/merge the one native Add status without copying lifecycle code."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    standalone = install_stamina_consumption_add(
        _add_runtime(state), effect, block_add_status=block_add_status
    )
    after = replace(
        state,
        stamina_consumption_add_status=standalone.after.layer,
        next_status_uid=standalone.after.next_status_uid,
    )
    action = (
        "blocked"
        if not standalone.installed
        else "created"
        if standalone.created_status_uid is not None
        else "merged"
    )
    return Plan2StaminaConsumptionAddInstall(
        state,
        after,
        standalone,
        (f"direct:StaminaConsumptionAdd:{action}",),
    )


def pay_plan2_stamina_cost(
    state: Plan2State,
    context: StaminaConsumptionAddPaymentContext,
    *,
    settings: StaminaConsumptionAddSettings | None = None,
) -> Plan2StaminaPaymentTransition:
    """Apply the exact payment pipeline from the pre-card state."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if (
        context.current_stamina != state.stamina
        or context.max_stamina != state.max_stamina
        or context.current_block != state.block
    ):
        raise ValueError("payment context resources must match Plan2State")
    standalone = evaluate_stamina_payment(
        _add_runtime(state), context, settings=settings
    )
    after = replace(
        state,
        stamina=standalone.new_stamina,
        block=standalone.new_block,
    )
    return Plan2StaminaPaymentTransition(
        state,
        after,
        standalone,
        (
            "pre-card-payment:read:StaminaConsumptionAdd",
            "pre-card-payment:effective-cost",
            "pre-card-payment:block-stamina-split",
        ),
    )


def apply_plan2_stamina_recover_fix(
    state: Plan2State,
    effect: StaminaRecoverFixContract,
    *,
    stamina_recover_restricted: bool = False,
    stamina_recover_add_permil: int | None = None,
    simulate: bool = False,
) -> Plan2StaminaRecoverFixTransition:
    """Route fixed recovery through its exact cap/difference/callback runtime."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    standalone = evaluate_stamina_recover_fix(
        effect,
        StaminaRecoverFixRuntime(
            stamina=state.stamina,
            max_stamina=state.max_stamina,
            stamina_recover_restricted=stamina_recover_restricted,
            stamina_recover_add_permil=stamina_recover_add_permil,
            block=state.block,
        ),
        simulate=simulate,
    )
    after = (
        state
        if not standalone.executable or simulate
        else replace(state, stamina=standalone.runtime_after.stamina)
    )
    return Plan2StaminaRecoverFixTransition(
        state,
        after,
        standalone,
        tuple(f"direct:StaminaRecoverFix:{item}" for item in standalone.trace),
    )


def apply_plan2_block_per_use_card_count_direct(
    state: Plan2State,
    effect: Plan2BlockPerUseCardCountEffect,
    *,
    pipeline: Plan2BlockPerUseCardCountRuntime | None = None,
) -> Plan2BlockPerUseCardCountDirectTransition:
    """Evaluate the direct block effect against the pre-increment global count."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if pipeline is None:
        pipeline = Plan2BlockPerUseCardCountRuntime()
    if not isinstance(pipeline, Plan2BlockPerUseCardCountRuntime):
        raise TypeError("pipeline must be Plan2BlockPerUseCardCountRuntime")
    runtime = replace(
        pipeline,
        block=state.block,
        exam_card_play_count=state.exam_card_play_count,
        turn_card_play_count=state.turn_card_play_count,
        turn_index=state.current_turn - 1,
        aggressive=state.card_play_aggressive,
    )
    standalone = evaluate_plan2_block_per_use_card_count(runtime, effect)
    after = replace(state, block=standalone.block_after)
    return Plan2BlockPerUseCardCountDirectTransition(
        state,
        after,
        standalone,
        tuple(f"direct:BlockPerUseCardCount:{item}" for item in standalone.event_trace),
    )


def apply_plan2_hand_grave_draw_direct(
    state: Plan3NativeState,
    executable: Plan2HandGraveDrawExecutable,
    *,
    hand_limit: int,
    lesson_type: str,
    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    simulate: bool = False,
) -> Plan2HandGraveDrawDirectTransition:
    """Delegate Hand->Grave->draw to the shared immutable native primitive."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    standalone = execute_plan2_hand_grave_draw(
        executable,
        state,
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
        simulate=simulate,
    )
    return Plan2HandGraveDrawDirectTransition(
        state,
        standalone.after,
        standalone,
        tuple(f"direct:HandGraveDraw:{item}" for item in standalone.operation_order),
    )


def install_plan2_anti_debuff_native(
    state: Plan3NativeState,
    effect: AntiDebuffEffectRow,
    *,
    simulate: bool = False,
) -> Plan2AntiDebuffInstallTransition:
    """Install/stack shared AntiDebuff charges on the plan-neutral state."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    standalone = execute_plan2_anti_debuff(
        effect, state.anti_debuff_runtime, simulate=simulate
    )
    after = (
        replace(state, anti_debuff_runtime=standalone.state_after)
        if isinstance(standalone, AntiDebuffExecution)
        else state
    )
    action = standalone.operation if isinstance(standalone, AntiDebuffExecution) else "unresolved"
    return Plan2AntiDebuffInstallTransition(
        state,
        after,
        standalone,
        (f"direct:AntiDebuff:{action}",),
    )


def gate_plan2_status_addition_native(
    state: Plan3NativeState,
    *,
    incoming_effect_type_value: object,
    incoming_target_type: object,
    simulate: bool = False,
) -> Plan2AntiDebuffGateTransition:
    """Run the proven target-classification gate before an incoming add."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    standalone = try_block_plan2_status_addition(
        state.anti_debuff_runtime,
        incoming_effect_type_value=incoming_effect_type_value,
        incoming_target_type=incoming_target_type,
        simulate=simulate,
    )
    after = (
        replace(state, anti_debuff_runtime=standalone.state_after)
        if isinstance(standalone, AntiDebuffBlockResult)
        and standalone.state_after is not state.anti_debuff_runtime
        else state
    )
    action = "blocked" if standalone.blocked else "pass-or-unresolved"
    return Plan2AntiDebuffGateTransition(
        state,
        after,
        standalone,
        (f"pre-add:AntiDebuff:{action}",),
    )


def _assert_integrated_card_move_subset(
    contract: Plan2CardMoveContract,
) -> None:
    assert_plan2_card_move_lost_random_direct_contract(contract)


def apply_plan2_card_move_lost_random_direct(
    state: Plan3NativeState,
    contract: Plan2CardMoveContract,
    *,
    playing_guid: str,
    search: ProduceCardSearchRule | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2CardMoveDirectTransition:
    """Execute only the exact DeckGrave Random(1)->Lost Master subset."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    _assert_integrated_card_move_subset(contract)
    resolved_search = (
        load_produce_card_search(contract.search_id, database)
        if search is None
        else search
    )
    assert_plan2_card_move_lost_random_direct_search(
        contract,
        resolved_search,
    )
    standalone = apply_plan2_card_move(
        state,
        contract,
        playing_guid=playing_guid,
        search=resolved_search,
    )
    expected_moved_count = min(len(standalone.candidates), 1)
    if len(standalone.selected) != expected_moved_count:
        raise CardMoveResolutionError(
            "card-move-random-selection-count-mismatch",
            f"candidates={len(standalone.candidates)};"
            f"selected={len(standalone.selected)};"
            f"expected={expected_moved_count}",
        )
    return Plan2CardMoveDirectTransition(
        state,
        standalone.after,
        standalone,
        (
            "direct:CardMove:DeckGrave:Random(1):Lost",
            "card-move:playing-card-final-settlement:deferred",
        ),
    )


def evaluate_plan2_aggressive_card_trigger_pre_payment(
    state: Plan2State,
    row: AggressiveTriggerRow,
) -> Plan2AggressiveCardTriggerEvaluation:
    """Evaluate only the integrated card-owned Up-3 predicate before payment."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(row, AggressiveTriggerRow):
        raise TypeError("row must be AggressiveTriggerRow")
    if row.id != PLAN2_AGGRESSIVE_CARD_TRIGGER_ID:
        raise ValueError(f"unintegrated-aggressive-card-trigger:{row.id}")
    standalone = evaluate_aggressive_trigger(
        row,
        AggressiveEvaluationInput(
            aggressive_status_value=state.card_play_aggressive,
            global_card_play_count=state.exam_card_play_count,
            turn_card_play_count=state.turn_card_play_count,
        ),
    )
    if not standalone.supported or standalone.fires is None:
        raise ValueError(
            f"aggressive-card-trigger-failed-closed:{row.id}:"
            f"{','.join(standalone.reasons)}"
        )
    return Plan2AggressiveCardTriggerEvaluation(
        state,
        state,
        standalone,
        (
            "pre-payment-build:AggressiveCardTrigger:read-current-value",
            f"pre-payment-build:AggressiveCardTrigger:fires={standalone.fires}",
        ),
    )


def evaluate_plan2_card_play_aggressive_up6_pre_payment(
    state: Plan2State,
    *,
    card_origin: str = "normal",
    is_use_playable_count: bool = True,
    trigger: AggressiveTriggerRow = PLAN2_AGGRESSIVE_UP6_TRIGGER,
) -> Plan2CardPlayAggressiveUp6Evaluation:
    """Capture the exact Up-6 card gate at Playing before payment/effects."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    standalone = evaluate_card_play_aggressive_up6_trigger(
        AggressiveUp6GateInput(
            aggressive_status_value=state.card_play_aggressive,
            boundary=AggressiveGateBoundary.CARD_PLAY_BEFORE_PAYMENT,
            card_origin=card_origin,  # type: ignore[arg-type]
            is_use_playable_count=is_use_playable_count,
            global_card_play_count=state.exam_card_play_count,
            turn_card_play_count=state.turn_card_play_count,
        ),
        trigger,
    )
    if standalone.fail_closed:
        raise ValueError(
            "aggressive-up6-card-gate-failed-closed:"
            + ",".join(standalone.reasons)
        )
    return Plan2CardPlayAggressiveUp6Evaluation(
        state,
        state,
        standalone,
        (
            "playing:snapshot:Aggressive:signed-current",
            "playing:card-gate:before-payment:before-direct-effects",
            f"playing:card-gate:Aggressive>=6:fires={standalone.fires}",
        ),
    )


def evaluate_plan2_card_play_aggressive_up9_pre_payment(
    state: Plan2State,
    *,
    card_origin: str = "normal",
    is_use_playable_count: bool = True,
) -> Plan2CardPlayAggressiveUp9Evaluation:
    """Capture the exact Up-9 card gate before payment and direct effects."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    standalone = evaluate_card_play_aggressive_up9_trigger(
        AggressiveUp9GateInput(
            aggressive_status_value=state.card_play_aggressive,
            card_origin=card_origin,  # type: ignore[arg-type]
            is_use_playable_count=is_use_playable_count,
            global_card_play_count=state.exam_card_play_count,
            turn_card_play_count=state.turn_card_play_count,
        )
    )
    if standalone.fail_closed:
        raise ValueError(
            "aggressive-up9-card-gate-failed-closed:"
            + ",".join(standalone.reasons)
        )
    return Plan2CardPlayAggressiveUp9Evaluation(
        state,
        state,
        standalone,
        (
            "playing:snapshot:Aggressive:signed-current",
            "playing:card-gate:before-payment:before-direct-effects",
            f"playing:card-gate:origin={card_origin}",
            f"playing:card-gate:Aggressive>=9:fires={standalone.fires}",
        ),
    )


def execute_plan2_aggressive_up9_stamina_reduce_card_exact(
    state: Plan2State,
    scheduler: Plan2StaminaReduceCardScheduler,
    program: Plan2StaminaReduceCardProgram,
    event: Plan2StaminaReduceCardEvent,
    *,
    card_origin: str = "normal",
) -> Plan2AggressiveUp9StaminaReduceCardTransition:
    """Run the exact shared card's gate, phase-24 plan, install, and finish."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(scheduler, Plan2StaminaReduceCardScheduler):
        raise TypeError("scheduler must be Plan2StaminaReduceCardScheduler")
    if not isinstance(program, Plan2StaminaReduceCardProgram):
        raise TypeError("program must be Plan2StaminaReduceCardProgram")
    if not isinstance(event, Plan2StaminaReduceCardEvent):
        raise TypeError("event must be Plan2StaminaReduceCardEvent")
    gate = evaluate_plan2_card_play_aggressive_up9_pre_payment(
        state, card_origin=card_origin
    )
    if not gate.standalone.fires:
        return Plan2AggressiveUp9StaminaReduceCardTransition(
            state,
            state,
            scheduler,
            scheduler,
            gate,
            None,
            None,
            None,
            None,
            True,
            False,
            (*gate.event_trace, "card-play:rejected-before-payment"),
        )
    expected_source = {
        "normal": StaminaReduceCardSource.CARD_COST.value,
        "forced": StaminaReduceCardSource.FORCED_CARD_COST.value,
        "extra": StaminaReduceCardSource.EXTRA_CARD_COST.value,
    }[card_origin]
    observed_source = (
        event.source.value
        if isinstance(event.source, StaminaReduceCardSource)
        else event.source
    )
    if observed_source != expected_source:
        return Plan2AggressiveUp9StaminaReduceCardTransition(
            state,
            state,
            scheduler,
            scheduler,
            gate,
            None,
            None,
            None,
            None,
            False,
            True,
            (
                *gate.event_trace,
                "reject:card-origin-and-payment-source-mismatch",
            ),
        )
    capture = capture_plan2_stamina_reduce_card_commands(
        scheduler, event, program=program
    )
    execution = execute_captured_plan2_stamina_reduce_card_commands(capture)
    if not capture.evaluation.supported:
        return Plan2AggressiveUp9StaminaReduceCardTransition(
            state,
            state,
            scheduler,
            scheduler,
            gate,
            capture,
            execution,
            None,
            None,
            False,
            True,
            (*gate.event_trace, *execution.event_trace),
        )
    if not event.payment_succeeded or not event.accepted_by_cost_validator:
        return Plan2AggressiveUp9StaminaReduceCardTransition(
            state,
            state,
            scheduler,
            scheduler,
            gate,
            capture,
            execution,
            None,
            None,
            True,
            False,
            (
                *gate.event_trace,
                *execution.event_trace,
                "card-payment:failed:no-direct-effects-move-or-counts",
            ),
        )
    install = install_plan2_stamina_reduce_card_listener(
        execution.after_scheduler, program
    )
    completion = complete_plan2_card_play_after_move(
        state, card_move_completed=True
    )
    return Plan2AggressiveUp9StaminaReduceCardTransition(
        state,
        completion.after,
        scheduler,
        install.after,
        gate,
        capture,
        execution,
        install,
        completion,
        True,
        True,
        (
            *gate.event_trace,
            "card-payment:accepted:phase24-post-block-delta-captured",
            *execution.event_trace,
            *install.event_trace,
            *completion.event_trace,
            "card-transaction:history-move-counts:exactly-once",
        ),
    )
def evaluate_plan2_direct_effect_aggressive_up6_sequence_build(
    state: Plan2State,
    card: AggressiveCardVersion,
    *,
    card_origin: str = "normal",
    is_use_playable_count: bool = True,
    trigger: AggressiveTriggerRow = PLAN2_AGGRESSIVE_UP6_TRIGGER,
) -> Plan2DirectEffectAggressiveUp6Evaluation:
    """Capture one signed Aggressive snapshot while ordered effects are built."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(card, AggressiveCardVersion):
        raise TypeError("card must be AggressiveCardVersion")
    standalone = simulate_aggressive_up6_direct_effect_sequence(
        card,
        AggressiveUp6GateInput(
            aggressive_status_value=state.card_play_aggressive,
            boundary=AggressiveGateBoundary.DIRECT_EFFECT_SEQUENCE_BUILD,
            card_origin=card_origin,  # type: ignore[arg-type]
            is_use_playable_count=is_use_playable_count,
            global_card_play_count=state.exam_card_play_count,
            turn_card_play_count=state.turn_card_play_count,
        ),
        trigger,
    )
    if not standalone.supported or standalone.fires is None:
        reasons = tuple(
            reason
            for evaluation in standalone.evaluations
            for reason in evaluation.reasons
        )
        raise ValueError(
            "aggressive-up6-effect-gate-failed-closed:"
            + ",".join(dict.fromkeys(reasons))
        )
    return Plan2DirectEffectAggressiveUp6Evaluation(
        state,
        state,
        standalone,
        (
            "direct-effect-sequence:snapshot:Aggressive:signed-current",
            "direct-effect-sequence:build:ordered-slots",
            "direct-effect-sequence:earlier-effects-not-yet-executed",
            f"direct-effect-sequence:Aggressive>=6:fires={standalone.fires}",
        ),
    )


def apply_plan2_card_upgrade_hand_all_direct(
    state: Plan3NativeState,
    contract: Plan2CardUpgradeContract,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2CardUpgradeDirectTransition:
    """Execute only the canonical reachable Hand-All CardUpgrade contract."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    if not isinstance(contract, Plan2CardUpgradeContract):
        raise TypeError("contract must be Plan2CardUpgradeContract")
    canonical = resolve_plan2_card_upgrade(
        PLAN2_CARD_UPGRADE_HAND_ALL_EFFECT_ID,
        database=database,
    )
    if canonical.contract is None or contract != canonical.contract:
        raise Plan2CardUpgradeResolutionError(
            "unintegrated-card-upgrade-shape", contract.effect_id
        )
    standalone = execute_plan2_card_upgrade(state, contract)
    action = standalone.status.value
    return Plan2CardUpgradeDirectTransition(
        state,
        standalone.state_after,
        standalone,
        (f"direct:CardUpgrade:HandAll:{action}",),
    )


def _sync_plan2_timer_scheduler_zones(
    scheduler: Plan3State,
    native: Plan3NativeState,
) -> Plan3State:
    zones = native.projection()
    return replace(
        scheduler,
        hand=zones.hand,
        draw_pile=zones.deck,
        discard_pile=zones.grave,
        lost_pile=zones.lost,
        hold_pile=zones.hold,
    )


def create_plan2_exam_effect_timer_runtime(
    native: Plan3NativeState,
    *,
    turns_remaining: int,
    lesson_type: str,
) -> Plan2ExamEffectTimerRuntime:
    """Create the minimal scalar shell required by the reused timer boundary."""

    if not isinstance(native, Plan3NativeState):
        raise TypeError("native must be Plan3NativeState")
    if (
        not isinstance(turns_remaining, int)
        or isinstance(turns_remaining, bool)
        or turns_remaining <= 0
    ):
        raise ValueError("turns_remaining must be a positive integer")
    if not isinstance(lesson_type, str) or not lesson_type:
        raise ValueError("lesson_type must be non-empty text")
    scheduler = _sync_plan2_timer_scheduler_zones(
        Plan3State(
            turns_remaining=turns_remaining,
            stamina=0,
            max_stamina=0,
            plays_remaining=0,
            lesson_type=lesson_type,
        ),
        native,
    )
    return Plan2ExamEffectTimerRuntime(scheduler, native)


def install_plan2_exam_effect_timer_exact(
    runtime: Plan2ExamEffectTimerRuntime,
    effect: str | object,
    *,
    source_id: str,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ExamEffectTimerInstallTransition:
    """Install one exact timer without executing its child in the same slot."""

    if not isinstance(runtime, Plan2ExamEffectTimerRuntime):
        raise TypeError("runtime must be Plan2ExamEffectTimerRuntime")
    if not isinstance(source_id, str) or not source_id:
        raise ValueError("source_id must be non-empty text")
    contract = resolve_plan2_exam_effect_timer(
        effect,  # type: ignore[arg-type]
        database=database,
    )
    before_count = len(runtime.scheduler.active_status_enchants)
    scheduler = _install_effect_timer(
        runtime.scheduler,
        contract.effect,
        source_id=source_id,
    )
    after = Plan2ExamEffectTimerRuntime(
        scheduler,
        runtime.native,
        runtime.relative_turn,
    )
    listener = scheduler.active_status_enchants[-1]
    return Plan2ExamEffectTimerInstallTransition(
        runtime,
        after,
        contract,
        Plan2ExamEffectTimerInstallDifference(
            before_count,
            len(scheduler.active_status_enchants),
            listener.instance_id,
        ),
        False,
        (
            f"direct:ExamEffectTimer:install:{contract.effect_id}",
            f"difference:active-timer-listener:{before_count}->{before_count + 1}",
            "child-callback:deferred-until-relative-start-turn",
        ),
    )


def end_plan2_exam_effect_timer_turn(
    runtime: Plan2ExamEffectTimerRuntime,
    native_after_settlement: Plan3NativeState,
) -> Plan2ExamEffectTimerEndTurnTransition:
    """Close a turn; timer counts deliberately do not advance at EndTurn."""

    if not isinstance(runtime, Plan2ExamEffectTimerRuntime):
        raise TypeError("runtime must be Plan2ExamEffectTimerRuntime")
    if not isinstance(native_after_settlement, Plan3NativeState):
        raise TypeError("native_after_settlement must be Plan3NativeState")
    if runtime.scheduler.awaiting_turn_start:
        raise ValueError("timer runtime is already awaiting StartTurn")
    scheduler = replace(
        _sync_plan2_timer_scheduler_zones(
            runtime.scheduler,
            native_after_settlement,
        ),
        plays_remaining=0,
    )
    turn_counts_before = tuple(
        listener.turn_count for listener in scheduler.active_status_enchants
    )
    scheduler_after = end_plan3_turn(scheduler)
    native_after = native_after_settlement.reset_turn_used_supports()
    scheduler_after = _sync_plan2_timer_scheduler_zones(
        scheduler_after,
        native_after,
    )
    turn_counts_after = tuple(
        listener.turn_count
        for listener in scheduler_after.active_status_enchants
    )
    if turn_counts_after != turn_counts_before:
        raise AssertionError("EndTurn advanced an ExamEffectTimer count")
    after = Plan2ExamEffectTimerRuntime(
        scheduler_after,
        native_after,
        runtime.relative_turn,
    )
    return Plan2ExamEffectTimerEndTurnTransition(
        runtime,
        after,
        turn_counts_before,
        turn_counts_after,
        (
            "ExamEndTurn:timer-count:unchanged",
            "ExamEndTurn:awaiting-StartTurn",
        ),
    )


def start_plan2_exam_effect_timer_turn(
    runtime: Plan2ExamEffectTimerRuntime,
    *,
    ordinary_draw_count: int,
    hand_limit: int,
    lesson_type: str,
    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ExamEffectTimerStartTurnTransition:
    """Advance the reused StartTurn timer and execute exact native children."""

    if not isinstance(runtime, Plan2ExamEffectTimerRuntime):
        raise TypeError("runtime must be Plan2ExamEffectTimerRuntime")
    if not runtime.scheduler.awaiting_turn_start:
        raise ValueError("timer runtime is not awaiting StartTurn")
    if runtime.native.hand:
        raise ValueError("timer StartTurn requires externally settled empty Hand")
    searches = (
        {}
        if support_card_searches is None
        else dict(support_card_searches)
    )
    source_cards = {card.guid: card for card in runtime.native.all_cards}
    ordinary = runtime.native.draw_to_hand(
        ordinary_draw_count,
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        support_upgrades=support_upgrades,
        support_card_searches=searches,
    )
    authoritative_order = tuple(
        source_cards[guid].ref for guid in ordinary.drawn_guids
    )
    settings = replace(
        load_plan3_exam_settings(),
        turn_start_distribute=ordinary_draw_count,
        hand_limit=hand_limit,
    )
    started = start_plan3_turn(
        runtime.scheduler,
        authoritative_draw_order=authoritative_order,
        settings=settings,
    )
    if not started.supported:
        raise ValueError(
            "plan2-exam-effect-timer-start-failed:"
            + ",".join(started.unsupported_rules)
        )
    events = tuple(started.runtime_effect_events)
    if tuple(event.sequence_index for event in events) != tuple(
        range(len(events))
    ):
        raise ValueError("plan2-exam-effect-timer-event-order-invalid")
    contracts: list[Plan2ExamEffectTimerContract] = []
    for event in events:
        contract = resolve_plan2_exam_effect_timer(
            event.source_id,
            database=database,
        )
        if not (
            event.phase == PHASE_TURN_TIMER
            and event.source_rule_id == f"effect-timer:{contract.effect_id}"
            and event.effect_id == contract.child_effect_id
            and event.effect_type == contract.child_effect_type
        ):
            raise ValueError(
                "plan2-exam-effect-timer-child-event-invalid:"
                f"{event.effect_id}"
            )
        contracts.append(contract)

    upgrade_resolution = resolve_plan2_card_upgrade(
        PLAN2_CARD_UPGRADE_HAND_ALL_EFFECT_ID,
        database=database,
    )
    if upgrade_resolution.contract is None:
        raise Plan2CardUpgradeResolutionError(
            "unintegrated-card-upgrade-shape",
            PLAN2_CARD_UPGRADE_HAND_ALL_EFFECT_ID,
        )
    working = ordinary.after
    children: list[Plan2ExamEffectTimerChildExecution] = []
    trace: list[str] = [
        "ExamStartTurn:ordinary-draw:settled",
        "ExamStartTurn:timer-count:advanced",
    ]
    for event, contract in zip(events, contracts, strict=True):
        before = working
        hand_snapshot = tuple(card.guid for card in before.hand)
        draw: Plan3NativeDrawTransition | None = None
        upgrade: Plan2CardUpgradeDirectTransition | None = None
        if event.effect_type == EFFECT_CARD_DRAW:
            draw = before.draw_to_hand(
                contract.child_value,
                hand_limit=hand_limit,
                lesson_type=lesson_type,
                support_upgrades=support_upgrades,
                support_card_searches=searches,
                effect_draw=True,
            )
            working = draw.after
            changed_guids = draw.drawn_guids
            operation = f"draw:{draw.actual_count}/{draw.requested_count}"
        elif event.effect_type == EFFECT_CARD_UPGRADE:
            upgrade = apply_plan2_card_upgrade_hand_all_direct(
                before,
                upgrade_resolution.contract,
                database=database,
            )
            working = upgrade.after
            changed_guids = upgrade.standalone.selected_guids
            operation = f"upgrade:{len(changed_guids)}"
        else:  # pragma: no cover - prevalidated exact contracts exclude this.
            raise AssertionError(event.effect_type)
        children.append(
            Plan2ExamEffectTimerChildExecution(
                event,
                contract.effect_id,
                hand_snapshot,
                before,
                working,
                draw,
                upgrade,
                changed_guids,
                True,
            )
        )
        trace.extend(
            (
                f"timer-child:{event.sequence_index}:{contract.child_effect_id}:{operation}",
                f"child-callback:{event.sequence_index}:fired",
            )
        )

    scheduler_after = _sync_plan2_timer_scheduler_zones(
        started.after,
        working,
    )
    after = Plan2ExamEffectTimerRuntime(
        scheduler_after,
        working,
        runtime.relative_turn + 1,
    )
    trace.append("ExamStartTurn:triggered-timers:removed")
    return Plan2ExamEffectTimerStartTurnTransition(
        runtime,
        after,
        ordinary,
        started,
        tuple(children),
        tuple(trace),
    )


def evaluate_plan2_end_turn_remaining_one_before_decrement(
    state: Plan2State,
    remaining_turn: int,
    trigger: object = EXACT_REMAINING_ONE_TRIGGER,
) -> Plan2EndTurnRemainingOneEvaluationTransition:
    """Evaluate only the exact EndTurn RemainingTurn<=1 listener gate."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if (
        not isinstance(trigger, Plan2EndTurnRemainingOneTrigger)
        or trigger != EXACT_REMAINING_ONE_TRIGGER
    ):
        raise ValueError(
            "unintegrated-end-turn-remaining-one-trigger:"
            f"{getattr(trigger, 'trigger_id', trigger)}"
        )
    standalone = evaluate_plan2_end_turn_remaining_one(
        remaining_turn,
        trigger,
    )
    return Plan2EndTurnRemainingOneEvaluationTransition(
        state,
        state,
        standalone,
        (
            "ExamEndTurn:before-decrement:RemainingTurn:read",
            f"ExamEndTurn:RemainingTurn<=1:fires={standalone.triggered}",
        ),
    )


def capture_plan2_start_play_exact(
    listeners: Sequence[Plan2StartPlayListener],
    row: Plan2StartPlayTriggerRow = PLAN2_START_PLAY_TRIGGER_ROW,
) -> Plan2StartPlayCaptureTransition:
    """Capture only the canonical unconditional StartPlay listener row."""

    before = tuple(listeners)
    if any(not isinstance(listener, Plan2StartPlayListener) for listener in before):
        raise TypeError("listeners must contain Plan2StartPlayListener")
    if not isinstance(row, Plan2StartPlayTriggerRow):
        raise TypeError("row must be Plan2StartPlayTriggerRow")
    if row != PLAN2_START_PLAY_TRIGGER_ROW:
        raise ValueError(f"unintegrated-start-play-trigger:{row.id}")
    standalone = capture_plan2_start_play(before, row=row)
    if any(
        not evaluation.supported or evaluation.fires is not True
        for evaluation in standalone.evaluations
    ):
        raise ValueError("start-play-trigger-failed-closed")
    return Plan2StartPlayCaptureTransition(
        before,
        standalone.after_count_spend,
        standalone,
        standalone.event_trace,
    )


def evaluate_plan2_start_turn_condition_threshold_down_exact(
    row: ConditionThresholdDownTriggerRow,
    *,
    judge_parameter: int,
    clear_border: int,
    execution_mode: ConditionThresholdDownExecutionMode = (
        ConditionThresholdDownExecutionMode.ORDINARY
    ),
) -> Plan2StartTurnConditionThresholdDownEvaluation:
    """Evaluate the exact settled post-draw StartTurn card gate before payment."""

    if not isinstance(row, ConditionThresholdDownTriggerRow):
        raise TypeError("row must be ConditionThresholdDownTriggerRow")
    standalone = evaluate_condition_threshold_down(
        row,
        ConditionThresholdDownEvaluationInput(
            judge_parameter=judge_parameter,
            clear_border=clear_border,
            execution_mode=execution_mode,
        ),
    )
    if standalone.fail_closed:
        raise ValueError(
            "start-turn-condition-threshold-down-failed-closed:"
            + ",".join(standalone.reasons)
        )
    return Plan2StartTurnConditionThresholdDownEvaluation(
        standalone,
        (
            "ExamStartTurn:turn-spend:completed",
            "ExamStartTurn:ordinary-draw:settled",
            "ExamStartTurn:snapshot:JudgeParameter:ClearBorder:signed-current",
            "card-gate:pre-payment:before-direct-effects",
            f"ConditionThresholdMultipleDown<=1000:fires={standalone.fires}",
        ),
    )


def evaluate_plan2_start_turn_no_block_card_gate_exact(
    state: Plan2State,
    *,
    play_origin: StartTurnNoBlockPlayOrigin = StartTurnNoBlockPlayOrigin.ORDINARY,
    row: StartTurnNoBlockTriggerRow = PLAN2_START_TURN_NO_BLOCK_TRIGGER,
) -> Plan2StartTurnNoBlockCardGateEvaluation:
    """Evaluate ordinary card NoBlock after turn-spend and draw settlement."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(row, StartTurnNoBlockTriggerRow):
        raise TypeError("row must be StartTurnNoBlockTriggerRow")
    standalone = evaluate_card_no_block_gate(
        row,
        StartTurnNoBlockEvaluationInput(
            block=state.block,
            play_origin=play_origin,
        ),
    )
    if standalone.fail_closed:
        raise ValueError(
            "start-turn-no-block-card-gate-failed-closed:"
            + ",".join(standalone.reasons)
        )
    return Plan2StartTurnNoBlockCardGateEvaluation(
        state,
        state,
        standalone,
        (
            "ExamStartTurn:turn-spend:completed",
            "ExamStartTurn:ordinary-draw:settled",
            "ExamStartTurn:snapshot:Block:signed-current",
            "card-gate:ordinary-only:pre-payment",
            f"NoBlock==0:fires={standalone.fires}",
        ),
    )


def capture_plan2_start_turn_no_block_status_exact(
    state: Plan2State,
    listeners: Sequence[Plan2StartTurnNoBlockListener],
    *,
    row: StartTurnNoBlockTriggerRow = PLAN2_START_TURN_NO_BLOCK_TRIGGER,
) -> Plan2StartTurnNoBlockCaptureTransition:
    """Spend exact NoBlock listeners and retain their ordered co-blocked child plan."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    before = tuple(listeners)
    if any(
        not isinstance(listener, Plan2StartTurnNoBlockListener)
        for listener in before
    ):
        raise TypeError("listeners must contain Plan2StartTurnNoBlockListener")
    if not isinstance(row, StartTurnNoBlockTriggerRow):
        raise TypeError("row must be StartTurnNoBlockTriggerRow")
    standalone = capture_plan2_start_turn_no_block(
        before,
        row=row,
        inputs=StartTurnNoBlockEvaluationInput(
            block=state.block,
            boundary="status-command-build",
        ),
    )
    if any(evaluation.fail_closed for evaluation in standalone.evaluations):
        reasons = tuple(
            reason
            for evaluation in standalone.evaluations
            for reason in evaluation.reasons
        )
        raise ValueError(
            "start-turn-no-block-status-failed-closed:"
            + ",".join(dict.fromkeys(reasons))
        )
    if standalone.execute_children() or any(
        not activation.co_blocked for activation in standalone.activations
    ):
        raise ValueError("start-turn-no-block-status-child-must-remain-co-blocked")
    return Plan2StartTurnNoBlockCaptureTransition(
        state,
        state,
        standalone,
        (
            "ExamStartTurn:turn-spend:completed",
            "ExamStartTurn:ordinary-draw:settled",
            "ExamStartTurn:snapshot:Block:signed-current",
            *standalone.event_trace,
            "status-child-plan:retained-not-executed",
        ),
    )


def install_plan2_block_depend_consumption_sum_exact_listener(
    scheduler: Plan2BlockDependScheduler,
    program: Plan2BlockDependConsumptionSumProgram,
    *,
    card_id: str,
    upgrade: int,
) -> Plan2BlockDependInstallTransition:
    """Install only the validated BlockConsumptionSum listener program."""

    if not isinstance(scheduler, Plan2BlockDependScheduler):
        raise TypeError("scheduler must be Plan2BlockDependScheduler")
    if not isinstance(program, Plan2BlockDependConsumptionSumProgram):
        raise TypeError("program must be Plan2BlockDependConsumptionSumProgram")
    return install_plan2_block_depend_consumption_sum_listener(
        scheduler,
        program,
        card_id=card_id,
        upgrade=upgrade,
    )


def execute_plan2_block_depend_consumption_sum_start_turn_exact(
    state: Plan2State,
    runtime: Plan2BlockDependConsumptionSumRuntime,
    scheduler: Plan2BlockDependScheduler,
) -> Plan2BlockDependStartTurnTransition:
    """Bind the proven NoBlock status gate and execute its exact child plan."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(runtime, Plan2BlockDependConsumptionSumRuntime):
        raise TypeError("runtime must be Plan2BlockDependConsumptionSumRuntime")
    if not isinstance(scheduler, Plan2BlockDependScheduler):
        raise TypeError("scheduler must be Plan2BlockDependScheduler")
    if state.block != runtime.block:
        raise ValueError("block-depend-runtime-block-must-match-plan2-state")
    no_block = evaluate_status_no_block_listener(
        PLAN2_START_TURN_NO_BLOCK_TRIGGER,
        StartTurnNoBlockEvaluationInput(
            block=state.block,
            boundary="status-command-build",
        ),
    )
    if no_block.fail_closed:
        raise ValueError(
            "block-depend-no-block-admission-failed-closed:"
            + ",".join(no_block.reasons)
        )
    capture = capture_plan2_block_depend_start_turn_commands(
        scheduler,
        Plan2NoBlockAdmission(no_block.fires is True),
    )
    execution = execute_captured_plan2_block_depend_commands(runtime, capture)
    after = replace(state, block=execution.after_runtime.block)
    return Plan2BlockDependStartTurnTransition(
        state,
        after,
        runtime,
        execution.after_runtime,
        scheduler,
        execution.after_scheduler,
        no_block,
        capture,
        execution,
        (
            "ExamStartTurn:turn-spend:completed",
            "ExamStartTurn:ordinary-draw:settled",
            f"ExamStartTurn:NoBlock==0:fires={no_block.fires}",
            *capture.event_trace,
            *execution.event_trace,
            "ExamStartTurn:BlockDependBlockConsumptionSum:committed",
        ),
    )


def evaluate_plan2_start_turn_stamina_up_multiple_exact(
    state: Plan2State,
    *,
    trigger: object = PLAN2_START_TURN_STAMINA_UP_MULTIPLE_TRIGGER,
    execution_kind: StartTurnStaminaExecutionKind = (
        StartTurnStaminaExecutionKind.ORDINARY
    ),
) -> Plan2StartTurnStaminaUpMultipleEvaluation:
    """Evaluate the exact settled ordinary Stamina>=500permille card gate."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    standalone = evaluate_plan2_start_turn_stamina_up_multiple(
        trigger,
        StartTurnStaminaSnapshot(state.stamina, state.max_stamina),
        execution_kind=execution_kind,
    )
    if not standalone.supported:
        raise ValueError(
            "start-turn-stamina-up-multiple-failed-closed:"
            + ",".join(standalone.reasons)
        )
    return Plan2StartTurnStaminaUpMultipleEvaluation(
        state,
        state,
        standalone,
        (
            "ExamStartTurn:turn-spend:completed",
            "ExamStartTurn:ordinary-draw:settled",
            "ExamStartTurn:snapshot:Stamina:MaxStamina:signed-current",
            "card-gate:ordinary-only:pre-payment",
            f"StaminaUpMultiple>=500:fires={standalone.fires}",
        ),
    )


def evaluate_plan2_start_turn_turn_progress_up2_exact(
    state: Plan2State,
    row: TurnProgressTriggerRow,
    *,
    execution_mode: StartTurnProgressUp2ExecutionMode = (
        StartTurnProgressUp2ExecutionMode.ORDINARY
    ),
    remaining_turn: int | None = None,
    extra_turn: int | None = None,
) -> Plan2StartTurnProgressUp2Evaluation:
    """Evaluate exact one-based CurrentTurn>2 after StartTurn settlement."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(row, TurnProgressTriggerRow):
        raise TypeError("row must be TurnProgressTriggerRow")
    if row.id != PLAN2_START_TURN_PROGRESS_UP2_TRIGGER_ID:
        raise ValueError(f"unintegrated-start-turn-progress-up2:{row.id}")
    standalone = evaluate_turn_progress_up2(
        row,
        TurnProgressUp2EvaluationInput(
            current_turn=state.current_turn,
            remaining_turn=remaining_turn,
            extra_turn=extra_turn,
            execution_mode=execution_mode,
        ),
    )
    if standalone.fail_closed:
        raise ValueError(
            "start-turn-progress-up2-failed-closed:"
            + ",".join(standalone.reasons)
        )
    return Plan2StartTurnProgressUp2Evaluation(
        state,
        state,
        standalone,
        (
            "ExamStartTurn:turn-spend:completed",
            "ExamStartTurn:ordinary-draw:settled",
            "ExamStartTurn:snapshot:CurrentTurn:one-based",
            "card-gate:pre-payment:before-direct-effects",
            f"CurrentTurn>2:fires={standalone.fires}",
        ),
    )


def execute_plan2_start_turn_review_stamina_recover_exact(
    catalog: ReviewStaminaCatalog,
    upgrade: int,
    review_inputs: ReviewUp1EvaluationInput,
    runtime: StaminaRecoverMultipleRuntime,
) -> Plan2StartTurnReviewStaminaRecoverTransition:
    """Evaluate the settled Review gate, then effect zero's recovery."""

    if not isinstance(catalog, ReviewStaminaCatalog):
        raise TypeError("catalog must be ReviewStaminaCatalog")
    if type(upgrade) is not int:
        raise TypeError("upgrade must be an integer")
    if not isinstance(review_inputs, ReviewUp1EvaluationInput):
        raise TypeError("review_inputs must be ReviewUp1EvaluationInput")
    if not isinstance(runtime, StaminaRecoverMultipleRuntime):
        raise TypeError("runtime must be StaminaRecoverMultipleRuntime")
    if not catalog.exact_shape_supported:
        gate = evaluate_review_up1_trigger(
            review_inputs, "unresolved-target-card-catalog"
        )
        return Plan2StartTurnReviewStaminaRecoverTransition(
            catalog,
            upgrade,
            runtime,
            runtime,
            gate,
            None,
            (),
            (),
            False,
            False,
            (
                "ExamStartTurn:settled-pre-card-gate",
                "reject:target-card-catalog-shape-unresolved",
            ),
        )
    try:
        card = catalog.card_version(upgrade)
    except KeyError as error:
        raise ValueError(f"unintegrated-review-stamina-upgrade:{upgrade}") from error
    gate = evaluate_review_up1_trigger(review_inputs, catalog.trigger)
    prefix = (
        "ExamStartTurn:turn-spend:completed",
        "ExamStartTurn:ordinary-effects:settled",
        "ExamStartTurn:snapshot:current-signed-Review",
        f"card-gate:Review>=1:fires={gate.fires}",
    )
    if gate.fail_closed:
        return Plan2StartTurnReviewStaminaRecoverTransition(
            catalog,
            upgrade,
            runtime,
            runtime,
            gate,
            None,
            card.ordered_effect_ids,
            (),
            False,
            False,
            (*prefix, "reject:review-gate-unresolved"),
        )
    if not gate.fires:
        return Plan2StartTurnReviewStaminaRecoverTransition(
            catalog,
            upgrade,
            runtime,
            runtime,
            gate,
            None,
            card.ordered_effect_ids,
            (),
            False,
            True,
            (*prefix, "card-play:rejected-before-payment"),
        )
    recovery = evaluate_stamina_recover_multiple(
        catalog.multiple_effect(upgrade), runtime
    )
    downstream = card.ordered_effect_ids[1:]
    return Plan2StartTurnReviewStaminaRecoverTransition(
        catalog,
        upgrade,
        runtime,
        recovery.runtime_after,
        gate,
        recovery,
        card.ordered_effect_ids,
        downstream,
        recovery.executable,
        recovery.executable,
        (
            *prefix,
            f"card-play:effect:0:{card.multiple_effect_id}:execute",
            *recovery.trace,
            *(
                f"card-play:effect:{index}:downstream:{effect_id}"
                for index, effect_id in enumerate(downstream, start=1)
            ),
        ),
    )


def execute_plan2_add_grow_effect_exact(
    scalar_state: Plan2State,
    deck_state: Plan2AddGrowDeckAllState,
    program: Plan2AddGrowProgram,
) -> Plan2TargetAddGrowStageTransition:
    """Run the exact target's ordered ReviewMultiple/AddGrow handoff."""

    if not isinstance(scalar_state, Plan2State):
        raise TypeError("scalar_state must be Plan2State")
    if not isinstance(deck_state, Plan2AddGrowDeckAllState):
        raise TypeError("deck_state must be Plan2AddGrowDeckAllState")
    if not isinstance(program, Plan2AddGrowProgram):
        raise TypeError("program must be Plan2AddGrowProgram")
    return simulate_plan2_target_add_grow_stage(
        scalar_state, deck_state, program
    )


def evaluate_plan2_turn_progress_card_trigger_pre_payment(
    state: Plan2State,
    row: TurnProgressTriggerRow,
) -> Plan2TurnProgressCardTriggerEvaluation:
    """Evaluate only direct Phase-None CurrentTurn>2 before card payment."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(row, TurnProgressTriggerRow):
        raise TypeError("row must be TurnProgressTriggerRow")
    if row.id != PLAN2_TURN_PROGRESS_TRIGGER_ID:
        raise ValueError(f"unintegrated-turn-progress-card-trigger:{row.id}")
    standalone = evaluate_turn_progress_trigger(
        row,
        TurnProgressEvaluationInput(
            current_turn=state.current_turn,
            global_card_play_count=state.exam_card_play_count,
            turn_card_play_count=state.turn_card_play_count,
        ),
    )
    if not standalone.supported or standalone.fires is None:
        raise ValueError(
            f"turn-progress-card-trigger-failed-closed:{row.id}:"
            f"{','.join(standalone.reasons)}"
        )
    return Plan2TurnProgressCardTriggerEvaluation(
        state,
        state,
        standalone,
        (
            "pre-payment-build:TurnProgress:read-current-turn",
            f"pre-payment-build:CurrentTurn>2:fires={standalone.fires}",
        ),
    )


def evaluate_plan2_card_play_stamina_effect_trigger_pre_payment(
    state: Plan2State,
    *,
    effect_id: str,
    trigger: Plan2CardPlayTriggerShape = (
        PLAN2_CARD_PLAY_STAMINA_TRIGGER_SHAPE
    ),
) -> Plan2CardPlayStaminaTriggerEvaluation:
    """Evaluate the exact stamina-ratio trigger on its exact owning effect."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if type(effect_id) is not str:
        raise TypeError("effect_id must be a string")
    if (
        effect_id != PLAN2_CARD_PLAY_STAMINA_EFFECT_ID
        or not isinstance(trigger, Plan2CardPlayTriggerShape)
        or trigger != PLAN2_CARD_PLAY_STAMINA_TRIGGER_SHAPE
    ):
        raise ValueError(
            "unintegrated-card-play-stamina-trigger-shape:"
            f"{effect_id}"
        )
    standalone = evaluate_plan2_card_play_stamina_trigger(
        trigger,
        candidate=state,
    )
    if not standalone.resolved:
        raise ValueError(
            "card-play-stamina-trigger-failed-closed:"
            f"{','.join(standalone.reasons)}"
        )
    return Plan2CardPlayStaminaTriggerEvaluation(
        state,
        state,
        standalone,
        (
            "pre-payment-capture:CardPlayStamina:read-current-and-max",
            f"pre-payment-capture:StaminaRatio>=500permil:fires={standalone.fires}",
        ),
    )


def apply_plan2_lesson_value_multiple_exact_direct(
    state: Plan2LessonParameterMultipleState,
    effect: object,
    *,
    database: Path = DEFAULT_DATABASE,
    block_add_status: bool = False,
) -> Plan2LessonValueMultipleDirectTransition:
    """Install only the two exact target rows through the shared NIA state."""

    if not isinstance(state, Plan2LessonParameterMultipleState):
        raise TypeError("state must be LessonParameterMultipleState")
    resolution = try_resolve_plan2_lesson_value_multiple(
        effect,  # type: ignore[arg-type]
        database=database,
    )
    if resolution.contract is None:
        assert resolution.pause is not None
        raise Plan2LessonValueMultipleContractError(
            resolution.pause.code,
            resolution.pause.detail or resolution.pause.effect_id,
        )
    contract = resolution.contract
    if contract.effect_id not in PLAN2_LESSON_VALUE_MULTIPLE_EFFECT_IDS:
        raise Plan2LessonValueMultipleContractError(
            "unintegrated-lesson-value-multiple-shape",
            contract.effect_id,
        )
    standalone = execute_plan2_lesson_value_multiple(
        state,
        contract,
        block_add_status=block_add_status,
    )
    action = "installed" if standalone.installed else "blocked"
    return Plan2LessonValueMultipleDirectTransition(
        state,
        standalone.after,
        standalone,
        (
            f"direct:LessonValueMultiple:{contract.effect_id}:{action}",
            *(f"direct:LessonValueMultiple:{item}" for item in standalone.trace),
        ),
    )


def apply_plan2_review_per_search_count_exact_direct(
    state: Plan2State,
    request: Plan2ReviewPerSearchCountRequest,
    contract: Plan2ReviewPerSearchCountContract,
) -> Plan2ReviewPerSearchCountDirectTransition:
    """Bind the exact Deck+Grave evaluator to the Plan2 Review scalar."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(request, Plan2ReviewPerSearchCountRequest):
        raise TypeError("request must be Plan2ReviewPerSearchCountRequest")
    if not isinstance(contract, Plan2ReviewPerSearchCountContract):
        raise TypeError("contract must be Plan2ReviewPerSearchCountContract")
    if request.review_before != state.review:
        raise ValueError("request Review must match Plan2State")
    standalone = apply_plan2_review_per_search_count(request, contract)
    after = replace(state, review=standalone.review_after)
    return Plan2ReviewPerSearchCountDirectTransition(
        state,
        after,
        request.state,
        standalone.after_state,
        standalone,
        (
            f"direct:ReviewPerSearchCount:candidates={standalone.search_count}",
            f"direct:ReviewPerSearchCount:review={standalone.review_before}->{standalone.review_after}",
            "direct:ReviewPerSearchCount:difference:appended"
            if standalone.difference_emitted
            else "direct:ReviewPerSearchCount:difference:skipped",
        ),
    )


def install_plan2_search_play_card_stamina_change_exact_direct(
    runtime: SearchPlayCardStaminaRuntime,
    contract: SearchPlayCardStaminaContract,
    native: Plan3NativeState,
    *,
    playing_card: Plan3NativeCard | None = None,
    status_add_blocked: bool = False,
) -> Plan2SearchPlayCardStaminaChangeDirectTransition:
    """Install/merge the exact Deck-All zero-cost one-use status."""

    if not isinstance(runtime, SearchPlayCardStaminaRuntime):
        raise TypeError("runtime must be SearchPlayCardStaminaRuntime")
    if not isinstance(contract, SearchPlayCardStaminaContract):
        raise TypeError("contract must be SearchPlayCardStaminaContract")
    if not isinstance(native, Plan3NativeState):
        raise TypeError("native must be Plan3NativeState")
    standalone = apply_plan2_search_play_card_stamina_change(
        runtime,
        contract,
        native,
        playing_card=playing_card,
        status_add_blocked=status_add_blocked,
    )
    return Plan2SearchPlayCardStaminaChangeDirectTransition(
        runtime,
        standalone.runtime_after,
        native,
        native,
        standalone,
        (
            f"direct:SearchPlayCardStaminaChange:{standalone.operation}",
            "direct:SearchPlayCardStaminaChange:difference:appended"
            if standalone.effect_difference_appended
            else "direct:SearchPlayCardStaminaChange:difference:skipped",
            "direct:SearchPlayCardStaminaChange:card-zones:unchanged",
        ),
    )


def install_plan2_block_restriction_exact_direct(
    runtime: Plan2BlockRestrictionRuntime,
    native: Plan3NativeState,
    effect: Plan2BlockRestrictionEffectRow,
    *,
    plan_type: str = PLAN2_BLOCK_RESTRICTION_COMMON_PLAN,
) -> Plan2BlockRestrictionDirectTransition:
    """Route the exact direct row through the shared restriction runtime."""

    if not isinstance(runtime, Plan2BlockRestrictionRuntime):
        raise TypeError("runtime must be Plan2BlockRestrictionRuntime")
    if not isinstance(native, Plan3NativeState):
        raise TypeError("native must be Plan3NativeState")
    standalone = execute_plan2_block_restriction(
        effect,
        runtime,
        native.anti_debuff_runtime,
        plan_type=plan_type,
    )
    if not standalone.executable:
        raise Plan2BlockRestrictionResolutionError(
            "unintegrated-block-restriction-shape:"
            + ",".join(standalone.reasons)
        )
    return Plan2BlockRestrictionDirectTransition(
        runtime,
        standalone.after,
        native,
        native,
        standalone,
        (
            f"direct:BlockRestriction:{standalone.operation}",
            *(
                f"direct:BlockRestriction:{item}"
                for item in standalone.trace
            ),
        ),
    )


def install_plan2_end_turn_card_play_aggressive_exact_listener(
    state: Plan2State,
    program: Plan2EndTurnCardPlayAggressiveProgram,
) -> Plan2EndTurnInstallTransition:
    """Install the exact Aggressive>=6 EndTurn listener without re-parsing it."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnCardPlayAggressiveProgram):
        raise TypeError(
            "program must be Plan2EndTurnCardPlayAggressiveProgram"
        )
    return install_plan2_end_turn_card_play_aggressive_listener(state, program)


def install_plan2_end_turn_remaining_three_exact_listener(
    state: Plan2State,
    program: Plan2EndTurnRemainingThreeProgram,
) -> Plan2EndTurnInstallTransition:
    """Install one exact RemainingTurn<=3 listener through shared state."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnRemainingThreeProgram):
        raise TypeError("program must be Plan2EndTurnRemainingThreeProgram")
    return install_plan2_end_turn_remaining_three_listener(state, program)


def install_plan2_card_play_playing_search_exact_listener(
    state: Plan2State,
    program: Plan2PlayingSearchProgram,
) -> tuple[Plan2State, int]:
    """Install one exact Playing-search listener through shared Plan2 state."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2PlayingSearchProgram):
        raise TypeError("program must be Plan2PlayingSearchProgram")
    if (
        program.trigger_id != PLAN2_PLAYING_SEARCH_TRIGGER_ID
        or program.search.id != PLAN2_PLAYING_SEARCH_ID
    ):
        raise ValueError("unintegrated-card-play-playing-search-program")
    return install_plan2_card_play_playing_search_listener(state, program)


def execute_plan2_card_play_playing_search_exact(
    request: Plan2PlayingSearchRequest,
    contract: Plan2PlayingSearchContract,
    **event_hooks: object,
) -> Plan2PlayingSearchTransition:
    """Preserve capture, payment, listener, and direct-effect event order."""

    if not isinstance(request, Plan2PlayingSearchRequest):
        raise TypeError("request must be Plan2PlayingSearchRequest")
    if not isinstance(contract, Plan2PlayingSearchContract):
        raise TypeError("contract must be Plan2PlayingSearchContract")
    if (
        contract.trigger_id != PLAN2_PLAYING_SEARCH_TRIGGER_ID
        or contract.search.id != PLAN2_PLAYING_SEARCH_ID
    ):
        raise ValueError("unintegrated-card-play-playing-search-contract")
    allowed_hooks = {"pay_cost", "apply_direct_effects", "on_lesson_effect"}
    unknown_hooks = set(event_hooks) - allowed_hooks
    if unknown_hooks:
        raise TypeError(f"unknown event hooks: {sorted(unknown_hooks)}")
    return evaluate_plan2_card_play_playing_search(
        request,
        contract,
        **event_hooks,  # type: ignore[arg-type]
    )


def execute_plan2_card_create_search_exact(
    state: Plan2CardCreateSearchState,
    contract: Plan2CardCreateSearchContract,
    *,
    execution_input: CardCreateSearchChanceInput | None = None,
) -> Plan2CardCreateSearchResult:
    """Run the exact Common/Plan2 search-create through its native primitive."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    return apply_plan2_card_create_search(
        state,
        contract,
        execution_input=execution_input,
    )


def execute_plan2_move_card_play_aggressive_exact(
    event: HandMoveCommitEvent,
    runtime: HandMoveAggressiveRuntimeState,
    *,
    used_move_effect_guids: frozenset[str] = frozenset(),
    database: Path = DEFAULT_DATABASE,
) -> HandMoveAggressiveRuntimeResult:
    """Run the exact post-commit HandAdd move-effect boundary."""

    if not isinstance(event, HandMoveCommitEvent):
        raise TypeError("event must be HandMoveCommitEvent")
    if not isinstance(runtime, HandMoveAggressiveRuntimeState):
        raise TypeError("runtime must be HandMoveAggressiveRuntimeState")
    return execute_plan2_move_card_play_aggressive(
        event,
        runtime,
        used_move_effect_guids=used_move_effect_guids,
        database=database,
    )


def install_plan2_end_turn_review_up3_exact_listener(
    state: Plan2State,
    program: Plan2EndTurnReviewUp3Program,
) -> Plan2EndTurnInstallTransition:
    """Install the exact ReviewUp>=3 EndTurn listener on shared state."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2EndTurnReviewUp3Program):
        raise TypeError("program must be Plan2EndTurnReviewUp3Program")
    return install_plan2_end_turn_review_up3_listener(state, program)


def install_plan2_play_count_interval5_exact_listener(
    state: Plan2PlayCountIntervalState,
    program: Plan2PlayCountIntervalProgram,
) -> Plan2PlayCountIntervalInstallTransition:
    """Install the exact permanent phase-23 interval-five listener."""

    if not isinstance(state, Plan2PlayCountIntervalState):
        raise TypeError("state must be Plan2PlayCountIntervalState")
    return install_plan2_play_count_interval5_listener(state, program)


def install_plan2_interval2_aggressive_active_exact_listener(
    state: Plan2Interval2AggressiveActiveState,
    program: Plan2Interval2AggressiveActiveProgram,
) -> Plan2Interval2AggressiveActiveInstallTransition:
    """Install the exact permanent interval-two ActiveSkill listener."""

    if not isinstance(state, Plan2Interval2AggressiveActiveState):
        raise TypeError("state must be Plan2Interval2AggressiveActiveState")
    if not isinstance(program, Plan2Interval2AggressiveActiveProgram):
        raise TypeError("program must be Plan2Interval2AggressiveActiveProgram")
    return install_plan2_interval2_aggressive_active_listener(state, program)


def execute_plan2_interval2_aggressive_active_exact(
    state: Plan2Interval2AggressiveActiveState,
    event: object,
    contract: Plan2Interval2AggressiveActiveContract,
) -> Plan2Interval2AggressiveActiveExecution:
    """Run the exact modulo-two/Aggressive/Playing ActiveSkill gate."""

    if not isinstance(state, Plan2Interval2AggressiveActiveState):
        raise TypeError("state must be Plan2Interval2AggressiveActiveState")
    if not isinstance(event, Plan2Interval2AggressiveActiveAcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    if not isinstance(contract, Plan2Interval2AggressiveActiveContract):
        raise TypeError("contract must be Plan2Interval2AggressiveActiveContract")
    transition = execute_plan2_interval2_aggressive_active_card_start(
        state,
        event,
        contract,
    )
    if not transition.executable:
        raise ValueError(
            "unintegrated-interval2-aggressive-active-event:"
            + ",".join(transition.unresolved)
        )
    return transition


def install_plan2_interval2_target_block_group_exact_listener(
    state: Plan2Interval2TargetBlockGroupState,
    program: Plan2Interval2TargetBlockGroupProgram,
) -> Plan2Interval2TargetBlockGroupInstallTransition:
    """Install the exact permanent interval-two Target/Block-group listener."""

    if not isinstance(state, Plan2Interval2TargetBlockGroupState):
        raise TypeError("state must be Plan2Interval2TargetBlockGroupState")
    if not isinstance(program, Plan2Interval2TargetBlockGroupProgram):
        raise TypeError("program must be Plan2Interval2TargetBlockGroupProgram")
    return install_plan2_interval2_target_block_group_listener(state, program)


def execute_plan2_interval2_target_block_group_exact(
    state: Plan2Interval2TargetBlockGroupState,
    event: object,
    program: Plan2Interval2TargetBlockGroupProgram,
    **event_hooks: object,
) -> Plan2Interval2TargetBlockGroupExecution:
    """Run exact Target/Block-group capture, CardCreateId child, and SpendCount."""

    if not isinstance(state, Plan2Interval2TargetBlockGroupState):
        raise TypeError("state must be Plan2Interval2TargetBlockGroupState")
    if not isinstance(event, Plan2PlayCountIntervalAcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    if not isinstance(program, Plan2Interval2TargetBlockGroupProgram):
        raise TypeError("program must be Plan2Interval2TargetBlockGroupProgram")
    unknown_hooks = set(event_hooks).difference(
        {"guid_tokens", "guid_allocator", "pay_cost", "apply_direct_effects"}
    )
    if unknown_hooks:
        raise TypeError(f"unknown event hooks: {sorted(unknown_hooks)}")
    transition = execute_plan2_interval2_target_block_group_card_start(
        state,
        event,
        program,
        **event_hooks,  # type: ignore[arg-type]
    )
    if not transition.executable:
        raise ValueError(
            "unintegrated-interval2-target-block-group-event:"
            + ",".join(transition.unresolved)
        )
    return transition


def execute_plan2_play_count_interval5_exact(
    state: Plan2PlayCountIntervalState,
    event: Plan2PlayCountIntervalAcceptedPlay,
    program: Plan2PlayCountIntervalProgram,
) -> Plan2PlayCountIntervalExecution:
    """Run exact phase-23 counting before direct effects and final movement."""

    if not isinstance(state, Plan2PlayCountIntervalState):
        raise TypeError("state must be Plan2PlayCountIntervalState")
    if not isinstance(event, Plan2PlayCountIntervalAcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    transition = execute_plan2_play_count_interval5_card_start(
        state, event, program
    )
    if not transition.executable:
        raise ValueError(
            "unintegrated-play-count-interval5-event:"
            + ",".join(transition.unresolved)
        )
    return transition


def plan_plan2_force_play_search_exact(
    native_state: Plan3NativeState,
    plan2_state: Plan2State,
    contract: ForcePlayEffectContract,
    *,
    playing_guid: str,
    execution_input: ForcePlayExecutionInput | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ForcePlayPlanResult:
    """Plan only the exact R-Hand Random/Limit2 force-play search."""

    from .plan2_force_play_search import (
        EFFECT_ID as exact_effect_id,
        ForcePlayEffectContract,
        plan_force_play_card_search,
    )

    if not isinstance(native_state, Plan3NativeState):
        raise TypeError("native_state must be Plan3NativeState")
    if not isinstance(plan2_state, Plan2State):
        raise TypeError("plan2_state must be Plan2State")
    if not isinstance(contract, ForcePlayEffectContract):
        raise TypeError("contract must be ForcePlayEffectContract")
    if contract.row.effect_id != exact_effect_id:
        raise ValueError(
            f"unintegrated-force-play-search-effect:{contract.row.effect_id}"
        )
    return plan_force_play_card_search(
        native_state,
        plan2_state,
        contract,
        playing_guid=playing_guid,
        execution_input=execution_input,
        database=database,
    )


def execute_plan2_force_play_queue_exact(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    commands: Sequence[Plan2ForcePlayQueuedCommand],
    *,
    nested_by_guid: Mapping[
        str, tuple[Plan2ForcePlayQueuedCommand, ...]
    ] | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ForcePlayQueueExecution:
    """Settle exact forced UsePool commands with counts/history/move once."""

    from .plan2_force_play_search import (
        Plan2ForcePlayQueuedCommand,
        execute_plan2_force_play_queue,
    )

    if not isinstance(plan2_state, Plan2State):
        raise TypeError("plan2_state must be Plan2State")
    if not isinstance(native_state, Plan3NativeState):
        raise TypeError("native_state must be Plan3NativeState")
    if not isinstance(commands, Sequence) or any(
        not isinstance(command, Plan2ForcePlayQueuedCommand)
        for command in commands
    ):
        raise TypeError("commands must contain Plan2ForcePlayQueuedCommand")
    return execute_plan2_force_play_queue(
        plan2_state,
        native_state,
        commands,
        nested_by_guid=nested_by_guid,
        database=database,
    )


@dataclass(frozen=True, slots=True)
class Plan2LessonDependReviewAggressiveTransition:
    before: LessonParameterMultipleDependReviewOrAggressiveState
    after: LessonParameterMultipleDependReviewOrAggressiveState
    gate: Plan2CardGateSnapshot
    upgrade: int
    execution: Plan2LessonMultipleDependReviewOrAggressiveExecution | None
    admitted: bool
    reasons: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.admitted and self.execution is not None


def execute_plan2_lesson_depend_review_aggressive_exact(
    catalog: Plan2LessonMultipleDependReviewAggressiveCatalog,
    state: LessonParameterMultipleDependReviewOrAggressiveState,
    gate: Plan2CardGateSnapshot,
    *,
    upgrade: int,
    block_add_status: bool = False,
) -> Plan2LessonDependReviewAggressiveTransition:
    """Apply the exact five-turn status after the signed pre-payment gate."""

    if not isinstance(catalog, Plan2LessonMultipleDependReviewAggressiveCatalog):
        raise TypeError(
            "catalog must be Plan2LessonMultipleDependReviewAggressiveCatalog"
        )
    if not isinstance(
        state, LessonParameterMultipleDependReviewOrAggressiveState
    ):
        raise TypeError("state must be dependent Review/Aggressive state")
    if not isinstance(gate, Plan2CardGateSnapshot):
        raise TypeError("gate must be Plan2CardGateSnapshot")
    if (
        type(upgrade) is not int
        or upgrade not in PLAN2_LESSON_DEPEND_REVIEW_AGGRESSIVE_UPGRADES
    ):
        raise ValueError(
            f"unintegrated-lesson-depend-review-aggressive-upgrade:{upgrade}"
        )
    version = next(
        (
            row
            for row in catalog.card_versions
            if row.card_id == PLAN2_LESSON_DEPEND_REVIEW_AGGRESSIVE_CARD_ID
            and row.upgrade == upgrade
        ),
        None,
    )
    if (
        version is None
        or version.target_effect_id
        != PLAN2_LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_ID
    ):
        raise ValueError("unintegrated-lesson-depend-review-aggressive-version")
    if not gate.admitted:
        return Plan2LessonDependReviewAggressiveTransition(
            state,
            state,
            gate,
            upgrade,
            None,
            False,
            ("signed-aggressive-below-9",),
        )
    effect = load_plan2_lesson_multiple_depend_review_aggressive_effect(
        PLAN2_LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_ID,
        database=Path(catalog.database),
    )
    execution = execute_plan2_lesson_multiple_depend_review_or_aggressive(
        state,
        effect,
        block_add_status=block_add_status,
    )
    return Plan2LessonDependReviewAggressiveTransition(
        state,
        execution.after,
        gate,
        upgrade,
        execution,
        True,
    )


@dataclass(frozen=True, slots=True)
class Plan2StartTurnStaminaLessRecoverTransition:
    catalog: StaminaLessRecoverCatalog
    upgrade: int
    trigger: StaminaLessEvaluation
    recovery: StaminaRecoverMultipleEvaluation | None
    before: StaminaRecoverMultipleRuntime
    after: StaminaRecoverMultipleRuntime
    event_trace: tuple[str, ...]
    reasons: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return (
            self.trigger.supported
            and self.trigger.fires is not None
            and (self.recovery is None or self.recovery.executable)
            and not self.reasons
        )


def execute_plan2_start_turn_stamina_less_recover_exact(
    catalog: StaminaLessRecoverCatalog,
    inputs: StaminaLessEvaluationInput,
    runtime: StaminaRecoverMultipleRuntime,
    *,
    upgrade: int,
) -> Plan2StartTurnStaminaLessRecoverTransition:
    """Evaluate settled post-draw Stamina<=500‰ then ordered recovery."""

    if not isinstance(catalog, StaminaLessRecoverCatalog):
        raise TypeError("catalog must be StaminaLessRecoverCatalog")
    if not isinstance(inputs, StaminaLessEvaluationInput):
        raise TypeError("inputs must be StaminaLessEvaluationInput")
    if not isinstance(runtime, StaminaRecoverMultipleRuntime):
        raise TypeError("runtime must be StaminaRecoverMultipleRuntime")
    if (
        type(upgrade) is not int
        or upgrade not in PLAN2_START_TURN_STAMINA_LESS_UPGRADES
    ):
        raise ValueError(f"unintegrated-start-turn-stamina-less-upgrade:{upgrade}")
    if not catalog.exact_shape_supported:
        raise ValueError("unintegrated-start-turn-stamina-less-catalog")
    version = catalog.card_version(upgrade)
    if version.card_id != PLAN2_START_TURN_STAMINA_LESS_CARD_ID:
        raise ValueError("unintegrated-start-turn-stamina-less-version")
    trigger = evaluate_stamina_less_trigger(inputs, catalog.trigger)
    if trigger.fail_closed:
        return Plan2StartTurnStaminaLessRecoverTransition(
            catalog,
            upgrade,
            trigger,
            None,
            runtime,
            runtime,
            ("settled-post-draw-trigger:fail-closed",),
            trigger.reasons,
        )
    if not trigger.fires:
        return Plan2StartTurnStaminaLessRecoverTransition(
            catalog,
            upgrade,
            trigger,
            None,
            runtime,
            runtime,
            ("settled-post-draw-trigger:not-fired",),
        )
    recovery = evaluate_stamina_less_recover_multiple(
        catalog.multiple_effect(upgrade), runtime
    )
    if not recovery.executable:
        return Plan2StartTurnStaminaLessRecoverTransition(
            catalog,
            upgrade,
            trigger,
            recovery,
            runtime,
            runtime,
            (
                "settled-post-draw-trigger:fired",
                "ordered-child:lesson-depend-block",
                "ordered-child:stamina-recover-multiple:fail-closed",
            ),
            (recovery.reason or "stamina-recovery-unresolved",),
        )
    return Plan2StartTurnStaminaLessRecoverTransition(
        catalog,
        upgrade,
        trigger,
        recovery,
        runtime,
        recovery.runtime_after,
        (
            "settled-post-draw-trigger:fired",
            "ordered-child:lesson-depend-block",
            "ordered-child:stamina-recover-multiple",
            *recovery.trace,
        ),
    )


def execute_plan2_stamina_up500_block_fix_exact(
    catalog: Plan2StaminaUp500BlockFixCatalog,
    runtime: Plan2StaminaUp500CardRuntime,
    *,
    upgrade: int,
    execution_kind: StaminaUp500CardExecutionKind | str = (
        StaminaUp500CardExecutionKind.ORDINARY
    ),
) -> Plan2StaminaUp500CardTransition:
    """Execute only the four exact Stamina>=500/BlockFix card versions."""

    if not isinstance(catalog, Plan2StaminaUp500BlockFixCatalog):
        raise TypeError("catalog must be Plan2StaminaUp500BlockFixCatalog")
    if not isinstance(runtime, Plan2StaminaUp500CardRuntime):
        raise TypeError("runtime must be Plan2CardRuntime")
    if type(upgrade) is not int or upgrade not in PLAN2_STAMINA_UP500_UPGRADES:
        raise ValueError(f"unintegrated-stamina-up500-upgrade:{upgrade}")
    transition = execute_stamina_up500_target_card(
        catalog,
        runtime,
        card_id=PLAN2_STAMINA_UP500_CARD_ID,
        upgrade_count=upgrade,
        execution_kind=execution_kind,
    )
    if transition.committed and transition.reasons:
        raise ValueError("stamina-up500-transition-committed-with-reasons")
    return transition


def enqueue_plan2_timer_lesson_depend_review_exact(
    runtime: Plan2TimerReviewRuntime,
    version: Plan2TimerReviewCardVersion,
    *,
    source_guid: str,
    review_snapshot: int,
) -> Plan2TimerReviewEnqueue:
    """Capture exact card gates and queue exact Review-dependent timers."""

    if not isinstance(runtime, Plan2TimerReviewRuntime):
        raise TypeError("runtime must be Plan2TimerReviewRuntime")
    if not isinstance(version, Plan2TimerReviewCardVersion):
        raise TypeError("version must be Plan2TimerReviewCardVersion")
    if version.ref not in PLAN2_TIMER_LESSON_REVIEW_REFS:
        raise ValueError(f"unintegrated-timer-review-version:{version.ref}")
    return enqueue_plan2_timer_review_card(
        runtime,
        version,
        source_guid=source_guid,
        review_snapshot=review_snapshot,
    )


def advance_plan2_timer_lesson_depend_review_exact(
    runtime: Plan2TimerReviewRuntime,
    turn_input: Plan2TimerReviewTurnInput,
) -> Plan2TimerReviewTurnTransition:
    """Advance the exact relative-turn queue through its native phase order."""

    if not isinstance(runtime, Plan2TimerReviewRuntime):
        raise TypeError("runtime must be Plan2TimerReviewRuntime")
    if not isinstance(turn_input, Plan2TimerReviewTurnInput):
        raise TypeError("turn_input must be Plan2TimerReviewTurnInput")
    return advance_plan2_timer_review_turn(runtime, turn_input)


def search_plan2_timer_lesson_depend_review_exact(
    initial: Plan2TimerReviewRuntime,
    turn_inputs: Sequence[Plan2TimerReviewTurnInput],
) -> Plan2TimerReviewSearchResult:
    """Persist exact queued timer identity across a bounded search horizon."""

    if not isinstance(initial, Plan2TimerReviewRuntime):
        raise TypeError("initial must be Plan2TimerReviewRuntime")
    if not isinstance(turn_inputs, Sequence) or any(
        not isinstance(item, Plan2TimerReviewTurnInput) for item in turn_inputs
    ):
        raise TypeError("turn_inputs must contain Plan2TimerReviewTurnInput")
    return search_plan2_timer_review_native_horizon(initial, turn_inputs)


def execute_plan2_remaining_card_move_exact(
    state: RemainingCardMoveState,
    contract: RemainingCardMoveContract,
    handoff: RemainingCardMoveHandoff,
    context: RemainingCardMoveExecutionContext,
    *,
    database: Path = DEFAULT_DATABASE,
) -> RemainingCardMoveResult:
    """Execute the six exact remaining CardMove versions and three handoffs."""

    if not isinstance(state, RemainingCardMoveState):
        raise TypeError("state must be RemainingCardMoveState")
    if not isinstance(contract, RemainingCardMoveContract):
        raise TypeError("contract must be RemainingCardMoveContract")
    if not isinstance(handoff, RemainingCardMoveHandoff):
        raise TypeError("handoff must be RemainingCardMoveHandoff")
    if not isinstance(context, RemainingCardMoveExecutionContext):
        raise TypeError("context must be RemainingCardMoveExecutionContext")
    if contract.version_ref not in PLAN2_REMAINING_CARD_MOVE_REFS:
        raise ValueError(
            f"unintegrated-remaining-card-move-version:{contract.version_ref}"
        )
    return simulate_remaining_card_move(
        state,
        contract,
        handoff,
        context,
        database=database,
    )


def evaluate_plan2_effect_triggers_aggressive_up9_exact(
    card: object,
    inputs: object,
    triggers: Mapping[str, object],
) -> object:
    """Build the exact complementary Aggressive>=9 direct-effect slots."""

    from .plan2_effect_triggers_aggressive_up9 import (
        EffectTriggerEvaluationInput,
        TARGET_CARD_LEVEL_REFS,
        TargetCardVersion,
        simulate_effect_trigger_build,
    )

    if not isinstance(card, TargetCardVersion):
        raise TypeError("card must be the exact AggressiveUp9 TargetCardVersion")
    if not isinstance(inputs, EffectTriggerEvaluationInput):
        raise TypeError("inputs must be EffectTriggerEvaluationInput")
    if card.ref not in TARGET_CARD_LEVEL_REFS:
        raise ValueError(f"unintegrated-aggressive-up9-effect-version:{card.ref}")
    if not isinstance(triggers, Mapping):
        raise TypeError("triggers must be a trigger-row mapping")
    return simulate_effect_trigger_build(card, inputs, triggers)


def evaluate_plan2_effect_triggers_stamina_thresholds_exact(
    catalog: object,
    *,
    upgrade_count: int,
    current_stamina: int,
    max_stamina: int,
    card_origin: str = "normal",
) -> object:
    """Re-read current/max stamina for each exact 500/800/1000 gate."""

    from .plan2_effect_triggers_stamina_thresholds import (
        Plan2EffectTriggerThresholdCatalog,
        simulate_direct_effect_sequence,
    )

    if not isinstance(catalog, Plan2EffectTriggerThresholdCatalog):
        raise TypeError("catalog must be Plan2EffectTriggerThresholdCatalog")
    return simulate_direct_effect_sequence(
        catalog,
        upgrade_count=upgrade_count,
        current_stamina=current_stamina,
        max_stamina=max_stamina,
        card_origin=card_origin,
    )


def evaluate_plan2_effect_trigger_aggressive_up3_exact(
    card: object,
    inputs: object,
    *,
    repetitions: int = 1,
) -> object:
    """Build the exact Aggressive>=3 slot after the timer enqueue handoff."""

    from .plan2_effect_trigger_aggressive_up3 import (
        EffectTriggerEvaluationInput,
        TARGET_CARD_LEVEL_REFS,
        TargetCardVersion,
        simulate_effect_trigger_build,
    )

    if not isinstance(card, TargetCardVersion):
        raise TypeError("card must be the exact AggressiveUp3 TargetCardVersion")
    if not isinstance(inputs, EffectTriggerEvaluationInput):
        raise TypeError("inputs must be EffectTriggerEvaluationInput")
    if card.ref not in TARGET_CARD_LEVEL_REFS:
        raise ValueError(f"unintegrated-aggressive-up3-effect-version:{card.ref}")
    return simulate_effect_trigger_build(card, inputs, repetitions=repetitions)


def execute_plan2_review_count_add_interval3_exact(
    state: object,
    event: object,
    program: object,
    **stages: object,
) -> object:
    """Execute the exact listener-local interval-3 ReviewUp6 transaction."""

    from .plan2_review_count_add_interval3 import (
        AcceptedPlay,
        Plan2ReviewCountAddInterval3Program,
        Plan2ReviewCountAddInterval3State,
        evaluate_plan2_review_count_add_interval3,
    )

    if not isinstance(state, Plan2ReviewCountAddInterval3State):
        raise TypeError("state must be Plan2ReviewCountAddInterval3State")
    if not isinstance(event, AcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    if not isinstance(program, Plan2ReviewCountAddInterval3Program):
        raise TypeError("program must be Plan2ReviewCountAddInterval3Program")
    return evaluate_plan2_review_count_add_interval3(
        state,
        event,
        program,
        **stages,
    )


def install_plan2_status_enchant_encore_exact(
    runtime: object,
    supplied: object,
) -> object:
    """Install one exact once-only permanent Encore listener."""

    from .plan2_status_enchant_encore import (
        Plan2EncoreInstallInput,
        Plan2EncoreRuntime,
        install_plan2_encore_listener,
    )

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise TypeError("runtime must be Plan2EncoreRuntime")
    if not isinstance(supplied, Plan2EncoreInstallInput):
        raise TypeError("supplied must be Plan2EncoreInstallInput")
    return install_plan2_encore_listener(runtime, supplied)


def plan_plan2_status_enchant_encore_exact(
    runtime: object,
    event: object,
    *,
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Spend exact Encore counts and queue self-target ForcePlay handoffs."""

    from .plan2_status_enchant_encore import (
        Plan2EncoreRuntime,
        Plan2EncoreTriggerEvent,
        plan_plan2_encore_trigger,
    )

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise TypeError("runtime must be Plan2EncoreRuntime")
    if not isinstance(event, Plan2EncoreTriggerEvent):
        raise TypeError("event must be Plan2EncoreTriggerEvent")
    return plan_plan2_encore_trigger(runtime, event, database=database)


def stage_plan2_status_enchant_encore_exact(
    runtime: object,
    handoff_id: str,
) -> object:
    """Rematch one exact Encore ForcePlay command by captured GUID."""

    from .plan2_status_enchant_encore import (
        Plan2EncoreRuntime,
        stage_plan2_encore_handoff,
    )

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise TypeError("runtime must be Plan2EncoreRuntime")
    return stage_plan2_encore_handoff(runtime, handoff_id)


def record_plan2_status_enchant_encore_exact(
    runtime: object,
    receipt: object,
) -> object:
    """Commit one exact Encore replay settlement and count/history update."""

    from .plan2_status_enchant_encore import (
        Plan2EncorePlayReceipt,
        Plan2EncoreRuntime,
        record_plan2_encore_play_completion,
    )

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise TypeError("runtime must be Plan2EncoreRuntime")
    if not isinstance(receipt, Plan2EncorePlayReceipt):
        raise TypeError("receipt must be Plan2EncorePlayReceipt")
    return record_plan2_encore_play_completion(runtime, receipt)


@dataclass(frozen=True, slots=True)
class Plan2Final26CoreBinding:
    """One exact card-version boundary in the final Plan2 integration batch."""

    card_id: str
    upgrade: int
    family: str
    standalone_modules: tuple[str, ...]
    removed_gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.card_id or type(self.upgrade) is not int or self.upgrade < 0:
            raise ValueError("invalid final26 card version")
        if not self.family or not self.standalone_modules or not self.removed_gaps:
            raise ValueError("incomplete final26 core binding")
        if len(set(self.removed_gaps)) != len(self.removed_gaps):
            raise ValueError("duplicate final26 removed gap")
        if any(not gap.startswith(("C:", "D:")) for gap in self.removed_gaps):
            raise ValueError("final26 bindings may remove only unresolved gaps")

    @property
    def ref(self) -> tuple[str, int]:
        return (self.card_id, self.upgrade)


def _final26_bindings() -> tuple[Plan2Final26CoreBinding, ...]:
    rows: list[Plan2Final26CoreBinding] = []

    def add_four(
        card_id: str,
        family: str,
        modules: tuple[str, ...],
        gaps_by_upgrade: tuple[tuple[str, ...], ...],
    ) -> None:
        if len(gaps_by_upgrade) != 4:
            raise ValueError("final26 four-version family must define four gap sets")
        rows.extend(
            Plan2Final26CoreBinding(card_id, upgrade, family, modules, gaps)
            for upgrade, gaps in enumerate(gaps_by_upgrade)
        )

    add_four(
        "p_card-02-act-3_186",
        "timer_block",
        ("gkms_tool.plan2_timer_block_chains",),
        tuple(
            (
                f"C:effect-chain:{child_id}",
                "C:effect:ProduceExamEffectType_ExamEffectTimer",
            )
            for child_id in (
                "e_effect-exam_lesson_depend_block-1800-01",
                "e_effect-exam_lesson_depend_block-2800-01",
                "e_effect-exam_lesson_depend_block-3200-01",
                "e_effect-exam_lesson_depend_block-3200-01",
            )
        ),
    )
    add_four(
        "p_card-02-ido-3_103",
        "timer_block",
        ("gkms_tool.plan2_timer_block_chains",),
        tuple(
            (
                f"C:effect-chain:{child_id}",
                "C:effect:ProduceExamEffectType_ExamEffectTimer",
            )
            for child_id in (
                "e_effect-exam_block-0005",
                "e_effect-exam_block-0008",
                "e_effect-exam_block-0008",
                "e_effect-exam_block-0009",
            )
        ),
    )
    add_four(
        "p_card-02-ido-3_127",
        "timer_block_aggressive_value_multiple_debuff_recover",
        (
            "gkms_tool.plan2_timer_block_chains",
            "gkms_tool.plan2_aggressive_value_multiple",
            "gkms_tool.plan2_debuff_recover",
        ),
        tuple(
            (
                f"C:effect-chain:{child_id}",
                "C:effect:ProduceExamEffectType_ExamAggressiveValueMultiple",
                "C:effect:ProduceExamEffectType_ExamDebuffRecover",
                "C:effect:ProduceExamEffectType_ExamEffectTimer",
            )
            for child_id in (
                "e_effect-exam_block-0004",
                "e_effect-exam_block-0008",
                "e_effect-exam_block-0010",
                "e_effect-exam_block-0010",
            )
        ),
    )
    add_four(
        "p_card-02-ido-3_130",
        "timer_block_status_change_block30",
        (
            "gkms_tool.plan2_timer_block_chains",
            "gkms_tool.plan2_status_change_block30",
        ),
        tuple(
            (
                f"C:effect-chain:{child_id}",
                "C:effect:ProduceExamEffectType_ExamEffectTimer",
                "C:status-trigger:e_trigger-exam_status_change-30-exam_block",
            )
            for child_id in (
                "e_effect-exam_block-0003",
                "e_effect-exam_block-0003",
                "e_effect-exam_block-0003",
                "e_effect-exam_block-0006",
            )
        ),
    )
    add_four(
        "p_card-02-ido-3_192",
        "card_search_play_count_buff",
        ("gkms_tool.plan2_card_search_play_count_buff",),
        (
            (
                "C:effect:ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff",
                "C:effect:ProduceExamEffectType_ExamForcePlayCardSearch",
            ),
        )
        * 4,
    )
    add_four(
        "p_card-02-ido-3_201",
        "aggressive_additive_interval5",
        ("gkms_tool.plan2_aggressive_additive_interval5",),
        (
            (
                "C:effect:ProduceExamEffectType_ExamAggressiveAdditiveFix",
                "C:status-trigger:e_trigger-exam_aggressive_up_interval-5-exam_card_play_aggressive",
            ),
        )
        * 4,
    )
    rows.append(
        Plan2Final26CoreBinding(
            "p_card-02-ido-100_041",
            0,
            "timer_card_move",
            ("gkms_tool.plan2_timer_card_move",),
            ("C:effect:ProduceExamEffectType_ExamEffectTimer",),
        )
    )
    rows.append(
        Plan2Final26CoreBinding(
            "p_card-02-act-100_010",
            0,
            "remaining_turn1_lesson_depend_block_consumption_final",
            (
                "gkms_tool.plan2_card_trigger_remaining_turn1",
                "gkms_tool.plan2_lesson_depend_block_consumption_final",
            ),
            (
                "C:card-trigger:e_trigger-none-remaining_turn-1",
                "C:effect:ProduceExamEffectType_ExamLessonDependBlockConsumptionSum",
                "C:move-effect-runtime:e_effect-exam_block-0005",
            ),
        )
    )
    result = tuple(sorted(rows, key=lambda row: row.ref))
    if len(result) != 26 or len({row.ref for row in result}) != 26:
        raise AssertionError("final26 central bindings must contain 26 unique versions")
    return result


PLAN2_FINAL26_CORE_BINDINGS = _final26_bindings()
_PLAN2_FINAL26_CORE_BINDING_BY_REF = {
    binding.ref: binding for binding in PLAN2_FINAL26_CORE_BINDINGS
}


def load_plan2_final26_core_bindings(
    *, database: Path = DEFAULT_DATABASE
) -> tuple[Plan2Final26CoreBinding, ...]:
    """Revalidate every standalone owner before exposing the all-or-none batch."""

    from .plan2_aggressive_additive_interval5 import (
        load_plan2_aggressive_additive_interval5_catalog,
    )
    from .plan2_aggressive_value_multiple import (
        load_plan2_aggressive_value_multiple_catalog,
    )
    from .plan2_card_search_play_count_buff import (
        load_affected_card_versions as load_play_count_buff_card_versions,
        load_card_search_effect_play_count_buff_contract,
    )
    from .plan2_debuff_recover import load_plan2_debuff_recover_catalog
    from .plan2_lesson_depend_block_consumption_final import (
        load_plan2_final_card_contract,
    )
    from .plan2_status_change_block30 import (
        load_plan2_status_change_block30_program,
    )
    from .plan2_timer_block_chains import load_plan2_timer_block_catalog
    from .plan2_timer_card_move import load_plan2_timer_card_move_contract

    database = Path(database)
    # Each loader is strict against its exact Master slice.  Do not return a
    # partial registry when any owner is missing, stale, or structurally altered.
    load_play_count_buff_card_versions(database)
    load_card_search_effect_play_count_buff_contract(database)
    load_plan2_aggressive_additive_interval5_catalog(database)
    load_plan2_timer_block_catalog(database=database)
    load_plan2_status_change_block30_program(database)
    load_plan2_timer_card_move_contract(database)
    load_plan2_aggressive_value_multiple_catalog(database=database)
    load_plan2_debuff_recover_catalog(database=database)
    # The remaining-turn standalone's catalog intentionally reads the prior
    # formal coverage artifact (where this version is still C).  The central
    # batch must remain rebuildable after that artifact advances to B, so its
    # Master card/effect half is revalidated here and the trigger gap itself is
    # matched exactly by the coverage builder before promotion.
    load_plan2_final_card_contract(database)
    return PLAN2_FINAL26_CORE_BINDINGS


def resolve_plan2_final26_core_binding(
    card_id: str,
    upgrade: int,
) -> Plan2Final26CoreBinding:
    """Resolve only a card version owned by the exact final-26 batch."""

    try:
        return _PLAN2_FINAL26_CORE_BINDING_BY_REF[(card_id, upgrade)]
    except KeyError as error:
        raise ValueError(f"unintegrated-final26-card-version:{card_id}#{upgrade}") from error


def execute_plan2_card_search_play_count_buff_exact(
    hook: object,
    *,
    contract: object | None = None,
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Delegate p192's pre-direct hook to its strict standalone owner."""

    from .plan2_card_search_play_count_buff import (
        execute_card_search_effect_play_count_buff,
    )

    return execute_card_search_effect_play_count_buff(
        hook, contract=contract, database=database
    )


def execute_plan2_aggressive_additive_interval5_exact(
    runtime: object,
    request: object,
) -> object:
    """Delegate the complete p201 card transaction to its exact owner."""

    from .plan2_aggressive_additive_interval5 import play_p201_card

    return play_p201_card(runtime, request)


def enqueue_plan2_timer_block_card_exact(
    runtime: object,
    *,
    card_id: str,
    upgrade: int,
    source_guid: str,
    play_origin: str = "ordinary",
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Resolve and enqueue one exact p186/p103/p127/p130 TimerBlock card."""

    from .plan2_timer_block_chains import (
        enqueue_plan2_timer_block_card,
        load_plan2_timer_block_catalog,
    )

    version = load_plan2_timer_block_catalog(database=database).version(
        card_id, upgrade
    )
    return enqueue_plan2_timer_block_card(
        runtime,
        version,
        source_guid=source_guid,
        play_origin=play_origin,
    )


def advance_plan2_timer_block_exact(runtime: object, turn_input: object) -> object:
    """Advance an exact TimerBlock runtime at its proved turn boundary."""

    from .plan2_timer_block_chains import advance_plan2_timer_block_turn

    return advance_plan2_timer_block_turn(runtime, turn_input)


def install_plan2_status_change_block30_exact(
    scheduler: object,
    program: object,
    *,
    upgrade: int,
    play_origin: str = "ordinary",
) -> object:
    """Install p130's exact post-direct permanent Block30 listener."""

    from .plan2_status_change_block30 import (
        install_plan2_status_change_block30_listener,
    )

    return install_plan2_status_change_block30_listener(
        scheduler,
        program,
        upgrade=upgrade,
        play_origin=play_origin,
    )


def evaluate_plan2_status_change_block30_exact(
    scheduler: object,
    event: object,
    program: object,
) -> object:
    """Evaluate one p130 Block status-difference event."""

    from .plan2_status_change_block30 import evaluate_plan2_status_change_block30

    return evaluate_plan2_status_change_block30(scheduler, event, program)


def install_plan2_timer_card_move_exact(
    runtime: object,
    request: object,
    *,
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Install p041's exact TimerCardMove transaction."""

    from .plan2_timer_card_move import install_plan2_timer_card_move

    return install_plan2_timer_card_move(runtime, request, database=database)


def advance_plan2_timer_card_move_exact(
    runtime: object,
    request: object | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Advance p041's exact timer/child-move runtime."""

    from .plan2_timer_card_move import advance_plan2_timer_card_move

    return advance_plan2_timer_card_move(runtime, request, database=database)


def execute_plan2_p127_aggressive_value_multiple_exact(
    *,
    upgrade: int,
    current_aggressive: int,
    source_guid: str,
    play_origin: str = "normal",
    status_add_blocked: bool = False,
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Execute p127 slot zero and return its ordered standalone handoff."""

    from .plan2_aggressive_value_multiple import (
        execute_p127_aggressive_value_multiple_handoff,
        load_plan2_aggressive_value_multiple_catalog,
    )

    version = load_plan2_aggressive_value_multiple_catalog(
        database=database
    ).version(upgrade)
    return execute_p127_aggressive_value_multiple_handoff(
        version,
        current_aggressive=current_aggressive,
        source_guid=source_guid,
        play_origin=play_origin,
        status_add_blocked=status_add_blocked,
    )


def execute_plan2_debuff_recover_exact(
    effect: object,
    state: object,
    *,
    statuses_at_remove: object | None = None,
    play_origin: str = "ordinary",
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Execute p127's exact DebuffRecover snapshot/commit boundary."""

    from .plan2_debuff_recover import execute_debuff_recover

    return execute_debuff_recover(
        effect,
        state,
        statuses_at_remove=statuses_at_remove,
        play_origin=play_origin,
        database=database,
    )


def admit_plan2_card_remaining_turn1_exact(*args: object, **kwargs: object) -> object:
    """Delegate the final card's exact remaining-turn-one admission gate."""

    from .plan2_card_trigger_remaining_turn1 import (
        admit_plan2_card_remaining_turn1,
    )

    return admit_plan2_card_remaining_turn1(*args, **kwargs)


def play_plan2_lesson_depend_block_consumption_final_exact(
    runtime: object,
    request: object,
    *,
    database: Path = DEFAULT_DATABASE,
) -> object:
    """Delegate the final card's complete admitted card transaction."""

    from .plan2_lesson_depend_block_consumption_final import play_plan2_final_card

    return play_plan2_final_card(runtime, request, database=database)


def complete_plan2_card_play_after_move(
    state: Plan2State,
    *,
    card_move_completed: bool,
) -> Plan2CardPlayCompletion:
    """Increment play counts only after external plan-neutral card settlement."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if type(card_move_completed) is not bool:
        raise TypeError("card_move_completed must be bool")
    if not card_move_completed:
        raise ValueError("card move must complete before play-count increment")
    if (
        state.exam_card_play_count == INT32_MAX
        or state.turn_card_play_count == INT32_MAX
    ):
        raise OverflowError("card-play count cannot increment past Int32")
    after = replace(
        state,
        exam_card_play_count=state.exam_card_play_count + 1,
        turn_card_play_count=state.turn_card_play_count + 1,
    )
    return Plan2CardPlayCompletion(
        state,
        after,
        (
            "card-move:external-plan-neutral-settlement:completed",
            "card-play-count:increment:global",
            "card-play-count:increment:turn",
        ),
    )


__all__ = [
    "PLAN2_AGGRESSIVE_CARD_TRIGGER_ID",
    "PLAN2_CARD_MOVE_LOST_RANDOM_EFFECT_ID",
    "PLAN2_CARD_MOVE_LOST_RANDOM_SEARCH_ID",
    "PLAN2_CARD_PLAY_STAMINA_EFFECT_ID",
    "PLAN2_CARD_UPGRADE_HAND_ALL_EFFECT_ID",
    "PLAN2_END_TURN_REMAINING_ONE_TRIGGER_ID",
    "PLAN2_LESSON_VALUE_MULTIPLE_EFFECT_IDS",
    "PLAN2_START_PLAY_TRIGGER_ID",
    "PLAN2_TURN_PROGRESS_TRIGGER_ID",
    "PLAN2_FINAL26_CORE_BINDINGS",
    "Plan2AggressiveCardTriggerEvaluation",
    "Plan2AntiDebuffGateTransition",
    "Plan2AntiDebuffInstallTransition",
    "Plan2BlockDependStartTurnTransition",
    "Plan2BlockPerUseCardCountDirectTransition",
    "Plan2BlockRestrictionDirectTransition",
    "Plan2CardMoveDirectTransition",
    "Plan2CardPlayAggressiveUp6Evaluation",
    "Plan2CardPlayAggressiveUp9Evaluation",
    "Plan2AggressiveUp9StaminaReduceCardTransition",
    "Plan2CardPlayCompletion",
    "Plan2CardPlayStaminaTriggerEvaluation",
    "Plan2CardUpgradeDirectTransition",
    "Plan2EndTurnRemainingOneEvaluationTransition",
    "Plan2DirectEffectAggressiveUp6Evaluation",
    "Plan2ExamEffectTimerChildExecution",
    "Plan2ExamEffectTimerEndTurnTransition",
    "Plan2ExamEffectTimerInstallDifference",
    "Plan2ExamEffectTimerInstallTransition",
    "Plan2ExamEffectTimerRuntime",
    "Plan2ExamEffectTimerStartTurnTransition",
    "Plan2HandGraveDrawDirectTransition",
    "Plan2Final26CoreBinding",
    "Plan2LessonValueMultipleDirectTransition",
    "Plan2LessonDependReviewAggressiveTransition",
    "Plan2ReviewPerSearchCountDirectTransition",
    "Plan2SearchPlayCardStaminaChangeDirectTransition",
    "Plan2StaminaConsumptionAddInstall",
    "Plan2StaminaPaymentTransition",
    "Plan2StaminaRecoverFixTransition",
    "Plan2StartPlayCaptureTransition",
    "Plan2StartTurnConditionThresholdDownEvaluation",
    "Plan2StartTurnProgressUp2Evaluation",
    "Plan2StartTurnReviewStaminaRecoverTransition",
    "Plan2StartTurnStaminaLessRecoverTransition",
    "Plan2StartTurnStaminaUpMultipleEvaluation",
    "Plan2StartTurnNoBlockCaptureTransition",
    "Plan2StartTurnNoBlockCardGateEvaluation",
    "Plan2TurnProgressCardTriggerEvaluation",
    "apply_plan2_block_per_use_card_count_direct",
    "apply_plan2_card_move_lost_random_direct",
    "apply_plan2_card_upgrade_hand_all_direct",
    "apply_plan2_hand_grave_draw_direct",
    "apply_plan2_lesson_value_multiple_exact_direct",
    "apply_plan2_review_per_search_count_exact_direct",
    "apply_plan2_stamina_recover_fix",
    "capture_plan2_start_play_exact",
    "capture_plan2_start_turn_no_block_status_exact",
    "complete_plan2_card_play_after_move",
    "create_plan2_exam_effect_timer_runtime",
    "advance_plan2_timer_lesson_depend_review_exact",
    "advance_plan2_timer_block_exact",
    "advance_plan2_timer_card_move_exact",
    "end_plan2_exam_effect_timer_turn",
    "execute_plan2_card_play_playing_search_exact",
    "execute_plan2_card_create_search_exact",
    "execute_plan2_force_play_queue_exact",
    "execute_plan2_add_grow_effect_exact",
    "execute_plan2_aggressive_additive_interval5_exact",
    "execute_plan2_aggressive_up9_stamina_reduce_card_exact",
    "execute_plan2_block_depend_consumption_sum_start_turn_exact",
    "execute_plan2_interval2_aggressive_active_exact",
    "execute_plan2_interval2_target_block_group_exact",
    "execute_plan2_play_count_interval5_exact",
    "execute_plan2_move_card_play_aggressive_exact",
    "execute_plan2_lesson_depend_review_aggressive_exact",
    "execute_plan2_remaining_card_move_exact",
    "evaluate_plan2_effect_trigger_aggressive_up3_exact",
    "evaluate_plan2_effect_triggers_aggressive_up9_exact",
    "evaluate_plan2_effect_triggers_stamina_thresholds_exact",
    "execute_plan2_review_count_add_interval3_exact",
    "execute_plan2_card_search_play_count_buff_exact",
    "execute_plan2_debuff_recover_exact",
    "execute_plan2_p127_aggressive_value_multiple_exact",
    "execute_plan2_start_turn_review_stamina_recover_exact",
    "execute_plan2_start_turn_stamina_less_recover_exact",
    "execute_plan2_stamina_up500_block_fix_exact",
    "enqueue_plan2_timer_lesson_depend_review_exact",
    "enqueue_plan2_timer_block_card_exact",
    "evaluate_plan2_aggressive_card_trigger_pre_payment",
    "evaluate_plan2_card_play_aggressive_up6_pre_payment",
    "evaluate_plan2_card_play_aggressive_up9_pre_payment",
    "evaluate_plan2_card_play_stamina_effect_trigger_pre_payment",
    "evaluate_plan2_end_turn_remaining_one_before_decrement",
    "evaluate_plan2_direct_effect_aggressive_up6_sequence_build",
    "evaluate_plan2_start_turn_condition_threshold_down_exact",
    "evaluate_plan2_start_turn_no_block_card_gate_exact",
    "evaluate_plan2_start_turn_stamina_up_multiple_exact",
    "evaluate_plan2_start_turn_turn_progress_up2_exact",
    "evaluate_plan2_turn_progress_card_trigger_pre_payment",
    "gate_plan2_status_addition_native",
    "install_plan2_anti_debuff_native",
    "install_plan2_card_play_playing_search_exact_listener",
    "install_plan2_end_turn_card_play_aggressive_exact_listener",
    "install_plan2_end_turn_remaining_three_exact_listener",
    "install_plan2_end_turn_review_up3_exact_listener",
    "install_plan2_exam_effect_timer_exact",
    "install_plan2_interval2_aggressive_active_exact_listener",
    "install_plan2_interval2_target_block_group_exact_listener",
    "install_plan2_block_restriction_exact_direct",
    "install_plan2_block_depend_consumption_sum_exact_listener",
    "install_plan2_search_play_card_stamina_change_exact_direct",
    "install_plan2_play_count_interval5_exact_listener",
    "install_plan2_stamina_consumption_add",
    "install_plan2_status_enchant_encore_exact",
    "install_plan2_status_change_block30_exact",
    "install_plan2_timer_card_move_exact",
    "evaluate_plan2_status_change_block30_exact",
    "admit_plan2_card_remaining_turn1_exact",
    "play_plan2_lesson_depend_block_consumption_final_exact",
    "load_plan2_final26_core_bindings",
    "resolve_plan2_final26_core_binding",
    "pay_plan2_stamina_cost",
    "plan_plan2_force_play_search_exact",
    "plan_plan2_status_enchant_encore_exact",
    "record_plan2_status_enchant_encore_exact",
    "search_plan2_timer_lesson_depend_review_exact",
    "start_plan2_exam_effect_timer_turn",
    "stage_plan2_status_enchant_encore_exact",
]
