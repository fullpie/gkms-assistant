"""Shared typed scalar item owners for both production event receivers.

No horizon import, caller discovery, guessed defaults, or phase inference.
Callers bind current values and the shared UID floor before executing a child.
An absent runtime dependency is an explicit failure, not a supported no-op.
"""
from dataclasses import dataclass, replace

from .native_exam_formula import AddingParameterSettings, AddingParameterStatus, get_ratio_effect_int_value
from .plan2_core_runtime import apply_plan2_stamina_recover_fix
from .plan2_native_catalog_aggressive_additive import (
    Plan2AggressiveAdditiveRuntime, Plan2NativeAggressiveAdditiveCatalog,
    Plan2NativeAggressiveAdditiveEffect, Plan2NativeAggressiveAdditiveProgram,
    Plan2NativeAggressiveAdditiveSlot, install_plan2_native_aggressive_additive,
)
from .plan2_native_catalog_debuff import Plan2NativeDebuffRegistry, gate_plan2_native_status_addition
from .plan2_native_catalog_review_dynamic import (
    OP_INSTALL_REVIEW_ADDITIVE, Plan2NativeReviewDynamicInstallInput,
    Plan2NativeReviewDynamicLessonRequest, Plan2NativeReviewDynamicProgram,
    Plan2NativeReviewDynamicRuntime, handoff_plan2_native_review_dynamic_lesson,
    install_plan2_native_review_dynamic,
)
from .plan2_native_item_runtime import Plan2NativeItemEffect
from .plan2_stamina_recover_fix import StaminaRecoverFixContract
from .plan2_state import Plan2State, carry_plan2_state_application_status

REVIEW_ADDITIVE = "ProduceExamEffectType_ExamReviewAdditive"
AGGRESSIVE_ADDITIVE = "ProduceExamEffectType_ExamAggressiveAdditive"
LESSON_REVIEW = "ProduceExamEffectType_ExamLessonDependExamReview"
STAMINA_RECOVER = "ProduceExamEffectType_ExamStaminaRecoverFix"
SHARED_ITEM_SCALAR_EFFECT_TYPES = frozenset({REVIEW_ADDITIVE, AGGRESSIVE_ADDITIVE, LESSON_REVIEW, STAMINA_RECOVER})


class ItemScalarExecutionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ItemScalarContext:
    scalar: Plan2State
    review_dynamic: Plan2NativeReviewDynamicRuntime
    aggressive_runtime: Plan2AggressiveAdditiveRuntime | None
    aggressive_catalog: Plan2NativeAggressiveAdditiveCatalog | None
    debuff_registry: Plan2NativeDebuffRegistry
    next_status_uid: int
    source_item_id: str
    play_origin: str
    stamina_recover_restricted: bool
    stamina_recover_add_permil: int | None

    def __post_init__(self):
        if not isinstance(self.scalar, Plan2State) or not isinstance(self.review_dynamic, Plan2NativeReviewDynamicRuntime):
            raise ItemScalarExecutionError("item-scalar-context-unbound")
        if self.review_dynamic.review != self.scalar.review:
            raise ItemScalarExecutionError("item-review-runtime-scalar-mismatch")
        if not isinstance(self.debuff_registry, Plan2NativeDebuffRegistry):
            raise ItemScalarExecutionError("item-debuff-context-unbound")
        if type(self.next_status_uid) is not int or self.next_status_uid < 1:
            raise ItemScalarExecutionError("item-status-uid-floor-unbound")
        floor = max(self.scalar.next_status_uid, self.review_dynamic.next_status_uid,
            self.debuff_registry.next_uid, self.aggressive_runtime.next_status_uid if self.aggressive_runtime is not None else 1)
        if self.next_status_uid < floor:
            raise ItemScalarExecutionError("item-status-uid-floor-stale")
        if not isinstance(self.source_item_id, str) or not self.source_item_id or self.play_origin not in ("normal", "forced", "extra"):
            raise ItemScalarExecutionError("item-source-context-unbound")
        # None is the existing core's explicit absent-recovery-multiplier
        # representation. It is passed by the caller, never filled in here.
        if type(self.stamina_recover_restricted) is not bool or (self.stamina_recover_add_permil is not None and type(self.stamina_recover_add_permil) is not int):
            raise ItemScalarExecutionError("item-recovery-context-unbound")


def execute_item_scalar_effect(context: ItemScalarContext, effect: Plan2NativeItemEffect):
    if not isinstance(context, ItemScalarContext):
        raise ItemScalarExecutionError("item-scalar-context-unbound")
    if not isinstance(effect, Plan2NativeItemEffect) or effect.effect_type not in SHARED_ITEM_SCALAR_EFFECT_TYPES:
        raise ItemScalarExecutionError("item-shared-scalar-owner-unavailable")
    scalar, review, aggressive, registry = context.scalar, context.review_dynamic, context.aggressive_runtime, context.debuff_registry
    trace = [f"shared-item-scalar:{effect.effect_type}"]
    blocked = False
    if registry.anti_debuff_count:
        gate = gate_plan2_native_status_addition(registry, effect.effect_type)
        if not gate.resolved:
            raise ItemScalarExecutionError(f"item-status-addition-unresolved:{effect.effect_type}:{gate.reason}")
        registry, blocked = gate.registry_after, gate.blocked
        trace.extend(gate.queue_handoff.trace)
        trace.extend(f"debuff-difference:{v!r}" for v in gate.queue_handoff.differences)
        trace.extend(f"debuff-callback:{v}" for v in gate.queue_handoff.callback_events)
    if effect.effect_type == REVIEW_ADDITIVE:
        program = Plan2NativeReviewDynamicProgram(context.source_item_id, 0, 0,
            (effect.effect_id,), effect.effect_id, effect.effect_type, OP_INSTALL_REVIEW_ADDITIVE,
            effect.value1, effect.value2, effect.effect_count, effect.effect_turn)
        result = install_plan2_native_review_dynamic(replace(review, next_status_uid=context.next_status_uid), program,
            Plan2NativeReviewDynamicInstallInput(source_guid=f"item:{context.source_item_id}",
                play_origin=context.play_origin, completed_effect_ids=(), status_addition_blocked=blocked))
        if not result.executable:
            raise ItemScalarExecutionError(f"item-review-additive-install-failed-closed:{effect.effect_id}:{result.unresolved}")
        review = result.after
        trace.extend(result.trace)
        trace.append(f"item-review-additive:{effect.effect_id}:" + ("blocked" if blocked else "installed"))
    elif effect.effect_type == AGGRESSIVE_ADDITIVE:
        if context.aggressive_catalog is None:
            raise ItemScalarExecutionError("item-aggressive-additive-catalog-unbound")
        if aggressive is None:
            aggressive = Plan2AggressiveAdditiveRuntime.empty(context.aggressive_catalog, aggressive=scalar.card_play_aggressive)
        elif aggressive.catalog is not context.aggressive_catalog:
            raise ItemScalarExecutionError("item-aggressive-additive-catalog-mismatch")
        runtime = replace(aggressive, aggressive=scalar.card_play_aggressive, next_status_uid=context.next_status_uid)
        child = Plan2NativeAggressiveAdditiveEffect(effect.effect_id, effect.effect_type, effect.value1,
            effect.value2, effect.effect_count, effect.effect_turn, ("effect_group-visible-exam_card_play_aggressive-000",))
        program = Plan2NativeAggressiveAdditiveProgram(card_id=context.source_item_id, upgrade=0,
            plan_type="ProducePlanType_Plan2", category="ProduceCardCategory_MentalSkill", stamina=0,
            cost_type="ExamCostType_Unknown", cost_value=0, play_trigger_id="", move_position="ProduceCardMovePositionType_Unknown",
            ordered_slots=(Plan2NativeAggressiveAdditiveSlot(0, child),), target_slot_index=0,
            target_family="additive", companion_blockers=())
        result = install_plan2_native_aggressive_additive(runtime, program,
            play_origin="ordinary" if context.play_origin == "normal" else context.play_origin,
            completed_effect_ids=(), uid=context.next_status_uid, blocked=blocked)
        if not blocked and not result.executable:
            raise ItemScalarExecutionError(f"item-aggressive-additive-install-failed-closed:{effect.effect_id}:{result.reason}")
        aggressive = result.after
        trace.extend(result.operations)
        trace.append(f"item-aggressive-additive:{effect.effect_id}:" + ("blocked" if blocked else "installed"))
    elif effect.effect_type == LESSON_REVIEW:
        requested = get_ratio_effect_int_value(scalar.review, effect.value1, is_ceil=True)
        result = handoff_plan2_native_review_dynamic_lesson(review, Plan2NativeReviewDynamicLessonRequest(
            value=requested, count=effect.effect_count, aggressive_at_execution=scalar.card_play_aggressive,
            adding_status=AddingParameterStatus(aggressive=scalar.card_play_aggressive), settings=AddingParameterSettings(),
            application_status=scalar.parameter_application_status(slump=False), play_origin=context.play_origin))
        if not result.executable:
            raise ItemScalarExecutionError(f"item-lesson-depend-review-failed-closed:{effect.effect_id}:{result.unresolved}")
        scalar = carry_plan2_state_application_status(scalar, result.final_application_status)
        trace.extend(result.trace)
        trace.append(f"item-lesson-depend-review:{effect.effect_id}:requested={requested}:count={effect.effect_count}")
    else:
        result = apply_plan2_stamina_recover_fix(scalar, StaminaRecoverFixContract(effect.effect_id,
            effect.value1, effect.value2, effect.effect_count, effect.effect_turn),
            stamina_recover_restricted=context.stamina_recover_restricted,
            stamina_recover_add_permil=context.stamina_recover_add_permil)
        if not result.standalone.executable:
            raise ItemScalarExecutionError(f"item-stamina-recovery-failed-closed:{effect.effect_id}:{result.standalone.reason}")
        scalar = result.after
        trace.extend(result.event_trace)
        trace.append(f"item-stamina-recover-fix:{effect.effect_id}")
    next_uid = max(context.next_status_uid, scalar.next_status_uid, review.next_status_uid,
        registry.next_uid, aggressive.next_status_uid if aggressive is not None else 1)
    return replace(context, scalar=scalar, review_dynamic=review, aggressive_runtime=aggressive,
        debuff_registry=registry, next_status_uid=next_uid), tuple(trace)
