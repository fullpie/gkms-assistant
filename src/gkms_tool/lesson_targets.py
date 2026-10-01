"""Static score gates for the currently fingerprinted live lesson."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LESSON_LEVEL_SOURCE = (
    PROJECT_ROOT / "_research" / "gakumasu-diff" / "ProduceStepLessonLevel.yaml"
)
DEFAULT_LESSON_SOURCE = (
    PROJECT_ROOT / "_research" / "gakumasu-diff" / "ProduceStepLesson.yaml"
)
LIVE_LESSON_LEVEL_ID = "p_step_lesson_level-002-plan2-hard-001"

_FIRSTSTAR_PURSUIT_ID = re.compile(
    r"^p_step_lesson_level-(?P<series>\d+)-(?P<character>[a-z0-9]+)"
    r"-hard-(?P<attribute>vo|da|vi)-(?P<lesson_number>001)$"
)


@dataclass(frozen=True, slots=True)
class LessonScoreTargets:
    lesson_level_id: str
    limit_turn: int
    clear: int
    perfect: int

    def for_tier(self, tier: str) -> int:
        if tier == "clear":
            return self.clear
        if tier == "perfect":
            return self.perfect
        raise ValueError(f"unsupported lesson score tier: {tier}")


@dataclass(frozen=True, slots=True)
class FirstStarPursuitLesson:
    """A selected FirstStar hard-lesson row and its authoritative score gates.

    ``series`` intentionally remains part of the identity.  The local Master
    contains both v001 (75/300) and v002 (90/360) rows; production mode alone
    does not establish which one a live selection is using.
    """

    lesson_id: str
    lesson_level_id: str
    character_id: str
    attribute: str
    series: str
    targets: LessonScoreTargets


@lru_cache(maxsize=2)
def _lesson_level_rows(
    source: Path = DEFAULT_LESSON_LEVEL_SOURCE,
) -> tuple[dict, ...]:
    if not source.is_file():
        raise FileNotFoundError(f"lesson threshold Master is missing: {source}")
    rows = yaml.load(source.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("ProduceStepLessonLevel Master must be a list of objects")
    return tuple(rows)


@lru_cache(maxsize=2)
def _lesson_rows(source: Path = DEFAULT_LESSON_SOURCE) -> tuple[dict, ...]:
    if not source.is_file():
        raise FileNotFoundError(f"lesson Master is missing: {source}")
    rows = yaml.load(source.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("ProduceStepLesson Master must be a list of objects")
    return tuple(rows)


def load_firststar_pursuit_lesson(
    lesson_id: str,
    *,
    lesson_source: Path = DEFAULT_LESSON_SOURCE,
    level_source: Path = DEFAULT_LESSON_LEVEL_SOURCE,
) -> FirstStarPursuitLesson:
    """Resolve one explicitly identified FirstStar Mid1 hard-lesson choice.

    This rejects normal/SP rows, another lesson number, malformed IDs, and
    rows whose level identity does not agree with the selected lesson.  It
    deliberately does not infer a v001/v002 choice from ``produce-001``.
    """

    match = _FIRSTSTAR_PURSUIT_ID.fullmatch(lesson_id)
    if match is None:
        raise ValueError(f"unsupported FirstStar pursuit lesson: {lesson_id}")
    selected = [row for row in _lesson_rows(lesson_source) if row.get("id") == lesson_id]
    if len(selected) != 1:
        raise KeyError(f"FirstStar pursuit lesson not found uniquely: {lesson_id}")
    level_id = selected[0].get("produceStepLessonLevelId")
    if not isinstance(level_id, str):
        raise ValueError(f"invalid lesson level ID: {lesson_id}")
    expected_prefix = f"p_step_lesson_level-{match['series']}-plan2-hard-"
    if not level_id.startswith(expected_prefix):
        raise ValueError(f"lesson/level identity mismatch: {lesson_id}:{level_id}")
    targets = load_lesson_score_targets(level_id, level_source)
    return FirstStarPursuitLesson(
        lesson_id=lesson_id,
        lesson_level_id=level_id,
        character_id=match["character"],
        attribute=match["attribute"],
        series=match["series"],
        targets=targets,
    )


def infer_lesson_score_targets(
    visible_threshold: int,
    *,
    observed_limit_turn: int | None = None,
    source: Path = DEFAULT_LESSON_LEVEL_SOURCE,
) -> LessonScoreTargets:
    """Infer a unique CLEAR/PERFECT pair from the visible lesson HUD.

    Multiple character and lesson IDs can share identical numeric rules. They
    are collapsed only when all matching rows agree. The observed turn count
    is retained because support effects can add turns beyond the base limit.
    """

    if visible_threshold <= 0:
        raise ValueError("visible lesson threshold must be positive")
    rows = _lesson_level_rows(source)
    matching = [
        row for row in rows if int(row.get("successThreshold", 0)) == visible_threshold
    ]
    if not matching:
        matching = [
            row
            for row in rows
            if int(row.get("resultTargetValueLimit", 0)) == visible_threshold
        ]
    if not matching:
        raise KeyError(f"no lesson target matches visible threshold {visible_threshold}")
    if observed_limit_turn is not None:
        exact_turn_rows = [
            row
            for row in matching
            if int(row.get("limitTurn", 0)) == int(observed_limit_turn)
        ]
        if exact_turn_rows:
            matching = exact_turn_rows
    pairs = {
        (
            int(row.get("successThreshold", 0)),
            int(row.get("resultTargetValueLimit", 0)),
        )
        for row in matching
    }
    if len(pairs) != 1:
        raise ValueError(
            f"visible lesson threshold {visible_threshold} is ambiguous: {sorted(pairs)}"
        )
    clear, perfect = next(iter(pairs))
    base_limits = {int(row.get("limitTurn", 0)) for row in matching}
    if any(limit < 1 for limit in base_limits):
        raise ValueError("matching lesson target has an invalid turn limit")
    effective_limit = (
        int(observed_limit_turn)
        if observed_limit_turn is not None
        else max(base_limits)
    )
    if effective_limit < 1:
        raise ValueError("observed lesson turn limit must be positive")
    aliases = sorted(str(row.get("id", "")) for row in matching)
    return LessonScoreTargets(
        lesson_level_id="inferred:" + ",".join(aliases),
        limit_turn=effective_limit,
        clear=clear,
        perfect=perfect,
    )


@lru_cache(maxsize=8)
def load_lesson_score_targets(
    lesson_level_id: str = LIVE_LESSON_LEVEL_ID,
    source: Path = DEFAULT_LESSON_LEVEL_SOURCE,
) -> LessonScoreTargets:
    """Load CLEAR and PERFECT absolute thresholds from local Master YAML."""

    if not source.is_file():
        raise FileNotFoundError(f"找不到課程門檻 Master：{source}")
    rows = yaml.load(source.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
    if not isinstance(rows, list):
        raise ValueError("ProduceStepLessonLevel Master 不是 list")
    selected = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("id") == lesson_level_id
    ]
    if len(selected) != 1:
        raise KeyError(
            f"Master 的課程門檻 ID 必須恰有一筆：{lesson_level_id}（{len(selected)}）"
        )
    row = selected[0]
    targets = LessonScoreTargets(
        lesson_level_id=lesson_level_id,
        limit_turn=int(row.get("limitTurn", 0)),
        clear=int(row.get("successThreshold", 0)),
        perfect=int(row.get("resultTargetValueLimit", 0)),
    )
    if targets.limit_turn < 1 or not (0 < targets.clear < targets.perfect):
        raise ValueError(f"課程門檻 Master 數值無效：{lesson_level_id}")
    return targets
