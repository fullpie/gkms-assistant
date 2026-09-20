"""Typed, item-id-independent Plan2 mandatory P-item effect graphs.

This module is deliberately a contract boundary.  It reads the Master graph
``produce_item -> produce_item_effect -> produce_exam_status_enchant ->
produce_exam_trigger / effect`` and keeps every array in Master order.  It
does not install listeners, mutate a simulator, or silently emulate a native
executor.  Callers can therefore use :func:`load_plan2_pitem_catalog` to
audit all idol-bound P-items before wiring the graph into a runtime.

The graph owns the composite shapes which are easy to lose when an item is
handled by item-id branches:

* ``ExamEffectTimer`` one-child and ordered multi-child chains;
* ``ExamStatusEnchant`` wrappers and their recursively ordered status graph;
* scalar and dependent Review/Block/Aggressive/Lesson/Stamina effects;
* card draw/move/create and ExtraTurn operations.

Static parsing is supported for every current Plan2 idol-card mandatory item
(59 cards x before/after = 118 variants).  Evaluation emits immutable typed
operations in the same Master order.  Operations which need live native
state are explicit operations with ``runtime_required=True``; missing live
state never becomes a guessed zero or an implicit successful trigger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Final

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .plan2_pitem_composite_trigger import (
    CompositeTriggerError,
    CompositeTriggerSpec,
    parse_composite_trigger,
)


PLAN2: Final = "ProducePlanType_Plan2"
UNKNOWN_MOVE: Final = "ProduceCardMovePositionType_Unknown"
UNKNOWN_PICK_RANGE: Final = "ProducePickRangeType_Unknown"
UNKNOWN_PICK_COUNT: Final = "ProducePickCountType_Unknown"
UNKNOWN_EXAM_EFFECT: Final = "ProduceExamEffectType_Unknown"

# These are all effect families reachable from the current 118 mandatory
# Plan2 item variants.  CardCreate* are included as a generic contract even
# though the current matrix has no mandatory root of that family; a future
# Master row must still be admitted only after its typed payload is checked.
EFFECT_AGGRESSIVE_ADDITIVE: Final = "ProduceExamEffectType_ExamAggressiveAdditive"
EFFECT_AGGRESSIVE_REDUCE: Final = "ProduceExamEffectType_ExamAggressiveReduce"
EFFECT_AGGRESSIVE_MULTIPLE: Final = "ProduceExamEffectType_ExamAggressiveValueMultiple"
EFFECT_BLOCK: Final = "ProduceExamEffectType_ExamBlock"
EFFECT_BLOCK_ADD_DOWN: Final = "ProduceExamEffectType_ExamBlockAddDown"
EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE: Final = (
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive"
)
EFFECT_BLOCK_DEPEND_BLOCK_CONSUMPTION_SUM: Final = (
    "ProduceExamEffectType_ExamBlockDependBlockConsumptionSum"
)
EFFECT_BLOCK_DEPEND_REVIEW: Final = "ProduceExamEffectType_ExamBlockDependExamReview"
EFFECT_BLOCK_RESTRICTION: Final = "ProduceExamEffectType_ExamBlockRestriction"
EFFECT_BLOCK_MULTIPLE: Final = "ProduceExamEffectType_ExamBlockValueMultiple"
EFFECT_CARD_CREATE_ID: Final = "ProduceExamEffectType_ExamCardCreateId"
EFFECT_CARD_CREATE_SEARCH: Final = "ProduceExamEffectType_ExamCardCreateSearch"
EFFECT_CARD_DRAW: Final = "ProduceExamEffectType_ExamCardDraw"
EFFECT_CARD_MOVE: Final = "ProduceExamEffectType_ExamCardMove"
EFFECT_CARD_PLAY_AGGRESSIVE: Final = "ProduceExamEffectType_ExamCardPlayAggressive"
EFFECT_TIMER: Final = "ProduceExamEffectType_ExamEffectTimer"
EFFECT_EXTRA_TURN: Final = "ProduceExamEffectType_ExamExtraTurn"
EFFECT_LESSON_DEPEND_BLOCK: Final = "ProduceExamEffectType_ExamLessonDependBlock"
EFFECT_LESSON_DEPEND_BLOCK_SEARCH: Final = (
    "ProduceExamEffectType_ExamLessonDependBlockAndSearchCount"
)
EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE: Final = (
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive"
)
EFFECT_LESSON_DEPEND_REVIEW: Final = "ProduceExamEffectType_ExamLessonDependExamReview"
EFFECT_LESSON_MULTIPLE: Final = "ProduceExamEffectType_ExamLessonValueMultiple"
EFFECT_LESSON_DEPEND_REVIEW_OR_AGGRESSIVE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive"
)
EFFECT_LESSON_MULTIPLE_DOWN: Final = "ProduceExamEffectType_ExamLessonValueMultipleDown"
EFFECT_PLAYABLE_ADD: Final = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_REVIEW: Final = "ProduceExamEffectType_ExamReview"
EFFECT_REVIEW_ADDITIVE: Final = "ProduceExamEffectType_ExamReviewAdditive"
EFFECT_REVIEW_DEPEND_BLOCK: Final = "ProduceExamEffectType_ExamReviewDependExamBlock"
EFFECT_REVIEW_MULTIPLE: Final = "ProduceExamEffectType_ExamReviewMultiple"
EFFECT_REVIEW_REDUCE: Final = "ProduceExamEffectType_ExamReviewReduce"
EFFECT_REVIEW_VALUE_MULTIPLE: Final = "ProduceExamEffectType_ExamReviewValueMultiple"
EFFECT_STAMINA_CONSUMPTION_DOWN: Final = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_STAMINA_RECOVER_FIX: Final = "ProduceExamEffectType_ExamStaminaRecoverFix"
EFFECT_STAMINA_RECOVER_MULTIPLE: Final = (
    "ProduceExamEffectType_ExamStaminaRecoverMultiple"
)
EFFECT_STAMINA_REDUCE: Final = "ProduceExamEffectType_ExamStaminaReduce"
EFFECT_STAMINA_REDUCE_FIX: Final = "ProduceExamEffectType_ExamStaminaReduceFix"
EFFECT_STATUS_ENCHANT: Final = "ProduceExamEffectType_ExamStatusEnchant"

# Descriptive aliases match the Master names used by neighbouring native
# catalog modules.  Keeping both spellings avoids making callers depend on a
# shortened family name while still exposing one canonical value.
EFFECT_AGGRESSIVE_VALUE_MULTIPLE: Final = EFFECT_AGGRESSIVE_MULTIPLE
EFFECT_BLOCK_VALUE_MULTIPLE: Final = EFFECT_BLOCK_MULTIPLE
EFFECT_LESSON_DEPEND_EXAM_CARD_PLAY_AGGRESSIVE: Final = (
    EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE
)
EFFECT_LESSON_DEPEND_EXAM_REVIEW: Final = EFFECT_LESSON_DEPEND_REVIEW
EFFECT_LESSON_VALUE_MULTIPLE: Final = EFFECT_LESSON_MULTIPLE
EFFECT_LESSON_VALUE_MULTIPLE_DEPEND_REVIEW_OR_AGGRESSIVE: Final = (
    EFFECT_LESSON_DEPEND_REVIEW_OR_AGGRESSIVE
)
EFFECT_LESSON_VALUE_MULTIPLE_DOWN: Final = EFFECT_LESSON_MULTIPLE_DOWN

SUPPORTED_EFFECT_TYPES: Final = frozenset(
    {
        EFFECT_AGGRESSIVE_ADDITIVE,
        EFFECT_AGGRESSIVE_REDUCE,
        EFFECT_AGGRESSIVE_MULTIPLE,
        EFFECT_BLOCK,
        EFFECT_BLOCK_ADD_DOWN,
        EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE,
        EFFECT_BLOCK_DEPEND_BLOCK_CONSUMPTION_SUM,
        EFFECT_BLOCK_DEPEND_REVIEW,
        EFFECT_BLOCK_RESTRICTION,
        EFFECT_BLOCK_MULTIPLE,
        EFFECT_CARD_CREATE_ID,
        EFFECT_CARD_CREATE_SEARCH,
        EFFECT_CARD_DRAW,
        EFFECT_CARD_MOVE,
        EFFECT_CARD_PLAY_AGGRESSIVE,
        EFFECT_TIMER,
        EFFECT_EXTRA_TURN,
        EFFECT_LESSON_DEPEND_BLOCK,
        EFFECT_LESSON_DEPEND_BLOCK_SEARCH,
        EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE,
        EFFECT_LESSON_DEPEND_REVIEW,
        EFFECT_LESSON_MULTIPLE,
        EFFECT_LESSON_DEPEND_REVIEW_OR_AGGRESSIVE,
        EFFECT_LESSON_MULTIPLE_DOWN,
        EFFECT_PLAYABLE_ADD,
        EFFECT_REVIEW,
        EFFECT_REVIEW_ADDITIVE,
        EFFECT_REVIEW_DEPEND_BLOCK,
        EFFECT_REVIEW_MULTIPLE,
        EFFECT_REVIEW_REDUCE,
        EFFECT_REVIEW_VALUE_MULTIPLE,
        EFFECT_STAMINA_CONSUMPTION_DOWN,
        EFFECT_STAMINA_RECOVER_FIX,
        EFFECT_STAMINA_RECOVER_MULTIPLE,
        EFFECT_STAMINA_REDUCE,
        EFFECT_STAMINA_REDUCE_FIX,
        EFFECT_STATUS_ENCHANT,
    }
)


class Plan2PItemEffectGraphError(ValueError):
    """Strict parser error with a stable fail-closed code and path."""

    def __init__(self, code: str, detail: str = "") -> None:
        if type(code) is not str or not code:
            raise ValueError("error code must be non-empty text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise Plan2PItemEffectGraphError("invalid-text", label)
    return value


def _int(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise Plan2PItemEffectGraphError("invalid-integer", label)
    if not -(2**31) <= value <= 2**31 - 1:
        raise Plan2PItemEffectGraphError("int32-out-of-range", label)
    if minimum is not None and value < minimum:
        raise Plan2PItemEffectGraphError("integer-below-minimum", label)
    return value


def _json_array(value: object, label: str) -> tuple[object, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise Plan2PItemEffectGraphError("invalid-json", label) from error
    if not isinstance(value, (tuple, list)):
        raise Plan2PItemEffectGraphError("invalid-array", label)
    return tuple(value)


def _str_tuple(value: object, label: str, *, empty: bool = False) -> tuple[str, ...]:
    result = _json_array(value, label)
    output = []
    for index, entry in enumerate(result):
        output.append(_text(entry, f"{label}[{index}]", empty=empty))
    return tuple(output)


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    return tuple(_int(entry, f"{label}[{index}]") for index, entry in enumerate(_json_array(value, label)))


def _mapping_get(raw: Mapping[str, object], *names: str, default: object = None) -> object:
    for name in names:
        if name in raw:
            return raw[name]
    return default


@dataclass(frozen=True, slots=True)
class Plan2PItemOperation:
    """One immutable typed operation in exact Master order.

    ``parameters`` is a sorted tuple instead of a mutable mapping, which
    makes accidental value/order drift visible in equality and snapshots.
    ``children`` carries Timer/StatusEnchant operations in their original
    order.  ``runtime_required`` documents why a static contract cannot be
    used as a complete native execution result by itself.
    """

    operation: str
    effect_id: str
    effect_type: str
    parameters: tuple[tuple[str, object], ...] = ()
    children: tuple["Plan2PItemOperation", ...] = ()
    runtime_required: bool = True

    def __post_init__(self) -> None:
        _text(self.operation, "operation")
        _text(self.effect_id, "effect_id")
        _text(self.effect_type, "effect_type")
        parameters = tuple(self.parameters)
        keys = [key for key, _ in parameters]
        if any(type(key) is not str or not key for key in keys):
            raise Plan2PItemEffectGraphError("invalid-operation-parameter-key", self.effect_id)
        if keys != sorted(keys):
            raise Plan2PItemEffectGraphError("operation-parameter-order", self.effect_id)
        if len(keys) != len(set(keys)):
            raise Plan2PItemEffectGraphError("duplicate-operation-parameter", self.effect_id)
        object.__setattr__(self, "parameters", parameters)
        children = tuple(self.children)
        if any(not isinstance(child, Plan2PItemOperation) for child in children):
            raise TypeError("children must contain Plan2PItemOperation values")
        object.__setattr__(self, "children", children)
        if type(self.runtime_required) is not bool:
            raise TypeError("runtime_required must be boolean")

    def parameter(self, name: str) -> object:
        for key, value in self.parameters:
            if key == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class Plan2PItemEffectNode:
    """One Master ``effect`` row plus all resolved nested references."""

    id: str
    effect_type: str
    value1: int = 0
    value2: int = 0
    effect_count: int = 0
    effect_turn: int = 0
    target_produce_card_id: str = ""
    target_upgrade_count: int = 0
    produce_card_search_id: str = ""
    move_position_type: str = UNKNOWN_MOVE
    pick_range_type: str = UNKNOWN_PICK_RANGE
    pick_count_reference_search_id: str = ""
    pick_count_type: str = UNKNOWN_PICK_COUNT
    pick_count_min: int = 0
    pick_count_max: int = 0
    produce_card_search_id2: str = ""
    pick_range_type2: str = UNKNOWN_PICK_RANGE
    pick_count_reference_search_id2: str = ""
    pick_count_type2: str = UNKNOWN_PICK_COUNT
    pick_count_min2: int = 0
    pick_count_max2: int = 0
    chain_effect_ids: tuple[str, ...] = ()
    children: tuple["Plan2PItemEffectNode", ...] = ()
    status_enchant_id: str = ""
    status_enchant: "Plan2PItemStatusGraph | None" = None
    produce_card_status_enchant_id: str = ""
    produce_card_grow_effect_ids: tuple[str, ...] = ()
    effect_group_ids: tuple[str, ...] = ()
    raw_payload: tuple[tuple[str, object], ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "effect.id")
        _text(self.effect_type, f"{self.id}.effect_type")
        for name in ("value1", "value2", "effect_count", "pick_count_min", "pick_count_max", "pick_count_min2", "pick_count_max2", "target_upgrade_count"):
            _int(getattr(self, name), f"{self.id}.{name}")
        _int(self.effect_turn, f"{self.id}.effect_turn")
        for name in (
            "target_produce_card_id",
            "produce_card_search_id",
            "move_position_type",
            "pick_range_type",
            "pick_count_reference_search_id",
            "pick_count_type",
            "produce_card_search_id2",
            "pick_range_type2",
            "pick_count_reference_search_id2",
            "pick_count_type2",
            "status_enchant_id",
            "produce_card_status_enchant_id",
        ):
            _text(getattr(self, name), f"{self.id}.{name}", empty=True)
        if self.effect_count < 0:
            raise Plan2PItemEffectGraphError("negative-effect-count", self.id)
        if self.target_upgrade_count < 0:
            raise Plan2PItemEffectGraphError("negative-target-upgrade", self.id)
        for name in ("chain_effect_ids", "produce_card_grow_effect_ids", "effect_group_ids"):
            values = tuple(getattr(self, name))
            if any(type(value) is not str or not value for value in values):
                raise Plan2PItemEffectGraphError("invalid-string-array", f"{self.id}.{name}")
            object.__setattr__(self, name, values)
        children = tuple(self.children)
        if any(not isinstance(child, Plan2PItemEffectNode) for child in children):
            raise TypeError("children must contain Plan2PItemEffectNode values")
        if tuple(child.id for child in children) != self.chain_effect_ids:
            raise Plan2PItemEffectGraphError("child-order-drift", self.id)
        object.__setattr__(self, "children", children)
        if self.status_enchant_id and self.status_enchant is None:
            raise Plan2PItemEffectGraphError("status-enchant-unresolved", self.id)
        if self.status_enchant is not None:
            if not isinstance(self.status_enchant, Plan2PItemStatusGraph):
                raise TypeError("status_enchant must be Plan2PItemStatusGraph")
            if self.status_enchant.id != self.status_enchant_id:
                raise Plan2PItemEffectGraphError("status-enchant-id-drift", self.id)
        if self.status_enchant_id and self.chain_effect_ids:
            raise Plan2PItemEffectGraphError("status-chain-conflict", self.id)
        if self.effect_type == EFFECT_TIMER:
            if not self.chain_effect_ids:
                raise Plan2PItemEffectGraphError("timer-child-missing", self.id)
            if self.effect_count < 1:
                raise Plan2PItemEffectGraphError("timer-count-invalid", self.id)
            if self.value1 < 1:
                raise Plan2PItemGraphError("timer-delay-invalid", self.id)
        if self.effect_type == EFFECT_STATUS_ENCHANT and not self.status_enchant_id:
            raise Plan2PItemEffectGraphError("status-enchant-id-missing", self.id)

    @property
    def nested(self) -> tuple["Plan2PItemEffectNode", ...]:
        """Alias used by graph consumers that do not care about chain naming."""

        return self.children

    @property
    def is_composite(self) -> bool:
        return bool(self.children or self.status_enchant is not None)


# Typo-resistant alias kept private to avoid changing the public contract if
# a caller imports only the documented error class.
Plan2PItemGraphError = Plan2PItemEffectGraphError


@dataclass(frozen=True, slots=True)
class Plan2PItemStatusGraph:
    id: str
    trigger_id: str
    trigger: CompositeTriggerSpec
    ordered_effect_ids: tuple[str, ...]
    effects: tuple[Plan2PItemEffectNode, ...]
    max_uses: int = 0
    effect_turn: int = -1

    def __post_init__(self) -> None:
        _text(self.id, "status.id")
        _text(self.trigger_id, f"{self.id}.trigger_id")
        if not isinstance(self.trigger, CompositeTriggerSpec):
            raise TypeError("trigger must be CompositeTriggerSpec")
        ids = tuple(self.ordered_effect_ids)
        if any(type(value) is not str or not value for value in ids):
            raise Plan2PItemEffectGraphError("invalid-status-effect-ids", self.id)
        effects = tuple(self.effects)
        if tuple(effect.id for effect in effects) != ids:
            raise Plan2PItemEffectGraphError("status-effect-order-drift", self.id)
        if len(set(ids)) != len(ids):
            raise Plan2PItemEffectGraphError("duplicate-status-effect-id", self.id)
        object.__setattr__(self, "ordered_effect_ids", ids)
        object.__setattr__(self, "effects", effects)
        _int(self.max_uses, f"{self.id}.max_uses")
        _int(self.effect_turn, f"{self.id}.effect_turn")
        if self.max_uses < 0:
            raise Plan2PItemEffectGraphError("negative-max-uses", self.id)

    @property
    def operations(self) -> tuple[Plan2PItemOperation, ...]:
        return tuple(effect_to_operation(effect) for effect in self.effects)

    @property
    def effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids


@dataclass(frozen=True, slots=True)
class Plan2PItemEnchantmentGraph:
    id: str
    item_effect_id: str
    trigger: CompositeTriggerSpec
    status: Plan2PItemStatusGraph
    max_uses: int
    effect_turn: int

    def __post_init__(self) -> None:
        _text(self.id, "enchantment.id")
        _text(self.item_effect_id, f"{self.id}.item_effect_id")
        if not isinstance(self.trigger, CompositeTriggerSpec):
            raise TypeError("enchantment.trigger must be CompositeTriggerSpec")
        if not isinstance(self.status, Plan2PItemStatusGraph):
            raise TypeError("enchantment.status must be Plan2PItemStatusGraph")
        _int(self.max_uses, f"{self.id}.max_uses")
        _int(self.effect_turn, f"{self.id}.effect_turn")
        if self.max_uses < 0:
            raise Plan2PItemEffectGraphError("negative-max-uses", self.id)


@dataclass(frozen=True, slots=True)
class Plan2PItemVariant:
    idol_card_id: str
    item_id: str
    upgrade: int
    item_name: str
    enchantments: tuple[Plan2PItemEnchantmentGraph, ...]
    unsupported: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.idol_card_id, "variant.idol_card_id")
        _text(self.item_id, "variant.item_id")
        _int(self.upgrade, "variant.upgrade", minimum=0)
        _text(self.item_name, "variant.item_name")
        enchantments = tuple(self.enchantments)
        if any(not isinstance(value, Plan2PItemEnchantmentGraph) for value in enchantments):
            raise TypeError("enchantments must contain Plan2PItemEnchantmentGraph values")
        if not enchantments:
            raise Plan2PItemEffectGraphError("item-enchantment-missing", self.item_id)
        object.__setattr__(self, "enchantments", enchantments)
        unsupported = tuple(self.unsupported)
        if any(type(value) is not str or not value for value in unsupported):
            raise Plan2PItemEffectGraphError("invalid-blocker", self.item_id)
        object.__setattr__(self, "unsupported", unsupported)

    @property
    def supported(self) -> bool:
        return not self.unsupported

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(
            effect.id
            for enchantment in self.enchantments
            for effect in enchantment.status.effects
        )

    @property
    def trigger_ids(self) -> tuple[str, ...]:
        return tuple(enchantment.trigger.trigger_id for enchantment in self.enchantments)

    @property
    def effects(self) -> tuple[Plan2PItemEffectNode, ...]:
        return tuple(
            effect
            for enchantment in self.enchantments
            for effect in enchantment.status.effects
        )

    @property
    def trigger(self) -> CompositeTriggerSpec:
        if len(self.enchantments) != 1:
            raise Plan2PItemEffectGraphError("ambiguous-trigger", self.item_id)
        return self.enchantments[0].trigger

    @property
    def operations(self) -> tuple[Plan2PItemOperation, ...]:
        return tuple(effect_to_operation(effect) for effect in self.effects)

    @property
    def all_effect_ids(self) -> tuple[str, ...]:
        """Flatten root, Timer-child and StatusEnchant-child IDs in order."""

        result: list[str] = []

        def visit(node: Plan2PItemEffectNode) -> None:
            result.append(node.id)
            for child in node.children:
                visit(child)
            if node.status_enchant is not None:
                for child in node.status_enchant.effects:
                    visit(child)

        for node in self.effects:
            visit(node)
        return tuple(result)


@dataclass(frozen=True, slots=True)
class Plan2PItemCatalog:
    variants: tuple[Plan2PItemVariant, ...]

    def __post_init__(self) -> None:
        variants = tuple(self.variants)
        if any(not isinstance(value, Plan2PItemVariant) for value in variants):
            raise TypeError("variants must contain Plan2PItemVariant values")
        keys = [(value.idol_card_id, value.upgrade) for value in variants]
        if len(keys) != len(set(keys)):
            raise Plan2PItemEffectGraphError("duplicate-variant", repr(keys))
        object.__setattr__(self, "variants", variants)

    @property
    def supported(self) -> bool:
        return all(variant.supported for variant in self.variants)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(
            f"{variant.idol_card_id}:{variant.upgrade}:{blocker}"
            for variant in self.variants
            for blocker in variant.unsupported
        )

    def variant(self, idol_card_id: str, upgrade: int = 0) -> Plan2PItemVariant:
        matches = tuple(
            value
            for value in self.variants
            if value.idol_card_id == idol_card_id and value.upgrade == upgrade
        )
        if len(matches) != 1:
            raise KeyError(f"variant resolves {len(matches)} times: {idol_card_id}+{upgrade}")
        return matches[0]


@dataclass(frozen=True, slots=True)
class Plan2PItemEvaluation:
    """Ordered operation emission result.

    ``supported`` means graph parsing and operation typing succeeded.  A
    result may still contain runtime-required operations: the operation list
    is exact, while :attr:`runtime_complete` tells a caller whether a native
    state executor has been connected.
    """

    supported: bool
    operations: tuple[Plan2PItemOperation, ...]
    blockers: tuple[str, ...] = ()

    @property
    def runtime_complete(self) -> bool:
        return self.supported and not any(
            operation.runtime_required for operation in self.operations
        )

    @property
    def fail_closed(self) -> bool:
        return not self.supported


@dataclass(frozen=True, slots=True)
class Plan2PItemGraphResolution:
    supported: bool
    graph: Plan2PItemCatalog | Plan2PItemVariant | None
    blockers: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.graph is None


@dataclass(frozen=True, slots=True)
class Plan2PItemEffectGraphResolution:
    supported: bool
    effects: tuple[Plan2PItemEffectNode, ...]
    blockers: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported


def _raw_effect_from_row(row: sqlite3.Row) -> Mapping[str, object]:
    try:
        raw = json.loads(str(row["raw_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2PItemEffectGraphError("invalid-effect-json", str(row["id"])) from error
    if not isinstance(raw, Mapping):
        raise Plan2PItemEffectGraphError("invalid-effect-json-object", str(row["id"]))
    return raw


def _raw_trigger_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "phase_types": row["phase_types_json"],
        "phase_values": row["phase_values_json"],
        "field_check_types": row["field_status_check_types_json"],
        "field_types": row["field_status_types_json"],
        "field_values": row["field_status_values_json"],
        "field_card_search_ids": row["field_status_produce_card_search_ids_json"],
        "produce_card_search_id": str(row["produce_card_search_id"]),
        "upper_search_count": int(row["upper_search_count"]),
        "lower_search_count": int(row["lower_search_count"]),
        "card_move_position_type": str(row["card_move_position_type"]),
        "effect_types": row["effect_types_json"],
        "lesson_type": str(row["lesson_type"]),
    }


def _trigger_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    database: Path,
) -> CompositeTriggerSpec:
    raw = _raw_trigger_from_row(row)
    search_id = str(raw["produce_card_search_id"])
    search_rule: ProduceCardSearchRule | None = None
    if search_id:
        search_rule = load_produce_card_search(search_id, database)
    try:
        return parse_composite_trigger(raw, card_search_rule=search_rule)
    except (CompositeTriggerError, TypeError, ValueError) as error:
        code = getattr(error, "code", "trigger-parse-error")
        raise Plan2PItemEffectGraphError(str(code), str(error)) from error


def _effect_raw_fields(raw: Mapping[str, object]) -> dict[str, object]:
    """Normalize Master camelCase fields without dropping payload."""

    defaults: dict[str, object] = {
        "id": "",
        "effectType": "",
        "effectValue1": 0,
        "effectValue2": 0,
        "effectCount": 0,
        "effectTurn": 0,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "produceCardSearchId": "",
        "movePositionType": UNKNOWN_MOVE,
        "pickRangeType": UNKNOWN_PICK_RANGE,
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": UNKNOWN_PICK_COUNT,
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": UNKNOWN_PICK_RANGE,
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": UNKNOWN_PICK_COUNT,
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": (),
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": (),
        "effectGroupIds": (),
    }
    aliases = {
        "id": ("id",),
        "effectType": ("effectType", "effect_type"),
        "effectValue1": ("effectValue1", "value1"),
        "effectValue2": ("effectValue2", "value2"),
        "effectCount": ("effectCount", "effect_count"),
        "effectTurn": ("effectTurn", "effect_turn"),
        "targetProduceCardId": ("targetProduceCardId", "target_produce_card_id"),
        "targetUpgradeCount": ("targetUpgradeCount", "target_upgrade_count"),
        "produceCardSearchId": ("produceCardSearchId", "produce_card_search_id"),
        "movePositionType": ("movePositionType", "move_position_type"),
        "pickRangeType": ("pickRangeType", "pick_range_type"),
        "pickCountReferenceProduceCardSearchId": (
            "pickCountReferenceProduceCardSearchId",
            "pick_count_reference_search_id",
        ),
        "pickCountType": ("pickCountType", "pick_count_type"),
        "pickCountMin": ("pickCountMin", "pick_count_min"),
        "pickCountMax": ("pickCountMax", "pick_count_max"),
        "produceCardSearchId2": ("produceCardSearchId2", "produce_card_search_id2"),
        "pickRangeType2": ("pickRangeType2", "pick_range_type2"),
        "pickCountReferenceProduceCardSearchId2": (
            "pickCountReferenceProduceCardSearchId2",
            "pick_count_reference_search_id2",
        ),
        "pickCountType2": ("pickCountType2", "pick_count_type2"),
        "pickCountMin2": ("pickCountMin2", "pick_count_min2"),
        "pickCountMax2": ("pickCountMax2", "pick_count_max2"),
        "chainProduceExamEffectId": ("chainProduceExamEffectId", "chain_effect_id"),
        "chainProduceExamEffectIds": ("chainProduceExamEffectIds", "chain_effect_ids"),
        "produceExamStatusEnchantId": ("produceExamStatusEnchantId", "status_enchant_id"),
        "produceCardStatusEnchantId": (
            "produceCardStatusEnchantId",
            "produce_card_status_enchant_id",
        ),
        "produceCardGrowEffectIds": (
            "produceCardGrowEffectIds",
            "produce_card_grow_effect_ids",
        ),
        "effectGroupIds": ("effectGroupIds", "effect_group_ids"),
    }
    result = dict(defaults)
    for target, names in aliases.items():
        for name in names:
            if name in raw:
                result[target] = raw[name]
                break
    return result


class _GraphLoader:
    def __init__(self, connection: sqlite3.Connection, database: Path) -> None:
        self.connection = connection
        self.database = database
        self._effect_cache: dict[str, Mapping[str, object]] = {}
        self._status_cache: dict[str, Plan2PItemStatusGraph] = {}
        self._effect_stack: list[str] = []
        self._status_stack: list[str] = []

    def effect_raw(self, effect_id: str) -> Mapping[str, object]:
        effect_id = _text(effect_id, "effect_id")
        if effect_id not in self._effect_cache:
            row = self.connection.execute(
                "SELECT id, raw_json FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
            if row is None:
                raise Plan2PItemEffectGraphError("effect-not-found", effect_id)
            raw = _raw_effect_from_row(row)
            if raw.get("id") != effect_id:
                raise Plan2PItemEffectGraphError("effect-id-drift", effect_id)
            self._effect_cache[effect_id] = raw
        return self._effect_cache[effect_id]

    def status(self, status_id: str) -> Plan2PItemStatusGraph:
        status_id = _text(status_id, "status_id")
        cached = self._status_cache.get(status_id)
        if cached is not None:
            return cached
        if status_id in self._status_stack:
            raise Plan2PItemEffectGraphError("status-cycle", status_id)
        self._status_stack.append(status_id)
        try:
            row = self.connection.execute(
                "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (status_id,)
            ).fetchone()
            if row is None:
                raise Plan2PItemEffectGraphError("status-not-found", status_id)
            trigger_id = _text(row["produce_exam_trigger_id"], f"{status_id}.trigger_id")
            trigger_row = self.connection.execute(
                "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
            ).fetchone()
            if trigger_row is None:
                raise Plan2PItemEffectGraphError("trigger-not-found", trigger_id)
            trigger = _trigger_from_row(self.connection, trigger_row, self.database)
            effect_ids = _str_tuple(
                row["produce_exam_effect_ids_json"],
                f"{status_id}.effect_ids",
            )
            effects = tuple(self.effect(effect_id) for effect_id in effect_ids)
            result = Plan2PItemStatusGraph(
                id=status_id,
                trigger_id=trigger_id,
                trigger=trigger,
                ordered_effect_ids=effect_ids,
                effects=effects,
            )
            self._status_cache[status_id] = result
            return result
        finally:
            self._status_stack.pop()

    def effect(self, effect_id: str) -> Plan2PItemEffectNode:
        effect_id = _text(effect_id, "effect_id")
        if effect_id in self._effect_stack:
            raise Plan2PItemEffectGraphError("effect-cycle", effect_id)
        self._effect_stack.append(effect_id)
        try:
            raw = _effect_raw_fields(self.effect_raw(effect_id))
            raw_id = _text(raw["id"], f"{effect_id}.id")
            if raw_id != effect_id:
                raise Plan2PItemEffectGraphError("effect-id-drift", effect_id)
            effect_type = _text(raw["effectType"], f"{effect_id}.effect_type")
            if effect_type not in SUPPORTED_EFFECT_TYPES:
                raise Plan2PItemEffectGraphError("unknown-effect-type", f"{effect_id}:{effect_type}")
            chain_head = _text(raw["chainProduceExamEffectId"], f"{effect_id}.chain_head", empty=True)
            chain_list = _str_tuple(raw["chainProduceExamEffectIds"], f"{effect_id}.chain_ids")
            if chain_head and chain_list:
                raise Plan2PItemEffectGraphError("chain-storage-conflict", effect_id)
            chain_ids = (chain_head,) if chain_head else chain_list
            children = tuple(self.effect(child_id) for child_id in chain_ids)
            status_id = _text(raw["produceExamStatusEnchantId"], f"{effect_id}.status_id", empty=True)
            status = self.status(status_id) if status_id else None
            if raw["produceCardStatusEnchantId"]:
                # The imported DB has no card-status-enchant table.  Keeping
                # the reference in the node would falsely imply execution.
                raise Plan2PItemEffectGraphError(
                    "card-status-enchant-unsupported",
                    f"{effect_id}:{raw['produceCardStatusEnchantId']}",
                )
            node = Plan2PItemEffectNode(
                id=effect_id,
                effect_type=effect_type,
                value1=_int(raw["effectValue1"], f"{effect_id}.value1"),
                value2=_int(raw["effectValue2"], f"{effect_id}.value2"),
                effect_count=_int(raw["effectCount"], f"{effect_id}.effect_count"),
                effect_turn=_int(raw["effectTurn"], f"{effect_id}.effect_turn"),
                target_produce_card_id=_text(raw["targetProduceCardId"], f"{effect_id}.target_card", empty=True),
                target_upgrade_count=_int(raw["targetUpgradeCount"], f"{effect_id}.target_upgrade"),
                produce_card_search_id=_text(raw["produceCardSearchId"], f"{effect_id}.search", empty=True),
                move_position_type=_text(raw["movePositionType"], f"{effect_id}.move_position"),
                pick_range_type=_text(raw["pickRangeType"], f"{effect_id}.pick_range"),
                pick_count_reference_search_id=_text(raw["pickCountReferenceProduceCardSearchId"], f"{effect_id}.pick_ref", empty=True),
                pick_count_type=_text(raw["pickCountType"], f"{effect_id}.pick_type"),
                pick_count_min=_int(raw["pickCountMin"], f"{effect_id}.pick_min"),
                pick_count_max=_int(raw["pickCountMax"], f"{effect_id}.pick_max"),
                produce_card_search_id2=_text(raw["produceCardSearchId2"], f"{effect_id}.search2", empty=True),
                pick_range_type2=_text(raw["pickRangeType2"], f"{effect_id}.pick_range2"),
                pick_count_reference_search_id2=_text(raw["pickCountReferenceProduceCardSearchId2"], f"{effect_id}.pick_ref2", empty=True),
                pick_count_type2=_text(raw["pickCountType2"], f"{effect_id}.pick_type2"),
                pick_count_min2=_int(raw["pickCountMin2"], f"{effect_id}.pick_min2"),
                pick_count_max2=_int(raw["pickCountMax2"], f"{effect_id}.pick_max2"),
                chain_effect_ids=chain_ids,
                children=children,
                status_enchant_id=status_id,
                status_enchant=status,
                produce_card_status_enchant_id=_text(raw["produceCardStatusEnchantId"], f"{effect_id}.card_status_id", empty=True),
                produce_card_grow_effect_ids=_str_tuple(raw["produceCardGrowEffectIds"], f"{effect_id}.grow_ids"),
                effect_group_ids=_str_tuple(raw["effectGroupIds"], f"{effect_id}.groups"),
                raw_payload=tuple(sorted(raw.items(), key=lambda value: value[0])),
            )
            _validate_effect_payload(node)
            return node
        finally:
            self._effect_stack.pop()


def _validate_effect_payload(node: Plan2PItemEffectNode) -> None:
    """Validate shape/value contracts without guessing native arithmetic."""

    if node.effect_type in {
        EFFECT_CARD_DRAW,
        EFFECT_CARD_CREATE_ID,
        EFFECT_CARD_CREATE_SEARCH,
    } and node.value1 < 0:
        raise Plan2PItemEffectGraphError("card-count-invalid", node.id)
    if node.effect_type == EFFECT_CARD_MOVE:
        if not node.produce_card_search_id or node.move_position_type == UNKNOWN_MOVE:
            raise Plan2PItemEffectGraphError("card-move-target-missing", node.id)
    if node.effect_type in {EFFECT_CARD_CREATE_ID, EFFECT_CARD_CREATE_SEARCH}:
        if node.effect_type == EFFECT_CARD_CREATE_ID and not node.target_produce_card_id:
            raise Plan2PItemEffectGraphError("card-create-target-missing", node.id)
        if node.effect_type == EFFECT_CARD_CREATE_SEARCH and not node.produce_card_search_id:
            raise Plan2PItemEffectGraphError("card-create-search-missing", node.id)
    if node.pick_count_min < 0 or node.pick_count_max < 0 or node.pick_count_min2 < 0 or node.pick_count_max2 < 0:
        raise Plan2PItemEffectGraphError("negative-pick-count", node.id)
    if node.pick_count_max and node.pick_count_min > node.pick_count_max:
        raise Plan2PItemEffectGraphError("pick-count-range-drift", node.id)
    if node.pick_count_max2 and node.pick_count_min2 > node.pick_count_max2:
        raise Plan2PItemEffectGraphError("pick-count-range-drift", node.id)
    if node.effect_type == EFFECT_TIMER and node.value1 < 1:
        raise Plan2PItemEffectGraphError("timer-delay-invalid", node.id)
    if node.effect_type == EFFECT_STATUS_ENCHANT and node.status_enchant is None:
        raise Plan2PItemEffectGraphError("status-enchant-unresolved", node.id)


def _operation_parameters(**values: object) -> tuple[tuple[str, object], ...]:
    return tuple(sorted(values.items(), key=lambda item: item[0]))


_OPERATION_BY_EFFECT: Final[dict[str, str]] = {
    EFFECT_AGGRESSIVE_ADDITIVE: "aggressive_additive",
    EFFECT_AGGRESSIVE_REDUCE: "aggressive_reduce",
    EFFECT_AGGRESSIVE_MULTIPLE: "aggressive_value_multiple",
    EFFECT_BLOCK: "block_add",
    EFFECT_BLOCK_ADD_DOWN: "block_add_down",
    EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE: "block_add_multiple_aggressive",
    EFFECT_BLOCK_DEPEND_BLOCK_CONSUMPTION_SUM: "block_depend_block_consumption_sum",
    EFFECT_BLOCK_DEPEND_REVIEW: "block_depend_review",
    EFFECT_BLOCK_RESTRICTION: "block_restriction",
    EFFECT_BLOCK_MULTIPLE: "block_value_multiple",
    EFFECT_CARD_CREATE_ID: "card_create_id",
    EFFECT_CARD_CREATE_SEARCH: "card_create_search",
    EFFECT_CARD_DRAW: "card_draw",
    EFFECT_CARD_MOVE: "card_move",
    EFFECT_CARD_PLAY_AGGRESSIVE: "card_play_aggressive_add",
    EFFECT_TIMER: "effect_timer",
    EFFECT_EXTRA_TURN: "extra_turn",
    EFFECT_LESSON_DEPEND_BLOCK: "lesson_depend_block",
    EFFECT_LESSON_DEPEND_BLOCK_SEARCH: "lesson_depend_block_and_search_count",
    EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE: "lesson_depend_card_play_aggressive",
    EFFECT_LESSON_DEPEND_REVIEW: "lesson_depend_review",
    EFFECT_LESSON_MULTIPLE: "lesson_value_multiple",
    EFFECT_LESSON_DEPEND_REVIEW_OR_AGGRESSIVE: "lesson_value_multiple_depend_review_or_aggressive",
    EFFECT_LESSON_MULTIPLE_DOWN: "lesson_value_multiple_down",
    EFFECT_PLAYABLE_ADD: "playable_value_add",
    EFFECT_REVIEW: "review_add",
    EFFECT_REVIEW_ADDITIVE: "review_additive",
    EFFECT_REVIEW_DEPEND_BLOCK: "review_depend_block",
    EFFECT_REVIEW_MULTIPLE: "review_multiple",
    EFFECT_REVIEW_REDUCE: "review_reduce",
    EFFECT_REVIEW_VALUE_MULTIPLE: "review_value_multiple",
    EFFECT_STAMINA_CONSUMPTION_DOWN: "stamina_consumption_down",
    EFFECT_STAMINA_RECOVER_FIX: "stamina_recover_fix",
    EFFECT_STAMINA_RECOVER_MULTIPLE: "stamina_recover_multiple",
    EFFECT_STAMINA_REDUCE: "stamina_reduce",
    EFFECT_STAMINA_REDUCE_FIX: "stamina_reduce_fix",
    EFFECT_STATUS_ENCHANT: "status_enchant",
}


def effect_to_operation(effect: Plan2PItemEffectNode) -> Plan2PItemOperation:
    """Convert one parsed node to a typed operation without changing order."""

    if not isinstance(effect, Plan2PItemEffectNode):
        raise TypeError("effect must be Plan2PItemEffectNode")
    _validate_effect_node_integrity(effect)
    operation = _OPERATION_BY_EFFECT.get(effect.effect_type)
    if operation is None:
        raise Plan2PItemEffectGraphError("unknown-effect-type", effect.effect_type)
    params: dict[str, object] = {
        "effect_count": effect.effect_count,
        "effect_turn": effect.effect_turn,
        "value1": effect.value1,
        "value2": effect.value2,
    }
    if effect.target_produce_card_id:
        params["target_produce_card_id"] = effect.target_produce_card_id
        params["target_upgrade_count"] = effect.target_upgrade_count
    if effect.produce_card_search_id:
        params["produce_card_search_id"] = effect.produce_card_search_id
    if effect.move_position_type != UNKNOWN_MOVE:
        params["move_position_type"] = effect.move_position_type
    if effect.pick_range_type != UNKNOWN_PICK_RANGE:
        params["pick_range_type"] = effect.pick_range_type
    if effect.pick_count_type != UNKNOWN_PICK_COUNT:
        params["pick_count_type"] = effect.pick_count_type
    if effect.pick_count_min or effect.pick_count_max:
        params["pick_count_min"] = effect.pick_count_min
        params["pick_count_max"] = effect.pick_count_max
    if effect.produce_card_search_id2:
        params["produce_card_search_id2"] = effect.produce_card_search_id2
    if effect.status_enchant_id:
        params["status_enchant_id"] = effect.status_enchant_id
    children: tuple[Plan2PItemOperation, ...] = ()
    if effect.children:
        children = tuple(effect_to_operation(child) for child in effect.children)
    if effect.status_enchant is not None:
        if tuple(child.id for child in effect.status_enchant.effects) != effect.status_enchant.ordered_effect_ids:
            raise Plan2PItemEffectGraphError("status-effect-order-drift", effect.id)
        children = tuple(effect_to_operation(child) for child in effect.status_enchant.effects)
        params["status_trigger_id"] = effect.status_enchant.trigger_id
        params["status_effect_ids"] = effect.status_enchant.ordered_effect_ids
    return Plan2PItemOperation(
        operation=operation,
        effect_id=effect.id,
        effect_type=effect.effect_type,
        parameters=_operation_parameters(**params),
        children=children,
        runtime_required=True,
    )


def _validate_effect_node_integrity(effect: Plan2PItemEffectNode) -> None:
    """Reject mutation of a node after it was bound to a Master raw row."""

    if not effect.raw_payload:
        return
    raw = dict(effect.raw_payload)
    expected: dict[str, object] = {
        "id": effect.id,
        "effectType": effect.effect_type,
        "effectValue1": effect.value1,
        "effectValue2": effect.value2,
        "effectCount": effect.effect_count,
        "effectTurn": effect.effect_turn,
        "targetProduceCardId": effect.target_produce_card_id,
        "targetUpgradeCount": effect.target_upgrade_count,
        "produceCardSearchId": effect.produce_card_search_id,
        "movePositionType": effect.move_position_type,
        "pickRangeType": effect.pick_range_type,
        "pickCountReferenceProduceCardSearchId": effect.pick_count_reference_search_id,
        "pickCountType": effect.pick_count_type,
        "pickCountMin": effect.pick_count_min,
        "pickCountMax": effect.pick_count_max,
        "produceCardSearchId2": effect.produce_card_search_id2,
        "pickRangeType2": effect.pick_range_type2,
        "pickCountReferenceProduceCardSearchId2": effect.pick_count_reference_search_id2,
        "pickCountType2": effect.pick_count_type2,
        "pickCountMin2": effect.pick_count_min2,
        "pickCountMax2": effect.pick_count_max2,
        "chainProduceExamEffectId": effect.chain_effect_ids[0]
        if len(effect.chain_effect_ids) == 1
        and raw.get("chainProduceExamEffectId")
        else "",
        "chainProduceExamEffectIds": list(effect.chain_effect_ids)
        if raw.get("chainProduceExamEffectIds")
        else [],
        "produceExamStatusEnchantId": effect.status_enchant_id,
        "produceCardStatusEnchantId": effect.produce_card_status_enchant_id,
        "produceCardGrowEffectIds": list(effect.produce_card_grow_effect_ids),
        "effectGroupIds": list(effect.effect_group_ids),
    }

    def normalize(value: object) -> object:
        if isinstance(value, tuple):
            return [normalize(item) for item in value]
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {str(key): normalize(item) for key, item in value.items()}
        return value

    for key, value in expected.items():
        if key in raw and normalize(raw[key]) != normalize(value):
            raise Plan2PItemEffectGraphError("effect-value-drift", f"{effect.id}:{key}")


def evaluate_effect_graph(
    graph: Plan2PItemVariant | Plan2PItemStatusGraph | Sequence[Plan2PItemEffectNode],
) -> Plan2PItemEvaluation:
    """Emit exact typed operations in Master order.

    The function intentionally takes no fake state arguments.  Native
    arithmetic, trigger dispatch, pile mutation and status lifecycle require
    live runtime state and therefore remain marked ``runtime_required`` on
    each emitted operation.  A malformed graph returns a stable unsupported
    result instead of producing a partial guessed transition.
    """

    try:
        if isinstance(graph, Plan2PItemVariant):
            effects = tuple(
                effect
                for enchantment in graph.enchantments
                for effect in enchantment.status.effects
            )
            blockers = list(graph.unsupported)
        elif isinstance(graph, Plan2PItemStatusGraph):
            effects = graph.effects
            blockers = []
        else:
            effects = tuple(graph)
            blockers = []
        if any(not isinstance(effect, Plan2PItemEffectNode) for effect in effects):
            raise Plan2PItemEffectGraphError("invalid-effect-sequence", "graph")
        operations = tuple(effect_to_operation(effect) for effect in effects)
        return Plan2PItemEvaluation(not blockers, operations, tuple(blockers))
    except (Plan2PItemEffectGraphError, TypeError, ValueError) as error:
        code = getattr(error, "code", "evaluation-error")
        return Plan2PItemEvaluation(False, (), (f"{code}:{error}",))


def _load_item_variant(
    connection: sqlite3.Connection,
    loader: _GraphLoader,
    idol_card_id: str,
    item_id: str,
    upgrade: int,
) -> Plan2PItemVariant:
    row = connection.execute(
        "SELECT id, name, plan_type, produce_item_effect_ids_json FROM produce_item WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise Plan2PItemEffectGraphError("item-not-found", item_id)
    if str(row["plan_type"]) != PLAN2:
        raise Plan2PItemEffectGraphError("item-plan-mismatch", item_id)
    item_effect_ids = _str_tuple(row["produce_item_effect_ids_json"], f"{item_id}.item_effect_ids")
    enchantments: list[Plan2PItemEnchantmentGraph] = []
    blockers: list[str] = []
    for item_effect_id in item_effect_ids:
        item_effect = connection.execute(
            "SELECT * FROM produce_item_effect WHERE id = ?", (item_effect_id,)
        ).fetchone()
        if item_effect is None:
            blockers.append(f"item-effect-not-found:{item_effect_id}")
            continue
        if str(item_effect["effect_type"]) != "ProduceItemEffectType_ExamStatusEnchant":
            blockers.append(f"item-effect-type:{item_effect_id}:{item_effect['effect_type']}")
            continue
        status_id = _text(item_effect["produce_exam_status_enchant_id"], f"{item_effect_id}.status_id")
        try:
            status = loader.status(status_id)
            enchantments.append(
                Plan2PItemEnchantmentGraph(
                    id=status_id,
                    item_effect_id=item_effect_id,
                    trigger=status.trigger,
                    status=status,
                    max_uses=_int(item_effect["effect_count"], f"{item_effect_id}.effect_count"),
                    effect_turn=_int(item_effect["effect_turn"], f"{item_effect_id}.effect_turn"),
                )
            )
        except Plan2PItemEffectGraphError as error:
            blockers.append(f"{error.code}:{error.detail}")
    try:
        variant = Plan2PItemVariant(
            idol_card_id=idol_card_id,
            item_id=item_id,
            upgrade=upgrade,
            item_name=str(row["name"]),
            enchantments=tuple(enchantments),
            unsupported=tuple(blockers),
        )
    except Plan2PItemEffectGraphError as error:
        blockers.append(f"{error.code}:{error.detail}")
        # Preserve a non-throwing graph audit result for an item with no
        # usable enchantment.  A synthetic placeholder is not safe to expose.
        raise
    return variant


def load_plan2_pitem_catalog(database: Path = DEFAULT_DATABASE) -> Plan2PItemCatalog:
    """Load all 118 current Plan2 idol-card mandatory item variants."""

    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, raw_json FROM idol_card WHERE plan_type = ? ORDER BY id",
            (PLAN2,),
        ).fetchall()
        loader = _GraphLoader(connection, path)
        variants: list[Plan2PItemVariant] = []
        for row in rows:
            try:
                idol_raw = json.loads(str(row["raw_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise Plan2PItemEffectGraphError("invalid-idol-json", str(row["id"])) from error
            if not isinstance(idol_raw, Mapping):
                raise Plan2PItemEffectGraphError("invalid-idol-json-object", str(row["id"]))
            for upgrade, key in ((0, "beforeProduceItemId"), (1, "afterProduceItemId")):
                item_id = _text(idol_raw.get(key), f"{row['id']}.{key}")
                variants.append(_load_item_variant(connection, loader, str(row["id"]), item_id, upgrade))
        return Plan2PItemCatalog(tuple(variants))
    finally:
        connection.close()


def load_plan2_pitem_variant(
    item_id: str,
    *,
    idol_card_id: str = "standalone",
    upgrade: int = 0,
    database: Path = DEFAULT_DATABASE,
) -> Plan2PItemVariant:
    """Load one item by ID while preserving its Master effect graph."""

    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return _load_item_variant(connection, _GraphLoader(connection, path), idol_card_id, item_id, upgrade)
    finally:
        connection.close()


def resolve_plan2_pitem_catalog(
    database: Path = DEFAULT_DATABASE,
) -> Plan2PItemGraphResolution:
    """Non-throwing fail-closed catalog loader for unattended callers."""

    try:
        catalog = load_plan2_pitem_catalog(database)
    except (Plan2PItemEffectGraphError, OSError, sqlite3.Error, ValueError) as error:
        code = getattr(error, "code", "catalog-load-error")
        return Plan2PItemGraphResolution(False, None, (f"{code}:{error}",))
    if len(catalog.variants) != 118:
        return Plan2PItemGraphResolution(False, None, (f"variant-count:{len(catalog.variants)}",))
    if not catalog.supported:
        return Plan2PItemGraphResolution(False, None, catalog.blockers)
    # A catalog resolution carries the catalog through the graph slot so the
    # result remains useful while keeping one explicit resolution type.
    return Plan2PItemGraphResolution(True, catalog, ())


def _parse_raw_effect_sequence(
    raw_effects: Sequence[Mapping[str, object]],
    *,
    database: Path,
    expected_effect_ids: Sequence[str] | None = None,
) -> tuple[Plan2PItemEffectNode, ...]:
    """Parse an in-memory Master-shaped effect sequence.

    This is used by audit/tests that deliberately tamper a copied raw row.
    References not embedded in the mapping are still resolved against the
    read-only Master database, so a missing or unknown nested ID fails closed.
    """

    payloads = tuple(raw_effects)
    ids = tuple(
        _text(_effect_raw_fields(payload).get("id"), f"effect[{index}].id")
        for index, payload in enumerate(payloads)
    )
    if expected_effect_ids is not None and ids != tuple(expected_effect_ids):
        raise Plan2PItemEffectGraphError(
            "effect-order-drift", repr((ids, tuple(expected_effect_ids)))
        )
    # Keep one connection for the nested resolver.  The supplied mapping is
    # overlaid only for its own root IDs, which makes a value tamper observable
    # without weakening nested Master identity checks.
    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        loader = _GraphLoader(connection, path)
        originals = dict(loader._effect_cache)
        for payload in payloads:
            normalized = _effect_raw_fields(payload)
            root_id = _text(normalized.get("id"), "effect.id")
            canonical = _effect_raw_fields(loader.effect_raw(root_id))

            def normalize(value: object) -> object:
                if isinstance(value, tuple):
                    return [normalize(item) for item in value]
                if isinstance(value, list):
                    return [normalize(item) for item in value]
                if isinstance(value, dict):
                    return {str(key): normalize(item) for key, item in value.items()}
                return value

            if normalize(dict(normalized)) != normalize(dict(canonical)):
                raise Plan2PItemEffectGraphError("master-raw-drift", root_id)
            loader._effect_cache[root_id] = dict(payload)
        try:
            return tuple(loader.effect(effect_id) for effect_id in ids)
        finally:
            loader._effect_cache.clear()
            loader._effect_cache.update(originals)
    finally:
        connection.close()


def parse_plan2_pitem_effect_graph(
    effect_ids: Sequence[str] | Sequence[Mapping[str, object]],
    *,
    database: Path = DEFAULT_DATABASE,
    expected_effect_ids: Sequence[str] | None = None,
) -> tuple[Plan2PItemEffectNode, ...]:
    """Parse an ordered Master effect-id sequence with exact order checking."""

    if effect_ids and all(isinstance(value, Mapping) for value in effect_ids):
        return _parse_raw_effect_sequence(  # type: ignore[arg-type]
            effect_ids,
            database=database,
            expected_effect_ids=expected_effect_ids,
        )
    if any(isinstance(value, Mapping) for value in effect_ids):
        raise Plan2PItemEffectGraphError("mixed-effect-input", "effect_ids")
    ids = tuple(_text(value, f"effect_ids[{index}]") for index, value in enumerate(effect_ids))
    if expected_effect_ids is not None and ids != tuple(expected_effect_ids):
        raise Plan2PItemEffectGraphError("effect-order-drift", repr((ids, tuple(expected_effect_ids))))
    path = Path(database).resolve()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        loader = _GraphLoader(connection, path)
        return tuple(loader.effect(effect_id) for effect_id in ids)
    finally:
        connection.close()


def resolve_plan2_pitem_effect_graph(
    effect_ids: Sequence[str] | Sequence[Mapping[str, object]],
    *,
    database: Path = DEFAULT_DATABASE,
    expected_effect_ids: Sequence[str] | None = None,
) -> Plan2PItemEffectGraphResolution:
    """Non-throwing parser result for a single ordered effect sequence."""

    try:
        return Plan2PItemEffectGraphResolution(
            True,
            parse_plan2_pitem_effect_graph(
                effect_ids,
                database=database,
                expected_effect_ids=expected_effect_ids,
            ),
            (),
        )
    except (Plan2PItemEffectGraphError, OSError, sqlite3.Error, ValueError) as error:
        code = getattr(error, "code", "parse-error")
        return Plan2PItemEffectGraphResolution(False, (), (f"{code}:{error}",))


# Friendly aliases for callers using the shorter ``graph`` terminology.
load_plan2_pitem_graph_catalog = load_plan2_pitem_catalog
load_plan2_pitem_graph_variant = load_plan2_pitem_variant
evaluate_plan2_pitem_effect_graph = evaluate_effect_graph


__all__ = [
    "PLAN2",
    "SUPPORTED_EFFECT_TYPES",
    "EFFECT_AGGRESSIVE_ADDITIVE",
    "EFFECT_AGGRESSIVE_REDUCE",
    "EFFECT_AGGRESSIVE_MULTIPLE",
    "EFFECT_AGGRESSIVE_VALUE_MULTIPLE",
    "EFFECT_BLOCK",
    "EFFECT_BLOCK_ADD_DOWN",
    "EFFECT_BLOCK_ADD_MULTIPLE_AGGRESSIVE",
    "EFFECT_BLOCK_DEPEND_BLOCK_CONSUMPTION_SUM",
    "EFFECT_BLOCK_DEPEND_REVIEW",
    "EFFECT_BLOCK_RESTRICTION",
    "EFFECT_BLOCK_MULTIPLE",
    "EFFECT_BLOCK_VALUE_MULTIPLE",
    "EFFECT_CARD_CREATE_ID",
    "EFFECT_CARD_CREATE_SEARCH",
    "EFFECT_CARD_DRAW",
    "EFFECT_CARD_MOVE",
    "EFFECT_CARD_PLAY_AGGRESSIVE",
    "EFFECT_TIMER",
    "EFFECT_EXTRA_TURN",
    "EFFECT_LESSON_DEPEND_BLOCK",
    "EFFECT_LESSON_DEPEND_BLOCK_SEARCH",
    "EFFECT_LESSON_DEPEND_EXAM_CARD_PLAY_AGGRESSIVE",
    "EFFECT_LESSON_DEPEND_EXAM_REVIEW",
    "EFFECT_LESSON_DEPEND_CARD_PLAY_AGGRESSIVE",
    "EFFECT_LESSON_DEPEND_REVIEW",
    "EFFECT_LESSON_VALUE_MULTIPLE",
    "EFFECT_LESSON_VALUE_MULTIPLE_DEPEND_REVIEW_OR_AGGRESSIVE",
    "EFFECT_LESSON_VALUE_MULTIPLE_DOWN",
    "EFFECT_LESSON_MULTIPLE",
    "EFFECT_LESSON_DEPEND_REVIEW_OR_AGGRESSIVE",
    "EFFECT_LESSON_MULTIPLE_DOWN",
    "EFFECT_PLAYABLE_ADD",
    "EFFECT_REVIEW",
    "EFFECT_REVIEW_ADDITIVE",
    "EFFECT_REVIEW_DEPEND_BLOCK",
    "EFFECT_REVIEW_MULTIPLE",
    "EFFECT_REVIEW_REDUCE",
    "EFFECT_REVIEW_VALUE_MULTIPLE",
    "EFFECT_STAMINA_CONSUMPTION_DOWN",
    "EFFECT_STAMINA_RECOVER_FIX",
    "EFFECT_STAMINA_RECOVER_MULTIPLE",
    "EFFECT_STAMINA_REDUCE",
    "EFFECT_STAMINA_REDUCE_FIX",
    "EFFECT_STATUS_ENCHANT",
    "Plan2PItemEffectGraphError",
    "Plan2PItemGraphError",
    "Plan2PItemEffectGraphResolution",
    "Plan2PItemOperation",
    "Plan2PItemEffectNode",
    "Plan2PItemStatusGraph",
    "Plan2PItemEnchantmentGraph",
    "Plan2PItemVariant",
    "Plan2PItemCatalog",
    "Plan2PItemEvaluation",
    "Plan2PItemGraphResolution",
    "effect_to_operation",
    "evaluate_effect_graph",
    "evaluate_plan2_pitem_effect_graph",
    "load_plan2_pitem_catalog",
    "load_plan2_pitem_graph_catalog",
    "load_plan2_pitem_variant",
    "load_plan2_pitem_graph_variant",
    "parse_plan2_pitem_effect_graph",
    "resolve_plan2_pitem_effect_graph",
    "resolve_plan2_pitem_catalog",
]
