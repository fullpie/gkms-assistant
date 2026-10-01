"""Initial Regular adapter for the generic outer Produce rollout kernel.

All mode constants come through :mod:`route_calendar` and
``load_initial_regular_mode_rules``.  The adapter can start from explicit
offline inputs or project the already-decoded outer LocalSave/run shadow.  It
does not read those files itself and never invokes a live reader.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .initial_regular_outer_advisor import load_initial_regular_mode_rules
from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot
from .produce_rollout import (
    AttributeValues,
    DeckEntry,
    Lifecycle,
    ProduceRolloutKernel,
    ProduceRolloutState,
    RolloutPhase,
)
from .route_calendar import DEFAULT_MASTER_DIR, load_route_calendar
from .reward_card_semantics import normalize_excluded_reward_card_ids
from .run_shadow import RunShadowState


INITIAL_REGULAR_PRODUCE_ID = "produce-001"


def build_initial_regular_kernel(
    *,
    character_id: str,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> ProduceRolloutKernel:
    """Build the verified 13-step kernel without copying Master fields."""

    directory = Path(master_dir).resolve()
    calendar = load_route_calendar(
        INITIAL_REGULAR_PRODUCE_ID,
        character_id=character_id,
        master_dir=directory,
    )
    if not all(week.exact_actions for week in calendar.weeks):
        raise ValueError(
            f"Initial Regular calendar is not exact for character {character_id!r}"
        )
    rules = load_initial_regular_mode_rules(master_dir=directory)
    if rules.total_steps != calendar.total_weeks:
        raise ValueError("Initial Regular route and Produce Master step counts differ")
    return ProduceRolloutKernel(
        calendar,
        rest_recovery_permille=rules.rest_recovery_permille,
    )


def new_initial_regular_state(
    *,
    character_id: str,
    stamina: int,
    max_stamina: int,
    vocal: int,
    dance: int,
    visual: int,
    deck: Iterable[DeckEntry],
    produce_points: int = 0,
    week: int = 1,
    item_session_refs: Iterable[str] = (),
    drink_session_refs: Iterable[str] = (),
    passive_session_refs: Iterable[str] = (),
    excluded_reward_card_ids: Iterable[str] = (),
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[ProduceRolloutKernel, ProduceRolloutState]:
    """Create a fresh pure state and its Master-backed transition kernel."""

    kernel = build_initial_regular_kernel(
        character_id=character_id,
        master_dir=master_dir,
    )
    state = ProduceRolloutState(
        mode_id=INITIAL_REGULAR_PRODUCE_ID,
        character_id=character_id,
        week=week,
        total_weeks=kernel.calendar.total_weeks,
        phase=RolloutPhase.READY_FOR_WEEK,
        stamina=stamina,
        max_stamina=max_stamina,
        attributes=AttributeValues(vocal, dance, visual),
        deck=tuple(sorted(deck)),
        produce_points=produce_points,
        item_session_refs=_unique_refs(item_session_refs, "item_session_refs"),
        drink_session_refs=_ordered_refs(drink_session_refs, "drink_session_refs"),
        passive_session_refs=_unique_refs(passive_session_refs, "passive_session_refs"),
        excluded_reward_card_ids=_canonical_exclusions(excluded_reward_card_ids),
    )
    kernel.validate_state(state)
    return kernel, state


def initial_regular_state_from_sources(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    character_id: str,
    shadow: RunShadowState | None = None,
    week: int | None = None,
    deck: Iterable[DeckEntry] | None = None,
    item_session_refs: Iterable[str] = (),
    drink_session_refs: Iterable[str] = (),
    passive_session_refs: Iterable[str] = (),
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[ProduceRolloutKernel, ProduceRolloutState]:
    """Project decoded read-only adapters into the rollout state.

    The LocalSave is authoritative for status values; the run shadow is used
    only for fields that the LocalSave does not carry (deck and exclusions) or
    as an explicit numeric fallback.  A route week is accepted only from an
    explicit argument, a shadow route position, or the unambiguous
    ``last_completed_week + 1`` boundary.
    """

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if shadow is not None:
        if not isinstance(shadow, RunShadowState):
            raise TypeError("shadow must be RunShadowState or None")
        shadow.validate()
        if shadow.produce_id != INITIAL_REGULAR_PRODUCE_ID:
            raise ValueError("run shadow is not Initial Regular")
        if shadow.character_id != character_id:
            raise ValueError("run shadow character does not match requested character")
    kernel = build_initial_regular_kernel(
        character_id=character_id,
        master_dir=master_dir,
    )
    if snapshot.lifecycle is not None and not snapshot.lifecycle.is_in_progress:
        raise ValueError("outer LocalSave does not describe an in-progress Produce")
    current_week = _resolve_week(
        week,
        shadow_week=None if shadow is None else shadow.route_week,
        last_completed_week=snapshot.last_completed_week,
        total_weeks=kernel.calendar.total_weeks,
    )
    if deck is None:
        if shadow is None or shadow.deck is None:
            raise ValueError("deck identity is required when run shadow has no deck")
        deck_rows = _deck_from_shadow(shadow.deck)
    else:
        deck_rows = tuple(sorted(deck))
    stamina = _source_integer("stamina", snapshot.stamina, shadow)
    max_stamina = _source_integer("max_stamina", snapshot.max_stamina, shadow)
    vocal = _source_integer("vocal", snapshot.vocal, shadow)
    dance = _source_integer("dance", snapshot.dance, shadow)
    visual = _source_integer("visual", snapshot.visual, shadow)
    produce_points = _source_integer("produce_points", snapshot.produce_points, shadow)
    excluded = () if shadow is None else shadow.excluded_reward_card_ids
    state = ProduceRolloutState(
        mode_id=INITIAL_REGULAR_PRODUCE_ID,
        character_id=character_id,
        week=current_week,
        total_weeks=kernel.calendar.total_weeks,
        phase=RolloutPhase.READY_FOR_WEEK,
        stamina=stamina,
        max_stamina=max_stamina,
        attributes=AttributeValues(vocal, dance, visual),
        deck=deck_rows,
        produce_points=produce_points,
        item_session_refs=_unique_refs(item_session_refs, "item_session_refs"),
        drink_session_refs=_ordered_refs(drink_session_refs, "drink_session_refs"),
        passive_session_refs=_unique_refs(passive_session_refs, "passive_session_refs"),
        excluded_reward_card_ids=_canonical_exclusions(excluded),
        lifecycle=Lifecycle.IN_PROGRESS,
    )
    kernel.validate_state(state)
    return kernel, state


def _source_integer(
    field: str,
    snapshot_value: int | None,
    shadow: RunShadowState | None,
) -> int:
    value = snapshot_value
    if value is None and shadow is not None:
        value = getattr(shadow, field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} is unavailable from the offline sources")
    if field == "max_stamina" and value < 1:
        raise ValueError("max_stamina must be positive")
    return value


def _resolve_week(
    explicit: int | None,
    *,
    shadow_week: int | None,
    last_completed_week: int | None,
    total_weeks: int,
) -> int:
    candidates: list[tuple[str, int]] = []
    if explicit is not None:
        candidates.append(("explicit", explicit))
    if shadow_week is not None:
        candidates.append(("shadow", shadow_week))
    if last_completed_week is not None and last_completed_week < total_weeks:
        candidates.append(("outer-save", last_completed_week + 1))
    if not candidates:
        raise ValueError("current route week is unavailable from the offline sources")
    unique = {value for _source, value in candidates}
    if len(unique) != 1:
        detail = ", ".join(f"{source}={value}" for source, value in candidates)
        raise ValueError(f"route week sources disagree: {detail}")
    value = unique.pop()
    if not 1 <= value <= total_weeks:
        raise ValueError("current route week is outside Initial Regular")
    return value


def _deck_from_shadow(deck: object) -> tuple[DeckEntry, ...]:
    if not isinstance(deck, dict) and not hasattr(deck, "items"):
        raise ValueError("run shadow deck is invalid")
    result: list[DeckEntry] = []
    for key, count in deck.items():  # type: ignore[union-attr]
        if not isinstance(key, str) or isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("run shadow deck entry is invalid")
        card_id, separator, upgrade_text = key.rpartition("@")
        if not separator or not card_id or not upgrade_text.isdigit():
            raise ValueError(f"run shadow deck key is invalid: {key!r}")
        result.append(DeckEntry(card_id, int(upgrade_text), count))
    return tuple(sorted(result))


def _unique_refs(values: Iterable[str], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{label} must contain non-empty text")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must contain unique values")
    return result


def _ordered_refs(values: Iterable[str], label: str) -> tuple[str, ...]:
    """Preserve inventory-slot multiplicity while validating each identity."""

    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{label} must contain non-empty text")
    return result


def _canonical_exclusions(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(values)
    normalized = normalize_excluded_reward_card_ids(result)
    if len(normalized) != len(result):
        raise ValueError("excluded_reward_card_ids must be unique")
    return result


__all__ = [
    "INITIAL_REGULAR_PRODUCE_ID",
    "build_initial_regular_kernel",
    "initial_regular_state_from_sources",
    "new_initial_regular_state",
]
