"""Resolve an exact Plan 3 Master card from serialized runtime upgrade lineage.

Android v3.2.3 ``ExamCardData.get_UpgradeCount`` computes the effective
upgrade as base + temporary + the number of successful support upgrades.
``SetSupportUpgrade`` then reloads the card through
``InGameResolver.GetProduceCardData(id, effective_upgrade)``.  This module
models only that proven selection rule; it never falls back to another Master
variant when the exact row is absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .audition_local_save_state import LocalSaveExamCard
from .master_db import DEFAULT_DATABASE
from .plan3_engine import Plan3Card, load_plan3_card


def _upgrade(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def resolve_plan3_runtime_card(
    card_id: str,
    *,
    base_upgrade: int,
    temporary_upgrade: int,
    effective_upgrade: int,
    support_upgrade_ids: Iterable[str],
    database: Path = DEFAULT_DATABASE,
) -> Plan3Card:
    """Load the exact Master variant selected by an ``ExamCardData`` snapshot.

    The explicit ``effective_upgrade`` is checked against the serialized
    lineage instead of being silently recomputed.  This catches incomplete or
    internally inconsistent LocalSave projections before consulting Master.
    """

    if not isinstance(card_id, str) or not card_id.strip():
        raise ValueError("card_id must be non-empty text")
    base = _upgrade(base_upgrade, "base_upgrade")
    temporary = _upgrade(temporary_upgrade, "temporary_upgrade")
    effective = _upgrade(effective_upgrade, "effective_upgrade")
    if isinstance(support_upgrade_ids, (str, bytes)):
        raise ValueError("support_upgrade_ids must be an iterable of support IDs")
    support_ids = tuple(support_upgrade_ids)
    if any(not isinstance(value, str) or not value.strip() for value in support_ids):
        raise ValueError("support_upgrade_ids must contain non-empty text")
    if len(set(support_ids)) != len(support_ids):
        raise ValueError("support_upgrade_ids must not contain duplicates")

    expected = base + temporary + len(support_ids)
    if effective != expected:
        raise ValueError(
            "effective_upgrade is not explained by base, temporary, and "
            f"support upgrades: effective={effective}; expected={expected}"
        )

    # Native asks Master for exactly this pair.  load_plan3_card raises KeyError
    # when it does not exist, preserving the same fail-closed boundary.
    return load_plan3_card(card_id, effective, database)


def resolve_plan3_local_save_card(
    card: LocalSaveExamCard,
    database: Path = DEFAULT_DATABASE,
) -> Plan3Card:
    """Resolve a parsed LocalSave card without depending on live observation."""

    if not isinstance(card, LocalSaveExamCard):
        raise TypeError("card must be a LocalSaveExamCard")
    return resolve_plan3_runtime_card(
        card.card_id,
        base_upgrade=card.base_upgrade,
        temporary_upgrade=card.temporary_upgrade,
        effective_upgrade=card.effective_upgrade,
        support_upgrade_ids=card.support_upgrade_ids,
        database=database,
    )


__all__ = ["resolve_plan3_local_save_card", "resolve_plan3_runtime_card"]
