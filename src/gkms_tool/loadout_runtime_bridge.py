"""Safety-gated bridge from an observed loadout to exam runtime state.

The three participating modules deliberately have different responsibilities:

* :mod:`loadout_snapshot` proves that the *whole* selected loadout was observed;
* :mod:`passive_runtime` resolves deterministic run modifiers and deferred
  start-step status-enchant contracts; and
* :class:`logic_engine.LogicExamState` executes installed Master enchants.

This module is the transaction boundary between them.  It never returns a
partially applicable result: an observation, catalog, runtime, session, or
Master-enchant problem raises :class:`LoadoutRuntimeBridgeError` before the
caller receives a changed run mapping or status-enchant tuple.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .audition_rules import FINAL, MID1, MID2
from .exam_status_runtime import load_runtime_status_enchant
from .loadout_snapshot import (
    DEFAULT_AUTHORITATIVE_CONFIDENCE,
    LoadoutSnapshot,
)
from .logic_engine import ActiveRuntimeStatusEnchant
from .passive_catalog import MasterPassiveCatalog, PassiveSource
from .passive_chance import (
    PassiveChanceContract,
    PassiveChanceExpansion,
    PassiveChanceRuntimeResult,
    expand_passive_chance,
    expand_single_passive_chance_event,
)
from .passive_runtime import (
    PHASE_START_AUDITION,
    PHASE_START_AUDITION_FINAL,
    PHASE_START_AUDITION_MID1,
    PHASE_START_AUDITION_MID2,
    PHASE_START_LESSON,
    PassiveRuntimeResult,
    RunModifiers,
    StatusEnchantInstallContract,
    apply_run_modifiers,
    passive_source_usage_key,
    resolve_passive_runtime,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOADOUT_RUNTIME_SESSION_PATH = (
    PROJECT_ROOT / "var" / "loadout_runtime_session.json"
)
LOADOUT_RUNTIME_SESSION_SCHEMA_VERSION = 1

STEP_KIND_LESSON = "lesson"
STEP_KIND_AUDITION = "audition"
LESSON_VOCAL = "vocal"
LESSON_DANCE = "dance"
LESSON_VISUAL = "visual"
LESSON_ATTRIBUTES = frozenset({LESSON_VOCAL, LESSON_DANCE, LESSON_VISUAL})
AUDITION_STEP_TYPES = frozenset({MID1, MID2, FINAL})

_GENERIC_AUDITION_TRIGGER = "p_trigger-start_audition-for_hif_memory"
_AUDITION_TRIGGER_BY_STEP = {
    MID1: "p_trigger-start_audition_mid1",
    MID2: "p_trigger-start_audition_mid2",
    FINAL: "p_trigger-start_audition_final",
}
_AUDITION_PHASE_BY_STEP = {
    MID1: PHASE_START_AUDITION_MID1,
    MID2: PHASE_START_AUDITION_MID2,
    FINAL: PHASE_START_AUDITION_FINAL,
}

# The mapping is intentionally explicit here.  A bridge caller must provide
# the value *before* passives for every non-zero target; unlike the low-level
# helper, this layer must not turn a missing field into an implicit zero.
_RUN_TARGET_BY_MODIFIER = {
    "vocal_addition": "vocal",
    "dance_addition": "dance",
    "visual_addition": "visual",
    "produce_point_addition": "produce_points",
    "max_stamina_addition": "max_stamina",
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _strict_optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _strict_text(value, label)


def _strict_int(value: object, label: str, *, minimum: int = 0) -> int:
    if not _is_int(value) or int(value) < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return int(value)


def _deduplicate(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


class LoadoutRuntimeBridgeError(RuntimeError):
    """Aggregate hard blocker for one all-or-nothing bridge operation."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = _deduplicate(tuple(str(reason) for reason in reasons))
        if not self.reasons:
            raise ValueError("bridge error requires at least one reason")
        super().__init__("loadout runtime blocked: " + "; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class PassiveStepContext:
    """Exact start-step event; ``context_id`` must be unique within a run."""

    context_id: str
    kind: str
    lesson_attribute: str | None = None
    lesson_sp: bool = False
    audition_step_type: str | None = None

    def __post_init__(self) -> None:
        _strict_text(self.context_id, "context_id")
        if not isinstance(self.lesson_sp, bool):
            raise ValueError("lesson_sp must be a boolean")
        if self.kind == STEP_KIND_LESSON:
            if self.lesson_attribute not in LESSON_ATTRIBUTES:
                raise ValueError("lesson context requires vocal, dance, or visual")
            if self.audition_step_type is not None:
                raise ValueError("lesson context cannot have an audition step type")
        elif self.kind == STEP_KIND_AUDITION:
            if self.audition_step_type not in AUDITION_STEP_TYPES:
                raise ValueError("audition context requires Mid1, Mid2, or Final")
            if self.lesson_attribute is not None or self.lesson_sp:
                raise ValueError("audition context cannot have lesson fields")
        else:
            raise ValueError(f"unsupported step kind: {self.kind!r}")

    @classmethod
    def lesson(
        cls,
        context_id: str,
        attribute: str,
        *,
        sp: bool = False,
    ) -> "PassiveStepContext":
        return cls(context_id, STEP_KIND_LESSON, attribute, sp, None)

    @classmethod
    def audition(
        cls, context_id: str, step_type: str
    ) -> "PassiveStepContext":
        return cls(context_id, STEP_KIND_AUDITION, None, False, step_type)

    def matches(self, contract: StatusEnchantInstallContract) -> bool:
        if not isinstance(contract, StatusEnchantInstallContract):
            raise TypeError("contract must be StatusEnchantInstallContract")
        if self.kind == STEP_KIND_LESSON:
            suffix = f"{self.lesson_attribute}{'_sp' if self.lesson_sp else ''}"
            return (
                contract.produce_trigger_phase_type == PHASE_START_LESSON
                and contract.produce_trigger_id
                == f"p_trigger-start_lesson-lesson_{suffix}"
            )
        step_type = str(self.audition_step_type)
        return (
            contract.produce_trigger_phase_type == PHASE_START_AUDITION
            and contract.produce_trigger_id == _GENERIC_AUDITION_TRIGGER
        ) or (
            contract.produce_trigger_phase_type
            == _AUDITION_PHASE_BY_STEP[step_type]
            and contract.produce_trigger_id
            == _AUDITION_TRIGGER_BY_STEP[step_type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "context_id": self.context_id,
            "kind": self.kind,
            "lesson_attribute": self.lesson_attribute,
            "lesson_sp": self.lesson_sp,
            "audition_step_type": self.audition_step_type,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PassiveStepContext":
        return cls(
            context_id=_strict_text(payload.get("context_id"), "context_id"),
            kind=_strict_text(payload.get("kind"), "kind"),
            lesson_attribute=_strict_optional_text(
                payload.get("lesson_attribute"), "lesson_attribute"
            ),
            lesson_sp=payload.get("lesson_sp"),
            audition_step_type=_strict_optional_text(
                payload.get("audition_step_type"), "audition_step_type"
            ),
        )


@dataclass(frozen=True, slots=True)
class InstalledPassiveStatus:
    instance_id: str
    enchant_id: str
    usage_key: str
    source_index: int
    skill_id: str
    rule_slot: int
    produce_trigger_id: str

    def __post_init__(self) -> None:
        for label, value in (
            ("instance_id", self.instance_id),
            ("enchant_id", self.enchant_id),
            ("usage_key", self.usage_key),
            ("skill_id", self.skill_id),
            ("produce_trigger_id", self.produce_trigger_id),
        ):
            _strict_text(value, label)
        _strict_int(self.source_index, "source_index")
        _strict_int(self.rule_slot, "rule_slot", minimum=1)

    def to_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "enchant_id": self.enchant_id,
            "usage_key": self.usage_key,
            "source_index": self.source_index,
            "skill_id": self.skill_id,
            "rule_slot": self.rule_slot,
            "produce_trigger_id": self.produce_trigger_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InstalledPassiveStatus":
        return cls(
            instance_id=_strict_text(payload.get("instance_id"), "instance_id"),
            enchant_id=_strict_text(payload.get("enchant_id"), "enchant_id"),
            usage_key=_strict_text(payload.get("usage_key"), "usage_key"),
            source_index=_strict_int(payload.get("source_index"), "source_index"),
            skill_id=_strict_text(payload.get("skill_id"), "skill_id"),
            rule_slot=_strict_int(
                payload.get("rule_slot"), "rule_slot", minimum=1
            ),
            produce_trigger_id=_strict_text(
                payload.get("produce_trigger_id"), "produce_trigger_id"
            ),
        )


@dataclass(frozen=True, slots=True)
class PassiveStepRecord:
    context: PassiveStepContext
    activated_usage_keys: tuple[str, ...]
    installs: tuple[InstalledPassiveStatus, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.context, PassiveStepContext):
            raise TypeError("context must be PassiveStepContext")
        if len(self.activated_usage_keys) != len(set(self.activated_usage_keys)):
            raise ValueError("activated usage keys must be unique")
        for key in self.activated_usage_keys:
            _strict_text(key, "activated usage key")
        if not all(isinstance(item, InstalledPassiveStatus) for item in self.installs):
            raise TypeError("installs must contain InstalledPassiveStatus")
        ids = [item.instance_id for item in self.installs]
        if len(ids) != len(set(ids)):
            raise ValueError("installed passive instance ids must be unique")

    def to_dict(self) -> dict[str, object]:
        return {
            "context": self.context.to_dict(),
            "activated_usage_keys": list(self.activated_usage_keys),
            "installs": [item.to_dict() for item in self.installs],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "PassiveStepRecord":
        raw_context = payload.get("context")
        raw_keys = payload.get("activated_usage_keys")
        raw_installs = payload.get("installs")
        if not isinstance(raw_context, Mapping):
            raise ValueError("step record context must be an object")
        if not isinstance(raw_keys, list) or not all(
            isinstance(value, str) and value for value in raw_keys
        ):
            raise ValueError("activated_usage_keys must be a text array")
        if not isinstance(raw_installs, list) or not all(
            isinstance(value, Mapping) for value in raw_installs
        ):
            raise ValueError("step installs must be an object array")
        return cls(
            context=PassiveStepContext.from_dict(raw_context),
            activated_usage_keys=tuple(raw_keys),
            installs=tuple(
                InstalledPassiveStatus.from_dict(value) for value in raw_installs
            ),
        )


@dataclass(frozen=True, slots=True)
class LoadoutRuntimeSession:
    run_id: str
    snapshot_captured_at: str
    snapshot_digest: str
    produce_id: str | None = None
    initial_modifiers_applied: bool = False
    initial_base_values: tuple[tuple[str, int], ...] = ()
    initial_result_values: tuple[tuple[str, int], ...] = ()
    usage_counts: tuple[tuple[str, int], ...] = ()
    step_records: tuple[PassiveStepRecord, ...] = ()
    schema_version: int = LOADOUT_RUNTIME_SESSION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _strict_text(self.run_id, "run_id")
        _strict_text(self.snapshot_captured_at, "snapshot_captured_at")
        digest = _strict_text(self.snapshot_digest, "snapshot_digest")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise ValueError("snapshot_digest must be a lowercase SHA-256 digest")
        _strict_optional_text(self.produce_id, "produce_id")
        if not isinstance(self.initial_modifiers_applied, bool):
            raise ValueError("initial_modifiers_applied must be a boolean")
        if self.schema_version != LOADOUT_RUNTIME_SESSION_SCHEMA_VERSION:
            raise ValueError("unsupported loadout runtime session schema version")
        for label, pairs in (
            ("initial_base_values", self.initial_base_values),
            ("initial_result_values", self.initial_result_values),
            ("usage_counts", self.usage_counts),
        ):
            keys: list[str] = []
            for key, value in pairs:
                keys.append(_strict_text(key, f"{label} key"))
                _strict_int(value, f"{label}:{key}")
            if len(keys) != len(set(keys)):
                raise ValueError(f"{label} keys must be unique")
            if tuple(keys) != tuple(sorted(keys)):
                raise ValueError(f"{label} must be sorted by key")
        if not self.initial_modifiers_applied and (
            self.initial_base_values or self.initial_result_values
        ):
            raise ValueError("unapplied initial modifiers cannot retain values")
        if not all(isinstance(item, PassiveStepRecord) for item in self.step_records):
            raise TypeError("step_records must contain PassiveStepRecord")
        context_ids = [item.context.context_id for item in self.step_records]
        if len(context_ids) != len(set(context_ids)):
            raise ValueError("step context ids must be unique")
        all_instance_ids = [
            install.instance_id
            for record in self.step_records
            for install in record.installs
        ]
        if len(all_instance_ids) != len(set(all_instance_ids)):
            raise ValueError("passive instance ids must be unique across the session")

    @property
    def usage_map(self) -> dict[str, int]:
        return dict(self.usage_counts)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "produce_id": self.produce_id,
            "snapshot_captured_at": self.snapshot_captured_at,
            "snapshot_digest": self.snapshot_digest,
            "initial_modifiers_applied": self.initial_modifiers_applied,
            "initial_base_values": [list(item) for item in self.initial_base_values],
            "initial_result_values": [
                list(item) for item in self.initial_result_values
            ],
            "usage_counts": [list(item) for item in self.usage_counts],
            "step_records": [item.to_dict() for item in self.step_records],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LoadoutRuntimeSession":
        if payload.get("schema_version") != LOADOUT_RUNTIME_SESSION_SCHEMA_VERSION:
            raise ValueError("unsupported loadout runtime session schema version")

        def pairs(name: str) -> tuple[tuple[str, int], ...]:
            raw = payload.get(name)
            if not isinstance(raw, list) or not all(
                isinstance(item, list) and len(item) == 2 for item in raw
            ):
                raise ValueError(f"{name} must be an array of pairs")
            normalized = tuple(
                (
                    _strict_text(item[0], f"{name} key"),
                    _strict_int(item[1], f"{name} value"),
                )
                for item in raw
            )
            return normalized

        raw_records = payload.get("step_records")
        if not isinstance(raw_records, list) or not all(
            isinstance(value, Mapping) for value in raw_records
        ):
            raise ValueError("step_records must be an object array")
        applied = payload.get("initial_modifiers_applied")
        if not isinstance(applied, bool):
            raise ValueError("initial_modifiers_applied must be a boolean")
        return cls(
            run_id=_strict_text(payload.get("run_id"), "run_id"),
            produce_id=_strict_optional_text(payload.get("produce_id"), "produce_id"),
            snapshot_captured_at=_strict_text(
                payload.get("snapshot_captured_at"), "snapshot_captured_at"
            ),
            snapshot_digest=_strict_text(
                payload.get("snapshot_digest"), "snapshot_digest"
            ),
            initial_modifiers_applied=applied,
            initial_base_values=pairs("initial_base_values"),
            initial_result_values=pairs("initial_result_values"),
            usage_counts=pairs("usage_counts"),
            step_records=tuple(
                PassiveStepRecord.from_dict(value) for value in raw_records
            ),
        )


@dataclass(frozen=True, slots=True)
class PreparedLoadoutRuntime:
    session: LoadoutRuntimeSession
    sources: tuple[PassiveSource, ...]
    runtime: PassiveRuntimeResult


def loadout_chance_contracts(
    prepared: PreparedLoadoutRuntime,
) -> tuple[PassiveChanceContract, ...]:
    """Return exact probabilistic gates from an accepted loadout."""

    if not isinstance(prepared, PreparedLoadoutRuntime):
        raise TypeError("prepared must be PreparedLoadoutRuntime")
    prepared.runtime.require_supported()
    return prepared.runtime.chance_contracts


def expand_loadout_passive_chance(
    prepared: PreparedLoadoutRuntime,
    contract_id: str,
) -> PassiveChanceExpansion:
    """Pure Beam/Expectimax expansion for one scheduler-ordered passive.

    The returned hit branch carries the next source use-count.  This function
    deliberately does not commit that branch to ``LoadoutRuntimeSession``;
    the caller must first choose/observe an outcome.
    """

    if not isinstance(prepared, PreparedLoadoutRuntime):
        raise TypeError("prepared must be PreparedLoadoutRuntime")
    _strict_text(contract_id, "contract_id")
    prepared.runtime.require_supported()
    matches = tuple(
        contract
        for contract in prepared.runtime.chance_contracts
        if contract.contract_id == contract_id
    )
    if len(matches) != 1:
        raise LoadoutRuntimeBridgeError(
            (f"unknown-passive-chance-contract:{contract_id}",)
        )
    return expand_passive_chance(
        matches[0], usage_counts=prepared.session.usage_map
    )


def expand_single_loadout_passive_chance_event(
    prepared: PreparedLoadoutRuntime,
    *,
    trigger_id: str,
    trigger_phase_type: str,
) -> PassiveChanceExpansion | None:
    """Convenience event expansion that rejects cross-source RNG ambiguity."""

    if not isinstance(prepared, PreparedLoadoutRuntime):
        raise TypeError("prepared must be PreparedLoadoutRuntime")
    _strict_text(trigger_id, "trigger_id")
    _strict_text(trigger_phase_type, "trigger_phase_type")
    prepared.runtime.require_supported()
    chance_runtime = PassiveChanceRuntimeResult(
        contracts=prepared.runtime.chance_contracts,
        blocking_rules=(),
    )
    return expand_single_passive_chance_event(
        chance_runtime,
        trigger_id=trigger_id,
        trigger_phase_type=trigger_phase_type,
        usage_counts=prepared.session.usage_map,
    )


@dataclass(frozen=True, slots=True)
class InitialRunModifierResult:
    run_state: dict[str, Any]
    session: LoadoutRuntimeSession
    modifiers: RunModifiers


@dataclass(frozen=True, slots=True)
class StepStatusInstallResult:
    runtime_status_enchants: tuple[ActiveRuntimeStatusEnchant, ...]
    installed: tuple[InstalledPassiveStatus, ...]
    activated_usage_keys: tuple[str, ...]
    session: LoadoutRuntimeSession
    replayed: bool


@dataclass(frozen=True, slots=True)
class LoadoutRuleIdentifiers:
    """Replay identifiers contributed by one authoritative loadout.

    ``memory_ids`` intentionally includes the observed memory instance/gift
    identifiers *and* the selected MemoryAbility identifiers.  The generated
    instance is not reconstructible from Master alone, while the ability ID is
    the stable key that resolves its ProduceSkill chain.
    """

    memory_ids: tuple[str, ...] = ()
    support_ids: tuple[str, ...] = ()
    effect_ids: tuple[str, ...] = ()
    trigger_ids: tuple[str, ...] = ()
    status_enchant_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "memory_ids": list(self.memory_ids),
            "support_ids": list(self.support_ids),
            "effect_ids": list(self.effect_ids),
            "trigger_ids": list(self.trigger_ids),
            "status_enchant_ids": list(self.status_enchant_ids),
        }


def loadout_snapshot_digest(snapshot: LoadoutSnapshot) -> str:
    if not isinstance(snapshot, LoadoutSnapshot):
        raise TypeError("snapshot must be LoadoutSnapshot")
    encoded = json.dumps(
        snapshot.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def loadout_rule_identifiers(
    snapshot: LoadoutSnapshot,
    prepared: PreparedLoadoutRuntime,
) -> LoadoutRuleIdentifiers:
    """Return deterministic replay IDs only after the whole loadout is safe."""

    if not isinstance(snapshot, LoadoutSnapshot):
        raise TypeError("snapshot must be LoadoutSnapshot")
    if not isinstance(prepared, PreparedLoadoutRuntime):
        raise TypeError("prepared must be PreparedLoadoutRuntime")
    if prepared.session.snapshot_digest != loadout_snapshot_digest(snapshot):
        raise LoadoutRuntimeBridgeError(("prepared-snapshot-digest-mismatch",))
    prepared.runtime.require_supported()

    memory_ids: list[str] = []
    for memory in snapshot.memories:
        if memory.memory_id:
            memory_ids.append(memory.memory_id)
        if memory.memory_gift_id:
            memory_ids.append(memory.memory_gift_id)
    memory_ids.extend(
        source.source_id
        for source in prepared.sources
        if source.source_kind == "memory_ability"
    )
    support_ids = [
        slot.card_id for slot in snapshot.support_cards if slot.card_id
    ]
    effect_ids = [
        rule.effect_id
        for source in prepared.sources
        for rule in source.rules
        if rule.effect_id
    ]
    trigger_ids = [
        rule.trigger_id
        for source in prepared.sources
        for rule in source.rules
        if rule.trigger_id
    ]
    status_enchant_ids = [
        contract.status_enchant_id
        for contract in prepared.runtime.status_enchant_installs
    ]
    status_enchant_ids.extend(
        contract.effect.status_enchant_id
        for contract in prepared.runtime.chance_contracts
        if contract.effect.status_enchant_id
    )
    return LoadoutRuleIdentifiers(
        memory_ids=tuple(dict.fromkeys(memory_ids)),
        support_ids=tuple(dict.fromkeys(support_ids)),
        effect_ids=tuple(dict.fromkeys(effect_ids)),
        trigger_ids=tuple(dict.fromkeys(trigger_ids)),
        status_enchant_ids=tuple(dict.fromkeys(status_enchant_ids)),
    )


def new_loadout_runtime_session(
    snapshot: LoadoutSnapshot,
) -> LoadoutRuntimeSession:
    return LoadoutRuntimeSession(
        run_id=snapshot.run_id,
        produce_id=snapshot.produce_id,
        snapshot_captured_at=snapshot.captured_at,
        snapshot_digest=loadout_snapshot_digest(snapshot),
    )


def _bridge_runtime_reasons(runtime: PassiveRuntimeResult) -> tuple[str, ...]:
    return tuple(
        "passive-runtime:"
        f"{block.source_index}:{block.source_kind}:{block.source_id}:"
        f"{block.code}:{block.reason}"
        for block in runtime.blocking_rules
    )


def _session_record_reasons(
    session: LoadoutRuntimeSession,
    contracts: Sequence[StatusEnchantInstallContract],
) -> tuple[str, ...]:
    """Prove persisted installs still derive from the authoritative runtime."""

    reasons: list[str] = []
    activation_tally: dict[str, int] = {}
    for record in session.step_records:
        matching = tuple(
            contract for contract in contracts if record.context.matches(contract)
        )
        by_key: dict[str, list[StatusEnchantInstallContract]] = {}
        for contract in matching:
            by_key.setdefault(contract.usage_key, []).append(contract)
        for key in record.activated_usage_keys:
            activation_tally[key] = activation_tally.get(key, 0) + 1
            expected_group = by_key.get(key, [])
            if not expected_group:
                reasons.append(
                    f"session-step-activation-not-authoritative:"
                    f"{record.context.context_id}:{key}"
                )
                continue
            actual_group = [
                install for install in record.installs if install.usage_key == key
            ]
            if len(actual_group) != len(expected_group):
                reasons.append(
                    f"session-step-install-count-mismatch:"
                    f"{record.context.context_id}:{key}"
                )
                continue
            expected_by_instance = {
                _instance_id(session, record.context, contract): contract
                for contract in expected_group
            }
            for install in actual_group:
                contract = expected_by_instance.get(install.instance_id)
                if contract is None or (
                    install.enchant_id != contract.status_enchant_id
                    or install.source_index != contract.source_index
                    or install.skill_id != contract.skill_id
                    or install.rule_slot != contract.rule_slot
                    or install.produce_trigger_id
                    != contract.produce_trigger_id
                ):
                    reasons.append(
                        f"session-step-install-not-authoritative:"
                        f"{record.context.context_id}:{install.instance_id}"
                    )
        activated = set(record.activated_usage_keys)
        for install in record.installs:
            if install.usage_key not in activated:
                reasons.append(
                    f"session-step-install-without-activation:"
                    f"{record.context.context_id}:{install.instance_id}"
                )
    all_usage_keys = set(activation_tally) | set(session.usage_map)
    for key in all_usage_keys:
        if activation_tally.get(key, 0) != session.usage_map.get(key, 0):
            reasons.append(
                f"session-usage-history-mismatch:{key}:"
                f"{session.usage_map.get(key, 0)}!="
                f"{activation_tally.get(key, 0)}"
            )
    return _deduplicate(tuple(reasons))


def prepare_loadout_runtime(
    snapshot: LoadoutSnapshot,
    catalog: MasterPassiveCatalog,
    *,
    session: LoadoutRuntimeSession | None = None,
    confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
) -> PreparedLoadoutRuntime:
    """Resolve the entire authoritative snapshot or reject the entire loadout."""

    if not isinstance(snapshot, LoadoutSnapshot):
        raise TypeError("snapshot must be LoadoutSnapshot")
    if not isinstance(catalog, MasterPassiveCatalog):
        raise TypeError("catalog must be MasterPassiveCatalog")
    resolution = snapshot.audit_resolution(
        catalog, confidence_threshold=confidence_threshold
    )
    if resolution.blocking_reasons:
        raise LoadoutRuntimeBridgeError(resolution.blocking_reasons)

    active_session = session or new_loadout_runtime_session(snapshot)
    expected_digest = loadout_snapshot_digest(snapshot)
    identity_reasons: list[str] = []
    if active_session.run_id != snapshot.run_id:
        identity_reasons.append("session-run-id-mismatch")
    if active_session.produce_id != snapshot.produce_id:
        identity_reasons.append("session-produce-id-mismatch")
    if active_session.snapshot_captured_at != snapshot.captured_at:
        identity_reasons.append("session-capture-mismatch")
    if active_session.snapshot_digest != expected_digest:
        identity_reasons.append("session-snapshot-digest-mismatch")

    valid_usage_limits: dict[str, int] = {}
    for index, source in enumerate(resolution.passive_sources):
        key = passive_source_usage_key(source, index)
        valid_usage_limits[key] = int(source.activation_count or 0)
    for key, count in active_session.usage_counts:
        if key not in valid_usage_limits:
            identity_reasons.append(f"session-unknown-usage-key:{key}")
            continue
        limit = valid_usage_limits[key]
        if limit > 0 and count > limit:
            identity_reasons.append(
                f"session-usage-exceeds-limit:{key}:{count}>{limit}"
            )
    if identity_reasons:
        raise LoadoutRuntimeBridgeError(identity_reasons)

    runtime = resolve_passive_runtime(
        resolution.passive_sources,
        usage_counts=active_session.usage_map,
    )
    if runtime.blocking_rules:
        raise LoadoutRuntimeBridgeError(_bridge_runtime_reasons(runtime))
    record_reasons = _session_record_reasons(
        active_session, runtime.status_enchant_installs
    )
    if record_reasons:
        raise LoadoutRuntimeBridgeError(record_reasons)
    return PreparedLoadoutRuntime(
        active_session, resolution.passive_sources, runtime
    )


def required_modifier_base_fields(modifiers: RunModifiers) -> tuple[str, ...]:
    if not isinstance(modifiers, RunModifiers):
        raise TypeError("modifiers must be RunModifiers")
    return tuple(
        sorted(
            _RUN_TARGET_BY_MODIFIER.get(name, name)
            for name, value in modifiers.as_dict().items()
            if value != 0
        )
    )


def apply_initial_loadout_modifiers(
    snapshot: LoadoutSnapshot,
    catalog: MasterPassiveCatalog,
    pre_passive: Mapping[str, Any],
    *,
    session: LoadoutRuntimeSession | None = None,
    confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
) -> InitialRunModifierResult:
    """Apply deterministic produce-start values to an explicit baseline.

    Repeating the operation with the same *pre-passive* values is idempotent.
    Passing a post-passive or otherwise changed baseline after the transaction
    was recorded is rejected instead of applying the deltas twice.
    """

    if not isinstance(pre_passive, Mapping):
        raise TypeError("pre_passive must be a mapping")
    prepared = prepare_loadout_runtime(
        snapshot,
        catalog,
        session=session,
        confidence_threshold=confidence_threshold,
    )
    required = required_modifier_base_fields(prepared.runtime.modifiers)
    reasons: list[str] = []
    values: list[tuple[str, int]] = []
    for field in required:
        if field not in pre_passive:
            reasons.append(f"missing-pre-passive-field:{field}")
            continue
        value = pre_passive[field]
        if not _is_int(value):
            reasons.append(f"invalid-pre-passive-field:{field}:{value!r}")
            continue
        values.append((field, int(value)))
    if reasons:
        raise LoadoutRuntimeBridgeError(reasons)
    base_values = tuple(sorted(values))
    old = prepared.session
    if old.initial_modifiers_applied and old.initial_base_values != base_values:
        raise LoadoutRuntimeBridgeError(("initial-modifier-base-mismatch",))

    result = apply_run_modifiers(pre_passive, prepared.runtime)
    result_values = tuple((field, int(result[field])) for field in required)
    if old.initial_modifiers_applied and old.initial_result_values != result_values:
        raise LoadoutRuntimeBridgeError(("initial-modifier-result-mismatch",))
    updated = replace(
        old,
        initial_modifiers_applied=True,
        initial_base_values=base_values,
        initial_result_values=result_values,
    )
    return InitialRunModifierResult(result, updated, prepared.runtime.modifiers)


def _validate_known_enchant(enchant_id: str) -> None:
    try:
        load_runtime_status_enchant(enchant_id)
    except (KeyError, ValueError, sqlite3.Error, json.JSONDecodeError) as error:
        raise LoadoutRuntimeBridgeError(
            (
                f"unknown-runtime-status-enchant:{enchant_id}:"
                f"{type(error).__name__}",
            )
        ) from error


def _instance_id(
    session: LoadoutRuntimeSession,
    context: PassiveStepContext,
    contract: StatusEnchantInstallContract,
) -> str:
    seed = "\0".join(
        (
            session.snapshot_digest,
            context.context_id,
            contract.usage_key,
            str(contract.rule_slot),
            contract.status_enchant_id,
        )
    ).encode("utf-8")
    return "passive:" + hashlib.sha256(seed).hexdigest()[:24]


def _merge_step_installs(
    current: Sequence[ActiveRuntimeStatusEnchant],
    installs: Sequence[InstalledPassiveStatus],
) -> tuple[ActiveRuntimeStatusEnchant, ...]:
    if isinstance(current, (str, bytes)) or not isinstance(current, Sequence):
        raise TypeError("current runtime enchants must be a sequence")
    merged: list[ActiveRuntimeStatusEnchant] = []
    by_instance: dict[str, ActiveRuntimeStatusEnchant] = {}
    reasons: list[str] = []
    for item in current:
        if not isinstance(item, ActiveRuntimeStatusEnchant):
            raise TypeError(
                "current runtime enchants must contain ActiveRuntimeStatusEnchant"
            )
        if (
            not item.instance_id
            or not item.enchant_id
            or item.max_uses < 0
            or item.uses < 0
            or (item.max_uses > 0 and item.uses > item.max_uses)
        ):
            reasons.append(f"invalid-runtime-instance:{item.instance_id!r}")
            continue
        try:
            _validate_known_enchant(item.enchant_id)
        except LoadoutRuntimeBridgeError as error:
            reasons.extend(error.reasons)
            continue
        if item.instance_id in by_instance:
            reasons.append(f"duplicate-runtime-instance:{item.instance_id}")
        else:
            by_instance[item.instance_id] = item
            merged.append(item)
    for install in installs:
        _validate_known_enchant(install.enchant_id)
        existing = by_instance.get(install.instance_id)
        if existing is not None:
            if existing.enchant_id != install.enchant_id or existing.max_uses != 0:
                reasons.append(
                    f"runtime-instance-conflict:{install.instance_id}"
                )
            continue
        entry = ActiveRuntimeStatusEnchant(
            instance_id=install.instance_id,
            enchant_id=install.enchant_id,
            # ProduceSkill.activationCount limits installation across stages;
            # it is not the in-exam trigger-use limit of the Master enchant.
            max_uses=0,
            uses=0,
        )
        by_instance[entry.instance_id] = entry
        merged.append(entry)
    if reasons:
        raise LoadoutRuntimeBridgeError(reasons)
    return tuple(merged)


def install_status_enchant_contracts(
    session: LoadoutRuntimeSession,
    context: PassiveStepContext,
    contracts: Sequence[StatusEnchantInstallContract],
    *,
    current: Sequence[ActiveRuntimeStatusEnchant] = (),
) -> StepStatusInstallResult:
    """Install matching contracts once, preserving source instances and uses."""

    if not isinstance(session, LoadoutRuntimeSession):
        raise TypeError("session must be LoadoutRuntimeSession")
    if not isinstance(context, PassiveStepContext):
        raise TypeError("context must be PassiveStepContext")
    if isinstance(contracts, (str, bytes)) or not isinstance(contracts, Sequence):
        raise TypeError("contracts must be a sequence")
    if not all(isinstance(item, StatusEnchantInstallContract) for item in contracts):
        raise TypeError("contracts must contain StatusEnchantInstallContract")

    prior = next(
        (
            record
            for record in session.step_records
            if record.context.context_id == context.context_id
        ),
        None,
    )
    if prior is not None:
        if prior.context != context:
            raise LoadoutRuntimeBridgeError(
                (f"step-context-id-reused:{context.context_id}",)
            )
        merged = _merge_step_installs(current, prior.installs)
        return StepStatusInstallResult(
            merged,
            prior.installs,
            prior.activated_usage_keys,
            session,
            True,
        )

    matching = tuple(contract for contract in contracts if context.matches(contract))
    grouped: dict[str, list[StatusEnchantInstallContract]] = {}
    for contract in matching:
        grouped.setdefault(contract.usage_key, []).append(contract)

    usage = session.usage_map
    installs: list[InstalledPassiveStatus] = []
    activated: list[str] = []
    reasons: list[str] = []
    for usage_key, group in grouped.items():
        persisted = usage.get(usage_key, 0)
        if any(contract.use_count != persisted for contract in group):
            reasons.append(f"contract-usage-mismatch:{usage_key}")
            continue
        limits = {contract.activation_limit for contract in group}
        if len(limits) != 1:
            reasons.append(f"contract-activation-limit-mismatch:{usage_key}")
            continue
        limit = next(iter(limits))
        if limit > 0 and persisted >= limit:
            continue
        group_installs: list[InstalledPassiveStatus] = []
        for contract in group:
            try:
                _validate_known_enchant(contract.status_enchant_id)
            except LoadoutRuntimeBridgeError as error:
                reasons.extend(error.reasons)
                continue
            group_installs.append(
                InstalledPassiveStatus(
                    instance_id=_instance_id(session, context, contract),
                    enchant_id=contract.status_enchant_id,
                    usage_key=usage_key,
                    source_index=contract.source_index,
                    skill_id=contract.skill_id,
                    rule_slot=contract.rule_slot,
                    produce_trigger_id=contract.produce_trigger_id,
                )
            )
        if len(group_installs) != len(group):
            continue
        installs.extend(group_installs)
        usage[usage_key] = persisted + 1
        activated.append(usage_key)
    if reasons:
        raise LoadoutRuntimeBridgeError(reasons)

    record = PassiveStepRecord(context, tuple(activated), tuple(installs))
    updated_session = replace(
        session,
        usage_counts=tuple(sorted(usage.items())),
        step_records=tuple((*session.step_records, record)),
    )
    merged = _merge_step_installs(current, installs)
    return StepStatusInstallResult(
        merged,
        tuple(installs),
        tuple(activated),
        updated_session,
        False,
    )


def install_loadout_step_passives(
    snapshot: LoadoutSnapshot,
    catalog: MasterPassiveCatalog,
    context: PassiveStepContext,
    *,
    session: LoadoutRuntimeSession | None = None,
    current: Sequence[ActiveRuntimeStatusEnchant] = (),
    confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
) -> StepStatusInstallResult:
    """Authoritative high-level entry point for one lesson/audition start."""

    prepared = prepare_loadout_runtime(
        snapshot,
        catalog,
        session=session,
        confidence_threshold=confidence_threshold,
    )
    return install_status_enchant_contracts(
        prepared.session,
        context,
        prepared.runtime.status_enchant_installs,
        current=current,
    )


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
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


def save_loadout_runtime_session(
    session: LoadoutRuntimeSession,
    path: Path = DEFAULT_LOADOUT_RUNTIME_SESSION_PATH,
) -> None:
    if not isinstance(session, LoadoutRuntimeSession):
        raise TypeError("session must be LoadoutRuntimeSession")
    _atomic_write_json(Path(path), session.to_dict())


def load_loadout_runtime_session(
    path: Path = DEFAULT_LOADOUT_RUNTIME_SESSION_PATH,
) -> LoadoutRuntimeSession | None:
    path = Path(path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("loadout runtime session must be a JSON object")
    return LoadoutRuntimeSession.from_dict(payload)
