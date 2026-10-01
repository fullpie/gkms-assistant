"""Read fixed numeric fields from a visible First Produce lesson screen.

The reader consumes only a screenshot. Coordinates are expressed in Maa's
720 x 1280 client space and are scaled when a differently sized capture is
provided.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from PIL import Image

from .screen_state import scale_canonical_box
from .text_recognizer import PaddleLineRecognizer, TextRecognition


LESSON_REGIONS: Mapping[str, tuple[int, int, int, int]] = {
    # Deliberately crop the large digits rather than the localized labels.
    # Keep the crop away from the decorative horizontal bar on the left.  At
    # some animation phases Paddle otherwise merges it with ``3`` as ``13``.
    "turns_remaining": (70, 55, 135, 115),
    "clear_remaining": (310, 102, 420, 192),
    "stamina": (574, 158, 644, 203),
    # Two-digit shields extend left of x=650.  The narrower crop dropped the
    # leading stroke (11 -> 1, 64 -> 34), which made verified shadow states
    # diverge even though the screenshot itself was stable.
    "block": (635, 137, 685, 167),
    "lesson_parameter": (600, 42, 690, 94),
}
# A zero overlaps the cyan shield frame and the wide multi-digit crop can be
# decoded as the CJK glyph ``回``.  Keep the wide crop as the primary source so
# values such as 63/82 retain their leading digit, then use these interior-only
# crops only when the primary result contains no integer at all.
LESSON_BLOCK_FALLBACK_REGIONS: tuple[tuple[int, int, int, int], ...] = (
    (640, 140, 684, 170),
    (645, 140, 684, 170),
    (650, 140, 682, 170),
)
# Once CLEAR is reached, the centre badge changes to the PERFECT layout and
# moves the small ``CLEAR`` caption immediately below the remaining number.
# The normal wide crop is deliberately kept for 2/3-digit targets, but Paddle
# can occasionally return only ``EAR`` on a one-digit PERFECT remainder.  In
# that case retry crops which exclude the caption.
LESSON_CLEAR_FALLBACK_REGIONS: tuple[tuple[int, int, int, int], ...] = (
    (320, 105, 405, 178),
    (330, 110, 395, 175),
)
LESSON_OPTIONAL_NUMERIC_REGIONS: Mapping[
    str, tuple[int, int, int, int]
] = {
    # The right-side countdown belongs to the equipped P-item. It resets after
    # activation, so it must never be interpreted as the lesson round number.
    "item_cooldown": (570, 235, 645, 285),
    # Logic status values appear beside fixed icons once the status is active.
    # The raised-fist motivation icon is the upper row; the thumbs-up good
    # impression icon is the lower row.  Keeping these semantic names correct
    # matters when both statuses are visible because good impression ticks and
    # decays at turn end while motivation does not.
    "motivation": (55, 235, 115, 285),
    "good_impression": (55, 295, 115, 345),
}
# Status icons can contribute a leading stroke (for example visible ``9`` is
# OCRed as ``19``).  These alternatives start to the right of the icon and are
# evaluated independently; the higher-confidence numeric reading wins.
LESSON_OPTIONAL_DIGIT_REGIONS: Mapping[
    str, tuple[int, int, int, int]
] = {
    "motivation": (72, 235, 125, 285),
    "good_impression": (72, 295, 125, 345),
}
LESSON_TEXT_REGIONS: Mapping[str, tuple[int, int, int, int]] = {
    "target_tier": (295, 75, 430, 125),
}
OPTIONAL_NUMERIC_MIN_CONFIDENCE = 0.90


@dataclass(frozen=True, slots=True)
class LessonScreenState:
    turns_remaining: int
    clear_remaining: int
    stamina: int
    block: int
    lesson_parameter: int
    item_cooldown: int | None
    good_impression: int | None
    motivation: int | None
    target_tier: str
    confidence: float
    minimum_confidence: float
    raw_text: Mapping[str, str]
    field_confidence: Mapping[str, float]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _integer(text: str, field: str) -> int:
    normalized = text.strip()
    # The tiny shield digit has identical 0/O geometry at 720 px. Accept this
    # substitution only when the entire numeric crop is that single glyph.
    if normalized in {"O", "o", "〇", "○"}:
        normalized = "0"
    match = re.search(r"\d+", normalized)
    if match is None:
        raise ValueError(f"{field} OCR is not an integer: {text!r}")
    return int(match.group())


def parse_lesson_text(
    recognized: Mapping[str, TextRecognition],
) -> LessonScreenState:
    missing = (
        set(LESSON_REGIONS)
        | set(LESSON_OPTIONAL_NUMERIC_REGIONS)
        | set(LESSON_TEXT_REGIONS)
    ) - set(recognized)
    if missing:
        raise ValueError(f"missing lesson OCR fields: {sorted(missing)}")
    values = {
        key: _integer(recognized[key].text, key) for key in LESSON_REGIONS
    }
    optional_values: dict[str, int | None] = {}
    for key in LESSON_OPTIONAL_NUMERIC_REGIONS:
        try:
            value = _integer(recognized[key].text, key)
            optional_values[key] = (
                value
                if recognized[key].confidence >= OPTIONAL_NUMERIC_MIN_CONFIDENCE
                else None
            )
        except ValueError:
            optional_values[key] = None
    if values["turns_remaining"] < 1:
        raise ValueError("lesson must have at least one remaining turn")
    confidences = {
        key: float(recognized[key].confidence)
        for key in (
            *LESSON_REGIONS,
            *LESSON_OPTIONAL_NUMERIC_REGIONS,
            *LESSON_TEXT_REGIONS,
        )
    }
    trusted_confidences = {
        key: confidence
        for key, confidence in confidences.items()
        if key not in LESSON_OPTIONAL_NUMERIC_REGIONS
        or optional_values[key] is not None
    }
    raw_tier = recognized["target_tier"].text.upper()
    if "PERFECT" in raw_tier:
        target_tier = "perfect"
    elif "CLEAR" in raw_tier:
        target_tier = "clear"
    else:
        raise ValueError(
            f"target_tier OCR is neither CLEAR nor PERFECT: {raw_tier!r}"
        )
    return LessonScreenState(
        turns_remaining=values["turns_remaining"],
        clear_remaining=values["clear_remaining"],
        stamina=values["stamina"],
        block=values["block"],
        lesson_parameter=values["lesson_parameter"],
        item_cooldown=optional_values["item_cooldown"],
        good_impression=optional_values["good_impression"],
        motivation=optional_values["motivation"],
        target_tier=target_tier,
        confidence=sum(trusted_confidences.values()) / len(trusted_confidences),
        minimum_confidence=min(trusted_confidences.values()),
        raw_text={
            key: recognized[key].text
            for key in (
                *LESSON_REGIONS,
                *LESSON_OPTIONAL_NUMERIC_REGIONS,
                *LESSON_TEXT_REGIONS,
            )
        },
        field_confidence=confidences,
    )


def _recognize_lesson_block(
    image: Image.Image,
    recognizer: PaddleLineRecognizer,
) -> TextRecognition:
    primary = recognizer.recognize(
        image.crop(
            scale_canonical_box(
                LESSON_REGIONS["block"], image.width, image.height
            )
        )
    )
    try:
        _integer(primary.text, "block")
    except ValueError:
        numeric_fallbacks: list[TextRecognition] = []
        for box in LESSON_BLOCK_FALLBACK_REGIONS:
            candidate = recognizer.recognize(
                image.crop(scale_canonical_box(box, image.width, image.height))
            )
            try:
                _integer(candidate.text, "block")
            except ValueError:
                continue
            numeric_fallbacks.append(candidate)
        if numeric_fallbacks:
            return max(numeric_fallbacks, key=lambda item: item.confidence)
    return primary


def _recognize_lesson_clear_remaining(
    image: Image.Image,
    recognizer: PaddleLineRecognizer,
) -> TextRecognition:
    primary = recognizer.recognize(
        image.crop(
            scale_canonical_box(
                LESSON_REGIONS["clear_remaining"], image.width, image.height
            )
        )
    )
    try:
        _integer(primary.text, "clear_remaining")
    except ValueError:
        numeric_fallbacks: list[TextRecognition] = []
        for box in LESSON_CLEAR_FALLBACK_REGIONS:
            candidate = recognizer.recognize(
                image.crop(scale_canonical_box(box, image.width, image.height))
            )
            try:
                _integer(candidate.text, "clear_remaining")
            except ValueError:
                continue
            numeric_fallbacks.append(candidate)
        if numeric_fallbacks:
            return max(numeric_fallbacks, key=lambda item: item.confidence)
    return primary


def _recognize_lesson_optional_numeric(
    image: Image.Image,
    recognizer: PaddleLineRecognizer,
    field: str,
) -> TextRecognition:
    primary = recognizer.recognize(
        image.crop(
            scale_canonical_box(
                LESSON_OPTIONAL_NUMERIC_REGIONS[field],
                image.width,
                image.height,
            )
        )
    )
    if field not in LESSON_OPTIONAL_DIGIT_REGIONS:
        return primary
    fallback = recognizer.recognize(
        image.crop(
            scale_canonical_box(
                LESSON_OPTIONAL_DIGIT_REGIONS[field],
                image.width,
                image.height,
            )
        )
    )
    numeric: list[TextRecognition] = []
    for candidate in (primary, fallback):
        try:
            _integer(candidate.text, field)
        except ValueError:
            continue
        numeric.append(candidate)
    return max(numeric, key=lambda item: item.confidence) if numeric else primary


def read_lesson_screen(
    image: Image.Image, recognizer: PaddleLineRecognizer | None = None
) -> LessonScreenState:
    recognizer = recognizer or PaddleLineRecognizer()
    recognized = {
        key: recognizer.recognize(
            image.crop(scale_canonical_box(box, image.width, image.height))
        )
        for key, box in {
            **LESSON_REGIONS,
            **LESSON_OPTIONAL_NUMERIC_REGIONS,
            **LESSON_TEXT_REGIONS,
        }.items()
        if key
        not in {
            "block",
            "clear_remaining",
            *LESSON_OPTIONAL_NUMERIC_REGIONS,
        }
    }
    for key in LESSON_OPTIONAL_NUMERIC_REGIONS:
        recognized[key] = _recognize_lesson_optional_numeric(
            image, recognizer, key
        )
    recognized["block"] = _recognize_lesson_block(image, recognizer)
    recognized["clear_remaining"] = _recognize_lesson_clear_remaining(
        image, recognizer
    )
    return parse_lesson_text(recognized)


def read_lesson_screen_path(
    path: Path, recognizer: PaddleLineRecognizer | None = None
) -> LessonScreenState:
    with Image.open(path.resolve()) as image:
        image.load()
        return read_lesson_screen(image, recognizer)
