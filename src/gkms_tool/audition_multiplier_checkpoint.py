"""Durable direct evidence for all three live audition multipliers.

The server supplies three runtime values which cannot, in general, be
recovered from one rounded multiplier shown by the HUD.  This module avoids
guessing those values.  Instead it accumulates dual-stable Maa background
captures from Vocal, Dance, and Visual turns in the same run/stage.  Once all
three colours have been observed, the exact displayed permils are sufficient
for the full remaining-turn horizon even if the underlying E/Vote/Star
decomposition remains unknown.

No recognition or input control lives here.  Callers supply already decoded
ring frames together with the controller capture metadata.  Every load
re-hashes the PNG files, and every append is compare-and-swap bound to the
previous canonical checkpoint digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .audition_horizon import LESSON_DANCE, LESSON_VISUAL, LESSON_VOCAL
from .audition_rules import AuditionRules
from .exam_session import SESSION_SCHEMA_VERSION, ExamSession
from .run_identity import RunIdentity

if TYPE_CHECKING:
    from .audition_turn_schedule import RingFrameEvidence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDITION_MULTIPLIER_CHECKPOINT_PATH = (
    PROJECT_ROOT / "var" / "audition_multiplier_checkpoint.json"
)
AUDITION_MULTIPLIER_CHECKPOINT_SCHEMA_VERSION = 1
EXPECTED_CAPTURE_METHOD = "MaaFramework(PrintWindow-background)"
LESSON_TYPES = (LESSON_VOCAL, LESSON_DANCE, LESSON_VISUAL)
_LESSON_TYPE_SET = frozenset(LESSON_TYPES)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _strict_int(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _strict_float(value: object, label: str, *, minimum: float = 0.0) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or float(value) < minimum
    ):
        raise ValueError(f"{label} must be a number >= {minimum}")
    return float(value)


def _strict_sha256(value: object, label: str) -> str:
    text = _strict_text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _exact_fields(
    payload: Mapping[str, object], expected: Iterable[str], label: str
) -> None:
    expected_set = set(expected)
    actual = set(payload)
    if actual != expected_set:
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(expected_set - actual)} "
            f"unknown={sorted(actual - expected_set)}"
        )


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.resolve().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _session_digest(session: ExamSession) -> str:
    return hashlib.sha256(
        _canonical_json(session.to_dict()).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditionMultiplierBinding:
    run_id: str
    idol_card_id: str
    produce_id: str
    step_type: str
    stage_number: int
    battle_config_id: str
    step_context_id: str
    step_context_digest: str
    attributes: tuple[int, int, int]
    attribute_evidence_sha256: str

    def __post_init__(self) -> None:
        for label in (
            "run_id",
            "idol_card_id",
            "produce_id",
            "step_type",
            "battle_config_id",
            "step_context_id",
        ):
            _strict_text(getattr(self, label), label)
        _strict_int(self.stage_number, "stage_number", minimum=1)
        _strict_sha256(self.step_context_digest, "step_context_digest")
        if (
            not isinstance(self.attributes, tuple)
            or len(self.attributes) != 3
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
                for value in self.attributes
            )
        ):
            raise ValueError("attributes must be a non-negative Vo/Da/Vi tuple")
        _strict_sha256(
            self.attribute_evidence_sha256, "attribute_evidence_sha256"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "idol_card_id": self.idol_card_id,
            "produce_id": self.produce_id,
            "step_type": self.step_type,
            "stage_number": self.stage_number,
            "battle_config_id": self.battle_config_id,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "attributes": list(self.attributes),
            "attribute_evidence_sha256": self.attribute_evidence_sha256,
        }

    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict()).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionMultiplierBinding":
        fields = {
            "run_id",
            "idol_card_id",
            "produce_id",
            "step_type",
            "stage_number",
            "battle_config_id",
            "step_context_id",
            "step_context_digest",
            "attributes",
            "attribute_evidence_sha256",
        }
        if not isinstance(payload, Mapping):
            raise ValueError("multiplier binding must be an object")
        _exact_fields(payload, fields, "multiplier binding")
        attributes = payload["attributes"]
        if not isinstance(attributes, list) or len(attributes) != 3:
            raise ValueError("binding attributes must be a three-item list")
        return cls(
            run_id=_strict_text(payload["run_id"], "run_id"),
            idol_card_id=_strict_text(payload["idol_card_id"], "idol_card_id"),
            produce_id=_strict_text(payload["produce_id"], "produce_id"),
            step_type=_strict_text(payload["step_type"], "step_type"),
            stage_number=_strict_int(
                payload["stage_number"], "stage_number", minimum=1
            ),
            battle_config_id=_strict_text(
                payload["battle_config_id"], "battle_config_id"
            ),
            step_context_id=_strict_text(
                payload["step_context_id"], "step_context_id"
            ),
            step_context_digest=_strict_sha256(
                payload["step_context_digest"], "step_context_digest"
            ),
            attributes=tuple(
                _strict_int(value, "attribute") for value in attributes
            ),
            attribute_evidence_sha256=_strict_sha256(
                payload["attribute_evidence_sha256"],
                "attribute_evidence_sha256",
            ),
        )


@dataclass(frozen=True, slots=True)
class AuditionMultiplierCapture:
    capture_path: str
    capture_sha256: str
    capture_method: str
    hwnd: int
    pid: int
    width: int
    height: int
    timestamp: float
    ring_frame_json: str
    ring_frame_sha256: str

    def __post_init__(self) -> None:
        _strict_text(self.capture_path, "capture_path")
        _strict_sha256(self.capture_sha256, "capture_sha256")
        if self.capture_method != EXPECTED_CAPTURE_METHOD:
            raise ValueError("capture did not use Maa background PrintWindow")
        for label in ("hwnd", "pid", "width", "height"):
            _strict_int(getattr(self, label), label, minimum=1)
        _strict_float(self.timestamp, "timestamp", minimum=0.0)
        _strict_text(self.ring_frame_json, "ring_frame_json")
        _strict_sha256(self.ring_frame_sha256, "ring_frame_sha256")
        if hashlib.sha256(self.ring_frame_json.encode("utf-8")).hexdigest() != (
            self.ring_frame_sha256
        ):
            raise ValueError("ring_frame_json digest mismatch")
        payload = json.loads(self.ring_frame_json)
        if not isinstance(payload, Mapping):
            raise ValueError("ring_frame_json must contain an object")
        if (
            payload.get("capture_path") != self.capture_path
            or payload.get("capture_sha256") != self.capture_sha256
            or payload.get("width") != self.width
            or payload.get("height") != self.height
        ):
            raise ValueError("ring frame payload differs from capture metadata")

    def to_dict(self) -> dict[str, object]:
        return {
            "capture_path": self.capture_path,
            "capture_sha256": self.capture_sha256,
            "capture_method": self.capture_method,
            "hwnd": self.hwnd,
            "pid": self.pid,
            "width": self.width,
            "height": self.height,
            "timestamp": self.timestamp,
            "ring_frame_json": self.ring_frame_json,
            "ring_frame_sha256": self.ring_frame_sha256,
        }

    def validate_file(self) -> None:
        path = Path(self.capture_path).resolve()
        if not path.is_file():
            raise ValueError(f"multiplier capture is missing: {path}")
        if _sha256_file(path) != self.capture_sha256:
            raise ValueError(f"multiplier capture hash changed: {path}")

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionMultiplierCapture":
        fields = {
            "capture_path",
            "capture_sha256",
            "capture_method",
            "hwnd",
            "pid",
            "width",
            "height",
            "timestamp",
            "ring_frame_json",
            "ring_frame_sha256",
        }
        if not isinstance(payload, Mapping):
            raise ValueError("multiplier capture must be an object")
        _exact_fields(payload, fields, "multiplier capture")
        return cls(
            capture_path=_strict_text(payload["capture_path"], "capture_path"),
            capture_sha256=_strict_sha256(
                payload["capture_sha256"], "capture_sha256"
            ),
            capture_method=_strict_text(
                payload["capture_method"], "capture_method"
            ),
            hwnd=_strict_int(payload["hwnd"], "hwnd", minimum=1),
            pid=_strict_int(payload["pid"], "pid", minimum=1),
            width=_strict_int(payload["width"], "width", minimum=1),
            height=_strict_int(payload["height"], "height", minimum=1),
            timestamp=_strict_float(payload["timestamp"], "timestamp"),
            ring_frame_json=_strict_text(
                payload["ring_frame_json"], "ring_frame_json"
            ),
            ring_frame_sha256=_strict_sha256(
                payload["ring_frame_sha256"], "ring_frame_sha256"
            ),
        )


@dataclass(frozen=True, slots=True)
class AuditionMultiplierObservation:
    lesson_type: str
    score_multiplier_permille: int
    round_number: int
    turns_remaining: int
    remaining_types: tuple[str, ...]
    session_transition_id: str
    exam_session_sha256: str
    captures: tuple[AuditionMultiplierCapture, AuditionMultiplierCapture]

    def __post_init__(self) -> None:
        if self.lesson_type not in _LESSON_TYPE_SET:
            raise ValueError(f"unsupported lesson_type: {self.lesson_type}")
        _strict_int(
            self.score_multiplier_permille,
            "score_multiplier_permille",
            minimum=1,
        )
        _strict_int(self.round_number, "round_number", minimum=1)
        _strict_int(self.turns_remaining, "turns_remaining", minimum=1)
        if (
            not isinstance(self.remaining_types, tuple)
            or not self.remaining_types
            or self.remaining_types[0] != self.lesson_type
            or any(value not in _LESSON_TYPE_SET for value in self.remaining_types)
        ):
            raise ValueError("remaining_types must start with lesson_type")
        _strict_text(self.session_transition_id, "session_transition_id")
        _strict_sha256(self.exam_session_sha256, "exam_session_sha256")
        if (
            not isinstance(self.captures, tuple)
            or len(self.captures) != 2
            or any(
                not isinstance(value, AuditionMultiplierCapture)
                for value in self.captures
            )
        ):
            raise ValueError("captures must contain exactly two capture values")
        first, second = self.captures
        if first.capture_sha256 == second.capture_sha256:
            raise ValueError("multiplier observations require distinct captures")
        if (first.hwnd, first.pid) != (second.hwnd, second.pid):
            raise ValueError("multiplier captures came from different windows")
        if first.timestamp >= second.timestamp:
            raise ValueError("multiplier captures must be strictly ordered")
        for capture in self.captures:
            payload = json.loads(capture.ring_frame_json)
            slots = payload.get("slots")
            current_slot_index = payload.get("current_slot_index")
            if not isinstance(slots, list) or not isinstance(current_slot_index, int):
                raise ValueError("stored ring frame slots are invalid")
            remaining = tuple(
                slot.get("lesson_type")
                for slot in slots[current_slot_index:]
                if isinstance(slot, Mapping) and slot.get("lesson_type") is not None
            )
            if (
                payload.get("turns_remaining") != self.turns_remaining
                or payload.get("score_multiplier_permille")
                != self.score_multiplier_permille
                or remaining != self.remaining_types
            ):
                raise ValueError("stored ring frame differs from multiplier observation")

    def to_dict(self) -> dict[str, object]:
        return {
            "lesson_type": self.lesson_type,
            "score_multiplier_permille": self.score_multiplier_permille,
            "round_number": self.round_number,
            "turns_remaining": self.turns_remaining,
            "remaining_types": list(self.remaining_types),
            "session_transition_id": self.session_transition_id,
            "exam_session_sha256": self.exam_session_sha256,
            "captures": [capture.to_dict() for capture in self.captures],
        }

    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.to_dict()).encode("utf-8")
        ).hexdigest()

    def validate_files(self) -> None:
        for capture in self.captures:
            capture.validate_file()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionMultiplierObservation":
        fields = {
            "lesson_type",
            "score_multiplier_permille",
            "round_number",
            "turns_remaining",
            "remaining_types",
            "session_transition_id",
            "exam_session_sha256",
            "captures",
        }
        if not isinstance(payload, Mapping):
            raise ValueError("multiplier observation must be an object")
        _exact_fields(payload, fields, "multiplier observation")
        remaining = payload["remaining_types"]
        captures = payload["captures"]
        if not isinstance(remaining, list):
            raise ValueError("remaining_types must be a list")
        if not isinstance(captures, list) or len(captures) != 2:
            raise ValueError("captures must be a two-item list")
        return cls(
            lesson_type=_strict_text(payload["lesson_type"], "lesson_type"),
            score_multiplier_permille=_strict_int(
                payload["score_multiplier_permille"],
                "score_multiplier_permille",
                minimum=1,
            ),
            round_number=_strict_int(
                payload["round_number"], "round_number", minimum=1
            ),
            turns_remaining=_strict_int(
                payload["turns_remaining"], "turns_remaining", minimum=1
            ),
            remaining_types=tuple(
                _strict_text(value, "remaining_type") for value in remaining
            ),
            session_transition_id=_strict_text(
                payload["session_transition_id"], "session_transition_id"
            ),
            exam_session_sha256=_strict_sha256(
                payload["exam_session_sha256"], "exam_session_sha256"
            ),
            captures=tuple(
                AuditionMultiplierCapture.from_dict(value) for value in captures
            ),
        )


@dataclass(frozen=True, slots=True)
class AuditionMultiplierCheckpoint:
    schema_version: int
    binding: AuditionMultiplierBinding
    target_hwnd: int
    target_pid: int
    lineage_depth: int
    previous_checkpoint_sha256: str | None
    observations: tuple[AuditionMultiplierObservation, ...]

    def __post_init__(self) -> None:
        if self.schema_version != AUDITION_MULTIPLIER_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("unsupported audition multiplier checkpoint schema")
        if not isinstance(self.binding, AuditionMultiplierBinding):
            raise TypeError("binding must be AuditionMultiplierBinding")
        _strict_int(self.target_hwnd, "target_hwnd", minimum=1)
        _strict_int(self.target_pid, "target_pid", minimum=1)
        _strict_int(self.lineage_depth, "lineage_depth")
        if self.previous_checkpoint_sha256 is not None:
            _strict_sha256(
                self.previous_checkpoint_sha256, "previous_checkpoint_sha256"
            )
        if (
            not isinstance(self.observations, tuple)
            or not self.observations
            or any(
                not isinstance(value, AuditionMultiplierObservation)
                for value in self.observations
            )
        ):
            raise ValueError("observations must be a non-empty tuple")
        if self.lineage_depth != len(self.observations) - 1:
            raise ValueError("lineage_depth does not match observation count")
        if self.lineage_depth == 0 and self.previous_checkpoint_sha256 is not None:
            raise ValueError("initial checkpoint cannot have a previous digest")
        if self.lineage_depth > 0 and self.previous_checkpoint_sha256 is None:
            raise ValueError("appended checkpoint requires a previous digest")
        seen: set[str] = set()
        seen_captures: set[str] = set()
        previous_round = 0
        previous_turns: int | None = None
        for observation in self.observations:
            if observation.lesson_type in seen:
                raise ValueError("each multiplier colour may be committed only once")
            seen.add(observation.lesson_type)
            for capture in observation.captures:
                if capture.capture_sha256 in seen_captures:
                    raise ValueError("a capture cannot prove more than one multiplier colour")
                seen_captures.add(capture.capture_sha256)
            if (observation.captures[0].hwnd, observation.captures[0].pid) != (
                self.target_hwnd,
                self.target_pid,
            ):
                raise ValueError("observation target differs from checkpoint target")
            if observation.round_number <= previous_round:
                raise ValueError("multiplier observations must advance round_number")
            if previous_turns is not None and observation.turns_remaining >= previous_turns:
                raise ValueError("multiplier observations must reduce turns_remaining")
            previous_round = observation.round_number
            previous_turns = observation.turns_remaining

    @property
    def complete(self) -> bool:
        return {value.lesson_type for value in self.observations} == _LESSON_TYPE_SET

    @property
    def final_multipliers(self) -> tuple[tuple[str, int], ...]:
        values = {
            observation.lesson_type: observation.score_multiplier_permille
            for observation in self.observations
        }
        return tuple(
            (lesson_type, values[lesson_type])
            for lesson_type in LESSON_TYPES
            if lesson_type in values
        )

    @property
    def observation_digests(self) -> tuple[str, ...]:
        return tuple(value.digest() for value in self.observations)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "binding": self.binding.to_dict(),
            "target_hwnd": self.target_hwnd,
            "target_pid": self.target_pid,
            "lineage_depth": self.lineage_depth,
            "previous_checkpoint_sha256": self.previous_checkpoint_sha256,
            "observations": [value.to_dict() for value in self.observations],
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def validate_files(self) -> None:
        for observation in self.observations:
            observation.validate_files()

    def validate_for(
        self,
        *,
        run_id: str,
        rules: AuditionRules,
        attributes: tuple[int, int, int],
        attribute_evidence_path: Path,
        current_step_context_id: str,
        current_step_context_digest: str,
    ) -> None:
        if not isinstance(rules, AuditionRules):
            raise TypeError("rules must be AuditionRules")
        context_id = _strict_text(
            current_step_context_id, "current_step_context_id"
        )
        context_digest = _strict_sha256(
            current_step_context_digest, "current_step_context_digest"
        )
        binding = self.binding
        expected = (
            run_id,
            rules.idol_card_id,
            rules.produce_id,
            rules.step_type,
            rules.number,
            rules.battle_config_id,
        )
        actual = (
            binding.run_id,
            binding.idol_card_id,
            binding.produce_id,
            binding.step_type,
            binding.stage_number,
            binding.battle_config_id,
        )
        if actual != expected:
            raise ValueError("multiplier checkpoint run/stage binding mismatch")
        if (
            binding.step_context_id != context_id
            or binding.step_context_digest != context_digest
        ):
            raise ValueError("multiplier checkpoint step-context binding mismatch")
        if binding.attributes != attributes:
            raise ValueError("multiplier checkpoint attribute values changed")
        path = Path(attribute_evidence_path).resolve()
        if not path.is_file():
            raise ValueError(f"attribute evidence is missing: {path}")
        if _sha256_file(path) != binding.attribute_evidence_sha256:
            raise ValueError("multiplier checkpoint attribute evidence hash changed")
        self.validate_files()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionMultiplierCheckpoint":
        fields = {
            "schema_version",
            "binding",
            "target_hwnd",
            "target_pid",
            "lineage_depth",
            "previous_checkpoint_sha256",
            "observations",
        }
        if not isinstance(payload, Mapping):
            raise ValueError("multiplier checkpoint must be an object")
        _exact_fields(payload, fields, "multiplier checkpoint")
        binding = payload["binding"]
        observations = payload["observations"]
        if not isinstance(binding, Mapping):
            raise ValueError("checkpoint binding must be an object")
        if not isinstance(observations, list):
            raise ValueError("checkpoint observations must be a list")
        previous = payload["previous_checkpoint_sha256"]
        return cls(
            schema_version=_strict_int(payload["schema_version"], "schema_version"),
            binding=AuditionMultiplierBinding.from_dict(binding),
            target_hwnd=_strict_int(
                payload["target_hwnd"], "target_hwnd", minimum=1
            ),
            target_pid=_strict_int(payload["target_pid"], "target_pid", minimum=1),
            lineage_depth=_strict_int(payload["lineage_depth"], "lineage_depth"),
            previous_checkpoint_sha256=(
                None
                if previous is None
                else _strict_sha256(previous, "previous_checkpoint_sha256")
            ),
            observations=tuple(
                AuditionMultiplierObservation.from_dict(value)
                for value in observations
            ),
        )


def build_multiplier_binding(
    *,
    identity: RunIdentity,
    session: ExamSession,
    rules: AuditionRules,
    attributes: tuple[int, int, int],
    attribute_evidence_path: Path,
) -> AuditionMultiplierBinding:
    identity.validate()
    if session.schema_version != SESSION_SCHEMA_VERSION:
        raise ValueError("multiplier evidence requires a schema-v3 ExamSession")
    if not session.matches_run(
        run_id=identity.run_id,
        idol_card_id=identity.idol_card_id,
        character_id=identity.character_id,
        produce_id=identity.produce_id,
    ):
        raise ValueError("ExamSession does not match the active run")
    if session.step_type != rules.step_type or session.stage_number != rules.number:
        raise ValueError("ExamSession does not match audition rules")
    if rules.idol_card_id != identity.idol_card_id or rules.produce_id != identity.produce_id:
        raise ValueError("audition rules do not match the active run")
    assert session.preflight_binding is not None
    evidence_path = Path(attribute_evidence_path).resolve()
    if not evidence_path.is_file():
        raise ValueError(f"attribute evidence is missing: {evidence_path}")
    return AuditionMultiplierBinding(
        run_id=identity.run_id,
        idol_card_id=identity.idol_card_id,
        produce_id=identity.produce_id,
        step_type=rules.step_type,
        stage_number=rules.number,
        battle_config_id=rules.battle_config_id,
        step_context_id=session.preflight_binding.step_context_id,
        step_context_digest=session.preflight_binding.step_context_digest,
        attributes=attributes,
        attribute_evidence_sha256=_sha256_file(evidence_path),
    )


def _ring_frame_digest(frame: "RingFrameEvidence") -> str:
    return hashlib.sha256(
        _canonical_json(frame.to_dict()).encode("utf-8")
    ).hexdigest()


def _capture_from(
    metadata: Mapping[str, Any], frame: "RingFrameEvidence"
) -> AuditionMultiplierCapture:
    path = Path(_strict_text(metadata.get("png_path"), "png_path")).resolve()
    if str(path) != str(Path(frame.capture_path).resolve()):
        raise ValueError("controller capture path differs from decoded ring frame")
    if not path.is_file():
        raise ValueError(f"controller capture is missing: {path}")
    file_hash = _sha256_file(path)
    if file_hash != frame.capture_sha256:
        raise ValueError("decoded ring frame hash differs from controller capture")
    if int(metadata.get("width", 0)) != frame.width or int(
        metadata.get("height", 0)
    ) != frame.height:
        raise ValueError("controller capture dimensions differ from ring frame")
    ring_frame_json = _canonical_json(frame.to_dict())
    return AuditionMultiplierCapture(
        capture_path=str(path),
        capture_sha256=file_hash,
        capture_method=_strict_text(
            metadata.get("capture_method"), "capture_method"
        ),
        hwnd=_strict_int(metadata.get("hwnd"), "hwnd", minimum=1),
        pid=_strict_int(metadata.get("pid"), "pid", minimum=1),
        width=frame.width,
        height=frame.height,
        timestamp=_strict_float(metadata.get("timestamp"), "timestamp"),
        ring_frame_json=ring_frame_json,
        ring_frame_sha256=hashlib.sha256(
            ring_frame_json.encode("utf-8")
        ).hexdigest(),
    )


def build_multiplier_observation(
    *,
    session: ExamSession,
    rules: AuditionRules,
    ring_frames: tuple["RingFrameEvidence", "RingFrameEvidence"],
    capture_metadata: tuple[Mapping[str, Any], Mapping[str, Any]],
) -> AuditionMultiplierObservation:
    if session.schema_version != SESSION_SCHEMA_VERSION:
        raise ValueError("multiplier evidence requires a schema-v3 ExamSession")
    if session.step_type != rules.step_type or session.stage_number != rules.number:
        raise ValueError("ExamSession stage differs from audition rules")
    if len(ring_frames) != 2 or len(capture_metadata) != 2:
        raise ValueError("dual-stable multiplier evidence requires two frames")
    first, second = ring_frames
    if first.capture_sha256 == second.capture_sha256:
        raise ValueError("dual-stable multiplier evidence must be distinct")
    stable = (
        first.turns_remaining,
        first.score_multiplier_permille,
        first.stamina,
        first.block,
        first.player_score,
        first.current_slot_index,
        first.remaining_types,
    )
    if stable != (
        second.turns_remaining,
        second.score_multiplier_permille,
        second.stamina,
        second.block,
        second.player_score,
        second.current_slot_index,
        second.remaining_types,
    ):
        raise ValueError("multiplier ring frames are not semantically stable")
    state = session.logic_state
    if (
        first.turns_remaining != state.turns_remaining
        or first.score_multiplier_permille != state.score_multiplier_permille
        or first.stamina != state.stamina
        or first.block != state.block
        or first.player_score != state.score
    ):
        raise ValueError("ring frame HUD differs from durable ExamSession")
    expected_round = rules.turns - first.turns_remaining + 1
    if first.current_slot_index + 1 != expected_round:
        raise ValueError("ring slot index differs from remaining turn count")
    if state.round_number != expected_round:
        raise ValueError("ExamSession round differs from ring position")
    if not first.remaining_types:
        raise ValueError("ring frame has no remaining lesson types")
    captures = tuple(
        _capture_from(metadata, frame)
        for metadata, frame in zip(capture_metadata, ring_frames, strict=True)
    )
    return AuditionMultiplierObservation(
        lesson_type=first.remaining_types[0],
        score_multiplier_permille=first.score_multiplier_permille,
        round_number=expected_round,
        turns_remaining=first.turns_remaining,
        remaining_types=first.remaining_types,
        session_transition_id=_strict_text(
            session.transition_id, "session.transition_id"
        ),
        exam_session_sha256=_session_digest(session),
        captures=captures,
    )


def initial_multiplier_checkpoint(
    *,
    binding: AuditionMultiplierBinding,
    observation: AuditionMultiplierObservation,
) -> AuditionMultiplierCheckpoint:
    first_capture = observation.captures[0]
    return AuditionMultiplierCheckpoint(
        schema_version=AUDITION_MULTIPLIER_CHECKPOINT_SCHEMA_VERSION,
        binding=binding,
        target_hwnd=first_capture.hwnd,
        target_pid=first_capture.pid,
        lineage_depth=0,
        previous_checkpoint_sha256=None,
        observations=(observation,),
    )


def append_multiplier_observation(
    previous: AuditionMultiplierCheckpoint,
    observation: AuditionMultiplierObservation,
) -> AuditionMultiplierCheckpoint:
    if not isinstance(previous, AuditionMultiplierCheckpoint):
        raise TypeError("previous must be AuditionMultiplierCheckpoint")
    if not isinstance(observation, AuditionMultiplierObservation):
        raise TypeError("observation must be AuditionMultiplierObservation")
    return AuditionMultiplierCheckpoint(
        schema_version=AUDITION_MULTIPLIER_CHECKPOINT_SCHEMA_VERSION,
        binding=previous.binding,
        target_hwnd=previous.target_hwnd,
        target_pid=previous.target_pid,
        lineage_depth=previous.lineage_depth + 1,
        previous_checkpoint_sha256=previous.digest(),
        observations=(*previous.observations, observation),
    )


class AuditionMultiplierCheckpointConflictError(RuntimeError):
    def __init__(self, expected: str | None, actual: str | None) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "audition multiplier checkpoint compare-and-swap failed: "
            f"expected={expected!r}, actual={actual!r}"
        )


def load_multiplier_checkpoint(
    path: Path = DEFAULT_AUDITION_MULTIPLIER_CHECKPOINT_PATH,
    *,
    validate_files: bool = True,
) -> AuditionMultiplierCheckpoint | None:
    selected = Path(path)
    if not selected.is_file():
        return None
    payload = json.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("multiplier checkpoint root must be an object")
    checkpoint = AuditionMultiplierCheckpoint.from_dict(payload)
    if validate_files:
        checkpoint.validate_files()
    return checkpoint


def _atomic_write(path: Path, checkpoint: AuditionMultiplierCheckpoint) -> None:
    selected = Path(path)
    selected.parent.mkdir(parents=True, exist_ok=True)
    temporary = selected.with_name(f".{selected.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(checkpoint.canonical_json() + "\n", encoding="utf-8")
        os.replace(temporary, selected)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_initial_multiplier_checkpoint(
    checkpoint: AuditionMultiplierCheckpoint,
    *,
    path: Path = DEFAULT_AUDITION_MULTIPLIER_CHECKPOINT_PATH,
) -> AuditionMultiplierCheckpoint:
    if checkpoint.lineage_depth != 0:
        raise ValueError("save_initial requires an initial multiplier checkpoint")
    actual = load_multiplier_checkpoint(path)
    if actual is not None:
        raise AuditionMultiplierCheckpointConflictError(None, actual.digest())
    checkpoint.validate_files()
    _atomic_write(Path(path), checkpoint)
    return checkpoint


def commit_multiplier_checkpoint(
    checkpoint: AuditionMultiplierCheckpoint,
    *,
    expected_previous_sha256: str,
    path: Path = DEFAULT_AUDITION_MULTIPLIER_CHECKPOINT_PATH,
) -> AuditionMultiplierCheckpoint:
    _strict_sha256(expected_previous_sha256, "expected_previous_sha256")
    actual = load_multiplier_checkpoint(path)
    actual_digest = None if actual is None else actual.digest()
    if actual_digest != expected_previous_sha256:
        raise AuditionMultiplierCheckpointConflictError(
            expected_previous_sha256, actual_digest
        )
    if checkpoint.previous_checkpoint_sha256 != expected_previous_sha256:
        raise ValueError("checkpoint previous digest differs from CAS expectation")
    assert actual is not None
    if checkpoint.binding != actual.binding:
        raise ValueError("multiplier checkpoint binding changed during append")
    if checkpoint.observations[:-1] != actual.observations:
        raise ValueError("multiplier checkpoint history changed during append")
    if checkpoint.lineage_depth != actual.lineage_depth + 1:
        raise ValueError("multiplier checkpoint lineage is not consecutive")
    checkpoint.validate_files()
    _atomic_write(Path(path), checkpoint)
    return checkpoint


__all__ = [
    "AUDITION_MULTIPLIER_CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_AUDITION_MULTIPLIER_CHECKPOINT_PATH",
    "EXPECTED_CAPTURE_METHOD",
    "LESSON_TYPES",
    "AuditionMultiplierBinding",
    "AuditionMultiplierCapture",
    "AuditionMultiplierCheckpoint",
    "AuditionMultiplierCheckpointConflictError",
    "AuditionMultiplierObservation",
    "append_multiplier_observation",
    "build_multiplier_binding",
    "build_multiplier_observation",
    "commit_multiplier_checkpoint",
    "initial_multiplier_checkpoint",
    "load_multiplier_checkpoint",
    "save_initial_multiplier_checkpoint",
]
