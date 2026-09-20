"""Typed, fail-closed data access for the ProduceDrink Master catalog.

This module is deliberately a data boundary.  It reads the normalized v5
master tables, preserves the original JSON payloads, and classifies only the
small set of effect shapes already understood by the Plan3 drink runtime.
Every other effect remains queryable as an explicit unsupported record; it is
never converted into a guessed runtime action.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .master_db import DEFAULT_DATABASE


DRINK_CATALOG_SCHEMA_VERSION = "5"

EFFECT_LESSON = "ProduceExamEffectType_ExamLesson"
EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
EFFECT_PRESERVATION = "ProduceExamEffectType_ExamPreservation"
EFFECT_CONCENTRATION = "ProduceExamEffectType_ExamConcentration"
EFFECT_FULL_POWER_POINT = "ProduceExamEffectType_ExamFullPowerPoint"
EFFECT_CARD_MOVE = "ProduceExamEffectType_ExamCardMove"
EFFECT_EXTRA_TURN = "ProduceExamEffectType_ExamExtraTurn"
EFFECT_PLAYABLE_VALUE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_STAMINA_RECOVER_FIX = "ProduceExamEffectType_ExamStaminaRecoverFix"
EFFECT_STAMINA_CONSUMPTION_DOWN = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_STAMINA_CONSUMPTION_ADD = (
    "ProduceExamEffectType_ExamStaminaConsumptionAdd"
)
EFFECT_STAMINA_REDUCE_FIX = "ProduceExamEffectType_ExamStaminaReduceFix"
EFFECT_CARD_PLAY_AGGRESSIVE = (
    "ProduceExamEffectType_ExamCardPlayAggressive"
)
EFFECT_REVIEW = "ProduceExamEffectType_ExamReview"

MOVE_HOLD = "ProduceCardMovePositionType_Hold"
PICK_SELECT = "ProducePickRangeType_Select"
SEARCH_DECK_GRAVE = "p_card_search-deck_grave"

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


class DrinkCatalogError(ValueError):
    """Base error for malformed catalog data or an unsupported effect."""


class DrinkCatalogSchemaError(DrinkCatalogError):
    """The database does not expose the complete typed catalog schema."""


class DrinkCatalogReferenceError(DrinkCatalogError):
    """A drink, effect link, or ordered reference is inconsistent."""


class DrinkCatalogUnsupportedEffect(DrinkCatalogError):
    """A catalog effect has no proven Plan3 runtime representation."""


class DrinkEffectShape(str, Enum):
    NUMERIC = "numeric"
    CARD_MOVE = "card_move"
    CARD_UPGRADE = "card_upgrade"
    UNSUPPORTED = "unsupported"


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DrinkCatalogSchemaError(f"{label} must be non-empty text")
    return value


def _require_optional_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DrinkCatalogSchemaError(f"{label} must be text")
    return value


def _require_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise DrinkCatalogSchemaError(f"{label} must be an integer")
    return value


def _json_value(value: object, label: str) -> Any:
    if not isinstance(value, str):
        raise DrinkCatalogSchemaError(f"{label} must contain JSON text")
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise DrinkCatalogSchemaError(f"{label} contains invalid JSON") from exc


def _json_object(value: object, label: str) -> dict[str, Any]:
    decoded = _json_value(value, label)
    if not isinstance(decoded, dict):
        raise DrinkCatalogSchemaError(f"{label} must contain a JSON object")
    return decoded


def _json_array(value: object, label: str) -> tuple[Any, ...]:
    decoded = _json_value(value, label)
    if not isinstance(decoded, list):
        raise DrinkCatalogSchemaError(f"{label} must contain a JSON array")
    return tuple(decoded)


def _json_string_array(value: object, label: str) -> tuple[str, ...]:
    values = _json_array(value, label)
    if not all(isinstance(item, str) for item in values):
        raise DrinkCatalogSchemaError(f"{label} must contain only text values")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class DrinkEffectLink:
    """One row from ProduceDrinkEffect.yaml."""

    id: str
    produce_effect_id: str
    produce_exam_effect_id: str
    raw_json: str

    def __post_init__(self) -> None:
        _require_text(self.id, "drink effect link id")
        if bool(self.produce_effect_id) == bool(self.produce_exam_effect_id):
            raise DrinkCatalogReferenceError(
                f"effect link must select exactly one target: {self.id}"
            )
        _require_optional_text(self.produce_effect_id, "produce_effect_id")
        _require_optional_text(self.produce_exam_effect_id, "produce_exam_effect_id")
        _json_object(self.raw_json, f"{self.id}.raw_json")

    @property
    def source_kind(self) -> str:
        return "produce" if self.produce_effect_id else "exam"

    @property
    def source_id(self) -> str:
        return self.produce_effect_id or self.produce_exam_effect_id


@dataclass(frozen=True, slots=True)
class DrinkEffectCatalogEntry:
    """Normalized execution fields for one distinct drink effect target."""

    source_kind: str
    source_id: str
    effect_type: str
    effect_value1: int
    effect_value2: int
    effect_value_min: int
    effect_value_max: int
    effect_count: int
    effect_turn: int
    produce_card_search_id: str
    produce_card_search_id2: str
    produce_card_status_enchant_id: str
    produce_exam_status_enchant_id: str
    produce_exam_trigger_id: str
    produce_exam_trigger_effect_ids: tuple[str, ...]
    chain_produce_exam_effect_id: str
    chain_produce_exam_effect_ids: tuple[str, ...]
    move_position_type: str
    pick_range_type: str
    pick_range_type2: str
    pick_count_type: str
    pick_count_type2: str
    pick_count_min: int
    pick_count_max: int
    pick_count_min2: int
    pick_count_max2: int
    pick_count_reference_produce_card_search_id: str
    pick_count_reference_produce_card_search_id2: str
    target_exam_effect_type: str
    target_produce_card_id: str
    target_upgrade_count: int
    produce_card_grow_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]
    produce_resource_type: str
    produce_rewards: tuple[Any, ...]
    produce_step_event_detail_id: str
    is_research: bool
    raw_json: str
    shape: DrinkEffectShape = field(init=False)
    unsupported_reason: str | None = field(init=False)

    def __post_init__(self) -> None:
        _require_text(self.source_kind, "effect source_kind")
        _require_text(self.source_id, "effect source_id")
        _require_optional_text(self.effect_type, f"{self.source_id}.effect_type")
        for name in (
            "effect_value1",
            "effect_value2",
            "effect_value_min",
            "effect_value_max",
            "effect_count",
            "effect_turn",
            "pick_count_min",
            "pick_count_max",
            "pick_count_min2",
            "pick_count_max2",
            "target_upgrade_count",
        ):
            _require_int(getattr(self, name), f"{self.source_id}.{name}")
        for name in (
            "produce_card_search_id",
            "produce_card_search_id2",
            "produce_card_status_enchant_id",
            "produce_exam_status_enchant_id",
            "produce_exam_trigger_id",
            "chain_produce_exam_effect_id",
            "move_position_type",
            "pick_range_type",
            "pick_range_type2",
            "pick_count_type",
            "pick_count_type2",
            "pick_count_reference_produce_card_search_id",
            "pick_count_reference_produce_card_search_id2",
            "target_exam_effect_type",
            "target_produce_card_id",
            "produce_resource_type",
            "produce_step_event_detail_id",
        ):
            _require_optional_text(
                getattr(self, name), f"{self.source_id}.{name}"
            )
        if not isinstance(self.is_research, bool):
            raise DrinkCatalogSchemaError(f"{self.source_id}.is_research must be bool")
        for name in (
            "produce_exam_trigger_effect_ids",
            "chain_produce_exam_effect_ids",
            "produce_card_grow_effect_ids",
            "effect_group_ids",
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not all(
                isinstance(item, str) for item in values
            ):
                raise DrinkCatalogSchemaError(
                    f"{self.source_id}.{name} must be a tuple of text"
                )
        if not isinstance(self.produce_rewards, tuple):
            raise DrinkCatalogSchemaError(
                f"{self.source_id}.produce_rewards must be a tuple"
            )
        _json_object(self.raw_json, f"{self.source_id}.raw_json")

        shape, reason = self._classify_shape()
        object.__setattr__(self, "shape", shape)
        object.__setattr__(self, "unsupported_reason", reason)

    def _classify_shape(self) -> tuple[DrinkEffectShape, str | None]:
        if self.source_kind != "exam":
            return (
                DrinkEffectShape.UNSUPPORTED,
                f"non-exam drink effect source is not a Plan3 exam action: {self.source_kind}",
            )
        if not self.effect_type:
            return DrinkEffectShape.UNSUPPORTED, "missing effect type"
        if self.effect_type == "ProduceExamEffectType_ExamCardUpgrade":
            from .plan3_drink_upgrade import matches_catalog_effect

            if matches_catalog_effect(self):
                return DrinkEffectShape.CARD_UPGRADE, None
            return DrinkEffectShape.UNSUPPORTED, "unsupported drink card-upgrade shape"
        if self.effect_type in _NUMERIC_EFFECT_TYPES:
            if (
                self.chain_produce_exam_effect_id
                or self.chain_produce_exam_effect_ids
                or self.produce_exam_status_enchant_id
                or self.produce_exam_trigger_id
            ):
                return (
                    DrinkEffectShape.UNSUPPORTED,
                    "numeric effect has nested status, trigger, or chain data",
                )
            if self.effect_type == EFFECT_LESSON:
                valid = (
                    self.effect_value2 == 0
                    and self.effect_count > 0
                    and self.effect_turn == 0
                )
            elif self.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
                valid = (
                    self.effect_value1 == 0
                    and self.effect_value2 == 0
                    and self.effect_count > 0
                    and self.effect_turn == 0
                )
            elif self.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
                valid = (
                    self.effect_value1 == 0
                    and self.effect_value2 == 0
                    and self.effect_count == 0
                    and self.effect_turn > 0
                )
            elif self.effect_type == EFFECT_STAMINA_CONSUMPTION_ADD:
                valid = (
                    self.effect_value1 == 0
                    and self.effect_value2 == 0
                    and self.effect_count == 0
                    and self.effect_turn > 0
                )
            elif self.effect_type == EFFECT_EXTRA_TURN:
                valid = (
                    self.effect_value1 == 0
                    and self.effect_value2 == 0
                    and self.effect_count == 0
                    and self.effect_turn == 0
                )
            elif self.effect_type in (
                EFFECT_STAMINA_RECOVER_FIX,
                EFFECT_STAMINA_REDUCE_FIX,
            ):
                valid = (
                    self.effect_value1 > 0
                    and self.effect_value2 == 0
                    and self.effect_count == 0
                    and self.effect_turn == 0
                )
            else:
                valid = (
                    self.effect_value2 == 0
                    and self.effect_count == 0
                    and self.effect_turn == 0
                )
            if valid:
                return DrinkEffectShape.NUMERIC, None
            return DrinkEffectShape.UNSUPPORTED, "unsupported numeric effect shape"
        if self.effect_type == EFFECT_CARD_MOVE:
            valid = (
                not self.produce_exam_status_enchant_id
                and not self.produce_exam_trigger_id
                and not self.chain_produce_exam_effect_id
                and not self.chain_produce_exam_effect_ids
                and self.produce_card_search_id == SEARCH_DECK_GRAVE
                and self.move_position_type == MOVE_HOLD
                and self.pick_range_type == PICK_SELECT
                and self.pick_count_min == 1
                and self.pick_count_max == 1
            )
            if valid:
                return DrinkEffectShape.CARD_MOVE, None
            return DrinkEffectShape.UNSUPPORTED, "unsupported card-move effect shape"
        return (
            DrinkEffectShape.UNSUPPORTED,
            f"unsupported effect type: {self.effect_type}",
        )

    @property
    def plan3_supported(self) -> bool:
        return self.shape is not DrinkEffectShape.UNSUPPORTED

    @property
    def gap_family(self) -> str:
        return f"{self.source_kind}:{self.effect_type or '<missing>'}"

    def require_plan3_supported(self) -> "DrinkEffectCatalogEntry":
        if not self.plan3_supported:
            raise DrinkCatalogUnsupportedEffect(
                f"unsupported Plan3 drink effect {self.source_id} "
                f"({self.gap_family}): {self.unsupported_reason}"
            )
        return self


@dataclass(frozen=True, slots=True)
class DrinkEffectReference:
    """One ordered effect slot in a drink definition."""

    drink_id: str
    effect_index: int
    drink_effect_id: str
    link: DrinkEffectLink
    effect: DrinkEffectCatalogEntry

    def __post_init__(self) -> None:
        _require_text(self.drink_id, "drink_id")
        _require_int(self.effect_index, "effect_index")
        if self.effect_index < 0:
            raise DrinkCatalogReferenceError("effect_index cannot be negative")
        if self.drink_effect_id != self.link.id:
            raise DrinkCatalogReferenceError(
                f"ordered reference link mismatch: {self.drink_id}:{self.effect_index}"
            )
        if (
            self.link.source_kind != self.effect.source_kind
            or self.link.source_id != self.effect.source_id
        ):
            raise DrinkCatalogReferenceError(
                f"ordered reference target mismatch: {self.drink_id}:{self.effect_index}"
            )


@dataclass(frozen=True, slots=True)
class DrinkCatalogDrink:
    """One complete ProduceDrink row plus its Master-ordered references."""

    id: str
    asset_id: str
    name: str
    plan_type: str
    rarity: str
    display_order: str
    library_hidden: bool
    origin_support_card_id: str
    unlock_producer_level: int
    view_start_time: str
    effect_group_ids: tuple[str, ...]
    produce_drink_effect_ids: tuple[str, ...]
    produce_descriptions: tuple[Any, ...]
    raw_json: str
    effect_refs: tuple[DrinkEffectReference, ...]

    def __post_init__(self) -> None:
        for name in ("id", "name", "plan_type", "rarity"):
            _require_text(getattr(self, name), name)
        for name in (
            "asset_id",
            "display_order",
            "origin_support_card_id",
            "view_start_time",
        ):
            _require_optional_text(getattr(self, name), name)
        _require_int(self.unlock_producer_level, "unlock_producer_level")
        if not isinstance(self.library_hidden, bool):
            raise DrinkCatalogSchemaError("library_hidden must be bool")
        if not isinstance(self.effect_group_ids, tuple) or not all(
            isinstance(item, str) for item in self.effect_group_ids
        ):
            raise DrinkCatalogSchemaError("effect_group_ids must be text tuple")
        if not isinstance(self.produce_drink_effect_ids, tuple) or not all(
            isinstance(item, str) and item for item in self.produce_drink_effect_ids
        ):
            raise DrinkCatalogSchemaError(
                "produce_drink_effect_ids must be an id tuple"
            )
        if not isinstance(self.produce_descriptions, tuple):
            raise DrinkCatalogSchemaError("produce_descriptions must be a tuple")
        _json_object(self.raw_json, f"{self.id}.raw_json")
        if len(self.effect_refs) != len(self.produce_drink_effect_ids):
            raise DrinkCatalogReferenceError(
                f"ordered reference count mismatch: {self.id}"
            )
        for index, (ref, effect_id) in enumerate(
            zip(self.effect_refs, self.produce_drink_effect_ids, strict=True)
        ):
            if ref.drink_id != self.id or ref.effect_index != index:
                raise DrinkCatalogReferenceError(
                    f"ordered reference position mismatch: {self.id}:{index}"
                )
            if ref.drink_effect_id != effect_id:
                raise DrinkCatalogReferenceError(
                    f"ordered effect id mismatch: {self.id}:{index}"
                )

    @property
    def plan3_supported(self) -> bool:
        return bool(self.effect_refs) and all(
            ref.effect.plan3_supported for ref in self.effect_refs
        )

    def require_plan3_supported(self) -> "DrinkCatalogDrink":
        for ref in self.effect_refs:
            ref.effect.require_plan3_supported()
        return self


@dataclass(frozen=True, slots=True)
class DrinkCatalog:
    """Immutable catalog with both definitions and ordered usage references."""

    schema_version: str
    drinks: tuple[DrinkCatalogDrink, ...]
    effect_links: tuple[DrinkEffectLink, ...]
    effects: tuple[DrinkEffectCatalogEntry, ...]
    ordered_references: tuple[DrinkEffectReference, ...]

    def __post_init__(self) -> None:
        if self.schema_version != DRINK_CATALOG_SCHEMA_VERSION:
            raise DrinkCatalogSchemaError(
                f"unsupported drink catalog schema: {self.schema_version}"
            )
        if len({drink.id for drink in self.drinks}) != len(self.drinks):
            raise DrinkCatalogReferenceError("duplicate drink ids")
        if len({link.id for link in self.effect_links}) != len(self.effect_links):
            raise DrinkCatalogReferenceError("duplicate drink effect link ids")
        effect_keys = {(effect.source_kind, effect.source_id) for effect in self.effects}
        if len(effect_keys) != len(self.effects):
            raise DrinkCatalogReferenceError("duplicate drink effect catalog keys")

    def get_drink(self, drink_id: str) -> DrinkCatalogDrink:
        for drink in self.drinks:
            if drink.id == drink_id:
                return drink
        raise KeyError(f"Master drink not found: {drink_id}")

    def get_effect(self, source_kind: str, source_id: str) -> DrinkEffectCatalogEntry:
        for effect in self.effects:
            if effect.source_kind == source_kind and effect.source_id == source_id:
                return effect
        raise KeyError(f"Master drink effect not found: {source_kind}:{source_id}")

    def require_plan3_drink(self, drink_id: str) -> DrinkCatalogDrink:
        return self.get_drink(drink_id).require_plan3_supported()

    def coverage_report(self) -> dict[str, object]:
        executable_effects = tuple(effect for effect in self.effects if effect.plan3_supported)
        executable_references = tuple(
            ref for ref in self.ordered_references if ref.effect.plan3_supported
        )
        executable_drinks = tuple(
            drink for drink in self.drinks if drink.plan3_supported
        )
        unsupported_effects = tuple(
            effect for effect in self.effects if not effect.plan3_supported
        )
        unsupported_references = tuple(
            ref for ref in self.ordered_references if not ref.effect.plan3_supported
        )
        effect_type_counts = Counter(effect.gap_family for effect in unsupported_effects)
        reference_type_counts = Counter(
            ref.effect.gap_family for ref in unsupported_references
        )
        return {
            "drink_count": len(self.drinks),
            "effect_definition_count": len(self.effect_links),
            "ordered_reference_count": len(self.ordered_references),
            "distinct_effect_count": len(self.effects),
            "plan3_executable_drink_count": len(executable_drinks),
            "plan3_executable_effect_count": len(executable_effects),
            "plan3_executable_reference_count": len(executable_references),
            "unsupported_drink_count": len(self.drinks) - len(executable_drinks),
            "unsupported_effect_count": len(unsupported_effects),
            "unsupported_reference_count": len(unsupported_references),
            "executable_drink_ids": tuple(drink.id for drink in executable_drinks),
            "unsupported_drink_ids": tuple(
                drink.id for drink in self.drinks if not drink.plan3_supported
            ),
            "unsupported_effect_type_counts": dict(sorted(effect_type_counts.items())),
            "unsupported_effect_reference_type_counts": dict(
                sorted(reference_type_counts.items())
            ),
        }


def _row_to_effect(row: sqlite3.Row) -> DrinkEffectCatalogEntry:
    return DrinkEffectCatalogEntry(
        source_kind=_require_text(row["source_kind"], "source_kind"),
        source_id=_require_text(row["source_id"], "source_id"),
        effect_type=_require_optional_text(row["effect_type"], "effect_type"),
        effect_value1=_require_int(row["effect_value1"], "effect_value1"),
        effect_value2=_require_int(row["effect_value2"], "effect_value2"),
        effect_value_min=_require_int(row["effect_value_min"], "effect_value_min"),
        effect_value_max=_require_int(row["effect_value_max"], "effect_value_max"),
        effect_count=_require_int(row["effect_count"], "effect_count"),
        effect_turn=_require_int(row["effect_turn"], "effect_turn"),
        produce_card_search_id=_require_optional_text(
            row["produce_card_search_id"], "produce_card_search_id"
        ),
        produce_card_search_id2=_require_optional_text(
            row["produce_card_search_id2"], "produce_card_search_id2"
        ),
        produce_card_status_enchant_id=_require_optional_text(
            row["produce_card_status_enchant_id"], "produce_card_status_enchant_id"
        ),
        produce_exam_status_enchant_id=_require_optional_text(
            row["produce_exam_status_enchant_id"],
            "produce_exam_status_enchant_id",
        ),
        produce_exam_trigger_id=_require_optional_text(
            row["produce_exam_trigger_id"], "produce_exam_trigger_id"
        ),
        produce_exam_trigger_effect_ids=_json_string_array(
            row["produce_exam_trigger_effect_ids_json"],
            "produce_exam_trigger_effect_ids_json",
        ),
        chain_produce_exam_effect_id=_require_optional_text(
            row["chain_produce_exam_effect_id"],
            "chain_produce_exam_effect_id",
        ),
        chain_produce_exam_effect_ids=_json_string_array(
            row["chain_produce_exam_effect_ids_json"],
            "chain_produce_exam_effect_ids_json",
        ),
        move_position_type=_require_optional_text(
            row["move_position_type"], "move_position_type"
        ),
        pick_range_type=_require_optional_text(row["pick_range_type"], "pick_range_type"),
        pick_range_type2=_require_optional_text(
            row["pick_range_type2"], "pick_range_type2"
        ),
        pick_count_type=_require_optional_text(
            row["pick_count_type"], "pick_count_type"
        ),
        pick_count_type2=_require_optional_text(
            row["pick_count_type2"], "pick_count_type2"
        ),
        pick_count_min=_require_int(row["pick_count_min"], "pick_count_min"),
        pick_count_max=_require_int(row["pick_count_max"], "pick_count_max"),
        pick_count_min2=_require_int(row["pick_count_min2"], "pick_count_min2"),
        pick_count_max2=_require_int(row["pick_count_max2"], "pick_count_max2"),
        pick_count_reference_produce_card_search_id=_require_optional_text(
            row["pick_count_reference_produce_card_search_id"],
            "pick_count_reference_produce_card_search_id",
        ),
        pick_count_reference_produce_card_search_id2=_require_optional_text(
            row["pick_count_reference_produce_card_search_id2"],
            "pick_count_reference_produce_card_search_id2",
        ),
        target_exam_effect_type=_require_optional_text(
            row["target_exam_effect_type"], "target_exam_effect_type"
        ),
        target_produce_card_id=_require_optional_text(
            row["target_produce_card_id"], "target_produce_card_id"
        ),
        target_upgrade_count=_require_int(
            row["target_upgrade_count"], "target_upgrade_count"
        ),
        produce_card_grow_effect_ids=_json_string_array(
            row["produce_card_grow_effect_ids_json"],
            "produce_card_grow_effect_ids_json",
        ),
        effect_group_ids=_json_string_array(
            row["effect_group_ids_json"], "effect_group_ids_json"
        ),
        produce_resource_type=_require_optional_text(
            row["produce_resource_type"], "produce_resource_type"
        ),
        produce_rewards=_json_array(row["produce_rewards_json"], "produce_rewards_json"),
        produce_step_event_detail_id=_require_optional_text(
            row["produce_step_event_detail_id"], "produce_step_event_detail_id"
        ),
        is_research=bool(_require_int(row["is_research"], "is_research")),
        raw_json=_require_text(row["raw_json"], "raw_json"),
    )


def load_drink_catalog(
    database: Path = DEFAULT_DATABASE,
) -> DrinkCatalog:
    """Load the complete v5 catalog, including unsupported effect families."""

    database = Path(database)
    if not database.is_file():
        raise FileNotFoundError(f"master database not found: {database}")
    try:
        connection = sqlite3.connect(database)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise DrinkCatalogSchemaError(f"cannot open master database: {database}") from exc
    try:
        metadata_rows = connection.execute(
            "SELECT key, value FROM metadata WHERE key IN ('schema_version')"
        ).fetchall()
        metadata = {str(row["key"]): str(row["value"]) for row in metadata_rows}
        if metadata.get("schema_version") != DRINK_CATALOG_SCHEMA_VERSION:
            raise DrinkCatalogSchemaError(
                "drink catalog requires master schema "
                f"{DRINK_CATALOG_SCHEMA_VERSION}, got {metadata.get('schema_version')!r}"
            )

        effect_rows = connection.execute(
            "SELECT * FROM produce_drink_effect_catalog ORDER BY source_kind, source_id"
        ).fetchall()
        effects = tuple(_row_to_effect(row) for row in effect_rows)
        effects_by_key = {(effect.source_kind, effect.source_id): effect for effect in effects}

        link_rows = connection.execute(
            "SELECT id, produce_effect_id, produce_exam_effect_id, raw_json "
            "FROM produce_drink_effect ORDER BY id"
        ).fetchall()
        links = tuple(
            DrinkEffectLink(
                id=_require_text(row["id"], "drink effect link id"),
                produce_effect_id=_require_optional_text(
                    row["produce_effect_id"], "produce_effect_id"
                ),
                produce_exam_effect_id=_require_optional_text(
                    row["produce_exam_effect_id"], "produce_exam_effect_id"
                ),
                raw_json=_require_text(row["raw_json"], "drink effect link raw_json"),
            )
            for row in link_rows
        )
        links_by_id = {link.id: link for link in links}

        ref_rows = connection.execute(
            "SELECT drink_id, effect_index, drink_effect_id, source_kind, source_effect_id "
            "FROM produce_drink_effect_ref ORDER BY drink_id, effect_index"
        ).fetchall()
        refs_by_drink: dict[str, list[DrinkEffectReference]] = {}
        ordered_references: list[DrinkEffectReference] = []
        for row in ref_rows:
            drink_id = _require_text(row["drink_id"], "drink_id")
            effect_index = _require_int(row["effect_index"], "effect_index")
            link_id = _require_text(row["drink_effect_id"], "drink_effect_id")
            link = links_by_id.get(link_id)
            if link is None:
                raise DrinkCatalogReferenceError(
                    f"ordered reference link not found: {drink_id}:{effect_index} -> {link_id}"
                )
            source_kind = _require_text(row["source_kind"], "source_kind")
            source_id = _require_text(row["source_effect_id"], "source_effect_id")
            if (source_kind, source_id) != (link.source_kind, link.source_id):
                raise DrinkCatalogReferenceError(
                    f"ordered reference source mismatch: {drink_id}:{effect_index}"
                )
            effect = effects_by_key.get((source_kind, source_id))
            if effect is None:
                raise DrinkCatalogReferenceError(
                    f"ordered reference effect not found: {source_kind}:{source_id}"
                )
            reference = DrinkEffectReference(
                drink_id=drink_id,
                effect_index=effect_index,
                drink_effect_id=link_id,
                link=link,
                effect=effect,
            )
            refs_by_drink.setdefault(drink_id, []).append(reference)
            ordered_references.append(reference)

        drink_rows = connection.execute(
            "SELECT * FROM produce_drink ORDER BY display_order, id"
        ).fetchall()
        drinks: list[DrinkCatalogDrink] = []
        for row in drink_rows:
            drink_id = _require_text(row["id"], "drink id")
            effect_ids = _json_string_array(
                row["produce_drink_effect_ids_json"],
                f"{drink_id}.produce_drink_effect_ids_json",
            )
            references = tuple(refs_by_drink.get(drink_id, ()))
            if tuple(ref.drink_effect_id for ref in references) != effect_ids:
                raise DrinkCatalogReferenceError(
                    f"drink ordered references do not match JSON order: {drink_id}"
                )
            drinks.append(
                DrinkCatalogDrink(
                    id=drink_id,
                    asset_id=_require_optional_text(row["asset_id"], "asset_id"),
                    name=_require_text(row["name"], f"{drink_id}.name"),
                    plan_type=_require_text(row["plan_type"], f"{drink_id}.plan_type"),
                    rarity=_require_text(row["rarity"], f"{drink_id}.rarity"),
                    display_order=_require_optional_text(
                        row["display_order"], "display_order"
                    ),
                    library_hidden=bool(
                        _require_int(row["library_hidden"], "library_hidden")
                    ),
                    origin_support_card_id=_require_optional_text(
                        row["origin_support_card_id"], "origin_support_card_id"
                    ),
                    unlock_producer_level=_require_int(
                        row["unlock_producer_level"], "unlock_producer_level"
                    ),
                    view_start_time=_require_optional_text(
                        row["view_start_time"], "view_start_time"
                    ),
                    effect_group_ids=_json_string_array(
                        row["effect_group_ids_json"], "effect_group_ids_json"
                    ),
                    produce_drink_effect_ids=effect_ids,
                    produce_descriptions=_json_array(
                        row["produce_descriptions_json"],
                        "produce_descriptions_json",
                    ),
                    raw_json=_require_text(row["raw_json"], "drink raw_json"),
                    effect_refs=references,
                )
            )
        return DrinkCatalog(
            schema_version=metadata["schema_version"],
            drinks=tuple(drinks),
            effect_links=links,
            effects=effects,
            ordered_references=tuple(ordered_references),
        )
    except sqlite3.Error as exc:
        raise DrinkCatalogSchemaError(
            "master database does not contain the complete drink catalog schema"
        ) from exc
    finally:
        connection.close()


__all__ = [
    "DRINK_CATALOG_SCHEMA_VERSION",
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
    "DrinkCatalog",
    "DrinkCatalogDrink",
    "DrinkCatalogError",
    "DrinkCatalogReferenceError",
    "DrinkCatalogSchemaError",
    "DrinkCatalogUnsupportedEffect",
    "DrinkEffectCatalogEntry",
    "DrinkEffectLink",
    "DrinkEffectReference",
    "DrinkEffectShape",
    "MOVE_HOLD",
    "PICK_SELECT",
    "SEARCH_DECK_GRAVE",
    "load_drink_catalog",
]
