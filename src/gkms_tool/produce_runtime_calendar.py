"""Source-neutral projection of server-authored Produce schedules.

Both leaderboard histories (``ProduceSchedule``) and a live run
(``UserProduceProgressSchedule``) expose the same route facts.  The only wire
difference currently observed is the week key: histories use ``number`` while
live progress may use ``stepNumber``.  This module normalises that envelope and
delegates enum/value parsing to :func:`adapt_nia_runtime_schedule`.

Mode metadata contributes only stable route evidence: total length, setting
identity, audition milestones, and the two fixed pursuit weeks in Initial Pro
and Master.  Ordinary weekly choices are never filled from a static calendar;
they exist only when a runtime schedule row exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .nia_static_adapter import adapt_nia_runtime_schedule
from .route_calendar import CRAM_LESSON, DEFAULT_MASTER_DIR, load_route_calendar


SUPPORTED_PRODUCE_IDS = frozenset(
    {"produce-001", "produce-002", "produce-003", "produce-004", "produce-005"}
)


@dataclass(frozen=True, slots=True)
class FixedMilestoneEvidence:
    week: int
    step_type: str
    authority_ref: str = "PC Master route lifecycle"


@dataclass(frozen=True, slots=True)
class FixedPursuitEvidence:
    week: int
    authority_ref: str = "Initial route fixed pursuit evidence"


@dataclass(frozen=True, slots=True)
class ModeDescriptor:
    """Stable mode identity; never a substitute for ordinary runtime rows."""

    produce_id: str
    total_weeks: int
    setting_id: str
    fixed_milestones: tuple[FixedMilestoneEvidence, ...]
    fixed_pursuits: tuple[FixedPursuitEvidence, ...]

    def __post_init__(self) -> None:
        if self.produce_id not in SUPPORTED_PRODUCE_IDS:
            raise ValueError(f"unsupported Produce mode: {self.produce_id}")
        if self.total_weeks < 1:
            raise ValueError("total_weeks must be positive")
        if not self.setting_id:
            raise ValueError("setting_id is required")


@dataclass(frozen=True, slots=True)
class RuntimeCalendarWeek:
    """One immutable server-authored week, independent of capture source."""

    step_number: int
    step_types: tuple[str, ...]
    step_sub_parameter_types: tuple[str, ...]
    selected_step_type: str
    refresh_stamina: int

    @property
    def all_step_types(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys((self.selected_step_type, *self.step_types))
        )


@dataclass(frozen=True, slots=True)
class RuntimeCalendarIssue:
    code: str
    detail: str
    week: int | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeCalendarProjection:
    mode: ModeDescriptor
    weeks: tuple[RuntimeCalendarWeek, ...]
    issues: tuple[RuntimeCalendarIssue, ...]
    source_family: str

    @property
    def complete(self) -> bool:
        return not self.issues and tuple(
            week.step_number for week in self.weeks
        ) == tuple(range(1, self.mode.total_weeks + 1))

    def week(self, number: int) -> RuntimeCalendarWeek | None:
        return next(
            (week for week in self.weeks if week.step_number == number),
            None,
        )


@lru_cache(maxsize=8)
def _produce_rows(master_dir: Path) -> tuple[Mapping[str, Any], ...]:
    path = master_dir / "Produce.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Master file not found: {path}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError("Produce.yaml must contain a list")
    return tuple(row for row in payload if isinstance(row, Mapping))


def load_mode_descriptor(
    produce_id: str,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> ModeDescriptor:
    """Load only source-stable mode facts for Produce 001 through 005."""

    if produce_id not in SUPPORTED_PRODUCE_IDS:
        raise KeyError(f"unsupported Produce mode: {produce_id}")
    directory = Path(master_dir).resolve()
    calendar = load_route_calendar(produce_id, master_dir=directory)
    matches = tuple(
        row for row in _produce_rows(directory) if row.get("id") == produce_id
    )
    if len(matches) != 1:
        raise KeyError(
            f"Produce[{produce_id}] must resolve exactly once, found {len(matches)}"
        )
    setting_id = matches[0].get("produceSettingId")
    if not isinstance(setting_id, str) or not setting_id:
        raise ValueError(f"Produce[{produce_id}].produceSettingId is invalid")

    pursuit_evidence = tuple(
        FixedPursuitEvidence(week.week)
        for week in calendar.weeks
        if week.exact_actions and CRAM_LESSON in week.actions
    )
    if produce_id == "produce-001":
        # Regular has no character-independent ordinary-week table.  Two
        # independently preserved character samples do agree on the pursuit
        # positions, so retain that fact as scoped evidence only.  It still
        # does not create a runtime week or an ordinary-week action list.
        sample_calendars = tuple(
            load_route_calendar(
                produce_id,
                character_id=character_id,
                master_dir=directory,
            )
            for character_id in ("kllj", "fktn")
        )
        sample_pursuits = tuple(
            {
                week.week
                for week in sample.weeks
                if week.exact_actions and CRAM_LESSON in week.actions
            }
            for sample in sample_calendars
        )
        agreed = set.intersection(*sample_pursuits)
        pursuit_evidence = tuple(
            FixedPursuitEvidence(
                week,
                "Initial Regular preserved KLLJ/FKTN route samples",
            )
            for week in sorted(agreed)
        )

    return ModeDescriptor(
        produce_id=produce_id,
        total_weeks=calendar.total_weeks,
        setting_id=setting_id,
        fixed_milestones=tuple(
            FixedMilestoneEvidence(item.week, item.step_type)
            for item in calendar.milestones
        ),
        fixed_pursuits=pursuit_evidence,
    )


_NESTED_SCHEDULE_KEYS = (
    "ProduceSchedule",
    "produceSchedule",
    "UserProduceProgressSchedule",
    "userProduceProgressSchedule",
)
_REQUIRED_FIELD_ALIASES = {
    "number": ("number", "stepNumber", "step_number"),
    "selectedStepType": ("selectedStepType", "selected_step_type"),
    "stepTypes": ("stepTypes", "step_types"),
    "stepSubParameterTypes": (
        "stepSubParameterTypes",
        "step_sub_parameter_types",
    ),
    "refreshStamina": ("refreshStamina", "refresh_stamina"),
}


def _unwrap_schedule_row(row: Mapping[str, object]) -> Mapping[str, object]:
    for key in _NESTED_SCHEDULE_KEYS:
        nested = row.get(key)
        if isinstance(nested, Mapping):
            return nested
    return row


def _canonical_row(
    raw: Mapping[str, object],
) -> tuple[dict[str, object] | None, tuple[str, ...]]:
    row = _unwrap_schedule_row(raw)
    canonical: dict[str, object] = {}
    missing: list[str] = []
    for target, aliases in _REQUIRED_FIELD_ALIASES.items():
        found = next((alias for alias in aliases if alias in row), None)
        if found is None:
            missing.append(target)
        else:
            canonical[target] = row[found]
    return (canonical if not missing else None), tuple(missing)


def project_runtime_calendar(
    records: Sequence[Mapping[str, object]],
    *,
    produce_id: str,
    source_family: str,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> RuntimeCalendarProjection:
    """Project leaderboard or live schedule rows without inventing missing weeks.

    Invalid or incomplete rows are reported as evidence issues.  In particular,
    a partial Initial live capture returns only the observed weeks and an
    explicit ``runtime-calendar-incomplete`` issue; it never receives guessed
    empty candidates, zero stamina, or static ordinary-week defaults.
    """

    if not isinstance(source_family, str) or not source_family:
        raise ValueError("source_family is required")
    mode = load_mode_descriptor(produce_id, master_dir=master_dir)
    weeks: list[RuntimeCalendarWeek] = []
    issues: list[RuntimeCalendarIssue] = []

    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            issues.append(
                RuntimeCalendarIssue(
                    "runtime-row-invalid",
                    f"schedule row {index} is not an object",
                )
            )
            continue
        canonical, missing = _canonical_row(raw)
        if canonical is None:
            raw_number = _unwrap_schedule_row(raw).get("stepNumber")
            if raw_number is None:
                raw_number = _unwrap_schedule_row(raw).get("number")
            week = (
                raw_number
                if isinstance(raw_number, int) and not isinstance(raw_number, bool)
                else None
            )
            issues.append(
                RuntimeCalendarIssue(
                    "runtime-row-incomplete",
                    "missing required schedule fields: " + ",".join(missing),
                    week=week,
                    field=missing[0],
                )
            )
            continue
        try:
            parsed = adapt_nia_runtime_schedule((canonical,)).steps[0]
        except (TypeError, ValueError) as error:
            issues.append(
                RuntimeCalendarIssue(
                    "runtime-row-invalid",
                    str(error),
                    week=(
                        canonical["number"]
                        if isinstance(canonical["number"], int)
                        and not isinstance(canonical["number"], bool)
                        else None
                    ),
                )
            )
            continue
        weeks.append(
            RuntimeCalendarWeek(
                step_number=parsed.number,
                step_types=parsed.step_types,
                step_sub_parameter_types=parsed.step_sub_parameter_types,
                selected_step_type=parsed.selected_step_type,
                refresh_stamina=parsed.refresh_stamina,
            )
        )

    weeks.sort(key=lambda item: item.step_number)
    numbers = tuple(week.step_number for week in weeks)
    duplicate_numbers = tuple(
        sorted({number for number in numbers if numbers.count(number) > 1})
    )
    if duplicate_numbers:
        issues.append(
            RuntimeCalendarIssue(
                "runtime-calendar-duplicate-weeks",
                f"duplicate schedule weeks: {duplicate_numbers}",
            )
        )
    expected_numbers = tuple(range(1, mode.total_weeks + 1))
    if numbers != expected_numbers:
        issues.append(
            RuntimeCalendarIssue(
                "runtime-calendar-incomplete",
                f"expected weeks 1..{mode.total_weeks}, received {numbers}",
            )
        )

    week_by_number = {week.step_number: week for week in weeks}
    for milestone in mode.fixed_milestones:
        observed = week_by_number.get(milestone.week)
        if observed is not None and observed.selected_step_type != milestone.step_type:
            issues.append(
                RuntimeCalendarIssue(
                    "runtime-milestone-mismatch",
                    f"expected {milestone.step_type}, received "
                    f"{observed.selected_step_type}",
                    week=milestone.week,
                    field="selectedStepType",
                )
            )

    return RuntimeCalendarProjection(
        mode=mode,
        weeks=tuple(weeks),
        issues=tuple(issues),
        source_family=source_family,
    )


__all__ = [
    "FixedMilestoneEvidence",
    "FixedPursuitEvidence",
    "ModeDescriptor",
    "RuntimeCalendarIssue",
    "RuntimeCalendarProjection",
    "RuntimeCalendarWeek",
    "SUPPORTED_PRODUCE_IDS",
    "load_mode_descriptor",
    "project_runtime_calendar",
]
