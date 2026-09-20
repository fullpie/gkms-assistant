"""Caller-authoritative Plan 1 audition terminal/rank adapter.

PC Master supplies the stage/NPC-group/score-curve bounds through the existing
catalog loader.  The caller supplies the already-resolved NPC terminal scores;
this module never rolls NPC RNG.  Android's proven rank predicate is strict
``npc_score > player_score`` (ties stay below the player), followed by the
explicit rank reward rule.  Missing battle-bonus or gimmick runtime rows are
returned as field-specific typed blockers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Final

from .audition_rules import DEFAULT_MASTER_DIR
from .initial_regular_plan2_audition_runtime import (
    InitialRegularPlan2AuditionNpcCatalog,
    load_initial_regular_plan2_audition_npc_catalog,
)
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageKind,
    InitialRegularInnerStageRequest,
    InitialRegularInnerStageIssue,
)


INITIAL_REGULAR_PLAN1_AUDITION_ADAPTER_ID: Final = (
    "initial-regular.plan1.audition-terminal"
)


def _issue(code: str, field: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


class InitialRegularPlan1NpcScenarioAuthority(StrEnum):
    SERVER_RUNTIME = "server_runtime"
    MASTER_STATIC = "master_static"


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1NpcTerminalScore:
    npc_group_id: str
    number: int
    final_score: int

    def __post_init__(self) -> None:
        if not self.npc_group_id:
            raise ValueError("npc_group_id must be non-empty")
        if type(self.number) is not int or self.number < 1:
            raise ValueError("number must be positive")
        if type(self.final_score) is not int or self.final_score < 0:
            raise ValueError("final_score must be non-negative")


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1AuditionRewardRule:
    stage_type: str
    number: int
    reward_requested_by_rank: tuple[bool, ...]

    def __post_init__(self) -> None:
        if not self.stage_type:
            raise ValueError("stage_type must be non-empty")
        if type(self.number) is not int or self.number < 1:
            raise ValueError("number must be positive")
        values = tuple(self.reward_requested_by_rank)
        if not values or any(type(value) is not bool for value in values):
            raise TypeError("reward_requested_by_rank must contain booleans")
        object.__setattr__(self, "reward_requested_by_rank", values)

    def for_rank(self, rank: int) -> bool:
        if type(rank) is not int or rank < 1 or rank > len(self.reward_requested_by_rank):
            raise IndexError("rank is outside reward rule")
        return self.reward_requested_by_rank[rank - 1]


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1AuditionScenario:
    stage_type: str
    number: int
    npc_group_id: str
    authority: InitialRegularPlan1NpcScenarioAuthority
    probability: Fraction | None
    rng_before: int | None
    battle_bonus_permille: tuple[int, int, int] | None
    gimmick_group_id: str | None
    npc_score_multiple_permille: int | None
    npc_scores: tuple[InitialRegularPlan1NpcTerminalScore, ...] | None
    reward_rule: InitialRegularPlan1AuditionRewardRule | None
    branch_id: str | None = None
    rng_token: str | None = None

    def __post_init__(self) -> None:
        if not self.stage_type or not self.npc_group_id:
            raise ValueError("stage_type/npc_group_id must be non-empty")
        if type(self.number) is not int or self.number < 1:
            raise ValueError("number must be positive")
        if not isinstance(self.authority, InitialRegularPlan1NpcScenarioAuthority):
            raise TypeError("authority must be InitialRegularPlan1NpcScenarioAuthority")
        if self.probability is not None and (
            type(self.probability) is not Fraction
            or not 0 < self.probability <= 1
        ):
            raise ValueError("probability must be a Fraction in (0,1]")
        if self.rng_before is not None and (
            type(self.rng_before) is not int or not 0 <= self.rng_before <= 0xFFFFFFFF
        ):
            raise ValueError("rng_before must be UInt32 or None")
        if self.battle_bonus_permille is not None:
            values = tuple(self.battle_bonus_permille)
            if len(values) != 3 or any(type(value) is not int or value < 0 for value in values):
                raise ValueError("battle_bonus_permille must contain three non-negative integers")
            object.__setattr__(self, "battle_bonus_permille", values)
        if self.gimmick_group_id is not None and not isinstance(self.gimmick_group_id, str):
            raise TypeError("gimmick_group_id must be text or None")
        if self.npc_score_multiple_permille is not None and (
            type(self.npc_score_multiple_permille) is not int
            or self.npc_score_multiple_permille < 0
        ):
            raise ValueError("npc_score_multiple_permille must be non-negative or None")
        if self.npc_scores is not None:
            scores = tuple(self.npc_scores)
            if any(not isinstance(value, InitialRegularPlan1NpcTerminalScore) for value in scores):
                raise TypeError("npc_scores must contain typed terminal scores")
            object.__setattr__(self, "npc_scores", scores)
        if self.reward_rule is not None and not isinstance(
            self.reward_rule, InitialRegularPlan1AuditionRewardRule
        ):
            raise TypeError("reward_rule must be typed or None")
        for name in ("branch_id", "rng_token"):
            value = getattr(self, name)
            if value is not None and not value:
                raise ValueError(f"{name} must be non-empty or None")


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1AuditionResolution:
    player_score: int
    npc_final_scores: tuple[int, ...]
    rank: int
    reward_requested: bool
    rng_after: int | None
    trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (("player_score", self.player_score), ("rank", self.rank)):
            if type(value) is not int or value < 0 or (name == "rank" and value < 1):
                raise ValueError(f"{name} is invalid")
        if any(type(value) is not int or value < 0 for value in self.npc_final_scores):
            raise ValueError("npc_final_scores must be non-negative integers")
        if not isinstance(self.reward_requested, bool):
            raise TypeError("reward_requested must be boolean")
        if self.rng_after is not None and (
            type(self.rng_after) is not int or not 0 <= self.rng_after <= 0xFFFFFFFF
        ):
            raise ValueError("rng_after must be UInt32 or None")
        object.__setattr__(self, "npc_final_scores", tuple(self.npc_final_scores))
        object.__setattr__(self, "trace", tuple(self.trace))


@dataclass(frozen=True, slots=True)
class InitialRegularPlan1AuditionResult:
    resolution: InitialRegularPlan1AuditionResolution | None
    catalog: InitialRegularPlan2AuditionNpcCatalog | None
    issues: tuple[InitialRegularInnerStageIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.resolution is not None and not isinstance(
            self.resolution, InitialRegularPlan1AuditionResolution
        ):
            raise TypeError("resolution must be typed or None")
        if self.catalog is not None and not isinstance(
            self.catalog, InitialRegularPlan2AuditionNpcCatalog
        ):
            raise TypeError("catalog must be typed or None")
        object.__setattr__(self, "issues", tuple(self.issues))
        if (self.resolution is None) == (not self.issues):
            raise ValueError("result must be either ready or blocked")

    @property
    def ready(self) -> bool:
        return self.resolution is not None and not self.issues


def resolve_initial_regular_plan1_audition(
    request: InitialRegularInnerStageRequest,
    *,
    player_score: int,
    scenario: InitialRegularPlan1AuditionScenario,
    catalog: InitialRegularPlan2AuditionNpcCatalog | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    load_catalog: bool = True,
) -> InitialRegularPlan1AuditionResult:
    """Resolve strict rank/reward from player score and explicit NPC scores."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    if type(player_score) is not int or player_score < 0:
        return InitialRegularPlan1AuditionResult(
            None,
            catalog,
            (_issue("plan1-audition-player-score-invalid", "player_score"),),
        )
    if not isinstance(scenario, InitialRegularPlan1AuditionScenario):
        raise TypeError("scenario must be InitialRegularPlan1AuditionScenario")
    issues: list[InitialRegularInnerStageIssue] = []
    if request.stage_kind is not InitialRegularInnerStageKind.AUDITION:
        issues.append(_issue("plan1-audition-request-kind-invalid", "request.stage_kind"))
    if request.stage_type != scenario.stage_type:
        issues.append(_issue("plan1-audition-stage-type-mismatch", "scenario.stage_type"))
    if request.rng_before is None:
        issues.append(_issue("plan1-audition-rng-before-unresolved", "request.rng_before"))
    elif scenario.rng_before is None:
        issues.append(_issue("plan1-audition-rng-before-unresolved", "scenario.rng_before"))
    elif request.rng_before != scenario.rng_before:
        issues.append(_issue("plan1-audition-rng-before-mismatch", "scenario.rng_before"))
    if request.battle_bonus_permille is None:
        issues.append(_issue("plan1-audition-battle-bonus-unresolved", "request.battle_bonus_permille"))
    if scenario.battle_bonus_permille is None:
        issues.append(_issue("plan1-audition-battle-bonus-unresolved", "scenario.battle_bonus_permille"))
    elif request.battle_bonus_permille is not None and tuple(request.battle_bonus_permille) != tuple(scenario.battle_bonus_permille):
        issues.append(_issue("plan1-audition-battle-bonus-mismatch", "scenario.battle_bonus_permille"))
    if request.gimmick_group_id is None:
        issues.append(_issue("plan1-audition-gimmick-unresolved", "request.gimmick_group_id"))
    if scenario.gimmick_group_id is None:
        issues.append(_issue("plan1-audition-gimmick-unresolved", "scenario.gimmick_group_id"))
    elif request.gimmick_group_id is not None and request.gimmick_group_id != scenario.gimmick_group_id:
        issues.append(_issue("plan1-audition-gimmick-mismatch", "scenario.gimmick_group_id"))
    if scenario.npc_score_multiple_permille is None:
        issues.append(_issue("plan1-audition-npc-score-multiple-unresolved", "scenario.npc_score_multiple_permille"))

    selected_catalog = catalog
    if selected_catalog is None and load_catalog:
        if not request.idol_card_id:
            issues.append(_issue("plan1-audition-idol-card-unresolved", "request.idol_card_id"))
        else:
            loaded = load_initial_regular_plan2_audition_npc_catalog(
                request.idol_card_id,
                stage_type=scenario.stage_type,
                number=scenario.number,
                master_dir=master_dir,
            )
            if loaded.ready:
                selected_catalog = loaded.catalog
            else:
                issues.extend(
                    _issue(f"plan1-catalog:{item.code}", item.field, item.detail)
                    for item in loaded.issues
                )
    if selected_catalog is None and not load_catalog:
        issues.append(_issue("plan1-audition-catalog-unresolved", "catalog"))
    if selected_catalog is not None:
        if request.idol_card_id != selected_catalog.idol_card_id:
            issues.append(_issue("plan1-audition-catalog-idol-mismatch", "catalog.idol_card_id"))
        if selected_catalog.stage_type != scenario.stage_type or selected_catalog.number != scenario.number:
            issues.append(_issue("plan1-audition-catalog-stage-mismatch", "catalog.stage_type"))
        if selected_catalog.npc_group_id != scenario.npc_group_id:
            issues.append(_issue("plan1-audition-npc-group-mismatch", "scenario.npc_group_id"))

    tracks = scenario.npc_scores
    if tracks is None:
        issues.append(_issue("plan1-audition-npc-scores-unresolved", "scenario.npc_scores"))
        tracks = ()
    else:
        expected_numbers = tuple(range(1, len(tracks) + 1))
        if tuple(track.number for track in tracks) != expected_numbers:
            issues.append(_issue("plan1-audition-npc-number-order-invalid", "scenario.npc_scores"))
        if any(track.npc_group_id != scenario.npc_group_id for track in tracks):
            issues.append(_issue("plan1-audition-npc-score-group-mismatch", "scenario.npc_scores"))
        if selected_catalog is not None and len(tracks) != len(selected_catalog.npcs):
            issues.append(_issue("plan1-audition-npc-count-mismatch", "scenario.npc_scores"))
        if selected_catalog is not None:
            for track, master in zip(tracks, selected_catalog.npcs):
                if not master.score_min <= track.final_score <= master.score_max:
                    issues.append(
                        _issue(
                            "plan1-audition-npc-score-out-of-master-range",
                            f"scenario.npc_scores[{track.number - 1}].final_score",
                            f"range={master.score_min}..{master.score_max}",
                        )
                    )
    reward_rule = scenario.reward_rule
    if reward_rule is None:
        issues.append(_issue("plan1-audition-reward-rule-unresolved", "scenario.reward_rule"))
    else:
        if reward_rule.stage_type != scenario.stage_type or reward_rule.number != scenario.number:
            issues.append(_issue("plan1-audition-reward-rule-mismatch", "scenario.reward_rule"))
        expected_size = (len(selected_catalog.npcs) + 1) if selected_catalog is not None else len(tracks) + 1
        if len(reward_rule.reward_requested_by_rank) != expected_size:
            issues.append(_issue("plan1-audition-reward-rule-incomplete", "scenario.reward_rule"))
    if issues:
        return InitialRegularPlan1AuditionResult(None, selected_catalog, tuple(dict.fromkeys(issues)))

    assert reward_rule is not None
    npc_scores = tuple(track.final_score for track in tracks)
    # Android tie semantics: only strictly greater NPC scores outrank player.
    rank = 1 + sum(score > player_score for score in npc_scores)
    reward_requested = reward_rule.for_rank(rank)
    if request.clear_border is None or request.clear_border < 1:
        return InitialRegularPlan1AuditionResult(
            None,
            selected_catalog,
            (_issue("plan1-audition-clear-border-unresolved", "request.clear_border"),),
        )
    if reward_requested and rank > request.clear_border:
        return InitialRegularPlan1AuditionResult(
            None,
            selected_catalog,
            (
                _issue(
                    "plan1-audition-reward-on-uncleared-rank",
                    "scenario.reward_rule",
                    f"rank={rank}:clear_border={request.clear_border}",
                ),
            ),
        )
    rng_after = scenario.rng_before
    resolution = InitialRegularPlan1AuditionResolution(
        player_score=player_score,
        npc_final_scores=npc_scores,
        rank=rank,
        reward_requested=reward_requested,
        rng_after=rng_after,
        trace=(
            "rank-predicate:npc.CurrentScore > player.Parameter",
            f"player-score:{player_score}",
            f"npc-scores:{','.join(str(value) for value in npc_scores)}",
            f"rank:{rank}",
            f"npc-authority:{scenario.authority.value}",
            f"npc-score-multiple-permille:{scenario.npc_score_multiple_permille}",
        ),
    )
    return InitialRegularPlan1AuditionResult(resolution, selected_catalog, ())


__all__ = [
    "INITIAL_REGULAR_PLAN1_AUDITION_ADAPTER_ID",
    "InitialRegularPlan1AuditionResolution",
    "InitialRegularPlan1AuditionResult",
    "InitialRegularPlan1AuditionRewardRule",
    "InitialRegularPlan1AuditionScenario",
    "InitialRegularPlan1NpcScenarioAuthority",
    "InitialRegularPlan1NpcTerminalScore",
    "resolve_initial_regular_plan1_audition",
]
