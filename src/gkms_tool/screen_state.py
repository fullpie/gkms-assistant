"""OCR the fixed fields on the First Produce action overview screen."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from PIL import Image

from .text_recognizer import PaddleLineRecognizer, TextRecognition


CANONICAL_WIDTH = 720
CANONICAL_HEIGHT = 1280
OVERVIEW_REGIONS: Mapping[str, tuple[int, int, int, int]] = {
    "milestone": (45, 20, 180, 75),
    "stamina": (300, 47, 450, 100),
    "points": (346, 97, 453, 150),
    "weeks": (53, 73, 170, 150),
    "vocal": (177, 670, 287, 723),
    "dance": (316, 670, 433, 723),
    "visual": (460, 670, 577, 723),
}

# The fixed First Star REGULAR route represented by this reader exposes
# three-digit parameter HUD fields.  A fourth digit has repeatedly been an OCR
# glyph from the left edge of the neighbouring UI, never a verified parameter
# value (for example 308 -> 3308).  Reject rather than repair: there is no
# second independent reading in this layer with which to justify a correction.
# First Star REGULAR's actual attribute cap is 1000.  The capped value is a
# valid four-digit HUD value and must not be rejected as an OCR overflow.
MAX_REGULAR_OVERVIEW_PARAMETER = 1000
MAX_NON_REGULAR_OVERVIEW_PARAMETER = 9999
PARAMETER_FIELDS = frozenset({"vocal", "dance", "visual"})


@dataclass(frozen=True, slots=True)
class OverviewState:
    weeks_remaining: int
    stamina: int
    max_stamina: int
    produce_points: int
    vocal: int
    dance: int
    visual: int
    confidence: float
    raw_text: Mapping[str, str]
    countdown_target: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ParameterTemporalEvidence:
    """A shadow-confirmed parameter transition for a non-Regular overview.

    Four-digit HUD values are never accepted from OCR alone.  The caller must
    identify the active non-Regular produce mode and supply the previously
    trusted values plus the independently verified deltas expected since that
    observation.  This layer then accepts only the exact resulting values.
    """

    before: Mapping[str, int]
    expected_deltas: Mapping[str, int]

    def expected_values(self) -> dict[str, int]:
        if set(self.before) != PARAMETER_FIELDS or set(self.expected_deltas) != PARAMETER_FIELDS:
            raise ValueError("parameter temporal evidence must cover vocal, dance, and visual")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.before.values()
        ):
            raise ValueError("parameter temporal evidence before values are invalid")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in self.expected_deltas.values()
        ):
            raise ValueError("parameter temporal evidence deltas are invalid")
        return {
            field: self.before[field] + self.expected_deltas[field]
            for field in PARAMETER_FIELDS
        }


def scale_canonical_box(
    box: tuple[int, int, int, int], image_width: int, image_height: int
) -> tuple[int, int, int, int]:
    scale_x = image_width / CANONICAL_WIDTH
    scale_y = image_height / CANONICAL_HEIGHT
    left, top, right, bottom = box
    return (
        round(left * scale_x),
        round(top * scale_y),
        round(right * scale_x),
        round(bottom * scale_y),
    )


def _integer(text: str, field: str) -> int:
    match = re.search(r"\d+", text)
    if match is None:
        raise ValueError(f"{field} OCR is not an integer: {text!r}")
    return int(match.group())


def _standalone_parameter_integer(text: str, field: str) -> int:
    """Read one HUD parameter without extracting a substring from its crop."""

    # Unlike the week label, this crop contains only the HUD numeral.  Do not
    # silently extract a substring from an inconsistent raw crop.
    if re.fullmatch(r"\s*\d+\s*", text) is None:
        raise ValueError(f"{field} OCR is not a standalone HUD integer: {text!r}")
    return int(text.strip())


def _parameter_integer(text: str, field: str) -> int:
    """Read one Regular bounded parameter without accepting a leading digit."""

    value = _standalone_parameter_integer(text, field)
    if value > MAX_REGULAR_OVERVIEW_PARAMETER:
        raise ValueError(
            f"{field} OCR exceeds the verified First Star REGULAR HUD range: "
            f"{text!r}"
        )
    return value


def _non_regular_parameter_values(
    recognized: Mapping[str, TextRecognition],
    *,
    produce_id: str,
    temporal_evidence: ParameterTemporalEvidence | None,
) -> dict[str, int]:
    """Validate a non-Regular parameter domain without widening generic OCR."""

    if not produce_id or produce_id == "produce-001":
        raise ValueError("a non-Regular four-digit HUD value requires an explicit produce_id")
    values = {
        key: _standalone_parameter_integer(recognized[key].text, key)
        for key in PARAMETER_FIELDS
    }
    if any(value > MAX_NON_REGULAR_OVERVIEW_PARAMETER for value in values.values()):
        raise ValueError("parameter OCR exceeds the verified non-Regular HUD range")
    if not any(value > MAX_REGULAR_OVERVIEW_PARAMETER for value in values.values()):
        return values
    if temporal_evidence is None:
        raise ValueError(
            "a non-Regular four-digit HUD value requires temporal/shadow consistency evidence"
        )
    if values != temporal_evidence.expected_values():
        raise ValueError("non-Regular parameter OCR does not match temporal/shadow evidence")
    return values


def parse_overview_text(
    recognized: Mapping[str, TextRecognition],
    *,
    produce_id: str = "produce-001",
    parameter_temporal_evidence: ParameterTemporalEvidence | None = None,
) -> OverviewState:
    missing = (set(OVERVIEW_REGIONS) - {"milestone"}) - set(recognized)
    if missing:
        raise ValueError(f"missing overview OCR fields: {sorted(missing)}")
    stamina_match = re.search(r"(\d+)\s*[/／]\s*(\d+)", recognized["stamina"].text)
    if stamina_match is None:
        raise ValueError(
            f"stamina OCR is not current/max: {recognized['stamina'].text!r}"
        )
    stamina, max_stamina = map(int, stamina_match.groups())
    if max_stamina < 1 or stamina > max_stamina:
        raise ValueError(f"invalid stamina values: {stamina}/{max_stamina}")
    values = {
        key: _integer(recognized[key].text, key)
        for key in ("weeks", "points")
    }
    if produce_id == "produce-001":
        values.update(
            {
                key: _parameter_integer(recognized[key].text, key)
                for key in PARAMETER_FIELDS
            }
        )
    else:
        values.update(
            _non_regular_parameter_values(
                recognized,
                produce_id=produce_id,
                temporal_evidence=parameter_temporal_evidence,
            )
        )
    milestone = recognized.get("milestone")
    milestone_text = "" if milestone is None else milestone.text
    if any(token in milestone_text for token in ("期中", "中間")):
        countdown_target = "mid1"
    elif any(token in milestone_text for token in ("最終", "期末")):
        countdown_target = "final"
    else:
        countdown_target = "unknown"
    confidences = [
        recognized[key].confidence
        for key in OVERVIEW_REGIONS
        if key in recognized
    ]
    return OverviewState(
        weeks_remaining=values["weeks"],
        stamina=stamina,
        max_stamina=max_stamina,
        produce_points=values["points"],
        vocal=values["vocal"],
        dance=values["dance"],
        visual=values["visual"],
        confidence=sum(confidences) / len(confidences),
        raw_text={
            key: recognized[key].text
            for key in OVERVIEW_REGIONS
            if key in recognized
        },
        countdown_target=countdown_target,
    )


def read_overview(
    image: Image.Image,
    recognizer: PaddleLineRecognizer | None = None,
    *,
    produce_id: str = "produce-001",
    parameter_temporal_evidence: ParameterTemporalEvidence | None = None,
) -> OverviewState:
    recognizer = recognizer or PaddleLineRecognizer()
    recognized = {
        key: recognizer.recognize(
            image.crop(scale_canonical_box(box, image.width, image.height))
        )
        for key, box in OVERVIEW_REGIONS.items()
    }
    return parse_overview_text(
        recognized,
        produce_id=produce_id,
        parameter_temporal_evidence=parameter_temporal_evidence,
    )


def read_overview_path(
    path: Path,
    recognizer: PaddleLineRecognizer | None = None,
    *,
    produce_id: str = "produce-001",
    parameter_temporal_evidence: ParameterTemporalEvidence | None = None,
) -> OverviewState:
    with Image.open(path.resolve()) as image:
        image.load()
        return read_overview(
            image,
            recognizer,
            produce_id=produce_id,
            parameter_temporal_evidence=parameter_temporal_evidence,
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Read the First Produce overview")
    parser.add_argument("image", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            read_overview_path(arguments.image).to_dict(),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
