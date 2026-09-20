"""Native counterfactual evaluation for Plan2 reward-card choices.

The static reward policy can recognize deck roles quickly.  This module is
the slower, stronger second stage: append each resolved offer to the current
deck, run the same native Plan2 planner under paired RNG seeds, and compare
terminal score distributions.  It never performs UI input and never treats a
failed simulation as a low score.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from statistics import fmean
from typing import Final

from .master_db import DEFAULT_DATABASE, get_idol_profile
from .leaderboard_card_prior import Plan2LeaderboardCardPrior
from .plan2_native_expectimax import Plan2NativeExpectimaxLimits
from .plan2_native_horizon import (
    Plan2MasterInitialDeck,
    load_master_plan2_initial_deck,
)
from .plan2_native_program_catalog import compile_plan2_native_program_catalog
from .plan2_native_self_play_acceptance import (
    Plan2NativeSelfPlayDependencies,
    run_plan2_native_self_play_acceptance,
)
from .plan2_strategy_runtime import build_plan2_promoted_strategy_planner
from .reward_state import (
    RewardCardOffer,
    Plan2RewardOfferEvaluation,
    evaluate_plan2_reward_offers,
    rank_plan2_reward_offers,
)


DEFAULT_ROLLOUT_SEEDS: Final = (0x12345678, 0xA5A5A5A5, 0xCAFEBABE)


@dataclass(frozen=True, slots=True)
class Plan2RewardRolloutConfig:
    random_states: tuple[int, ...] = DEFAULT_ROLLOUT_SEEDS
    limit_turns: tuple[int, ...] = (6, 9)
    max_actions: int = 48
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(
        max_depth=3,
        beam_width=5,
        max_nodes=1_800,
    )
    stop_on_incomplete: bool = False

    def __post_init__(self) -> None:
        seeds = tuple(self.random_states)
        turns = tuple(self.limit_turns)
        if not seeds or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFF
            for value in seeds
        ):
            raise ValueError("random_states must contain UInt32 seeds")
        if not turns or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in turns
        ):
            raise ValueError("limit_turns must contain positive integers")
        if isinstance(self.max_actions, bool) or self.max_actions < 1:
            raise ValueError("max_actions must be positive")
        if not isinstance(self.limits, Plan2NativeExpectimaxLimits):
            raise TypeError("limits must be Plan2NativeExpectimaxLimits")
        if type(self.stop_on_incomplete) is not bool:
            raise TypeError("stop_on_incomplete must be bool")
        object.__setattr__(self, "random_states", seeds)
        object.__setattr__(self, "limit_turns", turns)


@dataclass(frozen=True, slots=True)
class Plan2RewardRolloutSample:
    random_state: int
    limit_turn: int
    terminal_score: int | None
    issue: str = ""

    @property
    def completed(self) -> bool:
        return self.terminal_score is not None and not self.issue


@dataclass(frozen=True, slots=True)
class Plan2RewardRolloutEvaluation:
    offer: RewardCardOffer
    samples: tuple[Plan2RewardRolloutSample, ...]
    mean_terminal_score: float | None
    worst_terminal_score: int | None

    @property
    def complete(self) -> bool:
        return bool(self.samples) and all(value.completed for value in self.samples)

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(value.issue for value in self.samples if value.issue)


@dataclass(frozen=True, slots=True)
class Plan2RewardRolloutRanking:
    evaluations: tuple[Plan2RewardRolloutEvaluation, ...]

    @property
    def complete(self) -> bool:
        return bool(self.evaluations) and all(value.complete for value in self.evaluations)

    @property
    def ranked_offers(self) -> tuple[RewardCardOffer, ...]:
        return tuple(value.offer for value in self.evaluations if value.complete)

    @property
    def issues(self) -> tuple[str, ...]:
        return tuple(
            f"slot={value.offer.slot}:{issue}"
            for value in self.evaluations
            for issue in value.issues
        )


@dataclass(frozen=True, slots=True)
class Plan2RewardStrongRanking:
    ranked_offers: tuple[RewardCardOffer, ...]
    policy: str
    rollout: Plan2RewardRolloutRanking | None = None
    # Preserve the exact heuristic pass used by the strong ranker.  Live
    # telemetry can expose the already-computed leaderboard bonus/reasons
    # without rerunning or approximating the ranking after the click decision.
    heuristic_evaluations: tuple[Plan2RewardOfferEvaluation, ...] = ()

    def __post_init__(self) -> None:
        if self.policy not in {
            "native-counterfactual-rollout",
            "deck-aware-heuristic-fallback",
        }:
            raise ValueError("unsupported strong reward policy")


def _paired_rollout_dominates(
    candidate: Plan2RewardRolloutEvaluation,
    baseline: Plan2RewardRolloutEvaluation,
) -> bool:
    """Return whether ``candidate`` wins without losing a paired scenario.

    Reward rollouts deliberately reuse the same seed/turn pairs.  A tiny mean
    advantage assembled from large wins and losses is not stable evidence for
    replacing an archetype-compatible heuristic winner with an off-plan card.
    """

    if not candidate.complete or not baseline.complete:
        return False
    candidate_scores = {
        (sample.limit_turn, sample.random_state): sample.terminal_score
        for sample in candidate.samples
    }
    baseline_scores = {
        (sample.limit_turn, sample.random_state): sample.terminal_score
        for sample in baseline.samples
    }
    if not candidate_scores or candidate_scores.keys() != baseline_scores.keys():
        return False
    differences = tuple(
        int(candidate_scores[key]) - int(baseline_scores[key])
        for key in candidate_scores
    )
    return all(value >= 0 for value in differences) and any(
        value > 0 for value in differences
    )


def _unstable_off_plan_reward_override(
    rollout: Plan2RewardRolloutRanking,
    heuristic: tuple[RewardCardOffer, ...],
    exam_effect_type: str,
    *,
    deck_counts: Mapping[str, int],
    weeks_remaining: int | None,
    database: Path,
) -> bool:
    """Guard only conflicting off-plan overrides, not ordinary rollout wins."""

    if not rollout.complete or not rollout.evaluations or not heuristic:
        return False
    rollout_top = rollout.evaluations[0]
    heuristic_top = heuristic[0]
    if rollout_top.offer.slot == heuristic_top.slot:
        return False
    strategic = {
        value.offer.slot: value
        for value in evaluate_plan2_reward_offers(
            tuple(value.offer for value in rollout.evaluations),
            exam_effect_type,
            deck_counts=deck_counts,
            weeks_remaining=weeks_remaining,
            database=database,
        )
    }
    rollout_strategy = strategic.get(rollout_top.offer.slot)
    heuristic_strategy = strategic.get(heuristic_top.slot)
    if rollout_strategy is None or heuristic_strategy is None:
        return False
    if (
        "archetype:off-plan" not in rollout_strategy.reasons
        or "archetype:off-plan" in heuristic_strategy.reasons
    ):
        return False
    baseline = next(
        (
            value
            for value in rollout.evaluations
            if value.offer.slot == heuristic_top.slot
        ),
        None,
    )
    return baseline is None or not _paired_rollout_dominates(
        rollout_top,
        baseline,
    )


RolloutSimulator = Callable[
    [
        Plan2MasterInitialDeck,
        int,
        int,
        int,
        int,
        Plan2RewardRolloutConfig,
        Path,
    ],
    Plan2RewardRolloutSample,
]


@lru_cache(maxsize=4)
def _compiled_catalog(database: Path):
    return compile_plan2_native_program_catalog(database=database)


def _default_simulator(
    manifest: Plan2MasterInitialDeck,
    random_state: int,
    limit_turn: int,
    stamina: int,
    max_stamina: int,
    config: Plan2RewardRolloutConfig,
    database: Path,
) -> Plan2RewardRolloutSample:
    compilation = _compiled_catalog(database.resolve())
    planner = build_plan2_promoted_strategy_planner(
        manifest.idol_card_id,
        manifest.produce_id,
        database=database,
    )
    dependencies = Plan2NativeSelfPlayDependencies(
        manifest_loader=lambda *_args: manifest,
        catalog_compiler=lambda _path: compilation,
        planner=planner,
    )
    result = run_plan2_native_self_play_acceptance(
        manifest.idol_card_id,
        random_state=random_state,
        limit_turn=limit_turn,
        stamina=stamina,
        max_stamina=max_stamina,
        produce_id=manifest.produce_id,
        max_actions=config.max_actions,
        limits=config.limits,
        database=database,
        dependencies=dependencies,
    )
    if not result.acceptance_passed or result.final_state is None:
        detail = ",".join(
            f"{value.code}:{value.detail}" for value in result.blockers
        ) or result.stop_reason
        return Plan2RewardRolloutSample(
            random_state,
            limit_turn,
            None,
            f"native-rollout-incomplete:{detail}",
        )
    return Plan2RewardRolloutSample(
        random_state,
        limit_turn,
        result.final_state.scalar.score,
    )


def _deck_refs(deck_counts: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    refs: list[tuple[str, int]] = []
    for identity, count in deck_counts.items():
        if not isinstance(identity, str):
            raise ValueError("deck keys must be CARD_ID@UPGRADE")
        card_id, separator, raw_upgrade = identity.rpartition("@")
        if not separator or not card_id or not raw_upgrade.isdigit():
            raise ValueError("deck keys must be CARD_ID@UPGRADE")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("deck counts must be positive integers")
        refs.extend(((card_id, int(raw_upgrade)),) * count)
    if not refs:
        raise ValueError("deck_counts cannot be empty")
    return tuple(refs)


def _evaluate_rollout_samples(
    manifest: Plan2MasterInitialDeck,
    *,
    stamina: int,
    max_stamina: int,
    config: Plan2RewardRolloutConfig,
    database: Path,
    simulator: RolloutSimulator,
) -> tuple[Plan2RewardRolloutSample, ...]:
    """Run one paired grid, optionally stopping at its first incomplete row."""

    samples: list[Plan2RewardRolloutSample] = []
    for limit_turn in config.limit_turns:
        for seed in config.random_states:
            sample = simulator(
                manifest,
                seed,
                limit_turn,
                stamina,
                max_stamina,
                config,
                database,
            )
            samples.append(sample)
            if config.stop_on_incomplete and not sample.completed:
                return tuple(samples)
    return tuple(samples)


def evaluate_plan2_reward_rollouts(
    offers: Sequence[RewardCardOffer],
    *,
    idol_card_id: str,
    produce_id: str,
    deck_counts: Mapping[str, int],
    stamina: int | None = None,
    max_stamina: int | None = None,
    config: Plan2RewardRolloutConfig = Plan2RewardRolloutConfig(),
    database: Path = DEFAULT_DATABASE,
    simulator: RolloutSimulator = _default_simulator,
) -> Plan2RewardRolloutRanking:
    """Rank resolved offers by paired native terminal-score rollouts.

    The current deck is caller-authoritative.  The idol's Master manifest is
    used only for its mandatory item and deck metadata; its starter card list
    is replaced by ``deck_counts`` before each candidate is appended.
    """

    candidates = tuple(offers)
    if not candidates:
        raise ValueError("offers cannot be empty")
    if any(not isinstance(value, RewardCardOffer) for value in candidates):
        raise TypeError("offers must contain RewardCardOffer")
    if len({value.slot for value in candidates}) != len(candidates):
        raise ValueError("reward offer slots must be unique")
    if not callable(simulator):
        raise TypeError("simulator must be callable")
    profile = get_idol_profile(idol_card_id, Path(database))
    if profile is None or profile.plan_type != "ProducePlanType_Plan2":
        raise ValueError("idol_card_id must identify a Plan2 idol")
    resolved_max = profile.stamina if max_stamina is None else max_stamina
    resolved_stamina = resolved_max if stamina is None else stamina
    if (
        isinstance(resolved_stamina, bool)
        or not isinstance(resolved_stamina, int)
        or isinstance(resolved_max, bool)
        or not isinstance(resolved_max, int)
        or resolved_max < 1
        or not 0 <= resolved_stamina <= resolved_max
    ):
        raise ValueError("stamina/max_stamina are invalid")
    base = load_master_plan2_initial_deck(
        idol_card_id,
        produce_id=produce_id,
    )
    current_refs = _deck_refs(deck_counts)
    evaluations: list[Plan2RewardRolloutEvaluation] = []
    for offer in candidates:
        manifest = replace(
            base,
            card_refs=(*current_refs, (offer.card_id, offer.upgrade)),
        )
        samples = _evaluate_rollout_samples(
            manifest,
            stamina=resolved_stamina,
            max_stamina=resolved_max,
            config=config,
            database=Path(database),
            simulator=simulator,
        )
        completed_scores = tuple(
            value.terminal_score
            for value in samples
            if value.completed and value.terminal_score is not None
        )
        complete = len(completed_scores) == len(samples)
        evaluations.append(
            Plan2RewardRolloutEvaluation(
                offer,
                samples,
                fmean(completed_scores) if complete else None,
                min(completed_scores) if complete else None,
            )
        )
        if config.stop_on_incomplete and not complete:
            return Plan2RewardRolloutRanking(tuple(evaluations))
    evaluations.sort(
        key=lambda value: (
            value.complete,
            float("-inf")
            if value.mean_terminal_score is None
            else value.mean_terminal_score,
            -1 if value.worst_terminal_score is None else value.worst_terminal_score,
            value.offer.evaluation,
            -value.offer.slot,
        ),
        reverse=True,
    )
    return Plan2RewardRolloutRanking(tuple(evaluations))


def evaluate_plan2_upgrade_rollouts(
    offers: Sequence[RewardCardOffer],
    *,
    idol_card_id: str,
    produce_id: str,
    deck_counts: Mapping[str, int],
    stamina: int | None = None,
    max_stamina: int | None = None,
    config: Plan2RewardRolloutConfig = Plan2RewardRolloutConfig(),
    database: Path = DEFAULT_DATABASE,
    simulator: RolloutSimulator = _default_simulator,
) -> Plan2RewardRolloutRanking:
    """Rank N.I.A. strengthen targets by replacing one owned card with +1."""

    candidates = tuple(offers)
    if not candidates or any(
        not isinstance(value, RewardCardOffer) for value in candidates
    ):
        raise ValueError("offers must contain typed reward offers")
    if len({value.slot for value in candidates}) != len(candidates):
        raise ValueError("reward offer slots must be unique")
    profile = get_idol_profile(idol_card_id, Path(database))
    if profile is None or profile.plan_type != "ProducePlanType_Plan2":
        raise ValueError("idol_card_id must identify a Plan2 idol")
    resolved_max = profile.stamina if max_stamina is None else max_stamina
    resolved_stamina = resolved_max if stamina is None else stamina
    if (
        isinstance(resolved_stamina, bool)
        or not isinstance(resolved_stamina, int)
        or isinstance(resolved_max, bool)
        or not isinstance(resolved_max, int)
        or resolved_max < 1
        or not 0 <= resolved_stamina <= resolved_max
    ):
        raise ValueError("stamina/max_stamina are invalid")
    base = load_master_plan2_initial_deck(
        idol_card_id,
        produce_id=produce_id,
    )
    current_refs = _deck_refs(deck_counts)
    evaluations: list[Plan2RewardRolloutEvaluation] = []
    for offer in candidates:
        source = (offer.card_id, offer.upgrade)
        if source not in current_refs or offer.upgrade >= 3:
            raise ValueError(
                f"strengthen target is not an upgradable owned card: {source!r}"
            )
        upgraded_refs = list(current_refs)
        upgraded_refs[upgraded_refs.index(source)] = (
            offer.card_id,
            offer.upgrade + 1,
        )
        manifest = replace(base, card_refs=tuple(upgraded_refs))
        samples = _evaluate_rollout_samples(
            manifest,
            stamina=resolved_stamina,
            max_stamina=resolved_max,
            config=config,
            database=Path(database),
            simulator=simulator,
        )
        scores = tuple(
            value.terminal_score
            for value in samples
            if value.completed and value.terminal_score is not None
        )
        complete = len(scores) == len(samples)
        evaluations.append(
            Plan2RewardRolloutEvaluation(
                offer,
                samples,
                fmean(scores) if complete else None,
                min(scores) if complete else None,
            )
        )
        if config.stop_on_incomplete and not complete:
            return Plan2RewardRolloutRanking(tuple(evaluations))
    evaluations.sort(
        key=lambda value: (
            value.complete,
            float("-inf")
            if value.mean_terminal_score is None
            else value.mean_terminal_score,
            -1 if value.worst_terminal_score is None else value.worst_terminal_score,
            value.offer.evaluation,
            -value.offer.slot,
        ),
        reverse=True,
    )
    return Plan2RewardRolloutRanking(tuple(evaluations))


def rank_plan2_reward_offers_strong(
    offers: Sequence[RewardCardOffer],
    exam_effect_type: str,
    *,
    idol_card_id: str,
    produce_id: str,
    deck_counts: Mapping[str, int] | None,
    weeks_remaining: int | None = None,
    stamina: int | None = None,
    max_stamina: int | None = None,
    leaderboard_prior: Plan2LeaderboardCardPrior | None = None,
    config: Plan2RewardRolloutConfig | None = None,
    database: Path = DEFAULT_DATABASE,
    simulator: RolloutSimulator = _default_simulator,
) -> Plan2RewardStrongRanking:
    """Use native counterfactuals when authority is complete, otherwise fall back.

    The fallback is deliberate: reward recognition must remain usable when an
    idol item or newly released card has not reached full native coverage.
    Incomplete native simulations are never mixed into the score ranking.
    """

    candidates = tuple(offers)
    applicable_prior = (
        leaderboard_prior
        if leaderboard_prior is not None
        and leaderboard_prior.applies_to(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            exam_effect_type=exam_effect_type,
        )
        else None
    )
    heuristic_evaluations = evaluate_plan2_reward_offers(
        candidates,
        exam_effect_type,
        deck_counts=deck_counts,
        weeks_remaining=weeks_remaining,
        leaderboard_prior=applicable_prior,
        database=database,
    )
    heuristic = tuple(item.offer for item in heuristic_evaluations)
    if not deck_counts:
        return Plan2RewardStrongRanking(
            heuristic,
            "deck-aware-heuristic-fallback",
            heuristic_evaluations=heuristic_evaluations,
        )
    resolved_config = config
    if resolved_config is None:
        turns = (
            (9,)
            if weeks_remaining is not None and weeks_remaining <= 4
            else (6,)
            if weeks_remaining is not None and weeks_remaining >= 15
            else (6, 9)
        )
        resolved_config = Plan2RewardRolloutConfig(
            random_states=(0x12345678, 0xA5A5A5A5),
            limit_turns=turns,
            max_actions=16,
            limits=Plan2NativeExpectimaxLimits(
                max_depth=2,
                beam_width=4,
                max_nodes=256,
            ),
            stop_on_incomplete=True,
        )
    try:
        rollout = evaluate_plan2_reward_rollouts(
            candidates,
            idol_card_id=idol_card_id,
            produce_id=produce_id,
            deck_counts=deck_counts,
            stamina=stamina,
            max_stamina=max_stamina,
            config=resolved_config,
            database=database,
            simulator=simulator,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return Plan2RewardStrongRanking(
            heuristic,
            "deck-aware-heuristic-fallback",
        )
    if rollout.complete and len(rollout.ranked_offers) == len(candidates):
        if _unstable_off_plan_reward_override(
            rollout,
            heuristic,
            exam_effect_type,
            deck_counts=deck_counts,
            weeks_remaining=weeks_remaining,
            database=Path(database),
        ):
            return Plan2RewardStrongRanking(
                heuristic,
                "deck-aware-heuristic-fallback",
                rollout,
                heuristic_evaluations,
            )
        return Plan2RewardStrongRanking(
            rollout.ranked_offers,
            "native-counterfactual-rollout",
            rollout,
            heuristic_evaluations,
        )
    return Plan2RewardStrongRanking(
        heuristic,
        "deck-aware-heuristic-fallback",
        rollout,
        heuristic_evaluations,
    )


def rank_plan2_upgrade_offers_strong(
    offers: Sequence[RewardCardOffer],
    exam_effect_type: str,
    *,
    idol_card_id: str,
    produce_id: str,
    deck_counts: Mapping[str, int] | None,
    weeks_remaining: int | None = None,
    stamina: int | None = None,
    max_stamina: int | None = None,
    leaderboard_prior: Plan2LeaderboardCardPrior | None = None,
    config: Plan2RewardRolloutConfig | None = None,
    database: Path = DEFAULT_DATABASE,
    simulator: RolloutSimulator = _default_simulator,
) -> Plan2RewardStrongRanking:
    """Choose a strengthen target by paired upgraded-deck simulations."""

    candidates = tuple(offers)
    applicable_prior = (
        leaderboard_prior
        if leaderboard_prior is not None
        and leaderboard_prior.applies_to(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            exam_effect_type=exam_effect_type,
        )
        else None
    )
    heuristic = rank_plan2_reward_offers(
        candidates,
        exam_effect_type,
        deck_counts=deck_counts,
        weeks_remaining=weeks_remaining,
        leaderboard_prior=applicable_prior,
        database=database,
    )
    if not deck_counts:
        return Plan2RewardStrongRanking(heuristic, "deck-aware-heuristic-fallback")
    resolved_config = config or Plan2RewardRolloutConfig(
        random_states=(0x12345678, 0xA5A5A5A5),
        limit_turns=(
            (9,)
            if weeks_remaining is not None and weeks_remaining <= 4
            else (6,)
            if weeks_remaining is not None and weeks_remaining >= 15
            else (6, 9)
        ),
        max_actions=16,
        limits=Plan2NativeExpectimaxLimits(
            max_depth=2,
            beam_width=4,
            max_nodes=256,
        ),
        stop_on_incomplete=True,
    )
    try:
        rollout = evaluate_plan2_upgrade_rollouts(
            candidates,
            idol_card_id=idol_card_id,
            produce_id=produce_id,
            deck_counts=deck_counts,
            stamina=stamina,
            max_stamina=max_stamina,
            config=resolved_config,
            database=database,
            simulator=simulator,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return Plan2RewardStrongRanking(heuristic, "deck-aware-heuristic-fallback")
    if rollout.complete and len(rollout.ranked_offers) == len(candidates):
        return Plan2RewardStrongRanking(
            rollout.ranked_offers,
            "native-counterfactual-rollout",
            rollout,
        )
    return Plan2RewardStrongRanking(
        heuristic,
        "deck-aware-heuristic-fallback",
        rollout,
    )


__all__ = [
    "DEFAULT_ROLLOUT_SEEDS",
    "Plan2RewardRolloutConfig",
    "Plan2RewardRolloutEvaluation",
    "Plan2RewardRolloutRanking",
    "Plan2RewardRolloutSample",
    "Plan2RewardStrongRanking",
    "evaluate_plan2_reward_rollouts",
    "evaluate_plan2_upgrade_rollouts",
    "rank_plan2_reward_offers_strong",
    "rank_plan2_upgrade_offers_strong",
]
