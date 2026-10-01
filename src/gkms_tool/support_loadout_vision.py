"""Offline, safety-gated recognition of the six support cards at produce start.

The recognizer is deliberately independent from live capture and runtime code.
It accepts a saved screenshot, reads the installed game's Octo cache, and
returns the durable snapshot models without applying them anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Mapping

import numpy as np
import yaml
from PIL import Image

from .loadout_snapshot import (
    DEFAULT_AUTHORITATIVE_CONFIDENCE,
    RecognitionEvidence,
    SupportLoadoutSlot,
)
from .octo_assets import DEFAULT_OCTO_ROOT, OctoAssetIndex
from .text_recognizer import PaddleLineRecognizer, TextRecognition


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUPPORT_CARD_MASTER = PROJECT_ROOT / "_research" / "gakumasu-diff" / "SupportCard.yaml"
DEFAULT_SUPPORT_ART_CACHE = PROJECT_ROOT / "var" / "support_loadout_vision"
CANONICAL_SIZE = (720, 1280)
IDENTITY_THRESHOLD = 0.72
IDENTITY_MARGIN = 0.055


@dataclass(frozen=True, slots=True)
class SupportCardCandidate:
    card_id: str
    asset_id: str
    asset_name: str


@dataclass(frozen=True, slots=True)
class SupportCardMatch:
    card_id: str | None
    score: float
    runner_up_score: float

    @property
    def margin(self) -> float:
        return self.score - self.runner_up_score


@dataclass(frozen=True, slots=True)
class SupportLoadoutRecognition:
    slots: tuple[SupportLoadoutSlot, ...]
    page: str | None
    page_confidence: float


@dataclass(frozen=True, slots=True)
class SupportLoadoutReconciliation:
    """One six-slot result proven by semantically independent observations."""

    slots: tuple[SupportLoadoutSlot, ...]
    authoritative: bool
    blocking_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SupportDetailEvidence:
    """Independent evidence from one opened support-card detail page.

    ``card_id`` and ``level`` are observations from the full detail page.  They
    never replace the notebook values: :func:`reconcile_support_notebook_details`
    only uses them to confirm the same slot/card/level tuple.  Page context,
    artwork identity, and level text have separate confidence gates so a bare
    caller-supplied card id can never become authoritative by itself.
    """

    slot: int
    card_id: str | None
    level: int | None
    maximum_level: int | None
    image_path: Path
    page_context_verified: bool
    page_confidence: float
    identity_confidence: float
    identity_margin: float
    level_confidence: float
    level_observed_text: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.slot, bool)
            or not isinstance(self.slot, int)
            or not 1 <= self.slot <= 6
        ):
            raise ValueError("support detail slot must be 1..6")
        if self.card_id is not None and (
            not isinstance(self.card_id, str) or not self.card_id.strip()
        ):
            raise ValueError("support detail card_id must be non-empty text or None")
        for label, value in (
            ("level", self.level),
            ("maximum_level", self.maximum_level),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(
                    f"support detail {label} must be an integer >= 1 or None"
                )
        if (
            self.level is not None
            and self.maximum_level is not None
            and self.level > self.maximum_level
        ):
            raise ValueError("support detail level cannot exceed maximum_level")
        if not isinstance(self.image_path, Path):
            raise TypeError("support detail image_path must be a Path")
        if not isinstance(self.page_context_verified, bool):
            raise TypeError("support detail page_context_verified must be a boolean")
        for label, value in (
            ("page_confidence", self.page_confidence),
            ("identity_confidence", self.identity_confidence),
            ("level_confidence", self.level_confidence),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise ValueError(f"support detail {label} must be between 0 and 1")
        if (
            isinstance(self.identity_margin, bool)
            or not isinstance(self.identity_margin, (int, float))
            or not -2.0 <= float(self.identity_margin) <= 2.0
        ):
            raise ValueError("support detail identity_margin must be between -2 and 2")
        if not isinstance(self.level_observed_text, str):
            raise TypeError("support detail level_observed_text must be text")


def support_card_candidates(master_path: Path = DEFAULT_SUPPORT_CARD_MASTER) -> tuple[SupportCardCandidate, ...]:
    """Build candidates from every ``SupportCard.yaml`` row, never from a screenshot."""
    raw = yaml.safe_load(master_path.resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("SupportCard.yaml must contain a list")
    candidates = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        card_id, asset_id = row.get("id"), row.get("assetId")
        if isinstance(card_id, str) and isinstance(asset_id, str) and card_id and asset_id:
            candidates.append(SupportCardCandidate(card_id, asset_id, f"img_general_{asset_id}_thumb-landscape"))
    if not candidates:
        raise ValueError("SupportCard.yaml contains no usable support cards")
    return tuple(candidates)


def _signature(image: Image.Image, size: int = 24) -> np.ndarray:
    """Colour-and-shape signature, intentionally excluding UI text/frames."""
    rgb = np.asarray(image.convert("RGB").resize((size, size), Image.Resampling.LANCZOS), dtype=np.float32) / 255.0
    # Chroma makes this tolerant to the game's dark tile shading.
    chroma = rgb - rgb.mean(axis=2, keepdims=True)
    dark = np.maximum(0.0, 0.80 - rgb.mean(axis=2, keepdims=True)) * 0.18
    vector = np.concatenate((chroma, dark), axis=2).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def _candidate_art_crop(image: Image.Image) -> Image.Image:
    """Use the same relative artwork window as the screenshot safe crop."""
    width, height = image.size
    return image.crop((round(width * .18), round(height * .04), round(width * .85), round(height * .60)))


class PreparedSupportCardMatcher:
    """Precomputed signatures for support-card thumbnail artwork."""

    def __init__(self, candidates: tuple[SupportCardCandidate, ...], signatures: np.ndarray) -> None:
        self.candidates = candidates
        self.signatures = signatures

    @classmethod
    def build(cls, candidates: tuple[SupportCardCandidate, ...], art: Mapping[str, Path | Image.Image]) -> "PreparedSupportCardMatcher":
        selected: list[SupportCardCandidate] = []
        signatures: list[np.ndarray] = []
        for candidate in candidates:
            source = art.get(candidate.asset_name)
            if source is None:
                continue
            if isinstance(source, Image.Image):
                image, close = source, False
            else:
                image, close = Image.open(source), True
            try:
                signature = _signature(_candidate_art_crop(image))
            finally:
                if close:
                    image.close()
            if float(np.linalg.norm(signature)) > 0:
                selected.append(candidate)
                signatures.append(signature)
        if not selected:
            raise ValueError("no support thumbnail artwork was available")
        return cls(tuple(selected), np.ascontiguousarray(np.stack(signatures), dtype=np.float32))

    @classmethod
    def from_octo(
        cls,
        *,
        master_path: Path = DEFAULT_SUPPORT_CARD_MASTER,
        octo_root: Path = DEFAULT_OCTO_ROOT,
        cache_dir: Path = DEFAULT_SUPPORT_ART_CACHE,
    ) -> "PreparedSupportCardMatcher":
        candidates = support_card_candidates(master_path)
        cache_dir.resolve().mkdir(parents=True, exist_ok=True)
        art: dict[str, Path] = {}
        missing = []
        for candidate in candidates:
            output = cache_dir.resolve() / f"{candidate.asset_name}.png"
            if output.is_file():
                art[candidate.asset_name] = output
            else:
                missing.append(candidate)
        # A populated project cache is a reproducible local source and does
        # not depend on the currently installed Octo bundle.  Only consult
        # Octo for genuinely absent entries.
        if missing:
            index = OctoAssetIndex.load(octo_root)
            for candidate in missing:
                if candidate.asset_name not in index.by_name:
                    continue
                output = cache_dir.resolve() / f"{candidate.asset_name}.png"
                try:
                    index.extract_texture(candidate.asset_name).save(output)
                except FileNotFoundError:
                    # Keep available cached artwork usable; callers still
                    # fail closed on an ambiguous/unmatched visible tile.
                    continue
                art[candidate.asset_name] = output
        return cls.build(candidates, art)

    def match(self, image: Image.Image) -> SupportCardMatch:
        vector = _signature(image)
        scores = self.signatures @ vector
        order = np.argsort(scores)[::-1]
        best = int(order[0])
        runner_up = float(scores[int(order[1])]) if len(order) > 1 else 0.0
        return SupportCardMatch(self.candidates[best].card_id, float(scores[best]), runner_up)


# (x, y, width, height) in canonical 720x1280 coordinates.  The inner crop
# only covers artwork: it avoids the lower-left Lv text, type badge, stars,
# and selection/borrow overlays.
_SELECTION_CARDS = ((25, 710, 215, 117), (253, 710, 215, 117), (480, 710, 215, 117), (25, 843, 215, 120), (253, 843, 215, 120), (480, 843, 215, 120))
_CONFIRM_CARDS = ((63, 575, 168, 92), (235, 575, 168, 92), (63, 673, 168, 92), (235, 673, 168, 92), (63, 771, 168, 92), (235, 771, 168, 92))
# P notebook → 編成中支持卡/回憶.  This is intentionally a distinct
# geometry; it must never be inferred through selection/confirmation slots.
_NOTEBOOK_CARDS = (
    (59, 627, 191, 107), (262, 627, 191, 107),
    (59, 748, 191, 107), (262, 748, 191, 107),
    (59, 869, 191, 107), (262, 869, 191, 107),
)
# Full support-card detail page.  The top panel uses the same landscape art as
# the local support thumbnail asset.  Cropping that panel through
# ``_candidate_art_crop`` deliberately excludes the speech text, rarity, level,
# and action buttons before matching.
_DETAIL_CARD_PANEL = (0, 0, 720, 405)
_DETAIL_LEVEL_BOX = (85, 320, 335, 405)
_DETAIL_LEVEL_PATTERN = re.compile(
    r"L\s*V?\s*([1-9]\d?)\s*/\s*([1-9]\d?)", re.IGNORECASE
)


def _scale_rect(rect: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    sx, sy = size[0] / CANONICAL_SIZE[0], size[1] / CANONICAL_SIZE[1]
    x, y, width, height = rect
    return round(x * sx), round(y * sy), max(1, round(width * sx)), max(1, round(height * sy))


def _scale_box(
    box: tuple[int, int, int, int], size: tuple[int, int]
) -> tuple[int, int, int, int]:
    sx, sy = size[0] / CANONICAL_SIZE[0], size[1] / CANONICAL_SIZE[1]
    left, top, right, bottom = box
    return (
        round(left * sx),
        round(top * sy),
        round(right * sx),
        round(bottom * sy),
    )


def _art_rect(rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x, y, width, height = rect
    return x + round(width * .18), y + round(height * .04), round(width * .67), round(height * .56)


def _selection_level_rect(
    rect: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    x, y, _width, _height = rect
    return x + 11, y + 66, 58, 38


def _level_candidate(result: TextRecognition) -> tuple[int, float] | None:
    normalized = result.text.translate(
        str.maketrans({"o": "0", "O": "0", "〇": "0"})
    )
    matches = re.findall(r"(?<!\d)(?:[1-5]?\d|60)(?!\d)", normalized)
    values = [int(value) for value in matches if 1 <= int(value) <= 60]
    if len(set(values)) != 1:
        return None
    return values[0], float(result.confidence)


def _recognize_selection_level(
    image: Image.Image,
    rect: tuple[int, int, int, int],
    recognizer: PaddleLineRecognizer,
) -> tuple[int | None, float, str]:
    from PIL import ImageOps

    x, y, width, height = _scale_rect(_selection_level_rect(rect), image.size)
    crop = image.crop((x, y, x + width, y + height)).resize(
        (width * 2, height * 2), Image.Resampling.LANCZOS
    )
    grayscale = ImageOps.autocontrast(crop.convert("L"))
    results = (
        recognizer.recognize(crop),
        recognizer.recognize(grayscale),
        recognizer.recognize(ImageOps.invert(grayscale)),
    )
    candidates = [
        (candidate, result.text)
        for result in results
        if (candidate := _level_candidate(result)) is not None
    ]
    if not candidates:
        return None, 0.0, ";".join(result.text for result in results)
    candidates.sort(key=lambda item: item[0][1], reverse=True)
    (value, confidence), _text = candidates[0]
    competing = {
        other_value
        for (other_value, other_confidence), _ in candidates[1:]
        if other_confidence >= confidence - 0.08
    }
    if competing - {value}:
        return None, confidence, ";".join(result.text for result in results)
    return value, confidence, ";".join(result.text for result in results)


def _detail_level_candidate(
    result: TextRecognition,
) -> tuple[int, int, float] | None:
    matches = _DETAIL_LEVEL_PATTERN.findall(result.text)
    pairs = {
        (int(current), int(maximum))
        for current, maximum in matches
        if 1 <= int(current) <= int(maximum) <= 60
    }
    if len(pairs) != 1:
        return None
    current, maximum = next(iter(pairs))
    return current, maximum, float(result.confidence)


def _recognize_support_detail_level(
    image: Image.Image,
    recognizer: PaddleLineRecognizer,
) -> tuple[int | None, int | None, float, str, tuple[int, int, int, int]]:
    """Read the dedicated ``Lv current/max`` label without losing the cap."""

    from PIL import ImageOps

    left, top, right, bottom = _scale_box(_DETAIL_LEVEL_BOX, image.size)
    crop = image.crop((left, top, right, bottom)).resize(
        ((right - left) * 2, (bottom - top) * 2), Image.Resampling.LANCZOS
    )
    grayscale = ImageOps.autocontrast(crop.convert("L"))
    results = (
        recognizer.recognize(crop),
        recognizer.recognize(grayscale),
        recognizer.recognize(ImageOps.invert(grayscale)),
    )
    candidates = [
        (candidate, result.text)
        for result in results
        if (candidate := _detail_level_candidate(result)) is not None
    ]
    observed_text = ";".join(result.text for result in results)
    region = (left, top, right - left, bottom - top)
    if not candidates:
        return None, None, 0.0, observed_text, region
    candidates.sort(key=lambda item: item[0][2], reverse=True)
    (current, maximum, confidence), _text = candidates[0]
    competing = {
        (other_current, other_maximum)
        for (other_current, other_maximum, other_confidence), _ in candidates[1:]
        if other_confidence >= confidence - 0.08
    }
    if competing - {(current, maximum)}:
        return None, None, confidence, observed_text, region
    return current, maximum, confidence, observed_text, region


def _detect_page(image: Image.Image) -> tuple[str | None, float, tuple[tuple[int, int, int, int], ...]]:
    # Both layouts have six image tiles; their positions are far enough apart
    # that a simple brightness test is a conservative page-semantic gate.
    values = []
    for name, rects in (
        ("selection", _SELECTION_CARDS),
        ("confirmation", _CONFIRM_CARDS),
    ):
        samples = []
        for rect in rects:
            x, y, width, height = _scale_rect(_art_rect(rect), image.size)
            samples.append(np.asarray(image.crop((x, y, x + width, y + height)).convert("L"), dtype=np.float32).std())
        values.append((float(np.median(samples)), name, rects))
    values.sort(reverse=True)
    score, name, rects = values[0]
    other = values[1][0]
    confidence = min(1.0, max(0.0, (score - other + 15.0) / 45.0))
    return (name if confidence >= .80 else None), confidence, rects


def recognize_support_loadout(
    image_path: Path,
    matcher: PreparedSupportCardMatcher,
    *,
    page_hint: str | None = None,
    page_hint_authoritative: bool = False,
    recognizer: PaddleLineRecognizer | None = None,
    read_levels: bool = True,
) -> SupportLoadoutRecognition:
    """Recognize an offline selection/confirmation screenshot without guessing.

    Levels are read only from the selection layout and pass through a separate
    confidence/ambiguity gate.  The confirmation layout intentionally leaves
    them ``None`` because it does not expose a calibrated level label.
    """
    path = image_path.resolve()
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    detected_page, detected_confidence, detected_rects = _detect_page(image)
    if not isinstance(page_hint_authoritative, bool):
        raise TypeError("page_hint_authoritative must be a boolean")
    if not isinstance(read_levels, bool):
        raise TypeError("read_levels must be a boolean")
    if page_hint is not None:
        if page_hint not in {"selection", "confirmation", "notebook"}:
            raise ValueError("page_hint must be selection, confirmation, notebook, or None")
        rects = {
            "selection": _SELECTION_CARDS,
            "confirmation": _CONFIRM_CARDS,
            "notebook": _NOTEBOOK_CARDS,
        }[page_hint]
        # An ordinary hint chooses geometry but never bypasses the screenshot
        # check.  A separate explicit flag is reserved for callers that have
        # already verified the page through a stronger semantic state reader.
        if page_hint_authoritative:
            page = page_hint
            page_confidence = 1.0
        elif page_hint == "notebook":
            # The notebook's surrounding UI is not interchangeable with the
            # selection/confirmation pages.  Its geometry alone is not a
            # semantic proof, so require the caller's independent page check.
            page = None
            page_confidence = 0.0
        else:
            page = page_hint if detected_page == page_hint else None
            page_confidence = (
                detected_confidence if detected_page == page_hint else 0.0
            )
    else:
        page, page_confidence, rects = (
            detected_page,
            detected_confidence,
            detected_rects,
        )
    slots: list[SupportLoadoutSlot] = []
    level_reader = (
        recognizer or PaddleLineRecognizer()
        if read_levels and page in {"selection", "notebook"}
        else None
    )
    for number, rect in enumerate(rects, start=1):
        region = _scale_rect(_art_rect(rect), image.size)
        x, y, width, height = region
        match = matcher.match(image.crop((x, y, x + width, y + height)))
        valid = page is not None and match.score >= IDENTITY_THRESHOLD and match.margin >= IDENTITY_MARGIN
        origin = "borrowed" if number == 6 else "owned"
        if level_reader is None:
            level = None
            level_confidence = 0.0
            level_text = "not-read"
        else:
            level, level_confidence, level_text = _recognize_selection_level(
                image, rect, level_reader
            )
        level_valid = (
            page is not None
            and level is not None
            and level_confidence >= 0.80
        )
        level_region = _scale_rect(_selection_level_rect(rect), image.size)
        slots.append(SupportLoadoutSlot(
            slot=number, origin=origin, card_id=match.card_id if valid else None, level=level if level_valid else None,
            origin_evidence=RecognitionEvidence("offline-support-layout", page_confidence, page is not None, observed_text=origin, image_path=str(path), region=region),
            identity_evidence=RecognitionEvidence("offline-support-thumbnail", max(0.0, min(1.0, match.score)), valid, observed_text=f"candidate={match.card_id};score={match.score:.3f};margin={match.margin:.3f}", image_path=str(path), region=region),
            level_evidence=RecognitionEvidence("offline-support-level-ocr", max(0.0, min(1.0, level_confidence)), level_valid, observed_text=level_text, image_path=str(path), region=level_region),
        ))
    return SupportLoadoutRecognition(tuple(slots), page, page_confidence)


def recognize_support_detail(
    image_path: Path,
    matcher: PreparedSupportCardMatcher,
    *,
    slot: int,
    page_context_verified: bool,
    page_confidence: float,
    recognizer: PaddleLineRecognizer | None = None,
) -> SupportDetailEvidence:
    """Recognize one saved full support-card detail page, without applying it.

    Page semantics are intentionally not inferred from the artwork crop.  The
    caller must explicitly provide the result and confidence of an independent
    detail-page context check.  Artwork and ``Lv current/max`` are then read
    from disjoint regions and retain separate confidences for reconciliation.
    """

    if (
        isinstance(slot, bool)
        or not isinstance(slot, int)
        or not 1 <= slot <= 6
    ):
        raise ValueError("support detail slot must be 1..6")
    if not isinstance(page_context_verified, bool):
        raise TypeError("page_context_verified must be a boolean")
    if (
        isinstance(page_confidence, bool)
        or not isinstance(page_confidence, (int, float))
        or not 0.0 <= float(page_confidence) <= 1.0
    ):
        raise ValueError("page_confidence must be between 0 and 1")
    if not isinstance(matcher, PreparedSupportCardMatcher):
        raise TypeError("matcher must be PreparedSupportCardMatcher")

    path = Path(image_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"support detail evidence not found: {path}")
    with Image.open(path) as opened:
        image = opened.convert("RGB")

    panel_x, panel_y, panel_width, panel_height = _scale_rect(
        _DETAIL_CARD_PANEL, image.size
    )
    panel = image.crop(
        (
            panel_x,
            panel_y,
            panel_x + panel_width,
            panel_y + panel_height,
        )
    )
    match = matcher.match(_candidate_art_crop(panel))
    identity_confidence = max(0.0, min(1.0, float(match.score)))
    identity_margin = float(match.margin)
    card_id = (
        match.card_id
        if identity_confidence >= DEFAULT_AUTHORITATIVE_CONFIDENCE
        and identity_margin >= IDENTITY_MARGIN
        else None
    )
    level, maximum_level, level_confidence, level_text, _level_region = (
        _recognize_support_detail_level(
            image, recognizer or PaddleLineRecognizer()
        )
    )
    return SupportDetailEvidence(
        slot=slot,
        card_id=card_id,
        level=level,
        maximum_level=maximum_level,
        image_path=path,
        page_context_verified=page_context_verified,
        page_confidence=float(page_confidence),
        identity_confidence=identity_confidence,
        identity_margin=identity_margin,
        level_confidence=max(0.0, min(1.0, float(level_confidence))),
        level_observed_text=level_text,
    )


def reconcile_support_notebook_details(
    notebook: SupportLoadoutRecognition,
    details: Iterable[SupportDetailEvidence],
    *,
    confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
) -> SupportLoadoutReconciliation:
    """Confirm one notebook loadout with six independent detail screenshots.

    This is an all-or-nothing gate.  Every notebook slot and every detail page
    must be authoritative and must agree on ``slot/card_id/level``.  Detail
    pages confirm notebook values but never overwrite them; notably the owned
    versus borrowed origin remains exclusively notebook evidence.  Any global
    or per-slot discrepancy clears card identity and level from all six output
    slots so no partial support loadout can reach shadow state.
    """

    if not isinstance(notebook, SupportLoadoutRecognition):
        raise TypeError("notebook must be SupportLoadoutRecognition")
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or not 0.0 <= float(confidence_threshold) <= 1.0
    ):
        raise ValueError("confidence_threshold must be between 0 and 1")
    threshold = float(confidence_threshold)
    observed_details = tuple(details)
    if not all(
        isinstance(detail, SupportDetailEvidence) for detail in observed_details
    ):
        raise TypeError("details must contain SupportDetailEvidence values")
    if len(notebook.slots) != 6:
        raise ValueError("notebook reconciliation requires exactly six slots")
    notebook_by_slot = {slot.slot: slot for slot in notebook.slots}
    if set(notebook_by_slot) != set(range(1, 7)) or len(notebook_by_slot) != 6:
        raise ValueError("notebook reconciliation requires unique slots 1..6")

    reasons: list[str] = []
    if notebook.page != "notebook":
        reasons.append("support-notebook-page-not-verified")
    if notebook.page_confidence < threshold:
        reasons.append(
            f"support-notebook-page-low-confidence:{notebook.page_confidence:.6g}"
        )
    if len(observed_details) != 6:
        reasons.append(f"support-detail-count-not-six:{len(observed_details)}")

    details_by_slot: dict[int, list[SupportDetailEvidence]] = {}
    for detail in observed_details:
        details_by_slot.setdefault(detail.slot, []).append(detail)
    for slot in range(1, 7):
        count = len(details_by_slot.get(slot, ()))
        if count == 0:
            reasons.append(f"support-detail-slot-missing:{slot}")
        elif count > 1:
            reasons.append(f"support-detail-slot-duplicate:{slot}")

    detail_paths: dict[Path, list[int]] = {}
    for detail in observed_details:
        detail_paths.setdefault(detail.image_path.resolve(), []).append(detail.slot)
    for slots in detail_paths.values():
        if len(slots) > 1:
            reasons.append(
                "support-detail-screenshot-reused:" + ",".join(map(str, sorted(slots)))
            )

    notebook_paths = {
        Path(path).resolve()
        for slot in notebook.slots
        for path in (
            slot.origin_evidence.image_path,
            slot.identity_evidence.image_path,
            slot.level_evidence.image_path,
        )
        if path
    }
    for slot_number in range(1, 7):
        notebook_slot = notebook_by_slot[slot_number]
        for label, evidence in (
            ("origin", notebook_slot.origin_evidence),
            ("identity", notebook_slot.identity_evidence),
            ("level", notebook_slot.level_evidence),
        ):
            if not evidence.authoritative:
                reasons.append(
                    f"support-notebook-{label}-not-authoritative:{slot_number}"
                )
            if evidence.confidence < threshold:
                reasons.append(
                    f"support-notebook-{label}-low-confidence:{slot_number}:"
                    f"{evidence.confidence:.6g}"
                )
        if notebook_slot.card_id is None:
            reasons.append(f"support-notebook-card-missing:{slot_number}")
        if notebook_slot.level is None:
            reasons.append(f"support-notebook-level-missing:{slot_number}")

        candidates = details_by_slot.get(slot_number, ())
        if len(candidates) != 1:
            continue
        detail = candidates[0]
        detail_path = detail.image_path.resolve()
        if not detail_path.is_file():
            reasons.append(f"support-detail-screenshot-missing:{slot_number}")
        else:
            try:
                with Image.open(detail_path) as opened:
                    opened.verify()
            except (OSError, ValueError):
                reasons.append(f"support-detail-screenshot-invalid:{slot_number}")
        if detail_path in notebook_paths:
            reasons.append(f"support-detail-not-independent:{slot_number}")
        if not detail.page_context_verified:
            reasons.append(f"support-detail-page-not-verified:{slot_number}")
        if detail.page_confidence < threshold:
            reasons.append(
                f"support-detail-page-low-confidence:{slot_number}:"
                f"{detail.page_confidence:.6g}"
            )
        if detail.card_id is None:
            reasons.append(f"support-detail-card-missing:{slot_number}")
        if detail.identity_confidence < threshold:
            reasons.append(
                f"support-detail-identity-low-confidence:{slot_number}:"
                f"{detail.identity_confidence:.6g}"
            )
        if detail.identity_margin < IDENTITY_MARGIN:
            reasons.append(
                f"support-detail-identity-ambiguous:{slot_number}:"
                f"{detail.identity_margin:.6g}"
            )
        if detail.level is None or detail.maximum_level is None:
            reasons.append(f"support-detail-level-missing:{slot_number}")
        if detail.level_confidence < threshold:
            reasons.append(
                f"support-detail-level-low-confidence:{slot_number}:"
                f"{detail.level_confidence:.6g}"
            )
        if detail.card_id != notebook_slot.card_id:
            reasons.append(f"support-detail-card-mismatch:{slot_number}")
        if detail.level != notebook_slot.level:
            reasons.append(f"support-detail-level-mismatch:{slot_number}")

    unique_reasons = tuple(dict.fromkeys(reasons))
    accepted = not unique_reasons
    reconciled: list[SupportLoadoutSlot] = []
    for slot_number in range(1, 7):
        notebook_slot = notebook_by_slot[slot_number]
        candidates = details_by_slot.get(slot_number, ())
        detail = candidates[0] if len(candidates) == 1 else None
        detail_path = str(detail.image_path.resolve()) if detail is not None else None
        identity_confidence = min(
            notebook.page_confidence,
            notebook_slot.identity_evidence.confidence,
            detail.page_confidence if detail is not None else 0.0,
            detail.identity_confidence if detail is not None else 0.0,
        )
        level_confidence = min(
            notebook.page_confidence,
            notebook_slot.level_evidence.confidence,
            detail.page_confidence if detail is not None else 0.0,
            detail.level_confidence if detail is not None else 0.0,
        )
        reconciled.append(
            SupportLoadoutSlot(
                slot=notebook_slot.slot,
                origin=notebook_slot.origin,
                card_id=notebook_slot.card_id if accepted else None,
                level=notebook_slot.level if accepted else None,
                # Detail pages do not encode owned/borrowed status.
                origin_evidence=notebook_slot.origin_evidence,
                identity_evidence=RecognitionEvidence(
                    source="support-notebook-detail-identity",
                    confidence=identity_confidence,
                    authoritative=accepted,
                    observed_text=(
                        f"notebook={notebook_slot.card_id};"
                        f"detail={detail.card_id if detail is not None else None}"
                    ),
                    image_path=detail_path,
                ),
                level_evidence=RecognitionEvidence(
                    source="support-notebook-detail-level",
                    confidence=level_confidence,
                    authoritative=accepted,
                    observed_text=(
                        f"notebook={notebook_slot.level};"
                        f"detail={detail.level if detail is not None else None}/"
                        f"{detail.maximum_level if detail is not None else None};"
                        f"ocr={detail.level_observed_text if detail is not None else ''}"
                    ),
                    image_path=detail_path,
                ),
            )
        )
    return SupportLoadoutReconciliation(
        slots=tuple(reconciled),
        authoritative=accepted,
        blocking_reasons=unique_reasons,
    )


def reconcile_support_loadout(
    selection: SupportLoadoutRecognition,
    confirmation: SupportLoadoutRecognition,
) -> SupportLoadoutReconciliation:
    """Require the same six identities on two semantically distinct pages.

    The selection page remains the sole source of card levels.  Identity is
    authoritative only when both pages independently name the same card in the
    same slot.  A disagreement clears that slot instead of choosing either
    observation.
    """

    if not isinstance(selection, SupportLoadoutRecognition) or not isinstance(
        confirmation, SupportLoadoutRecognition
    ):
        raise TypeError("selection and confirmation must be loadout recognitions")
    reasons: list[str] = []
    if selection.page != "selection":
        reasons.append("support-selection-page-not-verified")
    if confirmation.page != "confirmation":
        reasons.append("support-confirmation-page-not-verified")
    if len(selection.slots) != 6 or len(confirmation.slots) != 6:
        raise ValueError("support reconciliation requires exactly six slots per page")

    reconciled: list[SupportLoadoutSlot] = []
    for selected, confirmed in zip(
        selection.slots, confirmation.slots, strict=True
    ):
        if selected.slot != confirmed.slot or selected.origin != confirmed.origin:
            raise ValueError("support slot order/origin differs between pages")
        identity_confidence = min(
            selected.identity_evidence.confidence,
            confirmed.identity_evidence.confidence,
            selection.page_confidence,
            confirmation.page_confidence,
        )
        identity_matches = bool(
            selection.page == "selection"
            and confirmation.page == "confirmation"
            and selected.card_id
            and selected.card_id == confirmed.card_id
            and selected.identity_evidence.authoritative
            and confirmed.identity_evidence.authoritative
            and identity_confidence >= DEFAULT_AUTHORITATIVE_CONFIDENCE
        )
        level_valid = bool(
            selected.level is not None
            and selected.level_evidence.authoritative
        )
        if not identity_matches:
            reasons.append(f"support-identity-not-confirmed:{selected.slot}")
        if not level_valid:
            reasons.append(f"support-level-not-verified:{selected.slot}")

        selection_path = selected.identity_evidence.image_path or "unknown"
        confirmation_path = confirmed.identity_evidence.image_path or "unknown"
        reconciled.append(
            SupportLoadoutSlot(
                slot=selected.slot,
                origin=selected.origin,
                card_id=selected.card_id if identity_matches else None,
                level=selected.level if level_valid else None,
                origin_evidence=RecognitionEvidence(
                    source="support-selection-confirmation-layout",
                    confidence=min(
                        selection.page_confidence,
                        confirmation.page_confidence,
                    ),
                    authoritative=(
                        selection.page == "selection"
                        and confirmation.page == "confirmation"
                    ),
                    observed_text=selected.origin,
                    image_path=confirmation_path,
                    region=confirmed.origin_evidence.region,
                ),
                identity_evidence=RecognitionEvidence(
                    source="support-selection-confirmation-identity",
                    confidence=identity_confidence,
                    authoritative=identity_matches,
                    observed_text=(
                        f"selection={selection_path};"
                        f"confirmation={confirmation_path};"
                        f"selected={selected.card_id};confirmed={confirmed.card_id}"
                    ),
                    image_path=confirmation_path,
                    region=confirmed.identity_evidence.region,
                ),
                level_evidence=selected.level_evidence,
            )
        )

    unique_reasons = tuple(dict.fromkeys(reasons))
    return SupportLoadoutReconciliation(
        slots=tuple(reconciled),
        authoritative=not unique_reasons,
        blocking_reasons=unique_reasons,
    )
