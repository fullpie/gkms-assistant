"""Standalone native stamina/cost contracts for the Plan2 catalog.

This module is an integration leaf.  It compiles the current Common+Plan2
Master rows that install stamina-consumption statuses, the direct fixed
stamina recovery rows, and the two non-stamina card-cost families.  It does
not import the native horizon, the central catalog compiler, or a mutable
Plan2 state.

The public boundary deliberately separates three native operations:

* compile a strict Master row into a typed handoff;
* preview card-cost legality without mutating resources;
* commit a previously previewable payment or execute one stamina handoff.

Unknown effect/cost shapes fail closed.  Percentage/fixed stamina payment is
delegated to :mod:`native_exam_formula`; Add status merging and lifetime are
delegated to the reviewed standalone Add adapter; fixed recovery is delegated
to the reviewed standalone recovery adapter.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import TypeAlias

from .logic_engine import (
    COST_GOOD_IMPRESSION,
    COST_MOTIVATION,
    COST_STAMINA,
    PLAN_COMMON,
    PLAN_LOGIC,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    IdolStatusType,
    StaminaPayment,
    StaminaPaymentStatus,
    calculate_buff_cost,
    calculate_effective_stamina_cost,
    get_stamina_cost,
    split_stamina_payment,
)
from .plan2_stamina_consumption_add import (
    StaminaConsumptionAddEffect,
    StaminaConsumptionAddRuntime,
    StaminaConsumptionAddSettings,
    install_stamina_consumption_add,
    simulate_turn_start as simulate_add_turn_start,
)
from .plan2_stamina_recover_fix import (
    StaminaRecoverFixContract,
    StaminaRecoverFixEvaluation,
    StaminaRecoverFixRuntime,
    evaluate_stamina_recover_fix,
)


EFFECT_STAMINA_CONSUMPTION_DOWN = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_STAMINA_CONSUMPTION_DOWN_FIX = (
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix"
)
EFFECT_STAMINA_CONSUMPTION_ADD = (
    "ProduceExamEffectType_ExamStaminaConsumptionAdd"
)
EFFECT_STAMINA_RECOVER_FIX = "ProduceExamEffectType_ExamStaminaRecoverFix"

STAMINA_EFFECT_TYPES = frozenset(
    {
        EFFECT_STAMINA_CONSUMPTION_DOWN,
        EFFECT_STAMINA_CONSUMPTION_DOWN_FIX,
        EFFECT_STAMINA_CONSUMPTION_ADD,
        EFFECT_STAMINA_RECOVER_FIX,
    }
)
BUFF_COST_TYPES = frozenset({COST_GOOD_IMPRESSION, COST_MOTIVATION})
PLAN_TYPES = (PLAN_COMMON, PLAN_LOGIC)
PERMANENT_TURN = -1

CARD_PLAY_STAMINA_TRIGGER_ID = (
    "e_trigger-exam_card_play-stamina_up_multiple-500"
)
CARD_PLAY_STAMINA_TRIGGER_OWNER = "plan2_card_play_stamina_trigger"
DIRECT_EFFECT_OWNER = "plan2_native_catalog_stamina"

CURRENT_MASTER_DIRECT_FAMILY_COUNTS = {
    EFFECT_STAMINA_CONSUMPTION_DOWN: 39,
    EFFECT_STAMINA_CONSUMPTION_DOWN_FIX: 24,
    EFFECT_STAMINA_CONSUMPTION_ADD: 16,
    EFFECT_STAMINA_RECOVER_FIX: 12,
}
CURRENT_MASTER_DELEGATED_FAMILY_COUNTS = {
    EFFECT_STAMINA_CONSUMPTION_DOWN_FIX: 4,
}
CURRENT_MASTER_COST_TYPE_COUNTS = {
    COST_GOOD_IMPRESSION: 16,
    COST_MOTIVATION: 41,
}


class Plan2NativeStaminaCatalogError(ValueError):
    """A Master/runtime input is outside this exact standalone boundary."""


class StaminaEffectFamily(str, Enum):
    DOWN = EFFECT_STAMINA_CONSUMPTION_DOWN
    DOWN_FIX = EFFECT_STAMINA_CONSUMPTION_DOWN_FIX
    ADD = EFFECT_STAMINA_CONSUMPTION_ADD
    RECOVER_FIX = EFFECT_STAMINA_RECOVER_FIX


class PlayOrigin(str, Enum):
    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"


def _i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeStaminaCatalogError(f"{label} is outside Int32")
    return value


def _nonnegative_i32(value: object, label: str) -> int:
    result = _i32(value, label)
    if result < 0:
        raise Plan2NativeStaminaCatalogError(f"{label} must be non-negative")
    return result


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "text" if empty else "non-empty text"
        raise TypeError(f"{label} must be {qualifier}")
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(str(value)) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise Plan2NativeStaminaCatalogError(f"{label} is invalid JSON") from error
    if not isinstance(parsed, Mapping):
        raise Plan2NativeStaminaCatalogError(f"{label} must be a JSON object")
    return parsed


def _json_array(value: object, label: str) -> tuple[object, ...]:
    try:
        parsed = json.loads(str(value)) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise Plan2NativeStaminaCatalogError(f"{label} is invalid JSON") from error
    if not isinstance(parsed, list):
        raise Plan2NativeStaminaCatalogError(f"{label} must be a JSON array")
    return tuple(parsed)


def _wrapped_i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _validate_raw_effect(row: Mapping[str, object]) -> None:
    effect_id = _text(row.get("id"), "effect.id")
    raw = _json_object(row.get("raw_json"), f"{effect_id}.raw_json")
    expected = {
        "id": effect_id,
        "effectType": row.get("effect_type"),
        "effectValue1": row.get("value1"),
        "effectValue2": row.get("value2"),
        "effectCount": row.get("effect_count"),
        "effectTurn": row.get("effect_turn"),
        "produceExamStatusEnchantId": row.get("status_enchant_id"),
        "chainProduceExamEffectId": row.get("chain_effect_id"),
    }
    for key, value in expected.items():
        if key not in raw or raw[key] != value:
            raise Plan2NativeStaminaCatalogError(
                f"{effect_id}: raw {key} does not match the normalized Master row"
            )
    empty_fields = {
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": "",
        "movePositionType": "ProduceCardMovePositionType_Unknown",
        "produceCardSearchId2": "",
        "chainProduceExamEffectIds": [],
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
    }
    for key, value in empty_fields.items():
        if raw.get(key) != value:
            raise Plan2NativeStaminaCatalogError(
                f"{effect_id}: unsupported nested Master field {key}"
            )


@dataclass(frozen=True, slots=True)
class StaminaConsumptionDownEffect:
    effect_id: str
    effect_turn: int
    value1: int = 0
    value2: int = 0
    effect_count: int = 0
    effect_type: str = EFFECT_STAMINA_CONSUMPTION_DOWN

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        if self.effect_type != EFFECT_STAMINA_CONSUMPTION_DOWN:
            raise Plan2NativeStaminaCatalogError("wrong Down effect type")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _i32(getattr(self, name), name)
        if self.value1 or self.value2 or self.effect_count:
            raise Plan2NativeStaminaCatalogError("unsupported Down value/count shape")
        if self.effect_turn != PERMANENT_TURN and self.effect_turn < 1:
            raise Plan2NativeStaminaCatalogError(
                "Down turn must be -1 (permanent) or positive"
            )


@dataclass(frozen=True, slots=True)
class StaminaConsumptionDownFixEffect:
    effect_id: str
    value1: int
    value2: int = 0
    effect_count: int = 0
    effect_turn: int = PERMANENT_TURN
    effect_type: str = EFFECT_STAMINA_CONSUMPTION_DOWN_FIX

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        if self.effect_type != EFFECT_STAMINA_CONSUMPTION_DOWN_FIX:
            raise Plan2NativeStaminaCatalogError("wrong DownFix effect type")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _i32(getattr(self, name), name)
        if self.value1 < 1 or self.value2 or self.effect_count:
            raise Plan2NativeStaminaCatalogError("unsupported DownFix value/count shape")
        if self.effect_turn != PERMANENT_TURN:
            raise Plan2NativeStaminaCatalogError("DownFix must be permanent")


StaminaEffectContract: TypeAlias = (
    StaminaConsumptionDownEffect
    | StaminaConsumptionDownFixEffect
    | StaminaConsumptionAddEffect
    | StaminaRecoverFixContract
)


def _compile_effect(row: Mapping[str, object]) -> StaminaEffectContract:
    _validate_raw_effect(row)
    effect_type = _text(row.get("effect_type"), "effect.effect_type")
    common = {
        "effect_id": _text(row.get("id"), "effect.id"),
        "value1": _i32(row.get("value1"), "effect.value1"),
        "value2": _i32(row.get("value2"), "effect.value2"),
        "effect_count": _i32(row.get("effect_count"), "effect.effect_count"),
        "effect_turn": _i32(row.get("effect_turn"), "effect.effect_turn"),
    }
    status_id = _text(row.get("status_enchant_id"), "status_enchant_id", empty=True)
    chain_id = _text(row.get("chain_effect_id"), "chain_effect_id", empty=True)
    if status_id or chain_id:
        raise Plan2NativeStaminaCatalogError(
            f"{common['effect_id']}: nested status/chain is unsupported"
        )
    if effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
        return StaminaConsumptionDownEffect(effect_type=effect_type, **common)
    if effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX:
        return StaminaConsumptionDownFixEffect(effect_type=effect_type, **common)
    if effect_type == EFFECT_STAMINA_CONSUMPTION_ADD:
        return StaminaConsumptionAddEffect.from_mapping(dict(row))
    if effect_type == EFFECT_STAMINA_RECOVER_FIX:
        return StaminaRecoverFixContract(
            effect_id=common["effect_id"],
            effect_value1=common["value1"],
            effect_value2=common["value2"],
            effect_count=common["effect_count"],
            effect_turn=common["effect_turn"],
            effect_type=effect_type,
            status_enchant_id=status_id,
            chain_effect_id=chain_id,
        )
    raise Plan2NativeStaminaCatalogError(f"unknown stamina effect type: {effect_type}")


def effect_family(contract: StaminaEffectContract) -> StaminaEffectFamily:
    if isinstance(contract, StaminaConsumptionDownEffect):
        return StaminaEffectFamily.DOWN
    if isinstance(contract, StaminaConsumptionDownFixEffect):
        return StaminaEffectFamily.DOWN_FIX
    if isinstance(contract, StaminaConsumptionAddEffect):
        return StaminaEffectFamily.ADD
    if isinstance(contract, StaminaRecoverFixContract):
        return StaminaEffectFamily.RECOVER_FIX
    raise TypeError("contract must be a stamina effect contract")


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeStaminaEffectHandoff:
    card_id: str
    upgrade: int
    slot_index: int
    effect_id: str
    effect_type: str
    owner: str
    trigger_id: str = ""
    companion_effect_ids: tuple[str, ...] = ()
    card_trigger_id: str = ""

    def __post_init__(self) -> None:
        _text(self.card_id, "card_id")
        _nonnegative_i32(self.upgrade, "upgrade")
        _nonnegative_i32(self.slot_index, "slot_index")
        _text(self.effect_id, "effect_id")
        if self.effect_type not in STAMINA_EFFECT_TYPES:
            raise Plan2NativeStaminaCatalogError("unknown handoff effect type")
        _text(self.owner, "owner")
        _text(self.trigger_id, "trigger_id", empty=True)
        _text(self.card_trigger_id, "card_trigger_id", empty=True)
        if any(not isinstance(value, str) or not value for value in self.companion_effect_ids):
            raise TypeError("companion_effect_ids must contain non-empty text")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def direct(self) -> bool:
        return not self.trigger_id and self.owner == DIRECT_EFFECT_OWNER

    @property
    def has_companion_requirements(self) -> bool:
        return bool(self.companion_effect_ids or self.card_trigger_id)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeCardCost:
    card_id: str
    upgrade: int
    cost_type: str
    base_cost: int
    stamina: int = 0
    force_stamina: int = 0
    penetrate: bool = False
    card_trigger_id: str = ""
    companion_effect_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.card_id, "card_id")
        _nonnegative_i32(self.upgrade, "upgrade")
        _text(self.cost_type, "cost_type")
        _nonnegative_i32(self.base_cost, "base_cost")
        _nonnegative_i32(self.stamina, "stamina")
        _nonnegative_i32(self.force_stamina, "force_stamina")
        if not isinstance(self.penetrate, bool):
            raise TypeError("penetrate must be bool")
        _text(self.card_trigger_id, "card_trigger_id", empty=True)
        if any(not isinstance(value, str) or not value for value in self.companion_effect_ids):
            raise TypeError("companion_effect_ids must contain non-empty text")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def has_companion_requirements(self) -> bool:
        return bool(self.card_trigger_id or self.companion_effect_ids)


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaFamilyAccounting:
    effect_type: str
    affected: int
    direct: int
    delegated: int

    def __post_init__(self) -> None:
        if self.effect_type not in STAMINA_EFFECT_TYPES:
            raise Plan2NativeStaminaCatalogError("unknown accounting family")
        for name in ("affected", "direct", "delegated"):
            _nonnegative_i32(getattr(self, name), name)
        if self.affected != self.direct + self.delegated:
            raise Plan2NativeStaminaCatalogError("family accounting does not reconcile")


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogStamina:
    master_database: str
    effects: tuple[StaminaEffectContract, ...]
    direct_handoffs: tuple[Plan2NativeStaminaEffectHandoff, ...]
    delegated_handoffs: tuple[Plan2NativeStaminaEffectHandoff, ...]
    costs: tuple[Plan2NativeCardCost, ...]
    family_accounting: tuple[Plan2NativeStaminaFamilyAccounting, ...]

    def __post_init__(self) -> None:
        _text(self.master_database, "master_database")
        all_handoffs = (*self.direct_handoffs, *self.delegated_handoffs)
        keys = [(value.ref, value.slot_index) for value in all_handoffs]
        if len(keys) != len(set(keys)):
            raise Plan2NativeStaminaCatalogError("duplicate stamina handoff slot")
        refs = [value.ref for value in self.costs]
        if len(refs) != len(set(refs)):
            raise Plan2NativeStaminaCatalogError("duplicate cost version")

    @property
    def effect_by_id(self) -> dict[str, StaminaEffectContract]:
        return {getattr(value, "effect_id"): value for value in self.effects}

    @property
    def cost_by_ref(self) -> dict[tuple[str, int], Plan2NativeCardCost]:
        return {value.ref: value for value in self.costs}

    @property
    def direct_version_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(value.ref for value in self.direct_handoffs))

    @property
    def cost_version_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(value.ref for value in self.costs))

    @property
    def direct_family_counts(self) -> dict[str, int]:
        return {value.effect_type: value.direct for value in self.family_accounting}

    @property
    def delegated_family_counts(self) -> dict[str, int]:
        return {
            value.effect_type: value.delegated
            for value in self.family_accounting
            if value.delegated
        }

    @property
    def cost_type_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(value.cost_type for value in self.costs).items()))

    def handoffs_for(
        self, ref: tuple[str, int]
    ) -> tuple[Plan2NativeStaminaEffectHandoff, ...]:
        return tuple(
            value
            for value in (*self.direct_handoffs, *self.delegated_handoffs)
            if value.ref == ref
        )


def _validate_card_raw(row: Mapping[str, object]) -> Mapping[str, object]:
    card_id = _text(row.get("id"), "card.id")
    upgrade = _nonnegative_i32(row.get("upgrade_count"), "card.upgrade_count")
    raw = _json_object(row.get("raw_json"), f"{card_id}@{upgrade}.raw_json")
    play_effects = _json_array(
        row.get("play_effects_json"), f"{card_id}@{upgrade}.play_effects_json"
    )
    expected = {
        "id": card_id,
        "upgradeCount": upgrade,
        "planType": row.get("plan_type"),
        "category": row.get("category"),
        "stamina": row.get("stamina"),
        "costType": row.get("cost_type"),
        "costValue": row.get("cost_value"),
        "forceStamina": row.get("force_stamina", raw.get("forceStamina")),
        "playProduceExamTriggerId": row.get("play_trigger_id"),
        "playEffects": list(play_effects),
    }
    for key, value in expected.items():
        if key not in raw or raw[key] != value:
            raise Plan2NativeStaminaCatalogError(
                f"{card_id}@{upgrade}: raw {key} does not match normalized Master"
            )
    return raw


def compile_plan2_native_catalog_stamina(
    *,
    database: str | Path = DEFAULT_DATABASE,
    enforce_current_master_counts: bool = True,
) -> Plan2NativeCatalogStamina:
    """Compile the exact current stamina/cost slice from Master, read-only."""

    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        effect_rows = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM effect WHERE effect_type IN (?, ?, ?, ?) ORDER BY id",
                tuple(sorted(STAMINA_EFFECT_TYPES)),
            )
        )
        card_rows = tuple(
            dict(row)
            for row in connection.execute(
                """
                SELECT id, upgrade_count, plan_type, category, stamina,
                       cost_type, cost_value, play_trigger_id,
                       play_effects_json, raw_json
                  FROM card
                 WHERE plan_type IN (?, ?)
                 ORDER BY id, upgrade_count
                """,
                PLAN_TYPES,
            )
        )

    effects = tuple(_compile_effect(row) for row in effect_rows)
    effects_by_id = {getattr(value, "effect_id"): value for value in effects}
    effect_type_by_id = {
        getattr(value, "effect_id"): effect_family(value).value for value in effects
    }
    direct: list[Plan2NativeStaminaEffectHandoff] = []
    delegated: list[Plan2NativeStaminaEffectHandoff] = []
    costs: list[Plan2NativeCardCost] = []

    for row in card_rows:
        card_id = str(row["id"])
        upgrade = int(row["upgrade_count"])
        links = _json_array(row["play_effects_json"], f"{card_id}@{upgrade}.effects")
        target_link_present = any(
            isinstance(link, Mapping)
            and link.get("produceExamEffectId") in effects_by_id
            for link in links
        )
        cost_type = str(row["cost_type"])
        if not target_link_present and cost_type not in BUFF_COST_TYPES:
            continue
        raw = _validate_card_raw(row)
        effect_ids: list[str] = []
        for index, link in enumerate(links):
            if not isinstance(link, Mapping):
                raise Plan2NativeStaminaCatalogError(
                    f"{card_id}@{upgrade}: play-effect link {index} is not an object"
                )
            effect_id = _text(
                link.get("produceExamEffectId"),
                f"{card_id}@{upgrade}.effect[{index}].id",
            )
            trigger_id = _text(
                link.get("produceExamTriggerId", ""),
                f"{card_id}@{upgrade}.effect[{index}].trigger",
                empty=True,
            )
            effect_ids.append(effect_id)
            effect_type = effect_type_by_id.get(effect_id)
            if effect_type is None:
                continue
            companion_ids = tuple(
                str(other.get("produceExamEffectId", ""))
                for other_index, other in enumerate(links)
                if other_index != index and isinstance(other, Mapping)
            )
            if any(not value for value in companion_ids):
                raise Plan2NativeStaminaCatalogError(
                    f"{card_id}@{upgrade}: empty companion effect id"
                )
            if not trigger_id:
                handoff = Plan2NativeStaminaEffectHandoff(
                    card_id,
                    upgrade,
                    index,
                    effect_id,
                    effect_type,
                    DIRECT_EFFECT_OWNER,
                    companion_effect_ids=companion_ids,
                    card_trigger_id=str(row["play_trigger_id"]),
                )
                direct.append(handoff)
            elif (
                effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN_FIX
                and trigger_id == CARD_PLAY_STAMINA_TRIGGER_ID
            ):
                delegated.append(
                    Plan2NativeStaminaEffectHandoff(
                        card_id,
                        upgrade,
                        index,
                        effect_id,
                        effect_type,
                        CARD_PLAY_STAMINA_TRIGGER_OWNER,
                        trigger_id=trigger_id,
                        companion_effect_ids=companion_ids,
                        card_trigger_id=str(row["play_trigger_id"]),
                    )
                )
            else:
                raise Plan2NativeStaminaCatalogError(
                    f"{card_id}@{upgrade}: unowned stamina trigger {trigger_id}"
                )

        if cost_type in BUFF_COST_TYPES:
            force_stamina = _nonnegative_i32(
                raw.get("forceStamina"), f"{card_id}@{upgrade}.forceStamina"
            )
            stamina = _nonnegative_i32(row["stamina"], f"{card_id}@{upgrade}.stamina")
            if stamina or force_stamina:
                raise Plan2NativeStaminaCatalogError(
                    f"{card_id}@{upgrade}: mixed buff/stamina cost shape is unsupported"
                )
            costs.append(
                Plan2NativeCardCost(
                    card_id=card_id,
                    upgrade=upgrade,
                    cost_type=cost_type,
                    base_cost=_nonnegative_i32(
                        row["cost_value"], f"{card_id}@{upgrade}.costValue"
                    ),
                    stamina=stamina,
                    force_stamina=force_stamina,
                    penetrate=False,
                    card_trigger_id=str(row["play_trigger_id"]),
                    companion_effect_ids=tuple(effect_ids),
                )
            )

    direct.sort(key=lambda value: (value.card_id, value.upgrade, value.slot_index))
    delegated.sort(key=lambda value: (value.card_id, value.upgrade, value.slot_index))
    costs.sort(key=lambda value: value.ref)
    direct_counts = Counter(value.effect_type for value in direct)
    delegated_counts = Counter(value.effect_type for value in delegated)
    accounting = tuple(
        Plan2NativeStaminaFamilyAccounting(
            effect_type=effect_type,
            affected=direct_counts[effect_type] + delegated_counts[effect_type],
            direct=direct_counts[effect_type],
            delegated=delegated_counts[effect_type],
        )
        for effect_type in sorted(STAMINA_EFFECT_TYPES)
    )
    catalog = Plan2NativeCatalogStamina(
        master_database=str(database_path),
        effects=effects,
        direct_handoffs=tuple(direct),
        delegated_handoffs=tuple(delegated),
        costs=tuple(costs),
        family_accounting=accounting,
    )
    if enforce_current_master_counts:
        if catalog.direct_family_counts != CURRENT_MASTER_DIRECT_FAMILY_COUNTS:
            raise Plan2NativeStaminaCatalogError(
                "current Master direct stamina-family counts drifted: "
                f"{catalog.direct_family_counts!r}"
            )
        if catalog.delegated_family_counts != CURRENT_MASTER_DELEGATED_FAMILY_COUNTS:
            raise Plan2NativeStaminaCatalogError(
                "current Master delegated stamina-family counts drifted: "
                f"{catalog.delegated_family_counts!r}"
            )
        if catalog.cost_type_counts != CURRENT_MASTER_COST_TYPE_COUNTS:
            raise Plan2NativeStaminaCatalogError(
                f"current Master cost-family counts drifted: {catalog.cost_type_counts!r}"
            )
    return catalog


load_plan2_native_catalog_stamina = compile_plan2_native_catalog_stamina


@dataclass(frozen=True, slots=True)
class StaminaConsumptionDownLayer:
    turn: int
    is_passing_turn_start: bool = False
    status_uid: int = 1
    is_turn_limited: bool | None = None

    def __post_init__(self) -> None:
        _i32(self.turn, "Down status turn")
        if not isinstance(self.is_passing_turn_start, bool):
            raise TypeError("is_passing_turn_start must be bool")
        if _i32(self.status_uid, "status_uid") < 1:
            raise Plan2NativeStaminaCatalogError("status_uid must be positive")
        if self.is_turn_limited is None:
            object.__setattr__(self, "is_turn_limited", self.turn != PERMANENT_TURN)
        elif not isinstance(self.is_turn_limited, bool):
            raise TypeError("is_turn_limited must be bool or None")
        elif not self.is_turn_limited and self.turn != PERMANENT_TURN:
            raise Plan2NativeStaminaCatalogError("only turn -1 may be permanent")

    @property
    def is_permanent(self) -> bool:
        return not bool(self.is_turn_limited)


@dataclass(frozen=True, slots=True)
class StaminaConsumptionDownFixLayer:
    value: int
    status_uid: int

    def __post_init__(self) -> None:
        if _i32(self.value, "DownFix value") < 1:
            raise Plan2NativeStaminaCatalogError("DownFix value must be positive")
        if _i32(self.status_uid, "status_uid") < 1:
            raise Plan2NativeStaminaCatalogError("status_uid must be positive")


@dataclass(frozen=True, slots=True)
class Plan2StaminaModifierRuntime:
    down: StaminaConsumptionDownLayer | None = None
    down_fix_layers: tuple[StaminaConsumptionDownFixLayer, ...] = ()
    add: StaminaConsumptionAddRuntime = StaminaConsumptionAddRuntime()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        if self.down is not None and not isinstance(
            self.down, StaminaConsumptionDownLayer
        ):
            raise TypeError("down must be StaminaConsumptionDownLayer or None")
        if not isinstance(self.down_fix_layers, tuple) or any(
            not isinstance(value, StaminaConsumptionDownFixLayer)
            for value in self.down_fix_layers
        ):
            raise TypeError("down_fix_layers has the wrong shape")
        if not isinstance(self.add, StaminaConsumptionAddRuntime):
            raise TypeError("add must be StaminaConsumptionAddRuntime")
        if _i32(self.next_status_uid, "next_status_uid") < 1:
            raise Plan2NativeStaminaCatalogError("next_status_uid must be positive")
        uids = [
            *(value.status_uid for value in self.down_fix_layers),
            *((self.down.status_uid,) if self.down is not None else ()),
            *((self.add.layer.status_uid,) if self.add.layer is not None else ()),
        ]
        if len(uids) != len(set(uids)):
            raise Plan2NativeStaminaCatalogError("duplicate modifier status UID")
        if uids and self.next_status_uid <= max(uids):
            raise Plan2NativeStaminaCatalogError(
                "next_status_uid must exceed native modifier UIDs"
            )

    @property
    def consumption_down(self) -> bool:
        return self.down is not None

    @property
    def consumption_add(self) -> bool:
        return self.add.is_active

    @property
    def consumption_down_fix(self) -> int:
        total = 0
        for layer in self.down_fix_layers:
            total += layer.value
            if total > INT32_MAX:
                raise Plan2NativeStaminaCatalogError("DownFix sum is outside Int32")
        return total


@dataclass(frozen=True, slots=True)
class Plan2StaminaModifierExecution:
    before: Plan2StaminaModifierRuntime
    after: Plan2StaminaModifierRuntime
    contract: StaminaEffectContract | None
    executable: bool
    installed: bool
    reason: str | None = None


def apply_plan2_stamina_modifier(
    runtime: Plan2StaminaModifierRuntime,
    contract: object,
    *,
    block_add_status: bool = False,
) -> Plan2StaminaModifierExecution:
    """Install one Down/DownFix/Add status; unknown/recovery rows fail closed."""

    if not isinstance(runtime, Plan2StaminaModifierRuntime):
        raise TypeError("runtime must be Plan2StaminaModifierRuntime")
    if not isinstance(block_add_status, bool):
        raise TypeError("block_add_status must be bool")
    if isinstance(contract, StaminaConsumptionDownEffect):
        if block_add_status:
            return Plan2StaminaModifierExecution(
                runtime, runtime, contract, True, False, "status-add-blocked"
            )
        current = runtime.down
        if current is None:
            uid = runtime.next_status_uid
            after = replace(
                runtime,
                down=StaminaConsumptionDownLayer(contract.effect_turn, status_uid=uid),
                next_status_uid=uid + 1,
            )
        elif current.is_permanent:
            after = runtime
        else:
            after = replace(
                runtime,
                down=replace(
                    current, turn=_wrapped_i32(current.turn + contract.effect_turn)
                ),
            )
        return Plan2StaminaModifierExecution(
            runtime, after, contract, True, after != runtime
        )
    if isinstance(contract, StaminaConsumptionDownFixEffect):
        if block_add_status:
            return Plan2StaminaModifierExecution(
                runtime, runtime, contract, True, False, "status-add-blocked"
            )
        uid = runtime.next_status_uid
        after = replace(
            runtime,
            down_fix_layers=(
                *runtime.down_fix_layers,
                StaminaConsumptionDownFixLayer(contract.value1, uid),
            ),
            next_status_uid=uid + 1,
        )
        # Force the exact Int32 accounting boundary now, not at later payment.
        _ = after.consumption_down_fix
        return Plan2StaminaModifierExecution(runtime, after, contract, True, True)
    if isinstance(contract, StaminaConsumptionAddEffect):
        synchronized_add = StaminaConsumptionAddRuntime(
            layers=runtime.add.layers,
            next_status_uid=max(runtime.add.next_status_uid, runtime.next_status_uid),
        )
        execution = install_stamina_consumption_add(
            synchronized_add, contract, block_add_status=block_add_status
        )
        after = replace(
            runtime,
            add=execution.after,
            next_status_uid=max(
                runtime.next_status_uid, execution.after.next_status_uid
            ),
        )
        return Plan2StaminaModifierExecution(
            runtime,
            after,
            contract,
            True,
            execution.installed,
            None if execution.installed else "status-add-blocked-or-permanent",
        )
    return Plan2StaminaModifierExecution(
        runtime,
        runtime,
        contract if isinstance(contract, StaminaRecoverFixContract) else None,
        False,
        False,
        "unknown-or-non-modifier-effect",
    )


@dataclass(frozen=True, slots=True)
class Plan2StaminaTurnStartTransition:
    before: Plan2StaminaModifierRuntime
    after: Plan2StaminaModifierRuntime
    fresh_status_uids: tuple[int, ...] = ()
    spent_status_uids: tuple[int, ...] = ()
    expired_status_uids: tuple[int, ...] = ()
    permanent_status_uids: tuple[int, ...] = ()


def advance_plan2_stamina_turn_start(
    runtime: Plan2StaminaModifierRuntime,
) -> Plan2StaminaTurnStartTransition:
    """Spend finite Down/Add statuses at the native fresh TurnStart boundary."""

    if not isinstance(runtime, Plan2StaminaModifierRuntime):
        raise TypeError("runtime must be Plan2StaminaModifierRuntime")
    fresh: list[int] = []
    spent: list[int] = []
    expired: list[int] = []
    permanent = [value.status_uid for value in runtime.down_fix_layers]
    down = runtime.down
    next_down = down
    if down is not None:
        if down.is_permanent:
            next_down = replace(down, is_passing_turn_start=True)
            permanent.append(down.status_uid)
        elif not down.is_passing_turn_start:
            if down.turn <= 0:
                next_down = None
                expired.append(down.status_uid)
            else:
                next_down = replace(down, is_passing_turn_start=True)
                fresh.append(down.status_uid)
        else:
            next_turn = _wrapped_i32(down.turn - 1)
            spent.append(down.status_uid)
            if next_turn <= 0:
                next_down = None
                expired.append(down.status_uid)
            else:
                next_down = replace(down, turn=next_turn)

    add_transition = simulate_add_turn_start(runtime.add)
    fresh.extend(add_transition.fresh_status_uids)
    spent.extend(add_transition.spent_status_uids)
    expired.extend(add_transition.expired_status_uids)
    permanent.extend(add_transition.permanent_status_uids)
    after = replace(runtime, down=next_down, add=add_transition.after)
    return Plan2StaminaTurnStartTransition(
        runtime,
        after,
        tuple(fresh),
        tuple(spent),
        tuple(expired),
        tuple(permanent),
    )


@dataclass(frozen=True, slots=True)
class Plan2CostResources:
    stamina: int
    max_stamina: int
    block: int = 0
    review: int = 0
    motivation: int = 0
    review_exists: bool = False
    review_consumption_sum: int = 0

    def __post_init__(self) -> None:
        for name in (
            "stamina",
            "max_stamina",
            "block",
            "review",
            "motivation",
            "review_consumption_sum",
        ):
            _nonnegative_i32(getattr(self, name), name)
        if self.stamina > self.max_stamina:
            raise Plan2NativeStaminaCatalogError("stamina exceeds max_stamina")
        if not isinstance(self.review_exists, bool):
            raise TypeError("review_exists must be bool")
        if self.review > 0 and not self.review_exists:
            raise Plan2NativeStaminaCatalogError(
                "positive review requires an active Review status"
            )


@dataclass(frozen=True, slots=True)
class Plan2NativeCostSettings:
    stamina: StaminaConsumptionAddSettings = StaminaConsumptionAddSettings()
    buff_consumption_down_permille: int = 0
    buff_consumption_add_permille: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.stamina, StaminaConsumptionAddSettings):
            raise TypeError("stamina must be StaminaConsumptionAddSettings")
        _i32(self.buff_consumption_down_permille, "buff down permille")
        _i32(self.buff_consumption_add_permille, "buff add permille")


@dataclass(frozen=True, slots=True)
class Plan2NativeCostRequest:
    cost: Plan2NativeCardCost
    resources: Plan2CostResources
    modifiers: Plan2StaminaModifierRuntime = Plan2StaminaModifierRuntime()
    origin: PlayOrigin = PlayOrigin.NORMAL
    consume_cost: bool | None = None
    search_override: int | None = None
    temporary_stamina_specify: tuple[int, ...] = ()
    idol_status_type: IdolStatusType = IdolStatusType.UNKNOWN
    idol_status_step: int = 1
    consumption_down_add: bool = False
    consumption_add_down: bool = False
    consumption_add_fix: int = 0
    reduce_change_threshold: int = -1
    reduce_change_active: bool = False
    buff_consumption_down: bool = False
    buff_consumption_add: bool = False
    settings: Plan2NativeCostSettings = Plan2NativeCostSettings()

    def __post_init__(self) -> None:
        if not isinstance(self.cost, Plan2NativeCardCost):
            raise TypeError("cost must be Plan2NativeCardCost")
        if not isinstance(self.resources, Plan2CostResources):
            raise TypeError("resources must be Plan2CostResources")
        if not isinstance(self.modifiers, Plan2StaminaModifierRuntime):
            raise TypeError("modifiers must be Plan2StaminaModifierRuntime")
        if not isinstance(self.origin, PlayOrigin):
            raise TypeError("origin must be PlayOrigin")
        if self.consume_cost is not None and not isinstance(self.consume_cost, bool):
            raise TypeError("consume_cost must be bool or None")
        if self.search_override is not None:
            _nonnegative_i32(self.search_override, "search_override")
        if not isinstance(self.temporary_stamina_specify, tuple):
            raise TypeError("temporary_stamina_specify must be a tuple")
        for value in self.temporary_stamina_specify:
            _nonnegative_i32(value, "temporary stamina specify")
        if not isinstance(self.idol_status_type, IdolStatusType):
            raise TypeError("idol_status_type must be IdolStatusType")
        _i32(self.idol_status_step, "idol_status_step")
        _i32(self.consumption_add_fix, "consumption_add_fix")
        _i32(self.reduce_change_threshold, "reduce_change_threshold")
        for name in (
            "consumption_down_add",
            "consumption_add_down",
            "reduce_change_active",
            "buff_consumption_down",
            "buff_consumption_add",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if not isinstance(self.settings, Plan2NativeCostSettings):
            raise TypeError("settings must be Plan2NativeCostSettings")

    @property
    def resolved_consume_cost(self) -> bool:
        if self.consume_cost is not None:
            return self.consume_cost
        return self.origin is PlayOrigin.NORMAL


@dataclass(frozen=True, slots=True)
class Plan2NativeCostPredicate:
    request: Plan2NativeCostRequest
    resolved: bool
    playable: bool
    consumes_cost: bool
    selected_base_cost: int | None
    effective_cost: int | None
    available_resource: int | None
    reason: str | None = None


def evaluate_plan2_native_cost_predicate(
    request: Plan2NativeCostRequest,
) -> Plan2NativeCostPredicate:
    """Preview native card-cost legality without consuming any status/resource."""

    if not isinstance(request, Plan2NativeCostRequest):
        raise TypeError("request must be Plan2NativeCostRequest")
    consumes = request.resolved_consume_cost
    if request.origin is PlayOrigin.NORMAL and not consumes:
        return Plan2NativeCostPredicate(
            request, False, False, False, None, None, None, "normal-play-must-consume-cost"
        )
    if not consumes:
        return Plan2NativeCostPredicate(
            request, True, True, False, 0, 0, None, None
        )

    cost_type = request.cost.cost_type
    if cost_type not in {COST_STAMINA, *BUFF_COST_TYPES}:
        return Plan2NativeCostPredicate(
            request, False, False, True, None, None, None, "unknown-cost-type"
        )
    selected = request.search_override
    if selected is None:
        if cost_type == COST_STAMINA:
            selected = get_stamina_cost(
                request.cost.base_cost,
                penetrate=request.cost.penetrate,
                temporary_specify=request.temporary_stamina_specify or None,
            )
        else:
            if request.temporary_stamina_specify:
                return Plan2NativeCostPredicate(
                    request,
                    False,
                    False,
                    True,
                    None,
                    None,
                    None,
                    "stamina-specify-on-buff-cost",
                )
            selected = request.cost.base_cost

    if cost_type == COST_STAMINA:
        status = StaminaPaymentStatus(
            idol_status_type=request.idol_status_type,
            idol_status_step=request.idol_status_step,
            consumption_down=request.modifiers.consumption_down,
            consumption_down_add=request.consumption_down_add,
            consumption_add=request.modifiers.consumption_add,
            consumption_add_down=request.consumption_add_down,
            consumption_add_fix=request.consumption_add_fix,
            consumption_down_fix=request.modifiers.consumption_down_fix,
            reduce_change_threshold=request.reduce_change_threshold,
            reduce_change_active=request.reduce_change_active,
        )
        effective = calculate_effective_stamina_cost(
            selected, status=status, settings=request.settings.stamina.native()
        )
        available = request.resources.stamina + (
            0 if request.cost.penetrate else request.resources.block
        )
    else:
        if selected < 1:
            effective = 0
        else:
            effective = calculate_buff_cost(
                selected,
                consumption_down=request.buff_consumption_down,
                consumption_down_permille=(
                    request.settings.buff_consumption_down_permille
                ),
                consumption_add=request.buff_consumption_add,
                consumption_add_permille=(
                    request.settings.buff_consumption_add_permille
                ),
            )
        if effective < 0:
            return Plan2NativeCostPredicate(
                request,
                False,
                False,
                True,
                selected,
                effective,
                None,
                "negative-effective-cost",
            )
        available = (
            request.resources.review
            if cost_type == COST_GOOD_IMPRESSION
            else request.resources.motivation
        )
    playable = effective <= available
    return Plan2NativeCostPredicate(
        request,
        True,
        playable,
        True,
        selected,
        effective,
        available,
        None if playable else "insufficient-resource",
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeCostPayment:
    predicate: Plan2NativeCostPredicate
    committed: bool
    resources_before: Plan2CostResources
    resources_after: Plan2CostResources
    stamina_payment: StaminaPayment | None = None
    actual_paid: int = 0
    trace: tuple[str, ...] = ()


def pay_plan2_native_cost(
    request: Plan2NativeCostRequest,
) -> Plan2NativeCostPayment:
    """Commit a legal cost preview as one immutable payment handoff."""

    predicate = evaluate_plan2_native_cost_predicate(request)
    before = request.resources
    if not predicate.resolved or not predicate.playable:
        return Plan2NativeCostPayment(
            predicate,
            False,
            before,
            before,
            trace=(f"cost:rejected:{predicate.reason}",),
        )
    if not predicate.consumes_cost:
        return Plan2NativeCostPayment(
            predicate,
            True,
            before,
            before,
            actual_paid=0,
            trace=(f"{request.origin.value}:cost-free",),
        )
    assert predicate.effective_cost is not None
    effective = predicate.effective_cost
    if request.cost.cost_type == COST_STAMINA:
        payment = split_stamina_payment(
            effective,
            penetrate=request.cost.penetrate,
            current_stamina=before.stamina,
            max_stamina=before.max_stamina,
            current_block=before.block,
        )
        after = replace(
            before, stamina=payment.new_stamina, block=payment.new_block
        )
        return Plan2NativeCostPayment(
            predicate,
            True,
            before,
            after,
            stamina_payment=payment,
            actual_paid=payment.stamina_loss + payment.block_loss,
            trace=(
                f"{request.origin.value}:ConsumeCardCost:{COST_STAMINA}:{effective}",
                "payment:DamageStamina",
            ),
        )
    if effective == 0:
        return Plan2NativeCostPayment(
            predicate,
            True,
            before,
            before,
            actual_paid=0,
            trace=(
                f"{request.origin.value}:ConsumeCardCost:{request.cost.cost_type}:0",
                "payment:buff-zero-noop",
            ),
        )
    if request.cost.cost_type == COST_GOOD_IMPRESSION:
        new_review = before.review - effective
        actual = before.review - new_review
        after = replace(
            before,
            review=new_review,
            review_exists=before.review_exists and new_review > 0,
            review_consumption_sum=before.review_consumption_sum + actual,
        )
    else:
        actual = effective
        after = replace(before, motivation=before.motivation - effective)
    return Plan2NativeCostPayment(
        predicate,
        True,
        before,
        after,
        actual_paid=actual,
        trace=(
            f"{request.origin.value}:ConsumeCardCost:{request.cost.cost_type}:{effective}",
            "payment:ConsumeBuffCost",
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaHandoffExecution:
    handoff: Plan2NativeStaminaEffectHandoff
    executable: bool
    modifier_before: Plan2StaminaModifierRuntime
    modifier_after: Plan2StaminaModifierRuntime
    resources_before: Plan2CostResources
    resources_after: Plan2CostResources
    modifier_execution: Plan2StaminaModifierExecution | None = None
    recovery_evaluation: StaminaRecoverFixEvaluation | None = None
    reason: str | None = None


def execute_plan2_native_stamina_handoff(
    catalog: Plan2NativeCatalogStamina,
    handoff: Plan2NativeStaminaEffectHandoff,
    *,
    modifiers: Plan2StaminaModifierRuntime,
    resources: Plan2CostResources,
    block_add_status: bool = False,
    stamina_recover_restricted: bool = False,
    stamina_recover_add_permil: int | None = None,
) -> Plan2NativeStaminaHandoffExecution:
    """Execute one typed direct handoff without touching an outer state object."""

    if not isinstance(catalog, Plan2NativeCatalogStamina):
        raise TypeError("catalog must be Plan2NativeCatalogStamina")
    if not isinstance(handoff, Plan2NativeStaminaEffectHandoff):
        raise TypeError("handoff must be Plan2NativeStaminaEffectHandoff")
    if not isinstance(modifiers, Plan2StaminaModifierRuntime):
        raise TypeError("modifiers must be Plan2StaminaModifierRuntime")
    if not isinstance(resources, Plan2CostResources):
        raise TypeError("resources must be Plan2CostResources")
    contract = catalog.effect_by_id.get(handoff.effect_id)
    if contract is None or effect_family(contract).value != handoff.effect_type:
        return Plan2NativeStaminaHandoffExecution(
            handoff,
            False,
            modifiers,
            modifiers,
            resources,
            resources,
            reason="unknown-or-mismatched-effect-handoff",
        )
    if isinstance(contract, StaminaRecoverFixContract):
        evaluation = evaluate_stamina_recover_fix(
            contract,
            StaminaRecoverFixRuntime(
                stamina=resources.stamina,
                max_stamina=resources.max_stamina,
                stamina_recover_restricted=stamina_recover_restricted,
                stamina_recover_add_permil=stamina_recover_add_permil,
                block=resources.block,
            ),
        )
        after_resources = (
            replace(resources, stamina=evaluation.runtime_after.stamina)
            if evaluation.executable
            else resources
        )
        return Plan2NativeStaminaHandoffExecution(
            handoff,
            evaluation.executable,
            modifiers,
            modifiers,
            resources,
            after_resources,
            recovery_evaluation=evaluation,
            reason=evaluation.reason,
        )
    execution = apply_plan2_stamina_modifier(
        modifiers, contract, block_add_status=block_add_status
    )
    return Plan2NativeStaminaHandoffExecution(
        handoff,
        execution.executable,
        modifiers,
        execution.after,
        resources,
        resources,
        modifier_execution=execution,
        reason=execution.reason,
    )


__all__ = [
    "BUFF_COST_TYPES",
    "CARD_PLAY_STAMINA_TRIGGER_ID",
    "CARD_PLAY_STAMINA_TRIGGER_OWNER",
    "CURRENT_MASTER_COST_TYPE_COUNTS",
    "CURRENT_MASTER_DELEGATED_FAMILY_COUNTS",
    "CURRENT_MASTER_DIRECT_FAMILY_COUNTS",
    "DIRECT_EFFECT_OWNER",
    "EFFECT_STAMINA_CONSUMPTION_ADD",
    "EFFECT_STAMINA_CONSUMPTION_DOWN",
    "EFFECT_STAMINA_CONSUMPTION_DOWN_FIX",
    "EFFECT_STAMINA_RECOVER_FIX",
    "PERMANENT_TURN",
    "PLAN_TYPES",
    "Plan2CostResources",
    "Plan2NativeCardCost",
    "Plan2NativeCatalogStamina",
    "Plan2NativeCostPayment",
    "Plan2NativeCostPredicate",
    "Plan2NativeCostRequest",
    "Plan2NativeCostSettings",
    "Plan2NativeStaminaCatalogError",
    "Plan2NativeStaminaEffectHandoff",
    "Plan2NativeStaminaFamilyAccounting",
    "Plan2NativeStaminaHandoffExecution",
    "Plan2StaminaModifierExecution",
    "Plan2StaminaModifierRuntime",
    "Plan2StaminaTurnStartTransition",
    "PlayOrigin",
    "STAMINA_EFFECT_TYPES",
    "StaminaConsumptionDownEffect",
    "StaminaConsumptionDownFixEffect",
    "StaminaConsumptionDownFixLayer",
    "StaminaConsumptionDownLayer",
    "StaminaEffectContract",
    "StaminaEffectFamily",
    "advance_plan2_stamina_turn_start",
    "apply_plan2_stamina_modifier",
    "compile_plan2_native_catalog_stamina",
    "effect_family",
    "evaluate_plan2_native_cost_predicate",
    "execute_plan2_native_stamina_handoff",
    "load_plan2_native_catalog_stamina",
    "pay_plan2_native_cost",
]
