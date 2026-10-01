"""Bounded Plan2 leaf for the three ``心が跳ねるままに`` gates.

The module intentionally owns only the target card's three direct effect
triggers.  The field predicate is delegated to the already audited 500
adapter, which in turn exposes the Android v3.2.3 binary32 implementation.
The card catalog, direct-effect build boundary, timer ordering, and formal
overlap accounting are checked locally and fail closed on an unknown shape.

Important native boundary:

* ``ExamSequence.ExecuteCardCommandImpl`` builds all card-owned PlayEffect
  commands and evaluates each non-empty ``PlayEffectTrigger`` before ordinary
  card payment is committed.
* The card-owned command is created through the one-argument
  ``CreatePlayEffectCommand(effect)`` path.  The queued command therefore has
  no original trigger to re-run when the effect executes.
* Each gate is still a separate predicate invocation and re-reads stamina and
  max stamina from the same pre-payment build context.  Earlier queued card
  effects cannot mutate that context before the later gates are tested.

No central runtime, native-search, Plan3 source, GUI/controller, formal
coverage, or JSON artifact is written by this leaf.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from . import plan2_stamina_up500_block_fix as _stamina500
from .master_db import DEFAULT_DATABASE


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_FORMAL_COVERAGE_PATH: Final = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_card_executable_coverage.json"
)

# Re-export the audited predicate and its native evidence.  Keeping the
# identity is deliberate: this module must not grow a second stamina formula.
Comparison = _stamina500.Comparison
FIELD_STAMINA_UP_MULTIPLE = _stamina500.FIELD_STAMINA_UP_MULTIPLE
NATIVE_BRANCHES = _stamina500.NATIVE_BRANCHES
NATIVE_FORMULA_EVIDENCE = _stamina500.NATIVE_FORMULA_EVIDENCE
StaminaField = _stamina500.StaminaField
StaminaSnapshot = _stamina500.StaminaSnapshot
StaminaMultipleEvaluation = _stamina500.StaminaMultipleEvaluation
evaluate_stamina_multiple = _stamina500.evaluate_stamina_multiple
Plan2NoneStaminaSnapshot = _stamina500.Plan2NoneStaminaSnapshot
Plan2StaminaSnapshot = _stamina500.Plan2StaminaSnapshot
TriggerSnapshotBoundary = _stamina500.TriggerSnapshotBoundary
PHASE_NONE = _stamina500.PHASE_NONE
NONE_STAMINA_UP_MULTIPLE_500 = _stamina500.NONE_STAMINA_UP_MULTIPLE_500


TARGET_CARD_ID: Final = "p_card-02-ido-3_167"
CARD_ID: Final = TARGET_CARD_ID
TARGET_CARD_NAME: Final = "心が跳ねるままに"
TARGET_CARD_NAMES: Final = tuple(
    f"{TARGET_CARD_NAME}{'+' * upgrade}" for upgrade in range(4)
)
TARGET_UPGRADES: Final = (0, 1, 2, 3)
UPGRADES: Final = TARGET_UPGRADES

PLAN2: Final = "ProducePlanType_Plan2"
ACTIVE_SKILL: Final = "ProduceCardCategory_ActiveSkill"
COST_TYPE: Final = "ExamCostType_ExamCardPlayAggressive"
COST_VALUE: Final = 2
MOVE_GRAVE: Final = "ProduceCardMovePositionType_Grave"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"
TRIGGER_FIELD_TYPE: Final = FIELD_STAMINA_UP_MULTIPLE
DIRECT_DISPATCH_PHASE: Final = "PlayEffect"
DIRECT_BUILD_CALLSITE: Final = (
    "ExamSequence.ExecuteCardCommandImpl:direct-effect-sequence-build"
)
PRE_PAYMENT_SNAPSHOT_PHASE: Final = (
    "ExamSequence.ExecuteCardCommandImpl:pre-payment-card-play"
)

TRIGGER_ID_500: Final = "e_trigger-none-stamina_up_multiple-500"
TRIGGER_ID_800: Final = "e_trigger-none-stamina_up_multiple-800"
TRIGGER_ID_1000: Final = "e_trigger-none-stamina_up_multiple-1000"
TRIGGER_IDS: Final = (TRIGGER_ID_500, TRIGGER_ID_800, TRIGGER_ID_1000)
TARGET_TRIGGER_IDS: Final = TRIGGER_IDS
TRIGGER_THRESHOLD_BY_ID: Final = {
    TRIGGER_ID_500: 500,
    TRIGGER_ID_800: 800,
    TRIGGER_ID_1000: 1000,
}
TARGET_THRESHOLDS: Final = (500, 800, 1000)
TRIGGER_ID_BY_THRESHOLD: Final = {
    threshold: trigger_id
    for trigger_id, threshold in TRIGGER_THRESHOLD_BY_ID.items()
}
FORMAL_TRIGGER_GAPS: Final = tuple(
    f"C:effect-trigger:{trigger_id}" for trigger_id in TRIGGER_IDS
)

REVIEW_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamReview"
TIMER_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamEffectTimer"
CARD_DRAW_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamCardDraw"
LESSON_REVIEW_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
TIMER_EFFECT_ID: Final = (
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0002"
)
TIMER_CHILD_EFFECT_ID: Final = "e_effect-exam_card_draw-0002"

EXPECTED_EFFECT_IDS_BY_UPGRADE: Final = {
    0: (
        "e_effect-exam_review-0001",
        TIMER_EFFECT_ID,
        "e_effect-exam_review-0001",
        "e_effect-exam_review-0002",
        "e_effect-exam_lesson_depend_exam_review-2200-01",
    ),
    1: (
        "e_effect-exam_review-0001",
        TIMER_EFFECT_ID,
        "e_effect-exam_review-0001",
        "e_effect-exam_review-0002",
        "e_effect-exam_lesson_depend_exam_review-3200-01",
    ),
    2: (
        "e_effect-exam_review-0001",
        TIMER_EFFECT_ID,
        "e_effect-exam_review-0001",
        "e_effect-exam_review-0002",
        "e_effect-exam_lesson_depend_exam_review-3600-01",
    ),
    3: (
        "e_effect-exam_review-0001",
        TIMER_EFFECT_ID,
        "e_effect-exam_review-0001",
        "e_effect-exam_review-0002",
        "e_effect-exam_lesson_depend_exam_review-4200-01",
    ),
}
TARGET_EFFECT_IDS: Final = tuple(
    dict.fromkeys(
        [
            *(
                effect_id
                for effect_ids in EXPECTED_EFFECT_IDS_BY_UPGRADE.values()
                for effect_id in effect_ids
            ),
            TIMER_CHILD_EFFECT_ID,
        ]
    )
)


INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1


class CardOrigin(str, Enum):
    """Native card dispatch origins sharing the direct-effect gate."""

    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"


# Convenient names used by adjacent Plan2 leaves and callers.
CardExecutionOrigin = CardOrigin
CARD_ORIGINS: Final = tuple(item.value for item in CardOrigin)


class Plan2EffectTriggerThresholdError(ValueError):
    """Base error for a malformed local contract."""


class CatalogContractError(Plan2EffectTriggerThresholdError):
    """Master rows are missing or cannot be read read-only."""


class NativeBoundaryError(Plan2EffectTriggerThresholdError):
    """The supplied snapshot or direct-effect boundary is unproven."""


class CoverageContractError(Plan2EffectTriggerThresholdError):
    """The read-only formal coverage slice is not the expected target slice."""


def _json_value(value: object, label: str, issues: list[str]) -> object:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        issues.append(f"invalid-json:{label}")
        return None


def _json_list(value: object, label: str, issues: list[str]) -> tuple[Any, ...]:
    parsed = _json_value(value, label, issues)
    if not isinstance(parsed, list):
        issues.append(f"json-array-required:{label}")
        return ()
    return tuple(parsed)


def _string_tuple(value: object, label: str, issues: list[str]) -> tuple[str, ...]:
    parsed = _json_list(value, label, issues)
    if any(type(item) is not str for item in parsed):
        issues.append(f"string-array-required:{label}")
        return ()
    return tuple(parsed)


def _int_tuple(value: object, label: str, issues: list[str]) -> tuple[int, ...]:
    parsed = _json_list(value, label, issues)
    if any(type(item) is not int for item in parsed):
        issues.append(f"int-array-required:{label}")
        return ()
    return tuple(parsed)


def _text(value: object, label: str, issues: list[str]) -> str:
    if type(value) is not str:
        issues.append(f"string-required:{label}")
        return ""
    return value


def _plain_int(value: object, label: str, issues: list[str]) -> int:
    if type(value) is not int:
        issues.append(f"int-required:{label}")
        return 0
    return value


def _plain_i32(value: object, label: str) -> int:
    if type(value) is not int or not INT32_MIN <= value <= INT32_MAX:
        raise NativeBoundaryError(f"{label}-not-signed-int32")
    return value


def _expected_trigger_contract(trigger_id: str, threshold: int) -> dict[str, Any]:
    return {
        "id": trigger_id,
        "phase_types": [PHASE_NONE],
        "phase_values": [],
        "field_check_types": [],
        "field_types": [TRIGGER_FIELD_TYPE],
        "field_values": [threshold],
        "field_card_search_ids": [],
        "produce_card_search_id": "",
        "upper_search_count": 0,
        "lower_search_count": 0,
        "card_move_position_type": MOVE_UNKNOWN,
        "effect_types": [],
        "lesson_type": LESSON_UNKNOWN,
    }


EXPECTED_TRIGGER_CONTRACTS: Final = {
    trigger_id: _expected_trigger_contract(trigger_id, threshold)
    for trigger_id, threshold in TRIGGER_THRESHOLD_BY_ID.items()
}

_TRIGGER_RAW_KEYS: Final = frozenset(
    {
        "id",
        "phaseTypes",
        "phaseValues",
        "fieldStatusCheckTypes",
        "fieldStatusTypes",
        "fieldStatusValues",
        "fieldStatusProduceCardSearchIds",
        "produceCardSearchId",
        "upperSearchCount",
        "lowerSearchCount",
        "cardMovePositionType",
        "effectTypes",
        "lessonType",
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    }
)


@dataclass(frozen=True, slots=True)
class EffectTriggerContract:
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

    @property
    def threshold(self) -> int | None:
        return self.field_values[0] if len(self.field_values) == 1 else None

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
class Plan2CardEffectSlot:
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectRow:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]
    produce_description_count: int
    customize_description_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect_group_ids", tuple(self.effect_group_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
            "produce_description_count": self.produce_description_count,
            "customize_description_count": self.customize_description_count,
        }


@dataclass(frozen=True, slots=True)
class Plan2CardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    effect_slots: tuple[Plan2CardEffectSlot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "move_effect_ids", tuple(self.move_effect_ids))
        object.__setattr__(self, "effect_slots", tuple(self.effect_slots))

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def ref(self) -> str:
        return f"{self.card_id}#{self.upgrade_count}"

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
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "move_effect_trigger_type": self.move_effect_trigger_type,
            "move_effect_ids": list(self.move_effect_ids),
            "effect_slots": [slot.to_dict() for slot in self.effect_slots],
            "ordered_effect_ids": list(self.ordered_effect_ids),
        }


@dataclass(frozen=True, slots=True)
class Plan2EffectTriggerThresholdCatalog:
    database: str
    trigger_rows_by_id: Mapping[str, EffectTriggerContract]
    card_versions: tuple[Plan2CardVersion, ...]
    effect_rows_by_id: Mapping[str, Plan2ExamEffectRow]
    shape_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_rows_by_id", dict(self.trigger_rows_by_id))
        object.__setattr__(self, "card_versions", tuple(self.card_versions))
        object.__setattr__(self, "effect_rows_by_id", dict(self.effect_rows_by_id))
        object.__setattr__(
            self, "shape_issues", tuple(dict.fromkeys(self.shape_issues))
        )

    @property
    def triggers(self) -> tuple[EffectTriggerContract, ...]:
        return tuple(
            self.trigger_rows_by_id[trigger_id]
            for trigger_id in TRIGGER_IDS
            if trigger_id in self.trigger_rows_by_id
        )

    @property
    def trigger(self) -> EffectTriggerContract | None:
        return self.trigger_rows_by_id.get(TRIGGER_ID_500)

    @property
    def exact_shape_supported(self) -> bool:
        return (
            not self.shape_issues
            and tuple(card.upgrade_count for card in self.card_versions)
            == TARGET_UPGRADES
            and set(self.trigger_rows_by_id) == set(TRIGGER_IDS)
            and set(self.effect_rows_by_id) == set(TARGET_EFFECT_IDS)
            and all(len(card.effect_slots) == 5 for card in self.card_versions)
        )

    @property
    def affected_card_versions(self) -> tuple[Plan2CardVersion, ...]:
        return self.card_versions

    def card(self, card_id: str = TARGET_CARD_ID, upgrade_count: int = 0) -> Plan2CardVersion | None:
        return next(
            (
                card
                for card in self.card_versions
                if card.card_id == card_id and card.upgrade_count == upgrade_count
            ),
            None,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "affected_card_versions": len(self.card_versions),
            "affected_card_refs": [card.ref for card in self.card_versions],
            "exact_shape_supported": self.exact_shape_supported,
            "shape_issues": list(self.shape_issues),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "triggers": [
                self.trigger_rows_by_id[trigger_id].to_dict()
                for trigger_id in TRIGGER_IDS
                if trigger_id in self.trigger_rows_by_id
            ],
            "card_versions": [card.to_dict() for card in self.card_versions],
            "effect_rows": [
                self.effect_rows_by_id[effect_id].to_dict()
                for effect_id in TARGET_EFFECT_IDS
                if effect_id in self.effect_rows_by_id
            ],
            "summary": self.summary(),
        }


# Adjacent naming aliases are intentionally kept stable.
Plan2EffectTriggerThresholdsCatalog = Plan2EffectTriggerThresholdCatalog
Catalog = Plan2EffectTriggerThresholdCatalog


_CARD_SLOT_KEYS: Final = frozenset(
    {"produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"}
)
_EFFECT_RAW_KEYS: Final = frozenset(
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
_CARD_RAW_KEYS: Final = frozenset(
    {
        "assetId",
        "category",
        "costType",
        "costValue",
        "effectGroupIds",
        "evaluation",
        "forceStamina",
        "id",
        "isCharacterAsset",
        "isConversion",
        "isEndTurnLost",
        "isInitial",
        "isInitialDeckProduceCard",
        "isLimited",
        "isRestrict",
        "isReward",
        "libraryHidden",
        "maxCustomizeCount",
        "moveEffectTriggerType",
        "moveProduceExamEffectIds",
        "moveProduceExamTriggerIds",
        "name",
        "noDeckDuplication",
        "order",
        "originCharacterId",
        "originIdolCardId",
        "originPrimaStellaIdolCardId",
        "originSupportCardId",
        "planType",
        "playEffects",
        "playMovePositionType",
        "playProduceExamTriggerId",
        "produceCardCustomizeIds",
        "produceCardStatusEnchantId",
        "produceDescriptions",
        "rarity",
        "rentalUnlockProducerLevel",
        "searchTag",
        "stamina",
        "unlockProducerLevel",
        "upgradeCount",
        "viewStartTime",
        "voiceAssetId",
    }
)


def _effect_spec(effect_id: str) -> dict[str, Any]:
    lesson_values = {
        "e_effect-exam_lesson_depend_exam_review-2200-01": 2200,
        "e_effect-exam_lesson_depend_exam_review-3200-01": 3200,
        "e_effect-exam_lesson_depend_exam_review-3600-01": 3600,
        "e_effect-exam_lesson_depend_exam_review-4200-01": 4200,
    }
    if effect_id == "e_effect-exam_review-0001":
        return {
            "effect_type": REVIEW_EFFECT_TYPE,
            "value1": 1,
            "value2": 0,
            "effect_count": 0,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": "",
            "effect_group_ids": ("effect_group-visible-exam_review-000",),
        }
    if effect_id == "e_effect-exam_review-0002":
        return {
            "effect_type": REVIEW_EFFECT_TYPE,
            "value1": 2,
            "value2": 0,
            "effect_count": 0,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": "",
            "effect_group_ids": ("effect_group-visible-exam_review-000",),
        }
    if effect_id == TIMER_EFFECT_ID:
        return {
            "effect_type": TIMER_EFFECT_TYPE,
            "value1": 1,
            "value2": 0,
            "effect_count": 1,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": TIMER_CHILD_EFFECT_ID,
            "effect_group_ids": (
                "effect_group-visible-exam_effect_timer-000",
                "effect_group-visible-exam_card_draw-000",
            ),
        }
    if effect_id == TIMER_CHILD_EFFECT_ID:
        return {
            "effect_type": CARD_DRAW_EFFECT_TYPE,
            "value1": 2,
            "value2": 0,
            "effect_count": 0,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": "",
            "effect_group_ids": ("effect_group-visible-exam_card_draw-000",),
        }
    if effect_id in lesson_values:
        return {
            "effect_type": LESSON_REVIEW_EFFECT_TYPE,
            "value1": lesson_values[effect_id],
            "value2": 0,
            "effect_count": 1,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": "",
            "effect_group_ids": (
                "effect_group-visible-exam_lesson_depend_exam_review-000",
                "effect_group-visible-exam_lesson-000",
            ),
        }
    raise KeyError(effect_id)


def _parse_trigger(
    row: sqlite3.Row | None, trigger_id: str, issues: list[str]
) -> EffectTriggerContract | None:
    if row is None:
        issues.append(f"missing-target-trigger:{trigger_id}")
        return None
    actual = {
        "id": _text(row["id"], f"{trigger_id}.id", issues),
        "phase_types": list(
            _string_tuple(row["phase_types_json"], f"{trigger_id}.phase_types", issues)
        ),
        "phase_values": list(
            _int_tuple(row["phase_values_json"], f"{trigger_id}.phase_values", issues)
        ),
        "field_check_types": list(
            _string_tuple(
                row["field_status_check_types_json"],
                f"{trigger_id}.field_check_types",
                issues,
            )
        ),
        "field_types": list(
            _string_tuple(
                row["field_status_types_json"],
                f"{trigger_id}.field_types",
                issues,
            )
        ),
        "field_values": list(
            _int_tuple(
                row["field_status_values_json"],
                f"{trigger_id}.field_values",
                issues,
            )
        ),
        "field_card_search_ids": list(
            _string_tuple(
                row["field_status_produce_card_search_ids_json"],
                f"{trigger_id}.field_card_search_ids",
                issues,
            )
        ),
        "produce_card_search_id": _text(
            row["produce_card_search_id"],
            f"{trigger_id}.produce_card_search_id",
            issues,
        ),
        "upper_search_count": _plain_int(
            row["upper_search_count"], f"{trigger_id}.upper_search_count", issues
        ),
        "lower_search_count": _plain_int(
            row["lower_search_count"], f"{trigger_id}.lower_search_count", issues
        ),
        "card_move_position_type": _text(
            row["card_move_position_type"],
            f"{trigger_id}.card_move_position_type",
            issues,
        ),
        "effect_types": list(
            _string_tuple(row["effect_types_json"], f"{trigger_id}.effect_types", issues)
        ),
        "lesson_type": _text(row["lesson_type"], f"{trigger_id}.lesson_type", issues),
    }
    expected = EXPECTED_TRIGGER_CONTRACTS[trigger_id]
    for key, expected_value in expected.items():
        if actual.get(key) != expected_value:
            issues.append(f"trigger-shape-mismatch:{trigger_id}:{key}")

    raw = _json_value(row["raw_json"], f"{trigger_id}.raw_json", issues)
    if not isinstance(raw, Mapping):
        issues.append(f"trigger-raw-object-required:{trigger_id}")
        raw = {}
    if set(raw) != _TRIGGER_RAW_KEYS:
        issues.append(f"trigger-raw-key-shape:{trigger_id}")
    raw_expected = {
        "id": trigger_id,
        "phaseTypes": expected["phase_types"],
        "phaseValues": expected["phase_values"],
        "fieldStatusCheckTypes": expected["field_check_types"],
        "fieldStatusTypes": expected["field_types"],
        "fieldStatusValues": expected["field_values"],
        "fieldStatusProduceCardSearchIds": expected["field_card_search_ids"],
        "produceCardSearchId": expected["produce_card_search_id"],
        "upperSearchCount": expected["upper_search_count"],
        "lowerSearchCount": expected["lower_search_count"],
        "cardMovePositionType": expected["card_move_position_type"],
        "effectTypes": expected["effect_types"],
        "lessonType": expected["lesson_type"],
    }
    for key, expected_value in raw_expected.items():
        if raw.get(key) != expected_value:
            issues.append(f"trigger-raw-mismatch:{trigger_id}:{key}")
    for key in (
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    ):
        if not isinstance(raw.get(key), list) or any(
            not isinstance(item, Mapping) for item in raw.get(key, [])
        ):
            issues.append(f"trigger-description-shape:{trigger_id}:{key}")
    return EffectTriggerContract(
        id=actual["id"],
        phase_types=tuple(actual["phase_types"]),
        phase_values=tuple(actual["phase_values"]),
        field_check_types=tuple(actual["field_check_types"]),
        field_types=tuple(actual["field_types"]),
        field_values=tuple(actual["field_values"]),
        field_card_search_ids=tuple(actual["field_card_search_ids"]),
        produce_card_search_id=actual["produce_card_search_id"],
        upper_search_count=actual["upper_search_count"],
        lower_search_count=actual["lower_search_count"],
        card_move_position_type=actual["card_move_position_type"],
        effect_types=tuple(actual["effect_types"]),
        lesson_type=actual["lesson_type"],
    )


def _parse_card_slots(
    card_id: str, upgrade: int, value: object, issues: list[str]
) -> tuple[Plan2CardEffectSlot, ...]:
    parsed = _json_list(value, f"card:{card_id}#{upgrade}.play_effects", issues)
    expected_ids = EXPECTED_EFFECT_IDS_BY_UPGRADE.get(upgrade, ())
    expected_triggers = (
        "",
        "",
        TRIGGER_ID_500,
        TRIGGER_ID_800,
        TRIGGER_ID_1000,
    )
    if len(parsed) != 5:
        issues.append(f"card-effect-slot-count:{card_id}#{upgrade}")
    slots: list[Plan2CardEffectSlot] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, Mapping):
            issues.append(f"card-effect-slot-not-object:{card_id}#{upgrade}:{index}")
            continue
        if set(item) != _CARD_SLOT_KEYS:
            issues.append(f"card-effect-slot-key-shape:{card_id}#{upgrade}:{index}")
        effect_id = item.get("produceExamEffectId")
        trigger_id = item.get("produceExamTriggerId")
        hide_icon = item.get("hideIcon")
        once = item.get("isOncePlayEffect")
        if index < len(expected_ids) and effect_id != expected_ids[index]:
            issues.append(f"card-effect-id-mismatch:{card_id}#{upgrade}:{index}")
        if index < len(expected_triggers) and trigger_id != expected_triggers[index]:
            issues.append(f"card-effect-trigger-mismatch:{card_id}#{upgrade}:{index}")
        if type(effect_id) is not str or not effect_id:
            issues.append(f"card-effect-id-invalid:{card_id}#{upgrade}:{index}")
            effect_id = ""
        if type(trigger_id) is not str:
            issues.append(f"card-effect-trigger-invalid:{card_id}#{upgrade}:{index}")
            trigger_id = ""
        if hide_icon is not False:
            issues.append(f"card-effect-hide-icon-shape:{card_id}#{upgrade}:{index}")
        if once is not False:
            issues.append(f"card-effect-once-shape:{card_id}#{upgrade}:{index}")
        slots.append(
            Plan2CardEffectSlot(
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                hide_icon=hide_icon if type(hide_icon) is bool else False,
                is_once_play_effect=once if type(once) is bool else False,
            )
        )
    return tuple(slots)


def _parse_card(row: sqlite3.Row, issues: list[str]) -> Plan2CardVersion:
    card_id = _text(row["id"], "card.id", issues)
    upgrade = _plain_int(row["upgrade_count"], f"card:{card_id}.upgrade", issues)
    raw = _json_value(row["raw_json"], f"card:{card_id}#{upgrade}.raw_json", issues)
    if not isinstance(raw, Mapping):
        issues.append(f"card-raw-object-required:{card_id}#{upgrade}")
        raw = {}
    if set(raw) != _CARD_RAW_KEYS:
        issues.append(f"card-raw-key-shape:{card_id}#{upgrade}")
    slots = _parse_card_slots(card_id, upgrade, row["play_effects_json"], issues)
    raw_slots = raw.get("playEffects")
    parsed_slots = _json_value(row["play_effects_json"], "card.play_effects", issues)
    if raw_slots != parsed_slots:
        issues.append(f"card-raw-play-effects-mismatch:{card_id}#{upgrade}")
    core_expected = {
        "id": card_id,
        "upgradeCount": upgrade,
        "name": TARGET_CARD_NAMES[upgrade] if upgrade in TARGET_UPGRADES else "",
        "planType": PLAN2,
        "category": ACTIVE_SKILL,
        "stamina": 0,
        "costType": COST_TYPE,
        "costValue": COST_VALUE,
        "playProduceExamTriggerId": "",
        "playMovePositionType": MOVE_GRAVE,
        "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
        "moveProduceExamEffectIds": [],
    }
    for key, expected in core_expected.items():
        if raw.get(key) != expected:
            issues.append(f"card-raw-mismatch:{card_id}#{upgrade}:{key}")
    move_trigger = raw.get("moveEffectTriggerType")
    move_effect_ids = raw.get("moveProduceExamEffectIds")
    if not isinstance(move_effect_ids, list) or any(
        type(value) is not str for value in move_effect_ids
    ):
        issues.append(f"card-move-effect-ids-shape:{card_id}#{upgrade}")
        move_effect_ids = []
    return Plan2CardVersion(
        card_id=card_id,
        upgrade_count=upgrade,
        name=_text(row["name"], f"card:{card_id}.name", issues),
        plan_type=_text(row["plan_type"], f"card:{card_id}.plan_type", issues),
        category=_text(row["category"], f"card:{card_id}.category", issues),
        stamina=_plain_int(row["stamina"], f"card:{card_id}.stamina", issues),
        cost_type=_text(row["cost_type"], f"card:{card_id}.cost_type", issues),
        cost_value=_plain_int(row["cost_value"], f"card:{card_id}.cost_value", issues),
        play_trigger_id=_text(
            row["play_trigger_id"], f"card:{card_id}.play_trigger_id", issues
        ),
        move_position_type=_text(
            row["move_position_type"], f"card:{card_id}.move_position_type", issues
        ),
        move_effect_trigger_type=move_trigger if type(move_trigger) is str else "",
        move_effect_ids=tuple(move_effect_ids),
        effect_slots=slots,
    )


def _validate_card(card: Plan2CardVersion, issues: list[str]) -> None:
    if card.card_id != TARGET_CARD_ID:
        issues.append(f"card-id-mismatch:{card.ref}")
    if card.upgrade_count not in TARGET_UPGRADES:
        issues.append(f"card-upgrade-out-of-scope:{card.ref}")
        return
    if card.name != TARGET_CARD_NAMES[card.upgrade_count]:
        issues.append(f"card-name-mismatch:{card.ref}")
    if (card.plan_type, card.category) != (PLAN2, ACTIVE_SKILL):
        issues.append(f"card-plan-category-mismatch:{card.ref}")
    if card.stamina != 0:
        issues.append(f"card-stamina-mismatch:{card.ref}")
    if (card.cost_type, card.cost_value) != (COST_TYPE, COST_VALUE):
        issues.append(f"card-cost-shape:{card.ref}")
    if card.play_trigger_id != "":
        issues.append(f"card-play-trigger-shape:{card.ref}")
    if card.move_position_type != MOVE_GRAVE:
        issues.append(f"card-move-position-shape:{card.ref}")
    if card.move_effect_trigger_type != "ProduceCardMoveEffectTriggerType_Unknown":
        issues.append(f"card-move-trigger-shape:{card.ref}")
    if card.move_effect_ids != ():
        issues.append(f"card-move-effects-shape:{card.ref}")
    if card.ordered_effect_ids != EXPECTED_EFFECT_IDS_BY_UPGRADE[card.upgrade_count]:
        issues.append(f"card-ordered-effects-mismatch:{card.ref}")


def _parse_effect(
    row: sqlite3.Row | None, effect_id: str, issues: list[str]
) -> Plan2ExamEffectRow | None:
    if row is None:
        issues.append(f"missing-target-effect:{effect_id}")
        return None
    spec = _effect_spec(effect_id)
    raw = _json_value(row["raw_json"], f"effect:{effect_id}.raw_json", issues)
    if not isinstance(raw, Mapping):
        issues.append(f"effect-raw-object-required:{effect_id}")
        raw = {}
    if set(raw) != _EFFECT_RAW_KEYS:
        issues.append(f"effect-raw-key-shape:{effect_id}")
    normalized = {
        "effect_type": row["effect_type"],
        "value1": row["value1"],
        "value2": row["value2"],
        "effect_count": row["effect_count"],
        "effect_turn": row["effect_turn"],
        "status_enchant_id": row["status_enchant_id"],
        "chain_effect_id": row["chain_effect_id"],
        "effect_group_ids": tuple(raw.get("effectGroupIds", ()))
        if isinstance(raw.get("effectGroupIds"), list)
        else (),
    }
    for key, expected in spec.items():
        if normalized.get(key) != expected:
            issues.append(f"effect-shape-mismatch:{effect_id}:{key}")
    raw_expected = {
        "id": effect_id,
        "effectType": spec["effect_type"],
        "effectValue1": spec["value1"],
        "effectValue2": spec["value2"],
        "effectCount": spec["effect_count"],
        "effectTurn": spec["effect_turn"],
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": "",
        "movePositionType": MOVE_UNKNOWN,
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
        "chainProduceExamEffectId": spec["chain_effect_id"],
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": list(spec["effect_group_ids"]),
    }
    for key, expected in raw_expected.items():
        if raw.get(key) != expected:
            issues.append(f"effect-raw-mismatch:{effect_id}:{key}")
    for key in ("produceDescriptions", "customizeProduceDescriptions"):
        if not isinstance(raw.get(key), list) or any(
            not isinstance(item, Mapping) for item in raw.get(key, [])
        ):
            issues.append(f"effect-description-shape:{effect_id}:{key}")
    return Plan2ExamEffectRow(
        effect_id=effect_id,
        effect_type=row["effect_type"] if type(row["effect_type"]) is str else "",
        value1=row["value1"] if type(row["value1"]) is int else 0,
        value2=row["value2"] if type(row["value2"]) is int else 0,
        effect_count=row["effect_count"] if type(row["effect_count"]) is int else 0,
        effect_turn=row["effect_turn"] if type(row["effect_turn"]) is int else 0,
        status_enchant_id=(
            row["status_enchant_id"]
            if type(row["status_enchant_id"]) is str
            else ""
        ),
        chain_effect_id=(
            row["chain_effect_id"] if type(row["chain_effect_id"]) is str else ""
        ),
        effect_group_ids=normalized["effect_group_ids"],
        produce_description_count=(
            len(raw["produceDescriptions"])
            if isinstance(raw.get("produceDescriptions"), list)
            else 0
        ),
        customize_description_count=(
            len(raw["customizeProduceDescriptions"])
            if isinstance(raw.get("customizeProduceDescriptions"), list)
            else 0
        ),
    )


def load_plan2_effect_triggers_stamina_thresholds_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2EffectTriggerThresholdCatalog:
    """Read the exact Master slice through a read-only SQLite connection."""

    path = Path(database)
    if not path.is_file():
        raise CatalogContractError(f"Master database not found: {path}")
    issues: list[str] = []
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise CatalogContractError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        trigger_rows: dict[str, EffectTriggerContract] = {}
        for trigger_id in TRIGGER_IDS:
            trigger = _parse_trigger(
                connection.execute(
                    "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
                ).fetchone(),
                trigger_id,
                issues,
            )
            if trigger is not None:
                trigger_rows[trigger_id] = trigger

        raw_cards = connection.execute(
            "SELECT id, upgrade_count, name, plan_type, category, stamina, "
            "cost_type, cost_value, play_trigger_id, move_position_type, "
            "play_effects_json, raw_json FROM card WHERE id = ? "
            "ORDER BY upgrade_count",
            (TARGET_CARD_ID,),
        ).fetchall()
        cards: list[Plan2CardVersion] = []
        for raw_card in raw_cards:
            card = _parse_card(raw_card, issues)
            cards.append(card)
            _validate_card(card, issues)
        target_cards = tuple(
            card for card in cards if card.upgrade_count in TARGET_UPGRADES
        )
        if len(target_cards) != len(TARGET_UPGRADES):
            issues.append(f"unexpected-target-card-version-count:{len(target_cards)}")
        if tuple(card.upgrade_count for card in target_cards) != TARGET_UPGRADES:
            issues.append("target-upgrades-not-exact-0-1-2-3")

        placeholders = ",".join("?" for _ in TARGET_EFFECT_IDS)
        raw_effects = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({placeholders})", TARGET_EFFECT_IDS
        ).fetchall()
        effects_by_id = {str(row["id"]): row for row in raw_effects}
        effect_rows: dict[str, Plan2ExamEffectRow] = {}
        for effect_id in TARGET_EFFECT_IDS:
            effect = _parse_effect(effects_by_id.get(effect_id), effect_id, issues)
            if effect is not None:
                effect_rows[effect_id] = effect

    return Plan2EffectTriggerThresholdCatalog(
        database=str(path),
        trigger_rows_by_id=trigger_rows,
        card_versions=target_cards,
        effect_rows_by_id=effect_rows,
        shape_issues=tuple(dict.fromkeys(issues)),
    )


load_catalog = load_plan2_effect_triggers_stamina_thresholds_catalog


def _contract_from_source(source: object) -> tuple[str, Mapping[str, Any] | None, tuple[str, ...]]:
    if isinstance(source, EffectTriggerContract):
        label = source.id
        return label, source.master_contract(), ()
    if isinstance(source, str):
        label = source
        expected = EXPECTED_TRIGGER_CONTRACTS.get(source)
        return label, expected, () if expected is not None else ("unknown-trigger-id",)
    if isinstance(source, Mapping):
        label = source.get("id", "<unknown-trigger-shape>")
        label = label if type(label) is str else "<unknown-trigger-shape>"
        expected_keys = set(next(iter(EXPECTED_TRIGGER_CONTRACTS.values())))
        if set(source) != expected_keys:
            return label, None, ("unknown-trigger-shape",)
        return label, source, ()
    # The 500 adapter's trigger object is accepted as an audited source.
    master_contract = getattr(source, "master_contract", None)
    if callable(master_contract):
        try:
            contract = master_contract()
        except Exception:
            return "<unknown-trigger-shape>", None, ("unknown-trigger-shape",)
        if isinstance(contract, Mapping):
            label = contract.get("id", "<unknown-trigger-shape>")
            return (
                label if type(label) is str else "<unknown-trigger-shape>",
                contract,
                (),
            )
    return "<unknown-trigger-shape>", None, ("unknown-trigger-shape",)


def _trigger_reasons(
    source: object, catalog: Plan2EffectTriggerThresholdCatalog | None
) -> tuple[str, Mapping[str, Any] | None, tuple[str, ...]]:
    label, source_contract, reasons = _contract_from_source(source)
    if catalog is not None:
        if not catalog.exact_shape_supported:
            reasons = (*reasons, "master-shape-not-exact")
        if isinstance(source, str) and source in catalog.trigger_rows_by_id:
            source_contract = catalog.trigger_rows_by_id[source].master_contract()
        elif isinstance(source, str) and source not in catalog.trigger_rows_by_id:
            reasons = (*reasons, "unknown-trigger-id")
    expected = EXPECTED_TRIGGER_CONTRACTS.get(label)
    if expected is None:
        reasons = (*reasons, "unknown-trigger-id")
    elif source_contract is None:
        reasons = (*reasons, "unknown-trigger-shape")
    else:
        for key, expected_value in expected.items():
            if source_contract.get(key) != expected_value:
                reasons = (*reasons, f"trigger-shape-mismatch:{key}")
    return label, source_contract, tuple(dict.fromkeys(reasons))


def _source_value(source: object, *names: str) -> tuple[bool, object]:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return True, source[name]
        return False, None
    for name in names:
        try:
            return True, getattr(source, name)
        except AttributeError:
            continue
        except Exception:
            return True, None
    return False, None


def _adapt_snapshot(
    source: object | None,
    *,
    default_boundary: TriggerSnapshotBoundary,
    default_phase: str,
) -> tuple[Plan2NoneStaminaSnapshot | None, tuple[str, ...]]:
    if isinstance(source, Plan2NoneStaminaSnapshot):
        return source, ()
    if isinstance(source, StaminaSnapshot):
        try:
            return (
                Plan2NoneStaminaSnapshot(
                    source.current_stamina,
                    source.max_stamina,
                    default_boundary,
                    default_phase,
                ),
                (),
            )
        except (TypeError, ValueError, NativeBoundaryError):
            return None, ("snapshot-values-not-signed-int32-or-finite",)
    if source is None:
        return None, ("none-trigger-snapshot-unavailable",)
    current_found, current = _source_value(
        source, "current_stamina", "currentStamina", "stamina"
    )
    max_found, maximum = _source_value(
        source, "max_stamina", "maxStamina", "maximum_stamina"
    )
    if not current_found or not max_found:
        return None, ("none-trigger-snapshot-unavailable",)
    boundary_found, boundary = _source_value(source, "boundary", "capture_boundary")
    if not boundary_found:
        boundary = default_boundary
    # The direct-build label is descriptive; native snapshot validation uses
    # the already audited pre-payment enum.
    if boundary == DIRECT_BUILD_CALLSITE or boundary == "direct-effect-sequence-build":
        boundary = TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY
    phase_found, phase = _source_value(source, "phase", "event_phase")
    if not phase_found:
        phase = default_phase
    try:
        return (
            Plan2NoneStaminaSnapshot(current, maximum, boundary, phase),
            (),
        )
    except (TypeError, ValueError, NativeBoundaryError):
        return None, ("snapshot-values-not-signed-int32-or-finite",)


adapt_effect_trigger_snapshot = _adapt_snapshot


def _origin(value: object) -> tuple[CardOrigin, tuple[str, ...]]:
    if isinstance(value, CardOrigin):
        return value, ()
    if isinstance(value, _stamina500.CardExecutionKind):
        mapping = {
            _stamina500.CardExecutionKind.ORDINARY: CardOrigin.NORMAL,
            _stamina500.CardExecutionKind.FORCED: CardOrigin.FORCED,
            _stamina500.CardExecutionKind.EXTRA: CardOrigin.EXTRA,
        }
        return mapping.get(value, CardOrigin.NORMAL), ()
    aliases = {
        "ordinary": CardOrigin.NORMAL,
        "ordinary-card-play": CardOrigin.NORMAL,
        "normal-card-play": CardOrigin.NORMAL,
        "forced-card-play": CardOrigin.FORCED,
        "extra-card-play": CardOrigin.EXTRA,
    }
    if isinstance(value, str) and value in aliases:
        return aliases[value], ()
    try:
        return CardOrigin(value), ()
    except (TypeError, ValueError):
        return CardOrigin.NORMAL, ("card-origin-unproven",)


_MISSING: Final = object()


@dataclass(frozen=True, slots=True)
class StaminaThresholdEvaluation:
    trigger_id: str
    threshold: int | None
    event_phase: str | None
    dispatch_phase: str | None
    snapshot: Plan2NoneStaminaSnapshot | None
    snapshot_boundary: TriggerSnapshotBoundary | None
    formula: StaminaMultipleEvaluation | None
    fires: bool | None
    card_origin: CardOrigin
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True
    trigger_selected_before_payment: bool = True
    trigger_rechecked_at_effect: bool = False
    predicate_invocation_count: int = 1
    getter_read_count: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_origin", CardOrigin(self.card_origin))
        if self.snapshot_boundary is not None:
            object.__setattr__(
                self, "snapshot_boundary", TriggerSnapshotBoundary(self.snapshot_boundary)
            )
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    @property
    def resolved(self) -> bool:
        return self.fires is not None

    @property
    def supported(self) -> bool:
        return self.fires is not None and not self.reasons

    @property
    def snapshot_phase(self) -> str | None:
        return None if self.snapshot is None else self.snapshot.phase

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "threshold": self.threshold,
            "event_phase": self.event_phase,
            "dispatch_phase": self.dispatch_phase,
            "snapshot": None if self.snapshot is None else self.snapshot.to_dict(),
            "snapshot_boundary": (
                None
                if self.snapshot_boundary is None
                else self.snapshot_boundary.value
            ),
            "snapshot_phase": self.snapshot_phase,
            "formula": None if self.formula is None else self.formula.to_dict(),
            "fires": self.fires,
            "card_origin": self.card_origin.value,
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
            "trigger_selected_before_payment": self.trigger_selected_before_payment,
            "trigger_rechecked_at_effect": self.trigger_rechecked_at_effect,
            "predicate_invocation_count": self.predicate_invocation_count,
            "getter_read_count": self.getter_read_count,
        }


StaminaEffectTriggerEvaluation = StaminaThresholdEvaluation
EffectTriggerEvaluation = StaminaThresholdEvaluation


def evaluate_effect_trigger(
    trigger_or_id: object = TRIGGER_ID_500,
    snapshot: object | None = None,
    *,
    catalog: Plan2EffectTriggerThresholdCatalog | None = None,
    event_phase: str | None = PHASE_NONE,
    dispatch_phase: str | None = DIRECT_DISPATCH_PHASE,
    current_stamina: object = _MISSING,
    max_stamina: object = _MISSING,
    boundary: TriggerSnapshotBoundary | str = (
        TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY
    ),
    card_origin: CardOrigin | str = CardOrigin.NORMAL,
) -> StaminaThresholdEvaluation:
    """Evaluate one exact target gate, with fail-closed shape handling."""

    trigger_id, contract, reasons = _trigger_reasons(trigger_or_id, catalog)
    reasons_list = list(reasons)
    origin, origin_reasons = _origin(card_origin)
    reasons_list.extend(origin_reasons)
    threshold = TRIGGER_THRESHOLD_BY_ID.get(trigger_id)
    if contract is not None:
        candidate = contract.get("field_values")
        if isinstance(candidate, list) and len(candidate) == 1 and type(candidate[0]) is int:
            threshold = candidate[0]
    if threshold not in TARGET_THRESHOLDS:
        reasons_list.append("threshold-unproven")
        threshold = None
    if event_phase != PHASE_NONE:
        reasons_list.append("event-phase-mismatch-or-unproven")
    if dispatch_phase != DIRECT_DISPATCH_PHASE:
        reasons_list.append("direct-effect-dispatch-phase-unproven")

    source = snapshot
    if source is None and (
        current_stamina is not _MISSING or max_stamina is not _MISSING
    ):
        source = {
            "current_stamina": (
                None if current_stamina is _MISSING else current_stamina
            ),
            "max_stamina": None if max_stamina is _MISSING else max_stamina,
            "boundary": boundary,
            "phase": event_phase,
        }
    try:
        default_boundary = TriggerSnapshotBoundary(boundary)
    except (TypeError, ValueError):
        default_boundary = TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY
        reasons_list.append("snapshot-boundary-unproven")
    adapted, adaptation_reasons = _adapt_snapshot(
        source, default_boundary=default_boundary, default_phase=event_phase or PHASE_NONE
    )
    reasons_list.extend(adaptation_reasons)
    formula: StaminaMultipleEvaluation | None = None
    snapshot_boundary: TriggerSnapshotBoundary | None = None
    if adapted is not None:
        snapshot_boundary = adapted.boundary
        if adapted.phase != PHASE_NONE:
            reasons_list.append("snapshot-phase-is-not-none")
        if adapted.boundary is not TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY:
            reasons_list.append("snapshot-is-after-card-payment-or-effect")
        if threshold is not None:
            try:
                # The 500 ordinary path is delegated intact to the proven
                # Plan2 adapter.  Other origins share the same native field
                # body; they use the exact same exported formula object.
                if trigger_id == TRIGGER_ID_500 and origin is CardOrigin.NORMAL:
                    legacy = _stamina500.evaluate_plan2_none_stamina_up_multiple(
                        NONE_STAMINA_UP_MULTIPLE_500,
                        adapted,
                        event_phase=PHASE_NONE,
                        execution_kind=_stamina500.CardExecutionKind.ORDINARY,
                    )
                    formula = legacy.formula
                    reasons_list.extend(legacy.reasons)
                else:
                    formula = evaluate_stamina_multiple(
                        StaminaField.STAMINA_UP_MULTIPLE,
                        threshold,
                        StaminaSnapshot(
                            adapted.current_stamina, adapted.max_stamina
                        ),
                    )
                    reasons_list.extend(formula.reasons)
            except (TypeError, ValueError, NativeBoundaryError):
                reasons_list.append("snapshot-values-not-signed-int32-or-finite")

    unique_reasons = tuple(dict.fromkeys(reasons_list))
    fires = formula.fires if formula is not None and not unique_reasons else None
    return StaminaThresholdEvaluation(
        trigger_id=trigger_id,
        threshold=threshold,
        event_phase=event_phase,
        dispatch_phase=dispatch_phase,
        snapshot=adapted,
        snapshot_boundary=snapshot_boundary,
        formula=formula,
        fires=fires,
        card_origin=origin,
        reasons=unique_reasons,
    )


evaluate_trigger = evaluate_effect_trigger
evaluate = evaluate_effect_trigger
evaluate_plan2_effect_trigger = evaluate_effect_trigger
evaluate_plan2_none_stamina_up_multiple = evaluate_effect_trigger


@dataclass(frozen=True, slots=True)
class DirectEffectSequenceEvaluation:
    card_id: str
    upgrade_count: int
    card_origin: CardOrigin
    target_slot_indexes: tuple[int, ...]
    evaluations: tuple[StaminaThresholdEvaluation, ...]
    queue_effect_ids: tuple[str, ...]
    deferred_effect_ids: tuple[str, ...]
    predicate_invocation_count: int
    getter_read_count: int
    each_gate_re_reads_current_max: bool
    same_pre_payment_build_snapshot: bool
    prior_effects_can_mutate_stamina: bool
    prior_effects: tuple[str, ...]
    prior_effects_by_gate: tuple[tuple[int, tuple[str, ...]], ...]
    trigger_rechecked_at_effect: bool
    supported: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_origin", CardOrigin(self.card_origin))
        object.__setattr__(self, "target_slot_indexes", tuple(self.target_slot_indexes))
        object.__setattr__(self, "evaluations", tuple(self.evaluations))
        object.__setattr__(self, "queue_effect_ids", tuple(self.queue_effect_ids))
        object.__setattr__(self, "deferred_effect_ids", tuple(self.deferred_effect_ids))
        object.__setattr__(
            self,
            "prior_effects_by_gate",
            tuple((index, tuple(effects)) for index, effects in self.prior_effects_by_gate),
        )
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    @property
    def fires(self) -> tuple[bool | None, ...]:
        return tuple(evaluation.fires for evaluation in self.evaluations)

    @property
    def snapshot_values_by_gate(self) -> tuple[tuple[int, int] | None, ...]:
        return tuple(
            None
            if evaluation.snapshot is None
            else (
                evaluation.snapshot.current_stamina,
                evaluation.snapshot.max_stamina,
            )
            for evaluation in self.evaluations
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "card_origin": self.card_origin.value,
            "target_slot_indexes": list(self.target_slot_indexes),
            "evaluations": [evaluation.to_dict() for evaluation in self.evaluations],
            "fires": list(self.fires),
            "queue_effect_ids": list(self.queue_effect_ids),
            "deferred_effect_ids": list(self.deferred_effect_ids),
            "predicate_invocation_count": self.predicate_invocation_count,
            "getter_read_count": self.getter_read_count,
            "each_gate_re_reads_current_max": self.each_gate_re_reads_current_max,
            "same_pre_payment_build_snapshot": self.same_pre_payment_build_snapshot,
            "prior_effects_can_mutate_stamina": self.prior_effects_can_mutate_stamina,
            "prior_effects": list(self.prior_effects),
            "prior_effects_by_gate": [
                {"slot_index": index, "effects": list(effects)}
                for index, effects in self.prior_effects_by_gate
            ],
            "trigger_rechecked_at_effect": self.trigger_rechecked_at_effect,
            "supported": self.supported,
            "reasons": list(self.reasons),
        }


def simulate_direct_effect_sequence(
    catalog: Plan2EffectTriggerThresholdCatalog | None = None,
    *,
    upgrade_count: int = 0,
    card_id: str = TARGET_CARD_ID,
    snapshot: object | None = None,
    current_stamina: object = _MISSING,
    max_stamina: object = _MISSING,
    event_phase: str | None = PHASE_NONE,
    dispatch_phase: str | None = DIRECT_DISPATCH_PHASE,
    boundary: TriggerSnapshotBoundary | str = (
        TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY
    ),
    card_origin: CardOrigin | str = CardOrigin.NORMAL,
) -> DirectEffectSequenceEvaluation:
    """Build the card's five ordered direct effects from one build context."""

    if catalog is None:
        catalog = load_catalog()
    origin, origin_reasons = _origin(card_origin)
    reasons = list(origin_reasons)
    card = catalog.card(card_id, upgrade_count)
    if card is None:
        reasons.append("unknown-target-card-version")
        return DirectEffectSequenceEvaluation(
            card_id,
            upgrade_count,
            origin,
            (),
            (),
            (),
            (),
            0,
            0,
            True,
            True,
            (),
            (),
            False,
            False,
            tuple(reasons),
        )
    if not catalog.exact_shape_supported:
        reasons.append("master-shape-not-exact")
    target_slots = tuple(
        slot for slot in card.effect_slots if slot.trigger_id in TRIGGER_IDS
    )
    target_indexes = tuple(slot.slot_index for slot in target_slots)
    if target_indexes != (2, 3, 4):
        reasons.append("target-trigger-slot-order-unproven")

    source = snapshot
    if source is None and (
        current_stamina is not _MISSING or max_stamina is not _MISSING
    ):
        source = {
            "current_stamina": (
                None if current_stamina is _MISSING else current_stamina
            ),
            "max_stamina": None if max_stamina is _MISSING else max_stamina,
            "boundary": boundary,
            "phase": event_phase,
        }
    evaluations: list[StaminaThresholdEvaluation] = []
    for slot in target_slots:
        evaluation = evaluate_effect_trigger(
            slot.trigger_id,
            source,
            catalog=catalog,
            event_phase=event_phase,
            dispatch_phase=dispatch_phase,
            boundary=boundary,
            card_origin=origin,
        )
        evaluations.append(evaluation)
        reasons.extend(evaluation.reasons)

    if not evaluations or any(not evaluation.supported for evaluation in evaluations):
        reasons.append("one-or-more-effect-trigger-gates-unresolved")
        queue_effect_ids: tuple[str, ...] = ()
        supported = False
    else:
        queue: list[str] = []
        for slot in card.effect_slots:
            if not slot.trigger_id:
                queue.append(slot.effect_id)
                continue
            evaluation = next(
                item for item in evaluations if item.trigger_id == slot.trigger_id
            )
            if evaluation.fires:
                queue.append(slot.effect_id)
        queue_effect_ids = tuple(queue)
        supported = not reasons

    # The timer executor installs a one-shot relative StartTurn status.  Its
    # CardDraw child is not executed in this direct-effect slot.
    prior_effects = (
        "slot0:e_effect-exam_review-0001:no-stamina-mutation",
        "slot1:e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0002:install-only",
    )
    prior_effects_by_gate = (
        (
            2,
            (
                "slot0:e_effect-exam_review-0001:no-stamina-mutation",
                "slot1:e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0002:install-only",
            ),
        ),
        (
            3,
            (
                "slot0:e_effect-exam_review-0001:no-stamina-mutation",
                "slot1:e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0002:install-only",
                "slot2:e_effect-exam_review-0001:conditional-review:no-stamina-mutation",
            ),
        ),
        (
            4,
            (
                "slot0:e_effect-exam_review-0001:no-stamina-mutation",
                "slot1:e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0002:install-only",
                "slot2:e_effect-exam_review-0001:conditional-review:no-stamina-mutation",
                "slot3:e_effect-exam_review-0002:conditional-review:no-stamina-mutation",
            ),
        ),
    )
    return DirectEffectSequenceEvaluation(
        card_id=card_id,
        upgrade_count=upgrade_count,
        card_origin=origin,
        target_slot_indexes=target_indexes,
        evaluations=tuple(evaluations),
        queue_effect_ids=queue_effect_ids,
        deferred_effect_ids=(TIMER_CHILD_EFFECT_ID,),
        predicate_invocation_count=len(evaluations),
        getter_read_count=len(evaluations),
        each_gate_re_reads_current_max=True,
        same_pre_payment_build_snapshot=True,
        prior_effects_can_mutate_stamina=False,
        prior_effects=prior_effects,
        prior_effects_by_gate=prior_effects_by_gate,
        trigger_rechecked_at_effect=False,
        supported=supported,
        reasons=tuple(dict.fromkeys(reasons)),
    )


build_direct_effect_sequence = simulate_direct_effect_sequence


@dataclass(frozen=True, slots=True)
class CardTransactionTrace:
    card_id: str
    upgrade_count: int
    card_origin: CardOrigin
    cost_paid: bool
    is_use_playable_count: bool
    event_trace: tuple[str, ...]
    direct_sequence: DirectEffectSequenceEvaluation
    supported: bool
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_origin", CardOrigin(self.card_origin))
        object.__setattr__(self, "event_trace", tuple(self.event_trace))
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "card_origin": self.card_origin.value,
            "cost_paid": self.cost_paid,
            "is_use_playable_count": self.is_use_playable_count,
            "event_trace": list(self.event_trace),
            "direct_sequence": self.direct_sequence.to_dict(),
            "supported": self.supported,
            "reasons": list(self.reasons),
        }


def build_card_transaction_trace(
    catalog: Plan2EffectTriggerThresholdCatalog | None = None,
    *,
    upgrade_count: int = 0,
    card_id: str = TARGET_CARD_ID,
    current_stamina: object = _MISSING,
    max_stamina: object = _MISSING,
    snapshot: object | None = None,
    card_origin: CardOrigin | str = CardOrigin.NORMAL,
    is_consume_cost: bool | None = None,
    is_use_playable_count: bool | None = None,
) -> CardTransactionTrace:
    """Return the proven transaction order without mutating game state."""

    if catalog is None:
        catalog = load_catalog()
    origin, origin_reasons = _origin(card_origin)
    cost_paid = (
        origin is CardOrigin.NORMAL if is_consume_cost is None else is_consume_cost
    )
    playable_count = (
        origin is CardOrigin.NORMAL
        if is_use_playable_count is None
        else is_use_playable_count
    )
    if type(cost_paid) is not bool or type(playable_count) is not bool:
        sequence = simulate_direct_effect_sequence(
            catalog,
            upgrade_count=upgrade_count,
            card_id=card_id,
            snapshot=snapshot,
            current_stamina=current_stamina,
            max_stamina=max_stamina,
            card_origin=origin,
        )
        return CardTransactionTrace(
            card_id,
            upgrade_count,
            origin,
            False,
            False,
            (),
            sequence,
            False,
            (*origin_reasons, "transaction-policy-shape-unproven"),
        )
    sequence = simulate_direct_effect_sequence(
        catalog,
        upgrade_count=upgrade_count,
        card_id=card_id,
        snapshot=snapshot,
        current_stamina=current_stamina,
        max_stamina=max_stamina,
        card_origin=origin,
    )
    reasons = list(origin_reasons)
    reasons.extend(sequence.reasons)
    if not sequence.supported:
        return CardTransactionTrace(
            card_id,
            upgrade_count,
            origin,
            cost_paid,
            playable_count,
            (),
            sequence,
            False,
            tuple(dict.fromkeys(reasons)),
        )

    prefix = origin.value
    events: list[str] = []
    if origin is CardOrigin.NORMAL:
        events.extend(
            (
                f"{prefix}:UseHand:ValidateUseHandCard",
                f"{prefix}:UseHand:QueueUseHand",
                f"{prefix}:UseHand:RemoveUseHand",
                f"{prefix}:UseHand:history-log",
                f"{prefix}:{DIRECT_BUILD_CALLSITE}:pre-payment",
            )
        )
    else:
        events.append(f"{prefix}:UsePool:RemoveCurrent")
        events.append(f"{prefix}:UsePool:isConsumeCost={str(cost_paid).lower()}")
        if cost_paid:
            # UsePool's consuming branch pays before ExecuteCardCommandImpl
            # builds the direct-effect candidates; the default forced/extra
            # branch is the no-cost path.
            events.append(f"{prefix}:UsePool:ValidateCardCost")
            events.append(f"{prefix}:ConsumeCardCost:{COST_TYPE}:{COST_VALUE}")
        events.append(f"{prefix}:{DIRECT_BUILD_CALLSITE}:dispatch=UsePool")
    for slot, evaluation in zip(
        sequence.target_slot_indexes, sequence.evaluations, strict=True
    ):
        threshold = evaluation.threshold
        events.append(
            f"{prefix}:build:effect-trigger:slot{slot}:threshold={threshold}:"
            "read-current-max"
        )
    events.append(f"{prefix}:build:ExamCardData.PlayCardCount")
    events.append(f"{prefix}:build:append:UserCardAfterCheck")
    events.append(f"{prefix}:build:append:MovePlayCard(destination={MOVE_GRAVE})")
    if origin is CardOrigin.NORMAL and cost_paid:
        events.append(f"{prefix}:ConsumeCardCost:{COST_TYPE}:{COST_VALUE}")
    for slot in sequence.queue_effect_ids:
        if slot == TIMER_EFFECT_ID:
            events.append(f"{prefix}:execute:effect-slot:1:{slot}:TimerInstall")
        elif slot == "e_effect-exam_review-0001":
            # The same effect id occurs at slots 0 and 2.  The textual slot is
            # resolved from the Master order rather than from an effect id.
            card = catalog.card(card_id, upgrade_count)
            indexes = [
                item.slot_index
                for item in card.effect_slots
                if item.effect_id == slot
            ] if card is not None else []
            next_index = 0 if not any(
                f"effect-slot:0:{slot}" in existing for existing in events
            ) else (indexes[-1] if indexes else 2)
            events.append(f"{prefix}:execute:effect-slot:{next_index}:{slot}:Review")
        else:
            card = catalog.card(card_id, upgrade_count)
            slot_index = next(
                (
                    item.slot_index
                    for item in card.effect_slots
                    if item.effect_id == slot
                ),
                -1,
            ) if card is not None else -1
            events.append(
                f"{prefix}:execute:effect-slot:{slot_index}:{slot}:effect"
            )
    events.append(f"{prefix}:execute:UserCardAfterCheck")
    events.append(f"{prefix}:move:ExamCardData.PlayCardCount")
    events.append(f"{prefix}:move:ExamParameterModel.PlayCardCountIncrement")
    if playable_count:
        events.append(f"{prefix}:move:Status.UpdatePlayCardCount")
    events.append(f"{prefix}:move:RemoveCurrent:destination={MOVE_GRAVE}")
    events.append(f"{prefix}:execute:SeparateActivity(end)")
    events.append(f"{prefix}:timer-child:{TIMER_CHILD_EFFECT_ID}:deferred-next-relative-StartTurn")
    return CardTransactionTrace(
        card_id=card_id,
        upgrade_count=upgrade_count,
        card_origin=origin,
        cost_paid=cost_paid,
        is_use_playable_count=playable_count,
        event_trace=tuple(events),
        direct_sequence=sequence,
        supported=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
    )


build_transaction_trace = build_card_transaction_trace


@dataclass(frozen=True, slots=True)
class FamilyAccounting:
    gap: str
    affected: int
    direct: int
    co: int
    affected_refs: tuple[str, ...]
    direct_refs: tuple[str, ...]
    co_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap": self.gap,
            "affected": self.affected,
            "direct": self.direct,
            "co": self.co,
            "affected_refs": list(self.affected_refs),
            "direct_refs": list(self.direct_refs),
            "co_refs": list(self.co_refs),
        }


@dataclass(frozen=True, slots=True)
class CoverageAccounting:
    artifact: str
    target_refs: tuple[str, ...]
    families: tuple[FamilyAccounting, ...]
    combined_affected: int
    combined_direct: int
    combined_co: int
    combined_direct_refs: tuple[str, ...]
    combined_co_refs: tuple[str, ...]
    combined_unlocked_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": self.artifact,
            "target_refs": list(self.target_refs),
            "families": [family.to_dict() for family in self.families],
            "combined": {
                "affected": self.combined_affected,
                "direct": self.combined_direct,
                "co": self.combined_co,
                "direct_refs": list(self.combined_direct_refs),
                "co_refs": list(self.combined_co_refs),
                "unlocked_refs": list(self.combined_unlocked_refs),
            },
        }


def build_coverage_accounting(
    path: Path | str = DEFAULT_FORMAL_COVERAGE_PATH,
) -> CoverageAccounting:
    """Read, never rewrite, the four target rows from formal coverage."""

    coverage_path = Path(path)
    if not coverage_path.is_file():
        raise CoverageContractError(f"formal coverage not found: {coverage_path}")
    try:
        data = json.loads(coverage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CoverageContractError("formal coverage is not readable JSON") from error
    rows = [
        row
        for row in data.get("cards", [])
        if isinstance(row, Mapping) and row.get("card_id") == TARGET_CARD_ID
    ]
    rows = sorted(rows, key=lambda row: row.get("upgrade", -1))
    if len(rows) != 4 or tuple(row.get("upgrade") for row in rows) != TARGET_UPGRADES:
        raise CoverageContractError("target formal rows are not exactly upgrades 0..3")
    expected_gaps = set(FORMAL_TRIGGER_GAPS)
    target_refs: list[str] = []
    gap_sets: dict[str, set[str]] = {}
    for row in rows:
        gaps = row.get("gaps")
        if not isinstance(gaps, list) or any(type(gap) is not str for gap in gaps):
            raise CoverageContractError("target formal gap shape is altered")
        observed_gaps = set(gaps)
        if observed_gaps == expected_gaps:
            accounting_gaps = observed_gaps
        elif not observed_gaps:
            batch = row.get("plan2_trigger_review_encore_batch_overlay")
            family = row.get("effect_triggers_stamina_thresholds_overlay")
            if (
                not isinstance(batch, Mapping)
                or not isinstance(family, Mapping)
                or set(batch.get("previous_gaps", ())) != expected_gaps
                or batch.get("remaining_gaps") != []
                or family.get("slots_executable") is not True
                or family.get("newly_executable_from_previous_baseline") is not True
            ):
                raise CoverageContractError(
                    "target formal integrated overlay shape is altered"
                )
            accounting_gaps = expected_gaps
        else:
            raise CoverageContractError("target formal trigger gap set is altered")
        ref = f"{row.get('card_id')}#{row.get('upgrade')}"
        target_refs.append(ref)
        gap_sets[ref] = accounting_gaps

    families: list[FamilyAccounting] = []
    for gap in FORMAL_TRIGGER_GAPS:
        affected_refs = tuple(ref for ref in target_refs if gap in gap_sets[ref])
        direct_refs = tuple(
            ref for ref in affected_refs if gap_sets[ref] == {gap}
        )
        co_refs = tuple(ref for ref in affected_refs if ref not in direct_refs)
        families.append(
            FamilyAccounting(
                gap=gap,
                affected=len(affected_refs),
                direct=len(direct_refs),
                co=len(co_refs),
                affected_refs=affected_refs,
                direct_refs=direct_refs,
                co_refs=co_refs,
            )
        )
    combined_direct_refs = tuple(
        ref
        for ref in target_refs
        if not (gap_sets[ref] - expected_gaps)
        and gap_sets[ref] <= expected_gaps
        and not (gap_sets[ref] - set(FORMAL_TRIGGER_GAPS))
    )
    # The target slice is valid only when the three gaps are the complete
    # remaining set.  Every exact target row is therefore unlocked by batch.
    combined_co_refs = tuple(
        ref for ref in target_refs if ref not in combined_direct_refs
    )
    return CoverageAccounting(
        artifact=str(coverage_path),
        target_refs=tuple(target_refs),
        families=tuple(families),
        combined_affected=len(target_refs),
        combined_direct=len(combined_direct_refs),
        combined_co=len(combined_co_refs),
        combined_direct_refs=combined_direct_refs,
        combined_co_refs=combined_co_refs,
        combined_unlocked_refs=combined_direct_refs,
    )


accounting = build_coverage_accounting


ANDROID_V323_NATIVE_AUDIT: Final = {
    "platform": "Android",
    "game_version": "3.2.3",
    "formula_method": "Campus.InGame.Exam.ExamExtensions.IsFieldStatusTriggerStatusEffect",
    "formula_method_va": "0x68082D4",
    "field_enum": 4,
    "current_getter_va": "0x7EB7ABC",
    "current_getter": "Campus.InGame.Exam.ExamParameterModel$$get_Stamina",
    "max_getter_va": "0x7EB7BB0",
    "max_getter": "Campus.InGame.Exam.ExamParameterModel$$get_MaxStamina",
    "denominator_bits": "0x447A0000",
    "denominator": 1000.0,
    "operations": (
        "SCVTF signed-int32 current",
        "SCVTF signed-int32 max",
        "FDIV float32(current) / float32(max)",
        "SCVTF signed-int32 value",
        "FDIV float32(value) / float32(1000.0f)",
        "FCMP S0,S1",
        "CSET W0,GE",
    ),
    "comparison": ">= inclusive",
    "integer_rounding": "none",
    "native_guard": "no explicit clamp/zero/overcap guard in the field body",
    "adapter_fail_closed": (
        "max<=0, current<0, current>max, non-signed-int32, and unknown shape"
    ),
    "callsite": {
        "method": "ExamSequence.ExecuteCardCommandImpl",
        "rva_range": "0x7ECE628-0x7ECF228",
        "list_getter": "ExamCardData.get_PlayEffectTriggerList",
        "predicate": "ExamExtensions.IsEffectTriggerFieldValid",
        "command_factory": "ExamPlayCommand.CreatePlayEffectCommand(effect)",
        "factory_arity": 1,
        "snapshot": "pre-payment direct-effect sequence build",
        "gates": "slot 2, slot 3, slot 4 each invoke the predicate once",
        "runtime_recheck": False,
    },
    "transaction_order": {
        "normal": "build/gate -> card cost -> direct effects -> after-check -> move/count/history",
        "forced_extra": "UsePool(isConsumeCost=false) -> build/gate -> direct effects -> after-check -> move/count; consuming UsePool pays before build",
        "timer": "slot 1 installs one-shot relative StartTurn; child CardDraw2 is deferred",
        "stamina_mutation_before_gates": False,
    },
}

PC_METADATA_AUDIT: Final = {
    "metadata_shape_compatible": True,
    "matched_method_sets": {
        "ExamEffectUtility": "93/93",
        "IExamSequenceHandler": "24/24",
        "ExamSequence": "98/98",
        "fields": "0/0",
        "methods": "29/29",
    },
    "native_body_parity": "not proven",
    "native_scan_authoritative": False,
    "native_scan_candidates": [],
    "claim_boundary": "PC metadata supports routing only; Android owns exact formula/timing proof",
}


def build_native_audit(
    catalog: Plan2EffectTriggerThresholdCatalog | None = None,
    *,
    coverage_path: Path | str = DEFAULT_FORMAL_COVERAGE_PATH,
) -> dict[str, Any]:
    """Return the bounded native audit and read-only accounting evidence."""

    if catalog is None:
        catalog = load_catalog()
    coverage = build_coverage_accounting(coverage_path)
    return {
        "scope": {
            "card_id": TARGET_CARD_ID,
            "card_name": TARGET_CARD_NAME,
            "upgrades": list(TARGET_UPGRADES),
            "trigger_ids": list(TRIGGER_IDS),
            "thresholds": list(TARGET_THRESHOLDS),
            "focused_only": True,
            "runtime_agent_calls": False,
            "security_hash_clock_work": False,
            "writes_existing_json": False,
        },
        "master": catalog.to_dict(),
        "android_v323": dict(ANDROID_V323_NATIVE_AUDIT),
        "pc": dict(PC_METADATA_AUDIT),
        "accounting": coverage.to_dict(),
        "reuse": {
            "shared_predicate_identity": True,
            "shared_predicate_module": "plan2_stamina_up500_block_fix",
            "three_thresholds_same_callsite": True,
            "threshold_formula": "float32(current)/float32(max) >= float32(value)/float32(1000)",
        },
        "fail_closed": {
            "altered_master_shape": True,
            "unknown_trigger": True,
            "wrong_phase_or_dispatch": True,
            "invalid_stamina_domain": True,
        },
        "forbidden_scope_untouched": {
            "plan2_core_runtime": True,
            "native_search": True,
            "formal_coverage_json": True,
            "plan3": True,
            "gui_controller": True,
        },
    }


build_plan2_effect_triggers_stamina_thresholds_audit = build_native_audit
native_audit = build_native_audit


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def formula_summary(threshold: int) -> dict[str, Any]:
    """Small deterministic probe used by focused tests and the final report."""

    result = evaluate_effect_trigger(
        TRIGGER_ID_BY_THRESHOLD[threshold],
        current_stamina=threshold,
        max_stamina=1000,
    )
    return {
        "threshold": threshold,
        "current_ratio": None if result.formula is None else result.formula.current_ratio,
        "threshold_ratio": None
        if result.formula is None
        else result.formula.threshold_ratio,
        "fires": result.fires,
        "expected_threshold_ratio": _f32(_f32(float(threshold)) / _f32(1000.0)),
    }
