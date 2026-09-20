"""Verified, single-step execution for screenshot-derived suggestions.

The public entry point in this module performs one declared UI action only.  An
action may be a single click or an explicitly recognized two-click game choice.
It binds the suggestion to the observed game window and a stable screen region,
captures immediately before input, and captures again afterwards.  A missing
visual change is reported to the caller; it is never used as a reason to retry
beyond the declared click count.

This module does not read game memory, inject code, or accept arbitrary shell
commands.  Coordinates are always expressed in Maa's 720 x 1280 client space
until they are converted with geometry returned by the elevated controller.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import numpy as np
from PIL import Image

from .live_capture import CANONICAL_HEIGHT, CANONICAL_WIDTH


CommandSender = Callable[..., Mapping[str, Any]]
SemanticStaleVerifier = Callable[[Path, Path, "SuggestedClick"], bool]
CanonicalBox = tuple[int, int, int, int]
DEFAULT_VERIFIED_CARD_MAX_AGE_SECONDS = 15.0
CardPlayPhase = Literal[
    "selected_preview",
    "resolving",
    "settled",
    "result",
    "failed",
]


@dataclass(frozen=True, slots=True)
class ChoicePreviewAnalysis:
    """Minimal semantic proof required before a choice confirmation click."""

    page: str
    target_key: str
    highlighted: bool
    semantic_key: tuple[Any, ...]
    confidence: float = 1.0
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.page or not self.target_key:
            raise ValueError("choice preview page and target must be non-empty")
        if type(self.highlighted) is not bool:
            raise TypeError("choice preview highlighted must be boolean")
        if type(self.semantic_key) is not tuple or not self.semantic_key:
            raise ValueError("choice preview semantic_key must be a non-empty tuple")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("choice preview confidence must be between zero and one")
        if type(self.issues) is not tuple or any(
            type(value) is not str for value in self.issues
        ):
            raise TypeError("choice preview issues must be an exact tuple of text")


class StaleSuggestionError(RuntimeError):
    """Raised when the current screen no longer matches the analyzed screen."""


@dataclass(frozen=True, slots=True)
class SuggestedClick:
    label: str
    canonical_x: int
    canonical_y: int
    source_png_path: str
    source_timestamp: float
    source_hwnd: int
    source_pid: int
    verification_box: CanonicalBox
    click_count: int = 1

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("suggested click must have a label")
        if not 0 <= self.canonical_x < CANONICAL_WIDTH:
            raise ValueError("canonical_x is outside the 720-wide client")
        if not 0 <= self.canonical_y < CANONICAL_HEIGHT:
            raise ValueError("canonical_y is outside the 1280-high client")
        left, top, right, bottom = self.verification_box
        if not (0 <= left < right <= CANONICAL_WIDTH):
            raise ValueError("verification_box has an invalid horizontal range")
        if not (0 <= top < bottom <= CANONICAL_HEIGHT):
            raise ValueError("verification_box has an invalid vertical range")
        if not (left <= self.canonical_x < right and top <= self.canonical_y < bottom):
            raise ValueError("suggested point must be inside verification_box")
        if self.source_hwnd <= 0 or self.source_pid <= 0:
            raise ValueError("suggested click must be bound to a window and process")
        if self.click_count not in {1, 2}:
            raise ValueError("click_count must be one or two")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SuggestedClick":
        raw_box = payload["verification_box"]
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            raise ValueError("verification_box must contain four integers")
        return cls(
            label=str(payload["label"]),
            canonical_x=int(payload["canonical_x"]),
            canonical_y=int(payload["canonical_y"]),
            source_png_path=str(payload["source_png_path"]),
            source_timestamp=float(payload["source_timestamp"]),
            source_hwnd=int(payload["source_hwnd"]),
            source_pid=int(payload["source_pid"]),
            verification_box=tuple(int(value) for value in raw_box),  # type: ignore[arg-type]
            click_count=int(payload.get("click_count", 1)),
        )


@dataclass(frozen=True, slots=True)
class SuggestedSwipe:
    """One screenshot-bound, canonical-coordinate swipe suggestion.

    Swipes are deliberately a separate action type from clicks.  This keeps a
    scrolling recovery visible in the surface payload and lets its executor
    enforce the exact gesture instead of allowing a reader to send input as a
    side effect of parsing a frame.
    """

    label: str
    canonical_x1: int
    canonical_y1: int
    canonical_x2: int
    canonical_y2: int
    duration_ms: int
    source_png_path: str
    source_timestamp: float
    source_hwnd: int
    source_pid: int
    verification_box: CanonicalBox
    action_type: Literal["swipe"] = "swipe"

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("suggested swipe must have a label")
        for name, value, limit in (
            ("canonical_x1", self.canonical_x1, CANONICAL_WIDTH),
            ("canonical_x2", self.canonical_x2, CANONICAL_WIDTH),
            ("canonical_y1", self.canonical_y1, CANONICAL_HEIGHT),
            ("canonical_y2", self.canonical_y2, CANONICAL_HEIGHT),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 0 <= value < limit:
                raise ValueError(f"{name} is outside the canonical client")
        if isinstance(self.duration_ms, bool) or not isinstance(self.duration_ms, int):
            raise TypeError("swipe duration must be an integer")
        if not 1 <= self.duration_ms <= 2000:
            raise ValueError("swipe duration must be between 1 and 2000 ms")
        left, top, right, bottom = self.verification_box
        if not (0 <= left < right <= CANONICAL_WIDTH):
            raise ValueError("verification_box has an invalid horizontal range")
        if not (0 <= top < bottom <= CANONICAL_HEIGHT):
            raise ValueError("verification_box has an invalid vertical range")
        if not all(
            left <= x < right and top <= y < bottom
            for x, y in (
                (self.canonical_x1, self.canonical_y1),
                (self.canonical_x2, self.canonical_y2),
            )
        ):
            raise ValueError("suggested swipe endpoints must be inside verification_box")
        if self.source_hwnd <= 0 or self.source_pid <= 0:
            raise ValueError("suggested swipe must be bound to a window and process")
        if self.action_type != "swipe":
            raise ValueError("unsupported suggested swipe action type")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SuggestedSwipe":
        if payload.get("action_type") != "swipe":
            raise ValueError("swipe action must declare action_type=swipe")
        raw_box = payload["verification_box"]
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            raise ValueError("verification_box must contain four integers")
        return cls(
            label=str(payload["label"]),
            canonical_x1=int(payload["canonical_x1"]),
            canonical_y1=int(payload["canonical_y1"]),
            canonical_x2=int(payload["canonical_x2"]),
            canonical_y2=int(payload["canonical_y2"]),
            duration_ms=int(payload["duration_ms"]),
            source_png_path=str(payload["source_png_path"]),
            source_timestamp=float(payload["source_timestamp"]),
            source_hwnd=int(payload["source_hwnd"]),
            source_pid=int(payload["source_pid"]),
            verification_box=tuple(int(value) for value in raw_box),  # type: ignore[arg-type]
            action_type="swipe",
        )


@dataclass(frozen=True, slots=True)
class FrameDifference:
    mean_absolute: float
    changed_fraction: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClickExecutionResult:
    action_label: str
    window_x: int
    window_y: int
    pre_capture: Mapping[str, Any]
    post_capture: Mapping[str, Any]
    source_difference: FrameDifference
    post_difference: FrameDifference
    visual_change_detected: bool
    click_results: tuple[Mapping[str, Any], ...]
    intermediate_capture: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "source_difference": self.source_difference.to_dict(),
            "post_difference": self.post_difference.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ChoiceExecutionResult:
    """Evidence for a select -> stable preview -> confirm choice."""

    action_label: str
    expected_page: str
    expected_target: str
    window_x: int
    window_y: int
    pre_capture: Mapping[str, Any]
    preview_captures: tuple[Mapping[str, Any], Mapping[str, Any]]
    post_capture: Mapping[str, Any]
    source_difference: FrameDifference
    post_difference: FrameDifference
    preview_analyses: tuple[ChoicePreviewAnalysis, ChoicePreviewAnalysis]
    visual_change_detected: bool
    click_results: tuple[Mapping[str, Any], Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_label": self.action_label,
            "expected_page": self.expected_page,
            "expected_target": self.expected_target,
            "window_x": self.window_x,
            "window_y": self.window_y,
            "pre_capture": dict(self.pre_capture),
            "preview_captures": [dict(value) for value in self.preview_captures],
            "post_capture": dict(self.post_capture),
            "source_difference": self.source_difference.to_dict(),
            "post_difference": self.post_difference.to_dict(),
            "preview_analyses": [asdict(value) for value in self.preview_analyses],
            "visual_change_detected": self.visual_change_detected,
            "click_results": [dict(value) for value in self.click_results],
        }


@dataclass(frozen=True, slots=True)
class CardPlayFrameAnalysis:
    """Semantic classification of one frame in a pending card play.

    ``semantic_key`` deliberately excludes animated/background pixels.  It must
    contain every visible HUD and hand field used to decide that two frames are
    the same committed game state.  ``expected_matches`` is kept separate so a
    stable but surprising result can be archived without being committed.
    """

    phase: CardPlayPhase
    semantic_key: tuple[Any, ...] | None = None
    observed_state: Mapping[str, Any] = field(default_factory=dict)
    # Full model state that may be committed only after the independently
    # observed fields match.  It is deliberately separate from observed_state
    # so unobservable prediction fields never masquerade as measurements.
    verified_state: Mapping[str, Any] = field(default_factory=dict)
    expected_matches: bool = False
    selected_card_matches: bool | None = None
    confidence: float = 0.0
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.phase not in {
            "selected_preview",
            "resolving",
            "settled",
            "result",
            "failed",
        }:
            raise ValueError("unsupported card-play frame phase")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("frame confidence must be between zero and one")
        if self.semantic_key is not None and type(self.semantic_key) is not tuple:
            raise TypeError("semantic_key must be an exact tuple or None")
        if not isinstance(self.observed_state, Mapping):
            raise TypeError("observed_state must be a mapping")
        if not isinstance(self.verified_state, Mapping):
            raise TypeError("verified_state must be a mapping")
        if type(self.expected_matches) is not bool:
            raise TypeError("expected_matches must be boolean")
        if self.selected_card_matches is not None and type(
            self.selected_card_matches
        ) is not bool:
            raise TypeError("selected_card_matches must be boolean or None")
        if type(self.issues) is not tuple or any(
            type(value) is not str for value in self.issues
        ):
            raise TypeError("issues must be an exact tuple of text")
        if self.phase in {"settled", "result"} and self.semantic_key is None:
            raise ValueError("settled/result frames require a semantic key")
        if self.expected_matches and self.phase in {"settled", "result"}:
            if not self.verified_state:
                raise ValueError(
                    "matching settled/result frames require a verified model state"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "semantic_key": (
                list(self.semantic_key) if self.semantic_key is not None else None
            ),
            "observed_state": dict(self.observed_state),
            "verified_state": dict(self.verified_state),
            "expected_matches": self.expected_matches,
            "selected_card_matches": self.selected_card_matches,
            "confidence": self.confidence,
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class CardPlayFrameSample:
    capture: Mapping[str, Any]
    analysis: CardPlayFrameAnalysis

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture": dict(self.capture),
            "analysis": self.analysis.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class CardPlayExecutionResult:
    """Evidence-rich result of one conditionally confirmed card play."""

    action_label: str
    window_x: int
    window_y: int
    pre_capture: Mapping[str, Any]
    preview_capture: Mapping[str, Any]
    post_capture: Mapping[str, Any]
    source_difference: FrameDifference
    post_difference: FrameDifference
    visual_change_detected: bool
    click_results: tuple[Mapping[str, Any], ...]
    phase_samples: tuple[CardPlayFrameSample, ...]
    committed: bool
    failure_reason: str | None
    observed_state: Mapping[str, Any]
    verified_state: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_label": self.action_label,
            "window_x": self.window_x,
            "window_y": self.window_y,
            "pre_capture": dict(self.pre_capture),
            "preview_capture": dict(self.preview_capture),
            "post_capture": dict(self.post_capture),
            "source_difference": self.source_difference.to_dict(),
            "post_difference": self.post_difference.to_dict(),
            "visual_change_detected": self.visual_change_detected,
            "click_results": [dict(item) for item in self.click_results],
            "phase_samples": [sample.to_dict() for sample in self.phase_samples],
            "committed": self.committed,
            "failure_reason": self.failure_reason,
            "observed_state": dict(self.observed_state),
            "verified_state": dict(self.verified_state),
        }


def canonical_to_outer_window(
    canonical_x: int,
    canonical_y: int,
    geometry: Mapping[str, Any],
) -> tuple[int, int]:
    """Convert a client-space Maa coordinate to controller outer-window space."""

    if not 0 <= canonical_x < CANONICAL_WIDTH:
        raise ValueError("canonical_x is outside the client")
    if not 0 <= canonical_y < CANONICAL_HEIGHT:
        raise ValueError("canonical_y is outside the client")
    client_width = int(geometry["client_width"])
    client_height = int(geometry["client_height"])
    offset_x = int(geometry["client_offset_x"])
    offset_y = int(geometry["client_offset_y"])
    if client_width <= 0 or client_height <= 0:
        raise ValueError("controller returned invalid client geometry")
    client_x = round(canonical_x * client_width / CANONICAL_WIDTH)
    client_y = round(canonical_y * client_height / CANONICAL_HEIGHT)
    client_x = min(client_x, client_width - 1)
    client_y = min(client_y, client_height - 1)
    return offset_x + client_x, offset_y + client_y


def _scaled_box(
    box: CanonicalBox,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    left, top, right, bottom = box
    return (
        round(left * width / CANONICAL_WIDTH),
        round(top * height / CANONICAL_HEIGHT),
        round(right * width / CANONICAL_WIDTH),
        round(bottom * height / CANONICAL_HEIGHT),
    )


def compare_frame_paths(
    before_path: str | Path,
    after_path: str | Path,
    *,
    canonical_box: CanonicalBox | None = None,
    signature_size: tuple[int, int] = (90, 160),
    pixel_threshold: float = 20 / 255,
) -> FrameDifference:
    """Return normalized low-resolution visual difference between two PNGs."""

    with Image.open(Path(before_path).resolve()) as before_image:
        before = before_image.convert("RGB")
    with Image.open(Path(after_path).resolve()) as after_image:
        after = after_image.convert("RGB")
    if canonical_box is not None:
        before = before.crop(_scaled_box(canonical_box, before.width, before.height))
        after = after.crop(_scaled_box(canonical_box, after.width, after.height))
    before_array = np.asarray(
        before.resize(signature_size, Image.Resampling.BILINEAR), dtype=np.float32
    ) / 255.0
    after_array = np.asarray(
        after.resize(signature_size, Image.Resampling.BILINEAR), dtype=np.float32
    ) / 255.0
    delta = np.mean(np.abs(before_array - after_array), axis=2)
    return FrameDifference(
        mean_absolute=float(np.mean(delta)),
        changed_fraction=float(np.mean(delta >= pixel_threshold)),
    )


def _capture_path(capture: Mapping[str, Any]) -> str:
    path = capture.get("png_path")
    if not isinstance(path, str) or not path:
        raise RuntimeError("controller capture did not provide a PNG path")
    if not Path(path).is_file():
        raise FileNotFoundError(f"controller capture PNG is missing: {path}")
    return path


def _require_bound_capture(
    capture: Mapping[str, Any], action: SuggestedClick
) -> None:
    if int(capture.get("hwnd", 0)) != action.source_hwnd:
        raise StaleSuggestionError("game window changed after the suggestion was made")
    if int(capture.get("pid", 0)) != action.source_pid:
        raise StaleSuggestionError("game process changed after the suggestion was made")


def execute_suggested_click(
    action: SuggestedClick,
    *,
    command_sender: CommandSender | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    max_age_seconds: float = 300.0,
    settle_seconds: float = 0.65,
    inter_click_seconds: float = 0.5,
    stale_mean_limit: float = 0.045,
    stale_fraction_limit: float = 0.18,
    changed_mean_threshold: float = 0.025,
    changed_fraction_threshold: float = 0.08,
    semantic_stale_verifier: SemanticStaleVerifier | None = None,
) -> ClickExecutionResult:
    """Perform one non-card click; confirmations require a semantic workflow.

    A second input is a submission on several game surfaces.  This generic
    visual-difference helper cannot establish that a SELECT preview identifies
    the intended target, so it deliberately refuses declared double clicks.
    """

    if action.click_count != 1:
        raise ValueError(
            "generic click execution refuses two-phase actions; "
            "a verified preview/confirmation workflow is required"
        )
    if now() - action.source_timestamp > max_age_seconds:
        raise StaleSuggestionError("suggestion expired; analyze the current screen again")
    if command_sender is None:
        from .controller_client import send_command

        command_sender = send_command

    status = dict(command_sender("status"))
    if int(status.get("controller_version", 0)) < 7:
        raise RuntimeError("elevated controller is too old for verified clicking")
    if int(status.get("target_hwnd", 0)) != action.source_hwnd:
        raise StaleSuggestionError("controller is bound to a different game window")
    if int(status.get("target_pid", 0)) != action.source_pid:
        raise StaleSuggestionError("controller is bound to a different game process")

    pre_capture = dict(command_sender("capture_screen_once", timeout=15.0))
    _require_bound_capture(pre_capture, action)
    pre_path = _capture_path(pre_capture)
    source_difference = compare_frame_paths(
        action.source_png_path,
        pre_path,
        canonical_box=action.verification_box,
    )
    if (
        source_difference.mean_absolute > stale_mean_limit
        or source_difference.changed_fraction > stale_fraction_limit
    ):
        # Animated entrances can invalidate a raw pixel signature while the
        # intended screen and target remain identical.  This opt-in callback
        # receives both evidence images and the immutable target binding; it
        # must positively prove identity.  A false result or any OCR/analyzer
        # exception remains a stale rejection, never a permission to click.
        if semantic_stale_verifier is None:
            raise StaleSuggestionError("the suggested UI target is no longer on screen")
        try:
            semantically_current = bool(
                semantic_stale_verifier(Path(action.source_png_path), Path(pre_path), action)
            )
        except Exception as error:
            raise StaleSuggestionError(
                f"semantic stale verifier failed: {type(error).__name__}: {error}"
            ) from error
        if not semantically_current:
            raise StaleSuggestionError(
                "semantic stale verifier did not confirm the same screen and target"
            )

    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise RuntimeError("controller status is missing window geometry")
    window_x, window_y = canonical_to_outer_window(
        action.canonical_x,
        action.canonical_y,
        geometry,
    )
    click_results: list[Mapping[str, Any]] = []
    click_results.append(dict(
        command_sender(
            "send_input_click_once",
            window_x=window_x,
            window_y=window_y,
        )
    ))
    intermediate_capture: Mapping[str, Any] | None = None
    sleep(max(0.0, settle_seconds))
    post_capture = dict(command_sender("capture_screen_once", timeout=15.0))
    _require_bound_capture(post_capture, action)
    post_path = _capture_path(post_capture)
    post_difference = compare_frame_paths(pre_path, post_path)
    visual_change_detected = (
        post_difference.mean_absolute >= changed_mean_threshold
        and post_difference.changed_fraction >= changed_fraction_threshold
    )
    return ClickExecutionResult(
        action_label=action.label,
        window_x=window_x,
        window_y=window_y,
        pre_capture=pre_capture,
        post_capture=post_capture,
        source_difference=source_difference,
        post_difference=post_difference,
        visual_change_detected=visual_change_detected,
        click_results=tuple(click_results),
        intermediate_capture=intermediate_capture,
    )


def execute_verified_choice_click(
    action: SuggestedClick,
    frame_analyzer: Callable[[Path], ChoicePreviewAnalysis],
    *,
    expected_page: str,
    expected_target: str,
    command_sender: CommandSender | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    max_age_seconds: float = 30.0,
    select_settle_seconds: float = 0.65,
    stable_sample_seconds: float = 0.35,
    confirm_settle_seconds: float = 0.65,
    minimum_confidence: float = 0.80,
    stale_mean_limit: float = 0.045,
    stale_fraction_limit: float = 0.18,
    changed_mean_threshold: float = 0.025,
    changed_fraction_threshold: float = 0.08,
) -> ChoiceExecutionResult:
    """Execute one declared two-stage choice after two stable preview proofs.

    The first input may only select/highlight a row.  A second input is sent
    only when two fresh, window-bound captures independently classify the
    same semantic page and target, report that target highlighted, and expose
    an identical semantic key.  Analyzer failure is a hard stop before the
    confirmation input.
    """

    if action.click_count != 2:
        raise ValueError("verified choice execution requires click_count=2")
    if not expected_page or not expected_target:
        raise ValueError("expected choice page and target must be non-empty")
    if not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("minimum_confidence must be between zero and one")
    if now() - action.source_timestamp > max_age_seconds:
        raise StaleSuggestionError("suggestion expired; analyze the current screen again")
    if command_sender is None:
        from .controller_client import send_command

        command_sender = send_command

    status = dict(command_sender("status"))
    if int(status.get("controller_version", 0)) < 7:
        raise RuntimeError("elevated controller is too old for verified clicking")
    if not bool(status.get("background_control")):
        raise RuntimeError("verified choice execution requires MAA background control")
    if int(status.get("target_hwnd", 0)) != action.source_hwnd:
        raise StaleSuggestionError("controller is bound to a different game window")
    if int(status.get("target_pid", 0)) != action.source_pid:
        raise StaleSuggestionError("controller is bound to a different game process")
    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise RuntimeError("controller status is missing window geometry")

    pre_capture = dict(command_sender("capture_screen_once", timeout=15.0))
    _require_bound_capture(pre_capture, action)
    pre_path = _capture_path(pre_capture)
    source_difference = compare_frame_paths(
        action.source_png_path,
        pre_path,
        canonical_box=action.verification_box,
    )
    if (
        source_difference.mean_absolute > stale_mean_limit
        or source_difference.changed_fraction > stale_fraction_limit
    ):
        raise StaleSuggestionError("the suggested UI target is no longer on screen")

    window_x, window_y = canonical_to_outer_window(
        action.canonical_x,
        action.canonical_y,
        geometry,
    )
    first_click = dict(
        command_sender(
            "send_input_click_once",
            window_x=window_x,
            window_y=window_y,
        )
    )

    preview_captures: list[Mapping[str, Any]] = []
    preview_analyses: list[ChoicePreviewAnalysis] = []
    for delay in (select_settle_seconds, stable_sample_seconds):
        sleep(max(0.0, delay))
        capture = dict(command_sender("capture_screen_once", timeout=15.0))
        _require_bound_capture(capture, action)
        analysis = frame_analyzer(Path(_capture_path(capture)))
        if analysis.page != expected_page:
            raise StaleSuggestionError("choice preview left the expected semantic page")
        if analysis.target_key != expected_target:
            raise StaleSuggestionError("choice preview selected a different target")
        if not analysis.highlighted:
            raise StaleSuggestionError("choice preview did not highlight the target")
        if analysis.confidence < minimum_confidence or analysis.issues:
            raise StaleSuggestionError("choice preview evidence is not trustworthy")
        preview_captures.append(capture)
        preview_analyses.append(analysis)

    if preview_analyses[0].semantic_key != preview_analyses[1].semantic_key:
        raise StaleSuggestionError("choice preview did not settle to one semantic state")

    second_click = dict(
        command_sender(
            "send_input_click_once",
            window_x=window_x,
            window_y=window_y,
        )
    )
    sleep(max(0.0, confirm_settle_seconds))
    post_capture = dict(command_sender("capture_screen_once", timeout=15.0))
    _require_bound_capture(post_capture, action)
    post_difference = compare_frame_paths(pre_path, _capture_path(post_capture))
    visual_change_detected = (
        post_difference.mean_absolute >= changed_mean_threshold
        and post_difference.changed_fraction >= changed_fraction_threshold
    )
    return ChoiceExecutionResult(
        action_label=action.label,
        expected_page=expected_page,
        expected_target=expected_target,
        window_x=window_x,
        window_y=window_y,
        pre_capture=pre_capture,
        preview_captures=(preview_captures[0], preview_captures[1]),
        post_capture=post_capture,
        source_difference=source_difference,
        post_difference=post_difference,
        preview_analyses=(preview_analyses[0], preview_analyses[1]),
        visual_change_detected=visual_change_detected,
        click_results=(first_click, second_click),
    )


def execute_verified_card_click(
    action: SuggestedClick,
    frame_analyzer: Callable[[Path], CardPlayFrameAnalysis],
    *,
    command_sender: CommandSender | None = None,
    pre_input_authorizer: Callable[[Mapping[str, Any]], None] | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    max_age_seconds: float = DEFAULT_VERIFIED_CARD_MAX_AGE_SECONDS,
    preview_seconds: float = 0.5,
    stable_sample_seconds: float = 0.65,
    # Long performance animations observed in archived captures can take about
    # fifteen seconds; keep the retry read-only and bounded beyond that window.
    max_settle_samples: int = 32,
    stale_mean_limit: float = 0.045,
    stale_fraction_limit: float = 0.18,
    changed_mean_threshold: float = 0.025,
    changed_fraction_threshold: float = 0.08,
) -> CardPlayExecutionResult:
    """Select, confirm, and commit one card only after semantic verification.

    The first click is allowed to reveal the game's SELECT preview.  The same
    point is clicked a second time only when that preview explicitly identifies
    the intended card.  Afterwards the function never retries input: it merely
    samples bounded frames until two consecutive semantic states agree.  A
    visual pixel change is retained for diagnostics but is never a success
    condition.
    """

    if action.click_count != 1:
        raise ValueError(
            "verified card actions must declare one target; confirmation is "
            "controlled by the SELECT state machine"
        )
    if max_settle_samples < 2:
        raise ValueError("max_settle_samples must allow at least two frames")
    if now() - action.source_timestamp > max_age_seconds:
        raise StaleSuggestionError("suggestion expired; analyze the current screen again")
    if command_sender is None:
        from .controller_client import send_command

        command_sender = send_command

    status = dict(command_sender("status"))
    if int(status.get("controller_version", 0)) < 7:
        raise RuntimeError("elevated controller is too old for verified clicking")
    if not bool(status.get("background_control")):
        raise RuntimeError("verified card play requires background window control")
    if int(status.get("target_hwnd", 0)) != action.source_hwnd:
        raise StaleSuggestionError("controller is bound to a different game window")
    if int(status.get("target_pid", 0)) != action.source_pid:
        raise StaleSuggestionError("controller is bound to a different game process")

    pre_capture = dict(command_sender("capture_screen_once", timeout=15.0))
    _require_bound_capture(pre_capture, action)
    pre_path = _capture_path(pre_capture)
    source_difference = compare_frame_paths(
        action.source_png_path,
        pre_path,
        canonical_box=action.verification_box,
    )
    if (
        source_difference.mean_absolute > stale_mean_limit
        or source_difference.changed_fraction > stale_fraction_limit
    ):
        raise StaleSuggestionError("the suggested UI target is no longer on screen")
    if pre_input_authorizer is not None:
        # This hook is intentionally after the fresh, HWND/PID-bound capture
        # and immediately before the first input.  Durable action callers use
        # it to replay their evidence CAS while holding their transaction lock.
        pre_input_authorizer(pre_capture)

    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise RuntimeError("controller status is missing window geometry")
    window_x, window_y = canonical_to_outer_window(
        action.canonical_x,
        action.canonical_y,
        geometry,
    )
    click_results: list[Mapping[str, Any]] = [
        dict(
            command_sender(
                "send_input_click_once",
                window_x=window_x,
                window_y=window_y,
            )
        )
    ]

    sleep(max(0.0, preview_seconds))
    preview_capture = dict(command_sender("capture_screen_once", timeout=15.0))
    _require_bound_capture(preview_capture, action)
    preview_path = Path(_capture_path(preview_capture))
    try:
        preview_analysis = frame_analyzer(preview_path)
    except Exception as error:  # OCR/model failures are evidence, never permission.
        preview_analysis = CardPlayFrameAnalysis(
            phase="failed",
            issues=(f"{type(error).__name__}: {error}",),
        )
    samples: list[CardPlayFrameSample] = [
        CardPlayFrameSample(preview_capture, preview_analysis)
    ]

    def finish(
        *,
        committed: bool,
        failure_reason: str | None,
        post_capture: Mapping[str, Any],
        observed_state: Mapping[str, Any],
        verified_state: Mapping[str, Any],
    ) -> CardPlayExecutionResult:
        post_path = _capture_path(post_capture)
        post_difference = compare_frame_paths(pre_path, post_path)
        visual_change_detected = (
            post_difference.mean_absolute >= changed_mean_threshold
            and post_difference.changed_fraction >= changed_fraction_threshold
        )
        return CardPlayExecutionResult(
            action_label=action.label,
            window_x=window_x,
            window_y=window_y,
            pre_capture=pre_capture,
            preview_capture=preview_capture,
            post_capture=post_capture,
            source_difference=source_difference,
            post_difference=post_difference,
            visual_change_detected=visual_change_detected,
            click_results=tuple(click_results),
            phase_samples=tuple(samples),
            committed=committed,
            failure_reason=failure_reason,
            observed_state=dict(observed_state),
            verified_state=dict(verified_state),
        )

    if preview_analysis.phase != "selected_preview":
        return finish(
            committed=False,
            failure_reason="select-preview-not-confirmed",
            post_capture=preview_capture,
            observed_state=preview_analysis.observed_state,
            verified_state=preview_analysis.verified_state,
        )
    if preview_analysis.selected_card_matches is not True:
        return finish(
            committed=False,
            failure_reason="selected-card-mismatch",
            post_capture=preview_capture,
            observed_state=preview_analysis.observed_state,
            verified_state=preview_analysis.verified_state,
        )

    if pre_input_authorizer is not None:
        # SELECT is reversible, but CONFIRM is the irreversible card play.
        # Replay the caller's evidence CAS against the verified preview at the
        # last possible point before sending that second input.
        pre_input_authorizer(preview_capture)

    click_results.append(
        dict(
            command_sender(
                "send_input_click_once",
                window_x=window_x,
                window_y=window_y,
            )
        )
    )

    previous_stable: CardPlayFrameSample | None = None
    last_capture: Mapping[str, Any] = preview_capture
    last_observed: Mapping[str, Any] = preview_analysis.observed_state
    last_verified: Mapping[str, Any] = preview_analysis.verified_state
    failure_reason = "settled-frame-timeout"
    for _index in range(max_settle_samples):
        sleep(max(0.0, stable_sample_seconds))
        capture = dict(command_sender("capture_screen_once", timeout=15.0))
        _require_bound_capture(capture, action)
        path = Path(_capture_path(capture))
        try:
            analysis = frame_analyzer(path)
        except Exception as error:  # stop safely after preserving the frame.
            analysis = CardPlayFrameAnalysis(
                phase="failed",
                issues=(f"{type(error).__name__}: {error}",),
            )
        sample = CardPlayFrameSample(capture, analysis)
        samples.append(sample)
        last_capture = capture
        last_observed = analysis.observed_state
        last_verified = analysis.verified_state

        if analysis.phase == "failed":
            # The irreversible CONFIRM input has already been submitted.  A
            # support-card cut-in or another transient animation can make one
            # screenshot intentionally unclassifiable; that is not evidence
            # that the input failed.  Forget any partially stable pair and
            # keep sampling within the existing bounded window.  We still
            # fail closed if no two matching settled/result frames arrive.
            failure_reason = "frame-analysis-failed"
            previous_stable = None
            continue
        if analysis.phase not in {"settled", "result"}:
            previous_stable = None
            continue
        if previous_stable is None:
            previous_stable = sample
            continue

        previous = previous_stable.analysis
        same_semantic_state = (
            previous.phase == analysis.phase
            and previous.semantic_key == analysis.semantic_key
        )
        if not same_semantic_state:
            previous_stable = sample
            continue
        if previous.expected_matches and analysis.expected_matches:
            return finish(
                committed=True,
                failure_reason=None,
                post_capture=last_capture,
                observed_state=last_observed,
                verified_state=last_verified,
            )
        failure_reason = "stable-state-mismatch"
        break

    return finish(
        committed=False,
        failure_reason=failure_reason,
        post_capture=last_capture,
        observed_state=last_observed,
        verified_state=last_verified,
    )


def execute_verified_card_confirmation(
    action: SuggestedClick,
    frame_analyzer: Callable[[Path], CardPlayFrameAnalysis],
    *,
    original_pre_capture: Mapping[str, Any],
    preview_source_capture: Mapping[str, Any],
    command_sender: CommandSender | None = None,
    pre_input_authorizer: Callable[[Mapping[str, Any]], None] | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    max_age_seconds: float = DEFAULT_VERIFIED_CARD_MAX_AGE_SECONDS,
    stable_sample_seconds: float = 0.65,
    max_settle_samples: int = 32,
    changed_mean_threshold: float = 0.025,
    changed_fraction_threshold: float = 0.08,
) -> CardPlayExecutionResult:
    """Resume an already selected card without repeating its first click.

    This is the recovery path for a prior execution that stopped after the
    selection click.  The supplied preview is evidence only: a fresh capture
    must independently decode as ``selected_preview`` and identify the exact
    expected card before the single confirmation click is sent.  No click is
    retried after confirmation.
    """

    if action.click_count != 1:
        raise ValueError("verified card confirmation requires one target")
    if max_settle_samples < 2:
        raise ValueError("max_settle_samples must allow at least two frames")
    preview_timestamp = preview_source_capture.get("timestamp")
    if not isinstance(preview_timestamp, (int, float)) or isinstance(
        preview_timestamp, bool
    ):
        raise ValueError("preview_source_capture has no timestamp")
    if now() - float(preview_timestamp) > max_age_seconds:
        raise StaleSuggestionError("selected preview expired; reanalyze the hand")
    if command_sender is None:
        from .controller_client import send_command

        command_sender = send_command

    status = dict(command_sender("status"))
    if int(status.get("controller_version", 0)) < 7:
        raise RuntimeError("elevated controller is too old for verified clicking")
    if not bool(status.get("background_control")):
        raise RuntimeError("verified card play requires background window control")
    if int(status.get("target_hwnd", 0)) != action.source_hwnd:
        raise StaleSuggestionError("controller is bound to a different game window")
    if int(status.get("target_pid", 0)) != action.source_pid:
        raise StaleSuggestionError("controller is bound to a different game process")

    pre_capture = dict(original_pre_capture)
    _require_bound_capture(pre_capture, action)
    pre_path = _capture_path(pre_capture)
    if Path(pre_path).resolve() != Path(action.source_png_path).resolve():
        raise StaleSuggestionError("resume pre-capture differs from the original hand")
    source_difference = compare_frame_paths(
        action.source_png_path,
        pre_path,
        canonical_box=action.verification_box,
    )
    preview_source = dict(preview_source_capture)
    _require_bound_capture(preview_source, action)
    _capture_path(preview_source)

    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise RuntimeError("controller status is missing window geometry")
    window_x, window_y = canonical_to_outer_window(
        action.canonical_x,
        action.canonical_y,
        geometry,
    )
    preview_capture = dict(command_sender("capture_screen_once", timeout=15.0))
    _require_bound_capture(preview_capture, action)
    preview_path = Path(_capture_path(preview_capture))
    try:
        preview_analysis = frame_analyzer(preview_path)
    except Exception as error:
        preview_analysis = CardPlayFrameAnalysis(
            phase="failed",
            issues=(f"{type(error).__name__}: {error}",),
        )
    samples: list[CardPlayFrameSample] = [
        CardPlayFrameSample(preview_capture, preview_analysis)
    ]
    click_results: list[Mapping[str, Any]] = []

    def finish(
        *,
        committed: bool,
        failure_reason: str | None,
        post_capture: Mapping[str, Any],
        observed_state: Mapping[str, Any],
        verified_state: Mapping[str, Any],
    ) -> CardPlayExecutionResult:
        post_path = _capture_path(post_capture)
        post_difference = compare_frame_paths(pre_path, post_path)
        visual_change_detected = (
            post_difference.mean_absolute >= changed_mean_threshold
            and post_difference.changed_fraction >= changed_fraction_threshold
        )
        return CardPlayExecutionResult(
            action_label=action.label,
            window_x=window_x,
            window_y=window_y,
            pre_capture=pre_capture,
            preview_capture=preview_capture,
            post_capture=post_capture,
            source_difference=source_difference,
            post_difference=post_difference,
            visual_change_detected=visual_change_detected,
            click_results=tuple(click_results),
            phase_samples=tuple(samples),
            committed=committed,
            failure_reason=failure_reason,
            observed_state=dict(observed_state),
            verified_state=dict(verified_state),
        )

    if preview_analysis.phase != "selected_preview":
        return finish(
            committed=False,
            failure_reason="select-preview-not-confirmed",
            post_capture=preview_capture,
            observed_state=preview_analysis.observed_state,
            verified_state=preview_analysis.verified_state,
        )
    if preview_analysis.selected_card_matches is not True:
        return finish(
            committed=False,
            failure_reason="selected-card-mismatch",
            post_capture=preview_capture,
            observed_state=preview_analysis.observed_state,
            verified_state=preview_analysis.verified_state,
        )

    if pre_input_authorizer is not None:
        # Confirmation is the only input on this recovery path.  Authorize it
        # only after a fresh preview has independently identified the card.
        pre_input_authorizer(preview_capture)

    click_results.append(
        dict(
            command_sender(
                "send_input_click_once",
                window_x=window_x,
                window_y=window_y,
            )
        )
    )
    previous_stable: CardPlayFrameSample | None = None
    last_capture: Mapping[str, Any] = preview_capture
    last_observed: Mapping[str, Any] = preview_analysis.observed_state
    last_verified: Mapping[str, Any] = preview_analysis.verified_state
    failure_reason = "settled-frame-timeout"
    for _index in range(max_settle_samples):
        sleep(max(0.0, stable_sample_seconds))
        capture = dict(command_sender("capture_screen_once", timeout=15.0))
        _require_bound_capture(capture, action)
        path = Path(_capture_path(capture))
        try:
            analysis = frame_analyzer(path)
        except Exception as error:
            analysis = CardPlayFrameAnalysis(
                phase="failed",
                issues=(f"{type(error).__name__}: {error}",),
            )
        sample = CardPlayFrameSample(capture, analysis)
        samples.append(sample)
        last_capture = capture
        last_observed = analysis.observed_state
        last_verified = analysis.verified_state
        if analysis.phase == "failed":
            # Confirmation was already submitted on this recovery path.  A
            # transient animation frame is observationally inconclusive, so
            # continue bounded read-only sampling instead of misreporting the
            # irreversible input as unsent.
            failure_reason = "frame-analysis-failed"
            previous_stable = None
            continue
        if analysis.phase not in {"settled", "result"}:
            previous_stable = None
            continue
        if previous_stable is None:
            previous_stable = sample
            continue
        previous = previous_stable.analysis
        if not (
            previous.phase == analysis.phase
            and previous.semantic_key == analysis.semantic_key
        ):
            previous_stable = sample
            continue
        if previous.expected_matches and analysis.expected_matches:
            return finish(
                committed=True,
                failure_reason=None,
                post_capture=last_capture,
                observed_state=last_observed,
                verified_state=last_verified,
            )
        failure_reason = "stable-state-mismatch"
        break
    return finish(
        committed=False,
        failure_reason=failure_reason,
        post_capture=last_capture,
        observed_state=last_observed,
        verified_state=last_verified,
    )
