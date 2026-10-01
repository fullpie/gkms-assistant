"""Pure post-actor Plan1 drink refinement; the existing executor retains input."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .exam_drink_decision import DrinkDecisionContext
from .master_db import DEFAULT_DATABASE
from .plan1_drink_comparison import audit_plan1_drink_comparison_input, recommend_plan1_drink_before_play


POLICY_ID = "native-plan1-drink-before-play-v1"


class RuntimePlan1DrinkPolicy:
    def __init__(self, projection, *, database=DEFAULT_DATABASE):
        self.projection = projection
        self.database = Path(database)

    def __call__(self, *, decision, snapshot, evidence, state_before, native_state, stage, baseline_candidate=None):
        report = {"policy_id": POLICY_ID, "candidate": None, "blockers": [],
                  "actor_action_id": decision.action_id, "comparison_baseline_action_id": decision.action_id,
                  "boundary_digest": snapshot.boundary_digest}
        actor = decision.candidate
        if not decision.ready or not isinstance(actor, Mapping) or actor.get("kind") not in {"play", "use-hand", "card"}:
            report["blockers"] = ["drink-rule-requires-existing-actor-play"]
            return report
        baseline_id = decision.action_id
        if baseline_candidate is not None:
            if (not isinstance(baseline_candidate, Mapping) or baseline_candidate.get("kind") not in {"play", "use-hand", "card"}
                    or not isinstance(baseline_candidate.get("action_id"), str) or not snapshot.contains(baseline_candidate)):
                report["blockers"] = ["drink-comparison-baseline-not-current-native-play"]
                return report
            baseline_id = baseline_candidate["action_id"]
            report["comparison_baseline_action_id"] = baseline_id
        produce_id, plan, effect = snapshot.flow
        if produce_id != "produce-004" or plan != "ProducePlanType_Plan1":
            report["blockers"] = ["drink-rule-mode-plan-outside-scope"]
            return report
        if (decision.boundary_digest != snapshot.boundary_digest or evidence.digest() != snapshot.boundary_digest
                or not snapshot.complete or not snapshot.contains(actor) or native_state != self.projection.state):
            report["blockers"] = ["drink-rule-does-not-bind-actor-native-boundary"]
            return report
        if not any(value.get("kind") in {"drink", "use-drink"} for value in snapshot.candidates):
            report["blockers"] = ["no-native-legal-drink"]
            return report
        # This is local input/rule completeness, not the bridge's intentionally
        # false native-promotion flag or a legacy recorder training-stage flag.
        audit = audit_plan1_drink_comparison_input(self.projection, database=self.database)
        result = recommend_plan1_drink_before_play(self.projection, legal_candidates=snapshot.candidates,
            baseline_action_id=baseline_id,
            context=DrinkDecisionContext(produce_id, plan, effect, stage, snapshot.boundary_digest,
                projection_complete=audit["complete"], coverage_blockers=tuple(audit["blockers"])),
            database=self.database)
        return {**result, "policy_id": POLICY_ID, "actor_action_id": decision.action_id,
                "comparison_baseline_action_id": baseline_id}
