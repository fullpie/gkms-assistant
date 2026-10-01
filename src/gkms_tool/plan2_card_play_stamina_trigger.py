"""Standalone Plan2/Common audit and evaluator for one native card-play trigger.

This module deliberately stops at the trigger predicate and Master linkage.  It
does not import ``Plan2State`` or any outer runtime.  The arithmetic is owned by
``plan3_stamina_multiple_trigger`` because that is the shared, native-shaped
predicate; this module only adapts the CardPlay candidate and validates the
exact trigger shape.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from .master_db import DEFAULT_DATABASE
from .plan3_stamina_multiple_trigger import (
    FIELD_STAMINA_UP_MULTIPLE,
    PHASE_CARD_PLAY,
    STAMINA_MULTIPLE_TRIGGER_BY_ID,
    StaminaField,
    StaminaMultipleEvaluation,
    StaminaMultipleTrigger,
    StaminaSnapshot,
    evaluate_stamina_multiple,
)


TRIGGER_ID: Final = "e_trigger-exam_card_play-stamina_up_multiple-500"
CARD_PLAY_TRIGGER_ID: Final = TRIGGER_ID
CARD_PLAY_PHASE_TYPE: Final = PHASE_CARD_PLAY
TRIGGER_THRESHOLD: Final = 500
COMMON_PLAN_TYPE: Final = "ProducePlanType_Common"
MENTAL_SKILL_CATEGORY: Final = "ProduceCardCategory_MentalSkill"
DIRECT_EFFECT_ID: Final = "e_effect-exam_stamina_consumption_down_fix-0001-inf"
DIRECT_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamStaminaConsumptionDownFix"
DIRECT_EFFECT_GROUP_ID: Final = (
    "effect_group-visible-exam_stamina_consumption_down_fix-000"
)
CARD_MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
TRIGGER_CHECK_NOT: Final = "ProduceExamTriggerCheckType_Not"
DIRECT_CARD_SCOPE: Final = "Plan2/Common"
DIRECT_CARD_PLAY_EFFECT_KIND: Final = "direct-card-play-effect"


class CandidateBoundary(str, Enum):
    """The transaction boundary at which the scalar candidate was captured."""

    PRE_PAYMENT = "pre-payment"
    POST_PAYMENT = "post-payment"


@dataclass(frozen=True, slots=True)
class Plan2CardPlayCandidate:
    """Read-only stamina values captured for an ExamCardPlay candidate."""

    current_stamina: int | None
    max_stamina: int | None
    boundary: CandidateBoundary = CandidateBoundary.PRE_PAYMENT

    def __post_init__(self) -> None:
        if self.current_stamina is not None and type(self.current_stamina) is not int:
            raise TypeError("current_stamina must be a plain integer or None")
        if self.max_stamina is not None and type(self.max_stamina) is not int:
            raise TypeError("max_stamina must be a plain integer or None")
        object.__setattr__(self, "boundary", CandidateBoundary(self.boundary))

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_stamina": self.current_stamina,
            "max_stamina": self.max_stamina,
            "boundary": self.boundary.value,
        }


@dataclass(frozen=True, slots=True)
class Plan2CardPlayTriggerShape:
    """The trigger columns that are relevant to this exact native row."""

    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_check_types: tuple[str, ...]
    field_types: tuple[str, ...]
    field_values: tuple[int, ...]
    field_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str

    def __post_init__(self) -> None:
        for name in (
            "phase_types",
            "phase_values",
            "field_check_types",
            "field_types",
            "field_values",
            "field_card_search_ids",
            "effect_types",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @classmethod
    def from_common_row(cls, row: StaminaMultipleTrigger) -> "Plan2CardPlayTriggerShape":
        return cls(
            id=row.id,
            phase_types=row.phase_types,
            phase_values=row.phase_values,
            field_check_types=row.field_check_types,
            field_types=row.field_types,
            field_values=row.field_values,
            field_card_search_ids=row.field_card_search_ids,
            produce_card_search_id=row.produce_card_search_id,
            upper_search_count=row.upper_search_count,
            lower_search_count=row.lower_search_count,
            card_move_position_type=row.card_move_position_type,
            effect_types=row.effect_types,
            lesson_type=row.lesson_type,
        )

    def master_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_check_types": list(self.field_check_types),
            "field_types": list(self.field_types),
            "field_values": list(self.field_values),
            "field_card_search_ids": list(self.field_card_search_ids),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "effect_types": list(self.effect_types),
            "lesson_type": self.lesson_type,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.master_contract()


@dataclass(frozen=True, slots=True)
class Plan2CardPlayStaminaStatusRow:
    """Exact StatusEnchant rows for the target trigger (currently none)."""

    id: str
    asset_id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "child_effect_ids", tuple(self.child_effect_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "asset_id": self.asset_id,
            "trigger_id": self.trigger_id,
            "child_effect_ids": list(self.child_effect_ids),
        }


@dataclass(frozen=True, slots=True)
class Plan2CardPlayStaminaEffectRow:
    """A typed row from the local ``effect`` table."""

    id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect_group_ids", tuple(self.effect_group_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
        }


@dataclass(frozen=True, slots=True)
class Plan2CardPlayStaminaCardRow:
    """One card version whose play-effect list references the exact trigger."""

    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    move_position_type: str
    play_effect_ids: tuple[str, ...]
    target_slot_index: int
    target_effect_id: str
    target_hide_icon: bool | None
    target_is_once_play_effect: bool | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "play_effect_ids", tuple(self.play_effect_ids))

    @property
    def is_common_direct(self) -> bool:
        return self.plan_type == COMMON_PLAN_TYPE

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "name": self.name,
            "plan_type": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "cost_type": self.cost_type,
            "cost_value": self.cost_value,
            "move_position_type": self.move_position_type,
            "play_effect_ids": list(self.play_effect_ids),
            "target_slot_index": self.target_slot_index,
            "target_effect_id": self.target_effect_id,
            "target_hide_icon": self.target_hide_icon,
            "target_is_once_play_effect": self.target_is_once_play_effect,
            "scope": DIRECT_CARD_SCOPE if self.is_common_direct else self.plan_type,
        }


@dataclass(frozen=True, slots=True)
class Plan2CardPlayStaminaCatalog:
    """Read-only exact trigger linkage and standalone direct-card inventory."""

    database: str
    trigger: Plan2CardPlayTriggerShape
    status_rows: tuple[Plan2CardPlayStaminaStatusRow, ...]
    effect_rows: tuple[Plan2CardPlayStaminaEffectRow, ...]
    card_rows: tuple[Plan2CardPlayStaminaCardRow, ...]
    affected_effect_rows: tuple[Plan2CardPlayStaminaEffectRow, ...]
    affected_card_rows: tuple[Plan2CardPlayStaminaCardRow, ...]
    shape_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "status_rows",
            "effect_rows",
            "card_rows",
            "affected_effect_rows",
            "affected_card_rows",
            "shape_issues",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def direct_card_rows(self) -> tuple[Plan2CardPlayStaminaCardRow, ...]:
        return self.card_rows

    @property
    def exact_shape_supported(self) -> bool:
        return not self.shape_issues

    @property
    def standalone_rows(self) -> dict[str, int]:
        return {
            "trigger": 1,
            "status": len(self.status_rows),
            "effect": len(self.effect_rows),
            "direct_card": len(self.card_rows),
        }

    def to_dict(self, *, include_affected: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "database": self.database,
            "trigger": self.trigger.to_dict(),
            "status_rows": [row.to_dict() for row in self.status_rows],
            "effect_rows": [row.to_dict() for row in self.effect_rows],
            "card_rows": [row.to_dict() for row in self.card_rows],
            "standalone_rows": self.standalone_rows,
            "shape_issues": list(self.shape_issues),
            "exact_shape_supported": self.exact_shape_supported,
        }
        if include_affected:
            result["affected_effect_rows"] = [
                row.to_dict() for row in self.affected_effect_rows
            ]
            result["affected_card_rows"] = [
                row.to_dict() for row in self.affected_card_rows
            ]
        return result


@dataclass(frozen=True, slots=True)
class Plan2CardPlayStaminaEvaluation:
    """Pure trigger decision; ``fires=None`` is the fail-closed result."""

    trigger_id: str
    event_phase: str | None
    candidate: Plan2CardPlayCandidate | None
    formula: StaminaMultipleEvaluation | None
    fires: bool | None
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True
    source_kind: str = DIRECT_CARD_PLAY_EFFECT_KIND

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def resolved(self) -> bool:
        return self.fires is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "event_phase": self.event_phase,
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "formula": None if self.formula is None else self.formula.to_dict(),
            "fires": self.fires,
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
            "source_kind": self.source_kind,
        }


_MISSING: Final = object()
_TARGET_TRIGGER_ROW: Final = STAMINA_MULTIPLE_TRIGGER_BY_ID[TRIGGER_ID]
TARGET_TRIGGER_SHAPE: Final = Plan2CardPlayTriggerShape.from_common_row(
    _TARGET_TRIGGER_ROW
)


def _json_array(
    value: object,
    *,
    label: str,
    issues: list[str],
) -> tuple[Any, ...]:
    try:
        parsed = json.loads(value if isinstance(value, str) else str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        issues.append(f"invalid-json:{label}")
        return ()
    if not isinstance(parsed, list):
        issues.append(f"json-array-required:{label}")
        return ()
    return tuple(parsed)


def _text(value: object) -> str:
    return value if isinstance(value, str) else str(value)


def _trigger_shape_from_db_row(
    row: sqlite3.Row,
    issues: list[str],
) -> Plan2CardPlayTriggerShape:
    arrays = {
        key: _json_array(row[column], label=column, issues=issues)
        for key, column in (
            ("phase_types", "phase_types_json"),
            ("phase_values", "phase_values_json"),
            ("field_check_types", "field_status_check_types_json"),
            ("field_types", "field_status_types_json"),
            ("field_values", "field_status_values_json"),
            ("field_card_search_ids", "field_status_produce_card_search_ids_json"),
            ("effect_types", "effect_types_json"),
        )
    }
    return Plan2CardPlayTriggerShape(
        id=_text(row["id"]),
        phase_types=tuple(str(value) for value in arrays["phase_types"]),
        phase_values=tuple(value for value in arrays["phase_values"]),
        field_check_types=tuple(str(value) for value in arrays["field_check_types"]),
        field_types=tuple(str(value) for value in arrays["field_types"]),
        field_values=tuple(value for value in arrays["field_values"]),
        field_card_search_ids=tuple(
            str(value) for value in arrays["field_card_search_ids"]
        ),
        produce_card_search_id=_text(row["produce_card_search_id"]),
        upper_search_count=row["upper_search_count"],
        lower_search_count=row["lower_search_count"],
        card_move_position_type=_text(row["card_move_position_type"]),
        effect_types=tuple(str(value) for value in arrays["effect_types"]),
        lesson_type=_text(row["lesson_type"]),
    )


def _status_from_db_row(row: sqlite3.Row, issues: list[str]) -> Plan2CardPlayStaminaStatusRow:
    effects = _json_array(
        row["produce_exam_effect_ids_json"],
        label=f"status:{row['id']}:produce_exam_effect_ids_json",
        issues=issues,
    )
    return Plan2CardPlayStaminaStatusRow(
        id=_text(row["id"]),
        asset_id=_text(row["asset_id"]),
        trigger_id=_text(row["produce_exam_trigger_id"]),
        child_effect_ids=tuple(str(value) for value in effects),
    )


def _effect_from_db_row(row: sqlite3.Row, issues: list[str]) -> Plan2CardPlayStaminaEffectRow:
    try:
        raw = json.loads(row["raw_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
        issues.append(f"invalid-json:effect:{row['id']}:raw_json")
    groups = raw.get("effectGroupIds", ()) if isinstance(raw, dict) else ()
    if not isinstance(groups, list):
        issues.append(f"json-array-required:effect:{row['id']}:effectGroupIds")
        groups = ()
    return Plan2CardPlayStaminaEffectRow(
        id=_text(row["id"]),
        effect_type=_text(row["effect_type"]),
        value1=row["value1"],
        value2=row["value2"],
        effect_count=row["effect_count"],
        effect_turn=row["effect_turn"],
        status_enchant_id=_text(row["status_enchant_id"]),
        chain_effect_id=_text(row["chain_effect_id"]),
        effect_group_ids=tuple(str(value) for value in groups),
    )


def _card_rows_from_db_row(
    row: sqlite3.Row,
    trigger_id: str,
    issues: list[str],
) -> list[Plan2CardPlayStaminaCardRow]:
    try:
        play_effects = json.loads(row["play_effects_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        issues.append(f"invalid-json:card:{row['id']}:play_effects_json")
        return []
    if not isinstance(play_effects, list):
        issues.append(f"json-array-required:card:{row['id']}:play_effects_json")
        return []

    effect_ids: list[str] = []
    for item in play_effects:
        effect_ids.append(
            _text(item.get("produceExamEffectId", ""))
            if isinstance(item, Mapping)
            else ""
        )

    result: list[Plan2CardPlayStaminaCardRow] = []
    for index, item in enumerate(play_effects):
        if not isinstance(item, Mapping):
            continue
        if item.get("produceExamTriggerId") != trigger_id:
            continue
        result.append(
            Plan2CardPlayStaminaCardRow(
                card_id=_text(row["id"]),
                upgrade_count=row["upgrade_count"],
                name=_text(row["name"]),
                plan_type=_text(row["plan_type"]),
                category=_text(row["category"]),
                stamina=row["stamina"],
                cost_type=_text(row["cost_type"]),
                cost_value=row["cost_value"],
                move_position_type=_text(row["move_position_type"]),
                play_effect_ids=tuple(effect_ids),
                target_slot_index=index,
                target_effect_id=_text(item.get("produceExamEffectId", "")),
                target_hide_icon=(
                    item.get("hideIcon")
                    if type(item.get("hideIcon")) is bool
                    else None
                ),
                target_is_once_play_effect=(
                    item.get("isOncePlayEffect")
                    if type(item.get("isOncePlayEffect")) is bool
                    else None
                ),
            )
        )
    return result


def _unique_in_order(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def load_plan2_card_play_stamina_catalog(
    database: Path = DEFAULT_DATABASE,
) -> Plan2CardPlayStaminaCatalog:
    """Read the exact target linkage from local Master using a read-only DB URI."""

    database = Path(database)
    issues: list[str] = []
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TRIGGER_ID,),
        ).fetchone()
        if trigger_row is None:
            raise LookupError(f"target trigger is absent from Master: {TRIGGER_ID}")
        trigger = _trigger_shape_from_db_row(trigger_row, issues)

        status_rows = tuple(
            _status_from_db_row(row, issues)
            for row in connection.execute(
                "SELECT * FROM produce_exam_status_enchant "
                "WHERE produce_exam_trigger_id = ? ORDER BY id",
                (TRIGGER_ID,),
            )
        )
        if status_rows:
            issues.append("target-trigger-has-status-enchant-rows")

        affected_cards: list[Plan2CardPlayStaminaCardRow] = []
        for row in connection.execute(
            "SELECT * FROM card WHERE play_effects_json LIKE ? "
            "ORDER BY id, upgrade_count",
            (f"%{TRIGGER_ID}%",),
        ):
            affected_cards.extend(_card_rows_from_db_row(row, TRIGGER_ID, issues))

        direct_cards = tuple(row for row in affected_cards if row.is_common_direct)
        if len(direct_cards) != 4:
            issues.append(f"unexpected-direct-card-row-count:{len(direct_cards)}")
        for row in direct_cards:
            if row.target_effect_id != DIRECT_EFFECT_ID:
                issues.append(f"direct-card-effect-mismatch:{row.card_id}:{row.upgrade_count}")

        all_effect_ids = _unique_in_order(
            [row.target_effect_id for row in affected_cards if row.target_effect_id]
        )
        direct_effect_ids = _unique_in_order(
            [row.target_effect_id for row in direct_cards if row.target_effect_id]
        )
        effects_by_id: dict[str, Plan2CardPlayStaminaEffectRow] = {}
        for effect_id in all_effect_ids:
            effect_row = connection.execute(
                "SELECT * FROM effect WHERE id = ?",
                (effect_id,),
            ).fetchone()
            if effect_row is None:
                issues.append(f"missing-effect-row:{effect_id}")
                continue
            effects_by_id[effect_id] = _effect_from_db_row(effect_row, issues)

    expected_contract = TARGET_TRIGGER_SHAPE.master_contract()
    actual_contract = trigger.master_contract()
    for key, expected in expected_contract.items():
        if actual_contract.get(key) != expected:
            issues.append(f"trigger-contract-mismatch:{key}")
    return Plan2CardPlayStaminaCatalog(
        database=str(database),
        trigger=trigger,
        status_rows=status_rows,
        effect_rows=tuple(
            effects_by_id[effect_id]
            for effect_id in direct_effect_ids
            if effect_id in effects_by_id
        ),
        card_rows=direct_cards,
        affected_effect_rows=tuple(
            effects_by_id[effect_id]
            for effect_id in all_effect_ids
            if effect_id in effects_by_id
        ),
        affected_card_rows=tuple(affected_cards),
        shape_issues=tuple(dict.fromkeys(issues)),
    )


def _mapping_value(
    source: Mapping[object, object],
    *names: str,
) -> tuple[bool, object]:
    for name in names:
        if name in source:
            return True, source[name]
    return False, None


def _tuple_from_mapping(
    source: Mapping[object, object],
    *names: str,
) -> tuple[bool, tuple[object, ...] | None]:
    found, value = _mapping_value(source, *names)
    if not found:
        return False, None
    if not isinstance(value, (list, tuple)):
        return True, None
    return True, tuple(value)


def _shape_from_mapping(source: Mapping[object, object]) -> Plan2CardPlayTriggerShape | None:
    scalar_names = {
        "id": ("id",),
        "produce_card_search_id": ("produce_card_search_id", "produceCardSearchId"),
        "upper_search_count": ("upper_search_count", "upperSearchCount"),
        "lower_search_count": ("lower_search_count", "lowerSearchCount"),
        "card_move_position_type": (
            "card_move_position_type",
            "cardMovePositionType",
        ),
        "lesson_type": ("lesson_type", "lessonType"),
    }
    scalars: dict[str, object] = {}
    for field_name, names in scalar_names.items():
        found, value = _mapping_value(source, *names)
        if not found:
            return None
        scalars[field_name] = value
    arrays: dict[str, tuple[object, ...]] = {}
    array_names = {
        "phase_types": ("phase_types", "phaseTypes"),
        "phase_values": ("phase_values", "phaseValues"),
        "field_check_types": ("field_check_types", "fieldStatusCheckTypes"),
        "field_types": ("field_types", "fieldStatusTypes"),
        "field_values": ("field_values", "fieldStatusValues"),
        "field_card_search_ids": (
            "field_card_search_ids",
            "fieldStatusProduceCardSearchIds",
        ),
        "effect_types": ("effect_types", "effectTypes"),
    }
    for field_name, names in array_names.items():
        found, values = _tuple_from_mapping(source, *names)
        if not found or values is None:
            return None
        arrays[field_name] = values
    if not isinstance(scalars["id"], str):
        return None
    if not isinstance(scalars["produce_card_search_id"], str):
        return None
    if type(scalars["upper_search_count"]) is not int:
        return None
    if type(scalars["lower_search_count"]) is not int:
        return None
    if not isinstance(scalars["card_move_position_type"], str):
        return None
    if not isinstance(scalars["lesson_type"], str):
        return None
    if not all(isinstance(value, str) for value in arrays["phase_types"]):
        return None
    if not all(type(value) is int for value in arrays["phase_values"]):
        return None
    if not all(isinstance(value, str) for value in arrays["field_check_types"]):
        return None
    if not all(isinstance(value, str) for value in arrays["field_types"]):
        return None
    if not all(type(value) is int for value in arrays["field_values"]):
        return None
    if not all(isinstance(value, str) for value in arrays["field_card_search_ids"]):
        return None
    if not all(isinstance(value, str) for value in arrays["effect_types"]):
        return None
    return Plan2CardPlayTriggerShape(
        id=scalars["id"],
        phase_types=tuple(arrays["phase_types"]),
        phase_values=tuple(arrays["phase_values"]),
        field_check_types=tuple(arrays["field_check_types"]),
        field_types=tuple(arrays["field_types"]),
        field_values=tuple(arrays["field_values"]),
        field_card_search_ids=tuple(arrays["field_card_search_ids"]),
        produce_card_search_id=scalars["produce_card_search_id"],
        upper_search_count=scalars["upper_search_count"],
        lower_search_count=scalars["lower_search_count"],
        card_move_position_type=scalars["card_move_position_type"],
        effect_types=tuple(arrays["effect_types"]),
        lesson_type=scalars["lesson_type"],
    )


def _coerce_trigger_shape(source: object) -> Plan2CardPlayTriggerShape | None:
    if isinstance(source, Plan2CardPlayTriggerShape):
        return source
    if isinstance(source, StaminaMultipleTrigger):
        if source.id != TRIGGER_ID:
            return None
        return Plan2CardPlayTriggerShape.from_common_row(source)
    if isinstance(source, str):
        if source != TRIGGER_ID:
            return None
        return TARGET_TRIGGER_SHAPE
    if isinstance(source, Mapping):
        return _shape_from_mapping(source)
    return None


def _trigger_shape_reasons(shape: Plan2CardPlayTriggerShape) -> tuple[str, ...]:
    expected = TARGET_TRIGGER_SHAPE.master_contract()
    actual = shape.master_contract()
    reasons: list[str] = []
    if actual.get("id") != TRIGGER_ID:
        reasons.append("unknown-or-non-target-trigger-id")
    if actual.get("field_check_types"):
        if any(
            "not" in str(value).lower()
            for value in actual["field_check_types"]
        ):
            reasons.append("trigger-not-check-unproven")
        else:
            reasons.append("trigger-field-check-shape-not-empty")
    for key, expected_value in expected.items():
        if key == "field_check_types":
            continue
        if actual.get(key) != expected_value:
            reasons.append(f"trigger-shape-mismatch:{key}")
    return tuple(dict.fromkeys(reasons))


def _source_value(source: object, *names: str) -> tuple[bool, object]:
    for name in names:
        try:
            value = getattr(source, name)
        except AttributeError:
            continue
        except Exception:
            return True, None
        return True, value
    return False, None


def _boundary_value(source: object, default: CandidateBoundary) -> CandidateBoundary | None:
    if isinstance(source, Mapping):
        found, value = _mapping_value(source, "boundary", "capture_boundary")
        if not found:
            value = default
    else:
        found, value = _source_value(source, "boundary", "capture_boundary")
        if not found:
            value = default
    try:
        return CandidateBoundary(value)
    except (TypeError, ValueError):
        return None


def _candidate_values(source: object) -> tuple[bool, object, bool, object]:
    if isinstance(source, Mapping):
        current_found, current = _mapping_value(
            source,
            "current_stamina",
            "currentStamina",
            "stamina",
        )
        max_found, maximum = _mapping_value(
            source,
            "max_stamina",
            "maxStamina",
            "maximum_stamina",
        )
        return current_found, current, max_found, maximum
    current_found, current = _source_value(
        source,
        "current_stamina",
        "currentStamina",
        "stamina",
    )
    max_found, maximum = _source_value(
        source,
        "max_stamina",
        "maxStamina",
        "maximum_stamina",
    )
    return current_found, current, max_found, maximum


def adapt_plan2_card_play_candidate(
    source: object,
    *,
    capture_boundary: CandidateBoundary | str = CandidateBoundary.PRE_PAYMENT,
) -> Plan2CardPlayCandidate | None:
    """Adapt a snapshot/state-like source without importing Plan2State.

    A missing max-stamina member is retained as ``None`` so the evaluator can
    report the exact fail-closed reason.  A source with no recognized fields,
    or with non-integer scalar values, is rejected rather than guessed.
    """

    if isinstance(source, Plan2CardPlayCandidate):
        return source
    if isinstance(source, StaminaSnapshot):
        try:
            boundary = CandidateBoundary(capture_boundary)
            return Plan2CardPlayCandidate(
                source.current_stamina,
                source.max_stamina,
                boundary,
            )
        except (TypeError, ValueError):
            return None
    if source is None:
        return None
    current_found, current, max_found, maximum = _candidate_values(source)
    if not current_found and not max_found:
        return None
    boundary = _boundary_value(source, CandidateBoundary(capture_boundary))
    if boundary is None:
        return None
    if not current_found:
        current = None
    if not max_found:
        maximum = None
    try:
        return Plan2CardPlayCandidate(current, maximum, boundary)
    except (TypeError, ValueError):
        return None


def _trigger_label(source: object) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, (Plan2CardPlayTriggerShape, StaminaMultipleTrigger)):
        return source.id
    return "<unknown-trigger-shape>"


def evaluate_plan2_card_play_stamina_trigger(
    trigger_or_id: object = TRIGGER_ID,
    candidate: object | None = None,
    *,
    event_phase: str | None = CARD_PLAY_PHASE_TYPE,
    current_stamina: object = _MISSING,
    max_stamina: object = _MISSING,
    capture_boundary: CandidateBoundary | str = CandidateBoundary.PRE_PAYMENT,
) -> Plan2CardPlayStaminaEvaluation:
    """Evaluate the exact CardPlay trigger using a pre-payment candidate.

    ``current_stamina``/``max_stamina`` are convenience inputs for a pure
    caller.  A runtime adapter may instead provide a state-like ``candidate``.
    The candidate is never mutated or recomputed after payment.  Any trigger,
    phase, candidate, or status shape outside the proven contract returns
    ``fires=None``.
    """

    trigger_id = _trigger_label(trigger_or_id)
    shape = _coerce_trigger_shape(trigger_or_id)
    reasons: list[str] = []
    if shape is None:
        reasons.append("unknown-trigger-shape")
    else:
        reasons.extend(_trigger_shape_reasons(shape))
        if event_phase != CARD_PLAY_PHASE_TYPE:
            reasons.append("event-phase-mismatch-or-unproven")

    candidate_source = candidate
    if candidate_source is None and (
        current_stamina is not _MISSING or max_stamina is not _MISSING
    ):
        candidate_source = {
            "current_stamina": (
                None if current_stamina is _MISSING else current_stamina
            ),
            "max_stamina": None if max_stamina is _MISSING else max_stamina,
            "boundary": capture_boundary,
        }
    adapted = (
        adapt_plan2_card_play_candidate(
            candidate_source,
            capture_boundary=capture_boundary,
        )
        if candidate_source is not None
        else None
    )
    formula: StaminaMultipleEvaluation | None = None
    if adapted is None:
        reasons.append("card-play-candidate-unavailable")
    elif adapted.boundary is not CandidateBoundary.PRE_PAYMENT:
        reasons.append("card-play-candidate-not-pre-payment")
    elif adapted.current_stamina is None:
        reasons.append("current-stamina-missing")
    elif adapted.max_stamina is None:
        reasons.append("max-stamina-missing")
    else:
        # This is the shared native-shaped predicate.  No arithmetic is copied
        # into the Plan2 adapter.
        formula = evaluate_stamina_multiple(
            StaminaField.STAMINA_UP_MULTIPLE,
            TRIGGER_THRESHOLD,
            StaminaSnapshot(adapted.current_stamina, adapted.max_stamina),
        )
        reasons.extend(formula.reasons)

    unique_reasons = tuple(dict.fromkeys(reasons))
    fires = (
        formula.fires
        if formula is not None and not unique_reasons
        else None
    )
    return Plan2CardPlayStaminaEvaluation(
        trigger_id=trigger_id,
        event_phase=event_phase,
        candidate=adapted,
        formula=formula,
        fires=fires,
        reasons=unique_reasons,
    )


evaluate = evaluate_plan2_card_play_stamina_trigger


__all__ = [
    "CARD_PLAY_PHASE_TYPE",
    "CARD_PLAY_TRIGGER_ID",
    "CARD_MOVE_LOST",
    "CandidateBoundary",
    "COMMON_PLAN_TYPE",
    "DIRECT_CARD_PLAY_EFFECT_KIND",
    "DIRECT_CARD_SCOPE",
    "DIRECT_EFFECT_GROUP_ID",
    "DIRECT_EFFECT_ID",
    "DIRECT_EFFECT_TYPE",
    "FIELD_STAMINA_UP_MULTIPLE",
    "MENTAL_SKILL_CATEGORY",
    "Plan2CardPlayCandidate",
    "Plan2CardPlayStaminaCardRow",
    "Plan2CardPlayStaminaCatalog",
    "Plan2CardPlayStaminaEffectRow",
    "Plan2CardPlayStaminaEvaluation",
    "Plan2CardPlayStaminaStatusRow",
    "Plan2CardPlayTriggerShape",
    "TARGET_TRIGGER_SHAPE",
    "TRIGGER_CHECK_NOT",
    "TRIGGER_ID",
    "TRIGGER_THRESHOLD",
    "adapt_plan2_card_play_candidate",
    "evaluate",
    "evaluate_plan2_card_play_stamina_trigger",
    "load_plan2_card_play_stamina_catalog",
]
