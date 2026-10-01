"""Fail-closed, evidence-bound resolution for one passive start-step.

This module deliberately does *not* replace the strict loadout runtime bridge.
The bridge remains the all-or-nothing API for deterministic application.  This
contract is narrower: it lets a caller isolate the probabilistic occurrences
that are relevant to the current observed step, without treating future or
already-passed occurrences as current blockers and without ever sampling RNG.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .audition_rules import FINAL, MID1, MID2
from .passive_catalog import (
    DETERMINISTIC_RULE_WHITELIST,
    EFFECT_DANCE_ADDITION,
    EFFECT_VISUAL_ADDITION,
    EFFECT_VOCAL_ADDITION,
    PassiveRule,
    PassiveSource,
)
from .passive_runtime import (
    EFFECT_EXAM_STATUS_ENCHANT,
    PHASE_START_AUDITION,
    PHASE_START_AUDITION_FINAL,
    PHASE_START_AUDITION_MID1,
    PHASE_START_AUDITION_MID2,
    PHASE_START_LESSON,
    PHASE_PRODUCE_START,
    passive_source_usage_key,
)


STAGE_LESSON = "lesson"
STAGES = frozenset({STAGE_LESSON, MID1, MID2, FINAL})
_STAGE_ORDER = {STAGE_LESSON: 0, MID1: 1, MID2: 2, FINAL: 3}
_LESSON_TRIGGER = re.compile(
    r"^p_trigger-start_lesson-lesson_(vocal|dance|visual)(?:_sp)?$"
)
_AUDITION_TARGETS = {
    (PHASE_START_AUDITION_MID1, "p_trigger-start_audition_mid1"): MID1,
    (PHASE_START_AUDITION_MID2, "p_trigger-start_audition_mid2"): MID2,
    (PHASE_START_AUDITION_FINAL, "p_trigger-start_audition_final"): FINAL,
}
_GENERIC_AUDITION = (
    PHASE_START_AUDITION,
    "p_trigger-start_audition-for_hif_memory",
)

CURRENT_REQUIRED = "current_required"
HISTORICAL_ABSORBED = "historical_absorbed"
FUTURE_DEFERRED = "future_deferred"
STRUCTURALLY_UNKNOWN = "structurally_unknown"
PRODUCE_START_PENDING_RECONCILIATION = "produce_start_pending_reconciliation"

_OUTER_ATTRIBUTE_EFFECT_TYPES = frozenset(
    {EFFECT_VOCAL_ADDITION, EFFECT_DANCE_ADDITION, EFFECT_VISUAL_ADDITION}
)
# Deliberately finite, Master-proven outer search shapes.  This is not a
# prefix/regex allowance: a new opcode or a new trigger must be reviewed first.
_OUTER_ATTRIBUTE_TRIGGER_PHASES = frozenset(
    {
        ("ProducePhaseType_BuyShopItemProduceCard", "p_trigger-buy_shop_item_produce_card"),
        ("ProducePhaseType_BuyShopItemProduceDrink", "p_trigger-buy_shop_item_produce_drink"),
        ("ProducePhaseType_ChangeProduceCard", "p_trigger-change_produce_card"),
        ("ProducePhaseType_CustomizeProduceCard", "p_trigger-customize_produce_card"),
        ("ProducePhaseType_DeleteProduceCard", "p_trigger-delete_produce_card-0000_0000-p_card_search-active_skill-deck_all"),
        ("ProducePhaseType_DeleteProduceCard", "p_trigger-delete_produce_card-0000_0000-p_card_search-deck_all"),
        ("ProducePhaseType_DeleteProduceCard", "p_trigger-delete_produce_card-0000_0000-p_card_search-mental_skill-deck_all"),
        ("ProducePhaseType_EndLesson", "p_trigger-end_lesson-lesson_dance"),
        ("ProducePhaseType_EndLesson", "p_trigger-end_lesson-lesson_dance_sp"),
        ("ProducePhaseType_EndLesson", "p_trigger-end_lesson-lesson_visual"),
        ("ProducePhaseType_EndLesson", "p_trigger-end_lesson-lesson_visual_sp"),
        ("ProducePhaseType_EndLesson", "p_trigger-end_lesson-lesson_vocal"),
        ("ProducePhaseType_EndLesson", "p_trigger-end_lesson-lesson_vocal_sp"),
        ("ProducePhaseType_GetProduceCard", "p_trigger-get_produce_card-0000_0000-p_card_search-active_skill-deck_all"),
        ("ProducePhaseType_GetProduceCard", "p_trigger-get_produce_card-0000_0000-p_card_search-deck_all"),
        ("ProducePhaseType_GetProduceCard", "p_trigger-get_produce_card-0000_0000-p_card_search-mental_skill-deck_all"),
        ("ProducePhaseType_GetProduceCard", "p_trigger-get_produce_card-p_card_search-ssr-deck_all-1"),
        ("ProducePhaseType_GetProduceDrink", "p_trigger-get_produce_drink"),
        ("ProducePhaseType_StartPresent", "p_trigger-start_present"),
        ("ProducePhaseType_UpgradeProduceCard", "p_trigger-upgrade_produce_card-0000_0000-p_card_search-active_skill-deck_all"),
        ("ProducePhaseType_UpgradeProduceCard", "p_trigger-upgrade_produce_card-0000_0000-p_card_search-deck_all"),
        ("ProducePhaseType_UpgradeProduceCard", "p_trigger-upgrade_produce_card-0000_0000-p_card_search-mental_skill-deck_all"),
    }
)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _exact_mapping(
    payload: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    actual = set(payload)
    if actual != set(expected):
        raise ValueError(
            f"{label} fields are invalid: missing={sorted(set(expected) - actual)} "
            f"unknown={sorted(actual - set(expected))}"
        )


def _sha256_text(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _strict_nonnegative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be an integer >= 0")
    return value


def _strict_positive_int(value: object, label: str) -> int:
    value = _strict_nonnegative_int(value, label)
    if value < 1:
        raise ValueError(f"{label} must be an integer >= 1")
    return value


@dataclass(frozen=True, slots=True)
class ContextualPassiveStepContext:
    """The observed run identity and exact step where a passive can occur."""

    run_id: str
    snapshot_digest: str
    produce_id: str
    context_id: str
    stage: str
    turn: int
    lesson_attribute: str | None = None
    route_position: int = 0
    passed_audition_stages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("snapshot_digest", self.snapshot_digest),
            ("produce_id", self.produce_id),
            ("context_id", self.context_id),
        ):
            _text(value, label)
        if len(self.snapshot_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.snapshot_digest
        ):
            raise ValueError("snapshot_digest must be a lowercase SHA-256 digest")
        if self.stage not in STAGES:
            raise ValueError("stage must be lesson, Mid1, Mid2, or Final")
        if not isinstance(self.turn, int) or isinstance(self.turn, bool) or self.turn < 1:
            raise ValueError("turn must be an integer >= 1")
        if self.stage == STAGE_LESSON:
            if self.lesson_attribute not in {"vocal", "dance", "visual"}:
                raise ValueError("lesson context requires vocal, dance, or visual")
        elif self.lesson_attribute is not None:
            raise ValueError("audition context cannot have lesson_attribute")
        if (
            not isinstance(self.route_position, int)
            or isinstance(self.route_position, bool)
            or self.route_position < 0
        ):
            raise ValueError("route_position must be an integer >= 0")
        if any(stage not in {MID1, MID2, FINAL} for stage in self.passed_audition_stages):
            raise ValueError("passed_audition_stages contains an unknown stage")
        if len(self.passed_audition_stages) != len(set(self.passed_audition_stages)):
            raise ValueError("passed_audition_stages must not repeat stages")
        if self.stage in self.passed_audition_stages:
            raise ValueError("current audition stage cannot already be passed")
        if self.stage != STAGE_LESSON:
            current_order = _STAGE_ORDER[self.stage]
            if any(
                _STAGE_ORDER[stage] >= current_order
                for stage in self.passed_audition_stages
            ):
                raise ValueError(
                    "passed_audition_stages contains a stage not earlier than current"
                )

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "snapshot_digest": self.snapshot_digest,
            "produce_id": self.produce_id,
            "context_id": self.context_id,
            "stage": self.stage,
            "turn": self.turn,
            "lesson_attribute": self.lesson_attribute,
            "route_position": self.route_position,
            "passed_audition_stages": list(self.passed_audition_stages),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ContextualPassiveStepContext":
        if not isinstance(payload, Mapping):
            raise ValueError("context must be an object")
        _exact_mapping(
            payload,
            frozenset({
                "run_id", "snapshot_digest", "produce_id", "context_id", "stage",
                "turn", "lesson_attribute", "route_position", "passed_audition_stages",
            }),
            "context",
        )
        passed = payload["passed_audition_stages"]
        if not isinstance(passed, list) or not all(isinstance(item, str) for item in passed):
            raise ValueError("passed_audition_stages must be a text array")
        lesson_attribute = payload["lesson_attribute"]
        if lesson_attribute is not None and not isinstance(lesson_attribute, str):
            raise ValueError("lesson_attribute must be text or null")
        return cls(
            _text(payload["run_id"], "run_id"),
            _sha256_text(payload["snapshot_digest"], "snapshot_digest"),
            _text(payload["produce_id"], "produce_id"),
            _text(payload["context_id"], "context_id"),
            _text(payload["stage"], "stage"),
            _strict_positive_int(payload["turn"], "turn"),
            lesson_attribute,
            _strict_nonnegative_int(payload["route_position"], "route_position"),
            tuple(passed),
        )


@dataclass(frozen=True, slots=True)
class PassiveOccurrence:
    """One non-deduplicated source/rule occurrence and its local disposition."""

    source_occurrence_key: str
    source_usage_key: str
    source_index: int
    rule_slot: int
    activation_limit: int
    status_enchant_id: str | None
    classification: str
    probabilistic: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ContextualPassiveBlocker:
    code: str
    source_occurrence_key: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ContextualPassiveResolution:
    occurrences: tuple[PassiveOccurrence, ...]
    blockers: tuple[ContextualPassiveBlocker, ...]

    @property
    def deferred(self) -> tuple[PassiveOccurrence, ...]:
        return tuple(
            item
            for item in self.occurrences
            if item.classification in {HISTORICAL_ABSORBED, FUTURE_DEFERRED}
        )

    @property
    def current_required(self) -> tuple[PassiveOccurrence, ...]:
        return tuple(
            item for item in self.occurrences if item.classification == CURRENT_REQUIRED
        )


@dataclass(frozen=True, slots=True)
class EvidenceBoundPassiveOutcome:
    """Observed outcome; ``triggered`` is evidence, never an RNG instruction."""

    run_id: str
    snapshot_digest: str
    context_digest: str
    source_occurrence_key: str
    capture_path: str
    capture_hash: str
    stable_status_proof: bool
    full_status_proof: bool
    triggered: bool

    def __post_init__(self) -> None:
        for label, value in (
            ("run_id", self.run_id),
            ("snapshot_digest", self.snapshot_digest),
            ("context_digest", self.context_digest),
            ("source_occurrence_key", self.source_occurrence_key),
            ("capture_path", self.capture_path),
            ("capture_hash", self.capture_hash),
        ):
            _text(value, label)
        for label, value in (
            ("stable_status_proof", self.stable_status_proof),
            ("full_status_proof", self.full_status_proof),
            ("triggered", self.triggered),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{label} must be a boolean")
        if any(
            len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
            for value in (self.snapshot_digest, self.context_digest)
        ):
            raise ValueError("outcome digests must be SHA-256 digests")
        if len(self.capture_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.capture_hash
        ):
            raise ValueError("capture_hash must be a SHA-256 digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "snapshot_digest": self.snapshot_digest,
            "context_digest": self.context_digest,
            "source_occurrence_key": self.source_occurrence_key,
            "capture_path": self.capture_path,
            "capture_hash": self.capture_hash,
            "stable_status_proof": self.stable_status_proof,
            "full_status_proof": self.full_status_proof,
            "triggered": self.triggered,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "EvidenceBoundPassiveOutcome":
        if not isinstance(payload, Mapping):
            raise ValueError("outcome must be an object")
        _exact_mapping(payload, frozenset({
            "run_id", "snapshot_digest", "context_digest", "source_occurrence_key",
            "capture_path", "capture_hash", "stable_status_proof",
            "full_status_proof", "triggered",
        }), "outcome")
        for name in ("stable_status_proof", "full_status_proof", "triggered"):
            if not isinstance(payload[name], bool):
                raise ValueError(f"{name} must be a boolean")
        return cls(
            _text(payload["run_id"], "run_id"),
            _sha256_text(payload["snapshot_digest"], "snapshot_digest"),
            _sha256_text(payload["context_digest"], "context_digest"),
            _text(payload["source_occurrence_key"], "source_occurrence_key"),
            _text(payload["capture_path"], "capture_path"),
            _sha256_text(payload["capture_hash"], "capture_hash"),
            payload["stable_status_proof"], payload["full_status_proof"],
            payload["triggered"],
        )


def verify_evidence_bound_outcome(
    outcome: EvidenceBoundPassiveOutcome,
) -> ContextualPassiveBlocker | None:
    """Verify that the evidence file still exists and matches its declared hash."""

    if not isinstance(outcome, EvidenceBoundPassiveOutcome):
        raise TypeError("outcome must be EvidenceBoundPassiveOutcome")
    path = Path(outcome.capture_path)
    if not path.is_file():
        return ContextualPassiveBlocker(
            "evidence-file-missing", outcome.source_occurrence_key,
            "capture_path does not name a regular file",
        )
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != outcome.capture_hash:
        return ContextualPassiveBlocker(
            "evidence-hash-mismatch", outcome.source_occurrence_key,
            "capture file SHA-256 differs from capture_hash",
        )
    return None


@dataclass(frozen=True, slots=True)
class ContextualPassiveStepRecord:
    context_id: str
    context_digest: str
    outcomes: tuple[EvidenceBoundPassiveOutcome, ...]
    installed_source_occurrence_keys: tuple[str, ...]
    activated_source_occurrence_keys: tuple[str, ...]
    activated_usage_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.context_id, "context_id")
        _sha256_text(self.context_digest, "context_digest")
        if not all(isinstance(item, EvidenceBoundPassiveOutcome) for item in self.outcomes):
            raise TypeError("outcomes must contain EvidenceBoundPassiveOutcome")
        outcome_keys = [item.source_occurrence_key for item in self.outcomes]
        if len(outcome_keys) != len(set(outcome_keys)):
            raise ValueError("outcomes must not repeat an occurrence")
        for label, keys in (
            ("installed_source_occurrence_keys", self.installed_source_occurrence_keys),
            ("activated_source_occurrence_keys", self.activated_source_occurrence_keys),
            ("activated_usage_keys", self.activated_usage_keys),
        ):
            if not all(isinstance(key, str) and key for key in keys):
                raise ValueError(f"{label} must be a text array")
            if len(keys) != len(set(keys)):
                raise ValueError(f"{label} must not repeat keys")
        if self.installed_source_occurrence_keys != self.activated_source_occurrence_keys:
            raise ValueError("installed and activated occurrence keys must agree")

    def to_dict(self) -> dict[str, object]:
        return {
            "context_id": self.context_id,
            "context_digest": self.context_digest,
            "outcomes": [item.to_dict() for item in self.outcomes],
            "installed_source_occurrence_keys": list(self.installed_source_occurrence_keys),
            "activated_source_occurrence_keys": list(self.activated_source_occurrence_keys),
            "activated_usage_keys": list(self.activated_usage_keys),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ContextualPassiveStepRecord":
        if not isinstance(payload, Mapping):
            raise ValueError("record must be an object")
        _exact_mapping(payload, frozenset({
            "context_id", "context_digest", "outcomes",
            "installed_source_occurrence_keys", "activated_source_occurrence_keys",
            "activated_usage_keys",
        }), "record")
        outcomes = payload["outcomes"]
        key_fields = (
            "installed_source_occurrence_keys", "activated_source_occurrence_keys",
            "activated_usage_keys",
        )
        if not isinstance(outcomes, list) or not all(isinstance(item, Mapping) for item in outcomes):
            raise ValueError("record outcomes must be an object array")
        if any(
            not isinstance(payload[field], list)
            or not all(isinstance(item, str) and item for item in payload[field])
            for field in key_fields
        ):
            raise ValueError("record key fields must be text arrays")
        return cls(
            _text(payload["context_id"], "context_id"),
            _sha256_text(payload["context_digest"], "context_digest"),
            tuple(EvidenceBoundPassiveOutcome.from_dict(item) for item in outcomes),
            tuple(payload["installed_source_occurrence_keys"]),
            tuple(payload["activated_source_occurrence_keys"]),
            tuple(payload["activated_usage_keys"]),
        )


@dataclass(frozen=True, slots=True)
class ContextualPassiveStepSession:
    run_id: str
    snapshot_digest: str
    produce_id: str
    usage_counts: tuple[tuple[str, int], ...] = ()
    records: tuple[ContextualPassiveStepRecord, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _text(self.run_id, "run_id")
        _text(self.produce_id, "produce_id")
        if self.schema_version != 1:
            raise ValueError("unsupported contextual passive session schema version")
        if len(self.snapshot_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.snapshot_digest
        ):
            raise ValueError("snapshot_digest must be a SHA-256 digest")
        keys = []
        for key, count in self.usage_counts:
            keys.append(_text(key, "usage key"))
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("usage count must be a non-negative integer")
        if len(keys) != len(set(keys)) or tuple(keys) != tuple(sorted(keys)):
            raise ValueError("usage_counts must have unique sorted keys")
        if not all(isinstance(record, ContextualPassiveStepRecord) for record in self.records):
            raise TypeError("records must contain ContextualPassiveStepRecord")
        record_ids = [record.context_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("context ids must be unique")
        activation_tally: dict[str, int] = {}
        for record in self.records:
            for key in record.activated_usage_keys:
                activation_tally[key] = activation_tally.get(key, 0) + 1
        if activation_tally != dict(self.usage_counts):
            raise ValueError("usage_counts do not match recorded activations")

    @property
    def usage_map(self) -> dict[str, int]:
        return dict(self.usage_counts)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "snapshot_digest": self.snapshot_digest,
            "produce_id": self.produce_id,
            "usage_counts": [list(item) for item in self.usage_counts],
            "records": [item.to_dict() for item in self.records],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ContextualPassiveStepSession":
        if not isinstance(payload, Mapping):
            raise ValueError("session must be an object")
        _exact_mapping(payload, frozenset({
            "schema_version", "run_id", "snapshot_digest", "produce_id",
            "usage_counts", "records",
        }), "session")
        if payload["schema_version"] != 1:
            raise ValueError("unsupported contextual passive session schema version")
        pairs = payload["usage_counts"]
        records = payload["records"]
        if not isinstance(pairs, list) or not all(
            isinstance(item, list) and len(item) == 2 for item in pairs
        ):
            raise ValueError("usage_counts must be an array of pairs")
        if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
            raise ValueError("records must be an object array")
        return cls(
            _text(payload["run_id"], "run_id"),
            _sha256_text(payload["snapshot_digest"], "snapshot_digest"),
            _text(payload["produce_id"], "produce_id"),
            tuple(
                (_text(item[0], "usage key"), _strict_nonnegative_int(item[1], "usage count"))
                for item in pairs
            ),
            tuple(ContextualPassiveStepRecord.from_dict(item) for item in records),
            1,
        )


@dataclass(frozen=True, slots=True)
class ContextualPassiveStepResult:
    session: ContextualPassiveStepSession
    resolution: ContextualPassiveResolution
    evaluated: tuple[EvidenceBoundPassiveOutcome, ...]
    installed_source_occurrence_keys: tuple[str, ...]
    activated_source_occurrence_keys: tuple[str, ...]
    activated_usage_keys: tuple[str, ...]
    blockers: tuple[ContextualPassiveBlocker, ...]
    replayed: bool


def new_contextual_passive_step_session(
    context: ContextualPassiveStepContext,
) -> ContextualPassiveStepSession:
    return ContextualPassiveStepSession(
        context.run_id, context.snapshot_digest, context.produce_id
    )


def save_contextual_passive_step_session(
    session: ContextualPassiveStepSession, path: Path
) -> None:
    """Atomically persist a strict replay session to an explicit path."""

    if not isinstance(session, ContextualPassiveStepSession):
        raise TypeError("session must be ContextualPassiveStepSession")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(session.to_dict(), ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_contextual_passive_step_session(
    path: Path,
) -> ContextualPassiveStepSession | None:
    path = Path(path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("contextual passive session root must be an object")
    return ContextualPassiveStepSession.from_dict(payload)


def source_occurrence_key(source: PassiveSource, source_index: int, rule: PassiveRule) -> str:
    """Stable key for the actual occurrence, never a deduplicated diagnostic."""

    if not isinstance(source, PassiveSource) or not isinstance(rule, PassiveRule):
        raise TypeError("source and rule must be passive catalog values")
    return f"{passive_source_usage_key(source, source_index)}|rule:{rule.slot}:{rule.effect_id}"


def _status_enchant_id(rule: PassiveRule) -> str | None:
    effect = rule.raw.get("effect") if isinstance(rule.raw, Mapping) else None
    value = effect.get("produceExamStatusEnchantId") if isinstance(effect, Mapping) else None
    return value if isinstance(value, str) and value else None


def _target_for(rule: PassiveRule) -> tuple[str | None, str | None]:
    lesson = _LESSON_TRIGGER.fullmatch(rule.trigger_id)
    if rule.trigger_phase_type == PHASE_START_LESSON and lesson:
        return STAGE_LESSON, lesson.group(1)
    if (rule.trigger_phase_type, rule.trigger_id) == _GENERIC_AUDITION:
        return "audition", None
    return _AUDITION_TARGETS.get(
        (rule.trigger_phase_type, rule.trigger_id)
    ), None


def _classification(context: ContextualPassiveStepContext, rule: PassiveRule) -> tuple[str, str]:
    target, lesson_attribute = _target_for(rule)
    if target is None:
        return STRUCTURALLY_UNKNOWN, "unknown-status-trigger"
    if target == "audition":
        if context.stage == STAGE_LESSON:
            if context.passed_audition_stages:
                return HISTORICAL_ABSORBED, "generic-audition-already-passed"
            return FUTURE_DEFERRED, "generic-audition-after-lesson"
        return CURRENT_REQUIRED, "generic-audition-matches-current-stage"
    if target == STAGE_LESSON:
        if context.stage != STAGE_LESSON:
            return FUTURE_DEFERRED, "lesson-may-occur-after-current-audition"
        if lesson_attribute == context.lesson_attribute:
            return CURRENT_REQUIRED, "lesson-trigger-matches-current-step"
        return FUTURE_DEFERRED, "other-lesson-step-not-current"
    if target in context.passed_audition_stages:
        return HISTORICAL_ABSORBED, "stage-already-passed"
    if context.stage == STAGE_LESSON:
        return FUTURE_DEFERRED, "stage-not-yet-passed"
    target_order = _STAGE_ORDER[target]
    current_order = _STAGE_ORDER[context.stage]
    if target_order < current_order:
        return FUTURE_DEFERRED, "stage-history-not-proven-by-context"
    if target_order > current_order:
        return FUTURE_DEFERRED, "stage-not-reached"
    return CURRENT_REQUIRED, "stage-matches-current-step"


def _fixed_scalar(rule: PassiveRule) -> bool:
    return (
        isinstance(rule.effect_value_min, int)
        and not isinstance(rule.effect_value_min, bool)
        and isinstance(rule.effect_value_max, int)
        and not isinstance(rule.effect_value_max, bool)
        and rule.effect_value_min == rule.effect_value_max
    )


def _known_status_catalog_reason(reason: str, rule: PassiveRule) -> bool:
    """Allow only the two diagnostics expected for reviewed status passives."""

    if reason == (
        f"rule-not-whitelisted:{rule.trigger_id}:"
        f"{EFFECT_EXAM_STATUS_ENCHANT}"
    ):
        return True
    return (
        isinstance(rule.activation_rate_permil, int)
        and not isinstance(rule.activation_rate_permil, bool)
        and rule.activation_rate_permil > 0
        and reason
        == f"probabilistic-activation-rate:{rule.activation_rate_permil}"
    )


def _known_non_status_classification(
    rule: PassiveRule,
) -> tuple[str, str] | None:
    """Return a disposition only for an explicitly reviewed non-status rule."""

    if rule.activation_rate_permil != 0 or not _fixed_scalar(rule):
        return None
    if (
        rule.trigger_phase_type == PHASE_PRODUCE_START
        and (rule.trigger_id, rule.effect_type) in DETERMINISTIC_RULE_WHITELIST
        and not rule.unsupported_rules
    ):
        return (
            PRODUCE_START_PENDING_RECONCILIATION,
            "produce-start-known-pending-reconciliation",
        )
    if (
        rule.effect_type in _OUTER_ATTRIBUTE_EFFECT_TYPES
        and (rule.trigger_phase_type, rule.trigger_id)
        in _OUTER_ATTRIBUTE_TRIGGER_PHASES
    ):
        expected_catalog_reason = (
            f"rule-not-whitelisted:{rule.trigger_id}:{rule.effect_type}"
        )
        if rule.unsupported_rules in ((), (expected_catalog_reason,)):
            return FUTURE_DEFERRED, "known-outer-attribute-addition"
    return None


def classify_contextual_passive_occurrences(
    sources: Sequence[PassiveSource], context: ContextualPassiveStepContext
) -> ContextualPassiveResolution:
    """Classify each source occurrence locally, retaining duplicate sources."""

    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence")
    occurrences: list[PassiveOccurrence] = []
    blockers: list[ContextualPassiveBlocker] = []
    for source_index, source in enumerate(sources):
        if not isinstance(source, PassiveSource):
            raise TypeError("sources must contain PassiveSource")
        source_invalid = bool(source.unsupported_rules) or not isinstance(
            source.activation_count, int
        ) or isinstance(source.activation_count, bool) or source.activation_count < 0
        usage_key = passive_source_usage_key(source, source_index)
        activation_limit = int(source.activation_count) if not source_invalid else 0
        for rule in source.rules:
            if not isinstance(rule, PassiveRule):
                raise TypeError("PassiveSource.rules must contain PassiveRule")
            key = source_occurrence_key(source, source_index, rule)
            probabilistic = isinstance(rule.activation_rate_permil, int) and not isinstance(rule.activation_rate_permil, bool) and rule.activation_rate_permil != 0
            invalid_rate = (
                not isinstance(rule.activation_rate_permil, int)
                or isinstance(rule.activation_rate_permil, bool)
                or not 0 <= rule.activation_rate_permil <= 1000
            )
            invalid_rule = (
                source_invalid
                or invalid_rate
                or not isinstance(rule.effect_type, str)
                or not rule.effect_type
                or not isinstance(rule.trigger_id, str)
                or not rule.trigger_id
                or not isinstance(rule.trigger_phase_type, str)
                or not rule.trigger_phase_type
            )
            status_rule = rule.effect_type == EFFECT_EXAM_STATUS_ENCHANT
            known_non_status = (
                None if status_rule else _known_non_status_classification(rule)
            )
            if status_rule:
                invalid_rule = (
                    invalid_rule
                    or _status_enchant_id(rule) is None
                    or rule.effect_value_min != 0
                    or rule.effect_value_max != 0
                    or bool(
                    rule.unsupported_rules
                    and any(
                        not _known_status_catalog_reason(reason, rule)
                        for reason in rule.unsupported_rules
                    )
                    )
                )
            else:
                invalid_rule = invalid_rule or known_non_status is None
            if invalid_rule:
                occurrence = PassiveOccurrence(
                    key, usage_key, source_index, rule.slot, activation_limit,
                    _status_enchant_id(rule),
                    STRUCTURALLY_UNKNOWN, probabilistic, "unsupported-or-malformed-rule"
                )
                occurrences.append(occurrence)
                blockers.append(ContextualPassiveBlocker(
                    "structurally-unknown", key, occurrence.reason
                ))
                continue
            if not status_rule:
                assert known_non_status is not None
                classification, reason = known_non_status
                occurrences.append(PassiveOccurrence(
                    key, usage_key, source_index, rule.slot, activation_limit,
                    None, classification, probabilistic, reason,
                ))
                continue
            classification, reason = _classification(context, rule)
            occurrence = PassiveOccurrence(
                key, usage_key, source_index, rule.slot, activation_limit,
                _status_enchant_id(rule),
                classification, probabilistic, reason
            )
            occurrences.append(occurrence)
            if classification == STRUCTURALLY_UNKNOWN:
                blockers.append(ContextualPassiveBlocker(
                    "structurally-unknown", key, reason
                ))
    return ContextualPassiveResolution(tuple(occurrences), tuple(blockers))


def resolve_contextual_passive_step(
    session: ContextualPassiveStepSession,
    context: ContextualPassiveStepContext,
    sources: Sequence[PassiveSource],
    outcomes: Sequence[EvidenceBoundPassiveOutcome] = (),
) -> ContextualPassiveStepResult:
    """Commit only evidence-proven current probabilities; never sample RNG.

    Any mismatch or missing proof returns the original session and no install or
    usage change.  A proven false outcome is recorded as evaluated but does not
    install or consume usage.
    """

    if not isinstance(session, ContextualPassiveStepSession):
        raise TypeError("session must be ContextualPassiveStepSession")
    if not isinstance(context, ContextualPassiveStepContext):
        raise TypeError("context must be ContextualPassiveStepContext")
    if not all(isinstance(item, EvidenceBoundPassiveOutcome) for item in outcomes):
        raise TypeError("outcomes must contain EvidenceBoundPassiveOutcome")
    identity_blockers: list[ContextualPassiveBlocker] = []
    if session.run_id != context.run_id:
        identity_blockers.append(ContextualPassiveBlocker("run-id-mismatch", None, "session and context run_id differ"))
    if session.snapshot_digest != context.snapshot_digest:
        identity_blockers.append(ContextualPassiveBlocker("snapshot-digest-mismatch", None, "session and context snapshot_digest differ"))
    if session.produce_id != context.produce_id:
        identity_blockers.append(ContextualPassiveBlocker("produce-id-mismatch", None, "session and context produce_id differ"))
    resolution = classify_contextual_passive_occurrences(sources, context)
    prior = next((record for record in session.records if record.context_id == context.context_id), None)
    if prior is not None:
        if prior.context_digest != context.digest:
            identity_blockers.append(ContextualPassiveBlocker("context-mismatch", None, "context_id is bound to a different context digest"))
        prior_index = session.records.index(prior)
        usage_before: dict[str, int] = {}
        for earlier in session.records[:prior_index]:
            for key in earlier.activated_usage_keys:
                usage_before[key] = usage_before.get(key, 0) + 1
        eligible_current = tuple(
            item
            for item in resolution.current_required
            if not (
                item.activation_limit > 0
                and usage_before.get(item.source_usage_key, 0)
                >= item.activation_limit
            )
        )
        expected_outcome_keys = {
            item.source_occurrence_key
            for item in eligible_current
            if item.probabilistic
        }
        actual_outcome_keys = {
            item.source_occurrence_key for item in prior.outcomes
        }
        if actual_outcome_keys != expected_outcome_keys:
            identity_blockers.append(ContextualPassiveBlocker(
                "replay-current-occurrence-mismatch", None,
                "recorded outcomes differ from current Master-required occurrences",
            ))
        outcome_by_key = {
            item.source_occurrence_key: item for item in prior.outcomes
        }
        for outcome in prior.outcomes:
            if (
                outcome.run_id != context.run_id
                or outcome.snapshot_digest != context.snapshot_digest
                or outcome.context_digest != context.digest
            ):
                identity_blockers.append(ContextualPassiveBlocker(
                    "outcome-identity-mismatch", outcome.source_occurrence_key,
                    "persisted outcome is not bound to replay context",
                ))
            if not outcome.stable_status_proof or not outcome.full_status_proof:
                identity_blockers.append(ContextualPassiveBlocker(
                    "outcome-status-proof-missing", outcome.source_occurrence_key,
                    "persisted outcome no longer has complete proof",
                ))
            evidence_blocker = verify_evidence_bound_outcome(outcome)
            if evidence_blocker is not None:
                identity_blockers.append(evidence_blocker)
        expected_installed = tuple(
            item.source_occurrence_key
            for item in eligible_current
            if not item.probabilistic
            or outcome_by_key.get(item.source_occurrence_key, None) is not None
            and outcome_by_key[item.source_occurrence_key].triggered
        )
        expected_usage = tuple(dict.fromkeys(
            item.source_usage_key
            for item in eligible_current
            if item.source_occurrence_key in expected_installed
        ))
        if (
            prior.installed_source_occurrence_keys != expected_installed
            or prior.activated_source_occurrence_keys != expected_installed
            or prior.activated_usage_keys != expected_usage
        ):
            identity_blockers.append(ContextualPassiveBlocker(
                "replay-activation-mismatch", None,
                "persisted installs or usage differ from current Master resolution",
            ))
        if identity_blockers:
            return ContextualPassiveStepResult(session, resolution, (), (), (), (), tuple((*resolution.blockers, *identity_blockers)), False)
        return ContextualPassiveStepResult(session, resolution, prior.outcomes, prior.installed_source_occurrence_keys, prior.activated_source_occurrence_keys, prior.activated_usage_keys, resolution.blockers, True)
    blockers = [*resolution.blockers, *identity_blockers]
    usage = session.usage_map
    current_by_usage: dict[str, list[PassiveOccurrence]] = {}
    for occurrence in resolution.current_required:
        current_by_usage.setdefault(occurrence.source_usage_key, []).append(occurrence)
    exhausted_usage_keys = {
        usage_key
        for usage_key, group in current_by_usage.items()
        if group[0].activation_limit > 0
        and usage.get(usage_key, 0) >= group[0].activation_limit
    }
    current_probability = {
        item.source_occurrence_key: item
        for item in resolution.current_required
        if item.probabilistic and item.source_usage_key not in exhausted_usage_keys
    }
    supplied: dict[str, EvidenceBoundPassiveOutcome] = {}
    for outcome in outcomes:
        if outcome.source_occurrence_key in supplied:
            blockers.append(ContextualPassiveBlocker("duplicate-outcome", outcome.source_occurrence_key, "more than one outcome for occurrence"))
        supplied[outcome.source_occurrence_key] = outcome
        if outcome.source_occurrence_key not in current_probability:
            blockers.append(ContextualPassiveBlocker("outcome-not-current-required", outcome.source_occurrence_key, "outcome does not belong to an eligible current probabilistic occurrence"))
            continue
        if (outcome.run_id != context.run_id or outcome.snapshot_digest != context.snapshot_digest or outcome.context_digest != context.digest):
            blockers.append(ContextualPassiveBlocker("outcome-identity-mismatch", outcome.source_occurrence_key, "outcome is not bound to this run, snapshot, and context"))
        if not outcome.stable_status_proof or not outcome.full_status_proof:
            blockers.append(ContextualPassiveBlocker("outcome-status-proof-missing", outcome.source_occurrence_key, "outcome requires stable and full status proof"))
        evidence_blocker = verify_evidence_bound_outcome(outcome)
        if evidence_blocker is not None:
            blockers.append(evidence_blocker)
    for key in current_probability:
        if key not in supplied:
            blockers.append(ContextualPassiveBlocker("outcome-missing", key, "current probabilistic occurrence has no evidence-bound outcome"))
    if blockers:
        return ContextualPassiveStepResult(session, resolution, (), (), (), (), tuple(blockers), False)

    installed: list[str] = []
    activated: list[str] = []
    activated_usage: list[str] = []
    evaluated = tuple(outcomes)
    for usage_key, group in current_by_usage.items():
        if usage_key in exhausted_usage_keys:
            continue
        group_installs = [
            occurrence.source_occurrence_key
            for occurrence in group
            if not occurrence.probabilistic
            or supplied[occurrence.source_occurrence_key].triggered
        ]
        if not group_installs:
            continue
        # activation_count is source-wide: several rule occurrences share one use.
        usage[usage_key] = usage.get(usage_key, 0) + 1
        installed.extend(group_installs)
        activated.extend(group_installs)
        activated_usage.append(usage_key)
    record = ContextualPassiveStepRecord(
        context.context_id, context.digest, evaluated, tuple(installed), tuple(activated),
        tuple(activated_usage),
    )
    updated = ContextualPassiveStepSession(
        session.run_id, session.snapshot_digest, session.produce_id,
        tuple(sorted(usage.items())), (*session.records, record)
    )
    return ContextualPassiveStepResult(updated, resolution, evaluated, tuple(installed), tuple(activated), tuple(activated_usage), (), False)
