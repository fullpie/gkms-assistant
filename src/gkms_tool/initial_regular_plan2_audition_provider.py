"""Offline-route glue for one exact Initial Regular Plan2 audition scenario.

This module deliberately owns no audition formulas.  It joins the typed NPC
catalog/scenario boundary to the existing common audition frontier builder,
and exposes the resulting callable directly to
``InitialRegularOfflineBranchRegistry``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from .audition_rules import DEFAULT_MASTER_DIR
from .audition_turn_schedule import calculate_audition_base_multiplier_permils
from .exam_context import (
    AuditionProgressBonusValues,
    calculate_audition_bonus_permils,
)
from .initial_regular_audition_branch_catalog import (
    build_initial_regular_audition_frontier,
)
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedInnerBranch,
)
from .initial_regular_plan2_audition_runtime import (
    INITIAL_REGULAR_PRODUCE_ID,
    InitialRegularPlan2AuditionNpcCatalog,
    InitialRegularPlan2AuditionRuntimeResult,
    InitialRegularPlan2AuditionRuntimeIssue,
    InitialRegularPlan2AuditionScenario,
    build_initial_regular_plan2_audition_scenario_branch,
    load_initial_regular_plan2_audition_npc_catalog,
    resolve_initial_regular_plan2_audition,
)
from .initial_regular_plan2_inner_adapter import (
    InitialRegularPlan2AuditionResolution,
    InitialRegularPlan2TerminalContext,
)
from .initial_regular_weekly_adapter import InitialRegularWeeklyNode
from .master_db import DEFAULT_DATABASE
from .produce_rollout import ExternalRequest, ProduceRolloutState
from .produce_rollout_expectimax import OuterSearchIssue


INITIAL_REGULAR_PLAN2_AUDITION_PROVIDER_ID: Final = (
    "initial-regular.plan2.audition-scenario-provider"
)


def _runtime_pause(
    issues: tuple[InitialRegularPlan2AuditionRuntimeIssue, ...],
) -> InitialRegularOfflineBranchExpansion:
    """Map every typed runtime issue without prefixes or lost detail."""

    return InitialRegularOfflineBranchExpansion.paused(
        *(
            OuterSearchIssue(issue.code, issue.field, issue.detail)
            for issue in issues
        )
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionRuntimeBonusFactors:
    """Server/runtime factors applied after the stat-derived base permils."""

    audition_effect_parameter_bonus_permille: int | None
    vote_bonus_permille: int | None
    star_bonus_permille: int | None

    def __post_init__(self) -> None:
        for name in (
            "audition_effect_parameter_bonus_permille",
            "vote_bonus_permille",
            "star_bonus_permille",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be non-negative or None")


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionBattleBonusResult:
    """Static base plus the optional fully resolved runtime multiplier."""

    base_bonus_permille: tuple[int, int, int] | None
    final_bonus_permille: tuple[int, int, int] | None
    issues: tuple[InitialRegularPlan2AuditionRuntimeIssue, ...] = ()

    def __post_init__(self) -> None:
        for name in ("base_bonus_permille", "final_bonus_permille"):
            values = getattr(self, name)
            if values is not None and (
                len(values) != 3
                or any(type(value) is not int or value < 0 for value in values)
            ):
                raise ValueError(f"{name} must contain three non-negative integers")
        issues = tuple(self.issues)
        if any(
            not isinstance(value, InitialRegularPlan2AuditionRuntimeIssue)
            for value in issues
        ):
            raise TypeError("issues must contain typed runtime issues")
        if self.final_bonus_permille is None and not issues:
            raise ValueError("an unresolved final bonus requires typed issues")
        if self.final_bonus_permille is not None and issues:
            raise ValueError("a resolved final bonus cannot carry issues")
        object.__setattr__(self, "issues", issues)

    @property
    def ready(self) -> bool:
        return self.final_bonus_permille is not None and not self.issues


def calculate_initial_regular_plan2_audition_battle_bonus(
    state: ProduceRolloutState,
    catalog: InitialRegularPlan2AuditionNpcCatalog,
    factors: InitialRegularPlan2AuditionRuntimeBonusFactors | None,
) -> InitialRegularPlan2AuditionBattleBonusResult:
    """Resolve exact Vo/Da/Vi permils without reading runtime-owned factors.

    The base delegates the Android CalcPermil/interpolation, combined Setting
    penalty, integer division, and tens rounding to the authoritative pure
    helper in ``audition_turn_schedule``.  E/Vote/Star use the already-public
    float32 helper in ``exam_context`` and must be supplied explicitly.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(catalog, InitialRegularPlan2AuditionNpcCatalog):
        raise TypeError("catalog must be InitialRegularPlan2AuditionNpcCatalog")
    if factors is not None and not isinstance(
        factors, InitialRegularPlan2AuditionRuntimeBonusFactors
    ):
        raise TypeError("factors must be typed or None")
    issues: list[InitialRegularPlan2AuditionRuntimeIssue] = []
    configured = catalog.configured_parameter_values
    if configured is None:
        issues.append(
            InitialRegularPlan2AuditionRuntimeIssue(
                "plan2-audition-configured-parameters-unresolved",
                "catalog.configured_parameter_values",
            )
        )
    penalties = catalog.score_penalty_permille
    if penalties is None:
        issues.append(
            InitialRegularPlan2AuditionRuntimeIssue(
                "plan2-audition-score-penalty-unresolved",
                "catalog.score_penalty_permille",
            )
        )
    base: tuple[int, int, int] | None = None
    if configured is not None and penalties is not None:
        try:
            base = calculate_audition_base_multiplier_permils(
                score_curve=tuple(
                    (
                        point.parameter,
                        point.vocal_permille,
                        point.dance_permille,
                        point.visual_permille,
                    )
                    for point in catalog.score_curve
                ),
                attributes=(
                    state.attributes.vocal,
                    state.attributes.dance,
                    state.attributes.visual,
                ),
                configured_parameters=configured,
                penalty_min_permille=penalties[0],
                penalty_max_permille=penalties[1],
            )
        except (TypeError, ValueError) as error:
            issues.append(
                InitialRegularPlan2AuditionRuntimeIssue(
                    "plan2-audition-base-bonus-invalid",
                    "catalog.score_curve",
                    f"{type(error).__name__}:{error}",
                )
            )
    for name in (
        "audition_effect_parameter_bonus_permille",
        "vote_bonus_permille",
        "star_bonus_permille",
    ):
        if factors is None or getattr(factors, name) is None:
            issues.append(
                InitialRegularPlan2AuditionRuntimeIssue(
                    "plan2-audition-runtime-bonus-unresolved",
                    f"bonus_factors.{name}",
                )
            )
    if issues:
        return InitialRegularPlan2AuditionBattleBonusResult(
            base,
            None,
            tuple(issues),
        )
    assert base is not None
    assert factors is not None
    assert factors.audition_effect_parameter_bonus_permille is not None
    assert factors.vote_bonus_permille is not None
    assert factors.star_bonus_permille is not None
    try:
        resolved = calculate_audition_bonus_permils(
            AuditionProgressBonusValues(
                vocal_permil=base[0],
                dance_permil=base[1],
                visual_permil=base[2],
                vote_bonus_permil=factors.vote_bonus_permille,
                star_bonus_permil=factors.star_bonus_permille,
                audition_effect_parameter_bonus_permil=(
                    factors.audition_effect_parameter_bonus_permille
                ),
            )
        )
    except (TypeError, ValueError) as error:
        return InitialRegularPlan2AuditionBattleBonusResult(
            base,
            None,
            (
                InitialRegularPlan2AuditionRuntimeIssue(
                    "plan2-audition-final-bonus-invalid",
                    "bonus_factors",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    return InitialRegularPlan2AuditionBattleBonusResult(
        base,
        (resolved.vocal, resolved.dance, resolved.visual),
        (),
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2ResolvedAuditionScenario:
    """Pure resolution shared by route and terminal provider surfaces."""

    catalog: InitialRegularPlan2AuditionNpcCatalog | None
    scenario: InitialRegularPlan2AuditionScenario | None
    base_bonus_permille: tuple[int, int, int] | None
    issues: tuple[InitialRegularPlan2AuditionRuntimeIssue, ...] = ()

    def __post_init__(self) -> None:
        issues = tuple(self.issues)
        if any(
            not isinstance(value, InitialRegularPlan2AuditionRuntimeIssue)
            for value in issues
        ):
            raise TypeError("issues must contain typed runtime issues")
        if self.scenario is not None and not isinstance(
            self.scenario, InitialRegularPlan2AuditionScenario
        ):
            raise TypeError("scenario must be typed or None")
        if self.catalog is not None and not isinstance(
            self.catalog, InitialRegularPlan2AuditionNpcCatalog
        ):
            raise TypeError("catalog must be typed or None")
        if (self.scenario is None) == (not issues):
            raise ValueError("resolved scenario must be either ready or blocked")
        object.__setattr__(self, "issues", issues)

    @property
    def ready(self) -> bool:
        return self.scenario is not None and self.catalog is not None and not self.issues


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2AuditionScenarioProvider:
    """One exact scenario as a registry-compatible branch provider."""

    idol_card_id: str
    scenario: InitialRegularPlan2AuditionScenario
    catalog: InitialRegularPlan2AuditionNpcCatalog | None = None
    bonus_factors: InitialRegularPlan2AuditionRuntimeBonusFactors | None = None
    database: Path = DEFAULT_DATABASE
    master_dir: Path = DEFAULT_MASTER_DIR

    def __post_init__(self) -> None:
        if not isinstance(self.idol_card_id, str) or not self.idol_card_id:
            raise ValueError("idol_card_id must be non-empty text")
        if not isinstance(self.scenario, InitialRegularPlan2AuditionScenario):
            raise TypeError("scenario must be InitialRegularPlan2AuditionScenario")
        if self.catalog is not None and not isinstance(
            self.catalog, InitialRegularPlan2AuditionNpcCatalog
        ):
            raise TypeError("catalog must be typed or None")
        if self.bonus_factors is not None and not isinstance(
            self.bonus_factors,
            InitialRegularPlan2AuditionRuntimeBonusFactors,
        ):
            raise TypeError("bonus_factors must be typed or None")
        object.__setattr__(self, "database", Path(self.database))
        object.__setattr__(self, "master_dir", Path(self.master_dir))

    def resolve_scenario(
        self,
        state: ProduceRolloutState,
    ) -> InitialRegularPlan2ResolvedAuditionScenario:
        """Purely bind catalog, outer attributes, and explicit bonus inputs."""

        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        selected_catalog = self.catalog
        if selected_catalog is None:
            loaded = load_initial_regular_plan2_audition_npc_catalog(
                self.idol_card_id,
                stage_type=self.scenario.stage_type,
                number=self.scenario.number,
                produce_id=INITIAL_REGULAR_PRODUCE_ID,
                master_dir=self.master_dir,
            )
            if not loaded.ready:
                return InitialRegularPlan2ResolvedAuditionScenario(
                    None,
                    None,
                    None,
                    loaded.issues,
                )
            selected_catalog = loaded.catalog
        assert selected_catalog is not None

        scenario = self.scenario
        base_bonus: tuple[int, int, int] | None = None
        if self.bonus_factors is not None or scenario.battle_bonus_permille is None:
            bonuses = calculate_initial_regular_plan2_audition_battle_bonus(
                state,
                selected_catalog,
                self.bonus_factors,
            )
            base_bonus = bonuses.base_bonus_permille
            if not bonuses.ready:
                return InitialRegularPlan2ResolvedAuditionScenario(
                    selected_catalog,
                    None,
                    base_bonus,
                    bonuses.issues,
                )
            assert bonuses.final_bonus_permille is not None
            if (
                scenario.battle_bonus_permille is not None
                and scenario.battle_bonus_permille
                != bonuses.final_bonus_permille
            ):
                return InitialRegularPlan2ResolvedAuditionScenario(
                    selected_catalog,
                    None,
                    base_bonus,
                    (
                        InitialRegularPlan2AuditionRuntimeIssue(
                            "plan2-audition-scenario-bonus-mismatch",
                            "scenario.battle_bonus_permille",
                            (
                                f"calculated={bonuses.final_bonus_permille}:"
                                f"scenario={scenario.battle_bonus_permille}"
                            ),
                        ),
                    ),
                )
            scenario = replace(
                scenario,
                battle_bonus_permille=bonuses.final_bonus_permille,
            )
        return InitialRegularPlan2ResolvedAuditionScenario(
            selected_catalog,
            scenario,
            base_bonus,
            (),
        )

    def resolve_terminal(
        self,
        context: InitialRegularPlan2TerminalContext,
    ) -> InitialRegularPlan2AuditionRuntimeResult:
        """Use the same pure scenario resolution at the terminal boundary."""

        if not isinstance(context, InitialRegularPlan2TerminalContext):
            raise TypeError("context must be InitialRegularPlan2TerminalContext")
        resolved = self.resolve_scenario(context.request.outer_state)
        if not resolved.ready:
            return InitialRegularPlan2AuditionRuntimeResult(
                resolved.catalog,
                None,
                None,
                (),
                resolved.issues,
            )
        assert resolved.catalog is not None
        assert resolved.scenario is not None
        return resolve_initial_regular_plan2_audition(
            context,
            resolved.scenario,
            catalog=resolved.catalog,
            master_dir=self.master_dir,
            produce_id=INITIAL_REGULAR_PRODUCE_ID,
        )

    def audition_resolution_provider(
        self,
        context: InitialRegularPlan2TerminalContext,
    ) -> InitialRegularPlan2AuditionResolution | None:
        """Bound callback matching ``Plan2AuditionResolutionProvider``."""

        return self.resolve_terminal(context).resolution

    def __call__(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        weekly_node: InitialRegularWeeklyNode | None,
    ) -> InitialRegularOfflineBranchExpansion:
        """Build one common inner branch or return the complete typed pause."""

        del weekly_node  # Audition requests have no weekly-node interpretation.
        resolved = self.resolve_scenario(state)
        if not resolved.ready:
            return _runtime_pause(resolved.issues)
        assert resolved.catalog is not None
        assert resolved.scenario is not None
        selected_catalog = resolved.catalog
        scenario = resolved.scenario

        projected = build_initial_regular_plan2_audition_scenario_branch(
            scenario,
            idol_card_id=self.idol_card_id,
            catalog=selected_catalog,
            master_dir=self.master_dir,
            produce_id=INITIAL_REGULAR_PRODUCE_ID,
        )
        if not projected.ready:
            return _runtime_pause(projected.issues)
        assert projected.branch is not None

        frontier = build_initial_regular_audition_frontier(
            state,
            request,
            idol_card_id=self.idol_card_id,
            branches=(projected.branch,),
            database=self.database,
            master_dir=self.master_dir,
        )
        if frontier.issues:
            return frontier

        # The existing frontier builder owns pending/request, card/profile,
        # stage rules, border, and common-request validation.  Only assert
        # that its resolved identity is the catalog selected above; no rule
        # arithmetic is repeated here.
        for raw_branch in frontier.branches:
            if not isinstance(raw_branch, InitialRegularWeightedInnerBranch):
                return InitialRegularOfflineBranchExpansion.paused(
                    OuterSearchIssue(
                        "plan2-audition-provider-branch-kind-invalid",
                        "audition_frontier.branches",
                        type(raw_branch).__name__,
                    )
                )
            inner = raw_branch.inner_request
            actual = (
                inner.idol_card_id,
                inner.stage_type,
                inner.stage_id,
                inner.setting_id,
            )
            expected = (
                selected_catalog.idol_card_id,
                selected_catalog.stage_type,
                selected_catalog.stage_id,
                selected_catalog.setting_id,
            )
            if actual != expected:
                return InitialRegularOfflineBranchExpansion.paused(
                    OuterSearchIssue(
                        "plan2-audition-provider-catalog-identity-mismatch",
                        "audition_frontier.inner_request",
                        f"expected={expected!r}:actual={actual!r}",
                    )
                )
        return frontier


def build_initial_regular_plan2_audition_scenario_provider(
    *,
    idol_card_id: str,
    scenario: InitialRegularPlan2AuditionScenario,
    catalog: InitialRegularPlan2AuditionNpcCatalog | None = None,
    bonus_factors: InitialRegularPlan2AuditionRuntimeBonusFactors | None = None,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularPlan2AuditionScenarioProvider:
    """Small factory convenient when declaring a branch-registry route."""

    return InitialRegularPlan2AuditionScenarioProvider(
        idol_card_id=idol_card_id,
        scenario=scenario,
        catalog=catalog,
        bonus_factors=bonus_factors,
        database=database,
        master_dir=master_dir,
    )


__all__ = [
    "INITIAL_REGULAR_PLAN2_AUDITION_PROVIDER_ID",
    "InitialRegularPlan2AuditionBattleBonusResult",
    "InitialRegularPlan2AuditionRuntimeBonusFactors",
    "InitialRegularPlan2AuditionScenarioProvider",
    "InitialRegularPlan2ResolvedAuditionScenario",
    "build_initial_regular_plan2_audition_scenario_provider",
    "calculate_initial_regular_plan2_audition_battle_bonus",
]
