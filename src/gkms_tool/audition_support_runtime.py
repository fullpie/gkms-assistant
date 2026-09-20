"""Durable exact inputs for native draw-card support upgrades.

The support O6 bonus is not a probability.  A live horizon therefore accepts
only the already resolved native ``runtimePermil`` value, bound to the exact
run/checkpoint transition being evaluated.  This module is deliberately an
artifact/API boundary; it does not derive or guess those values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


AUDITION_SUPPORT_RUNTIME_SCHEMA_VERSION = 2
SUPPORT_RUNTIME_FILENAME = "audition_support_runtime.json"
HAND_HOLD_EVIDENCE_KIND = "complete-runtime-source-audit-v1"
SUPPORTED_LESSON_TYPES = frozenset(
    {
        "ProduceStepLessonType_LessonVocal",
        "ProduceStepLessonType_LessonDance",
        "ProduceStepLessonType_LessonVisual",
    }
)
# ``ProduceParameterType.Unknown`` is the native default for a support row
# whose upgrade applies to cards from every lesson attribute.  Keep it out of
# ``SUPPORTED_LESSON_TYPES``: that set validates the current lesson event,
# while this set validates the support-side predicate.  In particular, an
# unknown current lesson must never become an implicit wildcard.
LESSON_TYPE_UNKNOWN = "ProduceStepLessonType_Unknown"
SUPPORTED_SUPPORT_LESSON_TYPES = frozenset(
    {*SUPPORTED_LESSON_TYPES, LESSON_TYPE_UNKNOWN}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(
    value: object, label: str, *, minimum: int = 0, maximum: int | None = None
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be an integer <= {maximum}")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _exact(payload: Mapping[str, object], fields: set[str], label: str) -> None:
    actual = set(payload)
    if actual != fields:
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )


@dataclass(frozen=True, order=True, slots=True)
class SupportUpgradeRuntimeInput:
    """One support's exact HandAdd predicate and native probability."""

    support_id: str
    lesson_type: str
    runtime_permil: int
    loadout_order: int
    card_search_id: str

    def __post_init__(self) -> None:
        _text(self.support_id, "support_id")
        if self.lesson_type not in SUPPORTED_SUPPORT_LESSON_TYPES:
            raise ValueError(f"unsupported support lesson_type: {self.lesson_type}")
        _integer(
            self.runtime_permil,
            "runtime_permil",
            minimum=0,
            maximum=1000,
        )
        _integer(self.loadout_order, "loadout_order")
        _text(self.card_search_id, "card_search_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "lesson_type": self.lesson_type,
            "runtime_permil": self.runtime_permil,
            "loadout_order": self.loadout_order,
            "card_search_id": self.card_search_id,
        }

    @classmethod
    def from_keyed_dict(
        cls, support_id: str, payload: Mapping[str, object]
    ) -> "SupportUpgradeRuntimeInput":
        if not isinstance(payload, Mapping):
            raise ValueError(f"support {support_id!r} must be an object")
        _exact(
            payload,
            {"lesson_type", "runtime_permil", "loadout_order", "card_search_id"},
            f"support {support_id!r}",
        )
        return cls(
            support_id=_text(support_id, "support_id"),
            lesson_type=_text(payload["lesson_type"], "lesson_type"),
            runtime_permil=_integer(
                payload["runtime_permil"],
                "runtime_permil",
                maximum=1000,
            ),
            loadout_order=_integer(payload["loadout_order"], "loadout_order"),
            card_search_id=_text(payload["card_search_id"], "card_search_id"),
        )


def support_lesson_type_matches(
    support_lesson_type: object, current_lesson_type: object
) -> bool:
    """Return whether one support predicate applies to a current lesson.

    A support row serialized with native ``_filterParameterType=0`` carries
    ``ProduceStepLessonType_Unknown`` after typed projection.  In this
    support-specific predicate that value means *Any* (not an unresolved
    current lesson), so it matches each of the three concrete lesson types.
    The current lesson remains strict and must itself be one of the three
    known values.
    """

    if not isinstance(support_lesson_type, str) or not isinstance(
        current_lesson_type, str
    ):
        return False
    return (
        current_lesson_type in SUPPORTED_LESSON_TYPES
        and support_lesson_type in SUPPORTED_SUPPORT_LESSON_TYPES
        and (
            support_lesson_type == LESSON_TYPE_UNKNOWN
            or support_lesson_type == current_lesson_type
        )
    )


@dataclass(frozen=True, slots=True)
class AuditionSupportRuntimeArtifact:
    """Run/checkpoint-bound support inputs and current-turn used IDs."""

    schema_version: int
    run_id: str
    step_context_id: str
    step_context_digest: str
    session_transition_id: str
    zone_checkpoint_digest: str
    round_number: int
    hand_hold_count: int
    hand_hold_evidence_kind: str
    hand_hold_evidence_sha256: str
    runtime_permil_evidence_kind: str
    runtime_permil_evidence_sha256: str
    supports: tuple[SupportUpgradeRuntimeInput, ...]
    used_support_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != AUDITION_SUPPORT_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported audition support runtime schema")
        _text(self.run_id, "run_id")
        _text(self.step_context_id, "step_context_id")
        _sha256(self.step_context_digest, "step_context_digest")
        _text(self.session_transition_id, "session_transition_id")
        _sha256(self.zone_checkpoint_digest, "zone_checkpoint_digest")
        _integer(self.round_number, "round_number", minimum=1)
        _integer(self.hand_hold_count, "hand_hold_count")
        _text(self.hand_hold_evidence_kind, "hand_hold_evidence_kind")
        _sha256(self.hand_hold_evidence_sha256, "hand_hold_evidence_sha256")
        _text(self.runtime_permil_evidence_kind, "runtime_permil_evidence_kind")
        _sha256(
            self.runtime_permil_evidence_sha256,
            "runtime_permil_evidence_sha256",
        )
        values = tuple(self.supports)
        if not all(isinstance(value, SupportUpgradeRuntimeInput) for value in values):
            raise TypeError("supports must contain SupportUpgradeRuntimeInput values")
        ids = [value.support_id for value in values]
        orders = [value.loadout_order for value in values]
        if len(set(ids)) != len(ids):
            raise ValueError("support IDs must be unique")
        if len(set(orders)) != len(orders):
            raise ValueError("support loadout_order values must be unique")
        object.__setattr__(
            self,
            "supports",
            tuple(sorted(values, key=lambda value: value.loadout_order)),
        )
        used = tuple(self.used_support_ids)
        if any(not isinstance(value, str) or not value.strip() for value in used):
            raise ValueError("used_support_ids must contain non-empty text")
        if len(set(used)) != len(used):
            raise ValueError("used_support_ids must be unique")
        unknown = set(used) - set(ids)
        if unknown:
            raise ValueError(f"used_support_ids are absent from supports: {sorted(unknown)}")
        order_by_id = {value.support_id: value.loadout_order for value in values}
        object.__setattr__(
            self,
            "used_support_ids",
            tuple(sorted(used, key=order_by_id.__getitem__)),
        )

    @property
    def support_inputs(self) -> Mapping[str, SupportUpgradeRuntimeInput]:
        return {value.support_id: value for value in self.supports}

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "session_transition_id": self.session_transition_id,
            "zone_checkpoint_digest": self.zone_checkpoint_digest,
            "round_number": self.round_number,
            "hand_hold_count": self.hand_hold_count,
            "hand_hold_evidence_kind": self.hand_hold_evidence_kind,
            "hand_hold_evidence_sha256": self.hand_hold_evidence_sha256,
            "runtime_permil_evidence_kind": self.runtime_permil_evidence_kind,
            "runtime_permil_evidence_sha256": (
                self.runtime_permil_evidence_sha256
            ),
            "supports": {
                value.support_id: value.to_dict() for value in self.supports
            },
            "used_support_ids": list(self.used_support_ids),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionSupportRuntimeArtifact":
        if not isinstance(payload, Mapping):
            raise ValueError("audition support runtime root must be an object")
        if payload.get("schema_version") != AUDITION_SUPPORT_RUNTIME_SCHEMA_VERSION:
            raise ValueError("unsupported audition support runtime schema")
        fields = {
            "schema_version",
            "run_id",
            "step_context_id",
            "step_context_digest",
            "session_transition_id",
            "zone_checkpoint_digest",
            "round_number",
            "hand_hold_count",
            "hand_hold_evidence_kind",
            "hand_hold_evidence_sha256",
            "runtime_permil_evidence_kind",
            "runtime_permil_evidence_sha256",
            "supports",
            "used_support_ids",
        }
        _exact(payload, fields, "audition support runtime")
        raw_supports = payload["supports"]
        if not isinstance(raw_supports, Mapping):
            raise ValueError("supports must be an object keyed by support_id")
        raw_used = payload["used_support_ids"]
        if not isinstance(raw_used, list):
            raise ValueError("used_support_ids must be a list")
        return cls(
            schema_version=_integer(payload["schema_version"], "schema_version"),
            run_id=_text(payload["run_id"], "run_id"),
            step_context_id=_text(payload["step_context_id"], "step_context_id"),
            step_context_digest=_sha256(
                payload["step_context_digest"], "step_context_digest"
            ),
            session_transition_id=_text(
                payload["session_transition_id"], "session_transition_id"
            ),
            zone_checkpoint_digest=_sha256(
                payload["zone_checkpoint_digest"], "zone_checkpoint_digest"
            ),
            round_number=_integer(payload["round_number"], "round_number", minimum=1),
            hand_hold_count=_integer(payload["hand_hold_count"], "hand_hold_count"),
            hand_hold_evidence_kind=_text(
                payload["hand_hold_evidence_kind"], "hand_hold_evidence_kind"
            ),
            hand_hold_evidence_sha256=_sha256(
                payload["hand_hold_evidence_sha256"],
                "hand_hold_evidence_sha256",
            ),
            runtime_permil_evidence_kind=_text(
                payload["runtime_permil_evidence_kind"],
                "runtime_permil_evidence_kind",
            ),
            runtime_permil_evidence_sha256=_sha256(
                payload["runtime_permil_evidence_sha256"],
                "runtime_permil_evidence_sha256",
            ),
            supports=tuple(
                SupportUpgradeRuntimeInput.from_keyed_dict(support_id, value)
                for support_id, value in raw_supports.items()
            ),  # type: ignore[arg-type]
            used_support_ids=tuple(raw_used),  # type: ignore[arg-type]
        )


def load_audition_support_runtime(
    path: Path,
) -> AuditionSupportRuntimeArtifact | None:
    target = Path(path)
    if not target.exists():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("audition support runtime root must be an object")
    return AuditionSupportRuntimeArtifact.from_dict(payload)


def build_audition_support_runtime_artifact(
    *,
    session,
    checkpoint,
    hand_hold_audit,
    runtime_evidence,
) -> AuditionSupportRuntimeArtifact:
    """Bind independently decoded support values to one effective checkpoint."""

    # Local imports avoid coupling the pure runtime value to acquisition code.
    from .audition_hand_hold_audit import AuditionHandHoldAudit
    from .audition_support_runtime_evidence import (
        RUNTIME_PERMIL_EVIDENCE_KIND,
        AuditionSupportRuntimeEvidence,
    )
    from .audition_zone_checkpoint import AuditionZoneCheckpoint
    from .exam_session import ExamSession, SESSION_SCHEMA_VERSION

    if not isinstance(session, ExamSession) or session.schema_version != SESSION_SCHEMA_VERSION:
        raise ValueError("a schema-v3 ExamSession is required")
    if not isinstance(checkpoint, AuditionZoneCheckpoint):
        raise TypeError("checkpoint must be AuditionZoneCheckpoint")
    if not isinstance(hand_hold_audit, AuditionHandHoldAudit):
        raise TypeError("hand_hold_audit must be AuditionHandHoldAudit")
    if not isinstance(runtime_evidence, AuditionSupportRuntimeEvidence):
        raise TypeError("runtime_evidence must be AuditionSupportRuntimeEvidence")
    binding = session.preflight_binding
    if binding is None or session.transition_id is None:
        raise ValueError("ExamSession is missing live binding")
    checkpoint_digest = checkpoint.digest()
    expected_checkpoint_binding = (
        session.run_id,
        session.produce_id,
        session.step_type,
        session.stage_number,
        binding.step_context_id,
        binding.step_context_digest,
        binding.deck_snapshot_digest,
        session.transition_id,
        session.transition_id,
    )
    actual_checkpoint_binding = (
        checkpoint.binding.run_id,
        checkpoint.binding.produce_id,
        checkpoint.binding.step_type,
        checkpoint.binding.stage_number,
        checkpoint.binding.step_context_id,
        checkpoint.binding.step_context_digest,
        checkpoint.binding.deck_snapshot_digest,
        checkpoint.transition_id,
        checkpoint.session_transition_id,
    )
    if actual_checkpoint_binding != expected_checkpoint_binding:
        raise ValueError("effective checkpoint differs from ExamSession")
    expected_runtime_binding = (
        session.run_id,
        binding.step_context_id,
        binding.step_context_digest,
        session.transition_id,
        session.logic_state.round_number,
    )
    audit_binding = (
        hand_hold_audit.run_id,
        hand_hold_audit.step_context_id,
        hand_hold_audit.step_context_digest,
        hand_hold_audit.session_transition_id,
        hand_hold_audit.round_number,
    )
    if (
        audit_binding != expected_runtime_binding
        or hand_hold_audit.hand_hold_count != 0
    ):
        raise ValueError("HandHold audit differs from current ExamSession")
    evidence_binding = (
        runtime_evidence.run_id,
        runtime_evidence.loadout_snapshot_digest,
        runtime_evidence.session_transition_id,
        runtime_evidence.zone_checkpoint_digest,
        runtime_evidence.current_turn,
        runtime_evidence.remain_turn,
    )
    if evidence_binding != (
        session.run_id,
        binding.loadout_snapshot_digest,
        session.transition_id,
        checkpoint_digest,
        session.logic_state.round_number,
        session.logic_state.turns_remaining,
    ):
        raise ValueError("support runtime evidence differs from current checkpoint")
    evidence_hand = tuple(
        (value.card_id, value.base_upgrade, value.effective_upgrade)
        for value in runtime_evidence.hand
    )
    checkpoint_hand = tuple(
        (effective.card_id, base.upgrade, effective.upgrade)
        for base, effective in zip(
            checkpoint.hand_base_cards,
            checkpoint.zones.hand,
            strict=True,
        )
    )
    if evidence_hand != checkpoint_hand:
        raise ValueError("support runtime Hand differs from effective checkpoint")
    supports = tuple(
        SupportUpgradeRuntimeInput(
            support_id=value.support_id,
            lesson_type=value.lesson_type,
            runtime_permil=value.runtime_permil,
            loadout_order=value.loadout_order,
            card_search_id=value.card_search_id,
        )
        for value in runtime_evidence.support_permils
    )
    return AuditionSupportRuntimeArtifact(
        schema_version=AUDITION_SUPPORT_RUNTIME_SCHEMA_VERSION,
        run_id=session.run_id,
        step_context_id=binding.step_context_id,
        step_context_digest=binding.step_context_digest,
        session_transition_id=session.transition_id,
        zone_checkpoint_digest=checkpoint_digest,
        round_number=session.logic_state.round_number,
        hand_hold_count=hand_hold_audit.hand_hold_count,
        hand_hold_evidence_kind=HAND_HOLD_EVIDENCE_KIND,
        hand_hold_evidence_sha256=hand_hold_audit.digest(),
        runtime_permil_evidence_kind=RUNTIME_PERMIL_EVIDENCE_KIND,
        runtime_permil_evidence_sha256=runtime_evidence.digest(),
        supports=supports,
        used_support_ids=runtime_evidence.turn_use_support_ids,
    )


def save_audition_support_runtime(
    artifact: AuditionSupportRuntimeArtifact, path: Path
) -> Path:
    if not isinstance(artifact, AuditionSupportRuntimeArtifact):
        raise TypeError("artifact must be AuditionSupportRuntimeArtifact")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(artifact.canonical_json() + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


__all__ = [
    "AUDITION_SUPPORT_RUNTIME_SCHEMA_VERSION",
    "HAND_HOLD_EVIDENCE_KIND",
    "LESSON_TYPE_UNKNOWN",
    "AuditionSupportRuntimeArtifact",
    "SUPPORTED_SUPPORT_LESSON_TYPES",
    "SUPPORT_RUNTIME_FILENAME",
    "SupportUpgradeRuntimeInput",
    "build_audition_support_runtime_artifact",
    "load_audition_support_runtime",
    "save_audition_support_runtime",
    "support_lesson_type_matches",
]
