"""Recognize the visible Vo/Da/Vi training-choice screen.

The option templates come from the checked-out MaaGakumasu resources.  Their
bright-green areas are masks, so localized option text is deliberately ignored;
only the stable icon, outline, and button shape are compared.  The recommended
attribute is read from the colored recommendation text in the speech bubble.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

import numpy as np
from PIL import Image

from .live_capture import CANONICAL_HEIGHT, CANONICAL_WIDTH


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEMPLATE_DIR = (
    PROJECT_ROOT
    / "_research"
    / "MaaGakumasu"
    / "assets"
    / "resource"
    / "base"
    / "image"
    / "produce"
)
ATTRIBUTE_LABELS: Mapping[str, str] = {
    "Vo": "歌唱",
    "Da": "舞蹈",
    "Vi": "視覺效果",
}
TEMPLATE_FILES: Mapping[str, str] = {
    "Vo": "choose_Vo.png",
    "Da": "choose_Da.png",
    "Vi": "choose_Vi.png",
}


@dataclass(frozen=True, slots=True)
class TrainingOption:
    attribute: str
    label: str
    canonical_box: tuple[int, int, int, int]
    match_score: float
    highlighted: bool = False

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.canonical_box
        return (left + right) // 2, (top + bottom) // 2

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TrainingChoiceState:
    recommended_attribute: str | None
    recommendation_confidence: float
    options: tuple[TrainingOption, ...]

    @property
    def recommended_option(self) -> TrainingOption:
        if self.recommended_attribute is None:
            raise ValueError("the game does not show a decisive recommended attribute")
        for option in self.options:
            if option.attribute == self.recommended_attribute:
                return option
        raise ValueError("recommended attribute is not present in visible options")

    def to_dict(self) -> dict[str, object]:
        return {
            "recommended_attribute": self.recommended_attribute,
            "recommendation_confidence": self.recommendation_confidence,
            "options": [option.to_dict() for option in self.options],
        }


def _canonical_rgb(image: Image.Image) -> np.ndarray:
    return np.asarray(
        image.convert("RGB").resize(
            (CANONICAL_WIDTH, CANONICAL_HEIGHT), Image.Resampling.LANCZOS
        ),
        dtype=np.int16,
    )


def _template_samples(template: Image.Image) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    pixels = np.asarray(template.convert("RGB"), dtype=np.int16)
    red, green, blue = pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
    green_mask = (green > 235) & (red < 35) & (blue < 35)
    maximum = pixels.max(axis=2)
    minimum = pixels.min(axis=2)
    saturation = maximum - minimum
    informative = ~green_mask & ((saturation > 20) | (maximum < 205))
    ys, xs = np.nonzero(informative)
    if len(xs) < 100:
        raise ValueError("training option template has too few informative pixels")
    # A deterministic sample keeps the small local search fast without losing
    # the colored outline or icon geometry.
    stride = max(1, len(xs) // 2800)
    ys = ys[::stride]
    xs = xs[::stride]
    return ys, xs, pixels[ys, xs]


def _best_template_match(
    screen: np.ndarray,
    template: Image.Image,
    *,
    x_range: range = range(42, 69),
    y_range: range = range(560, 1041, 2),
) -> tuple[int, int, float]:
    template_width, template_height = template.size
    ys, xs, expected = _template_samples(template)
    best_x = best_y = 0
    best_error = float("inf")
    for top in y_range:
        if top + template_height > screen.shape[0]:
            break
        for left in x_range:
            if left + template_width > screen.shape[1]:
                continue
            observed = screen[top + ys, left + xs]
            error = float(np.mean(np.abs(observed - expected)))
            if error < best_error:
                best_x, best_y, best_error = left, top, error
    if not np.isfinite(best_error):
        raise ValueError("training option template search had no valid positions")
    return best_x, best_y, max(0.0, 1.0 - best_error / 255.0)


def _recommendation_color(screen: np.ndarray) -> tuple[str, float]:
    # Restrict the sample to the two text rows inside the speech bubble.  The
    # previous 150:290 crop reached the three attribute gauges below it; on an
    # unselected final-training screen the blue Da ring could therefore
    # outvote the genuinely orange ``recommend Vi`` text.  The right edge also
    # stays left of the chibi portrait and its unrelated colours.
    crop = screen[180:240, 300:625].astype(np.float32)
    palettes = {
        "Vo": np.array((245, 65, 145), dtype=np.float32),
        "Da": np.array((45, 145, 235), dtype=np.float32),
        "Vi": np.array((240, 165, 45), dtype=np.float32),
    }
    counts: dict[str, int] = {}
    for attribute, palette in palettes.items():
        distance = np.sqrt(np.sum((crop - palette) ** 2, axis=2))
        counts[attribute] = int(np.count_nonzero(distance < 82.0))
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    attribute, count = ranked[0]
    runner_up = ranked[1][1]
    if count < 12 or count < runner_up * 1.25:
        raise ValueError(f"recommendation color is not decisive: {counts}")
    confidence = min(1.0, (count - runner_up) / max(20.0, float(count)))
    return attribute, confidence


def _option_is_highlighted(
    screen: np.ndarray,
    box: tuple[int, int, int, int],
) -> bool:
    """Detect the saturated full-row fill used after the first selection click."""

    left, top, right, bottom = box
    interior = screen[top + 10 : bottom - 10, left + 100 : right - 50]
    if interior.size == 0:
        return False
    saturation = interior.max(axis=2) - interior.min(axis=2)
    return float(np.mean(saturation > 45)) >= 0.5


def _validate_training_choice_geometry(options: list[TrainingOption]) -> None:
    """Require the fixed three-row layout before treating a frame as training.

    Recommendation colours and generic rounded-card templates also occur on
    reward, lesson, and result screens.  The live training screen is instead
    identified by three distinct rows in its stable Vo/Da/Vi order.
    """

    by_attribute = {option.attribute: option for option in options}
    if set(by_attribute) != set(TEMPLATE_FILES) or len(options) != 3:
        raise ValueError("screen does not have the three training-choice rows")
    rows = tuple(by_attribute[attribute] for attribute in ("Vo", "Da", "Vi"))
    tops = tuple(option.canonical_box[1] for option in rows)
    # Measured from the genuine captured training screen and retained broad
    # enough for the existing synthetic fixture's 690/800/910 layout.
    expected_ranges = ((620, 720), (720, 830), (820, 930))
    if any(not low <= top <= high for top, (low, high) in zip(tops, expected_ranges)):
        raise ValueError(f"training-choice rows outside fixed geometry: {tops}")
    gaps = (tops[1] - tops[0], tops[2] - tops[1])
    if any(gap < 75 or gap > 130 for gap in gaps):
        raise ValueError(f"training-choice row spacing is invalid: {gaps}")


def analyze_training_choice(
    image: Image.Image,
    *,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
    minimum_option_score: float = 0.72,
) -> TrainingChoiceState:
    """Identify visible training options and the game's colored recommendation."""

    screen = _canonical_rgb(image)
    options: list[TrainingOption] = []
    for attribute, filename in TEMPLATE_FILES.items():
        path = template_dir.resolve() / filename
        if not path.is_file():
            raise FileNotFoundError(f"Maa training option template is missing: {path}")
        with Image.open(path) as template:
            template.load()
            left, top, score = _best_template_match(screen, template)
            width, height = template.size
        if score >= minimum_option_score:
            box = (left, top, left + width, top + height)
            options.append(
                TrainingOption(
                    attribute=attribute,
                    label=ATTRIBUTE_LABELS[attribute],
                    canonical_box=box,
                    match_score=score,
                    highlighted=_option_is_highlighted(screen, box),
                )
            )
    if len(options) < 2:
        scores = {option.attribute: option.match_score for option in options}
        raise ValueError(f"screen does not contain enough training options: {scores}")
    _validate_training_choice_geometry(options)
    options.sort(key=lambda option: option.canonical_box[1])
    highlighted = [option for option in options if option.highlighted]
    if len(highlighted) == 1:
        # Once the game has expanded one row, that row is the selected target;
        # animated arrows can contaminate the speech-bubble color classifier.
        recommendation = highlighted[0].attribute
        confidence = 1.0
    else:
        try:
            recommendation, confidence = _recommendation_color(screen)
        except ValueError:
            # Mandatory/focused lesson screens can contain the same stable
            # three Vo/Da/Vi rows without a coloured recommendation sentence.
            # Row geometry is sufficient to bind a policy-selected click.
            recommendation, confidence = None, 0.0
    state = TrainingChoiceState(recommendation, confidence, tuple(options))
    if recommendation is not None:
        # A visible recommendation still has to name one of the proven rows.
        state.recommended_option
    return state


def analyze_training_choice_path(path: Path, **kwargs: object) -> TrainingChoiceState:
    with Image.open(path.resolve()) as image:
        image.load()
        return analyze_training_choice(image, **kwargs)  # type: ignore[arg-type]
