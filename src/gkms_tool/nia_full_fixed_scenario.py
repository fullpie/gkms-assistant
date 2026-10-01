"""Deterministic 27-week N.I.A. Pro fixed scenario for FKTN Plan3.

This runner proves that the shared outer lifecycle, real Master starter deck,
bounded weekly action adapters, all three true N.I.A. gimmick profiles, the
Plan3 native search, rank resolver, and response-bound reward GUIDs compose
from week 1 through Final.  The weekly route and server rolls are explicit
caller-authored scenario facts: PC Master does not expose the generated
player-facing ``ProduceSchedule`` for an individual run.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from functools import lru_cache
import json
from pathlib import Path
from typing import Final, Mapping, TypeVar

from .audition_rules import FINAL, MID1, MID2
from .audition_turn_schedule import calculate_audition_base_multiplier_permils
from .exam_context import (
    AuditionProgressBonusValues,
    calculate_audition_bonus_permils,
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
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
    project_initial_regular_inner_stage_outcome,
)
from .initial_regular_nia_audition_resolver import (
    NiaAuditionRewardRule,
    NiaAuditionTerminalResolution,
    NiaAuditionTerminalScenario,
    NiaNpcMultiplierTimingResolution,
    NiaNpcTerminalTrack,
    NiaNpcTerminalTrackAuthority,
    load_nia_audition_stage_for_acceptance,
)
from .initial_regular_nia_inner_adapter import InitialRegularNiaInnerAdapter
from .initial_regular_nia_interval_scenario import (
    InitialRegularNiaIntervalScenario,
)
from .initial_regular_nia_event_business_scenario import (
    InitialRegularNiaEventBusinessScenario,
)
from .initial_regular_nia_business_scenario import (
    InitialRegularNiaBusinessScenario,
)
from .initial_regular_nia_customize_scenario import (
    InitialRegularNiaCustomizeScenario,
)
from .initial_regular_nia_refresh_scenario import (
    InitialRegularNiaRefreshScenario,
)
from .initial_regular_nia_self_lesson_scenario import (
    InitialRegularNiaSelfLessonScenario,
)
from .initial_regular_supply_scenario_adapter import InitialRegularSupplyScenario
from .master_db import DEFAULT_DATABASE, get_idol_profile
from .nia_inner_terminal_acceptance import (
    NiaCallerOwnedInnerRuntime,
    NiaInnerMasterProfileSelector,
    NiaInnerTerminalAcceptance,
    build_nia_produce004_fktn_starter_terminal_fixture,
    build_nia_terminal_fixture_from_outer_deck,
    project_nia_fixture_outer_deck,
    run_nia_terminal_acceptance,
)
from .nia_outer_action_runtime import (
    BUSINESS,
    CARE_PACKAGE,
    EVENT_BUSINESS,
    INTERVAL,
    OUTING,
    REFRESH,
    SELF_LESSON,
    SPECIAL_GUIDANCE,
    NiaBusinessScenario,
    NiaCarePackageScenario,
    NiaResolvedWeeklyAction,
    NiaSelfLessonScenario,
    resolve_nia_business,
    resolve_nia_care_package,
    resolve_nia_event_business,
    resolve_nia_interval,
    resolve_nia_outing,
    resolve_nia_refresh,
    resolve_nia_self_lesson,
    resolve_nia_special_guidance_skip,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .nia_static_inventory import (
    NIA_PLAN_TYPE,
    NiaStaticAuditionStage,
    NiaStaticInventory,
    build_nia_static_inventory,
)
from .produce_rollout import (
    ActionKind,
    AttributeValues,
    ChanceBranch,
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


NIA_FULL_FIXED_SCENARIO_LABEL: Final = "nia-pro-fktn-plan3-w1-final-fixed"

_ResponseScenarioT = TypeVar("_ResponseScenarioT")

_WEEK_ACTION = {
    1: SELF_LESSON,
    2: BUSINESS,
    3: OUTING,
    4: SELF_LESSON,
    5: BUSINESS,
    6: OUTING,
    7: BUSINESS,
    8: SPECIAL_GUIDANCE,
    10: OUTING,
    11: SELF_LESSON,
    12: BUSINESS,
    13: OUTING,
    14: SELF_LESSON,
    15: BUSINESS,
    16: SELF_LESSON,
    17: SPECIAL_GUIDANCE,
    19: OUTING,
    20: SELF_LESSON,
    21: BUSINESS,
    22: SPECIAL_GUIDANCE,
    23: SELF_LESSON,
    24: BUSINESS,
    25: SELF_LESSON,
    26: OUTING,
}
_SELF_PHASE = {
    1: 1,
    4: 1,
    11: 2,
    14: 2,
    16: 2,
    20: 3,
    23: 3,
    25: 3,
}
_BUSINESS_PHASE = {
    2: ("before_1st", "01-033"),
    5: ("before_1st", "01-034"),
    7: ("before_1st", "02-033"),
    12: ("before_2nd", "03-033"),
    15: ("before_2nd", "04-033"),
    21: ("before_3rd", "05-033"),
    24: ("before_3rd", "06-033"),
}
_STAGE_WEEK = {9: MID1, 18: MID2, 27: FINAL}
_STEP_TYPE_VALUE = {MID1: 16, MID2: 17, FINAL: 18}
# These are explicit server-runtime scenario inputs, not values inferred from
# the accumulated vote counter or Master.  The fixed acceptance scenario uses
# a rank-one trace at each milestone; callers replacing it with captured
# runtime data must supply the observed VoteBonusPermil instead.
_RUNTIME_VOTE_BONUS_PERMILLE = {MID1: 10000, MID2: 100000, FINAL: 170000}
_EXAM_REWARD_CARD = {
    MID1: "p_card-00-act-0_002",
    MID2: "p_card-03-act-0_014",
    FINAL: "p_card-03-act-0_020",
}

_AUTHORITY_DESCRIPTION: Final = (
    "caller-authored 27-week route and server scenario facts; PC Master "
    "starter deck, card/effect/gimmick/score formulas and weekly fixed "
    "effects; caller-resolved SP choices, business detail rows, rolled reward "
    "cards, RNG roots, audition VoteBonusPermil, zero Star/Effect bonus, and "
    "complete NPC terminal tracks; audition post-result attribute/fan "
    "mutation remains server-owned and is not invented"
)


def _scenario_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _scenario_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _scenario_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _scenario_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class NiaFullAuditionRuntimeScenario:
    """Caller/server facts that are not derivable from static Master alone."""

    step_type: str
    vote_bonus_permille: int
    star_bonus_permille: int
    effect_bonus_permille: int
    schedule_random_state: int
    exam_random_state: int
    concentration_change_count: int
    npc_score_multiple_permille: int
    npc_multiplier_timing: NiaNpcMultiplierTimingResolution | None
    npc_final_scores: tuple[int, ...]
    reward_requested_by_rank: tuple[bool, ...]
    reward_offer: RewardOffer

    def __post_init__(self) -> None:
        if self.step_type not in (MID1, MID2, FINAL):
            raise ValueError("NIA audition runtime stage is unsupported")
        for field_name in (
            "vote_bonus_permille",
            "star_bonus_permille",
            "effect_bonus_permille",
            "concentration_change_count",
            "npc_score_multiple_permille",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        for field_name in ("schedule_random_state", "exam_random_state"):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
                raise ValueError(f"{field_name} must be UInt32")
        scores = tuple(self.npc_final_scores)
        if not scores or any(type(value) is not int or value < 0 for value in scores):
            raise ValueError("npc_final_scores must contain non-negative integers")
        rewards = tuple(self.reward_requested_by_rank)
        if not rewards or any(type(value) is not bool for value in rewards):
            raise ValueError("reward_requested_by_rank must contain booleans")
        if len(rewards) != len(scores) + 1:
            raise ValueError(
                "rank reward rule must cover player ranks 1 through NPC count + 1"
            )
        if not isinstance(self.reward_offer, RewardOffer):
            raise TypeError("reward_offer must be RewardOffer")
        if self.reward_offer.received_card_instance_id is None:
            raise ValueError("audition reward must preserve its response card GUID")
        object.__setattr__(self, "npc_final_scores", scores)
        object.__setattr__(self, "reward_requested_by_rank", rewards)

    def to_dict(self) -> dict[str, object]:
        timing = self.npc_multiplier_timing
        return {
            "step_type": self.step_type,
            "vote_bonus_permille": self.vote_bonus_permille,
            "star_bonus_permille": self.star_bonus_permille,
            "effect_bonus_permille": self.effect_bonus_permille,
            "schedule_random_state": self.schedule_random_state,
            "exam_random_state": self.exam_random_state,
            "concentration_change_count": self.concentration_change_count,
            "npc_score_multiple_permille": self.npc_score_multiple_permille,
            "npc_multiplier_timing": (
                None
                if timing is None
                else {
                    "authority_ref": timing.authority_ref,
                    "transition_turns": list(timing.transition_turns),
                    "terminal_tracks_include_transitions": (
                        timing.terminal_tracks_include_transitions
                    ),
                }
            ),
            "npc_final_scores": list(self.npc_final_scores),
            "reward_requested_by_rank": list(self.reward_requested_by_rank),
            "reward_offer": self.reward_offer.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "NiaFullAuditionRuntimeScenario":
        timing_raw = payload.get("npc_multiplier_timing")
        timing = None
        if timing_raw is not None:
            timing_row = _scenario_mapping(
                timing_raw,
                "audition.npc_multiplier_timing",
            )
            include = timing_row.get("terminal_tracks_include_transitions")
            if type(include) is not bool:
                raise ValueError(
                    "audition.npc_multiplier_timing."
                    "terminal_tracks_include_transitions must be bool"
                )
            timing = NiaNpcMultiplierTimingResolution(
                authority_ref=_scenario_text(
                    timing_row.get("authority_ref"),
                    "audition.npc_multiplier_timing.authority_ref",
                ),
                transition_turns=tuple(
                    _scenario_int(
                        value,
                        "audition.npc_multiplier_timing.transition_turn",
                        minimum=1,
                    )
                    for value in _scenario_list(
                        timing_row.get("transition_turns"),
                        "audition.npc_multiplier_timing.transition_turns",
                    )
                ),
                terminal_tracks_include_transitions=include,
            )
        rewards = _scenario_list(
            payload.get("reward_requested_by_rank"),
            "audition.reward_requested_by_rank",
        )
        if any(type(value) is not bool for value in rewards):
            raise ValueError("audition rank reward values must be bool")
        return cls(
            step_type=_scenario_text(
                payload.get("step_type"), "audition.step_type"
            ),
            vote_bonus_permille=_scenario_int(
                payload.get("vote_bonus_permille"),
                "audition.vote_bonus_permille",
            ),
            star_bonus_permille=_scenario_int(
                payload.get("star_bonus_permille"),
                "audition.star_bonus_permille",
            ),
            effect_bonus_permille=_scenario_int(
                payload.get("effect_bonus_permille"),
                "audition.effect_bonus_permille",
            ),
            schedule_random_state=_scenario_int(
                payload.get("schedule_random_state"),
                "audition.schedule_random_state",
            ),
            exam_random_state=_scenario_int(
                payload.get("exam_random_state"),
                "audition.exam_random_state",
            ),
            concentration_change_count=_scenario_int(
                payload.get("concentration_change_count"),
                "audition.concentration_change_count",
            ),
            npc_score_multiple_permille=_scenario_int(
                payload.get("npc_score_multiple_permille"),
                "audition.npc_score_multiple_permille",
            ),
            npc_multiplier_timing=timing,
            npc_final_scores=tuple(
                _scenario_int(value, "audition.npc_final_score")
                for value in _scenario_list(
                    payload.get("npc_final_scores"),
                    "audition.npc_final_scores",
                )
            ),
            reward_requested_by_rank=tuple(rewards),
            reward_offer=RewardOffer.from_dict(
                _scenario_mapping(
                    payload.get("reward_offer"),
                    "audition.reward_offer",
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaFullRouteScenario:
    """Replaceable 27-week route facts around the reusable simulator core."""

    initial_state: ProduceRolloutState
    weekly_actions: tuple[tuple[int, str], ...]
    self_lessons: tuple[tuple[int, NiaSelfLessonScenario], ...]
    businesses: tuple[tuple[int, NiaBusinessScenario], ...]
    auditions: tuple[NiaFullAuditionRuntimeScenario, ...]
    authority_ref: str
    care_packages: tuple[tuple[int, NiaCarePackageScenario], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.initial_state, ProduceRolloutState):
            raise TypeError("initial_state must be ProduceRolloutState")
        if (
            self.initial_state.mode_id != "produce-004"
            or self.initial_state.total_weeks != 27
            or self.initial_state.phase is not RolloutPhase.READY_FOR_WEEK
            or self.initial_state.lifecycle is not Lifecycle.IN_PROGRESS
            or self.initial_state.pending is not None
        ):
            raise ValueError(
                "NIA route initial state must be a ready produce-004 checkpoint"
            )
        if not self.authority_ref:
            raise ValueError("NIA route scenario authority_ref is required")
        for entry in self.initial_state.deck:
            if len(entry.instance_ids) != entry.count:
                raise ValueError("NIA route deck must preserve every card GUID")

        weekly = tuple(self.weekly_actions)
        self_rows = tuple(self.self_lessons)
        business_rows = tuple(self.businesses)
        care_package_rows = tuple(self.care_packages)
        auditions = tuple(self.auditions)
        for pairs, label in (
            (weekly, "weekly_actions"),
            (self_rows, "self_lessons"),
            (business_rows, "businesses"),
            (care_package_rows, "care_packages"),
        ):
            weeks = tuple(value[0] for value in pairs)
            if len(weeks) != len(set(weeks)):
                raise ValueError(f"{label} contains duplicate weeks")
        expected_weekly = set(range(1, 28)) - set(_STAGE_WEEK)
        if {week for week, _action in weekly} != expected_weekly:
            raise ValueError("weekly_actions must cover every non-audition week")
        supported = {
            SELF_LESSON,
            BUSINESS,
            EVENT_BUSINESS,
            INTERVAL,
            OUTING,
            REFRESH,
            SPECIAL_GUIDANCE,
            CARE_PACKAGE,
        }
        if any(action not in supported for _week, action in weekly):
            raise ValueError("weekly_actions contains an unsupported adapter")
        weekly_map = dict(weekly)
        if {week for week, _value in self_rows} != {
            week for week, action in weekly if action == SELF_LESSON
        }:
            raise ValueError("self lesson scenarios do not match the route")
        if {week for week, _value in business_rows} != {
            week for week, action in weekly if action == BUSINESS
        }:
            raise ValueError("business scenarios do not match the route")
        if {week for week, _value in care_package_rows} != {
            week for week, action in weekly if action == CARE_PACKAGE
        }:
            raise ValueError("care package scenarios do not match the route")
        if any(weekly_map[week] != SELF_LESSON for week, _value in self_rows):
            raise ValueError("self lesson scenario is bound to the wrong action")
        if any(weekly_map[week] != BUSINESS for week, _value in business_rows):
            raise ValueError("business scenario is bound to the wrong action")
        if any(
            weekly_map[week] != CARE_PACKAGE
            for week, _value in care_package_rows
        ):
            raise ValueError("care package scenario is bound to the wrong action")
        if any(
            value.reward_offer.received_card_instance_id is None
            for _week, value in business_rows
        ):
            raise ValueError("business rewards must preserve response card GUIDs")
        stages = tuple(value.step_type for value in auditions)
        if stages != (MID1, MID2, FINAL):
            raise ValueError("auditions must be ordered Mid1, Mid2, Final")
        object.__setattr__(self, "weekly_actions", weekly)
        object.__setattr__(self, "self_lessons", self_rows)
        object.__setattr__(self, "businesses", business_rows)
        object.__setattr__(self, "care_packages", care_package_rows)
        object.__setattr__(self, "auditions", auditions)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "gkms_tool.nia_full_route_scenario",
            "schema_version": 1,
            "authority_ref": self.authority_ref,
            "initial_state": self.initial_state.to_dict(),
            "weekly_actions": [
                {"week": week, "action": action}
                for week, action in self.weekly_actions
            ],
            "self_lessons": [
                {
                    "week": week,
                    "phase": scenario.phase,
                    "attribute": scenario.attribute,
                    "is_sp": scenario.is_sp,
                    "branch_id": scenario.branch_id,
                }
                for week, scenario in self.self_lessons
            ],
            "businesses": [
                {
                    "week": week,
                    "detail_id": scenario.detail_id,
                    "suggestion_id": scenario.suggestion_id,
                    "reward_offer": scenario.reward_offer.to_dict(),
                    "branch_id": scenario.branch_id,
                }
                for week, scenario in self.businesses
            ],
            "care_packages": [
                {
                    "week": week,
                    "branch_id": scenario.branch_id,
                    "present_scenario": scenario.present_scenario.to_dict(),
                }
                for week, scenario in self.care_packages
            ],
            "auditions": [value.to_dict() for value in self.auditions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaFullRouteScenario":
        if payload.get("schema") != "gkms_tool.nia_full_route_scenario":
            raise ValueError("NIA route scenario schema is unsupported")
        if payload.get("schema_version") != 1:
            raise ValueError("NIA route scenario version is unsupported")

        weekly_actions = []
        for raw in _scenario_list(
            payload.get("weekly_actions"), "weekly_actions"
        ):
            row = _scenario_mapping(raw, "weekly_action")
            weekly_actions.append(
                (
                    _scenario_int(row.get("week"), "weekly_action.week", minimum=1),
                    _scenario_text(row.get("action"), "weekly_action.action"),
                )
            )

        self_lessons = []
        for raw in _scenario_list(
            payload.get("self_lessons"), "self_lessons"
        ):
            row = _scenario_mapping(raw, "self_lesson")
            is_sp = row.get("is_sp")
            if type(is_sp) is not bool:
                raise ValueError("self_lesson.is_sp must be bool")
            self_lessons.append(
                (
                    _scenario_int(row.get("week"), "self_lesson.week", minimum=1),
                    NiaSelfLessonScenario(
                        _scenario_int(
                            row.get("phase"), "self_lesson.phase", minimum=1
                        ),
                        _scenario_text(
                            row.get("attribute"), "self_lesson.attribute"
                        ),
                        is_sp,
                        _scenario_text(
                            row.get("branch_id"), "self_lesson.branch_id"
                        ),
                    ),
                )
            )

        businesses = []
        for raw in _scenario_list(payload.get("businesses"), "businesses"):
            row = _scenario_mapping(raw, "business")
            businesses.append(
                (
                    _scenario_int(row.get("week"), "business.week", minimum=1),
                    NiaBusinessScenario(
                        _scenario_text(row.get("detail_id"), "business.detail_id"),
                        _scenario_text(
                            row.get("suggestion_id"), "business.suggestion_id"
                        ),
                        RewardOffer.from_dict(
                            _scenario_mapping(
                                row.get("reward_offer"),
                                "business.reward_offer",
                            )
                        ),
                        _scenario_text(row.get("branch_id"), "business.branch_id"),
                    ),
                )
            )

        care_packages = []
        for raw in _scenario_list(
            payload.get("care_packages", []), "care_packages"
        ):
            row = _scenario_mapping(raw, "care_package")
            care_packages.append(
                (
                    _scenario_int(
                        row.get("week"), "care_package.week", minimum=1
                    ),
                    NiaCarePackageScenario(
                        InitialRegularSupplyScenario.from_dict(
                            _scenario_mapping(
                                row.get("present_scenario"),
                                "care_package.present_scenario",
                            )
                        ),
                        _scenario_text(
                            row.get("branch_id"), "care_package.branch_id"
                        ),
                    ),
                )
            )

        return cls(
            initial_state=ProduceRolloutState.from_dict(
                _scenario_mapping(payload.get("initial_state"), "initial_state")
            ),
            weekly_actions=tuple(weekly_actions),
            self_lessons=tuple(self_lessons),
            businesses=tuple(businesses),
            auditions=tuple(
                NiaFullAuditionRuntimeScenario.from_dict(
                    _scenario_mapping(value, "audition")
                )
                for value in _scenario_list(
                    payload.get("auditions"), "auditions"
                )
            ),
            authority_ref=_scenario_text(
                payload.get("authority_ref"), "authority_ref"
            ),
            care_packages=tuple(care_packages),
        )


@dataclass(frozen=True, slots=True)
class NiaFullStageRecord:
    week: int
    catalog: NiaStaticAuditionStage
    acceptance: NiaInnerTerminalAcceptance
    resolution: NiaAuditionTerminalResolution
    outcome: InitialRegularInnerStageOutcome
    base_bonus_permille: tuple[int, int, int]
    vote_bonus_permille: int
    star_bonus_permille: int
    effect_bonus_permille: int
    final_bonus_permille: tuple[int, int, int]

    def __post_init__(self) -> None:
        if self.week not in _STAGE_WEEK:
            raise ValueError("NIA stage week is invalid")
        if self.catalog.step_type != _STAGE_WEEK[self.week]:
            raise ValueError("NIA stage record does not match its week")


@dataclass(frozen=True, slots=True)
class NiaFullFixedScenarioResult:
    run: InitialRegularFixedRunResult
    report: InitialRegularFixedRunReport
    weekly_actions: tuple[NiaResolvedWeeklyAction, ...]
    stages: tuple[NiaFullStageRecord, ...]
    vote_count_delta_total: int
    scenario_label: str = NIA_FULL_FIXED_SCENARIO_LABEL
    authority_description: str = _AUTHORITY_DESCRIPTION
    authentic_nia_local_save: bool = False
    scenario: NiaFullRouteScenario | None = None

    def __post_init__(self) -> None:
        if self.report.stop is not self.run.stop:
            raise ValueError("NIA fixed report stop must match its run")
        if self.report.completed != self.run.completed:
            raise ValueError("NIA fixed report completion must match its run")
        if not self.run.completed:
            if (
                self.run.stop is not InitialRegularFixedRunStop.EXTERNAL_BLOCKED
                or not self.run.issues
                or self.report.completed
            ):
                raise ValueError(
                    "partial NIA fixed scenario must be a typed external stop"
                )
            if self.run.final_state.lifecycle is Lifecycle.COMPLETED:
                raise ValueError("blocked NIA fixed scenario cannot be completed")
            if self.scenario is not None and (
                self.run.initial_state != self.scenario.initial_state
            ):
                raise ValueError(
                    "run initial state does not match its route scenario"
                )
            return
        if self.run.stop is not InitialRegularFixedRunStop.TERMINAL:
            raise ValueError("full NIA fixed scenario must reach terminal")
        if self.run.issues or not self.report.completed:
            raise ValueError("full NIA fixed scenario must complete without issues")
        if self.run.final_state.lifecycle is not Lifecycle.COMPLETED:
            raise ValueError("full NIA fixed scenario lifecycle must be completed")
        expected_stages = tuple(
            week
            for week in sorted(_STAGE_WEEK)
            if week >= self.run.initial_state.week
        )
        if tuple(record.week for record in self.stages) != expected_stages:
            raise ValueError(
                "NIA fixed scenario must resolve every remaining milestone"
            )
        if self.vote_count_delta_total != sum(
            value.vote_count_delta for value in self.weekly_actions
        ):
            raise ValueError("vote count total does not match weekly actions")
        if self.scenario is not None and (
            self.run.initial_state != self.scenario.initial_state
        ):
            raise ValueError("run initial state does not match its route scenario")

    @property
    def completed(self) -> bool:
        return self.run.completed

    @property
    def start_week(self) -> int:
        return self.run.initial_state.week


def _split_score(score: int, turns: int) -> tuple[int, ...]:
    quotient, remainder = divmod(score, turns)
    return tuple(
        quotient + (1 if index < remainder else 0)
        for index in range(turns)
    )


def _business_scenario(week: int) -> NiaBusinessScenario:
    phase, detail_suffix = _BUSINESS_PHASE[week]
    detail_id = (
        "event-detail-business-produce_004-plan3-stamina-"
        f"{phase}-{detail_suffix}"
    )
    suggestion_id = (
        "p_s_e_s-event-detail-business-produce_004-"
        f"{phase}-stamina-plan3-visual-concentration"
    )
    return NiaBusinessScenario(
        detail_id,
        suggestion_id,
        RewardOffer(
            f"nia-full:w{week}:business-reward",
            "p_card-03-men-0_015",
            0,
            f"nia-full:w{week}:business-reward-guid",
        ),
        f"nia-full:w{week}:business",
    )


def _initial_state(
    inventory: NiaStaticInventory,
    *,
    database: Path,
) -> ProduceRolloutState:
    profile = get_idol_profile(inventory.idol_card_id, database)
    if profile is None:
        raise RuntimeError("NIA full fixed IdolCard is absent")
    fixture = build_nia_produce004_fktn_starter_terminal_fixture(
        database=database,
    )
    return ProduceRolloutState(
        mode_id=inventory.produce_id,
        character_id=profile.character_id,
        week=1,
        total_weeks=inventory.total_weeks,
        phase=RolloutPhase.READY_FOR_WEEK,
        stamina=profile.stamina,
        max_stamina=profile.stamina,
        attributes=AttributeValues(profile.vocal, profile.dance, profile.visual),
        deck=project_nia_fixture_outer_deck(fixture),
        produce_points=inventory.setting.initial_produce_point,
    )


@lru_cache(maxsize=4)
def _cached_nia_full_inventory(
    database: Path,
    master_dir: Path,
) -> NiaStaticInventory:
    """Reuse one immutable static projection inside the GUI worker process."""

    return build_nia_static_inventory(
        "i_card-fktn-3-011",
        produce_id="produce-004",
        master_dir=master_dir,
        database=database,
    )


def build_nia_produce004_fktn_default_full_scenario(
    inventory: NiaStaticInventory | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaFullRouteScenario:
    """Build the checked-in deterministic caller-scenario envelope.

    The returned object is intentionally separate from the simulator.  A
    LocalSave/response adapter can replace these exact inputs later without
    changing card execution, stage search, or the outer kernel.
    """

    database = Path(database)
    master_dir = Path(master_dir)
    resolved_inventory = inventory or _cached_nia_full_inventory(
        database,
        master_dir,
    )
    initial = _initial_state(resolved_inventory, database=database)
    self_lessons = tuple(
        (
            week,
            NiaSelfLessonScenario(
                _SELF_PHASE[week],
                "visual",
                True,
                f"nia-full:w{week}:self-lesson-sp-visual",
            ),
        )
        for week, action in sorted(_WEEK_ACTION.items())
        if action == SELF_LESSON
    )
    businesses = tuple(
        (week, _business_scenario(week))
        for week, action in sorted(_WEEK_ACTION.items())
        if action == BUSINESS
    )
    auditions = []
    for week, step_type in sorted(_STAGE_WEEK.items()):
        catalog = _stage_catalog(resolved_inventory, step_type)
        auditions.append(
            NiaFullAuditionRuntimeScenario(
                step_type=step_type,
                vote_bonus_permille=_RUNTIME_VOTE_BONUS_PERMILLE[step_type],
                star_bonus_permille=0,
                effect_bonus_permille=0,
                schedule_random_state=0x12000000 + week,
                exam_random_state=0x24000000 + week,
                concentration_change_count=2,
                npc_score_multiple_permille=0,
                npc_multiplier_timing=None,
                npc_final_scores=tuple(
                    value.score_min for value in catalog.npc_scores
                ),
                reward_requested_by_rank=(
                    True,
                    *(False for _value in catalog.npc_scores),
                ),
                reward_offer=RewardOffer(
                    f"nia-full:w{week}:{step_type}:reward",
                    _EXAM_REWARD_CARD[step_type],
                    0,
                    f"nia-full:w{week}:{step_type}:reward-guid",
                ),
            )
        )
    return NiaFullRouteScenario(
        initial_state=initial,
        weekly_actions=tuple(sorted(_WEEK_ACTION.items())),
        self_lessons=self_lessons,
        businesses=businesses,
        auditions=tuple(auditions),
        authority_ref=NIA_FULL_FIXED_SCENARIO_LABEL,
    )


def _stage_catalog(
    inventory: NiaStaticInventory,
    step_type: str,
) -> NiaStaticAuditionStage:
    matches = tuple(
        value
        for value in inventory.auditions
        if value.step_type == step_type and value.number == 1
    )
    if len(matches) != 1:
        raise RuntimeError(f"NIA stage catalog is ambiguous: {step_type}")
    return matches[0]


def _battle_bonus(
    state: ProduceRolloutState,
    inventory: NiaStaticInventory,
    catalog: NiaStaticAuditionStage,
    runtime: NiaFullAuditionRuntimeScenario,
) -> tuple[tuple[int, int, int], int, tuple[int, int, int]]:
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
        configured_parameters=(
            catalog.vocal_parameter,
            catalog.dance_parameter,
            catalog.visual_parameter,
        ),
        penalty_min_permille=inventory.score_penalty_min_permille,
        penalty_max_permille=inventory.score_penalty_max_permille,
    )
    vote = runtime.vote_bonus_permille
    final = calculate_audition_bonus_permils(
        AuditionProgressBonusValues(
            vocal_permil=base[0],
            dance_permil=base[1],
            visual_permil=base[2],
            vote_bonus_permil=vote,
            star_bonus_permil=runtime.star_bonus_permille,
            audition_effect_parameter_bonus_permil=(
                runtime.effect_bonus_permille
            ),
        )
    )
    return base, vote, (final.vocal, final.dance, final.visual)


def _npc_scenario(
    catalog: NiaStaticAuditionStage,
    battle_bonus: tuple[int, int, int],
    runtime: NiaFullAuditionRuntimeScenario,
) -> NiaAuditionTerminalScenario:
    tracks: list[NiaNpcTerminalTrack] = []
    if len(runtime.npc_final_scores) != len(catalog.npc_scores):
        raise RuntimeError("NIA runtime NPC track count does not match Master")
    for npc, score in zip(
        catalog.npc_scores,
        runtime.npc_final_scores,
        strict=True,
    ):
        vocal = score * npc.vocal_permille // 1000
        dance = score * npc.dance_permille // 1000
        tracks.append(
            NiaNpcTerminalTrack(
                npc_group_id=catalog.npc_group_id,
                number=npc.number,
                mob_id=npc.mob_id,
                turn_scores=_split_score(score, catalog.turns),
                final_score=score,
                vocal_score=vocal,
                dance_score=dance,
                visual_score=score - vocal - dance,
            )
        )
    return NiaAuditionTerminalScenario(
        stage_type=catalog.step_type,
        number=catalog.number,
        npc_group_id=catalog.npc_group_id,
        authority=NiaNpcTerminalTrackAuthority.CALLER_RESOLVED,
        is_static_npc_score=catalog.is_static_npc_score,
        npc_score_multiple_permille=runtime.npc_score_multiple_permille,
        npc_multiplier_timing=runtime.npc_multiplier_timing,
        battle_bonus_permille=battle_bonus,
        gimmick_group_id=catalog.gimmick_group_id,
        turn_parameter_types=(),  # replaced after the player schedule is replayed
        npc_tracks=tuple(tracks),
        reward_rule=NiaAuditionRewardRule(
            catalog.step_type,
            catalog.number,
            runtime.reward_requested_by_rank,
        ),
    )


def _run_stage(
    state: ProduceRolloutState,
    request: ExternalRequest,
    inventory: NiaStaticInventory,
    runtime_scenario: NiaFullAuditionRuntimeScenario,
    *,
    database: Path,
    master_dir: Path,
) -> tuple[NiaFullStageRecord, InnerExamOutcome]:
    step_type = _STAGE_WEEK[state.week]
    catalog = _stage_catalog(inventory, step_type)
    if runtime_scenario.step_type != step_type:
        raise RuntimeError("NIA stage/runtime scenario identity mismatch")
    base_bonus, vote_bonus, final_bonus = _battle_bonus(
        state,
        inventory,
        catalog,
        runtime_scenario,
    )
    runtime = NiaCallerOwnedInnerRuntime(
        schedule_random_state=runtime_scenario.schedule_random_state,
        exam_random_state=runtime_scenario.exam_random_state,
        stamina=state.stamina,
        max_stamina=state.max_stamina,
        concentration_change_count=runtime_scenario.concentration_change_count,
        battle_bonus_permille_vocal=final_bonus[0],
        battle_bonus_permille_dance=final_bonus[1],
        battle_bonus_permille_visual=final_bonus[2],
    )
    fixture = build_nia_terminal_fixture_from_outer_deck(
        state.deck,
        selector=NiaInnerMasterProfileSelector(
            idol_card_id=inventory.idol_card_id,
            produce_id=inventory.produce_id,
            step_type=step_type,
            audition_number=1,
        ),
        runtime=runtime,
        beam_width=8,
        database=database,
    )
    acceptance = run_nia_terminal_acceptance(
        fixture,
        database=database,
        master_dir=master_dir,
    )
    if not acceptance.player_inner_terminal:
        raise RuntimeError(
            f"NIA player stage did not terminate: {step_type}:{acceptance.issues}"
        )
    exact_catalog = load_nia_audition_stage_for_acceptance(
        acceptance,
        master_dir=master_dir,
        database=database,
    )
    scenario = replace(
        _npc_scenario(exact_catalog, final_bonus, runtime_scenario),
        turn_parameter_types=acceptance.battle_parameter_schedule,
    )
    adapter = InitialRegularNiaInnerAdapter(
        acceptance,
        scenario,
        exact_catalog,
        master_dir,
        database,
    )
    resolved = adapter.resolve()
    if not resolved.ready or resolved.resolution is None:
        raise RuntimeError(f"NIA terminal rank is unresolved: {resolved.issues}")
    branch = ChanceBranch(
        f"nia-full:w{state.week}:inner",
        probability=1.0,
        rng_token=f"caller:nia-full:w{state.week}:inner-rng",
    )
    common_request = InitialRegularInnerStageRequest(
        external_request_id=request.request_id,
        week=state.week,
        action_id=request.action_id,
        stage_kind=InitialRegularInnerStageKind.AUDITION,
        plan_type=NIA_PLAN_TYPE,
        character_id=state.character_id,
        outer_state=state,
        branch=branch,
        idol_card_id=inventory.idol_card_id,
        stage_type=exact_catalog.step_type,
        stage_id=exact_catalog.stage_id,
        setting_id=exact_catalog.exam_setting_id,
        exam_type=1,
        step_type_value=_STEP_TYPE_VALUE[step_type],
        limit_turn=exact_catalog.turns,
        extra_turn=0,
        clear_border=exact_catalog.clear_border,
        limit_border=(
            exact_catalog.limit_border
            if exact_catalog.limit_border > 0
            else -1
        ),
        rng_before=acceptance.initial_native_state.random_state,
        battle_bonus_permille=final_bonus,
        gimmick_group_id=exact_catalog.gimmick_group_id,
        turn_parameter_types=acceptance.battle_parameter_schedule,
    )
    common = adapter(common_request)
    if not isinstance(common, InitialRegularInnerStageOutcome):
        raise RuntimeError(f"NIA common inner adapter blocked: {common}")
    outer = project_initial_regular_inner_stage_outcome(common_request, common)
    if not isinstance(outer, InnerExamOutcome):
        raise AssertionError("NIA audition must project to InnerExamOutcome")
    record = NiaFullStageRecord(
        state.week,
        exact_catalog,
        acceptance,
        resolved.resolution,
        common,
        base_bonus,
        vote_bonus,
        runtime_scenario.star_bonus_permille,
        runtime_scenario.effect_bonus_permille,
        final_bonus,
    )
    return record, outer


def _typed_response_overlay(
    values: Mapping[int, _ResponseScenarioT] | None,
    *,
    field: str,
    scenario_type: type[_ResponseScenarioT],
) -> dict[int, _ResponseScenarioT]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError(f"{field} must be a mapping")
    resolved = dict(values)
    for week, scenario in resolved.items():
        if type(week) is not int:
            raise TypeError(f"{field} keys must be integer weeks")
        if not isinstance(scenario, scenario_type):
            raise TypeError(f"{field} values must be typed scenarios")
    return resolved


def _response_overlay_route_issues(
    weekly_action_by_week: Mapping[int, str],
    *,
    action: str,
    field: str,
    scenarios: Mapping[int, object],
    scenario_has_week: bool = True,
    code_action: str | None = None,
) -> tuple[OuterSearchIssue, ...]:
    route_weeks = {
        week
        for week, route_action in weekly_action_by_week.items()
        if route_action == action
    }
    issues: list[OuterSearchIssue] = []
    issue_action = code_action or action.replace("_", "-")
    for week, scenario in sorted(scenarios.items()):
        if week not in route_weeks:
            issues.append(
                OuterSearchIssue(
                    f"nia-{issue_action}-response-scenario-extra",
                    field,
                    (
                        f"week={week}; typed response supplied for a "
                        f"non-{action} route week"
                    ),
                )
            )
        elif scenario_has_week and getattr(scenario, "week") != week:
            issues.append(
                OuterSearchIssue(
                    f"nia-{issue_action}-response-week-mismatch",
                    f"{field}[week].week",
                    f"route={week};scenario={getattr(scenario, 'week')}",
                )
            )
    return tuple(issues)


def _blocked_full_result(
    route: NiaFullRouteScenario,
    issues: tuple[OuterSearchIssue, ...],
) -> NiaFullFixedScenarioResult:
    run = InitialRegularFixedRunResult(
        route.initial_state,
        route.initial_state,
        InitialRegularFixedRunStop.EXTERNAL_BLOCKED,
        issues,
        (),
    )
    return NiaFullFixedScenarioResult(
        run,
        build_initial_regular_fixed_run_report(run),
        (),
        (),
        0,
        scenario=route,
    )


def run_nia_produce004_fktn_full_fixed_scenario(
    *,
    scenario: NiaFullRouteScenario | None = None,
    self_lesson_response_scenarios: Mapping[
        int,
        InitialRegularNiaSelfLessonScenario,
    ]
    | None = None,
    business_scenarios: Mapping[
        int,
        InitialRegularNiaBusinessScenario,
    ]
    | None = None,
    customize_scenarios: Mapping[
        int,
        InitialRegularNiaCustomizeScenario,
    ]
    | None = None,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
    interval_scenarios: Mapping[
        int,
        InitialRegularNiaIntervalScenario,
    ]
    | None = None,
    event_business_scenarios: Mapping[
        int,
        InitialRegularNiaEventBusinessScenario,
    ]
    | None = None,
    refresh_scenarios: Mapping[
        int,
        InitialRegularNiaRefreshScenario,
    ]
    | None = None,
    fan_present_scenarios: Mapping[
        int,
        InitialRegularSupplyScenario,
    ]
    | None = None,
    strict_runtime_response_bundle: bool = False,
) -> NiaFullFixedScenarioResult:
    """Run one typed FKTN N.I.A. Pro checkpoint through Final.

    ``self_lesson_response_scenarios`` is an out-of-schema runtime overlay for
    captured Start/End responses.  By default, omitting a week preserves the
    version-1 JSON route selector and bridges it to an explicit synthetic
    Master-only response at execution time.  A caller loading one strict
    response bundle sets ``strict_runtime_response_bundle`` so every selected
    SelfLesson, Business, Customize, or FanPresent week must have a matching
    bundle entry.

    Business, Interval, EventBusiness, and Refresh responses are trailing
    out-of-v1 overlays.  Business exact responses use Android's dedicated
    Start/Select lifecycle; omitting them preserves the legacy synthetic v1
    detail/effect/reward flow.  FanPresent may likewise override the Present lifecycle
    embedded in a custom version-1 route; omitting the entire map preserves
    that embedded lifecycle.  A custom route selecting one of the required
    actions must supply the matching week's exact typed response; unused
    entries stop with a typed issue rather than being silently ignored.
    """

    database = Path(database)
    master_dir = Path(master_dir)
    if type(strict_runtime_response_bundle) is not bool:
        raise TypeError("strict_runtime_response_bundle must be bool")
    inventory = _cached_nia_full_inventory(database, master_dir)
    route = scenario or build_nia_produce004_fktn_default_full_scenario(
        inventory,
        database=database,
        master_dir=master_dir,
    )
    weekly_action_by_week = dict(route.weekly_actions)
    self_lesson_by_week = dict(route.self_lessons)
    require_self_lesson_response = strict_runtime_response_bundle
    require_business_response = strict_runtime_response_bundle
    require_customize_response = strict_runtime_response_bundle
    require_fan_present_response = strict_runtime_response_bundle
    self_lesson_response_by_week = _typed_response_overlay(
        self_lesson_response_scenarios,
        field="self_lesson_response_scenarios",
        scenario_type=InitialRegularNiaSelfLessonScenario,
    )
    business_response_by_week = _typed_response_overlay(
        business_scenarios,
        field="business_scenarios",
        scenario_type=InitialRegularNiaBusinessScenario,
    )
    customize_response_by_week = _typed_response_overlay(
        customize_scenarios,
        field="customize_scenarios",
        scenario_type=InitialRegularNiaCustomizeScenario,
    )
    interval_by_week = _typed_response_overlay(
        interval_scenarios,
        field="interval_scenarios",
        scenario_type=InitialRegularNiaIntervalScenario,
    )
    event_business_by_week = _typed_response_overlay(
        event_business_scenarios,
        field="event_business_scenarios",
        scenario_type=InitialRegularNiaEventBusinessScenario,
    )
    refresh_by_week = _typed_response_overlay(
        refresh_scenarios,
        field="refresh_scenarios",
        scenario_type=InitialRegularNiaRefreshScenario,
    )
    fan_present_by_week = _typed_response_overlay(
        fan_present_scenarios,
        field="fan_present_scenarios",
        scenario_type=InitialRegularSupplyScenario,
    )
    overlay_inputs: list[tuple[str, str, Mapping[int, object]]] = [
        (INTERVAL, "interval_scenarios", interval_by_week),
        (
            EVENT_BUSINESS,
            "event_business_scenarios",
            event_business_by_week,
        ),
        (REFRESH, "refresh_scenarios", refresh_by_week),
    ]
    if require_business_response or business_response_by_week:
        overlay_inputs.insert(
            0,
            (BUSINESS, "business_scenarios", business_response_by_week),
        )
    if require_customize_response or customize_response_by_week:
        overlay_inputs.insert(
            0,
            (
                SPECIAL_GUIDANCE,
                "customize_scenarios",
                customize_response_by_week,
            ),
        )
    if require_self_lesson_response:
        overlay_inputs.insert(
            0,
            (
                SELF_LESSON,
                "self_lesson_response_scenarios",
                self_lesson_response_by_week,
            ),
        )
    if require_fan_present_response:
        overlay_inputs.append(
            (
                CARE_PACKAGE,
                "fan_present_scenarios",
                fan_present_by_week,
            )
        )
    overlay_issues = tuple(
        issue
        for action, field, values in overlay_inputs
        for issue in _response_overlay_route_issues(
            weekly_action_by_week,
            action=action,
            field=field,
            scenarios=values,
            scenario_has_week=action != CARE_PACKAGE,
            code_action="fan-present" if action == CARE_PACKAGE else None,
        )
    )
    if overlay_issues:
        return _blocked_full_result(route, overlay_issues)

    # The checked-in v1 calendar intentionally remains unchanged.  Only an
    # explicit custom route may expose these out-of-v1 server response steps.
    response_action_by_week = {
        week: action
        for week, action in weekly_action_by_week.items()
        if action in {INTERVAL, EVENT_BUSINESS, REFRESH}
    }
    calendar = load_route_calendar("produce-004", master_dir=master_dir)
    if response_action_by_week:
        calendar = replace(
            calendar,
            weeks=tuple(
                replace(
                    value,
                    actions=(response_action_by_week[value.week],),
                    exact_actions=True,
                )
                if value.week in response_action_by_week
                else value
                for value in calendar.weeks
            ),
            detail_scope=(
                f"{calendar.detail_scope}; caller-authored out-of-v1 "
                "response actions"
            ),
        )
    kernel = ProduceRolloutKernel(
        calendar,
        rest_recovery_permille=inventory.setting.refresh_stamina_recovery_permille,
        inner_exam_adapter_ref=(
            "gkms_tool.initial_regular_nia_inner_adapter."
            "InitialRegularNiaInnerAdapter"
        ),
    )
    business_by_week = dict(route.businesses)
    care_package_by_week = dict(route.care_packages)
    audition_by_stage = {
        value.step_type: value for value in route.auditions
    }
    initial_state = route.initial_state
    weekly_records: list[NiaResolvedWeeklyAction] = []
    stage_records: list[NiaFullStageRecord] = []

    def chance_provider(
        state: ProduceRolloutState,
        request: ExternalRequest,
    ) -> ExternalChanceExpansion:
        try:
            if request.kind is ExternalKind.WEEKLY_ACTION_OUTCOME:
                action = request.action_id
                if action == SELF_LESSON:
                    response_scenario = self_lesson_response_by_week.get(
                        state.week
                    )
                    if require_self_lesson_response and response_scenario is None:
                        return ExternalChanceExpansion.paused(
                            OuterSearchIssue(
                                "nia-self-lesson-response-scenario-missing",
                                "self_lesson_response_scenarios",
                                (
                                    f"week={state.week}; exact SelfLesson "
                                    "Start/End response and post-state required"
                                ),
                            )
                        )
                    resolution = resolve_nia_self_lesson(
                        state,
                        request,
                        self_lesson_by_week[state.week],
                        inventory,
                        response_scenario=response_scenario,
                    )
                elif action == BUSINESS:
                    response_scenario = business_response_by_week.get(
                        state.week
                    )
                    if require_business_response and response_scenario is None:
                        return ExternalChanceExpansion.paused(
                            OuterSearchIssue(
                                "nia-business-response-scenario-missing",
                                "business_scenarios",
                                (
                                    f"week={state.week}; exact Business "
                                    "Start/Select responses and post-state required"
                                ),
                            )
                        )
                    resolution = resolve_nia_business(
                        state,
                        request,
                        response_scenario or business_by_week[state.week],
                        inventory,
                        database=database,
                    )
                elif action == CARE_PACKAGE:
                    present_scenario = fan_present_by_week.get(state.week)
                    if (
                        require_fan_present_response
                        and present_scenario is None
                    ):
                        return ExternalChanceExpansion.paused(
                            OuterSearchIssue(
                                "nia-fan-present-response-scenario-missing",
                                "fan_present_scenarios",
                                (
                                    f"week={state.week}; exact "
                                    "UserProduceProgressPresent, PresentReceive/"
                                    "PresentEnd responses, and post-state required"
                                ),
                            )
                        )
                    care_package_scenario = care_package_by_week[state.week]
                    if present_scenario is not None:
                        care_package_scenario = replace(
                            care_package_scenario,
                            present_scenario=present_scenario,
                        )
                    resolution = resolve_nia_care_package(
                        state,
                        request,
                        care_package_scenario,
                    )
                elif action == INTERVAL:
                    response_scenario = interval_by_week.get(state.week)
                    if response_scenario is None:
                        return ExternalChanceExpansion.paused(
                            OuterSearchIssue(
                                "nia-interval-response-scenario-missing",
                                "interval_scenarios",
                                (
                                    f"week={state.week}; exact Start/Buy/"
                                    "Reroll/End response and post-state required"
                                ),
                            )
                        )
                    resolution = resolve_nia_interval(
                        state,
                        request,
                        response_scenario,
                    )
                elif action == EVENT_BUSINESS:
                    response_scenario = event_business_by_week.get(state.week)
                    if response_scenario is None:
                        return ExternalChanceExpansion.paused(
                            OuterSearchIssue(
                                "nia-event-business-response-scenario-missing",
                                "event_business_scenarios",
                                (
                                    f"week={state.week}; exact event response "
                                    "identity, order, and post-state required"
                                ),
                            )
                        )
                    resolution = resolve_nia_event_business(
                        state,
                        request,
                        response_scenario,
                        database=database,
                    )
                elif action == REFRESH:
                    response_scenario = refresh_by_week.get(state.week)
                    if response_scenario is None:
                        return ExternalChanceExpansion.paused(
                            OuterSearchIssue(
                                "nia-refresh-response-scenario-missing",
                                "refresh_scenarios",
                                (
                                    f"week={state.week}; exact Refresh response, "
                                    "schedule delta, and post-state required"
                                ),
                            )
                        )
                    resolution = resolve_nia_refresh(
                        state,
                        request,
                        response_scenario,
                    )
                elif action == OUTING:
                    resolution = resolve_nia_outing(
                        state,
                        request,
                        inventory,
                        branch_id=f"nia-full:w{state.week}:outing",
                    )
                elif action == SPECIAL_GUIDANCE:
                    response_scenario = customize_response_by_week.get(
                        state.week
                    )
                    if require_customize_response and response_scenario is None:
                        return ExternalChanceExpansion.paused(
                            OuterSearchIssue(
                                "nia-customize-response-scenario-missing",
                                "customize_scenarios",
                                (
                                    f"week={state.week}; exact Customize "
                                    "Start/End responses and zero Selects required"
                                ),
                            )
                        )
                    resolution = resolve_nia_special_guidance_skip(
                        state,
                        request,
                        inventory,
                        branch_id=f"nia-full:w{state.week}:customize-end",
                        response_scenario=response_scenario,
                    )
                else:
                    raise RuntimeError(f"unhandled NIA weekly action: {action}")
                weekly_records.append(resolution)
                outcome = resolution.outcome
            elif request.kind is ExternalKind.INNER_EXAM_OUTCOME:
                record, outcome = _run_stage(
                    state,
                    request,
                    inventory,
                    audition_by_stage[request.stage_type or ""],
                    database=database,
                    master_dir=master_dir,
                )
                stage_records.append(record)
            elif request.kind is ExternalKind.REWARD_OFFERS:
                context = state.reward_context
                if context is None:
                    raise RuntimeError("NIA reward request has no context")
                if context.source_action_id == BUSINESS:
                    offer = business_by_week[state.week].reward_offer
                elif context.source_action_id in audition_by_stage:
                    stage = context.source_action_id
                    offer = audition_by_stage[stage].reward_offer
                else:
                    raise RuntimeError(
                        "unhandled NIA reward source: "
                        f"{context.source_action_id}"
                    )
                outcome = RewardOffersOutcome(
                    request.request_id,
                    ChanceBranch(
                        f"nia-full:w{state.week}:reward-offers",
                        probability=1.0,
                        rng_token=f"caller:nia-full:w{state.week}:reward-roll",
                    ),
                    (offer,),
                    remaining_exclude_count=0,
                )
            else:
                return ExternalChanceExpansion.paused(
                    OuterSearchIssue(
                        "nia-full-external-kind-unhandled",
                        "request.kind",
                        request.kind.value,
                    )
                )
        except (KeyError, RuntimeError, TypeError, ValueError) as error:
            return ExternalChanceExpansion.paused(
                OuterSearchIssue(
                    "nia-full-scenario-blocked",
                    "chance_provider",
                    f"{type(error).__name__}:{error}",
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
                (value for value in legal if value.kind is ActionKind.SELECT_REWARD),
                None,
            )
        stage = _STAGE_WEEK.get(state.week)
        if stage is not None:
            desired = ProduceAction.delegate_exam(stage)
        else:
            action = weekly_action_by_week.get(state.week)
            if action is None:
                return None
            desired = ProduceAction.weekly(action)
        return desired if desired in legal else None

    run = run_initial_regular_fixed_scenario(
        kernel,
        initial_state,
        policy=policy,
        chance_provider=chance_provider,
        max_transitions=160,
    )
    if not run.completed:
        typed_missing_codes = {
            "nia-self-lesson-response-scenario-missing",
            "nia-business-response-scenario-missing",
            "nia-customize-response-scenario-missing",
            "nia-interval-response-scenario-missing",
            "nia-event-business-response-scenario-missing",
            "nia-refresh-response-scenario-missing",
        }
        if (
            run.stop is not InitialRegularFixedRunStop.EXTERNAL_BLOCKED
            or not run.issues
            or any(value.code not in typed_missing_codes for value in run.issues)
        ):
            raise RuntimeError(
                "NIA full fixed scenario did not complete: "
                f"{run.stop.value}:{run.issues}"
            )
    report = build_initial_regular_fixed_run_report(run)
    return NiaFullFixedScenarioResult(
        run,
        report,
        tuple(weekly_records),
        tuple(stage_records),
        sum(value.vote_count_delta for value in weekly_records),
        scenario=route,
    )


def load_nia_full_route_scenario(path: str | Path) -> NiaFullRouteScenario:
    """Load a versioned scenario JSON without inferring omitted facts."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return NiaFullRouteScenario.from_dict(
        _scenario_mapping(payload, "NIA route scenario")
    )


def save_nia_full_route_scenario(
    scenario: NiaFullRouteScenario,
    path: str | Path,
) -> Path:
    """Persist one explicit scenario for GUI/offline reuse."""

    if not isinstance(scenario, NiaFullRouteScenario):
        raise TypeError("scenario must be NiaFullRouteScenario")
    destination = Path(path)
    destination.write_text(
        json.dumps(scenario.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


run_nia_full_fixed_scenario = run_nia_produce004_fktn_full_fixed_scenario


__all__ = [
    "NIA_FULL_FIXED_SCENARIO_LABEL",
    "NiaFullAuditionRuntimeScenario",
    "NiaFullFixedScenarioResult",
    "NiaFullRouteScenario",
    "NiaFullStageRecord",
    "build_nia_produce004_fktn_default_full_scenario",
    "load_nia_full_route_scenario",
    "run_nia_full_fixed_scenario",
    "run_nia_produce004_fktn_full_fixed_scenario",
    "save_nia_full_route_scenario",
]
