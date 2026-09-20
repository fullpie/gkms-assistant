"""Bounded Plan2 ``ExamForcePlayCardSearch`` native adapter.

This module owns one deliberately small slice: the current Master shape
reachable from ``p_card-02-men-100_007`` upgrade 0.  It is not a replacement
for the Plan2 card runtime.  The effect executor only selects cards and
queues native UsePool commands; the normal card executor still owns direct
effect execution, listener snapshots, and final card settlement.

The implementation keeps the two native layers separate:

* :class:`Plan3NativeState` is reused for ordered GUID zones and the verified
  xorshift32 state.
* :class:`Plan2State` is adapted only for the Plan2 scalar/status boundary.
  Forced UsePool commands pay no stamina and consume no playable count; the
  shared Plan2 completion boundary increments the global and turn card-play
  counters after the external native move.

No process, proxy, agent, clock, hash, or tamper service is called here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .exam_native_rng import INT32_MAX, next_int32
from .master_db import DEFAULT_DATABASE
from .plan2_core_runtime import complete_plan2_card_play_after_move
from .plan2_state import Plan2State
from .plan3_engine import Plan3State
from .plan3_force_play_card_search import (
    FORCED_USE_POOL_TRANSACTION_ORDER as NATIVE_FORCED_USE_POOL_TRANSACTION_ORDER,
    ForcePlayTargetCardMaster as Plan3ForcePlayTargetCardMaster,
    load_force_play_target_card_master,
)
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeCardMoveTarget,
    Plan3NativeState,
    Plan3NativeStateError,
)
from .plan3_use_pool import (
    Plan3UsePoolCommand,
    Plan3UsePoolStage,
    stage_plan3_use_pool_guid,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamForcePlayCardSearch"
EFFECT_TYPE_VALUE = 24
EFFECT_ID = "e_effect-exam_force_play_card_search-p_card_search-r-random-hand-2-all-0_0"
SEARCH_ID = "p_card_search-r-random-hand-2"

TARGET_CARD_ID = "p_card-02-men-100_007"
TARGET_CARD_UPGRADE = 0
TARGET_CARD_NAME = "エクセレント♪"
TARGET_TRIGGER_ID = "e_trigger-start_play"
TARGET_STATUS_DRAW_ID = "enchant-p_card-02-men-100_007-enc01"
TARGET_STATUS_FORCE_ID = "enchant-p_card-02-men-100_007-enc02"
TARGET_STATUS_DRAW_WRAPPER_ID = (
    "e_effect-exam_status_enchant-05-enchant-p_card-02-men-100_007-enc01"
)
TARGET_STATUS_FORCE_WRAPPER_ID = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-men-100_007-enc02"
)
TARGET_DRAW_EFFECT_ID = "e_effect-exam_card_draw-0001"
TARGET_PLAYABLE_EFFECT_ID = "e_effect-exam_playable_value_add-01"

PICK_RANGE_ALL = "ProducePickRangeType_All"
PICK_RANGE_RANDOM = "ProducePickRangeType_Random"
PICK_RANGE_SELECT = "ProducePickRangeType_Select"
PICK_COUNT_UNKNOWN = "ProducePickCountType_Unknown"
POSITION_HAND = "ProduceCardPositionType_Hand"
POSITION_PLAY_HAND = "ProduceCardPositionType_PlayHand"
POSITION_LOST = "ProduceCardMovePositionType_Lost"
POSITION_GRAVE = "ProduceCardMovePositionType_Grave"
RARITY_R = "ProduceCardRarity_R"
ORDER_RANDOM = "ProduceCardOrderType_Random"
UNKNOWN_PLAN = "ProducePlanType_Unknown"
UNKNOWN_STATUS = "ProduceCardSearchStatusType_Unknown"
UNKNOWN_ORDER = "ProduceCardOrderType_Unknown"
UNKNOWN_MIN_MAX = "ConditionMinMaxType_Unknown"
UNKNOWN_EXAM_EFFECT = "ProduceExamEffectType_Unknown"
UNKNOWN_COST = "ExamCostType_Unknown"
UNKNOWN_POSITION = "ProduceCardPositionType_Unknown"
UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"

DIFFERENCE_TYPE = "CardForcePlay"
DIFFERENCE_TYPE_VALUE = 14

ANDROID_VERSION = "v3.2.3"
ANDROID_BINARY = (
    "_research/android/game-v3.2.3/origin-extracted/lib/arm64-v8a/libil2cpp.so"
)
ANDROID_EXECUTOR_CTOR = "0x7E7A170"
ANDROID_EXECUTOR_EXECUTE = "0x7E7A480"
ANDROID_PICK_IMPL = "0x7E59CA4"
ANDROID_PICK_RANDOM_KEY = "0x7E6E54C"
ANDROID_RECURSION_PREDICATE = "0x7E7AC54"
ANDROID_GET_SEARCH_CARD_LIST = "0x7E57B94"
ANDROID_CREATE_USE_POOL = "0x7EC3458"
ANDROID_SET_FORCE_DIFFERENCE = "0x7E57420"

FORCED_USE_POOL_TRANSACTION_ORDER = NATIVE_FORCED_USE_POOL_TRANSACTION_ORDER
CORE_HOOK_REQUIREMENTS = (
    "live IsPlayable evaluation",
    "normal card passive/status/card-status listener snapshot",
    "ordered direct-effect execution with recursive difference insertion",
    "Playing transient-state ownership",
    "runtime PlayMovePositionType and FullPower Hold rejection",
    "card/global play-count object synchronization and callbacks",
)


class Plan2ForcePlaySearchError(ValueError):
    """Stable fail-closed error with a machine-readable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


Plan2ForcePlaySearchInputError = Plan2ForcePlaySearchError
Plan2ForcePlaySearchResolutionError = Plan2ForcePlaySearchError


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan2ForcePlaySearchInputError("invalid-text", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Plan2ForcePlaySearchInputError("invalid-nonnegative-int", label)
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise Plan2ForcePlaySearchResolutionError("invalid-string-array", label)
    return tuple(value)


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in value
    ):
        raise Plan2ForcePlaySearchResolutionError("invalid-integer-array", label)
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ForcePlayEffectRow:
    """The complete raw Master effect row, not a translation projection."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    target_card_id: str
    target_upgrade_count: int
    target_effect_type: str
    search_id: str
    move_position_type: str
    pick_range_type: str
    pick_count_reference_search_id: str
    pick_count_type: str
    pick_count_min: int
    pick_count_max: int
    search_id2: str
    pick_range_type2: str
    pick_count_reference_search_id2: str
    pick_count_type2: str
    pick_count_min2: int
    pick_count_max2: int
    chain_effect_id: str
    chain_effect_ids: tuple[str, ...]
    status_enchant_id: str
    card_status_enchant_id: str
    card_grow_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ForcePlayEffectContract:
    row: ForcePlayEffectRow
    search: ProduceCardSearchRule

    def __post_init__(self) -> None:
        if not isinstance(self.row, ForcePlayEffectRow):
            raise Plan2ForcePlaySearchInputError("invalid-effect-row")
        if not isinstance(self.search, ProduceCardSearchRule):
            raise Plan2ForcePlaySearchInputError("invalid-search-rule")
        if self.search.id != self.row.search_id:
            raise Plan2ForcePlaySearchInputError("effect-search-id-mismatch")

    @property
    def unresolved_reasons(self) -> tuple[str, ...]:
        expected_row: Mapping[str, object] = {
            "effect_id": EFFECT_ID,
            "effect_type": EFFECT_TYPE,
            "value1": 0,
            "value2": 0,
            "effect_count": 0,
            "effect_turn": 0,
            "target_card_id": "",
            "target_upgrade_count": 0,
            "target_effect_type": "ProduceExamEffectType_Unknown",
            "search_id": SEARCH_ID,
            "move_position_type": UNKNOWN_MOVE,
            "pick_range_type": PICK_RANGE_ALL,
            "pick_count_reference_search_id": "",
            "pick_count_type": PICK_COUNT_UNKNOWN,
            "pick_count_min": 0,
            "pick_count_max": 0,
            "search_id2": "",
            "pick_range_type2": "ProducePickRangeType_Unknown",
            "pick_count_reference_search_id2": "",
            "pick_count_type2": PICK_COUNT_UNKNOWN,
            "pick_count_min2": 0,
            "pick_count_max2": 0,
            "chain_effect_id": "",
            "chain_effect_ids": (),
            "status_enchant_id": "",
            "card_status_enchant_id": "",
            "card_grow_effect_ids": (),
            "effect_group_ids": (),
        }
        reasons = [
            f"effect-row:{field}"
            for field, value in expected_row.items()
            if getattr(self.row, field) != value
        ]
        expected_search: Mapping[str, object] = {
            "card_rarities": (RARITY_R,),
            "produce_card_ids": (),
            "upgrade_counts": (),
            "plan_type": UNKNOWN_PLAN,
            "card_categories": (),
            "card_status_type": UNKNOWN_STATUS,
            "order_type": ORDER_RANDOM,
            "card_position_type": POSITION_HAND,
            "card_search_tag": "",
            "produce_card_random_pool_id": "",
            "limit_count": 2,
            "stamina_min_max_type": UNKNOWN_MIN_MAX,
            "stamina_min": 0,
            "stamina_max": 0,
            "exam_effect_type": UNKNOWN_EXAM_EFFECT,
            "effect_group_ids": (),
            "is_self": False,
            "produce_card_pool_id": "",
            "cost_type": UNKNOWN_COST,
            "is_customized": False,
        }
        reasons.extend(
            f"search-row:{field}"
            for field, value in expected_search.items()
            if getattr(self.search, field) != value
        )
        return tuple(dict.fromkeys(reasons))

    @property
    def executable(self) -> bool:
        return not self.unresolved_reasons


def _effect_row_from_raw(raw: Mapping[str, object]) -> ForcePlayEffectRow:
    def text(name: str) -> str:
        value = raw.get(name)
        if not isinstance(value, str):
            raise Plan2ForcePlaySearchResolutionError("invalid-effect-field", name)
        return value

    def integer(name: str) -> int:
        value = raw.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            raise Plan2ForcePlaySearchResolutionError("invalid-effect-field", name)
        return value

    return ForcePlayEffectRow(
        effect_id=text("id"),
        effect_type=text("effectType"),
        value1=integer("effectValue1"),
        value2=integer("effectValue2"),
        effect_count=integer("effectCount"),
        effect_turn=integer("effectTurn"),
        target_card_id=text("targetProduceCardId"),
        target_upgrade_count=integer("targetUpgradeCount"),
        target_effect_type=text("targetExamEffectType"),
        search_id=text("produceCardSearchId"),
        move_position_type=text("movePositionType"),
        pick_range_type=text("pickRangeType"),
        pick_count_reference_search_id=text(
            "pickCountReferenceProduceCardSearchId"
        ),
        pick_count_type=text("pickCountType"),
        pick_count_min=integer("pickCountMin"),
        pick_count_max=integer("pickCountMax"),
        search_id2=text("produceCardSearchId2"),
        pick_range_type2=text("pickRangeType2"),
        pick_count_reference_search_id2=text(
            "pickCountReferenceProduceCardSearchId2"
        ),
        pick_count_type2=text("pickCountType2"),
        pick_count_min2=integer("pickCountMin2"),
        pick_count_max2=integer("pickCountMax2"),
        chain_effect_id=text("chainProduceExamEffectId"),
        chain_effect_ids=_string_tuple(
            raw.get("chainProduceExamEffectIds"), "chainProduceExamEffectIds"
        ),
        status_enchant_id=text("produceExamStatusEnchantId"),
        card_status_enchant_id=text("produceCardStatusEnchantId"),
        card_grow_effect_ids=_string_tuple(
            raw.get("produceCardGrowEffectIds"), "produceCardGrowEffectIds"
        ),
        effect_group_ids=_string_tuple(raw.get("effectGroupIds"), "effectGroupIds"),
    )


def load_force_play_effect_row(
    effect_id: str = EFFECT_ID, database: Path = DEFAULT_DATABASE
) -> ForcePlayEffectRow:
    """Load and structurally validate one raw ``effect`` Master row."""

    _text(effect_id, "effect_id")
    with sqlite3.connect(Path(database)) as connection:
        row = connection.execute(
            "SELECT id, effect_type, raw_json FROM effect WHERE id = ?",
            (effect_id,),
        ).fetchone()
    if row is None:
        raise Plan2ForcePlaySearchResolutionError("effect-not-found", effect_id)
    try:
        raw = json.loads(row[2])
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2ForcePlaySearchResolutionError(
            "effect-raw-json-invalid", effect_id
        ) from error
    if not isinstance(raw, Mapping) or raw.get("id") != effect_id:
        raise Plan2ForcePlaySearchResolutionError("effect-raw-id-mismatch", effect_id)
    parsed = _effect_row_from_raw(raw)
    if parsed.effect_type != row[1]:
        raise Plan2ForcePlaySearchResolutionError("effect-type-column-mismatch", effect_id)
    return parsed


def contract_for_effect(
    effect_id: str = EFFECT_ID, database: Path = DEFAULT_DATABASE
) -> ForcePlayEffectContract:
    row = load_force_play_effect_row(effect_id, database)
    try:
        search = load_produce_card_search(row.search_id, Path(database))
    except (KeyError, ValueError, sqlite3.Error) as error:
        raise Plan2ForcePlaySearchResolutionError(
            "search-load-failed", row.search_id
        ) from error
    return ForcePlayEffectContract(row, search)


@dataclass(frozen=True, slots=True)
class ForcePlayExecutionInput:
    selected_guids: tuple[str, ...] = ()
    enchant_effect_uid: int = 0

    def __post_init__(self) -> None:
        values = tuple(self.selected_guids)
        if any(not isinstance(value, str) or not value for value in values):
            raise Plan2ForcePlaySearchInputError("invalid-selected-guid")
        object.__setattr__(self, "selected_guids", values)
        _nonnegative(self.enchant_effect_uid, "enchant_effect_uid")


@dataclass(frozen=True, slots=True)
class TargetCardEffectSlot:
    slot: int
    effect_id: str
    trigger_id: str
    is_once: bool
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TargetStatusRow:
    status_id: str
    trigger_id: str
    wrapper_effect_id: str
    wrapper_effect_type: str
    wrapper_effect_count: int
    wrapper_effect_turn: int
    wrapper_effect_group_ids: tuple[str, ...]
    child_effect_ids: tuple[str, ...]
    trigger_phase_types: tuple[str, ...]
    trigger_phase_values: tuple[str, ...]
    trigger_field_status_check_types: tuple[str, ...]
    trigger_field_status_types: tuple[str, ...]
    trigger_field_status_values: tuple[int, ...]
    trigger_field_search_ids: tuple[str, ...]
    trigger_search_id: str
    trigger_upper_search_count: int
    trigger_lower_search_count: int
    trigger_move_position_type: str
    trigger_effect_types: tuple[str, ...]
    trigger_lesson_type: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Plan2TargetCardMaster:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    rarity: str
    stamina: int
    cost_type: str
    cost_value: int
    max_customize_count: int
    produce_card_customize_ids: tuple[str, ...]
    play_trigger_id: str
    move_position_type: str
    ordered_effect_slots: tuple[TargetCardEffectSlot, ...]
    status_rows: tuple[TargetStatusRow, ...]

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.ordered_effect_slots)

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "name": self.name,
            "plan_type": self.plan_type,
            "category": self.category,
            "rarity": self.rarity,
            "stamina": self.stamina,
            "cost_type": self.cost_type,
            "cost_value": self.cost_value,
            "max_customize_count": self.max_customize_count,
            "produce_card_customize_ids": list(self.produce_card_customize_ids),
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "ordered_effect_slots": [
                slot.to_dict() for slot in self.ordered_effect_slots
            ],
            "status_rows": [status.to_dict() for status in self.status_rows],
        }


def _json_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Plan2ForcePlaySearchResolutionError("invalid-json-mapping", label)
    return value


def _raw_effect_snapshot(
    connection: sqlite3.Connection, effect_id: str
) -> tuple[str, int, int, int, int, str, tuple[str, ...]]:
    row = connection.execute(
        "SELECT effect_type, value1, value2, effect_count, effect_turn, "
        "status_enchant_id, raw_json FROM effect WHERE id = ?",
        (effect_id,),
    ).fetchone()
    if row is None:
        raise Plan2ForcePlaySearchResolutionError("effect-not-found", effect_id)
    try:
        raw = _json_mapping(json.loads(row[6]), effect_id)
        groups = _string_tuple(raw.get("effectGroupIds"), "effectGroupIds")
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2ForcePlaySearchResolutionError(
            "effect-raw-json-invalid", effect_id
        ) from error
    return (
        str(row[0]),
        int(row[1]),
        int(row[2]),
        int(row[3]),
        int(row[4]),
        str(row[5]),
        groups,
    )


def load_target_card_master(
    database: Path = DEFAULT_DATABASE,
    *,
    card_id: str = TARGET_CARD_ID,
    upgrade: int = TARGET_CARD_UPGRADE,
) -> Plan2TargetCardMaster:
    """Load the target Card, its ordered direct slots, and status programs."""

    _text(card_id, "card_id")
    _nonnegative(upgrade, "upgrade")
    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        card = connection.execute(
            "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
            (card_id, upgrade),
        ).fetchone()
        if card is None:
            raise Plan2ForcePlaySearchResolutionError(
                "card-not-found", f"{card_id}+{upgrade}"
            )
        try:
            raw_card = _json_mapping(json.loads(card["raw_json"]), card_id)
            raw_play_effects = json.loads(card["play_effects_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise Plan2ForcePlaySearchResolutionError(
                "card-raw-json-invalid", card_id
            ) from error
        if not isinstance(raw_play_effects, list):
            raise Plan2ForcePlaySearchResolutionError("card-play-effects-invalid", card_id)
        slots: list[TargetCardEffectSlot] = []
        for slot, item in enumerate(raw_play_effects):
            row = _json_mapping(item, f"play_effect[{slot}]")
            effect_id = row.get("produceExamEffectId")
            trigger_id = row.get("produceExamTriggerId")
            once = row.get("isOncePlayEffect")
            if (
                not isinstance(effect_id, str)
                or not effect_id
                or not isinstance(trigger_id, str)
                or not isinstance(once, bool)
            ):
                raise Plan2ForcePlaySearchResolutionError(
                    "card-play-effect-shape", f"{card_id}:{slot}"
                )
            (
                effect_type,
                value1,
                value2,
                effect_count,
                effect_turn,
                status_id,
                groups,
            ) = _raw_effect_snapshot(connection, effect_id)
            slots.append(
                TargetCardEffectSlot(
                    slot,
                    effect_id,
                    trigger_id,
                    once,
                    effect_type,
                    value1,
                    value2,
                    effect_count,
                    effect_turn,
                    status_id,
                    groups,
                )
            )

        status_rows: list[TargetStatusRow] = []
        status_ids = tuple(
            slot.status_enchant_id
            for slot in slots
            if slot.status_enchant_id
        )
        for status_id in status_ids:
            status = connection.execute(
                "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
                (status_id,),
            ).fetchone()
            if status is None:
                raise Plan2ForcePlaySearchResolutionError(
                    "status-not-found", status_id
                )
            child_ids = _string_tuple(
                json.loads(status["produce_exam_effect_ids_json"]),
                f"{status_id}.child_effect_ids",
            )
            wrapper_id = next(
                slot.effect_id for slot in slots if slot.status_enchant_id == status_id
            )
            (
                wrapper_type,
                _wrapper_v1,
                _wrapper_v2,
                wrapper_count,
                wrapper_turn,
                _wrapper_status,
                wrapper_groups,
            ) = _raw_effect_snapshot(connection, wrapper_id)
            trigger_id = str(status["produce_exam_trigger_id"])
            trigger = connection.execute(
                "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
            ).fetchone()
            if trigger is None:
                raise Plan2ForcePlaySearchResolutionError(
                    "trigger-not-found", trigger_id
                )
            raw_trigger = _json_mapping(json.loads(trigger["raw_json"]), trigger_id)

            def trigger_strings(name: str) -> tuple[str, ...]:
                return _string_tuple(raw_trigger.get(name), f"{trigger_id}.{name}")

            def trigger_ints(name: str) -> tuple[int, ...]:
                return _integer_tuple(raw_trigger.get(name), f"{trigger_id}.{name}")

            status_rows.append(
                TargetStatusRow(
                    status_id,
                    trigger_id,
                    wrapper_id,
                    wrapper_type,
                    wrapper_count,
                    wrapper_turn,
                    wrapper_groups,
                    child_ids,
                    trigger_strings("phaseTypes"),
                    trigger_strings("phaseValues"),
                    trigger_strings("fieldStatusCheckTypes"),
                    trigger_strings("fieldStatusTypes"),
                    trigger_ints("fieldStatusValues"),
                    trigger_strings("fieldStatusProduceCardSearchIds"),
                    str(raw_trigger.get("produceCardSearchId", "")),
                    int(raw_trigger.get("upperSearchCount", 0)),
                    int(raw_trigger.get("lowerSearchCount", 0)),
                    str(raw_trigger.get("cardMovePositionType", UNKNOWN_MOVE)),
                    trigger_strings("effectTypes"),
                    str(raw_trigger.get("lessonType", "ProduceStepLessonType_Unknown")),
                )
            )
        return Plan2TargetCardMaster(
            card_id=str(card["id"]),
            upgrade=int(card["upgrade_count"]),
            name=str(card["name"]),
            plan_type=str(card["plan_type"]),
            category=str(card["category"]),
            rarity=str(raw_card.get("rarity", "")),
            stamina=int(card["stamina"]),
            cost_type=str(card["cost_type"]),
            cost_value=int(card["cost_value"]),
            max_customize_count=int(raw_card.get("maxCustomizeCount", 0)),
            produce_card_customize_ids=_string_tuple(
                raw_card.get("produceCardCustomizeIds", []),
                f"{card_id}.produceCardCustomizeIds",
            ),
            play_trigger_id=str(card["play_trigger_id"]),
            move_position_type=str(card["move_position_type"]),
            ordered_effect_slots=tuple(slots),
            status_rows=tuple(status_rows),
        )


@dataclass(frozen=True, slots=True)
class AffectedCardVersion:
    card_id: str
    upgrade: int
    relation: str
    force_effect_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _force_effect_ids_for_card(
    connection: sqlite3.Connection, direct_effect_ids: Sequence[str]
) -> tuple[str, ...]:
    found: list[str] = []
    for effect_id in direct_effect_ids:
        effect = connection.execute(
            "SELECT effect_type, status_enchant_id FROM effect WHERE id = ?",
            (effect_id,),
        ).fetchone()
        if effect is None:
            continue
        if effect[0] == EFFECT_TYPE:
            found.append(effect_id)
        status_id = str(effect[1] or "")
        if not status_id:
            continue
        status = connection.execute(
            "SELECT produce_exam_effect_ids_json FROM produce_exam_status_enchant "
            "WHERE id = ?",
            (status_id,),
        ).fetchone()
        if status is None:
            continue
        try:
            child_ids = json.loads(status[0])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(child_ids, list):
            continue
        for child_id in child_ids:
            child = connection.execute(
                "SELECT effect_type FROM effect WHERE id = ?", (child_id,)
            ).fetchone()
            if child is not None and child[0] == EFFECT_TYPE:
                found.append(str(child_id))
    return tuple(dict.fromkeys(found))


def load_affected_card_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[AffectedCardVersion, ...]:
    """Probe the current Plan2 Master graph; do not read official coverage."""

    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        result: list[AffectedCardVersion] = []
        cards = connection.execute(
            "SELECT id, upgrade_count, play_effects_json FROM card "
            "WHERE plan_type = 'ProducePlanType_Plan2' ORDER BY id, upgrade_count"
        ).fetchall()
        for card in cards:
            try:
                raw_effects = json.loads(card["play_effects_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(raw_effects, list):
                continue
            direct_ids = tuple(
                item.get("produceExamEffectId")
                for item in raw_effects
                if isinstance(item, Mapping)
                and isinstance(item.get("produceExamEffectId"), str)
            )
            force_ids = _force_effect_ids_for_card(connection, direct_ids)
            if not force_ids:
                continue
            relation = (
                "direct-unlock"
                if (str(card["id"]), int(card["upgrade_count"]))
                == (TARGET_CARD_ID, TARGET_CARD_UPGRADE)
                else "co-overlap-only"
            )
            result.append(
                AffectedCardVersion(
                    str(card["id"]), int(card["upgrade_count"]), relation, force_ids
                )
            )
    return tuple(result)


AFFECTED_CARD_VERSIONS = (
    *(
        AffectedCardVersion(
            "p_card-02-ido-3_192",
            upgrade,
            "co-overlap-only",
            ("e_effect-exam_force_play_card_search-p_card_search-deck_grave-select-1_1",),
        )
        for upgrade in range(4)
    ),
    *(
        AffectedCardVersion(
            "p_card-02-ido-3_198",
            upgrade,
            "co-overlap-only",
            ("e_effect-exam_force_play_card_search-p_card_search-target_is_self-exam_status_enchant_encore-all-1_1",),
        )
        for upgrade in range(4)
    ),
    *(
        AffectedCardVersion(
            "p_card-02-ido-3_201",
            upgrade,
            "co-overlap-only",
            ("e_effect-exam_force_play_card_search-p_card_search-target_is_self-exam_status_enchant_encore-all-1_1",),
        )
        for upgrade in range(4)
    ),
    AffectedCardVersion(TARGET_CARD_ID, TARGET_CARD_UPGRADE, "direct-unlock", (EFFECT_ID,)),
)


@dataclass(frozen=True, slots=True)
class ForcePlayAccounting:
    affected: int
    direct: int
    co: int
    affected_unique_cards: int
    direct_unique_cards: int
    co_unique_cards: int
    versions: tuple[AffectedCardVersion, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "affected": self.affected,
            "direct": self.direct,
            "co": self.co,
            "affected_unique_cards": self.affected_unique_cards,
            "direct_unique_cards": self.direct_unique_cards,
            "co_unique_cards": self.co_unique_cards,
            "versions": [version.to_dict() for version in self.versions],
        }


def probe_accounting(database: Path = DEFAULT_DATABASE) -> ForcePlayAccounting:
    versions = load_affected_card_versions(database)
    direct = tuple(item for item in versions if item.relation == "direct-unlock")
    co = tuple(item for item in versions if item.relation == "co-overlap-only")
    return ForcePlayAccounting(
        affected=len(versions),
        direct=len(direct),
        co=len(co),
        affected_unique_cards=len({item.card_id for item in versions}),
        direct_unique_cards=len({item.card_id for item in direct}),
        co_unique_cards=len({item.card_id for item in co}),
        versions=versions,
    )


def _card_rarity(card: Plan3NativeCard, database: Path) -> str:
    if (
        card.runtime_customize_ids
        and card.plan2_runtime_customization.customize_ids
        != card.runtime_customize_ids
    ):
        raise Plan2ForcePlaySearchResolutionError(
            "runtime-target-customization-unbound", card.guid
        )
    with sqlite3.connect(Path(database)) as connection:
        row = connection.execute(
            "SELECT raw_json FROM card WHERE id = ? AND upgrade_count = ?",
            (card.card_id, card.effective_upgrade),
        ).fetchone()
    if row is None:
        raise Plan2ForcePlaySearchResolutionError(
            "target-card-master-missing", f"{card.card_id}+{card.effective_upgrade}"
        )
    try:
        raw = _json_mapping(json.loads(row[0]), card.card_id)
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2ForcePlaySearchResolutionError(
            "target-card-raw-json-invalid", card.guid
        ) from error
    rarity = raw.get("rarity")
    if not isinstance(rarity, str) or not rarity:
        raise Plan2ForcePlaySearchResolutionError("target-card-rarity-unavailable", card.guid)
    return rarity


def _search_candidates(
    state: Plan3NativeState,
    search: ProduceCardSearchRule,
    *,
    playing_guid: str,
    database: Path,
) -> tuple[
    tuple[Plan3NativeCardMoveTarget, ...],
    tuple[Plan3NativeCardMoveTarget, ...],
    tuple[Plan3NativeCardMoveTarget, ...],
    dict[str, Plan3ForcePlayTargetCardMaster],
]:
    if search.card_position_type != POSITION_HAND:
        raise Plan3NativeStateError("card-search-position", search.id)
    playing = tuple(card for card in state.hand if card.guid == playing_guid)
    if len(playing) != 1:
        raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)

    candidates: list[Plan3NativeCardMoveTarget] = []
    for index, card in enumerate(state.hand):
        if card.guid == playing_guid:
            continue
        if _card_rarity(card, database) != RARITY_R:
            continue
        candidates.append(Plan3NativeCardMoveTarget(card.guid, card, "hand", index))

    masters: dict[str, Plan3ForcePlayTargetCardMaster] = {}
    for candidate in candidates:
        master = load_force_play_target_card_master(candidate.card, database)
        if master.base_move_position_type not in {POSITION_LOST, POSITION_GRAVE}:
            raise Plan2ForcePlaySearchResolutionError(
                "target-move-position-unmodelled",
                f"{candidate.guid}:{master.base_move_position_type}",
            )
        masters[candidate.guid] = master
    eligible = tuple(
        candidate
        for candidate in candidates
        if not masters[candidate.guid].has_recursive_force_play
    )
    excluded = tuple(
        candidate
        for candidate in candidates
        if masters[candidate.guid].has_recursive_force_play
    )
    return tuple(candidates), eligible, excluded, masters


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayQueuedCommand:
    ordinal: int
    guid: str
    card: Plan3NativeCard
    original_source_zone: str
    original_source_position_type: str
    original_source_index: int
    base_move_position_type: str
    enchant_effect_uid: int = 0
    command_type: int = 4
    is_consume_cost: bool = False
    is_manual: bool = False
    is_use_playable_count: bool = False
    resolves_source_by_guid_at_execution: bool = True
    transaction_order: tuple[str, ...] = FORCED_USE_POOL_TRANSACTION_ORDER

    def __post_init__(self) -> None:
        _nonnegative(self.ordinal, "ordinal")
        _text(self.guid, "guid")
        if self.guid != self.card.guid:
            raise Plan2ForcePlaySearchInputError("command-guid-card-mismatch")
        _nonnegative(self.original_source_index, "original_source_index")
        _nonnegative(self.enchant_effect_uid, "enchant_effect_uid")
        if self.original_source_position_type != POSITION_PLAY_HAND:
            raise Plan2ForcePlaySearchInputError("force-play-source-position", self.guid)
        if self.is_consume_cost or self.is_manual or self.is_use_playable_count:
            raise Plan2ForcePlaySearchInputError("forced-use-pool-policy", self.guid)
        if not self.resolves_source_by_guid_at_execution:
            raise Plan2ForcePlaySearchInputError("forced-use-pool-guid-policy", self.guid)

    @property
    def cost_delta(self) -> int:
        return 0

    @property
    def plays_remaining_delta(self) -> int:
        return 0

    @property
    def global_play_count_delta_after_move(self) -> int:
        return 1

    @property
    def card_play_count_native_callsite_count(self) -> int:
        return 2

    @property
    def playable_count_consumed(self) -> int:
        return 0

    def to_use_pool_command(self) -> Plan3UsePoolCommand:
        return Plan3UsePoolCommand(
            guid=self.guid,
            is_consume_cost=False,
            is_use_playable_count=False,
            is_manual=False,
            enchant_effect_uid=self.enchant_effect_uid,
        )


@dataclass(frozen=True, slots=True)
class ForcePlayDifference:
    ordinal: int
    guid: str
    difference_type: str = DIFFERENCE_TYPE
    difference_type_value: int = DIFFERENCE_TYPE_VALUE
    appended_after_matching_command: bool = True


@dataclass(frozen=True, slots=True)
class ForcePlayResolvedBranch:
    candidate_guids: tuple[str, ...]
    eligible_guids: tuple[str, ...]
    recursive_excluded_guids: tuple[str, ...]
    selected_guids: tuple[str, ...]
    selection_mode: str


@dataclass(frozen=True, slots=True)
class ForcePlayUnresolvedBranch:
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ForcePlayTrace:
    effect_id: str
    random_state_before: int
    random_state_after: int
    rng_consumed: bool
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayPlanResult:
    before: Plan3NativeState
    after: Plan3NativeState
    plan2_before: Plan2State
    plan2_after: Plan2State
    contract: ForcePlayEffectContract
    execution_input: ForcePlayExecutionInput
    branch: ForcePlayResolvedBranch | ForcePlayUnresolvedBranch
    commands: tuple[Plan2ForcePlayQueuedCommand, ...]
    differences: tuple[ForcePlayDifference, ...]
    trace: ForcePlayTrace

    @property
    def resolved(self) -> bool:
        return isinstance(self.branch, ForcePlayResolvedBranch)

    @property
    def unresolved(self) -> bool:
        return not self.resolved

    @property
    def core_hook_required(self) -> bool:
        return bool(self.commands)

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after, Plan3NativeState
        ):
            raise Plan2ForcePlaySearchInputError("invalid-native-state")
        if not isinstance(self.plan2_before, Plan2State) or not isinstance(
            self.plan2_after, Plan2State
        ):
            raise Plan2ForcePlaySearchInputError("invalid-plan2-state")
        for zone in ("hand", "deck", "grave", "lost", "hold"):
            if getattr(self.before, zone) != getattr(self.after, zone):
                raise Plan2ForcePlaySearchInputError("planner-mutated-zone", zone)
        if self.plan2_after is not self.plan2_before:
            raise Plan2ForcePlaySearchInputError("planner-mutated-plan2-scalar")
        if self.unresolved and (self.commands or self.differences):
            raise Plan2ForcePlaySearchInputError("unresolved-result-has-output")
        if len(self.commands) != len(self.differences):
            raise Plan2ForcePlaySearchInputError("command-difference-count-mismatch")
        for command, difference in zip(self.commands, self.differences, strict=True):
            if command.ordinal != difference.ordinal or command.guid != difference.guid:
                raise Plan2ForcePlaySearchInputError("command-difference-order-mismatch")


def _unresolved_result(
    state: Plan3NativeState,
    plan2_state: Plan2State,
    contract: ForcePlayEffectContract,
    supplied: ForcePlayExecutionInput,
    reason: str,
    detail: str = "",
) -> Plan2ForcePlayPlanResult:
    return Plan2ForcePlayPlanResult(
        state,
        state,
        plan2_state,
        plan2_state,
        contract,
        supplied,
        ForcePlayUnresolvedBranch(reason, detail),
        (),
        (),
        ForcePlayTrace(
            contract.row.effect_id,
            state.random_state,
            state.random_state,
            False,
            ("fail-closed-before-rng-and-queue",),
        ),
    )


def plan_force_play_card_search(
    state: Plan3NativeState,
    plan2_state: Plan2State,
    contract: ForcePlayEffectContract,
    *,
    playing_guid: str,
    execution_input: ForcePlayExecutionInput | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ForcePlayPlanResult:
    """Resolve the exact target search and queue GUID commands only."""

    if not isinstance(state, Plan3NativeState):
        raise Plan2ForcePlaySearchInputError("state-must-be-plan3-native-state")
    if not isinstance(plan2_state, Plan2State):
        raise Plan2ForcePlaySearchInputError("plan2-state-must-be-plan2-state")
    if not isinstance(contract, ForcePlayEffectContract):
        raise Plan2ForcePlaySearchInputError("invalid-contract")
    supplied = execution_input or ForcePlayExecutionInput()
    if not isinstance(supplied, ForcePlayExecutionInput):
        raise Plan2ForcePlaySearchInputError("invalid-execution-input")
    _text(playing_guid, "playing_guid")
    if not contract.executable:
        return _unresolved_result(
            state,
            plan2_state,
            contract,
            supplied,
            "force-play-contract-unresolved",
            contract.unresolved_reasons[0],
        )
    if supplied.selected_guids:
        return _unresolved_result(
            state,
            plan2_state,
            contract,
            supplied,
            "all-shape-rejects-explicit-selection",
        )
    try:
        candidates, eligible, excluded, masters = _search_candidates(
            state,
            contract.search,
            playing_guid=playing_guid,
            database=Path(database),
        )
    except (Plan3NativeStateError, Plan2ForcePlaySearchError) as error:
        return _unresolved_result(
            state,
            plan2_state,
            contract,
            supplied,
            getattr(error, "code", "search-failed"),
            getattr(error, "detail", str(error)),
        )

    keyed: list[tuple[int, int, Plan3NativeCardMoveTarget]] = []
    random_state = state.random_state
    for index, candidate in enumerate(eligible):
        key, random_state = next_int32(random_state)
        keyed.append((key, index, candidate))
    ranked = sorted(keyed, key=lambda item: (item[0], item[1]))
    retained = ranked[: contract.search.limit_count]
    retained_indices = {index for _key, index, _candidate in retained}
    selected = tuple(
        candidate
        for index, candidate in enumerate(eligible)
        if index in retained_indices
    )
    after = replace(state, random_state=random_state) if keyed else state
    operations = [
        "GetSearchCardList(search=p_card_search-r-random-hand-2)",
        "build-Hand-position-list-in-native-hand-order",
        "filter-rarity-R",
        "additional-condition:every-current-play-effect-type-is-not-24",
        "OrderType.Random:one-parameterless-rng-key-per-eligible-candidate",
        "apply-LimitCount=2",
        "restore-original-ticket-order-after-selection",
        "PickRange.All:count-min-max-ignored",
    ]
    commands: list[Plan2ForcePlayQueuedCommand] = []
    differences: list[ForcePlayDifference] = []
    for ordinal, target in enumerate(selected):
        commands.append(
            Plan2ForcePlayQueuedCommand(
                ordinal=ordinal,
                guid=target.guid,
                card=target.card,
                original_source_zone=target.source_zone,
                original_source_position_type=POSITION_PLAY_HAND,
                original_source_index=target.source_index,
                base_move_position_type=masters[target.guid].base_move_position_type,
                enchant_effect_uid=supplied.enchant_effect_uid,
            )
        )
        differences.append(ForcePlayDifference(ordinal, target.guid))
        operations.extend(
            (
                f"CreateUsePoolCommand(isConsumeCost=false,isManual=false):{target.guid}",
                f"SetCardForcePlay:{target.guid}",
            )
        )
    operations.extend(
        (
            "append-command-and-difference-in-effect-order",
            "difference-trigger-insertion-is-depth-first-at-core-boundary",
            "post-effect-callback-after-difference-callback",
        )
    )
    branch = ForcePlayResolvedBranch(
        tuple(candidate.guid for candidate in candidates),
        tuple(candidate.guid for candidate in eligible),
        tuple(candidate.guid for candidate in excluded),
        tuple(target.guid for target in selected),
        "all-after-random-order-limit",
    )
    return Plan2ForcePlayPlanResult(
        state,
        after,
        plan2_state,
        plan2_state,
        contract,
        supplied,
        branch,
        tuple(commands),
        tuple(differences),
        ForcePlayTrace(
            contract.row.effect_id,
            state.random_state,
            after.random_state,
            bool(keyed),
            tuple(operations),
        ),
    )


apply_force_play_card_search = plan_force_play_card_search
execute_force_play_card_search = plan_force_play_card_search
plan2_force_play_card_search = plan_force_play_card_search
apply_plan2_force_play_card_search = plan_force_play_card_search
execute_plan2_force_play_card_search = plan_force_play_card_search


def _plan3_scalar_bridge(plan2_state: Plan2State, native: Plan3NativeState) -> Plan3State:
    """Create only the projection required by the shared UsePool stage."""

    return Plan3State(
        turns_remaining=1,
        stamina=plan2_state.stamina,
        plays_remaining=1,
        hand=tuple(card.ref for card in native.hand),
        draw_pile=tuple(card.ref for card in native.deck),
        discard_pile=tuple(card.ref for card in native.grave),
        lost_pile=tuple(card.ref for card in native.lost),
        hold_pile=tuple(card.ref for card in native.hold),
    )


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayStage:
    command: Plan2ForcePlayQueuedCommand
    plan2_before: Plan2State
    native_before: Plan3NativeState
    plan2_playing: Plan2State
    native_playing: Plan3NativeState
    shared_stage: Plan3UsePoolStage
    card: Plan3NativeCard
    source_zone: str
    source_index: int
    order: tuple[str, ...] = FORCED_USE_POOL_TRANSACTION_ORDER


def stage_plan2_force_play_guid(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    command: Plan2ForcePlayQueuedCommand,
) -> Plan2ForcePlayStage:
    """Resolve the current GUID location, ignoring the queued source index."""

    if not isinstance(plan2_state, Plan2State):
        raise Plan2ForcePlaySearchInputError("plan2-state-must-be-plan2-state")
    if not isinstance(native_state, Plan3NativeState):
        raise Plan2ForcePlaySearchInputError("native-state-must-be-plan3-native-state")
    if not isinstance(command, Plan2ForcePlayQueuedCommand):
        raise Plan2ForcePlaySearchInputError("invalid-command")
    bridge = _plan3_scalar_bridge(plan2_state, native_state)
    try:
        shared = stage_plan3_use_pool_guid(
            bridge, native_state, command.to_use_pool_command()
        )
    except Plan3NativeStateError as error:
        raise Plan2ForcePlaySearchError(error.code, error.detail) from error
    if (
        shared.card.card_id,
        shared.card.base_upgrade,
        shared.card.temporary_upgrade,
        shared.card.effective_upgrade,
    ) != (
        command.card.card_id,
        command.card.base_upgrade,
        command.card.temporary_upgrade,
        command.card.effective_upgrade,
    ):
        raise Plan2ForcePlaySearchError("guid-card-snapshot-mismatch", command.guid)
    return Plan2ForcePlayStage(
        command,
        plan2_state,
        native_state,
        plan2_state,
        shared.native_playing,
        shared,
        shared.card,
        shared.source_zone,
        shared.source_index,
    )


def _move_playing_card(
    native: Plan3NativeState,
    guid: str,
    destination: str,
) -> tuple[Plan3NativeState, Plan3NativeCard, str]:
    if destination not in {POSITION_LOST, POSITION_GRAVE}:
        raise Plan2ForcePlaySearchError("target-move-position-unmodelled", destination)
    matches: list[tuple[str, int, Plan3NativeCard]] = []
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        for index, card in enumerate(getattr(native, zone)):
            if card.guid == guid:
                matches.append((zone, index, card))
    if len(matches) != 1:
        raise Plan2ForcePlaySearchError("playing-guid-not-found-for-move", guid)
    source_zone, _source_index, card = matches[0]
    if card.play_count >= INT32_MAX:
        raise Plan2ForcePlaySearchError("card-play-count-overflow", guid)
    played = replace(card, play_count=card.play_count + 1)
    values: dict[str, tuple[Plan3NativeCard, ...]] = {}
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        values[zone] = tuple(
            item for item in getattr(native, zone) if item.guid != guid
        )
    target_zone = "lost" if destination == POSITION_LOST else "grave"
    values[target_zone] = (*values[target_zone], played)
    return replace(native, **values), played, source_zone


@dataclass(frozen=True, slots=True)
class Plan2ForcePlaySettlement:
    command: Plan2ForcePlayQueuedCommand
    before_plan2: Plan2State
    after_plan2: Plan2State
    before_native: Plan3NativeState
    after_native: Plan3NativeState
    source_zone: str
    destination: str
    cost_paid: int
    playable_count_consumed: int
    once_effect_ids_executed: tuple[str, ...]
    card_play_count_delta: int = 1
    global_card_play_count_delta: int = 1
    turn_card_play_count_delta: int = 1
    history_event_count: int = 1
    final_move_count: int = 1
    nested: tuple["Plan2ForcePlaySettlement", ...] = ()
    operation_order: tuple[str, ...] = FORCED_USE_POOL_TRANSACTION_ORDER

    def __post_init__(self) -> None:
        if (
            self.cost_paid,
            self.playable_count_consumed,
            self.card_play_count_delta,
            self.global_card_play_count_delta,
            self.turn_card_play_count_delta,
            self.history_event_count,
            self.final_move_count,
        ) != (0, 0, 1, 1, 1, 1, 1):
            raise Plan2ForcePlaySearchError("forced-settlement-count-policy")
        if not isinstance(self.before_plan2, Plan2State) or not isinstance(
            self.after_plan2, Plan2State
        ):
            raise Plan2ForcePlaySearchError("invalid-settlement-plan2-state")


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayQueueExecution:
    before_plan2: Plan2State
    after_plan2: Plan2State
    before_native: Plan3NativeState
    after_native: Plan3NativeState
    settlements: tuple[Plan2ForcePlaySettlement, ...]
    operation_order: tuple[str, ...]

    @property
    def history_event_count(self) -> int:
        return sum(item.history_event_count for item in self.settlements)

    @property
    def final_move_count(self) -> int:
        return sum(item.final_move_count for item in self.settlements)

    @property
    def global_card_play_count_delta(self) -> int:
        return sum(item.global_card_play_count_delta for item in self.settlements)


def _once_effect_ids(
    card: Plan3NativeCard, database: Path
) -> tuple[str, ...]:
    if card.play_count > 0:
        return ()
    with sqlite3.connect(Path(database)) as connection:
        row = connection.execute(
            "SELECT play_effects_json FROM card WHERE id = ? AND upgrade_count = ?",
            (card.card_id, card.effective_upgrade),
        ).fetchone()
    if row is None:
        raise Plan2ForcePlaySearchError(
            "target-card-master-missing", f"{card.card_id}+{card.effective_upgrade}"
        )
    try:
        raw_effects = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2ForcePlaySearchError("target-card-effects-invalid", card.guid) from error
    if not isinstance(raw_effects, list):
        raise Plan2ForcePlaySearchError("target-card-effects-invalid", card.guid)
    result: list[str] = []
    for item in raw_effects:
        if not isinstance(item, Mapping):
            raise Plan2ForcePlaySearchError("target-card-effects-invalid", card.guid)
        effect_id = item.get("produceExamEffectId")
        once = item.get("isOncePlayEffect")
        if not isinstance(effect_id, str) or not effect_id or not isinstance(once, bool):
            raise Plan2ForcePlaySearchError("target-card-effects-invalid", card.guid)
        if once:
            result.append(effect_id)
    return tuple(result)


def _flatten_nested_commands(
    command: Plan2ForcePlayQueuedCommand,
    nested_by_guid: Mapping[str, tuple[Plan2ForcePlayQueuedCommand, ...]],
    active: tuple[str, ...] = (),
) -> tuple[Plan2ForcePlayQueuedCommand, ...]:
    if command.guid in active:
        raise Plan2ForcePlaySearchError("recursive-force-play-loop", command.guid)
    children = nested_by_guid.get(command.guid, ())
    result = (command,)
    next_active = (*active, command.guid)
    for child in children:
        if not isinstance(child, Plan2ForcePlayQueuedCommand):
            raise Plan2ForcePlaySearchError("invalid-nested-command", command.guid)
        result += _flatten_nested_commands(child, nested_by_guid, next_active)
    return result


def execute_plan2_force_play_queue(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    commands: Sequence[Plan2ForcePlayQueuedCommand],
    *,
    nested_by_guid: Mapping[str, tuple[Plan2ForcePlayQueuedCommand, ...]] | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ForcePlayQueueExecution:
    """Settle a bounded command queue with depth-first nested differences.

    This is only the shared UsePool settlement boundary.  Direct card effects
    and listener programs are intentionally not reimplemented; callers pass
    already resolved nested commands from the normal Plan2 runtime.
    """

    if not isinstance(plan2_state, Plan2State):
        raise Plan2ForcePlaySearchInputError("plan2-state-must-be-plan2-state")
    if not isinstance(native_state, Plan3NativeState):
        raise Plan2ForcePlaySearchInputError("native-state-must-be-plan3-native-state")
    queue = tuple(commands)
    if any(not isinstance(command, Plan2ForcePlayQueuedCommand) for command in queue):
        raise Plan2ForcePlaySearchInputError("invalid-command-queue")
    nested = {} if nested_by_guid is None else dict(nested_by_guid)
    for key, values in nested.items():
        _text(key, "nested-command-key")
        if not isinstance(values, tuple):
            raise Plan2ForcePlaySearchInputError("nested-command-list-must-be-tuple", key)
    flattened: list[Plan2ForcePlayQueuedCommand] = []
    for command in queue:
        flattened.extend(_flatten_nested_commands(command, nested))
    guids = tuple(command.guid for command in flattened)
    if len(set(guids)) != len(guids):
        raise Plan2ForcePlaySearchError("recursive-force-play-guid-revisit")
    if any(
        command.base_move_position_type not in {POSITION_LOST, POSITION_GRAVE}
        for command in flattened
    ):
        raise Plan2ForcePlaySearchError("target-move-position-unmodelled")

    settlements: list[Plan2ForcePlaySettlement] = []
    operations: list[str] = []

    def run(
        command: Plan2ForcePlayQueuedCommand,
        current_plan2: Plan2State,
        current_native: Plan3NativeState,
        active: tuple[str, ...],
    ) -> tuple[Plan2State, Plan3NativeState, Plan2ForcePlaySettlement]:
        if command.guid in active:
            raise Plan2ForcePlaySearchError("recursive-force-play-loop", command.guid)
        stage = stage_plan2_force_play_guid(current_plan2, current_native, command)
        operations.append(f"stage-playing-guid-at-execution:{command.guid}")
        once_ids = _once_effect_ids(stage.card, Path(database))
        child_settlements: list[Plan2ForcePlaySettlement] = []
        child_plan2 = current_plan2
        child_native = stage.native_playing
        for child in nested.get(command.guid, ()):
            child_plan2, child_native, child_settlement = run(
                child, child_plan2, child_native, (*active, command.guid)
            )
            child_settlements.append(child_settlement)
        after_native, _played, source_zone = _move_playing_card(
            child_native, command.guid, command.base_move_position_type
        )
        scalar_completion = complete_plan2_card_play_after_move(
            child_plan2, card_move_completed=True
        )
        operations.extend(
            (
                f"snapshot-listeners-before-direct-effects:{command.guid}",
                f"execute-ordered-direct-effects-deferred-to-core:{command.guid}",
                f"move-live-playing-guid-to:{command.base_move_position_type}:{command.guid}",
                f"append-history-once:{command.guid}",
                f"increment-card-and-global-play-count-once:{command.guid}",
            )
        )
        settlement = Plan2ForcePlaySettlement(
            command=command,
            before_plan2=current_plan2,
            after_plan2=scalar_completion.after,
            before_native=current_native,
            after_native=after_native,
            source_zone=source_zone,
            destination=command.base_move_position_type,
            cost_paid=0,
            playable_count_consumed=0,
            once_effect_ids_executed=once_ids,
            nested=tuple(child_settlements),
        )
        settlements.append(settlement)
        return scalar_completion.after, after_native, settlement

    current_plan2 = plan2_state
    current_native = native_state
    for command in queue:
        current_plan2, current_native, _ = run(command, current_plan2, current_native, ())
    return Plan2ForcePlayQueueExecution(
        plan2_state,
        current_plan2,
        native_state,
        current_native,
        tuple(settlements),
        tuple(operations),
    )


def settle_plan2_force_play_command(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    command: Plan2ForcePlayQueuedCommand,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ForcePlaySettlement:
    execution = execute_plan2_force_play_queue(
        plan2_state, native_state, (command,), database=database
    )
    return execution.settlements[0]


def force_play_accounting_dict(database: Path = DEFAULT_DATABASE) -> dict[str, object]:
    return probe_accounting(database).to_dict()


ForcePlayPlanResult = Plan2ForcePlayPlanResult
Plan2ForcePlayContract = ForcePlayEffectContract
Plan2ForcePlayCommand = Plan2ForcePlayQueuedCommand


def native_audit_to_dict(database: Path = DEFAULT_DATABASE) -> dict[str, object]:
    contract = contract_for_effect(EFFECT_ID, database)
    target = load_target_card_master(database)
    accounting = probe_accounting(database)
    return {
        "schema_version": 1,
        "scope": "Plan2 standalone ForcePlayCardSearch exact target",
        "authoritative": {
            "master_database": str(Path(database)),
            "android_version": ANDROID_VERSION,
            "android_binary": ANDROID_BINARY,
            "pc_cross_check": "ForcePlaySearchEffectExecutor class shape only; Android body is execution authority",
        },
        "target_card": target.to_dict(),
        "effect_contract": {
            "row": contract.row.to_dict(),
            "search": asdict(contract.search),
            "executable": contract.executable,
        },
        "native_addresses": {
            "executor_ctor": ANDROID_EXECUTOR_CTOR,
            "executor_execute": ANDROID_EXECUTOR_EXECUTE,
            "get_search_card_list": ANDROID_GET_SEARCH_CARD_LIST,
            "pick_card_position_list_impl": ANDROID_PICK_IMPL,
            "random_order_key_closure": ANDROID_PICK_RANDOM_KEY,
            "recursion_predicate": ANDROID_RECURSION_PREDICATE,
            "create_use_pool_command": ANDROID_CREATE_USE_POOL,
            "set_card_force_play": ANDROID_SET_FORCE_DIFFERENCE,
        },
        "pc_structural_cross_check": {
            "type_index": 3558,
            "type": "Campus.InGame.Exam.ForcePlaySearchEffectExecutor",
            "field_order": [
                "_search",
                "_pickRangeType",
                "_pickCountMin",
                "_pickCountMax",
                "_pickCountType",
                "_search2",
                "_value",
            ],
            "methods": [".ctor", "ExecuteEffect"],
            "android_type_index": 3675,
            "authority_limit": "structural cross-check only; Android v3.2.3 native body is execution authority",
        },
        "native_semantics": {
            "search_pool": "current Hand, R rarity, execution-time ordered card positions",
            "selection": "OrderType.Random gives one parameterless RNG key per eligible candidate; stable sort; LimitCount=2; final selected list returns original ticket order",
            "pick_range": "All; pickCountType/min/max are nominal zero/unknown and do not add a second random count",
            "recursive_guard": "additionalCondition rejects a candidate when any current play effect has enum value 24; no numeric recursion depth is claimed for this executor",
            "source_position": "PositionType_PlayHand (enum 9) creates UsePool; other branches are outside this exact Hand search",
            "forced_cost": "CreateUsePoolCommand isConsumeCost=false; no stamina payment",
            "forced_playable": "isUsePlayableCount=false; no playable-count consumption",
            "card_play_count": "successful settlement increments the live GUID card once; native has two card PlayCardCount callsites requiring object synchronization",
            "global_play_count": "MovePlayCard settlement increments global/per-turn Plan2 counts after external move",
            "history_listener": "normal listener snapshot and history callback remain in shared card core; standalone reports exactly one settlement per successful command",
            "move_destination": "resolve live PlayMovePositionType after direct effects; current target R cards are Master Lost or Grave; unknown/altered destination fails closed",
            "queue_phase": "executor appends command and CardForcePlay difference in source order; core inserts difference-triggered work depth-first, then post-effect callback",
        },
        "reused_primitives": {
            "plan3_use_pool": "Plan3UsePoolCommand and stage_plan3_use_pool_guid; source GUID/index is resolved at execution time across Hand/Deck/Grave/Hold/Lost",
            "plan3_native_zone": "Plan3NativeCard/Plan3NativeState ordered GUID identity and immutable zone projection",
            "plan3_native_search": "Plan3NativeCardMoveTarget and the same zone-order model; Plan3NativeState.card_move_candidates is intentionally not broadened because its neutral-only validator would reject the authoritative R/Random search",
            "plan3_rng": "shared exam_native_rng.next_int32 xorshift32 primitive; selection layer adds one key per eligible candidate",
            "plan2_shared_runtime": "complete_plan2_card_play_after_move increments exam_card_play_count and turn_card_play_count only after external move",
            "plan_neutral": ["GUID identity", "zone order", "RNG state", "UsePool staging", "recursive effect-type-24 exclusion"],
            "plan2_adaptation": ["stamina scalar", "Unknown cost + stamina=5 outer card metadata", "playable value-add/status rows", "Plan2 global/turn play counters", "Plan2 listener/status boundary"],
        },
        "transaction_order": list(FORCED_USE_POOL_TRANSACTION_ORDER),
        "core_hook_requirements": list(CORE_HOOK_REQUIREMENTS),
        "accounting": accounting.to_dict(),
        "claim_boundary": {
            "direct_unlocked_versions": [
                {"card_id": TARGET_CARD_ID, "upgrade": TARGET_CARD_UPGRADE}
            ],
            "co_overlap_only_versions": [
                item.to_dict()
                for item in accounting.versions
                if item.relation == "co-overlap-only"
            ],
            "official_coverage_json_modified": False,
        },
    }


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "ANDROID_BINARY",
    "ANDROID_EXECUTOR_CTOR",
    "ANDROID_EXECUTOR_EXECUTE",
    "ANDROID_GET_SEARCH_CARD_LIST",
    "ANDROID_PICK_IMPL",
    "ANDROID_PICK_RANDOM_KEY",
    "ANDROID_RECURSION_PREDICATE",
    "ANDROID_CREATE_USE_POOL",
    "ANDROID_SET_FORCE_DIFFERENCE",
    "CORE_HOOK_REQUIREMENTS",
    "DIFFERENCE_TYPE",
    "DIFFERENCE_TYPE_VALUE",
    "EFFECT_ID",
    "EFFECT_TYPE",
    "EFFECT_TYPE_VALUE",
    "FORCED_USE_POOL_TRANSACTION_ORDER",
    "ForcePlayAccounting",
    "ForcePlayDifference",
    "ForcePlayEffectContract",
    "ForcePlayEffectRow",
    "ForcePlayExecutionInput",
    "ForcePlayPlanResult",
    "ForcePlayResolvedBranch",
    "ForcePlayTrace",
    "ForcePlayUnresolvedBranch",
    "Plan2ForcePlayPlanResult",
    "Plan2ForcePlayContract",
    "Plan2ForcePlayCommand",
    "Plan2ForcePlayQueueExecution",
    "Plan2ForcePlayQueuedCommand",
    "Plan2ForcePlaySearchError",
    "Plan2ForcePlaySearchInputError",
    "Plan2ForcePlaySearchResolutionError",
    "Plan2ForcePlaySettlement",
    "Plan2ForcePlayStage",
    "Plan2State",
    "Plan2TargetCardMaster",
    "SEARCH_ID",
    "TARGET_CARD_ID",
    "TARGET_CARD_UPGRADE",
    "TARGET_DRAW_EFFECT_ID",
    "TARGET_PLAYABLE_EFFECT_ID",
    "TARGET_STATUS_DRAW_ID",
    "TARGET_STATUS_FORCE_ID",
    "contract_for_effect",
    "apply_plan2_force_play_card_search",
    "execute_force_play_card_search",
    "execute_plan2_force_play_card_search",
    "execute_plan2_force_play_queue",
    "force_play_accounting_dict",
    "load_affected_card_versions",
    "load_force_play_effect_row",
    "load_target_card_master",
    "native_audit_to_dict",
    "plan_force_play_card_search",
    "plan2_force_play_card_search",
    "probe_accounting",
    "settle_plan2_force_play_command",
    "stage_plan2_force_play_guid",
]
