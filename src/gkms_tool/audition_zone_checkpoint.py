"""Durable, fail-closed card-zone checkpoints for live auditions.

The horizon solver can model an unknown Deck order as a multiset, but a live
runner must preserve the *realized* Deck/Hand/Grave/Lost state between
verified actions.  Rebuilding the zones from the original full-deck panel at
every hand would silently put already played cards back into Deck.  This
module therefore provides a versioned checkpoint and one compare-and-swap
commit value which can only be derived from:

* an authoritative fourteen-card :class:`~gkms_tool.deck_snapshot.DeckSnapshot`
  plus a screenshot-verified opening Hand; or
* the immediately preceding checkpoint plus a committed replay action/result.

No recognition or game control lives here.  Draw identities are explicit in
the committed result.  If those identities cannot be reconciled against the
previous Deck/Grave multisets, evolution fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass, replace
from itertools import combinations, permutations
from pathlib import Path
from typing import Any, Iterable, Mapping

from .deck_snapshot import DeckSnapshot
from .exam_session import SESSION_SCHEMA_VERSION, ExamSession
from .run_deck_snapshot import RunDeckSnapshot


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDITION_ZONE_CHECKPOINT_PATH = (
    PROJECT_ROOT / "var" / "audition_zone_checkpoint.json"
)
AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION = 2
LEGACY_AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION = 1
EXPECTED_INITIAL_DECK_SIZE = 14

ACTION_CARD_PLAY = "card_play"
ACTION_END_TURN = "end_turn"
SUPPORTED_ACTION_KINDS = frozenset({ACTION_CARD_PLAY, ACTION_END_TURN})
DESTINATION_GRAVE = "grave"
DESTINATION_LOST = "lost"
SUPPORTED_DESTINATIONS = frozenset({DESTINATION_GRAVE, DESTINATION_LOST})
REPLAY_STATUS_VERIFIED_PENDING_CHECKPOINT = "verified_pending_checkpoint"
UPGRADE_ZONE_HAND = "hand"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CHECKPOINT_V1_FIELDS = frozenset(
    {
        "schema_version",
        "binding",
        "transition_id",
        "previous_transition_id",
        "session_transition_id",
        "replay_record_id",
        "action_digest",
        "lineage_depth",
        "capture_hashes",
        "baseline_cards",
        "created_cards",
        "zones",
    }
)
_CHECKPOINT_FIELDS = frozenset(
    {
        *_CHECKPOINT_V1_FIELDS,
        "hand_base_cards",
        "active_support_upgrades",
        "support_lineage",
    }
)


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _strict_sha256(value: object, label: str) -> str:
    text = _strict_text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _strict_int(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _require_exact_fields(
    payload: Mapping[str, object], expected: set[str] | frozenset[str], label: str
) -> None:
    actual = set(payload)
    if actual != set(expected):
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(set(expected) - actual)} "
            f"unknown={sorted(actual - set(expected))}"
        )


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, order=True, slots=True)
class ZoneCardRef:
    card_id: str
    upgrade: int = 0

    def __post_init__(self) -> None:
        _strict_text(self.card_id, "card_id")
        _strict_int(self.upgrade, "upgrade")

    @property
    def key(self) -> str:
        return f"{self.card_id}@{self.upgrade}"

    def to_dict(self) -> dict[str, object]:
        return {"card_id": self.card_id, "upgrade": self.upgrade}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ZoneCardRef":
        if not isinstance(payload, Mapping):
            raise ValueError("zone card must be an object")
        _require_exact_fields(payload, {"card_id", "upgrade"}, "zone card")
        return cls(
            card_id=_strict_text(payload["card_id"], "card_id"),
            upgrade=_strict_int(payload["upgrade"], "upgrade"),
        )


def _visible_cards(cards: Iterable[ZoneCardRef]) -> tuple[ZoneCardRef, ...]:
    result = tuple(cards)
    if any(not isinstance(card, ZoneCardRef) for card in result):
        raise TypeError("visible Hand must contain ZoneCardRef values")
    return result


def _hidden_cards(cards: Iterable[ZoneCardRef]) -> tuple[ZoneCardRef, ...]:
    result = tuple(cards)
    if any(not isinstance(card, ZoneCardRef) for card in result):
        raise TypeError("hidden zones must contain ZoneCardRef values")
    return tuple(sorted(result))


def _cards_to_json(cards: Iterable[ZoneCardRef]) -> list[dict[str, object]]:
    return [card.to_dict() for card in cards]


def _cards_from_json(value: object, label: str) -> tuple[ZoneCardRef, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise ValueError(f"{label} must be a list of card objects")
    return tuple(ZoneCardRef.from_dict(item) for item in value)


@dataclass(frozen=True, slots=True)
class AuditionCardZoneState:
    """Visible Hand order and canonical hidden zone multisets."""

    hand: tuple[ZoneCardRef, ...]
    deck: tuple[ZoneCardRef, ...]
    grave: tuple[ZoneCardRef, ...] = ()
    lost: tuple[ZoneCardRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "hand", _visible_cards(self.hand))
        object.__setattr__(self, "deck", _hidden_cards(self.deck))
        object.__setattr__(self, "grave", _hidden_cards(self.grave))
        object.__setattr__(self, "lost", _hidden_cards(self.lost))

    @property
    def all_cards(self) -> tuple[ZoneCardRef, ...]:
        return _hidden_cards((*self.hand, *self.deck, *self.grave, *self.lost))

    def to_dict(self) -> dict[str, object]:
        return {
            "hand": _cards_to_json(self.hand),
            "deck": _cards_to_json(self.deck),
            "grave": _cards_to_json(self.grave),
            "lost": _cards_to_json(self.lost),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AuditionCardZoneState":
        if not isinstance(payload, Mapping):
            raise ValueError("audition card zones must be an object")
        _require_exact_fields(payload, {"hand", "deck", "grave", "lost"}, "zones")
        return cls(
            hand=_cards_from_json(payload["hand"], "zones.hand"),
            deck=_cards_from_json(payload["deck"], "zones.deck"),
            grave=_cards_from_json(payload["grave"], "zones.grave"),
            lost=_cards_from_json(payload["lost"], "zones.lost"),
        )


@dataclass(frozen=True, slots=True)
class AuditionZoneBinding:
    """Immutable run/stage/context identity of one zone lineage."""

    run_id: str
    produce_id: str
    step_type: str
    stage_number: int
    step_context_id: str
    step_context_digest: str
    deck_snapshot_digest: str

    def __post_init__(self) -> None:
        for label in ("run_id", "produce_id", "step_type", "step_context_id"):
            _strict_text(getattr(self, label), label)
        _strict_int(self.stage_number, "stage_number", minimum=1)
        _strict_sha256(self.step_context_digest, "step_context_digest")
        _strict_sha256(self.deck_snapshot_digest, "deck_snapshot_digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "produce_id": self.produce_id,
            "step_type": self.step_type,
            "stage_number": self.stage_number,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "deck_snapshot_digest": self.deck_snapshot_digest,
        }

    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict()).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AuditionZoneBinding":
        if not isinstance(payload, Mapping):
            raise ValueError("audition zone binding must be an object")
        fields = {
            "run_id",
            "produce_id",
            "step_type",
            "stage_number",
            "step_context_id",
            "step_context_digest",
            "deck_snapshot_digest",
        }
        _require_exact_fields(payload, fields, "audition zone binding")
        return cls(
            run_id=_strict_text(payload["run_id"], "run_id"),
            produce_id=_strict_text(payload["produce_id"], "produce_id"),
            step_type=_strict_text(payload["step_type"], "step_type"),
            stage_number=_strict_int(
                payload["stage_number"], "stage_number", minimum=1
            ),
            step_context_id=_strict_text(
                payload["step_context_id"], "step_context_id"
            ),
            step_context_digest=_strict_sha256(
                payload["step_context_digest"], "step_context_digest"
            ),
            deck_snapshot_digest=_strict_sha256(
                payload["deck_snapshot_digest"], "deck_snapshot_digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class AuditionZoneCaptureHashes:
    """Content hashes for the pre-action and terminal stable frame pair."""

    before_sha256: str
    settled_sha256: tuple[str, str]

    def __post_init__(self) -> None:
        _strict_sha256(self.before_sha256, "before_sha256")
        if not isinstance(self.settled_sha256, tuple) or len(self.settled_sha256) != 2:
            raise ValueError("settled_sha256 must contain exactly two frame hashes")
        for index, digest in enumerate(self.settled_sha256):
            _strict_sha256(digest, f"settled_sha256[{index}]")

    def to_dict(self) -> dict[str, object]:
        return {
            "before_sha256": self.before_sha256,
            "settled_sha256": list(self.settled_sha256),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionZoneCaptureHashes":
        if not isinstance(payload, Mapping):
            raise ValueError("capture_hashes must be an object")
        _require_exact_fields(
            payload, {"before_sha256", "settled_sha256"}, "capture_hashes"
        )
        raw_settled = payload["settled_sha256"]
        if not isinstance(raw_settled, list) or len(raw_settled) != 2:
            raise ValueError("settled_sha256 must contain exactly two frame hashes")
        return cls(
            before_sha256=_strict_sha256(
                payload["before_sha256"], "before_sha256"
            ),
            settled_sha256=tuple(
                _strict_sha256(value, f"settled_sha256[{index}]")
                for index, value in enumerate(raw_settled)
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class AuditionZoneAction:
    """The exact UI action selected against one preceding checkpoint."""

    kind: str
    previous_transition_id: str
    binding_digest: str
    hand_index: int | None = None
    card_id: str | None = None
    upgrade: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in SUPPORTED_ACTION_KINDS:
            raise ValueError(f"unsupported zone action kind: {self.kind}")
        _strict_text(self.previous_transition_id, "previous_transition_id")
        _strict_sha256(self.binding_digest, "binding_digest")
        if self.kind == ACTION_CARD_PLAY:
            _strict_int(self.hand_index, "hand_index")
            _strict_text(self.card_id, "card_id")
            _strict_int(self.upgrade, "upgrade")
        elif any(
            value is not None for value in (self.hand_index, self.card_id, self.upgrade)
        ):
            raise ValueError("end_turn action cannot contain card selection fields")

    @property
    def selected_card(self) -> ZoneCardRef | None:
        if self.kind != ACTION_CARD_PLAY:
            return None
        assert self.card_id is not None and self.upgrade is not None
        return ZoneCardRef(self.card_id, self.upgrade)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "previous_transition_id": self.previous_transition_id,
            "binding_digest": self.binding_digest,
            "hand_index": self.hand_index,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
        }

    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditionZoneSupportUpgradeProof:
    """Two-stable-frame proof of one temporary Hand-only card upgrade.

    The source is always the conserved/base card.  ``effective_card`` is only
    the card rule presented by the current visible Hand; it never enters the
    Deck/Grave/Lost conservation ledger.  ``support_delta`` is the observed
    course-support delta (1..3), not a permanent card upgrade count.
    """

    source_card: ZoneCardRef
    effective_card: ZoneCardRef
    zone: str
    support_delta: int
    evidence_ref: str
    settled_sha256: tuple[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.source_card, ZoneCardRef) or not isinstance(
            self.effective_card, ZoneCardRef
        ):
            raise TypeError("support upgrade cards must be ZoneCardRef values")
        if self.zone != UPGRADE_ZONE_HAND:
            raise ValueError("temporary support upgrades are only valid in Hand")
        if self.source_card.card_id != self.effective_card.card_id:
            raise ValueError("support upgrade cannot cross card IDs")
        value = _strict_int(self.support_delta, "support_delta", minimum=1)
        if value > 3:
            raise ValueError("support_delta must be in the range 1..3")
        if self.effective_card.upgrade != self.source_card.upgrade + value:
            raise ValueError(
                "effective support upgrade must equal base upgrade plus support_delta"
            )
        _strict_text(self.evidence_ref, "evidence_ref")
        if not isinstance(self.settled_sha256, tuple) or len(self.settled_sha256) != 2:
            raise ValueError("support upgrade proof requires exactly two stable frames")
        for index, digest in enumerate(self.settled_sha256):
            _strict_sha256(digest, f"support settled_sha256[{index}]")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_card": self.source_card.to_dict(),
            "effective_card": self.effective_card.to_dict(),
            "zone": self.zone,
            "support_delta": self.support_delta,
            "evidence_ref": self.evidence_ref,
            "settled_sha256": list(self.settled_sha256),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionZoneSupportUpgradeProof":
        if not isinstance(payload, Mapping):
            raise ValueError("support upgrade proof must be an object")
        fields = {
            "source_card",
            "effective_card",
            "zone",
            "support_delta",
            "evidence_ref",
            "settled_sha256",
        }
        _require_exact_fields(payload, fields, "support upgrade proof")
        hashes = payload["settled_sha256"]
        if not isinstance(hashes, list) or len(hashes) != 2:
            raise ValueError("support upgrade proof requires exactly two stable frames")
        return cls(
            source_card=ZoneCardRef.from_dict(payload["source_card"]),  # type: ignore[arg-type]
            effective_card=ZoneCardRef.from_dict(payload["effective_card"]),  # type: ignore[arg-type]
            zone=_strict_text(payload["zone"], "zone"),
            support_delta=_strict_int(
                payload["support_delta"], "support_delta", minimum=1
            ),
            evidence_ref=_strict_text(payload["evidence_ref"], "evidence_ref"),
            settled_sha256=tuple(
                _strict_sha256(value, f"support settled_sha256[{index}]")
                for index, value in enumerate(hashes)
            ),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class AuditionZoneSupportLineageEntry:
    """Durable audit record for one observed temporary support application."""

    replay_record_id: str
    action_digest: str
    proof: AuditionZoneSupportUpgradeProof

    def __post_init__(self) -> None:
        _strict_text(self.replay_record_id, "support replay_record_id")
        _strict_sha256(self.action_digest, "support action_digest")
        if not isinstance(self.proof, AuditionZoneSupportUpgradeProof):
            raise TypeError("support lineage proof must be AuditionZoneSupportUpgradeProof")

    def to_dict(self) -> dict[str, object]:
        return {
            "replay_record_id": self.replay_record_id,
            "action_digest": self.action_digest,
            "proof": self.proof.to_dict(),
        }

    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict()).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionZoneSupportLineageEntry":
        if not isinstance(payload, Mapping):
            raise ValueError("support lineage entry must be an object")
        _require_exact_fields(
            payload, {"replay_record_id", "action_digest", "proof"},
            "support lineage entry"
        )
        return cls(
            replay_record_id=_strict_text(
                payload["replay_record_id"], "support replay_record_id"
            ),
            action_digest=_strict_sha256(
                payload["action_digest"], "support action_digest"
            ),
            proof=AuditionZoneSupportUpgradeProof.from_dict(
                payload["proof"]  # type: ignore[arg-type]
            ),
        )


@dataclass(frozen=True, slots=True)
class AuditionZoneActiveSupportUpgrade:
    """Current Hand position affected by one previously proven support event."""

    hand_index: int
    lineage_digest: str

    def __post_init__(self) -> None:
        _strict_int(self.hand_index, "support hand_index")
        _strict_sha256(self.lineage_digest, "support lineage_digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "hand_index": self.hand_index,
            "lineage_digest": self.lineage_digest,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionZoneActiveSupportUpgrade":
        if not isinstance(payload, Mapping):
            raise ValueError("active support upgrade must be an object")
        _require_exact_fields(
            payload, {"hand_index", "lineage_digest"}, "active support upgrade"
        )
        return cls(
            hand_index=_strict_int(payload["hand_index"], "support hand_index"),
            lineage_digest=_strict_sha256(
                payload["lineage_digest"], "support lineage_digest"
            ),
        )


@dataclass(frozen=True, slots=True)
class AuditionZoneTransitionResult:
    """Replay-committed realized card movement for one action.

    ``direct_drawn_cards`` are draws resolved before the played card moves.
    If the action ends the turn, ``next_turn_drawn_cards`` are drawn only
    after the remaining Hand is moved to Grave.  Keeping both phases explicit
    prevents an unobserved end-turn draw from being guessed from the final
    Hand alone.
    """

    replay_record_id: str
    previous_session_transition_id: str
    next_session_transition_id: str
    action_digest: str
    capture_hashes: AuditionZoneCaptureHashes
    replay_status: str
    visible_hand: tuple[ZoneCardRef, ...]
    direct_drawn_cards: tuple[ZoneCardRef, ...] = ()
    next_turn_drawn_cards: tuple[ZoneCardRef, ...] = ()
    created_to_deck: tuple[ZoneCardRef, ...] = ()
    observed_support_upgrades: tuple[AuditionZoneSupportUpgradeProof, ...] = ()
    played_destination: str | None = None
    turn_ended: bool = False

    def __post_init__(self) -> None:
        for label in (
            "replay_record_id",
            "previous_session_transition_id",
            "next_session_transition_id",
        ):
            _strict_text(getattr(self, label), label)
        _strict_sha256(self.action_digest, "action_digest")
        if not isinstance(self.capture_hashes, AuditionZoneCaptureHashes):
            raise TypeError("capture_hashes must be AuditionZoneCaptureHashes")
        if self.replay_status != REPLAY_STATUS_VERIFIED_PENDING_CHECKPOINT:
            raise ValueError(
                "zone transition result must come from a durable "
                "verified_pending_checkpoint replay"
            )
        if self.next_session_transition_id != self.replay_record_id:
            raise ValueError(
                "next_session_transition_id must equal replay_record_id"
            )
        object.__setattr__(self, "visible_hand", _visible_cards(self.visible_hand))
        object.__setattr__(
            self, "direct_drawn_cards", _hidden_cards(self.direct_drawn_cards)
        )
        object.__setattr__(
            self, "next_turn_drawn_cards", _hidden_cards(self.next_turn_drawn_cards)
        )
        object.__setattr__(
            self, "created_to_deck", _hidden_cards(self.created_to_deck)
        )
        upgrades = tuple(self.observed_support_upgrades)
        if not all(isinstance(item, AuditionZoneSupportUpgradeProof) for item in upgrades):
            raise TypeError(
                "observed_support_upgrades must contain support upgrade proofs"
            )
        for proof in upgrades:
            if proof.settled_sha256 != self.capture_hashes.settled_sha256:
                raise ValueError(
                    "support upgrade proof must reference both settled replay frames"
                )
            if proof.source_card in self.created_to_deck:
                raise ValueError(
                    "support upgrade source cannot be created in the same transition"
                )
        if len(
            {(proof.source_card, proof.effective_card) for proof in upgrades}
        ) != len(upgrades):
            raise ValueError("duplicate temporary support upgrade proof")
        object.__setattr__(self, "observed_support_upgrades", upgrades)
        if not isinstance(self.turn_ended, bool):
            raise ValueError("turn_ended must be a boolean")
        if not self.turn_ended and self.next_turn_drawn_cards:
            raise ValueError("next-turn draws require turn_ended=true")
        if self.played_destination is not None and (
            self.played_destination not in SUPPORTED_DESTINATIONS
        ):
            raise ValueError(
                f"unsupported played card destination: {self.played_destination}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "replay_record_id": self.replay_record_id,
            "previous_session_transition_id": self.previous_session_transition_id,
            "next_session_transition_id": self.next_session_transition_id,
            "action_digest": self.action_digest,
            "capture_hashes": self.capture_hashes.to_dict(),
            "replay_status": self.replay_status,
            "visible_hand": _cards_to_json(self.visible_hand),
            "direct_drawn_cards": _cards_to_json(self.direct_drawn_cards),
            "next_turn_drawn_cards": _cards_to_json(self.next_turn_drawn_cards),
            "created_to_deck": _cards_to_json(self.created_to_deck),
            "observed_support_upgrades": [
                proof.to_dict() for proof in self.observed_support_upgrades
            ],
            "played_destination": self.played_destination,
            "turn_ended": self.turn_ended,
        }


@dataclass(frozen=True, slots=True)
class AuditionZoneDerivedObservation:
    """Fail-closed draw/support fields derived from one final visible Hand."""

    direct_drawn_cards: tuple[ZoneCardRef, ...]
    next_turn_drawn_cards: tuple[ZoneCardRef, ...]
    observed_support_upgrades: tuple[AuditionZoneSupportUpgradeProof, ...]

    @property
    def source_drawn_cards(self) -> tuple[ZoneCardRef, ...]:
        """The base refs consumed by the only draw phase in this derivation."""

        return self.next_turn_drawn_cards or self.direct_drawn_cards

    def to_transition_result_kwargs(self) -> dict[str, object]:
        """Fields that can be passed directly to ``AuditionZoneTransitionResult``."""

        return {
            "direct_drawn_cards": self.direct_drawn_cards,
            "next_turn_drawn_cards": self.next_turn_drawn_cards,
            "observed_support_upgrades": self.observed_support_upgrades,
        }


@dataclass(frozen=True, slots=True)
class AuditionZoneCheckpoint:
    schema_version: int
    binding: AuditionZoneBinding
    transition_id: str
    previous_transition_id: str | None
    session_transition_id: str
    replay_record_id: str | None
    action_digest: str | None
    lineage_depth: int
    capture_hashes: AuditionZoneCaptureHashes
    baseline_cards: tuple[ZoneCardRef, ...]
    created_cards: tuple[ZoneCardRef, ...]
    zones: AuditionCardZoneState
    hand_base_cards: tuple[ZoneCardRef, ...] = ()
    active_support_upgrades: tuple[AuditionZoneActiveSupportUpgrade, ...] = ()
    support_lineage: tuple[AuditionZoneSupportLineageEntry, ...] = ()

    @property
    def conserved_base_cards(self) -> tuple[ZoneCardRef, ...]:
        """All zone cards normalized to their persistent/base identities."""

        return _hidden_cards(
            (
                *self.hand_base_cards,
                *self.zones.deck,
                *self.zones.grave,
                *self.zones.lost,
            )
        )

    def __post_init__(self) -> None:
        if self.schema_version != AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported audition zone checkpoint schema version")
        if not isinstance(self.binding, AuditionZoneBinding):
            raise TypeError("binding must be AuditionZoneBinding")
        if not isinstance(self.capture_hashes, AuditionZoneCaptureHashes):
            raise TypeError("capture_hashes must be AuditionZoneCaptureHashes")
        if not isinstance(self.zones, AuditionCardZoneState):
            raise TypeError("zones must be AuditionCardZoneState")
        _strict_text(self.transition_id, "transition_id")
        _strict_text(self.session_transition_id, "session_transition_id")
        if self.transition_id != self.session_transition_id:
            raise ValueError("zone and ExamSession transition IDs must match")
        _strict_int(self.lineage_depth, "lineage_depth")
        object.__setattr__(self, "baseline_cards", _hidden_cards(self.baseline_cards))
        object.__setattr__(self, "created_cards", _hidden_cards(self.created_cards))
        object.__setattr__(self, "hand_base_cards", _visible_cards(self.hand_base_cards))
        active = tuple(self.active_support_upgrades)
        if not all(isinstance(item, AuditionZoneActiveSupportUpgrade) for item in active):
            raise TypeError(
                "active_support_upgrades must contain active support values"
            )
        object.__setattr__(self, "active_support_upgrades", active)
        lineage = tuple(self.support_lineage)
        if not all(isinstance(item, AuditionZoneSupportLineageEntry) for item in lineage):
            raise TypeError("support_lineage must contain support lineage entries")
        object.__setattr__(self, "support_lineage", lineage)
        # Source compatibility for callers that constructed the v1 dataclass
        # directly.  Serialized schema-v2 values must still carry the field.
        if (
            not self.hand_base_cards
            and self.zones.hand
            and not self.active_support_upgrades
            and not self.support_lineage
        ):
            object.__setattr__(self, "hand_base_cards", self.zones.hand)
        if len(self.hand_base_cards) != len(self.zones.hand):
            raise ValueError("hand_base_cards must align one-to-one with visible Hand")
        if len(self.baseline_cards) != EXPECTED_INITIAL_DECK_SIZE:
            raise ValueError(
                f"baseline deck must contain exactly {EXPECTED_INITIAL_DECK_SIZE} cards"
            )
        if self.lineage_depth == 0:
            if any(
                value is not None
                for value in (
                    self.previous_transition_id,
                    self.replay_record_id,
                    self.action_digest,
                )
            ):
                raise ValueError("initial zone checkpoint cannot claim a replay lineage")
            if self.created_cards:
                raise ValueError("initial zone checkpoint cannot contain created cards")
            if self.support_lineage or self.active_support_upgrades:
                raise ValueError("initial zone checkpoint cannot claim support lineage")
        else:
            _strict_text(self.previous_transition_id, "previous_transition_id")
            _strict_text(self.replay_record_id, "replay_record_id")
            _strict_sha256(self.action_digest, "action_digest")
            if self.replay_record_id != self.transition_id:
                raise ValueError("zone transition ID must equal replay_record_id")
            if self.previous_transition_id == self.transition_id:
                raise ValueError("zone transition cannot refer to itself as previous")
        lineage_by_digest: dict[str, AuditionZoneSupportLineageEntry] = {}
        for entry in self.support_lineage:
            entry_digest = entry.digest()
            if entry_digest in lineage_by_digest:
                raise ValueError("duplicate support lineage entry")
            lineage_by_digest[entry_digest] = entry
            if entry.replay_record_id == self.transition_id:
                if entry.action_digest != self.action_digest:
                    raise ValueError(
                        "current support lineage action digest does not match checkpoint"
                    )
                if entry.proof.settled_sha256 != self.capture_hashes.settled_sha256:
                    raise ValueError(
                        "current support lineage frames do not match checkpoint"
                    )
        active_indexes: set[int] = set()
        active_lineage_digests: set[str] = set()
        for value in self.active_support_upgrades:
            if value.hand_index >= len(self.zones.hand):
                raise ValueError("active support hand_index is outside visible Hand")
            if value.hand_index in active_indexes:
                raise ValueError("duplicate active support Hand position")
            active_indexes.add(value.hand_index)
            if value.lineage_digest in active_lineage_digests:
                raise ValueError("support lineage cannot be active in multiple Hand slots")
            active_lineage_digests.add(value.lineage_digest)
            entry = lineage_by_digest.get(value.lineage_digest)
            if entry is None:
                raise ValueError("active support upgrade has no durable lineage entry")
            proof = entry.proof
            if self.hand_base_cards[value.hand_index] != proof.source_card:
                raise ValueError("active support base card does not match lineage")
            if self.zones.hand[value.hand_index] != proof.effective_card:
                raise ValueError("active support effective card does not match lineage")
        for index, (base_card, effective_card) in enumerate(
            zip(self.hand_base_cards, self.zones.hand, strict=True)
        ):
            if index in active_indexes:
                continue
            if base_card != effective_card:
                raise ValueError(
                    "visible Hand differs from base without active support evidence"
                )
        expected = Counter((*self.baseline_cards, *self.created_cards))
        actual = Counter(self.conserved_base_cards)
        if actual != expected:
            missing = expected - actual
            extra = actual - expected
            raise ValueError(
                "card conservation failed: "
                f"missing={_counter_text(missing)} extra={_counter_text(extra)}"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "binding": self.binding.to_dict(),
            "transition_id": self.transition_id,
            "previous_transition_id": self.previous_transition_id,
            "session_transition_id": self.session_transition_id,
            "replay_record_id": self.replay_record_id,
            "action_digest": self.action_digest,
            "lineage_depth": self.lineage_depth,
            "capture_hashes": self.capture_hashes.to_dict(),
            "baseline_cards": _cards_to_json(self.baseline_cards),
            "created_cards": _cards_to_json(self.created_cards),
            "zones": self.zones.to_dict(),
            "hand_base_cards": _cards_to_json(self.hand_base_cards),
            "active_support_upgrades": [
                value.to_dict() for value in self.active_support_upgrades
            ],
            "support_lineage": [entry.to_dict() for entry in self.support_lineage],
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "AuditionZoneCheckpoint":
        if not isinstance(payload, Mapping):
            raise ValueError("audition zone checkpoint root must be an object")
        schema_version = payload.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("unsupported audition zone checkpoint schema version")
        if schema_version == LEGACY_AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION:
            _require_exact_fields(payload, _CHECKPOINT_V1_FIELDS, "zone checkpoint")
        elif schema_version == AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION:
            _require_exact_fields(payload, _CHECKPOINT_FIELDS, "zone checkpoint")
        else:
            raise ValueError("unsupported audition zone checkpoint schema version")
        previous = payload["previous_transition_id"]
        replay = payload["replay_record_id"]
        action_digest = payload["action_digest"]
        for value, label in (
            (previous, "previous_transition_id"),
            (replay, "replay_record_id"),
            (action_digest, "action_digest"),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{label} must be text or null")
        zones = AuditionCardZoneState.from_dict(payload["zones"])  # type: ignore[arg-type]
        if schema_version == LEGACY_AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION:
            hand_base_cards = zones.hand
            active_support_upgrades: tuple[AuditionZoneActiveSupportUpgrade, ...] = ()
            support_lineage: tuple[AuditionZoneSupportLineageEntry, ...] = ()
        else:
            hand_base_cards = _cards_from_json(
                payload["hand_base_cards"], "hand_base_cards"
            )
            raw_active = payload["active_support_upgrades"]
            raw_lineage = payload["support_lineage"]
            if not isinstance(raw_active, list) or not all(
                isinstance(item, Mapping) for item in raw_active
            ):
                raise ValueError("active_support_upgrades must be a list of objects")
            if not isinstance(raw_lineage, list) or not all(
                isinstance(item, Mapping) for item in raw_lineage
            ):
                raise ValueError("support_lineage must be a list of objects")
            active_support_upgrades = tuple(
                AuditionZoneActiveSupportUpgrade.from_dict(item)
                for item in raw_active
            )
            support_lineage = tuple(
                AuditionZoneSupportLineageEntry.from_dict(item)
                for item in raw_lineage
            )
        return cls(
            schema_version=AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION,
            binding=AuditionZoneBinding.from_dict(payload["binding"]),  # type: ignore[arg-type]
            transition_id=_strict_text(payload["transition_id"], "transition_id"),
            previous_transition_id=previous,
            session_transition_id=_strict_text(
                payload["session_transition_id"], "session_transition_id"
            ),
            replay_record_id=replay,
            action_digest=action_digest,
            lineage_depth=_strict_int(payload["lineage_depth"], "lineage_depth"),
            capture_hashes=AuditionZoneCaptureHashes.from_dict(
                payload["capture_hashes"]  # type: ignore[arg-type]
            ),
            baseline_cards=_cards_from_json(
                payload["baseline_cards"], "baseline_cards"
            ),
            created_cards=_cards_from_json(
                payload["created_cards"], "created_cards"
            ),
            zones=zones,
            hand_base_cards=hand_base_cards,
            active_support_upgrades=active_support_upgrades,
            support_lineage=support_lineage,
        )


def _counter_text(counter: Counter[ZoneCardRef]) -> dict[str, int]:
    return {card.key: count for card, count in sorted(counter.items()) if count}


def deck_snapshot_digest(snapshot: DeckSnapshot | RunDeckSnapshot) -> str:
    """Digest the exact deck artifact bound by preflight.

    Live schema-v3 sessions bind the outer :class:`RunDeckSnapshot` artifact,
    while focused/offline callers may only have a bare ``DeckSnapshot``.  The
    builder accepts both but hashes exactly the object supplied; it never
    substitutes one digest form for the other.
    """

    if isinstance(snapshot, RunDeckSnapshot):
        payload = snapshot.to_dict()
    elif isinstance(snapshot, DeckSnapshot):
        snapshot.validate()
        payload = snapshot.to_dict()
    else:
        raise TypeError("snapshot must be DeckSnapshot or RunDeckSnapshot")
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _snapshot_cards(snapshot: DeckSnapshot) -> tuple[ZoneCardRef, ...]:
    return _hidden_cards(
        ZoneCardRef(stack.card_id, stack.upgrade)
        for stack in snapshot.cards
        for _ in range(stack.count)
    )


def build_initial_audition_zone_checkpoint(
    *,
    snapshot: DeckSnapshot | RunDeckSnapshot,
    visible_hand: Iterable[ZoneCardRef],
    binding: AuditionZoneBinding,
    session_transition_id: str,
    capture_hashes: AuditionZoneCaptureHashes,
    confidence_threshold: float = 0.80,
) -> AuditionZoneCheckpoint:
    """Condition one verified fourteen-card deck on its opening Hand."""

    run_snapshot = snapshot if isinstance(snapshot, RunDeckSnapshot) else None
    deck = run_snapshot.snapshot if run_snapshot is not None else snapshot
    if not isinstance(deck, DeckSnapshot):
        raise TypeError("snapshot must be DeckSnapshot or RunDeckSnapshot")
    deck.validate()
    if run_snapshot is not None and run_snapshot.run_id != binding.run_id:
        raise ValueError("run deck snapshot belongs to a different run")
    if not deck.is_authoritative(confidence_threshold=confidence_threshold):
        raise ValueError("deck snapshot is not authoritative")
    if deck.total_cards != EXPECTED_INITIAL_DECK_SIZE:
        raise ValueError(
            f"initial deck must contain exactly {EXPECTED_INITIAL_DECK_SIZE} cards"
        )
    if (
        binding.produce_id != deck.produce_id
        or binding.step_type != deck.step_type
        or binding.stage_number != deck.stage_number
    ):
        raise ValueError("deck snapshot stage identity does not match zone binding")
    actual_digest = deck_snapshot_digest(snapshot)
    if binding.deck_snapshot_digest != actual_digest:
        raise ValueError("deck snapshot digest does not match zone binding")
    hand = _visible_cards(visible_hand)
    baseline = _snapshot_cards(deck)
    remaining = list(baseline)
    for card in hand:
        try:
            remaining.remove(card)
        except ValueError as error:
            raise ValueError(
                f"visible Hand card is absent from verified deck: {card.key}"
            ) from error
    return AuditionZoneCheckpoint(
        schema_version=AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION,
        binding=binding,
        transition_id=_strict_text(session_transition_id, "session_transition_id"),
        previous_transition_id=None,
        session_transition_id=session_transition_id,
        replay_record_id=None,
        action_digest=None,
        lineage_depth=0,
        capture_hashes=capture_hashes,
        baseline_cards=baseline,
        created_cards=(),
        zones=AuditionCardZoneState(hand=hand, deck=tuple(remaining)),
        hand_base_cards=hand,
    )


def _remove_exact_cards(
    pool: Iterable[ZoneCardRef],
    removed: Iterable[ZoneCardRef],
    *,
    label: str,
) -> tuple[ZoneCardRef, ...]:
    pool_counter = Counter(pool)
    removed_counter = Counter(removed)
    unavailable = removed_counter - pool_counter
    if unavailable:
        raise ValueError(
            f"{label} contains cards absent from source zone: "
            f"{_counter_text(unavailable)}"
        )
    return _hidden_cards((pool_counter - removed_counter).elements())


def _consume_exact_draw(
    zones: AuditionCardZoneState,
    drawn: Iterable[ZoneCardRef],
    *,
    label: str,
) -> AuditionCardZoneState:
    """Consume an exact realized draw using Deck-remainder then Grave rules."""

    selected = _hidden_cards(drawn)
    count = len(selected)
    if count == 0:
        return zones
    available = len(zones.deck) + len(zones.grave)
    if count > available:
        raise ValueError(f"{label} exceeds Deck plus Grave availability")
    if count <= len(zones.deck):
        return replace(
            zones,
            deck=_remove_exact_cards(zones.deck, selected, label=label),
        )

    # Native draws every remaining Deck card before shuffling Grave.  The
    # committed exact draw must therefore contain the complete old Deck.
    selected_counter = Counter(selected)
    deck_counter = Counter(zones.deck)
    missing_deck = deck_counter - selected_counter
    if missing_deck:
        raise ValueError(
            f"{label} does not contain complete Deck remainder before refill: "
            f"{_counter_text(missing_deck)}"
        )
    from_grave = tuple((selected_counter - deck_counter).elements())
    remaining_grave = _remove_exact_cards(zones.grave, from_grave, label=label)
    return replace(zones, deck=remaining_grave, grave=())


def _draw_multiset_candidates(
    deck: Iterable[ZoneCardRef],
    grave: Iterable[ZoneCardRef],
    count: int,
) -> tuple[tuple[ZoneCardRef, ...], ...]:
    """Enumerate exact draw multisets while preserving native refill rules."""

    selected_deck = _hidden_cards(deck)
    selected_grave = _hidden_cards(grave)
    if count < 0 or count > len(selected_deck) + len(selected_grave):
        return ()
    if count == 0:
        return ((),)
    if count <= len(selected_deck):
        raw = combinations(selected_deck, count)
    else:
        from_grave = count - len(selected_deck)
        raw = (
            (*selected_deck, *chosen)
            for chosen in combinations(selected_grave, from_grave)
        )
    return tuple(sorted({_hidden_cards(candidate) for candidate in raw}))


def _support_transform_candidates(
    candidate_pairs: Iterable[tuple[ZoneCardRef, ZoneCardRef, str | None]],
    observed_counter: Counter[ZoneCardRef],
) -> tuple[tuple[tuple[ZoneCardRef, ZoneCardRef], ...], ...]:
    pairs = tuple(candidate_pairs)
    effective_counter = Counter(pair[1] for pair in pairs)
    missing = tuple(sorted((effective_counter - observed_counter).elements()))
    extra = tuple(sorted((observed_counter - effective_counter).elements()))
    if not missing or len(missing) != len(extra):
        return ()
    eligible_counter = Counter(
        pair[0]
        for pair in pairs
        if pair[0] == pair[1] and pair[2] is None
    )
    results: set[tuple[tuple[ZoneCardRef, ZoneCardRef], ...]] = set()
    for target_order in set(permutations(extra)):
        transforms = tuple(sorted(zip(missing, target_order, strict=True)))
        if any(
            source.card_id != target.card_id
            or target.upgrade <= source.upgrade
            or target.upgrade - source.upgrade > 3
            for source, target in transforms
        ):
            continue
        if len(set(transforms)) != len(transforms):
            continue
        required = Counter(source for source, _ in transforms)
        if required - eligible_counter:
            continue
        if any(
            eligible_counter[source] != count
            for source, count in required.items()
        ):
            continue
        # Identical source refs cannot be bound to distinct effective results
        # without an independently visible source position.
        source_targets: dict[ZoneCardRef, set[ZoneCardRef]] = {}
        for source, target in transforms:
            source_targets.setdefault(source, set()).add(target)
        if any(len(targets) > 1 for targets in source_targets.values()):
            continue
        results.add(transforms)
    return tuple(sorted(results))


def derive_audition_zone_observation(
    previous: AuditionZoneCheckpoint,
    action: AuditionZoneAction,
    *,
    visible_hand: Iterable[ZoneCardRef],
    turn_ended: bool,
    settled_sha256: tuple[str, str],
    played_destination: str | None = None,
    support_delta: int | None = None,
    support_evidence_ref: str | None = None,
) -> AuditionZoneDerivedObservation:
    """Strictly derive realized base draws and temporary Hand upgrades.

    Exact base/effective reconciliation is attempted first.  A temporary
    same-ID ``@n -> @(n+1..3)`` substitutions are considered only when no
    exact candidate is possible and there is exactly one source/draw solution.
    The function
    deliberately supports a single draw phase: direct draws for a continuing
    turn, or next-turn draws after a turn end.  A card that both directly
    draws and ends the turn needs rule-derived phase data and must bypass this
    helper rather than letting the final Hand guess both hidden phases.
    """

    if not isinstance(previous, AuditionZoneCheckpoint):
        raise TypeError("previous must be AuditionZoneCheckpoint")
    if not isinstance(action, AuditionZoneAction):
        raise TypeError("action must be AuditionZoneAction")
    if action.previous_transition_id != previous.transition_id:
        raise ValueError("action is bound to a stale zone transition")
    if action.binding_digest != previous.binding.digest():
        raise ValueError("action run/context binding does not match zone checkpoint")
    if not isinstance(turn_ended, bool):
        raise ValueError("turn_ended must be a boolean")
    if not isinstance(settled_sha256, tuple) or len(settled_sha256) != 2:
        raise ValueError("zone observation derivation requires two stable frames")
    for index, digest in enumerate(settled_sha256):
        _strict_sha256(digest, f"derived settled_sha256[{index}]")
    observed = _visible_cards(visible_hand)

    active_by_index = {
        value.hand_index: value.lineage_digest
        for value in previous.active_support_upgrades
    }
    pairs: list[tuple[ZoneCardRef, ZoneCardRef, str | None]] = [
        (base_card, effective_card, active_by_index.get(index))
        for index, (base_card, effective_card) in enumerate(
            zip(previous.hand_base_cards, previous.zones.hand, strict=True)
        )
    ]
    selected_base: ZoneCardRef | None = None
    if action.kind == ACTION_CARD_PLAY:
        assert action.hand_index is not None and action.selected_card is not None
        if action.hand_index >= len(pairs):
            raise ValueError("hand_index is outside the verified visible Hand")
        selected_base, selected_effective, _ = pairs[action.hand_index]
        if selected_effective != action.selected_card:
            raise ValueError("selected hand_index/card_id/upgrade mismatch")
        pairs.pop(action.hand_index)
    elif played_destination is not None:
        raise ValueError("end_turn derivation cannot contain played_destination")

    if turn_ended:
        if action.kind == ACTION_CARD_PLAY:
            if played_destination not in SUPPORTED_DESTINATIONS:
                raise ValueError(
                    "turn-ending card derivation requires played_destination"
                )
        elif played_destination is not None:
            raise ValueError("end_turn derivation cannot contain played_destination")
        grave = [*previous.zones.grave, *(pair[0] for pair in pairs)]
        if selected_base is not None and played_destination == DESTINATION_GRAVE:
            grave.append(selected_base)
        draw_count = len(observed)
        draw_candidates = _draw_multiset_candidates(
            previous.zones.deck, grave, draw_count
        )
        candidate_prefixes = [
            [(drawn, drawn, None) for drawn in drawn_cards]
            for drawn_cards in draw_candidates
        ]
    else:
        if action.kind == ACTION_END_TURN:
            raise ValueError("end_turn action requires turn_ended=true")
        draw_count = len(observed) - len(pairs)
        if draw_count < 0:
            raise ValueError("visible Hand cannot discard unplayed cards mid-turn")
        draw_candidates = _draw_multiset_candidates(
            previous.zones.deck, previous.zones.grave, draw_count
        )
        candidate_prefixes = [
            [*pairs, *((drawn, drawn, None) for drawn in drawn_cards)]
            for drawn_cards in draw_candidates
        ]

    exact: list[tuple[ZoneCardRef, ...]] = []
    upgraded: list[
        tuple[
            tuple[ZoneCardRef, ...],
            tuple[tuple[ZoneCardRef, ZoneCardRef], ...],
        ]
    ] = []
    observed_counter = Counter(observed)
    for drawn_cards, candidate_pairs in zip(
        draw_candidates, candidate_prefixes, strict=True
    ):
        effective_counter = Counter(pair[1] for pair in candidate_pairs)
        if effective_counter == observed_counter:
            exact.append(drawn_cards)
            continue
        for transforms in _support_transform_candidates(
            candidate_pairs, observed_counter
        ):
            upgraded.append((drawn_cards, transforms))

    exact = list(dict.fromkeys(exact))
    if exact:
        if len(exact) != 1:
            raise ValueError("visible Hand has ambiguous exact draw sources")
        if any(
            value is not None
            for value in (support_delta, support_evidence_ref)
        ):
            raise ValueError("support evidence was supplied but no upgrade was observed")
        proofs: tuple[AuditionZoneSupportUpgradeProof, ...] = ()
        drawn = exact[0]
    else:
        upgraded = list(dict.fromkeys(upgraded))
        if len(upgraded) != 1:
            reason = "missing" if not upgraded else "ambiguous"
            raise ValueError(
                f"visible Hand support/draw derivation is {reason}; refusing to guess"
            )
        if support_evidence_ref is None:
            raise ValueError(
                "temporary support upgrade requires marker and two-frame evidence"
            )
        drawn, transforms = upgraded[0]
        if support_delta is not None and any(
            target.upgrade - source.upgrade != support_delta
            for source, target in transforms
        ):
            raise ValueError("observed support delta does not match supplied marker")
        proofs = tuple(
            AuditionZoneSupportUpgradeProof(
                source_card=source_card,
                effective_card=effective_card,
                zone=UPGRADE_ZONE_HAND,
                support_delta=effective_card.upgrade - source_card.upgrade,
                evidence_ref=support_evidence_ref,
                settled_sha256=settled_sha256,
            )
            for source_card, effective_card in transforms
        )

    return AuditionZoneDerivedObservation(
        direct_drawn_cards=() if turn_ended else drawn,
        next_turn_drawn_cards=drawn if turn_ended else (),
        observed_support_upgrades=proofs,
    )


def evolve_audition_zone_checkpoint(
    previous: AuditionZoneCheckpoint,
    action: AuditionZoneAction,
    result: AuditionZoneTransitionResult,
) -> AuditionZoneCheckpoint:
    """Derive the next checkpoint without consulting the original deck again."""

    if not isinstance(previous, AuditionZoneCheckpoint):
        raise TypeError("previous must be AuditionZoneCheckpoint")
    if not isinstance(action, AuditionZoneAction):
        raise TypeError("action must be AuditionZoneAction")
    if not isinstance(result, AuditionZoneTransitionResult):
        raise TypeError("result must be AuditionZoneTransitionResult")
    if action.previous_transition_id != previous.transition_id:
        raise ValueError("action is bound to a stale zone transition")
    if action.binding_digest != previous.binding.digest():
        raise ValueError("action run/context binding does not match zone checkpoint")
    if result.previous_session_transition_id != previous.session_transition_id:
        raise ValueError("result is bound to a stale ExamSession transition")
    if result.action_digest != action.digest():
        raise ValueError("result action_digest does not match submitted action")
    if result.replay_record_id == previous.replay_record_id or (
        result.next_session_transition_id == previous.transition_id
    ):
        raise AuditionZoneDuplicateTransitionError(result.replay_record_id)

    zones = previous.zones
    lineage_by_digest = {
        entry.digest(): entry for entry in previous.support_lineage
    }
    active_by_index = {
        value.hand_index: value.lineage_digest
        for value in previous.active_support_upgrades
    }
    hand_pairs: list[tuple[ZoneCardRef, ZoneCardRef, str | None]] = [
        (base_card, effective_card, active_by_index.get(index))
        for index, (base_card, effective_card) in enumerate(
            zip(previous.hand_base_cards, zones.hand, strict=True)
        )
    ]
    retained_hand = list(hand_pairs)
    selected = action.selected_card
    selected_base: ZoneCardRef | None = None
    if action.kind == ACTION_CARD_PLAY:
        assert action.hand_index is not None and selected is not None
        if action.hand_index >= len(retained_hand):
            raise ValueError("hand_index is outside the verified visible Hand")
        selected_base, actual, _ = retained_hand[action.hand_index]
        if actual != selected:
            raise ValueError(
                "selected hand_index/card_id/upgrade mismatch: "
                f"expected={actual.key} submitted={selected.key}"
            )
        retained_hand.pop(action.hand_index)
        if result.played_destination not in SUPPORTED_DESTINATIONS:
            raise ValueError("card_play result requires a supported played_destination")
    else:
        if result.played_destination is not None:
            raise ValueError("end_turn result cannot contain played_destination")
        if result.direct_drawn_cards:
            raise ValueError("end_turn action cannot contain direct card draws")
        if not result.turn_ended:
            raise ValueError("end_turn action requires turn_ended=true")

    if result.created_to_deck:
        zones = replace(zones, deck=(*zones.deck, *result.created_to_deck))
    zones = replace(zones, hand=tuple(item[0] for item in retained_hand))
    zones = _consume_exact_draw(
        zones, result.direct_drawn_cards, label="direct_drawn_cards"
    )
    retained_hand.extend(
        (drawn, drawn, None) for drawn in result.direct_drawn_cards
    )
    zones = replace(zones, hand=tuple(item[0] for item in retained_hand))

    # MovePlayCard runs after direct Draw effects.
    if selected_base is not None:
        if result.played_destination == DESTINATION_GRAVE:
            zones = replace(zones, grave=(*zones.grave, selected_base))
        else:
            zones = replace(zones, lost=(*zones.lost, selected_base))

    if result.turn_ended:
        zones = replace(
            zones,
            hand=(),
            grave=(*zones.grave, *(item[0] for item in retained_hand)),
        )
        zones = _consume_exact_draw(
            zones, result.next_turn_drawn_cards, label="next_turn_drawn_cards"
        )
        retained_hand = [
            (drawn, drawn, None) for drawn in result.next_turn_drawn_cards
        ]

    support_lineage = list(previous.support_lineage)
    for proof in result.observed_support_upgrades:
        candidates = [
            index
            for index, (base_card, effective_card, active_digest) in enumerate(
                retained_hand
            )
            if base_card == proof.source_card
            and effective_card == proof.source_card
            and active_digest is None
        ]
        if len(candidates) != 1:
            raise ValueError(
                "support upgrade source is missing or ambiguous in Hand: "
                f"source={proof.source_card.key} candidates={len(candidates)}"
            )
        entry = AuditionZoneSupportLineageEntry(
            replay_record_id=result.replay_record_id,
            action_digest=result.action_digest,
            proof=proof,
        )
        entry_digest = entry.digest()
        if entry_digest in lineage_by_digest:
            raise ValueError("duplicate support upgrade lineage")
        index = candidates[0]
        retained_hand[index] = (
            proof.source_card,
            proof.effective_card,
            entry_digest,
        )
        support_lineage.append(entry)
        lineage_by_digest[entry_digest] = entry

    # Map the base/effective pairs onto the screenshot-owned left-to-right
    # Hand.  Multiple truly distinct base/effect candidates are ambiguous and
    # must never be guessed.
    remaining = list(retained_hand)
    ordered: list[tuple[ZoneCardRef, ZoneCardRef, str | None]] = []
    for observed in result.visible_hand:
        candidates = [
            (index, pair) for index, pair in enumerate(remaining)
            if pair[1] == observed
        ]
        if not candidates:
            raise ValueError(
                "verified visible Hand has no exact base/effective source: "
                f"observed={observed.key}"
            )
        signatures = {(pair[0], pair[2]) for _, pair in candidates}
        if len(signatures) != 1:
            raise ValueError(
                "verified visible Hand is ambiguous across base/support sources: "
                f"observed={observed.key}"
            )
        selected_index, pair = candidates[0]
        ordered.append(pair)
        remaining.pop(selected_index)
    if remaining:
        raise ValueError(
            "verified visible Hand omitted evolved base/effective cards: "
            f"missing={_counter_text(Counter(pair[1] for pair in remaining))}"
        )

    # The screenshot's left-to-right order owns future hand_index semantics.
    zones = replace(zones, hand=result.visible_hand)
    hand_base_cards = tuple(pair[0] for pair in ordered)
    active_support_upgrades = tuple(
        AuditionZoneActiveSupportUpgrade(index, pair[2])
        for index, pair in enumerate(ordered)
        if pair[2] is not None
    )
    return AuditionZoneCheckpoint(
        schema_version=AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION,
        binding=previous.binding,
        transition_id=result.replay_record_id,
        previous_transition_id=previous.transition_id,
        session_transition_id=result.next_session_transition_id,
        replay_record_id=result.replay_record_id,
        action_digest=result.action_digest,
        lineage_depth=previous.lineage_depth + 1,
        capture_hashes=result.capture_hashes,
        baseline_cards=previous.baseline_cards,
        created_cards=(*previous.created_cards, *result.created_to_deck),
        zones=zones,
        hand_base_cards=hand_base_cards,
        active_support_upgrades=active_support_upgrades,
        support_lineage=tuple(support_lineage),
    )


class AuditionZoneCheckpointConflictError(RuntimeError):
    def __init__(self, expected: str | None, actual: str | None) -> None:
        self.expected_previous_transition_id = expected
        self.actual_previous_transition_id = actual
        super().__init__(
            "audition zone checkpoint compare-and-swap failed: "
            f"expected={expected!r}, actual={actual!r}"
        )


class AuditionZoneDuplicateTransitionError(RuntimeError):
    def __init__(self, replay_record_id: str) -> None:
        self.replay_record_id = replay_record_id
        super().__init__(f"audition zone replay was already committed: {replay_record_id}")


@dataclass(frozen=True, slots=True)
class AuditionZoneAtomicCommit:
    """One CAS value ready to join the same replay/ExamSession transaction."""

    expected_previous_transition_id: str
    previous_checkpoint_digest: str
    replay_record_id: str
    session_transition_id: str
    checkpoint: AuditionZoneCheckpoint

    def __post_init__(self) -> None:
        _strict_text(
            self.expected_previous_transition_id,
            "expected_previous_transition_id",
        )
        _strict_sha256(self.previous_checkpoint_digest, "previous_checkpoint_digest")
        _strict_text(self.replay_record_id, "replay_record_id")
        _strict_text(self.session_transition_id, "session_transition_id")
        if not isinstance(self.checkpoint, AuditionZoneCheckpoint):
            raise TypeError("checkpoint must be AuditionZoneCheckpoint")
        if self.checkpoint.previous_transition_id != self.expected_previous_transition_id:
            raise ValueError("atomic commit previous transition binding mismatch")
        if self.checkpoint.replay_record_id != self.replay_record_id:
            raise ValueError("atomic commit replay binding mismatch")
        if self.checkpoint.transition_id != self.replay_record_id:
            raise ValueError("atomic commit transition must equal replay_record_id")
        if self.checkpoint.session_transition_id != self.session_transition_id:
            raise ValueError("atomic commit ExamSession transition binding mismatch")
        if self.session_transition_id != self.replay_record_id:
            raise ValueError("replay, zone, and ExamSession transitions must be identical")

    def to_dict(self) -> dict[str, object]:
        return {
            "expected_previous_transition_id": self.expected_previous_transition_id,
            "previous_checkpoint_digest": self.previous_checkpoint_digest,
            "replay_record_id": self.replay_record_id,
            "session_transition_id": self.session_transition_id,
            "checkpoint": self.checkpoint.to_dict(),
        }

    def validate_exam_session_pair(
        self,
        previous_session: ExamSession,
        next_session: ExamSession,
    ) -> None:
        """Prove this zone write belongs to the same ExamSession CAS/replay.

        This method does not write either file.  The live integration must
        call it before handing both values to its transaction/journal layer,
        so a torn or cross-run pair is rejected before mutation begins.
        """

        if not isinstance(previous_session, ExamSession) or not isinstance(
            next_session, ExamSession
        ):
            raise TypeError("previous_session and next_session must be ExamSession")
        if (
            previous_session.schema_version != SESSION_SCHEMA_VERSION
            or next_session.schema_version != SESSION_SCHEMA_VERSION
        ):
            raise ValueError("zone commits require schema-v3 ExamSession values")
        if previous_session.transition_id != self.expected_previous_transition_id:
            raise ValueError("previous ExamSession CAS transition does not match zone commit")
        if next_session.transition_id != self.session_transition_id:
            raise ValueError("next ExamSession transition does not match replay/zone commit")
        if previous_session.preflight_binding != next_session.preflight_binding:
            raise ValueError("ExamSession preflight binding changed during zone commit")
        binding = self.checkpoint.binding
        for session in (previous_session, next_session):
            if (
                session.run_id != binding.run_id
                or session.produce_id != binding.produce_id
                or session.step_type != binding.step_type
                or session.stage_number != binding.stage_number
            ):
                raise ValueError("ExamSession run/stage identity does not match zone binding")
            preflight = session.preflight_binding
            assert preflight is not None
            if (
                preflight.step_context_id != binding.step_context_id
                or preflight.step_context_digest != binding.step_context_digest
                or preflight.deck_snapshot_digest != binding.deck_snapshot_digest
            ):
                raise ValueError("ExamSession context/deck evidence does not match zone binding")


def prepare_audition_zone_atomic_commit(
    previous: AuditionZoneCheckpoint,
    action: AuditionZoneAction,
    result: AuditionZoneTransitionResult,
) -> AuditionZoneAtomicCommit:
    checkpoint = evolve_audition_zone_checkpoint(previous, action, result)
    return AuditionZoneAtomicCommit(
        expected_previous_transition_id=previous.transition_id,
        previous_checkpoint_digest=previous.digest(),
        replay_record_id=result.replay_record_id,
        session_transition_id=result.next_session_transition_id,
        checkpoint=checkpoint,
    )


def load_audition_zone_checkpoint(
    path: Path = DEFAULT_AUDITION_ZONE_CHECKPOINT_PATH,
) -> AuditionZoneCheckpoint | None:
    selected = Path(path)
    if not selected.is_file():
        return None
    payload = json.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("audition zone checkpoint root must be an object")
    return AuditionZoneCheckpoint.from_dict(payload)


def _atomic_write(path: Path, checkpoint: AuditionZoneCheckpoint) -> None:
    selected = Path(path)
    selected.parent.mkdir(parents=True, exist_ok=True)
    temporary = selected.with_name(f".{selected.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(checkpoint.canonical_json() + "\n", encoding="utf-8")
        os.replace(temporary, selected)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_initial_audition_zone_checkpoint(
    checkpoint: AuditionZoneCheckpoint,
    *,
    path: Path = DEFAULT_AUDITION_ZONE_CHECKPOINT_PATH,
) -> AuditionZoneCheckpoint:
    if checkpoint.lineage_depth != 0:
        raise ValueError("save_initial requires an initial zone checkpoint")
    actual = load_audition_zone_checkpoint(path)
    if actual is not None:
        raise AuditionZoneCheckpointConflictError(None, actual.transition_id)
    _atomic_write(Path(path), checkpoint)
    return checkpoint


def commit_audition_zone_checkpoint(
    commit: AuditionZoneAtomicCommit,
    *,
    path: Path = DEFAULT_AUDITION_ZONE_CHECKPOINT_PATH,
) -> AuditionZoneCheckpoint:
    """CAS-persist one already prepared replay/session-bound checkpoint."""

    if not isinstance(commit, AuditionZoneAtomicCommit):
        raise TypeError("commit must be AuditionZoneAtomicCommit")
    actual = load_audition_zone_checkpoint(path)
    if actual is None:
        raise AuditionZoneCheckpointConflictError(
            commit.expected_previous_transition_id, None
        )
    if (
        actual.transition_id == commit.replay_record_id
        or actual.replay_record_id == commit.replay_record_id
    ):
        raise AuditionZoneDuplicateTransitionError(commit.replay_record_id)
    if actual.transition_id != commit.expected_previous_transition_id:
        raise AuditionZoneCheckpointConflictError(
            commit.expected_previous_transition_id, actual.transition_id
        )
    if actual.digest() != commit.previous_checkpoint_digest:
        raise ValueError("previous zone checkpoint content digest changed under same CAS ID")
    if commit.checkpoint.binding != actual.binding:
        raise ValueError("zone checkpoint binding changed during atomic commit")
    if commit.checkpoint.baseline_cards != actual.baseline_cards:
        raise ValueError("zone checkpoint baseline deck changed during atomic commit")
    if (
        commit.checkpoint.support_lineage[: len(actual.support_lineage)]
        != actual.support_lineage
    ):
        raise ValueError("zone checkpoint support lineage is not append-only")
    if commit.checkpoint.lineage_depth != actual.lineage_depth + 1:
        raise ValueError("zone checkpoint lineage depth is not consecutive")
    _atomic_write(Path(path), commit.checkpoint)
    return commit.checkpoint


__all__ = [
    "ACTION_CARD_PLAY",
    "ACTION_END_TURN",
    "AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_AUDITION_ZONE_CHECKPOINT_PATH",
    "DESTINATION_GRAVE",
    "DESTINATION_LOST",
    "LEGACY_AUDITION_ZONE_CHECKPOINT_SCHEMA_VERSION",
    "REPLAY_STATUS_VERIFIED_PENDING_CHECKPOINT",
    "UPGRADE_ZONE_HAND",
    "AuditionCardZoneState",
    "AuditionZoneActiveSupportUpgrade",
    "AuditionZoneAction",
    "AuditionZoneAtomicCommit",
    "AuditionZoneBinding",
    "AuditionZoneCaptureHashes",
    "AuditionZoneCheckpoint",
    "AuditionZoneCheckpointConflictError",
    "AuditionZoneDerivedObservation",
    "AuditionZoneDuplicateTransitionError",
    "AuditionZoneSupportLineageEntry",
    "AuditionZoneSupportUpgradeProof",
    "AuditionZoneTransitionResult",
    "ZoneCardRef",
    "build_initial_audition_zone_checkpoint",
    "commit_audition_zone_checkpoint",
    "deck_snapshot_digest",
    "derive_audition_zone_observation",
    "evolve_audition_zone_checkpoint",
    "load_audition_zone_checkpoint",
    "prepare_audition_zone_atomic_commit",
    "save_initial_audition_zone_checkpoint",
]
