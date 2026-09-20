"""Common inner/weekly projections for one resolved NIA audition scenario.

The adapter composes the deterministic player-side NIA terminal with the
caller-resolved NPC/reward resolver.  It does not load or roll NPC runtime.
Both the common inner protocol and a weekly-action projection reuse the same
rank result and exact post-exam stamina/RNG state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from .audition_rules import FINAL, MID1, MID2
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageKind,
    InitialRegularInnerStageOutcome,
    InitialRegularInnerStageRequest,
    validate_initial_regular_inner_stage_outcome,
    validate_initial_regular_inner_stage_request,
)
from .initial_regular_nia_audition_resolver import (
    NiaAuditionTerminalResolution,
    NiaAuditionTerminalResolverResult,
    NiaAuditionTerminalScenario,
    resolve_initial_regular_nia_audition,
)
from .master_db import DEFAULT_DATABASE
from .nia_inner_terminal_acceptance import NiaInnerTerminalAcceptance
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .nia_static_inventory import NIA_PLAN_TYPE, NiaStaticAuditionStage
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    ProduceStatePatch,
    RolloutPhase,
    WeeklyActionOutcome,
)


INITIAL_REGULAR_NIA_INNER_ADAPTER_ID: Final = (
    "initial-regular.nia.audition-terminal"
)
INITIAL_REGULAR_NIA_PLAN_TYPES: Final = frozenset({NIA_PLAN_TYPE, "plan3"})
_STEP_TYPE_VALUE = {MID1: 16, MID2: 17, FINAL: 18}


def _issue(
    code: str,
    field: str,
    detail: str = "",
) -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _dedupe(
    issues: list[InitialRegularInnerStageIssue]
    | tuple[InitialRegularInnerStageIssue, ...],
) -> tuple[InitialRegularInnerStageIssue, ...]:
    return tuple(dict.fromkeys(issues))


def _append_mismatch(
    issues: list[InitialRegularInnerStageIssue],
    field: str,
    actual: object,
    expected: object,
) -> None:
    if actual != expected:
        issues.append(
            _issue(
                "nia-inner-context-mismatch",
                field,
                f"expected={expected}:actual={actual}",
            )
        )


def _outer_runtime_issues(
    state: ProduceRolloutState,
    acceptance: NiaInnerTerminalAcceptance,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    fixture = acceptance.fixture
    runtime = fixture.runtime
    _append_mismatch(
        issues,
        "outer_state.mode_id",
        state.mode_id,
        fixture.selector.produce_id,
    )
    _append_mismatch(
        issues,
        "outer_state.stamina",
        state.stamina,
        runtime.stamina,
    )
    _append_mismatch(
        issues,
        "outer_state.max_stamina",
        state.max_stamina,
        runtime.max_stamina,
    )

    expected_cards = {
        card.guid: (card.card_id, card.effective_upgrade)
        for card in fixture.ordered_deck.cards
    }
    actual_cards: dict[str, tuple[str, int]] = {}
    missing_guid_entries: list[int] = []
    for index, entry in enumerate(state.deck):
        if not entry.instance_ids:
            missing_guid_entries.append(index)
            continue
        for guid in entry.instance_ids:
            if guid in actual_cards:
                issues.append(
                    _issue(
                        "nia-inner-outer-deck-guid-duplicate",
                        f"outer_state.deck[{index}].instance_ids",
                        guid,
                    )
                )
            actual_cards[guid] = (entry.card_id, entry.upgrade)
    if missing_guid_entries:
        issues.append(
            _issue(
                "nia-inner-outer-deck-guid-unresolved",
                "outer_state.deck.instance_ids",
                ",".join(str(value) for value in missing_guid_entries),
            )
        )
    if actual_cards != expected_cards:
        issues.append(
            _issue(
                "nia-inner-outer-deck-mismatch",
                "outer_state.deck",
                f"expected={len(expected_cards)}:actual={len(actual_cards)}",
            )
        )
    for field in (
        "item_session_refs",
        "drink_session_refs",
        "passive_session_refs",
    ):
        values = getattr(state, field)
        if values:
            issues.append(
                _issue(
                    "nia-inner-unmodelled-runtime-refs",
                    f"outer_state.{field}",
                    ",".join(values),
                )
            )
    return _dedupe(issues)


def _common_request_issues(
    request: InitialRegularInnerStageRequest,
    acceptance: NiaInnerTerminalAcceptance,
    catalog: NiaStaticAuditionStage,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues = list(validate_initial_regular_inner_stage_request(request))
    if request.stage_kind is not InitialRegularInnerStageKind.AUDITION:
        issues.append(
            _issue(
                "nia-inner-stage-kind-invalid",
                "request.stage_kind",
                request.stage_kind.value,
            )
        )
    if request.plan_type not in INITIAL_REGULAR_NIA_PLAN_TYPES:
        issues.append(
            _issue(
                "nia-inner-plan-type-mismatch",
                "request.plan_type",
                request.plan_type,
            )
        )
    expected_limit_border = (
        catalog.limit_border if catalog.limit_border > 0 else -1
    )
    fixture = acceptance.fixture
    expected_bonus = (
        acceptance.initial_state.battle_bonus_permille_vocal,
        acceptance.initial_state.battle_bonus_permille_dance,
        acceptance.initial_state.battle_bonus_permille_visual,
    )
    checks = (
        ("request.idol_card_id", request.idol_card_id, fixture.selector.idol_card_id),
        ("request.stage_type", request.stage_type, catalog.step_type),
        ("request.stage_id", request.stage_id, catalog.stage_id),
        ("request.setting_id", request.setting_id, catalog.exam_setting_id),
        ("request.exam_type", request.exam_type, 1),
        (
            "request.step_type_value",
            request.step_type_value,
            _STEP_TYPE_VALUE[catalog.step_type],
        ),
        ("request.limit_turn", request.limit_turn, catalog.turns),
        ("request.extra_turn", request.extra_turn, 0),
        ("request.clear_border", request.clear_border, catalog.clear_border),
        ("request.limit_border", request.limit_border, expected_limit_border),
        (
            "request.rng_before",
            request.rng_before,
            acceptance.initial_native_state.random_state,
        ),
        ("request.battle_bonus_permille", request.battle_bonus_permille, expected_bonus),
        (
            "request.gimmick_group_id",
            request.gimmick_group_id,
            catalog.gimmick_group_id,
        ),
        (
            "request.turn_parameter_types",
            request.turn_parameter_types,
            acceptance.battle_parameter_schedule,
        ),
    )
    for field, actual, expected in checks:
        _append_mismatch(issues, field, actual, expected)
    issues.extend(_outer_runtime_issues(request.outer_state, acceptance))
    return _dedupe(issues)


def _outcome_from_resolution(
    *,
    request_id: str,
    branch: ChanceBranch,
    acceptance: NiaInnerTerminalAcceptance,
    catalog: NiaStaticAuditionStage,
    resolution: NiaAuditionTerminalResolution,
) -> InitialRegularInnerStageOutcome:
    best = acceptance.best
    if best is None or not best.complete:
        raise ValueError("NIA player terminal is incomplete")
    return InitialRegularInnerStageOutcome(
        request_id=request_id,
        branch=branch,
        patch=ProduceStatePatch(stamina=best.state.stamina),
        cleared=resolution.cleared,
        terminal=catalog.step_type == FINAL,
        reward_requested=resolution.reward_requested,
        rng_after=best.native_state.random_state,
        score=resolution.player_score,
        rank=resolution.rank,
        trace=(
            f"adapter:{INITIAL_REGULAR_NIA_INNER_ADAPTER_ID}",
            "player-runtime-authority:caller-authored-synthetic-fixture",
            "npc-runtime-authority:caller-resolved-terminal-tracks",
            f"master-stage:{catalog.stage_id}",
            *resolution.trace,
        ),
    )


@dataclass(frozen=True, slots=True)
class InitialRegularNiaInnerAdapter:
    """Scenario-bound callable implementing the common inner contract."""

    acceptance: NiaInnerTerminalAcceptance
    scenario: NiaAuditionTerminalScenario
    catalog: NiaStaticAuditionStage | None = None
    master_dir: Path = DEFAULT_MASTER_DIR
    database: Path = DEFAULT_DATABASE

    def __post_init__(self) -> None:
        if not isinstance(self.acceptance, NiaInnerTerminalAcceptance):
            raise TypeError("acceptance must be NiaInnerTerminalAcceptance")
        if not isinstance(self.scenario, NiaAuditionTerminalScenario):
            raise TypeError("scenario must be NiaAuditionTerminalScenario")
        if self.catalog is not None and not isinstance(
            self.catalog,
            NiaStaticAuditionStage,
        ):
            raise TypeError("catalog must be NiaStaticAuditionStage or None")
        object.__setattr__(self, "master_dir", Path(self.master_dir))
        object.__setattr__(self, "database", Path(self.database))

    def resolve(self) -> NiaAuditionTerminalResolverResult:
        return resolve_initial_regular_nia_audition(
            self.acceptance,
            self.scenario,
            catalog=self.catalog,
            master_dir=self.master_dir,
            database=self.database,
        )

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularInnerStageOutcome | tuple[InitialRegularInnerStageIssue, ...]:
        if not isinstance(request, InitialRegularInnerStageRequest):
            raise TypeError("request must be InitialRegularInnerStageRequest")
        resolved = self.resolve()
        if not resolved.ready:
            return resolved.issues
        catalog = resolved.catalog
        resolution = resolved.resolution
        assert catalog is not None and resolution is not None
        issues = _common_request_issues(request, self.acceptance, catalog)
        if issues:
            return issues
        outcome = _outcome_from_resolution(
            request_id=request.external_request_id,
            branch=request.branch,
            acceptance=self.acceptance,
            catalog=catalog,
            resolution=resolution,
        )
        outcome_issues = validate_initial_regular_inner_stage_outcome(
            request,
            outcome,
        )
        return outcome_issues or outcome

    def weekly(
        self,
        outer_state: ProduceRolloutState,
        request: ExternalRequest,
        branch: ChanceBranch,
    ) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
        """Project the same rank result onto a pending weekly request."""

        resolved = self.resolve()
        if not resolved.ready:
            return resolved.issues
        catalog = resolved.catalog
        resolution = resolved.resolution
        assert catalog is not None and resolution is not None
        return project_initial_regular_nia_weekly_action_outcome(
            outer_state,
            request,
            branch,
            self.acceptance,
            catalog,
            resolution,
        )


def project_initial_regular_nia_weekly_action_outcome(
    outer_state: ProduceRolloutState,
    request: ExternalRequest,
    branch: ChanceBranch,
    acceptance: NiaInnerTerminalAcceptance,
    catalog: NiaStaticAuditionStage,
    resolution: NiaAuditionTerminalResolution,
) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
    """Project a resolved NIA inner result onto a pending weekly node."""

    if not isinstance(outer_state, ProduceRolloutState):
        raise TypeError("outer_state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(branch, ChanceBranch):
        raise TypeError("branch must be ChanceBranch")
    if not isinstance(acceptance, NiaInnerTerminalAcceptance):
        raise TypeError("acceptance must be NiaInnerTerminalAcceptance")
    if not isinstance(catalog, NiaStaticAuditionStage):
        raise TypeError("catalog must be NiaStaticAuditionStage")
    if not isinstance(resolution, NiaAuditionTerminalResolution):
        raise TypeError("resolution must be NiaAuditionTerminalResolution")
    issues: list[InitialRegularInnerStageIssue] = []
    if outer_state.phase is not RolloutPhase.WAITING_EXTERNAL:
        issues.append(
            _issue(
                "nia-weekly-outer-request-not-pending",
                "outer_state.phase",
            )
        )
    if outer_state.pending != request:
        issues.append(
            _issue(
                "nia-weekly-pending-request-mismatch",
                "outer_state.pending",
            )
        )
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(
            _issue(
                "nia-weekly-request-kind-mismatch",
                "request.kind",
                request.kind.value,
            )
        )
    if request.week != outer_state.week:
        issues.append(
            _issue(
                "nia-weekly-request-week-mismatch",
                "request.week",
            )
        )
    if request.stage_type != catalog.step_type:
        issues.append(
            _issue(
                "nia-weekly-stage-type-mismatch",
                "request.stage_type",
                f"expected={catalog.step_type}:actual={request.stage_type}",
            )
        )
    if branch.probability is None or branch.probability <= 0.0:
        issues.append(
            _issue(
                "nia-weekly-branch-probability-unresolved",
                "branch.probability",
            )
        )
    issues.extend(_outer_runtime_issues(outer_state, acceptance))
    if issues:
        return _dedupe(issues)
    inner = _outcome_from_resolution(
        request_id=request.request_id,
        branch=branch,
        acceptance=acceptance,
        catalog=catalog,
        resolution=resolution,
    )
    return WeeklyActionOutcome(
        request_id=inner.request_id,
        branch=inner.branch,
        patch=inner.patch,
        reward_requested=inner.reward_requested,
    )


def build_initial_regular_nia_inner_adapter(
    acceptance: NiaInnerTerminalAcceptance,
    scenario: NiaAuditionTerminalScenario,
    **kwargs: object,
) -> InitialRegularNiaInnerAdapter:
    return InitialRegularNiaInnerAdapter(
        acceptance,
        scenario,
        **kwargs,  # type: ignore[arg-type]
    )


InitialRegularNiaInnerAdapterResult: TypeAlias = (
    InitialRegularInnerStageOutcome
    | WeeklyActionOutcome
    | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "INITIAL_REGULAR_NIA_INNER_ADAPTER_ID",
    "INITIAL_REGULAR_NIA_PLAN_TYPES",
    "InitialRegularNiaInnerAdapter",
    "InitialRegularNiaInnerAdapterResult",
    "build_initial_regular_nia_inner_adapter",
    "project_initial_regular_nia_weekly_action_outcome",
]
