"""Lossless, calculation-free snapshots for First Star REGULAR exam turns.

This module deliberately records observations only.  It does not interpret card
effects or apply any game rules; that remains the responsibility of a verified
engine layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


SCHEMA_VERSION = 1

# Kept as a plain JSON-Schema document so a bridge written in another language
# can validate exactly the same payload without importing Python code.
REGULAR_TURN_STATE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://gkms-tool.local/schema/regular-turn-state-v1.json",
    "type": "object",
    "required": ["schema_version", "mode", "turn", "clear", "stamina", "buffs", "hand", "observation"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "mode": {"const": "first_star_regular"},
        "turn": {"type": "integer", "minimum": 1},
        "clear": {
            "type": "object",
            "required": ["threshold", "current_parameter"],
            "properties": {
                "threshold": {"type": "integer", "minimum": 0},
                "current_parameter": {"type": "integer", "minimum": 0},
            },
        },
        "stamina": {"type": "integer", "minimum": 0},
        "buffs": {"type": "array", "items": {"type": "object", "required": ["id", "value"]}},
        "hand": {"type": "array", "items": {"type": "object", "required": ["card_id", "upgrade", "cost", "legal"]}},
        "observation": {
            "type": "object",
            "required": ["source", "confidence", "captured_at"],
            "properties": {"confidence": {"type": "number", "minimum": 0, "maximum": 1}},
        },
    },
}


def _extra(payload: Mapping[str, Any], known: set[str]) -> dict[str, Any]:
    """Keep forward-compatible keys intact rather than silently dropping them."""
    return {key: value for key, value in payload.items() if key not in known}


def _required(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise ValueError(f"missing required field: {key}")
    return payload[key]


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class ClearProgress:
    threshold: int
    current_parameter: int
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "current_parameter": self.current_parameter, **self.extra}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ClearProgress":
        return cls(_integer(_required(payload, "threshold"), "clear.threshold"), _integer(_required(payload, "current_parameter"), "clear.current_parameter"), _extra(payload, {"threshold", "current_parameter"}))


@dataclass(frozen=True, slots=True)
class ObservedBuff:
    """An observed buff; IDs and semantics intentionally remain opaque."""

    id: str
    value: Any
    duration: int | None = None
    source: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {"id": self.id, "value": self.value, **self.extra}
        if self.duration is not None:
            result["duration"] = self.duration
        if self.source is not None:
            result["source"] = self.source
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ObservedBuff":
        buff_id = _required(payload, "id")
        if not isinstance(buff_id, str) or not buff_id:
            raise ValueError("buff.id must be a non-empty string")
        duration = payload.get("duration")
        if duration is not None:
            duration = _integer(duration, "buff.duration")
        source = payload.get("source")
        if source is not None and not isinstance(source, str):
            raise ValueError("buff.source must be a string")
        return cls(buff_id, _required(payload, "value"), duration, source, _extra(payload, {"id", "value", "duration", "source"}))


@dataclass(frozen=True, slots=True)
class HandCard:
    card_id: str
    upgrade: int
    cost: int | None
    legal: bool | None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result = {"card_id": self.card_id, "upgrade": self.upgrade, "cost": self.cost, "legal": self.legal, **self.extra}
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HandCard":
        card_id = _required(payload, "card_id")
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("hand.card_id must be a non-empty string")
        cost = _required(payload, "cost")
        if cost is not None:
            cost = _integer(cost, "hand.cost")
        legal = _required(payload, "legal")
        if legal is not None and not isinstance(legal, bool):
            raise ValueError("hand.legal must be a boolean or null")
        return cls(card_id, _integer(_required(payload, "upgrade"), "hand.upgrade"), cost, legal, _extra(payload, {"card_id", "upgrade", "cost", "legal"}))


@dataclass(frozen=True, slots=True)
class Observation:
    source: str
    confidence: float
    captured_at: datetime
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "confidence": self.confidence, "captured_at": self.captured_at.isoformat(), **self.extra}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Observation":
        source = _required(payload, "source")
        confidence = _required(payload, "confidence")
        captured_at = _required(payload, "captured_at")
        if not isinstance(source, str) or not source:
            raise ValueError("observation.source must be a non-empty string")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("observation.confidence must be between 0 and 1")
        if not isinstance(captured_at, str):
            raise ValueError("observation.captured_at must be ISO-8601 text")
        try:
            timestamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("observation.captured_at must be ISO-8601 text") from error
        return cls(source, float(confidence), timestamp, _extra(payload, {"source", "confidence", "captured_at"}))


@dataclass(frozen=True, slots=True)
class RegularTurnState:
    """Portable First Star REGULAR turn observation with lossless extensions."""

    turn: int
    clear: ClearProgress
    stamina: int
    buffs: tuple[ObservedBuff, ...]
    hand: tuple[HandCard, ...]
    observation: Observation
    mode: str = "first_star_regular"
    schema_version: int = SCHEMA_VERSION
    extra: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        _integer(self.turn, "turn", 1)
        _integer(self.stamina, "stamina")
        if self.mode != "first_star_regular":
            raise ValueError("mode must be first_star_regular")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {"schema_version": self.schema_version, "mode": self.mode, "turn": self.turn, "clear": self.clear.to_dict(), "stamina": self.stamina, "buffs": [buff.to_dict() for buff in self.buffs], "hand": [card.to_dict() for card in self.hand], "observation": self.observation.to_dict(), **self.extra}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegularTurnState":
        version = _integer(_required(payload, "schema_version"), "schema_version", 1)
        mode = _required(payload, "mode")
        if not isinstance(mode, str):
            raise ValueError("mode must be a string")
        clear = _required(payload, "clear")
        buffs = _required(payload, "buffs")
        hand = _required(payload, "hand")
        observation = _required(payload, "observation")
        if not all(isinstance(item, Mapping) for item in (clear, observation)) or not isinstance(buffs, list) or not isinstance(hand, list):
            raise ValueError("clear, buffs, hand, and observation have invalid types")
        state = cls(_integer(_required(payload, "turn"), "turn", 1), ClearProgress.from_dict(clear), _integer(_required(payload, "stamina"), "stamina"), tuple(ObservedBuff.from_dict(item) for item in buffs if isinstance(item, Mapping)), tuple(HandCard.from_dict(item) for item in hand if isinstance(item, Mapping)), Observation.from_dict(observation), mode, version, _extra(payload, {"schema_version", "mode", "turn", "clear", "stamina", "buffs", "hand", "observation"}))
        if len(state.buffs) != len(buffs) or len(state.hand) != len(hand):
            raise ValueError("buffs and hand entries must be objects")
        state.validate()
        return state
