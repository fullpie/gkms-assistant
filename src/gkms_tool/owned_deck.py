"""Read the out-of-performance ``持有的技能卡`` panel from screenshots.

This panel is available between activities and lists the entire current deck,
unlike the in-performance pile panel which splits cards into draw/discard/lost
piles.  Card positions come from Maa's detector, card identity comes from the
local Octo artwork, upgrade state comes from the visible ``+`` overlay, and the
declared total comes from OCR.  No game process memory is read.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping, Protocol

from PIL import Image

from .card_detector import CardDetection
from .reward_state import detect_reward_upgrade
from .text_recognizer import TextRecognition


OWNED_DECK_HEADER_BOX = (0, 24, 305, 92)
OWNED_DECK_COUNT_BOX = (40, 1010, 260, 1082)
OWNED_DECK_GRID_BOX = (30, 420, 690, 1015)
OWNED_DECK_VISIBLE_CAPACITY = 16


class LineRecognizer(Protocol):
    def recognize(self, image: Image.Image) -> TextRecognition: ...


@dataclass(frozen=True, slots=True)
class OwnedDeckCard:
    page_slot: int
    box: tuple[int, int, int, int]
    card_id: str | None
    upgrade: int | None
    display_name: str
    asset_name: str
    detection_confidence: float
    art_score: float
    art_margin: float
    accepted: bool

    @property
    def key(self) -> tuple[str, int] | None:
        if self.card_id is None or self.upgrade is None:
            return None
        return self.card_id, self.upgrade

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OwnedDeckPage:
    total_count: int
    cards: tuple[OwnedDeckCard, ...]
    header_text: str
    count_text: str
    ocr_confidence: float
    unresolved_slots: int

    @property
    def all_accepted(self) -> bool:
        return bool(self.cards) and all(card.accepted for card in self.cards)

    def to_dict(self) -> dict[str, object]:
        return {
            "total_count": self.total_count,
            "cards": [card.to_dict() for card in self.cards],
            "header_text": self.header_text,
            "count_text": self.count_text,
            "ocr_confidence": self.ocr_confidence,
            "unresolved_slots": self.unresolved_slots,
            "all_accepted": self.all_accepted,
        }


@dataclass(frozen=True, slots=True)
class OwnedDeckInventory:
    total_count: int
    cards: tuple[OwnedDeckCard, ...]
    complete: bool
    overlap_lengths: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "total_count": self.total_count,
            "cards": [card.to_dict() for card in self.cards],
            "observed_count": len(self.cards),
            "complete": self.complete,
            "overlap_lengths": list(self.overlap_lengths),
        }


def parse_owned_deck_count(text: str) -> int:
    compact = re.sub(r"\s+", "", text)
    match = re.search(r"技能卡[\(（](\d+)[\)）]", compact)
    if match is None:
        raise ValueError(f"不是持有技能卡張數：{text!r}")
    count = int(match.group(1))
    if count < 1:
        raise ValueError("持有技能卡張數必須至少為 1")
    return count


def _cluster_detections(
    detections: Iterable[CardDetection],
    *,
    tolerance: int,
) -> tuple[tuple[CardDetection, ...], ...]:
    ordered = sorted(detections, key=lambda item: item.y)
    groups: list[list[CardDetection]] = []
    for detection in ordered:
        if not groups or abs(detection.y - median(item.y for item in groups[-1])) > tolerance:
            groups.append([detection])
        else:
            groups[-1].append(detection)
    return tuple(tuple(sorted(group, key=lambda item: item.x)) for group in groups)


def _visible_grid_cards(
    detections: Iterable[CardDetection], image: Image.Image
) -> tuple[CardDetection, ...]:
    left, top, right, bottom = OWNED_DECK_GRID_BOX
    scaled = (
        round(left * image.width / 720),
        round(top * image.height / 1280),
        round(right * image.width / 720),
        round(bottom * image.height / 1280),
    )
    candidates = tuple(
        item
        for item in detections
        if item.label == "cards"
        and scaled[0] <= item.x < scaled[2]
        and scaled[1] <= item.y < scaled[3]
        and 0.14 <= item.width / image.width <= 0.24
        and 0.075 <= item.height / image.height <= 0.15
    )
    if len(candidates) < 4:
        raise ValueError("目前畫面不像持有技能卡的四欄面板")
    row_tolerance = max(16, round(median(item.height for item in candidates) * 0.24))
    rows = _cluster_detections(candidates, tolerance=row_tolerance)
    if any(len(row) > 4 for row in rows):
        raise ValueError("持有技能卡面板單列超過四張")
    typical_width = round(median(item.width for item in candidates))
    typical_height = round(median(item.height for item in candidates))
    center_groups: list[list[float]] = []
    for center in sorted(item.x + item.width / 2 for item in candidates):
        if not center_groups or center - median(center_groups[-1]) > typical_width * 0.45:
            center_groups.append([center])
        else:
            center_groups[-1].append(center)
    if len(center_groups) != 4:
        raise ValueError(f"持有技能卡面板預期四欄，目前為 {len(center_groups)} 欄")
    column_centers = tuple(round(median(group)) for group in center_groups)

    recovered: list[CardDetection] = []
    for row_index, row in enumerate(rows):
        row_top = round(median(item.y for item in row))
        # The current in-performance panel starts with a three-card hand row,
        # followed by a visibly separated draw-pile section.  Do not invent a
        # fourth hand card; ordinary grid rows use all four columns and may
        # recover one detector miss from the still-visible tile pixels.
        separated_after = (
            row_index + 1 < len(rows)
            and median(item.y for item in rows[row_index + 1]) - row_top
            > typical_height * 1.30
        )
        expected_centers = (
            tuple(item.x + item.width / 2 for item in row)
            if separated_after and len(row) == 3
            else column_centers
        )
        for center in expected_centers:
            nearest = min(
                row,
                key=lambda item: abs(item.x + item.width / 2 - center),
            )
            if abs(nearest.x + nearest.width / 2 - center) <= typical_width * 0.38:
                recovered.append(nearest)
            else:
                recovered.append(
                    CardDetection(
                        "cards",
                        0.0,
                        round(center - typical_width / 2),
                        row_top,
                        typical_width,
                        typical_height,
                    )
                )
    return tuple(recovered)


def read_owned_deck_page(
    image: Image.Image,
    detections: Iterable[CardDetection],
    recognizer: LineRecognizer,
    matcher: object,
    catalog: object,
    card_art: Mapping[str, Path | Image.Image],
    *,
    minimum_art_score: float = 0.20,
    minimum_art_margin: float = 0.035,
    low_margin_art_score: float = 0.65,
    low_margin_art_margin: float = 0.008,
    expected_visible_capacity: int | None = OWNED_DECK_VISIBLE_CAPACITY,
) -> OwnedDeckPage:
    """Read one visible page; callers scroll and merge until the count agrees."""

    header = recognizer.recognize(image.crop(OWNED_DECK_HEADER_BOX))
    if "技能卡" not in re.sub(r"\s+", "", header.text):
        raise ValueError(f"目前畫面不是持有技能卡面板：{header.text!r}")
    count_result = recognizer.recognize(image.crop(OWNED_DECK_COUNT_BOX))
    total_count = parse_owned_deck_count(count_result.text)
    cards = _visible_grid_cards(detections, image)
    identities: list[OwnedDeckCard] = []
    for page_slot, detection in enumerate(cards):
        tile_size = detection.width
        box = (
            detection.x,
            detection.y,
            detection.x + tile_size,
            detection.y + tile_size,
        )
        rendered = image.crop(box)
        match = matcher.match(rendered)
        # A few circular/silhouette card illustrations have a genuinely small
        # nearest-neighbour margin even in two crisp, stable captures.  Permit
        # those only when the absolute match is strong and Maa supplied a real
        # detection.  A zero-confidence box is synthesized by
        # ``_visible_grid_cards`` to recover an occasional detector miss; it
        # must not turn background pixels in an incomplete final row into a
        # phantom card through this relaxed path.
        strong_margin_match = (
            match.score >= minimum_art_score
            and match.margin >= minimum_art_margin
        )
        strong_absolute_match = (
            detection.confidence > 0.0
            and match.score >= low_margin_art_score
            and match.margin >= low_margin_art_margin
        )
        accepted = (
            (strong_margin_match or strong_absolute_match)
            and match.asset_name in card_art
        )
        upgrade: int | None = None
        card_id: str | None = None
        display_name = "辨識信心不足"
        if accepted:
            upgrade = detect_reward_upgrade(rendered, card_art[match.asset_name])
            try:
                entry = catalog.resolve_asset(match.asset_name, upgrade=upgrade)
            except ValueError:
                accepted = False
            else:
                card_id = entry.card_id
                display_name = entry.display_name
        identities.append(
            OwnedDeckCard(
                page_slot=page_slot,
                box=box,
                card_id=card_id,
                upgrade=upgrade if accepted else None,
                display_name=display_name,
                asset_name=match.asset_name,
                detection_confidence=detection.confidence,
                art_score=match.score,
                art_margin=match.margin,
                accepted=accepted,
            )
        )
    expected_visible = (
        len(identities)
        if expected_visible_capacity is None
        else min(total_count, expected_visible_capacity)
    )
    return OwnedDeckPage(
        total_count=total_count,
        cards=tuple(identities),
        header_text=header.text,
        count_text=count_result.text,
        ocr_confidence=min(header.confidence, count_result.confidence),
        unresolved_slots=max(0, expected_visible - len(identities)),
    )


def merge_owned_deck_pages(pages: Iterable[OwnedDeckPage]) -> OwnedDeckInventory:
    """Merge ordered, overlapping pages while retaining unresolved grid gaps."""

    def evidence_quality(card: OwnedDeckCard) -> tuple[float, float, float]:
        return card.art_margin, card.art_score, card.detection_confidence

    def dominates(left: OwnedDeckCard, right: OwnedDeckCard) -> bool:
        """Return true only for a plainly stronger observation of one slot.

        A scrolled tile can be partially covered or sampled during motion and
        occasionally crosses the ordinary acceptance threshold as the wrong
        near-neighbour artwork.  One such weak conflict must not discard a
        long, otherwise exact page overlap.  The evidence gap is deliberately
        large; similar-strength conflicts remain hard blockers.
        """

        if not left.accepted:
            return False
        return bool(
            (
                left.art_margin >= right.art_margin + 0.10
                and left.art_score >= right.art_score - 0.05
            )
            or (
                left.art_score >= right.art_score + 0.20
                and left.art_margin >= right.art_margin + 0.03
            )
        )

    page_list = tuple(pages)
    if not page_list:
        raise ValueError("至少需要一頁持有技能卡畫面")
    totals = {page.total_count for page in page_list}
    if len(totals) != 1:
        raise ValueError(f"持有技能卡頁面張數不一致：{sorted(totals)}")
    total_count = page_list[0].total_count
    merged: list[OwnedDeckCard | None] = [None] * total_count
    for index, card in enumerate(page_list[0].cards[:total_count]):
        if card.key is not None:
            merged[index] = card
    overlaps: list[int] = []
    for page in page_list[1:]:
        candidates: list[tuple[int, int, int, int]] = []
        for offset in range(total_count):
            matches = 0
            fills = 0
            soft_conflicts = 0
            hard_conflict = False
            for page_index, card in enumerate(page.cards):
                target = offset + page_index
                if target >= total_count or card.key is None:
                    continue
                existing = merged[target]
                if existing is None:
                    fills += 1
                elif existing.key == card.key:
                    matches += 1
                elif dominates(existing, card) or dominates(card, existing):
                    soft_conflicts += 1
                else:
                    hard_conflict = True
                    break
            # A single coincidental duplicate cannot justify reconciling a
            # disagreement.  Soft conflict recovery requires a real ordered
            # overlap of at least two independently matching cards.
            if not hard_conflict and matches and (
                not soft_conflicts or matches >= 2
            ):
                candidates.append((matches, fills, -soft_conflicts, offset))
        if not candidates:
            overlaps.append(0)
            continue
        matches, _fills, _negative_conflicts, offset = max(
            candidates,
            key=lambda row: (row[0], row[1], row[2], -row[3]),
        )
        overlaps.append(matches)
        for page_index, card in enumerate(page.cards):
            target = offset + page_index
            if target >= total_count or card.key is None:
                continue
            existing = merged[target]
            if existing is None:
                merged[target] = card
            elif existing.key == card.key:
                if evidence_quality(card) > evidence_quality(existing):
                    merged[target] = card
            elif dominates(card, existing):
                merged[target] = card
            elif not dominates(existing, card):
                # The selected offset should make this unreachable, but keep
                # any future caller fail-closed if page evidence changes.
                raise ValueError("deck page merge encountered an unresolved conflict")
    resolved = tuple(card for card in merged if card is not None)
    complete = (
        len(resolved) == total_count
        and all(card.accepted for card in resolved)
    )
    return OwnedDeckInventory(
        total_count=total_count,
        cards=resolved,
        complete=complete,
        overlap_lengths=tuple(overlaps),
    )
