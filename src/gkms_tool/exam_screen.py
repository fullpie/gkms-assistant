"""Screenshot-only reader for a First Produce audition hand."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
from PIL import Image

from .screen_state import scale_canonical_box
from .text_recognizer import PaddleLineRecognizer, TextRecognition


# Maa canonical client coordinates are 720 x 1280.  The crops deliberately
# exclude localized labels and icons; only the stable numeric glyphs remain.
EXAM_REGIONS: Mapping[str, tuple[int, int, int, int]] = {
    # The tighter vertical crop keeps the animated ring highlight from making
    # a 7 look like a 1 while still fitting the two-digit 10/11 states.
    "turns_remaining": (39, 72, 84, 116),
    # Keep only the lower numeric line.  Including the attribute label above
    # caused high-confidence leading-digit hallucinations (884% -> 1884%).
    "score_multiplier": (105, 85, 225, 116),
    "stamina": (578, 213, 632, 243),
    # The zero glyph sits partly over the cyan shield frame.  A narrow crop
    # can therefore be classified as the CJK glyph ``回`` even though nonzero
    # values read correctly.  Keeping the full glyph width while trimming the
    # animated frame vertically is stable for both 0 and multi-digit values.
    "block": (640, 178, 690, 208),
}
PLAYER_SCORE_RELATIVE_REGIONS: tuple[tuple[int, int, int, int], ...] = (
    (6, 134, 30, 159),
    (3, 124, 65, 170),
    (4, 127, 60, 167),
    (5, 128, 65, 168),
    (0, 120, 100, 172),
    (4, 127, 95, 167),
    (0, 118, 124, 174),
    (4, 124, 123, 170),
)
TRACKED_SCORE_ROUNDING_TOLERANCE = 2
CANONICAL_SIZE = (720, 1280)
RANKING_BAND_BOTTOM = 165
RANKING_BORDER_MIN_X = 170
RANKING_BORDER_WHITE_THRESHOLD = 225
RANKING_BORDER_MIN_PIXELS = 100
EXPANDED_TILE_MIN_WIDTH = 110
EXPANDED_TILE_MAX_WIDTH = 130

# Logic status rows are rendered in activation order.  Their vertical position
# therefore does not identify Review (good impression) versus Aggressive
# (motivation).  The cyan icon fill is, however, a stable piece of the HUD: an
# inactive Plan2 placeholder has the white icon but no cyan fill, while an
# active compact status has the exact UI cyan used by all historical captures.
# Scan the icon band first, then OCR numbers relative to every detected row.
EXAM_LOGIC_STATUS_ICON_BAND = (5, 215, 75, 410)
EXAM_LOGIC_STATUS_NUMBER_X = (55, 125)
EXAM_LOGIC_STATUS_DIGIT_X = (72, 125)
EXAM_LOGIC_STATUS_MIN_ICON_PIXELS = 250
EXAM_LOGIC_STATUS_PARTIAL_ICON_PIXELS = 25
EXAM_LOGIC_STATUS_MIN_CONFIDENCE = 0.90
EXAM_LOGIC_STATUS_MAX_BOOTSTRAP_ROWS = 2


@dataclass(frozen=True, slots=True)
class ExamLogicStatusObservation:
    """Conservative evidence for the two Plan2 scalar status values.

    ``values`` is deliberately an unordered candidate multiset.  ``None``
    means that the layout or at least one active row could not be verified;
    an empty tuple means that the inactive HUD anchor was seen and no active
    status row was present.  The latter is the only case where zero is proven.

    Semantic fields stay ``None`` when the icon shape has not been named
    independently.  Call :func:`reconcile_exam_logic_statuses` with known
    first-turn passive outcomes to name an otherwise unordered multiset.
    """

    values: tuple[int, ...] | None
    good_impression: int | None
    motivation: int | None
    confidence: float | None
    minimum_confidence: float | None
    raw_text: tuple[str, ...]
    row_boxes: tuple[tuple[int, int, int, int], ...]
    issue: str | None = None

    @property
    def layout_verified(self) -> bool:
        return self.values is not None

    @property
    def absence_verified(self) -> bool:
        return self.values == ()

    @classmethod
    def unknown(cls, issue: str) -> "ExamLogicStatusObservation":
        return cls(None, None, None, None, None, (), (), issue)


@dataclass(frozen=True, slots=True)
class ExamLogicStatusResolution:
    good_impression: int
    motivation: int


@dataclass(frozen=True, slots=True)
class ExamScreenState:
    turns_remaining: int
    score_multiplier_permille: int
    stamina: int
    block: int
    player_score: int
    logic_status: ExamLogicStatusObservation
    confidence: float
    minimum_confidence: float
    raw_text: Mapping[str, str]
    field_confidence: Mapping[str, float]

    @property
    def score_multiplier_percent(self) -> int:
        return self.score_multiplier_permille // 10

    @property
    def good_impression(self) -> int | None:
        return self.logic_status.good_impression

    @property
    def motivation(self) -> int | None:
        return self.logic_status.motivation

    @property
    def logic_status_values(self) -> tuple[int, ...] | None:
        return self.logic_status.values

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _integer(text: str, field: str) -> int:
    normalized = text.strip()
    if normalized in {"O", "o"}:
        normalized = "0"
    match = re.search(r"\d+", normalized)
    if match is None:
        raise ValueError(f"{field} OCR is not an integer: {text!r}")
    return int(match.group())


def _multiplier_permille(text: str) -> int:
    normalized = text.strip().replace("％", "%")
    match = re.search(r"(\d+)\s*%", normalized)
    if match is not None:
        raw = match.group(1)
        percent = int(raw)
    else:
        digits = re.search(r"\d+", normalized)
        if digits is None:
            raise ValueError(
                f"score_multiplier OCR is not a percentage: {text!r}"
            )
        raw = digits.group()
        # Paddle occasionally decodes the percent glyph as a trailing 9.
        if len(raw) >= 3 and raw.endswith("9"):
            raw = raw[:-1]
        percent = int(raw)
    if percent > 3000:
        # The attribute icon immediately to the left of the multiplier can be
        # joined to the OCR line (the live N.I.A. frame ``902%`` was read as
        # ``7902%``).  Keep the longest plausible numeric suffix.  This is a
        # local glyph-boundary repair; ExamSave remains the action/state
        # authority and a bad multiplier reading must not reject a completed
        # card play.
        suffixes = tuple(
            int(raw[-width:])
            for width in range(min(len(raw), 4), 0, -1)
            if 1 <= int(raw[-width:]) <= 3000
        )
        if suffixes:
            percent = suffixes[0]
    if not 1 <= percent <= 3000:
        raise ValueError(f"score multiplier OCR has no plausible suffix: {text!r}")
    return percent * 10


def parse_exam_text(
    recognized: Mapping[str, TextRecognition],
    *,
    logic_status: ExamLogicStatusObservation | None = None,
) -> ExamScreenState:
    required = {*EXAM_REGIONS, "player_score"}
    missing = required - set(recognized)
    if missing:
        raise ValueError(f"missing exam OCR fields: {sorted(missing)}")
    turns = _integer(recognized["turns_remaining"].text, "turns_remaining")
    stamina = _integer(recognized["stamina"].text, "stamina")
    block = _integer(recognized["block"].text, "block")
    score = _integer(recognized["player_score"].text, "player_score")
    multiplier = _multiplier_permille(recognized["score_multiplier"].text)
    if turns < 1:
        raise ValueError("audition must have at least one remaining turn")
    confidences = {
        key: float(recognized[key].confidence) for key in required
    }
    return ExamScreenState(
        turns_remaining=turns,
        score_multiplier_permille=multiplier,
        stamina=stamina,
        block=block,
        player_score=score,
        logic_status=(
            logic_status
            if logic_status is not None
            else ExamLogicStatusObservation.unknown(
                "logic status layout was not inspected"
            )
        ),
        confidence=sum(confidences.values()) / len(confidences),
        minimum_confidence=min(confidences.values()),
        raw_text={key: recognized[key].text for key in required},
        field_confidence=confidences,
    )


def _active_logic_status_rows(
    image: Image.Image,
) -> tuple[tuple[int, int], ...]:
    """Return active compact-row vertical bounds in canonical coordinates."""

    canonical = image.convert("RGB")
    if canonical.size != CANONICAL_SIZE:
        canonical = canonical.resize(CANONICAL_SIZE, Image.Resampling.BILINEAR)
    pixels = np.asarray(canonical)
    left, top, right, bottom = EXAM_LOGIC_STATUS_ICON_BAND
    icon_band = pixels[top:bottom, left:right]
    cyan = (
        (icon_band[:, :, 0] <= 45)
        & (icon_band[:, :, 1] >= 160)
        & (icon_band[:, :, 1] <= 210)
        & (icon_band[:, :, 2] >= 235)
    )
    occupied_y = np.flatnonzero(cyan.sum(axis=1) > 0)
    groups: list[list[int]] = []
    for raw_y in occupied_y:
        y = int(raw_y)
        if not groups or y > groups[-1][-1] + 5:
            groups.append([y])
        else:
            groups[-1].append(y)
    rows = []
    for group in groups:
        group_pixels = int(cyan[group[0] : group[-1] + 1].sum())
        if group_pixels < EXAM_LOGIC_STATUS_MIN_ICON_PIXELS:
            continue
        rows.append((top + group[0], top + group[-1] + 1))
    return tuple(rows)


def _logic_status_cyan_pixel_count(image: Image.Image) -> int:
    canonical = image.convert("RGB")
    if canonical.size != CANONICAL_SIZE:
        canonical = canonical.resize(CANONICAL_SIZE, Image.Resampling.BILINEAR)
    pixels = np.asarray(canonical)
    left, top, right, bottom = EXAM_LOGIC_STATUS_ICON_BAND
    icon_band = pixels[top:bottom, left:right]
    return int(
        (
            (icon_band[:, :, 0] <= 45)
            & (icon_band[:, :, 1] >= 160)
            & (icon_band[:, :, 1] <= 210)
            & (icon_band[:, :, 2] >= 235)
        ).sum()
    )


def _inactive_logic_status_anchor_present(image: Image.Image) -> bool:
    """Verify the white Plan2 placeholder instead of treating blank OCR as 0."""

    canonical = image.convert("RGB")
    if canonical.size != CANONICAL_SIZE:
        canonical = canonical.resize(CANONICAL_SIZE, Image.Resampling.BILINEAR)
    pixels = np.asarray(canonical)
    left, top, right, bottom = (0, 215, 90, 300)
    white = np.all(pixels[top:bottom, left:right] >= 252, axis=2)
    visited = np.zeros(white.shape, dtype=bool)
    for raw_y, raw_x in zip(*np.where(white)):
        y = int(raw_y)
        x = int(raw_x)
        if visited[y, x]:
            continue
        stack = [(y, x)]
        visited[y, x] = True
        component: list[tuple[int, int]] = []
        while stack:
            current_y, current_x = stack.pop()
            component.append((current_y, current_x))
            for delta_y, delta_x in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                next_y = current_y + delta_y
                next_x = current_x + delta_x
                if not (
                    0 <= next_y < white.shape[0]
                    and 0 <= next_x < white.shape[1]
                    and white[next_y, next_x]
                    and not visited[next_y, next_x]
                ):
                    continue
                visited[next_y, next_x] = True
                stack.append((next_y, next_x))
        if not 300 <= len(component) <= 700:
            continue
        component_y = [point[0] for point in component]
        component_x = [point[1] for point in component]
        min_x, max_x = min(component_x), max(component_x)
        min_y, max_y = min(component_y), max(component_y)
        width = max_x - min_x + 1
        height = max_y - min_y + 1
        canonical_min_x = left + min_x
        canonical_min_y = top + min_y
        if (
            20 <= width <= 35
            and 20 <= height <= 45
            and 20 <= canonical_min_x <= 70
            and 225 <= canonical_min_y <= 295
        ):
            return True
    return False


def _recognize_exam_logic_status_row(
    image: Image.Image,
    recognizer: PaddleLineRecognizer,
    row: tuple[int, int],
) -> tuple[TextRecognition, tuple[int, int, int, int]]:
    top, bottom = row
    primary_box = (
        EXAM_LOGIC_STATUS_NUMBER_X[0],
        max(0, top - 4),
        EXAM_LOGIC_STATUS_NUMBER_X[1],
        min(CANONICAL_SIZE[1], bottom + 4),
    )
    digit_box = (
        EXAM_LOGIC_STATUS_DIGIT_X[0],
        primary_box[1],
        EXAM_LOGIC_STATUS_DIGIT_X[1],
        primary_box[3],
    )
    candidates = []
    attempted: list[TextRecognition] = []
    for box in (primary_box, digit_box):
        candidate = recognizer.recognize(
            image.crop(scale_canonical_box(box, image.width, image.height))
        )
        attempted.append(candidate)
        try:
            value = _integer(candidate.text, "logic_status")
        except ValueError:
            continue
        if value > 0:
            candidates.append(candidate)
    if candidates:
        return max(candidates, key=lambda item: item.confidence), primary_box
    # Preserve the primary raw OCR result for diagnostics even when neither
    # crop is a positive integer; never turn that failure into zero.
    return attempted[0], primary_box


def read_exam_logic_status_observation(
    image: Image.Image,
    recognizer: PaddleLineRecognizer,
) -> ExamLogicStatusObservation:
    """Read first-turn Plan2 scalar rows without assigning positional names."""

    rows = _active_logic_status_rows(image)
    if len(rows) > EXAM_LOGIC_STATUS_MAX_BOOTSTRAP_ROWS:
        return ExamLogicStatusObservation.unknown(
            "more than two active compact status rows are visible"
        )
    if not rows:
        # A partially faded/entering cyan icon is neither absence nor a stable
        # active row.  Do not let its white symbol satisfy the inactive-anchor
        # check and silently collapse an unread status to zero.
        if (
            _logic_status_cyan_pixel_count(image)
            >= EXAM_LOGIC_STATUS_PARTIAL_ICON_PIXELS
        ):
            return ExamLogicStatusObservation.unknown(
                "compact status icon is between animation frames"
            )
        if not _inactive_logic_status_anchor_present(image):
            return ExamLogicStatusObservation.unknown(
                "inactive logic-status HUD anchor is not verified"
            )
        return ExamLogicStatusObservation(
            values=(),
            good_impression=0,
            motivation=0,
            confidence=None,
            minimum_confidence=None,
            raw_text=(),
            row_boxes=(),
        )

    readings: list[TextRecognition] = []
    boxes: list[tuple[int, int, int, int]] = []
    values: list[int] = []
    for row in rows:
        reading, box = _recognize_exam_logic_status_row(
            image, recognizer, row
        )
        readings.append(reading)
        boxes.append(box)
        try:
            value = _integer(reading.text, "logic_status")
        except ValueError:
            value = 0
        if (
            value <= 0
            or reading.confidence < EXAM_LOGIC_STATUS_MIN_CONFIDENCE
        ):
            confidences = [float(item.confidence) for item in readings]
            return ExamLogicStatusObservation(
                values=None,
                good_impression=None,
                motivation=None,
                confidence=sum(confidences) / len(confidences),
                minimum_confidence=min(confidences),
                raw_text=tuple(item.text for item in readings),
                row_boxes=tuple(boxes),
                issue="an active compact status row has no trusted value",
            )
        values.append(value)
    confidences = [float(item.confidence) for item in readings]
    return ExamLogicStatusObservation(
        values=tuple(values),
        good_impression=None,
        motivation=None,
        confidence=sum(confidences) / len(confidences),
        minimum_confidence=min(confidences),
        raw_text=tuple(item.text for item in readings),
        row_boxes=tuple(boxes),
    )


def reconcile_exam_logic_statuses(
    observation: ExamLogicStatusObservation,
    possible_values: Mapping[str, int | Sequence[int]] | None = None,
) -> ExamLogicStatusResolution:
    """Name an observed status multiset only when exactly one assignment fits.

    ``possible_values`` must describe the full set of first-turn outcomes for
    both fields, including zero for a probabilistic passive.  An integer is a
    convenience shorthand for ``(0, value)``.  Duplicate candidate values are
    intentionally ambiguous when only one row is visible.
    """

    if not isinstance(observation, ExamLogicStatusObservation):
        raise TypeError("observation must be ExamLogicStatusObservation")
    if observation.values is None:
        raise ValueError(
            "audition logic-status layout is unverified: "
            + (observation.issue or "unknown status row")
        )
    if (
        observation.good_impression is not None
        and observation.motivation is not None
    ):
        return ExamLogicStatusResolution(
            observation.good_impression,
            observation.motivation,
        )
    if not observation.values:
        return ExamLogicStatusResolution(0, 0)
    if possible_values is None:
        raise ValueError(
            "active audition logic-status values are semantically unnamed"
        )
    required = {"good_impression", "motivation"}
    if set(possible_values) != required:
        raise ValueError(
            "possible logic-status values must name good_impression and motivation"
        )

    normalized: dict[str, tuple[int, ...]] = {}
    for name in ("good_impression", "motivation"):
        raw = possible_values[name]
        values = (0, int(raw)) if isinstance(raw, int) else tuple(
            int(value) for value in raw
        )
        if not values or any(value < 0 for value in values):
            raise ValueError(f"invalid possible values for {name}: {values!r}")
        normalized[name] = tuple(sorted(set(values)))

    observed_multiset = tuple(sorted(observation.values))
    assignments = []
    for good_impression, motivation in product(
        normalized["good_impression"], normalized["motivation"]
    ):
        candidate_multiset = tuple(
            sorted(
                value
                for value in (good_impression, motivation)
                if value > 0
            )
        )
        if candidate_multiset == observed_multiset:
            assignments.append(
                ExamLogicStatusResolution(good_impression, motivation)
            )
    if len(assignments) != 1:
        detail = "no" if not assignments else "multiple"
        raise ValueError(
            f"{detail} unique semantic assignment for audition status values "
            f"{observed_multiset!r}"
        )
    return assignments[0]


def _recognize_player_score(
    image: Image.Image,
    recognizer: PaddleLineRecognizer,
    *,
    expected_score: int | None,
) -> TextRecognition:
    player_left = _expanded_player_tile_left(image)
    candidates: list[tuple[int, TextRecognition]] = []
    for left, top, right, bottom in PLAYER_SCORE_RELATIVE_REGIONS:
        box = (
            player_left + left,
            top,
            player_left + right,
            bottom,
        )
        result = recognizer.recognize(
            image.crop(scale_canonical_box(box, image.width, image.height))
        )
        try:
            value = _integer(result.text, "player_score")
        except ValueError:
            continue
        candidates.append((value, result))
    if not candidates:
        raise ValueError("player_score OCR produced no numeric candidate")
    if expected_score is not None:
        exact = [item for item in candidates if item[0] == expected_score]
        if exact:
            return max(exact, key=lambda item: item[1].confidence)[1]
        nearby = [
            item
            for item in candidates
            if abs(item[0] - expected_score)
            <= TRACKED_SCORE_ROUNDING_TOLERANCE
        ]
        if nearby:
            return min(
                nearby,
                key=lambda item: (
                    abs(item[0] - expected_score),
                    -item[1].confidence,
                ),
            )[1]
        raise ValueError(
            "player_score OCR did not reproduce the tracked checkpoint "
            f"value {expected_score}"
        )
    # Narrow crops can confidently read only the left-most one or two digits
    # of a four-digit score.  Prefer the candidate that preserves the most
    # numeric glyphs, then use OCR confidence as the tie-breaker.
    return max(
        candidates,
        key=lambda item: (len(str(item[0])), item[1].confidence),
    )[1]


def _expanded_player_tile_left(image: Image.Image) -> int:
    """Locate the player's expanded audition ranking tile.

    The player cell is the only ranking cell whose two near-white vertical
    borders are about 120 canonical pixels apart.  Its position follows the
    player's rank, so using a fixed score crop can silently read an NPC score.
    """

    canonical = image.convert("RGB")
    if canonical.size != CANONICAL_SIZE:
        canonical = canonical.resize(CANONICAL_SIZE, Image.Resampling.BILINEAR)
    pixels = np.asarray(canonical)
    band = pixels[:RANKING_BAND_BOTTOM, :, :]
    white = np.all(band > RANKING_BORDER_WHITE_THRESHOLD, axis=2)
    # The selected player tile uses a gold border while holding 1st/2nd/3rd.
    # Treat that border as the same structural evidence as the ordinary white
    # border; the tile width remains invariant and no rank coordinate is
    # hard-coded.
    gold = (
        (band[:, :, 0] >= 180)
        & (band[:, :, 1] >= 115)
        & (band[:, :, 1] <= 245)
        & (band[:, :, 2] <= 170)
        & (band[:, :, 0] >= band[:, :, 2] + 35)
    )
    border_counts = (white | gold).sum(axis=0)
    candidate_x = np.flatnonzero(
        (border_counts >= RANKING_BORDER_MIN_PIXELS)
        & (np.arange(CANONICAL_SIZE[0]) >= RANKING_BORDER_MIN_X)
    )

    pairs = tuple(
        (int(left), int(right))
        for left in candidate_x
        for right in candidate_x
        if EXPANDED_TILE_MIN_WIDTH
        <= int(right) - int(left)
        <= EXPANDED_TILE_MAX_WIDTH
    )
    if pairs:
        # Thick borders contribute several adjacent candidate columns.  Pick
        # the strongest pair at the invariant expanded-tile width instead of
        # assuming the coloured interior is one connected component.
        left, _ = max(
            pairs,
            key=lambda pair: (
                min(int(border_counts[pair[0]]), int(border_counts[pair[1]])),
                int(border_counts[pair[0]]) + int(border_counts[pair[1]]),
            ),
        )
        return left
    raise ValueError("could not locate expanded player ranking tile")


def read_exam_screen(
    image: Image.Image,
    recognizer: PaddleLineRecognizer | None = None,
    *,
    expected_score: int | None = None,
) -> ExamScreenState:
    recognizer = recognizer or PaddleLineRecognizer()
    recognized = {
        key: recognizer.recognize(
            image.crop(scale_canonical_box(box, image.width, image.height))
        )
        for key, box in EXAM_REGIONS.items()
    }
    recognized["player_score"] = _recognize_player_score(
        image,
        recognizer,
        expected_score=expected_score,
    )
    logic_status = read_exam_logic_status_observation(image, recognizer)
    return parse_exam_text(recognized, logic_status=logic_status)


def read_exam_screen_path(
    path: Path,
    recognizer: PaddleLineRecognizer | None = None,
    *,
    expected_score: int | None = None,
) -> ExamScreenState:
    with Image.open(path.resolve()) as image:
        image.load()
        return read_exam_screen(
            image,
            recognizer,
            expected_score=expected_score,
        )
