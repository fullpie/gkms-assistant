"""Pure model coverage and explicitly unvalidated cross-mode BC routing.

Native mode/rule/input capability remains a separate caller-owned check. A
transfer never renames produceId, supplies a reward prior, or authorizes RL.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path

from .canonical_training_labels import EFFECT_BY_NATIVE_VALUE, PLAN_BY_NATIVE_VALUE, STAGE_BY_NATIVE_VALUE


SCHEMA = "gkms.explicit-bc-mode-transfer.v1"
_TRANSFER_FEATURE_SCHEMA = "gkms.card-semantic-features.v2"
_STAGE_NAMES = {**STAGE_BY_NATIVE_VALUE, **{f"ProduceStepType_Audition{v}": v for v in STAGE_BY_NATIVE_VALUE.values()}}


def _stage(value: object) -> str:
    result = _STAGE_NAMES.get(value, value)
    if result not in STAGE_BY_NATIVE_VALUE.values():
        raise ValueError(f"unsupported model stage: {value}")
    return str(result)


def _flow(produce_id: str, plan_type: object, main_effect_type: object) -> str:
    plan = PLAN_BY_NATIVE_VALUE.get(plan_type, plan_type)
    effect = EFFECT_BY_NATIVE_VALUE.get(main_effect_type, main_effect_type)
    if not isinstance(produce_id, str) or not produce_id or not isinstance(plan, str) or not isinstance(effect, str):
        raise ValueError("mode/Plan/main-effect identity is incomplete")
    return "|".join((produce_id, plan, effect))


def _trained(spec: Mapping[str, object]) -> Mapping[str, object]:
    return spec.get("inventory", {}).get("exact_exam", {}).get("per_flow", {})


def _stage_counts(flow_inventory: Mapping[str, object]) -> dict[str, Mapping[str, int]]:
    return {_stage(stage): counts for stage, counts in flow_inventory.get("stage_split_counts", {}).items()}


def validate_mode_transfer_declaration(payload: Mapping[str, object], spec: Mapping[str, object]) -> None:
    """Validate the declaration against the bundle's already hash-bound spec."""
    component = payload.get("components", {}).get("exact_exam_policy", {})
    transfer = component.get("mode_transfer")
    if transfer is None:
        return
    if not isinstance(transfer, Mapping) or transfer.get("schema") != SCHEMA:
        raise ValueError("unsupported mode transfer declaration")
    features = spec.get("card_feature_contract", {})
    if features.get("schema") != _TRANSFER_FEATURE_SCHEMA or payload.get("card_data_contract", {}).get("schema") != _TRANSFER_FEATURE_SCHEMA:
        raise ValueError("mode transfer requires the explicit semantic v2 artifact")
    exact = {
        "validation_status": "unvalidated-cross-mode",
        "stage_kind": "audition", "policy_component": "exact_exam_policy",
        "offline_rl_allowed": False, "score_forecast_supported": False,
        "model_sha256": component.get("model_sha256"),
        "source_training_spec_sha256": payload.get("training_spec_sha256"),
        "features_sha256": features.get("features_sha256"),
    }
    if any(transfer.get(key) != value for key, value in exact.items()):
        raise ValueError("mode transfer declaration differs from model/spec/BC-only contract")
    if transfer.get("offline_rl_allowed") is not False or transfer.get("score_forecast_supported") is not False:
        raise ValueError("mode transfer must explicitly disable RL and score forecasts")
    routes = transfer.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("mode transfer has no explicit target routes")
    trained = _trained(spec)
    seen = set()
    for route in routes:
        if not isinstance(route, Mapping):
            raise ValueError("mode transfer route must be an object")
        source, target = route.get("source_flow"), route.get("target_flow")
        if source not in trained or not isinstance(target, str):
            raise ValueError("mode transfer source flow was not trained")
        a, b = source.split("|"), target.split("|")
        if len(a) != 3 or len(b) != 3 or a[1:] != b[1:] or a[0] == b[0] or target in trained:
            raise ValueError("mode transfer must preserve the trained Plan and main effect")
        if target in seen or b[0] not in {f"produce-{i:03d}" for i in range(1, 9)}:
            raise ValueError("mode transfer target is duplicate or unknown")
        seen.add(target)
        stages = route.get("stages")
        counts = _stage_counts(trained[source])
        if not isinstance(stages, list) or not stages or len(stages) != len(set(stages)):
            raise ValueError("mode transfer stage list is incomplete")
        if any(stage not in STAGE_BY_NATIVE_VALUE.values() or counts.get(stage, {}).get("train", 0) <= 0 for stage in stages):
            raise ValueError("mode transfer has an untrained source stage")


@dataclass(frozen=True, slots=True)
class ExamPolicyModeRoute:
    status: str
    flow: str
    source_flow: str | None = None
    stages: tuple[str, ...] = ()
    validated_for_target: bool = False
    blockers: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status in {"trained", "bc-only-transfer"} and not self.blockers

    @property
    def allow_offline_rl(self) -> bool:
        return self.available and self.status == "trained"

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "available": self.available, "allow_offline_rl": self.allow_offline_rl}


def resolve_exam_policy_mode_route(
    bundle: object, *, produce_id: str, plan_type: object, main_effect_type: object,
    stage: str | int | None = None, stage_kind: str = "audition",
    required_stages: Sequence[str] = (),
) -> ExamPolicyModeRoute:
    """Read-only preflight, usable before opening a mode or consuming AP.

    Pass mode_profile.audition_stages before cultivation. With no explicit
    stage list, this function reads that mode's Master profile itself. At a
    native boundary pass its real stage and its real audition/lesson kind.
    """
    flow = "unresolved"
    try:
        flow = _flow(produce_id, plan_type, main_effect_type)
        if stage_kind != "audition":
            raise ValueError("learned mode coverage is audition-only")
        payload = getattr(bundle, "payload", None)
        if not isinstance(payload, Mapping):
            raise ValueError("model bundle has no hash-bound coverage declaration")
        component = bundle.component("exact_exam_policy")
        if not all(component.get(key) is True for key in ("enabled", "shadow_ready", "live_apply_allowed")):
            raise ValueError("exact BC component is not enabled for this bundle")
        path = Path(str(payload["training_spec_path"]))
        if not path.is_absolute():
            path = Path(bundle.project_root) / path
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != payload.get("training_spec_sha256"):
            raise ValueError("model coverage training spec hash mismatch")
        spec = json.loads(data)
        validate_mode_transfer_declaration(payload, spec)
        if stage is not None:
            wanted = (_stage(stage),)
        elif required_stages:
            wanted = tuple(_stage(value) for value in required_stages)
        else:
            from .runtime_mode_profile import load_runtime_mode_profile
            wanted = tuple(_stage(value) for value in load_runtime_mode_profile(produce_id).audition_stages)
        trained = _trained(spec)
        if flow in trained:
            counts = _stage_counts(trained[flow])
            if any(counts.get(value, {}).get("train", 0) <= 0 for value in wanted):
                raise ValueError("model has no training examples for a required stage")
            validated = all(counts[value].get("validation", 0) > 0 and counts[value].get("test", 0) > 0 for value in wanted)
            return ExamPolicyModeRoute("trained", flow, flow, wanted, validated)
        declaration = component.get("mode_transfer", {})
        candidates = [row for row in declaration.get("routes", ()) if row["target_flow"] == flow]
        if len(candidates) != 1 or not set(wanted).issubset(candidates[0]["stages"]):
            raise ValueError("model has no trained flow or explicit BC-only transfer for this mode/Plan/effect/stage")
        return ExamPolicyModeRoute("bc-only-transfer", flow, candidates[0]["source_flow"], wanted, False)
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
        return ExamPolicyModeRoute("unavailable", flow, blockers=(str(error),))


def make_mode_transfer_declaration(
    payload: Mapping[str, object], spec: Mapping[str, object], *, target_produce_ids: Sequence[str],
) -> dict[str, object]:
    """Create a declaration for a NEW bundle; leave model bytes and active alone."""
    from .runtime_mode_profile import load_runtime_mode_profile
    routes = []
    for produce_id in target_produce_ids:
        stages = [_stage(value) for value in load_runtime_mode_profile(produce_id).audition_stages]
        for source in sorted(_trained(spec)):
            _source_mode, plan, effect = source.split("|")
            target = "|".join((produce_id, plan, effect))
            if target in _trained(spec):
                continue
            routes.append({"source_flow": source, "target_flow": target, "stages": stages})
    declaration = {
        "schema": SCHEMA, "validation_status": "unvalidated-cross-mode", "stage_kind": "audition",
        "policy_component": "exact_exam_policy", "offline_rl_allowed": False, "score_forecast_supported": False,
        "model_sha256": payload["components"]["exact_exam_policy"]["model_sha256"],
        "source_training_spec_sha256": payload["training_spec_sha256"],
        "features_sha256": spec["card_feature_contract"]["features_sha256"], "routes": routes,
    }
    return declaration


__all__ = ["ExamPolicyModeRoute", "resolve_exam_policy_mode_route", "validate_mode_transfer_declaration", "make_mode_transfer_declaration"]
