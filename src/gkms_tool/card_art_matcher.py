"""Match a rendered card tile to artwork extracted from the static cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import numpy as np
from PIL import Image


SIGNATURE_SIZE = 192


@dataclass(frozen=True, slots=True)
class CardArtMatch:
    asset_name: str
    score: float
    runner_up_score: float

    @property
    def margin(self) -> float:
        return self.score - self.runner_up_score


@dataclass(frozen=True, slots=True)
class PreparedCardArtMatcher:
    """Precomputed small signatures for matching many compact deck tiles."""

    asset_names: tuple[str, ...]
    normalized_signatures: np.ndarray
    size: int
    signature: Callable[..., np.ndarray]

    @classmethod
    def build(
        cls,
        candidates: Mapping[str, Path | Image.Image],
        *,
        size: int = 64,
        all_hues: bool = True,
    ) -> "PreparedCardArtMatcher":
        if not candidates:
            raise ValueError("card-art candidate set is empty")
        if size < 16:
            raise ValueError("prepared card-art signature size is too small")
        signature = card_color_signature if all_hues else card_chroma_signature
        names: list[str] = []
        vectors: list[np.ndarray] = []
        for asset_name, source in candidates.items():
            if isinstance(source, Image.Image):
                candidate = source
                close = False
            else:
                candidate = Image.open(source)
                close = True
            try:
                vector = signature(candidate, size=size).reshape(-1)
            finally:
                if close:
                    candidate.close()
            norm = float(np.linalg.norm(vector))
            if norm <= 0:
                continue
            names.append(asset_name)
            vectors.append(vector / norm)
        if not vectors:
            raise ValueError("card-art candidates produced no usable signatures")
        matrix = np.ascontiguousarray(np.stack(vectors).astype(np.float32))
        matrix.setflags(write=False)
        return cls(tuple(names), matrix, size, signature)

    def match(self, observed: Image.Image) -> CardArtMatch:
        vector = self.signature(observed, size=self.size).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if norm <= 0:
            return CardArtMatch(self.asset_names[0], 0.0, 0.0)
        scores = self.normalized_signatures @ (vector / norm)
        order = np.argsort(scores)[::-1]
        best = int(order[0])
        runner_up = float(scores[int(order[1])]) if len(order) > 1 else 0.0
        return CardArtMatch(
            self.asset_names[best],
            float(scores[best]),
            runner_up,
        )


def _safe_shop_mask(size: int = SIGNATURE_SIZE) -> np.ndarray:
    y, x = np.mgrid[:size, :size]
    scale = size / SIGNATURE_SIZE
    return (
        (x > 12 * scale)
        & (x < 180 * scale)
        & (y > 8 * scale)
        & (y < 180 * scale)
        & ~((x > 130 * scale) & (y < 80 * scale))
        & ~((x < 65 * scale) & (y > 125 * scale))
        & ~((x > 130 * scale) & (y > 125 * scale))
    )


def card_chroma_signature(
    image: Image.Image, *, size: int = SIGNATURE_SIZE
) -> np.ndarray:
    """Keep orange/purple glyph structure while suppressing pale card UI."""

    rgb = np.asarray(
        image.convert("RGB").resize((size, size), Image.Resampling.LANCZOS),
        dtype=np.float32,
    ) / 255.0
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    orange = np.maximum(0.0, red - (green + blue) / 2.0)
    purple = np.maximum(0.0, (red + blue) / 2.0 - green)
    return np.maximum(orange, purple) * _safe_shop_mask(size)


def card_color_signature(
    image: Image.Image, *, size: int = SIGNATURE_SIZE
) -> np.ndarray:
    """Keep artwork of every hue while suppressing the pale card background.

    The original matcher intentionally emphasized orange and purple because it
    was calibrated on full-size lesson/shop cards.  Reward tiles are much
    smaller and some character cards (for example ``開花``) are cyan, green,
    and yellow.  Per-pixel chroma preserves those shapes without letting the
    white card frame dominate the comparison.  A weak darkness channel keeps
    neutral line art useful as a secondary signal.
    """

    rgb = np.asarray(
        image.convert("RGB").resize((size, size), Image.Resampling.LANCZOS),
        dtype=np.float32,
    ) / 255.0
    luminance = np.mean(rgb, axis=2, keepdims=True)
    chroma = rgb - luminance
    darkness = np.maximum(0.0, 0.93 - luminance) * 0.22
    features = np.concatenate((chroma, darkness), axis=2)
    return features * _safe_shop_mask(size)[:, :, None]


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.sqrt(np.sum(left * left) * np.sum(right * right)))
    return float(np.sum(left * right) / denominator) if denominator else 0.0


def match_card_art(
    observed: Image.Image,
    candidates: Mapping[str, Path | Image.Image],
) -> CardArtMatch:
    return _match_card_art_with_signature(
        observed,
        candidates,
        signature=card_chroma_signature,
    )


def match_card_art_color(
    observed: Image.Image,
    candidates: Mapping[str, Path | Image.Image],
) -> CardArtMatch:
    """Match a compact/rainbow reward tile using the all-hue signature."""

    return _match_card_art_with_signature(
        observed,
        candidates,
        signature=card_color_signature,
    )


def _match_card_art_with_signature(
    observed: Image.Image,
    candidates: Mapping[str, Path | Image.Image],
    *,
    signature,
) -> CardArtMatch:
    if not candidates:
        raise ValueError("card-art candidate set is empty")
    observed_signature = signature(observed)
    ranked: list[tuple[float, str]] = []
    for asset_name, source in candidates.items():
        if isinstance(source, Image.Image):
            candidate = source
            close = False
        else:
            candidate = Image.open(source)
            close = True
        try:
            score = _cosine(observed_signature, signature(candidate))
        finally:
            if close:
                candidate.close()
        ranked.append((score, asset_name))
    ranked.sort(reverse=True)
    best_score, best_name = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    return CardArtMatch(best_name, best_score, runner_up)
