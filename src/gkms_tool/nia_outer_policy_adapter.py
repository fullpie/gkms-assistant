"""Minimal authority adapter from N.I.A. lesson choices to Outer Policy v1."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Mapping, Sequence

from .audition_rules import FINAL, MID1, MID2
from .nia_outer_prior import NiaOuterPrior
from .nia_static_adapter import (
    NiaAuditionDefinition,
    load_nia_static_bundle,
)
from .nia_strategy_journal import NiaStrategyState
from .nia_week_gate import nia_week_gate
from .overview_actions import REST
from .outer_policy_core import (
    OuterDeckSummary,
    OuterPolicyCandidate,
    OuterPolicyRequest,
    OuterPolicyTargets,
    OuterPolicyWeights,
    rank_outer_policy,
)
from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot
from .run_shadow import RunShadowState


_STAGE_TYPES = {"mid1": MID1, "mid2": MID2, "final": FINAL}


@lru_cache(maxsize=64)
def _auditions(
    produce_id: str, idol_card_id: str, master_dir: Path
) -> tuple[NiaAuditionDefinition, ...]:
    return tuple(
        load_nia_static_bundle(
            idol_card_id,
            produce_id=produce_id,
            master_dir=master_dir,
        ).auditions
    )


def _targets(
    snapshot: ProduceOuterLocalSaveSnapshot,
    route: object,
    idol_card_id: str,
    reserve: int,
    master_dir: Path,
) -> OuterPolicyTargets | None:
    vote_count = snapshot.vote_count
    if isinstance(vote_count, bool) or not isinstance(vote_count, int):
        return None
    try:
        rows = _auditions(route.produce_id, idol_card_id, master_dir)  # type: ignore[attr-defined]
        next_type = _STAGE_TYPES[route.next_audition_stage]  # type: ignore[attr-defined]
        unlocked = tuple(
            row
            for row in rows
            if row.rules.step_type == next_type and row.vote_count <= vote_count
        )
        finals = tuple(row for row in rows if row.rules.step_type == FINAL)
        final_week = next(
            week
            for stage, week in route.audition_boundaries  # type: ignore[attr-defined]
            if stage == "final"
        )
    except (AttributeError, KeyError, OSError, StopIteration, TypeError, ValueError):
        return None
    if not unlocked or not finals:
        return None
    next_row = max(unlocked, key=lambda row: (row.vote_count, row.rules.number))
    final_row = max(finals, key=lambda row: (row.vote_count, row.rules.number))

    def parameters(row: NiaAuditionDefinition) -> Mapping[str, int]:
        return {
            "vocal": row.rules.vocal_parameter,
            "dance": row.rules.dance_parameter,
            "visual": row.rules.visual_parameter,
        }

    return OuterPolicyTargets(
        next_audition_in_weeks=route.next_audition_week - route.week,  # type: ignore[attr-defined]
        final_in_weeks=final_week - route.week,  # type: ignore[attr-defined]
        stamina_reserve=reserve,
        next_audition=parameters(next_row),
        final=parameters(final_row),
        source=(
            "master:highest-fan-vote-unlocked-no-parameter-penalty:"
            f"{next_row.difficulty_key.stable_key};"
            f"{final_row.difficulty_key.stable_key}"
        ),
    )


def _deck(
    shadow: RunShadowState | None,
    produce_id: str,
    idol_card_id: str,
) -> OuterDeckSummary | None:
    if (
        shadow is None
        or shadow.produce_id != produce_id
        or shadow.idol_card_id != idol_card_id
        or not shadow.deck
    ):
        return None
    try:
        total = sum(shadow.deck.values())
        upgraded = sum(
            count
            for key, count in shadow.deck.items()
            if int(key.rsplit("@", 1)[1]) > 0
        )
    except (IndexError, TypeError, ValueError):
        return None
    # @0 does not prove that a particular card can be upgraded or removed.
    return None if total < 1 else OuterDeckSummary(total, upgraded, 0, 0)


def rank_nia_outer_lesson_policy_v1(
    snapshot: ProduceOuterLocalSaveSnapshot,
    route: object,
    lesson_candidates: Sequence[tuple[str, str, int, int]],
    *,
    rest_visible: bool,
    reserve: int,
    profile: object | None,
    shadow: RunShadowState | None,
    prior: NiaOuterPrior | None,
    prior_features: Mapping[str, object],
    weights: OuterPolicyWeights,
    master_dir: Path,
) -> str | None:
    """Return a core choice, or abstain without changing legacy advice."""

    idol_card_id = getattr(profile, "id", None)
    if (
        not lesson_candidates
        or not isinstance(idol_card_id, str)
        or snapshot.produce_points is None
        or snapshot.vote_count is None
        or snapshot.stamina is None
        or snapshot.max_stamina is None
    ):
        return None
    deck = _deck(shadow, route.produce_id, idol_card_id)  # type: ignore[attr-defined]
    targets = _targets(snapshot, route, idol_card_id, reserve, master_dir)
    if targets is None:
        return None
    try:
        legal = tuple(candidate[0] for candidate in lesson_candidates)
        if rest_visible:
            legal = (*legal, REST)
        schedule = nia_week_gate(route.produce_id, route.week).actions  # type: ignore[attr-defined]
        if any(action not in schedule for action in legal):
            return None
        candidates = tuple(
            OuterPolicyCandidate(
                action,
                "lesson",
                {field: base_gain},
                stamina_cost=stamina_cost,
            )
            for action, field, base_gain, stamina_cost in lesson_candidates
        )
        if rest_visible:
            candidates = (
                *candidates,
                OuterPolicyCandidate(
                    REST,
                    "recovery",
                    stamina_recovery=(
                        snapshot.max_stamina
                        * route.rest_recovery_permille  # type: ignore[attr-defined]
                        // 1000
                    ),
                ),
            )
        prior_scores: dict[str, float] = {}
        if prior is not None:
            ranked = tuple(prior.rank(prior_features, legal))
            prior_scores = {
                action: float(len(ranked) - index)
                for index, action in enumerate(ranked)
                if action in legal
            }
        decision = rank_outer_policy(
            OuterPolicyRequest(
                mode=route.produce_id,  # type: ignore[attr-defined]
                scheduled_step_types=schedule,
                week=route.week,  # type: ignore[attr-defined]
                stage=route.stage,  # type: ignore[attr-defined]
                legal_candidate_ids=legal,
                state=NiaStrategyState(
                    stamina=snapshot.stamina,
                    max_stamina=snapshot.max_stamina,
                    produce_points=snapshot.produce_points,
                    vocal=int(prior_features["vocal"]),
                    dance=int(prior_features["dance"]),
                    visual=int(prior_features["visual"]),
                    vote_count=snapshot.vote_count,
                    details={"target_semantics": "no-parameter-penalty"},
                ),
                growth_permil={
                    name: int(getattr(profile, f"{name}_growth"))
                    for name in ("vocal", "dance", "visual")
                },
                deck=deck,
                targets=targets,
                plan_weights=weights,
                candidates=candidates,
                learned_prior_scores=prior_scores,
            )
        )
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return None
    return decision.chosen_id if decision.ready else None


__all__ = ["rank_nia_outer_lesson_policy_v1"]
