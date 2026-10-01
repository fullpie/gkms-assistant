"""Pure outer-Produce state machine for offline whole-run rollouts.

The inner lesson/exam kernels model a battle after its runtime inputs are
known.  This module owns the lifecycle around those kernels.  It deliberately
does not invent server-owned offers, SP rolls, events, or RNG.  Every such
boundary becomes an :class:`ExternalRequest` that a caller may observe,
enumerate for Expectimax, or resolve from recorded game data.

The kernel is policy agnostic and performs no game, UI, save-file, or process
I/O.  Static route calendars and mode rules are injected by the mode adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import json
from typing import Iterable, Mapping, Protocol, TypeAlias

from .audition_rules import FINAL
from .reward_card_semantics import (
    filter_excluded_reward_card_candidates,
    normalize_excluded_reward_card_ids,
)
from .route_calendar import CONSULTATION, RouteCalendar


SCHEMA_NAME = "gkms_tool.produce_rollout_state"
SCHEMA_VERSION = 1


class RolloutPhase(StrEnum):
    READY_FOR_WEEK = "ready_for_week"
    WAITING_EXTERNAL = "waiting_external"
    READY_FOR_REWARD = "ready_for_reward"
    TERMINAL = "terminal"


class Lifecycle(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ActionKind(StrEnum):
    WEEKLY_ACTION = "weekly_action"
    REST = "rest"
    DELEGATE_INNER_EXAM = "delegate_inner_exam"
    SELECT_REWARD = "select_reward"
    EXCLUDE_REWARD = "exclude_reward"


class ExternalKind(StrEnum):
    WEEKLY_ACTION_OUTCOME = "weekly_action_outcome"
    INNER_EXAM_OUTCOME = "inner_exam_outcome"
    REWARD_OFFERS = "reward_offers"
    REWARD_REPLACEMENT = "reward_replacement"


class RolloutStop(StrEnum):
    EXTERNAL = "external"
    TERMINAL = "terminal"
    POLICY = "policy"
    STEP_LIMIT = "step_limit"


@dataclass(frozen=True, slots=True, order=True)
class DeckEntry:
    """Identity-preserving deck row.

    ``instance_ids`` is optional because an outer shadow often has only
    card-id/upgrade/count identity.  Native inner states may keep GUIDs here.
    """

    card_id: str
    upgrade: int = 0
    count: int = 1
    instance_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.card_id, str) or not self.card_id:
            raise ValueError("deck card_id must be non-empty text")
        if self.upgrade < 0 or self.count < 1:
            raise ValueError("deck upgrade/count is invalid")
        if self.instance_ids and len(self.instance_ids) != self.count:
            raise ValueError("deck instance_ids must be empty or match count")
        if any(not value for value in self.instance_ids):
            raise ValueError("deck instance IDs must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "count": self.count,
            "instance_ids": list(self.instance_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "DeckEntry":
        raw_instances = payload.get("instance_ids", [])
        if not isinstance(raw_instances, list):
            raise ValueError("deck instance_ids must be a list")
        return cls(
            card_id=_text(payload.get("card_id"), "deck.card_id"),
            upgrade=_integer(payload.get("upgrade"), "deck.upgrade"),
            count=_integer(payload.get("count"), "deck.count", minimum=1),
            instance_ids=tuple(_text(value, "deck.instance_id") for value in raw_instances),
        )


@dataclass(frozen=True, slots=True)
class AttributeValues:
    vocal: int
    dance: int
    visual: int

    def __post_init__(self) -> None:
        if min(self.vocal, self.dance, self.visual) < 0:
            raise ValueError("attributes cannot be negative")

    def to_dict(self) -> dict[str, int]:
        return {"vocal": self.vocal, "dance": self.dance, "visual": self.visual}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AttributeValues":
        return cls(
            vocal=_integer(payload.get("vocal"), "attributes.vocal"),
            dance=_integer(payload.get("dance"), "attributes.dance"),
            visual=_integer(payload.get("visual"), "attributes.visual"),
        )


@dataclass(frozen=True, slots=True)
class RewardOffer:
    offer_id: str
    card_id: str
    upgrade: int = 0
    received_card_instance_id: str | None = None

    def __post_init__(self) -> None:
        if not self.offer_id or not self.card_id or self.upgrade < 0:
            raise ValueError("reward offer identity is invalid")
        if (
            self.received_card_instance_id is not None
            and not self.received_card_instance_id
        ):
            raise ValueError("received card instance ID cannot be empty")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "offer_id": self.offer_id,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
        }
        if self.received_card_instance_id is not None:
            payload["received_card_instance_id"] = (
                self.received_card_instance_id
            )
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RewardOffer":
        return cls(
            offer_id=_text(payload.get("offer_id"), "reward.offer_id"),
            card_id=_text(payload.get("card_id"), "reward.card_id"),
            upgrade=_integer(payload.get("upgrade"), "reward.upgrade"),
            received_card_instance_id=_optional_text(
                payload.get("received_card_instance_id"),
                "reward.received_card_instance_id",
            ),
        )


@dataclass(frozen=True, slots=True)
class ChanceBranch:
    """One externally supplied branch; probability remains unknown if absent."""

    branch_id: str
    probability: float | None = None
    rng_token: str | None = None
    event_id: str | None = None
    was_sp: bool | None = None

    def __post_init__(self) -> None:
        if not self.branch_id:
            raise ValueError("chance branch_id is required")
        if self.probability is not None and not 0.0 <= self.probability <= 1.0:
            raise ValueError("chance probability must be in 0..1")
        for name, value in (("rng_token", self.rng_token), ("event_id", self.event_id)):
            if value is not None and not value:
                raise ValueError(f"{name} cannot be empty")
        if self.was_sp is not None and not isinstance(self.was_sp, bool):
            raise ValueError("was_sp must be boolean or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "branch_id": self.branch_id,
            "probability": self.probability,
            "rng_token": self.rng_token,
            "event_id": self.event_id,
            "was_sp": self.was_sp,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ChanceBranch":
        probability = payload.get("probability")
        if probability is not None and (
            isinstance(probability, bool) or not isinstance(probability, (int, float))
        ):
            raise ValueError("chance probability must be numeric or None")
        was_sp = payload.get("was_sp")
        if was_sp is not None and not isinstance(was_sp, bool):
            raise ValueError("chance was_sp must be boolean or None")
        return cls(
            branch_id=_text(payload.get("branch_id"), "chance.branch_id"),
            probability=None if probability is None else float(probability),
            rng_token=_optional_text(payload.get("rng_token"), "chance.rng_token"),
            event_id=_optional_text(payload.get("event_id"), "chance.event_id"),
            was_sp=was_sp,
        )


@dataclass(frozen=True, slots=True)
class ChanceHistoryEntry:
    request_id: str
    kind: ExternalKind
    week: int
    branch: ChanceBranch

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "kind": self.kind.value,
            "week": self.week,
            "branch": self.branch.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ChanceHistoryEntry":
        return cls(
            request_id=_text(payload.get("request_id"), "history.request_id"),
            kind=_external_kind(payload.get("kind")),
            week=_integer(payload.get("week"), "history.week", minimum=1),
            branch=ChanceBranch.from_dict(_mapping(payload.get("branch"), "history.branch")),
        )


@dataclass(frozen=True, slots=True)
class ExternalRequest:
    """Typed pause node for data not owned by the static simulator."""

    request_id: str
    kind: ExternalKind
    week: int
    action_id: str
    stage_type: str | None = None
    adapter_ref: str | None = None
    required_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id or self.week < 1 or not self.action_id:
            raise ValueError("external request identity is invalid")
        if self.adapter_ref is not None and not self.adapter_ref:
            raise ValueError("external adapter_ref cannot be empty")
        if any(not value for value in self.required_fields):
            raise ValueError("external required_fields cannot contain empty values")

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "kind": self.kind.value,
            "week": self.week,
            "action_id": self.action_id,
            "stage_type": self.stage_type,
            "adapter_ref": self.adapter_ref,
            "required_fields": list(self.required_fields),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ExternalRequest":
        fields = payload.get("required_fields", [])
        if not isinstance(fields, list):
            raise ValueError("external required_fields must be a list")
        return cls(
            request_id=_text(payload.get("request_id"), "external.request_id"),
            kind=_external_kind(payload.get("kind")),
            week=_integer(payload.get("week"), "external.week", minimum=1),
            action_id=_text(payload.get("action_id"), "external.action_id"),
            stage_type=_optional_text(payload.get("stage_type"), "external.stage_type"),
            adapter_ref=_optional_text(payload.get("adapter_ref"), "external.adapter_ref"),
            required_fields=tuple(_text(value, "external.required_field") for value in fields),
        )


@dataclass(frozen=True, slots=True)
class ExternalContinuation:
    """One exact same-week continuation requested after an outcome settles.

    This is a request *specification*, not an already pending request.  The
    kernel owns the request identity so chained event/lesson nodes remain in
    the same chance history without callers fabricating serials.
    """

    kind: ExternalKind
    action_id: str
    stage_type: str | None = None
    adapter_ref: str | None = None
    required_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in {
            ExternalKind.WEEKLY_ACTION_OUTCOME,
            ExternalKind.INNER_EXAM_OUTCOME,
        }:
            raise ValueError("continuation must be a weekly or inner-stage request")
        if not self.action_id:
            raise ValueError("continuation action_id is required")
        if self.stage_type is not None and not self.stage_type:
            raise ValueError("continuation stage_type cannot be empty")
        if self.adapter_ref is not None and not self.adapter_ref:
            raise ValueError("continuation adapter_ref cannot be empty")
        if any(not value for value in self.required_fields):
            raise ValueError("continuation required_fields cannot contain empty values")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "action_id": self.action_id,
            "stage_type": self.stage_type,
            "adapter_ref": self.adapter_ref,
            "required_fields": list(self.required_fields),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ExternalContinuation":
        fields = payload.get("required_fields", [])
        if not isinstance(fields, list):
            raise ValueError("continuation required_fields must be a list")
        return cls(
            kind=_external_kind(payload.get("kind")),
            action_id=_text(payload.get("action_id"), "continuation.action_id"),
            stage_type=_optional_text(
                payload.get("stage_type"), "continuation.stage_type"
            ),
            adapter_ref=_optional_text(
                payload.get("adapter_ref"), "continuation.adapter_ref"
            ),
            required_fields=tuple(
                _text(value, "continuation.required_field") for value in fields
            ),
        )


@dataclass(frozen=True, slots=True)
class RewardContext:
    source_action_id: str
    terminal_after_reward: bool
    cleared: bool | None = None
    continuation: ExternalContinuation | None = None

    def __post_init__(self) -> None:
        if not self.source_action_id:
            raise ValueError("reward context source_action_id is required")
        if not isinstance(self.terminal_after_reward, bool) or (
            self.cleared is not None and not isinstance(self.cleared, bool)
        ):
            raise ValueError("reward context booleans are invalid")
        if self.continuation is not None and not isinstance(
            self.continuation, ExternalContinuation
        ):
            raise TypeError("reward context continuation is invalid")
        if self.terminal_after_reward and self.continuation is not None:
            raise ValueError("terminal reward context cannot also continue")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_action_id": self.source_action_id,
            "terminal_after_reward": self.terminal_after_reward,
            "cleared": self.cleared,
            "continuation": (
                None if self.continuation is None else self.continuation.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RewardContext":
        terminal = payload.get("terminal_after_reward")
        cleared = payload.get("cleared")
        if not isinstance(terminal, bool) or (
            cleared is not None and not isinstance(cleared, bool)
        ):
            raise ValueError("reward context booleans are invalid")
        return cls(
            source_action_id=_text(payload.get("source_action_id"), "reward_context.action"),
            terminal_after_reward=terminal,
            cleared=cleared,
            continuation=(
                None
                if payload.get("continuation") is None
                else ExternalContinuation.from_dict(
                    _mapping(payload.get("continuation"), "reward_context.continuation")
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ProduceStatePatch:
    """Exact post-outcome values.  Omitted fields explicitly remain unchanged."""

    stamina: int | None = None
    max_stamina: int | None = None
    produce_points: int | None = None
    attributes: AttributeValues | None = None
    deck: tuple[DeckEntry, ...] | None = None
    item_session_refs: tuple[str, ...] | None = None
    drink_session_refs: tuple[str, ...] | None = None
    passive_session_refs: tuple[str, ...] | None = None
    excluded_reward_card_ids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("stamina", self.stamina),
            ("max_stamina", self.max_stamina),
            ("produce_points", self.produce_points),
        ):
            if value is not None and value < (1 if name == "max_stamina" else 0):
                raise ValueError(f"patch {name} is invalid")
        for name, values in (
            ("item_session_refs", self.item_session_refs),
            ("passive_session_refs", self.passive_session_refs),
        ):
            if values is not None and (any(not value for value in values) or len(set(values)) != len(values)):
                raise ValueError(f"patch {name} must contain unique non-empty values")
        if self.drink_session_refs is not None and any(
            not value for value in self.drink_session_refs
        ):
            raise ValueError(
                "patch drink_session_refs must contain non-empty ordered values"
            )
        if self.excluded_reward_card_ids is not None:
            normalized = normalize_excluded_reward_card_ids(
                self.excluded_reward_card_ids
            )
            if len(normalized) != len(self.excluded_reward_card_ids):
                raise ValueError(
                    "patch excluded_reward_card_ids must contain unique card IDs"
                )


@dataclass(frozen=True, slots=True)
class ProduceAction:
    kind: ActionKind
    choice_id: str
    offer_index: int | None = None

    @classmethod
    def weekly(cls, action_id: str) -> "ProduceAction":
        return cls(ActionKind.WEEKLY_ACTION, action_id)

    @classmethod
    def rest(cls) -> "ProduceAction":
        return cls(ActionKind.REST, "rest")

    @classmethod
    def delegate_exam(cls, stage_type: str) -> "ProduceAction":
        return cls(ActionKind.DELEGATE_INNER_EXAM, stage_type)

    @classmethod
    def select_reward(cls, index: int, offer_id: str) -> "ProduceAction":
        return cls(ActionKind.SELECT_REWARD, offer_id, index)

    @classmethod
    def exclude_reward(cls, index: int, offer_id: str) -> "ProduceAction":
        return cls(ActionKind.EXCLUDE_REWARD, offer_id, index)


@dataclass(frozen=True, slots=True)
class WeeklyActionOutcome:
    request_id: str
    branch: ChanceBranch
    patch: ProduceStatePatch = ProduceStatePatch()
    reward_requested: bool = False
    continuation: ExternalContinuation | None = None

    def __post_init__(self) -> None:
        if not self.request_id or not isinstance(self.reward_requested, bool):
            raise ValueError("weekly outcome identity/flags are invalid")
        if self.continuation is not None and not isinstance(
            self.continuation, ExternalContinuation
        ):
            raise TypeError("weekly outcome continuation is invalid")


@dataclass(frozen=True, slots=True)
class InnerExamOutcome:
    request_id: str
    branch: ChanceBranch
    cleared: bool
    terminal: bool
    patch: ProduceStatePatch = ProduceStatePatch()
    reward_requested: bool = False

    def __post_init__(self) -> None:
        if not self.request_id or not all(
            isinstance(value, bool)
            for value in (self.cleared, self.terminal, self.reward_requested)
        ):
            raise ValueError("inner exam outcome identity/flags are invalid")


@dataclass(frozen=True, slots=True)
class RewardOffersOutcome:
    request_id: str
    branch: ChanceBranch
    offers: tuple[RewardOffer, ...]
    remaining_exclude_count: int = 0

    def __post_init__(self) -> None:
        if not self.request_id or not self.offers:
            raise ValueError("reward offers cannot be empty")
        if self.remaining_exclude_count < 0:
            raise ValueError("remaining reward excludes cannot be negative")
        offer_ids = [offer.offer_id for offer in self.offers]
        if len(set(offer_ids)) != len(offer_ids):
            raise ValueError("reward offer IDs must be unique")


@dataclass(frozen=True, slots=True)
class RewardReplacementOutcome:
    request_id: str
    branch: ChanceBranch
    replacement: RewardOffer
    remaining_exclude_count_after: int

    def __post_init__(self) -> None:
        if not self.request_id or self.remaining_exclude_count_after < 0:
            raise ValueError("remaining reward excludes cannot be negative")


ExternalOutcome: TypeAlias = (
    WeeklyActionOutcome
    | InnerExamOutcome
    | RewardOffersOutcome
    | RewardReplacementOutcome
)


@dataclass(frozen=True, slots=True)
class ProduceRolloutState:
    mode_id: str
    character_id: str
    week: int
    total_weeks: int
    phase: RolloutPhase
    stamina: int
    max_stamina: int
    attributes: AttributeValues
    deck: tuple[DeckEntry, ...]
    produce_points: int = 0
    item_session_refs: tuple[str, ...] = ()
    drink_session_refs: tuple[str, ...] = ()
    passive_session_refs: tuple[str, ...] = ()
    excluded_reward_card_ids: tuple[str, ...] = ()
    chance_history: tuple[ChanceHistoryEntry, ...] = ()
    lifecycle: Lifecycle = Lifecycle.IN_PROGRESS
    pending: ExternalRequest | None = None
    reward_context: RewardContext | None = None
    reward_offers: tuple[RewardOffer, ...] = ()
    remaining_reward_excludes: int = 0

    def __post_init__(self) -> None:
        self.validate()

    @property
    def terminal(self) -> bool:
        return self.phase == RolloutPhase.TERMINAL

    @property
    def mode(self) -> str:
        """Stable mode alias used by generic search code."""

        return self.mode_id

    @property
    def excluded_reward_ids(self) -> tuple[str, ...]:
        """Compatibility alias; new consumers should use the canonical name."""

        return self.excluded_reward_card_ids

    def validate(self) -> None:
        if not self.mode_id or not self.character_id:
            raise ValueError("rollout mode/character identity is incomplete")
        if self.total_weeks < 1 or not 1 <= self.week <= self.total_weeks:
            raise ValueError("rollout week is outside the route")
        if self.max_stamina < 1 or not 0 <= self.stamina <= self.max_stamina:
            raise ValueError("rollout stamina is outside 0..max_stamina")
        if self.produce_points < 0:
            raise ValueError("rollout produce_points cannot be negative")
        for name, values in (
            ("item_session_refs", self.item_session_refs),
            ("passive_session_refs", self.passive_session_refs),
        ):
            if any(not value for value in values) or len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique non-empty values")
        if any(not value for value in self.drink_session_refs):
            raise ValueError(
                "drink_session_refs must contain non-empty ordered values"
            )
        normalized_exclusions = normalize_excluded_reward_card_ids(
            self.excluded_reward_card_ids
        )
        if len(normalized_exclusions) != len(self.excluded_reward_card_ids):
            raise ValueError(
                "excluded_reward_card_ids must contain unique canonical card IDs"
            )
        if self.remaining_reward_excludes < 0:
            raise ValueError("remaining reward excludes cannot be negative")
        if self.phase == RolloutPhase.WAITING_EXTERNAL and self.pending is None:
            raise ValueError("waiting state requires a pending external request")
        if self.phase != RolloutPhase.WAITING_EXTERNAL and self.pending is not None:
            raise ValueError("only a waiting state may have a pending request")
        awaiting_replacement = (
            self.phase == RolloutPhase.WAITING_EXTERNAL
            and self.pending is not None
            and self.pending.kind == ExternalKind.REWARD_REPLACEMENT
        )
        if self.phase == RolloutPhase.READY_FOR_REWARD:
            if not self.reward_offers or self.reward_context is None:
                raise ValueError("reward phase requires offers and context")
        elif awaiting_replacement:
            if not self.reward_offers or self.reward_context is None:
                raise ValueError("reward replacement requires offers and context")
        elif self.reward_offers:
            raise ValueError("reward offers may exist only in reward/replacement phase")
        offer_ids = [offer.offer_id for offer in self.reward_offers]
        if len(set(offer_ids)) != len(offer_ids):
            raise ValueError("reward offer IDs must be unique")
        if self.lifecycle == Lifecycle.IN_PROGRESS and self.terminal:
            raise ValueError("terminal state must have a terminal lifecycle")
        if self.lifecycle != Lifecycle.IN_PROGRESS and not self.terminal:
            raise ValueError("terminal lifecycle requires terminal phase")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode_id,
            "mode_id": self.mode_id,
            "character_id": self.character_id,
            "week": self.week,
            "total_weeks": self.total_weeks,
            "phase": self.phase.value,
            "stamina": self.stamina,
            "max_stamina": self.max_stamina,
            "produce_points": self.produce_points,
            "attributes": self.attributes.to_dict(),
            "deck": [entry.to_dict() for entry in self.deck],
            "item_session_refs": list(self.item_session_refs),
            "drink_session_refs": list(self.drink_session_refs),
            "passive_session_refs": list(self.passive_session_refs),
            "excluded_reward_card_ids": list(self.excluded_reward_card_ids),
            "chance_history": [entry.to_dict() for entry in self.chance_history],
            "lifecycle": self.lifecycle.value,
            "pending": None if self.pending is None else self.pending.to_dict(),
            "reward_context": (
                None if self.reward_context is None else self.reward_context.to_dict()
            ),
            "reward_offers": [offer.to_dict() for offer in self.reward_offers],
            "remaining_reward_excludes": self.remaining_reward_excludes,
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, indent=indent)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ProduceRolloutState":
        if payload.get("schema") != SCHEMA_NAME or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported produce rollout state schema")
        deck = _object_list(payload.get("deck"), "deck")
        history = _object_list(payload.get("chance_history"), "chance_history")
        offers = _object_list(payload.get("reward_offers"), "reward_offers")
        pending_raw = payload.get("pending")
        context_raw = payload.get("reward_context")
        mode_id = _text(payload.get("mode_id"), "mode_id")
        if payload.get("mode") not in {None, mode_id}:
            raise ValueError("mode and mode_id disagree")
        return cls(
            mode_id=mode_id,
            character_id=_text(payload.get("character_id"), "character_id"),
            week=_integer(payload.get("week"), "week", minimum=1),
            total_weeks=_integer(payload.get("total_weeks"), "total_weeks", minimum=1),
            phase=_rollout_phase(payload.get("phase")),
            stamina=_integer(payload.get("stamina"), "stamina"),
            max_stamina=_integer(payload.get("max_stamina"), "max_stamina", minimum=1),
            attributes=AttributeValues.from_dict(_mapping(payload.get("attributes"), "attributes")),
            deck=tuple(DeckEntry.from_dict(value) for value in deck),
            produce_points=_integer(payload.get("produce_points", 0), "produce_points"),
            item_session_refs=_text_list(payload.get("item_session_refs"), "item_session_refs"),
            drink_session_refs=_text_list(payload.get("drink_session_refs"), "drink_session_refs"),
            passive_session_refs=_text_list(payload.get("passive_session_refs"), "passive_session_refs"),
            excluded_reward_card_ids=_text_list(
                payload.get(
                    "excluded_reward_card_ids",
                    payload.get("excluded_reward_ids"),
                ),
                "excluded_reward_card_ids",
            ),
            chance_history=tuple(ChanceHistoryEntry.from_dict(value) for value in history),
            lifecycle=_lifecycle(payload.get("lifecycle")),
            pending=(
                None
                if pending_raw is None
                else ExternalRequest.from_dict(_mapping(pending_raw, "pending"))
            ),
            reward_context=(
                None
                if context_raw is None
                else RewardContext.from_dict(_mapping(context_raw, "reward_context"))
            ),
            reward_offers=tuple(RewardOffer.from_dict(value) for value in offers),
            remaining_reward_excludes=_integer(
                payload.get("remaining_reward_excludes"),
                "remaining_reward_excludes",
            ),
        )

    @classmethod
    def from_json(cls, value: str) -> "ProduceRolloutState":
        payload = json.loads(value)
        return cls.from_dict(_mapping(payload, "rollout state"))


@dataclass(frozen=True, slots=True)
class TransitionResult:
    state: ProduceRolloutState
    request: ExternalRequest | None = None


@dataclass(frozen=True, slots=True)
class RolloutResult:
    state: ProduceRolloutState
    stop: RolloutStop
    request: ExternalRequest | None
    actions_applied: tuple[ProduceAction, ...]


class RolloutPolicy(Protocol):
    def __call__(
        self,
        state: ProduceRolloutState,
        legal_actions: tuple[ProduceAction, ...],
    ) -> ProduceAction | None: ...


class InnerExamAdapter(Protocol):
    """Contract implemented by a Plan3 native-state/search integration layer."""

    def __call__(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
    ) -> InnerExamOutcome: ...


class ProduceRolloutKernel:
    """Static-route transition kernel shared by Initial and future NIA modes."""

    def __init__(
        self,
        calendar: RouteCalendar,
        *,
        rest_recovery_permille: int,
        skipped_route_actions: Iterable[str] = (CONSULTATION, "shop"),
        inner_exam_adapter_ref: str = "gkms_tool.plan3_native_search.search_plan3_native",
    ) -> None:
        if not 0 <= rest_recovery_permille <= 1000:
            raise ValueError("rest recovery must be in 0..1000 permille")
        if not inner_exam_adapter_ref:
            raise ValueError("inner exam adapter ref is required")
        self.calendar = calendar
        self.rest_recovery_permille = rest_recovery_permille
        self.skipped_route_actions = frozenset(skipped_route_actions)
        self.inner_exam_adapter_ref = inner_exam_adapter_ref

    def validate_state(self, state: ProduceRolloutState) -> None:
        state.validate()
        if state.mode_id != self.calendar.produce_id:
            raise ValueError("state mode does not match rollout calendar")
        if state.total_weeks != self.calendar.total_weeks:
            raise ValueError("state total_weeks does not match rollout calendar")

    def legal_actions(self, state: ProduceRolloutState) -> tuple[ProduceAction, ...]:
        self.validate_state(state)
        if state.terminal or state.phase == RolloutPhase.WAITING_EXTERNAL:
            return ()
        if state.phase == RolloutPhase.READY_FOR_REWARD:
            result: list[ProduceAction] = []
            for index, offer in enumerate(state.reward_offers):
                result.append(ProduceAction.select_reward(index, offer.offer_id))
                if state.remaining_reward_excludes > 0:
                    result.append(ProduceAction.exclude_reward(index, offer.offer_id))
            return tuple(result)
        route_week = self.calendar.week(state.week)
        if route_week.stage_type is not None:
            return (ProduceAction.delegate_exam(route_week.stage_type),)
        route_actions = tuple(
            ProduceAction.weekly(action_id)
            for action_id in route_week.actions
            if action_id not in self.skipped_route_actions
        )
        # Rest is a visible recovery alternative in Regular, independent of
        # the character calendar's primary tiles.
        return (*route_actions, ProduceAction.rest())

    def apply_action(
        self, state: ProduceRolloutState, action: ProduceAction
    ) -> TransitionResult:
        legal = self.legal_actions(state)
        if action not in legal:
            raise ValueError(f"illegal rollout action: {action}")
        if action.kind == ActionKind.REST:
            recovery = state.max_stamina * self.rest_recovery_permille // 1000
            rested = replace(state, stamina=min(state.max_stamina, state.stamina + recovery))
            return TransitionResult(self._complete_week(rested))
        if action.kind == ActionKind.WEEKLY_ACTION:
            request = self._request(
                state,
                ExternalKind.WEEKLY_ACTION_OUTCOME,
                action.choice_id,
                required_fields=(
                    "branch_id",
                    "exact_post_action_state_patch",
                    "reward_requested",
                ),
            )
            return TransitionResult(
                replace(state, phase=RolloutPhase.WAITING_EXTERNAL, pending=request),
                request,
            )
        if action.kind == ActionKind.DELEGATE_INNER_EXAM:
            request = self._request(
                state,
                ExternalKind.INNER_EXAM_OUTCOME,
                action.choice_id,
                stage_type=action.choice_id,
                adapter_ref=self.inner_exam_adapter_ref,
                required_fields=(
                    "Plan3NativeState",
                    "search_plan3_native result",
                    "cleared",
                    "terminal",
                    "exact_post_exam_state_patch",
                    "reward_requested",
                ),
            )
            return TransitionResult(
                replace(state, phase=RolloutPhase.WAITING_EXTERNAL, pending=request),
                request,
            )
        if action.kind == ActionKind.SELECT_REWARD:
            index, offer = self._selected_offer(state, action)
            del index
            deck = _add_deck_entry(
                state.deck,
                offer.card_id,
                offer.upgrade,
                instance_id=offer.received_card_instance_id,
            )
            selected = replace(
                state,
                deck=deck,
                phase=RolloutPhase.READY_FOR_WEEK,
                reward_offers=(),
                remaining_reward_excludes=0,
            )
            return self._complete_reward(selected)
        if action.kind == ActionKind.EXCLUDE_REWARD:
            index, offer = self._selected_offer(state, action)
            request = self._request(
                state,
                ExternalKind.REWARD_REPLACEMENT,
                offer.offer_id,
                required_fields=(
                    "branch_id",
                    "replacement_offer",
                    "remaining_exclude_count_after",
                ),
            )
            # Keep the old offer and exclusion count until the replacement is
            # externally confirmed.  No optimistic mutation enters the state.
            return TransitionResult(
                replace(
                    state,
                    phase=RolloutPhase.WAITING_EXTERNAL,
                    pending=request,
                    remaining_reward_excludes=state.remaining_reward_excludes,
                    reward_context=state.reward_context,
                    reward_offers=state.reward_offers,
                ),
                request,
            )
        raise AssertionError(f"unhandled rollout action: {action.kind}")

    def resolve_external(
        self, state: ProduceRolloutState, outcome: ExternalOutcome
    ) -> TransitionResult:
        self.validate_state(state)
        request = state.pending
        if state.phase != RolloutPhase.WAITING_EXTERNAL or request is None:
            raise ValueError("state is not waiting for an external outcome")
        if outcome.request_id != request.request_id:
            raise ValueError("outcome does not match pending request")
        expected = {
            WeeklyActionOutcome: ExternalKind.WEEKLY_ACTION_OUTCOME,
            InnerExamOutcome: ExternalKind.INNER_EXAM_OUTCOME,
            RewardOffersOutcome: ExternalKind.REWARD_OFFERS,
            RewardReplacementOutcome: ExternalKind.REWARD_REPLACEMENT,
        }.get(type(outcome))
        if expected is None or request.kind != expected:
            raise ValueError("outcome type does not match pending request kind")
        history = (
            *state.chance_history,
            ChanceHistoryEntry(request.request_id, request.kind, state.week, outcome.branch),
        )
        # Move through a valid non-waiting phase while dispatching the typed
        # outcome.  Reward replacement keeps the existing offer set; all
        # other pending nodes return to the week phase before their specific
        # continuation is applied.
        resolved_phase = (
            RolloutPhase.READY_FOR_REWARD
            if request.kind == ExternalKind.REWARD_REPLACEMENT
            else RolloutPhase.READY_FOR_WEEK
        )
        base = replace(
            state,
            phase=resolved_phase,
            pending=None,
            chance_history=history,
        )
        if isinstance(outcome, WeeklyActionOutcome):
            updated = self._apply_patch(base, outcome.patch)
            if outcome.reward_requested:
                return self._request_rewards(
                    updated,
                    RewardContext(
                        request.action_id,
                        terminal_after_reward=False,
                        continuation=outcome.continuation,
                    ),
                )
            if outcome.continuation is not None:
                return self._request_continuation(updated, outcome.continuation)
            return TransitionResult(self._complete_week(updated))
        if isinstance(outcome, InnerExamOutcome):
            updated = self._apply_patch(base, outcome.patch)
            final_boundary = self.calendar.week(state.week).stage_type == FINAL
            terminal = outcome.terminal or final_boundary
            if outcome.reward_requested:
                return self._request_rewards(
                    updated,
                    RewardContext(
                        request.action_id,
                        terminal_after_reward=terminal,
                        cleared=outcome.cleared,
                    ),
                )
            if terminal:
                return TransitionResult(self._finish(updated, cleared=outcome.cleared))
            return TransitionResult(self._complete_week(updated))
        if isinstance(outcome, RewardOffersOutcome):
            filtered = filter_excluded_reward_card_candidates(
                outcome.offers,
                state.excluded_reward_card_ids,
            )
            if len(filtered) != len(outcome.offers):
                raise ValueError("externally offered reward is already excluded")
            return TransitionResult(
                replace(
                    base,
                    phase=RolloutPhase.READY_FOR_REWARD,
                    reward_offers=outcome.offers,
                    remaining_reward_excludes=outcome.remaining_exclude_count,
                )
            )
        if isinstance(outcome, RewardReplacementOutcome):
            old_offers = state.reward_offers
            old_index = next(
                (index for index, offer in enumerate(old_offers) if offer.offer_id == request.action_id),
                None,
            )
            if old_index is None:
                raise ValueError("excluded reward no longer exists in the pending offer list")
            excluded_card_id = old_offers[old_index].card_id
            if outcome.replacement.card_id == excluded_card_id:
                raise ValueError("replacement reward must differ from excluded reward")
            if outcome.replacement.card_id in normalize_excluded_reward_card_ids(
                state.excluded_reward_card_ids
            ):
                raise ValueError("replacement reward is already excluded")
            if outcome.remaining_exclude_count_after != state.remaining_reward_excludes - 1:
                raise ValueError("replacement exclusion count is not an exact decrement")
            offers = list(old_offers)
            offers[old_index] = outcome.replacement
            excluded = (*state.excluded_reward_card_ids, excluded_card_id)
            return TransitionResult(
                replace(
                    base,
                    phase=RolloutPhase.READY_FOR_REWARD,
                    reward_offers=tuple(offers),
                    excluded_reward_card_ids=excluded,
                    remaining_reward_excludes=outcome.remaining_exclude_count_after,
                )
            )
        raise AssertionError("unhandled external outcome")

    def rollout(
        self,
        state: ProduceRolloutState,
        policy: RolloutPolicy,
        *,
        max_actions: int = 100,
    ) -> RolloutResult:
        """Apply policy choices until terminal or the first external boundary."""

        if max_actions < 1:
            raise ValueError("max_actions must be positive")
        self.validate_state(state)
        applied: list[ProduceAction] = []
        current = state
        for _ in range(max_actions):
            if current.terminal:
                return RolloutResult(current, RolloutStop.TERMINAL, None, tuple(applied))
            if current.pending is not None:
                return RolloutResult(
                    current, RolloutStop.EXTERNAL, current.pending, tuple(applied)
                )
            legal = self.legal_actions(current)
            action = policy(current, legal)
            if action is None:
                return RolloutResult(current, RolloutStop.POLICY, None, tuple(applied))
            result = self.apply_action(current, action)
            current = result.state
            applied.append(action)
            if result.request is not None:
                return RolloutResult(
                    current, RolloutStop.EXTERNAL, result.request, tuple(applied)
                )
        return RolloutResult(current, RolloutStop.STEP_LIMIT, current.pending, tuple(applied))

    def resolve_with_inner_adapter(
        self,
        state: ProduceRolloutState,
        adapter: InnerExamAdapter,
    ) -> TransitionResult:
        """Invoke an injected inner simulator only at an inner-exam node."""

        request = state.pending
        if request is None or request.kind != ExternalKind.INNER_EXAM_OUTCOME:
            raise ValueError("state has no pending inner-exam request")
        return self.resolve_external(state, adapter(state, request))

    def _request(
        self,
        state: ProduceRolloutState,
        kind: ExternalKind,
        action_id: str,
        *,
        stage_type: str | None = None,
        adapter_ref: str | None = None,
        required_fields: tuple[str, ...] = (),
    ) -> ExternalRequest:
        serial = len(state.chance_history) + 1
        return ExternalRequest(
            request_id=f"w{state.week}:{kind.value}:{serial}",
            kind=kind,
            week=state.week,
            action_id=action_id,
            stage_type=stage_type,
            adapter_ref=adapter_ref,
            required_fields=required_fields,
        )

    def _request_rewards(
        self, state: ProduceRolloutState, context: RewardContext
    ) -> TransitionResult:
        request = self._request(
            state,
            ExternalKind.REWARD_OFFERS,
            context.source_action_id,
            required_fields=(
                "branch_id",
                "server_offer_ids",
                "remaining_exclude_count",
            ),
        )
        return TransitionResult(
            replace(
                state,
                phase=RolloutPhase.WAITING_EXTERNAL,
                pending=request,
                reward_context=context,
            ),
            request,
        )

    def _request_continuation(
        self,
        state: ProduceRolloutState,
        continuation: ExternalContinuation,
    ) -> TransitionResult:
        request = self._request(
            state,
            continuation.kind,
            continuation.action_id,
            stage_type=continuation.stage_type,
            adapter_ref=continuation.adapter_ref,
            required_fields=continuation.required_fields,
        )
        return TransitionResult(
            replace(
                state,
                phase=RolloutPhase.WAITING_EXTERNAL,
                pending=request,
                reward_context=None,
                reward_offers=(),
                remaining_reward_excludes=0,
            ),
            request,
        )

    def _complete_reward(self, state: ProduceRolloutState) -> TransitionResult:
        context = state.reward_context
        if context is None:
            raise ValueError("reward completion requires context")
        cleared = True if context.cleared is None else context.cleared
        updated = replace(state, reward_context=None)
        if context.terminal_after_reward:
            return TransitionResult(self._finish(updated, cleared=cleared))
        if context.continuation is not None:
            return self._request_continuation(updated, context.continuation)
        return TransitionResult(self._complete_week(updated))

    def _complete_week(self, state: ProduceRolloutState) -> ProduceRolloutState:
        if state.week >= state.total_weeks:
            raise ValueError("final route week can end only through its audition boundary")
        return replace(
            state,
            week=state.week + 1,
            phase=RolloutPhase.READY_FOR_WEEK,
            pending=None,
            reward_context=None,
            reward_offers=(),
            remaining_reward_excludes=0,
        )

    @staticmethod
    def _finish(state: ProduceRolloutState, *, cleared: bool) -> ProduceRolloutState:
        return replace(
            state,
            phase=RolloutPhase.TERMINAL,
            lifecycle=Lifecycle.COMPLETED if cleared else Lifecycle.FAILED,
            pending=None,
            reward_context=None,
            reward_offers=(),
            remaining_reward_excludes=0,
        )

    @staticmethod
    def _selected_offer(
        state: ProduceRolloutState, action: ProduceAction
    ) -> tuple[int, RewardOffer]:
        if action.offer_index is None or not 0 <= action.offer_index < len(state.reward_offers):
            raise ValueError("reward action index is invalid")
        offer = state.reward_offers[action.offer_index]
        if offer.offer_id != action.choice_id:
            raise ValueError("reward action identity no longer matches its index")
        return action.offer_index, offer

    @staticmethod
    def _apply_patch(
        state: ProduceRolloutState, patch: ProduceStatePatch
    ) -> ProduceRolloutState:
        maximum = state.max_stamina if patch.max_stamina is None else patch.max_stamina
        stamina = state.stamina if patch.stamina is None else patch.stamina
        assert maximum is not None
        if stamina > maximum:
            raise ValueError("external patch stamina exceeds max_stamina")
        return replace(
            state,
            stamina=stamina,
            max_stamina=maximum,
            produce_points=(
                state.produce_points
                if patch.produce_points is None
                else patch.produce_points
            ),
            attributes=state.attributes if patch.attributes is None else patch.attributes,
            deck=state.deck if patch.deck is None else patch.deck,
            item_session_refs=(
                state.item_session_refs
                if patch.item_session_refs is None
                else patch.item_session_refs
            ),
            drink_session_refs=(
                state.drink_session_refs
                if patch.drink_session_refs is None
                else patch.drink_session_refs
            ),
            passive_session_refs=(
                state.passive_session_refs
                if patch.passive_session_refs is None
                else patch.passive_session_refs
            ),
            excluded_reward_card_ids=(
                state.excluded_reward_card_ids
                if patch.excluded_reward_card_ids is None
                else patch.excluded_reward_card_ids
            ),
        )


def _add_deck_entry(
    deck: tuple[DeckEntry, ...],
    card_id: str,
    upgrade: int,
    *,
    instance_id: str | None = None,
) -> tuple[DeckEntry, ...]:
    result = list(deck)
    existing_ids = {
        value
        for entry in result
        for value in entry.instance_ids
    }
    if instance_id is not None and instance_id in existing_ids:
        raise ValueError("reward card instance ID already exists in deck")
    for index, entry in enumerate(result):
        if entry.card_id != card_id or entry.upgrade != upgrade:
            continue
        if instance_id is None and not entry.instance_ids:
            result[index] = replace(entry, count=entry.count + 1)
            return tuple(sorted(result))
        if instance_id is not None and entry.instance_ids:
            result[index] = replace(
                entry,
                count=entry.count + 1,
                instance_ids=(*entry.instance_ids, instance_id),
            )
            return tuple(sorted(result))
    result.append(
        DeckEntry(
            card_id,
            upgrade,
            instance_ids=() if instance_id is None else (instance_id,),
        )
    )
    return tuple(sorted(result))


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _integer(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _object_list(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{label} must be a list of objects")
    return tuple(value)  # type: ignore[return-value]


def _text_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return tuple(_text(item, f"{label} item") for item in value)


def _rollout_phase(value: object) -> RolloutPhase:
    try:
        return RolloutPhase(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid rollout phase: {value!r}") from error


def _lifecycle(value: object) -> Lifecycle:
    try:
        return Lifecycle(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid rollout lifecycle: {value!r}") from error


def _external_kind(value: object) -> ExternalKind:
    try:
        return ExternalKind(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid external kind: {value!r}") from error


__all__ = [
    "ActionKind",
    "AttributeValues",
    "ChanceBranch",
    "ChanceHistoryEntry",
    "DeckEntry",
    "ExternalKind",
    "ExternalContinuation",
    "ExternalOutcome",
    "ExternalRequest",
    "InnerExamAdapter",
    "InnerExamOutcome",
    "Lifecycle",
    "ProduceAction",
    "ProduceRolloutKernel",
    "ProduceRolloutState",
    "ProduceStatePatch",
    "RewardContext",
    "RewardOffer",
    "RewardOffersOutcome",
    "RewardReplacementOutcome",
    "RolloutPhase",
    "RolloutPolicy",
    "RolloutResult",
    "RolloutStop",
    "TransitionResult",
    "WeeklyActionOutcome",
]
