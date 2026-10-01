"""Durable, safety-gated snapshots of the carried produce loadout.

The in-game loadout is six support cards (five owned and one borrowed) plus
four memories.  This module deliberately starts *after* visual/manual
recognition: it records the evidence for every field, validates the fixed
layout, and converts authoritative observations into the selection types used
by :mod:`gkms_tool.passive_catalog`.

Incomplete and uncertain observations remain serializable for later audit.
They are never partially applied: ``resolve_passives`` raises
``LoadoutBlockedError`` when any slot is missing, low-confidence, unknown to
Master, or resolves to an unsupported passive rule.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .passive_catalog import (
    MasterPassiveCatalog,
    MemoryAbilitySelection,
    PassiveSource,
    SupportCardSelection,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOADOUT_SNAPSHOT_PATH = PROJECT_ROOT / "var" / "loadout_snapshot.json"
LOADOUT_SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_AUTHORITATIVE_CONFIDENCE = 0.80

SUPPORT_CARD_COUNT = 6
OWNED_SUPPORT_COUNT = 5
BORROWED_SUPPORT_COUNT = 1
MEMORY_COUNT = 4

SUPPORT_ORIGIN_OWNED = "owned"
SUPPORT_ORIGIN_BORROWED = "borrowed"
SUPPORT_ORIGINS = frozenset(
    {SUPPORT_ORIGIN_OWNED, SUPPORT_ORIGIN_BORROWED}
)


def _strict_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _strict_text(value, label)


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _optional_int(value: Any, label: str, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    return _strict_int(value, label, minimum=minimum)


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _confidence(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise ValueError(f"{label} must be between 0 and 1")
    return float(value)


def _validate_timestamp(value: Any, label: str) -> str:
    text = _strict_text(value, label)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} must be ISO-8601 text") from error
    return text


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class RecognitionEvidence:
    """Evidence supporting one recognized field.

    ``source`` describes how the value was obtained (for example
    ``visible-loadout-panel`` or ``manual-confirmed``).  Image/frame/region
    fields are audit locators rather than a requirement for manual evidence.
    ``authoritative`` is an explicit producer assertion; the safety gate also
    independently enforces the confidence threshold.
    """

    source: str
    confidence: float
    authoritative: bool
    observed_text: str | None = None
    image_path: str | None = None
    frame_id: str | int | None = None
    region: tuple[int, int, int, int] | None = None

    def __post_init__(self) -> None:
        _strict_text(self.source, "evidence.source")
        _confidence(self.confidence, "evidence.confidence")
        _strict_bool(self.authoritative, "evidence.authoritative")
        _optional_text(self.observed_text, "evidence.observed_text")
        _optional_text(self.image_path, "evidence.image_path")
        if self.frame_id is not None and (
            isinstance(self.frame_id, bool)
            or not isinstance(self.frame_id, (str, int))
            or (isinstance(self.frame_id, str) and not self.frame_id.strip())
        ):
            raise ValueError("evidence.frame_id must be non-empty text or an integer")
        if self.region is not None:
            if not isinstance(self.region, tuple) or len(self.region) != 4:
                raise ValueError("evidence.region must be an (x, y, width, height) tuple")
            x, y, width, height = self.region
            _strict_int(x, "evidence.region.x")
            _strict_int(y, "evidence.region.y")
            _strict_int(width, "evidence.region.width", minimum=1)
            _strict_int(height, "evidence.region.height", minimum=1)

    def blocking_reasons(
        self,
        field: str,
        *,
        confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
    ) -> tuple[str, ...]:
        _confidence(confidence_threshold, "confidence_threshold")
        reasons: list[str] = []
        if not self.authoritative:
            reasons.append(f"evidence-not-authoritative:{field}")
        if self.confidence < confidence_threshold:
            reasons.append(
                f"low-confidence:{field}:{self.confidence:.6g}"
            )
        return tuple(reasons)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "confidence": self.confidence,
            "authoritative": self.authoritative,
            "observed_text": self.observed_text,
            "image_path": self.image_path,
            "frame_id": self.frame_id,
            "region": list(self.region) if self.region is not None else None,
        }
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RecognitionEvidence":
        raw_region = payload.get("region")
        region: tuple[int, int, int, int] | None = None
        if raw_region is not None:
            if not isinstance(raw_region, list) or len(raw_region) != 4:
                raise ValueError("evidence.region must be a four-item array")
            region = tuple(
                _strict_int(
                    value,
                    f"evidence.region[{index}]",
                    minimum=1 if index >= 2 else 0,
                )
                for index, value in enumerate(raw_region)
            )  # type: ignore[assignment]
        return cls(
            source=_strict_text(payload.get("source"), "evidence.source"),
            confidence=_confidence(
                payload.get("confidence"), "evidence.confidence"
            ),
            authoritative=_strict_bool(
                payload.get("authoritative"), "evidence.authoritative"
            ),
            observed_text=_optional_text(
                payload.get("observed_text"), "evidence.observed_text"
            ),
            image_path=_optional_text(
                payload.get("image_path"), "evidence.image_path"
            ),
            frame_id=payload.get("frame_id"),
            region=region,
        )


@dataclass(frozen=True, slots=True)
class ObservedMemoryAbility:
    ability_id: str | None
    level: int | None
    identity_evidence: RecognitionEvidence
    level_evidence: RecognitionEvidence

    def __post_init__(self) -> None:
        _optional_text(self.ability_id, "memory ability id")
        _optional_int(self.level, "memory ability level", minimum=0)
        if not isinstance(self.identity_evidence, RecognitionEvidence):
            raise TypeError("identity_evidence must be RecognitionEvidence")
        if not isinstance(self.level_evidence, RecognitionEvidence):
            raise TypeError("level_evidence must be RecognitionEvidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ability_id": self.ability_id,
            "level": self.level,
            "identity_evidence": self.identity_evidence.to_dict(),
            "level_evidence": self.level_evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ObservedMemoryAbility":
        identity = payload.get("identity_evidence")
        level_evidence = payload.get("level_evidence")
        if not isinstance(identity, Mapping) or not isinstance(
            level_evidence, Mapping
        ):
            raise ValueError("memory ability evidence fields must be objects")
        return cls(
            ability_id=_optional_text(
                payload.get("ability_id"), "memory ability id"
            ),
            level=_optional_int(
                payload.get("level"), "memory ability level", minimum=0
            ),
            identity_evidence=RecognitionEvidence.from_dict(identity),
            level_evidence=RecognitionEvidence.from_dict(level_evidence),
        )


@dataclass(frozen=True, slots=True)
class SupportLoadoutSlot:
    slot: int
    origin: str
    card_id: str | None
    level: int | None
    origin_evidence: RecognitionEvidence
    identity_evidence: RecognitionEvidence
    level_evidence: RecognitionEvidence

    def __post_init__(self) -> None:
        _strict_int(self.slot, "support slot", minimum=1)
        if self.origin not in SUPPORT_ORIGINS:
            raise ValueError(f"unsupported support origin: {self.origin}")
        _optional_text(self.card_id, "support card id")
        _optional_int(self.level, "support card level", minimum=1)
        for label, evidence in (
            ("origin_evidence", self.origin_evidence),
            ("identity_evidence", self.identity_evidence),
            ("level_evidence", self.level_evidence),
        ):
            if not isinstance(evidence, RecognitionEvidence):
                raise TypeError(f"{label} must be RecognitionEvidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "origin": self.origin,
            "card_id": self.card_id,
            "level": self.level,
            "origin_evidence": self.origin_evidence.to_dict(),
            "identity_evidence": self.identity_evidence.to_dict(),
            "level_evidence": self.level_evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SupportLoadoutSlot":
        raw_evidence = tuple(
            payload.get(name)
            for name in (
                "origin_evidence",
                "identity_evidence",
                "level_evidence",
            )
        )
        if not all(isinstance(value, Mapping) for value in raw_evidence):
            raise ValueError("support slot evidence fields must be objects")
        origin_evidence, identity_evidence, level_evidence = raw_evidence
        return cls(
            slot=_strict_int(payload.get("slot"), "support slot", minimum=1),
            origin=_strict_text(payload.get("origin"), "support origin"),
            card_id=_optional_text(payload.get("card_id"), "support card id"),
            level=_optional_int(
                payload.get("level"), "support card level", minimum=1
            ),
            origin_evidence=RecognitionEvidence.from_dict(origin_evidence),
            identity_evidence=RecognitionEvidence.from_dict(identity_evidence),
            level_evidence=RecognitionEvidence.from_dict(level_evidence),
        )


@dataclass(frozen=True, slots=True)
class MemoryLoadoutSlot:
    slot: int
    memory_id: str | None
    memory_gift_id: str | None
    abilities: tuple[ObservedMemoryAbility, ...]
    abilities_complete: bool
    identity_evidence: RecognitionEvidence
    abilities_evidence: RecognitionEvidence

    def __post_init__(self) -> None:
        _strict_int(self.slot, "memory slot", minimum=1)
        _optional_text(self.memory_id, "memory id")
        _optional_text(self.memory_gift_id, "memory gift id")
        if not isinstance(self.abilities, tuple) or not all(
            isinstance(ability, ObservedMemoryAbility)
            for ability in self.abilities
        ):
            raise TypeError("abilities must be a tuple of ObservedMemoryAbility")
        _strict_bool(self.abilities_complete, "abilities_complete")
        if not isinstance(self.identity_evidence, RecognitionEvidence):
            raise TypeError("identity_evidence must be RecognitionEvidence")
        if not isinstance(self.abilities_evidence, RecognitionEvidence):
            raise TypeError("abilities_evidence must be RecognitionEvidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "memory_id": self.memory_id,
            "memory_gift_id": self.memory_gift_id,
            "abilities": [ability.to_dict() for ability in self.abilities],
            "abilities_complete": self.abilities_complete,
            "identity_evidence": self.identity_evidence.to_dict(),
            "abilities_evidence": self.abilities_evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MemoryLoadoutSlot":
        raw_abilities = payload.get("abilities")
        identity = payload.get("identity_evidence")
        abilities_evidence = payload.get("abilities_evidence")
        if not isinstance(raw_abilities, list) or not all(
            isinstance(value, Mapping) for value in raw_abilities
        ):
            raise ValueError("memory abilities must be an array of objects")
        if not isinstance(identity, Mapping) or not isinstance(
            abilities_evidence, Mapping
        ):
            raise ValueError("memory slot evidence fields must be objects")
        return cls(
            slot=_strict_int(payload.get("slot"), "memory slot", minimum=1),
            memory_id=_optional_text(payload.get("memory_id"), "memory id"),
            memory_gift_id=_optional_text(
                payload.get("memory_gift_id"), "memory gift id"
            ),
            abilities=tuple(
                ObservedMemoryAbility.from_dict(value)
                for value in raw_abilities
            ),
            abilities_complete=_strict_bool(
                payload.get("abilities_complete"), "abilities_complete"
            ),
            identity_evidence=RecognitionEvidence.from_dict(identity),
            abilities_evidence=RecognitionEvidence.from_dict(
                abilities_evidence
            ),
        )


class LoadoutBlockedError(RuntimeError):
    """Raised when an observed loadout cannot safely affect shadow state."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = _deduplicate(reasons)
        if not self.reasons:
            raise ValueError("LoadoutBlockedError requires at least one reason")
        super().__init__("loadout blocked: " + "; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class LoadoutResolution:
    memory_abilities: tuple[MemoryAbilitySelection, ...]
    support_cards: tuple[SupportCardSelection, ...]
    passive_sources: tuple[PassiveSource, ...]
    blocking_reasons: tuple[str, ...]

    @property
    def safe_to_apply(self) -> bool:
        return not self.blocking_reasons

    def require_safe(self) -> tuple[PassiveSource, ...]:
        if self.blocking_reasons:
            raise LoadoutBlockedError(self.blocking_reasons)
        return self.passive_sources


@dataclass(frozen=True, slots=True)
class LoadoutSnapshot:
    run_id: str
    captured_at: str
    source: str
    support_cards: tuple[SupportLoadoutSlot, ...]
    memories: tuple[MemoryLoadoutSlot, ...]
    complete: bool
    observation_authoritative: bool
    produce_id: str | None = None
    schema_version: int = LOADOUT_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_text(self.run_id, "run_id")
        _validate_timestamp(self.captured_at, "captured_at")
        _strict_text(self.source, "source")
        _optional_text(self.produce_id, "produce_id")
        _strict_bool(self.complete, "complete")
        _strict_bool(
            self.observation_authoritative, "observation_authoritative"
        )
        if self.schema_version != LOADOUT_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported loadout schema version: {self.schema_version}"
            )
        if not isinstance(self.support_cards, tuple) or not all(
            isinstance(slot, SupportLoadoutSlot)
            for slot in self.support_cards
        ):
            raise TypeError("support_cards must be a tuple of SupportLoadoutSlot")
        if not isinstance(self.memories, tuple) or not all(
            isinstance(slot, MemoryLoadoutSlot) for slot in self.memories
        ):
            raise TypeError("memories must be a tuple of MemoryLoadoutSlot")
        if tuple(slot.slot for slot in self.support_cards) != tuple(
            range(1, SUPPORT_CARD_COUNT + 1)
        ):
            raise ValueError("loadout must contain support slots 1..6 exactly once")
        if tuple(slot.slot for slot in self.memories) != tuple(
            range(1, MEMORY_COUNT + 1)
        ):
            raise ValueError("loadout must contain memory slots 1..4 exactly once")
        owned = sum(
            slot.origin == SUPPORT_ORIGIN_OWNED for slot in self.support_cards
        )
        borrowed = sum(
            slot.origin == SUPPORT_ORIGIN_BORROWED for slot in self.support_cards
        )
        if owned != OWNED_SUPPORT_COUNT or borrowed != BORROWED_SUPPORT_COUNT:
            raise ValueError("loadout must contain five owned and one borrowed support card")

    def observation_blocking_reasons(
        self,
        *,
        confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
    ) -> tuple[str, ...]:
        _confidence(confidence_threshold, "confidence_threshold")
        reasons: list[str] = []
        if not self.complete:
            reasons.append("snapshot-incomplete")
        if not self.observation_authoritative:
            reasons.append("snapshot-not-authoritative")

        for slot in self.support_cards:
            prefix = f"support:{slot.slot}"
            if slot.card_id is None:
                reasons.append(f"missing-card-id:{prefix}")
            if slot.level is None:
                reasons.append(f"missing-level:{prefix}")
            reasons.extend(
                slot.origin_evidence.blocking_reasons(
                    f"{prefix}:origin",
                    confidence_threshold=confidence_threshold,
                )
            )
            reasons.extend(
                slot.identity_evidence.blocking_reasons(
                    f"{prefix}:identity",
                    confidence_threshold=confidence_threshold,
                )
            )
            reasons.extend(
                slot.level_evidence.blocking_reasons(
                    f"{prefix}:level",
                    confidence_threshold=confidence_threshold,
                )
            )

        for slot in self.memories:
            prefix = f"memory:{slot.slot}"
            if slot.memory_id is None:
                reasons.append(f"missing-memory-id:{prefix}")
            if not slot.abilities_complete:
                reasons.append(f"incomplete-abilities:{prefix}")
            reasons.extend(
                slot.identity_evidence.blocking_reasons(
                    f"{prefix}:identity",
                    confidence_threshold=confidence_threshold,
                )
            )
            reasons.extend(
                slot.abilities_evidence.blocking_reasons(
                    f"{prefix}:abilities-completeness",
                    confidence_threshold=confidence_threshold,
                )
            )
            for index, ability in enumerate(slot.abilities, start=1):
                ability_prefix = f"{prefix}:ability:{index}"
                if ability.ability_id is None:
                    reasons.append(f"missing-ability-id:{ability_prefix}")
                if ability.level is None:
                    reasons.append(f"missing-level:{ability_prefix}")
                reasons.extend(
                    ability.identity_evidence.blocking_reasons(
                        f"{ability_prefix}:identity",
                        confidence_threshold=confidence_threshold,
                    )
                )
                reasons.extend(
                    ability.level_evidence.blocking_reasons(
                        f"{ability_prefix}:level",
                        confidence_threshold=confidence_threshold,
                    )
                )
        return _deduplicate(reasons)

    def is_observation_authoritative(
        self,
        *,
        confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
    ) -> bool:
        return not self.observation_blocking_reasons(
            confidence_threshold=confidence_threshold
        )

    def audit_resolution(
        self,
        catalog: MasterPassiveCatalog,
        *,
        confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
    ) -> LoadoutResolution:
        """Resolve every selection while accumulating all safety blockers."""

        if not isinstance(catalog, MasterPassiveCatalog):
            raise TypeError("catalog must be MasterPassiveCatalog")
        reasons = list(
            self.observation_blocking_reasons(
                confidence_threshold=confidence_threshold
            )
        )
        memory_selections: list[MemoryAbilitySelection] = []
        support_selections: list[SupportCardSelection] = []
        sources: list[PassiveSource] = []

        for memory in self.memories:
            for index, ability in enumerate(memory.abilities, start=1):
                if ability.ability_id is None or ability.level is None:
                    continue
                selection = MemoryAbilitySelection(
                    ability.ability_id, ability.level
                )
                memory_selections.append(selection)
                try:
                    sources.append(
                        catalog.resolve_memory_ability(
                            selection.id, selection.level
                        )
                    )
                except KeyError:
                    reasons.append(
                        "unknown-memory-ability:"
                        f"memory:{memory.slot}:ability:{index}:"
                        f"{selection.id}:level-{selection.level}"
                    )

        for slot in self.support_cards:
            if slot.card_id is None or slot.level is None:
                continue
            selection = SupportCardSelection(slot.card_id, slot.level)
            support_selections.append(selection)
            try:
                sources.extend(
                    catalog.resolve_support_card(selection.id, selection.level)
                )
            except KeyError:
                reasons.append(
                    "unknown-support-card:"
                    f"support:{slot.slot}:{selection.id}:level-{selection.level}"
                )

        # Runtime support is intentionally authoritative here.  The catalog's
        # fixed whitelist predates start-lesson/start-audition status-enchant
        # contracts; rejecting those at this layer would make a rule that the
        # runtime can safely install look unknown.  The runtime still carries
        # forward every structural, probabilistic, ranged, or unsupported
        # catalog reason as an explicit blocker.
        from .passive_runtime import resolve_passive_runtime

        runtime = resolve_passive_runtime(tuple(sources))
        reasons.extend(
            "unsupported-passive:"
            f"{block.source_kind}:{block.source_id}:"
            f"{block.code}:{block.reason}"
            for block in runtime.blocking_rules
        )

        return LoadoutResolution(
            memory_abilities=tuple(memory_selections),
            support_cards=tuple(support_selections),
            passive_sources=tuple(sources),
            blocking_reasons=_deduplicate(reasons),
        )

    def resolve_passives(
        self,
        catalog: MasterPassiveCatalog,
        *,
        confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
    ) -> tuple[PassiveSource, ...]:
        """Return all passives only when the complete loadout is safe."""

        return self.audit_resolution(
            catalog, confidence_threshold=confidence_threshold
        ).require_safe()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "produce_id": self.produce_id,
            "captured_at": self.captured_at,
            "source": self.source,
            "complete": self.complete,
            "observation_authoritative": self.observation_authoritative,
            "support_cards": [slot.to_dict() for slot in self.support_cards],
            "memories": [slot.to_dict() for slot in self.memories],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LoadoutSnapshot":
        if payload.get("schema_version") != LOADOUT_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported loadout snapshot schema version")
        raw_support = payload.get("support_cards")
        raw_memories = payload.get("memories")
        if not isinstance(raw_support, list) or not all(
            isinstance(value, Mapping) for value in raw_support
        ):
            raise ValueError("support_cards must be an array of objects")
        if not isinstance(raw_memories, list) or not all(
            isinstance(value, Mapping) for value in raw_memories
        ):
            raise ValueError("memories must be an array of objects")
        return cls(
            run_id=_strict_text(payload.get("run_id"), "run_id"),
            produce_id=_optional_text(payload.get("produce_id"), "produce_id"),
            captured_at=_validate_timestamp(
                payload.get("captured_at"), "captured_at"
            ),
            source=_strict_text(payload.get("source"), "source"),
            support_cards=tuple(
                sorted(
                    (SupportLoadoutSlot.from_dict(value) for value in raw_support),
                    key=lambda slot: slot.slot,
                )
            ),
            memories=tuple(
                sorted(
                    (MemoryLoadoutSlot.from_dict(value) for value in raw_memories),
                    key=lambda slot: slot.slot,
                )
            ),
            complete=_strict_bool(payload.get("complete"), "complete"),
            observation_authoritative=_strict_bool(
                payload.get("observation_authoritative"),
                "observation_authoritative",
            ),
            schema_version=LOADOUT_SNAPSHOT_SCHEMA_VERSION,
        )


def build_loadout_snapshot(
    *,
    run_id: str,
    captured_at: str,
    source: str,
    support_cards: Iterable[SupportLoadoutSlot],
    memories: Iterable[MemoryLoadoutSlot],
    complete: bool,
    observation_authoritative: bool,
    produce_id: str | None = None,
) -> LoadoutSnapshot:
    return LoadoutSnapshot(
        run_id=run_id,
        produce_id=produce_id,
        captured_at=captured_at,
        source=source,
        support_cards=tuple(sorted(support_cards, key=lambda slot: slot.slot)),
        memories=tuple(sorted(memories, key=lambda slot: slot.slot)),
        complete=complete,
        observation_authoritative=observation_authoritative,
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_loadout_snapshot(
    snapshot: LoadoutSnapshot,
    path: Path = DEFAULT_LOADOUT_SNAPSHOT_PATH,
) -> None:
    if not isinstance(snapshot, LoadoutSnapshot):
        raise TypeError("snapshot must be LoadoutSnapshot")
    _atomic_write_json(Path(path), snapshot.to_dict())


def load_loadout_snapshot(
    path: Path = DEFAULT_LOADOUT_SNAPSHOT_PATH,
) -> LoadoutSnapshot | None:
    path = Path(path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("loadout snapshot must be a JSON object")
    return LoadoutSnapshot.from_dict(payload)
