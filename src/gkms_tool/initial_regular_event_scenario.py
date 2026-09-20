"""Strict offline scenario facts for Initial Regular outer events.

The game client sends only a suggestion index and receives a server-resolved
``ProduceStepEventResponse``.  Consequently this module preserves the response
effect order exactly, but deliberately assigns no Master bucket meaning to that
order.  Direct costs and the final outer state are separate authoritative
boundaries; neither is reconstructed from the effect trace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import json


INITIAL_REGULAR_EVENT_SCENARIO_SCHEMA_VERSION = 1
_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_EFFECT_STATE_FIELDS = (
    "max_stamina",
    "stamina",
    "produce_point",
    "vote_count",
    "star",
    "vocal",
    "dance",
    "visual",
    "produce_cards",
    "high_score_gold",
)


class EffectResultChainError(ValueError):
    """Adjacent server result boundaries disagree."""

    def __init__(self, mismatches: Sequence["EffectResultChainMismatch"]) -> None:
        values = tuple(mismatches)
        if not values:
            raise ValueError("effect-result chain error needs a mismatch")
        self.mismatches = values
        summary = ", ".join(
            f"{value.left_index}->{value.right_index}:{value.field}"
            for value in values
        )
        super().__init__(f"effect-result chain mismatch: {summary}")


class PresentScenarioError(ValueError):
    """A present sequence or receive choice is internally inconsistent."""


def _exact(payload: Mapping[str, object], fields: set[str], name: str) -> None:
    actual = set(payload)
    if actual != fields:
        raise ValueError(
            f"{name} fields differ: missing={sorted(fields - actual)!r}; "
            f"extra={sorted(actual - fields)!r}"
        )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{name} keys must be strings")
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be a list")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text")
    return value


def _text(value: object, name: str) -> str:
    result = _string(value, name)
    if not result.strip():
        raise ValueError(f"{name} must be non-empty text")
    return result


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be boolean")
    return value


def _int32(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if not _INT32_MIN <= value <= _INT32_MAX:
        raise ValueError(f"{name} must fit in a signed 32-bit integer")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    result = _int32(value, name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _positive_int(value: object, name: str) -> int:
    result = _int32(value, name)
    if result < 1:
        raise ValueError(f"{name} must be positive")
    return result


def _optional_nonnegative_int(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, name)


def _optional_nonnegative_index(value: object, name: str) -> int | None:
    return _optional_nonnegative_int(value, name)


@dataclass(frozen=True, slots=True)
class ProduceCardCustomizeSnapshot:
    """Proto-shaped ``ProduceCardCustomize`` value."""

    id: str
    customize_count: int

    def __post_init__(self) -> None:
        _text(self.id, "card customize id")
        _nonnegative_int(self.customize_count, "card customize count")

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "customize_count": self.customize_count}

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "ProduceCardCustomizeSnapshot":
        value = _mapping(payload, "card customize")
        _exact(value, {"id", "customize_count"}, "card customize")
        return cls(
            id=_text(value["id"], "card customize id"),
            customize_count=_nonnegative_int(
                value["customize_count"], "card customize count"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProduceCardSnapshot:
    """One ordered card value from ``ProduceEffectResult``."""

    id: str
    upgrade_count: int
    customizes: tuple[ProduceCardCustomizeSnapshot, ...] = ()
    produce_card_skin_id: str = ""

    def __post_init__(self) -> None:
        _text(self.id, "produce card id")
        _nonnegative_int(self.upgrade_count, "produce card upgrade count")
        values = tuple(self.customizes)
        if not all(isinstance(value, ProduceCardCustomizeSnapshot) for value in values):
            raise TypeError("produce card customizes must contain typed snapshots")
        object.__setattr__(self, "customizes", values)
        _string(self.produce_card_skin_id, "produce card skin id")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "upgrade_count": self.upgrade_count,
            "customizes": [value.to_dict() for value in self.customizes],
            "produce_card_skin_id": self.produce_card_skin_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ProduceCardSnapshot":
        value = _mapping(payload, "produce card")
        _exact(
            value,
            {"id", "upgrade_count", "customizes", "produce_card_skin_id"},
            "produce card",
        )
        customizes = _list(value["customizes"], "produce card customizes")
        return cls(
            id=_text(value["id"], "produce card id"),
            upgrade_count=_nonnegative_int(
                value["upgrade_count"], "produce card upgrade count"
            ),
            customizes=tuple(
                ProduceCardCustomizeSnapshot.from_dict(
                    _mapping(item, "produce card customize")
                )
                for item in customizes
            ),
            produce_card_skin_id=_string(
                value["produce_card_skin_id"], "produce card skin id"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProduceRewardResultSnapshot:
    """One ordered ``ProduceRewardResult`` supplied by an effect."""

    resource_type: str
    resource_id: str
    resource_level: int
    quantity: int
    customizes: tuple[ProduceCardCustomizeSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _text(self.resource_type, "reward resource type")
        _string(self.resource_id, "reward resource id")
        _nonnegative_int(self.resource_level, "reward resource level")
        _nonnegative_int(self.quantity, "reward quantity")
        values = tuple(self.customizes)
        if not all(isinstance(value, ProduceCardCustomizeSnapshot) for value in values):
            raise TypeError("reward customizes must contain typed snapshots")
        object.__setattr__(self, "customizes", values)

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_level": self.resource_level,
            "quantity": self.quantity,
            "customizes": [value.to_dict() for value in self.customizes],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "ProduceRewardResultSnapshot":
        value = _mapping(payload, "produce reward result")
        _exact(
            value,
            {
                "resource_type",
                "resource_id",
                "resource_level",
                "quantity",
                "customizes",
            },
            "produce reward result",
        )
        customizes = _list(value["customizes"], "reward customizes")
        return cls(
            resource_type=_text(value["resource_type"], "reward resource type"),
            resource_id=_string(value["resource_id"], "reward resource id"),
            resource_level=_nonnegative_int(
                value["resource_level"], "reward resource level"
            ),
            quantity=_nonnegative_int(value["quantity"], "reward quantity"),
            customizes=tuple(
                ProduceCardCustomizeSnapshot.from_dict(
                    _mapping(item, "reward customize")
                )
                for item in customizes
            ),
        )


@dataclass(frozen=True, slots=True)
class ProduceEffectStateSnapshot:
    """Observed state fields on one side of a server effect result.

    ``None`` means that a capture did not establish that proto dimension.  It
    is distinct from the valid observed value zero and lets chain validation
    remain fail-closed without inventing proto3 scalar presence.
    """

    max_stamina: int | None = None
    stamina: int | None = None
    produce_point: int | None = None
    vote_count: int | None = None
    star: int | None = None
    vocal: int | None = None
    dance: int | None = None
    visual: int | None = None
    produce_cards: tuple[ProduceCardSnapshot, ...] | None = None
    high_score_gold: int | None = None

    def __post_init__(self) -> None:
        for name in _EFFECT_STATE_FIELDS:
            value = getattr(self, name)
            if name == "produce_cards":
                if value is None:
                    continue
                cards = tuple(value)
                if not all(isinstance(card, ProduceCardSnapshot) for card in cards):
                    raise TypeError("effect-state cards must contain typed snapshots")
                object.__setattr__(self, name, cards)
                continue
            _optional_nonnegative_int(value, f"effect-state {name}")
        if (
            self.stamina is not None
            and self.max_stamina is not None
            and self.stamina > self.max_stamina
        ):
            raise ValueError("effect-state stamina exceeds max stamina")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_stamina": self.max_stamina,
            "stamina": self.stamina,
            "produce_point": self.produce_point,
            "vote_count": self.vote_count,
            "star": self.star,
            "vocal": self.vocal,
            "dance": self.dance,
            "visual": self.visual,
            "produce_cards": (
                None
                if self.produce_cards is None
                else [card.to_dict() for card in self.produce_cards]
            ),
            "high_score_gold": self.high_score_gold,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "ProduceEffectStateSnapshot":
        value = _mapping(payload, "produce effect state")
        _exact(value, set(_EFFECT_STATE_FIELDS), "produce effect state")
        raw_cards = value["produce_cards"]
        cards: tuple[ProduceCardSnapshot, ...] | None
        if raw_cards is None:
            cards = None
        else:
            cards = tuple(
                ProduceCardSnapshot.from_dict(_mapping(item, "effect-state card"))
                for item in _list(raw_cards, "effect-state cards")
            )
        return cls(
            **{
                name: _optional_nonnegative_int(
                    value[name], f"effect-state {name}"
                )
                for name in _EFFECT_STATE_FIELDS
                if name != "produce_cards"
            },
            produce_cards=cards,
        )


@dataclass(frozen=True, slots=True)
class ProduceEffectResultTrace:
    """One response-ordered ``ProduceEffectResult`` without bucket inference."""

    effect_type: str
    produce_effect_id: str
    origin: str
    effect_value: int
    effect_target_id: str
    before: ProduceEffectStateSnapshot
    after: ProduceEffectStateSnapshot
    provided_rewards: tuple[ProduceRewardResultSnapshot, ...] = ()
    effect_numbers: tuple[int, ...] = ()
    ineffective: bool = False

    def __post_init__(self) -> None:
        _text(self.effect_type, "effect type")
        _string(self.produce_effect_id, "produce effect id")
        _text(self.origin, "effect origin")
        _int32(self.effect_value, "effect value")
        _string(self.effect_target_id, "effect target id")
        if not isinstance(self.before, ProduceEffectStateSnapshot):
            raise TypeError("before must be ProduceEffectStateSnapshot")
        if not isinstance(self.after, ProduceEffectStateSnapshot):
            raise TypeError("after must be ProduceEffectStateSnapshot")
        rewards = tuple(self.provided_rewards)
        if not all(isinstance(value, ProduceRewardResultSnapshot) for value in rewards):
            raise TypeError("provided rewards must contain typed snapshots")
        object.__setattr__(self, "provided_rewards", rewards)
        numbers = tuple(self.effect_numbers)
        for index, value in enumerate(numbers):
            _int32(value, f"effect_numbers[{index}]")
        object.__setattr__(self, "effect_numbers", numbers)
        _boolean(self.ineffective, "ineffective")

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_type": self.effect_type,
            "produce_effect_id": self.produce_effect_id,
            "origin": self.origin,
            "effect_value": self.effect_value,
            "effect_target_id": self.effect_target_id,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "provided_rewards": [value.to_dict() for value in self.provided_rewards],
            "effect_numbers": list(self.effect_numbers),
            "ineffective": self.ineffective,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "ProduceEffectResultTrace":
        value = _mapping(payload, "produce effect result")
        fields = {
            "effect_type",
            "produce_effect_id",
            "origin",
            "effect_value",
            "effect_target_id",
            "before",
            "after",
            "provided_rewards",
            "effect_numbers",
            "ineffective",
        }
        _exact(value, fields, "produce effect result")
        rewards = _list(value["provided_rewards"], "provided rewards")
        numbers = _list(value["effect_numbers"], "effect numbers")
        return cls(
            effect_type=_text(value["effect_type"], "effect type"),
            produce_effect_id=_string(
                value["produce_effect_id"], "produce effect id"
            ),
            origin=_text(value["origin"], "effect origin"),
            effect_value=_int32(value["effect_value"], "effect value"),
            effect_target_id=_string(value["effect_target_id"], "effect target id"),
            before=ProduceEffectStateSnapshot.from_dict(
                _mapping(value["before"], "before effect state")
            ),
            after=ProduceEffectStateSnapshot.from_dict(
                _mapping(value["after"], "after effect state")
            ),
            provided_rewards=tuple(
                ProduceRewardResultSnapshot.from_dict(
                    _mapping(item, "provided reward")
                )
                for item in rewards
            ),
            effect_numbers=tuple(
                _int32(item, f"effect_numbers[{index}]")
                for index, item in enumerate(numbers)
            ),
            ineffective=_boolean(value["ineffective"], "ineffective"),
        )


@dataclass(frozen=True, slots=True)
class EffectResultChainMismatch:
    left_index: int
    right_index: int
    field: str
    left_after: object
    right_before: object


def validate_effect_result_chain(
    values: Sequence[ProduceEffectResultTrace],
) -> None:
    """Validate only overlapping observed fields on adjacent response entries."""

    trace = tuple(values)
    if not all(isinstance(value, ProduceEffectResultTrace) for value in trace):
        raise TypeError("effect trace must contain ProduceEffectResultTrace values")
    mismatches: list[EffectResultChainMismatch] = []
    for left_index, (left, right) in enumerate(zip(trace, trace[1:])):
        for field in _EFFECT_STATE_FIELDS:
            left_after = getattr(left.after, field)
            right_before = getattr(right.before, field)
            if left_after is None or right_before is None:
                continue
            if left_after != right_before:
                mismatches.append(
                    EffectResultChainMismatch(
                        left_index=left_index,
                        right_index=left_index + 1,
                        field=field,
                        left_after=left_after,
                        right_before=right_before,
                    )
                )
    if mismatches:
        raise EffectResultChainError(mismatches)


@dataclass(frozen=True, slots=True)
class EffectiveDirectCosts:
    """Server-effective costs, kept separate from raw Master fields."""

    stamina: int
    produce_point: int

    def __post_init__(self) -> None:
        _nonnegative_int(self.stamina, "effective stamina cost")
        _nonnegative_int(self.produce_point, "effective produce-point cost")

    def to_dict(self) -> dict[str, object]:
        return {"stamina": self.stamina, "produce_point": self.produce_point}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "EffectiveDirectCosts":
        value = _mapping(payload, "effective direct costs")
        _exact(value, {"stamina", "produce_point"}, "effective direct costs")
        return cls(
            stamina=_nonnegative_int(value["stamina"], "effective stamina cost"),
            produce_point=_nonnegative_int(
                value["produce_point"], "effective produce-point cost"
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularPostState:
    """Complete authoritative outer-state boundary after the event response."""

    max_stamina: int
    stamina: int
    produce_point: int
    vote_count: int
    star: int
    vocal: int
    dance: int
    visual: int
    produce_cards: tuple[ProduceCardSnapshot, ...]
    high_score_gold: int

    def __post_init__(self) -> None:
        for name in _EFFECT_STATE_FIELDS:
            value = getattr(self, name)
            if name == "produce_cards":
                cards = tuple(value)
                if not all(isinstance(card, ProduceCardSnapshot) for card in cards):
                    raise TypeError("post-state cards must contain typed snapshots")
                object.__setattr__(self, name, cards)
            else:
                _nonnegative_int(value, f"post-state {name}")
        if self.stamina > self.max_stamina:
            raise ValueError("post-state stamina exceeds max stamina")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_stamina": self.max_stamina,
            "stamina": self.stamina,
            "produce_point": self.produce_point,
            "vote_count": self.vote_count,
            "star": self.star,
            "vocal": self.vocal,
            "dance": self.dance,
            "visual": self.visual,
            "produce_cards": [card.to_dict() for card in self.produce_cards],
            "high_score_gold": self.high_score_gold,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularPostState":
        value = _mapping(payload, "initial-regular post state")
        _exact(value, set(_EFFECT_STATE_FIELDS), "initial-regular post state")
        cards = _list(value["produce_cards"], "post-state cards")
        return cls(
            **{
                name: _nonnegative_int(value[name], f"post-state {name}")
                for name in _EFFECT_STATE_FIELDS
                if name != "produce_cards"
            },
            produce_cards=tuple(
                ProduceCardSnapshot.from_dict(_mapping(item, "post-state card"))
                for item in cards
            ),
        )


@dataclass(frozen=True, slots=True)
class NextStepRef:
    """Already-resolved direct/success/fail continuation target."""

    step_type: str
    step_id: str

    def __post_init__(self) -> None:
        _text(self.step_type, "next step type")
        _text(self.step_id, "next step id")

    def to_dict(self) -> dict[str, object]:
        return {"step_type": self.step_type, "step_id": self.step_id}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NextStepRef":
        value = _mapping(payload, "next step")
        _exact(value, {"step_type", "step_id"}, "next step")
        return cls(
            step_type=_text(value["step_type"], "next step type"),
            step_id=_text(value["step_id"], "next step id"),
        )


@dataclass(frozen=True, slots=True)
class ScenarioBlocker:
    """Explicit unresolved semantics; never synthesized from an effect ID."""

    code: str
    message: str
    effect_result_index: int | None = None
    produce_effect_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.code, "scenario blocker code")
        _text(self.message, "scenario blocker message")
        _optional_nonnegative_index(
            self.effect_result_index, "scenario blocker effect-result index"
        )
        if self.produce_effect_id is not None:
            _text(self.produce_effect_id, "scenario blocker produce-effect id")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "effect_result_index": self.effect_result_index,
            "produce_effect_id": self.produce_effect_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ScenarioBlocker":
        value = _mapping(payload, "scenario blocker")
        _exact(
            value,
            {"code", "message", "effect_result_index", "produce_effect_id"},
            "scenario blocker",
        )
        raw_effect_id = value["produce_effect_id"]
        return cls(
            code=_text(value["code"], "scenario blocker code"),
            message=_text(value["message"], "scenario blocker message"),
            effect_result_index=_optional_nonnegative_index(
                value["effect_result_index"],
                "scenario blocker effect-result index",
            ),
            produce_effect_id=(
                None
                if raw_effect_id is None
                else _text(raw_effect_id, "scenario blocker produce-effect id")
            ),
        )


@dataclass(frozen=True, slots=True)
class UserProduceProgressPresentReward:
    """Nested transaction ``UserProduceProgressPresent.Reward``."""

    resource_type: str
    resource_id: str
    resource_level: int
    quantity: int

    def __post_init__(self) -> None:
        _text(self.resource_type, "present reward resource type")
        _string(self.resource_id, "present reward resource id")
        _nonnegative_int(self.resource_level, "present reward resource level")
        _positive_int(self.quantity, "present reward quantity")

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "resource_level": self.resource_level,
            "quantity": self.quantity,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "UserProduceProgressPresentReward":
        value = _mapping(payload, "present reward")
        _exact(
            value,
            {"resource_type", "resource_id", "resource_level", "quantity"},
            "present reward",
        )
        return cls(
            resource_type=_text(
                value["resource_type"], "present reward resource type"
            ),
            resource_id=_string(value["resource_id"], "present reward resource id"),
            resource_level=_nonnegative_int(
                value["resource_level"], "present reward resource level"
            ),
            quantity=_positive_int(value["quantity"], "present reward quantity"),
        )


@dataclass(frozen=True, slots=True)
class UserProduceProgressPresent:
    """One exact server-owned present, including receive state and picks."""

    position_number: int
    received: bool
    display_type: str
    reward_count: int
    pick_count: int
    rewards: tuple[UserProduceProgressPresentReward, ...]
    reward_indexes: tuple[int, ...] = ()
    is_vote_bonus: bool = False
    research_external_indexes: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative_int(self.position_number, "present position number")
        _boolean(self.received, "present received")
        _text(self.display_type, "present display type")
        _nonnegative_int(self.reward_count, "present reward count")
        _nonnegative_int(self.pick_count, "present pick count")
        rewards = tuple(self.rewards)
        if not all(isinstance(value, UserProduceProgressPresentReward) for value in rewards):
            raise TypeError("present rewards must contain typed rewards")
        object.__setattr__(self, "rewards", rewards)
        indexes = tuple(self.reward_indexes)
        for index, value in enumerate(indexes):
            _nonnegative_int(value, f"present reward_indexes[{index}]")
        if len(indexes) != len(set(indexes)):
            raise PresentScenarioError("present reward indexes must be unique")
        if any(value >= len(rewards) for value in indexes):
            raise PresentScenarioError("present reward index is out of range")
        if self.pick_count > len(rewards):
            raise PresentScenarioError("present pick count exceeds reward list")
        if len(indexes) > self.pick_count:
            raise PresentScenarioError("present has more reward indexes than picks")
        if self.received and len(indexes) != self.pick_count:
            raise PresentScenarioError(
                "received present must bind exactly pick_count reward indexes"
            )
        object.__setattr__(self, "reward_indexes", indexes)
        _boolean(self.is_vote_bonus, "present vote bonus")
        research = tuple(self.research_external_indexes)
        for index, value in enumerate(research):
            _nonnegative_int(value, f"research_external_indexes[{index}]")
        if len(research) != len(set(research)):
            raise PresentScenarioError("research external indexes must be unique")
        object.__setattr__(self, "research_external_indexes", research)

    def receive(self, reward_indexes: Sequence[int]) -> "UserProduceProgressPresent":
        """Return the received state; replaying the identical receive is idempotent."""

        indexes = tuple(reward_indexes)
        for index, value in enumerate(indexes):
            _nonnegative_int(value, f"receive reward_indexes[{index}]")
        if len(indexes) != self.pick_count:
            raise PresentScenarioError("receive indexes must equal present pick_count")
        if len(indexes) != len(set(indexes)):
            raise PresentScenarioError("receive indexes must be unique")
        if any(value >= len(self.rewards) for value in indexes):
            raise PresentScenarioError("receive reward index is out of range")
        if self.reward_indexes and self.reward_indexes != indexes:
            raise PresentScenarioError("receive conflicts with server-bound indexes")
        if self.received:
            return self
        return replace(self, received=True, reward_indexes=indexes)

    def to_dict(self) -> dict[str, object]:
        return {
            "position_number": self.position_number,
            "received": self.received,
            "display_type": self.display_type,
            "reward_count": self.reward_count,
            "pick_count": self.pick_count,
            "rewards": [value.to_dict() for value in self.rewards],
            "reward_indexes": list(self.reward_indexes),
            "is_vote_bonus": self.is_vote_bonus,
            "research_external_indexes": list(self.research_external_indexes),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "UserProduceProgressPresent":
        value = _mapping(payload, "user produce-progress present")
        fields = {
            "position_number",
            "received",
            "display_type",
            "reward_count",
            "pick_count",
            "rewards",
            "reward_indexes",
            "is_vote_bonus",
            "research_external_indexes",
        }
        _exact(value, fields, "user produce-progress present")
        rewards = _list(value["rewards"], "present rewards")
        indexes = _list(value["reward_indexes"], "present reward indexes")
        research = _list(
            value["research_external_indexes"], "research external indexes"
        )
        return cls(
            position_number=_nonnegative_int(
                value["position_number"], "present position number"
            ),
            received=_boolean(value["received"], "present received"),
            display_type=_text(value["display_type"], "present display type"),
            reward_count=_nonnegative_int(
                value["reward_count"], "present reward count"
            ),
            pick_count=_nonnegative_int(value["pick_count"], "present pick count"),
            rewards=tuple(
                UserProduceProgressPresentReward.from_dict(
                    _mapping(item, "present reward")
                )
                for item in rewards
            ),
            reward_indexes=tuple(
                _nonnegative_int(item, f"present reward_indexes[{index}]")
                for index, item in enumerate(indexes)
            ),
            is_vote_bonus=_boolean(value["is_vote_bonus"], "present vote bonus"),
            research_external_indexes=tuple(
                _nonnegative_int(item, f"research_external_indexes[{index}]")
                for index, item in enumerate(research)
            ),
        )


def validate_present_position_order(
    values: Sequence[UserProduceProgressPresent],
) -> None:
    """Require response order to agree with native PositionNumber ordering."""

    presents = tuple(values)
    if not all(isinstance(value, UserProduceProgressPresent) for value in presents):
        raise TypeError("presents must contain UserProduceProgressPresent values")
    positions = tuple(value.position_number for value in presents)
    if any(left >= right for left, right in zip(positions, positions[1:])):
        raise PresentScenarioError(
            "present positions must be unique and strictly increasing"
        )


@dataclass(frozen=True, slots=True)
class InitialRegularEventScenario:
    """One exact, replayable outer-event response boundary."""

    detail_id: str
    suggestion_id: str
    suggestion_index: int
    success: bool
    effective_direct_costs: EffectiveDirectCosts
    effect_results: tuple[ProduceEffectResultTrace, ...]
    post_state: InitialRegularPostState
    next_step: NextStepRef | None = None
    presents: tuple[UserProduceProgressPresent, ...] = ()
    blockers: tuple[ScenarioBlocker, ...] = ()

    def __post_init__(self) -> None:
        _text(self.detail_id, "event detail id")
        _text(self.suggestion_id, "event suggestion id")
        _nonnegative_int(self.suggestion_index, "event suggestion index")
        _boolean(self.success, "event success")
        if not isinstance(self.effective_direct_costs, EffectiveDirectCosts):
            raise TypeError("effective_direct_costs must be EffectiveDirectCosts")
        results = tuple(self.effect_results)
        if not all(isinstance(value, ProduceEffectResultTrace) for value in results):
            raise TypeError("effect_results must contain typed trace values")
        object.__setattr__(self, "effect_results", results)
        validate_effect_result_chain(results)
        if not isinstance(self.post_state, InitialRegularPostState):
            raise TypeError("post_state must be InitialRegularPostState")
        if self.next_step is not None and not isinstance(self.next_step, NextStepRef):
            raise TypeError("next_step must be NextStepRef or None")
        presents = tuple(self.presents)
        if not all(isinstance(value, UserProduceProgressPresent) for value in presents):
            raise TypeError("presents must contain typed transaction values")
        object.__setattr__(self, "presents", presents)
        validate_present_position_order(presents)
        blockers = tuple(self.blockers)
        if not all(isinstance(value, ScenarioBlocker) for value in blockers):
            raise TypeError("blockers must contain ScenarioBlocker values")
        for blocker in blockers:
            if (
                blocker.effect_result_index is not None
                and blocker.effect_result_index >= len(results)
            ):
                raise ValueError("scenario blocker effect-result index is out of range")
        object.__setattr__(self, "blockers", blockers)

    @property
    def executable(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": INITIAL_REGULAR_EVENT_SCENARIO_SCHEMA_VERSION,
            "detail_id": self.detail_id,
            "suggestion_id": self.suggestion_id,
            "suggestion_index": self.suggestion_index,
            "success": self.success,
            "effective_direct_costs": self.effective_direct_costs.to_dict(),
            "effect_results": [value.to_dict() for value in self.effect_results],
            "post_state": self.post_state.to_dict(),
            "next_step": None if self.next_step is None else self.next_step.to_dict(),
            "presents": [value.to_dict() for value in self.presents],
            "blockers": [value.to_dict() for value in self.blockers],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularEventScenario":
        value = _mapping(payload, "initial-regular event scenario")
        fields = {
            "schema_version",
            "detail_id",
            "suggestion_id",
            "suggestion_index",
            "success",
            "effective_direct_costs",
            "effect_results",
            "post_state",
            "next_step",
            "presents",
            "blockers",
        }
        _exact(value, fields, "initial-regular event scenario")
        version = _nonnegative_int(value["schema_version"], "scenario schema version")
        if version != INITIAL_REGULAR_EVENT_SCENARIO_SCHEMA_VERSION:
            raise ValueError(f"unsupported initial-regular event schema: {version}")
        raw_next_step = value["next_step"]
        results = _list(value["effect_results"], "scenario effect results")
        presents = _list(value["presents"], "scenario presents")
        blockers = _list(value["blockers"], "scenario blockers")
        return cls(
            detail_id=_text(value["detail_id"], "event detail id"),
            suggestion_id=_text(value["suggestion_id"], "event suggestion id"),
            suggestion_index=_nonnegative_int(
                value["suggestion_index"], "event suggestion index"
            ),
            success=_boolean(value["success"], "event success"),
            effective_direct_costs=EffectiveDirectCosts.from_dict(
                _mapping(value["effective_direct_costs"], "effective direct costs")
            ),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(
                    _mapping(item, "scenario effect result")
                )
                for item in results
            ),
            post_state=InitialRegularPostState.from_dict(
                _mapping(value["post_state"], "scenario post state")
            ),
            next_step=(
                None
                if raw_next_step is None
                else NextStepRef.from_dict(_mapping(raw_next_step, "scenario next step"))
            ),
            presents=tuple(
                UserProduceProgressPresent.from_dict(
                    _mapping(item, "scenario present")
                )
                for item in presents
            ),
            blockers=tuple(
                ScenarioBlocker.from_dict(_mapping(item, "scenario blocker"))
                for item in blockers
            ),
        )

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, payload: str) -> "InitialRegularEventScenario":
        if not isinstance(payload, str):
            raise TypeError("scenario JSON must be text")

        def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON field: {key}")
                result[key] = value
            return result

        def reject_constant(value: str) -> object:
            raise ValueError(f"invalid JSON constant: {value}")

        decoded = json.loads(
            payload,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
        return cls.from_dict(_mapping(decoded, "initial-regular event scenario JSON"))


__all__ = [
    "EffectResultChainError",
    "EffectResultChainMismatch",
    "EffectiveDirectCosts",
    "INITIAL_REGULAR_EVENT_SCENARIO_SCHEMA_VERSION",
    "InitialRegularEventScenario",
    "InitialRegularPostState",
    "NextStepRef",
    "PresentScenarioError",
    "ProduceCardCustomizeSnapshot",
    "ProduceCardSnapshot",
    "ProduceEffectResultTrace",
    "ProduceEffectStateSnapshot",
    "ProduceRewardResultSnapshot",
    "ScenarioBlocker",
    "UserProduceProgressPresent",
    "UserProduceProgressPresentReward",
    "validate_effect_result_chain",
    "validate_present_position_order",
]
