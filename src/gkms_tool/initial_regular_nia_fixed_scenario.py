"""One bounded N.I.A. Pro Mid1 inner/outer fixed scenario.

This is deliberately *not* a 27-week N.I.A. route.  It starts from an
explicit caller-authored week-9 outer state, runs the first ``produce-004``
Mid1 player inner against the pinned PC Master profile, resolves rank/reward
from complete caller-owned NPC terminal tracks, selects one caller-resolved
reward offer, and stops at the continuable week-10 policy boundary.

Master owns the stage/card definitions and route shape.  Deck order/GUIDs,
RNG, scalar runtime, battle bonus, NPC tracks, the zero NPC multiplier, and
the reward offer are scenario inputs.  They are not claimed to come from an
authentic N.I.A. LocalSave and no server-owned candidate pool is regenerated.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Final

from .audition_rules import MID1
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
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
    project_initial_regular_inner_stage_outcome,
)
from .initial_regular_nia_audition_resolver import (
    NiaAuditionRewardRule,
    NiaAuditionTerminalResolution,
    NiaAuditionTerminalScenario,
    NiaNpcTerminalTrack,
    NiaNpcTerminalTrackAuthority,
    load_nia_audition_stage_for_acceptance,
)
from .initial_regular_nia_inner_adapter import InitialRegularNiaInnerAdapter
from .master_db import DEFAULT_DATABASE
from .nia_inner_terminal_acceptance import (
    NiaInnerTerminalAcceptance,
    build_nia_produce004_fktn_starter_terminal_fixture,
    project_nia_fixture_outer_deck,
    run_nia_produce004_mid1_terminal_acceptance,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .nia_static_inventory import (
    NIA_PLAN_TYPE,
    NiaStaticAuditionStage,
    build_nia_static_inventory,
)
from .produce_rollout import (
    ActionKind,
    AttributeValues,
    ChanceBranch,
    DeckEntry,
    ExternalKind,
    ExternalRequest,
    InnerExamOutcome,
    Lifecycle,
    ProduceAction,
    ProduceRolloutKernel,
    ProduceRolloutState,
    RewardOffer,
    RewardOffersOutcome,
    RolloutPhase,
)
from .produce_rollout_expectimax import (
    ExternalChanceExpansion,
    OuterSearchIssue,
    WeightedExternalOutcome,
)
from .route_calendar import load_route_calendar


NIA_PRO_MID1_FIXED_SCENARIO_LABEL: Final = (
    "nia-pro-produce004-mid1-single-outer-section"
)
NIA_PRO_MID1_START_WEEK: Final = 9
NIA_PRO_MID1_NEXT_WEEK: Final = 10
NIA_PRO_MID1_STEP_TYPE_VALUE: Final = 16
NIA_PRO_MID1_BATTLE_BONUS_PERMILLE: Final = (10000, 10000, 10000)
NIA_PRO_MID1_NPC_TERMINAL_SCORES: Final = (1090, 700, 400, 300, 300)
NIA_PRO_MID1_NPC_SCORE_MULTIPLE_PERMILLE: Final = 0
NIA_PRO_MID1_REWARD_CARD_ID: Final = "p_card-00-act-0_002"
NIA_PRO_MID1_REWARD_CARD_UPGRADE: Final = 0
NIA_PRO_MID1_REWARD_OFFER_ID: Final = "nia-pro-mid1-fixed-reward-0"
NIA_PRO_MID1_REWARD_INSTANCE_ID: Final = "nia-pro-mid1-fixed-reward-guid-01"
NIA_PRO_MID1_INNER_BRANCH_ID: Final = "caller:nia-pro-mid1:terminal"
NIA_PRO_MID1_REWARD_BRANCH_ID: Final = "caller:nia-pro-mid1:reward-offers"

_AUTHORITY_DESCRIPTION: Final = (
    "PC Master produce-004 Mid1 stage/card/calendar rows; caller-authored "
    "synthetic outer state, ordered deck/GUIDs, RNG and 10000-permille bonus; "
    "caller-resolved complete NPC terminal tracks with explicit zero "
    "npcScoreMultiplePermil (no multiplier transition timing applies), rank "
    "reward rule, and fixed reward offer; not an authentic N.I.A. LocalSave"
)
_EXPECTED_POLICY_STOP_ISSUE: Final = OuterSearchIssue(
    "fixed-scenario-policy-stopped",
    "policy",
)


class InitialRegularNiaFixedScenarioAuthority(StrEnum):
    """Machine-readable authority boundary for this one-section fixture."""

    PC_MASTER_PLUS_CALLER_RESOLVED = (
        "pc-master-plus-caller-resolved-synthetic-runtime"
    )


@dataclass(frozen=True, slots=True)
class InitialRegularNiaFixedScenarioResult:
    """Report envelope for the bounded week-9 Mid1 to week-10 scenario."""

    run: InitialRegularFixedRunResult
    report: InitialRegularFixedRunReport
    acceptance: NiaInnerTerminalAcceptance
    terminal_scenario: NiaAuditionTerminalScenario
    resolution: NiaAuditionTerminalResolution
    inner_outcome: InitialRegularInnerStageOutcome
    reward_offers_outcome: RewardOffersOutcome
    scenario_label: str = field(
        default=NIA_PRO_MID1_FIXED_SCENARIO_LABEL,
        init=False,
    )
    authority: InitialRegularNiaFixedScenarioAuthority = field(
        default=InitialRegularNiaFixedScenarioAuthority.PC_MASTER_PLUS_CALLER_RESOLVED,
        init=False,
    )
    authority_description: str = field(
        default=_AUTHORITY_DESCRIPTION,
        init=False,
    )
    partial_success: bool = field(default=True, init=False)
    authentic_nia_local_save: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.run, InitialRegularFixedRunResult):
            raise TypeError("run must be InitialRegularFixedRunResult")
        if not isinstance(self.report, InitialRegularFixedRunReport):
            raise TypeError("report must be InitialRegularFixedRunReport")
        if not isinstance(self.acceptance, NiaInnerTerminalAcceptance):
            raise TypeError("acceptance must be NiaInnerTerminalAcceptance")
        if not isinstance(self.terminal_scenario, NiaAuditionTerminalScenario):
            raise TypeError("terminal_scenario must be NiaAuditionTerminalScenario")
        if not isinstance(self.resolution, NiaAuditionTerminalResolution):
            raise TypeError("resolution must be NiaAuditionTerminalResolution")
        if not isinstance(self.inner_outcome, InitialRegularInnerStageOutcome):
            raise TypeError("inner_outcome must be InitialRegularInnerStageOutcome")
        if not isinstance(self.reward_offers_outcome, RewardOffersOutcome):
            raise TypeError("reward_offers_outcome must be RewardOffersOutcome")
        if self.report.stop is not self.run.stop:
            raise ValueError("report stop must match the fixed run")
        if self.run.stop is not InitialRegularFixedRunStop.POLICY:
            raise ValueError("bounded NIA scenario must stop at its policy boundary")
        if self.run.issues or self.report.issues:
            raise ValueError("bounded NIA scenario cannot contain unresolved issues")
        if self.run.initial_state.week != NIA_PRO_MID1_START_WEEK:
            raise ValueError("bounded NIA scenario must start at Mid1 week 9")
        final = self.run.final_state
        if (
            final.week != NIA_PRO_MID1_NEXT_WEEK
            or final.phase is not RolloutPhase.READY_FOR_WEEK
            or final.pending is not None
            or final.lifecycle is not Lifecycle.IN_PROGRESS
        ):
            raise ValueError("bounded NIA scenario must end at continuable week 10")


def _split_score(final_score: int, turns: int) -> tuple[int, ...]:
    quotient, remainder = divmod(final_score, turns)
    return tuple(
        quotient + (1 if index < remainder else 0)
        for index in range(turns)
    )


def _boosted_acceptance(
    *,
    database: Path,
    master_dir: Path,
) -> NiaInnerTerminalAcceptance:
    fixture = build_nia_produce004_fktn_starter_terminal_fixture(
        database=database,
    )
    runtime = replace(
        fixture.runtime,
        battle_bonus_permille_vocal=NIA_PRO_MID1_BATTLE_BONUS_PERMILLE[0],
        battle_bonus_permille_dance=NIA_PRO_MID1_BATTLE_BONUS_PERMILLE[1],
        battle_bonus_permille_visual=NIA_PRO_MID1_BATTLE_BONUS_PERMILLE[2],
    )
    return run_nia_produce004_mid1_terminal_acceptance(
        replace(fixture, runtime=runtime),
        database=database,
        master_dir=master_dir,
    )


def _npc_tracks(
    catalog: NiaStaticAuditionStage,
) -> tuple[NiaNpcTerminalTrack, ...]:
    if len(catalog.npc_scores) != len(NIA_PRO_MID1_NPC_TERMINAL_SCORES):
        raise ValueError("fixed NIA NPC scores do not cover the Master field")
    tracks: list[NiaNpcTerminalTrack] = []
    for master, final_score in zip(
        catalog.npc_scores,
        NIA_PRO_MID1_NPC_TERMINAL_SCORES,
        strict=True,
    ):
        vocal = final_score * master.vocal_permille // 1000
        dance = final_score * master.dance_permille // 1000
        tracks.append(
            NiaNpcTerminalTrack(
                npc_group_id=catalog.npc_group_id,
                number=master.number,
                mob_id=master.mob_id,
                turn_scores=_split_score(final_score, catalog.turns),
                final_score=final_score,
                vocal_score=vocal,
                dance_score=dance,
                visual_score=final_score - vocal - dance,
            )
        )
    return tuple(tracks)


def _terminal_scenario(
    acceptance: NiaInnerTerminalAcceptance,
    catalog: NiaStaticAuditionStage,
) -> NiaAuditionTerminalScenario:
    return NiaAuditionTerminalScenario(
        stage_type=catalog.step_type,
        number=catalog.number,
        npc_group_id=catalog.npc_group_id,
        authority=NiaNpcTerminalTrackAuthority.CALLER_RESOLVED,
        is_static_npc_score=catalog.is_static_npc_score,
        npc_score_multiple_permille=(
            NIA_PRO_MID1_NPC_SCORE_MULTIPLE_PERMILLE
        ),
        # With an explicit zero multiplier there is no runtime mutation turn.
        npc_multiplier_timing=None,
        battle_bonus_permille=NIA_PRO_MID1_BATTLE_BONUS_PERMILLE,
        gimmick_group_id=catalog.gimmick_group_id,
        turn_parameter_types=acceptance.battle_parameter_schedule,
        npc_tracks=_npc_tracks(catalog),
        reward_rule=NiaAuditionRewardRule(
            catalog.step_type,
            catalog.number,
            (True, False, False, False, False, False),
        ),
    )


def _outer_state(
    acceptance: NiaInnerTerminalAcceptance,
    *,
    initial_produce_points: int,
) -> ProduceRolloutState:
    return ProduceRolloutState(
        mode_id=acceptance.fixture.selector.produce_id,
        character_id="fktn",
        week=NIA_PRO_MID1_START_WEEK,
        total_weeks=27,
        phase=RolloutPhase.READY_FOR_WEEK,
        stamina=acceptance.fixture.runtime.stamina,
        max_stamina=acceptance.fixture.runtime.max_stamina,
        attributes=AttributeValues(0, 0, 0),
        deck=project_nia_fixture_outer_deck(acceptance.fixture),
        # This is a caller-declared Mid1 start scalar, not a replay of W1-W8.
        produce_points=initial_produce_points,
    )


def _inner_request(
    state: ProduceRolloutState,
    request: ExternalRequest,
    branch: ChanceBranch,
    acceptance: NiaInnerTerminalAcceptance,
    catalog: NiaStaticAuditionStage,
) -> InitialRegularInnerStageRequest:
    return InitialRegularInnerStageRequest(
        external_request_id=request.request_id,
        week=request.week,
        action_id=request.action_id,
        stage_kind=InitialRegularInnerStageKind.AUDITION,
        plan_type=NIA_PLAN_TYPE,
        character_id=state.character_id,
        outer_state=state,
        branch=branch,
        idol_card_id=acceptance.fixture.selector.idol_card_id,
        stage_type=catalog.step_type,
        stage_id=catalog.stage_id,
        setting_id=catalog.exam_setting_id,
        exam_type=1,
        step_type_value=NIA_PRO_MID1_STEP_TYPE_VALUE,
        limit_turn=catalog.turns,
        extra_turn=0,
        clear_border=catalog.clear_border,
        # Android audition construction maps disabled force-end 0 to -1.
        limit_border=catalog.limit_border if catalog.limit_border > 0 else -1,
        rng_before=acceptance.initial_native_state.random_state,
        battle_bonus_permille=NIA_PRO_MID1_BATTLE_BONUS_PERMILLE,
        gimmick_group_id=catalog.gimmick_group_id,
        turn_parameter_types=acceptance.battle_parameter_schedule,
    )


def _outer_issues(
    issues: tuple[InitialRegularInnerStageIssue, ...],
) -> tuple[OuterSearchIssue, ...]:
    return tuple(
        OuterSearchIssue(
            f"nia-fixed-{issue.code}",
            issue.field,
            issue.detail,
        )
        for issue in issues
    )


def _normalize_expected_partial_stop(
    run: InitialRegularFixedRunResult,
) -> InitialRegularFixedRunResult:
    """Remove only the driver's synthetic issue for this intentional stop."""

    if (
        run.stop is not InitialRegularFixedRunStop.POLICY
        or run.issues != (_EXPECTED_POLICY_STOP_ISSUE,)
        or run.final_state.week != NIA_PRO_MID1_NEXT_WEEK
        or run.final_state.phase is not RolloutPhase.READY_FOR_WEEK
        or run.final_state.pending is not None
    ):
        return run
    return replace(run, issues=())


def run_nia_produce004_mid1_fixed_scenario(
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularNiaFixedScenarioResult:
    """Execute one N.I.A. Pro Mid1 inner/reward/outer section.

    A successful result is intentionally partial: ``run.stop`` remains
    ``POLICY`` at week 10, while the result envelope marks
    ``partial_success=True`` and retains no issue for that requested boundary.
    """

    database = Path(database)
    master_dir = Path(master_dir)
    acceptance = _boosted_acceptance(
        database=database,
        master_dir=master_dir,
    )
    catalog = load_nia_audition_stage_for_acceptance(
        acceptance,
        master_dir=master_dir,
        database=database,
    )
    scenario = _terminal_scenario(acceptance, catalog)
    adapter = InitialRegularNiaInnerAdapter(
        acceptance,
        scenario,
        catalog,
        master_dir,
        database,
    )
    resolved = adapter.resolve()
    if not resolved.ready:
        detail = ",".join(
            f"{issue.code}:{issue.field}" for issue in resolved.issues
        )
        raise RuntimeError(f"fixed NIA rank scenario is blocked: {detail}")
    resolution = resolved.resolution
    assert resolution is not None

    inventory = build_nia_static_inventory(
        acceptance.fixture.selector.idol_card_id,
        produce_id=acceptance.fixture.selector.produce_id,
        master_dir=master_dir,
        database=database,
    )
    calendar = load_route_calendar(
        acceptance.fixture.selector.produce_id,
        master_dir=master_dir,
    )
    kernel = ProduceRolloutKernel(
        calendar,
        rest_recovery_permille=(
            inventory.setting.refresh_stamina_recovery_permille
        ),
        inner_exam_adapter_ref=(
            "gkms_tool.initial_regular_nia_inner_adapter."
            "InitialRegularNiaInnerAdapter"
        ),
    )
    initial_state = _outer_state(
        acceptance,
        initial_produce_points=inventory.setting.initial_produce_point,
    )

    captured_inner: list[InitialRegularInnerStageOutcome] = []
    captured_rewards: list[RewardOffersOutcome] = []

    def chance_provider(
        state: ProduceRolloutState,
        request: ExternalRequest,
    ) -> ExternalChanceExpansion:
        if request.kind is ExternalKind.INNER_EXAM_OUTCOME:
            branch = ChanceBranch(
                NIA_PRO_MID1_INNER_BRANCH_ID,
                probability=1.0,
                rng_token="caller:nia-pro-mid1:inner-rng",
            )
            common_request = _inner_request(
                state,
                request,
                branch,
                acceptance,
                catalog,
            )
            common = adapter(common_request)
            if not isinstance(common, InitialRegularInnerStageOutcome):
                return ExternalChanceExpansion.paused(*_outer_issues(common))
            captured_inner.append(common)
            outcome = project_initial_regular_inner_stage_outcome(
                common_request,
                common,
            )
            if not isinstance(outcome, InnerExamOutcome):
                raise AssertionError("NIA audition must project to InnerExamOutcome")
        elif request.kind is ExternalKind.REWARD_OFFERS:
            outcome = RewardOffersOutcome(
                request_id=request.request_id,
                branch=ChanceBranch(
                    NIA_PRO_MID1_REWARD_BRANCH_ID,
                    probability=1.0,
                    rng_token="caller:nia-pro-mid1:fixed-reward",
                ),
                offers=(
                    RewardOffer(
                        NIA_PRO_MID1_REWARD_OFFER_ID,
                        NIA_PRO_MID1_REWARD_CARD_ID,
                        NIA_PRO_MID1_REWARD_CARD_UPGRADE,
                        NIA_PRO_MID1_REWARD_INSTANCE_ID,
                    ),
                ),
                remaining_exclude_count=0,
            )
            captured_rewards.append(outcome)
        else:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(
                    "nia-fixed-external-kind-unhandled",
                    "request.kind",
                    request.kind.value,
                )
            )
        return ExternalChanceExpansion.resolved(
            WeightedExternalOutcome(Fraction(1), outcome)
        )

    def policy(
        state: ProduceRolloutState,
        legal: tuple[ProduceAction, ...],
    ) -> ProduceAction | None:
        if state.phase is RolloutPhase.READY_FOR_REWARD:
            return next(
                (
                    action
                    for action in legal
                    if action.kind is ActionKind.SELECT_REWARD
                    and action.choice_id == NIA_PRO_MID1_REWARD_OFFER_ID
                ),
                None,
            )
        if state.week == NIA_PRO_MID1_START_WEEK:
            desired = ProduceAction.delegate_exam(MID1)
            return desired if desired in legal else None
        return None

    raw_run = run_initial_regular_fixed_scenario(
        kernel,
        initial_state,
        policy=policy,
        chance_provider=chance_provider,
        max_transitions=8,
    )
    run = _normalize_expected_partial_stop(raw_run)
    if len(captured_inner) != 1 or len(captured_rewards) != 1:
        raise RuntimeError(
            "fixed NIA scenario did not resolve exactly one inner and reward row"
        )
    report = build_initial_regular_fixed_run_report(run)
    return InitialRegularNiaFixedScenarioResult(
        run=run,
        report=report,
        acceptance=acceptance,
        terminal_scenario=scenario,
        resolution=resolution,
        inner_outcome=captured_inner[0],
        reward_offers_outcome=captured_rewards[0],
    )


# Concise alias for callers which already know the module's fixed profile.
run_initial_regular_nia_fixed_scenario = (
    run_nia_produce004_mid1_fixed_scenario
)


__all__ = [
    "NIA_PRO_MID1_BATTLE_BONUS_PERMILLE",
    "NIA_PRO_MID1_FIXED_SCENARIO_LABEL",
    "NIA_PRO_MID1_INNER_BRANCH_ID",
    "NIA_PRO_MID1_NEXT_WEEK",
    "NIA_PRO_MID1_NPC_SCORE_MULTIPLE_PERMILLE",
    "NIA_PRO_MID1_NPC_TERMINAL_SCORES",
    "NIA_PRO_MID1_REWARD_BRANCH_ID",
    "NIA_PRO_MID1_REWARD_CARD_ID",
    "NIA_PRO_MID1_REWARD_CARD_UPGRADE",
    "NIA_PRO_MID1_REWARD_OFFER_ID",
    "NIA_PRO_MID1_REWARD_INSTANCE_ID",
    "NIA_PRO_MID1_START_WEEK",
    "NIA_PRO_MID1_STEP_TYPE_VALUE",
    "InitialRegularNiaFixedScenarioAuthority",
    "InitialRegularNiaFixedScenarioResult",
    "run_initial_regular_nia_fixed_scenario",
    "run_nia_produce004_mid1_fixed_scenario",
]
