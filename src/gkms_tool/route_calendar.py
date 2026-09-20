"""Offline route calendars for Produce modes.

The local Master is authoritative for mode identity and total step count, but
the client data currently available to this project does not contain the
player-facing weekly action table.  Weekly actions therefore come from a
small, explicitly sourced community-data layer and are validated against the
current Master every time they are loaded.

Visible game state always wins.  A calendar position inferred from the
``final audition in N weeks`` label is a navigation aid, not permission to
click an action whose screen type has not also been recognised.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

import yaml

from .audition_rules import FINAL, MID1, MID2


PROJECT_ROOT = Path(__file__).resolve().parents[2]
from .application_paths import master_directory
DEFAULT_MASTER_DIR = master_directory()

LESSON = "lesson"
CLASS = "class"
SUPPLY = "supply"
OUTING = "outing"
CONSULTATION = "consultation"
CRAM_LESSON = "cram_lesson"
SELF_LESSON = "self_lesson"
BUSINESS = "business"
CARE_PACKAGE = "care_package"
SPECIAL_GUIDANCE = "special_guidance"
EVENT_BUSINESS = "event_business"
FAN_PRESENT = "fan_present"
INTERVAL = "interval"
LEGEND_LESSON = "legend_lesson"

ACTION_LABELS = {
    LESSON: "課程",
    CLASS: "授業",
    SUPPLY: "活動支給",
    OUTING: "外出",
    CONSULTATION: "諮詢",
    CRAM_LESSON: "衝刺課程",
    SELF_LESSON: "自主課程",
    BUSINESS: "營業",
    CARE_PACKAGE: "差入",
    SPECIAL_GUIDANCE: "特別指導",
    EVENT_BUSINESS: "營業事件",
    FAN_PRESENT: "粉絲差入",
    INTERVAL: "間隔／回復",
    LEGEND_LESSON: "Legend 課程",
    # Canonical live N.I.A. tile actions.  RouteCalendar's historical
    # SELF_LESSON/CARE_PACKAGE aliases remain for offline scenarios, while the
    # production FixedWeekGate uses the exact UI action identities below.
    "vocal_lesson": "Vo 自主課程",
    "dance_lesson": "Da 自主課程",
    "visual_lesson": "Vi 自主課程",
    "activity": "活動",
    "rest": "休息",
}

STAGE_LABELS = {
    MID1: "期中／第一次演出",
    MID2: "第二次演出",
    FINAL: "最終演出",
}


@dataclass(frozen=True, slots=True)
class AuditionMilestone:
    week: int
    step_type: str

    @property
    def label(self) -> str:
        return STAGE_LABELS[self.step_type]


@dataclass(frozen=True, slots=True)
class RouteWeek:
    week: int
    actions: tuple[str, ...] = ()
    stage_type: str | None = None
    exact_actions: bool = False

    @property
    def action_labels(self) -> tuple[str, ...]:
        if self.stage_type is not None:
            return (STAGE_LABELS[self.stage_type],)
        return tuple(ACTION_LABELS[action] for action in self.actions)

    @property
    def display(self) -> str:
        return "／".join(self.action_labels) if self.action_labels else "尚未匯入逐週選項"


@dataclass(frozen=True, slots=True)
class RouteCalendar:
    produce_id: str
    scenario_name: str
    difficulty_name: str
    total_weeks: int
    milestones: tuple[AuditionMilestone, ...]
    weeks: tuple[RouteWeek, ...]
    detail_scope: str
    source_note: str

    @property
    def label(self) -> str:
        return f"{self.scenario_name}｜{self.difficulty_name}"

    @property
    def final_week(self) -> int:
        final = next(
            (milestone.week for milestone in self.milestones if milestone.step_type == FINAL),
            None,
        )
        if final is None:
            raise ValueError(f"{self.produce_id} 缺少最終演出節點")
        return final

    def week(self, number: int) -> RouteWeek:
        if not 1 <= number <= self.total_weeks:
            raise ValueError(f"週次必須介於 1 與 {self.total_weeks}")
        return self.weeks[number - 1]

    def next_milestone(self, current_week: int) -> AuditionMilestone | None:
        self.week(current_week)
        return next(
            (milestone for milestone in self.milestones if milestone.week >= current_week),
            None,
        )


@dataclass(frozen=True, slots=True)
class RoutePosition:
    current_week: int
    weeks_until_final: int
    route_week: RouteWeek
    next_milestone: AuditionMilestone

    @property
    def weeks_until_next_milestone(self) -> int:
        return self.next_milestone.week - self.current_week


# These lifecycle nodes are stable mode structure.  The final week is checked
# against Produce.yaml so a game update fails loudly instead of shifting the
# calculator silently.
_MILESTONES: dict[str, tuple[tuple[int, str], ...]] = {
    "produce-001": ((6, MID1), (13, FINAL)),
    "produce-002": ((7, MID1), (16, FINAL)),
    "produce-003": ((8, MID1), (18, FINAL)),
    "produce-004": ((9, MID1), (18, MID2), (27, FINAL)),
    "produce-005": ((9, MID1), (17, MID2), (26, FINAL)),
    "produce-006": ((10, MID1), (18, FINAL)),
}

_MODE_NAMES: dict[str, tuple[str, str]] = {
    "produce-001": ("定期公演『初』", "Regular"),
    "produce-002": ("定期公演『初』", "Pro"),
    "produce-003": ("定期公演『初』", "Master"),
    "produce-004": ("N.I.A", "Pro"),
    "produce-005": ("N.I.A", "Master"),
    "produce-006": ("定期公演『初』", "Legend"),
}


# Character-independent lifecycle rows corroborated by the player-facing
# schedule.  These are intentionally narrower than a complete weekly table:
# ordinary weeks remain live-visible choices, while pursuit lessons and exams
# are fixed route facts.  Keeping the rows partial avoids turning one example
# walkthrough into a brittle click script.
_COMMON_FIXED_ACTIONS: dict[str, dict[int, tuple[str, ...]]] = {
    # Initial Pro: pursuit lessons immediately before the W7 midterm and W16
    # final exam.
    "produce-002": {
        6: (CRAM_LESSON,),
        15: (CRAM_LESSON,),
    },
    # Initial Master: the 18-week route shifts those fixed lessons to W7/W17.
    "produce-003": {
        7: (CRAM_LESSON,),
        17: (CRAM_LESSON,),
    },
}


def _actions(*values: str) -> tuple[str, ...]:
    return values


# 葛城リーリヤ's Regular and Pro calendars are character-level schedules.
# They apply to every P idol card whose characterId is ``kllj``; individual
# card effects and starting decks remain separate Master inputs.
_KLLJ_REGULAR: tuple[tuple[str, ...], ...] = (
    _actions(LESSON),
    _actions(CLASS),
    _actions(LESSON, CLASS),
    _actions(SUPPLY, OUTING, CONSULTATION),
    _actions(CRAM_LESSON),
    (),
    _actions(SUPPLY, OUTING),
    _actions(OUTING, CLASS),
    _actions(LESSON),
    _actions(LESSON, CLASS),
    _actions(SUPPLY, OUTING, CONSULTATION),
    _actions(CRAM_LESSON),
    (),
)

# Two preserved Fujita Kotone Initial Regular runs (overview captures plus
# ProducePlayLogSaveData) match this same 13-week action shape.  Keep the
# mapping character-specific so every other character still fails closed
# until its schedule has comparable evidence.
_FKTN_REGULAR = _KLLJ_REGULAR

_KLLJ_PRO: tuple[tuple[str, ...], ...] = (
    _actions(LESSON),
    _actions(CLASS),
    _actions(SUPPLY, OUTING),
    _actions(LESSON, CLASS),
    _actions(OUTING, CONSULTATION),
    _actions(CRAM_LESSON),
    (),
    _actions(OUTING, CONSULTATION),
    _actions(SUPPLY, CLASS),
    _actions(LESSON),
    _actions(SUPPLY, OUTING, CLASS),
    _actions(LESSON),
    _actions(LESSON, CLASS),
    _actions(SUPPLY, OUTING, CONSULTATION),
    _actions(CRAM_LESSON),
    (),
)

# N.I.A Pro is common to all characters.
_NIA_PRO: tuple[tuple[str, ...], ...] = (
    _actions(SELF_LESSON),
    _actions(BUSINESS),
    _actions(OUTING, CARE_PACKAGE),
    _actions(SELF_LESSON),
    _actions(BUSINESS),
    _actions(OUTING, CONSULTATION),
    _actions(BUSINESS),
    _actions(SPECIAL_GUIDANCE),
    (),
    _actions(OUTING, CARE_PACKAGE),
    _actions(SELF_LESSON),
    _actions(BUSINESS),
    _actions(OUTING, CONSULTATION, CARE_PACKAGE),
    _actions(SELF_LESSON),
    _actions(BUSINESS),
    _actions(SELF_LESSON),
    _actions(SPECIAL_GUIDANCE),
    (),
    _actions(OUTING, CARE_PACKAGE),
    _actions(SELF_LESSON),
    _actions(BUSINESS),
    _actions(CONSULTATION, CARE_PACKAGE, SPECIAL_GUIDANCE),
    _actions(SELF_LESSON),
    _actions(BUSINESS),
    _actions(SELF_LESSON),
    _actions(OUTING, CONSULTATION, SPECIAL_GUIDANCE),
    (),
)

_LEGEND: tuple[tuple[str, ...], ...] = (
    _actions(CLASS),
    _actions(CLASS),
    _actions(OUTING, SUPPLY),
    _actions(LEGEND_LESSON),
    _actions(OUTING, CONSULTATION, SUPPLY),
    _actions(CLASS),
    _actions(LEGEND_LESSON),
    _actions(CONSULTATION),
    _actions(SPECIAL_GUIDANCE),
    (),
    _actions(OUTING, SUPPLY),
    _actions(LEGEND_LESSON),
    _actions(OUTING, CONSULTATION, SUPPLY),
    _actions(LEGEND_LESSON),
    _actions(CLASS),
    _actions(LEGEND_LESSON),
    _actions(CONSULTATION, SPECIAL_GUIDANCE),
    (),
)


@lru_cache(maxsize=8)
def _produce_rows(master_dir: Path) -> tuple[dict[str, Any], ...]:
    path = master_dir / "Produce.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"找不到 Master 表：{path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError("Produce.yaml 根節點不是 list")
    return tuple(row for row in payload if isinstance(row, dict))


def _master_total_weeks(produce_id: str, master_dir: Path) -> int:
    rows = [row for row in _produce_rows(master_dir) if row.get("id") == produce_id]
    if len(rows) != 1:
        raise KeyError(f"{produce_id} 在 Produce.yaml 預期一筆，實際 {len(rows)} 筆")
    total = int(rows[0].get("steps", 0))
    if total < 1:
        raise ValueError(f"{produce_id} 的 Master steps 無效：{total}")
    return total


def _detailed_actions(
    produce_id: str, character_id: str | None
) -> tuple[tuple[tuple[str, ...], ...] | None, str, str]:
    if produce_id == "produce-001" and character_id == "kllj":
        return (
            _KLLJ_REGULAR,
            "character:kllj",
            "葛城リーリヤ公開逐週表；Master 校正總週數與演出規則",
        )
    if produce_id == "produce-001" and character_id == "fktn":
        return (
            _FKTN_REGULAR,
            "character:fktn",
            "FKTN Initial Regular: preserved overview captures plus outer "
            "ProducePlayLogSaveData; total steps are revalidated from Master.",
        )
    if produce_id == "produce-002" and character_id == "kllj":
        return (
            _KLLJ_PRO,
            "character:kllj",
            "葛城リーリヤ公開逐週表；Master 校正總週數與演出規則",
        )
    if produce_id == "produce-004":
        return (
            _NIA_PRO,
            "all-characters",
            "N.I.A Pro 公開共同行程；Master 校正總週數與演出規則",
        )
    if produce_id == "produce-006":
        return (
            _LEGEND,
            "all-characters",
            "初 Legend 公開共同行程；Master 校正總週數與演出規則",
        )
    return (
        None,
        "milestones-only",
        "Master 總週數與演出節點；逐週可選行動尚未匯入",
    )


def load_route_calendar(
    produce_id: str,
    *,
    character_id: str | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> RouteCalendar:
    if produce_id not in _MILESTONES or produce_id not in _MODE_NAMES:
        raise KeyError(f"尚未建立路線日曆：{produce_id}")
    directory = master_dir.resolve()
    total_weeks = _master_total_weeks(produce_id, directory)
    milestones = tuple(
        AuditionMilestone(week, step_type)
        for week, step_type in _MILESTONES[produce_id]
    )
    if milestones[-1].step_type != FINAL or milestones[-1].week != total_weeks:
        raise ValueError(
            f"{produce_id} 行程資料已過期：日曆 final={milestones[-1].week}，"
            f"Master steps={total_weeks}"
        )
    detailed, detail_scope, source_note = _detailed_actions(
        produce_id, character_id
    )
    if detailed is not None and len(detailed) != total_weeks:
        raise ValueError(
            f"{produce_id} 逐週資料長度 {len(detailed)} 與 Master {total_weeks} 不符"
        )
    stage_by_week = {milestone.week: milestone.step_type for milestone in milestones}
    common_fixed = _COMMON_FIXED_ACTIONS.get(produce_id, {})
    weeks = tuple(
        RouteWeek(
            week=week,
            actions=(
                detailed[week - 1]
                if detailed is not None
                else common_fixed.get(week, ())
            ),
            stage_type=stage_by_week.get(week),
            exact_actions=(
                detailed is not None
                or week in common_fixed
                or week in stage_by_week
            ),
        )
        for week in range(1, total_weeks + 1)
    )
    scenario_name, difficulty_name = _MODE_NAMES[produce_id]
    return RouteCalendar(
        produce_id=produce_id,
        scenario_name=scenario_name,
        difficulty_name=difficulty_name,
        total_weeks=total_weeks,
        milestones=milestones,
        weeks=weeks,
        detail_scope=detail_scope,
        source_note=source_note,
    )


def infer_position_from_final_countdown(
    calendar: RouteCalendar, weeks_until_final: int
) -> RoutePosition:
    """Convert the visible final-countdown label to a route position.

    The game label is the distance between the current week and the final
    audition week, so Regular ``6`` maps to week ``13 - 6 == 7``.  This does
    not infer which action was already clicked; consumers must keep the latest
    screenshot as the authoritative state.
    """

    if weeks_until_final < 0:
        raise ValueError("距離最終演出週數不可為負數")
    current_week = calendar.final_week - weeks_until_final
    if not 1 <= current_week <= calendar.final_week:
        raise ValueError(
            f"倒數 {weeks_until_final} 無法對應 {calendar.label} 的 "
            f"1..{calendar.final_week} 週"
        )
    milestone = calendar.next_milestone(current_week)
    if milestone is None:
        raise ValueError("目前位置之後找不到演出節點")
    return RoutePosition(
        current_week=current_week,
        weeks_until_final=weeks_until_final,
        route_week=calendar.week(current_week),
        next_milestone=milestone,
    )


def infer_position_from_milestone_countdown(
    calendar: RouteCalendar,
    weeks_remaining: int,
    countdown_target: str,
) -> RoutePosition:
    """Map a visible midterm/final countdown to its unambiguous route week."""

    if countdown_target == "final":
        return infer_position_from_final_countdown(calendar, weeks_remaining)
    step_type = MID1 if countdown_target == "mid1" else None
    milestone = next(
        (item for item in calendar.milestones if item.step_type == step_type),
        None,
    )
    if milestone is None:
        raise ValueError(
            f"countdown target {countdown_target!r} is not available for {calendar.produce_id}"
        )
    current_week = milestone.week - weeks_remaining
    if not 1 <= current_week <= milestone.week:
        raise ValueError(
            f"countdown {weeks_remaining} is outside the {countdown_target} route"
        )
    next_milestone = calendar.next_milestone(current_week)
    if next_milestone is None:
        raise ValueError("route has no milestone at or after the inferred week")
    return RoutePosition(
        current_week=current_week,
        weeks_until_final=calendar.final_week - current_week,
        route_week=calendar.week(current_week),
        next_milestone=next_milestone,
    )


def supported_produce_ids() -> tuple[str, ...]:
    return tuple(_MILESTONES)


def milestone_summary(calendar: RouteCalendar) -> str:
    return "、".join(
        f"第 {milestone.week} 週 {milestone.label}"
        for milestone in calendar.milestones
    )


def action_labels(actions: Iterable[str]) -> tuple[str, ...]:
    return tuple(ACTION_LABELS[action] for action in actions)
