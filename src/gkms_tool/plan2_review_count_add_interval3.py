"""Exact standalone Plan2 ReviewCountAdd / interval-3 listener leaf.

This module is deliberately not registered in the central Plan2 runtime.  It
models only the two co-blocking families used by ``p_card-02-ido-3_173``:

* the permanent listener whose Target card must carry the visible Review
  effect group, whose listener-local phase-23 count is divisible by three,
  and whose post-payment Review value is at least six; and
* ``ExamReviewCountAdd(value=1, turn=3)``, including same-Turn layer merge,
  native Int32 wrap-then-clamp, freshness, and TurnCheck projection.

The native boundary represented here is::

    SetPlayingCard -> Target snapshot -> phase-23 count -> payment ->
    ReviewUp6 gate -> listener children -> ordered direct effects -> final move

``Target`` is the single supplied played-card object, not a DeckAll search.
The standalone transaction records its GUID so a caller can safely split the
snapshot and execution stages: new cards are irrelevant, the same GUID may be
relocated, and a missing or identity-drifted GUID fails closed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import sqlite3
from typing import TypeAlias

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_card_play_playing_search_trigger import PlaySource
from .plan2_play_count_interval5 import AcceptedPlay, Plan2PlayCountPhaseCounter
from .plan2_review_multiple import (
    Plan2ReviewScoreTransition,
    Plan2ScoreApplicator,
    simulate_end_turn_review_score,
)
from .plan2_state import (
    PERMANENT_TURN,
    Plan2EndTurnEffect,
    Plan2EndTurnListener,
    Plan2State,
    simulate_plan2_turn_start,
)


CARD_ID = "p_card-02-ido-3_173"
CARD_NAME = "鮮やかに咲く花"
CARD_UPGRADES = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple((CARD_ID, value) for value in CARD_UPGRADES)
CARD_STAMINA_BY_UPGRADE = (5, 2, 2, 2)

PLAN2 = "ProducePlanType_Plan2"
CATEGORY_MENTAL = "ProduceCardCategory_MentalSkill"
COST_UNKNOWN = "ExamCostType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"

INSTALLER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_173-enc01"
)
STATUS_ENCHANT_ID = "enchant-p_card-02-ido-3_173-enc01"
TRIGGER_ID = (
    "e_trigger-exam_play_count_interval-3-review_up-6-"
    "p_card_search-target-effect_group-visible-exam_review-000"
)
TARGET_SEARCH_ID = "p_card_search-target-effect_group-visible-exam_review-000"
CHILD_EFFECT_ID = "e_effect-exam_review_count_add-0001-03"

EFFECT_STATUS_ENCHANT = "ProduceExamEffectType_ExamStatusEnchant"
EFFECT_REVIEW = "ProduceExamEffectType_ExamReview"
EFFECT_REVIEW_COUNT_ADD = "ProduceExamEffectType_ExamReviewCountAdd"
PHASE_PLAY_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
FIELD_REVIEW_UP = "ProduceExamFieldStatusType_ReviewUp"
TARGET_POSITION = "ProduceCardPositionType_Target"

INTERVAL = 3
REVIEW_THRESHOLD = 6
PHASE_SEMANTIC_NUMBER = 23
CHILD_VALUE = 1
CHILD_TURN = 3

STATUS_ENCHANT_GROUP = "effect_group-visible-exam_status_enchant-000"
REVIEW_GROUP = "effect_group-visible-exam_review-000"
EXPECTED_CARD_GROUPS = (STATUS_ENCHANT_GROUP, REVIEW_GROUP)
EXPECTED_WRAPPER_GROUPS = EXPECTED_CARD_GROUPS
EXPECTED_CHILD_GROUPS = (REVIEW_GROUP,)

REVIEW_EFFECT_BY_UPGRADE: Mapping[int, str | None] = {
    0: None,
    1: None,
    2: "e_effect-exam_review-0002",
    3: "e_effect-exam_review-0003",
}
EXPECTED_EFFECT_IDS_BY_UPGRADE: Mapping[int, tuple[str, ...]] = {
    0: (INSTALLER_EFFECT_ID,),
    1: (INSTALLER_EFFECT_ID,),
    2: ("e_effect-exam_review-0002", INSTALLER_EFFECT_ID),
    3: ("e_effect-exam_review-0003", INSTALLER_EFFECT_ID),
}

EFFECT_FAMILY_GAP = "C:effect:ProduceExamEffectType_ExamReviewCountAdd"
TRIGGER_FAMILY_GAP = f"C:status-trigger:{TRIGGER_ID}"

NATIVE_TRANSACTION_ORDER = (
    "set-playing-card",
    "immutable-target-guid-snapshot",
    "increment-matching-listener-phase23-counts",
    "pay-cost",
    "review-up-6-field-gate",
    "listener-children-in-active-order",
    "ordered-direct-card-effects",
    "final-zone-move",
    "card-play-after-and-interval-after-listeners",
)

_UNKNOWN_PLAN = "ProducePlanType_Unknown"
_UNKNOWN_CARD_STATUS = "ProduceCardSearchStatusType_Unknown"
_UNKNOWN_CARD_ORDER = "ProduceCardOrderType_Unknown"
_UNKNOWN_MIN_MAX = "ConditionMinMaxType_Unknown"
_UNKNOWN_EXAM_EFFECT = "ProduceExamEffectType_Unknown"
_UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"
_UNKNOWN_PICK_RANGE = "ProducePickRangeType_Unknown"
_UNKNOWN_PICK_COUNT = "ProducePickCountType_Unknown"
_UNKNOWN_LESSON = "ProduceStepLessonType_Unknown"


class Plan2ReviewCountAddInterval3ContractError(ValueError):
    """Master/native/runtime input is outside this exact standalone leaf."""


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{label} is outside Int32"
        )
    return value


def _i32(value: int) -> int:
    return ((value + (1 << 31)) & 0xFFFF_FFFF) - (1 << 31)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        raw = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{label}: invalid JSON object"
        ) from error
    if not isinstance(raw, dict):
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{label}: JSON is not an object"
        )
    return raw


def _json_array(value: object, label: str) -> list[object]:
    try:
        raw = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{label}: invalid JSON array"
        ) from error
    if not isinstance(raw, list):
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{label}: JSON is not an array"
        )
    return raw


def _require_raw(raw: Mapping[str, object], key: str, expected: object, label: str) -> None:
    if raw.get(key) != expected:
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{label}: {key} drifted"
        )


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddFamilyAccounting:
    gap: str
    affected: int
    direct: int
    co_blocked: int

    def __post_init__(self) -> None:
        if self.affected != self.direct + self.co_blocked:
            raise ValueError("family accounting does not balance")


EFFECT_FAMILY_ACCOUNTING = Plan2ReviewCountAddFamilyAccounting(
    EFFECT_FAMILY_GAP, 4, 0, 4
)
TRIGGER_FAMILY_ACCOUNTING = Plan2ReviewCountAddFamilyAccounting(
    TRIGGER_FAMILY_GAP, 4, 0, 4
)
COMBINED_FAMILY_ACCOUNTING = Plan2ReviewCountAddFamilyAccounting(
    f"{EFFECT_FAMILY_GAP} + {TRIGGER_FAMILY_GAP}", 4, 4, 0
)
EXPECTED_COMBINED_BATCH_DELTA = 4


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddTarget:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina: int
    force_stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    ordered_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        expected = EXPECTED_EFFECT_IDS_BY_UPGRADE.get(self.upgrade)
        if (
            self.card_id != CARD_ID
            or expected is None
            or self.plan_type != PLAN2
            or self.category != CATEGORY_MENTAL
            or self.stamina != CARD_STAMINA_BY_UPGRADE[self.upgrade]
            or self.force_stamina != 0
            or self.cost_type != COST_UNKNOWN
            or self.cost_value != 0
            or self.play_trigger_id
            or self.move_position_type != MOVE_LOST
            or self.ordered_effect_ids != expected
            or self.effect_group_ids != EXPECTED_CARD_GROUPS
        ):
            raise Plan2ReviewCountAddInterval3ContractError(
                f"source card shape drifted: {self.card_id}#{self.upgrade}"
            )


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddInstaller:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    status_enchant_id: str
    effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.effect_id != INSTALLER_EFFECT_ID
            or self.effect_type != EFFECT_STATUS_ENCHANT
            or (self.value1, self.value2, self.count, self.turn) != (0, 0, 0, -1)
            or self.status_enchant_id != STATUS_ENCHANT_ID
            or self.effect_group_ids != EXPECTED_WRAPPER_GROUPS
        ):
            raise Plan2ReviewCountAddInterval3ContractError(
                "installer shape drifted"
            )


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddTrigger:
    trigger_id: str = TRIGGER_ID
    phase_types: tuple[str, ...] = (PHASE_PLAY_COUNT_INTERVAL,)
    phase_values: tuple[int, ...] = (INTERVAL,)
    field_status_types: tuple[str, ...] = (FIELD_REVIEW_UP,)
    field_status_values: tuple[int, ...] = (REVIEW_THRESHOLD,)
    search_id: str = TARGET_SEARCH_ID

    def __post_init__(self) -> None:
        if (
            self.trigger_id != TRIGGER_ID
            or self.phase_types != (PHASE_PLAY_COUNT_INTERVAL,)
            or self.phase_values != (INTERVAL,)
            or self.field_status_types != (FIELD_REVIEW_UP,)
            or self.field_status_values != (REVIEW_THRESHOLD,)
            or self.search_id != TARGET_SEARCH_ID
        ):
            raise Plan2ReviewCountAddInterval3ContractError(
                "trigger shape drifted"
            )


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddInterval3Program:
    targets: tuple[Plan2ReviewCountAddTarget, ...]
    installer: Plan2ReviewCountAddInstaller
    trigger: Plan2ReviewCountAddTrigger
    search: ProduceCardSearchRule
    child: Plan2EndTurnEffect
    direct_review_effects: tuple[Plan2EndTurnEffect, ...]

    def __post_init__(self) -> None:
        if tuple((item.card_id, item.upgrade) for item in self.targets) != AFFECTED_CARD_VERSIONS:
            raise Plan2ReviewCountAddInterval3ContractError(
                "affected source-card version set drifted"
            )
        if not isinstance(self.installer, Plan2ReviewCountAddInstaller):
            raise TypeError("installer must be the exact typed installer")
        if not isinstance(self.trigger, Plan2ReviewCountAddTrigger):
            raise TypeError("trigger must be the exact typed trigger")
        if _search_errors(self.search):
            raise Plan2ReviewCountAddInterval3ContractError(
                str(_search_errors(self.search))
            )
        if (
            self.child.effect_id != CHILD_EFFECT_ID
            or self.child.effect_type != EFFECT_REVIEW_COUNT_ADD
            or (
                self.child.value1,
                self.child.value2,
                self.child.count,
                self.child.turn,
            )
            != (CHILD_VALUE, 0, 0, CHILD_TURN)
            or self.child.effect_group_ids != EXPECTED_CHILD_GROUPS
        ):
            raise Plan2ReviewCountAddInterval3ContractError("child shape drifted")
        if tuple(effect.effect_id for effect in self.direct_review_effects) != (
            "e_effect-exam_review-0002",
            "e_effect-exam_review-0003",
        ):
            raise Plan2ReviewCountAddInterval3ContractError(
                "direct Review rows drifted"
            )
        for effect, value in zip(self.direct_review_effects, (2, 3), strict=True):
            if (
                effect.effect_type != EFFECT_REVIEW
                or (effect.value1, effect.value2, effect.count, effect.turn)
                != (value, 0, 0, 0)
                or effect.effect_group_ids != (REVIEW_GROUP,)
            ):
                raise Plan2ReviewCountAddInterval3ContractError(
                    f"direct Review row drifted: {effect.effect_id}"
                )

    @property
    def affected_version_count(self) -> int:
        return len(self.targets)

    @property
    def combined_direct_version_count(self) -> int:
        return COMBINED_FAMILY_ACCOUNTING.direct

    def target(self, upgrade: int) -> Plan2ReviewCountAddTarget:
        for target in self.targets:
            if target.upgrade == upgrade:
                return target
        raise KeyError(upgrade)

    def direct_review(self, effect_id: str) -> Plan2EndTurnEffect:
        for effect in self.direct_review_effects:
            if effect.effect_id == effect_id:
                return effect
        raise KeyError(effect_id)


def _search_errors(search: object) -> tuple[str, ...]:
    if not isinstance(search, ProduceCardSearchRule):
        return ("search:typed-rule-required",)
    errors: list[str] = []
    expected = (
        search.id == TARGET_SEARCH_ID,
        search.card_position_type == TARGET_POSITION,
        search.effect_group_ids == (REVIEW_GROUP,),
        search.limit_count == 0,
        not search.card_rarities,
        not search.produce_card_ids,
        not search.upgrade_counts,
        search.plan_type == _UNKNOWN_PLAN,
        not search.card_categories,
        search.card_status_type == _UNKNOWN_CARD_STATUS,
        search.order_type == _UNKNOWN_CARD_ORDER,
        not search.card_search_tag,
        not search.produce_card_random_pool_id,
        search.stamina_min_max_type == _UNKNOWN_MIN_MAX,
        search.stamina_min == 0,
        search.stamina_max == 0,
        search.exam_effect_type == _UNKNOWN_EXAM_EFFECT,
        not search.is_self,
        not search.produce_card_pool_id,
        search.cost_type == COST_UNKNOWN,
        not search.is_customized,
    )
    if not all(expected):
        errors.append("search:exact-target-review-group-shape")
    return tuple(errors)


def _validate_effect_controls(
    row: sqlite3.Row,
    *,
    expected_type: str,
    expected_values: tuple[int, int, int, int],
    status_id: str,
    groups: tuple[str, ...],
) -> Plan2EndTurnEffect:
    effect_id = str(row["id"])
    raw = _json_object(row["raw_json"], effect_id)
    value1, value2, count, turn = expected_values
    for key, expected in (
        ("id", effect_id),
        ("effectType", expected_type),
        ("effectValue1", value1),
        ("effectValue2", value2),
        ("effectCount", count),
        ("effectTurn", turn),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", _UNKNOWN_EXAM_EFFECT),
        ("produceCardSearchId", ""),
        ("movePositionType", _UNKNOWN_MOVE),
        ("pickRangeType", _UNKNOWN_PICK_RANGE),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", _UNKNOWN_PICK_COUNT),
        ("pickCountMin", 0),
        ("pickCountMax", 0),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", _UNKNOWN_PICK_RANGE),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", _UNKNOWN_PICK_COUNT),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", status_id),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
        ("effectGroupIds", list(groups)),
    ):
        _require_raw(raw, key, expected, effect_id)
    if (
        str(row["effect_type"]) != expected_type
        or (
            int(row["value1"]),
            int(row["value2"]),
            int(row["effect_count"]),
            int(row["effect_turn"]),
        )
        != expected_values
        or str(row["status_enchant_id"]) != status_id
        or str(row["chain_effect_id"])
    ):
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{effect_id}: normalized effect shape drifted"
        )
    return Plan2EndTurnEffect(
        effect_id=effect_id,
        effect_type=expected_type,
        value1=value1,
        value2=value2,
        count=count,
        turn=turn,
        effect_group_ids=groups,
    )


def _load_target(row: sqlite3.Row) -> Plan2ReviewCountAddTarget:
    card_id = str(row["id"])
    upgrade = int(row["upgrade_count"])
    label = f"{card_id}#{upgrade}"
    raw = _json_object(row["raw_json"], label)
    expected_effects = EXPECTED_EFFECT_IDS_BY_UPGRADE[upgrade]
    for key, expected in (
        ("id", CARD_ID),
        ("upgradeCount", upgrade),
        ("planType", PLAN2),
        ("category", CATEGORY_MENTAL),
        ("stamina", CARD_STAMINA_BY_UPGRADE[upgrade]),
        ("forceStamina", 0),
        ("costType", COST_UNKNOWN),
        ("costValue", 0),
        ("playProduceExamTriggerId", ""),
        ("playMovePositionType", MOVE_LOST),
        ("moveProduceExamEffectIds", []),
        ("produceCardStatusEnchantId", ""),
        ("isInitial", True),
        ("isRestrict", False),
        ("noDeckDuplication", True),
        ("effectGroupIds", list(EXPECTED_CARD_GROUPS)),
        ("moveProduceExamTriggerIds", []),
    ):
        _require_raw(raw, key, expected, label)
    entries = _json_array(row["play_effects_json"], label)
    ordered: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise Plan2ReviewCountAddInterval3ContractError(
                f"{label}: effect ref {index} is not an object"
            )
        for key, expected in (
            ("produceExamTriggerId", ""),
            ("hideIcon", False),
            ("isOncePlayEffect", False),
        ):
            _require_raw(entry, key, expected, f"{label}:effect:{index}")
        effect_id = entry.get("produceExamEffectId")
        if not isinstance(effect_id, str) or not effect_id:
            raise Plan2ReviewCountAddInterval3ContractError(
                f"{label}: effect ref {index} has no ID"
            )
        ordered.append(effect_id)
    if tuple(ordered) != expected_effects:
        raise Plan2ReviewCountAddInterval3ContractError(
            f"{label}: ordered effects drifted"
        )
    return Plan2ReviewCountAddTarget(
        card_id=card_id,
        upgrade=upgrade,
        name=str(row["name"]),
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina=int(row["stamina"]),
        force_stamina=int(raw["forceStamina"]),
        cost_type=str(row["cost_type"]),
        cost_value=int(row["cost_value"]),
        play_trigger_id=str(row["play_trigger_id"]),
        move_position_type=str(row["move_position_type"]),
        ordered_effect_ids=tuple(ordered),
        effect_group_ids=tuple(raw["effectGroupIds"]),
    )


def _validate_trigger(row: sqlite3.Row) -> Plan2ReviewCountAddTrigger:
    expected_arrays = (
        ("phase_types_json", [PHASE_PLAY_COUNT_INTERVAL]),
        ("phase_values_json", [INTERVAL]),
        ("field_status_check_types_json", []),
        ("field_status_types_json", [FIELD_REVIEW_UP]),
        ("field_status_values_json", [REVIEW_THRESHOLD]),
        ("field_status_produce_card_search_ids_json", []),
        ("effect_types_json", []),
    )
    for column, expected in expected_arrays:
        if _json_array(row[column], f"{TRIGGER_ID}.{column}") != expected:
            raise Plan2ReviewCountAddInterval3ContractError(
                f"{TRIGGER_ID}: {column} drifted"
            )
    if (
        str(row["id"]) != TRIGGER_ID
        or str(row["produce_card_search_id"]) != TARGET_SEARCH_ID
        or int(row["upper_search_count"]) != 0
        or int(row["lower_search_count"]) != 0
        or str(row["card_move_position_type"]) != _UNKNOWN_MOVE
        or str(row["lesson_type"]) != _UNKNOWN_LESSON
    ):
        raise Plan2ReviewCountAddInterval3ContractError(
            "trigger normalized fields drifted"
        )
    raw = _json_object(row["raw_json"], TRIGGER_ID)
    for key, expected in (
        ("id", TRIGGER_ID),
        ("phaseTypes", [PHASE_PLAY_COUNT_INTERVAL]),
        ("phaseValues", [INTERVAL]),
        ("fieldStatusCheckTypes", []),
        ("fieldStatusTypes", [FIELD_REVIEW_UP]),
        ("fieldStatusValues", [REVIEW_THRESHOLD]),
        ("fieldStatusProduceCardSearchIds", []),
        ("produceCardSearchId", TARGET_SEARCH_ID),
        ("upperSearchCount", 0),
        ("lowerSearchCount", 0),
        ("cardMovePositionType", _UNKNOWN_MOVE),
        ("effectTypes", []),
        ("lessonType", _UNKNOWN_LESSON),
    ):
        _require_raw(raw, key, expected, TRIGGER_ID)
    return Plan2ReviewCountAddTrigger()


def _validate_search_raw(row: sqlite3.Row) -> None:
    raw = _json_object(row["raw_json"], TARGET_SEARCH_ID)
    for key, expected in (
        ("id", TARGET_SEARCH_ID),
        ("cardRarities", []),
        ("produceCardIds", []),
        ("upgradeCounts", []),
        ("planType", _UNKNOWN_PLAN),
        ("cardCategories", []),
        ("cardStatusType", _UNKNOWN_CARD_STATUS),
        ("orderType", _UNKNOWN_CARD_ORDER),
        ("cardPositionType", TARGET_POSITION),
        ("cardSearchTag", ""),
        ("produceCardRandomPoolId", ""),
        ("limitCount", 0),
        ("staminaMinMaxType", _UNKNOWN_MIN_MAX),
        ("staminaMin", 0),
        ("staminaMax", 0),
        ("examEffectType", _UNKNOWN_EXAM_EFFECT),
        ("effectGroupIds", [REVIEW_GROUP]),
        ("isSelf", False),
        ("produceCardPoolId", ""),
        ("costType", COST_UNKNOWN),
        ("isCustomized", False),
    ):
        _require_raw(raw, key, expected, TARGET_SEARCH_ID)


def load_plan2_review_count_add_interval3_program(
    database: Path = DEFAULT_DATABASE,
) -> Plan2ReviewCountAddInterval3Program:
    """Load and validate only the named current-Master rows."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        cards = connection.execute(
            "SELECT * FROM card WHERE id = ? ORDER BY upgrade_count", (CARD_ID,)
        ).fetchall()
        installer_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (INSTALLER_EFFECT_ID,)
        ).fetchone()
        status_row = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (STATUS_ENCHANT_ID,),
        ).fetchone()
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
        ).fetchone()
        search_row = connection.execute(
            "SELECT * FROM produce_card_search WHERE id = ?", (TARGET_SEARCH_ID,)
        ).fetchone()
        child_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (CHILD_EFFECT_ID,)
        ).fetchone()
        review_rows = connection.execute(
            "SELECT * FROM effect WHERE id IN (?,?) ORDER BY value1",
            ("e_effect-exam_review-0002", "e_effect-exam_review-0003"),
        ).fetchall()
        if (
            len(cards) != 4
            or installer_row is None
            or status_row is None
            or trigger_row is None
            or search_row is None
            or child_row is None
            or len(review_rows) != 2
        ):
            raise Plan2ReviewCountAddInterval3ContractError(
                "one or more exact Master rows are missing"
            )
        installer_effect = _validate_effect_controls(
            installer_row,
            expected_type=EFFECT_STATUS_ENCHANT,
            expected_values=(0, 0, 0, -1),
            status_id=STATUS_ENCHANT_ID,
            groups=EXPECTED_WRAPPER_GROUPS,
        )
        installer = Plan2ReviewCountAddInstaller(
            installer_effect.effect_id,
            installer_effect.effect_type,
            installer_effect.value1,
            installer_effect.value2,
            installer_effect.count,
            installer_effect.turn,
            STATUS_ENCHANT_ID,
            installer_effect.effect_group_ids,
        )
        status_raw = _json_object(status_row["raw_json"], STATUS_ENCHANT_ID)
        child_ids = _json_array(
            status_row["produce_exam_effect_ids_json"], STATUS_ENCHANT_ID
        )
        if (
            str(status_row["produce_exam_trigger_id"]) != TRIGGER_ID
            or child_ids != [CHILD_EFFECT_ID]
        ):
            raise Plan2ReviewCountAddInterval3ContractError(
                "status enchant graph drifted"
            )
        for key, expected in (
            ("id", STATUS_ENCHANT_ID),
            ("produceExamTriggerId", TRIGGER_ID),
            ("produceExamEffectIds", [CHILD_EFFECT_ID]),
        ):
            _require_raw(status_raw, key, expected, STATUS_ENCHANT_ID)
        trigger = _validate_trigger(trigger_row)
        _validate_search_raw(search_row)
        child = _validate_effect_controls(
            child_row,
            expected_type=EFFECT_REVIEW_COUNT_ADD,
            expected_values=(1, 0, 0, 3),
            status_id="",
            groups=EXPECTED_CHILD_GROUPS,
        )
        direct_reviews = tuple(
            _validate_effect_controls(
                row,
                expected_type=EFFECT_REVIEW,
                expected_values=(int(row["value1"]), 0, 0, 0),
                status_id="",
                groups=(REVIEW_GROUP,),
            )
            for row in review_rows
        )
        targets = tuple(_load_target(row) for row in cards)
    search = load_produce_card_search(TARGET_SEARCH_ID, database)
    return Plan2ReviewCountAddInterval3Program(
        targets=targets,
        installer=installer,
        trigger=trigger,
        search=search,
        child=child,
        direct_review_effects=direct_reviews,
    )


load_plan2_review_count_add_program = load_plan2_review_count_add_interval3_program


@dataclass(frozen=True, slots=True)
class Plan2ReviewTargetCard:
    """One immutable card identity used by the native Target search."""

    guid: str
    card_id: str
    upgrade: int
    effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.guid, str) or not self.guid.strip():
            raise ValueError("card GUID must be non-empty")
        if not isinstance(self.card_id, str) or not self.card_id:
            raise ValueError("card_id must be non-empty")
        if isinstance(self.upgrade, bool) or not isinstance(self.upgrade, int):
            raise TypeError("upgrade must be an integer")
        if self.upgrade < 0:
            raise ValueError("upgrade must be non-negative")
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(value, str) or not value for value in groups):
            raise ValueError("effect groups must be non-empty strings")
        object.__setattr__(self, "effect_group_ids", groups)


@dataclass(frozen=True, slots=True)
class Plan2ReviewTargetZones:
    """Complete ordered zone universe used only for GUID transaction safety."""

    hand: tuple[Plan2ReviewTargetCard, ...] = ()
    deck: tuple[Plan2ReviewTargetCard, ...] = ()
    grave: tuple[Plan2ReviewTargetCard, ...] = ()
    lost: tuple[Plan2ReviewTargetCard, ...] = ()
    hold: tuple[Plan2ReviewTargetCard, ...] = ()
    playing: tuple[Plan2ReviewTargetCard, ...] = ()
    future_decks: tuple[tuple[Plan2ReviewTargetCard, ...], ...] = ()
    past_decks: tuple[tuple[Plan2ReviewTargetCard, ...], ...] = ()

    def __post_init__(self) -> None:
        for name in ("hand", "deck", "grave", "lost", "hold", "playing"):
            values = tuple(getattr(self, name))
            if any(not isinstance(card, Plan2ReviewTargetCard) for card in values):
                raise TypeError(f"{name} must contain Plan2ReviewTargetCard")
            object.__setattr__(self, name, values)
        if len(self.playing) > 1:
            raise Plan2ReviewCountAddInterval3ContractError(
                "Playing may contain at most one card"
            )
        for name in ("future_decks", "past_decks"):
            services = tuple(tuple(service) for service in getattr(self, name))
            if any(
                not isinstance(card, Plan2ReviewTargetCard)
                for service in services
                for card in service
            ):
                raise TypeError(f"{name} must contain card services")
            object.__setattr__(self, name, services)
        guids = tuple(card.guid for card in self.ordered_cards)
        if len(guids) != len(set(guids)):
            raise Plan2ReviewCountAddInterval3ContractError(
                "card GUIDs must be unique across the complete zone universe"
            )

    @property
    def ordered_cards(self) -> tuple[Plan2ReviewTargetCard, ...]:
        return (
            *self.hand,
            *self.deck,
            *self.grave,
            *self.lost,
            *self.hold,
            *self.playing,
            *(card for service in self.future_decks for card in service),
            *(card for service in self.past_decks for card in service),
        )

    def find_guid(self, guid: str) -> tuple[str, Plan2ReviewTargetCard] | None:
        for name in ("hand", "deck", "grave", "lost", "hold", "playing"):
            for card in getattr(self, name):
                if card.guid == guid:
                    return name, card
        for name in ("future_decks", "past_decks"):
            for service_index, service in enumerate(getattr(self, name)):
                for card in service:
                    if card.guid == guid:
                        return f"{name}[{service_index}]", card
        return None


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddLayer:
    status_uid: int
    value: int
    turn: int
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.status_uid, bool) or not isinstance(self.status_uid, int):
            raise TypeError("status_uid must be an integer")
        if self.status_uid < 1:
            raise ValueError("status_uid must be positive")
        value = _plain_i32(self.value, "ReviewCountAdd value")
        if value < 0:
            raise ValueError("ReviewCountAdd value must be non-negative")
        turn = _plain_i32(self.turn, "ReviewCountAdd turn")
        if turn != PERMANENT_TURN and turn < 1:
            raise ValueError("ReviewCountAdd turn must be -1 or positive")
        if type(self.is_passing_turn_start) is not bool:
            raise TypeError("is_passing_turn_start must be boolean")


def _checked_layer_total(layers: Sequence[Plan2ReviewCountAddLayer]) -> int:
    total = 0
    for layer in layers:
        total += layer.value
        if total > INT32_MAX:
            raise Plan2ReviewCountAddInterval3ContractError(
                "GetReviewCountAdd Sum<Int32> overflow"
            )
    return total


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddInterval3State:
    zones: Plan2ReviewTargetZones = field(default_factory=Plan2ReviewTargetZones)
    active: tuple[Plan2EndTurnListener, ...] = ()
    phase_counts: tuple[Plan2PlayCountPhaseCounter, ...] = ()
    review_count_add_layers: tuple[Plan2ReviewCountAddLayer, ...] = ()
    plan2_state: Plan2State = field(default_factory=Plan2State)
    review_count_add_blocked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.zones, Plan2ReviewTargetZones):
            raise TypeError("zones must be Plan2ReviewTargetZones")
        if not isinstance(self.plan2_state, Plan2State):
            raise TypeError("plan2_state must be Plan2State")
        if type(self.review_count_add_blocked) is not bool:
            raise TypeError("review_count_add_blocked must be boolean")
        active = tuple(self.active)
        counters = tuple(self.phase_counts)
        layers = tuple(self.review_count_add_layers)
        if any(not isinstance(item, Plan2EndTurnListener) for item in active):
            raise TypeError("active must contain Plan2EndTurnListener")
        if any(not isinstance(item, Plan2PlayCountPhaseCounter) for item in counters):
            raise TypeError("phase_counts must contain Plan2PlayCountPhaseCounter")
        if any(not isinstance(item, Plan2ReviewCountAddLayer) for item in layers):
            raise TypeError("review_count_add_layers contain an invalid value")
        active_uids = tuple(item.status_uid for item in active)
        counter_uids = tuple(item.status_uid for item in counters)
        layer_uids = tuple(item.status_uid for item in layers)
        base_uids = {
            *(item.status_uid for item in self.plan2_state.review_multiple_layers),
            *(item.status_uid for item in self.plan2_state.end_turn_listeners),
            *(item.status_uid for item in self.plan2_state.card_play_listeners),
        }
        all_uids = (*active_uids, *layer_uids, *base_uids)
        if len(all_uids) != len(set(all_uids)):
            raise Plan2ReviewCountAddInterval3ContractError(
                "status UIDs must be unique across shared/local state"
            )
        if len(counter_uids) != len(set(counter_uids)):
            raise Plan2ReviewCountAddInterval3ContractError(
                "phase counter owner UIDs must be unique"
            )
        if set(counter_uids).difference(active_uids):
            raise Plan2ReviewCountAddInterval3ContractError(
                "phase counters must belong to active interval listeners"
            )
        total = _checked_layer_total(layers)
        if self.plan2_state.review_count_add != total:
            raise Plan2ReviewCountAddInterval3ContractError(
                "Plan2 review_count_add scalar must equal active layer sum"
            )
        if all_uids and self.plan2_state.next_status_uid <= max(all_uids):
            raise Plan2ReviewCountAddInterval3ContractError(
                "next_status_uid must be greater than every active UID"
            )
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "phase_counts", counters)
        object.__setattr__(self, "review_count_add_layers", layers)

    @property
    def review_count_add(self) -> int:
        return self.plan2_state.review_count_add


Plan2ReviewCountAddState = Plan2ReviewCountAddInterval3State


def _expected_listener(
    status_uid: int, program: Plan2ReviewCountAddInterval3Program
) -> Plan2EndTurnListener:
    return Plan2EndTurnListener(
        status_uid=status_uid,
        wrapper_effect_id=INSTALLER_EFFECT_ID,
        status_enchant_id=STATUS_ENCHANT_ID,
        trigger_id=TRIGGER_ID,
        effects=(program.child,),
        wrapper_effect_group_ids=EXPECTED_WRAPPER_GROUPS,
        turn=PERMANENT_TURN,
        limit_count=-1,
        limit_count_in_turn=-1,
        limit_count_in_turn_remaining=-1,
        turn_count=0,
        is_passing_turn_start=False,
    )


def _listener_errors(
    listener: Plan2EndTurnListener,
    program: Plan2ReviewCountAddInterval3Program,
) -> tuple[str, ...]:
    expected = _expected_listener(listener.status_uid, program)
    errors: list[str] = []
    for name in (
        "wrapper_effect_id",
        "status_enchant_id",
        "trigger_id",
        "effects",
        "wrapper_effect_group_ids",
        "turn",
        "limit_count",
        "limit_count_in_turn",
        "limit_count_in_turn_remaining",
    ):
        if getattr(listener, name) != getattr(expected, name):
            errors.append(f"listener:{listener.status_uid}:{name}")
    return tuple(errors)


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddInstallTransition:
    before: Plan2ReviewCountAddInterval3State
    after: Plan2ReviewCountAddInterval3State
    created_status_uid: int


def install_plan2_review_count_add_interval3_listener(
    state: Plan2ReviewCountAddInterval3State,
    program: Plan2ReviewCountAddInterval3Program,
) -> Plan2ReviewCountAddInstallTransition:
    if not isinstance(state, Plan2ReviewCountAddInterval3State):
        raise TypeError("state must be Plan2ReviewCountAddInterval3State")
    if not isinstance(program, Plan2ReviewCountAddInterval3Program):
        raise TypeError("program must be Plan2ReviewCountAddInterval3Program")
    uid = state.plan2_state.next_status_uid
    if uid >= INT32_MAX:
        raise OverflowError("status UID allocation reached the Int32 boundary")
    listener = _expected_listener(uid, program)
    after = replace(
        state,
        active=state.active + (listener,),
        plan2_state=replace(state.plan2_state, next_status_uid=uid + 1),
    )
    return Plan2ReviewCountAddInstallTransition(state, after, uid)


install_plan2_review_count_add_listener = (
    install_plan2_review_count_add_interval3_listener
)


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddSourceDirectTransition:
    before: Plan2ReviewCountAddInterval3State
    after: Plan2ReviewCountAddInterval3State
    target: Plan2ReviewCountAddTarget
    ordered_effect_ids: tuple[str, ...]
    installed_status_uid: int
    event_trace: tuple[str, ...]


Plan2DirectReviewStage: TypeAlias = Callable[
    [Plan2ReviewCountAddInterval3State, Plan2EndTurnEffect],
    Plan2ReviewCountAddInterval3State,
]


def apply_plan2_review_count_add_source_direct_effects(
    state: Plan2ReviewCountAddInterval3State,
    upgrade: int,
    program: Plan2ReviewCountAddInterval3Program,
    *,
    apply_review_effect: Plan2DirectReviewStage,
) -> Plan2ReviewCountAddSourceDirectTransition:
    """Execute the exact source-card direct rows in Master order.

    Review itself is delegated to the already-resolved Plan2 Review family;
    this leaf owns only the ordering and the listener installation.
    """

    if not callable(apply_review_effect):
        raise TypeError("apply_review_effect must be callable")
    target = program.target(upgrade)
    current = state
    trace: list[str] = []
    installed_uid = 0
    for index, effect_id in enumerate(target.ordered_effect_ids):
        trace.append(f"direct:{index}:{effect_id}")
        if effect_id == INSTALLER_EFFECT_ID:
            installed = install_plan2_review_count_add_interval3_listener(
                current, program
            )
            current = installed.after
            installed_uid = installed.created_status_uid
            continue
        effect = program.direct_review(effect_id)
        next_state = apply_review_effect(current, effect)
        if not isinstance(next_state, Plan2ReviewCountAddInterval3State):
            raise TypeError(
                "apply_review_effect must return Plan2ReviewCountAddInterval3State"
            )
        current = next_state
    return Plan2ReviewCountAddSourceDirectTransition(
        before=state,
        after=current,
        target=target,
        ordered_effect_ids=target.ordered_effect_ids,
        installed_status_uid=installed_uid,
        event_trace=tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class Plan2ReviewUp6Snapshot:
    current_review: int
    admitted: bool
    stage: str = "post-payment-pre-listener-child-pre-direct"

    def __post_init__(self) -> None:
        value = _plain_i32(self.current_review, "current_review")
        if type(self.admitted) is not bool or self.admitted != (
            value >= REVIEW_THRESHOLD
        ):
            raise Plan2ReviewCountAddInterval3ContractError(
                "ReviewUp6 snapshot is inconsistent"
            )
        if self.stage != "post-payment-pre-listener-child-pre-direct":
            raise Plan2ReviewCountAddInterval3ContractError(
                "ReviewUp6 snapshot stage drifted"
            )


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddCapture:
    before: Plan2ReviewCountAddInterval3State
    after_count_spend: Plan2ReviewCountAddInterval3State
    event: AcceptedPlay
    target_snapshot: Plan2ReviewTargetCard | None
    target_matched: bool
    unresolved: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved


def match_plan2_review_target(
    search: ProduceCardSearchRule, card: Plan2ReviewTargetCard
) -> tuple[bool, str | None]:
    errors = _search_errors(search)
    if errors:
        return False, errors[0]
    if not isinstance(card, Plan2ReviewTargetCard):
        return False, "target-card-type"
    return REVIEW_GROUP in card.effect_group_ids, None


def capture_plan2_review_count_add_interval3(
    state: Plan2ReviewCountAddInterval3State,
    event: AcceptedPlay,
    program: Plan2ReviewCountAddInterval3Program,
) -> Plan2ReviewCountAddCapture:
    """Capture the native Target object and propose matching phase increments."""

    if not isinstance(state, Plan2ReviewCountAddInterval3State):
        raise TypeError("state must be Plan2ReviewCountAddInterval3State")
    if not isinstance(event, AcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    if not isinstance(program, Plan2ReviewCountAddInterval3Program):
        raise TypeError("program must be Plan2ReviewCountAddInterval3Program")
    if not event.accepted:
        return Plan2ReviewCountAddCapture(
            state,
            state,
            event,
            None,
            False,
            event_trace=("rejected-before-set-playing-card",),
        )
    errors: list[str] = []
    if not event.playing_card_present:
        errors.append("accepted-play-has-no-playing-card")
    if not isinstance(event.card, Plan2ReviewTargetCard):
        errors.append("accepted-play-target-snapshot-type")
        snapshot = None
    else:
        snapshot = event.card
    if snapshot is not None:
        current = state.zones.find_guid(snapshot.guid)
        if current is None or current[0] != "playing":
            errors.append("set-playing-card-guid-not-in-Playing")
        elif current[1] != snapshot:
            errors.append("set-playing-card-snapshot-identity-drift")
    errors.extend(
        error
        for listener in state.active
        for error in _listener_errors(listener, program)
    )
    if errors or snapshot is None:
        return Plan2ReviewCountAddCapture(
            state,
            state,
            event,
            snapshot,
            False,
            unresolved=_unique(errors),
        )
    matched, reason = match_plan2_review_target(program.search, snapshot)
    trace = [
        f"set-playing-card:{snapshot.guid}",
        f"target-snapshot:{snapshot.guid}",
        "target-search:single-played-card",
    ]
    if reason is not None:
        return Plan2ReviewCountAddCapture(
            state,
            state,
            event,
            snapshot,
            False,
            unresolved=(reason,),
            event_trace=tuple(trace),
        )
    if not matched:
        trace.append("target-search:no-review-group:no-count")
        return Plan2ReviewCountAddCapture(
            state,
            state,
            event,
            snapshot,
            False,
            event_trace=tuple(trace),
        )
    old_counts = {item.status_uid: item.count for item in state.phase_counts}
    counters: list[Plan2PlayCountPhaseCounter] = []
    for listener in state.active:
        before = old_counts.get(listener.status_uid, 0)
        if before == INT32_MAX:
            return Plan2ReviewCountAddCapture(
                state,
                state,
                event,
                snapshot,
                True,
                unresolved=(f"phase-count-overflow:{listener.status_uid}",),
                event_trace=tuple(trace),
            )
        after = before + 1
        counters.append(Plan2PlayCountPhaseCounter(listener.status_uid, after))
        trace.append(f"listener:{listener.status_uid}:phase23:{before}->{after}")
    proposed = replace(state, phase_counts=tuple(counters)) if state.active else state
    return Plan2ReviewCountAddCapture(
        state,
        proposed,
        event,
        snapshot,
        True,
        event_trace=tuple(trace),
    )


Plan2ReviewIntervalStage: TypeAlias = Callable[
    [Plan2ReviewCountAddInterval3State], Plan2ReviewCountAddInterval3State
]


def _identity_stage(
    state: Plan2ReviewCountAddInterval3State,
) -> Plan2ReviewCountAddInterval3State:
    return state


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddFire:
    listener_sequence: int
    status_uid: int
    count_before: int
    count_after: int
    child_effect_id: str
    child_applied: bool
    child_blocked: bool
    review_count_add_before: int
    review_count_add_after: int
    created_status_uid: int | None = None
    merged_status_uid: int | None = None


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddExecution:
    before: Plan2ReviewCountAddInterval3State
    after: Plan2ReviewCountAddInterval3State
    capture: Plan2ReviewCountAddCapture
    after_payment: Plan2ReviewCountAddInterval3State | None = None
    after_listeners: Plan2ReviewCountAddInterval3State | None = None
    after_direct_effects: Plan2ReviewCountAddInterval3State | None = None
    review_gate: Plan2ReviewUp6Snapshot | None = None
    rematched_zone: str | None = None
    fires: tuple[Plan2ReviewCountAddFire, ...] = ()
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = NATIVE_TRANSACTION_ORDER
    event_trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def state_unchanged(self) -> bool:
        return self.before == self.after


def _sync_review_count_add(
    state: Plan2ReviewCountAddInterval3State,
    layers: Sequence[Plan2ReviewCountAddLayer],
    *,
    next_status_uid: int | None = None,
) -> Plan2ReviewCountAddInterval3State:
    values = tuple(layers)
    total = _checked_layer_total(values)
    plan2 = replace(
        state.plan2_state,
        review_count_add=total,
        next_status_uid=(
            state.plan2_state.next_status_uid
            if next_status_uid is None
            else next_status_uid
        ),
    )
    return replace(state, review_count_add_layers=values, plan2_state=plan2)


def _apply_review_count_add_child(
    state: Plan2ReviewCountAddInterval3State,
    program: Plan2ReviewCountAddInterval3Program,
) -> tuple[
    Plan2ReviewCountAddInterval3State,
    bool,
    int | None,
    int | None,
]:
    if state.review_count_add_blocked:
        return state, False, None, None
    layers = list(state.review_count_add_layers)
    match_index = next(
        (
            index
            for index in range(len(layers) - 1, -1, -1)
            if layers[index].turn == program.child.turn
        ),
        None,
    )
    if match_index is not None:
        layer = layers[match_index]
        merged = max(0, _i32(layer.value + program.child.value1))
        layers[match_index] = replace(layer, value=merged)
        return _sync_review_count_add(state, layers), True, None, layer.status_uid
    uid = state.plan2_state.next_status_uid
    if uid >= INT32_MAX:
        raise OverflowError("ReviewCountAdd status UID allocation overflow")
    layers.append(
        Plan2ReviewCountAddLayer(
            status_uid=uid,
            value=program.child.value1,
            turn=program.child.turn,
            is_passing_turn_start=False,
        )
    )
    return _sync_review_count_add(state, layers, next_status_uid=uid + 1), True, uid, None


def _validate_payment_handoff(
    expected: Plan2ReviewCountAddInterval3State,
    observed: Plan2ReviewCountAddInterval3State,
) -> str | None:
    if expected.active != observed.active:
        return "payment-mutated-interval-listeners"
    if expected.phase_counts != observed.phase_counts:
        return "payment-mutated-phase-counts"
    if expected.review_count_add_layers != observed.review_count_add_layers:
        return "payment-mutated-review-count-add-layers"
    if expected.plan2_state.next_status_uid != observed.plan2_state.next_status_uid:
        return "payment-mutated-status-uid-sequence"
    return None


def evaluate_plan2_review_count_add_interval3(
    state: Plan2ReviewCountAddInterval3State,
    event: AcceptedPlay,
    program: Plan2ReviewCountAddInterval3Program,
    *,
    pay_cost: Plan2ReviewIntervalStage = _identity_stage,
    apply_direct_effects: Plan2ReviewIntervalStage = _identity_stage,
    settle_final_move: Plan2ReviewIntervalStage = _identity_stage,
) -> Plan2ReviewCountAddExecution:
    """Resolve one accepted normal/forced/extra play atomically."""

    capture = capture_plan2_review_count_add_interval3(state, event, program)
    if not capture.executable or not event.accepted:
        return Plan2ReviewCountAddExecution(
            state,
            state,
            capture,
            unresolved=capture.unresolved,
            event_trace=capture.event_trace,
        )
    after_payment = pay_cost(capture.after_count_spend)
    if not isinstance(after_payment, Plan2ReviewCountAddInterval3State):
        raise TypeError("pay_cost must return Plan2ReviewCountAddInterval3State")
    trace = [*capture.event_trace, "pay-cost"]
    handoff_error = _validate_payment_handoff(capture.after_count_spend, after_payment)
    if handoff_error is not None:
        return Plan2ReviewCountAddExecution(
            state,
            state,
            capture,
            after_payment=after_payment,
            unresolved=(handoff_error,),
            event_trace=tuple(trace),
        )
    assert capture.target_snapshot is not None
    rematch = after_payment.zones.find_guid(capture.target_snapshot.guid)
    if rematch is None:
        return Plan2ReviewCountAddExecution(
            state,
            state,
            capture,
            after_payment=after_payment,
            unresolved=("target-guid-missing-at-execution",),
            event_trace=tuple((*trace, "target-guid-rematch:missing")),
        )
    rematched_zone, rematched_card = rematch
    if rematched_card != capture.target_snapshot:
        return Plan2ReviewCountAddExecution(
            state,
            state,
            capture,
            after_payment=after_payment,
            unresolved=("target-guid-identity-drift-at-execution",),
            event_trace=tuple((*trace, "target-guid-rematch:identity-drift")),
        )
    trace.append(f"target-guid-rematch:{rematched_zone}")
    gate = Plan2ReviewUp6Snapshot(
        current_review=after_payment.plan2_state.review,
        admitted=after_payment.plan2_state.review >= REVIEW_THRESHOLD,
    )
    trace.append(
        f"ReviewUp6:{gate.current_review}:admitted={str(gate.admitted).lower()}"
    )
    before_counts = {item.status_uid: item.count for item in state.phase_counts}
    after_counts = {
        item.status_uid: item.count for item in capture.after_count_spend.phase_counts
    }
    current = after_payment
    fires: list[Plan2ReviewCountAddFire] = []
    try:
        if capture.target_matched and gate.admitted:
            for sequence, listener in enumerate(state.active):
                count_before = before_counts.get(listener.status_uid, 0)
                count_after = after_counts[listener.status_uid]
                if count_after % INTERVAL != 0:
                    continue
                total_before = current.review_count_add
                current, applied, created_uid, merged_uid = (
                    _apply_review_count_add_child(current, program)
                )
                trace.append(
                    f"listener:{listener.status_uid}:child:{CHILD_EFFECT_ID}:"
                    f"{'applied' if applied else 'blocked'}"
                )
                fires.append(
                    Plan2ReviewCountAddFire(
                        listener_sequence=sequence,
                        status_uid=listener.status_uid,
                        count_before=count_before,
                        count_after=count_after,
                        child_effect_id=CHILD_EFFECT_ID,
                        child_applied=applied,
                        child_blocked=not applied,
                        review_count_add_before=total_before,
                        review_count_add_after=current.review_count_add,
                        created_status_uid=created_uid,
                        merged_status_uid=merged_uid,
                    )
                )
    except (OverflowError, Plan2ReviewCountAddInterval3ContractError) as error:
        return Plan2ReviewCountAddExecution(
            state,
            state,
            capture,
            after_payment=after_payment,
            review_gate=gate,
            rematched_zone=rematched_zone,
            unresolved=(f"child-failed-closed:{error}",),
            event_trace=tuple(trace),
        )
    after_listeners = current
    trace.append("ordered-direct-card-effects")
    after_direct = apply_direct_effects(after_listeners)
    if not isinstance(after_direct, Plan2ReviewCountAddInterval3State):
        raise TypeError(
            "apply_direct_effects must return Plan2ReviewCountAddInterval3State"
        )
    trace.append("final-zone-move")
    after = settle_final_move(after_direct)
    if not isinstance(after, Plan2ReviewCountAddInterval3State):
        raise TypeError(
            "settle_final_move must return Plan2ReviewCountAddInterval3State"
        )
    trace.append("card-play-after-and-interval-after-listeners")
    return Plan2ReviewCountAddExecution(
        before=state,
        after=after,
        capture=capture,
        after_payment=after_payment,
        after_listeners=after_listeners,
        after_direct_effects=after_direct,
        review_gate=gate,
        rematched_zone=rematched_zone,
        fires=tuple(fires),
        event_trace=tuple(trace),
    )


execute_plan2_review_count_add_interval3 = evaluate_plan2_review_count_add_interval3


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddTurnStartTransition:
    before: Plan2ReviewCountAddInterval3State
    after: Plan2ReviewCountAddInterval3State
    fresh_status_uids: tuple[int, ...]
    spent_status_uids: tuple[int, ...]
    expired_status_uids: tuple[int, ...]
    permanent_listener_uids: tuple[int, ...]


def simulate_plan2_review_count_add_interval3_turn_start(
    state: Plan2ReviewCountAddInterval3State,
    program: Plan2ReviewCountAddInterval3Program,
) -> Plan2ReviewCountAddTurnStartTransition:
    """Advance one TurnStart; phase-23 counters persist for the whole exam."""

    if state.plan2_state.current_turn >= INT32_MAX:
        raise OverflowError("current_turn reached the Int32 boundary")
    listener_errors = [
        error
        for listener in state.active
        for error in _listener_errors(listener, program)
    ]
    if listener_errors:
        raise Plan2ReviewCountAddInterval3ContractError(str(listener_errors))
    for listener in state.active:
        if listener.turn_count == INT32_MAX:
            raise OverflowError(
                f"listener turn_count overflow: {listener.status_uid}"
            )
    base_transition = simulate_plan2_turn_start(state.plan2_state)
    listeners = tuple(
        replace(
            listener,
            turn_count=listener.turn_count + 1,
            limit_count_in_turn_remaining=listener.limit_count_in_turn,
            is_passing_turn_start=True,
        )
        for listener in state.active
    )
    layers: list[Plan2ReviewCountAddLayer] = []
    fresh: list[int] = []
    spent: list[int] = []
    expired: list[int] = []
    for layer in state.review_count_add_layers:
        if layer.turn == PERMANENT_TURN:
            layers.append(replace(layer, is_passing_turn_start=True))
            continue
        if not layer.is_passing_turn_start:
            fresh.append(layer.status_uid)
            layers.append(replace(layer, is_passing_turn_start=True))
            continue
        spent.append(layer.status_uid)
        remaining = layer.turn - 1
        if remaining <= 0:
            expired.append(layer.status_uid)
        else:
            layers.append(replace(layer, turn=remaining))
    # Publish the local layer list and its shared scalar projection together;
    # constructing an intermediate mismatched immutable state would violate
    # the state invariant before ``_sync_review_count_add`` could repair it.
    total = _checked_layer_total(layers)
    base_state = replace(base_transition.after, review_count_add=total)
    after = replace(
        state,
        active=listeners,
        review_count_add_layers=tuple(layers),
        plan2_state=base_state,
    )
    return Plan2ReviewCountAddTurnStartTransition(
        before=state,
        after=after,
        fresh_status_uids=tuple(fresh),
        spent_status_uids=tuple(spent),
        expired_status_uids=tuple(expired),
        permanent_listener_uids=tuple(listener.status_uid for listener in listeners),
    )


simulate_plan2_review_count_add_turn_start = (
    simulate_plan2_review_count_add_interval3_turn_start
)


@dataclass(frozen=True, slots=True)
class Plan2ReviewCountAddTurnCheckTransition:
    before: Plan2ReviewCountAddInterval3State
    after: Plan2ReviewCountAddInterval3State
    score: Plan2ReviewScoreTransition


def simulate_plan2_review_count_add_turn_check(
    state: Plan2ReviewCountAddInterval3State,
    *,
    apply_score: Plan2ScoreApplicator | None = None,
) -> Plan2ReviewCountAddTurnCheckTransition:
    """Reuse the proven per-occurrence Review scoring implementation.

    Native snapshots Review, ReviewMultiple, and ReviewCountAdd once, computes
    one raw score, then emits ``ReviewCountAdd + 1`` independent normal score
    effects.  It does not multiply or replay a card's direct ExamReview row.
    """

    # The native loop has an unsigned guard at Android 0x7F05EA8..0x7F05EB4:
    # ReviewCountAdd values above Int32.MaxValue - 1 skip the loop before the
    # unchecked ``countAdd + 1`` at 0x7F05EE0.  The state contract makes
    # Int32.MaxValue the only admitted value on that branch.  Handle it here
    # instead of asking the shared helper to construct an impractically large
    # Python range.
    if state.review_count_add == INT32_MAX:
        score = Plan2ReviewScoreTransition(
            before=state.plan2_state,
            after=state.plan2_state,
            review_value=state.plan2_state.review,
            review_multiple=state.plan2_state.get_review_multiple(),
            review_count_add=state.review_count_add,
            raw_score_per_occurrence=state.plan2_state.get_review_raw_score(),
            occurrences=(),
        )
    elif apply_score is None:
        score = simulate_end_turn_review_score(state.plan2_state)
    else:
        score = simulate_end_turn_review_score(
            state.plan2_state, apply_score=apply_score
        )
    return Plan2ReviewCountAddTurnCheckTransition(
        before=state,
        after=replace(state, plan2_state=score.after),
        score=score,
    )


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "CARD_ID",
    "CARD_NAME",
    "CARD_STAMINA_BY_UPGRADE",
    "CARD_UPGRADES",
    "CHILD_EFFECT_ID",
    "CHILD_TURN",
    "CHILD_VALUE",
    "COMBINED_FAMILY_ACCOUNTING",
    "EFFECT_FAMILY_ACCOUNTING",
    "EXPECTED_COMBINED_BATCH_DELTA",
    "INSTALLER_EFFECT_ID",
    "INTERVAL",
    "NATIVE_TRANSACTION_ORDER",
    "PHASE_SEMANTIC_NUMBER",
    "Plan2DirectReviewStage",
    "Plan2ReviewCountAddCapture",
    "Plan2ReviewCountAddExecution",
    "Plan2ReviewCountAddFamilyAccounting",
    "Plan2ReviewCountAddFire",
    "Plan2ReviewCountAddInstallTransition",
    "Plan2ReviewCountAddInstaller",
    "Plan2ReviewCountAddInterval3ContractError",
    "Plan2ReviewCountAddInterval3Program",
    "Plan2ReviewCountAddInterval3State",
    "Plan2ReviewCountAddLayer",
    "Plan2ReviewCountAddSourceDirectTransition",
    "Plan2ReviewCountAddState",
    "Plan2ReviewCountAddTarget",
    "Plan2ReviewCountAddTrigger",
    "Plan2ReviewCountAddTurnCheckTransition",
    "Plan2ReviewCountAddTurnStartTransition",
    "Plan2ReviewTargetCard",
    "Plan2ReviewTargetZones",
    "Plan2ReviewUp6Snapshot",
    "PlaySource",
    "REVIEW_GROUP",
    "REVIEW_THRESHOLD",
    "STATUS_ENCHANT_ID",
    "TARGET_SEARCH_ID",
    "TRIGGER_FAMILY_ACCOUNTING",
    "TRIGGER_ID",
    "AcceptedPlay",
    "apply_plan2_review_count_add_source_direct_effects",
    "capture_plan2_review_count_add_interval3",
    "evaluate_plan2_review_count_add_interval3",
    "execute_plan2_review_count_add_interval3",
    "install_plan2_review_count_add_interval3_listener",
    "install_plan2_review_count_add_listener",
    "load_plan2_review_count_add_interval3_program",
    "load_plan2_review_count_add_program",
    "match_plan2_review_target",
    "simulate_plan2_review_count_add_interval3_turn_start",
    "simulate_plan2_review_count_add_turn_check",
    "simulate_plan2_review_count_add_turn_start",
]
