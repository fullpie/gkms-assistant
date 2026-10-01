"""Persistent, evidence-backed shadow state for one visible Produce run."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .activity_reward import ActivityRewardState
from .school_event import SchoolEventChoice, SchoolEventResolution


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_SHADOW_PATH = PROJECT_ROOT / "var" / "run_shadow.json"
RUN_SHADOW_SCHEMA_VERSION = 3
NUMERIC_FIELDS = frozenset(
    {
        "route_week",
        "weeks_remaining",
        "stamina",
        "max_stamina",
        "produce_points",
        "vocal",
        "dance",
        "visual",
    }
)


@dataclass(frozen=True, slots=True)
class DrinkRewardDefinition:
    """Static drink identity retained at acquisition, never applied here."""

    drink_id: str
    display_name: str
    effect_ids: tuple[str, ...]

    def validate(self) -> None:
        if not self.drink_id or not self.display_name:
            raise ValueError("drink reward identity is incomplete")
        if not self.effect_ids or any(not effect_id for effect_id in self.effect_ids):
            raise ValueError("drink reward effects are incomplete")


def drink_reward_observation(
    reward: DrinkRewardDefinition,
    *,
    captured_at: float,
    selection_evidence_path: str,
    received_evidence_path: str,
    confidence: float,
    followup_evidence_path: str | None = None,
    observed_hud: Mapping[str, int] | None = None,
    settled_attributes: Mapping[str, int] | None = None,
    confirmed_passives: tuple[Mapping[str, Any], ...] = (),
    unattributed_attribute_deltas: Mapping[str, int] | None = None,
) -> ShadowObservation:
    """Record a drink and any separately observed outer-settlement attributes.

    Receiving a drink does not apply the drink's exam effect.  Attribute
    values in ``settled_attributes`` are different: they are authoritative
    post-reward values already applied by the game (for example, a support
    passive triggered by receiving that drink).  Keeping the two concepts
    separate lets the route planner rebase from observed truth without
    pretending it predicted the passive calculation.
    """

    reward.validate()
    if not selection_evidence_path or not received_evidence_path:
        raise ValueError("drink reward evidence paths are required")
    if followup_evidence_path is not None and not followup_evidence_path:
        raise ValueError("drink reward follow-up evidence path is invalid")
    hud = dict(observed_hud or {})
    if set(hud) - {"vocal", "dance", "visual"} or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in hud.values()
    ):
        raise ValueError("drink reward HUD values are invalid")
    settled = dict(settled_attributes or {})
    if set(settled) - {"vocal", "dance", "visual"} or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in settled.values()
    ):
        raise ValueError("drink reward settled attributes are invalid")
    unresolved = dict(unattributed_attribute_deltas or {})
    if set(unresolved) - {"vocal", "dance", "visual"} or any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in unresolved.values()
    ):
        raise ValueError("drink reward unattributed deltas are invalid")
    if any(not isinstance(item, Mapping) for item in confirmed_passives):
        raise ValueError("drink reward confirmed passives are invalid")
    metadata: dict[str, Any] = {
        "drink_id": reward.drink_id,
        "display_name": reward.display_name,
        "drink_effect_ids": list(reward.effect_ids),
        "selection_evidence_path": selection_evidence_path,
        "received_evidence_path": received_evidence_path,
        "effect_application": "not_used",
    }
    if followup_evidence_path is not None:
        metadata["followup_evidence_path"] = followup_evidence_path
    if hud:
        metadata["observed_hud"] = hud
    if settled:
        metadata["settlement_authority"] = "outer-local-save-observed-value"
    if confirmed_passives:
        metadata["confirmed_passives"] = [dict(item) for item in confirmed_passives]
    if unresolved:
        metadata["unattributed_attribute_deltas"] = unresolved
    return ShadowObservation(
        kind="drink_reward",
        captured_at=captured_at,
        evidence_path=received_evidence_path,
        confidence=confidence,
        values=settled,
        inventory_delta={reward.display_name: 1},
        metadata=metadata,
    )


def passive_step_outcome_observation(
    *,
    run_id: str,
    snapshot_digest: str,
    context_id: str,
    context_digest: str,
    stage: str,
    turn: int,
    captured_at: float,
    evidence_path: str,
    evidence_sha256: str,
    outcomes: Mapping[str, bool],
    observed_status: Mapping[str, int | None],
    contextual_session_path: str,
    stable_status_proof: bool,
    full_status_proof: bool,
) -> ShadowObservation:
    """Record observed passive outcomes without mutating run/exam values."""

    text_fields = {
        "run_id": run_id,
        "snapshot_digest": snapshot_digest,
        "context_id": context_id,
        "context_digest": context_digest,
        "stage": stage,
        "evidence_path": evidence_path,
        "contextual_session_path": contextual_session_path,
    }
    if any(not isinstance(value, str) or not value for value in text_fields.values()):
        raise ValueError("passive outcome identity fields must be non-empty text")
    for label, digest in (
        ("snapshot_digest", snapshot_digest),
        ("context_digest", context_digest),
        ("evidence_sha256", evidence_sha256),
    ):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
        raise ValueError("passive outcome turn must be an integer >= 1")
    if not isinstance(stable_status_proof, bool) or not isinstance(
        full_status_proof, bool
    ):
        raise ValueError("passive outcome proof flags must be booleans")
    if not stable_status_proof or not full_status_proof:
        raise ValueError("passive outcome requires stable and full status proof")
    if not outcomes or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, bool)
        for key, value in outcomes.items()
    ):
        raise ValueError("passive outcomes must map occurrence keys to booleans")
    allowed_status = {"good_impression", "motivation"}
    if set(observed_status) - allowed_status or any(
        value is not None
        and (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        )
        for value in observed_status.values()
    ):
        raise ValueError("observed passive status values are invalid")
    evidence = Path(evidence_path)
    session = Path(contextual_session_path)
    if not evidence.is_file():
        raise FileNotFoundError(f"passive outcome evidence is missing: {evidence}")
    if not session.is_file():
        raise FileNotFoundError(f"contextual passive session is missing: {session}")
    if hashlib.sha256(evidence.read_bytes()).hexdigest() != evidence_sha256:
        raise ValueError("passive outcome evidence hash mismatch")
    return ShadowObservation(
        kind="passive_step_outcome",
        captured_at=captured_at,
        evidence_path=evidence_path,
        confidence=1.0,
        values={},
        inventory_delta={},
        metadata={
            "run_id": run_id,
            "snapshot_digest": snapshot_digest,
            "context_id": context_id,
            "context_digest": context_digest,
            "stage": stage,
            "turn": turn,
            "evidence_sha256": evidence_sha256,
            "contextual_session_path": contextual_session_path,
            "stable_status_proof": stable_status_proof,
            "full_status_proof": full_status_proof,
            "outcomes": dict(outcomes),
            "observed_status": dict(observed_status),
        },
    )


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    kind: str
    captured_at: float
    evidence_path: str
    confidence: float
    values: Mapping[str, int]
    inventory_delta: Mapping[str, int]
    metadata: Mapping[str, Any]
    deck_delta: Mapping[str, int] = field(default_factory=dict)
    reward_excluded_card_ids: tuple[str, ...] = ()

    def validate(self) -> None:
        if not self.kind:
            raise ValueError("shadow observation kind is empty")
        if self.captured_at <= 0:
            raise ValueError("shadow observation timestamp must be positive")
        if not self.evidence_path:
            raise ValueError("shadow observation evidence path is empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("shadow observation confidence must be 0..1")
        unknown = set(self.values) - NUMERIC_FIELDS
        if unknown:
            raise ValueError(f"unknown shadow numeric fields: {sorted(unknown)}")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in self.values.values()):
            raise ValueError("shadow numeric values must be integers")
        if any(not name or not isinstance(delta, int) for name, delta in self.inventory_delta.items()):
            raise ValueError("shadow inventory deltas must map names to integers")
        if any(
            not _valid_deck_key(name) or not isinstance(delta, int)
            for name, delta in self.deck_delta.items()
        ):
            raise ValueError("shadow deck deltas must use CARD_ID@UPGRADE keys")
        if any(not isinstance(card_id, str) or not card_id for card_id in self.reward_excluded_card_ids):
            raise ValueError("shadow reward exclusions must contain card IDs")
        if len(set(self.reward_excluded_card_ids)) != len(self.reward_excluded_card_ids):
            raise ValueError("shadow reward exclusions must be unique")

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "kind": self.kind,
            "captured_at": self.captured_at,
            "evidence_path": self.evidence_path,
            "confidence": self.confidence,
            "values": dict(self.values),
            "inventory_delta": dict(self.inventory_delta),
            "metadata": dict(self.metadata),
            "deck_delta": dict(self.deck_delta),
            "reward_excluded_card_ids": list(self.reward_excluded_card_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ShadowObservation":
        values = payload.get("values", {})
        inventory_delta = payload.get("inventory_delta", {})
        metadata = payload.get("metadata", {})
        deck_delta = payload.get("deck_delta", {})
        reward_excluded_card_ids = payload.get("reward_excluded_card_ids", [])
        if not isinstance(values, Mapping) or not isinstance(inventory_delta, Mapping):
            raise ValueError("shadow observation mappings are invalid")
        if not isinstance(metadata, Mapping) or not isinstance(deck_delta, Mapping):
            raise ValueError("shadow observation metadata is invalid")
        if not isinstance(reward_excluded_card_ids, list):
            raise ValueError("shadow observation reward exclusions are invalid")
        result = cls(
            kind=str(payload.get("kind", "")),
            captured_at=float(payload.get("captured_at", 0.0)),
            evidence_path=str(payload.get("evidence_path", "")),
            confidence=float(payload.get("confidence", -1.0)),
            values={str(key): int(value) for key, value in values.items()},
            inventory_delta={
                str(key): int(value) for key, value in inventory_delta.items()
            },
            metadata=dict(metadata),
            deck_delta={str(key): int(value) for key, value in deck_delta.items()},
            reward_excluded_card_ids=tuple(str(value) for value in reward_excluded_card_ids),
        )
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class RunShadowState:
    produce_id: str
    character_id: str
    idol_card_id: str
    route_week: int | None = None
    weeks_remaining: int | None = None
    stamina: int | None = None
    max_stamina: int | None = None
    produce_points: int | None = None
    vocal: int | None = None
    dance: int | None = None
    visual: int | None = None
    inventory: Mapping[str, int] | None = None
    deck: Mapping[str, int] | None = None
    excluded_reward_card_ids: tuple[str, ...] = ()
    observations: tuple[ShadowObservation, ...] = ()

    def validate(self) -> None:
        if not self.produce_id or not self.character_id or not self.idol_card_id:
            raise ValueError("run shadow identity is incomplete")
        if self.route_week is not None and self.route_week < 1:
            raise ValueError("route_week must be at least 1")
        if self.weeks_remaining is not None and self.weeks_remaining < 0:
            raise ValueError("weeks_remaining cannot be negative")
        if self.max_stamina is not None and self.max_stamina < 1:
            raise ValueError("max_stamina must be positive")
        if (
            self.stamina is not None
            and self.max_stamina is not None
            and not 0 <= self.stamina <= self.max_stamina
        ):
            raise ValueError("stamina is outside 0..max_stamina")
        for name in ("produce_points", "vocal", "dance", "visual"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if any(not name or count < 0 for name, count in (self.inventory or {}).items()):
            raise ValueError("shadow inventory is invalid")
        if any(
            not _valid_deck_key(name) or count < 1
            for name, count in (self.deck or {}).items()
        ):
            raise ValueError("shadow deck is invalid")
        if any(not isinstance(card_id, str) or not card_id for card_id in self.excluded_reward_card_ids):
            raise ValueError("shadow excluded reward card IDs are invalid")
        if len(set(self.excluded_reward_card_ids)) != len(self.excluded_reward_card_ids):
            raise ValueError("shadow excluded reward card IDs must be unique")
        for observation in self.observations:
            observation.validate()

    def apply(self, observation: ShadowObservation) -> "RunShadowState":
        self.validate()
        observation.validate()
        if self.observations and observation.captured_at < self.observations[-1].captured_at:
            raise ValueError("cannot apply an older observation after a newer one")
        updates = {
            key: value for key, value in observation.values.items() if key in NUMERIC_FIELDS
        }
        inventory = dict(self.inventory or {})
        for name, delta in observation.inventory_delta.items():
            new_count = inventory.get(name, 0) + delta
            if new_count < 0:
                raise ValueError(f"inventory delta makes {name!r} negative")
            if new_count:
                inventory[name] = new_count
            else:
                inventory.pop(name, None)
        deck = None if self.deck is None else dict(self.deck)
        if deck is not None:
            for name, delta in observation.deck_delta.items():
                new_count = deck.get(name, 0) + delta
                if new_count < 0:
                    raise ValueError(f"deck delta makes {name!r} negative")
                if new_count:
                    deck[name] = new_count
                else:
                    deck.pop(name, None)
        excluded_reward_card_ids = list(self.excluded_reward_card_ids)
        for card_id in observation.reward_excluded_card_ids:
            if card_id not in excluded_reward_card_ids:
                excluded_reward_card_ids.append(card_id)
        result = replace(
            self,
            **updates,
            inventory=inventory,
            deck=deck,
            excluded_reward_card_ids=tuple(excluded_reward_card_ids),
            observations=(*self.observations, observation),
        )
        result.validate()
        return result

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": RUN_SHADOW_SCHEMA_VERSION,
            "produce_id": self.produce_id,
            "character_id": self.character_id,
            "idol_card_id": self.idol_card_id,
            "route_week": self.route_week,
            "weeks_remaining": self.weeks_remaining,
            "stamina": self.stamina,
            "max_stamina": self.max_stamina,
            "produce_points": self.produce_points,
            "vocal": self.vocal,
            "dance": self.dance,
            "visual": self.visual,
            "inventory": dict(self.inventory or {}),
            "deck": None if self.deck is None else dict(self.deck),
            "excluded_reward_card_ids": list(self.excluded_reward_card_ids),
            "observations": [item.to_dict() for item in self.observations],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunShadowState":
        if payload.get("schema_version") not in {1, 2, RUN_SHADOW_SCHEMA_VERSION}:
            raise ValueError("unsupported run shadow schema")
        inventory = payload.get("inventory", {})
        deck = payload.get("deck")
        observations = payload.get("observations", [])
        excluded_reward_card_ids = payload.get("excluded_reward_card_ids", [])
        if not isinstance(inventory, Mapping) or not isinstance(observations, list):
            raise ValueError("run shadow collections are invalid")
        if deck is not None and not isinstance(deck, Mapping):
            raise ValueError("run shadow deck is invalid")
        if not isinstance(excluded_reward_card_ids, list):
            raise ValueError("run shadow excluded reward card IDs are invalid")

        def optional_int(name: str) -> int | None:
            value = payload.get(name)
            return None if value is None else int(value)

        result = cls(
            produce_id=str(payload.get("produce_id", "")),
            character_id=str(payload.get("character_id", "")),
            idol_card_id=str(payload.get("idol_card_id", "")),
            route_week=optional_int("route_week"),
            weeks_remaining=optional_int("weeks_remaining"),
            stamina=optional_int("stamina"),
            max_stamina=optional_int("max_stamina"),
            produce_points=optional_int("produce_points"),
            vocal=optional_int("vocal"),
            dance=optional_int("dance"),
            visual=optional_int("visual"),
            inventory={str(key): int(value) for key, value in inventory.items()},
            deck=(
                None
                if deck is None
                else {str(key): int(value) for key, value in deck.items()}
            ),
            excluded_reward_card_ids=tuple(
                str(value) for value in excluded_reward_card_ids
            ),
            observations=tuple(
                ShadowObservation.from_dict(item)
                for item in observations
                if isinstance(item, Mapping)
            ),
        )
        if len(result.observations) != len(observations):
            raise ValueError("run shadow observation entry is invalid")
        result.validate()
        return result


def activity_reward_observation(
    reward: ActivityRewardState,
    *,
    captured_at: float,
    evidence_path: str,
) -> ShadowObservation:
    reward_data = reward.shadow_patch()
    return ShadowObservation(
        kind="activity_reward",
        captured_at=captured_at,
        evidence_path=evidence_path,
        confidence=reward.confidence,
        values={
            "stamina": reward.stamina,
            "max_stamina": reward.max_stamina,
            "produce_points": reward.produce_points,
        },
        inventory_delta={reward.item_name: 1},
        metadata={"item_effect": reward_data["item_effect"]},
    )


def outer_state_rebase_observation(
    *,
    captured_at: float,
    evidence_path: str,
    values: Mapping[str, int],
    authority: Mapping[str, Any],
    confidence: float = 1.0,
) -> ShadowObservation:
    """Rebase outer planning state from a post-settlement game snapshot.

    This is deliberately formula-free.  Callers may provide only fields that
    the current LocalSave/screen actually exposes; omitted attributes retain
    their previous observed value.
    """

    normalized = dict(values)
    allowed = {
        "stamina",
        "max_stamina",
        "produce_points",
        "vocal",
        "dance",
        "visual",
        "route_week",
        "weeks_remaining",
    }
    if not normalized or set(normalized) - allowed or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in normalized.values()
    ):
        raise ValueError("outer state rebase values are invalid")
    if not isinstance(authority, Mapping) or not authority:
        raise ValueError("outer state rebase authority is required")
    return ShadowObservation(
        kind="outer_state_rebase",
        captured_at=captured_at,
        evidence_path=evidence_path,
        confidence=confidence,
        values=normalized,
        inventory_delta={},
        metadata={
            "authority": dict(authority),
            "policy": "observe-after-settlement",
        },
    )


def deck_key(card_id: str, upgrade: int) -> str:
    if not card_id or upgrade < 0:
        raise ValueError("deck card identity is invalid")
    return f"{card_id}@{upgrade}"


def _valid_deck_key(value: str) -> bool:
    card_id, separator, upgrade = value.rpartition("@")
    return bool(separator and card_id and upgrade.isdigit())


def card_operation_observation(
    shadow: RunShadowState,
    *,
    operation: str,
    selected_card: Mapping[str, Any] | object,
    settled_successor: Mapping[str, Any],
) -> ShadowObservation:
    """Project one confirmed card mutation onto an authoritative shadow deck.

    Card identity comes from the already selected card-operation offer.  The
    successor is used only as proof that the server settled this card-operation
    transaction; it is never used to guess a card from notification artwork.
    """

    if not isinstance(shadow, RunShadowState):
        raise TypeError("shadow must be RunShadowState")
    if shadow.deck is None:
        raise ValueError("card operation requires an authoritative deck")
    if operation not in {"strengthen", "delete"}:
        raise ValueError("card operation must be strengthen or delete")

    def selected_value(name: str) -> Any:
        if isinstance(selected_card, Mapping):
            return selected_card.get(name)
        return getattr(selected_card, name, None)

    card_id = selected_value("card_id")
    upgrade = selected_value("upgrade")
    if not isinstance(card_id, str) or not card_id:
        raise ValueError("selected card identity is incomplete")
    if not isinstance(upgrade, int) or isinstance(upgrade, bool) or upgrade < 0:
        raise ValueError("selected card upgrade is invalid")

    source_key = deck_key(card_id, upgrade)
    source_count = shadow.deck.get(source_key)
    if source_count is None:
        raise ValueError("selected card is absent from the authoritative deck")
    if (
        not isinstance(source_count, int)
        or isinstance(source_count, bool)
        or source_count < 1
    ):
        raise ValueError("selected card stack would underflow")
    shadow.validate()

    if not isinstance(settled_successor, Mapping):
        raise TypeError("settled successor must be a mapping")
    successor_kind = settled_successor.get("kind")
    successor_target = settled_successor.get("target")
    evidence = settled_successor.get("evidence", {})
    capture = settled_successor.get("capture", {})
    if not isinstance(evidence, Mapping) or not isinstance(capture, Mapping):
        raise ValueError("settled successor evidence is invalid")
    if successor_kind not in {"completed", "continue"} or successor_target not in {
        "card-mutation-notification",
        "card-upgrade-notification",
    }:
        raise ValueError("card operation successor is not server-settled")

    successor_operation = settled_successor.get("operation", evidence.get("operation"))
    successor_card_id = settled_successor.get(
        "selected_card_id",
        settled_successor.get(
            "card_id", evidence.get("selected_card_id", evidence.get("card_id"))
        ),
    )
    successor_upgrade = settled_successor.get(
        "selected_card_upgrade",
        settled_successor.get(
            "upgrade",
            evidence.get("selected_card_upgrade", evidence.get("upgrade")),
        ),
    )
    if successor_operation is not None and successor_operation != operation:
        raise ValueError("card operation successor does not match the submitted operation")
    if successor_card_id is not None and successor_card_id != card_id:
        raise ValueError("card operation successor does not match the selected card")
    if successor_upgrade is not None and successor_upgrade != upgrade:
        raise ValueError("card operation successor does not match the selected upgrade")

    captured_at = capture.get("timestamp")
    evidence_path = capture.get("png_path")
    if (
        not isinstance(captured_at, (int, float))
        or isinstance(captured_at, bool)
        or captured_at <= 0
        or not isinstance(evidence_path, str)
        or not evidence_path
    ):
        raise ValueError("card operation successor capture is incomplete")
    confidence = evidence.get("confidence", 1.0)
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ValueError("card operation successor confidence is invalid")

    deck_delta = {source_key: -1}
    if operation == "strengthen":
        deck_delta[deck_key(card_id, upgrade + 1)] = 1
    return ShadowObservation(
        kind="card_operation",
        captured_at=float(captured_at),
        evidence_path=evidence_path,
        confidence=float(confidence),
        values={},
        inventory_delta={},
        metadata={
            "operation": operation,
            "card_id": card_id,
            "upgrade_before": upgrade,
            "upgrade_after": upgrade + 1 if operation == "strengthen" else None,
            "successor_kind": successor_kind,
            "successor_target": successor_target,
            "settlement_authority": "server-settled-typed-successor",
        },
        deck_delta=deck_delta,
    )


def card_reward_observation(
    *,
    card_id: str,
    upgrade: int,
    display_name: str,
    captured_at: float,
    evidence_path: str,
    confidence: float,
    settled_attributes: Mapping[str, int] | None = None,
    confirmed_passives: tuple[Mapping[str, Any], ...] = (),
) -> ShadowObservation:
    """Record one confirmed reward even before a full deck baseline exists."""

    settled = dict(settled_attributes or {})
    if set(settled) - {"vocal", "dance", "visual"} or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in settled.values()
    ):
        raise ValueError("card reward settled attributes are invalid")
    if any(not isinstance(item, Mapping) for item in confirmed_passives):
        raise ValueError("card reward confirmed passives are invalid")
    metadata: dict[str, Any] = {
        "card_id": card_id,
        "upgrade": upgrade,
        "name": display_name,
    }
    if settled:
        metadata["settlement_authority"] = "outer-local-save-observed-value"
    if confirmed_passives:
        metadata["confirmed_passives"] = [dict(item) for item in confirmed_passives]

    return ShadowObservation(
        kind="card_reward",
        captured_at=captured_at,
        evidence_path=evidence_path,
        confidence=confidence,
        values=settled,
        inventory_delta={},
        metadata=metadata,
        deck_delta={deck_key(card_id, upgrade): 1},
    )


def reward_card_exclude_observation(
    *,
    excluded_card_id: str,
    replacement_card_id: str,
    replacement_upgrade: int,
    remaining_exclude_count_after: int,
    captured_at: float,
    evidence_path: str,
    confidence: float,
) -> ShadowObservation:
    """Persist one verified whole-Produce reward-card exclusion."""

    if not excluded_card_id or not replacement_card_id:
        raise ValueError("reward exclusion card IDs are required")
    if excluded_card_id == replacement_card_id:
        raise ValueError("reward exclusion replacement must differ")
    if replacement_upgrade < 0 or remaining_exclude_count_after < 0:
        raise ValueError("reward exclusion counts cannot be negative")
    return ShadowObservation(
        kind="reward_card_exclude",
        captured_at=captured_at,
        evidence_path=evidence_path,
        confidence=confidence,
        values={},
        inventory_delta={},
        metadata={
            "excluded_card_id": excluded_card_id,
            "replacement_card_id": replacement_card_id,
            "replacement_upgrade": replacement_upgrade,
            "remaining_exclude_count_after": remaining_exclude_count_after,
            "excludes_from_future_reward_pool": True,
            "excludes_from_result_memory_pool": True,
            "mutates_active_deck": False,
        },
        deck_delta={},
        reward_excluded_card_ids=(excluded_card_id,),
    )


def apply_authoritative_deck_baseline(
    shadow: RunShadowState,
    snapshot: Any,
    *,
    captured_at: float,
    evidence_path: str,
    required_card_id: str | None = None,
) -> RunShadowState:
    """Replace an incomplete shadow deck with one verified visible snapshot."""

    if not snapshot.is_authoritative():
        raise ValueError("牌組快照尚未達到 authoritative 條件")
    if required_card_id is not None:
        if not required_card_id:
            raise ValueError("required unique card ID is empty")
        if not snapshot.contains_card_id(required_card_id):
            raise ValueError(
                "visible deck is missing the idol's required unique card: "
                f"{required_card_id}"
            )
    deck = {
        deck_key(card.card_id, card.upgrade): card.count
        for card in snapshot.cards
    }
    observation = ShadowObservation(
        kind="deck_baseline",
        captured_at=captured_at,
        evidence_path=evidence_path,
        confidence=snapshot.minimum_confidence,
        values={},
        inventory_delta={},
        metadata={
            "source": snapshot.source,
            "step_type": snapshot.step_type,
            "stage_number": snapshot.stage_number,
            "total_cards": snapshot.total_cards,
            "required_unique_card_id": required_card_id,
        },
    )
    if shadow.observations and captured_at < shadow.observations[-1].captured_at:
        raise ValueError("cannot apply an older deck baseline after a newer observation")
    result = replace(
        shadow,
        deck=deck,
        observations=(*shadow.observations, observation),
    )
    result.validate()
    return result


def apply_plan2_exam_save_deck_baseline(
    shadow: RunShadowState,
    evidence: Any,
    *,
    captured_at: float,
    required_card_id: str | None = None,
) -> RunShadowState:
    """Replace the shadow deck from one exact turn-one Plan2 ExamSave.

    N.I.A. does not always expose the full deck panel before card rewards.  A
    settled turn-one ExamSave already contains every persistent card GUID in
    Hand+Deck, including cards acquired earlier in the run.  Base upgrade is
    the persistent identity; temporary and support HandAdd upgrades remain
    stage-local and are deliberately excluded.

    This is a baseline refresh, not a delta.  It also repairs any reward
    checkpoint that was missed before the next audition without guessing
    from the starter manifest.
    """

    from .audition_local_save_state import AuditionLocalSaveStateEvidence

    if not isinstance(shadow, RunShadowState):
        raise TypeError("shadow must be RunShadowState")
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
        raise TypeError("captured_at must be numeric")
    shadow.validate()
    state = evidence.state
    runtime = state.root_runtime
    if (
        state.phase != 6
        or state.current_turn != 1
        or runtime is None
        or runtime.command_list.to_value() != []
        or runtime.draw_card_guid_list
        or runtime.is_turn_card_grave
        or runtime.is_turn_card_lost
        or runtime.is_exam_end_complete
        or state.playing_card is not None
        or state.removed_cards
        or state.future_deck
        or state.past_deck is None
        or state.past_deck
        or state.zones.grave
        or state.zones.lost
        or state.zones.hold
    ):
        raise ValueError("Plan2 ExamSave is not an exact turn-one deck baseline")
    cards = (*state.zones.hand, *state.zones.deck)
    if not cards:
        raise ValueError("Plan2 ExamSave deck baseline is empty")
    if required_card_id is not None and not any(
        card.card_id == required_card_id for card in cards
    ):
        raise ValueError("Plan2 ExamSave is missing the idol unique card")
    counts = Counter(deck_key(card.card_id, card.base_upgrade) for card in cards)
    deck = dict(sorted(counts.items()))
    observation = ShadowObservation(
        kind="plan2_exam_save_deck_baseline",
        captured_at=float(captured_at),
        evidence_path=evidence.source_path,
        confidence=1.0,
        values={},
        inventory_delta={},
        metadata={
            "source": "typed-plan2-exam-save-turn-one",
            "source_sha256": evidence.source_sha256,
            "step_context_id": evidence.step_context_id,
            "card_instance_count": len(cards),
            "persistent_upgrade_authority": "base_upgrade",
        },
    )
    if shadow.observations and captured_at < shadow.observations[-1].captured_at:
        raise ValueError("cannot apply an older ExamSave deck baseline")
    if (
        shadow.deck == deck
        and shadow.observations
        and shadow.observations[-1] == observation
    ):
        return shadow
    result = replace(
        shadow,
        deck=deck,
        observations=(*shadow.observations, observation),
    )
    result.validate()
    return result


def plan2_exam_save_active_deck_counts(evidence: Any) -> dict[str, int]:
    """Project the exact active card universe from a settled Plan2 ExamSave.

    The turn-one baseline above intentionally has a narrower contract: it
    counts only Hand+Deck so that it can prove the persistent deck before any
    card has been played.  A runner can miss that instant while the game is
    still on the same audition (for example after a background transition).
    In that case a settled ExamSave still exposes the exact active universe in
    Hand, Deck, Grave, Lost, Hold, and any serialized future/past groups.

    ``removedCardList`` is a tombstone/history list on PC when its GUID is
    already positioned in a live zone.  It is therefore never counted a
    second time.  A card present only in that list is excluded from the active
    deck.  No card identity, upgrade, or count is inferred from Master here.
    """

    from .audition_local_save_state import AuditionLocalSaveStateEvidence

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    state = evidence.state
    if (
        state.phase != 6
        or not state.is_native_actionable_settled
        or state.past_deck is None
    ):
        raise ValueError("Plan2 ExamSave is not a settled active deck snapshot")

    live_cards = [
        *state.zones.hand,
        *state.zones.deck,
        *state.zones.grave,
        *state.zones.lost,
        *state.zones.hold,
    ]
    if state.playing_card is not None:
        live_cards.append(state.playing_card)
    live_cards.extend(card for group in state.future_deck for card in group)
    live_cards.extend(card for group in state.past_deck for card in group)
    if not live_cards:
        raise ValueError("Plan2 ExamSave active deck snapshot is empty")

    by_guid: dict[str, Any] = {}
    for card in live_cards:
        if card.guid in by_guid:
            raise ValueError(
                "Plan2 ExamSave active deck snapshot contains duplicate card GUID"
            )
        by_guid[card.guid] = card

    counts: Counter[str] = Counter()
    for card in live_cards:
        counts[deck_key(card.card_id, card.base_upgrade)] += 1
    return dict(sorted(counts.items()))


def apply_plan2_exam_save_active_deck_snapshot(
    shadow: RunShadowState,
    evidence: Any,
    *,
    captured_at: float,
) -> RunShadowState:
    """Refresh ``shadow.deck`` from any exact settled Plan2 ExamSave state."""

    from .audition_local_save_state import AuditionLocalSaveStateEvidence

    if not isinstance(shadow, RunShadowState):
        raise TypeError("shadow must be RunShadowState")
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)):
        raise TypeError("captured_at must be numeric")
    shadow.validate()
    deck = plan2_exam_save_active_deck_counts(evidence)
    observation = ShadowObservation(
        kind="plan2_exam_save_active_deck_snapshot",
        captured_at=float(captured_at),
        evidence_path=evidence.source_path,
        confidence=1.0,
        values={},
        inventory_delta={},
        metadata={
            "source": "typed-plan2-exam-save-active-card-universe",
            "source_sha256": evidence.source_sha256,
            "step_context_id": evidence.step_context_id,
            "card_instance_count": sum(deck.values()),
            "persistent_upgrade_authority": "base_upgrade",
        },
    )
    if shadow.observations and captured_at < shadow.observations[-1].captured_at:
        raise ValueError("cannot apply an older active ExamSave deck snapshot")
    if (
        shadow.deck == deck
        and shadow.observations
        and shadow.observations[-1] == observation
    ):
        return shadow
    result = replace(
        shadow,
        deck=deck,
        observations=(*shadow.observations, observation),
    )
    result.validate()
    return result


def school_event_observation(
    shadow: RunShadowState,
    event: SchoolEventResolution,
    choice: SchoolEventChoice,
    *,
    captured_at: float,
    evidence_path: str,
    confidence: float = 0.99,
) -> ShadowObservation:
    """Turn one fixed Master-resolved school choice into a shadow delta."""

    if choice not in event.choices:
        raise ValueError("school-event choice does not belong to the resolution")
    if shadow.stamina is None:
        raise ValueError("school-event update requires known stamina")
    deltas = choice.fixed_status_delta
    values: dict[str, int] = {"stamina": shadow.stamina - choice.stamina_cost}
    if values["stamina"] < 0:
        raise ValueError("school-event choice would make stamina negative")
    if shadow.produce_points is not None:
        values["produce_points"] = shadow.produce_points - choice.produce_point_cost
    for attribute, delta in deltas.items():
        current = getattr(shadow, attribute)
        if current is None:
            raise ValueError(f"school-event update requires known {attribute}")
        values[attribute] = current + delta
    if shadow.route_week is not None:
        values["route_week"] = shadow.route_week + 1
    if shadow.weeks_remaining is not None:
        values["weeks_remaining"] = max(0, shadow.weeks_remaining - 1)

    direct_card_delta = (
        {deck_key(choice.direct_card_id, choice.direct_card_upgrade): 1}
        if choice.direct_card_id
        else {}
    )
    return ShadowObservation(
        kind="school_event",
        captured_at=captured_at,
        evidence_path=evidence_path,
        confidence=confidence,
        values=values,
        inventory_delta={},
        metadata={
            "adv_asset_id": event.adv_asset_id,
            "story_id": event.story_id,
            "detail_ids": list(event.matching_detail_ids),
            "choice_slot": choice.slot,
            "suggestion_id": choice.suggestion_id,
            "status_delta": deltas,
            "stamina_cost": choice.stamina_cost,
            "grants_card_selection": choice.grants_card_selection,
        },
        deck_delta=direct_card_delta,
    )


def load_run_shadow(path: Path = DEFAULT_RUN_SHADOW_PATH) -> RunShadowState | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("run shadow root must be an object")
    return RunShadowState.from_dict(payload)


def load_matching_active_run_shadow(
    produce_id: str,
    idol_card_id: str,
    *,
    expected_run_id: str | None = None,
) -> RunShadowState | None:
    """Return the active shadow only for the requested run identity."""

    try:
        from .run_identity import load_active_run, paths_for

        active = load_active_run()
        if (
            active is None
            or active.produce_id != produce_id
            or active.idol_card_id != idol_card_id
            or (
                expected_run_id is not None
                and active.run_id != expected_run_id
            )
        ):
            return None
        shadow = load_run_shadow(paths_for(active).shadow)
        if (
            shadow is None
            or shadow.produce_id != produce_id
            or shadow.character_id != active.character_id
            or shadow.idol_card_id != idol_card_id
        ):
            return None
        return shadow
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None


def save_run_shadow(
    state: RunShadowState,
    path: Path = DEFAULT_RUN_SHADOW_PATH,
) -> None:
    state.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def checkpoint_drink_reward(
    identity: Any,
    reward: DrinkRewardDefinition,
    *,
    captured_at: float,
    selection_evidence_path: str,
    received_evidence_path: str,
    confidence: float,
    followup_evidence_path: str | None = None,
    observed_hud: Mapping[str, int] | None = None,
    settled_attributes: Mapping[str, int] | None = None,
    confirmed_passives: tuple[Mapping[str, Any], ...] = (),
    unattributed_attribute_deltas: Mapping[str, int] | None = None,
    root: Path | None = None,
) -> RunShadowState:
    """Atomically append one evidence-backed drink acquisition to one run.

    The checkpoint refuses unscoped, missing, mismatched, or duplicate input.
    It never updates status values from a drink: receiving and using are separate
    gameplay events.
    """

    from .run_identity import DEFAULT_RUN_ROOT, load_run, paths_for

    run_root = DEFAULT_RUN_ROOT if root is None else Path(root)
    identity.validate()
    stored_identity = load_run(identity.run_id, root=run_root)
    if stored_identity != identity:
        raise ValueError("drink reward identity differs from stored run manifest")
    paths = paths_for(identity, root=run_root)
    evidence_paths = [selection_evidence_path, received_evidence_path]
    if followup_evidence_path is not None:
        evidence_paths.append(followup_evidence_path)
    for evidence_path in evidence_paths:
        if not Path(evidence_path).is_file():
            raise FileNotFoundError(f"drink reward evidence does not exist: {evidence_path}")
    shadow = load_run_shadow(paths.shadow)
    if shadow is None:
        raise FileNotFoundError(f"run shadow does not exist: {paths.shadow}")
    if (
        shadow.produce_id,
        shadow.character_id,
        shadow.idol_card_id,
    ) != (
        identity.produce_id,
        identity.character_id,
        identity.idol_card_id,
    ):
        raise ValueError("drink reward shadow identity differs from run manifest")
    if any(
        observation.kind == "drink_reward"
        and observation.metadata.get("drink_id") == reward.drink_id
        and observation.metadata.get("received_evidence_path") == received_evidence_path
        for observation in shadow.observations
    ):
        raise ValueError("drink reward receipt is already checkpointed")
    updated = shadow.apply(
        drink_reward_observation(
            reward,
            captured_at=captured_at,
            selection_evidence_path=selection_evidence_path,
            received_evidence_path=received_evidence_path,
            confidence=confidence,
            followup_evidence_path=followup_evidence_path,
            observed_hud=observed_hud,
            settled_attributes=settled_attributes,
            confirmed_passives=confirmed_passives,
            unattributed_attribute_deltas=unattributed_attribute_deltas,
        )
    )
    save_run_shadow(updated, paths.shadow)
    return updated
