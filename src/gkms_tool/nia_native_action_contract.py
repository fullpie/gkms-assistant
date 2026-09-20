"""Small plan-neutral action contract for N.I.A. learning sidecars.

The game-specific planners remain responsible for legality and execution.
This module only gives Plan1/Plan2/Plan3 adapters the same persisted action
identity and step shape, so learning code does not need to pretend every plan
uses ``Plan2NativeAction``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json


ACTION_SCHEMA = "gkms.nia-native-action.v1"
CANDIDATE_SET_SCHEMA = "gkms.nia-native-candidate-set.v1"
STEP_SCHEMA = "gkms.nia-native-step-record.v1"

KIND_PLAY = "play"
KIND_DRINK = "drink"
KIND_END_TURN = "end_turn"
ACTION_KINDS = frozenset({KIND_PLAY, KIND_DRINK, KIND_END_TURN})

_KIND_ALIASES = {
    "card": KIND_PLAY,
    "use-hand": KIND_PLAY,
    "use_hand": KIND_PLAY,
    "use-drink": KIND_DRINK,
    "use_drink": KIND_DRINK,
    "turn-end": KIND_END_TURN,
    "turn_end": KIND_END_TURN,
    "skip": KIND_END_TURN,
}


def _text(value: object, label: str, *, optional: bool = False) -> str:
    if optional and value in {None, ""}:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _slot(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("drink slot_index must be a non-negative integer")
    return value


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        detached = json.loads(
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON-compatible") from error
    if not isinstance(detached, dict):
        raise ValueError(f"{label} must be an object")
    return detached


@dataclass(frozen=True, slots=True)
class NiaNativeAction:
    kind: str
    card_guid: str = ""
    card_id: str = ""
    slot_index: int | None = None
    instance_id: str = ""
    drink_id: str = ""
    selected_card_guid: str = ""
    schema: str = ACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTION_SCHEMA:
            raise ValueError("unsupported N.I.A. native action schema")
        kind = _KIND_ALIASES.get(self.kind, self.kind)
        if kind not in ACTION_KINDS:
            raise ValueError(f"unsupported N.I.A. native action kind: {self.kind}")
        object.__setattr__(self, "kind", kind)
        if kind == KIND_PLAY:
            _text(self.card_guid, "card_guid")
            _text(self.card_id, "card_id", optional=True)
            if self.slot_index is not None or any(
                (self.instance_id, self.drink_id, self.selected_card_guid)
            ):
                raise ValueError("PLAY cannot carry drink fields")
        elif kind == KIND_DRINK:
            if self.slot_index is None:
                raise ValueError("DRINK requires slot_index")
            _slot(self.slot_index)
            _text(self.drink_id, "drink_id")
            _text(self.instance_id, "instance_id", optional=True)
            _text(
                self.selected_card_guid,
                "selected_card_guid",
                optional=True,
            )
            if self.card_guid or self.card_id:
                raise ValueError("DRINK cannot carry PLAY fields")
        else:
            if (
                self.card_guid
                or self.card_id
                or self.slot_index is not None
                or self.instance_id
                or self.drink_id
                or self.selected_card_guid
            ):
                raise ValueError("END_TURN cannot carry card or drink fields")

    @property
    def action_id(self) -> str:
        if self.kind == KIND_PLAY:
            return f"PLAY:{self.card_guid}"
        if self.kind == KIND_END_TURN:
            return "END_TURN"
        assert self.slot_index is not None
        instance = self.instance_id or "-"
        selected = self.selected_card_guid or "-"
        return (
            f"DRINK:{self.slot_index}:{instance}:{self.drink_id}:"
            f"{selected}"
        )

    @classmethod
    def play(cls, card_guid: str, *, card_id: str = "") -> "NiaNativeAction":
        return cls(KIND_PLAY, card_guid=card_guid, card_id=card_id)

    @classmethod
    def drink(
        cls,
        slot_index: int,
        drink_id: str,
        *,
        instance_id: str = "",
        selected_card_guid: str = "",
    ) -> "NiaNativeAction":
        return cls(
            KIND_DRINK,
            slot_index=slot_index,
            instance_id=instance_id,
            drink_id=drink_id,
            selected_card_guid=selected_card_guid,
        )

    @classmethod
    def end_turn(cls) -> "NiaNativeAction":
        return cls(KIND_END_TURN)

    @classmethod
    def from_value(cls, value: object) -> "NiaNativeAction":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            raw = value
            kind = raw.get("kind")
            if not isinstance(kind, str):
                action_id = raw.get("action_id")
                if action_id == "END_TURN":
                    kind = KIND_END_TURN
                elif isinstance(action_id, str) and action_id.startswith("PLAY:"):
                    kind = KIND_PLAY
                elif isinstance(action_id, str) and action_id.startswith("DRINK:"):
                    kind = KIND_DRINK
            normalized = _KIND_ALIASES.get(str(kind), str(kind))
            if normalized == KIND_PLAY:
                return cls.play(
                    _text(
                        raw.get("card_guid", raw.get("guid")),
                        "card_guid",
                    ),
                    card_id=_text(raw.get("card_id"), "card_id", optional=True),
                )
            if normalized == KIND_DRINK:
                return cls.drink(
                    _slot(raw.get("slot_index", raw.get("slot"))),
                    _text(raw.get("drink_id"), "drink_id"),
                    instance_id=_text(
                        raw.get("instance_id", raw.get("instance")),
                        "instance_id",
                        optional=True,
                    ),
                    selected_card_guid=_text(
                        raw.get(
                            "selected_card_guid",
                            raw.get("selection"),
                        ),
                        "selected_card_guid",
                        optional=True,
                    ),
                )
            if normalized == KIND_END_TURN:
                return cls.end_turn()
            raise ValueError("mapping has no supported native action kind")

        kind = getattr(value, "kind", None)
        normalized = _KIND_ALIASES.get(str(kind), str(kind))
        if normalized == KIND_PLAY:
            return cls.play(
                _text(getattr(value, "card_guid", None), "card_guid"),
                card_id=_text(
                    getattr(value, "card_id", None),
                    "card_id",
                    optional=True,
                ),
            )
        if normalized == KIND_DRINK:
            return cls.drink(
                _slot(
                    getattr(
                        value,
                        "slot_index",
                        getattr(value, "slot", None),
                    )
                ),
                _text(getattr(value, "drink_id", None), "drink_id"),
                instance_id=_text(
                    getattr(
                        value,
                        "instance_id",
                        getattr(value, "instance", None),
                    ),
                    "instance_id",
                    optional=True,
                ),
                selected_card_guid=_text(
                    getattr(
                        value,
                        "selected_card_guid",
                        getattr(value, "selection", None),
                    ),
                    "selected_card_guid",
                    optional=True,
                ),
            )
        if normalized == KIND_END_TURN:
            return cls.end_turn()
        raise TypeError(f"unsupported native action: {type(value).__name__}")

    def to_dict(self) -> dict[str, object]:
        if self.kind == KIND_PLAY:
            result: dict[str, object] = {
                "kind": self.kind,
                "card_guid": self.card_guid,
                "action_id": self.action_id,
            }
            if self.card_id:
                result["card_id"] = self.card_id
            return result
        if self.kind == KIND_DRINK:
            result = {
                "kind": self.kind,
                "slot_index": self.slot_index,
                "drink_id": self.drink_id,
                "selected_card_guid": self.selected_card_guid,
                "action_id": self.action_id,
            }
            if self.instance_id:
                result["instance_id"] = self.instance_id
            return result
        return {"kind": self.kind, "action_id": self.action_id}


@dataclass(frozen=True, slots=True)
class NiaNativeCandidateSet:
    candidates: tuple[NiaNativeAction, ...]
    authority: str
    complete: bool
    blockers: tuple[str, ...] = ()
    schema: str = CANDIDATE_SET_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CANDIDATE_SET_SCHEMA:
            raise ValueError("unsupported native candidate-set schema")
        values = tuple(NiaNativeAction.from_value(value) for value in self.candidates)
        _text(self.authority, "candidate authority")
        if type(self.complete) is not bool:
            raise TypeError("candidate complete must be boolean")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, str) or not value for value in blockers):
            raise ValueError("candidate blockers must contain non-empty text")
        identities = tuple(value.action_id for value in values)
        if len(identities) != len(set(identities)):
            raise ValueError("native candidates must have unique action IDs")
        if self.complete and (not values or blockers):
            raise ValueError("complete candidate set must be non-empty and unblocked")
        object.__setattr__(self, "candidates", values)
        object.__setattr__(self, "blockers", blockers)

    def contains(self, action: object) -> bool:
        identity = NiaNativeAction.from_value(action).action_id
        return any(value.action_id == identity for value in self.candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "authority": self.authority,
            "complete": self.complete,
            "blockers": list(self.blockers),
            "candidates": [value.to_dict() for value in self.candidates],
        }


@dataclass(frozen=True, slots=True)
class NiaNativeStepRecord:
    source_id: str
    step: int
    state_before: Mapping[str, object]
    legal: NiaNativeCandidateSet
    action: NiaNativeAction
    submission_proof: Mapping[str, object]
    state_after: Mapping[str, object] | None = None
    reward: int | float | None = None
    terminal: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema: str = STEP_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != STEP_SCHEMA:
            raise ValueError("unsupported native step-record schema")
        _text(self.source_id, "source_id")
        if isinstance(self.step, bool) or not isinstance(self.step, int) or self.step < 1:
            raise ValueError("step must be a positive integer")
        before = _json_object(self.state_before, "state_before")
        if not isinstance(self.legal, NiaNativeCandidateSet):
            raise TypeError("legal must be NiaNativeCandidateSet")
        action = NiaNativeAction.from_value(self.action)
        proof = _json_object(self.submission_proof, "submission_proof")
        if not proof:
            raise ValueError("submission_proof must be non-empty")
        after = (
            None
            if self.state_after is None
            else _json_object(self.state_after, "state_after")
        )
        if not self.legal.complete or not self.legal.contains(action):
            raise ValueError("submitted action requires one complete containing candidate set")
        if after is None:
            if self.reward is not None or self.terminal:
                raise ValueError("pending/contextual step cannot claim reward or terminal")
        elif isinstance(self.reward, bool) or not isinstance(self.reward, (int, float)):
            raise ValueError("observed state_after requires numeric reward")
        if type(self.terminal) is not bool:
            raise TypeError("terminal must be boolean")
        metadata = _json_object(self.metadata, "metadata")
        object.__setattr__(self, "state_before", before)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "submission_proof", proof)
        object.__setattr__(self, "state_after", after)
        object.__setattr__(self, "metadata", metadata)

    @property
    def full_rl_transition(self) -> bool:
        return self.state_after is not None

    @property
    def contextual_bandit(self) -> bool:
        return self.state_after is None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "source_id": self.source_id,
            "step": self.step,
            "state_before": dict(self.state_before),
            "legal": self.legal.to_dict(),
            "action": self.action.to_dict(),
            "submission_proof": dict(self.submission_proof),
            "state_after": (
                None if self.state_after is None else dict(self.state_after)
            ),
            "reward": self.reward,
            "terminal": self.terminal,
            "metadata": dict(self.metadata),
            "contextual_bandit": self.contextual_bandit,
            "full_rl_transition": self.full_rl_transition,
        }


__all__ = [
    "ACTION_SCHEMA",
    "CANDIDATE_SET_SCHEMA",
    "KIND_DRINK",
    "KIND_END_TURN",
    "KIND_PLAY",
    "NiaNativeAction",
    "NiaNativeCandidateSet",
    "NiaNativeStepRecord",
    "STEP_SCHEMA",
]
