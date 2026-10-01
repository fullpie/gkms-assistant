"""Bounded deterministic Initial Regular Plan 3 W1-to-Mid1 scenario.

The outer lifecycle, calendar, lesson catalog, reward adapter, common inner
dispatch, Plan 3 stage-start bootstrap, and native search are all existing
production components.  This module only binds the facts which static Master
does not own: the final live-deck merge, unique-card upgrade, SP result, GUIDs,
deck orders, RNG roots, exact post-lesson patch, reward offers, audition turn
schedule/bonuses, and effective NPC score tracks.

Those values form a deterministic caller scenario.  In particular, the NPC
tracks and rewards are not inferred random outcomes and are not represented as
probability distributions.  The prefix API deliberately stops at the first W7
policy boundary; the full API resumes that exact state through Final terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from fractions import Fraction
from pathlib import Path

from .audition_rules import FINAL, MID1, AuditionRules, load_audition_rules
from .initial_regular_audition_branch_catalog import (
    InitialRegularAuditionScenarioBranch,
    build_initial_regular_audition_frontier,
)
from .initial_regular_branch_registry import (
    InitialRegularBranchRoute,
    InitialRegularBranchRouteKey,
    InitialRegularOfflineBranchRegistry,
)
from .initial_regular_event_scenario import (
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
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageAdapterRegistry,
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
)
from .initial_regular_lesson_branch_catalog import (
    InitialRegularInnerLessonBranchExecution,
    InitialRegularInnerLessonExecutionInputs,
    InitialRegularLessonRuntimeContext,
    InitialRegularSpRatePermils,
    build_initial_regular_lesson_branch_catalog,
    build_initial_regular_weighted_weekly_frontier,
)
from .initial_regular_offline_search import InitialRegularOfflineChanceAdapter
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedRewardBranch,
)
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
from .master_db import DEFAULT_DATABASE, get_idol_profile
from .plan3_audition_search_bridge import (
    Plan3AuditionContext,
    Plan3AuditionNpcTrack,
    Plan3AuditionTurnFrame,
)
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    LESSON_DANCE,
    LESSON_VISUAL,
    LESSON_VOCAL,
    Plan3GimmickProfile,
    load_plan3_initial_deck,
    load_plan3_gimmick_profile,
)
from .plan3_exam_start import Plan3ExamStartChance, Plan3ExamStartLoadout
from .plan3_native_search import search_plan3_native
from .plan3_rollout_inner_adapter import (
    Plan3InnerBattleResolution,
    Plan3InnerPostExamResolution,
    Plan3InnerResolutionContext,
    Plan3InnerRuntimeRefs,
    Plan3InnerSearchRequest,
)
from .produce_rollout_expectimax import OuterSearchIssue
from .initial_regular_plan3_stage_start_adapter import (
    InitialRegularPlan3StageStartScenario,
    build_initial_regular_plan3_stage_start_adapter,
)
from .produce_rollout import (
    ActionKind,
    AttributeValues,
    DeckEntry,
    ExternalKind,
    Lifecycle,
    ProduceRolloutKernel,
    ProduceRolloutState,
    ProduceStatePatch,
    RewardOffer,
    RolloutPhase,
)
from .route_calendar import LESSON


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"
MASTER_DIR = PROJECT_ROOT / "_research" / "gakumasu-diff"

IDOL_CARD_ID = "i_card-fktn-3-011"
SCENARIO_LABEL = "fktn-plan3-deterministic-w1-mid1"
FULL_SCENARIO_LABEL = "fktn-plan3-deterministic-w1-final"
LESSON_STAGE_ID = "p_step_lesson_level-001-fktn-normal-vo-001"
LESSON_RNG_BEFORE = 0x1001
LESSON_RNG_TOKEN = "caller:plan3:w1:lesson:vocal-normal"
SECOND_LESSON_STAGE_ID = "p_step_lesson_level-001-fktn-normal-vo-003"
SECOND_LESSON_RNG_BEFORE = 0x9009
SECOND_LESSON_RNG_TOKEN = "caller:plan3:w9:lesson:vocal-normal"
MID1_RNG_BEFORE = 0x6001
MID1_RNG_TOKEN = "caller:plan3:w6:mid1"
MID1_TURN_PARAMETER_TYPES = (1, 2, 3, 1, 2, 3, 1, 2, 3)
MID1_BATTLE_BONUS_PERMILLE = (1500, 1500, 1500)
FINAL_RNG_BEFORE = 0x13001
FINAL_RNG_TOKEN = "caller:plan3:w13:final"
FINAL_TURN_PARAMETER_TYPES = (1, 2, 3, 1, 2, 3, 1, 2, 3, 1, 2, 3)
FINAL_BATTLE_BONUS_PERMILLE = (4000, 4000, 4000)

# Exact effective terminal scores selected by this deterministic caller
# scenario.  They are within the real FKTN Mid1 NPC Master ranges, but their
# selection and per-turn realization remain server/runtime-owned facts.
MID1_NPC_TERMINAL_SCORES = (553, 338, 161, 118, 118)
FINAL_NPC_TERMINAL_SCORES = (2381, 1450, 476, 594, 515)
REWARD_CARDS = {
    1: "p_card-00-act-0_002",
    6: "p_card-03-act-1_038",
    9: "p_card-00-men-0_003",
    13: "p_card-03-act-0_014",
}
# These IDs model the caller-owned receive response that is intentionally not
# represented in ``UserProduceProgressPresent``.  The generic reward frontier
# still validates the raw present with ``received=False``; the overlay only
# annotates the downstream RewardOffer consumed by the outer kernel.
REWARD_INSTANCE_IDS = {
    week: f"caller:plan3:w{week}:reward:{card_id}"
    for week, card_id in REWARD_CARDS.items()
}

_LESSON_TYPES = {
    1: LESSON_VOCAL,
    2: LESSON_DANCE,
    3: LESSON_VISUAL,
}
_LESSON_POST_ATTRIBUTES = AttributeValues(371, 456, 654)
_SECOND_LESSON_POST_ATTRIBUTES = AttributeValues(421, 456, 654)
_AUTHORITY_DESCRIPTION = (
    "real Master stage/deck rows; caller-fixed live-deck merge and unique-card "
    "upgrade, SP absence, GUIDs, fixed orders, RNG roots, post-lesson patch, "
    "reward rows, fixed caller response reward receive GUID overlays, audition "
    "schedule/bonuses, and effective NPC score tracks; not authentic LocalSave"
)


def _caller_owned_guid(namespace: int, index: int) -> str:
    """Return one explicit deterministic fixture GUID, never a live claim."""

    return f"{namespace:08x}-0000-0000-0000-{index:012x}"


@dataclass(frozen=True, slots=True)
class InitialRegularPlan3W1Mid1FixedScenarioResult:
    """Result envelope for the bounded deterministic caller scenario."""

    run: InitialRegularFixedRunResult
    inner_outcomes: tuple[InitialRegularInnerStageOutcome, ...]
    audition_context: Plan3AuditionContext
    scenario_label: str
    authority_description: str

    def __post_init__(self) -> None:
        if not isinstance(self.run, InitialRegularFixedRunResult):
            raise TypeError("run must be InitialRegularFixedRunResult")
        if not self.inner_outcomes or any(
            not isinstance(value, InitialRegularInnerStageOutcome)
            for value in self.inner_outcomes
        ):
            raise TypeError("inner_outcomes must contain typed common outcomes")
        if not isinstance(self.audition_context, Plan3AuditionContext):
            raise TypeError("audition_context must be Plan3AuditionContext")
        if not self.scenario_label or not self.authority_description:
            raise ValueError("scenario identity and authority are required")

    @property
    def lesson_outcome(self) -> InitialRegularInnerStageOutcome:
        return next(
            value for value in self.inner_outcomes if value.rank is None
        )

    @property
    def audition_outcome(self) -> InitialRegularInnerStageOutcome:
        return next(
            value for value in self.inner_outcomes if value.rank is not None
        )


def _caller_bound_initial_deck() -> tuple[DeckEntry, ...]:
    """Join separate Master sources using the caller's explicit live choice."""

    mode_deck = load_plan3_initial_deck(
        produce_id="produce-001",
        database=DEFAULT_DATABASE,
    )
    profile = get_idol_profile(IDOL_CARD_ID, DEFAULT_DATABASE)
    if profile is None:
        raise RuntimeError(f"Plan3 fixed scenario IdolCard is absent: {IDOL_CARD_ID}")
    if profile.plan_type != "ProducePlanType_Plan3" or not profile.produce_card_id:
        raise RuntimeError("Plan3 fixed scenario IdolCard identity is incompatible")
    rows = (
        *(DeckEntry(value.card_id, value.upgrade) for value in mode_deck.cards),
        # Master supplies the identity, not this live upgrade selection.
        DeckEntry(profile.produce_card_id, upgrade=1),
    )
    # These are the same caller-owned identities previously supplied only at
    # the first stage bootstrap.  Keeping them on the outer deck makes card
    # identity persist across the whole fixed run and lets the GUI distinguish
    # known starter GUIDs from reward rows whose receive response is absent.
    return tuple(
        DeckEntry(
            row.card_id,
            row.upgrade,
            row.count,
            (_caller_owned_guid(0x31000001, index),),
        )
        for index, row in enumerate(rows, start=1)
    )


def _audition_context(
    rules: AuditionRules,
    *,
    step_type_value: int,
    turn_parameter_types: tuple[int, ...],
    battle_bonus_permille: tuple[int, int, int],
    npc_terminal_scores: tuple[int, ...],
) -> Plan3AuditionContext:
    frames = tuple(
        Plan3AuditionTurnFrame(
            round_number=index,
            parameter_type=parameter_type,
            lesson_type=_LESSON_TYPES[parameter_type],
            score_multiplier_permille=battle_bonus_permille[
                parameter_type - 1
            ],
        )
        for index, parameter_type in enumerate(
            turn_parameter_types,
            start=1,
        )
    )
    # Only the terminal values are authoritative for this acceptance.  The
    # effective score is placed at the last server track entry rather than
    # regenerated from an unavailable NPC RNG formula.
    tracks = tuple(
        Plan3AuditionNpcTrack(
            npc_id=f"{rules.npc_group_id}:{number}",
            number=number,
            turn_scores=(0,) * (rules.turns - 1) + (terminal_score,),
            current_score=0,
            vocal_score=0,
            dance_score=0,
            visual_score=terminal_score,
        )
        for number, terminal_score in enumerate(
            npc_terminal_scores,
            start=1,
        )
    )
    return Plan3AuditionContext(
        setting_id=rules.exam_setting_id,
        step_type_value=step_type_value,
        current_round=1,
        limit_round=rules.turns,
        clear_rank=rules.rank_threshold,
        force_end_score=rules.force_end_score,
        frames=frames,
        npcs=tracks,
        # Tracks above are already the effective server result; no unresolved
        # multiplier transition is applied by this fixture.
        npc_score_multiple_permille=0,
        is_static_npc_score=False,
        turn_end_stamina_recovery=rules.turn_end_stamina_recovery,
        extra_turn=0,
    )


def _chance(
    request: InitialRegularInnerStageRequest,
    *,
    random_state: int,
    random_root: str,
    namespace: int,
) -> Plan3ExamStartChance:
    missing = sum(
        value.count for value in request.outer_deck if not value.instance_ids
    )
    total = sum(value.count for value in request.outer_deck)
    return Plan3ExamStartChance(
        random_root=random_root,
        random_state=random_state,
        instance_guids=tuple(
            _caller_owned_guid(namespace, index)
            for index in range(1, missing + 1)
        ),
        fixed_deck_orders=(0,) * total,
    )


def _search_with_resolved_rank(
    request: Plan3InnerSearchRequest,
    *,
    gimmick_profile: Plan3GimmickProfile | None = None,
):
    """Use the native search while declaring the injected rank boundary."""

    return search_plan3_native(
        request.state,
        request.native_state,
        beam_width=request.beam_width,
        depth=None,
        settings=request.settings,
        gimmick_profile=gimmick_profile,
        battle_parameter_schedule=(
            request.runtime_refs.battle_parameter_schedule
        ),
        battle_ranking_resolved=True,
        force_end_score=request.runtime_refs.force_end_score,
        force_end_stamina_recovery=(
            request.runtime_refs.force_end_stamina_recovery
        ),
    )


class _InitialRegularPlan3FixedScenarioAdapter:
    """Small stage router over the production Plan 3 stage-start adapter."""

    def __init__(
        self,
        *,
        mid1_rules: AuditionRules,
        mid1_context: Plan3AuditionContext,
        final_rules: AuditionRules,
        final_context: Plan3AuditionContext,
    ) -> None:
        self.audition_rules = {
            MID1: mid1_rules,
            FINAL: final_rules,
        }
        self.audition_contexts = {
            MID1: mid1_context,
            FINAL: final_context,
        }
        self._final_gimmick_profile: Plan3GimmickProfile | None = None
        self.outcomes: list[InitialRegularInnerStageOutcome] = []

    @staticmethod
    def _issue(
        code: str,
        field: str,
        detail: str = "",
    ) -> tuple[InitialRegularInnerStageIssue, ...]:
        return (InitialRegularInnerStageIssue(code, field, detail),)

    def _battle_resolution(
        self,
        context: Plan3InnerResolutionContext,
    ) -> Plan3InnerBattleResolution:
        stage_type = context.external_request.stage_type
        if stage_type not in self.audition_rules:
            raise ValueError(f"deterministic NPC scenario is unbound: {stage_type}")
        rules = self.audition_rules[stage_type]
        audition_context = self.audition_contexts[stage_type]
        expected_stage_id = f"{rules.battle_config_id}:{stage_type}:1"
        actual_stage_id = context.bootstrap.inputs.context.stage_id
        if actual_stage_id != expected_stage_id:
            raise ValueError(
                "deterministic NPC scenario stage mismatch: "
                f"expected={expected_stage_id}:actual={actual_stage_id}"
            )
        rank = audition_context.player_rank(
            context.final_state.score,
            after_round=audition_context.limit_round,
        )
        return Plan3InnerBattleResolution(
            rank=rank,
            cleared=rank <= audition_context.clear_rank,
        )

    def _lesson_scenario(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularPlan3StageStartScenario | tuple[
        InitialRegularInnerStageIssue, ...
    ]:
        lesson_inputs = {
            1: (
                LESSON_STAGE_ID,
                LESSON_RNG_BEFORE,
                LESSON_RNG_TOKEN,
                0x31000001,
                _LESSON_POST_ATTRIBUTES,
            ),
            9: (
                SECOND_LESSON_STAGE_ID,
                SECOND_LESSON_RNG_BEFORE,
                SECOND_LESSON_RNG_TOKEN,
                0x39000001,
                _SECOND_LESSON_POST_ATTRIBUTES,
            ),
        }
        selected = lesson_inputs.get(request.week)
        if selected is None or request.stage_id != selected[0]:
            return self._issue(
                "plan3-fixed-lesson-stage-unbound",
                "request.stage_id",
                f"week={request.week}:stage={request.stage_id}",
            )
        stage_id, rng_before, rng_token, namespace, attributes = selected
        return InitialRegularPlan3StageStartScenario(
            expected_stage_id=stage_id,
            expected_idol_card_id=IDOL_CARD_ID,
            chance=_chance(
                request,
                random_state=rng_before,
                random_root=rng_token,
                namespace=namespace,
            ),
            loadout=Plan3ExamStartLoadout(),
            authority_description=_AUTHORITY_DESCRIPTION,
            start_turn_effects_known_absent=True,
            post_exam_resolution=Plan3InnerPostExamResolution(
                patch=ProduceStatePatch(attributes=attributes),
                reward_requested=True,
            ),
            beam_width=16,
            database=DATABASE,
            master_dir=MASTER_DIR,
        )

    def _final_gimmick(
        self,
        rules: AuditionRules,
    ) -> Plan3GimmickProfile | tuple[InitialRegularInnerStageIssue, ...]:
        if self._final_gimmick_profile is None:
            try:
                profile = load_plan3_gimmick_profile(
                    rules.gimmick_group_id,
                    master_dir=MASTER_DIR,
                    database=DATABASE,
                )
            except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
                return self._issue(
                    "plan3-fixed-final-gimmick-unresolved",
                    "final.gimmick_group_id",
                    f"{type(error).__name__}:{error}",
                )
            if any(value.start_turn <= 1 for value in profile.steps):
                return self._issue(
                    "plan3-fixed-final-start-gimmick-unresolved",
                    "final.gimmick_profile.steps.start_turn",
                    profile.id,
                )
            self._final_gimmick_profile = profile
        return self._final_gimmick_profile

    def _audition_scenario(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularPlan3StageStartScenario | tuple[
        InitialRegularInnerStageIssue, ...
    ]:
        stage_type = request.stage_type
        if stage_type not in self.audition_rules:
            return self._issue(
                "plan3-fixed-audition-stage-unbound",
                "request.stage_type",
                repr(stage_type),
            )
        rules = self.audition_rules[stage_type]
        expected_week = 6 if stage_type == MID1 else 13
        expected_stage_id = f"{rules.battle_config_id}:{stage_type}:1"
        if request.week != expected_week or request.stage_id != expected_stage_id:
            return self._issue(
                "plan3-fixed-audition-stage-unbound",
                "request.stage_id",
                (
                    f"week={request.week}:type={stage_type}:"
                    f"stage={request.stage_id}"
                ),
            )

        if stage_type == MID1:
            rng_before = MID1_RNG_BEFORE
            rng_token = MID1_RNG_TOKEN
            schedule = MID1_TURN_PARAMETER_TYPES
            namespace = 0x36000001
            gimmick_profile = None
        else:
            rng_before = FINAL_RNG_BEFORE
            rng_token = FINAL_RNG_TOKEN
            schedule = FINAL_TURN_PARAMETER_TYPES
            namespace = 0x3F000001
            loaded = self._final_gimmick(rules)
            if isinstance(loaded, tuple):
                return loaded
            gimmick_profile = loaded

        authority = _AUTHORITY_DESCRIPTION
        if gimmick_profile is not None:
            authority = (
                f"{authority}; scheduled turn-start effects from Master "
                f"gimmick profile {gimmick_profile.id}"
            )
        return InitialRegularPlan3StageStartScenario(
            expected_stage_id=expected_stage_id,
            expected_idol_card_id=IDOL_CARD_ID,
            chance=_chance(
                request,
                random_state=rng_before,
                random_root=rng_token,
                namespace=namespace,
            ),
            loadout=Plan3ExamStartLoadout(),
            authority_description=authority,
            # Final's first Master gimmick starts on turn 2; the actionable
            # turn-1 bootstrap itself therefore has no pending start effect.
            start_turn_effects_known_absent=True,
            post_exam_resolution=Plan3InnerPostExamResolution(
                patch=ProduceStatePatch(
                    attributes=request.outer_state.attributes,
                ),
                reward_requested=True,
            ),
            runtime_refs=Plan3InnerRuntimeRefs(
                setting_id=rules.exam_setting_id,
                gimmick_ref=rules.gimmick_group_id,
                battle_parameter_schedule=schedule,
                force_end_score=rules.force_end_score,
                force_end_stamina_recovery=(
                    rules.turn_end_stamina_recovery
                    if rules.force_end_score > 0
                    else 0
                ),
            ),
            search=partial(
                _search_with_resolved_rank,
                gimmick_profile=gimmick_profile,
            ),
            battle_resolver=self._battle_resolution,
            beam_width=16,
            database=DATABASE,
            master_dir=MASTER_DIR,
        )

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularInnerStageOutcome | tuple[InitialRegularInnerStageIssue, ...]:
        if request.stage_kind is InitialRegularInnerStageKind.LESSON:
            prepared = self._lesson_scenario(request)
        elif request.stage_kind is InitialRegularInnerStageKind.AUDITION:
            prepared = self._audition_scenario(request)
        else:  # pragma: no cover - enum is closed, retained as fail-closed seam
            return self._issue(
                "plan3-fixed-stage-kind-unbound",
                "request.stage_kind",
                str(request.stage_kind),
            )

        if isinstance(prepared, tuple):
            return prepared
        scenario = prepared

        result = build_initial_regular_plan3_stage_start_adapter(scenario)(request)
        if isinstance(result, InitialRegularInnerStageOutcome):
            self.outcomes.append(result)
        return result


def _reward_provider() -> InitialRegularRewardScenarioProvider:
    def source(_state, request, _node):
        card_id = REWARD_CARDS[request.week]
        progress = InitialRegularRewardProgressScenario(
            source=(
                InitialRegularRewardOfferSource.LESSON_END
                if request.week in {1, 9}
                else InitialRegularRewardOfferSource.AUDITION_END
            ),
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
                    f"caller:reward:w{request.week}:{card_id}",
                    InitialRegularRewardOffersScenario(progress),
                    rng_token=f"caller:reward:rng:w{request.week}",
                ),
            ),
            complete=True,
        )

    frontier_provider = InitialRegularRewardScenarioProvider(
        source,
        database=DATABASE,
    )

    def provider(state, request, weekly_node):
        """Keep the generic frontier, then overlay explicit receive IDs.

        ``UserProduceProgressPresent`` deliberately remains an unreceived
        server row.  The four IDs below are a caller-owned response overlay
        applied only to the typed ``RewardOffer`` consumed by the outer
        kernel; no authentic LocalSave or hidden present field is claimed.
        """

        expansion = frontier_provider(state, request, weekly_node)
        if expansion.issues or request.kind is not ExternalKind.REWARD_OFFERS:
            return expansion
        updated = []
        for branch in expansion.branches:
            if not isinstance(branch, InitialRegularWeightedRewardBranch):
                return InitialRegularOfflineBranchExpansion.paused(
                    # This is an internal typed seam; the generic provider
                    # should only emit reward branches for this request.
                    OuterSearchIssue(
                        "plan3-reward-guid-overlay-branch-kind",
                        "reward_provider.branch",
                        type(branch).__name__,
                    )
                )
            offers = tuple(
                replace(
                    offer,
                    received_card_instance_id=(
                        REWARD_INSTANCE_IDS.get(request.week)
                        if offer.card_id == REWARD_CARDS.get(request.week)
                        else offer.received_card_instance_id
                    ),
                )
                for offer in branch.outcome.offers
            )
            updated.append(
                replace(
                    branch,
                    outcome=replace(branch.outcome, offers=offers),
                )
            )
        return InitialRegularOfflineBranchExpansion.resolved(*updated)

    return provider


@dataclass(slots=True)
class _InitialRegularPlan3FixedScenarioAssembly:
    kernel: ProduceRolloutKernel
    initial_state: ProduceRolloutState
    inner_adapter: _InitialRegularPlan3FixedScenarioAdapter
    chance_provider: InitialRegularOfflineChanceAdapter
    mid1_context: Plan3AuditionContext
    final_context: Plan3AuditionContext


@dataclass(frozen=True, slots=True)
class InitialRegularPlan3FullFixedScenarioResult:
    """Prefix plus W7-to-Final continuation, retaining both typed traces."""

    prefix: InitialRegularPlan3W1Mid1FixedScenarioResult
    continuation: InitialRegularFixedRunResult
    report: InitialRegularFixedRunReport
    inner_outcomes: tuple[InitialRegularInnerStageOutcome, ...]
    audition_contexts: tuple[Plan3AuditionContext, Plan3AuditionContext]
    scenario_label: str
    authority_description: str

    def __post_init__(self) -> None:
        if not isinstance(self.prefix, InitialRegularPlan3W1Mid1FixedScenarioResult):
            raise TypeError("prefix must be the typed W1-to-Mid1 result")
        if not isinstance(self.continuation, InitialRegularFixedRunResult):
            raise TypeError("continuation must be InitialRegularFixedRunResult")
        if not isinstance(self.report, InitialRegularFixedRunReport):
            raise TypeError("report must be InitialRegularFixedRunReport")
        if self.continuation.initial_state != self.prefix.run.final_state:
            raise ValueError("continuation must start at the exact prefix W7 state")
        if any(
            not isinstance(value, InitialRegularInnerStageOutcome)
            for value in self.inner_outcomes
        ):
            raise TypeError("inner_outcomes must contain common outcomes")
        if any(
            not isinstance(value, Plan3AuditionContext)
            for value in self.audition_contexts
        ):
            raise TypeError("audition_contexts must contain typed contexts")
        if not self.scenario_label or not self.authority_description:
            raise ValueError("scenario identity and authority are required")

    @property
    def completed(self) -> bool:
        return self.continuation.completed

    @property
    def final_state(self) -> ProduceRolloutState:
        return self.continuation.final_state

    @property
    def action_path(self) -> tuple[str, ...]:
        return (*self.prefix.run.action_path, *self.continuation.action_path)

    @property
    def branch_path(self) -> tuple[str, ...]:
        return (*self.prefix.run.branch_path, *self.continuation.branch_path)

    @property
    def final_audition_outcome(
        self,
    ) -> InitialRegularInnerStageOutcome | None:
        if not self.completed:
            return None
        return next(
            (
                value
                for value in reversed(self.inner_outcomes)
                if value.rank is not None
            ),
            None,
        )


def _build_fixed_scenario_assembly(
) -> _InitialRegularPlan3FixedScenarioAssembly:
    kernel, initial = new_initial_regular_state(
        character_id="fktn",
        stamina=42,
        max_stamina=42,
        vocal=321,
        dance=456,
        visual=654,
        deck=_caller_bound_initial_deck(),
        week=1,
        master_dir=MASTER_DIR,
    )
    mid1_rules = load_audition_rules(
        IDOL_CARD_ID,
        step_type=MID1,
        number=1,
        master_dir=MASTER_DIR,
    )
    final_rules = load_audition_rules(
        IDOL_CARD_ID,
        step_type=FINAL,
        number=1,
        master_dir=MASTER_DIR,
    )
    mid1_context = _audition_context(
        mid1_rules,
        step_type_value=16,
        turn_parameter_types=MID1_TURN_PARAMETER_TYPES,
        battle_bonus_permille=MID1_BATTLE_BONUS_PERMILLE,
        npc_terminal_scores=MID1_NPC_TERMINAL_SCORES,
    )
    final_context = _audition_context(
        final_rules,
        step_type_value=18,
        turn_parameter_types=FINAL_TURN_PARAMETER_TYPES,
        battle_bonus_permille=FINAL_BATTLE_BONUS_PERMILLE,
        npc_terminal_scores=FINAL_NPC_TERMINAL_SCORES,
    )
    inner_adapter = _InitialRegularPlan3FixedScenarioAdapter(
        mid1_rules=mid1_rules,
        mid1_context=mid1_context,
        final_rules=final_rules,
        final_context=final_context,
    )
    lesson_runtime = InitialRegularLessonRuntimeContext(
        lesson_limit_up_score=0,
        exam_extra_turn=0,
        battle_bonus_permille=(0, 0, 0),
        gimmick_group_id="",
    )

    def lesson_provider(state, request, _node):
        catalog = build_initial_regular_lesson_branch_catalog(
            state,
            request,
            idol_card_id=IDOL_CARD_ID,
            base_sp_rate_permils=InitialRegularSpRatePermils(0, 0, 0),
            sp_modifiers_known_absent=True,
            selected_sp_stage_ids={
                "vocal": "p_step_lesson_level-001-fktn-sp-vo-001",
                "dance": "p_step_lesson_level-001-fktn-sp-da-001",
                "visual": "p_step_lesson_level-001-fktn-sp-vi-001",
            },
            runtime_context=lesson_runtime,
            master_dir=MASTER_DIR,
        )
        stage = catalog.for_attribute("vocal").fixed_stage
        if request.week == 1:
            rng_before = LESSON_RNG_BEFORE
            rng_token = LESSON_RNG_TOKEN
        else:
            rng_before = SECOND_LESSON_RNG_BEFORE
            rng_token = SECOND_LESSON_RNG_TOKEN
        return build_initial_regular_weighted_weekly_frontier(
            catalog,
            "vocal",
            inner_execution=InitialRegularInnerLessonExecutionInputs(
                idol_card_id=IDOL_CARD_ID,
                branches=(
                    InitialRegularInnerLessonBranchExecution(
                        stage.branch_key,
                        rng_before,
                        rng_token,
                    ),
                ),
            ),
        )

    def audition_provider(state, request, _node):
        if request.stage_type == MID1:
            rng_before = MID1_RNG_BEFORE
            rng_token = MID1_RNG_TOKEN
            schedule = MID1_TURN_PARAMETER_TYPES
            bonuses = MID1_BATTLE_BONUS_PERMILLE
            branch_id = "caller:plan3:w6:mid1:exact"
        else:
            rng_before = FINAL_RNG_BEFORE
            rng_token = FINAL_RNG_TOKEN
            schedule = FINAL_TURN_PARAMETER_TYPES
            bonuses = FINAL_BATTLE_BONUS_PERMILLE
            branch_id = "caller:plan3:w13:final:exact"
        return build_initial_regular_audition_frontier(
            state,
            request,
            idol_card_id=IDOL_CARD_ID,
            branches=(
                InitialRegularAuditionScenarioBranch(
                    probability=Fraction(1),
                    number=1,
                    rng_before=rng_before,
                    turn_parameter_types=schedule,
                    battle_bonus_permille=bonuses,
                    extra_turn=0,
                    rng_token=rng_token,
                    branch_id=branch_id,
                ),
            ),
            database=DATABASE,
            master_dir=MASTER_DIR,
        )

    reward_provider = _reward_provider()
    routes: list[InitialRegularBranchRoute] = []
    for week in (1, 9):
        routes.extend(
            (
                InitialRegularBranchRoute(
                    InitialRegularBranchRouteKey(
                        ExternalKind.WEEKLY_ACTION_OUTCOME,
                        LESSON,
                        None,
                        None,
                        week,
                    ),
                    lesson_provider,
                ),
                InitialRegularBranchRoute(
                    InitialRegularBranchRouteKey(
                        ExternalKind.REWARD_OFFERS,
                        LESSON,
                        None,
                        None,
                        week,
                    ),
                    reward_provider,
                ),
            )
        )
    for stage_type, week in ((MID1, 6), (FINAL, 13)):
        routes.extend(
            (
                InitialRegularBranchRoute(
                    InitialRegularBranchRouteKey(
                        ExternalKind.INNER_EXAM_OUTCOME,
                        stage_type,
                        stage_type,
                        kernel.inner_exam_adapter_ref,
                        week,
                    ),
                    audition_provider,
                ),
                InitialRegularBranchRoute(
                    InitialRegularBranchRouteKey(
                        ExternalKind.REWARD_OFFERS,
                        stage_type,
                        None,
                        None,
                        week,
                    ),
                    reward_provider,
                ),
            )
        )
    chance_provider = InitialRegularOfflineChanceAdapter(
        InitialRegularOfflineBranchRegistry(routes),
        inner_registry=InitialRegularInnerStageAdapterRegistry(
            {"plan3": inner_adapter}
        ),
        database=DATABASE,
        master_dir=MASTER_DIR,
    )
    return _InitialRegularPlan3FixedScenarioAssembly(
        kernel,
        initial,
        inner_adapter,
        chance_provider,
        mid1_context,
        final_context,
    )


def _run_prefix(
    assembly: _InitialRegularPlan3FixedScenarioAssembly,
) -> InitialRegularPlan3W1Mid1FixedScenarioResult:
    def policy(state, legal):
        if state.phase is RolloutPhase.READY_FOR_REWARD:
            return next(
                value for value in legal if value.kind is ActionKind.SELECT_REWARD
            )
        if state.week == 1:
            return next(value for value in legal if value.choice_id == LESSON)
        if 2 <= state.week <= 5:
            return next(value for value in legal if value.choice_id == "rest")
        if state.week == 6:
            return next(value for value in legal if value.choice_id == MID1)
        return None

    run = run_initial_regular_fixed_scenario(
        assembly.kernel,
        assembly.initial_state,
        policy=policy,
        chance_provider=assembly.chance_provider,
        max_transitions=32,
    )
    if (
        run.stop is not InitialRegularFixedRunStop.POLICY
        or run.final_state.week != 7
        or run.final_state.phase is not RolloutPhase.READY_FOR_WEEK
        or run.final_state.pending is not None
        or run.final_state.terminal
    ):
        raise RuntimeError(
            "Plan3 W1-to-Mid1 scenario did not reach its continuable W7 "
            f"boundary: stop={run.stop}:issues={run.issues}"
        )
    if len(assembly.inner_adapter.outcomes) != 2:
        raise RuntimeError(
            "Plan3 W1-to-Mid1 scenario did not resolve both inner stages"
        )
    return InitialRegularPlan3W1Mid1FixedScenarioResult(
        run=run,
        inner_outcomes=tuple(assembly.inner_adapter.outcomes),
        audition_context=assembly.mid1_context,
        scenario_label=SCENARIO_LABEL,
        authority_description=_AUTHORITY_DESCRIPTION,
    )


def run_fktn_plan3_deterministic_w1_mid1(
) -> InitialRegularPlan3W1Mid1FixedScenarioResult:
    """Run the deterministic Plan 3 prefix through its continuable W7 state."""

    return _run_prefix(_build_fixed_scenario_assembly())


def run_fktn_plan3_deterministic_w1_final(
) -> InitialRegularPlan3FullFixedScenarioResult:
    """Run the prefix once, then resume its exact W7 state through Final.

    A genuine Final runtime gap is returned as the continuation's typed
    ``EXTERNAL_BLOCKED`` result, preserving every completed W7-to-boundary
    transition.  No prefix transition is replayed during the continuation.
    """

    assembly = _build_fixed_scenario_assembly()
    prefix = _run_prefix(assembly)

    def policy(state, legal):
        if state.phase is RolloutPhase.READY_FOR_REWARD:
            return next(
                value for value in legal if value.kind is ActionKind.SELECT_REWARD
            )
        if state.week == 9:
            return next(value for value in legal if value.choice_id == LESSON)
        if state.week in {7, 8, 10, 11, 12}:
            return next(value for value in legal if value.choice_id == "rest")
        if state.week == 13:
            return next(value for value in legal if value.choice_id == FINAL)
        return None

    continuation = run_initial_regular_fixed_scenario(
        assembly.kernel,
        prefix.run.final_state,
        policy=policy,
        chance_provider=assembly.chance_provider,
        max_transitions=32,
    )
    if continuation.stop is InitialRegularFixedRunStop.TERMINAL:
        if (
            not continuation.completed
            or continuation.final_state.lifecycle is not Lifecycle.COMPLETED
        ):
            raise RuntimeError("Plan3 Final terminal boundary is inconsistent")
    elif continuation.stop is not InitialRegularFixedRunStop.EXTERNAL_BLOCKED:
        raise RuntimeError(
            "Plan3 W7-to-Final scenario stopped outside a typed runtime "
            f"boundary: stop={continuation.stop}:issues={continuation.issues}"
        )
    offset = len(prefix.run.steps)
    combined_run = InitialRegularFixedRunResult(
        initial_state=prefix.run.initial_state,
        final_state=continuation.final_state,
        stop=continuation.stop,
        issues=continuation.issues,
        steps=(
            *prefix.run.steps,
            *(
                replace(step, index=offset + index)
                for index, step in enumerate(continuation.steps, start=1)
            ),
        ),
    )
    return InitialRegularPlan3FullFixedScenarioResult(
        prefix=prefix,
        continuation=continuation,
        report=build_initial_regular_fixed_run_report(combined_run),
        inner_outcomes=tuple(assembly.inner_adapter.outcomes),
        audition_contexts=(assembly.mid1_context, assembly.final_context),
        scenario_label=FULL_SCENARIO_LABEL,
        authority_description=_AUTHORITY_DESCRIPTION,
    )


__all__ = [
    "FINAL_BATTLE_BONUS_PERMILLE",
    "FINAL_NPC_TERMINAL_SCORES",
    "FINAL_RNG_BEFORE",
    "FINAL_RNG_TOKEN",
    "FINAL_TURN_PARAMETER_TYPES",
    "FULL_SCENARIO_LABEL",
    "IDOL_CARD_ID",
    "LESSON_RNG_BEFORE",
    "LESSON_RNG_TOKEN",
    "LESSON_STAGE_ID",
    "MID1_BATTLE_BONUS_PERMILLE",
    "MID1_NPC_TERMINAL_SCORES",
    "MID1_RNG_BEFORE",
    "MID1_RNG_TOKEN",
    "MID1_TURN_PARAMETER_TYPES",
    "REWARD_CARDS",
    "REWARD_INSTANCE_IDS",
    "SCENARIO_LABEL",
    "SECOND_LESSON_RNG_BEFORE",
    "SECOND_LESSON_RNG_TOKEN",
    "SECOND_LESSON_STAGE_ID",
    "InitialRegularPlan3FullFixedScenarioResult",
    "InitialRegularPlan3W1Mid1FixedScenarioResult",
    "run_fktn_plan3_deterministic_w1_final",
    "run_fktn_plan3_deterministic_w1_mid1",
]
