"""Typed N.I.A. ``Interval`` response boundary for offline outer rollouts.

Android v3.2.3 keeps ``ProduceStepType_Interval`` separate from
``ProduceStepType_Refresh``.  Interval is a small server-owned shop lifecycle:
Start and End return ordered ``EffectResults``; Buy returns the selected
``ProvidedRewards``, before/after produce points and stamina, and ordered
``EffectResults``; Reroll has no scalar response fields.  The product rows
themselves live in ``UserProduceProgressInterval`` and therefore remain
caller-resolved scenario input.

``ProduceSchedule.RefreshStamina`` is not an Interval rule.  Native
``ScheduleListStepCellView.OnSetView`` only renders that value when the
decided step type is ``Refresh`` (enum value 14), while Interval is enum value
47.  The dedicated Refresh endpoint returns authoritative before/after
stamina and ordered effects.  This module records that boundary explicitly
and never applies the schedule field to an Interval state.

The bounded adapter below replays no product or effect rule.  It validates
the response topology and projects one complete caller-authoritative outer
post-state.  This makes captured/synthetic test scenarios executable without
inventing server offers, reroll membership, CommonResponse user-data patches,
or cross-response effect order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping, TypeAlias

from .initial_regular_event_scenario import (
    ProduceEffectResultTrace,
    ProduceRewardResultSnapshot,
    validate_effect_result_chain,
)
from .initial_regular_inner_protocol import InitialRegularInnerStageIssue
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


NIA_INTERVAL_ACTION_ID: Final = "interval"
NIA_REFRESH_ACTION_ID: Final = "refresh"

# Generated Android v3.2.3 protobuf surfaces.  CommonResponse is omitted from
# these tuples because it is the envelope which carries the updated user data,
# represented below by products_after/post_state rather than guessed locally.
ANDROID_INTERVAL_START_REQUEST_FIELDS: Final = ("device_produce_uuid",)
ANDROID_INTERVAL_START_RESPONSE_FIELDS: Final = ("effect_results",)
ANDROID_INTERVAL_BUY_REQUEST_FIELDS: Final = (
    "device_produce_uuid",
    "position_number",
    "number",
    "produce_card_customize_id",
)
ANDROID_INTERVAL_BUY_RESPONSE_FIELDS: Final = (
    "provided_rewards",
    "before_produce_point",
    "after_produce_point",
    "before_stamina",
    "after_stamina",
    "effect_results",
)
ANDROID_INTERVAL_REROLL_REQUEST_FIELDS: Final = ("device_produce_uuid",)
ANDROID_INTERVAL_REROLL_RESPONSE_FIELDS: Final = ()
ANDROID_INTERVAL_END_REQUEST_FIELDS: Final = ("device_produce_uuid",)
ANDROID_INTERVAL_END_RESPONSE_FIELDS: Final = ("effect_results",)
ANDROID_INTERVAL_PRODUCT_FIELDS: Final = (
    "position_number",
    "purchased",
    "resource_type",
    "resource_id",
    "upgrade_count",
    "stamina_recover_value",
    "price",
    "next_price",
)

# The similarly named schedule value belongs to the distinct Refresh step.
ANDROID_SCHEDULE_REFRESH_STAMINA_DISPLAY_STEP_TYPE: Final = (
    "ProduceStepType_Refresh"
)
ANDROID_SCHEDULE_REFRESH_STAMINA_DISPLAY_STEP_TYPE_VALUE: Final = 14
ANDROID_INTERVAL_STEP_TYPE_VALUE: Final = 47
ANDROID_REFRESH_REQUEST_FIELDS: Final = ("device_produce_uuid",)
ANDROID_REFRESH_RESPONSE_FIELDS: Final = (
    "before_stamina",
    "after_stamina",
    "effect_results",
)
ANDROID_REFRESH_RECOVERY_AUTHORITY: Final = (
    "ProduceStepRefreshResponse.BeforeStamina/AfterStamina; "
    "ProduceSchedule.RefreshStamina is a displayed recovery delta and is not "
    "a client-side Interval mutation"
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


def _typed_effects(
    values: tuple[ProduceEffectResultTrace, ...],
    field: str,
) -> tuple[ProduceEffectResultTrace, ...]:
    result = tuple(values)
    if not all(isinstance(value, ProduceEffectResultTrace) for value in result):
        raise TypeError(f"{field} must contain ProduceEffectResultTrace values")
    validate_effect_result_chain(result)
    return result


def _typed_products(
    values: tuple["NiaIntervalProductSnapshot", ...],
    field: str,
) -> tuple["NiaIntervalProductSnapshot", ...]:
    result = tuple(values)
    if not all(isinstance(value, NiaIntervalProductSnapshot) for value in result):
        raise TypeError(f"{field} must contain NiaIntervalProductSnapshot values")
    positions = tuple(value.position_number for value in result)
    if len(set(positions)) != len(positions):
        raise ValueError(f"{field} position_number values must be unique")
    return result


class NiaIntervalScenarioAuthority(StrEnum):
    """Authority of the runtime-only Interval response and product rows."""

    CALLER_RESOLVED_SERVER_RESPONSE = "caller-resolved-server-response"
    CALLER_AUTHORED_SYNTHETIC_EXACT_RESPONSE = (
        "caller-authored-synthetic-exact-response"
    )


@dataclass(frozen=True, slots=True)
class NiaIntervalProductSnapshot:
    """One server-provided ``UserProduceProgressInterval`` row."""

    position_number: int
    purchased: bool
    resource_type: str
    resource_id: str
    upgrade_count: int
    stamina_recover_value: int
    price: int
    next_price: int

    def __post_init__(self) -> None:
        _plain_int(self.position_number, "Interval product position", minimum=1)
        if not isinstance(self.purchased, bool):
            raise TypeError("Interval product purchased must be boolean")
        if not isinstance(self.resource_type, str) or not self.resource_type:
            raise ValueError("Interval product resource_type is required")
        if not isinstance(self.resource_id, str):
            raise TypeError("Interval product resource_id must be text")
        for field in (
            "upgrade_count",
            "stamina_recover_value",
            "price",
            "next_price",
        ):
            _plain_int(getattr(self, field), f"Interval product {field}")

    def to_dict(self) -> dict[str, object]:
        return {
            "position_number": self.position_number,
            "purchased": self.purchased,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "upgrade_count": self.upgrade_count,
            "stamina_recover_value": self.stamina_recover_value,
            "price": self.price,
            "next_price": self.next_price,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaIntervalProductSnapshot":
        value = _mapping(payload, "Interval product")
        _exact(
            value,
            {
                "position_number",
                "purchased",
                "resource_type",
                "resource_id",
                "upgrade_count",
                "stamina_recover_value",
                "price",
                "next_price",
            },
            "Interval product",
        )
        return cls(
            position_number=_plain_int(value["position_number"], "Interval product position", minimum=1),
            purchased=_bool(value["purchased"], "Interval product purchased"),
            resource_type=_text(value["resource_type"], "Interval product resource_type"),
            resource_id=_text(value["resource_id"], "Interval product resource_id", empty=True),
            upgrade_count=_plain_int(value["upgrade_count"], "Interval product upgrade_count"),
            stamina_recover_value=_plain_int(value["stamina_recover_value"], "Interval product stamina_recover_value"),
            price=_plain_int(value["price"], "Interval product price"),
            next_price=_plain_int(value["next_price"], "Interval product next_price"),
        )


@dataclass(frozen=True, slots=True)
class NiaIntervalStartScenario:
    """Exact Start response plus products from its CommonResponse user data."""

    effect_results: tuple[ProduceEffectResultTrace, ...]
    products_after: tuple[NiaIntervalProductSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effect_results",
            _typed_effects(self.effect_results, "Interval Start effect_results"),
        )
        object.__setattr__(
            self,
            "products_after",
            _typed_products(self.products_after, "Interval Start products_after"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_results": [value.to_dict() for value in self.effect_results],
            "products_after": [value.to_dict() for value in self.products_after],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaIntervalStartScenario":
        value = _mapping(payload, "Interval Start")
        _exact(value, {"effect_results", "products_after"}, "Interval Start")
        return cls(
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "Interval Start effect"))
                for item in _list(value["effect_results"], "Interval Start effect_results")
            ),
            products_after=tuple(
                NiaIntervalProductSnapshot.from_dict(_mapping(item, "Interval Start product"))
                for item in _list(value["products_after"], "Interval Start products_after")
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaIntervalBuyScenario:
    """One ordered Buy request/response and resulting server product rows."""

    position_number: int
    number: int
    produce_card_customize_id: str
    provided_rewards: tuple[ProduceRewardResultSnapshot, ...]
    before_produce_points: int
    after_produce_points: int
    before_stamina: int
    after_stamina: int
    effect_results: tuple[ProduceEffectResultTrace, ...]
    products_after: tuple[NiaIntervalProductSnapshot, ...]

    def __post_init__(self) -> None:
        _plain_int(self.position_number, "Interval Buy position", minimum=1)
        _plain_int(self.number, "Interval Buy number")
        if not isinstance(self.produce_card_customize_id, str):
            raise TypeError("Interval Buy produce_card_customize_id must be text")
        rewards = tuple(self.provided_rewards)
        if not all(isinstance(value, ProduceRewardResultSnapshot) for value in rewards):
            raise TypeError("Interval Buy provided_rewards must be typed")
        object.__setattr__(self, "provided_rewards", rewards)
        for field in (
            "before_produce_points",
            "after_produce_points",
            "before_stamina",
            "after_stamina",
        ):
            _plain_int(getattr(self, field), f"Interval Buy {field}")
        object.__setattr__(
            self,
            "effect_results",
            _typed_effects(self.effect_results, "Interval Buy effect_results"),
        )
        object.__setattr__(
            self,
            "products_after",
            _typed_products(self.products_after, "Interval Buy products_after"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "buy",
            "position_number": self.position_number,
            "number": self.number,
            "produce_card_customize_id": self.produce_card_customize_id,
            "provided_rewards": [value.to_dict() for value in self.provided_rewards],
            "before_produce_points": self.before_produce_points,
            "after_produce_points": self.after_produce_points,
            "before_stamina": self.before_stamina,
            "after_stamina": self.after_stamina,
            "effect_results": [value.to_dict() for value in self.effect_results],
            "products_after": [value.to_dict() for value in self.products_after],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaIntervalBuyScenario":
        value = _mapping(payload, "Interval Buy")
        _exact(
            value,
            {
                "kind",
                "position_number",
                "number",
                "produce_card_customize_id",
                "provided_rewards",
                "before_produce_points",
                "after_produce_points",
                "before_stamina",
                "after_stamina",
                "effect_results",
                "products_after",
            },
            "Interval Buy",
        )
        if value["kind"] != "buy":
            raise ValueError("Interval Buy kind must be 'buy'")
        return cls(
            position_number=_plain_int(value["position_number"], "Interval Buy position", minimum=1),
            number=_plain_int(value["number"], "Interval Buy number"),
            produce_card_customize_id=_text(value["produce_card_customize_id"], "Interval Buy produce_card_customize_id", empty=True),
            provided_rewards=tuple(
                ProduceRewardResultSnapshot.from_dict(_mapping(item, "Interval Buy reward"))
                for item in _list(value["provided_rewards"], "Interval Buy provided_rewards")
            ),
            before_produce_points=_plain_int(value["before_produce_points"], "Interval Buy before_produce_points"),
            after_produce_points=_plain_int(value["after_produce_points"], "Interval Buy after_produce_points"),
            before_stamina=_plain_int(value["before_stamina"], "Interval Buy before_stamina"),
            after_stamina=_plain_int(value["after_stamina"], "Interval Buy after_stamina"),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "Interval Buy effect"))
                for item in _list(value["effect_results"], "Interval Buy effect_results")
            ),
            products_after=tuple(
                NiaIntervalProductSnapshot.from_dict(_mapping(item, "Interval Buy product"))
                for item in _list(value["products_after"], "Interval Buy products_after")
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaIntervalRerollScenario:
    """One Reroll response's caller-resolved CommonResponse product state."""

    products_after: tuple[NiaIntervalProductSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "products_after",
            _typed_products(self.products_after, "Interval Reroll products_after"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "reroll",
            "products_after": [value.to_dict() for value in self.products_after],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaIntervalRerollScenario":
        value = _mapping(payload, "Interval Reroll")
        _exact(value, {"kind", "products_after"}, "Interval Reroll")
        if value["kind"] != "reroll":
            raise ValueError("Interval Reroll kind must be 'reroll'")
        return cls(
            products_after=tuple(
                NiaIntervalProductSnapshot.from_dict(_mapping(item, "Interval Reroll product"))
                for item in _list(value["products_after"], "Interval Reroll products_after")
            )
        )


NiaIntervalOperation: TypeAlias = NiaIntervalBuyScenario | NiaIntervalRerollScenario


@dataclass(frozen=True, slots=True)
class NiaIntervalEndScenario:
    """Exact ordered End response effect bucket."""

    effect_results: tuple[ProduceEffectResultTrace, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "effect_results",
            _typed_effects(self.effect_results, "Interval End effect_results"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"effect_results": [value.to_dict() for value in self.effect_results]}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaIntervalEndScenario":
        value = _mapping(payload, "Interval End")
        _exact(value, {"effect_results"}, "Interval End")
        return cls(
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "Interval End effect"))
                for item in _list(value["effect_results"], "Interval End effect_results")
            )
        )


@dataclass(frozen=True, slots=True)
class NiaIntervalPostState:
    """Complete caller-authoritative persistent outer state after End."""

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
        _plain_int(self.stamina, "Interval post stamina")
        _plain_int(self.max_stamina, "Interval post max_stamina", minimum=1)
        _plain_int(self.produce_points, "Interval post produce_points")
        if self.stamina > self.max_stamina:
            raise ValueError("Interval post stamina exceeds max_stamina")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("Interval post attributes must be AttributeValues")
        cards = tuple(self.deck)
        if not all(isinstance(value, DeckEntry) for value in cards):
            raise TypeError("Interval post deck must contain DeckEntry values")
        object.__setattr__(self, "deck", cards)
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
        stamina: int | None = None,
        max_stamina: int | None = None,
        produce_points: int | None = None,
        attributes: AttributeValues | None = None,
        deck: tuple[DeckEntry, ...] | None = None,
        item_session_refs: tuple[str, ...] | None = None,
        drink_session_refs: tuple[str, ...] | None = None,
        passive_session_refs: tuple[str, ...] | None = None,
    ) -> "NiaIntervalPostState":
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
            "attributes": self.attributes.to_dict(),
            "deck": [value.to_dict() for value in self.deck],
            "item_session_refs": list(self.item_session_refs),
            "drink_session_refs": list(self.drink_session_refs),
            "passive_session_refs": list(self.passive_session_refs),
            "excluded_reward_card_ids": list(self.excluded_reward_card_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaIntervalPostState":
        value = _mapping(payload, "Interval post state")
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
        _exact(value, fields, "Interval post state")

        def _refs(name: str) -> tuple[str, ...]:
            return tuple(
                _text(item, f"Interval post {name} item", empty=True)
                for item in _list(value[name], f"Interval post {name}")
            )

        return cls(
            stamina=_plain_int(value["stamina"], "Interval post stamina"),
            max_stamina=_plain_int(value["max_stamina"], "Interval post max_stamina", minimum=1),
            produce_points=_plain_int(value["produce_points"], "Interval post produce_points"),
            attributes=_strict_attributes(value["attributes"], "Interval post attributes"),
            deck=tuple(
                _strict_deck_entry(item, "Interval post deck entry")
                for item in _list(value["deck"], "Interval post deck")
            ),
            item_session_refs=_refs("item_session_refs"),
            drink_session_refs=_refs("drink_session_refs"),
            passive_session_refs=_refs("passive_session_refs"),
            excluded_reward_card_ids=_refs("excluded_reward_card_ids"),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularNiaIntervalScenario:
    """One exact caller-owned Interval Start/operations/End lifecycle."""

    week: int
    schedule_refresh_stamina: int
    branch: ChanceBranch
    start: NiaIntervalStartScenario
    operations: tuple[NiaIntervalOperation, ...]
    end: NiaIntervalEndScenario
    post_state: NiaIntervalPostState
    authority: NiaIntervalScenarioAuthority
    runtime_ref: str

    def __post_init__(self) -> None:
        _plain_int(self.week, "Interval scenario week", minimum=1)
        _plain_int(
            self.schedule_refresh_stamina,
            "Interval schedule_refresh_stamina observation",
        )
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("Interval branch must be ChanceBranch")
        if not isinstance(self.start, NiaIntervalStartScenario):
            raise TypeError("Interval start must be typed")
        operations = tuple(self.operations)
        if not all(
            isinstance(value, (NiaIntervalBuyScenario, NiaIntervalRerollScenario))
            for value in operations
        ):
            raise TypeError("Interval operations must be typed Buy/Reroll values")
        object.__setattr__(self, "operations", operations)
        if not isinstance(self.end, NiaIntervalEndScenario):
            raise TypeError("Interval end must be typed")
        if not isinstance(self.post_state, NiaIntervalPostState):
            raise TypeError("Interval post_state must be typed")
        authority = self.authority
        if isinstance(authority, str) and not isinstance(
            authority,
            NiaIntervalScenarioAuthority,
        ):
            try:
                authority = NiaIntervalScenarioAuthority(authority)
            except ValueError as error:
                raise ValueError("Interval scenario authority is invalid") from error
            object.__setattr__(self, "authority", authority)
        if not isinstance(authority, NiaIntervalScenarioAuthority):
            raise TypeError("Interval scenario authority must be typed")
        if not isinstance(self.runtime_ref, str) or not self.runtime_ref:
            raise ValueError("Interval scenario runtime_ref is required")
        self._validate_product_sequence()

    def _validate_product_sequence(self) -> None:
        products = self.start.products_after
        for index, operation in enumerate(self.operations):
            if isinstance(operation, NiaIntervalBuyScenario):
                positions = {value.position_number for value in products}
                if operation.position_number not in positions:
                    raise ValueError(
                        "Interval Buy position is absent from the preceding "
                        f"server product snapshot: operation={index}"
                    )
            products = operation.products_after

    @property
    def schedule_refresh_stamina_applied(self) -> bool:
        """Interval never consumes the separate Refresh-step display field."""

        return False

    @property
    def authentic_nia_local_save(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "schedule_refresh_stamina": self.schedule_refresh_stamina,
            "branch": self.branch.to_dict(),
            "start": self.start.to_dict(),
            "operations": [value.to_dict() for value in self.operations],
            "end": self.end.to_dict(),
            "post_state": self.post_state.to_dict(),
            "authority": self.authority.value,
            "runtime_ref": self.runtime_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularNiaIntervalScenario":
        value = _mapping(payload, "Interval scenario")
        _exact(
            value,
            {
                "week",
                "schedule_refresh_stamina",
                "branch",
                "start",
                "operations",
                "end",
                "post_state",
                "authority",
                "runtime_ref",
            },
            "Interval scenario",
        )
        operations: list[NiaIntervalOperation] = []
        for raw in _list(value["operations"], "Interval operations"):
            operation = _mapping(raw, "Interval operation")
            kind = operation.get("kind")
            if kind == "buy":
                operations.append(NiaIntervalBuyScenario.from_dict(operation))
            elif kind == "reroll":
                operations.append(NiaIntervalRerollScenario.from_dict(operation))
            else:
                raise ValueError("Interval operation kind must be 'buy' or 'reroll'")
        authority = _text(value["authority"], "Interval scenario authority")
        try:
            authority_value = NiaIntervalScenarioAuthority(authority)
        except ValueError as error:
            raise ValueError("Interval scenario authority is invalid") from error
        return cls(
            week=_plain_int(value["week"], "Interval scenario week", minimum=1),
            schedule_refresh_stamina=_plain_int(
                value["schedule_refresh_stamina"],
                "Interval schedule_refresh_stamina",
            ),
            branch=_strict_branch(value["branch"], "Interval scenario branch"),
            start=NiaIntervalStartScenario.from_dict(_mapping(value["start"], "Interval scenario start")),
            operations=tuple(operations),
            end=NiaIntervalEndScenario.from_dict(_mapping(value["end"], "Interval scenario end")),
            post_state=NiaIntervalPostState.from_dict(_mapping(value["post_state"], "Interval scenario post_state")),
            authority=authority_value,
            runtime_ref=_text(value["runtime_ref"], "Interval scenario runtime_ref"),
        )


def _issue(
    code: str,
    field: str,
    detail: str = "",
) -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _scenario_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaIntervalScenario,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    if state.phase is not RolloutPhase.WAITING_EXTERNAL:
        issues.append(
            _issue("nia-interval-request-not-pending", "outer_state.phase", state.phase.value)
        )
    if state.pending != request:
        issues.append(
            _issue("nia-interval-pending-request-mismatch", "outer_state.pending")
        )
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(
            _issue("nia-interval-request-kind-mismatch", "request.kind", request.kind.value)
        )
    if request.action_id != NIA_INTERVAL_ACTION_ID:
        issues.append(
            _issue("nia-interval-action-mismatch", "request.action_id", request.action_id)
        )
    if request.week != state.week or scenario.week != state.week:
        issues.append(
            _issue(
                "nia-interval-week-mismatch",
                "request/scenario.week",
                f"state={state.week}:request={request.week}:scenario={scenario.week}",
            )
        )
    if request.stage_type is not None:
        issues.append(
            _issue(
                "nia-interval-stage-type-unexpected",
                "request.stage_type",
                request.stage_type,
            )
        )
    if state.mode_id not in {"produce-004", "produce-005"}:
        issues.append(
            _issue("nia-interval-produce-id-mismatch", "outer_state.mode_id", state.mode_id)
        )
    if scenario.branch.probability is None or scenario.branch.probability <= 0.0:
        issues.append(
            _issue("nia-interval-branch-probability-unresolved", "scenario.branch.probability")
        )
    # Every persistent field is authoritative after End.  Do not demand that
    # any one scalar Buy response equal it: Start/Buy/End EffectResults and a
    # Reroll CommonResponse are distinct ordered buckets whose cross-bucket
    # mutation order is not reconstructed by this bounded adapter.
    return tuple(dict.fromkeys(issues))


def adapt_initial_regular_nia_interval_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaIntervalScenario,
) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
    """Project an exact Interval lifecycle to the common outer kernel.

    Product membership and all persistent mutations come from the caller's
    response scenario.  ``schedule_refresh_stamina`` is retained as a raw
    route observation but is deliberately not applied: native uses it only on
    the distinct Refresh step's schedule cell.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularNiaIntervalScenario):
        raise TypeError("scenario must be InitialRegularNiaIntervalScenario")
    issues = _scenario_issues(state, request, scenario)
    if issues:
        return issues
    return WeeklyActionOutcome(
        request_id=request.request_id,
        branch=scenario.branch,
        patch=scenario.post_state.to_patch(),
        reward_requested=False,
    )


InitialRegularNiaIntervalAdapterResult: TypeAlias = (
    WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "ANDROID_INTERVAL_BUY_REQUEST_FIELDS",
    "ANDROID_INTERVAL_BUY_RESPONSE_FIELDS",
    "ANDROID_INTERVAL_END_REQUEST_FIELDS",
    "ANDROID_INTERVAL_END_RESPONSE_FIELDS",
    "ANDROID_INTERVAL_PRODUCT_FIELDS",
    "ANDROID_INTERVAL_REROLL_REQUEST_FIELDS",
    "ANDROID_INTERVAL_REROLL_RESPONSE_FIELDS",
    "ANDROID_INTERVAL_START_REQUEST_FIELDS",
    "ANDROID_INTERVAL_START_RESPONSE_FIELDS",
    "ANDROID_INTERVAL_STEP_TYPE_VALUE",
    "ANDROID_REFRESH_RECOVERY_AUTHORITY",
    "ANDROID_REFRESH_REQUEST_FIELDS",
    "ANDROID_REFRESH_RESPONSE_FIELDS",
    "ANDROID_SCHEDULE_REFRESH_STAMINA_DISPLAY_STEP_TYPE",
    "ANDROID_SCHEDULE_REFRESH_STAMINA_DISPLAY_STEP_TYPE_VALUE",
    "InitialRegularNiaIntervalAdapterResult",
    "InitialRegularNiaIntervalScenario",
    "NIA_INTERVAL_ACTION_ID",
    "NIA_REFRESH_ACTION_ID",
    "NiaIntervalBuyScenario",
    "NiaIntervalEndScenario",
    "NiaIntervalOperation",
    "NiaIntervalPostState",
    "NiaIntervalProductSnapshot",
    "NiaIntervalRerollScenario",
    "NiaIntervalScenarioAuthority",
    "NiaIntervalStartScenario",
    "adapt_initial_regular_nia_interval_scenario",
]
