"""Exact caller-resolved N.I.A. ``Business`` response boundary.

Android v3.2.3 exposes Business as two distinct API transactions::

    ProduceStepBusinessStartRequest  -> ProduceStepBusinessStartResponse
    ProduceStepBusinessSelectRequest -> ProduceStepBusinessSelectResponse

This is not the generic ``ProduceStepEventRequest`` used by EventBusiness.
Both responses own an ordered ``EffectResults`` list; Select additionally
owns an ordered ``ConsumptionResults`` list.  Neither response contains an
explicit success or reward field.  The server-selected normal/excellent
detail identity is therefore an explicit caller fact, while rewards remain
nested in ``ProduceEffectResult.ProvidedRewards``.

PC Master validates only the selected detail/suggestion identities and the
membership/type of response effect IDs.  Its detail/base/success/fail buckets
remain separate and are never flattened into an asserted execution order.
The complete CommonResponse post-state supplied by the caller is the sole
outer-state mutation authority.  Both ``produce-004`` and ``produce-005``
use this exact response boundary; the scenario mode is explicit and must
match both its Master detail IDs and the outer state.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping, TypeAlias

from .initial_regular_event_scenario import (
    ProduceEffectResultTrace,
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


NIA_BUSINESS_ACTION_ID = "business"
ANDROID_BUSINESS_STEP_TYPE = "ProduceStepType_Business"
ANDROID_BUSINESS_STEP_TYPE_VALUE = 25
ANDROID_BUSINESS_START_REQUEST_TYPE = "ProduceStepBusinessStartRequest"
ANDROID_BUSINESS_START_RESPONSE_TYPE = "ProduceStepBusinessStartResponse"
ANDROID_BUSINESS_SELECT_REQUEST_TYPE = "ProduceStepBusinessSelectRequest"
ANDROID_BUSINESS_SELECT_RESPONSE_TYPE = "ProduceStepBusinessSelectResponse"
ANDROID_BUSINESS_START_REQUEST_FIELDS = ("device_produce_uuid",)
ANDROID_BUSINESS_START_RESPONSE_FIELDS = ("effect_results", "common_response")
ANDROID_BUSINESS_SELECT_REQUEST_FIELDS = (
    "business_type",
    "device_produce_uuid",
)
ANDROID_BUSINESS_SELECT_RESPONSE_FIELDS = (
    "consumption_results",
    "effect_results",
    "common_response",
)
ANDROID_BUSINESS_LIFECYCLE = (
    "StepBusinessStart precedes StepBusinessSelect; EffectResults are ordered "
    "inside each response only"
)
ANDROID_BUSINESS_NATIVE_AUTHORITY = (
    "ProduceApiUtility.<ProduceStepBusinessStartRequestAsync>d__40.MoveNext",
    "ProduceApiUtility.<ProduceStepBusinessSelectRequestAsync>d__41.MoveNext",
    "Api.Produce.StepBusinessStartAsync",
    "Api.Produce.StepBusinessSelectAsync",
)

_SUPPORTED_PRODUCE_IDS = frozenset({"produce-004", "produce-005"})
_DETAIL_ID = re.compile(
    r"^event-detail-business-(?P<produce>produce_004|produce_005)-plan3-"
)
_EVENT_TYPE = "ProduceEventType_Business"


def _int(value: object, field: str, *, minimum: int | None = None) -> int:
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


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be boolean")
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
    if actual == fields:
        return
    missing = sorted(fields - actual)
    extra = sorted(actual - fields)
    details: list[str] = []
    if missing:
        details.append(f"missing={','.join(missing)}")
    if extra:
        details.append(f"extra={','.join(extra)}")
    raise ValueError(f"{field} keys mismatch ({';'.join(details)})")


def _refs(value: object, field: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{field}[{index}]", empty=True)
        for index, item in enumerate(_list(value, field))
    )


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


class NiaBusinessType(IntEnum):
    UNKNOWN = 0
    PRODUCE_CARD = 1
    PRODUCE_DRINK = 2
    PRODUCE_POINT = 3
    STAMINA = 4


def _business_type(value: object, field: str) -> NiaBusinessType:
    raw = _int(value, field, minimum=0)
    try:
        result = NiaBusinessType(raw)
    except ValueError as error:
        raise ValueError(f"{field} is not a known ProduceStepBusinessType") from error
    if result is NiaBusinessType.UNKNOWN:
        raise ValueError(f"{field} cannot be Unknown")
    return result


@dataclass(frozen=True, slots=True)
class NiaBusinessProgressSnapshot:
    """All protobuf fields on ``UserProduceProgressBusiness``."""

    business_type: NiaBusinessType
    number: int
    name: str
    destination_number: int
    produce_point: int
    stamina: int
    excellent_permille: int
    normal_detail_id: str
    excellent_detail_id: str

    def __post_init__(self) -> None:
        business_type = self.business_type
        if isinstance(business_type, int) and not isinstance(business_type, NiaBusinessType):
            business_type = _business_type(business_type, "Business progress business_type")
            object.__setattr__(self, "business_type", business_type)
        if not isinstance(business_type, NiaBusinessType) or business_type is NiaBusinessType.UNKNOWN:
            raise ValueError("Business progress business_type is invalid")
        for field in (
            "number",
            "destination_number",
            "produce_point",
            "stamina",
            "excellent_permille",
        ):
            _int(getattr(self, field), f"Business progress {field}", minimum=0)
        _text(self.name, "Business progress name", empty=True)
        _text(self.normal_detail_id, "Business progress normal detail id")
        _text(self.excellent_detail_id, "Business progress excellent detail id")
        if self.normal_detail_id == self.excellent_detail_id:
            raise ValueError("Business normal/excellent detail IDs must differ")

    def to_dict(self) -> dict[str, object]:
        return {
            "business_type": int(self.business_type),
            "number": self.number,
            "name": self.name,
            "destination_number": self.destination_number,
            "produce_point": self.produce_point,
            "stamina": self.stamina,
            "excellent_permille": self.excellent_permille,
            "normal_detail_id": self.normal_detail_id,
            "excellent_detail_id": self.excellent_detail_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaBusinessProgressSnapshot":
        value = _mapping(payload, "Business progress")
        fields = {
            "business_type",
            "number",
            "name",
            "destination_number",
            "produce_point",
            "stamina",
            "excellent_permille",
            "normal_detail_id",
            "excellent_detail_id",
        }
        _exact(value, fields, "Business progress")
        return cls(
            business_type=_business_type(value["business_type"], "Business progress business_type"),
            number=_int(value["number"], "Business progress number", minimum=0),
            name=_text(value["name"], "Business progress name", empty=True),
            destination_number=_int(value["destination_number"], "Business progress destination_number", minimum=0),
            produce_point=_int(value["produce_point"], "Business progress produce_point", minimum=0),
            stamina=_int(value["stamina"], "Business progress stamina", minimum=0),
            excellent_permille=_int(value["excellent_permille"], "Business progress excellent_permille", minimum=0),
            normal_detail_id=_text(value["normal_detail_id"], "Business progress normal detail id"),
            excellent_detail_id=_text(value["excellent_detail_id"], "Business progress excellent detail id"),
        )


@dataclass(frozen=True, slots=True)
class NiaBusinessStartRequestScenario:
    device_produce_uuid_ref: str

    def __post_init__(self) -> None:
        _text(self.device_produce_uuid_ref, "Business Start request UUID ref")

    def to_dict(self) -> dict[str, object]:
        return {"device_produce_uuid_ref": self.device_produce_uuid_ref}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaBusinessStartRequestScenario":
        value = _mapping(payload, "Business Start request")
        _exact(value, {"device_produce_uuid_ref"}, "Business Start request")
        return cls(_text(value["device_produce_uuid_ref"], "Business Start request UUID ref"))


@dataclass(frozen=True, slots=True)
class NiaBusinessSelectRequestScenario:
    business_type: NiaBusinessType
    device_produce_uuid_ref: str

    def __post_init__(self) -> None:
        business_type = self.business_type
        if isinstance(business_type, int) and not isinstance(business_type, NiaBusinessType):
            business_type = _business_type(business_type, "Business Select request business_type")
            object.__setattr__(self, "business_type", business_type)
        if not isinstance(business_type, NiaBusinessType) or business_type is NiaBusinessType.UNKNOWN:
            raise ValueError("Business Select request business_type is invalid")
        _text(self.device_produce_uuid_ref, "Business Select request UUID ref")

    def to_dict(self) -> dict[str, object]:
        return {
            "business_type": int(self.business_type),
            "device_produce_uuid_ref": self.device_produce_uuid_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaBusinessSelectRequestScenario":
        value = _mapping(payload, "Business Select request")
        _exact(value, {"business_type", "device_produce_uuid_ref"}, "Business Select request")
        return cls(
            _business_type(value["business_type"], "Business Select request business_type"),
            _text(value["device_produce_uuid_ref"], "Business Select request UUID ref"),
        )


@dataclass(frozen=True, slots=True)
class NiaBusinessConsumptionResult:
    """One response-ordered ``ProduceConsumptionResult``."""

    resource_type: str
    resource_id: str
    quantity: int
    before_quantity: int
    after_quantity: int

    def __post_init__(self) -> None:
        _text(self.resource_type, "Business consumption resource_type")
        _text(self.resource_id, "Business consumption resource_id", empty=True)
        for field in ("quantity", "before_quantity", "after_quantity"):
            _int(getattr(self, field), f"Business consumption {field}", minimum=0)

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "quantity": self.quantity,
            "before_quantity": self.before_quantity,
            "after_quantity": self.after_quantity,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaBusinessConsumptionResult":
        value = _mapping(payload, "Business consumption result")
        fields = {"resource_type", "resource_id", "quantity", "before_quantity", "after_quantity"}
        _exact(value, fields, "Business consumption result")
        return cls(
            resource_type=_text(value["resource_type"], "Business consumption resource_type"),
            resource_id=_text(value["resource_id"], "Business consumption resource_id", empty=True),
            quantity=_int(value["quantity"], "Business consumption quantity", minimum=0),
            before_quantity=_int(value["before_quantity"], "Business consumption before_quantity", minimum=0),
            after_quantity=_int(value["after_quantity"], "Business consumption after_quantity", minimum=0),
        )


@dataclass(frozen=True, slots=True)
class NiaBusinessPostState:
    """Complete simulator-owned state represented by one CommonResponse."""

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
        _int(self.stamina, "Business post stamina", minimum=0)
        _int(self.max_stamina, "Business post max_stamina", minimum=1)
        _int(self.produce_points, "Business post produce_points", minimum=0)
        _int(self.vote_count, "Business post vote_count", minimum=0)
        if self.stamina > self.max_stamina:
            raise ValueError("Business post stamina exceeds max_stamina")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("Business post attributes must be typed")
        deck = tuple(self.deck)
        if not all(isinstance(value, DeckEntry) for value in deck):
            raise TypeError("Business post deck must contain DeckEntry values")
        object.__setattr__(self, "deck", deck)
        for field in (
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        ):
            values = tuple(getattr(self, field))
            if any(not isinstance(value, str) for value in values):
                raise TypeError(f"Business post {field} must contain text")
            object.__setattr__(self, field, values)
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
    ) -> "NiaBusinessPostState":
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        return cls(
            stamina=state.stamina if stamina is None else stamina,
            max_stamina=state.max_stamina if max_stamina is None else max_stamina,
            produce_points=state.produce_points if produce_points is None else produce_points,
            vote_count=vote_count,
            attributes=state.attributes if attributes is None else attributes,
            deck=state.deck if deck is None else deck,
            item_session_refs=state.item_session_refs,
            drink_session_refs=state.drink_session_refs,
            passive_session_refs=state.passive_session_refs,
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
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaBusinessPostState":
        value = _mapping(payload, "Business post state")
        fields = {
            "stamina", "max_stamina", "produce_points", "vote_count",
            "attributes", "deck", "item_session_refs", "drink_session_refs",
            "passive_session_refs", "excluded_reward_card_ids",
        }
        _exact(value, fields, "Business post state")
        return cls(
            stamina=_int(value["stamina"], "Business post stamina", minimum=0),
            max_stamina=_int(value["max_stamina"], "Business post max_stamina", minimum=1),
            produce_points=_int(value["produce_points"], "Business post produce_points", minimum=0),
            vote_count=_int(value["vote_count"], "Business post vote_count", minimum=0),
            attributes=_attributes(value["attributes"], "Business post attributes"),
            deck=tuple(
                _deck_entry(item, "Business post deck entry")
                for item in _list(value["deck"], "Business post deck")
            ),
            item_session_refs=_refs(value["item_session_refs"], "Business post item_session_refs"),
            drink_session_refs=_refs(value["drink_session_refs"], "Business post drink_session_refs"),
            passive_session_refs=_refs(value["passive_session_refs"], "Business post passive_session_refs"),
            excluded_reward_card_ids=_refs(value["excluded_reward_card_ids"], "Business post excluded_reward_card_ids"),
        )


@dataclass(frozen=True, slots=True)
class NiaBusinessStartResponseScenario:
    effect_results: tuple[ProduceEffectResultTrace, ...]
    common_post_state: NiaBusinessPostState

    def __post_init__(self) -> None:
        effects = tuple(self.effect_results)
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("Business Start effect_results must be typed")
        validate_effect_result_chain(effects)
        object.__setattr__(self, "effect_results", effects)
        if not isinstance(self.common_post_state, NiaBusinessPostState):
            raise TypeError("Business Start common_post_state must be typed")

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_results": [value.to_dict() for value in self.effect_results],
            "common_post_state": self.common_post_state.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaBusinessStartResponseScenario":
        value = _mapping(payload, "Business Start response")
        _exact(value, {"effect_results", "common_post_state"}, "Business Start response")
        return cls(
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "Business Start effect result"))
                for item in _list(value["effect_results"], "Business Start effect_results")
            ),
            common_post_state=NiaBusinessPostState.from_dict(
                _mapping(value["common_post_state"], "Business Start common_post_state")
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaBusinessSelectResponseScenario:
    consumption_results: tuple[NiaBusinessConsumptionResult, ...]
    effect_results: tuple[ProduceEffectResultTrace, ...]
    common_post_state: NiaBusinessPostState

    def __post_init__(self) -> None:
        consumptions = tuple(self.consumption_results)
        if not all(isinstance(value, NiaBusinessConsumptionResult) for value in consumptions):
            raise TypeError("Business Select consumption_results must be typed")
        object.__setattr__(self, "consumption_results", consumptions)
        effects = tuple(self.effect_results)
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("Business Select effect_results must be typed")
        validate_effect_result_chain(effects)
        object.__setattr__(self, "effect_results", effects)
        if not isinstance(self.common_post_state, NiaBusinessPostState):
            raise TypeError("Business Select common_post_state must be typed")

    def to_dict(self) -> dict[str, object]:
        return {
            "consumption_results": [value.to_dict() for value in self.consumption_results],
            "effect_results": [value.to_dict() for value in self.effect_results],
            "common_post_state": self.common_post_state.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaBusinessSelectResponseScenario":
        value = _mapping(payload, "Business Select response")
        _exact(
            value,
            {"consumption_results", "effect_results", "common_post_state"},
            "Business Select response",
        )
        return cls(
            consumption_results=tuple(
                NiaBusinessConsumptionResult.from_dict(_mapping(item, "Business consumption result"))
                for item in _list(value["consumption_results"], "Business consumption_results")
            ),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "Business Select effect result"))
                for item in _list(value["effect_results"], "Business Select effect_results")
            ),
            common_post_state=NiaBusinessPostState.from_dict(
                _mapping(value["common_post_state"], "Business Select common_post_state")
            ),
        )


class NiaBusinessScenarioAuthority(StrEnum):
    CALLER_RESOLVED_SERVER_RESPONSE = "caller-resolved-server-response"
    CALLER_AUTHORED_SYNTHETIC_EXACT_RESPONSE = "caller-authored-synthetic-exact-response"


@dataclass(frozen=True, slots=True)
class InitialRegularNiaBusinessScenario:
    """One exact two-phase Business transaction."""

    week: int
    branch: ChanceBranch
    progress: NiaBusinessProgressSnapshot
    suggestion_id: str
    was_excellent: bool
    start_request: NiaBusinessStartRequestScenario
    start_response: NiaBusinessStartResponseScenario
    select_request: NiaBusinessSelectRequestScenario
    select_response: NiaBusinessSelectResponseScenario
    vote_count_before: int
    authority: NiaBusinessScenarioAuthority
    runtime_ref: str
    # Version-1 bundles predate this field and therefore deserialize to the
    # original Pro mode.  Master scenarios must encode produce-005 explicitly.
    produce_id: str = "produce-004"

    def __post_init__(self) -> None:
        _int(self.week, "Business scenario week", minimum=1)
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("Business scenario branch must be typed")
        if not isinstance(self.progress, NiaBusinessProgressSnapshot):
            raise TypeError("Business scenario progress must be typed")
        _text(self.suggestion_id, "Business scenario suggestion_id")
        _bool(self.was_excellent, "Business scenario was_excellent")
        if not isinstance(self.start_request, NiaBusinessStartRequestScenario):
            raise TypeError("Business scenario start_request must be typed")
        if not isinstance(self.start_response, NiaBusinessStartResponseScenario):
            raise TypeError("Business scenario start_response must be typed")
        if not isinstance(self.select_request, NiaBusinessSelectRequestScenario):
            raise TypeError("Business scenario select_request must be typed")
        if not isinstance(self.select_response, NiaBusinessSelectResponseScenario):
            raise TypeError("Business scenario select_response must be typed")
        if self.start_request.device_produce_uuid_ref != self.select_request.device_produce_uuid_ref:
            raise ValueError("Business Start/Select UUID refs disagree")
        if self.select_request.business_type is not self.progress.business_type:
            raise ValueError("Business Select type disagrees with progress")
        _int(self.vote_count_before, "Business scenario vote_count_before", minimum=0)
        authority = self.authority
        if isinstance(authority, str) and not isinstance(authority, NiaBusinessScenarioAuthority):
            try:
                authority = NiaBusinessScenarioAuthority(authority)
            except ValueError as error:
                raise ValueError("Business scenario authority is invalid") from error
            object.__setattr__(self, "authority", authority)
        if not isinstance(authority, NiaBusinessScenarioAuthority):
            raise TypeError("Business scenario authority must be typed")
        _text(self.runtime_ref, "Business scenario runtime_ref")
        if self.produce_id not in _SUPPORTED_PRODUCE_IDS:
            raise ValueError("Business scenario produce_id is unsupported")

    @property
    def selected_detail_id(self) -> str:
        return (
            self.progress.excellent_detail_id
            if self.was_excellent
            else self.progress.normal_detail_id
        )

    @property
    def post_state(self) -> NiaBusinessPostState:
        return self.select_response.common_post_state

    @property
    def authentic_nia_local_save(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "branch": self.branch.to_dict(),
            "progress": self.progress.to_dict(),
            "suggestion_id": self.suggestion_id,
            "was_excellent": self.was_excellent,
            "start_request": self.start_request.to_dict(),
            "start_response": self.start_response.to_dict(),
            "select_request": self.select_request.to_dict(),
            "select_response": self.select_response.to_dict(),
            "vote_count_before": self.vote_count_before,
            "authority": self.authority.value,
            "runtime_ref": self.runtime_ref,
            "produce_id": self.produce_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularNiaBusinessScenario":
        value = _mapping(payload, "Business scenario")
        fields = {
            "week", "branch", "progress", "suggestion_id", "was_excellent",
            "start_request", "start_response", "select_request", "select_response",
            "vote_count_before", "authority", "runtime_ref",
        }
        # Missing produce_id is the strict legacy v1 shape and means Pro.
        # Any other missing/extra key remains rejected.
        if "produce_id" in value:
            _exact(value, fields | {"produce_id"}, "Business scenario")
            produce_id = _text(value["produce_id"], "Business scenario produce_id")
        else:
            _exact(value, fields, "Business scenario")
            produce_id = "produce-004"
        authority = _text(value["authority"], "Business scenario authority")
        try:
            authority_value = NiaBusinessScenarioAuthority(authority)
        except ValueError as error:
            raise ValueError("Business scenario authority is invalid") from error
        return cls(
            week=_int(value["week"], "Business scenario week", minimum=1),
            branch=_branch(value["branch"], "Business scenario branch"),
            progress=NiaBusinessProgressSnapshot.from_dict(_mapping(value["progress"], "Business progress")),
            suggestion_id=_text(value["suggestion_id"], "Business scenario suggestion_id"),
            was_excellent=_bool(value["was_excellent"], "Business scenario was_excellent"),
            start_request=NiaBusinessStartRequestScenario.from_dict(_mapping(value["start_request"], "Business Start request")),
            start_response=NiaBusinessStartResponseScenario.from_dict(_mapping(value["start_response"], "Business Start response")),
            select_request=NiaBusinessSelectRequestScenario.from_dict(_mapping(value["select_request"], "Business Select request")),
            select_response=NiaBusinessSelectResponseScenario.from_dict(_mapping(value["select_response"], "Business Select response")),
            vote_count_before=_int(value["vote_count_before"], "Business scenario vote_count_before", minimum=0),
            authority=authority_value,
            runtime_ref=_text(value["runtime_ref"], "Business scenario runtime_ref"),
            produce_id=produce_id,
        )


class NiaBusinessMasterError(ValueError):
    def __init__(self, code: str, field: str, detail: str) -> None:
        self.code = _text(code, "Business Master error code")
        self.field = _text(field, "Business Master error field")
        self.detail = _text(detail, "Business Master error detail", empty=True)
        super().__init__(f"{self.code}:{self.field}:{self.detail}")


@dataclass(frozen=True, slots=True)
class NiaBusinessStaticEffect:
    effect_id: str
    effect_type: str


@dataclass(frozen=True, slots=True)
class NiaBusinessMasterSelection:
    mode_id: str
    normal_detail_id: str
    excellent_detail_id: str
    selected_detail_id: str
    suggestion_id: str
    was_excellent: bool
    detail_effects: tuple[NiaBusinessStaticEffect, ...]
    suggestion_base_effects: tuple[NiaBusinessStaticEffect, ...]
    suggestion_success_effects: tuple[NiaBusinessStaticEffect, ...]
    suggestion_fail_effects: tuple[NiaBusinessStaticEffect, ...]

    @property
    def effect_membership(self) -> Mapping[str, str]:
        result: dict[str, str] = {}
        for bucket in (
            self.detail_effects,
            self.suggestion_base_effects,
            self.suggestion_success_effects,
            self.suggestion_fail_effects,
        ):
            for effect in bucket:
                previous = result.setdefault(effect.effect_id, effect.effect_type)
                if previous != effect.effect_type:
                    raise ValueError("ProduceEffect ID has inconsistent Master types")
        return result


def _master_ids(raw: object, field: str) -> tuple[str, ...]:
    try:
        value = json.loads(_text(raw, field))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise NiaBusinessMasterError("invalid-master-id-list", field, str(error)) from error
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise NiaBusinessMasterError("invalid-master-id-list", field, repr(value))
    return tuple(value)


def _master_effects(
    connection: sqlite3.Connection,
    ids: tuple[str, ...],
    field: str,
) -> tuple[NiaBusinessStaticEffect, ...]:
    result: list[NiaBusinessStaticEffect] = []
    for index, effect_id in enumerate(ids):
        row = connection.execute(
            "SELECT id, effect_type FROM produce_effect WHERE id = ?",
            (effect_id,),
        ).fetchone()
        if row is None:
            raise NiaBusinessMasterError("missing-master-effect", f"{field}[{index}]", effect_id)
        result.append(NiaBusinessStaticEffect(str(row["id"]), str(row["effect_type"])))
    return tuple(result)


def load_nia_business_master_selection(
    progress: NiaBusinessProgressSnapshot,
    suggestion_id: str,
    *,
    was_excellent: bool,
    produce_id: str = "produce-004",
    database: Path = DEFAULT_DATABASE,
) -> NiaBusinessMasterSelection:
    """Load one exact N.I.A. detail pair/suggestion without pool inference."""

    if not isinstance(progress, NiaBusinessProgressSnapshot):
        raise TypeError("progress must be NiaBusinessProgressSnapshot")
    suggestion_id = _text(suggestion_id, "Business suggestion_id")
    _bool(was_excellent, "Business was_excellent")
    produce_id = _text(produce_id, "Business produce_id")
    if produce_id not in _SUPPORTED_PRODUCE_IDS:
        raise NiaBusinessMasterError(
            "unsupported-produce-id", "produce_id", produce_id
        )
    database = Path(database)
    if not database.is_file():
        raise NiaBusinessMasterError("master-database-missing", "database", str(database))
    for field, detail_id in (
        ("normal_detail_id", progress.normal_detail_id),
        ("excellent_detail_id", progress.excellent_detail_id),
    ):
        match = _DETAIL_ID.match(detail_id)
        if match is None:
            raise NiaBusinessMasterError(
                "detail-outside-nia-plan3-business", field, detail_id
            )
        detail_produce_id = match.group("produce").replace("_", "-")
        if detail_produce_id != produce_id:
            raise NiaBusinessMasterError(
                "detail-produce-id-mismatch",
                field,
                f"scenario={produce_id}:detail={detail_produce_id}:{detail_id}",
            )

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        details: dict[bool, sqlite3.Row] = {}
        for expected_excellent, detail_id in (
            (False, progress.normal_detail_id),
            (True, progress.excellent_detail_id),
        ):
            row = connection.execute(
                "SELECT id, event_type, produce_story_id, is_business_excellent, "
                "suggestion_ids_json, produce_effect_ids_json "
                "FROM step_event_detail WHERE id = ?",
                (detail_id,),
            ).fetchone()
            if row is None:
                raise NiaBusinessMasterError("detail-row-missing", "detail_id", detail_id)
            if str(row["event_type"]) != _EVENT_TYPE or str(row["produce_story_id"]):
                raise NiaBusinessMasterError("not-business-detail", "step_event_detail", detail_id)
            raw_excellent = row["is_business_excellent"]
            if raw_excellent not in (0, 1) or bool(raw_excellent) != expected_excellent:
                raise NiaBusinessMasterError(
                    "business-excellent-identity-mismatch",
                    "step_event_detail.is_business_excellent",
                    detail_id,
                )
            suggestion_ids = _master_ids(row["suggestion_ids_json"], f"{detail_id}.suggestion_ids")
            if suggestion_ids.count(suggestion_id) != 1:
                raise NiaBusinessMasterError(
                    "suggestion-not-in-detail",
                    "suggestion_id",
                    f"{detail_id}:{suggestion_id}",
                )
            details[expected_excellent] = row
        suggestion = connection.execute(
            "SELECT id, produce_effect_ids_json, success_produce_effect_ids_json, "
            "fail_produce_effect_ids_json FROM step_event_suggestion WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
        if suggestion is None:
            raise NiaBusinessMasterError("suggestion-row-missing", "suggestion_id", suggestion_id)
        selected = details[was_excellent]
        return NiaBusinessMasterSelection(
            mode_id=produce_id,
            normal_detail_id=progress.normal_detail_id,
            excellent_detail_id=progress.excellent_detail_id,
            selected_detail_id=str(selected["id"]),
            suggestion_id=suggestion_id,
            was_excellent=was_excellent,
            detail_effects=_master_effects(
                connection,
                _master_ids(selected["produce_effect_ids_json"], "selected_detail.effects"),
                "detail_effects",
            ),
            suggestion_base_effects=_master_effects(
                connection,
                _master_ids(suggestion["produce_effect_ids_json"], "suggestion.base_effects"),
                "suggestion_base_effects",
            ),
            suggestion_success_effects=_master_effects(
                connection,
                _master_ids(suggestion["success_produce_effect_ids_json"], "suggestion.success_effects"),
                "suggestion_success_effects",
            ),
            suggestion_fail_effects=_master_effects(
                connection,
                _master_ids(suggestion["fail_produce_effect_ids_json"], "suggestion.fail_effects"),
                "suggestion_fail_effects",
            ),
        )
    finally:
        connection.close()


def _issue(code: str, field: str, detail: str = "") -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _scenario_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaBusinessScenario,
    master: NiaBusinessMasterSelection | None,
    master_error: NiaBusinessMasterError | None,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    if state.phase is not RolloutPhase.WAITING_EXTERNAL:
        issues.append(_issue("nia-business-request-not-pending", "outer_state.phase", state.phase.value))
    if state.pending != request:
        issues.append(_issue("nia-business-pending-request-mismatch", "outer_state.pending"))
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(_issue("nia-business-request-kind-mismatch", "request.kind", request.kind.value))
    if request.action_id != NIA_BUSINESS_ACTION_ID:
        issues.append(_issue("nia-business-action-mismatch", "request.action_id", request.action_id))
    if request.week != state.week or scenario.week != state.week:
        issues.append(
            _issue(
                "nia-business-week-mismatch",
                "request/scenario.week",
                f"state={state.week}:request={request.week}:scenario={scenario.week}",
            )
        )
    if request.stage_type is not None:
        issues.append(_issue("nia-business-stage-type-unexpected", "request.stage_type", request.stage_type))
    if state.mode_id != scenario.produce_id:
        issues.append(
            _issue(
                "nia-business-produce-id-mismatch",
                "outer_state.mode_id/scenario.produce_id",
                f"state={state.mode_id}:scenario={scenario.produce_id}",
            )
        )
    if scenario.branch.probability is None or scenario.branch.probability <= 0.0:
        issues.append(_issue("nia-business-branch-probability-unresolved", "scenario.branch.probability"))
    if scenario.post_state.vote_count < scenario.vote_count_before:
        issues.append(
            _issue(
                "nia-business-vote-count-decreased",
                "scenario.select_response.common_post_state.vote_count",
                f"before={scenario.vote_count_before}:after={scenario.post_state.vote_count}",
            )
        )
    if master_error is not None:
        issues.append(_issue(f"nia-business-master:{master_error.code}", master_error.field, master_error.detail))
    elif master is not None:
        membership = master.effect_membership
        response_groups = (
            ("start_response.effect_results", scenario.start_response.effect_results),
            ("select_response.effect_results", scenario.select_response.effect_results),
        )
        for group, effects in response_groups:
            for index, effect in enumerate(effects):
                field = f"scenario.{group}[{index}]"
                expected_type = membership.get(effect.produce_effect_id)
                if expected_type is None:
                    issues.append(
                        _issue(
                            "nia-business-response-effect-not-in-master-selection",
                            f"{field}.produce_effect_id",
                            effect.produce_effect_id,
                        )
                    )
                elif expected_type != effect.effect_type:
                    issues.append(
                        _issue(
                            "nia-business-response-effect-type-mismatch",
                            f"{field}.effect_type",
                            f"id={effect.produce_effect_id}:master={expected_type}:response={effect.effect_type}",
                        )
                    )
    return tuple(dict.fromkeys(issues))


def adapt_initial_regular_nia_business_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaBusinessScenario,
    *,
    database: Path = DEFAULT_DATABASE,
) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
    """Project exact Business responses without replaying Master effects."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularNiaBusinessScenario):
        raise TypeError("scenario must be InitialRegularNiaBusinessScenario")
    master: NiaBusinessMasterSelection | None = None
    master_error: NiaBusinessMasterError | None = None
    try:
        master = load_nia_business_master_selection(
            scenario.progress,
            scenario.suggestion_id,
            was_excellent=scenario.was_excellent,
            produce_id=scenario.produce_id,
            database=Path(database),
        )
    except NiaBusinessMasterError as error:
        master_error = error
    issues = _scenario_issues(state, request, scenario, master, master_error)
    if issues:
        return issues
    return WeeklyActionOutcome(
        request_id=request.request_id,
        branch=scenario.branch,
        patch=scenario.post_state.to_patch(),
        reward_requested=False,
    )


InitialRegularNiaBusinessAdapterResult: TypeAlias = (
    WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "ANDROID_BUSINESS_LIFECYCLE",
    "ANDROID_BUSINESS_NATIVE_AUTHORITY",
    "ANDROID_BUSINESS_SELECT_REQUEST_FIELDS",
    "ANDROID_BUSINESS_SELECT_REQUEST_TYPE",
    "ANDROID_BUSINESS_SELECT_RESPONSE_FIELDS",
    "ANDROID_BUSINESS_SELECT_RESPONSE_TYPE",
    "ANDROID_BUSINESS_START_REQUEST_FIELDS",
    "ANDROID_BUSINESS_START_REQUEST_TYPE",
    "ANDROID_BUSINESS_START_RESPONSE_FIELDS",
    "ANDROID_BUSINESS_START_RESPONSE_TYPE",
    "ANDROID_BUSINESS_STEP_TYPE",
    "ANDROID_BUSINESS_STEP_TYPE_VALUE",
    "InitialRegularNiaBusinessAdapterResult",
    "InitialRegularNiaBusinessScenario",
    "NIA_BUSINESS_ACTION_ID",
    "NiaBusinessConsumptionResult",
    "NiaBusinessMasterError",
    "NiaBusinessMasterSelection",
    "NiaBusinessPostState",
    "NiaBusinessProgressSnapshot",
    "NiaBusinessScenarioAuthority",
    "NiaBusinessSelectRequestScenario",
    "NiaBusinessSelectResponseScenario",
    "NiaBusinessStartRequestScenario",
    "NiaBusinessStartResponseScenario",
    "NiaBusinessStaticEffect",
    "NiaBusinessType",
    "adapt_initial_regular_nia_business_scenario",
    "load_nia_business_master_selection",
]
