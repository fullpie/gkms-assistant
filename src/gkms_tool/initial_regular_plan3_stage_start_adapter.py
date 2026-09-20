"""Plan 3 stage-start implementation of the common Initial Regular contract.

This is a thin compatibility layer over :mod:`plan3_rollout_inner_adapter`.
The common request supplies Master-derived stage facts and the outer state;
the caller scenario supplies server/runtime-owned GUIDs, fixed deck orders,
chance state, loadout, start-boundary authority, and the explicit outer patch.
No LocalSave checkpoint or alternative card/effect executor is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, TypeAlias

from .initial_regular_inner_protocol import (
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
    validate_initial_regular_inner_stage_outcome,
    validate_initial_regular_inner_stage_request,
)
from .master_db import DEFAULT_DATABASE
from .plan3_engine import DEFAULT_MASTER_DIR, LESSON_UNKNOWN
from .plan3_exam_start import (
    Plan3ExamStartChance,
    Plan3ExamStartContext,
    Plan3ExamStartInputs,
    Plan3ExamStartLoadout,
)
from .plan3_rollout_inner_adapter import (
    Plan3InnerBattleResolver,
    Plan3InnerOutcomeResolver,
    Plan3InnerPostExamResolution,
    Plan3InnerRolloutPause,
    Plan3InnerRolloutReady,
    Plan3InnerRuntimeRefs,
    Plan3InnerSearchCallable,
    Plan3InnerStageContract,
    adapt_plan3_rollout_inner,
)
from .produce_rollout import ExternalKind, ExternalRequest, ProduceRolloutState


PLAN3_INITIAL_REGULAR_STAGE_START_ADAPTER_ID: Final = (
    "initial-regular.plan3.master-stage-start"
)
PLAN3_INITIAL_REGULAR_PLAN_TYPE: Final = "plan3"

_PARAMETER_BY_ATTRIBUTE: Final = {
    "vocal": 1,
    "dance": 2,
    "visual": 3,
}


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _issue(
    code: str,
    field: str,
    detail: str = "",
) -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan3StageStartScenario:
    """Explicit authority absent from a static Master stage request.

    ``chance`` owns the server-selected PRNG state plus independently created
    GUIDs and ordered-deck facts.  ``start_turn_effects_known_absent`` must be
    explicitly true unless a future runtime-aware bootstrap replaces the
    legacy boundary.  Persistent outer mutations and reward availability are
    supplied by ``post_exam_resolution`` or ``outcome_resolver`` and are never
    derived from the native score.
    """

    expected_stage_id: str
    expected_idol_card_id: str
    chance: Plan3ExamStartChance
    loadout: Plan3ExamStartLoadout
    authority_description: str
    start_turn_effects_known_absent: bool | None
    post_exam_resolution: Plan3InnerPostExamResolution | None = None
    outcome_resolver: Plan3InnerOutcomeResolver | None = None
    runtime_refs: Plan3InnerRuntimeRefs | None = None
    search: Plan3InnerSearchCallable | None = None
    battle_resolver: Plan3InnerBattleResolver | None = None
    beam_width: int = 64
    database: Path = DEFAULT_DATABASE
    master_dir: Path = DEFAULT_MASTER_DIR

    def __post_init__(self) -> None:
        _text(self.expected_stage_id, "expected_stage_id")
        _text(self.expected_idol_card_id, "expected_idol_card_id")
        _text(self.authority_description, "authority_description")
        if not isinstance(self.chance, Plan3ExamStartChance):
            raise TypeError("chance must be Plan3ExamStartChance")
        if not isinstance(self.loadout, Plan3ExamStartLoadout):
            raise TypeError("loadout must be Plan3ExamStartLoadout")
        if self.start_turn_effects_known_absent is not None and not isinstance(
            self.start_turn_effects_known_absent,
            bool,
        ):
            raise TypeError(
                "start_turn_effects_known_absent must be bool or None"
            )
        if self.post_exam_resolution is not None and not isinstance(
            self.post_exam_resolution,
            Plan3InnerPostExamResolution,
        ):
            raise TypeError(
                "post_exam_resolution must be Plan3InnerPostExamResolution or None"
            )
        for name in ("outcome_resolver", "search", "battle_resolver"):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable or None")
        if self.runtime_refs is not None and not isinstance(
            self.runtime_refs,
            Plan3InnerRuntimeRefs,
        ):
            raise TypeError("runtime_refs must be Plan3InnerRuntimeRefs or None")
        _positive_integer(self.beam_width, "beam_width")
        object.__setattr__(self, "database", Path(self.database))
        object.__setattr__(self, "master_dir", Path(self.master_dir))


def _scenario_issues(
    request: InitialRegularInnerStageRequest,
    scenario: InitialRegularPlan3StageStartScenario,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    if request.plan_type != PLAN3_INITIAL_REGULAR_PLAN_TYPE:
        issues.append(
            _issue(
                "plan3-stage-start-plan-type-mismatch",
                "request.plan_type",
                request.plan_type,
            )
        )
    for field, expected, actual in (
        ("stage_id", scenario.expected_stage_id, request.stage_id),
        ("idol_card_id", scenario.expected_idol_card_id, request.idol_card_id),
    ):
        if expected != actual:
            issues.append(
                _issue(
                    f"plan3-stage-start-{field.replace('_', '-')}-mismatch",
                    f"request.{field}",
                    f"scenario={expected}:request={actual}",
                )
            )

    chance = scenario.chance
    if chance.random_root != request.branch.rng_token:
        issues.append(
            _issue(
                "plan3-stage-start-random-root-mismatch",
                "scenario.chance.random_root",
                (
                    f"scenario={chance.random_root}:"
                    f"branch={request.branch.rng_token}"
                ),
            )
        )
    if chance.random_state != request.rng_before:
        issues.append(
            _issue(
                "plan3-stage-start-random-state-mismatch",
                "scenario.chance.random_state",
                f"scenario={chance.random_state}:request={request.rng_before}",
            )
        )

    missing_guid_count = sum(
        entry.count for entry in request.outer_deck if not entry.instance_ids
    )
    if len(chance.instance_guids) != missing_guid_count:
        issues.append(
            _issue(
                "plan3-stage-start-instance-guids-count-mismatch",
                "scenario.chance.instance_guids",
                (
                    f"required={missing_guid_count}:"
                    f"supplied={len(chance.instance_guids)}"
                ),
            )
        )
    deck_count = sum(entry.count for entry in request.outer_deck)
    if len(chance.fixed_deck_orders) != deck_count:
        issues.append(
            _issue(
                "plan3-stage-start-fixed-deck-orders-count-mismatch",
                "scenario.chance.fixed_deck_orders",
                (
                    f"required={deck_count}:"
                    f"supplied={len(chance.fixed_deck_orders)}"
                ),
            )
        )

    if scenario.start_turn_effects_known_absent is None:
        issues.append(
            _issue(
                "plan3-stage-start-effects-unresolved",
                "scenario.start_turn_effects_known_absent",
            )
        )
    elif not scenario.start_turn_effects_known_absent:
        issues.append(
            _issue(
                "plan3-stage-start-effects-unsupported",
                "scenario.start_turn_effects_known_absent",
            )
        )
    if (
        scenario.post_exam_resolution is None
        and scenario.outcome_resolver is None
    ):
        issues.append(
            _issue(
                "plan3-stage-start-post-exam-outcome-unresolved",
                "scenario.post_exam_resolution",
            )
        )
    elif (
        scenario.post_exam_resolution is not None
        and scenario.outcome_resolver is not None
    ):
        issues.append(
            _issue(
                "plan3-stage-start-post-exam-outcome-conflict",
                "scenario.post_exam_resolution/outcome_resolver",
            )
        )

    loadout = scenario.loadout
    for field, expected, actual in (
        ("item_ids", request.outer_state.item_session_refs, loadout.item_ids),
        ("drink_ids", request.outer_state.drink_session_refs, loadout.drink_ids),
        (
            "passive_ids",
            request.outer_state.passive_session_refs,
            loadout.passive_ids,
        ),
    ):
        if expected != actual:
            issues.append(
                _issue(
                    f"plan3-stage-start-loadout-{field.replace('_', '-')}-mismatch",
                    f"scenario.loadout.{field}",
                    f"outer={expected}:scenario={actual}",
                )
            )
    if scenario.search is None and loadout.memory_ids:
        issues.append(
            _issue(
                "plan3-stage-start-default-search-memory-unresolved",
                "scenario.search",
                ",".join(loadout.memory_ids),
            )
        )
    return tuple(issues)


def _current_parameter_type(
    request: InitialRegularInnerStageRequest,
) -> int | None:
    if request.stage_kind is InitialRegularInnerStageKind.LESSON:
        return _PARAMETER_BY_ATTRIBUTE.get(request.attribute or "")
    schedule = request.turn_parameter_types
    return None if not schedule else schedule[0]


def _effective_runtime_refs(
    request: InitialRegularInnerStageRequest,
    scenario: InitialRegularPlan3StageStartScenario,
) -> Plan3InnerRuntimeRefs:
    if scenario.runtime_refs is not None:
        return scenario.runtime_refs
    assert request.setting_id is not None
    assert request.gimmick_group_id is not None
    return Plan3InnerRuntimeRefs(
        setting_id=request.setting_id,
        gimmick_ref=request.gimmick_group_id,
        item_refs=request.outer_state.item_session_refs,
        support_refs=scenario.loadout.support_card_ids,
        drink_refs=request.outer_state.drink_session_refs,
        passive_refs=request.outer_state.passive_session_refs,
        battle_parameter_schedule=(
            request.turn_parameter_types
            if request.stage_kind is InitialRegularInnerStageKind.AUDITION
            else None
        ),
    )


InitialRegularPlan3ExamStartInputsResult: TypeAlias = (
    Plan3ExamStartInputs | tuple[InitialRegularInnerStageIssue, ...]
)


def build_initial_regular_plan3_exam_start_inputs(
    request: InitialRegularInnerStageRequest,
    scenario: InitialRegularPlan3StageStartScenario,
) -> InitialRegularPlan3ExamStartInputsResult:
    """Map one validated common request into the legacy native bootstrap."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    if not isinstance(scenario, InitialRegularPlan3StageStartScenario):
        raise TypeError("scenario must be InitialRegularPlan3StageStartScenario")
    request_issues = validate_initial_regular_inner_stage_request(request)
    if request_issues:
        return request_issues
    scenario_issues = _scenario_issues(request, scenario)
    if scenario_issues:
        return scenario_issues

    parameter_type = _current_parameter_type(request)
    if parameter_type is None:
        return (
            _issue(
                "plan3-stage-start-current-parameter-unresolved",
                (
                    "request.attribute"
                    if request.stage_kind is InitialRegularInnerStageKind.LESSON
                    else "request.turn_parameter_types"
                ),
            ),
        )
    assert request.stage_id is not None
    assert request.setting_id is not None
    assert request.clear_border is not None
    assert request.limit_border is not None
    assert request.battle_bonus_permille is not None
    assert request.turns is not None
    return Plan3ExamStartInputs(
        deck=request.outer_deck,
        context=Plan3ExamStartContext(
            stage_id=request.stage_id,
            setting_id=request.setting_id,
            turns=request.turns,
            current_parameter_type=parameter_type,
            lesson_type=(
                request.lesson_type
                if request.stage_kind is InitialRegularInnerStageKind.LESSON
                else LESSON_UNKNOWN
            ),
            step_type_value=(
                request.step_type_value
                if request.stage_kind is InitialRegularInnerStageKind.LESSON
                else 0
            ),
            is_battle=(
                request.stage_kind is InitialRegularInnerStageKind.AUDITION
            ),
            clear_border=request.clear_border,
            limit_border=request.limit_border,
            battle_bonus_permille=request.battle_bonus_permille,
            gimmick_group_id=request.gimmick_group_id or "",
        ),
        chance=scenario.chance,
        attributes=request.outer_state.attributes,
        stamina=request.outer_state.stamina,
        max_stamina=request.outer_state.max_stamina,
        loadout=scenario.loadout,
        start_turn_effects_known_absent=(
            scenario.start_turn_effects_known_absent
        ),
    )


def _legacy_request(
    request: InitialRegularInnerStageRequest,
) -> tuple[ProduceRolloutState, ExternalRequest]:
    pending = request.outer_state.pending
    assert pending is not None
    if request.stage_kind is InitialRegularInnerStageKind.AUDITION:
        return request.outer_state, pending
    translated = ExternalRequest(
        request_id=pending.request_id,
        kind=ExternalKind.INNER_EXAM_OUTCOME,
        week=pending.week,
        action_id=pending.action_id,
        stage_type=request.stage_id,
        adapter_ref=pending.adapter_ref,
        required_fields=pending.required_fields,
    )
    return replace(request.outer_state, pending=translated), translated


@dataclass(frozen=True, slots=True)
class InitialRegularPlan3StageStartAdapter:
    """Callable common adapter backed by the existing Plan 3 start runner."""

    scenario: InitialRegularPlan3StageStartScenario

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, InitialRegularPlan3StageStartScenario):
            raise TypeError(
                "scenario must be InitialRegularPlan3StageStartScenario"
            )

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularInnerStageOutcome | tuple[InitialRegularInnerStageIssue, ...]:
        prepared = build_initial_regular_plan3_exam_start_inputs(
            request,
            self.scenario,
        )
        if isinstance(prepared, tuple):
            return prepared
        legacy_state, legacy_request = _legacy_request(request)
        runtime_refs = _effective_runtime_refs(request, self.scenario)
        if request.stage_kind is InitialRegularInnerStageKind.AUDITION:
            expected_schedule = request.turn_parameter_types
            if runtime_refs.battle_parameter_schedule != expected_schedule:
                return (
                    _issue(
                        "plan3-stage-start-battle-schedule-mismatch",
                        "scenario.runtime_refs.battle_parameter_schedule",
                        (
                            f"request={expected_schedule}:"
                            f"runtime={runtime_refs.battle_parameter_schedule}"
                        ),
                    ),
                )
        elif runtime_refs.battle_parameter_schedule not in (None, ()):
            return (
                _issue(
                    "plan3-stage-start-lesson-battle-schedule-invalid",
                    "scenario.runtime_refs.battle_parameter_schedule",
                ),
            )
        try:
            legacy = adapt_plan3_rollout_inner(
                legacy_state,
                legacy_request,
                prepared,
                branch=request.branch,
                runtime_refs=runtime_refs,
                search=self.scenario.search,
                beam_width=self.scenario.beam_width,
                battle_resolver=self.scenario.battle_resolver,
                post_exam_resolution=self.scenario.post_exam_resolution,
                outcome_resolver=self.scenario.outcome_resolver,
                stage_contract=(
                    Plan3InnerStageContract(terminal_stage_types=())
                    if request.stage_kind is InitialRegularInnerStageKind.LESSON
                    else Plan3InnerStageContract()
                ),
                database=self.scenario.database,
                master_dir=self.scenario.master_dir,
            )
        except Exception as error:
            return (
                _issue(
                    "plan3-stage-start-legacy-adapter-failed",
                    "plan3.legacy_adapter",
                    f"{type(error).__name__}:{error}",
                ),
            )
        if isinstance(legacy, Plan3InnerRolloutPause):
            return tuple(
                _issue(
                    f"plan3-stage-start:{value.code}",
                    f"plan3.legacy_adapter.{value.field}",
                    value.detail,
                )
                for value in legacy.issues
            )
        if not isinstance(legacy, Plan3InnerRolloutReady):
            return (
                _issue(
                    "plan3-stage-start-legacy-result-invalid",
                    "plan3.legacy_adapter",
                ),
            )

        best = getattr(legacy.search_result, "best", None)
        if best is None:
            return (
                _issue(
                    "plan3-stage-start-terminal-best-unresolved",
                    "plan3.legacy_adapter.search_result.best",
                ),
            )
        action_guids = tuple(getattr(best, "card_guids", ()))
        if any(not isinstance(value, str) or not value for value in action_guids):
            return (
                _issue(
                    "plan3-stage-start-action-guid-invalid",
                    "plan3.legacy_adapter.search_result.best.card_guids",
                ),
            )
        rank = (
            None
            if legacy.battle_resolution is None
            else legacy.battle_resolution.rank
        )
        outcome = InitialRegularInnerStageOutcome(
            request_id=request.external_request_id,
            branch=request.branch,
            patch=legacy.outcome.patch,
            cleared=legacy.outcome.cleared,
            terminal=legacy.outcome.terminal,
            reward_requested=legacy.outcome.reward_requested,
            rng_after=best.native_state.random_state,
            score=best.state.score,
            rank=rank,
            trace=(
                f"adapter:{PLAN3_INITIAL_REGULAR_STAGE_START_ADAPTER_ID}",
                "stage-facts-authority:common-master-request",
                "runtime-authority:caller-deterministic-scenario",
                f"authority:{self.scenario.authority_description}",
                *(
                    f"opening-hand-guid:{card.guid}"
                    for card in legacy.bootstrap.native_state.hand
                ),
                *(f"card-guid:{guid}" for guid in action_guids),
                f"terminal-score:{best.state.score}",
                "local-save-checkpoint:false",
            ),
        )
        outcome_issues = validate_initial_regular_inner_stage_outcome(
            request,
            outcome,
        )
        return outcome_issues or outcome


def build_initial_regular_plan3_stage_start_adapter(
    scenario: InitialRegularPlan3StageStartScenario,
) -> InitialRegularPlan3StageStartAdapter:
    """Build a callable suitable for the common adapter registry."""

    return InitialRegularPlan3StageStartAdapter(scenario)


InitialRegularPlan3StageStartAdapterResult: TypeAlias = (
    InitialRegularInnerStageOutcome
    | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "PLAN3_INITIAL_REGULAR_STAGE_START_ADAPTER_ID",
    "InitialRegularPlan3ExamStartInputsResult",
    "InitialRegularPlan3StageStartAdapter",
    "InitialRegularPlan3StageStartAdapterResult",
    "InitialRegularPlan3StageStartScenario",
    "build_initial_regular_plan3_exam_start_inputs",
    "build_initial_regular_plan3_stage_start_adapter",
]
