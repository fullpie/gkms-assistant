"""One optional pure drink decision around an existing Plan2 actor.

The native executor still sends exactly one action, then observes again. No
simulated continuation is queued and no counterfactual becomes a native label.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import hashlib

from .exam_drink_decision import DrinkDecisionContext, recommend_plan2_drink_before_play
from .plan2_exact_bc_canary_bridge import _physical_identity
from .plan2_native_exam_save_orchestrator import with_plan2_policy_action
from .runtime_native_legal_inputs import RuntimeNativeLegalInputs
from .training_artifact_io import canonical_json_bytes


def strict_counterfactual_coverage(evidence, catalog):
    from .plan2_native_local_save_bootstrap import (
        bootstrap_plan2_native_horizon_from_evidence, load_plan2_native_exam_setting_authority,
    )
    from .initial_regular_plan2_gimmick_runtime import build_plan2_audition_gimmick_hooks

    authority = load_plan2_native_exam_setting_authority(evidence.state.setting_id)
    opaque = evidence.state.root_runtime.opaque_fields.to_value()
    audit = bootstrap_plan2_native_horizon_from_evidence(evidence, catalog=catalog,
        draw_count=authority.draw_count, hand_limit=authority.hand_limit,
        gimmick_hooks=build_plan2_audition_gimmick_hooks(opaque.get("gimmickList")),
        observe_external_effects=False)
    return audit


class RuntimePlan2DrinkPolicy:
    def __init__(self, baseline_policy, native_provider, *, coverage_auditor=strict_counterfactual_coverage):
        # Native observation callers pass None and use evaluate() with the
        # already selected actor action. A baseline is only for the explicit
        # legacy evidence-only __call__ contract below.
        self.baseline_policy, self.native_provider = baseline_policy, native_provider
        self.coverage_auditor = coverage_auditor
        self.last_drink_decision = None

    def __getattr__(self, name):
        return getattr(self.baseline_policy, name)

    def _evaluate(self, evidence, baseline_action, *, native_pool=None, legal_action_ids=None):
        """Return an optional hint and its local simulation boundary.

        Only the legacy caller uses that private boundary to retain its old
        predicted S'. The native observation route consumes the report only.
        """
        def skip(reason):
            # Preserve legacy no-report behavior at its former early exits.
            if native_pool is None:
                return None, None
            return {"candidate": None, "boundary_digest": evidence.digest(), "blockers": [reason]}, None

        try:
            if baseline_action is None or baseline_action.kind != "play":
                return skip("drink-comparison-requires-baseline-play")
            identity = _physical_identity(evidence)
            if identity is None:
                return skip("drink-comparison-native-flow-unavailable")
            flow, stage = identity
            produce_id, plan, effect = flow.split("|")
            if produce_id != "produce-004" or plan != "ProducePlanType_Plan2":
                return skip("drink-comparison-mode-plan-outside-scope")
            if native_pool is not None:
                if not isinstance(native_pool, RuntimeNativeLegalInputs) or not native_pool.boundary_ready:
                    raise ValueError("drink comparison requires the current native primary pool")
                if (native_pool.exam_save_sha256 != evidence.source_sha256
                        or evidence.session_transition_id != "native-sequence:" + native_pool.sequence_key):
                    raise ValueError("drink comparison native pool/evidence source mismatch")
                state = evidence.state
                context_digest = hashlib.sha256(canonical_json_bytes({
                    "session_generation": native_pool.session_generation, "sequence_id": native_pool.sequence_key,
                    "setting_id": state.setting_id, "character_id": state.character_id, "step_type": state.step_type_value,
                })).hexdigest()
                if evidence.step_context_digest != context_digest:
                    raise ValueError("drink comparison native session context differs")
                if native_pool.lookup("play", card_guid=baseline_action.card_guid) is None:
                    raise ValueError("baseline PLAY is not proven by this native pool")
            boundary = self.native_provider.plan2_native_boundary_provider(evidence)
            if boundary.root is None or boundary.blockers or not any(a.kind == "drink" for a in boundary.actions):
                return skip("drink-comparison-local-simulator-boundary-unavailable")
            native_ids = tuple(a.action_id for a in boundary.actions)
            omitted = []
            if native_pool is None:
                if legal_action_ids is not None and set(native_ids) != set(legal_action_ids):
                    return {"candidate": None, "blockers": ["drink-actor-current-legal-set-mismatch"]}, None
                compared = boundary.actions
                baseline_id = baseline_action.action_id
            else:
                # A native primary ID intentionally differs from a retained
                # simulator's localsave-instance ID. Match physical identity.
                if tuple(card.guid for card in boundary.root.zones.hand) != tuple(row["card_guid"] for row in native_pool.hand):
                    raise ValueError("comparison hand differs from native ordered GUIDs")
                if tuple(drink.drink_id for drink in boundary.root.drink_runtime.inventory) != tuple(row["drink_id"] for row in native_pool.drinks):
                    raise ValueError("comparison dropped/reordered an observed drink; reserve count is unknown")
                plays = [a for a in boundary.actions if a.kind == "play" and a.card_guid == baseline_action.card_guid]
                if len(plays) != 1:
                    return skip("baseline-play-has-no-unique-local-transition")
                compared = [plays[0]]
                baseline_id = plays[0].action_id
                for native in native_pool.actions:
                    if native.kind != "drink":
                        continue
                    target = native.target
                    matches = [a for a in boundary.actions if a.kind == "drink"
                        and a.slot_index == target["slot"] and a.drink_id == target["drink_id"]
                        and not a.selected_card_guid]
                    if len(matches) != 1:
                        omitted.append({"slot": target["slot"], "drink_id": target["drink_id"],
                            "reason": "no-unique-supported-primary-transition; secondary-not-preselected"})
                        continue
                    compared.append(matches[0])
                if len(compared) == 1:
                    return skip("no-native-legal-drink-has-a-local-primary-transition")
            audit = self.coverage_auditor(evidence, boundary.catalog)
            report = recommend_plan2_drink_before_play(boundary.root, boundary.catalog,
                legal_candidates=[{**asdict(candidate), "kind": candidate.kind, "action_id": candidate.action_id}
                                  for candidate in compared], baseline_action_id=baseline_id,
                context=DrinkDecisionContext(produce_id, plan, effect, stage, evidence.digest(),
                    coverage_blockers=tuple(f"{issue.code}:{issue.detail}" for issue in audit.blockers),
                    projection_complete=audit.simulation_ready),
                transitioner=self.native_provider.plan2_native_transitioner)
            if native_pool is not None:
                report.update(comparison_mode="native-primary-physical-intersection",
                    actor_baseline_action_id=baseline_action.action_id, native_revision=native_pool.revision,
                    native_sequence_id=native_pool.sequence_key, native_session_generation=native_pool.session_generation,
                    native_primary_pool_complete=native_pool.complete,
                    omitted_primary_drinks=omitted, compared_drink_count=len(compared)-1,
                    original_drink_count=len(native_pool.drinks), native_candidate=None)
                candidate = report.get("candidate")
                if candidate is not None:
                    if candidate.get("selected_card_guid"):
                        raise ValueError("primary drink hint cannot preselect an unobserved secondary GUID")
                    chosen = native_pool.lookup("drink", slot=candidate.get("slot_index"), drink_id=candidate.get("drink_id"))
                    if chosen is None:
                        raise ValueError("simulated drink does not bind this native primary pool")
                    report["native_candidate"] = chosen.to_dict()
            return report, boundary
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            return {"candidate": None, "boundary_digest": evidence.digest(),
                "blockers": [f"drink-comparison-unavailable:{type(error).__name__}:{error}"]}, None

    def evaluate(self, evidence, baseline_action, *, native_pool=None, legal_action_ids=None):
        """Optional pure drink hint; never invokes the baseline actor or input."""
        report, _ = self._evaluate(evidence, baseline_action, native_pool=native_pool, legal_action_ids=legal_action_ids)
        self.last_drink_decision = report
        return report

    choose_drink = evaluate

    def __call__(self, evidence):
        """Keep the legacy evidence-only actor/retained prediction contract."""
        if self.baseline_policy is None:
            raise TypeError("native drink refinement requires evaluate() with an existing actor action")
        self.last_drink_decision = None
        decision = self.baseline_policy(evidence)
        action = decision.best_action
        if action is None or action.kind != "play":
            return decision
        try:
            report, boundary = self._evaluate(evidence, action, legal_action_ids=decision.policy_legal_action_ids)
            self.last_drink_decision = report
            candidate = report.get("candidate") if isinstance(report, Mapping) else None
            if candidate is None or boundary is None:
                return decision
            matches = [a for a in boundary.actions if a.kind == "drink" and a.action_id == candidate.get("action_id")]
            if len(matches) != 1:
                raise ValueError("drink suggestion does not bind a unique current typed action")
            drink = matches[0]
            transition = self.native_provider.plan2_native_transitioner(boundary.root, drink, boundary.catalog)
            if not transition.supported or transition.after is None:
                raise ValueError("selected drink transition is no longer supported")
            return with_plan2_policy_action(decision, source="native-drink-before-play-v1",
                action=drink, predicted_after_state=transition.after, probability=None,
                legal_action_ids=tuple(a.action_id for a in boundary.actions))
        except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
            # A counterfactual hint cannot disable an already accepted actor
            # choice. Preserve an explicit diagnostic, never invent a drink.
            self.last_drink_decision = {"candidate": None,
                "blockers": [f"drink-comparison-unavailable:{type(error).__name__}:{error}"]}
            return decision
