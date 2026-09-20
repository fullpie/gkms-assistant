"""Bounded Plan2 card-admission leaf for ``p_card-02-act-100_010``.

This module owns exactly one thing: the card-owned
``e_trigger-none-remaining_turn-1`` admission predicate and a read-only
handoff to the rest of the card command.  It does not pay the card, execute
the direct ``ExamLessonDependBlockConsumptionSum`` formula, run the Hand move
effect, update counters/history, or move a card to Lost.

The Android v3.2.3 field-status native path reads the signed current
``ExamParameterModel.RemainTurn`` value and uses a signed less-or-equal branch
against the Master value.  The PC metadata snapshot corroborates that
``RemainTurn``, ``CurrentTurn``, ``LimitTurn``, ``ExtraTurn`` and ``Phase`` are
separate ExamParameterModel members/properties.  ExtraTurn is therefore
captured as context but is not folded into the gate value.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from .master_db import DEFAULT_DATABASE


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COVERAGE_ARTIFACT: Final = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_card_executable_coverage.json"
)
NATIVE_AUDIT_ARTIFACT: Final = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_card_trigger_remaining_turn1_native_audit.json"
)

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1

TARGET_CARD_ID: Final = "p_card-02-act-100_010"
TARGET_CARD_NAME: Final = "輝きの到達点"
TARGET_CARD_UPGRADE: Final = 0
TARGET_CARD_REF: Final = (TARGET_CARD_ID, TARGET_CARD_UPGRADE)
TARGET_TRIGGER_ID: Final = "e_trigger-none-remaining_turn-1"
TARGET_THRESHOLD: Final = 1

PLAN2: Final = "ProducePlanType_Plan2"
ACTIVE_SKILL: Final = "ProduceCardCategory_ActiveSkill"
COST_TYPE_UNKNOWN: Final = "ExamCostType_Unknown"
CARD_DESTINATION_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_EFFECT_TRIGGER_HAND: Final = "ProduceCardMoveEffectTriggerType_Hand"
UNKNOWN_MOVE: Final = "ProduceCardMovePositionType_Unknown"
UNKNOWN_LESSON: Final = "ProduceStepLessonType_Unknown"
FIELD_REMAINING_TURN: Final = "ProduceExamFieldStatusType_RemainingTurn"
TRIGGER_PHASE_NONE: Final = "ProduceExamPhaseType_None"
RUNTIME_CARD_PLAY_PHASE: Final = "ProduceExamPhaseType_ExamCardPlay"
TRIGGER_CHECK_NOT: Final = "ProduceExamTriggerCheckType_Not"

DIRECT_EFFECT_ID: Final = (
    "e_effect-exam_lesson_depend_block_consumption_sum-4000-01"
)
MOVE_EFFECT_ID: Final = "e_effect-exam_block-0005"
DIRECT_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonDependBlockConsumptionSum"
)
MOVE_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlock"

CARD_ORIGINS: Final = ("normal", "forced", "extra-play")
AdmissionOrigin = Literal["normal", "forced", "extra-play"]

# This is the formal card-row slot order, retained for accounting and audit.
# The native command chronology is represented separately by DOWNSTREAM_ORDER.
MASTER_SLOT_ORDER: Final = (
    ("cost", COST_TYPE_UNKNOWN),
    ("play-destination", CARD_DESTINATION_LOST),
    ("card-trigger", TARGET_TRIGGER_ID),
    ("effect", DIRECT_EFFECT_TYPE),
    ("move-effect-runtime", MOVE_EFFECT_ID),
    ("effect", MOVE_EFFECT_TYPE),
)

DOWNSTREAM_ORDER: Final = (
    "payment:stamina-1",
    f"direct-play-effect[0]:{DIRECT_EFFECT_ID}",
    "card-count-and-history:native PlayCardCount/UserCardAfterCheck",
    f"move:{CARD_DESTINATION_LOST}",
    f"move-effect-runtime[0]:{MOVE_EFFECT_ID}",
)

BLOCKERS: Final = (
    "C:effect:ProduceExamEffectType_ExamLessonDependBlockConsumptionSum",
    "C:move-effect-runtime:e_effect-exam_block-0005",
)


class RemainingTurnContractError(ValueError):
    """The supplied Master/native shape is outside this exact leaf."""


def _strict_int(value: object, label: str, *, signed: bool = True) -> int:
    if type(value) is not int:
        raise RemainingTurnContractError(f"{label} must be a plain integer")
    if signed and not INT32_MIN <= value <= INT32_MAX:
        raise RemainingTurnContractError(f"{label} is outside signed Int32")
    if not signed and value < 0:
        raise RemainingTurnContractError(f"{label} must be non-negative")
    return value


def _json_value(value: object, label: str) -> object:
    try:
        parsed = json.loads(value if isinstance(value, str) else str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise RemainingTurnContractError(f"{label}: invalid JSON") from error
    return parsed


def _json_object(value: object, label: str) -> dict[str, object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, dict):
        raise RemainingTurnContractError(f"{label}: expected JSON object")
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise RemainingTurnContractError(f"{label}: expected JSON array")
    return parsed


def _strict_equal(actual: object, expected: object, label: str) -> None:
    if type(actual) is not type(expected) or actual != expected:
        raise RemainingTurnContractError(
            f"{label}: got {actual!r}, expected {expected!r}"
        )


def _strict_raw_fields(
    raw: Mapping[str, object], expected: Mapping[str, object], label: str
) -> None:
    for key, wanted in expected.items():
        if key not in raw:
            raise RemainingTurnContractError(f"{label}: missing raw field {key}")
        _strict_equal(raw[key], wanted, f"{label}.{key}")


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not str for item in parsed):
        raise RemainingTurnContractError(f"{label}: expected string entries")
    return tuple(parsed)  # type: ignore[arg-type]


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not int for item in parsed):
        raise RemainingTurnContractError(f"{label}: expected integer entries")
    return tuple(parsed)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RemainingTurnTriggerRow:
    """All trigger fields needed to reject unknown/altered shapes."""

    trigger_id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.trigger_id,
            "phaseTypes": list(self.phase_types),
            "phaseValues": list(self.phase_values),
            "fieldStatusCheckTypes": list(self.field_status_check_types),
            "fieldStatusTypes": list(self.field_status_types),
            "fieldStatusValues": list(self.field_status_values),
            "fieldStatusProduceCardSearchIds": list(
                self.field_status_card_search_ids
            ),
            "produceCardSearchId": self.produce_card_search_id,
            "upperSearchCount": self.upper_search_count,
            "lowerSearchCount": self.lower_search_count,
            "cardMovePositionType": self.card_move_position_type,
            "effectTypes": list(self.effect_types),
            "lessonType": self.lesson_type,
        }


EXACT_TARGET_TRIGGER: Final = RemainingTurnTriggerRow(
    trigger_id=TARGET_TRIGGER_ID,
    phase_types=(TRIGGER_PHASE_NONE,),
    phase_values=(),
    field_status_check_types=(),
    field_status_types=(FIELD_REMAINING_TURN,),
    field_status_values=(TARGET_THRESHOLD,),
    field_status_card_search_ids=(),
    produce_card_search_id="",
    upper_search_count=0,
    lower_search_count=0,
    card_move_position_type=UNKNOWN_MOVE,
    effect_types=(),
    lesson_type=UNKNOWN_LESSON,
)


@dataclass(frozen=True, slots=True)
class TriggerResolution:
    supported: bool
    threshold: int | None
    reasons: tuple[str, ...] = ()


def resolve_exact_remaining_turn_trigger(
    trigger: object = EXACT_TARGET_TRIGGER,
) -> TriggerResolution:
    """Accept only the exact positive None/RemainingTurn/1 row."""

    if not isinstance(trigger, RemainingTurnTriggerRow):
        return TriggerResolution(False, None, ("trigger-row-type-is-unknown",))
    reasons: list[str] = []
    if trigger.trigger_id != TARGET_TRIGGER_ID:
        reasons.append("trigger-id-is-not-exact-target")
    if trigger.phase_types != (TRIGGER_PHASE_NONE,):
        reasons.append("trigger-phase-types-changed")
    if trigger.phase_values:
        reasons.append("trigger-phase-values-must-be-empty")
    if trigger.field_status_check_types:
        # In particular, do not guess an inversion for ProduceExamTriggerCheckType_Not.
        if TRIGGER_CHECK_NOT in trigger.field_status_check_types:
            reasons.append("trigger-not-inversion-is-unsupported")
        else:
            reasons.append("trigger-field-check-shape-is-unsupported")
    if trigger.field_status_types != (FIELD_REMAINING_TURN,):
        reasons.append("trigger-field-is-not-remaining-turn")
    if trigger.field_status_values != (TARGET_THRESHOLD,):
        reasons.append("trigger-value-shape-is-not-exact-one")
    if trigger.field_status_card_search_ids:
        reasons.append("trigger-field-search-shape-is-unsupported")
    if trigger.produce_card_search_id:
        reasons.append("trigger-card-search-shape-is-unsupported")
    if trigger.upper_search_count != 0 or trigger.lower_search_count != 0:
        reasons.append("trigger-search-counts-must-be-zero")
    if trigger.card_move_position_type != UNKNOWN_MOVE:
        reasons.append("trigger-card-move-shape-is-unsupported")
    if trigger.effect_types:
        reasons.append("trigger-effect-type-shape-is-unsupported")
    if trigger.lesson_type != UNKNOWN_LESSON:
        reasons.append("trigger-lesson-type-shape-is-unsupported")
    unique = tuple(dict.fromkeys(reasons))
    return TriggerResolution(not unique, TARGET_THRESHOLD if not unique else None, unique)


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.slot_index,
            "effectId": self.effect_id,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class RemainingTurnCardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    force_stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    play_move_position_type: str
    move_effect_trigger_type: str
    play_effect_slots: tuple[CardEffectSlot, ...]
    move_effect_ids: tuple[str, ...]
    is_end_turn_lost: bool
    direct_effect_type: str
    move_effect_type: str

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade_count

    @property
    def play_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.play_effect_slots)

    @property
    def master_slot_order(self) -> tuple[tuple[str, str], ...]:
        return MASTER_SLOT_ORDER

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade_count,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "forceStamina": self.force_stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "playMovePositionType": self.play_move_position_type,
            "moveEffectTriggerType": self.move_effect_trigger_type,
            "playEffects": [slot.to_dict() for slot in self.play_effect_slots],
            "moveProduceExamEffectIds": list(self.move_effect_ids),
            "isEndTurnLost": self.is_end_turn_lost,
            "directEffectType": self.direct_effect_type,
            "moveEffectType": self.move_effect_type,
            "masterSlotOrder": [
                {"kind": kind, "id": identifier}
                for kind, identifier in self.master_slot_order
            ],
        }


EXACT_TARGET_CARD: Final = RemainingTurnCardVersion(
    card_id=TARGET_CARD_ID,
    upgrade_count=TARGET_CARD_UPGRADE,
    name=TARGET_CARD_NAME,
    plan_type=PLAN2,
    category=ACTIVE_SKILL,
    stamina=1,
    force_stamina=0,
    cost_type=COST_TYPE_UNKNOWN,
    cost_value=0,
    play_trigger_id=TARGET_TRIGGER_ID,
    play_move_position_type=CARD_DESTINATION_LOST,
    move_effect_trigger_type=MOVE_EFFECT_TRIGGER_HAND,
    play_effect_slots=(
        CardEffectSlot(
            slot_index=0,
            effect_id=DIRECT_EFFECT_ID,
            trigger_id="",
            hide_icon=False,
            is_once_play_effect=False,
        ),
    ),
    move_effect_ids=(MOVE_EFFECT_ID,),
    is_end_turn_lost=False,
    direct_effect_type=DIRECT_EFFECT_TYPE,
    move_effect_type=MOVE_EFFECT_TYPE,
)


@dataclass(frozen=True, slots=True)
class CoverageReadback:
    source: str
    classification: str
    gaps: tuple[str, ...]
    slots: tuple[tuple[str, str], ...]

    @property
    def affected(self) -> int:
        return 1

    @property
    def direct(self) -> int:
        return int(self.classification in {"A", "B"} and not self.gaps)

    @property
    def co_blocked(self) -> int:
        return self.affected - self.direct

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "scope": "one exact Plan2 card version; card-trigger admission only",
            "classification": self.classification,
            "gaps": list(self.gaps),
            "slots": [
                {"kind": kind, "id": identifier}
                for kind, identifier in self.slots
            ],
            "affected": self.affected,
            "direct": self.direct,
            "coBlocked": self.co_blocked,
        }


def load_coverage_readback(
    artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
) -> CoverageReadback:
    """Read the existing formal coverage JSON without rebuilding or writing it."""

    path = Path(artifact)
    if not path.is_file():
        raise RemainingTurnContractError(f"coverage artifact not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RemainingTurnContractError(
            f"cannot read coverage artifact: {path}"
        ) from error
    if not isinstance(data, dict):
        raise RemainingTurnContractError("coverage artifact must be an object")
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise RemainingTurnContractError("coverage cards list is missing")
    matches = [
        item
        for item in cards
        if isinstance(item, dict)
        and item.get("card_id") == TARGET_CARD_ID
        and item.get("upgrade") == TARGET_CARD_UPGRADE
    ]
    if len(matches) != 1:
        raise RemainingTurnContractError("target coverage row is not unique")
    row = matches[0]
    classification = row.get("classification")
    gaps = row.get("gaps")
    raw_slots = row.get("slots")
    if type(classification) is not str or not isinstance(gaps, list):
        raise RemainingTurnContractError("target coverage row is malformed")
    if any(type(gap) is not str for gap in gaps):
        raise RemainingTurnContractError("target coverage gaps are malformed")
    if not isinstance(raw_slots, list):
        raise RemainingTurnContractError("target coverage slots are missing")
    slots: list[tuple[str, str]] = []
    for index, raw_slot in enumerate(raw_slots):
        if not isinstance(raw_slot, dict):
            raise RemainingTurnContractError(f"coverage slot {index} is malformed")
        kind, identifier = raw_slot.get("kind"), raw_slot.get("id")
        if type(kind) is not str or type(identifier) is not str:
            raise RemainingTurnContractError(f"coverage slot {index} is malformed")
        slots.append((kind, identifier))
    expected_gaps = (
        "C:card-trigger:e_trigger-none-remaining_turn-1",
        "C:effect:ProduceExamEffectType_ExamLessonDependBlockConsumptionSum",
        "C:move-effect-runtime:e_effect-exam_block-0005",
    )
    legacy_shape = classification == "C" and tuple(gaps) == expected_gaps
    current_shape = classification == "B" and not gaps
    if not legacy_shape and not current_shape:
        raise RemainingTurnContractError("target coverage blocker list changed")
    if tuple(slots) != MASTER_SLOT_ORDER:
        raise RemainingTurnContractError("target coverage slot order changed")
    return CoverageReadback(
        source=str(path),
        classification=classification,
        gaps=tuple(gaps),
        slots=tuple(slots),
    )


def _trigger_from_db_row(row: sqlite3.Row) -> RemainingTurnTriggerRow:
    trigger_id = str(row["id"])
    trigger = RemainingTurnTriggerRow(
        trigger_id=trigger_id,
        phase_types=_string_tuple(row["phase_types_json"], f"{trigger_id}.phaseTypes"),
        phase_values=_int_tuple(row["phase_values_json"], f"{trigger_id}.phaseValues"),
        field_status_check_types=_string_tuple(
            row["field_status_check_types_json"],
            f"{trigger_id}.fieldStatusCheckTypes",
        ),
        field_status_types=_string_tuple(
            row["field_status_types_json"], f"{trigger_id}.fieldStatusTypes"
        ),
        field_status_values=_int_tuple(
            row["field_status_values_json"], f"{trigger_id}.fieldStatusValues"
        ),
        field_status_card_search_ids=_string_tuple(
            row["field_status_produce_card_search_ids_json"],
            f"{trigger_id}.fieldStatusProduceCardSearchIds",
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_strict_int(row["upper_search_count"], "upperSearchCount", signed=False),
        lower_search_count=_strict_int(row["lower_search_count"], "lowerSearchCount", signed=False),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=_string_tuple(row["effect_types_json"], f"{trigger_id}.effectTypes"),
        lesson_type=str(row["lesson_type"]),
    )
    raw = _json_object(row["raw_json"], trigger_id)
    _strict_raw_fields(
        raw,
        {
            "id": TARGET_TRIGGER_ID,
            "phaseTypes": [TRIGGER_PHASE_NONE],
            "phaseValues": [],
            "fieldStatusCheckTypes": [],
            "fieldStatusTypes": [FIELD_REMAINING_TURN],
            "fieldStatusValues": [TARGET_THRESHOLD],
            "fieldStatusProduceCardSearchIds": [],
            "produceCardSearchId": "",
            "upperSearchCount": 0,
            "lowerSearchCount": 0,
            "cardMovePositionType": UNKNOWN_MOVE,
            "effectTypes": [],
            "lessonType": UNKNOWN_LESSON,
        },
        trigger_id,
    )
    resolution = resolve_exact_remaining_turn_trigger(trigger)
    if not resolution.supported:
        raise RemainingTurnContractError(
            f"target trigger is outside exact shape: {resolution.reasons}"
        )
    return trigger


def _strict_card_slot(raw: object, index: int) -> CardEffectSlot:
    if not isinstance(raw, dict):
        raise RemainingTurnContractError(f"card playEffects[{index}] is not an object")
    expected_keys = {
        "produceExamTriggerId",
        "produceExamEffectId",
        "hideIcon",
        "isOncePlayEffect",
    }
    if set(raw) != expected_keys:
        raise RemainingTurnContractError(f"card playEffects[{index}] shape changed")
    if raw.get("produceExamTriggerId") != "":
        raise RemainingTurnContractError("direct play-effect trigger must be empty")
    if raw.get("produceExamEffectId") != DIRECT_EFFECT_ID:
        raise RemainingTurnContractError("direct play-effect id changed")
    if raw.get("hideIcon") is not False or raw.get("isOncePlayEffect") is not False:
        raise RemainingTurnContractError("direct play-effect flags changed")
    return CardEffectSlot(
        slot_index=index,
        effect_id=DIRECT_EFFECT_ID,
        trigger_id="",
        hide_icon=False,
        is_once_play_effect=False,
    )


def _load_card_version(connection: sqlite3.Connection) -> RemainingTurnCardVersion:
    row = connection.execute(
        """
        SELECT id, upgrade_count, name, plan_type, category, stamina,
               cost_type, cost_value, play_trigger_id, move_position_type,
               play_effects_json, raw_json
          FROM card
         WHERE id = ? AND upgrade_count = ?
        """,
        TARGET_CARD_REF,
    ).fetchone()
    if row is None:
        raise RemainingTurnContractError(f"missing target card: {TARGET_CARD_ID}#0")
    raw = _json_object(row["raw_json"], f"{TARGET_CARD_ID}#0")
    _strict_raw_fields(
        raw,
        {
            "id": TARGET_CARD_ID,
            "upgradeCount": TARGET_CARD_UPGRADE,
            "planType": PLAN2,
            "category": ACTIVE_SKILL,
            "stamina": 1,
            "forceStamina": 0,
            "costType": COST_TYPE_UNKNOWN,
            "costValue": 0,
            "playProduceExamTriggerId": TARGET_TRIGGER_ID,
            "playMovePositionType": CARD_DESTINATION_LOST,
            "moveEffectTriggerType": MOVE_EFFECT_TRIGGER_HAND,
            "moveProduceExamEffectIds": [MOVE_EFFECT_ID],
            "isEndTurnLost": False,
        },
        f"{TARGET_CARD_ID}#0",
    )
    raw_effects = _json_array(row["play_effects_json"], "card.playEffects")
    if len(raw_effects) != 1:
        raise RemainingTurnContractError("target card must have one direct effect slot")
    slots = (_strict_card_slot(raw_effects[0], 0),)

    scalar_expectations = (
        ("id", str(row["id"]), TARGET_CARD_ID),
        ("upgrade_count", row["upgrade_count"], TARGET_CARD_UPGRADE),
        ("plan_type", row["plan_type"], PLAN2),
        ("category", row["category"], ACTIVE_SKILL),
        ("stamina", row["stamina"], 1),
        ("cost_type", row["cost_type"], COST_TYPE_UNKNOWN),
        ("cost_value", row["cost_value"], 0),
        ("play_trigger_id", row["play_trigger_id"], TARGET_TRIGGER_ID),
        ("move_position_type", row["move_position_type"], CARD_DESTINATION_LOST),
    )
    for label, actual, expected in scalar_expectations:
        _strict_equal(actual, expected, f"card.{label}")

    effect_rows = connection.execute(
        """
        SELECT id, effect_type, value1, value2, effect_count, effect_turn
          FROM effect
         WHERE id IN (?, ?)
        """,
        (DIRECT_EFFECT_ID, MOVE_EFFECT_ID),
    ).fetchall()
    effects = {str(effect["id"]): effect for effect in effect_rows}
    if set(effects) != {DIRECT_EFFECT_ID, MOVE_EFFECT_ID}:
        raise RemainingTurnContractError("target downstream effects are incomplete")
    direct = effects[DIRECT_EFFECT_ID]
    move = effects[MOVE_EFFECT_ID]
    for label, effect, expected in (
        (
            "direct effect",
            direct,
            (DIRECT_EFFECT_TYPE, 4000, 0, 1, 0),
        ),
        ("move effect", move, (MOVE_EFFECT_TYPE, 5, 0, 0, 0)),
    ):
        actual = (
            effect["effect_type"],
            effect["value1"],
            effect["value2"],
            effect["effect_count"],
            effect["effect_turn"],
        )
        _strict_equal(actual, expected, label)

    return RemainingTurnCardVersion(
        card_id=str(row["id"]),
        upgrade_count=_strict_int(row["upgrade_count"], "card.upgrade_count", signed=False),
        name=str(row["name"]),
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina=_strict_int(row["stamina"], "card.stamina", signed=False),
        force_stamina=0,
        cost_type=str(row["cost_type"]),
        cost_value=_strict_int(row["cost_value"], "card.cost_value", signed=False),
        play_trigger_id=str(row["play_trigger_id"]),
        play_move_position_type=str(row["move_position_type"]),
        move_effect_trigger_type=MOVE_EFFECT_TRIGGER_HAND,
        play_effect_slots=slots,
        move_effect_ids=(MOVE_EFFECT_ID,),
        is_end_turn_lost=False,
        direct_effect_type=DIRECT_EFFECT_TYPE,
        move_effect_type=MOVE_EFFECT_TYPE,
    )


@dataclass(frozen=True, slots=True)
class RemainingTurnCatalog:
    database: str
    trigger: RemainingTurnTriggerRow
    card: RemainingTurnCardVersion
    coverage: CoverageReadback

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "trigger": self.trigger.to_dict(),
            "card": self.card.to_dict(),
            "coverage": self.coverage.to_dict(),
        }


def load_plan2_card_trigger_remaining_turn1_catalog(
    database: Path | str = DEFAULT_DATABASE,
    coverage_artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
) -> RemainingTurnCatalog:
    """Load the one card and trigger from read-only Master plus readback JSON."""

    coverage = load_coverage_readback(coverage_artifact)
    path = Path(database)
    if not path.is_file():
        raise RemainingTurnContractError(f"Master database not found: {path}")
    try:
        connection_uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(connection_uri, uri=True)
    except sqlite3.Error as error:
        raise RemainingTurnContractError(
            f"cannot open Master read-only: {path}"
        ) from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        if trigger_row is None:
            raise RemainingTurnContractError(f"missing target trigger: {TARGET_TRIGGER_ID}")
        trigger = _trigger_from_db_row(trigger_row)
        card = _load_card_version(connection)
    if card.play_trigger_id != trigger.trigger_id:
        raise RemainingTurnContractError("card/trigger linkage changed")
    if coverage.slots != MASTER_SLOT_ORDER:
        raise RemainingTurnContractError("coverage/card slot order is inconsistent")
    return RemainingTurnCatalog(
        database=str(path),
        trigger=trigger,
        card=card,
        coverage=coverage,
    )


load_catalog = load_plan2_card_trigger_remaining_turn1_catalog


@dataclass(frozen=True, slots=True)
class RemainingTurnAdmissionInput:
    """Read-only Playing/pre-payment snapshot supplied by a caller."""

    remaining_turn: int
    phase: str = RUNTIME_CARD_PLAY_PHASE
    trigger_phase: str = TRIGGER_PHASE_NONE
    boundary: str = "playing-pre-payment"
    card_origin: AdmissionOrigin = "normal"
    payment_state: str = "not-applied"
    direct_effect_state: str = "not-applied"
    current_turn: int = 0
    limit_turn: int = 0
    extra_turn: int = 0
    post_payment_remaining_turn: int | None = None
    post_direct_effect_remaining_turn: int | None = None

    def __post_init__(self) -> None:
        _strict_int(self.remaining_turn, "remaining_turn")
        for label in ("current_turn", "limit_turn", "extra_turn"):
            _strict_int(getattr(self, label), label)
        for label in (
            "post_payment_remaining_turn",
            "post_direct_effect_remaining_turn",
        ):
            value = getattr(self, label)
            if value is not None:
                _strict_int(value, label)
        for label in ("phase", "trigger_phase", "boundary", "payment_state", "direct_effect_state"):
            value = getattr(self, label)
            if type(value) is not str or not value:
                raise TypeError(f"{label} must be a non-empty string")
        if self.card_origin not in CARD_ORIGINS:
            raise ValueError("card_origin must be normal, forced, or extra-play")


@dataclass(frozen=True, slots=True)
class RemainingTurnGateEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    remaining_turn: int | None
    threshold: int | None
    comparison: str | None
    phase: str
    trigger_phase: str
    boundary: str
    card_origin: str
    extra_turn: int
    snapshot_rule: str
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "remainingTurn": self.remaining_turn,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "phase": self.phase,
            "triggerPhase": self.trigger_phase,
            "boundary": self.boundary,
            "cardOrigin": self.card_origin,
            "extraTurn": self.extra_turn,
            "valueSource": "ExamParameterModel.get_RemainTurn (signed Int32)",
            "snapshotRule": self.snapshot_rule,
            "reasons": list(self.reasons),
        }


SNAPSHOT_RULE: Final = (
    "one signed current RemainTurn read while Playing/card admission before "
    "payment and direct effects; post-payment/post-direct values are not reread"
)


def _trigger_id(trigger: object) -> str:
    if isinstance(trigger, RemainingTurnTriggerRow):
        return trigger.trigger_id
    return "<unknown-trigger>"


def _unsupported_evaluation(
    inputs: RemainingTurnAdmissionInput,
    trigger: object,
    reasons: Sequence[str],
    *,
    remaining_turn: int | None = None,
    threshold: int | None = None,
    comparison: str | None = None,
) -> RemainingTurnGateEvaluation:
    return RemainingTurnGateEvaluation(
        trigger_id=_trigger_id(trigger),
        supported=False,
        fires=None,
        remaining_turn=remaining_turn,
        threshold=threshold,
        comparison=comparison,
        phase=inputs.phase,
        trigger_phase=inputs.trigger_phase,
        boundary=inputs.boundary,
        card_origin=inputs.card_origin,
        extra_turn=inputs.extra_turn,
        snapshot_rule=SNAPSHOT_RULE,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_card_play_remaining_turn1_trigger(
    inputs: RemainingTurnAdmissionInput,
    trigger: object = EXACT_TARGET_TRIGGER,
) -> RemainingTurnGateEvaluation:
    """Evaluate signed ``RemainTurn <= 1`` at the card-admission boundary."""

    if not isinstance(inputs, RemainingTurnAdmissionInput):
        raise TypeError("inputs must be RemainingTurnAdmissionInput")
    resolution = resolve_exact_remaining_turn_trigger(trigger)
    if not resolution.supported:
        return _unsupported_evaluation(inputs, trigger, resolution.reasons)
    reasons: list[str] = []
    if inputs.phase != RUNTIME_CARD_PLAY_PHASE:
        reasons.append("runtime-phase-is-not-exam-card-play")
    if inputs.trigger_phase != TRIGGER_PHASE_NONE:
        reasons.append("master-trigger-phase-is-not-none")
    if inputs.boundary != "playing-pre-payment":
        reasons.append("snapshot-boundary-is-not-playing-pre-payment")
    if inputs.payment_state != "not-applied":
        reasons.append("payment-is-already-applied")
    if inputs.direct_effect_state != "not-applied":
        reasons.append("direct-effects-are-already-applied")
    if inputs.card_origin not in CARD_ORIGINS:
        reasons.append("card-origin-is-unknown")
    if reasons:
        return _unsupported_evaluation(
            inputs,
            trigger,
            reasons,
            remaining_turn=inputs.remaining_turn,
            threshold=TARGET_THRESHOLD,
            comparison="signed_less_equal",
        )
    return RemainingTurnGateEvaluation(
        trigger_id=TARGET_TRIGGER_ID,
        supported=True,
        fires=inputs.remaining_turn <= TARGET_THRESHOLD,
        remaining_turn=inputs.remaining_turn,
        threshold=TARGET_THRESHOLD,
        comparison="signed_less_equal",
        phase=inputs.phase,
        trigger_phase=inputs.trigger_phase,
        boundary=inputs.boundary,
        card_origin=inputs.card_origin,
        extra_turn=inputs.extra_turn,
        snapshot_rule=SNAPSHOT_RULE,
    )


evaluate_card_trigger = evaluate_card_play_remaining_turn1_trigger
evaluate_card_play_gate = evaluate_card_play_remaining_turn1_trigger


@dataclass(frozen=True, slots=True)
class CardAdmissionHandoff:
    """A pure admission result; all downstream booleans describe authorization only."""

    card_ref: tuple[str, int]
    evaluation: RemainingTurnGateEvaluation
    downstream_handoff: bool
    payment_authorized: bool
    direct_effects_authorized: bool
    counts_history_authorized: bool
    move_authorized: bool
    payment_performed: bool = False
    direct_effects_performed: bool = False
    counts_updated: bool = False
    history_updated: bool = False
    moved_to_lost: bool = False
    destination: str | None = None
    preserved_downstream_order: tuple[str, ...] = ()
    blockers: tuple[str, ...] = BLOCKERS

    @property
    def accepted(self) -> bool:
        return self.downstream_handoff

    @property
    def gate_failed(self) -> bool:
        return not self.evaluation.supported or self.evaluation.fires is not True

    @property
    def no_mutation(self) -> bool:
        return not any(
            (
                self.payment_performed,
                self.direct_effects_performed,
                self.counts_updated,
                self.history_updated,
                self.moved_to_lost,
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_ref[0],
            "upgrade": self.card_ref[1],
            "accepted": self.accepted,
            "gate": self.evaluation.to_dict(),
            "downstreamHandoff": self.downstream_handoff,
            "authorization": {
                "payment": self.payment_authorized,
                "directEffects": self.direct_effects_authorized,
                "countsAndHistory": self.counts_history_authorized,
                "move": self.move_authorized,
            },
            "performedHere": {
                "payment": self.payment_performed,
                "directEffects": self.direct_effects_performed,
                "countsUpdated": self.counts_updated,
                "historyUpdated": self.history_updated,
                "movedToLost": self.moved_to_lost,
            },
            "destination": self.destination,
            "preservedDownstreamOrder": list(self.preserved_downstream_order),
            "blockers": list(self.blockers),
        }


def admit_plan2_card_remaining_turn1(
    inputs: RemainingTurnAdmissionInput,
    trigger: object = EXACT_TARGET_TRIGGER,
    card: RemainingTurnCardVersion = EXACT_TARGET_CARD,
) -> CardAdmissionHandoff:
    """Return admission authorization without executing any downstream stage."""

    if not isinstance(card, RemainingTurnCardVersion):
        raise TypeError("card must be RemainingTurnCardVersion")
    if card.ref != TARGET_CARD_REF:
        evaluation = _unsupported_evaluation(
            inputs,
            trigger,
            ("card-ref-is-not-exact-target",),
        )
    elif card.play_trigger_id != TARGET_TRIGGER_ID:
        evaluation = _unsupported_evaluation(
            inputs,
            trigger,
            ("card-play-trigger-linkage-is-unknown",),
        )
    else:
        evaluation = evaluate_card_play_remaining_turn1_trigger(inputs, trigger)
    accepted = evaluation.supported and evaluation.fires is True
    return CardAdmissionHandoff(
        card_ref=card.ref,
        evaluation=evaluation,
        downstream_handoff=accepted,
        payment_authorized=accepted,
        direct_effects_authorized=accepted,
        counts_history_authorized=accepted,
        move_authorized=accepted,
        destination=CARD_DESTINATION_LOST if accepted else None,
        preserved_downstream_order=DOWNSTREAM_ORDER if accepted else (),
    )


card_admission_handoff = admit_plan2_card_remaining_turn1


ANDROID_PC_NATIVE_EVIDENCE: Final = {
    "android_v3_2_3": {
        "field_enum": {
            "source": "_research/android/game-v3.2.3/cpp2il-plugin/DiffableCs/Assembly-CSharp/Campus/Common/Proto/Client/Enums/ProduceExamFieldStatusType.cs",
            "enum": "ProduceExamFieldStatusType.RemainingTurn",
            "value": 41,
        },
        "phase_enum": {
            "source": "_research/android/game-v3.2.3/cpp2il-plugin/DiffableCs/Assembly-CSharp/Campus/Common/Proto/Client/Enums/ProduceExamPhaseType.cs",
            "runtime_card_play": {"name": "ExamCardPlay", "value": 2},
            "master_none_literal": {"name": "None", "value": 999},
        },
        "field_predicate": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
            "symbol": "ExamExtensions.IsFieldStatusTriggerStatusEffect",
            "rva": "0x68082D4",
            "value_source": "ExamParameterModel.get_RemainTurn",
            "comparison": "CMP W0, W21; B.LE 0x6808E60",
            "comparison_va": "0x6808D24 / 0x6808D28",
            "meaning": "signed current RemainTurn <= trigger fieldStatusValues[0]",
        },
        "card_admission": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
            "symbol": "ExamExtensions.IsPlayable",
            "calls": ["ExamCardData.get_PlayTrigger", "ExamExtensions.IsEffectTriggerFieldValid"],
            "boundary": "card-play admission while Playing, before payment/direct effects",
        },
        "turn_storage": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence_NestedType__ExamLoopTaskAsync_d__94.txt",
            "symbol": "ExamSequence.<ExamLoopTaskAsync>d__94.MoveNext",
            "isil_instruction_numbers": [682, 683, 686],
            "sequence": ["get_RemainTurn", "subtract 1", "SetRemainTurn"],
            "meaning": "turn-boundary decrement/store is later than the current trigger snapshot",
        },
        "card_command": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence.txt",
            "symbol": "ExamSequence.ExecuteCardCommandImpl",
            "tail": ["PlayCardCount", "UserCardAfterCheck", "MovePlayCardCommand"],
            "meaning": "successful admission hands off to downstream formula/move/count/history work",
        },
    },
    "pc_metadata": {
        "source": "_research/il2cpp/6270BD53FF9E0843B4CFF8565DC326A3760BE8DFD1EB43FE02F62A39DDB5CEB4/exam-runtime-models.json",
        "type": "Campus.InGame.Exam.ExamParameterModel",
        "signed_int32_members": [
            "<CurrentTurn>k__BackingField",
            "<RemainTurn>k__BackingField",
            "<LimitTurn>k__BackingField",
            "<ExtraTurn>k__BackingField",
        ],
        "properties": [
            "get_Phase",
            "get_CurrentTurn",
            "get_RemainTurn",
            "get_LimitTurn",
            "get_ExtraTurn",
        ],
        "fact": "PC metadata names separate signed Int32 fields/properties; Android v3.2.3 native body supplies predicate/timing semantics",
    },
}


def build_plan2_card_trigger_remaining_turn1_audit(
    database: Path | str = DEFAULT_DATABASE,
    coverage_artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
    *,
    focused_test_count: int = 0,
) -> dict[str, object]:
    """Build the committed audit payload from read-only local evidence."""

    if type(focused_test_count) is not int or focused_test_count < 0:
        raise TypeError("focused_test_count must be a non-negative integer")
    catalog = load_plan2_card_trigger_remaining_turn1_catalog(
        database, coverage_artifact
    )
    version_ref = f"{TARGET_CARD_ID}#{TARGET_CARD_UPGRADE}"
    direct_refs = [version_ref] if catalog.coverage.direct else []
    co_blocked_refs = [version_ref] if catalog.coverage.co_blocked else []
    return {
        "schema_version": 1,
        "audit_id": "plan2_card_trigger_remaining_turn1_native_audit",
        "scope": {
            "plan": "Plan2/Common",
            "card": f"{TARGET_CARD_ID}#{TARGET_CARD_UPGRADE}",
            "display_name": TARGET_CARD_NAME,
            "trigger": TARGET_TRIGGER_ID,
            "leaf": "card admission trigger/handoff only",
            "excluded": [
                "ExamLessonDependBlockConsumptionSum formula",
                "move-effect-runtime e_effect-exam_block-0005",
                "Plan2 core runtime",
                "Plan3",
                "GUI/controller",
            ],
        },
        "validation": {
            "master_access": "read_only",
            "central_coverage": "read_only_reference; not rebuilt or modified",
            "full_suite": "not_performed",
            "security": "not_performed",
            "hash": "not_performed",
            "clock_or_time": "not_performed",
            "runtime_agent_calls": "not_performed",
        },
        "accounting": {
            "affected": catalog.coverage.affected,
            "direct": catalog.coverage.direct,
            "co_blocked": catalog.coverage.co_blocked,
            "affected_refs": [version_ref],
            "direct_refs": direct_refs,
            "co_blocked_refs": co_blocked_refs,
            "remaining_blockers": list(catalog.coverage.gaps),
        },
        "master_snapshot": {
            "database": str(database),
            "trigger": catalog.trigger.to_dict(),
            "card": catalog.card.to_dict(),
            "formal_slot_order": [
                {"kind": kind, "id": identifier}
                for kind, identifier in MASTER_SLOT_ORDER
            ],
            "downstream_order": list(DOWNSTREAM_ORDER),
        },
        "coverage_readback": catalog.coverage.to_dict(),
        "native_sources": ANDROID_PC_NATIVE_EVIDENCE,
        "exact_semantics": {
            "value_source": "signed current ExamParameterModel.RemainTurn; not CurrentTurn, LimitTurn, or ExtraTurn",
            "threshold": TARGET_THRESHOLD,
            "comparison": "signed_less_equal",
            "expression": "signed_remaining_turn <= 1",
            "equality_at_one": True,
            "phase": "Master ProduceExamPhaseType_None is literal; runtime card admission is ExamCardPlay/Playing",
            "extra_turn": "separate signed Int32 field; captured as context but not added to or substituted for RemainTurn",
            "snapshot": SNAPSHOT_RULE,
            "origins": {
                origin: "same admission predicate and same pre-payment snapshot"
                for origin in CARD_ORIGINS
            },
            "boundary_cases": [
                {"remainingTurn": 0, "fires": True},
                {"remainingTurn": 1, "fires": True},
                {"remainingTurn": 2, "fires": False},
                {"remainingTurn": -1, "fires": True},
                {"remainingTurn": INT32_MIN, "fires": True},
                {"remainingTurn": INT32_MAX, "fires": False},
            ],
            "unknown_or_altered_shape": "fail closed; no Not inversion, search, effect, lesson, phase, boundary, payment, or direct-effect drift is guessed",
        },
        "admission_handoff": {
            "gate_failure": {
                "payment": "not performed",
                "direct_effects": "not performed",
                "counts": "not updated",
                "history": "not updated",
                "movement": "not performed",
            },
            "success": {
                "handoff_only": True,
                "authorizes": [
                    "payment:stamina-1",
                    f"direct formula:{DIRECT_EFFECT_ID}",
                    "counts/history",
                    f"move:{CARD_DESTINATION_LOST}",
                    f"move-effect-runtime:{MOVE_EFFECT_ID}",
                ],
                "preserves_order": list(DOWNSTREAM_ORDER),
                "this_leaf_performs": [],
            },
        },
        "blockers": list(BLOCKERS),
        "scope_guard": {
            "new_runtime": "src/gkms_tool/plan2_card_trigger_remaining_turn1.py",
            "new_tests": ["tests/test_plan2_card_trigger_remaining_turn1.py"],
            "new_audit": "var/coverage/plan2_card_trigger_remaining_turn1_native_audit.json",
            "central_coverage_modified": False,
            "plan2_core_runtime_modified": False,
            "native_search_modified": False,
            "formal_coverage_modified": False,
            "plan3_modified": False,
            "gui_or_controller_modified": False,
        },
        "focused_verification": {
            "command": ".venv\\Scripts\\python.exe -m pytest -q tests/test_plan2_card_trigger_remaining_turn1.py",
            "test_count": focused_test_count,
            "scope": "focused file only",
        },
    }


make_native_audit = build_plan2_card_trigger_remaining_turn1_audit
