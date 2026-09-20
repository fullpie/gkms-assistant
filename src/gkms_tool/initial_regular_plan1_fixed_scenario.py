"""Deterministic Initial Regular Plan 1 W1 -> Final acceptance.

The existing fixed-run driver and ``ProduceRolloutKernel`` own the outer
state machine.  This module wires a tiny caller/server-owned branch registry:
week 1 lesson goes through the Plan 1 common adapter, explicit caller-owned
reward offers and response GUIDs are resolved and selected, generic weekly
rows advance the preserved FKTN
calendar, and Mid1/Final use explicit NPC terminal-score scenarios plus the
typed Plan 1 audition resolver.  The deterministic whole-run smoke is still
caller-authoritative: it does not claim an NPC RNG distribution or an optimal
strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Final

from .audition_rules import FINAL, MID1
from .initial_regular_branch_registry import (
    InitialRegularBranchRoute,
    InitialRegularBranchRouteKey,
    InitialRegularOfflineBranchRegistry,
)
from .initial_regular_fixed_run import (
    InitialRegularFixedRunResult,
    InitialRegularFixedRunStop,
    run_initial_regular_fixed_scenario,
)
from .initial_regular_fixed_run_report import (
    InitialRegularFixedRunReport,
    build_initial_regular_fixed_run_report,
)
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageAdapterRegistry,
    InitialRegularInnerStageRequest,
    InitialRegularInnerStageKind,
    build_initial_regular_lesson_stage_request,
)
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedInnerBranch,
    InitialRegularWeightedResolvedWeeklyBranch,
    InitialRegularWeightedRewardBranch,
    InitialRegularOfflineChanceAdapter,
)
from .initial_regular_plan1_inner_adapter import (
    InitialRegularPlan1DeterministicScenario,
    InitialRegularPlan1InnerAdapter,
)
from .initial_regular_plan1_stage_bootstrap import (
    Plan1StageBootstrap,
    Plan1StageBootstrapAuthority,
    Plan1StageOuterStateIdentity,
    provision_plan1_stage,
)
from .initial_regular_plan1_run_runtime_loadout import (
    Plan1RunRuntimeLoadoutBundle,
    Plan1RunRuntimeLoadoutError,
)
from .initial_regular_plan1_audition_adapter import (
    InitialRegularPlan1AuditionResolution,
    InitialRegularPlan1AuditionRewardRule,
    InitialRegularPlan1AuditionScenario,
    InitialRegularPlan1NpcScenarioAuthority,
    InitialRegularPlan1NpcTerminalScore,
    resolve_initial_regular_plan1_audition,
)
from .initial_regular_plan2_audition_runtime import (
    load_initial_regular_plan2_audition_npc_catalog,
)
from .initial_regular_plan1_outer_acceptance import (
    InitialRegularPlan1LessonRuntimeContext,
    InitialRegularPlan1LessonStageFact,
)
from .plan1_native_terminal_acceptance import (
    Plan1TerminalFixture,
    build_fktn_ssr_plan1_terminal_fixture,
    run_fktn_ssr_plan1_terminal_acceptance,
)
from .plan1_native_core import Plan1ScalarState
from .produce_rollout import (
    ActionKind,
    AttributeValues,
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    InnerExamOutcome,
    ProduceAction,
    ProduceRolloutKernel,
    ProduceRolloutState,
    ProduceStatePatch,
    DeckEntry,
    RewardOffer,
    RewardOffersOutcome,
    RolloutPhase,
    WeeklyActionOutcome,
)
from .produce_rollout_expectimax import (
    ExternalChanceExpansion,
    OuterSearchIssue,
    WeightedExternalOutcome,
)
from .route_calendar import (
    DEFAULT_MASTER_DIR,
    LESSON,
    RouteCalendar,
    load_route_calendar,
)


PLAN1_W1_MID1_SCENARIO_LABEL: Final = "fktn-plan1-w1-mid1-deterministic"
PLAN1_W1_FINAL_SCENARIO_LABEL: Final = "fktn-plan1-w1-final-deterministic"
PLAN1_FIXED_SYNTHETIC_INITIAL_STAMINA: Final = 30
PLAN1_FIXED_SYNTHETIC_MAX_STAMINA: Final = 30
PLAN1_FIXED_SYNTHETIC_INITIAL_SCORE: Final = 600
PLAN1_FIXED_SYNTHETIC_ATTRIBUTES: Final = AttributeValues(100, 100, 100)
PLAN1_FIXED_SYNTHETIC_BATTLE_BONUS_PERMILLE: Final = (1000, 1000, 1000)
PLAN1_FIXED_SYNTHETIC_LESSON_MULTIPLE_PERMILLE: Final = 1000
PLAN1_FIXED_SYNTHETIC_RNG_BEFORE: Final = 0


def build_fktn_plan1_fixed_smoke_stage_bootstrap(
    request: InitialRegularInnerStageRequest,
) -> Plan1StageBootstrap:
    """Bind the fixed caller scenario to one exact common request.

    Every scalar below is deterministic synthetic server-smoke authority.  It
    is intentionally not described as a Master/loadout-derived stage start.
    """

    return Plan1StageBootstrap(
        authority=Plan1StageBootstrapAuthority.SYNTHETIC_FIXED_SMOKE,
        authority_detail=(
            "deterministic caller-owned Initial Regular fixed smoke; "
            "not Master/loadout authority"
        ),
        outer_state_identity=Plan1StageOuterStateIdentity.from_request(request),
        initial_state=Plan1ScalarState(
            score=PLAN1_FIXED_SYNTHETIC_INITIAL_SCORE,
            stamina=PLAN1_FIXED_SYNTHETIC_INITIAL_STAMINA,
            max_stamina=PLAN1_FIXED_SYNTHETIC_MAX_STAMINA,
            block=0,
            parameter_buff_turns=0,
            lesson_buff=0,
            turn=1,
            plays_remaining=1,
            play_count=0,
            limit_border=-1,
            parameter_buff_multiple_per_turn_turns=0,
            stamina_consumption_down_turns=0,
            stamina_consumption_down_fresh=False,
            stamina_consumption_add_turns=0,
            stamina_consumption_add_fresh=False,
            stamina_consumption_down_fixed=0,
            lesson_buff_multiple_permille=(
                PLAN1_FIXED_SYNTHETIC_LESSON_MULTIPLE_PERMILLE
            ),
            anti_debuff_count=0,
            total_effect_draw_card_count=0,
        ),
        attributes=PLAN1_FIXED_SYNTHETIC_ATTRIBUTES,
        battle_bonus_permille=PLAN1_FIXED_SYNTHETIC_BATTLE_BONUS_PERMILLE,
        lesson_buff_multiple_permille=(
            PLAN1_FIXED_SYNTHETIC_LESSON_MULTIPLE_PERMILLE
        ),
        rng_before=PLAN1_FIXED_SYNTHETIC_RNG_BEFORE,
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1W1Mid1Result:
    """Fixed-run artefacts plus the shared GUI scalar report projection."""

    run: InitialRegularFixedRunResult
    scenario_label: str
    w1_inner_request: InitialRegularInnerStageRequest | None
    w1_weekly_outcome: WeeklyActionOutcome | None
    w1_reward_offers: RewardOffersOutcome | None
    mid1_resolution: InitialRegularPlan1AuditionResolution | None = None
    mid1_outcome: InnerExamOutcome | None = None
    mid1_reward_offers: RewardOffersOutcome | None = None
    final_resolution: InitialRegularPlan1AuditionResolution | None = None
    final_outcome: InnerExamOutcome | None = None
    final_reward_offers: RewardOffersOutcome | None = None
    post_mid1_reward_offers: RewardOffersOutcome | None = None
    report: InitialRegularFixedRunReport | None = None
    mid1_inner_request: InitialRegularInnerStageRequest | None = None
    final_inner_request: InitialRegularInnerStageRequest | None = None
    w9_inner_request: InitialRegularInnerStageRequest | None = None
    runtime_loadout_request_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.report is not None and not isinstance(
            self.report, InitialRegularFixedRunReport
        ):
            raise TypeError("report must be InitialRegularFixedRunReport or None")
        for name in (
            "mid1_inner_request",
            "final_inner_request",
            "w9_inner_request",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(
                value, InitialRegularInnerStageRequest
            ):
                raise TypeError(
                    f"{name} must be InitialRegularInnerStageRequest or None"
                )
        request_ids = tuple(self.runtime_loadout_request_ids)
        if any(not isinstance(value, str) or not value for value in request_ids):
            raise ValueError(
                "runtime_loadout_request_ids must contain non-empty text"
            )
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("runtime_loadout_request_ids must be unique")
        object.__setattr__(self, "runtime_loadout_request_ids", request_ids)

    @property
    def fixed_run_report(self) -> InitialRegularFixedRunReport:
        """Return the scalar GUI projection without rerunning the scenario."""

        return self.report or build_initial_regular_fixed_run_report(self.run)

    @property
    def final_state(self) -> ProduceRolloutState:
        return self.run.final_state

    @property
    def completed_prefix(self) -> bool:
        return (
            self.run.stop is InitialRegularFixedRunStop.POLICY
            and self.run.final_state.week == 7
            and self.w1_weekly_outcome is not None
            and self.w1_reward_offers is not None
            and self.mid1_outcome is not None
        )

    @property
    def mid1_blocked(self) -> bool:
        return self.run.stop is InitialRegularFixedRunStop.EXTERNAL_BLOCKED

    @property
    def mid1_resolved(self) -> bool:
        return self.mid1_outcome is not None and self.mid1_resolution is not None

    @property
    def final_resolved(self) -> bool:
        return self.final_outcome is not None and self.final_resolution is not None

    @property
    def completed(self) -> bool:
        """Whether the injected fixed run reached the kernel terminal state."""

        return self.run.completed


def _outer_deck_from_fixture(fixture: Plan1TerminalFixture) -> tuple[DeckEntry, ...]:
    """Project the fixture's Master manifest into the outer deck shadow.

    The native fixture remains authoritative for GUID/runtime state.  Grouping
    its typed ``card_universe`` by the same Master manifest keys preserves the
    exact eleven-card count *and* the caller-owned fixture GUIDs without
    inventing a second identity list.  Draw order remains owned by the inner
    fixture rather than this outer stack projection.
    """

    if not isinstance(fixture, Plan1TerminalFixture):
        raise TypeError("fixture must be Plan1TerminalFixture")
    grouped: dict[tuple[str, int], int] = {}
    key_order: list[tuple[str, int]] = []
    for entry in fixture.compilation.manifest.entries:
        key = (entry.card_id, entry.upgrade)
        if key not in grouped:
            key_order.append(key)
        grouped[key] = grouped.get(key, 0) + 1

    guids_by_key: dict[tuple[str, int], list[str]] = {}
    for card in fixture.stage.zones.card_universe:
        key = (card.card_id, card.effective_upgrade)
        guids_by_key.setdefault(key, []).append(card.guid)
    if set(guids_by_key) != set(grouped):
        raise ValueError("Plan1 fixture card universe diverges from its Master manifest")
    for key, count in grouped.items():
        if len(guids_by_key[key]) != count:
            raise ValueError("Plan1 fixture GUID multiplicity diverges from its manifest")

    return tuple(
        DeckEntry(
            card_id=card_id,
            upgrade=upgrade,
            count=grouped[(card_id, upgrade)],
            instance_ids=tuple(guids_by_key[(card_id, upgrade)]),
        )
        for card_id, upgrade in key_order
    )


def _initial_state(*, fixture: Plan1TerminalFixture) -> ProduceRolloutState:
    return ProduceRolloutState(
        mode_id="produce-001",
        character_id="fktn",
        week=1,
        total_weeks=13,
        phase=RolloutPhase.READY_FOR_WEEK,
        stamina=20,
        max_stamina=PLAN1_FIXED_SYNTHETIC_MAX_STAMINA,
        attributes=PLAN1_FIXED_SYNTHETIC_ATTRIBUTES,
        deck=_outer_deck_from_fixture(fixture),
    )


def _generic_weekly_provider(
    state: ProduceRolloutState,
    request: ExternalRequest,
    _weekly_node: object,
) -> InitialRegularOfflineBranchExpansion:
    outcome = WeeklyActionOutcome(
        request_id=request.request_id,
        branch=ChanceBranch(
            f"server:weekly:w{request.week}:{request.action_id}",
            probability=1.0,
            rng_token=f"rng:weekly:w{request.week}",
        ),
        patch=ProduceStatePatch(stamina=state.stamina),
        reward_requested=False,
    )
    return InitialRegularOfflineBranchExpansion.resolved(
        InitialRegularWeightedResolvedWeeklyBranch(Fraction(1), outcome)
    )


def _mid1_blocked_provider(
    _state: ProduceRolloutState,
    request: ExternalRequest,
    _weekly_node: object,
) -> InitialRegularOfflineBranchExpansion:
    return InitialRegularOfflineBranchExpansion.paused(
        OuterSearchIssue(
            "plan1-mid1-audition-resolver-unresolved",
            "inner_registry.plan1",
            f"stage={request.stage_type};rank/NPC runtime evidence is not supplied",
        )
    )


def run_fktn_plan1_w1_to_mid1_fixed_scenario(
    *,
    initial_state: ProduceRolloutState | None = None,
    kernel: ProduceRolloutKernel | None = None,
    calendar: RouteCalendar | None = None,
    fixture: Plan1TerminalFixture | None = None,
    run_runtime_loadout: Plan1RunRuntimeLoadoutBundle | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    stop_after_week: int = 7,
) -> InitialRegularPlan1W1Mid1Result:
    """Run W1 lesson/reward, resolve Mid1, and stop at week 7 by default.

    The route/calendar, branch registry, reward rows, NPC terminal scores, and
    RNG labels are all explicit deterministic inputs.  The scenario is a
    caller-authoritative server smoke: it does not roll NPC RNG or infer a
    general ranking/reward distribution.  ``stop_after_week=13`` is used by
    :func:`run_fktn_plan1_w1_to_final_fixed_scenario` to continue this exact
    assembly through Final.
    """

    if stop_after_week not in (7, 13):
        raise ValueError("stop_after_week must be 7 or 13")

    if fixture is None:
        fixture = build_fktn_ssr_plan1_terminal_fixture()
    if not isinstance(fixture, Plan1TerminalFixture):
        raise TypeError("fixture must be Plan1TerminalFixture")
    if run_runtime_loadout is not None and not isinstance(
        run_runtime_loadout, Plan1RunRuntimeLoadoutBundle
    ):
        raise TypeError(
            "run_runtime_loadout must be Plan1RunRuntimeLoadoutBundle or None"
        )
    if initial_state is None:
        initial_state = _initial_state(fixture=fixture)
    if calendar is None:
        calendar = load_route_calendar(
            "produce-001",
            character_id=initial_state.character_id,
            master_dir=master_dir,
        )
    if kernel is None:
        kernel = ProduceRolloutKernel(calendar, rest_recovery_permille=700)
    else:
        if not isinstance(kernel, ProduceRolloutKernel):
            raise TypeError("kernel must be ProduceRolloutKernel")
    captured_inner: list[InitialRegularInnerStageRequest] = []
    captured_weekly: list[WeeklyActionOutcome] = []
    captured_rewards: list[RewardOffersOutcome] = []
    captured_mid1: list[InitialRegularPlan1AuditionResolution] = []
    captured_mid1_outcomes: list[InnerExamOutcome] = []
    captured_mid1_rewards: list[RewardOffersOutcome] = []
    captured_final: list[InitialRegularPlan1AuditionResolution] = []
    captured_final_outcomes: list[InnerExamOutcome] = []
    captured_final_rewards: list[RewardOffersOutcome] = []
    captured_post_mid1_rewards: list[RewardOffersOutcome] = []
    captured_mid1_requests: list[InitialRegularInnerStageRequest] = []
    captured_final_requests: list[InitialRegularInnerStageRequest] = []
    captured_w9_requests: list[InitialRegularInnerStageRequest] = []
    used_runtime_loadout_request_ids: list[str] = []

    def build_result(
        run: InitialRegularFixedRunResult,
    ) -> InitialRegularPlan1W1Mid1Result:
        weekly_outcome = next(
            (
                step.outcome
                for step in run.steps
                if step.outcome is not None
                and step.request is not None
                and step.request.week == 1
                and step.request.kind is ExternalKind.WEEKLY_ACTION_OUTCOME
            ),
            None,
        )
        return InitialRegularPlan1W1Mid1Result(
            run=run,
            scenario_label=(
                PLAN1_W1_FINAL_SCENARIO_LABEL
                if stop_after_week == 13
                else PLAN1_W1_MID1_SCENARIO_LABEL
            ),
            w1_inner_request=(captured_inner[0] if captured_inner else None),
            w1_weekly_outcome=(
                weekly_outcome
                if isinstance(weekly_outcome, WeeklyActionOutcome)
                else (captured_weekly[0] if captured_weekly else None)
            ),
            w1_reward_offers=(captured_rewards[0] if captured_rewards else None),
            mid1_resolution=(captured_mid1[0] if captured_mid1 else None),
            mid1_outcome=(
                captured_mid1_outcomes[0] if captured_mid1_outcomes else None
            ),
            mid1_reward_offers=(
                captured_mid1_rewards[0] if captured_mid1_rewards else None
            ),
            final_resolution=(captured_final[0] if captured_final else None),
            final_outcome=(
                captured_final_outcomes[0] if captured_final_outcomes else None
            ),
            final_reward_offers=(
                captured_final_rewards[0] if captured_final_rewards else None
            ),
            post_mid1_reward_offers=(
                captured_post_mid1_rewards[0]
                if captured_post_mid1_rewards
                else None
            ),
            report=build_initial_regular_fixed_run_report(run),
            mid1_inner_request=(
                captured_mid1_requests[0] if captured_mid1_requests else None
            ),
            final_inner_request=(
                captured_final_requests[0] if captured_final_requests else None
            ),
            w9_inner_request=(
                captured_w9_requests[0] if captured_w9_requests else None
            ),
            runtime_loadout_request_ids=tuple(
                used_runtime_loadout_request_ids
            ),
        )

    if run_runtime_loadout is not None:
        identity_issues: list[OuterSearchIssue] = []
        if run_runtime_loadout.produce_id != initial_state.mode_id:
            identity_issues.append(
                OuterSearchIssue(
                    "plan1-run-loadout-produce-mismatch",
                    "run_runtime_loadout.produce_id",
                    f"outer={initial_state.mode_id}:bundle={run_runtime_loadout.produce_id}",
                )
            )
        if run_runtime_loadout.character_id != initial_state.character_id:
            identity_issues.append(
                OuterSearchIssue(
                    "plan1-run-loadout-character-mismatch",
                    "run_runtime_loadout.character_id",
                    f"outer={initial_state.character_id}:bundle={run_runtime_loadout.character_id}",
                )
            )
        if identity_issues:
            return build_result(
                InitialRegularFixedRunResult(
                    initial_state=initial_state,
                    final_state=initial_state,
                    stop=InitialRegularFixedRunStop.EXTERNAL_BLOCKED,
                    issues=tuple(identity_issues),
                    steps=(),
                )
            )

    def scenario_for_request(
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularPlan1DeterministicScenario:
        if run_runtime_loadout is None:
            bootstrap = build_fktn_plan1_fixed_smoke_stage_bootstrap(request)
            support_resolver = None
        else:
            if request.external_request_id in used_runtime_loadout_request_ids:
                raise Plan1RunRuntimeLoadoutError(
                    "plan1-run-loadout-stage-reused",
                    "request.external_request_id",
                    request.external_request_id,
                )
            projected = run_runtime_loadout.provision_stage(
                request.external_request_id,
                fixture,
            )
            bootstrap = projected.bootstrap
            support_resolver = projected.hand_add_support_resolver
        provision = provision_plan1_stage(
            request,
            bootstrap,
            fixture.stage,
        )
        if provision.issues:
            issue = provision.issues[0]
            raise Plan1RunRuntimeLoadoutError(
                issue.code,
                issue.field,
                issue.detail,
            )
        assert provision.stage is not None
        if run_runtime_loadout is not None:
            used_runtime_loadout_request_ids.append(request.external_request_id)
        return InitialRegularPlan1DeterministicScenario(
            replace(
                fixture,
                stage=provision.stage,
                bootstrap=bootstrap,
            ),
            bootstrap=bootstrap,
            hand_add_support_resolver=support_resolver,
        )

    npc_scenario = InitialRegularPlan1AuditionScenario(
        stage_type=MID1,
        number=1,
        npc_group_id="p_npc_group-fktn-produce-001-1-04",
        authority=InitialRegularPlan1NpcScenarioAuthority.SERVER_RUNTIME,
        probability=Fraction(1),
        rng_before=0,
        battle_bonus_permille=PLAN1_FIXED_SYNTHETIC_BATTLE_BONUS_PERMILLE,
        gimmick_group_id="",
        npc_score_multiple_permille=1000,
        npc_scores=(
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-1-04", 1, 620
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-1-04", 2, 450
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-1-04", 3, 269
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-1-04", 4, 269
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-1-04", 5, 269
            ),
        ),
        reward_rule=InitialRegularPlan1AuditionRewardRule(
            stage_type=MID1,
            number=1,
            reward_requested_by_rank=(True, True, True, True, True, False),
        ),
        branch_id="server:plan1-mid1",
        rng_token="rng:plan1-mid1",
    )
    final_npc_scenario = InitialRegularPlan1AuditionScenario(
        stage_type=FINAL,
        number=1,
        npc_group_id="p_npc_group-fktn-produce-001-2-04",
        authority=InitialRegularPlan1NpcScenarioAuthority.SERVER_RUNTIME,
        probability=Fraction(1),
        rng_before=0,
        # Caller-owned runtime rows are explicit; no battle/gimmick formula
        # is inferred by this deterministic smoke.
        battle_bonus_permille=PLAN1_FIXED_SYNTHETIC_BATTLE_BONUS_PERMILLE,
        gimmick_group_id=(
            "p_exam_gimmick-produce-001-exam_parameter_buff_01-audition-after_mid"
        ),
        npc_score_multiple_permille=1000,
        # Every score is inside its corresponding Master NPC range.  NPC #3
        # ties the player at 684, exercising strict greater-than rank semantics.
        npc_scores=(
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-2-04", 1, 2381
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-2-04", 2, 1450
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-2-04", 3, 684
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-2-04", 4, 594
            ),
            InitialRegularPlan1NpcTerminalScore(
                "p_npc_group-fktn-produce-001-2-04", 5, 515
            ),
        ),
        reward_rule=InitialRegularPlan1AuditionRewardRule(
            stage_type=FINAL,
            number=1,
            reward_requested_by_rank=(True, True, True, True, True, False),
        ),
        branch_id="server:plan1-final",
        rng_token="rng:plan1-final",
    )

    def w1_inner_provider(
        state: ProduceRolloutState,
        request: ExternalRequest,
        _weekly_node: object,
    ) -> InitialRegularOfflineBranchExpansion:
        try:
            inner_request = build_initial_regular_lesson_stage_request(
                state,
                request,
                InitialRegularPlan1LessonStageFact(
                    runtime_context=InitialRegularPlan1LessonRuntimeContext(
                        battle_bonus_permille=(
                            PLAN1_FIXED_SYNTHETIC_BATTLE_BONUS_PERMILLE
                        )
                    )
                ),
                branch=ChanceBranch("server:plan1-w1", probability=1.0, rng_token="rng:plan1-w1"),
                idol_card_id="i_card-fktn-3-001",
                rng_before=PLAN1_FIXED_SYNTHETIC_RNG_BEFORE,
            )
        except Exception as error:
            return InitialRegularOfflineBranchExpansion.paused(
                OuterSearchIssue(
                    "plan1-w1-inner-request-failed",
                    "branch_registry.plan1",
                    f"{type(error).__name__}:{error}",
                )
            )
        captured_inner.append(inner_request)
        return InitialRegularOfflineBranchExpansion.resolved(
            InitialRegularWeightedInnerBranch(Fraction(1), inner_request)
        )

    def w1_reward_provider(
        _state: ProduceRolloutState,
        request: ExternalRequest,
        _weekly_node: object,
    ) -> InitialRegularOfflineBranchExpansion:
        outcome = RewardOffersOutcome(
            request_id=request.request_id,
            branch=ChanceBranch("server:reward-w1", probability=1.0, rng_token="rng:reward-w1"),
            offers=(
                RewardOffer(
                    "offer:plan1-w1",
                    "p_card-plan1-reward",
                    0,
                    "caller:plan1:w1:reward-guid",
                ),
            ),
            remaining_exclude_count=0,
        )
        captured_rewards.append(outcome)
        return InitialRegularOfflineBranchExpansion.resolved(
            InitialRegularWeightedRewardBranch(Fraction(1), outcome)
        )

    def mid1_reward_provider(
        _state: ProduceRolloutState,
        request: ExternalRequest,
        _weekly_node: object,
    ) -> InitialRegularOfflineBranchExpansion:
        outcome = RewardOffersOutcome(
            request_id=request.request_id,
            branch=ChanceBranch(
                "server:reward-mid1", probability=1.0, rng_token="rng:reward-mid1"
            ),
            offers=(
                RewardOffer(
                    "offer:plan1-mid1",
                    "p_card-plan1-mid1-reward",
                    0,
                    "caller:plan1:mid1:reward-guid",
                ),
            ),
            remaining_exclude_count=0,
        )
        captured_mid1_rewards.append(outcome)
        return InitialRegularOfflineBranchExpansion.resolved(
            InitialRegularWeightedRewardBranch(Fraction(1), outcome)
        )

    def late_lesson_inner_provider(
        state: ProduceRolloutState,
        request: ExternalRequest,
        _weekly_node: object,
    ) -> InitialRegularOfflineBranchExpansion:
        """Build the exact W9 common request; the Plan1 adapter runs native."""

        try:
            inner_request = build_initial_regular_lesson_stage_request(
                state,
                request,
                InitialRegularPlan1LessonStageFact(
                    runtime_context=InitialRegularPlan1LessonRuntimeContext(
                        battle_bonus_permille=(
                            PLAN1_FIXED_SYNTHETIC_BATTLE_BONUS_PERMILLE
                        )
                    )
                ),
                branch=ChanceBranch(
                    "server:plan1-w9-lesson",
                    probability=1.0,
                    rng_token="rng:plan1-w9-lesson",
                ),
                idol_card_id="i_card-fktn-3-001",
                rng_before=PLAN1_FIXED_SYNTHETIC_RNG_BEFORE,
            )
        except Exception as error:
            return InitialRegularOfflineBranchExpansion.paused(
                OuterSearchIssue(
                    "plan1-w9-inner-request-failed",
                    "branch_registry.plan1",
                    f"{type(error).__name__}:{error}",
                )
            )
        captured_w9_requests.append(inner_request)
        return InitialRegularOfflineBranchExpansion.resolved(
            InitialRegularWeightedInnerBranch(Fraction(1), inner_request)
        )

    def late_lesson_offers_provider(
        _state: ProduceRolloutState,
        request: ExternalRequest,
        _weekly_node: object,
    ) -> InitialRegularOfflineBranchExpansion:
        outcome = RewardOffersOutcome(
            request_id=request.request_id,
            branch=ChanceBranch(
                "server:reward-plan1-w9",
                probability=1.0,
                rng_token="rng:reward-plan1-w9",
            ),
            offers=(
                RewardOffer(
                    "offer:plan1-w9",
                    "p_card-plan1-w9-reward",
                    0,
                    "caller:plan1:w9:reward-guid",
                ),
            ),
            remaining_exclude_count=0,
        )
        captured_post_mid1_rewards.append(outcome)
        return InitialRegularOfflineBranchExpansion.resolved(
            InitialRegularWeightedRewardBranch(Fraction(1), outcome)
        )

    def final_reward_provider(
        _state: ProduceRolloutState,
        request: ExternalRequest,
        _weekly_node: object,
    ) -> InitialRegularOfflineBranchExpansion:
        outcome = RewardOffersOutcome(
            request_id=request.request_id,
            branch=ChanceBranch(
                "server:reward-plan1-final",
                probability=1.0,
                rng_token="rng:reward-plan1-final",
            ),
            offers=(
                RewardOffer(
                    "offer:plan1-final",
                    "p_card-plan1-final-reward",
                    0,
                    "caller:plan1:final:reward-guid",
                ),
            ),
            remaining_exclude_count=0,
        )
        captured_final_rewards.append(outcome)
        return InitialRegularOfflineBranchExpansion.resolved(
            InitialRegularWeightedRewardBranch(Fraction(1), outcome)
        )

    audition_scenarios = {MID1: npc_scenario, FINAL: final_npc_scenario}
    audition_shape = {
        MID1: ("mid1", 16, 9, 900),
        FINAL: ("final", 18, 12, -1),
    }

    def resolve_audition_direct(
        state: ProduceRolloutState,
        request: ExternalRequest,
    ) -> ExternalChanceExpansion:
        """Resolve one explicit Mid1/Final Plan 1 terminal branch."""

        stage_type = request.stage_type
        selected_scenario = audition_scenarios.get(stage_type)
        shape = audition_shape.get(stage_type)
        if selected_scenario is None or shape is None:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue("plan1-audition-stage-unsupported", "request.stage_type")
            )
        stage_id, step_type_value, limit_turn, limit_border = shape
        branch_id = selected_scenario.branch_id
        rng_token = selected_scenario.rng_token
        assert branch_id is not None and rng_token is not None
        battle_bonus = selected_scenario.battle_bonus_permille
        gimmick_group_id = selected_scenario.gimmick_group_id
        assert battle_bonus is not None and gimmick_group_id is not None
        audition_request = InitialRegularInnerStageRequest(
            external_request_id=request.request_id,
            week=state.week,
            action_id=request.action_id,
            stage_kind=InitialRegularInnerStageKind.AUDITION,
            plan_type="plan1",
            character_id=state.character_id,
            outer_state=state,
            branch=ChanceBranch(
                branch_id, probability=1.0, rng_token=rng_token
            ),
            idol_card_id="i_card-fktn-3-001",
            stage_type=stage_type,
            stage_id=f"plan1:{stage_id}",
            setting_id="p_exam_setting-1",
            exam_type=1,
            lesson_type=None,
            step_type_value=step_type_value,
            limit_turn=limit_turn,
            extra_turn=0,
            clear_border=3,
            limit_border=limit_border,
            rng_before=PLAN1_FIXED_SYNTHETIC_RNG_BEFORE,
            attribute=None,
            is_sp=None,
            battle_bonus_permille=battle_bonus,
            gimmick_group_id=gimmick_group_id,
            turn_parameter_types=(1, 2, 3) * limit_turn,
        )
        if stage_type == MID1:
            captured_mid1_requests.append(audition_request)
        else:
            captured_final_requests.append(audition_request)
        try:
            player_scenario = scenario_for_request(audition_request)
        except Plan1RunRuntimeLoadoutError as error:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(error.code, error.field, error.detail)
            )
        player_run = run_fktn_ssr_plan1_terminal_acceptance(
            fixture=player_scenario.fixture,
            limits=player_scenario.limits,
            hand_add_support_resolver=(
                player_scenario.hand_add_support_resolver
            ),
        )
        if not player_run.completed:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(
                    "plan1-audition-player-terminal-blocked",
                    "plan1.terminal",
                    ",".join(blocker.code for blocker in player_run.blockers),
                )
            )
        catalog_result = load_initial_regular_plan2_audition_npc_catalog(
            "i_card-fktn-3-001",
            stage_type=stage_type,
            number=1,
            master_dir=master_dir,
        )
        if not catalog_result.ready:
            return ExternalChanceExpansion.paused(
                *(
                    OuterSearchIssue(f"plan1-catalog:{issue.code}", issue.field, issue.detail)
                    for issue in catalog_result.issues
                )
            )
        resolved = resolve_initial_regular_plan1_audition(
            audition_request,
            player_score=player_run.score,
            scenario=selected_scenario,
            catalog=catalog_result.catalog,
            master_dir=master_dir,
            load_catalog=False,
        )
        if not resolved.ready:
            return ExternalChanceExpansion.paused(
                *(
                    OuterSearchIssue(issue.code, issue.field, issue.detail)
                    for issue in resolved.issues
                )
            )
        assert resolved.resolution is not None
        resolution = resolved.resolution
        if stage_type == MID1:
            captured_mid1.append(resolution)
        else:
            captured_final.append(resolution)
        outcome = InnerExamOutcome(
            request_id=request.request_id,
            branch=audition_request.branch,
            cleared=resolution.rank <= audition_request.clear_border,
            terminal=stage_type == FINAL,
            patch=ProduceStatePatch(stamina=player_run.stamina),
            reward_requested=resolution.reward_requested,
        )
        if stage_type == MID1:
            captured_mid1_outcomes.append(outcome)
        else:
            captured_final_outcomes.append(outcome)
        return ExternalChanceExpansion.resolved(
            WeightedExternalOutcome(Fraction(1), outcome)
        )

    adapter = InitialRegularPlan1InnerAdapter(
        scenario_provider=scenario_for_request
    )
    registry = InitialRegularOfflineBranchRegistry(
        (
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.WEEKLY_ACTION_OUTCOME,
                    LESSON,
                    None,
                    None,
                    1,
                ),
                w1_inner_provider,
            ),
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.REWARD_OFFERS,
                    LESSON,
                    None,
                    None,
                    1,
                ),
                w1_reward_provider,
            ),
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.REWARD_OFFERS,
                    MID1,
                    None,
                    None,
                    6,
                ),
                mid1_reward_provider,
            ),
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.REWARD_OFFERS,
                    FINAL,
                    None,
                    None,
                    13,
                ),
                final_reward_provider,
            ),
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.WEEKLY_ACTION_OUTCOME,
                    LESSON,
                    None,
                    None,
                    9,
                ),
                late_lesson_inner_provider,
            ),
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.REWARD_OFFERS,
                    LESSON,
                    None,
                    None,
                    9,
                ),
                late_lesson_offers_provider,
            ),
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.INNER_EXAM_OUTCOME,
                    MID1,
                    MID1,
                    kernel.inner_exam_adapter_ref,
                    6,
                ),
                _mid1_blocked_provider,
            ),
            # Non-milestone weeks use explicit server-owned no-reward rows;
            # week 9's lesson route above overrides this reusable row.
            *tuple(
                InitialRegularBranchRoute(
                    InitialRegularBranchRouteKey(
                        ExternalKind.WEEKLY_ACTION_OUTCOME,
                        action_id,
                        None,
                        None,
                        None,
                    ),
                    _generic_weekly_provider,
                )
                for action_id in ("class", LESSON, "cram_lesson", "supply", "outing")
            ),
        )
    )
    inner_registry = InitialRegularInnerStageAdapterRegistry({"plan1": adapter})
    offline_chance_provider = InitialRegularOfflineChanceAdapter(
        registry,
        inner_registry=inner_registry,
        master_dir=master_dir,
    )

    def chance_provider(
        state: ProduceRolloutState,
        request: ExternalRequest,
    ) -> ExternalChanceExpansion:
        if request.kind is ExternalKind.INNER_EXAM_OUTCOME:
            return resolve_audition_direct(state, request)
        return offline_chance_provider(state, request)

    def policy(state: ProduceRolloutState, legal: tuple[ProduceAction, ...]) -> ProduceAction | None:
        if state.phase is RolloutPhase.READY_FOR_REWARD:
            return next(
                (action for action in legal if action.kind is ActionKind.SELECT_REWARD),
                None,
            )
        if (stop_after_week == 7 and state.week >= 7) or state.week > stop_after_week:
            return None
        desired = {
            1: LESSON,
            2: "class",
            3: LESSON,
            4: "supply",
            5: "cram_lesson",
            7: "supply",
            8: "class",
            9: LESSON,
            10: LESSON,
            11: "supply",
            12: "cram_lesson",
        }.get(state.week)
        if desired is None and state.week == 6:
            desired_action = ProduceAction.delegate_exam(MID1)
            return desired_action if desired_action in legal else None
        if desired is None and state.week == 13:
            desired_action = ProduceAction.delegate_exam(FINAL)
            return desired_action if desired_action in legal else None
        return next(
            (action for action in legal if action.choice_id == desired),
            legal[0] if legal else None,
        )

    run = run_initial_regular_fixed_scenario(
        kernel,
        initial_state,
        policy=policy,
        chance_provider=chance_provider,
        max_transitions=64,
    )
    if run_runtime_loadout is not None:
        postflight = run_runtime_loadout.preflight_issues(
            used_request_ids=tuple(used_runtime_loadout_request_ids)
        )
        if postflight:
            run = replace(
                run,
                stop=InitialRegularFixedRunStop.EXTERNAL_BLOCKED,
                issues=(
                    *run.issues,
                    *(
                        OuterSearchIssue(issue.code, issue.field, issue.detail)
                        for issue in postflight
                    ),
                ),
            )
    return build_result(run)


def run_fktn_plan1_w1_to_final_fixed_scenario(
    *,
    initial_state: ProduceRolloutState | None = None,
    kernel: ProduceRolloutKernel | None = None,
    calendar: RouteCalendar | None = None,
    fixture: Plan1TerminalFixture | None = None,
    run_runtime_loadout: Plan1RunRuntimeLoadoutBundle | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularPlan1W1Mid1Result:
    """Run the same W1/Mid1 assembly through Final and terminal reward.

    This is a deterministic server-smoke continuation, not a general
    thirteen-week policy or an NPC-randomness replay.  The caller-owned Final
    scenario records NPC scores, battle bonus, gimmick group, and rank reward
    rule explicitly.
    """

    return run_fktn_plan1_w1_to_mid1_fixed_scenario(
        initial_state=initial_state,
        kernel=kernel,
        calendar=calendar,
        fixture=fixture,
        run_runtime_loadout=run_runtime_loadout,
        master_dir=master_dir,
        stop_after_week=13,
    )


__all__ = [
    "PLAN1_FIXED_SYNTHETIC_ATTRIBUTES",
    "PLAN1_FIXED_SYNTHETIC_BATTLE_BONUS_PERMILLE",
    "PLAN1_FIXED_SYNTHETIC_INITIAL_SCORE",
    "PLAN1_FIXED_SYNTHETIC_INITIAL_STAMINA",
    "PLAN1_FIXED_SYNTHETIC_LESSON_MULTIPLE_PERMILLE",
    "PLAN1_FIXED_SYNTHETIC_MAX_STAMINA",
    "PLAN1_FIXED_SYNTHETIC_RNG_BEFORE",
    "PLAN1_W1_MID1_SCENARIO_LABEL",
    "PLAN1_W1_FINAL_SCENARIO_LABEL",
    "InitialRegularPlan1W1Mid1Result",
    "build_fktn_plan1_fixed_smoke_stage_bootstrap",
    "run_fktn_plan1_w1_to_final_fixed_scenario",
    "run_fktn_plan1_w1_to_mid1_fixed_scenario",
]
