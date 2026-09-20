"""Typed N.I.A. ``Refresh`` (step type 14) response boundary.

The Android Refresh RPC is deliberately kept separate from the outer
``outing`` helper.  The request carries only ``DeviceProduceUuid`` and the
response carries authoritative before/after stamina plus ordered
``EffectResults`` and a ``CommonResponse`` user-data envelope.  This module
does not replay those effects or infer an order between the response buckets;
the caller supplies the complete CommonResponse post-state instead.

``ProduceSchedule.RefreshStamina`` is retained only as an observed recovery
delta.  It is never interpreted as an absolute stamina value.  The exact
recovery formula is the native ratio helper with its float32 epsilon:

``min(max_stamina - current_stamina,
      floor(max_stamina * permille / 1000 + epsilon))``.

No save/device/process I/O occurs here and a caller scenario is never claimed
to be an authentic LocalSave.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping, TypeAlias

from .initial_regular_event_scenario import (
    ProduceEffectResultTrace,
    validate_effect_result_chain,
)
from .initial_regular_inner_protocol import InitialRegularInnerStageIssue
from .native_exam_formula import get_ratio_effect_int_value
from .produce_rollout import (
    AttributeValues,
    ChanceBranch,
    DeckEntry,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    ProduceStatePatch,
    RolloutPhase,
    WeeklyActionOutcome,
)


NIA_REFRESH_ACTION_ID: Final = "refresh"
ANDROID_REFRESH_STEP_TYPE: Final = "ProduceStepType_Refresh"
ANDROID_REFRESH_STEP_TYPE_VALUE: Final = 14
ANDROID_REFRESH_REQUEST_FIELDS: Final = ("device_produce_uuid",)
# CommonResponse is an RPC envelope, so it is listed separately just as in
# the Interval/SelfLesson adapters.  The scalar response payload is ordered.
ANDROID_REFRESH_RESPONSE_FIELDS: Final = (
    "before_stamina",
    "after_stamina",
    "effect_results",
)
ANDROID_REFRESH_COMMON_RESPONSE_FIELDS: Final = ("common_response",)
ANDROID_REFRESH_SCHEDULE_FIELD: Final = "ProduceSchedule.RefreshStamina"
ANDROID_REFRESH_RECOVERY_AUTHORITY: Final = (
    "ProduceStepRefreshResponse.BeforeStamina/AfterStamina; "
    "ProduceSchedule.RefreshStamina is a cross-checkable recovery delta, "
    "not an absolute stamina value"
)


def _plain_int(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return value


def _exact(value: Mapping[str, object], fields: set[str], field: str) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        detail: list[str] = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if extra:
            detail.append(f"extra={','.join(extra)}")
        raise ValueError(f"{field} keys mismatch ({';'.join(detail)})")


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return value


def _text(value: object, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    if not empty and not value:
        raise ValueError(f"{field} must be non-empty")
    return value


def _strict_branch(value: object, field: str) -> ChanceBranch:
    payload = _mapping(value, field)
    _exact(
        payload,
        {"branch_id", "probability", "rng_token", "event_id", "was_sp"},
        field,
    )
    return ChanceBranch.from_dict(payload)


def _strict_attributes(value: object, field: str) -> AttributeValues:
    payload = _mapping(value, field)
    _exact(payload, {"vocal", "dance", "visual"}, field)
    return AttributeValues.from_dict(payload)


def _strict_deck_entry(value: object, field: str) -> DeckEntry:
    payload = _mapping(value, field)
    _exact(payload, {"card_id", "upgrade", "count", "instance_ids"}, field)
    return DeckEntry.from_dict(payload)


def calculate_nia_refresh_recovery(
    current_stamina: int,
    max_stamina: int,
    refresh_stamina_recovery_permille: int,
) -> int:
    """Return the exact native Refresh stamina increment.

    ``get_ratio_effect_int_value`` performs the Android float32 conversion
    and positive epsilon before flooring.  The cap is applied after the ratio
    result, matching the native recovery ordering.
    """

    _plain_int(current_stamina, "current_stamina")
    _plain_int(max_stamina, "max_stamina", minimum=1)
    _plain_int(
        refresh_stamina_recovery_permille,
        "refresh_stamina_recovery_permille",
    )
    if current_stamina > max_stamina:
        raise ValueError("current_stamina must not exceed max_stamina")
    if refresh_stamina_recovery_permille > 1000:
        raise ValueError("refresh_stamina_recovery_permille must be <= 1000")
    ratio_recovery = get_ratio_effect_int_value(
        max_stamina,
        refresh_stamina_recovery_permille,
        is_ceil=False,
    )
    return min(max_stamina - current_stamina, ratio_recovery)


def validate_nia_refresh_formula(
    *,
    before_stamina: int,
    after_stamina: int,
    max_stamina: int,
    refresh_stamina_recovery_permille: int,
) -> int:
    """Validate response stamina and return the expected recovery delta."""

    _plain_int(before_stamina, "before_stamina")
    _plain_int(after_stamina, "after_stamina")
    recovery = calculate_nia_refresh_recovery(
        before_stamina,
        max_stamina,
        refresh_stamina_recovery_permille,
    )
    expected_after = before_stamina + recovery
    if after_stamina != expected_after:
        raise ValueError(
            "Refresh after_stamina disagrees with native recovery formula: "
            f"expected={expected_after}:actual={after_stamina}"
        )
    return recovery


# Concise aliases for callers that use the route terminology rather than the
# N.I.A. prefix.  They intentionally refer to the same exact implementation.
calculate_refresh_recovery = calculate_nia_refresh_recovery
validate_refresh_formula = validate_nia_refresh_formula


def _typed_effects(
    values: tuple[ProduceEffectResultTrace, ...],
) -> tuple[ProduceEffectResultTrace, ...]:
    result = tuple(values)
    if not all(isinstance(value, ProduceEffectResultTrace) for value in result):
        raise TypeError("Refresh effect_results must contain typed traces")
    validate_effect_result_chain(result)
    return result


@dataclass(frozen=True, slots=True)
class NiaRefreshPostState:
    """Complete caller-authoritative persistent state from CommonResponse."""

    stamina: int
    max_stamina: int
    produce_points: int
    attributes: AttributeValues
    deck: tuple[DeckEntry, ...]
    item_session_refs: tuple[str, ...] = ()
    drink_session_refs: tuple[str, ...] = ()
    passive_session_refs: tuple[str, ...] = ()
    excluded_reward_card_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _plain_int(self.stamina, "Refresh post stamina")
        _plain_int(self.max_stamina, "Refresh post max_stamina", minimum=1)
        _plain_int(self.produce_points, "Refresh post produce_points")
        if self.stamina > self.max_stamina:
            raise ValueError("Refresh post stamina exceeds max_stamina")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("Refresh post attributes must be AttributeValues")
        cards = tuple(self.deck)
        if not all(isinstance(value, DeckEntry) for value in cards):
            raise TypeError("Refresh post deck must contain DeckEntry values")
        object.__setattr__(self, "deck", cards)
        for field in (
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        ):
            values = tuple(getattr(self, field))
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"Refresh post {field} must contain text values")
            if len(set(values)) != len(values):
                raise ValueError(f"Refresh post {field} must be unique")
            object.__setattr__(self, field, values)
        self.to_patch()

    @classmethod
    def from_outer_state(
        cls,
        state: ProduceRolloutState,
        *,
        stamina: int | None = None,
        max_stamina: int | None = None,
        produce_points: int | None = None,
        attributes: AttributeValues | None = None,
        deck: tuple[DeckEntry, ...] | None = None,
        item_session_refs: tuple[str, ...] | None = None,
        drink_session_refs: tuple[str, ...] | None = None,
        passive_session_refs: tuple[str, ...] | None = None,
        excluded_reward_card_ids: tuple[str, ...] | None = None,
    ) -> "NiaRefreshPostState":
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        return cls(
            stamina=state.stamina if stamina is None else stamina,
            max_stamina=state.max_stamina if max_stamina is None else max_stamina,
            produce_points=(
                state.produce_points if produce_points is None else produce_points
            ),
            attributes=state.attributes if attributes is None else attributes,
            deck=state.deck if deck is None else deck,
            item_session_refs=(
                state.item_session_refs
                if item_session_refs is None
                else item_session_refs
            ),
            drink_session_refs=(
                state.drink_session_refs
                if drink_session_refs is None
                else drink_session_refs
            ),
            passive_session_refs=(
                state.passive_session_refs
                if passive_session_refs is None
                else passive_session_refs
            ),
            excluded_reward_card_ids=(
                state.excluded_reward_card_ids
                if excluded_reward_card_ids is None
                else excluded_reward_card_ids
            ),
        )

    def to_patch(self) -> ProduceStatePatch:
        return ProduceStatePatch(
            stamina=self.stamina,
            max_stamina=self.max_stamina,
            produce_points=self.produce_points,
            attributes=self.attributes,
            deck=self.deck,
            item_session_refs=self.item_session_refs,
            drink_session_refs=self.drink_session_refs,
            passive_session_refs=self.passive_session_refs,
            excluded_reward_card_ids=self.excluded_reward_card_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "stamina": self.stamina,
            "max_stamina": self.max_stamina,
            "produce_points": self.produce_points,
            "attributes": self.attributes.to_dict(),
            "deck": [value.to_dict() for value in self.deck],
            "item_session_refs": list(self.item_session_refs),
            "drink_session_refs": list(self.drink_session_refs),
            "passive_session_refs": list(self.passive_session_refs),
            "excluded_reward_card_ids": list(self.excluded_reward_card_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaRefreshPostState":
        value = _mapping(payload, "Refresh post state")
        fields = {
            "stamina",
            "max_stamina",
            "produce_points",
            "attributes",
            "deck",
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        }
        _exact(value, fields, "Refresh post state")

        def _refs(name: str) -> tuple[str, ...]:
            return tuple(
                _text(item, f"Refresh post {name} item")
                for item in _list(value[name], f"Refresh post {name}")
            )

        return cls(
            stamina=_plain_int(value["stamina"], "Refresh post stamina"),
            max_stamina=_plain_int(value["max_stamina"], "Refresh post max_stamina", minimum=1),
            produce_points=_plain_int(value["produce_points"], "Refresh post produce_points"),
            attributes=_strict_attributes(value["attributes"], "Refresh post attributes"),
            deck=tuple(
                _strict_deck_entry(item, "Refresh post deck entry")
                for item in _list(value["deck"], "Refresh post deck")
            ),
            item_session_refs=_refs("item_session_refs"),
            drink_session_refs=_refs("drink_session_refs"),
            passive_session_refs=_refs("passive_session_refs"),
            excluded_reward_card_ids=_refs("excluded_reward_card_ids"),
        )


@dataclass(frozen=True, slots=True)
class NiaRefreshCommonResponse:
    """The caller-resolved CommonResponse user-data projection."""

    post_state: NiaRefreshPostState
    response_ref: str = "caller:nia-refresh:common-response"

    def __post_init__(self) -> None:
        if not isinstance(self.post_state, NiaRefreshPostState):
            raise TypeError("Refresh CommonResponse post_state must be typed")
        if not isinstance(self.response_ref, str) or not self.response_ref:
            raise ValueError("Refresh CommonResponse response_ref is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "post_state": self.post_state.to_dict(),
            "response_ref": self.response_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaRefreshCommonResponse":
        value = _mapping(payload, "Refresh CommonResponse")
        _exact(value, {"post_state", "response_ref"}, "Refresh CommonResponse")
        return cls(
            post_state=NiaRefreshPostState.from_dict(
                _mapping(value["post_state"], "Refresh CommonResponse post_state")
            ),
            response_ref=_text(value["response_ref"], "Refresh CommonResponse response_ref"),
        )


@dataclass(frozen=True, slots=True)
class NiaRefreshResponseScenario:
    """Exact ordered Refresh response payload plus CommonResponse state."""

    before_stamina: int
    after_stamina: int
    effect_results: tuple[ProduceEffectResultTrace, ...]
    common_response: NiaRefreshCommonResponse

    def __post_init__(self) -> None:
        _plain_int(self.before_stamina, "Refresh response before_stamina")
        _plain_int(self.after_stamina, "Refresh response after_stamina")
        object.__setattr__(self, "effect_results", _typed_effects(self.effect_results))
        if not isinstance(self.common_response, NiaRefreshCommonResponse):
            raise TypeError("Refresh response common_response must be typed")

    @property
    def post_state(self) -> NiaRefreshPostState:
        return self.common_response.post_state

    def to_dict(self) -> dict[str, object]:
        return {
            "before_stamina": self.before_stamina,
            "after_stamina": self.after_stamina,
            "effect_results": [value.to_dict() for value in self.effect_results],
            "common_response": self.common_response.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaRefreshResponseScenario":
        value = _mapping(payload, "Refresh response")
        _exact(
            value,
            {"before_stamina", "after_stamina", "effect_results", "common_response"},
            "Refresh response",
        )
        return cls(
            before_stamina=_plain_int(value["before_stamina"], "Refresh response before_stamina"),
            after_stamina=_plain_int(value["after_stamina"], "Refresh response after_stamina"),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "Refresh effect"))
                for item in _list(value["effect_results"], "Refresh effect_results")
            ),
            common_response=NiaRefreshCommonResponse.from_dict(
                _mapping(value["common_response"], "Refresh common_response")
            ),
        )


# Friendly aliases used by adapters that call the payload a response rather
# than a scenario.  They remain the same typed value, not a second schema.
NiaRefreshResponse = NiaRefreshResponseScenario
NiaRefreshCommonResponseSnapshot = NiaRefreshCommonResponse


class NiaRefreshScenarioAuthority(StrEnum):
    """Authority of the runtime-only Refresh response."""

    CALLER_RESOLVED_SERVER_RESPONSE = "caller-resolved-server-response"
    CALLER_AUTHORED_SYNTHETIC_EXACT_RESPONSE = (
        "caller-authored-synthetic-exact-response"
    )


@dataclass(frozen=True, slots=True)
class InitialRegularNiaRefreshScenario:
    """Caller-resolved Refresh(14) lifecycle for one outer week."""

    week: int
    refresh_stamina_recovery_permille: int
    branch: ChanceBranch
    response: NiaRefreshResponseScenario
    authority: NiaRefreshScenarioAuthority
    runtime_ref: str
    schedule_refresh_stamina: int | None = None
    # Optional compatibility projection: the canonical value is always the
    # response CommonResponse post-state.  A supplied value must match it.
    post_state: NiaRefreshPostState | None = None

    def __post_init__(self) -> None:
        _plain_int(self.week, "Refresh scenario week", minimum=1)
        _plain_int(
            self.refresh_stamina_recovery_permille,
            "Refresh scenario recovery permille",
        )
        if self.refresh_stamina_recovery_permille > 1000:
            raise ValueError("Refresh scenario recovery permille must be <= 1000")
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("Refresh branch must be ChanceBranch")
        if not isinstance(self.response, NiaRefreshResponseScenario):
            raise TypeError("Refresh response must be typed")
        if self.schedule_refresh_stamina is not None:
            _plain_int(
                self.schedule_refresh_stamina,
                "Refresh schedule_refresh_stamina",
            )
        if self.post_state is not None and self.post_state != self.response.post_state:
            raise ValueError(
                "Refresh post_state must match CommonResponse post_state"
            )
        object.__setattr__(self, "post_state", self.response.post_state)
        authority = self.authority
        if isinstance(authority, str) and not isinstance(
            authority, NiaRefreshScenarioAuthority
        ):
            try:
                authority = NiaRefreshScenarioAuthority(authority)
            except ValueError as error:
                raise ValueError("Refresh scenario authority is invalid") from error
            object.__setattr__(self, "authority", authority)
        if not isinstance(authority, NiaRefreshScenarioAuthority):
            raise TypeError("Refresh scenario authority must be typed")
        if not isinstance(self.runtime_ref, str) or not self.runtime_ref:
            raise ValueError("Refresh caller runtime_ref is required")

    @property
    def authentic_nia_local_save(self) -> bool:
        return False

    @property
    def schedule_refresh_stamina_applied(self) -> bool:
        """The schedule observation is never used as an absolute value."""

        return False

    @property
    def response_stamina_delta(self) -> int:
        return self.response.after_stamina - self.response.before_stamina

    def to_dict(self) -> dict[str, object]:
        # ``post_state`` is a compatibility alias of response.common_response
        # and is intentionally not duplicated in the canonical payload.
        return {
            "week": self.week,
            "refresh_stamina_recovery_permille": self.refresh_stamina_recovery_permille,
            "branch": self.branch.to_dict(),
            "response": self.response.to_dict(),
            "authority": self.authority.value,
            "runtime_ref": self.runtime_ref,
            "schedule_refresh_stamina": self.schedule_refresh_stamina,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularNiaRefreshScenario":
        value = _mapping(payload, "Refresh scenario")
        _exact(
            value,
            {
                "week",
                "refresh_stamina_recovery_permille",
                "branch",
                "response",
                "authority",
                "runtime_ref",
                "schedule_refresh_stamina",
            },
            "Refresh scenario",
        )
        schedule = value["schedule_refresh_stamina"]
        if schedule is not None:
            schedule = _plain_int(schedule, "Refresh schedule_refresh_stamina")
        authority = _text(value["authority"], "Refresh scenario authority")
        try:
            authority_value = NiaRefreshScenarioAuthority(authority)
        except ValueError as error:
            raise ValueError("Refresh scenario authority is invalid") from error
        return cls(
            week=_plain_int(value["week"], "Refresh scenario week", minimum=1),
            refresh_stamina_recovery_permille=_plain_int(
                value["refresh_stamina_recovery_permille"],
                "Refresh scenario recovery permille",
            ),
            branch=_strict_branch(value["branch"], "Refresh scenario branch"),
            response=NiaRefreshResponseScenario.from_dict(
                _mapping(value["response"], "Refresh scenario response")
            ),
            authority=authority_value,
            runtime_ref=_text(value["runtime_ref"], "Refresh scenario runtime_ref"),
            schedule_refresh_stamina=schedule,
        )


def _issue(code: str, field: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _dedupe(
    values: list[InitialRegularInnerStageIssue],
) -> tuple[InitialRegularInnerStageIssue, ...]:
    return tuple(dict.fromkeys(values))


def _scenario_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaRefreshScenario,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    if state.phase is not RolloutPhase.WAITING_EXTERNAL:
        issues.append(_issue("nia-refresh-request-not-pending", "outer_state.phase", state.phase.value))
    if state.pending != request:
        issues.append(_issue("nia-refresh-pending-request-mismatch", "outer_state.pending"))
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(_issue("nia-refresh-request-kind-mismatch", "request.kind", request.kind.value))
    if request.action_id != NIA_REFRESH_ACTION_ID:
        issues.append(_issue("nia-refresh-action-mismatch", "request.action_id", request.action_id))
    if request.stage_type is not None:
        issues.append(_issue("nia-refresh-stage-type-unexpected", "request.stage_type", request.stage_type))
    if request.week != state.week or scenario.week != state.week:
        issues.append(
            _issue(
                "nia-refresh-week-mismatch",
                "request/scenario.week",
                f"state={state.week}:request={request.week}:scenario={scenario.week}",
            )
        )
    if state.mode_id not in {"produce-004", "produce-005"}:
        issues.append(_issue("nia-refresh-produce-id-mismatch", "outer_state.mode_id", state.mode_id))
    response = scenario.response
    if response.before_stamina != state.stamina:
        issues.append(
            _issue(
                "nia-refresh-before-stamina-mismatch",
                "response.before_stamina",
                f"expected={state.stamina}:actual={response.before_stamina}",
            )
        )
    try:
        recovery = validate_nia_refresh_formula(
            before_stamina=state.stamina,
            after_stamina=response.after_stamina,
            max_stamina=state.max_stamina,
            refresh_stamina_recovery_permille=(
                scenario.refresh_stamina_recovery_permille
            ),
        )
    except (TypeError, ValueError) as error:
        recovery = None
        issues.append(_issue("nia-refresh-formula-mismatch", "response.before_stamina/after_stamina", str(error)))
    if (
        scenario.schedule_refresh_stamina is not None
        and recovery is not None
        and scenario.schedule_refresh_stamina != recovery
    ):
        issues.append(
            _issue(
                "nia-refresh-schedule-delta-mismatch",
                ANDROID_REFRESH_SCHEDULE_FIELD,
                f"expected_delta={recovery}:actual={scenario.schedule_refresh_stamina}",
            )
        )
    if response.post_state.stamina != response.after_stamina:
        issues.append(
            _issue(
                "nia-refresh-post-state-stamina-mismatch",
                "common_response.post_state.stamina",
                f"expected={response.after_stamina}:actual={response.post_state.stamina}",
            )
        )
    if scenario.branch.probability is None or scenario.branch.probability <= 0.0:
        issues.append(_issue("nia-refresh-branch-probability-unresolved", "scenario.branch.probability"))
    return _dedupe(issues)


def adapt_initial_regular_nia_refresh_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaRefreshScenario,
) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
    """Project a validated Refresh response to the common outer outcome.

    ``effect_results`` remain evidence in response order.  Any unknown effect
    mutation is preserved by applying the complete CommonResponse post-state;
    no cross-bucket effect ordering is inferred.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularNiaRefreshScenario):
        raise TypeError("scenario must be InitialRegularNiaRefreshScenario")
    issues = _scenario_issues(state, request, scenario)
    if issues:
        return issues
    return WeeklyActionOutcome(
        request_id=request.request_id,
        branch=scenario.branch,
        patch=scenario.post_state.to_patch(),
        reward_requested=False,
    )


InitialRegularNiaRefreshAdapterResult: TypeAlias = (
    WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]
)


# Short aliases mirror the existing Interval adapter naming without creating
# another schema or mutating the shared runtime module.
adapt_nia_refresh_scenario = adapt_initial_regular_nia_refresh_scenario
NiaRefreshScenario = InitialRegularNiaRefreshScenario


__all__ = [
    "ANDROID_REFRESH_COMMON_RESPONSE_FIELDS",
    "ANDROID_REFRESH_RECOVERY_AUTHORITY",
    "ANDROID_REFRESH_REQUEST_FIELDS",
    "ANDROID_REFRESH_RESPONSE_FIELDS",
    "ANDROID_REFRESH_SCHEDULE_FIELD",
    "ANDROID_REFRESH_STEP_TYPE",
    "ANDROID_REFRESH_STEP_TYPE_VALUE",
    "InitialRegularNiaRefreshAdapterResult",
    "InitialRegularNiaRefreshScenario",
    "NIA_REFRESH_ACTION_ID",
    "NiaRefreshCommonResponse",
    "NiaRefreshCommonResponseSnapshot",
    "NiaRefreshPostState",
    "NiaRefreshResponse",
    "NiaRefreshResponseScenario",
    "NiaRefreshScenario",
    "NiaRefreshScenarioAuthority",
    "adapt_initial_regular_nia_refresh_scenario",
    "adapt_nia_refresh_scenario",
    "calculate_nia_refresh_recovery",
    "calculate_refresh_recovery",
    "validate_nia_refresh_formula",
    "validate_refresh_formula",
]
