"""Deterministic counterfactual comparison over complete N.I.A. scenarios.

This is not an expected-value claim: every candidate retains explicit caller
runtime facts and is replayed independently through Final.  It is the first
outer advisor layer that can compare a player-controlled choice while keeping
server rolls, RNG roots, NPC tracks, and reward GUIDs fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, Iterable

from .master_db import DEFAULT_DATABASE
from .nia_full_fixed_scenario import (
    NiaFullFixedScenarioResult,
    NiaFullRouteScenario,
    run_nia_produce004_fktn_full_fixed_scenario,
)
from .nia_outer_action_runtime import (
    CARE_PACKAGE,
    OUTING,
    NiaCarePackageScenario,
    NiaSelfLessonScenario,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .produce_rollout import AttributeValues
from .route_calendar import load_route_calendar


OBJECTIVE_FINAL_AUDITION_SCORE: Final = "final_audition_score"
OBJECTIVE_TOTAL_AUDITION_SCORE: Final = "total_audition_score"
OBJECTIVE_FINAL_ATTRIBUTE_SUM: Final = "final_attribute_sum"
_OBJECTIVES: Final = frozenset(
    (
        OBJECTIVE_FINAL_AUDITION_SCORE,
        OBJECTIVE_TOTAL_AUDITION_SCORE,
        OBJECTIVE_FINAL_ATTRIBUTE_SUM,
    )
)


@dataclass(frozen=True, slots=True)
class NiaFullScenarioCandidate:
    candidate_id: str
    label: str
    scenario: NiaFullRouteScenario
    decision_week: int
    choice_id: str

    def __post_init__(self) -> None:
        if not self.candidate_id or not self.label:
            raise ValueError("NIA comparison candidate identity is required")
        if not isinstance(self.scenario, NiaFullRouteScenario):
            raise TypeError("candidate scenario must be NiaFullRouteScenario")
        if type(self.decision_week) is not int or not 1 <= self.decision_week <= 27:
            raise ValueError("candidate decision_week must be within the route")
        if not self.choice_id:
            raise ValueError("candidate choice_id is required")


@dataclass(frozen=True, slots=True)
class NiaFullScenarioEvaluation:
    candidate: NiaFullScenarioCandidate
    result: NiaFullFixedScenarioResult
    audition_scores: tuple[int, ...]
    final_attributes: AttributeValues
    final_stamina: int

    def __post_init__(self) -> None:
        if not self.result.completed or not self.result.stages:
            raise ValueError("NIA comparison candidate did not reach Final")
        if self.result.scenario != self.candidate.scenario:
            raise ValueError("NIA comparison result/scenario identity diverged")
        if self.audition_scores != tuple(
            value.resolution.player_score for value in self.result.stages
        ):
            raise ValueError("NIA comparison score projection diverged")
        if self.final_stamina < 0:
            raise ValueError("final_stamina cannot be negative")

    @property
    def final_audition_score(self) -> int:
        return self.audition_scores[-1]

    @property
    def total_audition_score(self) -> int:
        return sum(self.audition_scores)

    @property
    def final_attribute_sum(self) -> int:
        return sum(
            (
                self.final_attributes.vocal,
                self.final_attributes.dance,
                self.final_attributes.visual,
            )
        )

    def value_for(self, objective: str) -> int:
        if objective == OBJECTIVE_FINAL_AUDITION_SCORE:
            return self.final_audition_score
        if objective == OBJECTIVE_TOTAL_AUDITION_SCORE:
            return self.total_audition_score
        if objective == OBJECTIVE_FINAL_ATTRIBUTE_SUM:
            return self.final_attribute_sum
        raise ValueError(f"unsupported NIA comparison objective: {objective}")


@dataclass(frozen=True, slots=True)
class NiaFullScenarioComparison:
    objective: str
    evaluations: tuple[NiaFullScenarioEvaluation, ...]

    def __post_init__(self) -> None:
        if self.objective not in _OBJECTIVES:
            raise ValueError("NIA comparison objective is unsupported")
        values = tuple(self.evaluations)
        if len(values) < 2:
            raise ValueError("NIA comparison requires at least two candidates")
        ids = tuple(value.candidate.candidate_id for value in values)
        if len(ids) != len(set(ids)):
            raise ValueError("NIA comparison candidate IDs must be unique")
        weeks = {value.candidate.decision_week for value in values}
        if len(weeks) != 1:
            raise ValueError("NIA comparison candidates must share one decision week")
        choices = tuple(value.candidate.choice_id for value in values)
        if len(choices) != len(set(choices)):
            raise ValueError("NIA comparison choice IDs must be unique")
        expected = tuple(
            sorted(
                values,
                key=lambda value: (
                    -value.value_for(self.objective),
                    -value.total_audition_score,
                    -value.final_attribute_sum,
                    -value.final_stamina,
                    value.candidate.candidate_id,
                ),
            )
        )
        if values != expected:
            raise ValueError("NIA comparison evaluations must be ranked")
        object.__setattr__(self, "evaluations", values)

    @property
    def best(self) -> NiaFullScenarioEvaluation:
        return self.evaluations[0]

    @property
    def decision_week(self) -> int:
        return self.best.candidate.decision_week


def build_nia_self_lesson_attribute_candidates(
    scenario: NiaFullRouteScenario,
    *,
    week: int,
    attributes: tuple[str, ...] = ("vocal", "dance", "visual"),
) -> tuple[NiaFullScenarioCandidate, ...]:
    """Hold every runtime fact fixed and vary one chosen lesson attribute."""

    if not isinstance(scenario, NiaFullRouteScenario):
        raise TypeError("scenario must be NiaFullRouteScenario")
    rows = dict(scenario.self_lessons)
    selected = rows.get(week)
    if selected is None:
        raise ValueError(f"week {week} is not a self-lesson in this scenario")
    if week < scenario.initial_state.week:
        raise ValueError(
            "comparison week cannot precede the scenario checkpoint"
        )
    if len(attributes) < 2 or len(attributes) != len(set(attributes)):
        raise ValueError("attributes must contain at least two unique choices")
    candidates = []
    for attribute in attributes:
        replacement = NiaSelfLessonScenario(
            selected.phase,
            attribute,
            selected.is_sp,
            f"comparison:w{week}:self-lesson-{attribute}",
        )
        replaced_rows = tuple(
            (row_week, replacement if row_week == week else row)
            for row_week, row in scenario.self_lessons
        )
        candidate_scenario = replace(
            scenario,
            self_lessons=replaced_rows,
            authority_ref=f"{scenario.authority_ref}:compare-w{week}-{attribute}",
        )
        candidates.append(
            NiaFullScenarioCandidate(
                candidate_id=f"w{week}:self-lesson:{attribute}",
                label=f"W{week} 自主課程 {attribute}",
                scenario=candidate_scenario,
                decision_week=week,
                choice_id=attribute,
            )
        )
    return tuple(candidates)


def build_nia_outing_care_package_candidates(
    scenario: NiaFullRouteScenario,
    *,
    week: int,
    care_package: NiaCarePackageScenario | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[NiaFullScenarioCandidate, NiaFullScenarioCandidate]:
    """Compare an outing with one exact server-resolved Present scenario.

    The care-package branch is never synthesized from a Master random pool.
    It must already be present in ``scenario`` or be passed explicitly from a
    captured Present Start/Receive/End response envelope.
    """

    if not isinstance(scenario, NiaFullRouteScenario):
        raise TypeError("scenario must be NiaFullRouteScenario")
    if type(week) is not int or not 1 <= week <= 27:
        raise ValueError("comparison week must be within the route")
    if week < scenario.initial_state.week:
        raise ValueError("comparison week cannot precede the scenario checkpoint")
    available = set(
        load_route_calendar(
            "produce-004",
            master_dir=Path(master_dir),
        ).week(week).actions
    )
    if not {OUTING, CARE_PACKAGE}.issubset(available):
        raise ValueError("selected NIA week does not offer outing and care package")
    selected = care_package or dict(scenario.care_packages).get(week)
    if not isinstance(selected, NiaCarePackageScenario):
        raise ValueError(
            "care package comparison requires an exact Present response scenario"
        )

    def weekly(action: str) -> tuple[tuple[int, str], ...]:
        return tuple(
            (row_week, action if row_week == week else row_action)
            for row_week, row_action in scenario.weekly_actions
        )

    other_care_packages = tuple(
        (row_week, row)
        for row_week, row in scenario.care_packages
        if row_week != week
    )
    outing_scenario = replace(
        scenario,
        weekly_actions=weekly(OUTING),
        care_packages=other_care_packages,
        authority_ref=f"{scenario.authority_ref}:compare-w{week}-outing",
    )
    care_scenario = replace(
        scenario,
        weekly_actions=weekly(CARE_PACKAGE),
        care_packages=(*other_care_packages, (week, selected)),
        authority_ref=f"{scenario.authority_ref}:compare-w{week}-care-package",
    )
    return (
        NiaFullScenarioCandidate(
            candidate_id=f"w{week}:weekly:{OUTING}",
            label=f"W{week} 外出",
            scenario=outing_scenario,
            decision_week=week,
            choice_id=OUTING,
        ),
        NiaFullScenarioCandidate(
            candidate_id=f"w{week}:weekly:{CARE_PACKAGE}",
            label=f"W{week} 差入",
            scenario=care_scenario,
            decision_week=week,
            choice_id=CARE_PACKAGE,
        ),
    )


def compare_nia_full_route_scenarios(
    candidates: Iterable[NiaFullScenarioCandidate],
    *,
    objective: str = OBJECTIVE_FINAL_AUDITION_SCORE,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaFullScenarioComparison:
    """Replay and rank complete deterministic route candidates."""

    if objective not in _OBJECTIVES:
        raise ValueError("NIA comparison objective is unsupported")
    normalized = tuple(candidates)
    if len(normalized) < 2:
        raise ValueError("NIA comparison requires at least two candidates")
    evaluations = []
    for candidate in normalized:
        if not isinstance(candidate, NiaFullScenarioCandidate):
            raise TypeError("candidates must contain NiaFullScenarioCandidate")
        result = run_nia_produce004_fktn_full_fixed_scenario(
            scenario=candidate.scenario,
            database=Path(database),
            master_dir=Path(master_dir),
        )
        final = result.run.final_state
        evaluations.append(
            NiaFullScenarioEvaluation(
                candidate=candidate,
                result=result,
                audition_scores=tuple(
                    value.resolution.player_score for value in result.stages
                ),
                final_attributes=final.attributes,
                final_stamina=final.stamina,
            )
        )
    ranked = tuple(
        sorted(
            evaluations,
            key=lambda value: (
                -value.value_for(objective),
                -value.total_audition_score,
                -value.final_attribute_sum,
                -value.final_stamina,
                value.candidate.candidate_id,
            ),
        )
    )
    return NiaFullScenarioComparison(objective, ranked)


__all__ = [
    "OBJECTIVE_FINAL_ATTRIBUTE_SUM",
    "OBJECTIVE_FINAL_AUDITION_SCORE",
    "OBJECTIVE_TOTAL_AUDITION_SCORE",
    "NiaFullScenarioCandidate",
    "NiaFullScenarioComparison",
    "NiaFullScenarioEvaluation",
    "build_nia_self_lesson_attribute_candidates",
    "build_nia_outing_care_package_candidates",
    "compare_nia_full_route_scenarios",
]
