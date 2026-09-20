"""Caller-authoritative Plan 1 stage-start scalar provisioning.

The native Plan 1 stage deliberately does not infer an exam checkpoint from
Master, a loadout, or the outer rollout shadow.  This module binds those two
typed boundaries instead: callers provide the complete initial scalar state,
attribute and multiplier inputs, RNG state, and the exact outer-state
identity against which an Initial Regular request must be checked.

The synthetic terminal factory is acceptance-fixture authority only.  Its
values are explicitly labelled synthetic and are not evidence for real
Master/loadout stage-start values.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from .initial_regular_inner_protocol import (
    InitialRegularInnerStageKind,
    InitialRegularInnerStageRequest,
)
from .plan1_native_core import Plan1ScalarState
from .plan1_native_stage import Plan1NativeStageState
from .produce_rollout import (
    AttributeValues,
    DeckEntry,
    ProduceRolloutState,
    RolloutPhase,
)


PLAN1_SYNTHETIC_TERMINAL_INITIAL_STAMINA: Final = 100
PLAN1_SYNTHETIC_TERMINAL_MAX_STAMINA: Final = 100
PLAN1_SYNTHETIC_TERMINAL_INITIAL_SCORE: Final = 0
PLAN1_SYNTHETIC_TERMINAL_BATTLE_BONUS_PERMILLE: Final = (1000, 1000, 1000)
PLAN1_SYNTHETIC_TERMINAL_LESSON_MULTIPLE_PERMILLE: Final = 1000


class Plan1StageBootstrapAuthority(StrEnum):
    """Authority class for the caller-provided stage-start checkpoint."""

    CALLER_RUNTIME = "caller_runtime"
    SYNTHETIC_FIXED_SMOKE = "synthetic_fixed_smoke"
    SYNTHETIC_TERMINAL_FIXTURE = "synthetic_terminal_fixture"


class Plan1StageOuterIdentityScope(StrEnum):
    """Whether an identity binds a common request or a standalone fixture."""

    OUTER_REQUEST = "outer_request"
    STANDALONE_FIXTURE = "standalone_fixture"


@dataclass(frozen=True, slots=True)
class Plan1StageOuterStateIdentity:
    """Exact typed outer snapshot and request join key for one Plan 1 start."""

    scope: Plan1StageOuterIdentityScope
    external_request_id: str
    action_id: str
    stage_kind: InitialRegularInnerStageKind
    stage_type: str | None
    outer_state: ProduceRolloutState

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Plan1StageOuterIdentityScope):
            raise TypeError("scope must be Plan1StageOuterIdentityScope")
        for name in ("external_request_id", "action_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        if not isinstance(self.stage_kind, InitialRegularInnerStageKind):
            raise TypeError("stage_kind must be InitialRegularInnerStageKind")
        if self.stage_type is not None and (
            not isinstance(self.stage_type, str) or not self.stage_type
        ):
            raise ValueError("stage_type must be non-empty text or None")
        if not isinstance(self.outer_state, ProduceRolloutState):
            raise TypeError("outer_state must be ProduceRolloutState")

    @classmethod
    def from_request(
        cls, request: InitialRegularInnerStageRequest
    ) -> "Plan1StageOuterStateIdentity":
        if not isinstance(request, InitialRegularInnerStageRequest):
            raise TypeError("request must be InitialRegularInnerStageRequest")
        return cls(
            scope=Plan1StageOuterIdentityScope.OUTER_REQUEST,
            external_request_id=request.external_request_id,
            action_id=request.action_id,
            stage_kind=request.stage_kind,
            stage_type=request.stage_type,
            outer_state=request.outer_state,
        )


@dataclass(frozen=True, slots=True)
class Plan1StageBootstrap:
    """Complete caller-authoritative scalar inputs for one native Plan 1 run.

    ``initial_state`` contains the initial score, stamina, and every currently
    represented Plan 1 status counter.  Attributes and battle bonus remain
    separate because they belong to the exam context rather than
    :class:`Plan1ScalarState`.
    """

    authority: Plan1StageBootstrapAuthority
    authority_detail: str
    outer_state_identity: Plan1StageOuterStateIdentity
    initial_state: Plan1ScalarState
    attributes: AttributeValues
    battle_bonus_permille: tuple[int, int, int]
    lesson_buff_multiple_permille: int
    rng_before: int

    def __post_init__(self) -> None:
        if not isinstance(self.authority, Plan1StageBootstrapAuthority):
            raise TypeError("authority must be Plan1StageBootstrapAuthority")
        if not isinstance(self.authority_detail, str) or not self.authority_detail:
            raise ValueError("authority_detail must be non-empty text")
        if not isinstance(self.outer_state_identity, Plan1StageOuterStateIdentity):
            raise TypeError(
                "outer_state_identity must be Plan1StageOuterStateIdentity"
            )
        if not isinstance(self.initial_state, Plan1ScalarState):
            raise TypeError("initial_state must be Plan1ScalarState")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("attributes must be AttributeValues")
        bonuses = tuple(self.battle_bonus_permille)
        if len(bonuses) != 3 or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in bonuses
        ):
            raise ValueError(
                "battle_bonus_permille must contain three non-negative integers"
            )
        object.__setattr__(self, "battle_bonus_permille", bonuses)
        if (
            isinstance(self.lesson_buff_multiple_permille, bool)
            or not isinstance(self.lesson_buff_multiple_permille, int)
            or self.lesson_buff_multiple_permille < 0
        ):
            raise ValueError(
                "lesson_buff_multiple_permille must be a non-negative integer"
            )
        if (
            self.initial_state.lesson_buff_multiple_permille
            != self.lesson_buff_multiple_permille
        ):
            raise ValueError(
                "initial_state lesson multiplier disagrees with bootstrap"
            )
        if (
            isinstance(self.rng_before, bool)
            or not isinstance(self.rng_before, int)
            or not 0 <= self.rng_before <= 0xFFFFFFFF
        ):
            raise ValueError("rng_before must be a UInt32 integer")

    @property
    def initial_stamina(self) -> int:
        return self.initial_state.stamina

    @property
    def max_stamina(self) -> int:
        return self.initial_state.max_stamina

    @property
    def initial_score(self) -> int:
        return self.initial_state.score


@dataclass(frozen=True, slots=True)
class Plan1StageBootstrapIssue:
    """Typed fail-closed bootstrap/request or bootstrap/stage mismatch."""

    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("code must be non-empty text")
        if not isinstance(self.field, str) or not self.field:
            raise ValueError("field must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("detail must be text")


@dataclass(frozen=True, slots=True)
class Plan1StageBootstrapProvision:
    """Provisioned stage or the exact typed issues that prevented it."""

    bootstrap: Plan1StageBootstrap | None
    stage: Plan1NativeStageState | None
    issues: tuple[Plan1StageBootstrapIssue, ...] = ()

    def __post_init__(self) -> None:
        if self.bootstrap is not None and not isinstance(
            self.bootstrap, Plan1StageBootstrap
        ):
            raise TypeError("bootstrap must be Plan1StageBootstrap or None")
        if self.stage is not None and not isinstance(
            self.stage, Plan1NativeStageState
        ):
            raise TypeError("stage must be Plan1NativeStageState or None")
        issues = tuple(self.issues)
        if any(not isinstance(value, Plan1StageBootstrapIssue) for value in issues):
            raise TypeError("issues must contain Plan1StageBootstrapIssue values")
        object.__setattr__(self, "issues", issues)
        if bool(self.stage is None) == bool(not issues):
            raise ValueError("provision must contain exactly a stage or issues")

    @property
    def ready(self) -> bool:
        return self.stage is not None and not self.issues


def _issue(code: str, field: str, detail: str = "") -> Plan1StageBootstrapIssue:
    return Plan1StageBootstrapIssue(code, field, detail)


def validate_plan1_stage_bootstrap_request(
    request: InitialRegularInnerStageRequest,
    bootstrap: Plan1StageBootstrap | None,
) -> tuple[Plan1StageBootstrapIssue, ...]:
    """Validate exact common-request identity and stage-start context inputs."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    if bootstrap is None:
        return (
            _issue(
                "plan1-bootstrap-missing",
                "scenario.bootstrap",
                "caller must supply a Plan1StageBootstrap",
            ),
        )
    if not isinstance(bootstrap, Plan1StageBootstrap):
        raise TypeError("bootstrap must be Plan1StageBootstrap or None")

    issues: list[Plan1StageBootstrapIssue] = []
    expected_identity = Plan1StageOuterStateIdentity.from_request(request)
    if bootstrap.outer_state_identity.scope is not Plan1StageOuterIdentityScope.OUTER_REQUEST:
        issues.append(
            _issue(
                "plan1-bootstrap-outer-scope-mismatch",
                "bootstrap.outer_state_identity.scope",
                f"expected={Plan1StageOuterIdentityScope.OUTER_REQUEST.value}:"
                f"actual={bootstrap.outer_state_identity.scope.value}",
            )
        )
    if bootstrap.outer_state_identity != expected_identity:
        issues.append(
            _issue(
                "plan1-bootstrap-outer-identity-mismatch",
                "bootstrap.outer_state_identity",
                f"expected_request={request.external_request_id}:"
                f"actual_request={bootstrap.outer_state_identity.external_request_id}",
            )
        )
    if bootstrap.attributes != request.outer_state.attributes:
        issues.append(
            _issue(
                "plan1-bootstrap-attributes-mismatch",
                "bootstrap.attributes",
                f"outer={request.outer_state.attributes}:bootstrap={bootstrap.attributes}",
            )
        )
    if bootstrap.initial_state.max_stamina != request.outer_state.max_stamina:
        issues.append(
            _issue(
                "plan1-bootstrap-max-stamina-mismatch",
                "bootstrap.initial_state.max_stamina",
                f"outer={request.outer_state.max_stamina}:"
                f"bootstrap={bootstrap.initial_state.max_stamina}",
            )
        )
    if request.battle_bonus_permille is None:
        issues.append(
            _issue(
                "plan1-bootstrap-request-battle-bonus-missing",
                "request.battle_bonus_permille",
            )
        )
    elif bootstrap.battle_bonus_permille != request.battle_bonus_permille:
        issues.append(
            _issue(
                "plan1-bootstrap-battle-bonus-mismatch",
                "bootstrap.battle_bonus_permille",
                f"request={request.battle_bonus_permille}:"
                f"bootstrap={bootstrap.battle_bonus_permille}",
            )
        )
    if request.rng_before is None:
        issues.append(
            _issue(
                "plan1-bootstrap-request-rng-missing",
                "request.rng_before",
            )
        )
    elif bootstrap.rng_before != request.rng_before:
        issues.append(
            _issue(
                "plan1-bootstrap-rng-mismatch",
                "bootstrap.rng_before",
                f"request={request.rng_before}:bootstrap={bootstrap.rng_before}",
            )
        )
    return tuple(issues)


def validate_plan1_stage_bootstrap_fixture(
    stage: Plan1NativeStageState,
    bootstrap: Plan1StageBootstrap | None,
) -> tuple[Plan1StageBootstrapIssue, ...]:
    """Validate that a concrete stage is exactly the provisioned checkpoint."""

    if not isinstance(stage, Plan1NativeStageState):
        raise TypeError("stage must be Plan1NativeStageState")
    if bootstrap is None:
        return (
            _issue(
                "plan1-bootstrap-missing",
                "fixture.bootstrap",
                "terminal fixture has no stage bootstrap",
            ),
        )
    if not isinstance(bootstrap, Plan1StageBootstrap):
        raise TypeError("bootstrap must be Plan1StageBootstrap or None")
    issues: list[Plan1StageBootstrapIssue] = []
    if stage.scalar != bootstrap.initial_state:
        issues.append(
            _issue(
                "plan1-bootstrap-scalar-mismatch",
                "fixture.stage.scalar",
            )
        )
    if stage.zones.random_state != bootstrap.rng_before:
        issues.append(
            _issue(
                "plan1-bootstrap-zone-rng-mismatch",
                "fixture.stage.zones.random_state",
                f"stage={stage.zones.random_state}:bootstrap={bootstrap.rng_before}",
            )
        )
    return tuple(issues)


def provision_plan1_stage(
    request: InitialRegularInnerStageRequest,
    bootstrap: Plan1StageBootstrap | None,
    stage: Plan1NativeStageState,
) -> Plan1StageBootstrapProvision:
    """Apply one validated bootstrap scalar to an existing ordered-zone stage."""

    if not isinstance(stage, Plan1NativeStageState):
        raise TypeError("stage must be Plan1NativeStageState")
    issues = list(validate_plan1_stage_bootstrap_request(request, bootstrap))
    if bootstrap is not None and stage.zones.random_state != bootstrap.rng_before:
        issues.append(
            _issue(
                "plan1-bootstrap-zone-rng-mismatch",
                "fixture.stage.zones.random_state",
                f"stage={stage.zones.random_state}:bootstrap={bootstrap.rng_before}",
            )
        )
    if issues:
        return Plan1StageBootstrapProvision(bootstrap, None, tuple(issues))
    assert bootstrap is not None
    return Plan1StageBootstrapProvision(
        bootstrap,
        replace(stage, scalar=bootstrap.initial_state),
    )


def build_synthetic_plan1_terminal_stage_bootstrap(
    *,
    outer_deck: tuple[DeckEntry, ...] = (),
) -> Plan1StageBootstrap:
    """Build the explicit standalone terminal fixture checkpoint.

    This is synthetic acceptance authority.  It is not derived from Master,
    loadout calculation, LocalSave, or a server stage-start response.
    """

    deck = tuple(outer_deck)
    if any(not isinstance(value, DeckEntry) for value in deck):
        raise TypeError("outer_deck must contain DeckEntry values")
    attributes = AttributeValues(vocal=0, dance=0, visual=0)
    outer_state = ProduceRolloutState(
        mode_id="synthetic-plan1-terminal",
        character_id="fktn",
        week=1,
        total_weeks=1,
        phase=RolloutPhase.READY_FOR_WEEK,
        stamina=PLAN1_SYNTHETIC_TERMINAL_INITIAL_STAMINA,
        max_stamina=PLAN1_SYNTHETIC_TERMINAL_MAX_STAMINA,
        attributes=attributes,
        deck=deck,
    )
    identity = Plan1StageOuterStateIdentity(
        scope=Plan1StageOuterIdentityScope.STANDALONE_FIXTURE,
        external_request_id="synthetic:plan1-terminal-fixture",
        action_id="synthetic-plan1-terminal",
        stage_kind=InitialRegularInnerStageKind.LESSON,
        stage_type=None,
        outer_state=outer_state,
    )
    scalar = Plan1ScalarState(
        score=PLAN1_SYNTHETIC_TERMINAL_INITIAL_SCORE,
        stamina=PLAN1_SYNTHETIC_TERMINAL_INITIAL_STAMINA,
        max_stamina=PLAN1_SYNTHETIC_TERMINAL_MAX_STAMINA,
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
            PLAN1_SYNTHETIC_TERMINAL_LESSON_MULTIPLE_PERMILLE
        ),
        anti_debuff_count=0,
        total_effect_draw_card_count=0,
    )
    return Plan1StageBootstrap(
        authority=Plan1StageBootstrapAuthority.SYNTHETIC_TERMINAL_FIXTURE,
        authority_detail=(
            "synthetic standalone terminal acceptance checkpoint; "
            "not Master/loadout authority"
        ),
        outer_state_identity=identity,
        initial_state=scalar,
        attributes=attributes,
        battle_bonus_permille=(
            PLAN1_SYNTHETIC_TERMINAL_BATTLE_BONUS_PERMILLE
        ),
        lesson_buff_multiple_permille=(
            PLAN1_SYNTHETIC_TERMINAL_LESSON_MULTIPLE_PERMILLE
        ),
        rng_before=0,
    )


__all__ = [
    "PLAN1_SYNTHETIC_TERMINAL_BATTLE_BONUS_PERMILLE",
    "PLAN1_SYNTHETIC_TERMINAL_INITIAL_SCORE",
    "PLAN1_SYNTHETIC_TERMINAL_INITIAL_STAMINA",
    "PLAN1_SYNTHETIC_TERMINAL_LESSON_MULTIPLE_PERMILLE",
    "PLAN1_SYNTHETIC_TERMINAL_MAX_STAMINA",
    "Plan1StageBootstrap",
    "Plan1StageBootstrapAuthority",
    "Plan1StageBootstrapIssue",
    "Plan1StageBootstrapProvision",
    "Plan1StageOuterIdentityScope",
    "Plan1StageOuterStateIdentity",
    "build_synthetic_plan1_terminal_stage_bootstrap",
    "provision_plan1_stage",
    "validate_plan1_stage_bootstrap_fixture",
    "validate_plan1_stage_bootstrap_request",
]
