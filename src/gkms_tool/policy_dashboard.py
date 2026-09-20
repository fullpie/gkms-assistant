"""Read adapters for the standalone model/shadow dashboard."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from .policy_bundle import (
    DEFAULT_ACTIVE_BUNDLE,
    DEFAULT_BUNDLE_ROOT,
    PROJECT_ROOT,
    PolicyBundle,
    discover_policy_bundle_manifests,
    resolve_active_policy_bundle,
)
from .training_artifact_io import sha256_file


DASHBOARD_SCHEMA: Final = "gkms.policy-dashboard.v1"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> Mapping[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected JSON object: {path}")
    return value


@dataclass(frozen=True, slots=True)
class PolicyComponentView:
    role: str
    quality: str
    shadow_ready: bool
    live_apply_allowed: bool
    syntax_only: bool
    model_sha256: str


@dataclass(frozen=True, slots=True)
class PolicyMetricView:
    role: str
    sample_count: int | None
    validation_top1: float | None
    test_top1: float | None
    baseline_top1: float | None
    improvement_pp: float | None
    note: str


@dataclass(frozen=True, slots=True)
class PolicyDashboardView:
    schema: str
    bundle_id: str
    manifest_path: str
    manifest_sha256: str
    active: bool
    training_spec_sha256: str
    runtime_mode: str
    swap_boundary: str
    components: tuple[PolicyComponentView, ...]
    metrics: tuple[PolicyMetricView, ...]
    exact_stage_count: int | None
    exact_transition_count: int | None
    exact_split_counts: Mapping[str, int]
    live_shadow: Mapping[str, object]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["components"] = [asdict(item) for item in self.components]
        value["metrics"] = [asdict(item) for item in self.metrics]
        value["exact_split_counts"] = dict(self.exact_split_counts)
        value["live_shadow"] = dict(self.live_shadow)
        value["warnings"] = list(self.warnings)
        return value


def _report_metric(role: str, report: Mapping[str, Any]) -> PolicyMetricView:
    dataset = _mapping(report.get("dataset"))
    metrics = _mapping(report.get("metrics"))
    validation = _mapping(metrics.get("validation"))
    test = _mapping(metrics.get("test"))
    baseline = _mapping(metrics.get("validation_frequency_baseline"))
    acceptance = _mapping(report.get("acceptance"))
    if role == "outer_policy":
        sample_count = dataset.get("example_count")
        validation_top1 = validation.get("macro_flow_top1")
        test_top1 = test.get("macro_flow_top1")
        baseline_top1 = baseline.get("macro_flow_top1")
        improvement = acceptance.get("improvement_over_frequency_baseline_pp")
        note = "外層培育決策；已通過離線 acceptance，仍只作 shadow。"
    elif role == "exact_exam_policy":
        sample_count = dataset.get("example_count")
        validation_top1 = validation.get("macro_flow_top1")
        test_top1 = test.get("macro_flow_top1")
        baseline_top1 = baseline.get("macro_flow_top1")
        improvement = acceptance.get(
            "validation_improvement_over_frequency_baseline_pp"
        )
        note = (
            "Exact Exam BC v0；已通過離線 shadow 門檻，仍不會套用動作。"
            if acceptance.get("shadow_ready") is True
            else "Exact Exam BC v0；合法候選綁定完成，但尚未通過 shadow 門檻。"
        )
    else:
        sample_count = dataset.get("action_example_count")
        validation_top1 = validation.get("macro_flow_token_top1")
        test_top1 = test.get("macro_flow_token_top1")
        baseline_top1 = _mapping(
            metrics.get("validation_frequency_baseline")
        ).get("macro_flow_token_top1")
        improvement = acceptance.get("validation_macro_token_improvement_pp")
        note = "排行榜 action/index 語法先驗；不能直接選擇實機卡牌。"

    def integer(value: object) -> int | None:
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else None

    def number(value: object) -> float | None:
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    return PolicyMetricView(
        role=role,
        sample_count=integer(sample_count),
        validation_top1=number(validation_top1),
        test_top1=number(test_top1),
        baseline_top1=number(baseline_top1),
        improvement_pp=number(improvement),
        note=note,
    )


def load_policy_dashboard_view(
    manifest_path: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    active_selection_path: Path = DEFAULT_ACTIVE_BUNDLE,
) -> PolicyDashboardView:
    bundle = PolicyBundle.load(manifest_path, project_root=project_root)
    payload = bundle.payload
    runtime = _mapping(payload.get("runtime"))
    try:
        active_manifest = resolve_active_policy_bundle(
            selection_path=active_selection_path,
            project_root=project_root,
        ).resolve()
    except (FileNotFoundError, OSError, TypeError, ValueError):
        active_manifest = None
    components: list[PolicyComponentView] = []
    metrics: list[PolicyMetricView] = []
    warnings: list[str] = []
    outer_report_path: Path | None = None
    outer_model_sha: str | None = None
    for role, raw in sorted(_mapping(payload.get("components")).items()):
        component = _mapping(raw)
        quality = str(component.get("quality", "unknown"))
        components.append(
            PolicyComponentView(
                role=str(role),
                quality=quality,
                shadow_ready=component.get("shadow_ready") is True,
                live_apply_allowed=component.get("live_apply_allowed") is True,
                syntax_only=component.get("syntax_only") is True,
                model_sha256=str(component.get("model_sha256", "")),
            )
        )
        metrics.append(_report_metric(str(role), _read_json(bundle.report_path(str(role)))))
        if role == "outer_policy":
            outer_report_path = bundle.report_path(str(role))
            outer_model_sha = str(component.get("model_sha256", ""))
        if quality != "accepted":
            warnings.append(f"{role}: {quality}")
    spec_path_value = payload.get("training_spec_path")
    if not isinstance(spec_path_value, str):
        raise ValueError("bundle training spec path is missing")
    spec_path = Path(spec_path_value)
    if not spec_path.is_absolute():
        spec_path = Path(project_root) / spec_path
    spec = _read_json(spec_path)
    exact_inventory = _mapping(_mapping(spec.get("inventory")).get("exact_exam"))
    split_counts = {
        str(key): int(value)
        for key, value in _mapping(exact_inventory.get("split_transition_counts")).items()
        if isinstance(value, int) and not isinstance(value, bool)
    }
    live_shadow: dict[str, object] = {"status": "not-recorded"}
    if outer_report_path is not None:
        audits = sorted(
            outer_report_path.parent.glob("outer_bc_live_shadow_audit_*.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if audits:
            audit = _read_json(audits[-1])
            shadow = _mapping(audit.get("shadow"))
            model_hashes = shadow.get("model_sha256")
            matches_model = (
                isinstance(model_hashes, list)
                and model_hashes == [outer_model_sha]
            )
            live_shadow = {
                "status": "recorded" if matches_model else "model-mismatch",
                "path": str(audits[-1]),
                "accepted": _mapping(audit.get("acceptance")).get("passed") is True,
                "decision_count": shadow.get("decision_count"),
                "agreement_rate": shadow.get("agreement_rate"),
                "formal_binding_mismatch_count": len(
                    shadow.get("formal_binding_mismatches", [])
                    if isinstance(shadow.get("formal_binding_mismatches"), list)
                    else []
                ),
                "applied_true_count": len(
                    shadow.get("applied_true_steps", [])
                    if isinstance(shadow.get("applied_true_steps"), list)
                    else []
                ),
            }
    return PolicyDashboardView(
        schema=DASHBOARD_SCHEMA,
        bundle_id=bundle.bundle_id,
        manifest_path=str(bundle.manifest_path),
        manifest_sha256=sha256_file(bundle.manifest_path),
        active=bundle.manifest_path == active_manifest,
        training_spec_sha256=str(payload.get("training_spec_sha256", "")),
        runtime_mode=str(runtime.get("mode", "unknown")),
        swap_boundary=str(runtime.get("model_swap_boundary", "unknown")),
        components=tuple(components),
        metrics=tuple(metrics),
        exact_stage_count=(
            int(exact_inventory["stage_count"])
            if isinstance(exact_inventory.get("stage_count"), int)
            else None
        ),
        exact_transition_count=(
            int(exact_inventory["transition_count"])
            if isinstance(exact_inventory.get("transition_count"), int)
            else None
        ),
        exact_split_counts=split_counts,
        live_shadow=live_shadow,
        warnings=tuple(warnings),
    )


def discover_policy_dashboard_views(
    root: Path = DEFAULT_BUNDLE_ROOT,
    *,
    project_root: Path = PROJECT_ROOT,
    active_selection_path: Path = DEFAULT_ACTIVE_BUNDLE,
) -> tuple[PolicyDashboardView, ...]:
    return tuple(
        load_policy_dashboard_view(
            path,
            project_root=project_root,
            active_selection_path=active_selection_path,
        )
        for path in discover_policy_bundle_manifests(root)
    )


__all__ = [
    "DASHBOARD_SCHEMA",
    "PolicyComponentView",
    "PolicyDashboardView",
    "PolicyMetricView",
    "discover_policy_dashboard_views",
    "load_policy_dashboard_view",
]
