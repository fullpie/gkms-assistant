"""Offline promotion and A/B readiness for native-search imitation hints.

The leaderboard card prior is a behavior-only signal.  This module gives it a
separate, flow-scoped promotion gate so a good-looking LOO accuracy cannot
silently become a second simulator.  A promotion candidate must be compared
with the same native legal candidate set and the existing native search on
the same settled state.  The prior may then be used only as an equal-objective
tie-break (or as a search-node ordering hint); it may not add/remove an action,
change a native objective or change a terminal value.

Everything here is read-only.  It consumes JSON artifacts and writes an audit
artifact; it does not create a game/controller session, submit a queue job or
operate a save.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Final

from .leaderboard_card_imitation_prior import (
    LeaderboardCardImitationObservation,
    build_leaderboard_card_imitation_observations,
    evaluate_leaderboard_card_imitation,
)


SCHEMA: Final = "gkms.native-search-imitation-promotion-readiness.v2"
# The machine-readable schema remains v2 for compatibility.  The generated
# report is versioned independently so a consumer can distinguish the v5
# evidence bundle from the historical v2/v3/v4 files without changing the
# schema contract.
READINESS_ARTIFACT_VERSION: Final = "v5"
COMPARISON_SCHEMA: Final = "gkms.native-search-imitation-comparison.v1"
RUNTIME_DIFF_SCHEMA: Final = "gkms.runtime-simulator-diff.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
LEGACY_EPISODES: Final = (
    PROJECT_ROOT
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_native_verified_v1"
    / "episodes.jsonl"
)
LATEST_V5_EPISODES: Final = (
    PROJECT_ROOT
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_background_refresh_v5_incremental_plan1"
    / "episodes.jsonl"
)
# v5 is the current replay corpus.  Callers can still pass any explicit
# episodes path (including LEGACY_EPISODES) to keep older audits reproducible.
DEFAULT_EPISODES: Final = LATEST_V5_EPISODES
DEFAULT_EVALUATION: Final = DEFAULT_EPISODES.with_name("imitation_evaluation.json")
DEFAULT_COMPARISONS: Final = (
    PROJECT_ROOT
    / "var"
    / "nia_training"
    / "native_search_imitation_comparisons.jsonl"
)
LEGACY_OUTPUT: Final = (
    PROJECT_ROOT
    / "var"
    / "nia_training"
    / "native_search_imitation_promotion_readiness.json"
)
DEFAULT_OUTPUT: Final = (
    PROJECT_ROOT
    / "var"
    / "nia_training"
    / "native_search_imitation_promotion_readiness_v5.json"
)
DEFAULT_RUNTIME_LEGAL_CANDIDATE_EVIDENCE: Final = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "runtime_legal_candidate_evidence_130104.json"
)
RUNTIME_LEGAL_CANDIDATE_EVIDENCE_SCHEMA: Final = (
    "gkms.runtime-replay-legal-candidate-evidence.v1"
)
# This is a different input from the v1 observational candidate ingest above.
# It is the dedicated runtime three-family probe whose own legal-set
# ``promotion.allowed`` may be true.  That result is deliberately kept
# separate from the strategy/imitation promotion gate below.
DEFAULT_RUNTIME_LEGAL_VERIFIED_PROBE: Final = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "runtime_legal_verified_probe_132724_v2.json"
)
RUNTIME_LEGAL_VERIFIED_PROBE_SCHEMA: Final = (
    "gkms.runtime-replay-legal-candidate-probe.v1"
)
RUNTIME_LEGAL_VERIFIED_PROBE_SHA256: Final = (
    "929fdecaf776f388f6c6b9131bdc4ff2d72f06984236faafc90f5cfc809482a1"
)
RUNTIME_LEGAL_VERIFIED_PROBE_FLOW: Final = (
    "produce-004|ProducePlanType_Plan2|"
    "ProduceExamEffectType_ExamCardPlayAggressive"
)
RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS: Final = 10

# These are deliberately exact flow keys.  A prior trained on Review must not
# be silently reused for another plan/effect pair.
KNOWN_FLOWS: Final = (
    "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamLessonBuff",
    "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamParameterBuff",
    "produce-004|ProducePlanType_Plan2|ProduceExamEffectType_ExamCardPlayAggressive",
    "produce-004|ProducePlanType_Plan2|ProduceExamEffectType_ExamReview",
    "produce-004|ProducePlanType_Plan3|ProduceExamEffectType_ExamConcentration",
)
ARM_NAMES: Final = ("hand-order", "static", "native-search", "imitation")
NATIVE_SEARCH_SEAMS: Final = {
    "ProducePlanType_Plan1": {
        "available": True,
        "seam": "search_plan1_stage.imitation_tie_breaker",
        "mode": "equal-native-score-stamina-tie-only",
        "legal_set_unchanged": True,
        "terminal_value_unchanged": True,
    },
    "ProducePlanType_Plan2": {
        "available": True,
        "seam": "plan_plan2_native_expectimax.root_action_tiebreaker",
        "mode": "equal-native-expected-value-or-native-node-ordering",
        "legal_set_unchanged": True,
        "terminal_value_unchanged": True,
        "numeric_root_action_bonus_safe_for_imitation": False,
    },
    "ProducePlanType_Plan3": {
        "available": True,
        "seam": "search_plan3_native.imitation_tie_breaker",
        "mode": "equal-native-objective-path-ordering",
        "legal_set_unchanged": True,
        "terminal_value_unchanged": True,
    },
}

# These are official, read-only runtime diff artifacts.  They are deliberately
# mapped to a flow only when the replay identity supplies that flow.  The
# Plan2 reports cover Review; no report is silently re-used for the adjacent
# CardPlayAggressive flow.  A tuple allows multiple independent runtime
# captures for one flow without collapsing their evidence.
DEFAULT_RUNTIME_DIFF_SOURCES: Final[dict[str, tuple[Path, ...]]] = {
    "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamLessonBuff": (
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_simulator_diff_ttmr_3000_plan1_transition_local_current.json",
    ),
    "produce-004|ProducePlanType_Plan1|ProduceExamEffectType_ExamParameterBuff": (
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_simulator_diff_fktn_3001_plan1_transition_local_current.json",
    ),
    "produce-004|ProducePlanType_Plan2|ProduceExamEffectType_ExamReview": (
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_simulator_diff_jsna_plan2_transition_local_current.json",
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_simulator_diff_kllj_plan2_transition_local_current.json",
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_simulator_diff_hrnm_stage1_114744.json",
    ),
    "produce-004|ProducePlanType_Plan2|ProduceExamEffectType_ExamCardPlayAggressive": (
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_simulator_diff_ssmk_3001_plan2_current"
        / "runtime_exam_recorder_shadow_52804.stage-1.transition-local.diff.json",
    ),
    "produce-004|ProducePlanType_Plan3|ProduceExamEffectType_ExamConcentration": (
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_simulator_diff_fktn_3011_plan3_transition_local.json",
    ),
}
# The CardPlayAggressive diff is a transition-local simulator artifact.  Its
# top-level report only carries a compact ``leaderboard_join_status`` marker;
# the strict identity+action join (including candidate count and source
# hashes) is retained in the read-only authority report below.  Keeping this
# map separate from runtime diff sources prevents the authority report from
# being mistaken for another simulator run.
DEFAULT_RUNTIME_AUTHORITY_SOURCES: Final[dict[str, tuple[Path, ...]]] = {
    "produce-004|ProducePlanType_Plan2|ProduceExamEffectType_ExamCardPlayAggressive": (
        PROJECT_ROOT
        / "var"
        / "coverage"
        / "runtime_recorder_action_authority_20260827"
        / "ssmk_52804"
        / "runtime_exam_recorder_shadow_52804.runtime-recorder-action-authority.json",
    ),
}
# Short aliases are useful to audit callers that refer to the evidence as a
# set rather than a source map; both names intentionally point at the same
# immutable-by-convention mapping.
RUNTIME_DIFF_SOURCES: Final = DEFAULT_RUNTIME_DIFF_SOURCES
DEFAULT_RUNTIME_DIFFS: Final = DEFAULT_RUNTIME_DIFF_SOURCES


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _positive_int(value: object, label: str) -> int:
    result = _nonnegative_int(value, label)
    if result < 1:
        raise ValueError(f"{label} must be positive")
    return result


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _display_path(path: Path | str) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _path_record(path: Path | str, *, required: bool = False) -> dict[str, object]:
    resolved = Path(path).resolve()
    present = resolved.is_file()
    if required and not present:
        raise FileNotFoundError(resolved)
    row: dict[str, object] = {
        "path": _display_path(resolved),
        "present": present,
    }
    if present:
        row["size_bytes"] = resolved.stat().st_size
        row["sha256"] = _sha256_path(resolved)
    return row


def _flow_string(value: object, label: str = "flow") -> str:
    if isinstance(value, str):
        parts = tuple(value.split("|"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts = tuple(value)
    else:
        raise ValueError(f"{label} must be a three-field flow")
    if len(parts) != 3 or any(not isinstance(item, str) or not item for item in parts):
        raise ValueError(f"{label} must be a three-field flow")
    return "|".join(parts)


def _plan_type_string(value: object) -> str | None:
    """Canonicalize the numeric/string plan values used by runtime reports."""

    if isinstance(value, str):
        value = value.strip()
        if value.startswith("ProducePlanType_Plan"):
            return value
        if value.isdigit():
            value = int(value)
    if isinstance(value, int) and not isinstance(value, bool):
        # The game enum is Plan1=2, Plan2=3, Plan3=4 in the recorder output.
        return {
            2: "ProducePlanType_Plan1",
            3: "ProducePlanType_Plan2",
            4: "ProducePlanType_Plan3",
        }.get(value)
    return None


def _runtime_diff_flow(
    payload: Mapping[str, object],
    path: Path,
    *,
    expected_flow: str | None = None,
) -> str | None:
    """Return a reviewed flow identity without guessing from zero divergence.

    Runtime simulator diffs currently carry produce/plan identity but not the
    exam effect in their top-level identity object.  When available we use an
    explicit flow field; otherwise we read only the first replay row named by
    the diff.  A caller-supplied flow mapping is authoritative for compact
    manifests and avoids deriving an effect from a filename.
    """

    if expected_flow is not None:
        return _flow_string(expected_flow, "runtime_diff.flow")
    for key in ("flow", "flow_id", "flow_key"):
        if payload.get(key) is not None:
            try:
                return _flow_string(payload[key], f"runtime_diff.{key}")
            except ValueError:
                return None

    identity = payload.get("identity")
    identity_map = identity if isinstance(identity, Mapping) else {}
    expected = identity_map.get("expected")
    observed = identity_map.get("observed")
    expected_map = expected if isinstance(expected, Mapping) else {}
    observed_map = observed if isinstance(observed, Mapping) else {}
    produce_id = expected_map.get("produce_id", observed_map.get("produce_id"))
    plan_type = _plan_type_string(
        expected_map.get("plan_type", observed_map.get("plan_type", payload.get("plan_type")))
    )
    effect_type: object = expected_map.get("exam_effect_type")
    if effect_type is None:
        effect_type = observed_map.get("exam_effect_type")

    replay_source = payload.get("replay_source")
    replay_path: Path | None = None
    if isinstance(replay_source, str) and replay_source.strip():
        replay_path = Path(replay_source).expanduser()
        if not replay_path.is_absolute():
            replay_path = (PROJECT_ROOT / replay_path).resolve()
        else:
            replay_path = replay_path.resolve()
    # Replay JSONL is intentionally sampled, not loaded as a corpus.  This
    # keeps readiness generation bounded even when the v5 corpus is large.
    if replay_path is not None and replay_path.is_file():
        try:
            with replay_path.open("r", encoding="utf-8-sig") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    first = json.loads(line)
                    if isinstance(first, Mapping):
                        produce_id = first.get("produce_id", produce_id)
                        plan_type = _plan_type_string(first.get("plan_type", plan_type))
                        effect_type = first.get("exam_effect_type", effect_type)
                    break
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            pass
    if (
        produce_id is None
        or plan_type is None
        or not isinstance(effect_type, str)
        or not effect_type.strip()
    ):
        return None
    candidate = f"{produce_id}|{plan_type}|{effect_type.strip()}"
    return candidate if candidate in KNOWN_FLOWS else None


def _resolve_payload_path(value: object, *, base: Path) -> Path | None:
    """Resolve a path embedded in a runtime artifact without guessing files."""

    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        # Runtime reports historically used both project-relative paths and
        # paths relative to the report directory.  Prefer an existing report
        # sibling, then the project root; neither branch invents a source.
        sibling = (base.parent / candidate).resolve()
        if sibling.is_file():
            return sibling
        candidate = (PROJECT_ROOT / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def _runtime_source_provenance(
    payload: Mapping[str, object],
    report_path: Path | None,
) -> dict[str, object]:
    """Verify the recorder/source SHA declared by a simulator diff.

    A missing source declaration is represented as ``None`` because compact
    synthetic reports and older diffs may not carry one.  When a declaration
    is present, a missing source or a hash mismatch is an explicit failure;
    callers can therefore distinguish "not supplied" from "tampered".
    """

    raw_source = payload.get("runtime_source")
    source_path = (
        _resolve_payload_path(raw_source, base=report_path)
        if report_path is not None
        else None
    )
    declared = payload.get("runtime_source_sha256")
    declared_text = declared.strip().lower() if isinstance(declared, str) else None
    declared_valid = (
        declared_text is None
        or bool(re.fullmatch(r"[0-9a-f]{64}", declared_text))
    )
    actual: str | None = None
    if source_path is not None and source_path.is_file():
        try:
            actual = _sha256_path(source_path)
        except OSError:
            actual = None
    if declared_text is None and source_path is None:
        verified: bool | None = None
    else:
        verified = bool(
            declared_valid
            and declared_text is not None
            and actual is not None
            and declared_text == actual.lower()
        )
    result: dict[str, object] = {
        "runtime_source": (
            None
            if source_path is None
            else _path_record(source_path)
        ),
        "declared_runtime_source_sha256": declared_text,
        "actual_runtime_source_sha256": actual,
        "runtime_source_sha256_verified": verified,
    }
    return result


def _runtime_diff_verification(
    payload: Mapping[str, object],
    *,
    report_path: Path | None = None,
) -> dict[str, object]:
    """Reduce one runtime diff to conservative promotion evidence.

    A zero local divergence proves only that the captured transitions did not
    diverge under the available simulator projection.  It does not upgrade
    ``exact`` or ``legal_actions_complete`` and therefore can never make the
    legal-set gate pass on its own.
    """

    identity = payload.get("identity")
    identity_map = identity if isinstance(identity, Mapping) else {}
    identity_passed = identity_map.get("passed") is True
    action_stream = payload.get("action_stream")
    action_map = action_stream if isinstance(action_stream, Mapping) else {}
    action_passed = action_map.get("passed") is True
    summary = payload.get("summary")
    summary_map = summary if isinstance(summary, Mapping) else {}

    def _count(*names: str) -> int | None:
        for name in names:
            value = summary_map.get(name)
            if value is None:
                value = action_map.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return None

    runtime_count = _count("runtime_transition_count", "native_action_count")
    replay_count = _count("replay_action_count")
    matched_count = _count("action_match_count")
    supported_count = _count("supported_count", "local_supported_count")
    applied_count = _count("applied_count", "local_applied_count")
    divergence_count = _count("divergence_step_count")
    if divergence_count is None:
        # Some early reports used a first-difference marker without a count.
        divergence_count = 0 if summary_map.get("first_difference") is None else 1

    simulation_started = summary_map.get("simulation_started")
    if simulation_started is None:
        simulation_started = bool(summary_map.get("transition_local"))
    simulation_blocked = summary_map.get("simulation_blocked") is True
    first_difference = summary_map.get("first_difference")
    first_unapplied = summary_map.get("first_unapplied_step")
    first_unsupported = summary_map.get("first_unsupported_step")
    counts_complete = (
        runtime_count is not None
        and replay_count is not None
        and matched_count is not None
        and runtime_count == replay_count == matched_count
        and supported_count is not None
        and 0 <= supported_count <= runtime_count
        and applied_count == runtime_count
    )
    source_provenance = _runtime_source_provenance(payload, report_path)
    source_sha_verified = source_provenance.get("runtime_source_sha256_verified")
    simulator_transition_verified = bool(
        identity_passed
        and action_passed
        and counts_complete
        and divergence_count == 0
        and simulation_started is True
        and not simulation_blocked
        and first_difference is None
        and first_unapplied is None
        # Unsupported runtime families are retained as evidence debt.  They
        # do not invalidate a transition-local comparison when every action
        # was applied and no state divergence was observed.  This is the
        # distinction needed for the official 15/15-applied, 12-supported
        # Plan2 CardPlayAggressive replay diff.
        and source_sha_verified is not False
    )

    # This is intentionally an explicit field check.  In particular, a zero
    # divergence report with legal_actions_complete=false remains unverified.
    legal_actions_complete = payload.get("legal_actions_complete")
    if legal_actions_complete is None:
        legal_actions_complete = summary_map.get("legal_actions_complete")
    legal_set_verified = legal_actions_complete is True

    return {
        "simulator_transition_verified": simulator_transition_verified,
        "legal_set_verified": legal_set_verified,
        "promotion_allowed": False,
        "identity_passed": identity_passed,
        "action_stream_passed": action_passed,
        "runtime_transition_count": runtime_count,
        "replay_action_count": replay_count,
        "action_match_count": matched_count,
        "supported_count": supported_count,
        "applied_count": applied_count,
        "divergence_step_count": divergence_count,
        "exact": payload.get("exact") is True,
        "legal_actions_complete": legal_actions_complete is True,
        "simulation_started": simulation_started is True,
        "simulation_blocked": simulation_blocked,
        "first_difference": first_difference,
        "first_unapplied_step": first_unapplied,
        "first_unsupported_step": first_unsupported,
        **source_provenance,
    }


def _runtime_path_values(value: object, label: str) -> tuple[Path, ...]:
    if isinstance(value, (str, Path)):
        return (Path(value),)
    if isinstance(value, Mapping):
        for key in ("path", "source", "runtime_diff", "report"):
            if key in value:
                return _runtime_path_values(value[key], f"{label}.{key}")
        raise ValueError(f"{label} must contain a runtime diff path")
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ValueError(f"{label} must be a path or path array")
    result = tuple(Path(item) for item in value if isinstance(item, (str, Path)))
    if len(result) != len(value):
        raise ValueError(f"{label} must contain only paths")
    return result


def _authority_item_flow(item: Mapping[str, object]) -> str | None:
    """Project a runtime-recorder authority item to a reviewed flow key."""

    identity = item.get("identity")
    identity_map = identity if isinstance(identity, Mapping) else {}

    def first(*keys: str) -> object:
        for key in keys:
            value = identity_map.get(key)
            if value is None:
                value = item.get(key)
            if value is not None:
                return value
        return None

    produce_id = first("produce_id", "produceId")
    plan_type = _plan_type_string(first("plan_type", "planType"))
    effect = item.get("exam_effect_type")
    effect_map = effect if isinstance(effect, Mapping) else {}
    effect_type = effect_map.get("name") if effect_map else effect
    if not isinstance(effect_type, str) or not effect_type.strip():
        effect_type = first("exam_effect_type", "main_effect_type", "mainEffectType")
    if (
        not isinstance(produce_id, str)
        or not produce_id.strip()
        or plan_type is None
        or not isinstance(effect_type, str)
        or not effect_type.strip()
    ):
        return None
    candidate = f"{produce_id.strip()}|{plan_type}|{effect_type.strip()}"
    return candidate if candidate in KNOWN_FLOWS else None


def _authority_stage_hint(path: Path) -> int | None:
    match = re.search(r"\.stage-(\d+)(?:\.|$)", path.name)
    return int(match.group(1)) if match else None


def _join_provenance(
    join: Mapping[str, object] | None,
    *,
    source_kind: str,
    source_path: Path | None = None,
    authority_stage_index: int | None = None,
    authority_item_index: int | None = None,
) -> dict[str, object]:
    """Normalize a compact or authority-report leaderboard join marker.

    A strict join is a provenance fact, not exact simulator evidence.  The
    returned record therefore always carries ``exact=false`` and
    ``legal_actions_complete=false`` even for a unique match.
    """

    raw = join if isinstance(join, Mapping) else {}
    status = raw.get("status")
    if not isinstance(status, str) or not status.strip():
        status = "unknown"
    else:
        status = status.strip()
    strict_status = raw.get("strict_status")
    if not isinstance(strict_status, str) or not strict_status.strip():
        strict_status = None
    else:
        strict_status = strict_status.strip()
    candidate_count = raw.get("candidate_count")
    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int):
        candidate_count = None
    strict_match = bool(
        status == "matched"
        and strict_status == "unique-match"
        and candidate_count == 1
    )
    result: dict[str, object] = {
        "status": status,
        "strict_status": strict_status,
        "candidate_count": candidate_count,
        "strict_match": strict_match,
        "source_kind": source_kind,
        "exact": False,
        "legal_actions_complete": False,
        "promotion_allowed": False,
    }
    if source_path is not None:
        result["authority_report"] = _path_record(source_path)
    if authority_stage_index is not None:
        result["authority_stage_index"] = authority_stage_index
    if authority_item_index is not None:
        result["authority_item_index"] = authority_item_index
    reason = raw.get("reason")
    if isinstance(reason, str) and reason.strip():
        result["reason"] = reason.strip()
    comparison = raw.get("comparison")
    if isinstance(comparison, Mapping):
        # Keep only immutable join provenance.  The full comparison can be
        # many megabytes and is already retained by the authority report.
        result["comparison_source"] = comparison.get("source")
        auto_match = comparison.get("auto_match")
        if isinstance(auto_match, Mapping):
            result["selection_policy"] = auto_match.get("selection_policy")
            selected = auto_match.get("selected")
            if isinstance(selected, Mapping):
                result["selected_candidate"] = {
                    key: selected.get(key)
                    for key in (
                        "source_ordinal",
                        "trajectory_id",
                        "action_count",
                        "identity_match",
                        "action_stream_match",
                    )
                    if key in selected
                }
    return result


def _payload_join_provenance(payload: Mapping[str, object]) -> dict[str, object] | None:
    """Read join data directly from a diff when the report includes it."""

    direct = payload.get("leaderboard_join")
    join = direct if isinstance(direct, Mapping) else None
    summary = payload.get("summary")
    summary_map = summary if isinstance(summary, Mapping) else {}
    if join is None:
        summary_join = summary_map.get("leaderboard_join")
        join = summary_join if isinstance(summary_join, Mapping) else None
    status = (
        join.get("status")
        if isinstance(join, Mapping)
        else payload.get("leaderboard_join_status", summary_map.get("leaderboard_join_status"))
    )
    if status is None and join is None:
        return None
    marker: dict[str, object] = dict(join) if isinstance(join, Mapping) else {}
    if "status" not in marker:
        marker["status"] = status
    return _join_provenance(marker, source_kind="runtime-diff")


def _authority_join_provenance(
    authority_sources: object,
    flow: str | None,
    diff_path: Path,
) -> dict[str, object] | None:
    """Load the strict join marker from a separate authority report.

    Authority reports can contain multiple recorder stages.  We select only
    the item matching the expected flow and the ``.stage-N`` suffix of the
    supplied diff; if that does not identify exactly one item, the result is
    deliberately unresolved.
    """

    if flow is None or not isinstance(authority_sources, Mapping):
        return None
    raw_paths = authority_sources.get(flow)
    if raw_paths is None:
        return None
    try:
        paths = _runtime_path_values(raw_paths, f"authority.{flow}")
    except (TypeError, ValueError):
        return None
    stage_hint = _authority_stage_hint(diff_path)
    for raw_path in paths:
        path = raw_path.expanduser()
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        else:
            path = path.resolve()
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        raw_items = payload.get("items")
        if isinstance(raw_items, Sequence) and not isinstance(
            raw_items, (str, bytes, bytearray)
        ):
            items = tuple(item for item in raw_items if isinstance(item, Mapping))
        else:
            items = (payload,)
        matches: list[tuple[int, Mapping[str, object]]] = []
        for index, item in enumerate(items):
            if _authority_item_flow(item) != flow:
                continue
            raw_stage = item.get("stage_index")
            item_stage = raw_stage if isinstance(raw_stage, int) and not isinstance(raw_stage, bool) else None
            simulator_report = item.get("simulator_diff_report")
            simulator_path = _resolve_payload_path(simulator_report, base=path)
            if simulator_path is not None and simulator_path == diff_path.resolve():
                matches.append((index, item))
            elif stage_hint is None or item_stage == stage_hint:
                matches.append((index, item))
        if len(matches) != 1:
            continue
        index, item = matches[0]
        raw_join = item.get("leaderboard_join")
        join = raw_join if isinstance(raw_join, Mapping) else None
        if join is None:
            marker: dict[str, object] = {}
            if item.get("leaderboard_join_status") is not None:
                marker["status"] = item.get("leaderboard_join_status")
            join = marker
        return _join_provenance(
            join,
            source_kind="authority-report",
            source_path=path,
            authority_stage_index=(
                item.get("stage_index")
                if isinstance(item.get("stage_index"), int)
                and not isinstance(item.get("stage_index"), bool)
                else None
            ),
            authority_item_index=index,
        )
    return None


def _runtime_join_provenance(
    payload: Mapping[str, object],
    *,
    flow: str | None,
    diff_path: Path,
    authority_sources: object,
) -> dict[str, object] | None:
    direct = _payload_join_provenance(payload)
    authority = _authority_join_provenance(authority_sources, flow, diff_path)
    if direct is None and authority is None:
        return None
    if direct is None:
        return authority
    if authority is None:
        return direct
    # The authority report supplies the strict fields when the diff only has
    # a compact status marker.  If the two artifacts disagree, fail closed.
    if (
        direct.get("status") not in {"unknown", authority.get("status")}
        or (
            direct.get("strict_status") is not None
            and direct.get("strict_status") != authority.get("strict_status")
        )
    ):
        conflict = dict(authority)
        conflict.update(
            {
                "source_kind": "runtime-diff+authority-report-conflict",
                "status": "unresolved",
                "strict_status": "conflict",
                "candidate_count": None,
                "strict_match": False,
                "reason": "runtime diff and authority report join markers disagree",
            }
        )
        return conflict
    merged = dict(authority)
    merged["source_kind"] = "runtime-diff+authority-report"
    return merged


def _runtime_payload(path: Path) -> tuple[Mapping[str, object], ...]:
    """Load direct diff JSON or expand a batch's linked simulator reports."""

    if not path.is_file():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ()
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return tuple(item for item in payload if isinstance(item, Mapping))
    if not isinstance(payload, Mapping):
        return ()
    items = payload.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return (payload,)
    expanded: list[Mapping[str, object]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        linked = item.get("simulator_diff_report")
        if isinstance(linked, str) and linked.strip():
            linked_path = Path(linked)
            if not linked_path.is_absolute():
                linked_path = (path.parent / linked_path).resolve()
            linked_payload = _runtime_payload(linked_path)
            if linked_payload:
                expanded.extend(linked_payload)
                continue
        # A compact batch item may itself carry the summary.  Retain it so a
        # malformed link is recorded as evidence debt instead of disappearing.
        expanded.append(item)
    return tuple(expanded)


def _runtime_legal_candidate_evidence(
    source: str | Path | None = DEFAULT_RUNTIME_LEGAL_CANDIDATE_EVIDENCE,
) -> dict[str, object]:
    """Read the observational candidate ingest without opening a game.

    This is intentionally a reporting-only join.  The candidate ingest can
    resolve observation debt (presence, ordered identity, purity and chosen
    action binding), but its ``legal_set_verified``/``exact``/promotion flags
    are required to remain false and are never copied into the native-search
    gate as true.
    """

    if source is None:
        return {
            "source_count": 0,
            "sources": [],
            "valid": False,
            "flow": None,
            "evidence": None,
            "reason": "runtime-legal-candidate-evidence-disabled",
        }
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    else:
        path = path.resolve()
    source_record: dict[str, object] = {
        **_path_record(path),
        "kind": "runtime-legal-candidate-evidence",
        "schema": RUNTIME_LEGAL_CANDIDATE_EVIDENCE_SCHEMA,
        "valid": False,
    }
    if not path.is_file():
        source_record["reason"] = "missing-runtime-legal-candidate-evidence"
        return {
            "source_count": 1,
            "sources": [source_record],
            "valid": False,
            "flow": None,
            "evidence": None,
        }
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8-sig"),
            parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        source_record["reason"] = f"invalid-runtime-legal-candidate-evidence:{type(error).__name__}"
        return {
            "source_count": 1,
            "sources": [source_record],
            "valid": False,
            "flow": None,
            "evidence": None,
        }
    if not isinstance(payload, Mapping):
        source_record["reason"] = "runtime-legal-candidate-evidence-not-object"
        return {
            "source_count": 1,
            "sources": [source_record],
            "valid": False,
            "flow": None,
            "evidence": None,
        }
    summary = payload.get("summary")
    summary_map = summary if isinstance(summary, Mapping) else {}
    flow = payload.get("flow") if isinstance(payload.get("flow"), str) else None
    safe_observational = {
        "native_state_count": summary_map.get("native_state_count", 0),
        "transition_count": summary_map.get("transition_count", 0),
        "observed_hand_identity_count": summary_map.get(
            "observed_hand_identity_count", 0
        ),
        "candidate_presence_count": summary_map.get("candidate_presence_count", 0),
        "candidate_presence_complete": summary_map.get(
            "candidate_presence_complete", False
        ),
        "hand_observed_candidates_presence_count": summary_map.get(
            "hand_observed_candidates_presence_count", 0
        ),
        "hand_observed_candidates_presence_complete": summary_map.get(
            "hand_observed_candidates_presence_complete", False
        ),
        "enumeration_purity_equal_count": summary_map.get(
            "enumeration_purity_equal_count", 0
        ),
        "enumeration_purity_equal_complete": summary_map.get(
            "enumeration_purity_equal_complete", False
        ),
        "official_chosen_action_count": summary_map.get(
            "official_chosen_action_count", 0
        ),
        "official_chosen_action_complete": summary_map.get(
            "official_chosen_action_complete", False
        ),
        "evidence_debt": payload.get("evidence_debt"),
        # These values are explicit constants at this gate boundary.  Do not
        # trust a malformed candidate artifact to upgrade readiness.
        "legal_set_verified": False,
        "legal_actions_complete": False,
        "exact": False,
        "promotion_allowed": False,
    }
    schema_valid = payload.get("schema") == RUNTIME_LEGAL_CANDIDATE_EVIDENCE_SCHEMA
    passed = payload.get("passed") is True
    safety_valid = all(
        summary_map.get(field) is False
        for field in ("legal_set_verified", "legal_actions_complete", "exact")
    )
    promotion = payload.get("promotion")
    if isinstance(promotion, Mapping):
        safety_valid = safety_valid and promotion.get("allowed") is False
    source_record["schema"] = payload.get("schema")
    source_record["flow"] = flow
    source_record["valid"] = schema_valid and passed and safety_valid
    if source_record["valid"] is not True:
        source_record["reason"] = "candidate-evidence-safety-or-schema-failed"
    return {
        "source_count": 1,
        "sources": [source_record],
        "valid": source_record["valid"],
        "flow": flow,
        "evidence": safe_observational if source_record["valid"] is True else None,
    }


def _runtime_legal_verified_probe(
    source: str | Path | Mapping[str, object] | None = DEFAULT_RUNTIME_LEGAL_VERIFIED_PROBE,
) -> dict[str, object]:
    """Read the dedicated three-family legal-set probe, fail closed.

    ``runtime_legal_candidate_evidence`` is intentionally observational and
    must never turn a getter-only candidate list into a legal set.  The v2
    verified probe is a separate, explicitly authoritative runtime artifact:
    it owns only the legal-action-set decision for its one bound flow.  Its
    ``promotion.allowed`` therefore means *DLL legal-set promotion*, not
    native-search/imitation strategy promotion.

    The check is deliberately structural and read-only.  It does not load the
    candidate DLL, read process memory, start a game, or consult a simulator.
    The checked-in capture is hash pinned so a changed default source cannot
    silently become readiness evidence.
    """

    def _empty(reason: str, *, source_record: Mapping[str, object] | None = None) -> dict[str, object]:
        record = dict(source_record or {})
        record.setdefault("kind", "runtime-legal-verified-probe")
        record.setdefault("schema", RUNTIME_LEGAL_VERIFIED_PROBE_SCHEMA)
        record["valid"] = False
        record["reason"] = reason
        return {
            "source_count": 0 if source is None else 1,
            "sources": [record] if source is not None else [],
            "valid": False,
            "flow": RUNTIME_LEGAL_VERIFIED_PROBE_FLOW,
            "evidence": None,
            "legal_set_verified": False,
            "legal_actions_complete": False,
            "exact": False,
            "dll_legal_set_promotion_allowed": False,
            "strategy_promotion_allowed": False,
        }

    if source is None:
        return _empty("runtime-legal-verified-probe-disabled")

    payload: Mapping[str, object] | None = None
    path: Path | None = None
    source_record: dict[str, object] = {
        "kind": "runtime-legal-verified-probe",
        "schema": RUNTIME_LEGAL_VERIFIED_PROBE_SCHEMA,
        "valid": False,
    }
    if isinstance(source, Mapping):
        # A parsed report is useful for pure callers/tests.  Also accept the
        # same flow-to-source wrapper used by the runtime diff reader.
        if isinstance(source.get("schema"), str):
            payload = source
            source_record["path"] = None
            source_record["present"] = True
            source_record["sha256"] = None
            source_record["sha256_verified"] = None
        else:
            raw = source.get(RUNTIME_LEGAL_VERIFIED_PROBE_FLOW)
            if isinstance(raw, (str, Path)):
                path = Path(raw).expanduser()
            else:
                return _empty("runtime-legal-verified-probe-source-wrapper-invalid")
    elif isinstance(source, (str, Path)):
        path = Path(source).expanduser()
    else:
        return _empty("runtime-legal-verified-probe-source-invalid")

    if path is not None:
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        else:
            path = path.resolve()
        source_record = {
            **_path_record(path),
            "kind": "runtime-legal-verified-probe",
            "schema": RUNTIME_LEGAL_VERIFIED_PROBE_SCHEMA,
            "expected_sha256": RUNTIME_LEGAL_VERIFIED_PROBE_SHA256,
            "sha256_verified": False,
            "valid": False,
        }
        if not path.is_file():
            return _empty("missing-runtime-legal-verified-probe", source_record=source_record)
        actual_sha = source_record.get("sha256")
        source_record["sha256_verified"] = (
            isinstance(actual_sha, str)
            and actual_sha.lower() == RUNTIME_LEGAL_VERIFIED_PROBE_SHA256
        )
        if source_record["sha256_verified"] is not True:
            return _empty("runtime-legal-verified-probe-sha256-mismatch", source_record=source_record)
        try:
            loaded = json.loads(
                path.read_text(encoding="utf-8-sig"),
                parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)),
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            return _empty(
                f"invalid-runtime-legal-verified-probe:{type(error).__name__}",
                source_record=source_record,
            )
        if not isinstance(loaded, Mapping):
            return _empty("runtime-legal-verified-probe-not-object", source_record=source_record)
        payload = loaded

    if payload is None:
        return _empty("runtime-legal-verified-probe-payload-missing", source_record=source_record)

    # The source report is a completed replay capture for exactly one
    # Plan2/CardPlayAggressive flow.  Its top-level and nested fields are
    # checked independently; copying a single optimistic flag cannot pass.
    issues: list[str] = []

    def require(condition: bool, reason: str) -> None:
        if not condition:
            issues.append(reason)

    require(payload.get("schema") == RUNTIME_LEGAL_VERIFIED_PROBE_SCHEMA, "schema-mismatch")
    require(payload.get("status") in (None, "complete"), "status-not-complete")
    require(payload.get("started") is True, "probe-not-started")
    require(payload.get("complete") is True, "probe-not-complete")
    require(payload.get("timed_out") is not True, "probe-timed-out")
    require(payload.get("passed") is True, "probe-passed-false")
    require(payload.get("transition_count") == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS, "transition-count")
    require(payload.get("legal_actions_complete") is True, "probe-legal-actions-incomplete")
    require(payload.get("exact") is True, "probe-exact-false")
    require(payload.get("candidate_actions_authoritative") is True, "probe-actions-not-authoritative")
    require(payload.get("legality_inferred") is False, "probe-legality-inferred")
    require(payload.get("hash_manifest_passed") is True, "hash-manifest-not-passed")

    stage = payload.get("stage")
    stage_map = stage if isinstance(stage, Mapping) else {}
    require(stage_map.get("started") is True, "stage-not-started")
    require(stage_map.get("complete") is True, "stage-not-complete")
    require(stage_map.get("source_mode") == "replay", "stage-source-mode")
    require(stage_map.get("passed") is True, "stage-passed-false")
    require(
        stage_map.get("transition_count") == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS,
        "stage-transition-count",
    )
    stage_identity = stage_map.get("identity")
    identity_map = stage_identity if isinstance(stage_identity, Mapping) else {}
    require(identity_map.get("produceId") == "produce-004", "flow-produce-id")
    require(identity_map.get("phase") == 6, "flow-phase")
    stage_orders = stage_map.get("action_orders")
    stage_order_map = stage_orders if isinstance(stage_orders, Mapping) else {}
    require(stage_order_map.get("passed") is True, "stage-action-orders")
    require(
        stage_order_map.get("values") == list(range(1, RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS + 1)),
        "stage-action-order-values",
    )
    require(stage_map.get("turn1", {}).get("passed") is True if isinstance(stage_map.get("turn1"), Mapping) else False, "stage-turn1")
    require(stage_map.get("state_capture", {}).get("passed") is True if isinstance(stage_map.get("state_capture"), Mapping) else False, "stage-state-capture")
    require(stage_map.get("adjacent_canonical_continuity", {}).get("passed") is True if isinstance(stage_map.get("adjacent_canonical_continuity"), Mapping) else False, "stage-continuity")
    require(stage_map.get("terminal", {}).get("passed") is True if isinstance(stage_map.get("terminal"), Mapping) else False, "stage-terminal")

    # The same three-family runtime object is present under both names in the
    # retained artifact.  Prefer the explicit runtime name, but accept the
    # legacy nested alias for future compact captures.
    runtime_probe = payload.get("runtime_legal_action_probe")
    if not isinstance(runtime_probe, Mapping):
        runtime_probe = payload.get("legal_candidate_probe")
    runtime_map = runtime_probe if isinstance(runtime_probe, Mapping) else {}
    require(runtime_map.get("schema") == "gkms.runtime-exam-main-action-runtime-probe.v1", "runtime-probe-schema")
    require(runtime_map.get("passed") is True, "runtime-probe-passed-false")
    require(runtime_map.get("transition_count") == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS, "runtime-transition-count")
    require(runtime_map.get("legal_actions_complete") is True, "runtime-legal-actions-incomplete")
    require(runtime_map.get("exact") is True, "runtime-exact-false")
    require(runtime_map.get("candidate_actions_authoritative") is True, "runtime-actions-not-authoritative")
    require(runtime_map.get("legality_inferred") is False, "runtime-legality-inferred")

    runtime_promotion = runtime_map.get("promotion")
    runtime_promotion_map = runtime_promotion if isinstance(runtime_promotion, Mapping) else {}
    for field in ("allowed", "family_verified", "hand_verified", "drink_verified", "end_turn_verified", "settlement_verified", "exact", "legal_actions_complete"):
        require(runtime_promotion_map.get(field) is True, f"runtime-promotion-{field}")
    top_promotion = payload.get("promotion")
    top_promotion_map = top_promotion if isinstance(top_promotion, Mapping) else {}
    for field in ("allowed", "family_verified", "hand_verified", "drink_verified", "end_turn_verified", "settlement_verified", "exact", "legal_actions_complete"):
        require(top_promotion_map.get(field) is True, f"probe-promotion-{field}")

    family_raw = runtime_map.get("family_promotion_eligibility")
    family_map = family_raw if isinstance(family_raw, Mapping) else {}
    family_summary: dict[str, dict[str, object]] = {}
    for family in ("hand", "drink", "end_turn"):
        value = family_map.get(family)
        item = value if isinstance(value, Mapping) else {}
        require(item.get("family_verified") is True, f"{family}-family-unverified")
        require(item.get("promotion_eligible") is True, f"{family}-family-not-eligible")
        require(item.get("complete") is True, f"{family}-family-incomplete")
        require(item.get("runtime_evidence_steps") == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS, f"{family}-runtime-steps")
        require(item.get("verified_steps") == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS, f"{family}-verified-steps")
        family_summary[family] = {
            key: item.get(key)
            for key in (
                "family",
                "runtime_evidence_steps",
                "verified_steps",
                "family_verified",
                "promotion_eligible",
                "complete",
                "blockers",
            )
        }

    settlement = runtime_map.get("settlement")
    settlement_map = settlement if isinstance(settlement, Mapping) else {}
    require(settlement_map.get("complete") is True, "settlement-incomplete")
    require(settlement_map.get("terminal_once_last") is True, "settlement-terminal")
    consistency = runtime_map.get("action_set_consistency")
    consistency_map = consistency if isinstance(consistency, Mapping) else {}
    require(consistency_map.get("complete") is True, "action-set-consistency")

    memberships = runtime_map.get("official_chosen_membership")
    if not isinstance(memberships, list):
        memberships = payload.get("official_chosen_membership")
    membership_rows = memberships if isinstance(memberships, list) else []
    require(len(membership_rows) == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS, "official-membership-count")
    membership_ok = bool(membership_rows) and all(
        isinstance(item, Mapping)
        and item.get("known") is True
        and item.get("member") is True
        for item in membership_rows
    )
    require(membership_ok, "official-membership-not-all-member")

    legal_actions = runtime_map.get("legal_actions")
    authoritative_actions = runtime_map.get("authoritative_legal_actions")
    if not isinstance(legal_actions, list):
        legal_actions = payload.get("legal_actions")
    if not isinstance(authoritative_actions, list):
        authoritative_actions = payload.get("authoritative_legal_actions")
    require(isinstance(legal_actions, list) and len(legal_actions) == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS, "legal-actions-count")
    require(isinstance(authoritative_actions, list) and len(authoritative_actions) == RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS, "authoritative-actions-count")
    require(
        isinstance(legal_actions, list)
        and isinstance(authoritative_actions, list)
        and legal_actions == authoritative_actions,
        "legal-actions-authority-mismatch",
    )
    legal_action_counts = [
        len(item) if isinstance(item, list) else None
        for item in legal_actions
    ] if isinstance(legal_actions, list) else []
    require(all(isinstance(value, int) and value >= 0 for value in legal_action_counts), "legal-actions-step-invalid")

    # The source report carries a read-only hash manifest.  These checks make
    # its operational boundary explicit in the readiness artifact too.
    manifest = payload.get("hash_manifest")
    manifest_map = manifest if isinstance(manifest, Mapping) else payload.get("manifest")
    manifest_map = manifest_map if isinstance(manifest_map, Mapping) else {}
    require(manifest_map.get("passed") is True, "manifest-passed-false")
    require(manifest_map.get("read_only") is True, "manifest-not-read-only")
    for field in ("deployment_performed", "deployment_allowed", "game_started", "controller_access"):
        require(manifest_map.get(field) is False, f"manifest-{field}")
    require(manifest_map.get("blockers") in (None, []), "manifest-blockers")

    source_record["schema"] = payload.get("schema")
    source_record["flow"] = RUNTIME_LEGAL_VERIFIED_PROBE_FLOW
    source_record["transition_count"] = payload.get("transition_count")
    source_record["candidate_dll_sha256"] = payload.get("candidate_dll_sha256")
    source_record["candidate_source_sha256"] = payload.get("candidate_source_sha256")
    source_record["observer_restore_sha256"] = payload.get("observer_restore_sha256")
    source_record["valid"] = not issues
    if issues:
        source_record["reason"] = "runtime-legal-verified-probe-validation-failed"
        source_record["issues"] = issues[:32]
        return _empty("runtime-legal-verified-probe-validation-failed", source_record=source_record)

    dll_promotion = {
        key: top_promotion_map.get(key)
        for key in (
            "allowed",
            "status",
            "family_verified",
            "hand_verified",
            "drink_verified",
            "end_turn_verified",
            "settlement_verified",
            "exact",
            "legal_actions_complete",
            "reason",
        )
    }
    evidence: dict[str, object] = {
        "flow": RUNTIME_LEGAL_VERIFIED_PROBE_FLOW,
        "schema": payload.get("schema"),
        "transition_count": RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS,
        "probe_sha256": source_record.get("sha256"),
        "probe_sha256_verified": source_record.get("sha256_verified"),
        "hand": family_summary["hand"],
        "drink": family_summary["drink"],
        "end_turn": family_summary["end_turn"],
        "family_verified": family_summary,
        "family_verified_steps": {
            family: {
                "verified": family_summary[family].get("verified_steps"),
                "total": RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS,
            }
            for family in family_summary
        },
        "settlement": {
            "complete": settlement_map.get("complete"),
            "terminal_once_last": settlement_map.get("terminal_once_last"),
        },
        "official_chosen_membership": {
            "member_count": sum(
                isinstance(item, Mapping) and item.get("member") is True
                for item in membership_rows
            ),
            "total": len(membership_rows),
            "complete": membership_ok,
        },
        "legal_action_counts": legal_action_counts,
        "legal_actions_complete": True,
        "exact": True,
        "passed": True,
        "legal_set_verified": True,
        # This is the runtime/DLL legal-set decision only.  It is not the
        # strategy promotion decision produced by ``_flow_gate``.
        "dll_legal_set_promotion": dll_promotion,
        "dll_legal_set_promotion_allowed": True,
        "strategy_promotion": {
            "allowed": False,
            "default_enabled": False,
            "reason": "native-search/imitation LOO and same-state safety evidence are separate gates",
        },
    }
    source_record["promotion"] = dll_promotion
    source_record["legal_set_verified"] = True
    source_record["legal_actions_complete"] = True
    source_record["exact"] = True
    source_record["passed"] = True
    return {
        "source_count": 1,
        "sources": [source_record],
        "valid": True,
        "flow": RUNTIME_LEGAL_VERIFIED_PROBE_FLOW,
        "evidence": evidence,
        "legal_set_verified": True,
        "legal_actions_complete": True,
        "exact": True,
        "dll_legal_set_promotion_allowed": True,
        "strategy_promotion_allowed": False,
    }


def _merge_runtime_legal_verified_probe(
    runtime_evidence: Mapping[str, object] | None,
    verified_probe: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Join legal-set evidence into one flow without joining strategy gates."""

    if runtime_evidence is None and verified_probe is None:
        return None
    merged: dict[str, object] = dict(runtime_evidence or {})
    if not isinstance(verified_probe, Mapping) or verified_probe.get("valid") is not True:
        return merged
    flow = verified_probe.get("flow")
    evidence = verified_probe.get("evidence")
    merged.update(
        {
            "legal_set_verified": True,
            "legal_actions_complete": True,
            "legal_set_exact": True,
            "legal_set_source": "runtime-legal-verified-probe",
            "runtime_legal_verified_probe": dict(evidence)
            if isinstance(evidence, Mapping)
            else None,
            "runtime_legal_verified_probe_source_count": verified_probe.get("source_count", 0),
            "dll_legal_set_promotion": (
                dict(evidence.get("dll_legal_set_promotion"))
                if isinstance(evidence, Mapping)
                and isinstance(evidence.get("dll_legal_set_promotion"), Mapping)
                else {"allowed": True}
            ),
            "dll_legal_set_promotion_allowed": True,
            # Never inherit the DLL's legal-set ``promotion.allowed`` as the
            # strategy promotion result.  _flow_gate owns that decision.
            "promotion_allowed": False,
        }
    )
    if isinstance(flow, str):
        merged["legal_set_flow"] = flow
    return merged


def _runtime_diff_evidence(
    source: object,
    *,
    authority_sources: object = DEFAULT_RUNTIME_AUTHORITY_SOURCES,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Read runtime diff artifacts and return flow summaries plus provenance."""

    requested: list[tuple[str | None, Path]] = []
    if isinstance(source, Mapping):
        # Permit a manifest wrapper in addition to flow -> paths mappings.
        nested = source.get("runtime_diffs", source.get("sources"))
        if isinstance(nested, (Mapping, Sequence)) and not isinstance(
            nested, (str, bytes, bytearray)
        ):
            source = nested
        if not isinstance(source, Mapping):
            return _runtime_diff_evidence(source, authority_sources=authority_sources)
        for raw_flow, raw_paths in source.items():
            if not isinstance(raw_flow, str):
                continue
            try:
                flow = _flow_string(raw_flow, "runtime_diff.flow")
                paths = _runtime_path_values(raw_paths, f"runtime_diff.{raw_flow}")
            except (TypeError, ValueError):
                continue
            requested.extend((flow, Path(path)) for path in paths)
    elif isinstance(source, (str, Path)):
        requested.append((None, Path(source)))
    elif isinstance(source, Sequence) and not isinstance(source, (bytes, bytearray)):
        for index, raw_path in enumerate(source):
            try:
                expected_flow: str | None = None
                if isinstance(raw_path, Mapping) and raw_path.get("flow") is not None:
                    expected_flow = _flow_string(
                        raw_path["flow"], f"runtime_diff[{index}].flow"
                    )
                requested.append(
                    (
                        expected_flow,
                        _runtime_path_values(raw_path, f"runtime_diff[{index}]")[0],
                    )
                )
            except (TypeError, ValueError):
                continue

    grouped: defaultdict[str, list[dict[str, object]]] = defaultdict(list)
    source_records: list[dict[str, object]] = []
    for expected_flow, raw_path in requested:
        path = raw_path.expanduser()
        if not path.is_absolute():
            path = (PROJECT_ROOT / path).resolve()
        else:
            path = path.resolve()
        payloads = _runtime_payload(path)
        if not payloads:
            source_records.append(
                {
                    **_path_record(path),
                    "kind": "runtime-simulator-diff",
                    "flow": expected_flow,
                    "valid": False,
                    "reason": "missing-or-invalid-runtime-diff",
                }
            )
            continue
        for payload in payloads:
            flow = _runtime_diff_flow(payload, path, expected_flow=expected_flow)
            verification = _runtime_diff_verification(payload, report_path=path)
            join_provenance = _runtime_join_provenance(
                payload,
                flow=flow,
                diff_path=path,
                authority_sources=authority_sources,
            )
            if join_provenance is not None:
                verification["leaderboard_join_provenance"] = join_provenance
            record: dict[str, object] = {
                **_path_record(path),
                "kind": "runtime-simulator-diff",
                "flow": flow,
                "schema": payload.get("schema"),
                "valid": payload.get("schema") == RUNTIME_DIFF_SCHEMA,
                "evidence": verification,
            }
            if join_provenance is not None:
                record["leaderboard_join_provenance"] = join_provenance
            if flow is None:
                record["valid"] = False
                record["reason"] = "runtime-diff-flow-unresolved"
            if record["valid"] is not True:
                record.setdefault("reason", "unsupported-runtime-diff-schema")
            source_records.append(record)
            if flow is not None:
                grouped[flow].append(record)

    by_flow: dict[str, dict[str, object]] = {}
    for flow, records in sorted(grouped.items()):
        evidence = [
            record.get("evidence", {})
            for record in records
            if isinstance(record.get("evidence"), Mapping)
        ]
        records_valid = bool(records) and all(record.get("valid") is True for record in records)
        verified = records_valid and bool(evidence) and all(
            item.get("simulator_transition_verified") is True for item in evidence
        )
        legal = records_valid and bool(evidence) and all(
            item.get("legal_set_verified") is True for item in evidence
        )
        by_flow[flow] = {
            "source_count": len(records),
            "sources": [dict(record) for record in records],
            "simulator_transition_verified": verified,
            "legal_set_verified": legal,
            "promotion_allowed": False,
            "runtime_transition_count": sum(
                int(item["runtime_transition_count"])
                for item in evidence
                if isinstance(item.get("runtime_transition_count"), int)
            ),
            "transition_counts": [
                item.get("runtime_transition_count")
                for item in evidence
                if isinstance(item.get("runtime_transition_count"), int)
            ],
            "replay_action_count": sum(
                int(item["replay_action_count"])
                for item in evidence
                if isinstance(item.get("replay_action_count"), int)
            ),
            "supported_count": sum(
                int(item["supported_count"])
                for item in evidence
                if isinstance(item.get("supported_count"), int)
            ),
            "applied_count": sum(
                int(item["applied_count"])
                for item in evidence
                if isinstance(item.get("applied_count"), int)
            ),
            "action_match_counts": [
                item.get("action_match_count")
                for item in evidence
                if isinstance(item.get("action_match_count"), int)
            ],
            "divergence_step_count": sum(
                int(item.get("divergence_step_count", 0) or 0)
                for item in evidence
                if isinstance(item.get("divergence_step_count", 0), int)
            ),
            "runtime_source_sha256_verified": (
                None
                if not any(
                    item.get("runtime_source_sha256_verified") is not None
                    for item in evidence
                )
                else all(
                    item.get("runtime_source_sha256_verified") is True
                    for item in evidence
                    if item.get("runtime_source_sha256_verified") is not None
                )
            ),
            "exact": all(item.get("exact") is True for item in evidence),
            "legal_actions_complete": legal,
            "leaderboard_join_provenance": [
                item.get("leaderboard_join_provenance")
                for item in records
                if isinstance(item.get("leaderboard_join_provenance"), Mapping)
            ],
            "strict_leaderboard_join_verified": bool(evidence)
            and all(
                isinstance(item.get("leaderboard_join_provenance"), Mapping)
                and item["leaderboard_join_provenance"].get("strict_match") is True
                for item in records
            ),
        }

    return by_flow, {
        "schema": RUNTIME_DIFF_SCHEMA,
        "source_count": len(source_records),
        "valid_source_count": sum(record.get("valid") is True for record in source_records),
        "invalid_source_count": sum(record.get("valid") is not True for record in source_records),
        "sources": source_records,
        "by_flow": by_flow,
        "authority_sources": [
            _path_record(path)
            for raw_paths in authority_sources.values()
            if isinstance(authority_sources, Mapping)
            for path in _runtime_path_values(raw_paths, "authority-source")
        ]
        if isinstance(authority_sources, Mapping)
        else [],
    }


def _string_tuple(value: object, label: str, *, unique: bool = True) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be an array")
    result = tuple(_text(item, f"{label}[]") for item in value)
    if not result:
        raise ValueError(f"{label} must not be empty")
    if unique and len(result) != len(set(result)):
        raise ValueError(f"{label} must be unique")
    return result


def _objective(value: object, label: str) -> tuple[float, ...]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (_finite_number(value, label),)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{label} must be a number or number tuple")
    result = tuple(_finite_number(item, f"{label}[]") for item in value)
    if not result:
        raise ValueError(f"{label} must not be empty")
    return result


def _objective_map(value: object, candidates: tuple[str, ...], label: str) -> dict[str, tuple[float, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object containing every candidate")
    result: dict[str, tuple[float, ...]] = {}
    for card_id in candidates:
        if card_id not in value:
            raise ValueError(f"{label} is missing candidate {card_id}")
        result[card_id] = _objective(value[card_id], f"{label}.{card_id}")
    if set(value) != set(candidates):
        raise ValueError(f"{label} contains an out-of-set candidate")
    widths = {len(item) for item in result.values()}
    if len(widths) != 1:
        raise ValueError(f"{label} objective vectors must have equal width")
    return result


def _number_map(value: object, candidates: tuple[str, ...], label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object containing every candidate")
    result = {
        card_id: _finite_number(value[card_id], f"{label}.{card_id}")
        for card_id in candidates
        if card_id in value
    }
    if set(result) != set(candidates) or set(value) != set(candidates):
        raise ValueError(f"{label} must contain exactly every candidate")
    return result


def _action(value: object, label: str, candidates: tuple[str, ...]) -> str:
    result = _text(value, label)
    if result not in candidates:
        raise ValueError(f"{label} is outside the native legal candidate set")
    return result


@dataclass(frozen=True, slots=True)
class NativeSearchComparison:
    """One same-state comparison of the four offline policy arms.

    ``legal_candidates`` is the exact set published by the native planner.
    ``imitation_legal_candidates`` is intentionally separate: omitting it
    does not prove parity and therefore cannot pass the promotion gate.
    ``native_objective_by_action`` and ``terminal_value_by_action`` are native
    outputs, not values guessed by the imitation prior.
    """

    row_id: str
    flow: str
    trajectory_id: str
    legal_candidates: tuple[str, ...]
    native_search_action: str
    imitation_action: str | None = None
    static_action: str | None = None
    observed_action: str | None = None
    imitation_legal_candidates: tuple[str, ...] | None = None
    native_objective_by_action: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    terminal_value_by_action: Mapping[str, float] = field(default_factory=dict)
    state_digest: str | None = None
    schema: str = COMPARISON_SCHEMA

    def __post_init__(self) -> None:
        _text(self.row_id, "comparison.row_id")
        flow = _flow_string(self.flow, "comparison.flow")
        object.__setattr__(self, "flow", flow)
        _text(self.trajectory_id, "comparison.trajectory_id")
        candidates = _string_tuple(self.legal_candidates, "comparison.legal_candidates")
        object.__setattr__(self, "legal_candidates", candidates)
        native = _action(self.native_search_action, "comparison.native_search_action", candidates)
        object.__setattr__(self, "native_search_action", native)
        for name in ("imitation_action", "static_action", "observed_action"):
            value = getattr(self, name)
            if value is not None:
                value = _action(value, f"comparison.{name}", candidates)
                object.__setattr__(self, name, value)
        if self.imitation_legal_candidates is not None:
            treatment_candidates = _string_tuple(
                self.imitation_legal_candidates,
                "comparison.imitation_legal_candidates",
            )
            object.__setattr__(self, "imitation_legal_candidates", treatment_candidates)
        objectives = _objective_map(
            self.native_objective_by_action,
            candidates,
            "comparison.native_objective_by_action",
        )
        object.__setattr__(self, "native_objective_by_action", objectives)
        terminal = _number_map(
            self.terminal_value_by_action,
            candidates,
            "comparison.terminal_value_by_action",
        )
        object.__setattr__(self, "terminal_value_by_action", terminal)
        if self.state_digest is not None:
            _text(self.state_digest, "comparison.state_digest")
        if self.schema != COMPARISON_SCHEMA:
            raise ValueError("unsupported native search comparison schema")

    @property
    def hand_order_action(self) -> str:
        return self.legal_candidates[0]

    @property
    def static_card_id_order_action(self) -> str:
        return min(self.legal_candidates)

    @property
    def legal_set_preserved(self) -> bool:
        return (
            self.imitation_legal_candidates is not None
            and self.imitation_legal_candidates == self.legal_candidates
        )

    @property
    def objective_tied(self) -> bool | None:
        if self.imitation_action is None:
            return None
        return (
            self.native_objective_by_action[self.imitation_action]
            == self.native_objective_by_action[self.native_search_action]
        )

    @property
    def objective_non_regressing(self) -> bool | None:
        if self.imitation_action is None:
            return None
        return (
            self.native_objective_by_action[self.imitation_action]
            >= self.native_objective_by_action[self.native_search_action]
        )

    @property
    def terminal_value_preserved(self) -> bool | None:
        if self.imitation_action is None:
            return None
        return math.isclose(
            self.terminal_value_by_action[self.imitation_action],
            self.terminal_value_by_action[self.native_search_action],
            rel_tol=0.0,
            abs_tol=0.0,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "row_id": self.row_id,
            "flow": self.flow,
            "trajectory_id": self.trajectory_id,
            "candidate_set_kind": "legal",
            "legal_candidates": list(self.legal_candidates),
            "imitation_legal_candidates": (
                None
                if self.imitation_legal_candidates is None
                else list(self.imitation_legal_candidates)
            ),
            "hand_order_action": self.hand_order_action,
            "static_action": self.static_action,
            "static_card_id_order_action": self.static_card_id_order_action,
            "native_search_action": self.native_search_action,
            "imitation_action": self.imitation_action,
            "observed_action": self.observed_action,
            "native_objective_by_action": {
                key: list(value) for key, value in self.native_objective_by_action.items()
            },
            "terminal_value_by_action": dict(self.terminal_value_by_action),
            "state_digest": self.state_digest,
            "legal_set_preserved": self.legal_set_preserved,
            "objective_tied": self.objective_tied,
            "objective_non_regressing": self.objective_non_regressing,
            "terminal_value_preserved": self.terminal_value_preserved,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object], index: int = 0) -> "NativeSearchComparison":
        if raw.get("schema", COMPARISON_SCHEMA) != COMPARISON_SCHEMA:
            raise ValueError(f"comparison row {index} has unsupported schema")
        candidates = raw.get("legal_candidates", raw.get("candidate_ids"))
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
            raise ValueError(f"comparison row {index} has no legal candidates")
        flow = raw.get("flow")
        if flow is None:
            flow = (
                raw.get("produce_id"),
                raw.get("plan_type"),
                raw.get("exam_effect_type"),
            )
        objective = raw.get("native_objective_by_action", raw.get("objective_by_action"))
        terminal = raw.get("terminal_value_by_action", raw.get("terminal_values_by_action"))
        if objective is None or terminal is None:
            raise ValueError(f"comparison row {index} lacks native objective/value maps")
        return cls(
            row_id=_text(raw.get("row_id", raw.get("id", raw.get("boundary_digest"))), f"comparison[{index}].row_id"),
            flow=_flow_string(flow, f"comparison[{index}].flow"),
            trajectory_id=_text(
                raw.get("trajectory_id", raw.get("source_id")),
                f"comparison[{index}].trajectory_id",
            ),
            legal_candidates=tuple(candidates),
            native_search_action=_text(
                raw.get("native_search_action", raw.get("search_action", raw.get("native_action"))),
                f"comparison[{index}].native_search_action",
            ),
            imitation_action=(
                None
                if raw.get("imitation_action", raw.get("treatment_action")) is None
                else _text(
                    raw.get("imitation_action", raw.get("treatment_action")),
                    f"comparison[{index}].imitation_action",
                )
            ),
            static_action=(
                None
                if raw.get("static_action") is None
                else _text(raw.get("static_action"), f"comparison[{index}].static_action")
            ),
            observed_action=(
                None
                if raw.get("observed_action") is None
                else _text(raw.get("observed_action"), f"comparison[{index}].observed_action")
            ),
            imitation_legal_candidates=(
                None
                if raw.get("imitation_legal_candidates", raw.get("treatment_legal_candidates")) is None
                else tuple(raw.get("imitation_legal_candidates", raw.get("treatment_legal_candidates")))
            ),
            native_objective_by_action=objective,
            terminal_value_by_action=terminal,
            state_digest=(
                None
                if raw.get("state_digest", raw.get("native_state_sha256")) is None
                else _text(
                    raw.get("state_digest", raw.get("native_state_sha256")),
                    f"comparison[{index}].state_digest",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class NativeSearchPromotionPolicy:
    """Conservative flow-level promotion thresholds.

    Accuracy is included, but it is only one condition among data coverage,
    independent trajectories, same-state A/B evidence and native invariants.
    """

    min_trajectory_count: int = 3
    min_observation_count: int = 30
    min_loo_coverage: float = 0.75
    min_loo_accuracy: float = 0.60
    min_comparison_count: int = 12
    min_objective_tie_rate: float = 1.0
    min_terminal_value_preservation_rate: float = 1.0
    min_legal_set_preservation_rate: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "min_trajectory_count",
            "min_observation_count",
            "min_comparison_count",
        ):
            _positive_int(getattr(self, name), f"policy.{name}")
        for name in (
            "min_loo_coverage",
            "min_loo_accuracy",
            "min_objective_tie_rate",
            "min_terminal_value_preservation_rate",
            "min_legal_set_preservation_rate",
        ):
            value = _finite_number(getattr(self, name), f"policy.{name}")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"policy.{name} must be between zero and one")

    def to_dict(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in (
                "min_trajectory_count",
                "min_observation_count",
                "min_loo_coverage",
                "min_loo_accuracy",
                "min_comparison_count",
                "min_objective_tie_rate",
                "min_terminal_value_preservation_rate",
                "min_legal_set_preservation_rate",
            )
        }


def _load_json_rows(source: str | Path | None) -> tuple[Mapping[str, object], ...]:
    if source is None:
        return ()
    path = Path(source).resolve()
    if not path.is_file():
        return ()
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return ()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows: list[object] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"comparison source line {line_number} is malformed") from error
    else:
        if isinstance(payload, Mapping):
            nested = payload.get("rows", payload.get("comparisons"))
            # A single comparison row is a valid JSON artifact as well as a
            # JSONL line.  Only unwrap a mapping when it explicitly carries a
            # container key; otherwise retain the mapping as one row.
            rows = [payload] if nested is None else nested  # type: ignore[assignment]
        else:
            rows = payload
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        raise ValueError("comparison source rows must be an array")
    result: list[Mapping[str, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"comparison source row {index} is not an object")
        result.append(row)
    return tuple(result)


def _comparison_rows(source: str | Path | None) -> tuple[tuple[NativeSearchComparison, ...], dict[str, object]]:
    if source is None:
        return (), {"present": False, "row_count": 0, "valid_row_count": 0, "invalid_row_count": 0}
    path = Path(source).resolve()
    if not path.is_file():
        return (), {
            "present": False,
            "path": _display_path(path),
            "row_count": 0,
            "valid_row_count": 0,
            "invalid_row_count": 0,
        }
    raw_rows = _load_json_rows(path)
    valid: list[NativeSearchComparison] = []
    invalid: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rows):
        try:
            row = NativeSearchComparison.from_mapping(raw, index)
            if row.row_id in seen:
                raise ValueError("duplicate row_id")
            seen.add(row.row_id)
            valid.append(row)
        except (TypeError, ValueError) as error:
            invalid.append({"row_index": index, "reason": f"{type(error).__name__}:{error}"})
    return tuple(valid), {
        **_path_record(path),
        "row_count": len(raw_rows),
        "valid_row_count": len(valid),
        "invalid_row_count": len(invalid),
        "invalid_rows": invalid,
    }


def _read_observations(
    source: str | Path,
) -> tuple[tuple[LeaderboardCardImitationObservation, ...], dict[str, object], dict[str, object]]:
    path = Path(source).resolve()
    observations, projection = build_leaderboard_card_imitation_observations(path)
    evaluation = evaluate_leaderboard_card_imitation(observations)
    return observations, projection.to_dict(), evaluation.to_dict()


def _evaluation_from_file(
    path: Path,
    fallback: Mapping[str, object],
) -> tuple[Mapping[str, object], dict[str, object]]:
    if not path.is_file():
        return fallback, {"present": False, "path": _display_path(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, Mapping):
            raise ValueError("evaluation artifact is not an object")
        loo = payload.get("leave_one_trajectory_out")
        if not isinstance(loo, Mapping):
            raise ValueError("evaluation artifact has no LOO section")
        if not isinstance(loo.get("by_flow"), Mapping):
            raise ValueError("evaluation artifact has no by_flow section")
        return loo, _path_record(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return fallback, {
            **_path_record(path),
            "invalid": True,
        }


def _observation_flow_stats(
    observations: Sequence[LeaderboardCardImitationObservation],
) -> dict[str, dict[str, object]]:
    rows: defaultdict[str, list[LeaderboardCardImitationObservation]] = defaultdict(list)
    for value in observations:
        rows[value.flow_id].append(value)
    result: dict[str, dict[str, object]] = {}
    for flow, values in sorted(rows.items()):
        result[flow] = {
            "observation_count": len(values),
            "trajectory_count": len({value.source_id for value in values}),
            "episode_count": len({value.source_id + ":" + value.stage for value in values}),
            "hand_order": {
                "available_count": len(values),
                "observed_action_count": len(values),
                "matches": sum(value.candidate_ids[0] == value.chosen_id for value in values),
                "match_rate": _rate(
                    sum(value.candidate_ids[0] == value.chosen_id for value in values),
                    len(values),
                ),
            },
            "static_card_id_order": {
                "available_count": len(values),
                "observed_action_count": len(values),
                "matches": sum(min(value.candidate_ids) == value.chosen_id for value in values),
                "match_rate": _rate(
                    sum(min(value.candidate_ids) == value.chosen_id for value in values),
                    len(values),
                ),
                "exact_native_static_objective": False,
            },
        }
    return result


def _comparison_arm_stats(
    rows: Sequence[NativeSearchComparison],
    *,
    observed_fallback: bool = False,
) -> dict[str, object]:
    total = len(rows)
    arms: dict[str, dict[str, object]] = {}
    for name in ARM_NAMES:
        if name == "hand-order":
            row_actions = [(row, row.hand_order_action) for row in rows]
        elif name == "static":
            row_actions = [
                (row, row.static_action or row.static_card_id_order_action)
                for row in rows
            ]
        elif name == "native-search":
            row_actions = [(row, row.native_search_action) for row in rows]
        else:
            row_actions = [
                (row, row.imitation_action)
                for row in rows
                if row.imitation_action is not None
            ]
        observed_pairs = [
            (action, row.observed_action)
            for row, action in row_actions
            if action is not None and row.observed_action is not None
        ]
        arms[name] = {
            "available_count": len(row_actions),
            "observed_action_count": len(observed_pairs),
            "matches_observed": sum(left == right for left, right in observed_pairs),
            "match_rate": _rate(
                sum(left == right for left, right in observed_pairs),
                len(observed_pairs),
            ),
        }

    treatment = [row for row in rows if row.imitation_action is not None]
    objective_tied = [row for row in treatment if row.objective_tied is True]
    objective_non_regressing = [row for row in treatment if row.objective_non_regressing is True]
    terminal_preserved = [row for row in treatment if row.terminal_value_preserved is True]
    legal_preserved = [row for row in treatment if row.legal_set_preserved]
    static_explicit = [row for row in treatment if row.static_action is not None]
    state_bound = [row for row in treatment if row.state_digest is not None]
    treatment_vs = {}
    for label, action_for in (
        ("hand_order", lambda row: row.hand_order_action),
        ("static", lambda row: row.static_action or row.static_card_id_order_action),
        ("native_search", lambda row: row.native_search_action),
    ):
        changed = sum(row.imitation_action != action_for(row) for row in treatment)
        treatment_vs[label] = {
            "paired_count": len(treatment),
            "action_changed_count": changed,
            "action_changed_rate": _rate(changed, len(treatment)),
        }
    return {
        "row_count": total,
        "treatment_count": len(treatment),
        "arms": arms,
        "treatment_vs": treatment_vs,
        "native_safety": {
            "objective_tied_count": len(objective_tied),
            "objective_tie_rate": _rate(len(objective_tied), len(treatment)),
            "objective_non_regressing_count": len(objective_non_regressing),
            "objective_non_regression_rate": _rate(len(objective_non_regressing), len(treatment)),
            "terminal_value_preserved_count": len(terminal_preserved),
            "terminal_value_preservation_rate": _rate(len(terminal_preserved), len(treatment)),
            "legal_set_preserved_count": len(legal_preserved),
            "legal_set_preservation_rate": _rate(len(legal_preserved), len(treatment)),
            "static_baseline_explicit_count": len(static_explicit),
            "state_binding_count": len(state_bound),
            "missing_objective_count": len(treatment) - len([row for row in treatment if row.objective_tied is not None]),
            "missing_terminal_value_count": len(treatment) - len([row for row in treatment if row.terminal_value_preserved is not None]),
            "missing_legal_set_parity_count": len(treatment) - len(legal_preserved),
            "missing_static_baseline_count": len(treatment) - len(static_explicit),
            "missing_state_binding_count": len(treatment) - len(state_bound),
        },
    }


def _flow_comparison_stats(rows: Sequence[NativeSearchComparison]) -> dict[str, dict[str, object]]:
    grouped: defaultdict[str, list[NativeSearchComparison]] = defaultdict(list)
    for row in rows:
        grouped[row.flow].append(row)
    return {
        flow: _comparison_arm_stats(tuple(values))
        for flow, values in sorted(grouped.items())
    }


def _loo_metrics(
    flow: str,
    fallback: Mapping[str, object],
    declared: Mapping[str, object],
) -> dict[str, object]:
    by_flow = declared.get("by_flow")
    value = by_flow.get(flow) if isinstance(by_flow, Mapping) else None
    if not isinstance(value, Mapping):
        value = fallback.get("by_flow", {}).get(flow) if isinstance(fallback.get("by_flow"), Mapping) else None
    if not isinstance(value, Mapping):
        value = {}
    def count(name: str) -> int:
        raw = value.get(name, 0)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0

    def rate(name: str) -> float:
        raw = value.get(name, 0.0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return 0.0
        number = float(raw)
        return number if math.isfinite(number) and 0.0 <= number <= 1.0 else 0.0

    return {
        "observation_count": count("observation_count"),
        "scored_count": count("scored_count"),
        "top1_correct": count("top1_correct"),
        "coverage": rate("coverage"),
        "top1_accuracy": rate("top1_accuracy"),
        "source": "declared-evaluation-artifact" if by_flow is not None and flow in by_flow else "recomputed",
    }


def _flow_gate(
    flow: str,
    observed: Mapping[str, object],
    loo: Mapping[str, object],
    comparison: Mapping[str, object] | None,
    policy: NativeSearchPromotionPolicy,
    reviewed_flows: Sequence[str] = KNOWN_FLOWS,
    runtime_evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    blockers: list[str] = []
    evidence_debt: list[str] = []
    if flow not in reviewed_flows:
        blockers.append("flow-not-in-reviewed-native-search-scope")
    observation_count = int(observed.get("observation_count", 0) or 0)
    trajectory_count = int(observed.get("trajectory_count", 0) or 0)
    if observation_count < policy.min_observation_count:
        blockers.append(f"observation-count:{observation_count}<{policy.min_observation_count}")
    if trajectory_count < policy.min_trajectory_count:
        blockers.append(f"independent-trajectory-count:{trajectory_count}<{policy.min_trajectory_count}")
    coverage = float(loo.get("coverage", 0.0) or 0.0)
    accuracy = float(loo.get("top1_accuracy", 0.0) or 0.0)
    if coverage < policy.min_loo_coverage:
        blockers.append(f"loo-coverage:{coverage:.6f}<{policy.min_loo_coverage:.6f}")
    if accuracy < policy.min_loo_accuracy:
        blockers.append(f"loo-accuracy:{accuracy:.6f}<{policy.min_loo_accuracy:.6f}")

    runtime = runtime_evidence if isinstance(runtime_evidence, Mapping) else {}
    simulator_transition_verified = runtime.get("simulator_transition_verified") is True
    legal_set_verified = runtime.get("legal_set_verified") is True
    runtime_source_count = int(runtime.get("source_count", 0) or 0)
    runtime_divergence_count = int(runtime.get("divergence_step_count", 0) or 0)
    runtime_source_sha_verified = runtime.get("runtime_source_sha256_verified")
    strict_join_verified = runtime.get("strict_leaderboard_join_verified") is True
    dll_legal_set_promotion_allowed = (
        runtime.get("dll_legal_set_promotion_allowed") is True
    )
    legal_set_exact = runtime.get("legal_set_exact") is True
    if runtime_source_count < 1:
        blockers.append("runtime-diff-evidence-missing")
        evidence_debt.append("runtime-diff-evidence-missing")
    elif not simulator_transition_verified:
        # A zero divergence count is not enough by itself: identity, action
        # pairing and complete application must also be present.  Conversely,
        # a verified zero-divergence transition clears only this simulator
        # state blocker; it does not affect the legal-set gate below.
        if runtime_divergence_count:
            blockers.append("simulator-state-divergence")
            evidence_debt.append("simulator-state-divergence")
        else:
            blockers.append("simulator-transition-unverified")
            evidence_debt.append("simulator-transition-unverified")
    if runtime_source_sha_verified is False:
        blockers.append("runtime-source-sha-unverified")
        evidence_debt.append("runtime-source-sha-unverified")
    if not legal_set_verified:
        blockers.append("legal-actions-complete-unverified")
        evidence_debt.append("legal-actions-complete-unverified")

    comparison_debt: dict[str, object] = {
        "required": policy.min_comparison_count,
        "available": 0,
        "observed": 0,
        "shortfall": policy.min_comparison_count,
        "missing": policy.min_comparison_count,
        "status": "missing",
    }
    if comparison is None:
        blockers.extend(
            (
                "native-search-comparison-evidence-missing",
                "static-baseline-native-objective-evidence-missing",
                "candidate-set-parity-unproven",
                "native-objective-tie-unproven",
                "terminal-value-preservation-unproven",
                "same-native-state-binding-unproven",
            )
        )
        evidence_debt.append(
            f"comparison-rows-insufficient:0<{policy.min_comparison_count}"
        )
    else:
        count = int(comparison.get("treatment_count", 0) or 0)
        comparison_debt = {
            "required": policy.min_comparison_count,
            "available": count,
            "observed": count,
            "shortfall": max(policy.min_comparison_count - count, 0),
            "missing": max(policy.min_comparison_count - count, 0),
            "status": (
                "sufficient"
                if count >= policy.min_comparison_count
                else "insufficient"
            ),
        }
        if count < policy.min_comparison_count:
            blockers.append(f"paired-native-comparison-count:{count}<{policy.min_comparison_count}")
            evidence_debt.append(
                f"comparison-rows-insufficient:{count}<{policy.min_comparison_count}"
            )
        safety = comparison.get("native_safety", {})
        if not isinstance(safety, Mapping):
            safety = {}
        for key, threshold, label in (
            ("objective_tie_rate", policy.min_objective_tie_rate, "native-objective-tie-rate"),
            (
                "terminal_value_preservation_rate",
                policy.min_terminal_value_preservation_rate,
                "terminal-value-preservation-rate",
            ),
            (
                "legal_set_preservation_rate",
                policy.min_legal_set_preservation_rate,
                "legal-set-preservation-rate",
            ),
        ):
            value = safety.get(key)
            if value is None:
                blockers.append(f"{label}-unproven")
            elif float(value) < threshold:
                blockers.append(f"{label}:{float(value):.6f}<{threshold:.6f}")
        if int(safety.get("missing_objective_count", 0) or 0):
            blockers.append("native-objective-evidence-incomplete")
        if int(safety.get("missing_terminal_value_count", 0) or 0):
            blockers.append("terminal-value-evidence-incomplete")
        if int(safety.get("missing_legal_set_parity_count", 0) or 0):
            blockers.append("candidate-set-parity-evidence-incomplete")
        if int(safety.get("missing_static_baseline_count", 0) or 0):
            blockers.append("static-baseline-comparison-incomplete")
        if int(safety.get("missing_state_binding_count", 0) or 0):
            blockers.append("same-native-state-binding-incomplete")

    return {
        "flow": flow,
        "eligible": not blockers,
        "simulator_transition_verified": simulator_transition_verified,
        "legal_set_verified": legal_set_verified,
        "legal_set_exact": legal_set_exact,
        "dll_legal_set_promotion_allowed": dll_legal_set_promotion_allowed,
        "dll_legal_set_promotion": runtime.get("dll_legal_set_promotion"),
        # Runtime-recorder/simulator diffs are never exact leaderboard
        # evidence, even when their source hash and all local transitions
        # verify successfully.
        "exact": False,
        # Keep this explicit and independent from the legacy ``eligible``
        # alias.  In particular, a simulator-only pass must remain false.
        "promotion_allowed": not blockers,
        "strategy_promotion_allowed": not blockers,
        "strategy_promotion": {
            "allowed": not blockers,
            "default_enabled": False,
            "owner": "native-search/imitation readiness gate",
        },
        "legal_actions_complete": legal_set_verified,
        "runtime_source_sha256_verified": runtime_source_sha_verified,
        "leaderboard_join_provenance": runtime.get("leaderboard_join_provenance"),
        "strict_leaderboard_join_verified": strict_join_verified,
        "default_enabled": False,
        "opt_in_allowed": not blockers,
        "blockers": list(dict.fromkeys(blockers)),
        "evidence_debt": list(dict.fromkeys(evidence_debt)),
        "evidence_debt_detail": {
            "comparison_rows": comparison_debt,
            "runtime_diff_sources": runtime_source_count,
        },
        "metrics": {
            "observation_count": observation_count,
            "trajectory_count": trajectory_count,
            "loo": dict(loo),
            "comparison": None if comparison is None else dict(comparison),
            "runtime_diff": None if runtime_evidence is None else dict(runtime_evidence),
        },
    }


def evaluate_native_search_imitation_readiness(
    *,
    episodes_source: str | Path = DEFAULT_EPISODES,
    evaluation_source: str | Path | None = DEFAULT_EVALUATION,
    comparison_source: str | Path | None = DEFAULT_COMPARISONS,
    policy: NativeSearchPromotionPolicy = NativeSearchPromotionPolicy(),
    flow_scope: Sequence[str] | None = None,
    runtime_diff_sources: object = DEFAULT_RUNTIME_DIFF_SOURCES,
    runtime_diff_source: object | None = None,
    runtime_authority_sources: object = DEFAULT_RUNTIME_AUTHORITY_SOURCES,
    runtime_legal_candidate_evidence_source: str | Path | None = DEFAULT_RUNTIME_LEGAL_CANDIDATE_EVIDENCE,
    runtime_legal_verified_probe_source: str | Path | Mapping[str, object] | None = DEFAULT_RUNTIME_LEGAL_VERIFIED_PROBE,
) -> dict[str, object]:
    """Build the flow-specific promotion/A-B readiness report.

    Runtime simulator diff artifacts are observational evidence.  A complete
    zero-divergence transition can clear a simulator-state blocker, but the
    report never infers a legal candidate set or ``legal_actions_complete``
    from that result.  ``runtime_diff_source`` is a singular compatibility
    alias for callers that provide one path; ``runtime_diff_sources`` accepts
    a flow-to-path(s) mapping, path sequence, or one path.  Strict leaderboard
    join details may be supplied by the separate
    ``runtime_authority_sources`` map; those details never change exactness or
    legal-action completeness.  The optional runtime legal-candidate evidence
    ingest adds observational state/identity/purity metrics only; it never
    upgrades the legal set or promotion gate.  The separate verified-probe
    source may verify one runtime legal set, but its DLL-level
    ``promotion.allowed`` is never used as strategy promotion.
    """

    if not isinstance(policy, NativeSearchPromotionPolicy):
        raise TypeError("policy must be NativeSearchPromotionPolicy")
    if flow_scope is None:
        reviewed_flows = KNOWN_FLOWS
    else:
        values: list[str] = []
        for index, value in enumerate(flow_scope):
            values.append(_flow_string(value, f"flow_scope[{index}]"))
        reviewed_flows = tuple(sorted(set(values)))
        if not reviewed_flows:
            raise ValueError("flow_scope must contain at least one flow")
    episode_path = Path(episodes_source).resolve()
    observations, projection, recomputed = _read_observations(episode_path)
    declared, evaluation_record = _evaluation_from_file(
        Path(evaluation_source).resolve() if evaluation_source is not None else Path("__missing__"),
        recomputed,
    )
    comparison_rows, comparison_record = _comparison_rows(comparison_source)
    runtime_by_flow, runtime_record = _runtime_diff_evidence(
        runtime_diff_sources if runtime_diff_source is None else runtime_diff_source,
        authority_sources=runtime_authority_sources,
    )
    candidate_evidence_record = _runtime_legal_candidate_evidence(
        runtime_legal_candidate_evidence_source
    )
    verified_probe_record = _runtime_legal_verified_probe(
        runtime_legal_verified_probe_source
    )
    observed_by_flow = _observation_flow_stats(observations)
    comparison_by_flow = _flow_comparison_stats(comparison_rows)
    flow_ids = tuple(
        sorted(
            set(reviewed_flows)
            | set(observed_by_flow)
            | set(comparison_by_flow)
            | set(runtime_by_flow)
        )
    )
    flow_reports: list[dict[str, object]] = []
    for flow in flow_ids:
        observed = observed_by_flow.get(
            flow,
            {
                "observation_count": 0,
                "trajectory_count": 0,
                "episode_count": 0,
                "hand_order": {"available_count": 0, "observed_action_count": 0, "matches": 0, "match_rate": None},
                "static_card_id_order": {"available_count": 0, "observed_action_count": 0, "matches": 0, "match_rate": None, "exact_native_static_objective": False},
            },
        )
        loo = _loo_metrics(flow, recomputed, declared)
        comparison = comparison_by_flow.get(flow)
        runtime_evidence = _merge_runtime_legal_verified_probe(
            runtime_by_flow.get(flow),
            verified_probe_record
            if verified_probe_record.get("flow") == flow
            else None,
        )
        gate = _flow_gate(
            flow,
            observed,
            loo,
            comparison,
            policy,
            reviewed_flows,
            runtime_evidence,
        )
        flow_reports.append(
            {
                **gate,
                "baseline_comparison": {
                    "hand-order": observed.get("hand_order"),
                    "static": observed.get("static_card_id_order"),
                    "native-search": (
                        None
                        if comparison is None
                        else comparison.get("arms", {}).get("native-search")
                        if isinstance(comparison.get("arms"), Mapping)
                        else None
                    ),
                    "imitation": (
                        None
                        if comparison is None
                        else comparison.get("arms", {}).get("imitation")
                        if isinstance(comparison.get("arms"), Mapping)
                        else None
                    ),
                    "runtime-diff": runtime_evidence,
                    "runtime-legal-verified-probe": (
                        None
                        if verified_probe_record.get("flow") != flow
                        else verified_probe_record.get("evidence")
                    ),
                },
            }
        )
    candidate_flow = candidate_evidence_record.get("flow")
    candidate_evidence = candidate_evidence_record.get("evidence")
    if (
        isinstance(candidate_flow, str)
        and isinstance(candidate_evidence, Mapping)
    ):
        for row in flow_reports:
            if row.get("flow") != candidate_flow:
                continue
            metrics = row.get("metrics")
            if isinstance(metrics, dict):
                metrics["runtime_legal_candidate_evidence"] = dict(candidate_evidence)
            detail = row.get("evidence_debt_detail")
            if isinstance(detail, dict):
                detail["runtime_legal_candidate_evidence"] = {
                    "status": "observational-only",
                    "resolved": [
                        "candidate-presence",
                        "ordered-hand-identity",
                        "enumeration-purity",
                        "official-chosen-action-binding",
                    ],
                    "remaining": [
                        "legal-actions-complete-unverified",
                        "same-native-state-objective-comparison-unavailable",
                    ],
                    "legal_set_verified": False,
                    "exact": False,
                    "promotion_allowed": False,
                }
    verified_flow = verified_probe_record.get("flow")
    verified_evidence = verified_probe_record.get("evidence")
    for row in flow_reports:
        if row.get("flow") != verified_flow:
            continue
        detail = row.get("evidence_debt_detail")
        if not isinstance(detail, dict):
            continue
        if verified_probe_record.get("valid") is True and isinstance(
            verified_evidence, Mapping
        ):
            detail["runtime_legal_verified_probe"] = {
                "status": "runtime-verified",
                "resolved": [
                    "legal-actions-complete",
                    "exact-legal-set",
                    "hand-family-10-of-10",
                    "drink-family-10-of-10",
                    "end-turn-family-10-of-10",
                    "official-chosen-membership-10-of-10",
                    "settlement",
                ],
                "legal_set_verified": True,
                "exact": True,
                "dll_legal_set_promotion_allowed": True,
                "strategy_promotion_allowed": False,
            }
        else:
            detail["runtime_legal_verified_probe"] = {
                "status": "unverified",
                "legal_set_verified": False,
                "exact": False,
                "dll_legal_set_promotion_allowed": False,
                "strategy_promotion_allowed": False,
            }
    eligible = [row["flow"] for row in flow_reports if row["eligible"] is True]
    simulator_verified = [
        row["flow"]
        for row in flow_reports
        if row.get("simulator_transition_verified") is True
    ]
    legal_verified = [
        row["flow"]
        for row in flow_reports
        if row.get("legal_set_verified") is True
    ]
    promotion_allowed_flows = [
        row["flow"]
        for row in flow_reports
        if row.get("promotion_allowed") is True
    ]
    all_blockers = Counter(
        blocker
        for row in flow_reports
        for blocker in row.get("blockers", [])
        if isinstance(blocker, str)
    )
    report: dict[str, object] = {
        "schema": SCHEMA,
        "artifact_version": READINESS_ARTIFACT_VERSION,
        "offline_only": True,
        "game_operated": False,
        "queue_operated": False,
        "terra_used": False,
        # Runtime diff evidence is observational.  Keep these root-level
        # declarations explicit so no consumer can read a zero-divergence
        # child row as an exact/legal-complete artifact.
        "exact": False,
        "legal_actions_complete": False,
        "runtime": {
            "inner_imitation_default_enabled": False,
            "runtime_mode": "native-search-only-bounded-tiebreak-or-ordering-hint",
            "legal_set_owner": "Plan1/Plan2/Plan3 native planner",
            "terminal_value_owner": "Plan1/Plan2/Plan3 native simulator/search",
            "explicit_opt_in_required": True,
            "eligible_opt_in_flows": eligible,
            "simulator_transition_verified_flows": simulator_verified,
            "legal_set_verified_flows": legal_verified,
            "strict_leaderboard_join_verified_flows": [
                row["flow"]
                for row in flow_reports
                if row.get("strict_leaderboard_join_verified") is True
            ],
            "legal_actions_complete": False,
        },
        "policy": policy.to_dict(),
        "arms": {
            "hand-order": {
                "name": "first candidate in the settled/native Hand order",
                "role": "transparent control comparator",
            },
            "static": {
                "name": "lexical card-ID order proxy unless a native static result is supplied",
                "role": "diagnostic comparator",
                "exact_native_static_objective_required_for_promotion": True,
            },
            "native-search": {
                "name": "existing Plan1/Plan2/Plan3 native search",
                "role": "authoritative control",
            },
            "imitation": {
                "name": "leaderboard card behavior prior",
                "role": "bounded advisory only",
                "allowed_effect": "equal-native-objective tie-break or node ordering",
            },
        },
        "native_search_contract": NATIVE_SEARCH_SEAMS,
        "sources": {
            "episodes": _path_record(episode_path, required=True),
            "imitation_evaluation": evaluation_record,
            "comparisons": comparison_record,
            "runtime_diffs": runtime_record,
            "runtime_diff": runtime_record,
            "runtime_legal_candidate_evidence": candidate_evidence_record,
            "runtime_legal_verified_probe": verified_probe_record,
            "runtime_authority": {
                "sources": runtime_record.get("authority_sources", []),
                "strict_join_is_provenance_only": True,
            },
        },
        # Keep runtime evidence in a first-class section so consumers do not
        # mistake it for candidate-set comparison evidence.
        "runtime_diff_evidence": runtime_record,
        # Candidate rows are a separate observational input.  They provide
        # ordered identity/purity metrics, never legal-set completeness.
        "runtime_legal_candidate_evidence": candidate_evidence_record,
        # The dedicated verified probe is authoritative only for the runtime
        # legal set.  Its nested DLL ``promotion.allowed`` must not be read as
        # strategy/imitation promotion permission.
        "runtime_legal_verified_probe": verified_probe_record,
        "dataset": {
            "projection": projection,
            "recomputed_loo": recomputed,
            "declared_loo": dict(declared),
            "declared_loo_source": evaluation_record.get("path"),
            "reviewed_flow_scope": list(reviewed_flows),
            "episode_count": len({value.source_id + ":" + value.stage for value in observations}),
            "observation_count": len(observations),
            "trajectory_count": len({value.source_id for value in observations}),
            "flow_count": len(flow_ids),
            "latest_v5_episodes": episode_path == LATEST_V5_EPISODES,
        },
        "imitation_prior": {
            "behavior_only": True,
            "full_rl_transition_model": False,
            "declared_loo": dict(declared),
            "coverage": declared.get("coverage"),
            "top1_accuracy": declared.get("top1_accuracy"),
            "top1_over_all": declared.get("top1_over_all"),
        },
        "flows": flow_reports,
        "gate_status": {
            "simulator_transition_verified_flows": simulator_verified,
            "legal_set_verified_flows": legal_verified,
            "strict_leaderboard_join_verified_flows": [
                row["flow"]
                for row in flow_reports
                if row.get("strict_leaderboard_join_verified") is True
            ],
            "promotion_allowed_flows": promotion_allowed_flows,
        },
        "summary": {
            "flow_count": len(flow_reports),
            "eligible_flow_count": len(eligible),
            "eligible_flows": eligible,
            "blocked_flow_count": len(flow_reports) - len(eligible),
            "simulator_transition_verified_flow_count": len(simulator_verified),
            "simulator_transition_verified_flows": simulator_verified,
            "strict_leaderboard_join_verified_flow_count": sum(
                1
                for row in flow_reports
                if row.get("strict_leaderboard_join_verified") is True
            ),
            "legal_set_verified_flow_count": len(legal_verified),
            "legal_set_verified_flows": legal_verified,
            "promotion_allowed_flow_count": len(promotion_allowed_flows),
            "blocker_counts": dict(sorted(all_blockers.items())),
            "runtime_legal_candidate_evidence": candidate_evidence_record.get(
                "evidence"
            ),
            "runtime_legal_verified_probe": verified_probe_record.get("evidence"),
            "dll_legal_set_promotion_allowed_flow_count": sum(
                1
                for row in flow_reports
                if row.get("dll_legal_set_promotion_allowed") is True
            ),
            "dll_legal_set_promotion_allowed_flows": [
                row["flow"]
                for row in flow_reports
                if row.get("dll_legal_set_promotion_allowed") is True
            ],
        },
        "promotion": {
            "status": "opt-in-only" if eligible else "not-ready",
            "allowed": bool(promotion_allowed_flows),
            "default_enabled": False,
            "promotion_allowed": bool(promotion_allowed_flows),
            "eligible_opt_in_flows": eligible,
            "blocked_flows": [row["flow"] for row in flow_reports if not row["eligible"]],
            "requirements": [
                "same-flow independent trajectories and LOO coverage",
                "hand-order/static/native-search/imitation comparison on one native legal set",
                "native objective unchanged for any imitation action change",
                "native terminal value unchanged",
                "native legal candidate set unchanged",
            ],
            "promotion_kind": "native-search-imitation-strategy",
            "dll_legal_set_promotion_allowed_flows": [
                row["flow"]
                for row in flow_reports
                if row.get("dll_legal_set_promotion_allowed") is True
            ],
            "dll_legal_set_promotion_is_independent": True,
        },
        "strategy_promotion": {
            "allowed": bool(promotion_allowed_flows),
            "default_enabled": False,
            "eligible_opt_in_flows": promotion_allowed_flows,
            "reason": "LOO/comparison/objective/terminal preservation gates remain required",
        },
        "dll_legal_set_promotion": {
            "allowed": verified_probe_record.get("valid") is True,
            "allowed_flows": (
                [verified_probe_record.get("flow")]
                if verified_probe_record.get("valid") is True
                else []
            ),
            "source": verified_probe_record,
            "scope": "runtime legal-action set only",
            "does_not_enable_strategy_promotion": True,
        },
    }
    report["report_sha256"] = _sha256_bytes(_canonical(report))
    return report


# Naming aliases make the artifact discoverable from both the native-search
# and imitation/promotion terminology used by callers.
load_native_runtime_diff_evidence = _runtime_diff_evidence
evaluate_native_imitation_promotion = evaluate_native_search_imitation_readiness
build_native_search_imitation_promotion_report = evaluate_native_search_imitation_readiness


def _atomic_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with open(descriptor, "w", encoding="utf-8", newline="\n", closefd=True) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
        Path(temporary).replace(path)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def write_native_search_imitation_readiness(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    episodes_source: str | Path = DEFAULT_EPISODES,
    evaluation_source: str | Path | None = DEFAULT_EVALUATION,
    comparison_source: str | Path | None = DEFAULT_COMPARISONS,
    policy: NativeSearchPromotionPolicy = NativeSearchPromotionPolicy(),
    flow_scope: Sequence[str] | None = None,
    runtime_diff_sources: object = DEFAULT_RUNTIME_DIFF_SOURCES,
    runtime_diff_source: object | None = None,
    runtime_authority_sources: object = DEFAULT_RUNTIME_AUTHORITY_SOURCES,
    runtime_legal_candidate_evidence_source: str | Path | None = DEFAULT_RUNTIME_LEGAL_CANDIDATE_EVIDENCE,
    runtime_legal_verified_probe_source: str | Path | Mapping[str, object] | None = DEFAULT_RUNTIME_LEGAL_VERIFIED_PROBE,
) -> dict[str, object]:
    report = evaluate_native_search_imitation_readiness(
        episodes_source=episodes_source,
        evaluation_source=evaluation_source,
        comparison_source=comparison_source,
        policy=policy,
        flow_scope=flow_scope,
        runtime_diff_sources=runtime_diff_sources,
        runtime_diff_source=runtime_diff_source,
        runtime_authority_sources=runtime_authority_sources,
        runtime_legal_candidate_evidence_source=runtime_legal_candidate_evidence_source,
        runtime_legal_verified_probe_source=runtime_legal_verified_probe_source,
    )
    _atomic_write(Path(output).resolve(), report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--episodes",
        "--episodes-path",
        dest="episodes",
        type=Path,
        default=DEFAULT_EPISODES,
    )
    parser.add_argument("--evaluation", type=Path, default=DEFAULT_EVALUATION)
    parser.add_argument("--comparisons", type=Path, default=DEFAULT_COMPARISONS)
    parser.add_argument(
        "--runtime-legal-candidate-evidence",
        type=Path,
        default=DEFAULT_RUNTIME_LEGAL_CANDIDATE_EVIDENCE,
        help=(
            "observational runtime candidate ingest; it never upgrades legal "
            "set completeness or promotion"
        ),
    )
    parser.add_argument(
        "--runtime-legal-verified-probe",
        type=Path,
        default=DEFAULT_RUNTIME_LEGAL_VERIFIED_PROBE,
        help=(
            "verified three-family runtime legal-set probe; its DLL-level "
            "promotion.allowed never enables strategy promotion"
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--runtime-diff",
        "--runtime-diffs",
        dest="runtime_diffs",
        action="append",
        type=Path,
        help=(
            "official runtime simulator diff JSON; may be repeated.  When "
            "omitted, the reviewed Plan1/Plan2/Plan3 evidence set is used"
        ),
    )
    parser.add_argument(
        "--flow",
        dest="flow_scope",
        action="append",
        help=(
            "exact flow key to include in the reviewed scope; may be repeated "
            "(default: the five produce-004 flows)"
        ),
    )
    args = parser.parse_args(argv)
    report = write_native_search_imitation_readiness(
        args.output,
        episodes_source=args.episodes,
        evaluation_source=args.evaluation,
        comparison_source=args.comparisons,
        runtime_diff_sources=(
            DEFAULT_RUNTIME_DIFF_SOURCES
            if args.runtime_diffs is None
            else tuple(args.runtime_diffs)
        ),
        flow_scope=args.flow_scope,
        runtime_legal_candidate_evidence_source=args.runtime_legal_candidate_evidence,
        runtime_legal_verified_probe_source=args.runtime_legal_verified_probe,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


__all__ = [
    "ARM_NAMES",
    "COMPARISON_SCHEMA",
    "DEFAULT_COMPARISONS",
    "DEFAULT_EPISODES",
    "DEFAULT_EVALUATION",
    "DEFAULT_OUTPUT",
    "DEFAULT_RUNTIME_AUTHORITY_SOURCES",
    "DEFAULT_RUNTIME_LEGAL_CANDIDATE_EVIDENCE",
    "DEFAULT_RUNTIME_LEGAL_VERIFIED_PROBE",
    "DEFAULT_RUNTIME_DIFFS",
    "DEFAULT_RUNTIME_DIFF_SOURCES",
    "LATEST_V5_EPISODES",
    "LEGACY_EPISODES",
    "LEGACY_OUTPUT",
    "KNOWN_FLOWS",
    "NATIVE_SEARCH_SEAMS",
    "NativeSearchComparison",
    "NativeSearchPromotionPolicy",
    "RUNTIME_DIFF_SCHEMA",
    "RUNTIME_DIFF_SOURCES",
    "RUNTIME_LEGAL_CANDIDATE_EVIDENCE_SCHEMA",
    "RUNTIME_LEGAL_VERIFIED_PROBE_FLOW",
    "RUNTIME_LEGAL_VERIFIED_PROBE_SCHEMA",
    "RUNTIME_LEGAL_VERIFIED_PROBE_SHA256",
    "RUNTIME_LEGAL_VERIFIED_PROBE_TRANSITIONS",
    "READINESS_ARTIFACT_VERSION",
    "SCHEMA",
    "build_native_search_imitation_promotion_report",
    "evaluate_native_imitation_promotion",
    "evaluate_native_search_imitation_readiness",
    "load_native_runtime_diff_evidence",
    "main",
    "write_native_search_imitation_readiness",
]


if __name__ == "__main__":
    raise SystemExit(main())
