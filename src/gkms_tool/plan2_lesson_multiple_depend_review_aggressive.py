"""Bounded Plan2 leaf for ``ExamLessonValueMultipleDependReviewOrAggressive``.

This module owns one exact Master effect and the four ``p_card-02-ido-3_140``
versions only.  It does not import the Plan2 core, read a coverage artifact,
or execute the other three card slots.  The status installation is the small
native boundary visible in Android v3.2.3 metadata; lesson score arithmetic is
delegated to the already audited NIA/native formula primitives.

The target executor receives ``effectTurn`` and installs a boolean status.
Master ``effectValue1`` is zero and is not a multiplier input.  The review and
aggressive-dependent factor is read later by ``CalculateAddingParameter`` from
the current signed Review/Aggressive getters supplied by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final, TypeAlias

from . import nia_lesson_value_multiple as _nia
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    INT32_MAX,
    INT32_MIN,
    ParameterApplicationStatus,
    calculate_adding_parameter,
)


EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive"
)
PLAN2: Final = "ProducePlanType_Plan2"
TARGET_CARD_ID: Final = "p_card-02-ido-3_140"
TARGET_CARD_LABEL: Final = "私は、決して"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_EFFECT_ID: Final = (
    "e_effect-exam_lesson_value_multiple_depend_review_or_aggressive-05"
)
TARGET_EFFECT_IDS: Final = (TARGET_EFFECT_ID,)
TARGET_EFFECT_BY_UPGRADE: Final = {
    upgrade: TARGET_EFFECT_ID for upgrade in TARGET_UPGRADES
}
TARGET_TRIGGER_ID: Final = "e_trigger-none-card_play_aggressive_up-9"
TARGET_GATE_THRESHOLD: Final = 9
TARGET_SLOT_INDEX: Final = 0

EFFECT_PLAYABLE_VALUE_ADD: Final = "e_effect-exam_playable_value_add-01"
EFFECT_CARD_DRAW: Final = "e_effect-exam_card_draw-0001"
REVIEW_EFFECT_BY_UPGRADE: Final = {
    0: "e_effect-exam_review-0008",
    1: "e_effect-exam_review-0009",
    2: "e_effect-exam_review-0011",
    3: "e_effect-exam_review-0013",
}
REVIEW_VALUE_BY_UPGRADE: Final = {0: 8, 1: 9, 2: 11, 3: 13}
TARGET_EFFECT_IDS_BY_UPGRADE: Final = {
    upgrade: (
        TARGET_EFFECT_ID,
        EFFECT_PLAYABLE_VALUE_ADD,
        REVIEW_EFFECT_BY_UPGRADE[upgrade],
        EFFECT_CARD_DRAW,
    )
    for upgrade in TARGET_UPGRADES
}

PLAYING_CARD_ADMISSION_BOUNDARY: Final = "Playing/card-admission-before-payment"


class Plan2LessonMultipleDependReviewAggressiveContractError(ValueError):
    """A Master row or runtime input is outside this certified leaf."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressivePause:
    """Typed fail-closed result for an unresolved effect reference."""

    code: str
    detail: str = ""
    effect_id: str = ""


def _plain_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-integer", label
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "integer-out-of-range", label
        )
    return value


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-text", label
        )
    return value


def _row_value(
    row: Mapping[str, object] | sqlite3.Row, *names: str
) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
    else:
        available = set(row.keys())
        for name in names:
            if name in available:
                return row[name]
    raise Plan2LessonMultipleDependReviewAggressiveContractError(
        "missing-master-field", names[0]
    )


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-json", label
        ) from error
    if not isinstance(parsed, dict):
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-json-object", label
        )
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-json", label
        ) from error
    if not isinstance(parsed, list):
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-json-array", label
        )
    return parsed


_RAW_EFFECT_KEYS: Final = frozenset(
    {
        "id",
        "effectType",
        "effectValue1",
        "effectValue2",
        "effectCount",
        "effectTurn",
        "targetProduceCardId",
        "targetUpgradeCount",
        "targetExamEffectType",
        "produceCardSearchId",
        "movePositionType",
        "pickRangeType",
        "pickCountReferenceProduceCardSearchId",
        "pickCountType",
        "pickCountMin",
        "pickCountMax",
        "produceCardSearchId2",
        "pickRangeType2",
        "pickCountReferenceProduceCardSearchId2",
        "pickCountType2",
        "pickCountMin2",
        "pickCountMax2",
        "chainProduceExamEffectId",
        "chainProduceExamEffectIds",
        "produceExamStatusEnchantId",
        "produceCardStatusEnchantId",
        "produceCardGrowEffectIds",
        "effectGroupIds",
        "produceDescriptions",
        "customizeProduceDescriptions",
    }
)


@dataclass(frozen=True, slots=True)
class _ExpectedEffectRow:
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]


_EXPECTED_EFFECT_ROWS: Final[dict[str, _ExpectedEffectRow]] = {
    TARGET_EFFECT_ID: _ExpectedEffectRow(EFFECT_TYPE, 0, 0, 0, 5, ()),
    EFFECT_PLAYABLE_VALUE_ADD: _ExpectedEffectRow(
        "ProduceExamEffectType_ExamPlayableValueAdd",
        0,
        0,
        1,
        0,
        ("effect_group-visible-exam_playable_value_add-000",),
    ),
    "e_effect-exam_review-0008": _ExpectedEffectRow(
        "ProduceExamEffectType_ExamReview", 8, 0, 0, 0, ("effect_group-visible-exam_review-000",)
    ),
    "e_effect-exam_review-0009": _ExpectedEffectRow(
        "ProduceExamEffectType_ExamReview", 9, 0, 0, 0, ("effect_group-visible-exam_review-000",)
    ),
    "e_effect-exam_review-0011": _ExpectedEffectRow(
        "ProduceExamEffectType_ExamReview", 11, 0, 0, 0, ("effect_group-visible-exam_review-000",)
    ),
    "e_effect-exam_review-0013": _ExpectedEffectRow(
        "ProduceExamEffectType_ExamReview", 13, 0, 0, 0, ("effect_group-visible-exam_review-000",)
    ),
    EFFECT_CARD_DRAW: _ExpectedEffectRow(
        "ProduceExamEffectType_ExamCardDraw",
        1,
        0,
        0,
        0,
        ("effect_group-visible-exam_card_draw-000",),
    ),
}


def _validate_raw_effect(
    raw: Mapping[str, object],
    *,
    effect_id: str,
    expected: _ExpectedEffectRow,
) -> None:
    if frozenset(raw) != _RAW_EFFECT_KEYS:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "effect-raw-shape", effect_id
        )
    exact: dict[str, object] = {
        "id": effect_id,
        "effectType": expected.effect_type,
        "effectValue1": expected.value1,
        "effectValue2": expected.value2,
        "effectCount": expected.count,
        "effectTurn": expected.turn,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": "",
        "movePositionType": "ProduceCardMovePositionType_Unknown",
        "pickRangeType": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": "ProducePickCountType_Unknown",
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": list(expected.effect_group_ids),
    }
    for key, wanted in exact.items():
        if type(raw[key]) is not type(wanted) or raw[key] != wanted:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "effect-raw-value", f"{effect_id}:{key}"
            )
    for key in ("produceDescriptions", "customizeProduceDescriptions"):
        value = raw[key]
        if type(value) is not list or any(type(item) is not dict for item in value):
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "effect-description-shape", f"{effect_id}:{key}"
            )


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressiveEffect:
    """The exact executable fields of the target effect row."""

    effect_id: str
    effect_value1: int
    effect_value2: int
    effect_count: int
    effect_turn: int
    effect_type: str = EFFECT_TYPE
    status_enchant_id: str = ""
    chain_effect_id: str = ""

    def __post_init__(self) -> None:
        expected = _EXPECTED_EFFECT_ROWS.get(self.effect_id)
        if expected is None or expected.effect_type != EFFECT_TYPE:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "unproven-effect-row", self.effect_id
            )
        fields = (
            (self.effect_type, expected.effect_type, "effect_type"),
            (self.effect_value1, expected.value1, "effect_value1"),
            (self.effect_value2, expected.value2, "effect_value2"),
            (self.effect_count, expected.count, "effect_count"),
            (self.effect_turn, expected.turn, "effect_turn"),
        )
        for actual, wanted, label in fields:
            if type(actual) is not type(wanted) or actual != wanted:
                raise Plan2LessonMultipleDependReviewAggressiveContractError(
                    "effect-shape", f"{self.effect_id}:{label}"
                )
        if self.status_enchant_id or self.chain_effect_id:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "effect-links", self.effect_id
            )

    @property
    def value1(self) -> int:
        return self.effect_value1

    @property
    def value2(self) -> int:
        return self.effect_value2

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressiveMasterEffect:
    """One exact effect row used by the card inventory validator."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]


def _parse_exact_effect_row(
    row: Mapping[str, object] | sqlite3.Row,
    *,
    expected_id: str | None = None,
) -> Plan2LessonMultipleDependReviewAggressiveMasterEffect:
    effect_id = _required_text(_row_value(row, "id", "effect_id"), "effect_id")
    if expected_id is not None and effect_id != expected_id:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "effect-id-mismatch", f"{effect_id}:{expected_id}"
        )
    expected = _EXPECTED_EFFECT_ROWS.get(effect_id)
    if expected is None:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "unproven-effect-row", effect_id
        )
    values = (
        _row_value(row, "effect_type", "effectType"),
        _row_value(row, "value1", "effectValue1"),
        _row_value(row, "value2", "effectValue2"),
        _row_value(row, "effect_count", "effectCount"),
        _row_value(row, "effect_turn", "effectTurn"),
    )
    effect_type = _required_text(values[0], f"{effect_id}:effect_type")
    numeric = tuple(
        _plain_int(value, f"{effect_id}:field") for value in values[1:]
    )
    if (effect_type, *numeric) != (
        expected.effect_type,
        expected.value1,
        expected.value2,
        expected.count,
        expected.turn,
    ):
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "effect-value-shape", effect_id
        )
    status = _row_value(row, "status_enchant_id", "produceExamStatusEnchantId")
    chain = _row_value(row, "chain_effect_id", "chainProduceExamEffectId")
    if status != "" or chain != "":
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "effect-links", effect_id
        )
    raw_value = _row_value(row, "raw_json")
    raw = _json_object(raw_value, f"{effect_id}:raw_json")
    _validate_raw_effect(raw, effect_id=effect_id, expected=expected)
    return Plan2LessonMultipleDependReviewAggressiveMasterEffect(
        effect_id,
        effect_type,
        numeric[0],
        numeric[1],
        numeric[2],
        numeric[3],
        expected.effect_group_ids,
    )


def _target_effect_from_row(
    row: Mapping[str, object] | sqlite3.Row,
) -> Plan2LessonMultipleDependReviewAggressiveEffect:
    parsed = _parse_exact_effect_row(row, expected_id=TARGET_EFFECT_ID)
    return Plan2LessonMultipleDependReviewAggressiveEffect(
        effect_id=parsed.effect_id,
        effect_value1=parsed.value1,
        effect_value2=parsed.value2,
        effect_count=parsed.count,
        effect_turn=parsed.turn,
        effect_type=parsed.effect_type,
    )


def _read_effect_rows(
    connection: sqlite3.Connection, effect_ids: Sequence[str]
) -> dict[str, sqlite3.Row]:
    ids = tuple(effect_ids)
    if not ids or any(type(item) is not str or not item for item in ids):
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-effect-list"
        )
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT * FROM effect WHERE id IN ({placeholders})", ids
    ).fetchall()
    result = {str(row["id"]): row for row in rows}
    if set(result) != set(ids) or len(rows) != len(ids):
        missing = sorted(set(ids) - set(result))
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "effect-catalog", ",".join(missing)
        )
    return result


def load_plan2_lesson_multiple_depend_review_aggressive_effect(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2LessonMultipleDependReviewAggressiveEffect:
    """Load only the exact ``-05`` target effect from read-only Master."""

    if type(effect_id) is not str or not effect_id:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "invalid-effect-id"
        )
    if effect_id != TARGET_EFFECT_ID:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "unproven-effect-row", effect_id
        )
    path = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "master-effect-read-failed", str(error)
        ) from error
    if row is None:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "effect-not-found", effect_id
        )
    return _target_effect_from_row(row)


def parse_plan2_lesson_multiple_depend_review_aggressive_master_row(
    row: Mapping[str, object] | sqlite3.Row,
) -> Plan2LessonMultipleDependReviewAggressiveEffect:
    """Validate a normalized/raw target Master row without writing it."""

    if not isinstance(row, (Mapping, sqlite3.Row)):
        raise TypeError("row must be a mapping or sqlite3.Row")
    return _target_effect_from_row(row)


def try_resolve_plan2_lesson_multiple_depend_review_aggressive(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2LessonMultipleDependReviewAggressivePause | Plan2LessonMultipleDependReviewAggressiveEffect:
    """Return a typed pause instead of allowing an unknown shape through."""

    try:
        if isinstance(effect, str):
            return load_plan2_lesson_multiple_depend_review_aggressive_effect(
                effect, database=database
            )
        return parse_plan2_lesson_multiple_depend_review_aggressive_master_row(
            effect
        )
    except Plan2LessonMultipleDependReviewAggressiveContractError as error:
        effect_id = effect if isinstance(effect, str) else ""
        return Plan2LessonMultipleDependReviewAggressivePause(
            error.code, error.detail, effect_id
        )


load_plan2_lesson_multiple_depend_review_aggressive_effects = (
    lambda *, database=DEFAULT_DATABASE: (
        load_plan2_lesson_multiple_depend_review_aggressive_effect(
            TARGET_EFFECT_ID, database=database
        ),
    )
)


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressiveCardEffectSlot:
    index: int
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False
    effect_group_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressiveCardVersion:
    card_id: str
    upgrade: int
    plan_type: str
    play_trigger_id: str
    effect_slots: tuple[Plan2LessonMultipleDependReviewAggressiveCardEffectSlot, ...]

    @property
    def effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def target_effect_id(self) -> str:
        return self.effect_slots[TARGET_SLOT_INDEX].effect_id

    @property
    def target_slot_index(self) -> int:
        return TARGET_SLOT_INDEX

    @property
    def review_effect_id(self) -> str:
        return self.effect_slots[2].effect_id

    @property
    def review_value(self) -> int:
        return self.effect_slots[2].value1


def _parse_card_slot(
    raw: object,
    *,
    card_id: str,
    upgrade: int,
    index: int,
    expected_effect_id: str,
    effects: Mapping[str, Plan2LessonMultipleDependReviewAggressiveMasterEffect],
) -> Plan2LessonMultipleDependReviewAggressiveCardEffectSlot:
    if not isinstance(raw, Mapping):
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "card-slot-shape", f"{card_id}:{upgrade}:{index}"
        )
    if set(raw) != {
        "produceExamTriggerId",
        "produceExamEffectId",
        "hideIcon",
        "isOncePlayEffect",
    }:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "card-slot-shape", f"{card_id}:{upgrade}:{index}"
        )
    if raw["produceExamTriggerId"] != "":
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "card-slot-trigger", f"{card_id}:{upgrade}:{index}"
        )
    if raw["produceExamEffectId"] != expected_effect_id:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "card-slot-order", f"{card_id}:{upgrade}:{index}"
        )
    if type(raw["hideIcon"]) is not bool or raw["hideIcon"] is not False:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "card-slot-hide-icon", f"{card_id}:{upgrade}:{index}"
        )
    if type(raw["isOncePlayEffect"]) is not bool or raw["isOncePlayEffect"] is not False:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "card-slot-once-shape", f"{card_id}:{upgrade}:{index}"
        )
    effect = effects[expected_effect_id]
    return Plan2LessonMultipleDependReviewAggressiveCardEffectSlot(
        index=index,
        effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        count=effect.count,
        turn=effect.turn,
        effect_group_ids=effect.effect_group_ids,
    )


def load_plan2_lesson_multiple_depend_review_aggressive_card_versions(
    *,
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan2LessonMultipleDependReviewAggressiveCardVersion, ...]:
    """Read exactly four target card rows and preserve their stored slot order."""

    path = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, upgrade_count, plan_type, play_trigger_id,
                       play_effects_json
                  FROM card
                 WHERE id = ? AND upgrade_count BETWEEN 0 AND 3
                 ORDER BY upgrade_count
                """,
                (TARGET_CARD_ID,),
            ).fetchall()
            actual = tuple((str(row["id"]), int(row["upgrade_count"])) for row in rows)
            expected_refs = tuple((TARGET_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES)
            if actual != expected_refs:
                raise Plan2LessonMultipleDependReviewAggressiveContractError(
                    "card-catalog", repr(actual)
                )
            all_effect_ids = tuple(
                dict.fromkeys(
                    effect_id
                    for upgrade in TARGET_UPGRADES
                    for effect_id in TARGET_EFFECT_IDS_BY_UPGRADE[upgrade]
                )
            )
            raw_effect_rows = _read_effect_rows(connection, all_effect_ids)
            effects = {
                effect_id: _parse_exact_effect_row(
                    raw_effect_rows[effect_id], expected_id=effect_id
                )
                for effect_id in all_effect_ids
            }
            versions: list[Plan2LessonMultipleDependReviewAggressiveCardVersion] = []
            for row in rows:
                upgrade = _plain_int(row["upgrade_count"], "card.upgrade_count")
                if upgrade not in TARGET_UPGRADES:
                    raise Plan2LessonMultipleDependReviewAggressiveContractError(
                        "card-upgrade", str(upgrade)
                    )
                if row["plan_type"] != PLAN2:
                    raise Plan2LessonMultipleDependReviewAggressiveContractError(
                        "card-plan", f"{TARGET_CARD_ID}:{upgrade}"
                    )
                if row["play_trigger_id"] != TARGET_TRIGGER_ID:
                    raise Plan2LessonMultipleDependReviewAggressiveContractError(
                        "card-gate", f"{TARGET_CARD_ID}:{upgrade}"
                    )
                payload = _json_array(
                    row["play_effects_json"],
                    f"{TARGET_CARD_ID}:{upgrade}:play_effects_json",
                )
                expected_ids = TARGET_EFFECT_IDS_BY_UPGRADE[upgrade]
                if len(payload) != len(expected_ids):
                    raise Plan2LessonMultipleDependReviewAggressiveContractError(
                        "card-slot-count", f"{TARGET_CARD_ID}:{upgrade}"
                    )
                slots = tuple(
                    _parse_card_slot(
                        slot,
                        card_id=TARGET_CARD_ID,
                        upgrade=upgrade,
                        index=index,
                        expected_effect_id=expected_id,
                        effects=effects,
                    )
                    for index, (slot, expected_id) in enumerate(
                        zip(payload, expected_ids)
                    )
                )
                versions.append(
                    Plan2LessonMultipleDependReviewAggressiveCardVersion(
                        card_id=TARGET_CARD_ID,
                        upgrade=upgrade,
                        plan_type=str(row["plan_type"]),
                        play_trigger_id=str(row["play_trigger_id"]),
                        effect_slots=slots,
                    )
                )
    except sqlite3.Error as error:
        raise Plan2LessonMultipleDependReviewAggressiveContractError(
            "master-card-read-failed", str(error)
        ) from error
    return tuple(versions)


load_plan2_target_card_versions = (
    load_plan2_lesson_multiple_depend_review_aggressive_card_versions
)


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressiveCoverage:
    affected_versions: tuple[Plan2LessonMultipleDependReviewAggressiveCardVersion, ...]
    direct_versions: tuple[Plan2LessonMultipleDependReviewAggressiveCardVersion, ...]
    co_blocked_versions: tuple[Plan2LessonMultipleDependReviewAggressiveCardVersion, ...]

    @property
    def affected_count(self) -> int:
        return len(self.affected_versions)

    @property
    def direct_count(self) -> int:
        return len(self.direct_versions)

    @property
    def co_blocked_count(self) -> int:
        return len(self.co_blocked_versions)


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressiveCatalog:
    database: str
    card_versions: tuple[Plan2LessonMultipleDependReviewAggressiveCardVersion, ...]
    coverage: Plan2LessonMultipleDependReviewAggressiveCoverage

    @property
    def affected_count(self) -> int:
        return self.coverage.affected_count

    @property
    def direct_count(self) -> int:
        return self.coverage.direct_count

    @property
    def co_blocked_count(self) -> int:
        return self.coverage.co_blocked_count


def load_plan2_lesson_multiple_depend_review_aggressive_catalog(
    *, database: Path = DEFAULT_DATABASE
) -> Plan2LessonMultipleDependReviewAggressiveCatalog:
    versions = load_plan2_lesson_multiple_depend_review_aggressive_card_versions(
        database=database
    )
    coverage = Plan2LessonMultipleDependReviewAggressiveCoverage(
        affected_versions=versions,
        direct_versions=(),
        co_blocked_versions=versions,
    )
    return Plan2LessonMultipleDependReviewAggressiveCatalog(
        database=str(Path(database).resolve()),
        card_versions=versions,
        coverage=coverage,
    )


load_catalog = load_plan2_lesson_multiple_depend_review_aggressive_catalog


@dataclass(frozen=True, slots=True)
class Plan2CardGateSnapshot:
    """The signed gate value captured before payment/direct dispatch."""

    aggressive: int
    evaluation_boundary: str = PLAYING_CARD_ADMISSION_BOUNDARY
    payment_committed: bool = False
    direct_effects_dispatched: bool = False

    def __post_init__(self) -> None:
        _plain_int(self.aggressive, "gate.aggressive")
        if self.evaluation_boundary != PLAYING_CARD_ADMISSION_BOUNDARY:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "gate-boundary", self.evaluation_boundary
            )
        if type(self.payment_committed) is not bool or type(self.direct_effects_dispatched) is not bool:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "gate-state"
            )
        if self.payment_committed or self.direct_effects_dispatched:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "gate-snapshot-too-late"
            )

    @property
    def admitted(self) -> bool:
        return self.aggressive >= TARGET_GATE_THRESHOLD

    @property
    def result_expression(self) -> str:
        return f"signed_aggressive_value >= {TARGET_GATE_THRESHOLD}"


def snapshot_plan2_card_gate(
    aggressive: int,
    *,
    evaluation_boundary: str = PLAYING_CARD_ADMISSION_BOUNDARY,
) -> Plan2CardGateSnapshot:
    return Plan2CardGateSnapshot(
        aggressive=aggressive,
        evaluation_boundary=evaluation_boundary,
    )


@dataclass(frozen=True, slots=True)
class LessonParameterMultipleDependReviewOrAggressiveStatus:
    """One native boolean status created with ``effectTurn`` only."""

    turn: int
    passing_turn_start: bool = False
    is_turn_limited: bool | None = None

    def __post_init__(self) -> None:
        _plain_int(self.turn, "status.turn")
        if type(self.passing_turn_start) is not bool:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "status-passing-flag"
            )
        if self.is_turn_limited is None:
            object.__setattr__(self, "is_turn_limited", self.turn >= 0)
        elif type(self.is_turn_limited) is not bool:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "status-limited-flag"
            )


@dataclass(frozen=True, slots=True)
class LessonParameterMultipleDependReviewOrAggressiveState:
    """Ordered native status list projection for the boolean getter."""

    statuses: tuple[LessonParameterMultipleDependReviewOrAggressiveStatus, ...] = ()

    def __post_init__(self) -> None:
        if type(self.statuses) is not tuple or any(
            not isinstance(item, LessonParameterMultipleDependReviewOrAggressiveStatus)
            for item in self.statuses
        ):
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "status-list-shape"
            )

    @property
    def is_active(self) -> bool:
        return any(
            (not status.is_turn_limited) or status.turn > 0
            for status in self.statuses
        )

    def get_turn(self, *, is_use: bool = False) -> int:
        """Return the last active status turn, matching ordered-list reads."""

        if type(is_use) is not bool:
            raise TypeError("is_use must be bool")
        for status in reversed(self.statuses):
            if (not status.is_turn_limited) or status.turn > 0:
                return status.turn
        return 0

    def mark_passing_turn_start(
        self,
    ) -> "LessonParameterMultipleDependReviewOrAggressiveState":
        return LessonParameterMultipleDependReviewOrAggressiveState(
            tuple(replace(status, passing_turn_start=True) for status in self.statuses)
        )

    def spend_turn_start(
        self,
    ) -> "LessonParameterMultipleDependReviewOrAggressiveState":
        survivors: list[LessonParameterMultipleDependReviewOrAggressiveStatus] = []
        for status in self.statuses:
            current = status
            if status.is_turn_limited and status.passing_turn_start:
                wrapped = (status.turn - 1) & 0xFFFFFFFF
                if wrapped >= 0x80000000:
                    wrapped -= 0x100000000
                current = replace(status, turn=wrapped)
            if current.is_turn_limited and current.turn <= 0:
                continue
            survivors.append(current)
        return LessonParameterMultipleDependReviewOrAggressiveState(
            tuple(survivors)
        )


Plan2LessonMultipleDependReviewAggressiveState: TypeAlias = (
    LessonParameterMultipleDependReviewOrAggressiveState
)
Plan2LessonMultipleDependReviewAggressiveStatus: TypeAlias = (
    LessonParameterMultipleDependReviewOrAggressiveStatus
)


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewOrAggressiveExecution:
    before: LessonParameterMultipleDependReviewOrAggressiveState
    after: LessonParameterMultipleDependReviewOrAggressiveState
    effect: Plan2LessonMultipleDependReviewAggressiveEffect
    installed: bool
    created_index: int | None
    active_before: bool
    active_after: bool
    trace: tuple[str, ...]


def install_plan2_lesson_multiple_depend_review_or_aggressive(
    state: LessonParameterMultipleDependReviewOrAggressiveState,
    *,
    turn: int,
    block_add_status: bool = False,
) -> Plan2LessonMultipleDependReviewOrAggressiveExecution:
    """Project the native TryAdd call; no value merge is invented."""

    if not isinstance(state, LessonParameterMultipleDependReviewOrAggressiveState):
        raise TypeError("state must be the dependent-status state")
    turn = _plain_int(turn, "effect.turn")
    if type(block_add_status) is not bool:
        raise TypeError("block_add_status must be bool")
    before_active = state.is_active
    if block_add_status:
        return Plan2LessonMultipleDependReviewOrAggressiveExecution(
            state,
            state,
            Plan2LessonMultipleDependReviewAggressiveEffect(
                TARGET_EFFECT_ID, 0, 0, 0, turn
            ),
            False,
            None,
            before_active,
            before_active,
            ("read-effect-turn", "try-add-status-blocked"),
        )
    statuses = state.statuses + (
        LessonParameterMultipleDependReviewOrAggressiveStatus(turn),
    )
    after = LessonParameterMultipleDependReviewOrAggressiveState(statuses)
    return Plan2LessonMultipleDependReviewOrAggressiveExecution(
        state,
        after,
        Plan2LessonMultipleDependReviewAggressiveEffect(
            TARGET_EFFECT_ID, 0, 0, 0, turn
        ),
        True,
        len(state.statuses),
        before_active,
        after.is_active,
        ("read-effect-turn", "try-add-status", "read-dependent-flag-after"),
    )


def execute_plan2_lesson_multiple_depend_review_or_aggressive(
    state: LessonParameterMultipleDependReviewOrAggressiveState,
    effect: Plan2LessonMultipleDependReviewAggressiveEffect,
    *,
    block_add_status: bool = False,
) -> Plan2LessonMultipleDependReviewOrAggressiveExecution:
    if not isinstance(effect, Plan2LessonMultipleDependReviewAggressiveEffect):
        raise TypeError("effect must be the exact target effect")
    return install_plan2_lesson_multiple_depend_review_or_aggressive(
        state, turn=effect.turn, block_add_status=block_add_status
    )


install_effect = execute_plan2_lesson_multiple_depend_review_or_aggressive


@dataclass(frozen=True, slots=True)
class Plan2ReviewAggressiveSnapshot:
    """Signed current getter values consumed by CalculateAddingParameter."""

    review: int
    aggressive: int

    def __post_init__(self) -> None:
        _plain_int(self.review, "review")
        _plain_int(self.aggressive, "aggressive")

    @property
    def minimum(self) -> int:
        return min(self.review, self.aggressive)


def bind_plan2_review_aggressive_status(
    status: AddingParameterStatus,
    *,
    dependent_state: LessonParameterMultipleDependReviewOrAggressiveState,
    snapshot: Plan2ReviewAggressiveSnapshot,
) -> AddingParameterStatus:
    """Bind the boolean status and one signed getter snapshot for one hit."""

    if not isinstance(status, AddingParameterStatus):
        raise TypeError("status must be AddingParameterStatus")
    if not isinstance(dependent_state, LessonParameterMultipleDependReviewOrAggressiveState):
        raise TypeError("dependent_state has unsupported shape")
    if not isinstance(snapshot, Plan2ReviewAggressiveSnapshot):
        raise TypeError("snapshot must be Plan2ReviewAggressiveSnapshot")
    return replace(
        status,
        lesson_parameter_multiple_depend_review_or_aggressive=(
            dependent_state.is_active
        ),
        review=snapshot.review,
        aggressive=snapshot.aggressive,
    )


def calculate_plan2_lesson_parameter(
    value: int,
    *,
    dependent_state: LessonParameterMultipleDependReviewOrAggressiveState,
    review_aggressive: Plan2ReviewAggressiveSnapshot,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    additional: AddingParameterAdditionalData | None = None,
    is_buff_active: bool = True,
) -> int:
    """Use the shared exact CalculateAddingParameter primitive once."""

    bound = bind_plan2_review_aggressive_status(
        adding_status,
        dependent_state=dependent_state,
        snapshot=review_aggressive,
    )
    return calculate_adding_parameter(
        value,
        is_buff_active=is_buff_active,
        status=bound,
        settings=settings,
        additional=additional,
    )


Plan2LessonHitMutation: TypeAlias = _nia.LessonHitMutation


def apply_plan2_lesson_multiple_depend_review_or_aggressive_hit(
    value: int,
    *,
    multiplier_state: _nia.LessonParameterMultipleState,
    dependent_state: LessonParameterMultipleDependReviewOrAggressiveState,
    review_aggressive: Plan2ReviewAggressiveSnapshot,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    application_status: ParameterApplicationStatus,
    is_buff_active: bool = True,
    additional: AddingParameterAdditionalData | None = None,
) -> Plan2LessonHitMutation:
    """Run one Calculate/AddParameter pair with the dependent flag bound."""

    bound = bind_plan2_review_aggressive_status(
        adding_status,
        dependent_state=dependent_state,
        snapshot=review_aggressive,
    )
    return _nia.apply_lesson_hit(
        value,
        multiplier_state=multiplier_state,
        adding_status=bound,
        settings=settings,
        application_status=application_status,
        is_buff_active=is_buff_active,
        additional=additional,
    )


def apply_plan2_lesson_multiple_depend_review_or_aggressive_hits(
    value: int,
    count: int,
    *,
    multiplier_state: _nia.LessonParameterMultipleState,
    dependent_state: LessonParameterMultipleDependReviewOrAggressiveState,
    review_aggressive: Plan2ReviewAggressiveSnapshot,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    application_status: ParameterApplicationStatus,
    is_buff_active: bool = True,
    additional: AddingParameterAdditionalData | None = None,
) -> tuple[Plan2LessonHitMutation, ...]:
    """Repeat the native calculate-then-add pair once per lesson hit."""

    bound = bind_plan2_review_aggressive_status(
        adding_status,
        dependent_state=dependent_state,
        snapshot=review_aggressive,
    )
    return _nia.apply_lesson_hits(
        value,
        count,
        multiplier_state=multiplier_state,
        adding_status=bound,
        settings=settings,
        application_status=application_status,
        is_buff_active=is_buff_active,
        additional=additional,
    )


@dataclass(frozen=True, slots=True)
class Plan2LessonMultipleDependReviewAggressiveSlot:
    index: int
    kind: str
    effect: Plan2LessonMultipleDependReviewAggressiveEffect | None = None
    lesson_value: int | None = None
    lesson_count: int = 1


def evaluate_plan2_lesson_multiple_depend_review_or_aggressive_slots(
    slots: Sequence[Plan2LessonMultipleDependReviewAggressiveSlot],
    *,
    dependent_state: LessonParameterMultipleDependReviewOrAggressiveState | None = None,
    multiplier_state: _nia.LessonParameterMultipleState | None = None,
    adding_status: AddingParameterStatus | None = None,
    settings: AddingParameterSettings | None = None,
    review_aggressive: Plan2ReviewAggressiveSnapshot | None = None,
    application_status: ParameterApplicationStatus | None = None,
) -> tuple[
    LessonParameterMultipleDependReviewOrAggressiveState,
    ParameterApplicationStatus,
    tuple[Plan2LessonHitMutation, ...],
]:
    """Evaluate only INSTALL/LESSON probes; unknown slots fail closed."""

    if not isinstance(slots, Sequence):
        raise TypeError("slots must be a sequence")
    dependent = (
        LessonParameterMultipleDependReviewOrAggressiveState()
        if dependent_state is None
        else dependent_state
    )
    multiple = _nia.LessonParameterMultipleState() if multiplier_state is None else multiplier_state
    status = AddingParameterStatus() if adding_status is None else adding_status
    config = AddingParameterSettings() if settings is None else settings
    application = (
        ParameterApplicationStatus(judge_parameter=0)
        if application_status is None
        else application_status
    )
    if review_aggressive is None:
        snapshot = Plan2ReviewAggressiveSnapshot(review=status.review, aggressive=status.aggressive)
    else:
        snapshot = review_aggressive
    if not isinstance(dependent, LessonParameterMultipleDependReviewOrAggressiveState):
        raise TypeError("dependent_state has unsupported shape")
    if not isinstance(multiple, _nia.LessonParameterMultipleState):
        raise TypeError("multiplier_state has unsupported shape")
    if not isinstance(status, AddingParameterStatus) or not isinstance(config, AddingParameterSettings):
        raise TypeError("formula inputs have unsupported shape")
    if not isinstance(application, ParameterApplicationStatus):
        raise TypeError("application_status has unsupported shape")
    hits: list[Plan2LessonHitMutation] = []
    for slot in slots:
        if not isinstance(slot, Plan2LessonMultipleDependReviewAggressiveSlot):
            raise TypeError("slots contain an unsupported value")
        if slot.kind == "install":
            if slot.effect is None:
                raise Plan2LessonMultipleDependReviewAggressiveContractError(
                    "install-effect-required", str(slot.index)
                )
            dependent = execute_plan2_lesson_multiple_depend_review_or_aggressive(
                dependent, slot.effect
            ).after
        elif slot.kind == "lesson":
            if slot.lesson_value is None:
                raise Plan2LessonMultipleDependReviewAggressiveContractError(
                    "lesson-value-required", str(slot.index)
                )
            current = apply_plan2_lesson_multiple_depend_review_or_aggressive_hits(
                slot.lesson_value,
                slot.lesson_count,
                multiplier_state=multiple,
                dependent_state=dependent,
                review_aggressive=snapshot,
                adding_status=status,
                settings=config,
                application_status=application,
            )
            hits.extend(current)
            if current:
                application = current[-1].next_application_status
        elif slot.kind in {"other", "block"}:
            continue
        else:
            raise Plan2LessonMultipleDependReviewAggressiveContractError(
                "unknown-slot-kind", slot.kind
            )
    return dependent, application, tuple(hits)


def build_plan2_lesson_multiple_depend_review_aggressive_audit(
    *, database: Path = DEFAULT_DATABASE
) -> dict[str, object]:
    """Build the standalone audit payload from the local Master rows."""

    effect = load_plan2_lesson_multiple_depend_review_aggressive_effect(
        TARGET_EFFECT_ID, database=database
    )
    catalog = load_plan2_lesson_multiple_depend_review_aggressive_catalog(
        database=database
    )
    versions = [
        {
            "cardId": version.card_id,
            "upgrade": version.upgrade,
            "planType": version.plan_type,
            "playTriggerId": version.play_trigger_id,
            "effectSlots": [
                {
                    "slot": slot.index,
                    "effectId": slot.effect_id,
                    "effectType": slot.effect_type,
                    "value1": slot.value1,
                    "value2": slot.value2,
                    "count": slot.count,
                    "turn": slot.turn,
                    "triggerId": slot.trigger_id,
                    "hideIcon": slot.hide_icon,
                    "isOncePlayEffect": slot.is_once_play_effect,
                }
                for slot in version.effect_slots
            ],
        }
        for version in catalog.card_versions
    ]
    return {
        "schema_version": 1,
        "scope": "Plan2 standalone target effect only",
        "target": {
            "cardId": TARGET_CARD_ID,
            "label": TARGET_CARD_LABEL,
            "upgrades": list(TARGET_UPGRADES),
            "effectType": EFFECT_TYPE,
            "effectId": effect.effect_id,
        },
        "master": {
            "effectValue1": effect.value1,
            "effectValue2": effect.value2,
            "effectCount": effect.count,
            "effectTurn": effect.turn,
            "orderedVersions": versions,
        },
        "native": {
            "android": "Android v3.2.3",
            "pcMetadataVersion": 31,
            "executor": "Campus.InGame.Exam.LessonDependReviewOrAggressiveEffectExecutor.ExecuteEffect",
            "executorCtorVa": "0x7E83BEC",
            "executorToken": "0x0600489A",
            "tryAdd": "ExamStatusEffectCollection.TryAddLessonParameterMultipleDependReviewOrAggressiveStatus(turn, context)",
            "statusCtor": "LessonParameterMultipleDependReviewOrAggressiveStatusEffect.ctor(turn)",
            "getter": "ExamStatusEffectCollection.IsLessonParameterMultipleDependReviewOrAggressive",
            "pcMetadata": "_research/il2cpp/<pc-build>/targeted-metadata-index.json",
        },
        "formula": {
            "selection": "signed minimum(Review, Aggressive); not sum, max, or branch",
            "multiplierUnit": "settings permille / float32(1000.0f)",
            "dependFactor": "min(f32(f32(settings.lesson_depend_review_aggressive_permille) / 1000.0f * f32(minimum)), f32(settings.lesson_depend_review_aggressive_max_permille) / 1000.0f)",
            "roundingOrder": [
                "integer minimum before float conversion",
                "float32 after each division and multiplication",
                "existing lesson/parameter factors and dependent factor are added/multiplied in CalculateAddingParameter order",
                "ceil_f32(lesson_buff_multiple * lesson_buff) before base subtraction/floor",
                "ceil_f32(raw + float32(-0.0001f)) once at final score calculation",
                "no truncation step in this path",
            ],
            "applicationOrder": "each hit: CalculateAddingParameter, then AddParameter; never aggregate count first",
            "zeroSignedOverflow": "signed Int32 Review/Aggressive; zero gives zero dependent factor; negative minimum is preserved; finite inputs outside Int32 or non-finite float32 fail closed; native base floors at zero and final score caps at INT32_MAX",
            "clearPerfectCaps": "ordinary per-hit AddParameter limit/clear/perfect transitions; target status adds no separate cap",
        },
        "transactionOrder": {
            "gate": f"{TARGET_TRIGGER_ID}: signed current Aggressive >= {TARGET_GATE_THRESHOLD}, captured at {PLAYING_CARD_ADMISSION_BOUNDARY}",
            "payment": "after gate snapshot; payment is outside this leaf",
            "directEffects": "after payment; target status is ordered slot 0 but dispatch is outside this leaf",
            "sameCardRead": "do not reread post-payment or same-card effect mutations for the gate snapshot",
        },
        "coverage": {
            "affected": catalog.affected_count,
            "direct": catalog.direct_count,
            "coBlocked": catalog.co_blocked_count,
            "blocker": TARGET_TRIGGER_ID,
            "centralCoverageIntegrated": False,
        },
        "scopeGuards": {
            "centralCoreModified": False,
            "formalCoverageModified": False,
            "plan3Modified": False,
            "guiModified": False,
            "unknownStateEffectCardShape": "raise Plan2LessonMultipleDependReviewAggressiveContractError",
        },
    }


__all__ = [
    "EFFECT_CARD_DRAW",
    "EFFECT_PLAYABLE_VALUE_ADD",
    "EFFECT_TYPE",
    "PLAN2",
    "PLAYING_CARD_ADMISSION_BOUNDARY",
    "REVIEW_EFFECT_BY_UPGRADE",
    "REVIEW_VALUE_BY_UPGRADE",
    "TARGET_CARD_ID",
    "TARGET_CARD_LABEL",
    "TARGET_EFFECT_ID",
    "TARGET_EFFECT_IDS",
    "TARGET_EFFECT_BY_UPGRADE",
    "TARGET_EFFECT_IDS_BY_UPGRADE",
    "TARGET_GATE_THRESHOLD",
    "TARGET_SLOT_INDEX",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "LessonParameterMultipleDependReviewOrAggressiveState",
    "LessonParameterMultipleDependReviewOrAggressiveStatus",
    "Plan2CardGateSnapshot",
    "Plan2LessonHitMutation",
    "Plan2LessonMultipleDependReviewAggressiveCardEffectSlot",
    "Plan2LessonMultipleDependReviewAggressiveCardVersion",
    "Plan2LessonMultipleDependReviewAggressiveCatalog",
    "Plan2LessonMultipleDependReviewAggressiveContractError",
    "Plan2LessonMultipleDependReviewAggressiveCoverage",
    "Plan2LessonMultipleDependReviewAggressiveEffect",
    "Plan2LessonMultipleDependReviewAggressiveMasterEffect",
    "Plan2LessonMultipleDependReviewAggressivePause",
    "Plan2LessonMultipleDependReviewAggressiveSlot",
    "Plan2LessonMultipleDependReviewAggressiveState",
    "Plan2LessonMultipleDependReviewAggressiveStatus",
    "Plan2LessonMultipleDependReviewOrAggressiveExecution",
    "Plan2ReviewAggressiveSnapshot",
    "apply_plan2_lesson_multiple_depend_review_or_aggressive_hit",
    "apply_plan2_lesson_multiple_depend_review_or_aggressive_hits",
    "bind_plan2_review_aggressive_status",
    "build_plan2_lesson_multiple_depend_review_aggressive_audit",
    "calculate_plan2_lesson_parameter",
    "execute_plan2_lesson_multiple_depend_review_or_aggressive",
    "install_effect",
    "install_plan2_lesson_multiple_depend_review_or_aggressive",
    "load_catalog",
    "load_plan2_lesson_multiple_depend_review_aggressive_card_versions",
    "load_plan2_lesson_multiple_depend_review_aggressive_effect",
    "load_plan2_lesson_multiple_depend_review_aggressive_effects",
    "load_plan2_lesson_multiple_depend_review_aggressive_catalog",
    "load_plan2_target_card_versions",
    "parse_plan2_lesson_multiple_depend_review_aggressive_master_row",
    "snapshot_plan2_card_gate",
    "evaluate_plan2_lesson_multiple_depend_review_or_aggressive_slots",
    "try_resolve_plan2_lesson_multiple_depend_review_aggressive",
]
