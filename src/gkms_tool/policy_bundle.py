"""Versioned, swappable policy bundles for GUI and shadow inference."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .behavior_cloning import (
    EXACT_REPORT_SCHEMA,
    OUTER_REPORT_SCHEMA,
    REPLAY_REPORT_SCHEMA,
    load_model_artifact,
    predict_replay_syntax,
    score_exact_main_action_candidates,
    score_pointer_candidates,
)
from .training_artifact_io import atomic_write, sha256_file
from .training_spec import SPEC_V2_SCHEMA, SPEC_SCOPED_SCHEMA, declared_training_scope
from .card_semantic_features import load_card_semantic_features


BUNDLE_SCHEMA: Final = "gkms.policy-bundle.v1"
ACTIVE_BUNDLE_SCHEMA: Final = "gkms.active-policy-bundle.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_SPEC: Final = (
    PROJECT_ROOT / "var" / "training_specs" / "exact_v2" / "training_spec.json"
)
DEFAULT_OUTER_MODEL: Final = (
    PROJECT_ROOT / "var" / "models" / "behavior_cloning_v1" / "outer_bc_model.npz"
)
DEFAULT_OUTER_REPORT: Final = (
    PROJECT_ROOT / "var" / "models" / "behavior_cloning_v1" / "outer_bc_report.json"
)
DEFAULT_EXACT_MODEL: Final = (
    PROJECT_ROOT
    / "var"
    / "models"
    / "behavior_cloning_v0"
    / "exact_main_action_bc_model.npz"
)
DEFAULT_EXACT_REPORT: Final = (
    PROJECT_ROOT
    / "var"
    / "models"
    / "behavior_cloning_v0"
    / "exact_main_action_bc_report.json"
)
DEFAULT_REPLAY_MODEL: Final = (
    PROJECT_ROOT / "var" / "models" / "behavior_cloning_v1" / "replay_bc_model.npz"
)
DEFAULT_REPLAY_REPORT: Final = (
    PROJECT_ROOT / "var" / "models" / "behavior_cloning_v1" / "replay_bc_report.json"
)
DEFAULT_OUTPUT: Final = (
    PROJECT_ROOT
    / "var"
    / "models"
    / "policy_bundles"
    / "behavior_cloning_v0"
    / "manifest.json"
)
DEFAULT_BUNDLE_ROOT: Final = PROJECT_ROOT / "var" / "models" / "policy_bundles"
DEFAULT_ACTIVE_BUNDLE: Final = DEFAULT_BUNDLE_ROOT / "active.json"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _validate_shared_exam_binding(spec, metadata, report):
    expected = spec.get('shared_primary_feature_contract')
    declared = metadata.get('shared_primary_feature_contract')
    reported = _mapping(report.get('model')).get('shared_primary_feature_contract')
    if any(value is not None for value in (expected, declared, reported)):
        if expected is None or declared != expected or reported != expected:
            raise ValueError('Shared primary model/report/training-spec feature contracts differ')
        if metadata.get('untrained_fixture') is not True:
            evidence = _mapping(metadata.get('shared_primary_training_evidence'))
            refs = _mapping(spec.get('shared_primary_evidence'))
            expected_witness = {
                'native_dataset_sha256': _mapping(refs.get('native_dataset')).get('sha256'),
                'feature_report_sha256': _mapping(refs.get('feature_report')).get('sha256'),
                'player_split_sha256': _mapping(refs.get('player_split')).get('sha256'),
                'stages_sha256': _mapping(_mapping(spec.get('dataset_lock')).get('exact_exam')).get('stages_sha256'),
                'shared_contract_sha256': expected['contract_sha256'],
            }
            if (any(not value for value in expected_witness.values())
                    or any(evidence.get(key) != value for key, value in expected_witness.items())
                    or _mapping(report.get('model')).get('shared_primary_training_evidence') != evidence):
                raise ValueError('Shared primary model/report lack matching native/feature/player evidence receipts')


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _portable_path(path: Path, project_root: Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(Path(project_root).resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_path(value: object, project_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("policy component has no path")
    path = Path(value)
    return path if path.is_absolute() else Path(project_root) / path


def _component(
    *,
    role: str,
    model_path: Path,
    report_path: Path,
    report_schema: str,
    expected_kind: str,
    project_root: Path,
    training_spec: Mapping[str, Any],
    training_spec_sha256: str,
    live_apply_allowed: bool = False,
) -> dict[str, object]:
    if type(live_apply_allowed) is not bool:
        raise TypeError("policy component live_apply_allowed must be bool")
    model_path = Path(model_path)
    report_path = Path(report_path)
    if not model_path.is_file() or not report_path.is_file():
        raise FileNotFoundError(f"missing {role} model/report")
    report = _read_json(report_path)
    if report.get("schema") != report_schema:
        raise ValueError(f"{role} report schema mismatch")
    declared_model_sha = _mapping(report.get("artifacts")).get("model_sha256")
    actual_model_sha = sha256_file(model_path)
    if declared_model_sha != actual_model_sha:
        raise ValueError(f"{role} model hash mismatch")
    _parameters, metadata, _extras = load_model_artifact(model_path)
    if metadata.get("kind") != expected_kind:
        raise ValueError(f"{role} model kind mismatch")
    acceptance = _mapping(report.get("acceptance"))
    if role == "exact_exam_policy":
        _validate_shared_exam_binding(training_spec, metadata, report)
        if _mapping(metadata.get('shared_primary_feature_contract')).get('diagnostic_only') is True:
            raise ValueError('Diagnostic-only shared feature contract cannot enter a policy bundle')
        if metadata.get('shared_primary_feature_contract') is not None and metadata.get('untrained_fixture') is True:
            raise ValueError('Untrained shared pointer fixture cannot enter a policy bundle')
        expected_features = training_spec.get("card_feature_contract")
        actual_features = metadata.get("card_feature_contract")
        if (actual_features is None) != (expected_features is None) or (
            expected_features is not None and any(
                _mapping(actual_features).get(key) != _mapping(expected_features).get(key)
                for key in ("schema", "features_sha256", "snapshot_fingerprint")
            )
        ):
            raise ValueError("exact Exam model and training spec card feature contracts differ")
        report_dataset = _mapping(report.get("dataset"))
        exact_lock = _mapping(
            _mapping(training_spec.get("dataset_lock")).get("exact_exam")
        )
        exact_inventory = _mapping(
            _mapping(training_spec.get("inventory")).get("exact_exam")
        )
        if (
            report_dataset.get("spec_sha256") != training_spec_sha256
            or report_dataset.get("stages_sha256")
            != exact_lock.get("stages_sha256")
            or report_dataset.get("example_count")
            != exact_inventory.get("transition_count")
        ):
            raise ValueError("exact Exam BC report is not bound to this training spec")
        if training_spec.get("schema") == SPEC_SCOPED_SCHEMA:
            expected_scope = training_spec.get("training_scope")
            if expected_scope != declared_training_scope(_mapping(expected_scope).get("flows", [])):
                raise ValueError("exact Exam training scope is invalid")
            for source in (metadata, _mapping(report.get("model"))):
                if (source.get("training_scope") != expected_scope
                        or source.get("training_spec_sha256") != training_spec_sha256
                        or source.get("stages_sha256") != exact_lock.get("stages_sha256")):
                    raise ValueError("exact Exam model/report scoped training binding mismatch")
    if role == "outer_policy" and acceptance.get("passed") is not True:
        raise ValueError("outer policy did not pass its frozen acceptance")
    if role == "exact_exam_policy" and not (
        acceptance.get("trained") is True
        and acceptance.get("strict_legal_binding_rate") == 1.0
    ):
        raise ValueError("exact Exam BC did not pass the shadow data contract")
    quality = "accepted"
    if role == "exact_exam_policy":
        improvement = float(
            acceptance.get("validation_improvement_over_frequency_baseline_pp", 0.0)
        )
        quality = (
            "accepted-for-shadow"
            if acceptance.get("shadow_ready") is True
            else (
                "below-frequency-baseline"
                if improvement < 0
                else "below-shadow-threshold"
            )
        )
    if role == "replay_syntax_prior" and acceptance.get("syntax_prior_accepted") is not True:
        quality = "diagnostic-only"
    shadow_ready = (
        acceptance.get("shadow_ready") is True
        if role == "exact_exam_policy"
        else role != "replay_syntax_prior" or quality == "accepted"
    )
    if live_apply_allowed and (
        role not in {"outer_policy", "exact_exam_policy"} or not shadow_ready
    ):
        raise ValueError("live apply requires a shadow-ready decision policy")
    return {
        "role": role,
        # ``enabled`` says that the artifact may be evaluated.  It is kept
        # separate from the live-apply gate so a shadow component can never
        # acquire decision ownership merely by being present in a bundle.
        "enabled": True,
        "model_path": _portable_path(model_path, project_root),
        "model_sha256": actual_model_sha,
        "report_path": _portable_path(report_path, project_root),
        "report_sha256": sha256_file(report_path),
        "model_kind": expected_kind,
        "quality": quality,
        "shadow_ready": shadow_ready,
        "live_apply_allowed": live_apply_allowed,
        "syntax_only": role == "replay_syntax_prior",
    }


def _offline_rl_component(
    *,
    model_path: Path,
    report_path: Path,
    runtime_enabled: bool,
    project_root: Path,
) -> dict[str, object]:
    """Bind one IQL artifact; promotion remains an explicit bundle choice."""

    if type(runtime_enabled) is not bool:
        raise TypeError("offline RL runtime_enabled must be bool")
    from .offline_rl_learner import (
        MODEL_KIND as OFFLINE_RL_MODEL_KIND,
        REPORT_SCHEMA as OFFLINE_RL_REPORT_SCHEMA,
        load_offline_rl_model,
    )

    model_path = Path(model_path)
    report_path = Path(report_path)
    if not model_path.is_file() or not report_path.is_file():
        raise FileNotFoundError("missing offline RL model/report")
    report = _read_json(report_path)
    if report.get("schema") != OFFLINE_RL_REPORT_SCHEMA:
        raise ValueError("offline RL report schema mismatch")
    actual_model_sha = sha256_file(model_path)
    if _mapping(report.get("artifacts")).get("model_sha256") != actual_model_sha:
        raise ValueError("offline RL report/model hash mismatch")
    _arrays, metadata = load_offline_rl_model(
        model_path,
        expected_sha256=actual_model_sha,
    )
    if metadata.get("kind") != OFFLINE_RL_MODEL_KIND:
        raise ValueError("offline RL model kind mismatch")
    acceptance = _mapping(report.get("acceptance"))
    validated = acceptance.get("iql_validated") is True
    shadow_ready = acceptance.get("shadow_ready") is True
    if runtime_enabled and not (validated and shadow_ready):
        raise ValueError(
            "offline RL runtime requires a validated shadow-ready artifact"
        )
    return {
        "role": "offline_rl_policy",
        "enabled": runtime_enabled,
        "model_path": _portable_path(model_path, project_root),
        "model_sha256": actual_model_sha,
        "report_path": _portable_path(report_path, project_root),
        "report_sha256": sha256_file(report_path),
        "model_kind": OFFLINE_RL_MODEL_KIND,
        "quality": (
            "accepted-for-learned-chain"
            if validated and shadow_ready
            else "diagnostic-only"
        ),
        "shadow_ready": shadow_ready,
        "iql_validated": validated,
        "runtime_enabled": runtime_enabled,
        "live_apply_allowed": runtime_enabled,
        "promotion_boundary": "explicit-policy-bundle",
    }


def build_policy_bundle_manifest(
    *,
    bundle_id: str = "behavior-cloning-v0",
    spec_path: Path = DEFAULT_SPEC,
    outer_model_path: Path | None = DEFAULT_OUTER_MODEL,
    outer_report_path: Path | None = DEFAULT_OUTER_REPORT,
    exact_model_path: Path = DEFAULT_EXACT_MODEL,
    exact_report_path: Path = DEFAULT_EXACT_REPORT,
    replay_model_path: Path | None = DEFAULT_REPLAY_MODEL,
    replay_report_path: Path | None = DEFAULT_REPLAY_REPORT,
    outer_live_apply_allowed: bool = False,
    exact_live_apply_allowed: bool = False,
    offline_rl_model_path: Path | None = None,
    offline_rl_report_path: Path | None = None,
    offline_rl_runtime_enabled: bool = False,
    action_executor: str = "existing Maa/controller input adapter",
    project_root: Path = PROJECT_ROOT,
) -> dict[str, object]:
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        raise ValueError("bundle_id must be non-empty text")
    bundle_id = bundle_id.strip()
    if type(outer_live_apply_allowed) is not bool:
        raise TypeError("outer_live_apply_allowed must be bool")
    if type(exact_live_apply_allowed) is not bool:
        raise TypeError("exact_live_apply_allowed must be bool")
    if type(offline_rl_runtime_enabled) is not bool:
        raise TypeError("offline_rl_runtime_enabled must be bool")
    if (offline_rl_model_path is None) != (offline_rl_report_path is None):
        raise ValueError("offline RL model and report must be supplied together")
    if offline_rl_runtime_enabled and not exact_live_apply_allowed:
        raise ValueError("offline RL runtime requires live Exact BC fallback")
    if (outer_model_path is None) != (outer_report_path is None):
        raise ValueError("outer model and report must be supplied together")
    if outer_live_apply_allowed and outer_model_path is None:
        raise ValueError("live outer policy requires its explicit model")
    if action_executor not in {"existing Maa/controller input adapter", "dll-managed-single-action"}:
        raise ValueError("unsupported policy input executor")
    spec_path = Path(spec_path)
    spec = _read_json(spec_path)
    if spec.get("schema") != SPEC_V2_SCHEMA:
        raise ValueError("policy bundle requires training spec v2")
    spec_sha256 = sha256_file(spec_path)
    components = {}
    if outer_model_path is not None:
        components["outer_policy"] = _component(
            role="outer_policy",
            model_path=outer_model_path,
            report_path=outer_report_path,
            report_schema=OUTER_REPORT_SCHEMA,
            expected_kind="outer-candidate-pointer",
            project_root=project_root,
            training_spec=spec,
            training_spec_sha256=spec_sha256,
            live_apply_allowed=outer_live_apply_allowed,
        )
    components["exact_exam_policy"] = _component(
            role="exact_exam_policy",
            model_path=exact_model_path,
            report_path=exact_report_path,
            report_schema=EXACT_REPORT_SCHEMA,
            expected_kind="exact-main-action-candidate-pointer",
            project_root=project_root,
            training_spec=spec,
            training_spec_sha256=spec_sha256,
            live_apply_allowed=exact_live_apply_allowed,
        )
    if replay_model_path is not None and replay_report_path is not None:
        components["replay_syntax_prior"] = _component(
            role="replay_syntax_prior",
            model_path=replay_model_path,
            report_path=replay_report_path,
            report_schema=REPLAY_REPORT_SCHEMA,
            expected_kind="replay-hierarchical-action-prefix",
            project_root=project_root,
            training_spec=spec,
            training_spec_sha256=spec_sha256,
        )
    if offline_rl_model_path is not None and offline_rl_report_path is not None:
        components["offline_rl_policy"] = _offline_rl_component(
            model_path=offline_rl_model_path,
            report_path=offline_rl_report_path,
            runtime_enabled=offline_rl_runtime_enabled,
            project_root=project_root,
        )
    default_enabled = outer_live_apply_allowed or exact_live_apply_allowed
    outer_owner = (
        "outer-behavior-cloning"
        if outer_live_apply_allowed
        else "existing-formal-policy"
    )
    exam_owner = (
        "offline-rl-to-exact-behavior-cloning"
        if offline_rl_runtime_enabled
        else "exact-behavior-cloning"
        if exact_live_apply_allowed
        else "existing-formal-policy"
    )
    decision_owner = (
        "surface-routed-learned-policy"
        if outer_live_apply_allowed and exact_live_apply_allowed
        else "outer-learned-policy"
        if outer_live_apply_allowed
        else "offline-rl-to-behavior-cloning"
        if offline_rl_runtime_enabled
        else "exact-learned-policy"
        if exact_live_apply_allowed
        else "existing-formal-policy"
    )
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "bundle_id": bundle_id,
        "training_spec_path": _portable_path(spec_path, project_root),
        "training_spec_sha256": spec_sha256,
        "components": components,
        "runtime": {
            "default_enabled": default_enabled,
            "live_apply_allowed": {
                "outer": outer_live_apply_allowed,
                "exam": exact_live_apply_allowed,
            },
            "mode": (
                "learned-policy" if default_enabled else "shadow-only"
            ),
            "decision_owner": decision_owner,
            "decision_owner_by_surface": {
                "outer": outer_owner,
                "exam": exam_owner,
            },
            "action_executor": action_executor,
            "formal_controller_remains_owner": not default_enabled,
            "formal_controller_remains_owner_by_surface": {
                "outer": not outer_live_apply_allowed,
                "exam": not exact_live_apply_allowed,
            },
            "model_swap_boundary": "new cultivation or new Exam stage",
            "mid_action_swap_allowed": False,
            "fallback": (
                "stop-on-no-learned-decision"
                if default_enabled
                else "none-shadow-only"
            ),
        },
    }
    contract = spec.get("card_feature_contract")
    if contract is not None:
        load_card_semantic_features(contract, relative_to=spec_path.parent)
        manifest["card_data_contract"] = {
            "schema": contract["schema"],
            "snapshot_fingerprint": contract["snapshot_fingerprint"],
            "features_sha256": contract["features_sha256"],
        }
    if spec.get('shared_primary_feature_contract') is not None:
        manifest['shared_primary_feature_contract'] = dict(spec['shared_primary_feature_contract'])
    return manifest


def write_policy_bundle_manifest(
    output_path: Path = DEFAULT_OUTPUT,
    **kwargs: Any,
) -> dict[str, object]:
    manifest = build_policy_bundle_manifest(**kwargs)
    atomic_write(
        Path(output_path),
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n",
    )
    return manifest


def discover_policy_bundle_manifests(
    root: Path = DEFAULT_BUNDLE_ROOT,
) -> tuple[Path, ...]:
    root = Path(root)
    if not root.is_dir():
        return ()
    return tuple(sorted(root.glob("*/manifest.json"), key=lambda path: path.as_posix()))


def activate_policy_bundle(
    manifest_path: Path,
    *,
    output_path: Path = DEFAULT_ACTIVE_BUNDLE,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, object]:
    bundle = PolicyBundle.load(manifest_path, project_root=project_root)
    contract = bundle.payload.get("card_data_contract")
    if isinstance(contract, Mapping):
        from .card_data_update import read_card_data_snapshot
        current = read_card_data_snapshot(Path(project_root) / "var" / "master.sqlite3")
        if contract.get("snapshot_fingerprint") != current["fingerprint"]:
            raise ValueError("policy bundle card data differs from current Master; rebuild or validate the model update")
    payload = {
        "schema": ACTIVE_BUNDLE_SCHEMA,
        "bundle_id": bundle.bundle_id,
        "manifest_path": _portable_path(bundle.manifest_path, project_root),
        "manifest_sha256": sha256_file(bundle.manifest_path),
        "activation_boundary": "next cultivation or next Exam stage",
    }
    atomic_write(
        Path(output_path),
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n",
    )
    return payload


def resolve_active_policy_bundle(
    *,
    selection_path: Path = DEFAULT_ACTIVE_BUNDLE,
    fallback_path: Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    selection_path = Path(selection_path)
    if not selection_path.is_file():
        # A missing current selection must not silently revive the historical
        # v0 model. Research callers may still request an explicit fallback.
        if fallback_path is not None:
            return Path(fallback_path)
        raise FileNotFoundError("no policy bundle selected; select a validated model explicitly")
    selection = _read_json(selection_path)
    if selection.get("schema") != ACTIVE_BUNDLE_SCHEMA:
        raise ValueError("active policy bundle schema mismatch")
    manifest = _resolve_path(selection.get("manifest_path"), project_root)
    if sha256_file(manifest) != selection.get("manifest_sha256"):
        raise ValueError("active policy bundle manifest hash mismatch")
    return manifest


def _validate_scoped_inner_component(
    name: str, component: Mapping[str, Any], *, model: Path, report: Path,
    spec: Mapping[str, Any], spec_sha256: str, project_root: Path,
) -> None:
    """Reuse the existing BC/IQL artifact validators at the execution scope."""
    if name == "exact_exam_policy":
        _component(role=name, model_path=model, report_path=report,
            report_schema=EXACT_REPORT_SCHEMA, expected_kind="exact-main-action-candidate-pointer",
            project_root=project_root, training_spec=spec, training_spec_sha256=spec_sha256,
            live_apply_allowed=component.get("live_apply_allowed", False))
        metadata = None  # _component already verifies BC metadata/spec features.
    elif name == "offline_rl_policy":
        _offline_rl_component(model_path=model, report_path=report,
            runtime_enabled=component.get("runtime_enabled", False), project_root=project_root)
        from .offline_rl_learner import load_offline_rl_model
        _arrays, metadata = load_offline_rl_model(model, expected_sha256=component.get("model_sha256"))
    else:
        return
    report_payload = _read_json(report)
    expected_features = spec.get("card_feature_contract")
    contracts = [(_mapping(report_payload.get("model")).get("card_feature_contract"), "report")]
    if metadata is not None:
        contracts.append((metadata.get("card_feature_contract"), "model"))
    for actual, source in contracts:
        if (actual is None) != (expected_features is None) or (
            expected_features is not None and any(_mapping(actual).get(key) != _mapping(expected_features).get(key)
                for key in ("schema", "features_sha256", "snapshot_fingerprint"))
        ):
            raise ValueError(f"policy bundle {name} {source} card feature contract mismatch")
    if metadata is not None:
        dataset = _mapping(report_payload.get("dataset"))
        exact_lock = _mapping(_mapping(spec.get("dataset_lock")).get("exact_exam"))
        exact_inventory = _mapping(_mapping(spec.get("inventory")).get("exact_exam"))
        expected_stages = exact_lock.get("stages_sha256")
        if (dataset.get("spec_sha256") != spec_sha256 or dataset.get("stages_sha256") != expected_stages
                or metadata.get("training_spec_sha256") != spec_sha256 or metadata.get("stages_sha256") != expected_stages):
            raise ValueError("policy bundle offline RL report/model/spec binding mismatch")
        if "transition_count" in dataset and dataset["transition_count"] != exact_inventory.get("transition_count"):
            raise ValueError("policy bundle offline RL report/spec transition count mismatch")
        if spec.get("schema") == SPEC_SCOPED_SCHEMA:
            for source in (metadata, _mapping(report_payload.get("model"))):
                if source.get("training_scope") != spec.get("training_scope"):
                    raise ValueError("policy bundle offline RL scoped training binding mismatch")


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    manifest_path: Path
    project_root: Path
    payload: Mapping[str, Any]
    # None is the historical full-load contract. A scoped instance cannot
    # accidentally execute an unrelated component that was not validated.
    validated_component_roles: frozenset[str] | None = None

    @classmethod
    def load(
        cls,
        manifest_path: Path | None = None,
        *,
        project_root: Path = PROJECT_ROOT,
        component_roles: Sequence[str] | None = None,
        optional_component_roles: Sequence[str] = (),
    ) -> "PolicyBundle":
        """Load all components by default, or an explicit execution scope.

        Required roles must exist. Optional roles may be absent, but when
        present receive exactly the same artifact/spec/feature checks. Common
        spec and card-feature dependencies are always validated. This does not
        create another bundle registry or change the active selection.
        """
        def roles(value: Sequence[str], *, label: str, empty: bool) -> frozenset[str]:
            if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
                raise ValueError(f"{label} must be a sequence of component names")
            if any(not isinstance(role, str) or not role.strip() for role in value):
                raise ValueError(f"{label} has an invalid component name")
            if not empty and not value:
                raise ValueError(f"{label} must not be empty")
            if len(set(value)) != len(value):
                raise ValueError(f"{label} contains duplicate component names")
            return frozenset(value)

        optional = roles(optional_component_roles, label="optional_component_roles", empty=True)
        required = None if component_roles is None else roles(component_roles, label="component_roles", empty=False)
        if required is None and optional:
            raise ValueError("optional_component_roles requires an explicit component_roles scope")
        manifest_path = (
            resolve_active_policy_bundle(project_root=project_root)
            if manifest_path is None
            else Path(manifest_path)
        )
        payload = _read_json(manifest_path)
        if payload.get("schema") != BUNDLE_SCHEMA:
            raise ValueError("policy bundle schema mismatch")
        spec_path = _resolve_path(payload.get("training_spec_path"), project_root)
        if sha256_file(spec_path) != payload.get("training_spec_sha256"):
            raise ValueError("policy bundle training spec hash mismatch")
        spec = _read_json(spec_path)
        if payload.get('shared_primary_feature_contract') != spec.get('shared_primary_feature_contract'):
            raise ValueError('Policy bundle shared primary contract differs from training spec')
        expected_features = spec.get("card_feature_contract")
        if expected_features is not None:
            load_card_semantic_features(expected_features, relative_to=spec_path.parent)
            declared = _mapping(payload.get("card_data_contract"))
            if any(declared.get(key) != expected_features.get(key) for key in (
                "schema", "snapshot_fingerprint", "features_sha256",
            )):
                raise ValueError("policy bundle card feature contract differs from training spec")
        components = payload.get("components")
        if not isinstance(components, Mapping) or not components:
            raise ValueError("policy bundle has no components")
        validated_roles = None
        if required is not None:
            absent = required.difference(components)
            if absent:
                raise KeyError("required policy bundle components are missing: " + ",".join(sorted(absent)))
            validated_roles = required | optional.intersection(components)
        for name, raw in components.items():
            if validated_roles is not None and name not in validated_roles:
                continue
            component = _mapping(raw)
            model = _resolve_path(component.get("model_path"), project_root)
            report = _resolve_path(component.get("report_path"), project_root)
            if sha256_file(model) != component.get("model_sha256"):
                raise ValueError(f"policy bundle {name} model hash mismatch")
            if sha256_file(report) != component.get("report_sha256"):
                raise ValueError(f"policy bundle {name} report hash mismatch")
            if (validated_roles is not None or spec.get("schema") == SPEC_SCOPED_SCHEMA
                    or spec.get('shared_primary_feature_contract') is not None) and name in {"exact_exam_policy", "offline_rl_policy"}:
                _validate_scoped_inner_component(name, component, model=model, report=report,
                    spec=spec, spec_sha256=str(payload.get("training_spec_sha256")), project_root=Path(project_root))
            elif expected_features is not None and name in {"exact_exam_policy", "offline_rl_policy"}:
                if name == "exact_exam_policy":
                    metadata = load_model_artifact(model)[1]
                else:
                    from .offline_rl_learner import load_offline_rl_model
                    metadata = load_offline_rl_model(model)[1]
                actual_features = _mapping(metadata.get("card_feature_contract"))
                if any(actual_features.get(key) != expected_features.get(key) for key in (
                    "schema", "snapshot_fingerprint", "features_sha256",
                )):
                    raise ValueError(f"policy bundle {name} card feature contract mismatch")
            if name == "exact_exam_policy":
                report_payload = _read_json(report)
                _validate_shared_exam_binding(spec, load_model_artifact(model)[1], report_payload)
                report_features = _mapping(report_payload.get("model")).get("card_feature_contract")
                if (report_features is None) != (expected_features is None):
                    raise ValueError("exact Exam report and spec use different card feature schemas")
                report_dataset = _mapping(report_payload.get("dataset"))
                exact_lock = _mapping(
                    _mapping(spec.get("dataset_lock")).get("exact_exam")
                )
                exact_inventory = _mapping(
                    _mapping(spec.get("inventory")).get("exact_exam")
                )
                if (
                    report_dataset.get("spec_sha256")
                    != payload.get("training_spec_sha256")
                    or report_dataset.get("stages_sha256")
                    != exact_lock.get("stages_sha256")
                    or report_dataset.get("example_count")
                    != exact_inventory.get("transition_count")
                ):
                    raise ValueError("policy bundle exact report/spec binding mismatch")
        from .policy_mode_coverage import validate_mode_transfer_declaration
        validate_mode_transfer_declaration(payload, spec)
        return cls(manifest_path.resolve(), Path(project_root).resolve(), dict(payload), validated_roles)

    @property
    def bundle_id(self) -> str:
        return str(self.payload.get("bundle_id"))

    def component(self, role: str) -> Mapping[str, Any]:
        if self.validated_component_roles is not None and role not in self.validated_component_roles:
            raise KeyError(f"policy component was not validated in this load scope: {role}")
        component = _mapping(_mapping(self.payload.get("components")).get(role))
        if not component:
            raise KeyError(role)
        return component

    def model_path(self, role: str) -> Path:
        return _resolve_path(self.component(role).get("model_path"), self.project_root)

    def report_path(self, role: str) -> Path:
        return _resolve_path(self.component(role).get("report_path"), self.project_root)

    def score_outer(
        self,
        *,
        context_tokens: Sequence[str],
        candidate_ids: Sequence[str],
    ) -> tuple[float, ...]:
        return score_pointer_candidates(
            self.model_path("outer_policy"),
            context_tokens=context_tokens,
            candidate_semantics=candidate_ids,
        )

    def score_exam(
        self,
        *,
        state_before: Mapping[str, Any],
        flow: str,
        stage: str,
        legal_candidates: Sequence[object],
        allow_diagnostic: bool = False,
    ) -> tuple[float, ...]:
        component = self.component("exact_exam_policy")
        if component.get("shadow_ready") is not True and not allow_diagnostic:
            raise RuntimeError("exact Exam policy is not shadow-ready")
        return score_exact_main_action_candidates(
            self.model_path("exact_exam_policy"),
            state_before=state_before,
            flow=flow,
            stage=stage,
            legal_candidates=legal_candidates,
        )

    def replay_prior(self, *, context_tokens: Sequence[str]) -> dict[str, object]:
        return predict_replay_syntax(
            self.model_path("replay_syntax_prior"), context_tokens=context_tokens
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a swappable GKMS policy bundle")
    parser.add_argument("--bundle-id", default="behavior-cloning-v0")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--outer-model", type=Path, default=DEFAULT_OUTER_MODEL)
    parser.add_argument("--outer-report", type=Path, default=DEFAULT_OUTER_REPORT)
    parser.add_argument("--exact-model", type=Path, default=DEFAULT_EXACT_MODEL)
    parser.add_argument("--exact-report", type=Path, default=DEFAULT_EXACT_REPORT)
    parser.add_argument("--replay-model", type=Path, default=DEFAULT_REPLAY_MODEL)
    parser.add_argument("--replay-report", type=Path, default=DEFAULT_REPLAY_REPORT)
    parser.add_argument("--outer-live-apply-allowed", action="store_true")
    parser.add_argument("--exact-live-apply-allowed", action="store_true")
    parser.add_argument("--offline-rl-model", type=Path)
    parser.add_argument("--offline-rl-report", type=Path)
    parser.add_argument("--offline-rl-runtime-enabled", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = write_policy_bundle_manifest(
        args.output,
        bundle_id=args.bundle_id,
        spec_path=args.spec,
        outer_model_path=args.outer_model,
        outer_report_path=args.outer_report,
        exact_model_path=args.exact_model,
        exact_report_path=args.exact_report,
        replay_model_path=args.replay_model,
        replay_report_path=args.replay_report,
        outer_live_apply_allowed=args.outer_live_apply_allowed,
        exact_live_apply_allowed=args.exact_live_apply_allowed,
        offline_rl_model_path=args.offline_rl_model,
        offline_rl_report_path=args.offline_rl_report,
        offline_rl_runtime_enabled=args.offline_rl_runtime_enabled,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


__all__ = [
    "ACTIVE_BUNDLE_SCHEMA",
    "BUNDLE_SCHEMA",
    "DEFAULT_ACTIVE_BUNDLE",
    "DEFAULT_BUNDLE_ROOT",
    "DEFAULT_OUTPUT",
    "PolicyBundle",
    "activate_policy_bundle",
    "build_policy_bundle_manifest",
    "discover_policy_bundle_manifests",
    "main",
    "resolve_active_policy_bundle",
    "write_policy_bundle_manifest",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
