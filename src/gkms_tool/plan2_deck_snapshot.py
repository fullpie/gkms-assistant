"""Pure, exact Plan2 deck/zone snapshot projection.

The runtime and save adapters in this repository intentionally keep more
information than the Plan2 scalar kernel needs.  This module is a read-only
view over that *already observed* information.  It never opens a save file,
reads a process, performs a catalog lookup, or fills in a card that is not in
the supplied state.

Two sources are accepted:

* :class:`~gkms_tool.plan2_native_horizon.Plan2NativeHorizonState`, whose
  ``NativeOrderedZoneState`` is the settled, ordered native projection; and
* :class:`~gkms_tool.audition_local_save_state.LocalSaveExamState` (or its
  evidence envelope), which retains the five save zones and the auxiliary
  ``playingCard``/``removedCardList``/future/past groups.

The output is deliberately a data object.  ``to_dict`` returns deep copies of
raw JSON values so a caller can render a panel without being able to mutate the
state that was projected.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    AuditionLocalSaveStateEvidence,
    LocalSaveExamCard,
    LocalSaveExamState,
)
from .audition_native_ordered_zones import NativeOrderedCardInstance, NativeOrderedZoneState
from .loadout_snapshot import LoadoutSnapshot
from .plan2_native_horizon import Plan2NativeHorizonState


PLAN2_DECK_SNAPSHOT_SCHEMA_VERSION = 1


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer or None")
    return value


def _tuple_text(values: Iterable[object], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise ValueError(f"{label} must contain non-empty text")
    return result


def _copy_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy one source-owned mapping and keep the top-level read-only."""

    return MappingProxyType(deepcopy(dict(value)))


def _thaw(value: object) -> object:
    """Return a JSON-friendly copy of proxy/tuple values."""

    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    return deepcopy(value)


@dataclass(frozen=True, slots=True)
class Plan2CardSnapshot:
    """One observed card instance in one ordered zone."""

    zone: str
    zone_order: int
    guid: str
    card_id: str
    base_upgrade: int | None
    temporary_upgrade: int | None
    effective_upgrade: int | None
    support_upgrade_ids: tuple[str, ...] = ()
    fixed_deck_order: int | None = None
    play_count: int | None = None
    runtime: Mapping[str, Any] | None = None
    source: str = "unknown"

    def __post_init__(self) -> None:
        _text(self.zone, "zone")
        if isinstance(self.zone_order, bool) or not isinstance(self.zone_order, int):
            raise TypeError("zone_order must be an integer")
        _text(self.guid, "guid")
        _text(self.card_id, "card_id")
        for name in ("base_upgrade", "temporary_upgrade", "effective_upgrade", "fixed_deck_order", "play_count"):
            _optional_int(getattr(self, name), name)
        object.__setattr__(self, "support_upgrade_ids", _tuple_text(self.support_upgrade_ids, "support_upgrade_ids"))
        if self.runtime is not None:
            if not isinstance(self.runtime, Mapping):
                raise TypeError("runtime must be a mapping or None")
            object.__setattr__(self, "runtime", _copy_mapping(self.runtime))
        _text(self.source, "source")

    def to_dict(self) -> dict[str, object]:
        return {
            "zone": self.zone,
            "zone_order": self.zone_order,
            "guid": self.guid,
            "card_id": self.card_id,
            "base_upgrade": self.base_upgrade,
            "temporary_upgrade": self.temporary_upgrade,
            "effective_upgrade": self.effective_upgrade,
            "support_upgrade_ids": list(self.support_upgrade_ids),
            "fixed_deck_order": self.fixed_deck_order,
            "play_count": self.play_count,
            "runtime": None if self.runtime is None else _thaw(self.runtime),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan2CardSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("card snapshot must be an object")
        raw_support = payload.get("support_upgrade_ids", [])
        if not isinstance(raw_support, list):
            raise ValueError("card snapshot support_upgrade_ids must be a list")
        raw_runtime = payload.get("runtime")
        if raw_runtime is not None and not isinstance(raw_runtime, Mapping):
            raise ValueError("card snapshot runtime must be an object or null")
        return cls(
            zone=_text(payload.get("zone"), "zone"),
            zone_order=payload.get("zone_order"),  # type: ignore[arg-type]
            guid=_text(payload.get("guid"), "guid"),
            card_id=_text(payload.get("card_id"), "card_id"),
            base_upgrade=_optional_int(payload.get("base_upgrade"), "base_upgrade"),
            temporary_upgrade=_optional_int(payload.get("temporary_upgrade"), "temporary_upgrade"),
            effective_upgrade=_optional_int(payload.get("effective_upgrade"), "effective_upgrade"),
            support_upgrade_ids=tuple(raw_support),  # type: ignore[arg-type]
            fixed_deck_order=_optional_int(payload.get("fixed_deck_order"), "fixed_deck_order"),
            play_count=_optional_int(payload.get("play_count"), "play_count"),
            runtime=raw_runtime,
            source=_text(payload.get("source", "unknown"), "source"),
        )


@dataclass(frozen=True, slots=True)
class Plan2PItemSnapshot:
    """Observed P-item counters; ``None`` means the source did not expose it."""

    item_id: str | None
    remaining_uses: int | None
    max_uses: int | None = None
    fire_count: int | None = None
    reaction_count: int | None = None
    enchantment_id: str | None = None
    source: str = "unknown"
    raw: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name in ("item_id", "enchantment_id"):
            _optional_text(getattr(self, name), name)
        for name in ("remaining_uses", "max_uses", "fire_count", "reaction_count"):
            _optional_int(getattr(self, name), name)
        _text(self.source, "source")
        if self.raw is not None:
            if not isinstance(self.raw, Mapping):
                raise TypeError("raw must be a mapping or None")
            object.__setattr__(self, "raw", _copy_mapping(self.raw))

    def to_dict(self) -> dict[str, object]:
        return {
            "item_id": self.item_id,
            "remaining_uses": self.remaining_uses,
            "max_uses": self.max_uses,
            "fire_count": self.fire_count,
            "reaction_count": self.reaction_count,
            "enchantment_id": self.enchantment_id,
            "source": self.source,
            "raw": None if self.raw is None else _thaw(self.raw),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan2PItemSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("P item snapshot must be an object")
        raw = payload.get("raw")
        if raw is not None and not isinstance(raw, Mapping):
            raise ValueError("P item raw value must be an object or null")
        return cls(
            item_id=_optional_text(payload.get("item_id"), "item_id"),
            remaining_uses=_optional_int(payload.get("remaining_uses"), "remaining_uses"),
            max_uses=_optional_int(payload.get("max_uses"), "max_uses"),
            fire_count=_optional_int(payload.get("fire_count"), "fire_count"),
            reaction_count=_optional_int(payload.get("reaction_count"), "reaction_count"),
            enchantment_id=_optional_text(payload.get("enchantment_id"), "enchantment_id"),
            source=_text(payload.get("source", "unknown"), "source"),
            raw=raw,
        )


@dataclass(frozen=True, slots=True)
class Plan2PassiveSnapshot:
    """Observed memory/support identity and level summary."""

    kind: str
    slot: int | None
    identity: str | None
    level: int | None
    gift_id: str | None = None
    ability_ids: tuple[str, ...] = ()
    ability_levels: tuple[int | None, ...] = ()
    origin: str | None = None
    complete: bool | None = None
    authoritative: bool | None = None
    source: str = "unknown"
    raw: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"memory", "support"}:
            raise ValueError("passive kind must be memory or support")
        _optional_int(self.slot, "slot")
        _optional_text(self.identity, "identity")
        _optional_int(self.level, "level")
        _optional_text(self.gift_id, "gift_id")
        _optional_text(self.origin, "origin")
        _tuple_text(self.ability_ids, "ability_ids")
        levels = tuple(self.ability_levels)
        if any(value is not None and (isinstance(value, bool) or not isinstance(value, int)) for value in levels):
            raise TypeError("ability_levels must contain integers or None")
        object.__setattr__(self, "ability_ids", tuple(self.ability_ids))
        object.__setattr__(self, "ability_levels", levels)
        for name in ("complete", "authoritative"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be boolean or None")
        _text(self.source, "source")
        if self.raw is not None:
            if not isinstance(self.raw, Mapping):
                raise TypeError("raw must be a mapping or None")
            object.__setattr__(self, "raw", _copy_mapping(self.raw))

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "slot": self.slot,
            "identity": self.identity,
            "level": self.level,
            "gift_id": self.gift_id,
            "ability_ids": list(self.ability_ids),
            "ability_levels": list(self.ability_levels),
            "origin": self.origin,
            "complete": self.complete,
            "authoritative": self.authoritative,
            "source": self.source,
            "raw": None if self.raw is None else _thaw(self.raw),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan2PassiveSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("passive snapshot must be an object")
        raw_ids = payload.get("ability_ids", [])
        raw_levels = payload.get("ability_levels", [])
        if not isinstance(raw_ids, list) or not isinstance(raw_levels, list):
            raise ValueError("passive ability arrays must be lists")
        raw = payload.get("raw")
        if raw is not None and not isinstance(raw, Mapping):
            raise ValueError("passive raw value must be an object or null")
        return cls(
            kind=_text(payload.get("kind"), "kind"),
            slot=_optional_int(payload.get("slot"), "slot"),
            identity=_optional_text(payload.get("identity"), "identity"),
            level=_optional_int(payload.get("level"), "level"),
            gift_id=_optional_text(payload.get("gift_id"), "gift_id"),
            ability_ids=tuple(raw_ids),  # type: ignore[arg-type]
            ability_levels=tuple(raw_levels),  # type: ignore[arg-type]
            origin=_optional_text(payload.get("origin"), "origin"),
            complete=payload.get("complete"),  # type: ignore[arg-type]
            authoritative=payload.get("authoritative"),  # type: ignore[arg-type]
            source=_text(payload.get("source", "unknown"), "source"),
            raw=raw,
        )


def _card_from_source(
    card: LocalSaveExamCard | NativeOrderedCardInstance,
    *,
    zone: str,
    order: int,
    source: str,
) -> Plan2CardSnapshot:
    if isinstance(card, NativeOrderedCardInstance):
        runtime = card.runtime_state.to_dict()
        return Plan2CardSnapshot(
            zone=zone,
            zone_order=order,
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            support_upgrade_ids=card.support_upgrade_ids,
            fixed_deck_order=card.fixed_deck_order,
            play_count=card.runtime_state.play_count,
            runtime=runtime,
            source=source,
        )
    if isinstance(card, LocalSaveExamCard):
        runtime = None if card.runtime_state is None else card.runtime_state.to_dict()
        return Plan2CardSnapshot(
            zone=zone,
            zone_order=order,
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            support_upgrade_ids=card.support_upgrade_ids,
            fixed_deck_order=card.fixed_deck_order,
            play_count=(None if card.runtime_state is None else card.runtime_state.play_count),
            runtime=runtime,
            source=source,
        )
    raise TypeError("card must be LocalSaveExamCard or NativeOrderedCardInstance")


def _snapshot_sequence(
    cards: Sequence[LocalSaveExamCard | NativeOrderedCardInstance],
    *,
    zone: str,
    source: str,
) -> tuple[Plan2CardSnapshot, ...]:
    return tuple(
        _card_from_source(card, zone=zone, order=index, source=source)
        for index, card in enumerate(cards)
    )


def _passives_from_loadout(
    loadout: LoadoutSnapshot | None,
    *,
    unknown: list[str],
    blockers: list[str],
) -> tuple[Plan2PassiveSnapshot, ...]:
    if loadout is None:
        unknown.append("memory-passives:source-not-provided")
        unknown.append("support-passives:source-not-provided")
        return ()
    result: list[Plan2PassiveSnapshot] = []
    for slot in loadout.support_cards:
        result.append(
            Plan2PassiveSnapshot(
                kind="support",
                slot=slot.slot,
                identity=slot.card_id,
                level=slot.level,
                origin=slot.origin,
                complete=(slot.card_id is not None and slot.level is not None),
                authoritative=loadout.observation_authoritative,
                source="loadout-snapshot",
                raw=slot.to_dict(),
            )
        )
    for slot in loadout.memories:
        abilities = tuple(value.ability_id for value in slot.abilities if value.ability_id is not None)
        levels = tuple(value.level for value in slot.abilities)
        result.append(
            Plan2PassiveSnapshot(
                kind="memory",
                slot=slot.slot,
                identity=slot.memory_id,
                level=None,
                gift_id=slot.memory_gift_id,
                ability_ids=abilities,
                ability_levels=levels,
                complete=slot.abilities_complete and slot.memory_id is not None,
                authoritative=loadout.observation_authoritative,
                source="loadout-snapshot",
                raw=slot.to_dict(),
            )
        )
    try:
        blockers.extend(loadout.observation_blocking_reasons())
    except (TypeError, ValueError) as error:
        blockers.append(f"loadout-observation-invalid:{error}")
    return tuple(result)


def _summary_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(deepcopy(dict(value)))


def _blocker_text(value: object) -> str:
    """Render typed bridge blockers without relying on dataclass reprs."""

    code = getattr(value, "code", None)
    detail = getattr(value, "detail", None)
    if isinstance(code, str) and code.strip():
        return code if not isinstance(detail, str) or not detail else f"{code}:{detail}"
    return str(value)


@dataclass(frozen=True, slots=True)
class Plan2DeckSnapshot:
    """Immutable, rendering-friendly projection of one observed Plan2 state."""

    source_kind: str
    source_schema_version: int | None
    card_universe: tuple[Plan2CardSnapshot, ...]
    hand: tuple[Plan2CardSnapshot, ...]
    deck: tuple[Plan2CardSnapshot, ...]
    grave: tuple[Plan2CardSnapshot, ...]
    lost: tuple[Plan2CardSnapshot, ...]
    hold: tuple[Plan2CardSnapshot, ...] = ()
    removed: tuple[Plan2CardSnapshot, ...] = ()
    playing: tuple[Plan2CardSnapshot, ...] = ()
    future_deck: tuple[tuple[Plan2CardSnapshot, ...], ...] = ()
    past_deck: tuple[tuple[Plan2CardSnapshot, ...], ...] | None = ()
    draw_pending_guids: tuple[str, ...] = ()
    drinks: tuple[Any, ...] = ()
    p_items: tuple[Plan2PItemSnapshot, ...] = ()
    passives: tuple[Plan2PassiveSnapshot, ...] = ()
    state_summary: Mapping[str, object] = field(default_factory=dict)
    unknown: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    schema_version: int = PLAN2_DECK_SNAPSHOT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_DECK_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 deck snapshot schema")
        _text(self.source_kind, "source_kind")
        _optional_int(self.source_schema_version, "source_schema_version")
        for name in (
            "card_universe",
            "hand",
            "deck",
            "grave",
            "lost",
            "hold",
            "removed",
            "playing",
        ):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, Plan2CardSnapshot) for value in values):
                raise TypeError(f"{name} must contain Plan2CardSnapshot values")
            object.__setattr__(self, name, values)
        groups = tuple(tuple(group) for group in self.future_deck)
        if any(not isinstance(value, Plan2CardSnapshot) for group in groups for value in group):
            raise TypeError("future_deck must contain card snapshot groups")
        object.__setattr__(self, "future_deck", groups)
        if self.past_deck is not None:
            past = tuple(tuple(group) for group in self.past_deck)
            if any(not isinstance(value, Plan2CardSnapshot) for group in past for value in group):
                raise TypeError("past_deck must contain card snapshot groups")
            object.__setattr__(self, "past_deck", past)
        object.__setattr__(self, "draw_pending_guids", _tuple_text(self.draw_pending_guids, "draw_pending_guids"))
        object.__setattr__(self, "drinks", tuple(deepcopy(value) for value in self.drinks))
        if any(not isinstance(value, Plan2PItemSnapshot) for value in self.p_items):
            raise TypeError("p_items must contain Plan2PItemSnapshot values")
        object.__setattr__(self, "p_items", tuple(self.p_items))
        if any(not isinstance(value, Plan2PassiveSnapshot) for value in self.passives):
            raise TypeError("passives must contain Plan2PassiveSnapshot values")
        object.__setattr__(self, "passives", tuple(self.passives))
        if not isinstance(self.state_summary, Mapping):
            raise TypeError("state_summary must be a mapping")
        object.__setattr__(self, "state_summary", _summary_mapping(self.state_summary))
        object.__setattr__(self, "unknown", _tuple_text(self.unknown, "unknown"))
        object.__setattr__(self, "blockers", _tuple_text(self.blockers, "blockers"))

    @property
    def ordered_deck(self) -> tuple[Plan2CardSnapshot, ...]:
        """The current Deck prefix in draw order."""

        return self.deck

    @property
    def draw(self) -> tuple[Plan2CardSnapshot, ...]:
        return self.deck

    @property
    def discard(self) -> tuple[Plan2CardSnapshot, ...]:
        return self.grave

    @property
    def exile(self) -> tuple[Plan2CardSnapshot, ...]:
        return (*self.lost, *self.removed)

    @property
    def memory_passives(self) -> tuple[Plan2PassiveSnapshot, ...]:
        return tuple(value for value in self.passives if value.kind == "memory")

    @property
    def support_passives(self) -> tuple[Plan2PassiveSnapshot, ...]:
        return tuple(value for value in self.passives if value.kind == "support")

    @property
    def zones(self) -> Mapping[str, object]:
        """Named zones, preserving nested future/past deck groups."""

        return MappingProxyType(
            {
                "hand": self.hand,
                "deck": self.deck,
                "grave": self.grave,
                "lost": self.lost,
                "hold": self.hold,
                "removed": self.removed,
                "playing": self.playing,
                "future_deck": self.future_deck,
                "past_deck": self.past_deck,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind,
            "source_schema_version": self.source_schema_version,
            "card_universe": [value.to_dict() for value in self.card_universe],
            "zones": {
                "hand": [value.to_dict() for value in self.hand],
                "deck": [value.to_dict() for value in self.deck],
                "grave": [value.to_dict() for value in self.grave],
                "lost": [value.to_dict() for value in self.lost],
                "hold": [value.to_dict() for value in self.hold],
                "removed": [value.to_dict() for value in self.removed],
                "playing": [value.to_dict() for value in self.playing],
                "future_deck": [
                    [value.to_dict() for value in group] for group in self.future_deck
                ],
                "past_deck": (
                    None
                    if self.past_deck is None
                    else [[value.to_dict() for value in group] for group in self.past_deck]
                ),
            },
            "draw_pending_guids": list(self.draw_pending_guids),
            "drinks": _thaw(self.drinks),
            "p_items": [value.to_dict() for value in self.p_items],
            "passives": [value.to_dict() for value in self.passives],
            "state_summary": _thaw(self.state_summary),
            "unknown": list(self.unknown),
            "blockers": list(self.blockers),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan2DeckSnapshot":
        if not isinstance(payload, Mapping):
            raise ValueError("Plan2 deck snapshot must be an object")
        if payload.get("schema_version") != PLAN2_DECK_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 deck snapshot schema")
        raw_zones = payload.get("zones")
        if not isinstance(raw_zones, Mapping):
            raise ValueError("snapshot zones must be an object")

        def cards(name: str) -> tuple[Plan2CardSnapshot, ...]:
            raw = raw_zones.get(name, [])
            if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
                raise ValueError(f"snapshot zone {name} must be an array of objects")
            return tuple(Plan2CardSnapshot.from_dict(item) for item in raw)

        def groups(name: str, *, nullable: bool = False) -> tuple[tuple[Plan2CardSnapshot, ...], ...] | None:
            raw = raw_zones.get(name)
            if raw is None and nullable:
                return None
            if not isinstance(raw, list):
                raise ValueError(f"snapshot zone {name} must be an array")
            result: list[tuple[Plan2CardSnapshot, ...]] = []
            for group in raw:
                if not isinstance(group, list) or not all(isinstance(item, Mapping) for item in group):
                    raise ValueError(f"snapshot zone {name} groups must be arrays of objects")
                result.append(tuple(Plan2CardSnapshot.from_dict(item) for item in group))
            return tuple(result)

        raw_universe = payload.get("card_universe", [])
        if not isinstance(raw_universe, list) or not all(isinstance(item, Mapping) for item in raw_universe):
            raise ValueError("snapshot card_universe must be an array of objects")
        raw_pending = payload.get("draw_pending_guids", [])
        raw_drinks = payload.get("drinks", [])
        raw_items = payload.get("p_items", [])
        raw_passives = payload.get("passives", [])
        if not isinstance(raw_pending, list) or not isinstance(raw_drinks, list):
            raise ValueError("snapshot pending/drinks fields must be arrays")
        if not isinstance(raw_items, list) or not all(isinstance(item, Mapping) for item in raw_items):
            raise ValueError("snapshot p_items must be an array of objects")
        if not isinstance(raw_passives, list) or not all(isinstance(item, Mapping) for item in raw_passives):
            raise ValueError("snapshot passives must be an array of objects")
        raw_summary = payload.get("state_summary", {})
        if not isinstance(raw_summary, Mapping):
            raise ValueError("snapshot state_summary must be an object")
        raw_unknown = payload.get("unknown", [])
        raw_blockers = payload.get("blockers", [])
        if not isinstance(raw_unknown, list) or not isinstance(raw_blockers, list):
            raise ValueError("snapshot unknown/blockers must be arrays")
        return cls(
            source_kind=_text(payload.get("source_kind"), "source_kind"),
            source_schema_version=_optional_int(payload.get("source_schema_version"), "source_schema_version"),
            card_universe=tuple(Plan2CardSnapshot.from_dict(item) for item in raw_universe),
            hand=cards("hand"),
            deck=cards("deck"),
            grave=cards("grave"),
            lost=cards("lost"),
            hold=cards("hold"),
            removed=cards("removed"),
            playing=cards("playing"),
            future_deck=groups("future_deck") or (),
            past_deck=groups("past_deck", nullable=True),
            draw_pending_guids=tuple(raw_pending),  # type: ignore[arg-type]
            drinks=tuple(raw_drinks),
            p_items=tuple(Plan2PItemSnapshot.from_dict(item) for item in raw_items),
            passives=tuple(Plan2PassiveSnapshot.from_dict(item) for item in raw_passives),
            state_summary=raw_summary,
            unknown=tuple(raw_unknown),  # type: ignore[arg-type]
            blockers=tuple(raw_blockers),  # type: ignore[arg-type]
            schema_version=payload.get("schema_version"),  # type: ignore[arg-type]
        )


def _empty_snapshot_parts(
    *,
    source_kind: str,
    source_schema_version: int | None,
    loadout: LoadoutSnapshot | None,
    unknown: list[str],
    blockers: list[str],
) -> dict[str, object]:
    passives = _passives_from_loadout(loadout, unknown=unknown, blockers=blockers)
    return {
        "source_kind": source_kind,
        "source_schema_version": source_schema_version,
        "passives": passives,
        "unknown": unknown,
        "blockers": blockers,
    }


def _raw_root_list(
    opaque: Mapping[str, Any] | None,
    field_name: str,
    *,
    unknown: list[str],
    source_label: str,
) -> tuple[Any, ...]:
    if opaque is None:
        unknown.append(f"{field_name}:{source_label}-absent")
        return ()
    raw = opaque.get(field_name)
    if not isinstance(raw, list):
        unknown.append(f"{field_name}:unavailable-shape")
        return ()
    return tuple(deepcopy(raw))


def _local_p_items(
    raw_items: Sequence[Any],
    *,
    unknown: list[str],
) -> tuple[Plan2PItemSnapshot, ...]:
    result: list[Plan2PItemSnapshot] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, Mapping):
            unknown.append(f"itemList[{index}]:not-an-object")
            continue
        raw_id = raw.get("_id")
        item_id = raw_id if isinstance(raw_id, str) and raw_id.strip() else None
        fire = raw.get("_fireCount")
        reaction = raw.get("_reactionCount")
        fire_count = fire if isinstance(fire, int) and not isinstance(fire, bool) else None
        reaction_count = reaction if isinstance(reaction, int) and not isinstance(reaction, bool) else None
        if item_id is None:
            unknown.append(f"itemList[{index}]:missing-_id")
        if fire_count is None:
            unknown.append(f"itemList[{index}]:missing-_fireCount")
        if reaction_count is None:
            unknown.append(f"itemList[{index}]:missing-_reactionCount")
        result.append(
            Plan2PItemSnapshot(
                item_id=item_id,
                # ``_fireCount`` is the native source/fire counter, not the
                # remaining TriggerEffectStatus usage budget.  The latter is
                # held in the active status graph and is intentionally not
                # guessed by this projection.
                remaining_uses=None,
                fire_count=fire_count,
                reaction_count=reaction_count,
                source="local-save.itemList",
                raw=raw,
            )
        )
        unknown.append(f"itemList[{index}]:remaining-uses-unmapped")
    return tuple(result)


def _support_passives_from_root(
    raw_support: Sequence[Any],
    *,
    unknown: list[str],
) -> tuple[Plan2PassiveSnapshot, ...]:
    result: list[Plan2PassiveSnapshot] = []
    for index, raw in enumerate(raw_support):
        if not isinstance(raw, Mapping):
            unknown.append(f"supportCardList[{index}]:not-an-object")
            continue
        identity = raw.get("_supportCardId")
        identity = identity if isinstance(identity, str) and identity.strip() else None
        if identity is None:
            unknown.append(f"supportCardList[{index}]:missing-_supportCardId")
        raw_permils = raw.get("_produceCardUpgradePermil")
        level = None
        if isinstance(raw_permils, list) and raw_permils:
            # Preserve only the observed runtime vector; a support level is
            # not serialized in ExamSaveData and must not be guessed.
            level = None
        result.append(
            Plan2PassiveSnapshot(
                kind="support",
                slot=index + 1,
                identity=identity,
                level=level,
                complete=identity is not None,
                authoritative=True,
                source="local-save.supportCardList",
                raw=raw,
            )
        )
    return tuple(result)


def _snapshot_from_horizon(
    state: Plan2NativeHorizonState,
    *,
    loadout: LoadoutSnapshot | None,
    extra_blockers: Iterable[str],
) -> Plan2DeckSnapshot:
    zones = state.zones
    source = state.source_kind
    unknown: list[str] = [
        "drinks:absent-from-plan2-horizon-state",
    ]
    blockers = [_blocker_text(value) for value in extra_blockers]
    parts = _empty_snapshot_parts(
        source_kind=source,
        source_schema_version=state.schema_version,
        loadout=loadout,
        unknown=unknown,
        blockers=blockers,
    )
    scalar = state.scalar
    summary: dict[str, object] = {
        "current_turn": scalar.current_turn,
        "review": scalar.review,
        "score": scalar.score,
        "review_count_add": scalar.review_count_add,
        "block": scalar.block,
        "card_play_aggressive": scalar.card_play_aggressive,
        "stamina": scalar.stamina,
        "max_stamina": scalar.max_stamina,
        "exam_card_play_count": scalar.exam_card_play_count,
        "turn_card_play_count": scalar.turn_card_play_count,
        "limit_turn": state.limit_turn,
        "extra_turn": state.extra_turn,
        "draw_count": state.draw_count,
        "hand_limit": state.hand_limit,
        "plays_remaining": state.plays_remaining,
        "phase": state.phase,
    }
    if state.opaque_status_queue:
        unknown.extend(f"opaque-status:{value}" for value in state.opaque_status_queue)
    cards_universe = _snapshot_sequence(zones.card_universe, zone="card_universe", source=source)
    return Plan2DeckSnapshot(
        **parts,
        card_universe=cards_universe,
        hand=_snapshot_sequence(zones.hand, zone="hand", source=source),
        deck=_snapshot_sequence(zones.deck, zone="deck", source=source),
        grave=_snapshot_sequence(zones.grave, zone="grave", source=source),
        lost=_snapshot_sequence(zones.lost, zone="lost", source=source),
        p_items=_horizon_p_items(state, unknown=unknown),
        state_summary=summary,
    )


def _horizon_p_items(
    state: Plan2NativeHorizonState,
    *,
    unknown: list[str],
) -> tuple[Plan2PItemSnapshot, ...]:
    runtime = state.item_runtime
    listeners_by_item: dict[str, list[Any]] = {}
    for listener in runtime.listeners:
        listeners_by_item.setdefault(listener.source_item_id, []).append(listener)
    result: list[Plan2PItemSnapshot] = []
    for source in runtime.sources:
        listeners = listeners_by_item.get(source.item_id, [])
        if not listeners:
            result.append(
                Plan2PItemSnapshot(
                    item_id=source.item_id,
                    # A source without an active listener has no exact
                    # TriggerEffectStatus budget in this runtime projection.
                    remaining_uses=None,
                    fire_count=source.fire_count,
                    reaction_count=source.reaction_count,
                    source="plan2-native.item-runtime.source",
                )
            )
            unknown.append(f"item:{source.item_id}:remaining-uses-unmapped")
            continue
        for listener in listeners:
            result.append(
                Plan2PItemSnapshot(
                    item_id=source.item_id,
                    remaining_uses=listener.remaining_uses,
                    max_uses=listener.max_uses,
                    fire_count=source.fire_count,
                    reaction_count=source.reaction_count,
                    enchantment_id=listener.enchantment_id,
                    source="plan2-native.item-runtime.listener",
                )
            )
    return tuple(result)


def _snapshot_from_local_save(
    state: LocalSaveExamState,
    *,
    loadout: LoadoutSnapshot | None,
    extra_blockers: Iterable[str],
    source_kind: str = "local-save",
    source_schema_version: int | None = None,
) -> Plan2DeckSnapshot:
    unknown: list[str] = []
    blockers = [_blocker_text(value) for value in extra_blockers]
    opaque: Mapping[str, Any] | None = None
    if state.root_runtime is not None:
        raw_opaque = state.root_runtime.opaque_fields.to_value()
        if isinstance(raw_opaque, Mapping):
            opaque = raw_opaque
        else:
            unknown.append("root_runtime.opaque_fields:not-an-object")
    else:
        unknown.append("root_runtime:absent")
    parts = _empty_snapshot_parts(
        source_kind=source_kind,
        source_schema_version=source_schema_version,
        loadout=loadout,
        unknown=unknown,
        blockers=blockers,
    )
    if loadout is None:
        # ExamSaveData has no memory list; its support list is still useful as
        # an observed identity summary and never receives an invented level.
        # ``_empty_snapshot_parts`` records both passive sources as unknown
        # before this branch.  The root ``supportCardList`` below is an
        # authoritative support source, so clear only that provisional
        # marker; memory remains explicitly unknown because ExamSaveData does
        # not serialize memory slots.
        if isinstance(opaque, Mapping) and isinstance(
            opaque.get("supportCardList"), list
        ):
            try:
                unknown.remove("support-passives:source-not-provided")
            except ValueError:
                pass
        parts["passives"] = _support_passives_from_root(
            _raw_root_list(opaque, "supportCardList", unknown=unknown, source_label="root-runtime"),
            unknown=unknown,
        )
        unknown.append("memory-passives:absent-from-local-save")
    drinks = _raw_root_list(opaque, "drinkList", unknown=unknown, source_label="root-runtime")
    raw_items = _raw_root_list(opaque, "itemList", unknown=unknown, source_label="root-runtime")
    p_items = _local_p_items(raw_items, unknown=unknown)
    pending = () if state.root_runtime is None else state.root_runtime.draw_card_guid_list
    if state.past_deck is None:
        unknown.append("past_deck:unavailable-after-migration")
    universe_cards: list[Plan2CardSnapshot] = []
    for card in state.all_card_instances:
        universe_cards.append(_card_from_source(card, zone="card_universe", order=len(universe_cards), source=source_kind))
    summary: dict[str, object] = {
        "character_id": state.character_id,
        "setting_id": state.setting_id,
        "current_turn": state.current_turn,
        "limit_turn": state.limit_turn,
        "remain_turn": state.remain_turn,
        "extra_turn": state.extra_turn,
        "score": state.score,
        "stamina": state.stamina,
        "max_stamina": state.max_stamina,
        "block": state.block,
        "exam_card_play_count": state.exam_card_play_count,
        "turn_card_play_count": state.turn_card_play_count,
        "phase": state.phase,
    }
    future_groups = tuple(
        _snapshot_sequence(group, zone=f"future_deck[{index}]", source=source_kind)
        for index, group in enumerate(state.future_deck)
    )
    past_groups = (
        None
        if state.past_deck is None
        else tuple(
            _snapshot_sequence(group, zone=f"past_deck[{index}]", source=source_kind)
            for index, group in enumerate(state.past_deck)
        )
    )
    return Plan2DeckSnapshot(
        **parts,
        card_universe=tuple(universe_cards),
        hand=_snapshot_sequence(state.zones.hand, zone="hand", source=source_kind),
        deck=_snapshot_sequence(state.zones.deck, zone="deck", source=source_kind),
        grave=_snapshot_sequence(state.zones.grave, zone="grave", source=source_kind),
        lost=_snapshot_sequence(state.zones.lost, zone="lost", source=source_kind),
        hold=_snapshot_sequence(state.zones.hold, zone="hold", source=source_kind),
        removed=_snapshot_sequence(state.removed_cards, zone="removed", source=source_kind),
        playing=(
            ()
            if state.playing_card is None
            else (_card_from_source(state.playing_card, zone="playing", order=0, source=source_kind),)
        ),
        future_deck=future_groups,
        past_deck=past_groups,
        draw_pending_guids=pending,
        drinks=drinks,
        p_items=p_items,
        state_summary=summary,
    )


def build_plan2_deck_snapshot(
    source: Plan2NativeHorizonState
    | LocalSaveExamState
    | AuditionLocalSaveStateEvidence,
    *,
    loadout: LoadoutSnapshot | None = None,
    blockers: Iterable[str] = (),
) -> Plan2DeckSnapshot:
    """Project one exact state/save without mutating it or inventing cards."""

    # The native LocalSave bridge and bootstrap result intentionally expose a
    # ``state`` plus typed ``blockers``.  Accept that envelope without importing
    # it here (the bridge imports the horizon module) so this projection stays
    # dependency-light and remains usable by either GUI path.
    if not isinstance(
        source,
        (Plan2NativeHorizonState, LocalSaveExamState, AuditionLocalSaveStateEvidence),
    ) and hasattr(source, "state") and hasattr(source, "blockers"):
        bridge_state = getattr(source, "state")
        bridge_blockers = tuple(getattr(source, "blockers"))
        combined_blockers = (*bridge_blockers, *tuple(blockers))
        if bridge_state is None:
            return _blocked_plan2_snapshot(
                source_kind=type(source).__name__,
                blockers=combined_blockers,
                loadout=loadout,
            )
        return build_plan2_deck_snapshot(
            bridge_state,
            loadout=loadout,
            blockers=combined_blockers,
        )

    if isinstance(source, AuditionLocalSaveStateEvidence):
        return _snapshot_from_local_save(
            source.state,
            loadout=loadout,
            extra_blockers=blockers,
            source_kind="local-save-evidence",
            source_schema_version=source.schema_version,
        )
    if isinstance(source, LocalSaveExamState):
        return _snapshot_from_local_save(
            source,
            loadout=loadout,
            extra_blockers=blockers,
            source_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        )
    if isinstance(source, Plan2NativeHorizonState):
        return _snapshot_from_horizon(
            source,
            loadout=loadout,
            extra_blockers=blockers,
        )
    raise TypeError(
        "source must be Plan2NativeHorizonState, LocalSaveExamState, "
        "or AuditionLocalSaveStateEvidence"
    )


def _blocked_plan2_snapshot(
    *,
    source_kind: str,
    blockers: Iterable[object],
    loadout: LoadoutSnapshot | None,
) -> Plan2DeckSnapshot:
    """Represent a bridge that has no dispatchable state without inventing cards."""

    unknown = ["state:unavailable-from-blocked-bridge"]
    blocker_texts = [_blocker_text(value) for value in blockers]
    passives = _passives_from_loadout(loadout, unknown=unknown, blockers=blocker_texts)
    return Plan2DeckSnapshot(
        source_kind=source_kind,
        source_schema_version=None,
        card_universe=(),
        hand=(),
        deck=(),
        grave=(),
        lost=(),
        p_items=(),
        passives=passives,
        unknown=unknown,
        blockers=blocker_texts,
    )


# Explicit aliases make the small bridge easy to discover from existing GUI
# code without coupling that code to the implementation name.
snapshot_plan2_state = build_plan2_deck_snapshot
plan2_deck_snapshot_from_state = build_plan2_deck_snapshot


__all__ = [
    "PLAN2_DECK_SNAPSHOT_SCHEMA_VERSION",
    "Plan2CardSnapshot",
    "Plan2DeckSnapshot",
    "Plan2PItemSnapshot",
    "Plan2PassiveSnapshot",
    "build_plan2_deck_snapshot",
    "plan2_deck_snapshot_from_state",
    "snapshot_plan2_state",
]
