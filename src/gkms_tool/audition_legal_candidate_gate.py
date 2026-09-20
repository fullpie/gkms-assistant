"""Read-only, fail-closed legality candidates for visible Exam hands.

MaaGakumasu's ``ProduceRecognitionCards`` recognition does not identify a
card.  It only emits all detector boxes with one of three visual labels:
``cards`` (available), ``suggestions`` (available and recommended), or
``useless`` (not available).  This module joins those labels to the ordered
``ExamSaveData`` Hand and the already pinned 1..5-card screen slots.

The join is intentionally stricter than the existing click-oriented readers:
we do not collapse overlapping class overlays, infer a missing slot, or use
detector confidence as a value function.  A result is publishable only when
every visible detector box maps one-to-one to one ordered Hand slot and the
ExamSave/capture identity is explicitly bound.  The module performs no
capture, click, controller, simulator, or file-write operation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
    LocalSaveExamState,
)


SCHEMA_NAME = "gkms_tool.audition_legal_candidate_gate"
SCHEMA_VERSION = 1

# These bounds intentionally mirror Maa's source guard in
# agent/custom/action/produce.py.  They are detector top-left y coordinates,
# not a broad lower-half screen heuristic.
CARD_Y_MIN = 840
CARD_Y_MAX = 1150
SUPPORTED_PLAN_TYPES = frozenset(
    {
        "ProducePlanType_Plan1",
        "ProducePlanType_Plan2",
        "ProducePlanType_Plan3",
    }
)
DETECTOR_LABELS = frozenset({"cards", "suggestions", "useless"})
LEGAL_LABELS = frozenset({"cards", "suggestions"})
LABEL_ALIASES = {"recommend": "suggestions"}
CANONICAL_WIDTH = 720
CANONICAL_HEIGHT = 1280
_SHA256_HEX = frozenset("0123456789abcdef")

CanonicalBox = tuple[int, int, int, int]


def _fixed_hand_slot_boxes() -> Mapping[int, tuple[CanonicalBox, ...]]:
    """Return the existing pinned hand geometry without importing it eagerly.

    ``plan3_audition_executor`` owns the constant because it is shared by the
    existing Plan 3 executor and Plan 2 native binder.  Lazy import keeps this
    read-only provider cheap to import and avoids making it an execution
    dependency.
    """

    from .plan3_audition_executor import FIXED_HAND_SLOT_BOXES

    return FIXED_HAND_SLOT_BOXES


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(value: object, label: str) -> float:
    if not _is_number(value) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _canonical_digest(value: object, label: str) -> str:
    text = _nonempty_text(value, label).lower()
    if len(text) != 64 or any(character not in _SHA256_HEX for character in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _canonical_box(raw: object, label: str) -> CanonicalBox:
    if isinstance(raw, Mapping):
        values = (
            raw.get("x", raw.get("left")),
            raw.get("y", raw.get("top")),
            raw.get("width", raw.get("w")),
            raw.get("height", raw.get("h")),
        )
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        if len(raw) != 4:
            raise ValueError(f"{label} must contain x, y, width, height")
        values = tuple(raw)
    else:
        raise ValueError(f"{label} must be a box")
    x, y, width, height = values
    # Detector coordinates are integer pixels in the current CardDetection
    # contract, but Maa's raw Result may contain numeric subclasses/floats.
    # Keep a deterministic integer pixel box while rejecting fractional values
    # that would move a center across a slot boundary.
    numbers = tuple(_finite_number(value, f"{label}[{index}]") for index, value in enumerate(values))
    if any(value != math.floor(value) for value in numbers):
        raise ValueError(f"{label} must use integer pixel coordinates")
    result = tuple(int(value) for value in numbers)
    if result[2] <= 0 or result[3] <= 0:
        raise ValueError(f"{label} width and height must be positive")
    return result  # type: ignore[return-value]


def _mapping_or_attributes(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    # CardDetection, Maa recognition Result, and LiveCapture are all simple
    # attribute records.  Refuse arbitrary objects with no useful fields.
    fields = {}
    for name in (
        "detections",
        "label",
        "score",
        "confidence",
        "box",
        "x",
        "y",
        "width",
        "height",
        "w",
        "h",
        "source",
        "model",
        "provider",
        "elapsed_ms",
        "image_width",
        "image_height",
        "source_path",
        "png_path",
        "timestamp",
        "hwnd",
        "pid",
        "width",
        "height",
        "evidence_digest",
        "exam_save_digest",
        "exam_save_sha256",
        "source_sha256",
        "session_transition_id",
        "transition_id",
        "run_id",
        "step_context_id",
        "stale",
        "is_stale",
        "settled",
    ):
        if hasattr(value, name):
            fields[name] = getattr(value, name)
    if fields:
        return fields
    if is_dataclass(value):
        return asdict(value)
    raise ValueError(f"unsupported evidence record: {type(value).__name__}")


def _read_field(value: object, *names: str) -> object | None:
    try:
        record = _mapping_or_attributes(value)
    except ValueError:
        return None
    for name in names:
        if name in record:
            return record[name]
    return None


def _normalize_plan_type(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        # ExamSaveData's native enum values used by the checked-in PC
        # evidence: Plan1=2, Plan2=3, Plan3=4.
        return {2: "ProducePlanType_Plan1", 3: "ProducePlanType_Plan2", 4: "ProducePlanType_Plan3"}.get(value)
    if not isinstance(value, str):
        return None
    aliases = {
        "plan1": "ProducePlanType_Plan1",
        "plan2": "ProducePlanType_Plan2",
        "plan3": "ProducePlanType_Plan3",
        "Plan1": "ProducePlanType_Plan1",
        "Plan2": "ProducePlanType_Plan2",
        "Plan3": "ProducePlanType_Plan3",
    }
    result = aliases.get(value, value)
    return result if result in SUPPORTED_PLAN_TYPES else None


def _extract_plan_type(state: LocalSaveExamState) -> str | None:
    runtime = state.root_runtime
    if runtime is None:
        return None
    try:
        opaque = runtime.opaque_fields.to_value()
    except Exception:
        return None
    if not isinstance(opaque, Mapping):
        return None
    return _normalize_plan_type(opaque.get("planType"))


@dataclass(frozen=True, slots=True)
class LegalCandidateGateIssue:
    """One reason the provider abstained."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        _nonempty_text(self.code, "issue.code")
        if not isinstance(self.detail, str):
            raise TypeError("issue.detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class LegalCandidate:
    """One ordered Hand slot joined to its detector state."""

    slot: int
    guid: str
    card_id: str
    label: str
    box: CanonicalBox
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.slot, int) or isinstance(self.slot, bool) or self.slot < 0:
            raise ValueError("candidate.slot must be a non-negative integer")
        _nonempty_text(self.guid, "candidate.guid")
        _nonempty_text(self.card_id, "candidate.card_id")
        if self.label not in DETECTOR_LABELS:
            raise ValueError("candidate.label is not a Maa card label")
        _canonical_box(self.box, "candidate.box")
        if self.confidence is not None:
            _finite_number(self.confidence, "candidate.confidence")
            if not 0.0 <= float(self.confidence) <= 1.0:
                raise ValueError("candidate.confidence must be between zero and one")

    @property
    def legal(self) -> bool:
        return self.label in LEGAL_LABELS

    @property
    def is_legal(self) -> bool:
        return self.legal

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "guid": self.guid,
            "card_id": self.card_id,
            "label": self.label,
            "box": list(self.box),
            "confidence": self.confidence,
            "legal": self.legal,
        }


@dataclass(frozen=True, slots=True)
class LegalCandidateGateResult:
    """Immutable gate result; ``accepted=False`` always means no candidates."""

    accepted: bool
    plan_type: str | None
    hand_count: int
    candidates: tuple[LegalCandidate, ...] = ()
    issues: tuple[LegalCandidateGateIssue, ...] = ()
    evidence_digest: str | None = None
    capture_identity: str | None = None

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        issues = tuple(self.issues)
        if any(not isinstance(item, LegalCandidate) for item in candidates):
            raise TypeError("candidates must contain LegalCandidate values")
        if any(not isinstance(item, LegalCandidateGateIssue) for item in issues):
            raise TypeError("issues must contain LegalCandidateGateIssue values")
        if not isinstance(self.hand_count, int) or isinstance(self.hand_count, bool) or self.hand_count < 0:
            raise ValueError("hand_count must be a non-negative integer")
        if self.plan_type is not None and self.plan_type not in SUPPORTED_PLAN_TYPES:
            raise ValueError("plan_type is not supported")
        if self.accepted and issues:
            raise ValueError("an accepted result cannot carry issues")
        if self.accepted and len(candidates) != self.hand_count:
            raise ValueError("an accepted result must contain every Hand slot")
        if not self.accepted and candidates:
            raise ValueError("an abstained result cannot publish candidates")
        if self.evidence_digest is not None:
            _canonical_digest(self.evidence_digest, "evidence_digest")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "issues", issues)

    @property
    def abstain(self) -> bool:
        return not self.accepted

    @property
    def decision_ready(self) -> bool:
        return self.accepted

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return self.blocker_codes

    @property
    def legal_candidates(self) -> tuple[LegalCandidate, ...]:
        return tuple(candidate for candidate in self.candidates if candidate.legal)

    @property
    def legal_guids(self) -> tuple[str, ...]:
        return tuple(candidate.guid for candidate in self.legal_candidates)

    @property
    def legal_candidate_ids(self) -> tuple[str, ...]:
        return self.legal_guids

    @property
    def legal_guid_set(self) -> frozenset[str]:
        return frozenset(self.legal_guids)

    @property
    def legal_card_ids(self) -> tuple[str, ...]:
        return tuple(candidate.card_id for candidate in self.legal_candidates)

    @property
    def slot_mapping(self) -> tuple[tuple[int, str, CanonicalBox], ...]:
        return tuple((item.slot, item.guid, item.box) for item in self.candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_name": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "accepted": self.accepted,
            "abstain": self.abstain,
            "plan_type": self.plan_type,
            "hand_count": self.hand_count,
            "candidates": [item.to_dict() for item in self.candidates],
            "legal_guids": list(self.legal_guids),
            "legal_card_ids": list(self.legal_card_ids),
            "slot_mapping": [
                {"slot": slot, "guid": guid, "box": list(box)}
                for slot, guid, box in self.slot_mapping
            ],
            "issues": [issue.to_dict() for issue in self.issues],
            "evidence_digest": self.evidence_digest,
            "capture_identity": self.capture_identity,
        }


@dataclass(frozen=True, slots=True)
class _NormalizedDetection:
    label: str
    confidence: float | None
    box: CanonicalBox
    original_index: int


@dataclass(frozen=True, slots=True)
class _CaptureContext:
    width: int
    height: int
    identity: str
    timestamp: float


def _issue(code: str, detail: object = "") -> LegalCandidateGateIssue:
    return LegalCandidateGateIssue(code, str(detail))


def _state_from_evidence(
    evidence: AuditionLocalSaveStateEvidence | LocalSaveExamState,
) -> tuple[LocalSaveExamState | None, str | None, list[LegalCandidateGateIssue]]:
    issues: list[LegalCandidateGateIssue] = []
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        if isinstance(evidence, LocalSaveExamState):
            issues.append(_issue("exam-save-envelope-required", "source digest and transition identity are required"))
        else:
            issues.append(_issue("exam-save-type-invalid", type(evidence).__name__))
        return None, None, issues
    digest = evidence.digest()
    state = evidence.state
    if not state.has_complete_root_runtime_state:
        issues.append(_issue("exam-save-root-runtime-incomplete"))
    if not state.has_complete_card_runtime_state:
        issues.append(_issue("exam-save-card-runtime-incomplete"))
    if not state.is_native_actionable_settled:
        issues.append(_issue("exam-save-not-settled"))
    if not evidence.run_id or not evidence.session_transition_id:
        issues.append(_issue("exam-save-identity-incomplete"))
    return state, digest, issues


def _capture_context(
    capture: object,
    *,
    evidence: AuditionLocalSaveStateEvidence,
    report: object | None,
) -> tuple[_CaptureContext | None, list[LegalCandidateGateIssue]]:
    issues: list[LegalCandidateGateIssue] = []
    try:
        raw = _mapping_or_attributes(capture)
    except Exception as error:
        return None, [_issue("capture-invalid", f"{type(error).__name__}: {error}")]

    stale = raw.get("stale", raw.get("is_stale"))
    if stale is True:
        issues.append(_issue("capture-stale-flag"))
    if stale is not None and not isinstance(stale, bool):
        issues.append(_issue("capture-stale-flag-invalid"))
    settled = raw.get("settled")
    if settled is False:
        issues.append(_issue("capture-not-settled"))
    if settled is not None and not isinstance(settled, bool):
        issues.append(_issue("capture-settled-flag-invalid"))

    timestamp_value = raw.get("timestamp", raw.get("captured_at"))
    try:
        timestamp = _finite_number(timestamp_value, "capture.timestamp")
    except ValueError as error:
        issues.append(_issue("capture-timestamp-invalid", error))
        timestamp = 0.0
    if timestamp <= 0:
        issues.append(_issue("capture-timestamp-invalid", "timestamp must be positive"))

    # A caller must explicitly bind this frame to the settled ExamSave.  A
    # timestamp/window tuple alone cannot distinguish a fresh frame from a
    # stale image from the previous turn.
    evidence_digest = evidence.digest()
    bindings: list[tuple[str, str]] = []
    for key in ("evidence_digest", "exam_save_evidence_digest", "local_save_digest"):
        value = raw.get(key)
        if value is not None:
            bindings.append((key, str(value).lower()))
    for key in ("exam_save_sha256", "source_sha256"):
        value = raw.get(key)
        if value is not None:
            bindings.append((key, str(value).lower()))
    transition = raw.get("session_transition_id", raw.get("transition_id"))
    if transition is not None:
        bindings.append(("session_transition_id", str(transition)))
    run_id = raw.get("run_id")
    step_context_id = raw.get("step_context_id")
    if run_id is not None or step_context_id is not None:
        bindings.append(("run_step_context", f"{run_id}:{step_context_id}"))

    identity_matches: list[str] = []
    for key, value in bindings:
        if key in {"evidence_digest", "exam_save_evidence_digest", "local_save_digest"}:
            if value == evidence_digest:
                identity_matches.append(value)
            else:
                issues.append(_issue("capture-exam-save-mismatch", f"{key} does not equal retained evidence digest"))
        elif key in {"exam_save_sha256", "source_sha256"}:
            if value == evidence.source_sha256:
                identity_matches.append(value)
            else:
                issues.append(_issue("capture-exam-save-mismatch", f"{key} does not equal ExamSave source hash"))
        elif key == "session_transition_id":
            if value == evidence.session_transition_id:
                identity_matches.append(value)
            else:
                issues.append(_issue("capture-transition-mismatch"))
        else:
            if run_id == evidence.run_id and step_context_id == evidence.step_context_id:
                identity_matches.append(value)
            else:
                issues.append(_issue("capture-context-mismatch"))
    if not identity_matches:
        issues.append(_issue("capture-exam-save-unbound", "provide evidence_digest, exam_save_sha256, session_transition_id, or run_id+step_context_id"))

    # If a path is present, a missing file is a stale/unavailable frame.  An
    # omitted path remains valid for pure synthetic/log fixtures whose binding
    # digest is explicit; production LiveCapture normally supplies png_path.
    path_value = raw.get("png_path", raw.get("source_path", raw.get("source")))
    if path_value is not None:
        try:
            path = Path(_nonempty_text(path_value, "capture path"))
            if not path.is_file():
                issues.append(_issue("capture-file-missing", str(path)))
        except ValueError as error:
            issues.append(_issue("capture-file-invalid", error))

    report_width = _read_field(report, "image_width", "width") if report is not None else None
    report_height = _read_field(report, "image_height", "height") if report is not None else None
    width_value = raw.get("width", report_width)
    height_value = raw.get("height", report_height)
    try:
        width = _positive_int(width_value, "capture.width")
        height = _positive_int(height_value, "capture.height")
    except ValueError as error:
        issues.append(_issue("capture-dimensions-invalid", error))
        width, height = 0, 0
    if (width, height) != (CANONICAL_WIDTH, CANONICAL_HEIGHT):
        issues.append(_issue("capture-dimensions-not-canonical", f"expected={CANONICAL_WIDTH}x{CANONICAL_HEIGHT}:actual={width}x{height}"))

    hwnd_value = raw.get("hwnd")
    pid_value = raw.get("pid")
    # hwnd/pid are required for real captures.  Synthetic fixtures may omit
    # them, but if either is supplied both must be positive integers.
    if (hwnd_value is None) != (pid_value is None):
        issues.append(_issue("capture-window-identity-incomplete"))
    if hwnd_value is not None and pid_value is not None:
        try:
            _positive_int(hwnd_value, "capture.hwnd")
            _positive_int(pid_value, "capture.pid")
        except ValueError as error:
            issues.append(_issue("capture-window-identity-invalid", error))

    identity = identity_matches[0] if identity_matches else ""
    if not issues:
        return _CaptureContext(width, height, identity, timestamp), []
    return None, issues


def _normalize_detection(value: object, index: int) -> _NormalizedDetection:
    raw = _mapping_or_attributes(value)
    label = _nonempty_text(raw.get("label"), f"detector[{index}].label")
    label = LABEL_ALIASES.get(label, label)
    confidence_raw = raw.get("confidence", raw.get("score"))
    confidence: float | None = None
    if confidence_raw is not None:
        confidence = _finite_number(confidence_raw, f"detector[{index}].confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"detector[{index}].confidence must be between zero and one")
    box_raw = raw.get("box")
    if box_raw is None:
        box_raw = {key: raw.get(key) for key in ("x", "y", "width", "height")}
    box = _canonical_box(box_raw, f"detector[{index}].box")
    return _NormalizedDetection(label, confidence, box, index)


def _intersection_area(left: CanonicalBox, right: CanonicalBox) -> int:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[0] + left[2], right[0] + right[2])
    y2 = min(left[1] + left[3], right[1] + right[3])
    return max(0, x2 - x1) * max(0, y2 - y1)


def _center(box: CanonicalBox) -> tuple[float, float]:
    return box[0] + box[2] / 2.0, box[1] + box[3] / 2.0


def _slot_for_box(box: CanonicalBox, fixed: Sequence[CanonicalBox]) -> tuple[int, ...]:
    center_x, center_y = _center(box)
    matches = []
    for index, (left, top, right, bottom) in enumerate(fixed):
        if left <= center_x < right and top <= center_y < bottom:
            matches.append(index)
    return tuple(matches)


def _benign_adjacent_slot_overlap(
    left: CanonicalBox,
    right: CanonicalBox,
    fixed: Sequence[CanonicalBox],
) -> bool:
    """Accept the small edge overlap produced by adjacent Maa card boxes.

    The 3.3.0 detector commonly returns full-card boxes whose borders cross
    by a few pixels.  Their centers still map uniquely to neighboring fixed
    slots, so rejecting the shared border loses an otherwise complete legal
    set.  Narrow/ambiguous boxes remain rejected here and are handled by the
    existing one-to-one slot checks below.
    """

    left_slots = _slot_for_box(left, fixed)
    right_slots = _slot_for_box(right, fixed)
    if (
        len(left_slots) != 1
        or len(right_slots) != 1
        or abs(left_slots[0] - right_slots[0]) != 1
    ):
        return False
    expected_width = min(fixed[left_slots[0]][2] - fixed[left_slots[0]][0], fixed[right_slots[0]][2] - fixed[right_slots[0]][0])
    if min(left[2], right[2]) < expected_width * 0.75:
        return False
    overlap_width = max(
        0,
        min(left[0] + left[2], right[0] + right[2])
        - max(left[0], right[0]),
    )
    overlap_height = max(
        0,
        min(left[1] + left[3], right[1] + right[3])
        - max(left[1], right[1]),
    )
    return bool(
        overlap_width <= min(left[2], right[2]) * 0.25
        and overlap_height >= min(left[3], right[3]) * 0.75
    )


def gate_legal_candidates(
    evidence: AuditionLocalSaveStateEvidence | LocalSaveExamState,
    capture: object,
    detections: Iterable[object] | object,
    *,
    expected_plan_type: str | None = None,
    fixed_hand_slot_boxes: Mapping[int, tuple[CanonicalBox, ...]] | None = None,
    card_y_min: int = CARD_Y_MIN,
    card_y_max: int = CARD_Y_MAX,
) -> LegalCandidateGateResult:
    """Join all detector results to the ordered Hand or abstain.

    ``detections`` may be a :class:`DetectionReport`, a sequence of
    ``CardDetection`` values, or Maa-like result objects exposing ``label``
    and ``box``.  No detector is run here.  A capture must carry an explicit
    retained ExamSave binding (digest, source hash, transition id, or exact
    run/context pair); otherwise the result is deliberately not publishable.
    """

    issues: list[LegalCandidateGateIssue] = []
    state, evidence_digest, state_issues = _state_from_evidence(evidence)
    issues.extend(state_issues)
    if expected_plan_type is not None:
        expected_plan_type = _normalize_plan_type(expected_plan_type)
        if expected_plan_type is None:
            issues.append(_issue("expected-plan-type-invalid"))
    plan_type = None if state is None else _extract_plan_type(state)
    if state is not None and plan_type is None:
        issues.append(_issue("exam-save-plan-type-invalid"))
    if plan_type is not None and expected_plan_type is not None and plan_type != expected_plan_type:
        issues.append(_issue("exam-save-plan-type-mismatch", f"expected={expected_plan_type}:actual={plan_type}"))

    hand = () if state is None else tuple(state.zones.hand)
    hand_count = len(hand)
    if hand_count == 0:
        issues.append(_issue("hand-empty"))
    elif hand_count > 5:
        issues.append(_issue("hand-count-overflow", f"count={hand_count}"))
    elif fixed_hand_slot_boxes is not None and hand_count not in fixed_hand_slot_boxes:
        issues.append(_issue("fixed-hand-layout-missing", f"count={hand_count}"))
    if hand and len({card.guid for card in hand}) != hand_count:
        issues.append(_issue("hand-guid-duplicate"))

    report = detections if _read_field(detections, "detections") is not None else None
    raw_detections: object = detections
    if report is not None:
        raw_detections = _read_field(report, "detections")
    if isinstance(raw_detections, (str, bytes, bytearray)) or raw_detections is None:
        issues.append(_issue("detector-results-invalid"))
        raw_detection_items: tuple[object, ...] = ()
    else:
        try:
            raw_detection_items = tuple(raw_detections)  # type: ignore[arg-type]
        except TypeError as error:
            issues.append(_issue("detector-results-invalid", error))
            raw_detection_items = ()
    normalized: list[_NormalizedDetection] = []
    for index, item in enumerate(raw_detection_items):
        try:
            normalized.append(_normalize_detection(item, index))
        except (TypeError, ValueError) as error:
            issues.append(_issue("detector-box-invalid", f"index={index}:{error}"))
    try:
        capture_context, capture_issues = _capture_context(
            capture, evidence=evidence if isinstance(evidence, AuditionLocalSaveStateEvidence) else _synthetic_envelope_error(), report=report
        )
    except Exception as error:
        capture_context, capture_issues = None, [_issue("capture-invalid", f"{type(error).__name__}: {error}")]
    issues.extend(capture_issues)

    try:
        y_min = _positive_int(card_y_min, "card_y_min")
        y_max = _positive_int(card_y_max, "card_y_max")
    except ValueError as error:
        issues.append(_issue("card-y-range-invalid", error))
        y_min, y_max = CARD_Y_MIN, CARD_Y_MAX
    if y_min > y_max:
        issues.append(_issue("card-y-range-invalid", "minimum exceeds maximum"))

    # Only boxes in Maa's card y band participate.  All of them are retained;
    # overlays are never deduplicated.
    band = [item for item in normalized if y_min <= item.box[1] <= y_max]
    for item in band:
        if item.label not in DETECTOR_LABELS:
            issues.append(_issue("detector-label-invalid", f"index={item.original_index}:label={item.label}"))
    if len(band) != hand_count:
        issues.append(_issue("detector-hand-count-mismatch", f"detected={len(band)}:hand={hand_count}"))
    if fixed_hand_slot_boxes is None:
        fixed_hand_slot_boxes = _fixed_hand_slot_boxes()
    fixed = tuple(fixed_hand_slot_boxes.get(hand_count, ()))
    if hand_count and len(fixed) != hand_count:
        issues.append(_issue("fixed-hand-layout-invalid", f"count={hand_count}:boxes={len(fixed)}"))
    for left_index, left in enumerate(band):
        for right in band[left_index + 1 :]:
            if (
                _intersection_area(left.box, right.box) > 0
                and not _benign_adjacent_slot_overlap(
                    left.box,
                    right.box,
                    fixed,
                )
            ):
                issues.append(_issue("detector-box-overlap", f"indexes={left.original_index},{right.original_index}"))

    assignments: list[tuple[_NormalizedDetection, int]] = []
    if len(fixed) == hand_count and hand_count:
        for item in band:
            slots = _slot_for_box(item.box, fixed)
            if len(slots) != 1:
                issues.append(_issue("detector-box-not-unique-slot", f"index={item.original_index}:slots={slots}"))
            else:
                assignments.append((item, slots[0]))
        assigned_slots = [slot for _, slot in assignments]
        if len(assigned_slots) != len(set(assigned_slots)):
            issues.append(_issue("detector-slot-duplicate", f"slots={assigned_slots}"))
        if set(assigned_slots) != set(range(hand_count)):
            issues.append(_issue("detector-slot-set-incomplete", f"slots={sorted(set(assigned_slots))}:expected={list(range(hand_count))}"))

    if issues or state is None or capture_context is None or evidence_digest is None:
        return LegalCandidateGateResult(
            accepted=False,
            plan_type=plan_type,
            hand_count=hand_count,
            issues=tuple(issues),
            evidence_digest=evidence_digest,
            capture_identity=None if capture_context is None else capture_context.identity,
        )

    by_slot = {slot: item for item, slot in assignments}
    candidates = tuple(
        LegalCandidate(
            slot=slot,
            guid=hand[slot].guid,
            card_id=hand[slot].card_id,
            label=by_slot[slot].label,
            box=by_slot[slot].box,
            confidence=by_slot[slot].confidence,
        )
        for slot in range(hand_count)
    )
    return LegalCandidateGateResult(
        accepted=True,
        plan_type=plan_type,
        hand_count=hand_count,
        candidates=candidates,
        evidence_digest=evidence_digest,
        capture_identity=capture_context.identity,
    )


def _synthetic_envelope_error() -> AuditionLocalSaveStateEvidence:
    """Never called for a supported state; keeps malformed input fail-closed."""

    raise ValueError("ExamSave evidence envelope is required before capture binding")


# Verb-first aliases make the provider easy to discover from integrations
# without creating a second implementation.
audit_legal_candidates = gate_legal_candidates
resolve_legal_candidates = gate_legal_candidates
build_legal_candidate_gate = gate_legal_candidates
build_legal_candidate_set = gate_legal_candidates
derive_legal_candidate_set = gate_legal_candidates
evaluate_legal_candidate_gate = gate_legal_candidates


class LegalCandidateGate:
    """Reusable immutable-config wrapper around :func:`gate_legal_candidates`."""

    def __init__(
        self,
        *,
        expected_plan_type: str | None = None,
        fixed_hand_slot_boxes: Mapping[int, tuple[CanonicalBox, ...]] | None = None,
        card_y_min: int = CARD_Y_MIN,
        card_y_max: int = CARD_Y_MAX,
    ) -> None:
        self.expected_plan_type = expected_plan_type
        self.fixed_hand_slot_boxes = fixed_hand_slot_boxes
        self.card_y_min = card_y_min
        self.card_y_max = card_y_max

    def evaluate(
        self,
        evidence: AuditionLocalSaveStateEvidence | LocalSaveExamState,
        capture: object,
        detections: Iterable[object] | object,
    ) -> LegalCandidateGateResult:
        return gate_legal_candidates(
            evidence,
            capture,
            detections,
            expected_plan_type=self.expected_plan_type,
            fixed_hand_slot_boxes=self.fixed_hand_slot_boxes,
            card_y_min=self.card_y_min,
            card_y_max=self.card_y_max,
        )

    __call__ = evaluate


__all__ = [
    "CARD_Y_MAX",
    "CARD_Y_MIN",
    "CanonicalBox",
    "DETECTOR_LABELS",
    "LEGAL_LABELS",
    "LegalCandidate",
    "LegalCandidateGate",
    "LegalCandidateGateIssue",
    "LegalCandidateGateResult",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SUPPORTED_PLAN_TYPES",
    "audit_legal_candidates",
    "build_legal_candidate_gate",
    "build_legal_candidate_set",
    "derive_legal_candidate_set",
    "evaluate_legal_candidate_gate",
    "gate_legal_candidates",
    "resolve_legal_candidates",
]
