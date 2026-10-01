"""OCR one visible weekly activity-reward checkpoint.

The reward overlay is useful to the shadow state because it exposes the
post-action stamina and Produce Point totals plus the newly acquired item.
The item description is recorded as metadata; it is not applied to stamina or
another stat until the item is actually consumed.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from PIL import Image

from .screen_state import scale_canonical_box
from .text_recognizer import PaddleLineRecognizer, TextRecognition


ACTIVITY_REWARD_REGIONS: Mapping[str, tuple[int, int, int, int]] = {
    "header": (35, 45, 150, 95),
    "stamina": (285, 45, 440, 105),
    "points": (335, 95, 445, 150),
    # Exclude the small drink glyph immediately left of the item name.
    "item": (315, 835, 460, 905),
    "effect": (70, 900, 270, 965),
}

# The lesson reward uses the same fixed HUD and reward-description rows as the
# weekly activity overlay, but its upper-left tile is a training tile rather
# than ``活動獎勵``.  Keep it as a distinct contract: the earned Produce Point
# delta and the acquired drink are both independently persisted by the outer
# Produce log before this reader is allowed to authorize input.
TRAINING_DRINK_REWARD_REGIONS: Mapping[str, tuple[int, int, int, int]] = {
    "stamina": ACTIVITY_REWARD_REGIONS["stamina"],
    "points": ACTIVITY_REWARD_REGIONS["points"],
    "earned_points": (535, 735, 660, 815),
    "item": ACTIVITY_REWARD_REGIONS["item"],
    "effect": ACTIVITY_REWARD_REGIONS["effect"],
}


@dataclass(frozen=True, slots=True)
class ActivityRewardState:
    item_name: str
    effect_name: str
    effect_value: int
    stamina: int
    max_stamina: int
    produce_points: int
    confidence: float
    raw_text: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def shadow_patch(self) -> dict[str, object]:
        """Return only facts that this overlay authoritatively exposes."""

        return {
            "stamina": self.stamina,
            "max_stamina": self.max_stamina,
            "produce_points": self.produce_points,
            "inventory_add": {self.item_name: 1},
            "item_effect": {
                "name": self.effect_name,
                "value": self.effect_value,
                "applied": False,
            },
        }


@dataclass(frozen=True, slots=True)
class TrainingDrinkRewardState:
    """Visible lesson reward facts; Master/LocalSave identity is joined later."""

    item_name: str
    effect_name: str
    effect_value: int
    stamina: int
    max_stamina: int
    produce_points: int
    earned_produce_points: int
    confidence: float
    raw_text: Mapping[str, str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).replace("＋", "+").replace("－", "-")


def parse_activity_reward_text(
    recognized: Mapping[str, TextRecognition],
) -> ActivityRewardState:
    missing = set(ACTIVITY_REWARD_REGIONS) - set(recognized)
    if missing:
        raise ValueError(f"missing activity reward OCR fields: {sorted(missing)}")

    header = _normalize(recognized["header"].text)
    if "活動" not in header or not any(word in header for word in ("獎勵", "報酬")):
        raise ValueError(f"not an activity reward overlay: {header!r}")

    stamina_text = _normalize(recognized["stamina"].text)
    stamina_match = re.search(r"(\d+)[/／](\d+)", stamina_text)
    if stamina_match is None:
        raise ValueError(f"stamina OCR is not current/max: {stamina_text!r}")
    stamina, max_stamina = map(int, stamina_match.groups())
    if max_stamina < 1 or not 0 <= stamina <= max_stamina:
        raise ValueError(f"invalid stamina values: {stamina}/{max_stamina}")

    points_match = re.search(r"\d+", _normalize(recognized["points"].text))
    if points_match is None:
        raise ValueError(f"points OCR is not an integer: {recognized['points'].text!r}")

    item_name = _normalize(recognized["item"].text)
    if not item_name:
        raise ValueError("activity reward item name is empty")

    effect_text = _normalize(recognized["effect"].text)
    effect_match = re.fullmatch(r"(.+?)([+-])(\d+)", effect_text)
    if effect_match is None:
        raise ValueError(f"activity reward effect is not NAME+VALUE: {effect_text!r}")
    effect_name, sign, raw_value = effect_match.groups()
    effect_value = int(raw_value) * (1 if sign == "+" else -1)

    confidences = [recognized[key].confidence for key in ACTIVITY_REWARD_REGIONS]
    return ActivityRewardState(
        item_name=item_name,
        effect_name=effect_name,
        effect_value=effect_value,
        stamina=stamina,
        max_stamina=max_stamina,
        produce_points=int(points_match.group()),
        confidence=sum(confidences) / len(confidences),
        raw_text={key: recognized[key].text for key in ACTIVITY_REWARD_REGIONS},
    )


def _parse_common_reward_values(
    recognized: Mapping[str, TextRecognition],
    *,
    expected_fields: frozenset[str],
    label: str,
) -> tuple[int, int, int, str, str, int]:
    missing = expected_fields - set(recognized)
    if missing:
        raise ValueError(f"missing {label} OCR fields: {sorted(missing)}")

    stamina_text = _normalize(recognized["stamina"].text)
    stamina_match = re.search(r"(\d+)[/嚗(](\d+)", stamina_text)
    if stamina_match is None:
        raise ValueError(f"stamina OCR is not current/max: {stamina_text!r}")
    stamina, max_stamina = map(int, stamina_match.groups())
    if max_stamina < 1 or not 0 <= stamina <= max_stamina:
        raise ValueError(f"invalid stamina values: {stamina}/{max_stamina}")

    points_match = re.search(r"\d+", _normalize(recognized["points"].text))
    if points_match is None:
        raise ValueError(f"points OCR is not an integer: {recognized['points'].text!r}")

    item_name = _normalize(recognized["item"].text)
    if not item_name:
        raise ValueError(f"{label} item name is empty")

    effect_text = _normalize(recognized["effect"].text)
    effect_match = re.fullmatch(r"(.+?)([+-])(\d+)", effect_text)
    if effect_match is None:
        raise ValueError(f"{label} effect is not NAME+VALUE: {effect_text!r}")
    effect_name, sign, raw_value = effect_match.groups()
    effect_value = int(raw_value) * (1 if sign == "+" else -1)
    return (
        stamina,
        max_stamina,
        int(points_match.group()),
        item_name,
        effect_name,
        effect_value,
    )


def parse_training_drink_reward_text(
    recognized: Mapping[str, TextRecognition],
) -> TrainingDrinkRewardState:
    """Parse the fixed lesson-reward fields without trusting its animated title."""

    fields = frozenset(TRAINING_DRINK_REWARD_REGIONS)
    (
        stamina,
        max_stamina,
        produce_points,
        item_name,
        effect_name,
        effect_value,
    ) = _parse_common_reward_values(
        recognized,
        expected_fields=fields,
        label="training drink reward",
    )
    earned_match = re.fullmatch(
        r"\d+", _normalize(recognized["earned_points"].text)
    )
    if earned_match is None:
        raise ValueError(
            "training reward earned points OCR is not an integer: "
            f"{recognized['earned_points'].text!r}"
        )
    earned_points = int(earned_match.group())
    if earned_points < 1:
        raise ValueError("training reward earned points must be positive")
    confidences = [recognized[key].confidence for key in fields]
    return TrainingDrinkRewardState(
        item_name=item_name,
        effect_name=effect_name,
        effect_value=effect_value,
        stamina=stamina,
        max_stamina=max_stamina,
        produce_points=produce_points,
        earned_produce_points=earned_points,
        confidence=sum(confidences) / len(confidences),
        raw_text={key: recognized[key].text for key in fields},
    )


def read_activity_reward(
    image: Image.Image,
    recognizer: PaddleLineRecognizer | None = None,
) -> ActivityRewardState:
    recognizer = recognizer or PaddleLineRecognizer()
    recognized = {
        key: recognizer.recognize(
            image.crop(scale_canonical_box(box, image.width, image.height))
        )
        for key, box in ACTIVITY_REWARD_REGIONS.items()
    }
    return parse_activity_reward_text(recognized)


def read_activity_reward_path(
    path: Path,
    recognizer: PaddleLineRecognizer | None = None,
) -> ActivityRewardState:
    with Image.open(path.resolve()) as image:
        image.load()
        return read_activity_reward(image, recognizer)


def read_training_drink_reward(
    image: Image.Image,
    recognizer: PaddleLineRecognizer | None = None,
) -> TrainingDrinkRewardState:
    recognizer = recognizer or PaddleLineRecognizer()
    recognized = {
        key: recognizer.recognize(
            image.crop(scale_canonical_box(box, image.width, image.height))
        )
        for key, box in TRAINING_DRINK_REWARD_REGIONS.items()
    }
    return parse_training_drink_reward_text(recognized)


def read_training_drink_reward_path(
    path: Path,
    recognizer: PaddleLineRecognizer | None = None,
) -> TrainingDrinkRewardState:
    with Image.open(path.resolve()) as image:
        image.load()
        return read_training_drink_reward(image, recognizer)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Read a weekly activity reward overlay")
    parser.add_argument("image", type=Path)
    arguments = parser.parse_args()
    state = read_activity_reward_path(arguments.image)
    print(
        json.dumps(
            {"state": state.to_dict(), "shadow_patch": state.shadow_patch()},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
