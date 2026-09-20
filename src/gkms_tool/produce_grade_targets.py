"""Master-backed Produce grade targets and per-attribute caps.

The thresholds in this module are authoritative Master facts.  They are final
rating-point thresholds, not raw attribute requirements.  The component
formula that turns parameters, an exam result, fan votes, or HIF stars into
rating points is intentionally not hard-coded here: those formulas are
community-derived and can change independently of the Master threshold table.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import yaml

from .route_calendar import DEFAULT_MASTER_DIR


_GRADE_LABELS = {
    "ResultGrade_F": "F",
    "ResultGrade_E": "E",
    "ResultGrade_D": "D",
    "ResultGrade_C": "C",
    "ResultGrade_CPlus": "C+",
    "ResultGrade_B": "B",
    "ResultGrade_BPlus": "B+",
    "ResultGrade_A": "A",
    "ResultGrade_APlus": "A+",
    "ResultGrade_S": "S",
    "ResultGrade_SPlus": "S+",
    "ResultGrade_Ss": "SS",
    "ResultGrade_SsPlus": "SS+",
    "ResultGrade_Sss": "SSS",
    "ResultGrade_SssPlus": "SSS+",
    "ResultGrade_Ssss": "S4",
    "ResultGrade_SsssPlus": "S4+",
    "ResultGrade_Sssss": "S5",
}


@dataclass(frozen=True, slots=True)
class ProduceGradeThreshold:
    grade: str
    master_grade: str
    rating_points: int


@dataclass(frozen=True, slots=True)
class ProduceGradeTargets:
    produce_id: str
    produce_group_id: str
    produce_type: str
    maximum_grade: str
    attribute_cap: int
    thresholds: tuple[ProduceGradeThreshold, ...]

    def rating_points_for(self, grade: str) -> int:
        matches = tuple(row.rating_points for row in self.thresholds if row.grade == grade)
        if len(matches) != 1:
            raise ValueError(f"grade is unavailable for {self.produce_id}: {grade}")
        return matches[0]

    @property
    def headline_targets(self) -> Mapping[str, int]:
        return {
            grade: self.rating_points_for(grade)
            for grade in ("S", "SS", "SSS")
        }


def _rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or any(not isinstance(row, Mapping) for row in payload):
        raise ValueError(f"Master file must contain mapping rows: {path.name}")
    return tuple(payload)


@lru_cache(maxsize=16)
def _load_cached(produce_id: str, master_dir: Path) -> ProduceGradeTargets:
    produce_matches = tuple(
        row for row in _rows(master_dir / "Produce.yaml") if row.get("id") == produce_id
    )
    if len(produce_matches) != 1:
        raise ValueError(f"Produce must resolve exactly once: {produce_id}")
    produce = produce_matches[0]
    cap = produce.get("idolCardParameterGrowthLimit")
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError(f"Produce attribute cap is invalid: {produce_id}")

    group_matches = tuple(
        row
        for row in _rows(master_dir / "ProduceGroup.yaml")
        if isinstance(row.get("produceIds"), list) and produce_id in row["produceIds"]
    )
    if len(group_matches) != 1:
        raise ValueError(f"ProduceGroup must resolve exactly once: {produce_id}")
    group = group_matches[0]
    group_id = group.get("id")
    group_type = group.get("type")
    limit_grade = group.get("limitGrade")
    if not all(isinstance(value, str) and value for value in (group_id, group_type, limit_grade)):
        raise ValueError(f"ProduceGroup identity is incomplete: {produce_id}")
    if limit_grade not in _GRADE_LABELS:
        raise ValueError(f"unknown maximum grade: {limit_grade}")

    grade_rows = tuple(
        row
        for row in _rows(master_dir / "ProduceGrade.yaml")
        if row.get("produceGroupId") == group_id
    )
    thresholds: list[ProduceGradeThreshold] = []
    for row in grade_rows:
        master_grade = row.get("grade")
        threshold = row.get("threshold")
        if master_grade not in _GRADE_LABELS:
            raise ValueError(f"unknown grade in {group_id}: {master_grade}")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            raise ValueError(f"invalid grade threshold in {group_id}: {threshold!r}")
        thresholds.append(
            ProduceGradeThreshold(_GRADE_LABELS[master_grade], master_grade, threshold)
        )
    if not thresholds or len({row.grade for row in thresholds}) != len(thresholds):
        raise ValueError(f"ProduceGrade rows are missing or duplicated: {group_id}")
    if any(
        later.rating_points <= earlier.rating_points
        for earlier, later in zip(thresholds, thresholds[1:])
    ):
        raise ValueError(f"ProduceGrade thresholds are not strictly increasing: {group_id}")

    return ProduceGradeTargets(
        produce_id=produce_id,
        produce_group_id=group_id,
        produce_type=group_type,
        maximum_grade=_GRADE_LABELS[limit_grade],
        attribute_cap=cap,
        thresholds=tuple(thresholds),
    )


def load_produce_grade_targets(
    produce_id: str,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> ProduceGradeTargets:
    """Load exact grade thresholds and the per-attribute cap for one mode."""

    if not isinstance(produce_id, str) or not produce_id.strip():
        raise ValueError("produce_id must be non-empty text")
    return _load_cached(produce_id, Path(master_dir).resolve())

