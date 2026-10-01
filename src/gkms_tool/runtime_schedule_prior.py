"""Weak behavior-count priors over an already-owned native schedule set.

This bridge does not read game state, choose a target, change legality, or
load the separate Outer BC/RL model artifacts. The caller supplies the exact
candidate IDs it will later score and dispatch through the existing owner.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .nia_route_profile import nia_final_week, nia_phase_for_week
from .nia_training_dataset import (
    DECISION_OUTER_ACTION, NiaHierarchicalCandidateBehaviorPrior,
    load_nia_exact_candidate_behavior_prior,
)


SCHEMA = "gkms.runtime-schedule-behavior-prior.v1"
COARSE_SCHEDULE_LABELS = frozenset({"vocal_lesson", "dance_lesson", "visual_lesson", "rest",
    "business", "activity", "outing", "consultation", "special_guidance"})
_BY_STEP = {19: "vocal_lesson", 20: "vocal_lesson", 21: "dance_lesson", 22: "dance_lesson",
    23: "visual_lesson", 24: "visual_lesson", 14: "rest", 25: "business", 27: "activity",
    11: "outing", 13: "consultation", 28: "special_guidance"}
_SCOPE = ("produce_id", "idol_card_id", "character_id", "plan_type", "exam_effect_type")
_STAGES = {1: "Mid1", 2: "Mid2", 3: "Final"}


def coarse_schedule_label(target: Mapping[str, object]) -> str | None:
    """Map a normal native schedule target; no event/PT choices are aliases."""
    if not isinstance(target, Mapping):
        return None
    action = target.get("action_id")
    if action == "ui.navigation" and target.get("button_id") == "schedule.refresh":
        return "rest"
    if action != "schedule.choose" or type(target.get("step_type")) is not int:
        return None
    return _BY_STEP.get(target["step_type"])


@dataclass(frozen=True, slots=True)
class RuntimeSchedulePriorResult:
    status: str
    scores: Mapping[str, float]
    source: str | None
    reason: str
    features: Mapping[str, object]
    candidate_labels: Mapping[str, str]
    label_votes: Mapping[str, int]
    ranked_labels: tuple[str, ...]
    provenance: tuple[str, ...]
    coverage: Mapping[str, object]
    schema: str = SCHEMA

    def __post_init__(self):
        for name in ("scores", "features", "candidate_labels", "label_votes", "coverage"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def available(self) -> bool:
        return self.status == "scored"

    def to_dict(self):
        return {"schema": self.schema, "status": self.status, "scores": dict(self.scores),
                "source": self.source, "reason": self.reason, "features": dict(self.features),
                "candidate_labels": dict(self.candidate_labels), "label_votes": dict(self.label_votes),
                "ranked_labels": list(self.ranked_labels), "provenance": list(self.provenance),
                "coverage": dict(self.coverage), "normalization": "votes/max_vote; bounded 0..1",
                "owns_input": False, "predicts_score": False}


def score_runtime_schedule_prior(context: Mapping[str, object], candidate_labels: Mapping[str, str | None], *,
                                  schedule_number: int, candidate_set_complete: bool,
                                  prior: NiaHierarchicalCandidateBehaviorPrior | None = None) -> RuntimeSchedulePriorResult:
    """Return optional native-ID scores; any missing coverage merely abstains.

    ``schedule_number`` is the actual pending schedule row, not the progress
    week that may describe the preceding completed action. SP/Normal labels
    collapse only for querying the coarse prior; both original IDs receive
    the same score and remain distinct legal alternatives.
    """
    labels = dict(candidate_labels) if isinstance(candidate_labels, Mapping) else {}
    features: dict[str, object] = {}
    coverage: dict[str, object] = {"decision_kind": "schedule", "candidate_set_complete": candidate_set_complete is True,
        "native_candidate_count": len(labels), "candidate_signature_matched": False,
        "scope": "NIA coarse weekly schedule only", "sp_normal_separated": False}

    def abstain(reason, source=None):
        retained = {key: value for key, value in labels.items() if isinstance(key, str) and isinstance(value, str)}
        return RuntimeSchedulePriorResult("abstain", {}, source, reason, features, retained, {}, (), (), coverage)

    if not isinstance(context, Mapping) or context.get("surface") != "schedule":
        return abstain("not-a-native-schedule-decision")
    if context.get("decision_kind") not in (None, "schedule", "outer_action"):
        return abstain("event-and-pt-options-have-no-schedule-label-contract")
    if context.get("produce_id") not in {"produce-004", "produce-005"}:
        return abstain("mode-outside-NIA-schedule-prior")
    if candidate_set_complete is not True:
        return abstain("native-candidate-set-incomplete")
    if not labels or any(not isinstance(key, str) or not key for key in labels):
        return abstain("native-candidate-ids-unavailable")
    if any(not isinstance(value, str) or value not in COARSE_SCHEDULE_LABELS for value in labels.values()):
        return abstain("unmapped-native-candidate; no-subset-scoring")
    for name in _SCOPE:
        value = context.get(name)
        if not isinstance(value, str) or not value:
            return abstain("native-scope-incomplete:" + name)
        features[name] = value
    if type(schedule_number) is not int or not 1 <= schedule_number <= nia_final_week(features["produce_id"]):
        return abstain("native-pending-schedule-number-invalid")
    phase = nia_phase_for_week(features["produce_id"], schedule_number)
    features.update(week=schedule_number, phase=phase, stage=_STAGES[phase])
    unique_labels = tuple(dict.fromkeys(labels.values()))
    signature = tuple(sorted(unique_labels))
    coverage["coarse_candidate_count"] = len(signature)
    try:
        loaded = load_nia_exact_candidate_behavior_prior() if prior is None else prior
        if not isinstance(loaded, NiaHierarchicalCandidateBehaviorPrior):
            return abstain("hierarchical-prior-contract-unavailable")
        ranking, source = loaded.rank_with_source(features, unique_labels)
        if not ranking or source not in {"exact", "broad"}:
            return abstain("no-matching-behavior-prior")
        if len(ranking) != len(signature) or set(ranking) != set(signature):
            return abstain("prior-ranking-does-not-preserve-coarse-candidate-set", source)
        if source == "exact":
            selected = loaded.exact_prior
            scope = tuple(features[name] for name in _SCOPE)
        else:
            selected = loaded.broad_prior
            scope = tuple(features[name] for name in ("produce_id", "plan_type", "exam_effect_type"))
        if selected is None:
            return abstain("selected-prior-scope-unavailable", source)
        prefix = (DECISION_OUTER_ACTION, *scope, schedule_number, phase, signature)
        votes, provenance = {}, set()
        for label in unique_labels:
            key = (*prefix, label)
            count = selected.counts.get(key, 0)
            if source == "broad":
                units = selected.source_action_counts.get(key)
                if units is not None:
                    count = len(units)
            if type(count) is not int or count < 0:
                return abstain("prior-vote-contract-invalid", source)
            votes[label] = count
            if count:
                provenance.update(selected.source_kinds_by_key.get(key, ()))
        maximum = max(votes.values(), default=0)
        if maximum == 0:
            # Existing broad prior permits subset signatures. This bridge
            # deliberately requires the whole observed coarse set instead.
            return abstain("candidate-signature-not-observed; no-subset-scoring", source)
        coverage.update(candidate_signature_matched=True,
            scope_source_support=selected.decision_scope_source_counts.get((DECISION_OUTER_ACTION, *scope), 0),
            matched_votes=sum(votes.values()), prior_schema=loaded.schema)
        scores = {identity: votes[label] / maximum for identity, label in labels.items()}
        return RuntimeSchedulePriorResult("scored", scores, source, "matched-complete-coarse-schedule-signature",
            features, labels, votes, tuple(ranking), tuple(sorted(provenance)), coverage)
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as error:
        return abstain(f"prior-unavailable:{type(error).__name__}:{error}")
