"""Screenshot-only adapters for the Live tab.

The calibration replay is labelled as recorded evidence rather than a live
hand. Live overview/shop readers use visible pixels and static files only.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence

from .regular_state import RegularTurnState

if TYPE_CHECKING:
    from .reward_state import RewardPreviewDisambiguation


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CALIBRATION_SAMPLE = PROJECT_ROOT / "tests" / "fixtures" / "live_logic_lesson_20260728.json"
DECK_PANEL_BUTTON_CENTER = (558, 1200)
DECK_PANEL_BUTTON_BOX = (520, 1148, 598, 1276)
# SELECT is rendered beneath the selected card, not at a fixed screen centre.
# These centres cover the stable two-to-five-card fan layouts.  The narrow
# crop deliberately excludes neighbouring card titles; the detail-panel title
# is still verified independently before a confirmation click is allowed.
CARD_PREVIEW_SELECT_CENTERS = (
    80,
    143,
    220,
    251,
    287,
    359,
    431,
    468,
    503,
    575,
    640,
)
CARD_PREVIEW_SELECT_HALF_WIDTH = 70
CARD_PREVIEW_SELECT_Y_RANGE = (1090, 1145)
CARD_PREVIEW_PANEL_BOX = (90, 575, 630, 845)
CARD_PREVIEW_NAME_BOX = (240, 585, 480, 650)
LESSON_RESULT_TEXT_BOX = (30, 560, 350, 720)
EXAM_RESULT_NEXT_BOX = (180, 1040, 540, 1210)
POST_AUDITION_DIALOGUE_BOX = (40, 950, 680, 1130)
POST_AUDITION_SUMMARY_CONTINUE_BOX = (120, 1100, 600, 1195)
INITIAL_OPENING_CHOICE_BOXES = (
    (50, 760, 670, 845),
    (50, 855, 670, 945),
)
# The fan-present card-acquisition animation is a full-screen informational
# overlay.  It has no card title or receive button yet: one tap on the
# glowing parcel advances to the actual three-card reward page.  Keep the
# semantic OCR crop and tap area in the shared live reader so Initial/Plan1,
# Plan2 and N.I.A. do not each grow a slightly different fallback.
CARD_ACQUIRE_NOTIFICATION_TEXT_BOX = (120, 650, 600, 820)
CARD_ACQUIRE_NOTIFICATION_TAP_BOX = (250, 610, 520, 1080)
CARD_ACQUIRE_NOTIFICATION_MAA_NODES = (
    "card-acquire-notification",
    "card-acquire",
    "reward-card-acquired",
)


@dataclass(frozen=True, slots=True)
class CardAcquireNotificationEvidence:
    """Typed proof for the one-tap card-acquisition animation."""

    title: str
    confidence: float
    action_box: tuple[int, int, int, int]
    source: str
    maa_node: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "confidence": self.confidence,
            "action_box": list(self.action_box),
            "source": self.source,
            "maa_node": self.maa_node,
        }


def detect_card_acquire_notification(
    image: Any,
    *,
    maa_nodes: Mapping[str, tuple[int, int, int, int, float]] | None = None,
) -> CardAcquireNotificationEvidence | None:
    """Recognize the settled ``獲得技能卡`` overlay without ranking cards.

    A dedicated Maa node, when present in a recognition batch, is the input
    geometry authority.  Otherwise the localized title is OCRed from the
    narrow centre band and the existing fan-present tap box is used.  The
    title gate is intentionally retained even for generic ``common-continue``
    / ``common-next`` matches so those shared buttons cannot steal an
    unrelated frame.
    """

    recognized = maa_nodes or {}
    typed_node: tuple[str, tuple[int, int, int, int, float]] | None = None
    for node_name in CARD_ACQUIRE_NOTIFICATION_MAA_NODES:
        marker = recognized.get(node_name)
        if marker is not None:
            typed_node = (node_name, marker)
            break

    # An explicit typed node is already a semantic Maa proof.  It is allowed
    # to survive the animated title frame where OCR is temporarily blank.
    if typed_node is not None:
        node_name, marker = typed_node
        return CardAcquireNotificationEvidence(
            title="",
            confidence=float(marker[4]),
            action_box=tuple(int(value) for value in marker[:4]),
            source="maa-node",
            maa_node=node_name,
        )

    from PIL import ImageOps

    recognizer = _live_text_recognizer()
    text_box = CARD_ACQUIRE_NOTIFICATION_TEXT_BOX
    read = recognizer.recognize(
        ImageOps.autocontrast(image.crop(text_box).convert("L"))
    )
    compact = re.sub(r"\s+", "", read.text)
    markers = (
        "獲得技能卡",
        "获得技能卡",
        "獲得技能牌",
        "获得技能牌",
        "スキルカード獲得",
        "スキルカードを獲得",
        "スキルカード入手",
    )
    if read.confidence < 0.78 or not any(marker in compact for marker in markers):
        return None

    # If the legacy Maa batch exposes a shared continuation, reuse its exact
    # recognized box.  The title OCR remains the semantic gate; the fixed box
    # below is only the screenshot-reader fallback.
    shared_node: tuple[str, tuple[int, int, int, int, float]] | None = None
    for node_name in ("common-continue", "common-next"):
        marker = recognized.get(node_name)
        if marker is None:
            continue
        if shared_node is None or marker[4] > shared_node[1][4]:
            shared_node = (node_name, marker)
    if shared_node is not None:
        node_name, marker = shared_node
        action_box = tuple(int(value) for value in marker[:4])
        source = "ocr-title+maa-node"
    else:
        action_box = CARD_ACQUIRE_NOTIFICATION_TAP_BOX
        source = "ocr-title"
    return CardAcquireNotificationEvidence(
        title=read.text,
        confidence=float(read.confidence),
        action_box=action_box,
        source=source,
        maa_node=None if shared_node is None else shared_node[0],
    )

@dataclass(frozen=True, slots=True)
class LiveCardRow:
    card_id: str
    upgrade: int | None
    label: str
    legal: str
    observed_stamina_cost: int | None = None
    cost_confidence: float = 0.0


@dataclass(frozen=True, slots=True)
class LiveView:
    state: RegularTurnState | None
    cards: tuple[LiveCardRow, ...]
    cards_label: str
    recommendation: str
    confidence: str
    reasons: tuple[str, ...]
    capture: Mapping[str, Any] | None = None


class LessonFrameNotReady(ValueError):
    """The game is between lesson frames and should be captured again."""


class ExamFrameNotReady(ValueError):
    """The game is between audition frames and should be captured again."""


@dataclass(frozen=True, slots=True)
class CardPreviewEvidence:
    """Screenshot evidence that the client is showing a hypothetical preview."""

    detected: bool
    select_text: str
    select_confidence: float
    panel_white_ratio: float


@dataclass(frozen=True, slots=True)
class CardPreviewIdentityEvidence:
    observed_text: str
    ocr_confidence: float
    card_id: str | None
    upgrade: int | None
    name_similarity: float
    matches_expected: bool


@dataclass(frozen=True, slots=True)
class RewardPlanContext:
    """Active-run identity joined to its authoritative idol Master plan."""

    run_id: str
    idol_card_id: str
    character_id: str
    produce_id: str
    plan_type: str

    @property
    def candidate_plan_types(self) -> tuple[str, str]:
        return ("ProducePlanType_Common", self.plan_type)

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "candidate_plan_types": list(self.candidate_plan_types),
        }


@dataclass(frozen=True, slots=True)
class LiveRewardPreviewWorkflowSession:
    """Run- and window-bound state for screenshot-only reward inspection."""

    context: RewardPlanContext
    overview_png_path: str
    overview_timestamp: float
    source_hwnd: int
    source_pid: int
    image_size: tuple[int, int]
    overview_detections: tuple[Any, ...]
    state: RewardPreviewDisambiguation
    last_capture_timestamp: float

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.context.to_dict(),
            "overview_png_path": self.overview_png_path,
            "overview_timestamp": self.overview_timestamp,
            "source_hwnd": self.source_hwnd,
            "source_pid": self.source_pid,
            "image_size": list(self.image_size),
            "last_capture_timestamp": self.last_capture_timestamp,
            "state": self.state.to_dict(),
            "collect_allowed": False,
        }


@dataclass(frozen=True, slots=True)
class CardPlayResultEvidence:
    detected: bool
    result_kind: str
    score: int | None
    rank_text: str
    confidence: float
    raw_text: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardSupportUpgradeMarkerEvidence:
    """Pixel proof for the small support-card ``+`` overlay on one Hand card.

    Only the fixed-position cross topology is observed.  Hue and saturation
    are deliberately ignored because the game may render the ``+`` in
    different colours.  The caller still has to reconcile the visible marker
    with the ordered LocalSave Hand; this marker alone never mutates state.
    """

    detected: bool
    score: float
    horizontal_ratio: float
    vertical_ratio: float
    corner_ratio: float
    support_card_id: str | None
    identity_score: float
    identity_margin: float
    box: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _card_support_upgrade_markers(
    image_path: str | Path,
    detections: Sequence[Any],
    support_card_ids: Sequence[str] = (),
) -> tuple[CardSupportUpgradeMarkerEvidence, ...]:
    """Detect temporary skill-card-support markers for ordered Hand boxes.

    The geometry was validated against archived Maa ``PrintWindow`` captures
    at the canonical 720-wide layout.  Coordinates are relative to each YOLO
    card box, so the same test remains valid when the backing window is scaled.
    A contrast shape is accepted only when it matches the invariant ``+``
    topology.  Exact ordered-slot reconciliation and two-frame stability
    remain separate mandatory gates.
    """

    import numpy as np
    from PIL import Image

    candidate_ids = tuple(
        dict.fromkeys(str(value) for value in support_card_ids if value)
    )

    results: list[CardSupportUpgradeMarkerEvidence] = []
    with Image.open(Path(image_path).resolve()) as source:
        rgb = source.convert("RGB")
        for detection in detections:
            left = int(detection.x + round(detection.width * 0.851))
            top = int(detection.y + round(detection.height * 0.337))
            right = int(detection.x + round(detection.width * 1.000))
            bottom = int(detection.y + round(detection.height * 0.450))
            left = max(0, min(left, rgb.width - 1))
            top = max(0, min(top, rgb.height - 1))
            right = max(left + 1, min(right, rgb.width))
            bottom = max(top + 1, min(bottom, rgb.height))
            array = np.asarray(
                rgb.crop((left, top, right, bottom)).resize(
                    (28, 28), Image.Resampling.BILINEAR
                ),
                dtype=np.float32,
            ) / 255.0
            luminance = (
                0.2126 * array[:, :, 0]
                + 0.7152 * array[:, :, 1]
                + 0.0722 * array[:, :, 2]
            )

            horizontal = np.zeros((28, 28), dtype=bool)
            horizontal[4:11, 1:24] = True
            vertical = np.zeros((28, 28), dtype=bool)
            vertical[0:19, 10:17] = True
            corners = np.zeros((28, 28), dtype=bool)
            corners[0:4, 0:8] = True
            corners[0:4, 20:28] = True
            background = float(np.median(luminance[corners]))
            contrasting = np.abs(luminance - background) >= 0.12
            horizontal_ratio = float(np.mean(contrasting[horizontal]))
            vertical_ratio = float(np.mean(contrasting[vertical]))
            corner_ratio = float(np.mean(contrasting[corners]))
            positive_ratio = float(np.mean(contrasting[horizontal | vertical]))
            score = positive_ratio - 0.5 * corner_ratio
            detected = bool(
                horizontal_ratio >= 0.60
                and vertical_ratio >= 0.55
                and score >= 0.60
            )
            support_card_id: str | None = None
            identity_score = 0.0
            identity_margin = 0.0
            if detected and len(candidate_ids) == 1:
                # Identity comes from the exact ordered LocalSave slot.  The
                # screen proves only that a cross exists there; no colour or
                # support-art classifier participates in the decision.
                support_card_id = candidate_ids[0]
                identity_score = 1.0
                identity_margin = 1.0
            results.append(
                CardSupportUpgradeMarkerEvidence(
                    detected=detected,
                    score=score,
                    horizontal_ratio=horizontal_ratio,
                    vertical_ratio=vertical_ratio,
                    corner_ratio=corner_ratio,
                    support_card_id=support_card_id,
                    identity_score=identity_score,
                    identity_margin=identity_margin,
                    box=(left, top, right, bottom),
                )
            )
    return tuple(results)


@lru_cache(maxsize=32)
def _support_marker_signature_catalog(
    support_card_ids: tuple[str, ...],
) -> tuple[tuple[str, ...], Any]:
    """Load only cached artwork for the run-bound support candidates."""

    import numpy as np
    from PIL import Image

    if not support_card_ids:
        return (), np.empty((0, 0), dtype=np.float32)
    from .support_loadout_vision import (
        DEFAULT_SUPPORT_ART_CACHE,
        _signature,
        support_card_candidates,
    )

    by_id = {candidate.card_id: candidate for candidate in support_card_candidates()}
    selected: list[str] = []
    signatures: list[Any] = []
    for support_card_id in support_card_ids:
        candidate = by_id.get(support_card_id)
        if candidate is None:
            continue
        path = DEFAULT_SUPPORT_ART_CACHE / f"{candidate.asset_name}.png"
        if not path.is_file():
            continue
        with Image.open(path) as source:
            art = source.convert("RGB")
            width, height = art.size
            if width < height or height < 2:
                continue
            # The in-exam badge uses the centre square of the landscape art
            # and crops its lower portion behind the overlaid plus symbol.
            transformed = art.crop(
                (
                    (width - height) // 2,
                    0,
                    (width + height) // 2,
                    round(height * 0.72),
                )
            )
            signatures.append(_signature(transformed))
            selected.append(support_card_id)
    if not signatures:
        return (), np.empty((0, 0), dtype=np.float32)
    return (
        tuple(selected),
        np.ascontiguousarray(np.stack(signatures), dtype=np.float32),
    )


@lru_cache(maxsize=1)
def _loadout_passive_catalog() -> Any:
    # Loading the YAML-backed catalog takes several seconds; it is immutable
    # for one process and must not be rebuilt for every captured frame.
    from .passive_catalog import MasterPassiveCatalog

    return MasterPassiveCatalog.load()


_START_CONFIRMATION_GROWTH_BOXES = {
    "vocal": (390, 405, 484, 450),
    "dance": (500, 405, 590, 450),
    "visual": (610, 405, 705, 450),
}


def _active_run_effective_growth_permils(
    *,
    idol_card_id: str,
    produce_id: str,
) -> tuple[int, int, int, str] | None:
    """Read exact effective growth rates from the run's saved start screen.

    The confirmation screen is immutable run-configuration evidence and shows
    the final rates after idol, support, and memory effects.  This helper never
    captures or operates the game.  Missing/mismatched/uncertain evidence is a
    normal ``None`` result so the planner can label its legacy deficit fallback.
    """

    from PIL import Image

    from .run_identity import load_active_run
    from .screen_state import scale_canonical_box
    from .text_recognizer import PaddleLineRecognizer

    active = load_active_run()
    if (
        active is None
        or active.idol_card_id != idol_card_id
        or active.produce_id != produce_id
    ):
        return None
    raw_path = active.evidence.get("loadout_confirmation_capture")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path)
    if not path.is_file():
        return None

    recognizer = PaddleLineRecognizer()
    values: dict[str, int] = {}
    with Image.open(path.resolve()) as image:
        image.load()
        for field, box in _START_CONFIRMATION_GROWTH_BOXES.items():
            result = recognizer.recognize(
                image.crop(scale_canonical_box(box, image.width, image.height))
            )
            match = re.fullmatch(r"\s*(\d{1,3})\.(\d)%\s*", result.text)
            if match is None or result.confidence < 0.95:
                return None
            values[field] = int(match.group(1)) * 10 + int(match.group(2))
    return (
        values["vocal"],
        values["dance"],
        values["visual"],
        f"active-run-start-confirmation:{active.run_id}",
    )


def _live_loadout_safety_metadata(
    *,
    step_context_verified: bool = False,
) -> dict[str, Any]:
    """Resolve replay IDs and hard blockers for the selected loadout.

    Reading a safe snapshot is intentionally separate from claiming that its
    start-step status contracts were installed before the visible HUD was
    produced.  Until the outer route adapter supplies that verified boundary,
    recommendations remain useful but background card execution is blocked.
    """

    from .loadout_runtime_bridge import (
        LoadoutRuntimeBridgeError,
        load_loadout_runtime_session,
        loadout_rule_identifiers,
        prepare_loadout_runtime,
    )
    from .loadout_snapshot import load_loadout_snapshot
    from .run_identity import load_active_run, paths_for

    empty_ids = {
        "memory_ids": [],
        "support_ids": [],
        "effect_ids": [],
        "trigger_ids": [],
        "status_enchant_ids": [],
    }
    active_run = load_active_run()
    if active_run is None:
        return {
            "run_id": None,
            "support_count": 0,
            "memory_count": 0,
            "identifiers": empty_ids,
            "auto_click_eligible": False,
            "blockers": ["active-run-missing"],
        }
    run_paths = paths_for(active_run)
    snapshot = load_loadout_snapshot(run_paths.loadout_snapshot)
    if snapshot is None:
        return {
            "run_id": active_run.run_id,
            "support_count": 0,
            "memory_count": 0,
            "identifiers": empty_ids,
            "auto_click_eligible": False,
            "blockers": ["loadout-snapshot-missing"],
        }
    if (
        snapshot.run_id != active_run.run_id
        or snapshot.produce_id != active_run.produce_id
    ):
        return {
            "run_id": active_run.run_id,
            "support_count": 0,
            "memory_count": 0,
            "identifiers": empty_ids,
            "auto_click_eligible": False,
            "blockers": ["loadout-snapshot-active-run-mismatch"],
        }
    try:
        session = load_loadout_runtime_session(run_paths.loadout_runtime_session)
        if session is None:
            return {
                "run_id": active_run.run_id,
                "support_count": len(snapshot.support_cards),
                "memory_count": len(snapshot.memories),
                "identifiers": empty_ids,
                "auto_click_eligible": False,
                "blockers": ["loadout-runtime-session-missing"],
            }
        prepared = prepare_loadout_runtime(
            snapshot,
            _loadout_passive_catalog(),
            session=session,
        )
        identifiers = loadout_rule_identifiers(snapshot, prepared).to_dict()
    except (LoadoutRuntimeBridgeError, ValueError, TypeError, OSError) as error:
        reasons = getattr(error, "reasons", None)
        if reasons is None:
            reasons = (f"loadout-runtime:{type(error).__name__}:{error}",)
        return {
            "run_id": snapshot.run_id,
            "support_count": len(snapshot.support_cards),
            "memory_count": len(snapshot.memories),
            "identifiers": empty_ids,
            "auto_click_eligible": False,
            "blockers": list(dict.fromkeys(str(reason) for reason in reasons)),
        }

    blockers: list[str] = []
    if any(prepared.runtime.modifiers.as_dict().values()) and not (
        prepared.session.initial_modifiers_applied
    ):
        blockers.append("loadout-initial-modifiers-not-verified")
    if prepared.runtime.status_enchant_installs and not step_context_verified:
        blockers.append("loadout-step-status-not-verified")
    return {
        "run_id": snapshot.run_id,
        "support_count": len(snapshot.support_cards),
        "memory_count": len(snapshot.memories),
        "identifiers": identifiers,
        "auto_click_eligible": not blockers,
        "blockers": blockers,
    }


def _merge_loadout_rule_ids(
    rule_ids: Mapping[str, Any], loadout: Mapping[str, Any]
) -> dict[str, list[str]]:
    merged = {
        key: list(value)
        for key, value in rule_ids.items()
    }
    identifiers = loadout.get("identifiers", {})
    if not isinstance(identifiers, Mapping):
        return merged
    for key in (
        "memory_ids",
        "support_ids",
        "effect_ids",
        "trigger_ids",
        "status_enchant_ids",
    ):
        existing = merged.get(key, [])
        additional = identifiers.get(key, [])
        if not isinstance(existing, list) or not isinstance(additional, list):
            continue
        merged[key] = list(dict.fromkeys((*existing, *additional)))
    return merged


def load_regular_turn(payload: Mapping[str, Any]) -> LiveView:
    """Adapt a StateBridge-compatible payload without interpreting its rules."""

    state = RegularTurnState.from_dict(payload)
    cards = tuple(
        LiveCardRow(card.card_id, card.upgrade, "StateBridge 手牌", _legal(card.legal))
        for card in state.hand
    )
    return LiveView(
        state, cards, "目前手牌（StateBridge）", "尚未接入此狀態的求解器", f"觀測信心 {state.observation.confidence:.0%}",
        ("已接受 RegularTurnState；不會臆測未提供的卡牌效果。",),
    )


def load_calibration_sample(path: Path = CALIBRATION_SAMPLE) -> LiveView:
    """Replay the checked-in visual calibration fixture as explicitly non-live data."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    transitions = payload.get("transitions")
    lesson = payload.get("lesson")
    if not isinstance(transitions, list) or len(transitions) < 4 or not isinstance(lesson, dict):
        raise ValueError("校正樣本缺少 lesson 或 transitions")
    final = transitions[-1]
    before = final.get("before")
    card = final.get("card")
    if not isinstance(before, dict) or not isinstance(card, dict):
        raise ValueError("校正樣本最後一回合格式不正確")
    observed = []
    for transition in transitions[:-1][-3:]:
        item = transition.get("card")
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("校正樣本的卡牌格式不正確")
        observed.append(LiveCardRow(item["id"], int(item.get("upgrade", 0)), "已觀測出牌（非目前手牌）", "已使用"))
    state = RegularTurnState.from_dict({
        "schema_version": 1, "mode": "first_star_regular", "turn": int(final.get("round", 1)),
        # By round four the 25-point clear gate has already been crossed, so the
        # active target shown in the Live tab is the 50-point Perfect gate.
        "clear": {"threshold": int(lesson["perfect_threshold"]), "current_parameter": int(before["current_parameter"])},
        "stamina": int(before["stamina"]),
        "buffs": [
            {"id": "block", "value": before.get("block", 0)},
            {"id": "good_impression", "value": before.get("good_impression", 0)},
            {"id": "good_impression_bonus_permille", "value": before.get("good_impression_bonus_permille", 0)},
        ],
        "hand": [],
        "observation": {"source": "live_visual_calibration_replay", "confidence": 1.0, "captured_at": str(payload["captured_at"])},
        "identity": payload.get("identity", {}),
    })
    predicted = final.get("predicted_score_gain")
    return LiveView(
        state, tuple(observed), "校正回放：前 3 次已觀測出牌（非目前手牌）",
        f"回放記錄：{card['id']} +{int(card.get('upgrade', 0))}", "校正回放；非即時辨識",
        (
            f"第 {state.turn} 回合前：參數 {state.clear.current_parameter}/{state.clear.threshold}、體力 {state.stamina}。",
            f"樣本記錄此牌預測增加 {predicted}；並記錄達成 Perfect。" if isinstance(predicted, int) else "樣本未記錄預測分數。",
            "此樣本沒有三張同步手牌；表格只呈現已觀測出牌，沒有把它偽裝成 OCR。",
        ),
    )


def capture_once(
    *,
    allow_development_fallback: bool = False,
) -> Mapping[str, Any]:
    """Capture through the persistent background controller when available."""

    from .controller_client import send_command
    from .live_capture import capture_live

    try:
        return dict(send_command("capture_once", timeout=15.0))
    except (ConnectionError, TimeoutError, RuntimeError):
        if not allow_development_fallback:
            raise
        # Explicit development-only fallback.  Production routing must never
        # mix a desktop frame with an uncleared Maa request.
        return asdict(capture_live(save_png=True, capture_mode="screen"))


def controller_status() -> Mapping[str, Any]:
    """Report capture readiness and optional elevated click availability."""

    from .controller_client import send_command
    from .live_capture import capture_live

    try:
        status = dict(send_command("status"))
    except (ConnectionError, TimeoutError, RuntimeError):
        capture = capture_live(save_png=False, capture_mode="screen")
        status = {
            "controller_version": None,
            "helper_integrity": "capture-only (no elevation)",
            "click_available": False,
            "target_pid": capture.pid,
            "target_hwnd": capture.hwnd,
            "capture_method": capture.capture_method,
            "capture_width": capture.width,
            "capture_height": capture.height,
            "background_control": False,
        }
    else:
        status["click_available"] = int(status.get("controller_version", 0)) >= 7
        geometry = status.get("geometry", {})
        status["capture_method"] = (
            "MaaFramework(PrintWindow-background)"
            if status.get("background_control")
            else "elevated verified capture"
        )
        status["capture_width"] = int(geometry.get("client_width", 0))
        status["capture_height"] = int(geometry.get("client_height", 0))
    return status


@lru_cache(maxsize=1)
def _card_detector():
    from .card_detector import CardDetector

    return CardDetector()


def resolve_active_reward_plan_context(
    *,
    expected_run_id: str | None = None,
    expected_idol_card_id: str | None = None,
    expected_produce_id: str | None = None,
    run_root: Path | None = None,
    database: Path | None = None,
) -> RewardPlanContext:
    """Resolve reward plan from active run -> idol profile, or fail closed."""

    from .master_db import DEFAULT_DATABASE, get_idol_profile
    from .reward_state import SUPPORTED_REWARD_PLAN_TYPES
    from .run_identity import DEFAULT_RUN_ROOT, load_active_run

    active_run = load_active_run(
        root=DEFAULT_RUN_ROOT if run_root is None else Path(run_root)
    )
    if active_run is None:
        raise ValueError("no active run identity; reward plan is unavailable")
    expected = {
        "run_id": expected_run_id,
        "idol_card_id": expected_idol_card_id,
        "produce_id": expected_produce_id,
    }
    conflicts = tuple(
        f"{field}: expected {value}, active {getattr(active_run, field)}"
        for field, value in expected.items()
        if value is not None and value != getattr(active_run, field)
    )
    if conflicts:
        raise ValueError("active reward run identity conflict: " + "; ".join(conflicts))
    master_database = DEFAULT_DATABASE if database is None else Path(database)
    profile = get_idol_profile(active_run.idol_card_id, master_database)
    if profile is None:
        raise ValueError(
            "Master cannot resolve active-run idol profile: "
            f"{active_run.idol_card_id}"
        )
    if profile.id != active_run.idol_card_id:
        raise ValueError(
            "active-run/Master idol-card conflict: "
            f"{active_run.idol_card_id} != {profile.id}"
        )
    if profile.character_id != active_run.character_id:
        raise ValueError(
            "active-run/Master character conflict: "
            f"{active_run.character_id} != {profile.character_id}"
        )
    if profile.plan_type not in SUPPORTED_REWARD_PLAN_TYPES:
        raise ValueError(
            f"active-run idol Master has unsupported reward plan: {profile.plan_type!r}"
        )
    return RewardPlanContext(
        run_id=active_run.run_id,
        idol_card_id=active_run.idol_card_id,
        character_id=active_run.character_id,
        produce_id=active_run.produce_id,
        plan_type=profile.plan_type,
    )


def _prove_nia_post_reward_detail_authority(
    snapshot: Mapping[str, Any],
    *,
    active_run: Mapping[str, Any],
    run_shadow: Mapping[str, Any],
    strategy_checkpoint: Mapping[str, Any],
    expected_run_id: str,
    expected_idol_card_id: str,
    expected_produce_id: str,
    expected_plan_type: str,
) -> Mapping[str, Any]:
    """Prove one pending post-Receive detail without looking at its pixels.

    The proof intentionally joins three independent durable facts: the last
    strategy action was an exact card ``reward-confirm``; the run shadow then
    recorded that same card as received; and the current outer LocalSave
    equals the shadow's settled state at the next route week.  It authorizes
    only Maa's existing one-shot Click_1 continuation when the normal named
    subpage pass has already failed and the overview reader returned an exact
    route wait.  It never identifies a card or an overview from artwork, text,
    colour, or coordinates.
    """

    identity = {
        "run_id": expected_run_id,
        "idol_card_id": expected_idol_card_id,
        "produce_id": expected_produce_id,
    }
    if any(active_run.get(name) != value for name, value in identity.items()):
        raise ValueError("post-reward active-run identity mismatch")
    if any(
        strategy_checkpoint.get(name) != value
        for name, value in {
            "run_id": expected_run_id,
            "idol": expected_idol_card_id,
            "mode": expected_produce_id,
            "archetype": expected_plan_type,
        }.items()
    ):
        raise ValueError("post-reward strategy identity mismatch")
    if any(
        run_shadow.get(name) != value
        for name, value in {
            "idol_card_id": expected_idol_card_id,
            "produce_id": expected_produce_id,
        }.items()
    ):
        raise ValueError("post-reward shadow identity mismatch")

    week = snapshot.get("week")
    completed_week = snapshot.get("last_completed_week")
    if (
        type(week) is not int
        or week < 1
        or completed_week != week
        or strategy_checkpoint.get("week") != week
        or run_shadow.get("route_week") != week + 1
    ):
        raise ValueError("post-reward route boundary mismatch")

    choices = strategy_checkpoint.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("post-reward strategy has no recent choice")
    recent = choices[-1]
    if not isinstance(recent, Mapping):
        raise ValueError("post-reward recent choice is malformed")
    card_id = recent.get("chosen")
    if not (
        recent.get("week") == week
        and recent.get("page") == "nia-outer-subpage"
        and recent.get("decision_kind") == "reward-confirm"
        and isinstance(card_id, str)
        and card_id.startswith("p_card-")
    ):
        raise ValueError("post-reward recent choice is not a card confirmation")

    checkpoint_values = strategy_checkpoint.get("values")
    recent_state = recent.get("state")
    if not isinstance(checkpoint_values, Mapping) or not isinstance(
        recent_state, Mapping
    ):
        raise ValueError("post-reward pre-receive state is unavailable")
    if dict(recent_state) != dict(checkpoint_values):
        raise ValueError("post-reward recent choice/checkpoint state mismatch")

    observations = run_shadow.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("post-reward shadow has no observation")
    received = observations[-1]
    if not isinstance(received, Mapping) or received.get("kind") != "card_reward":
        raise ValueError("post-reward latest observation is not a card reward")
    metadata = received.get("metadata")
    values = received.get("values")
    deck_delta = received.get("deck_delta")
    if not all(isinstance(value, Mapping) for value in (metadata, values, deck_delta)):
        raise ValueError("post-reward card observation is malformed")
    assert isinstance(metadata, Mapping)
    assert isinstance(values, Mapping)
    assert isinstance(deck_delta, Mapping)
    upgrade = metadata.get("upgrade")
    if (
        metadata.get("card_id") != card_id
        or type(upgrade) is not int
        or upgrade < 0
        or metadata.get("settlement_authority")
        != "outer-local-save-observed-value"
        or dict(deck_delta) != {f"{card_id}@{upgrade}": 1}
    ):
        raise ValueError("post-reward received-card identity mismatch")
    if (
        isinstance(received.get("captured_at"), bool)
        or not isinstance(received.get("captured_at"), int | float)
        or float(received["captured_at"]) <= 0.0
        or not isinstance(received.get("evidence_path"), str)
        or not received["evidence_path"]
    ):
        raise ValueError("post-reward observation evidence is incomplete")

    state_fields = ("stamina", "produce_points", "vocal", "dance", "visual")
    for name in state_fields:
        current = snapshot.get(name)
        settled = run_shadow.get(name)
        if type(current) is not int or current < 0 or settled != current:
            raise ValueError(f"post-reward settled state mismatch: {name}")
    if not values or any(
        name not in state_fields
        or type(value) is not int
        or snapshot.get(name) != value
        for name, value in values.items()
    ):
        raise ValueError("post-reward observed settlement mismatch")

    vote_count = snapshot.get("vote_count")
    if (
        type(vote_count) is not int
        or vote_count < 0
        or checkpoint_values.get("vote_count") != vote_count
    ):
        raise ValueError("post-reward vote authority mismatch")
    return {
        "kind": "durable-post-reward-single-detail",
        "run_id": expected_run_id,
        "week": week,
        "next_route_week": week + 1,
        "card_id": card_id,
        "upgrade": upgrade,
        "observation_captured_at": float(received["captured_at"]),
        "policy": "strategy-confirm+shadow-receipt+settled-localsave-v1",
    }


def resolve_nia_post_reward_detail_authority(
    snapshot: Any,
    *,
    expected_run_id: str | None,
    expected_idol_card_id: str,
    expected_produce_id: str,
    expected_plan_type: str,
) -> Mapping[str, Any] | None:
    """Load and prove the durable post-reward transition, or fail closed."""

    if not isinstance(expected_run_id, str) or not expected_run_id:
        return None
    try:
        from .nia_strategy_journal import nia_strategy_journal_path
        from .run_identity import load_active_run, paths_for
        from .run_shadow import load_run_shadow

        active = load_active_run()
        if active is None:
            return None
        shadow = load_run_shadow(paths_for(active).shadow)
        if shadow is None:
            return None
        checkpoint_path = nia_strategy_journal_path(active.run_id).with_suffix(
            ".telemetry.json"
        )
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(checkpoint, Mapping):
            return None
        snapshot_payload = {
            "log_count": getattr(snapshot, "log_count", None),
            "week": getattr(snapshot, "latest_week_marker", None),
            "last_completed_week": getattr(snapshot, "last_completed_week", None),
            "stamina": getattr(snapshot, "stamina", None),
            "max_stamina": getattr(snapshot, "max_stamina", None),
            "produce_points": getattr(snapshot, "produce_points", None),
            "vocal": getattr(snapshot, "vocal", None),
            "dance": getattr(snapshot, "dance", None),
            "visual": getattr(snapshot, "visual", None),
            "vote_count": getattr(snapshot, "vote_count", None),
        }
        return _prove_nia_post_reward_detail_authority(
            snapshot_payload,
            active_run=active.to_dict(),
            run_shadow=shadow.to_dict(),
            strategy_checkpoint=checkpoint,
            expected_run_id=expected_run_id,
            expected_idol_card_id=expected_idol_card_id,
            expected_produce_id=expected_produce_id,
            expected_plan_type=expected_plan_type,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _rank_live_reward_offers(
    context: RewardPlanContext,
    offers: Sequence[Any],
) -> tuple[Any, ...]:
    """Apply deck-aware Plan2 strategy, with the old ranking as fallback."""

    from .plan2_reward_rollout import rank_plan2_reward_offers_strong
    from .reward_state import rank_reward_offers

    if context.plan_type != "ProducePlanType_Plan2":
        return rank_reward_offers(offers)
    try:
        from .master_db import get_idol_profile
        from .leaderboard_card_prior import (
            try_load_default_plan2_leaderboard_card_prior,
        )
        from .run_identity import load_active_run, paths_for
        from .run_shadow import load_run_shadow

        profile = get_idol_profile(context.idol_card_id)
        active = load_active_run()
        if profile is None or active is None or active.run_id != context.run_id:
            return rank_reward_offers(offers)
        shadow = load_run_shadow(paths_for(active).shadow)
        return rank_plan2_reward_offers_strong(
            offers,
            profile.exam_effect_type,
            idol_card_id=context.idol_card_id,
            produce_id=context.produce_id,
            deck_counts=None if shadow is None else shadow.deck,
            weeks_remaining=None if shadow is None else shadow.weeks_remaining,
            stamina=None if shadow is None else shadow.stamina,
            max_stamina=None if shadow is None else shadow.max_stamina,
            leaderboard_prior=try_load_default_plan2_leaderboard_card_prior(),
        ).ranked_offers
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        # Strategy data must never turn a recognized reward page into a UI
        # blocker.  The previous deterministic Master ranking is still safe.
        return rank_reward_offers(offers)


def ensure_initial_regular_active_run(
    *,
    idol_card_id: str,
    produce_id: str,
    snapshot: Any,
    overview_output: Mapping[str, Any] | None = None,
    observed_character_id: str | None = None,
    selected_idol_confirmed: bool = False,
    expected_run_id: str | None = None,
    run_root: Path | None = None,
    database: Path | None = None,
) -> Mapping[str, Any]:
    """Create or resume the run identity needed by unattended Produce.

    Identity comes from the selected idol validated against Master, the active
    outer LocalSave lifecycle, and either an exact overview route or a matching
    ExamSave character.  The shadow is initialized only from current LocalSave
    values, with a trusted overview allowed to fill fields not logged yet.
    Existing non-null shadow values are never overwritten here.
    """

    from .master_db import DEFAULT_DATABASE, get_idol_profile
    from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot
    from .route_calendar import load_route_calendar
    from .run_identity import (
        DEFAULT_RUN_ROOT,
        create_run,
        load_active_run,
        paths_for,
    )
    from .run_shadow import RunShadowState, load_run_shadow, save_run_shadow

    if not isinstance(idol_card_id, str) or not idol_card_id.strip():
        raise ValueError("idol_card_id must be non-empty text")
    if not isinstance(produce_id, str) or not produce_id.strip():
        raise ValueError("produce_id must be non-empty text")
    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if overview_output is not None and not isinstance(overview_output, Mapping):
        raise TypeError("overview_output must be a mapping or None")
    if observed_character_id is not None and (
        not isinstance(observed_character_id, str) or not observed_character_id.strip()
    ):
        raise ValueError("observed_character_id must be non-empty text or None")
    if type(selected_idol_confirmed) is not bool:
        raise TypeError("selected_idol_confirmed must be bool")
    if expected_run_id is not None and (
        not isinstance(expected_run_id, str) or not expected_run_id.strip()
    ):
        raise ValueError("expected_run_id must be non-empty text or None")

    is_nia = produce_id in {"produce-004", "produce-005"}
    allows_opening_partial = produce_id in {
        "produce-002",
        "produce-003",
        "produce-004",
        "produce-005",
    }
    lifecycle = snapshot.lifecycle
    if lifecycle is None or not lifecycle.is_in_progress:
        raise ValueError("outer LocalSave does not prove an active Produce run")

    master_database = DEFAULT_DATABASE if database is None else Path(database)
    profile = get_idol_profile(idol_card_id, master_database)
    if profile is None:
        raise ValueError(f"idol_card_id is not present in Master: {idol_card_id}")
    if profile.id != idol_card_id:
        raise ValueError("Master returned a different idol profile")
    if is_nia and getattr(profile, "plan_type", None) not in {
        "ProducePlanType_Plan1",
        "ProducePlanType_Plan2",
        "ProducePlanType_Plan3",
    }:
        raise ValueError(
            "N.I.A. requires a supported Plan1, Plan2, or Plan3 idol card: "
            f"{idol_card_id} has {getattr(profile, 'plan_type', None)!r}"
        )
    character_id = profile.character_id
    if observed_character_id is not None and observed_character_id != character_id:
        raise ValueError(
            "ExamSave character differs from selected idol: "
            f"{observed_character_id} != {character_id}"
        )
    expected_produce_type = 2 if is_nia else 1
    if snapshot.produce_type not in {None, expected_produce_type}:
        raise ValueError(
            "outer LocalSave Produce family differs from the selected mode: "
            f"selected={produce_id}, "
            f"actual={snapshot.produce_type_name or snapshot.produce_type}"
        )

    overview_state: Mapping[str, Any] = {}
    overview_route: Mapping[str, Any] | None = None
    if overview_output is not None:
        raw_state = overview_output.get("state")
        raw_route = overview_output.get("route")
        if not isinstance(raw_state, Mapping) or not isinstance(raw_route, Mapping):
            raise ValueError("trusted overview state and route are required")
        if raw_route.get("produce_id") != produce_id:
            raise ValueError("overview route differs from selected produce_id")
        if raw_route.get("character_id") != character_id:
            raise ValueError("overview route differs from selected idol character")
        # N.I.A. and Initial Pro/Master weekly choices are server-provided.
        # Their currently visible action identity is resolved by the reader;
        # only Initial Regular has a preserved exact FKTN week table here.
        if (
            produce_id == "produce-001"
            and raw_route.get("route_actions_exact") is not True
        ):
            raise ValueError("overview route actions are not exact")
        overview_state = raw_state
        overview_route = raw_route

    calendar = load_route_calendar(produce_id, character_id=character_id)

    def exact_int(value: object, label: str, *, minimum: int = 0) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"{label} must be an integer >= {minimum}")
        return value

    def effective_outer_week() -> int | None:
        latest = snapshot.latest_week_marker
        completed = snapshot.last_completed_week
        if latest is None:
            return None
        latest = exact_int(latest, "outer latest_week_marker", minimum=1)
        if completed is not None:
            completed = exact_int(completed, "outer last_completed_week", minimum=1)
            if completed > latest:
                raise ValueError("outer completed week is ahead of its week marker")
        if completed == latest:
            # Once the Final week is complete, post-live/result pages still
            # retain the active Produce save.  There is no week N+1; keep the
            # route anchored to Final until the save disappears after finish.
            return latest if latest == calendar.total_weeks else latest + 1
        return latest

    root = DEFAULT_RUN_ROOT if run_root is None else Path(run_root)
    active = load_active_run(root=root)
    if expected_run_id is not None:
        if active is None or active.run_id != expected_run_id:
            raise ValueError("expected run_id is not the active run")
    expected_identity = (idol_card_id, character_id, produce_id)
    active_matches = active is not None and (
        active.idol_card_id,
        active.character_id,
        active.produce_id,
    ) == expected_identity
    existing_shadow = None
    if active_matches:
        assert active is not None
        existing_shadow = load_run_shadow(paths_for(active, root=root).shadow)
        if existing_shadow is not None:
            if (
                existing_shadow.idol_card_id,
                existing_shadow.character_id,
                existing_shadow.produce_id,
            ) != expected_identity:
                raise ValueError("active run shadow identity differs from its manifest")
            outer_week = effective_outer_week()
            shadow_week = existing_shadow.route_week
            if (
                outer_week is not None
                and shadow_week is not None
                and outer_week < shadow_week
            ):
                if expected_run_id is not None:
                    raise ValueError(
                        "outer LocalSave route restarted before expected run_id"
                    )
                # Same mode/idol is a new Produce session once the authoritative
                # outer route moves backwards.  Do not inherit the previous
                # run's week, stamina, parameters, inventory, or deck shadow.
                active_matches = False
                existing_shadow = None
            if active_matches and not is_nia and all(
                getattr(existing_shadow, field) is not None
                for field in (
                    "route_week",
                    "weeks_remaining",
                    "stamina",
                    "vocal",
                    "dance",
                    "visual",
                )
            ):
                return {
                    "run_id": active.run_id,
                    "created": False,
                    "shadow_initialized": False,
                    "shadow": existing_shadow.to_dict(),
                }
    elif expected_run_id is not None:
        raise ValueError("active run identity differs from the selected Initial Regular run")

    if overview_route is not None:
        current_week = exact_int(
            overview_route.get("current_week"), "overview current_week", minimum=1
        )
        total_weeks = exact_int(
            overview_route.get("total_weeks"), "overview total_weeks", minimum=1
        )
        if total_weeks != calendar.total_weeks:
            raise ValueError("overview route length differs from the route calendar")
        local_week = effective_outer_week()
        if local_week is not None and local_week != current_week:
            raise ValueError("overview week differs from outer LocalSave")
    else:
        if (
            observed_character_id is None
            and not is_nia
            and not selected_idol_confirmed
            and not active_matches
        ):
            raise ValueError(
                "trusted overview or ExamSave character evidence is required"
            )
        if snapshot.produce_type is None:
            raise ValueError(
                "outer LocalSave has no Produce identity; trusted overview is required"
            )
        current_week = effective_outer_week()
        if current_week is None:
            raise ValueError("outer LocalSave has no current week evidence")
        total_weeks = calendar.total_weeks
    if current_week > total_weeks:
        raise ValueError("current route week is outside the Initial Regular calendar")

    def current_value(field: str, *, required: bool) -> int | None:
        local = getattr(snapshot, field)
        value = local if local is not None else overview_state.get(field)
        if value is None:
            if required:
                raise ValueError(f"trusted {field} evidence is unavailable")
            return None
        return exact_int(value, field)

    trusted = {
        "route_week": current_week,
        "weeks_remaining": total_weeks - current_week,
        # A freshly-created Pro/Master/N.I.A. Produce save can exist while its
        # opening story/item notifications are still on screen.  At that
        # point the play log proves identity and week but has not serialized
        # the first stamina/parameter snapshot.  Keep those shadow fields
        # unknown until the real overview/update writes them.  Initial Regular
        # alone retains its strict complete bootstrap requirement.
        "stamina": current_value("stamina", required=not allows_opening_partial),
        "max_stamina": current_value("max_stamina", required=False),
        "produce_points": current_value("produce_points", required=False),
        "vocal": current_value("vocal", required=not allows_opening_partial),
        "dance": current_value("dance", required=not allows_opening_partial),
        "visual": current_value("visual", required=not allows_opening_partial),
    }

    created = False
    if not active_matches:
        active = create_run(
            idol_card_id=idol_card_id,
            character_id=character_id,
            produce_id=produce_id,
            evidence={
                "source": "initial-regular-production-bootstrap",
                "identity_evidence": (
                    "overview-route+outer-local-save"
                    if overview_output is not None
                    else (
                        "exam-character+outer-local-save"
                        if observed_character_id is not None
                        else (
                            "maa-ocr-selected-idol+outer-local-save"
                            if selected_idol_confirmed
                            else "selected-idol+nia-family-outer-local-save"
                        )
                    )
                ),
                "outer_save_data_version": snapshot.save_data_version,
                "outer_log_count": snapshot.log_count,
                "route_week": current_week,
            },
            root=root,
            database=master_database,
        )
        existing_shadow = None
        created = True

    assert active is not None
    run_paths = paths_for(active, root=root)
    shadow = existing_shadow or RunShadowState(
        produce_id=produce_id,
        character_id=character_id,
        idol_card_id=idol_card_id,
    )
    updates = {
        field: value
        for field, value in trusted.items()
        if value is not None
        and (
            getattr(shadow, field) != value
            if is_nia
            else getattr(shadow, field) is None
        )
    }
    updated = replace(shadow, **updates) if updates else shadow
    if existing_shadow is None or updates:
        save_run_shadow(updated, run_paths.shadow)
    return {
        "run_id": active.run_id,
        "created": created,
        "shadow_initialized": existing_shadow is None or bool(updates),
        "shadow": updated.to_dict(),
    }


def read_live_initial_opening_plan_choice(
    snapshot: Any,
    *,
    idol_card_id: str,
) -> Mapping[str, Any]:
    """Select the localized opening-plan row by Maa Paddle OCR text.

    Initial Pro/Master can show a two-row opening dialogue after LocalSave is
    created but before the first overview is serialized.  Prefer the localized
    semantic row when it is present.  Some idol cards instead show two ordinary
    story replies without the idol name; when both Maa OCR rows are stable,
    reuse MaaGakumasu's existing first-choice policy.  The returned action uses
    the ordinary Maa/LocalSave subpage executor.
    """

    from .controller_client import send_command
    from .live_actions import SuggestedClick
    from .nia_idol_catalog import load_nia_idol_catalog
    from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if snapshot.lifecycle is None or not snapshot.lifecycle.is_in_progress:
        raise ValueError("opening plan choice requires active Produce LocalSave")
    if snapshot.last_completed_week is not None or snapshot.log_count > 2:
        raise ValueError("opening plan choice is only valid before the first week")
    entry = load_nia_idol_catalog().require(idol_card_id)

    def normalized(value: str) -> str:
        return "".join(
            character
            for character in unicodedata.normalize("NFKC", value).casefold()
            if character.isalnum()
        )

    names = tuple(
        dict.fromkeys(
            normalized(value)
            for value in (
                entry.idol_name,
                entry.idol_name_zh_tw,
                *entry.idol_name_aliases,
            )
            if value
        )
    )
    capture = dict(send_command("capture_screen_once", timeout=15.0))
    path_value = capture.get("png_path")
    if not isinstance(path_value, str) or not Path(path_value).is_file():
        raise ValueError("opening plan choice has no Maa capture")
    recognizer = _live_text_recognizer()
    rows: list[tuple[tuple[int, int, int, int], Any, str]] = []
    readable_rows: list[tuple[tuple[int, int, int, int], Any, str]] = []
    start_tokens = tuple(
        normalized(value)
        for value in (
            "強化計畫開始",
            "強化計劃開始",
            "強化計画開始",
        )
    )
    for box in INITIAL_OPENING_CHOICE_BOXES:
        result = recognizer.recognize_path(Path(path_value), box)
        text = normalized(result.text)
        if result.confidence >= 0.80 and text:
            readable_rows.append((box, result, text))
        if (
            result.confidence >= 0.80
            and any(name and name in text for name in names)
            and any(token in text for token in start_tokens)
        ):
            rows.append((box, result, text))
    policy = "localized-idol-plan-start"
    if len(rows) == 1:
        selected_row = rows[0]
    elif not rows and len(readable_rows) == len(INITIAL_OPENING_CHOICE_BOXES):
        selected_row = readable_rows[0]
        policy = "maa-first-choice-stable-two-row-fallback"
    else:
        raise ValueError(
            "opening plan choice did not resolve a localized row or two stable rows"
        )
    box, result, _text = selected_row
    left, top, right, bottom = box
    action = SuggestedClick(
        label="nia-subpage:opening-plan-choice:start",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=path_value,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        click_count=1,
    )
    return {
        "capture": capture,
        "kind": "opening-plan-choice",
        "target": policy,
        "action": action.to_dict(),
        "evidence": {
            "authority": "maa-paddle-ocr-localized-opening-choice",
            "policy": policy,
            "text": result.text,
            "confidence": result.confidence,
            "candidate_rows": [list(value) for value in INITIAL_OPENING_CHOICE_BOXES],
        },
        "outer_authority": {
            "kind": "produce-local-save-before-maa-subpage-input",
            "log_count": snapshot.log_count,
            "week": snapshot.latest_week_marker,
            "last_completed_week": snapshot.last_completed_week,
            "stamina": snapshot.stamina,
            "max_stamina": snapshot.max_stamina,
            "produce_points": snapshot.produce_points,
            "vocal": snapshot.vocal,
            "dance": snapshot.dance,
            "visual": snapshot.visual,
            "vote_count": snapshot.vote_count,
        },
    }


def view_card_detections(report: Any, capture: Mapping[str, Any]) -> LiveView:
    """Expose card boxes honestly; identity recognition is a later stage."""

    detections = tuple(report.detections)
    cards = tuple(
        LiveCardRow(
            f"未知卡牌 #{index + 1}",
            0,
            f"{item.label}｜{item.confidence:.0%}｜({item.x},{item.y},{item.width},{item.height})",
            "未判定",
        )
        for index, item in enumerate(detections)
    )
    if detections:
        confidence = max(item.confidence for item in detections)
        recommendation = "已找到卡牌區域；等待卡名 OCR／圖像比對"
        cards_label = f"目前畫面：偵測到 {len(detections)} 張卡牌候選"
    else:
        confidence = 0.0
        recommendation = "目前不像出牌畫面，或卡牌尚未完全出現"
        cards_label = "目前畫面：未偵測到卡牌"
    return LiveView(
        state=None,
        cards=cards,
        cards_label=cards_label,
        recommendation=recommendation,
        confidence=f"YOLO 區域信心 {confidence:.0%}",
        reasons=(
            f"Maa cards.onnx｜{report.provider}｜{report.elapsed_ms:.1f} ms。",
            "模型只判斷卡牌區域與視覺狀態，不把它假裝成卡牌 ID。",
            "沒有讀取遊戲程序記憶體。",
        ),
        capture={**dict(capture), "detection_report": report.to_dict()},
    )


@lru_cache(maxsize=1)
def _live_card_art() -> Mapping[str, Path]:
    """Extract only Common/Logic art plus the current idol's variants."""

    from .octo_assets import OctoAssetIndex, card_asset_name, card_suffix_from_asset

    catalog = _shop_card_catalog()
    suffixes = set()
    for entry in catalog.entries:
        if entry.upgrade != 0 or entry.plan_type not in {
            "ProducePlanType_Common",
            "ProducePlanType_Plan2",
        }:
            continue
        suffixes.add(card_suffix_from_asset(card_asset_name(entry.card_id)))
    return OctoAssetIndex.load().ensure_card_art(
        character_id="ssmk",
        suffixes=suffixes,
    )


@lru_cache(maxsize=3)
def _live_reward_card_art(plan_type: str) -> Mapping[str, Path]:
    """Extract only ordinary Common + active-plan reward artwork."""

    from .octo_assets import OctoAssetIndex, card_asset_name, card_suffix_from_asset
    from .reward_state import regular_reward_candidate_entries

    entries = regular_reward_candidate_entries(
        catalog=_shop_card_catalog(),
        plan_type=plan_type,
    )
    suffixes = {
        card_suffix_from_asset(card_asset_name(entry.card_id))
        for entry in entries
    }
    return OctoAssetIndex.load().ensure_card_art(
        include_generic=True,
        categories=("act", "men"),
        suffixes=suffixes,
    )


@lru_cache(maxsize=1)
def _deck_card_art_matcher():
    from .card_art_matcher import PreparedCardArtMatcher

    return PreparedCardArtMatcher.build(_live_card_art(), size=64)


@lru_cache(maxsize=1)
def _live_text_recognizer():
    from .text_recognizer import PaddleLineRecognizer

    return PaddleLineRecognizer()


def _card_name_crop(image: Any, detection: Any):
    """Crop the printed card-name strip using ratios stable across scaling."""

    left = detection.x + round(detection.width * 0.02)
    right = detection.x + detection.width - round(detection.width * 0.02)
    top = detection.y + round(detection.height * 0.78)
    bottom = detection.y + round(detection.height * 0.98)
    return image.crop((left, top, right, bottom))


def _card_name_recognitions(image: Any, detection: Any, recognizer: Any):
    """Read both the normal strip and a high-contrast upgrade-marker strip."""

    from PIL import ImageOps

    crop = _card_name_crop(image, detection)
    normal = recognizer.recognize(crop)
    grayscale = ImageOps.autocontrast(crop.convert("L"))
    thresholded = grayscale.point(lambda value: 255 if value > 160 else 0)
    contrasted = recognizer.recognize(thresholded)
    return normal, contrasted


def _card_displayed_stamina_cost(
    image: Any,
    detection: Any,
    recognizer: Any,
    *,
    minimum_confidence: float = 0.80,
) -> tuple[int | None, float]:
    """Read the effective cost printed on the lower-right card badge."""

    from PIL import ImageOps

    candidates: list[tuple[float, int]] = []
    for left_ratio, top_ratio, right_ratio, bottom_ratio in (
        (0.78, 0.72, 0.97, 0.87),
        (0.76, 0.70, 0.98, 0.89),
        (0.72, 0.68, 0.99, 0.90),
    ):
        left = detection.x + round(detection.width * left_ratio)
        right = detection.x + round(detection.width * right_ratio)
        top = detection.y + round(detection.height * top_ratio)
        bottom = detection.y + round(detection.height * bottom_ratio)
        crop = image.crop((left, top, right, bottom))
        variants = (
            crop,
            ImageOps.autocontrast(crop.convert("L")),
            ImageOps.invert(ImageOps.autocontrast(crop.convert("L"))),
        )
        for variant in variants:
            enlarged = variant.resize((variant.width * 3, variant.height * 3))
            result = recognizer.recognize(enlarged)
            match = re.fullmatch(r"\s*[-−]?\s*(\d+)\s*", result.text)
            if match is not None:
                candidates.append((float(result.confidence), int(match.group(1))))
    if not candidates:
        return None, 0.0
    confidence, value = max(candidates)
    if confidence < minimum_confidence:
        return None, confidence
    return value, confidence


def _best_distinct_name_matches(catalog: Any, text: str) -> tuple[Any, Any | None]:
    """Ignore upgrade variants when measuring name ambiguity."""

    distinct = []
    seen_ids: set[str] = set()
    for match in catalog.match(text, limit=12):
        if match.card_id in seen_ids:
            continue
        seen_ids.add(match.card_id)
        distinct.append(match)
        if len(distinct) == 2:
            break
    if not distinct:
        raise ValueError("card-name OCR did not produce a catalog candidate")
    return distinct[0], distinct[1] if len(distinct) > 1 else None


def identify_card_detections(
    report: Any,
    image_path: Path,
    capture: Mapping[str, Any],
    *,
    minimum_score: float = 0.58,
    minimum_margin: float = 0.045,
    minimum_ocr_confidence: float = 0.85,
    minimum_name_similarity: float = 0.90,
    minimum_name_margin: float = 0.08,
) -> LiveView:
    """Resolve card names with local OCR, using static art only as fallback."""

    from PIL import Image

    from .card_art_matcher import match_card_art

    detections = _deduplicate_card_detections(
        tuple(
            item
            for item in report.detections
            if item.label in {"cards", "recommend"}
        )
    )
    if not detections:
        return view_card_detections(report, capture)
    art = _live_card_art()
    catalog = _shop_card_catalog()
    recognizer = _live_text_recognizer()
    rows: list[LiveCardRow] = []
    match_notes: list[str] = []
    identified = 0
    with Image.open(image_path.resolve()) as image:
        image.load()
        for index, item in enumerate(detections, 1):
            observed_cost, cost_confidence = _card_displayed_stamina_cost(
                image, item, recognizer
            )
            name_results = _card_name_recognitions(image, item, recognizer)
            name_result = max(name_results, key=lambda result: result.confidence)
            name_match = None
            name_margin = 0.0
            accepted_name_results = []
            for candidate_result in name_results:
                if not candidate_result.text:
                    continue
                best_name, runner_up_name = _best_distinct_name_matches(
                    catalog, candidate_result.text
                )
                candidate_margin = best_name.similarity - (
                    runner_up_name.similarity if runner_up_name else 0.0
                )
                if (
                    candidate_result.confidence >= minimum_ocr_confidence
                    and best_name.similarity >= minimum_name_similarity
                    and candidate_margin >= minimum_name_margin
                ):
                    accepted_name_results.append(
                        (
                            best_name.similarity,
                            candidate_result.confidence,
                            candidate_result.text.count("+"),
                            best_name,
                            candidate_result,
                            candidate_margin,
                        )
                    )
            if accepted_name_results:
                strongest = max(
                    accepted_name_results,
                    key=lambda candidate: candidate[:2],
                )
                compatible = [
                    candidate
                    for candidate in accepted_name_results
                    if candidate[3].card_id == strongest[3].card_id
                    and candidate[0] >= strongest[0] - 0.01
                    and candidate[1] >= strongest[1] - 0.05
                ]
                (
                    _similarity,
                    _ocr_confidence,
                    _upgrade_markers,
                    name_match,
                    name_result,
                    name_margin,
                ) = max(
                    compatible,
                    key=lambda candidate: (
                        candidate[2],
                        candidate[0],
                        candidate[1],
                    ),
                )
            rendered = image.crop(
                (item.x, item.y, item.x + item.width, item.y + item.height)
            )
            match = match_card_art(rendered, art)
            if name_match is not None:
                rows.append(
                    LiveCardRow(
                        name_match.card_id,
                        name_match.upgrade,
                        f"{name_match.display_name}｜OCR {name_result.confidence:.0%}／"
                        f"名稱 {name_match.similarity:.0%}",
                        "未判定",
                    )
                )
                identified += 1
                match_notes.append(
                    f"#{index} {name_result.text} → {name_match.card_id} "
                    f"（OCR {name_result.confidence:.0%}；名稱領先 {name_margin:.0%}）"
                )
            elif match.score >= minimum_score and match.margin >= minimum_margin:
                entry = catalog.resolve_asset(match.asset_name)
                rows.append(
                    LiveCardRow(
                        entry.card_id,
                        None,
                        f"{entry.display_name}｜卡圖 {match.score:.0%}／領先 {match.margin:.0%}",
                        "未判定",
                    )
                )
                identified += 1
                match_notes.append(
                    f"#{index} {entry.display_name} → {entry.card_id}"
                )
            else:
                rows.append(
                    LiveCardRow(
                        f"未知卡牌 #{index}",
                        None,
                        f"最佳 {match.asset_name}｜卡圖 {match.score:.0%}／領先 {match.margin:.0%}",
                        "未判定",
                    )
                )
                match_notes.append(
                    f"#{index} OCR {name_result.text!r} 未達門檻；卡圖也未達安全門檻"
                    f"（{match.score:.0%}／{match.margin:.0%}）"
                )
            rows[-1] = replace(
                rows[-1],
                observed_stamina_cost=observed_cost,
                cost_confidence=cost_confidence,
            )
    if identified == len(rows):
        recommendation = "卡牌身分已對應 Master；等待回合數值與求解器排序"
    else:
        recommendation = "部分卡牌辨識信心不足；不會用猜測結果出牌"
    return LiveView(
        state=None,
        cards=tuple(rows),
        cards_label=f"目前畫面：辨識 {identified}/{len(rows)} 張卡牌（強化未判定）",
        recommendation=recommendation,
        confidence="名稱 OCR ≥85%、相似度 ≥90%、異卡領先 ≥8%；卡圖為備援",
        reasons=(
            *match_notes,
            f"Maa cards.onnx｜{report.provider}｜{report.elapsed_ms:.1f} ms。",
            "OCR 模型、Master 與備援卡圖都來自本機；沒有讀取遊戲程序記憶體。",
        ),
        capture={**dict(capture), "detection_report": report.to_dict()},
    )


def detect_live_cards() -> LiveView:
    """Capture, locate cards, and resolve safe matches against static art."""

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("controller 擷取沒有 PNG 路徑")
    report = _card_detector().detect_path(Path(png_path))
    return identify_card_detections(report, Path(png_path), capture)


def prepare_live_deck_panel_open() -> Mapping[str, Any]:
    """Bind one safe click to the in-performance bag/deck button.

    The same lower-right location is a P-item bag on the weekly overview, so
    the click is offered only when Maa's model sees a stable hand along the
    bottom of a lesson/audition frame.
    """

    from .live_actions import SuggestedClick

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("controller 擷取沒有 PNG 路徑")
    path = Path(png_path)
    # Overview action tiles are card-shaped and can legitimately trigger the
    # generic YOLO model.  Require a lesson/audition numeric HUD as a second,
    # independent screen-type check before exposing the bag click.
    performance_frame = False
    try:
        from .lesson_screen import read_lesson_screen_path

        read_lesson_screen_path(path, _live_text_recognizer())
        performance_frame = True
    except ValueError:
        try:
            from .exam_screen import read_exam_screen_path

            read_exam_screen_path(path, _live_text_recognizer())
            performance_frame = True
        except ValueError:
            pass
    if not performance_frame:
        raise ValueError("目前不是通過 HUD 驗證的課程／演出出牌畫面")
    report = _card_detector().detect_path(path)
    cards = tuple(
        item
        for item in report.detections
        if item.label == "cards" and item.confidence >= 0.75
    )
    if not cards:
        raise ValueError("目前不是可同步牌組的課程／演出出牌畫面")
    median_y = sorted(item.y for item in cards)[len(cards) // 2]
    if median_y < report.image_height * 0.62:
        raise ValueError("目前不像底部手牌畫面；不會盲點右下角包包")
    action = SuggestedClick(
        label="打開牌組（山札／捨札／除外）",
        canonical_x=DECK_PANEL_BUTTON_CENTER[0],
        canonical_y=DECK_PANEL_BUTTON_CENTER[1],
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=DECK_PANEL_BUTTON_BOX,
        click_count=1,
    )
    return {
        "capture": dict(capture),
        "hand_detection_count": len(cards),
        "action": action.to_dict(),
    }


def _analyze_deck_panel_capture(capture: Mapping[str, Any]) -> Mapping[str, Any]:
    from PIL import Image

    from .deck_panel import identify_deck_panel_cards, read_deck_panel_layout

    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("controller 擷取沒有 PNG 路徑")
    path = Path(png_path)
    report = _card_detector().detect_path(path)
    with Image.open(path.resolve()) as image:
        image.load()
        layout = read_deck_panel_layout(
            image,
            report.detections,
            _live_text_recognizer(),
        )
        identities = identify_deck_panel_cards(
            image,
            layout,
            _deck_card_art_matcher(),
            _shop_card_catalog(),
        )
    recognized = sum(identity.accepted for identity in identities)
    return {
        "capture": dict(capture),
        "layout": layout.to_dict(),
        "cards": [identity.to_dict() for identity in identities],
        "recognized_count": recognized,
        "unresolved_identity_count": len(identities) - recognized,
        # Upgrade markers remain deliberately unresolved until calibrated on
        # the current localized client, so this is not yet a DeckSnapshot.
        "unresolved_upgrade_count": recognized,
        "detection_report": report.to_dict(),
    }


def read_live_deck_panel() -> Mapping[str, Any]:
    """Read pile counts and every visible compact card slot from an open panel."""

    return _analyze_deck_panel_capture(capture_once())


def _read_owned_deck_page_capture(capture: Mapping[str, Any]) -> Any:
    """Read one visible page of the localized full-card panel."""

    from PIL import Image

    from .owned_deck import read_owned_deck_page

    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    path = Path(png_path)
    report = _card_detector().detect_path(path)
    with Image.open(path.resolve()) as image:
        image.load()
        return read_owned_deck_page(
            image,
            tuple(report.detections),
            _live_text_recognizer(),
            _deck_card_art_matcher(),
            _shop_card_catalog(),
            _live_card_art(),
            expected_visible_capacity=None,
        )


def read_live_owned_deck_page() -> Mapping[str, Any]:
    """Read one currently visible page from 持有的技能卡."""

    capture = capture_once()
    page = _read_owned_deck_page_capture(capture)
    return {"capture": dict(capture), "page": page.to_dict()}


def _owned_deck_scrollbar_thumb(png_path: str) -> tuple[int, int]:
    """Locate the long pale scrollbar thumb on the panel's right edge."""

    import numpy as np
    from PIL import Image

    with Image.open(Path(png_path).resolve()) as image:
        rgb = np.asarray(image.convert("RGB"))
    column = rgb[380:1010, 703, :].astype(np.int16)
    mask = (
        (np.max(column, axis=1) - np.min(column, axis=1) < 30)
        & (np.mean(column, axis=1) > 100)
    )
    positions = tuple(int(value) + 380 for value in np.flatnonzero(mask))
    runs: list[tuple[int, int]] = []
    if positions:
        start = previous = positions[0]
        for value in positions[1:]:
            if value > previous + 1:
                runs.append((start, previous))
                start = value
            previous = value
        runs.append((start, previous))
    eligible = tuple(run for run in runs if run[1] - run[0] >= 100)
    if not eligible:
        raise ValueError("無法定位持有技能卡面板的捲軸")
    return max(eligible, key=lambda run: run[1] - run[0])


def _commit_scanned_owned_deck_snapshot(
    *,
    identity: Any,
    snapshot: Any,
    evidence_path: str,
    captured_at: float,
) -> Any:
    """Persist every new scan in the run-scoped wrapper format.

    This small boundary makes it impossible for the scanner to fall back to
    the older bare ``DeckSnapshot`` writer without a regression test failing.
    """

    from .run_deck_snapshot import commit_run_deck_snapshot

    return commit_run_deck_snapshot(
        identity=identity,
        snapshot=snapshot,
        evidence_path=evidence_path,
        captured_at=datetime.fromtimestamp(float(captured_at), tz=timezone.utc),
    )


def scan_live_owned_deck(
    *,
    step_type: str,
    stage_number: int,
    source: str,
    max_pages: int = 8,
) -> Mapping[str, Any]:
    """Swipe, merge, and bind the visible deck to an explicit audition stage."""

    from .controller_client import send_command
    from .deck_snapshot import (
        DeckCardStack,
        build_deck_snapshot,
    )
    from .owned_deck import merge_owned_deck_pages
    from .run_shadow import (
        apply_authoritative_deck_baseline,
        load_run_shadow,
        save_run_shadow,
    )
    from .master_db import get_idol_profile
    from .run_identity import load_active_run, paths_for

    from .audition_rules import FINAL, MID1, MID2

    if step_type not in {MID1, MID2, FINAL}:
        raise ValueError("deck scan requires a supported explicit step_type")
    if not isinstance(stage_number, int) or isinstance(stage_number, bool) or stage_number < 1:
        raise ValueError("deck scan requires a positive explicit stage_number")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("deck scan requires a non-empty source")
    if not 1 <= max_pages <= 12:
        raise ValueError("max_pages 必須介於 1 與 12")
    initial_capture = capture_once()
    thumb_top, thumb_bottom = _owned_deck_scrollbar_thumb(
        str(initial_capture["png_path"])
    )
    if thumb_top > 430:
        thumb_center = (thumb_top + thumb_bottom) // 2
        send_command(
            "send_input_swipe_once",
            x1=704,
            y1=thumb_center,
            x2=704,
            y2=537,
            duration_ms=600,
        )
        time.sleep(1.0)
    pages = []
    evidence: list[str] = []
    last_keys: tuple[tuple[str, int] | None, ...] | None = None
    final_capture: Mapping[str, Any] | None = None
    inventory = None
    for page_index in range(max_pages):
        capture = capture_once()
        page = _read_owned_deck_page_capture(capture)
        accepted_count = sum(card.accepted for card in page.cards)
        if accepted_count < 4:
            raise ValueError(f"牌庫第 {page_index + 1} 頁可確認卡牌不足 4 張")
        clean_page = replace(page, unresolved_slots=0)
        keys = tuple(card.key for card in clean_page.cards)
        if keys == last_keys:
            raise ValueError("牌庫 swipe 後畫面沒有移動")
        pages.append(clean_page)
        evidence.append(str(capture["png_path"]))
        final_capture = capture
        inventory = merge_owned_deck_pages(pages)
        if len(pages) > 1 and inventory.overlap_lengths[-1] == 0:
            raise ValueError("相鄰牌庫頁面沒有可驗證的重疊卡序")
        if inventory.complete:
            break
        last_keys = keys
        thumb_top, thumb_bottom = _owned_deck_scrollbar_thumb(
            str(capture["png_path"])
        )
        if thumb_bottom >= 985:
            break
        thumb_center = (thumb_top + thumb_bottom) // 2
        # A 150 px thumb movement advances this compact panel by almost two
        # complete card rows at 720x1280.  That can leave only a clipped row
        # between captures, which is not enough evidence for the ordered
        # overlap check.  Move half as far so adjacent pages retain at least
        # one fully rendered row in common.
        next_center = min(858, thumb_center + 75)
        send_command(
            "send_input_swipe_once",
            x1=704,
            y1=thumb_center,
            x2=704,
            y2=next_center,
            duration_ms=600,
        )
        time.sleep(1.0)
    if inventory is None or final_capture is None or not inventory.complete:
        observed = 0 if inventory is None else len(inventory.cards)
        total = 0 if inventory is None else inventory.total_count
        raise ValueError(f"牌庫逐頁掃描未完成：已合併 {observed}/{total} 張")

    stacks = (
        DeckCardStack(
            card_id=str(card.card_id),
            upgrade=int(card.upgrade),
            confidence=min(0.99, 0.80 + min(0.19, card.art_margin)),
        )
        for card in inventory.cards
    )
    active_run = load_active_run()
    if active_run is None:
        raise ValueError("no active run identity; refuse to bind a visible deck")
    run_paths = paths_for(active_run)
    shadow = load_run_shadow(run_paths.shadow)
    if shadow is None:
        raise ValueError("找不到目前培育的 run shadow")
    if (
        shadow.produce_id != active_run.produce_id
        or shadow.character_id != active_run.character_id
        or shadow.idol_card_id != active_run.idol_card_id
    ):
        raise ValueError("active run manifest and run shadow identity differ")
    idol_profile = get_idol_profile(shadow.idol_card_id)
    if idol_profile is None or not idol_profile.produce_card_id:
        raise ValueError(
            "Master cannot resolve the run idol's required unique card: "
            f"{shadow.idol_card_id}"
        )
    snapshot = build_deck_snapshot(
        produce_id=shadow.produce_id,
        step_type=step_type,
        stage_number=stage_number,
        cards=stacks,
        complete=True,
        source=source,
    )
    if not snapshot.is_authoritative():
        raise ValueError("完整牌庫已合併，但身份信心未達 authoritative 門檻")
    snapshot_record = _commit_scanned_owned_deck_snapshot(
        identity=active_run,
        snapshot=snapshot,
        evidence_path=str(final_capture["png_path"]),
        captured_at=float(final_capture["timestamp"]),
    )
    updated_shadow = apply_authoritative_deck_baseline(
        shadow,
        snapshot,
        captured_at=float(final_capture["timestamp"]),
        evidence_path=str(final_capture["png_path"]),
        required_card_id=idol_profile.produce_card_id,
    )
    save_run_shadow(updated_shadow, run_paths.shadow)
    return {
        "snapshot": snapshot_record.snapshot.to_dict(),
        "snapshot_record": snapshot_record.to_dict(),
        "inventory": inventory.to_dict(),
        "page_count": len(pages),
        "evidence_paths": evidence,
    }


def sync_live_deck_panel(
    *, step_type: str, stage_number: int, source: str
) -> Mapping[str, Any]:
    """Open and read the visible deck panel as one verified GUI operation."""

    from .live_actions import SuggestedClick, execute_suggested_click

    prepared = prepare_live_deck_panel_open()
    action = SuggestedClick.from_dict(prepared["action"])
    execution = execute_suggested_click(action, settle_seconds=1.0)
    if not execution.visual_change_detected:
        raise ValueError("已點擊牌組按鈕，但沒有偵測到面板畫面變化")
    analyzed = dict(_analyze_deck_panel_capture(execution.post_capture))
    # The panel is now open.  Scroll every page once and make that visible
    # inventory—not a speculative initial-deck merge—the authoritative run
    # baseline used by later search.
    full_scan = scan_live_owned_deck(
        step_type=step_type, stage_number=stage_number, source=source
    )
    return {
        **analyzed,
        "authoritative_deck": full_scan,
        "source_capture": prepared["capture"],
        "hand_detection_count": prepared["hand_detection_count"],
        "execution": execution.to_dict(),
    }


def _logic_state_for_lesson_screen(
    screen: Any,
    prior_state: Mapping[str, Any] | None,
    clear_target: int | None,
    gimmick_group_id: str | None = None,
    max_stamina: int | None = None,
) -> tuple[Any, int, tuple[str, ...]]:
    """Bootstrap turn one or verify a state predicted from our last click."""

    from .logic_engine import LogicExamState
    from .lesson_targets import infer_lesson_score_targets

    # A completed checkpoint belongs to the previous lesson.  It must remain
    # loadable for its result summary, but an exact new turn-one screen starts
    # a fresh tracked lesson without requiring manual file deletion.
    if (
        prior_state is not None
        and prior_state.get("turns_remaining") == 0
    ):
        prior_state = None
        clear_target = None

    if prior_state is None:
        targets = infer_lesson_score_targets(
            screen.clear_remaining,
            observed_limit_turn=screen.turns_remaining,
        )
        if screen.target_tier != "clear":
            raise ValueError(
                "尚未追蹤這堂課；只會從 CLEAR 首回合自動開始"
            )
        if screen.clear_remaining != targets.clear:
            raise ValueError("首回合 CLEAR 倒數與本機 Master 門檻不一致")
        if screen.block != 0:
            raise ValueError("首回合元氣不是 0；不會猜測未觀測的開場效果")
        # Round number is not printed by the lesson HUD. Bootstrap is anchored
        # by the full CLEAR target, zero score/block, and independent HUD
        # fields; later rounds advance only through verified transitions.
        state = LogicExamState(
            turns_remaining=screen.turns_remaining,
            stamina=screen.stamina,
            score=0,
            block=screen.block,
            good_impression=0,
            motivation=0,
            round_number=1,
            last_resolved_turn_start_round=1,
            max_stamina=0 if max_stamina is None else max_stamina,
        )
        return state, targets.clear, ()

    if clear_target is None:
        raise ValueError("tracked lesson is missing its visible target")
    try:
        state = LogicExamState(**dict(prior_state))
    except TypeError as error:
        raise ValueError("追蹤中的課程狀態欄位不完整") from error
    state.validate()
    original_limit_turn = state.turns_remaining + state.round_number - 1
    targets = infer_lesson_score_targets(
        clear_target,
        observed_limit_turn=original_limit_turn,
    )
    if clear_target is None or clear_target not in {
        targets.clear,
        targets.perfect,
    }:
        raise ValueError("追蹤中的課程缺少 CLEAR 基準")
    verified_target = targets.for_tier(screen.target_tier)
    if screen.target_tier == "perfect" and state.score < targets.clear:
        raise LessonFrameNotReady("畫面過早切換到 PERFECT 門檻")
    if screen.target_tier == "clear" and state.score >= targets.clear:
        raise LessonFrameNotReady("CLEAR 已達成，正在等待 PERFECT 門檻出現")

    runtime_status_effect_ids: tuple[str, ...] = ()
    if state.runtime_status_enchants:
        from .logic_engine import resolve_logic_status_turn_start

        runtime_status = resolve_logic_status_turn_start(state)
        if runtime_status.unsupported_rules:
            raise ValueError(
                "lesson runtime status contains unsupported rules: "
                + ", ".join(runtime_status.unsupported_rules)
            )
        state = runtime_status.after
        runtime_status_effect_ids = runtime_status.fired_effect_ids

    gimmick_effect_ids: tuple[str, ...] = ()
    if (
        gimmick_group_id == "none-observed"
        and state.last_resolved_turn_start_round < state.round_number
    ):
        state = replace(
            state,
            last_resolved_turn_start_round=state.round_number,
        )
    elif (
        gimmick_group_id
        and state.last_resolved_turn_start_round < state.round_number
    ):
        from .lesson_gimmick import (
            load_lesson_gimmick_profile,
            resolve_lesson_turn_start,
        )

        gimmick = resolve_lesson_turn_start(
            load_lesson_gimmick_profile(gimmick_group_id), state
        )
        if gimmick.unsupported_rules:
            raise ValueError(
                "課程 gimmick 尚未支援："
                + ", ".join(gimmick.unsupported_rules)
            )
        state = replace(
            gimmick.after,
            last_resolved_turn_start_round=state.round_number,
        )
        gimmick_effect_ids = gimmick.fired_effect_ids

    expected = {
        "turns_remaining": state.turns_remaining,
        "stamina": state.stamina,
        "block": state.block,
        "clear_remaining": max(0, verified_target - state.score),
    }
    observed = {
        "turns_remaining": screen.turns_remaining,
        "stamina": screen.stamina,
        "block": screen.block,
        "clear_remaining": screen.clear_remaining,
    }
    # Logic status icons occupy compact rows in activation order, not one
    # permanent row per status type.  The two OCR crops therefore identify the
    # visible values but cannot by themselves name them.  Reconcile the value
    # multiset against the rule-derived checkpoint; only fall back to the old
    # positional comparison when the sets do not agree.
    visible_status_values = sorted(
        int(value)
        for value in (
            getattr(screen, "good_impression", None),
            getattr(screen, "motivation", None),
        )
        if value is not None
    )
    expected_status_values = sorted(
        value
        for value in (int(state.good_impression), int(state.motivation))
        if value > 0
    )
    # Gimmick callouts can push the second compact status row below both fixed
    # OCR crops.  A visible value matching any rule-derived status is therefore
    # compatible evidence; requiring every expected value to be visible would
    # incorrectly relabel the remaining first row by its crop position.
    visible_status_matches_checkpoint = (
        not expected_status_values
        or not visible_status_values
        or any(
            value in expected_status_values for value in visible_status_values
        )
    )
    if not visible_status_matches_checkpoint:
        for status_name in ("good_impression", "motivation"):
            observed_value = getattr(screen, status_name, None)
            if observed_value is not None:
                expected[status_name] = int(getattr(state, status_name))
                observed[status_name] = int(observed_value)
    mismatches = [
        f"{key}:預測 {expected[key]}／畫面 {observed[key]}"
        for key in expected
        if expected[key] != observed[key]
    ]
    if mismatches:
        raise LessonFrameNotReady(
            "畫面與上一手及課程 gimmick 預測不一致："
            + "；".join(mismatches)
        )
    return (
        state,
        verified_target,
        tuple((*runtime_status_effect_ids, *gimmick_effect_ids)),
    )


def _canonical_detection_box(report: Any, detection: Any) -> tuple[int, int, int, int]:
    from .live_capture import CANONICAL_HEIGHT, CANONICAL_WIDTH

    left = round(detection.x * CANONICAL_WIDTH / report.image_width)
    top = round(detection.y * CANONICAL_HEIGHT / report.image_height)
    right = round(
        (detection.x + detection.width) * CANONICAL_WIDTH / report.image_width
    )
    bottom = round(
        (detection.y + detection.height) * CANONICAL_HEIGHT / report.image_height
    )
    return (
        max(0, min(CANONICAL_WIDTH - 1, left)),
        max(0, min(CANONICAL_HEIGHT - 1, top)),
        max(1, min(CANONICAL_WIDTH, right)),
        max(1, min(CANONICAL_HEIGHT, bottom)),
    )


def _master_card_with_observed_cost(
    card_id: str,
    upgrade: int,
    displayed_stamina_cost: int | None,
    *,
    stamina_consumption_down: bool = False,
    stamina_consumption_down_fixed: int = 0,
) -> Any:
    """Load a card while keeping the visible ordinary stamina cost.

    A card's displayed cost already includes ``forceStamina``.  The engine
    pays that component separately because block cannot absorb it, so split
    the visible total into its unavoidable and ordinary portions.  Capping the
    unavoidable portion at the visible total is important when a live-only
    reduction makes the displayed cost lower than our statically projected
    ``forceStamina`` value.
    """

    from .logic_engine import load_master_card

    card = load_master_card(card_id, upgrade)
    observed_cost = displayed_stamina_cost
    observed_force_cost = None
    if observed_cost is not None:
        effective_force_cost = card.force_stamina_cost
        if stamina_consumption_down:
            effective_force_cost = (effective_force_cost + 1) // 2
        effective_force_cost = max(
            0, effective_force_cost - stamina_consumption_down_fixed
        )
        observed_force_cost = min(observed_cost, effective_force_cost)
        observed_cost = max(0, observed_cost - observed_force_cost)
    return replace(
        card,
        observed_stamina_cost=observed_cost,
        observed_force_stamina_cost=observed_force_cost,
    )


def _card_rule_identifiers(card: Any) -> dict[str, list[str]]:
    """Collect direct and nested Master IDs for transition replay provenance."""

    effect_ids: set[str] = set()
    trigger_ids: set[str] = set()
    status_enchant_ids: set[str] = set()

    def visit(effect: Any) -> None:
        if getattr(effect, "id", ""):
            effect_ids.add(str(effect.id))
        if getattr(effect, "chain_effect_id", ""):
            effect_ids.add(str(effect.chain_effect_id))
        if getattr(effect, "trigger_id", ""):
            trigger_ids.add(str(effect.trigger_id))
        if getattr(effect, "status_enchant_id", ""):
            status_enchant_ids.add(str(effect.status_enchant_id))
        enchant = getattr(effect, "status_enchant_rule", None)
        if enchant is not None:
            status_enchant_ids.add(str(enchant.id))
            if getattr(enchant, "trigger_id", ""):
                trigger_ids.add(str(enchant.trigger_id))
            for nested in enchant.effects:
                visit(nested)

    if getattr(card, "play_trigger_id", ""):
        trigger_ids.add(str(card.play_trigger_id))
    for effect in card.effects:
        visit(effect)
    return {
        "card_ids": [str(card.id)],
        "effect_ids": sorted(effect_ids),
        "trigger_ids": sorted(trigger_ids),
        "status_enchant_ids": sorted(status_enchant_ids),
    }


def _lesson_card_layout_issue(report: Any, detections: tuple[Any, ...]) -> str | None:
    """Return why a one-to-five-card hand is still moving, or ``None``.

    The ordinary hand has three cards, but draw effects can leave four or five
    cards visible before the next play. Treating every larger hand as an
    animation made legitimate draw-card turns unreadable.
    """

    if not 1 <= len(detections) <= 5:
        return f"出牌畫面必須清楚辨識 1 至 5 張可用牌，目前為 {len(detections)}"
    if min(item.confidence for item in detections) < 0.80:
        return "至少一張卡牌的區域辨識信心不足 80%"

    # A selected card is enlarged in the middle of the screen and can still
    # be labelled ``cards`` by Maa's detector.  A stable playable hand lives in
    # the bottom band; frames above it are animation/preview frames.
    if min(item.y for item in detections) < report.image_height * 0.68:
        return "card row is above the stable bottom-hand band"

    minimum_width = report.image_width * 0.18
    minimum_height = report.image_height * 0.16
    if any(
        item.width < minimum_width or item.height < minimum_height
        for item in detections
    ):
        return "卡牌仍在展開動畫中（尺寸尚未穩定）"

    top_spread = max(item.y for item in detections) - min(
        item.y for item in detections
    )
    if top_spread > report.image_height * 0.08:
        return "卡牌仍在展開動畫中（尚未排列成同一列）"

    centers = tuple(item.x + item.width / 2 for item in detections)
    # Four- and five-card hands intentionally fan out with visible overlap.
    # The normal one-to-three-card row has wider spacing, so retain its stricter
    # transition guard while accepting the game's stable compact layout.
    minimum_center_ratio = 0.60 if len(detections) >= 4 else 0.85
    for left, right, left_center, right_center in zip(
        detections, detections[1:], centers, centers[1:]
    ):
        if (
            right_center - left_center
            < min(left.width, right.width) * minimum_center_ratio
        ):
            return "卡牌仍在展開動畫中（卡面仍互相重疊）"
    return None


def _card_preview_evidence(
    image_path: Path,
    recognizer: Any,
    *,
    expected_card_x: int | None = None,
    minimum_select_confidence: float = 0.85,
    minimum_panel_white_ratio: float = 0.35,
) -> CardPreviewEvidence:
    """Detect the SELECT overlay before any hypothetical HUD is consumed.

    The word alone is highly specific, but pairing it with the large white
    card-description panel avoids treating unrelated reward highlights as a
    card preview.  All coordinates are Maa's canonical 720x1280 space.
    """

    from PIL import Image

    from .screen_state import scale_canonical_box

    with Image.open(image_path.resolve()) as source:
        image = source.convert("RGB")
    if expected_card_x is not None and not 0 <= expected_card_x < 720:
        raise ValueError("expected card x must use Maa's canonical 720px space")
    # A pending action has stronger geometric evidence than a guessed hand
    # layout.  Inspect only that card's SELECT label so another selected slot
    # can never authorize the confirmation click.  Read-only callers without
    # a pending action retain the complete stable-layout scan.
    select_centers = (
        (int(expected_card_x),)
        if expected_card_x is not None
        else CARD_PREVIEW_SELECT_CENTERS
    )
    select_candidates = []
    for center_x in select_centers:
        box = (
            max(0, center_x - CARD_PREVIEW_SELECT_HALF_WIDTH),
            CARD_PREVIEW_SELECT_Y_RANGE[0],
            min(720, center_x + CARD_PREVIEW_SELECT_HALF_WIDTH),
            CARD_PREVIEW_SELECT_Y_RANGE[1],
        )
        crop = image.crop(scale_canonical_box(box, image.width, image.height))
        result = recognizer.recognize(crop)
        normalized = re.sub(r"[^A-Z]", "", result.text.upper())
        select_candidates.append(("SELECT" in normalized, result))
    panel = image.crop(
        scale_canonical_box(CARD_PREVIEW_PANEL_BOX, image.width, image.height)
    ).resize((54, 27), Image.Resampling.BILINEAR)
    pixels = tuple(panel.getdata())
    panel_white_ratio = (
        sum(1 for red, green, blue in pixels if min(red, green, blue) >= 230)
        / len(pixels)
    )
    select_matches = [
        result for contains_select, result in select_candidates if contains_select
    ]
    if select_matches:
        select = max(select_matches, key=lambda value: float(value.confidence))
        normalized = "SELECT"
    else:
        select = max(
            (result for _contains_select, result in select_candidates),
            key=lambda value: float(value.confidence),
        )
        normalized = re.sub(r"[^A-Z]", "", select.text.upper())
    detected = bool(
        "SELECT" in normalized
        and float(select.confidence) >= minimum_select_confidence
        and panel_white_ratio >= minimum_panel_white_ratio
    )
    return CardPreviewEvidence(
        detected=detected,
        select_text=select.text,
        select_confidence=float(select.confidence),
        panel_white_ratio=panel_white_ratio,
    )


def _preview_card_identity_evidence(
    image_path: Path,
    recognizer: Any,
    *,
    expected_card_id: str,
    expected_upgrade: int,
    minimum_ocr_confidence: float = 0.85,
    minimum_name_similarity: float = 0.90,
) -> CardPreviewIdentityEvidence:
    """Resolve the title in the SELECT detail panel against static Master names."""

    from PIL import Image, ImageOps

    from .screen_state import scale_canonical_box

    with Image.open(image_path.resolve()) as source:
        image = source.convert("RGB")
    crop = image.crop(
        scale_canonical_box(CARD_PREVIEW_NAME_BOX, image.width, image.height)
    )
    variants = (
        crop,
        ImageOps.autocontrast(crop.convert("L")),
        ImageOps.autocontrast(crop.convert("L")).point(
            lambda value: 255 if value > 150 else 0
        ),
    )
    candidates: list[tuple[float, float, Any, Any]] = []
    catalog = _shop_card_catalog()
    for variant in variants:
        result = recognizer.recognize(variant)
        matches = catalog.match(result.text, limit=3)
        if not matches:
            continue
        match = matches[0]
        candidates.append(
            (float(match.similarity), float(result.confidence), match, result)
        )
    if not candidates:
        return CardPreviewIdentityEvidence("", 0.0, None, None, 0.0, False)
    similarity, ocr_confidence, match, result = max(candidates, key=lambda item: item[:2])
    reliable = bool(
        ocr_confidence >= minimum_ocr_confidence
        and similarity >= minimum_name_similarity
    )
    card_id = str(match.card_id) if reliable else None
    # The SELECT panel title omits the enhancement suffix on real client
    # captures even when the source hand card is upgraded.  An explicit ``+``
    # remains authoritative; otherwise the stale-bound source box supplies the
    # upgrade and the preview independently verifies the base card identity.
    explicit_upgrade = "+" in str(result.text)
    upgrade = int(match.upgrade) if reliable and explicit_upgrade else None
    return CardPreviewIdentityEvidence(
        observed_text=str(result.text),
        ocr_confidence=ocr_confidence,
        card_id=card_id,
        upgrade=upgrade,
        name_similarity=similarity,
        matches_expected=bool(
            reliable
            and card_id == expected_card_id
            and (not explicit_upgrade or upgrade == expected_upgrade)
        ),
    )


def _card_play_result_evidence(
    image_path: Path,
    recognizer: Any,
    *,
    mode: Literal["lesson", "exam"],
) -> CardPlayResultEvidence:
    """Detect an explicit terminal screen without treating animation as success."""

    from PIL import Image

    from .screen_state import scale_canonical_box

    with Image.open(image_path.resolve()) as source:
        image = source.convert("RGB")
    if mode == "lesson":
        result = recognizer.recognize(
            image.crop(
                scale_canonical_box(
                    LESSON_RESULT_TEXT_BOX, image.width, image.height
                )
            )
        )
        normalized = re.sub(r"[^A-Z]", "", result.text.upper())
        kind = "perfect" if "PERFECT" in normalized else "clear" if "CLEAR" in normalized else ""
        detected = bool(kind and float(result.confidence) >= 0.80)
        return CardPlayResultEvidence(
            detected=detected,
            result_kind=kind,
            score=None,
            rank_text="",
            confidence=float(result.confidence) if detected else 0.0,
            raw_text={"result": result.text},
        )

    import numpy as np

    canonical = image
    if canonical.size != (720, 1280):
        canonical = canonical.resize((720, 1280), Image.Resampling.BILINEAR)
    pixels = np.asarray(canonical)
    white_per_row = np.all(pixels[:, 20:700, :] >= 235, axis=2).sum(axis=1)
    row_indexes = [
        int(index)
        for index in np.flatnonzero(white_per_row >= 600)
        if 180 <= int(index) <= 1050
    ]
    groups: list[list[int]] = []
    for index in row_indexes:
        if not groups or index > groups[-1][-1] + 1:
            groups.append([index])
        else:
            groups[-1].append(index)
    outline: tuple[int, int] | None = None
    for top_group, bottom_group in zip(groups, groups[1:]):
        top = top_group[0]
        bottom = bottom_group[-1]
        if 110 <= bottom - top <= 175:
            outline = (top, bottom)
            break
    next_result = recognizer.recognize(
        canonical.crop(EXAM_RESULT_NEXT_BOX)
    )
    next_text = re.sub(r"\s+", "", next_result.text).upper()
    next_detected = any(
        marker in next_text for marker in ("下一步", "NEXT", "次へ")
    )
    if outline is None or not next_detected:
        return CardPlayResultEvidence(
            detected=False,
            result_kind="",
            score=None,
            rank_text="",
            confidence=0.0,
            raw_text={"next": next_result.text},
        )
    top, bottom = outline
    score_result = recognizer.recognize(
        canonical.crop((190, top + 55, 385, max(top + 75, bottom - 5)))
    )
    rank_result = recognizer.recognize(
        canonical.crop((30, top + 15, 200, max(top + 55, bottom - 15)))
    )
    digits = "".join(re.findall(r"\d+", score_result.text))
    score = int(digits) if digits else None
    detected = bool(score is not None and float(score_result.confidence) >= 0.80)
    confidence = (
        min(float(score_result.confidence), max(0.0, float(next_result.confidence)))
        if detected
        else 0.0
    )
    return CardPlayResultEvidence(
        detected=detected,
        result_kind="ranked",
        score=score,
        rank_text=rank_result.text,
        confidence=confidence,
        raw_text={
            "next": next_result.text,
            "score": score_result.text,
            "rank": rank_result.text,
        },
    )


def _fixed_audition_result_layout(image_path: str | Path) -> bool:
    """Recognize the invariant ranking frame and sole orange Next target.

    This intentionally does not OCR rank, score, or button text.  Those facts
    are already serialized in the completed outer audition step; pixels only
    prove that the ranking page (rather than another page sharing the lower
    screen area) is visible before the fixed Next click.
    """

    import numpy as np
    from PIL import Image

    with Image.open(Path(image_path).resolve()) as source:
        image = source.convert("RGB")
    if image.size != (720, 1280):
        image = image.resize((720, 1280), Image.Resampling.BILINEAR)
    pixels = np.asarray(image)
    pale_rows = np.all(pixels[:, 20:700, :] >= 235, axis=2).mean(axis=1)
    bands = tuple(index for index in range(220, 430) if pale_rows[index] >= 0.80)
    if not bands or max(bands) - min(bands) < 110:
        return False
    left, top, right, bottom = EXAM_RESULT_NEXT_BOX
    target = pixels[top:bottom, left:right, :]
    orange = (
        (target[:, :, 0] >= 200)
        & (target[:, :, 1] >= 65)
        & (target[:, :, 1] <= 195)
        & (target[:, :, 2] <= 120)
    )
    # The canonical verification box deliberately includes padding around the
    # button. Real captures put the orange rounded rectangle at about 29.3%
    # of that box; require a substantial fixed target without making OCR or
    # locale text part of the input gate.
    return bool(float(orange.mean()) >= 0.25)


def _fixed_post_audition_dialogue_layout(image_path: str | Path) -> bool:
    """Recognize the fixed portrait dialogue panel after a Final result.

    The outer completed-step ledger proves that Final was cleared.  Pixels are
    used only to locate the large, safe dialogue panel: no character art,
    localized text, ``+`` marker colour, or OCR identity participates in the
    input decision.
    """

    import numpy as np
    from PIL import Image

    with Image.open(Path(image_path).resolve()) as source:
        image = source.convert("RGB")
    if image.size != (720, 1280):
        image = image.resize((720, 1280), Image.Resampling.BILINEAR)
    pixels = np.asarray(image)
    left, top, right, bottom = POST_AUDITION_DIALOGUE_BOX
    panel = pixels[top:bottom, left:right, :]
    panel_cream = (
        (panel[:, :, 0] >= 220)
        & (panel[:, :, 1] >= 205)
        & (panel[:, :, 2] >= 180)
    )
    name = pixels[905:955, 40:260, :]
    name_yellow = (
        (name[:, :, 0] >= 200)
        & (name[:, :, 1] >= 140)
        & (name[:, :, 2] <= 130)
    )
    return bool(panel_cream.mean() >= 0.50 and name_yellow.mean() >= 0.45)


def _fixed_post_audition_summary_layout(image_path: str | Path) -> bool:
    """Recognize the fixed rank/True-End condition summary sheet."""

    import numpy as np
    from PIL import Image

    with Image.open(Path(image_path).resolve()) as source:
        image = source.convert("RGB")
    if image.size != (720, 1280):
        image = image.resize((720, 1280), Image.Resampling.BILINEAR)
    pixels = np.asarray(image)
    rank = pixels[43:510, 40:680, :]
    conditions = pixels[538:1040, 40:680, :]
    orange_bar = pixels[1040:1098, 40:680, :]
    orange = (
        (orange_bar[:, :, 0] >= 220)
        & (orange_bar[:, :, 1] >= 120)
        & (orange_bar[:, :, 1] <= 210)
        & (orange_bar[:, :, 2] <= 120)
    )
    return bool(
        (rank.min(axis=2) >= 225).mean() >= 0.80
        and (conditions.min(axis=2) >= 225).mean() >= 0.75
        and orange.mean() >= 0.70
    )


def classify_initial_regular_final_live_native_path(image_path: str | Path) -> str:
    """Accept an exact 1280x720 Final-live frame without colour/text gates.

    Kept as a compatibility helper: any landscape frame is a valid first
    entry for Maa's safe top-left Click_1.  The live reader owns the one-click
    process-local state and returns ``playing`` on subsequent reads.
    """

    from PIL import Image

    with Image.open(Path(image_path).resolve()) as source:
        size = source.size
    if size != (1280, 720):
        raise ValueError("Final live native frame must be exactly 1280x720")
    return "start"


def execute_initial_regular_final_live_start(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Send Maa's sole safe Click_1 input through the landscape session."""

    from .controller_client import send_command

    capture = payload.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("Final live start payload is missing native capture")
    if (capture.get("width"), capture.get("height")) != (1280, 720):
        raise ValueError("Final live start capture is not landscape")
    current = dict(send_command("capture_native_once", timeout=15.0))
    if (current.get("width"), current.get("height")) != (1280, 720):
        raise ValueError("Final live page changed orientation before MAA input")
    status = dict(send_command("status"))
    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise ValueError("controller status is missing window geometry")
    client_width = int(geometry["client_width"])
    client_height = int(geometry["client_height"])
    offset_x = int(geometry["client_offset_x"])
    offset_y = int(geometry["client_offset_y"])
    # MaaGakumasu's ProduceShowStart runs the original Click_1 at (20,20)
    # before checking orientation.  It is outside selectable rewards/content.
    native_x, native_y = 20, 20
    window_x = offset_x + round(native_x * client_width / 1280)
    window_y = offset_y + round(native_y * client_height / 720)
    clicked = dict(
        send_command(
            "send_input_click_once",
            window_x=window_x,
            window_y=window_y,
        )
    )
    return {
        "accepted": True,
        "action": "maa-click-1-start-live",
        "pre_capture": current,
        "click": clicked,
    }


def _is_supported_maa_native_navigation_size(size: tuple[object, object]) -> bool:
    """Mirror the Maa controller's native portrait/landscape contract."""

    return size in {
        (720, 1279),
        (720, 1280),
        (720, 1281),
        (1280, 720),
    }


def execute_initial_regular_post_live_advance(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Run MaaGakumasu's original fixed ProduceEnd route to Home.

    LocalSave authorizes that Final live has ended.  Maa then recognizes and
    advances its own Generation/Finished/dialogue pages.  No local colour,
    text, card-art, or coordinate classifier participates in this path.
    """

    from .controller_client import send_command
    from .produce_outer_local_save import read_current_produce_outer_local_save

    authority = payload.get("authority")
    if not isinstance(authority, Mapping):
        raise ValueError("post-live advance lacks Produce LocalSave authority")
    authority_kind = authority.get("kind")
    if authority_kind not in {
        "produce-local-save-post-live-in-progress",
        "outer-completed-final-failure+maa-native-landscape",
    }:
        raise ValueError("post-live advance lacks Produce LocalSave authority")
    snapshot = read_current_produce_outer_local_save()
    lifecycle = snapshot.lifecycle
    if authority_kind == "produce-local-save-post-live-in-progress":
        if not (
            lifecycle is not None
            and lifecycle.is_end_live
            and lifecycle.is_in_progress
        ):
            raise ValueError("Produce LocalSave no longer authorizes post-live advance")
    else:
        completed_steps = snapshot.completed_steps
        completed = completed_steps[-1] if completed_steps else None
        if not (
            completed is not None
            and completed.step_type == 18
            and completed.log_index == authority.get("log_index")
            and any(
                line.line_type_name == "audition_failure"
                for line in completed.lines
            )
        ):
            raise ValueError("Produce LocalSave no longer proves failed Final")
    current = dict(send_command("capture_native_once", timeout=15.0))
    native_size = (current.get("width"), current.get("height"))
    if not _is_supported_maa_native_navigation_size(native_size):
        raise ValueError("post-live capture has an unsupported native size")
    completed = dict(send_command("advance_nia_post_live", timeout=330.0))
    if not bool(completed.get("completed")):
        raise RuntimeError("Maa post-live route stopped before Produce Home")
    return {
        "accepted": True,
        "action": "maa-produce-end",
        "pre_capture": current,
        "maa": completed,
    }


def execute_initial_regular_nia_early_end_advance(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Run Maa ProduceEnd after N.I.A. failed-audition ``next``.

    This route intentionally has no normal Final-live LocalSave requirement:
    selecting ``next`` on N.I.A.'s failed audition is itself the typed early
    termination boundary.  Maa recognizes the subsequent result pages.
    """

    from .controller_client import send_command

    if payload.get("kind") != "audition-failure-end" or payload.get("target") != "next":
        raise ValueError("N.I.A. early-end payload is not failed-audition next")
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or evidence.get("audition_failed") is not True:
        raise ValueError("N.I.A. early-end payload lacks failed-audition evidence")
    authority = payload.get("outer_authority")
    if not isinstance(authority, Mapping) or authority.get("kind") != (
        "produce-local-save-before-maa-subpage-input"
    ):
        raise ValueError("N.I.A. early-end payload lacks Produce LocalSave authority")
    current = dict(send_command("capture_native_once", timeout=15.0))
    native_size = (current.get("width"), current.get("height"))
    if not _is_supported_maa_native_navigation_size(native_size):
        raise ValueError("N.I.A. early-end result has an unsupported native size")
    completed = dict(send_command("advance_nia_post_live", timeout=330.0))
    if not bool(completed.get("completed")):
        raise RuntimeError("Maa N.I.A. early-end route stopped before Produce Home")
    return {
        "accepted": True,
        "action": "maa-nia-audition-failure-produce-end",
        "pre_capture": current,
        "maa": completed,
    }


def _deduplicate_card_detections(
    detections: tuple[Any, ...],
) -> tuple[Any, ...]:
    """Collapse Maa's recommendation/useless overlay onto its card box."""

    priority = {"recommend": 0, "cards": 1, "useless": 2}
    selected: list[Any] = []
    for candidate in sorted(
        detections,
        key=lambda item: (
            item.x,
            -priority.get(item.label, 0),
            -item.confidence,
        ),
    ):
        candidate_left = candidate.x
        candidate_top = candidate.y
        candidate_right = candidate.x + candidate.width
        candidate_bottom = candidate.y + candidate.height
        duplicate_index: int | None = None
        for index, existing in enumerate(selected):
            left = max(candidate_left, existing.x)
            top = max(candidate_top, existing.y)
            right = min(candidate_right, existing.x + existing.width)
            bottom = min(candidate_bottom, existing.y + existing.height)
            intersection = max(0, right - left) * max(0, bottom - top)
            smaller_area = min(
                candidate.width * candidate.height,
                existing.width * existing.height,
            )
            if smaller_area > 0 and intersection / smaller_area >= 0.80:
                duplicate_index = index
                break
        if duplicate_index is None:
            selected.append(candidate)
            continue
        existing = selected[duplicate_index]
        candidate_key = (
            priority.get(candidate.label, 0),
            candidate.confidence,
        )
        existing_key = (
            priority.get(existing.label, 0),
            existing.confidence,
        )
        if candidate_key > existing_key:
            selected[duplicate_index] = candidate
    return tuple(sorted(selected, key=lambda item: item.x))


def _trusted_low_confidence_lesson_numeric(screen: Any, *, tracked: bool) -> bool:
    """Allow weak stylized digits only when exact shadow verification follows."""

    if not tracked:
        return False
    verified_numeric_fields = {
        "turns_remaining",
        "clear_remaining",
        "stamina",
        "block",
    }
    low_fields = [
        key
        for key in (
            "turns_remaining",
            "clear_remaining",
            "stamina",
            "block",
            "lesson_parameter",
            "target_tier",
        )
        for confidence in (screen.field_confidence[key],)
        if confidence < 0.80 and getattr(screen, key, None) is not None
    ]
    if not low_fields or len(low_fields) > 2:
        return False
    for key in low_fields:
        if key not in verified_numeric_fields:
            return False
        confidence = float(screen.field_confidence[key])
        value = getattr(screen, key, None)
        raw = str(screen.raw_text.get(key, "")).strip()
        numeric_groups = re.findall(r"\d+", raw)
        if not (
            confidence >= 0.25
            and isinstance(value, int)
            and numeric_groups == [str(value)]
            and 0 <= value <= 9999
        ):
            return False
    return True


def _lesson_core_minimum_confidence(screen: Any) -> float:
    """Return the confidence of fields that actually anchor a lesson frame.

    The two left-side optional status crops are not fixed rows: tutorial and
    gimmick callouts can overlap them, and status icons collapse upward as
    effects expire.  They are reconciled against the rule-derived checkpoint
    later, so a stray digit in either crop must not make an otherwise crisp
    first frame look unstable.  The P-item countdown is likewise informative
    metadata rather than a checkpoint field.
    """

    core_fields = (
        "turns_remaining",
        "clear_remaining",
        "stamina",
        "block",
        "lesson_parameter",
        "target_tier",
    )
    return min(float(screen.field_confidence[key]) for key in core_fields)


def _trusted_low_confidence_exam_score(
    screen: Any, prior_state: Mapping[str, Any] | None
) -> bool:
    """Trust one weak score glyph only when the checkpoint predicts it exactly.

    The animated outdoor Final stage frequently lowers OCR confidence for the
    otherwise unambiguous zero score while the other four HUD fields remain
    crisp.  This exception is deliberately narrower than the lesson fallback:
    it applies only to ``player_score``, requires an existing checkpoint, and
    still leaves the exact turn/stamina/block comparison to the state verifier.
    """

    if prior_state is None:
        return False
    confidences = screen.field_confidence
    low_fields = [
        key for key, confidence in confidences.items() if confidence < 0.80
    ]
    if low_fields != ["player_score"]:
        return False
    expected = prior_state.get("score")
    raw = str(screen.raw_text.get("player_score", "")).strip()
    numeric_groups = re.findall(r"\d+", raw)
    return bool(
        float(confidences["player_score"]) >= 0.55
        and isinstance(expected, int)
        and screen.player_score == expected
        and numeric_groups == [str(screen.player_score)]
        and all(
            float(confidence) >= 0.80
            for key, confidence in confidences.items()
            if key != "player_score"
        )
    )


def _exam_top_tie_is_ambiguous(ranked: Any) -> bool:
    """An exact tie between duplicate copies is still one decision."""

    if len(ranked) < 2:
        return False
    first, second = ranked[0], ranked[1]
    tied = (
        first.auto_play_evaluation == second.auto_play_evaluation
        and first.strategic_value == second.strategic_value
        and first.transition.total_score_gain
        == second.transition.total_score_gain
    )
    if not tied:
        return False
    return (first.card.id, first.card.upgrade) != (
        second.card.id,
        second.card.upgrade,
    )


def _live_exam_solver_force_end_score(master_force_end_score: int) -> int:
    """Keep a static threshold advisory until the live result is verified.

    A predicted score can differ from the client when an unmodelled item,
    enchantment or rounding rule is active.  Writing ``turns_remaining=0``
    from that prediction would poison the next shadow checkpoint.  The live
    path therefore waits for post-click/result-screen evidence; the pure
    solver still supports force-end simulation for callers that have it.
    """

    if master_force_end_score < 0:
        raise ValueError("master force-end score cannot be negative")
    return 0


def _logic_state_for_exam_screen(
    screen: Any,
    rules: Any,
    prior_state: Mapping[str, Any] | None,
    bootstrap_status_candidates: Mapping[
        str, int | Sequence[int]
    ] | None = None,
) -> tuple[Any, tuple[str, ...]]:
    """Bootstrap or verify one audition checkpoint against visible numbers."""

    from .logic_engine import LogicExamState

    prior = None if prior_state is None else LogicExamState(**dict(prior_state))
    expected_round = rules.turns - screen.turns_remaining + 1
    fresh_first_turn = (
        screen.turns_remaining == rules.turns
        and screen.player_score == 0
        and expected_round == 1
    )
    if prior is None or (prior.turns_remaining == 0 and fresh_first_turn):
        from .exam_screen import (
            ExamLogicStatusObservation,
            reconcile_exam_logic_statuses,
        )

        if not 1 <= expected_round <= rules.turns:
            raise ValueError(
                f"畫面剩餘回合 {screen.turns_remaining} 與 Master "
                f"總回合 {rules.turns} 不一致"
            )
        if not fresh_first_turn:
            raise ValueError(
                "目前不是考試第一回合，且沒有可驗證的 exam checkpoint"
            )
        logic_status = getattr(screen, "logic_status", None)
        if not isinstance(logic_status, ExamLogicStatusObservation):
            raise ExamFrameNotReady(
                "first-turn audition logic-status layout is missing"
            )
        if not logic_status.layout_verified:
            raise ExamFrameNotReady(
                "first-turn audition logic-status layout is not settled: "
                + (logic_status.issue or "unknown compact status row")
            )
        try:
            resolved_status = reconcile_exam_logic_statuses(
                logic_status,
                bootstrap_status_candidates,
            )
        except ValueError as error:
            raise ValueError(
                "cannot uniquely bootstrap first-turn audition statuses: "
                f"{error}"
            ) from error
        state = LogicExamState(
            turns_remaining=screen.turns_remaining,
            stamina=screen.stamina,
            score=screen.player_score,
            block=screen.block,
            good_impression=resolved_status.good_impression,
            motivation=resolved_status.motivation,
            round_number=expected_round,
            last_resolved_turn_start_round=expected_round,
            score_multiplier_permille=screen.score_multiplier_permille,
            max_stamina=screen.stamina,
            turn_end_stamina_recovery=rules.turn_end_stamina_recovery,
        )
        state.validate()
        return state, ()

    prior.validate(allow_completed=True)
    state = prior
    runtime_status_effect_ids: tuple[str, ...] = ()
    if state.runtime_status_enchants:
        from .logic_engine import resolve_logic_status_turn_start

        runtime_status = resolve_logic_status_turn_start(state)
        if runtime_status.unsupported_rules:
            raise ValueError(
                "audition runtime status contains unsupported rules: "
                + ", ".join(runtime_status.unsupported_rules)
            )
        state = runtime_status.after
        runtime_status_effect_ids = runtime_status.fired_effect_ids

    gimmick_effect_ids: tuple[str, ...] = ()
    if (
        rules.gimmick_group_id
        and state.last_resolved_turn_start_round < state.round_number
    ):
        from .lesson_gimmick import (
            load_lesson_gimmick_profile,
            resolve_lesson_turn_start,
        )

        gimmick = resolve_lesson_turn_start(
            load_lesson_gimmick_profile(rules.gimmick_group_id), state
        )
        if gimmick.unsupported_rules:
            raise ValueError(
                "audition gimmick contains unsupported rules: "
                + ", ".join(gimmick.unsupported_rules)
            )
        state = replace(
            gimmick.after,
            last_resolved_turn_start_round=state.round_number,
        )
        gimmick_effect_ids = gimmick.fired_effect_ids

    expected = {
        "turns_remaining": state.turns_remaining,
        "stamina": state.stamina,
        "score": state.score,
        "block": state.block,
    }
    visible = {
        "turns_remaining": screen.turns_remaining,
        "stamina": screen.stamina,
        "score": screen.player_score,
        "block": screen.block,
    }
    # The HUD exposes only an integer percentage, while the client scores with
    # the underlying interpolated multiplier. Reconcile the resulting tiny
    # drift only when every independently visible structural field still
    # matches the checkpoint exactly.
    if (
        all(
            visible[key] == expected[key]
            for key in ("turns_remaining", "stamina", "block")
        )
        and abs(visible["score"] - expected["score"]) <= 2
    ):
        state = replace(state, score=visible["score"])
        expected["score"] = visible["score"]
    mismatches = [
        f"{key}: 畫面 {visible[key]} != checkpoint {value}"
        for key, value in expected.items()
        if visible[key] != value
    ]
    if mismatches:
        raise ValueError("考試 checkpoint 驗證失敗：" + "；".join(mismatches))
    return (
        replace(
            state,
            score_multiplier_permille=screen.score_multiplier_permille,
            turn_end_stamina_recovery=rules.turn_end_stamina_recovery,
        ),
        tuple((*runtime_status_effect_ids, *gimmick_effect_ids)),
    )


def _lesson_reaches_perfect_target(
    *,
    screen: Any,
    state: Any,
    verified_target: int,
    after_score: int,
) -> bool:
    """Return whether the clicked card makes the lesson end immediately.

    The HUD initially shows CLEAR and changes to PERFECT only after a card has
    resolved.  A large scoring card can cross both thresholds at once, so the
    post-click checkpoint must use the static PERFECT value even though the
    source frame still says CLEAR.
    """

    if screen.target_tier == "perfect":
        perfect_target = verified_target
    else:
        from .lesson_targets import infer_lesson_score_targets

        original_limit_turn = state.turns_remaining + state.round_number - 1
        perfect_target = infer_lesson_score_targets(
            verified_target,
            observed_limit_turn=original_limit_turn,
        ).perfect
    return after_score >= perfect_target


def _partial_screen_observation(
    values: Mapping[str, Any],
    *,
    semantic_boundary: str,
    source: str,
    evidence: Mapping[str, Any] | None = None,
    comparable_to_prediction: bool = True,
) -> dict[str, Any]:
    """Label screenshot-derived state without prediction-filling unknown fields."""

    known_fields = tuple(values)
    return {
        **dict(values),
        "_observation": {
            "schema_version": 1,
            "partial": True,
            "semantic_boundary": semantic_boundary,
            "comparable_to_prediction": comparable_to_prediction,
            "known_fields": list(known_fields),
            "provenance": {field: source for field in known_fields},
            "evidence": dict(evidence or {}),
        },
    }


def make_live_card_frame_analyzer(
    *,
    mode: Literal["lesson", "exam"],
    expected_card_id: str,
    expected_upgrade: int,
    expected_after: Mapping[str, Any],
    expected_card_x: int | None = None,
    support_card_ids: Sequence[str] = (),
    expected_hand_support_ids: Sequence[Sequence[str]] | None = None,
    expected_ordered_hand: (
        Sequence[tuple[str, int, Sequence[str]]] | None
    ) = None,
    clear_target: int | None = None,
    lesson_perfect_target: int | None = None,
    gimmick_group_id: str | None = None,
    idol_card_id: str | None = None,
    produce_id: str | None = None,
    step_type: str | None = None,
    stage_number: int | None = None,
) -> Callable[[Path], Any]:
    """Build a screenshot-only analyzer for a single pending card play."""

    from .live_actions import CardPlayFrameAnalysis

    expected = dict(expected_after)
    ordered_hand_authority: tuple[tuple[str, int, tuple[str, ...]], ...] | None = None
    if expected_ordered_hand is not None:
        normalized: list[tuple[str, int, tuple[str, ...]]] = []
        for index, raw in enumerate(expected_ordered_hand):
            if not isinstance(raw, tuple) or len(raw) != 3:
                raise ValueError(f"expected_ordered_hand[{index}] is malformed")
            card_id, upgrade, support_ids = raw
            if not isinstance(card_id, str) or not card_id:
                raise ValueError(
                    f"expected_ordered_hand[{index}] has no card ID"
                )
            if (
                isinstance(upgrade, bool)
                or not isinstance(upgrade, int)
                or upgrade < 0
            ):
                raise ValueError(
                    f"expected_ordered_hand[{index}] has invalid upgrade"
                )
            ids = tuple(dict.fromkeys(str(value) for value in support_ids if value))
            if len(ids) > 1:
                raise ValueError(
                    "multiple support upgrades on one Hand card need a richer "
                    "screen proof"
                )
            normalized.append((card_id, upgrade, ids))
        if not normalized:
            raise ValueError("expected_ordered_hand must not be empty")
        ordered_hand_authority = tuple(normalized)
    exam_rules = None
    if mode == "lesson":
        if clear_target is None or lesson_perfect_target is None:
            raise ValueError("lesson card verification requires both score targets")
    elif mode == "exam":
        if (
            idol_card_id is None
            or produce_id is None
            or step_type is None
            or stage_number is None
        ):
            raise ValueError("exam card verification requires an exact stage identity")
        from .audition_rules import load_audition_rules

        exam_rules = load_audition_rules(
            idol_card_id,
            produce_id=produce_id,
            step_type=step_type,
            number=stage_number,
        )
    else:
        raise ValueError(f"unsupported card-play mode: {mode}")

    def analyze(image_path: Path) -> Any:
        recognizer = _live_text_recognizer()
        preview = _card_preview_evidence(
            image_path,
            recognizer,
            expected_card_x=expected_card_x,
        )
        if preview.detected:
            # With an ordered ExamSave Hand, the selected slot is already
            # authoritative.  The SELECT label is read only inside that
            # slot's fixed x-range, so card-title OCR would be a redundant and
            # less reliable identity guess.
            identity = None
            if ordered_hand_authority is None:
                identity = _preview_card_identity_evidence(
                    image_path,
                    recognizer,
                    expected_card_id=expected_card_id,
                    expected_upgrade=expected_upgrade,
                )
            matches_expected = bool(
                ordered_hand_authority is not None
                or (identity is not None and identity.matches_expected)
            )
            issues = (
                ()
                if matches_expected
                else (
                    "SELECT card mismatch: expected "
                    f"{expected_card_id}@{expected_upgrade}, observed "
                    f"{identity.card_id}@{identity.upgrade} "
                    f"({identity.observed_text!r})",
                )
            )
            return CardPlayFrameAnalysis(
                phase="selected_preview",
                observed_state=_partial_screen_observation(
                    {},
                    semantic_boundary="selected-preview",
                    source="screenshot:selected-card-preview",
                    evidence={
                        "selected_card_id": (
                            expected_card_id
                            if identity is None
                            else identity.card_id
                        ),
                        "selected_upgrade": (
                            expected_upgrade
                            if identity is None
                            else identity.upgrade
                        ),
                        "selected_name_text": (
                            "ExamSave ordered-slot authority"
                            if identity is None
                            else identity.observed_text
                        ),
                    },
                    comparable_to_prediction=False,
                ),
                selected_card_matches=matches_expected,
                confidence=(
                    preview.select_confidence
                    if identity is None
                    else min(
                        preview.select_confidence,
                        identity.ocr_confidence,
                        identity.name_similarity,
                    )
                ),
                issues=issues,
            )

        result = _card_play_result_evidence(
            image_path, recognizer, mode=mode
        )
        if result.detected:
            issues: list[str] = []
            if mode == "lesson":
                assert clear_target is not None
                assert lesson_perfect_target is not None
                score = int(expected.get("score", 0))
                predicted_kind = (
                    "perfect"
                    if score >= lesson_perfect_target
                    else "clear"
                    if score >= clear_target
                    else "failed"
                )
                terminal_expected = bool(
                    int(expected.get("turns_remaining", -1)) == 0
                    or score >= lesson_perfect_target
                )
                matches = terminal_expected and result.result_kind == predicted_kind
                if not matches:
                    issues.append(
                        "lesson result mismatch: expected "
                        f"{predicted_kind}, observed {result.result_kind}"
                    )
                semantic_key = (
                    "lesson",
                    "result",
                    result.result_kind,
                )
                observed = _partial_screen_observation(
                    {"turns_remaining": 0},
                    semantic_boundary="post-card-result",
                    source="screenshot:lesson-result",
                    evidence={"result_kind": result.result_kind},
                )
                verified_state = dict(expected)
                verified_state["turns_remaining"] = 0
            else:
                assert exam_rules is not None
                actual_score = int(result.score or 0)
                predicted_score = int(expected.get("score", -1))
                terminal_expected = bool(
                    int(expected.get("turns_remaining", -1)) == 0
                    or (
                        exam_rules.force_end_score > 0
                        and actual_score >= exam_rules.force_end_score
                    )
                )
                score_matches = abs(actual_score - predicted_score) <= 2
                matches = terminal_expected and score_matches
                if not score_matches:
                    issues.append(
                        "exam result score mismatch: expected "
                        f"{predicted_score}, observed {actual_score}"
                    )
                if not terminal_expected:
                    issues.append("exam result appeared before a predicted terminal condition")
                semantic_key = (
                    "exam",
                    "result",
                    result.rank_text,
                    actual_score,
                )
                observed = _partial_screen_observation(
                    {
                        "turns_remaining": 0,
                        "score": actual_score,
                    },
                    semantic_boundary="post-card-result",
                    source="screenshot:exam-result",
                    evidence={"rank_text": result.rank_text},
                )
                verified_state = dict(expected)
                verified_state["turns_remaining"] = 0
                verified_state["score"] = actual_score
            return CardPlayFrameAnalysis(
                phase="result",
                semantic_key=semantic_key,
                observed_state=observed,
                verified_state=verified_state if matches else {},
                expected_matches=matches,
                confidence=result.confidence,
                issues=tuple(issues),
            )

        try:
            report = _card_detector().detect_path(image_path)
            hand_detections = _deduplicate_card_detections(
                tuple(
                    item
                    for item in report.detections
                    if item.label in {"cards", "recommend", "useless"}
                    and item.y >= report.image_height * 0.62
                )
            )
            layout_issue = _lesson_card_layout_issue(report, hand_detections)
            if layout_issue is not None:
                return CardPlayFrameAnalysis(
                    phase="resolving", issues=(layout_issue,)
                )
            # Relabel an unplayable overlay only for identity OCR.  Its original
            # label remains in the semantic key and no legal recommendation is
            # inferred from this verification pass.
            view = None
            if ordered_hand_authority is None:
                identity_report = replace(
                    report,
                    detections=tuple(
                        replace(item, label="cards") for item in hand_detections
                    ),
                )
                view = identify_card_detections(identity_report, image_path, {})
                if len(view.cards) != len(hand_detections):
                    raise ValueError(
                        "hand identity count differs from stable card boxes"
                    )
                if any(card.upgrade is None for card in view.cards):
                    raise ValueError(
                        "at least one stable hand card has unknown upgrade"
                    )
            if ordered_hand_authority is not None:
                if len(ordered_hand_authority) != len(hand_detections):
                    raise ValueError(
                        "ExamSave ordered Hand count differs from stable card boxes"
                    )
                expected_supports = tuple(
                    value[2] for value in ordered_hand_authority
                )
                support_markers = tuple(
                    _card_support_upgrade_markers(
                        image_path,
                        (detection,),
                        support_card_ids=values,
                    )[0]
                    for detection, values in zip(
                        hand_detections,
                        expected_supports,
                        strict=True,
                    )
                )
            elif expected_hand_support_ids is None:
                support_markers = _card_support_upgrade_markers(
                    image_path,
                    hand_detections,
                    support_card_ids=support_card_ids,
                )
            else:
                expected_supports = tuple(
                    tuple(dict.fromkeys(str(value) for value in values if value))
                    for values in expected_hand_support_ids
                )
                if len(expected_supports) != len(hand_detections):
                    raise ValueError(
                        "expected support lineage count differs from stable card boxes"
                    )
                if any(len(values) > 1 for values in expected_supports):
                    raise ValueError(
                        "multiple support upgrades on one Hand card need a richer screen proof"
                    )
                support_markers = tuple(
                    _card_support_upgrade_markers(
                        image_path,
                        (detection,),
                        support_card_ids=values,
                    )[0]
                    for detection, values in zip(
                        hand_detections,
                        expected_supports,
                        strict=True,
                    )
                )
            if len(support_markers) != len(hand_detections):
                raise ValueError("support marker count differs from stable card boxes")
            if (
                ordered_hand_authority is not None
                or expected_hand_support_ids is not None
            ):
                marker_lineage_mismatches = tuple(
                    index
                    for index, (marker, expected_ids) in enumerate(
                        zip(support_markers, expected_supports, strict=True)
                    )
                    if (
                        bool(expected_ids) != marker.detected
                        or (
                            expected_ids
                            and marker.support_card_id != expected_ids[0]
                        )
                    )
                )
                if marker_lineage_mismatches:
                    raise ValueError(
                        "support marker topology differs from expected Hand lineage in slots: "
                        + ",".join(
                            str(index) for index in marker_lineage_mismatches
                        )
                    )
            unresolved_support = tuple(
                index
                for index, marker in enumerate(support_markers)
                if marker.detected and marker.support_card_id is None
            )
            if unresolved_support:
                raise ValueError(
                    "support marker identity is unresolved for Hand slots: "
                    + ",".join(str(index) for index in unresolved_support)
                )
            if ordered_hand_authority is not None:
                hand_key = tuple(
                    (
                        index,
                        card_id,
                        upgrade,
                        0,
                        "fixed-slot",
                        marker.detected,
                        support_ids[0] if support_ids else None,
                    )
                    for index, (
                        (card_id, upgrade, support_ids),
                        marker,
                    ) in enumerate(
                        zip(
                            ordered_hand_authority,
                            support_markers,
                            strict=True,
                        )
                    )
                )
            else:
                assert view is not None
                hand_key = tuple(
                    (
                        index,
                        card.card_id,
                        int(card.upgrade),
                        card.observed_stamina_cost,
                        detection.label,
                        marker.detected,
                        marker.support_card_id,
                    )
                    for index, (card, detection, marker) in enumerate(
                        zip(
                            view.cards,
                            hand_detections,
                            support_markers,
                            strict=True,
                        )
                    )
                )
            hand_confidence = min(
                float(item.confidence) for item in hand_detections
            )
        except Exception as error:
            return CardPlayFrameAnalysis(
                phase="resolving",
                issues=(f"hand is not settled: {type(error).__name__}: {error}",),
            )

        issues = []
        if mode == "exam":
            assert exam_rules is not None
            from .exam_screen import read_exam_screen_path

            try:
                screen = read_exam_screen_path(image_path, recognizer)
            except ValueError as error:
                return CardPlayFrameAnalysis(
                    phase="resolving",
                    issues=(f"exam HUD is not settled: {error}",),
                )
            if (
                screen.minimum_confidence < 0.80
                and not _trusted_low_confidence_exam_score(screen, expected)
            ):
                return CardPlayFrameAnalysis(
                    phase="resolving",
                    issues=(
                        "exam HUD confidence below stable threshold: "
                        f"{screen.minimum_confidence:.0%}",
                    ),
                )
            visible = {
                "turns_remaining": screen.turns_remaining,
                "score": screen.player_score,
                "stamina": screen.stamina,
                "block": screen.block,
                "score_multiplier_permille": screen.score_multiplier_permille,
            }
            try:
                matched_state, _gimmick_ids = _logic_state_for_exam_screen(
                    screen, exam_rules, expected
                )
            except (ValueError, ExamFrameNotReady) as error:
                matched_state = None
                issues.append(str(error))
            observed_state = _partial_screen_observation(
                visible,
                semantic_boundary="post-card-settled",
                source="screenshot:exam-hud",
                evidence={
                    "support_upgrade_markers": [
                        {
                            "hand_index": index,
                            **marker.to_dict(),
                        }
                        for index, marker in enumerate(support_markers)
                        if marker.detected
                    ]
                },
            )
            expected_matches = matched_state is not None
            verified_state = (
                asdict(matched_state) if matched_state is not None else {}
            )
            semantic_key = (
                "exam",
                screen.turns_remaining,
                screen.player_score,
                screen.stamina,
                screen.block,
                screen.score_multiplier_permille,
                hand_key,
            )
            confidence = min(screen.minimum_confidence, hand_confidence)
        else:
            assert clear_target is not None
            from .lesson_screen import read_lesson_screen_path

            try:
                screen = read_lesson_screen_path(image_path, recognizer)
            except ValueError as error:
                return CardPlayFrameAnalysis(
                    phase="resolving",
                    issues=(f"lesson HUD is not settled: {error}",),
                )
            core_confidence = _lesson_core_minimum_confidence(screen)
            if core_confidence < 0.80 and not _trusted_low_confidence_lesson_numeric(
                screen, tracked=True
            ):
                return CardPlayFrameAnalysis(
                    phase="resolving",
                    issues=(
                        "lesson HUD confidence below stable threshold: "
                        f"{core_confidence:.0%}",
                    ),
                )
            try:
                matched_state, verified_target, _gimmick_ids = (
                    _logic_state_for_lesson_screen(
                        screen, expected, clear_target, gimmick_group_id
                    )
                )
            except (ValueError, LessonFrameNotReady) as error:
                matched_state = None
                verified_target = (
                    lesson_perfect_target
                    if screen.target_tier == "perfect"
                    else clear_target
                )
                issues.append(str(error))
            visible = {
                "turns_remaining": screen.turns_remaining,
                "score": max(0, int(verified_target) - screen.clear_remaining),
                "stamina": screen.stamina,
                "block": screen.block,
            }
            observed_state = _partial_screen_observation(
                visible,
                semantic_boundary="post-card-settled",
                source="screenshot:lesson-hud",
                evidence={
                    "clear_remaining": screen.clear_remaining,
                    "target_tier": screen.target_tier,
                    "lesson_parameter": screen.lesson_parameter,
                    "support_upgrade_markers": [
                        {
                            "hand_index": index,
                            **marker.to_dict(),
                        }
                        for index, marker in enumerate(support_markers)
                        if marker.detected
                    ],
                },
            )
            expected_matches = matched_state is not None
            verified_state = (
                asdict(matched_state) if matched_state is not None else {}
            )
            semantic_key = (
                "lesson",
                screen.turns_remaining,
                screen.clear_remaining,
                screen.stamina,
                screen.block,
                screen.lesson_parameter,
                screen.target_tier,
                hand_key,
            )
            confidence = min(core_confidence, hand_confidence)
        return CardPlayFrameAnalysis(
            phase="settled",
            semantic_key=semantic_key,
            observed_state=observed_state,
            verified_state=verified_state,
            expected_matches=expected_matches,
            confidence=confidence,
            issues=tuple(issues),
        )

    return analyze


def read_live_exam(
    *,
    prior_state: Mapping[str, Any] | None = None,
    idol_card_id: str,
    produce_id: str = "produce-001",
    step_type: str = "ProduceStepType_AuditionMid1",
    stage_number: int = 1,
    bootstrap_status_candidates: Mapping[
        str, int | Sequence[int]
    ] | None = None,
    identity: Any | None = None,
) -> Mapping[str, Any]:
    """Read and rank one visible audition-stage hand."""

    from .audition_rules import load_audition_rules
    from .exam_screen import read_exam_screen_path
    from .exam_solver import recommend_audition_hand
    from .item_rules import load_idol_item_rule
    from .live_actions import SuggestedClick

    rules = load_audition_rules(
        idol_card_id,
        produce_id=produce_id,
        step_type=step_type,
        number=stage_number,
    )
    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("controller capture did not provide a PNG path")
    path = Path(png_path)
    recognizer = _live_text_recognizer()
    preview = _card_preview_evidence(path, recognizer)
    if preview.detected:
        raise ExamFrameNotReady(
            "card SELECT preview is hypothetical, not a settled hand "
            f"(OCR {preview.select_confidence:.0%})"
        )
    expected_score = None
    if prior_state is not None and isinstance(prior_state.get("score"), int):
        expected_score = int(prior_state["score"])
    try:
        screen = read_exam_screen_path(
            path,
            recognizer,
            expected_score=expected_score,
        )
    except ValueError as error:
        raise ExamFrameNotReady(f"考試畫面 OCR 尚未穩定：{error}") from error
    if (
        screen.minimum_confidence < 0.80
        and not _trusted_low_confidence_exam_score(screen, prior_state)
    ):
        raise ExamFrameNotReady(
            f"考試畫面 OCR 最低信心只有 {screen.minimum_confidence:.0%}"
        )
    state, gimmick_effect_ids = _logic_state_for_exam_screen(
        screen,
        rules,
        prior_state,
        bootstrap_status_candidates,
    )

    runtime_gate = None
    if identity is not None:
        from .live_exam_runtime_gate import resolve_live_exam_runtime_gate
        from .run_identity import RunIdentity

        if not isinstance(identity, RunIdentity):
            raise TypeError("identity must be RunIdentity")
        if identity.idol_card_id != idol_card_id:
            raise ValueError("live Exam identity idol card mismatch")
        if identity.produce_id != produce_id:
            raise ValueError("live Exam identity produce mismatch")
        # Capture/OCR and checkpoint reconciliation intentionally happen
        # before this durable replay gate.  A recommendation is solved only
        # after the marker, artifacts, and schema-v3 checkpoint all replay.
        runtime_gate = resolve_live_exam_runtime_gate(
            identity=identity,
            step_type=step_type,
            stage_number=stage_number,
            observed_checkpoint_state=prior_state,
            catalog=_loadout_passive_catalog(),
        )

    report = _card_detector().detect_path(path)
    detections = tuple(
        sorted(
            (item for item in report.detections if item.label == "cards"),
            key=lambda item: item.x,
        )
    )
    layout_issue = _lesson_card_layout_issue(report, detections)
    if layout_issue is not None:
        raise ExamFrameNotReady(layout_issue)
    view = identify_card_detections(report, path, capture)
    if len(view.cards) != len(detections):
        raise ExamFrameNotReady("卡牌身分數與偵測框數不一致")
    if any(card.upgrade is None for card in view.cards):
        raise ExamFrameNotReady("至少一張卡牌的強化狀態尚未辨識完成")

    cards = tuple(
        _master_card_with_observed_cost(
            card.card_id,
            int(card.upgrade),
            card.observed_stamina_cost,
            stamina_consumption_down=(
                state.stamina_consumption_down_turns > 0
            ),
            stamina_consumption_down_fixed=(
                state.stamina_consumption_down_fixed
            ),
        )
        for card in view.cards
    )
    item = (
        runtime_gate.merged_item_rule
        if runtime_gate is not None and runtime_gate.ready
        else load_idol_item_rule(idol_card_id)
    )
    assert item is not None
    lesson_type = "ProduceStepLessonType_Unknown"
    if identity is not None:
        from .audition_local_save_state import (
            load_audition_local_save_state,
            verified_turn_schedule_from_local_save,
        )
        from .run_identity import paths_for

        local_evidence = load_audition_local_save_state(
            paths_for(identity).audition_local_save_state
        )
        if local_evidence is not None:
            schedule = verified_turn_schedule_from_local_save(local_evidence)
            frame = next(
                (
                    value
                    for value in schedule.frames
                    if value.round_number == state.round_number
                ),
                None,
            )
            if frame is not None:
                lesson_type = frame.lesson_type
    solver_force_end_score = _live_exam_solver_force_end_score(
        rules.force_end_score
    )
    ranked = recommend_audition_hand(
        state,
        cards,
        item,
        force_end_score=solver_force_end_score,
        lesson_type=lesson_type,
    )
    if not ranked or not ranked[0].fully_supported:
        unsupported = ranked[0].unsupported_rules if ranked else ("empty-hand",)
        raise ValueError(
            "目前手牌含未支援規則：" + ", ".join(unsupported)
        )
    if _exam_top_tie_is_ambiguous(ranked):
        raise ValueError("目前最高兩張牌同分；暫停自動建議")

    winner = ranked[0]
    winner_row = view.cards[winner.hand_index]
    winner_detection = detections[winner.hand_index]
    box = _canonical_detection_box(report, winner_detection)
    display_name = winner_row.label.split("｜", 1)[0]
    action = SuggestedClick(
        label=f"出牌：{display_name}",
        canonical_x=(box[0] + box[2]) // 2,
        canonical_y=(box[1] + box[3]) // 2,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        # One declared target click; the verified executor owns the conditional
        # SELECT confirmation click.
        click_count=1,
    )
    candidates = []
    for entry in ranked:
        payload = entry.to_dict()
        payload["display_name"] = view.cards[entry.hand_index].label.split(
            "｜", 1
        )[0]
        candidates.append(payload)
    if runtime_gate is None:
        loadout = _live_loadout_safety_metadata()
        safety_blockers = tuple(str(value) for value in loadout["blockers"])
        item_ids = [item.id]
    else:
        resolution = runtime_gate.resolution
        passive_source_ids = (
            () if resolution is None else resolution.passive_source_ids
        )
        memory_ids = [
            value for value in passive_source_ids if value.startswith("ability-")
        ]
        support_ids = [
            value for value in passive_source_ids if value.startswith("s_card-")
        ]
        # A verified one-step transition is not yet a verified full-horizon
        # decision.  Keep execution closed until the deck search result is
        # bound to this exact hand below.
        safety_blockers = tuple(
            (*runtime_gate.blockers, "audition-horizon-not-integrated")
        )
        loadout = {
            "run_id": identity.run_id,
            "support_count": len(support_ids),
            "memory_count": len(memory_ids),
            "identifiers": {
                "memory_ids": memory_ids,
                "support_ids": support_ids,
                "effect_ids": [],
                "trigger_ids": [],
                "status_enchant_ids": [],
            },
            "preflight_rule_ids": (
                [] if resolution is None else list(resolution.rule_ids)
            ),
            "auto_click_eligible": not safety_blockers,
            "blockers": list(safety_blockers),
        }
        item_ids = (
            [item.id]
            if resolution is None or not resolution.item_source_ids
            else list(resolution.item_source_ids)
        )
    rule_ids = _card_rule_identifiers(winner.card)
    rule_ids.update(
        {
            "item_ids": item_ids,
            "memory_ids": [],
            "support_ids": [],
            "gimmick_ids": [rules.gimmick_group_id]
            if rules.gimmick_group_id
            else [],
            "effect_ids": sorted(
                set(rule_ids["effect_ids"])
                | set(winner.item_effect_ids)
                | set(gimmick_effect_ids)
                | set(winner.transition.runtime_status_effect_ids)
            ),
            "status_enchant_ids": sorted(
                set(rule_ids["status_enchant_ids"])
                | set(winner.item_enchantment_ids)
                | set(winner.transition.runtime_status_enchant_ids)
                | set(winner.transition.runtime_status_added_ids)
            ),
        }
    )
    rule_ids = _merge_loadout_rule_ids(rule_ids, loadout)
    return {
        "capture": dict(capture),
        "screen": screen.to_dict(),
        "logic_state": asdict(state),
        "rules": rules.to_dict(),
        "force_end_policy": {
            "master_score": rules.force_end_score,
            "solver_score": solver_force_end_score,
            "verified": False,
            "reason": "等待點擊後或結果畫面確認，不以預測分數提前結束 shadow state",
        },
        "gimmick_effect_ids": list(gimmick_effect_ids),
        "item": {
            "id": item.id,
            "name": item.name,
            "source_ids": item_ids,
        },
        "cards": [asdict(row) for row in view.cards],
        "candidates": candidates,
        "recommendation": candidates[0],
        "recommended_after": asdict(winner.transition.after),
        "card_play_context": {
            "mode": "exam",
            "expected_card_id": winner.card.id,
            "expected_upgrade": winner.card.upgrade,
            "idol_card_id": idol_card_id,
            "produce_id": rules.produce_id,
            "step_type": rules.step_type,
            "stage_number": rules.number,
            "support_card_ids": list(rule_ids["support_ids"]),
        },
        "rule_ids": rule_ids,
        "loadout": loadout,
        "exam_session": (
            None
            if runtime_gate is None or runtime_gate.session is None
            else runtime_gate.session.to_dict()
        ),
        "preflight": {
            "ready": bool(runtime_gate is not None and runtime_gate.ready),
            "context_id": (
                None
                if runtime_gate is None or runtime_gate.context is None
                else runtime_gate.context.context_id
            ),
            "context_digest": (
                None
                if runtime_gate is None or runtime_gate.context is None
                else runtime_gate.context.digest
            ),
            "transition_id": (
                None
                if runtime_gate is None or runtime_gate.session is None
                else runtime_gate.session.transition_id
            ),
        },
        "safety": {
            "auto_click_eligible": not safety_blockers,
            "blockers": list(safety_blockers),
        },
        "action": action.to_dict(),
        "confidence": min(
            screen.minimum_confidence,
            *(item.confidence for item in detections),
        ),
        "method": (
            "本回合分數、體力與狀態轉移為畫面 OCR＋本機 Master 精算；"
            "跨回合排序只使用既有好印象自然衰減與早期幹勁／元氣的保守 setup heuristic，"
            "尚未假裝知道未出現的抽牌順序。"
        ),
    }


def read_live_exam_with_retry(
    *,
    prior_state: Mapping[str, Any] | None = None,
    idol_card_id: str,
    produce_id: str = "produce-001",
    step_type: str = "ProduceStepType_AuditionMid1",
    stage_number: int = 1,
    bootstrap_status_candidates: Mapping[
        str, int | Sequence[int]
    ] | None = None,
    identity: Any | None = None,
    attempts: int = 5,
    delay_seconds: float = 0.9,
) -> Mapping[str, Any]:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_error: ExamFrameNotReady | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = dict(
                read_live_exam(
                    prior_state=prior_state,
                    idol_card_id=idol_card_id,
                    produce_id=produce_id,
                    step_type=step_type,
                    stage_number=stage_number,
                    bootstrap_status_candidates=bootstrap_status_candidates,
                    identity=identity,
                )
            )
            result["read_attempts"] = attempt
            return result
        except ExamFrameNotReady as error:
            last_error = error
            if attempt < attempts:
                time.sleep(max(0.0, delay_seconds))
    assert last_error is not None
    raise ExamFrameNotReady(
        f"連續 {attempts} 次仍無穩定考試手牌：{last_error}"
    ) from last_error


def _recommend_lesson_hand_with_context(
    state: LogicExamState,
    cards: tuple[MasterCard, ...],
    item: object,
    *,
    clear_target: int,
    gimmick_group_id: str | None,
    lesson_type: str = "ProduceStepLessonType_Unknown",
    max_stamina: int | None = None,
):
    """Keep the observed lesson's exact gimmick context in solver carry."""

    from .lesson_solver import recommend_logic_hand

    return recommend_logic_hand(
        state,
        cards,
        item,
        clear_target=clear_target,
        gimmick_group_id=gimmick_group_id,
        lesson_type=lesson_type,
    )


def read_live_lesson(
    *,
    prior_state: Mapping[str, Any] | None = None,
    clear_target: int | None = None,
    idol_card_id: str,
    gimmick_group_id: str | None = None,
    lesson_type: str = "ProduceStepLessonType_Unknown",
    max_stamina: int | None = None,
    equipped_item_rule: Any | None = None,
    equipped_item_ids: Sequence[str] = (),
) -> Mapping[str, Any]:
    """Read, verify, and rank one visible Plan 2 lesson hand.

    A continued lesson is accepted only when the visible turn, stamina, block,
    and CLEAR countdown exactly match the transition produced by our previous
    verified click.
    """

    from dataclasses import asdict

    from .item_rules import EquippedItemRule, load_idol_item_rule
    from .lesson_screen import read_lesson_screen_path
    from .live_actions import SuggestedClick

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("controller capture did not provide a PNG path")
    path = Path(png_path)
    recognizer = _live_text_recognizer()
    preview = _card_preview_evidence(path, recognizer)
    if preview.detected:
        raise LessonFrameNotReady(
            "card SELECT preview is hypothetical, not a settled hand "
            f"(OCR {preview.select_confidence:.0%})"
        )
    try:
        screen = read_lesson_screen_path(path, recognizer)
    except ValueError as error:
        raise LessonFrameNotReady(
            f"課程畫面 OCR 尚未穩定：{error}"
        ) from error
    core_minimum_confidence = _lesson_core_minimum_confidence(screen)
    if core_minimum_confidence < 0.80:
        # Paddle occasionally assigns the stylized single remaining-turn
        # digit a modest confidence even when it reads the glyph correctly.
        # Accept only that narrow case: an exact one-digit parse at >=55%,
        # while every other required HUD field remains independently >=80%.
        other_required = (
            "clear_remaining",
            "stamina",
            "block",
            "lesson_parameter",
            "target_tier",
        )
        trusted_single_turn = (
            screen.field_confidence["turns_remaining"] >= 0.55
            and screen.raw_text["turns_remaining"].strip()
            == str(screen.turns_remaining)
            and 1 <= screen.turns_remaining <= 9
            and all(screen.field_confidence[key] >= 0.80 for key in other_required)
        )
        trusted_tracked_numeric = _trusted_low_confidence_lesson_numeric(
            screen, tracked=prior_state is not None
        )
        if not trusted_single_turn and not trusted_tracked_numeric:
            raise LessonFrameNotReady(
                f"課程核心數值 OCR 最低信心只有 {core_minimum_confidence:.0%}"
            )
    state, verified_target, gimmick_effect_ids = _logic_state_for_lesson_screen(
        screen,
        prior_state,
        clear_target,
        gimmick_group_id,
        max_stamina,
    )

    report = _card_detector().detect_path(path)
    hand_detections = _deduplicate_card_detections(
        tuple(
            item
            for item in report.detections
            if item.label in {"cards", "recommend", "useless"}
            and item.y >= report.image_height * 0.62
        )
    )
    layout_issue = _lesson_card_layout_issue(report, hand_detections)
    if layout_issue is not None:
        raise LessonFrameNotReady(layout_issue)
    detections = tuple(
        item for item in hand_detections if item.label in {"cards", "recommend"}
    )
    view = identify_card_detections(report, path, capture)
    if len(view.cards) != len(detections):
        raise LessonFrameNotReady("卡牌區域與卡名辨識數量不一致")
    if any(card.upgrade is None for card in view.cards):
        raise LessonFrameNotReady("至少一張卡牌沒有由卡名 OCR 判定強化階段")

    cards = tuple(
        _master_card_with_observed_cost(
            card.card_id,
            int(card.upgrade),
            card.observed_stamina_cost,
            stamina_consumption_down=(
                state.stamina_consumption_down_turns > 0
            ),
            stamina_consumption_down_fixed=(
                state.stamina_consumption_down_fixed
            ),
        )
        for card in view.cards
    )
    if equipped_item_rule is not None and not isinstance(
        equipped_item_rule, EquippedItemRule
    ):
        raise TypeError("equipped_item_rule must be EquippedItemRule")
    item = equipped_item_rule or load_idol_item_rule(idol_card_id)
    item_ids = tuple(equipped_item_ids)
    if any(not isinstance(item_id, str) or not item_id for item_id in item_ids):
        raise ValueError("equipped_item_ids must contain non-empty item IDs")
    ranked = _recommend_lesson_hand_with_context(
        state,
        cards,
        item,
        clear_target=verified_target,
        gimmick_group_id=gimmick_group_id,
        lesson_type=lesson_type,
    )
    if ranked and ranked[0].is_turn_skip:
        raise ValueError(
            "live lesson recommendation is SKIP; refusing to create a card action"
        )
    if not ranked or not ranked[0].fully_supported:
        unsupported = ranked[0].unsupported_rules if ranked else ("empty-hand",)
        raise ValueError("目前手牌包含未支援規則：" + ", ".join(unsupported))
    if len(ranked) > 1 and (
        ranked[0].strategic_value == ranked[1].strategic_value
        and ranked[0].auto_play_evaluation
        == ranked[1].auto_play_evaluation
        and
        ranked[0].carry_score_gain == ranked[1].carry_score_gain
        and ranked[0].transition.after.motivation
        == ranked[1].transition.after.motivation
        and ranked[0].transition.after.block == ranked[1].transition.after.block
        and ranked[0].transition.after.stamina
        == ranked[1].transition.after.stamina
        and (ranked[0].card.id, ranked[0].card.upgrade)
        != (ranked[1].card.id, ranked[1].card.upgrade)
    ):
        raise ValueError("目前最佳牌並非唯一；不建立自動點擊")

    winner = ranked[0]
    winner_row = view.cards[winner.hand_index]
    winner_detection = detections[winner.hand_index]
    box = _canonical_detection_box(report, winner_detection)
    center_x = (box[0] + box[2]) // 2
    center_y = (box[1] + box[3]) // 2
    display_name = winner_row.label.split("｜", 1)[0]
    action = SuggestedClick(
        label=f"出牌：{display_name}",
        canonical_x=center_x,
        canonical_y=center_y,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        # The second click is sent only after SELECT and card identity verify.
        click_count=1,
    )

    candidates = []
    for entry in ranked:
        if entry.is_turn_skip:
            continue
        payload = entry.to_dict()
        payload["display_name"] = view.cards[entry.hand_index].label.split("｜", 1)[0]
        candidates.append(payload)
    loadout = _live_loadout_safety_metadata()
    rule_ids = _card_rule_identifiers(winner.card)
    rule_ids.update(
        {
            "item_ids": list(item_ids or (item.id,)),
            "memory_ids": [],
            "support_ids": [],
            "gimmick_ids": [gimmick_group_id] if gimmick_group_id else [],
            "effect_ids": sorted(
                set(rule_ids["effect_ids"])
                | set(winner.current_item_effect_ids)
                | set(gimmick_effect_ids)
                | set(winner.transition.runtime_status_effect_ids)
            ),
            "status_enchant_ids": sorted(
                set(rule_ids["status_enchant_ids"])
                | set(winner.current_item_enchantment_ids)
                | set(winner.transition.runtime_status_enchant_ids)
                | set(winner.transition.runtime_status_added_ids)
            ),
        }
    )
    rule_ids = _merge_loadout_rule_ids(rule_ids, loadout)
    from .lesson_targets import infer_lesson_score_targets

    original_limit_turn = state.turns_remaining + state.round_number - 1
    lesson_targets = infer_lesson_score_targets(
        verified_target,
        observed_limit_turn=original_limit_turn,
    )
    predicted_terminal = bool(
        winner.transition.after.turns_remaining == 0
        or winner.transition.after.score >= lesson_targets.perfect
    )
    return {
        "capture": dict(capture),
        "screen": screen.to_dict(),
        "logic_state": asdict(state),
        "clear_target": verified_target,
        "item": {"id": item.id, "name": item.name},
        "gimmick": {
            "id": gimmick_group_id or "none-observed",
            "fired_effect_ids": list(gimmick_effect_ids),
        },
        "cards": [asdict(row) for row in view.cards],
        "candidates": candidates,
        "recommendation": candidates[0],
        # Do not write a terminal turn from a threshold prediction.  An actual
        # CLEAR/PERFECT result frame is the only live authority for turn zero.
        "recommended_after": asdict(winner.transition.after),
        "card_play_context": {
            "mode": "lesson",
            "expected_card_id": winner.card.id,
            "expected_upgrade": winner.card.upgrade,
            "clear_target": lesson_targets.clear,
            "lesson_perfect_target": lesson_targets.perfect,
            "gimmick_group_id": gimmick_group_id,
            "lesson_type": lesson_type,
            "predicted_terminal": predicted_terminal,
        },
        "rule_ids": rule_ids,
        "loadout": loadout,
        "safety": {
            "auto_click_eligible": bool(loadout["auto_click_eligible"]),
            "blockers": list(loadout["blockers"]),
        },
        "action": action.to_dict(),
        "confidence": min(
            core_minimum_confidence,
            *(item.confidence for item in detections),
        ),
        "method": (
            "本回合 Master 效果精算；保守延續分只延伸已取得的好印象，"
            "不猜未來抽牌或未來道具觸發"
        ),
    }


def read_live_lesson_with_retry(
    *,
    prior_state: Mapping[str, Any] | None = None,
    clear_target: int | None = None,
    idol_card_id: str,
    gimmick_group_id: str | None = None,
    lesson_type: str = "ProduceStepLessonType_Unknown",
    max_stamina: int | None = None,
    equipped_item_rule: Any | None = None,
    equipped_item_ids: Sequence[str] = (),
    attempts: int = 5,
    delay_seconds: float = 0.9,
) -> Mapping[str, Any]:
    """Wait for a stable next hand without weakening recognition checks."""

    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")

    last_error: LessonFrameNotReady | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = dict(
                read_live_lesson(
                    prior_state=prior_state,
                    clear_target=clear_target,
                    idol_card_id=idol_card_id,
                    gimmick_group_id=gimmick_group_id,
                    lesson_type=lesson_type,
                    max_stamina=max_stamina,
                    equipped_item_rule=equipped_item_rule,
                    equipped_item_ids=equipped_item_ids,
                )
            )
            result["read_attempts"] = attempt
            return result
        except LessonFrameNotReady as error:
            last_error = error
            if attempt < attempts:
                time.sleep(delay_seconds)

    assert last_error is not None
    raise LessonFrameNotReady(
        f"等待穩定出牌畫面共 {attempts} 次仍未成功：{last_error}"
    ) from last_error


def read_live_overview() -> Mapping[str, Any]:
    """Capture and OCR the visible First Produce action overview."""

    from .screen_state import read_overview_path

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    state = read_overview_path(Path(png_path))
    return {"capture": dict(capture), "state": state.to_dict()}


def read_live_activity_reward() -> Mapping[str, Any]:
    """Capture and OCR one visible activity-reward shadow checkpoint.

    The archived Initial-Regular reward evidence does not contain a visible
    Continue/Next control.  Keep this reader click-free until a detector can
    bind such a control to this exact capture; callers can still persist the
    reward before failing closed.
    """

    from .activity_reward import read_activity_reward_path

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    state = read_activity_reward_path(Path(png_path))
    return {
        "capture": dict(capture),
        "state": state.to_dict(),
        "shadow_patch": state.shadow_patch(),
        "action_blocker": "activity-reward-continue-unproven",
    }


TRAINING_DRINK_REWARD_OPEN_BOX = (70, 835, 650, 960)
SETTLED_PASSIVE_NOTIFICATION_BOX = (25, 420, 700, 575)
SETTLED_CARD_REWARD_DISPLAY_BOX = (70, 470, 650, 1140)


def _training_drink_reward_authority(
    state: Any,
    *,
    snapshot: Any,
) -> Mapping[str, Any]:
    """Join one visible lesson drink with exact outer-save and Master facts."""

    from .drink_catalog import load_drink_catalog
    from .equipped_item_snapshot import INVENTORY_DRINK, classify_inventory_name

    lesson_steps = tuple(
        step
        for step in snapshot.completed_steps
        if 1 <= step.step_type <= 9
        and any(line.line_type == 0 for line in step.lines)
    )
    if not lesson_steps:
        raise ValueError("outer LocalSave has no completed lesson for this reward")
    step = max(lesson_steps, key=lambda value: value.log_index)
    drink_lines = tuple(line for line in step.lines if line.line_type == 16)
    if len(drink_lines) != 1 or not drink_lines[0].target_id:
        raise ValueError("completed lesson does not contain exactly one drink reward")
    drink_id = drink_lines[0].target_id

    classification = classify_inventory_name(state.item_name)
    if classification.classification != INVENTORY_DRINK:
        raise ValueError("visible lesson reward is not an exact recognized drink")
    if classification.drink_id != drink_id:
        raise ValueError(
            "visible drink differs from outer LocalSave: "
            f"{classification.drink_id} != {drink_id}"
        )

    drink = load_drink_catalog().get_drink(drink_id)
    if len(drink.effect_refs) != 1:
        raise ValueError("lesson drink does not have one exact Master effect")
    effect = drink.effect_refs[0].effect
    descriptions = tuple(
        item for item in drink.produce_descriptions if isinstance(item, Mapping)
    )
    effect_names = tuple(
        str(item.get("text", ""))
        for item in descriptions
        if item.get("produceDescriptionType")
        == "ProduceDescriptionType_ProduceExamEffectType"
    )
    effect_values = tuple(
        int(item.get("effectValue1", 0))
        for item in descriptions
        if item.get("produceDescriptionType") == "ProduceDescriptionType_Exam"
    )
    if effect_names != (state.effect_name,) or effect_values != (state.effect_value,):
        raise ValueError(
            "visible lesson reward effect differs from Master: "
            f"{state.effect_name}+{state.effect_value}"
        )
    if effect.effect_value1 != state.effect_value:
        raise ValueError("drink effect value differs from normalized Master catalog")

    if snapshot.stamina != state.stamina:
        raise ValueError(
            f"lesson reward stamina differs from LocalSave: {state.stamina} != {snapshot.stamina}"
        )
    if snapshot.produce_points != state.produce_points:
        raise ValueError(
            "lesson reward Produce Points differ from LocalSave: "
            f"{state.produce_points} != {snapshot.produce_points}"
        )
    point_lines = tuple(line for line in step.lines if line.line_type == 21)
    if len(point_lines) != 1 or not point_lines[0].delta.is_integer():
        raise ValueError("completed lesson Produce Point delta is not exact")
    if int(point_lines[0].delta) != state.earned_produce_points:
        raise ValueError(
            "visible earned Produce Points differ from LocalSave: "
            f"{state.earned_produce_points} != {int(point_lines[0].delta)}"
        )
    attribute_by_line_type = {6: "vocal", 7: "dance", 8: "visual"}
    passive_lines = tuple(
        line
        for line in step.lines
        if line.is_triggered and line.line_type in attribute_by_line_type
    )
    confirmed_passives: list[dict[str, object]] = []
    settled_attributes: dict[str, int] = {}
    for line in passive_lines:
        if not line.before.is_integer() or not line.after.is_integer():
            raise ValueError("triggered reward attribute delta is not integral")
        attribute = attribute_by_line_type[line.line_type]
        after = int(line.after)
        prior = settled_attributes.get(attribute)
        if prior is not None and prior != after:
            raise ValueError(
                f"triggered reward has ambiguous settled {attribute} values"
            )
        settled_attributes[attribute] = after
        confirmed_passives.append(
            {
                "attribute": attribute,
                "before": int(line.before),
                "after": after,
                "delta": int(line.delta),
                # LocalSave support triggers carry both owner and trigger IDs;
                # item triggers legitimately carry only trigger_id.  The
                # applied before/after values are the authority needed here,
                # so preserve whichever source fields the game serialized
                # instead of rejecting a settled item effect.
                "source_id": line.trigger_owner_id or line.trigger_id or None,
                "support_card_id": line.trigger_owner_id or None,
                "trigger_id": line.trigger_id,
                "outer_line": [line.log_index, line.detail_index, line.line_index],
            }
        )
    return {
        "kind": "training-drink-reward+outer-completed-step+master",
        "outer_log_count": snapshot.log_count,
        "outer_step_log_index": step.log_index,
        "outer_step_type": step.step_type,
        "outer_week_marker": step.week_marker,
        "drink_id": drink_id,
        "drink_effect_ids": list(drink.produce_drink_effect_ids),
        "exam_effect_id": effect.source_id,
        "effect_type": effect.effect_type,
        "effect_value1": effect.effect_value1,
        "settled_attributes": settled_attributes,
        "confirmed_passives": confirmed_passives,
    }


def read_live_training_drink_reward() -> Mapping[str, Any]:
    """Read and authorize one completed-lesson drink reward for one click."""

    from .activity_reward import read_training_drink_reward_path
    from .live_actions import SuggestedClick
    from .produce_outer_local_save import (
        DEFAULT_PC_GAME_ROOT,
        read_current_produce_outer_local_save,
    )

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    state = read_training_drink_reward_path(Path(png_path))
    snapshot = read_current_produce_outer_local_save()
    authority = _training_drink_reward_authority(state, snapshot=snapshot)
    left, top, right, bottom = TRAINING_DRINK_REWARD_OPEN_BOX
    action = SuggestedClick(
        label="training-drink-reward:receive",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=TRAINING_DRINK_REWARD_OPEN_BOX,
        click_count=1,
    )
    return {
        "capture": dict(capture),
        "state": state.to_dict(),
        "authority": dict(authority),
        "action": action.to_dict(),
    }


def read_live_settled_passive_notification() -> Mapping[str, Any]:
    """Bind one harmless post-reward passive notification to a MAA click.

    These banners are displayed after the reward and its passive attribute
    mutation are already present in Produce LocalSave.  They do not offer a
    choice.  The reader therefore proves only the visible notification shape
    and signed delta; it deliberately does not identify card artwork or
    recompute the already-settled attribute value.
    """

    from PIL import Image, ImageOps

    from .live_actions import SuggestedClick

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    path = Path(png_path)
    image = Image.open(path).convert("RGB")
    recognizer = _live_text_recognizer()
    description = recognizer.recognize(
        ImageOps.autocontrast(image.crop((75, 440, 610, 505)).convert("L"))
    )
    delta_line = recognizer.recognize(
        ImageOps.autocontrast(image.crop((75, 490, 250, 550)).convert("L"))
    )
    text = re.sub(r"\s+", "", description.text)
    if not (
        ("獲得時" in text or "取得時" in text)
        and ("能力值" in text or "參數" in text or "パラメータ" in text)
        and ("上升" in text or "提升" in text or "增加" in text or "上昇" in text)
    ):
        raise ValueError(f"not a settled passive notification: {description.text!r}")
    delta_match = re.search(r"\+\s*(\d{1,3})", delta_line.text)
    if delta_match is None:
        raise ValueError(f"passive notification delta is not +N: {delta_line.text!r}")
    attribute_aliases = {
        "vocal": ("歌唱", "Vocal", "ボーカル"),
        "dance": ("舞蹈", "Dance", "ダンス"),
        "visual": ("視覺", "Visual", "ビジュアル"),
    }
    matched = [
        attribute
        for attribute, aliases in attribute_aliases.items()
        if any(alias in text for alias in aliases)
    ]
    if len(matched) != 1:
        raise ValueError(
            "passive notification attribute is not unique: "
            f"{description.text!r}"
        )
    left, top, right, bottom = SETTLED_PASSIVE_NOTIFICATION_BOX
    action = SuggestedClick(
        label="dismiss-settled-passive-notification",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=SETTLED_PASSIVE_NOTIFICATION_BOX,
        click_count=1,
    )
    return {
        "capture": dict(capture),
        "state": {
            "attribute": matched[0],
            "delta": int(delta_match.group(1)),
            "description": description.text,
            "confidence": min(description.confidence, delta_line.confidence),
        },
        "authority": {
            "kind": "visible-settled-passive-notification",
            "mutation_policy": "observe-only-already-settled",
        },
        "action": action.to_dict(),
    }


def read_live_card_acquire_notification() -> Mapping[str, Any]:
    """Dismiss one settled ``獲得技能卡`` animation with exactly one tap.

    The card is already server-owned at this point; this reader deliberately
    does not identify, rank, or submit a reward.  The next loop iteration must
    capture the fresh three-card page and hand ownership back to the reward
    reader.
    """

    from PIL import Image

    from .live_actions import SuggestedClick

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    path = Path(png_path)
    with Image.open(path) as image:
        evidence = detect_card_acquire_notification(image.convert("RGB"))
    if evidence is None:
        raise ValueError("not a card-acquire notification")
    left, top, right, bottom = evidence.action_box
    action = SuggestedClick(
        label="dismiss-card-acquire-notification",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=evidence.action_box,
        click_count=1,
    )
    return {
        "capture": dict(capture),
        "kind": "continue",
        "target": "card-acquire-notification",
        "state": evidence.to_dict(),
        "authority": {
            "kind": "visible-card-acquire-notification",
            "mutation_policy": "observe-only-already-settled",
            "next_capture_required": True,
        },
        "action": action.to_dict(),
    }


def read_live_settled_card_reward_display() -> Mapping[str, Any]:
    """Dismiss the card-receive animation after LocalSave proves card_add.

    The animation is informational and appears only after the server has
    accepted the reward.  Identity comes from the newest outer ``card_add``;
    OCR checks only that the enlarged card title is the same localized Master
    title.  No thumbnail matching, ranking, or second reward decision occurs.
    """

    from PIL import Image, ImageOps

    from .live_actions import SuggestedClick
    from .produce_outer_local_save import (
        DEFAULT_PC_GAME_ROOT,
        read_current_produce_outer_local_save,
    )

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    snapshot = read_current_produce_outer_local_save(DEFAULT_PC_GAME_ROOT)
    card_events = tuple(
        line for line in snapshot.inventory_events if line.line_type == 15
    )
    if not card_events:
        raise ValueError("outer LocalSave has no received card to bind")
    latest = max(
        card_events,
        key=lambda line: (line.log_index, line.detail_index, line.line_index),
    )
    catalog = _shop_card_catalog()
    entries = tuple(
        value
        for value in catalog.entries
        if value.card_id == latest.target_id
        and value.upgrade == int(latest.target_subscription_number)
    )
    names = {value.display_name for value in entries if value.display_name.strip()}
    if not names:
        raise ValueError("received card has no localized Master title")
    path = Path(png_path)
    image = Image.open(path).convert("RGB")
    title = _live_text_recognizer().recognize(
        ImageOps.autocontrast(image.crop((235, 835, 500, 910)).convert("L"))
    )
    observed = re.sub(r"\s+", "", title.text)
    matching_names = tuple(
        value for value in names if re.sub(r"\s+", "", value) == observed
    )
    if len(matching_names) != 1 or title.confidence < 0.85:
        raise ValueError(
            "received card display/title mismatch: "
            f"{title.text!r} for {latest.target_id}"
        )
    left, top, right, bottom = SETTLED_CARD_REWARD_DISPLAY_BOX
    action = SuggestedClick(
        label=f"dismiss-received-card:{latest.target_id}",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=SETTLED_CARD_REWARD_DISPLAY_BOX,
        click_count=1,
    )
    return {
        "capture": dict(capture),
        "state": {
            "card_id": latest.target_id,
            "upgrade": int(latest.target_subscription_number),
            "display_name": matching_names[0],
            "confidence": title.confidence,
        },
        "authority": {
            "kind": "outer-local-save-card-add+visible-title",
            "log_index": latest.log_index,
            "detail_index": latest.detail_index,
            "line_index": latest.line_index,
        },
        "action": action.to_dict(),
    }


def checkpoint_live_training_drink_reward(
    *,
    result: Mapping[str, Any] | None = None,
    produce_id: str | None = None,
    character_id: str | None = None,
    idol_card_id: str | None = None,
) -> Mapping[str, Any]:
    """Persist a Master/LocalSave-bound lesson drink before dismissing it."""

    from .activity_reward import TrainingDrinkRewardState
    from .run_identity import load_active_run, paths_for
    from .run_shadow import (
        DrinkRewardDefinition,
        drink_reward_observation,
        load_run_shadow,
        save_run_shadow,
    )

    active_run = load_active_run()
    if active_run is None:
        raise ValueError("no active run identity; refuse to checkpoint lesson drink")
    supplied = {
        "produce_id": produce_id,
        "character_id": character_id,
        "idol_card_id": idol_card_id,
    }
    expected = {
        "produce_id": active_run.produce_id,
        "character_id": active_run.character_id,
        "idol_card_id": active_run.idol_card_id,
    }
    mismatches = [
        key for key, value in supplied.items() if value is not None and value != expected[key]
    ]
    if mismatches:
        raise ValueError(
            "training drink identity differs from active run: " + ", ".join(mismatches)
        )
    checkpoint = dict(read_live_training_drink_reward() if result is None else result)
    capture = checkpoint.get("capture")
    raw_state = checkpoint.get("state")
    authority = checkpoint.get("authority")
    if not isinstance(capture, Mapping) or not isinstance(raw_state, Mapping):
        raise ValueError("training drink reward result is malformed")
    if not isinstance(authority, Mapping):
        raise ValueError("training drink reward authority is missing")
    png_path = capture.get("png_path")
    timestamp = capture.get("timestamp")
    if not isinstance(png_path, str) or not Path(png_path).is_file():
        raise ValueError("training drink reward screenshot evidence is missing")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise ValueError("training drink reward timestamp is missing")
    raw_text = raw_state.get("raw_text")
    if not isinstance(raw_text, Mapping):
        raise ValueError("training drink reward raw OCR evidence is missing")
    state = TrainingDrinkRewardState(
        item_name=str(raw_state["item_name"]),
        effect_name=str(raw_state["effect_name"]),
        effect_value=int(raw_state["effect_value"]),
        stamina=int(raw_state["stamina"]),
        max_stamina=int(raw_state["max_stamina"]),
        produce_points=int(raw_state["produce_points"]),
        earned_produce_points=int(raw_state["earned_produce_points"]),
        confidence=float(raw_state["confidence"]),
        raw_text=dict(raw_text),
    )
    # Re-read only the authoritative outer log and Master rows.  The exact
    # screenshot is not recaptured, so the subsequent click remains bound to it.
    from .produce_outer_local_save import read_current_produce_outer_local_save

    current_authority = _training_drink_reward_authority(
        state, snapshot=read_current_produce_outer_local_save()
    )
    if dict(authority) != dict(current_authority):
        raise ValueError("training drink reward authority changed before checkpoint")
    effect_ids = authority.get("drink_effect_ids")
    if not isinstance(effect_ids, list) or any(not isinstance(value, str) for value in effect_ids):
        raise ValueError("training drink reward effect identity is invalid")
    settled_attributes = authority.get("settled_attributes", {})
    confirmed_passives = authority.get("confirmed_passives", [])
    if not isinstance(settled_attributes, Mapping):
        raise ValueError("training drink settled attributes are invalid")
    if not isinstance(confirmed_passives, list) or any(
        not isinstance(value, Mapping) for value in confirmed_passives
    ):
        raise ValueError("training drink confirmed passives are invalid")
    reward = DrinkRewardDefinition(
        str(authority["drink_id"]),
        state.item_name,
        tuple(effect_ids),
    )
    run_paths = paths_for(active_run)
    shadow = load_run_shadow(run_paths.shadow)
    if shadow is None:
        raise ValueError("run shadow is missing for training drink reward")
    observation = drink_reward_observation(
        reward,
        captured_at=float(timestamp),
        selection_evidence_path=png_path,
        received_evidence_path=png_path,
        confidence=state.confidence,
        settled_attributes={
            str(key): int(value) for key, value in settled_attributes.items()
        },
        confirmed_passives=tuple(dict(value) for value in confirmed_passives),
    )
    matches = tuple(
        value
        for value in shadow.observations
        if value.kind == "drink_reward"
        and value.metadata.get("drink_id") == reward.drink_id
        and value.evidence_path == png_path
    )
    if matches:
        if len(matches) != 1 or matches[0] != observation:
            raise ValueError("training drink reward was checkpointed with different facts")
        updated = shadow
        already_checkpointed = True
    else:
        updated = shadow.apply(observation)
        save_run_shadow(updated, run_paths.shadow)
        already_checkpointed = False
    checkpoint.update(
        {
            "accepted": True,
            "run_id": active_run.run_id,
            "observation": observation.to_dict(),
            "already_checkpointed": already_checkpointed,
            "shadow": updated.to_dict(),
        }
    )
    return checkpoint


def checkpoint_live_activity_reward(
    *,
    result: Mapping[str, Any] | None = None,
    produce_id: str | None = None,
    character_id: str | None = None,
    idol_card_id: str | None = None,
) -> Mapping[str, Any]:
    """Persist one verified activity reward into the run shadow.

    ``result`` lets the production outer loop checkpoint the exact capture it
    already classified.  Omitting it preserves the standalone capture/read
    behavior used by older callers.
    """

    from .activity_reward import ActivityRewardState
    from .run_shadow import (
        RunShadowState,
        activity_reward_observation,
        load_run_shadow,
        save_run_shadow,
    )
    from .run_identity import load_active_run, paths_for

    active_run = load_active_run()
    if active_run is None:
        raise ValueError("no active run identity; refuse to checkpoint reward")
    expected = {
        "produce_id": active_run.produce_id,
        "character_id": active_run.character_id,
        "idol_card_id": active_run.idol_card_id,
    }
    supplied = {
        "produce_id": produce_id,
        "character_id": character_id,
        "idol_card_id": idol_card_id,
    }
    mismatches = [
        name
        for name, value in supplied.items()
        if value is not None and value != expected[name]
    ]
    if mismatches:
        raise ValueError(
            "activity reward identity differs from active run: "
            + ", ".join(mismatches)
        )
    produce_id = active_run.produce_id
    character_id = active_run.character_id
    idol_card_id = active_run.idol_card_id
    run_paths = paths_for(active_run)

    checkpoint_result = dict(
        read_live_activity_reward() if result is None else result
    )
    capture = checkpoint_result.get("capture")
    raw_state = checkpoint_result.get("state")
    if not isinstance(capture, Mapping) or not isinstance(raw_state, Mapping):
        raise ValueError("live activity reward result is malformed")
    png_path = capture.get("png_path")
    timestamp = capture.get("timestamp")
    if not isinstance(png_path, str) or not png_path or not Path(png_path).is_file():
        raise ValueError("activity reward is missing screenshot evidence")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise ValueError("activity reward is missing capture timestamp")
    raw_text = raw_state.get("raw_text")
    if not isinstance(raw_text, Mapping):
        raise ValueError("activity reward state is missing raw OCR evidence")
    reward = ActivityRewardState(
        item_name=str(raw_state["item_name"]),
        effect_name=str(raw_state["effect_name"]),
        effect_value=int(raw_state["effect_value"]),
        stamina=int(raw_state["stamina"]),
        max_stamina=int(raw_state["max_stamina"]),
        produce_points=int(raw_state["produce_points"]),
        confidence=float(raw_state["confidence"]),
        raw_text=dict(raw_text),
    )
    shadow = load_run_shadow(run_paths.shadow) or RunShadowState(
        produce_id=produce_id,
        character_id=character_id,
        idol_card_id=idol_card_id,
    )
    if (
        shadow.produce_id != produce_id
        or shadow.character_id != character_id
        or shadow.idol_card_id != idol_card_id
    ):
        raise ValueError("existing run shadow belongs to a different run identity")
    observation = activity_reward_observation(
        reward,
        captured_at=float(timestamp),
        evidence_path=png_path,
    )
    matching = tuple(
        value
        for value in shadow.observations
        if value.kind == "activity_reward" and value.evidence_path == png_path
    )
    if matching:
        if len(matching) != 1 or matching[0] != observation:
            raise ValueError(
                "activity reward evidence was already checkpointed with different facts"
            )
        updated = shadow
        already_checkpointed = True
    else:
        updated = shadow.apply(observation)
        save_run_shadow(updated, run_paths.shadow)
        already_checkpointed = False
    checkpoint_result.update(
        {
            "accepted": True,
            "run_id": active_run.run_id,
            "observation": observation.to_dict(),
            "already_checkpointed": already_checkpointed,
            "shadow": updated.to_dict(),
        }
    )
    return checkpoint_result


def checkpoint_confirmed_card_reward(
    *,
    card_id: str,
    upgrade: int,
    display_name: str,
    confidence: float,
    capture: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Persist a reward after LocalSave proves the card-add transaction.

    The post-click screenshot is retained as UI evidence, while the outer
    LocalSave is authoritative for the received card and any support passive
    attribute changes.  This intentionally observes settlement instead of
    requiring the route planner to predict it.
    """

    from .run_identity import load_active_run, paths_for
    from .run_shadow import (
        RunShadowState,
        card_reward_observation,
        load_run_shadow,
        save_run_shadow,
    )

    active_run = load_active_run()
    if active_run is None:
        raise ValueError("no active run identity; refuse to checkpoint card reward")
    png_path = capture.get("png_path")
    timestamp = capture.get("timestamp")
    if not isinstance(png_path, str) or not png_path or not Path(png_path).is_file():
        raise ValueError("confirmed card reward is missing screenshot evidence")
    if not isinstance(timestamp, (int, float)):
        raise ValueError("confirmed card reward is missing capture timestamp")
    from .produce_outer_local_save import read_current_produce_outer_local_save

    snapshot = read_current_produce_outer_local_save()
    matching_events = tuple(
        line
        for line in snapshot.inventory_events
        if line.line_type == 15 and line.target_id == card_id
    )
    if not matching_events:
        raise ValueError("outer LocalSave does not confirm the received card")
    card_event = max(
        matching_events,
        key=lambda line: (line.log_index, line.detail_index, line.line_index),
    )
    step_matches = tuple(
        step for step in snapshot.completed_steps if step.log_index == card_event.log_index
    )
    if len(step_matches) != 1:
        raise ValueError("received card does not map to one completed outer step")
    attribute_by_line_type = {6: "vocal", 7: "dance", 8: "visual"}
    settled_attributes: dict[str, int] = {}
    confirmed_passives: list[dict[str, object]] = []
    for line in step_matches[0].lines:
        if (
            not line.is_triggered
            or line.line_type not in attribute_by_line_type
            or (line.detail_index, line.line_index)
            <= (card_event.detail_index, card_event.line_index)
        ):
            continue
        if not line.before.is_integer() or not line.after.is_integer():
            raise ValueError("card reward passive attribute delta is not integral")
        attribute = attribute_by_line_type[line.line_type]
        settled_attributes[attribute] = int(line.after)
        confirmed_passives.append(
            {
                "attribute": attribute,
                "before": int(line.before),
                "after": int(line.after),
                "delta": int(line.delta),
                "source_id": line.trigger_owner_id or line.trigger_id or None,
                "support_card_id": line.trigger_owner_id or None,
                "trigger_id": line.trigger_id,
                "outer_line": [line.log_index, line.detail_index, line.line_index],
            }
        )
    run_paths = paths_for(active_run)
    shadow = load_run_shadow(run_paths.shadow) or RunShadowState(
        produce_id=active_run.produce_id,
        character_id=active_run.character_id,
        idol_card_id=active_run.idol_card_id,
    )
    if (
        shadow.produce_id != active_run.produce_id
        or shadow.character_id != active_run.character_id
        or shadow.idol_card_id != active_run.idol_card_id
    ):
        raise ValueError("existing run shadow belongs to a different run identity")
    observation = card_reward_observation(
        card_id=card_id,
        upgrade=int(upgrade),
        display_name=display_name,
        captured_at=float(timestamp),
        evidence_path=png_path,
        confidence=float(confidence),
        settled_attributes=settled_attributes,
        confirmed_passives=tuple(confirmed_passives),
    )
    existing_matches = tuple(
        value
        for value in shadow.observations
        if value.kind == "card_reward"
        and value.metadata.get("card_id") == card_id
        and value.evidence_path == png_path
    )
    if existing_matches:
        if len(existing_matches) != 1:
            raise ValueError("confirmed card reward has duplicate checkpoints")
        existing = existing_matches[0]
        if existing == observation:
            updated = shadow
        elif (
            not existing.values
            and not existing.metadata.get("confirmed_passives")
            and existing.deck_delta == observation.deck_delta
        ):
            # A click capture can precede the asynchronous LocalSave passive
            # rows by a fraction of a second.  Upgrade that single checkpoint
            # in place once the authoritative settlement appears; never append
            # a second copy of the received card.
            index = shadow.observations.index(existing)
            replaced = (*shadow.observations[:index], observation, *shadow.observations[index + 1 :])
            updated = replace(
                shadow,
                **dict(observation.values),
                observations=replaced,
            )
            updated.validate()
        else:
            raise ValueError("confirmed card reward checkpoint differs from LocalSave")
    else:
        updated = shadow.apply(observation)
    save_run_shadow(updated, run_paths.shadow)
    return {
        "run_id": active_run.run_id,
        "observation": observation.to_dict(),
        "shadow": updated.to_dict(),
    }


def checkpoint_live_outer_state_rebase(
    *,
    capture: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Persist currently serialized outer totals without predicting effects."""

    from .produce_outer_local_save import read_current_produce_outer_local_save
    from .route_calendar import load_route_calendar
    from .run_identity import load_active_run, paths_for
    from .run_shadow import (
        load_run_shadow,
        outer_state_rebase_observation,
        save_run_shadow,
    )

    active_run = load_active_run()
    if active_run is None:
        raise ValueError("no active run identity; refuse outer-state rebase")
    current_capture = dict(capture_once() if capture is None else capture)
    png_path = current_capture.get("png_path")
    timestamp = current_capture.get("timestamp")
    if not isinstance(png_path, str) or not Path(png_path).is_file():
        raise ValueError("outer-state rebase screenshot evidence is missing")
    if not isinstance(timestamp, (int, float)) or isinstance(timestamp, bool):
        raise ValueError("outer-state rebase capture timestamp is missing")
    snapshot = read_current_produce_outer_local_save()
    values = {
        name: value
        for name, value in (
            ("stamina", snapshot.stamina),
            ("max_stamina", snapshot.max_stamina),
            ("produce_points", snapshot.produce_points),
            ("vocal", snapshot.vocal),
            ("dance", snapshot.dance),
            ("visual", snapshot.visual),
        )
        if value is not None
    }
    latest_week = snapshot.latest_week_marker
    completed_week = snapshot.last_completed_week
    if latest_week is not None:
        if (
            not isinstance(latest_week, int)
            or isinstance(latest_week, bool)
            or latest_week < 1
        ):
            raise ValueError("outer latest week marker is invalid")
        if completed_week is not None and (
            not isinstance(completed_week, int)
            or isinstance(completed_week, bool)
            or completed_week < 1
            or completed_week > latest_week
        ):
            raise ValueError("outer completed week marker is invalid")
        current_week = latest_week + 1 if completed_week == latest_week else latest_week
        calendar = load_route_calendar(
            active_run.produce_id,
            character_id=active_run.character_id,
        )
        if current_week <= calendar.total_weeks:
            values["route_week"] = current_week
            values["weeks_remaining"] = calendar.total_weeks - current_week
    observation = outer_state_rebase_observation(
        captured_at=float(timestamp),
        evidence_path=png_path,
        values=values,
        authority={
            "kind": "outer-local-save-observed-totals",
            "log_count": snapshot.log_count,
            "latest_week_marker": snapshot.latest_week_marker,
            "last_completed_week": snapshot.last_completed_week,
        },
    )
    run_paths = paths_for(active_run)
    shadow = load_run_shadow(run_paths.shadow)
    if shadow is None:
        raise ValueError("run shadow is missing for outer-state rebase")
    updated = shadow.apply(observation)
    save_run_shadow(updated, run_paths.shadow)
    return {
        "run_id": active_run.run_id,
        "observation": observation.to_dict(),
        "shadow": updated.to_dict(),
    }


def read_live_overview_decision(
    *,
    idol_card_id: str,
    produce_id: str,
) -> Mapping[str, Any]:
    """Read the weekly overview and bind one explainable visible action.

    Initial Regular retains its OCR-backed route reader.  N.I.A. normally uses
    Produce LocalSave for week/stamina/parameters and Maa templates for visible
    action identity and coordinates.  A brand-new N.I.A. save can reach its
    first overview before those numeric rows have been logged; only for that
    missing-value boundary the same fixed HUD OCR supplies the opening values.
    Once LocalSave has values they remain authoritative, especially in the
    2000/2600 parameter domains.
    """

    from PIL import Image

    from .live_actions import SuggestedClick
    from .master_db import list_idol_profiles
    from .overview_actions import (
        ACTIVITY,
        CLASS,
        CONSULTATION,
        DANCE_LESSON,
        OUTING,
        REST,
        VOCAL_LESSON,
        VISUAL_LESSON,
        WeeklyActionDecision,
        detect_nia_weekly_actions,
        detect_weekly_actions,
        recommend_weekly_action,
    )
    from .regular_route_planner import (
        load_week_one_regular_lesson_gain_signal,
        rank_regular_route_candidates,
    )
    from .initial_regular_outer_advisor import load_initial_regular_mode_rules
    from .route_calendar import (
        BUSINESS,
        SELF_LESSON,
        SPECIAL_GUIDANCE,
        RoutePosition,
        RouteWeek,
        infer_position_from_final_countdown,
        infer_position_from_milestone_countdown,
        load_route_calendar,
    )
    from .screen_state import OverviewState, ParameterTemporalEvidence, read_overview_path

    def read_missing_stamina_values() -> tuple[int, int]:
        """Read only the HUD stamina pair when ProduceLocalSave omits it.

        Initial Pro/Master play-log rows often retain the settled attributes
        and P points while omitting ``maxStamina``.  Re-OCRing all three
        attributes in that case is both redundant and brittle because the
        coloured attribute icon can be joined to a four-digit OCR token.  The
        fixed stamina crop is the only missing authority needed here.
        """

        from .screen_state import OVERVIEW_REGIONS, scale_canonical_box
        from .text_recognizer import PaddleLineRecognizer

        with Image.open(path.resolve()) as image:
            image.load()
            stamina_crop = image.crop(
                scale_canonical_box(
                    OVERVIEW_REGIONS["stamina"], image.width, image.height
                )
            )
        recognized = PaddleLineRecognizer().recognize(stamina_crop)
        match = re.search(r"(\d+)\s*[/／]\s*(\d+)", recognized.text)
        if match is None:
            raise ValueError(
                f"stamina OCR is not current/max: {recognized.text!r}"
            )
        current, maximum = map(int, match.groups())
        if maximum < 1 or current > maximum:
            raise ValueError(f"invalid stamina values: {current}/{maximum}")
        return current, maximum

    nia_recognition: Mapping[str, Any] | None = None
    if produce_id in {"produce-004", "produce-005"}:
        from .controller_client import send_command

        nia_recognition = dict(
            send_command(
                "recognize_nia_outer_once", timeout=20.0, scope="overview"
            )
        )
        raw_capture = nia_recognition.get("capture")
        if not isinstance(raw_capture, Mapping):
            raise ValueError("Maa N.I.A. overview batch has no capture authority")
        capture = dict(raw_capture)
    else:
        capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    path = Path(png_path)
    profile = next(
        (item for item in list_idol_profiles() if item.id == idol_card_id),
        None,
    )
    calendar = load_route_calendar(
        produce_id,
        character_id=None if profile is None else profile.character_id,
    )
    if produce_id in {"produce-004", "produce-005"}:
        from .nia_outer_advisor import (
            DEFAULT_NIA_LIVE_POLICY,
            STATUS_READY as NIA_READY,
            advise_nia_outer,
        )
        from .produce_outer_local_save import (
            read_current_produce_outer_local_save,
        )
        from .screen_state import OverviewState

        snapshot = read_current_produce_outer_local_save()

        latest_week = snapshot.latest_week_marker
        if not isinstance(latest_week, int) or isinstance(latest_week, bool):
            raise ValueError("N.I.A. Produce LocalSave is missing the current week")
        completed_week = snapshot.last_completed_week
        if completed_week is not None and (
            not isinstance(completed_week, int)
            or isinstance(completed_week, bool)
            or completed_week > latest_week
        ):
            raise ValueError("N.I.A. Produce LocalSave has an invalid completed week")
        # Between weekly rows the game already displays the following
        # overview while the latest serialized Week marker still names the
        # step just completed.  This is the same outer-save lifecycle used by
        # Initial Regular: completed==latest means current=latest+1.
        week = latest_week + 1 if completed_week == latest_week else latest_week
        from .nia_week_gate import nia_week_gate

        fixed_gate = nia_week_gate(produce_id, week)
        route_week = RouteWeek(
            week=week,
            actions=fixed_gate.actions,
            stage_type=fixed_gate.exam_stage,
            exact_actions=True,
        )
        next_milestone = calendar.next_milestone(week)
        if next_milestone is None:
            raise ValueError("N.I.A. route has no milestone at or after this week")
        numeric_fields = (
            "stamina",
            "max_stamina",
            "produce_points",
            "vocal",
            "dance",
            "visual",
        )
        numeric_values: dict[str, int | None] = {
            field: (
                int(value)
                if isinstance((value := getattr(snapshot, field)), int)
                and not isinstance(value, bool)
                else None
            )
            for field in numeric_fields
        }
        shadow_fields: list[str] = []
        if any(value is None for value in numeric_values.values()):
            # ProducePlayLogSaveData commonly omits unchanged values (most
            # notably max stamina) after the opening week.  The active run
            # shadow already owns those last observed totals.  Reuse it only
            # when its run identity and effective week match this exact save;
            # do not reject an otherwise Maa-recognized overview by OCRing all
            # three parameters again merely to recover one omitted field.
            try:
                from .run_identity import load_active_run, paths_for
                from .run_shadow import load_run_shadow

                active_run = load_active_run()
                shadow = (
                    None
                    if active_run is None
                    or active_run.produce_id != produce_id
                    or active_run.idol_card_id != idol_card_id
                    else load_run_shadow(paths_for(active_run).shadow)
                )
                if shadow is not None and shadow.route_week == week:
                    for field, value in tuple(numeric_values.items()):
                        shadow_value = getattr(shadow, field, None)
                        if (
                            value is None
                            and isinstance(shadow_value, int)
                            and not isinstance(shadow_value, bool)
                        ):
                            numeric_values[field] = int(shadow_value)
                            shadow_fields.append(field)
            except (FileNotFoundError, TypeError, ValueError):
                # The opening-overview HUD fallback below remains available
                # when no exact active-run shadow exists.
                pass
        local_numeric_complete = all(
            value is not None for value in numeric_values.values()
        )
        if local_numeric_complete:
            state = OverviewState(
                weeks_remaining=max(0, next_milestone.week - week),
                stamina=int(numeric_values["stamina"]),
                max_stamina=int(numeric_values["max_stamina"]),
                produce_points=int(numeric_values["produce_points"]),
                vocal=int(numeric_values["vocal"]),
                dance=int(numeric_values["dance"]),
                visual=int(numeric_values["visual"]),
                confidence=1.0,
                raw_text={
                    "authority": (
                        "ProduceLocalSave+run-shadow"
                        if shadow_fields
                        else "ProduceLocalSave"
                    ),
                    "source": "outer-play-log",
                },
                countdown_target=next_milestone.step_type,
            )
            state_authority = (
                "ProduceLocalSave+run-shadow:"
                + ",".join(sorted(shadow_fields))
                if shadow_fields
                else "ProduceLocalSave"
            )
        else:
            # Opening notifications can precede the first numeric play-log
            # rows.  The presence of a complete Maa weekly-action batch proves
            # that this is the overview; read its fixed HUD fields once rather
            # than letting the story Click_1 fallback keep advancing it.
            state = read_overview_path(path, produce_id=produce_id)
            state_authority = "overview-hud-ocr-opening-fallback"
        from .nia_live_outer import nia_weekly_actions_from_maa_recognition

        assert nia_recognition is not None
        try:
            options = nia_weekly_actions_from_maa_recognition(nia_recognition)
        except ValueError:
            if not route_week.exact_actions:
                raise
            options = ()
        visible_actions = frozenset(option.action for option in options)
        completed_route_week = None
        if completed_week is not None:
            completed_gate = nia_week_gate(produce_id, completed_week)
            completed_route_week = RouteWeek(
                week=completed_week,
                actions=completed_gate.actions,
                stage_type=completed_gate.exam_stage,
                exact_actions=True,
            )
        just_completed_milestone_empty_surface = bool(
            completed_week == latest_week
            and completed_route_week is not None
            and completed_route_week.stage_type is not None
            and not options
        )
        if just_completed_milestone_empty_surface:
            # After an audition is durably completed, its bonus/result TAP
            # pages carry no weekly-action node.  This is not an upcoming
            # milestone transition and must not enter either exact-route or
            # milestone no-input waiting.  All named N.I.A. subpages already
            # had first refusal; rejecting the false overview here delegates
            # the empty result frame to Maa's existing bounded Click_1 graph.
            raise ValueError(
                "completed N.I.A. milestone empty result is not an overview"
            )
        direct_route_actions = frozenset(
            (
                ACTIVITY,
                BUSINESS,
                CONSULTATION,
                OUTING,
                REST,
                SPECIAL_GUIDANCE,
            )
        )
        expected_direct_actions = tuple(
            action
            for action in route_week.actions
            if action in direct_route_actions
            and not (
                action == REST
                and state.max_stamina > 0
                and state.stamina >= state.max_stamina
            )
        )
        missing_direct_actions = tuple(
            action
            for action in expected_direct_actions
            if action not in visible_actions
        )
        lesson_actions = frozenset(
            (VOCAL_LESSON, DANCE_LESSON, VISUAL_LESSON)
        )
        self_lesson_expected = bool(
            set(route_week.actions) & lesson_actions
        )
        self_lesson_visible = bool(visible_actions & lesson_actions)
        exact_route_visibility_required = bool(
            route_week.exact_actions
            and (expected_direct_actions or self_lesson_expected)
        )
        exact_route_visibility_complete = bool(
            not missing_direct_actions
            and (not self_lesson_expected or self_lesson_visible)
        )
        route_payload = {
            "produce_id": calendar.produce_id,
            "character_id": None if profile is None else profile.character_id,
            "current_week": week,
            "total_weeks": calendar.total_weeks,
            "route_actions": list(route_week.actions),
            "route_actions_exact": route_week.exact_actions,
            "route_week": route_week.display,
            "stage_type": route_week.stage_type,
            "next_milestone": next_milestone.label,
            "weeks_until_next_milestone": next_milestone.week - week,
            "lesson_gain_source": "ProduceStepSelfLesson/ProducePlayLog",
            "exact_route_visibility_required": exact_route_visibility_required,
            "expected_visible_actions": list(expected_direct_actions),
            "self_lesson_visibility_required": self_lesson_expected,
            "missing_visible_actions": [
                *missing_direct_actions,
                *(
                    [SELF_LESSON]
                    if self_lesson_expected and not self_lesson_visible
                    else []
                ),
            ],
        }
        outer_authority = {
            "kind": "produce-local-save-before-maa-input",
            "produce_id": produce_id,
            "log_count": snapshot.log_count,
            "week": week,
            "last_completed_week": snapshot.last_completed_week,
            "stamina": snapshot.stamina,
            "max_stamina": snapshot.max_stamina,
            "produce_points": snapshot.produce_points,
            "vocal": snapshot.vocal,
            "dance": snapshot.dance,
            "visual": snapshot.visual,
            "vote_count": snapshot.vote_count,
            "state_authority": state_authority,
        }
        payload: dict[str, Any] = {
            "capture": dict(capture),
            "state": state.to_dict(),
            "route": route_payload,
            "options": [option.to_dict() for option in options],
            "outer_authority": outer_authority,
            "plan_type": (
                "ProducePlanType_Plan3"
                if profile is None
                else profile.plan_type
            ),
        }
        upcoming_milestone = bool(
            route_week.stage_type is not None
            and (completed_week is None or completed_week < week)
        )
        if upcoming_milestone:
            # An audition boundary has no weekly outer action.  During the
            # transition into that boundary, the previous special-guidance
            # animation can still resemble an overview while none of Maa's
            # named guide/card-operation/mirror/briefing nodes is ready.  The
            # N.I.A. subpage reader already had first refusal for this exact
            # capture; keep polling it under the runner's LocalSave-bound wait
            # budget instead of asking the weekly advisor to decide an
            # impossible milestone action (fresh-v5 week 18).
            payload.update(
                {
                    "page_override": "nia-outer-subpage",
                    "kind": "wait",
                    "target": "milestone-subpage",
                    "wait_timeout_reason": (
                        "nia-milestone-surface-not-visible"
                    ),
                    "decision": {
                        "status": "waiting",
                        "reason": "nia-milestone-surface-not-visible",
                        "action": None,
                    },
                    "action": None,
                    "evidence": {
                        "policy": (
                            "route-milestone-defers-to-named-nia-subpage-v1"
                        ),
                        "stage_type": route_week.stage_type,
                        "visible_weekly_actions": sorted(visible_actions),
                    },
                }
            )
            return payload
        if (
            exact_route_visibility_required
            and not exact_route_visibility_complete
            and not options
        ):
            # An exact weekly route can wait on a *partial* Maa batch (for
            # example Rest is visible while Guide is still scaling in).  An
            # empty batch proves no overview action at all and therefore
            # cannot own the screen ahead of the established N.I.A. subpage /
            # Click_1 continuation graph.  This is what a received-card
            # detail overlay exposes in unattended-v1 week 5.
            raise ValueError(
                "empty N.I.A. weekly-action batch is not an overview"
            )
        if (
            exact_route_visibility_required
            and not exact_route_visibility_complete
        ):
            # N.I.A. weekly tiles scale during their entrance/idle animation.
            # One Maa batch can therefore contain only Rest (fresh-v4 missed
            # Guide twice) or only part of a mixed exact route.  Return a
            # typed no-input wait and let the outer runner's existing
            # LocalSave-bound 60-read budget obtain a fresh Maa batch.  The
            # advisor does not rank a partial candidate set, and the generic
            # Click_1 fallback never owns this recognized overview.
            payload.update(
                {
                    "page_override": "nia-outer-subpage",
                    "kind": "wait",
                    "target": "expected-route-actions",
                    "wait_timeout_reason": (
                        "expected-route-action-not-visible"
                    ),
                    "decision": {
                        "status": "waiting",
                        "reason": "expected-route-action-not-visible",
                        "action": None,
                    },
                    "action": None,
                    "evidence": {
                        "policy": (
                            "route-exact-weekly-actions-visible-before-advice-v1"
                        ),
                        "visible_actions": sorted(visible_actions),
                        "expected_direct_actions": list(
                            expected_direct_actions
                        ),
                        "self_lesson_any_of": (
                            sorted(lesson_actions)
                            if self_lesson_expected
                            else []
                        ),
                        "missing_visible_actions": list(
                            route_payload["missing_visible_actions"]
                        ),
                    },
                }
            )
            return payload
        advice = advise_nia_outer(
            snapshot,
            overview_output=payload,
            produce_id=produce_id,
            policy=DEFAULT_NIA_LIVE_POLICY,
        )
        payload["decision"] = advice.to_dict()
        payload["action"] = None
        if advice.status == NIA_READY:
            matches = [option for option in options if option.action == advice.action]
            if len(matches) != 1:
                raise ValueError(
                    "N.I.A. advisor did not identify one visible Maa action tile"
                )
            option = matches[0]
            center_x, center_y = option.center
            payload["action"] = SuggestedClick(
                label=f"nia-overview:{option.action}",
                canonical_x=center_x,
                canonical_y=center_y,
                source_png_path=png_path,
                source_timestamp=float(capture["timestamp"]),
                source_hwnd=int(capture["hwnd"]),
                source_pid=int(capture["pid"]),
                verification_box=option.canonical_box,
                click_count=1 if option.action == REST else 2,
            ).to_dict()
        return payload

    # Initial Pro/Master parameters can exceed the three-digit Regular HUD.
    # Their live outer save already owns the settled totals and week marker, so
    # use it directly instead of predicting lesson gains or forcing OCR to
    # reproduce all three values.  OCR remains the opening fallback before the
    # first numeric play-log row exists.
    position: RoutePosition
    if produce_id in {"produce-002", "produce-003"}:
        from .produce_outer_local_save import read_current_produce_outer_local_save

        snapshot = read_current_produce_outer_local_save()
        latest = snapshot.latest_week_marker
        completed = snapshot.last_completed_week
        current_week = (
            latest + 1
            if isinstance(latest, int)
            and not isinstance(latest, bool)
            and completed == latest
            and latest < calendar.total_weeks
            else latest
        )
        numeric_values = {
            "stamina": snapshot.stamina,
            "max_stamina": snapshot.max_stamina,
            "produce_points": snapshot.produce_points,
            "vocal": snapshot.vocal,
            "dance": snapshot.dance,
            "visual": snapshot.visual,
        }
        numeric_authority = "ProduceLocalSave"
        known_without_stamina = all(
            isinstance(numeric_values[field], int)
            and not isinstance(numeric_values[field], bool)
            for field in ("produce_points", "vocal", "dance", "visual")
        )
        if known_without_stamina and (
            numeric_values["stamina"] is None
            or numeric_values["max_stamina"] is None
        ):
            hud_stamina, hud_max_stamina = read_missing_stamina_values()
            local_stamina = numeric_values["stamina"]
            if (
                isinstance(local_stamina, int)
                and not isinstance(local_stamina, bool)
                and local_stamina != hud_stamina
            ):
                raise ValueError(
                    "overview stamina HUD differs from ProduceLocalSave"
                )
            if numeric_values["stamina"] is None:
                numeric_values["stamina"] = hud_stamina
            if numeric_values["max_stamina"] is None:
                numeric_values["max_stamina"] = hud_max_stamina
            numeric_authority = "ProduceLocalSave+overview-stamina-HUD"
        if (
            isinstance(current_week, int)
            and not isinstance(current_week, bool)
            and 1 <= current_week <= calendar.total_weeks
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in numeric_values.values()
            )
        ):
            next_milestone = calendar.next_milestone(current_week)
            if next_milestone is None:
                raise ValueError("Initial Pro/Master route has no remaining milestone")
            state = OverviewState(
                weeks_remaining=next_milestone.week - current_week,
                stamina=int(numeric_values["stamina"]),
                max_stamina=int(numeric_values["max_stamina"]),
                produce_points=int(numeric_values["produce_points"]),
                vocal=int(numeric_values["vocal"]),
                dance=int(numeric_values["dance"]),
                visual=int(numeric_values["visual"]),
                confidence=1.0,
                raw_text={"authority": numeric_authority},
                countdown_target=next_milestone.step_type,
            )
            position = RoutePosition(
                current_week=current_week,
                weeks_until_final=calendar.final_week - current_week,
                route_week=calendar.week(current_week),
                next_milestone=next_milestone,
            )
        else:
            temporal = None
            attributes = (snapshot.vocal, snapshot.dance, snapshot.visual)
            if all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in attributes
            ):
                temporal = ParameterTemporalEvidence(
                    before={
                        "vocal": int(snapshot.vocal),
                        "dance": int(snapshot.dance),
                        "visual": int(snapshot.visual),
                    },
                    expected_deltas={"vocal": 0, "dance": 0, "visual": 0},
                )
            state = read_overview_path(
                path,
                produce_id=produce_id,
                parameter_temporal_evidence=temporal,
            )
            position = (
                infer_position_from_milestone_countdown(
                    calendar,
                    state.weeks_remaining,
                    state.countdown_target,
                )
                if state.countdown_target != "unknown"
                else infer_position_from_final_countdown(
                    calendar, state.weeks_remaining
                )
            )
    else:
        state = read_overview_path(path, produce_id=produce_id)
        position = (
            infer_position_from_milestone_countdown(
                calendar,
                state.weeks_remaining,
                state.countdown_target,
            )
            if state.countdown_target != "unknown"
            else infer_position_from_final_countdown(calendar, state.weeks_remaining)
        )
    with Image.open(path.resolve()) as image:
        image.load()
        options = detect_weekly_actions(image)
    route_horizon = calendar.weeks[position.current_week - 1 : calendar.final_week]
    lesson_gain_signal = None
    if produce_id == "produce-001" and position.current_week == 1 and profile is not None:
        try:
            growth = _active_run_effective_growth_permils(
                idol_card_id=idol_card_id,
                produce_id=produce_id,
            )
            if growth is not None:
                vocal_growth, dance_growth, visual_growth, source = growth
                lesson_gain_signal = load_week_one_regular_lesson_gain_signal(
                    character_id=profile.character_id,
                    vocal_growth_permil=vocal_growth,
                    dance_growth_permil=dance_growth,
                    visual_growth_permil=visual_growth,
                    source=source,
                )
        except (FileNotFoundError, TypeError, ValueError):
            # This projection is optional evidence.  The route planner keeps
            # its legacy raw-deficit policy and labels that downgrade when a
            # complete exact W1 join cannot be built.
            lesson_gain_signal = None
    # The outer route planner is deliberately limited to Initial Regular.  It
    # ranks only tiles proven visible in this capture and does not fabricate a
    # deck before the owned-card panel has been observed.  Any incomplete
    # static/calendar condition falls back to the established visible-options
    # policy rather than producing an unsupported click.
    if produce_id == "produce-001" and position.route_week.exact_actions:
        try:
            route_ranking = rank_regular_route_candidates(
                state,
                options,
                position.route_week,
                route_horizon=route_horizon,
                lesson_gain_signal=lesson_gain_signal,
                attribute_cap=load_initial_regular_mode_rules().attribute_cap,
            )
            decision = WeeklyActionDecision(
                route_ranking.recommended.option,
                route_ranking.recommended.reasons,
                (
                    "regular-route-observe-after-settlement-v3"
                    if lesson_gain_signal is not None
                    else "regular-route-ranking-v1"
                ),
                route_ranking.stamina_plan,
            )
        except (TypeError, ValueError):
            decision = recommend_weekly_action(
                state,
                options,
                position.route_week,
                skip_consultation=True,
                route_horizon=route_horizon,
            )
    else:
        # Initial Pro/Master schedules are supplied by the live server and do
        # not have a proven static week table in this repository.  Select only
        # among the current visible tiles: protect critical stamina, otherwise
        # prefer SP and the attribute with the most remaining Master cap.
        by_action = {value.action: value for value in options}
        mode_rules = load_initial_regular_mode_rules(produce_id)
        lesson_values = {
            VOCAL_LESSON: state.vocal,
            DANCE_LESSON: state.dance,
            VISUAL_LESSON: state.visual,
        }
        visible_lessons = tuple(
            (value, by_action[action])
            for action, value in lesson_values.items()
            if action in by_action and value < mode_rules.attribute_cap
        )
        lesson_cost = max(8, math.ceil(state.max_stamina * 0.25))
        emergency_floor = max(3, math.ceil(state.max_stamina * 0.08))
        if (
            state.stamina < lesson_cost + emergency_floor
            and OUTING in by_action
        ):
            selected = by_action[OUTING]
            reasons = (
                f"Visible-action {produce_id}: use productive recovery at "
                f"{state.stamina}/{state.max_stamina}; Rest is the last resort.",
            )
        elif (
            state.stamina < lesson_cost + emergency_floor
            and REST in by_action
        ):
            selected = by_action[REST]
            reasons = (
                f"Visible-action {produce_id}: recover at {state.stamina}/"
                f"{state.max_stamina}; no productive recovery is visible.",
            )
        elif visible_lessons:
            _current, selected = max(
                visible_lessons,
                key=lambda item: (
                    item[1].is_sp,
                    mode_rules.attribute_cap - item[0],
                    -item[0],
                ),
            )
            reasons = (
                f"Visible-action {produce_id}: prefer "
                f"{'SP ' if selected.is_sp else ''}{selected.action} and "
                "fill the selected attribute toward its Master cap.",
            )
        else:
            # A server-driven Pro/Master week may expose a single legal tile
            # that is not a lesson (for example Consultation).  The template
            # detector has already established that tile's identity and exact
            # click box, so do not lose that evidence through a second,
            # incomplete action allow-list.  When several non-lesson choices
            # are visible, retain the established productive-action ordering
            # and leave Rest last.
            if len(by_action) == 1:
                selected = next(iter(by_action.values()))
            else:
                selected = next(
                    (
                        by_action[action]
                        for action in (
                            ACTIVITY,
                            CLASS,
                            OUTING,
                            CONSULTATION,
                            REST,
                        )
                        if action in by_action
                    ),
                    None,
                )
            if selected is None:
                raise ValueError("no usable Initial Pro/Master action is visible")
            reasons = (f"Visible-action {produce_id}: choose {selected.action}.",)
        decision = WeeklyActionDecision(
            selected,
            reasons,
            "initial-visible-actions-v2",
        )
    option = decision.recommended
    center_x, center_y = option.center
    action = SuggestedClick(
        label=f"本週行動：{option.label}",
        canonical_x=center_x,
        canonical_y=center_y,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=option.canonical_box,
        click_count=1 if option.action == REST else 2,
    )
    # Bind every Initial weekly choice to one immutable Produce snapshot.
    # The run loop consumes this as a transaction key: a still-visible old
    # overview may be observed repeatedly during animation, but it can submit
    # at most one input until LocalSave advances or a real subpage appears.
    from .produce_outer_local_save import read_current_produce_outer_local_save

    overview_snapshot = read_current_produce_outer_local_save()
    outer_authority = {
        "kind": "produce-local-save-before-weekly-input",
        "produce_id": produce_id,
        "log_count": overview_snapshot.log_count,
        "week": position.current_week,
        "last_completed_week": overview_snapshot.last_completed_week,
        "stamina": overview_snapshot.stamina,
        "max_stamina": overview_snapshot.max_stamina,
        "produce_points": overview_snapshot.produce_points,
        "vocal": overview_snapshot.vocal,
        "dance": overview_snapshot.dance,
        "visual": overview_snapshot.visual,
        "vote_count": overview_snapshot.vote_count,
    }
    return {
        "capture": dict(capture),
        "state": state.to_dict(),
        "route": {
            "produce_id": calendar.produce_id,
            "character_id": None if profile is None else profile.character_id,
            "current_week": position.current_week,
            "total_weeks": calendar.total_weeks,
            "route_actions": list(position.route_week.actions),
            "route_actions_exact": position.route_week.exact_actions,
            "route_week": position.route_week.display,
            "next_milestone": position.next_milestone.label,
            "weeks_until_next_milestone": position.weeks_until_next_milestone,
            "lesson_gain_source": (
                None if lesson_gain_signal is None else lesson_gain_signal.source
            ),
        },
        "options": [option.to_dict() for option in options],
        "decision": decision.to_dict(),
        "action": action.to_dict(),
        "outer_authority": outer_authority,
    }


def _training_overview_context() -> Mapping[str, Any] | None:
    """Rebuild structural week context when training is the first observed page."""

    from .produce_outer_local_save import read_current_produce_outer_local_save
    from .route_calendar import load_route_calendar
    from .run_identity import load_active_run, paths_for
    from .run_shadow import load_run_shadow

    active = load_active_run()
    if active is None:
        return None
    snapshot = read_current_produce_outer_local_save()
    shadow = load_run_shadow(paths_for(active).shadow)
    calendar = load_route_calendar(active.produce_id, character_id=active.character_id)
    latest = snapshot.latest_week_marker
    completed = snapshot.last_completed_week
    current_week = latest + 1 if latest is not None and completed == latest else latest
    if current_week is None:
        return None
    route_week = calendar.week(current_week)

    def current_value(field: str) -> int | None:
        local = getattr(snapshot, field)
        if isinstance(local, int) and not isinstance(local, bool):
            return int(local)
        if shadow is not None:
            previous = getattr(shadow, field, None)
            if isinstance(previous, int) and not isinstance(previous, bool):
                return int(previous)
        return None

    return {
        "state": {
            "stamina": current_value("stamina"),
            "max_stamina": current_value("max_stamina"),
            "produce_points": current_value("produce_points"),
            "vocal": current_value("vocal"),
            "dance": current_value("dance"),
            "visual": current_value("visual"),
        },
        "route": {
            "produce_id": calendar.produce_id,
            "character_id": active.character_id,
            "current_week": current_week,
            "total_weeks": calendar.total_weeks,
            "route_actions": list(route_week.actions),
            "route_actions_exact": route_week.exact_actions,
            "route_week": route_week.display,
        },
        "options": [],
        "source": "outer-local-save+route-calendar",
    }


def read_live_training_choice() -> Mapping[str, Any]:
    """Recognize fixed Vo/Da/Vi rows without requiring recommendation colour."""

    from .live_actions import SuggestedClick
    from .training_choice import analyze_training_choice_path

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    state = analyze_training_choice_path(Path(png_path))
    # Older analyzer doubles expose only ``recommended_option`` plus a
    # serialized recommended_attribute; keep that seam compatible while the
    # production state uses the typed property.
    recommended_attribute = getattr(state, "recommended_attribute", ...)
    if recommended_attribute is ...:
        recommended_attribute = state.to_dict().get("recommended_attribute")
    if recommended_attribute is None:
        state_payload = state.to_dict()
        active = None
        try:
            from .run_identity import load_active_run

            active = load_active_run()
            growth = (
                None
                if active is None
                else _active_run_effective_growth_permils(
                    idol_card_id=active.idol_card_id,
                    produce_id=active.produce_id,
                )
            )
        except (FileNotFoundError, TypeError, ValueError):
            growth = None
        if growth is not None:
            growth_by_attribute = {
                "Vo": int(growth[0]),
                "Da": int(growth[1]),
                "Vi": int(growth[2]),
            }
            visible = {option.attribute for option in state.options}
            state_payload["effective_growth_permils"] = growth_by_attribute
            state_payload["policy_preferred_attribute"] = max(
                visible, key=lambda attribute: growth_by_attribute[attribute]
            )
            state_payload["selection_signal_source"] = str(growth[3])
        result: dict[str, Any] = {
            "capture": dict(capture),
            "state": state_payload,
        }
        try:
            context = _training_overview_context()
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            context = None
        if context is not None:
            result["overview_context"] = dict(context)
        return result
    option = state.recommended_option
    center_x, center_y = option.center
    action = SuggestedClick(
        label=f"重點練習{option.label}",
        canonical_x=center_x,
        canonical_y=center_y,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=option.canonical_box,
        # MaaGakumasu's ProduceChooseEventBase always uses a two-click
        # sequence, including when the row is already highlighted.
        click_count=2,
    )
    result = {
        "capture": dict(capture),
        "state": state.to_dict(),
        "action": action.to_dict(),
    }
    try:
        context = _training_overview_context()
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        context = None
    if context is not None:
        result["overview_context"] = dict(context)
    return result


@lru_cache(maxsize=1)
def _shop_card_art() -> Mapping[str, Path]:
    from .octo_assets import OctoAssetIndex

    return OctoAssetIndex.load().ensure_generic_card_art()


@lru_cache(maxsize=1)
def _shop_card_catalog():
    from .card_identity import CardNameCatalog

    return CardNameCatalog.load()


def read_live_shop() -> Mapping[str, Any]:
    """Capture and analyze the visible consultation shop without clicking."""

    from .shop_state import read_shop_path

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    state = read_shop_path(
        Path(png_path),
        card_art=_shop_card_art(),
        catalog=_shop_card_catalog(),
    )
    return {"capture": dict(capture), "state": state.to_dict()}


def _validated_reward_workflow_capture(
    capture: Mapping[str, Any],
) -> tuple[dict[str, Any], Path, float, int, int, tuple[int, int]]:
    """Normalize one screenshot record without treating metadata as vision."""

    from PIL import Image

    if not isinstance(capture, Mapping):
        raise TypeError("reward workflow capture must be a mapping")
    normalized = dict(capture)
    png_path = normalized.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("reward workflow capture did not provide a PNG path")
    path = Path(png_path).resolve()
    if not path.is_file():
        raise ValueError(f"reward workflow capture is unavailable: {path}")
    timestamp_value = normalized.get("timestamp")
    if (
        isinstance(timestamp_value, bool)
        or not isinstance(timestamp_value, (int, float))
        or not math.isfinite(float(timestamp_value))
    ):
        raise ValueError("reward workflow capture timestamp is invalid")
    timestamp = float(timestamp_value)
    identifiers: dict[str, int] = {}
    for field in ("hwnd", "pid"):
        value = normalized.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"reward workflow capture {field} is invalid")
        identifiers[field] = value
    with Image.open(path) as source:
        image_size = source.size
    for index, field in enumerate(("width", "height")):
        if field not in normalized:
            continue
        value = normalized[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"reward workflow capture {field} is invalid")
        if value != image_size[index]:
            raise ValueError(
                f"reward workflow capture {field} conflicts with PNG: "
                f"{value} != {image_size[index]}"
            )
    normalized.update(
        {
            "png_path": str(path),
            "timestamp": timestamp,
            "hwnd": identifiers["hwnd"],
            "pid": identifiers["pid"],
            "width": image_size[0],
            "height": image_size[1],
        }
    )
    return (
        normalized,
        path,
        timestamp,
        identifiers["hwnd"],
        identifiers["pid"],
        image_size,
    )


def _reward_preview_progress(
    session: LiveRewardPreviewWorkflowSession,
    confirmed_count: int,
) -> dict[str, object]:
    state = session.state
    ambiguous_slots = tuple(
        item.slot for item in state.thumbnail_evidence if item.ambiguous
    )
    return {
        "total_slots": len(state.thumbnail_evidence),
        "confirmed_slots": confirmed_count,
        "ambiguous_slots": list(ambiguous_slots),
        "previewed_slots": [item.slot for item in state.preview_identities],
        "unresolved_slots": list(state.unresolved_slots),
        "complete": state.complete,
    }


def _reward_preview_workflow_result(
    session: LiveRewardPreviewWorkflowSession,
    capture: Mapping[str, Any],
    overview_image: Any,
    *,
    card_art: Mapping[str, Any],
    catalog: Any,
) -> Mapping[str, Any]:
    from .reward_state import (
        finalize_reward_preview_disambiguation,
        resolved_reward_identities,
    )

    confirmed = resolved_reward_identities(
        session.state,
        overview_image,
        card_art=card_art,
        catalog=catalog,
    )
    base: dict[str, Any] = {
        "capture": dict(capture),
        "identity": session.context.to_dict(),
        "workflow": session,
        "workflow_state": session.to_dict(),
        "confirmed": [item.to_dict() for item in confirmed],
        "progress": _reward_preview_progress(session, len(confirmed)),
        "next_probe": (
            None
            if session.state.next_probe is None
            else session.state.next_probe.to_dict()
        ),
        "collect_allowed": False,
        "method": (
            "同一培育場次與視窗的可見畫面；高信心縮圖直接確認，"
            "歧義槽只接受指定槽位的完整預覽標題。"
        ),
    }
    if not session.state.complete:
        return {
            **base,
            "stage": "needs_preview",
            "ranking": [],
            "reason": (
                "尚有縮圖歧義；只要求使用者或外層 MAA 做一次可逆選取，"
                "再提供新畫面。此流程不會送出選取、確認或領取。"
            ),
        }
    selection = finalize_reward_preview_disambiguation(
        session.state,
        overview_image,
        session.overview_detections,
        card_art=card_art,
        catalog=catalog,
    )
    ranked = _rank_live_reward_offers(session.context, selection.offers)
    ranking = []
    for rank, offer in enumerate(ranked, 1):
        ranking.append(
            {
                "rank": rank,
                **offer.to_dict(),
                "rank_reason": (
                    f"Master evaluation {offer.evaluation}；"
                    "同分才比較稀有度、遊戲推薦標記、縮圖辨識分數與槽位。"
                ),
            }
        )
    return {
        **base,
        "stage": "identified",
        "selection_state": selection.to_dict(),
        "ranking": ranking,
        "reason": selection.recommendation_basis,
    }


def analyze_live_reward_preview_capture(
    capture: Mapping[str, Any],
    *,
    session: LiveRewardPreviewWorkflowSession | None = None,
    expected_run_id: str | None = None,
    expected_idol_card_id: str | None = None,
    expected_produce_id: str | None = None,
    require_preview_all: bool = False,
) -> Mapping[str, Any]:
    """Advance the read-only reward workflow with exactly one new capture.

    The caller is responsible for any reversible selection between captures.
    This analyzer never sends game input and never returns a collect/confirm
    action.
    """

    from PIL import Image

    from .reward_state import (
        advance_reward_preview_disambiguation,
        begin_reward_preview_disambiguation,
        filter_regular_reward_card_art,
        reward_card_is_selected,
    )

    if not isinstance(require_preview_all, bool):
        raise TypeError("require_preview_all must be bool")
    context = resolve_active_reward_plan_context(
        expected_run_id=expected_run_id,
        expected_idol_card_id=expected_idol_card_id,
        expected_produce_id=expected_produce_id,
    )
    if session is not None:
        if not isinstance(session, LiveRewardPreviewWorkflowSession):
            raise TypeError(
                "session must be LiveRewardPreviewWorkflowSession or None"
            )
        if session.context != context:
            raise ValueError(
                "reward workflow active-run identity changed; restart required"
            )
    (
        normalized_capture,
        path,
        timestamp,
        hwnd,
        pid,
        image_size,
    ) = _validated_reward_workflow_capture(capture)
    catalog = _shop_card_catalog()
    reward_art = filter_regular_reward_card_art(
        _live_reward_card_art(context.plan_type),
        catalog=catalog,
        plan_type=context.plan_type,
    )
    with Image.open(path) as source:
        current_image = source.convert("RGB")
        current_image.load()

    if session is None:
        report = _card_detector().detect_path(path)
        state = begin_reward_preview_disambiguation(
            current_image,
            tuple(report.detections),
            card_art=reward_art,
            plan_type=context.plan_type,
        )
        if require_preview_all:
            # N.I.A. Localify reward pages use compact artwork that can remain
            # visually close even when the card pool or ordering changes.  The
            # boxes remain useful click geometry, but every identity must come
            # from the reversibly selected full localized title.
            state = replace(
                state,
                thumbnail_evidence=tuple(
                    replace(item, ambiguous=True)
                    for item in state.thumbnail_evidence
                ),
            )
        selected_slots = tuple(
            slot
            for slot, box in enumerate(state.slot_boxes, 1)
            if reward_card_is_selected(current_image, box)
        )
        if len(selected_slots) > 1:
            raise ValueError(
                "reward workflow initial capture has multiple selected slots; "
                f"observed selected slots {selected_slots}"
            )
        # N.I.A. can preserve one reversible tile selection when the first
        # capture is taken.  It is not an identity proof and must not skip any
        # required title probes; the normal state still requests the first
        # unresolved slot and the caller will reconcile selection on the next
        # fresh frame.  Only the malformed multi-selection case above is a
        # blocker.
        next_session = LiveRewardPreviewWorkflowSession(
            context=context,
            overview_png_path=str(path),
            overview_timestamp=timestamp,
            source_hwnd=hwnd,
            source_pid=pid,
            image_size=image_size,
            overview_detections=tuple(report.detections),
            state=state,
            last_capture_timestamp=timestamp,
        )
        overview_image = current_image
    else:
        if (hwnd, pid) != (session.source_hwnd, session.source_pid):
            raise ValueError(
                "reward workflow window identity changed; restart required"
            )
        if image_size != session.image_size:
            raise ValueError(
                "reward workflow screen dimensions changed; restart required"
            )
        if timestamp <= session.last_capture_timestamp:
            raise ValueError(
                "reward workflow requires a strictly newer capture"
            )
        if session.state.complete:
            raise ValueError(
                "reward workflow is already identified; restart from a new overview"
            )
        overview_path = Path(session.overview_png_path)
        if not overview_path.is_file():
            raise ValueError(
                "reward workflow overview capture is no longer available; "
                "restart required"
            )
        with Image.open(overview_path) as source:
            overview_image = source.convert("RGB")
            overview_image.load()
        if overview_image.size != session.image_size:
            raise ValueError(
                "reward workflow overview dimensions changed; restart required"
            )
        state = advance_reward_preview_disambiguation(
            session.state,
            overview_image,
            current_image,
            card_art=reward_art,
            catalog=catalog,
            recognizer=_live_text_recognizer(),
        )
        next_session = replace(
            session,
            state=state,
            last_capture_timestamp=timestamp,
        )
    return _reward_preview_workflow_result(
        next_session,
        normalized_capture,
        overview_image,
        card_art=reward_art,
        catalog=catalog,
    )


def read_live_reward_preview_workflow(
    *,
    session: LiveRewardPreviewWorkflowSession | None = None,
    expected_run_id: str | None = None,
    expected_idol_card_id: str | None = None,
    expected_produce_id: str | None = None,
    capture: Mapping[str, Any] | None = None,
    require_preview_all: bool = False,
) -> Mapping[str, Any]:
    """Capture once (or consume an outer capture) and advance read-only state."""

    context = resolve_active_reward_plan_context(
        expected_run_id=expected_run_id,
        expected_idol_card_id=expected_idol_card_id,
        expected_produce_id=expected_produce_id,
    )
    if session is not None:
        if not isinstance(session, LiveRewardPreviewWorkflowSession):
            raise TypeError(
                "session must be LiveRewardPreviewWorkflowSession or None"
            )
        if session.context != context:
            raise ValueError(
                "reward workflow active-run identity changed; restart required"
            )
    current_capture = capture_once() if capture is None else capture
    arguments: dict[str, Any] = {
        "session": session,
        "expected_run_id": context.run_id,
        "expected_idol_card_id": context.idol_card_id,
        "expected_produce_id": context.produce_id,
    }
    if require_preview_all:
        arguments["require_preview_all"] = True
    return analyze_live_reward_preview_capture(current_capture, **arguments)


def read_live_reward_from_completed_preview(
    session: LiveRewardPreviewWorkflowSession,
    *,
    expected_run_id: str | None = None,
    expected_idol_card_id: str | None = None,
    expected_produce_id: str | None = None,
    capture: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Bind Collect after a completed preview workflow proves the selection.

    Thumbnail ambiguity remains fail-closed in :func:`read_live_reward`.  This
    stronger bridge instead requires a completed, run/window-bound preview
    session, a strictly newer frame, the Master-ranked slot to be the sole
    visibly selected slot, and that slot's full localized title to resolve to
    the same card/version again.  It never lowers the artwork threshold and it
    never selects a card on the caller's behalf.
    """

    from PIL import Image

    from .live_actions import SuggestedClick
    from .reward_state import (
        REWARD_COLLECT_BOX,
        filter_regular_reward_card_art,
        finalize_reward_preview_disambiguation,
        identify_reward_preview,
        reward_card_is_selected,
    )

    if not isinstance(session, LiveRewardPreviewWorkflowSession):
        raise TypeError("session must be LiveRewardPreviewWorkflowSession")
    if not session.state.complete:
        raise ValueError(
            f"reward preview slots remain unresolved: {session.state.unresolved_slots}"
        )
    context = resolve_active_reward_plan_context(
        expected_run_id=expected_run_id,
        expected_idol_card_id=expected_idol_card_id,
        expected_produce_id=expected_produce_id,
    )
    if session.context != context:
        raise ValueError("reward workflow active-run identity changed; restart required")

    current_capture = capture_once() if capture is None else capture
    normalized, path, timestamp, hwnd, pid, image_size = (
        _validated_reward_workflow_capture(current_capture)
    )
    if (hwnd, pid) != (session.source_hwnd, session.source_pid):
        raise ValueError("reward workflow window identity changed; restart required")
    if image_size != session.image_size:
        raise ValueError("reward workflow screen dimensions changed; restart required")
    if timestamp <= session.last_capture_timestamp:
        raise ValueError("reward confirmation requires a strictly newer capture")

    overview_path = Path(session.overview_png_path)
    if not overview_path.is_file():
        raise ValueError(
            "reward workflow overview capture is no longer available; restart required"
        )
    with Image.open(overview_path) as source:
        overview_image = source.convert("RGB")
        overview_image.load()
    with Image.open(path) as source:
        current_image = source.convert("RGB")
        current_image.load()

    catalog = _shop_card_catalog()
    reward_art = filter_regular_reward_card_art(
        _live_reward_card_art(context.plan_type),
        catalog=catalog,
        plan_type=context.plan_type,
    )
    selection = finalize_reward_preview_disambiguation(
        session.state,
        overview_image,
        session.overview_detections,
        card_art=reward_art,
        catalog=catalog,
    )
    ranked = _rank_live_reward_offers(context, selection.offers)
    recommended = ranked[0]
    selected_slots = tuple(
        slot
        for slot, box in enumerate(session.state.slot_boxes, 1)
        if reward_card_is_selected(current_image, box)
    )
    if selected_slots != (recommended.slot,):
        raise ValueError(
            "reward confirmation requires only the Master-ranked slot to be "
            f"selected: expected {recommended.slot}, observed {selected_slots}"
        )
    preview = identify_reward_preview(
        overview_image,
        current_image,
        slot_boxes=session.state.slot_boxes,
        slot=recommended.slot,
        plan_type=context.plan_type,
        card_art=reward_art,
        catalog=catalog,
        recognizer=_live_text_recognizer(),
    )
    if (preview.card_id, preview.upgrade) != (
        recommended.card_id,
        recommended.upgrade,
    ):
        raise ValueError(
            "selected reward preview conflicts with the Master-ranked card: "
            f"{preview.card_id}@{preview.upgrade} != "
            f"{recommended.card_id}@{recommended.upgrade}"
        )

    left, top, right, bottom = REWARD_COLLECT_BOX
    action = SuggestedClick(
        label=f"reward:collect:{recommended.display_name}",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=str(path),
        source_timestamp=timestamp,
        source_hwnd=hwnd,
        source_pid=pid,
        verification_box=REWARD_COLLECT_BOX,
        click_count=1,
    )
    return {
        "capture": normalized,
        "identity": context.to_dict(),
        "stage": "confirm",
        "recommended": recommended.to_dict(),
        "selected_preview": preview.to_dict(),
        "ranking": [offer.to_dict() for offer in ranked],
        "action": action.to_dict(),
        "collect_allowed": True,
        "method": (
            "completed preview identity + Master ranking + fresh sole-selection "
            "and full-title revalidation"
        ),
    }


def read_live_reward(
    *,
    expected_run_id: str | None = None,
    expected_idol_card_id: str | None = None,
    expected_produce_id: str | None = None,
) -> Mapping[str, Any]:
    """Read and rank a visible three- or four-card post-lesson reward.

    The first action only highlights the recommended card.  A later fresh read
    may expose the separate ``領取`` action once that same card is visibly
    selected; selecting and confirming are never bundled into one blind step.
    """

    from .live_actions import SuggestedClick
    from .reward_state import (
        REWARD_COLLECT_BOX,
        filter_regular_reward_card_art,
        read_reward_path,
    )

    context = resolve_active_reward_plan_context(
        expected_run_id=expected_run_id,
        expected_idol_card_id=expected_idol_card_id,
        expected_produce_id=expected_produce_id,
    )
    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    path = Path(png_path)
    report = _card_detector().detect_path(path)
    catalog = _shop_card_catalog()
    reward_art = filter_regular_reward_card_art(
        _live_reward_card_art(context.plan_type),
        catalog=catalog,
        plan_type=context.plan_type,
    )
    state = read_reward_path(
        path,
        tuple(report.detections),
        card_art=reward_art,
        catalog=catalog,
        plan_type=context.plan_type,
    )
    recommended = state.recommended_offer
    if recommended.selected:
        left, top, right, bottom = REWARD_COLLECT_BOX
        action = SuggestedClick(
            label=f"領取獎勵：{recommended.display_name}",
            canonical_x=(left + right) // 2,
            canonical_y=(top + bottom) // 2,
            source_png_path=png_path,
            source_timestamp=float(capture["timestamp"]),
            source_hwnd=int(capture["hwnd"]),
            source_pid=int(capture["pid"]),
            verification_box=REWARD_COLLECT_BOX,
            click_count=1,
        )
        stage = "confirm"
    else:
        x, y, width, height = recommended.box
        box = (x, y, x + width, y + height)
        action = SuggestedClick(
            label=f"選取獎勵：{recommended.display_name}",
            canonical_x=(box[0] + box[2]) // 2,
            canonical_y=(box[1] + box[3]) // 2,
            source_png_path=png_path,
            source_timestamp=float(capture["timestamp"]),
            source_hwnd=int(capture["hwnd"]),
            source_pid=int(capture["pid"]),
            verification_box=box,
            click_count=1,
        )
        stage = "select"
    return {
        "capture": dict(capture),
        "identity": context.to_dict(),
        "state": state.to_dict(),
        "action": action.to_dict(),
        "stage": stage,
        "confidence": min(
            *(item.confidence for item in report.detections if item.label == "cards"),
            *(offer.art_score for offer in state.offers),
        ),
        "method": (
            "active run 偶像卡由 Master 決定 Common + plan 候選；"
            "縮圖以本機 Octo 全色卡圖辨識；升級符號由畫面驗證；"
            "效果與 evaluation 直接讀本機 Master"
        ),
    }


def read_live_initial_regular_result() -> Mapping[str, Any]:
    """Bind the sole Next action on a proven lesson or audition result page.

    Result classification is delegated to the same strict screenshot evidence
    routine used by the verified card executor.  This wrapper adds no new
    result OCR and exposes only the already-fixed Next region as a one-click
    suggestion.
    """

    from .live_actions import SuggestedClick

    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    path = Path(png_path)
    recognizer = _live_text_recognizer()
    evidence = _card_play_result_evidence(path, recognizer, mode="lesson")
    mode = "lesson"
    if not evidence.detected:
        evidence = _card_play_result_evidence(path, recognizer, mode="exam")
        mode = "exam"
    if not evidence.detected and _fixed_audition_result_layout(path):
        from .produce_outer_local_save import read_current_produce_outer_local_save

        snapshot = read_current_produce_outer_local_save()
        completed = snapshot.completed_steps[-1] if snapshot.completed_steps else None
        audition_clear = bool(
            completed is not None
            and completed.step_type in {16, 17, 18}
            and any(line.line_type_name == "audition_clear" for line in completed.lines)
        )
        if audition_clear:
            score_line = next(
                (
                    line
                    for line in completed.lines
                    if line.line_type_name == "audition_result"
                ),
                None,
            )
            score = (
                int(score_line.target_id)
                if score_line is not None and score_line.target_id.isdigit()
                else None
            )
            evidence = CardPlayResultEvidence(
                detected=True,
                result_kind="ranked-outer-ledger",
                score=score,
                rank_text="",
                confidence=1.0,
                raw_text={"authority": "outer-completed-audition-step"},
            )
    if evidence.detected and evidence.confidence >= 0.80:
        evidence_payload: Mapping[str, Any] = evidence.to_dict()
    else:
        # Normal single-attribute lessons can move directly from PERFECT to a
        # final Vo/Da/Vi summary which reports only the trained attribute's
        # delta.  The checkpointer cross-validates that sparse delta against
        # the exact completed-step LocalSave row before writing the shadow.
        from .pursuit_lesson_result import read_pursuit_lesson_result_path

        try:
            pursuit = read_pursuit_lesson_result_path(
                path,
                recognizer,
                minimum_confidence=0.90,
                allow_sparse_deltas=True,
            )
        except ValueError as error:
            raise ValueError(
                "screen is not a stable Initial Regular result page"
            ) from error
        mode = "lesson"
        evidence_payload = {
            "detected": True,
            "result_kind": "attribute-summary",
            "confidence": pursuit.confidence,
            "result": pursuit.to_dict(),
        }
    left, top, right, bottom = EXAM_RESULT_NEXT_BOX
    action = SuggestedClick(
        label=f"continue-{mode}-result",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=EXAM_RESULT_NEXT_BOX,
        click_count=1,
    )
    return {
        "capture": dict(capture),
        "mode": mode,
        "evidence": dict(evidence_payload),
        "action": action.to_dict(),
    }


def read_live_initial_regular_post_audition_dialogue() -> Mapping[str, Any]:
    """Bind one MAA tap on a visible post-Final portrait dialogue panel.

    Final completion is taken from the game's outer LocalSave ledger.  The
    screenshot supplies only the fixed dialogue-panel target, so animated
    character artwork and dialogue language cannot cause false rejection.
    """

    from .live_actions import SuggestedClick
    from .produce_outer_local_save import read_current_produce_outer_local_save

    snapshot = read_current_produce_outer_local_save()
    completed = snapshot.completed_steps[-1] if snapshot.completed_steps else None
    if not (
        completed is not None
        and completed.step_type == 18
        and any(line.line_type_name == "audition_clear" for line in completed.lines)
    ):
        raise ValueError("outer LocalSave does not prove a cleared Final audition")
    capture = capture_once()
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("capture did not provide a PNG path")
    if _fixed_post_audition_dialogue_layout(png_path):
        surface_kind = "dialogue"
        left, top, right, bottom = POST_AUDITION_DIALOGUE_BOX
    elif _fixed_post_audition_summary_layout(png_path):
        surface_kind = "summary"
        left, top, right, bottom = POST_AUDITION_SUMMARY_CONTINUE_BOX
    else:
        raise ValueError("screen is not a fixed post-audition continuation page")
    action = SuggestedClick(
        label=f"continue-post-audition-{surface_kind}",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=png_path,
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=(left, top, right, bottom),
        click_count=1,
    )
    return {
        "capture": dict(capture),
        "authority": {
            "kind": "outer-completed-final+fixed-dialogue-panel",
            "step_type": completed.step_type,
            "log_index": completed.log_index,
        },
        "surface_kind": surface_kind,
        "action": action.to_dict(),
    }


def checkpoint_live_pursuit_lesson_result(capture: Mapping[str, Any]) -> Mapping[str, Any]:
    """Checkpoint a terminal result only from trusted run-shadow before stats."""
    from .produce_outer_local_save import (
        DEFAULT_PC_GAME_ROOT,
        read_current_produce_outer_local_save,
    )
    from .pursuit_lesson_result import checkpoint_pursuit_lesson_result, read_pursuit_lesson_result_path
    from .route_calendar import load_route_calendar
    from .run_identity import load_active_run, paths_for
    from .run_shadow import load_run_shadow

    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("lesson result capture is missing its PNG evidence")
    active_run = load_active_run()
    if active_run is None:
        raise ValueError("no active run identity; refuse to checkpoint lesson result")
    run_paths = paths_for(active_run)
    shadow = load_run_shadow(run_paths.shadow)
    if shadow is None:
        raise ValueError("no trusted run shadow; refuse to infer pursuit result before stats")
    before = {"vocal": shadow.vocal, "dance": shadow.dance, "visual": shadow.visual}
    if any(value is None for value in before.values()):
        raise ValueError("trusted run shadow is missing pursuit result before stats")
    try:
        result = read_pursuit_lesson_result_path(
            Path(png_path),
            minimum_confidence=0.90,
            allow_sparse_deltas=True,
        )
        sparse = any(
            not str(result.raw_text[f"{field}_delta"]).strip()
            for field in ("vocal", "dance", "visual")
        )
        screen_before = {
            "vocal": result.vocal - result.vocal_delta,
            "dance": result.dance - result.dance_delta,
            "visual": result.visual - result.visual_delta,
        }
        # Preserve the cheap path when the run shadow already contains the
        # exact pre-lesson totals.  Otherwise fall through to the game's
        # completed-step ledger below; support/passive effects between the
        # last shadow checkpoint and lesson start legitimately make the
        # shadow stale.
        if not sparse and screen_before == before:
            updated = checkpoint_pursuit_lesson_result(
                result,
                before={key: int(value) for key, value in before.items()},
                captured_at=float(capture.get("timestamp", 0.0)),
                evidence_path=png_path,
                identity=active_run,
                run_paths=run_paths,
            )
            return {
                "accepted": True,
                "result": result.to_dict(),
                "shadow": updated.to_dict(),
                "capture": dict(capture),
            }

        snapshot = read_current_produce_outer_local_save(DEFAULT_PC_GAME_ROOT)
        lesson_steps = tuple(
            step
            for step in snapshot.completed_steps
            if 1 <= step.step_type <= 9
            and any(line.line_type == 0 for line in step.lines)
        )
        if not lesson_steps:
            raise ValueError("outer LocalSave has no completed lesson row")
        step = max(lesson_steps, key=lambda value: value.log_index)
        line_fields = {6: "vocal", 7: "dance", 8: "visual"}
        deltas = {"vocal": 0, "dance": 0, "visual": 0}
        seen_fields: set[str] = set()
        for line in step.lines:
            field = line_fields.get(line.line_type)
            if field is None:
                continue
            if field in seen_fields:
                raise ValueError("outer lesson contains duplicate attribute rows")
            seen_fields.add(field)
            before_value = int(line.before)
            after_value = int(line.after)
            if float(before_value) != line.before or float(after_value) != line.after:
                raise ValueError("outer lesson attribute row is not integral")
            deltas[field] = after_value - before_value
        visible_deltas = {
            "vocal": result.vocal_delta,
            "dance": result.dance_delta,
            "visual": result.visual_delta,
        }
        if visible_deltas != deltas:
            raise ValueError(
                "lesson result/outer LocalSave delta mismatch: "
                f"{visible_deltas} != {deltas}"
            )
        finals = {
            "vocal": result.vocal,
            "dance": result.dance,
            "visual": result.visual,
        }
        for field, final in finals.items():
            outer_value = getattr(snapshot, field)
            if outer_value is not None and outer_value != final:
                raise ValueError(
                    f"lesson result/outer LocalSave {field} mismatch: "
                    f"{final} != {outer_value}"
                )
        exact_before = {
            field: finals[field] - deltas[field] for field in finals
        }
        shadow_before = {field: getattr(shadow, field) for field in finals}
        # The completed lesson row itself is the authoritative before/after
        # ledger.  Once its deltas match the result screen and its finals match
        # the current outer totals, an older run shadow cannot veto it.
        calendar = load_route_calendar(
            active_run.produce_id,
            character_id=active_run.character_id,
        )
        current_week = (
            min(step.week_marker + 1, calendar.total_weeks)
            if step.week_marker is not None
            else shadow.route_week
        )
        additional_values: dict[str, int] = {}
        for field in ("stamina", "max_stamina", "produce_points"):
            value = getattr(snapshot, field)
            if value is not None:
                additional_values[field] = value
        if current_week is not None:
            additional_values["route_week"] = current_week
            additional_values["weeks_remaining"] = calendar.total_weeks - current_week
        updated = checkpoint_pursuit_lesson_result(
            result,
            before=exact_before,
            captured_at=float(capture.get("timestamp", 0.0)),
            evidence_path=png_path,
            identity=active_run,
            run_paths=run_paths,
            additional_values=additional_values,
            authority={
                "kind": "result-screen+outer-completed-step",
                "outer_log_count": snapshot.log_count,
                "outer_step_log_index": step.log_index,
                "outer_step_type": step.step_type,
                "shadow_before": shadow_before,
                "pre_lesson": exact_before,
            },
        )
    except ValueError as error:
        # The captured PNG is durable evidence; no observation is applied.
        return {"accepted": False, "reason": str(error), "capture": dict(capture)}
    return {"accepted": True, "result": result.to_dict(), "shadow": updated.to_dict(), "capture": dict(capture)}


def checkpoint_live_nia_result(
    capture: Mapping[str, Any],
    *,
    produce_id: str,
    game_root: str | Path | None = None,
) -> Mapping[str, Any]:
    """Record a N.I.A. result from the game's already-applied outer state.

    N.I.A. lesson/work/event settlement can include support and memory
    multipliers which the server has already applied before this page is
    shown.  The unattended route therefore does not predict or reverse those
    deltas.  The normal run bootstrap has already rebased the run shadow from
    this same Produce LocalSave; this checkpoint only preserves the observed
    values alongside the result capture before Maa advances the page.
    """

    if produce_id not in {"produce-004", "produce-005"}:
        raise ValueError("N.I.A. result checkpoint requires produce-004 or produce-005")
    png_path = capture.get("png_path")
    if not isinstance(png_path, str) or not png_path:
        raise ValueError("N.I.A. result capture is missing its PNG evidence")
    from .produce_outer_local_save import (
        DEFAULT_PC_GAME_ROOT,
        read_current_produce_outer_local_save,
    )

    root = DEFAULT_PC_GAME_ROOT if game_root is None else Path(game_root)
    snapshot = read_current_produce_outer_local_save(root)
    lifecycle = snapshot.lifecycle
    if lifecycle is not None and not lifecycle.is_in_progress:
        raise ValueError("N.I.A. Produce LocalSave is no longer in progress")
    observed = {
        "week": snapshot.latest_week_marker,
        "last_completed_week": snapshot.last_completed_week,
        "log_count": snapshot.log_count,
        "stamina": snapshot.stamina,
        "max_stamina": snapshot.max_stamina,
        "produce_points": snapshot.produce_points,
        "vocal": snapshot.vocal,
        "dance": snapshot.dance,
        "visual": snapshot.visual,
        "vote_count": snapshot.vote_count,
    }
    return {
        "accepted": True,
        "authority": "post-action-produce-local-save",
        "calculation": "none-server-result-observed",
        "produce_id": produce_id,
        "observed": observed,
        "capture": dict(capture),
    }


def _legal(value: bool | None) -> str:
    return "可用" if value is True else "不可用" if value is False else "未判定"
