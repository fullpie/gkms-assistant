"""Weak outer-action prior learned only from accepted completed NIA journals.

The stored journals do not contain complete candidate sets or next-state
transitions, so this is not an RL policy.  It is intentionally limited to
ranking actions that the live adviser has already proved visible and legal.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
import json
from pathlib import Path
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .nia_strategy_journal import (
    DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
    NiaFinalRecord,
    read_nia_strategy_journal,
)
from .nia_route_profile import nia_final_week, nia_phase_for_week
from .overview_actions import ACTIVITY, DANCE_LESSON, REST, VOCAL_LESSON, VISUAL_LESSON
from .route_calendar import BUSINESS, OUTING, SPECIAL_GUIDANCE


SCHEMA = "gkms.nia-accepted-outer-prior.v1"
DEFAULT_COMPLETION_REPORT_ROOT = Path(__file__).resolve().parents[2] / "var" / "nia_live"
_LEARNABLE_ACTIONS = frozenset(
    {
        ACTIVITY,
        BUSINESS,
        DANCE_LESSON,
        OUTING,
        REST,
        SPECIAL_GUIDANCE,
        VOCAL_LESSON,
        VISUAL_LESSON,
        "care_package",
        "class",
        "consultation",
    }
)

# An accepted run is only reusable for the exact N.I.A. identity that
# produced it.  In particular, ``produce-004`` and ``produce-005`` have
# different route calendars, and the same Plan2 archetype is not evidence for
# another idol card.  Keep this key deliberately small: it is the identity
# boundary, not a learned feature vector.
OuterScopeKey = tuple[str, str, str, str, str]


def _scope_from_features(features: Mapping[str, object]) -> OuterScopeKey | None:
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


@runtime_checkable
class NiaOuterPrior(Protocol):
    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class NiaAcceptedOuterPrior:
    accepted_run_count: int
    observation_count: int
    exact_counts: Mapping[tuple[str, str, str, str, str, int, int, str], int]
    phase_counts: Mapping[tuple[str, str, str, str, str, int, str], int]
    global_phase_counts: Mapping[tuple[str, str, str, str, str, int, str], int]
    overall_counts: Mapping[tuple[str, str, str, str, str, str], int]
    scope_keys: tuple[OuterScopeKey, ...] = ()
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA or self.accepted_run_count < 1 or self.observation_count < 1:
            raise ValueError("accepted outer prior requires completed observations")

    def action_score(self, features: Mapping[str, object], action: str) -> int:
        scope = _scope_from_features(features)
        phase = features.get("phase")
        week = features.get("week")
        if (
            scope is None
            or isinstance(phase, bool)
            or not isinstance(phase, int)
            or isinstance(week, bool)
            or not isinstance(week, int)
        ):
            return 0
        produce_id, idol_card_id, character_id, plan_type, exam_effect_type = scope
        scope_values = (
            produce_id,
            idol_card_id,
            character_id,
            plan_type,
            exam_effect_type,
        )
        return (
            8 * self.exact_counts.get((*scope_values, phase, week, action), 0)
            + 4 * self.phase_counts.get((*scope_values, phase, action), 0)
            + 2 * self.global_phase_counts.get((*scope_values, phase, action), 0)
            + self.overall_counts.get((*scope_values, action), 0)
        )

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]:
        legal = tuple(dict.fromkeys(action for action in legal_actions if isinstance(action, str)))
        scored = {action: self.action_score(features, action) for action in legal}
        if not scored or max(scored.values(), default=0) <= 0:
            return ()
        order = {action: index for index, action in enumerate(legal)}
        return tuple(sorted(legal, key=lambda action: (-scored[action], order[action])))


@dataclass(frozen=True, slots=True)
class NiaCompositeOuterPrior:
    """Use the first scoped prior with evidence; later priors are fallbacks."""

    priors: tuple[NiaOuterPrior, ...]
    _last_rank_source: str | None = field(default=None, init=False, repr=False, compare=False)
    _last_rank_provenance: tuple[str, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def last_rank_source(self) -> str | None:
        """Expose exact/broad provenance for decision telemetry."""

        return self._last_rank_source

    @property
    def last_rank_provenance(self) -> tuple[str, ...] | None:
        """Expose source families behind the most recent ordering."""

        return self._last_rank_provenance

    @property
    def rank_provenance(self) -> tuple[str, ...] | None:
        """Compatibility alias used by bounded production telemetry."""

        return self._last_rank_provenance

    def rank_with_source(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[tuple[str, ...], str | None]:
        for prior in self.priors:
            rank_with_source = getattr(prior, "rank_with_source", None)
            if callable(rank_with_source):
                ranked, source = rank_with_source(features, legal_actions)
            else:
                ranked = tuple(prior.rank(features, legal_actions))
                source = getattr(prior, "last_rank_source", None)
            ranked = tuple(ranked)
            if ranked:
                object.__setattr__(self, "_last_rank_source", source)
                provenance = getattr(prior, "last_rank_provenance", None)
                if provenance is None:
                    provenance = getattr(prior, "rank_provenance", None)
                if isinstance(provenance, (tuple, list)):
                    provenance = tuple(
                        value
                        for value in provenance
                        if isinstance(value, str) and value
                    ) or None
                else:
                    provenance = None
                object.__setattr__(self, "_last_rank_provenance", provenance)
                return ranked, source
        object.__setattr__(self, "_last_rank_source", None)
        object.__setattr__(self, "_last_rank_provenance", None)
        return (), None

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]:
        ranked, _source = self.rank_with_source(features, legal_actions)
        return ranked


def compose_nia_outer_priors(
    *priors: NiaOuterPrior | None,
) -> NiaOuterPrior | None:
    available = tuple(value for value in priors if value is not None)
    if not available:
        return None
    if len(available) == 1:
        return available[0]
    return NiaCompositeOuterPrior(available)


def _accepted_run_scopes(report_root: Path) -> Mapping[str, OuterScopeKey]:
    """Return accepted runs with their exact Master-derived identity.

    The completion gate proves that a run finished, but it intentionally does
    not make the run's actions globally reusable.  Resolve character, plan,
    and exam archetype from the same idol-card Master row used by the runtime;
    malformed or identity-incomplete reports are simply not a prior source.
    """

    from .master_db import get_idol_profile

    accepted: dict[str, OuterScopeKey] = {}
    for path in sorted(report_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeError):
            continue
        acceptance = (
            payload.get("completion_acceptance")
            if isinstance(payload, Mapping)
            else None
        )
        request = payload.get("request") if isinstance(payload, Mapping) else None
        result = payload.get("result") if isinstance(payload, Mapping) else None
        journal = (
            payload.get("strategy_journal")
            if isinstance(payload, Mapping)
            else None
        )
        run_id = payload.get("active_run_id") if isinstance(payload, Mapping) else None
        produce_id = request.get("produce_id") if isinstance(request, Mapping) else None
        idol_card_id = request.get("idol_card_id") if isinstance(request, Mapping) else None
        result_plan = result.get("plan_type") if isinstance(result, Mapping) else None
        if (
            isinstance(acceptance, Mapping)
            and acceptance.get("accepted") is True
            and payload.get("leaderboard_learning_unlocked") is True
            and isinstance(journal, Mapping)
            and journal.get("checkpoint_recovered") is False
            and isinstance(run_id, str)
            and run_id
            and isinstance(produce_id, str)
            and produce_id
            and isinstance(idol_card_id, str)
            and idol_card_id
        ):
            try:
                profile = get_idol_profile(idol_card_id)
            except (OSError, TypeError, ValueError):
                profile = None
            if (
                profile is None
                or result_plan != profile.plan_type
                or not profile.character_id
                or not profile.exam_effect_type
            ):
                continue
            accepted[run_id] = (
                produce_id,
                idol_card_id,
                profile.character_id,
                profile.plan_type,
                profile.exam_effect_type,
            )
    return accepted


@lru_cache(maxsize=4)
def _load_cached(report_root_text: str, journal_root_text: str) -> NiaAcceptedOuterPrior:
    report_root = Path(report_root_text)
    journal_root = Path(journal_root_text)
    accepted_run_scopes = _accepted_run_scopes(report_root)
    observations: list[tuple[OuterScopeKey, int, int, str]] = []
    accepted_runs: set[str] = set()
    for run_id in sorted(accepted_run_scopes):
        scope = accepted_run_scopes[run_id]
        path = journal_root / f"{run_id}.jsonl"
        try:
            records = read_nia_strategy_journal(path)
        except (OSError, TypeError, ValueError):
            continue
        final = next(
            (
                value
                for value in records
                if isinstance(value, NiaFinalRecord)
                and value.run_id == run_id
                and value.passed
                and value.outcome == "completed"
            ),
            None,
        )
        if final is None:
            continue
        if final.mode != scope[0] or final.idol != scope[1] or final.archetype != scope[3]:
            continue
        raw_choices = final.details.get("observed_choices")
        if not isinstance(raw_choices, Sequence) or isinstance(raw_choices, (str, bytes)):
            continue
        by_week: dict[int, set[str]] = defaultdict(set)
        for row in raw_choices:
            if not isinstance(row, Mapping):
                continue
            week = row.get("week")
            chosen = row.get("chosen")
            if (
                row.get("page") == "overview"
                and row.get("decision_kind") == "choose"
                and isinstance(week, int)
                and not isinstance(week, bool)
                and 1 <= week <= nia_final_week(final.mode)
                and isinstance(chosen, str)
                and chosen in _LEARNABLE_ACTIONS
            ):
                by_week[week].add(chosen)
        added = False
        for week, choices in sorted(by_week.items()):
            # A week with conflicting actions is a recovery/retry artifact, not
            # a trustworthy label.  Repeated identical clicks count only once.
            if len(choices) != 1:
                continue
            chosen = next(iter(choices))
            phase = nia_phase_for_week(final.mode, week)
            if phase is None:
                continue
            observations.append((scope, phase, week, chosen))
            added = True
        if added:
            accepted_runs.add(run_id)
    if not observations:
        raise ValueError("accepted NIA journals contain no unambiguous outer choices")

    exact = Counter(
        (*scope, phase, week, action)
        for scope, phase, week, action in observations
    )
    phase = Counter(
        (*scope, phase, action)
        for scope, phase, _week, action in observations
    )
    # Keep the former phase-level fallback, but never let it cross the exact
    # run identity boundary.
    global_phase = Counter(
        (*scope, phase, action)
        for scope, phase, _week, action in observations
    )
    overall = Counter(
        (*scope, action) for scope, _phase, _week, action in observations
    )
    return NiaAcceptedOuterPrior(
        accepted_run_count=len(accepted_runs),
        observation_count=len(observations),
        exact_counts=dict(exact),
        phase_counts=dict(phase),
        global_phase_counts=dict(global_phase),
        overall_counts=dict(overall),
        scope_keys=tuple(sorted({scope for scope, _phase, _week, _action in observations})),
    )


def load_nia_accepted_outer_prior(
    *,
    report_root: str | Path = DEFAULT_COMPLETION_REPORT_ROOT,
    journal_root: str | Path = DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
) -> NiaAcceptedOuterPrior:
    return _load_cached(str(Path(report_root).resolve()), str(Path(journal_root).resolve()))


def try_load_default_nia_accepted_outer_prior() -> NiaAcceptedOuterPrior | None:
    try:
        return load_nia_accepted_outer_prior()
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_COMPLETION_REPORT_ROOT",
    "NiaAcceptedOuterPrior",
    "NiaCompositeOuterPrior",
    "NiaOuterPrior",
    "OuterScopeKey",
    "SCHEMA",
    "load_nia_accepted_outer_prior",
    "compose_nia_outer_priors",
    "try_load_default_nia_accepted_outer_prior",
]
