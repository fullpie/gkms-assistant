"""Bounded common-random-number sampling for N.I.A. route candidates.

The simulator remains the authoritative transition system.  This layer only
varies caller-owned UInt32 schedule/exam RNG roots and aggregates complete
route results.  Every choice receives the same seed matrix, reducing variance
without claiming that currently unenumerated server events are sampled.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Final, Iterable

from .master_db import DEFAULT_DATABASE
from .nia_full_fixed_scenario import (
    NiaFullRouteScenario,
    run_nia_produce004_fktn_full_fixed_scenario,
)
from .nia_full_scenario_comparison import NiaFullScenarioCandidate
from .nia_static_adapter import DEFAULT_MASTER_DIR


NIA_MONTE_CARLO_SEED_ASSUMPTION: Final = (
    "caller-uniform-uint32-roots-via-splitmix64-common-random-numbers"
)
_MASK64: Final = (1 << 64) - 1


def _splitmix64(value: int) -> int:
    value = (value + 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (value ^ (value >> 31)) & _MASK64


def build_nia_common_rng_matrix(
    *,
    root_seed: int,
    sample_count: int,
    stage_count: int = 3,
) -> tuple[tuple[tuple[int, int], ...], ...]:
    """Return sample -> stage -> (schedule, exam) UInt32 roots."""

    if type(root_seed) is not int or not 0 <= root_seed <= _MASK64:
        raise ValueError("root_seed must be UInt64")
    if type(sample_count) is not int or sample_count < 1:
        raise ValueError("sample_count must be positive")
    if type(stage_count) is not int or stage_count < 1:
        raise ValueError("stage_count must be positive")
    state = root_seed
    matrix = []
    for _sample in range(sample_count):
        rows = []
        for _stage in range(stage_count):
            state = _splitmix64(state)
            schedule = state & 0xFFFFFFFF
            state = _splitmix64(state)
            exam = state & 0xFFFFFFFF
            rows.append((schedule, exam))
        matrix.append(tuple(rows))
    return tuple(matrix)


@dataclass(frozen=True, slots=True)
class NiaMonteCarloRouteSample:
    sample_index: int
    rng_roots: tuple[tuple[int, int], ...]
    audition_scores: tuple[int, ...]
    final_score: int

    def __post_init__(self) -> None:
        if type(self.sample_index) is not int or self.sample_index < 0:
            raise ValueError("sample_index must be non-negative")
        if not self.rng_roots or not self.audition_scores:
            raise ValueError("Monte Carlo sample cannot be empty")
        if self.final_score != self.audition_scores[-1]:
            raise ValueError("sample final score must match its last audition")


@dataclass(frozen=True, slots=True)
class NiaMonteCarloChoiceSummary:
    candidate: NiaFullScenarioCandidate
    samples: tuple[NiaMonteCarloRouteSample, ...]

    def __post_init__(self) -> None:
        values = tuple(self.samples)
        if not values:
            raise ValueError("Monte Carlo choice requires samples")
        if tuple(value.sample_index for value in values) != tuple(range(len(values))):
            raise ValueError("Monte Carlo samples must be ordered and contiguous")
        object.__setattr__(self, "samples", values)

    @property
    def mean_final_score(self) -> Fraction:
        return Fraction(sum(value.final_score for value in self.samples), len(self.samples))

    @property
    def minimum_final_score(self) -> int:
        return min(value.final_score for value in self.samples)

    @property
    def maximum_final_score(self) -> int:
        return max(value.final_score for value in self.samples)


@dataclass(frozen=True, slots=True)
class NiaMonteCarloComparison:
    root_seed: int
    sample_count: int
    seed_assumption: str
    choices: tuple[NiaMonteCarloChoiceSummary, ...]

    def __post_init__(self) -> None:
        if self.seed_assumption != NIA_MONTE_CARLO_SEED_ASSUMPTION:
            raise ValueError("Monte Carlo seed assumption is not explicit")
        values = tuple(self.choices)
        if len(values) < 2:
            raise ValueError("Monte Carlo comparison requires two choices")
        if any(len(value.samples) != self.sample_count for value in values):
            raise ValueError("Monte Carlo sample counts diverged")
        reference = tuple(value.rng_roots for value in values[0].samples)
        if any(
            tuple(sample.rng_roots for sample in value.samples) != reference
            for value in values[1:]
        ):
            raise ValueError("choices did not use common random numbers")
        ranked = tuple(
            sorted(
                values,
                key=lambda value: (
                    -value.mean_final_score,
                    -value.minimum_final_score,
                    -value.maximum_final_score,
                    value.candidate.candidate_id,
                ),
            )
        )
        if values != ranked:
            raise ValueError("Monte Carlo choices must be ranked")
        object.__setattr__(self, "choices", values)

    @property
    def best(self) -> NiaMonteCarloChoiceSummary:
        return self.choices[0]


def _scenario_with_rng_roots(
    scenario: NiaFullRouteScenario,
    roots: tuple[tuple[int, int], ...],
    *,
    sample_index: int,
) -> NiaFullRouteScenario:
    if len(roots) != len(scenario.auditions):
        raise ValueError("RNG root stage count does not match scenario auditions")
    auditions = tuple(
        replace(
            audition,
            schedule_random_state=schedule,
            exam_random_state=exam,
        )
        for audition, (schedule, exam) in zip(
            scenario.auditions,
            roots,
            strict=True,
        )
    )
    return replace(
        scenario,
        auditions=auditions,
        authority_ref=f"{scenario.authority_ref}:rng-sample-{sample_index}",
    )


def sample_nia_full_route_candidates(
    candidates: Iterable[NiaFullScenarioCandidate],
    *,
    sample_count: int,
    root_seed: int,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaMonteCarloComparison:
    """Replay candidates with a shared deterministic UInt32 RNG matrix."""

    normalized = tuple(candidates)
    if len(normalized) < 2:
        raise ValueError("Monte Carlo comparison requires two candidates")
    stage_counts = {len(value.scenario.auditions) for value in normalized}
    if len(stage_counts) != 1:
        raise ValueError("candidate audition stage counts diverged")
    matrix = build_nia_common_rng_matrix(
        root_seed=root_seed,
        sample_count=sample_count,
        stage_count=stage_counts.pop(),
    )
    summaries = []
    for candidate in normalized:
        samples = []
        for sample_index, roots in enumerate(matrix):
            sampled = _scenario_with_rng_roots(
                candidate.scenario,
                roots,
                sample_index=sample_index,
            )
            result = run_nia_produce004_fktn_full_fixed_scenario(
                scenario=sampled,
                database=Path(database),
                master_dir=Path(master_dir),
            )
            scores = tuple(
                value.resolution.player_score for value in result.stages
            )
            samples.append(
                NiaMonteCarloRouteSample(
                    sample_index,
                    roots,
                    scores,
                    scores[-1],
                )
            )
        summaries.append(NiaMonteCarloChoiceSummary(candidate, tuple(samples)))
    ranked = tuple(
        sorted(
            summaries,
            key=lambda value: (
                -value.mean_final_score,
                -value.minimum_final_score,
                -value.maximum_final_score,
                value.candidate.candidate_id,
            ),
        )
    )
    return NiaMonteCarloComparison(
        root_seed,
        sample_count,
        NIA_MONTE_CARLO_SEED_ASSUMPTION,
        ranked,
    )


__all__ = [
    "NIA_MONTE_CARLO_SEED_ASSUMPTION",
    "NiaMonteCarloChoiceSummary",
    "NiaMonteCarloComparison",
    "NiaMonteCarloRouteSample",
    "build_nia_common_rng_matrix",
    "sample_nia_full_route_candidates",
]
