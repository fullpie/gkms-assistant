"""Exact Plan2 ``p_card_search-playing`` status-trigger leaf.

This module is deliberately narrower than :mod:`plan2_card_play_trigger`.
It resolves only the four ``p_card-02-act-3_050`` card versions and the two
exact status-enchant wrappers that point at them.  The native transaction is
projected as one immutable boundary:

``SetPlayingCard -> Playing snapshot/capture -> payment -> child effects ->
direct card effects -> physical move``.

The module reuses the shared search, GUID identity, ordered-zone, listener,
and turn-start primitives.  It does not add a second Plan2State or modify the
central coverage/runtime layers.  Unproved trigger, wrapper, child, identity,
and search shapes raise ``Plan2CardPlayPlayingSearchContractError``.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from .card_search import (
    ProduceCardSearchRule,
    exact_playing_card_search_mismatches,
    load_produce_card_search,
    match_exact_playing_card_search,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    NativeFormulaDomainError,
    get_ratio_effect_int_value,
)
from .plan2_card_play_trigger import Plan2PlayingCard
from .plan2_state import (
    PERMANENT_TURN,
    Plan2CardPlayEffect,
    Plan2CardPlayListener,
    Plan2State,
    simulate_plan2_turn_start,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState
from .playing_card_identity import resolve_playing_card_identity


STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
CARD_PLAY_PHASE_TYPE = "ProduceExamPhaseType_ExamCardPlay"
CARD_PLAY_TRIGGER_ID = "e_trigger-exam_card_play-p_card_search-playing-0_1"
PLAYING_SEARCH_ID = "p_card_search-playing"
CARD_ID = "p_card-02-act-3_050"
CARD_UPGRADES = (0, 1, 2, 3)

LESSON_DEPEND_REVIEW_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
LESSON_DEPEND_REVIEW_GROUP = (
    "effect_group-visible-exam_lesson_depend_exam_review-000"
)
LESSON_GROUP = "effect_group-visible-exam_lesson-000"
STATUS_ENCHANT_GROUP = "effect_group-visible-exam_status_enchant-000"

WRAPPER_ENC01 = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-act-3_050-enc01"
)
WRAPPER_ENC02 = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-act-3_050-enc02"
)
WRAPPER_ENC03 = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-act-3_050-enc03"
)
STATUS_ENC01 = "enchant-p_card-02-act-3_050-enc01"
STATUS_ENC02 = "enchant-p_card-02-act-3_050-enc02"

CHILD_0300 = "e_effect-exam_lesson_depend_exam_review-0300-01"
CHILD_0500 = "e_effect-exam_lesson_depend_exam_review-0500-01"

ITEM_ALIAS_STATUS_IDS = (
    "enchant-p_item_effect_00-0-004-0-010-enc01",
    "enchant-pitem_00-0-004-0-010-enc01",
)
ITEM_ALIAS_ITEM_ID = "pitem_00-0-004-0-010"

UPPER_SEARCH_COUNT_NO_CAP = 0
LOWER_SEARCH_COUNT_INCLUSIVE = 1

EXPECTED_WRAPPER_GROUPS = (
    LESSON_DEPEND_REVIEW_GROUP,
    LESSON_GROUP,
    STATUS_ENCHANT_GROUP,
)
EXPECTED_CHILD_GROUPS = (LESSON_DEPEND_REVIEW_GROUP, LESSON_GROUP)
EXPECTED_CARD_EFFECTS = {
    0: ("e_effect-exam_playable_value_add-01", WRAPPER_ENC01),
    1: ("e_effect-exam_playable_value_add-01", WRAPPER_ENC02),
    2: ("e_effect-exam_playable_value_add-01", WRAPPER_ENC02),
    3: (
        "e_effect-exam_block-0001",
        "e_effect-exam_playable_value_add-01",
        WRAPPER_ENC02,
    ),
}
EXPECTED_CARD_GROUPS = {
    0: (
        LESSON_DEPEND_REVIEW_GROUP,
        LESSON_GROUP,
        STATUS_ENCHANT_GROUP,
        "effect_group-visible-exam_playable_value_add-000",
    ),
    1: (
        LESSON_DEPEND_REVIEW_GROUP,
        LESSON_GROUP,
        STATUS_ENCHANT_GROUP,
        "effect_group-visible-exam_playable_value_add-000",
    ),
    2: (
        LESSON_DEPEND_REVIEW_GROUP,
        LESSON_GROUP,
        STATUS_ENCHANT_GROUP,
        "effect_group-visible-exam_playable_value_add-000",
    ),
    3: (
        LESSON_DEPEND_REVIEW_GROUP,
        LESSON_GROUP,
        "effect_group-visible-exam_block-000",
        STATUS_ENCHANT_GROUP,
        "effect_group-visible-exam_playable_value_add-000",
    ),
}


class PlaySource(str, Enum):
    """Accepted native entry sources that can still reach CardPlay capture."""

    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


class Plan2CardPlayPlayingSearchContractError(ValueError):
    """Master/native evidence is outside this exact standalone leaf."""


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{label} must be an integer"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{label} is outside Int32"
        )
    return value


def _checked_i32(value: int, label: str) -> int:
    value = _plain_i32(value, label)
    return value


def _json_object(value: object, row_id: str) -> dict[str, object]:
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{row_id}: invalid raw_json"
        ) from error
    if not isinstance(raw, dict):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{row_id}: raw_json must be an object"
        )
    return raw


def _json_array(value: object, label: str) -> list[object]:
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{label}: invalid JSON array"
        ) from error
    if not isinstance(raw, list):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{label} must be an array"
        )
    return raw


def _require_equal(
    raw: dict[str, object], key: str, expected: object, row_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{row_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


def _ordered_groups(raw: dict[str, object], row_id: str) -> tuple[str, ...]:
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or any(
        not isinstance(group_id, str) or not group_id for group_id in groups
    ):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{row_id}: effectGroupIds must be an ordered string array"
        )
    return tuple(groups)


def _validate_effect_controls(
    row: sqlite3.Row, raw: dict[str, object], *, status_id: str
) -> None:
    effect_id = str(row["id"])
    expected = (
        ("id", effect_id),
        ("effectType", str(row["effect_type"])),
        ("effectValue1", int(row["value1"])),
        ("effectValue2", int(row["value2"])),
        ("effectCount", int(row["effect_count"])),
        ("effectTurn", int(row["effect_turn"])),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        ("produceCardSearchId", ""),
        ("movePositionType", "ProduceCardMovePositionType_Unknown"),
        ("pickRangeType", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", "ProducePickCountType_Unknown"),
        ("pickCountMin", 0),
        ("pickCountMax", 0),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", "ProducePickCountType_Unknown"),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", status_id),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
    )
    for key, value in expected:
        _require_equal(raw, key, value, effect_id)


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchProgram:
    """One exact accepted StatusEnchant -> trigger -> child program."""

    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    search: ProduceCardSearchRule
    child: Plan2CardPlayEffect
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int = PERMANENT_TURN
    effect_count: int = 0
    effect_value1: int = 0

    def __post_init__(self) -> None:
        if self.wrapper_effect_id not in {WRAPPER_ENC01, WRAPPER_ENC02}:
            raise Plan2CardPlayPlayingSearchContractError(
                "unsupported exact wrapper"
            )
        if not self.status_enchant_id:
            raise Plan2CardPlayPlayingSearchContractError(
                "status_enchant_id must be non-empty"
            )
        expected_status_id = {
            WRAPPER_ENC01: STATUS_ENC01,
            WRAPPER_ENC02: STATUS_ENC02,
        }[self.wrapper_effect_id]
        if self.status_enchant_id != expected_status_id:
            raise Plan2CardPlayPlayingSearchContractError(
                "wrapper/status pairing is not exact"
            )
        if self.trigger_id != CARD_PLAY_TRIGGER_ID:
            raise Plan2CardPlayPlayingSearchContractError(
                "unsupported exact trigger"
            )
        if self.search.id != PLAYING_SEARCH_ID:
            raise Plan2CardPlayPlayingSearchContractError(
                "unsupported exact Playing search"
            )
        mismatches = exact_playing_card_search_mismatches(
            self.search, expected_categories=(), expected_effect_group_ids=()
        )
        if mismatches:
            raise Plan2CardPlayPlayingSearchContractError(
                f"unsupported Playing search field: {mismatches[0]}"
            )
        if not isinstance(self.child, Plan2CardPlayEffect):
            raise TypeError("child must be Plan2CardPlayEffect")
        if (
            self.child.effect_type != LESSON_DEPEND_REVIEW_EFFECT_TYPE
            or self.child.value2 != 0
            or self.child.count != 1
            or self.child.turn != 0
            or self.child.effect_group_ids != EXPECTED_CHILD_GROUPS
        ):
            raise Plan2CardPlayPlayingSearchContractError(
                "unsupported Review-dependent lesson child"
            )
        expected_child_id = {
            WRAPPER_ENC01: CHILD_0300,
            WRAPPER_ENC02: CHILD_0500,
        }[self.wrapper_effect_id]
        if self.child.effect_id != expected_child_id:
            raise Plan2CardPlayPlayingSearchContractError(
                "wrapper/child pairing is not exact"
            )
        expected_value1 = {WRAPPER_ENC01: 300, WRAPPER_ENC02: 500}[
            self.wrapper_effect_id
        ]
        if self.child.value1 != expected_value1:
            raise Plan2CardPlayPlayingSearchContractError(
                "wrapper/Review-dependent lesson ratio pairing changed"
            )
        if self.wrapper_effect_group_ids != EXPECTED_WRAPPER_GROUPS:
            raise Plan2CardPlayPlayingSearchContractError(
                "wrapper effect-group order is not exact"
            )
        if (
            _plain_i32(self.turn, "program.turn") != PERMANENT_TURN
            or _plain_i32(self.effect_count, "program.effect_count") != 0
            or _plain_i32(self.effect_value1, "program.effect_value1") != 0
        ):
            raise Plan2CardPlayPlayingSearchContractError(
                "wrapper lifetime/count is not permanent and unlimited"
            )

    @property
    def native_limit_count(self) -> int:
        return -1

    @property
    def native_limit_count_in_turn(self) -> int:
        return -1


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchTarget:
    """One direct target card version and its ordered play-effect slot."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    ordered_effect_ids: tuple[str, ...]
    effect_slot: int
    wrapper_effect_id: str
    card_effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.card_id != CARD_ID or self.upgrade not in CARD_UPGRADES:
            raise Plan2CardPlayPlayingSearchContractError(
                "target is outside p_card-02-act-3_050 upgrade0..3"
            )
        if self.plan_type != "ProducePlanType_Plan2":
            raise Plan2CardPlayPlayingSearchContractError(
                "target plan type is not ActiveSkill"
            )
        if self.category != "ProduceCardCategory_ActiveSkill":
            raise Plan2CardPlayPlayingSearchContractError(
                "target category is not ActiveSkill"
            )
        if self.wrapper_effect_id not in {WRAPPER_ENC01, WRAPPER_ENC02}:
            raise Plan2CardPlayPlayingSearchContractError(
                "target wrapper is not exact"
            )
        if tuple(self.ordered_effect_ids) != EXPECTED_CARD_EFFECTS[self.upgrade]:
            raise Plan2CardPlayPlayingSearchContractError(
                "target ordered play effects are not exact"
            )
        if tuple(self.card_effect_group_ids) != EXPECTED_CARD_GROUPS[self.upgrade]:
            raise Plan2CardPlayPlayingSearchContractError(
                "target card effect-group order is not exact"
            )
        if not 0 <= self.effect_slot < len(self.ordered_effect_ids):
            raise Plan2CardPlayPlayingSearchContractError(
                "target effect slot is outside ordered effects"
            )
        if self.ordered_effect_ids[self.effect_slot] != self.wrapper_effect_id:
            raise Plan2CardPlayPlayingSearchContractError(
                "target wrapper is not at its ordered direct slot"
            )


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchCatalogEntry:
    identifier: str
    kind: str
    reason: str
    related_identifier: str = ""


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchContract:
    """Resolved exact Master contract plus explicitly rejected catalog rows."""

    trigger_id: str
    phase_type: str
    search: ProduceCardSearchRule
    programs: tuple[Plan2PlayingSearchProgram, ...]
    targets: tuple[Plan2PlayingSearchTarget, ...]
    catalog_rejections: tuple[Plan2PlayingSearchCatalogEntry, ...]

    def __post_init__(self) -> None:
        if self.trigger_id != CARD_PLAY_TRIGGER_ID:
            raise Plan2CardPlayPlayingSearchContractError(
                "contract trigger is not exact"
            )
        if self.phase_type != CARD_PLAY_PHASE_TYPE:
            raise Plan2CardPlayPlayingSearchContractError(
                "contract phase is not ExamCardPlay"
            )
        if self.search.id != PLAYING_SEARCH_ID:
            raise Plan2CardPlayPlayingSearchContractError(
                "contract search is not p_card_search-playing"
            )
        programs = tuple(self.programs)
        targets = tuple(self.targets)
        if tuple(program.wrapper_effect_id for program in programs) != (
            WRAPPER_ENC01,
            WRAPPER_ENC02,
        ):
            raise Plan2CardPlayPlayingSearchContractError(
                "accepted wrapper order is not enc01, enc02"
            )
        if tuple(target.upgrade for target in targets) != CARD_UPGRADES:
            raise Plan2CardPlayPlayingSearchContractError(
                "target versions are not upgrade0..3"
            )
        object.__setattr__(self, "programs", programs)
        object.__setattr__(self, "targets", targets)
        object.__setattr__(
            self, "catalog_rejections", tuple(self.catalog_rejections)
        )

    @property
    def affected_version_count(self) -> int:
        return len(self.targets)

    @property
    def direct_version_count(self) -> int:
        return sum(target.wrapper_effect_id in {WRAPPER_ENC01, WRAPPER_ENC02}
                   for target in self.targets)

    @property
    def co_blocked_version_count(self) -> int:
        """Target versions with this exact trigger blocked by another gap."""

        return self.affected_version_count - self.direct_version_count

    @property
    def affected_direct(self) -> tuple[int, int]:
        return self.affected_version_count, self.direct_version_count

    def program_for_wrapper(self, wrapper_effect_id: str) -> Plan2PlayingSearchProgram:
        for program in self.programs:
            if program.wrapper_effect_id == wrapper_effect_id:
                return program
        raise Plan2CardPlayPlayingSearchContractError(
            f"wrapper is not an executable exact wrapper: {wrapper_effect_id}"
        )


def _load_trigger(row: sqlite3.Row) -> None:
    trigger_id = str(row["id"])
    for column, expected in (
        ("phase_types_json", [CARD_PLAY_PHASE_TYPE]),
        ("phase_values_json", []),
        ("field_status_check_types_json", []),
        ("field_status_types_json", []),
        ("field_status_values_json", []),
        ("field_status_produce_card_search_ids_json", []),
        ("effect_types_json", []),
    ):
        if _json_array(row[column], f"{trigger_id}.{column}") != expected:
            raise Plan2CardPlayPlayingSearchContractError(
                f"{trigger_id}: unsupported trigger field {column}"
            )
    if (
        trigger_id != CARD_PLAY_TRIGGER_ID
        or str(row["produce_card_search_id"]) != PLAYING_SEARCH_ID
        or int(row["upper_search_count"]) != UPPER_SEARCH_COUNT_NO_CAP
        or int(row["lower_search_count"]) != LOWER_SEARCH_COUNT_INCLUSIVE
        or str(row["card_move_position_type"])
        != "ProduceCardMovePositionType_Unknown"
        or str(row["lesson_type"]) != "ProduceStepLessonType_Unknown"
    ):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{trigger_id}: unsupported trigger bounds or shape"
        )
    raw = _json_object(row["raw_json"], trigger_id)
    for key, expected in (
        ("id", CARD_PLAY_TRIGGER_ID),
        ("phaseTypes", [CARD_PLAY_PHASE_TYPE]),
        ("phaseValues", []),
        ("fieldStatusCheckTypes", []),
        ("fieldStatusTypes", []),
        ("fieldStatusValues", []),
        ("fieldStatusProduceCardSearchIds", []),
        ("produceCardSearchId", PLAYING_SEARCH_ID),
        ("upperSearchCount", UPPER_SEARCH_COUNT_NO_CAP),
        ("lowerSearchCount", LOWER_SEARCH_COUNT_INCLUSIVE),
        ("cardMovePositionType", "ProduceCardMovePositionType_Unknown"),
        ("effectTypes", []),
        ("lessonType", "ProduceStepLessonType_Unknown"),
    ):
        _require_equal(raw, key, expected, trigger_id)


def _load_child(
    row: sqlite3.Row, *, expected_id: str, expected_value1: int
) -> Plan2CardPlayEffect:
    effect_id = str(row["id"])
    if effect_id != expected_id:
        raise Plan2CardPlayPlayingSearchContractError(
            f"child order changed: expected {expected_id}, got {effect_id}"
        )
    raw = _json_object(row["raw_json"], effect_id)
    _validate_effect_controls(row, raw, status_id="")
    value1 = _plain_i32(int(row["value1"]), f"{effect_id}.value1")
    value2 = _plain_i32(int(row["value2"]), f"{effect_id}.value2")
    count = _plain_i32(int(row["effect_count"]), f"{effect_id}.count")
    turn = _plain_i32(int(row["effect_turn"]), f"{effect_id}.turn")
    if (
        str(row["effect_type"]) != LESSON_DEPEND_REVIEW_EFFECT_TYPE
        or value1 != expected_value1
        or value2 != 0
        or count != 1
        or turn != 0
        or _ordered_groups(raw, effect_id) != EXPECTED_CHILD_GROUPS
    ):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{effect_id}: unsupported Review-dependent lesson child shape"
        )
    return Plan2CardPlayEffect(
        effect_id,
        LESSON_DEPEND_REVIEW_EFFECT_TYPE,
        value1,
        value2,
        count,
        turn,
        EXPECTED_CHILD_GROUPS,
    )


def _load_program(
    connection: sqlite3.Connection,
    wrapper_effect_id: str,
    expected_status_id: str,
    expected_child_id: str,
    expected_value1: int,
    database: Path,
) -> Plan2PlayingSearchProgram:
    wrapper = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (wrapper_effect_id,)
    ).fetchone()
    if wrapper is None:
        raise Plan2CardPlayPlayingSearchContractError(
            f"missing exact wrapper: {wrapper_effect_id}"
        )
    if str(wrapper["effect_type"]) != STATUS_ENCHANT_EFFECT_TYPE:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{wrapper_effect_id}: expected StatusEnchant"
        )
    status_id = str(wrapper["status_enchant_id"])
    if status_id != expected_status_id:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{wrapper_effect_id}: status id changed"
        )
    status = connection.execute(
        "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (status_id,)
    ).fetchone()
    if status is None:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{wrapper_effect_id}: missing status {status_id}"
        )
    trigger_id = str(status["produce_exam_trigger_id"])
    trigger = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
    ).fetchone()
    if trigger is None:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{status_id}: missing trigger {trigger_id}"
        )
    _load_trigger(trigger)
    child_ids = tuple(
        str(value)
        for value in _json_array(status["produce_exam_effect_ids_json"], status_id)
    )
    if child_ids != (expected_child_id,):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{status_id}: child order/count is not exact"
        )
    child_row = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (expected_child_id,)
    ).fetchone()
    if child_row is None:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{status_id}: missing child {expected_child_id}"
        )
    child = _load_child(
        child_row, expected_id=expected_child_id, expected_value1=expected_value1
    )
    wrapper_raw = _json_object(wrapper["raw_json"], wrapper_effect_id)
    _validate_effect_controls(wrapper, wrapper_raw, status_id=status_id)
    if (
        int(wrapper["value1"]) != 0
        or int(wrapper["value2"]) != 0
        or int(wrapper["effect_count"]) != 0
        or int(wrapper["effect_turn"]) != PERMANENT_TURN
        or str(wrapper["chain_effect_id"])
    ):
        raise Plan2CardPlayPlayingSearchContractError(
            f"{wrapper_effect_id}: wrapper count/lifetime is not exact"
        )
    if _ordered_groups(wrapper_raw, wrapper_effect_id) != EXPECTED_WRAPPER_GROUPS:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{wrapper_effect_id}: wrapper effect-group order changed"
        )
    status_raw = _json_object(status["raw_json"], status_id)
    for key, expected in (
        ("id", status_id),
        ("assetId", str(status["asset_id"])),
        ("produceExamTriggerId", trigger_id),
        ("produceExamEffectIds", [expected_child_id]),
    ):
        _require_equal(status_raw, key, expected, status_id)
    return Plan2PlayingSearchProgram(
        wrapper_effect_id=wrapper_effect_id,
        status_enchant_id=status_id,
        trigger_id=trigger_id,
        search=load_produce_card_search(PLAYING_SEARCH_ID, database),
        child=child,
        wrapper_effect_group_ids=EXPECTED_WRAPPER_GROUPS,
    )


def _load_target(
    row: sqlite3.Row, accepted_wrappers: frozenset[str]
) -> Plan2PlayingSearchTarget:
    card_id = str(row["id"])
    upgrade = _plain_i32(int(row["upgrade_count"]), f"{card_id}.upgrade")
    if card_id != CARD_ID or upgrade not in CARD_UPGRADES:
        raise Plan2CardPlayPlayingSearchContractError(
            f"unexpected card version in exact target query: {card_id}/{upgrade}"
        )
    raw = _json_object(row["raw_json"], f"{card_id}:{upgrade}")
    for key, expected in (
        ("id", card_id),
        ("upgradeCount", upgrade),
        ("planType", "ProducePlanType_Plan2"),
        ("category", "ProduceCardCategory_ActiveSkill"),
    ):
        _require_equal(raw, key, expected, f"{card_id}:{upgrade}")
    groups = _ordered_groups(raw, f"{card_id}:{upgrade}")
    if groups != EXPECTED_CARD_GROUPS[upgrade]:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{card_id}:{upgrade}: card effect-group order changed"
        )
    effect_rows = _json_array(row["play_effects_json"], f"{card_id}:{upgrade}")
    ordered_ids: list[str] = []
    for index, value in enumerate(effect_rows):
        if not isinstance(value, dict):
            raise Plan2CardPlayPlayingSearchContractError(
                f"{card_id}:{upgrade}: play effect {index} is not an object"
            )
        if value.get("produceExamTriggerId") != "":
            raise Plan2CardPlayPlayingSearchContractError(
                f"{card_id}:{upgrade}: direct play trigger is not neutral"
            )
        if value.get("hideIcon") is not False or value.get("isOncePlayEffect") is not False:
            raise Plan2CardPlayPlayingSearchContractError(
                f"{card_id}:{upgrade}: direct play controls changed"
            )
        effect_id = value.get("produceExamEffectId")
        if not isinstance(effect_id, str) or not effect_id:
            raise Plan2CardPlayPlayingSearchContractError(
                f"{card_id}:{upgrade}: direct effect id is invalid"
            )
        ordered_ids.append(effect_id)
    expected_ids = EXPECTED_CARD_EFFECTS[upgrade]
    if tuple(ordered_ids) != expected_ids:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{card_id}:{upgrade}: ordered direct effects changed"
        )
    wrapper_ids = tuple(
        effect_id for effect_id in ordered_ids if effect_id in accepted_wrappers
    )
    if len(wrapper_ids) != 1:
        raise Plan2CardPlayPlayingSearchContractError(
            f"{card_id}:{upgrade}: expected one exact direct wrapper"
        )
    wrapper_id = wrapper_ids[0]
    return Plan2PlayingSearchTarget(
        card_id=card_id,
        upgrade=upgrade,
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        ordered_effect_ids=tuple(ordered_ids),
        effect_slot=ordered_ids.index(wrapper_id),
        wrapper_effect_id=wrapper_id,
        card_effect_group_ids=groups,
    )


def _catalog_rejections(
    connection: sqlite3.Connection,
) -> tuple[Plan2PlayingSearchCatalogEntry, ...]:
    rows = connection.execute(
        "SELECT id, produce_exam_trigger_id FROM produce_exam_status_enchant "
        "WHERE produce_exam_trigger_id = ? ORDER BY id",
        (CARD_PLAY_TRIGGER_ID,),
    ).fetchall()
    actual = tuple(str(row["id"]) for row in rows)
    expected = (
        STATUS_ENC01,
        STATUS_ENC02,
        "enchant-p_card-02-act-3_050-enc03",
        *ITEM_ALIAS_STATUS_IDS,
    )
    if set(actual) != set(expected):
        raise Plan2CardPlayPlayingSearchContractError(
            f"trigger catalog changed: expected {expected!r}, got {actual!r}"
        )
    entries = [
        Plan2PlayingSearchCatalogEntry(
            WRAPPER_ENC03,
            "card-wrapper",
            "reject:extra-effect-group-and-extra-child-review-effect",
            "enchant-p_card-02-act-3_050-enc03",
        )
    ]
    item_effect_rows = connection.execute(
        "SELECT id, produce_exam_status_enchant_id FROM produce_item_effect "
        "WHERE produce_exam_status_enchant_id IN (?, ?) ORDER BY id",
        ITEM_ALIAS_STATUS_IDS,
    ).fetchall()
    for row in item_effect_rows:
        entries.append(
            Plan2PlayingSearchCatalogEntry(
                str(row["id"]),
                "item-effect-alias",
                "reject:catalog-only item alias, not a p_card direct wrapper",
                str(row["produce_exam_status_enchant_id"]),
            )
        )
    for status_id in ITEM_ALIAS_STATUS_IDS:
        entries.append(
            Plan2PlayingSearchCatalogEntry(
                status_id,
                "item-status-alias",
                "reject:catalog-only item status, not an executable p_card wrapper",
                ITEM_ALIAS_ITEM_ID,
            )
        )
    return tuple(entries)


def resolve_plan2_card_play_playing_search(
    database: Path = DEFAULT_DATABASE,
) -> Plan2PlayingSearchContract:
    """Resolve the exact trigger, wrappers, target versions, and exclusions."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (CARD_PLAY_TRIGGER_ID,),
        ).fetchone()
        if trigger is None:
            raise Plan2CardPlayPlayingSearchContractError(
                f"missing exact trigger: {CARD_PLAY_TRIGGER_ID}"
            )
        _load_trigger(trigger)
        search = load_produce_card_search(PLAYING_SEARCH_ID, database)
        mismatches = exact_playing_card_search_mismatches(
            search, expected_categories=(), expected_effect_group_ids=()
        )
        if mismatches:
            raise Plan2CardPlayPlayingSearchContractError(
                f"{PLAYING_SEARCH_ID}: unsupported field {mismatches[0]}"
            )
        programs = (
            _load_program(
                connection, WRAPPER_ENC01, STATUS_ENC01, CHILD_0300, 300, database
            ),
            _load_program(
                connection, WRAPPER_ENC02, STATUS_ENC02, CHILD_0500, 500, database
            ),
        )
        target_rows = connection.execute(
            "SELECT * FROM card WHERE id = ? ORDER BY upgrade_count", (CARD_ID,)
        ).fetchall()
        targets = tuple(
            _load_target(row, frozenset({WRAPPER_ENC01, WRAPPER_ENC02}))
            for row in target_rows
        )
        if tuple(target.upgrade for target in targets) != CARD_UPGRADES:
            raise Plan2CardPlayPlayingSearchContractError(
                "target card does not have exactly upgrade0..3"
            )
        catalog = _catalog_rejections(connection)
    # The loader above uses the shared card-search row.  Rebind its exact
    # instance here so a future caller cannot observe a wrapper-local search.
    programs = tuple(
        replace(program, search=search) for program in programs
    )
    return Plan2PlayingSearchContract(
        trigger_id=CARD_PLAY_TRIGGER_ID,
        phase_type=CARD_PLAY_PHASE_TYPE,
        search=search,
        programs=programs,
        targets=targets,
        catalog_rejections=catalog,
    )


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchRequest:
    """Verified event context at the native Playing-card boundary."""

    state: Plan2State
    native_state: Plan3NativeState
    playing_guid: str
    playing_card: Plan2PlayingCard
    source: PlaySource = PlaySource.ORDINARY
    accepted: bool = True
    playing_card_present: bool = True
    is_use_playable_count: bool | None = None
    lesson_parameter_before: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, Plan2State):
            raise TypeError("state must be Plan2State")
        if not isinstance(self.native_state, Plan3NativeState):
            raise TypeError("native_state must be Plan3NativeState")
        if not isinstance(self.playing_card, Plan2PlayingCard):
            raise TypeError("playing_card must be Plan2PlayingCard")
        if not isinstance(self.playing_guid, str) or not self.playing_guid:
            raise Plan2CardPlayPlayingSearchContractError(
                "playing_guid must be non-empty"
            )
        if not isinstance(self.source, PlaySource):
            raise TypeError("source must be PlaySource")
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a boolean")
        if type(self.playing_card_present) is not bool:
            raise TypeError("playing_card_present must be a boolean")
        if self.is_use_playable_count is not None and type(
            self.is_use_playable_count
        ) is not bool:
            raise TypeError("is_use_playable_count must be bool or None")
        _plain_i32(self.lesson_parameter_before, "lesson_parameter_before")
        if self.lesson_parameter_before < 0:
            raise Plan2CardPlayPlayingSearchContractError(
                "lesson_parameter_before must be non-negative"
            )


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchOwnership:
    source: PlaySource
    playing_guid: str
    source_zone: str
    native_card: Plan3NativeCard
    transient_playing_snapshot: bool = True


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchCapturedActivation:
    listener_before: Plan2CardPlayListener
    listener_after_count_spend: Plan2CardPlayListener | None
    program: Plan2PlayingSearchProgram


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchCapture:
    before: Plan2State
    after_count_spend: Plan2State
    request: Plan2PlayingSearchRequest
    ownership: Plan2PlayingSearchOwnership | None
    candidates: tuple[Plan2PlayingSearchCapturedActivation, ...]
    removed_status_uids: tuple[int, ...]
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchLessonOccurrence:
    listener_status_uid: int
    effect_index: int
    effect_id: str
    effect_type: str
    value1: int
    effect_count: int
    review_input: int
    lesson_before: int
    lesson_after: int
    delta: int
    effect_group_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchActivation:
    captured: Plan2PlayingSearchCapturedActivation
    effects: tuple[Plan2PlayingSearchLessonOccurrence, ...]


@dataclass(frozen=True, slots=True)
class Plan2PlayingSearchTransition:
    before: Plan2State
    after_capture: Plan2State
    after_cost: Plan2State
    after_listeners: Plan2State
    after: Plan2State
    capture: Plan2PlayingSearchCapture
    activations: tuple[Plan2PlayingSearchActivation, ...]
    lesson_parameter_before: int
    lesson_parameter_after: int
    lesson_delta: int
    event_trace: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.capture.request.accepted

    @property
    def candidate_status_uids(self) -> tuple[int, ...]:
        return tuple(
            activation.captured.listener_before.status_uid
            for activation in self.activations
        )


Plan2PlayingSearchStage = Callable[[Plan2State], Plan2State]
Plan2PlayingSearchLessonHook = Callable[
    [Plan2PlayingSearchLessonOccurrence], None
]


def _identity_stage(state: Plan2State) -> Plan2State:
    return state


def _replace_or_remove_listener(
    state: Plan2State,
    uid: int,
    replacement: Plan2CardPlayListener | None,
) -> Plan2State:
    found = False
    listeners: list[Plan2CardPlayListener] = []
    for listener in state.card_play_listeners:
        if listener.status_uid != uid:
            listeners.append(listener)
            continue
        found = True
        if replacement is not None:
            listeners.append(replacement)
    if not found:
        raise Plan2CardPlayPlayingSearchContractError(
            f"active listener disappeared: {uid}"
        )
    return replace(state, card_play_listeners=tuple(listeners))


def _zone_for_card(state: Plan3NativeState, guid: str) -> tuple[str, Plan3NativeCard]:
    matches: list[tuple[str, Plan3NativeCard]] = []
    for zone_name in ("hand", "deck", "grave", "lost", "hold"):
        for card in getattr(state, zone_name):
            if card.guid == guid:
                matches.append((zone_name, card))
    if len(matches) != 1:
        raise Plan2CardPlayPlayingSearchContractError(
            f"playing GUID is not uniquely owned by ordered zones: {guid}"
        )
    return matches[0]


def _resolve_ownership(
    request: Plan2PlayingSearchRequest,
    contract: Plan2PlayingSearchContract,
) -> Plan2PlayingSearchOwnership:
    if request.source is PlaySource.ORDINARY:
        identity = resolve_playing_card_identity(
            request.native_state,
            request.playing_guid,
            request.playing_card,
            reason_context=CARD_PLAY_TRIGGER_ID,
        )
        if identity.issues:
            issue = identity.issues[0]
            raise Plan2CardPlayPlayingSearchContractError(
                f"{issue.field}: {issue.reason}"
            )
        native_card = identity.native_card
        if not isinstance(native_card, Plan3NativeCard):
            raise Plan2CardPlayPlayingSearchContractError(
                "ordinary Playing identity did not resolve a Plan3NativeCard"
            )
        try:
            candidates = request.native_state.card_move_candidates(
                contract.search, playing_guid=request.playing_guid
            )
        except Exception as error:
            raise Plan2CardPlayPlayingSearchContractError(
                f"ordered Playing candidate construction failed: {error}"
            ) from error
        if len(candidates) != 1 or candidates[0].guid != request.playing_guid:
            raise Plan2CardPlayPlayingSearchContractError(
                "ordered Playing search did not yield the selected GUID"
            )
        zone = "hand"
    else:
        # Force/extra commands use the same transient PlayingCard context but
        # are not ordinary Hand collection searches.  Bind their source GUID
        # through the shared native ordered-zone identity instead of inventing
        # a persistent Playing zone.  If the caller has already removed the
        # source from all modeled zones, this leaf cannot prove the binding.
        try:
            native_card = request.native_state.card_by_guid(request.playing_guid)
            zone, zoned_card = _zone_for_card(
                request.native_state, request.playing_guid
            )
        except Exception as error:
            raise Plan2CardPlayPlayingSearchContractError(
                f"{request.source.value} Playing identity is unresolved: {error}"
            ) from error
        if native_card is not zoned_card:
            raise Plan2CardPlayPlayingSearchContractError(
                "ordered-zone identity returned inconsistent card object"
            )
        if (
            native_card.card_id != request.playing_card.id
            or native_card.effective_upgrade != request.playing_card.upgrade
        ):
            raise Plan2CardPlayPlayingSearchContractError(
                "transient Playing card id/effective-upgrade mismatch"
            )
    matched, reason = match_exact_playing_card_search(
        contract.search,
        card_category=request.playing_card.category,
        card_effect_group_ids=request.playing_card.effect_group_ids,
        expected_categories=(),
        expected_effect_group_ids=(),
    )
    if reason is not None:
        raise Plan2CardPlayPlayingSearchContractError(reason)
    if not matched:
        raise Plan2CardPlayPlayingSearchContractError(
            "exact neutral Playing search unexpectedly did not match"
        )
    return Plan2PlayingSearchOwnership(
        source=request.source,
        playing_guid=request.playing_guid,
        source_zone=zone,
        native_card=native_card,
    )


def _validate_listener(
    listener: Plan2CardPlayListener,
    program: Plan2PlayingSearchProgram,
) -> None:
    if (
        listener.wrapper_effect_id != program.wrapper_effect_id
        or listener.status_enchant_id != program.status_enchant_id
        or listener.trigger_id != CARD_PLAY_TRIGGER_ID
        or listener.search_id != PLAYING_SEARCH_ID
        or listener.effects != (program.child,)
        or listener.wrapper_effect_group_ids != EXPECTED_WRAPPER_GROUPS
        or listener.turn != PERMANENT_TURN
        or listener.limit_count != -1
        or listener.limit_count_in_turn != -1
        or listener.limit_count_in_turn_remaining != -1
    ):
        raise Plan2CardPlayPlayingSearchContractError(
            f"listener:{listener.status_uid}: exact wrapper shape changed"
        )


def capture_plan2_card_play_playing_search(
    request: Plan2PlayingSearchRequest,
    contract: Plan2PlayingSearchContract,
) -> Plan2PlayingSearchCapture:
    """Capture exact listeners at Playing, before payment and direct effects."""

    if not isinstance(request, Plan2PlayingSearchRequest):
        raise TypeError("request must be Plan2PlayingSearchRequest")
    if not isinstance(contract, Plan2PlayingSearchContract):
        raise TypeError("contract must be Plan2PlayingSearchContract")
    if not request.accepted:
        return Plan2PlayingSearchCapture(
            before=request.state,
            after_count_spend=request.state,
            request=request,
            ownership=None,
            candidates=(),
            removed_status_uids=(),
            event_trace=("rejected-before-exam-card-play",),
        )
    if not request.playing_card_present:
        raise Plan2CardPlayPlayingSearchContractError(
            "accepted CardPlay has no PlayingCard context"
        )
    ownership = _resolve_ownership(request, contract)
    trace = (
        f"set-playing-card:{request.playing_guid}:snapshot:{ownership.source_zone}",
        f"capture:{CARD_PLAY_PHASE_TYPE}:pre-payment:pre-direct",
    )
    after = request.state
    candidates: list[Plan2PlayingSearchCapturedActivation] = []
    removed: list[int] = []
    for snapshot in request.state.card_play_listeners:
        if snapshot.trigger_id != CARD_PLAY_TRIGGER_ID:
            continue
        if snapshot.search_id != PLAYING_SEARCH_ID:
            raise Plan2CardPlayPlayingSearchContractError(
                f"listener:{snapshot.status_uid}: unsupported search id"
            )
        try:
            program = contract.program_for_wrapper(snapshot.wrapper_effect_id)
        except Plan2CardPlayPlayingSearchContractError as error:
            raise Plan2CardPlayPlayingSearchContractError(
                f"listener:{snapshot.status_uid}: {error}"
            ) from error
        _validate_listener(snapshot, program)
        if not snapshot.can_trigger:
            continue
        current = next(
            (
                item
                for item in after.card_play_listeners
                if item.status_uid == snapshot.status_uid
            ),
            None,
        )
        if current is None:
            raise Plan2CardPlayPlayingSearchContractError(
                f"listener:{snapshot.status_uid}: disappeared during capture"
            )
        # The exact wrappers are unlimited in both native count fields.  Keep
        # the listener object structurally unchanged while still recording the
        # native SpendCount queue boundary and capturing active-list order.
        listener_after = current
        candidates.append(
            Plan2PlayingSearchCapturedActivation(snapshot, listener_after, program)
        )
    if candidates:
        trace = (*trace, *(
            f"spend-count:{candidate.listener_before.status_uid}:queue-time"
            for candidate in candidates
        ))
    return Plan2PlayingSearchCapture(
        before=request.state,
        after_count_spend=after,
        request=request,
        ownership=ownership,
        candidates=tuple(candidates),
        removed_status_uids=tuple(removed),
        event_trace=tuple(trace),
    )


def lesson_depend_exam_review_delta(
    review: int, value1: int, effect_count: int = 1
) -> int:
    """Apply the native Review-dependent lesson ratio at float32 precision."""

    review = _plain_i32(review, "review")
    value1 = _plain_i32(value1, "value1")
    effect_count = _plain_i32(effect_count, "effect_count")
    if review < 0 or value1 < 0 or effect_count < 0:
        raise Plan2CardPlayPlayingSearchContractError(
            "Review-dependent lesson inputs must be non-negative"
        )
    if review == 0 or value1 == 0:
        return 0
    try:
        # Reuse the already-audited native helper.  It performs binary32
        # permille division/multiplication and the native negative epsilon
        # before ceil, rather than introducing a second ratio implementation.
        single = get_ratio_effect_int_value(review, value1, is_ceil=True)
    except (NativeFormulaDomainError, OverflowError, ValueError) as error:
        raise Plan2CardPlayPlayingSearchContractError(
            f"native Review-dependent lesson formula domain: {error}"
        ) from error
    repetitions = max(1, effect_count)
    total = single * repetitions
    if not INT32_MIN <= total <= INT32_MAX:
        raise Plan2CardPlayPlayingSearchContractError(
            "Review-dependent lesson result is outside Int32"
        )
    return total


def _execute_capture(
    capture: Plan2PlayingSearchCapture,
    *,
    pay_cost: Plan2PlayingSearchStage,
    apply_direct_effects: Plan2PlayingSearchStage,
    on_lesson_effect: Plan2PlayingSearchLessonHook | None,
) -> Plan2PlayingSearchTransition:
    if not isinstance(capture, Plan2PlayingSearchCapture):
        raise TypeError("capture must be Plan2PlayingSearchCapture")
    if not capture.request.accepted:
        lesson = capture.request.lesson_parameter_before
        return Plan2PlayingSearchTransition(
            before=capture.before,
            after_capture=capture.after_count_spend,
            after_cost=capture.after_count_spend,
            after_listeners=capture.after_count_spend,
            after=capture.after_count_spend,
            capture=capture,
            activations=(),
            lesson_parameter_before=lesson,
            lesson_parameter_after=lesson,
            lesson_delta=0,
            event_trace=capture.event_trace,
        )
    after_cost = pay_cost(capture.after_count_spend)
    if not isinstance(after_cost, Plan2State):
        raise TypeError("pay_cost must return Plan2State")
    trace = [*capture.event_trace, "pay-cost"]
    lesson = capture.request.lesson_parameter_before
    activations: list[Plan2PlayingSearchActivation] = []
    for candidate in capture.candidates:
        occurrences: list[Plan2PlayingSearchLessonOccurrence] = []
        for effect_index, effect in enumerate(candidate.listener_before.effects):
            if effect.effect_type != LESSON_DEPEND_REVIEW_EFFECT_TYPE:
                raise Plan2CardPlayPlayingSearchContractError(
                    f"unsupported runtime child type: {effect.effect_type}"
                )
            review_input = after_cost.review
            delta = lesson_depend_exam_review_delta(
                review_input, effect.value1, effect.count
            )
            lesson_before = lesson
            lesson_after = lesson_before + delta
            if not 0 <= lesson_after <= INT32_MAX:
                raise Plan2CardPlayPlayingSearchContractError(
                    "lesson parameter addition is outside non-negative Int32"
                )
            occurrence = Plan2PlayingSearchLessonOccurrence(
                listener_status_uid=candidate.listener_before.status_uid,
                effect_index=effect_index,
                effect_id=effect.effect_id,
                effect_type=effect.effect_type,
                value1=effect.value1,
                effect_count=effect.count,
                review_input=review_input,
                lesson_before=lesson_before,
                lesson_after=lesson_after,
                delta=delta,
                effect_group_ids=effect.effect_group_ids,
            )
            occurrences.append(occurrence)
            lesson = lesson_after
            if on_lesson_effect is not None:
                on_lesson_effect(occurrence)
            trace.append(
                f"listener:{candidate.listener_before.status_uid}:effect:"
                f"{effect_index}:{effect.effect_id}:post-payment:pre-direct"
            )
        activations.append(
            Plan2PlayingSearchActivation(candidate, tuple(occurrences))
        )
    after_listeners = after_cost
    after = apply_direct_effects(after_listeners)
    if not isinstance(after, Plan2State):
        raise TypeError("apply_direct_effects must return Plan2State")
    trace.extend(("direct-effects", "physical-move:later"))
    return Plan2PlayingSearchTransition(
        before=capture.before,
        after_capture=capture.after_count_spend,
        after_cost=after_cost,
        after_listeners=after_listeners,
        after=after,
        capture=capture,
        activations=tuple(activations),
        lesson_parameter_before=capture.request.lesson_parameter_before,
        lesson_parameter_after=lesson,
        lesson_delta=lesson - capture.request.lesson_parameter_before,
        event_trace=tuple(trace),
    )


def evaluate_plan2_card_play_playing_search(
    request: Plan2PlayingSearchRequest,
    contract: Plan2PlayingSearchContract,
    *,
    pay_cost: Plan2PlayingSearchStage = _identity_stage,
    apply_direct_effects: Plan2PlayingSearchStage = _identity_stage,
    on_lesson_effect: Plan2PlayingSearchLessonHook | None = None,
) -> Plan2PlayingSearchTransition:
    """Purely project one exact event; hooks receive immutable state values."""

    # ``isUsePlayableCount`` controls a separate play-count path.  Native
    # ordinary, forced, and extra accepted commands still enter the same
    # ExecuteCardCommandImpl/Playing listener path, so this leaf records the
    # flag but does not use it as a trigger guard.
    capture = capture_plan2_card_play_playing_search(request, contract)
    return _execute_capture(
        capture,
        pay_cost=pay_cost,
        apply_direct_effects=apply_direct_effects,
        on_lesson_effect=on_lesson_effect,
    )


def install_plan2_card_play_playing_search_listener(
    state: Plan2State, program: Plan2PlayingSearchProgram
) -> tuple[Plan2State, int]:
    """Install one exact permanent/unlimited listener using Plan2 listener state."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2PlayingSearchProgram):
        raise TypeError("program must be Plan2PlayingSearchProgram")
    uid = state.next_status_uid
    listener = Plan2CardPlayListener(
        status_uid=uid,
        wrapper_effect_id=program.wrapper_effect_id,
        status_enchant_id=program.status_enchant_id,
        trigger_id=program.trigger_id,
        search_id=program.search.id,
        effects=(program.child,),
        wrapper_effect_group_ids=program.wrapper_effect_group_ids,
        turn=program.turn,
        limit_count=program.native_limit_count,
        limit_count_in_turn=program.native_limit_count_in_turn,
        limit_count_in_turn_remaining=program.native_limit_count_in_turn,
    )
    after = replace(
        state,
        card_play_listeners=state.card_play_listeners + (listener,),
        next_status_uid=uid + 1,
    )
    return after, uid


__all__ = [
    "CARD_ID",
    "CARD_PLAY_PHASE_TYPE",
    "CARD_PLAY_TRIGGER_ID",
    "CARD_UPGRADES",
    "CHILD_0300",
    "CHILD_0500",
    "EXPECTED_CARD_EFFECTS",
    "EXPECTED_CHILD_GROUPS",
    "EXPECTED_WRAPPER_GROUPS",
    "ITEM_ALIAS_ITEM_ID",
    "ITEM_ALIAS_STATUS_IDS",
    "LESSON_DEPEND_REVIEW_EFFECT_TYPE",
    "LOWER_SEARCH_COUNT_INCLUSIVE",
    "PERMANENT_TURN",
    "PLAYING_SEARCH_ID",
    "PlaySource",
    "Plan2CardPlayPlayingSearchContractError",
    "Plan2PlayingSearchActivation",
    "Plan2PlayingSearchCapturedActivation",
    "Plan2PlayingSearchCapture",
    "Plan2PlayingSearchCatalogEntry",
    "Plan2PlayingSearchContract",
    "Plan2PlayingSearchLessonOccurrence",
    "Plan2PlayingSearchOwnership",
    "Plan2PlayingSearchProgram",
    "Plan2PlayingSearchRequest",
    "Plan2PlayingSearchTarget",
    "Plan2PlayingSearchTransition",
    "Plan2PlayingSearchStage",
    "STATUS_ENC01",
    "STATUS_ENC02",
    "UPPER_SEARCH_COUNT_NO_CAP",
    "WRAPPER_ENC01",
    "WRAPPER_ENC02",
    "WRAPPER_ENC03",
    "capture_plan2_card_play_playing_search",
    "evaluate_plan2_card_play_playing_search",
    "install_plan2_card_play_playing_search_listener",
    "lesson_depend_exam_review_delta",
    "resolve_plan2_card_play_playing_search",
    "simulate_plan2_turn_start",
]
