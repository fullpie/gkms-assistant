"""Screenshot-only analysis for the First Produce consultation shop."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from PIL import Image

from .card_art_matcher import CardArtMatch, match_card_art
from .card_identity import CardNameCatalog
from .octo_assets import DEFAULT_CARD_ART_CACHE, OctoAssetIndex
from .screen_state import scale_canonical_box
from .text_recognizer import PaddleLineRecognizer, TextRecognition


# Maa's pipelines use a 720 x 1280 canonical frame.  These boxes were measured
# from the same physical shop frame and scale cleanly to either 540p or 1080p.
SHOP_CARD_BOXES: tuple[tuple[int, int, int, int], ...] = (
    (64, 482, 194, 612),
    (219, 482, 348, 612),
    (374, 482, 503, 612),
    (529, 482, 658, 612),
)
SHOP_PRICE_BOXES: tuple[tuple[int, int, int, int], ...] = (
    (55, 612, 200, 655),
    (215, 612, 355, 655),
    (368, 612, 510, 655),
    (523, 612, 663, 655),
)
SHOP_POINTS_BOX = (345, 88, 455, 143)


@dataclass(frozen=True, slots=True)
class ShopCardOffer:
    slot: int
    asset_name: str
    card_id: str
    display_name: str
    price: int
    price_text: str
    art_score: float
    art_margin: float
    price_confidence: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ShopState:
    produce_points: int
    points_confidence: float
    offers: tuple[ShopCardOffer, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def parse_price_text(text: str) -> int:
    numbers = re.findall(r"\d+", text)
    if not numbers:
        raise ValueError(f"shop price OCR has no integer: {text!r}")
    # Sale rows contain both the struck-through original and current price.
    return int(numbers[-1])


def _resolve_card(
    asset_name: str,
    catalog: CardNameCatalog,
) -> tuple[str, str]:
    entry = catalog.resolve_asset(asset_name)
    return entry.card_id, entry.display_name


def _recognize_line(
    image: Image.Image,
    box: tuple[int, int, int, int],
    recognizer: PaddleLineRecognizer,
) -> TextRecognition:
    return recognizer.recognize(
        image.crop(scale_canonical_box(box, image.width, image.height))
    )


def read_shop_state(
    image: Image.Image,
    *,
    card_art: Mapping[str, Path | Image.Image] | None = None,
    recognizer: PaddleLineRecognizer | None = None,
    catalog: CardNameCatalog | None = None,
    octo_index: OctoAssetIndex | None = None,
) -> ShopState:
    recognizer = recognizer or PaddleLineRecognizer()
    catalog = catalog or CardNameCatalog.load()
    if card_art is None:
        index = octo_index or OctoAssetIndex.load()
        card_art = index.ensure_generic_card_art(DEFAULT_CARD_ART_CACHE)

    points_result = _recognize_line(image, SHOP_POINTS_BOX, recognizer)
    produce_points = parse_price_text(points_result.text)
    offers: list[ShopCardOffer] = []
    for slot, (card_box, price_box) in enumerate(
        zip(SHOP_CARD_BOXES, SHOP_PRICE_BOXES), 1
    ):
        rendered_card = image.crop(
            scale_canonical_box(card_box, image.width, image.height)
        )
        art_match: CardArtMatch = match_card_art(rendered_card, card_art)
        card_id, display_name = _resolve_card(art_match.asset_name, catalog)
        price_result = _recognize_line(image, price_box, recognizer)
        offers.append(
            ShopCardOffer(
                slot=slot,
                asset_name=art_match.asset_name,
                card_id=card_id,
                display_name=display_name,
                price=parse_price_text(price_result.text),
                price_text=price_result.text,
                art_score=art_match.score,
                art_margin=art_match.margin,
                price_confidence=price_result.confidence,
            )
        )
    return ShopState(
        produce_points=produce_points,
        points_confidence=points_result.confidence,
        offers=tuple(offers),
    )


def read_shop_path(path: Path, **kwargs: object) -> ShopState:
    with Image.open(path.resolve()) as image:
        image.load()
        return read_shop_state(image, **kwargs)  # type: ignore[arg-type]


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Read a First Produce shop screenshot")
    parser.add_argument("image", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            read_shop_path(arguments.image).to_dict(),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
