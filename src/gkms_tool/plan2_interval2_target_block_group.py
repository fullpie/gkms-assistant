"""Standalone Plan2 ``ExamPlayCountInterval(2)`` Target/effect-group leaf.

This module is intentionally a leaf adapter.  It does not register a new
phase with the Plan2 core, rebuild coverage, or install a GUI hook.  The
phase-23 counter and listener record are the already-audited Plan2 interval
adapter shapes; the Target search is checked through the shared card-search
predicate and the fixed CardCreateId child is delegated to the existing
Plan2/Common adapter.

The exact native boundary represented here is::

    SetPlayingCard -> immutable Playing snapshot -> payment ->
    phase-23 listener child -> direct card effects -> final move

Only a Playing card carrying ``effect_group-visible-exam_block-000`` is a
search match.  A well-formed card without that group is a normal no-fire
event, not an unsupported input.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import sqlite3
from typing import TypeAlias

from . import plan3_play_count_interval_trigger as _plan3
from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_exact_target_card_search,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX
from .plan2_card_create_id import (
    CARD_CREATE_ID_EFFECT_TYPE,
    CardCreateContract,
    CardCreateGuidAllocator,
    CardCreateResult,
    Plan2CardCreateState,
    apply_plan2_card_create_id,
    resolve_plan2_card_create_contract,
)
from .plan2_card_play_playing_search_trigger import (
    PlaySource,
    Plan2PlayingCard,
)
from .plan2_play_count_interval5 import (
    AcceptedPlay,
    Plan2PlayCountPhaseCounter,
)
from .plan2_state import (
    PERMANENT_TURN,
    Plan2EndTurnEffect,
    Plan2EndTurnListener,
    Plan2State,
    simulate_plan2_turn_start,
)
from .plan3_engine import (
    CATEGORY_MENTAL,
    COST_STAMINA,
    EFFECT_STATUS_ENCHANT,
    MOVE_LOST,
    PLAN3,
    Plan3Card,
    Plan3Effect,
    Plan3StatusEnchantRule,
    Plan3Trigger,
    load_plan3_effect,
)


CARD_ID = "p_card-02-ido-3_169"
CARD_UPGRADES = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple((CARD_ID, upgrade) for upgrade in CARD_UPGRADES)
AFFECTED_CARD_VERSION_COUNT = 4
DIRECT_CARD_VERSION_COUNT = 4
SOLE_UNLOCK_CARD_VERSION_COUNT = 4
CO_BLOCKED_CARD_VERSION_COUNT = 0
CARD_STAMINA_BY_UPGRADE = (6, 3, 2, 1)

TRIGGER_ID = (
    "e_trigger-exam_play_count_interval-2-"
    "p_card_search-target-effect_group-visible-exam_block-000"
)
PHASE_PLAY_COUNT_INTERVAL = _plan3.PHASE_PLAY_COUNT_INTERVAL
PHASE_CARD_PLAY = _plan3.PHASE_CARD_PLAY
PHASE_CARD_PLAY_AFTER = _plan3.PHASE_CARD_PLAY_AFTER
PHASE_PLAY_COUNT_INTERVAL_AFTER = _plan3.PHASE_PLAY_COUNT_INTERVAL_AFTER
PHASE_SEMANTIC_NUMBER = 23
INTERVAL = 2

INSTALLER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_169-enc01"
)
STATUS_ENCHANT_ID = "enchant-p_card-02-ido-3_169-enc01"
CHILD_EFFECT_ID = (
    "e_effect-exam_card_create_id-p_card-02-ido-3_190-0-deck_first-1_1"
)
CHILD_EFFECT_TYPE = CARD_CREATE_ID_EFFECT_TYPE
CHILD_EFFECT_COUNT = 0
CHILD_CARD_ID = "p_card-02-ido-3_190"
CHILD_UPGRADE = 0
CHILD_DESTINATION = "deck_first"
CHILD_COUNT = 1

TARGET_SEARCH_ID = "p_card_search-target-effect_group-visible-exam_block-000"
PLAYING_SEARCH_ID = TARGET_SEARCH_ID
TARGET_EFFECT_GROUP_ID = "effect_group-visible-exam_block-000"
EFFECT_GROUP_TARGET = TARGET_EFFECT_GROUP_ID
STATUS_ENCHANT_GROUP = "effect_group-visible-exam_status_enchant-000"
PLAYABLE_VALUE_GROUP = "effect_group-visible-exam_playable_value_add-000"
EXPECTED_SOURCE_CARD_GROUPS = (STATUS_ENCHANT_GROUP, PLAYABLE_VALUE_GROUP)
EXPECTED_WRAPPER_GROUPS = (STATUS_ENCHANT_GROUP,)
EXPECTED_CHILD_GROUPS: tuple[str, ...] = ()

NATIVE_CARD_TRANSACTION_ORDER = _plan3.NATIVE_CARD_TRANSACTION_ORDER
TRIGGER_PHASE_BOUNDARY = "phase23-before-card-direct-effects"
SNAPSHOT_BOUNDARY = "after-set-playing-card-before-payment-and-direct-effects"
SETTLEMENT_BOUNDARY = "after-final-zone-move"

PLAN2 = "ProducePlanType_Plan2"
UNKNOWN_PLAN = "ProducePlanType_Unknown"
UNKNOWN_STATUS = "ProduceCardSearchStatusType_Unknown"
UNKNOWN_ORDER = "ProduceCardOrderType_Unknown"
UNKNOWN_MIN_MAX = "ConditionMinMaxType_Unknown"
UNKNOWN_EXAM_EFFECT = "ProduceExamEffectType_Unknown"
UNKNOWN_COST = "ExamCostType_Unknown"
TARGET_POSITION = "ProduceCardPositionType_Target"
UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"
UNKNOWN_PICK_RANGE = "ProducePickRangeType_Unknown"
UNKNOWN_PICK_COUNT = "ProducePickCountType_Unknown"


class Plan2Interval2TargetBlockGroupContractError(ValueError):
    """Master/native evidence is outside this exact standalone family."""


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        raw = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2Interval2TargetBlockGroupContractError(
            f"{label}: invalid JSON object"
        ) from error
    if not isinstance(raw, dict):
        raise Plan2Interval2TargetBlockGroupContractError(
            f"{label}: JSON value is not an object"
        )
    return raw


def _json_array(value: object, label: str) -> list[object]:
    try:
        raw = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2Interval2TargetBlockGroupContractError(
            f"{label}: invalid JSON array"
        ) from error
    if not isinstance(raw, list):
        raise Plan2Interval2TargetBlockGroupContractError(
            f"{label}: JSON value is not an array"
        )
    return raw


def _require_equal(
    raw: Mapping[str, object], key: str, expected: object, label: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2Interval2TargetBlockGroupContractError(
            f"{label}: {key} drifted"
        )


def _ordered_strings(raw: Mapping[str, object], key: str, label: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise Plan2Interval2TargetBlockGroupContractError(
            f"{label}: {key} must be an ordered string array"
        )
    return tuple(value)


def _trigger_raw_errors(
    row: sqlite3.Row, *, expected_id: str
) -> tuple[str, ...]:
    errors: list[str] = []
    expected_columns: tuple[tuple[str, object], ...] = (
        ("phase_types_json", [PHASE_PLAY_COUNT_INTERVAL]),
        ("phase_values_json", [INTERVAL]),
        ("field_status_check_types_json", []),
        ("field_status_types_json", []),
        ("field_status_values_json", []),
        ("field_status_produce_card_search_ids_json", []),
        ("effect_types_json", []),
    )
    for column, expected in expected_columns:
        try:
            observed = _json_array(row[column], f"{expected_id}.{column}")
        except Plan2Interval2TargetBlockGroupContractError:
            errors.append(f"trigger:{column}")
            continue
        if observed != expected:
            errors.append(f"trigger:{column}")
    if (
        str(row["id"]) != expected_id
        or str(row["produce_card_search_id"]) != TARGET_SEARCH_ID
        or int(row["upper_search_count"]) != 0
        or int(row["lower_search_count"]) != 0
        or str(row["card_move_position_type"]) != UNKNOWN_MOVE
        or str(row["lesson_type"]) != "ProduceStepLessonType_Unknown"
    ):
        errors.append("trigger:scalar-fields")
    try:
        raw = _json_object(row["raw_json"], expected_id)
        for key, expected in (
            ("id", expected_id),
            ("phaseTypes", [PHASE_PLAY_COUNT_INTERVAL]),
            ("phaseValues", [INTERVAL]),
            ("fieldStatusCheckTypes", []),
            ("fieldStatusTypes", []),
            ("fieldStatusValues", []),
            ("fieldStatusProduceCardSearchIds", []),
            ("produceCardSearchId", TARGET_SEARCH_ID),
            ("upperSearchCount", 0),
            ("lowerSearchCount", 0),
            ("cardMovePositionType", UNKNOWN_MOVE),
            ("effectTypes", []),
            ("lessonType", "ProduceStepLessonType_Unknown"),
        ):
            if raw.get(key) != expected:
                errors.append(f"trigger:raw:{key}")
    except Plan2Interval2TargetBlockGroupContractError:
        errors.append("trigger:raw-json")
    return _unique(errors)


def matches_interval2_target_block_group_trigger(trigger: object) -> bool:
    """Match only the exact phase-23/value-2 trigger object."""

    return bool(
        isinstance(trigger, Plan3Trigger)
        and trigger.id == TRIGGER_ID
        and trigger.phase_types == (PHASE_PLAY_COUNT_INTERVAL,)
        and trigger.phase_values == (INTERVAL,)
        and not trigger.field_check_types
        and not trigger.field_types
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and trigger.produce_card_search_id == TARGET_SEARCH_ID
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == UNKNOWN_MOVE
        and not trigger.effect_types
        and trigger.lesson_type == "ProduceStepLessonType_Unknown"
    )


def _search_errors(search: object) -> tuple[str, ...]:
    if not isinstance(search, ProduceCardSearchRule):
        return ("search:typed-rule-required",)
    errors: list[str] = []
    if search.id != TARGET_SEARCH_ID:
        errors.append("search:id")
    # The shared exact Target helper proves the IsSearchTargetByKeys/neutral
    # grammar.  This row is the group-only variant, so probe that helper with
    # one synthetic key and then pin the real empty key sets below.  No local
    # copy of the Target grammar is introduced here.
    probe = replace(
        search,
        produce_card_ids=(CARD_ID,),
        upgrade_counts=(0,),
        effect_group_ids=(),
    )
    matched, reason = match_exact_target_card_search(probe, CARD_ID, 0)
    if reason is not None or not matched:
        errors.append(f"search:target-predicate:{reason or 'no-match'}")
    if search.produce_card_ids != ():
        errors.append("search:produce-card-ids")
    if search.upgrade_counts != ():
        errors.append("search:upgrade-counts")
    if search.effect_group_ids != (TARGET_EFFECT_GROUP_ID,):
        errors.append("search:effect-group")
    return _unique(errors)


def match_interval2_target_block_group_search(
    search: ProduceCardSearchRule, card: Plan2PlayingCard
) -> tuple[bool, str | None]:
    """Apply the exact Target/group predicate to one immutable card snapshot.

    ``False, None`` is the ordinary no-group non-match.  A non-``None``
    reason means the Master row or input shape drifted.
    """

    errors = _search_errors(search)
    if errors:
        return False, errors[0]
    if not isinstance(card, Plan2PlayingCard):
        return False, "search:playing-card-snapshot-type"
    if TARGET_EFFECT_GROUP_ID not in card.effect_group_ids:
        return False, None
    return True, None


def matches_interval2_target_block_group_search(
    search: ProduceCardSearchRule, card: Plan2PlayingCard
) -> bool:
    matched, reason = match_interval2_target_block_group_search(search, card)
    return reason is None and matched


@dataclass(frozen=True, slots=True)
class Plan2Interval2Target:
    """One exact affected source-card version from the named Master query."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    stamina_cost: int
    force_stamina_cost: int
    cost_type: str
    cost_value: int
    move_position_type: str
    ordered_effect_ids: tuple[str, ...]
    card_effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.card_id != CARD_ID or self.upgrade not in CARD_UPGRADES:
            raise Plan2Interval2TargetBlockGroupContractError(
                "source card is outside p_card-02-ido-3_169 upgrade0..3"
            )
        if (
            self.plan_type != PLAN2
            or self.category != CATEGORY_MENTAL
            or self.stamina_cost != CARD_STAMINA_BY_UPGRADE[self.upgrade]
            or self.force_stamina_cost != 0
            or self.cost_type != COST_STAMINA
            or self.cost_value != 0
            or self.move_position_type != MOVE_LOST
            or self.ordered_effect_ids
            != (
                "e_effect-exam_playable_value_add-01",
                INSTALLER_EFFECT_ID,
            )
            or self.card_effect_group_ids != EXPECTED_SOURCE_CARD_GROUPS
        ):
            raise Plan2Interval2TargetBlockGroupContractError(
                f"source card shape drifted: {self.card_id}:{self.upgrade}"
            )


def matches_interval2_target_block_group_card(card: object) -> bool:
    """Accept only the four source-card versions carrying the wrapper."""

    upgrade = getattr(card, "upgrade", None)
    if isinstance(upgrade, bool) or not isinstance(upgrade, int):
        return False
    effects = tuple(
        getattr(effect, "id", None) for effect in getattr(card, "effects", ())
    )
    groups = tuple(getattr(card, "effect_group_ids", ()))
    return bool(
        getattr(card, "id", None) == CARD_ID
        and upgrade in CARD_UPGRADES
        and getattr(card, "plan_type", None) == PLAN2
        and getattr(card, "category", None) == CATEGORY_MENTAL
        and getattr(card, "stamina_cost", None)
        == CARD_STAMINA_BY_UPGRADE[upgrade]
        and getattr(card, "force_stamina_cost", None) == 0
        and getattr(card, "cost_type", None) == COST_STAMINA
        and getattr(card, "cost_value", None) == 0
        and getattr(card, "play_trigger", None) is None
        and getattr(card, "move_position_type", None) == MOVE_LOST
        and effects
        == (
            "e_effect-exam_playable_value_add-01",
            INSTALLER_EFFECT_ID,
        )
        and groups == EXPECTED_SOURCE_CARD_GROUPS
    )


matches_interval2_card = matches_interval2_target_block_group_card
matches_plan2_interval2_target_block_group_card = (
    matches_interval2_target_block_group_card
)


@dataclass(frozen=True, slots=True)
class Plan2Interval2TargetBlockGroupProgram:
    """The exact wrapper, search, child adapter, and source-card catalog."""

    installer: Plan3Effect
    search: ProduceCardSearchRule | None = None
    card_create: CardCreateContract | None = None
    targets: tuple[Plan2Interval2Target, ...] = ()
    wrapper_effect_group_ids: tuple[str, ...] = EXPECTED_WRAPPER_GROUPS

    @property
    def rule(self) -> Plan3StatusEnchantRule | None:
        return self.installer.status_enchant

    @property
    def child(self) -> Plan3Effect | None:
        rule = self.rule
        return None if rule is None or len(rule.effects) != 1 else rule.effects[0]

    @property
    def affected_version_count(self) -> int:
        return len(self.targets)

    @property
    def direct_version_count(self) -> int:
        return len(self.targets)

    @property
    def co_blocked_version_count(self) -> int:
        return self.affected_version_count - self.direct_version_count

    @property
    def affected_direct(self) -> tuple[int, int]:
        return self.affected_version_count, self.direct_version_count


def _installer_errors(installer: object) -> tuple[str, ...]:
    if not isinstance(installer, Plan3Effect):
        return ("installer:typed-effect-required",)
    errors: list[str] = []
    if installer.id != INSTALLER_EFFECT_ID:
        errors.append("installer:id")
    if (
        installer.effect_type != EFFECT_STATUS_ENCHANT
        or installer.value1 != 0
        or installer.value2 != 0
        or installer.effect_count != 0
        or installer.effect_turn != PERMANENT_TURN
        or installer.status_enchant_id != STATUS_ENCHANT_ID
        or installer.chain_effect_id
        or installer.chain_effect_ids
        or installer.chain_effect is not None
        or installer.trigger is not None
        or installer.once
        or installer.card_move_rule is not None
    ):
        errors.append("installer:shape")
    rule = installer.status_enchant
    if not isinstance(rule, Plan3StatusEnchantRule):
        errors.append("status:missing")
        return _unique(errors)
    if rule.id != STATUS_ENCHANT_ID:
        errors.append("status:id")
    if not matches_interval2_target_block_group_trigger(rule.trigger):
        errors.append("trigger:shape")
    if len(rule.effects) != 1:
        errors.append("child:count")
        return _unique(errors)
    child = rule.effects[0]
    if not (
        child.id == CHILD_EFFECT_ID
        and child.effect_type == CHILD_EFFECT_TYPE
        and child.value1 == 0
        and child.value2 == 0
        and child.effect_count == 0
        and child.effect_turn == 0
        and not child.status_enchant_id
        and not child.chain_effect_id
        and not child.chain_effect_ids
        and child.chain_effect is None
        and child.trigger is None
        and not child.once
        and child.card_move_rule is None
    ):
        errors.append("child:shape")
    return _unique(errors)


def _program_errors(program: object) -> tuple[str, ...]:
    if not isinstance(program, Plan2Interval2TargetBlockGroupProgram):
        return ("program:typed-program-required",)
    errors = list(_installer_errors(program.installer))
    if program.search is None:
        errors.append("search:missing")
    else:
        errors.extend(_search_errors(program.search))
    contract = program.card_create
    if not isinstance(contract, CardCreateContract):
        errors.append("child:card-create-contract-missing")
    else:
        if (
            contract.effect_id != CHILD_EFFECT_ID
            or contract.card_id != CHILD_CARD_ID
            or contract.upgrade != CHILD_UPGRADE
            or contract.destination != CHILD_DESTINATION
            or contract.count_min != CHILD_COUNT
            or contract.count_max != CHILD_COUNT
            or contract.effect_type != CHILD_EFFECT_TYPE
        ):
            errors.append("child:card-create-contract")
    if tuple(program.wrapper_effect_group_ids) != EXPECTED_WRAPPER_GROUPS:
        errors.append("wrapper:effect-groups")
    targets = tuple(program.targets)
    if len(targets) != AFFECTED_CARD_VERSION_COUNT:
        errors.append("source-card:version-count")
    if tuple((target.card_id, target.upgrade) for target in targets) != (
        AFFECTED_CARD_VERSIONS
    ):
        errors.append("source-card:version-order")
    return _unique(errors)


def matches_interval2_target_block_group_installer(effect: object) -> bool:
    return not _installer_errors(effect)


matches_interval2_trigger = matches_interval2_target_block_group_trigger
matches_interval2_rule = matches_interval2_target_block_group_installer
matches_plan2_interval2_target_block_group_trigger = (
    matches_interval2_target_block_group_trigger
)
matches_plan2_interval2_target_block_group_rule = (
    matches_interval2_target_block_group_installer
)
matches_plan2_interval2_target_block_group_installer = (
    matches_interval2_target_block_group_installer
)


def _validate_effect_controls(
    row: sqlite3.Row,
    raw: Mapping[str, object],
    *,
    status_id: str,
    groups: tuple[str, ...],
    child: bool = False,
) -> None:
    effect_id = str(row["id"])
    target_card_id = CHILD_CARD_ID if child else ""
    move_position = (
        "ProduceCardMovePositionType_DeckFirst" if child else UNKNOWN_MOVE
    )
    pick_min = CHILD_COUNT if child else 0
    pick_max = CHILD_COUNT if child else 0
    expected: tuple[tuple[str, object], ...] = (
        ("id", effect_id),
        ("effectType", str(row["effect_type"])),
        ("effectValue1", int(row["value1"])),
        ("effectValue2", int(row["value2"])),
        ("effectCount", int(row["effect_count"])),
        ("effectTurn", int(row["effect_turn"])),
        ("targetProduceCardId", target_card_id),
        ("targetUpgradeCount", CHILD_UPGRADE if child else 0),
        ("targetExamEffectType", UNKNOWN_EXAM_EFFECT),
        ("produceCardSearchId", ""),
        ("movePositionType", move_position),
        ("pickRangeType", UNKNOWN_PICK_RANGE),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", UNKNOWN_PICK_COUNT),
        ("pickCountMin", pick_min),
        ("pickCountMax", pick_max),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", UNKNOWN_PICK_RANGE),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", UNKNOWN_PICK_COUNT),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", status_id),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
        ("effectGroupIds", list(groups)),
    )
    for key, expected_value in expected:
        _require_equal(raw, key, expected_value, effect_id)


def _validate_child_row(row: sqlite3.Row) -> CardCreateContract:
    if (
        str(row["id"]) != CHILD_EFFECT_ID
        or str(row["effect_type"]) != CHILD_EFFECT_TYPE
        or int(row["value1"]) != 0
        or int(row["value2"]) != 0
        or int(row["effect_count"]) != 0
        or int(row["effect_turn"]) != 0
        or str(row["status_enchant_id"])
        or str(row["chain_effect_id"])
    ):
        raise Plan2Interval2TargetBlockGroupContractError("child:outer-shape")
    raw = _json_object(row["raw_json"], CHILD_EFFECT_ID)
    _require_equal(raw, "targetProduceCardId", CHILD_CARD_ID, CHILD_EFFECT_ID)
    _require_equal(raw, "targetUpgradeCount", CHILD_UPGRADE, CHILD_EFFECT_ID)
    _require_equal(
        raw,
        "movePositionType",
        "ProduceCardMovePositionType_DeckFirst",
        CHILD_EFFECT_ID,
    )
    _require_equal(raw, "pickCountMin", CHILD_COUNT, CHILD_EFFECT_ID)
    _require_equal(raw, "pickCountMax", CHILD_COUNT, CHILD_EFFECT_ID)
    _validate_effect_controls(
        row,
        raw,
        status_id="",
        groups=EXPECTED_CHILD_GROUPS,
        child=True,
    )
    contract = resolve_plan2_card_create_contract(dict(row), plan_type=PLAN2)
    if (
        contract.effect_id,
        contract.card_id,
        contract.upgrade,
        contract.destination,
        contract.count_min,
        contract.count_max,
    ) != (
        CHILD_EFFECT_ID,
        CHILD_CARD_ID,
        CHILD_UPGRADE,
        CHILD_DESTINATION,
        CHILD_COUNT,
        CHILD_COUNT,
    ):
        raise Plan2Interval2TargetBlockGroupContractError("child:adapter-contract")
    return contract


def _load_target(row: sqlite3.Row) -> Plan2Interval2Target:
    card_id = str(row["id"])
    upgrade = int(row["upgrade_count"])
    label = f"{card_id}:{upgrade}"
    raw = _json_object(row["raw_json"], label)
    for key, expected in (
        ("id", card_id),
        ("upgradeCount", upgrade),
        ("planType", PLAN2),
        ("category", CATEGORY_MENTAL),
        ("forceStamina", 0),
        ("effectGroupIds", list(EXPECTED_SOURCE_CARD_GROUPS)),
    ):
        _require_equal(raw, key, expected, label)
    entries = _json_array(row["play_effects_json"], label)
    ordered: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise Plan2Interval2TargetBlockGroupContractError(
                f"{label}: direct effect {index} is not an object"
            )
        _require_equal(entry, "produceExamTriggerId", "", label)
        _require_equal(entry, "hideIcon", False, label)
        _require_equal(entry, "isOncePlayEffect", False, label)
        effect_id = entry.get("produceExamEffectId")
        if not isinstance(effect_id, str) or not effect_id:
            raise Plan2Interval2TargetBlockGroupContractError(
                f"{label}: direct effect id is invalid"
            )
        ordered.append(effect_id)
    return Plan2Interval2Target(
        card_id=card_id,
        upgrade=upgrade,
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina_cost=int(row["stamina"]),
        force_stamina_cost=int(raw["forceStamina"]),
        cost_type=str(row["cost_type"]),
        cost_value=int(row["cost_value"]),
        move_position_type=str(row["move_position_type"]),
        ordered_effect_ids=tuple(ordered),
        card_effect_group_ids=_ordered_strings(raw, "effectGroupIds", label),
    )


def _validate_search_raw(row: sqlite3.Row) -> None:
    raw = _json_object(row["raw_json"], TARGET_SEARCH_ID)
    for key, expected in (
        ("id", TARGET_SEARCH_ID),
        ("cardRarities", []),
        ("produceCardIds", []),
        ("upgradeCounts", []),
        ("planType", UNKNOWN_PLAN),
        ("cardCategories", []),
        ("cardStatusType", UNKNOWN_STATUS),
        ("orderType", UNKNOWN_ORDER),
        ("cardPositionType", TARGET_POSITION),
        ("cardSearchTag", ""),
        ("produceCardRandomPoolId", ""),
        ("limitCount", 0),
        ("staminaMinMaxType", UNKNOWN_MIN_MAX),
        ("staminaMin", 0),
        ("staminaMax", 0),
        ("examEffectType", UNKNOWN_EXAM_EFFECT),
        ("effectGroupIds", [TARGET_EFFECT_GROUP_ID]),
        ("isSelf", False),
        ("produceCardPoolId", ""),
        ("costType", UNKNOWN_COST),
        ("isCustomized", False),
    ):
        _require_equal(raw, key, expected, TARGET_SEARCH_ID)


def load_plan2_interval2_target_block_group_program(
    database: Path = DEFAULT_DATABASE,
) -> Plan2Interval2TargetBlockGroupProgram:
    """Load only the named Master rows for this exact trigger family."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        wrapper = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (INSTALLER_EFFECT_ID,)
        ).fetchone()
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (STATUS_ENCHANT_ID,),
        ).fetchone()
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
        ).fetchone()
        child = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (CHILD_EFFECT_ID,)
        ).fetchone()
        search_row = connection.execute(
            "SELECT * FROM produce_card_search WHERE id = ?",
            (TARGET_SEARCH_ID,),
        ).fetchone()
        target_rows = connection.execute(
            "SELECT * FROM card WHERE id = ? AND upgrade_count IN (?,?,?,?) "
            "ORDER BY upgrade_count",
            (CARD_ID, *CARD_UPGRADES),
        ).fetchall()
        if wrapper is None or status is None or trigger is None or child is None:
            raise Plan2Interval2TargetBlockGroupContractError(
                "missing exact wrapper/status/trigger/child row"
            )
        if search_row is None or len(target_rows) != AFFECTED_CARD_VERSION_COUNT:
            raise Plan2Interval2TargetBlockGroupContractError(
                "missing exact search or source-card versions"
            )
        trigger_errors = _trigger_raw_errors(trigger, expected_id=TRIGGER_ID)
        if trigger_errors:
            raise Plan2Interval2TargetBlockGroupContractError(str(trigger_errors))
        if str(wrapper["effect_type"]) != EFFECT_STATUS_ENCHANT:
            raise Plan2Interval2TargetBlockGroupContractError("wrapper:type")
        if str(wrapper["status_enchant_id"]) != STATUS_ENCHANT_ID:
            raise Plan2Interval2TargetBlockGroupContractError("wrapper:status")
        wrapper_raw = _json_object(wrapper["raw_json"], INSTALLER_EFFECT_ID)
        if (
            int(wrapper["value1"]) != 0
            or int(wrapper["value2"]) != 0
            or int(wrapper["effect_count"]) != 0
            or int(wrapper["effect_turn"]) != PERMANENT_TURN
            or str(wrapper["chain_effect_id"])
        ):
            raise Plan2Interval2TargetBlockGroupContractError("wrapper:lifecycle")
        _validate_effect_controls(
            wrapper,
            wrapper_raw,
            status_id=STATUS_ENCHANT_ID,
            groups=EXPECTED_WRAPPER_GROUPS,
        )
        status_raw = _json_object(status["raw_json"], STATUS_ENCHANT_ID)
        status_effect_ids = _json_array(
            status["produce_exam_effect_ids_json"], STATUS_ENCHANT_ID
        )
        for key, expected in (
            ("id", STATUS_ENCHANT_ID),
            ("assetId", str(status["asset_id"])),
            ("produceExamTriggerId", TRIGGER_ID),
            ("produceExamEffectIds", [CHILD_EFFECT_ID]),
        ):
            _require_equal(status_raw, key, expected, STATUS_ENCHANT_ID)
        if str(status["produce_exam_trigger_id"]) != TRIGGER_ID or status_effect_ids != [
            CHILD_EFFECT_ID
        ]:
            raise Plan2Interval2TargetBlockGroupContractError("status:graph")
        contract = _validate_child_row(child)
        _validate_search_raw(search_row)
        search = load_produce_card_search(TARGET_SEARCH_ID, database)
        targets = tuple(_load_target(row) for row in target_rows)

    installer = load_plan3_effect(INSTALLER_EFFECT_ID, database)
    program = Plan2Interval2TargetBlockGroupProgram(
        installer=installer,
        search=search,
        card_create=contract,
        targets=targets,
        wrapper_effect_group_ids=EXPECTED_WRAPPER_GROUPS,
    )
    errors = _program_errors(program)
    if errors:
        raise Plan2Interval2TargetBlockGroupContractError(str(errors))
    return program


load_plan2_interval2_program = load_plan2_interval2_target_block_group_program
load_interval2_target_block_group_program = (
    load_plan2_interval2_target_block_group_program
)


def _expected_listener(
    status_uid: int, program: Plan2Interval2TargetBlockGroupProgram
) -> Plan2EndTurnListener:
    child = program.child
    if child is None:
        raise Plan2Interval2TargetBlockGroupContractError("child:missing")
    return Plan2EndTurnListener(
        status_uid=status_uid,
        wrapper_effect_id=INSTALLER_EFFECT_ID,
        status_enchant_id=STATUS_ENCHANT_ID,
        trigger_id=TRIGGER_ID,
        effects=(
            Plan2EndTurnEffect(
                effect_id=CHILD_EFFECT_ID,
                effect_type=CHILD_EFFECT_TYPE,
                value1=child.value1,
                value2=child.value2,
                count=child.effect_count,
                turn=child.effect_turn,
                effect_group_ids=EXPECTED_CHILD_GROUPS,
            ),
        ),
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
    program: Plan2Interval2TargetBlockGroupProgram,
) -> tuple[str, ...]:
    errors: list[str] = []
    expected = _expected_listener(listener.status_uid, program)
    if listener.wrapper_effect_id != expected.wrapper_effect_id:
        errors.append(f"listener:{listener.status_uid}:wrapper")
    if listener.status_enchant_id != expected.status_enchant_id:
        errors.append(f"listener:{listener.status_uid}:status")
    if listener.trigger_id != expected.trigger_id:
        errors.append(f"listener:{listener.status_uid}:trigger")
    if listener.effects != expected.effects:
        errors.append(f"listener:{listener.status_uid}:child")
    if listener.wrapper_effect_group_ids != expected.wrapper_effect_group_ids:
        errors.append(f"listener:{listener.status_uid}:effect-groups")
    if not (
        listener.turn == PERMANENT_TURN
        and listener.limit_count == -1
        and listener.limit_count_in_turn == -1
        and listener.limit_count_in_turn_remaining == -1
        and listener.turn_count >= 0
        and isinstance(listener.is_passing_turn_start, bool)
    ):
        errors.append(f"listener:{listener.status_uid}:lifecycle")
    return tuple(errors)


@dataclass(frozen=True, slots=True)
class Plan2Interval2TargetBlockGroupState:
    """Plan2 scalar/listener state plus the shared GUID-native card state."""

    native_state: Plan2CardCreateState = field(default_factory=Plan2CardCreateState)
    active: tuple[Plan2EndTurnListener, ...] = ()
    phase_counts: tuple[Plan2PlayCountPhaseCounter, ...] = ()
    plan2_state: Plan2State = field(default_factory=Plan2State)

    def __post_init__(self) -> None:
        if not isinstance(self.native_state, Plan2CardCreateState):
            raise TypeError("native_state must be Plan2CardCreateState")
        if not isinstance(self.plan2_state, Plan2State):
            raise TypeError("plan2_state must be Plan2State")
        active = tuple(self.active)
        if not all(isinstance(item, Plan2EndTurnListener) for item in active):
            raise TypeError("active must contain Plan2EndTurnListener values")
        active_uids = tuple(item.status_uid for item in active)
        base_uids = {
            *(item.status_uid for item in self.plan2_state.end_turn_listeners),
            *(item.status_uid for item in self.plan2_state.card_play_listeners),
        }
        if len(active_uids) != len(set(active_uids)):
            raise ValueError("active listener status_uids must be unique")
        if set(active_uids).intersection(base_uids):
            raise ValueError("interval listener status_uids must be distinct")
        counters = tuple(self.phase_counts)
        if not all(
            isinstance(item, Plan2PlayCountPhaseCounter) for item in counters
        ):
            raise TypeError("phase_counts must contain Plan2PlayCountPhaseCounter")
        counter_uids = tuple(item.status_uid for item in counters)
        if len(counter_uids) != len(set(counter_uids)):
            raise ValueError("phase counter status_uids must be unique")
        if set(counter_uids).difference(active_uids):
            raise ValueError("phase counters must reference active listeners")
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "phase_counts", counters)

    @property
    def base_state(self) -> Plan2State:
        return self.plan2_state

    @property
    def card_create_state(self) -> Plan2CardCreateState:
        return self.native_state


Plan2Interval2State = Plan2Interval2TargetBlockGroupState


@dataclass(frozen=True, slots=True)
class Plan2Interval2InstallTransition:
    before: Plan2Interval2TargetBlockGroupState
    after: Plan2Interval2TargetBlockGroupState
    program: Plan2Interval2TargetBlockGroupProgram
    created_status_uid: int

    @property
    def listener(self) -> Plan2EndTurnListener:
        return self.after.active[-1]


def _require_program(
    program: Plan2Interval2TargetBlockGroupProgram,
) -> Plan2Interval2TargetBlockGroupProgram:
    if not isinstance(program, Plan2Interval2TargetBlockGroupProgram):
        raise TypeError("program must be Plan2Interval2TargetBlockGroupProgram")
    errors = _program_errors(program)
    if errors:
        raise Plan2Interval2TargetBlockGroupContractError(str(errors))
    return program


def install_plan2_interval2_target_block_group_listener(
    state: Plan2Interval2TargetBlockGroupState,
    program: Plan2Interval2TargetBlockGroupProgram,
) -> Plan2Interval2InstallTransition:
    """Install one fresh permanent/unlimited listener in active order."""

    if not isinstance(state, Plan2Interval2TargetBlockGroupState):
        raise TypeError("state must be Plan2Interval2TargetBlockGroupState")
    program = _require_program(program)
    uid = state.plan2_state.next_status_uid
    listener = _expected_listener(uid, program)
    after_plan2 = replace(state.plan2_state, next_status_uid=uid + 1)
    after = replace(
        state,
        active=state.active + (listener,),
        plan2_state=after_plan2,
    )
    return Plan2Interval2InstallTransition(state, after, program, uid)


install_plan2_interval2_listener = (
    install_plan2_interval2_target_block_group_listener
)


def install_plan2_interval2_target_block_group_listener_on_state(
    state: Plan2State,
    program: Plan2Interval2TargetBlockGroupProgram,
    *,
    native_state: Plan2CardCreateState | None = None,
) -> Plan2Interval2InstallTransition:
    """Bridge an existing Plan2 scalar state into this standalone leaf."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    return install_plan2_interval2_target_block_group_listener(
        Plan2Interval2TargetBlockGroupState(
            native_state=native_state or Plan2CardCreateState(),
            plan2_state=state,
        ),
        program,
    )


install_plan2_interval2_listener_on_state = (
    install_plan2_interval2_target_block_group_listener_on_state
)


@dataclass(frozen=True, slots=True)
class Plan2Interval2Request:
    """Verified event boundary, including caller-owned child GUID tokens."""

    state: Plan2Interval2TargetBlockGroupState
    event: AcceptedPlay
    guid_tokens: tuple[str, ...] | None = None
    guid_allocator: CardCreateGuidAllocator | Callable[..., str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, Plan2Interval2TargetBlockGroupState):
            raise TypeError("state must be Plan2Interval2TargetBlockGroupState")
        if not isinstance(self.event, AcceptedPlay):
            raise TypeError("event must be AcceptedPlay")
        if self.guid_tokens is not None:
            if isinstance(self.guid_tokens, (str, bytes)):
                raise TypeError("guid_tokens must be a sequence, not text")
            tokens = tuple(self.guid_tokens)
            if any(not isinstance(token, str) or not token.strip() for token in tokens):
                raise ValueError("guid_tokens must contain non-empty strings")
            object.__setattr__(self, "guid_tokens", tokens)


Plan2Interval2TargetBlockGroupRequest = Plan2Interval2Request


@dataclass(frozen=True, slots=True)
class Plan2Interval2CapturedActivation:
    listener_before: Plan2EndTurnListener
    listener_after_count_spend: Plan2EndTurnListener


@dataclass(frozen=True, slots=True)
class Plan2Interval2Capture:
    before: Plan2Interval2TargetBlockGroupState
    after_count_spend: Plan2Interval2TargetBlockGroupState
    request: Plan2Interval2Request
    playing_card_snapshot: Plan2PlayingCard | None
    search_matched: bool
    candidates: tuple[Plan2Interval2CapturedActivation, ...] = ()
    unresolved: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def state_unchanged(self) -> bool:
        return self.before == self.after_count_spend


def _snapshot_card(card: object) -> Plan2PlayingCard:
    if isinstance(card, Plan2PlayingCard):
        return card
    if isinstance(card, Plan3Card):
        return Plan2PlayingCard(
            id=card.id,
            upgrade=card.upgrade,
            category=card.category,
            effect_group_ids=card.effect_group_ids,
        )
    raise Plan2Interval2TargetBlockGroupContractError(
        "accepted-play:Playing snapshot must be Plan2PlayingCard"
    )


def capture_plan2_interval2_target_block_group(
    request: Plan2Interval2Request,
    program: Plan2Interval2TargetBlockGroupProgram,
) -> Plan2Interval2Capture:
    """Snapshot and queue exact listeners before payment/direct effects."""

    if not isinstance(request, Plan2Interval2Request):
        raise TypeError("request must be Plan2Interval2Request")
    if not isinstance(program, Plan2Interval2TargetBlockGroupProgram):
        raise TypeError("program must be Plan2Interval2TargetBlockGroupProgram")
    state = request.state
    event = request.event
    errors = list(_program_errors(program))
    if not event.accepted:
        return Plan2Interval2Capture(
            before=state,
            after_count_spend=state,
            request=request,
            playing_card_snapshot=None,
            search_matched=False,
            unresolved=tuple(errors),
            event_trace=("rejected-before-exam-card-play",),
        )
    if not event.playing_card_present:
        errors.append("accepted-play-has-no-playing-card")
    snapshot: Plan2PlayingCard | None = None
    if event.card is None:
        errors.append("accepted-play-has-no-playing-card-snapshot")
    else:
        try:
            snapshot = _snapshot_card(event.card)
        except Plan2Interval2TargetBlockGroupContractError as error:
            errors.append(str(error))
    if errors or snapshot is None:
        return Plan2Interval2Capture(
            before=state,
            after_count_spend=state,
            request=request,
            playing_card_snapshot=snapshot,
            search_matched=False,
            unresolved=_unique(errors),
        )
    assert program.search is not None
    matched, reason = match_interval2_target_block_group_search(
        program.search, snapshot
    )
    if reason is not None:
        return Plan2Interval2Capture(
            before=state,
            after_count_spend=state,
            request=request,
            playing_card_snapshot=snapshot,
            search_matched=False,
            unresolved=(reason,),
            event_trace=(
                "set-playing-card:snapshot",
                "capture:target-search:pre-payment:pre-direct",
            ),
        )
    listener_errors = [
        error
        for listener in state.active
        for error in _listener_errors(listener, program)
    ]
    if listener_errors:
        return Plan2Interval2Capture(
            before=state,
            after_count_spend=state,
            request=request,
            playing_card_snapshot=snapshot,
            search_matched=matched,
            unresolved=_unique(listener_errors),
            event_trace=(
                "set-playing-card:snapshot",
                "capture:target-search:pre-payment:pre-direct",
            ),
        )
    trace = [
        "set-playing-card:snapshot",
        "capture:target-search:pre-payment:pre-direct",
    ]
    if not matched:
        trace.append("search-target:no-effect-group:no-trigger")
        return Plan2Interval2Capture(
            before=state,
            after_count_spend=state,
            request=request,
            playing_card_snapshot=snapshot,
            search_matched=False,
            event_trace=tuple(trace),
        )

    old_counts = {item.status_uid: item.count for item in state.phase_counts}
    counters: list[Plan2PlayCountPhaseCounter] = []
    candidates: list[Plan2Interval2CapturedActivation] = []
    for listener in state.active:
        before = old_counts.get(listener.status_uid, 0)
        if before == INT32_MAX:
            return Plan2Interval2Capture(
                before=state,
                after_count_spend=state,
                request=request,
                playing_card_snapshot=snapshot,
                search_matched=True,
                unresolved=(f"phase-count-overflow:{listener.status_uid}",),
                event_trace=tuple(trace),
            )
        after_count = before + 1
        counters.append(Plan2PlayCountPhaseCounter(listener.status_uid, after_count))
        trace.append(
            f"listener:{listener.status_uid}:phase23-count:{before}->{after_count}"
        )
        # Unlimited wrappers are structurally unchanged by native SpendCount,
        # but the queue boundary remains explicit in the audit trace.
        candidates.append(Plan2Interval2CapturedActivation(listener, listener))
        if after_count % INTERVAL == 0:
            trace.append(f"spend-count:{listener.status_uid}:unlimited:no-delta")
    after = replace(state, phase_counts=tuple(counters)) if state.active else state
    return Plan2Interval2Capture(
        before=state,
        after_count_spend=after,
        request=request,
        playing_card_snapshot=snapshot,
        search_matched=True,
        candidates=tuple(candidates),
        event_trace=tuple(trace),
    )


Plan2Interval2TargetBlockGroupStage = Callable[
    [Plan2Interval2TargetBlockGroupState], Plan2Interval2TargetBlockGroupState
]


def _identity_stage(
    state: Plan2Interval2TargetBlockGroupState,
) -> Plan2Interval2TargetBlockGroupState:
    return state


@dataclass(frozen=True, slots=True)
class Plan2Interval2Fire:
    listener_sequence: int
    status_uid: int
    count_before: int
    count_after: int
    child_sequence: int
    child_effect_id: str
    child_effect_type: str
    child_effect_count: int
    created_guid: str
    spend_count_called: bool = True
    spend_count_delta: int = 0
    card_create_result: CardCreateResult | None = None

    @property
    def child_guid(self) -> str:
        return self.created_guid

    @property
    def created_count(self) -> int:
        return CHILD_COUNT

    @property
    def mutation_delta(self) -> int:
        if self.card_create_result is None:
            return 0
        return len(self.card_create_result.after.all_cards) - len(
            self.card_create_result.before.all_cards
        )

    @property
    def source_effect_id(self) -> str:
        return INSTALLER_EFFECT_ID


@dataclass(frozen=True, slots=True)
class Plan2Interval2Execution:
    before: Plan2Interval2TargetBlockGroupState
    after: Plan2Interval2TargetBlockGroupState
    request: Plan2Interval2Request
    capture: Plan2Interval2Capture
    playing_card_snapshot: Plan2PlayingCard | None
    after_payment: Plan2Interval2TargetBlockGroupState | None = None
    after_listeners: Plan2Interval2TargetBlockGroupState | None = None
    fires: tuple[Plan2Interval2Fire, ...] = ()
    card_create_results: tuple[CardCreateResult, ...] = ()
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = NATIVE_CARD_TRANSACTION_ORDER
    event_trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def state_unchanged(self) -> bool:
        return self.before == self.after

    @property
    def event(self) -> AcceptedPlay:
        return self.request.event

    @property
    def trigger_boundary(self) -> str:
        return TRIGGER_PHASE_BOUNDARY

    @property
    def snapshot_boundary(self) -> str:
        return SNAPSHOT_BOUNDARY

    @property
    def settlement_boundary(self) -> str:
        return SETTLEMENT_BOUNDARY

    @property
    def created_guids(self) -> tuple[str, ...]:
        return tuple(fire.created_guid for fire in self.fires)


def _fire_specs(
    capture: Plan2Interval2Capture,
) -> tuple[tuple[int, Plan2EndTurnListener, int, int], ...]:
    old = {item.status_uid: item.count for item in capture.before.phase_counts}
    new = {item.status_uid: item.count for item in capture.after_count_spend.phase_counts}
    specs: list[tuple[int, Plan2EndTurnListener, int, int]] = []
    for sequence, candidate in enumerate(capture.candidates):
        listener = candidate.listener_before
        count_before = old.get(listener.status_uid, 0)
        count_after = new[listener.status_uid]
        if count_after % INTERVAL == 0:
            specs.append((sequence, listener, count_before, count_after))
    return tuple(specs)


def _apply_child(
    state: Plan2Interval2TargetBlockGroupState,
    program: Plan2Interval2TargetBlockGroupProgram,
    *,
    guid_tokens: Sequence[str] | None,
    guid_allocator: CardCreateGuidAllocator | Callable[..., str] | None,
) -> tuple[Plan2Interval2TargetBlockGroupState, CardCreateResult, str | None]:
    assert program.card_create is not None
    try:
        result = apply_plan2_card_create_id(
            state.native_state,
            program.card_create,
            guid_tokens=guid_tokens,
            guid_allocator=guid_allocator,
        )
    except Exception as error:
        return state, None, f"child-card-create-input:{error}"
    if not result.resolved:
        return state, result, "child-card-create-unresolved"
    trace = result.trace
    if (
        result.branch.count != CHILD_COUNT
        or trace.allocated_guids == ()
        or len(trace.allocated_guids) != CHILD_COUNT
        or len(trace.mutations) != CHILD_COUNT
        or trace.mutations[0].card_id != CHILD_CARD_ID
        or trace.mutations[0].upgrade != CHILD_UPGRADE
        or trace.mutations[0].destination != CHILD_DESTINATION
        or result.after.deck[0].guid != trace.allocated_guids[0]
        or result.after.deck[0].card_id != CHILD_CARD_ID
        or result.after.deck[0].effective_upgrade != CHILD_UPGRADE
    ):
        return state, result, "child-card-create-delta-drift"
    return replace(state, native_state=result.after), result, None


def evaluate_plan2_interval2_target_block_group(
    request: Plan2Interval2Request,
    program: Plan2Interval2TargetBlockGroupProgram,
    *,
    pay_cost: Plan2Interval2TargetBlockGroupStage = _identity_stage,
    apply_direct_effects: Plan2Interval2TargetBlockGroupStage = _identity_stage,
) -> Plan2Interval2Execution:
    """Resolve one accepted event at the exact pre-direct phase-23 boundary."""

    if not isinstance(request, Plan2Interval2Request):
        raise TypeError("request must be Plan2Interval2Request")
    if not isinstance(program, Plan2Interval2TargetBlockGroupProgram):
        raise TypeError("program must be Plan2Interval2TargetBlockGroupProgram")
    capture = capture_plan2_interval2_target_block_group(request, program)
    if not capture.executable:
        return Plan2Interval2Execution(
            before=request.state,
            after=request.state,
            request=request,
            capture=capture,
            playing_card_snapshot=capture.playing_card_snapshot,
            unresolved=capture.unresolved,
            event_trace=capture.event_trace,
        )
    after_payment = pay_cost(capture.after_count_spend)
    if not isinstance(after_payment, Plan2Interval2TargetBlockGroupState):
        raise TypeError("pay_cost must return Plan2Interval2TargetBlockGroupState")
    trace = [*capture.event_trace, "pay-cost"]
    current = after_payment
    results: list[CardCreateResult] = []
    fires: list[Plan2Interval2Fire] = []
    specs = _fire_specs(capture) if capture.search_matched else ()
    tokens = request.guid_tokens
    if tokens is not None and len(tokens) != len(specs):
        if specs:
            return Plan2Interval2Execution(
                before=request.state,
                after=request.state,
                request=request,
                capture=capture,
                playing_card_snapshot=capture.playing_card_snapshot,
                after_payment=after_payment,
                unresolved=("child-card-create-guid-token-count",),
                event_trace=tuple(trace),
            )
    for fire_index, (sequence, listener, count_before, count_after) in enumerate(
        specs
    ):
        trace.append(
            f"listener:{listener.status_uid}:effect:0:{CHILD_EFFECT_ID}:"
            "post-payment:pre-direct"
        )
        per_fire_tokens = None
        if tokens is not None:
            per_fire_tokens = (tokens[fire_index],)
        current, result, error = _apply_child(
            current,
            program,
            guid_tokens=per_fire_tokens,
            guid_allocator=request.guid_allocator,
        )
        if error is not None or result is None:
            return Plan2Interval2Execution(
                before=request.state,
                after=request.state,
                request=request,
                capture=capture,
                playing_card_snapshot=capture.playing_card_snapshot,
                after_payment=after_payment,
                unresolved=(error or "child-card-create-unresolved",),
                event_trace=tuple(trace),
            )
        results.append(result)
        fires.append(
            Plan2Interval2Fire(
                listener_sequence=sequence,
                status_uid=listener.status_uid,
                count_before=count_before,
                count_after=count_after,
                child_sequence=0,
                child_effect_id=CHILD_EFFECT_ID,
                child_effect_type=CHILD_EFFECT_TYPE,
                child_effect_count=CHILD_EFFECT_COUNT,
                created_guid=result.trace.allocated_guids[0],
                card_create_result=result,
            )
        )
    after_listeners = current
    trace.extend(
        (
            "ordered-direct-card-effects",
            "direct-effects",
            "physical-move:later",
            "final-zone-move-settled",
            "card-play-after-and-interval-after-listeners",
        )
    )
    after = apply_direct_effects(after_listeners)
    if not isinstance(after, Plan2Interval2TargetBlockGroupState):
        raise TypeError(
            "apply_direct_effects must return Plan2Interval2TargetBlockGroupState"
        )
    return Plan2Interval2Execution(
        before=request.state,
        after=after,
        request=request,
        capture=capture,
        playing_card_snapshot=capture.playing_card_snapshot,
        after_payment=after_payment,
        after_listeners=after_listeners,
        fires=tuple(fires),
        card_create_results=tuple(results),
        event_trace=tuple(trace),
    )


def execute_plan2_interval2_target_block_group_card_start(
    state: Plan2Interval2TargetBlockGroupState,
    event: AcceptedPlay,
    program: Plan2Interval2TargetBlockGroupProgram,
    *,
    guid_tokens: Sequence[str] | None = None,
    guid_allocator: CardCreateGuidAllocator | Callable[..., str] | None = None,
    pay_cost: Plan2Interval2TargetBlockGroupStage = _identity_stage,
    apply_direct_effects: Plan2Interval2TargetBlockGroupStage = _identity_stage,
) -> Plan2Interval2Execution:
    """Convenience entry point reusing the interval5 AcceptedPlay boundary."""

    if not isinstance(state, Plan2Interval2TargetBlockGroupState):
        raise TypeError("state must be Plan2Interval2TargetBlockGroupState")
    if not isinstance(event, AcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    request = Plan2Interval2Request(
        state=state,
        event=event,
        guid_tokens=guid_tokens,
        guid_allocator=guid_allocator,
    )
    return evaluate_plan2_interval2_target_block_group(
        request,
        program,
        pay_cost=pay_cost,
        apply_direct_effects=apply_direct_effects,
    )


execute_plan2_interval2_card_start = (
    execute_plan2_interval2_target_block_group_card_start
)
execute_plan2_interval2_target_card_start = (
    execute_plan2_interval2_target_block_group_card_start
)


@dataclass(frozen=True, slots=True)
class Plan2Interval2TurnStartTransition:
    before: Plan2Interval2TargetBlockGroupState
    after: Plan2Interval2TargetBlockGroupState
    spent_status_uids: tuple[int, ...]
    expired_status_uids: tuple[int, ...]
    fresh_status_uids: tuple[int, ...]
    permanent_status_uids: tuple[int, ...]


def simulate_plan2_interval2_target_block_group_turn_start(
    state: Plan2Interval2TargetBlockGroupState,
    program: Plan2Interval2TargetBlockGroupProgram | None = None,
) -> Plan2Interval2TurnStartTransition:
    """Reuse Plan2 TurnStart and preserve permanent/fresh listener order."""

    if not isinstance(state, Plan2Interval2TargetBlockGroupState):
        raise TypeError("state must be Plan2Interval2TargetBlockGroupState")
    program = program or load_plan2_interval2_target_block_group_program()
    program = _require_program(program)
    errors = [
        error
        for listener in state.active
        for error in _listener_errors(listener, program)
    ]
    if errors:
        raise Plan2Interval2TargetBlockGroupContractError(str(_unique(errors)))
    base_transition = simulate_plan2_turn_start(state.plan2_state)
    survivors: list[Plan2EndTurnListener] = []
    counters = {item.status_uid: item for item in state.phase_counts}
    spent = list(base_transition.spent_status_uids)
    expired = list(base_transition.expired_status_uids)
    fresh = list(base_transition.fresh_status_uids)
    permanent = list(base_transition.permanent_status_uids)
    for listener in state.active:
        if listener.turn_count == INT32_MAX:
            raise Plan2Interval2TargetBlockGroupContractError(
                f"listener turn_count overflow: {listener.status_uid}"
            )
        next_listener = replace(
            listener,
            turn_count=listener.turn_count + 1,
            limit_count_in_turn_remaining=listener.limit_count_in_turn,
            is_passing_turn_start=True,
        )
        if listener.turn == PERMANENT_TURN:
            permanent.append(listener.status_uid)
            survivors.append(next_listener)
            continue
        if not listener.is_passing_turn_start:
            fresh.append(listener.status_uid)
            survivors.append(next_listener)
            continue
        spent.append(listener.status_uid)
        remaining_turns = listener.turn - 1
        if remaining_turns <= 0:
            expired.append(listener.status_uid)
            counters.pop(listener.status_uid, None)
        else:
            survivors.append(replace(next_listener, turn=remaining_turns))
    after = Plan2Interval2TargetBlockGroupState(
        native_state=state.native_state,
        active=tuple(survivors),
        phase_counts=tuple(
            counters[listener.status_uid]
            for listener in survivors
            if listener.status_uid in counters
        ),
        plan2_state=base_transition.after,
    )
    return Plan2Interval2TurnStartTransition(
        before=state,
        after=after,
        spent_status_uids=tuple(spent),
        expired_status_uids=tuple(expired),
        fresh_status_uids=tuple(fresh),
        permanent_status_uids=tuple(permanent),
    )


simulate_plan2_interval2_turn_start = (
    simulate_plan2_interval2_target_block_group_turn_start
)


Plan2Interval2TargetBlockGroupInstallTransition = Plan2Interval2InstallTransition
Plan2Interval2TargetBlockGroupFire = Plan2Interval2Fire
Plan2Interval2TargetBlockGroupExecution = Plan2Interval2Execution
Plan2Interval2TargetBlockGroupTurnStartTransition = (
    Plan2Interval2TurnStartTransition
)
Plan2Interval2TargetBlockGroupPhaseCounter = Plan2PlayCountPhaseCounter


__all__ = [
    "AcceptedPlay",
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "CARD_ID",
    "CARD_STAMINA_BY_UPGRADE",
    "CARD_UPGRADES",
    "CHILD_CARD_ID",
    "CHILD_COUNT",
    "CHILD_DESTINATION",
    "CHILD_EFFECT_ID",
    "CHILD_EFFECT_COUNT",
    "CHILD_EFFECT_TYPE",
    "CHILD_UPGRADE",
    "CO_BLOCKED_CARD_VERSION_COUNT",
    "DIRECT_CARD_VERSION_COUNT",
    "EFFECT_GROUP_TARGET",
    "EXPECTED_CHILD_GROUPS",
    "EXPECTED_SOURCE_CARD_GROUPS",
    "EXPECTED_WRAPPER_GROUPS",
    "INSTALLER_EFFECT_ID",
    "INTERVAL",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "PERMANENT_TURN",
    "PHASE_CARD_PLAY",
    "PHASE_CARD_PLAY_AFTER",
    "PHASE_PLAY_COUNT_INTERVAL",
    "PHASE_PLAY_COUNT_INTERVAL_AFTER",
    "PHASE_SEMANTIC_NUMBER",
    "PLAYING_SEARCH_ID",
    "Plan2Interval2Capture",
    "Plan2Interval2CapturedActivation",
    "Plan2Interval2Execution",
    "Plan2Interval2Fire",
    "Plan2Interval2InstallTransition",
    "Plan2Interval2Request",
    "Plan2Interval2State",
    "Plan2Interval2Target",
    "Plan2Interval2TargetBlockGroupContractError",
    "Plan2Interval2TargetBlockGroupExecution",
    "Plan2Interval2TargetBlockGroupFire",
    "Plan2Interval2TargetBlockGroupInstallTransition",
    "Plan2Interval2TargetBlockGroupPhaseCounter",
    "Plan2Interval2TargetBlockGroupProgram",
    "Plan2Interval2TargetBlockGroupRequest",
    "Plan2Interval2TargetBlockGroupStage",
    "Plan2Interval2TargetBlockGroupState",
    "Plan2Interval2TargetBlockGroupTurnStartTransition",
    "Plan2Interval2TurnStartTransition",
    "Plan2PlayCountPhaseCounter",
    "Plan2PlayingCard",
    "PlaySource",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "SNAPSHOT_BOUNDARY",
    "STATUS_ENCHANT_ID",
    "TARGET_EFFECT_GROUP_ID",
    "TARGET_SEARCH_ID",
    "TRIGGER_ID",
    "capture_plan2_interval2_target_block_group",
    "evaluate_plan2_interval2_target_block_group",
    "execute_plan2_interval2_card_start",
    "execute_plan2_interval2_target_card_start",
    "execute_plan2_interval2_target_block_group_card_start",
    "install_plan2_interval2_listener",
    "install_plan2_interval2_listener_on_state",
    "install_plan2_interval2_target_block_group_listener",
    "install_plan2_interval2_target_block_group_listener_on_state",
    "load_interval2_target_block_group_program",
    "load_plan2_interval2_program",
    "load_plan2_interval2_target_block_group_program",
    "match_interval2_target_block_group_search",
    "matches_interval2_card",
    "matches_interval2_rule",
    "matches_interval2_target_block_group_card",
    "matches_interval2_target_block_group_installer",
    "matches_interval2_target_block_group_search",
    "matches_interval2_target_block_group_trigger",
    "matches_interval2_trigger",
    "matches_plan2_interval2_target_block_group_card",
    "matches_plan2_interval2_target_block_group_installer",
    "matches_plan2_interval2_target_block_group_rule",
    "matches_plan2_interval2_target_block_group_trigger",
    "simulate_plan2_interval2_target_block_group_turn_start",
    "simulate_plan2_interval2_turn_start",
]
