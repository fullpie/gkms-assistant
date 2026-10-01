"""Strict NIA terminal rank and reward resolution over caller-owned tracks.

PC Master identifies the audition, NPC field, score ranges, and gimmick
profile.  It does not identify one runtime NPC roll or one reward decision.
Those values enter as an explicit scenario and are never regenerated here.

Android v3.2.3 ranks the player with the strict predicate
``npc.CurrentScore > player.Parameter``.  Equal scores therefore favour the
player.  A zero ``npcScoreMultiplePermil`` permits the serialized per-turn
track to be summed directly.  A non-zero value is accepted only with typed,
caller-authoritative mutation-timing evidence and terminal tracks which the
caller explicitly declares already include those transitions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from .initial_regular_inner_protocol import InitialRegularInnerStageIssue
from .master_db import DEFAULT_DATABASE
from .nia_inner_terminal_acceptance import NiaInnerTerminalAcceptance
from .nia_native_search import NiaSearchTurnStartTrace
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .nia_static_inventory import (
    NiaStaticAuditionStage,
    build_nia_static_inventory,
)


ANDROID_NIA_AUDITION_RANK_PREDICATE: Final = (
    "npc.CurrentScore > player.Parameter"
)


def _issue(
    code: str,
    field: str,
    detail: str = "",
) -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")


def _integer(value: object, label: str, *, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")


class NiaNpcTerminalTrackAuthority(StrEnum):
    """The accepted rank input is a resolved caller scenario, not a roll."""

    CALLER_RESOLVED = "caller-resolved-terminal-tracks"


@dataclass(frozen=True, slots=True)
class NiaNpcTerminalTrack:
    """One complete native-shaped NPC track at the terminal boundary."""

    npc_group_id: str
    number: int
    mob_id: str
    turn_scores: tuple[int, ...]
    final_score: int
    vocal_score: int
    dance_score: int
    visual_score: int

    def __post_init__(self) -> None:
        _text(self.npc_group_id, "npc_group_id")
        _text(self.mob_id, "mob_id")
        _integer(self.number, "number", minimum=1)
        for name in (
            "final_score",
            "vocal_score",
            "dance_score",
            "visual_score",
        ):
            _integer(getattr(self, name), name)
        scores = tuple(self.turn_scores)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in scores
        ):
            raise ValueError("turn_scores must contain non-negative integers")
        object.__setattr__(self, "turn_scores", scores)


@dataclass(frozen=True, slots=True)
class NiaNpcMultiplierTimingResolution:
    """Caller evidence for otherwise-unmodelled non-zero mutation timing.

    The resolver does not interpret or replay these transition turns.  They
    establish only that the supplied terminal tracks already incorporate the
    externally resolved transitions.
    """

    authority_ref: str
    transition_turns: tuple[int, ...]
    terminal_tracks_include_transitions: bool

    def __post_init__(self) -> None:
        _text(self.authority_ref, "authority_ref")
        turns = tuple(self.transition_turns)
        if not turns or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in turns
        ):
            raise ValueError(
                "transition_turns must contain positive integer turns"
            )
        if len(turns) != len(set(turns)) or tuple(sorted(turns)) != turns:
            raise ValueError("transition_turns must be unique and ordered")
        if not isinstance(self.terminal_tracks_include_transitions, bool):
            raise TypeError("terminal_tracks_include_transitions must be bool")
        object.__setattr__(self, "transition_turns", turns)


@dataclass(frozen=True, slots=True)
class NiaAuditionRewardRule:
    """Caller-resolved reward request decision for every possible rank."""

    stage_type: str
    number: int
    reward_requested_by_rank: tuple[bool, ...]

    def __post_init__(self) -> None:
        _text(self.stage_type, "reward_rule.stage_type")
        _integer(self.number, "reward_rule.number", minimum=1)
        values = tuple(self.reward_requested_by_rank)
        if not values or any(type(value) is not bool for value in values):
            raise TypeError("reward_requested_by_rank must contain booleans")
        object.__setattr__(self, "reward_requested_by_rank", values)

    def for_rank(self, rank: int) -> bool:
        _integer(rank, "rank", minimum=1)
        if rank > len(self.reward_requested_by_rank):
            raise IndexError("rank is not covered by the reward rule")
        return self.reward_requested_by_rank[rank - 1]


@dataclass(frozen=True, slots=True)
class NiaAuditionTerminalScenario:
    """Runtime facts which Master and the deterministic player search lack.

    Optional fields deliberately preserve incomplete captures so the resolver
    can return one blocker for each missing authority boundary.
    """

    stage_type: str
    number: int
    npc_group_id: str
    authority: NiaNpcTerminalTrackAuthority
    is_static_npc_score: bool | None
    npc_score_multiple_permille: int | None
    npc_multiplier_timing: NiaNpcMultiplierTimingResolution | None
    battle_bonus_permille: tuple[int, int, int] | None
    gimmick_group_id: str | None
    turn_parameter_types: tuple[int, ...] | None
    npc_tracks: tuple[NiaNpcTerminalTrack, ...] | None
    reward_rule: NiaAuditionRewardRule | None

    def __post_init__(self) -> None:
        _text(self.stage_type, "scenario.stage_type")
        _integer(self.number, "scenario.number", minimum=1)
        _text(self.npc_group_id, "scenario.npc_group_id")
        if not isinstance(self.authority, NiaNpcTerminalTrackAuthority):
            raise TypeError("scenario.authority must be typed")
        if self.is_static_npc_score is not None and not isinstance(
            self.is_static_npc_score, bool
        ):
            raise TypeError("is_static_npc_score must be bool or None")
        if self.npc_score_multiple_permille is not None:
            _integer(
                self.npc_score_multiple_permille,
                "npc_score_multiple_permille",
            )
        if self.npc_multiplier_timing is not None and not isinstance(
            self.npc_multiplier_timing,
            NiaNpcMultiplierTimingResolution,
        ):
            raise TypeError("npc_multiplier_timing must be typed or None")
        if self.battle_bonus_permille is not None:
            bonuses = tuple(self.battle_bonus_permille)
            if len(bonuses) != 3 or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in bonuses
            ):
                raise ValueError(
                    "battle_bonus_permille must contain three non-negative integers"
                )
            object.__setattr__(self, "battle_bonus_permille", bonuses)
        if self.gimmick_group_id is not None:
            _text(self.gimmick_group_id, "gimmick_group_id")
        if self.turn_parameter_types is not None:
            values = tuple(self.turn_parameter_types)
            if any(type(value) is not int or value not in (1, 2, 3) for value in values):
                raise ValueError(
                    "turn_parameter_types must contain Vocal/Dance/Visual"
                )
            object.__setattr__(self, "turn_parameter_types", values)
        if self.npc_tracks is not None:
            tracks = tuple(self.npc_tracks)
            if any(not isinstance(value, NiaNpcTerminalTrack) for value in tracks):
                raise TypeError("npc_tracks must contain NiaNpcTerminalTrack")
            object.__setattr__(self, "npc_tracks", tracks)
        if self.reward_rule is not None and not isinstance(
            self.reward_rule,
            NiaAuditionRewardRule,
        ):
            raise TypeError("reward_rule must be typed or None")


@dataclass(frozen=True, slots=True)
class NiaAuditionTerminalResolution:
    player_score: int
    npc_final_scores: tuple[int, ...]
    rank: int
    cleared: bool
    reward_requested: bool
    trace: tuple[str, ...]

    def __post_init__(self) -> None:
        _integer(self.player_score, "player_score")
        _integer(self.rank, "rank", minimum=1)
        scores = tuple(self.npc_final_scores)
        if any(type(value) is not int or value < 0 for value in scores):
            raise ValueError("npc_final_scores must be non-negative integers")
        for name in ("cleared", "reward_requested"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        trace = tuple(self.trace)
        if any(not isinstance(value, str) or not value for value in trace):
            raise ValueError("trace must contain non-empty text")
        object.__setattr__(self, "npc_final_scores", scores)
        object.__setattr__(self, "trace", trace)


@dataclass(frozen=True, slots=True)
class NiaAuditionTerminalResolverResult:
    catalog: NiaStaticAuditionStage | None
    resolution: NiaAuditionTerminalResolution | None
    issues: tuple[InitialRegularInnerStageIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.catalog is not None and not isinstance(
            self.catalog,
            NiaStaticAuditionStage,
        ):
            raise TypeError("catalog must be NiaStaticAuditionStage or None")
        if self.resolution is not None and not isinstance(
            self.resolution,
            NiaAuditionTerminalResolution,
        ):
            raise TypeError("resolution must be typed or None")
        issues = tuple(self.issues)
        if any(not isinstance(value, InitialRegularInnerStageIssue) for value in issues):
            raise TypeError("issues must contain InitialRegularInnerStageIssue")
        if (self.resolution is None) == (not issues):
            raise ValueError("resolver result must be either ready or blocked")
        object.__setattr__(self, "issues", issues)

    @property
    def ready(self) -> bool:
        return self.resolution is not None and not self.issues


def load_nia_audition_stage_for_acceptance(
    acceptance: NiaInnerTerminalAcceptance,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> NiaStaticAuditionStage:
    """Load exactly the Master stage selected by one terminal acceptance."""

    if not isinstance(acceptance, NiaInnerTerminalAcceptance):
        raise TypeError("acceptance must be NiaInnerTerminalAcceptance")
    selector = acceptance.fixture.selector
    inventory = build_nia_static_inventory(
        selector.idol_card_id,
        produce_id=selector.produce_id,
        master_dir=Path(master_dir),
        database=Path(database),
    )
    matches = tuple(
        stage
        for stage in inventory.auditions
        if stage.step_type == selector.step_type
        and stage.number == selector.audition_number
    )
    if len(matches) != 1:
        raise ValueError(
            "NIA audition catalog must resolve exactly once: "
            f"{selector.produce_id}/{selector.step_type}/"
            f"{selector.audition_number}:found={len(matches)}"
        )
    return matches[0]


def _catalog_issues(
    acceptance: NiaInnerTerminalAcceptance,
    catalog: NiaStaticAuditionStage,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    selector = acceptance.fixture.selector
    rules = acceptance.audition.rules
    checks = (
        ("catalog.step_type", catalog.step_type, selector.step_type),
        ("catalog.number", catalog.number, selector.audition_number),
        (
            "catalog.difficulty_row_id",
            catalog.difficulty_row_id,
            acceptance.audition.difficulty_key.row_id,
        ),
        ("catalog.battle_config_id", catalog.battle_config_id, rules.battle_config_id),
        ("catalog.npc_group_id", catalog.npc_group_id, rules.npc_group_id),
        (
            "catalog.gimmick_group_id",
            catalog.gimmick_group_id,
            acceptance.profile.identity.group_id,
        ),
        ("catalog.turns", catalog.turns, acceptance.profile.identity.turns),
        ("catalog.clear_border", catalog.clear_border, rules.rank_threshold),
    )
    for field, actual, expected in checks:
        if actual != expected:
            issues.append(
                _issue(
                    "nia-audition-catalog-mismatch",
                    field,
                    f"expected={expected}:actual={actual}",
                )
            )
    catalog_gimmicks = Counter(
        (item.priority, item.start_turn, item.effect_id)
        for item in catalog.gimmicks
    )
    acceptance_gimmicks = Counter(
        (item.priority, item.start_turn, item.effect_id)
        for item in acceptance.profile.steps
    )
    if catalog_gimmicks != acceptance_gimmicks:
        issues.append(
            _issue(
                "nia-audition-catalog-gimmick-mismatch",
                "catalog.gimmicks",
            )
        )
    if not acceptance.deterministic_player_inner_executable:
        issues.append(
            _issue(
                "nia-audition-player-terminal-incomplete",
                "acceptance",
                ",".join(item.code for item in acceptance.issues),
            )
        )
    best = acceptance.best
    if best is not None:
        # Every Master gimmick row must be *evaluated* at its scheduled
        # TurnStart boundary.  A row whose runtime counter condition is false
        # is still an exact, successfully handled decision; it is not an
        # effect that should be installed.  Comparing only ``installed`` rows
        # incorrectly rejected legitimate runs whenever an archetype did not
        # satisfy every optional gimmick condition.
        evaluated = Counter(
            (
                decision.step.priority,
                decision.step.start_turn,
                decision.step.effect_id,
            )
            for step in best.steps
            if isinstance(
                step.turn_start_extension_trace,
                NiaSearchTurnStartTrace,
            )
            for decision in step.turn_start_extension_trace.decisions
        )
        if evaluated != acceptance_gimmicks:
            issues.append(
                _issue(
                    "nia-audition-gimmick-execution-incomplete",
                    "acceptance.search.turn_start_extension_trace",
                    f"expected={sum(acceptance_gimmicks.values())}:"
                    f"actual={sum(evaluated.values())}",
                )
            )
        unhandled = tuple(
            decision
            for step in best.steps
            if isinstance(
                step.turn_start_extension_trace,
                NiaSearchTurnStartTrace,
            )
            for decision in step.turn_start_extension_trace.decisions
            if decision.condition_met and not decision.handled
        )
        if unhandled:
            issues.append(
                _issue(
                    "nia-audition-gimmick-execution-unhandled",
                    "acceptance.search.turn_start_extension_trace",
                    ",".join(
                        f"{item.step.priority}:{item.step.effect_id}:"
                        f"{item.reason}"
                        for item in unhandled
                    ),
                )
            )
    return tuple(issues)


def _scenario_issues(
    acceptance: NiaInnerTerminalAcceptance,
    scenario: NiaAuditionTerminalScenario,
    catalog: NiaStaticAuditionStage,
) -> tuple[
    tuple[NiaNpcTerminalTrack, ...],
    tuple[InitialRegularInnerStageIssue, ...],
]:
    issues: list[InitialRegularInnerStageIssue] = []
    checks = (
        ("scenario.stage_type", scenario.stage_type, catalog.step_type),
        ("scenario.number", scenario.number, catalog.number),
        ("scenario.npc_group_id", scenario.npc_group_id, catalog.npc_group_id),
    )
    for field, actual, expected in checks:
        if actual != expected:
            issues.append(
                _issue(
                    "nia-audition-scenario-identity-mismatch",
                    field,
                    f"expected={expected}:actual={actual}",
                )
            )

    if scenario.is_static_npc_score is None:
        issues.append(
            _issue(
                "nia-audition-static-npc-flag-unresolved",
                "scenario.is_static_npc_score",
            )
        )
    elif scenario.is_static_npc_score != catalog.is_static_npc_score:
        issues.append(
            _issue(
                "nia-audition-static-npc-flag-mismatch",
                "scenario.is_static_npc_score",
                f"master={catalog.is_static_npc_score}:"
                f"scenario={scenario.is_static_npc_score}",
            )
        )

    multiplier = scenario.npc_score_multiple_permille
    timing = scenario.npc_multiplier_timing
    if multiplier is None:
        issues.append(
            _issue(
                "nia-audition-npc-multiplier-unresolved",
                "scenario.npc_score_multiple_permille",
            )
        )
    elif multiplier != 0:
        if timing is None:
            issues.append(
                _issue(
                    "nia-audition-npc-multiplier-timing-unresolved",
                    "scenario.npc_multiplier_timing",
                    f"npcScoreMultiplePermil={multiplier}",
                )
            )
        else:
            if timing.transition_turns[-1] > catalog.turns:
                issues.append(
                    _issue(
                        "nia-audition-npc-multiplier-timing-out-of-range",
                        "scenario.npc_multiplier_timing.transition_turns",
                        f"limit_turn={catalog.turns}",
                    )
                )
            if not timing.terminal_tracks_include_transitions:
                issues.append(
                    _issue(
                        "nia-audition-npc-multiplier-tracks-unresolved",
                        (
                            "scenario.npc_multiplier_timing."
                            "terminal_tracks_include_transitions"
                        ),
                    )
                )

    expected_bonus = (
        acceptance.initial_state.battle_bonus_permille_vocal,
        acceptance.initial_state.battle_bonus_permille_dance,
        acceptance.initial_state.battle_bonus_permille_visual,
    )
    if scenario.battle_bonus_permille is None:
        issues.append(
            _issue(
                "nia-audition-battle-bonus-unresolved",
                "scenario.battle_bonus_permille",
            )
        )
    elif scenario.battle_bonus_permille != expected_bonus:
        issues.append(
            _issue(
                "nia-audition-battle-bonus-mismatch",
                "scenario.battle_bonus_permille",
                f"acceptance={expected_bonus}:scenario={scenario.battle_bonus_permille}",
            )
        )

    if scenario.gimmick_group_id is None:
        issues.append(
            _issue(
                "nia-audition-gimmick-unresolved",
                "scenario.gimmick_group_id",
            )
        )
    elif scenario.gimmick_group_id != catalog.gimmick_group_id:
        issues.append(
            _issue(
                "nia-audition-gimmick-mismatch",
                "scenario.gimmick_group_id",
                f"master={catalog.gimmick_group_id}:"
                f"scenario={scenario.gimmick_group_id}",
            )
        )

    if scenario.turn_parameter_types is None:
        issues.append(
            _issue(
                "nia-audition-turn-parameter-types-unresolved",
                "scenario.turn_parameter_types",
            )
        )
    elif scenario.turn_parameter_types != acceptance.battle_parameter_schedule:
        issues.append(
            _issue(
                "nia-audition-turn-parameter-types-mismatch",
                "scenario.turn_parameter_types",
            )
        )

    tracks = scenario.npc_tracks
    if tracks is None or not tracks:
        issues.append(
            _issue(
                "nia-audition-npc-tracks-unresolved",
                "scenario.npc_tracks",
            )
        )
        tracks = ()
    expected_numbers = tuple(item.number for item in catalog.npc_scores)
    actual_numbers = tuple(item.number for item in tracks)
    if tracks and actual_numbers != expected_numbers:
        issues.append(
            _issue(
                "nia-audition-npc-track-order-incomplete",
                "scenario.npc_tracks",
                f"expected={expected_numbers}:actual={actual_numbers}",
            )
        )
    master_by_number = {item.number: item for item in catalog.npc_scores}
    for index, track in enumerate(tracks):
        field = f"scenario.npc_tracks[{index}]"
        master = master_by_number.get(track.number)
        if track.npc_group_id != catalog.npc_group_id:
            issues.append(
                _issue(
                    "nia-audition-npc-track-group-mismatch",
                    f"{field}.npc_group_id",
                )
            )
        if master is None:
            continue
        if track.mob_id != master.mob_id:
            issues.append(
                _issue(
                    "nia-audition-npc-track-mob-mismatch",
                    f"{field}.mob_id",
                    f"master={master.mob_id}:scenario={track.mob_id}",
                )
            )
        if len(track.turn_scores) != catalog.turns:
            issues.append(
                _issue(
                    "nia-audition-npc-track-length-mismatch",
                    f"{field}.turn_scores",
                    f"expected={catalog.turns}:actual={len(track.turn_scores)}",
                )
            )
        if multiplier == 0:
            summed = sum(track.turn_scores)
            if summed != track.final_score:
                issues.append(
                    _issue(
                        "nia-audition-npc-track-final-mismatch",
                        f"{field}.final_score",
                        f"sum={summed}:final={track.final_score}",
                    )
                )
            if not master.score_min <= track.final_score <= master.score_max:
                issues.append(
                    _issue(
                        "nia-audition-npc-score-out-of-master-range",
                        f"{field}.final_score",
                        f"range={master.score_min}..{master.score_max}",
                    )
                )

    reward_rule = scenario.reward_rule
    if reward_rule is None:
        issues.append(
            _issue(
                "nia-audition-reward-rule-unresolved",
                "scenario.reward_rule",
            )
        )
    else:
        if (
            reward_rule.stage_type != catalog.step_type
            or reward_rule.number != catalog.number
        ):
            issues.append(
                _issue(
                    "nia-audition-reward-rule-identity-mismatch",
                    "scenario.reward_rule",
                )
            )
        field_size = len(catalog.npc_scores) + 1
        if len(reward_rule.reward_requested_by_rank) != field_size:
            issues.append(
                _issue(
                    "nia-audition-reward-rule-incomplete",
                    "scenario.reward_rule.reward_requested_by_rank",
                    f"expected={field_size}:"
                    f"actual={len(reward_rule.reward_requested_by_rank)}",
                )
            )
    return tuple(tracks), tuple(dict.fromkeys(issues))


def resolve_initial_regular_nia_audition(
    acceptance: NiaInnerTerminalAcceptance,
    scenario: NiaAuditionTerminalScenario,
    *,
    catalog: NiaStaticAuditionStage | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> NiaAuditionTerminalResolverResult:
    """Resolve player rank and reward without regenerating NPC state."""

    if not isinstance(acceptance, NiaInnerTerminalAcceptance):
        raise TypeError("acceptance must be NiaInnerTerminalAcceptance")
    if not isinstance(scenario, NiaAuditionTerminalScenario):
        raise TypeError("scenario must be NiaAuditionTerminalScenario")
    selected = catalog
    if selected is None:
        try:
            selected = load_nia_audition_stage_for_acceptance(
                acceptance,
                master_dir=Path(master_dir),
                database=Path(database),
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            return NiaAuditionTerminalResolverResult(
                None,
                None,
                (
                    _issue(
                        "nia-audition-catalog-unavailable",
                        "catalog",
                        f"{type(error).__name__}:{error}",
                    ),
                ),
            )
    if not isinstance(selected, NiaStaticAuditionStage):
        raise TypeError("catalog must be NiaStaticAuditionStage or None")

    issues = list(_catalog_issues(acceptance, selected))
    tracks, scenario_issues = _scenario_issues(
        acceptance,
        scenario,
        selected,
    )
    issues.extend(scenario_issues)
    if issues:
        return NiaAuditionTerminalResolverResult(
            selected,
            None,
            tuple(dict.fromkeys(issues)),
        )

    best = acceptance.best
    assert best is not None
    player_score = best.state.score
    npc_scores = tuple(track.final_score for track in tracks)
    rank = 1 + sum(score > player_score for score in npc_scores)
    cleared = rank <= selected.clear_border
    reward_rule = scenario.reward_rule
    assert reward_rule is not None
    reward_requested = reward_rule.for_rank(rank)
    if reward_requested and not cleared:
        return NiaAuditionTerminalResolverResult(
            selected,
            None,
            (
                _issue(
                    "nia-audition-reward-on-uncleared-rank",
                    f"scenario.reward_rule.reward_requested_by_rank[{rank - 1}]",
                    f"rank={rank}:clear_border={selected.clear_border}",
                ),
            ),
        )
    timing = scenario.npc_multiplier_timing
    timing_trace = (
        "none-zero-multiplier"
        if timing is None
        else (
            f"{timing.authority_ref}:"
            f"turns={','.join(str(value) for value in timing.transition_turns)}"
        )
    )
    return NiaAuditionTerminalResolverResult(
        selected,
        NiaAuditionTerminalResolution(
            player_score=player_score,
            npc_final_scores=npc_scores,
            rank=rank,
            cleared=cleared,
            reward_requested=reward_requested,
            trace=(
                f"rank-predicate:{ANDROID_NIA_AUDITION_RANK_PREDICATE}",
                f"player-score:{player_score}",
                f"npc-scores:{','.join(str(value) for value in npc_scores)}",
                f"rank:{rank}",
                f"clear-border:{selected.clear_border}",
                f"reward-requested:{str(reward_requested).lower()}",
                f"npc-authority:{scenario.authority.value}",
                (
                    "npc-score-multiple-permille:"
                    f"{scenario.npc_score_multiple_permille}"
                ),
                f"npc-multiplier-timing:{timing_trace}",
            ),
        ),
        (),
    )


__all__ = [
    "ANDROID_NIA_AUDITION_RANK_PREDICATE",
    "NiaAuditionRewardRule",
    "NiaAuditionTerminalResolution",
    "NiaAuditionTerminalResolverResult",
    "NiaAuditionTerminalScenario",
    "NiaNpcMultiplierTimingResolution",
    "NiaNpcTerminalTrack",
    "NiaNpcTerminalTrackAuthority",
    "load_nia_audition_stage_for_acceptance",
    "resolve_initial_regular_nia_audition",
]
