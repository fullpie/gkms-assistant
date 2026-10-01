"""Visible deck-pile panel layout reconstruction.

During a lesson or audition the bag button opens a panel containing the draw,
discard, and excluded piles.  MaaGakumasu's bundled card detector also sees
the compact four-column tiles in that panel, but it may merge two identical
neighbouring tiles or miss one.  The printed pile counts and the regular grid
therefore define the slots; YOLO is used to locate the grid, not to invent the
number of cards.

All coordinates in this module are image pixels.  It reads screenshots only
and never accesses game-process memory.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from statistics import median
from typing import Iterable, Protocol

from PIL import Image, ImageOps

from .card_detector import CardDetection
from .text_recognizer import TextRecognition


HAND_PILE = "hand"
DRAW_PILE = "draw"
DISCARD_PILE = "discard"
EXCLUDED_PILE = "excluded"
PILE_ORDER = (HAND_PILE, DRAW_PILE, DISCARD_PILE, EXCLUDED_PILE)
PILE_LABELS = {
    HAND_PILE: "手牌",
    DRAW_PILE: "山札",
    DISCARD_PILE: "捨札",
    EXCLUDED_PILE: "除外",
}

_PILE_ALIASES = {
    HAND_PILE: ("手札", "手牌"),
    DRAW_PILE: ("山札", "牌山", "牌庫", "牌堆"),
    DISCARD_PILE: ("捨札", "棄牌", "弃牌"),
    EXCLUDED_PILE: ("除外", "移除"),
}


class LineRecognizer(Protocol):
    def recognize(self, image: Image.Image) -> TextRecognition: ...


@dataclass(frozen=True, slots=True)
class DeckPileHeader:
    pile: str
    count: int
    raw_text: str
    confidence: float
    box: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeckPanelCell:
    pile: str
    pile_index: int
    row: int
    column: int
    box: tuple[int, int, int, int]
    detection_confidence: float
    inferred_from_grid: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DeckPanelLayout:
    image_width: int
    image_height: int
    column_lefts: tuple[int, int, int, int]
    tile_size: int
    headers: tuple[DeckPileHeader, ...]
    cells: tuple[DeckPanelCell, ...]
    complete: bool
    unresolved_slots: int

    @property
    def visible_total(self) -> int:
        return len(self.cells)

    @property
    def declared_total(self) -> int:
        return sum(header.count for header in self.headers)

    def to_dict(self) -> dict[str, object]:
        return {
            "image_width": self.image_width,
            "image_height": self.image_height,
            "column_lefts": list(self.column_lefts),
            "tile_size": self.tile_size,
            "headers": [header.to_dict() for header in self.headers],
            "cells": [cell.to_dict() for cell in self.cells],
            "visible_total": self.visible_total,
            "declared_total": self.declared_total,
            "complete": self.complete,
            "unresolved_slots": self.unresolved_slots,
        }


@dataclass(frozen=True, slots=True)
class DeckPanelIdentity:
    pile: str
    pile_index: int
    card_id: str | None
    display_name: str
    upgrade: int | None
    art_asset_name: str
    art_score: float
    art_margin: float
    accepted: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_pile_header_text(
    text: str,
    *,
    confidence: float = 1.0,
    box: tuple[int, int, int, int] = (0, 0, 1, 1),
) -> DeckPileHeader:
    """Parse Japanese or translated Chinese pile labels such as ``山札(7)``."""

    compact = re.sub(r"\s+", "", text)
    pile = next(
        (
            key
            for key, aliases in _PILE_ALIASES.items()
            if any(alias in compact for alias in aliases)
        ),
        None,
    )
    count_match = re.search(r"\d+", compact)
    if pile is None or count_match is None:
        raise ValueError(f"不是可辨識的牌堆標題：{text!r}")
    count = int(count_match.group())
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("牌堆標題 OCR 信心必須介於 0 與 1")
    return DeckPileHeader(pile, count, text, confidence, box)


def _cluster(values: Iterable[float], tolerance: float) -> tuple[tuple[float, ...], ...]:
    ordered = sorted(values)
    if not ordered:
        return ()
    groups: list[list[float]] = [[ordered[0]]]
    for value in ordered[1:]:
        if value - median(groups[-1]) <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return tuple(tuple(group) for group in groups)


def _four_columns(
    detections: tuple[CardDetection, ...], tile_size: int
) -> tuple[int, int, int, int]:
    center_groups = _cluster(
        (item.x + item.width / 2 for item in detections),
        tolerance=tile_size * 0.38,
    )
    centers = tuple(round(median(group)) for group in center_groups)
    if len(centers) != 4:
        raise ValueError(
            f"牌組面板預期四欄，實際定位到 {len(centers)} 欄：{centers}"
        )
    gaps = [right - left for left, right in zip(centers, centers[1:])]
    if min(gaps) < tile_size * 0.75 or max(gaps) > tile_size * 1.45:
        raise ValueError(f"牌組面板欄距不規則：{gaps}")
    return tuple(round(center - tile_size / 2) for center in centers)  # type: ignore[return-value]


def _row_segments(
    detections: tuple[CardDetection, ...], tile_size: int
) -> tuple[tuple[int, ...], ...]:
    groups = _cluster(
        (float(item.y) for item in detections),
        tolerance=tile_size * 0.30,
    )
    row_tops = tuple(round(median(group)) for group in groups)
    if not row_tops:
        raise ValueError("牌組面板沒有卡牌列")
    segments: list[list[int]] = [[row_tops[0]]]
    for row_top in row_tops[1:]:
        if row_top - segments[-1][-1] > tile_size * 1.30:
            segments.append([row_top])
        else:
            segments[-1].append(row_top)
    return tuple(tuple(segment) for segment in segments)


def _read_header(
    image: Image.Image,
    first_column_left: int,
    first_row_top: int,
    tile_size: int,
    recognizer: LineRecognizer,
) -> DeckPileHeader:
    box = (
        max(0, first_column_left - round(tile_size * 0.12)),
        max(0, first_row_top - round(tile_size * 0.34)),
        min(image.width, first_column_left + round(tile_size * 2.0)),
        min(image.height, first_row_top + round(tile_size * 0.05)),
    )
    crop = image.crop(box)
    candidates = (
        recognizer.recognize(crop),
        recognizer.recognize(ImageOps.autocontrast(crop.convert("L"))),
    )
    parsed: list[DeckPileHeader] = []
    for result in candidates:
        try:
            parsed.append(
                parse_pile_header_text(
                    result.text,
                    confidence=result.confidence,
                    box=box,
                )
            )
        except ValueError:
            continue
    if not parsed:
        rendered = ", ".join(repr(result.text) for result in candidates)
        raise ValueError(f"牌堆標題 OCR 無法判讀：{rendered}")
    return max(parsed, key=lambda header: header.confidence)


def _nearest_detection(
    detections: tuple[CardDetection, ...],
    *,
    left: int,
    top: int,
    tile_size: int,
    used: set[int],
) -> tuple[float, bool]:
    target_x = left + tile_size / 2
    target_y = top + tile_size / 2
    ranked = sorted(
        (
            (
                abs(item.x + item.width / 2 - target_x)
                + abs(item.y + min(item.height, tile_size) / 2 - target_y),
                index,
                item,
            )
            for index, item in enumerate(detections)
            if index not in used
        ),
        key=lambda entry: entry[0],
    )
    if not ranked or ranked[0][0] > tile_size * 0.62:
        return 0.0, True
    _, index, detection = ranked[0]
    used.add(index)
    return detection.confidence, False


def read_deck_panel_layout(
    image: Image.Image,
    detections: Iterable[CardDetection],
    recognizer: LineRecognizer,
) -> DeckPanelLayout:
    """Recover every visible grid slot using pile counts as the authority.

    ``complete`` means all cards declared by every visible pile header fit in
    the detected rows.  If a pile continues below the viewport, callers must
    scroll and merge another page before building an authoritative snapshot.
    """

    raw_cards = tuple(
        item
        for item in detections
        if item.label == "cards" and item.confidence >= 0.50
    )
    if len(raw_cards) < 3:
        raise ValueError("目前畫面不像已打開的牌組面板")
    preliminary_size = median(item.width for item in raw_cards)
    cards = tuple(
        item
        for item in raw_cards
        if (
            preliminary_size * 0.70 <= item.width <= preliminary_size * 1.45
            and preliminary_size * 0.70 <= item.height <= preliminary_size * 1.45
        )
    )
    if len(cards) < 3:
        raise ValueError("牌組面板卡格被非卡牌的大型區塊干擾")
    tile_size = round(median(item.width for item in cards))
    if tile_size < 24:
        raise ValueError(f"牌組面板卡格過小：{tile_size}px")
    column_lefts = _four_columns(cards, tile_size)
    segments = _row_segments(cards, tile_size)

    headers: list[DeckPileHeader] = []
    cells: list[DeckPanelCell] = []
    unresolved_slots = 0
    used_detections: set[int] = set()
    seen_piles: set[str] = set()
    for segment in segments:
        header = _read_header(
            image,
            column_lefts[0],
            segment[0],
            tile_size,
            recognizer,
        )
        if header.pile in seen_piles:
            raise ValueError(f"牌組面板重複出現 {PILE_LABELS[header.pile]}")
        seen_piles.add(header.pile)
        headers.append(header)
        visible_capacity = len(segment) * 4
        visible_count = min(header.count, visible_capacity)
        unresolved_slots += max(0, header.count - visible_capacity)
        for pile_index in range(visible_count):
            row = pile_index // 4
            column = pile_index % 4
            left = column_lefts[column]
            top = segment[row]
            confidence, inferred = _nearest_detection(
                cards,
                left=left,
                top=top,
                tile_size=tile_size,
                used=used_detections,
            )
            cells.append(
                DeckPanelCell(
                    pile=header.pile,
                    pile_index=pile_index,
                    row=row,
                    column=column,
                    box=(left, top, left + tile_size, top + tile_size),
                    detection_confidence=confidence,
                    inferred_from_grid=inferred,
                )
            )

    ordered_headers = tuple(
        sorted(headers, key=lambda header: PILE_ORDER.index(header.pile))
    )
    complete = unresolved_slots == 0 and all(
        header.confidence >= 0.80 for header in ordered_headers
    )
    return DeckPanelLayout(
        image_width=image.width,
        image_height=image.height,
        column_lefts=column_lefts,
        tile_size=tile_size,
        headers=ordered_headers,
        cells=tuple(cells),
        complete=complete,
        unresolved_slots=unresolved_slots,
    )


def identify_deck_panel_cards(
    image: Image.Image,
    layout: DeckPanelLayout,
    matcher: object,
    catalog: object,
    *,
    minimum_score: float = 0.52,
    minimum_margin: float = 0.035,
) -> tuple[DeckPanelIdentity, ...]:
    """Resolve compact art without guessing the separate red ``+`` overlay."""

    identities: list[DeckPanelIdentity] = []
    for cell in layout.cells:
        rendered = image.crop(cell.box)
        match = matcher.match(rendered)
        accepted = (
            match.score >= minimum_score and match.margin >= minimum_margin
        )
        if accepted:
            entry = catalog.resolve_asset(match.asset_name)
            card_id = entry.card_id
            display_name = entry.display_name
        else:
            card_id = None
            display_name = "辨識信心不足"
        identities.append(
            DeckPanelIdentity(
                pile=cell.pile,
                pile_index=cell.pile_index,
                card_id=card_id,
                display_name=display_name,
                upgrade=None,
                art_asset_name=match.asset_name,
                art_score=match.score,
                art_margin=match.margin,
                accepted=accepted,
            )
        )
    return tuple(identities)
