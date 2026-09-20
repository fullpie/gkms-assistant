"""Optional weekly candidate BC inference; never selects or sends game input.

The caller explicitly supplies a provider descriptor. No active bundle, default
model or historical corpus is loaded while scoring. Coverage is train-only and
joint, rather than a cross product of the training report's marginal counts.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType

from .nia_route_profile import nia_final_week, nia_phase_for_week
from .outer_behavior_candidate import SCHEMA as MODEL_SCHEMA, candidate_semantics, feature_tokens, predict_example

SCHEMA = "gkms.runtime-outer-behavior-prior.v1"
PROVIDER_SCHEMA = "gkms.outer-behavior-inference-provider.v1"
SCOPE_FIELDS = ("produce_id", "plan_type", "exam_effect_type", "idol_card_id")
# Selected by the live runner; the pure inference API still requires a caller
# to supply a provider and never activates or retrains a model on its own.
from .application_paths import weekly_behavior_provider
DEFAULT_WEEKLY_BC_PROVIDER = weekly_behavior_provider()


@dataclass(frozen=True, slots=True)
class RuntimeOuterBehaviorPriorResult:
    status: str
    reason: str
    scores: Mapping[str, float]
    probabilities: Mapping[str, float]
    candidate_semantics: Mapping[str, object]
    features: Mapping[str, object]
    coverage: Mapping[str, object]
    source: str | None = None
    schema: str = SCHEMA

    def __post_init__(self):
        for name in ("scores", "probabilities", "candidate_semantics", "features", "coverage"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    @property
    def available(self) -> bool:
        return self.status == "scored"

    def to_dict(self):
        return {"schema": self.schema, "status": self.status, "reason": self.reason,
            **{name: dict(getattr(self, name)) for name in
               ("scores", "probabilities", "candidate_semantics", "features", "coverage")},
            "source": self.source, "normalization": "conditional_probability/max_probability; bounded 0..1",
            "owns_input": False, "predicts_game_score": False, "changes_active_bundle": False}


def _load_provider(path: Path):
    descriptor = json.loads(path.read_bytes())
    if descriptor["schema"] != PROVIDER_SCHEMA or descriptor["coverage_split"] != "train":
        raise ValueError("provider-schema-or-coverage-invalid")
    directory = path.parent / descriptor["candidate_directory"]
    artifacts = {}
    for name in ("model.json", "manifest.json", "report.json"):
        data = (directory / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != descriptor["files"][name]:
            raise ValueError("provider-artifact-hash-mismatch")
        artifacts[name] = json.loads(data)
    model, manifest, report = (artifacts[name] for name in ("model.json", "manifest.json", "report.json"))
    if any(value["schema"] != MODEL_SCHEMA for value in artifacts.values()):
        raise ValueError("model-schema-invalid")
    if (model["model_type"] != "candidate-conditioned-linear-softmax"
            or manifest["component_role"] != "outer_schedule_bc"
            or manifest["input_dataset_digest"] != descriptor["input_dataset_digest"]
            or any(manifest["files"][name] != descriptor["files"][name] for name in ("model.json", "report.json"))):
        raise ValueError("provider-model-contract-invalid")
    tokens, weights = model["vocabulary"], model["weights"]
    if (not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens)
            or len(set(tokens)) != len(tokens) or len(weights) != len(tokens) + 1
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in weights)
            or weights[0] != 0):
        raise ValueError("model-weights-or-vocabulary-invalid")
    train = report["coverage"]["train"]
    if (train["decisions"] <= 0 or descriptor["train_decisions"] != train["decisions"]
            or any(train["before_" + field + "_missing"] != train["decisions"] for field in ("hp", "pt"))):
        raise ValueError("provider-needs-all-missing-before-state-v1")
    for frozen in descriptor["candidate_semantics"]:
        if frozen != candidate_semantics(frozen["native_step_type"]):
            raise ValueError("trained-candidate-mapping-changed")
    supported_steps = {row["native_step_type"] for row in descriptor["candidate_semantics"]}
    for row in descriptor["joint_scopes"]:
        if (set(row["context"]) != set(SCOPE_FIELDS)
                or any(not isinstance(value, str) or not value for value in row["context"].values())
                or type(row["decisions"]) is not int or row["decisions"] <= 0
                or type(row["trajectories"]) is not int or row["trajectories"] <= 0
                or not isinstance(row["week_counts"], dict) or not row["week_counts"]
                or any(not week.isdigit() or type(count) is not int or count <= 0
                       for week, count in row["week_counts"].items())
                or sum(row["week_counts"].values()) != row["decisions"]):
            raise ValueError("provider-joint-coverage-invalid")
    for row in descriptor["candidate_sets"]:
        steps = row["native_step_types"]
        if (not steps or any(type(step) is not int or step not in supported_steps for step in steps)
                or sorted(set(steps)) != steps or type(row["decisions"]) is not int or row["decisions"] <= 0):
            raise ValueError("provider-candidate-coverage-invalid")
    if (sum(row["decisions"] for row in descriptor["joint_scopes"]) != train["decisions"]
            or sum(row["decisions"] for row in descriptor["candidate_sets"]) != train["decisions"]):
        raise ValueError("provider-train-coverage-count-invalid")
    return descriptor, model


def score_runtime_outer_behavior_prior(context: Mapping[str, object], candidate_targets: Mapping[str, Mapping], *,
                                       schedule_number: int, candidate_set_complete: bool,
                                       provider_path: Path | str | None = None) -> RuntimeOuterBehaviorPriorResult:
    """Score exact native IDs as a weak prior, or abstain without partial scores.

    ``schedule_number`` is the pending native schedule number, not context.week
    (which can still describe the previous completed step). HP/PT stay masked
    because this model's teachers contain no observed before-state values; the
    caller's stateful planner retains responsibility for actual resources.
    """
    semantics, features = {}, {}
    coverage = {"coverage_split": "train", "joint_scope_matched": False,
        "candidate_signature_matched": False, "candidate_set_complete": candidate_set_complete is True,
        "sp_normal_separated": True, "before_hp_missing": True, "before_pt_missing": True}
    source = str(provider_path) if provider_path is not None else None

    def abstain(reason):
        return RuntimeOuterBehaviorPriorResult("abstain", reason, {}, {}, semantics, features, coverage, source)

    if not isinstance(context, Mapping) or context.get("surface") != "schedule":
        return abstain("not-a-native-schedule-decision")
    if context.get("decision_kind") not in (None, "schedule", "outer_action"):
        return abstain("event-and-pt-options-have-no-weekly-bc-labels")
    if candidate_set_complete is not True:
        return abstain("native-candidate-set-incomplete")
    if not isinstance(candidate_targets, Mapping) or not candidate_targets:
        return abstain("native-candidates-unavailable")
    for key, target in candidate_targets.items():
        if not isinstance(key, str) or not key or not isinstance(target, Mapping):
            return abstain("native-candidate-contract-invalid")
        if target.get("action_id") == "ui.navigation" and target.get("button_id") == "schedule.refresh":
            step = 14
        elif target.get("action_id") == "schedule.choose" and type(target.get("step_type")) is int:
            step = target["step_type"]
        else:
            return abstain("unmapped-native-candidate; no-subset-scoring")
        try:
            semantics[key] = candidate_semantics(step)
        except ValueError:
            return abstain("unmapped-native-candidate; no-subset-scoring")
    signature = sorted(row["native_step_type"] for row in semantics.values())
    if len(signature) != len(set(signature)):
        return abstain("duplicate-native-step-has-no-trained-identity-contract")
    for field in SCOPE_FIELDS:
        if not isinstance(context.get(field), str) or not context[field]:
            return abstain("native-scope-incomplete:" + field)
        features[field] = context[field]
    if features["produce_id"] not in {"produce-004", "produce-005"}:
        return abstain("mode-outside-trained-NIA-scope")
    if type(schedule_number) is not int or not 1 <= schedule_number <= nia_final_week(features["produce_id"]):
        return abstain("native-pending-schedule-number-invalid")
    features.update(week=schedule_number, phase=nia_phase_for_week(features["produce_id"], schedule_number),
        before_state={"hp": 0, "hp_missing": True, "pt": 0, "pt_missing": True},
        before_state_source="training-schema-missing; zeros-are-padding-not-game-values",
        runtime_hp_pt_used=False)
    if provider_path is None:
        return abstain("optional-provider-not-selected")
    try:
        descriptor, model = _load_provider(Path(provider_path))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        return abstain("provider-unavailable-or-invalid:" + type(error).__name__)
    scope = {field: features[field] for field in SCOPE_FIELDS}
    matched = next((row for row in descriptor["joint_scopes"] if row["context"] == scope), None)
    if matched is None:
        return abstain("joint-mode-plan-effect-idol-scope-unseen-in-train")
    coverage.update(joint_scope_matched=True, scope_train_decisions=matched["decisions"],
        scope_train_trajectories=matched["trajectories"], model_sha256=descriptor["files"]["model.json"],
        train_decisions=descriptor["train_decisions"])
    if str(schedule_number) not in matched["week_counts"]:
        return abstain("pending-week-unseen-for-joint-scope-in-train")
    coverage["scope_week_train_decisions"] = matched["week_counts"][str(schedule_number)]
    set_match = next((row for row in descriptor["candidate_sets"] if row["native_step_types"] == signature), None)
    if set_match is None:
        return abstain("exact-candidate-set-unseen-in-train; no-subset-scoring")
    coverage.update(candidate_signature_matched=True, candidate_signature=signature,
        candidate_set_train_decisions=set_match["decisions"], candidate_set_scope="global-train-exact-set")
    example = {"context": scope, "week": schedule_number, "phase": features["phase"],
        "total_weeks": nia_final_week(features["produce_id"]), "before_state": features["before_state"],
        "candidates": sorted(semantics.values(), key=lambda row: row["native_step_type"]),
        "candidate_set_complete": True}
    tokens = [token for row in example["candidates"] for token in feature_tokens(example, row)]
    vocabulary = set(model["vocabulary"])
    coverage["unknown_feature_fraction"] = sum(token not in vocabulary for token in tokens) / len(tokens)
    try:
        predicted = predict_example(model, example)
    except (ImportError, ValueError, TypeError, IndexError, FloatingPointError):
        return abstain("optional-model-inference-unavailable")
    probabilities = {key: predicted[row["step_type"]] for key, row in semantics.items()}
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in probabilities.values()):
        return abstain("model-returned-invalid-probabilities")
    maximum = max(probabilities.values())
    if maximum <= 0 or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=1e-9):
        return abstain("model-returned-invalid-probability-mass")
    return RuntimeOuterBehaviorPriorResult("scored", "covered-weekly-choice-weak-prior",
        {key: value / maximum for key, value in probabilities.items()}, probabilities,
        semantics, features, coverage, source)
