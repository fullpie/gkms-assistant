"""Master-bound Plan1 audition loop with an optional pure drink refinement.

Each settled boundary is projected into the existing Plan1 native state,
enumerated as one complete root legal set, selected by the learned chain, and
submitted by the shared DLL gateway, or the explicitly selected legacy Maa
adapter. A NIA Pro DLL drink rule may refine an accepted PLAY; unknown drink
valuation retains that actor choice. Native legality and input guards remain
owned by the existing snapshot and single-action executor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
import hashlib
import time
from typing import Any, Final

from .master_db import DEFAULT_DATABASE
from .audition_local_save_state import LocalSaveExamState
from .audition_rules import DEFAULT_MASTER_DIR
from .runtime_mode_profile import load_runtime_mode_profile
from .plan1_learned_policy_runtime import (
    Plan1LearnedPolicyRuntime,
    Plan1LearnedRuntimeResult,
)
from .plan1_native_core import Plan1CompiledCard, compile_plan1_card_instance
from .plan1_runtime_simulator_bridge import project_plan1_runtime_state


SCHEMA: Final = "gkms.plan1-exact-learned-exam.v1"
NIA_PRODUCE_IDS: Final = frozenset({"produce-004", "produce-005"})
_STAGE_NAMES: Final = {16: "Mid1", 17: "Mid2", 18: "Final"}


def _opaque(evidence: object) -> Mapping[str, object] | None:
    state = getattr(evidence, "state", None)
    runtime = getattr(state, "root_runtime", None)
    fields = getattr(runtime, "opaque_fields", None)
    to_value = getattr(fields, "to_value", None)
    if callable(to_value):
        try:
            fields = to_value()
        except (RuntimeError, TypeError, ValueError):
            return None
    return fields if isinstance(fields, Mapping) else None


def _terminal(evidence: object) -> bool:
    state = getattr(evidence, "state", None)
    runtime = getattr(state, "root_runtime", None)
    return bool(getattr(runtime, "is_exam_end_complete", False))


def _submitted_actions(reports):
    return sum(bool(report.get("input_submitted")) for report in reports)


def _state_before(evidence: object, native_observation: object | None = None) -> Mapping[str, Any]:
    if native_observation is not None:
        from .exact_exam_state_projection import project_exact_exam_native_state
        from .training_artifact_io import canonical_json_bytes
        raw = getattr(native_observation, "raw_state", None)
        if raw is None:
            snapshot = getattr(native_observation, "native_snapshot", None)
            raw = snapshot.get("exam_save") if isinstance(snapshot, Mapping) else None
        observed_evidence = getattr(native_observation, "evidence", None)
        if (not isinstance(raw, Mapping) or observed_evidence is None
                or observed_evidence.digest() != evidence.digest()
                or hashlib.sha256(canonical_json_bytes(raw)).hexdigest() != evidence.source_sha256):
            raise ValueError("Plan1 actor raw state is not the same native observation")
        # Preserve all 89 native fields, including the actual empty playingCard
        # struct. Typed LocalSave is retained separately for local simulation.
        return project_exact_exam_native_state(raw)
    state = getattr(evidence, "state", None)
    to_dict = getattr(state, "to_dict", None)
    if not callable(to_dict):
        raise TypeError("Plan1 evidence state has no native mapping")
    value = to_dict()
    if not isinstance(value, Mapping):
        raise TypeError("Plan1 evidence state mapping is invalid")
    from .exact_exam_state_projection import project_exact_exam_native_state
    return project_exact_exam_native_state(value)


def _compile_current_cards(
    evidence: object,
    *,
    database: str | Path = DEFAULT_DATABASE,
) -> tuple[Plan1CompiledCard, ...]:
    """Compile every distinct current native card instance fail-closed."""

    state = getattr(evidence, "state", None)
    zones = getattr(state, "zones", None)
    cards: list[object] = []
    for name in ("hand", "deck", "grave", "lost", "hold"):
        values = getattr(zones, name, ())
        if isinstance(values, Sequence) and not isinstance(
            values, (str, bytes, bytearray)
        ):
            cards.extend(values)
    playing = getattr(state, "playing_card", None)
    if playing is not None:
        cards.append(playing)
    programs: dict[str, Plan1CompiledCard] = {}
    for card in cards:
        card_id = getattr(card, "card_id", None)
        upgrade = getattr(card, "effective_upgrade", None)
        if not isinstance(card_id, str) or not card_id or type(upgrade) is not int:
            raise ValueError("Plan1 native card identity is incomplete")
        guid = getattr(card, "guid", None)
        if not isinstance(guid, str) or not guid:
            raise ValueError("Plan1 native card GUID is incomplete")
        program = compile_plan1_card_instance(card, database=Path(database))
        if not isinstance(program, Plan1CompiledCard):
            raise TypeError("Plan1 card compiler returned an invalid program")
        program = replace(program, instance_guid=guid)
        if guid in programs and programs[guid] != program:
            raise ValueError("one Plan1 native GUID has conflicting runtime programs")
        programs[guid] = program
    return tuple(programs[key] for key in sorted(programs))


def run_plan1_exact_learned_exam(
    exam_save_path: str | Path,
    *,
    expected_produce_id: str,
    expected_idol_card_id: str | None = None,
    learned_policy_bundle_path: str | Path | None = None,
    evidence_loader: Callable[[Path], object] | None = None,
    runtime: Plan1LearnedPolicyRuntime | None = None,
    compilation_provider: Callable[[object], Sequence[Plan1CompiledCard]] | None = None,
    command_sender: Callable[..., Mapping[str, object]] | None = None,
    database: str | Path = DEFAULT_DATABASE,
    master_dir: str | Path = DEFAULT_MASTER_DIR,
    sleep: Callable[[float], None] = time.sleep,
    max_actions: int = 100,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, object]], None] | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    """Run one observed audition; optional drink timing never replaces a missing actor."""

    mode_profile = load_runtime_mode_profile(expected_produce_id, master_dir=Path(master_dir))
    if expected_idol_card_id is not None and (
        not isinstance(expected_idol_card_id, str) or not expected_idol_card_id
    ):
        raise ValueError("expected_idol_card_id must be non-empty text or None")
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    path = Path(exam_save_path).resolve()
    native_observation = [None]
    bound_native_identity = None
    if evidence_loader is not None and not callable(evidence_loader):
        raise TypeError("evidence_loader must be callable")
    if compilation_provider is None:
        compilation_provider = lambda evidence: _compile_current_cards(
            evidence, database=database
        )
    if not callable(compilation_provider):
        raise TypeError("compilation_provider must be callable")
    if runtime is None:
        from .runtime_command_client import input_backend

        if input_backend() == "dll":
            from .runtime_exam_executor import RuntimeExamGateway, RuntimePlan1ActionExecutor
            from .plan1_learned_policy_runtime import bind_plan1_learned_policy_runtime
            from .policy_bundle import PolicyBundle

            gateway = RuntimeExamGateway(sleep=sleep, cancelled=stop_requested)
            native_identity = [None]

            def read_native_evidence(source):
                observed = gateway.read_native(native_identity[0], run_id=run_id)
                native_observation[0] = observed
                native_identity[0] = observed.evidence
                return native_identity[0]

            evidence_loader = read_native_evidence
            runtime = bind_plan1_learned_policy_runtime(RuntimePlan1ActionExecutor(gateway),
                bundle=PolicyBundle.load(learned_policy_bundle_path,
                    component_roles=("exact_exam_policy",), optional_component_roles=("offline_rl_policy",)))
        else:
            from .plan1_existing_maa_executor import bind_active_plan1_existing_maa_learned_runtime

            runtime = bind_active_plan1_existing_maa_learned_runtime(
                evidence_reader=lambda: evidence_loader(path), command_sender=command_sender, sleep=sleep,
            )
    if not isinstance(runtime, Plan1LearnedPolicyRuntime):
        raise TypeError("runtime must be Plan1LearnedPolicyRuntime")
    if evidence_loader is None:
        # This default belongs only to the explicitly selected retained-save
        # or legacy path. The DLL branch supplies its own native reader above.
        from .initial_regular_autopilot import load_initial_regular_plan2_exam_evidence
        evidence_loader = load_initial_regular_plan2_exam_evidence

    from .runtime_exam_executor import RuntimeExamOutcome, RuntimePlan1ActionExecutor
    action_executor_label = "dll-managed-single-action" if isinstance(runtime.executor, RuntimePlan1ActionExecutor) else "existing-maa-single-action"

    def load_evidence(source):
        loaded = evidence_loader(source)
        if isinstance(loaded, RuntimeExamOutcome):
            native_observation[0] = loaded
            if loaded.status not in {"observed", "settled"} or loaded.evidence is None:
                raise ValueError("Plan1 native observation has no accepted state")
            return loaded.evidence
        return loaded

    def native_pool_for(current):
        observed = native_observation[0]
        if observed is None:
            return None
        if getattr(observed, "native_snapshot", None) is None:
            raise ValueError("Plan1 native observation has no primary input pool")
        if observed.evidence is None or observed.evidence.digest() != current.digest():
            raise ValueError("Plan1 native pool belongs to another observation")
        from .plan1_native_legal_pool import bind_plan1_native_legal_pool
        return bind_plan1_native_legal_pool(current, observed.native_snapshot,
            session_generation=observed.native_session_generation)

    reports: list[dict[str, object]] = []
    for _ in range(max_actions):
        if stop_requested():
            return {"schema": SCHEMA, "accepted": False, "terminal": False,
                    "reason": "user-stop-requested", "actions_executed": _submitted_actions(reports), "steps": reports}
        try:
            evidence = load_evidence(path)
        except Exception as error:
            return {
                "schema": SCHEMA,
                "accepted": False,
                "terminal": False,
                "reason": f"plan1-evidence-load-failed:{type(error).__name__}",
                "actions_executed": _submitted_actions(reports),
                "steps": reports,
            }
        state = getattr(evidence, "state", None)
        if progress_callback is not None and state is not None:
            try:
                progress_callback({"source_run_id": getattr(evidence, "run_id", None), "current_page": "exam",
                    "monitor_snapshot": {"page": "exam", "state": state.to_dict(), "source": "dll"}})
            except Exception:
                pass
        opaque = _opaque(evidence)
        if (
            opaque is None
            or opaque.get("planType") != 2
            or opaque.get("produceId") != expected_produce_id
        ):
            return {
                "schema": SCHEMA,
                "accepted": False,
                "terminal": False,
                "reason": "plan1-exact-boundary-identity-mismatch",
                "actions_executed": _submitted_actions(reports),
                "steps": reports,
            }
        if getattr(state, "exam_type", None) == 0 or getattr(state, "step_type_value", None) in {*range(1, 10), *range(29, 35)}:
            return {"schema": SCHEMA, "accepted": False, "terminal": False,
                    "reason": "plan1-lesson-projection-unavailable", "actions_executed": _submitted_actions(reports), "steps": reports,
                    "detail": "examType=0 uses a fixed lesson axis and score clear/limit borders; "
                              "Plan1 runtime scoring currently requires audition examType=1 and the learned stage has no lesson binding.",
                    "step_type": getattr(state, "step_type_value", None)}
        observation = native_observation[0]
        raw_native = getattr(observation, "raw_state", None)
        context_native = getattr(observation, "native_context", None)
        if raw_native is None:
            # Injection seam for offline callers carrying one genuine native
            # observation; expectations alone never create this authority.
            raw_native = getattr(evidence, "raw_state", None)
            context_native = getattr(evidence, "native_context", None)
        if raw_native is not None:
            try:
                from .runtime_nia_plan3 import bind_runtime_audition_identity
                identity = bind_runtime_audition_identity(raw_native, context_native,
                    expected_produce_id=expected_produce_id, expected_idol_card_id=expected_idol_card_id,
                    master_dir=Path(master_dir))
                if bound_native_identity is not None and any(identity[key] != bound_native_identity[key]
                        for key in identity if key != "identity_source"):
                    raise ValueError("native Plan1 audition identity changed within one executor")
                bound_native_identity = identity
            except (KeyError, TypeError, ValueError, OSError) as error:
                return {"schema": SCHEMA, "accepted": False, "terminal": False,
                        "reason": "plan1-native-audition-binding-unavailable", "detail": str(error),
                        "actions_executed": _submitted_actions(reports), "steps": reports}
        elif expected_produce_id not in NIA_PRODUCE_IDS:
            return {"schema": SCHEMA, "accepted": False, "terminal": False,
                    "reason": "plan1-native-audition-context-missing", "actions_executed": _submitted_actions(reports), "steps": reports,
                    "detail": "new mode requires observed serializer and same-snapshot context or a unique Master match"}
        if _terminal(evidence):
            return {
                "schema": SCHEMA,
                "accepted": True,
                "terminal": True,
                "reason": "plan1-exact-learned-terminal",
                "actions_executed": _submitted_actions(reports),
                "steps": reports,
                "decision_owner": "offline-rl-to-behavior-cloning",
                "action_executor": action_executor_label,
                "produce_id": expected_produce_id,
                "idol_card_id": expected_idol_card_id,
                "native_audition_scope": bound_native_identity,
                "produce_type": mode_profile.produce_type,
            }
        if (
            getattr(state, "is_native_actionable_settled", False) is not True
            or getattr(state, "exam_type", None) not in {0, 1}
        ):
            return {
                "schema": SCHEMA,
                "accepted": False,
                "terminal": False,
                "reason": "plan1-exact-boundary-not-actionable",
                "actions_executed": _submitted_actions(reports),
                "steps": reports,
            }
        stage = _STAGE_NAMES.get(getattr(state, "step_type_value", None))
        if stage is None:
            return {
                "schema": SCHEMA,
                "accepted": False,
                "terminal": False,
                "reason": "plan1-exact-stage-unavailable",
                "actions_executed": _submitted_actions(reports),
                "steps": reports,
            }
        try:
            native_binding = native_pool_for(evidence)
            state_before = _state_before(evidence, native_observation[0] if native_binding is not None else None)
            projection = project_plan1_runtime_state(
                # The actor consumes the shared Exact-model native projection.
                # Keep the already parsed state for the simulator bridge.
                state if isinstance(state, LocalSaveExamState) else state_before,
                stage_label=stage,
            )
            from .plan1_native_policy_adapter import (
                build_plan1_master_drink_candidate_provider, require_plan1_policy_projection,
            )
            drink_provider = None
            if native_binding is None:
                # Explicit legacy path only. A present partial/invalid native
                # pool never falls back to a simulator-derived complete set.
                require_plan1_policy_projection(projection)
                if opaque.get("drinkList"):
                    drink_provider = build_plan1_master_drink_candidate_provider(projection, database=database)
            drink_policy = None
            terminal_policy = None
            if (expected_produce_id == "produce-004" and isinstance(runtime.executor, RuntimePlan1ActionExecutor)
                    and projection.parsed is not None and projection.parsed.remain_turn == 1):
                from .runtime_plan1_terminal_policy import RuntimePlan1TerminalPolicy
                terminal_policy = RuntimePlan1TerminalPolicy(projection, database=database)
            if expected_produce_id == "produce-004" and opaque.get("drinkList") and isinstance(runtime.executor, RuntimePlan1ActionExecutor):
                from .runtime_plan1_drink_policy import RuntimePlan1DrinkPolicy
                drink_policy = RuntimePlan1DrinkPolicy(projection, database=database)
            compilation = () if native_binding is not None else tuple(compilation_provider(evidence))
            result: Plan1LearnedRuntimeResult = runtime.execute_boundary(
                evidence=evidence,
                state_before=state_before,
                native_state=projection.state,
                compilation=compilation,
                stage=stage,
                current_evidence_reader=lambda: load_evidence(path),
                flow=None,
                # None asks the unified snapshot to read the exact ordered
                # native inventory. Every bound audition mode supplies the
                # same per-slot Master provider; unsupported effects still stop.
                drink_inventory=None,
                drink_candidate_provider=drink_provider,
                drink_policy=drink_policy,
                terminal_policy=terminal_policy,
                native_legal_binding=native_binding,
                current_native_legal_reader=native_pool_for if native_binding is not None else None,
            )
        except Exception as error:
            return {
                "schema": SCHEMA,
                "accepted": False,
                "terminal": False,
                "reason": f"plan1-exact-runtime-failed:{type(error).__name__}",
                "detail": str(error),
                "actions_executed": _submitted_actions(reports),
                "steps": reports,
            }
        report = result.to_dict()
        if bound_native_identity is not None:
            report["native_audition_scope"] = bound_native_identity
        if getattr(projection, "notes", ()):
            report["projection_notes"] = list(projection.notes)
        if native_binding is not None and getattr(projection, "blockers", ()):
            report["evaluation_blockers"] = [value.to_dict() for value in projection.blockers]
        reports.append(report)
        if not result.input_submitted and result.blockers in {
                ("learned-boundary-stale",),
                ("maa:dll-replan:stale native state revision",),
                ("maa:dll-replan:DLL state advanced before input",),
                ("maa:dll-replan:previous DLL action reconciled",),
        }:
            # The old command was not executed (or an existing one already
            # reconciled). Start a new observation/decision; never resubmit it.
            report["replanned"] = True
            continue
        after_runtime = (result.state_after or {}).get("root_runtime")
        if result.completed and isinstance(after_runtime, Mapping) and after_runtime.get("is_exam_end_complete") is True:
            return {"schema": SCHEMA, "accepted": True, "terminal": True,
                    "reason": "native-plan1-terminal", "actions_executed": _submitted_actions(reports),
                    "steps": reports, "decision_owner": "offline-rl-to-behavior-cloning",
                    "action_executor": action_executor_label, "native_audition_scope": bound_native_identity,
                    "produce_type": mode_profile.produce_type}
        if not result.completed:
            return {
                "schema": SCHEMA,
                "accepted": False,
                "terminal": False,
                "reason": (
                    result.blockers[0]
                    if result.blockers
                    else "plan1-learned-boundary-stopped"
                ),
                "input_submitted": result.input_submitted,
                "actions_executed": sum(
                    bool(step.get("input_submitted")) for step in reports
                ),
                "steps": reports,
                "decision_owner": "offline-rl-to-behavior-cloning",
                "action_executor": action_executor_label,
            }
    return {
        "schema": SCHEMA,
        "accepted": False,
        "terminal": False,
        "reason": "plan1-exact-action-budget-reached",
        "actions_executed": _submitted_actions(reports),
        "steps": reports,
        "decision_owner": "offline-rl-to-behavior-cloning",
        "action_executor": action_executor_label,
    }


__all__ = [
    "NIA_PRODUCE_IDS",
    "SCHEMA",
    "run_plan1_exact_learned_exam",
]
