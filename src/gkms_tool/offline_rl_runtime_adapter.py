"""Fail-closed runtime adapter for promoted Offline RL artifacts.

The adapter is deliberately policy-only: it reads and verifies one frozen
model/report pair, scores the caller's unchanged unified legal candidates,
and returns an :class:`OfflineRLActionProposal`.  It owns no controller,
LocalSave reader, Plan-specific executor, or fallback action.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Final

from .learned_policy_selector import OfflineRLActionProposal
from .exact_exam_state_projection import normalize_exact_exam_legal_candidates
from .offline_rl_learner import REPORT_SCHEMA, score_offline_rl_candidates
from .training_artifact_io import sha256_file
from .unified_legal_action_snapshot import UnifiedLegalActionSnapshot


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _read_report(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Offline RL report is unreadable: {path}") from error
    return _mapping(value, "Offline RL report")


def _resolve_path(value: object, *, project_root: Path, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value):
        raise ValueError(f"{label} is missing")
    path = Path(value)
    return path.resolve() if path.is_absolute() else (project_root / path).resolve()


def _expected_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be lowercase SHA-256 text")
    return value


def _verify_hash(path: Path, expected: str | None, label: str) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = sha256_file(path)
    if expected is not None and actual != expected:
        raise ValueError(f"Offline RL {label} SHA-256 mismatch")
    return actual


@dataclass(frozen=True, slots=True)
class OfflineRLArtifactSelector:
    """Verified model/report pair implementing ``OfflineRLSelector``.

    ``runtime_enabled`` is intentionally stricter than the component switch:
    it becomes true only when the report also declares both IQL validation and
    shadow readiness.  This keeps direct callers as fail-closed as the
    plan-neutral selector, which checks the two properties independently.
    """

    model_path: Path
    report_path: Path
    model_sha256: str
    report_sha256: str
    policy_id: str
    _validated: bool
    _runtime_requested: bool

    @classmethod
    def from_component(
        cls,
        component: Mapping[str, Any],
        *,
        project_root: str | Path = PROJECT_ROOT,
    ) -> "OfflineRLArtifactSelector":
        """Load a hash-bound selector from one component mapping."""

        raw = _mapping(component, "Offline RL component")
        root = Path(project_root).resolve()
        if type(raw.get("runtime_enabled")) is not bool:
            raise ValueError("Offline RL component runtime_enabled must be boolean")
        return cls.from_paths(
            model_path=_resolve_path(
                raw.get("model_path"), project_root=root, label="model_path"
            ),
            report_path=_resolve_path(
                raw.get("report_path"), project_root=root, label="report_path"
            ),
            expected_model_sha256=_expected_sha256(
                raw.get("model_sha256"), "component model_sha256"
            ),
            expected_report_sha256=_expected_sha256(
                raw.get("report_sha256"), "component report_sha256"
            ),
            policy_id=raw.get("policy_id"),
            runtime_enabled=raw["runtime_enabled"],
        )

    @classmethod
    def from_paths(
        cls,
        model_path: str | Path,
        report_path: str | Path,
        *,
        policy_id: str | None = None,
        runtime_enabled: bool = False,
        expected_model_sha256: str | None = None,
        expected_report_sha256: str | None = None,
    ) -> "OfflineRLArtifactSelector":
        """Load explicitly selected paths and freeze their current hashes."""

        if type(runtime_enabled) is not bool:
            raise TypeError("Offline RL runtime_enabled must be bool")
        model = Path(model_path).resolve()
        report = Path(report_path).resolve()
        expected_model = _expected_sha256(
            expected_model_sha256, "expected model SHA-256"
        )
        expected_report = _expected_sha256(
            expected_report_sha256, "expected report SHA-256"
        )
        model_hash = _verify_hash(model, expected_model, "model")
        report_hash = _verify_hash(report, expected_report, "report")
        payload = _read_report(report)
        if payload.get("schema") != REPORT_SCHEMA:
            raise ValueError("Offline RL report schema mismatch")
        artifacts = _mapping(payload.get("artifacts"), "Offline RL report artifacts")
        if artifacts.get("model_sha256") != model_hash:
            raise ValueError("Offline RL report/model binding mismatch")
        acceptance = _mapping(
            payload.get("acceptance"), "Offline RL report acceptance"
        )
        validated = bool(
            acceptance.get("iql_validated") is True
            and acceptance.get("shadow_ready") is True
        )
        identifier = policy_id
        if identifier is None:
            raw_identifier = payload.get("policy_id")
            identifier = (
                raw_identifier
                if isinstance(raw_identifier, str) and raw_identifier
                else f"offline-rl:{model_hash[:16]}"
            )
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Offline RL policy_id must be non-empty text")
        return cls(
            model_path=model,
            report_path=report,
            model_sha256=model_hash,
            report_sha256=report_hash,
            policy_id=identifier.strip(),
            _validated=validated,
            _runtime_requested=runtime_enabled,
        )

    @property
    def validated(self) -> bool:
        return self._validated

    @property
    def runtime_enabled(self) -> bool:
        return self._validated and self._runtime_requested

    def select_action(
        self,
        *,
        state_before: Mapping[str, Any],
        snapshot: UnifiedLegalActionSnapshot,
        stage: str,
    ) -> OfflineRLActionProposal:
        """Score the unchanged legal order and select only a unique top."""

        if not isinstance(state_before, Mapping):
            return OfflineRLActionProposal(blockers=("state-before-invalid",))
        if not isinstance(snapshot, UnifiedLegalActionSnapshot):
            return OfflineRLActionProposal(blockers=("legal-snapshot-invalid",))
        if not isinstance(stage, str) or not stage:
            return OfflineRLActionProposal(blockers=("stage-invalid",))
        if not self.validated:
            return OfflineRLActionProposal(blockers=("artifact-not-validated",))
        if not self.runtime_enabled:
            return OfflineRLActionProposal(blockers=("runtime-disabled",))
        if snapshot.complete is not True or snapshot.candidate_set_kind != "unified":
            return OfflineRLActionProposal(blockers=("unified-legal-set-incomplete",))

        try:
            model_candidates = normalize_exact_exam_legal_candidates(
                state_before,
                snapshot.legal_candidates,
            )
            raw_scores = score_offline_rl_candidates(
                self.model_path,
                state_before=state_before,
                flow=snapshot.flow_id,
                stage=stage,
                legal_candidates=model_candidates,
                expected_model_sha256=self.model_sha256,
                report_path=self.report_path,
                expected_report_sha256=self.report_sha256,
            )
            scores = tuple(raw_scores)
        except Exception as error:
            return OfflineRLActionProposal(
                blockers=(f"score-failed:{type(error).__name__}",)
            )
        candidates: Sequence[Mapping[str, object]] = snapshot.legal_candidates
        if not scores or len(scores) != len(candidates):
            return OfflineRLActionProposal(blockers=("score-count-mismatch",))
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0.0 <= float(value) <= 1.0
            for value in scores
        ):
            return OfflineRLActionProposal(blockers=("score-invalid",))
        total = math.fsum(float(value) for value in scores)
        if not math.isclose(total, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            return OfflineRLActionProposal(blockers=("score-not-normalized",))
        ranked = sorted(
            range(len(scores)), key=lambda index: (-float(scores[index]), index)
        )
        top = ranked[0]
        if len(ranked) > 1 and math.isclose(
            float(scores[top]),
            float(scores[ranked[1]]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return OfflineRLActionProposal(blockers=("top-action-tied",))
        return OfflineRLActionProposal(
            action_id=snapshot.action_ids[top],
            probability=float(scores[top]),
        )


def build_offline_rl_artifact_selector(
    component: Mapping[str, Any] | None = None,
    *,
    model_path: str | Path | None = None,
    report_path: str | Path | None = None,
    project_root: str | Path = PROJECT_ROOT,
    policy_id: str | None = None,
    runtime_enabled: bool = False,
    expected_model_sha256: str | None = None,
    expected_report_sha256: str | None = None,
) -> OfflineRLArtifactSelector:
    """Build from either one component mapping or explicit artifact paths."""

    if component is not None:
        if model_path is not None or report_path is not None:
            raise ValueError("component cannot be combined with explicit paths")
        if (
            policy_id is not None
            or runtime_enabled is not False
            or expected_model_sha256 is not None
            or expected_report_sha256 is not None
        ):
            raise ValueError("component cannot be combined with path options")
        return OfflineRLArtifactSelector.from_component(
            component, project_root=project_root
        )
    if model_path is None or report_path is None:
        raise ValueError("explicit model_path and report_path are required")
    return OfflineRLArtifactSelector.from_paths(
        model_path,
        report_path,
        policy_id=policy_id,
        runtime_enabled=runtime_enabled,
        expected_model_sha256=expected_model_sha256,
        expected_report_sha256=expected_report_sha256,
    )


# Discovery aliases without a second implementation.
OfflineRLRuntimeAdapter = OfflineRLArtifactSelector
build_offline_rl_runtime_adapter = build_offline_rl_artifact_selector


__all__ = [
    "OfflineRLArtifactSelector",
    "OfflineRLRuntimeAdapter",
    "PROJECT_ROOT",
    "build_offline_rl_artifact_selector",
    "build_offline_rl_runtime_adapter",
]
