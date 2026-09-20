"""Plan-neutral, fail-closed inner imitation runtime.

This module is the small live boundary between the read-only leaderboard prior
and a caller-owned Maa/ExamSave action adapter.  It deliberately does not
create a controller, capture a screen, run a simulator, or call a game API.
All of those operations are dependencies supplied by the caller.  The
default gate and prior are pure objects from :mod:`audition_legal_candidate_gate`
and :mod:`leaderboard_card_imitation_prior` respectively.

The runtime has one important safety property: it is *one way*.  A step may
become an imitation card dispatch only after all of the following are proved
for the same boundary:

* a settled, complete ExamSave evidence record with no pending queue;
* a fresh canonical capture bound to that ExamSave and the same HWND/PID;
* the complete ``ProduceRecognitionCards`` ``all_results`` sequence passes the
  legal-candidate gate;
* a non-abstaining, complete ``candidate_set_kind='legal'`` prior rank; and
* the selected action key maps to one or more settled GUID/slot candidates;
  equal complete semantic signatures may share a key, while differing or
  unknown same-ID instances retain signature/occurrence keys.

If any check abstains, or if CAS/dispatch reports a problem, this object calls
the injected ``run_maa_baseline_exam`` callable once and remains in fallback
mode.  Later calls never attempt to return to imitation or submit another
click.  An empty prior therefore behaves exactly like the existing Maa
completion baseline: the baseline is selected immediately and no imitation
dependency is touched.

The fixed-slot dispatcher is intentionally a dependency rather than an
import-time controller.  A production adapter can wrap
``MaaExamSaveCardPlayDispatcher`` (or the Plan 3 fixed-slot driver) and perform
its own ExamSave CAS; tests can provide a recorder that never touches a game.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import inspect
import json
import math
from pathlib import Path
import time
from typing import Any, Protocol, TypeAlias

from .audition_legal_candidate_gate import (
    CanonicalBox,
    LegalCandidate,
    LegalCandidateGateResult,
    gate_legal_candidates,
)
from .leaderboard_card_imitation_prior import (
    ImitationRanking,
    LeaderboardCardImitationPrior,
    candidate_action_keys,
    candidate_action_keys_aligned,
    instance_signature,
)


SCHEMA = "gkms.generic-inner-policy-runtime.v2"
SCHEMA_VERSION = 2


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _field(value: object, *names: str) -> object | None:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _as_mapping(value: object, label: str = "record") -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    fields: dict[str, object] = {}
    for name in (
        "all_results",
        "detections",
        "label",
        "text",
        "name",
        "score",
        "confidence",
        "box",
        "rect",
        "x",
        "y",
        "left",
        "top",
        "width",
        "height",
        "w",
        "h",
        "timestamp",
        "captured_at",
        "width",
        "height",
        "hwnd",
        "pid",
        "evidence_digest",
        "exam_save_evidence_digest",
        "local_save_digest",
        "exam_save_sha256",
        "source_sha256",
        "session_transition_id",
        "transition_id",
        "run_id",
        "step_context_id",
        "settled",
        "stale",
        "is_stale",
        "png_path",
        "source_path",
        "source",
    ):
        if hasattr(value, name):
            fields[name] = getattr(value, name)
    if fields:
        return fields
    raise TypeError(f"{label} must be a mapping or attribute record")


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value))


class InnerRuntimeMode(StrEnum):
    """The externally visible one-way mode of the runtime."""

    IMITATION = "imitation"
    BASELINE = "maa-completion-baseline"


@dataclass(frozen=True, slots=True)
class GenericInnerPolicyIssue:
    """One fail-closed reason recorded for a step or fallback."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.code, "issue.code")
        if not isinstance(self.detail, str):
            raise TypeError("issue.detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class InnerTelemetryBinding:
    """The two independent indices in the current telemetry contract.

    ``hand_slot`` comes from the manual command's ``play_index`` (or its
    ``hand_index`` alias).  ``action_order`` comes from the normalized row's
    ``order``.  They must never be substituted for one another: hand slots
    repeat after cards are removed while action order is monotonic within the
    episode.
    """

    action_order: int | None = None
    hand_slot: int | None = None

    def __post_init__(self) -> None:
        for name in ("action_order", "hand_slot"):
            value = getattr(self, name)
            if value is not None:
                _integer(value, f"telemetry.{name}")

    def to_dict(self) -> dict[str, int | None]:
        return {
            "action_order": self.action_order,
            "hand_slot": self.hand_slot,
        }


def _consistent_integer(
    raw: Mapping[str, object],
    names: Sequence[str],
    label: str,
) -> int | None:
    values: list[int] = []
    for name in names:
        if name not in raw or raw[name] is None:
            continue
        values.append(_integer(raw[name], f"{label}.{name}"))
    if not values:
        return None
    if len(set(values)) != 1:
        raise ValueError(f"{label} aliases disagree")
    return values[0]


def telemetry_binding(value: Mapping[str, object] | object) -> InnerTelemetryBinding:
    """Extract telemetry identity without conflating action order and slot.

    The v2 adapter emits ``order`` as the normalized action order and
    ``play_index`` as the original Hand slot.  ``hand_index``/``action_order``
    are accepted compatibility aliases only when they agree with their own
    field.  No list position is used as a fallback.
    """

    raw = _as_mapping(value, "telemetry")
    return InnerTelemetryBinding(
        action_order=_consistent_integer(
            raw,
            ("order", "action_order", "actionOrder"),
            "telemetry.action_order",
        ),
        hand_slot=_consistent_integer(
            raw,
            ("play_index", "playIndex", "hand_index", "handIndex", "hand_slot"),
            "telemetry.hand_slot",
        ),
    )


# Descriptive aliases used by callers that name this a telemetry adapter.
extract_telemetry_binding = telemetry_binding
parse_telemetry_binding = telemetry_binding


@dataclass(frozen=True, slots=True)
class InnerCardDispatchRequest:
    """One card dispatch request after every read-only gate has passed."""

    plan_type: str | None
    action_order: int | None
    hand_slot: int
    card_id: str
    guid: str
    box: CanonicalBox
    evidence: object
    capture: object
    gate: LegalCandidateGateResult
    ranking: ImitationRanking
    # Occurrence-aware identity.  ``candidate_key`` is stable across runs for
    # equivalent semantic instances; ``guid``/slot remain the authoritative
    # runtime target for the one dispatch.
    candidate_key: str | None = None
    instance_signature: str | None = None
    occurrence: int | None = None

    def __post_init__(self) -> None:
        if self.plan_type is not None:
            _text(self.plan_type, "request.plan_type")
        _integer(self.hand_slot, "request.hand_slot")
        _text(self.card_id, "request.card_id")
        _text(self.guid, "request.guid")
        if self.candidate_key is not None:
            _text(self.candidate_key, "request.candidate_key")
        if self.instance_signature is not None:
            _text(self.instance_signature, "request.instance_signature")
        if self.occurrence is not None:
            _integer(self.occurrence, "request.occurrence")
        if len(self.box) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) for value in self.box
        ):
            raise ValueError("request.box must be a four-integer box")
        if not isinstance(self.gate, LegalCandidateGateResult):
            raise TypeError("request.gate must be LegalCandidateGateResult")
        if not isinstance(self.ranking, ImitationRanking):
            raise TypeError("request.ranking must be ImitationRanking")

    @property
    def candidate(self) -> LegalCandidate:
        for value in self.gate.candidates:
            if value.slot == self.hand_slot and value.guid == self.guid:
                return value
        # Construction is normally reached only from a validated candidate;
        # this defensive path keeps a malformed injected dispatcher request
        # from looking like a legitimate card.
        raise ValueError("request candidate is absent from the accepted gate")

    @property
    def slot(self) -> int:
        return self.hand_slot

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_type": self.plan_type,
            "action_order": self.action_order,
            "hand_slot": self.hand_slot,
            "play_index": self.hand_slot,
            "card_id": self.card_id,
            "guid": self.guid,
            "candidate_key": self.candidate_key,
            "instance_signature": self.instance_signature,
            "occurrence": self.occurrence,
            "box": list(self.box),
        }


@dataclass(frozen=True, slots=True)
class GenericInnerPolicyStepResult:
    """Immutable result of one attempted inner step."""

    mode: InnerRuntimeMode
    dispatched: bool
    baseline_called: bool
    click_count: int
    plan_type: str | None = None
    action_order: int | None = None
    hand_slot: int | None = None
    card_id: str | None = None
    guid: str | None = None
    gate: LegalCandidateGateResult | None = None
    ranking: ImitationRanking | None = None
    baseline_result: object | None = None
    issues: tuple[GenericInnerPolicyIssue, ...] = ()
    dispatch_result: object | None = None
    candidate_key: str | None = None
    instance_signature: str | None = None
    occurrence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, InnerRuntimeMode):
            raise TypeError("result.mode must be InnerRuntimeMode")
        if type(self.dispatched) is not bool or type(self.baseline_called) is not bool:
            raise TypeError("result dispatched/baseline_called must be bool")
        _integer(self.click_count, "result.click_count")
        if self.dispatched and self.mode is not InnerRuntimeMode.IMITATION:
            raise ValueError("baseline mode cannot be dispatched")
        if self.baseline_called and self.mode is not InnerRuntimeMode.BASELINE:
            raise ValueError("baseline_called requires baseline mode")
        issues = tuple(self.issues)
        if any(not isinstance(value, GenericInnerPolicyIssue) for value in issues):
            raise TypeError("result.issues must contain GenericInnerPolicyIssue")
        if self.dispatched and self.click_count != 1:
            raise ValueError("a dispatched result must represent exactly one card")
        if not self.dispatched and self.click_count != 0:
            raise ValueError("an undispatched result cannot report a click")
        if self.candidate_key is not None:
            _text(self.candidate_key, "result.candidate_key")
        if self.instance_signature is not None:
            _text(self.instance_signature, "result.instance_signature")
        if self.occurrence is not None:
            _integer(self.occurrence, "result.occurrence")
        object.__setattr__(self, "issues", issues)

    @property
    def accepted(self) -> bool:
        return self.dispatched

    @property
    def fallback(self) -> bool:
        return self.mode is InnerRuntimeMode.BASELINE

    @property
    def abstained(self) -> bool:
        return not self.dispatched

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(value.code for value in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode.value,
            "dispatched": self.dispatched,
            "accepted": self.accepted,
            "fallback": self.fallback,
            "baseline_called": self.baseline_called,
            "click_count": self.click_count,
            "plan_type": self.plan_type,
            "action_order": self.action_order,
            "hand_slot": self.hand_slot,
            "play_index": self.hand_slot,
            "card_id": self.card_id,
            "guid": self.guid,
            "candidate_key": self.candidate_key,
            "instance_signature": self.instance_signature,
            "occurrence": self.occurrence,
            "gate": None if self.gate is None else self.gate.to_dict(),
            "ranking": None if self.ranking is None else self.ranking.to_dict(),
            "baseline_result": self.baseline_result,
            "issues": [value.to_dict() for value in self.issues],
            "dispatch_result": self.dispatch_result,
        }


@dataclass(frozen=True, slots=True)
class GenericInnerPolicyRunResult:
    """Optional aggregate returned by :meth:`GenericInnerPolicyRuntimeV2.run`."""

    steps: tuple[GenericInnerPolicyStepResult, ...]
    mode: InnerRuntimeMode
    baseline_result: object | None = None

    def __post_init__(self) -> None:
        values = tuple(self.steps)
        if any(not isinstance(value, GenericInnerPolicyStepResult) for value in values):
            raise TypeError("run steps must contain GenericInnerPolicyStepResult")
        if not isinstance(self.mode, InnerRuntimeMode):
            raise TypeError("run mode must be InnerRuntimeMode")
        object.__setattr__(self, "steps", values)

    @property
    def fallback(self) -> bool:
        return self.mode is InnerRuntimeMode.BASELINE

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "mode": self.mode.value,
            "fallback": self.fallback,
            "steps": [value.to_dict() for value in self.steps],
            "baseline_result": self.baseline_result,
        }


class InnerCardDispatcher(Protocol):
    """Caller-owned fixed-slot/CAS dispatcher contract."""

    def __call__(self, request: InnerCardDispatchRequest) -> object: ...


class BaselineRunner(Protocol):
    """The existing ``MaaWin32Session.run_maa_baseline_exam`` boundary."""

    def __call__(self) -> object: ...


GateCallable: TypeAlias = Callable[..., LegalCandidateGateResult]
RankCallable: TypeAlias = Callable[..., ImitationRanking]
EvidenceReader: TypeAlias = Callable[[], object]
CaptureReader: TypeAlias = Callable[..., object]
RecognitionReader: TypeAlias = Callable[..., object]
CasAuthorizer: TypeAlias = Callable[..., object]
FeatureProvider: TypeAlias = Callable[..., Mapping[str, object]]
InstanceSelector: TypeAlias = Callable[..., object]


@dataclass(frozen=True, slots=True)
class GenericInnerPolicyRuntimeV2Dependencies:
    """Explicit dependency bundle for construction in host applications.

    The bundle contains callables only; constructing it never starts Maa or
    reads a save.  ``baseline_runner`` should normally be the already-bound
    ``MaaWin32Session.run_maa_baseline_exam`` method, while
    ``fixed_slot_dispatcher`` should be a host adapter around the existing
    ExamSave/CAS fixed-slot dispatcher.
    """

    baseline_runner: BaselineRunner | None = None
    fixed_slot_dispatcher: InnerCardDispatcher | None = None
    cas_authorizer: CasAuthorizer | None = None
    evidence_reader: EvidenceReader | None = None
    capture_reader: CaptureReader | None = None
    recognition_reader: RecognitionReader | None = None
    feature_provider: FeatureProvider | None = None
    instance_selector: InstanceSelector | None = None

    def __post_init__(self) -> None:
        for name in (
            "baseline_runner",
            "fixed_slot_dispatcher",
            "cas_authorizer",
            "evidence_reader",
            "capture_reader",
            "recognition_reader",
            "feature_provider",
            "instance_selector",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value) and name != "fixed_slot_dispatcher":
                raise TypeError(f"dependencies.{name} must be callable or None")

    def to_dict(self) -> dict[str, bool]:
        return {
            name: getattr(self, name) is not None
            for name in (
                "baseline_runner",
                "fixed_slot_dispatcher",
                "cas_authorizer",
                "evidence_reader",
                "capture_reader",
                "recognition_reader",
                "feature_provider",
                "instance_selector",
            )
        }


def _issue(code: str, detail: object = "") -> GenericInnerPolicyIssue:
    return GenericInnerPolicyIssue(code, str(detail))


def _extract_evidence_state(evidence: object) -> object:
    # A Plan2 stable waiter carries ``first``/``second``.  A wrapper caller
    # may inject that record directly; only its second read can be used as the
    # current pre-action state.
    second = _field(evidence, "second")
    if second is not None and _field(evidence, "first") is not None:
        return second
    state = _field(evidence, "state")
    return state if state is not None else evidence


def _pending_queue_reasons(evidence: object) -> tuple[GenericInnerPolicyIssue, ...]:
    state = _extract_evidence_state(evidence)
    reasons: list[GenericInnerPolicyIssue] = []
    explicit_settled = _field(evidence, "settled")
    if explicit_settled is False or _field(state, "settled") is False:
        reasons.append(_issue("exam-save-not-settled"))
    for owner, value in (("evidence", evidence), ("state", state)):
        for name in ("pending_queue", "pending_commands", "command_queue"):
            queue = _field(value, name)
            if queue is not None:
                if isinstance(queue, (str, bytes, bytearray)) or not isinstance(queue, Sequence):
                    reasons.append(_issue("pending-queue-invalid", f"{owner}.{name}"))
                elif len(queue):
                    reasons.append(_issue("pending-queue"))
        command_list = _field(value, "command_list")
        if command_list is not None:
            try:
                if isinstance(command_list, Sequence) and not isinstance(command_list, (str, bytes, bytearray)) and len(command_list):
                    reasons.append(_issue("pending-queue"))
                elif hasattr(command_list, "to_value") and command_list.to_value():
                    reasons.append(_issue("pending-queue"))
            except Exception:
                reasons.append(_issue("pending-queue-invalid", f"{owner}.command_list"))
        playing = _field(value, "playing_card")
        if playing is not None:
            reasons.append(_issue("pending-playing-card"))
    runtime = _field(state, "root_runtime")
    actionable = _field(state, "is_native_actionable_settled")
    if actionable is False:
        reasons.append(_issue("exam-save-not-settled"))
    if runtime is not None:
        runtime_actionable = _field(runtime, "command_list_is_empty")
        if runtime_actionable is False:
            reasons.append(_issue("pending-queue"))
        if _field(runtime, "is_exam_end_complete") is True:
            reasons.append(_issue("exam-save-terminal"))
    # A typed evidence object must expose an explicit native settled property;
    # a test double may instead use ``settled=True``.  Missing both is not
    # silently accepted when no custom gate is supplied (the gate itself will
    # also reject an untyped record).
    if actionable is None and explicit_settled is None and _field(state, "settled") is None:
        if hasattr(state, "is_native_actionable_settled"):
            reasons.append(_issue("exam-save-settled-flag-invalid"))
    return tuple(_dedupe_issues(reasons))


def _dedupe_issues(values: Iterable[GenericInnerPolicyIssue]) -> tuple[GenericInnerPolicyIssue, ...]:
    seen: set[tuple[str, str]] = set()
    output: list[GenericInnerPolicyIssue] = []
    for value in values:
        key = (value.code, value.detail)
        if key not in seen:
            seen.add(key)
            output.append(value)
    return tuple(output)


def _capture_identity(capture: object) -> tuple[int, int]:
    hwnd = _integer(_field(capture, "hwnd"), "capture.hwnd", minimum=1)
    pid = _integer(_field(capture, "pid"), "capture.pid", minimum=1)
    return hwnd, pid


def _capture_timestamp(capture: object) -> float:
    timestamp = _number(
        _field(capture, "timestamp", "captured_at"),
        "capture.timestamp",
    )
    if timestamp <= 0:
        raise ValueError("capture.timestamp must be positive")
    return timestamp


def _capture_issues(
    capture: object,
    *,
    previous_timestamp: float | None,
    bound_window: tuple[int, int] | None,
    expected_window: tuple[int, int] | None,
) -> tuple[GenericInnerPolicyIssue, ...]:
    issues: list[GenericInnerPolicyIssue] = []
    try:
        timestamp = _capture_timestamp(capture)
    except (TypeError, ValueError) as error:
        issues.append(_issue("capture-invalid", error))
        timestamp = None
    try:
        width = _integer(_field(capture, "width", "image_width"), "capture.width", minimum=1)
        height = _integer(_field(capture, "height", "image_height"), "capture.height", minimum=1)
        if (width, height) != (720, 1280):
            issues.append(_issue("capture-dimensions-not-canonical", f"expected=720x1280:actual={width}x{height}"))
    except (TypeError, ValueError) as error:
        issues.append(_issue("capture-dimensions-invalid", error))
    try:
        window = _capture_identity(capture)
    except (TypeError, ValueError) as error:
        issues.append(_issue("capture-window-identity-invalid", error))
        window = None
    if expected_window is not None and window is not None and window != expected_window:
        issues.append(_issue("capture-window-mismatch", f"expected={expected_window}:actual={window}"))
    if bound_window is not None and window is not None and window != bound_window:
        issues.append(_issue("capture-window-mismatch", f"expected={bound_window}:actual={window}"))
    if timestamp is not None and previous_timestamp is not None and timestamp <= previous_timestamp:
        issues.append(_issue("capture-not-fresh", f"previous={previous_timestamp}:actual={timestamp}"))
    stale = _field(capture, "stale", "is_stale")
    if stale is True:
        issues.append(_issue("capture-stale-flag"))
    if stale is not None and not isinstance(stale, bool):
        issues.append(_issue("capture-stale-flag-invalid"))
    settled = _field(capture, "settled")
    if settled is False:
        issues.append(_issue("capture-not-settled"))
    if settled is not None and not isinstance(settled, bool):
        issues.append(_issue("capture-settled-flag-invalid"))
    return _dedupe_issues(issues)


def _recognition_items(value: object) -> tuple[object, ...] | None:
    """Return Maa's complete ``all_results`` without dropping any row."""

    nested = _field(value, "all_results")
    if nested is None:
        nested = _field(value, "detections")
    if nested is None:
        # A direct sequence is accepted for dependency-injected tests and for
        # callers that already extracted Maa's all_results.
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return tuple(value)
        return None
    if isinstance(nested, (str, bytes, bytearray)) or not isinstance(nested, Sequence):
        return None
    return tuple(nested)


def _box_value(value: object) -> object:
    if isinstance(value, Mapping):
        if "box" in value:
            return value["box"]
        if "rect" in value:
            return value["rect"]
        if all(name in value for name in ("x", "y")) and (
            ("width" in value and "height" in value)
            or ("w" in value and "h" in value)
        ):
            return value
    box = _field(value, "box", "rect")
    if box is not None:
        if isinstance(box, Mapping) or (
            _field(box, "x", "left") is not None
            and _field(box, "y", "top") is not None
        ):
            if isinstance(box, Mapping):
                return box
            return {
                "x": _field(box, "x", "left"),
                "y": _field(box, "y", "top"),
                "width": _field(box, "width", "w"),
                "height": _field(box, "height", "h"),
            }
        return box
    return {
        "x": _field(value, "x", "left"),
        "y": _field(value, "y", "top"),
        "width": _field(value, "width", "w"),
        "height": _field(value, "height", "h"),
    }


def _normalise_recognition_item(value: object, index: int) -> Mapping[str, object]:
    raw = _as_mapping(value, f"all_results[{index}]")
    label = raw.get("label")
    if label is None:
        label = raw.get("text", raw.get("name"))
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"all_results[{index}].label is missing")
    output: dict[str, object] = dict(raw)
    output["label"] = label
    output["box"] = _box_value(value if not isinstance(value, Mapping) else raw)
    confidence = raw.get("confidence", raw.get("score"))
    if confidence is not None:
        output["confidence"] = confidence
    return output


def normalise_produce_recognition_cards(value: object) -> tuple[Mapping[str, object], ...]:
    """Normalize a Maa report/all_results sequence for the legal gate.

    The function is read-only.  It keeps every detector row, including
    ``useless`` rows and any malformed row, so the gate can abstain instead of
    silently repairing an incomplete recognition result.
    """

    values = _recognition_items(value)
    if values is None:
        raise ValueError("ProduceRecognitionCards all_results are missing")
    output: list[Mapping[str, object]] = []
    for index, item in enumerate(values):
        try:
            output.append(_normalise_recognition_item(item, index))
        except (TypeError, ValueError) as error:
            # Preserve a malformed placeholder; gate will issue
            # detector-box-invalid and reject the complete set.
            output.append({"label": "__invalid__", "box": (0, 0, 1, 1), "_error": str(error)})
    return tuple(output)


# Both spellings are kept because Maa integration code in this repository
# uses American ``normalize`` while the older adapter used British
# ``normalise`` in its prose.
normalize_produce_recognition_cards = normalise_produce_recognition_cards


def _call_reader(reader: Callable[..., object], *args: object) -> object:
    """Call an injected reader without guessing after an internal TypeError."""

    if not callable(reader):
        raise TypeError("reader must be callable")
    try:
        signature = inspect.signature(reader)
    except (TypeError, ValueError):
        return reader(*args)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if has_varargs:
        return reader(*args)
    required = sum(parameter.default is inspect.Parameter.empty for parameter in positional)
    count = min(len(args), len(positional))
    if required > count:
        # Let Python report the natural missing-argument error; this is a
        # dependency failure and the caller will take the baseline path.
        return reader(*args)
    return reader(*args[:count])


def _call_card_dependency(
    dependency: Callable[..., object],
    request: InnerCardDispatchRequest,
) -> object:
    """Invoke a CAS/dispatcher using either request- or card-shaped DI.

    The documented contract is one ``InnerCardDispatchRequest``.  A few
    existing adapters naturally expose ``(candidate, evidence, capture)``;
    recognizing those parameter names keeps the wrapper reusable without
    speculative retry calls (a retry could itself be a duplicate click).
    """

    try:
        signature = inspect.signature(dependency)
    except (TypeError, ValueError):
        return dependency(request)
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in signature.parameters.values()
    )
    if has_varargs or len(positional) <= 1:
        return dependency(request)
    first_name = positional[0].name.lower()
    if any(token in first_name for token in ("candidate", "card", "action")):
        args: tuple[object, ...] = (request.candidate, request.evidence, request.capture)
    elif any(token in first_name for token in ("request", "context", "dispatch")):
        args = (request, request.evidence, request.capture)
    else:
        # A three-argument legacy callable is conventionally card/evidence/
        # capture; this branch is still invoked exactly once.
        args = (request.candidate, request.evidence, request.capture)
    return dependency(*args[: len(positional)])


def _call_gate(
    gate: GateCallable,
    evidence: object,
    capture: object,
    detections: Sequence[Mapping[str, object]],
    *,
    expected_plan_type: str | None,
    fixed_hand_slot_boxes: Mapping[int, tuple[CanonicalBox, ...]] | None,
) -> object:
    """Call the default gate or a minimal mock gate exactly once."""

    try:
        signature = inspect.signature(gate)
    except (TypeError, ValueError):
        return gate(
            evidence,
            capture,
            detections,
            expected_plan_type=expected_plan_type,
            fixed_hand_slot_boxes=fixed_hand_slot_boxes,
        )
    parameters = tuple(signature.parameters.values())
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    names = {parameter.name for parameter in parameters}
    kwargs: dict[str, object] = {}
    if accepts_kwargs or "expected_plan_type" in names:
        kwargs["expected_plan_type"] = expected_plan_type
    if accepts_kwargs or "fixed_hand_slot_boxes" in names:
        kwargs["fixed_hand_slot_boxes"] = fixed_hand_slot_boxes
    return gate(evidence, capture, detections, **kwargs)


def _dispatch_result_ok(value: object) -> bool:
    if isinstance(value, bool):
        return value
    submitted = _field(value, "submitted", "accepted", "committed")
    if submitted is not None:
        return submitted is True
    return True


def _coerce_ranking(value: object) -> ImitationRanking:
    """Accept the typed prior result plus small structural test doubles."""

    if isinstance(value, ImitationRanking):
        return value
    raw: Mapping[str, object] | None = value if isinstance(value, Mapping) else None
    if raw is None and value is not None:
        try:
            raw = {
                name: getattr(value, name)
                for name in (
                    "ranked",
                    "scores",
                    "known_candidates",
                    "unknown_candidates",
                    "source",
                    "abstained",
                    "reason",
                    "candidate_set_kind",
                )
                if hasattr(value, name)
            }
            if not raw:
                raw = None
        except Exception:
            raw = None
    if raw is None and isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        raw = {"ranked": tuple(value), "abstained": False, "candidate_set_kind": "legal"}
    if raw is None:
        raise TypeError("ranker must return ImitationRanking-shaped data")
    ranked_raw = raw.get("ranked", ())
    if not isinstance(ranked_raw, Sequence) or isinstance(ranked_raw, (str, bytes, bytearray)):
        raise TypeError("ranking.ranked must be a sequence")
    ranked = tuple(_text(item, "ranking.ranked item") for item in ranked_raw)
    scores_raw = raw.get("scores", {})
    scores = dict(scores_raw) if isinstance(scores_raw, Mapping) else {}
    known_raw = raw.get("known_candidates", ranked)
    unknown_raw = raw.get("unknown_candidates", ())
    if not isinstance(known_raw, Sequence) or isinstance(known_raw, (str, bytes, bytearray)):
        known_raw = ranked
    if not isinstance(unknown_raw, Sequence) or isinstance(unknown_raw, (str, bytes, bytearray)):
        unknown_raw = ()
    source = raw.get("source", "history")
    if source not in {"history", "stage-turn", "none"}:
        source = "none"
    abstained = raw.get("abstained", False)
    if not isinstance(abstained, bool):
        raise TypeError("ranking.abstained must be bool")
    kind = raw.get("candidate_set_kind", "legal")
    if kind not in {"legal", "settled_hand"}:
        kind = "legal"
    return ImitationRanking(
        ranked=ranked,
        scores=scores,
        known_candidates=tuple(_text(item, "ranking.known_candidates item") for item in known_raw),
        unknown_candidates=tuple(_text(item, "ranking.unknown_candidates item") for item in unknown_raw),
        source=source,
        abstained=abstained,
        reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
        candidate_set_kind=kind,
    )


def _json_value(value: object) -> object:
    """Project a CanonicalJsonValue or JSON-compatible runtime field."""

    if hasattr(value, "to_value") and callable(getattr(value, "to_value")):
        value = value.to_value()
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _card_instance_fingerprint(card: object) -> str | None:
    """Return the shared non-GUID semantic identity for one Hand card.

    The prior owns the canonical field normalization so offline training and
    runtime selection cannot accidentally disagree about customize/effect
    equivalence.  ``None`` is an unknown signature and therefore never proves
    that two duplicate IDs are interchangeable.
    """

    return instance_signature(card)


def _hand_cards(evidence: object) -> tuple[object, ...] | None:
    state = _extract_evidence_state(evidence)
    zones = _field(state, "zones")
    hand = _field(zones, "hand") if zones is not None else None
    if hand is None or isinstance(hand, (str, bytes, bytearray)):
        return None
    try:
        values = tuple(hand)
    except TypeError:
        return None
    return values if values else None


def _runtime_candidate_records(
    evidence: object,
    candidates: Sequence[LegalCandidate],
) -> tuple[dict[str, object], ...]:
    """Attach semantic instance identity to every accepted gate candidate.

    ``LegalCandidate`` intentionally stays a small detector/slot record.  The
    settled ExamSave hand is the authoritative source for upgrade, customize,
    and effect state, so enrich candidates only after the gate has accepted the
    complete hand.  A missing enrichment remains explicit unknown and is later
    represented by an occurrence key rather than being merged by card ID.
    """

    cards = _hand_cards(evidence)
    output: list[dict[str, object]] = []
    occurrences: dict[str, int] = {}
    for candidate in candidates:
        record = candidate.to_dict()
        record["card_id"] = candidate.card_id
        record["guid"] = candidate.guid
        record["slot"] = candidate.slot
        record["occurrence"] = occurrences.get(candidate.card_id, 0)
        occurrences[candidate.card_id] = record["occurrence"] + 1  # type: ignore[operator]
        card: object | None = None
        if cards is not None and 0 <= candidate.slot < len(cards):
            possible = cards[candidate.slot]
            if (
                _field(possible, "guid") == candidate.guid
                and _field(possible, "card_id", "id") == candidate.card_id
            ):
                card = possible
        if card is None:
            # Presence of this key prevents candidate_action_keys from trying
            # to infer a signature from the detector record itself.
            record["instance_signature"] = None
        else:
            record["instance_signature"] = instance_signature(card)
        output.append(record)
    return tuple(output)


def _candidate_instance_metadata(
    evidence: object,
    candidate: LegalCandidate,
    candidates: Sequence[LegalCandidate],
) -> tuple[str | None, int | None]:
    """Return signature/occurrence metadata for a selected gate candidate."""

    records = _runtime_candidate_records(evidence, candidates)
    for record in records:
        if record.get("guid") == candidate.guid and record.get("slot") == candidate.slot:
            signature = record.get("instance_signature")
            occurrence = record.get("occurrence")
            return (
                signature if isinstance(signature, str) else None,
                occurrence if isinstance(occurrence, int) and not isinstance(occurrence, bool) else None,
            )
    return None, None


def _resolve_selected_instance(
    evidence: object,
    matches: Sequence[LegalCandidate],
    *,
    selector: InstanceSelector | None = None,
) -> tuple[LegalCandidate | None, str]:
    """Resolve one card-ID's GUIDs without arbitrary instance selection."""

    values = tuple(matches)
    if len(values) == 1:
        return values[0], "unique"
    if not values:
        return None, "missing"
    if selector is not None:
        try:
            raw = _call_reader(selector, evidence, values)
        except Exception:
            return None, "selector-failed"
        selected: LegalCandidate | None = None
        if isinstance(raw, LegalCandidate):
            selected = raw
        elif isinstance(raw, str):
            selected = next((value for value in values if value.guid == raw), None)
        elif isinstance(raw, int) and not isinstance(raw, bool):
            selected = next((value for value in values if value.slot == raw), None)
        if selected is None or selected not in values:
            return None, "selector-unresolved"
        return selected, "selector"
    cards = _hand_cards(evidence)
    if cards is None:
        return None, "instance-evidence-missing"
    by_slot: dict[int, object] = {}
    for value in values:
        if not 0 <= value.slot < len(cards):
            return None, "instance-slot-unresolved"
        by_slot[value.slot] = cards[value.slot]
    fingerprints = tuple(_card_instance_fingerprint(by_slot[value.slot]) for value in values)
    if all(value is not None for value in fingerprints) and len(set(fingerprints)) == 1:
        # Equivalent instances are interchangeable; keep the ordered Hand
        # slot as the deterministic tie-breaker.
        return min(values, key=lambda value: value.slot), "equivalent"

    def preference(value: LegalCandidate) -> tuple[int, int, int, int, int]:
        card = by_slot[value.slot]
        runtime = _field(card, "runtime_state", "runtime")

        def integer(raw: object, default: int = 0) -> int:
            return raw if isinstance(raw, int) and not isinstance(raw, bool) else default

        effective = integer(
            _field(card, "effective_upgrade", "upgrade", "upgrade_count")
        )
        temporary = integer(
            _field(card, "temporary_upgrade", "temporaryUpgrade")
        )
        base = integer(_field(card, "base_upgrade", "baseUpgrade"))
        play_count = integer(
            _field(runtime, "play_count", "playCount")
            if runtime is not None
            else _field(card, "play_count", "playCount")
        )
        # Prefer the stronger copy, then the less-used copy, then the stable
        # ordered Hand slot.  GUID is never treated as a value signal.
        return (-effective, -temporary, -base, play_count, value.slot)

    return min(values, key=preference), "deterministic-instance-quality"


class GenericInnerPolicyRuntimeV2:
    """Plan-neutral one-card imitation runtime with a one-way baseline.

    The class has no default controller or file reader.  Use the explicit
    values on :meth:`step` in tests/offline adapters, or inject readers for a
    host that already owns the Maa/ExamSave lifecycle.
    """

    def __init__(
        self,
        *,
        dependencies: GenericInnerPolicyRuntimeV2Dependencies | None = None,
        prior: LeaderboardCardImitationPrior | None = None,
        imitation_prior: LeaderboardCardImitationPrior | None = None,
        baseline_runner: BaselineRunner | None = None,
        fixed_slot_dispatcher: InnerCardDispatcher | None = None,
        card_dispatcher: InnerCardDispatcher | None = None,
        cas_authorizer: CasAuthorizer | None = None,
        legal_gate: GateCallable = gate_legal_candidates,
        legal_candidate_gate: GateCallable | None = None,
        ranker: RankCallable | None = None,
        evidence_reader: EvidenceReader | None = None,
        capture_reader: CaptureReader | None = None,
        recognition_reader: RecognitionReader | None = None,
        feature_provider: FeatureProvider | None = None,
        instance_selector: InstanceSelector | None = None,
        card_instance_selector: InstanceSelector | None = None,
        expected_plan_type: str | None = None,
        expected_window: tuple[int, int] | None = None,
        fixed_hand_slot_boxes: Mapping[int, tuple[CanonicalBox, ...]] | None = None,
        require_fresh_capture: bool = True,
        run_maa_baseline_exam: BaselineRunner | None = None,
        dispatcher: InnerCardDispatcher | None = None,
    ) -> None:
        if imitation_prior is not None:
            if prior is not None:
                raise ValueError("prior and imitation_prior are aliases; pass one")
            prior = imitation_prior
        if legal_candidate_gate is not None:
            if legal_gate is not gate_legal_candidates:
                raise ValueError("legal_gate and legal_candidate_gate are aliases; pass one")
            legal_gate = legal_candidate_gate
        if card_instance_selector is not None:
            if instance_selector is not None:
                raise ValueError(
                    "instance_selector and card_instance_selector are aliases; pass one"
                )
            instance_selector = card_instance_selector
        if run_maa_baseline_exam is not None:
            if baseline_runner is not None:
                raise ValueError("baseline_runner and run_maa_baseline_exam are aliases; pass one")
            baseline_runner = run_maa_baseline_exam
        if dispatcher is not None:
            if fixed_slot_dispatcher is not None or card_dispatcher is not None:
                raise ValueError("dispatcher and fixed-slot dispatcher are aliases; pass one")
            fixed_slot_dispatcher = dispatcher
        if dependencies is not None:
            if not isinstance(dependencies, GenericInnerPolicyRuntimeV2Dependencies):
                raise TypeError(
                    "dependencies must be GenericInnerPolicyRuntimeV2Dependencies"
                )
            if any(
                value is not None
                for value in (
                    baseline_runner,
                    fixed_slot_dispatcher,
                    card_dispatcher,
                    cas_authorizer,
                    evidence_reader,
                    capture_reader,
                    recognition_reader,
                    feature_provider,
                    instance_selector,
                )
            ):
                raise ValueError(
                    "dependencies cannot be combined with individual dependency arguments"
                )
            baseline_runner = dependencies.baseline_runner
            fixed_slot_dispatcher = dependencies.fixed_slot_dispatcher
            cas_authorizer = dependencies.cas_authorizer
            evidence_reader = dependencies.evidence_reader
            capture_reader = dependencies.capture_reader
            recognition_reader = dependencies.recognition_reader
            feature_provider = dependencies.feature_provider
            instance_selector = dependencies.instance_selector
        if prior is not None:
            # Keep the production annotation precise while allowing a tiny
            # injected fake in unit tests.  The runtime still validates the
            # rank result below; it never treats an arbitrary object as a
            # legality source.
            raw_count = _field(prior, "observation_count")
            if raw_count is not None:
                _integer(raw_count, "prior.observation_count")
            if ranker is None and not callable(_field(prior, "rank_with_evidence")):
                raise TypeError("prior must expose rank_with_evidence")
        if baseline_runner is not None and not callable(baseline_runner):
            raise TypeError("baseline_runner must be callable or None")
        if fixed_slot_dispatcher is not None and card_dispatcher is not None:
            raise ValueError("fixed_slot_dispatcher and card_dispatcher are aliases; pass one")
        dispatcher = fixed_slot_dispatcher or card_dispatcher
        if dispatcher is not None and not callable(dispatcher) and not callable(_field(dispatcher, "dispatch")):
            raise TypeError("fixed-slot dispatcher must be callable or expose dispatch")
        if cas_authorizer is not None and not callable(cas_authorizer):
            raise TypeError("cas_authorizer must be callable or None")
        if not callable(legal_gate):
            raise TypeError("legal_gate must be callable")
        if ranker is not None and not callable(ranker):
            raise TypeError("ranker must be callable or None")
        for name, value in (
            ("evidence_reader", evidence_reader),
            ("capture_reader", capture_reader),
            ("recognition_reader", recognition_reader),
            ("feature_provider", feature_provider),
            ("instance_selector", instance_selector),
        ):
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable or None")
        if expected_plan_type is not None:
            _text(expected_plan_type, "expected_plan_type")
        if expected_window is not None:
            if len(expected_window) != 2 or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in expected_window
            ):
                raise ValueError("expected_window must be a positive (HWND, PID) pair")
            expected_window = (int(expected_window[0]), int(expected_window[1]))
        if type(require_fresh_capture) is not bool:
            raise TypeError("require_fresh_capture must be bool")
        self.prior = prior
        self.baseline_runner = baseline_runner
        self.fixed_slot_dispatcher = dispatcher
        self.cas_authorizer = cas_authorizer
        self.legal_gate = legal_gate
        self.ranker = ranker
        self.evidence_reader = evidence_reader
        self.capture_reader = capture_reader
        self.recognition_reader = recognition_reader
        self.feature_provider = feature_provider
        self.instance_selector = instance_selector
        self.expected_plan_type = expected_plan_type
        self.expected_window = expected_window
        self.fixed_hand_slot_boxes = fixed_hand_slot_boxes
        self.require_fresh_capture = require_fresh_capture
        self.mode = InnerRuntimeMode.IMITATION
        # ``baseline_runner`` is a side-effecting boundary.  Keep an explicit
        # attempted bit separate from its receipt so an exception cannot make
        # a caller retry the baseline and potentially submit it twice.
        self._baseline_attempted = False
        self._fallback_result: object | None = None
        self._fallback_issue: GenericInnerPolicyIssue | None = None
        self._bound_window: tuple[int, int] | None = expected_window
        self._last_capture_timestamp: float | None = None
        self._step_results: list[GenericInnerPolicyStepResult] = []

    @property
    def fallback_active(self) -> bool:
        return self.mode is InnerRuntimeMode.BASELINE

    @property
    def baseline_started(self) -> bool:
        return self.fallback_active

    @property
    def baseline_attempted(self) -> bool:
        """Whether the injected baseline boundary has already been entered."""

        return self._baseline_attempted

    @property
    def fallback_issue(self) -> GenericInnerPolicyIssue | None:
        return self._fallback_issue

    @property
    def fallback_result(self) -> object | None:
        return self._fallback_result

    @property
    def bound_window(self) -> tuple[int, int] | None:
        return self._bound_window

    @property
    def results(self) -> tuple[GenericInnerPolicyStepResult, ...]:
        return tuple(self._step_results)

    def _run_baseline(self, issue: GenericInnerPolicyIssue) -> object | None:
        # This assignment happens before invoking the dependency.  Even when
        # the baseline itself fails, a later caller cannot switch back to an
        # imitation click or submit the same card a second time.
        self.mode = InnerRuntimeMode.BASELINE
        self._fallback_issue = issue
        self._baseline_attempted = True
        if self.baseline_runner is None:
            self._fallback_result = None
            return None
        try:
            # The existing Maa method takes only optional keyword arguments;
            # a mock may instead request the triggering issue.  Signature
            # inspection chooses one shape without retrying a potentially
            # side-effecting baseline invocation.
            self._fallback_result = _call_reader(self.baseline_runner, issue)
        except Exception as error:  # baseline failure remains one-way
            self._fallback_result = None
            self._fallback_issue = _issue(
                "baseline-runner-failed",
                f"{issue.code}:{type(error).__name__}:{error}",
            )
        return self._fallback_result

    def _baseline_result(
        self,
        *,
        issue: GenericInnerPolicyIssue,
        plan_type: str | None = None,
        action_order: int | None = None,
        hand_slot: int | None = None,
        gate: LegalCandidateGateResult | None = None,
        ranking: ImitationRanking | None = None,
        card_id: str | None = None,
        guid: str | None = None,
        candidate_key: str | None = None,
        instance_signature: str | None = None,
        occurrence: int | None = None,
        dispatch_result: object | None = None,
    ) -> GenericInnerPolicyStepResult:
        baseline_result = self._run_baseline(issue)
        result = GenericInnerPolicyStepResult(
            mode=InnerRuntimeMode.BASELINE,
            dispatched=False,
            baseline_called=True,
            click_count=0,
            plan_type=plan_type,
            action_order=action_order,
            hand_slot=hand_slot,
            card_id=card_id,
            guid=guid,
            gate=gate,
            ranking=ranking,
            candidate_key=candidate_key,
            instance_signature=instance_signature,
            occurrence=occurrence,
            baseline_result=baseline_result,
            issues=(issue,),
            dispatch_result=dispatch_result,
        )
        self._step_results.append(result)
        return result

    def _already_baseline(
        self,
        *,
        action_order: int | None = None,
        hand_slot: int | None = None,
    ) -> GenericInnerPolicyStepResult:
        issue = _issue(
            "fallback-already-active",
            "baseline already owns the remainder of this exam",
        )
        result = GenericInnerPolicyStepResult(
            mode=InnerRuntimeMode.BASELINE,
            dispatched=False,
            baseline_called=False,
            click_count=0,
            action_order=action_order,
            hand_slot=hand_slot,
            baseline_result=self._fallback_result,
            issues=(issue,),
        )
        self._step_results.append(result)
        return result

    def _resolve_reader_values(
        self,
        evidence: object | None,
        capture: object | None,
        recognition: object | None,
    ) -> tuple[object | None, object | None, object | None]:
        if evidence is None and self.evidence_reader is not None:
            evidence = _call_reader(self.evidence_reader)
        if capture is None and self.capture_reader is not None:
            capture = _call_reader(self.capture_reader, evidence)
        if recognition is None and self.recognition_reader is not None:
            recognition = _call_reader(self.recognition_reader, capture, evidence)
        return evidence, capture, recognition

    def _features(
        self,
        features: Mapping[str, object] | None,
        evidence: object,
        telemetry: object | None,
    ) -> Mapping[str, object] | None:
        if features is not None:
            if not isinstance(features, Mapping):
                raise TypeError("features must be a mapping")
            return features
        if self.feature_provider is not None:
            result = _call_reader(self.feature_provider, evidence, telemetry)
            if not isinstance(result, Mapping):
                raise TypeError("feature_provider must return a mapping")
            return result
        if telemetry is not None:
            raw = _as_mapping(telemetry, "telemetry")
            # The adapter's row already uses feature names accepted by the
            # prior.  Keep a copy so action metadata remains audit-visible but
            # does not get synthesized from a positional list.
            return dict(raw)
        return None

    def _rank(
        self,
        features: Mapping[str, object],
        legal_ids: Sequence[str],
    ) -> ImitationRanking:
        if self.ranker is not None:
            result = _call_reader(self.ranker, features, legal_ids)
        elif self.prior is not None:
            rank_method = _field(self.prior, "rank_with_evidence")
            if not callable(rank_method):
                raise TypeError("prior does not expose rank_with_evidence")
            try:
                signature = inspect.signature(rank_method)
            except (TypeError, ValueError):
                result = rank_method(features, legal_ids, candidate_set_kind="legal")
            else:
                accepts_kind = "candidate_set_kind" in signature.parameters or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
                result = (
                    rank_method(features, legal_ids, candidate_set_kind="legal")
                    if accepts_kind
                    else rank_method(features, legal_ids)
                )
        else:
            raise ValueError("imitation prior is unavailable")
        return _coerce_ranking(result)

    def _invoke_cas(self, request: InnerCardDispatchRequest) -> object | None:
        if self.cas_authorizer is None:
            return True
        return _call_card_dependency(self.cas_authorizer, request)

    def _invoke_dispatcher(self, request: InnerCardDispatchRequest) -> object:
        if self.fixed_slot_dispatcher is None:
            raise RuntimeError("fixed-slot dispatcher is not configured")
        target = self.fixed_slot_dispatcher
        method = _field(target, "dispatch")
        callable_target = target if callable(target) else method
        if not callable(callable_target):
            raise TypeError("fixed-slot dispatcher is not callable")
        return _call_card_dependency(callable_target, request)

    def step(
        self,
        evidence: object | None = None,
        capture: object | None = None,
        all_results: object | None = None,
        features: Mapping[str, object] | None = None,
        *,
        recognition: object | None = None,
        telemetry: Mapping[str, object] | object | None = None,
        action_order: int | None = None,
        hand_slot: int | None = None,
        expected_plan_type: str | None = None,
        allow_dispatch: bool = True,
    ) -> GenericInnerPolicyStepResult:
        """Attempt one logical card action, or switch permanently to baseline.

        ``all_results`` and ``recognition`` are aliases.  Passing both is
        rejected.  If readers were injected, omitted values are obtained from
        those readers before any dispatcher or CAS dependency is called.
        """

        input_issue: GenericInnerPolicyIssue | None = None
        if all_results is not None and recognition is not None:
            input_issue = _issue("recognition-input-conflict", "all_results and recognition are aliases")
        if recognition is not None:
            all_results = recognition
        if type(allow_dispatch) is not bool:
            raise TypeError("allow_dispatch must be bool")
        telemetry_info = InnerTelemetryBinding()
        telemetry_issue: GenericInnerPolicyIssue | None = None
        if telemetry is not None:
            try:
                telemetry_info = telemetry_binding(telemetry)
            except (TypeError, ValueError) as error:
                telemetry_issue = _issue("telemetry-binding-invalid", error)
        if action_order is not None:
            try:
                action_order = _integer(action_order, "action_order")
            except ValueError as error:
                telemetry_issue = _issue("action-order-invalid", error)
        if hand_slot is not None:
            try:
                hand_slot = _integer(hand_slot, "hand_slot")
            except ValueError as error:
                telemetry_issue = _issue("hand-slot-invalid", error)
        if telemetry_info.action_order is not None:
            if action_order is not None and action_order != telemetry_info.action_order:
                telemetry_issue = _issue("action-order-mismatch")
            action_order = telemetry_info.action_order
        if telemetry_info.hand_slot is not None:
            if hand_slot is not None and hand_slot != telemetry_info.hand_slot:
                telemetry_issue = _issue("hand-slot-mismatch")
            hand_slot = telemetry_info.hand_slot
        if self.fallback_active:
            return self._already_baseline(action_order=action_order, hand_slot=hand_slot)
        if input_issue is not None:
            return self._baseline_result(issue=input_issue, action_order=action_order, hand_slot=hand_slot)
        if telemetry_issue is not None:
            return self._baseline_result(issue=telemetry_issue, action_order=action_order, hand_slot=hand_slot)

        # Empty/unknown prior is an explicit baseline configuration.  Do this
        # before reading evidence/capture so the default path is exactly the
        # established Maa baseline and does not perform a speculative read.
        prior_count = None if self.prior is None else _field(self.prior, "observation_count")
        if self.prior is None or prior_count == 0:
            return self._baseline_result(
                issue=_issue("prior-empty", "no verified leaderboard imitation observations"),
                action_order=action_order,
                hand_slot=hand_slot,
            )
        if self.fixed_slot_dispatcher is None:
            return self._baseline_result(
                issue=_issue("fixed-slot-dispatcher-missing"),
                action_order=action_order,
                hand_slot=hand_slot,
            )

        # Resolve the durable state first.  A pending queue must stop the
        # step before a screen/recognition read; this is both cheaper and
        # important for a reader that could otherwise observe a transitional
        # frame as if it were the current Hand.
        try:
            if evidence is None and self.evidence_reader is not None:
                evidence = _call_reader(self.evidence_reader)
        except Exception as error:
            return self._baseline_result(
                issue=_issue("runtime-reader-failed", f"{type(error).__name__}:{error}"),
                action_order=action_order,
                hand_slot=hand_slot,
            )
        if evidence is None:
            return self._baseline_result(issue=_issue("exam-save-missing"), action_order=action_order, hand_slot=hand_slot)
        try:
            settled_issues = _pending_queue_reasons(evidence)
        except Exception as error:
            return self._baseline_result(
                issue=_issue("exam-save-invalid", f"{type(error).__name__}:{error}"),
                action_order=action_order,
                hand_slot=hand_slot,
            )
        if settled_issues:
            return self._baseline_result(
                issue=settled_issues[0],
                action_order=action_order,
                hand_slot=hand_slot,
            )
        try:
            if capture is None and self.capture_reader is not None:
                capture = _call_reader(self.capture_reader, evidence)
        except Exception as error:
            return self._baseline_result(
                issue=_issue("runtime-reader-failed", f"{type(error).__name__}:{error}"),
                action_order=action_order,
                hand_slot=hand_slot,
            )
        if capture is None:
            return self._baseline_result(issue=_issue("capture-missing"), action_order=action_order, hand_slot=hand_slot)
        capture_issues = _capture_issues(
            capture,
            previous_timestamp=self._last_capture_timestamp if self.require_fresh_capture else None,
            bound_window=self._bound_window,
            expected_window=self.expected_window,
        )
        if capture_issues:
            return self._baseline_result(issue=capture_issues[0], action_order=action_order, hand_slot=hand_slot)
        try:
            window = _capture_identity(capture)
            timestamp = _capture_timestamp(capture)
        except (TypeError, ValueError) as error:
            return self._baseline_result(issue=_issue("capture-invalid", error), action_order=action_order, hand_slot=hand_slot)
        if self._bound_window is None:
            self._bound_window = window
        try:
            if all_results is None and self.recognition_reader is not None:
                all_results = _call_reader(self.recognition_reader, capture, evidence)
        except Exception as error:
            return self._baseline_result(
                issue=_issue("runtime-reader-failed", f"{type(error).__name__}:{error}"),
                action_order=action_order,
                hand_slot=hand_slot,
            )
        if all_results is None:
            return self._baseline_result(issue=_issue("recognition-results-missing"), action_order=action_order, hand_slot=hand_slot)
        recognition_hit = _field(all_results, "hit")
        if recognition_hit is False:
            return self._baseline_result(
                issue=_issue("recognition-results-not-hit"),
                action_order=action_order,
                hand_slot=hand_slot,
            )
        all_results_complete = _field(all_results, "all_results_complete")
        if all_results_complete is False:
            return self._baseline_result(
                issue=_issue("recognition-results-incomplete"),
                action_order=action_order,
                hand_slot=hand_slot,
            )
        try:
            detections = normalise_produce_recognition_cards(all_results)
        except (TypeError, ValueError) as error:
            return self._baseline_result(issue=_issue("recognition-results-invalid", error), action_order=action_order, hand_slot=hand_slot)
        try:
            gate = _call_gate(
                self.legal_gate,
                evidence,
                capture,
                detections,
                expected_plan_type=self.expected_plan_type if expected_plan_type is None else expected_plan_type,
                fixed_hand_slot_boxes=self.fixed_hand_slot_boxes,
            )
        except Exception as error:
            return self._baseline_result(issue=_issue("legal-gate-failed", f"{type(error).__name__}:{error}"), action_order=action_order, hand_slot=hand_slot)
        if not isinstance(gate, LegalCandidateGateResult):
            return self._baseline_result(issue=_issue("legal-gate-result-invalid"), gate=None, action_order=action_order, hand_slot=hand_slot)
        if not gate.accepted:
            detail = ",".join(gate.blocker_codes) or "abstain"
            return self._baseline_result(issue=_issue("legal-gate-abstain", detail), gate=gate, action_order=action_order, hand_slot=hand_slot)
        candidates = tuple(gate.candidates)
        if not candidates or len(candidates) != gate.hand_count:
            return self._baseline_result(issue=_issue("legal-candidates-incomplete"), gate=gate, action_order=action_order, hand_slot=hand_slot)
        card_ids = tuple(value.card_id for value in candidates if isinstance(value.card_id, str) and value.card_id)
        guids = tuple(value.guid for value in candidates if isinstance(value.guid, str) and value.guid)
        if len(card_ids) != len(candidates):
            return self._baseline_result(issue=_issue("card-id-incomplete"), gate=gate, action_order=action_order, hand_slot=hand_slot)
        if len(guids) != len(set(guids)):
            return self._baseline_result(issue=_issue("card-guid-duplicate"), gate=gate, action_order=action_order, hand_slot=hand_slot)
        # Card IDs are model/prior keys, not instance keys.  Build the action
        # classes from the accepted ordered hand.  Equal complete semantic
        # signatures collapse to one stable ID; differing/unknown signatures
        # retain a signature/occurrence key so a ranker cannot merge them.
        runtime_records = _runtime_candidate_records(evidence, candidates)
        aligned_keys = candidate_action_keys_aligned(runtime_records)
        legal_pairs = tuple(
            (candidate, aligned_keys[index])
            for index, candidate in enumerate(candidates)
            if candidate.legal and aligned_keys[index] is not None
        )
        legal_ids = tuple(dict.fromkeys(key for _candidate, key in legal_pairs if key is not None))
        legal_card_id_values = tuple(candidate.card_id for candidate, _key in legal_pairs)
        duplicate_card_ids = len(card_ids) != len(set(card_ids))
        duplicate_legal_card_ids = len(legal_card_id_values) != len(set(legal_card_id_values))
        if duplicate_legal_card_ids and _hand_cards(evidence) is None:
            # Gate GUIDs/slots are authoritative only when the same settled
            # hand is available to bind their semantic/occurrence context.
            return self._baseline_result(
                issue=_issue("card-id-instance-unresolved", "instance-evidence-missing"),
                gate=gate,
                action_order=action_order,
                hand_slot=hand_slot,
            )
        if not legal_ids:
            return self._baseline_result(issue=_issue("legal-candidates-incomplete"), gate=gate, action_order=action_order, hand_slot=hand_slot)
        if features is None:
            try:
                features = self._features(features, evidence, telemetry)
            except Exception as error:
                return self._baseline_result(issue=_issue("prior-features-invalid", f"{type(error).__name__}:{error}"), gate=gate, action_order=action_order, hand_slot=hand_slot)
        if features is None:
            return self._baseline_result(issue=_issue("prior-features-missing"), gate=gate, action_order=action_order, hand_slot=hand_slot)
        try:
            ranking = self._rank(features, legal_ids)
        except Exception as error:
            return self._baseline_result(issue=_issue("prior-rank-failed", f"{type(error).__name__}:{error}"), gate=gate, action_order=action_order, hand_slot=hand_slot)
        if ranking.candidate_set_kind != "legal":
            return self._baseline_result(issue=_issue("prior-rank-not-legal"), gate=gate, ranking=ranking, action_order=action_order, hand_slot=hand_slot)
        ranked = tuple(ranking.ranked)
        if ranking.abstained or not ranked:
            return self._baseline_result(issue=_issue("prior-rank-abstain", ranking.reason or "abstain"), gate=gate, ranking=ranking, action_order=action_order, hand_slot=hand_slot)
        if (
            len(set(ranked)) != len(ranked)
            or any(card_id not in legal_ids for card_id in ranked)
        ):
            return self._baseline_result(issue=_issue("prior-rank-incomplete"), gate=gate, ranking=ranking, action_order=action_order, hand_slot=hand_slot)
        # The behavior prior can know only a subset of the current legal Hand.
        # Keep its learned order for known cards, then append still-legal
        # unknown cards in the authoritative Hand order.  Completeness remains
        # owned by the legal gate; one unseen card no longer discards all
        # usable leaderboard evidence for the whole exam.
        ranked = (
            *ranked,
            *(card_id for card_id in legal_ids if card_id not in ranked),
        )
        selected_key = ranked[0]
        matches = tuple(
            candidate for candidate, key in legal_pairs if key == selected_key
        )
        # Compatibility with a custom ranker that returns a stable ID while
        # this hand's key is signature/occurrence-qualified.  Only use this
        # fallback when exactly one legal candidate carries that ID; a
        # duplicate group still has to be ranked by its occurrence key.
        if not matches:
            matches = tuple(
                value
                for value in candidates
                if value.legal and value.card_id == selected_key
            )
            if duplicate_legal_card_ids and len(matches) > 1:
                # A custom ranker returning the stable ID cannot identify one
                # of several non-equivalent copies.  Do not let the quality
                # tie-breaker silently merge different customize/effect
                # identities.
                matches = ()
        selected_id = matches[0].card_id if matches else selected_key
        selected, instance_reason = _resolve_selected_instance(
            evidence,
            matches,
            selector=self.instance_selector,
        )
        if selected is None:
            return self._baseline_result(
                issue=_issue("card-id-instance-unresolved", instance_reason),
                gate=gate,
                ranking=ranking,
                action_order=action_order,
                hand_slot=hand_slot,
                card_id=selected_id,
                candidate_key=selected_key,
            )
        if hand_slot is not None and hand_slot != selected.slot:
            # Telemetry hand slots are historical context, not a substitute
            # for this gate's current ordered Hand.  Retain the context in the
            # result but do not force a potentially stale slot onto input.
            pass
        selected_signature, selected_occurrence = _candidate_instance_metadata(
            evidence,
            selected,
            candidates,
        )
        request = InnerCardDispatchRequest(
            plan_type=gate.plan_type,
            action_order=action_order,
            hand_slot=selected.slot,
            card_id=selected.card_id,
            guid=selected.guid,
            box=selected.box,
            evidence=evidence,
            capture=capture,
            gate=gate,
            ranking=ranking,
            candidate_key=selected_key,
            instance_signature=selected_signature,
            occurrence=selected_occurrence,
        )
        if not allow_dispatch:
            # Shadow-only evaluation deliberately runs every read-only gate
            # and prior rank but stops before CAS/dispatcher.  The caller can
            # then one-way fall back to Maa without presenting a fake click
            # receipt as if a card had been submitted.
            result = GenericInnerPolicyStepResult(
                mode=InnerRuntimeMode.IMITATION,
                dispatched=False,
                baseline_called=False,
                click_count=0,
                plan_type=gate.plan_type,
                action_order=action_order,
                hand_slot=selected.slot,
                card_id=selected.card_id,
                guid=selected.guid,
                gate=gate,
                ranking=ranking,
                candidate_key=selected_key,
                instance_signature=selected_signature,
                occurrence=selected_occurrence,
                issues=(_issue("shadow-only-dispatch-disabled"),),
            )
            self._step_results.append(result)
            return result
        try:
            cas_result = self._invoke_cas(request)
        except Exception as error:
            return self._baseline_result(issue=_issue("examsave-cas-failed", f"{type(error).__name__}:{error}"), gate=gate, ranking=ranking, action_order=action_order, hand_slot=selected.slot, card_id=selected.card_id, guid=selected.guid, candidate_key=selected_key)
        if cas_result is False or (_field(cas_result, "authorized", "accepted") is False):
            return self._baseline_result(issue=_issue("examsave-cas-rejected"), gate=gate, ranking=ranking, action_order=action_order, hand_slot=selected.slot, card_id=selected.card_id, guid=selected.guid, candidate_key=selected_key)
        try:
            dispatch_result = self._invoke_dispatcher(request)
        except Exception as error:
            return self._baseline_result(issue=_issue("fixed-slot-dispatch-failed", f"{type(error).__name__}:{error}"), gate=gate, ranking=ranking, action_order=action_order, hand_slot=selected.slot, card_id=selected.card_id, guid=selected.guid, candidate_key=selected_key)
        if not _dispatch_result_ok(dispatch_result):
            return self._baseline_result(issue=_issue("fixed-slot-dispatch-rejected"), gate=gate, ranking=ranking, action_order=action_order, hand_slot=selected.slot, card_id=selected.card_id, guid=selected.guid, candidate_key=selected_key, dispatch_result=dispatch_result)
        self._last_capture_timestamp = timestamp
        result = GenericInnerPolicyStepResult(
            mode=InnerRuntimeMode.IMITATION,
            dispatched=True,
            baseline_called=False,
            click_count=1,
            plan_type=gate.plan_type,
            action_order=action_order,
            hand_slot=selected.slot,
            card_id=selected.card_id,
            guid=selected.guid,
            gate=gate,
            ranking=ranking,
            candidate_key=selected_key,
            instance_signature=selected_signature,
            occurrence=selected_occurrence,
            dispatch_result=dispatch_result,
        )
        self._step_results.append(result)
        return result

    # Common verb aliases used by adapters.
    dispatch_one = step
    run_step = step
    execute_step = step

    def run(self, steps: Iterable[object]) -> GenericInnerPolicyRunResult:
        """Consume injected step inputs until baseline owns the remainder.

        A step may be an :class:`InnerImitationStepInput`, a mapping, or a
        positional sequence ``(evidence, capture, all_results, features)``.
        This helper is still pure with respect to game/controller state: all
        reads and writes happen inside the caller-provided dependencies.
        """

        if isinstance(steps, (str, bytes, bytearray)):
            raise TypeError("steps must be an iterable of step records")
        for value in steps:
            if self.fallback_active:
                break
            if isinstance(value, InnerImitationStepInput):
                self.step(
                    value.evidence,
                    value.capture,
                    value.all_results,
                    value.features,
                    telemetry=value.telemetry,
                    action_order=value.action_order,
                    hand_slot=value.hand_slot,
                    expected_plan_type=value.expected_plan_type,
                    allow_dispatch=value.allow_dispatch,
                )
                continue
            if isinstance(value, Mapping):
                payload = dict(value)
                self.step(**payload)
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                self.step(*tuple(value))
                continue
            raise TypeError("step records must be mappings, sequences, or InnerImitationStepInput")
        return GenericInnerPolicyRunResult(
            steps=tuple(self._step_results),
            mode=self.mode,
            baseline_result=self._fallback_result,
        )


DEFAULT_NATIVE_VERIFIED_PRIOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_native_verified_v1"
    / "episodes.jsonl"
)
REVIEW_EFFECT_TYPE = "ProduceExamEffectType_ExamReview"


def _native_plan_type(value: object) -> str | None:
    if isinstance(value, str) and value in {
        "ProducePlanType_Plan1",
        "ProducePlanType_Plan2",
        "ProducePlanType_Plan3",
    }:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return {
            2: "ProducePlanType_Plan1",
            3: "ProducePlanType_Plan2",
            4: "ProducePlanType_Plan3",
        }.get(value)
    aliases = {
        "plan1": "ProducePlanType_Plan1",
        "plan2": "ProducePlanType_Plan2",
        "plan3": "ProducePlanType_Plan3",
    }
    return aliases.get(value) if isinstance(value, str) else None


_MAIN_EFFECT_TYPE_NAMES = {
    2: "ProduceExamEffectType_ExamParameterBuff",
    10: "ProduceExamEffectType_ExamLessonBuff",
    31: "ProduceExamEffectType_ExamReview",
    42: "ProduceExamEffectType_ExamCardPlayAggressive",
    45: "ProduceExamEffectType_ExamConcentration",
}
# These are the five main N.I.A. exam archetypes represented by the current
# Master mapping.  The plan (Plan1/2/3) and the exam archetype are separate
# dimensions: an imitation runner must be scoped to the actual archetype in
# ExamSave, rather than treating ExamReview as the only legal flow.
SUPPORTED_NIA_EXAM_EFFECT_TYPES = frozenset(_MAIN_EFFECT_TYPE_NAMES.values())


def nia_exam_effect_type_from_value(value: object) -> str | None:
    """Normalize one native ``mainEffectType`` Master value.

    The native save currently stores the archetype as its numeric Master
    value, while injected/offline callers may already provide the canonical
    enum name.  Unknown values intentionally return ``None`` so the existing
    Maa baseline remains the owner of unsupported flows.
    """

    if isinstance(value, int) and not isinstance(value, bool):
        return _MAIN_EFFECT_TYPE_NAMES.get(value)
    if isinstance(value, str) and value in SUPPORTED_NIA_EXAM_EFFECT_TYPES:
        return value
    return None
_AUDITION_STAGES = {
    16: "ProduceStepType_AuditionMid1",
    17: "ProduceStepType_AuditionMid2",
    18: "ProduceStepType_AuditionFinal",
}


class ProductionInnerImitationExamRunner:
    """Injected production bridge for one complete Maa-backed exam.

    The runner owns no controller by default and imports the controller
    sender/fixed-slot dispatcher lazily.  A host may inject every dependency
    for tests.  It loads one settled ExamSave, asks the controller's pure
    ``recognize_audition_cards_once`` command for each fresh frame, ranks
    stable card IDs, dispatches one fixed slot through the existing
    ExamSave/CAS dispatcher, then polls the same ExamSave until a changed
    settled/terminal boundary is visible.  Any pre-dispatch abstention calls
    the supplied baseline callback once and never returns to imitation.
    """

    def __init__(
        self,
        *,
        evidence_loader: Callable[[Path], object] | None = None,
        prior_path: str | Path = DEFAULT_NATIVE_VERIFIED_PRIOR_PATH,
        prior_loader: Callable[..., LeaderboardCardImitationPrior] | None = None,
        recognition_command: Callable[..., Mapping[str, object]] | None = None,
        command_sender: Callable[..., Mapping[str, object]] | None = None,
        fixed_slot_dispatcher: Callable[..., object] | None = None,
        legal_gate: GateCallable = gate_legal_candidates,
        ranker: RankCallable | None = None,
        produce_id: str | None = None,
        exam_effect_type: str | None = None,
        card_ready_after_noncard_actions: Callable[..., object] | None = None,
        allowed_flows: Sequence[Sequence[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = 0.25,
        max_settlement_polls: int = 120,
        max_actions: int = 100,
    ) -> None:
        if evidence_loader is None:
            from .initial_regular_autopilot import load_initial_regular_plan2_exam_evidence

            evidence_loader = load_initial_regular_plan2_exam_evidence
        if not callable(evidence_loader):
            raise TypeError("evidence_loader must be callable")
        if prior_loader is None:
            from .leaderboard_card_imitation_prior import load_leaderboard_card_imitation_prior

            prior_loader = load_leaderboard_card_imitation_prior
        if not callable(prior_loader):
            raise TypeError("prior_loader must be callable")
        if recognition_command is None:
            from .controller_client import send_command

            recognition_command = send_command
        if command_sender is None:
            command_sender = recognition_command
        for name, value in (
            ("recognition_command", recognition_command),
            ("command_sender", command_sender),
        ):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        if fixed_slot_dispatcher is not None and not callable(fixed_slot_dispatcher):
            raise TypeError("fixed_slot_dispatcher must be callable or None")
        if not callable(legal_gate):
            raise TypeError("legal_gate must be callable")
        if ranker is not None and not callable(ranker):
            raise TypeError("ranker must be callable or None")
        if produce_id is not None:
            _text(produce_id, "produce_id")
        if exam_effect_type is not None:
            _text(exam_effect_type, "exam_effect_type")
        if card_ready_after_noncard_actions is not None and not callable(
            card_ready_after_noncard_actions
        ):
            raise TypeError(
                "card_ready_after_noncard_actions must be callable or None"
            )
        if allowed_flows is None:
            normalized_flows: tuple[tuple[str, str, str], ...] = ()
        else:
            normalized: list[tuple[str, str, str]] = []
            for index, flow in enumerate(allowed_flows):
                if (
                    isinstance(flow, (str, bytes, bytearray))
                    or not isinstance(flow, Sequence)
                    or len(flow) != 3
                ):
                    raise ValueError(
                        f"allowed_flows[{index}] must contain produce, plan and effect"
                    )
                values = tuple(_text(value, f"allowed_flows[{index}]") for value in flow)
                normalized.append(values)  # type: ignore[arg-type]
            if len(normalized) != len(set(normalized)):
                raise ValueError("allowed_flows must be unique")
            normalized_flows = tuple(normalized)
        if not callable(sleep) or not callable(monotonic):
            raise TypeError("sleep and monotonic must be callable")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        _integer(max_settlement_polls, "max_settlement_polls", minimum=1)
        _integer(max_actions, "max_actions", minimum=1)
        self.evidence_loader = evidence_loader
        self.prior_path = Path(prior_path).resolve()
        self.prior_loader = prior_loader
        self.recognition_command = recognition_command
        self.command_sender = command_sender
        self.custom_fixed_slot_dispatcher = fixed_slot_dispatcher
        self.legal_gate = legal_gate
        self.ranker = ranker
        self.produce_id = produce_id
        self.exam_effect_type = exam_effect_type
        self.card_ready_after_noncard_actions = card_ready_after_noncard_actions
        self.allowed_flows = normalized_flows
        self.sleep = sleep
        self.monotonic = monotonic
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_settlement_polls = max_settlement_polls
        self.max_actions = max_actions

    def _load_prior(self) -> LeaderboardCardImitationPrior | None:
        try:
            prior = self.prior_loader(self.prior_path)
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return prior if getattr(prior, "observation_count", 0) > 0 else None

    @staticmethod
    def _terminal(evidence: object) -> bool:
        state = _extract_evidence_state(evidence)
        runtime = _field(state, "root_runtime")
        return bool(
            _field(runtime, "is_exam_end_complete") is True
            or _field(state, "is_exam_end_complete") is True
        )

    def _explicit_card_ready_marker(self, evidence: object) -> bool:
        """Return only caller/ExamSave-owned ready evidence (no inference)."""

        marker = _field(evidence, "card_ready_after_noncard_actions")
        state = _extract_evidence_state(evidence)
        if marker is not True:
            marker = _field(state, "card_ready_after_noncard_actions")
        if marker is True:
            return True
        owner = _field(evidence, "drink_owner_evidence", "drink_owner")
        if owner is None:
            owner = _field(state, "drink_owner_evidence", "drink_owner")
        if owner is not None and _field(
            owner, "card_ready_after_noncard_actions"
        ) is True:
            return True
        provider = self.card_ready_after_noncard_actions
        if provider is None:
            return False
        try:
            decision = _call_reader(provider, evidence)
        except Exception:
            return False
        if decision is True:
            return True
        if isinstance(decision, Mapping):
            return decision.get("card_ready_after_noncard_actions") is True
        return _field(decision, "card_ready_after_noncard_actions") is True

    def _card_ready_after_noncard_actions(self, evidence: object) -> bool:
        """Accept an explicit marker or a settled native Hand boundary."""

        if self._explicit_card_ready_marker(evidence):
            return True
        state = _extract_evidence_state(evidence)
        runtime = _field(state, "root_runtime")
        opaque = _field(runtime, "opaque_fields")
        if callable(_field(opaque, "to_value")):
            opaque = opaque.to_value()  # type: ignore[union-attr]
        if isinstance(opaque, Mapping):
            drinks = opaque.get("drinkList", opaque.get("drink_list"))
            if (
                isinstance(drinks, Sequence)
                and not isinstance(drinks, (str, bytes, bytearray))
                and len(drinks) > 0
            ):
                # This card-only bridge does not own the still-available
                # drink action.  Let Maa consume that prelude rather than
                # silently discarding a legal non-card candidate.
                return False
        zones = _field(state, "zones")
        hand = _field(zones, "hand") if zones is not None else None
        # The later Maa recognition + legal gate still has to publish every
        # visible slot and at least one legal card.  A settled ExamSave with a
        # non-empty Hand therefore proves this is a card decision boundary;
        # an extra owner marker is useful but not required.
        return bool(
            self._settled(evidence)
            and isinstance(hand, Sequence)
            and not isinstance(hand, (str, bytes, bytearray))
            and len(hand) > 0
        )

    def _unowned_drink_prelude(self, evidence: object) -> bool:
        """Whether a non-empty ExamSave drink owner lacks a ready marker."""

        state = _extract_evidence_state(evidence)
        runtime = _field(state, "root_runtime")
        opaque_value = _field(runtime, "opaque_fields")
        if callable(_field(opaque_value, "to_value")):
            opaque_value = opaque_value.to_value()  # type: ignore[union-attr]
        if not isinstance(opaque_value, Mapping):
            return False
        drink_list = opaque_value.get("drinkList")
        if not isinstance(drink_list, Sequence) or isinstance(
            drink_list, (str, bytes, bytearray)
        ) or not drink_list:
            return False
        marker = _field(evidence, "card_ready_after_noncard_actions")
        if marker is not True:
            marker = _field(state, "card_ready_after_noncard_actions")
        owner = _field(evidence, "drink_owner_evidence", "drink_owner")
        if owner is None:
            owner = _field(state, "drink_owner_evidence", "drink_owner")
        if marker is True or _field(owner, "card_ready_after_noncard_actions") is True:
            return False
        provider = self.card_ready_after_noncard_actions
        if provider is not None:
            try:
                decision = _call_reader(provider, evidence)
            except Exception:
                decision = False
            if decision is True or (
                isinstance(decision, Mapping)
                and decision.get("card_ready_after_noncard_actions") is True
            ):
                return False
        return True

    @staticmethod
    def _settled(evidence: object) -> bool:
        state = _extract_evidence_state(evidence)
        if ProductionInnerImitationExamRunner._terminal(evidence):
            return True
        value = _field(state, "is_native_actionable_settled")
        return value is True or _field(evidence, "settled") is True

    def _features(
        self,
        evidence: object,
        plan_type: str,
        history: Sequence[str],
    ) -> Mapping[str, object] | None:
        state = _extract_evidence_state(evidence)
        runtime = _field(state, "root_runtime")
        opaque_value = _field(runtime, "opaque_fields")
        if callable(_field(opaque_value, "to_value")):
            opaque_value = opaque_value.to_value()  # type: ignore[union-attr]
        opaque = opaque_value if isinstance(opaque_value, Mapping) else {}
        produce_id = self.produce_id or opaque.get("produceId")
        if not isinstance(produce_id, str) or not produce_id:
            return None
        raw_plan = opaque.get("planType", opaque.get("plan_type"))
        # The runner is invoked for one typed ExamSave stage.  Do not let a
        # stale/replaced save with a different plan reach recognition or the
        # legal gate merely because the caller supplied an expected plan.
        if _native_plan_type(raw_plan) != plan_type:
            return None
        effect = self.exam_effect_type
        if effect is None:
            raw_effect = opaque.get("mainEffectType")
            effect = nia_exam_effect_type_from_value(raw_effect)
        stage_value = _field(state, "step_type_value")
        stage = _AUDITION_STAGES.get(stage_value) if isinstance(stage_value, int) else None
        turn = _field(state, "current_turn")
        if (
            not isinstance(effect, str)
            or not effect
            or stage is None
            or isinstance(turn, bool)
            or not isinstance(turn, int)
            or turn < 1
        ):
            return None
        return {
            "flow": (produce_id, plan_type, effect),
            "produce_id": produce_id,
            "plan_type": plan_type,
            "exam_effect_type": effect,
            "stage": stage,
            "turn": turn,
            "action_history": list(history[-2:]),
        }

    def _flow_allowed(self, features: Mapping[str, object]) -> bool:
        if not self.allowed_flows:
            return False
        raw_flow = features.get("flow")
        if isinstance(raw_flow, str):
            values = tuple(raw_flow.split("|"))
        elif isinstance(raw_flow, Sequence) and not isinstance(
            raw_flow, (str, bytes, bytearray)
        ):
            values = tuple(raw_flow)
        else:
            return False
        if len(values) != 3 or any(not isinstance(value, str) for value in values):
            return False
        # ``allowed_flows`` scopes the run to the exact produce/plan/archetype
        # tuple.  Do not additionally hard-code ExamReview here: that made the
        # otherwise plan-neutral inner bridge silently abstain for the other
        # four Master-backed N.I.A. archetypes (including Anomaly's native
        # plan/effect combinations).  The effect-name set is derived from the
        # same Master numeric mapping used by ``_features``; unknown effects
        # remain fail-closed.
        return (
            values in self.allowed_flows
            and values[2] in SUPPORTED_NIA_EXAM_EFFECT_TYPES
        )

    def _unsupported_prelude_reason(self, evidence: object) -> str | None:
        """Reject card imitation while an unowned drink prelude is present.

        The production loop deliberately owns cards only.  ``drinkList`` is a
        complete native inventory of one-shot instances; without a drink
        dispatcher and status-aware prior, treating the first card as if no
        drink action preceded it would be a different decision boundary.  A
        typed ExamSave always carries this field, while an absent/malformed
        field is also unsafe to interpret as an empty inventory.
        """

        # Explicit owner evidence is the only override for a non-empty drink
        # inventory or a retained drink history.  No UI absence is inferred.
        if self._explicit_card_ready_marker(evidence):
            return None
        state = _extract_evidence_state(evidence)
        runtime = _field(state, "root_runtime")
        opaque_value = _field(runtime, "opaque_fields")
        if callable(_field(opaque_value, "to_value")):
            opaque_value = opaque_value.to_value()  # type: ignore[union-attr]
        if not isinstance(opaque_value, Mapping):
            return "inner-imitation-drink-state-unavailable"
        if "drinkList" not in opaque_value and "drink_list" not in opaque_value:
            return "inner-imitation-drink-state-unavailable"
        drink_list = opaque_value.get("drinkList", opaque_value.get("drink_list"))
        if isinstance(drink_list, (str, bytes, bytearray)) or not isinstance(
            drink_list, Sequence
        ):
            return "inner-imitation-drink-state-invalid"
        if len(drink_list):
            # Let the explicit card-ready gate below run the read-only prior
            # in shadow mode while keeping CAS/dispatch disabled.
            return None
        # A prelude can consume the last inventory instance before the first
        # card, so an empty current ``drinkList`` alone is not sufficient.  The
        # native user log retains completed drink cells; keep the card-only
        # runner out of that post-drink state as well.
        user_log = opaque_value.get("userPlayLogList", opaque_value.get("user_play_log_list"))
        if isinstance(user_log, (str, bytes, bytearray)) or not isinstance(
            user_log, Sequence
        ):
            return "inner-imitation-drink-state-invalid"
        for row in user_log:
            if not isinstance(row, Mapping):
                continue
            if row.get("_cellType") == 4 and isinstance(
                row.get("_triggerId"), str
            ) and str(row["_triggerId"]).startswith("pdrink_"):
                # The card-ready gate will keep this post-drink state in
                # shadow mode unless a separate owner proves the handoff.
                return None
        return None

    def _capture_metadata(self, evidence: object) -> dict[str, object]:
        return {
            "evidence_digest": evidence.digest(),  # type: ignore[union-attr]
            "exam_save_sha256": _field(evidence, "source_sha256"),
            "session_transition_id": _field(evidence, "session_transition_id"),
            "run_id": _field(evidence, "run_id"),
            "step_context_id": _field(evidence, "step_context_id"),
        }

    def _recognize(self, evidence: object) -> Mapping[str, object]:
        return self.recognition_command(
            "recognize_audition_cards_once",
            timeout=45.0,
            capture_metadata=self._capture_metadata(evidence),
        )

    @staticmethod
    def _step_audit(
        step: GenericInnerPolicyStepResult,
        recognition: object,
        *,
        shadow_only: bool,
        card_ready: bool,
        flow_allowed: bool,
    ) -> dict[str, object]:
        """Keep fallback diagnostics without embedding capture/image payloads."""

        payload = step.to_dict()
        # A baseline receipt or dispatcher audit may contain large traces and
        # capture paths.  The fallback audit needs gate/rank evidence, not an
        # image or the opaque baseline response.
        payload.pop("baseline_result", None)
        payload.pop("dispatch_result", None)
        count: int | None = None
        hit: bool | None = None
        if isinstance(recognition, Mapping):
            raw = recognition.get("all_results")
            if isinstance(raw, Sequence) and not isinstance(
                raw, (str, bytes, bytearray)
            ):
                count = len(raw)
            raw_hit = recognition.get("hit")
            if isinstance(raw_hit, bool):
                hit = raw_hit
        payload.update(
            {
                "recognition_result_count": count,
                "recognition_hit": hit,
                "shadow_only": shadow_only,
                "card_ready_after_noncard_actions": card_ready,
                "flow_allowed": flow_allowed,
                "click_count": step.click_count,
                "gate_blocker_codes": list(
                    step.gate.blocker_codes if step.gate is not None else ()
                ),
                "prior_abstained": bool(
                    step.ranking.abstained if step.ranking is not None else False
                ),
                "prior_reason": (
                    None
                    if step.ranking is None
                    else step.ranking.reason
                ),
            }
        )
        return payload

    @staticmethod
    def _same_exam_identity(before: object, current: object) -> bool:
        """Require a changed poll to remain inside the same exam boundary."""

        for name in (
            "run_id",
            "session_transition_id",
            "step_context_id",
            "step_context_digest",
        ):
            left = _field(before, name)
            right = _field(current, name)
            # A missing identity field is not evidence that the identity was
            # preserved.  Typed production evidence always supplies these
            # fields; injected/malformed records must fail closed instead of
            # being accepted when only one side exposes the field.
            if left != right:
                return False
        before_state = _extract_evidence_state(before)
        current_state = _extract_evidence_state(current)
        for name in (
            "character_id",
            "setting_id",
            "exam_type",
            "step_type_value",
            "limit_turn",
            "turn_parameter_types",
        ):
            left = _field(before_state, name)
            right = _field(current_state, name)
            if left != right:
                return False
        # The native opaque root carries the durable plan/effect identity.  A
        # replacement save can otherwise reuse character/setting/stage while
        # changing the archetype effect, which must never be treated as the
        # post-click settlement of the old exam.
        def opaque(state: object) -> Mapping[str, object]:
            runtime = _field(state, "root_runtime")
            value = _field(runtime, "opaque_fields")
            if callable(_field(value, "to_value")):
                value = value.to_value()  # type: ignore[union-attr]
            return value if isinstance(value, Mapping) else {}

        left_opaque = opaque(before_state)
        right_opaque = opaque(current_state)
        for names in (
            ("produceId", "produce_id"),
            ("planType", "plan_type"),
            ("mainEffectType", "main_effect_type"),
        ):
            left = next((left_opaque[name] for name in names if name in left_opaque), None)
            right = next((right_opaque[name] for name in names if name in right_opaque), None)
            if left != right:
                return False
        return True

    def _wait_next(self, path: Path, before: object) -> object | None:
        before_digest = before.digest()  # type: ignore[union-attr]
        for poll in range(self.max_settlement_polls):
            try:
                current = self.evidence_loader(path)
            except (FileNotFoundError, OSError, TypeError, ValueError, StopIteration):
                current = None
            if current is not None:
                try:
                    changed = (
                        current.digest() != before_digest  # type: ignore[union-attr]
                        and self._same_exam_identity(before, current)
                    )
                except Exception:
                    changed = False
                if changed and self._settled(current):
                    return current
            if poll + 1 < self.max_settlement_polls:
                self.sleep(self.poll_interval_seconds)
        return None

    @staticmethod
    def _fallback(
        baseline_runner: Callable[[], Mapping[str, object]],
        *,
        reason: str,
        prior_path: Path,
        trace: Sequence[Mapping[str, object]] = (),
        shadow_only: bool = False,
    ) -> dict[str, object]:
        result = dict(baseline_runner())
        return {
            "mode": "maa-completion-baseline",
            "baseline_called": True,
            "baseline_result": result,
            "inner_imitation": {
                "status": "baseline",
                "reason": reason,
                "prior_path": str(prior_path),
                "steps": [dict(value) for value in trace],
                "shadow_only": shadow_only,
            },
        }

    def _default_fixed_dispatcher(self, request: InnerCardDispatchRequest) -> object:
        from .live_actions import SuggestedClick
        from .plan2_native_maa_action_executor import (
            MaaExamSaveCardPlayDispatcher,
            Plan2InputStateAdvanced,
        )

        capture = _as_mapping(request.capture, "request.capture")
        path = capture.get("png_path")
        if not isinstance(path, str) or not path:
            raise ValueError("recognition capture has no png_path")
        left, top, width, height = request.box
        right, bottom = left + width, top + height
        action = SuggestedClick(
            label=f"inner-imitation:{request.card_id}",
            canonical_x=(left + right) // 2,
            canonical_y=(top + bottom) // 2,
            source_png_path=path,
            source_timestamp=float(capture["timestamp"]),
            source_hwnd=int(capture["hwnd"]),
            source_pid=int(capture["pid"]),
            verification_box=(left, top, right, bottom),
            click_count=1,
        )
        before_digest = request.evidence.digest()  # type: ignore[union-attr]

        def authorize(bound_capture: Mapping[str, object]) -> None:
            if (
                int(bound_capture.get("hwnd", 0)) != action.source_hwnd
                or int(bound_capture.get("pid", 0)) != action.source_pid
            ):
                raise Plan2InputStateAdvanced(
                    "inner imitation capture window changed",
                    input_submitted=False,
                )
            current = self.evidence_loader(Path(request.evidence.source_path))  # type: ignore[union-attr]
            if current.digest() != before_digest:  # type: ignore[union-attr]
                raise Plan2InputStateAdvanced(
                    "inner imitation ExamSave CAS advanced",
                    input_submitted=False,
                )
            state = _extract_evidence_state(current)
            zones = _field(state, "zones")
            hand = _field(zones, "hand") if zones is not None else None
            values = tuple(hand) if hand is not None else ()
            if request.hand_slot >= len(values):
                raise Plan2InputStateAdvanced(
                    "inner imitation Hand slot disappeared",
                    input_submitted=False,
                )
            card = values[request.hand_slot]
            if (
                _field(card, "guid") != request.guid
                or _field(card, "card_id", "id") != request.card_id
            ):
                raise Plan2InputStateAdvanced(
                    "inner imitation selected GUID changed",
                    input_submitted=False,
                )

        dispatcher = MaaExamSaveCardPlayDispatcher(
            command_sender=self.command_sender,
            sleep=self.sleep,
        )
        try:
            return dispatcher(action, None, pre_input_authorizer=authorize)
        except Plan2InputStateAdvanced as error:
            if getattr(error, "input_submitted", False):
                return {
                    "submitted": True,
                    "input_submitted": True,
                    "detail": str(error),
                }
            raise

    def __call__(
        self,
        plan_type: str,
        exam_save_path: Path,
        baseline_runner: Callable[[], Mapping[str, object]],
    ) -> Mapping[str, object]:
        plan = _native_plan_type(plan_type)
        if plan is None:
            return self._fallback(
                baseline_runner,
                reason="inner-imitation-plan-type-invalid",
                prior_path=self.prior_path,
            )
        prior = self._load_prior()
        if prior is None:
            return self._fallback(
                baseline_runner,
                reason="inner-imitation-prior-missing-or-empty",
                prior_path=self.prior_path,
            )
        path = Path(exam_save_path).resolve()
        try:
            current = self.evidence_loader(path)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            return self._fallback(
                baseline_runner,
                reason=f"inner-imitation-evidence-load-failed:{type(error).__name__}",
                prior_path=self.prior_path,
            )
        history: list[str] = []
        trace: list[Mapping[str, object]] = []
        action_order = 0
        dispatcher = self.custom_fixed_slot_dispatcher or self._default_fixed_dispatcher
        runtime = GenericInnerPolicyRuntimeV2(
            prior=prior,
            baseline_runner=baseline_runner,
            fixed_slot_dispatcher=dispatcher,
            legal_gate=self.legal_gate,
            ranker=self.ranker,
            expected_plan_type=plan,
        )
        for _ in range(self.max_actions):
            if self._terminal(current):
                return {
                    "accepted": True,
                    "terminal": True,
                    "reason": "inner-imitation-terminal",
                    "simulator_required": False,
                    "input_submitted": bool(trace),
                    "actions_executed": len(trace),
                    "steps": [dict(value) for value in trace],
                    "orchestration": {"source": "generic-inner-policy-runtime-v2"},
                    "inner_imitation": {
                        "status": "imitation",
                        "prior_path": str(self.prior_path),
                        "allowed_flows": [list(flow) for flow in self.allowed_flows],
                        "steps": [dict(value) for value in trace],
                    },
                }
            prelude_reason = self._unsupported_prelude_reason(current)
            if prelude_reason is not None:
                return self._fallback(
                    baseline_runner,
                    reason=prelude_reason,
                    prior_path=self.prior_path,
                    trace=trace,
                )
            features = self._features(current, plan, history)
            if features is None:
                return self._fallback(
                    baseline_runner,
                    reason="inner-imitation-features-unavailable",
                    prior_path=self.prior_path,
                    trace=trace,
                )
            try:
                recognition = self._recognize(current)
            except Exception as error:
                return self._fallback(
                    baseline_runner,
                    reason=f"inner-imitation-recognition-failed:{type(error).__name__}",
                    prior_path=self.prior_path,
                    trace=trace,
                )
            capture = recognition.get("capture") if isinstance(recognition, Mapping) else None
            flow_allowed = self._flow_allowed(features)
            card_ready = self._card_ready_after_noncard_actions(current)
            step = runtime.step(
                current,
                capture,
                recognition,
                features,
                action_order=action_order,
                allow_dispatch=flow_allowed and card_ready,
            )
            step_audit = self._step_audit(
                step,
                recognition,
                shadow_only=not (flow_allowed and card_ready),
                card_ready=card_ready,
                flow_allowed=flow_allowed,
            )
            if step.fallback:
                trace_with_step = (*trace, step_audit)
                nested = step.baseline_result
                if isinstance(nested, Mapping):
                    return {
                        "mode": "maa-completion-baseline",
                        "baseline_called": True,
                        "baseline_result": dict(nested),
                        "inner_imitation": {
                            "status": "baseline",
                            "reason": step.reason_codes[0] if step.reason_codes else "abstain",
                            "prior_path": str(self.prior_path),
                            "steps": [dict(value) for value in trace_with_step],
                        },
                    }
                if runtime.baseline_attempted:
                    # The baseline boundary was entered but did not produce a
                    # receipt (normally because it raised).  Do not invoke it
                    # a second time merely to manufacture a fallback result.
                    return {
                        "mode": "maa-completion-baseline",
                        "baseline_called": True,
                        "baseline_result": nested,
                        "inner_imitation": {
                            "status": "baseline",
                            "reason": step.reason_codes[0] if step.reason_codes else "abstain",
                            "prior_path": str(self.prior_path),
                            "steps": [dict(value) for value in trace_with_step],
                        },
                    }
                return self._fallback(
                    baseline_runner,
                    reason="inner-imitation-abstain-without-baseline-result",
                    prior_path=self.prior_path,
                    trace=trace_with_step,
                )
            if not step.dispatched:
                shadow_only = any(
                    issue.code == "shadow-only-dispatch-disabled"
                    for issue in step.issues
                )
                trace.append(step_audit)
                return self._fallback(
                    baseline_runner,
                    reason=(
                        "inner-imitation-card-ready-boundary-unproven"
                        if shadow_only and not card_ready
                        else "inner-imitation-flow-not-allowed"
                        if shadow_only
                        else "inner-imitation-step-not-dispatched"
                    ),
                    prior_path=self.prior_path,
                    trace=trace,
                    shadow_only=shadow_only,
                )
            trace.append(step.to_dict())
            history.append(step.card_id or "")
            action_order += 1
            next_evidence = self._wait_next(path, current)
            if next_evidence is None:
                return self._fallback(
                    baseline_runner,
                    reason="inner-imitation-next-settled-timeout",
                    prior_path=self.prior_path,
                    trace=trace,
                )
            current = next_evidence
        # The final permitted dispatch may itself publish the terminal save.
        # Inspect that post-action evidence before treating the action budget
        # as a fallback condition; otherwise a completed exam can receive a
        # second Maa baseline invocation.
        if self._terminal(current):
            return {
                "accepted": True,
                "terminal": True,
                "reason": "inner-imitation-terminal",
                "simulator_required": False,
                "input_submitted": bool(trace),
                "actions_executed": len(trace),
                "steps": [dict(value) for value in trace],
                "orchestration": {"source": "generic-inner-policy-runtime-v2"},
                "inner_imitation": {
                    "status": "imitation",
                    "prior_path": str(self.prior_path),
                    "allowed_flows": [list(flow) for flow in self.allowed_flows],
                    "steps": [dict(value) for value in trace],
                },
            }
        return self._fallback(
            baseline_runner,
            reason="inner-imitation-max-actions",
            prior_path=self.prior_path,
            trace=trace,
        )


def build_production_inner_imitation_exam_runner(
    **kwargs: object,
) -> ProductionInnerImitationExamRunner:
    """Build the opt-in production runner with all reads/input injected."""

    return ProductionInnerImitationExamRunner(**kwargs)  # type: ignore[arg-type]


# Discovery-friendly aliases; all names point at the same implementation.
build_inner_imitation_exam_runner = build_production_inner_imitation_exam_runner
InnerImitationExamRunner = ProductionInnerImitationExamRunner


@dataclass(frozen=True, slots=True)
class InnerImitationStepInput:
    """Convenient dependency-injected input for :meth:`run`."""

    evidence: object | None = None
    capture: object | None = None
    all_results: object | None = None
    features: Mapping[str, object] | None = None
    telemetry: Mapping[str, object] | object | None = None
    action_order: int | None = None
    hand_slot: int | None = None
    expected_plan_type: str | None = None
    allow_dispatch: bool = True


# Short aliases make the wrapper discoverable without creating parallel code.
GenericInnerPolicyRuntime = GenericInnerPolicyRuntimeV2
InnerImitationRuntime = GenericInnerPolicyRuntimeV2
GenericInnerImitationRuntime = GenericInnerPolicyRuntimeV2
InnerPolicyRuntimeV2 = GenericInnerPolicyRuntimeV2
InnerPolicyStepResult = GenericInnerPolicyStepResult


__all__ = [
    "BaselineRunner",
    "CasAuthorizer",
    "DEFAULT_NATIVE_VERIFIED_PRIOR_PATH",
    "InstanceSelector",
    "GenericInnerImitationRuntime",
    "GenericInnerPolicyIssue",
    "GenericInnerPolicyRuntime",
    "GenericInnerPolicyRuntimeV2",
    "GenericInnerPolicyRuntimeV2Dependencies",
    "GenericInnerPolicyRunResult",
    "GenericInnerPolicyStepResult",
    "InnerCardDispatchRequest",
    "InnerCardDispatcher",
    "InnerImitationExamRunner",
    "InnerImitationRuntime",
    "InnerImitationStepInput",
    "InnerPolicyRuntimeV2",
    "InnerPolicyStepResult",
    "InnerRuntimeMode",
    "InnerTelemetryBinding",
    "ProductionInnerImitationExamRunner",
    "SUPPORTED_NIA_EXAM_EFFECT_TYPES",
    "SCHEMA",
    "SCHEMA_VERSION",
    "nia_exam_effect_type_from_value",
    "extract_telemetry_binding",
    "normalise_produce_recognition_cards",
    "normalize_produce_recognition_cards",
    "parse_telemetry_binding",
    "build_production_inner_imitation_exam_runner",
    "build_inner_imitation_exam_runner",
    "telemetry_binding",
]
