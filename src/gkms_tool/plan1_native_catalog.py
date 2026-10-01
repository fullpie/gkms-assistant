"""Whole-catalog coverage projection for the fail-closed Plan 1 executor."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .master_db import DEFAULT_DATABASE
from .plan1_native_core import (
    PLAN1,
    PLAN_COMMON,
    Plan1Blocker,
    Plan1CompiledCard,
    compile_plan1_card,
)


@dataclass(frozen=True, slots=True)
class Plan1NativeCatalogEntry:
    card_id: str
    upgrade: int
    plan_type: str
    name: str
    executable: bool
    blockers: tuple[Plan1Blocker, ...]

    @classmethod
    def from_program(cls, value: Plan1CompiledCard) -> "Plan1NativeCatalogEntry":
        return cls(
            value.card_id,
            value.upgrade,
            value.plan_type,
            value.name,
            value.executable,
            value.blockers,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "plan_type": self.plan_type,
            "name": self.name,
            "executable": self.executable,
            "blockers": [
                {
                    "code": value.code,
                    "source_id": value.source_id,
                    "detail": value.detail,
                }
                for value in self.blockers
            ],
        }


@dataclass(frozen=True, slots=True)
class Plan1NativeCatalogCoverage:
    entries: tuple[Plan1NativeCatalogEntry, ...]

    def __post_init__(self) -> None:
        identities = tuple((value.card_id, value.upgrade) for value in self.entries)
        if len(identities) != len(set(identities)):
            raise ValueError("Plan1 catalog contains duplicate card versions")
        if identities != tuple(sorted(identities)):
            raise ValueError("Plan1 catalog entries must have stable identity order")

    @property
    def total_version_count(self) -> int:
        return len(self.entries)

    @property
    def executable_version_count(self) -> int:
        return sum(value.executable for value in self.entries)

    @property
    def blocked_version_count(self) -> int:
        return self.total_version_count - self.executable_version_count

    @property
    def counts_by_plan(self) -> tuple[tuple[str, int, int], ...]:
        totals = Counter(value.plan_type for value in self.entries)
        executable = Counter(
            value.plan_type for value in self.entries if value.executable
        )
        return tuple(
            (plan_type, totals[plan_type], executable[plan_type])
            for plan_type in (PLAN_COMMON, PLAN1)
        )

    @property
    def blocker_counts(self) -> tuple[tuple[str, int], ...]:
        counts = Counter(
            blocker.code
            for value in self.entries
            for blocker in value.blockers
        )
        return tuple(sorted(counts.items(), key=lambda value: (-value[1], value[0])))

    @property
    def unsupported_effect_type_counts(self) -> tuple[tuple[str, int], ...]:
        counts = Counter(
            blocker.detail
            for value in self.entries
            for blocker in value.blockers
            if blocker.code == "effect-type-unsupported"
        )
        return tuple(sorted(counts.items(), key=lambda value: (-value[1], value[0])))

    @property
    def summary_text(self) -> str:
        return (
            f"Plan1/Common card versions: {self.executable_version_count}/"
            f"{self.total_version_count} executable; "
            f"{self.blocked_version_count} fail-closed"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "gkms_tool.plan1_native_catalog_coverage",
            "schema_version": 1,
            "total_version_count": self.total_version_count,
            "executable_version_count": self.executable_version_count,
            "blocked_version_count": self.blocked_version_count,
            "counts_by_plan": [
                {
                    "plan_type": plan_type,
                    "total": total,
                    "executable": executable,
                }
                for plan_type, total, executable in self.counts_by_plan
            ],
            "blocker_counts": dict(self.blocker_counts),
            "unsupported_effect_type_counts": dict(
                self.unsupported_effect_type_counts
            ),
            "entries": [value.to_dict() for value in self.entries],
        }


def compile_plan1_native_catalog(
    *, database: Path = DEFAULT_DATABASE
) -> Plan1NativeCatalogCoverage:
    """Compile every Common/Plan1 card version in the current PC Master DB."""

    database = Path(database)
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id, upgrade_count FROM card "
            "WHERE plan_type IN (?, ?) ORDER BY id, upgrade_count",
            (PLAN_COMMON, PLAN1),
        ).fetchall()
    finally:
        connection.close()
    entries = tuple(
        Plan1NativeCatalogEntry.from_program(
            compile_plan1_card(str(card_id), int(upgrade), database=database)
        )
        for card_id, upgrade in rows
    )
    return Plan1NativeCatalogCoverage(entries)


__all__ = [
    "Plan1NativeCatalogCoverage",
    "Plan1NativeCatalogEntry",
    "compile_plan1_native_catalog",
]
