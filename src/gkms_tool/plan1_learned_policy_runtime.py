"""Fail-closed Plan1 RL -> BC runtime over the exact native action set.

This module is deliberately Plan1-specific only at the legality boundary.  It
reuses the plan-neutral learned selector and accepts the existing DLL or Maa
single-action executor. An optional pure drink rule can refine an accepted
PLAY. The physical executor receives exactly one final legal candidate.

The caller must supply the captured native ``state_before`` and the existing
``Plan1NativeStageState`` projection from that same settled boundary.  Missing
projection, incomplete drink legality, a stale boundary, no learned decision,
or an incomplete executor receipt all stop without a policy fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Final, Protocol

from .exact_bc_live_canary import ExactBCCanaryPolicy, ExactBCPolicyBundle
from .learned_policy_selector import (
    LearnedPolicyDecision,
    OfflineRLSelector,
    select_plan_neutral_learned_action,
)
from .plan1_native_core import (
    Plan1CompiledCard,
    Plan1DeckCompilation,
    Plan1NativeSettings,
)
from .plan1_native_stage import (
    Plan1HandAddSupportResolver,
    Plan1NativeStageState,
)
from .unified_legal_action_snapshot import (
    UnifiedLegalActionSnapshot,
    build_unified_legal_action_snapshot,
)


SCHEMA: Final = "gkms.plan1-learned-policy-runtime.v1"
PLAN1_FLOWS: Final = (
    "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamLessonBuff",
    "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamParameterBuff",
)


def _json_mapping(value: Mapping[str, object], label: str) -> dict[str, object]:
    try:
        detached = json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON-compatible") from error
    if not isinstance(detached, dict):
        raise ValueError(f"{label} must be an object")
    return detached


def _digest(value: object) -> str | None:
    digest = getattr(value, "digest", None)
    if callable(digest):
        try:
            result = digest()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return None
        if isinstance(result, str) and result:
            return result
    return None


@dataclass(frozen=True, slots=True)
class Plan1MaaExecutionReceipt:
    """Receipt from the existing Maa single-action executor.

    A successful receipt includes the exact action identity, the frozen
    before digest, one changed settled after digest, native ``state_after``,
    and a non-empty submission proof.  This is an execution contract only;
    it exposes no candidate-ranking callback to Maa.
    """

    action_id: str
    boundary_before_digest: str
    input_submitted: bool
    boundary_after_digest: str | None = None
    state_after: Mapping[str, object] | None = None
    submission_proof: Mapping[str, object] | None = None
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("action_id", "boundary_before_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        if type(self.input_submitted) is not bool:
            raise TypeError("input_submitted must be bool")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, str) or not value for value in blockers):
            raise ValueError("executor blockers must contain non-empty text")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(blockers)))
        if not self.input_submitted:
            if not blockers:
                raise ValueError("non-submitted receipt needs a blocker")
            if any(
                value is not None
                for value in (
                    self.boundary_after_digest,
                    self.state_after,
                    self.submission_proof,
                )
            ):
                raise ValueError("non-submitted receipt cannot claim after-state")
            return
        if blockers:
            if self.boundary_after_digest is not None or self.state_after is not None:
                raise ValueError(
                    "blocked submitted receipt cannot claim settled after-state"
                )
            if not isinstance(self.submission_proof, Mapping) or not self.submission_proof:
                raise ValueError(
                    "blocked submitted receipt needs a submission proof"
                )
            object.__setattr__(
                self,
                "submission_proof",
                _json_mapping(self.submission_proof, "submission_proof"),
            )
            return
        if (
            not isinstance(self.boundary_after_digest, str)
            or not self.boundary_after_digest
            or self.boundary_after_digest == self.boundary_before_digest
        ):
            raise ValueError("submitted receipt needs one changed after digest")
        if not isinstance(self.state_after, Mapping):
            raise ValueError("submitted receipt needs native state_after")
        if not isinstance(self.submission_proof, Mapping) or not self.submission_proof:
            raise ValueError("submitted receipt needs a submission proof")
        object.__setattr__(
            self, "state_after", _json_mapping(self.state_after, "state_after")
        )
        object.__setattr__(
            self,
            "submission_proof",
            _json_mapping(self.submission_proof, "submission_proof"),
        )


class Plan1MaaSingleActionExecutor(Protocol):
    """Physical-only Maa seam: execute the one selected exact candidate."""

    def __call__(
        self,
        *,
        candidate: Mapping[str, object],
        evidence: object,
        snapshot: UnifiedLegalActionSnapshot,
    ) -> Plan1MaaExecutionReceipt: ...


@dataclass(frozen=True, slots=True)
class Plan1LearnedRuntimeResult:
    status: str
    stage: str
    boundary_digest: str | None
    input_submitted: bool
    decision: LearnedPolicyDecision | None = None
    state_before: Mapping[str, object] | None = None
    legal_snapshot: UnifiedLegalActionSnapshot | None = None
    action: Mapping[str, object] | None = None
    state_after: Mapping[str, object] | None = None
    submission_proof: Mapping[str, object] | None = None
    blockers: tuple[str, ...] = ()
    schema: str = SCHEMA
    # decision remains the original RL/BC proposal. Actual selected input and
    # an optional pure rule refinement are recorded separately.
    action_selection: Mapping[str, object] | None = None
    native_legal_inputs: Mapping[str, object] | None = None

    @property
    def completed(self) -> bool:
        return (
            self.status == "completed"
            and self.input_submitted
            and self.decision is not None
            and self.decision.ready
            and self.state_before is not None
            and self.legal_snapshot is not None
            and self.legal_snapshot.complete
            and self.action is not None
            and self.state_after is not None
            and not self.blockers
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "status": self.status,
            "completed": self.completed,
            "stage": self.stage,
            "boundary_digest": self.boundary_digest,
            "input_submitted": self.input_submitted,
            "decision": None if self.decision is None else self.decision.to_dict(),
            "state_before": (
                None if self.state_before is None else dict(self.state_before)
            ),
            "legal_snapshot": (
                None
                if self.legal_snapshot is None
                else self.legal_snapshot.to_dict()
            ),
            "action": None if self.action is None else dict(self.action),
            "state_after": (
                None if self.state_after is None else dict(self.state_after)
            ),
            "submission_proof": (
                None
                if self.submission_proof is None
                else dict(self.submission_proof)
            ),
            "blockers": list(self.blockers),
            "action_selection": None if self.action_selection is None else dict(self.action_selection),
            "native_legal_inputs": None if self.native_legal_inputs is None else dict(self.native_legal_inputs),
        }


def build_plan1_exact_learned_snapshot(
    evidence: object,
    *,
    native_state: Plan1NativeStageState,
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard],
    flow: str | Sequence[str] | None = None,
    settings: Plan1NativeSettings | None = None,
    drink_inventory: Sequence[object] | None,
    drink_candidate_provider: Callable[..., Sequence[object]] | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
) -> UnifiedLegalActionSnapshot:
    """Build Plan1's one complete root set through the existing enumerator."""

    if not isinstance(native_state, Plan1NativeStageState):
        raise TypeError("native_state must be Plan1NativeStageState")
    if not isinstance(compilation, (Plan1DeckCompilation, Sequence)) or isinstance(
        compilation, (str, bytes, bytearray)
    ):
        raise TypeError("compilation must be Plan1DeckCompilation or a sequence")
    return build_unified_legal_action_snapshot(
        evidence,
        flow=flow,
        plan1_state=native_state,
        plan1_compilation=compilation,
        plan1_settings=settings,
        plan1_drink_inventory=drink_inventory,
        plan1_drink_candidate_provider=drink_candidate_provider,
        plan1_hand_add_support_resolver=hand_add_support_resolver,
        source_label="plan1-exact-native-learned-runtime",
    )


@dataclass(frozen=True, slots=True)
class Plan1LearnedPolicyRuntime:
    """One Plan1 actor boundary, optional drink refinement, one input executor."""

    bundle: ExactBCPolicyBundle
    executor: Plan1MaaSingleActionExecutor
    offline_rl: OfflineRLSelector | None = None
    bc_policy: ExactBCCanaryPolicy = ExactBCCanaryPolicy(
        enabled=True,
        allowed_flows=PLAN1_FLOWS,
        minimum_probability=0.0,
        minimum_margin=0.0,
    )

    def __post_init__(self) -> None:
        if not callable(self.executor):
            raise TypeError("Plan1 Maa single-action executor must be callable")
        if not isinstance(self.bc_policy, ExactBCCanaryPolicy):
            raise TypeError("bc_policy must be ExactBCCanaryPolicy")

    def execute_boundary(
        self,
        *,
        evidence: object,
        state_before: Mapping[str, Any],
        native_state: Plan1NativeStageState,
        compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard],
        stage: str,
        current_evidence_reader: Callable[[], object],
        flow: str | Sequence[str] | None = None,
        settings: Plan1NativeSettings | None = None,
        drink_inventory: Sequence[object] | None,
        drink_candidate_provider: Callable[..., Sequence[object]] | None = None,
        hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
        drink_policy: Callable[..., Mapping[str, object]] | None = None,
        terminal_policy: Callable[..., Mapping[str, object]] | None = None,
        native_legal_binding: object | None = None,
        current_native_legal_reader: Callable[[object], object] | None = None,
    ) -> Plan1LearnedRuntimeResult:
        """Select RL then BC and dispatch exactly once while boundary is fresh."""

        if not isinstance(state_before, Mapping):
            raise TypeError("state_before must be a mapping")
        if not isinstance(stage, str) or not stage:
            raise ValueError("stage must be non-empty text")
        if not callable(current_evidence_reader):
            raise TypeError("current_evidence_reader must be callable")
        native_pool_report = None
        if native_legal_binding is not None:
            from .plan1_native_legal_pool import NATIVE_SELECTOR_POLICY, Plan1NativeLegalBinding
            if not isinstance(native_legal_binding, Plan1NativeLegalBinding):
                raise TypeError("native_legal_binding must come from the native pool binder")
            native_legal_binding.require_current(evidence)
            snapshot = native_legal_binding.snapshot
            native_pool_report = native_legal_binding.to_dict()
        else:
            snapshot = build_plan1_exact_learned_snapshot(
                evidence, native_state=native_state, compilation=compilation, flow=flow, settings=settings,
                drink_inventory=drink_inventory, drink_candidate_provider=drink_candidate_provider,
                hand_add_support_resolver=hand_add_support_resolver)
        if not snapshot.complete:
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=False,
                native_legal_inputs=native_pool_report,
                legal_snapshot=snapshot,
                blockers=tuple(
                    f"legal:{value}" for value in snapshot.blockers
                ) or ("legal:unified-set-incomplete",),
            )

        decision = select_plan_neutral_learned_action(
            state_before=state_before,
            snapshot=snapshot,
            stage=stage,
            offline_rl=self.offline_rl,
            bc_bundle=self.bundle,
            bc_policy=self.bc_policy,
        )
        if not decision.ready or decision.candidate is None:
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=False,
                decision=decision,
                native_legal_inputs=native_pool_report,
                blockers=("learned-policy-no-decision", *decision.blockers),
            )

        candidate = dict(decision.candidate)
        action_selection = None
        terminal_comparison = None
        terminal_applied = False
        if terminal_policy is not None and candidate.get("kind") in {"play", "use-hand", "card"}:
            try:
                terminal_comparison = _json_mapping(terminal_policy(decision=decision, snapshot=snapshot,
                    evidence=evidence, state_before=state_before, native_state=native_state, stage=stage),
                    "terminal comparison")
                proposed = terminal_comparison.get("candidate")
                if proposed is not None:
                    if terminal_comparison.get("blockers") or terminal_comparison.get("boundary_digest") != snapshot.boundary_digest:
                        raise ValueError("terminal refinement is blocked or belongs to another boundary")
                    matches = [value for value in snapshot.candidates if value["action_id"] == proposed.get("action_id")]
                    if (len(matches) != 1 or matches[0].get("kind") not in {"play", "use-hand"}
                            or dict(proposed) != dict(matches[0])):
                        raise ValueError("terminal refinement is not the exact native legal PLAY")
                    candidate = dict(matches[0])
                    terminal_applied = True
            except Exception as error:
                terminal_comparison = {"candidate": None,
                    "blockers": [f"terminal-refinement-unavailable:{type(error).__name__}:{error}"]}
            action_selection = {"source": terminal_comparison.get("policy_id") if terminal_applied else decision.source,
                "actor_action_id": decision.action_id, "selected_action_id": candidate["action_id"], "actual_action_id": None,
                "probability": None if terminal_applied else decision.probability, "rule_applied": terminal_applied,
                "terminal_comparison": terminal_comparison}
        play_candidate = dict(candidate)
        play_source = action_selection["source"] if terminal_applied else decision.source
        if drink_policy is not None and candidate.get("kind") in {"play", "use-hand", "card"}:
            # The hook can refine an existing PLAY to one observed legal
            # DRINK only. It cannot fabricate a learned probability, acquire
            # control when the actor abstains, or queue the simulated PLAY.
            try:
                baseline_kwargs = {"baseline_candidate": play_candidate} if terminal_applied else {}
                comparison = drink_policy(decision=decision, snapshot=snapshot, evidence=evidence,
                    state_before=state_before, native_state=native_state, stage=stage, **baseline_kwargs)
                comparison = _json_mapping(comparison, "drink comparison")
                proposed = comparison.get("candidate")
                if proposed is not None:
                    if comparison.get("blockers") or comparison.get("boundary_digest") != snapshot.boundary_digest:
                        raise ValueError("drink refinement is blocked or belongs to another boundary")
                    if (candidate.get("kind") not in {"play", "use-hand", "card"} or not isinstance(proposed, Mapping)
                            or proposed.get("kind") not in {"drink", "use-drink"}):
                        raise ValueError("drink refinement requires an actor PLAY and one DRINK")
                    matches = [value for value in snapshot.candidates if value["action_id"] == proposed.get("action_id")]
                    if len(matches) != 1 or matches[0].get("kind") != "drink":
                        raise ValueError("drink refinement is not a unique native legal candidate")
                    bound = matches[0]
                    for key in ("slot_index", "drink_id", "instance_id", "selected_card_guid"):
                        if proposed.get(key, "") != bound.get(key, ""):
                            raise ValueError(f"drink refinement native {key} mismatch")
                    candidate = dict(bound)
                action_selection = {"source": comparison.get("policy_id", "native-plan1-drink-rule") if proposed is not None else play_source,
                    "actor_action_id": decision.action_id, "selected_action_id": candidate["action_id"], "actual_action_id": None,
                    "probability": None if proposed is not None or terminal_applied else decision.probability,
                    "rule_applied": proposed is not None or terminal_applied, "comparison": comparison}
            except Exception as error:
                # A failed value hint does not revoke the actor's already
                # accepted legal choice. The fresh-read/receipt guards below
                # still apply to whichever one action is ultimately selected.
                candidate = dict(play_candidate)
                action_selection = {"source": play_source, "actor_action_id": decision.action_id,
                    "selected_action_id": candidate["action_id"], "actual_action_id": None,
                    "probability": None if terminal_applied else decision.probability, "rule_applied": terminal_applied,
                    "comparison": {"candidate": None, "blockers": [f"drink-refinement-unavailable:{type(error).__name__}:{error}"]}}
        if terminal_comparison is not None:
            action_selection["terminal_comparison"] = terminal_comparison

        if (native_legal_binding is not None and candidate.get("kind") in {"play", "drink"}
                and not candidate.get("selected_card_guid")):
            # Primary legality does not predict the later selector or its
            # GUIDs. The same gateway/pending owner resolves only an actually
            # observed, parent-bound selector using this explicit policy.
            candidate["secondary_policy"] = NATIVE_SELECTOR_POLICY

        try:
            current = current_evidence_reader()
        except Exception as error:
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=False,
                decision=decision,
                action_selection=action_selection,
                native_legal_inputs=native_pool_report,
                blockers=(f"pre-dispatch-read-failed:{type(error).__name__}",),
            )
        if _digest(current) != snapshot.boundary_digest:
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=False,
                decision=decision,
                action_selection=action_selection,
                native_legal_inputs=native_pool_report,
                blockers=("learned-boundary-stale",),
            )
        if native_legal_binding is not None:
            try:
                if current_native_legal_reader is None:
                    raise ValueError("current native pool reader is missing")
                refreshed = current_native_legal_reader(current)
                if not isinstance(refreshed, Plan1NativeLegalBinding):
                    raise ValueError("current native pool is missing")
                refreshed.require_current(current)
                if (not refreshed.snapshot.complete or refreshed.pool.session_generation != native_legal_binding.pool.session_generation
                        or refreshed.pool.sequence_key != native_legal_binding.pool.sequence_key
                        or refreshed.snapshot.candidates != snapshot.candidates):
                    raise ValueError("native pool changed or became incomplete after selection")
            except Exception as error:
                return Plan1LearnedRuntimeResult(status="stopped", stage=stage, boundary_digest=snapshot.boundary_digest,
                    input_submitted=False, decision=decision, action_selection=action_selection,
                    native_legal_inputs=native_pool_report,
                    blockers=(f"native-legal-pool-pre-dispatch:{type(error).__name__}:{error}",))

        try:
            receipt = self.executor(
                candidate=candidate,
                evidence=evidence,
                snapshot=snapshot,
            )
        except Exception as error:
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=False,
                decision=decision,
                action_selection=action_selection,
                native_legal_inputs=native_pool_report,
                action=candidate,
                blockers=(f"maa-executor-failed:{type(error).__name__}",),
            )
        if not isinstance(receipt, Plan1MaaExecutionReceipt):
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=False,
                decision=decision,
                action_selection=action_selection,
                native_legal_inputs=native_pool_report,
                action=candidate,
                blockers=("maa-executor-receipt-invalid",),
            )
        if action_selection is not None:
            action_selection = {**action_selection, "actual_action_id": receipt.action_id if receipt.input_submitted else None}
        if (
            receipt.action_id != candidate["action_id"]
            or receipt.boundary_before_digest != snapshot.boundary_digest
        ):
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=receipt.input_submitted,
                decision=decision,
                action_selection=action_selection,
                native_legal_inputs=native_pool_report,
                action=candidate,
                state_after=receipt.state_after,
                submission_proof=receipt.submission_proof,
                blockers=("maa-executor-receipt-identity-mismatch",),
            )
        if receipt.blockers:
            return Plan1LearnedRuntimeResult(
                status="stopped",
                stage=stage,
                boundary_digest=snapshot.boundary_digest,
                input_submitted=receipt.input_submitted,
                decision=decision,
                action_selection=action_selection,
                native_legal_inputs=native_pool_report,
                action=candidate,
                state_after=receipt.state_after,
                submission_proof=receipt.submission_proof,
                blockers=tuple(
                    f"maa:{value}" for value in receipt.blockers
                ),
            )
        return Plan1LearnedRuntimeResult(
            status="completed",
            stage=stage,
            boundary_digest=snapshot.boundary_digest,
            input_submitted=True,
            decision=decision,
            action_selection=action_selection,
            native_legal_inputs=native_pool_report,
            state_before=_json_mapping(state_before, "state_before"),
            legal_snapshot=snapshot,
            action=candidate,
            state_after=receipt.state_after,
            submission_proof=receipt.submission_proof,
        )


class Plan1LearnedRuntimeConfigurationError(ValueError):
    """The active bundle cannot own Plan1 live decisions."""


def bind_plan1_learned_policy_runtime(
    executor: Plan1MaaSingleActionExecutor,
    *,
    bundle: ExactBCPolicyBundle,
) -> Plan1LearnedPolicyRuntime:
    """Bind one live bundle; invalid activation fails instead of using Maa policy."""

    payload = getattr(bundle, "payload", None)
    runtime = payload.get("runtime") if isinstance(payload, Mapping) else None
    try:
        component = bundle.component("exact_exam_policy")
    except Exception as error:
        raise Plan1LearnedRuntimeConfigurationError(
            "exact learned component unavailable"
        ) from error
    if not (
        isinstance(runtime, Mapping)
        and runtime.get("mode") == "learned-policy"
        and runtime.get("default_enabled") is True
        and runtime.get("fallback") == "stop-on-no-learned-decision"
        and component.get("shadow_ready") is True
        and component.get("live_apply_allowed") is True
    ):
        raise Plan1LearnedRuntimeConfigurationError(
            "bundle is not enabled for fail-closed learned runtime"
        )

    offline_rl: OfflineRLSelector | None = None
    try:
        offline_component = bundle.component("offline_rl_policy")
    except KeyError:
        pass
    else:
        try:
            from .offline_rl_runtime_adapter import (
                build_offline_rl_artifact_selector,
            )

            offline_rl = build_offline_rl_artifact_selector(
                offline_component,
                project_root=getattr(bundle, "project_root", Path.cwd()),
            )
        except (FileNotFoundError, OSError, TypeError, ValueError):
            # A broken optional RL artifact abstains; live Exact BC remains
            # the final learned fallback.  Maa never receives policy control.
            offline_rl = None
    return Plan1LearnedPolicyRuntime(
        bundle=bundle,
        executor=executor,
        offline_rl=offline_rl,
    )


def bind_active_plan1_learned_policy_runtime(
    executor: Plan1MaaSingleActionExecutor,
) -> Plan1LearnedPolicyRuntime:
    """Load and bind the active policy bundle for the next Plan1 boundary."""

    from .policy_bundle import PolicyBundle

    return bind_plan1_learned_policy_runtime(executor, bundle=PolicyBundle.load())


__all__ = [
    "PLAN1_FLOWS",
    "Plan1LearnedPolicyRuntime",
    "Plan1LearnedRuntimeConfigurationError",
    "Plan1LearnedRuntimeResult",
    "Plan1MaaExecutionReceipt",
    "Plan1MaaSingleActionExecutor",
    "SCHEMA",
    "bind_active_plan1_learned_policy_runtime",
    "bind_plan1_learned_policy_runtime",
    "build_plan1_exact_learned_snapshot",
]
