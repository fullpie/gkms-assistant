"""Decode an evidence-bound audition turn schedule from the HUD ring.

The upper-left audition ring is not decorative.  In the pinned Android
v3.2.3 client ``ExamBattleBonusScheduleView.SetTurnParameterType`` stores the
complete ``ProduceParameterType`` list and creates one coloured dot per turn;
``SetTurn`` advances through that list and darkens consumed dots.  The client
uses ``ProduceParameterExtensions.GetColor`` for the dot colour.

This decoder is deliberately conservative:

* two distinct stable screenshots must independently decode to the same ring;
* only the cross-turn-verified nine-slot geometry is accepted;
* consumed and remaining slot counts must agree with the OCR turn counter;
* the currently selected colour must agree with the visible multiplier; and
* every future colour needs independently proven native runtime factors or
  three directly observed final multipliers.

Any failed check returns a blocker and no :class:`VerifiedTurnSchedule`.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import yaml
from PIL import Image

from .audition_horizon import (
    LESSON_DANCE,
    LESSON_VISUAL,
    LESSON_VOCAL,
    VerifiedTurnFrame,
    VerifiedTurnSchedule,
)
from .audition_multiplier_checkpoint import AuditionMultiplierCheckpoint
from .audition_rules import AuditionRules, DEFAULT_MASTER_DIR, MID1
from .exam_screen import ExamScreenState, read_exam_screen_path
from .text_recognizer import PaddleLineRecognizer


CANONICAL_SIZE = (720, 1280)
RING_CENTER = (62, 91)
RING_START_ANGLE_DEGREES = 240.0
RING_SLOT_COUNT = 9
RING_RADIUS_MIN = 34.0
RING_RADIUS_MAX = 46.0
RING_HALF_WEDGE_DEGREES = 13.0
RING_MIN_COLOURED_PIXELS = 60
RING_MAX_CONSUMED_PIXELS = 20
RING_MIN_COLOR_CONFIDENCE = 0.88
SCREEN_MIN_CONFIDENCE = 0.80
SCREEN_MIN_CRITICAL_CONFIDENCE = 0.98
DECODER_VERSION = "dual-stable-screen-ring-v1"
MULTIPLIER_FORMULA_VERSION = "android-v3.2.3-audition-transition-v1"

_COLOR_HUES = {
    LESSON_DANCE: 212.0,
    LESSON_VOCAL: 331.0,
    LESSON_VISUAL: 38.0,
}
_MULTIPLIER_COLUMNS = {
    LESSON_VOCAL: ("vocalPermil", "vocal", "vocal_parameter"),
    LESSON_DANCE: ("dancePermil", "dance", "dance_parameter"),
    LESSON_VISUAL: ("visualPermil", "visual", "visual_parameter"),
}


def _exact_int(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) is not int:
        raise ValueError(f"{label} must be an exact integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be <= {maximum}")
    return value


def _confidence(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{label} must be finite and within [0, 1]")
    return number


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _sha256_text(value: object, label: str) -> str:
    text = _text(value, label)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class AuditionAttributes:
    vocal: int
    dance: int
    visual: int
    evidence_path: Path

    def validate(self) -> None:
        for label in ("vocal", "dance", "visual"):
            _exact_int(getattr(self, label), f"audition attribute {label}", minimum=0)
        if not isinstance(self.evidence_path, Path):
            raise ValueError("audition attribute evidence_path must be a Path")
        if not self.evidence_path.resolve().is_file():
            raise ValueError(
                f"audition attribute evidence is missing: {self.evidence_path}"
            )


@dataclass(frozen=True, slots=True)
class RingSlotObservation:
    slot_index: int
    lesson_type: str | None
    winner_pixels: int
    runner_up_pixels: int
    confidence: float
    counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        validate_ring_slot_observation(self)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RingFrameEvidence:
    capture_path: str
    capture_sha256: str
    width: int
    height: int
    turns_remaining: int
    score_multiplier_permille: int
    stamina: int
    block: int
    player_score: int
    screen_minimum_confidence: float
    screen_field_confidence: tuple[tuple[str, float], ...]
    current_slot_index: int
    slots: tuple[RingSlotObservation, ...]

    def __post_init__(self) -> None:
        validate_ring_frame_evidence(self)

    @property
    def remaining_types(self) -> tuple[str, ...]:
        return tuple(
            slot.lesson_type
            for slot in self.slots[self.current_slot_index :]
            if slot.lesson_type is not None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "slots": [slot.to_dict() for slot in self.slots],
        }


@dataclass(frozen=True, slots=True)
class AuditionMultiplierEvidence:
    attributes: tuple[int, int, int]
    attribute_evidence_path: str
    attribute_evidence_sha256: str
    score_config_id: str
    score_config_sha256: str
    setting_sha256: str
    produce_effect_sha256: str
    base_multipliers: tuple[tuple[str, int], ...]
    effective_bonus_candidates_permille: tuple[int, ...]
    final_multipliers: tuple[tuple[str, int], ...]
    runtime_evidence_kind: str = ""
    runtime_evidence_sha256: str = ""
    runtime_observation_sha256s: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_audition_multiplier_evidence(self)

    @property
    def by_lesson_type(self) -> Mapping[str, int]:
        return dict(self.final_multipliers)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuditionScheduleEvidence:
    run_id: str
    produce_id: str
    step_type: str
    stage_number: int
    battle_config_id: str
    frames: tuple[RingFrameEvidence, RingFrameEvidence]
    multiplier: AuditionMultiplierEvidence

    def __post_init__(self) -> None:
        validate_audition_schedule_evidence(self)

    def canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "decoder_version": DECODER_VERSION,
            "multiplier_formula_version": MULTIPLIER_FORMULA_VERSION,
            "native_proof": {
                "ExamTransitionParam.CreateAuditionTransitionParam": "0x07FD2F40",
                "ExamBattleBonusScheduleView.SetTurnParameterType": "0x07F2EE1C",
                "ExamBattleBonusScheduleView.SetTurn": "0x07F2EF30",
                "ExamBattleBonusScheduleView.SetBonus": "0x07F2F070",
            },
            "context": {
                "run_id": self.run_id,
                "produce_id": self.produce_id,
                "step_type": self.step_type,
                "stage_number": self.stage_number,
                "battle_config_id": self.battle_config_id,
            },
            "frames": [frame.to_dict() for frame in self.frames],
            "multiplier": self.multiplier.to_dict(),
        }

    def digest(self) -> str:
        encoded = json.dumps(
            self.canonical_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditionScheduleBlocker:
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AuditionScheduleDecodeResult:
    schedule: VerifiedTurnSchedule | None
    evidence: AuditionScheduleEvidence | None
    blocker: AuditionScheduleBlocker | None
    ring_frames: tuple[RingFrameEvidence, RingFrameEvidence] | None = None

    @property
    def decision_ready(self) -> bool:
        if (
            self.schedule is None
            or self.evidence is None
            or self.ring_frames is None
            or self.blocker is not None
        ):
            return False
        try:
            validate_verified_turn_schedule_against_ring(
                self.schedule,
                self.evidence,
                self.ring_frames,
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False
        return True


def validate_ring_slot_observation(slot: RingSlotObservation) -> None:
    if not isinstance(slot, RingSlotObservation):
        raise ValueError("ring slot must be RingSlotObservation")
    _exact_int(slot.slot_index, "ring slot index", minimum=0)
    if slot.lesson_type is not None:
        lesson_type = _text(slot.lesson_type, "ring lesson type")
        if lesson_type not in _COLOR_HUES:
            raise ValueError(f"unsupported ring lesson type: {lesson_type}")
    winner_pixels = _exact_int(
        slot.winner_pixels, "ring winner pixels", minimum=0
    )
    runner_up_pixels = _exact_int(
        slot.runner_up_pixels, "ring runner-up pixels", minimum=0
    )
    confidence = _confidence(slot.confidence, "ring slot confidence")
    if not isinstance(slot.counts, tuple):
        raise ValueError("ring slot counts must be a tuple")
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for index, item in enumerate(slot.counts):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"ring slot counts[{index}] is malformed")
        name, count = item
        name = _text(name, f"ring slot counts[{index}].name")
        if name in seen:
            raise ValueError("ring slot counts contain duplicate names")
        seen.add(name)
        counts[name] = _exact_int(
            count, f"ring slot counts[{index}].count", minimum=0
        )
    expected_names = tuple(sorted(_COLOR_HUES))
    if tuple(name for name, _ in slot.counts) != expected_names:
        raise ValueError(
            "ring slot counts must contain the complete canonical color set"
        )
    ordered = sorted(
        ((name, counts[name]) for name in _COLOR_HUES),
        key=lambda item: item[1],
        reverse=True,
    )
    expected_winner_type, expected_winner_pixels = ordered[0]
    expected_runner_up_pixels = ordered[1][1]
    coloured_total = sum(counts.values())
    expected_confidence = (
        expected_winner_pixels / coloured_total if coloured_total else 0.0
    )
    if winner_pixels != expected_winner_pixels:
        raise ValueError("ring winner pixels are not derived from counts")
    if runner_up_pixels != expected_runner_up_pixels:
        raise ValueError("ring runner-up pixels are not derived from counts")
    if confidence != expected_confidence:
        raise ValueError("ring confidence is not derived exactly from counts")
    if slot.lesson_type is not None:
        if slot.lesson_type != expected_winner_type:
            raise ValueError("ring lesson type is not the winner derived from counts")
        if winner_pixels < RING_MIN_COLOURED_PIXELS:
            raise ValueError("remaining ring slot has too few winner pixels")
        if confidence < RING_MIN_COLOR_CONFIDENCE:
            raise ValueError("remaining ring slot confidence is too low")


def validate_ring_frame_evidence(frame: RingFrameEvidence) -> None:
    """Strictly validate a ring frame, including injected decoder results."""

    if not isinstance(frame, RingFrameEvidence):
        raise ValueError("ring frame must be RingFrameEvidence")
    _text(frame.capture_path, "ring capture path")
    _sha256_text(frame.capture_sha256, "ring capture SHA-256")
    width = _exact_int(frame.width, "ring width", minimum=1)
    height = _exact_int(frame.height, "ring height", minimum=1)
    if (width, height) != CANONICAL_SIZE:
        raise ValueError(
            f"ring frame must be canonical {CANONICAL_SIZE[0]}x{CANONICAL_SIZE[1]}"
        )
    _exact_int(frame.turns_remaining, "ring turns_remaining", minimum=1)
    _exact_int(
        frame.score_multiplier_permille,
        "ring score_multiplier_permille",
        minimum=1,
    )
    _exact_int(frame.stamina, "ring stamina", minimum=0)
    _exact_int(frame.block, "ring block", minimum=0)
    _exact_int(frame.player_score, "ring player_score", minimum=0)
    minimum_confidence = _confidence(
        frame.screen_minimum_confidence, "ring minimum confidence"
    )
    if minimum_confidence < SCREEN_MIN_CONFIDENCE:
        raise ValueError("ring minimum confidence is below the safe threshold")
    _exact_int(frame.current_slot_index, "ring current slot index", minimum=0)
    if (
        not isinstance(frame.screen_field_confidence, tuple)
        or not frame.screen_field_confidence
    ):
        raise ValueError("ring field confidence must be a non-empty tuple")
    field_names: set[str] = set()
    field_values: dict[str, float] = {}
    for index, item in enumerate(frame.screen_field_confidence):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"ring field confidence[{index}] is malformed")
        name, confidence = item
        name = _text(name, f"ring field confidence[{index}].name")
        if name in field_names:
            raise ValueError("ring field confidence contains duplicate names")
        field_names.add(name)
        field_values[name] = _confidence(
            confidence, f"ring field confidence[{index}].value"
        )
    for critical in ("turns_remaining", "score_multiplier"):
        if critical not in field_values:
            raise ValueError(f"ring field confidence is missing {critical}")
        if field_values[critical] < SCREEN_MIN_CRITICAL_CONFIDENCE:
            raise ValueError(f"ring critical confidence is too low: {critical}")
    if minimum_confidence > min(field_values.values()):
        raise ValueError("ring minimum confidence exceeds a field confidence")
    if not isinstance(frame.slots, tuple) or len(frame.slots) != RING_SLOT_COUNT:
        raise ValueError(
            f"ring slots must contain exactly {RING_SLOT_COUNT} observations"
        )
    if frame.current_slot_index >= len(frame.slots):
        raise ValueError("ring current slot index is outside the slot sequence")
    if len(frame.slots) - frame.current_slot_index != frame.turns_remaining:
        raise ValueError("ring slot position disagrees with turns_remaining")
    for expected_index, slot in enumerate(frame.slots):
        validate_ring_slot_observation(slot)
        if slot.slot_index != expected_index:
            raise ValueError("ring slot indices must be contiguous and ordered")
        if expected_index < frame.current_slot_index and slot.lesson_type is not None:
            raise ValueError("consumed ring slots must not claim a lesson type")
        if (
            expected_index < frame.current_slot_index
            and slot.winner_pixels > RING_MAX_CONSUMED_PIXELS
        ):
            raise ValueError("consumed ring slot still has too many coloured pixels")
        if expected_index >= frame.current_slot_index and slot.lesson_type is None:
            raise ValueError("remaining ring slots must identify a lesson type")
        if (
            expected_index >= frame.current_slot_index
            and slot.winner_pixels < RING_MIN_COLOURED_PIXELS
        ):
            raise ValueError("remaining ring slot has too few winner pixels")
        if (
            expected_index >= frame.current_slot_index
            and float(slot.confidence) < RING_MIN_COLOR_CONFIDENCE
        ):
            raise ValueError("remaining ring slot confidence is too low")


def _validate_int_pairs(
    values: object, label: str, *, allow_empty: bool = True
) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise ValueError(f"{label} must be a tuple")
    seen: set[str] = set()
    for index, item in enumerate(values):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"{label}[{index}] is malformed")
        name, value = item
        name = _text(name, f"{label}[{index}].name")
        if name in seen:
            raise ValueError(f"{label} contains duplicate names")
        seen.add(name)
        _exact_int(value, f"{label}[{index}].value")


def validate_audition_multiplier_evidence(
    evidence: AuditionMultiplierEvidence,
) -> None:
    if not isinstance(evidence, AuditionMultiplierEvidence):
        raise ValueError("multiplier evidence has the wrong type")
    if not isinstance(evidence.attributes, tuple) or len(evidence.attributes) != 3:
        raise ValueError("multiplier attributes must contain exactly three integers")
    for index, value in enumerate(evidence.attributes):
        _exact_int(value, f"multiplier attributes[{index}]", minimum=0)
    _text(evidence.attribute_evidence_path, "attribute evidence path")
    _sha256_text(evidence.attribute_evidence_sha256, "attribute evidence SHA-256")
    _text(evidence.score_config_id, "score config ID")
    _sha256_text(evidence.score_config_sha256, "score config SHA-256")
    _sha256_text(evidence.setting_sha256, "Setting SHA-256")
    _sha256_text(evidence.produce_effect_sha256, "ProduceEffect SHA-256")
    _validate_int_pairs(evidence.base_multipliers, "base multipliers", allow_empty=False)
    if not isinstance(evidence.effective_bonus_candidates_permille, tuple):
        raise ValueError("effective bonus candidates must be a tuple")
    for index, value in enumerate(evidence.effective_bonus_candidates_permille):
        _exact_int(value, f"effective bonus candidates[{index}]")
    _validate_int_pairs(evidence.final_multipliers, "final multipliers", allow_empty=False)
    if not isinstance(evidence.runtime_evidence_kind, str):
        raise ValueError("runtime evidence kind must be text")
    if evidence.runtime_evidence_sha256:
        _sha256_text(evidence.runtime_evidence_sha256, "runtime evidence SHA-256")
    if not isinstance(evidence.runtime_observation_sha256s, tuple):
        raise ValueError("runtime observation SHA-256s must be a tuple")
    for index, value in enumerate(evidence.runtime_observation_sha256s):
        _sha256_text(value, f"runtime observation SHA-256s[{index}]")


def validate_audition_schedule_evidence(evidence: AuditionScheduleEvidence) -> None:
    if not isinstance(evidence, AuditionScheduleEvidence):
        raise ValueError("schedule evidence has the wrong type")
    for label in ("run_id", "produce_id", "step_type", "battle_config_id"):
        _text(getattr(evidence, label), f"schedule {label}")
    _exact_int(evidence.stage_number, "schedule stage_number", minimum=1)
    if not isinstance(evidence.frames, tuple) or len(evidence.frames) != 2:
        raise ValueError("schedule evidence must contain exactly two ring frames")
    for frame in evidence.frames:
        validate_ring_frame_evidence(frame)
    first, second = evidence.frames
    if first.current_slot_index != second.current_slot_index:
        raise ValueError("schedule ring frames disagree on current slot index")
    if first.remaining_types != second.remaining_types:
        raise ValueError("schedule ring frames disagree on remaining lesson sequence")
    validate_audition_multiplier_evidence(evidence.multiplier)


def validate_verified_turn_schedule_against_ring(
    schedule: VerifiedTurnSchedule,
    evidence: AuditionScheduleEvidence,
    ring_frames: tuple[RingFrameEvidence, RingFrameEvidence],
) -> None:
    """Replay the schedule from canonical ring evidence and require exact equality."""

    if type(schedule) is not VerifiedTurnSchedule:
        raise TypeError("schedule must be exactly VerifiedTurnSchedule")
    schedule.validate()
    validate_audition_schedule_evidence(evidence)
    if not isinstance(ring_frames, tuple) or len(ring_frames) != 2:
        raise ValueError("ring_frames must contain exactly two frames")
    if any(type(frame) is not RingFrameEvidence for frame in ring_frames):
        raise TypeError("ring_frames must contain exact RingFrameEvidence values")
    for frame in ring_frames:
        validate_ring_frame_evidence(frame)
    if evidence.frames != ring_frames:
        raise ValueError("schedule evidence and returned ring frames disagree")
    if schedule.evidence_sha256 != evidence.digest():
        raise ValueError("schedule digest disagrees with schedule evidence")
    first, second = ring_frames
    if first.current_slot_index != second.current_slot_index:
        raise ValueError("ring frames disagree on current slot index")
    if first.remaining_types != second.remaining_types:
        raise ValueError("ring frames disagree on remaining lesson sequence")
    multiplier_by_type = evidence.multiplier.by_lesson_type
    start_round = first.current_slot_index + 1
    expected_frames = tuple(
        (
            start_round + offset,
            lesson_type,
            multiplier_by_type[lesson_type],
        )
        for offset, lesson_type in enumerate(first.remaining_types)
    )
    actual_frames = tuple(
        (
            frame.round_number,
            frame.lesson_type,
            frame.score_multiplier_permille,
        )
        for frame in schedule.frames
    )
    if actual_frames != expected_frames:
        raise ValueError(
            "VerifiedTurnSchedule frames disagree with the ring lesson sequence"
        )


class _DecodeFailure(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.resolve().open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class _ScheduleSourceSnapshot:
    path: str
    size: int
    sha256: str


def _schedule_source_snapshot(path: Path) -> _ScheduleSourceSnapshot:
    resolved = path.resolve()
    try:
        before = resolved.stat()
        payload = resolved.read_bytes()
        after = resolved.stat()
    except OSError as exc:
        raise _DecodeFailure("schedule_source_missing", str(resolved)) from exc
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(payload) != after.st_size
    ):
        raise _DecodeFailure("schedule_source_changed", str(resolved))
    return _ScheduleSourceSnapshot(
        path=str(resolved),
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _schedule_source_paths(
    attributes: AuditionAttributes, master_dir: Path
) -> tuple[Path, Path, Path, Path]:
    root = master_dir.resolve()
    return (
        attributes.evidence_path.resolve(),
        root / "ProduceExamBattleScoreConfig.yaml",
        root / "Setting.yaml",
        root / "ProduceEffect.yaml",
    )


def _assert_schedule_sources_unchanged(
    before: tuple[_ScheduleSourceSnapshot, ...],
) -> tuple[_ScheduleSourceSnapshot, ...]:
    after: list[_ScheduleSourceSnapshot] = []
    for expected in before:
        try:
            current = _schedule_source_snapshot(Path(expected.path))
        except _DecodeFailure as exc:
            raise _DecodeFailure("schedule_source_changed", expected.path) from exc
        if current != expected:
            raise _DecodeFailure("schedule_source_changed", expected.path)
        after.append(current)
    return tuple(after)


def _validate_exam_screen(screen: ExamScreenState, *, total_turns: int) -> None:
    if not isinstance(screen, ExamScreenState):
        raise _DecodeFailure("screen_numeric_invalid", "screen has the wrong type")
    try:
        _exact_int(total_turns, "total turns", minimum=1)
        _exact_int(
            screen.turns_remaining,
            "screen turns_remaining",
            minimum=1,
            maximum=total_turns,
        )
        _exact_int(
            screen.score_multiplier_permille,
            "screen score_multiplier_permille",
            minimum=1,
        )
        _exact_int(screen.stamina, "screen stamina", minimum=0)
        _exact_int(screen.block, "screen block", minimum=0)
        _exact_int(screen.player_score, "screen player_score", minimum=0)
        overall_confidence = _confidence(screen.confidence, "screen confidence")
        minimum_confidence = _confidence(
            screen.minimum_confidence, "screen minimum confidence"
        )
        if minimum_confidence > overall_confidence:
            raise ValueError("screen minimum confidence exceeds overall confidence")
        if not isinstance(screen.field_confidence, Mapping):
            raise ValueError("screen field_confidence must be a mapping")
        seen: set[str] = set()
        field_values: list[float] = []
        for key, value in screen.field_confidence.items():
            key = _text(key, "screen field confidence key")
            if key in seen:
                raise ValueError("screen field confidence contains duplicate keys")
            seen.add(key)
            field_values.append(
                _confidence(value, f"screen field confidence {key}")
            )
        for critical in ("turns_remaining", "score_multiplier"):
            if critical not in screen.field_confidence:
                raise ValueError(f"screen field confidence is missing {critical}")
        if field_values and minimum_confidence > min(field_values):
            raise ValueError("screen minimum confidence exceeds a field confidence")
        logic_status = screen.logic_status
        for label in ("confidence", "minimum_confidence"):
            value = getattr(logic_status, label, None)
            if value is not None:
                _confidence(value, f"logic status {label}")
    except ValueError as exc:
        raise _DecodeFailure("screen_numeric_invalid", str(exc)) from exc


def _angular_distance(left: float, right: float) -> float:
    return abs((left - right + 180.0) % 360.0 - 180.0)


def _slot_angles() -> tuple[float, ...]:
    step = 360.0 / RING_SLOT_COUNT
    return tuple(
        (RING_START_ANGLE_DEGREES - step * index) % 360.0
        for index in range(RING_SLOT_COUNT)
    )


def _decode_ring_slots(image: Image.Image) -> tuple[RingSlotObservation, ...]:
    canonical = image.convert("RGB")
    if canonical.size != CANONICAL_SIZE:
        raise _DecodeFailure(
            "unexpected_capture_size",
            f"capture is {canonical.size[0]}x{canonical.size[1]}",
        )
    pixels = np.asarray(canonical)
    center_x, center_y = RING_CENTER
    observations: list[RingSlotObservation] = []
    for index, target_angle in enumerate(_slot_angles()):
        counts = {lesson_type: 0 for lesson_type in _COLOR_HUES}
        for y in range(center_y - 56, center_y + 56):
            for x in range(center_x - 56, center_x + 56):
                delta_x = x - center_x
                delta_y = y - center_y
                radius = math.hypot(delta_x, delta_y)
                if not RING_RADIUS_MIN <= radius <= RING_RADIUS_MAX:
                    continue
                angle = math.degrees(math.atan2(delta_y, delta_x)) % 360.0
                if _angular_distance(angle, target_angle) > RING_HALF_WEDGE_DEGREES:
                    continue
                red, green, blue = (float(value) / 255.0 for value in pixels[y, x])
                hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
                hue *= 360.0
                if saturation < 0.38 or value < 0.45:
                    continue
                lesson_type, distance = min(
                    (
                        (candidate, _angular_distance(hue, prototype))
                        for candidate, prototype in _COLOR_HUES.items()
                    ),
                    key=lambda item: item[1],
                )
                if distance <= 30.0:
                    counts[lesson_type] += 1
        ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        winner_type, winner_pixels = ordered[0]
        runner_up_pixels = ordered[1][1]
        coloured_total = sum(counts.values())
        confidence = (
            winner_pixels / coloured_total if coloured_total else 0.0
        )
        observations.append(
            RingSlotObservation(
                slot_index=index,
                lesson_type=(
                    winner_type
                    if winner_pixels >= RING_MIN_COLOURED_PIXELS
                    and confidence >= RING_MIN_COLOR_CONFIDENCE
                    else None
                ),
                winner_pixels=winner_pixels,
                runner_up_pixels=runner_up_pixels,
                confidence=confidence,
                counts=tuple(sorted(counts.items())),
            )
        )
    return tuple(observations)


def decode_ring_frame(
    capture_path: Path,
    screen: ExamScreenState,
    *,
    total_turns: int,
) -> RingFrameEvidence:
    """Decode one already-OCR'd frame and bind it to its PNG hash."""

    path = capture_path.resolve()
    if not path.is_file():
        raise _DecodeFailure("capture_missing", str(path))
    if type(total_turns) is not int or total_turns != RING_SLOT_COUNT:
        raise _DecodeFailure(
            "unverified_ring_geometry",
            f"only the cross-turn-verified {RING_SLOT_COUNT}-slot ring is supported",
        )
    _validate_exam_screen(screen, total_turns=total_turns)
    if screen.minimum_confidence < SCREEN_MIN_CONFIDENCE:
        raise _DecodeFailure(
            "low_screen_confidence",
            f"minimum OCR confidence={screen.minimum_confidence:.6f}",
        )
    critical_confidence = min(
        float(screen.field_confidence.get(field, 0.0))
        for field in ("turns_remaining", "score_multiplier")
    )
    if critical_confidence < SCREEN_MIN_CRITICAL_CONFIDENCE:
        raise _DecodeFailure(
            "low_critical_screen_confidence",
            f"turn/multiplier OCR confidence={critical_confidence:.6f}",
        )
    with Image.open(path) as image:
        if image.format != "PNG":
            raise _DecodeFailure(
                "unexpected_capture_format", f"capture format is {image.format!r}"
            )
        image.load()
        width, height = image.size
        if (width, height) != CANONICAL_SIZE:
            raise _DecodeFailure(
                "unexpected_capture_size", f"capture is {width}x{height}"
            )
        slots = _decode_ring_slots(image)
    current_slot_index = total_turns - screen.turns_remaining
    consumed = slots[:current_slot_index]
    remaining = slots[current_slot_index:]
    if any(
        slot.winner_pixels > RING_MAX_CONSUMED_PIXELS for slot in consumed
    ):
        raise _DecodeFailure(
            "consumed_ring_slot_still_coloured",
            f"current slot index={current_slot_index}",
        )
    if any(slot.lesson_type is None for slot in remaining):
        unresolved = [
            slot.slot_index for slot in remaining if slot.lesson_type is None
        ]
        raise _DecodeFailure(
            "ring_color_unresolved", f"unresolved remaining slots={unresolved}"
        )
    return RingFrameEvidence(
        capture_path=str(path),
        capture_sha256=_sha256(path),
        width=width,
        height=height,
        turns_remaining=screen.turns_remaining,
        score_multiplier_permille=screen.score_multiplier_permille,
        stamina=screen.stamina,
        block=screen.block,
        player_score=screen.player_score,
        screen_minimum_confidence=screen.minimum_confidence,
        screen_field_confidence=tuple(
            sorted(
                (key, float(value))
                for key, value in screen.field_confidence.items()
            )
        ),
        current_slot_index=current_slot_index,
        slots=slots,
    )


def _load_yaml_rows(path: Path) -> tuple[dict[str, object], ...]:
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise _DecodeFailure("master_invalid", f"{path.name} is not a row list")
    return tuple(row for row in payload if isinstance(row, dict))


def _ceil_float32(value: np.float32) -> int:
    return int(np.ceil(value))


def _interpolated_permille(
    rows: Sequence[Mapping[str, object]], parameter: int, column: str
) -> int:
    ordered = sorted(rows, key=lambda row: int(row["parameter"]))
    exact = [row for row in ordered if int(row["parameter"]) == parameter]
    if exact:
        return int(exact[0][column])
    smaller = [row for row in ordered if int(row["parameter"]) < parameter]
    larger = [row for row in ordered if int(row["parameter"]) > parameter]
    if not smaller:
        return int(ordered[0][column])
    if not larger:
        return int(ordered[-1][column])
    low = smaller[-1]
    high = larger[0]
    low_parameter = int(low["parameter"])
    high_parameter = int(high["parameter"])
    low_value = int(low[column])
    high_value = int(high[column])
    # Native CalcPermil performs this interpolation as binary64 and applies
    # ceiling only to the delta before adding the lower row.
    delta = (
        (high_value - low_value)
        / (high_parameter - low_parameter)
        * (parameter - low_parameter)
    )
    return low_value + math.ceil(delta)


def _penalty_permille(
    parameter: int,
    configured_parameter: int,
    minimum: int,
    maximum: int,
) -> int:
    if parameter >= configured_parameter:
        return 0
    return math.trunc(
        minimum
        + (maximum - minimum)
        * (configured_parameter - parameter)
        / configured_parameter
    )


def calculate_audition_base_multiplier_permils(
    *,
    score_curve: Sequence[tuple[int, int, int, int]],
    attributes: tuple[int, int, int],
    configured_parameters: tuple[int, int, int],
    penalty_min_permille: int,
    penalty_max_permille: int,
) -> tuple[int, int, int]:
    """Pure native base multipliers before audition E/Vote/Star factors.

    Each score-curve row is ``(parameter, vocal, dance, visual)``.  The
    returned tuple uses the same Vocal/Dance/Visual order.  This is the
    data-only part of :func:`_base_multipliers`; it performs no file,
    evidence, hash, clock, screenshot, or device access.
    """

    rows = tuple(score_curve)
    if len(rows) < 2:
        raise ValueError("score_curve must contain at least two points")
    for row_index, row in enumerate(rows):
        if not isinstance(row, tuple) or len(row) != 4:
            raise TypeError(
                f"score_curve[{row_index}] must contain four integers"
            )
        for value_index, value in enumerate(row):
            _exact_int(
                value,
                f"score_curve[{row_index}][{value_index}]",
                minimum=0,
            )
    parameters = tuple(row[0] for row in rows)
    if len(parameters) != len(set(parameters)):
        raise ValueError("score_curve parameter values must be unique")
    if len(attributes) != 3 or len(configured_parameters) != 3:
        raise ValueError(
            "attributes and configured_parameters must contain three values"
        )
    for index, value in enumerate(attributes):
        _exact_int(value, f"attributes[{index}]", minimum=0)
    for index, value in enumerate(configured_parameters):
        _exact_int(value, f"configured_parameters[{index}]", minimum=1)
    minimum = _exact_int(
        penalty_min_permille,
        "penalty_min_permille",
        minimum=0,
    )
    maximum = _exact_int(
        penalty_max_permille,
        "penalty_max_permille",
        minimum=minimum,
    )
    penalties = sum(
        _penalty_permille(parameter, configured, minimum, maximum)
        for parameter, configured in zip(attributes, configured_parameters)
    )
    scale = 1000 - penalties
    if scale <= 0:
        raise ValueError(f"combined parameter penalty is invalid: {penalties}")
    mapping_rows = tuple(
        {
            "parameter": parameter,
            "vocalPermil": vocal,
            "dancePermil": dance,
            "visualPermil": visual,
        }
        for parameter, vocal, dance, visual in rows
    )
    result: list[int] = []
    for parameter, column in zip(
        attributes,
        ("vocalPermil", "dancePermil", "visualPermil"),
    ):
        raw = _interpolated_permille(mapping_rows, parameter, column)
        adjusted = (raw * scale) // 1000 + 1000
        result.append(((adjusted + 9) // 10) * 10)
    return result[0], result[1], result[2]


def _base_multipliers(
    rules: AuditionRules,
    attributes: AuditionAttributes,
    *,
    master_dir: Path,
) -> tuple[dict[str, int], str, str]:
    score_path = master_dir.resolve() / "ProduceExamBattleScoreConfig.yaml"
    setting_path = master_dir.resolve() / "Setting.yaml"
    if not score_path.is_file() or not setting_path.is_file():
        raise _DecodeFailure("master_missing", str(master_dir.resolve()))
    rows = tuple(
        row
        for row in _load_yaml_rows(score_path)
        if row.get("id") == rules.score_config_id
    )
    if len(rows) < 2:
        raise _DecodeFailure(
            "score_config_unresolved", rules.score_config_id
        )
    settings = _load_yaml_rows(setting_path)
    if len(settings) != 1:
        raise _DecodeFailure("setting_unresolved", str(len(settings)))
    minimum = int(settings[0]["produceExamBattleScorePenaltyMinPermil"])
    maximum = int(settings[0]["produceExamBattleScorePenaltyMaxPermil"])
    try:
        resolved = calculate_audition_base_multiplier_permils(
            score_curve=tuple(
                (
                    int(row["parameter"]),
                    int(row["vocalPermil"]),
                    int(row["dancePermil"]),
                    int(row["visualPermil"]),
                )
                for row in rows
            ),
            attributes=(attributes.vocal, attributes.dance, attributes.visual),
            configured_parameters=(
                rules.vocal_parameter,
                rules.dance_parameter,
                rules.visual_parameter,
            ),
            penalty_min_permille=minimum,
            penalty_max_permille=maximum,
        )
    except (TypeError, ValueError) as exc:
        raise _DecodeFailure("invalid_parameter_penalty", str(exc)) from exc
    result = dict(
        zip(
            (LESSON_VOCAL, LESSON_DANCE, LESSON_VISUAL),
            resolved,
        )
    )
    return result, _sha256(score_path), _sha256(setting_path)


def _multiplier_evidence_unchecked(
    rules: AuditionRules,
    attributes: AuditionAttributes,
    *,
    run_id: str,
    current_lesson_type: str,
    visible_current_multiplier: int,
    current_step_context_id: str | None,
    current_step_context_digest: str | None,
    master_dir: Path,
    runtime_checkpoint: AuditionMultiplierCheckpoint | None,
) -> AuditionMultiplierEvidence:
    attributes.validate()
    base, score_sha, setting_sha = _base_multipliers(
        rules, attributes, master_dir=master_dir
    )
    # CreateAuditionTransitionParam applies two separately rounded float32
    # factors: (1 + E/1000) and (1 + (Vote+Star)/1000).  One displayed colour
    # cannot recover those two integers or the unseen final multipliers.  For
    # the current bases, for example, E=0 with Vote+Star=499 and 501 both show
    # Dance=6560 while producing Visual=6960 and 6970 respectively.  Static
    # ProduceEffect rows are possible E values, not proof of the live E/V/S
    # transaction.  Keep the verified colour ring unless a run-bound
    # checkpoint contains direct dual-stable observations of all three final
    # colour multipliers.
    if runtime_checkpoint is None:
        raise _DecodeFailure(
            "runtime_audition_factors_unverified",
            f"{current_lesson_type} visible={visible_current_multiplier}, "
            f"base={base[current_lesson_type]}; require E/V/S or all final multipliers",
        )
    if current_step_context_id is None or current_step_context_digest is None:
        raise _DecodeFailure(
            "runtime_multiplier_context_missing",
            "current ExamSession step_context_id/digest are required",
        )
    try:
        runtime_checkpoint.validate_for(
            run_id=run_id,
            rules=rules,
            attributes=(attributes.vocal, attributes.dance, attributes.visual),
            attribute_evidence_path=attributes.evidence_path,
            current_step_context_id=current_step_context_id,
            current_step_context_digest=current_step_context_digest,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _DecodeFailure(
            "runtime_multiplier_checkpoint_invalid", str(exc)
        ) from exc
    if not runtime_checkpoint.complete:
        observed = [
            lesson_type
            for lesson_type, _ in runtime_checkpoint.final_multipliers
        ]
        raise _DecodeFailure(
            "runtime_audition_factors_unverified",
            "direct multiplier checkpoint is incomplete: "
            + ",".join(observed),
        )
    finals = dict(runtime_checkpoint.final_multipliers)
    if finals.get(current_lesson_type) != visible_current_multiplier:
        raise _DecodeFailure(
            "runtime_multiplier_current_mismatch",
            f"{current_lesson_type} checkpoint={finals.get(current_lesson_type)} "
            f"visible={visible_current_multiplier}",
        )
    produce_effect_path = master_dir.resolve() / "ProduceEffect.yaml"
    if not produce_effect_path.is_file():
        raise _DecodeFailure("master_missing", str(produce_effect_path))
    return AuditionMultiplierEvidence(
        attributes=(attributes.vocal, attributes.dance, attributes.visual),
        attribute_evidence_path=str(attributes.evidence_path.resolve()),
        attribute_evidence_sha256=_sha256(attributes.evidence_path),
        score_config_id=rules.score_config_id,
        score_config_sha256=score_sha,
        setting_sha256=setting_sha,
        produce_effect_sha256=_sha256(produce_effect_path),
        base_multipliers=tuple(sorted(base.items())),
        effective_bonus_candidates_permille=(),
        final_multipliers=tuple(
            (lesson_type, finals[lesson_type])
            for lesson_type in (LESSON_VOCAL, LESSON_DANCE, LESSON_VISUAL)
        ),
        runtime_evidence_kind="direct-three-colour-dual-stable-v1",
        runtime_evidence_sha256=runtime_checkpoint.digest(),
        runtime_observation_sha256s=runtime_checkpoint.observation_digests,
    )


def _multiplier_evidence(
    rules: AuditionRules,
    attributes: AuditionAttributes,
    *,
    run_id: str,
    current_lesson_type: str,
    visible_current_multiplier: int,
    current_step_context_id: str | None,
    current_step_context_digest: str | None,
    master_dir: Path,
    runtime_checkpoint: AuditionMultiplierCheckpoint | None,
) -> AuditionMultiplierEvidence:
    """Bind multiplier derivation to one stable attribute/Master snapshot."""

    attributes.validate()
    before = tuple(
        _schedule_source_snapshot(path)
        for path in _schedule_source_paths(attributes, master_dir)
    )
    try:
        result = _multiplier_evidence_unchecked(
            rules,
            attributes,
            run_id=run_id,
            current_lesson_type=current_lesson_type,
            visible_current_multiplier=visible_current_multiplier,
            current_step_context_id=current_step_context_id,
            current_step_context_digest=current_step_context_digest,
            master_dir=master_dir,
            runtime_checkpoint=runtime_checkpoint,
        )
    except Exception as exc:
        try:
            _assert_schedule_sources_unchanged(before)
        except _DecodeFailure as changed:
            raise changed from exc
        raise
    _assert_schedule_sources_unchanged(before)
    attribute, score, setting, produce_effect = before
    expected_provenance = (
        (attributes.vocal, attributes.dance, attributes.visual),
        attribute.path,
        attribute.sha256,
        score.sha256,
        setting.sha256,
        produce_effect.sha256,
    )
    actual_provenance = (
        result.attributes,
        str(Path(result.attribute_evidence_path).resolve()),
        result.attribute_evidence_sha256,
        result.score_config_sha256,
        result.setting_sha256,
        result.produce_effect_sha256,
    )
    if actual_provenance != expected_provenance:
        raise _DecodeFailure(
            "schedule_source_provenance_mismatch",
            f"{actual_provenance!r} != {expected_provenance!r}",
        )
    return result


def _same_stable_screen(
    first: RingFrameEvidence, second: RingFrameEvidence
) -> bool:
    return (
        first.turns_remaining,
        first.score_multiplier_permille,
        first.stamina,
        first.block,
        first.player_score,
    ) == (
        second.turns_remaining,
        second.score_multiplier_permille,
        second.stamina,
        second.block,
        second.player_score,
    )


def _decode_dual_stable_schedule_from_observations_unchecked(
    first_path: Path,
    second_path: Path,
    first_screen: ExamScreenState,
    second_screen: ExamScreenState,
    *,
    run_id: str,
    rules: AuditionRules,
    attributes: AuditionAttributes,
    current_step_context_id: str | None = None,
    current_step_context_digest: str | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    runtime_checkpoint: AuditionMultiplierCheckpoint | None = None,
) -> AuditionScheduleDecodeResult:
    """Build a schedule from two replay/test OCR observations.

    Live callers should use :func:`decode_dual_stable_schedule_paths`, which
    performs OCR from the capture files in the same call.  This lower-level
    entry point exists for deterministic replay and unit tests; it still
    hashes and decodes both PNGs itself.  A runtime checkpoint can become
    decision-ready only when the caller also supplies the current durable
    ExamSession step-context id and digest.
    """

    ring_frames: tuple[RingFrameEvidence, RingFrameEvidence] | None = None
    try:
        if not run_id:
            raise _DecodeFailure("context_invalid", "run_id is empty")
        first = decode_ring_frame(
            first_path, first_screen, total_turns=rules.turns
        )
        second = decode_ring_frame(
            second_path, second_screen, total_turns=rules.turns
        )
        if first_path.resolve() == second_path.resolve():
            raise _DecodeFailure(
                "duplicate_capture", "dual evidence resolves to the same PNG path"
            )
        if not _same_stable_screen(first, second):
            raise _DecodeFailure(
                "unstable_screen", "HUD values changed between evidence frames"
            )
        if first.current_slot_index != second.current_slot_index:
            raise _DecodeFailure(
                "ring_position_mismatch", "current ring slot changed"
            )
        if first.remaining_types != second.remaining_types:
            raise _DecodeFailure(
                "ring_sequence_mismatch",
                f"{first.remaining_types} != {second.remaining_types}",
            )
        ring_frames = (first, second)
        current_lesson_type = first.remaining_types[0]
        multiplier = _multiplier_evidence(
            rules,
            attributes,
            run_id=run_id,
            current_lesson_type=current_lesson_type,
            visible_current_multiplier=first.score_multiplier_permille,
            current_step_context_id=current_step_context_id,
            current_step_context_digest=current_step_context_digest,
            master_dir=master_dir,
            runtime_checkpoint=runtime_checkpoint,
        )
        multipliers = multiplier.by_lesson_type
        start_round = first.current_slot_index + 1
        frames = tuple(
            VerifiedTurnFrame(
                round_number=start_round + offset,
                lesson_type=lesson_type,
                score_multiplier_permille=multipliers[lesson_type],
            )
            for offset, lesson_type in enumerate(first.remaining_types)
        )
        evidence = AuditionScheduleEvidence(
            run_id=run_id,
            produce_id=rules.produce_id,
            step_type=rules.step_type,
            stage_number=rules.number,
            battle_config_id=rules.battle_config_id,
            frames=(first, second),
            multiplier=multiplier,
        )
        schedule = VerifiedTurnSchedule(
            source=(
                f"{DECODER_VERSION}:{run_id}:{rules.produce_id}:"
                f"{rules.step_type}:{rules.number}"
            ),
            evidence_sha256=evidence.digest(),
            frames=frames,
        )
        validate_verified_turn_schedule_against_ring(
            schedule,
            evidence,
            ring_frames,
        )
        return AuditionScheduleDecodeResult(schedule, evidence, None, ring_frames)
    except _DecodeFailure as exc:
        return AuditionScheduleDecodeResult(
            None, None, AuditionScheduleBlocker(exc.code, exc.detail), ring_frames
        )
    except (KeyError, TypeError, ValueError, OSError, yaml.YAMLError) as exc:
        return AuditionScheduleDecodeResult(
            None,
            None,
            AuditionScheduleBlocker("schedule_decode_error", str(exc)),
            ring_frames,
        )


def decode_dual_stable_schedule_from_observations(
    first_path: Path,
    second_path: Path,
    first_screen: ExamScreenState,
    second_screen: ExamScreenState,
    *,
    run_id: str,
    rules: AuditionRules,
    attributes: AuditionAttributes,
    current_step_context_id: str | None = None,
    current_step_context_digest: str | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    runtime_checkpoint: AuditionMultiplierCheckpoint | None = None,
) -> AuditionScheduleDecodeResult:
    """Decode only while every attribute/Master source remains unchanged."""

    try:
        attributes.validate()
        before = tuple(
            _schedule_source_snapshot(path)
            for path in _schedule_source_paths(attributes, master_dir)
        )
    except _DecodeFailure as exc:
        return AuditionScheduleDecodeResult(
            None, None, AuditionScheduleBlocker(exc.code, exc.detail)
        )
    except (OSError, TypeError, ValueError) as exc:
        return AuditionScheduleDecodeResult(
            None, None, AuditionScheduleBlocker("schedule_decode_error", str(exc))
        )
    result = _decode_dual_stable_schedule_from_observations_unchecked(
        first_path,
        second_path,
        first_screen,
        second_screen,
        run_id=run_id,
        rules=rules,
        attributes=attributes,
        current_step_context_id=current_step_context_id,
        current_step_context_digest=current_step_context_digest,
        master_dir=master_dir,
        runtime_checkpoint=runtime_checkpoint,
    )
    try:
        _assert_schedule_sources_unchanged(before)
    except _DecodeFailure as exc:
        return AuditionScheduleDecodeResult(
            None,
            None,
            AuditionScheduleBlocker(exc.code, exc.detail),
            result.ring_frames,
        )
    return result


def decode_dual_stable_schedule_paths(
    first_path: Path,
    second_path: Path,
    *,
    run_id: str,
    rules: AuditionRules,
    attributes: AuditionAttributes,
    current_step_context_id: str | None = None,
    current_step_context_digest: str | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    recognizer: PaddleLineRecognizer | None = None,
    expected_score: int | None = None,
    runtime_checkpoint: AuditionMultiplierCheckpoint | None = None,
) -> AuditionScheduleDecodeResult:
    """OCR two PNGs and recompute the complete canonical evidence bundle.

    ``current_step_context_id`` and ``current_step_context_digest`` are
    mandatory whenever ``runtime_checkpoint`` is supplied for a ready result.
    """

    try:
        attributes.validate()
        source_before = tuple(
            _schedule_source_snapshot(path)
            for path in _schedule_source_paths(attributes, master_dir)
        )
    except _DecodeFailure as exc:
        return AuditionScheduleDecodeResult(
            None, None, AuditionScheduleBlocker(exc.code, exc.detail)
        )
    except (OSError, TypeError, ValueError) as exc:
        return AuditionScheduleDecodeResult(
            None, None, AuditionScheduleBlocker("schedule_decode_error", str(exc))
        )
    try:
        reader = recognizer or PaddleLineRecognizer()
        first_screen = read_exam_screen_path(
            first_path.resolve(), reader, expected_score=expected_score
        )
        second_screen = read_exam_screen_path(
            second_path.resolve(), reader, expected_score=expected_score
        )
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            _assert_schedule_sources_unchanged(source_before)
        except _DecodeFailure as changed:
            return AuditionScheduleDecodeResult(
                None,
                None,
                AuditionScheduleBlocker(changed.code, changed.detail),
            )
        return AuditionScheduleDecodeResult(
            None, None, AuditionScheduleBlocker("screen_read_failed", str(exc))
        )
    result = decode_dual_stable_schedule_from_observations(
        first_path,
        second_path,
        first_screen,
        second_screen,
        run_id=run_id,
        rules=rules,
        attributes=attributes,
        current_step_context_id=current_step_context_id,
        current_step_context_digest=current_step_context_digest,
        master_dir=master_dir,
        runtime_checkpoint=runtime_checkpoint,
    )
    try:
        _assert_schedule_sources_unchanged(source_before)
    except _DecodeFailure as changed:
        return AuditionScheduleDecodeResult(
            None,
            None,
            AuditionScheduleBlocker(changed.code, changed.detail),
            result.ring_frames,
        )
    return result
