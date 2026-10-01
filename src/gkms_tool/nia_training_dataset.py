"""Strict behavior data and exact-candidate priors for N.I.A. decisions.

Leaderboard schedules and accepted live journals are valuable successful
behavior, but neither source currently contains a complete state transition at
every decision boundary.  This module therefore labels every exported row as
``behavior_only`` and refuses to claim full reinforcement-learning transitions.

The runtime prior can only reorder the exact legal candidate set supplied by
the live adviser.  Scope or candidate-set mismatches return no ranking and fall
back to the ordinary native/Maa policy.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .leaderboard_outer_schedule import (
    DEFAULT_RAW_HISTORY,
    collect_leaderboard_outer_history_sources,
    extract_leaderboard_outer_trajectories,
)
from .leaderboard_replay import LeaderboardRawHistoryCollection, list_leaderboard_raw_sources
from .master_db import get_idol_profile
from .nia_route_profile import nia_phase_for_week, nia_stage_positions
from .nia_strategy_journal import (
    DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
    NiaFinalRecord,
    read_nia_strategy_journal,
)


OBSERVATION_SCHEMA = "gkms.nia-training-observation.v1"
MANIFEST_SCHEMA = "gkms.nia-training-dataset-manifest.v1"
PRIOR_SCHEMA = "gkms.nia-exact-candidate-behavior-prior.v1"
BROAD_PRIOR_SCHEMA = "gkms.nia-broad-outer-behavior-prior.v1"
HIERARCHICAL_PRIOR_SCHEMA = "gkms.nia-hierarchical-candidate-behavior-prior.v1"
MIN_SCOPE_SOURCES = 3
# A broad scope is deliberately an inter-card aggregate.  Repeating one
# idol-card trajectory cannot manufacture broad evidence; independent source
# units are still counted as ``(source, source_id)`` below.
MIN_BROAD_IDOL_CARDS = 2
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COMPLETION_REPORT_ROOT = PROJECT_ROOT / "var" / "nia_live"
DEFAULT_OUTPUT = PROJECT_ROOT / "var" / "nia_training" / "behavior_observations.jsonl"

SOURCE_LEADERBOARD_OUTER = "leaderboard_outer"
SOURCE_ACCEPTED_LIVE = "accepted_live"
DECISION_OUTER_ACTION = "outer_action"
DECISION_REWARD_CARD = "reward_card"

RANK_SOURCE_EXACT = "exact"
RANK_SOURCE_BROAD = "broad"

ExactScopeKey = tuple[str, str, str, str, str]
BroadScopeKey = tuple[str, str, str]
SourceKey = tuple[str, str]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _json_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("state_before must be a mapping")
    # Round-trip through strict JSON to detach MappingProxyType/tuples and to
    # reject non-serializable runtime objects before they reach the dataset.
    detached = json.loads(_canonical_json(value).decode("utf-8"))
    if not isinstance(detached, dict):
        raise ValueError("state_before must serialize to an object")
    return detached


def _candidate_signature(values: Sequence[str]) -> tuple[str, ...]:
    candidates = tuple(values)
    if not candidates or any(not isinstance(value, str) or not value for value in candidates):
        raise ValueError("candidate IDs must be non-empty strings")
    if len(candidates) != len(set(candidates)):
        raise ValueError("candidate IDs must be unique")
    return tuple(sorted(candidates))


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _optional_source_sha256(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest or null")
    return value


@dataclass(frozen=True, slots=True)
class NiaTrainingScope:
    produce_id: str
    idol_card_id: str
    character_id: str
    plan_type: str
    exam_effect_type: str
    app_version: str | None = None
    master_version: str | None = None
    master_hash: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "produce_id",
            "idol_card_id",
            "character_id",
            "plan_type",
            "exam_effect_type",
        ):
            _text(getattr(self, name), f"scope.{name}")
        for name in ("app_version", "master_version", "master_hash"):
            value = getattr(self, name)
            if value is not None:
                _text(value, f"scope.{name}")

    @property
    def runtime_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.produce_id,
            self.idol_card_id,
            self.character_id,
            self.plan_type,
            self.exam_effect_type,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "NiaTrainingScope":
        if not isinstance(value, Mapping):
            raise ValueError("scope must be an object")
        allowed = {
            "produce_id",
            "idol_card_id",
            "character_id",
            "plan_type",
            "exam_effect_type",
            "app_version",
            "master_version",
            "master_hash",
        }
        if set(value) != allowed:
            raise ValueError("scope has an unsupported shape")
        return cls(**{name: value[name] for name in allowed})  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class NiaTrainingObservation:
    source: str
    source_id: str
    scope: NiaTrainingScope
    week: int
    phase: int
    decision_kind: str
    candidate_ids: tuple[str, ...]
    chosen_id: str
    state_before: Mapping[str, object] | None = None
    state_after: Mapping[str, object] | None = None
    terminal_value: int | float | None = None
    source_sha256: str | None = None
    behavior_only: bool = True
    full_rl_transition: bool = False
    schema: str = OBSERVATION_SCHEMA
    # Optional endpoint-level provenance.  Legacy observations intentionally
    # keep this empty; leaderboard-derived rows carry the merged source labels
    # (for example mode_ranking_top + mode_ranking).
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != OBSERVATION_SCHEMA:
            raise ValueError("unsupported N.I.A. training observation schema")
        if self.source not in {SOURCE_LEADERBOARD_OUTER, SOURCE_ACCEPTED_LIVE}:
            raise ValueError("unsupported N.I.A. training source")
        _text(self.source_id, "source_id")
        if not isinstance(self.scope, NiaTrainingScope):
            raise TypeError("scope must be NiaTrainingScope")
        _integer(self.week, "week", minimum=1)
        _integer(self.phase, "phase", minimum=1)
        if nia_phase_for_week(self.scope.produce_id, self.week) != self.phase:
            raise ValueError("observation phase does not match the N.I.A. route")
        if self.decision_kind not in {DECISION_OUTER_ACTION, DECISION_REWARD_CARD}:
            raise ValueError("unsupported N.I.A. decision kind")
        signature = _candidate_signature(self.candidate_ids)
        object.__setattr__(self, "candidate_ids", signature)
        if self.chosen_id not in signature:
            raise ValueError("chosen ID is outside the complete candidate set")
        if self.state_before is not None:
            object.__setattr__(self, "state_before", _json_mapping(self.state_before))
        if self.state_after is not None:
            object.__setattr__(self, "state_after", _json_mapping(self.state_after))
        if isinstance(self.terminal_value, bool) or (
            self.terminal_value is not None
            and not isinstance(self.terminal_value, (int, float))
        ):
            raise ValueError("terminal_value must be numeric or null")
        object.__setattr__(self, "source_sha256", _optional_source_sha256(self.source_sha256))
        if not isinstance(self.provenance, Sequence) or isinstance(
            self.provenance, (str, bytes)
        ):
            raise ValueError("provenance must be an array")
        provenance = tuple(self.provenance)
        if any(not isinstance(value, str) or not value for value in provenance):
            raise ValueError("provenance values must be non-empty strings")
        if len(provenance) != len(set(provenance)):
            raise ValueError("provenance values must be unique")
        object.__setattr__(self, "provenance", tuple(sorted(provenance)))
        # The four-field promotion gate is intentionally exact.  A row may
        # claim a full inner transition only when state_before, action
        # (chosen_id), state_after, and numeric reward/terminal value are all
        # present.  Leaderboard outer rows never satisfy this gate and remain
        # behavior-only with a zero RL count.
        full_gate = (
            isinstance(self.state_before, Mapping)
            and isinstance(self.state_after, Mapping)
            and isinstance(self.chosen_id, str)
            and bool(self.chosen_id)
            and self.terminal_value is not None
        )
        if self.full_rl_transition:
            if self.behavior_only is not False or not full_gate:
                raise ValueError(
                    "full RL transition requires exact state_before/action/state_after/reward gate; "
                    "behavior-only rows cannot claim RL"
                )
        elif self.behavior_only is not True or self.state_after is not None:
            raise ValueError(
                "current N.I.A. observations are behavior-only, not RL transitions"
            )

    def to_dict(self) -> dict[str, object]:
        result = {
            "schema": self.schema,
            "source": self.source,
            "source_id": self.source_id,
            "scope": self.scope.to_dict(),
            "week": self.week,
            "phase": self.phase,
            "decision_kind": self.decision_kind,
            "candidate_ids": list(self.candidate_ids),
            "chosen_id": self.chosen_id,
            "state_before": None if self.state_before is None else dict(self.state_before),
            "state_after": None if self.state_after is None else dict(self.state_after),
            "terminal_value": self.terminal_value,
            "source_sha256": self.source_sha256,
            "behavior_only": self.behavior_only,
            "full_rl_transition": self.full_rl_transition,
        }
        if self.provenance:
            result["provenance"] = list(self.provenance)
        return result

    @property
    def source_hash(self) -> str | None:
        """Compatibility alias for consumers that call the digest a hash."""

        return self.source_sha256

    @property
    def sources(self) -> tuple[str, ...]:
        """Alias for callers that use ``sources`` instead of provenance."""

        return self.provenance

    @classmethod
    def from_dict(cls, value: object) -> "NiaTrainingObservation":
        if not isinstance(value, Mapping):
            raise ValueError("training observation must be an object")
        expected = {
            "schema",
            "source",
            "source_id",
            "scope",
            "week",
            "phase",
            "decision_kind",
            "candidate_ids",
            "chosen_id",
            "state_before",
            "state_after",
            "terminal_value",
            "source_sha256",
            "behavior_only",
            "full_rl_transition",
        }
        optional_provenance = {"provenance"}
        optional_sources = {"sources"}
        # ``source_sha256`` was added after the first v1 artifact.  Existing
        # canonical rows remain loadable, while all new writer output carries
        # the field explicitly.
        legacy_expected = expected - {"source_sha256"}
        if set(value) not in (
            expected,
            legacy_expected,
            expected | optional_provenance,
            legacy_expected | optional_provenance,
            expected | optional_sources,
            legacy_expected | optional_sources,
        ):
            raise ValueError("training observation has an unsupported shape")
        raw_candidates = value["candidate_ids"]
        if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
            raise ValueError("candidate_ids must be an array")
        return cls(
            schema=value["schema"],  # type: ignore[arg-type]
            source=value["source"],  # type: ignore[arg-type]
            source_id=value["source_id"],  # type: ignore[arg-type]
            scope=NiaTrainingScope.from_dict(value["scope"]),
            week=value["week"],  # type: ignore[arg-type]
            phase=value["phase"],  # type: ignore[arg-type]
            decision_kind=value["decision_kind"],  # type: ignore[arg-type]
            candidate_ids=tuple(raw_candidates),  # type: ignore[arg-type]
            chosen_id=value["chosen_id"],  # type: ignore[arg-type]
            state_before=value["state_before"],  # type: ignore[arg-type]
            state_after=value["state_after"],  # type: ignore[arg-type]
            terminal_value=value["terminal_value"],  # type: ignore[arg-type]
            source_sha256=value.get("source_sha256"),  # type: ignore[arg-type]
            behavior_only=value["behavior_only"],  # type: ignore[arg-type]
            full_rl_transition=value["full_rl_transition"],  # type: ignore[arg-type]
            provenance=tuple(
                value.get("provenance", value.get("sources", ()))
            ),  # type: ignore[arg-type]
        )


def leaderboard_behavior_observations(
    source: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection = DEFAULT_RAW_HISTORY,
) -> tuple[NiaTrainingObservation, ...]:
    result: list[NiaTrainingObservation] = []
    for trajectory in extract_leaderboard_outer_trajectories(
        source,
        allow_schedule_only_fallback=False,
    ):
        scope = NiaTrainingScope(
            produce_id=trajectory.produce_id,
            idol_card_id=trajectory.idol_card_id,
            character_id=trajectory.character_id,
            plan_type=trajectory.plan_type,
            exam_effect_type=trajectory.exam_effect_type,
            app_version=trajectory.app_version,
            master_version=trajectory.master_version,
            master_hash=trajectory.master_hash,
        )
        for choice in trajectory.choices:
            if choice.candidate_set_complete is not True:
                raise ValueError("leaderboard outer candidate set is incomplete")
            candidates = tuple(dict.fromkeys(value.action for value in choice.candidates))
            result.append(
                NiaTrainingObservation(
                    source=SOURCE_LEADERBOARD_OUTER,
                    source_id=trajectory.trajectory_id,
                    scope=scope,
                    week=choice.number,
                    phase=nia_phase_for_week(trajectory.produce_id, choice.number) or 0,
                    decision_kind=DECISION_OUTER_ACTION,
                    candidate_ids=candidates,
                    chosen_id=choice.chosen_action,
                    state_before=None,
                    terminal_value=trajectory.history_score,
                    source_sha256=trajectory.source_sha256,
                    provenance=trajectory.sources or ("list_latest_history",),
                )
            )
    return tuple(result)


def _accepted_scope(report: Mapping[str, object], final: NiaFinalRecord) -> NiaTrainingScope:
    request = report.get("request")
    result = report.get("result")
    if not isinstance(request, Mapping) or not isinstance(result, Mapping):
        raise ValueError("accepted report is missing request/result")
    produce_id = _text(request.get("produce_id"), "request.produce_id")
    idol_card_id = _text(request.get("idol_card_id"), "request.idol_card_id")
    profile = get_idol_profile(idol_card_id)
    if profile is None:
        raise ValueError("accepted report idol card is absent from Master")
    if (
        final.mode != produce_id
        or final.idol != idol_card_id
        or final.archetype != profile.plan_type
        or result.get("plan_type") != profile.plan_type
    ):
        raise ValueError("accepted report/journal/Master identity mismatch")
    return NiaTrainingScope(
        produce_id=produce_id,
        idol_card_id=idol_card_id,
        character_id=profile.character_id,
        plan_type=profile.plan_type,
        exam_effect_type=profile.exam_effect_type,
    )


def _accepted_source_sha256(
    report: Mapping[str, object], final: NiaFinalRecord
) -> str:
    """Hash the accepted report/final pair used as one live source artifact."""

    return hashlib.sha256(
        _canonical_json({"report": report, "final": final.to_dict()})
    ).hexdigest()


def _validate_accepted_report(
    report: Mapping[str, object], final: NiaFinalRecord
) -> NiaTrainingScope:
    acceptance = report.get("completion_acceptance")
    result = report.get("result")
    journal = report.get("strategy_journal")
    run_id = report.get("active_run_id")
    date_rollover = (
        journal.get("date_rollover_resume")
        if isinstance(journal, Mapping)
        else None
    )
    uninterrupted_or_accepted_rollover = (
        isinstance(journal, Mapping)
        and (
            journal.get("checkpoint_recovered") is False
            or (
                journal.get("checkpoint_recovered") is True
                and isinstance(date_rollover, Mapping)
                and date_rollover.get("accepted") is True
            )
        )
    )
    started_here_or_accepted_rollover = (
        report.get("run_started_by_request") is True
        or (
            isinstance(journal, Mapping)
            and journal.get("checkpoint_recovered") is True
            and isinstance(date_rollover, Mapping)
            and date_rollover.get("accepted") is True
        )
    )
    if (
        report.get("status") != "completed"
        or report.get("error") is not None
        or not isinstance(acceptance, Mapping)
        or acceptance.get("accepted") is not True
        or report.get("leaderboard_learning_unlocked") is not True
        or not started_here_or_accepted_rollover
        or not isinstance(result, Mapping)
        or result.get("status") != "completed"
        or result.get("stop_reason") != "final-performance-ended"
        or not isinstance(journal, Mapping)
        # Completion acceptance already validates every strict date-rollover
        # binding.  Keep ordinary checkpoint resumes excluded, while allowing
        # that one accepted continuation path to reach the training adapter.
        or not uninterrupted_or_accepted_rollover
        or journal.get("terminal_written") is not True
        or journal.get("error") is not None
        or not isinstance(run_id, str)
        or not run_id
        or journal.get("run_id") != run_id
        or final.run_id != run_id
        or final.passed is not True
        or final.outcome != "completed"
    ):
        raise ValueError("report is not a clean accepted live run")
    ledger = journal.get("audition_ledger")
    rows = ledger.get("rows") if isinstance(ledger, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("accepted report has no durable exam ledger")
    expected_weeks = tuple(
        week for week, _phase in nia_stage_positions(final.mode).values()
    )
    observed_weeks = tuple(
        row.get("week") for row in rows if isinstance(row, Mapping) and row.get("terminal") is True
    )
    identities = tuple(
        row.get("exam_save_identity") for row in rows if isinstance(row, Mapping)
    )
    if observed_weeks != expected_weeks or len(identities) != len(expected_weeks):
        raise ValueError("accepted report exam ledger is incomplete")
    transaction_ids: set[tuple[object, ...]] = set()
    for identity in identities:
        if not isinstance(identity, Mapping) or identity.get("run_binding_id") != run_id:
            raise ValueError("accepted report exam identity is not run-bound")
        transaction = (
            identity.get("exam_source_run_id"),
            identity.get("step_context_id"),
            identity.get("session_transition_id"),
            identity.get("source_sha256"),
        )
        if any(not isinstance(value, str) or not value for value in transaction):
            raise ValueError("accepted report exam transaction is incomplete")
        transaction_ids.add(transaction)
    if len(transaction_ids) != len(expected_weeks):
        raise ValueError("accepted report exam transactions are duplicated")
    return _accepted_scope(report, final)


def accepted_live_behavior_observations(
    report: Mapping[str, object], final: NiaFinalRecord
) -> tuple[NiaTrainingObservation, ...]:
    scope = _validate_accepted_report(report, final)
    source_sha256 = _accepted_source_sha256(report, final)
    raw_choices = final.to_dict()["details"].get("observed_choices")
    if not isinstance(raw_choices, list):
        raise ValueError("accepted final record has no observed choices")

    staged: list[NiaTrainingObservation] = []
    for row in raw_choices:
        if not isinstance(row, Mapping) or row.get("candidate_set_complete") is not True:
            continue
        page = row.get("page")
        raw_kind = row.get("decision_kind")
        if page == "overview" and raw_kind == "choose":
            decision_kind = DECISION_OUTER_ACTION
            candidate_field = "action"
        elif raw_kind == "reward-confirm":
            decision_kind = DECISION_REWARD_CARD
            candidate_field = "card_id"
        else:
            continue
        week = _integer(row.get("week"), "choice.week", minimum=1)
        expected_phase = nia_phase_for_week(scope.produce_id, week)
        if expected_phase is None or row.get("phase") != expected_phase:
            continue
        raw_candidates = row.get("candidates")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            continue
        candidate_ids: list[str] = []
        malformed = False
        for candidate in raw_candidates:
            value = candidate.get(candidate_field) if isinstance(candidate, Mapping) else None
            if not isinstance(value, str) or not value:
                malformed = True
                break
            candidate_ids.append(value)
        chosen = row.get("chosen")
        if malformed or not isinstance(chosen, str):
            continue
        try:
            signature = _candidate_signature(candidate_ids)
        except ValueError:
            continue
        if chosen not in signature:
            continue
        raw_state = row.get("state")
        state_before = dict(raw_state) if isinstance(raw_state, Mapping) else None
        terminal_value: int | float | None = final.final_score
        if terminal_value is None:
            terminal_value = final.state.vote_count
        staged.append(
            NiaTrainingObservation(
                source=SOURCE_ACCEPTED_LIVE,
                source_id=final.run_id,
                scope=scope,
                week=week,
                phase=expected_phase,
                decision_kind=decision_kind,
                candidate_ids=signature,
                chosen_id=chosen,
                state_before=state_before,
                terminal_value=terminal_value,
                source_sha256=source_sha256,
            )
        )

    # Repeated identical observations are harmless restart noise and collapse
    # to one label.  A conflicting choice for the same decision boundary makes
    # that entire boundary ineligible.
    grouped: dict[
        tuple[str, int, int, tuple[str, ...]], list[NiaTrainingObservation]
    ] = defaultdict(list)
    for observation in staged:
        grouped[
            (
                observation.decision_kind,
                observation.week,
                observation.phase,
                observation.candidate_ids,
            )
        ].append(observation)
    result: list[NiaTrainingObservation] = []
    for values in grouped.values():
        if len({value.chosen_id for value in values}) == 1:
            result.append(values[0])
    return tuple(
        sorted(
            result,
            key=lambda value: (
                value.week,
                value.phase,
                value.decision_kind,
                value.candidate_ids,
                value.chosen_id,
            ),
        )
    )


def load_accepted_live_behavior_observations(
    *,
    report_root: str | Path = DEFAULT_COMPLETION_REPORT_ROOT,
    journal_root: str | Path = DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
) -> tuple[NiaTrainingObservation, ...]:
    reports_by_run: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for path in sorted(Path(report_root).glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        acceptance = payload.get("completion_acceptance")
        run_id = payload.get("active_run_id")
        if (
            isinstance(acceptance, Mapping)
            and acceptance.get("accepted") is True
            and isinstance(run_id, str)
            and run_id
        ):
            reports_by_run[run_id].append(payload)

    result: list[NiaTrainingObservation] = []
    for run_id, reports in sorted(reports_by_run.items()):
        if len(reports) != 1:
            continue
        records = read_nia_strategy_journal(Path(journal_root) / f"{run_id}.jsonl")
        finals = tuple(
            record
            for record in records
            if isinstance(record, NiaFinalRecord) and record.run_id == run_id
        )
        if len(finals) != 1:
            continue
        try:
            result.extend(accepted_live_behavior_observations(reports[0], finals[0]))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)


def build_default_nia_training_observations(
    *,
    raw_sources: str | Path | Sequence[str | Path] | None = None,
    report_root: str | Path = DEFAULT_COMPLETION_REPORT_ROOT,
    journal_root: str | Path = DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
) -> tuple[NiaTrainingObservation, ...]:
    result: list[NiaTrainingObservation] = []
    if raw_sources is None:
        if DEFAULT_RAW_HISTORY.is_file():
            result.extend(leaderboard_behavior_observations(DEFAULT_RAW_HISTORY))
    else:
        # An explicitly requested path/list is an input contract: missing or
        # malformed sources surface as an error instead of silently building a
        # live-only dataset.
        result.extend(leaderboard_behavior_observations(raw_sources))
    result.extend(
        load_accepted_live_behavior_observations(
            report_root=report_root,
            journal_root=journal_root,
        )
    )
    return tuple(result)


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Atomically replace one canonical artifact after fsync."""

    import os
    import tempfile

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _training_row_sort_key(row: NiaTrainingObservation) -> tuple[object, ...]:
    return (
        row.source,
        row.source_sha256 or "",
        row.scope.produce_id,
        row.scope.idol_card_id,
        row.scope.character_id,
        row.scope.plan_type,
        row.scope.exam_effect_type,
        row.week,
        row.phase,
        row.decision_kind,
        row.candidate_ids,
        row.chosen_id,
        row.source_id,
    )


def write_nia_training_dataset(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    observations: Sequence[NiaTrainingObservation] | None = None,
    raw_sources: str | Path | Sequence[str | Path] | None = None,
    source: str | Path | Sequence[str | Path] | None = None,
    report_root: str | Path = DEFAULT_COMPLETION_REPORT_ROOT,
    journal_root: str | Path = DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
) -> dict[str, object]:
    if source is not None:
        if raw_sources is not None:
            raise ValueError("specify only one of source and raw_sources")
        raw_sources = source
    raw_collection_manifest: dict[str, object] | None = None
    collection_source = DEFAULT_RAW_HISTORY if raw_sources is None else raw_sources
    if observations is None and (
        raw_sources is not None or DEFAULT_RAW_HISTORY.is_file()
    ):
        raw_collection_manifest = collect_leaderboard_outer_history_sources(
            collection_source
        ).to_manifest()
    rows = tuple(
        build_default_nia_training_observations(
            raw_sources=raw_sources,
            report_root=report_root,
            journal_root=journal_root,
        )
        if observations is None
        else observations
    )
    if not rows:
        raise ValueError("N.I.A. training dataset has no eligible observations")
    rows = tuple(sorted(rows, key=_training_row_sort_key))
    target = Path(output)
    guard_sources = DEFAULT_RAW_HISTORY if raw_sources is None else raw_sources
    if guard_sources is not None:
        target_resolved = target.resolve()
        manifest_resolved = target.with_suffix(".manifest.json").resolve()
        try:
            raw_source_rows = list_leaderboard_raw_sources(guard_sources)
        except FileNotFoundError:
            # The default capture is optional for a live-only local build;
            # an explicitly supplied missing source still fails below when it
            # is used to build observations.
            if raw_sources is None:
                raw_source_rows = ()
            else:
                raise
        for raw_source in raw_source_rows:
            if raw_source.path.resolve() in {target_resolved, manifest_resolved}:
                raise ValueError("N.I.A. dataset output must not overwrite a raw source")
    payload = b"".join(
        json.dumps(row.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        for row in rows
    )
    _atomic_write_bytes(target, payload)
    by_source = Counter(row.source for row in rows)
    by_decision = Counter(row.decision_kind for row in rows)
    by_mode = Counter(row.scope.produce_id for row in rows)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "output_path": str(target.resolve()),
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "observation_count": len(rows),
        "source_count": len({(row.source, row.source_id) for row in rows}),
        "by_source": dict(sorted(by_source.items())),
        "by_decision_kind": dict(sorted(by_decision.items())),
        "by_produce_id": dict(sorted(by_mode.items())),
        "scope_count": len({row.scope.runtime_key for row in rows}),
        "source_sha256": sorted(
            {row.source_sha256 for row in rows if row.source_sha256 is not None}
        ),
        "source_sha256_missing_count": sum(
            1 for row in rows if row.source_sha256 is None
        ),
        "provenance_sources": sorted(
            {source for row in rows for source in row.provenance}
        ),
        "behavior_only_count": sum(1 for row in rows if row.behavior_only),
        "full_rl_transition_count": sum(1 for row in rows if row.full_rl_transition),
    }
    if raw_collection_manifest is not None:
        manifest["raw_history_collection"] = raw_collection_manifest
    manifest_path = target.with_suffix(".manifest.json")
    manifest["manifest_path"] = str(manifest_path.resolve())
    _atomic_write_bytes(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return manifest


def write_nia_training_dry_run_report(
    output: str | Path,
    *,
    raw_sources: str | Path | Sequence[str | Path] = DEFAULT_RAW_HISTORY,
    source: str | Path | Sequence[str | Path] | None = None,
    report_root: str | Path = DEFAULT_COMPLETION_REPORT_ROOT,
    journal_root: str | Path = DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
) -> dict[str, object]:
    """Summarize a prospective build without writing raw or dataset output."""

    if source is not None:
        if raw_sources != DEFAULT_RAW_HISTORY:
            raise ValueError("specify only one of source and raw_sources")
        raw_sources = source
    source_files = list_leaderboard_raw_sources(raw_sources)
    raw_collection_manifest = collect_leaderboard_outer_history_sources(
        raw_sources
    ).to_manifest()
    rows = build_default_nia_training_observations(
        raw_sources=raw_sources,
        report_root=report_root,
        journal_root=journal_root,
    )
    by_source = Counter(row.source for row in rows)
    by_decision = Counter(row.decision_kind for row in rows)
    report = {
        "schema": "gkms.nia-training-dry-run-report.v1",
        "dry_run": True,
        "raw_write": False,
        "dataset_write": False,
        "source_files": [
            {
                "name": source.name,
                "sha256": hashlib.sha256(source.path.read_bytes()).hexdigest(),
            }
            for source in source_files
        ],
        "raw_history_collection": raw_collection_manifest,
        "observation_count": len(rows),
        "by_source": dict(sorted(by_source.items())),
        "by_decision_kind": dict(sorted(by_decision.items())),
        "produce_ids": sorted({row.scope.produce_id for row in rows}),
        "idol_card_ids": sorted({row.scope.idol_card_id for row in rows}),
        "source_sha256": sorted(
            {row.source_sha256 for row in rows if row.source_sha256 is not None}
        ),
        "provenance_sources": sorted(
            {source for row in rows for source in row.provenance}
        ),
        "behavior_only_count": sum(1 for row in rows if row.behavior_only),
        "full_rl_transition_count": sum(1 for row in rows if row.full_rl_transition),
    }
    target = Path(output)
    report["output_path"] = str(target.resolve())
    _atomic_write_bytes(
        target,
        (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return report


def load_nia_training_dataset(
    source: str | Path = DEFAULT_OUTPUT,
) -> tuple[NiaTrainingObservation, ...]:
    path = Path(source)
    rows: list[NiaTrainingObservation] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid training JSONL line {line_number}") from error
        rows.append(NiaTrainingObservation.from_dict(value))
    if not rows:
        raise ValueError("N.I.A. training dataset is empty")
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class NiaExactCandidateBehaviorPrior:
    observation_count: int
    source_count: int
    scope_source_counts: Mapping[tuple[str, str, str, str, str], int]
    decision_scope_source_counts: Mapping[
        tuple[str, str, str, str, str, str], int
    ]
    counts: Mapping[
        tuple[
            str,
            str,
            str,
            str,
            str,
            str,
            int,
            int,
            tuple[str, ...],
            str,
        ],
        int,
    ]
    # Keep the source family attached to every vote key.  The rank itself is
    # still the same bounded behavior prior; this small side channel lets the
    # live telemetry prove whether a selected ordering was backed by
    # leaderboard trajectories, accepted live runs, or both.
    source_kinds_by_key: Mapping[
        tuple[
            str,
            str,
            str,
            str,
            str,
            str,
            int,
            int,
            tuple[str, ...],
            str,
        ],
        frozenset[str],
    ] = field(default_factory=dict)
    schema: str = PRIOR_SCHEMA
    _last_rank_provenance: tuple[str, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.schema != PRIOR_SCHEMA or self.observation_count < 1 or self.source_count < 1:
            raise ValueError("exact-candidate behavior prior requires observations")

    @classmethod
    def from_observations(
        cls, observations: Sequence[NiaTrainingObservation]
    ) -> "NiaExactCandidateBehaviorPrior":
        eligible = tuple(observations)
        if not eligible:
            raise ValueError("training dataset has no behavior observations")
        counts = Counter(
            (
                row.decision_kind,
                *row.scope.runtime_key,
                row.week,
                row.phase,
                row.candidate_ids,
                row.chosen_id,
            )
            for row in eligible
        )
        scope_sources: dict[
            tuple[str, str, str, str, str], set[tuple[str, str]]
        ] = defaultdict(set)
        decision_scope_sources: dict[
            tuple[str, str, str, str, str, str], set[tuple[str, str]]
        ] = defaultdict(set)
        source_kinds_by_key: dict[
            tuple[
                str,
                str,
                str,
                str,
                str,
                str,
                int,
                int,
                tuple[str, ...],
                str,
            ],
            set[str],
        ] = defaultdict(set)
        for row in eligible:
            scope_sources[row.scope.runtime_key].add((row.source, row.source_id))
            decision_scope_sources[(row.decision_kind, *row.scope.runtime_key)].add(
                (row.source, row.source_id)
            )
            source_kinds_by_key[
                (
                    row.decision_kind,
                    *row.scope.runtime_key,
                    row.week,
                    row.phase,
                    row.candidate_ids,
                    row.chosen_id,
                )
            ].add(row.source)
        return cls(
            observation_count=len(eligible),
            source_count=len({(row.source, row.source_id) for row in eligible}),
            scope_source_counts={
                scope: len(sources) for scope, sources in scope_sources.items()
            },
            decision_scope_source_counts={
                scope: len(sources)
                for scope, sources in decision_scope_sources.items()
            },
            counts=dict(counts),
            source_kinds_by_key={
                key: frozenset(kinds) for key, kinds in source_kinds_by_key.items()
            },
        )

    @property
    def last_rank_provenance(self) -> tuple[str, ...] | None:
        """Source families that supplied the most recent successful rank."""

        return self._last_rank_provenance

    @property
    def rank_provenance(self) -> tuple[str, ...] | None:
        """Compatibility alias used by the bounded live adapter."""

        return self._last_rank_provenance

    @staticmethod
    def _scope_from_features(
        features: Mapping[str, object],
    ) -> tuple[str, str, str, str, str] | None:
        values = tuple(
            features.get(name)
            for name in (
                "produce_id",
                "idol_card_id",
                "character_id",
                "plan_type",
                "exam_effect_type",
            )
        )
        if any(not isinstance(value, str) or not value for value in values):
            return None
        return values  # type: ignore[return-value]

    def _rank_kind(
        self,
        decision_kind: str,
        features: Mapping[str, object],
        legal_ids: Sequence[str],
    ) -> tuple[str, ...]:
        object.__setattr__(self, "_last_rank_provenance", None)
        scope = self._scope_from_features(features)
        week = features.get("week")
        phase = features.get("phase")
        legal = tuple(dict.fromkeys(value for value in legal_ids if isinstance(value, str)))
        if (
            decision_kind not in {DECISION_OUTER_ACTION, DECISION_REWARD_CARD}
            or
            scope is None
            or isinstance(week, bool)
            or not isinstance(week, int)
            or isinstance(phase, bool)
            or not isinstance(phase, int)
            or not legal
        ):
            return ()
        if (
            self.decision_scope_source_counts.get((decision_kind, *scope), 0)
            < MIN_SCOPE_SOURCES
        ):
            return ()
        signature = tuple(sorted(legal))
        scores = {
            action: self.counts.get(
                (decision_kind, *scope, week, phase, signature, action), 0
            )
            for action in legal
        }
        if max(scores.values(), default=0) <= 0:
            return ()
        provenance: set[str] = set()
        for action, score in scores.items():
            if score <= 0:
                continue
            provenance.update(
                self.source_kinds_by_key.get(
                    (
                        decision_kind,
                        *scope,
                        week,
                        phase,
                        signature,
                        action,
                    ),
                    frozenset(),
                )
            )
        object.__setattr__(
            self,
            "_last_rank_provenance",
            tuple(sorted(provenance)) or None,
        )
        order = {action: index for index, action in enumerate(legal)}
        return tuple(sorted(legal, key=lambda action: (-scores[action], order[action])))

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]:
        return self._rank_kind(DECISION_OUTER_ACTION, features, legal_actions)

    def rank_reward(
        self,
        features: Mapping[str, object],
        legal_card_ids: Sequence[str],
    ) -> tuple[str, ...]:
        return self._rank_kind(DECISION_REWARD_CARD, features, legal_card_ids)


@dataclass(frozen=True, slots=True)
class NiaBroadCandidateBehaviorPrior:
    """Cross-idol outer-action prior for one produce/archetype/effect.

    Broad evidence intentionally drops only the idol-card and character
    dimensions.  ``produce_id``, ``plan_type`` and ``exam_effect_type`` stay
    in every key, so Pro/Master and Review/Aggressive evidence can never mix.
    A broad source is still one ``(source, source_id)`` trajectory; repeated
    rows from that trajectory are de-duplicated before votes are counted.

    This class is outer-only.  Reward-card ranking remains exact-card scoped
    through :class:`NiaExactCandidateBehaviorPrior`.
    """

    observation_count: int
    source_count: int
    scope_source_counts: Mapping[BroadScopeKey, int]
    decision_scope_source_counts: Mapping[tuple[str, str, str, str], int]
    scope_idol_card_counts: Mapping[BroadScopeKey, int]
    counts: Mapping[
        tuple[
            str,
            str,
            str,
            str,
            int,
            int,
            tuple[str, ...],
            str,
        ],
        int,
    ]
    source_action_counts: Mapping[
        tuple[
            str,
            str,
            str,
            str,
            int,
            int,
            tuple[str, ...],
            str,
        ],
        frozenset[SourceKey],
    ] = field(default_factory=dict)
    source_kinds_by_key: Mapping[
        tuple[
            str,
            str,
            str,
            str,
            int,
            int,
            tuple[str, ...],
            str,
        ],
        frozenset[str],
    ] = field(default_factory=dict)
    schema: str = BROAD_PRIOR_SCHEMA
    _last_rank_provenance: tuple[str, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            self.schema != BROAD_PRIOR_SCHEMA
            or self.observation_count < 1
            or self.source_count < 1
        ):
            raise ValueError("broad outer behavior prior requires observations")

    @staticmethod
    def _scope_from_features(
        features: Mapping[str, object],
    ) -> BroadScopeKey | None:
        values = tuple(features.get(name) for name in (
            "produce_id",
            "plan_type",
            "exam_effect_type",
        ))
        if any(not isinstance(value, str) or not value for value in values):
            return None
        return values  # type: ignore[return-value]

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[NiaTrainingObservation],
    ) -> "NiaBroadCandidateBehaviorPrior":
        outer = tuple(
            row for row in observations if row.decision_kind == DECISION_OUTER_ACTION
        )
        if not outer:
            raise ValueError("training dataset has no outer-action observations")

        scope_sources: dict[BroadScopeKey, set[SourceKey]] = defaultdict(set)
        scope_cards: dict[BroadScopeKey, set[str]] = defaultdict(set)
        decision_scope_sources: dict[tuple[str, str, str, str], set[SourceKey]] = defaultdict(set)
        counts: Counter[
            tuple[str, str, str, str, int, int, tuple[str, ...], str]
        ] = Counter()
        source_actions: dict[
            tuple[str, str, str, str, int, int, tuple[str, ...], str], set[SourceKey]
        ] = defaultdict(set)
        source_kinds_by_key: dict[
            tuple[
                str,
                str,
                str,
                str,
                int,
                int,
                tuple[str, ...],
                str,
            ],
            set[str],
        ] = defaultdict(set)
        # A source is one trajectory.  The broad row identity deliberately
        # omits idol_card_id/character_id so a duplicated export of one source
        # cannot add another vote merely because its exact scope differs.
        seen_rows: set[tuple[object, ...]] = set()
        for row in outer:
            broad = (
                row.scope.produce_id,
                row.scope.plan_type,
                row.scope.exam_effect_type,
            )
            source = (row.source, row.source_id)
            scope_sources[broad].add(source)
            scope_cards[broad].add(row.scope.idol_card_id)
            decision_scope = (row.decision_kind, *broad)
            decision_scope_sources[decision_scope].add(source)
            row_key = (
                source,
                *broad,
                row.week,
                row.phase,
                row.candidate_ids,
                row.chosen_id,
            )
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            count_key = (
                row.decision_kind,
                *broad,
                row.week,
                row.phase,
                row.candidate_ids,
                row.chosen_id,
            )
            counts[count_key] += 1
            source_actions[count_key].add(source)
            source_kinds_by_key[count_key].add(row.source)

        return cls(
            observation_count=len(seen_rows),
            source_count=len({(row.source, row.source_id) for row in outer}),
            scope_source_counts={
                scope: len(sources) for scope, sources in scope_sources.items()
            },
            decision_scope_source_counts={
                scope: len(sources)
                for scope, sources in decision_scope_sources.items()
            },
            scope_idol_card_counts={
                scope: len(cards) for scope, cards in scope_cards.items()
            },
            counts=dict(counts),
            source_action_counts={
                key: frozenset(sources) for key, sources in source_actions.items()
            },
            source_kinds_by_key={
                key: frozenset(kinds) for key, kinds in source_kinds_by_key.items()
            },
        )

    @property
    def last_rank_provenance(self) -> tuple[str, ...] | None:
        """Source families that supplied the most recent successful rank."""

        return self._last_rank_provenance

    @property
    def rank_provenance(self) -> tuple[str, ...] | None:
        """Compatibility alias used by the bounded live adapter."""

        return self._last_rank_provenance

    def _matching_signatures(
        self,
        decision_kind: str,
        broad: BroadScopeKey,
        week: int,
        phase: int,
        legal: tuple[str, ...],
    ) -> tuple[tuple[str, ...], ...]:
        legal_set = set(legal)
        signatures = {
            key[6]
            for key in self.counts
            if key[:6] == (decision_kind, *broad, week, phase)
        }
        exact = tuple(sorted(legal))
        if exact in signatures:
            return (exact,)
        # The runtime owns legality.  A runtime-only candidate is retained in
        # the result with score zero, while a learned signature that is wholly
        # contained in the visible legal set can still rank known actions.
        return tuple(
            sorted(
                (signature for signature in signatures if set(signature) <= legal_set),
                key=lambda signature: (len(signature), signature),
            )
        )

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]:
        object.__setattr__(self, "_last_rank_provenance", None)
        broad = self._scope_from_features(features)
        week = features.get("week")
        phase = features.get("phase")
        legal = tuple(
            dict.fromkeys(value for value in legal_actions if isinstance(value, str))
        )
        if (
            broad is None
            or isinstance(week, bool)
            or not isinstance(week, int)
            or isinstance(phase, bool)
            or not isinstance(phase, int)
            or not legal
        ):
            return ()
        decision_scope = (DECISION_OUTER_ACTION, *broad)
        if (
            self.decision_scope_source_counts.get(decision_scope, 0)
            < MIN_SCOPE_SOURCES
            or self.scope_idol_card_counts.get(broad, 0) < MIN_BROAD_IDOL_CARDS
        ):
            return ()
        signatures = self._matching_signatures(
            DECISION_OUTER_ACTION, broad, week, phase, legal
        )
        if not signatures:
            return ()
        scores: dict[str, int] = {}
        for action in legal:
            source_keys: set[SourceKey] = set()
            score = 0
            for signature in signatures:
                key = (
                    DECISION_OUTER_ACTION,
                    *broad,
                    week,
                    phase,
                    signature,
                    action,
                )
                observed_sources = self.source_action_counts.get(key)
                if observed_sources is not None:
                    source_keys.update(observed_sources)
                else:
                    score += self.counts.get(key, 0)
            # Prefer source units over raw row counts whenever the source map
            # is available, which makes duplicate rows from one trajectory a
            # single vote even when several candidate signatures match.
            scores[action] = len(source_keys) if source_keys else score
        if max(scores.values(), default=0) <= 0:
            return ()
        provenance: set[str] = set()
        for action, score in scores.items():
            if score <= 0:
                continue
            for signature in signatures:
                provenance.update(
                    self.source_kinds_by_key.get(
                        (
                            DECISION_OUTER_ACTION,
                            *broad,
                            week,
                            phase,
                            signature,
                            action,
                        ),
                        frozenset(),
                    )
                )
        object.__setattr__(
            self,
            "_last_rank_provenance",
            tuple(sorted(provenance)) or None,
        )
        order = {action: index for index, action in enumerate(legal)}
        return tuple(sorted(legal, key=lambda action: (-scores[action], order[action])))


@dataclass(frozen=True, slots=True)
class NiaHierarchicalCandidateBehaviorPrior:
    """Exact-card outer ranking with broad inter-card fallback.

    The exact prior is always attempted first.  Broad evidence is consulted
    only when exact ranking abstains (including its independent-source gate),
    and only ``rank``/outer actions use that fallback.  ``rank_reward`` stays
    exact-card scoped.
    """

    exact_prior: NiaExactCandidateBehaviorPrior
    broad_prior: NiaBroadCandidateBehaviorPrior | None
    schema: str = HIERARCHICAL_PRIOR_SCHEMA
    _last_rank_source: str | None = field(default=None, init=False, repr=False, compare=False)
    _last_rank_provenance: tuple[str, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.schema != HIERARCHICAL_PRIOR_SCHEMA:
            raise ValueError("unsupported hierarchical N.I.A. prior schema")
        if not isinstance(self.exact_prior, NiaExactCandidateBehaviorPrior):
            raise TypeError("exact_prior must be NiaExactCandidateBehaviorPrior")
        if self.broad_prior is not None and not isinstance(
            self.broad_prior, NiaBroadCandidateBehaviorPrior
        ):
            raise TypeError("broad_prior must be NiaBroadCandidateBehaviorPrior or None")

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[NiaTrainingObservation],
    ) -> "NiaHierarchicalCandidateBehaviorPrior":
        exact = NiaExactCandidateBehaviorPrior.from_observations(observations)
        try:
            broad: NiaBroadCandidateBehaviorPrior | None = (
                NiaBroadCandidateBehaviorPrior.from_observations(observations)
            )
        except ValueError:
            broad = None
        return cls(exact_prior=exact, broad_prior=broad)

    @property
    def last_rank_source(self) -> str | None:
        """Source of the latest outer rank: ``exact``, ``broad``, or ``None``."""

        return self._last_rank_source

    @property
    def last_rank_provenance(self) -> tuple[str, ...] | None:
        """Source families that supplied the most recent successful rank."""

        return self._last_rank_provenance

    @property
    def rank_provenance(self) -> tuple[str, ...] | None:
        """Compatibility alias used by the bounded live adapter."""

        return self._last_rank_provenance

    @property
    def rank_source(self) -> str | None:
        """Compatibility alias used by telemetry consumers."""

        return self._last_rank_source

    def rank_with_source(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[tuple[str, ...], str | None]:
        exact = tuple(self.exact_prior.rank(features, legal_actions))
        if exact:
            object.__setattr__(self, "_last_rank_source", RANK_SOURCE_EXACT)
            object.__setattr__(
                self,
                "_last_rank_provenance",
                self.exact_prior.last_rank_provenance,
            )
            return exact, RANK_SOURCE_EXACT
        broad = (
            ()
            if self.broad_prior is None
            else tuple(self.broad_prior.rank(features, legal_actions))
        )
        source = RANK_SOURCE_BROAD if broad else None
        object.__setattr__(self, "_last_rank_source", source)
        object.__setattr__(
            self,
            "_last_rank_provenance",
            None
            if not broad or self.broad_prior is None
            else self.broad_prior.last_rank_provenance,
        )
        return broad, source

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]:
        ranked, _source = self.rank_with_source(features, legal_actions)
        return ranked

    def rank_reward(
        self,
        features: Mapping[str, object],
        legal_card_ids: Sequence[str],
    ) -> tuple[str, ...]:
        ranked = tuple(self.exact_prior.rank_reward(features, legal_card_ids))
        object.__setattr__(
            self,
            "_last_rank_source",
            RANK_SOURCE_EXACT if ranked else None,
        )
        object.__setattr__(
            self,
            "_last_rank_provenance",
            self.exact_prior.last_rank_provenance if ranked else None,
        )
        return ranked


# The outer-specific names make the boundary obvious to new callers while
# keeping a short candidate-prior name available to existing integrations.
NiaBroadOuterBehaviorPrior = NiaBroadCandidateBehaviorPrior
NiaHierarchicalOuterBehaviorPrior = NiaHierarchicalCandidateBehaviorPrior


@lru_cache(maxsize=2)
def _load_prior_cached(path_text: str) -> NiaHierarchicalCandidateBehaviorPrior:
    return NiaHierarchicalCandidateBehaviorPrior.from_observations(
        load_nia_training_dataset(path_text)
    )


@lru_cache(maxsize=2)
def _load_public_prior_cached(path_text: str, mtime_ns: int, size: int) -> NiaHierarchicalCandidateBehaviorPrior:
    from .portable_behavior_assets import read_behavior_prior
    return read_behavior_prior(path_text)


def _read_public_prior(path: Path) -> NiaHierarchicalCandidateBehaviorPrior:
    stat = path.stat()
    return _load_public_prior_cached(str(path), stat.st_mtime_ns, stat.st_size)


def load_nia_exact_candidate_behavior_prior(
    source: str | Path = DEFAULT_OUTPUT,
) -> NiaHierarchicalCandidateBehaviorPrior:
    if Path(source) == DEFAULT_OUTPUT:
        from .portable_behavior_assets import load_public_behavior_role
        projected = load_public_behavior_role("behavior_count_prior", _read_public_prior)
        if projected is not None:
            return projected
    return _load_prior_cached(str(Path(source).resolve()))


def try_load_default_nia_exact_candidate_behavior_prior(
) -> NiaHierarchicalCandidateBehaviorPrior | None:
    try:
        return load_nia_exact_candidate_behavior_prior()
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None


def _evaluate_nia_training_shadow_reference(
    observations: Sequence[NiaTrainingObservation],
) -> dict[str, object]:
    """Reference implementation of leave-one-source-out shadow evaluation.

    This intentionally mirrors the original straightforward implementation.
    It remains available for small-fixture equivalence tests while the public
    evaluator below uses aggregated sufficient statistics for large corpora.

    This is a behavior-prior evaluation, not an online reward estimate.  Exact
    card evidence is tried first; when it abstains, the same
    produce/plan/effect broad aggregate is allowed under its independent
    trajectory and inter-card gates.  Full RL promotion remains false until
    real state transitions and a held-out live completion comparison exist.
    """

    outer = tuple(
        row for row in observations if row.decision_kind == DECISION_OUTER_ACTION
    )
    if not outer:
        raise ValueError("shadow evaluation has no outer-action observations")
    source_keys = tuple(sorted({(row.source, row.source_id) for row in outer}))
    scope_sources: dict[
        tuple[str, str, str, str, str], set[tuple[str, str]]
    ] = defaultdict(set)
    for row in outer:
        scope_sources[row.scope.runtime_key].add((row.source, row.source_id))

    totals = {
        "observations": 0,
        "evaluated": 0,
        "abstained": 0,
        "top1_matches": 0,
        "legal_violations": 0,
        "exact_ranked": 0,
        "broad_ranked": 0,
    }
    broad_scope_sources: dict[BroadScopeKey, set[SourceKey]] = defaultdict(set)
    broad_scope_cards: dict[BroadScopeKey, set[str]] = defaultdict(set)
    for row in outer:
        broad_scope = (
            row.scope.produce_id,
            row.scope.plan_type,
            row.scope.exam_effect_type,
        )
        broad_scope_sources[broad_scope].add((row.source, row.source_id))
        broad_scope_cards[broad_scope].add(row.scope.idol_card_id)
    per_scope: dict[tuple[str, str, str, str, str], dict[str, int]] = {
        scope: {
            "source_count": len(sources),
            "observations": 0,
            "evaluated": 0,
            "abstained": 0,
            "top1_matches": 0,
            "legal_violations": 0,
            "exact_ranked": 0,
            "broad_ranked": 0,
        }
        for scope, sources in scope_sources.items()
    }

    for held_out in source_keys:
        training = tuple(
            row
            for row in observations
            if (row.source, row.source_id) != held_out
        )
        try:
            prior = NiaHierarchicalCandidateBehaviorPrior.from_observations(training)
        except ValueError:
            prior = None
        for row in outer:
            if (row.source, row.source_id) != held_out:
                continue
            scope_metrics = per_scope[row.scope.runtime_key]
            totals["observations"] += 1
            scope_metrics["observations"] += 1
            features = {
                "produce_id": row.scope.produce_id,
                "idol_card_id": row.scope.idol_card_id,
                "character_id": row.scope.character_id,
                "plan_type": row.scope.plan_type,
                "exam_effect_type": row.scope.exam_effect_type,
                "week": row.week,
                "phase": row.phase,
            }
            if prior is None:
                ranked = ()
                rank_source = None
            else:
                ranked, rank_source = prior.rank_with_source(
                    features, row.candidate_ids
                )
            if not ranked:
                totals["abstained"] += 1
                scope_metrics["abstained"] += 1
                continue
            if rank_source == RANK_SOURCE_EXACT:
                totals["exact_ranked"] += 1
                scope_metrics["exact_ranked"] += 1
            elif rank_source == RANK_SOURCE_BROAD:
                totals["broad_ranked"] += 1
                scope_metrics["broad_ranked"] += 1
            illegal = tuple(action for action in ranked if action not in row.candidate_ids)
            if illegal or len(ranked) != len(set(ranked)):
                totals["legal_violations"] += 1
                scope_metrics["legal_violations"] += 1
                continue
            totals["evaluated"] += 1
            scope_metrics["evaluated"] += 1
            if ranked[0] == row.chosen_id:
                totals["top1_matches"] += 1
                scope_metrics["top1_matches"] += 1

    scope_rows: list[dict[str, object]] = []
    behavior_ready: list[dict[str, object]] = []
    for scope, metrics in sorted(per_scope.items()):
        evaluated = metrics["evaluated"]
        row = {
            "scope": {
                "produce_id": scope[0],
                "idol_card_id": scope[1],
                "character_id": scope[2],
                "plan_type": scope[3],
                "exam_effect_type": scope[4],
            },
            **metrics,
            "broad_scope": {
                "produce_id": scope[0],
                "plan_type": scope[3],
                "exam_effect_type": scope[4],
            },
            "broad_source_count": len(
                broad_scope_sources.get((scope[0], scope[3], scope[4]), set())
            ),
            "broad_idol_card_count": len(
                broad_scope_cards.get((scope[0], scope[3], scope[4]), set())
            ),
            "coverage": (
                evaluated / metrics["observations"]
                if metrics["observations"]
                else 0.0
            ),
            "top1_accuracy": (
                metrics["top1_matches"] / evaluated if evaluated else None
            ),
        }
        ready = bool(
            (
                metrics["source_count"] >= MIN_SCOPE_SOURCES
                or (
                    row["broad_source_count"] >= MIN_SCOPE_SOURCES
                    and row["broad_idol_card_count"] >= MIN_BROAD_IDOL_CARDS
                )
            )
            and evaluated > 0
            and metrics["legal_violations"] == 0
        )
        row["behavior_prior_data_ready"] = ready
        scope_rows.append(row)
        if ready:
            behavior_ready.append(row["scope"])

    evaluated = totals["evaluated"]
    return {
        "schema": "gkms.nia-training-shadow-evaluation.v1",
        "source_count": len(source_keys),
        **totals,
        "coverage": (
            evaluated / totals["observations"] if totals["observations"] else 0.0
        ),
        "top1_accuracy": (
            totals["top1_matches"] / evaluated if evaluated else None
        ),
        "scopes": scope_rows,
        "behavior_prior_ready_scopes": behavior_ready,
        "rank_source_counts": {
            RANK_SOURCE_EXACT: totals["exact_ranked"],
            RANK_SOURCE_BROAD: totals["broad_ranked"],
        },
        "full_rl_transition_count": sum(
            1 for row in observations if row.full_rl_transition
        ),
        "full_rl_policy_ready": False,
        "full_rl_blockers": [
            "full state_before/action/state_after transitions are unavailable",
            "held-out live completion A/B has not been run",
        ],
    }


_ShadowExactCountKey = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    int,
    int,
    tuple[str, ...],
    str,
]
_ShadowExactDecisionScopeKey = tuple[str, str, str, str, str, str]
_ShadowBroadContextKey = tuple[BroadScopeKey, int, int]


@dataclass(slots=True)
class _NiaShadowSufficientStatistics:
    """Aggregates needed to rank every held-out row exactly once.

    The old evaluator rebuilt a prior for every source and then scanned every
    observation for that source.  These maps retain the same row/source
    counts, so leaving out one source is a subtraction/set difference rather
    than a materialized ``training`` tuple.
    """

    exact_counts: Counter[_ShadowExactCountKey]
    exact_source_counts: dict[_ShadowExactCountKey, Counter[SourceKey]]
    exact_decision_scope_sources: dict[_ShadowExactDecisionScopeKey, set[SourceKey]]
    broad_context_sources: dict[
        _ShadowBroadContextKey,
        dict[tuple[str, ...], dict[str, set[SourceKey]]],
    ]
    broad_scope_sources: dict[BroadScopeKey, set[SourceKey]]
    broad_scope_card_count: dict[BroadScopeKey, int]
    broad_scope_single_source_card_counts: dict[BroadScopeKey, Counter[SourceKey]]

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[NiaTrainingObservation],
    ) -> "_NiaShadowSufficientStatistics":
        exact_counts: Counter[_ShadowExactCountKey] = Counter()
        exact_source_counts: dict[_ShadowExactCountKey, Counter[SourceKey]] = defaultdict(
            Counter
        )
        exact_decision_scope_sources: dict[
            _ShadowExactDecisionScopeKey, set[SourceKey]
        ] = defaultdict(set)
        broad_context_sources: dict[
            _ShadowBroadContextKey,
            dict[tuple[str, ...], dict[str, set[SourceKey]]],
        ] = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
        broad_scope_sources: dict[BroadScopeKey, set[SourceKey]] = defaultdict(set)
        broad_scope_cards: dict[
            BroadScopeKey, dict[SourceKey, set[str]]
        ] = defaultdict(lambda: defaultdict(set))

        for row in observations:
            source = (row.source, row.source_id)
            scope = row.scope.runtime_key
            exact_key: _ShadowExactCountKey = (
                row.decision_kind,
                *scope,
                row.week,
                row.phase,
                row.candidate_ids,
                row.chosen_id,
            )
            exact_counts[exact_key] += 1
            exact_source_counts[exact_key][source] += 1
            exact_decision_scope_sources[
                (row.decision_kind, *scope)
            ].add(source)

            if row.decision_kind != DECISION_OUTER_ACTION:
                continue
            broad: BroadScopeKey = (
                row.scope.produce_id,
                row.scope.plan_type,
                row.scope.exam_effect_type,
            )
            context: _ShadowBroadContextKey = (broad, row.week, row.phase)
            # ``NiaBroadCandidateBehaviorPrior`` de-duplicates one identical
            # row per source before counting.  A set per signature preserves
            # exactly that behavior and also lets LOO remove one source.
            broad_context_sources[context][row.candidate_ids][row.chosen_id].add(
                source
            )
            broad_scope_sources[broad].add(source)
            broad_scope_cards[broad][source].add(row.scope.idol_card_id)

        broad_scope_card_count: dict[BroadScopeKey, int] = {}
        broad_scope_single_source_card_counts: dict[
            BroadScopeKey, Counter[SourceKey]
        ] = {}
        for broad, source_cards in broad_scope_cards.items():
            cards_by_source: dict[str, set[SourceKey]] = defaultdict(set)
            for source, card_ids in source_cards.items():
                for card_id in card_ids:
                    cards_by_source[card_id].add(source)
            broad_scope_card_count[broad] = len(cards_by_source)
            single_source = Counter(
                source
                for sources in cards_by_source.values()
                if len(sources) == 1
                for source in sources
            )
            broad_scope_single_source_card_counts[broad] = single_source

        return cls(
            exact_counts=exact_counts,
            exact_source_counts=dict(exact_source_counts),
            exact_decision_scope_sources=dict(exact_decision_scope_sources),
            broad_context_sources={
                context: {
                    signature: dict(actions)
                    for signature, actions in signatures.items()
                }
                for context, signatures in broad_context_sources.items()
            },
            broad_scope_sources=dict(broad_scope_sources),
            broad_scope_card_count=broad_scope_card_count,
            broad_scope_single_source_card_counts=(
                broad_scope_single_source_card_counts
            ),
        )

    @staticmethod
    def _without_source(values: set[SourceKey], held_out: SourceKey) -> int:
        return len(values) - int(held_out in values)

    def exact_rank(
        self,
        row: NiaTrainingObservation,
        held_out: SourceKey,
    ) -> tuple[str, ...]:
        scope = row.scope.runtime_key
        decision_scope = (row.decision_kind, *scope)
        if (
            self._without_source(
                self.exact_decision_scope_sources.get(decision_scope, set()),
                held_out,
            )
            < MIN_SCOPE_SOURCES
        ):
            return ()
        legal = tuple(
            dict.fromkeys(value for value in row.candidate_ids if isinstance(value, str))
        )
        if not legal:
            return ()
        signature = tuple(sorted(legal))
        source_counts = self.exact_source_counts
        scores: dict[str, int] = {}
        for action in legal:
            key: _ShadowExactCountKey = (
                DECISION_OUTER_ACTION,
                *scope,
                row.week,
                row.phase,
                signature,
                action,
            )
            score = self.exact_counts.get(key, 0)
            score -= source_counts.get(key, {}).get(held_out, 0)
            scores[action] = score
        if max(scores.values(), default=0) <= 0:
            return ()
        order = {action: index for index, action in enumerate(legal)}
        return tuple(sorted(legal, key=lambda action: (-scores[action], order[action])))

    def broad_rank(
        self,
        row: NiaTrainingObservation,
        held_out: SourceKey,
    ) -> tuple[str, ...]:
        broad: BroadScopeKey = (
            row.scope.produce_id,
            row.scope.plan_type,
            row.scope.exam_effect_type,
        )
        scope_sources = self.broad_scope_sources.get(broad, set())
        source_count = self._without_source(scope_sources, held_out)
        card_count = self.broad_scope_card_count.get(broad, 0)
        card_count -= self.broad_scope_single_source_card_counts.get(
            broad, {}
        ).get(held_out, 0)
        if source_count < MIN_SCOPE_SOURCES or card_count < MIN_BROAD_IDOL_CARDS:
            return ()

        legal = tuple(
            dict.fromkeys(value for value in row.candidate_ids if isinstance(value, str))
        )
        if not legal:
            return ()
        context = (broad, row.week, row.phase)
        signature_actions = self.broad_context_sources.get(context, {})
        active_signatures = tuple(
            sorted(
                (
                    signature
                    for signature, actions in signature_actions.items()
                    if any(
                        source != held_out
                        for sources in actions.values()
                        for source in sources
                    )
                )
            )
        )
        if not active_signatures:
            return ()
        exact_signature = tuple(sorted(legal))
        if exact_signature in active_signatures:
            signatures = (exact_signature,)
        else:
            legal_set = set(legal)
            signatures = tuple(
                sorted(
                    (
                        signature
                        for signature in active_signatures
                        if set(signature) <= legal_set
                    ),
                    key=lambda signature: (len(signature), signature),
                )
            )
        if not signatures:
            return ()

        scores: dict[str, int] = {}
        for action in legal:
            source_keys: set[SourceKey] = set()
            for signature in signatures:
                source_keys.update(
                    source
                    for source in signature_actions.get(signature, {}).get(
                        action, set()
                    )
                    if source != held_out
                )
            scores[action] = len(source_keys)
        if max(scores.values(), default=0) <= 0:
            return ()
        order = {action: index for index, action in enumerate(legal)}
        return tuple(sorted(legal, key=lambda action: (-scores[action], order[action])))


def evaluate_nia_training_shadow(
    observations: Sequence[NiaTrainingObservation],
) -> dict[str, object]:
    """Evaluate the hierarchical prior with exact, near-linear LOO ranking.

    The result intentionally has the same schema and metrics as the original
    evaluator.  It builds sufficient statistics once, subtracts the current
    held-out source while ranking each row, and therefore scales with rows
    (plus the local candidate/signature sets) instead of
    ``source_count * observation_count``.
    """

    values = tuple(observations)
    outer = tuple(
        row for row in values if row.decision_kind == DECISION_OUTER_ACTION
    )
    if not outer:
        raise ValueError("shadow evaluation has no outer-action observations")

    source_keys = tuple(sorted({(row.source, row.source_id) for row in outer}))
    scope_sources: dict[
        tuple[str, str, str, str, str], set[tuple[str, str]]
    ] = defaultdict(set)
    broad_scope_sources: dict[BroadScopeKey, set[SourceKey]] = defaultdict(set)
    broad_scope_cards: dict[BroadScopeKey, set[str]] = defaultdict(set)
    for row in outer:
        scope_sources[row.scope.runtime_key].add((row.source, row.source_id))
        broad_scope = (
            row.scope.produce_id,
            row.scope.plan_type,
            row.scope.exam_effect_type,
        )
        broad_scope_sources[broad_scope].add((row.source, row.source_id))
        broad_scope_cards[broad_scope].add(row.scope.idol_card_id)

    totals = {
        "observations": 0,
        "evaluated": 0,
        "abstained": 0,
        "top1_matches": 0,
        "legal_violations": 0,
        "exact_ranked": 0,
        "broad_ranked": 0,
    }
    per_scope: dict[tuple[str, str, str, str, str], dict[str, int]] = {
        scope: {
            "source_count": len(sources),
            "observations": 0,
            "evaluated": 0,
            "abstained": 0,
            "top1_matches": 0,
            "legal_violations": 0,
            "exact_ranked": 0,
            "broad_ranked": 0,
        }
        for scope, sources in scope_sources.items()
    }

    stats = _NiaShadowSufficientStatistics.from_observations(values)
    for row in outer:
        held_out = (row.source, row.source_id)
        scope_metrics = per_scope[row.scope.runtime_key]
        totals["observations"] += 1
        scope_metrics["observations"] += 1
        ranked = stats.exact_rank(row, held_out)
        rank_source = RANK_SOURCE_EXACT if ranked else None
        if not ranked:
            ranked = stats.broad_rank(row, held_out)
            rank_source = RANK_SOURCE_BROAD if ranked else None
        if not ranked:
            totals["abstained"] += 1
            scope_metrics["abstained"] += 1
            continue
        if rank_source == RANK_SOURCE_EXACT:
            totals["exact_ranked"] += 1
            scope_metrics["exact_ranked"] += 1
        elif rank_source == RANK_SOURCE_BROAD:
            totals["broad_ranked"] += 1
            scope_metrics["broad_ranked"] += 1
        illegal = tuple(action for action in ranked if action not in row.candidate_ids)
        if illegal or len(ranked) != len(set(ranked)):
            totals["legal_violations"] += 1
            scope_metrics["legal_violations"] += 1
            continue
        totals["evaluated"] += 1
        scope_metrics["evaluated"] += 1
        if ranked[0] == row.chosen_id:
            totals["top1_matches"] += 1
            scope_metrics["top1_matches"] += 1

    scope_rows: list[dict[str, object]] = []
    behavior_ready: list[dict[str, object]] = []
    for scope, metrics in sorted(per_scope.items()):
        evaluated = metrics["evaluated"]
        broad = (scope[0], scope[3], scope[4])
        row = {
            "scope": {
                "produce_id": scope[0],
                "idol_card_id": scope[1],
                "character_id": scope[2],
                "plan_type": scope[3],
                "exam_effect_type": scope[4],
            },
            **metrics,
            "broad_scope": {
                "produce_id": broad[0],
                "plan_type": broad[1],
                "exam_effect_type": broad[2],
            },
            "broad_source_count": len(broad_scope_sources.get(broad, set())),
            "broad_idol_card_count": len(broad_scope_cards.get(broad, set())),
            "coverage": (
                evaluated / metrics["observations"]
                if metrics["observations"]
                else 0.0
            ),
            "top1_accuracy": (
                metrics["top1_matches"] / evaluated if evaluated else None
            ),
        }
        ready = bool(
            (
                metrics["source_count"] >= MIN_SCOPE_SOURCES
                or (
                    row["broad_source_count"] >= MIN_SCOPE_SOURCES
                    and row["broad_idol_card_count"] >= MIN_BROAD_IDOL_CARDS
                )
            )
            and evaluated > 0
            and metrics["legal_violations"] == 0
        )
        row["behavior_prior_data_ready"] = ready
        scope_rows.append(row)
        if ready:
            behavior_ready.append(row["scope"])

    evaluated = totals["evaluated"]
    return {
        "schema": "gkms.nia-training-shadow-evaluation.v1",
        "source_count": len(source_keys),
        **totals,
        "coverage": (
            evaluated / totals["observations"] if totals["observations"] else 0.0
        ),
        "top1_accuracy": (
            totals["top1_matches"] / evaluated if evaluated else None
        ),
        "scopes": scope_rows,
        "behavior_prior_ready_scopes": behavior_ready,
        "rank_source_counts": {
            RANK_SOURCE_EXACT: totals["exact_ranked"],
            RANK_SOURCE_BROAD: totals["broad_ranked"],
        },
        "full_rl_transition_count": sum(
            1 for row in values if row.full_rl_transition
        ),
        "full_rl_policy_ready": False,
        "full_rl_blockers": [
            "full state_before/action/state_after transitions are unavailable",
            "held-out live completion A/B has not been run",
        ],
    }


# Kept as a named opt-in helper for fixture equivalence/performance audits;
# production callers should use the sufficient-statistics evaluator above.
evaluate_nia_training_shadow_reference = _evaluate_nia_training_shadow_reference


def write_nia_training_shadow_evaluation(
    output: str | Path | None = None,
    *,
    observations: Sequence[NiaTrainingObservation] | None = None,
) -> dict[str, object]:
    rows = tuple(load_nia_training_dataset() if observations is None else observations)
    payload = evaluate_nia_training_shadow(rows)
    target = (
        DEFAULT_OUTPUT.with_name("shadow_evaluation.json")
        if output is None
        else Path(output)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {**payload, "output_path": str(target.resolve())}


__all__ = [
    "DECISION_OUTER_ACTION",
    "DECISION_REWARD_CARD",
    "BROAD_PRIOR_SCHEMA",
    "DEFAULT_OUTPUT",
    "HIERARCHICAL_PRIOR_SCHEMA",
    "MANIFEST_SCHEMA",
    "MIN_BROAD_IDOL_CARDS",
    "MIN_SCOPE_SOURCES",
    "NiaBroadCandidateBehaviorPrior",
    "NiaBroadOuterBehaviorPrior",
    "NiaExactCandidateBehaviorPrior",
    "NiaHierarchicalCandidateBehaviorPrior",
    "NiaHierarchicalOuterBehaviorPrior",
    "NiaTrainingObservation",
    "NiaTrainingScope",
    "OBSERVATION_SCHEMA",
    "PRIOR_SCHEMA",
    "RANK_SOURCE_BROAD",
    "RANK_SOURCE_EXACT",
    "SOURCE_ACCEPTED_LIVE",
    "SOURCE_LEADERBOARD_OUTER",
    "accepted_live_behavior_observations",
    "build_default_nia_training_observations",
    "leaderboard_behavior_observations",
    "load_accepted_live_behavior_observations",
    "load_nia_exact_candidate_behavior_prior",
    "load_nia_training_dataset",
    "try_load_default_nia_exact_candidate_behavior_prior",
    "write_nia_training_dataset",
    "write_nia_training_dry_run_report",
    "evaluate_nia_training_shadow",
    "evaluate_nia_training_shadow_reference",
    "write_nia_training_shadow_evaluation",
]
