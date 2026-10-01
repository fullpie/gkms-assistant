"""Fail-closed Plan 3 exam-start bootstrap from immutable outer inputs.

This boundary deliberately does not read OCR or LocalSave data.  A ready
result is produced only when the caller supplies both pieces of native chance
state: the exam RNG state and every independently generated card GUID.  The
module handles the proven ordinary-pool shuffle and opening-hand rules, then
projects the same ordered card instances into ``Plan3State``.

Fresh native card GUIDs are *not* derived from the exam RNG.  Android 3.2.3's
``ExamCardData.CreateGuidIfNeed`` calls ``System.Guid.NewGuid`` independently,
so missing GUIDs are an unresolved input rather than something this module may
invent.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
import json
from pathlib import Path
import sqlite3

from .exam_native_rng import (
    INT32_MAX,
    INT32_MIN,
    UINT32_MASK,
    shuffle_unfixed_pool,
)
from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    LESSON_UNKNOWN,
    PLAN3,
    PLAN_COMMON,
    Plan3ExamSettings,
    Plan3State,
    load_plan3_exam_settings,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState
from .produce_rollout import AttributeValues, DeckEntry


class Plan3ExamStartStatus(StrEnum):
    READY = "ready"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True, order=True)
class Plan3ExamStartIssue:
    """One stable reason an exact start state cannot be emitted."""

    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("issue code and field must be non-empty")


@dataclass(frozen=True, slots=True)
class Plan3ExamStartChance:
    """Authoritative chance inputs owned outside the deterministic bootstrap.

    ``random_root`` is an opaque branch/provenance identity. ``random_state``
    is the uint32 state consumed by the native exam PRNG. ``instance_guids``
    supplies GUIDs for deck rows whose ``DeckEntry.instance_ids`` are empty,
    in flattened outer-deck order. ``fixed_deck_orders`` is likewise flattened;
    omission means the proven fresh-card default of zero.
    """

    random_root: str | None
    random_state: int | None
    instance_guids: tuple[str, ...] = ()
    fixed_deck_orders: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "instance_guids", tuple(self.instance_guids))
        object.__setattr__(
            self, "fixed_deck_orders", tuple(self.fixed_deck_orders)
        )
        if self.random_root is not None and (
            not isinstance(self.random_root, str) or not self.random_root.strip()
        ):
            raise ValueError("random_root must be non-empty text or None")
        if self.random_state is not None and (
            not isinstance(self.random_state, int)
            or isinstance(self.random_state, bool)
            or not 0 <= self.random_state <= UINT32_MASK
        ):
            raise ValueError("random_state must be uint32 or None")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in self.instance_guids
        ):
            raise ValueError("instance_guids must contain non-empty text")
        for value in self.fixed_deck_orders:
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not INT32_MIN <= value <= INT32_MAX
            ):
                raise ValueError("fixed_deck_orders must contain Int32 values")


@dataclass(frozen=True, slots=True)
class Plan3ExamStartLoadout:
    """Opaque outer loadout identities retained with the ready state."""

    item_ids: tuple[str, ...] = ()
    drink_ids: tuple[str, ...] = ()
    support_card_ids: tuple[str, ...] = ()
    passive_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "item_ids",
            "drink_ids",
            "support_card_ids",
            "passive_ids",
            "memory_ids",
        ):
            values = tuple(getattr(self, name))
            if any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(f"{name} must contain non-empty text")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must not contain duplicate identities")
            object.__setattr__(self, name, values)

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.item_ids,
                self.drink_ids,
                self.support_card_ids,
                self.passive_ids,
                self.memory_ids,
            )
        )


@dataclass(frozen=True, slots=True)
class Plan3ExamStartContext:
    """Static stage/setting fields needed by the first actionable state."""

    stage_id: str
    setting_id: str
    turns: int
    current_parameter_type: int
    lesson_type: str = LESSON_UNKNOWN
    step_type_value: int = 0
    is_battle: bool = False
    clear_border: int = -1
    limit_border: int = -1
    battle_bonus_permille: tuple[int, int, int] = (1000, 1000, 1000)
    gimmick_group_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.stage_id, str) or not self.stage_id.strip():
            raise ValueError("stage_id must be non-empty text")
        if not isinstance(self.setting_id, str) or not self.setting_id.strip():
            raise ValueError("setting_id must be non-empty text")
        if (
            not isinstance(self.turns, int)
            or isinstance(self.turns, bool)
            or self.turns < 1
        ):
            raise ValueError("turns must be a positive integer")
        if (
            not isinstance(self.current_parameter_type, int)
            or isinstance(self.current_parameter_type, bool)
            or self.current_parameter_type not in range(4)
        ):
            raise ValueError(
                "current_parameter_type must be Unknown/Vocal/Dance/Visual"
            )
        if not isinstance(self.lesson_type, str) or not self.lesson_type:
            raise ValueError("lesson_type must be a non-empty Master enum")
        if (
            not isinstance(self.step_type_value, int)
            or isinstance(self.step_type_value, bool)
            or self.step_type_value not in {*range(10), *((16, 17, 18) if self.is_battle else ())}
        ):
            raise ValueError("step_type_value must be 0..9 or battle audition 16/17/18")
        if not isinstance(self.is_battle, bool):
            raise ValueError("is_battle must be a boolean")
        for name in ("clear_border", "limit_border"):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not INT32_MIN <= value <= INT32_MAX
            ):
                raise ValueError(f"{name} must be an Int32")
        bonuses = tuple(self.battle_bonus_permille)
        if len(bonuses) != 3 or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not INT32_MIN <= value <= INT32_MAX
            for value in bonuses
        ):
            raise ValueError("battle_bonus_permille must contain three Int32 values")
        object.__setattr__(self, "battle_bonus_permille", bonuses)
        if not isinstance(self.gimmick_group_id, str):
            raise ValueError("gimmick_group_id must be text")


@dataclass(frozen=True, slots=True)
class Plan3ExamStartInputs:
    """Complete immutable request for one Plan 3 exam-start bootstrap.

    ``start_turn_effects_known_absent`` is intentionally tri-state. ``None``
    means the passive/gimmick/loadout start boundary was not resolved. ``False``
    means effects are known to exist but are not represented by this bootstrap.
    Only explicit ``True`` permits a first-action-ready result.
    """

    deck: tuple[DeckEntry, ...]
    context: Plan3ExamStartContext
    chance: Plan3ExamStartChance
    attributes: AttributeValues
    stamina: int
    max_stamina: int
    loadout: Plan3ExamStartLoadout = field(default_factory=Plan3ExamStartLoadout)
    start_turn_effects_known_absent: bool | None = None

    def __post_init__(self) -> None:
        deck = tuple(self.deck)
        if not deck or not all(isinstance(value, DeckEntry) for value in deck):
            raise ValueError("deck must contain at least one DeckEntry")
        object.__setattr__(self, "deck", deck)
        if not isinstance(self.context, Plan3ExamStartContext):
            raise TypeError("context must be Plan3ExamStartContext")
        if not isinstance(self.chance, Plan3ExamStartChance):
            raise TypeError("chance must be Plan3ExamStartChance")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("attributes must be AttributeValues")
        for name in ("stamina", "max_stamina"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.stamina > self.max_stamina:
            raise ValueError("stamina must not exceed max_stamina")
        if not isinstance(self.loadout, Plan3ExamStartLoadout):
            raise TypeError("loadout must be Plan3ExamStartLoadout")
        if self.start_turn_effects_known_absent is not None and not isinstance(
            self.start_turn_effects_known_absent, bool
        ):
            raise ValueError("start_turn_effects_known_absent must be bool or None")


@dataclass(frozen=True, slots=True, order=True)
class Plan3ExamStartCardMetadata:
    """The exact Master row bound to one expanded card instance."""

    guid: str
    card_id: str
    upgrade: int
    plan_type: str
    is_initial: bool
    no_deck_duplication: bool
    fixed_deck_order: int


@dataclass(frozen=True, slots=True)
class Plan3ExamStartReady:
    inputs: Plan3ExamStartInputs
    settings: Plan3ExamSettings
    cards: tuple[Plan3ExamStartCardMetadata, ...]
    state: Plan3State
    native_state: Plan3NativeState
    opening_hand_size: int
    status: Plan3ExamStartStatus = field(
        default=Plan3ExamStartStatus.READY, init=False
    )
    issues: tuple[Plan3ExamStartIssue, ...] = field(default=(), init=False)

    def __post_init__(self) -> None:
        if self.opening_hand_size != len(self.native_state.hand):
            raise ValueError("opening_hand_size must match native hand")
        self.native_state.assert_plan3_projection(self.state)

    @property
    def available(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class Plan3ExamStartPause:
    inputs: Plan3ExamStartInputs
    issues: tuple[Plan3ExamStartIssue, ...]
    status: Plan3ExamStartStatus = field(
        default=Plan3ExamStartStatus.PAUSED, init=False
    )
    state: None = field(default=None, init=False)
    native_state: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not self.issues:
            raise ValueError("paused result must contain at least one issue")

    @property
    def available(self) -> bool:
        return False


Plan3ExamStartResult = Plan3ExamStartReady | Plan3ExamStartPause


def _pause(
    inputs: Plan3ExamStartInputs, *issues: Plan3ExamStartIssue
) -> Plan3ExamStartPause:
    return Plan3ExamStartPause(inputs=inputs, issues=tuple(issues))


def _expand_guids(
    inputs: Plan3ExamStartInputs,
) -> tuple[tuple[tuple[DeckEntry, str], ...] | None, tuple[Plan3ExamStartIssue, ...]]:
    missing_count = sum(entry.count for entry in inputs.deck if not entry.instance_ids)
    supplied = inputs.chance.instance_guids
    issues: list[Plan3ExamStartIssue] = []
    if len(supplied) != missing_count:
        issues.append(
            Plan3ExamStartIssue(
                "instance-guids-count-mismatch",
                "chance.instance_guids",
                f"required={missing_count}:supplied={len(supplied)}",
            )
        )
        return None, tuple(issues)

    supplied_index = 0
    expanded: list[tuple[DeckEntry, str]] = []
    for entry in inputs.deck:
        if entry.instance_ids:
            guids = entry.instance_ids
        else:
            guids = supplied[supplied_index : supplied_index + entry.count]
            supplied_index += entry.count
        expanded.extend((entry, guid) for guid in guids)

    duplicates = tuple(
        sorted(
            guid
            for guid, count in Counter(guid for _, guid in expanded).items()
            if count > 1
        )
    )
    if duplicates:
        issues.append(
            Plan3ExamStartIssue(
                "duplicate-instance-guid",
                "chance.instance_guids",
                duplicates[0],
            )
        )
        return None, tuple(issues)
    return tuple(expanded), ()


def _load_card_metadata(
    expanded: tuple[tuple[DeckEntry, str], ...],
    *,
    database: Path,
) -> tuple[
    tuple[Plan3ExamStartCardMetadata, ...] | None,
    tuple[Plan3ExamStartIssue, ...],
]:
    if not database.is_file():
        return None, (
            Plan3ExamStartIssue(
                "master-database-unresolved", "database", str(database)
            ),
        )

    rows: dict[tuple[str, int], tuple[str, str]] = {}
    issues: list[Plan3ExamStartIssue] = []
    try:
        with sqlite3.connect(database) as connection:
            for entry, _guid in expanded:
                key = (entry.card_id, entry.upgrade)
                if key in rows:
                    continue
                matches = connection.execute(
                    "SELECT plan_type, raw_json FROM card "
                    "WHERE id = ? AND upgrade_count = ?",
                    key,
                ).fetchall()
                if len(matches) != 1:
                    issues.append(
                        Plan3ExamStartIssue(
                            "master-card-unresolved",
                            "deck",
                            f"{entry.card_id}+{entry.upgrade}:matches={len(matches)}",
                        )
                    )
                    continue
                rows[key] = (str(matches[0][0]), str(matches[0][1]))
    except (sqlite3.Error, OSError) as error:
        return None, (
            Plan3ExamStartIssue(
                "master-database-unresolved", "database", str(error)
            ),
        )
    if issues:
        return None, tuple(issues)

    metadata: list[Plan3ExamStartCardMetadata] = []
    for entry, guid in expanded:
        plan_type, raw_text = rows[(entry.card_id, entry.upgrade)]
        if plan_type not in {PLAN_COMMON, PLAN3}:
            issues.append(
                Plan3ExamStartIssue(
                    "card-plan-type-mismatch",
                    "deck",
                    f"{entry.card_id}+{entry.upgrade}:{plan_type}",
                )
            )
            continue
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as error:
            issues.append(
                Plan3ExamStartIssue(
                    "master-card-metadata-invalid",
                    "deck",
                    f"{entry.card_id}+{entry.upgrade}:{error}",
                )
            )
            continue
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("isInitial"), bool)
            or not isinstance(raw.get("noDeckDuplication"), bool)
        ):
            issues.append(
                Plan3ExamStartIssue(
                    "master-card-metadata-invalid",
                    "deck",
                    f"{entry.card_id}+{entry.upgrade}",
                )
            )
            continue
        metadata.append(
            Plan3ExamStartCardMetadata(
                guid=guid,
                card_id=entry.card_id,
                upgrade=entry.upgrade,
                plan_type=plan_type,
                is_initial=raw["isInitial"],
                no_deck_duplication=raw["noDeckDuplication"],
                fixed_deck_order=0,
            )
        )
    if issues:
        return None, tuple(issues)
    return tuple(metadata), ()


def bootstrap_plan3_exam_start(
    inputs: Plan3ExamStartInputs,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3ExamStartResult:
    """Build a first-action-ready state, or return typed unresolved inputs.

    Proven order implemented here is: create the fresh zero-runtime cards,
    shuffle the ordinary deck pool, move Master ``isInitial`` cards while the
    Hand remains below the setting Hand limit, then draw the remainder of the
    setting's opening distribution.  A ready state has already crossed the
    turn-start boundary and therefore has ``awaiting_turn_start=False``.
    """

    if not isinstance(inputs, Plan3ExamStartInputs):
        raise TypeError("inputs must be Plan3ExamStartInputs")
    database = Path(database)
    master_dir = Path(master_dir)

    boundary_issues: list[Plan3ExamStartIssue] = []
    if inputs.chance.random_root is None:
        boundary_issues.append(
            Plan3ExamStartIssue(
                "random-root-unresolved", "chance.random_root"
            )
        )
    if inputs.chance.random_state is None:
        boundary_issues.append(
            Plan3ExamStartIssue(
                "random-state-unresolved", "chance.random_state"
            )
        )
    if inputs.start_turn_effects_known_absent is None:
        boundary_issues.append(
            Plan3ExamStartIssue(
                "start-turn-effects-unresolved",
                "start_turn_effects_known_absent",
            )
        )
    elif not inputs.start_turn_effects_known_absent:
        boundary_issues.append(
            Plan3ExamStartIssue(
                "start-turn-effects-unsupported",
                "start_turn_effects_known_absent",
            )
        )
    if boundary_issues:
        return _pause(inputs, *boundary_issues)

    expanded, guid_issues = _expand_guids(inputs)
    if guid_issues:
        return _pause(inputs, *guid_issues)
    assert expanded is not None
    total_count = len(expanded)

    orders = inputs.chance.fixed_deck_orders
    if orders and len(orders) != total_count:
        return _pause(
            inputs,
            Plan3ExamStartIssue(
                "fixed-deck-orders-count-mismatch",
                "chance.fixed_deck_orders",
                f"required={total_count}:supplied={len(orders)}",
            ),
        )
    if not orders:
        orders = (0,) * total_count
    if any(value > 0 for value in orders):
        # Native switches to List<T>.Sort with an order-only comparator.  Equal
        # order ties are not stable, so an exact identity order is unavailable.
        return _pause(
            inputs,
            Plan3ExamStartIssue(
                "fixed-deck-order-sort-unresolved",
                "chance.fixed_deck_orders",
                "positive fixedDeckOrder selects the native sort path",
            ),
        )

    metadata, metadata_issues = _load_card_metadata(expanded, database=database)
    if metadata_issues:
        return _pause(inputs, *metadata_issues)
    assert metadata is not None
    metadata = tuple(
        Plan3ExamStartCardMetadata(
            guid=value.guid,
            card_id=value.card_id,
            upgrade=value.upgrade,
            plan_type=value.plan_type,
            is_initial=value.is_initial,
            no_deck_duplication=value.no_deck_duplication,
            fixed_deck_order=order,
        )
        for value, order in zip(metadata, orders, strict=True)
    )

    id_counts = Counter(value.card_id for value in metadata)
    duplicate_unique = next(
        (
            value.card_id
            for value in metadata
            if value.no_deck_duplication and id_counts[value.card_id] > 1
        ),
        None,
    )
    if duplicate_unique is not None:
        return _pause(
            inputs,
            Plan3ExamStartIssue(
                "no-deck-duplication-violation", "deck", duplicate_unique
            ),
        )

    try:
        settings = load_plan3_exam_settings(
            inputs.context.setting_id, master_dir=master_dir
        )
    except (KeyError, ValueError, OSError) as error:
        return _pause(
            inputs,
            Plan3ExamStartIssue(
                "exam-setting-unresolved", "context.setting_id", str(error)
            ),
        )

    initial_count = sum(value.is_initial for value in metadata)
    if initial_count > settings.hand_limit:
        return _pause(
            inputs,
            Plan3ExamStartIssue(
                "initial-card-overflow-unresolved",
                "deck",
                f"initial={initial_count}:hand_limit={settings.hand_limit}",
            ),
        )

    native_cards = tuple(
        Plan3NativeCard(
            guid=value.guid,
            card_id=value.card_id,
            base_upgrade=value.upgrade,
            temporary_upgrade=0,
            effective_upgrade=value.upgrade,
            fixed_deck_order=value.fixed_deck_order,
        )
        for value in metadata
    )
    by_guid = {value.guid: value for value in metadata}
    shuffled, next_state = shuffle_unfixed_pool(
        native_cards,
        inputs.chance.random_state,
        fixed_deck_orders=orders,
    )

    # SetInitialCard scans Deck from Count-1 down to zero.  Removing by index
    # while scanning preserves all non-initial deck positions.
    hand: list[Plan3NativeCard] = []
    for index in range(len(shuffled) - 1, -1, -1):
        if by_guid[shuffled[index].guid].is_initial:
            hand.append(shuffled.pop(index))

    requested = min(settings.turn_start_distribute, settings.hand_limit)
    draw_count = min(max(0, requested - len(hand)), len(shuffled))
    hand.extend(shuffled[:draw_count])
    del shuffled[:draw_count]

    native_state = Plan3NativeState(
        hand=tuple(hand),
        deck=tuple(shuffled),
        random_state=next_state,
    )
    projection = native_state.projection()
    bonus_vocal, bonus_dance, bonus_visual = inputs.context.battle_bonus_permille
    state = Plan3State(
        turns_remaining=inputs.context.turns,
        stamina=inputs.stamina,
        max_stamina=inputs.max_stamina,
        is_battle=inputs.context.is_battle,
        current_parameter_type=inputs.context.current_parameter_type,
        battle_bonus_permille_vocal=bonus_vocal,
        battle_bonus_permille_dance=bonus_dance,
        battle_bonus_permille_visual=bonus_visual,
        clear_border=inputs.context.clear_border,
        limit_border=inputs.context.limit_border,
        lesson_type=inputs.context.lesson_type,
        step_type_value=inputs.context.step_type_value,
        awaiting_turn_start=False,
        hand=projection.hand,
        draw_pile=projection.deck,
        discard_pile=projection.grave,
        lost_pile=projection.lost,
        hold_pile=projection.hold,
    )
    native_state.assert_plan3_projection(state)
    return Plan3ExamStartReady(
        inputs=inputs,
        settings=settings,
        cards=metadata,
        state=state,
        native_state=native_state,
        opening_hand_size=len(hand),
    )


# Concise alias for callers that name this boundary as a builder.
build_plan3_exam_start = bootstrap_plan3_exam_start


__all__ = [
    "Plan3ExamStartCardMetadata",
    "Plan3ExamStartChance",
    "Plan3ExamStartContext",
    "Plan3ExamStartInputs",
    "Plan3ExamStartIssue",
    "Plan3ExamStartLoadout",
    "Plan3ExamStartPause",
    "Plan3ExamStartReady",
    "Plan3ExamStartResult",
    "Plan3ExamStartStatus",
    "bootstrap_plan3_exam_start",
    "build_plan3_exam_start",
]
