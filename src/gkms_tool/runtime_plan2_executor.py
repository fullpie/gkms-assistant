"""Plan2's DLL loop: observe once, choose once, submit once, observe outcome.

Only rule/model decision code is reused. This path has no Maa dependencies,
retained-save recovery chain, old replay journal, or visual settlement owner.
The shared native gateway owns the one pending action and its actual S'.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
import uuid

from .runtime_exam_executor import RuntimeExamGateway
from .training_artifact_io import atomic_write, canonical_json_bytes


def build_runtime_plan2_policy(context):
    from .plan2_native_unattended_loop import bind_plan2_native_exam_save_orchestrator
    from .plan2_native_exam_save_orchestrator import Plan2NativeExamSaveDependencies
    from .offline_rl_runtime_adapter import build_offline_rl_artifact_selector
    from .policy_bundle import PolicyBundle

    # The native observation policy owns the sole learned selection. Drinks
    # need only these pure boundary/catalog/transition providers, not another
    # evidence-only actor or unrelated residual/leaderboard artifacts.
    pure = bind_plan2_native_exam_save_orchestrator(
        draw_count=context.draw_count, hand_limit=context.hand_limit,
        dependencies=Plan2NativeExamSaveDependencies(),
    )
    bundle = PolicyBundle.load(context.learned_policy_bundle_path,
        component_roles=("exact_exam_policy",), optional_component_roles=("offline_rl_policy",))
    component = bundle.component("exact_exam_policy")
    if component.get("live_apply_allowed") is not True:
        raise ValueError("the selected bundle has no enabled Exam policy")
    try:
        offline_component = bundle.component("offline_rl_policy")
    except KeyError:
        offline_rl = None
    else:
        # Absent RL permits the existing BC route. A present but corrupt or
        # missing artifact is a validation failure, not an implicit fallback.
        offline_rl = build_offline_rl_artifact_selector(offline_component, project_root=bundle.project_root)
    from .runtime_plan2_drink_policy import RuntimePlan2DrinkPolicy
    from .runtime_plan2_native_observation_policy import RuntimePlan2NativeObservationPolicy

    return RuntimePlan2NativeObservationPolicy(bundle, offline_rl=offline_rl,
        drink_policy=RuntimePlan2DrinkPolicy(None, pure))


def run_runtime_plan2_exam(
    path: str | Path, *, context, gateway: RuntimeExamGateway | None = None,
    policy: Callable | None = None, evidence_loader: Callable | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    return run_runtime_exam_policy(path, max_actions=context.max_actions,
        policy=policy or build_runtime_plan2_policy(context), gateway=gateway, evidence_loader=evidence_loader,
        stop_requested=stop_requested, progress_callback=progress_callback, run_id=run_id,
        report_schema="gkms.plan2-runtime-exam.v1", error_prefix="native-plan2")


def run_runtime_exam_policy(
    path: str | Path, *, max_actions: int, policy: Callable,
    gateway: RuntimeExamGateway | None = None, evidence_loader: Callable | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    run_id: str | None = None, report_schema="gkms.native-policy-exam.v1", error_prefix="native-exam",
) -> dict[str, Any]:
    """One shared observe/choose/execute loop for any native pure policy.

    A provider with choose_observation receives the same native response as
    the hashed raw state. Existing evidence-only providers remain unchanged.
    """
    gateway = gateway or RuntimeExamGateway(cancelled=stop_requested)
    binding = getattr(policy, 'runtime_policy_binding', None)
    if isinstance(binding, Mapping):
        gateway.bind_model_policy(policy)
    base = evidence_loader(Path(path)) if evidence_loader is not None else None
    directory = gateway.client.root / "exam_runs" / uuid.uuid4().hex
    steps = []
    native_capture = None
    stage_binding = None
    try:
        from .native_runtime_stage_capture import begin_native_runtime_stage_capture
        native_capture = begin_native_runtime_stage_capture(gateway.client.read_status()["pid"])
    except (AttributeError, OSError, ValueError):
        pass  # Capture archival is observational; the action gateway owns proof.

    def finish(accepted, terminal, reason):
        result = {"schema": report_schema, "accepted": accepted, "terminal": terminal,
                  "reason": reason, "actions_executed": sum(s.get("input_submitted") is True for s in steps),
                  "steps": steps, "action_executor": "dll-managed-single-action", "source_run_id": run_id if base is None else base.run_id}
        if native_capture is not None:
            capture = native_capture.finish().to_dict()
            if stage_binding is not None:
                capture["stage_binding"] = stage_binding
            result["native_runtime_stage_capture"] = capture
        atomic_write(directory / "report.json", canonical_json_bytes(result))
        return result

    for index in range(max_actions):
        if stop_requested():
            return finish(False, False, "user-stop-requested")
        try:
            observed = gateway.read_native(base, run_id=run_id) if base is None else gateway.read_native(base)
            if observed.evidence is None or observed.raw_state is None:
                return finish(False, False, "native-state-unavailable")
            base = observed.evidence
            if native_capture is not None and stage_binding is None:
                stage_binding = {"source_run_id": base.run_id,
                                 "session_transition_id": base.session_transition_id,
                                 "step_context_digest": base.step_context_digest,
                                 "step_type_value": base.state.step_type_value}
            runtime = base.state.root_runtime
            if runtime is not None and runtime.is_exam_end_complete:
                return finish(True, True, "native-exam-terminal")
            observed_policy = getattr(policy, "choose_observation", None)
            # The observation interface is an explicit provider opt-in;
            # dynamic attributes do not create a second decision callback.
            if callable(getattr(type(policy), "choose_observation", None)):
                if observed.native_snapshot is None:
                    # A reconciled recorder receipt proves S' but carries no
                    # fresh native legal pool. Read the current boundary next.
                    continue
                decision = observed_policy(observed)
            else:
                decision = policy(base)
            action = decision.best_action
            if action is None:
                issues = list(getattr(decision, "blockers", ()))
                blockers = [getattr(issue, "code", str(issue)) for issue in issues]
                atomic_write(directory / "policy_failure.json", canonical_json_bytes({
                    "source_run_id": run_id, "state_source": "dll", "native_source_path": base.source_path,
                    "native_source_sha256": base.source_sha256,
                    "native_context": observed.native_context,
                    "policy_source": getattr(decision, "policy_source", None),
                    "blockers": [{"code": getattr(issue, "code", str(issue)),
                                  "detail": getattr(issue, "detail", "")} for issue in issues],
                }))
                return finish(False, False, "policy-no-decision:" + ",".join(blockers))
            selection_guid = getattr(action, "selected_card_guid", None)
            if selection_guid is not None and "exam.continuation" not in gateway.client.read_status().get("capabilities", ()):
                return finish(False, False, "native-secondary-selection-capability-required")
            selected = {"kind": action.kind, "card_guid": getattr(action, "card_guid", None),
                        "slot": getattr(action, "slot_index", None), "drink_id": getattr(action, "drink_id", None),
                        "selected_card_guid": selection_guid}
            if progress_callback is not None:
                try:
                    progress_callback({"source_run_id": base.run_id, "current_page": "exam",
                        "monitor_snapshot": {"page": "exam", "source": "dll", "state": base.state.to_dict(),
                                             "recommendation": selected, "policy_source": getattr(decision, "policy_source", None),
                                             **({'exam_policy':dict(policy.runtime_policy_binding)}
                                                if isinstance(binding, Mapping) else {})}})
                except Exception:
                    pass
            if stop_requested():
                return finish(False, False, "user-stop-requested")
            outcome = gateway.execute(action.kind, base, card_guid=selected["card_guid"],
                                      slot=selected["slot"], drink_id=selected["drink_id"],
                                      preferred_selection_guid=selection_guid,
                                      **({"secondary_policy": action.secondary_policy}
                                         if getattr(action, "secondary_policy", None) else {}))
            if outcome.status == "replan":
                if outcome.evidence is not None:
                    base = outcome.evidence
                continue
            record = {"index": index, "state_source": "dll", "state_before": observed.raw_state,
                      "action": selected, "policy_source": getattr(decision, "policy_source", None),
                      "legal_action_ids": list(getattr(decision, "policy_legal_action_ids", ())),
                      "request_id": outcome.request_id, "input_submitted": outcome.submitted,
                      "status": outcome.status, "state_after": outcome.raw_state,
                      "native_context_before": observed.native_context, "native_context_after": outcome.native_context,
                      "native_provenance": None if outcome.provenance is None else outcome.provenance.to_dict()}
            if observed.native_snapshot is not None and "native_legal_inputs" in observed.native_snapshot:
                record["native_legal_inputs"] = observed.native_snapshot["native_legal_inputs"]
                record["native_legal_revision"] = observed.native_snapshot.get("revision")
                record["native_legal_session_generation"] = observed.native_session_generation
            policy_detail = getattr(decision, "metadata", None)
            if isinstance(policy_detail, Mapping):
                record["policy_detail"] = dict(policy_detail)
            drink_report = getattr(policy, "last_drink_decision", None)
            if isinstance(drink_report, Mapping):
                record["drink_comparison"] = drink_report
            # This is native evidence. It is not promoted to a frozen training
            # trajectory until the existing collector validates the whole stage.
            atomic_write(directory / f"step-{index:03d}.json", canonical_json_bytes(record))
            steps.append({key: value for key, value in record.items() if key not in {"state_before", "state_after"}})
            if outcome.status != "settled" or outcome.evidence is None:
                return finish(False, False, outcome.detail)
            base = outcome.evidence
            if base.state.root_runtime is not None and base.state.root_runtime.is_exam_end_complete:
                return finish(True, True, "native-exam-terminal")
        except Exception as error:
            return finish(False, False, f"{error_prefix}:{type(error).__name__}:{error}")
    return finish(False, False, "native-action-budget-reached")
