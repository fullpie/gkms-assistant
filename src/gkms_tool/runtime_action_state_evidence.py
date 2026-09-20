"""Pure adapter from a recorder transition state into Plan2 evidence.

The telemetry file is provenance only.  Its path or byte offset is never used
as the ``source_sha256`` of the projected evidence; that digest covers the
canonical 89-field ``state_after`` JSON supplied by the recorder.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
    LocalSaveExamState,
    parse_local_save_exam_state,
)


@dataclass(frozen=True, slots=True)
class RuntimeActionStateProvenance:
    telemetry_path: str
    telemetry_end_offset: int
    canonical_state_sha256: str
    canonical_state_size: int
    zones_digest: str
    base_source_sha256: str
    capture_kind: str = "recorder-transition"

    def __post_init__(self) -> None:
        if self.capture_kind not in {"recorder-transition", "runtime-command-snapshot"}:
            raise ValueError("unsupported native state capture kind")
        if not isinstance(self.telemetry_path, str) or not self.telemetry_path:
            raise ValueError("telemetry_path must be non-empty text")
        if (
            isinstance(self.telemetry_end_offset, bool)
            or not isinstance(self.telemetry_end_offset, int)
            or self.telemetry_end_offset < 0
        ):
            raise ValueError("telemetry_end_offset must be a non-negative integer")
        for name in (
            "canonical_state_sha256",
            "zones_digest",
            "base_source_sha256",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 digest")
        if self.canonical_state_size < 1:
            raise ValueError("canonical_state_size must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "telemetry_path": self.telemetry_path,
            "telemetry_end_offset": self.telemetry_end_offset,
            "canonical_state_sha256": self.canonical_state_sha256,
            "canonical_state_size": self.canonical_state_size,
            "zones_digest": self.zones_digest,
            "base_source_sha256": self.base_source_sha256,
            "digest_kind": (
                "canonical-recorder-transition-state-after-json"
                if self.capture_kind == "recorder-transition"
                else "canonical-runtime-command-exam-save-json"
            ),
        }


_STAGE_IDENTITY_FIELDS = (
    "character_id",
    "setting_id",
    "exam_type",
    "step_type_value",
    "limit_turn",
    "max_stamina",
    "vocal_bonus_permille",
    "dance_bonus_permille",
    "visual_bonus_permille",
)


def _canonical_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_stage_identity(
    base: LocalSaveExamState,
    state_after: LocalSaveExamState,
) -> None:
    mismatches = tuple(
        name
        for name in _STAGE_IDENTITY_FIELDS
        if getattr(base, name) != getattr(state_after, name)
    )
    if mismatches:
        raise ValueError(
            "recorder state_after stage identity differs: " + ",".join(mismatches)
        )
    old_schedule = base.turn_parameter_types
    new_schedule = state_after.turn_parameter_types
    extra_delta = state_after.extra_turn - base.extra_turn
    # The native serializer supports a base table or an expanded extra-turn
    # table; lessons use an empty table. Parsing already validates those
    # shapes. Compare their shared observed prefix, not their storage length.
    common_length = min(len(old_schedule), len(new_schedule))
    if (new_schedule[:common_length] != old_schedule[:common_length] or
            bool(new_schedule) != bool(old_schedule) or extra_delta < 0):
        raise ValueError("recorder state_after turn schedule is not a matching extra-turn extension")


def adapt_runtime_action_state_evidence(
    base: AuditionLocalSaveStateEvidence,
    state_after: Mapping[str, object],
    *,
    telemetry_path: str | Path,
    telemetry_end_offset: int,
    capture_kind: str = "recorder-transition",
) -> tuple[AuditionLocalSaveStateEvidence, RuntimeActionStateProvenance]:
    """Parse one complete recorder ``transition.state_after`` without I/O."""

    if not isinstance(base, AuditionLocalSaveStateEvidence):
        raise TypeError("base must be AuditionLocalSaveStateEvidence")
    if not isinstance(state_after, Mapping):
        raise TypeError("state_after must be a complete recorder state mapping")
    path_text = str(telemetry_path)
    if not path_text:
        raise ValueError("telemetry_path must be non-empty")
    if (
        isinstance(telemetry_end_offset, bool)
        or not isinstance(telemetry_end_offset, int)
        or telemetry_end_offset < 0
    ):
        raise ValueError("telemetry_end_offset must be a non-negative integer")

    parsed = parse_local_save_exam_state(state_after)
    _require_stage_identity(base.state, parsed)
    state_bytes = _canonical_bytes(state_after)
    state_sha256 = hashlib.sha256(state_bytes).hexdigest()
    zones_bytes = _canonical_bytes(parsed.zones.to_dict())
    zones_digest = hashlib.sha256(zones_bytes).hexdigest()
    evidence = replace(
        base,
        zone_checkpoint_digest=zones_digest,
        source_sha256=state_sha256,
        source_size=len(state_bytes),
        state=parsed,
    )
    provenance = RuntimeActionStateProvenance(
        telemetry_path=path_text,
        telemetry_end_offset=telemetry_end_offset,
        canonical_state_sha256=state_sha256,
        canonical_state_size=len(state_bytes),
        zones_digest=zones_digest,
        base_source_sha256=base.source_sha256,
        capture_kind=capture_kind,
    )
    return evidence, provenance


__all__ = [
    "RuntimeActionStateProvenance",
    "adapt_runtime_action_state_evidence",
]
