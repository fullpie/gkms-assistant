"""Pure Plan3 scheduler for exact probabilistic loadout passives.

The scheduler deliberately stops at one marginal RNG gate at a time.  PC and
Android do not prove the order (or independence) of multiple passive sources
matching one Produce lifecycle event, so an ambiguous event becomes a typed
pause until a caller supplies the complete observed/external contract order.

Expanding a node is read-only.  Usage is copied into a successor frontier only
by :func:`commit_passive_scheduler_branch`, after the caller has selected or
observed one of the exact hit/miss branches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .passive_chance import (
    PHASE_END_LESSON,
    PHASE_START_AUDITION_FINAL,
    PHASE_START_AUDITION_MID1,
    PHASE_START_AUDITION_MID2,
    PHASE_START_LESSON,
    ExactProbability,
    PassiveChanceContract,
    PassiveChanceEffect,
    expand_passive_chance,
)


PASSIVE_SCHEDULER_SCHEMA_VERSION = 1

EVENT_START_LESSON = "StartLesson"
EVENT_END_LESSON = "EndLesson"
EVENT_START_AUDITION_MID1 = "StartAuditionMid1"
EVENT_START_AUDITION_MID2 = "StartAuditionMid2"
EVENT_START_AUDITION_FINAL = "StartAuditionFinal"

FRONTIER_READY = "ready"
FRONTIER_COMPLETE = "complete"
FRONTIER_PAUSED_UNRESOLVED_ORDER = "paused-unresolved-order"

PAUSE_UNKNOWN_CROSS_SOURCE_RNG_ORDER = "unknown-cross-source-rng-order"

_LESSON_ATTRIBUTES = frozenset({"vocal", "dance", "visual"})
_AUDITION_EVENT_PHASE_TRIGGER: dict[str, tuple[str, str]] = {
    EVENT_START_AUDITION_MID1: (
        PHASE_START_AUDITION_MID1,
        "p_trigger-start_audition_mid1",
    ),
    EVENT_START_AUDITION_MID2: (
        PHASE_START_AUDITION_MID2,
        "p_trigger-start_audition_mid2",
    ),
    EVENT_START_AUDITION_FINAL: (
        PHASE_START_AUDITION_FINAL,
        "p_trigger-start_audition_final",
    ),
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not _is_int(value) or int(value) < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return int(value)


@dataclass(frozen=True, slots=True)
class Plan3PassiveLifecycleEvent:
    """One explicit Plan3/Produce lifecycle occurrence."""

    event_id: str
    kind: str
    lesson_attribute: str | None = None
    lesson_sp: bool = False

    def __post_init__(self) -> None:
        _text(self.event_id, "event_id")
        if not isinstance(self.lesson_sp, bool):
            raise ValueError("lesson_sp must be a boolean")
        if self.kind in {EVENT_START_LESSON, EVENT_END_LESSON}:
            if self.lesson_attribute not in _LESSON_ATTRIBUTES:
                raise ValueError(
                    "lesson lifecycle events require vocal, dance, or visual"
                )
        elif self.kind in _AUDITION_EVENT_PHASE_TRIGGER:
            if self.lesson_attribute is not None or self.lesson_sp:
                raise ValueError("audition lifecycle events cannot have lesson fields")
        else:
            raise ValueError(f"unsupported passive lifecycle event: {self.kind!r}")

    @classmethod
    def start_lesson(
        cls,
        event_id: str,
        attribute: str,
        *,
        sp: bool = False,
    ) -> "Plan3PassiveLifecycleEvent":
        return cls(event_id, EVENT_START_LESSON, attribute, sp)

    @classmethod
    def end_lesson(
        cls,
        event_id: str,
        attribute: str,
        *,
        sp: bool = False,
    ) -> "Plan3PassiveLifecycleEvent":
        return cls(event_id, EVENT_END_LESSON, attribute, sp)

    @classmethod
    def start_audition_mid1(cls, event_id: str) -> "Plan3PassiveLifecycleEvent":
        return cls(event_id, EVENT_START_AUDITION_MID1)

    @classmethod
    def start_audition_mid2(cls, event_id: str) -> "Plan3PassiveLifecycleEvent":
        return cls(event_id, EVENT_START_AUDITION_MID2)

    @classmethod
    def start_audition_final(cls, event_id: str) -> "Plan3PassiveLifecycleEvent":
        return cls(event_id, EVENT_START_AUDITION_FINAL)

    @property
    def trigger_phase_type(self) -> str:
        if self.kind == EVENT_START_LESSON:
            return PHASE_START_LESSON
        if self.kind == EVENT_END_LESSON:
            return PHASE_END_LESSON
        return _AUDITION_EVENT_PHASE_TRIGGER[self.kind][0]

    @property
    def trigger_id(self) -> str:
        if self.kind in {EVENT_START_LESSON, EVENT_END_LESSON}:
            boundary = "start" if self.kind == EVENT_START_LESSON else "end"
            suffix = f"{self.lesson_attribute}{'_sp' if self.lesson_sp else ''}"
            return f"p_trigger-{boundary}_lesson-lesson_{suffix}"
        return _AUDITION_EVENT_PHASE_TRIGGER[self.kind][1]

    def matches(self, contract: PassiveChanceContract) -> bool:
        if not isinstance(contract, PassiveChanceContract):
            raise TypeError("contract must be PassiveChanceContract")
        return (
            contract.trigger_phase_type == self.trigger_phase_type
            and contract.trigger_id == self.trigger_id
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "lesson_attribute": self.lesson_attribute,
            "lesson_sp": self.lesson_sp,
            "trigger_phase_type": self.trigger_phase_type,
            "trigger_id": self.trigger_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan3PassiveLifecycleEvent":
        if not isinstance(payload, Mapping):
            raise TypeError("lifecycle event payload must be a mapping")
        lesson_sp = payload.get("lesson_sp")
        if not isinstance(lesson_sp, bool):
            raise ValueError("lesson_sp must be a boolean")
        event = cls(
            event_id=_text(payload.get("event_id"), "event_id"),
            kind=_text(payload.get("kind"), "kind"),
            lesson_attribute=_optional_text(
                payload.get("lesson_attribute"), "lesson_attribute"
            ),
            lesson_sp=lesson_sp,
        )
        for field, expected in (
            ("trigger_phase_type", event.trigger_phase_type),
            ("trigger_id", event.trigger_id),
        ):
            if field in payload and payload[field] != expected:
                raise ValueError(f"serialized lifecycle {field} does not match kind")
        return event


def _effect_from_dict(payload: Mapping[str, object]) -> PassiveChanceEffect:
    return PassiveChanceEffect(
        effect_id=_text(payload.get("effect_id"), "effect.effect_id"),
        effect_type=_text(payload.get("effect_type"), "effect.effect_type"),
        effect_value=_integer(
            payload.get("effect_value"), "effect.effect_value", minimum=-10**9
        ),
        produce_resource_type=_optional_text(
            payload.get("produce_resource_type"), "effect.produce_resource_type"
        ),
        status_enchant_id=_optional_text(
            payload.get("status_enchant_id"), "effect.status_enchant_id"
        ),
    )


def _contract_from_dict(payload: Mapping[str, object]) -> PassiveChanceContract:
    effect = payload.get("effect")
    if not isinstance(effect, Mapping):
        raise ValueError("contract.effect must be a mapping")
    contract = PassiveChanceContract(
        contract_id=_text(payload.get("contract_id"), "contract_id"),
        source_index=_integer(payload.get("source_index"), "source_index"),
        source_kind=_text(payload.get("source_kind"), "source_kind"),
        source_id=_text(payload.get("source_id"), "source_id"),
        skill_id=_text(payload.get("skill_id"), "skill_id"),
        rule_slot=_integer(payload.get("rule_slot"), "rule_slot", minimum=1),
        trigger_id=_text(payload.get("trigger_id"), "trigger_id"),
        trigger_phase_type=_text(
            payload.get("trigger_phase_type"), "trigger_phase_type"
        ),
        activation_rate_permil=_integer(
            payload.get("activation_rate_permil"),
            "activation_rate_permil",
            minimum=1,
        ),
        usage_key=_text(payload.get("usage_key"), "usage_key"),
        activation_limit=_integer(
            payload.get("activation_limit"), "activation_limit"
        ),
        use_count=_integer(payload.get("use_count"), "use_count"),
        effect=_effect_from_dict(effect),
    )
    if contract.activation_rate_permil > 999:
        raise ValueError("activation_rate_permil must be <= 999")
    probability = payload.get("activation_probability")
    if probability is not None and probability != contract.activation_probability.to_dict():
        raise ValueError("serialized activation probability does not match rate")
    if payload.get("rng_contract", "single-marginal-only") != "single-marginal-only":
        raise ValueError("unsupported passive RNG contract")
    return contract


@dataclass(frozen=True, slots=True)
class Plan3PassiveSchedulerNode:
    """One caller-ordered node; expanding it touches only this contract."""

    ordinal: int
    contract: PassiveChanceContract

    def __post_init__(self) -> None:
        _integer(self.ordinal, "ordinal")
        if not isinstance(self.contract, PassiveChanceContract):
            raise TypeError("node contract must be PassiveChanceContract")

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    def to_dict(self) -> dict[str, object]:
        return {"ordinal": self.ordinal, "contract": self.contract.to_dict()}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan3PassiveSchedulerNode":
        raw_contract = payload.get("contract")
        if not isinstance(raw_contract, Mapping):
            raise ValueError("node.contract must be a mapping")
        return cls(
            ordinal=_integer(payload.get("ordinal"), "ordinal"),
            contract=_contract_from_dict(raw_contract),
        )


@dataclass(frozen=True, slots=True)
class Plan3PassiveOrderPause:
    """Typed fail-closed result for an event with unproved source order."""

    code: str
    event_id: str
    candidate_contracts: tuple[PassiveChanceContract, ...]
    reason: str

    def __post_init__(self) -> None:
        if self.code != PAUSE_UNKNOWN_CROSS_SOURCE_RNG_ORDER:
            raise ValueError(f"unsupported passive scheduler pause: {self.code!r}")
        _text(self.event_id, "event_id")
        _text(self.reason, "reason")
        if len(self.candidate_contracts) < 2:
            raise ValueError("cross-source order pause requires at least two contracts")
        ids = tuple(item.contract_id for item in self.candidate_contracts)
        if len(ids) != len(set(ids)):
            raise ValueError("pause candidate contract IDs must be unique")

    @property
    def candidate_contract_ids(self) -> tuple[str, ...]:
        """Candidate set in canonical serialization order, not asserted RNG order."""

        return tuple(item.contract_id for item in self.candidate_contracts)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "event_id": self.event_id,
            "reason": self.reason,
            "ordering_semantics": "candidate-set-only",
            "candidate_contract_ids": list(self.candidate_contract_ids),
            "candidate_contracts": [
                contract.to_dict() for contract in self.candidate_contracts
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan3PassiveOrderPause":
        raw_contracts = payload.get("candidate_contracts")
        if not isinstance(raw_contracts, list) or not all(
            isinstance(item, Mapping) for item in raw_contracts
        ):
            raise ValueError("candidate_contracts must be an object array")
        pause = cls(
            code=_text(payload.get("code"), "pause.code"),
            event_id=_text(payload.get("event_id"), "pause.event_id"),
            reason=_text(payload.get("reason"), "pause.reason"),
            candidate_contracts=tuple(
                _contract_from_dict(item) for item in raw_contracts
            ),
        )
        if payload.get("ordering_semantics") != "candidate-set-only":
            raise ValueError("pause must retain candidate-set-only semantics")
        raw_ids = payload.get("candidate_contract_ids")
        if raw_ids != list(pause.candidate_contract_ids):
            raise ValueError("pause candidate IDs do not match contracts")
        return pause


FrontierState = Literal[
    "ready", "complete", "paused-unresolved-order"
]


@dataclass(frozen=True, slots=True)
class Plan3PassiveSearchFrontier:
    """Immutable, hashable and JSON-safe input for search/Expectimax."""

    event: Plan3PassiveLifecycleEvent
    nodes: tuple[Plan3PassiveSchedulerNode, ...]
    cursor: int
    usage_counts: tuple[tuple[str, int], ...]
    pause: Plan3PassiveOrderPause | None = None
    schema_version: int = PASSIVE_SCHEDULER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PASSIVE_SCHEDULER_SCHEMA_VERSION:
            raise ValueError("unsupported passive scheduler schema version")
        if not isinstance(self.event, Plan3PassiveLifecycleEvent):
            raise TypeError("frontier event must be Plan3PassiveLifecycleEvent")
        _integer(self.cursor, "cursor")
        if not all(isinstance(node, Plan3PassiveSchedulerNode) for node in self.nodes):
            raise TypeError("frontier nodes must be Plan3PassiveSchedulerNode")
        if tuple(node.ordinal for node in self.nodes) != tuple(range(len(self.nodes))):
            raise ValueError("frontier node ordinals must be contiguous")
        ids = tuple(node.contract_id for node in self.nodes)
        if len(ids) != len(set(ids)):
            raise ValueError("frontier contract IDs must be unique")
        if not all(self.event.matches(node.contract) for node in self.nodes):
            raise ValueError("frontier nodes must match the lifecycle event")
        if self.cursor > len(self.nodes):
            raise ValueError("frontier cursor exceeds node count")

        usage_keys: list[str] = []
        for key, value in self.usage_counts:
            usage_keys.append(_text(key, "usage key"))
            _integer(value, f"usage count:{key}")
        if len(usage_keys) != len(set(usage_keys)):
            raise ValueError("frontier usage keys must be unique")
        if tuple(usage_keys) != tuple(sorted(usage_keys)):
            raise ValueError("frontier usage counts must be sorted")

        if self.pause is not None:
            if not isinstance(self.pause, Plan3PassiveOrderPause):
                raise TypeError("frontier pause must be Plan3PassiveOrderPause")
            if self.nodes or self.cursor != 0:
                raise ValueError("a paused frontier cannot assert an ordered node list")
            if self.pause.event_id != self.event.event_id:
                raise ValueError("pause event ID must match frontier event")
            if not all(
                self.event.matches(contract)
                for contract in self.pause.candidate_contracts
            ):
                raise ValueError("pause candidates must match the lifecycle event")

    @property
    def state(self) -> FrontierState:
        if self.pause is not None:
            return FRONTIER_PAUSED_UNRESOLVED_ORDER
        if self.cursor >= len(self.nodes):
            return FRONTIER_COMPLETE
        return FRONTIER_READY

    @property
    def current_node(self) -> Plan3PassiveSchedulerNode | None:
        if self.state != FRONTIER_READY:
            return None
        return self.nodes[self.cursor]

    @property
    def usage_map(self) -> dict[str, int]:
        return dict(self.usage_counts)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "event": self.event.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
            "cursor": self.cursor,
            "usage_counts": [list(item) for item in self.usage_counts],
            "pause": self.pause.to_dict() if self.pause is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan3PassiveSearchFrontier":
        if payload.get("schema_version") != PASSIVE_SCHEDULER_SCHEMA_VERSION:
            raise ValueError("unsupported passive scheduler schema version")
        raw_event = payload.get("event")
        raw_nodes = payload.get("nodes")
        raw_usage = payload.get("usage_counts")
        raw_pause = payload.get("pause")
        if not isinstance(raw_event, Mapping):
            raise ValueError("event must be a mapping")
        if not isinstance(raw_nodes, list) or not all(
            isinstance(item, Mapping) for item in raw_nodes
        ):
            raise ValueError("nodes must be an object array")
        if not isinstance(raw_usage, list) or not all(
            isinstance(item, list) and len(item) == 2 for item in raw_usage
        ):
            raise ValueError("usage_counts must be an array of pairs")
        if raw_pause is not None and not isinstance(raw_pause, Mapping):
            raise ValueError("pause must be a mapping or null")
        frontier = cls(
            event=Plan3PassiveLifecycleEvent.from_dict(raw_event),
            nodes=tuple(Plan3PassiveSchedulerNode.from_dict(item) for item in raw_nodes),
            cursor=_integer(payload.get("cursor"), "cursor"),
            usage_counts=tuple(
                (
                    _text(item[0], "usage key"),
                    _integer(item[1], f"usage count:{item[0]}"),
                )
                for item in raw_usage
            ),
            pause=(
                Plan3PassiveOrderPause.from_dict(raw_pause)
                if raw_pause is not None
                else None
            ),
            schema_version=PASSIVE_SCHEDULER_SCHEMA_VERSION,
        )
        if payload.get("state") != frontier.state:
            raise ValueError("serialized frontier state is inconsistent")
        return frontier


@dataclass(frozen=True, slots=True)
class Plan3PassiveScheduledBranch:
    """One exact branch preview; it does not mutate or advance a frontier."""

    event_id: str
    node_ordinal: int
    contract_id: str
    outcome: str
    probability: ExactProbability
    activated: bool
    usage_key: str
    prior_use_count: int
    next_use_count: int
    effect: PassiveChanceEffect | None

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "node_ordinal": self.node_ordinal,
            "contract_id": self.contract_id,
            "outcome": self.outcome,
            "probability": self.probability.to_dict(),
            "activated": self.activated,
            "usage_key": self.usage_key,
            "prior_use_count": self.prior_use_count,
            "next_use_count": self.next_use_count,
            "effect": self.effect.to_dict() if self.effect is not None else None,
        }


@dataclass(frozen=True, slots=True)
class Plan3PassiveScheduledExpansion:
    event_id: str
    node_ordinal: int
    contract_id: str
    branches: tuple[Plan3PassiveScheduledBranch, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "node_ordinal": self.node_ordinal,
            "contract_id": self.contract_id,
            "branches": [branch.to_dict() for branch in self.branches],
        }


class PassiveSchedulerNotReadyError(RuntimeError):
    def __init__(self, frontier: Plan3PassiveSearchFrontier) -> None:
        self.state = frontier.state
        self.pause = frontier.pause
        super().__init__(f"passive scheduler frontier is not ready: {self.state}")


def _normalized_contracts(
    contracts: Sequence[PassiveChanceContract],
) -> tuple[PassiveChanceContract, ...]:
    if isinstance(contracts, (str, bytes)) or not isinstance(contracts, Sequence):
        raise TypeError("contracts must be a sequence of PassiveChanceContract")
    normalized = tuple(contracts)
    if not all(isinstance(item, PassiveChanceContract) for item in normalized):
        raise TypeError("contracts must contain PassiveChanceContract")
    ids = tuple(item.contract_id for item in normalized)
    if len(ids) != len(set(ids)):
        raise ValueError("contract IDs must be unique")
    return normalized


def _normalized_usage(
    contracts: Sequence[PassiveChanceContract],
    usage_counts: Mapping[str, int] | None,
) -> tuple[tuple[str, int], ...]:
    usage: dict[str, int] = {}
    for contract in contracts:
        existing = usage.get(contract.usage_key)
        if existing is not None and existing != contract.use_count:
            raise ValueError(
                f"contracts disagree on use count for {contract.usage_key}"
            )
        usage[contract.usage_key] = contract.use_count
    if usage_counts is not None:
        if not isinstance(usage_counts, Mapping):
            raise TypeError("usage_counts must be a mapping")
        for key, value in usage_counts.items():
            usage[_text(key, "usage key")] = _integer(
                value, f"usage count:{key}"
            )
    return tuple(sorted(usage.items()))


def _order_matches(
    matches: Sequence[PassiveChanceContract],
    ordered_contract_ids: Sequence[str],
) -> tuple[PassiveChanceContract, ...]:
    if isinstance(ordered_contract_ids, (str, bytes)) or not isinstance(
        ordered_contract_ids, Sequence
    ):
        raise TypeError("ordered_contract_ids must be a sequence of strings")
    order = tuple(_text(value, "ordered contract ID") for value in ordered_contract_ids)
    if len(order) != len(set(order)):
        raise ValueError("ordered contract IDs must be unique")
    by_id = {contract.contract_id: contract for contract in matches}
    if len(order) != len(by_id) or set(order) != set(by_id):
        raise ValueError(
            "ordered_contract_ids must be an exact permutation of event contracts"
        )
    return tuple(by_id[contract_id] for contract_id in order)


def schedule_passive_chance_event(
    event: Plan3PassiveLifecycleEvent,
    contracts: Sequence[PassiveChanceContract],
    *,
    usage_counts: Mapping[str, int] | None = None,
    ordered_contract_ids: Sequence[str] | None = None,
) -> Plan3PassiveSearchFrontier:
    """Build a pure frontier for one explicit lifecycle event."""

    if not isinstance(event, Plan3PassiveLifecycleEvent):
        raise TypeError("event must be Plan3PassiveLifecycleEvent")
    normalized = _normalized_contracts(contracts)
    usage = _normalized_usage(normalized, usage_counts)
    matches = tuple(contract for contract in normalized if event.matches(contract))

    if ordered_contract_ids is not None:
        ordered = _order_matches(matches, ordered_contract_ids)
    elif len(matches) <= 1:
        ordered = matches
    else:
        # contract_id sorting is only a stable representation of a candidate
        # set.  It is explicitly not used as a claimed execution/RNG order.
        candidates = tuple(sorted(matches, key=lambda item: item.contract_id))
        return Plan3PassiveSearchFrontier(
            event=event,
            nodes=(),
            cursor=0,
            usage_counts=usage,
            pause=Plan3PassiveOrderPause(
                code=PAUSE_UNKNOWN_CROSS_SOURCE_RNG_ORDER,
                event_id=event.event_id,
                candidate_contracts=candidates,
                reason=(
                    "PC/Android do not prove the RNG order or independence of "
                    "multiple loadout sources matching this lifecycle event"
                ),
            ),
        )

    return Plan3PassiveSearchFrontier(
        event=event,
        nodes=tuple(
            Plan3PassiveSchedulerNode(index, contract)
            for index, contract in enumerate(ordered)
        ),
        cursor=0,
        usage_counts=usage,
    )


def resolve_passive_scheduler_order(
    frontier: Plan3PassiveSearchFrontier,
    ordered_contract_ids: Sequence[str],
) -> Plan3PassiveSearchFrontier:
    """Resume a typed order pause using caller-supplied external ordering."""

    if not isinstance(frontier, Plan3PassiveSearchFrontier):
        raise TypeError("frontier must be Plan3PassiveSearchFrontier")
    if frontier.pause is None:
        raise ValueError("frontier is not paused for unresolved order")
    ordered = _order_matches(
        frontier.pause.candidate_contracts, ordered_contract_ids
    )
    return Plan3PassiveSearchFrontier(
        event=frontier.event,
        nodes=tuple(
            Plan3PassiveSchedulerNode(index, contract)
            for index, contract in enumerate(ordered)
        ),
        cursor=0,
        usage_counts=frontier.usage_counts,
    )


def expand_next_passive_chance(
    frontier: Plan3PassiveSearchFrontier,
) -> Plan3PassiveScheduledExpansion:
    """Expand exactly the current ordered contract without committing usage."""

    if not isinstance(frontier, Plan3PassiveSearchFrontier):
        raise TypeError("frontier must be Plan3PassiveSearchFrontier")
    node = frontier.current_node
    if node is None:
        raise PassiveSchedulerNotReadyError(frontier)
    expansion = expand_passive_chance(
        node.contract, usage_counts=frontier.usage_map
    )
    branches = tuple(
        Plan3PassiveScheduledBranch(
            event_id=frontier.event.event_id,
            node_ordinal=node.ordinal,
            contract_id=node.contract_id,
            outcome=branch.outcome,
            probability=branch.probability,
            activated=branch.activated,
            usage_key=branch.usage_key,
            prior_use_count=branch.prior_use_count,
            next_use_count=branch.next_use_count,
            effect=branch.effect,
        )
        for branch in expansion.branches
    )
    return Plan3PassiveScheduledExpansion(
        event_id=frontier.event.event_id,
        node_ordinal=node.ordinal,
        contract_id=node.contract_id,
        branches=branches,
    )


def commit_passive_scheduler_branch(
    frontier: Plan3PassiveSearchFrontier,
    branch: Plan3PassiveScheduledBranch,
) -> Plan3PassiveSearchFrontier:
    """Advance after a caller selects/observes one valid branch."""

    if not isinstance(frontier, Plan3PassiveSearchFrontier):
        raise TypeError("frontier must be Plan3PassiveSearchFrontier")
    if not isinstance(branch, Plan3PassiveScheduledBranch):
        raise TypeError("branch must be Plan3PassiveScheduledBranch")
    expansion = expand_next_passive_chance(frontier)
    if branch not in expansion.branches:
        raise ValueError("branch does not belong to the current scheduler node")
    usage = frontier.usage_map
    usage[branch.usage_key] = branch.next_use_count
    return Plan3PassiveSearchFrontier(
        event=frontier.event,
        nodes=frontier.nodes,
        cursor=frontier.cursor + 1,
        usage_counts=tuple(sorted(usage.items())),
    )


__all__ = [
    "EVENT_END_LESSON",
    "EVENT_START_AUDITION_FINAL",
    "EVENT_START_AUDITION_MID1",
    "EVENT_START_AUDITION_MID2",
    "EVENT_START_LESSON",
    "FRONTIER_COMPLETE",
    "FRONTIER_PAUSED_UNRESOLVED_ORDER",
    "FRONTIER_READY",
    "PASSIVE_SCHEDULER_SCHEMA_VERSION",
    "PAUSE_UNKNOWN_CROSS_SOURCE_RNG_ORDER",
    "PassiveSchedulerNotReadyError",
    "Plan3PassiveLifecycleEvent",
    "Plan3PassiveOrderPause",
    "Plan3PassiveScheduledBranch",
    "Plan3PassiveScheduledExpansion",
    "Plan3PassiveSchedulerNode",
    "Plan3PassiveSearchFrontier",
    "commit_passive_scheduler_branch",
    "expand_next_passive_chance",
    "resolve_passive_scheduler_order",
    "schedule_passive_chance_event",
]
