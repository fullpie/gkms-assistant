"""Official card-timing preferences from the local auto-play Master table."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = (
    PROJECT_ROOT
    / "_research"
    / "gakumasu-diff"
    / "ProduceExamAutoPlayCardEvaluation.yaml"
)
MAX_MASTER_REMAINING_TERM = 7


@lru_cache(maxsize=4)
def load_auto_play_card_evaluations(
    source: Path = DEFAULT_SOURCE,
) -> dict[tuple[str, int], int]:
    """Load the game's per-card, per-remaining-turn timing adjustment."""

    resolved = source.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"auto-play evaluation Master not found: {resolved}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    rows = yaml.load(resolved.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(rows, list):
        raise ValueError("ProduceExamAutoPlayCardEvaluation Master must be a list")

    result: dict[tuple[str, int], int] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"auto-play evaluation row {index} is not an object")
        card_id = row.get("produceCardId")
        remaining = row.get("remainingTerm")
        evaluation = row.get("evaluation")
        if (
            not isinstance(card_id, str)
            or not card_id
            or not isinstance(remaining, int)
            or isinstance(remaining, bool)
            or remaining < 1
            or not isinstance(evaluation, int)
            or isinstance(evaluation, bool)
        ):
            raise ValueError(f"invalid auto-play evaluation row {index}")
        key = (card_id, remaining)
        if key in result:
            raise ValueError(f"duplicate auto-play evaluation key: {key}")
        result[key] = evaluation
    return result


def auto_play_card_evaluation(
    card_id: str,
    turns_remaining: int,
    *,
    source: Path = DEFAULT_SOURCE,
) -> int:
    """Return the official timing bias, clamping long exams to term seven."""

    if turns_remaining < 1:
        raise ValueError("turns_remaining must be positive")
    term = min(turns_remaining, MAX_MASTER_REMAINING_TERM)
    return load_auto_play_card_evaluations(source).get((card_id, term), 0)
