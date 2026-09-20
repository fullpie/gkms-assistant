"""Small, fail-closed composition adapter for N.I.A. outer priors.

The existing outer advisers own route, stamina, Master, and Maa fallback
policy.  This module only validates the optional learned ordering at the last
boundary: the caller must provide a complete, duplicate-free candidate set
and the prior must return the same complete set with evidence.  Any missing,
partial, out-of-scope, or malformed result is an abstention, so the caller's
existing heuristic/native order remains unchanged.

No screen, save, controller, or game API is accessed here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class BoundedOuterPrior(Protocol):
    """Minimal prior surface used by the production composition seam."""

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> Sequence[str]: ...


@dataclass(frozen=True, slots=True)
class BoundedCandidateRanking:
    """Auditable result of one bounded prior query."""

    candidate_ids: tuple[str, ...]
    ranked: tuple[str, ...]
    used: bool
    source: str | None
    reason: str
    candidate_set_complete: bool
    provenance: tuple[str, ...] = ()

    @property
    def abstained(self) -> bool:
        return not self.used

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_ids": list(self.candidate_ids),
            "ranked": list(self.ranked),
            "used": self.used,
            "source": self.source,
            "reason": self.reason,
            "candidate_set_complete": self.candidate_set_complete,
            "abstained": self.abstained,
            "provenance": list(self.provenance),
        }


def _candidate_ids(value: object) -> tuple[str, ...] | None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Sequence
    ):
        return None
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        return None
    if not result or len(result) != len(set(result)):
        return None
    return result


def _source(prior: object, explicit: object | None = None) -> str | None:
    if isinstance(explicit, str) and explicit:
        return explicit
    for name in ("rank_source", "last_rank_source"):
        value = getattr(prior, name, None)
        if isinstance(value, str) and value:
            return value
    # A complete ranking is itself the prior's evidence contract for legacy
    # priors that predate rank_with_source().
    return type(prior).__name__ if prior is not None else None


def _provenance(prior: object) -> tuple[str, ...]:
    """Read optional training-source provenance without making it required."""

    value = getattr(prior, "last_rank_provenance", None)
    if value is None:
        value = getattr(prior, "rank_provenance", None)
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(
        item for item in value if isinstance(item, str) and item
    )


def _call_rank(
    prior: object,
    features: Mapping[str, object],
    candidates: tuple[str, ...],
    *,
    decision_kind: str,
) -> tuple[object, str | None]:
    if decision_kind == "reward_card":
        method = getattr(prior, "rank_reward", None)
        if not callable(method):
            return (), None
        return method(features, candidates), None

    with_source = getattr(prior, "rank_with_source", None)
    if callable(with_source):
        result = with_source(features, candidates)
        if (
            isinstance(result, tuple)
            and len(result) == 2
            and isinstance(result[1], (str, type(None)))
        ):
            return result[0], result[1]
        # Do not reinterpret an unexpected tuple as a ranking.
        return result, None
    method = getattr(prior, "rank", None)
    if not callable(method):
        return (), None
    return method(features, candidates), None


def rank_bounded_candidates(
    prior: object | None,
    features: Mapping[str, object],
    candidate_ids: Sequence[str],
    *,
    candidate_set_complete: bool = True,
    decision_kind: str = "outer_action",
) -> BoundedCandidateRanking:
    """Return a prior ranking only when it is a complete legal permutation.

    ``candidate_set_complete`` is caller authority.  The helper deliberately
    does not infer completeness from the number of rows, because a partial UI
    read can contain a perfectly valid-looking non-empty subset.
    """

    if not isinstance(features, Mapping):
        raise TypeError("features must be a mapping")
    candidates = _candidate_ids(candidate_ids)
    if candidates is None:
        return BoundedCandidateRanking(
            (),
            (),
            False,
            None,
            "candidate-set-invalid-or-duplicate",
            False,
        )
    if type(candidate_set_complete) is not bool:
        raise TypeError("candidate_set_complete must be boolean")
    if not candidate_set_complete:
        return BoundedCandidateRanking(
            candidates,
            (),
            False,
            None,
            "candidate-set-incomplete",
            False,
        )
    if prior is None:
        return BoundedCandidateRanking(
            candidates,
            (),
            False,
            None,
            "prior-missing",
            True,
        )
    if decision_kind not in {"outer_action", "reward_card"}:
        raise ValueError("unsupported decision_kind")
    try:
        raw_rank, explicit_source = _call_rank(
            prior,
            features,
            candidates,
            decision_kind=decision_kind,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return BoundedCandidateRanking(
            candidates,
            (),
            False,
            None,
            "prior-query-failed",
            True,
        )
    provenance = _provenance(prior)
    ranked = _candidate_ids(raw_rank)
    if ranked is None:
        return BoundedCandidateRanking(
            candidates,
            (),
            False,
            None,
            "prior-ranking-invalid-or-duplicate",
            True,
        )
    if set(ranked) != set(candidates):
        return BoundedCandidateRanking(
            candidates,
            ranked,
            False,
            _source(prior, explicit_source),
            "prior-ranking-incomplete-or-out-of-scope",
            True,
            provenance,
        )
    return BoundedCandidateRanking(
        candidates,
        ranked,
        True,
        _source(prior, explicit_source),
        "prior-ranking-complete",
        True,
        provenance,
    )


class BoundedNiaOuterPrior:
    """Adapter that keeps the existing :func:`advise_nia_outer` unchanged."""

    def __init__(self, prior: object | None) -> None:
        self.prior = prior
        self._last_rank_source: str | None = None
        self._last_rank_provenance: tuple[str, ...] = ()
        self._last_result: BoundedCandidateRanking | None = None

    @property
    def last_rank_source(self) -> str | None:
        return self._last_rank_source

    @property
    def rank_source(self) -> str | None:
        return self._last_rank_source

    @property
    def last_result(self) -> BoundedCandidateRanking | None:
        return self._last_result

    @property
    def last_rank_provenance(self) -> tuple[str, ...]:
        return self._last_rank_provenance

    @property
    def rank_provenance(self) -> tuple[str, ...]:
        return self._last_rank_provenance

    def _rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
        *,
        decision_kind: str,
    ) -> tuple[str, ...]:
        result = rank_bounded_candidates(
            self.prior,
            features,
            legal_actions,
            decision_kind=decision_kind,
        )
        self._last_result = result
        self._last_rank_source = result.source if result.used else None
        self._last_rank_provenance = result.provenance if result.used else ()
        return result.ranked if result.used else ()

    def rank(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[str, ...]:
        return self._rank(features, legal_actions, decision_kind="outer_action")

    def rank_with_source(
        self,
        features: Mapping[str, object],
        legal_actions: Sequence[str],
    ) -> tuple[tuple[str, ...], str | None]:
        ranked = self.rank(features, legal_actions)
        return ranked, self._last_rank_source

    def rank_reward(
        self,
        features: Mapping[str, object],
        legal_card_ids: Sequence[str],
    ) -> tuple[str, ...]:
        return self._rank(features, legal_card_ids, decision_kind="reward_card")


def build_bounded_nia_outer_advisor(
    advisor: Callable[..., object],
    prior: object | None,
    *,
    fixed_kwargs: Mapping[str, object] | None = None,
) -> Callable[..., object]:
    """Compose a bounded prior around an existing outer adviser.

    The existing adviser remains the sole owner of route/stamina/Maa fallback
    policy.  This wrapper only replaces the optional prior object with the
    complete-permutation adapter.
    """

    if not callable(advisor):
        raise TypeError("advisor must be callable")
    if fixed_kwargs is not None and not isinstance(fixed_kwargs, Mapping):
        raise TypeError("fixed_kwargs must be a mapping or None")
    bounded = None if prior is None else BoundedNiaOuterPrior(prior)
    fixed = {} if fixed_kwargs is None else dict(fixed_kwargs)

    def wrapped(snapshot: object, *args: object, **kwargs: object) -> object:
        payload = dict(fixed)
        payload.update(kwargs)
        payload["learned_prior"] = bounded
        return advisor(snapshot, *args, **payload)

    setattr(wrapped, "_bounded_outer_prior", bounded)
    return wrapped


__all__ = [
    "BoundedCandidateRanking",
    "BoundedNiaOuterPrior",
    "BoundedOuterPrior",
    "build_bounded_nia_outer_advisor",
    "rank_bounded_candidates",
]
