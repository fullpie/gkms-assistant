"""Exact ordered Hand/Deck/Grave/Lost movement for native audition replay.

This module is deliberately only a *zone-mechanics foundation*.  It does not
model card effects and must not be treated as solver-ready.  Local-save schema
v5 retains the effect-relevant per-instance runtime fields plus the complete
root runtime; migrated v2/v3/v4 evidence has an explicit unknown component and
is rejected.  The card digest prevents equality/hash
from collapsing instances whose native runtime differs.

The supported scope contains only settled ordered Hand/Deck/Grave/Lost zones.
Hold, playing, removed, future, and past pools are rejected.  Within that
scope the operations below preserve instance identity, model the native
``ResetSupportUpgrade`` performed while moving played/end-turn cards, and
reproduce the ordinary Grave-to-Deck shuffle using the audited Android 3.2.3
RNG helper.  Other effect/runtime updates remain the responsibility of a
future exact executor.  No solver, controller, or game process is referenced.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import re

from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    AuditionLocalSaveStateEvidence,
    LocalSaveExamCard,
    LocalSaveExamCardRuntimeState,
)
from .exam_native_rng import (
    INT32_MAX,
    INT32_MIN,
    UINT32_MASK,
    FixedDeckOrderError,
    next_range,
    shuffle_unfixed_pool,
)


NATIVE_ORDERED_ZONE_SCHEMA_VERSION = 1
MINIMUM_LOCAL_SAVE_SCHEMA_VERSION = 5
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class NativeOrderedZoneError(ValueError):
    """The bounded native ordered-zone model cannot proceed exactly."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _sha256(value: object, name: str) -> str:
    result = _text(value, name)
    if _SHA256_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return result


def _integer(
    value: object,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        if maximum is None:
            raise ValueError(f"{name} must be at least {minimum}")
        raise ValueError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def _fixed_deck_order(value: object, *, guid: str) -> int:
    if value is None:
        raise NativeOrderedZoneError(
            "unknown-fixed-deck-order",
            f"card {guid!r} has no observed top-level fixedDeckOrder",
        )
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("fixed_deck_order must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise ValueError("fixed_deck_order must fit in a signed 32-bit integer")
    if value > 0:
        raise NativeOrderedZoneError(
            "fixed-order-shuffle-unsupported",
            f"card {guid!r} requires the native fixed-order sort path",
        )
    return value


@dataclass(frozen=True, slots=True)
class NativeOrderedZoneEvidenceBinding:
    """Immutable provenance shared by every simulated descendant state."""

    schema_version: int
    local_save_schema_version: int
    run_id: str
    step_context_id: str
    step_context_digest: str
    session_transition_id: str
    zone_checkpoint_digest: str
    local_save_source_sha256: str
    local_save_evidence_digest: str
    runtime_evidence_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != NATIVE_ORDERED_ZONE_SCHEMA_VERSION:
            raise ValueError("unsupported native ordered-zone binding schema")
        _integer(
            self.local_save_schema_version,
            "local_save_schema_version",
            minimum=MINIMUM_LOCAL_SAVE_SCHEMA_VERSION,
        )
        for name in ("run_id", "step_context_id", "session_transition_id"):
            _text(getattr(self, name), name)
        for name in (
            "step_context_digest",
            "zone_checkpoint_digest",
            "local_save_source_sha256",
            "local_save_evidence_digest",
            "runtime_evidence_digest",
        ):
            _sha256(getattr(self, name), name)

    @classmethod
    def from_evidence(
        cls,
        evidence: AuditionLocalSaveStateEvidence,
        *,
        runtime_evidence_digest: str,
    ) -> "NativeOrderedZoneEvidenceBinding":
        if not isinstance(evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
        if evidence.schema_version < MINIMUM_LOCAL_SAVE_SCHEMA_VERSION:
            raise NativeOrderedZoneError(
                "local-save-schema-too-old",
                "ordered native facts require LocalSave schema v5",
            )
        if not evidence.state.has_complete_root_runtime_state:
            raise NativeOrderedZoneError(
                "unknown-root-runtime-state",
                "ordered native facts require complete schema-v5 root runtime",
            )
        if not evidence.state.is_native_actionable_settled:
            raise NativeOrderedZoneError(
                "not-native-actionable-settled",
                "ordered native facts require an actionable settled source",
            )
        return cls(
            schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
            local_save_schema_version=evidence.schema_version,
            run_id=evidence.run_id,
            step_context_id=evidence.step_context_id,
            step_context_digest=evidence.step_context_digest,
            session_transition_id=evidence.session_transition_id,
            zone_checkpoint_digest=evidence.zone_checkpoint_digest,
            local_save_source_sha256=evidence.source_sha256,
            local_save_evidence_digest=evidence.digest(),
            runtime_evidence_digest=_sha256(
                runtime_evidence_digest, "runtime_evidence_digest"
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "local_save_schema_version": self.local_save_schema_version,
            "run_id": self.run_id,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "session_transition_id": self.session_transition_id,
            "zone_checkpoint_digest": self.zone_checkpoint_digest,
            "local_save_source_sha256": self.local_save_source_sha256,
            "local_save_evidence_digest": self.local_save_evidence_digest,
            "runtime_evidence_digest": self.runtime_evidence_digest,
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _runtime_state_digest(
    *,
    base_upgrade: int,
    temporary_upgrade: int,
    effective_upgrade: int,
    support_upgrade_ids: Sequence[str],
    fixed_deck_order: int,
    runtime_state: LocalSaveExamCardRuntimeState,
) -> str:
    payload = {
        "base_upgrade": base_upgrade,
        "temporary_upgrade": temporary_upgrade,
        "effective_upgrade": effective_upgrade,
        "support_upgrade_ids": list(support_upgrade_ids),
        "fixed_deck_order": fixed_deck_order,
        "runtime_state": runtime_state.to_dict(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeOrderedCardInstance:
    """Strongly typed v4 card runtime used by bounded zone mechanics."""

    guid: str
    card_id: str
    base_upgrade: int
    temporary_upgrade: int
    effective_upgrade: int
    support_upgrade_ids: tuple[str, ...]
    fixed_deck_order: int
    runtime_state: LocalSaveExamCardRuntimeState
    runtime_state_digest: str

    def __post_init__(self) -> None:
        guid = _text(self.guid, "guid")
        _text(self.card_id, "card_id")
        _integer(self.base_upgrade, "base_upgrade", maximum=3)
        _integer(self.temporary_upgrade, "temporary_upgrade", maximum=3)
        _integer(self.effective_upgrade, "effective_upgrade", maximum=3)
        support_ids = tuple(self.support_upgrade_ids)
        if any(not isinstance(value, str) or not value.strip() for value in support_ids):
            raise ValueError("support_upgrade_ids must contain non-empty text")
        if len(set(support_ids)) != len(support_ids):
            raise ValueError("support_upgrade_ids must not contain duplicates")
        object.__setattr__(self, "support_upgrade_ids", support_ids)
        expected_upgrade = (
            self.base_upgrade + self.temporary_upgrade + len(support_ids)
        )
        if self.effective_upgrade != expected_upgrade:
            raise ValueError(
                "effective_upgrade is not explained by base, temporary, and support upgrades"
            )
        fixed_order = _fixed_deck_order(self.fixed_deck_order, guid=guid)
        if not isinstance(self.runtime_state, LocalSaveExamCardRuntimeState):
            raise NativeOrderedZoneError(
                "unknown-card-runtime-state",
                f"card {guid!r} lacks exact LocalSave schema-v4 runtime state",
            )
        supplied_digest = _sha256(
            self.runtime_state_digest, "runtime_state_digest"
        )
        expected_digest = _runtime_state_digest(
            base_upgrade=self.base_upgrade,
            temporary_upgrade=self.temporary_upgrade,
            effective_upgrade=self.effective_upgrade,
            support_upgrade_ids=support_ids,
            fixed_deck_order=fixed_order,
            runtime_state=self.runtime_state,
        )
        if supplied_digest != expected_digest:
            raise NativeOrderedZoneError(
                "card-runtime-digest-mismatch",
                f"card {guid!r} runtime payload does not match its digest",
            )

    @classmethod
    def from_local_save(
        cls,
        card: LocalSaveExamCard,
    ) -> "NativeOrderedCardInstance":
        if not isinstance(card, LocalSaveExamCard):
            raise TypeError("card must be LocalSaveExamCard")
        if card.runtime_state is None or card.runtime_state_digest is None:
            raise NativeOrderedZoneError(
                "unknown-card-runtime-state",
                f"card {card.guid!r} was migrated from schema v2/v3 or is incomplete",
            )
        fixed_order = _fixed_deck_order(
            getattr(card, "fixed_deck_order", None),
            guid=card.guid,
        )
        return cls(
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            support_upgrade_ids=card.support_upgrade_ids,
            fixed_deck_order=fixed_order,
            runtime_state=card.runtime_state,
            runtime_state_digest=card.runtime_state_digest,
        )

    @property
    def persistent_identity(self) -> tuple[str, str, int, int]:
        return (
            self.guid,
            self.card_id,
            self.base_upgrade,
            self.fixed_deck_order,
        )

    def reset_support_upgrade(self) -> "NativeOrderedCardInstance":
        """Mirror native ResetSupportUpgrade without guessing other updates."""

        if not self.support_upgrade_ids:
            return self
        effective_upgrade = self.base_upgrade + self.temporary_upgrade
        runtime_digest = _runtime_state_digest(
            base_upgrade=self.base_upgrade,
            temporary_upgrade=self.temporary_upgrade,
            effective_upgrade=effective_upgrade,
            support_upgrade_ids=(),
            fixed_deck_order=self.fixed_deck_order,
            runtime_state=self.runtime_state,
        )
        return replace(
            self,
            effective_upgrade=effective_upgrade,
            support_upgrade_ids=(),
            runtime_state_digest=runtime_digest,
        )

    def increment_play_count(self) -> "NativeOrderedCardInstance":
        """Increment the exact v4 play count and recompute the runtime digest."""

        if self.runtime_state.play_count >= INT32_MAX:
            raise NativeOrderedZoneError(
                "play-count-overflow",
                "native signed play_count cannot be incremented exactly",
            )
        runtime_state = replace(
            self.runtime_state,
            play_count=self.runtime_state.play_count + 1,
        )
        runtime_digest = _runtime_state_digest(
            base_upgrade=self.base_upgrade,
            temporary_upgrade=self.temporary_upgrade,
            effective_upgrade=self.effective_upgrade,
            support_upgrade_ids=self.support_upgrade_ids,
            fixed_deck_order=self.fixed_deck_order,
            runtime_state=runtime_state,
        )
        return replace(
            self,
            runtime_state=runtime_state,
            runtime_state_digest=runtime_digest,
        )

    def install_support_upgrades(
        self,
        ordered_ids: Sequence[str],
    ) -> "NativeOrderedCardInstance":
        """Install already-resolved support IDs in native result order.

        This method consumes no RNG.  Eligibility, roll order, and successful
        IDs must be resolved by the separate native-support RNG executor.
        """

        if isinstance(ordered_ids, (str, bytes)):
            raise TypeError("ordered_ids must be a sequence of support IDs")
        try:
            additions = tuple(ordered_ids)
        except TypeError as error:
            raise TypeError(
                "ordered_ids must be a sequence of support IDs"
            ) from error
        existing = set(self.support_upgrade_ids)
        seen: set[str] = set()
        for support_id in additions:
            _text(support_id, "support upgrade ID")
            if support_id in existing or support_id in seen:
                raise NativeOrderedZoneError(
                    "duplicate-support-upgrade",
                    f"support ID {support_id!r} is already installed or repeated",
                )
            if self.effective_upgrade + len(seen) >= 3:
                raise NativeOrderedZoneError(
                    "support-upgrade-overflow",
                    "installing the ordered support results would exceed upgrade 3",
                )
            seen.add(support_id)
        if not additions:
            return self
        support_ids = (*self.support_upgrade_ids, *additions)
        effective_upgrade = self.effective_upgrade + len(additions)
        runtime_digest = _runtime_state_digest(
            base_upgrade=self.base_upgrade,
            temporary_upgrade=self.temporary_upgrade,
            effective_upgrade=effective_upgrade,
            support_upgrade_ids=support_ids,
            fixed_deck_order=self.fixed_deck_order,
            runtime_state=self.runtime_state,
        )
        return replace(
            self,
            effective_upgrade=effective_upgrade,
            support_upgrade_ids=support_ids,
            runtime_state_digest=runtime_digest,
        )

    def set_temporary_upgrade(
        self,
        temporary_upgrade: int,
    ) -> "NativeOrderedCardInstance":
        """Apply the native temporary-upgrade field and refresh its digest."""

        _integer(temporary_upgrade, "temporary_upgrade", maximum=3)
        effective_upgrade = (
            self.base_upgrade
            + temporary_upgrade
            + len(self.support_upgrade_ids)
        )
        if effective_upgrade > 3:
            raise NativeOrderedZoneError(
                "temporary-upgrade-overflow",
                f"effective upgrade would be {effective_upgrade}",
            )
        runtime_digest = _runtime_state_digest(
            base_upgrade=self.base_upgrade,
            temporary_upgrade=temporary_upgrade,
            effective_upgrade=effective_upgrade,
            support_upgrade_ids=self.support_upgrade_ids,
            fixed_deck_order=self.fixed_deck_order,
            runtime_state=self.runtime_state,
        )
        return replace(
            self,
            temporary_upgrade=temporary_upgrade,
            effective_upgrade=effective_upgrade,
            runtime_state_digest=runtime_digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "guid": self.guid,
            "card_id": self.card_id,
            "base_upgrade": self.base_upgrade,
            "temporary_upgrade": self.temporary_upgrade,
            "effective_upgrade": self.effective_upgrade,
            "support_upgrade_ids": list(self.support_upgrade_ids),
            "fixed_deck_order": self.fixed_deck_order,
            "runtime_state": self.runtime_state.to_dict(),
            "runtime_state_digest": self.runtime_state_digest,
        }


def canonical_runtime_digest_aggregate(
    cards: Sequence[NativeOrderedCardInstance],
) -> str:
    """Hash the canonical GUID-to-runtime-digest mapping for exact v4 cards."""

    values = _card_tuple(cards, "cards")
    by_guid = _card_map(values, name="runtime digest aggregate")
    payload = {
        "runtime_state_digest_by_guid": [
            {
                "guid": guid,
                "runtime_state_digest": by_guid[guid].runtime_state_digest,
            }
            for guid in sorted(by_guid)
        ]
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class NativeEndTurnDisposition(str, Enum):
    """Explicit static ``isEndTurnLost`` classification for one Hand card."""

    GRAVE = "grave"
    LOST = "lost"


def _card_tuple(value: object, name: str) -> tuple[NativeOrderedCardInstance, ...]:
    try:
        result = tuple(value)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(
            f"{name} must contain NativeOrderedCardInstance values"
        ) from error
    if not all(isinstance(card, NativeOrderedCardInstance) for card in result):
        raise TypeError(f"{name} must contain NativeOrderedCardInstance values")
    return result


def _card_map(
    cards: Sequence[NativeOrderedCardInstance],
    *,
    name: str,
) -> dict[str, NativeOrderedCardInstance]:
    result: dict[str, NativeOrderedCardInstance] = {}
    for card in cards:
        if card.guid in result:
            raise NativeOrderedZoneError(
                "duplicate-guid",
                f"GUID {card.guid!r} occurs more than once in {name}",
            )
        result[card.guid] = card
    return result


@dataclass(frozen=True, slots=True)
class NativeRngAuditEvent:
    """One audited bounded RNG call and its Fisher--Yates swap."""

    consumer: str
    before_state: int
    after_state: int
    minimum: int
    maximum_exclusive: int
    sampled_index: int
    swapped_positions: tuple[int, int]

    def __post_init__(self) -> None:
        _text(self.consumer, "consumer")
        _integer(self.before_state, "before_state", maximum=UINT32_MASK)
        _integer(self.after_state, "after_state", maximum=UINT32_MASK)
        _integer(self.minimum, "minimum")
        _integer(self.maximum_exclusive, "maximum_exclusive", minimum=1)
        if self.minimum >= self.maximum_exclusive:
            raise ValueError("minimum must be less than maximum_exclusive")
        _integer(self.sampled_index, "sampled_index")
        if not self.minimum <= self.sampled_index < self.maximum_exclusive:
            raise ValueError("sampled_index is outside the audited range")
        positions = tuple(self.swapped_positions)
        if len(positions) != 2:
            raise ValueError("swapped_positions must contain exactly two indices")
        for position in positions:
            _integer(position, "swapped position")
        object.__setattr__(self, "swapped_positions", positions)

    def to_dict(self) -> dict[str, object]:
        return {
            "consumer": self.consumer,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "minimum": self.minimum,
            "maximum_exclusive": self.maximum_exclusive,
            "sampled_index": self.sampled_index,
            "swapped_positions": list(self.swapped_positions),
        }


@dataclass(frozen=True, slots=True)
class NativeOrderedDrawTransition:
    """Compact draw result; audit trace is deliberately non-semantic."""

    state: "NativeOrderedZoneState"
    random_state_before: int
    recycled_grave_count: int
    drawn_guids: tuple[str, ...]
    rng_audit_events: tuple[NativeRngAuditEvent, ...] = field(
        default=(), compare=False, hash=False, repr=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.state, NativeOrderedZoneState):
            raise TypeError("state must be NativeOrderedZoneState")
        _integer(
            self.random_state_before,
            "random_state_before",
            maximum=UINT32_MASK,
        )
        _integer(self.recycled_grave_count, "recycled_grave_count")
        drawn = tuple(_text(guid, "drawn_guid") for guid in self.drawn_guids)
        if len(drawn) != len(set(drawn)):
            raise ValueError("drawn_guids must be unique")
        object.__setattr__(self, "drawn_guids", drawn)
        positioned_guids = {card.guid for card in self.state.all_positioned_cards}
        missing_drawn = sorted(set(drawn) - positioned_guids)
        if missing_drawn:
            raise ValueError(
                f"drawn_guids are absent from the result state: {missing_drawn!r}"
            )
        hand_guids = {card.guid for card in self.state.hand}
        if not set(drawn) <= hand_guids:
            raise ValueError("every drawn GUID must settle in the result Hand")
        events = tuple(self.rng_audit_events)
        if not all(isinstance(event, NativeRngAuditEvent) for event in events):
            raise TypeError("rng_audit_events must contain NativeRngAuditEvent values")
        object.__setattr__(self, "rng_audit_events", events)

        expected_count = max(self.recycled_grave_count - 1, 0)
        if len(events) != expected_count:
            raise ValueError(
                "rng_audit_events count does not match the Grave shuffle size"
            )
        cursor = self.random_state_before
        for offset, event in enumerate(events):
            native_count = self.recycled_grave_count - offset
            if event.before_state != cursor:
                raise ValueError("rng_audit_events do not form a state chain")
            if (
                event.minimum != 0
                or event.maximum_exclusive != native_count
                or event.swapped_positions
                != (event.sampled_index, native_count - 1)
            ):
                raise ValueError(
                    "rng_audit_event does not describe descending Fisher-Yates"
                )
            expected_sample, expected_after = next_range(
                event.before_state,
                event.minimum,
                event.maximum_exclusive,
            )
            if (
                event.sampled_index != expected_sample
                or event.after_state != expected_after
            ):
                raise ValueError("rng_audit_event does not match native RNG output")
            cursor = event.after_state
        if cursor != self.state.random_state:
            raise ValueError("audit trace final state does not match result state")

    @property
    def drawn_instances(self) -> tuple[NativeOrderedCardInstance, ...]:
        """Resolve drawn GUIDs to the exact instances in the result state."""

        by_guid = {card.guid: card for card in self.state.all_positioned_cards}
        return tuple(by_guid[guid] for guid in self.drawn_guids)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.to_dict(),
            "random_state_before": self.random_state_before,
            "recycled_grave_count": self.recycled_grave_count,
            "drawn_guids": list(self.drawn_guids),
            "rng_audit_events": [
                event.to_dict() for event in self.rng_audit_events
            ],
        }


@dataclass(frozen=True, slots=True)
class NativeOrderedZoneState:
    """One immutable ordinary-zone state with exact conservation checks."""

    schema_version: int
    binding: NativeOrderedZoneEvidenceBinding
    random_state: int
    card_universe: tuple[NativeOrderedCardInstance, ...]
    hand: tuple[NativeOrderedCardInstance, ...]
    deck: tuple[NativeOrderedCardInstance, ...]
    grave: tuple[NativeOrderedCardInstance, ...]
    lost: tuple[NativeOrderedCardInstance, ...]
    pending_played: NativeOrderedCardInstance | None = None

    def __post_init__(self) -> None:
        if self.schema_version != NATIVE_ORDERED_ZONE_SCHEMA_VERSION:
            raise ValueError("unsupported native ordered-zone state schema")
        if not isinstance(self.binding, NativeOrderedZoneEvidenceBinding):
            raise TypeError("binding must be NativeOrderedZoneEvidenceBinding")
        _integer(self.random_state, "random_state", maximum=UINT32_MASK)

        universe = _card_tuple(self.card_universe, "card_universe")
        canonical_universe = tuple(sorted(universe, key=lambda card: card.guid))
        if universe != canonical_universe:
            raise NativeOrderedZoneError(
                "noncanonical-card-universe",
                "card_universe must be sorted by GUID",
            )
        expected = _card_map(universe, name="card_universe")
        object.__setattr__(self, "card_universe", universe)

        positioned: list[NativeOrderedCardInstance] = []
        for name in ("hand", "deck", "grave", "lost"):
            zone = _card_tuple(getattr(self, name), name)
            object.__setattr__(self, name, zone)
            positioned.extend(zone)
        if self.pending_played is not None:
            if not isinstance(self.pending_played, NativeOrderedCardInstance):
                raise TypeError(
                    "pending_played must be NativeOrderedCardInstance or None"
                )
            positioned.append(self.pending_played)

        actual = _card_map(positioned, name="ordered zones and pending_played")
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            changed = sorted(
                guid
                for guid in set(actual) & set(expected)
                if actual[guid] != expected[guid]
            )
            raise NativeOrderedZoneError(
                "card-conservation-failed",
                f"missing={missing!r}; extra={extra!r}; changed={changed!r}",
            )

    @property
    def all_positioned_cards(self) -> tuple[NativeOrderedCardInstance, ...]:
        pending = () if self.pending_played is None else (self.pending_played,)
        return (*self.hand, *self.deck, *self.grave, *self.lost, *pending)

    def card_universe_digest(self) -> str:
        payload = {"cards": [card.to_dict() for card in self.card_universe]}
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def persistent_card_identity_digest(self) -> str:
        """Digest identities that must survive every runtime reset/move."""

        payload = {
            "cards": [
                {
                    "guid": card.guid,
                    "card_id": card.card_id,
                    "base_upgrade": card.base_upgrade,
                    "fixed_deck_order": card.fixed_deck_order,
                }
                for card in self.card_universe
            ]
        }
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def _replace_universe_card(
        self,
        previous: NativeOrderedCardInstance,
        updated: NativeOrderedCardInstance,
    ) -> tuple[NativeOrderedCardInstance, ...]:
        if previous.persistent_identity != updated.persistent_identity:
            raise NativeOrderedZoneError(
                "persistent-card-identity-changed",
                "a runtime update changed GUID/card/base/fixed-order identity",
            )
        replacements = 0
        result: list[NativeOrderedCardInstance] = []
        for card in self.card_universe:
            if card.guid == previous.guid:
                if card != previous:
                    raise NativeOrderedZoneError(
                        "stale-card-runtime-update",
                        f"card {previous.guid!r} no longer matches the universe",
                    )
                result.append(updated)
                replacements += 1
            else:
                result.append(card)
        if replacements != 1:
            raise NativeOrderedZoneError(
                "card-universe-replacement-failed",
                f"expected one universe card for GUID {previous.guid!r}",
            )
        return tuple(result)

    def remove_hand(
        self, index: int
    ) -> tuple["NativeOrderedZoneState", NativeOrderedCardInstance]:
        """Move one Hand card to the pending played slot and return it."""

        _integer(index, "index")
        if self.pending_played is not None:
            raise NativeOrderedZoneError(
                "pending-played-card-exists",
                "place the current pending card before removing another Hand card",
            )
        if index >= len(self.hand):
            raise IndexError("Hand index is out of range")
        card = self.hand[index]
        next_state = replace(
            self,
            hand=(*self.hand[:index], *self.hand[index + 1 :]),
            pending_played=card,
        )
        return next_state, card

    def replace_hand_runtime(
        self,
        previous: NativeOrderedCardInstance,
        updated: NativeOrderedCardInstance,
    ) -> "NativeOrderedZoneState":
        """Replace one exact Hand runtime while preserving persistent identity."""

        if not isinstance(previous, NativeOrderedCardInstance) or not isinstance(
            updated, NativeOrderedCardInstance
        ):
            raise TypeError("previous and updated must be card instances")
        if previous.persistent_identity != updated.persistent_identity:
            raise NativeOrderedZoneError(
                "persistent-card-identity-changed",
                "a Hand runtime update changed GUID/card/base/fixed-order identity",
            )
        matching_indices = tuple(
            index
            for index, card in enumerate(self.hand)
            if card.guid == previous.guid
        )
        if not matching_indices:
            raise NativeOrderedZoneError(
                "card-not-in-hand",
                f"GUID {previous.guid!r} is not in Hand",
            )
        if len(matching_indices) != 1:
            raise NativeOrderedZoneError(
                "duplicate-guid",
                f"GUID {previous.guid!r} occurs more than once in Hand",
            )
        index = matching_indices[0]
        if self.hand[index] != previous:
            raise NativeOrderedZoneError(
                "stale-card-runtime-update",
                f"Hand runtime for GUID {previous.guid!r} has already changed",
            )
        return replace(
            self,
            card_universe=self._replace_universe_card(previous, updated),
            hand=(*self.hand[:index], updated, *self.hand[index + 1 :]),
        )

    def replace_pending_runtime(
        self,
        previous: NativeOrderedCardInstance,
        updated: NativeOrderedCardInstance,
    ) -> "NativeOrderedZoneState":
        """Install an externally proven non-zone runtime update.

        This mechanics module does not infer play-count, status, growth,
        stamina, customization, or effect changes.  A future exact executor
        may supply a fully validated replacement before the pending card is
        placed; persistent identity may not change.
        """

        if not isinstance(previous, NativeOrderedCardInstance) or not isinstance(
            updated, NativeOrderedCardInstance
        ):
            raise TypeError("previous and updated must be card instances")
        if self.pending_played is None or self.pending_played != previous:
            raise NativeOrderedZoneError(
                "pending-played-card-mismatch",
                "previous does not equal the current pending played card",
            )
        return replace(
            self,
            card_universe=self._replace_universe_card(previous, updated),
            pending_played=updated,
        )

    def _append_pending(
        self,
        card: NativeOrderedCardInstance,
        *,
        destination: str,
    ) -> "NativeOrderedZoneState":
        if not isinstance(card, NativeOrderedCardInstance):
            raise TypeError("card must be NativeOrderedCardInstance")
        if self.pending_played is None:
            raise NativeOrderedZoneError(
                "no-pending-played-card",
                "remove a Hand card before placing the played card",
            )
        if card != self.pending_played:
            raise NativeOrderedZoneError(
                "pending-played-card-mismatch",
                "the supplied instance is not the pending played card",
            )
        settled = self.pending_played.reset_support_upgrade()
        zone = getattr(self, destination)
        return replace(
            self,
            card_universe=self._replace_universe_card(
                self.pending_played,
                settled,
            ),
            **{destination: (*zone, settled)},
            pending_played=None,
        )

    def append_played_to_grave(
        self, card: NativeOrderedCardInstance
    ) -> "NativeOrderedZoneState":
        """Reset its support upgrades, then append it to ordered Grave."""

        return self._append_pending(card, destination="grave")

    def append_played_to_lost(
        self, card: NativeOrderedCardInstance
    ) -> "NativeOrderedZoneState":
        """Reset its support upgrades, then append it to ordered Lost."""

        return self._append_pending(card, destination="lost")

    def close_turn(
        self,
        dispositions: Mapping[str, NativeEndTurnDisposition],
    ) -> "NativeOrderedZoneState":
        """Reset support upgrades and route Hand by explicit isEndTurnLost.

        The mapping must classify every current Hand GUID exactly once and
        may not contain any other GUID.  Relative Hand order is retained
        independently in the Grave and Lost suffixes.
        """

        if self.pending_played is not None:
            raise NativeOrderedZoneError(
                "pending-played-card-exists",
                "the played card must settle before the turn can close",
            )
        if not isinstance(dispositions, Mapping):
            raise TypeError("dispositions must be a mapping")
        classified: dict[str, NativeEndTurnDisposition] = {}
        for raw_guid, disposition in dispositions.items():
            guid = _text(raw_guid, "end-turn disposition GUID")
            if guid in classified:
                raise NativeOrderedZoneError(
                    "duplicate-end-turn-disposition",
                    f"GUID {guid!r} is classified more than once",
                )
            if not isinstance(disposition, NativeEndTurnDisposition):
                raise TypeError(
                    "end-turn dispositions must be NativeEndTurnDisposition values"
                )
            classified[guid] = disposition
        expected_guids = {card.guid for card in self.hand}
        classified_guids = set(classified)
        if classified_guids != expected_guids:
            raise NativeOrderedZoneError(
                "end-turn-disposition-guid-mismatch",
                f"missing={sorted(expected_guids - classified_guids)!r}; "
                f"extra={sorted(classified_guids - expected_guids)!r}",
            )
        if not self.hand:
            return self

        universe_by_guid = {card.guid: card for card in self.card_universe}
        grave_suffix: list[NativeOrderedCardInstance] = []
        lost_suffix: list[NativeOrderedCardInstance] = []
        for card in self.hand:
            settled = card.reset_support_upgrade()
            universe_by_guid[card.guid] = settled
            if classified[card.guid] is NativeEndTurnDisposition.LOST:
                lost_suffix.append(settled)
            else:
                grave_suffix.append(settled)
        universe = tuple(
            universe_by_guid[card.guid] for card in self.card_universe
        )
        return replace(
            self,
            card_universe=universe,
            hand=(),
            grave=(*self.grave, *grave_suffix),
            lost=(*self.lost, *lost_suffix),
        )

    def draw_to_hand(
        self, count: int
    ) -> NativeOrderedDrawTransition:
        """Draw an exact Deck prefix, recycling and shuffling Grave if needed.

        Existing Hand order is preserved and drawn instances are appended in
        draw order.  When the Deck prefix is too short, all of it is drawn
        first, then ordered Grave is moved to Deck and shuffled once using the
        saved uint32 state.  A request larger than Deck+Grave is rejected.
        """

        _integer(count, "count")
        if count == 0:
            return NativeOrderedDrawTransition(
                state=self,
                random_state_before=self.random_state,
                recycled_grave_count=0,
                drawn_guids=(),
            )
        if count > len(self.deck) + len(self.grave):
            raise NativeOrderedZoneError(
                "insufficient-draw-cards",
                f"requested {count}, but Deck+Grave contains "
                f"{len(self.deck) + len(self.grave)} cards",
            )

        if count <= len(self.deck):
            drawn = self.deck[:count]
            return NativeOrderedDrawTransition(
                state=replace(
                    self,
                    hand=(*self.hand, *drawn),
                    deck=self.deck[count:],
                ),
                random_state_before=self.random_state,
                recycled_grave_count=0,
                drawn_guids=tuple(card.guid for card in drawn),
            )

        deck_prefix = self.deck
        grave_orders = tuple(card.fixed_deck_order for card in self.grave)
        # Recheck at the exact shuffle boundary.  The constructor already
        # enforces this, but keeping the boundary guard makes the safety
        # invariant explicit if instances ever come from another adapter.
        for card in self.grave:
            _fixed_deck_order(card.fixed_deck_order, guid=card.guid)
        audit_events: list[NativeRngAuditEvent] = []
        audit_state = self.random_state
        for native_count in range(len(self.grave), 1, -1):
            sampled_index, after_state = next_range(
                audit_state,
                0,
                native_count,
            )
            audit_events.append(
                NativeRngAuditEvent(
                    consumer=(
                        "ExamCardMoveController.ReplaceGraveToDeck"
                        "->ExamCardPoolModel.Shuffle"
                    ),
                    before_state=audit_state,
                    after_state=after_state,
                    minimum=0,
                    maximum_exclusive=native_count,
                    sampled_index=sampled_index,
                    swapped_positions=(sampled_index, native_count - 1),
                )
            )
            audit_state = after_state
        try:
            shuffled, next_random_state = shuffle_unfixed_pool(
                self.grave,
                self.random_state,
                fixed_deck_orders=grave_orders,
            )
        except FixedDeckOrderError as error:
            raise NativeOrderedZoneError(
                "fixed-order-shuffle-unsupported",
                "Grave requires the native fixed-order sort path",
            ) from error
        if next_random_state != audit_state:
            raise AssertionError("native shuffle helper and RNG audit trace diverged")

        remainder_count = count - len(deck_prefix)
        shuffled_tuple = tuple(shuffled)
        drawn = (*deck_prefix, *shuffled_tuple[:remainder_count])
        return NativeOrderedDrawTransition(
            state=replace(
                self,
                random_state=next_random_state,
                hand=(*self.hand, *drawn),
                deck=shuffled_tuple[remainder_count:],
                grave=(),
            ),
            random_state_before=self.random_state,
            recycled_grave_count=len(self.grave),
            drawn_guids=tuple(card.guid for card in drawn),
            rng_audit_events=tuple(audit_events),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "binding": self.binding.to_dict(),
            "random_state": self.random_state,
            "card_universe": [card.to_dict() for card in self.card_universe],
            **{
                name: [card.to_dict() for card in getattr(self, name)]
                for name in ("hand", "deck", "grave", "lost")
            },
            "pending_played": (
                None
                if self.pending_played is None
                else self.pending_played.to_dict()
            ),
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def build_native_ordered_zone_state(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    runtime_evidence_digest: str,
) -> NativeOrderedZoneState:
    """Build the bounded mechanics state from exact evidence.

    Every card must contain exact schema-v4 card runtime state and the source
    must contain complete schema-v5 root runtime.  The caller-provided
    ``runtime_evidence_digest`` must equal the canonical aggregate calculated
    directly from those v4 cards; it cannot substitute invented per-GUID
    digests for migrated v2/v3 unknowns.
    """

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    if (
        evidence.schema_version != AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION
        or evidence.schema_version < MINIMUM_LOCAL_SAVE_SCHEMA_VERSION
    ):
        raise NativeOrderedZoneError(
            "local-save-schema-too-old",
            "native ordered zones require exact LocalSave schema v5 evidence",
        )

    state = evidence.state
    if not state.has_complete_root_runtime_state:
        raise NativeOrderedZoneError(
            "unknown-root-runtime-state",
            "native ordered zones require complete schema-v5 root runtime",
        )
    if state.exam_type != 1:
        raise NativeOrderedZoneError(
            "unsupported-exam-type",
            "native ordered-zone foundation currently supports audition exam type 1",
        )
    if state.phase != 6:
        raise NativeOrderedZoneError(
            "unsupported-exam-phase",
            "native ordered-zone evidence must be in settled Main phase 6",
        )
    if state.extra_turn != 0:
        raise NativeOrderedZoneError(
            "unsupported-extra-turn",
            "extra-turn replay is outside the bounded ordered-zone foundation",
        )
    if state.is_turn_card_play_end:
        raise NativeOrderedZoneError(
            "turn-play-already-ended",
            "the source must be a settled actionable turn state",
        )
    unsupported: list[str] = []
    if state.zones.hold:
        unsupported.append("Hold")
    if state.playing_card is not None:
        unsupported.append("playingCard")
    if state.removed_cards and not state.removed_cards_are_positioned_tombstones:
        unsupported.append("removedCardList")
    if state.future_deck:
        unsupported.append("futureDeckList")
    if state.past_deck is None:
        unsupported.append("pastDeckList(unknown)")
    elif state.past_deck:
        unsupported.append("pastDeckList")
    if unsupported:
        raise NativeOrderedZoneError(
            "unsupported-auxiliary-zone",
            f"bounded mechanics cannot discard {', '.join(unsupported)}",
        )
    if not state.is_native_actionable_settled:
        raise NativeOrderedZoneError(
            "not-native-actionable-settled",
            "the source is not a native actionable settled state",
        )

    local_zones = (
        state.zones.hand,
        state.zones.deck,
        state.zones.grave,
        state.zones.lost,
    )
    local_cards = tuple(card for zone in local_zones for card in zone)
    if not local_cards:
        raise NativeOrderedZoneError(
            "empty-card-universe",
            "an audition ordered-zone state must contain at least one card",
        )
    local_guids = tuple(card.guid for card in local_cards)
    if len(local_guids) != len(set(local_guids)):
        raise NativeOrderedZoneError(
            "duplicate-guid",
            "the local-save ordered zones contain duplicate GUIDs",
        )

    def convert(zone: Sequence[LocalSaveExamCard]) -> tuple[NativeOrderedCardInstance, ...]:
        return tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in zone
        )

    hand, deck, grave, lost = (convert(zone) for zone in local_zones)
    all_cards = (*hand, *deck, *grave, *lost)
    universe = tuple(sorted(all_cards, key=lambda card: card.guid))
    expected_runtime_evidence_digest = canonical_runtime_digest_aggregate(
        universe
    )
    supplied_runtime_evidence_digest = _sha256(
        runtime_evidence_digest,
        "runtime_evidence_digest",
    )
    if supplied_runtime_evidence_digest != expected_runtime_evidence_digest:
        raise NativeOrderedZoneError(
            "runtime-evidence-digest-mismatch",
            "runtime_evidence_digest is not the canonical LocalSave v5 card "
            "GUID-to-runtime aggregate",
        )
    return NativeOrderedZoneState(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        binding=NativeOrderedZoneEvidenceBinding.from_evidence(
            evidence,
            runtime_evidence_digest=expected_runtime_evidence_digest,
        ),
        random_state=state.random_state,
        card_universe=universe,
        hand=hand,
        deck=deck,
        grave=grave,
        lost=lost,
    )


__all__ = [
    "MINIMUM_LOCAL_SAVE_SCHEMA_VERSION",
    "NATIVE_ORDERED_ZONE_SCHEMA_VERSION",
    "NativeEndTurnDisposition",
    "NativeOrderedCardInstance",
    "NativeOrderedDrawTransition",
    "NativeOrderedZoneError",
    "NativeOrderedZoneEvidenceBinding",
    "NativeOrderedZoneState",
    "NativeRngAuditEvent",
    "build_native_ordered_zone_state",
    "canonical_runtime_digest_aggregate",
]
