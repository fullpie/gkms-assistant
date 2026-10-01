"""One native lesson loop; Plan-specific pure rules share the DLL gateway."""
from __future__ import annotations

from dataclasses import asdict
from typing import Callable, Mapping

from .runtime_lesson_context import evaluate_native_lesson, parse_native_lesson_context


def run_runtime_lesson(*, expected_produce_id: str, expected_idol_card_id: str,
                       run_id: str | None = None, gateway=None, max_actions: int = 100,
                       stop_requested: Callable[[], bool] = lambda: False,
                       progress_callback=None, decision_provider=None, plan3_beam_width: int = 16):
    """Observe -> rule decision -> one DLL input -> native settlement.

    Plan1 and Plan3 have default rule planners. Another Plan can provide a
    pure ``decision_provider(observation, context)`` returning an action and
    explicit policy metadata; it cannot acquire a second input owner here.
    """
    from .runtime_exam_executor import RuntimeExamGateway
    from .runtime_lesson_policy import (RuntimeLessonPlan3CandidateProvider,
        build_runtime_plan3_lesson_advisor, choose_runtime_plan1_lesson, prepare_runtime_plan1_lesson)
    from .runtime_plan3_executor import build_native_plan3_execution_plan, decoded_runtime_plan3
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    if decision_provider is not None and not callable(decision_provider):
        raise TypeError("decision_provider must be callable")
    gateway = gateway or RuntimeExamGateway(cancelled=stop_requested)
    if hasattr(gateway, "cancelled"):
        prior_cancelled = gateway.cancelled
        gateway.cancelled = lambda: stop_requested() or bool(prior_cancelled and prior_cancelled())
    steps = []
    base, identity, last_context = None, None, None
    plan3_provider = RuntimeLessonPlan3CandidateProvider(expected_produce_id=expected_produce_id,
                                                       expected_idol_card_id=expected_idol_card_id)
    plan3_advisor = build_runtime_plan3_lesson_advisor(plan3_provider, beam_width=plan3_beam_width)

    def finish(accepted, reason, *, detail=None, evaluation=None):
        return {"schema": "gkms.native-lesson-execution.v1", "accepted": accepted, "terminal": accepted,
                "reason": reason, "detail": detail, "actions_executed": len(steps), "steps": steps,
                "produce_id": expected_produce_id, "idol_card_id": expected_idol_card_id,
                "stage": "Lesson", "decision_owner": "rule-planner", "learned_policy_used": False,
                "lesson_context": None if last_context is None else last_context.to_dict(),
                "lesson_result": None if evaluation is None else asdict(evaluation)}

    def progress(observation, context, *, decision=None):
        if progress_callback is not None:
            try:
                progress_callback({"source_run_id": getattr(observation.evidence, "run_id", run_id), "current_page": "exam",
                    "outer_action_count": len(steps), "monitor_snapshot": {"page": "exam", "source": "dll",
                        "state": observation.evidence.state.to_dict(), "lesson_context": context.to_dict(),
                        "recommendation": decision, "policy_source": "rule-planner"}})
            except Exception:
                pass  # GUI telemetry cannot acquire control or stop the input owner.

    for _attempt in range(max_actions):
        if stop_requested():
            return finish(False, "user-stop-requested")
        try:
            observed = gateway.read_native(base, run_id=run_id)
            if observed.evidence is None or not isinstance(observed.raw_state, Mapping):
                raise ValueError("native lesson observation lacks raw/typed state")
            base = observed.evidence
            context = parse_native_lesson_context(observed.raw_state, observed.native_context,
                expected_produce_id=expected_produce_id, expected_idol_card_id=expected_idol_card_id)
            last_context = context
            binding = (context.produce_id, context.idol_card_id, context.step.step_type,
                       getattr(base, "session_transition_id", None), getattr(base, "step_context_digest", None))
            if identity is not None and identity != binding:
                return finish(False, "native-lesson-identity-changed")
            identity = binding
            progress(observed, context)
            if context.native_complete:
                return finish(True, "native-lesson-completed", evaluation=evaluate_native_lesson(context))
            if decision_provider is not None:
                decision = decision_provider(observed, context)
            elif context.plan_type == "ProducePlanType_Plan1":
                candidates = prepare_runtime_plan1_lesson(observed.raw_state, observed.native_context,
                    expected_produce_id=expected_produce_id, expected_idol_card_id=expected_idol_card_id)
                decision = choose_runtime_plan1_lesson(candidates)
            elif context.plan_type == "ProducePlanType_Plan3":
                decoded = decoded_runtime_plan3(observed)
                report = plan3_advisor(decoded)
                if not report.available:
                    return finish(False, "native-lesson-rule-policy-unavailable",
                                  detail=";".join(issue.code + ":" + issue.detail for issue in report.issues))
                plan = build_native_plan3_execution_plan(report, decoded)
                decision = {"action": plan.action, "decision_owner": "rule-planner", "learned_policy_used": False,
                            "policy": report.search["policy"], "projection": report.terminal}
            else:
                return finish(False, "native-lesson-plan-policy-unavailable", detail=context.plan_type)
            if not isinstance(decision, Mapping) or not isinstance(decision.get("action"), Mapping):
                raise ValueError("native lesson rule provider returned no action")
            if decision.get("decision_owner") != "rule-planner" or decision.get("learned_policy_used") is not False:
                raise ValueError("native lesson policy must explicitly identify its rule planner")
            action = decision["action"]
            kind = {"card": "play", "play": "play", "drink": "drink", "skip": "end_turn",
                    "turn-end": "end_turn", "end_turn": "end_turn"}.get(action.get("kind"))
            if kind is None:
                raise ValueError("native lesson rule selected an unknown action kind")
            progress(observed, context, decision=decision)
        except Exception as error:
            return finish(False, "native-lesson-preparation-failed", detail=f"{type(error).__name__}:{error}")
        if stop_requested():
            return finish(False, "user-stop-requested")
        try:
            outcome = gateway.execute(kind, observed.evidence, card_guid=action.get("card_guid"),
                slot=action.get("drink_slot_index", action.get("slot_index")), drink_id=action.get("drink_id"),
                **({"preferred_selection_guid": action["selected_card_guid"]} if action.get("selected_card_guid") else {}))
        except Exception as error:
            # The durable gateway may own a pending mutation. Do not retry it.
            return finish(False, "native-lesson-action-outcome-unknown", detail=f"{type(error).__name__}:{error}")
        if outcome.status == "replan" and not outcome.submitted:
            continue
        if outcome.submitted:
            steps.append({"action": dict(action), "decision": dict(decision), "status": outcome.status,
                          "request_id": outcome.request_id, "executor": "dll",
                          "state_before": observed.evidence.state.to_dict(),
                          "state_after": None if outcome.evidence is None else outcome.evidence.state.to_dict()})
        if outcome.status != "settled" or outcome.evidence is None or not isinstance(outcome.raw_state, Mapping):
            return finish(False, "native-lesson-action-not-settled", detail=outcome.detail)
        base = outcome.evidence
        if outcome.raw_state.get("isExamEndComplete") is True:
            try:
                last_context = parse_native_lesson_context(outcome.raw_state, outcome.native_context,
                    expected_produce_id=expected_produce_id, expected_idol_card_id=expected_idol_card_id)
                return finish(True, "native-lesson-completed", evaluation=evaluate_native_lesson(last_context))
            except (TypeError, ValueError) as error:
                return finish(False, "native-lesson-terminal-context-invalid", detail=str(error))
    return finish(False, "native-lesson-action-budget-reached")
