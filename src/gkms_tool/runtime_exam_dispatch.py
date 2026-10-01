"""Native Exam dispatch; mode and Plan never select a visual controller."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace


def _plan2_lesson_provider(context):
    from .plan2_native_expectimax import Plan2NativeExpectimaxLimits
    from .plan2_native_unattended_loop import bind_plan2_native_exam_save_orchestrator

    planner = bind_plan2_native_exam_save_orchestrator(draw_count=context.draw_count,
        hand_limit=context.hand_limit, limits=Plan2NativeExpectimaxLimits(max_depth=2, beam_width=8, max_nodes=128))

    def choose(observation, lesson):
        if lesson.plan_type != "ProducePlanType_Plan2":
            raise ValueError("Plan2 lesson policy received another Plan")
        result = planner(observation.evidence)
        action = result.best_action
        if action is None:
            raise ValueError("native Plan2 lesson has no rule decision: " + ";".join(
                issue.code + ":" + issue.detail for issue in result.blockers))
        return {"action": {"kind": action.kind, "card_guid": getattr(action, "card_guid", None),
                "slot_index": getattr(action, "slot_index", None), "drink_id": getattr(action, "drink_id", None),
                "selected_card_guid": getattr(action, "selected_card_guid", None)},
            "decision_owner": "rule-planner", "learned_policy_used": False,
            "policy": "native-plan2-lesson-bounded-rules", "full_lesson_score_prediction_available": False}

    return choose


def dispatch_runtime_exam(plan_type, path, *, exam_type, produce_id, idol_card_id, run_id=None,
                          plan2_context=None, policy_bundle_path=None, max_actions=100,
                          stop_requested=lambda: False, progress_callback=None, exam_policy_variant=None):
    """Choose a pure policy; all actions use the shared native gateway."""
    plans = {"ProducePlanType_Plan1", "ProducePlanType_Plan2", "ProducePlanType_Plan3"}
    if plan_type not in plans or type(exam_type) is not int or exam_type not in (0, 1):
        raise ValueError("native mode/Plan requires a known lesson or audition")
    if exam_type == 0:
        from .runtime_lesson_executor import run_runtime_lesson

        if plan_type == "ProducePlanType_Plan2" and plan2_context is None:
            raise ValueError("Plan2 native lesson requires a rule context")
        return run_runtime_lesson(expected_produce_id=produce_id, expected_idol_card_id=idol_card_id,
            run_id=run_id, max_actions=max_actions, stop_requested=stop_requested, progress_callback=progress_callback,
            decision_provider=_plan2_lesson_provider(plan2_context) if plan_type == "ProducePlanType_Plan2" else None)
    if exam_policy_variant is not None:
        from .runtime_gui_exam_policy import build_live_exam_policy
        from .runtime_plan2_executor import run_runtime_exam_policy
        return run_runtime_exam_policy(path, max_actions=max_actions,
            policy=build_live_exam_policy(exam_policy_variant,produce_id=produce_id,
                idol_card_id=idol_card_id,run_id=run_id),run_id=run_id,
            stop_requested=stop_requested,progress_callback=progress_callback,
            report_schema='gkms.gui-model-native-exam.v1')
    if plan_type == "ProducePlanType_Plan1":
        from .plan1_exact_learned_exam import run_plan1_exact_learned_exam

        return run_plan1_exact_learned_exam(path, expected_produce_id=produce_id,
            expected_idol_card_id=idol_card_id, run_id=run_id, max_actions=max_actions,
            learned_policy_bundle_path=policy_bundle_path,
            stop_requested=stop_requested, progress_callback=progress_callback)
    if plan_type == "ProducePlanType_Plan2":
        from .runtime_plan2_executor import run_runtime_plan2_exam

        if plan2_context is None:
            raise ValueError("Plan2 native audition requires a policy context")
        if policy_bundle_path is not None:
            plan2_context = replace(plan2_context, learned_policy_bundle_path=Path(policy_bundle_path))
        return run_runtime_plan2_exam(path, context=plan2_context, run_id=run_id,
            stop_requested=stop_requested, progress_callback=progress_callback)
    from .offline_rl_runtime_adapter import build_offline_rl_artifact_selector
    from .policy_bundle import PolicyBundle
    from .runtime_nia_plan3 import RuntimeAuditionPlan3CandidateProvider, build_runtime_audition_learned_advisor
    from .runtime_plan3_executor import run_runtime_plan3_orchestrator

    from .master_db import get_idol_profile
    idol = get_idol_profile(idol_card_id)
    if (produce_id == "produce-004" and idol is not None
            and idol.plan_type == "ProducePlanType_Plan3"
            and idol.exam_effect_type == "ProduceExamEffectType_ExamFullPower"):
        from .runtime_fullpower_policy import build_runtime_fullpower_policy
        from .runtime_plan2_executor import run_runtime_exam_policy

        return run_runtime_exam_policy(path, max_actions=max_actions,
            policy=build_runtime_fullpower_policy(produce_id=produce_id, idol_card_id=idol_card_id,
                policy_bundle_path=policy_bundle_path), run_id=run_id,
            stop_requested=stop_requested, progress_callback=progress_callback)

    bundle = PolicyBundle.load(policy_bundle_path, component_roles=("exact_exam_policy",),
                               optional_component_roles=("offline_rl_policy",))
    try:
        component = bundle.component("offline_rl_policy")
    except KeyError:
        offline_rl = None
    else:
        offline_rl = build_offline_rl_artifact_selector(component, project_root=bundle.project_root)
    if (produce_id == "produce-004" and idol is not None
            and idol.plan_type == "ProducePlanType_Plan3"
            and idol.exam_effect_type == "ProduceExamEffectType_ExamConcentration"):
        from .runtime_plan3_native_primary_policy import RuntimePlan3ConcentrationNativeObservationPolicy
        from .runtime_plan2_executor import run_runtime_exam_policy

        return run_runtime_exam_policy(path, max_actions=max_actions,
            policy=RuntimePlan3ConcentrationNativeObservationPolicy(bundle, offline_rl=offline_rl), run_id=run_id,
            stop_requested=stop_requested, progress_callback=progress_callback)
    provider = RuntimeAuditionPlan3CandidateProvider(expected_produce_id=produce_id, expected_idol_card_id=idol_card_id)
    advisor = build_runtime_audition_learned_advisor(provider, bundle=bundle, offline_rl=offline_rl, source="dll-native-audition")
    result = run_runtime_plan3_orchestrator(Path(path), advisor_mode="audition", dry_run=False, max_actions=max_actions,
        executor_options={"advisor_factory": advisor}, expected_run_id=run_id,
        stop_requested=stop_requested, progress_callback=progress_callback)
    return {"accepted": result.terminal_reached, "terminal": result.terminal_reached,
            "reason": result.stop_reason, "orchestration": result.to_dict()}
