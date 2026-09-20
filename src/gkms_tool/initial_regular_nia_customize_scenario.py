"""Exact N.I.A. Customize Start/End boundary for an explicit no-card skip.

Android v3.2.3 executes ``CustomizeStart``, zero or more ``CustomizeSelect``
transactions, then ``CustomizeEnd``.  This bounded adapter supports only the
zero-Select path.  Start and End each preserve their own ordered
``EffectResults`` and complete CommonResponse post-state.  A non-zero
``selected_card_count`` is retained only to return a typed blocker; card
selection/customization is never treated as a no-op.

The PC Master ``ProduceSetting`` values are pinned for identity checks.  The
step-skip recovery permil is *not* evaluated here: the caller-owned End
post-state is the sole mutation authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, TypeAlias

from .initial_regular_event_scenario import (
    ProduceEffectResultTrace,
    validate_effect_result_chain,
)
from .initial_regular_inner_protocol import InitialRegularInnerStageIssue
from .nia_static_inventory import NiaStaticInventory
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


NIA_CUSTOMIZE_ACTION_ID = "special_guidance"
ANDROID_CUSTOMIZE_STEP_TYPE = "ProduceStepType_Customize"
ANDROID_CUSTOMIZE_STEP_TYPE_VALUE = 28
ANDROID_CUSTOMIZE_START_REQUEST_TYPE = "ProduceStepCustomizeStartRequest"
ANDROID_CUSTOMIZE_START_RESPONSE_TYPE = "ProduceStepCustomizeStartResponse"
ANDROID_CUSTOMIZE_SELECT_REQUEST_TYPE = "ProduceStepCustomizeSelectRequest"
ANDROID_CUSTOMIZE_SELECT_RESPONSE_TYPE = "ProduceStepCustomizeSelectResponse"
ANDROID_CUSTOMIZE_END_REQUEST_TYPE = "ProduceStepCustomizeEndRequest"
ANDROID_CUSTOMIZE_END_RESPONSE_TYPE = "ProduceStepCustomizeEndResponse"
ANDROID_CUSTOMIZE_START_REQUEST_FIELDS = ("device_produce_uuid",)
ANDROID_CUSTOMIZE_START_RESPONSE_FIELDS = ("effect_results", "common_response")
ANDROID_CUSTOMIZE_SELECT_REQUEST_FIELDS = (
    "deck_produce_card_index",
    "produce_card_customize_id",
    "device_produce_uuid",
)
ANDROID_CUSTOMIZE_SELECT_RESPONSE_FIELDS = (
    "before_produce_point",
    "after_produce_point",
    "effect_results",
    "common_response",
)
ANDROID_CUSTOMIZE_END_REQUEST_FIELDS = ("device_produce_uuid",)
ANDROID_CUSTOMIZE_END_RESPONSE_FIELDS = ("effect_results", "common_response")
ANDROID_CUSTOMIZE_SKIP_LIFECYCLE = (
    "ScheduleCustomizeScreenPresenter.NonBlockOpenInAsync awaits Start; "
    "GoNextAsync can await End without OnCustomizeAsync/Select"
)
ANDROID_CUSTOMIZE_NATIVE_AUTHORITY = (
    "ScheduleCustomizeScreenPresenter.<NonBlockOpenInAsync>d__5.MoveNext",
    "ScheduleCustomizeScreenPresenter.<GoNextAsync>d__10.MoveNext",
    "ProduceApiUtility.<ProduceStepCustomizeStartRequestAsync>d__37.MoveNext",
    "ProduceApiUtility.<ProduceStepCustomizeEndRequestAsync>d__39.MoveNext",
)


def _int(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _text(value: object, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise ValueError(f"{field} must be text")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return value


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return value


def _exact(value: Mapping[str, object], fields: set[str], field: str) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        raise ValueError(f"{field} keys mismatch:missing={missing}:extra={extra}")


def _branch(value: object, field: str) -> ChanceBranch:
    payload = _mapping(value, field)
    _exact(
        payload,
        {"branch_id", "probability", "rng_token", "event_id", "was_sp"},
        field,
    )
    return ChanceBranch.from_dict(payload)


def _attributes(value: object, field: str) -> AttributeValues:
    payload = _mapping(value, field)
    _exact(payload, {"vocal", "dance", "visual"}, field)
    return AttributeValues.from_dict(payload)


def _deck_entry(value: object, field: str) -> DeckEntry:
    payload = _mapping(value, field)
    _exact(payload, {"card_id", "upgrade", "count", "instance_ids"}, field)
    return DeckEntry.from_dict(payload)


def _refs(value: object, field: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{field}[{index}]", empty=True)
        for index, item in enumerate(_list(value, field))
    )


@dataclass(frozen=True, slots=True)
class NiaCustomizeMasterSetting:
    produce_id: str
    produce_setting_id: str
    customize_produce_card_count: int
    step_skip_stamina_recovery_permille: int

    @classmethod
    def from_inventory(cls, inventory: NiaStaticInventory) -> "NiaCustomizeMasterSetting":
        if not isinstance(inventory, NiaStaticInventory):
            raise TypeError("inventory must be NiaStaticInventory")
        return cls(
            inventory.produce_id,
            inventory.produce_setting_id,
            inventory.setting.customize_produce_card_count,
            inventory.setting.step_skip_stamina_recovery_permille,
        )

    def __post_init__(self) -> None:
        _text(self.produce_id, "Customize Master produce_id")
        _text(self.produce_setting_id, "Customize Master produce_setting_id")
        _int(self.customize_produce_card_count, "Customize Master card count")
        _int(self.step_skip_stamina_recovery_permille, "Customize Master skip recovery")

    def to_dict(self) -> dict[str, object]:
        return {
            "produce_id": self.produce_id,
            "produce_setting_id": self.produce_setting_id,
            "customize_produce_card_count": self.customize_produce_card_count,
            "step_skip_stamina_recovery_permille": self.step_skip_stamina_recovery_permille,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaCustomizeMasterSetting":
        value = _mapping(payload, "Customize Master setting")
        _exact(
            value,
            {
                "produce_id",
                "produce_setting_id",
                "customize_produce_card_count",
                "step_skip_stamina_recovery_permille",
            },
            "Customize Master setting",
        )
        return cls(
            _text(value["produce_id"], "Customize Master produce_id"),
            _text(value["produce_setting_id"], "Customize Master produce_setting_id"),
            _int(value["customize_produce_card_count"], "Customize Master card count"),
            _int(value["step_skip_stamina_recovery_permille"], "Customize Master skip recovery"),
        )


@dataclass(frozen=True, slots=True)
class NiaCustomizeRequestScenario:
    device_produce_uuid_ref: str

    def __post_init__(self) -> None:
        _text(self.device_produce_uuid_ref, "Customize request UUID ref")

    def to_dict(self) -> dict[str, object]:
        return {"device_produce_uuid_ref": self.device_produce_uuid_ref}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaCustomizeRequestScenario":
        value = _mapping(payload, "Customize request")
        _exact(value, {"device_produce_uuid_ref"}, "Customize request")
        return cls(_text(value["device_produce_uuid_ref"], "Customize request UUID ref"))


@dataclass(frozen=True, slots=True)
class NiaCustomizePostState:
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
        _int(self.stamina, "Customize post stamina")
        _int(self.max_stamina, "Customize post max_stamina", minimum=1)
        _int(self.produce_points, "Customize post produce_points")
        if self.stamina > self.max_stamina:
            raise ValueError("Customize post stamina exceeds max_stamina")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("Customize post attributes must be typed")
        deck = tuple(self.deck)
        if not all(isinstance(value, DeckEntry) for value in deck):
            raise TypeError("Customize post deck must contain DeckEntry values")
        object.__setattr__(self, "deck", deck)
        for field in (
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        ):
            values = tuple(getattr(self, field))
            if any(not isinstance(value, str) for value in values):
                raise TypeError(f"Customize post {field} must contain text")
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
    ) -> "NiaCustomizePostState":
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        return cls(
            state.stamina if stamina is None else stamina,
            state.max_stamina if max_stamina is None else max_stamina,
            state.produce_points if produce_points is None else produce_points,
            state.attributes if attributes is None else attributes,
            state.deck if deck is None else deck,
            state.item_session_refs,
            state.drink_session_refs,
            state.passive_session_refs,
            state.excluded_reward_card_ids,
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
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaCustomizePostState":
        value = _mapping(payload, "Customize post state")
        fields = {
            "stamina", "max_stamina", "produce_points", "attributes", "deck",
            "item_session_refs", "drink_session_refs", "passive_session_refs",
            "excluded_reward_card_ids",
        }
        _exact(value, fields, "Customize post state")
        return cls(
            stamina=_int(value["stamina"], "Customize post stamina"),
            max_stamina=_int(value["max_stamina"], "Customize post max_stamina", minimum=1),
            produce_points=_int(value["produce_points"], "Customize post produce_points"),
            attributes=_attributes(value["attributes"], "Customize post attributes"),
            deck=tuple(
                _deck_entry(item, "Customize post deck entry")
                for item in _list(value["deck"], "Customize post deck")
            ),
            item_session_refs=_refs(value["item_session_refs"], "Customize post item_session_refs"),
            drink_session_refs=_refs(value["drink_session_refs"], "Customize post drink_session_refs"),
            passive_session_refs=_refs(value["passive_session_refs"], "Customize post passive_session_refs"),
            excluded_reward_card_ids=_refs(value["excluded_reward_card_ids"], "Customize post excluded_reward_card_ids"),
        )


@dataclass(frozen=True, slots=True)
class NiaCustomizeResponseScenario:
    effect_results: tuple[ProduceEffectResultTrace, ...]
    common_post_state: NiaCustomizePostState

    def __post_init__(self) -> None:
        effects = tuple(self.effect_results)
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("Customize response effects must be typed")
        validate_effect_result_chain(effects)
        object.__setattr__(self, "effect_results", effects)
        if not isinstance(self.common_post_state, NiaCustomizePostState):
            raise TypeError("Customize response post-state must be typed")

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_results": [value.to_dict() for value in self.effect_results],
            "common_post_state": self.common_post_state.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaCustomizeResponseScenario":
        value = _mapping(payload, "Customize response")
        _exact(value, {"effect_results", "common_post_state"}, "Customize response")
        return cls(
            tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "Customize effect result"))
                for item in _list(value["effect_results"], "Customize effect_results")
            ),
            NiaCustomizePostState.from_dict(
                _mapping(value["common_post_state"], "Customize common_post_state")
            ),
        )


class NiaCustomizeScenarioAuthority(StrEnum):
    CALLER_RESOLVED_SERVER_RESPONSE = "caller-resolved-server-response"
    CALLER_AUTHORED_SYNTHETIC_EXACT_RESPONSE = "caller-authored-synthetic-exact-response"


@dataclass(frozen=True, slots=True)
class InitialRegularNiaCustomizeScenario:
    week: int
    branch: ChanceBranch
    master_setting: NiaCustomizeMasterSetting
    start_request: NiaCustomizeRequestScenario
    start_response: NiaCustomizeResponseScenario
    selected_card_count: int
    end_request: NiaCustomizeRequestScenario
    end_response: NiaCustomizeResponseScenario
    authority: NiaCustomizeScenarioAuthority
    runtime_ref: str

    def __post_init__(self) -> None:
        _int(self.week, "Customize scenario week", minimum=1)
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("Customize scenario branch must be typed")
        if not isinstance(self.master_setting, NiaCustomizeMasterSetting):
            raise TypeError("Customize scenario master_setting must be typed")
        if not isinstance(self.start_request, NiaCustomizeRequestScenario):
            raise TypeError("Customize scenario start_request must be typed")
        if not isinstance(self.start_response, NiaCustomizeResponseScenario):
            raise TypeError("Customize scenario start_response must be typed")
        _int(self.selected_card_count, "Customize selected_card_count")
        if not isinstance(self.end_request, NiaCustomizeRequestScenario):
            raise TypeError("Customize scenario end_request must be typed")
        if not isinstance(self.end_response, NiaCustomizeResponseScenario):
            raise TypeError("Customize scenario end_response must be typed")
        if self.start_request.device_produce_uuid_ref != self.end_request.device_produce_uuid_ref:
            raise ValueError("Customize Start/End UUID refs disagree")
        authority = self.authority
        if isinstance(authority, str) and not isinstance(authority, NiaCustomizeScenarioAuthority):
            try:
                authority = NiaCustomizeScenarioAuthority(authority)
            except ValueError as error:
                raise ValueError("Customize scenario authority is invalid") from error
            object.__setattr__(self, "authority", authority)
        if not isinstance(authority, NiaCustomizeScenarioAuthority):
            raise TypeError("Customize scenario authority must be typed")
        _text(self.runtime_ref, "Customize scenario runtime_ref")

    @property
    def post_state(self) -> NiaCustomizePostState:
        return self.end_response.common_post_state

    @property
    def authentic_nia_local_save(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "branch": self.branch.to_dict(),
            "master_setting": self.master_setting.to_dict(),
            "start_request": self.start_request.to_dict(),
            "start_response": self.start_response.to_dict(),
            "selected_card_count": self.selected_card_count,
            "end_request": self.end_request.to_dict(),
            "end_response": self.end_response.to_dict(),
            "authority": self.authority.value,
            "runtime_ref": self.runtime_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularNiaCustomizeScenario":
        value = _mapping(payload, "Customize scenario")
        fields = {
            "week", "branch", "master_setting", "start_request", "start_response",
            "selected_card_count", "end_request", "end_response", "authority",
            "runtime_ref",
        }
        _exact(value, fields, "Customize scenario")
        try:
            authority = NiaCustomizeScenarioAuthority(
                _text(value["authority"], "Customize scenario authority")
            )
        except ValueError as error:
            raise ValueError("Customize scenario authority is invalid") from error
        return cls(
            week=_int(value["week"], "Customize scenario week", minimum=1),
            branch=_branch(value["branch"], "Customize scenario branch"),
            master_setting=NiaCustomizeMasterSetting.from_dict(
                _mapping(value["master_setting"], "Customize Master setting")
            ),
            start_request=NiaCustomizeRequestScenario.from_dict(
                _mapping(value["start_request"], "Customize Start request")
            ),
            start_response=NiaCustomizeResponseScenario.from_dict(
                _mapping(value["start_response"], "Customize Start response")
            ),
            selected_card_count=_int(value["selected_card_count"], "Customize selected_card_count"),
            end_request=NiaCustomizeRequestScenario.from_dict(
                _mapping(value["end_request"], "Customize End request")
            ),
            end_response=NiaCustomizeResponseScenario.from_dict(
                _mapping(value["end_response"], "Customize End response")
            ),
            authority=authority,
            runtime_ref=_text(value["runtime_ref"], "Customize scenario runtime_ref"),
        )


def _issue(code: str, field: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def adapt_initial_regular_nia_customize_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaCustomizeScenario,
    inventory: NiaStaticInventory,
) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
    """Apply only an explicit Start→End path with zero Select requests."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularNiaCustomizeScenario):
        raise TypeError("scenario must be InitialRegularNiaCustomizeScenario")
    if not isinstance(inventory, NiaStaticInventory):
        raise TypeError("inventory must be NiaStaticInventory")
    issues: list[InitialRegularInnerStageIssue] = []
    if state.phase is not RolloutPhase.WAITING_EXTERNAL:
        issues.append(_issue("nia-customize-request-not-pending", "outer_state.phase", state.phase.value))
    if state.pending != request:
        issues.append(_issue("nia-customize-pending-request-mismatch", "outer_state.pending"))
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(_issue("nia-customize-request-kind-mismatch", "request.kind", request.kind.value))
    if request.action_id != NIA_CUSTOMIZE_ACTION_ID:
        issues.append(_issue("nia-customize-action-mismatch", "request.action_id", request.action_id))
    if request.week != state.week or scenario.week != state.week:
        issues.append(
            _issue(
                "nia-customize-week-mismatch",
                "request/scenario.week",
                f"state={state.week}:request={request.week}:scenario={scenario.week}",
            )
        )
    if request.stage_type is not None:
        issues.append(_issue("nia-customize-stage-type-unexpected", "request.stage_type", request.stage_type))
    if scenario.branch.probability is None or scenario.branch.probability <= 0:
        issues.append(_issue("nia-customize-branch-probability-unresolved", "scenario.branch.probability"))
    expected = NiaCustomizeMasterSetting.from_inventory(inventory)
    if state.mode_id != inventory.produce_id or scenario.master_setting != expected:
        issues.append(
            _issue(
                "nia-customize-master-setting-mismatch",
                "scenario.master_setting",
                f"state={state.mode_id}:expected={expected!r}:actual={scenario.master_setting!r}",
            )
        )
    if scenario.selected_card_count != 0:
        issues.append(
            _issue(
                "nia-customize-card-selection-unsupported",
                "scenario.selected_card_count",
                (
                    f"selected={scenario.selected_card_count}:master_limit="
                    f"{expected.customize_produce_card_count}; only explicit no-card End is executable"
                ),
            )
        )
    if issues:
        return tuple(dict.fromkeys(issues))
    return WeeklyActionOutcome(
        request.request_id,
        scenario.branch,
        scenario.post_state.to_patch(),
        reward_requested=False,
    )


InitialRegularNiaCustomizeAdapterResult: TypeAlias = (
    WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "ANDROID_CUSTOMIZE_END_REQUEST_FIELDS",
    "ANDROID_CUSTOMIZE_END_REQUEST_TYPE",
    "ANDROID_CUSTOMIZE_END_RESPONSE_FIELDS",
    "ANDROID_CUSTOMIZE_END_RESPONSE_TYPE",
    "ANDROID_CUSTOMIZE_NATIVE_AUTHORITY",
    "ANDROID_CUSTOMIZE_SELECT_REQUEST_FIELDS",
    "ANDROID_CUSTOMIZE_SELECT_REQUEST_TYPE",
    "ANDROID_CUSTOMIZE_SELECT_RESPONSE_FIELDS",
    "ANDROID_CUSTOMIZE_SELECT_RESPONSE_TYPE",
    "ANDROID_CUSTOMIZE_SKIP_LIFECYCLE",
    "ANDROID_CUSTOMIZE_START_REQUEST_FIELDS",
    "ANDROID_CUSTOMIZE_START_REQUEST_TYPE",
    "ANDROID_CUSTOMIZE_START_RESPONSE_FIELDS",
    "ANDROID_CUSTOMIZE_START_RESPONSE_TYPE",
    "ANDROID_CUSTOMIZE_STEP_TYPE",
    "ANDROID_CUSTOMIZE_STEP_TYPE_VALUE",
    "InitialRegularNiaCustomizeAdapterResult",
    "InitialRegularNiaCustomizeScenario",
    "NIA_CUSTOMIZE_ACTION_ID",
    "NiaCustomizeMasterSetting",
    "NiaCustomizePostState",
    "NiaCustomizeRequestScenario",
    "NiaCustomizeResponseScenario",
    "NiaCustomizeScenarioAuthority",
    "adapt_initial_regular_nia_customize_scenario",
]
