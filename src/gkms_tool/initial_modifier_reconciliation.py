"""Evidence-bound reconciliation for deterministic ProduceStart modifiers.

The existing loadout runtime bridge is intentionally a whole-loadout gate: it
must understand every rule before it can install anything into the live run
runtime.  This module serves a narrower purpose.  It resolves the complete
observed snapshot through :class:`MasterPassiveCatalog`, then considers only
fixed scalar rules whose trigger phase is ``ProduceStart``.  Rules for later
events, statuses, or future phases are retained by the catalog but do not
block this *initial visible-value* reconciliation.

Nothing in this module accepts or mutates ``RunShadowState``.  A successful
call returns immutable, provenance-bound metadata and the independently
observed visible post-values.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .loadout_runtime_bridge import loadout_snapshot_digest
from .loadout_snapshot import LoadoutSnapshot
from .passive_catalog import (
    EFFECT_DANCE_ADDITION,
    EFFECT_DANCE_GROWTH_RATE_ADDITION,
    EFFECT_PRODUCE_POINT_ADDITION,
    EFFECT_VISUAL_ADDITION,
    EFFECT_VISUAL_GROWTH_RATE_ADDITION,
    EFFECT_VOCAL_ADDITION,
    EFFECT_VOCAL_GROWTH_RATE_ADDITION,
    TRIGGER_PRODUCE_START_CONFIGURATION,
    TRIGGER_PRODUCE_START_INITIAL,
    MasterPassiveCatalog,
    MemoryAbilitySelection,
    PassiveRule,
    PassiveSource,
    SupportCardSelection,
)
from .passive_runtime import (
    EFFECT_LESSON_DANCE_SP_RATE,
    EFFECT_LESSON_SP_RATE,
    EFFECT_LESSON_VISUAL_SP_RATE,
    EFFECT_LESSON_VOCAL_SP_RATE,
    EFFECT_MAX_STAMINA_ADDITION,
    EFFECT_SUPPORT_CARD_UPGRADE_RATE,
    EFFECT_SUPPORT_EVENT_PARAMETER_UP,
    EFFECT_SUPPORT_EVENT_POINT_UP,
    EFFECT_SUPPORT_EVENT_STAMINA_UP,
    PHASE_PRODUCE_START,
)


SCHEMA_VERSION = 1
VISIBLE_FIELDS = ("vocal", "dance", "visual", "produce_points")


# The keys intentionally mirror the strict field mapping in passive_runtime.
# Keeping the mapping local means this targeted reconciliation can inspect
# only ProduceStart rules without invoking the bridge's whole-runtime gate.
_PRODUCE_START_TARGETS: dict[tuple[str, str], tuple[str, str | None]] = {
    (
        TRIGGER_PRODUCE_START_INITIAL,
        EFFECT_VOCAL_ADDITION,
    ): ("vocal", "vocal"),
    (
        TRIGGER_PRODUCE_START_INITIAL,
        EFFECT_DANCE_ADDITION,
    ): ("dance", "dance"),
    (
        TRIGGER_PRODUCE_START_INITIAL,
        EFFECT_VISUAL_ADDITION,
    ): ("visual", "visual"),
    (
        TRIGGER_PRODUCE_START_INITIAL,
        EFFECT_PRODUCE_POINT_ADDITION,
    ): ("produce_point_addition", "produce_points"),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_MAX_STAMINA_ADDITION,
    ): ("max_stamina_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_VOCAL_GROWTH_RATE_ADDITION,
    ): ("vocal_growth_rate_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_DANCE_GROWTH_RATE_ADDITION,
    ): ("dance_growth_rate_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_VISUAL_GROWTH_RATE_ADDITION,
    ): ("visual_growth_rate_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_SP_RATE,
    ): ("lesson_sp_change_rate_permil_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_VOCAL_SP_RATE,
    ): ("lesson_vocal_sp_change_rate_permil_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_DANCE_SP_RATE,
    ): ("lesson_dance_sp_change_rate_permil_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_VISUAL_SP_RATE,
    ): ("lesson_visual_sp_change_rate_permil_addition", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_EVENT_PARAMETER_UP,
    ): ("support_event_parameter_addition_value_up", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_EVENT_POINT_UP,
    ): ("support_event_produce_point_addition_value_up", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_EVENT_STAMINA_UP,
    ): ("support_event_stamina_recover_up", None),
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_CARD_UPGRADE_RATE,
    ): ("support_card_upgrade_probability_up", None),
}
_PRODUCE_START_TRIGGER_IDS = frozenset(
    trigger_id for trigger_id, _ in _PRODUCE_START_TARGETS
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _strict_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if not _is_int(value):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return int(value)


def _strict_sha256(value: object, label: str) -> str:
    value = _strict_text(value, label).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a 64-character SHA-256 hex digest")
    return value


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _require_exact_fields(
    payload: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = set(payload)
    if actual != set(expected):
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(set(expected) - actual)} "
            f"unknown={sorted(actual - set(expected))}"
        )


class InitialModifierReconciliationError(ValueError):
    """Raised when initial-modifier evidence or catalog data is unsafe."""

    def __init__(self, reasons: Sequence[str]) -> None:
        normalized = _deduplicate(tuple(str(reason) for reason in reasons))
        if not normalized:
            raise ValueError("at least one reconciliation blocker is required")
        self.reasons = normalized
        super().__init__("initial modifier reconciliation blocked: " + "; ".join(normalized))


@dataclass(frozen=True, slots=True)
class OverviewProof:
    """Proof that one capture is a stable, complete visible overview."""

    stable: bool
    full: bool
    fields: tuple[str, ...] = VISIBLE_FIELDS

    def __post_init__(self) -> None:
        _strict_bool(self.stable, "overview proof stable")
        _strict_bool(self.full, "overview proof full")
        if not isinstance(self.fields, tuple) or not self.fields:
            raise ValueError("overview proof fields must be a non-empty tuple")
        if any(not isinstance(value, str) or not value for value in self.fields):
            raise ValueError("overview proof fields must contain non-empty text")
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("overview proof fields must be unique")

    @property
    def stable_overview(self) -> bool:
        return self.stable

    @property
    def full_overview(self) -> bool:
        return self.full

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable": self.stable,
            "full": self.full,
            "fields": list(self.fields),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OverviewProof":
        if not isinstance(payload, Mapping):
            raise ValueError("overview proof must be an object")
        _require_exact_fields(
            payload, frozenset({"stable", "full", "fields"}), "overview proof"
        )
        raw_fields = payload["fields"]
        if not isinstance(raw_fields, list):
            raise ValueError("overview proof fields must be an array")
        return cls(
            stable=_strict_bool(payload.get("stable"), "overview proof stable"),
            full=_strict_bool(payload.get("full"), "overview proof full"),
            fields=tuple(
                _strict_text(value, "overview proof field") for value in raw_fields
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialModifierEvidence:
    """Identity, capture, and proof bindings required for reconciliation."""

    run_id: str
    produce_id: str
    snapshot_digest: str
    capture_path: str | Path
    capture_sha256: str
    overview_proof: OverviewProof

    def __post_init__(self) -> None:
        _strict_text(self.run_id, "evidence run_id")
        _strict_text(self.produce_id, "evidence produce_id")
        object.__setattr__(
            self,
            "snapshot_digest",
            _strict_sha256(self.snapshot_digest, "evidence snapshot_digest"),
        )
        if isinstance(self.capture_path, Path):
            capture_path = str(self.capture_path)
        else:
            capture_path = _strict_text(self.capture_path, "evidence capture_path")
        if not capture_path:
            raise ValueError("evidence capture_path must be non-empty text")
        object.__setattr__(self, "capture_path", capture_path)
        object.__setattr__(
            self,
            "capture_sha256",
            _strict_sha256(self.capture_sha256, "evidence capture_sha256"),
        )
        if not isinstance(self.overview_proof, OverviewProof):
            raise TypeError("evidence overview_proof must be OverviewProof")

    @property
    def capture_hash(self) -> str:
        return self.capture_sha256

    @property
    def stable_overview_proof(self) -> bool:
        return self.overview_proof.stable

    @property
    def full_overview_proof(self) -> bool:
        return self.overview_proof.full

    def verify_capture(self) -> None:
        path = Path(self.capture_path)
        if not path.is_file():
            raise InitialModifierReconciliationError(
                (f"evidence-capture-missing:{self.capture_path}",)
            )
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise InitialModifierReconciliationError(
                (f"evidence-capture-unreadable:{self.capture_path}:{type(error).__name__}",)
            ) from error
        if digest.hexdigest() != self.capture_sha256:
            raise InitialModifierReconciliationError(
                (
                    "evidence-capture-hash-mismatch:"
                    f"{self.capture_path}:{digest.hexdigest()}!={self.capture_sha256}",
                )
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "produce_id": self.produce_id,
            "snapshot_digest": self.snapshot_digest,
            "capture_path": self.capture_path,
            "capture_sha256": self.capture_sha256,
            "overview_proof": self.overview_proof.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InitialModifierEvidence":
        if not isinstance(payload, Mapping):
            raise ValueError("initial modifier evidence must be an object")
        _require_exact_fields(
            payload,
            frozenset({
                "run_id", "produce_id", "snapshot_digest", "capture_path",
                "capture_sha256", "overview_proof",
            }),
            "initial modifier evidence",
        )
        raw_proof = payload.get("overview_proof")
        if not isinstance(raw_proof, Mapping):
            raise ValueError("initial modifier overview_proof must be an object")
        return cls(
            run_id=_strict_text(payload.get("run_id"), "evidence run_id"),
            produce_id=_strict_text(payload.get("produce_id"), "evidence produce_id"),
            snapshot_digest=_strict_sha256(
                payload.get("snapshot_digest"), "evidence snapshot_digest"
            ),
            capture_path=payload.get("capture_path"),
            capture_sha256=_strict_sha256(
                payload.get("capture_sha256"), "evidence capture_sha256"
            ),
            overview_proof=OverviewProof.from_dict(raw_proof),
        )


@dataclass(frozen=True, slots=True)
class VisibleProduceValues:
    """The four visible scalar values proved before or after ProduceStart."""

    vocal: int
    dance: int
    visual: int
    produce_points: int

    def __post_init__(self) -> None:
        for name in VISIBLE_FIELDS:
            _strict_int(getattr(self, name), f"visible {name}", minimum=0)

    def to_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in VISIBLE_FIELDS}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisibleProduceValues":
        if not isinstance(payload, Mapping):
            raise ValueError("visible values must be an object")
        _require_exact_fields(payload, frozenset(VISIBLE_FIELDS), "visible values")
        return cls(
            vocal=_strict_int(payload.get("vocal"), "visible vocal", minimum=0),
            dance=_strict_int(payload.get("dance"), "visible dance", minimum=0),
            visual=_strict_int(payload.get("visual"), "visible visual", minimum=0),
            produce_points=_strict_int(
                payload.get("produce_points"), "visible produce_points", minimum=0
            ),
        )


@dataclass(frozen=True, slots=True)
class VisibleModifierDeltas:
    """Aggregated deterministic deltas for the four visible fields."""

    vocal: int
    dance: int
    visual: int
    produce_points: int

    def __post_init__(self) -> None:
        for name in VISIBLE_FIELDS:
            _strict_int(getattr(self, name), f"delta {name}")

    def to_dict(self) -> dict[str, int]:
        return {name: int(getattr(self, name)) for name in VISIBLE_FIELDS}

    def as_dict(self) -> dict[str, int]:
        return self.to_dict()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VisibleModifierDeltas":
        if not isinstance(payload, Mapping):
            raise ValueError("visible deltas must be an object")
        _require_exact_fields(payload, frozenset(VISIBLE_FIELDS), "visible deltas")
        return cls(
            vocal=_strict_int(payload.get("vocal"), "delta vocal"),
            dance=_strict_int(payload.get("dance"), "delta dance"),
            visual=_strict_int(payload.get("visual"), "delta visual"),
            produce_points=_strict_int(
                payload.get("produce_points"), "delta produce_points"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProduceStartModifier:
    """One verified, fixed-value ProduceStart scalar occurrence."""

    source_index: int
    source_kind: str
    source_id: str
    skill_id: str
    rule_slot: int
    effect_id: str
    effect_type: str
    trigger_id: str
    trigger_phase_type: str
    target_field: str
    value: int
    visible_field: str | None
    activation_count: int = 1
    activation_rate_permil: int = 0

    def __post_init__(self) -> None:
        _strict_int(self.source_index, "modifier source_index", minimum=0)
        for name, value in (
            ("source_kind", self.source_kind),
            ("source_id", self.source_id),
            ("skill_id", self.skill_id),
            ("effect_id", self.effect_id),
            ("effect_type", self.effect_type),
            ("trigger_id", self.trigger_id),
            ("trigger_phase_type", self.trigger_phase_type),
            ("target_field", self.target_field),
        ):
            _strict_text(value, f"modifier {name}")
        _strict_int(self.rule_slot, "modifier rule_slot", minimum=1)
        _strict_int(self.value, "modifier value")
        if self.visible_field is not None:
            if self.visible_field not in VISIBLE_FIELDS:
                raise ValueError("modifier visible_field is unsupported")
        _strict_int(self.activation_count, "modifier activation_count", minimum=1)
        _strict_int(
            self.activation_rate_permil,
            "modifier activation_rate_permil",
            minimum=0,
        )
        if self.trigger_phase_type != PHASE_PRODUCE_START:
            raise ValueError("modifier trigger phase must be ProduceStart")
        if self.activation_count != 1 or self.activation_rate_permil != 0:
            raise ValueError("modifier must be fixed and fire exactly once")

    @property
    def field(self) -> str:
        return self.target_field

    @property
    def is_visible_resource(self) -> bool:
        return self.visible_field is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_index": self.source_index,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "skill_id": self.skill_id,
            "rule_slot": self.rule_slot,
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "trigger_id": self.trigger_id,
            "trigger_phase_type": self.trigger_phase_type,
            "target_field": self.target_field,
            "value": self.value,
            "visible_field": self.visible_field,
            "activation_count": self.activation_count,
            "activation_rate_permil": self.activation_rate_permil,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProduceStartModifier":
        if not isinstance(payload, Mapping):
            raise ValueError("produce-start modifier must be an object")
        _require_exact_fields(
            payload,
            frozenset({
                "source_index", "source_kind", "source_id", "skill_id",
                "rule_slot", "effect_id", "effect_type", "trigger_id",
                "trigger_phase_type", "target_field", "value", "visible_field",
                "activation_count", "activation_rate_permil",
            }),
            "produce-start modifier",
        )
        return cls(
            source_index=_strict_int(
                payload.get("source_index"), "modifier source_index", minimum=0
            ),
            source_kind=_strict_text(payload.get("source_kind"), "modifier source_kind"),
            source_id=_strict_text(payload.get("source_id"), "modifier source_id"),
            skill_id=_strict_text(payload.get("skill_id"), "modifier skill_id"),
            rule_slot=_strict_int(payload.get("rule_slot"), "modifier rule_slot", minimum=1),
            effect_id=_strict_text(payload.get("effect_id"), "modifier effect_id"),
            effect_type=_strict_text(payload.get("effect_type"), "modifier effect_type"),
            trigger_id=_strict_text(payload.get("trigger_id"), "modifier trigger_id"),
            trigger_phase_type=_strict_text(
                payload.get("trigger_phase_type"), "modifier trigger_phase_type"
            ),
            target_field=_strict_text(payload.get("target_field"), "modifier target_field"),
            value=_strict_int(payload.get("value"), "modifier value"),
            visible_field=payload.get("visible_field"),
            activation_count=_strict_int(
                payload.get("activation_count"), "modifier activation_count", minimum=1
            ),
            activation_rate_permil=_strict_int(
                payload.get("activation_rate_permil"),
                "modifier activation_rate_permil",
                minimum=0,
            ),
        )


# Descriptive alias for callers that want to emphasize that these records
# came from catalog resolution rather than caller-supplied deltas.
ResolvedProduceStartModifier = ProduceStartModifier


@dataclass(frozen=True, slots=True)
class InitialModifierReconciliation:
    """A verified, immutable reconciliation result with complete audit data."""

    evidence: InitialModifierEvidence
    pre_values: VisibleProduceValues
    post_values: VisibleProduceValues
    visible_deltas: VisibleModifierDeltas
    produce_start_modifiers: tuple[ProduceStartModifier, ...]
    configuration_modifiers: tuple[ProduceStartModifier, ...]
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported initial modifier reconciliation schema")
        if not isinstance(self.evidence, InitialModifierEvidence):
            raise TypeError("reconciliation evidence must be InitialModifierEvidence")
        if not isinstance(self.pre_values, VisibleProduceValues):
            raise TypeError("reconciliation pre_values must be VisibleProduceValues")
        if not isinstance(self.post_values, VisibleProduceValues):
            raise TypeError("reconciliation post_values must be VisibleProduceValues")
        if not isinstance(self.visible_deltas, VisibleModifierDeltas):
            raise TypeError("reconciliation visible_deltas must be VisibleModifierDeltas")
        if not isinstance(self.produce_start_modifiers, tuple) or not all(
            isinstance(value, ProduceStartModifier)
            for value in self.produce_start_modifiers
        ):
            raise TypeError("produce_start_modifiers must be a tuple of ProduceStartModifier")
        if not isinstance(self.configuration_modifiers, tuple) or not all(
            isinstance(value, ProduceStartModifier)
            for value in self.configuration_modifiers
        ):
            raise TypeError("configuration_modifiers must be a tuple of ProduceStartModifier")

        expected_configuration = tuple(
            value
            for value in self.produce_start_modifiers
            if value.visible_field is None
        )
        if self.configuration_modifiers != expected_configuration:
            raise ValueError("configuration modifiers are not the non-resource start subset")

        totals = {name: 0 for name in VISIBLE_FIELDS}
        for modifier in self.produce_start_modifiers:
            if modifier.visible_field is not None:
                totals[modifier.visible_field] += modifier.value
        actual_deltas = VisibleModifierDeltas(**totals)
        if actual_deltas != self.visible_deltas:
            raise ValueError("visible deltas do not match resolved ProduceStart modifiers")
        expected_post = {
            name: getattr(self.pre_values, name) + getattr(self.visible_deltas, name)
            for name in VISIBLE_FIELDS
        }
        if expected_post != self.post_values.to_dict():
            raise ValueError("post values do not equal pre values plus visible deltas")

    @property
    def run_id(self) -> str:
        return self.evidence.run_id

    @property
    def produce_id(self) -> str:
        return self.evidence.produce_id

    @property
    def snapshot_digest(self) -> str:
        return self.evidence.snapshot_digest

    @property
    def capture_path(self) -> str:
        return str(self.evidence.capture_path)

    @property
    def capture_sha256(self) -> str:
        return self.evidence.capture_sha256

    @property
    def pre(self) -> VisibleProduceValues:
        return self.pre_values

    @property
    def post(self) -> VisibleProduceValues:
        return self.post_values

    @property
    def observed_post_values(self) -> VisibleProduceValues:
        return self.post_values

    @property
    def modifiers(self) -> VisibleModifierDeltas:
        return self.visible_deltas

    @property
    def configuration_totals(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for modifier in self.configuration_modifiers:
            totals[modifier.target_field] = (
                totals.get(modifier.target_field, 0) + modifier.value
            )
        return totals

    def verify_integrity(self) -> None:
        """Recheck the capture binding and internal arithmetic invariants."""

        self.evidence.verify_capture()
        # ``__post_init__`` has already checked the immutable arithmetic and
        # subset invariants.  Rechecking through a fresh construction keeps
        # this method useful after a JSON round-trip without mutating state.
        type(self)(
            evidence=self.evidence,
            pre_values=self.pre_values,
            post_values=self.post_values,
            visible_deltas=self.visible_deltas,
            produce_start_modifiers=self.produce_start_modifiers,
            configuration_modifiers=self.configuration_modifiers,
            schema_version=self.schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence": self.evidence.to_dict(),
            "pre_values": self.pre_values.to_dict(),
            "post_values": self.post_values.to_dict(),
            "visible_deltas": self.visible_deltas.to_dict(),
            "produce_start_modifiers": [
                value.to_dict() for value in self.produce_start_modifiers
            ],
            "configuration_modifiers": [
                value.to_dict() for value in self.configuration_modifiers
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "InitialModifierReconciliation":
        if not isinstance(payload, Mapping):
            raise ValueError("initial modifier reconciliation must be an object")
        _require_exact_fields(
            payload,
            frozenset({
                "schema_version", "evidence", "pre_values", "post_values",
                "visible_deltas", "produce_start_modifiers",
                "configuration_modifiers",
            }),
            "initial modifier reconciliation",
        )
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported initial modifier reconciliation schema")
        raw_evidence = payload.get("evidence")
        raw_pre = payload.get("pre_values")
        raw_post = payload.get("post_values")
        raw_deltas = payload.get("visible_deltas")
        raw_modifiers = payload.get("produce_start_modifiers")
        raw_configuration = payload.get("configuration_modifiers")
        if not isinstance(raw_evidence, Mapping):
            raise ValueError("reconciliation evidence must be an object")
        if not isinstance(raw_pre, Mapping) or not isinstance(raw_post, Mapping):
            raise ValueError("reconciliation pre/post values must be objects")
        if not isinstance(raw_deltas, Mapping):
            raise ValueError("reconciliation visible_deltas must be an object")
        if not isinstance(raw_modifiers, list) or not all(
            isinstance(value, Mapping) for value in raw_modifiers
        ):
            raise ValueError("reconciliation produce_start_modifiers must be an array")
        if not isinstance(raw_configuration, list) or not all(
            isinstance(value, Mapping) for value in raw_configuration
        ):
            raise ValueError("reconciliation configuration_modifiers must be an array")
        result = cls(
            evidence=InitialModifierEvidence.from_dict(raw_evidence),
            pre_values=VisibleProduceValues.from_dict(raw_pre),
            post_values=VisibleProduceValues.from_dict(raw_post),
            visible_deltas=VisibleModifierDeltas.from_dict(raw_deltas),
            produce_start_modifiers=tuple(
                ProduceStartModifier.from_dict(value) for value in raw_modifiers
            ),
            configuration_modifiers=tuple(
                ProduceStartModifier.from_dict(value) for value in raw_configuration
            ),
            schema_version=SCHEMA_VERSION,
        )
        result.verify_integrity()
        return result

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            allow_nan=False,
        )

    @classmethod
    def from_json(cls, payload: str | bytes) -> "InitialModifierReconciliation":
        if not isinstance(payload, (str, bytes)):
            raise TypeError("reconciliation JSON must be text or bytes")
        decoded = json.loads(payload)
        if not isinstance(decoded, Mapping):
            raise ValueError("reconciliation JSON must contain an object")
        return cls.from_dict(decoded)

    def replay(
        self,
        snapshot: LoadoutSnapshot,
        catalog: MasterPassiveCatalog,
        *,
        evidence: InitialModifierEvidence | None = None,
        pre_values: VisibleProduceValues | Mapping[str, Any] | None = None,
        post_values: VisibleProduceValues | Mapping[str, Any] | None = None,
    ) -> "InitialModifierReconciliation":
        """Re-verify this record and return the same record on exact replay."""

        return replay_initial_modifier_reconciliation(
            self,
            snapshot,
            catalog,
            evidence=evidence,
            pre_values=pre_values,
            post_values=post_values,
        )


# Short alias retained for callers that prefer a result-oriented name.
InitialModifierResult = InitialModifierReconciliation


def save_initial_modifier_reconciliation(
    result: InitialModifierReconciliation, path: Path
) -> None:
    """Atomically save one strict, evidence-bound reconciliation record."""

    if not isinstance(result, InitialModifierReconciliation):
        raise TypeError("result must be InitialModifierReconciliation")
    result.verify_integrity()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(result.to_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_initial_modifier_reconciliation(
    path: Path,
) -> InitialModifierReconciliation | None:
    path = Path(path)
    if not path.is_file():
        return None
    return InitialModifierReconciliation.from_json(path.read_bytes())


def _coerce_visible_values(
    value: VisibleProduceValues | Mapping[str, Any], label: str
) -> VisibleProduceValues:
    if isinstance(value, VisibleProduceValues):
        return value
    if isinstance(value, Mapping):
        try:
            return VisibleProduceValues.from_dict(value)
        except (TypeError, ValueError) as error:
            raise InitialModifierReconciliationError(
                (f"invalid-{label}-values:{error}",)
            ) from error
    raise InitialModifierReconciliationError(
        (f"invalid-{label}-values:type:{type(value).__name__}",)
    )


def _is_produce_start_candidate(rule: PassiveRule) -> bool:
    trigger_id = rule.trigger_id
    return (
        rule.trigger_phase_type == PHASE_PRODUCE_START
        or (
            isinstance(trigger_id, str)
            and (
                trigger_id in _PRODUCE_START_TRIGGER_IDS
                or "produce_start" in trigger_id
            )
        )
    )


def _resolve_complete_snapshot(
    snapshot: LoadoutSnapshot, catalog: MasterPassiveCatalog
) -> tuple[tuple[PassiveSource, ...], tuple[str, ...]]:
    """Resolve every observed selection without applying broad runtime gates."""

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
                    "catalog-memory-resolution-failed:"
                    f"memory:{memory.slot}:ability:{index}:{selection.id}:"
                    f"{type(error).__name__}"
                )
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
                "catalog-support-resolution-failed:"
                f"support:{slot.slot}:{selection.id}:{type(error).__name__}"
            )
            continue
        sources.extend(resolved)

    for index, source in enumerate(sources):
        if not isinstance(source, PassiveSource):
            reasons.append(f"catalog-source-malformed:{index}")
            continue
        # A source-level diagnostic can describe the activation count for a
        # skill whose rules are all future/event/status rules.  That is not a
        # ProduceStart scalar occurrence and must stay outside this targeted
        # gate.  A source with no rules at all cannot be resolved, while a
        # source containing a ProduceStart candidate must carry every source
        # diagnostic forward.
        has_produce_start_candidate = any(
            isinstance(rule, PassiveRule) and _is_produce_start_candidate(rule)
            for rule in source.rules
        )
        if not source.rules or has_produce_start_candidate:
            for reason in source.unsupported_rules:
                reasons.append(
                    "catalog-source-unsupported:"
                    f"{index}:{source.source_kind}:{source.source_id}:{reason}"
                )

    return tuple(sources), _deduplicate(reasons)


def _resolve_produce_start_modifier(
    source_index: int,
    source: PassiveSource,
    rule: PassiveRule,
) -> tuple[ProduceStartModifier | None, tuple[str, ...]]:
    """Validate one candidate rule and normalize it, without guessing."""

    reasons: list[str] = []
    prefix = (
        f"produce-start:{source_index}:{source.source_kind}:{source.source_id}:"
        f"rule-{rule.slot}"
    )

    if rule.trigger_phase_type != PHASE_PRODUCE_START:
        reasons.append(f"{prefix}:invalid-trigger-phase:{rule.trigger_phase_type!r}")
    if not isinstance(rule.effect_type, str) or not rule.effect_type:
        reasons.append(f"{prefix}:malformed-effect-type")
    if not isinstance(rule.effect_id, str) or not rule.effect_id:
        reasons.append(f"{prefix}:malformed-effect-id")
    if not isinstance(rule.trigger_id, str) or not rule.trigger_id:
        reasons.append(f"{prefix}:malformed-trigger-id")

    try:
        target = _PRODUCE_START_TARGETS.get((rule.trigger_id, rule.effect_type))
    except TypeError:
        target = None
        reasons.append(f"{prefix}:malformed-effect-or-trigger-key")
    if target is None:
        reasons.append(
            f"{prefix}:unsupported-scalar:{rule.trigger_id}:{rule.effect_type or 'unknown'}"
        )
    if not _is_int(source.activation_count) or source.activation_count != 1:
        reasons.append(
            f"{prefix}:invalid-activation-count:{source.activation_count!r}:expected-1"
        )
    if not _is_int(rule.activation_rate_permil) or rule.activation_rate_permil != 0:
        reasons.append(
            f"{prefix}:probabilistic-or-malformed-activation:{rule.activation_rate_permil!r}"
        )
    if (
        not _is_int(rule.effect_value_min)
        or not _is_int(rule.effect_value_max)
        or rule.effect_value_min != rule.effect_value_max
    ):
        reasons.append(
            f"{prefix}:ranged-or-malformed-value:"
            f"{rule.effect_value_min!r}..{rule.effect_value_max!r}"
        )
    if not isinstance(rule.unsupported_rules, tuple) or any(
        not isinstance(reason, str) for reason in rule.unsupported_rules
    ):
        reasons.append(f"{prefix}:malformed-catalog-diagnostics")
    else:
        for reason in rule.unsupported_rules:
            reasons.append(f"{prefix}:catalog-unsupported:{reason}")

    if reasons or target is None:
        return None, _deduplicate(reasons)
    target_field, visible_field = target
    try:
        modifier = ProduceStartModifier(
            source_index=source_index,
            source_kind=source.source_kind,
            source_id=source.source_id,
            skill_id=source.skill_id,
            rule_slot=rule.slot,
            effect_id=rule.effect_id,
            effect_type=rule.effect_type,
            trigger_id=rule.trigger_id,
            trigger_phase_type=rule.trigger_phase_type,
            target_field=target_field,
            value=int(rule.effect_value_min),
            visible_field=visible_field,
            activation_count=1,
            activation_rate_permil=0,
        )
    except (TypeError, ValueError) as error:
        return None, (f"{prefix}:malformed-record:{error}",)
    return modifier, ()


def _identity_and_proof_reasons(
    snapshot: LoadoutSnapshot, evidence: InitialModifierEvidence
) -> tuple[str, ...]:
    reasons: list[str] = []
    expected_digest = loadout_snapshot_digest(snapshot)
    if evidence.run_id != snapshot.run_id:
        reasons.append("evidence-run-id-mismatch")
    if snapshot.produce_id is None:
        reasons.append("snapshot-produce-id-missing")
    elif evidence.produce_id != snapshot.produce_id:
        reasons.append("evidence-produce-id-mismatch")
    if evidence.snapshot_digest != expected_digest:
        reasons.append(
            f"evidence-snapshot-digest-mismatch:{evidence.snapshot_digest}!={expected_digest}"
        )
    if not evidence.overview_proof.stable:
        reasons.append("overview-proof-not-stable")
    if not evidence.overview_proof.full:
        reasons.append("overview-proof-not-full")
    if evidence.overview_proof.fields != VISIBLE_FIELDS:
        reasons.append("overview-proof-fields-mismatch")
    return _deduplicate(reasons)


def reconcile_initial_modifiers(
    snapshot: LoadoutSnapshot,
    catalog: MasterPassiveCatalog,
    evidence: InitialModifierEvidence,
    pre_values: VisibleProduceValues | Mapping[str, Any],
    post_values: VisibleProduceValues | Mapping[str, Any],
    *,
    prior: InitialModifierReconciliation | None = None,
) -> InitialModifierReconciliation:
    """Verify deterministic ProduceStart values and return metadata only.

    ``prior`` is optional replay evidence.  When supplied, all newly resolved
    identity, arithmetic, capture, and catalog metadata must equal it; an
    exact replay returns the prior immutable record rather than creating a
    second transaction.
    """

    if not isinstance(snapshot, LoadoutSnapshot):
        raise TypeError("snapshot must be LoadoutSnapshot")
    if not isinstance(catalog, MasterPassiveCatalog):
        raise TypeError("catalog must be MasterPassiveCatalog")
    if not isinstance(evidence, InitialModifierEvidence):
        raise TypeError("evidence must be InitialModifierEvidence")
    if prior is not None and not isinstance(prior, InitialModifierReconciliation):
        raise TypeError("prior must be InitialModifierReconciliation or None")

    pre = _coerce_visible_values(pre_values, "pre")
    post = _coerce_visible_values(post_values, "post")
    reasons = list(_identity_and_proof_reasons(snapshot, evidence))
    try:
        evidence.verify_capture()
    except InitialModifierReconciliationError as error:
        reasons.extend(error.reasons)

    sources, resolution_reasons = _resolve_complete_snapshot(snapshot, catalog)
    reasons.extend(resolution_reasons)

    resolved: list[ProduceStartModifier] = []
    for source_index, source in enumerate(sources):
        if not isinstance(source, PassiveSource):
            continue
        for rule in source.rules:
            if not isinstance(rule, PassiveRule):
                reasons.append(f"catalog-rule-malformed:{source_index}")
                continue
            if not _is_produce_start_candidate(rule):
                # Future/event/status rules are deliberately outside this
                # reconciliation's scope, even if the broad runtime bridge
                # would reject them for its own stricter purpose.
                continue
            modifier, rule_reasons = _resolve_produce_start_modifier(
                source_index, source, rule
            )
            reasons.extend(rule_reasons)
            if modifier is not None:
                resolved.append(modifier)

    if reasons:
        raise InitialModifierReconciliationError(reasons)

    totals = {name: 0 for name in VISIBLE_FIELDS}
    for modifier in resolved:
        if modifier.visible_field is not None:
            totals[modifier.visible_field] += modifier.value
    deltas = VisibleModifierDeltas(**totals)
    expected_post = {
        name: getattr(pre, name) + getattr(deltas, name) for name in VISIBLE_FIELDS
    }
    value_reasons = [
        f"visible-post-value-mismatch:{name}:{getattr(post, name)}!={expected_post[name]}"
        for name in VISIBLE_FIELDS
        if getattr(post, name) != expected_post[name]
    ]
    if value_reasons:
        raise InitialModifierReconciliationError(value_reasons)

    all_modifiers = tuple(resolved)
    candidate = InitialModifierReconciliation(
        evidence=evidence,
        pre_values=pre,
        post_values=post,
        visible_deltas=deltas,
        produce_start_modifiers=all_modifiers,
        configuration_modifiers=tuple(
            modifier for modifier in all_modifiers if modifier.visible_field is None
        ),
    )

    if prior is not None:
        try:
            prior.verify_integrity()
        except InitialModifierReconciliationError as error:
            raise InitialModifierReconciliationError(
                ("prior-record-integrity-failed", *error.reasons)
            ) from error
        if candidate != prior:
            raise InitialModifierReconciliationError(
                ("replay-verified-record-mismatch",)
            )
        return prior
    return candidate


def replay_initial_modifier_reconciliation(
    verified: InitialModifierReconciliation,
    snapshot: LoadoutSnapshot,
    catalog: MasterPassiveCatalog,
    *,
    evidence: InitialModifierEvidence | None = None,
    pre_values: VisibleProduceValues | Mapping[str, Any] | None = None,
    post_values: VisibleProduceValues | Mapping[str, Any] | None = None,
) -> InitialModifierReconciliation:
    """Replay a verified record without applying anything to run state."""

    if not isinstance(verified, InitialModifierReconciliation):
        raise TypeError("verified must be InitialModifierReconciliation")
    return reconcile_initial_modifiers(
        snapshot,
        catalog,
        evidence or verified.evidence,
        pre_values if pre_values is not None else verified.pre_values,
        post_values if post_values is not None else verified.post_values,
        prior=verified,
    )


def verify_initial_modifier_reconciliation(
    verified: InitialModifierReconciliation,
    snapshot: LoadoutSnapshot,
    catalog: MasterPassiveCatalog,
    *,
    evidence: InitialModifierEvidence | None = None,
    pre_values: VisibleProduceValues | Mapping[str, Any] | None = None,
    post_values: VisibleProduceValues | Mapping[str, Any] | None = None,
) -> InitialModifierReconciliation:
    """Explicit alias for exact replay/verification of a stored record."""

    return replay_initial_modifier_reconciliation(
        verified,
        snapshot,
        catalog,
        evidence=evidence,
        pre_values=pre_values,
        post_values=post_values,
    )


# Alternate function spelling for callers following the module name.
reconcile_initial_modifier_reconciliation = reconcile_initial_modifiers


__all__ = [
    "InitialModifierEvidence",
    "InitialModifierReconciliation",
    "InitialModifierReconciliationError",
    "InitialModifierResult",
    "OverviewProof",
    "ProduceStartModifier",
    "ResolvedProduceStartModifier",
    "VisibleModifierDeltas",
    "VisibleProduceValues",
    "SCHEMA_VERSION",
    "VISIBLE_FIELDS",
    "reconcile_initial_modifiers",
    "reconcile_initial_modifier_reconciliation",
    "replay_initial_modifier_reconciliation",
    "verify_initial_modifier_reconciliation",
]
