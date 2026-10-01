"""Deterministic server-smoke scenario for the FKTN Initial Plan2 route.

This is a small, explicit acceptance fixture copied from the green whole-run
test.  It is a deterministic server smoke, not a random-distribution model
and not an optimal-policy claim.  Every reward card, NPC minimum score,
audition seed, event branch and lesson branch is fixed in this module.  The
existing Master loaders and pure adapters do the actual data work; this
wrapper does not read a LocalSave, run MAA, or add a solver framework.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Mapping

from .audition_native_ordered_zones import (
    NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
    NativeOrderedZoneEvidenceBinding,
)
from .audition_rules import FINAL, MID1
from .initial_regular_branch_registry import (
    InitialRegularBranchRoute,
    InitialRegularBranchRouteKey,
    InitialRegularOfflineBranchRegistry,
)
from .initial_regular_event_frontier import (
    InitialRegularEventScenarioFrontier,
    InitialRegularWeightedEventScenario,
    build_initial_regular_event_scenario_frontier,
)
from .initial_regular_event_lesson_catalog import (
    INITIAL_REGULAR_EVENT_LESSON_ADAPTER_REF,
    InitialRegularEventLessonRuntimeFacts,
    load_initial_regular_event_lesson_catalog,
)
from .initial_regular_event_lesson_provider import (
    InitialRegularEventLessonScenarioInput,
    build_initial_regular_event_lesson_offline_branch,
)
from .initial_regular_event_scenario import (
    EffectiveDirectCosts,
    InitialRegularEventScenario,
    InitialRegularPostState,
    NextStepRef,
    ProduceCardSnapshot,
    UserProduceProgressPresent,
    UserProduceProgressPresentReward,
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
from .initial_regular_inner_protocol import InitialRegularInnerStageAdapterRegistry
from .initial_regular_lesson_branch_catalog import (
    InitialRegularInnerLessonBranchExecution,
    InitialRegularLessonRuntimeContext,
    build_initial_regular_lesson_branch_catalog,
)
from .initial_regular_lesson_provider import (
    InitialRegularLessonScenarioInput,
    build_initial_regular_lesson_offline_branches,
)
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularOfflineChanceAdapter,
    InitialRegularWeightedRewardBranch,
)
from .initial_regular_outer_advisor import load_initial_regular_mode_rules
from .initial_regular_plan2_audition_provider import (
    InitialRegularPlan2AuditionRuntimeBonusFactors,
    InitialRegularPlan2AuditionScenarioProvider,
)
from .initial_regular_plan2_audition_runtime import (
    InitialRegularPlan2AuditionRewardRule,
    InitialRegularPlan2AuditionRuntimeResult,
    InitialRegularPlan2AuditionScenario,
    InitialRegularPlan2NpcScenarioAuthority,
    InitialRegularPlan2NpcTerminalScore,
    load_initial_regular_plan2_audition_npc_catalog,
)
from .initial_regular_plan2_gimmick_runtime import InitialRegularPlan2MasterGimmickProvider
from .initial_regular_plan2_inner_adapter import InitialRegularPlan2InnerAdapter
from .initial_regular_plan2_master_runtime import InitialRegularPlan2MasterRuntimeProvider
from .initial_regular_reward_scenario import (
    PRODUCE_CARD_RESOURCE_TYPE,
    PRODUCE_DISPLAY_TYPE_CHOICE,
    InitialRegularRewardOfferSource,
    InitialRegularRewardOffersScenario,
    InitialRegularRewardProgressScenario,
    InitialRegularRewardScenarioFrontier,
    InitialRegularRewardScenarioProvider,
    InitialRegularWeightedRewardScenario,
)
from .initial_regular_rollout import new_initial_regular_state
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxLimits,
)
from .plan2_native_horizon import load_master_plan2_initial_deck
from .plan2_native_program_catalog import compile_plan2_native_program_catalog
from .plan2_native_stage_bootstrap import Plan2NativeStageAuthority
from .produce_rollout import ActionKind, DeckEntry, ExternalKind, Lifecycle, RolloutPhase
from .produce_rollout_expectimax import OuterSearchIssue
from .route_calendar import CLASS, CRAM_LESSON


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MASTER_DIR = PROJECT_ROOT / "_research" / "gakumasu-diff"
DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"
IDOL_CARD_ID = "i_card-fktn-3-007"
AGGRESSIVE_IDOL_CARD_ID = "i_card-fktn-3-002"
SCENARIO_LABEL = "fktn-plan2-deterministic-server-smoke"
AGGRESSIVE_SCENARIO_LABEL = "fktn-plan2-aggressive-deterministic-server-smoke"
DEFAULT_LIMITS = Plan2NativeExpectimaxLimits(
    max_depth=1,
    beam_width=8,
    max_nodes=400,
)
DEFAULT_WEIGHTS = Plan2NativeEvaluationWeights()
AGGRESSIVE_WEIGHTS = Plan2NativeEvaluationWeights(
    review_per_turn=0.55,
    aggressive_per_turn=0.55,
    block_per_turn=0.80,
    stamina_per_turn=0.08,
)
ZERO_DIGEST = "0" * 64
DETAIL = "event-detail-school-001-fktn-022"
SUGGESTION = "p_s_e_s-event-detail-school-001-fktn-022-01"
NEXT_STEP = "exam_n-002_school-event_fktn_vo"
NEXT_TYPE = "ProduceStepType_LessonVocalNormal"
REWARD_CARDS = {
    2: "p_card-02-men-1_032",
    5: "p_card-02-act-0_009",
    6: "p_card-02-act-0_010",
    12: "p_card-02-act-0_032",
    13: "p_card-02-men-0_011",
}
AGGRESSIVE_REWARD_CARDS = {
    2: "p_card-02-men-1_009",
    5: "p_card-02-act-1_004",
    6: "p_card-02-men-1_006",
    12: "p_card-02-act-2_047",
    13: "p_card-02-men-2_008",
}
AUDITION_SEEDS = {MID1: 0x6001, FINAL: 0x13001}
AGGRESSIVE_AUDITION_SEEDS = {MID1: 0x6001, FINAL: 0x13003}

AUTHORITY_DESCRIPTION = (
    "PC Master starter manifest plus deterministic caller-owned starter GUIDs; "
    "fixed caller response reward receive GUID overlays for five rewards; "
    "explicit NPC minimums/RNG/effects; not authentic LocalSave"
)


def _caller_owned_starter_guid(
    *,
    ordinal: int,
    card_id: str,
    upgrade: int,
) -> str:
    """Create an ordered deterministic GUID without implying LocalSave data."""

    return f"caller:plan2:starter:{ordinal:04d}:{card_id}@{upgrade}"


def _caller_owned_reward_guid(*, week: int, card_id: str) -> str:
    return f"caller:plan2:w{week}:reward:{card_id}"


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2FixedScenarioResult:
    """Small result envelope for the deterministic server-smoke fixture."""

    run: InitialRegularFixedRunResult
    report: InitialRegularFixedRunReport
    audition_results: tuple[InitialRegularPlan2AuditionRuntimeResult, ...]
    scenario_label: str

    def __post_init__(self) -> None:
        if not isinstance(self.run, InitialRegularFixedRunResult):
            raise TypeError("run must be InitialRegularFixedRunResult")
        if not isinstance(self.report, InitialRegularFixedRunReport):
            raise TypeError("report must be InitialRegularFixedRunReport")
        if any(
            not isinstance(value, InitialRegularPlan2AuditionRuntimeResult)
            for value in self.audition_results
        ):
            raise TypeError("audition_results must contain runtime results")
        if not self.scenario_label:
            raise ValueError("scenario_label is required")

    @property
    def authority_description(self) -> str:
        """Explain caller-owned identities without claiming an authentic save."""

        return AUTHORITY_DESCRIPTION


# The longer name is explicit in logs; keep a concise alias for callers.
InitialRegularPlan2FixedScenarioSmokeResult = InitialRegularPlan2FixedScenarioResult


def _binding() -> NativeOrderedZoneEvidenceBinding:
    return NativeOrderedZoneEvidenceBinding(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        local_save_schema_version=5,
        run_id="plan2-deterministic-smoke",
        step_context_id="plan2-deterministic-smoke-context",
        step_context_digest=ZERO_DIGEST,
        session_transition_id="plan2-deterministic-smoke-transition",
        zone_checkpoint_digest=ZERO_DIGEST,
        local_save_source_sha256=ZERO_DIGEST,
        local_save_evidence_digest=ZERO_DIGEST,
        runtime_evidence_digest=ZERO_DIGEST,
    )


def _caller_bound_initial_deck(manifest) -> tuple[DeckEntry, ...]:
    """Retain Master stack order while attaching caller-owned GUIDs.

    The ordinal follows the same flattened order used by the deterministic
    Plan2 stage bootstrap.  Therefore native tie-breaking sees the identical
    card order; only the already-known identity is persisted in outer state.
    """

    counts = Counter(manifest.card_refs)
    deck: list[DeckEntry] = []
    ordinal = 0
    for (card_id, upgrade), count in counts.items():
        instance_ids = tuple(
            _caller_owned_starter_guid(
                ordinal=ordinal + index,
                card_id=card_id,
                upgrade=upgrade,
            )
            for index in range(count)
        )
        deck.append(DeckEntry(card_id, upgrade, count, instance_ids))
        ordinal += count
    return tuple(deck)


def _event_scenario(state) -> InitialRegularEventScenario:
    cards = tuple(
        ProduceCardSnapshot(entry.card_id, entry.upgrade)
        for entry in state.deck
        for _ in range(entry.count)
    )
    return InitialRegularEventScenario(
        detail_id=DETAIL,
        suggestion_id=SUGGESTION,
        suggestion_index=0,
        success=True,
        effective_direct_costs=EffectiveDirectCosts(0, 0),
        effect_results=(),
        post_state=InitialRegularPostState(
            max_stamina=state.max_stamina,
            stamina=state.stamina,
            produce_point=state.produce_points,
            vote_count=0,
            star=0,
            vocal=state.attributes.vocal,
            dance=state.attributes.dance,
            visual=state.attributes.visual,
            produce_cards=cards,
            high_score_gold=0,
        ),
        next_step=NextStepRef(NEXT_TYPE, NEXT_STEP),
    )


def _audition_scenario(stage_type: str, catalog, *, seed: int) -> InitialRegularPlan2AuditionScenario:
    return InitialRegularPlan2AuditionScenario(
        stage_type=stage_type,
        number=1,
        npc_group_id=catalog.npc_group_id,
        authority=InitialRegularPlan2NpcScenarioAuthority.SERVER_RUNTIME,
        probability=Fraction(1),
        rng_before=seed,
        turn_parameter_types=tuple(
            (index % 3) + 1 for index in range(catalog.limit_turn)
        ),
        battle_bonus_permille=None,
        extra_turn=0,
        is_static_npc_score=catalog.is_static_npc_score,
        npc_score_multiple_permille=1000,
        npc_scores=tuple(
            InitialRegularPlan2NpcTerminalScore(
                catalog.npc_group_id,
                npc.number,
                npc.score_min,
            )
            for npc in catalog.npcs
        ),
        reward_rule=InitialRegularPlan2AuditionRewardRule(
            stage_type,
            1,
            tuple(
                rank <= catalog.clear_border
                for rank in range(1, catalog.field_size + 1)
            ),
        ),
        rng_token=f"rng:{stage_type}:deterministic-smoke",
        branch_id=f"{stage_type}:deterministic-smoke",
    )


def run_fktn_plan2_deterministic_smoke(
    *,
    idol_card_id: str = IDOL_CARD_ID,
    reward_cards: Mapping[int, str] | None = None,
    scenario_label: str | None = None,
    lesson_failure_reward_requested: bool | None = None,
    weights: Plan2NativeEvaluationWeights = DEFAULT_WEIGHTS,
    limits: Plan2NativeExpectimaxLimits = DEFAULT_LIMITS,
) -> InitialRegularPlan2FixedScenarioResult:
    """Run the fixed FKTN Plan2 deterministic server-smoke scenario.

    This is a bounded fixture with explicit one-branch rewards and NPC
    minimum scores.  It is neither a random distribution nor an optimal
    policy, and it does not claim authentic LocalSave evidence.
    """

    if not isinstance(limits, Plan2NativeExpectimaxLimits):
        raise TypeError("limits must be Plan2NativeExpectimaxLimits")
    if not isinstance(weights, Plan2NativeEvaluationWeights):
        raise TypeError("weights must be Plan2NativeEvaluationWeights")
    if (
        lesson_failure_reward_requested is not None
        and not isinstance(lesson_failure_reward_requested, bool)
    ):
        raise TypeError(
            "lesson_failure_reward_requested must be boolean or None"
        )
    if not isinstance(idol_card_id, str) or not idol_card_id:
        raise ValueError("idol_card_id is required")
    profile_defaults = {
        IDOL_CARD_ID: (SCENARIO_LABEL, REWARD_CARDS, AUDITION_SEEDS),
        AGGRESSIVE_IDOL_CARD_ID: (
            AGGRESSIVE_SCENARIO_LABEL,
            AGGRESSIVE_REWARD_CARDS,
            AGGRESSIVE_AUDITION_SEEDS,
        ),
    }
    default_label, default_rewards, selected_audition_seeds = profile_defaults.get(
        idol_card_id,
        (
            f"{idol_card_id}-plan2-deterministic-server-smoke",
            None,
            AUDITION_SEEDS,
        ),
    )
    if reward_cards is None and default_rewards is None:
        raise ValueError(
            "an unknown Plan2 idol card requires explicit response reward_cards"
        )
    if reward_cards is not None and not isinstance(reward_cards, Mapping):
        raise TypeError("reward_cards must be a mapping")
    selected_rewards = dict(
        default_rewards if reward_cards is None else reward_cards
    )
    if set(selected_rewards) != {2, 5, 6, 12, 13} or any(
        not isinstance(value, str) or not value
        for value in selected_rewards.values()
    ):
        raise ValueError("reward_cards must provide exact card IDs for weeks 2/5/6/12/13")
    selected_label = default_label if scenario_label is None else scenario_label
    if not isinstance(selected_label, str) or not selected_label:
        raise ValueError("scenario_label is required")

    manifest = load_master_plan2_initial_deck(idol_card_id, master_dir=MASTER_DIR)
    deck = _caller_bound_initial_deck(manifest)
    kernel, initial = new_initial_regular_state(
        character_id="fktn",
        stamina=42,
        max_stamina=42,
        vocal=321,
        dance=456,
        visual=654,
        deck=deck,
        week=1,
        item_session_refs=manifest.item_ids,
        master_dir=MASTER_DIR,
    )
    mode_rules = load_initial_regular_mode_rules(master_dir=MASTER_DIR)
    compilation = compile_plan2_native_program_catalog(database=DATABASE)
    if not compilation.fully_compiled:
        raise RuntimeError("Plan2 smoke program catalog is not fully compiled")
    event_lesson_catalog = load_initial_regular_event_lesson_catalog(
        master_dir=MASTER_DIR,
        database=DATABASE,
    )
    if not event_lesson_catalog.available:
        raise RuntimeError("Plan2 smoke event lesson catalog is unavailable")

    audition_providers = {}
    for stage_type, seed in selected_audition_seeds.items():
        loaded = load_initial_regular_plan2_audition_npc_catalog(
            idol_card_id,
            stage_type=stage_type,
            number=1,
            master_dir=MASTER_DIR,
        )
        if not loaded.ready or loaded.catalog is None:
            raise RuntimeError(f"Plan2 smoke audition catalog unavailable: {stage_type}")
        audition_providers[stage_type] = InitialRegularPlan2AuditionScenarioProvider(
            idol_card_id=idol_card_id,
            scenario=_audition_scenario(stage_type, loaded.catalog, seed=seed),
            catalog=loaded.catalog,
            bonus_factors=InitialRegularPlan2AuditionRuntimeBonusFactors(0, 0, 0),
            database=DATABASE,
            master_dir=MASTER_DIR,
        )

    audition_results: list[InitialRegularPlan2AuditionRuntimeResult] = []

    def resolve_audition(context):
        result = audition_providers[context.request.stage_type].resolve_terminal(context)
        audition_results.append(result)
        return result.resolution

    adapter = InitialRegularPlan2InnerAdapter(
        catalog_provider=lambda _request: compilation.catalog,
        binding_provider=lambda _request: _binding(),
        runtime_provider=InitialRegularPlan2MasterRuntimeProvider(),
        gimmick_provider=InitialRegularPlan2MasterGimmickProvider(database=DATABASE),
        attribute_cap_provider=lambda _request: mode_rules.attribute_cap,
        audition_resolution_provider=resolve_audition,
        authority=Plan2NativeStageAuthority.DETERMINISTIC_FIXTURE,
        lesson_reward_requested=True,
        lesson_failure_reward_requested=lesson_failure_reward_requested,
        weights=weights,
        limits=limits,
        max_actions=80,
    )
    lesson_runtime = InitialRegularLessonRuntimeContext(
        lesson_limit_up_score=0,
        exam_extra_turn=0,
        battle_bonus_permille=(1000, 1000, 1000),
        gimmick_group_id="",
    )

    def hard_lesson_provider(seed: int):
        def provider(state, request, _node):
            catalog = build_initial_regular_lesson_branch_catalog(
                state,
                request,
                idol_card_id=idol_card_id,
                runtime_context=lesson_runtime,
                master_dir=MASTER_DIR,
            )
            if catalog.issues:
                raise RuntimeError(catalog.issues)
            stage = catalog.for_attribute("vocal").frontier[0].stage
            return build_initial_regular_lesson_offline_branches(
                state,
                request,
                InitialRegularLessonScenarioInput(
                    attribute="vocal",
                    idol_card_id=idol_card_id,
                    runtime_context=lesson_runtime,
                    branches=(
                        InitialRegularInnerLessonBranchExecution(
                            stage.branch_key,
                            seed,
                            f"rng:{request.week}:vocal-hard",
                        ),
                    ),
                ),
                master_dir=MASTER_DIR,
            )

        return provider

    event_lesson_input = InitialRegularEventLessonScenarioInput(
        probability=Fraction(1),
        branch_id="event-022:vocal-lesson",
        rng_before=0x12345678,
        idol_card_id=idol_card_id,
        runtime_facts=InitialRegularEventLessonRuntimeFacts(
            current_progress_step_id=None,
            parameter_limits=(mode_rules.attribute_cap,) * 3,
            lesson_limit_up_score=0,
            exam_extra_turn=0,
            battle_bonus_permille=(1000, 1000, 1000),
            gimmick_group_id="",
        ),
        rng_token="rng:event-022:vocal-lesson",
    )

    def event_provider(state, request, _node):
        return build_initial_regular_event_scenario_frontier(
            state,
            request,
            InitialRegularEventScenarioFrontier(
                (
                    InitialRegularWeightedEventScenario(
                        Fraction(1),
                        "event-022:vocal",
                        _event_scenario(state),
                        "rng:event-022:vocal",
                    ),
                ),
                complete=True,
            ),
            attribute_cap=mode_rules.attribute_cap,
            database=DATABASE,
        )

    def event_lesson_provider(state, request, _node):
        return build_initial_regular_event_lesson_offline_branch(
            state,
            request,
            event_lesson_input,
            catalog=event_lesson_catalog,
            master_dir=MASTER_DIR,
            database=DATABASE,
        )

    def reward_source(_state, request, _node):
        card_id = selected_rewards[request.week]
        source = (
            InitialRegularRewardOfferSource.AUDITION_END
            if request.week in {6, 13}
            else InitialRegularRewardOfferSource.LESSON_END
        )
        progress = InitialRegularRewardProgressScenario(
            source=source,
            present=UserProduceProgressPresent(
                position_number=request.week,
                received=False,
                display_type=PRODUCE_DISPLAY_TYPE_CHOICE,
                reward_count=1,
                pick_count=1,
                rewards=(
                    UserProduceProgressPresentReward(
                        resource_type=PRODUCE_CARD_RESOURCE_TYPE,
                        resource_id=card_id,
                        resource_level=0,
                        quantity=1,
                    ),
                ),
            ),
        )
        return InitialRegularRewardScenarioFrontier(
            (
                InitialRegularWeightedRewardScenario(
                    Fraction(1),
                    f"reward:w{request.week}:{card_id}",
                    InitialRegularRewardOffersScenario(progress),
                    rng_token=f"rng:reward:w{request.week}",
                ),
            ),
            complete=True,
        )

    frontier_reward_provider = InitialRegularRewardScenarioProvider(
        reward_source,
        database=DATABASE,
    )

    def reward_provider(state, request, weekly_node):
        """Overlay explicit receive IDs after generic reward validation."""

        expansion = frontier_reward_provider(state, request, weekly_node)
        if expansion.issues or request.kind is not ExternalKind.REWARD_OFFERS:
            return expansion
        updated = []
        for branch in expansion.branches:
            if not isinstance(branch, InitialRegularWeightedRewardBranch):
                return InitialRegularOfflineBranchExpansion.paused(
                    OuterSearchIssue(
                        "plan2-reward-guid-overlay-branch-kind",
                        "reward_provider.branch",
                        type(branch).__name__,
                    )
                )
            offers = tuple(
                replace(
                    offer,
                    received_card_instance_id=(
                        _caller_owned_reward_guid(
                            week=request.week,
                            card_id=offer.card_id,
                        )
                        if offer.card_id == selected_rewards.get(request.week)
                        else offer.received_card_instance_id
                    ),
                )
                for offer in branch.outcome.offers
            )
            updated.append(
                replace(branch, outcome=replace(branch.outcome, offers=offers))
            )
        return InitialRegularOfflineBranchExpansion.resolved(*updated)
    routes = [
        InitialRegularBranchRoute(
            InitialRegularBranchRouteKey(
                ExternalKind.WEEKLY_ACTION_OUTCOME, CLASS, None, None, 2
            ),
            event_provider,
        ),
        InitialRegularBranchRoute(
            InitialRegularBranchRouteKey(
                ExternalKind.WEEKLY_ACTION_OUTCOME,
                NEXT_STEP,
                NEXT_TYPE,
                INITIAL_REGULAR_EVENT_LESSON_ADAPTER_REF,
                2,
            ),
            event_lesson_provider,
        ),
        InitialRegularBranchRoute(
            InitialRegularBranchRouteKey(
                ExternalKind.WEEKLY_ACTION_OUTCOME, CRAM_LESSON, None, None, 5
            ),
            hard_lesson_provider(0x5005),
        ),
        InitialRegularBranchRoute(
            InitialRegularBranchRouteKey(
                ExternalKind.WEEKLY_ACTION_OUTCOME, CRAM_LESSON, None, None, 12
            ),
            hard_lesson_provider(0x12012),
        ),
    ]
    for stage_type in (MID1, FINAL):
        routes.append(
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.INNER_EXAM_OUTCOME,
                    stage_type,
                    stage_type,
                    kernel.inner_exam_adapter_ref,
                    6 if stage_type == MID1 else 13,
                ),
                audition_providers[stage_type],
            )
        )
    for week, action_id in {
        2: NEXT_STEP,
        5: CRAM_LESSON,
        6: MID1,
        12: CRAM_LESSON,
        13: FINAL,
    }.items():
        routes.append(
            InitialRegularBranchRoute(
                InitialRegularBranchRouteKey(
                    ExternalKind.REWARD_OFFERS, action_id, None, None, week
                ),
                reward_provider,
            )
        )

    chance_provider = InitialRegularOfflineChanceAdapter(
        InitialRegularOfflineBranchRegistry(routes),
        inner_registry=InitialRegularInnerStageAdapterRegistry({"plan2": adapter}),
        database=DATABASE,
        master_dir=MASTER_DIR,
    )

    def policy(state, legal):
        if state.phase is RolloutPhase.READY_FOR_REWARD:
            return next(action for action in legal if action.kind is ActionKind.SELECT_REWARD)
        desired = {2: CLASS, 5: CRAM_LESSON, 12: CRAM_LESSON}.get(
            state.week, "rest"
        )
        return next(
            (action for action in legal if action.choice_id == desired),
            legal[0] if legal else None,
        )

    run = run_initial_regular_fixed_scenario(
        kernel,
        initial,
        policy=policy,
        chance_provider=chance_provider,
        max_transitions=80,
    )
    if run.stop is not InitialRegularFixedRunStop.TERMINAL or not run.completed:
        raise RuntimeError(f"deterministic smoke did not reach terminal: {run.issues}")
    if len(audition_results) != 2 or any(
        result.resolution is None
        or result.catalog is None
        or result.resolution.rank > result.catalog.clear_border
        for result in audition_results
    ):
        raise RuntimeError("deterministic smoke audition acceptance failed")
    report = build_initial_regular_fixed_run_report(run)
    return InitialRegularPlan2FixedScenarioResult(
        run=run,
        report=report,
        audition_results=tuple(audition_results),
        scenario_label=selected_label,
    )


def run_fktn_plan2_aggressive_deterministic_smoke(
    *,
    weights: Plan2NativeEvaluationWeights = AGGRESSIVE_WEIGHTS,
    limits: Plan2NativeExpectimaxLimits = DEFAULT_LIMITS,
) -> InitialRegularPlan2FixedScenarioResult:
    """Run FKTN ``冠菊`` with the Master aggressive/block starter deck."""

    return run_fktn_plan2_deterministic_smoke(
        idol_card_id=AGGRESSIVE_IDOL_CARD_ID,
        # The EndResponse is server-owned.  This deterministic smoke fixture
        # explicitly chooses its reward branch instead of inferring it from
        # the static lesson threshold.
        lesson_failure_reward_requested=True,
        weights=weights,
        limits=limits,
    )


__all__ = [
    "AUTHORITY_DESCRIPTION",
    "AGGRESSIVE_IDOL_CARD_ID",
    "AGGRESSIVE_REWARD_CARDS",
    "AGGRESSIVE_SCENARIO_LABEL",
    "AGGRESSIVE_AUDITION_SEEDS",
    "AGGRESSIVE_WEIGHTS",
    "AUDITION_SEEDS",
    "DEFAULT_LIMITS",
    "DEFAULT_WEIGHTS",
    "REWARD_CARDS",
    "SCENARIO_LABEL",
    "InitialRegularPlan2FixedScenarioResult",
    "InitialRegularPlan2FixedScenarioSmokeResult",
    "run_fktn_plan2_deterministic_smoke",
    "run_fktn_plan2_aggressive_deterministic_smoke",
]
