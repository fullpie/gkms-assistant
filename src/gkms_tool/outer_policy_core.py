"""Pure, plan-neutral scoring for one authoritative outer candidate set.

The caller owns state extraction, targets and candidate effects.  This module
does not inspect a screen, mutate state, call Maa, or invent missing targets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Mapping

from .nia_strategy_journal import NiaCandidateScores, NiaStrategyState


SCHEMA = "gkms.outer-policy-core.v1"
STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"
ATTRIBUTES = ("vocal", "dance", "visual")


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _finite(value: object, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"{label} must be finite")
    return result


def _attrs(
    value: Mapping[str, object], label: str, *, complete: bool
) -> Mapping[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    if set(value).difference(ATTRIBUTES) or (
        complete and set(value) != set(ATTRIBUTES)
    ):
        raise ValueError(f"{label} must use only Vo/Da/Vi fields")
    return MappingProxyType(
        {
            name: _nonnegative(value.get(name, 0), f"{label}.{name}")
            for name in ATTRIBUTES
        }
    )


@dataclass(frozen=True, slots=True)
class OuterDeckSummary:
    card_count: int
    upgraded_card_count: int
    upgradeable_card_count: int
    removable_card_count: int

    def __post_init__(self) -> None:
        values = tuple(
            _nonnegative(getattr(self, name), f"deck.{name}")
            for name in (
                "card_count",
                "upgraded_card_count",
                "upgradeable_card_count",
                "removable_card_count",
            )
        )
        if any(value > values[0] for value in values[1:]):
            raise ValueError("deck summary counts cannot exceed card_count")


@dataclass(frozen=True, slots=True)
class OuterPolicyTargets:
    next_audition_in_weeks: int
    final_in_weeks: int
    stamina_reserve: int
    next_audition: Mapping[str, object] | None
    final: Mapping[str, object] | None
    source: str = ""

    def __post_init__(self) -> None:
        next_weeks = _nonnegative(
            self.next_audition_in_weeks, "targets.next_audition_in_weeks"
        )
        final_weeks = _nonnegative(self.final_in_weeks, "targets.final_in_weeks")
        if final_weeks < next_weeks:
            raise ValueError("final target cannot precede the next audition")
        _nonnegative(self.stamina_reserve, "targets.stamina_reserve")
        if self.next_audition is not None:
            object.__setattr__(
                self,
                "next_audition",
                _attrs(self.next_audition, "targets.next_audition", complete=True),
            )
        if self.final is not None:
            object.__setattr__(
                self, "final", _attrs(self.final, "targets.final", complete=True)
            )
        if not isinstance(self.source, str) or (
            self.source and not self.source.strip()
        ):
            raise ValueError("targets.source must be text")

    @property
    def authoritative(self) -> bool:
        return (
            bool(self.source)
            and self.next_audition is not None
            and self.final is not None
        )


@dataclass(frozen=True, slots=True)
class OuterPolicyWeights:
    """The only Plan-dependent values accepted by this shared flow."""

    vocal: float = 1.0
    dance: float = 1.0
    visual: float = 1.0
    attribute_target_deficit: float = 1.0
    audition_readiness: float = 1.0
    deck_mutation_value: float = 1.0
    recovery_feasibility: float = 1.0
    learned_prior: float = 1.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = _finite(
                getattr(self, name), f"weights.{name}", nonnegative=True
            )
            if name == "learned_prior" and value > 1.0:
                raise ValueError("weights.learned_prior must be a 0..1 mixture")
            object.__setattr__(
                self,
                name,
                value,
            )

    @property
    def attributes(self) -> Mapping[str, float]:
        return {name: getattr(self, name) for name in ATTRIBUTES}


@dataclass(frozen=True, slots=True)
class OuterPolicyCandidate:
    candidate_id: str
    kind: str
    attribute_gains: Mapping[str, object] = field(default_factory=dict)
    stamina_cost: int = 0
    stamina_recovery: int = 0
    produce_point_cost: int = 0
    ticket_cost: int = 0
    deck_mutation_value: float = 0.0
    upgrade_count: int = 0
    remove_count: int = 0

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.kind:
            raise ValueError("candidate ID and kind must be non-empty")
        object.__setattr__(
            self,
            "attribute_gains",
            _attrs(self.attribute_gains, "candidate.attribute_gains", complete=False),
        )
        for name in (
            "stamina_cost",
            "stamina_recovery",
            "produce_point_cost",
            "ticket_cost",
            "upgrade_count",
            "remove_count",
        ):
            _nonnegative(getattr(self, name), f"candidate.{name}")
        _finite(self.deck_mutation_value, "candidate.deck_mutation_value")


@dataclass(frozen=True, slots=True)
class OuterPolicyRequest:
    mode: str
    scheduled_step_types: tuple[str, ...]
    week: int
    stage: str
    legal_candidate_ids: tuple[str, ...]
    state: NiaStrategyState
    growth_permil: Mapping[str, object]
    deck: OuterDeckSummary | None
    targets: OuterPolicyTargets
    plan_weights: OuterPolicyWeights
    candidates: tuple[OuterPolicyCandidate, ...]
    learned_prior_scores: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.mode or not self.stage or self.week < 1:
            raise ValueError("mode/stage/week identity is incomplete")
        if not self.scheduled_step_types or any(
            not isinstance(value, str) or not value
            for value in self.scheduled_step_types
        ):
            raise ValueError("scheduled_step_types is incomplete")
        if not self.legal_candidate_ids or len(self.legal_candidate_ids) != len(
            set(self.legal_candidate_ids)
        ):
            raise ValueError("legal_candidate_ids must be non-empty and unique")
        if not isinstance(self.state, NiaStrategyState):
            raise TypeError("state must be NiaStrategyState")
        if self.deck is not None and not isinstance(self.deck, OuterDeckSummary):
            raise TypeError("deck must be OuterDeckSummary or None")
        object.__setattr__(
            self,
            "growth_permil",
            _attrs(self.growth_permil, "growth_permil", complete=True),
        )
        ids = tuple(value.candidate_id for value in self.candidates)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be non-empty and unique")
        priors: dict[str, float] = {}
        for candidate_id, value in self.learned_prior_scores.items():
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError("learned prior candidate ID is invalid")
            priors[candidate_id] = _finite(value, f"prior.{candidate_id}")
        object.__setattr__(
            self, "learned_prior_scores", MappingProxyType(priors)
        )


@dataclass(frozen=True, slots=True)
class OuterPolicyBlocker:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class OuterPolicyCandidateEvaluation:
    candidate_id: str
    kind: str
    eligible: bool
    scores: NiaCandidateScores | None
    raw_components: Mapping[str, float]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "raw_components", MappingProxyType(dict(self.raw_components))
        )


@dataclass(frozen=True, slots=True)
class OuterPolicyDecision:
    status: str
    chosen_id: str | None
    evaluations: tuple[OuterPolicyCandidateEvaluation, ...]
    blockers: tuple[OuterPolicyBlocker, ...] = ()
    fallback_required: bool = False
    schema: str = SCHEMA

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY


def _blocked(
    request: OuterPolicyRequest, code: str, detail: str
) -> OuterPolicyDecision:
    return OuterPolicyDecision(
        STATUS_BLOCKED,
        None,
        tuple(
            OuterPolicyCandidateEvaluation(
                value.candidate_id,
                value.kind,
                False,
                None,
                {},
                (f"policy blocked: {code}",),
            )
            for value in request.candidates
        ),
        (OuterPolicyBlocker(code, detail),),
        True,
    )


def _hard_constraint(
    request: OuterPolicyRequest, candidate: OuterPolicyCandidate
) -> str | None:
    state = request.state
    checks = (
        (
            candidate.candidate_id not in request.legal_candidate_ids,
            "not present in authoritative legal candidate IDs",
        ),
        (candidate.stamina_cost > state.stamina, "unaffordable stamina cost"),
        (
            candidate.produce_point_cost > state.produce_points,
            "unaffordable produce-point cost",
        ),
        (candidate.ticket_cost > state.vote_count, "unaffordable ticket cost"),
        (
            request.deck is not None
            and candidate.upgrade_count > request.deck.upgradeable_card_count,
            "upgrade count exceeds deck summary",
        ),
        (
            request.deck is not None
            and candidate.remove_count > request.deck.removable_card_count,
            "remove count exceeds deck summary",
        ),
    )
    return next((reason for failed, reason in checks if failed), None)


def _gains(
    request: OuterPolicyRequest, candidate: OuterPolicyCandidate
) -> Mapping[str, int]:
    return {
        name: candidate.attribute_gains[name]
        * (1000 + request.growth_permil[name])
        // 1000
        for name in ATTRIBUTES
    }


def _reduction(
    current: Mapping[str, int],
    gains: Mapping[str, int],
    target: Mapping[str, object],
) -> tuple[Mapping[str, int], Mapping[str, int]]:
    deficits = {
        name: max(0, int(target[name]) - current[name]) for name in ATTRIBUTES
    }
    reduced = {name: min(deficits[name], gains[name]) for name in ATTRIBUTES}
    return reduced, {name: deficits[name] - reduced[name] for name in ATTRIBUTES}


def rank_outer_policy(request: OuterPolicyRequest) -> OuterPolicyDecision:
    """Return scores and one choice without game/controller side effects."""

    if not isinstance(request, OuterPolicyRequest):
        raise TypeError("request must be OuterPolicyRequest")
    specs = {value.candidate_id for value in request.candidates}
    missing = tuple(
        value for value in request.legal_candidate_ids if value not in specs
    )
    if missing:
        return _blocked(
            request,
            "outer-policy-candidate-metadata-unavailable",
            "missing candidate metadata: " + ", ".join(missing),
        )
    failures = {
        value.candidate_id: _hard_constraint(request, value)
        for value in request.candidates
    }
    deck_dependent = tuple(
        value.candidate_id
        for value in request.candidates
        if failures[value.candidate_id] is None
        and (
            value.deck_mutation_value != 0
            or value.upgrade_count > 0
            or value.remove_count > 0
        )
    )
    if request.deck is None and deck_dependent:
        return _blocked(
            request,
            "outer-policy-deck-summary-unavailable",
            "deck summary is required by an otherwise eligible candidate: "
            + ", ".join(deck_dependent),
        )
    eligible_ids = tuple(key for key, reason in failures.items() if reason is None)
    if not eligible_ids:
        return _blocked(
            request,
            "outer-policy-no-affordable-candidate",
            "every candidate was illegal or unaffordable",
        )
    if not request.targets.authoritative:
        return _blocked(
            request,
            "outer-policy-target-unavailable",
            "next-audition and final targets require an explicit source",
        )
    if request.targets.stamina_reserve > request.state.max_stamina:
        return _blocked(
            request,
            "outer-policy-stamina-target-invalid",
            "stamina reserve exceeds max stamina",
        )
    priors = {
        value: request.learned_prior_scores.get(value, 0.0) for value in eligible_ids
    }
    prior_scale = max((abs(value) for value in priors.values()), default=0.0)
    current = {name: getattr(request.state, name) for name in ATTRIBUTES}
    attribute_weights = request.plan_weights.attributes
    order = {value: index for index, value in enumerate(request.legal_candidate_ids)}
    evaluations: list[OuterPolicyCandidateEvaluation] = []

    for candidate in request.candidates:
        failure = failures[candidate.candidate_id]
        if failure is not None:
            evaluations.append(
                OuterPolicyCandidateEvaluation(
                    candidate.candidate_id,
                    candidate.kind,
                    False,
                    None,
                    {},
                    (f"filtered: {failure}",),
                )
            )
            continue
        gains = _gains(request, candidate)
        final_reduced, final_residual = _reduction(
            current, gains, request.targets.final  # type: ignore[arg-type]
        )
        next_reduced, next_residual = _reduction(
            current, gains, request.targets.next_audition  # type: ignore[arg-type]
        )
        final_urgency = 1.0 + 1.0 / max(1, request.targets.final_in_weeks)
        next_urgency = 1.0 + 2.0 / max(
            1, request.targets.next_audition_in_weeks
        )
        raw_attribute = sum(final_reduced.values()) * final_urgency
        raw_audition = (
            sum(next_reduced.values()) * next_urgency
            - sum(next_residual.values())
            / max(20.0, request.targets.next_audition_in_weeks * 20.0)
        )
        weighted_attribute = sum(
            final_reduced[name] * attribute_weights[name] for name in ATTRIBUTES
        ) * final_urgency
        weighted_audition = (
            sum(
                next_reduced[name] * attribute_weights[name]
                for name in ATTRIBUTES
            )
            * next_urgency
            - sum(
                next_residual[name] * attribute_weights[name]
                for name in ATTRIBUTES
            )
            / max(20.0, request.targets.next_audition_in_weeks * 20.0)
        )
        raw_deck = 0.0
        if request.deck is not None:
            raw_deck = (
                candidate.deck_mutation_value
                + candidate.upgrade_count
                / max(1, request.deck.upgradeable_card_count)
                + candidate.remove_count
                / max(1, request.deck.removable_card_count)
            ) / max(1, request.deck.card_count)
        after_hp = min(
            request.state.max_stamina,
            request.state.stamina
            - candidate.stamina_cost
            + candidate.stamina_recovery,
        )
        before_hp_gap = max(
            0, request.targets.stamina_reserve - request.state.stamina
        )
        after_hp_gap = max(0, request.targets.stamina_reserve - after_hp)
        recovery_urgency = 1.0 + 1.0 / max(
            1, request.targets.next_audition_in_weeks
        )
        raw_recovery = (
            (before_hp_gap - after_hp_gap) * recovery_urgency
            - after_hp_gap * recovery_urgency
            - max(0, candidate.stamina_cost - candidate.stamina_recovery) * 0.25
        )
        raw_prior = (
            0.0
            if not prior_scale
            else priors[candidate.candidate_id] / prior_scale
        )
        raw = {
            "attribute_target_deficit": raw_attribute,
            "audition_readiness": raw_audition,
            "deck_mutation_value": raw_deck,
            "recovery_feasibility": raw_recovery,
            "learned_prior": raw_prior,
        }
        weighted = {
            "attribute_target_deficit": weighted_attribute
            * request.plan_weights.attribute_target_deficit,
            "audition_readiness": weighted_audition
            * request.plan_weights.audition_readiness,
            "deck_mutation_value": raw_deck
            * request.plan_weights.deck_mutation_value,
            "recovery_feasibility": raw_recovery
            * request.plan_weights.recovery_feasibility,
            "learned_prior": raw_prior * request.plan_weights.learned_prior,
        }
        reasons = (
            "effective gains "
            + ",".join(f"{name}:{gains[name]}" for name in ATTRIBUTES),
            f"next audition in {request.targets.next_audition_in_weeks} "
            "weeks; residual="
            + ",".join(f"{name}:{next_residual[name]}" for name in ATTRIBUTES),
            f"final in {request.targets.final_in_weeks} weeks; residual="
            + ",".join(f"{name}:{final_residual[name]}" for name in ATTRIBUTES),
            f"deck value={candidate.deck_mutation_value}; "
            f"upgrades={candidate.upgrade_count}; "
            f"removals={candidate.remove_count}",
            f"HP {request.state.stamina}->{after_hp}; "
            f"reserve={request.targets.stamina_reserve}",
            f"learned prior={priors[candidate.candidate_id]:.3f}; "
            f"bounded={raw_prior:.3f}",
        )
        evaluations.append(
            OuterPolicyCandidateEvaluation(
                candidate.candidate_id,
                candidate.kind,
                True,
                NiaCandidateScores(sum(weighted.values()), weighted),
                raw,
                reasons,
            )
        )

    eligible = tuple(value for value in evaluations if value.eligible)
    chosen = max(
        eligible,
        key=lambda value: (
            value.scores.total,  # type: ignore[union-attr]
            -order[value.candidate_id],
        ),
    )
    ranked = tuple(
        sorted(
            evaluations,
            key=lambda value: (
                not value.eligible,
                -(
                    value.scores.total
                    if value.scores is not None
                    else float("-inf")
                ),
                order.get(value.candidate_id, len(order)),
            ),
        )
    )
    return OuterPolicyDecision(STATUS_READY, chosen.candidate_id, ranked)


__all__ = [
    "OuterDeckSummary",
    "OuterPolicyBlocker",
    "OuterPolicyCandidate",
    "OuterPolicyCandidateEvaluation",
    "OuterPolicyDecision",
    "OuterPolicyRequest",
    "OuterPolicyTargets",
    "OuterPolicyWeights",
    "SCHEMA",
    "STATUS_BLOCKED",
    "STATUS_READY",
    "rank_outer_policy",
]
