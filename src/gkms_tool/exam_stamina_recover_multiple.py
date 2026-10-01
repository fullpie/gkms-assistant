"""Plan-neutral entry point for native ``ExamStaminaRecoverMultiple``.

The proven float32/ceil/Fix-cap implementation predates this shared surface
and remains API-compatible in its original module.  Both plan runtimes use the
same objects and evaluator through aliases; no arithmetic is duplicated here.
"""

from __future__ import annotations

from .plan2_start_turn_review_stamina_recover import (
    MultipleEffectContract,
    ReviewStaminaContractError,
    StaminaRecoverMultipleEvaluation,
    StaminaRecoverMultipleRuntime,
    evaluate_stamina_recover_multiple,
)


ExamStaminaRecoverMultipleContract = MultipleEffectContract
ExamStaminaRecoverMultipleEvaluation = StaminaRecoverMultipleEvaluation
ExamStaminaRecoverMultipleRuntime = StaminaRecoverMultipleRuntime
evaluate_exam_stamina_recover_multiple = evaluate_stamina_recover_multiple


__all__ = [
    "ExamStaminaRecoverMultipleContract",
    "ExamStaminaRecoverMultipleEvaluation",
    "ExamStaminaRecoverMultipleRuntime",
    "MultipleEffectContract",
    "ReviewStaminaContractError",
    "StaminaRecoverMultipleEvaluation",
    "StaminaRecoverMultipleRuntime",
    "evaluate_exam_stamina_recover_multiple",
    "evaluate_stamina_recover_multiple",
]

