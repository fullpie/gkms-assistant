"""Master-backed common inner requests for Initial Regular auditions.

The client Master owns the selected audition difficulty and battle config.
The server/runtime owns the concrete turn-parameter schedule, effective battle
bonuses, extra turns, and RNG root.  This module joins those two inputs without
inventing any of the server-owned values and emits the same plan-neutral branch
shape used by weekly lessons.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .audition_rules import FINAL, MID1, MID2, DEFAULT_MASTER_DIR, load_audition_rules
from .exam_context import AuditionDifficultyValues, calculate_audition_borders
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageKind,
    InitialRegularInnerStageRequest,
)
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedInnerBranch,
)
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
from .master_db import DEFAULT_DATABASE, get_idol_profile
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    RolloutPhase,
)
from .produce_rollout_expectimax import OuterSearchIssue


_PLAN_TYPES = {
    "ProducePlanType_Plan1": "plan1",
    "ProducePlanType_Plan2": "plan2",
    "ProducePlanType_Plan3": "plan3",
}
_STEP_TYPE_VALUES = {MID1: 16, MID2: 17, FINAL: 18}


@dataclass(frozen=True, slots=True)
class InitialRegularAuditionScenarioBranch:
    """One exact server/runtime-owned audition root."""

    probability: Fraction
    number: int
    rng_before: int | None
    turn_parameter_types: tuple[int, ...] | None
    battle_bonus_permille: tuple[int, int, int] | None
    extra_turn: int | None = 0
    rng_token: str | None = None
    branch_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.probability, bool) or not isinstance(
            self.probability, Fraction
        ):
            raise TypeError("probability must be fractions.Fraction")
        if not 0 < self.probability <= 1:
            raise ValueError("probability must be in (0, 1]")
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 1:
            raise ValueError("number must be a positive integer")
        if self.rng_before is not None and (
            isinstance(self.rng_before, bool)
            or not isinstance(self.rng_before, int)
            or not 0 <= self.rng_before <= 0xFFFFFFFF
        ):
            raise ValueError("rng_before must be UInt32 or None")
        if self.extra_turn is not None and (
            isinstance(self.extra_turn, bool)
            or not isinstance(self.extra_turn, int)
            or self.extra_turn < 0
        ):
            raise ValueError("extra_turn must be non-negative or None")
        if self.turn_parameter_types is not None:
            schedule = tuple(self.turn_parameter_types)
            if any(type(value) is not int or value not in (1, 2, 3) for value in schedule):
                raise ValueError("turn_parameter_types must contain only Vocal/Dance/Visual")
            object.__setattr__(self, "turn_parameter_types", schedule)
        if self.battle_bonus_permille is not None:
            bonuses = tuple(self.battle_bonus_permille)
            if len(bonuses) != 3 or any(type(value) is not int for value in bonuses):
                raise ValueError("battle_bonus_permille must contain three integers")
            object.__setattr__(self, "battle_bonus_permille", bonuses)
        for name in ("rng_token", "branch_id"):
            value = getattr(self, name)
            if value is not None and not value:
                raise ValueError(f"{name} must be non-empty text or None")


def _issue(code: str, field: str, detail: str = "") -> OuterSearchIssue:
    return OuterSearchIssue(code, field, detail)


def build_initial_regular_audition_frontier(
    state: ProduceRolloutState,
    request: ExternalRequest,
    *,
    idol_card_id: str | None,
    branches: tuple[InitialRegularAuditionScenarioBranch, ...],
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> InitialRegularOfflineBranchExpansion:
    """Join static audition rules with explicit runtime chance roots."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    scenario_branches = tuple(branches)
    if any(
        not isinstance(value, InitialRegularAuditionScenarioBranch)
        for value in scenario_branches
    ):
        raise TypeError("branches must contain InitialRegularAuditionScenarioBranch")
    issues: list[OuterSearchIssue] = []
    if (
        state.phase is not RolloutPhase.WAITING_EXTERNAL
        or state.pending != request
        or request.kind is not ExternalKind.INNER_EXAM_OUTCOME
    ):
        issues.append(_issue("audition-request-not-pending", "state.pending"))
    if request.stage_type not in _STEP_TYPE_VALUES:
        issues.append(
            _issue(
                "audition-stage-type-unsupported",
                "request.stage_type",
                repr(request.stage_type),
            )
        )
    if not idol_card_id:
        issues.append(_issue("idol-card-id-required", "idol_card_id"))
    profile = None if not idol_card_id else get_idol_profile(idol_card_id, database)
    if idol_card_id and profile is None:
        issues.append(_issue("idol-card-not-found", "idol_card_id", idol_card_id))
    elif profile is not None and profile.character_id != state.character_id:
        issues.append(
            _issue(
                "idol-card-character-mismatch",
                "idol_card_id",
                f"outer={state.character_id}:card={profile.character_id}",
            )
        )
    plan_type = None if profile is None else _PLAN_TYPES.get(profile.plan_type)
    if profile is not None and plan_type is None:
        issues.append(
            _issue("idol-card-plan-unsupported", "idol_card.plan_type", profile.plan_type)
        )
    if not scenario_branches:
        issues.append(_issue("audition-scenario-branches-required", "branches"))
    elif sum((value.probability for value in scenario_branches), Fraction()) != 1:
        issues.append(_issue("audition-probability-frontier-invalid", "branches"))
    if issues:
        return InitialRegularOfflineBranchExpansion.paused(*tuple(dict.fromkeys(issues)))

    assert idol_card_id is not None
    assert plan_type is not None
    assert request.stage_type is not None
    weighted_requests: list[InitialRegularWeightedInnerBranch] = []
    for index, branch in enumerate(scenario_branches):
        try:
            rules = load_audition_rules(
                idol_card_id,
                produce_id=INITIAL_REGULAR_PRODUCE_ID,
                step_type=request.stage_type,
                number=branch.number,
                master_dir=master_dir,
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError) as error:
            issues.append(
                _issue(
                    "audition-master-stage-unresolved",
                    f"branches[{index}].number",
                    f"{type(error).__name__}:{error}",
                )
            )
            continue
        expected_turns = None
        if branch.extra_turn is not None:
            expected_turns = rules.turns + branch.extra_turn
        for field_name in (
            "rng_before",
            "turn_parameter_types",
            "battle_bonus_permille",
            "extra_turn",
        ):
            if getattr(branch, field_name) is None:
                issues.append(
                    _issue(
                        "audition-runtime-field-unresolved",
                        f"branches[{index}].{field_name}",
                    )
                )
        if (
            expected_turns is not None
            and branch.turn_parameter_types is not None
            and len(branch.turn_parameter_types) != expected_turns
        ):
            issues.append(
                _issue(
                    "audition-turn-schedule-length-mismatch",
                    f"branches[{index}].turn_parameter_types",
                    f"expected={expected_turns}:actual={len(branch.turn_parameter_types)}",
                )
            )
        if any(issue.field.startswith(f"branches[{index}].") for issue in issues):
            continue
        assert branch.rng_before is not None
        assert branch.turn_parameter_types is not None
        assert branch.battle_bonus_permille is not None
        assert branch.extra_turn is not None
        borders = calculate_audition_borders(
            AuditionDifficultyValues(rules.rank_threshold, rules.force_end_score)
        )
        chance = ChanceBranch(
            branch_id=(
                branch.branch_id
                or f"{request.request_id}:{request.stage_type}:{branch.number}:{index}"
            ),
            probability=float(branch.probability),
            rng_token=branch.rng_token,
        )
        inner = InitialRegularInnerStageRequest(
            external_request_id=request.request_id,
            week=request.week,
            action_id=request.action_id,
            stage_kind=InitialRegularInnerStageKind.AUDITION,
            plan_type=plan_type,
            character_id=state.character_id,
            outer_state=state,
            branch=chance,
            idol_card_id=idol_card_id,
            stage_type=request.stage_type,
            stage_id=(
                f"{rules.battle_config_id}:{request.stage_type}:{branch.number}"
            ),
            setting_id=rules.exam_setting_id,
            exam_type=1,
            lesson_type=None,
            step_type_value=_STEP_TYPE_VALUES[request.stage_type],
            limit_turn=rules.turns,
            extra_turn=branch.extra_turn,
            clear_border=borders.clear_border,
            limit_border=borders.limit_border,
            rng_before=branch.rng_before,
            attribute=None,
            is_sp=None,
            battle_bonus_permille=branch.battle_bonus_permille,
            gimmick_group_id=rules.gimmick_group_id,
            turn_parameter_types=branch.turn_parameter_types,
        )
        weighted_requests.append(
            InitialRegularWeightedInnerBranch(branch.probability, inner)
        )
    if issues:
        return InitialRegularOfflineBranchExpansion.paused(*tuple(dict.fromkeys(issues)))
    return InitialRegularOfflineBranchExpansion.resolved(*weighted_requests)


__all__ = [
    "InitialRegularAuditionScenarioBranch",
    "build_initial_regular_audition_frontier",
]
