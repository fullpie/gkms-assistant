"""Compare proven terminal outcomes without changing the learned proposal.

This pure refinement only replaces a terminal PLAY with another native legal
PLAY whose complete local terminal score is higher. Nonterminal branches are
not assigned an invented value. The normal executor still owns the sole input.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .plan1_drink_comparison import Plan1DrinkComparisonAdapter, Plan1DrinkComparisonState
from .plan1_runtime_simulator_bridge import _drink_inventory


POLICY_ID = "native-plan1-terminal-score-v1"
_STAGES = {"Mid1": 16, "Mid2": 17, "Final": 18}


def compare_terminal_plays(projection, *, candidates, baseline_action_id, database=DEFAULT_DATABASE):
    """Return a score improvement only when both compared outcomes end play."""
    report = {"policy_id": POLICY_ID, "candidate": None, "blockers": [], "branches": [],
              "comparison_baseline_action_id": baseline_action_id,
              "proof_scope": "local-terminal-counterfactual; not-native-or-training-label"}
    if projection.state is None or projection.parsed is None or projection.parsed.remain_turn != 1:
        report["blockers"] = ["terminal-comparison-requires-last-observed-turn"]
        return report
    plays = [dict(value) for value in candidates if value.get("kind") in {"play", "use-hand"}]
    baseline = [value for value in plays if value.get("action_id") == baseline_action_id]
    if len(baseline) != 1 or len({value.get("action_id") for value in plays}) != len(plays):
        report["blockers"] = ["terminal-comparison-baseline-or-action-identity-ambiguous"]
        return report
    adapter = Plan1DrinkComparisonAdapter(projection, database=database)
    if not adapter.audit["complete"]:
        report["blockers"] = list(adapter.audit["blockers"])
        return report
    root = Plan1DrinkComparisonState(projection, _drink_inventory(projection.parsed), 1)

    def evaluate(candidate):
        transition = adapter.transition(root, candidate)
        after = transition.after
        terminal = after is not None and after.terminal and not transition.blockers
        row = {"action_id": candidate["action_id"], "terminal": terminal,
               "blockers": list(transition.blockers),
               "score": after.projection.state.scalar.score if terminal else None}
        report["branches"].append(row)
        return row

    original = evaluate(baseline[0])
    if not original["terminal"]:
        report["blockers"] = ["actor-outcome-not-proven-terminal", *original["blockers"]]
        return report
    best, best_score = baseline[0], original["score"]
    for candidate in plays:
        if candidate["action_id"] == baseline_action_id:
            continue
        row = evaluate(candidate)
        if row["terminal"] and row["score"] > best_score:
            best, best_score = candidate, row["score"]
    report.update(baseline_terminal_score=original["score"], selected_terminal_score=best_score,
                  terminal_score_gain=best_score - original["score"])
    if best["action_id"] != baseline_action_id:
        report["candidate"] = best
    return report


class RuntimePlan1TerminalPolicy:
    def __init__(self, projection, *, database=DEFAULT_DATABASE):
        self.projection, self.database = projection, Path(database)

    def __call__(self, *, decision, snapshot, evidence, state_before, native_state, stage):
        report = {"policy_id": POLICY_ID, "candidate": None, "blockers": [],
                  "actor_action_id": decision.action_id, "boundary_digest": snapshot.boundary_digest}
        if (not decision.ready or not isinstance(decision.candidate, Mapping)
                or decision.candidate.get("kind") not in {"play", "use-hand"}):
            report["blockers"] = ["terminal-rule-requires-existing-actor-play"]
            return report
        if (snapshot.flow[0] != "produce-004" or snapshot.flow[1] != "ProducePlanType_Plan1"
                or self.projection.parsed is None or self.projection.parsed.exam_type != 1
                or self.projection.parsed.step_type_value != _STAGES.get(stage)):
            report["blockers"] = ["terminal-rule-mode-or-stage-outside-scope"]
            return report
        if (decision.boundary_digest != snapshot.boundary_digest or evidence.digest() != snapshot.boundary_digest
                or not snapshot.complete or not snapshot.contains(decision.candidate)
                or native_state != self.projection.state):
            report["blockers"] = ["terminal-rule-does-not-bind-actor-native-boundary"]
            return report
        return {**compare_terminal_plays(self.projection, candidates=snapshot.candidates,
                    baseline_action_id=decision.action_id, database=self.database),
                "actor_action_id": decision.action_id, "boundary_digest": snapshot.boundary_digest}
