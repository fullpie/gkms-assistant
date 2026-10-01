"""Exact caller-resolved N.I.A. ``EventBusiness`` response boundary.

Android v3.2.3 does not expose an EventBusiness-specific Start/End API.  The
selected schedule step enters the ordinary event screen and sends one
``ProduceStepEventRequest``.  The server owns the selected detail and the
success roll; its response owns the order of ``EffectResults``.  PC Master is
therefore used only to validate the exact detail/suggestion identity and to
retain its three effect buckets separately.  This module never invents a
detail pool, rolls a success locally, or flattens those buckets into a claimed
execution order.

The response trace is evidence.  The complete post-state is the mutation
authority because the native evidence does not establish an ordering between
detail, suggestion-base, success/fail, memory-reward, and CommonResponse
updates.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, TypeAlias

from .initial_regular_event_scenario import (
    EffectiveDirectCosts,
    NextStepRef,
    ProduceEffectResultTrace,
    ProduceRewardResultSnapshot,
    validate_effect_result_chain,
)
from .initial_regular_inner_protocol import InitialRegularInnerStageIssue
from .master_db import DEFAULT_DATABASE
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
from .route_calendar import EVENT_BUSINESS


ANDROID_EVENT_BUSINESS_STEP_TYPE = "ProduceStepType_EventBusiness"
ANDROID_EVENT_BUSINESS_STEP_TYPE_VALUE = 26
ANDROID_EVENT_BUSINESS_REQUEST_TYPE = "ProduceStepEventRequest"
ANDROID_EVENT_BUSINESS_RESPONSE_TYPE = "ProduceStepEventResponse"
ANDROID_EVENT_BUSINESS_REQUEST_FIELDS = (
    "suggestion_index",
    "is_skip",
    "is_fast_forward",
    "device_produce_uuid",
)
ANDROID_EVENT_BUSINESS_RESPONSE_FIELDS = (
    "success",
    "memory_reward_results",
    "effect_results",
    "common_response",
)
ANDROID_EVENT_BUSINESS_PROGRESS_EVENT_FIELDS = (
    "produce_step_event_detail_id",
    "number",
    "suggestion_index",
    "success",
)
ANDROID_EVENT_BUSINESS_LIFECYCLE = (
    "single ProduceStepEvent request/response; no dedicated EventBusiness "
    "Start/choice/End wire"
)
ANDROID_EVENT_BUSINESS_NATIVE_AUTHORITY = (
    "ScheduleEventScreenPresenter.<StepEventRequestAsync>d__45.MoveNext",
    "ProduceApiUtility.<ProduceStepEventRequestAsync>d__30.MoveNext",
)

_DETAIL_ID = re.compile(
    r"^event-detail-business-(?P<produce>produce_00[45])-plan3-"
)
_EVENT_TYPE = "ProduceEventType_Business"
_UNKNOWN_STEP_TYPE = "ProduceStepType_Unknown"
_MODELED_EFFECT_TYPES = frozenset(
    {
        "ProduceEffectType_VoteCountAddition",
        "ProduceEffectType_VocalAddition",
        "ProduceEffectType_DanceAddition",
        "ProduceEffectType_VisualAddition",
        "ProduceEffectType_ProducePointAddition",
        "ProduceEffectType_StaminaRecoverFix",
        "ProduceEffectType_ProduceRewardSet",
    }
)


def _plain_int(value: object, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return value


def _text(value: object, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    if not empty and not value:
        raise ValueError(f"{field} must be non-empty")
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


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be boolean")
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


def _sqlite_bool(value: object, field: str) -> bool:
    if value not in (0, 1):
        raise NiaEventBusinessMasterError(
            "invalid-master-boolean",
            field,
            repr(value),
        )
    return bool(value)


def _id_list(value: object, field: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(_text(value, field))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise NiaEventBusinessMasterError(
            "invalid-master-id-list",
            field,
            str(error),
        ) from error
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) or not item for item in decoded
    ):
        raise NiaEventBusinessMasterError(
            "invalid-master-id-list",
            field,
            repr(decoded),
        )
    return tuple(decoded)


class NiaEventBusinessMasterError(ValueError):
    """A stable fail-closed error at the PC Master projection boundary."""

    def __init__(self, code: str, field: str, detail: str) -> None:
        self.code = _text(code, "master error code")
        self.field = _text(field, "master error field")
        self.detail = _text(detail, "master error detail")
        super().__init__(f"{self.code}:{self.field}:{self.detail}")


@dataclass(frozen=True, slots=True)
class NiaEventBusinessStaticEffect:
    """One ordered PC Master effect row inside one named static bucket."""

    effect_id: str
    effect_type: str
    effect_value_min: int
    effect_value_max: int
    resource_type: str

    def __post_init__(self) -> None:
        _text(self.effect_id, "EventBusiness effect id")
        _text(self.effect_type, "EventBusiness effect type")
        minimum = _plain_int(self.effect_value_min, "EventBusiness effect minimum")
        maximum = _plain_int(self.effect_value_max, "EventBusiness effect maximum")
        if minimum > maximum:
            raise ValueError("EventBusiness effect range is reversed")
        _text(self.resource_type, "EventBusiness effect resource type", empty=True)


@dataclass(frozen=True, slots=True)
class NiaEventBusinessMasterChoice:
    """Exact Master identity; its buckets intentionally have no global order."""

    mode_id: str
    detail_id: str
    produce_story_group_id: str
    is_business_excellent: bool
    suggestion_id: str
    suggestion_index: int
    actual_success: bool
    always_successful: bool
    success_probability_permyriad: int
    raw_stamina_cost: int
    raw_produce_point_cost: int
    direct_produce_card_id: str
    direct_produce_card_upgrade_count: int
    detail_effects: tuple[NiaEventBusinessStaticEffect, ...]
    base_effects: tuple[NiaEventBusinessStaticEffect, ...]
    success_or_fail_effects: tuple[NiaEventBusinessStaticEffect, ...]
    selected_outcome: str
    direct_next_step: NextStepRef | None
    success_or_fail_next_step: NextStepRef | None
    produce_effect_fire_step: int
    is_campaign: bool

    def __post_init__(self) -> None:
        for field in (
            "mode_id",
            "detail_id",
            "produce_story_group_id",
            "suggestion_id",
        ):
            _text(getattr(self, field), f"EventBusiness Master {field}")
        _plain_int(self.suggestion_index, "EventBusiness suggestion index", minimum=0)
        probability = _plain_int(
            self.success_probability_permyriad,
            "EventBusiness success probability",
            minimum=0,
        )
        if probability > 10_000:
            raise ValueError("EventBusiness success probability exceeds 10000")
        for field in (
            "raw_stamina_cost",
            "raw_produce_point_cost",
            "direct_produce_card_upgrade_count",
            "produce_effect_fire_step",
        ):
            _plain_int(getattr(self, field), f"EventBusiness Master {field}", minimum=0)
        _text(
            self.direct_produce_card_id,
            "EventBusiness direct card id",
            empty=True,
        )
        for field in (
            "detail_effects",
            "base_effects",
            "success_or_fail_effects",
        ):
            values = tuple(getattr(self, field))
            if not all(isinstance(value, NiaEventBusinessStaticEffect) for value in values):
                raise TypeError(f"{field} must contain typed static effects")
            object.__setattr__(self, field, values)
        if self.selected_outcome not in {"success", "fail"}:
            raise ValueError("EventBusiness selected outcome is invalid")
        if (self.selected_outcome == "success") != self.actual_success:
            raise ValueError("EventBusiness selected outcome disagrees with response")
        for field in ("direct_next_step", "success_or_fail_next_step"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, NextStepRef):
                raise TypeError(f"{field} must be NextStepRef or None")


@dataclass(frozen=True, slots=True)
class NiaEventBusinessRequestScenario:
    """One exact generic ProduceStepEvent request made from EventBusiness."""

    suggestion_index: int
    is_skip: bool
    is_fast_forward: bool
    device_produce_uuid_ref: str

    def __post_init__(self) -> None:
        _plain_int(self.suggestion_index, "EventBusiness request suggestion_index", minimum=0)
        if not isinstance(self.is_skip, bool) or not isinstance(self.is_fast_forward, bool):
            raise TypeError("EventBusiness request flags must be boolean")
        _text(
            self.device_produce_uuid_ref,
            "EventBusiness request device_produce_uuid_ref",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "suggestion_index": self.suggestion_index,
            "is_skip": self.is_skip,
            "is_fast_forward": self.is_fast_forward,
            "device_produce_uuid_ref": self.device_produce_uuid_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaEventBusinessRequestScenario":
        value = _mapping(payload, "EventBusiness request")
        _exact(
            value,
            {"suggestion_index", "is_skip", "is_fast_forward", "device_produce_uuid_ref"},
            "EventBusiness request",
        )
        return cls(
            suggestion_index=_plain_int(value["suggestion_index"], "EventBusiness request suggestion_index", minimum=0),
            is_skip=_bool(value["is_skip"], "EventBusiness request is_skip"),
            is_fast_forward=_bool(value["is_fast_forward"], "EventBusiness request is_fast_forward"),
            device_produce_uuid_ref=_text(value["device_produce_uuid_ref"], "EventBusiness request device_produce_uuid_ref"),
        )


@dataclass(frozen=True, slots=True)
class NiaEventBusinessMemoryRewardResult:
    """Proto-shaped ``ProduceMemoryRewardResult`` from response field 2."""

    provided_rewards: tuple[ProduceRewardResultSnapshot, ...]
    origin: str

    def __post_init__(self) -> None:
        rewards = tuple(self.provided_rewards)
        if not all(isinstance(value, ProduceRewardResultSnapshot) for value in rewards):
            raise TypeError("EventBusiness memory rewards must be typed")
        object.__setattr__(self, "provided_rewards", rewards)
        _text(self.origin, "EventBusiness memory reward origin")

    def to_dict(self) -> dict[str, object]:
        return {
            "provided_rewards": [value.to_dict() for value in self.provided_rewards],
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaEventBusinessMemoryRewardResult":
        value = _mapping(payload, "EventBusiness memory reward")
        _exact(value, {"provided_rewards", "origin"}, "EventBusiness memory reward")
        return cls(
            provided_rewards=tuple(
                ProduceRewardResultSnapshot.from_dict(_mapping(item, "EventBusiness memory reward item"))
                for item in _list(value["provided_rewards"], "EventBusiness provided_rewards")
            ),
            origin=_text(value["origin"], "EventBusiness memory reward origin"),
        )


@dataclass(frozen=True, slots=True)
class NiaEventBusinessProgressEventSnapshot:
    """Exact post-response ``UserProduceProgressEvent`` identity row."""

    produce_step_event_detail_id: str
    number: int
    suggestion_index: int
    success: bool

    def __post_init__(self) -> None:
        _text(
            self.produce_step_event_detail_id,
            "EventBusiness progress detail id",
        )
        _plain_int(self.number, "EventBusiness progress number", minimum=0)
        _plain_int(
            self.suggestion_index,
            "EventBusiness progress suggestion_index",
            minimum=0,
        )
        if not isinstance(self.success, bool):
            raise TypeError("EventBusiness progress success must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "produce_step_event_detail_id": self.produce_step_event_detail_id,
            "number": self.number,
            "suggestion_index": self.suggestion_index,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaEventBusinessProgressEventSnapshot":
        value = _mapping(payload, "EventBusiness progress event")
        _exact(
            value,
            {"produce_step_event_detail_id", "number", "suggestion_index", "success"},
            "EventBusiness progress event",
        )
        return cls(
            produce_step_event_detail_id=_text(value["produce_step_event_detail_id"], "EventBusiness progress detail id"),
            number=_plain_int(value["number"], "EventBusiness progress number", minimum=0),
            suggestion_index=_plain_int(value["suggestion_index"], "EventBusiness progress suggestion_index", minimum=0),
            success=_bool(value["success"], "EventBusiness progress success"),
        )


@dataclass(frozen=True, slots=True)
class NiaEventBusinessResponseScenario:
    """One server-resolved ProduceStepEvent response, preserving list order."""

    success: bool
    memory_reward_results: tuple[NiaEventBusinessMemoryRewardResult, ...]
    effect_results: tuple[ProduceEffectResultTrace, ...]
    progress_event: NiaEventBusinessProgressEventSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("EventBusiness response success must be boolean")
        memory = tuple(self.memory_reward_results)
        if not all(isinstance(value, NiaEventBusinessMemoryRewardResult) for value in memory):
            raise TypeError("EventBusiness memory_reward_results must be typed")
        object.__setattr__(self, "memory_reward_results", memory)
        effects = tuple(self.effect_results)
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("EventBusiness effect_results must be typed")
        validate_effect_result_chain(effects)
        object.__setattr__(self, "effect_results", effects)
        if not isinstance(self.progress_event, NiaEventBusinessProgressEventSnapshot):
            raise TypeError("EventBusiness progress_event must be typed")

    def to_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "memory_reward_results": [value.to_dict() for value in self.memory_reward_results],
            "effect_results": [value.to_dict() for value in self.effect_results],
            "progress_event": self.progress_event.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaEventBusinessResponseScenario":
        value = _mapping(payload, "EventBusiness response")
        _exact(
            value,
            {"success", "memory_reward_results", "effect_results", "progress_event"},
            "EventBusiness response",
        )
        return cls(
            success=_bool(value["success"], "EventBusiness response success"),
            memory_reward_results=tuple(
                NiaEventBusinessMemoryRewardResult.from_dict(_mapping(item, "EventBusiness memory reward"))
                for item in _list(value["memory_reward_results"], "EventBusiness memory_reward_results")
            ),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "EventBusiness effect"))
                for item in _list(value["effect_results"], "EventBusiness effect_results")
            ),
            progress_event=NiaEventBusinessProgressEventSnapshot.from_dict(
                _mapping(value["progress_event"], "EventBusiness progress_event")
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaEventBusinessPostState:
    """Complete simulator-owned persistent state after CommonResponse.

    Vote count is retained even though the common rollout state does not yet
    store it; ``nia_outer_action_runtime`` exposes its exact delta alongside
    the kernel patch.
    """

    stamina: int
    max_stamina: int
    produce_points: int
    vote_count: int
    attributes: AttributeValues
    deck: tuple[DeckEntry, ...]
    item_session_refs: tuple[str, ...] = ()
    drink_session_refs: tuple[str, ...] = ()
    passive_session_refs: tuple[str, ...] = ()
    excluded_reward_card_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _plain_int(self.stamina, "EventBusiness post stamina", minimum=0)
        _plain_int(self.max_stamina, "EventBusiness post max_stamina", minimum=1)
        _plain_int(self.produce_points, "EventBusiness post produce_points", minimum=0)
        _plain_int(self.vote_count, "EventBusiness post vote_count", minimum=0)
        if self.stamina > self.max_stamina:
            raise ValueError("EventBusiness post stamina exceeds max_stamina")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("EventBusiness post attributes must be typed")
        deck = tuple(self.deck)
        if not all(isinstance(value, DeckEntry) for value in deck):
            raise TypeError("EventBusiness post deck must contain DeckEntry values")
        object.__setattr__(self, "deck", deck)
        for field in (
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        ):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        self.to_patch()

    @classmethod
    def from_outer_state(
        cls,
        state: ProduceRolloutState,
        *,
        vote_count: int,
        stamina: int | None = None,
        max_stamina: int | None = None,
        produce_points: int | None = None,
        attributes: AttributeValues | None = None,
        deck: tuple[DeckEntry, ...] | None = None,
        item_session_refs: tuple[str, ...] | None = None,
        drink_session_refs: tuple[str, ...] | None = None,
        passive_session_refs: tuple[str, ...] | None = None,
    ) -> "NiaEventBusinessPostState":
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        return cls(
            stamina=state.stamina if stamina is None else stamina,
            max_stamina=state.max_stamina if max_stamina is None else max_stamina,
            produce_points=(
                state.produce_points if produce_points is None else produce_points
            ),
            vote_count=vote_count,
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
            excluded_reward_card_ids=state.excluded_reward_card_ids,
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
            "vote_count": self.vote_count,
            "attributes": self.attributes.to_dict(),
            "deck": [value.to_dict() for value in self.deck],
            "item_session_refs": list(self.item_session_refs),
            "drink_session_refs": list(self.drink_session_refs),
            "passive_session_refs": list(self.passive_session_refs),
            "excluded_reward_card_ids": list(self.excluded_reward_card_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaEventBusinessPostState":
        value = _mapping(payload, "EventBusiness post state")
        fields = {
            "stamina",
            "max_stamina",
            "produce_points",
            "vote_count",
            "attributes",
            "deck",
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        }
        _exact(value, fields, "EventBusiness post state")

        def _refs(name: str) -> tuple[str, ...]:
            return tuple(
                _text(item, f"EventBusiness post {name} item", empty=True)
                for item in _list(value[name], f"EventBusiness post {name}")
            )

        return cls(
            stamina=_plain_int(value["stamina"], "EventBusiness post stamina", minimum=0),
            max_stamina=_plain_int(value["max_stamina"], "EventBusiness post max_stamina", minimum=1),
            produce_points=_plain_int(value["produce_points"], "EventBusiness post produce_points", minimum=0),
            vote_count=_plain_int(value["vote_count"], "EventBusiness post vote_count", minimum=0),
            attributes=_strict_attributes(value["attributes"], "EventBusiness post attributes"),
            deck=tuple(
                _strict_deck_entry(item, "EventBusiness post deck entry")
                for item in _list(value["deck"], "EventBusiness post deck")
            ),
            item_session_refs=_refs("item_session_refs"),
            drink_session_refs=_refs("drink_session_refs"),
            passive_session_refs=_refs("passive_session_refs"),
            excluded_reward_card_ids=_refs("excluded_reward_card_ids"),
        )


class NiaEventBusinessScenarioAuthority(StrEnum):
    CALLER_RESOLVED_SERVER_RESPONSE = "caller-resolved-server-response"
    CALLER_AUTHORED_SYNTHETIC_EXACT_RESPONSE = (
        "caller-authored-synthetic-exact-response"
    )


@dataclass(frozen=True, slots=True)
class InitialRegularNiaEventBusinessScenario:
    """One exact caller-owned EventBusiness request/response transaction."""

    week: int
    detail_id: str
    suggestion_id: str
    branch: ChanceBranch
    request: NiaEventBusinessRequestScenario
    response: NiaEventBusinessResponseScenario
    effective_direct_costs: EffectiveDirectCosts
    vote_count_before: int
    post_state: NiaEventBusinessPostState
    authority: NiaEventBusinessScenarioAuthority
    runtime_ref: str

    def __post_init__(self) -> None:
        _plain_int(self.week, "EventBusiness scenario week", minimum=1)
        _text(self.detail_id, "EventBusiness scenario detail id")
        _text(self.suggestion_id, "EventBusiness scenario suggestion id")
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("EventBusiness scenario branch must be typed")
        if not isinstance(self.request, NiaEventBusinessRequestScenario):
            raise TypeError("EventBusiness scenario request must be typed")
        if not isinstance(self.response, NiaEventBusinessResponseScenario):
            raise TypeError("EventBusiness scenario response must be typed")
        if not isinstance(self.effective_direct_costs, EffectiveDirectCosts):
            raise TypeError("EventBusiness effective_direct_costs must be typed")
        _plain_int(self.vote_count_before, "EventBusiness vote_count_before", minimum=0)
        if not isinstance(self.post_state, NiaEventBusinessPostState):
            raise TypeError("EventBusiness scenario post_state must be typed")
        authority = self.authority
        if isinstance(authority, str) and not isinstance(
            authority,
            NiaEventBusinessScenarioAuthority,
        ):
            try:
                authority = NiaEventBusinessScenarioAuthority(authority)
            except ValueError as error:
                raise ValueError("EventBusiness scenario authority is invalid") from error
            object.__setattr__(self, "authority", authority)
        if not isinstance(authority, NiaEventBusinessScenarioAuthority):
            raise TypeError("EventBusiness scenario authority must be typed")
        _text(self.runtime_ref, "EventBusiness scenario runtime_ref")

    @property
    def authentic_nia_local_save(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "detail_id": self.detail_id,
            "suggestion_id": self.suggestion_id,
            "branch": self.branch.to_dict(),
            "request": self.request.to_dict(),
            "response": self.response.to_dict(),
            "effective_direct_costs": self.effective_direct_costs.to_dict(),
            "vote_count_before": self.vote_count_before,
            "post_state": self.post_state.to_dict(),
            "authority": self.authority.value,
            "runtime_ref": self.runtime_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularNiaEventBusinessScenario":
        value = _mapping(payload, "EventBusiness scenario")
        _exact(
            value,
            {
                "week",
                "detail_id",
                "suggestion_id",
                "branch",
                "request",
                "response",
                "effective_direct_costs",
                "vote_count_before",
                "post_state",
                "authority",
                "runtime_ref",
            },
            "EventBusiness scenario",
        )
        authority = _text(value["authority"], "EventBusiness scenario authority")
        try:
            authority_value = NiaEventBusinessScenarioAuthority(authority)
        except ValueError as error:
            raise ValueError("EventBusiness scenario authority is invalid") from error
        return cls(
            week=_plain_int(value["week"], "EventBusiness scenario week", minimum=1),
            detail_id=_text(value["detail_id"], "EventBusiness scenario detail id"),
            suggestion_id=_text(value["suggestion_id"], "EventBusiness scenario suggestion id"),
            branch=_strict_branch(value["branch"], "EventBusiness scenario branch"),
            request=NiaEventBusinessRequestScenario.from_dict(_mapping(value["request"], "EventBusiness request")),
            response=NiaEventBusinessResponseScenario.from_dict(_mapping(value["response"], "EventBusiness response")),
            effective_direct_costs=EffectiveDirectCosts.from_dict(
                _mapping(value["effective_direct_costs"], "EventBusiness effective_direct_costs")
            ),
            vote_count_before=_plain_int(value["vote_count_before"], "EventBusiness vote_count_before", minimum=0),
            post_state=NiaEventBusinessPostState.from_dict(_mapping(value["post_state"], "EventBusiness post_state")),
            authority=authority_value,
            runtime_ref=_text(value["runtime_ref"], "EventBusiness scenario runtime_ref"),
        )


def _next_step(step_type: object, step_id: object) -> NextStepRef | None:
    raw_type = _text(step_type, "EventBusiness continuation type")
    raw_id = _text(step_id, "EventBusiness continuation id", empty=True)
    if not raw_id:
        return None
    return NextStepRef(raw_type, raw_id)


def _load_effects(
    connection: sqlite3.Connection,
    effect_ids: tuple[str, ...],
    bucket: str,
) -> tuple[NiaEventBusinessStaticEffect, ...]:
    result: list[NiaEventBusinessStaticEffect] = []
    for index, effect_id in enumerate(effect_ids):
        row = connection.execute(
            "SELECT id, effect_type, effect_value_min, effect_value_max, "
            "resource_type FROM produce_effect WHERE id = ?",
            (effect_id,),
        ).fetchone()
        if row is None:
            raise NiaEventBusinessMasterError(
                "missing-master-effect",
                f"{bucket}[{index}]",
                effect_id,
            )
        effect = NiaEventBusinessStaticEffect(
            effect_id=str(row["id"]),
            effect_type=str(row["effect_type"]),
            effect_value_min=int(row["effect_value_min"]),
            effect_value_max=int(row["effect_value_max"]),
            resource_type=str(row["resource_type"]),
        )
        if effect.effect_type not in _MODELED_EFFECT_TYPES:
            raise NiaEventBusinessMasterError(
                "unsupported-master-effect-type",
                f"{bucket}[{index}]",
                f"{effect.effect_id}:{effect.effect_type}",
            )
        result.append(effect)
    return tuple(result)


def load_nia_event_business_master_choice(
    detail_id: str,
    suggestion_id: str,
    *,
    actual_success: bool,
    database: Path = DEFAULT_DATABASE,
) -> NiaEventBusinessMasterChoice:
    """Validate one server-selected detail and caller-selected suggestion.

    This lookup deliberately starts from exact IDs.  It never enumerates or
    samples the server's EventBusiness detail pool.
    """

    detail_id = _text(detail_id, "EventBusiness detail id")
    suggestion_id = _text(suggestion_id, "EventBusiness suggestion id")
    if not isinstance(actual_success, bool):
        raise TypeError("EventBusiness actual_success must be boolean")
    database = Path(database)
    if not database.is_file():
        raise NiaEventBusinessMasterError(
            "master-database-missing",
            "database",
            str(database),
        )
    match = _DETAIL_ID.match(detail_id)
    if match is None:
        raise NiaEventBusinessMasterError(
            "detail-outside-nia-plan3",
            "detail_id",
            detail_id,
        )
    mode_id = match.group("produce").replace("_", "-")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        detail = connection.execute(
            "SELECT id, suggestion_type, produce_story_id, "
            "produce_story_group_id, produce_effect_ids_json, "
            "suggestion_ids_json, event_type, is_business_excellent "
            "FROM step_event_detail WHERE id = ?",
            (detail_id,),
        ).fetchone()
        if detail is None:
            raise NiaEventBusinessMasterError(
                "detail-row-missing",
                "detail_id",
                detail_id,
            )
        if detail["event_type"] != _EVENT_TYPE or detail["produce_story_id"] != "":
            raise NiaEventBusinessMasterError(
                "not-nia-event-business-detail",
                "step_event_detail.event_type/produce_story_id",
                detail_id,
            )
        story_group_id = str(detail["produce_story_group_id"])
        if not story_group_id:
            raise NiaEventBusinessMasterError(
                "story-group-missing",
                "step_event_detail.produce_story_group_id",
                detail_id,
            )
        suggestion_ids = _id_list(
            detail["suggestion_ids_json"],
            "step_event_detail.suggestion_ids",
        )
        indexes = tuple(
            index for index, value in enumerate(suggestion_ids) if value == suggestion_id
        )
        if len(indexes) != 1:
            raise NiaEventBusinessMasterError(
                "suggestion-not-in-detail",
                "suggestion_id",
                f"{suggestion_id}:occurrences={len(indexes)}",
            )
        suggestion_index = indexes[0]
        suggestion = connection.execute(
            "SELECT id, produce_point, stamina, produce_card_id, "
            "produce_card_upgrade_count, produce_effect_ids_json, "
            "step_type, step_id, success_probability_permyriad, "
            "success_produce_effect_ids_json, success_step_type, "
            "success_step_id, fail_produce_effect_ids_json, fail_step_type, "
            "fail_step_id, always_successful, produce_effect_fire_step, "
            "is_campaign FROM step_event_suggestion WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if suggestion is None:
            raise NiaEventBusinessMasterError(
                "suggestion-row-missing",
                "suggestion_id",
                suggestion_id,
            )
        probability = int(suggestion["success_probability_permyriad"])
        if not 0 <= probability <= 10_000:
            raise NiaEventBusinessMasterError(
                "invalid-success-probability",
                "step_event_suggestion.success_probability_permyriad",
                str(probability),
            )
        always_successful = _sqlite_bool(
            suggestion["always_successful"],
            "step_event_suggestion.always_successful",
        )
        impossible = (
            (always_successful and not actual_success)
            or (not always_successful and probability == 10_000 and not actual_success)
            or (not always_successful and probability == 0 and actual_success)
        )
        if impossible:
            raise NiaEventBusinessMasterError(
                "impossible-server-outcome",
                "ProduceStepEventResponse.success",
                (
                    f"always={always_successful}:permyriad={probability}:"
                    f"success={actual_success}"
                ),
            )
        detail_ids = _id_list(
            detail["produce_effect_ids_json"],
            "step_event_detail.produce_effect_ids",
        )
        base_ids = _id_list(
            suggestion["produce_effect_ids_json"],
            "step_event_suggestion.produce_effect_ids",
        )
        if actual_success:
            outcome_ids = _id_list(
                suggestion["success_produce_effect_ids_json"],
                "step_event_suggestion.success_produce_effect_ids",
            )
            outcome_step = _next_step(
                suggestion["success_step_type"],
                suggestion["success_step_id"],
            )
            selected_outcome = "success"
        else:
            outcome_ids = _id_list(
                suggestion["fail_produce_effect_ids_json"],
                "step_event_suggestion.fail_produce_effect_ids",
            )
            outcome_step = _next_step(
                suggestion["fail_step_type"],
                suggestion["fail_step_id"],
            )
            selected_outcome = "fail"
        return NiaEventBusinessMasterChoice(
            mode_id=mode_id,
            detail_id=detail_id,
            produce_story_group_id=story_group_id,
            is_business_excellent=_sqlite_bool(
                detail["is_business_excellent"],
                "step_event_detail.is_business_excellent",
            ),
            suggestion_id=suggestion_id,
            suggestion_index=suggestion_index,
            actual_success=actual_success,
            always_successful=always_successful,
            success_probability_permyriad=probability,
            raw_stamina_cost=int(suggestion["stamina"]),
            raw_produce_point_cost=int(suggestion["produce_point"]),
            direct_produce_card_id=str(suggestion["produce_card_id"]),
            direct_produce_card_upgrade_count=int(
                suggestion["produce_card_upgrade_count"]
            ),
            detail_effects=_load_effects(connection, detail_ids, "detail_effects"),
            base_effects=_load_effects(connection, base_ids, "base_effects"),
            success_or_fail_effects=_load_effects(
                connection,
                outcome_ids,
                "success_or_fail_effects",
            ),
            selected_outcome=selected_outcome,
            direct_next_step=_next_step(
                suggestion["step_type"],
                suggestion["step_id"],
            ),
            success_or_fail_next_step=outcome_step,
            produce_effect_fire_step=int(suggestion["produce_effect_fire_step"]),
            is_campaign=_sqlite_bool(
                suggestion["is_campaign"],
                "step_event_suggestion.is_campaign",
            ),
        )
    finally:
        connection.close()


def _issue(code: str, field: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _scenario_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaEventBusinessScenario,
    master: NiaEventBusinessMasterChoice | None,
    master_error: NiaEventBusinessMasterError | None,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    if state.phase is not RolloutPhase.WAITING_EXTERNAL:
        issues.append(
            _issue(
                "nia-event-business-request-not-pending",
                "outer_state.phase",
                state.phase.value,
            )
        )
    if state.pending != request:
        issues.append(
            _issue(
                "nia-event-business-pending-request-mismatch",
                "outer_state.pending",
            )
        )
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(
            _issue(
                "nia-event-business-request-kind-mismatch",
                "request.kind",
                request.kind.value,
            )
        )
    if request.action_id != EVENT_BUSINESS:
        issues.append(
            _issue(
                "nia-event-business-action-mismatch",
                "request.action_id",
                request.action_id,
            )
        )
    if request.week != state.week or scenario.week != state.week:
        issues.append(
            _issue(
                "nia-event-business-week-mismatch",
                "request/scenario.week",
                f"state={state.week}:request={request.week}:scenario={scenario.week}",
            )
        )
    if request.stage_type is not None:
        issues.append(
            _issue(
                "nia-event-business-stage-type-unexpected",
                "request.stage_type",
                request.stage_type,
            )
        )
    if scenario.branch.probability is None or scenario.branch.probability <= 0.0:
        issues.append(
            _issue(
                "nia-event-business-branch-probability-unresolved",
                "scenario.branch.probability",
            )
        )
    progress = scenario.response.progress_event
    for field, expected, actual in (
        (
            "progress_event.produce_step_event_detail_id",
            scenario.detail_id,
            progress.produce_step_event_detail_id,
        ),
        (
            "progress_event.suggestion_index",
            scenario.request.suggestion_index,
            progress.suggestion_index,
        ),
        ("progress_event.success", scenario.response.success, progress.success),
    ):
        if expected != actual:
            issues.append(
                _issue(
                    "nia-event-business-response-identity-mismatch",
                    field,
                    f"expected={expected!r}:actual={actual!r}",
                )
            )
    if scenario.effective_direct_costs.stamina > state.stamina:
        issues.append(
            _issue(
                "nia-event-business-insufficient-stamina",
                "scenario.effective_direct_costs.stamina",
            )
        )
    if scenario.effective_direct_costs.produce_point > state.produce_points:
        issues.append(
            _issue(
                "nia-event-business-insufficient-produce-points",
                "scenario.effective_direct_costs.produce_point",
            )
        )
    if scenario.post_state.vote_count < scenario.vote_count_before:
        issues.append(
            _issue(
                "nia-event-business-vote-count-decreased",
                "scenario.post_state.vote_count",
                (
                    f"before={scenario.vote_count_before}:"
                    f"after={scenario.post_state.vote_count}"
                ),
            )
        )
    for index, effect in enumerate(scenario.response.effect_results):
        if effect.effect_type not in _MODELED_EFFECT_TYPES:
            issues.append(
                _issue(
                    "nia-event-business-response-effect-unmodeled",
                    f"scenario.response.effect_results[{index}].effect_type",
                    f"{effect.produce_effect_id}:{effect.effect_type}",
                )
            )
    if master_error is not None:
        issues.append(
            _issue(
                f"nia-event-business-master:{master_error.code}",
                master_error.field,
                master_error.detail,
            )
        )
    elif master is not None:
        if master.mode_id != state.mode_id:
            issues.append(
                _issue(
                    "nia-event-business-produce-id-mismatch",
                    "master.mode_id",
                    f"state={state.mode_id}:master={master.mode_id}",
                )
            )
        if master.suggestion_index != scenario.request.suggestion_index:
            issues.append(
                _issue(
                    "nia-event-business-master-choice-mismatch",
                    "scenario.request.suggestion_index",
                    (
                        f"master={master.suggestion_index}:"
                        f"scenario={scenario.request.suggestion_index}"
                    ),
                )
            )
        if master.direct_next_step is not None or master.success_or_fail_next_step is not None:
            issues.append(
                _issue(
                    "nia-event-business-continuation-unhandled",
                    "step_event_suggestion continuation",
                    (
                        f"direct={master.direct_next_step!r}:"
                        f"outcome={master.success_or_fail_next_step!r}"
                    ),
                )
            )
    return tuple(dict.fromkeys(issues))


def _response_effect_identity_issues(
    database: Path,
    effects: tuple[ProduceEffectResultTrace, ...],
) -> tuple[InitialRegularInnerStageIssue, ...]:
    """Validate exact response ID/type pairs without asserting bucket membership."""

    issues: list[InitialRegularInnerStageIssue] = []
    connection = sqlite3.connect(database)
    try:
        for index, effect in enumerate(effects):
            if not effect.produce_effect_id:
                issues.append(
                    _issue(
                        "nia-event-business-response-effect-id-missing",
                        f"scenario.response.effect_results[{index}].produce_effect_id",
                    )
                )
                continue
            row = connection.execute(
                "SELECT effect_type FROM produce_effect WHERE id = ?",
                (effect.produce_effect_id,),
            ).fetchone()
            if row is None:
                issues.append(
                    _issue(
                        "nia-event-business-response-effect-id-unknown",
                        f"scenario.response.effect_results[{index}].produce_effect_id",
                        effect.produce_effect_id,
                    )
                )
            elif str(row[0]) != effect.effect_type:
                issues.append(
                    _issue(
                        "nia-event-business-response-effect-type-mismatch",
                        f"scenario.response.effect_results[{index}].effect_type",
                        (
                            f"id={effect.produce_effect_id}:"
                            f"master={row[0]}:response={effect.effect_type}"
                        ),
                    )
                )
    finally:
        connection.close()
    return tuple(issues)


def adapt_initial_regular_nia_event_business_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaEventBusinessScenario,
    *,
    database: Path = DEFAULT_DATABASE,
) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
    """Project one exact EventBusiness response into the outer kernel.

    ``EffectResults`` stay in response order for audit/replay.  They are not
    replayed here: the complete caller-owned post-state is the only mutation
    source, and the result never opens a second reward request.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularNiaEventBusinessScenario):
        raise TypeError("scenario must be InitialRegularNiaEventBusinessScenario")
    master: NiaEventBusinessMasterChoice | None = None
    master_error: NiaEventBusinessMasterError | None = None
    try:
        master = load_nia_event_business_master_choice(
            scenario.detail_id,
            scenario.suggestion_id,
            actual_success=scenario.response.success,
            database=Path(database),
        )
    except NiaEventBusinessMasterError as error:
        master_error = error
    issues = list(
        _scenario_issues(state, request, scenario, master, master_error)
    )
    if Path(database).is_file():
        issues.extend(
            _response_effect_identity_issues(
                Path(database),
                scenario.response.effect_results,
            )
        )
    if issues:
        return tuple(dict.fromkeys(issues))
    return WeeklyActionOutcome(
        request_id=request.request_id,
        branch=scenario.branch,
        patch=scenario.post_state.to_patch(),
        reward_requested=False,
    )


InitialRegularNiaEventBusinessAdapterResult: TypeAlias = (
    WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "ANDROID_EVENT_BUSINESS_LIFECYCLE",
    "ANDROID_EVENT_BUSINESS_NATIVE_AUTHORITY",
    "ANDROID_EVENT_BUSINESS_PROGRESS_EVENT_FIELDS",
    "ANDROID_EVENT_BUSINESS_REQUEST_FIELDS",
    "ANDROID_EVENT_BUSINESS_REQUEST_TYPE",
    "ANDROID_EVENT_BUSINESS_RESPONSE_FIELDS",
    "ANDROID_EVENT_BUSINESS_RESPONSE_TYPE",
    "ANDROID_EVENT_BUSINESS_STEP_TYPE",
    "ANDROID_EVENT_BUSINESS_STEP_TYPE_VALUE",
    "InitialRegularNiaEventBusinessAdapterResult",
    "InitialRegularNiaEventBusinessScenario",
    "NiaEventBusinessMasterChoice",
    "NiaEventBusinessMasterError",
    "NiaEventBusinessMemoryRewardResult",
    "NiaEventBusinessPostState",
    "NiaEventBusinessProgressEventSnapshot",
    "NiaEventBusinessRequestScenario",
    "NiaEventBusinessResponseScenario",
    "NiaEventBusinessScenarioAuthority",
    "NiaEventBusinessStaticEffect",
    "adapt_initial_regular_nia_event_business_scenario",
    "load_nia_event_business_master_choice",
]
