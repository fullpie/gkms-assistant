"""Exact standalone Plan 2 ``ExamStatusEnchantEncore`` boundary.

This module is deliberately limited to the eight Master versions of
``p_card-02-ido-3_198`` and ``p_card-02-ido-3_201``.  It validates every
ordered card slot, the Encore installer, status/trigger row, and target-self
ForcePlay child before exposing an immutable listener runtime.

The Encore portion is executable through listener installation, native
listener-local phase counting, count spend, stable multi-listener ordering,
and creation of a GUID-bound UsePool command.  The arbitrary card transaction
is an explicit receipt boundary: CardMove/ForcePlay for card 198 and
AggressiveAdditiveFix/ForcePlay/phase-53 coverage for card 201 remain shared
co-blockers.  No central Plan 2 runtime or search adapter is called here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Final, Literal, Mapping

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    ActivePlan3StatusEnchant,
    Plan3Effect,
    Plan3StatusEnchantRule,
    load_plan3_card,
    load_plan3_effect,
    load_plan3_status_enchant,
)
from .plan3_force_play_card_search import (
    ForcePlayCardSearchResolutionError,
    ForcePlayDifference,
    ForcePlayQueuedCommand,
    load_force_play_target_card_master,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


CARD_198 = "p_card-02-ido-3_198"
CARD_201 = "p_card-02-ido-3_201"
TARGET_CARD_211 = "p_card-02-ido-3_211"

PLAN_TYPE = "ProducePlanType_Plan2"
CATEGORY = "ProduceCardCategory_MentalSkill"
COST_TYPE = "ExamCostType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"

ENCORE_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchantEncore"
ENCORE_EFFECT_TYPE_VALUE = 219
FORCE_PLAY_EFFECT_TYPE = "ProduceExamEffectType_ExamForcePlayCardSearch"
FORCE_PLAY_EFFECT_TYPE_VALUE = 24
STATUS_ENCHANT_BLOCK_GATE_EFFECT_TYPE_VALUE = 22
TRIGGER_STATUS_TYPE_VALUE = 31
STATUS_ORIGIN_TYPE = "StatusEnchant"
STATUS_ORIGIN_TYPE_VALUE = 1

ENCORE_EFFECT_198 = (
    "e_effect-exam_status_enchant_encore-0001-02-inf-"
    "enchant-p_card-02-ido-3_198-enc01"
)
ENCORE_EFFECT_201 = (
    "e_effect-exam_status_enchant_encore-0001-02-inf-"
    "enchant-p_card-02-ido-3_201-enc01"
)
ENCORE_STATUS_198 = "enchant-p_card-02-ido-3_198-enc01"
ENCORE_STATUS_201 = "enchant-p_card-02-ido-3_201-enc01"
TRIGGER_198 = (
    "e_trigger-exam_card_play_after-p_card_search-target-"
    "p_card-02-ido-3_211-0_1"
)
TRIGGER_201 = (
    "e_trigger-exam_aggressive_up_interval-5-exam_card_play_aggressive"
)
FORCE_PLAY_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-target_is_self-"
    "exam_status_enchant_encore-all-1_1"
)
TARGET_SELF_SEARCH_ID = "p_card_search-target_is_self-exam_status_enchant_encore"
TARGET_211_SEARCH_ID = "p_card_search-target-p_card-02-ido-3_211"
DECK_211_SEARCH_ID = "p_card_search-deck_all-p_card-02-ido-3_211"

PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
PHASE_AGGRESSIVE_UP_INTERVAL = "ProduceExamPhaseType_ExamAggressiveUpInterval"
PHASE_AGGRESSIVE_UP_INTERVAL_VALUE = 53
AGGRESSIVE_EFFECT_TYPE = "ProduceExamEffectType_ExamCardPlayAggressive"
TARGET_POSITION = "ProduceCardPositionType_Target"

ANDROID_VERSION = "Android v3.2.3"
ANDROID_ENCORE_CTOR = "0x7E903E4"
ANDROID_ENCORE_EXECUTE = "0x7E90600"
ANDROID_TRY_ADD_TRIGGER_STATUS = "0x7E8FEE0"
ANDROID_GET_AGGRESSIVE_INTERVAL = "0x7EA318C"
ANDROID_AGGRESSIVE_INTERVAL_PREDICATE = "0x7EA8F04"
ANDROID_IS_AGGRESSIVE_INTERVAL = "0x7E69BF0"
ANDROID_TRY_ADD_AGGRESSIVE = "0x7E995A8"
ANDROID_INCREMENT_AGGRESSIVE_PHASE_CALL = "0x7E9979C"
ANDROID_INCREMENT_PHASE = "0x7EAE874"
ANDROID_SPEND_COUNT = "0x7EAE70C"

PC_VERSION = "PC GameAssembly/IL2CPP targeted metadata"
PC_ENCORE_TYPE_INDEX = 3672
PC_ENCORE_CTOR_TOKEN = "0x060046C3"
PC_ENCORE_EXECUTE_TOKEN = "0x060046C4"
PC_TRIGGER_STATUS_TYPE_INDEX = 3854

INSTALL_OPERATIONS: tuple[str, ...] = (
    "enter-playing-card-direct-effect-slot-3",
    "require-slots-0-through-2-completed-in-master-order",
    "check-is-once-play-effect-state",
    "check-block-add-status-effect-type-22",
    "construct-non-unique-trigger-effect-status-type-31",
    "capture-exact-playing-card-object-and-guid",
    "set-origin-status-enchant-and-is-encore-enchant",
    "append-listener-after-existing-statuses",
    "append-status-enchant-difference-after-install",
)

TRIGGER_OPERATIONS: tuple[str, ...] = (
    "increment-listener-local-phase-count-before-filter-when-phase-53",
    "snapshot-matching-listeners-in-active-install-order",
    "evaluate-trigger-row-and-count-limits",
    "resolve-target-is-self-by-captured-trigger-card-guid",
    "reject-current-card-play-effect-list-containing-enum-24",
    "spend-total-and-per-turn-count-before-nested-child",
    "queue-use-pool-cost-false-manual-false-playable-count-false",
    "append-force-play-difference-after-matching-command",
    "remove-total-exhausted-listeners-after-whole-trigger-batch",
)

REPLAY_CORE_HOOKS: tuple[str, ...] = (
    "live GUID-to-zone resolution when queued UsePool executes",
    "live IsPlayable evaluation",
    "normal passive/status/card-status listener snapshots",
    "ordered direct effects with recursive difference insertion",
    "Playing transient ownership and Lost settlement",
    "card/global/turn play-count synchronization and callbacks",
    "exactly-one history event and exactly-one final move",
)

ACCOUNTING = MappingProxyType(
    {
        "affected_versions": 8,
        "direct_unblocked": 0,
        "co_blocked": 8,
    }
)


class Plan2EncoreError(ValueError):
    """Base error with a stable fail-closed code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class Plan2EncoreResolutionError(Plan2EncoreError):
    """Master/native shape is absent, changed, or outside this exact slice."""


class Plan2EncoreInputError(Plan2EncoreError):
    """Runtime input cannot safely cross the standalone boundary."""


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Plan2EncoreInputError("invalid-positive-int", label)
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Plan2EncoreInputError("invalid-nonnegative-int", label)
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan2EncoreInputError("invalid-text", label)
    return value


@dataclass(frozen=True, slots=True)
class ExactEffectRow:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str = ""
    chain_effect_id: str = ""
    search_id: str = ""
    move_position_type: str = "ProduceCardMovePositionType_Unknown"
    pick_range_type: str = "ProducePickRangeType_Unknown"
    pick_count_min: int = 0
    pick_count_max: int = 0
    effect_group_ids: tuple[str, ...] = ()


AGGRESSIVE_GROUP = ("effect_group-visible-exam_card_play_aggressive-000",)
STATUS_ENCORE_GROUP = ("effect_group-visible-exam_status_enchant_encore-000",)
STAMINA_ADD_GROUP = ("effect_group-visible-exam_stamina_consumption_add-000",)
TIMER_DRAW_GROUP = (
    "effect_group-visible-exam_effect_timer-000",
    "effect_group-visible-exam_card_draw-000",
)

EFFECT_ROWS: Mapping[str, ExactEffectRow] = MappingProxyType(
    {
        row.effect_id: row
        for row in (
            ExactEffectRow(
                "e_effect-exam_card_play_aggressive-0003",
                AGGRESSIVE_EFFECT_TYPE,
                3,
                0,
                0,
                0,
                effect_group_ids=AGGRESSIVE_GROUP,
            ),
            ExactEffectRow(
                "e_effect-exam_card_play_aggressive-0004",
                AGGRESSIVE_EFFECT_TYPE,
                4,
                0,
                0,
                0,
                effect_group_ids=AGGRESSIVE_GROUP,
            ),
            ExactEffectRow(
                "e_effect-exam_card_play_aggressive-0005",
                AGGRESSIVE_EFFECT_TYPE,
                5,
                0,
                0,
                0,
                effect_group_ids=AGGRESSIVE_GROUP,
            ),
            ExactEffectRow(
                "e_effect-exam_card_move-p_card_search-deck_all-"
                "p_card-02-ido-3_211-deck_first-all-0_0",
                "ProduceExamEffectType_ExamCardMove",
                0,
                0,
                0,
                0,
                search_id=DECK_211_SEARCH_ID,
                move_position_type="ProduceCardMovePositionType_DeckFirst",
                pick_range_type="ProducePickRangeType_All",
            ),
            ExactEffectRow(
                "e_effect-exam_stamina_consumption_add-01",
                "ProduceExamEffectType_ExamStaminaConsumptionAdd",
                0,
                0,
                0,
                1,
                effect_group_ids=STAMINA_ADD_GROUP,
            ),
            ExactEffectRow(
                "e_effect-exam_aggressive_additive_fix-0001-02",
                "ProduceExamEffectType_ExamAggressiveAdditiveFix",
                1,
                0,
                0,
                2,
                effect_group_ids=AGGRESSIVE_GROUP,
            ),
            ExactEffectRow(
                "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
                "ProduceExamEffectType_ExamEffectTimer",
                1,
                0,
                1,
                0,
                chain_effect_id="e_effect-exam_card_draw-0001",
                effect_group_ids=TIMER_DRAW_GROUP,
            ),
            ExactEffectRow(
                "e_effect-exam_card_draw-0001",
                "ProduceExamEffectType_ExamCardDraw",
                1,
                0,
                0,
                0,
                effect_group_ids=("effect_group-visible-exam_card_draw-000",),
            ),
            ExactEffectRow(
                ENCORE_EFFECT_198,
                ENCORE_EFFECT_TYPE,
                1,
                0,
                2,
                -1,
                status_enchant_id=ENCORE_STATUS_198,
                effect_group_ids=STATUS_ENCORE_GROUP,
            ),
            ExactEffectRow(
                ENCORE_EFFECT_201,
                ENCORE_EFFECT_TYPE,
                1,
                0,
                2,
                -1,
                status_enchant_id=ENCORE_STATUS_201,
                effect_group_ids=STATUS_ENCORE_GROUP,
            ),
            ExactEffectRow(
                FORCE_PLAY_EFFECT_ID,
                FORCE_PLAY_EFFECT_TYPE,
                0,
                0,
                0,
                0,
                search_id=TARGET_SELF_SEARCH_ID,
                pick_range_type="ProducePickRangeType_All",
                pick_count_min=1,
                pick_count_max=1,
            ),
        )
    }
)


@dataclass(frozen=True, slots=True)
class ExactCardEffectSlot:
    slot: int
    effect_id: str
    trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False


@dataclass(frozen=True, slots=True)
class AffectedPlan2EncoreVersion:
    card_id: str
    upgrade: int
    stamina: int
    effect_group_ids: tuple[str, ...]
    effect_slots: tuple[ExactCardEffectSlot, ...]
    encore_effect_id: str
    status_enchant_id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]
    co_blockers: tuple[str, ...]

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def installer_prefix(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[:3]


CARD_198_GROUPS = (
    "effect_group-visible-exam_status_enchant_encore-000",
    "effect_group-visible-exam_stamina_consumption_add-000",
    "effect_group-visible-exam_card_play_aggressive-000",
)
CARD_201_GROUPS = (
    "effect_group-visible-exam_effect_timer-000",
    "effect_group-visible-exam_card_draw-000",
    "effect_group-visible-exam_status_enchant_encore-000",
    "effect_group-visible-exam_card_play_aggressive-000",
)


def _version_198(upgrade: int, stamina: int) -> AffectedPlan2EncoreVersion:
    aggressive = (
        "e_effect-exam_card_play_aggressive-0004"
        if upgrade == 0
        else "e_effect-exam_card_play_aggressive-0005"
    )
    return AffectedPlan2EncoreVersion(
        CARD_198,
        upgrade,
        stamina,
        CARD_198_GROUPS,
        (
            ExactCardEffectSlot(0, aggressive),
            ExactCardEffectSlot(
                1,
                "e_effect-exam_card_move-p_card_search-deck_all-"
                "p_card-02-ido-3_211-deck_first-all-0_0",
            ),
            ExactCardEffectSlot(2, "e_effect-exam_stamina_consumption_add-01"),
            ExactCardEffectSlot(3, ENCORE_EFFECT_198, is_once_play_effect=True),
        ),
        ENCORE_EFFECT_198,
        ENCORE_STATUS_198,
        TRIGGER_198,
        (FORCE_PLAY_EFFECT_ID,),
        ("ExamCardMove", "ExamForcePlayCardSearch"),
    )


def _version_201(upgrade: int, stamina: int) -> AffectedPlan2EncoreVersion:
    return AffectedPlan2EncoreVersion(
        CARD_201,
        upgrade,
        stamina,
        CARD_201_GROUPS,
        (
            ExactCardEffectSlot(0, "e_effect-exam_card_play_aggressive-0003"),
            ExactCardEffectSlot(1, "e_effect-exam_aggressive_additive_fix-0001-02"),
            ExactCardEffectSlot(
                2,
                "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001",
            ),
            ExactCardEffectSlot(3, ENCORE_EFFECT_201, is_once_play_effect=True),
        ),
        ENCORE_EFFECT_201,
        ENCORE_STATUS_201,
        TRIGGER_201,
        (FORCE_PLAY_EFFECT_ID,),
        (
            "ExamAggressiveAdditiveFix",
            "ExamForcePlayCardSearch",
            "status-trigger:ExamAggressiveUpInterval:5",
        ),
    )


AFFECTED_CARD_VERSIONS: tuple[AffectedPlan2EncoreVersion, ...] = (
    *(_version_198(upgrade, stamina) for upgrade, stamina in enumerate((3, 3, 2, 1))),
    *(_version_201(upgrade, stamina) for upgrade, stamina in enumerate((5, 2, 1, 0))),
)
VERSION_BY_REF: Mapping[tuple[str, int], AffectedPlan2EncoreVersion] = MappingProxyType(
    {(item.card_id, item.upgrade): item for item in AFFECTED_CARD_VERSIONS}
)


@dataclass(frozen=True, slots=True)
class Plan2EncoreContract:
    card_id: str
    effect: Plan3Effect
    rule: Plan3StatusEnchantRule
    target_self_search: ProduceCardSearchRule
    activation_search: ProduceCardSearchRule | None
    unresolved_reasons: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved_reasons


@dataclass(frozen=True, slots=True)
class Plan2EncoreCatalog:
    versions: tuple[AffectedPlan2EncoreVersion, ...]
    contracts: tuple[Plan2EncoreContract, ...]

    def version(self, card_id: str, upgrade: int) -> AffectedPlan2EncoreVersion:
        try:
            return next(
                row
                for row in self.versions
                if row.card_id == card_id and row.upgrade == upgrade
            )
        except StopIteration as error:
            raise Plan2EncoreResolutionError(
                "card-version-outside-exact-bundle", f"{card_id}+{upgrade}"
            ) from error

    def contract(self, card_id: str) -> Plan2EncoreContract:
        try:
            return next(row for row in self.contracts if row.card_id == card_id)
        except StopIteration as error:
            raise Plan2EncoreResolutionError(
                "encore-contract-outside-exact-bundle", card_id
            ) from error


_EFFECT_GAMEPLAY_DEFAULTS: Final[Mapping[str, object]] = MappingProxyType(
    {
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
        "chainProduceExamEffectIds": (),
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": (),
        "effectGroupIds": (),
    }
)


def _json_tuple(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise Plan2EncoreResolutionError("master-json-array-invalid", label)
    return tuple(value)


def _validate_effect_row(
    connection: sqlite3.Connection, expected: ExactEffectRow
) -> None:
    row = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (expected.effect_id,)
    ).fetchone()
    if row is None:
        raise Plan2EncoreResolutionError("effect-row-missing", expected.effect_id)
    scalar = (
        row["id"],
        row["effect_type"],
        row["value1"],
        row["value2"],
        row["effect_count"],
        row["effect_turn"],
        row["status_enchant_id"],
        row["chain_effect_id"],
    )
    expected_scalar = (
        expected.effect_id,
        expected.effect_type,
        expected.value1,
        expected.value2,
        expected.effect_count,
        expected.effect_turn,
        expected.status_enchant_id,
        expected.chain_effect_id,
    )
    if scalar != expected_scalar:
        raise Plan2EncoreResolutionError("effect-row-scalar-drift", expected.effect_id)
    try:
        raw = json.loads(row["raw_json"])
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2EncoreResolutionError("effect-row-json-invalid", expected.effect_id) from error
    if not isinstance(raw, dict):
        raise Plan2EncoreResolutionError("effect-row-json-invalid", expected.effect_id)
    raw_scalar = {
        "id": expected.effect_id,
        "effectType": expected.effect_type,
        "effectValue1": expected.value1,
        "effectValue2": expected.value2,
        "effectCount": expected.effect_count,
        "effectTurn": expected.effect_turn,
    }
    if any(raw.get(key) != value for key, value in raw_scalar.items()):
        raise Plan2EncoreResolutionError(
            "effect-row-raw-scalar-drift", expected.effect_id
        )
    allowed_keys = {
        *raw_scalar,
        *_EFFECT_GAMEPLAY_DEFAULTS,
        "produceDescriptions",
        "customizeProduceDescriptions",
    }
    unknown_keys = set(raw) - allowed_keys
    if unknown_keys:
        raise Plan2EncoreResolutionError(
            "effect-row-unknown-gameplay-shape",
            f"{expected.effect_id}:{sorted(unknown_keys)[0]}",
        )
    projected: dict[str, object] = {}
    for key, default in _EFFECT_GAMEPLAY_DEFAULTS.items():
        if key not in raw:
            raise Plan2EncoreResolutionError(
                "effect-row-gameplay-field-missing", f"{expected.effect_id}:{key}"
            )
        value = raw[key]
        projected[key] = _json_tuple(value, f"{expected.effect_id}:{key}") if isinstance(default, tuple) else value
    wanted = dict(_EFFECT_GAMEPLAY_DEFAULTS)
    wanted.update(
        {
            "produceCardSearchId": expected.search_id,
            "movePositionType": expected.move_position_type,
            "pickRangeType": expected.pick_range_type,
            "pickCountMin": expected.pick_count_min,
            "pickCountMax": expected.pick_count_max,
            "chainProduceExamEffectId": expected.chain_effect_id,
            "produceExamStatusEnchantId": expected.status_enchant_id,
            "effectGroupIds": expected.effect_group_ids,
        }
    )
    if projected != wanted:
        raise Plan2EncoreResolutionError("effect-row-gameplay-shape-drift", expected.effect_id)


def _search_projection(search: ProduceCardSearchRule) -> tuple[object, ...]:
    return (
        search.id,
        search.card_rarities,
        search.produce_card_ids,
        search.upgrade_counts,
        search.plan_type,
        search.card_categories,
        search.card_status_type,
        search.order_type,
        search.card_position_type,
        search.card_search_tag,
        search.produce_card_random_pool_id,
        search.limit_count,
        search.stamina_min_max_type,
        search.stamina_min,
        search.stamina_max,
        search.exam_effect_type,
        search.effect_group_ids,
        search.is_self,
        search.produce_card_pool_id,
        search.cost_type,
        search.is_customized,
    )


def _expected_search(
    search_id: str,
    *,
    card_ids: tuple[str, ...] = (),
    position: str = TARGET_POSITION,
    is_self: bool = False,
) -> tuple[object, ...]:
    return (
        search_id,
        (),
        card_ids,
        (),
        "ProducePlanType_Unknown",
        (),
        "ProduceCardSearchStatusType_Unknown",
        "ProduceCardOrderType_Unknown",
        position,
        "",
        "",
        0,
        "ConditionMinMaxType_Unknown",
        0,
        0,
        "ProduceExamEffectType_Unknown",
        (),
        is_self,
        "",
        "ExamCostType_Unknown",
        False,
    )


def _contract_shape_errors(contract: Plan2EncoreContract) -> tuple[str, ...]:
    reasons: list[str] = list(contract.unresolved_reasons)
    expected_status = ENCORE_STATUS_198 if contract.card_id == CARD_198 else ENCORE_STATUS_201
    expected_effect = ENCORE_EFFECT_198 if contract.card_id == CARD_198 else ENCORE_EFFECT_201
    expected_trigger = TRIGGER_198 if contract.card_id == CARD_198 else TRIGGER_201
    effect = contract.effect
    if (
        effect.id,
        effect.effect_type,
        effect.value1,
        effect.value2,
        effect.effect_count,
        effect.effect_turn,
        effect.status_enchant_id,
        effect.chain_effect_id,
        effect.chain_effect_ids,
        effect.trigger,
        effect.once,
        effect.card_move_rule,
    ) != (
        expected_effect,
        ENCORE_EFFECT_TYPE,
        1,
        0,
        2,
        -1,
        expected_status,
        "",
        (),
        None,
        False,
        None,
    ) or effect.status_enchant != contract.rule:
        reasons.append("encore-effect-shape")
    rule = contract.rule
    trigger = rule.trigger
    if rule.id != expected_status or trigger.id != expected_trigger:
        reasons.append("status-or-trigger-id")
    if len(rule.effects) != 1:
        reasons.append("child-effect-count")
    else:
        child = rule.effects[0]
        if (
            child.id,
            child.effect_type,
            child.value1,
            child.value2,
            child.effect_count,
            child.effect_turn,
            child.status_enchant_id,
            child.chain_effect_id,
            child.chain_effect_ids,
            child.status_enchant,
            child.chain_effect,
            child.trigger,
            child.once,
        ) != (
            FORCE_PLAY_EFFECT_ID,
            FORCE_PLAY_EFFECT_TYPE,
            0,
            0,
            0,
            0,
            "",
            "",
            (),
            None,
            None,
            None,
            False,
        ):
            reasons.append("child-effect-shape")
    neutral = (
        (),
        (),
        (),
        "ProduceCardMovePositionType_Unknown",
        "ProduceStepLessonType_Unknown",
    )
    if contract.card_id == CARD_198:
        actual = (
            trigger.phase_values,
            trigger.field_check_types,
            trigger.field_types,
            trigger.card_move_position_type,
            trigger.lesson_type,
        )
        if (
            trigger.phase_types != (PHASE_CARD_PLAY_AFTER,)
            or actual != neutral
            or trigger.field_values
            or trigger.field_card_search_ids
            or trigger.produce_card_search_id != TARGET_211_SEARCH_ID
            or trigger.upper_search_count != 0
            or trigger.lower_search_count != 1
            or trigger.effect_types
        ):
            reasons.append("card-play-after-trigger-shape")
        if contract.activation_search is None or _search_projection(
            contract.activation_search
        ) != _expected_search(
            TARGET_211_SEARCH_ID, card_ids=(TARGET_CARD_211,)
        ):
            reasons.append("card-play-after-search-shape")
    else:
        if (
            trigger.phase_types != (PHASE_AGGRESSIVE_UP_INTERVAL,)
            or trigger.phase_values != (5,)
            or trigger.field_check_types
            or trigger.field_types
            or trigger.field_values
            or trigger.field_card_search_ids
            or trigger.produce_card_search_id
            or trigger.upper_search_count != 0
            or trigger.lower_search_count != 0
            or trigger.card_move_position_type != "ProduceCardMovePositionType_Unknown"
            or trigger.effect_types != (AGGRESSIVE_EFFECT_TYPE,)
            or trigger.lesson_type != "ProduceStepLessonType_Unknown"
            or contract.activation_search is not None
        ):
            reasons.append("aggressive-interval-trigger-shape")
    if _search_projection(contract.target_self_search) != _expected_search(
        TARGET_SELF_SEARCH_ID, is_self=True
    ):
        reasons.append("target-is-self-search-shape")
    return tuple(dict.fromkeys(reasons))


def load_plan2_status_enchant_encore_catalog(
    database: Path = DEFAULT_DATABASE,
) -> Plan2EncoreCatalog:
    """Load and strictly validate all eight exact Master versions."""

    database = Path(database)
    try:
        target_self = load_produce_card_search(TARGET_SELF_SEARCH_ID, database)
        activation_198 = load_produce_card_search(TARGET_211_SEARCH_ID, database)
        deck_211 = load_produce_card_search(DECK_211_SEARCH_ID, database)
        effect_198 = load_plan3_effect(ENCORE_EFFECT_198, database)
        effect_201 = load_plan3_effect(ENCORE_EFFECT_201, database)
        rule_198 = load_plan3_status_enchant(ENCORE_STATUS_198, database)
        rule_201 = load_plan3_status_enchant(ENCORE_STATUS_201, database)
    except (KeyError, OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise Plan2EncoreResolutionError("encore-master-unavailable") from error
    if _search_projection(deck_211) != _expected_search(
        DECK_211_SEARCH_ID,
        card_ids=(TARGET_CARD_211,),
        position="ProduceCardPositionType_DeckAll",
    ):
        raise Plan2EncoreResolutionError("deck-card-move-search-shape-drift")
    contracts = (
        Plan2EncoreContract(CARD_198, effect_198, rule_198, target_self, activation_198),
        Plan2EncoreContract(CARD_201, effect_201, rule_201, target_self, None),
    )
    for contract in contracts:
        errors = _contract_shape_errors(contract)
        if errors:
            raise Plan2EncoreResolutionError("encore-contract-shape-drift", errors[0])
    try:
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            for effect in EFFECT_ROWS.values():
                _validate_effect_row(connection, effect)
            for expected in AFFECTED_CARD_VERSIONS:
                card = load_plan3_card(expected.card_id, expected.upgrade, database)
                if (
                    card.plan_type,
                    card.category,
                    card.stamina_cost,
                    card.force_stamina_cost,
                    card.cost_type,
                    card.cost_value,
                    card.play_trigger,
                    card.move_position_type,
                    card.effect_group_ids,
                ) != (
                    PLAN_TYPE,
                    CATEGORY,
                    expected.stamina,
                    0,
                    COST_TYPE,
                    0,
                    None,
                    MOVE_LOST,
                    expected.effect_group_ids,
                ):
                    raise Plan2EncoreResolutionError(
                        "card-row-scalar-drift", f"{expected.card_id}+{expected.upgrade}"
                    )
                row = connection.execute(
                    "SELECT play_effects_json FROM card WHERE id = ? AND upgrade_count = ?",
                    (expected.card_id, expected.upgrade),
                ).fetchone()
                if row is None:
                    raise Plan2EncoreResolutionError("card-row-missing")
                raw_slots = json.loads(row["play_effects_json"])
                actual_slots = tuple(
                    ExactCardEffectSlot(
                        index,
                        item["produceExamEffectId"],
                        item["produceExamTriggerId"],
                        item["hideIcon"],
                        item["isOncePlayEffect"],
                    )
                    for index, item in enumerate(raw_slots)
                    if isinstance(item, dict)
                    and set(item)
                    == {
                        "produceExamTriggerId",
                        "produceExamEffectId",
                        "hideIcon",
                        "isOncePlayEffect",
                    }
                )
                if len(actual_slots) != len(raw_slots) or actual_slots != expected.effect_slots:
                    raise Plan2EncoreResolutionError(
                        "card-effect-slot-shape-drift",
                        f"{expected.card_id}+{expected.upgrade}",
                    )
                if tuple(effect.id for effect in card.effects) != expected.ordered_effect_ids:
                    raise Plan2EncoreResolutionError("card-effect-loader-order-drift")
    except (json.JSONDecodeError, KeyError, OSError, sqlite3.Error, TypeError) as error:
        if isinstance(error, Plan2EncoreError):
            raise
        raise Plan2EncoreResolutionError("encore-master-validation-failed") from error
    return Plan2EncoreCatalog(AFFECTED_CARD_VERSIONS, contracts)


PlayOrigin = Literal["normal", "forced", "extra"]


@dataclass(frozen=True, slots=True)
class Plan2EncoreListener:
    uid: int
    captured_card: Plan3NativeCard
    active: ActivePlan3StatusEnchant
    install_sequence: int
    install_origin: PlayOrigin
    encore_effect_id: str
    is_encore_enchant: bool = True
    origin_type: str = STATUS_ORIGIN_TYPE
    origin_type_value: int = STATUS_ORIGIN_TYPE_VALUE
    status_type_value: int = TRIGGER_STATUS_TYPE_VALUE
    effect_ids: tuple[str, ...] | None = None

    @property
    def captured_guid(self) -> str:
        return self.captured_card.guid

    @property
    def total_remaining(self) -> int:
        return self.active.max_uses - self.active.uses

    @property
    def per_turn_remaining(self) -> int:
        return self.active.max_uses_per_turn - self.active.uses_this_turn


@dataclass(frozen=True, slots=True)
class Plan2EncoreInstallDifference:
    trigger_status_uid: int
    source_guid: str
    effect_type: str = ENCORE_EFFECT_TYPE
    effect_type_value: int = ENCORE_EFFECT_TYPE_VALUE
    preview_value: int = 0
    current_value: int = 0
    is_consume: bool = False
    appended_after_listener_install: bool = True


@dataclass(frozen=True, slots=True)
class Plan2EncoreReplayCommand:
    handoff_id: str
    force_play: ForcePlayQueuedCommand
    listener_uid: int
    captured_guid: str
    encore_effect_id: str
    current_play_effect_ids: tuple[str, ...]
    expected_core_effect_ids: tuple[str, ...]
    skipped_once_effect_ids: tuple[str, ...]
    ancestry: tuple[tuple[int, str], ...]
    rng_consumed: bool = False
    target_search_position_type: str = TARGET_POSITION
    nested_effect_id: str = FORCE_PLAY_EFFECT_ID
    play_origin: PlayOrigin = "forced"
    core_hook_requirements: tuple[str, ...] = REPLAY_CORE_HOOKS


@dataclass(frozen=True, slots=True)
class Plan2EncorePlayHistory:
    transaction_id: str
    guid: str
    card_id: str
    upgrade: int
    play_origin: PlayOrigin
    source_zone: str
    destination_zone: str
    card_count_before: int
    card_count_after: int
    global_count_before: int
    global_count_after: int
    turn_count_before: int
    turn_count_after: int
    random_state_before: int
    random_state_after: int
    once_effect_outcome: Literal["installed", "skipped-once"]
    history_event_count: int = 1
    final_move_count: int = 1


@dataclass(frozen=True, slots=True)
class Plan2EncoreRuntime:
    catalog: Plan2EncoreCatalog
    native_state: Plan3NativeState
    listeners: tuple[Plan2EncoreListener, ...] = ()
    once_consumed: tuple[tuple[str, str], ...] = ()
    pending_handoffs: tuple[Plan2EncoreReplayCommand, ...] = ()
    committed_transaction_ids: tuple[str, ...] = ()
    card_play_counts: tuple[tuple[str, int], ...] = ()
    global_play_count: int = 0
    turn_play_count: int = 0
    history: tuple[Plan2EncorePlayHistory, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, Plan2EncoreCatalog):
            raise Plan2EncoreInputError("invalid-catalog")
        if not isinstance(self.native_state, Plan3NativeState):
            raise Plan2EncoreInputError("invalid-native-state")
        listener_uids = tuple(listener.uid for listener in self.listeners)
        if len(set(listener_uids)) != len(listener_uids):
            raise Plan2EncoreInputError("duplicate-listener-uid")
        sequences = tuple(listener.install_sequence for listener in self.listeners)
        if len(set(sequences)) != len(sequences) or sequences != tuple(sorted(sequences)):
            raise Plan2EncoreInputError("listener-install-order-invalid")
        if len(set(self.once_consumed)) != len(self.once_consumed):
            raise Plan2EncoreInputError("duplicate-once-consumed-key")
        pending_ids = tuple(command.handoff_id for command in self.pending_handoffs)
        if len(set(pending_ids)) != len(pending_ids):
            raise Plan2EncoreInputError("duplicate-pending-handoff")
        if set(pending_ids) & set(self.committed_transaction_ids):
            raise Plan2EncoreInputError("pending-committed-overlap")
        if len(set(self.committed_transaction_ids)) != len(self.committed_transaction_ids):
            raise Plan2EncoreInputError("duplicate-committed-transaction")
        count_guids = tuple(guid for guid, _count in self.card_play_counts)
        if len(set(count_guids)) != len(count_guids):
            raise Plan2EncoreInputError("duplicate-card-count-guid")
        for guid, count in self.card_play_counts:
            _text(guid, "card_play_counts.guid")
            _nonnegative_int(count, "card_play_counts.count")
        _nonnegative_int(self.global_play_count, "global_play_count")
        _nonnegative_int(self.turn_play_count, "turn_play_count")

    def card_count(self, guid: str) -> int:
        return next((count for stored, count in self.card_play_counts if stored == guid), 0)


def new_plan2_encore_runtime(
    native_state: Plan3NativeState,
    *,
    catalog: Plan2EncoreCatalog | None = None,
    global_play_count: int = 0,
    turn_play_count: int = 0,
) -> Plan2EncoreRuntime:
    if not isinstance(native_state, Plan3NativeState):
        raise Plan2EncoreInputError("invalid-native-state")
    resolved_catalog = catalog or load_plan2_status_enchant_encore_catalog()
    counts = tuple((card.guid, card.play_count) for card in native_state.all_cards)
    return Plan2EncoreRuntime(
        resolved_catalog,
        native_state,
        card_play_counts=counts,
        global_play_count=global_play_count,
        turn_play_count=turn_play_count,
    )


@dataclass(frozen=True, slots=True)
class Plan2EncoreInstallInput:
    playing_card: Plan3NativeCard
    status_uid: int
    play_origin: PlayOrigin = "normal"
    completed_effect_ids: tuple[str, ...] = ()
    current_effect_slot: int = 3
    status_enchant_blocked: bool = False


@dataclass(frozen=True, slots=True)
class Plan2EncoreInstallResult:
    before: Plan2EncoreRuntime
    after: Plan2EncoreRuntime
    supplied: Plan2EncoreInstallInput
    resolved: bool
    installed: bool
    skipped_once: bool
    reason: str
    listener: Plan2EncoreListener | None
    differences: tuple[Plan2EncoreInstallDifference, ...]
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.resolved and self.after is not self.before:
            raise Plan2EncoreInputError("unresolved-install-mutated-runtime")
        if not self.installed and (self.listener is not None or self.differences):
            raise Plan2EncoreInputError("non-installed-result-has-output")


def _install_result(
    runtime: Plan2EncoreRuntime,
    supplied: Plan2EncoreInstallInput,
    *,
    resolved: bool,
    installed: bool = False,
    skipped_once: bool = False,
    reason: str = "",
    after: Plan2EncoreRuntime | None = None,
    listener: Plan2EncoreListener | None = None,
    differences: tuple[Plan2EncoreInstallDifference, ...] = (),
    operations: tuple[str, ...] = (),
) -> Plan2EncoreInstallResult:
    return Plan2EncoreInstallResult(
        runtime,
        after or runtime,
        supplied,
        resolved,
        installed,
        skipped_once,
        reason,
        listener,
        differences,
        operations,
    )


def install_plan2_encore_listener(
    runtime: Plan2EncoreRuntime, supplied: Plan2EncoreInstallInput
) -> Plan2EncoreInstallResult:
    """Install slot 3 after the exact three preceding direct effects."""

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise Plan2EncoreInputError("invalid-runtime")
    if not isinstance(supplied, Plan2EncoreInstallInput):
        raise Plan2EncoreInputError("invalid-install-input")
    if not isinstance(supplied.playing_card, Plan3NativeCard):
        return _install_result(runtime, supplied, resolved=False, reason="playing-card-unavailable")
    if supplied.play_origin not in ("normal", "forced", "extra"):
        return _install_result(runtime, supplied, resolved=False, reason="unknown-play-origin")
    try:
        version = runtime.catalog.version(
            supplied.playing_card.card_id, supplied.playing_card.effective_upgrade
        )
        contract = runtime.catalog.contract(supplied.playing_card.card_id)
        uid = _positive_int(supplied.status_uid, "status_uid")
    except Plan2EncoreError as error:
        return _install_result(runtime, supplied, resolved=False, reason=error.code)
    errors = _contract_shape_errors(contract)
    if errors:
        return _install_result(
            runtime,
            supplied,
            resolved=False,
            reason=f"encore-contract-unresolved:{errors[0]}",
        )
    once_key = (supplied.playing_card.guid, version.encore_effect_id)
    if once_key in runtime.once_consumed:
        return _install_result(
            runtime,
            supplied,
            resolved=True,
            skipped_once=True,
            reason="once-play-effect-already-consumed",
            operations=("skip-consumed-once-installer-without-new-listener",),
        )
    if supplied.current_effect_slot != 3 or supplied.completed_effect_ids != version.installer_prefix:
        return _install_result(
            runtime,
            supplied,
            resolved=False,
            reason="installer-phase-or-effect-order-mismatch",
        )
    if supplied.status_enchant_blocked:
        return _install_result(
            runtime, supplied, resolved=False, reason="status-enchant-add-blocked"
        )
    if uid in {listener.uid for listener in runtime.listeners}:
        return _install_result(runtime, supplied, resolved=False, reason="status-uid-collision")
    sequence = 1 + max(
        (listener.install_sequence for listener in runtime.listeners), default=0
    )
    active = ActivePlan3StatusEnchant(
        instance_id=f"plan2-encore-status-uid:{uid}",
        source_id=supplied.playing_card.card_id,
        rule=contract.rule,
        max_uses=2,
        max_uses_per_turn=1,
        remaining_turns=-1,
        is_item_direct=False,
        native_uid=uid,
        captured_card_guid=supplied.playing_card.guid,
        is_encore_enchant=True,
    )
    listener = Plan2EncoreListener(
        uid,
        supplied.playing_card,
        active,
        sequence,
        supplied.play_origin,
        version.encore_effect_id,
    )
    after = replace(
        runtime,
        listeners=(*runtime.listeners, listener),
        once_consumed=(*runtime.once_consumed, once_key),
    )
    difference = Plan2EncoreInstallDifference(uid, supplied.playing_card.guid)
    return _install_result(
        runtime,
        supplied,
        resolved=True,
        installed=True,
        after=after,
        listener=listener,
        differences=(difference,),
        operations=INSTALL_OPERATIONS,
    )


@dataclass(frozen=True, slots=True)
class Plan2EncoreTriggerEvent:
    event_id: str
    phase: str
    effect_type: str = ""
    target_card: Plan3NativeCard | None = None
    transient_playing_cards: tuple[Plan3NativeCard, ...] = ()
    replay_ancestry: tuple[tuple[int, str], ...] = ()
    positive_occurrence_count: int = 1
    max_cycle_depth: int = 32


@dataclass(frozen=True, slots=True)
class Plan2EncoreTriggerResult:
    before: Plan2EncoreRuntime
    after: Plan2EncoreRuntime
    supplied: Plan2EncoreTriggerEvent
    resolved: bool
    triggered: bool
    reason: str
    commands: tuple[Plan2EncoreReplayCommand, ...]
    differences: tuple[ForcePlayDifference, ...]
    spent_listener_uids: tuple[int, ...]
    removed_listener_uids: tuple[int, ...]
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.after.native_state is not self.before.native_state:
            raise Plan2EncoreInputError("trigger-planner-mutated-native-state")
        if not self.resolved and self.after is not self.before:
            raise Plan2EncoreInputError("unresolved-trigger-mutated-runtime")
        if not self.resolved and (self.commands or self.differences):
            raise Plan2EncoreInputError("unresolved-trigger-has-output")

    @property
    def rng_consumed(self) -> bool:
        return False

    @property
    def core_hook_required(self) -> bool:
        return bool(self.commands)


def _trigger_result(
    runtime: Plan2EncoreRuntime,
    supplied: Plan2EncoreTriggerEvent,
    *,
    resolved: bool,
    after: Plan2EncoreRuntime | None = None,
    triggered: bool = False,
    reason: str = "",
    commands: tuple[Plan2EncoreReplayCommand, ...] = (),
    differences: tuple[ForcePlayDifference, ...] = (),
    spent: tuple[int, ...] = (),
    removed: tuple[int, ...] = (),
    operations: tuple[str, ...] = (),
) -> Plan2EncoreTriggerResult:
    return Plan2EncoreTriggerResult(
        runtime,
        after or runtime,
        supplied,
        resolved,
        triggered,
        reason,
        commands,
        differences,
        spent,
        removed,
        operations,
    )


def _set_phase_count(
    listener: Plan2EncoreListener, phase: str, count: int
) -> Plan2EncoreListener:
    values = dict(listener.active.phase_counts)
    values[phase] = count
    active = replace(listener.active, phase_counts=tuple(sorted(values.items())))
    return replace(listener, active=active)


def _locate_captured_card(
    runtime: Plan2EncoreRuntime,
    listener: Plan2EncoreListener,
    transient: tuple[Plan3NativeCard, ...],
) -> tuple[str, int, Plan3NativeCard]:
    matches: list[tuple[str, int, Plan3NativeCard]] = []
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        matches.extend(
            (zone, index, card)
            for index, card in enumerate(getattr(runtime.native_state, zone))
            if card.guid == listener.captured_guid
        )
    matches.extend(
        ("playing-transient", index, card)
        for index, card in enumerate(transient)
        if card.guid == listener.captured_guid
    )
    if len(matches) != 1:
        raise Plan2EncoreResolutionError(
            "captured-guid-missing" if not matches else "captured-guid-ambiguous",
            listener.captured_guid,
        )
    zone, index, card = matches[0]
    captured = listener.captured_card
    if (
        card.guid,
        card.card_id,
        card.base_upgrade,
        card.temporary_upgrade,
        card.effective_upgrade,
        card.runtime_customize_ids,
        card.plan2_runtime_customization,
        card.runtime_customize_lesson_depend_review_add,
    ) != (
        captured.guid,
        captured.card_id,
        captured.base_upgrade,
        captured.temporary_upgrade,
        captured.effective_upgrade,
        captured.runtime_customize_ids,
        captured.plan2_runtime_customization,
        captured.runtime_customize_lesson_depend_review_add,
    ):
        raise Plan2EncoreResolutionError("captured-card-identity-drift", card.guid)
    return zone, index, card


def plan_plan2_encore_trigger(
    runtime: Plan2EncoreRuntime,
    supplied: Plan2EncoreTriggerEvent,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2EncoreTriggerResult:
    """Advance one exact native trigger occurrence and queue Encore children."""

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise Plan2EncoreInputError("invalid-runtime")
    if not isinstance(supplied, Plan2EncoreTriggerEvent):
        raise Plan2EncoreInputError("invalid-trigger-event")
    try:
        _text(supplied.event_id, "event_id")
        _positive_int(supplied.max_cycle_depth, "max_cycle_depth")
    except Plan2EncoreError as error:
        return _trigger_result(runtime, supplied, resolved=False, reason=error.code)
    if supplied.phase not in (PHASE_CARD_PLAY_AFTER, PHASE_AGGRESSIVE_UP_INTERVAL):
        return _trigger_result(runtime, supplied, resolved=False, reason="unknown-event-phase")
    if len(supplied.replay_ancestry) >= supplied.max_cycle_depth:
        return _trigger_result(runtime, supplied, resolved=False, reason="cycle-depth-limit")
    if len(set(supplied.replay_ancestry)) != len(supplied.replay_ancestry):
        return _trigger_result(runtime, supplied, resolved=False, reason="cyclic-replay-ancestry")
    transient_guids = tuple(card.guid for card in supplied.transient_playing_cards)
    if len(set(transient_guids)) != len(transient_guids):
        return _trigger_result(runtime, supplied, resolved=False, reason="duplicate-transient-guid")

    working = runtime.listeners
    family_card_id: str
    if supplied.phase == PHASE_CARD_PLAY_AFTER:
        family_card_id = CARD_198
        if supplied.target_card is None or supplied.target_card.card_id != TARGET_CARD_211:
            return _trigger_result(
                runtime,
                supplied,
                resolved=True,
                reason="target-card-search-not-matched",
                operations=("evaluate-target-p_card-02-ido-3_211-search:false",),
            )
    else:
        family_card_id = CARD_201
        if supplied.effect_type != AGGRESSIVE_EFFECT_TYPE:
            return _trigger_result(
                runtime,
                supplied,
                resolved=True,
                reason="aggressive-effect-type-not-matched",
                operations=("phase-53-effect-type-filter:false",),
            )
        if supplied.positive_occurrence_count != 1:
            return _trigger_result(
                runtime,
                supplied,
                resolved=False,
                reason="phase-53-requires-one-positive-effect-occurrence",
            )
        working = tuple(
            _set_phase_count(
                listener,
                PHASE_AGGRESSIVE_UP_INTERVAL,
                listener.active.phase_count(PHASE_AGGRESSIVE_UP_INTERVAL) + 1,
            )
            if listener.captured_card.card_id == CARD_201
            else listener
            for listener in working
        )

    candidates: list[Plan2EncoreListener] = []
    for listener in working:
        if listener.captured_card.card_id != family_card_id:
            continue
        if listener.active.uses >= listener.active.max_uses:
            continue
        if listener.active.uses_this_turn >= listener.active.max_uses_per_turn:
            continue
        if family_card_id == CARD_201:
            count = listener.active.phase_count(PHASE_AGGRESSIVE_UP_INTERVAL)
            if count < 1 or count % 5:
                continue
        candidates.append(listener)

    if not candidates:
        after = replace(runtime, listeners=working) if working != runtime.listeners else runtime
        return _trigger_result(
            runtime,
            supplied,
            resolved=True,
            after=after,
            reason="no-matching-available-listener",
            operations=(
                "increment-listener-local-phase-count-before-filter"
                if supplied.phase == PHASE_AGGRESSIVE_UP_INTERVAL
                else "snapshot-card-play-after-listeners",
            ),
        )

    prepared: list[
        tuple[
            Plan2EncoreListener,
            AffectedPlan2EncoreVersion,
            str,
            int,
            Plan3NativeCard,
        ]
    ] = []
    existing_transactions = {
        *(command.handoff_id for command in runtime.pending_handoffs),
        *runtime.committed_transaction_ids,
    }
    for listener in candidates:
        pair = (listener.uid, listener.captured_guid)
        if pair in supplied.replay_ancestry:
            return _trigger_result(runtime, supplied, resolved=False, reason="recursive-cycle-guard")
        try:
            version = runtime.catalog.version(
                listener.captured_card.card_id, listener.captured_card.effective_upgrade
            )
            contract = runtime.catalog.contract(listener.captured_card.card_id)
            errors = _contract_shape_errors(contract)
            if errors:
                raise Plan2EncoreResolutionError("unknown-listener-contract", errors[0])
            if listener.active.rule != contract.rule or listener.encore_effect_id != version.encore_effect_id:
                raise Plan2EncoreResolutionError("unknown-listener-shape", str(listener.uid))
            if (listener.captured_guid, version.encore_effect_id) not in runtime.once_consumed:
                raise Plan2EncoreResolutionError("once-installer-not-consumed", listener.captured_guid)
            zone, index, card = _locate_captured_card(
                runtime, listener, supplied.transient_playing_cards
            )
            master = load_force_play_target_card_master(card, Path(database))
            if master.has_recursive_force_play:
                raise Plan2EncoreResolutionError("captured-card-recursive-force-play", card.guid)
            if (
                master.play_effect_ids != version.ordered_effect_ids
                or master.base_move_position_type != MOVE_LOST
            ):
                raise Plan2EncoreResolutionError("captured-card-master-drift", card.guid)
        except (Plan2EncoreError, ForcePlayCardSearchResolutionError) as error:
            return _trigger_result(
                runtime,
                supplied,
                resolved=False,
                reason=getattr(error, "code", "target-master-unavailable"),
            )
        handoff_id = (
            f"plan2-encore:{supplied.event_id}:{listener.uid}:"
            f"{listener.active.uses + 1}"
        )
        if handoff_id in existing_transactions:
            return _trigger_result(runtime, supplied, resolved=False, reason="duplicate-trigger-event")
        existing_transactions.add(handoff_id)
        prepared.append((listener, version, zone, index, card))

    spent_by_uid: dict[int, Plan2EncoreListener] = {}
    commands: list[Plan2EncoreReplayCommand] = []
    differences: list[ForcePlayDifference] = []
    removed: list[int] = []
    for ordinal, (listener, version, zone, index, card) in enumerate(prepared):
        spent_active = replace(
            listener.active,
            uses=listener.active.uses + 1,
            uses_this_turn=listener.active.uses_this_turn + 1,
        )
        spent = replace(listener, active=spent_active)
        spent_by_uid[listener.uid] = spent
        handoff_id = (
            f"plan2-encore:{supplied.event_id}:{listener.uid}:{spent_active.uses}"
        )
        force_play = ForcePlayQueuedCommand(
            ordinal,
            card.guid,
            card,
            zone,
            TARGET_POSITION,
            index,
            MOVE_LOST,
            listener.uid,
        )
        command = Plan2EncoreReplayCommand(
            handoff_id,
            force_play,
            listener.uid,
            card.guid,
            version.encore_effect_id,
            version.ordered_effect_ids,
            version.installer_prefix,
            (version.encore_effect_id,),
            (*supplied.replay_ancestry, (listener.uid, card.guid)),
        )
        commands.append(command)
        differences.append(ForcePlayDifference(ordinal, card.guid))
        if spent_active.uses >= spent_active.max_uses:
            removed.append(listener.uid)

    next_listeners: list[Plan2EncoreListener] = []
    for listener in working:
        current = spent_by_uid.get(listener.uid, listener)
        if current.uid not in removed:
            next_listeners.append(current)
    after = replace(
        runtime,
        listeners=tuple(next_listeners),
        pending_handoffs=(*runtime.pending_handoffs, *commands),
    )
    return _trigger_result(
        runtime,
        supplied,
        resolved=True,
        after=after,
        triggered=True,
        commands=tuple(commands),
        differences=tuple(differences),
        spent=tuple(listener.uid for listener in candidates),
        removed=tuple(removed),
        operations=TRIGGER_OPERATIONS,
    )


@dataclass(frozen=True, slots=True)
class Plan2EncoreReplayStage:
    command: Plan2EncoreReplayCommand
    source_zone: str
    source_index: int
    live_card: Plan3NativeCard
    random_state_before: int
    random_state_after: int
    rng_consumed: bool = False


def stage_plan2_encore_handoff(
    runtime: Plan2EncoreRuntime, handoff_id: str
) -> Plan2EncoreReplayStage:
    """Resolve the queued GUID from its live zone without mutating or using RNG."""

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise Plan2EncoreInputError("invalid-runtime")
    command = next(
        (item for item in runtime.pending_handoffs if item.handoff_id == handoff_id),
        None,
    )
    if command is None:
        raise Plan2EncoreInputError("pending-handoff-not-found", handoff_id)
    matches: list[tuple[str, int, Plan3NativeCard]] = []
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        matches.extend(
            (zone, index, card)
            for index, card in enumerate(getattr(runtime.native_state, zone))
            if card.guid == command.captured_guid
        )
    if len(matches) != 1:
        raise Plan2EncoreInputError("live-handoff-guid-not-unique", command.captured_guid)
    zone, index, card = matches[0]
    if (
        card.card_id,
        card.effective_upgrade,
        card.runtime_customize_ids,
        card.plan2_runtime_customization,
        card.runtime_customize_lesson_depend_review_add,
    ) != (
        command.force_play.card.card_id,
        command.force_play.card.effective_upgrade,
        command.force_play.card.runtime_customize_ids,
        command.force_play.card.plan2_runtime_customization,
        command.force_play.card.runtime_customize_lesson_depend_review_add,
    ):
        raise Plan2EncoreInputError("live-handoff-card-identity-drift", card.guid)
    return Plan2EncoreReplayStage(
        command,
        zone,
        index,
        card,
        runtime.native_state.random_state,
        runtime.native_state.random_state,
    )


@dataclass(frozen=True, slots=True)
class Plan2EncorePlayReceipt:
    transaction_id: str
    playing_card: Plan3NativeCard
    after_state: Plan3NativeState
    play_origin: PlayOrigin
    source_zone: str
    processed_effect_ids: tuple[str, ...]
    core_executed_effect_ids: tuple[str, ...]
    installed_once_effect_ids: tuple[str, ...]
    skipped_once_effect_ids: tuple[str, ...]
    is_consume_cost: bool
    is_use_playable_count: bool
    history_event_count: int = 1
    final_move_count: int = 1


def record_plan2_encore_play_completion(
    runtime: Plan2EncoreRuntime, receipt: Plan2EncorePlayReceipt
) -> Plan2EncoreRuntime:
    """Commit one core-provided play receipt exactly once.

    The receipt is intentionally strict.  It does not execute neighboring
    unsupported effects; it proves that the authoritative core ran the exact
    ordered list, reported the Encore installer as exactly one of installed or
    skipped-once, left RNG unchanged, incremented card/global/turn counts once,
    emitted one history event, and appended the played card to Lost once.
    """

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise Plan2EncoreInputError("invalid-runtime")
    if not isinstance(receipt, Plan2EncorePlayReceipt):
        raise Plan2EncoreInputError("invalid-play-receipt")
    _text(receipt.transaction_id, "transaction_id")
    if receipt.transaction_id in runtime.committed_transaction_ids:
        raise Plan2EncoreInputError("transaction-already-committed", receipt.transaction_id)
    if receipt.play_origin not in ("normal", "forced", "extra"):
        raise Plan2EncoreInputError("unknown-play-origin")
    version = runtime.catalog.version(
        receipt.playing_card.card_id, receipt.playing_card.effective_upgrade
    )
    pending = next(
        (
            item
            for item in runtime.pending_handoffs
            if item.handoff_id == receipt.transaction_id
        ),
        None,
    )
    if receipt.play_origin == "forced":
        if pending is None:
            raise Plan2EncoreInputError("forced-receipt-handoff-not-found")
        if receipt.is_consume_cost or receipt.is_use_playable_count:
            raise Plan2EncoreInputError("forced-use-pool-policy-drift")
        if pending.captured_guid != receipt.playing_card.guid:
            raise Plan2EncoreInputError("forced-receipt-guid-mismatch")
    elif pending is not None:
        raise Plan2EncoreInputError("non-forced-receipt-used-handoff")
    if receipt.processed_effect_ids != version.ordered_effect_ids or (
        receipt.core_executed_effect_ids != version.installer_prefix
    ):
        raise Plan2EncoreInputError("play-receipt-effect-order-drift")
    installed = receipt.installed_once_effect_ids
    skipped = receipt.skipped_once_effect_ids
    if (
        (installed, skipped)
        not in (
            ((version.encore_effect_id,), ()),
            ((), (version.encore_effect_id,)),
        )
    ):
        raise Plan2EncoreInputError("play-receipt-once-outcome-drift")
    if receipt.play_origin == "forced" and installed:
        raise Plan2EncoreInputError("forced-receipt-reinstalled-once-effect")
    if installed and not any(
        listener.captured_guid == receipt.playing_card.guid
        and listener.encore_effect_id == version.encore_effect_id
        for listener in runtime.listeners
    ):
        raise Plan2EncoreInputError("play-receipt-installed-listener-missing")
    if (receipt.playing_card.guid, version.encore_effect_id) not in runtime.once_consumed:
        raise Plan2EncoreInputError("play-receipt-once-state-missing")
    if receipt.history_event_count != 1 or receipt.final_move_count != 1:
        raise Plan2EncoreInputError("play-receipt-exactly-once-policy")
    if receipt.after_state.random_state != runtime.native_state.random_state:
        raise Plan2EncoreInputError("encore-play-unexpected-rng-consumption")
    matches = tuple(
        card
        for card in receipt.after_state.all_cards
        if card.guid == receipt.playing_card.guid
    )
    if len(matches) != 1 or not receipt.after_state.lost:
        raise Plan2EncoreInputError("play-receipt-final-guid-not-unique-lost")
    settled = receipt.after_state.lost[-1]
    if settled.guid != receipt.playing_card.guid:
        raise Plan2EncoreInputError("play-receipt-lost-append-order-drift")
    before_card_count = runtime.card_count(receipt.playing_card.guid)
    if before_card_count != receipt.playing_card.play_count:
        raise Plan2EncoreInputError("play-receipt-card-count-before-drift")
    if settled.play_count != before_card_count + 1:
        raise Plan2EncoreInputError("play-receipt-card-count-after-drift")
    counts = dict(runtime.card_play_counts)
    counts[receipt.playing_card.guid] = before_card_count + 1
    history = Plan2EncorePlayHistory(
        receipt.transaction_id,
        receipt.playing_card.guid,
        receipt.playing_card.card_id,
        receipt.playing_card.effective_upgrade,
        receipt.play_origin,
        receipt.source_zone,
        "lost",
        before_card_count,
        before_card_count + 1,
        runtime.global_play_count,
        runtime.global_play_count + 1,
        runtime.turn_play_count,
        runtime.turn_play_count + 1,
        runtime.native_state.random_state,
        receipt.after_state.random_state,
        "installed" if installed else "skipped-once",
    )
    remaining_pending = tuple(
        item
        for item in runtime.pending_handoffs
        if item.handoff_id != receipt.transaction_id
    )
    return replace(
        runtime,
        native_state=receipt.after_state,
        pending_handoffs=remaining_pending,
        committed_transaction_ids=(
            *runtime.committed_transaction_ids,
            receipt.transaction_id,
        ),
        card_play_counts=tuple(counts.items()),
        global_play_count=runtime.global_play_count + 1,
        turn_play_count=runtime.turn_play_count + 1,
        history=(*runtime.history, history),
    )


def start_plan2_encore_turn(runtime: Plan2EncoreRuntime) -> Plan2EncoreRuntime:
    """Reset only the per-turn count; permanent lifetime and phase counts stay."""

    if not isinstance(runtime, Plan2EncoreRuntime):
        raise Plan2EncoreInputError("invalid-runtime")
    listeners = tuple(
        replace(
            listener,
            active=replace(
                listener.active,
                uses_this_turn=0,
                turn_count=listener.active.turn_count + 1,
                remaining_turns=-1,
                passing_turn_start=True,
            ),
        )
        for listener in runtime.listeners
    )
    return replace(runtime, listeners=listeners, turn_play_count=0)


def remove_plan2_encore_listener(
    runtime: Plan2EncoreRuntime,
    uid: int,
    *,
    reason: Literal["total-count-exhausted", "external-native-removal"],
) -> Plan2EncoreRuntime:
    """Represent native removal; ordinary turn expiry is invalid for turn=-1."""

    if reason not in ("total-count-exhausted", "external-native-removal"):
        raise Plan2EncoreInputError("invalid-listener-removal-reason")
    matches = tuple(listener for listener in runtime.listeners if listener.uid == uid)
    if len(matches) != 1:
        raise Plan2EncoreInputError("listener-removal-uid-not-found", str(uid))
    if reason == "total-count-exhausted" and matches[0].active.uses < 2:
        raise Plan2EncoreInputError("listener-not-total-exhausted", str(uid))
    return replace(
        runtime,
        listeners=tuple(listener for listener in runtime.listeners if listener.uid != uid),
    )


def catalog_to_dict(
    catalog: Plan2EncoreCatalog | None = None,
) -> dict[str, object]:
    catalog = catalog or load_plan2_status_enchant_encore_catalog()
    return {
        "effect_type": ENCORE_EFFECT_TYPE,
        "effect_type_value": ENCORE_EFFECT_TYPE_VALUE,
        "affected_versions": [
            {
                **asdict(version),
                "ordered_effect_ids": list(version.ordered_effect_ids),
            }
            for version in catalog.versions
        ],
        "effect_rows": [asdict(EFFECT_ROWS[key]) for key in sorted(EFFECT_ROWS)],
        "contracts": [
            {
                "card_id": contract.card_id,
                "effect_id": contract.effect.id,
                "status_enchant_id": contract.rule.id,
                "trigger_id": contract.rule.trigger.id,
                "trigger_phase_types": list(contract.rule.trigger.phase_types),
                "trigger_phase_values": list(contract.rule.trigger.phase_values),
                "child_effect_ids": [effect.id for effect in contract.rule.effects],
                "target_self_search_id": contract.target_self_search.id,
                "activation_search_id": (
                    contract.activation_search.id if contract.activation_search else ""
                ),
            }
            for contract in catalog.contracts
        ],
        "accounting": dict(ACCOUNTING),
        "native": {
            "android_version": ANDROID_VERSION,
            "encore_constructor": ANDROID_ENCORE_CTOR,
            "encore_execute": ANDROID_ENCORE_EXECUTE,
            "try_add_trigger_status": ANDROID_TRY_ADD_TRIGGER_STATUS,
            "get_aggressive_interval": ANDROID_GET_AGGRESSIVE_INTERVAL,
            "aggressive_interval_predicate": ANDROID_AGGRESSIVE_INTERVAL_PREDICATE,
            "is_aggressive_interval": ANDROID_IS_AGGRESSIVE_INTERVAL,
            "try_add_aggressive": ANDROID_TRY_ADD_AGGRESSIVE,
            "increment_aggressive_phase_call": (
                ANDROID_INCREMENT_AGGRESSIVE_PHASE_CALL
            ),
            "increment_phase": ANDROID_INCREMENT_PHASE,
            "spend_count": ANDROID_SPEND_COUNT,
            "pc_version": PC_VERSION,
            "pc_encore_type_index": PC_ENCORE_TYPE_INDEX,
            "pc_encore_constructor_token": PC_ENCORE_CTOR_TOKEN,
            "pc_encore_execute_token": PC_ENCORE_EXECUTE_TOKEN,
            "pc_trigger_status_type_index": PC_TRIGGER_STATUS_TYPE_INDEX,
        },
        "core_hook_requirements": list(REPLAY_CORE_HOOKS),
    }


load_catalog = load_plan2_status_enchant_encore_catalog
install_listener = install_plan2_encore_listener
plan_trigger = plan_plan2_encore_trigger
stage_handoff = stage_plan2_encore_handoff
record_play_completion = record_plan2_encore_play_completion


__all__ = [
    "ACCOUNTING",
    "AFFECTED_CARD_VERSIONS",
    "ANDROID_AGGRESSIVE_INTERVAL_PREDICATE",
    "ANDROID_ENCORE_EXECUTE",
    "ANDROID_IS_AGGRESSIVE_INTERVAL",
    "CARD_198",
    "CARD_201",
    "EFFECT_ROWS",
    "ENCORE_EFFECT_198",
    "ENCORE_EFFECT_201",
    "FORCE_PLAY_EFFECT_ID",
    "PHASE_AGGRESSIVE_UP_INTERVAL",
    "PHASE_CARD_PLAY_AFTER",
    "TARGET_CARD_211",
    "AffectedPlan2EncoreVersion",
    "ExactCardEffectSlot",
    "ExactEffectRow",
    "Plan2EncoreCatalog",
    "Plan2EncoreContract",
    "Plan2EncoreError",
    "Plan2EncoreInputError",
    "Plan2EncoreInstallDifference",
    "Plan2EncoreInstallInput",
    "Plan2EncoreInstallResult",
    "Plan2EncoreListener",
    "Plan2EncorePlayHistory",
    "Plan2EncorePlayReceipt",
    "Plan2EncoreReplayCommand",
    "Plan2EncoreReplayStage",
    "Plan2EncoreResolutionError",
    "Plan2EncoreRuntime",
    "Plan2EncoreTriggerEvent",
    "Plan2EncoreTriggerResult",
    "catalog_to_dict",
    "install_listener",
    "install_plan2_encore_listener",
    "load_catalog",
    "load_plan2_status_enchant_encore_catalog",
    "new_plan2_encore_runtime",
    "plan_plan2_encore_trigger",
    "plan_trigger",
    "record_plan2_encore_play_completion",
    "record_play_completion",
    "remove_plan2_encore_listener",
    "stage_handoff",
    "stage_plan2_encore_handoff",
    "start_plan2_encore_turn",
]
