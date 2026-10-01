"""Narrow MaaFramework Win32 adapter for the verified Gakumas window.

The elevated controller owns one instance of this adapter for its whole
lifetime.  MaaFramework supplies background ``PrintWindow`` capture and
``SendMessage`` mouse input.  Recognition and task execution are
restricted to a small allow-list: fixed N.I.A. buttons, one fixed Produce
bootstrap, and one fixed ProduceEnd route.  Keyboard, shell, process-memory,
arbitrary tasks, arbitrary pipelines, and arbitrary windows remain unavailable.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import time
import types
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
from PIL import Image

from .nia_idol_catalog import NiaIdolCatalog, load_nia_idol_catalog
from .nia_replay_control import (
    ReplayTemplateContract,
    audit_bundled_replay_controls,
)


CANONICAL_CAPTURE_WIDTH = 720
CANONICAL_CAPTURE_HEIGHT = 1280
_ALLOWED_NATIVE_CAPTURE_HEIGHTS = frozenset(
    {
        CANONICAL_CAPTURE_HEIGHT - 1,
        CANONICAL_CAPTURE_HEIGHT,
        CANONICAL_CAPTURE_HEIGHT + 1,
    }
)
_READ_ONLY_RECOGNITION_NODES = frozenset(
    {"ProduceRecognitionMirror", "ProduceRecognitionCards"}
)
_AUDITION_CARD_LABELS = frozenset({"cards", "suggestions", "useless"})
_AUDITION_CARD_CAPTURE_METADATA_FIELDS = frozenset(
    {
        # Ranking-refresh binding metadata is observational only.  These
        # fields let a fresh read-only frame carry the exact snapshot/rank
        # selected by the caller; they never authorize a click.
        "capture_id",
        "ranking_snapshot_id",
        "ranking_source_sha256",
        "ranking_request_sha256",
        "ranking_visible_rank",
        "ranking_replay_identity_sha256",
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
    }
)

# ``ProduceCardsAuto`` can legitimately spend several minutes on a long
# Final exam.  A wall-clock-only timeout used to either wait forever on a
# stuck task or kill a healthy result transition.  The baseline runner below
# uses the total budget as a fail-closed envelope and the durable ExamSave as
# its progress heartbeat.
MAA_BASELINE_EXAM_TOTAL_TIMEOUT_SECONDS = 600.0
MAA_BASELINE_EXAM_IDLE_TIMEOUT_SECONDS = 90.0
MAA_BASELINE_EXAM_READINESS_RETRY_SECONDS = 15.0
MAA_BASELINE_EXAM_FOREGROUND_RECOVERY_SECONDS = 60.0
MAA_BASELINE_EXAM_POLL_SECONDS = 0.1
MAA_TASK_STOP_ACK_TIMEOUT_SECONDS = 5.0
MAA_SCREENSHOT_JOB_TIMEOUT_SECONDS = 15.0
MAA_RECOGNITION_JOB_TIMEOUT_SECONDS = 10.0
MAA_INPUT_JOB_TIMEOUT_SECONDS = 10.0
MAA_INITIALIZATION_JOB_TIMEOUT_SECONDS = 30.0
# A baseline exam normally produces far fewer than this many typed state
# changes.  Keep the trace bounded anyway: the durable heartbeat may be
# polled for up to ten minutes and its result is returned through the JSON
# controller protocol.
MAA_BASELINE_EXAM_PROGRESS_TRACE_MAX_ENTRIES = 512
# ``ProduceMainPage`` is the official MaaGakumasu gate for the scenario
# picker.  Its action is DoNothing and its recognition is the small ranking
# icon in the footer.  Keep the two navigation source nodes separate from the
# ordinary read-only recognition allow-list: ProduceStart owns a Click action,
# but this client only ever posts its recognition and then performs one
# explicitly verified click.
_PRODUCE_RANKING_NAVIGATION_NODES = frozenset(
    {"ProduceStart", "ProduceMainPage"}
)
_PRODUCE_RANKING_PAGE_EXPECTED_TEXT = (
    "プロデュースランキング",
    "ランキング",
    "Pランキング",
    "排行榜",
    "培育排行榜",
    "製作人排名",
    "製作人排行榜",
    "Produce Ranking",
)
_PRODUCE_RANKING_PAGE_OCR_ROI = (0, 0, 720, 320)
# The current MaaGakumasu bundle does not expose a complete, action-owned
# route from the Produce home page to the N.I.A. scenario card.  The replay
# reader therefore starts only after the caller has reached the idol-card
# picker.  These ROIs are the same header ROIs used by the existing
# OCR-driven idol picker; no fixed screen coordinate is used for an input.
# The selected-idol page does not render a difficulty label in its header.  In
# the live 3.3.0 layout the N.I.A. logo is the left-side background mark.
_NIA_RECOMMENDED_REPLAY_NIA_ROI = (0, 280, 240, 240)
_NIA_RECOMMENDED_REPLAY_GRID_SONG_ROI = (140, 20, 430, 45)
_NIA_RECOMMENDED_REPLAY_GRID_IDOL_ROI = (140, 55, 360, 45)
_NIA_RECOMMENDED_REPLAY_LEGACY_SONG_ROI = (380, 90, 320, 45)
_NIA_RECOMMENDED_REPLAY_LEGACY_IDOL_ROI = (440, 128, 280, 64)
_NIA_RECOMMENDED_REPLAY_GROWTH_BUTTON_ROI = (0, 720, 720, 560)
_NIA_RECOMMENDED_REPLAY_OVERVIEW_CONFIRM_TEXT = (
    "確定",
    "确定",
    "決定",
)
_NIA_RECOMMENDED_REPLAY_OVERVIEW_CONFIRM_ROI = (200, 1000, 320, 180)
# ``培育資訊`` first opens a read-only modal.  Its upper-right replay button
# is a separate state transition; do not treat that button as the final page.
_NIA_RECOMMENDED_REPLAY_MODAL_BUTTON_ROI = (480, 450, 240, 180)
# The actual replay page has a compact title pill in the upper-left and a
# separate difficulty badge below/right of it.  Both gates are required.
_NIA_RECOMMENDED_REPLAY_FINAL_HEADER_ROI = (0, 0, 300, 100)
_NIA_RECOMMENDED_REPLAY_FINAL_MASTER_ROI = (240, 90, 220, 90)
# The Pro and Master replay pages use the same compact difficulty badge ROI.
# Keep the historical Master constant above for the existing generic N.I.A.
# command, while the Pro-only command supplies its own OCR labels at runtime.
_NIA_RECOMMENDED_REPLAY_FINAL_DIFFICULTY_ROI = (
    _NIA_RECOMMENDED_REPLAY_FINAL_MASTER_ROI
)
_NIA_RECOMMENDED_REPLAY_NIA_TEXT = (
    "N.I.A",
    "NIA",
    "N.I.A.",
)
_NIA_RECOMMENDED_REPLAY_FINAL_TEXT = (
    "おすすめリプレイ",
    "推薦回放",
)
# The verified 3.3.0 recommendation page renders one playback button for
# every audition row in a fixed right-hand column.  Selecting the topmost
# visible button is deterministic and does not depend on a player name, score,
# or screenshot coordinate.  The runtime/corpus matcher identifies which
# audition actually opened instead of inferring it from the list position.
_NIA_RECOMMENDED_REPLAY_PLAY_TEXT = (
    "再生",
    "播放",
)
_NIA_RECOMMENDED_REPLAY_PLAY_ROI = (520, 400, 200, 780)
_NIA_RECOMMENDED_REPLAY_PAGE_SONG_ROI = (140, 140, 430, 58)
_NIA_RECOMMENDED_REPLAY_PAGE_IDOL_ROI = (140, 190, 360, 58)
_NIA_RECOMMENDED_REPLAY_PAGE_NIA_TEXT = (
    "NEXT IDOL AUDITION",
    "NEXT IDOL",
)
_NIA_RECOMMENDED_REPLAY_PAGE_NIA_ROI = (0, 85, 300, 105)
_STARTUP_TAP_TO_START_TEXT = ("TAP TO START",)
_STARTUP_TAP_TO_START_ROI = (140, 960, 440, 130)
_STARTUP_DOWNLOAD_HEADER_TEXT = (
    "資料下載",
    "数据下载",
    "データダウンロード",
)
_STARTUP_DOWNLOAD_HEADER_ROI = (0, 820, 720, 120)
_STARTUP_DOWNLOAD_CONFIRM_TEXT = (
    "確定",
    "确定",
    "OK",
)
_STARTUP_DOWNLOAD_CONFIRM_ROI = (360, 1080, 320, 160)
_NIA_OUTER_RECOGNITION_NODES = (
    "ProduceNIAFailedFlag",
    "ProduceNIAFailedRetry",
    "ProduceNIAFailedRetryLastChallenge",
    "ProduceNIAFailedRetryConfirm",
    "ProduceChooseNIAEventFlag",
    "ProduceRecognitionWorkOptions",
    "ProduceChooseGetFlag",
    "ProduceChooseStrengthenFlag",
    "ProduceChooseDeleteFlag",
    "ProduceShoppingFlag",
    "ProduceKeepDrinkFlag",
    "ProduceSkipChatFlag",
    "ProduceNIAButton",
    "ProduceMirrorFlag",
    "ProduceSkip",
)

# Live PC 3.3.0 idol-card picker coordinates in Maa's canonical 720x1280
# portrait space.  The selection contract always resets each filter group
# before selecting one exact Master-backed value, so persisted UI state cannot
# invert a checkbox on the next run.
_PRODUCE_IDOL_RARITY_FILTER_TARGETS = {
    "IdolCardRarity_Ssr": [150, 270, 1, 1],
    "IdolCardRarity_Sr": [360, 270, 1, 1],
    "IdolCardRarity_R": [570, 270, 1, 1],
}
_PRODUCE_IDOL_PLAN_FILTER_TARGETS = {
    "ProducePlanType_Plan1": [200, 442, 1, 1],
    "ProducePlanType_Plan2": [520, 442, 1, 1],
    "ProducePlanType_Plan3": [200, 516, 1, 1],
}
_PRODUCE_IDOL_RARITY_CHECKBOX_ROIS = {
    "IdolCardRarity_Ssr": (65, 252, 100, 287),
    "IdolCardRarity_Sr": (275, 252, 310, 287),
    "IdolCardRarity_R": (485, 252, 520, 287),
}
_PRODUCE_IDOL_PLAN_CHECKBOX_ROIS = {
    "ProducePlanType_Plan1": (65, 425, 100, 460),
    "ProducePlanType_Plan2": (380, 425, 415, 460),
    "ProducePlanType_Plan3": (65, 500, 100, 535),
}


def _orange_pixel_count(
    image: np.ndarray, roi: tuple[int, int, int, int]
) -> int:
    """Count the live UI's orange pixels in one BGR Maa frame ROI."""

    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
        return 0
    left, top, right, bottom = roi
    if not (
        0 <= left < right <= image.shape[1]
        and 0 <= top < bottom <= image.shape[0]
    ):
        return 0
    region = image[top:bottom, left:right, :3]
    blue = region[:, :, 0]
    green = region[:, :, 1]
    red = region[:, :, 2]
    orange = (
        (red > 220)
        & (green > 60)
        & (green < 210)
        & (blue < 120)
    )
    return int(np.count_nonzero(orange))


def _produce_idol_filter_panel_visible(image: np.ndarray) -> bool:
    return _orange_pixel_count(image, (20, 75, 700, 150)) >= 10_000


def _produce_idol_filter_tab_visible(image: np.ndarray) -> bool:
    if not _produce_idol_filter_panel_visible(image):
        return False
    # The filter tab uniquely exposes all three rarity checkbox squares in one
    # row.  The sort tab has only its left radio button at this height.
    for roi in _PRODUCE_IDOL_RARITY_CHECKBOX_ROIS.values():
        left, top, right, bottom = roi
        region = image[top:bottom, left:right, :3]
        if int(np.count_nonzero(np.any(region < 248, axis=2))) < 100:
            return False
    return True


def _produce_idol_filter_group_exact(
    image: np.ndarray,
    rois: Mapping[str, tuple[int, int, int, int]],
    selected: str | None,
) -> bool:
    if selected is not None and selected not in rois:
        return False
    for key, roi in rois.items():
        is_selected = _orange_pixel_count(image, roi) >= 250
        if is_selected != (key == selected):
            return False
    return True


def _produce_idol_filter_frame_matches(
    image: np.ndarray,
    *,
    state: str,
    rarity: str,
    plan_type: str,
) -> bool:
    """Verify one transaction boundary in the live 3.3.0 picker."""

    if state == "overview":
        return _produce_idol_expanded_grid_visible(image)
    if state == "panel":
        return _produce_idol_filter_panel_visible(image)
    if state == "filter-tab":
        return _produce_idol_filter_tab_visible(image)
    if state == "rarity-cleared":
        return (
            _produce_idol_filter_tab_visible(image)
            and _produce_idol_filter_group_exact(
                image, _PRODUCE_IDOL_RARITY_CHECKBOX_ROIS, None
            )
        )
    if state == "rarity-selected":
        return (
            _produce_idol_filter_tab_visible(image)
            and _produce_idol_filter_group_exact(
                image, _PRODUCE_IDOL_RARITY_CHECKBOX_ROIS, rarity
            )
        )
    if state == "plan-cleared":
        return (
            _produce_idol_filter_tab_visible(image)
            and _produce_idol_filter_group_exact(
                image, _PRODUCE_IDOL_RARITY_CHECKBOX_ROIS, rarity
            )
            and _produce_idol_filter_group_exact(
                image, _PRODUCE_IDOL_PLAN_CHECKBOX_ROIS, None
            )
        )
    if state == "exact-selection":
        return (
            _produce_idol_filter_tab_visible(image)
            and _produce_idol_filter_group_exact(
                image, _PRODUCE_IDOL_RARITY_CHECKBOX_ROIS, rarity
            )
            and _produce_idol_filter_group_exact(
                image, _PRODUCE_IDOL_PLAN_CHECKBOX_ROIS, plan_type
            )
        )
    if state == "picker":
        return (
            not _produce_idol_filter_panel_visible(image)
            and _orange_pixel_count(image, (220, 1030, 500, 1140)) >= 8_000
        )
    return False


def _produce_idol_grid_target_selected(
    image: np.ndarray, target: tuple[int, int, int, int]
) -> bool:
    """Recognize the orange selection brackets around one picker-grid tile."""

    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
        return False
    center_x, nominal_y = int(target[0]), int(target[1])
    left = max(0, center_x - 82)
    right = min(image.shape[1], center_x + 82)
    if right - left < 120:
        return False
    # After one grid-page swipe the retained overlap row shifts upward by
    # roughly 45 px, while Maa's safe click remains inside the card.  Search a
    # narrow vertical band for the brackets instead of treating the first
    # page's card center as a permanent screen coordinate.
    for center_y in range(nominal_y - 80, nominal_y + 81, 5):
        top = max(0, center_y - 112)
        bottom = min(image.shape[0], center_y + 112)
        if bottom - top < 180:
            continue
        region = image[top:bottom, left:right, :3]
        blue = region[:, :, 0]
        green = region[:, :, 1]
        red = region[:, :, 2]
        orange = (
            (red > 220)
            & (green > 60)
            & (green < 210)
            & (blue < 120)
        )
        bands = (
            orange[:16, :],
            orange[-16:, :],
            orange[:, :16],
            orange[:, -16:],
        )
        if sum(int(np.count_nonzero(band)) >= 300 for band in bands) >= 3:
            return True
    return False


def _produce_idol_grid_target_present(
    image: np.ndarray, target: tuple[int, int, int, int]
) -> bool:
    """Return whether one row-major picker cell contains a rendered card.

    A partially filled final page leaves nearly white geometric cells after
    its last card.  Real card art -- owned or locked -- contains thousands of
    dark or saturated pixels.  This check is used only after the bounded
    selection-bracket retries failed; it never identifies or chooses a card.
    """

    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
        return False
    x, y = int(target[0]), int(target[1])
    left, right = max(0, x - 65), min(image.shape[1], x + 65)
    top, bottom = max(0, y - 115), min(image.shape[0], y + 80)
    if left >= right or top >= bottom:
        return False
    region = image[top:bottom, left:right, :3].astype(np.int16, copy=False)
    maximum = region.max(axis=2)
    minimum = region.min(axis=2)
    dark_pixels = int(np.count_nonzero(maximum < 180))
    saturated_pixels = int(np.count_nonzero(maximum - minimum > 45))
    return dark_pixels >= 1_000 or saturated_pixels >= 1_000


def _produce_idol_expanded_grid_visible(image: np.ndarray) -> bool:
    """Return whether the 3.3.0 picker shows its white Overview card sheet.

    The 3D picker and the Overview sheet reuse several button labels and old
    grid coordinates, so OCR is not a layout authority here.  Their header
    band is structurally different instead: the Overview sheet is an almost
    completely neutral white surface while the 3D picker contains character
    art and a saturated background.  This predicate only classifies layout;
    it never identifies or selects a card.
    """

    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] < 3:
        return False
    header_band = image[520:570, 0:720, :3]
    if header_band.shape[:2] != (50, 720):
        return False
    white_ratio = float(np.count_nonzero(np.all(header_band > 235, axis=2))) / float(
        header_band.shape[0] * header_band.shape[1]
    )
    neutral_ratio = float(
        np.count_nonzero(
            header_band.max(axis=2).astype(np.int16)
            - header_band.min(axis=2).astype(np.int16)
            < 15
        )
    ) / float(header_band.shape[0] * header_band.shape[1])
    if white_ratio < 0.60 or neutral_ratio < 0.95:
        return False
    # A difficulty transition can hold a nearly white frame long enough to
    # satisfy the header test.  The real Overview already contains a rendered
    # multi-card grid below that header; require its material pixels before a
    # filter click is allowed.
    card_region = image[570:1050, 0:720, :3].astype(np.int16, copy=False)
    maximum = card_region.max(axis=2)
    minimum = card_region.min(axis=2)
    material_pixels = int(
        np.count_nonzero((maximum < 210) | (maximum - minimum > 40))
    )
    return material_pixels >= 40_000


def _produce_idol_grid_visible_targets(
    image: np.ndarray,
) -> tuple[tuple[int, int, int, int], ...]:
    """Find the visible four-column card grid in row-major order.

    The 3.3.0 detail sheet can show two or three rows depending on its expanded
    state.  Column one is populated for every non-empty row, so its vertical
    artwork bands provide row centres without deriving identity from art.  A
    fixed four-column occupancy check then removes empty cells on the last row.
    """

    if not _produce_idol_expanded_grid_visible(image):
        # The 3D picker is not a card grid.  Its bottom controls overlap the
        # historical fixed row coordinates and must be normalized by opening
        # the dedicated Overview sheet first.
        return ()
    x = 103
    start_y, end_y = 540, min(1140, image.shape[0] - 1)
    active: list[bool] = []
    for y in range(start_y, end_y + 1):
        region = image[y : y + 1, x - 55 : x + 55, :3].astype(
            np.int16, copy=False
        )
        maximum = region.max(axis=2)
        minimum = region.min(axis=2)
        count = int(
            np.count_nonzero((maximum < 205) | (maximum - minimum > 40))
        )
        active.append(count >= 25)

    raw_segments: list[tuple[int, int]] = []
    segment_start: int | None = None
    for offset, hit in enumerate(active):
        y = start_y + offset
        if hit and segment_start is None:
            segment_start = y
        elif not hit and segment_start is not None:
            raw_segments.append((segment_start, y - 1))
            segment_start = None
    if segment_start is not None:
        raw_segments.append((segment_start, end_y))

    merged: list[list[int]] = []
    for top, bottom in raw_segments:
        if merged and top - merged[-1][1] - 1 <= 20:
            merged[-1][1] = bottom
        else:
            merged.append([top, bottom])
    rows = tuple(
        (top + bottom) // 2
        for top, bottom in merged
        if 80 <= bottom - top + 1 <= 260
    )
    targets: list[tuple[int, int, int, int]] = []
    for center_y in rows:
        for center_x in (103, 274, 445, 615):
            target = (center_x, center_y, 1, 1)
            if _produce_idol_grid_target_present(image, target):
                targets.append(target)
    return tuple(targets)


@lru_cache(maxsize=2)
def _load_client_safe_upstream_maa_class(
    relative_source: str,
    class_name: str,
) -> type:
    """Load one upstream Maa class without executing AgentServer decorators.

    MaaGakumasu's agent modules are authored for a separate AgentServer
    process.  Importing them normally inside this Framework client switches
    the process-wide Maa DLL mode before ``Resource.register_custom_*`` runs.
    Execute the same upstream source with only the AgentServer import and its
    registration decorators removed; method bodies and Maa OCR/task calls are
    unchanged, and registration remains owned by the existing Framework
    ``Resource``.
    """

    if not relative_source or not class_name:
        raise ValueError("upstream Maa source and class name must be non-empty")
    agent_root = Path(__file__).resolve().parents[2] / "_research" / "MaaGakumasu" / "agent"
    source_path = (agent_root / relative_source).resolve()
    try:
        source_path.relative_to(agent_root.resolve())
    except ValueError as error:
        raise ValueError("upstream Maa source escaped the agent root") from error
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    agent_root_text = str(agent_root)
    if agent_root_text not in sys.path:
        sys.path.insert(0, agent_root_text)

    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    class ClientSafeTransformer(ast.NodeTransformer):
        @staticmethod
        def _is_agent_decorator(node: ast.expr) -> bool:
            return bool(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "AgentServer"
                and node.func.attr in {"custom_action", "custom_recognition"}
            )

        def visit_ImportFrom(self, node: ast.ImportFrom):
            if node.module == "maa.agent.agent_server":
                return None
            return node

        def visit_ClassDef(self, node: ast.ClassDef):
            node.decorator_list = [
                decorator
                for decorator in node.decorator_list
                if not self._is_agent_decorator(decorator)
            ]
            return self.generic_visit(node)

    tree = ClientSafeTransformer().visit(tree)
    ast.fix_missing_locations(tree)
    module_name = "gkms_tool._maa_upstream_client_safe_" + source_path.stem
    module = types.ModuleType(module_name)
    module.__file__ = str(source_path)
    module.__package__ = ""
    exec(compile(tree, str(source_path), "exec"), module.__dict__)
    loaded = module.__dict__.get(class_name)
    if not isinstance(loaded, type):
        raise RuntimeError(
            f"upstream Maa source does not define class {class_name}: {source_path}"
        )
    return loaded


@dataclass(frozen=True, slots=True)
class MaaBaselineExamProgressSignature:
    """Cheap, read-only heartbeat for one active ``ExamSaveData``.

    The filesystem fields are deliberately part of the signature even when a
    partially-written save cannot be decoded.  Native LocalSave writes are
    atomic from the game's point of view but a reader can still observe the
    short replacement window; a changed path/mtime/size is useful progress in
    that case.  Typed fields make the heartbeat useful when an application
    keeps the same file metadata while advancing the in-memory projection.
    """

    path: Path
    mtime_ns: int
    size: int
    step_type: int | None = None
    current_turn: int | None = None
    remain_turn: int | None = None
    score: int | None = None
    hand: tuple[tuple[str, str, int], ...] = ()
    turn_end: bool | None = None

    @property
    def modified_ns(self) -> int:
        """Compatibility alias used by the existing LocalSave readers."""

        return self.mtime_ns

    @property
    def step(self) -> int | None:
        return self.step_type

    @property
    def remain(self) -> int | None:
        return self.remain_turn

    @property
    def hand_count(self) -> int:
        return len(self.hand)

    @property
    def is_turn_card_play_end(self) -> bool | None:
        return self.turn_end


class MaaBaselineExamProgressReader:
    """Read one active ExamSave heartbeat without touching the game.

    ``selector`` and ``evidence_loader`` are injectable so timeout behaviour
    can be tested with a deterministic clock and fake tasker.  The default
    selector is the existing active-Produce ExamSave selector; no outer-week
    save is used as an exam completion signal.
    """

    def __init__(
        self,
        exam_save_path: str | Path | None = None,
        *,
        selector: Callable[[], Path | None] | None = None,
        evidence_loader: Callable[[Path], Any] | None = None,
    ) -> None:
        self.exam_save_path = (
            None if exam_save_path is None else Path(exam_save_path)
        )
        self.selector = selector
        self.evidence_loader = evidence_loader
        self._last_file_marker: tuple[Path, int, int] | None = None
        self._last_signature: MaaBaselineExamProgressSignature | None = None

    def _select_path(self) -> Path | None:
        if self.exam_save_path is not None:
            return self.exam_save_path
        selector = self.selector
        if selector is None:
            from .plan3_audition_advisor_gui import (
                select_plan3_exam_local_save_path,
            )

            selector = select_plan3_exam_local_save_path
        try:
            selected = selector()
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
            return None
        return None if selected is None else Path(selected)

    @staticmethod
    def _typed_fields(state: Any) -> dict[str, Any]:
        zones = getattr(state, "zones", None)
        raw_hand = getattr(zones, "hand", ()) if zones is not None else ()
        hand: list[tuple[str, str, int]] = []
        for card in raw_hand or ():
            guid = getattr(card, "guid", "")
            card_id = getattr(card, "card_id", "")
            upgrade = getattr(card, "effective_upgrade", None)
            if callable(upgrade):
                upgrade = upgrade()
            if not isinstance(upgrade, int) or isinstance(upgrade, bool):
                upgrade = getattr(card, "base_upgrade", 0)
            hand.append((str(guid), str(card_id), int(upgrade)))
        return {
            "step_type": getattr(state, "step_type_value", None),
            "current_turn": getattr(state, "current_turn", None),
            "remain_turn": getattr(state, "remain_turn", None),
            "score": getattr(state, "score", None),
            "hand": tuple(hand),
            "turn_end": getattr(state, "is_turn_card_play_end", None),
        }

    def __call__(self) -> MaaBaselineExamProgressSignature | None:
        path = self._select_path()
        if path is None:
            return None
        try:
            target = path.resolve()
            metadata = target.stat()
        except (FileNotFoundError, OSError, RuntimeError):
            return None
        if not target.is_file():
            return None
        marker = (target, int(metadata.st_mtime_ns), int(metadata.st_size))
        if marker == self._last_file_marker and self._last_signature is not None:
            return self._last_signature

        fields: dict[str, Any] = {}
        loader = self.evidence_loader
        if loader is None:
            # Keep this import local: importing Maa static helpers must not
            # import the LocalSave decoder or initialize any game dependency.
            from .initial_regular_autopilot import (
                load_initial_regular_plan2_exam_evidence,
            )

            loader = load_initial_regular_plan2_exam_evidence
        try:
            evidence = loader(target)
            fields = self._typed_fields(getattr(evidence, "state", evidence))
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
            # Metadata remains a valid heartbeat during an atomic/replaced
            # save.  Typed fields are best-effort and intentionally fail open
            # to the cheap marker rather than turning a transient read into a
            # false timeout.
            fields = {}
        signature = MaaBaselineExamProgressSignature(
            path=target,
            mtime_ns=marker[1],
            size=marker[2],
            **fields,
        )
        self._last_file_marker = marker
        self._last_signature = signature
        return signature


def _active_exam_evidence() -> Any | None:
    """Read the active typed ExamSave envelope without touching game input."""

    try:
        from .initial_regular_autopilot import (
            load_initial_regular_plan2_exam_evidence,
        )
        from .plan3_audition_advisor_gui import (
            select_plan3_exam_local_save_path,
        )

        path = select_plan3_exam_local_save_path()
        if path is None or not path.is_file():
            return None
        return load_initial_regular_plan2_exam_evidence(path)
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _native_exam_zero_hand_actionable() -> bool | None:
    """Return exact active ExamSave zero-hand state, or ``None`` if unreadable."""

    evidence = _active_exam_evidence()
    if evidence is None:
        return None
    state = evidence.state
    return bool(
        state.remain_turn > 0
        and not state.is_turn_card_play_end
        and len(state.zones.hand) == 0
    )


# The completion action is intentionally kept as a thin Maa adapter.  Its
# transition journal is nevertheless required to be exact: a changed
# ExamSave is not, by itself, proof that the click submitted the selected
# card.  In particular, a drink and a number of UI-owned saves can change the
# digest while leaving the card action untouched.  The helpers below consume
# only fields already present in the typed ExamSave evidence (plus optional
# native observer/action-sequence fields supplied by test/integration
# adapters); they never infer a card play from score or screenshot changes.
_CARD_ACTION_SEQUENCE_FIELD_NAMES = (
    "native_action_sequence",
    "action_sequence",
    "native_actions",
    "action_states",
    "native_action_log",
    "action_log",
)
_CARD_ACTION_ZONE_NAMES = ("hand", "deck", "grave", "lost", "hold")
_MISSING = object()


def _member(value: Any, name: str, default: Any = _MISSING) -> Any:
    """Read a mapping/object member without turning malformed evidence fatal."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_json_value(value: Any) -> Any:
    """Unwrap the canonical JSON holder used by LocalSave evidence."""

    to_value = getattr(value, "to_value", None)
    if callable(to_value):
        try:
            return to_value()
        except (TypeError, ValueError, RuntimeError):
            return _MISSING
    return value


def _evidence_state(value: Any) -> Any:
    return _member(value, "state", value)


def _opaque_runtime_fields(value: Any) -> Mapping[str, Any] | None:
    state = _evidence_state(value)
    runtime = _member(state, "root_runtime", None)
    opaque = _as_json_value(_member(runtime, "opaque_fields", None))
    return opaque if isinstance(opaque, Mapping) else None


def _card_action_log_rows(value: Any, field_name: str) -> list[Any] | None:
    """Return one raw native log list, preserving ``None`` for absent data."""

    state = _evidence_state(value)
    runtime = _member(state, "root_runtime", None)
    aliases = (field_name, field_name[:1].lower() + field_name[1:])
    if field_name == "userPlayLogList":
        aliases += ("user_play_log_list",)
    elif field_name == "playLogList":
        aliases += ("play_log_list",)
    raw = _MISSING
    for owner in (value, state, runtime, _opaque_runtime_fields(value)):
        for alias in aliases:
            candidate = _member(owner, alias, _MISSING)
            if candidate is not _MISSING:
                raw = candidate
                break
        if raw is not _MISSING:
            break
    if raw is _MISSING:
        return None
    raw = _as_json_value(raw)
    if not isinstance(raw, list):
        return None
    return raw


def _strict_int(value: Any) -> int | None:
    return value if type(value) is int else None


def _card_action_record_from_row(row: Any) -> tuple[int | None, str, int, str | None] | None:
    """Project a native card-play row to ``(turn, id, upgrade, guid)``.

    The normal PC source is ``userPlayLogList`` where one cell can contain
    multiple card IDs.  A few observer adapters expose direct action rows;
    accepting those here keeps the boundary proof reusable without making the
    Maa action depend on one observer implementation.
    """

    forecast_state = _member(row, "forecast_state", "normal")
    if (
        _member(row, "state_complete", True) is False
        or _member(row, "complete", True) is False
        or _member(row, "committed", True) is False
        or _member(row, "submitted", True) is False
        or _member(row, "forecast", False) is True
        or (
            forecast_state is not None
            and forecast_state != "normal"
        )
    ):
        return None
    turn = _member(row, "_currentTurn", _member(row, "current_turn", None))
    turn_value = _strict_int(turn)
    card_ids = _member(row, "_playCardList", _MISSING)
    upgrades = _member(row, "_playCardUpgradeList", _MISSING)
    if card_ids is not _MISSING or upgrades is not _MISSING:
        # Only cell type 1 is the native user card-play cell.  Other cells
        # carry drinks/effects and must never become a card action witness.
        cell_type = _member(row, "_cellType", _member(row, "cell_type", 1))
        if cell_type != 1 or not isinstance(card_ids, list) or not isinstance(upgrades, list):
            return None
        if len(card_ids) != len(upgrades):
            return None
        # This function returns one row.  The caller expands all card IDs in
        # order, so a multi-card cell is handled without losing ordering.
        if not card_ids:
            return ()  # type: ignore[return-value]
        first_id = card_ids[0]
        first_upgrade = upgrades[0]
        if (
            not isinstance(first_id, str)
            or not first_id
            or type(first_upgrade) is not int
            or first_upgrade < 0
        ):
            return None
        return turn_value, first_id, first_upgrade, None

    # Observer/action-sequence rows use one of these direct spellings.
    action_type = _member(row, "action_type", _member(row, "kind", None))
    if action_type is None:
        play_type = _member(row, "play_type", _member(row, "playType", None))
        if play_type is not None and play_type != 2:
            return None
    elif str(action_type).casefold() not in {
        "use-hand",
        "use_hand",
        "play",
        "card",
        "hand",
    }:
        return None
    card = _member(row, "card", None)
    data = _member(row, "data", None)
    if card is None:
        card = _member(data, "card", None)
    card_id = _member(
        row,
        "card_id",
        _member(
            row,
            "cardId",
            _member(
                card,
                "card_id",
                _member(
                    card,
                    "id",
                    _member(data, "card_id", _member(data, "cardId", None)),
                ),
            ),
        ),
    )
    guid = _member(
        row,
        "card_guid",
        _member(
            row,
            "selected_guid",
            _member(
                row,
                "chosen_guid",
                _member(
                    card,
                    "guid",
                    _member(data, "card_guid", _member(data, "guid", None)),
                ),
            ),
        ),
    )
    upgrade = _member(
        row,
        "upgrade",
        _member(
            row,
            "card_upgrade",
            _member(card, "upgrade", _member(data, "upgrade", None)),
        ),
    )
    if (
        not isinstance(card_id, str)
        or not card_id
        or type(upgrade) is not int
        or upgrade < 0
    ):
        return None
    return turn_value, card_id, upgrade, guid if isinstance(guid, str) else None


def _expanded_card_action_log(
    value: Any,
    field_name: str,
) -> tuple[tuple[int | None, str, int, str | None], ...] | None:
    rows = _card_action_log_rows(value, field_name)
    if rows is None:
        return None
    result: list[tuple[int | None, str, int, str | None]] = []
    for row in rows:
        if not isinstance(row, Mapping) and not hasattr(row, "__dict__"):
            return None
        if (
            _member(row, "state_complete", True) is False
            or _member(row, "complete", True) is False
            or _member(row, "committed", True) is False
            or _member(row, "submitted", True) is False
            or _member(row, "forecast", False) is True
        ):
            return None
        card_ids = _member(row, "_playCardList", _MISSING)
        upgrades = _member(row, "_playCardUpgradeList", _MISSING)
        if card_ids is not _MISSING or upgrades is not _MISSING:
            cell_type = _member(row, "_cellType", _member(row, "cell_type", 1))
            if cell_type != 1 or not isinstance(card_ids, list) or not isinstance(upgrades, list):
                # Non-card user-log cells are valid and are ignored.  A
                # malformed card cell, however, cannot be used as a witness.
                if cell_type != 1:
                    continue
                return None
            if len(card_ids) != len(upgrades):
                return None
            turn = _strict_int(
                _member(row, "_currentTurn", _member(row, "current_turn", None))
            )
            for card_id, upgrade in zip(card_ids, upgrades):
                if (
                    not isinstance(card_id, str)
                    or not card_id
                    or type(upgrade) is not int
                    or upgrade < 0
                ):
                    return None
                result.append((turn, card_id, upgrade, None))
            continue
        direct = _card_action_record_from_row(row)
        if direct is None:
            # Empty/non-card direct rows are ignored only when they explicitly
            # identify themselves as another action kind.
            action_type = _member(row, "action_type", _member(row, "kind", None))
            if action_type is not None and str(action_type).casefold() not in {
                "use-hand",
                "use_hand",
                "play",
                "card",
                "hand",
            }:
                continue
            return None
        result.append(direct)
    return tuple(result)


def _native_card_play_log_proof(
    before: Any,
    after: Any,
    chosen: Any,
) -> str | None:
    """Prove one newly appended native card-play log row for ``chosen``."""

    for field_name in ("userPlayLogList", "playLogList"):
        previous = _expanded_card_action_log(before, field_name)
        current = _expanded_card_action_log(after, field_name)
        if previous is None or current is None or len(current) <= len(previous):
            continue
        if current[: len(previous)] != previous:
            continue
        delta = current[len(previous) :]
        # A single after-save must belong to one card boundary.  If multiple
        # card rows appeared between reads, retaining one final state for more
        # than one pending action would fabricate intermediate transitions.
        if len(delta) != 1:
            continue
        turn, card_id, upgrade, guid = delta[0]
        chosen_id = _member(chosen, "card_id", None)
        chosen_upgrade = _member(chosen, "effective_upgrade", None)
        if callable(chosen_upgrade):
            try:
                chosen_upgrade = chosen_upgrade()
            except (TypeError, ValueError, RuntimeError):
                chosen_upgrade = None
        before_state = _evidence_state(before)
        before_turn = _strict_int(_member(before_state, "current_turn", None))
        if (
            card_id == chosen_id
            and type(chosen_upgrade) is int
            and upgrade == chosen_upgrade
            and (turn is None or before_turn is None or turn == before_turn)
        ):
            # The native user log carries card ID/upgrade, not GUID.  It is
            # therefore a GUID witness only when that identity is unique in
            # the frozen Hand; duplicate card instances stay abstained unless
            # an observer row supplies the GUID itself.
            hand = _member(_member(before_state, "zones", None), "hand", ())
            try:
                matching_hand = tuple(
                    card
                    for card in hand
                    if _member(card, "card_id", None) == card_id
                    and _member(card, "effective_upgrade", None) == upgrade
                )
            except TypeError:
                matching_hand = ()
            chosen_guid = _member(chosen, "guid", None)
            if (
                len(matching_hand) == 1
                and _member(matching_hand[0], "guid", None) == chosen_guid
            ):
                return f"native-{field_name}-card-play"
        # A direct observer row may carry the selected GUID even when its
        # human-readable card ID is localized/projected differently.
        chosen_guid = _member(chosen, "guid", None)
        if isinstance(guid, str) and guid and guid == chosen_guid:
            return f"native-{field_name}-chosen-guid"
    return None


def _sequence_source(value: Any, field_name: str) -> Any:
    for owner in (
        value,
        _evidence_state(value),
        _member(_evidence_state(value), "root_runtime", None),
        _opaque_runtime_fields(value),
    ):
        raw = _member(owner, field_name, _MISSING)
        if raw is _MISSING:
            continue
        raw = _as_json_value(raw)
        if isinstance(raw, Mapping):
            # Most adapters wrap the rows under ``actions`` or ``events``;
            # a direct one-row mapping is also a valid source.
            for key in ("actions", "events", "rows", "entries"):
                nested = raw.get(key, _MISSING)
                if nested is not _MISSING:
                    return _as_json_value(nested)
        return raw
    return _MISSING


def _sequence_rows(value: Any, field_name: str) -> tuple[Any, ...] | None:
    raw = _sequence_source(value, field_name)
    if raw is _MISSING:
        return None
    if isinstance(raw, (list, tuple)):
        return tuple(raw)
    if isinstance(raw, Mapping):
        return (raw,)
    return None


def _native_action_sequence_proof(
    before: Any,
    after: Any,
    chosen: Any,
) -> str | None:
    """Prove one appended normal native action with the chosen GUID."""

    chosen_guid = _member(chosen, "guid", None)
    chosen_id = _member(chosen, "card_id", None)
    chosen_upgrade = _member(chosen, "effective_upgrade", None)
    before_state = _evidence_state(before)
    before_turn = _strict_int(_member(before_state, "current_turn", None))
    for field_name in _CARD_ACTION_SEQUENCE_FIELD_NAMES:
        previous = _sequence_rows(before, field_name)
        current = _sequence_rows(after, field_name)
        if previous is None or current is None or len(current) != len(previous) + 1:
            continue
        # Sequence rows are immutable journal records.  Compare the raw
        # prefix instead of a card-only projection so a drink/UI event cannot
        # be silently skipped and then attributed to this card.
        if current[: len(previous)] != previous:
            continue
        row = current[-1]
        if (
            _member(row, "state_complete", True) is False
            or _member(row, "complete", True) is False
            or _member(row, "committed", True) is False
            or _member(row, "submitted", True) is False
            or _member(row, "forecast", False) is True
        ):
            continue
        direct_record = _card_action_record_from_row(row)
        data = _member(row, "data", None)
        card = _member(row, "card", _member(data, "card", None))
        row_guid = _member(
            row,
            "card_guid",
            _member(
                row,
                "selected_guid",
                _member(
                    row,
                    "chosen_guid",
                    _member(card, "guid", _member(data, "card_guid", None)),
                ),
            ),
        )
        row_id = _member(
            row,
            "card_id",
            _member(
                row,
                "cardId",
                _member(card, "id", _member(data, "card_id", None)),
            ),
        )
        row_upgrade = _member(
            row,
            "upgrade",
            _member(
                row,
                "card_upgrade",
                _member(card, "upgrade", _member(data, "upgrade", None)),
            ),
        )
        row_turn = _strict_int(
            _member(
                row,
                "turn",
                _member(row, "current_turn", _member(data, "turn", None)),
            )
        )
        action_type = _member(row, "action_type", _member(row, "kind", "use-hand"))
        if str(action_type).casefold() not in {
            "use-hand",
            "use_hand",
            "play",
            "card",
            "hand",
        }:
            continue
        if direct_record is not None:
            direct_turn, direct_id, direct_upgrade, direct_guid = direct_record
            row_turn = direct_turn
            row_id = direct_id
            row_upgrade = direct_upgrade
            row_guid = direct_guid or row_guid
        guid_match = (
            isinstance(row_guid, str)
            and isinstance(chosen_guid, str)
            and row_guid == chosen_guid
        )
        id_match = (
            row_id == chosen_id
            and type(row_upgrade) is int
            and type(chosen_upgrade) is int
            and row_upgrade == chosen_upgrade
        )
        turn_match = row_turn is None or before_turn is None or row_turn == before_turn
        if turn_match and (guid_match or id_match):
            return f"native-{field_name}"
    return None


def _live_card_positions(state: Any) -> dict[str, tuple[str, int, Any]] | None:
    zones = _member(state, "zones", None)
    if zones is None:
        return None
    result: dict[str, tuple[str, int, Any]] = {}
    for zone_name in _CARD_ACTION_ZONE_NAMES:
        values = _member(zones, zone_name, ())
        if values is None:
            values = ()
        try:
            values = tuple(values)
        except TypeError:
            return None
        for index, card in enumerate(values):
            guid = _member(card, "guid", None)
            if not isinstance(guid, str) or not guid:
                return None
            if guid in result:
                # A duplicated GUID cannot establish an exact destination.
                return None
            result[guid] = (zone_name, index, card)
    playing = _member(state, "playing_card", None)
    if playing is not None:
        guid = _member(playing, "guid", None)
        if not isinstance(guid, str) or not guid or guid in result:
            return None
        result[guid] = ("playing", 0, playing)
    return result


def _card_play_count(card: Any) -> int | None:
    runtime = _member(card, "runtime_state", None)
    value = _member(runtime, "play_count", _member(card, "play_count", None))
    return _strict_int(value)


def _chosen_guid_zone_proof(before: Any, after: Any, chosen: Any) -> str | None:
    """Prove a chosen GUID changed live zone, including retained-in-hand cards."""

    before_positions = _live_card_positions(_evidence_state(before))
    after_positions = _live_card_positions(_evidence_state(after))
    chosen_guid = _member(chosen, "guid", None)
    if (
        before_positions is None
        or after_positions is None
        or not isinstance(chosen_guid, str)
        or chosen_guid not in before_positions
    ):
        return None
    before_zone, _before_index, before_card = before_positions[chosen_guid]
    after_value = after_positions.get(chosen_guid)
    if after_value is None:
        return None
    after_zone, _after_index, after_card = after_value
    if before_zone != "hand":
        return None
    if after_zone != "hand":
        # A complete chosen-GUID destination is itself native action
        # authority.  Do not additionally require a scalar counter: some
        # cards move before the aggregate counter is persisted, and a card
        # with a retained/temporary effect may legitimately have unusual
        # counter timing.  UI/drink saves cannot create this GUID transition.
        return f"chosen-guid-zone:{before_zone}->{after_zone}"

    # Some native effects retain the played card in Hand.  Its GUID remaining
    # there is not a rejection: require an independently advancing per-card
    # runtime counter (or an action counter when the card object lacks that
    # optional field), never mere digest/score/UI movement.
    old_play_count = _card_play_count(before_card)
    new_play_count = _card_play_count(after_card)
    if (
        old_play_count is not None
        and new_play_count is not None
        and new_play_count > old_play_count
    ):
        return "chosen-guid-retained-hand-play-count"
    return None


def _card_action_commit_proof(before: Any, after: Any, chosen: Any) -> str | None:
    """Return an explicit authority proving ``chosen`` was actually played."""

    return (
        _native_action_sequence_proof(before, after, chosen)
        or _native_card_play_log_proof(before, after, chosen)
        or _chosen_guid_zone_proof(before, after, chosen)
    )


def _evidence_snapshot_key(value: Any) -> tuple[Any, ...]:
    try:
        digest = value.digest()
    except (AttributeError, TypeError, ValueError, RuntimeError):
        digest = None
    if isinstance(digest, str) and digest:
        return (
            "digest",
            digest,
            _member(value, "run_id", None),
            _member(value, "step_context_id", None),
            _member(value, "session_transition_id", None),
            _member(value, "source_path", None),
        )
    return ("object", id(value))


@lru_cache(maxsize=1)
def _completion_safe_produce_cards_action_type() -> type:
    """Adapt Maa's card player to the 3.3.0 confirmation modal.

    Upstream ``ProduceCardsAuto`` runs the broad ``ProduceButton`` fallback
    while waiting for the next playable frame.  On 3.3.0 a card whose effect
    has no valid target opens a Select confirmation sheet: both Cancel and
    Decide are visible, and the broad node chooses the higher-scoring Cancel.
    That creates an endless select/cancel loop.  Keep upstream card/drink/move
    policy, but require the dedicated Cancel+Decide modal pair and click only
    Maa's right-side ``ProduceDecide`` node.
    """

    ProduceCardsAuto = _load_client_safe_upstream_maa_class(
        "custom/action/produce.py", "ProduceCardsAuto"
    )

    class GkmsToolCompletionSafeProduceCardsAuto(ProduceCardsAuto):
        CLICK_DELAY = 0.55
        NO_CARDS_SKIP_RETRY_EVERY_READS = 4
        NO_CARDS_SKIP_MAX_ATTEMPTS = 2
        ZERO_HAND_SKIP_POINT = (667, 783)
        _transition_observer: Callable[[Mapping[str, Any]], None] | None = None
        # Optional read-only exact root enumerator.  It is deliberately a
        # sidecar dependency: the completion action keeps its existing Maa
        # policy and this provider can neither add/remove an input nor submit
        # one.  A complete provider result is retained on the action-time
        # boundary so a later ExamSave cannot re-index the candidate list.
        _unified_legal_action_snapshot_provider: Callable[[Any], Any] | None = None
        # A card action can finish before the game's atomic ExamSave replace
        # becomes visible to this process.  Keep the exact, action-time
        # boundary and resolve it on the next settled read instead of making
        # Maa wait (or dropping the row after a short timeout).
        _pending_transition_boundaries: list[
            tuple[Any, tuple[Any, ...]]
        ] = []
        # A sidecar failure must never be turned into an exact row, but it
        # also must not disappear when the task's observer is detached.  Keep
        # lifecycle diagnostics so the baseline result can report an explicit
        # drop instead of silently losing a pending boundary.
        _transition_drop_log: list[dict[str, Any]] = []

        def __init__(self):
            super().__init__()
            self._latest_card_results: tuple[Any, ...] = ()
            # The upstream action can consume the first turn while it is
            # waiting for a playable frame (notably when the turn starts with
            # drinks).  Freeze the next settled turn-one ExamSave before each
            # semantic action so a failed/unfinished sidecar proof can be
            # replaced by the next existing playable poll.  This is
            # deliberately flow-neutral: the existing exact candidate
            # provider is still the authority for the candidate set when the
            # boundary is consumed.
            self._initial_exam_boundary: Any | None = None
            self._initial_exam_boundary_captured = False

        @classmethod
        def set_transition_observer(
            cls,
            observer: Callable[[Mapping[str, Any]], None] | None,
        ) -> None:
            if observer is not None and not callable(observer):
                raise TypeError("baseline transition observer must be callable or None")
            if observer is None:
                # Detaching is a lifecycle boundary.  Make any unresolved
                # sidecar rows explicit before clearing them; never let an
                # observer swap manufacture a transition or lose one without
                # an audit reason.
                cls.drop_pending_transition_boundaries("observer-cleared")
                # A provider is scoped to the recorder that owns the
                # observer.  Do not leak one run's ExamSave/catalog binding
                # into a later baseline task.
                cls._unified_legal_action_snapshot_provider = None
            else:
                # A recorder belongs to one baseline task.  Never let a stale
                # boundary from a prior task leak into the next run.  The
                # previous run's diagnostics are not part of this task.
                cls.drop_pending_transition_boundaries("observer-replaced")
                cls._transition_drop_log.clear()
            cls._transition_observer = observer

        @classmethod
        def set_unified_legal_action_snapshot_provider(
            cls,
            provider: Callable[[Any], Any] | None,
        ) -> None:
            """Bind a read-only exact unified-candidate provider.

            The callable receives the immutable typed ExamSave evidence that
            was captured immediately before Maa's action.  A provider result
            is used only when it explicitly carries ``complete=True`` and
            ``candidate_set_kind=unified``.  No fallback candidate is inferred
            from the selected action, inventory, or detector frame.
            """

            if provider is not None and not callable(provider):
                raise TypeError(
                    "unified legal action snapshot provider must be callable or None"
                )
            cls._unified_legal_action_snapshot_provider = provider

        @classmethod
        def unified_legal_action_snapshot_provider(cls) -> Callable[[Any], Any] | None:
            """Return the currently bound sidecar provider for diagnostics."""

            return cls._unified_legal_action_snapshot_provider

        @classmethod
        def transition_drop_log(cls) -> tuple[dict[str, Any], ...]:
            """Return immutable copies of sidecar boundaries dropped so far."""

            return tuple(dict(value) for value in cls._transition_drop_log)

        @classmethod
        def _record_transition_drop(
            cls,
            boundary: tuple[Any, ...],
            reason: str,
            after: Any | None = None,
        ) -> None:
            before = boundary[0] if boundary else None
            chosen = boundary[1] if len(boundary) > 1 else None
            before_digest = None
            after_digest = None
            try:
                before_digest = before.digest()
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass
            try:
                after_digest = None if after is None else after.digest()
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass
            cls._transition_drop_log.append(
                {
                    "reason": str(reason),
                    "action_kind": _member(chosen, "kind", "play"),
                    "guid": _member(chosen, "guid", None),
                    "card_id": _member(chosen, "card_id", None),
                    "drink_id": _member(chosen, "drink_id", None),
                    "before_digest": before_digest,
                    "after_digest": after_digest,
                }
            )

        @classmethod
        def drop_pending_transition_boundaries(
            cls,
            reason: str = "explicit-drop",
        ) -> tuple[dict[str, Any], ...]:
            """Drop unresolved boundaries with a durable, typed reason."""

            dropped: list[dict[str, Any]] = []
            while cls._pending_transition_boundaries:
                _action, boundary = cls._pending_transition_boundaries.pop(0)
                cls._record_transition_drop(boundary, reason)
                dropped.append(dict(cls._transition_drop_log[-1]))
            return tuple(dropped)

        @classmethod
        def _pending_after_disposition(
            cls,
            boundary: tuple[Any, ...],
            after: Any,
        ) -> str:
            """Classify one settled read without ever treating it as a card."""

            if not boundary:
                return "drop:malformed-boundary"
            before = boundary[0]
            try:
                before_digest = before.digest()
                after_digest = after.digest()
                same_stage = (
                    after.run_id == before.run_id
                    and after.step_context_id == before.step_context_id
                    and after.session_transition_id == before.session_transition_id
                    and after.source_path == before.source_path
                )
                settled = bool(
                    after.state.is_native_actionable_settled
                    or cls._terminal_state(after.state)
                )
            except (AttributeError, TypeError, ValueError, RuntimeError):
                return "drop:malformed-after-evidence"
            if not same_stage:
                return "drop:after-stage-mismatch"
            if after_digest == before_digest:
                return "keep"
            if not settled:
                return "keep"
            # A changed, settled save that did not satisfy the explicit card
            # authority is recoverable only by a future action-time snapshot;
            # retaining it at the FIFO head would cause later actions to be
            # paired with this unrelated drink/UI save.  Drop and record it.
            return "drop:settled-after-without-card-commit"

        @classmethod
        def flush_transition_observer(cls) -> int:
            """Resolve every FIFO boundary with a distinct current save.

            Each iteration performs one non-blocking read and consumes that
            snapshot at most once.  This allows several already-settled
            boundaries to drain in one frame when the evidence provider
            exposes them in order, while preventing one drink/UI/terminal
            after-state from being reused for another card action.
            """

            if cls._transition_observer is None or not cls._pending_transition_boundaries:
                return 0
            resolved = 0
            consumed_after: set[tuple[Any, ...]] = set()
            while cls._pending_transition_boundaries:
                after = _active_exam_evidence()
                if after is None:
                    break
                after_key = _evidence_snapshot_key(after)
                if after_key in consumed_after:
                    # The provider still exposes the same immutable save.  A
                    # second pending action cannot borrow it.
                    break
                consumed_after.add(after_key)
                action, boundary = cls._pending_transition_boundaries[0]
                try:
                    emitted = bool(action._emit_exact_transition(boundary, after))
                except Exception:
                    emitted = False
                if emitted:
                    cls._pending_transition_boundaries.pop(0)
                    resolved += 1
                    continue
                disposition = cls._pending_after_disposition(boundary, after)
                if disposition.startswith("drop:"):
                    cls._pending_transition_boundaries.pop(0)
                    cls._record_transition_drop(
                        boundary,
                        disposition[5:],
                        after,
                    )
                    continue
                # Unchanged or still-animating evidence remains at the FIFO
                # head for a later frame; do not inspect later boundaries out
                # of order.
                break
            return resolved

        @staticmethod
        def _stage_after_is_settled(
            before: Any,
            after: Any,
        ) -> bool:
            """Require one changed, settled ExamSave stage for any action."""

            try:
                return bool(
                    after.digest() != before.digest()
                    and (
                        after.state.is_native_actionable_settled
                        or GkmsToolCompletionSafeProduceCardsAuto._terminal_state(
                            after.state
                        )
                    )
                    and after.run_id == before.run_id
                    and after.step_context_id == before.step_context_id
                    and after.session_transition_id == before.session_transition_id
                    and after.source_path == before.source_path
                )
            except (AttributeError, TypeError, ValueError, RuntimeError):
                return False

        @staticmethod
        def _native_drink_inventory(value: Any) -> tuple[tuple[int, str, str], ...] | None:
            """Read the ordered native drink inventory from one ExamSave."""

            opaque = _opaque_runtime_fields(value)
            if opaque is None:
                return None
            rows = opaque.get("drinkList")
            if not isinstance(rows, list):
                return None
            result: list[tuple[int, str, str]] = []
            for index, row in enumerate(rows):
                if not isinstance(row, Mapping):
                    return None
                drink_id = row.get("_id", row.get("id", row.get("drink_id")))
                if not isinstance(drink_id, str) or not drink_id:
                    return None
                raw_instance = row.get(
                    "_instanceId",
                    row.get(
                        "instance_id",
                        row.get("instanceId", row.get("_uid", row.get("uid"))),
                    ),
                )
                instance_id = (
                    str(raw_instance)
                    if isinstance(raw_instance, (str, int))
                    and not isinstance(raw_instance, bool)
                    and str(raw_instance)
                    else f"slot-{index}"
                )
                result.append((index, instance_id, drink_id))
            return tuple(result)

        @staticmethod
        def _candidate_mapping(value: Any) -> Mapping[str, Any] | None:
            """Detach one provider candidate without inventing its identity."""

            if isinstance(value, Mapping):
                return dict(value)
            to_dict = getattr(value, "to_dict", None)
            if callable(to_dict):
                try:
                    mapped = to_dict()
                except (TypeError, ValueError, RuntimeError):
                    return None
                return dict(mapped) if isinstance(mapped, Mapping) else None
            return None

        @classmethod
        def _unified_action_candidates(
            cls,
            before: Any,
        ) -> tuple[tuple[dict[str, Any], ...], Any] | None:
            """Read one complete unified set from the bound exact provider.

            This is intentionally a strict adapter.  A per-kind enumeration,
            a bare list, or a provider result without ``complete=True`` is an
            abstention and leaves the historical Maa sidecar path intact.
            """

            provider = cls._unified_legal_action_snapshot_provider
            if provider is None:
                return None
            try:
                result = provider(before)
            except Exception:
                # The candidate sidecar must never alter Maa's completion
                # result when a catalog/evidence read is temporarily blocked.
                return None
            complete = (
                result.get("complete") is True
                if isinstance(result, Mapping)
                else getattr(result, "complete", False) is True
            )
            kind = (
                result.get("candidate_set_kind")
                if isinstance(result, Mapping)
                else getattr(result, "candidate_set_kind", None)
            )
            if not complete or (kind is not None and kind != "unified"):
                return None
            raw = (
                result.get("candidates", result.get("legal_candidates"))
                if isinstance(result, Mapping)
                else getattr(
                    result,
                    "candidates",
                    getattr(result, "legal_candidates", None),
                )
            )
            if not isinstance(raw, (list, tuple)) or not raw:
                return None
            values: list[dict[str, Any]] = []
            for value in raw:
                mapped = cls._candidate_mapping(value)
                if mapped is None:
                    return None
                values.append(mapped)
            identities = tuple(
                value.get("action_id") for value in values
            )
            if any(not isinstance(value, str) or not value for value in identities):
                return None
            if len(identities) != len(set(identities)):
                return None
            if "END_TURN" not in identities:
                return None
            authority = (
                result.get("authority")
                if isinstance(result, Mapping)
                else getattr(result, "authority", None)
            )
            if not isinstance(authority, str) or not authority:
                return None
            return tuple(values), result

        @classmethod
        def _boundary_candidates_metadata(
            cls,
            boundary: tuple[Any, ...],
        ) -> tuple[str, str, bool]:
            """Return ``(candidate kind, authority, ready)`` for one boundary."""

            if len(boundary) < 4:
                return "unknown", "", False
            result = boundary[3]
            complete = (
                result.get("complete") is True
                if isinstance(result, Mapping)
                else getattr(result, "complete", False) is True
            )
            kind = (
                result.get("candidate_set_kind")
                if isinstance(result, Mapping)
                else getattr(result, "candidate_set_kind", "unified")
            )
            authority = (
                result.get("authority")
                if isinstance(result, Mapping)
                else getattr(result, "authority", "")
            )
            if complete and kind in (None, "unified") and isinstance(authority, str):
                return "unified", authority, True
            return "unknown", "", False

        @classmethod
        def _native_drink_log_delta(
            cls,
            before: Any,
            after: Any,
        ) -> str | None:
            """Return one appended native drink ID, or abstain.

            A drink task can append effect/forced-card rows around its own
            user-log cell.  Compare the immutable raw prefix and inspect the
            explicit ``pdrink_`` trigger.  The caller separately proves the
            ordered inventory RemoveAt on the same settled boundary, so card
            rows caused by that drink do not invalidate its action identity.
            """

            for field_name in ("userPlayLogList", "playLogList"):
                previous = _card_action_log_rows(before, field_name)
                current = _card_action_log_rows(after, field_name)
                if previous is None or current is None or len(current) <= len(previous):
                    continue
                if current[: len(previous)] != previous:
                    continue
                drink_ids: list[str] = []
                for row in current[len(previous) :]:
                    trigger_id = _member(row, "_triggerId", _member(row, "trigger_id", ""))
                    if isinstance(trigger_id, str) and trigger_id.startswith("pdrink_"):
                        drink_ids.append(trigger_id)
                if len(drink_ids) == 1:
                    return drink_ids[0]
            return None

        @staticmethod
        def _drink_action(
            slot_index: int,
            instance_id: str,
            drink_id: str,
        ) -> dict[str, Any]:
            return {
                "kind": "drink",
                "slot_index": slot_index,
                "instance_id": instance_id,
                "drink_id": drink_id,
                "selected_card_guid": "",
                "action_id": (
                    f"DRINK:{slot_index}:{instance_id}:{drink_id}"
                ),
            }

        def _exact_end_turn_boundary(
            self,
            *,
            before_override: Any | None = None,
        ) -> tuple[Any, ...] | None:
            """Capture the only legal action on an upstream skip branch."""

            before = (
                before_override
                if before_override is not None
                else _active_exam_evidence()
            )
            if before is None:
                return None
            try:
                state = before.state
                if (
                    not state.is_native_actionable_settled
                    or state.remain_turn <= 0
                    or self._terminal_state(state)
                ):
                    return None
            except (AttributeError, TypeError, ValueError):
                return None
            action = {
                "kind": "end_turn",
                "action_id": "END_TURN",
            }
            unified = type(self)._unified_action_candidates(before)
            if unified is not None:
                candidates, snapshot = unified
                end_candidates = tuple(
                    value
                    for value in candidates
                    if value.get("kind") in {"end_turn", "turn-end", "turn_end"}
                    or value.get("action_id") == "END_TURN"
                )
                if len(end_candidates) != 1:
                    # Ambiguous or missing end-turn identity cannot be
                    # reconciled with this skip task without guessing.
                    return None
                action = dict(end_candidates[0])
                return before, action, candidates, snapshot
            # The upstream branch reaches this task only after every visible
            # card was classified unusable (or after an exact native empty
            # hand).  Keep the candidate set explicit and minimal; do not
            # invent card candidates from a stale detector frame.
            return before, action, (action,)

        def _exact_drink_boundary(
            self,
            *,
            before_override: Any | None = None,
        ) -> tuple[Any, ...] | None:
            """Freeze one ordered drink menu before Maa consumes its first slot."""

            before = (
                before_override
                if before_override is not None
                else _active_exam_evidence()
            )
            if before is None:
                return None
            try:
                if not before.state.is_native_actionable_settled:
                    return None
            except (AttributeError, TypeError, ValueError):
                return None
            inventory = self._native_drink_inventory(before)
            if not inventory:
                return None
            unified = type(self)._unified_action_candidates(before)
            if unified is not None:
                candidates, snapshot = unified
                slot, instance_id, drink_id = inventory[0]
                matching = tuple(
                    value
                    for value in candidates
                    if value.get("kind") in {"drink", "use-drink", "use_drink"}
                    and value.get("slot_index", value.get("slot")) == slot
                    and value.get("drink_id") == drink_id
                    and (
                        value.get("instance_id", value.get("instance"))
                        in {None, "", instance_id}
                    )
                )
                # A target-select drink has one candidate per target GUID.
                # ProduceUseDrink's current task does not expose that target,
                # so recording one of several variants would fabricate a
                # selected card.  Wait for a future target-aware action hook.
                if len(matching) != 1:
                    return None
                return before, dict(matching[0]), candidates, snapshot
            candidates = tuple(
                self._drink_action(slot, instance_id, drink_id)
                for slot, instance_id, drink_id in inventory
            )
            # ProduceUseDrink's bundled pipeline targets the first ordered
            # drink slot.  The native trigger in the after-save remains the
            # action authority; this pre-bound slot only freezes legality and
            # prevents a later inventory reorder from changing the row.
            return before, candidates[0], candidates

        def _emit_exact_end_turn_transition(
            self,
            boundary: tuple[Any, ...],
            after: Any | None = None,
        ) -> bool:
            observer = type(self)._transition_observer
            if observer is None or len(boundary) < 2:
                return False
            before, action = boundary[0], boundary[1]
            if not isinstance(action, Mapping) or action.get("kind") != "end_turn":
                return False
            if after is None:
                after = _active_exam_evidence()
            if after is None or not self._stage_after_is_settled(before, after):
                return False
            try:
                before_turn = before.state.current_turn
                after_turn = after.state.current_turn
                before_remain = before.state.remain_turn
                after_remain = after.state.remain_turn
                before_count = before.state.exam_card_play_count
                after_count = after.state.exam_card_play_count
                if (
                    type(before_turn) is not int
                    or type(after_turn) is not int
                    or type(before_remain) is not int
                    or type(after_remain) is not int
                    or type(before_count) is not int
                    or type(after_count) is not int
                    or after_remain >= before_remain
                    or after_count != before_count
                ):
                    return False
                turn_delta = after_turn - before_turn
                terminal = self._terminal_state(after.state)
                if turn_delta not in {0, 1}:
                    return False
                if not terminal and turn_delta != 1:
                    return False
                if terminal and after_remain != 0:
                    return False
            except (AttributeError, TypeError, ValueError):
                return False

            native_log_used = False
            for field_name in ("userPlayLogList", "playLogList"):
                previous = _expanded_card_action_log(before, field_name)
                current = _expanded_card_action_log(after, field_name)
                if previous is None or current is None:
                    continue
                native_log_used = True
                if len(current) != len(previous) or current != previous:
                    return False
            candidate_set_kind, candidate_authority, full_rl_ready = (
                type(self)._boundary_candidates_metadata(boundary)
            )
            if not full_rl_ready:
                candidate_set_kind = "end_turn_only"
                candidate_authority = "maa-native-no-usable-card-end-turn"
            metadata = {
                "evidence_before_digest": before.digest(),
                "evidence_after_digest": after.digest(),
                "source_before_sha256": before.source_sha256,
                "source_after_sha256": after.source_sha256,
                "step_context_id": before.step_context_id,
                "session_transition_id": before.session_transition_id,
                "candidate_authority": candidate_authority,
                "candidate_set_kind": candidate_set_kind,
                "full_rl_policy_ready": full_rl_ready,
                "transition_scope": "exact-dynamics",
                "commit_authority": "native-turn-progress",
                "native_user_card_log_checked": native_log_used,
            }
            observer(
                {
                    "state_before": before.state.to_dict(),
                    "legal_candidates": [
                        dict(value)
                        if isinstance(value, Mapping)
                        else dict(type(self)._candidate_mapping(value) or {})
                        for value in boundary[2]
                    ],
                    "action": dict(action),
                    "state_after": after.state.to_dict(),
                    "reward": after.state.score - before.state.score,
                    "terminal": terminal,
                    "metadata": metadata,
                }
            )
            return True

        def _emit_exact_drink_transition(
            self,
            boundary: tuple[Any, ...],
            after: Any | None = None,
        ) -> bool:
            observer = type(self)._transition_observer
            if observer is None or len(boundary) < 3:
                return False
            before, action, candidates = boundary[0], boundary[1], boundary[2]
            if not isinstance(action, Mapping) or action.get("kind") != "drink":
                return False
            if after is None:
                after = _active_exam_evidence()
            if after is None or not self._stage_after_is_settled(before, after):
                return False
            drink_id = action.get("drink_id")
            if not isinstance(drink_id, str) or not drink_id:
                return False
            if self._native_drink_log_delta(before, after) != drink_id:
                return False
            before_inventory = self._native_drink_inventory(before)
            after_inventory = self._native_drink_inventory(after)
            slot_index = action.get("slot_index")
            instance_id = action.get("instance_id")
            if (
                before_inventory is None
                or after_inventory is None
                or type(slot_index) is not int
                or not 0 <= slot_index < len(before_inventory)
                or before_inventory[slot_index]
                != (slot_index, instance_id, drink_id)
            ):
                return False
            expected_after_ids = tuple(
                value[2]
                for value in (
                    *before_inventory[:slot_index],
                    *before_inventory[slot_index + 1 :],
                )
            )
            if tuple(value[2] for value in after_inventory) != expected_after_ids:
                return False
            if not isinstance(candidates, (list, tuple)) or not candidates:
                return False
            if any(not isinstance(value, Mapping) for value in candidates):
                return False
            candidate_set_kind, candidate_authority, full_rl_ready = (
                type(self)._boundary_candidates_metadata(boundary)
            )
            if not full_rl_ready:
                candidate_set_kind = "drink_only"
                candidate_authority = "maa-native-ordered-drink-inventory"
            metadata = {
                "evidence_before_digest": before.digest(),
                "evidence_after_digest": after.digest(),
                "source_before_sha256": before.source_sha256,
                "source_after_sha256": after.source_sha256,
                "step_context_id": before.step_context_id,
                "session_transition_id": before.session_transition_id,
                "candidate_authority": candidate_authority,
                "candidate_set_kind": candidate_set_kind,
                "full_rl_policy_ready": full_rl_ready,
                "transition_scope": "exact-dynamics",
                "commit_authority": (
                    "native-userPlayLogList-drink+ordered-inventory-remove"
                ),
            }
            observer(
                {
                    "state_before": before.state.to_dict(),
                    "legal_candidates": [dict(value) for value in candidates],
                    "action": dict(action),
                    "state_after": after.state.to_dict(),
                    "reward": after.state.score - before.state.score,
                    "terminal": self._terminal_state(after.state),
                    "metadata": metadata,
                }
            )
            return True

        def _emit_exact_transition(
            self,
            boundary: tuple[Any, ...],
            after: Any | None = None,
        ) -> bool:
            """Dispatch one frozen boundary without changing card semantics."""

            if len(boundary) >= 2 and isinstance(boundary[1], Mapping):
                kind = boundary[1].get("kind")
                if kind == "end_turn":
                    return self._emit_exact_end_turn_transition(boundary, after)
                if kind == "drink":
                    return self._emit_exact_drink_transition(boundary, after)
            return self._emit_exact_card_transition(boundary, after)

        class _TransitionRecordingContext:
            """Forward Maa Context while observing semantic sidecar tasks."""

            _gkms_transition_proxy = True

            def __init__(self, context: Any, action: Any) -> None:
                self._context = context
                self._action = action

            def __getattr__(self, name: str) -> Any:
                return getattr(self._context, name)

            @staticmethod
            def _task_succeeded(result: Any) -> bool:
                succeeded = getattr(result, "succeeded", None)
                return True if succeeded is None else bool(succeeded)

            def run_task(self, node: str, *args: Any, **kwargs: Any) -> Any:
                # Resolve the prior action before another semantic task can
                # be submitted.  This is essential for Maa's back-to-back
                # drink loop: otherwise one final save contains several
                # appended drink rows and no individual state_after can be
                # assigned to the FIFO boundaries.
                type(self._action).flush_transition_observer()
                boundary = None
                if node == "ProduceRecognitionSkipRound":
                    try:
                        entry_before = self._action._take_initial_exam_boundary()
                        boundary = self._action._exact_end_turn_boundary(
                            before_override=entry_before
                        )
                        if boundary is None and entry_before is not None:
                            # The entry read may have crossed the atomic
                            # stage switch.  It is safer to retry the normal
                            # action-time read than to lose the first legal
                            # action or pair it with a stale stage.
                            boundary = self._action._exact_end_turn_boundary()
                    except Exception:
                        boundary = None
                elif node == "ProduceUseDrink":
                    try:
                        entry_before = self._action._take_initial_exam_boundary()
                        boundary = self._action._exact_drink_boundary(
                            before_override=entry_before
                        )
                        if boundary is None and entry_before is not None:
                            # See the skip branch above: retain the existing
                            # action-time proof when the frozen entry state is
                            # not compatible with this semantic task.
                            boundary = self._action._exact_drink_boundary()
                    except Exception:
                        boundary = None
                result = self._context.run_task(node, *args, **kwargs)
                if boundary is not None and self._task_succeeded(result):
                    try:
                        if node == "ProduceRecognitionSkipRound":
                            self._action._observe_exact_end_turn_transition(boundary)
                        elif node == "ProduceUseDrink":
                            self._action._observe_exact_drink_transition(boundary)
                    except Exception:
                        # This recorder is a sidecar; a temporary evidence
                        # failure must never change Maa's action result.
                        pass
                return result

            def run_recognition(
                self,
                node: str,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                # `_wait_until_playable` polls recognition between drinks and
                # after END_TURN.  Use those existing read-only polls to close
                # the pending exact boundary; no extra wait or input is added.
                type(self._action).flush_transition_observer()
                return self._context.run_recognition(node, *args, **kwargs)

        def run(self, context: Any, argv: Any) -> bool:
            """Run upstream policy through a non-invasive task observer."""

            # Maa keeps the registered CustomAction instance alive across
            # audition tasks (and often across cultivations).  The entry
            # snapshot is scoped to one exam, so a prior run must not leave
            # ``_initial_exam_boundary_captured`` latched for the next one.
            self._latest_card_results = ()
            self._initial_exam_boundary = None
            self._initial_exam_boundary_captured = False
            return super().run(self._TransitionRecordingContext(context, self), argv)

        def _observe_exact_end_turn_transition(
            self,
            boundary: tuple[Any, ...] | None,
        ) -> None:
            if boundary is None or type(self)._transition_observer is None:
                return
            if self._emit_exact_end_turn_transition(boundary):
                return
            if self._has_pending_boundary(boundary):
                return
            type(self)._pending_transition_boundaries.append((self, boundary))

        def _observe_exact_drink_transition(
            self,
            boundary: tuple[Any, ...] | None,
        ) -> None:
            if boundary is None or type(self)._transition_observer is None:
                return
            if self._emit_exact_drink_transition(boundary):
                return
            if self._has_pending_boundary(boundary):
                return
            type(self)._pending_transition_boundaries.append((self, boundary))

        @staticmethod
        def _has_pending_boundary(boundary: tuple[Any, ...]) -> bool:
            """Avoid retry clicks creating two rows for one unchanged save."""

            if len(boundary) < 2:
                return False
            before, action = boundary[0], boundary[1]
            try:
                before_digest = before.digest()
            except (AttributeError, TypeError, ValueError, RuntimeError):
                return False
            action_id = _member(action, "action_id", None)
            if not isinstance(action_id, str) or not action_id:
                return False
            for _owner, pending in type(self)._pending_transition_boundaries:
                if len(pending) < 2:
                    continue
                try:
                    if pending[0].digest() != before_digest:
                        continue
                except (AttributeError, TypeError, ValueError, RuntimeError):
                    continue
                if _member(pending[1], "action_id", None) == action_id:
                    return True
            return False

        @staticmethod
        def _terminal_state(state: Any) -> bool:
            runtime = getattr(state, "root_runtime", None)
            return bool(runtime is not None and runtime.is_exam_end_complete)

        def _capture_initial_exam_boundary(self) -> None:
            """Freeze the next settled turn-one ExamSave before user input.

            ``ProduceCardsAuto`` waits for a playable frame before it enters
            its drink loop.  Reading the save only from ``_play_a_card`` or
            ``ProduceUseDrink`` can therefore miss turn-one actions while the
            first frame is being consumed.  This sidecar snapshot changes no
            Maa input; it is consumed by the next semantic action and may be
            recaptured by the existing confirmation poll while turn one still
            has no played cards.
            """

            if self._initial_exam_boundary_captured:
                return
            if type(self)._transition_observer is None:
                return
            evidence = _active_exam_evidence()
            if evidence is None:
                return
            try:
                if not evidence.state.is_native_actionable_settled:
                    return
                # A stale ExamSave from the preceding audition can still be
                # settled while the new stage's first frame is entering.  The
                # entry snapshot exists specifically to recover stage-start
                # actions, so require the native stage-start counters rather
                # than consuming a different settled stage as the first
                # boundary.  Later/resumed action-time reads keep the normal
                # recorder path unchanged.
                if (
                    evidence.state.current_turn != 1
                    or evidence.state.exam_card_play_count != 0
                ):
                    return
            except (AttributeError, TypeError, ValueError):
                return
            self._initial_exam_boundary = evidence
            self._initial_exam_boundary_captured = True

        def _take_initial_exam_boundary(self) -> Any | None:
            """Consume the current entry state exactly once.

            The flag is a capture guard, not a lifetime marker.  A semantic
            action can consume the snapshot without producing an exact row
            (for example, when the native save is still in flight).  Clear
            the guard together with the snapshot so the existing playable
            poll can capture the next settled turn-one state before another
            drink/card action.  The state predicate in
            ``_capture_initial_exam_boundary`` prevents this from ever
            reusing a later turn as an entry boundary.
            """

            evidence = self._initial_exam_boundary
            self._initial_exam_boundary = None
            self._initial_exam_boundary_captured = False
            return evidence

        def _exact_card_boundary(
            self,
            box: list,
            *,
            before_override: Any | None = None,
        ) -> tuple[Any, ...] | None:
            """Bind Maa's complete detector result to one settled ExamSave hand."""

            before = (
                before_override
                if before_override is not None
                else _active_exam_evidence()
            )
            if before is None or not before.state.is_native_actionable_settled:
                return None
            try:
                from .audition_legal_candidate_gate import gate_legal_candidates

                gate = gate_legal_candidates(
                    before,
                    {
                        "timestamp": time.time(),
                        "width": CANONICAL_CAPTURE_WIDTH,
                        "height": CANONICAL_CAPTURE_HEIGHT,
                        "settled": True,
                        "evidence_digest": before.digest(),
                    },
                    self._latest_card_results,
                )
                target = tuple(int(value) for value in box)
                matches = tuple(
                    candidate
                    for candidate in gate.legal_candidates
                    if tuple(candidate.box) == target
                )
            except (AttributeError, KeyError, TypeError, ValueError):
                return None
            if not gate.accepted or len(matches) != 1:
                return None
            unified = type(self)._unified_action_candidates(before)
            if unified is not None:
                candidates, snapshot = unified
                selected_guid = getattr(matches[0], "guid", None)
                selected = tuple(
                    value
                    for value in candidates
                    if value.get("kind") in {"play", "card", "use-hand", "use_hand"}
                    and value.get("guid", value.get("card_guid")) == selected_guid
                )
                if len(selected) != 1:
                    # The native root set must contain the exact detector
                    # selection.  A missing/ambiguous GUID is a flow-level
                    # abstention, never a card-only fallback.
                    return None
                return before, matches[0], candidates, snapshot
            # Freeze the complete candidate set at the same settled boundary
            # as the selected card.  Re-running the detector after the click
            # can see an already-mutated hand or a different animation frame.
            return before, matches[0], tuple(gate.legal_candidates)

        def _observe_exact_card_transition(
            self,
            boundary: tuple[Any, ...] | None,
        ) -> None:
            if boundary is None or type(self)._transition_observer is None:
                return
            # Try the cheap immediate path first.  If the atomic save is
            # still in flight, defer without sleeping; the next card read
            # (and the final completion read) will resolve it.
            if self._emit_exact_card_transition(boundary):
                return
            type(self)._pending_transition_boundaries.append((self, boundary))

        def _emit_exact_card_transition(
            self,
            boundary: tuple[Any, ...],
            after: Any | None = None,
        ) -> bool:
            """Publish one frozen boundary only with card-commit authority."""

            observer = type(self)._transition_observer
            if observer is None or len(boundary) < 2:
                return False
            before, chosen = boundary[0], boundary[1]
            if after is None:
                after = _active_exam_evidence()
            if after is None:
                return False
            try:
                same_stage = (
                    after.digest() != before.digest()
                    and (
                        after.state.is_native_actionable_settled
                        or self._terminal_state(after.state)
                    )
                    and after.run_id == before.run_id
                    and after.step_context_id == before.step_context_id
                    and after.session_transition_id == before.session_transition_id
                    and after.source_path == before.source_path
                )
            except (AttributeError, TypeError, ValueError, RuntimeError):
                return False
            if not same_stage:
                return False

            hand_by_guid = {
                card.guid: card for card in before.state.zones.hand
            }
            candidate_values = boundary[2] if len(boundary) >= 3 else ()
            if not candidate_values:
                # Backwards-compatible handling for a boundary supplied by a
                # test/older caller.  Production boundaries always freeze
                # candidates at action time above.
                from .audition_legal_candidate_gate import gate_legal_candidates

                gate = gate_legal_candidates(
                    before,
                    {
                        "timestamp": time.time(),
                        "width": CANONICAL_CAPTURE_WIDTH,
                        "height": CANONICAL_CAPTURE_HEIGHT,
                        "settled": True,
                        "evidence_digest": before.digest(),
                    },
                    self._latest_card_results,
                )
                if not gate.accepted:
                    return False
                candidate_values = gate.legal_candidates
            legal = []
            for candidate in candidate_values:
                if isinstance(candidate, Mapping):
                    # A unified provider owns the complete play+drink+end
                    # surface.  Preserve each typed identity exactly; do not
                    # flatten drink targets or END_TURN into card rows.
                    kind = candidate.get("kind")
                    if kind in {"play", "card", "use-hand", "use_hand"}:
                        guid = candidate.get("guid", candidate.get("card_guid"))
                        if not isinstance(guid, str) or not guid:
                            return False
                        card = hand_by_guid.get(guid)
                        if card is None:
                            return False
                        value = dict(candidate)
                        value.setdefault("kind", "play")
                        value.setdefault("guid", guid)
                        value.setdefault("card_guid", guid)
                        value.setdefault("card_id", getattr(card, "card_id", None))
                        value.setdefault("upgrade", getattr(card, "effective_upgrade", None))
                        legal.append(value)
                    elif kind in {"drink", "use-drink", "use_drink", "end_turn", "turn-end", "turn_end"}:
                        legal.append(dict(candidate))
                    else:
                        return False
                    continue
                card = hand_by_guid.get(getattr(candidate, "guid", None))
                if card is None:
                    return False
                legal.append(
                    {
                        "kind": "play",
                        "guid": candidate.guid,
                        "card_id": candidate.card_id,
                        "slot": candidate.slot,
                        "upgrade": card.effective_upgrade,
                    }
                )
            selected = hand_by_guid.get(chosen.guid)
            if selected is None:
                return False
            commit_proof = _card_action_commit_proof(before, after, chosen)
            if commit_proof is None:
                # Digest/context/settled are intentionally insufficient.  A
                # concurrent drink or UI-owned save can satisfy all three
                # predicates without submitting this card.
                return False
            candidate_set_kind, candidate_authority, full_rl_ready = (
                type(self)._boundary_candidates_metadata(boundary)
            )
            if not full_rl_ready:
                candidate_set_kind = "card_only"
                candidate_authority = "maa-complete-card-detector+typed-examsave"
            observer(
                {
                    "state_before": before.state.to_dict(),
                    "legal_candidates": legal,
                    "action": {
                        "kind": "play",
                        "guid": chosen.guid,
                        "card_id": chosen.card_id,
                        "slot": chosen.slot,
                        "upgrade": selected.effective_upgrade,
                    },
                    "state_after": after.state.to_dict(),
                    "reward": after.state.score - before.state.score,
                    "terminal": self._terminal_state(after.state),
                    "metadata": {
                        "evidence_before_digest": before.digest(),
                        "evidence_after_digest": after.digest(),
                        "source_before_sha256": before.source_sha256,
                        "source_after_sha256": after.source_sha256,
                        "step_context_id": before.step_context_id,
                        "session_transition_id": before.session_transition_id,
                        "candidate_authority": candidate_authority,
                        "candidate_set_kind": candidate_set_kind,
                        "full_rl_policy_ready": full_rl_ready,
                        "transition_scope": "exact-dynamics",
                        "commit_authority": commit_proof,
                    },
                }
            )
            return True

        def _get_card_info(self, results: list):
            # Resolve the previous card only after the action-time candidate
            # tuple has been retained, and before replacing the detector cache
            # with this frame's results.
            type(self).flush_transition_observer()
            # Keep a second entry capture point immediately before the first
            # detector result is consumed.  On a fast stage transition the
            # initial wait can observe a transient/unsettled ExamSave; the
            # first card frame is still before any new input and is therefore
            # the last safe place to obtain the turn-one boundary.
            self._capture_initial_exam_boundary()
            self._latest_card_results = tuple(results)
            # The card detector can label bottom HUD/info controls as cards on
            # a native zero-hand turn.  Force the upstream no-usable-card path
            # so it reaches the exact SKIP transaction instead of double-
            # clicking that false box and opening the deck overlay.
            if _native_exam_zero_hand_actionable() is True:
                return 0, 1, 0, [0, 0, 1, 1], [0, 0, 1, 1]
            return super()._get_card_info(results)

        def _play_a_card(self, context: Any, box: list) -> bool:
            # Telemetry is a sidecar: any capture failure drops only this row
            # and never changes Maa's established completion action.
            initial_before = self._take_initial_exam_boundary()
            try:
                boundary = self._exact_card_boundary(
                    box,
                    before_override=initial_before,
                )
                if boundary is None and initial_before is not None:
                    # If the entry evidence was captured during a stage
                    # rollover, retry against the current settled action
                    # state.  This fallback is generic and preserves the
                    # previous recorder behavior for every flow.
                    boundary = self._exact_card_boundary(box)
            except Exception:
                boundary = None
            result = super()._play_a_card(context, box)
            # Upstream may reject/drop the click.  In that case there was no
            # submitted action to observe; enqueueing the pre-click boundary
            # would later pair it with an unrelated settled save.
            if result:
                try:
                    self._observe_exact_card_transition(boundary)
                except Exception:
                    pass
            return result

        def _wait_until_playable(self, context: Any, confirmation_count: int = 1):
            count_playable = 0
            count_exit = 0
            no_cards_skip_attempts = 0
            no_cards_skip_wait_reads = 0
            zero_hand_overlay_attempts = 0
            zero_hand_overlay_wait_reads = 0
            while True:
                if context.tasker.stopping:
                    return False
                image = _wait_maa_screenshot_job(
                    context.tasker.controller.post_screencap()
                ).get()

                # A prior baseline worker can stop after it opened the native
                # end-turn confirmation but before the fixed Yes task ran.
                # Recover that exact Maa-owned confirmation before consulting
                # ExamSave settlement; no card/drink action is resubmitted.
                confirm = context.run_recognition("ProduceYes", image)
                if confirm is not None and bool(confirm.hit):
                    context.run_task("ProduceYes")
                    count_playable = 0
                    count_exit = 0
                    no_cards_skip_attempts = 0
                    no_cards_skip_wait_reads = 0
                    time.sleep(0.8)
                    continue

                cancel = context.run_recognition("ProduceCancel", image)
                decide = context.run_recognition("ProduceDecide", image)
                native_zero_hand = _native_exam_zero_hand_actionable()
                if (
                    cancel is not None
                    and bool(cancel.hit)
                    and decide is not None
                    and bool(decide.hit)
                ):
                    context.run_task("ProduceDecide")
                    count_playable = 0
                    count_exit = 0
                    time.sleep(0.8)
                    continue

                # A deck/card-detail overlay can remain open above an exact
                # zero-hand turn.  It exposes Cancel without Decide and blocks
                # the SKIP control behind it.  Close that visible owner first,
                # using the same bounded retry rule, then reassess a fresh
                # frame before submitting SKIP.
                if (
                    native_zero_hand is True
                    and cancel is not None
                    and bool(cancel.hit)
                    and (decide is None or not bool(decide.hit))
                ):
                    should_close = zero_hand_overlay_attempts == 0
                    if not should_close:
                        zero_hand_overlay_wait_reads += 1
                        should_close = (
                            zero_hand_overlay_wait_reads
                            >= self.NO_CARDS_SKIP_RETRY_EVERY_READS
                            and zero_hand_overlay_attempts
                            < self.NO_CARDS_SKIP_MAX_ATTEMPTS
                        )
                    if should_close:
                        context.run_task("ProduceCancel")
                        zero_hand_overlay_attempts += 1
                        zero_hand_overlay_wait_reads = 0
                    no_cards_skip_attempts = 0
                    no_cards_skip_wait_reads = 0
                    count_playable = 0
                    count_exit = 0
                    time.sleep(0.8)
                    continue
                zero_hand_overlay_attempts = 0
                zero_hand_overlay_wait_reads = 0

                # ExamSave is the authoritative hand/turn state.  Localized
                # OCR can miss the zero-hand sentence even while the native
                # SKIP control is actionable.  Under this exact state only,
                # use Maa's controller coordinate and the same bounded retry
                # transaction as the OCR route.
                if native_zero_hand is True:
                    should_submit = no_cards_skip_attempts == 0
                    if not should_submit:
                        no_cards_skip_wait_reads += 1
                        should_submit = (
                            no_cards_skip_wait_reads
                            >= self.NO_CARDS_SKIP_RETRY_EVERY_READS
                            and no_cards_skip_attempts
                            < self.NO_CARDS_SKIP_MAX_ATTEMPTS
                        )
                    if should_submit:
                        shape = getattr(image, "shape", ())
                        if len(shape) >= 2:
                            frame_height = int(shape[0])
                            frame_width = int(shape[1])
                            skip_x = (
                                self.ZERO_HAND_SKIP_POINT[0]
                                * frame_width
                                // 720
                            )
                            skip_y = (
                                self.ZERO_HAND_SKIP_POINT[1]
                                * frame_height
                                // 1280
                            )
                        else:
                            skip_x, skip_y = self.ZERO_HAND_SKIP_POINT
                        if confirmation_count == 1:
                            self._capture_initial_exam_boundary()
                        boundary = self._exact_end_turn_boundary(
                            before_override=self._take_initial_exam_boundary()
                        )
                        click_job = context.tasker.controller.post_click(
                            skip_x,
                            skip_y,
                        )
                        result = _wait_maa_job(
                            click_job,
                            timeout_seconds=MAA_INPUT_JOB_TIMEOUT_SECONDS,
                            label="zero-hand skip input",
                        )
                        succeeded = getattr(result, "succeeded", None)
                        if boundary is not None and (
                            succeeded is None or bool(succeeded)
                        ):
                            try:
                                self._observe_exact_end_turn_transition(boundary)
                            except Exception:
                                pass
                        no_cards_skip_attempts += 1
                        no_cards_skip_wait_reads = 0
                    count_playable = 0
                    count_exit = 0
                    time.sleep(0.8)
                    continue

                playable = context.run_recognition(
                    "ProduceRecognitionSkipRound", image
                )
                if playable is not None and bool(playable.hit):
                    no_cards = context.run_recognition(
                        "GkmsToolCompletionNoCards",
                        image,
                        pipeline_override={
                            "GkmsToolCompletionNoCards": {
                                "recognition": {
                                    "type": "OCR",
                                    "param": {
                                        "roi": [159, 947, 393, 97],
                                        "expected": [
                                            "手札のスキルカードが0枚です",
                                            "手牌中的技能卡為0張",
                                            "手牌中的技能卡为0张",
                                        ],
                                    },
                                },
                                "action": {"type": "DoNothing", "param": {}},
                            }
                        },
                    )
                    if no_cards is not None and bool(no_cards.hit):
                        # A skip button plus the localized zero-hand sentence
                        # is the exact native empty-hand state.  Unity can drop
                        # an individual SendMessage click, so hold the same
                        # transaction for four unchanged reads and allow one
                        # retry.  Never turn a stale frame into unbounded skip
                        # spam.
                        if no_cards_skip_attempts == 0:
                            if confirmation_count == 1:
                                self._capture_initial_exam_boundary()
                            context.run_task("ProduceRecognitionSkipRound")
                            no_cards_skip_attempts = 1
                            no_cards_skip_wait_reads = 0
                        else:
                            no_cards_skip_wait_reads += 1
                            if (
                                no_cards_skip_wait_reads
                                >= self.NO_CARDS_SKIP_RETRY_EVERY_READS
                                and no_cards_skip_attempts
                                < self.NO_CARDS_SKIP_MAX_ATTEMPTS
                            ):
                                if confirmation_count == 1:
                                    self._capture_initial_exam_boundary()
                                context.run_task("ProduceRecognitionSkipRound")
                                no_cards_skip_attempts += 1
                                no_cards_skip_wait_reads = 0
                        count_playable = 0
                        count_exit = 0
                        time.sleep(0.8)
                        continue
                    no_cards_skip_attempts = 0
                    no_cards_skip_wait_reads = 0
                    count_playable += 1
                    if count_playable >= confirmation_count:
                        # ``ProduceCardsAuto`` calls this same wait with
                        # confirmation_count=2 after each drink.  Capture on
                        # every settled confirmation, not only the first
                        # wait, so a failed/unfinished drink boundary can be
                        # replaced by the post-drink turn-one ExamSave.
                        self._capture_initial_exam_boundary()
                        return True
                else:
                    count_playable = 0
                    no_cards_skip_attempts = 0
                    no_cards_skip_wait_reads = 0

                health = context.run_recognition(
                    "ProduceRecognitionHealthFlag", image
                )
                if health is None or not bool(health.hit):
                    count_exit += 1
                    if count_exit >= 2:
                        return False
                else:
                    count_exit = 0

                if self._handle_move_cards(context, image):
                    count_playable = 0
                    count_exit = 0
                if context.tasker.stopping:
                    return False
                time.sleep(1.0)

    GkmsToolCompletionSafeProduceCardsAuto.__name__ = (
        "GkmsToolCompletionSafeProduceCardsAuto"
    )
    return GkmsToolCompletionSafeProduceCardsAuto


_NIA_OUTER_TEMPLATE_BATCHES = (
    # Global Home has two mutually exclusive Produce entries.  Keep both
    # original Maa templates in the read-only batch so a bound live runner can
    # distinguish "start a new Produce" from "continue the active Produce"
    # before Maa's generic Click_1 fallback is considered.
    ("home-new-produce", "home.png", (0, 0, 720, 1280), 0.0),
    ("home-active-produce", "home_1.png", (0, 0, 720, 1280), 0.0),
    ("event-action", "produce/NIA/activity.png", (0, 850, 720, 280), 0.92),
    ("event-consultation", "produce/NIA/chat.png", (0, 850, 720, 280), 0.92),
    ("event-dance", "produce/NIA/Da.png", (0, 850, 720, 280), 0.92),
    ("event-outing", "produce/NIA/go_out.png", (0, 850, 720, 280), 0.92),
    ("event-guide", "produce/NIA/guide.png", (0, 850, 720, 280), 0.92),
    ("event-visual", "produce/NIA/Vi.png", (0, 850, 720, 280), 0.92),
    ("event-vocal", "produce/NIA/Vo.png", (0, 850, 720, 280), 0.92),
    ("event-business", "produce/NIA/work.png", (0, 850, 720, 280), 0.92),
    # Exact ROIs used by Maa's ProduceChooseNIAEventAuto custom action.
    ("event-sp-0", "produce/sp.png", (70, 900, 80, 80), 0.70),
    ("event-sp-1", "produce/sp.png", (250, 900, 80, 80), 0.70),
    ("event-sp-2", "produce/sp.png", (430, 900, 80, 80), 0.70),
    ("event-rest", "produce/rest.png", (0, 0, 0, 0), 0.70),
    ("work-page", "produce/NIA/work_choose.png", (150, 350, 420, 260), 0.90),
    ("work-vo", "produce/choose_Vo.png", (0, 620, 720, 390), 0.90),
    ("work-da", "produce/choose_Da.png", (0, 620, 720, 390), 0.90),
    ("work-vi", "produce/choose_Vi.png", (0, 620, 720, 390), 0.90),
    ("work-none", "produce/choose_null.png", (0, 620, 720, 390), 0.90),
    ("work-start", "produce/NIA/work_start.png", (0, 850, 720, 430), 0.97),
    ("exam-failed", "produce/exam_failed.png", (0, 700, 720, 250), 0.90),
    ("challenge", "produce/challenge.png", (0, 1040, 720, 240), 0.92),
    ("last-challenge", "produce/last_challenge.png", (450, 320, 270, 610), 0.90),
    ("decide", "produce/decide.png", (0, 980, 720, 300), 0.90),
    ("shop-buy", "produce/shopping_buy.png", (0, 980, 720, 300), 0.90),
    ("shop-exchange", "produce/shopping_exchange.png", (0, 980, 720, 300), 0.90),
    ("shop-point-runout", "produce/point_runout.png", (0, 980, 720, 300), 0.90),
    ("shop-page", "produce/shopping.png", (30, 150, 670, 320), 0.90),
    ("shop-sale", "produce/Sale.png", (40, 460, 640, 570), 0.90),
    ("shop-exit", "produce/shop_exit.png", (0, 780, 720, 500), 0.90),
    ("guide-page", "produce/NIA/guide_choose.png", (20, 150, 680, 500), 0.90),
    ("drink-full", "produce/drink_full_on_keep_window.png", (0, 0, 720, 300), 0.90),
    ("drink-unchecked", "produce/unchecked_mark.png", (0, 200, 720, 880), 0.90),
    ("card-submit-strengthen", "produce/strengthen.png", (0, 650, 720, 630), 0.96),
    ("card-submit-delete", "produce/delete.png", (0, 650, 720, 630), 0.96),
    ("card-submit-copy", "produce/copy.png", (0, 650, 720, 630), 0.96),
    ("card-submit-exchange", "produce/exchange.png", (0, 650, 720, 630), 0.96),
    ("card-icon-strengthen", "produce/strengthen_icon.png", (0, 20, 220, 80), 0.90),
    ("card-icon-delete", "produce/delete_icon.png", (0, 20, 220, 80), 0.90),
    ("card-icon-copy", "produce/copy_icon.png", (0, 20, 220, 80), 0.90),
    ("card-icon-exchange", "produce/exchange_icon.png", (0, 20, 220, 80), 0.90),
    ("recommend", "produce/recommend.png", (40, 450, 640, 660), 0.90),
    ("event-recommend", "produce/event_recommend.png", (40, 450, 640, 660), 0.90),
    ("choice-move-cards", "produce/choose_move_cards.png", (20, 430, 680, 370), 0.90),
    ("mirror-mid1", "produce/NIA/mirror_1.png", (0, 600, 720, 580), 0.90),
    ("mirror-mid2", "produce/NIA/mirror_2.png", (0, 600, 720, 580), 0.90),
    ("mirror-final", "produce/NIA/mirror_3.png", (0, 600, 720, 580), 0.90),
    ("common-next", "next.png", (0, 700, 720, 580), 0.92),
    ("common-continue", "produce/continue.png", (0, 700, 720, 580), 0.92),
    # Original ProduceTakeRest recognition contract.  Keeping this in the
    # one-frame Maa batch prevents the live analyzer from rescanning a broad
    # bottom-screen ROI and confusing Continue/Buy with the rest modal.
    ("rest-confirm", "produce/rest_take.png", (360, 1100, 360, 180), 0.90),
    ("common-round-close", "produce/round_close_button.png", (0, 700, 720, 580), 0.92),
    # Traditional-Chinese communication failures render the same Maa X glyph
    # inside a long ``return to title`` pill instead of the original circular
    # close button.  The ordinary ProduceNIAButton score is 0.6625 on the
    # captured production frame, just below Maa's generic 0.7 threshold.  Keep
    # this recovery matcher confined to the button's left-icon lane; never
    # lower the shared template threshold across the rest of the screen.
    (
        "communication-return-title",
        "produce/round_close_button.png",
        (200, 1080, 160, 160),
        0.64,
    ),
    ("common-choose", "produce/choose.png", (0, 700, 720, 580), 0.92),
    ("common-ok", "produce/ok.png", (0, 700, 720, 580), 0.92),
    ("common-ok-alt", "produce/ok_1.png", (0, 700, 720, 580), 0.92),
    ("common-decide", "produce/decide.png", (0, 700, 720, 580), 0.92),
    ("common-skip-confirm", "produce/skip_confirm.png", (0, 700, 720, 580), 0.92),
    ("common-drink-no", "produce/drink_no.png", (0, 700, 720, 580), 0.92),
    ("common-shop-exit", "produce/shop_exit.png", (0, 700, 720, 580), 0.92),
    ("common-close", "produce/close.png", (0, 700, 720, 580), 0.92),
    ("common-start", "produce/start.png", (0, 700, 720, 580), 0.92),
    ("common-skip-chat", "produce/skip_chat.png", (0, 700, 720, 580), 0.92),
    ("common-skip-chat-alt", "produce/skip_chat_1.png", (0, 700, 720, 580), 0.92),
    ("common-yes", "produce/yes.png", (0, 700, 720, 580), 0.92),
    ("common-cards-get", "produce/cards_get.png", (0, 700, 720, 580), 0.92),
    ("common-drink-reject", "produce/drink_reject.png", (0, 700, 720, 580), 0.92),
    ("common-challenge", "produce/challenge.png", (0, 700, 720, 580), 0.92),
    ("common-retry", "produce/retry.png", (0, 700, 720, 580), 0.92),
    ("common-cancel", "produce/cancel.png", (0, 700, 720, 580), 0.92),
    ("common-finished", "produce/finished.png", (0, 700, 720, 580), 0.92),
    ("common-follow", "produce/follow.png", (0, 700, 720, 580), 0.92),
    # Read-only copies of Maa's native actionable-round gate.  All three
    # upstream template variants share the exact ROI/method/threshold from
    # ProduceRecognitionSkipRound; callers accept any one hit.
    ("exam-playable-0", "produce/skip_round.png", (590, 720, 130, 120), 0.0),
    ("exam-playable-1", "produce/skip_round_1.png", (590, 720, 130, 120), 0.0),
    ("exam-playable-2", "produce/skip_round_2.png", (590, 720, 130, 120), 0.0),
    # Exact Maa ProduceUseDrinkConfirm recognition.  The Plan2 dispatcher
    # consumes only its recognized box; it never guesses that the drink panel
    # opened from a timed click alone.
    ("drink-use", "produce/use_drink.png", (0, 1000, 720, 280), 0.70),
)

# Each entry below names the original MaaGakumasu node which owns the
# template's recognition contract.  The batch executes recognition only, but
# it must preserve Maa's own ROI, threshold, method and green-mask values.  A
# missing entry is an auxiliary observation (currently SP badges and legacy
# choice-page hints), never a replacement for an original Maa action gate.
_NIA_OUTER_TEMPLATE_SOURCE_NODES = {
    "home-new-produce": "ProduceStart",
    "home-active-produce": "ProduceContinue",
    **{
        label: "ProduceChooseNIAEventFlag"
        for label in (
            "event-action",
            "event-consultation",
            "event-dance",
            "event-outing",
            "event-guide",
            "event-visual",
            "event-vocal",
            "event-business",
        )
    },
    "work-page": "ProduceWorkFlag",
    "work-vo": "ProduceRecognitionWorkOptions",
    "work-da": "ProduceRecognitionWorkOptions",
    "work-vi": "ProduceRecognitionWorkOptions",
    "work-none": "ProduceRecognitionWorkOptions",
    "work-start": "ProduceWorkConfirm",
    "event-rest": "ProduceChooseRest",
    "exam-failed": "ProduceNIAFailedFlag",
    "challenge": "ProduceNIAFailedRetry",
    "last-challenge": "ProduceNIAFailedRetryLastChallenge",
    "decide": "ProduceNIAButton",
    "shop-buy": "ProduceShoppingBuy",
    "shop-exchange": "ProduceShoppingDecide",
    "shop-point-runout": "ProduceShoppingDecide",
    "shop-page": "ProduceShoppingFlag",
    "shop-sale": "ProduceShoppingBuySale",
    "shop-exit": "ProduceShoppingExit",
    "guide-page": "ProduceGuideFlag",
    "drink-full": "ProduceKeepDrinkFlag",
    "drink-unchecked": "ProduceRecognitionUncheckedMark",
    "card-submit-strengthen": "ProduceChooseStrengthen",
    "card-submit-delete": "ProduceChooseDeleteClick",
    "card-submit-copy": "ProduceChooseDeleteClick",
    "card-submit-exchange": "ProduceChooseDeleteClick",
    "card-icon-strengthen": "ProduceChooseStrengthenFlag",
    "card-icon-delete": "ProduceChooseDeleteFlag",
    "card-icon-copy": "ProduceChooseDeleteFlag",
    "card-icon-exchange": "ProduceChooseDeleteFlag",
    "recommend": "ProduceChooseRecommend",
    "event-recommend": "ProduceChooseEventRecommend",
    "choice-move-cards": "ProduceRecognitionChooseMoveCards",
    "mirror-mid1": "ProduceMirrorFlag",
    "mirror-mid2": "ProduceMirrorFlag",
    "mirror-final": "ProduceMirrorFlag",
    **{
        label: "ProduceNIAButton"
        for label in (
            "common-next",
            "common-round-close",
            "common-choose",
            "common-ok",
            "common-ok-alt",
            "common-decide",
            "common-skip-confirm",
            "common-drink-no",
            "common-shop-exit",
            "common-close",
            "common-retry",
        )
    },
    "common-continue": "ProduceChooseContinue",
    "rest-confirm": "ProduceTakeRest",
    "common-start": "ProduceGuideStart",
    "common-skip-chat": "ProduceSkip",
    "common-skip-chat-alt": "ProduceSkip",
    "common-yes": "ProduceYes",
    "common-cards-get": "ProduceChooseConfirm",
    "common-drink-reject": "ProduceChooseSkip",
    "common-challenge": "ProduceNIAFailedRetry",
    "common-cancel": "ProduceCancel",
    "common-finished": "ProduceFinished",
    "common-follow": "ProduceFollow",
    "drink-use": "ProduceUseDrinkConfirm",
    "exam-playable-0": "ProduceRecognitionSkipRound",
    "exam-playable-1": "ProduceRecognitionSkipRound",
    "exam-playable-2": "ProduceRecognitionSkipRound",
}
_NIA_BASE_RESOURCE_TEMPLATE_LABELS = frozenset(
    {
        "card-submit-strengthen",
        "card-submit-delete",
        "card-submit-copy",
        "card-submit-exchange",
        "card-icon-strengthen",
        "card-icon-delete",
        "card-icon-copy",
        "card-icon-exchange",
    }
)
_NIA_OUTER_NODE_RECOGNITION_BATCHES = (
    # Maa's actual choice-page gate is OCR.  If no recommend marker exists,
    # its original graph clicks the first row; do not replace that gate with
    # ad-hoc artwork templates.
    ("choice-page", "ProduceChooseGetFlag"),
)
_NIA_OUTER_RECOGNITION_SCOPES = frozenset(
    {
        "all",
        "overview",
        "subpage",
        "drink-dialog",
        "exam-action",
        "business",
        "activity",
        "outing",
        "consultation",
        "guidance",
        "lesson",
        "rest",
        "reward",
        "card-operation",
    }
)
_NIA_EXAM_ACTION_LABELS = frozenset(
    {"exam-playable-0", "exam-playable-1", "exam-playable-2"}
)
_NIA_DRINK_DIALOG_LABELS = frozenset(
    {"common-cancel", "common-decide", "decide", "drink-use"}
)
_NIA_OUTER_OVERVIEW_LABELS = frozenset(
    {
        *(
            label
            for label, _action in (
                ("event-action", "activity"),
                ("event-consultation", "consultation"),
                ("event-dance", "dance"),
                ("event-outing", "outing"),
                ("event-guide", "guide"),
                ("event-visual", "visual"),
                ("event-vocal", "vocal"),
                ("event-business", "business"),
                ("event-rest", "rest"),
            )
        ),
        "event-sp-0",
        "event-sp-1",
        "event-sp-2",
    }
)
_NIA_COMMON_TRANSITION_LABELS = frozenset(
    {
        "exam-failed",
        "challenge",
        "last-challenge",
        "decide",
        "common-next",
        "common-continue",
        "common-round-close",
        "communication-return-title",
        "common-choose",
        "common-ok",
        "common-ok-alt",
        "common-decide",
        "common-skip-confirm",
        "common-drink-no",
        "common-shop-exit",
        "common-close",
        "common-start",
        "common-skip-chat",
        "common-skip-chat-alt",
        "common-yes",
        "common-cards-get",
        "common-drink-reject",
        "common-challenge",
        "common-retry",
        "common-cancel",
        "common-finished",
        "common-follow",
    }
)
_NIA_CARD_OPERATION_LABELS = frozenset(
    {
        "card-submit-strengthen",
        "card-submit-delete",
        "card-submit-copy",
        "card-submit-exchange",
        "card-icon-strengthen",
        "card-icon-delete",
        "card-icon-copy",
        "card-icon-exchange",
    }
)
_NIA_REWARD_LABELS = frozenset(
    {
        "recommend",
        "event-recommend",
        "choice-move-cards",
        "drink-full",
        "drink-unchecked",
        "drink-use",
        "choice-page",
    }
)
_NIA_ROUTE_SCOPE_LABELS: Mapping[str, frozenset[str]] = {
    "business": frozenset(
        {
            "event-business",
            "work-page",
            "work-vo",
            "work-da",
            "work-vi",
            "work-none",
            "work-start",
            *_NIA_REWARD_LABELS,
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "activity": frozenset(
        {
            "event-action",
            *_NIA_REWARD_LABELS,
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "outing": frozenset(
        {
            "event-outing",
            "event-recommend",
            "choice-move-cards",
            *_NIA_REWARD_LABELS,
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "consultation": frozenset(
        {
            "event-consultation",
            "shop-buy",
            "shop-exchange",
            "shop-point-runout",
            "shop-page",
            "shop-sale",
            "shop-exit",
            "drink-full",
            *_NIA_REWARD_LABELS,
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "guidance": frozenset(
        {
            "event-guide",
            "guide-page",
            *_NIA_REWARD_LABELS,
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "lesson": frozenset(
        {
            "event-vocal",
            "event-dance",
            "event-visual",
            *_NIA_REWARD_LABELS,
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "rest": frozenset(
        {
            "event-rest",
            "rest-confirm",
            *_NIA_REWARD_LABELS,
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "reward": frozenset(
        {
            *_NIA_REWARD_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
    "card-operation": frozenset(
        {
            *_NIA_CARD_OPERATION_LABELS,
            *_NIA_COMMON_TRANSITION_LABELS,
        }
    ),
}


def _nia_outer_scope_allows(scope: str, label: str) -> bool:
    if scope == "all":
        return True
    if scope == "overview":
        return label in _NIA_OUTER_OVERVIEW_LABELS
    if scope == "subpage":
        return label not in _NIA_OUTER_OVERVIEW_LABELS
    if scope == "drink-dialog":
        return label in _NIA_DRINK_DIALOG_LABELS
    if scope == "exam-action":
        return label in _NIA_EXAM_ACTION_LABELS
    return label in _NIA_ROUTE_SCOPE_LABELS.get(scope, frozenset())
_MAA_RESOURCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "_research"
    / "MaaGakumasu"
    / "assets"
    / "resource"
)


@dataclass(frozen=True, slots=True)
class MaaNiaProduceBootstrapResult:
    """Result of the fixed Maa pipeline that enters one Produce run.

    The historical class name remains compatible with existing N.I.A.
    callers; ``produce_id`` explicitly identifies Initial or N.I.A.
    """

    started: bool
    reason: str
    produce_id: str
    difficulty: str
    idol_card_id: str
    visited_nodes: tuple[str, ...]
    observed_song_names: tuple[str, ...] = ()
    maa_task_id: int | None = None
    receipt_source: str = "maa-task-detail"
    terminal_action_index: int | None = None
    trailing_actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "reason": self.reason,
            "produce_id": self.produce_id,
            "difficulty": self.difficulty,
            "idol_card_id": self.idol_card_id,
            "visited_nodes": list(self.visited_nodes),
            "observed_song_names": list(self.observed_song_names),
            "maa_task_id": self.maa_task_id,
            "receipt_source": self.receipt_source,
            "terminal_action_index": self.terminal_action_index,
            "trailing_actions": list(self.trailing_actions),
        }


@dataclass(frozen=True, slots=True)
class MaaNiaPostLiveResult:
    """Result of MaaGakumasu's allow-listed Produce completion route."""

    completed: bool
    reason: str
    visited_nodes: tuple[str, ...]
    maa_task_id: int | None = None
    receipt_source: str = "maa-task-detail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "reason": self.reason,
            "visited_nodes": list(self.visited_nodes),
            "maa_task_id": self.maa_task_id,
            "receipt_source": self.receipt_source,
        }


def _maa_box_to_list(box: Any) -> list[int] | None:
    """Serialize MaaFramework box objects across Python binding versions.

    Current Maa releases may expose a rectangle either as an object with
    ``x/y/w/h`` attributes or as a four-element sequence.  Recognition is
    read-only, so an unknown shape must remain a hard error instead of
    silently inventing coordinates.
    """

    if box is None:
        return None
    if all(hasattr(box, name) for name in ("x", "y", "w", "h")):
        return [int(box.x), int(box.y), int(box.w), int(box.h)]
    if isinstance(box, Mapping):
        values = (
            box.get("x", box.get("left")),
            box.get("y", box.get("top")),
            box.get("w", box.get("width")),
            box.get("h", box.get("height")),
        )
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in values
        ):
            return [int(value) for value in values]
    if isinstance(box, (list, tuple)) and len(box) == 4:
        return [int(value) for value in box]
    raise TypeError(f"unsupported MaaFramework box shape: {type(box).__name__}")


def _serialize_audition_card_result(value: Any, index: int) -> dict[str, Any]:
    """Serialize one Maa ``all_results`` row without filtering it out.

    ``ProduceRecognitionCards`` exposes the detector label as ``text`` in
    current Maa bindings.  Keep both spellings for the legal gate and retain
    a malformed/unknown row as an explicit row with ``serialization_error``
    rather than dropping it and accidentally making an incomplete result look
    complete.
    """

    text = getattr(value, "text", None)
    if text is None:
        # MaaFramework OCR rows expose ``text`` while current
        # NeuralNetworkDetect rows expose ``label``.  Both are official
        # RecognitionResult shapes; dropping the latter turns every correct
        # card box into an invalid placeholder at (0, 0, 1, 1).
        text = getattr(value, "label", None)
    if text is None and isinstance(value, Mapping):
        text = value.get("text", value.get("label"))
    label = text if isinstance(text, str) else None
    score = getattr(value, "score", None)
    if score is None and isinstance(value, Mapping):
        score = value.get("score", value.get("confidence"))
    box = getattr(value, "box", None)
    if box is None and isinstance(value, Mapping):
        box = value.get("box")
    serialized: dict[str, Any] = {
        "index": index,
        "label": label,
        "text": label,
        "score": float(score)
        if isinstance(score, (int, float)) and not isinstance(score, bool)
        else None,
        "box": None,
    }
    try:
        serialized["box"] = _maa_box_to_list(box)
    except (TypeError, ValueError) as error:
        serialized["serialization_error"] = f"{type(error).__name__}: {error}"
    if label not in _AUDITION_CARD_LABELS:
        serialized["label_valid"] = False
    else:
        serialized["label_valid"] = True
    return serialized


def _maa_recognition_from_job(
    job: Any,
    *,
    label: str = "generic",
    deadline: float | None = None,
    stop_callback: Callable[[], Any] | None = None,
) -> Any:
    """Return the last Maa recognition result from a completed job.

    Maa's Python bindings expose the result tree slightly differently across
    releases.  Navigation only accepts a hit when the binding provides a
    concrete recognition object; an absent/unknown result is a hard miss.
    """

    job = _wait_maa_recognition_job(
        job,
        label=label,
        deadline=deadline,
        stop_callback=stop_callback,
    )
    if not job.succeeded:
        raise RuntimeError("MaaFramework recognition failed")
    detail = job.get()
    nodes = () if detail is None else tuple(getattr(detail, "nodes", ()))
    return next(
        (
            value.recognition
            for value in reversed(nodes)
            if getattr(value, "recognition", None) is not None
        ),
        None,
    )


def _maa_recognition_box(recognition: Any) -> list[int] | None:
    """Choose the binding's best hit box without inventing coordinates."""

    if recognition is None or not bool(getattr(recognition, "hit", False)):
        return None
    best = getattr(recognition, "best_result", None)
    box = getattr(best, "box", None) if best is not None else None
    if box is None:
        box = getattr(recognition, "box", None)
    return _maa_box_to_list(box)


def _normalize_maa_ocr_text(value: str) -> str:
    """Normalize one Maa OCR result for strict, presentation-only matching."""

    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not character.isspace() and character not in "[]【】"
    ).casefold()


def _maa_ocr_text_matches(actual: str, expected: str) -> bool:
    """Match a localized OCR label without inventing a coordinate or alias."""

    left = _normalize_maa_ocr_text(actual)
    right = _normalize_maa_ocr_text(expected)
    if not left or not right:
        return False
    return left == right or right in left or SequenceMatcher(None, left, right).ratio() >= 0.78


def _maa_identity_text_matches(actual: str, expected: str) -> bool:
    """Match card identity while tolerating OCR-only punctuation variants."""

    def normalize(value: str) -> str:
        return "".join(
            character.casefold()
            for character in unicodedata.normalize("NFKC", value)
            if character.isalnum()
        )

    left = normalize(actual)
    right = normalize(expected)
    if not left or not right:
        return False
    return left == right or SequenceMatcher(None, left, right).ratio() >= 0.8


def _maa_template_node_param(node: Any, node_name: str) -> Any:
    """Validate and return an official Maa TemplateMatch parameter object."""

    from maa.define import AlgorithmEnum

    recognition = getattr(node, "recognition", None)
    if recognition is None or recognition.type != AlgorithmEnum.TemplateMatch:
        raise RuntimeError(
            f"Maa Produce ranking navigation source is not TemplateMatch: {node_name}"
        )
    param = getattr(recognition, "param", None)
    templates = tuple(str(value) for value in getattr(param, "template", ()))
    if not templates:
        raise RuntimeError(
            f"Maa Produce ranking navigation source has no templates: {node_name}"
        )
    return param


@lru_cache(maxsize=1)
def _default_nia_idol_catalog() -> NiaIdolCatalog:
    """Load the complete Master-backed catalog once per process.

    The source files are static during a Maa run.  Caching avoids reopening
    the 190-MB SQLite database for every bootstrap request while retaining
    strict loader errors when the authority is unavailable or malformed.
    """

    return load_nia_idol_catalog()


_GLOBAL_HOME_PRODUCE_TILE_ROI = (160, 880, 440, 260)


def _produce_new_run_home_pipeline_override() -> tuple[str, dict[str, Any]]:
    """Build the Maa-only preflight that normalizes one fresh Produce start.

    ``home.png`` is MaaGakumasu's authoritative new-run tile; ``home_1.png``
    means an existing Produce and is deliberately excluded.  When the game is
    still on a preparation subpage, Maa's own ReturnHome/BackHome templates
    click its visible Home control and jump back to the exact new-run gate.
    No run record exists and no AP can be spent in this preflight.
    """

    entry = "GkmsToolEnsureNewProduceHome"
    return entry, {
        entry: {
            "recognition": {"type": "DirectHit", "param": {}},
            "action": {"type": "DoNothing", "param": {}},
            "max_hit": 8,
            "next": [
                "[JumpBack]ProduceStartPopUpButtons",
                "[JumpBack]ReturnHome",
                "[JumpBack]BackHome",
                "[JumpBack]GkmsToolProduceBackToPicker",
                "GkmsToolNewProduceHomeFlag",
            ],
        },
        "GkmsToolNewProduceHomeFlag": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": list(_GLOBAL_HOME_PRODUCE_TILE_ROI),
                    "template": ["home.png"],
                },
            },
            "action": {"type": "DoNothing", "param": {}},
            "next": [],
        },
        # Reuse the same bounded popup recognizer as the actual Produce task so
        # a campaign notice on top of Home cannot make the preflight drift into
        # a preparation page.
        "ProduceStartPopUpButtons": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [0, 1080, 720, 200],
                    "template": ["produce/cancel.png", "close_icon.png"],
                    "order_by": "Score",
                },
            },
            "action": {"type": "Click", "param": {}},
            "post_wait_freezes": 500,
        },
        # The expanded idol-card Overview hides the Home glyph behind Maa's
        # existing double-chevron Back template.  Close at most one nested
        # page per pass; the root then recognizes the picker's real Home glyph
        # and Maa's ReturnHome route finishes the normalization.
        "GkmsToolProduceBackToPicker": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [0, 1080, 180, 200],
                    "template": ["back.png"],
                },
            },
            "action": {"type": "Click", "param": {}},
            "post_wait_freezes": 300,
        },
    }


def _produce_bootstrap_pipeline_override(
    *,
    produce_id: str,
    idol_card_id: str,
    use_ap_drink: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build the allow-listed Maa Produce bootstrap without touching the game."""

    if type(use_ap_drink) is not bool:
        raise TypeError("use_ap_drink must be bool")

    mode = {
        "produce-001": ("REGULAR", "produce/produce_mainpage_hajime.png"),
        "produce-002": ("PRO", "produce/produce_mainpage_hajime.png"),
        "produce-003": ("MASTER", "produce/produce_mainpage_hajime.png"),
        "produce-004": ("PRO", "produce/produce_mainpage_nia.png"),
        "produce-005": ("MASTER", "produce/produce_mainpage_nia.png"),
    }.get(produce_id)
    if mode is None:
        raise ValueError(f"unsupported Produce bootstrap produce_id: {produce_id}")
    difficulty, scenario_template = mode
    catalog = _default_nia_idol_catalog()
    idol_entry = catalog.require(idol_card_id)
    try:
        rarity_filter_target = _PRODUCE_IDOL_RARITY_FILTER_TARGETS[
            idol_entry.rarity
        ]
        plan_filter_target = _PRODUCE_IDOL_PLAN_FILTER_TARGETS[
            idol_entry.plan_type
        ]
    except KeyError as error:
        raise ValueError(
            "unsupported Produce idol-card filter identity: "
            f"{idol_card_id} / {idol_entry.rarity} / {idol_entry.plan_type}"
        ) from error

    def filter_state(state: str) -> dict[str, Any]:
        return {
            "type": "Custom",
            "param": {
                "custom_recognition": "GkmsToolProduceIdolFilterState",
                "custom_recognition_param": {
                    "state": state,
                    "rarity": idol_entry.rarity,
                    "plan_type": idol_entry.plan_type,
                },
            },
        }

    idol_name, song_name, idol_name_aliases, song_name_aliases = (
        idol_entry.bootstrap_tuple()
    )
    return difficulty, {
        # MaaGakumasu already routes home-page popups through
        # [JumpBack]ProduceStartPopUpButtons.  A single hit, however, cannot
        # return to ProduceLoop after dismissing one popup.  Keep the retry
        # bounded while allowing stacked campaign/expiry notices.
        "ProduceLoop": {"max_hit": 4},
        "ProduceStartPopUpButtons": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [0, 1080, 720, 200],
                    "template": ["produce/cancel.png", "close_icon.png"],
                    "order_by": "Score",
                },
            },
            "action": {"type": "Click", "param": {}},
            "post_wait_freezes": 500,
        },
        "ProduceChooseScenario": {
            "recognition": {
                "param": {"template": [scenario_template]}
            }
        },
        "ProduceChooseDifficulty": {
            "recognition": {"param": {"expected": difficulty}},
            # The translated 3.3.0 picker can already be visible while the
            # legacy choose-idol template is absent.  Try the AP dialog first,
            # then accept either the expanded Overview or the proven 3D
            # picker gate below.  No unconditional click participates here.
            "next": [
                "ProduceLackAP",
                "ProduceChooseIdolFlag",
                "GkmsToolProduceIdolOverviewReady",
            ],
        },
        # Never spend an AP recovery item on behalf of the user unless the
        # caller explicitly opts in.  The opt-in branch deliberately reuses
        # Maa's existing ProduceLackAPRestore/Use/Decide nodes; no diamond or
        # other purchase path is added here.
        "ProduceLackAP": {
            "next": (
                ["ProduceLackAPRestore"]
                if use_ap_drink
                else ["ProduceLackAPCancel"]
            )
        },
        "ProduceLackAPCancel": {"next": []},
        # An existing run is not necessarily the requested N.I.A. context.
        # Stop before clicking Continue; the LocalSave-driven runner handles a
        # valid existing run before this bootstrap is ever requested.
        "ProduceContinue": {"action": {"type": "DoNothing", "param": {}}, "next": []},
        "ProduceChooseIdol": {
            "recognition": {
                "param": {
                    "custom_recognition": "GkmsToolChooseNiaIdol",
                    "custom_recognition_param": {
                        "idol_name": idol_name,
                        "idol_name_aliases": list(idol_name_aliases),
                        "song_name": song_name,
                        "song_name_aliases": list(song_name_aliases),
                    },
                }
            },
            # SendMessage success is not proof that Unity consumed the
            # selected-card Confirm.  Prefer the two real next pages; if the
            # same identity screen remains, retry this exact confirm once.
            "max_hit": 2,
            # A grid hit first closes the detail sheet and returns to the
            # carousel, while a legacy carousel hit already advances to the
            # support page. Prefer the support marker; only when absent may
            # Maa click the carousel's actual Next button.
            "next": [
                "ProduceChooseSupport",
                "ProduceChooseIdolNext",
                "[JumpBack]ProduceChooseIdol",
            ],
        },
        # The current client enters the complete idol-card carousel directly
        # after ProduceChooseIdolFlag.  The upstream graph still assumes an
        # older two-step picker and first waits for ``produce/decide.png``,
        # then clicks a fixed first-card coordinate.  On the current zh-TW UI
        # that stale template no longer matches and the fixed click is not a
        # valid ordering authority.  Normalize the built-in character,
        # rarity, and plan filters first, then enter the existing idol+song
        # OCR recognizer and OCR-driven carousel walker.
        "ProduceChooseIdolFlag": {
            "recognition": filter_state("picker"),
            "action": {
                "type": "Click",
                "param": {"target": [80, 747, 1, 1]},
            },
            "post_wait_freezes": 300,
            "max_hit": 2,
            "next": [
                "GkmsToolProduceIdolOverviewReady",
                "ProduceChooseIdolFlag",
            ],
        },
        # Upstream selects the first currently filtered card.  Clear the
        # persisted character filter through Maa OCR before walking right;
        # otherwise a non-Kotone request can only ever see Kotone cards.
        "ProduceChooseIdolFirstDecide": {
            "next": [
                "GkmsToolProduceIdolOverviewReady",
                "GkmsToolOpenProduceIdolOverviewBeforeFilter",
            ],
        },
        # The filter controls below use the expanded Overview coordinates.  A
        # previous session may persist either the 3D picker or this sheet, so
        # first accept a proven white Overview; only otherwise click Overview
        # once and wait for the same structural proof before filtering.
        "GkmsToolProduceIdolOverviewReady": {
            "recognition": filter_state("overview"),
            "action": {"type": "DoNothing", "param": {}},
            "next": ["GkmsToolOpenProduceIdolFilter"],
        },
        "GkmsToolOpenProduceIdolOverviewBeforeFilter": {
            "recognition": {"type": "DirectHit", "param": {}},
            "action": {
                "type": "Click",
                "param": {"target": [80, 747, 1, 1]},
            },
            "post_wait_freezes": 300,
            "max_hit": 2,
            "next": [
                "GkmsToolProduceIdolOverviewReady",
                "[JumpBack]GkmsToolOpenProduceIdolOverviewBeforeFilter",
            ],
        },
        "GkmsToolOpenProduceIdolFilter": {
            "recognition": {"type": "DirectHit", "param": {}},
            "action": {
                "type": "Click",
                "param": {"target": [242, 1008, 1, 1]},
            },
            "post_wait_freezes": 200,
            "max_hit": 2,
            "next": [
                "GkmsToolClearProduceIdolFilter",
                "[JumpBack]GkmsToolOpenProduceIdolFilter",
            ],
        },
        "GkmsToolClearProduceIdolFilter": {
            "recognition": {
                "type": "OCR",
                "param": {
                    "expected": ["ALL"],
                    "roi": [240, 860, 430, 120],
                },
            },
            "action": {"type": "Click", "param": {}},
            "post_wait_freezes": 400,
            "max_hit": 2,
            "next": [
                "GkmsToolProduceIdolOverviewAfterCharacterFilter",
                "GkmsToolConfirmProduceIdolFilter",
                "[JumpBack]GkmsToolClearProduceIdolFilter",
            ],
        },
        # Selecting ALL clears the persisted character restriction.  The
        # panel normally closes by itself, so the overview branch above is
        # intentionally tried first after a longer settle.  Keep this gated
        # confirm only for frames where the character panel genuinely remains;
        # it must never click the idol-card Confirm on the parent overview.
        "GkmsToolConfirmProduceIdolFilter": {
            "recognition": {
                "type": "OCR",
                "param": {
                    "expected": ["ALL"],
                    "roi": [240, 860, 430, 120],
                },
            },
            "action": {
                "type": "Click",
                "param": {"target": [360, 1100, 1, 1]},
            },
            "post_wait_freezes": 200,
            "max_hit": 2,
            "next": [
                "GkmsToolProduceIdolOverviewAfterCharacterFilter",
                "[JumpBack]GkmsToolConfirmProduceIdolFilter",
            ],
        },
        "GkmsToolProduceIdolOverviewAfterCharacterFilter": {
            "recognition": filter_state("overview"),
            "action": {"type": "DoNothing", "param": {}},
            "next": ["GkmsToolOpenProduceIdolSortFilter"],
        },
        # The live picker retains its previous rarity/plan choices.  Open the
        # built-in filter, reset each relevant group, then apply exactly the
        # requested card's Master rarity and plan.  An SSR Plan2 request thus
        # scans only SSR 理性 cards; Plan1/Plan3 and SR/R reuse the same route.
        "GkmsToolOpenProduceIdolSortFilter": {
            "recognition": {"type": "DirectHit", "param": {}},
            "action": {
                "type": "Click",
                "param": {"target": [540, 1008, 1, 1]},
            },
            "post_wait_freezes": 200,
            "max_hit": 2,
            "next": [
                "GkmsToolOpenProduceIdolFilterTab",
                "[JumpBack]GkmsToolOpenProduceIdolSortFilter",
            ],
        },
        "GkmsToolOpenProduceIdolFilterTab": {
            "recognition": filter_state("panel"),
            "action": {
                "type": "Click",
                "param": {"target": [520, 1030, 1, 1]},
            },
            "post_wait_freezes": 200,
            "max_hit": 2,
            "next": [
                "GkmsToolClearProduceIdolRarityFilter",
                "[JumpBack]GkmsToolOpenProduceIdolFilterTab",
            ],
        },
        "GkmsToolClearProduceIdolRarityFilter": {
            "recognition": filter_state("filter-tab"),
            "action": {
                "type": "Click",
                "param": {"target": [480, 203, 1, 1]},
            },
            # Filter rows are toggles.  A stale frame followed by the bounded
            # retry would undo a successful first click, so wait for the live
            # selection animation to settle before verifying the new state.
            "post_wait_freezes": 400,
            "max_hit": 2,
            "next": [
                "GkmsToolSelectProduceIdolRarityFilter",
                "[JumpBack]GkmsToolClearProduceIdolRarityFilter",
            ],
        },
        "GkmsToolSelectProduceIdolRarityFilter": {
            "recognition": filter_state("rarity-cleared"),
            "action": {
                "type": "Click",
                "param": {"target": list(rarity_filter_target)},
            },
            "post_wait_freezes": 400,
            "max_hit": 2,
            "next": [
                "GkmsToolClearProduceIdolPlanFilter",
                "[JumpBack]GkmsToolSelectProduceIdolRarityFilter",
            ],
        },
        "GkmsToolClearProduceIdolPlanFilter": {
            "recognition": filter_state("rarity-selected"),
            "action": {
                "type": "Click",
                "param": {"target": [480, 375, 1, 1]},
            },
            "post_wait_freezes": 400,
            "max_hit": 2,
            "next": [
                "GkmsToolSelectProduceIdolPlanFilter",
                "[JumpBack]GkmsToolClearProduceIdolPlanFilter",
            ],
        },
        "GkmsToolSelectProduceIdolPlanFilter": {
            "recognition": filter_state("plan-cleared"),
            "action": {
                "type": "Click",
                "param": {"target": list(plan_filter_target)},
            },
            "post_wait_freezes": 400,
            "max_hit": 2,
            "next": [
                "GkmsToolApplyProduceIdolFilter",
                "[JumpBack]GkmsToolSelectProduceIdolPlanFilter",
            ],
        },
        "GkmsToolApplyProduceIdolFilter": {
            "recognition": filter_state("exact-selection"),
            "action": {
                "type": "Click",
                "param": {"target": [505, 1160, 1, 1]},
            },
            "post_wait_freezes": 300,
            "max_hit": 2,
            "next": [
                "GkmsToolVerifyProduceIdolPicker",
                "[JumpBack]GkmsToolApplyProduceIdolFilter",
            ],
        },
        "GkmsToolVerifyProduceIdolPicker": {
            "recognition": filter_state("picker"),
            "action": {"type": "DoNothing", "param": {}},
            "next": ["ProduceChooseIdol", "[JumpBack]ProduceChooseNextIdol"],
        },
        # Idol+song identity (not song alone) owns the end-of-list check,
        # because several characters share card song names.  The grid walker
        # proves its own endpoint by seeing the same OCR-identified viewport
        # twice; this cap is only a runaway guard and is deliberately unrelated
        # to catalog size or grid ordering.
        "ProduceChooseNextIdol": {
            "max_hit": 512,
            "action": {
                "type": "Custom",
                "param": {"custom_action": "GkmsToolAdvanceProduceIdol"},
            },
            "post_wait_freezes": 200,
        },
        "ProduceChooseMemory": {"enabled": True},
        # A card's first Produce can open one or more account setup dialogs
        # (voice playback, fast-forward, performance mode) immediately after
        # the memory page.  At that point the final Produce-start template is
        # covered, so the ordinary graph cannot reach ProduceUseItem.  Keep
        # the recovery inside this Maa task: try the real start page first,
        # then only Maa's affirmative button templates.  Cancel/reject/back
        # actions are deliberately absent, and a bounded self-edge supports
        # stacked one-time dialogs without a card- or locale-specific branch.
        "ProduceChooseMemoryNext": {
            "next": [
                "ProduceUseItem",
                "GkmsToolProduceBootstrapSetupConfirm",
                "[JumpBack]ProduceDecide",
                "[JumpBack]ProduceNext",
            ],
        },
        "ProduceUseItem": {
            "next": [
                "ProduceUseNote",
                "ProduceUsePt",
                "ProduceLauncher",
                "GkmsToolProduceBootstrapSetupConfirm",
            ],
        },
        "GkmsToolProduceBootstrapSetupConfirm": {
            "recognition": {
                "type": "TemplateMatch",
                "param": {
                    "roi": [0, 700, 720, 580],
                    "template": [
                        "produce/decide.png",
                        "produce/ok.png",
                        "produce/ok_1.png",
                        "next.png",
                        "produce/yes.png",
                        "produce/skip_confirm.png",
                        "produce/continue.png",
                        "produce/start.png",
                        "produce/close.png",
                    ],
                    "threshold": 0.8,
                    "order_by": "Score",
                },
            },
            "action": {"type": "Click", "param": {}},
            "post_wait_freezes": 500,
            "max_hit": 4,
            "next": [
                "ProduceUseItem",
                "GkmsToolProduceBootstrapSetupConfirm",
            ],
        },
        "ProduceUseNote": {"enabled": False},
        "ProduceUsePt": {"enabled": False},
        # End Maa's preparation task as soon as the game has accepted Produce
        # start. The LocalSave/ExamSave unattended driver owns everything next.
        "ProduceEntryFlag": {"next": []},
    }


def _nia_pro_recommended_replay_pipeline_override(
    *,
    idol_card_id: str,
    produce_id: str = "produce-004",
    use_ap_drink: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build the fixed N.I.A. picker route used by replay navigation.

    The normal Produce bootstrap owns the exact rarity/plan filter, idol/song
    OCR recognizer, carousel action, and the existing card-confirm click.  This
    route deliberately reuses that graph but makes the selected-card node a
    read-only terminal.  The current selected-card page already exposes
    ``培育資訊``; its orange bottom button is ``下一步`` and must not be
    clicked by replay navigation.  Maa therefore cannot continue into Next,
    support, memory, start, or AP nodes after the target card is recognized.
    """

    if produce_id not in {"produce-004", "produce-005"}:
        raise ValueError(
            f"unsupported N.I.A. replay produce_id: {produce_id}"
        )
    difficulty, pipeline = _produce_bootstrap_pipeline_override(
        produce_id=produce_id,
        idol_card_id=idol_card_id,
        use_ap_drink=use_ap_drink,
    )
    target = dict(pipeline["ProduceChooseIdol"])
    target["action"] = {"type": "DoNothing", "param": {}}
    target["next"] = []
    pipeline["ProduceChooseIdol"] = target
    return difficulty, pipeline


# Keep a descriptive alias for callers/tests that use the Produce-first
# naming convention used by the bootstrap helper.  Both names resolve to the
# same fixed graph and neither accepts arbitrary pipeline input.  The default
# remains produce-004 for compatibility with the original Pro-only callers.
_produce_nia_pro_recommended_replay_pipeline_override = (
    _nia_pro_recommended_replay_pipeline_override
)


def _nia_bootstrap_pipeline_override(
    *,
    produce_id: str,
    idol_card_id: str,
    use_ap_drink: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Compatibility wrapper for the original N.I.A.-only helper."""

    if produce_id not in {"produce-004", "produce-005"}:
        raise ValueError(f"unsupported N.I.A. produce_id: {produce_id}")
    return _produce_bootstrap_pipeline_override(
        produce_id=produce_id,
        idol_card_id=idol_card_id,
        use_ap_drink=use_ap_drink,
    )


def _nia_post_live_pipeline_override() -> dict[str, Any]:
    """Return the fixed MaaGakumasu ProduceEnd route used after Final live.

    LocalSave and native orientation are checked by the caller.  Maa's own
    pipeline then owns recognition and inputs for the remaining generation,
    completion, follow/dialogue, and return-home pages.  The synthetic entry
    reproduces ``ProduceShowEnd``'s only input (``Click_1`` at 20,20) without
    requiring MaaGakumasu's external AgentServer.  Home and the returned N.I.A.
    scenario-selection page are terminal, so this bounded task can never loop
    into another Produce run.
    """

    return {
        "GkmsToolNiaPostLive": {
            "recognition": {"type": "DirectHit", "param": {}},
            "action": {
                "type": "Click",
                "param": {"target": [20, 20, 1, 1]},
            },
            "next": [
                # A failed Final opens the game's native end/retry modal after
                # the caller selected Next.  MaaGakumasu already owns its End
                # button, so reuse that node instead of adding another visual
                # classifier or a coordinate click.
                "ProduceFailedStop",
                # These stock actions otherwise install their historical
                # next-lists.  Those lists do not contain every N.I.A. Final
                # result page (notably ProduceDecide on the producer-ranking
                # result), so execute one recognized click and return to this
                # complete post-live root.
                "[JumpBack]ProduceGeneration",
                "[JumpBack]ProduceFinished",
                "ProduceHomeFlag",
                # A completed N.I.A. run returns to the Produce scenario page,
                # not necessarily the legacy global Home page.  Enter through
                # Maa's native ProduceMainPage ranking-icon gate first, then
                # require its N.I.A. scenario template below.  The two existing
                # page-local templates prevent an N.I.A. stage background from
                # being mistaken for completion; neither node performs input.
                "ProduceMainPage",
                "[JumpBack]ProduceSkipShowConfirm",
                # The confirmation can already be visible when a failed Final
                # enters the landscape route.  It must get first refusal over
                # ProduceSkipED, whose orientation recognizer intentionally
                # matches every landscape frame.
                "[JumpBack]ProduceSkipED",
                "[JumpBack]ProduceNext",
                "[JumpBack]ProduceSkip",
                "[JumpBack]ProduceDecide",
                "[JumpBack]CloseRoundButton",
                "[JumpBack]ProduceError",
                "[JumpBack]Click_1",
            ],
        },
        # These stock clicks must explicitly return to the fixed root.  A
        # JumpBack label alone can still finish the task after the child click
        # when an override supplies an empty next-list (observed on the live
        # N.I.A. failed-Final generation page).  Returning to the same root
        # keeps one Maa task alive without adding another detector or input.
        "ProduceGeneration": {"next": ["GkmsToolNiaPostLive"]},
        "ProduceFinished": {"next": ["GkmsToolNiaPostLive"]},
        "ProduceHomeFlag": {"next": []},
        "ProduceBackHome": {"next": []},
        "ProduceMainPage": {"next": ["ProduceChooseScenario"]},
        "ProduceChooseScenario": {
            "recognition": {
                "param": {
                    "template": ["produce/produce_mainpage_nia.png"],
                }
            },
            "next": [],
        },
    }


def _produce_idol_filter_recognizer_type():
    """Create the verified live-picker filter-state recognizer lazily."""

    from maa.custom_recognition import CustomRecognition

    class GkmsToolProduceIdolFilterState(CustomRecognition):
        def __init__(self) -> None:
            super().__init__()
            self.reset()

        def reset(self) -> None:
            self.calls = 0
            self.last_detail: dict[str, object] = {}

        @property
        def diagnostic(self) -> str:
            return f"calls={self.calls}, last={self.last_detail!r}"

        def analyze(self, _context, argv):
            self.calls += 1
            try:
                raw = json.loads(argv.custom_recognition_param)
            except (json.JSONDecodeError, TypeError):
                raw = {}
            state = str(raw.get("state", ""))
            rarity = str(raw.get("rarity", ""))
            plan_type = str(raw.get("plan_type", ""))
            matched = _produce_idol_filter_frame_matches(
                np.asarray(argv.image),
                state=state,
                rarity=rarity,
                plan_type=plan_type,
            )
            self.last_detail = {
                "state": state,
                "rarity": rarity,
                "plan_type": plan_type,
                "matched": matched,
            }
            return CustomRecognition.AnalyzeResult(
                box=[0, 0, 1, 1] if matched else None,
                detail=dict(self.last_detail),
            )

    return GkmsToolProduceIdolFilterState


def _nia_idol_recognizer_type():
    """Create the local Maa recognizer lazily, keeping native imports optional."""

    from maa.custom_recognition import CustomRecognition

    class GkmsToolChooseNiaIdol(CustomRecognition):
        def __init__(self) -> None:
            super().__init__()
            self._local_grid_recognizer: Any | None = None
            self._local_grid_recognizer_attempted = False
            self.reset()

        def reset(self) -> None:
            # Keep the lazily-loaded OCR model, but never carry a prior Maa
            # task's diagnostic into the next card-picker result.
            self.calls = 0
            self.last_detail: dict[str, object] = {}


        def _read_local_grid_header(
            self,
            image: object,
        ) -> tuple[str, str, float, float]:
            """Read the current grid header when Maa's OCR text is corrupted.

            The same bundled Paddle model is already used by the rest of the
            live reader.  Loading it is lazy and happens only after the Maa
            OCR pair fails to match, so the ordinary picker path is unchanged.
            This remains a Maa custom-recognition decision; it never submits
            input or changes the carousel walk.
            """

            if not self._local_grid_recognizer_attempted:
                self._local_grid_recognizer_attempted = True
                try:
                    from .text_recognizer import PaddleLineRecognizer

                    self._local_grid_recognizer = PaddleLineRecognizer()
                except (FileNotFoundError, OSError, RuntimeError, ValueError):
                    self._local_grid_recognizer = None
            recognizer = self._local_grid_recognizer
            if recognizer is None:
                return "", "", 0.0, 0.0
            array = np.asarray(image)
            if array.ndim != 3 or array.shape[2] < 3:
                return "", "", 0.0, 0.0
            # MaaFramework exposes an OpenCV BGR/BGRA frame.  Header text is
            # dark and remains readable either way, but preserve the expected
            # RGB contract of Pillow/Paddle explicitly.
            rgb = np.ascontiguousarray(array[:, :, :3][:, :, ::-1])
            rendered = Image.fromarray(rgb.astype(np.uint8))
            song = recognizer.recognize(rendered.crop((140, 20, 570, 65)))
            idol = recognizer.recognize(rendered.crop((140, 55, 500, 100)))
            song_text = song.text if song.confidence >= 0.65 else ""
            idol_text = idol.text if idol.confidence >= 0.65 else ""
            return song_text, idol_text, float(song.confidence), float(idol.confidence)

        @property
        def diagnostic(self) -> str:
            return f"calls={self.calls}, last={self.last_detail!r}"

        @staticmethod
        def _similarity(left: str, right: str) -> float:
            def normalize(value: str) -> str:
                return "".join(
                    character
                    for character in unicodedata.normalize("NFKC", value)
                    if not character.isspace() and character not in "[]【】"
                ).casefold()

            return SequenceMatcher(None, normalize(left), normalize(right)).ratio()

        def analyze(self, context, argv):
            self.calls += 1
            raw = json.loads(argv.custom_recognition_param)
            idol_name = str(raw["idol_name"])
            idol_name_aliases = tuple(
                str(value) for value in raw.get("idol_name_aliases", ())
            )
            song_name = str(raw["song_name"])
            song_name_aliases = tuple(
                str(value) for value in raw.get("song_name_aliases", ())
            )
            self.last_detail = {
                "stage": "expected-loaded",
                "idol_name": idol_name,
                "song_name": song_name,
            }
            true_end = context.run_recognition(
                "GkmsToolChooseIdolTrueEnd",
                argv.image,
                pipeline_override={
                    "GkmsToolChooseIdolTrueEnd": {
                        "recognition": {
                            "type": "OCR",
                            "param": {
                                "expected": ["True", "End"],
                                "roi": [430, 34, 266, 48],
                            },
                        }
                    }
                },
            )

            def read_text(node: str, roi: list[int]) -> str:
                detail = context.run_recognition(
                    node,
                    argv.image,
                    pipeline_override={
                        node: {
                            "recognition": {
                                "type": "OCR",
                                "param": {"roi": roi},
                            }
                        }
                    },
                )
                if detail is None or not detail.hit:
                    return ""
                return "".join(
                    str(getattr(item, "text", "")) for item in detail.all_results
                ).strip()

            # The current 720x1280 picker is a four-column grid.  Its selected
            # card identity lives in the two header lines below.  The former
            # ROIs overlap stamina and plan labels on this layout, so never mix
            # a song read from one layout with an idol read from another.
            if true_end is not None and true_end.hit:
                layout = "legacy-true-end"
                actual_song = read_text(
                    "GkmsToolChooseIdolLegacySong", [380, 90, 320, 45]
                )
                actual_idol = read_text(
                    "GkmsToolChooseIdolLegacyName", [440, 128, 280, 64]
                )
            else:
                grid_song = read_text(
                    "GkmsToolChooseIdolGridSong", [140, 20, 430, 45]
                )
                grid_idol = read_text(
                    "GkmsToolChooseIdolGridName", [140, 55, 360, 45]
                )
                # The current non-True-End picker is the grid layout.  A
                # missing header read must remain missing: the old carousel
                # song ROI overlaps the grid's plan label (for example
                # ``理性``) and previously turned that label into a fake song
                # identity, stopping the walk after two tiles.
                layout = "grid"
                actual_song, actual_idol = grid_song, grid_idol
            text_source = "maa-ocr"
            local_confidence: tuple[float, float] | None = None
            self.last_detail = {
                "stage": "identity-read",
                "layout": layout,
                "idol_name": actual_idol,
                "song_name": actual_song,
            }
            matched_idol_name = next(
                (
                    expected
                    for expected in (idol_name, *idol_name_aliases)
                    if self._similarity(actual_idol, expected) >= 0.7
                ),
                None,
            )
            matched_song_name = next(
                (
                    expected
                    for expected in (song_name, *song_name_aliases)
                    if self._similarity(actual_song, expected) >= 0.7
                ),
                None,
            )
            matched = (
                matched_idol_name is not None
                and matched_song_name is not None
            )
            if not matched and layout == "grid":
                (
                    local_song,
                    local_idol,
                    local_song_confidence,
                    local_idol_confidence,
                ) = self._read_local_grid_header(argv.image)
                if local_song and local_idol:
                    actual_song, actual_idol = local_song, local_idol
                    text_source = "local-paddle-fallback"
                    local_confidence = (
                        local_song_confidence,
                        local_idol_confidence,
                    )
                    matched_idol_name = next(
                        (
                            expected
                            for expected in (idol_name, *idol_name_aliases)
                            if self._similarity(actual_idol, expected) >= 0.7
                        ),
                        None,
                    )
                    matched_song_name = next(
                        (
                            expected
                            for expected in (song_name, *song_name_aliases)
                            if self._similarity(actual_song, expected) >= 0.7
                        ),
                        None,
                    )
                    matched = (
                        matched_idol_name is not None
                        and matched_song_name is not None
                    )
            self.last_detail = {
                "stage": "matched",
                "layout": layout,
                "text_source": text_source,
                "idol_name": actual_idol,
                "song_name": actual_song,
                "matched_idol_name": matched_idol_name,
                "matched_song_name": matched_song_name,
                "matched": matched,
            }
            if local_confidence is not None:
                self.last_detail["local_song_confidence"] = local_confidence[0]
                self.last_detail["local_idol_confidence"] = local_confidence[1]
            return CustomRecognition.AnalyzeResult(
                box=[0, 0, 1, 1] if matched else None,
                detail={
                    "idol_name": actual_idol,
                    "matched_idol_name": matched_idol_name,
                    "song_name": actual_song,
                    "matched_song_name": matched_song_name,
                    "matched": matched,
                    "layout": layout,
                    "text_source": text_source,
                },
            )

    return GkmsToolChooseNiaIdol


def _produce_idol_carousel_action_type():
    """Create an OCR-driven idol-card walker with a proven endpoint."""

    from maa.custom_action import CustomAction

    class GkmsToolAdvanceProduceIdol(CustomAction):
        # The canonical first row is rendered near y=695.  A retained filter
        # scroll around y=771 is intentionally outside this margin.  Four
        # bounded gestures keep a stuck/unsupported surface fail-closed.
        _GRID_TOP_FIRST_ROW_MAX_Y = 730
        _GRID_TOP_MAX_SWIPES = 8
        _GRID_TOP_UNSAFE_SAMPLE_LIMIT = 3
        _GRID_PAGE_REPEAT_END_COUNT = 3
        _GRID_TARGETS = tuple(
            (x, y, 1, 1)
            for y in (695, 914)
            for x in (103, 274, 445, 615)
        )

        def __init__(self) -> None:
            super().__init__()
            self.reset()

        def reset(self) -> None:
            self.observed_song_names: list[str] = []
            self.observed_idol_song_keys: list[str] = []
            self.observed_grid_page_signatures: set[tuple[str, ...]] = set()
            self.grid_page_identities: list[str] = []
            # Keep the last proven top-row scan while a selected-card
            # animation temporarily hides the artwork bands.  This cache is
            # deliberately page-local: it is cleared before every page
            # swipe, so a stale coordinate can never be used for the next
            # page.
            self._grid_scan_targets: tuple[tuple[int, int, int, int], ...] = ()
            # Filtering the Overview sheet preserves its previous scroll
            # offset.  A fresh filtered grid therefore needs its own bounded
            # top-normalisation transaction before ``grid_target_index`` may
            # address a card.  This state is intentionally reset once per
            # picker run, not once per page: page swipes are part of the
            # ordered scan and must retain their overlap/cache behaviour.
            self._grid_top_normalized = False
            self._grid_top_normalization_swipes = 0
            self._grid_top_stable_signature: tuple[object, ...] | None = None
            self._grid_repeated_page_reads = 0
            self.grid_target_index = 0
            self.grid_unsafe_layout_samples = 0
            self._active_layout = "legacy"
            self.reached_end = False
            self.last_failure = ""

        @property
        def diagnostic(self) -> str:
            return (
                f"layout={self._active_layout}, grid_index={self.grid_target_index}, "
                f"unsafe_layout_samples={self.grid_unsafe_layout_samples}, "
                f"grid_top_normalized={self._grid_top_normalized}, "
                f"grid_top_swipes={self._grid_top_normalization_swipes}, "
                f"observed={len(self.observed_idol_song_keys)}, "
                f"reached_end={self.reached_end}, failure={self.last_failure!r}"
            )

        @staticmethod
        def _normalize(value: str) -> str:
            return "".join(
                character
                for character in unicodedata.normalize("NFKC", value)
                if not character.isspace() and character not in "[]【】"
            ).casefold()

        @staticmethod
        def _read_layout(context, image: np.ndarray) -> tuple[list[int], list[int]]:
            true_end = context.run_recognition(
                "GkmsToolChooseIdolTrueEnd",
                image,
                pipeline_override={
                    "GkmsToolChooseIdolTrueEnd": {
                        "recognition": {
                            "type": "OCR",
                            "param": {
                                "expected": ["True", "End"],
                                "roi": [430, 34, 266, 48],
                            },
                        }
                    }
                },
            )
            # Keep this layout choice identical to GkmsToolChooseNiaIdol.
            # Trying both ROIs is unsafe: on the normal Initial screen the
            # True End song ROI overlaps the character name.
            return (
                ([440, 128, 280, 64], [380, 90, 320, 45])
                if true_end is not None and true_end.hit
                else ([400, 98, 320, 64], [340, 60, 380, 45])
            )

        @staticmethod
        def _read_text(context, image: np.ndarray, node: str, roi: list[int]) -> str:
            detail = context.run_recognition(
                node,
                image,
                pipeline_override={
                    node: {
                        "recognition": {"type": "OCR", "param": {"roi": roi}}
                    }
                },
            )
            if detail is None or not detail.hit:
                return ""
            return "".join(
                str(getattr(item, "text", "")) for item in detail.all_results
            ).strip()

        def _read_identity(self, context, image: np.ndarray) -> tuple[str, str]:
            idol_roi, song_roi = self._read_layout(context, image)
            # A True End result is the only supported legacy layout.  The
            # current non-True-End screen is the four-column grid.  Do not
            # fall back when either grid header OCR is blank: legacy ROIs
            # overlap stamina/plan labels on this screen.
            if idol_roi == [440, 128, 280, 64]:
                self._active_layout = "legacy"
            else:
                grid_song = self._read_text(
                    context,
                    image,
                    "GkmsToolChooseIdolCarouselGridSong",
                    [140, 20, 430, 45],
                )
                grid_idol = self._read_text(
                    context,
                    image,
                    "GkmsToolChooseIdolCarouselGridName",
                    [140, 55, 360, 45],
                )
                self._active_layout = "grid"
                return grid_idol, grid_song
            return (
                self._read_text(
                    context, image, "GkmsToolChooseIdolCarouselName", idol_roi
                ),
                self._read_text(
                    context, image, "GkmsToolChooseIdolCarouselSong", song_roi
                ),
            )

        def _read_song(self, context, image: np.ndarray) -> str:
            return self._read_identity(context, image)[1]

        def _remember_identity(self, idol_name: str, song_name: str) -> str:
            normalized = f"{self._normalize(idol_name)}|{self._normalize(song_name)}"
            if normalized != "|" and self._normalize(song_name):
                if normalized not in self.observed_idol_song_keys:
                    self.observed_idol_song_keys.append(normalized)
                    self.observed_song_names.append(song_name)
            return normalized

        def _capture_identity(self, context) -> tuple[str, str] | None:
            capture_job = _wait_maa_screenshot_job(
                context.tasker.controller.post_screencap()
            )
            if not capture_job.succeeded:
                return None
            return self._read_identity(context, np.asarray(capture_job.get()))

        def _capture_selected_identity(
            self,
            context,
            target: tuple[int, int, int, int],
        ) -> tuple[str, str] | None:
            capture_job = _wait_maa_screenshot_job(
                context.tasker.controller.post_screencap()
            )
            if not capture_job.succeeded:
                return None
            image = np.asarray(capture_job.get())
            if not _produce_idol_grid_target_selected(image, target):
                return None
            return self._read_identity(context, image)

        @staticmethod
        def _grid_scroll_signature(
            image: np.ndarray,
            targets: tuple[tuple[int, int, int, int], ...],
        ) -> tuple[object, ...]:
            """Return a frame/scroll signature without reading card identity.

            A filtered Overview can retain a middle scroll offset even though
            its white-sheet layout predicate is already true.  The row
            geometry is the authoritative scroll marker; a small frame sample
            catches a settling animation where the same row centres have not
            moved yet.  This deliberately contains no OCR text or song target,
            so the top transaction is shared by every filtered card set.
            """

            row_geometry = tuple(
                (int(x), int(y), int(width), int(height))
                for x, y, width, height in targets
            )
            if not isinstance(image, np.ndarray) or image.ndim != 3:
                return (row_geometry, b"")
            # The sample is only a settling/page marker, not an image
            # classifier.  Keeping it sparse bounds the per-action state while
            # still observing transitions in the rendered sheet.
            sample = np.ascontiguousarray(image[520:1140:16, 0:720:16, :3])
            return (row_geometry, sample.tobytes())

        def _capture_grid_scroll_signature(
            self,
            context,
        ) -> tuple[tuple[object, ...], tuple[tuple[int, int, int, int], ...]] | None:
            """Capture one fresh grid frame for the top proof."""

            tasker = getattr(context, "tasker", None)
            controller = getattr(tasker, "controller", None)
            if controller is None:
                return None
            capture_job = _wait_maa_screenshot_job(
                controller.post_screencap()
            )
            if not capture_job.succeeded:
                return None
            image = np.asarray(capture_job.get())
            if not _produce_idol_expanded_grid_visible(image):
                return None
            targets = _produce_idol_grid_visible_targets(image)
            return self._grid_scroll_signature(image, targets), targets

        def _mark_grid_top_normalized(self) -> None:
            """Commit the top transaction and discard pre-normalisation cache."""

            self._grid_top_normalized = True
            self._grid_top_stable_signature = None
            self._grid_top_normalization_swipes = 0
            self.grid_unsafe_layout_samples = 0
            # No click/page may have happened before this transaction on a
            # fresh filtered grid.  Clearing these fields also prevents a
            # stale coordinate from being reused if a caller retries after a
            # transition frame.
            self.grid_target_index = 0
            self.grid_page_identities.clear()
            self._grid_scan_targets = ()

        def _normalize_grid_top(
            self,
            context,
            image: np.ndarray,
            targets: tuple[tuple[int, int, int, int], ...],
        ) -> bool | None:
            """Normalize one newly filtered grid before its first click.

            ``True`` means the top proof is complete and scanning may proceed;
            ``None`` means Maa should invoke this action again after the
            bounded swipe/settle pass; ``False`` is a fail-closed error.  A
            rendered first row near the canonical 695px origin proves the
            sheet is already at top.  Otherwise one downward Maa swipe is
            issued per bounded pass; unchanged non-top frames never get
            mistaken for top, and therefore cannot cause a mid-list click.
            """

            if self._grid_top_normalized:
                return True
            # Tests and the legacy fallback can drive the walker without a
            # controller/capture surface.  There is no filtered Overview
            # scroll to normalise in that mode; retain the old fixed-grid
            # behaviour while production always takes the branch below.
            tasker = getattr(context, "tasker", None)
            controller = getattr(tasker, "controller", None)
            if controller is None:
                self._mark_grid_top_normalized()
                return True

            # A caller that is already part-way through an ordered page scan
            # is not entering a new filtered grid.  Do not rewind its overlap
            # page; only reset() owns this top transaction.
            if self.grid_target_index > 0 or self.grid_page_identities:
                self._grid_top_normalized = True
                self._grid_top_stable_signature = None
                return True

            signature = self._grid_scroll_signature(image, targets)
            first_row_y = int(targets[0][1]) if targets else None

            # The first row is rendered at ~695px at top.  Keep a margin for a
            # one-pixel capture scale difference, but deliberately reject the
            # known mid-scroll ~771px origin.  On a fresh entry, prove that a
            # second frame has the same row/frame signature before clicking.
            # After a corrective swipe, the post-wait frame itself is the
            # bounded settle proof and can be committed immediately.
            if (
                first_row_y is not None
                and first_row_y <= self._GRID_TOP_FIRST_ROW_MAX_Y
            ):
                if self._grid_top_normalization_swipes:
                    self._mark_grid_top_normalized()
                    return True
                if self._grid_top_stable_signature != signature:
                    self._grid_top_stable_signature = signature
                    stable = self._capture_grid_scroll_signature(context)
                    if stable is None:
                        self._grid_top_stable_signature = None
                        self.last_failure = "grid-top-stability-capture-failed"
                        return None
                    stable_signature, stable_targets = stable
                    stable_first_row_y = (
                        int(stable_targets[0][1]) if stable_targets else None
                    )
                    if (
                        stable_first_row_y is not None
                        and stable_first_row_y <= self._GRID_TOP_FIRST_ROW_MAX_Y
                        and stable_signature == signature
                    ):
                        self._mark_grid_top_normalized()
                        self.last_failure = ""
                        return True
                    # The sheet is still settling or changed page/scroll;
                    # retain no click cache and re-prove on the next pass.
                    self._grid_top_stable_signature = stable_signature
                    self._grid_scan_targets = ()
                    self.last_failure = ""
                    return None
                self._mark_grid_top_normalized()
                self.last_failure = ""
                return True

            self._grid_scan_targets = ()
            # Preserve the old transition guard for the visibly unsafe
            # >940px origin.  It remains useful while Overview animates and
            # gives that branch a fixed, bounded number of safe samples before
            # the corrective swipe.
            if first_row_y is not None and first_row_y > 940:
                self.grid_unsafe_layout_samples += 1
                if (
                    self.grid_unsafe_layout_samples
                    <= self._GRID_TOP_UNSAFE_SAMPLE_LIMIT
                ):
                    self.last_failure = ""
                    time.sleep(0.15)
                    return None

            if self._grid_top_normalization_swipes >= self._GRID_TOP_MAX_SWIPES:
                self.last_failure = "grid-top-normalization-unproven"
                return False
            normalized = context.run_action(
                "GkmsToolNormalizeProduceIdolGridTop",
                pipeline_override={
                    "GkmsToolNormalizeProduceIdolGridTop": {
                        "recognition": {"type": "DirectHit", "param": {}},
                        "action": {
                            "type": "Swipe",
                            "param": {
                                # Downward is the idempotent Maa gesture that
                                # clamps the retained Overview offset to its
                                # top.  One bounded gesture per pass avoids
                                # racing the sheet animation.
                                "begin": [650, 720, 1, 1],
                                "end": [[650, 880, 1, 1]],
                            },
                        },
                        "post_wait_freezes": 300,
                    }
                },
            )
            if normalized is None or not normalized.success:
                self.last_failure = "grid-top-normalization-failed"
                return False
            self._grid_top_normalization_swipes += 1
            self._grid_top_stable_signature = None
            self.last_failure = ""
            return None

        def _run_grid(self, context) -> bool:
            tasker = getattr(context, "tasker", None)
            controller = getattr(tasker, "controller", None)
            visible_targets: tuple[tuple[int, int, int, int], ...] = ()
            if controller is not None:
                layout_job = _wait_maa_screenshot_job(
                    controller.post_screencap()
                )
                if not layout_job.succeeded:
                    self.last_failure = "grid-layout-capture-failed"
                    return False
                layout_image = np.asarray(layout_job.get())
                if not _produce_idol_expanded_grid_visible(layout_image):
                    opened = context.run_action(
                        "GkmsToolOpenProduceIdolOverview",
                        pipeline_override={
                            "GkmsToolOpenProduceIdolOverview": {
                                "recognition": {"type": "DirectHit", "param": {}},
                                "action": {
                                    "type": "Click",
                                    # The left-side Overview button owns the
                                    # complete white card grid.  Formation
                                    # Details on the lower right is a different
                                    # loadout page and must never be used for
                                    # idol-card enumeration.
                                    "param": {"target": [80, 747, 1, 1]},
                                },
                                "post_wait_freezes": 300,
                            }
                        },
                    )
                    if opened is None or not opened.success:
                        self.last_failure = "open-grid-overview-failed"
                        return False
                    self.grid_target_index = 0
                    self.grid_unsafe_layout_samples = 0
                    self.grid_page_identities.clear()
                    self._grid_scan_targets = ()
                    return True
                all_visible_targets = _produce_idol_grid_visible_targets(layout_image)
                if not all_visible_targets:
                    # A selected-card transition can leave the Overview
                    # header visible while all card artwork bands are blank
                    # for one or two PrintWindow frames.  This is not proof
                    # of an empty page and must not be reported as not-owned.
                    # Re-capture a bounded number of times before using the
                    # already proven row from this same page.
                    for _ in range(3):
                        time.sleep(0.15)
                        retry_job = _wait_maa_screenshot_job(
                            controller.post_screencap()
                        )
                        if not retry_job.succeeded:
                            continue
                        retry_image = np.asarray(retry_job.get())
                        if not _produce_idol_expanded_grid_visible(retry_image):
                            continue
                        layout_image = retry_image
                        all_visible_targets = _produce_idol_grid_visible_targets(
                            retry_image
                        )
                        if all_visible_targets:
                            break
                    if (
                        not all_visible_targets
                        and self.grid_target_index > 0
                        and self._grid_scan_targets
                    ):
                        all_visible_targets = self._grid_scan_targets
                # Filtering can leave the sheet at an arbitrary middle scroll
                # offset even though its expanded-grid predicate is already
                # true.  Normalize every newly filtered grid before deriving
                # ``grid_index=0`` targets; this runs for a y~771 origin too,
                # not only for the old >940 transition guard.
                if not self._grid_top_normalized:
                    normalized = self._normalize_grid_top(
                        context, layout_image, all_visible_targets
                    )
                    if normalized is None:
                        return True
                    if not normalized:
                        return False
                if not all_visible_targets:
                    self.last_failure = "expanded-grid-targets-unavailable"
                    return False
                # Only the top rendered row is a stable input surface.  The
                # current card's orange Confirm overlay can cover the middle
                # columns of lower rows and can also visually merge their
                # artwork bands.  Scan one complete row, then use the existing
                # one-row swipe and repeated-signature endpoint proof.  This
                # visits every row without ever clicking through the overlay.
                first_row_y = all_visible_targets[0][1]
                if first_row_y > 940:
                    # While Overview slides upward, its white sheet can already
                    # satisfy the layout predicate although the first card row
                    # still overlaps the 3D picker's Next/Details controls.
                    # Never click that region.  Usually waiting three Maa
                    # passes lets the animation settle; if it remains there,
                    # the sheet retained an old bottom scroll position and a
                    # downward Maa swipe normalizes it back to the top.
                    # Discard any prior safe-row cache as soon as a newer
                    # unsafe origin is observed; otherwise a later empty scan
                    # could reuse a coordinate from the wrong scroll offset.
                    self._grid_scan_targets = ()
                    self.grid_unsafe_layout_samples += 1
                    if (
                        self.grid_unsafe_layout_samples
                        <= self._GRID_TOP_UNSAFE_SAMPLE_LIMIT
                    ):
                        self.last_failure = ""
                        time.sleep(0.15)
                        return True
                    normalized = context.run_action(
                        "GkmsToolNormalizeProduceIdolGridTop",
                        pipeline_override={
                            "GkmsToolNormalizeProduceIdolGridTop": {
                                "recognition": {"type": "DirectHit", "param": {}},
                                "action": {
                                    "type": "Swipe",
                                    "param": {
                                        "begin": [650, 720, 1, 1],
                                        "end": [[650, 880, 1, 1]],
                                    },
                                },
                                "post_wait_freezes": 300,
                            }
                        },
                    )
                    self.grid_unsafe_layout_samples = 0
                    if normalized is None or not normalized.success:
                        self.last_failure = "grid-top-normalization-failed"
                        return False
                    self.last_failure = ""
                    return True
                self.grid_unsafe_layout_samples = 0
                visible_targets = tuple(
                    target
                    for target in all_visible_targets
                    if target[1] == first_row_y
                )
                if (
                    self.grid_target_index > 0
                    and self._grid_scan_targets
                    and len(visible_targets) < len(self._grid_scan_targets)
                ):
                    # A partial row during the same selection animation is a
                    # transient scan result, not an end marker.  Retain the
                    # complete row that was already proven before continuing
                    # at the current index.
                    visible_targets = self._grid_scan_targets
                if visible_targets:
                    self._grid_scan_targets = visible_targets
            targets = visible_targets or self._GRID_TARGETS
            if self.grid_target_index >= len(targets):
                self.last_failure = "grid-target-index-out-of-range"
                return False
            target = targets[self.grid_target_index]
            identity: tuple[str, str] | None = None
            observed_candidates: list[tuple[str, str]] = []
            # A successful SendMessage call proves delivery to the window, not
            # that the animated grid consumed it.  The orange selection
            # brackets make this tile click idempotently observable.  Re-click
            # the same tile at most once; never advance to another tile until
            # its own bracket and non-empty header identity agree.
            for click_attempt in range(2):
                result = context.run_action(
                    "GkmsToolAdvanceProduceIdolGridClick",
                    pipeline_override={
                        "GkmsToolAdvanceProduceIdolGridClick": {
                            "recognition": {"type": "DirectHit", "param": {}},
                            "action": {
                                "type": "Click",
                                "param": {"target": list(target)},
                            },
                            "post_wait_freezes": 300,
                        }
                    },
                )
                if result is None or not result.success:
                    self.last_failure = f"grid-click-failed:{target}"
                    return False
                for capture_attempt in range(8):
                    if capture_attempt:
                        time.sleep(0.15)
                    candidate = self._capture_selected_identity(context, target)
                    if candidate is None:
                        continue
                    observed_candidates.append(candidate)
                    if (
                        self._normalize(candidate[0])
                        and self._normalize(candidate[1])
                    ):
                        identity = candidate
                        break
                if identity is not None:
                    break
                if click_attempt == 0:
                    time.sleep(0.15)
            if identity is None:
                tail_job = _wait_maa_screenshot_job(
                    context.tasker.controller.post_screencap()
                )
                if tail_job.succeeded and not _produce_idol_grid_target_present(
                    np.asarray(tail_job.get()), target
                ):
                    # The first proven empty row-major cell terminates a
                    # partially filled final page.  Reaching it means the
                    # requested idol was not selectable in this filtered list,
                    # not that Maa failed to deliver the click.
                    self.reached_end = True
                    self.last_failure = ""
                    return False
                self.last_failure = (
                    f"selected-grid-identity-not-ready:{target}:"
                    f"{observed_candidates!r}"
                )
                return False
            idol_name, song_name = identity
            normalized = f"{self._normalize(idol_name)}|{self._normalize(song_name)}"
            if normalized == "|" or not self._normalize(song_name):
                self.last_failure = f"empty-grid-identity:{target}:{identity!r}"
                return False
            self.grid_page_identities.append(
                self._remember_identity(idol_name, song_name)
            )
            self.grid_target_index += 1
            if self.grid_target_index < len(targets):
                return True

            signature = tuple(self.grid_page_identities)
            self.grid_page_identities.clear()
            self.grid_target_index = 0
            if signature in self.observed_grid_page_signatures:
                self._grid_repeated_page_reads += 1
                if self._grid_repeated_page_reads >= self._GRID_PAGE_REPEAT_END_COUNT:
                    self.reached_end = True
                    self.last_failure = ""
                    return False
            else:
                self._grid_repeated_page_reads = 0
                self.observed_grid_page_signatures.add(signature)
            # The next page has a new row origin.  Never carry this page's
            # coordinates across the swipe; the next call must scan it anew.
            self._grid_scan_targets = ()
            swipe = context.run_action(
                "GkmsToolAdvanceProduceIdolGridPage",
                pipeline_override={
                    "GkmsToolAdvanceProduceIdolGridPage": {
                        "recognition": {"type": "DirectHit", "param": {}},
                        "action": {
                            "type": "Swipe",
                            "param": {
                                # Move one card row, retaining one overlap row.
                                # This cannot skip a card even if sorting or the
                                # number of cards changes.
                                "begin": [650, 940, 1, 1],
                                "end": [[650, 820, 1, 1]],
                            },
                        },
                        "post_wait_freezes": 300,
                    }
                },
            )
            if swipe is None or not swipe.success:
                self.last_failure = "grid-page-swipe-failed"
                return False
            self.last_failure = ""
            return True

        def run(self, context, argv):
            capture_job = _wait_maa_screenshot_job(
                context.tasker.controller.post_screencap()
            )
            if not capture_job.succeeded:
                self.last_failure = "initial-carousel-capture-failed"
                return False
            image = capture_job.get()
            idol_name, song_name = self._read_identity(context, np.asarray(image))
            if self._active_layout == "grid":
                # The header can still describe the previously selected card
                # while a newly filtered Overview is being rewound from a
                # retained middle scroll.  Do not let that stale identity
                # enter the observed sequence before the top transaction has
                # completed; grid card identities are recorded only after
                # their own selection brackets are proven.
                return self._run_grid(context)
            normalized = self._remember_identity(idol_name, song_name)
            if normalized == "|" or not self._normalize(song_name):
                self.last_failure = f"initial-carousel-identity-empty:{(idol_name, song_name)!r}"
                return False
            result = context.run_action(
                "GkmsToolAdvanceProduceIdolClick",
                pipeline_override={
                    "GkmsToolAdvanceProduceIdolClick": {
                        "recognition": {"type": "DirectHit", "param": {}},
                        "action": {
                            "type": "Click",
                            "param": {"target": [322, 804, 118, 186]},
                        },
                    }
                },
            )
            if result is None or not result.success:
                self.last_failure = "legacy-carousel-click-failed"
                return False

            # Treat a no-op as the real end only after two fresh stable OCR
            # samples.  A transient blank/old animation frame is ignored.
            unchanged_samples = 0
            time.sleep(0.5)
            for _ in range(8):
                time.sleep(0.2)
                followup_job = _wait_maa_screenshot_job(
                    context.tasker.controller.post_screencap()
                )
                if not followup_job.succeeded:
                    continue
                followup_idol, followup_song = self._read_identity(
                    context, np.asarray(followup_job.get())
                )
                followup_normalized = (
                    f"{self._normalize(followup_idol)}|"
                    f"{self._normalize(followup_song)}"
                )
                if followup_normalized == "|" or not self._normalize(followup_song):
                    unchanged_samples = 0
                    continue
                if followup_normalized != normalized:
                    if followup_normalized in self.observed_idol_song_keys:
                        self.reached_end = True
                        self.last_failure = ""
                        return False
                    self.observed_idol_song_keys.append(followup_normalized)
                    self.observed_song_names.append(followup_song)
                    return True
                unchanged_samples += 1
            if unchanged_samples >= 3:
                self.reached_end = True
                self.last_failure = ""
                return False
            self.last_failure = "legacy-carousel-followup-not-settled"
            return True

    return GkmsToolAdvanceProduceIdol


def _plan1_exam_ready_recognizer_type():
    """Use Maa's existing health marker as the only Plan1 entry readiness gate."""

    from maa.custom_recognition import CustomRecognition

    class GkmsToolInitialPlan1Ready(CustomRecognition):
        def analyze(self, context, argv):
            detail = context.run_recognition(
                "ProduceRecognitionHealthFlag", argv.image
            )
            return CustomRecognition.AnalyzeResult(
                box=[0, 0, 1, 1] if detail is not None and detail.hit else None,
                detail={"health_marker": bool(detail is not None and detail.hit)},
            )

    return GkmsToolInitialPlan1Ready


def _maa_baseline_exam_ready_recognizer_type():
    """Use Maa's health marker as the plan-neutral baseline readiness gate.

    The baseline intentionally knows nothing about card IDs, effects, or the
    native simulator.  Maa's bundled ``ProduceCardsAuto`` action owns the
    official recommendation/available-card fallback after this one visual
    readiness check succeeds.
    """

    from maa.custom_recognition import CustomRecognition

    class GkmsToolMaaBaselineReady(CustomRecognition):
        def analyze(self, context, argv):
            detail = context.run_recognition(
                "ProduceRecognitionHealthFlag", argv.image
            )
            return CustomRecognition.AnalyzeResult(
                box=[0, 0, 1, 1] if detail is not None and detail.hit else None,
                detail={"health_marker": bool(detail is not None and detail.hit)},
            )

    return GkmsToolMaaBaselineReady


def _nia_show_start_recognizer_type():
    """Create MaaGakumasu's exact orientation recognizer locally."""

    from maa.custom_recognition import CustomRecognition

    class ProduceShowStart(CustomRecognition):
        def analyze(self, context, argv):
            context.run_action("Click_1")
            height, width = argv.image.shape[:2]
            return CustomRecognition.AnalyzeResult(
                box=[0, 0, 1, 1] if height < width else None,
                detail={"landscape": height < width},
            )

    return ProduceShowStart


def _maa_action_receipt_sink_type():
    """Create a task-scoped receipt for Maa actions that actually succeeded.

    ``TaskDetail.nodes`` can be empty when a later JumpBack branch fails, even
    though earlier Maa clicks and terminal markers already ran.  The context
    event stream carries the real task id and exact ``Node.Action.Succeeded``
    name, so keep that small ordered receipt instead of inferring execution
    from the final task status or from screenshots.
    """

    from maa.context import ContextEventSink

    class GkmsToolMaaActionReceiptSink(ContextEventSink):
        _MAX_TASKS = 256

        def __init__(self) -> None:
            super().__init__()
            self._lock = threading.Lock()
            self._actions: dict[int, list[str]] = {}

        def on_raw_notification(self, _context, msg, details) -> None:
            if msg != "Node.Action.Succeeded" or not isinstance(details, Mapping):
                return
            task_id = details.get("task_id")
            name = details.get("name")
            if (
                isinstance(task_id, bool)
                or not isinstance(task_id, int)
                or not isinstance(name, str)
                or not name
            ):
                return
            with self._lock:
                if task_id not in self._actions and len(self._actions) >= self._MAX_TASKS:
                    self._actions.pop(min(self._actions), None)
                self._actions.setdefault(task_id, []).append(name)

        def actions(self, task_id: int | None) -> tuple[str, ...]:
            if task_id is None:
                return ()
            with self._lock:
                return tuple(self._actions.get(task_id, ()))

        def has_action(self, task_id: int | None, name: str) -> bool:
            return name in self.actions(task_id)

    return GkmsToolMaaActionReceiptSink


def _maa_job_id(job: Any) -> int | None:
    value = getattr(job, "job_id", None)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _maa_action_receipts(owner: Any, job: Any) -> tuple[str, ...]:
    sink = getattr(owner, "_recognition_action_receipts", None)
    actions = getattr(sink, "actions", None)
    if not callable(actions):
        return ()
    return tuple(actions(_maa_job_id(job)))


def _maa_task_names(
    owner: Any,
    job: Any,
    detail: Any | None,
) -> tuple[tuple[str, ...], str]:
    detail_names = (
        () if detail is None else tuple(node.name for node in detail.nodes)
    )
    # A successful TaskDetail is Maa's complete ordered result.  Context
    # notifications are asynchronous and may still be a non-empty prefix at
    # this instant, so they must not mask that complete detail.  The receipt
    # becomes authoritative only for failed/stopped jobs whose TaskDetail is
    # missing the actions Maa already executed.
    if bool(getattr(job, "succeeded", False)) and detail_names:
        return detail_names, "maa-task-detail"
    receipt_names = _maa_action_receipts(owner, job)
    if receipt_names:
        return receipt_names, "maa-action-receipt"
    return detail_names, "maa-task-detail"


def _maa_terminal_action(
    owner: Any,
    job: Any,
    terminal_names: tuple[str, ...],
) -> str | None:
    actions = _maa_action_receipts(owner, job)
    return next((name for name in terminal_names if name in actions), None)


def _stop_maa_task_and_recheck_terminal(
    owner: Any,
    tasker: Any,
    job: Any,
    terminal_names: tuple[str, ...],
) -> str | None:
    terminal = _maa_terminal_action(owner, job, terminal_names)
    _wait_maa_job(
        tasker.post_stop(),
        timeout_seconds=MAA_TASK_STOP_ACK_TIMEOUT_SECONDS,
        label="task stop acknowledgement",
    )
    # The terminal action callback can race with the stop acknowledgement.
    # Re-read the same task-id receipt after stop before declaring timeout.
    return terminal or _maa_terminal_action(owner, job, terminal_names)


def _normalize_capture_frame(
    image: np.ndarray,
) -> tuple[np.ndarray, tuple[int, int], str]:
    """Return one canonical frame without resampling its pixels.

    MaaFramework preserves the Win32 client aspect ratio when
    ``ScreenshotTargetShortSide`` is 720.  The verified game client is
    1081x1921, whose scaled integer height is 1279, while the game's resource
    and detector coordinate system is exactly 720x1280.  A one-row discrepancy
    is therefore an expected resize-rounding artifact, not permission to
    rescale an arbitrary image.

    Top-left coordinates stay invariant: a 1279-row source receives one copy
    of its bottom edge, and a 1281-row source loses only its bottom edge.  The
    caller must retain the returned native size for Maa input coordinates.
    """

    if not isinstance(image, np.ndarray):
        raise RuntimeError("MaaFramework capture is not a NumPy array")
    if image.ndim != 3 or image.shape[2] not in {3, 4} or image.size == 0:
        raise RuntimeError(
            f"MaaFramework returned an invalid image shape: {image.shape}"
        )
    if image.dtype != np.uint8:
        raise RuntimeError(f"MaaFramework returned an invalid image dtype: {image.dtype}")

    height, width = int(image.shape[0]), int(image.shape[1])
    native_size = (width, height)
    if (
        width != CANONICAL_CAPTURE_WIDTH
        or height not in _ALLOWED_NATIVE_CAPTURE_HEIGHTS
    ):
        raise RuntimeError(
            "MaaFramework capture geometry is outside the canonical contract: "
            f"{width}x{height}"
        )

    if height == CANONICAL_CAPTURE_HEIGHT - 1:
        # Duplicating one edge row is deterministic, introduces no new colour,
        # and leaves every source pixel at its original top-left coordinate.
        normalized = np.concatenate((image, image[-1:, :, :]), axis=0)
        operation = "pad-bottom-1-edge"
    elif height == CANONICAL_CAPTURE_HEIGHT + 1:
        normalized = np.array(
            image[:CANONICAL_CAPTURE_HEIGHT, :, :], copy=True, order="C"
        )
        operation = "crop-bottom-1"
    else:
        normalized = np.array(image, copy=True, order="C")
        operation = "identity"

    if normalized.shape[:2] != (CANONICAL_CAPTURE_HEIGHT, CANONICAL_CAPTURE_WIDTH):
        raise RuntimeError("MaaFramework canonical capture normalization failed")
    return normalized, native_size, operation


def _wait_maa_job(
    job: Any,
    *,
    timeout_seconds: float,
    label: str,
    deadline: float | None = None,
    stop_callback: Callable[[], Any] | None = None,
) -> Any:
    """Wait for one Maa job without pinning the single controller mailbox."""

    done = getattr(job, "done", None)
    if not isinstance(done, bool):
        # The pinned MaaFw binding always exposes bool ``done``.  Repository
        # test doubles are already-completed values; a real Maa object without
        # this contract is rejected instead of reintroducing unbounded wait.
        if type(job).__module__.startswith("maa."):
            raise TypeError(f"MaaFramework {label} job has no bool done state")
        return job
    job_deadline = time.monotonic() + float(timeout_seconds)
    if deadline is not None:
        job_deadline = min(job_deadline, float(deadline))
    while not done and time.monotonic() < job_deadline:
        time.sleep(0.05)
        done = bool(getattr(job, "done", False))
    if not done:
        if stop_callback is not None:
            stop_job = stop_callback()
            _wait_maa_job(
                stop_job,
                timeout_seconds=MAA_TASK_STOP_ACK_TIMEOUT_SECONDS,
                label=f"stop-after-{label}",
            )
        raise TimeoutError(f"MaaFramework {label} job timed out")
    return job


def _wait_maa_screenshot_job(
    job: Any,
    *,
    deadline: float | None = None,
) -> Any:
    return _wait_maa_job(
        job,
        timeout_seconds=MAA_SCREENSHOT_JOB_TIMEOUT_SECONDS,
        label="background screenshot",
        deadline=deadline,
    )


def _wait_maa_recognition_job(
    job: Any,
    *,
    label: str,
    deadline: float | None = None,
    stop_callback: Callable[[], Any] | None = None,
) -> Any:
    return _wait_maa_job(
        job,
        timeout_seconds=MAA_RECOGNITION_JOB_TIMEOUT_SECONDS,
        label=f"recognition:{label}",
        deadline=deadline,
        stop_callback=stop_callback,
    )


def _maa_stop_callback(tasker: Any) -> Callable[[], Any] | None:
    callback = getattr(tasker, "post_stop", None)
    return callback if callable(callback) else None


class MaaWin32Session:
    """One persistent MaaFramework controller bound to an exact HWND."""

    backend_name = "MaaFramework Win32 (PrintWindow + SendMessageWithWindowPos)"

    def __init__(self, hwnd: int, runtime_dir: Path) -> None:
        # Imports stay local so read-only/static tooling can import gkms_tool
        # without loading native MaaFramework DLLs.
        from maa.controller import Win32Controller
        from maa.define import MaaWin32InputMethodEnum, MaaWin32ScreencapMethodEnum
        from maa.toolkit import Toolkit

        runtime_dir.mkdir(parents=True, exist_ok=True)
        if not Toolkit.init_option(runtime_dir):
            raise RuntimeError("MaaFramework Toolkit 初始化失敗")

        controller = Win32Controller(
            hwnd,
            MaaWin32ScreencapMethodEnum.PrintWindow,
            # Unity ignores plain SendMessage mouse events because it also
            # consults the desktop pointer.  The cursor-position variant used
            # by upstream MaaGakumasu is incompatible with this secondary
            # high-DPI desktop (Maa's transformed SetCursorPos target can land
            # outside the virtual screen).  WithWindowPos is Maa's background
            # alternative: it briefly aligns/restores the HWND under the
            # existing pointer, never seizes or moves the user's mouse, while
            # preserving the verified 720x1280 client coordinates.
            MaaWin32InputMethodEnum.SendMessageWithWindowPos,
            MaaWin32InputMethodEnum.SendMessageWithWindowPos,
        )
        # MaaGakumasu's resources and click coordinates use a 720x1280
        # portrait canvas.  Keeping that coordinate system is important on
        # high-DPI desktops: raw 1081x1921 pixels are not desktop coordinates.
        if not controller.set_screenshot_target_short_side(720):
            raise RuntimeError("MaaFramework 無法設定 720px 短邊")
        connection = _wait_maa_job(
            controller.post_connection(),
            timeout_seconds=MAA_INITIALIZATION_JOB_TIMEOUT_SECONDS,
            label="controller connection",
        )
        if not connection.succeeded or not controller.connected:
            raise RuntimeError("MaaFramework Win32 controller 連線失敗")
        self._controller = controller
        # This remains Maa's native screenshot/input coordinate space.  It is
        # deliberately not overwritten with the normalized artifact size.
        self._capture_size: tuple[int, int] | None = None
        self._capture_normalization: str | None = None
        self._recognition_resource = None
        self._recognition_tasker = None
        self._recognition_action_receipts = None
        self._recognition_action_receipt_sink_id = None
        self._base_recognition_resource = None
        self._base_recognition_tasker = None
        self._nia_pro_recommended_replay_context = None

    @property
    def connected(self) -> bool:
        return bool(self._controller.connected)

    @property
    def info(self) -> dict[str, Any]:
        return dict(self._controller.info)

    @property
    def resolution(self) -> tuple[int, int]:
        width, height = self._controller.resolution
        return int(width), int(height)

    @property
    def coordinate_size(self) -> tuple[int, int]:
        """Maa's native screenshot/input coordinate size for the last frame."""

        if self._capture_size is None:
            raise RuntimeError("MaaFramework 尚未擷取可用的座標畫面")
        return self._capture_size

    @property
    def capture_contract(self) -> dict[str, Any]:
        """Describe the stable artifact and native click-coordinate mapping."""

        native = self._capture_size
        return {
            "artifact_width": CANONICAL_CAPTURE_WIDTH,
            "artifact_height": CANONICAL_CAPTURE_HEIGHT,
            "native_coordinate_width": native[0] if native is not None else None,
            "native_coordinate_height": native[1] if native is not None else None,
            "normalization": self._capture_normalization,
            "origin": "top-left",
            "resampling": False,
        }

    def capture(self, *, deadline: float | None = None) -> np.ndarray:
        # A failed refresh must not leave a stale coordinate geometry eligible
        # for a later input operation.
        self._capture_size = None
        self._capture_normalization = None
        job = _wait_maa_screenshot_job(
            self._controller.post_screencap(),
            deadline=deadline,
        )
        if not job.succeeded:
            raise RuntimeError("MaaFramework 背景擷取失敗")
        image = np.asarray(job.get())
        if image.ndim != 3 or image.shape[2] not in {3, 4} or image.size == 0:
            raise RuntimeError(f"MaaFramework 回傳不支援的影像格式：{image.shape}")
        # Keep the native frame geometry even when the portrait artifact
        # normalizer rejects it. The game intentionally rotates to 16:9 for
        # the post-produce live; Maa can still perform an exact background
        # click in that freshly captured coordinate space even though OCR and
        # detectors remain portrait-only.
        self._capture_size = (int(image.shape[1]), int(image.shape[0]))
        normalized, native_size, operation = _normalize_capture_frame(image)
        self._capture_size = native_size
        self._capture_normalization = operation
        return normalized

    def capture_native(self, *, deadline: float | None = None) -> np.ndarray:
        """Capture one navigation frame in the game's current orientation.

        Portrait OCR keeps using :meth:`capture` and its strict 720x1280
        artifact contract.  The post-Produce live intentionally rotates the
        same HWND to 1280x720, so background navigation needs a separate raw
        frame that never enters portrait detectors.
        """

        self._capture_size = None
        self._capture_normalization = None
        job = _wait_maa_screenshot_job(
            self._controller.post_screencap(),
            deadline=deadline,
        )
        if not job.succeeded:
            raise RuntimeError("MaaFramework native capture failed")
        image = np.asarray(job.get())
        if image.ndim != 3 or image.shape[2] not in {3, 4} or image.size == 0:
            raise RuntimeError(
                f"MaaFramework returned an invalid native image shape: {image.shape}"
            )
        if image.dtype != np.uint8:
            raise RuntimeError(
                f"MaaFramework returned an invalid native image dtype: {image.dtype}"
            )
        height, width = int(image.shape[0]), int(image.shape[1])
        portrait = (
            width == CANONICAL_CAPTURE_WIDTH
            and height in _ALLOWED_NATIVE_CAPTURE_HEIGHTS
        )
        landscape = (
            width == CANONICAL_CAPTURE_HEIGHT
            and height == CANONICAL_CAPTURE_WIDTH
        )
        if not (portrait or landscape):
            raise RuntimeError(
                "MaaFramework native capture geometry is outside the navigation "
                f"contract: {width}x{height}"
            )
        self._capture_size = (width, height)
        self._capture_normalization = "native-identity"
        return np.array(image, copy=True, order="C")

    def _ensure_read_only_recognizer(
        self,
        *,
        deadline: float | None = None,
    ):
        """Bind Maa's bundled OCR resources to this verified controller."""

        if self._recognition_tasker is not None:
            return self._recognition_resource, self._recognition_tasker
        from maa.resource import Resource
        from maa.tasker import Tasker

        resource = Resource()
        for name in ("base", "zh_CN"):
            bundle = _MAA_RESOURCE_ROOT / name
            job = _wait_maa_job(
                resource.post_bundle(bundle),
                timeout_seconds=MAA_INITIALIZATION_JOB_TIMEOUT_SECONDS,
                label=f"resource bundle:{name}",
                deadline=deadline,
            )
            if not job.succeeded:
                raise RuntimeError(f"MaaFramework failed to load resource bundle: {bundle}")
        tasker = Tasker()
        if not tasker.bind(resource, self._controller) or not tasker.inited:
            raise RuntimeError("MaaFramework read-only recognizer failed to initialize")
        action_receipts = _maa_action_receipt_sink_type()()
        sink_id = tasker.add_context_sink(action_receipts)
        if sink_id is None:
            raise RuntimeError("MaaFramework action receipt sink failed to initialize")
        self._recognition_resource = resource
        self._recognition_tasker = tasker
        self._recognition_action_receipts = action_receipts
        self._recognition_action_receipt_sink_id = sink_id
        return resource, tasker

    def _ensure_base_read_only_recognizer(
        self,
        *,
        deadline: float | None = None,
    ):
        """Bind Maa's base nodes without the Simplified-Chinese image override.

        Localify's Traditional-Chinese card-operation headers match the base
        Maa bundle.  The ordinary recognizer still loads ``zh_CN`` for OCR and
        every other localized page; only the eight existing card-operation
        template nodes use this second resource view.
        """

        resource = getattr(self, "_base_recognition_resource", None)
        tasker = getattr(self, "_base_recognition_tasker", None)
        if resource is not None and tasker is not None:
            return resource, tasker
        from maa.resource import Resource
        from maa.tasker import Tasker

        resource = Resource()
        bundle = _MAA_RESOURCE_ROOT / "base"
        job = _wait_maa_job(
            resource.post_bundle(bundle),
            timeout_seconds=MAA_INITIALIZATION_JOB_TIMEOUT_SECONDS,
            label="resource bundle:base-only",
            deadline=deadline,
        )
        if not job.succeeded:
            raise RuntimeError(f"MaaFramework failed to load resource bundle: {bundle}")
        tasker = Tasker()
        if not tasker.bind(resource, self._controller) or not tasker.inited:
            raise RuntimeError(
                "MaaFramework base read-only recognizer failed to initialize"
            )
        self._base_recognition_resource = resource
        self._base_recognition_tasker = tasker
        return resource, tasker

    def prewarm_read_only_recognizers(self) -> None:
        """Load both immutable Maa resource views before publishing ready."""

        self._ensure_read_only_recognizer()
        self._ensure_base_read_only_recognizer()

    def recognize_read_only_node(self, node_name: str) -> dict[str, Any]:
        """Run one fixed, DoNothing Maa recognition node on one fresh frame."""

        if node_name not in _READ_ONLY_RECOGNITION_NODES:
            raise ValueError(f"Maa read-only recognition node is not allowed: {node_name}")
        resource, tasker = self._ensure_read_only_recognizer()
        node = resource.get_node_object(node_name)
        if node is None or str(node.action.type) != "DoNothing":
            raise RuntimeError(f"Maa recognition node is missing or not read-only: {node_name}")
        image = self.capture()
        job = _wait_maa_recognition_job(
            tasker.post_recognition(
                node.recognition.type,
                node.recognition.param,
                image,
            ),
            label=node_name,
            stop_callback=_maa_stop_callback(tasker),
        )
        if not job.succeeded:
            raise RuntimeError(f"Maa recognition failed: {node_name}")
        detail = job.get()
        nodes = () if detail is None else tuple(detail.nodes)
        recognition = next(
            (value.recognition for value in reversed(nodes) if value.recognition is not None),
            None,
        )
        if recognition is None:
            return {"node": node_name, "hit": False, "results": []}
        results: list[dict[str, Any]] = []
        for value in recognition.filtered_results:
            box = getattr(value, "box", None)
            text = getattr(value, "text", None)
            score = getattr(value, "score", None)
            if box is None or not isinstance(text, str):
                continue
            serialized_box = _maa_box_to_list(box)
            if serialized_box is None:
                continue
            results.append(
                {
                    "text": text,
                    "score": float(score) if isinstance(score, (int, float)) else None,
                    "box": serialized_box,
                }
            )
        return {
            "node": node_name,
            "hit": bool(recognition.hit),
            "results": results,
        }

    def recognize_audition_cards_once(
        self,
        *,
        capture_path: str | Path,
        capture_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run ``ProduceRecognitionCards`` on exactly one fresh frame.

        The node is checked to be Maa's existing ``DoNothing`` recognition
        node, then the same captured image is saved and passed directly to
        ``post_recognition``.  This method never posts a Maa task and never
        calls click/input APIs.  Every detector row from ``all_results`` is
        serialized, including ``cards``, ``suggestions`` and ``useless``;
        malformed rows remain explicit rows so the downstream legal gate can
        abstain on an incomplete/invalid result.
        """

        if not isinstance(capture_path, (str, Path)) or not str(capture_path).strip():
            raise ValueError("capture_path must be non-empty text")
        if capture_metadata is not None and not isinstance(capture_metadata, Mapping):
            raise TypeError("capture_metadata must be a mapping or None")
        resource, tasker = self._ensure_read_only_recognizer()
        node_name = "ProduceRecognitionCards"
        node = resource.get_node_object(node_name)
        if node is None:
            raise RuntimeError("Maa ProduceRecognitionCards node is missing")
        action = getattr(node, "action", None)
        if str(getattr(action, "type", "")) != "DoNothing":
            raise RuntimeError("Maa ProduceRecognitionCards node is not read-only")
        recognition = getattr(node, "recognition", None)
        if recognition is None:
            raise RuntimeError("Maa ProduceRecognitionCards recognition is missing")

        # ``capture`` is the only screencap call in this method.  Keep the
        # timestamp after the call so it identifies this fresh artifact, not
        # a prior frame or the request creation time.
        image = self.capture()
        timestamp = time.time()
        output = Path(capture_path).resolve()
        self.save_bgr_png(image, output)
        height, width = int(image.shape[0]), int(image.shape[1])
        capture: dict[str, Any] = {
            "hwnd": None,
            "pid": None,
            "width": width,
            "height": height,
            "scale_x": width / CANONICAL_CAPTURE_WIDTH,
            "scale_y": height / CANONICAL_CAPTURE_HEIGHT,
            "timestamp": timestamp,
            "png_path": str(output),
            "capture_method": "MaaFramework(PrintWindow-background-recognition-only)",
        }
        capture["maa_capture_contract"] = self.capture_contract
        if capture_metadata is not None:
            for field in _AUDITION_CARD_CAPTURE_METADATA_FIELDS:
                if field not in capture_metadata:
                    continue
                value = capture_metadata[field]
                if field in {"hwnd", "pid"}:
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value <= 0
                    ):
                        raise ValueError(f"capture_metadata.{field} must be a positive integer")
                elif field == "ranking_visible_rank":
                    if value is not None and (
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 1
                    ):
                        raise ValueError(
                            "capture_metadata.ranking_visible_rank must be a positive integer"
                        )
                elif field in {
                    "ranking_source_sha256",
                    "ranking_request_sha256",
                    "ranking_replay_identity_sha256",
                }:
                    if value is not None and (
                        not isinstance(value, str)
                        or len(value) != 64
                        or value.casefold() != value
                        or any(character not in "0123456789abcdef" for character in value)
                    ):
                        raise ValueError(
                            f"capture_metadata.{field} must be a lowercase SHA-256 digest or None"
                        )
                elif value is not None and not isinstance(value, str):
                    raise ValueError(f"capture_metadata.{field} must be text or None")
                capture[field] = value
        job = _wait_maa_recognition_job(
            tasker.post_recognition(
                recognition.type,
                recognition.param,
                image,
            ),
            label=node_name,
            stop_callback=_maa_stop_callback(tasker),
        )
        if not job.succeeded:
            raise RuntimeError("Maa ProduceRecognitionCards recognition failed")
        detail = job.get()
        nodes = () if detail is None else tuple(getattr(detail, "nodes", ()))
        result = next(
            (
                value.recognition
                for value in reversed(nodes)
                if getattr(value, "recognition", None) is not None
            ),
            None,
        )
        raw_all_results = (
            None if result is None else getattr(result, "all_results", None)
        )
        if raw_all_results is None:
            all_results: tuple[Any, ...] = ()
            all_results_complete = False
        elif isinstance(raw_all_results, (str, bytes, bytearray)):
            all_results = (raw_all_results,)
            all_results_complete = False
        else:
            try:
                all_results = tuple(raw_all_results)
            except TypeError:
                all_results = (raw_all_results,)
                all_results_complete = False
            else:
                all_results_complete = True
        serialized = tuple(
            _serialize_audition_card_result(value, index)
            for index, value in enumerate(all_results)
        )
        return {
            "node": node_name,
            "hit": bool(result is not None and getattr(result, "hit", False)),
            "all_results": list(serialized),
            "all_results_complete": all_results_complete,
            "capture": capture,
            "recognition_result_count": len(serialized),
            "read_only": True,
            "input_submitted": False,
        }

    def _recognize_produce_template_node(
        self,
        resource: Any,
        tasker: Any,
        node_name: str,
        image: np.ndarray,
        *,
        expected_action: str,
    ) -> dict[str, Any]:
        """Recognize one allow-listed Produce node without running its action."""

        if node_name not in _PRODUCE_RANKING_NAVIGATION_NODES:
            raise ValueError(
                f"Maa Produce ranking navigation node is not allowed: {node_name}"
            )
        node = resource.get_node_object(node_name)
        if node is None:
            raise RuntimeError(f"Maa Produce ranking navigation node is missing: {node_name}")
        if str(getattr(getattr(node, "action", None), "type", "")) != expected_action:
            raise RuntimeError(
                f"Maa Produce ranking navigation action contract changed: {node_name}"
            )
        param = _maa_template_node_param(node, node_name)
        job = tasker.post_recognition(node.recognition.type, param, image)
        recognition = _maa_recognition_from_job(
            job,
            label=node_name,
            stop_callback=_maa_stop_callback(tasker),
        )
        box = _maa_recognition_box(recognition)
        score = None
        if recognition is not None:
            best = getattr(recognition, "best_result", None)
            value = getattr(best, "score", None) if best is not None else None
            if isinstance(value, (int, float)):
                score = float(value)
        return {
            "node": node_name,
            "hit": box is not None,
            "box": box,
            "score": score,
            "templates": [
                str(value) for value in getattr(param, "template", ())
            ],
        }

    def _wait_for_produce_template_node(
        self,
        resource: Any,
        tasker: Any,
        node_name: str,
        *,
        expected_action: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Wait for one official template gate, never clicking while waiting."""

        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            last = self._recognize_produce_template_node(
                resource,
                tasker,
                node_name,
                image,
                expected_action=expected_action,
            )
            if last["hit"]:
                return last
            time.sleep(0.15)
        raise TimeoutError(
            f"Maa Produce ranking navigation did not recognize {node_name}"
            + ("" if last is None else f" (last={last})")
        )

    def _click_recognized_produce_box(
        self,
        recognition: Mapping[str, Any],
        *,
        node_name: str,
    ) -> dict[str, Any]:
        """Click one recognized Produce target exactly once."""

        box = recognition.get("box")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(value) is not int for value in box)
            or box[2] <= 0
            or box[3] <= 0
        ):
            raise RuntimeError(
                f"Maa Produce ranking navigation {node_name} has no safe hit box"
            )
        x, y, width, height = box
        coordinate_width, coordinate_height = self.coordinate_size
        click_x = x + width // 2
        click_y = y + height // 2
        if not (
            0 <= click_x < coordinate_width
            and 0 <= click_y < coordinate_height
        ):
            raise RuntimeError(
                f"Maa Produce ranking navigation {node_name} hit box escaped the canvas"
            )
        # There is intentionally one call to Maa's input API per target.  The
        # caller verifies the next screen with a fresh capture and never
        # retries the click.
        result = dict(self.click(click_x, click_y))
        result.update(
            {
                "node": node_name,
                "box": list(box),
                "maa_x": click_x,
                "maa_y": click_y,
            }
        )
        return result

    @staticmethod
    def _validate_produce_ranking_navigation_contract(resource: Any) -> None:
        """Fail closed if Maa's upstream Produce templates/actions drift."""

        start = resource.get_node_object("ProduceStart")
        main = resource.get_node_object("ProduceMainPage")
        if start is None or main is None:
            raise RuntimeError("Maa Produce ranking navigation source node is missing")
        if str(getattr(getattr(start, "action", None), "type", "")) != "Click":
            raise RuntimeError("Maa ProduceStart is no longer a Click gate")
        if str(getattr(getattr(main, "action", None), "type", "")) != "DoNothing":
            raise RuntimeError("Maa ProduceMainPage must remain read-only")
        start_param = _maa_template_node_param(start, "ProduceStart")
        start_templates = tuple(str(value) for value in start_param.template)
        if start_templates != ("home.png",):
            raise RuntimeError(
                "Maa ProduceStart template drifted; active-run home_1 is forbidden"
            )
        main_param = _maa_template_node_param(main, "ProduceMainPage")
        main_templates = tuple(str(value) for value in main_param.template)
        if not {
            "produce/produce_mainpage_ranking.png",
            "produce/produce_mainpage_ranking_1.png",
        }.issubset(main_templates):
            raise RuntimeError("Maa ProduceMainPage ranking templates are incomplete")
        roi = tuple(int(value) for value in getattr(main_param, "roi", ()))
        if roi != (500, 1050, 200, 80):
            raise RuntimeError(
                f"Maa ProduceMainPage ranking ROI drifted: {roi!r}"
            )

    def _recognize_produce_ranking_page_once(
        self,
        tasker: Any,
        image: np.ndarray,
    ) -> dict[str, Any]:
        """Read-only OCR gate for the page opened by ProduceRanking."""

        from maa.define import AlgorithmEnum
        from maa.pipeline import JOCR

        param = JOCR(
            expected=list(_PRODUCE_RANKING_PAGE_EXPECTED_TEXT),
            roi=_PRODUCE_RANKING_PAGE_OCR_ROI,
            threshold=0.3,
        )
        job = tasker.post_recognition(AlgorithmEnum.OCR, param, image)
        recognition = _maa_recognition_from_job(
            job,
            label="ProduceRankingPageOCR",
            stop_callback=_maa_stop_callback(tasker),
        )
        results: list[dict[str, Any]] = []
        if recognition is not None:
            for value in getattr(recognition, "filtered_results", ()):
                text = getattr(value, "text", None)
                box = getattr(value, "box", None)
                if not isinstance(text, str):
                    continue
                serialized_box = _maa_box_to_list(box) if box is not None else None
                if serialized_box is None:
                    continue
                score = getattr(value, "score", None)
                results.append(
                    {
                        "text": text,
                        "box": serialized_box,
                        "score": float(score)
                        if isinstance(score, (int, float))
                        else None,
                    }
                )
        return {
            "node": "ProduceRankingPageOCR",
            "verified": bool(recognition is not None and recognition.hit),
            "expected": list(_PRODUCE_RANKING_PAGE_EXPECTED_TEXT),
            "roi": list(_PRODUCE_RANKING_PAGE_OCR_ROI),
            "results": results,
        }

    def _wait_for_produce_ranking_page(
        self,
        tasker: Any,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            last = self._recognize_produce_ranking_page_once(tasker, image)
            if last["verified"]:
                return last
            time.sleep(0.2)
        raise TimeoutError(
            "Maa Produce ranking page OCR verification timed out"
            + ("" if last is None else f" (last={last})")
        )

    @staticmethod
    def _recognize_fixed_ocr_once(
        tasker: Any,
        image: np.ndarray,
        *,
        node_name: str,
        expected: tuple[str, ...],
        roi: tuple[int, int, int, int],
    ) -> dict[str, Any]:
        """Recognize one fixed OCR gate and serialize only its visible labels."""

        from maa.define import AlgorithmEnum
        from maa.pipeline import JOCR

        if not expected:
            raise ValueError(f"Maa OCR gate {node_name} has no expected labels")
        if len(roi) != 4 or any(type(value) is not int for value in roi):
            raise ValueError(f"Maa OCR gate {node_name} ROI is invalid")
        if roi[2] <= 0 or roi[3] <= 0:
            raise ValueError(f"Maa OCR gate {node_name} ROI is empty")
        param = JOCR(expected=list(expected), roi=roi, threshold=0.3)
        job = tasker.post_recognition(AlgorithmEnum.OCR, param, image)
        recognition = _maa_recognition_from_job(
            job,
            label=node_name,
            stop_callback=_maa_stop_callback(tasker),
        )
        results: list[dict[str, Any]] = []
        if recognition is not None:
            for value in getattr(recognition, "filtered_results", ()):
                text = getattr(value, "text", None)
                box = getattr(value, "box", None)
                if not isinstance(text, str):
                    continue
                serialized_box = _maa_box_to_list(box) if box is not None else None
                if serialized_box is None:
                    continue
                score = getattr(value, "score", None)
                results.append(
                    {
                        "text": text,
                        "box": serialized_box,
                        "score": float(score)
                        if isinstance(score, (int, float))
                        else None,
                    }
                )
        return {
            "node": node_name,
            "verified": bool(recognition is not None and recognition.hit),
            "expected": list(expected),
            "roi": list(roi),
            "results": results,
        }

    @staticmethod
    def _recognize_open_ocr_once(
        tasker: Any,
        image: np.ndarray,
        *,
        node_name: str,
        roi: tuple[int, int, int, int],
    ) -> dict[str, Any]:
        """Read all OCR text in one fixed identity ROI without filtering it."""

        from maa.define import AlgorithmEnum
        from maa.pipeline import JOCR

        if len(roi) != 4 or any(type(value) is not int for value in roi):
            raise ValueError(f"Maa OCR gate {node_name} ROI is invalid")
        if roi[2] <= 0 or roi[3] <= 0:
            raise ValueError(f"Maa OCR gate {node_name} ROI is empty")
        job = tasker.post_recognition(
            AlgorithmEnum.OCR,
            JOCR(roi=roi, threshold=0.3),
            image,
        )
        recognition = _maa_recognition_from_job(
            job,
            label=node_name,
            stop_callback=_maa_stop_callback(tasker),
        )
        results: list[dict[str, Any]] = []
        if recognition is not None:
            for value in getattr(recognition, "all_results", ()):
                text = getattr(value, "text", None)
                box = getattr(value, "box", None)
                if not isinstance(text, str):
                    continue
                serialized_box = _maa_box_to_list(box) if box is not None else None
                if serialized_box is None:
                    continue
                score = getattr(value, "score", None)
                results.append(
                    {
                        "text": text,
                        "box": serialized_box,
                        "score": float(score)
                        if isinstance(score, (int, float))
                        else None,
                    }
                )
        return {
            "node": node_name,
            "roi": list(roi),
            "results": results,
        }

    @staticmethod
    def _ocr_has_any_label(
        recognition: Mapping[str, Any], expected: tuple[str, ...]
    ) -> bool:
        """Return whether one OCR result contains one of its allow-listed labels."""

        for result in recognition.get("results", ()):
            text = result.get("text") if isinstance(result, Mapping) else None
            if isinstance(text, str) and any(
                _maa_ocr_text_matches(text, value) for value in expected
            ):
                return True
        return False

    @classmethod
    def _ocr_unique_label(
        cls,
        recognition: Mapping[str, Any],
        *,
        expected: tuple[str, ...],
        node_name: str,
    ) -> dict[str, Any] | None:
        """Select one visible OCR label; ambiguity is always a hard failure."""

        matches: list[dict[str, Any]] = []
        for result in recognition.get("results", ()):
            if not isinstance(result, Mapping):
                continue
            text = result.get("text")
            if isinstance(text, str) and any(
                _maa_ocr_text_matches(text, value) for value in expected
            ):
                matches.append(dict(result))
        if len(matches) > 1:
            raise RuntimeError(
                f"Maa N.I.A. replay navigation {node_name} OCR is ambiguous"
            )
        if not matches:
            return None
        box = matches[0].get("box")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(type(value) is not int for value in box)
            or box[2] <= 0
            or box[3] <= 0
        ):
            raise RuntimeError(
                f"Maa N.I.A. replay navigation {node_name} OCR box is invalid"
            )
        return matches[0]

    def _recognize_fixed_template_once(
        self,
        resource: Any,
        tasker: Any,
        image: np.ndarray,
        *,
        node_name: str,
        expected_action: str,
        expected_templates: tuple[str, ...],
    ) -> dict[str, Any]:
        """Recognize one upstream template node without executing its action."""

        node = resource.get_node_object(node_name)
        if node is None:
            raise RuntimeError(
                f"Maa N.I.A. replay navigation source node is missing: {node_name}"
            )
        action = getattr(node, "action", None)
        if str(getattr(action, "type", "")) != expected_action:
            raise RuntimeError(
                f"Maa N.I.A. replay navigation action contract changed: {node_name}"
            )
        param = _maa_template_node_param(node, node_name)
        templates = tuple(str(value) for value in getattr(param, "template", ()))
        if expected_templates and templates != expected_templates:
            raise RuntimeError(
                f"Maa N.I.A. replay navigation template contract changed: {node_name}"
            )
        job = tasker.post_recognition(node.recognition.type, param, image)
        recognition = _maa_recognition_from_job(
            job,
            label=node_name,
            stop_callback=_maa_stop_callback(tasker),
        )
        box = _maa_recognition_box(recognition)
        score = None
        if recognition is not None:
            best = getattr(recognition, "best_result", None)
            value = getattr(best, "score", None) if best is not None else None
            if isinstance(value, (int, float)):
                score = float(value)
        return {
            "node": node_name,
            "hit": box is not None,
            "box": box,
            "score": score,
            "templates": list(templates),
        }

    def _wait_for_nia_recommended_replay_entry(
        self,
        resource: Any,
        tasker: Any,
        *,
        idol_name: tuple[str, ...],
        song_name: tuple[str, ...],
        deadline: float,
    ) -> dict[str, Any]:
        """Verify the N.I.A. logo plus one exact selected idol-card identity."""

        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            active = self._recognize_fixed_template_once(
                resource,
                tasker,
                image,
                node_name="ProduceContinue",
                expected_action="Click",
                expected_templates=("home_1.png",),
            )
            if active["hit"]:
                raise RuntimeError(
                    "Maa N.I.A. replay navigation rejected active Produce home_1"
                )

            nia = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayScenarioOCR",
                expected=_NIA_RECOMMENDED_REPLAY_NIA_TEXT,
                roi=_NIA_RECOMMENDED_REPLAY_NIA_ROI,
            )
            if not self._ocr_has_any_label(nia, _NIA_RECOMMENDED_REPLAY_NIA_TEXT):
                last = {
                    "active": active,
                    "scenario": nia,
                    "state": "scenario-not-nia",
                }
                time.sleep(0.2)
                continue

            # The current 3.3.0 picker is the grid layout.  The legacy
            # True-End carousel has different header ROIs and is allowed only
            # when its own marker is visible; no coordinate fallback exists.
            true_end = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayTrueEndOCR",
                expected=("True", "End"),
                roi=(430, 34, 266, 48),
            )
            if self._ocr_has_any_label(true_end, ("True", "End")):
                idol_roi = _NIA_RECOMMENDED_REPLAY_LEGACY_IDOL_ROI
                song_roi = _NIA_RECOMMENDED_REPLAY_LEGACY_SONG_ROI
                layout = "legacy-true-end"
            else:
                idol_roi = _NIA_RECOMMENDED_REPLAY_GRID_IDOL_ROI
                song_roi = _NIA_RECOMMENDED_REPLAY_GRID_SONG_ROI
                layout = "grid"
            idol = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayIdolOCR",
                expected=idol_name,
                roi=idol_roi,
            )
            song = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplaySongOCR",
                expected=song_name,
                roi=song_roi,
            )
            idol_hit = self._ocr_has_any_label(idol, idol_name)
            song_hit = self._ocr_has_any_label(song, song_name)
            if idol_hit and song_hit:
                return {
                    "state": "nia-idol-card-selected",
                    "layout": layout,
                    "scenario_gate": nia,
                    "idol_gate": idol,
                    "song_gate": song,
                }
            last = {
                "active": active,
                "scenario": nia,
                "idol": idol,
                "song": song,
                "state": "idol-card-not-selected",
            }
            time.sleep(0.2)
        raise TimeoutError(
            "Maa N.I.A. replay navigation did not verify the N.I.A. "
            "idol-card picker"
            + ("" if last is None else f" (last={last})")
        )

    def _wait_for_nia_recommended_replay_growth_button(
        self,
        tasker: Any,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        """Wait for the visible, non-AP ``培育資訊`` button."""

        last: dict[str, Any] | None = None
        expected = ("培育資訊", "育成情報", "育成情報")
        while time.monotonic() < deadline:
            image = self.capture()
            button = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayGrowthInfoOCR",
                expected=expected,
                roi=_NIA_RECOMMENDED_REPLAY_GROWTH_BUTTON_ROI,
            )
            try:
                unique = self._ocr_unique_label(
                    button,
                    expected=expected,
                    node_name="GrowthInfoButton",
                )
            except RuntimeError:
                # An ambiguous OCR frame is unsafe; do not keep rescanning and
                # potentially choose a different control after the animation.
                raise
            if unique is not None:
                return {"gate": button, "target": unique}
            last = button
            time.sleep(0.2)
        raise TimeoutError(
            "Maa N.I.A. replay navigation did not recognize the 培育資訊 button"
            + ("" if last is None else f" (last={last})")
        )

    def _wait_for_nia_recommended_replay_overview_confirm(
        self,
        tasker: Any,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        """Find the Overview sheet's confirm text without accepting Next."""

        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            button = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayOverviewConfirmOCR",
                expected=_NIA_RECOMMENDED_REPLAY_OVERVIEW_CONFIRM_TEXT,
                roi=_NIA_RECOMMENDED_REPLAY_OVERVIEW_CONFIRM_ROI,
            )
            unique = self._ocr_unique_label(
                button,
                expected=_NIA_RECOMMENDED_REPLAY_OVERVIEW_CONFIRM_TEXT,
                node_name="RecommendedReplayOverviewConfirm",
            )
            if unique is not None:
                return {"gate": button, "target": unique}
            last = button
            time.sleep(0.2)
        raise TimeoutError(
            "Maa N.I.A. replay navigation did not recognize the Overview confirm"
            + ("" if last is None else f" (last={last})")
        )

    def _wait_for_nia_recommended_replay_modal_button(
        self,
        tasker: Any,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        """Recognize the replay button inside the 培育資訊 modal."""

        last: dict[str, Any] | None = None
        expected = _NIA_RECOMMENDED_REPLAY_FINAL_TEXT
        while time.monotonic() < deadline:
            image = self.capture()
            button = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayModalButtonOCR",
                expected=expected,
                roi=_NIA_RECOMMENDED_REPLAY_MODAL_BUTTON_ROI,
            )
            unique = self._ocr_unique_label(
                button,
                expected=expected,
                node_name="RecommendedReplayModalButton",
            )
            if unique is not None:
                return {"gate": button, "target": unique}
            last = button
            time.sleep(0.2)
        raise TimeoutError(
            "Maa N.I.A. replay navigation did not recognize the modal "
            "おすすめリプレイ button"
            + ("" if last is None else f" (last={last})")
        )

    def _wait_for_nia_recommended_replay_page(
        self,
        tasker: Any,
        *,
        deadline: float,
        expected_difficulty: tuple[str, ...] = ("MASTER", "Master"),
        difficulty_node_name: str = "NiaRecommendedReplayFinalMasterOCR",
    ) -> dict[str, Any]:
        """Verify final replay header and one exact difficulty badge.

        The historical command defaults to the N.I.A. Master badge.  The
        Pro-only command passes ``("PRO", "Pro")`` explicitly; keeping the
        labels as a call-time contract prevents a Pro replay from being
        accepted merely because its header OCR matched.
        """

        if not expected_difficulty:
            raise ValueError("Maa replay difficulty OCR labels are required")
        if any(not isinstance(value, str) or not value for value in expected_difficulty):
            raise ValueError("Maa replay difficulty OCR labels must be non-empty text")
        if not isinstance(difficulty_node_name, str) or not difficulty_node_name:
            raise ValueError("Maa replay difficulty OCR node name is required")

        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            header = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayFinalHeaderOCR",
                expected=_NIA_RECOMMENDED_REPLAY_FINAL_TEXT,
                roi=_NIA_RECOMMENDED_REPLAY_FINAL_HEADER_ROI,
            )
            master = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name=difficulty_node_name,
                expected=expected_difficulty,
                roi=_NIA_RECOMMENDED_REPLAY_FINAL_DIFFICULTY_ROI,
            )
            header_verified = self._ocr_has_any_label(
                header, _NIA_RECOMMENDED_REPLAY_FINAL_TEXT
            )
            difficulty_verified = self._ocr_has_any_label(
                master, expected_difficulty
            )
            if header_verified and difficulty_verified:
                result = {
                    "header_gate": header,
                    "difficulty_gate": master,
                    "difficulty_expected": list(expected_difficulty),
                    "difficulty_verified_at_final_page": True,
                }
                normalized_difficulty = {
                    _normalize_maa_ocr_text(value) for value in expected_difficulty
                }
                if "pro" in normalized_difficulty:
                    result["pro_gate"] = master
                if "master" in normalized_difficulty:
                    # Compatibility field for the original Master-only
                    # command; the Pro path never relies on this key.
                    result["master_gate"] = master
                return result
            last = {
                "header_gate": header,
                "difficulty_gate": master,
                "difficulty_expected": list(expected_difficulty),
                "header_verified": header_verified,
                "difficulty_verified": difficulty_verified,
            }
            time.sleep(0.2)
        raise TimeoutError(
            "Maa N.I.A. replay navigation final OCR verification timed out"
            + ("" if last is None else f" (last={last})")
        )

    @staticmethod
    def _validate_replay_template_node(
        resource: Any,
        contract: ReplayTemplateContract,
    ) -> None:
        """Re-check one audited replay template against loaded Maa metadata."""

        node = resource.get_node_object(contract.node_name)
        if node is None:
            raise RuntimeError(
                "Maa replay control node is missing: " + contract.node_name
            )
        action = getattr(node, "action", None)
        if str(getattr(action, "type", "")) != "Click":
            raise RuntimeError(
                "Maa replay control action is not Click: " + contract.node_name
            )
        action_param = getattr(action, "param", None)
        if getattr(action_param, "target", True) is not True:
            raise RuntimeError(
                "Maa replay control has a fixed target: " + contract.node_name
            )
        param = _maa_template_node_param(node, contract.node_name)
        templates = tuple(str(value) for value in getattr(param, "template", ()))
        if templates != contract.templates:
            raise RuntimeError(
                "Maa replay control templates changed: " + contract.node_name
            )
        roi = tuple(int(value) for value in getattr(param, "roi", ()))
        if roi != contract.roi:
            raise RuntimeError(
                "Maa replay control ROI changed: " + contract.node_name
            )

    def audit_nia_replay_controls(self) -> dict[str, Any]:
        """Return a read-only audit of bundled replay controls.

        The ordinary Produce ``ProduceRecognitionSkipRound`` node is included
        as evidence, but it can never authorize input.  A future Maa bundle
        must ship a uniquely named replay-scoped TemplateMatch/Click node
        before this session will send a replay click.
        """

        return audit_bundled_replay_controls(_MAA_RESOURCE_ROOT).to_dict()

    def recognize_nia_replay_skip_once(self) -> dict[str, Any]:
        """Recognize a replay-scoped Skip template without clicking it."""

        audit = audit_bundled_replay_controls(_MAA_RESOURCE_ROOT)
        result = audit.to_dict()
        result.update(
            {
                "recognized": False,
                "safe_to_click": False,
                "gate": None,
            }
        )
        # Do not even capture the window when the bundle has no explicit
        # replay control.  In particular, the formal Produce Skip template
        # must not be surfaced as a usable hit box.
        if not audit.skip_input_allowed:
            return result
        resource, tasker = self._ensure_read_only_recognizer()
        contract = audit.replay_skip[0]
        self._validate_replay_template_node(resource, contract)
        image = self.capture()
        gate = self._recognize_fixed_template_once(
            resource,
            tasker,
            image,
            node_name=contract.node_name,
            expected_action="Click",
            expected_templates=contract.templates,
        )
        result.update(
            {
                "recognized": bool(gate["hit"]),
                "safe_to_click": bool(gate["hit"]),
                "gate": gate,
            }
        )
        return result

    def _wait_for_replay_template(
        self,
        resource: Any,
        tasker: Any,
        contract: ReplayTemplateContract,
        *,
        deadline: float,
    ) -> dict[str, Any]:
        """Wait for one explicit replay control and return its Maa hit box."""

        self._validate_replay_template_node(resource, contract)
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            last = self._recognize_fixed_template_once(
                resource,
                tasker,
                image,
                node_name=contract.node_name,
                expected_action="Click",
                expected_templates=contract.templates,
            )
            if last["hit"]:
                return last
            time.sleep(0.15)
        raise TimeoutError(
            "Maa replay control did not recognize "
            + contract.node_name
            + ("" if last is None else f" (last={last})")
        )

    def advance_nia_replay(
        self,
        *,
        target_turn: int | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Click replay Skip, optionally followed by a replay turn control.

        This command intentionally has no fallback to ``ProduceSkipRound`` or
        to a fixed coordinate.  With today's bundle it raises a typed
        fail-closed error because no replay-scoped control exists.
        """

        if target_turn is not None:
            if type(target_turn) is not int or target_turn < 0:
                raise ValueError("replay target_turn must be a non-negative int")
        if not 10.0 <= float(timeout_seconds) <= 180.0:
            raise ValueError(
                "N.I.A. replay control timeout must be between 10 and 180 seconds"
            )
        audit = audit_bundled_replay_controls(_MAA_RESOURCE_ROOT)
        if not audit.skip_input_allowed:
            raise RuntimeError(
                "Maa replay Skip input is fail-closed: "
                + ", ".join(audit.blockers)
            )
        if target_turn is not None and not audit.move_to_turn_allowed:
            raise RuntimeError(
                "Maa replay move-to-turn input is fail-closed: "
                + ", ".join(audit.blockers)
            )

        resource, tasker = self._ensure_read_only_recognizer()
        deadline = time.monotonic() + float(timeout_seconds)
        skip_gate = self._wait_for_replay_template(
            resource,
            tasker,
            audit.replay_skip[0],
            deadline=deadline,
        )
        skip_click = self._click_recognized_produce_box(
            skip_gate,
            node_name=audit.replay_skip[0].node_name,
        )
        clicks = [skip_click]
        move_gate = None
        move_click = None
        if target_turn is not None:
            move_gate = self._wait_for_replay_template(
                resource,
                tasker,
                audit.replay_move_to_turn[0],
                deadline=deadline,
            )
            move_click = self._click_recognized_produce_box(
                move_gate,
                node_name=audit.replay_move_to_turn[0].node_name,
            )
            clicks.append(move_click)
        return {
            "advanced": True,
            "reason": "replay-template-gated-input",
            "target_turn": target_turn,
            "click_count": len(clicks),
            "click_cap": 2 if target_turn is not None else 1,
            "skip_gate": skip_gate,
            "skip_click": skip_click,
            "move_to_turn_gate": move_gate,
            "move_to_turn_click": move_click,
            "telemetry_validation": "caller-owned",
        }

    def _wait_for_nia_recommended_replay_play_button(
        self,
        tasker: Any,
        *,
        deadline: float,
        visible_rank: int = 1,
    ) -> dict[str, Any]:
        """Return one caller-selected visible official replay button."""

        if isinstance(visible_rank, bool) or not isinstance(visible_rank, int):
            raise TypeError("visible replay rank must be an integer")
        if not 1 <= visible_rank <= 10:
            raise ValueError("visible replay rank must be between 1 and 10")

        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            gate = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayPlayButtonOCR",
                expected=_NIA_RECOMMENDED_REPLAY_PLAY_TEXT,
                roi=_NIA_RECOMMENDED_REPLAY_PLAY_ROI,
            )
            matches: list[dict[str, Any]] = []
            for result in gate.get("results", ()):
                if not isinstance(result, Mapping):
                    continue
                text = result.get("text")
                box = result.get("box")
                if not isinstance(text, str) or not any(
                    _maa_ocr_text_matches(text, expected)
                    for expected in _NIA_RECOMMENDED_REPLAY_PLAY_TEXT
                ):
                    continue
                if (
                    not isinstance(box, list)
                    or len(box) != 4
                    or any(type(value) is not int for value in box)
                    or box[2] <= 0
                    or box[3] <= 0
                ):
                    continue
                matches.append(dict(result))
            if len(matches) >= visible_rank:
                matches.sort(key=lambda value: (value["box"][1], value["box"][0]))
                selection = (
                    "topmost-visible-replay-button"
                    if visible_rank == 1
                    else f"visible-replay-rank-{visible_rank}"
                )
                return {
                    "gate": gate,
                    "target": matches[visible_rank - 1],
                    "visible_play_button_count": len(matches),
                    "visible_rank": visible_rank,
                    "selection": selection,
                }
            last = gate
            time.sleep(0.2)
        raise TimeoutError(
            "Maa N.I.A. recommended replay play button OCR timed out"
            + ("" if last is None else f" (last={last})")
        )

    def _wait_for_nia_recommended_replay_page_identity(
        self,
        tasker: Any,
        *,
        idol_names: tuple[str, ...],
        song_names: tuple[str, ...],
        deadline: float,
    ) -> dict[str, Any]:
        """Re-bind the visible replay page to the card opened by this worker."""

        if not idol_names or not song_names:
            raise ValueError("replay page identity requires idol and song labels")
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            image = self.capture()
            nia = self._recognize_fixed_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayPageNiaOCR",
                expected=_NIA_RECOMMENDED_REPLAY_PAGE_NIA_TEXT,
                roi=_NIA_RECOMMENDED_REPLAY_PAGE_NIA_ROI,
            )
            song = self._recognize_open_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayPageSongOCR",
                roi=_NIA_RECOMMENDED_REPLAY_PAGE_SONG_ROI,
            )
            idol = self._recognize_open_ocr_once(
                tasker,
                image,
                node_name="NiaRecommendedReplayPageIdolOCR",
                roi=_NIA_RECOMMENDED_REPLAY_PAGE_IDOL_ROI,
            )
            nia_verified = self._ocr_has_any_label(
                nia, _NIA_RECOMMENDED_REPLAY_PAGE_NIA_TEXT
            )
            song_verified = any(
                isinstance(row, Mapping)
                and isinstance(row.get("text"), str)
                and any(
                    _maa_identity_text_matches(row["text"], expected)
                    for expected in song_names
                )
                for row in song.get("results", ())
            )
            idol_verified = any(
                isinstance(row, Mapping)
                and isinstance(row.get("text"), str)
                and any(
                    _maa_identity_text_matches(row["text"], expected)
                    for expected in idol_names
                )
                for row in idol.get("results", ())
            )
            if nia_verified and song_verified and idol_verified:
                return {
                    "verified": True,
                    "nia_gate": nia,
                    "song_gate": song,
                    "idol_gate": idol,
                    "song_expected": list(song_names),
                    "idol_expected": list(idol_names),
                }
            last = {
                "verified": False,
                "nia_gate": nia,
                "song_gate": song,
                "idol_gate": idol,
                "nia_verified": nia_verified,
                "song_verified": song_verified,
                "idol_verified": idol_verified,
            }
            time.sleep(0.2)
        raise TimeoutError(
            "Maa N.I.A. recommended replay page card identity timed out"
            + ("" if last is None else f" (last={last})")
        )

    def start_first_nia_pro_recommended_replay(
        self,
        *,
        timeout_seconds: float = 30.0,
        visible_rank: int = 1,
    ) -> dict[str, Any]:
        """Start one visible N.I.A. Pro recommendation replay with Maa.

        The caller must already be on the verified recommendation page.  The
        method re-verifies the header and Pro badge, then clicks exactly one
        OCR-owned playback button.  Runtime ``isReplay`` evidence, not the
        click result, remains the authority that official replay actually
        started.
        """

        if not 10.0 <= float(timeout_seconds) <= 180.0:
            raise ValueError(
                "N.I.A. Pro replay start timeout must be between 10 and 180 seconds"
            )
        if isinstance(visible_rank, bool) or not isinstance(visible_rank, int):
            raise TypeError("visible replay rank must be an integer")
        if not 1 <= visible_rank <= 10:
            raise ValueError("visible replay rank must be between 1 and 10")
        context = getattr(self, "_nia_pro_recommended_replay_context", None)
        if (
            not isinstance(context, Mapping)
            or context.get("produce_id") != "produce-004"
            or context.get("difficulty") != "PRO"
            or not isinstance(context.get("idol_card_id"), str)
        ):
            raise RuntimeError(
                "N.I.A. Pro replay start requires a verified page opened by "
                "this Maa controller session"
            )
        raw_idol_names = context.get("idol_names")
        raw_song_names = context.get("song_names")
        if (
            not isinstance(raw_idol_names, (tuple, list))
            or not isinstance(raw_song_names, (tuple, list))
            or not all(isinstance(value, str) and value for value in raw_idol_names)
            or not all(isinstance(value, str) and value for value in raw_song_names)
        ):
            raise RuntimeError("verified replay context has no card identity labels")
        idol_names = tuple(raw_idol_names)
        song_names = tuple(raw_song_names)
        _resource, tasker = self._ensure_read_only_recognizer()
        deadline = time.monotonic() + float(timeout_seconds)
        page = self._wait_for_nia_recommended_replay_page(
            tasker,
            deadline=deadline,
            expected_difficulty=("PRO", "Pro"),
            difficulty_node_name="NiaRecommendedReplayFinalProOCR",
        )
        page_identity = self._wait_for_nia_recommended_replay_page_identity(
            tasker,
            idol_names=idol_names,
            song_names=song_names,
            deadline=deadline,
        )
        play = self._wait_for_nia_recommended_replay_play_button(
            tasker,
            deadline=deadline,
            visible_rank=visible_rank,
        )
        click = self._click_recognized_produce_box(
            play["target"],
            node_name="NiaRecommendedReplayPlayButton",
        )
        return {
            "started": True,
            "reason": "nia-pro-recommended-replay-play-button-clicked",
            "produce_id": "produce-004",
            "difficulty": "PRO",
            "idol_card_id": context["idol_card_id"],
            "selection": play["selection"],
            "visible_rank": play["visible_rank"],
            "visible_play_button_count": play["visible_play_button_count"],
            "click_count": 1,
            "click_cap": 1,
            "verified_page_context_retained": True,
            "page_gate": page,
            "page_identity_gate": page_identity,
            "play_button_gate": play["gate"],
            "play_button": play["target"],
            "play_click": click,
            "runtime_validation": "require-isReplay-true-transition",
        }

    def _open_nia_recommended_replay_from_picker_pipeline(
        self,
        *,
        produce_id: str,
        idol_card_id: str,
        timeout_seconds: float = 60.0,
        from_new_produce_home: bool = False,
        use_ap_drink: bool = False,
    ) -> dict[str, Any]:
        """Open one N.I.A. recommendation page through the bounded Maa picker.

        By default the caller is already on the N.I.A. idol-picker route.  A
        caller may instead opt into the fixed new-Produce-home entry.  That
        entry reuses Maa's existing Produce/scenario/difficulty gates, but the
        selected-card node is still the same hard terminal: support, memory,
        and Produce start remain unreachable. AP recovery is reachable only
        when the caller explicitly sets ``use_ap_drink``; it still must return
        to this same read-only selected-card terminal.
        """

        mode = {
            "produce-004": ("PRO", ("PRO", "Pro"), "Pro"),
            "produce-005": ("MASTER", ("MASTER", "Master"), "Master"),
        }.get(produce_id)
        if mode is None:
            raise ValueError(
                f"unsupported N.I.A. replay produce_id: {produce_id}"
            )
        difficulty, expected_difficulty, difficulty_name = mode
        if not isinstance(idol_card_id, str) or not idol_card_id.strip():
            raise ValueError("N.I.A. replay navigation idol_card_id is required")
        if not 10.0 <= float(timeout_seconds) <= 180.0:
            raise ValueError(
                "N.I.A. replay navigation timeout must be between 10 and 180 seconds"
            )
        if type(from_new_produce_home) is not bool:
            raise TypeError("from_new_produce_home must be bool")
        if type(use_ap_drink) is not bool:
            raise TypeError("use_ap_drink must be bool")
        self._nia_pro_recommended_replay_context = None
        deadline = time.monotonic() + float(timeout_seconds)

        def remaining_budget(*, minimum: float, preferred: float, label: str) -> float:
            remaining = deadline - time.monotonic()
            if remaining < minimum:
                raise TimeoutError(
                    f"Maa N.I.A. {difficulty_name} replay navigation exhausted "
                    f"its total "
                    f"deadline before {label}"
                )
            return min(preferred, remaining)

        pipeline_difficulty, pipeline = _nia_pro_recommended_replay_pipeline_override(
            idol_card_id=idol_card_id,
            produce_id=produce_id,
            use_ap_drink=use_ap_drink,
        )
        if pipeline_difficulty != difficulty:
            # This is an internal invariant, but keeping it explicit prevents
            # a future helper edit from silently broadening this command.
            raise RuntimeError(
                "N.I.A. replay navigation pipeline difficulty mismatch"
            )
        target_node = pipeline.get("ProduceChooseIdol")
        expected_target_action = {"type": "DoNothing", "param": {}}
        if (
            not isinstance(target_node, Mapping)
            or target_node.get("action") != expected_target_action
            or target_node.get("next") != []
        ):
            raise RuntimeError(
                "N.I.A. replay navigation target is not a read-only terminal"
            )

        resource, tasker = self._ensure_read_only_recognizer()
        if from_new_produce_home:
            self._wait_for_global_home_startup(
                tasker,
                timeout_seconds=remaining_budget(
                    minimum=10.0,
                    preferred=min(
                        60.0, max(10.0, float(timeout_seconds) * 0.5)
                    ),
                    label="startup Home",
                ),
            )
            self._ensure_new_produce_home(
                tasker,
                timeout_seconds=remaining_budget(
                    minimum=5.0,
                    preferred=min(
                        20.0, max(5.0, float(timeout_seconds) * 0.2)
                    ),
                    label="new-Produce Home preflight",
                ),
            )
        recognizer_name = "GkmsToolChooseNiaIdol"
        idol_recognizer = getattr(self, "_produce_idol_recognizer", None)
        if recognizer_name not in tuple(resource.custom_recognition_list):
            idol_recognizer = _nia_idol_recognizer_type()()
            if not resource.register_custom_recognition(
                recognizer_name, idol_recognizer
            ):
                raise RuntimeError("MaaFramework failed to register Produce idol OCR")
            self._produce_idol_recognizer = idol_recognizer
        filter_recognizer_name = "GkmsToolProduceIdolFilterState"
        filter_recognizer = getattr(self, "_produce_filter_recognizer", None)
        if filter_recognizer_name not in tuple(resource.custom_recognition_list):
            filter_recognizer = _produce_idol_filter_recognizer_type()()
            if not resource.register_custom_recognition(
                filter_recognizer_name, filter_recognizer
            ):
                raise RuntimeError(
                    "MaaFramework failed to register Produce idol filter state"
                )
            self._produce_filter_recognizer = filter_recognizer
        carousel_action_name = "GkmsToolAdvanceProduceIdol"
        carousel_action = getattr(self, "_produce_carousel_action", None)
        if carousel_action_name not in tuple(resource.custom_action_list):
            carousel_action = _produce_idol_carousel_action_type()()
            if not resource.register_custom_action(
                carousel_action_name, carousel_action
            ):
                raise RuntimeError(
                    "MaaFramework failed to register Produce carousel action"
                )
            self._produce_carousel_action = carousel_action
        elif carousel_action is None:
            raise RuntimeError("Maa Produce carousel action state is unavailable")
        for recognizer in (idol_recognizer, filter_recognizer):
            reset = getattr(recognizer, "reset", None)
            if callable(reset):
                reset()
        carousel_action.reset()

        # Both entries use the same audited pipeline and the same selected-card
        # hard terminal.  The home entry may traverse only Maa's normal
        # scenario/difficulty gates before reaching the picker; it can never
        # reach the support, memory, start, or AP-recovery nodes below.
        # The stock ``ProduceChooseIdolFlag`` template is tied to the original
        # untranslated header and no longer reaches threshold on the 3.3.0
        # translated detail grid.  The picker-only contract already requires
        # this page, so enter through our existing read-only overview-state
        # recognizer; the home route keeps Maa's full Produce graph.
        task_entry = (
            "Produce"
            if from_new_produce_home
            else "GkmsToolProduceIdolOverviewReady"
        )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Maa N.I.A. replay navigation exhausted its total deadline "
                "before the idol picker"
        )
        job = tasker.post_task(task_entry, pipeline)
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.1)
        if not job.done:
            terminal_receipt_observed = _stop_maa_task_and_recheck_terminal(
                self,
                tasker,
                job,
                ("ProduceChooseIdol",),
            )
            if terminal_receipt_observed is None:
                raise TimeoutError(
                    "Maa N.I.A. replay idol picker did not reach the target card"
                )
        detail = job.get() if job.done else None
        names, receipt_source = _maa_task_names(self, job, detail)
        forbidden = {
            "ProduceChooseIdolNext",
            "ProduceChooseSupport",
            "ProduceChooseSupportNext",
            "ProduceChooseMemory",
            "ProduceChooseMemoryNext",
            "ProduceEntry",
            "ProduceEntryFlag",
            "ProduceLackAPCancel",
            "ProduceUseItem",
            "ProduceUseNote",
            "ProduceUsePt",
            "ProduceLauncher",
            "GkmsToolProduceBootstrapSetupConfirm",
        }
        if not use_ap_drink:
            forbidden.update(
                {
                    "ProduceLackAP",
                    "ProduceLackAPRestore",
                    "ProduceLackAPUse",
                    "ProduceLackAPDecide",
                }
            )
        if not from_new_produce_home:
            forbidden.add("ProduceStart")
            forbidden.add("ProduceChooseDifficulty")
        forbidden_seen = tuple(name for name in names if name in forbidden)
        if (
            (not job.succeeded and "ProduceChooseIdol" not in names)
            or not names
            or "ProduceChooseIdol" not in names
            or forbidden_seen
        ):
            diagnostic = ""
            if idol_recognizer is not None:
                diagnostic += f" (idol_recognizer={idol_recognizer.diagnostic})"
            if filter_recognizer is not None:
                diagnostic += f" (filter_recognizer={filter_recognizer.diagnostic})"
            raise RuntimeError(
                "Maa N.I.A. replay idol picker stopped before its read-only "
                "target terminal"
                + ("" if not names else f" after {names[-1]}")
                + ("; forbidden=" + repr(forbidden_seen) if forbidden_seen else "")
                + diagnostic
            )

        overview_confirm = self._wait_for_nia_recommended_replay_overview_confirm(
            tasker,
            deadline=deadline,
        )
        overview_click = self._click_recognized_produce_box(
            overview_confirm["target"],
            node_name="NiaRecommendedReplayOverviewConfirm",
        )
        growth = self._wait_for_nia_recommended_replay_growth_button(
            tasker,
            deadline=deadline,
        )
        click = self._click_recognized_produce_box(
            growth["target"],
            node_name="NiaRecommendedReplayGrowthInfo",
        )
        modal = self._wait_for_nia_recommended_replay_modal_button(
            tasker,
            deadline=deadline,
        )
        modal_click = self._click_recognized_produce_box(
            modal["target"],
            node_name="NiaRecommendedReplayModalButton",
        )
        page = self._wait_for_nia_recommended_replay_page(
            tasker,
            deadline=deadline,
            expected_difficulty=expected_difficulty,
            difficulty_node_name=f"NiaRecommendedReplayFinal{difficulty_name}OCR",
        )
        visited_nodes = list(names)
        for node_name in (
            "NiaRecommendedReplayGrowthInfoOCR",
            "NiaRecommendedReplayModalButtonOCR",
            "NiaRecommendedReplayFinalHeaderOCR",
            f"NiaRecommendedReplayFinal{difficulty_name}OCR",
        ):
            if node_name not in visited_nodes:
                visited_nodes.append(node_name)
        replay_context = {
            "produce_id": produce_id,
            "difficulty": difficulty,
            "idol_card_id": idol_card_id,
            "idol_names": None,
            "song_names": None,
            "entry": (
                "new-produce-home"
                if from_new_produce_home
                else f"current-nia-{difficulty.lower()}-idol-picker"
            ),
        }
        catalog_entry = _default_nia_idol_catalog().require(idol_card_id)
        replay_context["idol_names"] = tuple(
            dict.fromkeys(
                (catalog_entry.idol_name, *catalog_entry.idol_name_aliases)
            )
        )
        replay_context["song_names"] = tuple(
            dict.fromkeys(
                (catalog_entry.song_name, *catalog_entry.song_name_aliases)
            )
        )
        if produce_id == "produce-004":
            self._nia_pro_recommended_replay_context = replay_context
        return {
            "opened": True,
            "reason": (
                "nia-pro-recommended-replay-page-verified"
                if produce_id == "produce-004"
                else "nia-recommended-replay-page-verified"
            ),
            "produce_id": produce_id,
            "difficulty": difficulty,
            "idol_card_id": idol_card_id,
            "entry": (
                "new-produce-home"
                if from_new_produce_home
                else f"current-nia-{difficulty.lower()}-idol-picker"
            ),
            "picker_terminal": "ProduceChooseIdol",
            "visited_nodes": visited_nodes,
            "click_count": 3,
            "click_cap": 3,
            "ap_safe": True,
            "ap_recovery_authorized": use_ap_drink,
            "active_produce_resume_blocked": True,
            "start_confirmation_clicked": False,
            "identity_gate": {
                "target": idol_card_id,
            "pipeline_entry": task_entry,
            "maa_task_id": _maa_job_id(job),
            "receipt_source": receipt_source,
                "terminal": "ProduceChooseIdol",
                "visited_nodes": list(names),
            },
            "overview_confirm_gate": overview_confirm["gate"],
            "overview_confirm_click": overview_click,
            "growth_info_gate": growth["gate"],
            "growth_info_click": click,
            "recommended_replay_modal_gate": modal["gate"],
            "recommended_replay_modal_click": modal_click,
            "recommended_replay_page_gate": page,
            "difficulty_verified_at_final_page": bool(
                page.get("difficulty_verified_at_final_page")
            ),
        }

    def open_nia_pro_recommended_replay(
        self,
        *,
        idol_card_id: str,
        timeout_seconds: float = 60.0,
        from_new_produce_home: bool = False,
        use_ap_drink: bool = False,
    ) -> dict[str, Any]:
        """Open one verified N.I.A. Pro recommendation replay page with Maa."""

        return self._open_nia_recommended_replay_from_picker_pipeline(
            produce_id="produce-004",
            idol_card_id=idol_card_id,
            timeout_seconds=timeout_seconds,
            from_new_produce_home=from_new_produce_home,
            use_ap_drink=use_ap_drink,
        )

    def open_nia_recommended_replay(
        self,
        *,
        idol_card_id: str,
        timeout_seconds: float = 60.0,
        from_new_produce_home: bool = False,
        use_ap_drink: bool = False,
    ) -> dict[str, Any]:
        """Open the N.I.A. recommendation replay page without starting Produce.

        By default this is the minimal safe entry from an already verified
        N.I.A. idol-card picker.  ``from_new_produce_home`` reuses the fixed
        Maa Produce/N.I.A./MASTER picker graph but hard-stops its selected-card
        node before support, memory, Produce start, or AP controls.  The picker
        does not reliably render its difficulty label; the final page must
        still prove the ``Master`` badge.  It then
        recognizes the requested idol/song pair, clicks only the OCR box for
        ``培育資訊``, then independently recognizes and clicks the modal's
        ``おすすめリプレイ``/``推薦回放`` button and verifies the compact
        final-page header.  The picker entry never posts ``Produce``; the
        explicit home entry may traverse the scenario and difficulty gates but
        never clicks start, next, support, memory, or AP UI.
        """

        if from_new_produce_home:
            return self._open_nia_recommended_replay_from_picker_pipeline(
                produce_id="produce-005",
                idol_card_id=idol_card_id,
                timeout_seconds=timeout_seconds,
                from_new_produce_home=True,
                use_ap_drink=use_ap_drink,
            )
        if not isinstance(idol_card_id, str) or not idol_card_id.strip():
            raise ValueError("N.I.A. replay navigation idol_card_id is required")
        if not 10.0 <= float(timeout_seconds) <= 180.0:
            raise ValueError(
                "N.I.A. replay navigation timeout must be between 10 and 180 seconds"
            )
        entry = _default_nia_idol_catalog().require(idol_card_id)
        resource, tasker = self._ensure_read_only_recognizer()
        deadline = time.monotonic() + float(timeout_seconds)
        identity = self._wait_for_nia_recommended_replay_entry(
            resource,
            tasker,
            idol_name=(entry.idol_name, *entry.idol_name_aliases),
            song_name=(entry.song_name, *entry.song_name_aliases),
            deadline=deadline,
        )
        growth = self._wait_for_nia_recommended_replay_growth_button(
            tasker,
            deadline=deadline,
        )
        click = self._click_recognized_produce_box(
            growth["target"],
            node_name="NiaRecommendedReplayGrowthInfo",
        )
        modal = self._wait_for_nia_recommended_replay_modal_button(
            tasker,
            deadline=deadline,
        )
        modal_click = self._click_recognized_produce_box(
            modal["target"],
            node_name="NiaRecommendedReplayModalButton",
        )
        page = self._wait_for_nia_recommended_replay_page(
            tasker,
            deadline=deadline,
        )
        return {
            "opened": True,
            "reason": "nia-recommended-replay-page-verified",
            "produce_id": "produce-005",
            "idol_card_id": idol_card_id,
            "entry": "verified-nia-idol-card-picker",
            "visited_nodes": [
                "NiaRecommendedReplayScenarioOCR",
                "NiaRecommendedReplayIdolOCR",
                "NiaRecommendedReplaySongOCR",
                "NiaRecommendedReplayGrowthInfoOCR",
                "NiaRecommendedReplayModalButtonOCR",
                "NiaRecommendedReplayFinalHeaderOCR",
                "NiaRecommendedReplayFinalMasterOCR",
            ],
            "click_count": 2,
            "click_cap": 2,
            "ap_safe": True,
            "active_produce_resume_blocked": True,
            "start_confirmation_clicked": False,
            "identity_gate": identity,
            "growth_info_gate": growth["gate"],
            "growth_info_click": click,
            "recommended_replay_modal_gate": modal["gate"],
            "recommended_replay_modal_click": modal_click,
            "recommended_replay_page_gate": page,
            "difficulty_verified_at_final_page": bool(
                page.get("difficulty_verified_at_final_page")
            ),
        }

    def open_produce_ranking(
        self,
        *,
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """Open the 3.3.0 Produce ranking page without starting Produce.

        This bounded route is intentionally narrower than ``start_produce``:
        it accepts only the new-run ``home.png`` tile, then the official
        ``ProduceMainPage`` ranking-icon gate.  It never posts the upstream
        ``Produce`` task, never recognizes/clicks a difficulty or idol card,
        and performs exactly two clicks with a fresh read-only verification
        after each one.
        """

        if not 10.0 <= float(timeout_seconds) <= 180.0:
            raise ValueError(
                "Produce ranking navigation timeout must be between 10 and 180 seconds"
            )
        resource, tasker = self._ensure_read_only_recognizer()
        self._validate_produce_ranking_navigation_contract(resource)
        deadline = time.monotonic() + float(timeout_seconds)

        start = self._wait_for_produce_template_node(
            resource,
            tasker,
            "ProduceStart",
            expected_action="Click",
            deadline=deadline,
        )
        start_click = self._click_recognized_produce_box(
            start,
            node_name="ProduceStart",
        )
        main = self._wait_for_produce_template_node(
            resource,
            tasker,
            "ProduceMainPage",
            expected_action="DoNothing",
            deadline=deadline,
        )
        ranking_click = self._click_recognized_produce_box(
            main,
            node_name="ProduceMainPage",
        )
        ranking_page = self._wait_for_produce_ranking_page(
            tasker,
            deadline=deadline,
        )
        return {
            "opened": True,
            "reason": "produce-ranking-page-verified",
            "visited_nodes": [
                "ProduceStart",
                "ProduceMainPage",
                "ProduceRankingPageOCR",
            ],
            "click_count": 2,
            "ap_safe": True,
            "active_produce_resume_blocked": True,
            "start_click": start_click,
            "ranking_click": ranking_click,
            "main_page_gate": main,
            "ranking_page_gate": ranking_page,
        }

    def recognize_nia_outer_once(
        self,
        *,
        capture_path: str | Path | None = None,
        scope: str = "all",
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        """Run Maa's original fixed N.I.A. recognizers on one native frame.

        This is a read-only batch.  It replaces Python's repeated full-screen
        template scans while leaving LocalSave policy and every click outside
        this method.  The original choice-page OCR gate is included;
        policy-specific OCR values retain their dedicated typed readers.
        """

        if scope not in _NIA_OUTER_RECOGNITION_SCOPES:
            raise ValueError(
                "unsupported N.I.A. recognition scope"
            )
        if not 5.0 <= float(timeout_seconds) <= 25.0:
            raise ValueError(
                "N.I.A. recognition timeout must be between 5 and 25 seconds"
            )
        batch_started = time.monotonic()
        deadline = batch_started + float(timeout_seconds)
        resource, tasker = self._ensure_read_only_recognizer(
            deadline=deadline
        )
        from maa.define import AlgorithmEnum
        from maa.pipeline import JTemplateMatch

        image = self.capture(deadline=deadline)
        if capture_path is not None:
            output = Path(capture_path).resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            self.save_bgr_png(image, output)
        results: list[dict[str, Any]] = []
        recognition_job_count = 0
        base_resource = None
        base_tasker = None
        for label, template, roi, threshold in _NIA_OUTER_TEMPLATE_BATCHES:
            if not _nia_outer_scope_allows(scope, label):
                continue
            source_name = _NIA_OUTER_TEMPLATE_SOURCE_NODES.get(label)
            if source_name is None:
                # SP markers and the tightly-scoped communication recovery
                # glyph are policy observations, not replacements for an
                # original Maa action node.
                recognition_type = AlgorithmEnum.TemplateMatch
                recognition_param = JTemplateMatch(
                    template=[template],
                    roi=roi,
                    threshold=[threshold],
                )
            else:
                source_resource = resource
                source_tasker = tasker
                if label in _NIA_BASE_RESOURCE_TEMPLATE_LABELS:
                    if base_resource is None or base_tasker is None:
                        base_resource, base_tasker = (
                            self._ensure_base_read_only_recognizer(
                                deadline=deadline
                            )
                        )
                    source_resource = base_resource
                    source_tasker = base_tasker
                source = source_resource.get_node_object(source_name)
                if source is None or source.recognition.type != AlgorithmEnum.TemplateMatch:
                    raise RuntimeError(
                        f"Maa N.I.A. recognition source is invalid: {source_name}"
                    )
                original = source.recognition.param
                if not isinstance(original, JTemplateMatch):
                    raise RuntimeError(
                        f"Maa N.I.A. recognition source is not TemplateMatch: {source_name}"
                    )
                if template not in original.template:
                    raise RuntimeError(
                        f"Maa N.I.A. template/source mismatch: {label}/{source_name}"
                    )
                recognition_type = source.recognition.type
                recognition_param = JTemplateMatch(
                    template=[template],
                    roi=original.roi,
                    roi_offset=original.roi_offset,
                    threshold=list(original.threshold),
                    order_by=original.order_by,
                    index=0,
                    method=original.method,
                    green_mask=original.green_mask,
                )
            recognition_tasker = (
                source_tasker if source_name is not None else tasker
            )
            recognition_job_count += 1
            job = _wait_maa_recognition_job(
                recognition_tasker.post_recognition(
                    recognition_type,
                    recognition_param,
                    image,
                ),
                label=f"nia-outer:{scope}:{label}",
                deadline=deadline,
                stop_callback=_maa_stop_callback(recognition_tasker),
            )
            if not job.succeeded:
                raise RuntimeError(f"Maa N.I.A. recognition failed: {label}")
            detail = job.get()
            nodes = () if detail is None else tuple(detail.nodes)
            recognition = next(
                (
                    value.recognition
                    for value in reversed(nodes)
                    if value.recognition is not None
                ),
                None,
            )
            if recognition is None or not recognition.hit:
                continue
            best = recognition.best_result
            box = recognition.box
            score = None
            if best is not None:
                score_value = getattr(best, "score", None)
                if isinstance(score_value, (int, float)):
                    score = float(score_value)
                best_box = getattr(best, "box", None)
                if best_box is not None:
                    box = best_box
            results.append(
                {
                    "node": label,
                    "box": _maa_box_to_list(box),
                    "score": score,
                }
            )
        for label, source_name in _NIA_OUTER_NODE_RECOGNITION_BATCHES:
            if not _nia_outer_scope_allows(scope, label):
                continue
            source = resource.get_node_object(source_name)
            if source is None:
                raise RuntimeError(
                    f"Maa N.I.A. recognition source is missing: {source_name}"
                )
            recognition_job_count += 1
            job = _wait_maa_recognition_job(
                tasker.post_recognition(
                    source.recognition.type,
                    source.recognition.param,
                    image,
                ),
                label=f"nia-outer:{scope}:{label}",
                deadline=deadline,
                stop_callback=_maa_stop_callback(tasker),
            )
            if not job.succeeded:
                raise RuntimeError(f"Maa N.I.A. recognition failed: {label}")
            detail = job.get()
            nodes = () if detail is None else tuple(detail.nodes)
            recognition = next(
                (
                    value.recognition
                    for value in reversed(nodes)
                    if value.recognition is not None
                ),
                None,
            )
            if recognition is None or not recognition.hit:
                continue
            box = recognition.box
            best = recognition.best_result
            score = None
            if best is not None:
                best_box = getattr(best, "box", None)
                if best_box is not None:
                    box = best_box
                score_value = getattr(best, "score", None)
                if isinstance(score_value, (int, float)):
                    score = float(score_value)
            results.append(
                {
                    "node": label,
                    "box": _maa_box_to_list(box),
                    "score": score,
                }
            )
        return {
            "nodes": results,
            "scope": scope,
            "capture_path": None if capture_path is None else str(Path(capture_path).resolve()),
            "recognition_job_count": recognition_job_count,
            "batch_elapsed_ms": round(
                (time.monotonic() - batch_started) * 1000,
                3,
            ),
            "batch_timeout_seconds": float(timeout_seconds),
        }

    def resume_active_produce(
        self,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Run only Maa's established active-Produce continuation chain.

        The entry is deliberately ``ProduceContinue`` rather than ``Produce``
        or ``ProduceStart``.  Therefore this capability cannot bootstrap a new
        run: Maa must first recognize the existing-run ``home_1.png`` tile,
        then its own confirmation button, and finally reach ``ProduceEntryFlag``.
        """

        if not 5.0 <= float(timeout_seconds) <= 60.0:
            raise ValueError("active Produce resume timeout must be between 5 and 60 seconds")
        resource, tasker = self._ensure_read_only_recognizer()
        expected_contracts = {
            "ProduceContinue": ("Click", ("home_1.png",)),
            "ProduceChooseContinue": ("Click", ("produce/continue.png",)),
            "ProduceEntryFlag": ("DoNothing", ()),
        }
        for name, (action_type, templates) in expected_contracts.items():
            node = resource.get_node_object(name)
            if node is None or str(node.action.type) != action_type:
                raise RuntimeError(f"Maa active Produce resume node is invalid: {name}")
            if templates:
                observed = tuple(str(value) for value in node.recognition.param.template)
                if observed != templates:
                    raise RuntimeError(
                        f"Maa active Produce resume template mismatch: {name}"
                    )

        pipeline = {
            "ProduceContinue": {"next": ["ProduceChooseContinue"]},
            "ProduceChooseContinue": {"next": ["ProduceEntryFlag"]},
            "ProduceEntryFlag": {"next": []},
        }
        job = tasker.post_task("ProduceContinue", pipeline)
        deadline = time.monotonic() + float(timeout_seconds)
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.1)
        if not job.done:
            terminal_receipt_observed = _stop_maa_task_and_recheck_terminal(
                self,
                tasker,
                job,
                ("ProduceEntryFlag",),
            )
            if terminal_receipt_observed is None:
                raise TimeoutError(
                    "Maa active Produce resume did not reach Produce entry"
                )
        detail = job.get() if job.done else None
        names, receipt_source = _maa_task_names(self, job, detail)
        expected = (
            "ProduceContinue",
            "ProduceChooseContinue",
            "ProduceEntryFlag",
        )
        positions = tuple(
            next((index for index, value in enumerate(names) if value == name), -1)
            for name in expected
        )
        if (
            (not job.succeeded and "ProduceEntryFlag" not in names)
            or "ProduceStart" in names
            or any(index < 0 for index in positions)
            or positions != tuple(sorted(positions))
        ):
            raise RuntimeError(
                "Maa active Produce resume failed closed"
                + ("" if not names else f" after {names[-1]}")
            )
        return {
            "resumed": True,
            "reason": "active-produce-entry-reached",
            "visited_nodes": list(names),
            "maa_task_id": _maa_job_id(job),
            "receipt_source": receipt_source,
        }

    def _ensure_new_produce_home(
        self,
        tasker: Any,
        *,
        timeout_seconds: float = 20.0,
    ) -> dict[str, Any]:
        """Require global Home before a fresh Produce bootstrap can run."""

        if not 5.0 <= float(timeout_seconds) <= 30.0:
            raise ValueError("Produce Home preflight timeout must be between 5 and 30 seconds")
        entry, pipeline = _produce_new_run_home_pipeline_override()
        job = tasker.post_task(entry, pipeline)
        deadline = time.monotonic() + float(timeout_seconds)
        terminal = "GkmsToolNewProduceHomeFlag"
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.1)
        if not job.done:
            terminal_receipt_observed = _stop_maa_task_and_recheck_terminal(
                self,
                tasker,
                job,
                (terminal,),
            )
            if terminal_receipt_observed is None:
                raise TimeoutError(
                    "Maa Produce Home preflight could not return to global Home in time"
                )
        detail = job.get() if job.done else None
        names, receipt_source = _maa_task_names(self, job, detail)
        if (
            (not job.succeeded and terminal not in names)
            or not names
            or terminal not in names
        ):
            raise RuntimeError(
                "Maa fresh Produce requires the exact global Home new-run tile"
                + ("" if not names else f" after {names[-1]}")
            )
        return {
            "ready": True,
            "reason": "global-home-new-produce-verified",
            "visited_nodes": list(names),
            "returned_home": any(
                name in {"ReturnHome", "BackHome"} for name in names
            ),
            "maa_task_id": _maa_job_id(job),
            "receipt_source": receipt_source,
        }

    def _wait_for_global_home_startup(
        self,
        tasker: Any,
        *,
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """Use Maa's startup recognizers to reach Home after a game restart.

        The elevated worker already launched the fixed PC executable.  This
        route therefore starts at Maa's screen-test node and never invokes
        ``StartApp``.  It reuses the bundled title/login/popup/home controls,
        removes the Android-only rotation custom recognizer, and makes Home a
        hard terminal.
        """

        if not 10.0 <= float(timeout_seconds) <= 120.0:
            raise ValueError(
                "global Home startup timeout must be between 10 and 120 seconds"
            )
        pipeline = {
            "StartUpTestScreen": {
                "recognition": {"type": "DirectHit", "param": {}},
                "action": {"type": "DoNothing", "param": {}},
                "max_hit": 128,
                "next": [
                    "[JumpBack]GkmsToolStartupTapToStart",
                    "GkmsToolStartupDownloadHeader",
                    "[JumpBack]StartUpLoginBonus",
                    "[JumpBack]ReturnHome",
                    "[JumpBack]BackHome",
                    "[JumpBack]StartUpCloseRoundButton",
                    "[JumpBack]StartUpCloseButton",
                    "StartUpHomeFlag",
                ],
            },
            "StartUpHomeFlag": {
                "recognition": {
                    "type": "TemplateMatch",
                    "param": {
                        "roi": list(_GLOBAL_HOME_PRODUCE_TILE_ROI),
                        "template": ["home.png", "home_1.png"],
                    },
                },
                "action": {"type": "DoNothing", "param": {}},
                "next": [],
            },
            "GkmsToolStartupTapToStart": {
                "recognition": {
                    "type": "OCR",
                    "param": {
                        "expected": list(_STARTUP_TAP_TO_START_TEXT),
                        "roi": list(_STARTUP_TAP_TO_START_ROI),
                    },
                },
                "action": {"type": "Click", "param": {}},
                "post_wait_freezes": 500,
                "next": [],
            },
            "GkmsToolStartupDownloadHeader": {
                "recognition": {
                    "type": "OCR",
                    "param": {
                        "expected": list(_STARTUP_DOWNLOAD_HEADER_TEXT),
                        "roi": list(_STARTUP_DOWNLOAD_HEADER_ROI),
                    },
                },
                "action": {"type": "DoNothing", "param": {}},
                "next": ["GkmsToolStartupDownloadConfirm"],
            },
            "GkmsToolStartupDownloadConfirm": {
                "recognition": {
                    "type": "OCR",
                    "param": {
                        "expected": list(_STARTUP_DOWNLOAD_CONFIRM_TEXT),
                        "roi": list(_STARTUP_DOWNLOAD_CONFIRM_ROI),
                    },
                },
                "action": {"type": "Click", "param": {}},
                "post_wait_freezes": 500,
                "next": ["StartUpTestScreen"],
            },
            # JumpBack should already return to StartUpTestScreen after one
            # action.  Empty child next-lists additionally ensure that none of
            # the Android counter/custom-action nodes can run in this PC-only
            # route if pipeline merge semantics change.
            "StartUpLoginBonus": {"next": []},
            "StartUpCloseButton": {"next": []},
            "StartUpCloseRoundButton": {"next": []},
            "ReturnHome": {"next": []},
            "BackHome": {"next": []},
        }
        job = tasker.post_task("StartUpTestScreen", pipeline)
        deadline = time.monotonic() + float(timeout_seconds)
        terminal = "StartUpHomeFlag"
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.1)
        if not job.done:
            terminal_receipt_observed = _stop_maa_task_and_recheck_terminal(
                self,
                tasker,
                job,
                (terminal,),
            )
            if terminal_receipt_observed is None:
                raise TimeoutError(
                    "Maa startup route did not reach global Home in time"
                )
        detail = job.get() if job.done else None
        names, receipt_source = _maa_task_names(self, job, detail)
        if (
            (not job.succeeded and terminal not in names)
            or not names
            or terminal not in names
            or "StartUpStartGame" in names
        ):
            raise RuntimeError(
                "Maa startup route stopped before global Home"
                + ("" if not names else f" after {names[-1]}")
            )
        return {
            "ready": True,
            "reason": "maa-startup-global-home-reached",
            "visited_nodes": list(names),
            "maa_task_id": _maa_job_id(job),
            "receipt_source": receipt_source,
        }

    def wait_global_home(
        self,
        *,
        timeout_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """Reach global Home through Maa without starting or resuming Produce."""

        _resource, tasker = self._ensure_read_only_recognizer()
        return self._wait_for_global_home_startup(
            tasker,
            timeout_seconds=timeout_seconds,
        )

    def start_nia_produce(
        self,
        *,
        produce_id: str,
        idol_card_id: str,
        timeout_seconds: float = 120.0,
        use_ap_drink: bool = False,
    ) -> MaaNiaProduceBootstrapResult:
        """Use Maa's fixed preparation flow, then stop at Produce entry.

        The method deliberately exposes neither an arbitrary Maa task name nor
        arbitrary pipeline JSON. It is the sole mutating Maa task available to
        the elevated IPC controller before LocalSave exists.
        """

        if produce_id not in {"produce-004", "produce-005"}:
            raise ValueError(f"unsupported N.I.A. produce_id: {produce_id}")
        return self.start_produce(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            timeout_seconds=timeout_seconds,
            use_ap_drink=use_ap_drink,
        )

    def start_produce(
        self,
        *,
        produce_id: str,
        idol_card_id: str,
        timeout_seconds: float = 120.0,
        use_ap_drink: bool = False,
    ) -> MaaNiaProduceBootstrapResult:
        """Enter one Initial/N.I.A. Produce using Maa's OCR card picker."""

        total_timeout = float(timeout_seconds)
        if not 10.0 <= total_timeout <= 300.0:
            raise ValueError("Produce bootstrap timeout must be between 10 and 300 seconds")
        if type(use_ap_drink) is not bool:
            raise TypeError("use_ap_drink must be bool")
        difficulty, pipeline = _produce_bootstrap_pipeline_override(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            use_ap_drink=use_ap_drink,
        )
        resource, tasker = self._ensure_read_only_recognizer()
        deadline = time.monotonic() + total_timeout
        self._wait_for_global_home_startup(
            tasker,
            timeout_seconds=min(120.0, total_timeout),
        )
        remaining = deadline - time.monotonic()
        if remaining < 5.0:
            raise TimeoutError(
                "Maa Produce bootstrap exhausted its deadline before Home preflight"
            )
        self._ensure_new_produce_home(
            tasker,
            timeout_seconds=min(
                remaining,
                min(20.0, max(5.0, total_timeout * 0.2)),
            ),
        )
        recognizer_name = "GkmsToolChooseNiaIdol"
        idol_recognizer = getattr(self, "_produce_idol_recognizer", None)
        if recognizer_name not in tuple(resource.custom_recognition_list):
            idol_recognizer = _nia_idol_recognizer_type()()
            if not resource.register_custom_recognition(
                recognizer_name, idol_recognizer
            ):
                raise RuntimeError("MaaFramework failed to register Produce idol OCR")
            self._produce_idol_recognizer = idol_recognizer
        filter_recognizer_name = "GkmsToolProduceIdolFilterState"
        filter_recognizer = getattr(self, "_produce_filter_recognizer", None)
        if filter_recognizer_name not in tuple(resource.custom_recognition_list):
            filter_recognizer = _produce_idol_filter_recognizer_type()()
            if not resource.register_custom_recognition(
                filter_recognizer_name, filter_recognizer
            ):
                raise RuntimeError(
                    "MaaFramework failed to register Produce idol filter state"
                )
            self._produce_filter_recognizer = filter_recognizer
        carousel_action_name = "GkmsToolAdvanceProduceIdol"
        carousel_action = getattr(self, "_produce_carousel_action", None)
        if carousel_action_name not in tuple(resource.custom_action_list):
            carousel_action = _produce_idol_carousel_action_type()()
            if not resource.register_custom_action(carousel_action_name, carousel_action):
                raise RuntimeError("MaaFramework failed to register Produce carousel action")
            self._produce_carousel_action = carousel_action
        elif carousel_action is None:
            raise RuntimeError("Maa Produce carousel action state is unavailable")
        for recognizer in (idol_recognizer, filter_recognizer):
            reset = getattr(recognizer, "reset", None)
            if callable(reset):
                reset()
        carousel_action.reset()

        job = tasker.post_task("Produce", pipeline)
        maa_task_id = _maa_job_id(job)
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.1)
        terminal_receipt_observed = (
            "ProduceEntryFlag" in _maa_action_receipts(self, job)
        )
        if not job.done:
            terminal_receipt_observed = bool(
                _stop_maa_task_and_recheck_terminal(
                    self,
                    tasker,
                    job,
                    ("ProduceEntryFlag",),
                )
            )
            if not terminal_receipt_observed:
                raise TimeoutError(
                    "Maa Produce preparation did not reach Produce entry in time"
                )
        detail = job.get() if job.done else None
        names, receipt_source = _maa_task_names(self, job, detail)
        terminal_receipt_observed = (
            terminal_receipt_observed or "ProduceEntryFlag" in names
        )
        observed_song_names = (
            ()
            if carousel_action is None
            else tuple(carousel_action.observed_song_names)
        )
        if not job.succeeded and not terminal_receipt_observed and not (
            carousel_action is not None and carousel_action.reached_end
        ):
            diagnostic = (
                ""
                if carousel_action is None
                else f" ({carousel_action.diagnostic})"
            )
            if idol_recognizer is not None:
                diagnostic += f" (idol_recognizer={idol_recognizer.diagnostic})"
            if filter_recognizer is not None:
                diagnostic += f" (filter_recognizer={filter_recognizer.diagnostic})"
            raise RuntimeError(
                "Maa Produce preparation failed"
                + ("" if not names else f" after {names[-1]}")
                + diagnostic
            )
        if "ProduceEntryFlag" in names:
            reason, started = "produce-entry-reached", True
        elif "ProduceLackAPCancel" in names:
            reason, started = "ap-insufficient-cancelled", False
        elif "ProduceContinue" in names:
            reason, started = "existing-produce-detected", False
        elif carousel_action is not None and carousel_action.reached_end:
            reason, started = "idol-card-not-selectable", False
        else:
            reason, started = "produce-entry-not-reached", False
        terminal_action_index = (
            names.index("ProduceEntryFlag")
            if "ProduceEntryFlag" in names
            else None
        )
        trailing_actions = (
            ()
            if terminal_action_index is None
            else names[terminal_action_index + 1 :]
        )
        return MaaNiaProduceBootstrapResult(
            started=started,
            reason=reason,
            produce_id=produce_id,
            difficulty=difficulty,
            idol_card_id=idol_card_id,
            visited_nodes=names,
            observed_song_names=observed_song_names,
            maa_task_id=maa_task_id,
            receipt_source=receipt_source,
            terminal_action_index=terminal_action_index,
            trailing_actions=trailing_actions,
        )

    def run_initial_plan1_exam(self, *, timeout_seconds: float = 240.0) -> dict[str, Any]:
        """Run MaaGakumasu's established Plan1 card player for one exam.

        This deliberately reuses the upstream custom action and its bundled
        OCR nodes instead of creating a second card detector or fixed click
        script.  The caller remains responsible for proving that the current
        ExamSave belongs to an Initial Plan1 lesson/audition.
        """

        if not 10.0 <= float(timeout_seconds) <= 600.0:
            raise ValueError("Plan1 exam timeout must be between 10 and 600 seconds")
        resource, tasker = self._ensure_read_only_recognizer()
        recognizer_name = "GkmsToolInitialPlan1CardsFlagAuto"
        action_name = "GkmsToolInitialPlan1CardsAuto"
        if (
            recognizer_name not in tuple(resource.custom_recognition_list)
            or action_name not in tuple(resource.custom_action_list)
        ):
            ProduceCardsAuto = _completion_safe_produce_cards_action_type()
            ProduceCardsFlagAuto = _plan1_exam_ready_recognizer_type()

            if (
                recognizer_name not in tuple(resource.custom_recognition_list)
                and not resource.register_custom_recognition(
                    recognizer_name, ProduceCardsFlagAuto()
                )
            ):
                raise RuntimeError(
                    "MaaFramework failed to register Plan1 ready recognizer"
                )
            if (
                action_name not in tuple(resource.custom_action_list)
                and not resource.register_custom_action(action_name, ProduceCardsAuto())
            ):
                raise RuntimeError("MaaFramework failed to register Plan1 card action")
        pipeline = {
            "GkmsToolInitialPlan1Exam": {
                "recognition": {
                    "type": "Custom",
                    "param": {"custom_recognition": recognizer_name},
                },
                "action": {
                    "type": "Custom",
                    "param": {"custom_action": action_name},
                },
                "next": [],
            }
        }
        deadline = time.monotonic() + float(timeout_seconds)
        last_names: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            job = tasker.post_task("GkmsToolInitialPlan1Exam", pipeline)
            while not job.done and time.monotonic() < deadline:
                time.sleep(0.1)
            if not job.done:
                _wait_maa_job(
                    tasker.post_stop(),
                    timeout_seconds=MAA_TASK_STOP_ACK_TIMEOUT_SECONDS,
                    label="plan1 exam stop",
                )
                break
            detail = job.get()
            last_names = (
                () if detail is None else tuple(node.name for node in detail.nodes)
            )
            if job.succeeded:
                return {
                    "accepted": True,
                    "terminal": True,
                    "reason": "maa-plan1-exam-finished",
                    "visited_nodes": list(last_names),
                }
            # This is Maa's original readiness gate.  A miss while the lesson
            # is entering is not an action failure; resubmit the same native
            # OCR/action pair without introducing another screen detector.
            time.sleep(0.5)
        raise TimeoutError(
            "Maa Plan1 card player did not reach exam end in time"
            + ("" if not last_names else f" after {last_names[-1]}")
        )

    def run_maa_baseline_exam(
        self,
        *,
        timeout_seconds: float = MAA_BASELINE_EXAM_TOTAL_TIMEOUT_SECONDS,
        idle_timeout_seconds: float = MAA_BASELINE_EXAM_IDLE_TIMEOUT_SECONDS,
        readiness_retry_seconds: float = MAA_BASELINE_EXAM_READINESS_RETRY_SECONDS,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        progress_reader: Callable[[], MaaBaselineExamProgressSignature | None]
        | None = None,
        foreground_recovery_callback: Callable[[], Any] | None = None,
        exam_save_path: str | Path | None = None,
        tasker: Any | None = None,
        unified_legal_action_snapshot_provider: Callable[[Any], Any] | None = None,
        monitor_source_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Run MaaGakumasu's plan-neutral completion baseline for one exam.

        This is the completion path, not the optimization path.  It reuses the
        game's recommendation marker and Maa's available/unavailable card
        detector, and therefore does not require the exact simulator to admit
        every card, drink, item, or trigger.  Callers must still prove that the
        current screen belongs to the expected Produce run before dispatch.
        """

        timeout_seconds = float(timeout_seconds)
        idle_timeout_seconds = float(idle_timeout_seconds)
        readiness_retry_seconds = float(readiness_retry_seconds)
        if not 10.0 <= timeout_seconds <= MAA_BASELINE_EXAM_TOTAL_TIMEOUT_SECONDS:
            raise ValueError(
                "Maa baseline exam timeout must be between 10 and 600 seconds"
            )
        if not 0.0 < idle_timeout_seconds <= MAA_BASELINE_EXAM_IDLE_TIMEOUT_SECONDS:
            raise ValueError(
                "Maa baseline exam idle timeout must be between 0 and 90 seconds"
            )
        if readiness_retry_seconds < 0.0:
            raise ValueError("Maa baseline exam readiness retry must be non-negative")
        monotonic = time.monotonic if monotonic is None else monotonic
        sleep = time.sleep if sleep is None else sleep
        if not callable(monotonic) or not callable(sleep):
            raise TypeError("Maa baseline exam clock and sleep must be callable")
        if tasker is None:
            resource, tasker = self._ensure_read_only_recognizer()
        else:
            resource = getattr(self, "_recognition_resource", None)
        recognizer_name = "GkmsToolMaaBaselineCardsFlagAuto"
        action_name = "GkmsToolMaaBaselineCardsAuto"
        exact_transitions: list[dict[str, Any]] = []
        ProduceCardsAuto = _completion_safe_produce_cards_action_type()
        if (
            recognizer_name not in tuple(resource.custom_recognition_list)
            or action_name not in tuple(resource.custom_action_list)
        ):
            ProduceCardsFlagAuto = _maa_baseline_exam_ready_recognizer_type()

            if (
                recognizer_name not in tuple(resource.custom_recognition_list)
                and not resource.register_custom_recognition(
                    recognizer_name, ProduceCardsFlagAuto()
                )
            ):
                raise RuntimeError(
                    "MaaFramework failed to register baseline ready recognizer"
                )
            if (
                action_name not in tuple(resource.custom_action_list)
                and not resource.register_custom_action(action_name, ProduceCardsAuto())
            ):
                raise RuntimeError(
                    "MaaFramework failed to register baseline card action"
                )
        monitor_publisher = None
        if isinstance(monitor_source_run_id, str) and monitor_source_run_id.strip():
            try:
                from .live_monitor_event_stream import (
                    get_live_monitor_event_publisher,
                )

                monitor_publisher = get_live_monitor_event_publisher()
            except Exception:
                monitor_publisher = None

        def observe_exact_transition(row: Mapping[str, Any]) -> None:
            # Preserve the exact recorder result byte-for-byte.  The monitor
            # fanout is a non-blocking in-memory queue put; its daemon alone
            # owns JSON serialization, file I/O, and event signaling.
            exact_transitions.append(row)
            if monitor_publisher is not None:
                try:
                    monitor_publisher.publish(
                        str(monitor_source_run_id),
                        row,
                    )
                except Exception:
                    pass

        ProduceCardsAuto.set_transition_observer(observe_exact_transition)
        if unified_legal_action_snapshot_provider is None:
            # Plan2 can build an exact unified candidate set directly from
            # the same typed ExamSave used by the transition recorder.  The
            # provider fails closed for flows whose native state pair is not
            # available, so enabling it by default cannot alter Maa input or
            # turn a partial candidate list into policy-ready data.
            from .unified_legal_action_snapshot import (
                UnifiedLegalActionSnapshotProvider,
            )

            unified_legal_action_snapshot_provider = (
                UnifiedLegalActionSnapshotProvider()
            )
        ProduceCardsAuto.set_unified_legal_action_snapshot_provider(
            unified_legal_action_snapshot_provider
        )
        entry = "GkmsToolMaaBaselineExam"
        pipeline = {
            entry: {
                "recognition": {
                    "type": "Custom",
                    "param": {"custom_recognition": recognizer_name},
                },
                "action": {
                    "type": "Custom",
                    "param": {"custom_action": action_name},
                },
                "next": [],
            }
        }
        if progress_reader is None:
            progress_reader = MaaBaselineExamProgressReader(exam_save_path)
        if not callable(progress_reader):
            raise TypeError("Maa baseline exam progress_reader must be callable")
        if foreground_recovery_callback is not None and not callable(
            foreground_recovery_callback
        ):
            raise TypeError(
                "Maa baseline exam foreground recovery callback must be callable"
            )

        started_at = monotonic()
        deadline = started_at + timeout_seconds
        last_names: tuple[str, ...] = ()
        last_progress: MaaBaselineExamProgressSignature | None = None
        idle_deadline: float | None = None
        foreground_recovery_deadline: float | None = None
        foreground_recovery_attempted = False
        foreground_recovery_count = 0
        saw_exam_save = False
        progress_trace: list[dict[str, Any]] = []
        progress_trace_last_key: tuple[Any, ...] | None = None
        progress_trace_last_signature: MaaBaselineExamProgressSignature | None = None
        progress_trace_truncated = False

        def append_progress_trace(
            current: MaaBaselineExamProgressSignature,
        ) -> None:
            """Record a bounded, typed state-change trace.

            The filesystem marker remains the authoritative heartbeat above,
            but it is intentionally not part of this trace: an atomic save
            can change mtime/size without changing the exam state.  This keeps
            the returned trace useful for comparing real scores and avoids
            filling it with duplicate rows while the game is idle.
            """

            nonlocal progress_trace_last_key
            nonlocal progress_trace_last_signature, progress_trace_truncated
            key = (
                current.current_turn,
                current.remain_turn,
                current.score,
                current.hand,
                current.turn_end,
            )
            # A metadata-only read during an atomic LocalSave replacement is
            # still useful to the timeout heartbeat, but is not a typed score
            # observation.  Keep the most recent known typed score in that
            # case rather than emitting an empty/duplicate trace row.
            if not any(
                value is not None
                for value in (
                    current.current_turn,
                    current.remain_turn,
                    current.score,
                    current.turn_end,
                )
            ) and not current.hand:
                return
            if key == progress_trace_last_key:
                return

            previous = progress_trace_last_signature
            if previous is None:
                changed = [
                    "current_turn",
                    "remain_turn",
                    "score",
                    "hand",
                    "turn_end",
                ]
            else:
                changed = [
                    name
                    for name, before, after in (
                        (
                            "current_turn",
                            previous.current_turn,
                            current.current_turn,
                        ),
                        (
                            "remain_turn",
                            previous.remain_turn,
                            current.remain_turn,
                        ),
                        ("score", previous.score, current.score),
                        ("hand", previous.hand, current.hand),
                        ("turn_end", previous.turn_end, current.turn_end),
                    )
                    if before != after
                ]
            entry = {
                "current_turn": current.current_turn,
                "remain_turn": current.remain_turn,
                "score": current.score,
                "hand": [list(card) for card in current.hand],
                "turn_end": current.turn_end,
                "changed": changed,
            }
            if len(progress_trace) >= MAA_BASELINE_EXAM_PROGRESS_TRACE_MAX_ENTRIES:
                # Retain the latest window.  The separate final_score field
                # remains authoritative even if a pathological run exceeds
                # the trace budget.
                progress_trace.pop(0)
                progress_trace_truncated = True
            progress_trace.append(entry)
            progress_trace_last_key = key
            progress_trace_last_signature = current

        def read_progress(now: float) -> None:
            """Refresh the durable heartbeat and its idle deadline."""

            nonlocal last_progress, idle_deadline
            nonlocal foreground_recovery_deadline, saw_exam_save
            try:
                current = progress_reader()
            except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
                current = None
            if current is None:
                return
            saw_exam_save = True
            if current != last_progress:
                last_progress = current
                append_progress_trace(current)
                idle_deadline = min(deadline, now + idle_timeout_seconds)
                if (
                    foreground_recovery_callback is not None
                    and not foreground_recovery_attempted
                ):
                    foreground_recovery_deadline = min(
                        deadline,
                        now + MAA_BASELINE_EXAM_FOREGROUND_RECOVERY_SECONDS,
                    )

        def stop_for_timeout(reason: str) -> None:
            """Stop the in-flight Maa task before exposing a typed timeout."""

            try:
                # A final non-blocking read may settle the last sidecar row;
                # it never changes the timeout or the completion action.
                ProduceCardsAuto.flush_transition_observer()
                ProduceCardsAuto.drop_pending_transition_boundaries(
                    "baseline-timeout"
                )
                ProduceCardsAuto.set_transition_observer(None)
                _wait_maa_job(
                    tasker.post_stop(),
                    timeout_seconds=MAA_TASK_STOP_ACK_TIMEOUT_SECONDS,
                    label="baseline exam stop",
                )
            finally:
                raise TimeoutError(reason)

        while monotonic() < deadline:
            now = monotonic()
            read_progress(now)
            if idle_deadline is not None and now >= idle_deadline:
                stop_for_timeout(
                    "Maa baseline exam made no ExamSaveData progress for "
                    f"{idle_timeout_seconds:g} seconds"
                )

            job = tasker.post_task(entry, pipeline)
            # A missing ExamSave at task entry is normal during the short
            # lesson->exam transition.  Give that one Maa task a bounded
            # readiness window, then stop/repost it.  Do not start the 90s
            # heartbeat until an actual ExamSave has been observed.
            readiness_deadline = min(
                deadline, monotonic() + readiness_retry_seconds
            )
            while not job.done:
                now = monotonic()
                if now >= deadline:
                    stop_for_timeout(
                        "Maa baseline exam reached its "
                        f"{timeout_seconds:g}-second total deadline"
                    )
                read_progress(now)
                if idle_deadline is not None and now >= idle_deadline:
                    stop_for_timeout(
                        "Maa baseline exam made no ExamSaveData progress for "
                        f"{idle_timeout_seconds:g} seconds"
                    )
                if (
                    foreground_recovery_callback is not None
                    and not foreground_recovery_attempted
                    and foreground_recovery_deadline is not None
                    and now >= foreground_recovery_deadline
                ):
                    # This is deliberately a single, optional foreground
                    # repair ticket.  The idle and total deadlines above stay
                    # authoritative; the callback cannot extend either one.
                    foreground_recovery_attempted = True
                    foreground_recovery_count += 1
                    foreground_recovery_callback()
                if not saw_exam_save and now >= readiness_deadline:
                    try:
                        _wait_maa_job(
                            tasker.post_stop(),
                            timeout_seconds=MAA_TASK_STOP_ACK_TIMEOUT_SECONDS,
                            label="baseline readiness stop",
                        )
                    except Exception:
                        # The next post_task will report the controller/tasker
                        # failure with its normal typed error.  Readiness is a
                        # retry boundary, not an exam completion signal.
                        pass
                    break
                remaining = max(0.0, min(deadline, readiness_deadline) - now)
                sleep(min(MAA_BASELINE_EXAM_POLL_SECONDS, remaining or 0.01))
            if not job.done:
                # Readiness retry; total deadline remains authoritative.
                continue
            if monotonic() >= deadline:
                # A Maa job can flip ``done`` between two polling samples.
                # Treat the outer deadline as a strict fail-closed boundary;
                # a late success must not turn into an unbounded wait.
                stop_for_timeout(
                    "Maa baseline exam reached its "
                    f"{timeout_seconds:g}-second total deadline"
                )
            completed_at = monotonic()
            read_progress(completed_at)
            if idle_deadline is not None and completed_at >= idle_deadline:
                stop_for_timeout(
                    "Maa baseline exam made no ExamSaveData progress for "
                    f"{idle_timeout_seconds:g} seconds"
                )
            detail = job.get()
            last_names = (
                () if detail is None else tuple(node.name for node in detail.nodes)
            )
            if job.succeeded:
                # The terminal ExamSave is often written just after Maa marks
                # its task done.  Resolve every distinct deferred card
                # boundary before taking the immutable result snapshot.
                ProduceCardsAuto.flush_transition_observer()
                # A successful Maa task may still have an action whose
                # after-state was never published.  Keep that loss explicit;
                # unresolved rows must not be silently discarded when the
                # observer is detached.
                ProduceCardsAuto.drop_pending_transition_boundaries(
                    "baseline-complete"
                )
                transition_drop_log = list(ProduceCardsAuto.transition_drop_log())
                result = {
                    "accepted": True,
                    "terminal": True,
                    "reason": "maa-baseline-exam-finished",
                    "visited_nodes": list(last_names),
                    "policy": "game-recommendation-or-available-card",
                    "simulator_required": False,
                    "foreground_recovery_attempted": foreground_recovery_attempted,
                    "foreground_recovery_count": foreground_recovery_count,
                    "foreground_recovery": {
                        "attempted": foreground_recovery_attempted,
                        "count": foreground_recovery_count,
                    },
                    "final_score": (
                        None
                        if progress_trace_last_signature is None
                        else progress_trace_last_signature.score
                    ),
                    "progress_trace": progress_trace,
                    "progress_trace_truncated": progress_trace_truncated,
                    "exact_transitions": list(exact_transitions),
                    "unified_exact_transition_count": sum(
                        isinstance(value.get("metadata"), Mapping)
                        and value["metadata"].get("candidate_set_kind") == "unified"
                        for value in exact_transitions
                        if isinstance(value, Mapping)
                    ),
                    "unified_legal_action_snapshot_provider_bound": (
                        ProduceCardsAuto.unified_legal_action_snapshot_provider()
                        is not None
                    ),
                    "dropped_exact_transitions": transition_drop_log,
                    "dropped_exact_transition_count": len(transition_drop_log),
                    # Keep the shorter spelling for callers that consume the
                    # generic transition sidecar rather than the exact-row
                    # extractor.
                    "transition_drops": transition_drop_log,
                }
                ProduceCardsAuto.set_transition_observer(None)
                return result
            # The same upstream health-marker gate owns readiness.  Repeating
            # the task while the exam enters does not submit a card because the
            # custom action is unreachable until recognition succeeds.
            if monotonic() >= deadline:
                stop_for_timeout(
                    "Maa baseline exam reached its "
                    f"{timeout_seconds:g}-second total deadline"
                )
            sleep(min(0.5, max(0.01, deadline - monotonic())))
        stop_for_timeout(
            "Maa baseline exam reached its "
            f"{timeout_seconds:g}-second total deadline"
        )

    def advance_nia_post_live(
        self,
        *,
        timeout_seconds: float = 300.0,
    ) -> MaaNiaPostLiveResult:
        """Run only MaaGakumasu's fixed post-live ProduceEnd node graph."""

        if not 10.0 <= float(timeout_seconds) <= 300.0:
            raise ValueError("N.I.A. post-live timeout must be between 10 and 300 seconds")
        resource, tasker = self._ensure_read_only_recognizer()
        recognizer_name = "ProduceShowStart"
        if recognizer_name not in tuple(resource.custom_recognition_list):
            recognizer = _nia_show_start_recognizer_type()()
            if not resource.register_custom_recognition(recognizer_name, recognizer):
                raise RuntimeError(
                    "MaaFramework failed to register ProduceShowStart"
                )
        job = tasker.post_task(
            "GkmsToolNiaPostLive",
            _nia_post_live_pipeline_override(),
        )
        deadline = time.monotonic() + float(timeout_seconds)
        terminal_names = (
            "ProduceHomeFlag",
            "ProduceBackHome",
            "ProduceChooseScenario",
        )
        while not job.done and time.monotonic() < deadline:
            time.sleep(0.1)
        if not job.done:
            terminal_receipt_observed = _stop_maa_task_and_recheck_terminal(
                self,
                tasker,
                job,
                terminal_names,
            )
            if terminal_receipt_observed is None:
                raise TimeoutError(
                    "Maa N.I.A. post-live route did not reach a terminal in time"
                )
        detail = job.get() if job.done else None
        names, _receipt_source = _maa_task_names(self, job, detail)
        if not job.succeeded and not any(name in names for name in terminal_names):
            raise RuntimeError(
                "Maa N.I.A. post-live route failed"
                + ("" if not names else f" after {names[-1]}")
            )
        terminal_name = next(
            (
                name
                for name in names
                if name in terminal_names
            ),
            None,
        )
        completed = terminal_name is not None
        if not completed:
            raise RuntimeError(
                "Maa N.I.A. post-live route stopped before a terminal"
                + ("" if not names else f" after {names[-1]}")
            )
        return MaaNiaPostLiveResult(
            completed=True,
            reason=(
                "produce-mode-selection-reached"
                if terminal_name == "ProduceChooseScenario"
                else "produce-home-reached"
            ),
            visited_nodes=names,
            maa_task_id=_maa_job_id(job),
            receipt_source=_receipt_source,
        )

    def click(self, maa_x: int, maa_y: int) -> dict[str, int | str]:
        job = _wait_maa_job(
            self._controller.post_click(maa_x, maa_y),
            timeout_seconds=MAA_INPUT_JOB_TIMEOUT_SECONDS,
            label="background click",
        )
        if not job.succeeded:
            raise RuntimeError("MaaFramework 背景點擊失敗")
        return {
            "method": "MaaFramework.SendMessage",
            "maa_x": maa_x,
            "maa_y": maa_y,
        }

    def scroll(self, delta_x: int, delta_y: int) -> dict[str, int | str]:
        """Send one bounded background mouse-wheel operation."""

        if not -2048 <= delta_x <= 2048 or not -2048 <= delta_y <= 2048:
            raise ValueError("MaaFramework 滾動量必須介於 -2048 與 2048")
        if delta_x == 0 and delta_y == 0:
            raise ValueError("MaaFramework 滾動量不可同時為 0")
        job = _wait_maa_job(
            self._controller.post_scroll(delta_x, delta_y),
            timeout_seconds=MAA_INPUT_JOB_TIMEOUT_SECONDS,
            label="background scroll",
        )
        if not job.succeeded:
            raise RuntimeError("MaaFramework 背景滾動失敗")
        return {
            "method": "MaaFramework.SendMessage.scroll",
            "delta_x": delta_x,
            "delta_y": delta_y,
        }

    def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: int = 450,
    ) -> dict[str, int | str]:
        """Send one bounded background drag in Maa's 720x1280 space."""

        width, height = self.coordinate_size
        if not all((0 <= x1 < width, 0 <= x2 < width, 0 <= y1 < height, 0 <= y2 < height)):
            raise ValueError("MaaFramework 滑動座標超出擷取畫面")
        if not 100 <= duration_ms <= 2000:
            raise ValueError("MaaFramework 滑動時間必須介於 100 與 2000 毫秒")
        job = _wait_maa_job(
            self._controller.post_swipe(x1, y1, x2, y2, duration_ms),
            timeout_seconds=MAA_INPUT_JOB_TIMEOUT_SECONDS,
            label="background swipe",
        )
        if not job.succeeded:
            raise RuntimeError("MaaFramework 背景滑動失敗")
        return {
            "method": "MaaFramework.SendMessage.swipe",
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "duration_ms": duration_ms,
        }

    @staticmethod
    def save_bgr_png(image: np.ndarray, path: Path) -> None:
        """Save MaaFramework's OpenCV-style BGR/BGRA array as a PNG."""

        path.parent.mkdir(parents=True, exist_ok=True)
        if image.shape[2] == 4:
            converted = image[:, :, [2, 1, 0, 3]]
            output = Image.fromarray(converted, mode="RGBA")
        else:
            converted = image[:, :, ::-1]
            output = Image.fromarray(converted, mode="RGB")
        output.save(path)
