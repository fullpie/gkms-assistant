"""Plan-neutral inner-stage contract for the Initial Regular outer shell.

The outer rollout owns request identity, the persistent Produce state, and
the probability attached to an external branch.  A plan adapter owns the
inner simulator and may capture its compiled catalog/loadout/runtime objects
in a closure.  Those plan-specific objects intentionally never enter this
module.

Missing runtime facts are representable as ``None`` on the request so a
catalog can report an incomplete frontier.  Dispatch remains fail closed:
the registry turns every unresolved or inconsistent fact into typed issues
before a plan adapter is called, and validates the adapter outcome before it
is projected back to the generic outer kernel.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isclose
from typing import Iterable, Mapping, Protocol, TypeAlias, runtime_checkable

from .audition_rules import FINAL
from .produce_rollout import (
    AttributeValues,
    ChanceBranch,
    DeckEntry,
    ExternalKind,
    ExternalRequest,
    InnerExamOutcome,
    ProduceRolloutState,
    ProduceStatePatch,
    RolloutPhase,
    WeeklyActionOutcome,
)


_UINT32_MAX = 0xFFFFFFFF


class InitialRegularInnerStageKind(StrEnum):
    """Outer meaning of an inner exam-shaped stage."""

    LESSON = "lesson"
    AUDITION = "audition"


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularInnerStageIssue:
    """Stable fail-closed diagnostic shared by every plan adapter."""

    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not _nonempty_text(self.code) or not _nonempty_text(self.field):
            raise ValueError("inner-stage issue code and field must be non-empty")
        if not isinstance(self.detail, str):
            raise TypeError("inner-stage issue detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "field": self.field, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class InitialRegularInnerRuntimeRefs:
    """Outer-owned session identities visible to every plan adapter."""

    item_session_refs: tuple[str, ...]
    drink_session_refs: tuple[str, ...]
    passive_session_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InitialRegularInnerStageRequest:
    """Plan-neutral, exact binding for one lesson or audition simulation.

    ``stage_type`` is the route milestone token for auditions.  Ordinary
    weekly lessons leave it ``None``; a same-week event continuation retains
    the exact ``ProduceStepType_Lesson*`` token from its pending request.
    ``exam_type`` is the native mode discriminator (lesson ``0``, audition
    ``1``); ``lesson_type`` retains the separate Master lesson enum, and
    ``step_type_value`` is the numeric lesson-step variant.
    """

    external_request_id: str
    week: int
    action_id: str
    stage_kind: InitialRegularInnerStageKind
    plan_type: str
    character_id: str
    outer_state: ProduceRolloutState
    branch: ChanceBranch
    idol_card_id: str | None = None
    stage_type: str | None = None
    stage_id: str | None = None
    setting_id: str | None = None
    exam_type: int | None = None
    lesson_type: str | None = None
    step_type_value: int | None = None
    limit_turn: int | None = None
    extra_turn: int | None = None
    clear_border: int | None = None
    limit_border: int | None = None
    rng_before: int | None = None
    attribute: str | None = None
    is_sp: bool | None = None
    battle_bonus_permille: tuple[int, int, int] | None = None
    gimmick_group_id: str | None = None
    turn_parameter_types: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        stage_kind = self.stage_kind
        if isinstance(stage_kind, str) and not isinstance(
            stage_kind, InitialRegularInnerStageKind
        ):
            try:
                stage_kind = InitialRegularInnerStageKind(stage_kind)
            except ValueError as error:
                raise ValueError("stage_kind must be lesson or audition") from error
            object.__setattr__(self, "stage_kind", stage_kind)
        if not isinstance(stage_kind, InitialRegularInnerStageKind):
            raise TypeError("stage_kind must be InitialRegularInnerStageKind")
        for name in (
            "external_request_id",
            "action_id",
            "plan_type",
            "character_id",
        ):
            if not _nonempty_text(getattr(self, name)):
                raise ValueError(f"{name} must be non-empty text")
        _plain_integer(self.week, "week", minimum=1)
        if not isinstance(self.outer_state, ProduceRolloutState):
            raise TypeError("outer_state must be ProduceRolloutState")
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("branch must be ChanceBranch")
        for name in (
            "idol_card_id",
            "stage_type",
            "stage_id",
            "setting_id",
            "lesson_type",
            "attribute",
        ):
            value = getattr(self, name)
            if value is not None and not _nonempty_text(value):
                raise ValueError(f"{name} must be non-empty text or None")
        for name, minimum in (
            ("exam_type", 0),
            ("step_type_value", 0),
            ("limit_turn", 1),
            ("extra_turn", 0),
            ("clear_border", -1),
            # Android's audition factory maps a disabled ForceEndScore to -1.
            ("limit_border", -1),
            ("rng_before", 0),
        ):
            value = getattr(self, name)
            if value is not None:
                _plain_integer(value, name, minimum=minimum)
        if self.rng_before is not None and self.rng_before > _UINT32_MAX:
            raise ValueError("rng_before must fit UInt32")
        if self.is_sp is not None and not isinstance(self.is_sp, bool):
            raise TypeError("is_sp must be bool or None")
        if self.battle_bonus_permille is not None:
            bonuses = tuple(self.battle_bonus_permille)
            if len(bonuses) != 3 or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in bonuses
            ):
                raise TypeError(
                    "battle_bonus_permille must contain exactly three integers"
                )
            object.__setattr__(self, "battle_bonus_permille", bonuses)
        if self.gimmick_group_id is not None and not isinstance(
            self.gimmick_group_id, str
        ):
            raise TypeError("gimmick_group_id must be text or None")
        if self.turn_parameter_types is not None:
            parameter_types = tuple(self.turn_parameter_types)
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in parameter_types
            ):
                raise TypeError(
                    "turn_parameter_types must contain non-negative integers"
                )
            object.__setattr__(self, "turn_parameter_types", parameter_types)

    @property
    def request_id(self) -> str:
        """Compatibility-friendly alias for the external request identity."""

        return self.external_request_id

    @property
    def outer_deck(self) -> tuple[DeckEntry, ...]:
        return self.outer_state.deck

    @property
    def deck(self) -> tuple[DeckEntry, ...]:
        return self.outer_deck

    @property
    def runtime_refs(self) -> InitialRegularInnerRuntimeRefs:
        state = self.outer_state
        return InitialRegularInnerRuntimeRefs(
            item_session_refs=state.item_session_refs,
            drink_session_refs=state.drink_session_refs,
            passive_session_refs=state.passive_session_refs,
        )

    @property
    def turns(self) -> int | None:
        if self.limit_turn is None or self.extra_turn is None:
            return None
        return self.limit_turn + self.extra_turn


@dataclass(frozen=True, slots=True)
class InitialRegularInnerStageOutcome:
    """Plan adapter result before projection to an outer outcome type."""

    request_id: str
    branch: ChanceBranch
    patch: ProduceStatePatch
    cleared: bool
    terminal: bool
    reward_requested: bool
    rng_after: int | None
    score: int | None = None
    rank: int | None = None
    trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _nonempty_text(self.request_id):
            raise ValueError("outcome request_id must be non-empty text")
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("outcome branch must be ChanceBranch")
        if not isinstance(self.patch, ProduceStatePatch):
            raise TypeError("outcome patch must be ProduceStatePatch")
        for name in ("cleared", "terminal", "reward_requested"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"outcome {name} must be bool")
        for name, minimum in (("rng_after", 0), ("score", 0), ("rank", 0)):
            value = getattr(self, name)
            if value is not None:
                _plain_integer(value, f"outcome {name}", minimum=minimum)
        if self.rng_after is not None and self.rng_after > _UINT32_MAX:
            raise ValueError("outcome rng_after must fit UInt32")
        if isinstance(self.trace, str):
            raise TypeError("outcome trace must be a tuple of text entries")
        trace = tuple(self.trace)
        if any(not _nonempty_text(value) for value in trace):
            raise ValueError("outcome trace must contain non-empty text")
        object.__setattr__(self, "trace", trace)


InitialRegularInnerStageAdapterResult: TypeAlias = (
    InitialRegularInnerStageOutcome
    | InitialRegularInnerStageIssue
    | tuple[InitialRegularInnerStageIssue, ...]
)
InitialRegularInnerExternalOutcome: TypeAlias = (
    WeeklyActionOutcome | InnerExamOutcome
)
InitialRegularInnerDispatchResult: TypeAlias = (
    InitialRegularInnerStageOutcome | tuple[InitialRegularInnerStageIssue, ...]
)
InitialRegularInnerExternalResult: TypeAlias = (
    InitialRegularInnerExternalOutcome | tuple[InitialRegularInnerStageIssue, ...]
)


@runtime_checkable
class InitialRegularInnerStageAdapter(Protocol):
    """Callable plan adapter; compiled plan objects belong in its closure."""

    def __call__(
        self, request: InitialRegularInnerStageRequest
    ) -> InitialRegularInnerStageAdapterResult: ...


class InitialRegularLessonRuntimeContextContract(Protocol):
    """Structural subset used by the lesson-catalog construction helper."""

    exam_extra_turn: int
    battle_bonus_permille: tuple[int, int, int]
    gimmick_group_id: str


class InitialRegularLessonStageFactContract(Protocol):
    """Plan-neutral view of ``InitialRegularLessonStageFact``.

    The concrete catalog stays outside this module, avoiding a circular
    dependency while retaining a typed, direct construction path.
    """

    stage_id: str
    plan_type: str
    setting_id: str
    lesson_type: str
    step_type_value: int
    base_turns: int
    runtime_turns: int | None
    clear_border: int
    limit_border: int | None
    attribute: object
    is_sp: bool
    runtime_context: InitialRegularLessonRuntimeContextContract | None


class InitialRegularInnerStageAdapterRegistry:
    """Exact plan-type dispatcher with a fail-closed result boundary."""

    def __init__(
        self,
        adapters: Mapping[str, InitialRegularInnerStageAdapter]
        | Iterable[tuple[str, InitialRegularInnerStageAdapter]]
        | None = None,
    ) -> None:
        self._adapters: dict[str, InitialRegularInnerStageAdapter] = {}
        if adapters is None:
            return
        entries = adapters.items() if isinstance(adapters, Mapping) else adapters
        for plan_type, adapter in entries:
            self.register(plan_type, adapter)

    @property
    def plan_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def register(
        self, plan_type: str, adapter: InitialRegularInnerStageAdapter
    ) -> None:
        if not _nonempty_text(plan_type):
            raise ValueError("registered plan_type must be non-empty text")
        if not callable(adapter):
            raise TypeError("inner-stage adapter must be callable")
        if plan_type in self._adapters:
            raise ValueError(f"inner-stage adapter is already registered: {plan_type}")
        self._adapters[plan_type] = adapter

    def adapter_for(
        self, plan_type: str
    ) -> InitialRegularInnerStageAdapter | None:
        if not isinstance(plan_type, str):
            return None
        return self._adapters.get(plan_type)

    def resolve(
        self, request: InitialRegularInnerStageRequest
    ) -> InitialRegularInnerDispatchResult:
        if not isinstance(request, InitialRegularInnerStageRequest):
            raise TypeError("request must be InitialRegularInnerStageRequest")
        request_issues = validate_initial_regular_inner_stage_request(request)
        if request_issues:
            return request_issues
        adapter = self._adapters.get(request.plan_type)
        if adapter is None:
            return (
                _issue(
                    "inner-adapter-unregistered",
                    "request.plan_type",
                    request.plan_type,
                ),
            )
        try:
            result = adapter(request)
        except Exception as error:
            return (
                _issue(
                    "inner-adapter-failed",
                    "adapter",
                    f"{type(error).__name__}:{error}",
                ),
            )
        if isinstance(result, InitialRegularInnerStageIssue):
            return (result,)
        if isinstance(result, tuple):
            if result and all(
                isinstance(value, InitialRegularInnerStageIssue) for value in result
            ):
                return result
            return (_issue("inner-adapter-result-invalid", "adapter"),)
        if not isinstance(result, InitialRegularInnerStageOutcome):
            return (_issue("inner-adapter-result-invalid", "adapter"),)
        outcome_issues = validate_initial_regular_inner_stage_outcome(
            request, result
        )
        return outcome_issues or result

    def resolve_external(
        self, request: InitialRegularInnerStageRequest
    ) -> InitialRegularInnerExternalResult:
        """Dispatch and project to the exact generic outer outcome class."""

        result = self.resolve(request)
        if isinstance(result, tuple):
            return result
        return project_initial_regular_inner_stage_outcome(request, result)

    dispatch = resolve

    def __call__(
        self, request: InitialRegularInnerStageRequest
    ) -> InitialRegularInnerDispatchResult:
        return self.resolve(request)


# Short aliases are useful for plan adapter modules while the Initial Regular
# prefix remains the stable public shell API.
InnerStageKind = InitialRegularInnerStageKind
InnerStageRequest = InitialRegularInnerStageRequest
InnerStageOutcome = InitialRegularInnerStageOutcome
InnerStageIssue = InitialRegularInnerStageIssue
InnerStageAdapter = InitialRegularInnerStageAdapter
InnerStageAdapterRegistry = InitialRegularInnerStageAdapterRegistry
InitialRegularInnerAdapter = InitialRegularInnerStageAdapter
InitialRegularInnerAdapterRegistry = InitialRegularInnerStageAdapterRegistry


def build_initial_regular_lesson_stage_request(
    state: ProduceRolloutState,
    external_request: ExternalRequest,
    stage_fact: InitialRegularLessonStageFactContract,
    *,
    branch: ChanceBranch,
    idol_card_id: str | None,
    rng_before: int | None,
) -> InitialRegularInnerStageRequest:
    """Build the common request directly from one lesson catalog stage fact.

    Runtime-owned values remain explicitly unresolved when the catalog has no
    runtime context yet.  Contradictory resolved turn facts are rejected at
    construction instead of being silently normalized.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(external_request, ExternalRequest):
        raise TypeError("external_request must be ExternalRequest")
    required_attributes = (
        "stage_id",
        "plan_type",
        "setting_id",
        "lesson_type",
        "step_type_value",
        "base_turns",
        "runtime_turns",
        "clear_border",
        "limit_border",
        "attribute",
        "is_sp",
        "runtime_context",
    )
    if any(not hasattr(stage_fact, name) for name in required_attributes):
        raise TypeError("stage_fact does not satisfy the lesson-stage contract")
    runtime_context = stage_fact.runtime_context
    extra_turn = (
        None if runtime_context is None else runtime_context.exam_extra_turn
    )
    if (
        stage_fact.runtime_turns is not None
        and extra_turn is not None
        and stage_fact.runtime_turns != stage_fact.base_turns + extra_turn
    ):
        raise ValueError("lesson stage runtime_turns disagrees with base + extra")
    raw_attribute = stage_fact.attribute
    attribute = getattr(raw_attribute, "value", raw_attribute)
    if not isinstance(attribute, str):
        raise TypeError("lesson stage attribute must expose a text value")
    return InitialRegularInnerStageRequest(
        external_request_id=external_request.request_id,
        week=external_request.week,
        action_id=external_request.action_id,
        stage_kind=InitialRegularInnerStageKind.LESSON,
        plan_type=stage_fact.plan_type,
        character_id=state.character_id,
        outer_state=state,
        branch=branch,
        idol_card_id=idol_card_id,
        stage_type=external_request.stage_type,
        stage_id=stage_fact.stage_id,
        setting_id=stage_fact.setting_id,
        exam_type=0,
        lesson_type=stage_fact.lesson_type,
        step_type_value=stage_fact.step_type_value,
        limit_turn=stage_fact.base_turns,
        extra_turn=extra_turn,
        clear_border=stage_fact.clear_border,
        limit_border=stage_fact.limit_border,
        rng_before=rng_before,
        attribute=attribute,
        is_sp=stage_fact.is_sp,
        battle_bonus_permille=(
            None
            if runtime_context is None
            else runtime_context.battle_bonus_permille
        ),
        gimmick_group_id=(
            None if runtime_context is None else runtime_context.gimmick_group_id
        ),
        turn_parameter_types=(),
    )


def validate_initial_regular_inner_stage_request(
    request: InitialRegularInnerStageRequest,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    """Validate request/external identity and all simulator-owned scalars."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    state = request.outer_state
    pending = state.pending
    issues: list[InitialRegularInnerStageIssue] = []
    if state.phase != RolloutPhase.WAITING_EXTERNAL or pending is None:
        issues.append(_issue("outer-request-not-pending", "request.outer_state.pending"))
    else:
        expected_kind = (
            ExternalKind.WEEKLY_ACTION_OUTCOME
            if request.stage_kind is InitialRegularInnerStageKind.LESSON
            else ExternalKind.INNER_EXAM_OUTCOME
        )
        if pending.kind is not expected_kind:
            issues.append(
                _issue(
                    "external-request-kind-mismatch",
                    "request.outer_state.pending.kind",
                    f"expected={expected_kind.value}:actual={pending.kind.value}",
                )
            )
        for field_name, expected, actual in (
            ("request_id", request.external_request_id, pending.request_id),
            ("week", request.week, pending.week),
            ("action_id", request.action_id, pending.action_id),
        ):
            if expected != actual:
                issues.append(
                    _issue(
                        f"external-{field_name.replace('_', '-')}-mismatch",
                        f"request.outer_state.pending.{field_name}",
                        f"request={expected}:external={actual}",
                    )
                )
        if request.stage_kind is InitialRegularInnerStageKind.AUDITION:
            if pending.stage_type != request.stage_type:
                issues.append(
                    _issue(
                        "external-stage-type-mismatch",
                        "request.outer_state.pending.stage_type",
                    )
                )
        elif pending.stage_type != request.stage_type:
            issues.append(
                _issue(
                    "external-stage-type-mismatch",
                    "request.outer_state.pending.stage_type",
                )
            )
    if state.week != request.week:
        issues.append(
            _issue(
                "outer-week-mismatch",
                "request.week",
                f"outer={state.week}:request={request.week}",
            )
        )
    if state.character_id != request.character_id:
        issues.append(
            _issue(
                "outer-character-mismatch",
                "request.character_id",
                f"outer={state.character_id}:request={request.character_id}",
            )
        )
    if request.branch.probability is None:
        issues.append(
            _issue(
                "inner-branch-probability-unresolved",
                "request.branch.probability",
            )
        )
    elif request.branch.probability <= 0.0:
        issues.append(
            _issue(
                "inner-branch-probability-invalid",
                "request.branch.probability",
            )
        )

    for field_name in (
        "idol_card_id",
        "stage_id",
        "setting_id",
        "exam_type",
        "step_type_value",
        "limit_turn",
        "extra_turn",
        "clear_border",
        "limit_border",
        "rng_before",
        "battle_bonus_permille",
        "gimmick_group_id",
        "turn_parameter_types",
    ):
        if getattr(request, field_name) is None:
            issues.append(
                _issue(
                    "inner-request-field-unresolved",
                    f"request.{field_name}",
                )
            )
    expected_exam_type = (
        0
        if request.stage_kind is InitialRegularInnerStageKind.LESSON
        else 1
    )
    if (
        request.exam_type is not None
        and request.exam_type != expected_exam_type
    ):
        issues.append(
            _issue(
                "inner-exam-type-mismatch",
                "request.exam_type",
                f"expected={expected_exam_type}:actual={request.exam_type}",
            )
        )
    if request.stage_kind is InitialRegularInnerStageKind.LESSON:
        for field_name in (
            "lesson_type",
            "attribute",
            "is_sp",
        ):
            if getattr(request, field_name) is None:
                issues.append(
                    _issue(
                        "inner-request-field-unresolved",
                        f"request.{field_name}",
                    )
                )
        if request.turn_parameter_types not in (None, ()):
            issues.append(
                _issue(
                    "lesson-turn-parameter-types-invalid",
                    "request.turn_parameter_types",
                )
            )
    else:
        if request.stage_type is None:
            issues.append(
                _issue("audition-stage-type-unresolved", "request.stage_type")
            )
        if request.lesson_type is not None:
            issues.append(
                _issue("audition-lesson-type-invalid", "request.lesson_type")
            )
        if request.turn_parameter_types == ():
            issues.append(
                _issue(
                    "audition-turn-parameter-types-unresolved",
                    "request.turn_parameter_types",
                )
            )
        for field_name in ("attribute", "is_sp"):
            if getattr(request, field_name) is not None:
                issues.append(
                    _issue(
                        "audition-lesson-field-invalid",
                        f"request.{field_name}",
                    )
                )

    if (
        request.clear_border is not None
        and request.limit_border is not None
        and request.limit_border >= 0
        and request.clear_border > request.limit_border
    ):
        issues.append(_issue("inner-border-order-invalid", "request.clear_border"))
    return tuple(issues)


def validate_initial_regular_inner_stage_outcome(
    request: InitialRegularInnerStageRequest,
    outcome: InitialRegularInnerStageOutcome,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    """Validate the plan adapter boundary before outer-state projection."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    if not isinstance(outcome, InitialRegularInnerStageOutcome):
        raise TypeError("outcome must be InitialRegularInnerStageOutcome")
    issues: list[InitialRegularInnerStageIssue] = []
    if outcome.request_id != request.external_request_id:
        issues.append(
            _issue("outcome-request-id-mismatch", "outcome.request_id")
        )
    if outcome.branch != request.branch:
        issues.append(_issue("outcome-branch-mismatch", "outcome.branch"))
    probability = outcome.branch.probability
    if probability is None:
        issues.append(
            _issue("inner-branch-probability-unresolved", "outcome.branch.probability")
        )
    elif probability <= 0.0:
        issues.append(
            _issue("inner-branch-probability-invalid", "outcome.branch.probability")
        )
    if request.rng_before is not None and outcome.rng_after is None:
        issues.append(_issue("outcome-rng-after-unresolved", "outcome.rng_after"))
    if (
        outcome.reward_requested
        and not outcome.cleared
        and request.stage_kind is InitialRegularInnerStageKind.AUDITION
    ):
        issues.append(
            _issue("outcome-reward-on-uncleared-stage", "outcome.reward_requested")
        )
    expected_terminal = (
        request.stage_kind is InitialRegularInnerStageKind.AUDITION
        and request.stage_type == FINAL
    )
    if outcome.terminal is not expected_terminal:
        issues.append(
            _issue(
                "outcome-terminal-mismatch",
                "outcome.terminal",
                f"expected={expected_terminal}:actual={outcome.terminal}",
            )
        )
    if request.stage_kind is InitialRegularInnerStageKind.LESSON:
        if (
            outcome.score is not None
            and request.clear_border is not None
            and request.clear_border >= 0
            and outcome.cleared != (outcome.score >= request.clear_border)
        ):
            issues.append(
                _issue(
                    "outcome-clear-border-mismatch",
                    "outcome.score",
                    f"score={outcome.score}:clear_border={request.clear_border}",
                )
            )
    else:
        # Audition clearBorder is a rank threshold, not a player-score target.
        if outcome.rank is None:
            issues.append(_issue("outcome-rank-unresolved", "outcome.rank"))
        elif (
            request.clear_border is not None
            and request.clear_border >= 1
            and outcome.cleared != (outcome.rank <= request.clear_border)
        ):
            issues.append(
                _issue(
                    "outcome-rank-threshold-mismatch",
                    "outcome.rank",
                    f"rank={outcome.rank}:threshold={request.clear_border}",
                )
            )
    issues.extend(_patch_issues(request.outer_state, outcome.patch))
    return tuple(issues)


def validate_initial_regular_inner_branch_probability(
    probability: object,
    outcome: InitialRegularInnerStageOutcome,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    """Cross-check exact outer weight against the adapter branch annotation."""

    from fractions import Fraction

    if isinstance(probability, bool) or not isinstance(probability, Fraction):
        raise TypeError("probability must be fractions.Fraction")
    if probability <= 0 or probability > 1:
        raise ValueError("probability must be in (0, 1]")
    if not isinstance(outcome, InitialRegularInnerStageOutcome):
        raise TypeError("outcome must be InitialRegularInnerStageOutcome")
    annotated = outcome.branch.probability
    if annotated is None:
        return (
            _issue(
                "inner-branch-probability-unresolved",
                "outcome.branch.probability",
            ),
        )
    if not isclose(annotated, float(probability), rel_tol=0.0, abs_tol=1e-12):
        return (
            _issue(
                "inner-branch-probability-mismatch",
                "outcome.branch.probability",
                f"outer={probability}:inner={annotated}",
            ),
        )
    return ()


def project_initial_regular_inner_stage_outcome(
    request: InitialRegularInnerStageRequest,
    outcome: InitialRegularInnerStageOutcome,
) -> InitialRegularInnerExternalOutcome:
    """Project a validated common result to the generic outer outcome."""

    issues = validate_initial_regular_inner_stage_outcome(request, outcome)
    if issues:
        codes = ",".join(issue.code for issue in issues)
        raise ValueError(f"inner-stage outcome is not projectable: {codes}")
    common = {
        "request_id": request.external_request_id,
        "branch": outcome.branch,
        "patch": outcome.patch,
        "reward_requested": outcome.reward_requested,
    }
    if request.stage_kind is InitialRegularInnerStageKind.LESSON:
        return WeeklyActionOutcome(**common)
    return InnerExamOutcome(
        **common,
        cleared=outcome.cleared,
        terminal=outcome.terminal,
    )


def _patch_issues(
    state: ProduceRolloutState, patch: ProduceStatePatch
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    for field_name in ("stamina", "max_stamina", "produce_points"):
        value = getattr(patch, field_name)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            issues.append(
                _issue(
                    "outcome-patch-scalar-invalid",
                    f"outcome.patch.{field_name}",
                )
            )
    max_stamina = (
        state.max_stamina if patch.max_stamina is None else patch.max_stamina
    )
    stamina = state.stamina if patch.stamina is None else patch.stamina
    if (
        isinstance(stamina, int)
        and not isinstance(stamina, bool)
        and isinstance(max_stamina, int)
        and not isinstance(max_stamina, bool)
        and stamina > max_stamina
    ):
        issues.append(
            _issue(
                "outcome-patch-stamina-invalid",
                "outcome.patch.stamina",
                f"stamina={stamina}:max={max_stamina}",
            )
        )
    if patch.attributes is not None and not isinstance(
        patch.attributes, AttributeValues
    ):
        issues.append(
            _issue("outcome-patch-attributes-invalid", "outcome.patch.attributes")
        )
    if patch.deck is not None and (
        not isinstance(patch.deck, tuple)
        or any(not isinstance(value, DeckEntry) for value in patch.deck)
    ):
        issues.append(_issue("outcome-patch-deck-invalid", "outcome.patch.deck"))
    for field_name in (
        "item_session_refs",
        "drink_session_refs",
        "passive_session_refs",
        "excluded_reward_card_ids",
    ):
        values = getattr(patch, field_name)
        if values is not None and (
            not isinstance(values, tuple)
            or any(not _nonempty_text(value) for value in values)
        ):
            issues.append(
                _issue(
                    "outcome-patch-refs-invalid",
                    f"outcome.patch.{field_name}",
                )
            )
    return tuple(issues)


def _issue(
    code: str, field: str, detail: str = ""
) -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _nonempty_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _plain_integer(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


__all__ = [
    "InitialRegularInnerDispatchResult",
    "InitialRegularInnerAdapter",
    "InitialRegularInnerAdapterRegistry",
    "InitialRegularInnerExternalOutcome",
    "InitialRegularInnerExternalResult",
    "InitialRegularInnerRuntimeRefs",
    "InitialRegularLessonRuntimeContextContract",
    "InitialRegularLessonStageFactContract",
    "InitialRegularInnerStageAdapter",
    "InitialRegularInnerStageAdapterRegistry",
    "InitialRegularInnerStageAdapterResult",
    "InitialRegularInnerStageIssue",
    "InitialRegularInnerStageKind",
    "InitialRegularInnerStageOutcome",
    "InitialRegularInnerStageRequest",
    "InnerStageAdapter",
    "InnerStageAdapterRegistry",
    "InnerStageIssue",
    "InnerStageKind",
    "InnerStageOutcome",
    "InnerStageRequest",
    "build_initial_regular_lesson_stage_request",
    "project_initial_regular_inner_stage_outcome",
    "validate_initial_regular_inner_branch_probability",
    "validate_initial_regular_inner_stage_outcome",
    "validate_initial_regular_inner_stage_request",
]
