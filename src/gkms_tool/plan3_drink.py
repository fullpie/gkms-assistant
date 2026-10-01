"""Static Plan3 drink definitions and one-use inventory actions.

This module deliberately stops before the Plan3 scalar engine and native beam
search.  It preserves the ordered Master effect list and turns one selected
inventory slot into an immutable sequence of drink steps that a later search
adapter can execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from .drink_catalog import (
    EFFECT_BLOCK,
    EFFECT_CARD_MOVE,
    EFFECT_CARD_PLAY_AGGRESSIVE,
    EFFECT_CONCENTRATION,
    EFFECT_EXTRA_TURN,
    EFFECT_FULL_POWER_POINT,
    EFFECT_LESSON,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_PRESERVATION,
    EFFECT_REVIEW,
    EFFECT_STAMINA_CONSUMPTION_DOWN,
    EFFECT_STAMINA_CONSUMPTION_ADD,
    EFFECT_STAMINA_RECOVER_FIX,
    EFFECT_STAMINA_REDUCE_FIX,
    MOVE_HOLD,
    PICK_SELECT,
    SEARCH_DECK_GRAVE,
    DrinkEffectCatalogEntry,
    load_drink_catalog,
)
from .master_db import DEFAULT_DATABASE
from .plan3_drink_upgrade import Plan3DrinkCardUpgradeEffect


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MASTER_DIR = PROJECT_ROOT / "_research" / "gakumasu-diff"

_NUMERIC_EFFECT_TYPES = frozenset(
    {
        EFFECT_LESSON,
        EFFECT_BLOCK,
        EFFECT_PRESERVATION,
        EFFECT_CONCENTRATION,
        EFFECT_FULL_POWER_POINT,
        EFFECT_EXTRA_TURN,
        EFFECT_PLAYABLE_VALUE_ADD,
        EFFECT_STAMINA_RECOVER_FIX,
        EFFECT_STAMINA_CONSUMPTION_ADD,
        EFFECT_STAMINA_CONSUMPTION_DOWN,
        EFFECT_STAMINA_REDUCE_FIX,
    }
)


def _nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Plan3DrinkNumericEffect:
    """One immediately applied scalar exam effect."""

    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    chain_effect_id: str = ""
    status_enchant_id: str = ""

    def __post_init__(self) -> None:
        _nonempty(self.id, "numeric effect id")
        if self.effect_type not in _NUMERIC_EFFECT_TYPES:
            raise ValueError(f"unsupported numeric drink effect: {self.effect_type}")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _integer(getattr(self, name), name)
        if self.effect_type == EFFECT_LESSON:
            valid_shape = (
                self.value2 == 0
                and self.effect_count > 0
                and self.effect_turn == 0
            )
        elif self.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
            valid_shape = (
                self.value1 == 0
                and self.value2 == 0
                and self.effect_count > 0
                and self.effect_turn == 0
            )
        elif self.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
            valid_shape = (
                self.value1 == 0
                and self.value2 == 0
                and self.effect_count == 0
                and self.effect_turn > 0
            )
        elif self.effect_type == EFFECT_STAMINA_CONSUMPTION_ADD:
            valid_shape = (
                self.value1 == 0
                and self.value2 == 0
                and self.effect_count == 0
                and self.effect_turn > 0
            )
        elif self.effect_type == EFFECT_EXTRA_TURN:
            valid_shape = (
                self.value1 == 0
                and self.value2 == 0
                and self.effect_count == 0
                and self.effect_turn == 0
            )
        elif self.effect_type in (
            EFFECT_STAMINA_RECOVER_FIX,
            EFFECT_STAMINA_REDUCE_FIX,
        ):
            valid_shape = (
                self.value1 > 0
                and self.value2 == 0
                and self.effect_count == 0
                and self.effect_turn == 0
            )
        else:
            valid_shape = (
                self.value2 == 0
                and self.effect_count == 0
                and self.effect_turn == 0
            )
        if not valid_shape:
            raise ValueError(f"unsupported numeric drink shape: {self.id}")
        if not isinstance(self.chain_effect_id, str):
            raise TypeError("chain_effect_id must be text")
        if not isinstance(self.status_enchant_id, str):
            raise TypeError("status_enchant_id must be text")


@dataclass(frozen=True, slots=True)
class Plan3DrinkSelectedCardMove:
    """One user-selected card movement performed by a drink."""

    id: str
    card_search_id: str
    destination: str
    pick_range_type: str
    pick_count_min: int
    pick_count_max: int

    def __post_init__(self) -> None:
        _nonempty(self.id, "card move effect id")
        _nonempty(self.card_search_id, "card_search_id")
        _nonempty(self.destination, "destination")
        _nonempty(self.pick_range_type, "pick_range_type")
        _integer(self.pick_count_min, "pick_count_min", minimum=0)
        _integer(self.pick_count_max, "pick_count_max", minimum=0)
        if self.pick_count_min > self.pick_count_max:
            raise ValueError("pick_count_min cannot exceed pick_count_max")


@dataclass(frozen=True, slots=True)
class Plan3DrinkKernelEffect:
    """An exact Master effect delegated to an existing scalar/native owner."""
    id: str
    effect_type: str
    database: Path = DEFAULT_DATABASE

    def resolve_contract(self):
        from .plan3_engine import load_plan3_effect, _effect_shape_errors
        effect = load_plan3_effect(self.id, Path(self.database))
        if effect.effect_type != self.effect_type:
            raise ValueError("drink kernel effect type differs from Master:" + self.id)
        if effect.effect_type == "ProduceExamEffectType_ExamForcePlayCardSearch":
            from .plan3_drink_force_play_random_pool import load_plan3_random_pool_force_program
            load_plan3_random_pool_force_program(self.id,Path(self.database))
            return effect
        errors = _effect_shape_errors(effect)
        if errors:
            raise ValueError("drink kernel effect unresolved:" + ",".join(errors))
        if effect.effect_type == "ProduceExamEffectType_ExamCardMove":
            from .plan3_startplay_idol_return import matches_effect
            if not matches_effect(effect):
                raise ValueError("drink kernel move shape unresolved:" + self.id)
        if effect.effect_type == "ProduceExamEffectType_ExamCardCreateSearch":
            from .plan3_card_create_search import load_master_card_create_search_rows, resolve_card_create_search_contract
            rows = load_master_card_create_search_rows(Path(self.database))
            row = next((value for value in rows if value["id"] == self.id), None)
            if row is None:
                raise ValueError("drink creation Master row unavailable:" + self.id)
            resolve_card_create_search_contract(row)
        return effect


_KERNEL_EFFECT_TYPES = frozenset({
    "ProduceExamEffectType_ExamHandGraveCountCardDraw",
    "ProduceExamEffectType_ExamCardDraw",
    "ProduceExamEffectType_ExamLessonValueMultiple",
    "ProduceExamEffectType_ExamStatusEnchant",
    "ProduceExamEffectType_ExamCardCreateSearch",
    "ProduceExamEffectType_ExamForcePlayCardSearch",
})

Plan3DrinkEffect: TypeAlias = Plan3DrinkNumericEffect | Plan3DrinkSelectedCardMove | Plan3DrinkCardUpgradeEffect | Plan3DrinkKernelEffect


@dataclass(frozen=True, slots=True)
class Plan3Drink:
    """One drink and its Master-ordered effects."""

    id: str
    name: str
    plan_type: str
    rarity: str
    effects: tuple[Plan3DrinkEffect, ...]

    def __post_init__(self) -> None:
        for name in ("id", "name", "plan_type", "rarity"):
            _nonempty(getattr(self, name), name)
        if not self.effects:
            raise ValueError("drink effects cannot be empty")
        if len({effect.id for effect in self.effects}) != len(self.effects):
            raise ValueError("drink effect ids must be unique")


@dataclass(frozen=True, slots=True)
class Plan3DrinkNumericStep:
    effect: Plan3DrinkNumericEffect


@dataclass(frozen=True, slots=True)
class Plan3DrinkCardUpgradeStep:
    effect: Plan3DrinkCardUpgradeEffect


@dataclass(frozen=True, slots=True)
class Plan3DrinkKernelStep:
    effect: Plan3DrinkKernelEffect


@dataclass(frozen=True, slots=True)
class Plan3DrinkSelectCardStep:
    effect: Plan3DrinkSelectedCardMove
    selected_card_guid: str

    def __post_init__(self) -> None:
        _nonempty(self.selected_card_guid, "selected_card_guid")


Plan3DrinkUseStep: TypeAlias = Plan3DrinkNumericStep | Plan3DrinkSelectCardStep | Plan3DrinkCardUpgradeStep | Plan3DrinkKernelStep


@dataclass(frozen=True, slots=True)
class Plan3DrinkInventory:
    """Ordered native inventory; tuple position is the current UseDrink index."""

    drinks: tuple[Plan3Drink, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.drinks, tuple) or not all(
            isinstance(drink, Plan3Drink) for drink in self.drinks
        ):
            raise TypeError("drinks must be a tuple of Plan3Drink values")

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(drink.id for drink in self.drinks)


@dataclass(frozen=True, slots=True)
class Plan3DrinkUse:
    """One consumed inventory slot and its ordered executable steps."""

    before: Plan3DrinkInventory
    after: Plan3DrinkInventory
    slot_index: int
    drink: Plan3Drink
    steps: tuple[Plan3DrinkUseStep, ...]

    def __post_init__(self) -> None:
        _integer(self.slot_index, "slot_index", minimum=0)
        if self.slot_index >= len(self.before.drinks):
            raise ValueError("slot_index is outside the before inventory")
        if self.before.drinks[self.slot_index] != self.drink:
            raise ValueError("drink does not match the consumed inventory slot")
        expected = (
            self.before.drinks[: self.slot_index]
            + self.before.drinks[self.slot_index + 1 :]
        )
        if self.after.drinks != expected:
            raise ValueError("after inventory must remove exactly one drink")
        if len(self.steps) != len(self.drink.effects):
            raise ValueError("use steps must preserve every ordered drink effect")
        for effect, step in zip(self.drink.effects, self.steps, strict=True):
            if isinstance(effect, Plan3DrinkSelectedCardMove):
                if not isinstance(step, Plan3DrinkSelectCardStep):
                    raise ValueError("selected card move must remain a select step")
            elif isinstance(effect, Plan3DrinkCardUpgradeEffect):
                if not isinstance(step, Plan3DrinkCardUpgradeStep):
                    raise ValueError("upgrade effect must remain an upgrade step")
            elif isinstance(effect, Plan3DrinkKernelEffect):
                if not isinstance(step, Plan3DrinkKernelStep):
                    raise ValueError("kernel effect must remain a kernel step")
            elif not isinstance(step, Plan3DrinkNumericStep):
                raise ValueError("numeric effect must remain a numeric step")
            if step.effect != effect:
                raise ValueError("use step effect order differs from Master")


def _catalog_effect_to_plan3(
    effect: DrinkEffectCatalogEntry,
    *, database: Path = DEFAULT_DATABASE,
) -> Plan3DrinkEffect:
    """Convert only a proven catalog shape into the legacy runtime types."""

    kernel = effect.effect_type in _KERNEL_EFFECT_TYPES or (
        effect.effect_type == EFFECT_CARD_MOVE and effect.source_id ==
        "e_effect-exam_card_move-p_card_search-deck_grave-idol-unique-1-hand-all-0_0")
    if kernel:
        if effect.source_kind != "exam":
            raise ValueError("drink kernel requires an exam source:" + effect.source_id)
        value = Plan3DrinkKernelEffect(effect.source_id, effect.effect_type, Path(database))
        value.resolve_contract()
        return value
    effect.require_plan3_supported()
    if effect.effect_type == "ProduceExamEffectType_ExamCardUpgrade":
        result = Plan3DrinkCardUpgradeEffect(effect.source_id, database)
        result.resolve_contract()
        return result
    if effect.effect_type in _NUMERIC_EFFECT_TYPES:
        return Plan3DrinkNumericEffect(
            id=effect.source_id,
            effect_type=effect.effect_type,
            value1=effect.effect_value1,
            value2=effect.effect_value2,
            effect_count=effect.effect_count,
            effect_turn=effect.effect_turn,
            status_enchant_id=effect.produce_exam_status_enchant_id,
            chain_effect_id=effect.chain_produce_exam_effect_id,
        )
    if effect.effect_type == EFFECT_CARD_MOVE:
        return Plan3DrinkSelectedCardMove(
            id=effect.source_id,
            card_search_id=_nonempty(
                effect.produce_card_search_id,
                f"{effect.source_id}.produceCardSearchId",
            ),
            destination=_nonempty(
                effect.move_position_type,
                f"{effect.source_id}.movePositionType",
            ),
            pick_range_type=_nonempty(
                effect.pick_range_type,
                f"{effect.source_id}.pickRangeType",
            ),
            pick_count_min=_integer(
                effect.pick_count_min,
                f"{effect.source_id}.pickCountMin",
                minimum=0,
            ),
            pick_count_max=_integer(
                effect.pick_count_max,
                f"{effect.source_id}.pickCountMax",
                minimum=0,
            ),
        )
    # The catalog classifier should have rejected this before this branch.
    raise ValueError(f"unsupported drink effect shape: {effect.source_id}")


def load_plan3_drink(
    drink_id: str,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> Plan3Drink:
    """Load one drink and preserve its `produceDrinkEffectIds` order."""

    drink_id = _nonempty(drink_id, "drink_id")
    # ``master_dir`` remains in the signature for callers that used the old
    # YAML loader.  The runtime now consumes only the rebuilt typed catalog.
    del master_dir
    catalog = load_drink_catalog(database)
    row = catalog.get_drink(drink_id)
    if row.plan_type not in {"ProducePlanType_Common", "ProducePlanType_Plan3"}:
        raise ValueError("drink is not compatible with Plan3:" + drink_id)
    effects = tuple(
        _catalog_effect_to_plan3(reference.effect, database=database)
        for reference in row.effect_refs
    )
    return Plan3Drink(
        id=drink_id,
        name=row.name,
        plan_type=row.plan_type,
        rarity=row.rarity,
        effects=effects,
    )


def load_plan3_drink_inventory(
    drink_ids: tuple[str, ...],
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> Plan3DrinkInventory:
    """Load native `drinkList` order without merging duplicate IDs."""

    if not isinstance(drink_ids, tuple):
        raise TypeError("drink_ids must be a tuple")
    return Plan3DrinkInventory(
        tuple(
            load_plan3_drink(
                drink_id, master_dir=master_dir, database=database
            )
            for drink_id in drink_ids
        )
    )


def use_plan3_drink(
    inventory: Plan3DrinkInventory,
    slot_index: int,
    *,
    selected_card_guid: str | None = None,
) -> Plan3DrinkUse:
    """Consume one slot and materialize its Master-ordered action steps."""

    if not isinstance(inventory, Plan3DrinkInventory):
        raise TypeError("inventory must be Plan3DrinkInventory")
    _integer(slot_index, "slot_index", minimum=0)
    if slot_index >= len(inventory.drinks):
        raise IndexError("drink slot is outside the inventory")
    drink = inventory.drinks[slot_index]
    selection_count = sum(
        isinstance(effect, Plan3DrinkSelectedCardMove)
        for effect in drink.effects
    )
    if selection_count > 1:
        raise ValueError("multiple selected moves in one drink are unsupported")
    if selection_count and selected_card_guid is None:
        raise ValueError("selected_card_guid is required for this drink")
    if not selection_count and selected_card_guid is not None:
        raise ValueError("selected_card_guid is not accepted by this drink")

    steps: list[Plan3DrinkUseStep] = []
    for effect in drink.effects:
        if isinstance(effect, Plan3DrinkSelectedCardMove):
            assert selected_card_guid is not None
            steps.append(Plan3DrinkSelectCardStep(effect, selected_card_guid))
        elif isinstance(effect, Plan3DrinkCardUpgradeEffect):
            steps.append(Plan3DrinkCardUpgradeStep(effect))
        elif isinstance(effect, Plan3DrinkKernelEffect):
            steps.append(Plan3DrinkKernelStep(effect))
        else:
            steps.append(Plan3DrinkNumericStep(effect))
    after = Plan3DrinkInventory(
        inventory.drinks[:slot_index] + inventory.drinks[slot_index + 1 :]
    )
    return Plan3DrinkUse(
        before=inventory,
        after=after,
        slot_index=slot_index,
        drink=drink,
        steps=tuple(steps),
    )


__all__ = [
    "DEFAULT_MASTER_DIR",
    "EFFECT_BLOCK",
    "EFFECT_CARD_MOVE",
    "EFFECT_CARD_PLAY_AGGRESSIVE",
    "EFFECT_CONCENTRATION",
    "EFFECT_EXTRA_TURN",
    "EFFECT_FULL_POWER_POINT",
    "EFFECT_LESSON",
    "EFFECT_PLAYABLE_VALUE_ADD",
    "EFFECT_PRESERVATION",
    "EFFECT_REVIEW",
    "EFFECT_STAMINA_CONSUMPTION_DOWN",
    "EFFECT_STAMINA_CONSUMPTION_ADD",
    "EFFECT_STAMINA_RECOVER_FIX",
    "EFFECT_STAMINA_REDUCE_FIX",
    "MOVE_HOLD",
    "PICK_SELECT",
    "SEARCH_DECK_GRAVE",
    "Plan3Drink",
    "Plan3DrinkEffect",
    "Plan3DrinkCardUpgradeEffect",
    "Plan3DrinkCardUpgradeStep",
    "Plan3DrinkKernelEffect",
    "Plan3DrinkKernelStep",
    "Plan3DrinkInventory",
    "Plan3DrinkNumericEffect",
    "Plan3DrinkNumericStep",
    "Plan3DrinkSelectCardStep",
    "Plan3DrinkSelectedCardMove",
    "Plan3DrinkUse",
    "Plan3DrinkUseStep",
    "load_plan3_drink",
    "load_plan3_drink_inventory",
    "use_plan3_drink",
]
