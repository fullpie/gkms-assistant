"""Typed N.I.A. week gates and selected-action continuation families.

The Pro route is the existing common player-facing schedule, with the
universal Rest candidate corroborated by live runs.  The Master route comes
from complete official 3.3.0 outer histories.  These gates constrain only the
weekly action family; reward pages remain effect-driven and are never invented
from the week alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .audition_rules import FINAL, MID1, MID2
from .overview_actions import (
    ACTIVITY,
    DANCE_LESSON,
    REST,
    VOCAL_LESSON,
    VISUAL_LESSON,
)
from .route_calendar import BUSINESS, CONSULTATION, OUTING, SPECIAL_GUIDANCE


SOURCE_APP_VERSION: Final = "3.3.0"
SOURCE_ARTIFACT_PATH: Final = (
    "var/leaderboard_dataset/v330_nia_five_archetype_all_modes_v1/"
    "outer_schedules.jsonl"
)
SOURCE_ARTIFACT_SHA256: Final = (
    "1460c24f8063c49d696f293922e897391e5725fc26aed23659cfca04072739aa"
)
_LESSON_OR_REST_ACTIONS: Final = (
    VOCAL_LESSON,
    DANCE_LESSON,
    VISUAL_LESSON,
    REST,
)


@dataclass(frozen=True, slots=True)
class NiaWeekGate:
    produce_id: str
    week: int
    actions: tuple[str, ...] = ()
    exam_stage: str | None = None
    source: str = "mode-calendar+official-3.3.0-outer-history"
    source_app_version: str = SOURCE_APP_VERSION
    source_artifact_sha256: str = SOURCE_ARTIFACT_SHA256


def _with_rest(*actions: str) -> tuple[str, ...]:
    return (*actions, REST)


_PRO: Final = (
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _with_rest(ACTIVITY, OUTING),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _with_rest(OUTING, CONSULTATION),
    _with_rest(BUSINESS),
    _with_rest(SPECIAL_GUIDANCE),
    MID1,
    _with_rest(ACTIVITY, OUTING),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _with_rest(ACTIVITY, OUTING, CONSULTATION),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(SPECIAL_GUIDANCE),
    MID2,
    _with_rest(ACTIVITY, OUTING),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _with_rest(ACTIVITY, CONSULTATION, SPECIAL_GUIDANCE),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(OUTING, CONSULTATION, SPECIAL_GUIDANCE),
    FINAL,
)

_MASTER: Final = (
    *_PRO[:15],
    _with_rest(SPECIAL_GUIDANCE),
    MID2,
    _with_rest(ACTIVITY, OUTING),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _with_rest(ACTIVITY, CONSULTATION, SPECIAL_GUIDANCE),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(BUSINESS),
    _LESSON_OR_REST_ACTIONS,
    _with_rest(OUTING, CONSULTATION, SPECIAL_GUIDANCE),
    FINAL,
)

_SCHEDULES: Final = {
    "produce-004": _PRO,
    "produce-005": _MASTER,
}


def nia_week_gate(produce_id: str, week: int) -> NiaWeekGate:
    if isinstance(week, bool) or not isinstance(week, int):
        raise TypeError("N.I.A. week must be an integer")
    try:
        schedule = _SCHEDULES[produce_id]
    except KeyError as error:
        raise ValueError(f"unsupported N.I.A. mode: {produce_id!r}") from error
    if not 1 <= week <= len(schedule):
        raise ValueError(f"N.I.A. week must be within 1..{len(schedule)}")
    row = schedule[week - 1]
    if isinstance(row, str):
        return NiaWeekGate(produce_id, week, exam_stage=row)
    return NiaWeekGate(produce_id, week, actions=tuple(row))


def nia_recognition_scope_for_action(action: str | None) -> str | None:
    """Return the fixed Maa recognition family for one submitted action."""

    if action == BUSINESS:
        return "business"
    if action == ACTIVITY:
        return "activity"
    if action == OUTING:
        return "outing"
    if action == CONSULTATION:
        return "consultation"
    if action == SPECIAL_GUIDANCE:
        return "guidance"
    if action in {VOCAL_LESSON, DANCE_LESSON, VISUAL_LESSON}:
        return "lesson"
    if action == REST:
        return "rest"
    return None


__all__ = [
    "NiaWeekGate",
    "SOURCE_APP_VERSION",
    "SOURCE_ARTIFACT_PATH",
    "SOURCE_ARTIFACT_SHA256",
    "nia_recognition_scope_for_action",
    "nia_week_gate",
]
