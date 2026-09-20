"""Fail-closed publication and replay checks for a live Exam hand.

Each Exam context has a stage-number-scoped immutable marker under
``live_exam_preflight_publications/``.  The former run-wide
``live_exam_preflight_publication.json`` is legacy input only: it can be
migrated during an exact publication replay, but is never a ready proof by
itself.  ``loadout_snapshot.json`` and every supplemental file are prerequisites
that can exist after an interrupted publication and never imply readiness
alone.  This module performs no capture, game input, or process-memory access.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .audition_rules import FINAL, MID1, MID2
from .contextual_passive_step import (
    ContextualPassiveBlocker,
    ContextualPassiveStepContext,
    ContextualPassiveStepResult,
    ContextualPassiveStepSession,
    EvidenceBoundPassiveOutcome,
    load_contextual_passive_step_session,
    resolve_contextual_passive_step,
    save_contextual_passive_step_session,
)
from .equipped_item_snapshot import (
    EquippedItemSnapshot,
    EquippedItemSnapshotResolution,
    load_equipped_item_snapshot,
    save_equipped_item_snapshot,
)
from .initial_modifier_reconciliation import (
    InitialModifierReconciliation,
    InitialModifierReconciliationError,
    load_initial_modifier_reconciliation,
    replay_initial_modifier_reconciliation,
    save_initial_modifier_reconciliation,
)
from .item_rules import EquippedItemRule
from .logic_engine import (
    ActiveRuntimeStatusEnchant,
    LogicExamState,
    load_master_card,
    resolve_logic_status_turn_start,
)
from .loadout_runtime_bridge import loadout_snapshot_digest
from .loadout_snapshot import (
    LoadoutSnapshot,
    save_loadout_snapshot,
)
from .master_db import DEFAULT_DATABASE
from .passive_catalog import (
    MasterPassiveCatalog,
    MemoryAbilitySelection,
    PassiveSource,
    SupportCardSelection,
)
from .run_deck_snapshot import RunDeckSnapshot, load_run_deck_snapshot
from .run_identity import (
    DEFAULT_RUN_ROOT,
    RunIdentity,
    load_active_run,
    load_run,
    paths_for,
)


EXAM_STAGES = frozenset({MID1, MID2, FINAL})


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _blocker_text(blocker: ContextualPassiveBlocker) -> str:
    key = "" if blocker.source_occurrence_key is None else f":{blocker.source_occurrence_key}"
    return f"contextual:{blocker.code}{key}:{blocker.reason}"


def _strict_status(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be an integer >= 0")
    return value


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _strict_digest(value: object, label: str) -> str:
    value = _strict_text(value, label)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _exact_fields(
    payload: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    actual = set(payload)
    if actual != set(expected):
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(set(expected) - actual)} "
            f"unknown={sorted(actual - set(expected))}"
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LiveExamEntryStatusObservation:
    """Stable, full, evidence-bound status values at the Exam entry boundary."""

    run_id: str
    snapshot_digest: str
    context_id: str
    context_digest: str
    capture_path: str
    capture_sha256: str
    stable_status_proof: bool
    full_status_proof: bool
    observed_good_impression: int
    observed_motivation: int
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        _strict_text(self.run_id, "entry status run_id")
        _strict_digest(self.snapshot_digest, "entry status snapshot_digest")
        _strict_text(self.context_id, "entry status context_id")
        _strict_digest(self.context_digest, "entry status context_digest")
        _strict_text(self.capture_path, "entry status capture_path")
        _strict_digest(self.capture_sha256, "entry status capture_sha256")
        if not isinstance(self.stable_status_proof, bool):
            raise ValueError("stable_status_proof must be a boolean")
        if not isinstance(self.full_status_proof, bool):
            raise ValueError("full_status_proof must be a boolean")
        _strict_status(self.observed_good_impression, "observed_good_impression")
        _strict_status(self.observed_motivation, "observed_motivation")
        object.__setattr__(self, "digest", _canonical_digest(self._digest_payload()))

    def _digest_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "snapshot_digest": self.snapshot_digest,
            "context_id": self.context_id,
            "context_digest": self.context_digest,
            "capture_path": self.capture_path,
            "capture_sha256": self.capture_sha256,
            "stable_status_proof": self.stable_status_proof,
            "full_status_proof": self.full_status_proof,
            "observed_good_impression": self.observed_good_impression,
            "observed_motivation": self.observed_motivation,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._digest_payload(), "digest": self.digest}

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "LiveExamEntryStatusObservation":
        _exact_fields(
            payload,
            frozenset(
                {
                    "run_id",
                    "snapshot_digest",
                    "context_id",
                    "context_digest",
                    "capture_path",
                    "capture_sha256",
                    "stable_status_proof",
                    "full_status_proof",
                    "observed_good_impression",
                    "observed_motivation",
                    "digest",
                }
            ),
            "entry status observation",
        )
        if not isinstance(payload["stable_status_proof"], bool) or not isinstance(
            payload["full_status_proof"], bool
        ):
            raise ValueError("entry status proof fields must be booleans")
        result = cls(
            run_id=_strict_text(payload["run_id"], "entry status run_id"),
            snapshot_digest=_strict_digest(
                payload["snapshot_digest"], "entry status snapshot_digest"
            ),
            context_id=_strict_text(
                payload["context_id"], "entry status context_id"
            ),
            context_digest=_strict_digest(
                payload["context_digest"], "entry status context_digest"
            ),
            capture_path=_strict_text(
                payload["capture_path"], "entry status capture_path"
            ),
            capture_sha256=_strict_digest(
                payload["capture_sha256"], "entry status capture_sha256"
            ),
            stable_status_proof=payload["stable_status_proof"],
            full_status_proof=payload["full_status_proof"],
            observed_good_impression=_strict_status(
                payload["observed_good_impression"], "observed_good_impression"
            ),
            observed_motivation=_strict_status(
                payload["observed_motivation"], "observed_motivation"
            ),
        )
        if payload["digest"] != result.digest:
            raise ValueError("entry status observation digest mismatch")
        return result

    def validation_reasons(
        self,
        identity: RunIdentity,
        context: ContextualPassiveStepContext,
        snapshot_digest: str,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.run_id != identity.run_id:
            reasons.append("entry-status-run-id-mismatch")
        if self.snapshot_digest != snapshot_digest:
            reasons.append("entry-status-loadout-digest-mismatch")
        if self.context_id != context.context_id:
            reasons.append("entry-status-context-id-mismatch")
        if self.context_digest != context.digest:
            reasons.append("entry-status-context-digest-mismatch")
        if not self.stable_status_proof:
            reasons.append("entry-status-proof-not-stable")
        if not self.full_status_proof:
            reasons.append("entry-status-proof-not-full")
        path = Path(self.capture_path)
        if not path.is_file():
            reasons.append("entry-status-evidence-file-missing")
        else:
            try:
                actual = _file_sha256(path)
            except OSError:
                reasons.append("entry-status-evidence-file-unreadable")
            else:
                if actual != self.capture_sha256:
                    reasons.append("entry-status-evidence-hash-mismatch")
        return _deduplicate(reasons)


@dataclass(frozen=True, slots=True)
class LiveExamPreflightPublicationMarker:
    """Strict create-once binding for every artifact required by preflight."""

    run_id: str
    produce_id: str
    idol_card_id: str
    loadout_snapshot_digest: str
    initial_reconciliation_digest: str
    contextual_session_digest: str
    equipped_item_snapshot_digest: str
    deck_snapshot_digest: str
    context_id: str
    context_digest: str
    stage: str
    stage_number: int
    entry_status_observation: LiveExamEntryStatusObservation
    schema_version: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != 1
        ):
            raise ValueError("unsupported live Exam preflight marker schema")
        _strict_text(self.run_id, "publication run_id")
        _strict_text(self.produce_id, "publication produce_id")
        _strict_text(self.idol_card_id, "publication idol_card_id")
        for label, value in (
            ("loadout_snapshot_digest", self.loadout_snapshot_digest),
            ("initial_reconciliation_digest", self.initial_reconciliation_digest),
            ("contextual_session_digest", self.contextual_session_digest),
            ("equipped_item_snapshot_digest", self.equipped_item_snapshot_digest),
            ("deck_snapshot_digest", self.deck_snapshot_digest),
            ("context_digest", self.context_digest),
        ):
            _strict_digest(value, f"publication {label}")
        _strict_text(self.context_id, "publication context_id")
        if self.stage not in EXAM_STAGES:
            raise ValueError("publication stage must be an Exam stage")
        if (
            not isinstance(self.stage_number, int)
            or isinstance(self.stage_number, bool)
            or self.stage_number < 1
        ):
            raise ValueError("publication stage_number must be an integer >= 1")
        if not isinstance(
            self.entry_status_observation, LiveExamEntryStatusObservation
        ):
            raise TypeError(
                "entry_status_observation must be LiveExamEntryStatusObservation"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "produce_id": self.produce_id,
            "idol_card_id": self.idol_card_id,
            "loadout_snapshot_digest": self.loadout_snapshot_digest,
            "initial_reconciliation_digest": self.initial_reconciliation_digest,
            "contextual_session_digest": self.contextual_session_digest,
            "equipped_item_snapshot_digest": self.equipped_item_snapshot_digest,
            "deck_snapshot_digest": self.deck_snapshot_digest,
            "context_id": self.context_id,
            "context_digest": self.context_digest,
            "stage": self.stage,
            "stage_number": self.stage_number,
            "entry_status_observation": self.entry_status_observation.to_dict(),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any]
    ) -> "LiveExamPreflightPublicationMarker":
        expected = frozenset(
            {
                "schema_version",
                "run_id",
                "produce_id",
                "idol_card_id",
                "loadout_snapshot_digest",
                "initial_reconciliation_digest",
                "contextual_session_digest",
                "equipped_item_snapshot_digest",
                "deck_snapshot_digest",
                "context_id",
                "context_digest",
                "stage",
                "stage_number",
                "entry_status_observation",
            }
        )
        _exact_fields(payload, expected, "live Exam preflight publication marker")
        raw_observation = payload["entry_status_observation"]
        if not isinstance(raw_observation, Mapping):
            raise ValueError("entry_status_observation must be a JSON object")
        return cls(
            run_id=_strict_text(payload["run_id"], "publication run_id"),
            produce_id=_strict_text(
                payload["produce_id"], "publication produce_id"
            ),
            idol_card_id=_strict_text(
                payload["idol_card_id"], "publication idol_card_id"
            ),
            loadout_snapshot_digest=_strict_digest(
                payload["loadout_snapshot_digest"],
                "publication loadout_snapshot_digest",
            ),
            initial_reconciliation_digest=_strict_digest(
                payload["initial_reconciliation_digest"],
                "publication initial_reconciliation_digest",
            ),
            contextual_session_digest=_strict_digest(
                payload["contextual_session_digest"],
                "publication contextual_session_digest",
            ),
            equipped_item_snapshot_digest=_strict_digest(
                payload["equipped_item_snapshot_digest"],
                "publication equipped_item_snapshot_digest",
            ),
            deck_snapshot_digest=_strict_digest(
                payload["deck_snapshot_digest"],
                "publication deck_snapshot_digest",
            ),
            context_id=_strict_text(
                payload["context_id"], "publication context_id"
            ),
            context_digest=_strict_digest(
                payload["context_digest"], "publication context_digest"
            ),
            stage=_strict_text(payload["stage"], "publication stage"),
            stage_number=_strict_status(
                payload["stage_number"], "publication stage_number"
            ),
            entry_status_observation=LiveExamEntryStatusObservation.from_dict(
                raw_observation
            ),
            schema_version=payload["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class LiveExamBootstrapStatus:
    """The exact already-observed state used to create an Exam round-one state."""

    context_id: str
    context_digest: str
    entry_status_observation_digest: str
    observed_good_impression: int
    observed_motivation: int
    good_impression_exists: bool
    good_impression_passing_turn_start: bool
    installed_occurrence_keys: tuple[str, ...]
    runtime_status_enchants: tuple[ActiveRuntimeStatusEnchant, ...]
    last_resolved_turn_start_round: int
    last_resolved_runtime_status_round: int
    turn_start_already_absorbed: bool
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.context_id, str) or not self.context_id:
            raise ValueError("context_id must be non-empty text")
        if (
            not isinstance(self.context_digest, str)
            or len(self.context_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.context_digest)
        ):
            raise ValueError("context_digest must be a lowercase SHA-256 digest")
        _strict_digest(
            self.entry_status_observation_digest,
            "entry_status_observation_digest",
        )
        _strict_status(self.observed_good_impression, "observed_good_impression")
        _strict_status(self.observed_motivation, "observed_motivation")
        if not isinstance(self.good_impression_exists, bool):
            raise ValueError("good_impression_exists must be a boolean")
        if not isinstance(self.good_impression_passing_turn_start, bool):
            raise ValueError(
                "good_impression_passing_turn_start must be a boolean"
            )
        _strict_status(
            self.last_resolved_turn_start_round,
            "last_resolved_turn_start_round",
        )
        _strict_status(
            self.last_resolved_runtime_status_round,
            "last_resolved_runtime_status_round",
        )
        if not isinstance(self.turn_start_already_absorbed, bool):
            raise ValueError("turn_start_already_absorbed must be a boolean")
        if not isinstance(self.installed_occurrence_keys, tuple) or not all(
            isinstance(value, str) and value
            for value in self.installed_occurrence_keys
        ):
            raise TypeError(
                "installed_occurrence_keys must be a tuple of non-empty strings"
            )
        if self.installed_occurrence_keys != tuple(
            sorted(set(self.installed_occurrence_keys))
        ):
            raise ValueError("installed_occurrence_keys must be sorted and unique")
        if not isinstance(self.runtime_status_enchants, tuple) or not all(
            isinstance(value, ActiveRuntimeStatusEnchant)
            for value in self.runtime_status_enchants
        ):
            raise TypeError(
                "runtime_status_enchants must contain ActiveRuntimeStatusEnchant"
            )
        instance_ids = tuple(
            value.instance_id for value in self.runtime_status_enchants
        )
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError(
                "runtime_status_enchants must have unique instance IDs"
            )
        if any(
            not value.instance_id
            or not value.enchant_id
            or not isinstance(value.max_uses, int)
            or isinstance(value.max_uses, bool)
            or value.max_uses < 0
            or not isinstance(value.uses, int)
            or isinstance(value.uses, bool)
            or value.uses < 0
            or (value.max_uses > 0 and value.uses > value.max_uses)
            for value in self.runtime_status_enchants
        ):
            raise ValueError("runtime_status_enchants contains an invalid bootstrap entry")
        object.__setattr__(self, "digest", _canonical_digest(self._digest_payload()))

    def _digest_payload(self) -> dict[str, Any]:
        return {
            "context_id": self.context_id,
            "context_digest": self.context_digest,
            "entry_status_observation_digest": self.entry_status_observation_digest,
            "observed_good_impression": self.observed_good_impression,
            "observed_motivation": self.observed_motivation,
            "good_impression_exists": self.good_impression_exists,
            "good_impression_passing_turn_start": (
                self.good_impression_passing_turn_start
            ),
            "installed_occurrence_keys": list(self.installed_occurrence_keys),
            "runtime_status_enchants": [
                {
                    "instance_id": value.instance_id,
                    "enchant_id": value.enchant_id,
                    "max_uses": value.max_uses,
                    "uses": value.uses,
                }
                for value in self.runtime_status_enchants
            ],
            "last_resolved_turn_start_round": self.last_resolved_turn_start_round,
            "last_resolved_runtime_status_round": self.last_resolved_runtime_status_round,
            "turn_start_already_absorbed": self.turn_start_already_absorbed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._digest_payload(), "digest": self.digest}

    @property
    def runtime_status_enchant_ids(self) -> tuple[str, ...]:
        return tuple(value.enchant_id for value in self.runtime_status_enchants)


@dataclass(frozen=True, slots=True)
class LoadoutPreflightPublication:
    loadout_snapshot_digest: str
    initial_reconciliation_digest: str
    contextual_session_digest: str
    equipped_item_snapshot_digest: str
    deck_snapshot_digest: str
    contextual_session: ContextualPassiveStepSession
    replayed: bool


@dataclass(frozen=True, slots=True)
class LiveExamPreflightResolution:
    """Immutable audit output.  ``ready`` is false for every mismatch."""

    loadout_snapshot_digest: str | None
    initial_reconciliation_digest: str | None
    contextual_session_digest: str | None
    equipped_item_snapshot_digest: str | None
    deck_snapshot_digest: str | None
    bootstrap_status: LiveExamBootstrapStatus | None
    merged_item_rule: EquippedItemRule | None
    item_source_ids: tuple[str, ...]
    passive_source_ids: tuple[str, ...]
    rule_ids: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.blockers and self.bootstrap_status is not None

    @property
    def safe_to_bootstrap(self) -> bool:
        return self.ready


class LiveExamPreflightBlockedError(RuntimeError):
    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = _deduplicate(reasons)
        if not self.reasons:
            raise ValueError("a blocked publication requires at least one reason")
        super().__init__("live Exam preflight blocked: " + "; ".join(self.reasons))


def _resolve_sources(
    snapshot: LoadoutSnapshot,
    catalog: MasterPassiveCatalog,
) -> tuple[tuple[PassiveSource, ...], tuple[str, ...]]:
    """Narrow catalog adapter; contextual and initial gates own rule support."""

    reasons = list(snapshot.observation_blocking_reasons())
    sources: list[PassiveSource] = []
    for memory in snapshot.memories:
        for index, ability in enumerate(memory.abilities, start=1):
            if ability.ability_id is None or ability.level is None:
                continue
            selection = MemoryAbilitySelection(ability.ability_id, ability.level)
            try:
                source = catalog.resolve_memory_ability(selection.id, selection.level)
            except (KeyError, TypeError, ValueError) as error:
                reasons.append(
                    f"catalog-memory-resolution-failed:{memory.slot}:{index}:"
                    f"{selection.id}:{type(error).__name__}"
                )
                continue
            if not isinstance(source, PassiveSource):
                reasons.append(f"catalog-memory-source-malformed:{memory.slot}:{index}")
                continue
            sources.append(source)
    for slot in snapshot.support_cards:
        if slot.card_id is None or slot.level is None:
            continue
        selection = SupportCardSelection(slot.card_id, slot.level)
        try:
            resolved = catalog.resolve_support_card(selection.id, selection.level)
        except (KeyError, TypeError, ValueError) as error:
            reasons.append(
                f"catalog-support-resolution-failed:{slot.slot}:{selection.id}:"
                f"{type(error).__name__}"
            )
            continue
        if not isinstance(resolved, tuple) or not all(
            isinstance(source, PassiveSource) for source in resolved
        ):
            reasons.append(f"catalog-support-source-malformed:{slot.slot}")
            continue
        sources.extend(resolved)
    return tuple(sources), _deduplicate(reasons)


def _identity_reasons(
    identity: RunIdentity,
    snapshot: LoadoutSnapshot,
    context: ContextualPassiveStepContext,
    *,
    root: Path,
) -> tuple[str, ...]:
    reasons: list[str] = []
    try:
        stored = load_run(identity.run_id, root=root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        stored = None
        reasons.append(f"run-manifest-invalid:{type(error).__name__}")
    if stored is not None and stored != identity:
        reasons.append("run-identity-mismatch")
    try:
        active = load_active_run(root=root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        active = None
        reasons.append(f"active-run-invalid:{type(error).__name__}")
    if active is None:
        reasons.append("active-run-missing")
    elif active != identity:
        reasons.append("active-run-identity-mismatch")
    digest = loadout_snapshot_digest(snapshot)
    if snapshot.run_id != identity.run_id:
        reasons.append("loadout-run-id-mismatch")
    if snapshot.produce_id != identity.produce_id:
        reasons.append("loadout-produce-id-mismatch")
    if context.run_id != identity.run_id:
        reasons.append("context-run-id-mismatch")
    if context.produce_id != identity.produce_id:
        reasons.append("context-produce-id-mismatch")
    if context.snapshot_digest != digest:
        reasons.append("context-loadout-digest-mismatch")
    if context.stage not in EXAM_STAGES:
        reasons.append("context-stage-not-exam")
    return _deduplicate(reasons)


def _deck_reasons(
    record: RunDeckSnapshot | None,
    identity: RunIdentity,
    context: ContextualPassiveStepContext,
    stage_number: int,
    database: Path,
) -> tuple[str, ...]:
    if record is None:
        return ("deck-snapshot-missing",)
    reasons: list[str] = []
    if record.run_id != identity.run_id:
        reasons.append("deck-run-id-mismatch")
    deck = record.snapshot
    try:
        deck.validate()
    except (TypeError, ValueError) as error:
        reasons.append(f"deck-snapshot-invalid:{type(error).__name__}")
    try:
        authoritative = deck.is_authoritative()
    except (TypeError, ValueError):
        authoritative = False
    if not authoritative:
        reasons.append("deck-snapshot-not-authoritative")
    if deck.produce_id != identity.produce_id:
        reasons.append("deck-produce-id-mismatch")
    if deck.step_type != context.stage:
        reasons.append("deck-step-type-mismatch")
    if deck.stage_number != stage_number:
        reasons.append("deck-stage-number-mismatch")
    if not record.evidence_valid:
        reasons.append("deck-evidence-invalid")
    for card in deck.cards:
        try:
            replayed = load_master_card(card.card_id, card.upgrade, database)
        except (KeyError, OSError, TypeError, ValueError, sqlite3.Error) as error:
            reasons.append(
                "deck-master-replay-failed:"
                f"{card.card_id}@{card.upgrade}:{type(error).__name__}"
            )
            continue
        if replayed.id != card.card_id or replayed.upgrade != card.upgrade:
            reasons.append(
                f"deck-master-identity-mismatch:{card.card_id}@{card.upgrade}"
            )
    return _deduplicate(reasons)


def _supplement_reasons(
    identity: RunIdentity,
    snapshot: LoadoutSnapshot,
    initial: InitialModifierReconciliation,
    contextual: ContextualPassiveStepSession,
    equipped: EquippedItemSnapshot,
    sources: Sequence[PassiveSource],
    context: ContextualPassiveStepContext,
    outcomes: Sequence[EvidenceBoundPassiveOutcome],
    catalog: MasterPassiveCatalog,
    database: Path,
) -> tuple[tuple[str, ...], ContextualPassiveStepResult | None, EquippedItemSnapshotResolution]:
    reasons: list[str] = []
    digest = loadout_snapshot_digest(snapshot)
    try:
        replay_initial_modifier_reconciliation(initial, snapshot, catalog)
    except (InitialModifierReconciliationError, TypeError, ValueError) as error:
        if isinstance(error, InitialModifierReconciliationError):
            reasons.extend(f"initial:{reason}" for reason in error.reasons)
        else:
            reasons.append(f"initial-replay-failed:{type(error).__name__}:{error}")
    if initial.run_id != identity.run_id:
        reasons.append("initial-run-id-mismatch")
    if initial.produce_id != identity.produce_id:
        reasons.append("initial-produce-id-mismatch")
    if initial.snapshot_digest != digest:
        reasons.append("initial-loadout-digest-mismatch")

    if contextual.run_id != identity.run_id:
        reasons.append("contextual-run-id-mismatch")
    if contextual.produce_id != identity.produce_id:
        reasons.append("contextual-produce-id-mismatch")
    if contextual.snapshot_digest != digest:
        reasons.append("contextual-loadout-digest-mismatch")
    contextual_result: ContextualPassiveStepResult | None = None
    try:
        contextual_result = resolve_contextual_passive_step(
            contextual, context, sources, outcomes
        )
    except (OSError, TypeError, ValueError) as error:
        reasons.append(f"contextual-replay-failed:{type(error).__name__}:{error}")
    if contextual_result is not None:
        reasons.extend(_blocker_text(blocker) for blocker in contextual_result.blockers)
        if not any(
            record.context_id == context.context_id
            and record.context_digest == context.digest
            for record in contextual_result.session.records
        ):
            reasons.append("contextual-current-record-missing")

    if equipped.run_id != identity.run_id:
        reasons.append("equipped-item-run-id-mismatch")
    if equipped.idol_card_id != identity.idol_card_id:
        reasons.append("equipped-item-idol-card-id-mismatch")
    if equipped.loadout_snapshot_digest != digest:
        reasons.append("equipped-item-loadout-digest-mismatch")
    item_resolution = equipped.audit_resolution(database=database)
    reasons.extend(
        f"equipped-item:{reason}" for reason in item_resolution.blocking_reasons
    )
    if not item_resolution.decision_ready:
        reasons.append("equipped-item-not-decision-ready")
    return _deduplicate(reasons), contextual_result, item_resolution


def _load_deck(
    identity: RunIdentity, root: Path
) -> tuple[RunDeckSnapshot | None, tuple[str, ...]]:
    try:
        return load_run_deck_snapshot(identity, root=root), ()
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, (f"deck-snapshot-load-failed:{type(error).__name__}:{error}",)


def _load_loadout_prerequisite(path: Path) -> LoadoutSnapshot | None:
    """Load the prerequisite without inheriting its legacy lenient decoder."""

    path = Path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("loadout prerequisite path is not a regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("loadout prerequisite must be a JSON object")
    snapshot = LoadoutSnapshot.from_dict(payload)
    if dict(payload) != snapshot.to_dict():
        raise ValueError("loadout prerequisite fields or values are not canonical")
    return snapshot


def _load_publication_marker(
    path: Path,
) -> LiveExamPreflightPublicationMarker | None:
    path = Path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("live Exam preflight marker path is not a regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("live Exam preflight marker must be a JSON object")
    marker = LiveExamPreflightPublicationMarker.from_dict(payload)
    if dict(payload) != marker.to_dict():
        raise ValueError("live Exam preflight marker is not canonical")
    return marker


@contextmanager
def _publication_lock(path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Hold one cross-process lock for the requested publication scope."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise LiveExamPreflightBlockedError(
                            ("publication-lock-timeout",)
                        ) from error
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _create_publication_marker(
    marker: LiveExamPreflightPublicationMarker, path: Path
) -> None:
    """Atomically create, but never replace, the immutable final marker."""

    if not isinstance(marker, LiveExamPreflightPublicationMarker):
        raise TypeError("marker must be LiveExamPreflightPublicationMarker")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    marker.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        # A hard-link installation is same-filesystem, atomic, and fails if the
        # destination appeared after the lock-time read.  Unlike replace(), it
        # can never overwrite a competing publication.
        os.link(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _runtime_instance_id(
    snapshot_digest: str, context_digest: str, occurrence_key: str
) -> str:
    encoded = (
        snapshot_digest + "\0" + context_digest + "\0" + occurrence_key
    ).encode("utf-8")
    return "preflight:" + hashlib.sha256(encoded).hexdigest()


def _simulate_bootstrap(
    *,
    contextual_result: ContextualPassiveStepResult,
    context: ContextualPassiveStepContext,
    observation: LiveExamEntryStatusObservation,
    database: Path,
) -> tuple[LiveExamBootstrapStatus | None, tuple[str, ...]]:
    """Replay installed entry statuses once and bind the exact settled result."""

    installed = tuple(
        sorted(set(contextual_result.installed_source_occurrence_keys))
    )
    current_by_key = {
        occurrence.source_occurrence_key: occurrence
        for occurrence in contextual_result.resolution.current_required
    }
    reasons: list[str] = []
    initial_runtime: list[ActiveRuntimeStatusEnchant] = []
    for key in installed:
        occurrence = current_by_key.get(key)
        if occurrence is None:
            reasons.append(f"bootstrap-installed-occurrence-missing:{key}")
            continue
        if occurrence.status_enchant_id is None:
            reasons.append(f"bootstrap-installed-status-enchant-missing:{key}")
            continue
        initial_runtime.append(
            ActiveRuntimeStatusEnchant(
                instance_id=_runtime_instance_id(
                    context.snapshot_digest, context.digest, key
                ),
                enchant_id=occurrence.status_enchant_id,
                max_uses=0,
                uses=0,
            )
        )
    if initial_runtime and database.resolve() != Path(DEFAULT_DATABASE).resolve():
        reasons.append("bootstrap-status-master-database-unsupported")
    if reasons:
        return None, _deduplicate(reasons)

    clean = LogicExamState(
        turns_remaining=1,
        stamina=0,
        good_impression=0,
        motivation=0,
        round_number=context.turn,
        last_resolved_turn_start_round=context.turn,
        runtime_status_enchants=tuple(initial_runtime),
        last_resolved_runtime_status_round=0,
        good_impression_exists=False,
        good_impression_passing_turn_start=False,
    )
    try:
        replayed = resolve_logic_status_turn_start(clean)
    except (KeyError, OSError, TypeError, ValueError, sqlite3.Error) as error:
        return None, (
            f"bootstrap-status-replay-failed:{type(error).__name__}:{error}",
        )
    if replayed.unsupported_rules:
        reasons.extend(
            f"bootstrap-status-unsupported:{reason}"
            for reason in replayed.unsupported_rules
        )
    simulated = replace(
        replayed.after,
        last_resolved_runtime_status_round=context.turn,
    )
    representable = replace(
        clean,
        good_impression=simulated.good_impression,
        motivation=simulated.motivation,
        runtime_status_enchants=simulated.runtime_status_enchants,
        last_resolved_runtime_status_round=context.turn,
        good_impression_exists=simulated.good_impression_exists,
        good_impression_passing_turn_start=(
            simulated.good_impression_passing_turn_start
        ),
    )
    if replayed.draw_count != 0 or simulated != representable:
        reasons.append("bootstrap-status-result-not-representable")
    if simulated.good_impression != observation.observed_good_impression:
        reasons.append(
            "entry-status-good-impression-mismatch:"
            f"{observation.observed_good_impression}!={simulated.good_impression}"
        )
    if simulated.motivation != observation.observed_motivation:
        reasons.append(
            "entry-status-motivation-mismatch:"
            f"{observation.observed_motivation}!={simulated.motivation}"
        )
    if reasons:
        return None, _deduplicate(reasons)
    return (
        LiveExamBootstrapStatus(
            context_id=context.context_id,
            context_digest=context.digest,
            entry_status_observation_digest=observation.digest,
            observed_good_impression=simulated.good_impression,
            observed_motivation=simulated.motivation,
            good_impression_exists=bool(simulated.good_impression_exists),
            good_impression_passing_turn_start=bool(
                simulated.good_impression_passing_turn_start
            ),
            installed_occurrence_keys=installed,
            runtime_status_enchants=simulated.runtime_status_enchants,
            last_resolved_turn_start_round=context.turn,
            last_resolved_runtime_status_round=context.turn,
            turn_start_already_absorbed=True,
        ),
        (),
    )


def apply_preflight_bootstrap(
    state: LogicExamState,
    bootstrap: LiveExamBootstrapStatus,
    *,
    context_id: str,
    context_digest: str,
    applied_bootstrap_digest: str | None = None,
) -> tuple[LogicExamState, str]:
    """Apply one bootstrap to a clean state, returning its persistence token.

    The returned digest must be stored by the owning session and supplied on a
    repeated call.  Without that session token a different context cannot be
    distinguished from a fresh process, so this helper refuses non-clean state.
    """

    if not isinstance(state, LogicExamState):
        raise TypeError("state must be LogicExamState")
    if not isinstance(bootstrap, LiveExamBootstrapStatus):
        raise TypeError("bootstrap must be LiveExamBootstrapStatus")
    if context_id != bootstrap.context_id or context_digest != bootstrap.context_digest:
        raise LiveExamPreflightBlockedError(("bootstrap-context-mismatch",))
    target_matches = (
        state.good_impression == bootstrap.observed_good_impression
        and state.motivation == bootstrap.observed_motivation
        and state.good_impression_exists == bootstrap.good_impression_exists
        and state.good_impression_passing_turn_start
        == bootstrap.good_impression_passing_turn_start
        and state.runtime_status_enchants == bootstrap.runtime_status_enchants
        and state.last_resolved_turn_start_round
        == bootstrap.last_resolved_turn_start_round
        and state.last_resolved_runtime_status_round
        == bootstrap.last_resolved_runtime_status_round
    )
    if applied_bootstrap_digest is not None:
        if applied_bootstrap_digest != bootstrap.digest:
            raise LiveExamPreflightBlockedError(
                ("bootstrap-already-applied-with-different-digest",)
            )
        if not target_matches:
            raise LiveExamPreflightBlockedError(
                ("bootstrap-idempotent-state-mismatch",)
            )
        return state, applied_bootstrap_digest
    clean = (
        state.round_number == bootstrap.last_resolved_turn_start_round
        and state.good_impression == 0
        and state.motivation == 0
        and not state.runtime_status_enchants
        and state.last_resolved_turn_start_round == 0
        and state.last_resolved_runtime_status_round == 0
        and state.good_impression_exists is False
        and state.good_impression_passing_turn_start is False
    )
    if not clean:
        raise LiveExamPreflightBlockedError(
            ("bootstrap-requires-clean-state-or-session-token",)
        )
    updated = replace(
        state,
        good_impression=bootstrap.observed_good_impression,
        motivation=bootstrap.observed_motivation,
        good_impression_exists=bootstrap.good_impression_exists,
        good_impression_passing_turn_start=(
            bootstrap.good_impression_passing_turn_start
        ),
        runtime_status_enchants=bootstrap.runtime_status_enchants,
        last_resolved_turn_start_round=bootstrap.last_resolved_turn_start_round,
        last_resolved_runtime_status_round=(
            bootstrap.last_resolved_runtime_status_round
        ),
    )
    updated.validate()
    return updated, bootstrap.digest


def publish_loadout_preflight(
    *,
    identity: RunIdentity,
    loadout_snapshot: LoadoutSnapshot,
    initial_reconciliation: InitialModifierReconciliation,
    contextual_session: ContextualPassiveStepSession,
    equipped_item_snapshot: EquippedItemSnapshot,
    context: ContextualPassiveStepContext,
    entry_status_observation: LiveExamEntryStatusObservation,
    stage_number: int,
    catalog: MasterPassiveCatalog,
    contextual_outcomes: Sequence[EvidenceBoundPassiveOutcome] = (),
    root: Path = DEFAULT_RUN_ROOT,
    database: Path = DEFAULT_DATABASE,
    deck_snapshot: RunDeckSnapshot | None = None,
) -> LoadoutPreflightPublication:
    """Publish a fully replayable bundle with a dedicated marker written last.

    An existing marker makes the publication immutable.  Only an exact replay
    of both the marker and every supplemental artifact is accepted, and that
    replay performs no writes.
    """

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be RunIdentity")
    if not isinstance(loadout_snapshot, LoadoutSnapshot):
        raise TypeError("loadout_snapshot must be LoadoutSnapshot")
    if not isinstance(initial_reconciliation, InitialModifierReconciliation):
        raise TypeError("initial_reconciliation must be InitialModifierReconciliation")
    if not isinstance(contextual_session, ContextualPassiveStepSession):
        raise TypeError("contextual_session must be ContextualPassiveStepSession")
    if not isinstance(equipped_item_snapshot, EquippedItemSnapshot):
        raise TypeError("equipped_item_snapshot must be EquippedItemSnapshot")
    if not isinstance(context, ContextualPassiveStepContext):
        raise TypeError("context must be ContextualPassiveStepContext")
    if not isinstance(entry_status_observation, LiveExamEntryStatusObservation):
        raise TypeError(
            "entry_status_observation must be LiveExamEntryStatusObservation"
        )
    if not isinstance(catalog, MasterPassiveCatalog):
        raise TypeError("catalog must be MasterPassiveCatalog")
    if not isinstance(stage_number, int) or isinstance(stage_number, bool) or stage_number < 1:
        raise ValueError("stage_number must be an integer >= 1")
    if not all(isinstance(value, EvidenceBoundPassiveOutcome) for value in contextual_outcomes):
        raise TypeError("contextual_outcomes must contain EvidenceBoundPassiveOutcome")
    root = Path(root)
    database = Path(database)
    paths = paths_for(identity, root=root)
    marker_path = paths.live_exam_preflight_publication_for(
        context_id=context.context_id,
        context_digest=context.digest,
        stage_number=stage_number,
    )
    marker_lock = paths.live_exam_preflight_lock_for(
        context_id=context.context_id,
        context_digest=context.digest,
        stage_number=stage_number,
    )
    # The context lock makes create-once/idempotency local to this exact Exam.
    # The bundle lock additionally serializes the still run-wide prerequisite
    # files so concurrent Mid1/Final publishers cannot interleave their writes.
    with _publication_lock(paths.live_exam_preflight_bundle_lock), _publication_lock(
        marker_lock
    ):
        # Both the prerequisite and final marker are re-read under the lock.
        try:
            final = _load_publication_marker(marker_path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LiveExamPreflightBlockedError(
                (f"preflight-publication-marker-invalid:{type(error).__name__}:{error}",)
            ) from error
        legacy_final: LiveExamPreflightPublicationMarker | None = None
        if final is None:
            try:
                legacy_final = _load_publication_marker(
                    paths.live_exam_preflight_publication
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise LiveExamPreflightBlockedError(
                    (
                        "legacy-preflight-publication-marker-invalid:"
                        f"{type(error).__name__}:{error}",
                    )
                ) from error
        try:
            stored_loadout = _load_loadout_prerequisite(paths.loadout_snapshot)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise LiveExamPreflightBlockedError(
                (f"loadout-prerequisite-invalid:{type(error).__name__}:{error}",)
            ) from error
        if stored_loadout is not None and stored_loadout != loadout_snapshot:
            raise LiveExamPreflightBlockedError(
                ("loadout-prerequisite-mismatch",)
            )
        if (final is not None or legacy_final is not None) and stored_loadout is None:
            raise LiveExamPreflightBlockedError(
                ("loadout-prerequisite-missing",)
            )

        reasons = list(
            _identity_reasons(identity, loadout_snapshot, context, root=root)
        )
        snapshot_digest = loadout_snapshot_digest(loadout_snapshot)
        reasons.extend(
            entry_status_observation.validation_reasons(
                identity, context, snapshot_digest
            )
        )
        sources, source_reasons = _resolve_sources(loadout_snapshot, catalog)
        reasons.extend(source_reasons)
        stored_deck, deck_load_reasons = _load_deck(identity, root)
        reasons.extend(deck_load_reasons)
        if deck_snapshot is not None and stored_deck != deck_snapshot:
            reasons.append("provided-deck-snapshot-mismatch")
        selected_deck = stored_deck if deck_snapshot is None else deck_snapshot
        reasons.extend(
            _deck_reasons(
                selected_deck, identity, context, stage_number, database
            )
        )
        supplement_reasons, contextual_result, _item_resolution = (
            _supplement_reasons(
                identity,
                loadout_snapshot,
                initial_reconciliation,
                contextual_session,
                equipped_item_snapshot,
                sources,
                context,
                contextual_outcomes,
                catalog,
                database,
            )
        )
        reasons.extend(supplement_reasons)
        bootstrap = None
        if contextual_result is not None:
            bootstrap, bootstrap_reasons = _simulate_bootstrap(
                contextual_result=contextual_result,
                context=context,
                observation=entry_status_observation,
                database=database,
            )
            reasons.extend(bootstrap_reasons)
        if (
            reasons
            or contextual_result is None
            or selected_deck is None
            or bootstrap is None
        ):
            raise LiveExamPreflightBlockedError(
                reasons or ("preflight-validation-failed",)
            )
        candidate_contextual = contextual_result.session

        initial_digest = _canonical_digest(initial_reconciliation.to_dict())
        contextual_digest = _canonical_digest(candidate_contextual.to_dict())
        item_digest = _canonical_digest(equipped_item_snapshot.to_dict())
        deck_digest = _canonical_digest(selected_deck.to_dict())
        candidate_marker = LiveExamPreflightPublicationMarker(
            run_id=identity.run_id,
            produce_id=identity.produce_id,
            idol_card_id=identity.idol_card_id,
            loadout_snapshot_digest=snapshot_digest,
            initial_reconciliation_digest=initial_digest,
            contextual_session_digest=contextual_digest,
            equipped_item_snapshot_digest=item_digest,
            deck_snapshot_digest=deck_digest,
            context_id=context.context_id,
            context_digest=context.digest,
            stage=context.stage,
            stage_number=stage_number,
            entry_status_observation=entry_status_observation,
        )

        migrating_legacy = (
            final is None
            and legacy_final is not None
            and legacy_final == candidate_marker
        )
        replay_marker = legacy_final if migrating_legacy else final
        if replay_marker is not None:
            if replay_marker != candidate_marker:
                raise LiveExamPreflightBlockedError(
                    ("preflight-publication-marker-mismatch",)
                )
            # Exact idempotent replay never repairs supplemental data.
            try:
                stored_initial = load_initial_modifier_reconciliation(
                    paths.initial_modifier_reconciliation
                )
                stored_contextual = load_contextual_passive_step_session(
                    paths.contextual_passive_session
                )
                stored_item = load_equipped_item_snapshot(
                    paths.equipped_item_snapshot, database=database
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise LiveExamPreflightBlockedError(
                    (f"stored-supplement-invalid:{type(error).__name__}:{error}",)
                ) from error
            replay_reasons: list[str] = []
            if stored_initial != initial_reconciliation:
                replay_reasons.append("stored-initial-reconciliation-mismatch")
            if stored_contextual != candidate_contextual:
                replay_reasons.append("stored-contextual-session-mismatch")
            if stored_item != equipped_item_snapshot:
                replay_reasons.append("stored-equipped-item-snapshot-mismatch")
            if replay_reasons:
                raise LiveExamPreflightBlockedError(replay_reasons)
            assert (
                stored_initial is not None
                and stored_contextual is not None
                and stored_item is not None
            )
            replay_initial_modifier_reconciliation(
                stored_initial, stored_loadout or loadout_snapshot, catalog
            )
            replayed_context = resolve_contextual_passive_step(
                stored_contextual, context, sources
            )
            if replayed_context.blockers or not replayed_context.replayed:
                raise LiveExamPreflightBlockedError(
                    tuple(
                        _blocker_text(value)
                        for value in replayed_context.blockers
                    )
                    or ("stored-contextual-session-not-replayable",)
                )
            if not stored_item.audit_resolution(database=database).decision_ready:
                raise LiveExamPreflightBlockedError(
                    ("stored-equipped-item-not-ready",)
                )
            if migrating_legacy:
                try:
                    _create_publication_marker(candidate_marker, marker_path)
                except FileExistsError as error:
                    raise LiveExamPreflightBlockedError(
                        ("preflight-publication-marker-create-conflict",)
                    ) from error
                migrated = _load_publication_marker(marker_path)
                if migrated != candidate_marker:
                    raise LiveExamPreflightBlockedError(
                        ("preflight-publication-marker-round-trip-mismatch",)
                    )
            return LoadoutPreflightPublication(
                snapshot_digest,
                initial_digest,
                contextual_digest,
                item_digest,
                deck_digest,
                stored_contextual,
                True,
            )

        # The legacy loadout is a prerequisite, never the ready marker.
        if stored_loadout is None:
            save_loadout_snapshot(loadout_snapshot, paths.loadout_snapshot)
            stored_loadout = _load_loadout_prerequisite(paths.loadout_snapshot)
            if stored_loadout != loadout_snapshot:
                raise LiveExamPreflightBlockedError(
                    ("loadout-prerequisite-round-trip-mismatch",)
                )

        save_initial_modifier_reconciliation(
            initial_reconciliation, paths.initial_modifier_reconciliation
        )
        stored_initial = load_initial_modifier_reconciliation(
            paths.initial_modifier_reconciliation
        )
        if stored_initial != initial_reconciliation:
            raise LiveExamPreflightBlockedError(("initial-round-trip-mismatch",))
        assert stored_initial is not None
        replay_initial_modifier_reconciliation(
            stored_initial, stored_loadout, catalog
        )

        save_contextual_passive_step_session(
            candidate_contextual, paths.contextual_passive_session
        )
        stored_contextual = load_contextual_passive_step_session(
            paths.contextual_passive_session
        )
        if stored_contextual != candidate_contextual:
            raise LiveExamPreflightBlockedError(("contextual-round-trip-mismatch",))
        assert stored_contextual is not None
        replayed_context = resolve_contextual_passive_step(
            stored_contextual, context, sources
        )
        if replayed_context.blockers or not replayed_context.replayed:
            raise LiveExamPreflightBlockedError(
                tuple(
                    _blocker_text(value) for value in replayed_context.blockers
                )
                or ("contextual-round-trip-not-replayable",)
            )

        save_equipped_item_snapshot(
            equipped_item_snapshot, paths.equipped_item_snapshot
        )
        stored_item = load_equipped_item_snapshot(
            paths.equipped_item_snapshot, database=database
        )
        if stored_item != equipped_item_snapshot:
            raise LiveExamPreflightBlockedError(
                ("equipped-item-round-trip-mismatch",)
            )
        assert stored_item is not None
        if not stored_item.audit_resolution(database=database).decision_ready:
            raise LiveExamPreflightBlockedError(
                ("equipped-item-round-trip-not-ready",)
            )

        # Dedicated publication marker: deliberately the final create-once write.
        try:
            _create_publication_marker(candidate_marker, marker_path)
        except FileExistsError as error:
            raise LiveExamPreflightBlockedError(
                ("preflight-publication-marker-create-conflict",)
            ) from error
        stored_final = _load_publication_marker(marker_path)
        if stored_final != candidate_marker:
            raise LiveExamPreflightBlockedError(
                ("preflight-publication-marker-round-trip-mismatch",)
            )
        return LoadoutPreflightPublication(
            snapshot_digest,
            initial_digest,
            contextual_digest,
            item_digest,
            deck_digest,
            stored_contextual,
            False,
        )


def _rule_ids(
    sources: Sequence[PassiveSource],
    initial: InitialModifierReconciliation,
    item_resolution: EquippedItemSnapshotResolution,
    deck: RunDeckSnapshot,
) -> tuple[str, ...]:
    values: set[str] = set()
    for source in sources:
        values.update((source.source_id, source.skill_id))
        for rule in source.rules:
            values.update((rule.effect_id, rule.trigger_id))
            effect = rule.raw.get("effect") if isinstance(rule.raw, Mapping) else None
            if isinstance(effect, Mapping):
                status_id = effect.get("produceExamStatusEnchantId")
                if isinstance(status_id, str) and status_id:
                    values.add(status_id)
    for modifier in initial.produce_start_modifiers:
        values.update(
            (modifier.source_id, modifier.skill_id, modifier.effect_id, modifier.trigger_id)
        )
    for rule in item_resolution.item_rules:
        values.add(rule.id)
        for enchantment in rule.enchantments:
            values.update((enchantment.id, enchantment.trigger.id))
            values.update(effect.id for effect in enchantment.effects)
    for card in deck.snapshot.cards:
        values.add(f"{card.card_id}@{card.upgrade}")
    values.discard("")
    return tuple(sorted(values))


def resolve_live_exam_preflight(
    *,
    identity: RunIdentity,
    context: ContextualPassiveStepContext,
    stage_number: int,
    catalog: MasterPassiveCatalog,
    root: Path = DEFAULT_RUN_ROOT,
    database: Path = DEFAULT_DATABASE,
) -> LiveExamPreflightResolution:
    """Replay every published proof and return bootstrap metadata or blockers."""

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be RunIdentity")
    if not isinstance(context, ContextualPassiveStepContext):
        raise TypeError("context must be ContextualPassiveStepContext")
    if not isinstance(catalog, MasterPassiveCatalog):
        raise TypeError("catalog must be MasterPassiveCatalog")
    if not isinstance(stage_number, int) or isinstance(stage_number, bool) or stage_number < 1:
        raise ValueError("stage_number must be an integer >= 1")
    root = Path(root)
    database = Path(database)
    paths = paths_for(identity, root=root)
    blockers: list[str] = []

    loadout: LoadoutSnapshot | None = None
    initial: InitialModifierReconciliation | None = None
    contextual: ContextualPassiveStepSession | None = None
    equipped: EquippedItemSnapshot | None = None
    deck: RunDeckSnapshot | None = None
    marker_path = paths.live_exam_preflight_publication_for(
        context_id=context.context_id,
        context_digest=context.digest,
        stage_number=stage_number,
    )
    marker: LiveExamPreflightPublicationMarker | None = None
    try:
        marker = _load_publication_marker(marker_path)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        blockers.append(
            f"preflight-publication-marker-load-failed:{type(error).__name__}:{error}"
        )
    if marker is None:
        blockers.append("preflight-publication-marker-missing")
        try:
            legacy_marker = _load_publication_marker(
                paths.live_exam_preflight_publication
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            blockers.append(
                "legacy-preflight-publication-marker-load-failed:"
                f"{type(error).__name__}:{error}"
            )
        else:
            if (
                legacy_marker is not None
                and legacy_marker.run_id == identity.run_id
                and legacy_marker.context_id == context.context_id
                and legacy_marker.context_digest == context.digest
                and legacy_marker.stage == context.stage
                and legacy_marker.stage_number == stage_number
            ):
                blockers.append(
                    "legacy-preflight-publication-marker-migration-required"
                )
    try:
        loadout = _load_loadout_prerequisite(paths.loadout_snapshot)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        blockers.append(
            f"loadout-prerequisite-load-failed:{type(error).__name__}:{error}"
        )
    if loadout is None:
        blockers.append("loadout-prerequisite-missing")
    try:
        initial = load_initial_modifier_reconciliation(
            paths.initial_modifier_reconciliation
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        blockers.append(f"initial-load-failed:{type(error).__name__}:{error}")
    if initial is None:
        blockers.append("initial-reconciliation-missing")
    try:
        contextual = load_contextual_passive_step_session(
            paths.contextual_passive_session
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        blockers.append(f"contextual-load-failed:{type(error).__name__}:{error}")
    if contextual is None:
        blockers.append("contextual-session-missing")
    try:
        equipped = load_equipped_item_snapshot(
            paths.equipped_item_snapshot, database=database
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        blockers.append(f"equipped-item-load-failed:{type(error).__name__}:{error}")
    if equipped is None:
        blockers.append("equipped-item-snapshot-missing")
    deck, deck_load_reasons = _load_deck(identity, root)
    blockers.extend(deck_load_reasons)

    loadout_digest_value = (
        None if loadout is None else loadout_snapshot_digest(loadout)
    )
    initial_digest = (
        None if initial is None else _canonical_digest(initial.to_dict())
    )
    contextual_digest = (
        None if contextual is None else _canonical_digest(contextual.to_dict())
    )
    item_digest = (
        None if equipped is None else _canonical_digest(equipped.to_dict())
    )
    deck_digest = None if deck is None else _canonical_digest(deck.to_dict())

    if marker is not None:
        if marker.run_id != identity.run_id:
            blockers.append("publication-run-id-mismatch")
        if marker.produce_id != identity.produce_id:
            blockers.append("publication-produce-id-mismatch")
        if marker.idol_card_id != identity.idol_card_id:
            blockers.append("publication-idol-card-id-mismatch")
        if marker.context_id != context.context_id:
            blockers.append("publication-context-id-mismatch")
        if marker.context_digest != context.digest:
            blockers.append("publication-context-digest-mismatch")
        if marker.stage != context.stage:
            blockers.append("publication-stage-mismatch")
        if marker.stage_number != stage_number:
            blockers.append("publication-stage-number-mismatch")
        for label, expected, actual in (
            (
                "loadout",
                marker.loadout_snapshot_digest,
                loadout_digest_value,
            ),
            (
                "initial",
                marker.initial_reconciliation_digest,
                initial_digest,
            ),
            (
                "contextual",
                marker.contextual_session_digest,
                contextual_digest,
            ),
            (
                "equipped-item",
                marker.equipped_item_snapshot_digest,
                item_digest,
            ),
            ("deck", marker.deck_snapshot_digest, deck_digest),
        ):
            if actual is None or expected != actual:
                blockers.append(f"publication-{label}-digest-mismatch")
        if loadout_digest_value is not None:
            blockers.extend(
                marker.entry_status_observation.validation_reasons(
                    identity, context, loadout_digest_value
                )
            )

    sources: tuple[PassiveSource, ...] = ()
    item_resolution: EquippedItemSnapshotResolution | None = None
    contextual_result: ContextualPassiveStepResult | None = None
    if loadout is not None:
        blockers.extend(_identity_reasons(identity, loadout, context, root=root))
        sources, source_reasons = _resolve_sources(loadout, catalog)
        blockers.extend(source_reasons)
    if deck is not None:
        blockers.extend(
            _deck_reasons(deck, identity, context, stage_number, database)
        )
    elif not deck_load_reasons:
        blockers.append("deck-snapshot-missing")
    if loadout is not None and initial is not None:
        try:
            replay_initial_modifier_reconciliation(initial, loadout, catalog)
        except (InitialModifierReconciliationError, TypeError, ValueError) as error:
            if isinstance(error, InitialModifierReconciliationError):
                blockers.extend(f"initial:{reason}" for reason in error.reasons)
            else:
                blockers.append(f"initial-replay-failed:{type(error).__name__}:{error}")
    if loadout is not None and contextual is not None:
        if contextual.run_id != identity.run_id:
            blockers.append("contextual-run-id-mismatch")
        if contextual.produce_id != identity.produce_id:
            blockers.append("contextual-produce-id-mismatch")
        if contextual.snapshot_digest != loadout_digest_value:
            blockers.append("contextual-loadout-digest-mismatch")
        try:
            contextual_result = resolve_contextual_passive_step(
                contextual, context, sources
            )
        except (OSError, TypeError, ValueError) as error:
            blockers.append(f"contextual-replay-failed:{type(error).__name__}:{error}")
        if contextual_result is not None:
            blockers.extend(_blocker_text(value) for value in contextual_result.blockers)
            if not contextual_result.replayed:
                blockers.append("contextual-current-step-not-published")
    if loadout is not None and equipped is not None:
        if equipped.run_id != identity.run_id:
            blockers.append("equipped-item-run-id-mismatch")
        if equipped.idol_card_id != identity.idol_card_id:
            blockers.append("equipped-item-idol-card-id-mismatch")
        if equipped.loadout_snapshot_digest != loadout_digest_value:
            blockers.append("equipped-item-loadout-digest-mismatch")
        item_resolution = equipped.audit_resolution(database=database)
        blockers.extend(
            f"equipped-item:{reason}" for reason in item_resolution.blocking_reasons
        )
        if not item_resolution.decision_ready:
            blockers.append("equipped-item-not-decision-ready")

    bootstrap: LiveExamBootstrapStatus | None = None
    merged_item_rule: EquippedItemRule | None = None
    item_source_ids: tuple[str, ...] = ()
    passive_source_ids: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    if (
        not _deduplicate(blockers)
        and marker is not None
        and loadout is not None
        and initial is not None
        and contextual_result is not None
        and item_resolution is not None
        and item_resolution.merged_rule is not None
        and equipped is not None
        and deck is not None
    ):
        bootstrap, bootstrap_reasons = _simulate_bootstrap(
            contextual_result=contextual_result,
            context=context,
            observation=marker.entry_status_observation,
            database=database,
        )
        blockers.extend(bootstrap_reasons)
        if bootstrap is not None:
            merged_item_rule = item_resolution.merged_rule
            item_source_ids = tuple(sorted(equipped.item_ids))
            passive_source_ids = tuple(
                sorted({source.source_id for source in sources})
            )
            rule_ids = _rule_ids(sources, initial, item_resolution, deck)

    blockers_tuple = _deduplicate(blockers)
    if blockers_tuple:
        bootstrap = None
        merged_item_rule = None
        item_source_ids = ()
        passive_source_ids = ()
        rule_ids = ()

    return LiveExamPreflightResolution(
        loadout_digest_value,
        initial_digest,
        contextual_digest,
        item_digest,
        deck_digest,
        bootstrap,
        merged_item_rule,
        item_source_ids,
        passive_source_ids,
        rule_ids,
        blockers_tuple,
    )


__all__ = [
    "LiveExamEntryStatusObservation",
    "LiveExamBootstrapStatus",
    "LiveExamPreflightBlockedError",
    "LiveExamPreflightPublicationMarker",
    "LiveExamPreflightResolution",
    "LoadoutPreflightPublication",
    "apply_preflight_bootstrap",
    "publish_loadout_preflight",
    "resolve_live_exam_preflight",
]
