"""Master-backed Plan2 drink runtime for Initial Regular offline stages.

The normalized drink catalog owns Master identity and the original
``produceDrinkEffectIds`` ordering.  This module adds only Plan2 execution
bindings whose scalar owners already exist in the native horizon.  A drink is
atomic at provisioning: one unknown source, handler, nested shape, or plan
type blocks the whole drink instead of dropping that effect.

Outer drink references are an ordered inventory and may repeat because the
game serializes each slot as ``drinkList[{_id}]`` without a separate GUID.
Unique references remain their own action identity; repeated references gain
a deterministic slot identity while the original ordered values are retained
for post-stage persistence.  Callers with opaque references may still inject
an exact Master-ID resolver.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Final, TypeAlias

from .card_search import load_produce_card_search
from .drink_catalog import (
    EFFECT_BLOCK,
    EFFECT_CARD_PLAY_AGGRESSIVE,
    EFFECT_LESSON,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_REVIEW,
    EFFECT_STAMINA_RECOVER_FIX,
    DrinkCatalog,
    DrinkCatalogDrink,
    DrinkEffectCatalogEntry,
    load_drink_catalog,
)
from .initial_regular_inner_protocol import InitialRegularInnerStageRequest
from .initial_regular_plan2_master_runtime import (
    InitialRegularPlan2MasterRuntimeBlocked,
)
from .plan2_native_horizon import (
    DRINK_HANDLER_AGGRESSIVE,
    DRINK_HANDLER_BLOCK,
    DRINK_HANDLER_CARD_UPGRADE_HAND_ALL,
    DRINK_HANDLER_END_TURN_STATUS,
    DRINK_HANDLER_HAND_GRAVE_DRAW,
    DRINK_HANDLER_LESSON,
    DRINK_HANDLER_LESSON_DEPEND_REVIEW,
    DRINK_HANDLER_LESSON_VALUE_MULTIPLE,
    DRINK_HANDLER_PLAYABLE_ADD,
    DRINK_HANDLER_PLAY_COUNT_BUFF,
    DRINK_HANDLER_REVIEW,
    DRINK_HANDLER_CARD_DRAW,
    DRINK_HANDLER_CARD_CREATE_SEARCH,
    DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL,
    DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND,
    DRINK_HANDLER_START_PLAY_STATUS,
    DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN,
    DRINK_HANDLER_STAMINA_CONSUMPTION_ADD,
    DRINK_HANDLER_STAMINA_RECOVER_FIX,
    DRINK_HANDLER_STAMINA_REDUCE_FIX,
    Plan2NativeBlocker,
    Plan2NativeDrinkEffect,
    Plan2NativeDrinkCardMoveProgram,
    Plan2NativeDrinkPlayCountBuffProgram,
    Plan2NativeDrinkInstance,
    Plan2NativeDrinkRuntime,
    Plan2NativeHorizonState,
)
from .master_db import DEFAULT_DATABASE
from .plan2_native_catalog_review_dynamic import (
    OP_INSTALL_LESSON_MULTIPLE,
    Plan2NativeReviewDynamicProgram,
)
from .plan2_native_catalog_stamina import StaminaConsumptionDownEffect
from .plan2_stamina_consumption_add import StaminaConsumptionAddEffect
from .plan2_start_play_trigger import (
    StartPlayCatalogError,
    load_plan2_start_play_status_program,
)
from .plan2_end_turn_trigger import (
    Plan2EndTurnContractError,
    load_plan2_end_turn_program,
)
from .plan2_native_stage_bootstrap import Plan2NativeStageRuntime
from .plan3_card_create_search import (
    CardCreateSearchEffectContract,
    CardCreateSearchError,
    resolve_card_create_search_contract,
)
from .plan2_drink_force_play_random_pool import (
    Plan2DrinkForcePlayRandomPoolProgram,
    RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID,
    load_plan2_drink_force_play_random_pool_program,
)


PLAN2_MASTER_DRINK_RUNTIME_ADAPTER_ID: Final = (
    "initial-regular.plan2.master-drink-rules.v1"
)
PLAN2_DRINK_PLAN_TYPES: Final = frozenset(
    {"ProducePlanType_Common", "ProducePlanType_Plan2"}
)
EFFECT_LESSON_DEPEND_REVIEW: Final = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
EFFECT_STATUS_ENCHANT: Final = "ProduceExamEffectType_ExamStatusEnchant"
EFFECT_HAND_GRAVE_DRAW: Final = (
    "ProduceExamEffectType_ExamHandGraveCountCardDraw"
)
EFFECT_LESSON_VALUE_MULTIPLE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
EFFECT_STAMINA_CONSUMPTION_DOWN: Final = (
    "ProduceExamEffectType_ExamStaminaConsumptionDown"
)
EFFECT_STAMINA_CONSUMPTION_ADD: Final = (
    "ProduceExamEffectType_ExamStaminaConsumptionAdd"
)
EFFECT_STAMINA_REDUCE_FIX: Final = (
    "ProduceExamEffectType_ExamStaminaReduceFix"
)
EFFECT_CARD_DRAW: Final = "ProduceExamEffectType_ExamCardDraw"
EFFECT_CARD_MOVE: Final = "ProduceExamEffectType_ExamCardMove"
EFFECT_CARD_CREATE_SEARCH: Final = (
    "ProduceExamEffectType_ExamCardCreateSearch"
)
EFFECT_FORCE_PLAY_CARD_SEARCH: Final = (
    "ProduceExamEffectType_ExamForcePlayCardSearch"
)
EFFECT_PLAY_COUNT_BUFF: Final = (
    "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff"
)

_HANDLER_BY_EFFECT_TYPE: Final = {
    EFFECT_LESSON: DRINK_HANDLER_LESSON,
    EFFECT_LESSON_DEPEND_REVIEW: DRINK_HANDLER_LESSON_DEPEND_REVIEW,
    EFFECT_REVIEW: DRINK_HANDLER_REVIEW,
    EFFECT_BLOCK: DRINK_HANDLER_BLOCK,
    EFFECT_CARD_PLAY_AGGRESSIVE: DRINK_HANDLER_AGGRESSIVE,
    EFFECT_PLAYABLE_VALUE_ADD: DRINK_HANDLER_PLAYABLE_ADD,
    EFFECT_STAMINA_RECOVER_FIX: DRINK_HANDLER_STAMINA_RECOVER_FIX,
    "ProduceExamEffectType_ExamCardUpgrade": (
        DRINK_HANDLER_CARD_UPGRADE_HAND_ALL
    ),
    EFFECT_STATUS_ENCHANT: DRINK_HANDLER_END_TURN_STATUS,
    EFFECT_HAND_GRAVE_DRAW: DRINK_HANDLER_HAND_GRAVE_DRAW,
    EFFECT_LESSON_VALUE_MULTIPLE: DRINK_HANDLER_LESSON_VALUE_MULTIPLE,
    EFFECT_STAMINA_CONSUMPTION_DOWN: (
        DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN
    ),
    EFFECT_STAMINA_CONSUMPTION_ADD: (
        DRINK_HANDLER_STAMINA_CONSUMPTION_ADD
    ),
    EFFECT_STAMINA_REDUCE_FIX: DRINK_HANDLER_STAMINA_REDUCE_FIX,
    EFFECT_CARD_DRAW: DRINK_HANDLER_CARD_DRAW,
    EFFECT_CARD_MOVE: DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND,
    EFFECT_CARD_CREATE_SEARCH: DRINK_HANDLER_CARD_CREATE_SEARCH,
    EFFECT_FORCE_PLAY_CARD_SEARCH: DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL,
    EFFECT_PLAY_COUNT_BUFF: DRINK_HANDLER_PLAY_COUNT_BUFF,
}

_HAND_ALL_CARD_UPGRADE_EFFECT_ID: Final = (
    "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"
)
_IDOL_UNIQUE_CARD_MOVE_EFFECT_ID: Final = (
    "e_effect-exam_card_move-p_card_search-deck_grave-"
    "idol-unique-1-hand-all-0_0"
)
_IDOL_UNIQUE_CARD_MOVE_SEARCH_ID: Final = (
    "p_card_search-deck_grave-idol-unique-1"
)
_SSR_CARD_CREATE_EFFECT_ID: Final = (
    "e_effect-exam_card_create_search-0001-"
    "p_card_search-random-random_pool-p_random_pool-ssr-upgrade_1-1-"
    "hand-random-1_1"
)
_RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID: Final = (
    RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID
)
_ACTIVE_SKILL_PLAY_COUNT_BUFF_EFFECT_ID: Final = (
    "e_effect-exam_card_search_effect_play_count_buff-0001-01-01-"
    "p_card_search-active_skill-n-r-sr-ssr-playing-all-0_0"
)
_ACTIVE_SKILL_PLAY_COUNT_BUFF_SEARCH_ID: Final = (
    "p_card_search-active_skill-n-r-sr-ssr-playing"
)
_PLAY_COUNT_BUFF_EFFECT_GROUP_ID: Final = (
    "effect_group-visible-exam_card_search_effect_play_count_buff-000"
)

Plan2DrinkCatalogLoader: TypeAlias = Callable[[], DrinkCatalog]
Plan2DrinkRefResolver: TypeAlias = Callable[[str], str]
Plan2BaseRuntimeProvider: TypeAlias = Callable[
    [InitialRegularInnerStageRequest], Plan2NativeStageRuntime
]


def _identity_ref(value: str) -> str:
    return value


def _block(
    blockers: list[Plan2NativeBlocker], code: str, detail: str = ""
) -> None:
    value = Plan2NativeBlocker(code, detail)
    if value not in blockers:
        blockers.append(value)


class InitialRegularPlan2DrinkRuntimeBlocked(
    InitialRegularPlan2MasterRuntimeBlocked
):
    """Typed adapter error; the common adapter preserves every blocker."""


@dataclass(frozen=True, slots=True)
class Plan2NativeDrinkCompilation:
    instance: Plan2NativeDrinkInstance | None = None
    blockers: tuple[Plan2NativeBlocker, ...] = ()

    def __post_init__(self) -> None:
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2NativeBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan2NativeBlocker values")
        if self.instance is not None and not isinstance(
            self.instance, Plan2NativeDrinkInstance
        ):
            raise TypeError("instance must be Plan2NativeDrinkInstance or None")
        if (self.instance is None) == (not blockers):
            raise ValueError("compilation must contain instance xor blockers")
        object.__setattr__(self, "blockers", blockers)

    @property
    def supported(self) -> bool:
        return self.instance is not None and not self.blockers


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2DrinkRuntimeProvision:
    runtime: Plan2NativeStageRuntime | None = None
    blockers: tuple[Plan2NativeBlocker, ...] = ()

    def __post_init__(self) -> None:
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2NativeBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan2NativeBlocker values")
        if self.runtime is not None and not isinstance(
            self.runtime, Plan2NativeStageRuntime
        ):
            raise TypeError("runtime must be Plan2NativeStageRuntime or None")
        if (self.runtime is None) == (not blockers):
            raise ValueError("provision must contain runtime xor blockers")
        object.__setattr__(self, "blockers", blockers)

    @property
    def supported(self) -> bool:
        return self.runtime is not None and not self.blockers

    def require_runtime(self) -> Plan2NativeStageRuntime:
        if not self.supported:
            raise InitialRegularPlan2DrinkRuntimeBlocked(self.blockers)
        assert self.runtime is not None
        return self.runtime


def _plain_exam_shape(effect: DrinkEffectCatalogEntry) -> bool:
    """Reject every execution-bearing field outside the selected handler."""

    return bool(
        effect.source_kind == "exam"
        and not effect.produce_card_search_id
        and not effect.produce_card_search_id2
        and not effect.produce_card_status_enchant_id
        and not effect.produce_exam_status_enchant_id
        and not effect.produce_exam_trigger_id
        and not effect.produce_exam_trigger_effect_ids
        and not effect.chain_produce_exam_effect_id
        and not effect.chain_produce_exam_effect_ids
        and not effect.target_produce_card_id
        and effect.target_upgrade_count == 0
        and not effect.produce_card_grow_effect_ids
        and not effect.pick_count_reference_produce_card_search_id
        and not effect.pick_count_reference_produce_card_search_id2
        and effect.pick_count_min == 0
        and effect.pick_count_max == 0
        and effect.pick_count_min2 == 0
        and effect.pick_count_max2 == 0
        and not effect.produce_resource_type
        and not effect.produce_rewards
        and not effect.produce_step_event_detail_id
        and effect.move_position_type
        == "ProduceCardMovePositionType_Unknown"
        and effect.pick_range_type == "ProducePickRangeType_Unknown"
        and effect.pick_range_type2 == "ProducePickRangeType_Unknown"
        and effect.pick_count_type == "ProducePickCountType_Unknown"
        and effect.pick_count_type2 == "ProducePickCountType_Unknown"
        and effect.target_exam_effect_type
        == "ProduceExamEffectType_Unknown"
        and effect.effect_value_min == 0
        and effect.effect_value_max == 0
    )


def _handler_shape(effect: DrinkEffectCatalogEntry, handler: str) -> bool:
    if handler in {
        DRINK_HANDLER_END_TURN_STATUS,
        DRINK_HANDLER_START_PLAY_STATUS,
    }:
        return bool(
            effect.effect_value1 == 0
            and effect.effect_value2 == 0
            and effect.effect_count == 0
            and (
                effect.effect_turn == -1
                if handler == DRINK_HANDLER_END_TURN_STATUS
                else effect.effect_turn > 0
            )
            and effect.produce_exam_status_enchant_id
            and effect.produce_exam_trigger_id
            and effect.produce_exam_trigger_effect_ids
        )
    if effect.effect_value2 != 0:
        return False
    if handler in {
        DRINK_HANDLER_LESSON,
        DRINK_HANDLER_LESSON_DEPEND_REVIEW,
    }:
        return effect.effect_value1 > 0 and effect.effect_count > 0
    if handler == DRINK_HANDLER_PLAYABLE_ADD:
        return effect.effect_value1 == 0 and effect.effect_count > 0
    if handler == DRINK_HANDLER_CARD_UPGRADE_HAND_ALL:
        return bool(
            effect.source_id == _HAND_ALL_CARD_UPGRADE_EFFECT_ID
            and effect.effect_value1 == 0
            and effect.effect_count == 0
            and effect.produce_card_search_id == "p_card_search-hand"
            and effect.pick_range_type == "ProducePickRangeType_All"
        )
    if handler == DRINK_HANDLER_HAND_GRAVE_DRAW:
        return bool(
            effect.effect_value1 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    if handler == DRINK_HANDLER_CARD_DRAW:
        return bool(
            effect.effect_value1 > 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    if handler == DRINK_HANDLER_LESSON_VALUE_MULTIPLE:
        return bool(
            effect.effect_value1 > 0
            and effect.effect_count == 0
            and effect.effect_turn > 0
        )
    if handler == DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN:
        return bool(
            effect.effect_value1 == 0
            and effect.effect_count == 0
            and effect.effect_turn > 0
        )
    if handler == DRINK_HANDLER_STAMINA_CONSUMPTION_ADD:
        return bool(
            effect.effect_value1 == 0
            and effect.effect_count == 0
            and effect.effect_turn > 0
        )
    if handler == DRINK_HANDLER_STAMINA_REDUCE_FIX:
        return bool(
            effect.effect_value1 > 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    if handler == DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND:
        return bool(
            effect.source_id == _IDOL_UNIQUE_CARD_MOVE_EFFECT_ID
            and effect.effect_value1 == 0
            and effect.effect_value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
            and effect.produce_card_search_id
            == _IDOL_UNIQUE_CARD_MOVE_SEARCH_ID
            and effect.move_position_type
            == "ProduceCardMovePositionType_Hand"
            and effect.pick_range_type == "ProducePickRangeType_All"
            and effect.pick_count_type == "ProducePickCountType_Unknown"
            and effect.pick_count_min == 0
            and effect.pick_count_max == 0
        )
    if handler == DRINK_HANDLER_CARD_CREATE_SEARCH:
        return bool(
            effect.source_id == _SSR_CARD_CREATE_EFFECT_ID
            and effect.effect_value1 == 1
            and effect.effect_value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    if handler == DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL:
        return bool(
            effect.source_id == _RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID
            and effect.effect_value1 == 0
            and effect.effect_value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    if handler == DRINK_HANDLER_PLAY_COUNT_BUFF:
        return bool(
            effect.source_id == _ACTIVE_SKILL_PLAY_COUNT_BUFF_EFFECT_ID
            and effect.effect_value1 == 1
            and effect.effect_value2 == 0
            and effect.effect_count == 1
            and effect.effect_turn == 1
            and effect.produce_card_search_id
            == _ACTIVE_SKILL_PLAY_COUNT_BUFF_SEARCH_ID
            and tuple(effect.effect_group_ids)
            == (_PLAY_COUNT_BUFF_EFFECT_GROUP_ID,)
        )
    if effect.effect_turn != 0:
        return False
    return effect.effect_value1 > 0 and effect.effect_count == 0


def _load_idol_unique_card_move_program(
    effect: DrinkEffectCatalogEntry,
    *,
    database: Path,
) -> Plan2NativeDrinkCardMoveProgram:
    """Project the exact search-tag predicate from current Master rows."""

    rule = load_produce_card_search(effect.produce_card_search_id, database)
    expected = {
        "id": _IDOL_UNIQUE_CARD_MOVE_SEARCH_ID,
        "card_rarities": (),
        "produce_card_ids": (),
        "upgrade_counts": (),
        "plan_type": "ProducePlanType_Unknown",
        "card_categories": (),
        "card_status_type": "ProduceCardSearchStatusType_Unknown",
        "order_type": "ProduceCardOrderType_Unknown",
        "card_position_type": "ProduceCardPositionType_DeckGrave",
        "card_search_tag": "idol-unique",
        "produce_card_random_pool_id": "",
        "limit_count": 1,
        "stamina_min_max_type": "ConditionMinMaxType_Unknown",
        "stamina_min": 0,
        "stamina_max": 0,
        "exam_effect_type": "ProduceExamEffectType_Unknown",
        "effect_group_ids": (),
        "is_self": False,
        "produce_card_pool_id": "",
        "cost_type": "ExamCostType_Unknown",
        "is_customized": False,
    }
    mismatch = next(
        (
            name
            for name, value in expected.items()
            if getattr(rule, name) != value
        ),
        None,
    )
    if mismatch is not None:
        raise ValueError(f"idol-unique-card-search-drift:{mismatch}")
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = tuple(
            connection.execute(
                "SELECT id, upgrade_count, raw_json FROM card "
                "ORDER BY id, upgrade_count"
            )
        )
    refs: list[tuple[str, int]] = []
    for row in rows:
        raw = json.loads(str(row["raw_json"]))
        if not isinstance(raw, dict):
            raise ValueError("card-master-raw-json-not-object")
        if raw.get("searchTag", "") == rule.card_search_tag:
            refs.append((str(row["id"]), int(row["upgrade_count"])))
    return Plan2NativeDrinkCardMoveProgram(
        effect_id=effect.source_id,
        search_id=rule.id,
        card_search_tag=rule.card_search_tag,
        limit_count=rule.limit_count,
        destination=effect.move_position_type,
        pick_range_type=effect.pick_range_type,
        master_refs=tuple(refs),
    )


def _load_card_create_search_program(
    effect: DrinkEffectCatalogEntry,
    *,
    database: Path,
) -> CardCreateSearchEffectContract:
    """Resolve the complete random-pool effect/search/pool Master graph."""

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM effect WHERE id = ?",
            (effect.source_id,),
        ).fetchone()
    if row is None:
        raise KeyError(effect.source_id)
    contract = resolve_card_create_search_contract(row)
    if contract.effect_id != effect.source_id:
        raise ValueError("card-create-search-effect-id-drift")
    return contract


def _load_force_play_random_pool_program(
    effect: DrinkEffectCatalogEntry,
    *,
    database: Path,
) -> Plan2DrinkForcePlayRandomPoolProgram:
    """Resolve the one detached rush-idol RandomPool ForcePlay graph."""

    if effect.source_id != _RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID:
        raise ValueError("force-play-random-pool-effect-id-drift")
    return load_plan2_drink_force_play_random_pool_program(database)


def _load_play_count_buff_program(
    effect: DrinkEffectCatalogEntry,
    *,
    database: Path,
) -> Plan2NativeDrinkPlayCountBuffProgram:
    """Validate the exact finite ActiveSkill Playing-search contract."""

    rule = load_produce_card_search(effect.produce_card_search_id, database)
    expected = {
        "id": _ACTIVE_SKILL_PLAY_COUNT_BUFF_SEARCH_ID,
        "card_rarities": (
            "ProduceCardRarity_N",
            "ProduceCardRarity_R",
            "ProduceCardRarity_Sr",
            "ProduceCardRarity_Ssr",
        ),
        "produce_card_ids": (),
        "upgrade_counts": (),
        "plan_type": "ProducePlanType_Unknown",
        "card_categories": ("ProduceCardCategory_ActiveSkill",),
        "card_status_type": "ProduceCardSearchStatusType_Unknown",
        "order_type": "ProduceCardOrderType_Unknown",
        "card_position_type": "ProduceCardPositionType_Playing",
        "card_search_tag": "",
        "produce_card_random_pool_id": "",
        "limit_count": 0,
        "stamina_min_max_type": "ConditionMinMaxType_Unknown",
        "stamina_min": 0,
        "stamina_max": 0,
        "exam_effect_type": "ProduceExamEffectType_Unknown",
        "effect_group_ids": (),
        "is_self": False,
        "produce_card_pool_id": "",
        "cost_type": "ExamCostType_Unknown",
        "is_customized": False,
    }
    mismatch = next(
        (
            name
            for name, value in expected.items()
            if getattr(rule, name) != value
        ),
        None,
    )
    if mismatch is not None:
        raise ValueError(f"play-count-buff-card-search-drift:{mismatch}")
    return Plan2NativeDrinkPlayCountBuffProgram(
        effect_id=effect.source_id,
        search_id=rule.id,
        value1=effect.effect_value1,
        effect_count=effect.effect_count,
        effect_turn=effect.effect_turn,
        effect_group_ids=tuple(effect.effect_group_ids),
    )


def compile_plan2_native_drink_instance(
    drink: DrinkCatalogDrink,
    *,
    instance_id: str,
    session_ref: str | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeDrinkCompilation:
    """Compile one complete drink, retaining Master effect ordering."""

    if not isinstance(drink, DrinkCatalogDrink):
        raise TypeError("drink must be DrinkCatalogDrink")
    if not isinstance(instance_id, str) or not instance_id:
        raise ValueError("instance_id must be non-empty text")
    if session_ref is not None and (
        not isinstance(session_ref, str) or not session_ref
    ):
        raise ValueError("session_ref must be non-empty text or None")
    blockers: list[Plan2NativeBlocker] = []
    if drink.plan_type not in PLAN2_DRINK_PLAN_TYPES:
        _block(
            blockers,
            "plan2-drink-plan-type-unsupported",
            f"{drink.id}:{drink.plan_type}",
        )
    expected_ids = tuple(drink.produce_drink_effect_ids)
    actual_ids = tuple(value.drink_effect_id for value in drink.effect_refs)
    actual_indexes = tuple(value.effect_index for value in drink.effect_refs)
    if expected_ids != actual_ids or actual_indexes != tuple(
        range(len(actual_ids))
    ):
        _block(blockers, "plan2-drink-effect-order-invalid", drink.id)

    compiled: list[Plan2NativeDrinkEffect] = []
    ordered_effect_ids = tuple(
        reference.effect.source_id for reference in drink.effect_refs
    )
    for reference in drink.effect_refs:
        effect = reference.effect
        detail = (
            f"{drink.id}:{reference.effect_index}:"
            f"{reference.drink_effect_id}:{effect.source_id}"
        )
        handler = _HANDLER_BY_EFFECT_TYPE.get(effect.effect_type)
        if (
            effect.effect_type == EFFECT_STATUS_ENCHANT
            and effect.produce_exam_trigger_id == "e_trigger-start_play"
        ):
            handler = DRINK_HANDLER_START_PLAY_STATUS
        if handler is None:
            _block(
                blockers,
                "plan2-drink-effect-handler-unbound",
                f"{detail}:{effect.effect_type or '<missing>'}",
            )
            continue
        end_turn_program = None
        review_dynamic_program = None
        stamina_down_effect = None
        stamina_add_effect = None
        start_play_program = None
        card_move_program = None
        card_create_program = None
        force_play_random_pool_program = None
        play_count_buff_program = None
        if handler == DRINK_HANDLER_END_TURN_STATUS:
            try:
                end_turn_program = load_plan2_end_turn_program(effect.source_id)
            except (KeyError, OSError, Plan2EndTurnContractError) as error:
                _block(
                    blockers,
                    "plan2-drink-end-turn-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
            if (
                effect.produce_exam_status_enchant_id
                != end_turn_program.status_enchant_id
                or effect.produce_exam_trigger_id != end_turn_program.trigger_id
                or tuple(effect.produce_exam_trigger_effect_ids)
                != tuple(value.effect_id for value in end_turn_program.effects)
                or tuple(effect.effect_group_ids)
                != end_turn_program.wrapper_effect_group_ids
            ):
                _block(
                    blockers,
                    "plan2-drink-end-turn-program-mismatch",
                    detail,
                )
                continue
        elif handler == DRINK_HANDLER_START_PLAY_STATUS:
            try:
                start_play_program = load_plan2_start_play_status_program(
                    effect.source_id
                )
            except (OSError, StartPlayCatalogError) as error:
                _block(
                    blockers,
                    "plan2-drink-start-play-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
            if (
                effect.produce_exam_status_enchant_id
                != start_play_program.status.id
                or effect.produce_exam_trigger_id
                != start_play_program.status.trigger_id
                or tuple(effect.produce_exam_trigger_effect_ids)
                != start_play_program.child_effect_ids
                or tuple(effect.effect_group_ids)
                != start_play_program.wrapper.effect_group_ids
            ):
                _block(
                    blockers,
                    "plan2-drink-start-play-program-mismatch",
                    detail,
                )
                continue
        elif handler == DRINK_HANDLER_LESSON_VALUE_MULTIPLE:
            try:
                review_dynamic_program = Plan2NativeReviewDynamicProgram(
                    card_id=drink.id,
                    upgrade=0,
                    slot_index=reference.effect_index,
                    ordered_effect_ids=ordered_effect_ids,
                    effect_id=effect.source_id,
                    effect_type=effect.effect_type,
                    operation_kind=OP_INSTALL_LESSON_MULTIPLE,
                    value1=effect.effect_value1,
                    value2=effect.effect_value2,
                    effect_count=effect.effect_count,
                    effect_turn=effect.effect_turn,
                )
            except (TypeError, ValueError) as error:
                _block(
                    blockers,
                    "plan2-drink-lesson-multiple-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
        elif handler == DRINK_HANDLER_STAMINA_CONSUMPTION_DOWN:
            try:
                stamina_down_effect = StaminaConsumptionDownEffect(
                    effect_id=effect.source_id,
                    effect_turn=effect.effect_turn,
                    value1=effect.effect_value1,
                    value2=effect.effect_value2,
                    effect_count=effect.effect_count,
                )
            except (TypeError, ValueError) as error:
                _block(
                    blockers,
                    "plan2-drink-stamina-down-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
        elif handler == DRINK_HANDLER_STAMINA_CONSUMPTION_ADD:
            try:
                stamina_add_effect = StaminaConsumptionAddEffect(
                    effect_id=effect.source_id,
                    effect_turn=effect.effect_turn,
                    value1=effect.effect_value1,
                    value2=effect.effect_value2,
                    effect_count=effect.effect_count,
                )
            except (TypeError, ValueError) as error:
                _block(
                    blockers,
                    "plan2-drink-stamina-add-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
        elif handler == DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND:
            try:
                card_move_program = _load_idol_unique_card_move_program(
                    effect,
                    database=Path(database),
                )
            except (KeyError, OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as error:
                _block(
                    blockers,
                    "plan2-drink-card-move-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
        elif handler == DRINK_HANDLER_CARD_CREATE_SEARCH:
            try:
                card_create_program = _load_card_create_search_program(
                    effect,
                    database=Path(database),
                )
            except (
                CardCreateSearchError,
                KeyError,
                OSError,
                sqlite3.Error,
                TypeError,
                ValueError,
            ) as error:
                _block(
                    blockers,
                    "plan2-drink-card-create-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
        elif handler == DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL:
            try:
                force_play_random_pool_program = (
                    _load_force_play_random_pool_program(
                        effect,
                        database=Path(database),
                    )
                )
            except (
                KeyError,
                OSError,
                sqlite3.Error,
                TypeError,
                ValueError,
            ) as error:
                _block(
                    blockers,
                    "plan2-drink-force-play-random-pool-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
        elif handler == DRINK_HANDLER_PLAY_COUNT_BUFF:
            try:
                play_count_buff_program = _load_play_count_buff_program(
                    effect,
                    database=Path(database),
                )
            except (KeyError, OSError, sqlite3.Error, TypeError, ValueError) as error:
                _block(
                    blockers,
                    "plan2-drink-play-count-buff-program-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
                continue
        shape_supported = (
            _handler_shape(effect, handler)
            if handler in {
                DRINK_HANDLER_CARD_UPGRADE_HAND_ALL,
                DRINK_HANDLER_END_TURN_STATUS,
                DRINK_HANDLER_START_PLAY_STATUS,
                DRINK_HANDLER_CARD_MOVE_IDOL_UNIQUE_HAND,
                DRINK_HANDLER_CARD_CREATE_SEARCH,
                DRINK_HANDLER_FORCE_PLAY_RANDOM_POOL,
                DRINK_HANDLER_PLAY_COUNT_BUFF,
            }
            else _plain_exam_shape(effect) and _handler_shape(effect, handler)
        )
        if (
            reference.link.source_kind != "exam"
            or reference.link.source_id != effect.source_id
            or not shape_supported
        ):
            _block(
                blockers,
                "plan2-drink-effect-shape-unbound",
                detail,
            )
            continue
        try:
            compiled.append(
                Plan2NativeDrinkEffect(
                    effect_index=reference.effect_index,
                    drink_effect_id=reference.drink_effect_id,
                    effect_id=effect.source_id,
                    effect_type=effect.effect_type,
                    handler=handler,
                    value1=effect.effect_value1,
                    value2=effect.effect_value2,
                    count=effect.effect_count,
                    turn=effect.effect_turn,
                    end_turn_program=end_turn_program,
                    review_dynamic_program=review_dynamic_program,
                    stamina_down_effect=stamina_down_effect,
                    stamina_add_effect=stamina_add_effect,
                    start_play_program=start_play_program,
                    card_move_program=card_move_program,
                    card_create_program=card_create_program,
                    force_play_random_pool_program=(
                        force_play_random_pool_program
                    ),
                    play_count_buff_program=play_count_buff_program,
                )
            )
        except (TypeError, ValueError) as error:
            _block(
                blockers,
                "plan2-drink-effect-compile-invalid",
                f"{detail}:{type(error).__name__}:{error}",
            )
    if blockers or len(compiled) != len(drink.effect_refs):
        return Plan2NativeDrinkCompilation(blockers=tuple(blockers))
    try:
        instance = Plan2NativeDrinkInstance(
            instance_id=instance_id,
            drink_id=drink.id,
            plan_type=drink.plan_type,
            effects=tuple(compiled),
            session_ref=(instance_id if session_ref is None else session_ref),
        )
    except (TypeError, ValueError) as error:
        return Plan2NativeDrinkCompilation(
            blockers=(
                Plan2NativeBlocker(
                    "plan2-drink-instance-invalid",
                    f"{drink.id}:{type(error).__name__}:{error}",
                ),
            )
        )
    return Plan2NativeDrinkCompilation(instance=instance)


def _base_runtime(
    request: InitialRegularInnerStageRequest,
    provider: Plan2BaseRuntimeProvider | None,
    blockers: list[Plan2NativeBlocker],
) -> Plan2NativeStageRuntime | None:
    refs = request.runtime_refs
    if provider is None:
        if refs.item_session_refs:
            _block(
                blockers,
                "plan2-drink-runtime-item-unresolved",
                repr(refs.item_session_refs),
            )
        if refs.passive_session_refs:
            _block(
                blockers,
                "plan2-drink-runtime-passive-unresolved",
                repr(refs.passive_session_refs),
            )
        return None if blockers else Plan2NativeStageRuntime()
    stripped_request = replace(
        request,
        outer_state=replace(request.outer_state, drink_session_refs=()),
    )
    try:
        runtime = provider(stripped_request)
    except InitialRegularPlan2MasterRuntimeBlocked as error:
        blockers.extend(
            value for value in error.blockers if value not in blockers
        )
        return None
    except Exception as error:
        _block(
            blockers,
            "plan2-drink-base-runtime-provider-failed",
            f"{type(error).__name__}:{error}",
        )
        return None
    if not isinstance(runtime, Plan2NativeStageRuntime):
        _block(
            blockers,
            "plan2-drink-base-runtime-result-invalid",
            type(runtime).__name__,
        )
        return None
    if runtime.resolved_drink_session_refs or runtime.drink_runtime.inventory:
        _block(
            blockers,
            "plan2-drink-base-runtime-owner-conflict",
        )
        return None
    return runtime


def provision_initial_regular_plan2_drink_runtime(
    request: InitialRegularInnerStageRequest,
    *,
    catalog_loader: Plan2DrinkCatalogLoader = load_drink_catalog,
    drink_ref_resolver: Plan2DrinkRefResolver = _identity_ref,
    base_runtime_provider: Plan2BaseRuntimeProvider | None = None,
) -> InitialRegularPlan2DrinkRuntimeProvision:
    """Resolve outer refs and compose their inventory with an optional base."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    if not callable(catalog_loader) or not callable(drink_ref_resolver):
        raise TypeError("catalog_loader and drink_ref_resolver must be callable")
    if base_runtime_provider is not None and not callable(base_runtime_provider):
        raise TypeError("base_runtime_provider must be callable or None")

    blockers: list[Plan2NativeBlocker] = []
    base = _base_runtime(request, base_runtime_provider, blockers)
    refs = tuple(request.runtime_refs.drink_session_refs)
    if blockers or base is None:
        return InitialRegularPlan2DrinkRuntimeProvision(blockers=tuple(blockers))
    if not refs:
        return InitialRegularPlan2DrinkRuntimeProvision(runtime=base)

    try:
        catalog = catalog_loader()
    except Exception as error:
        return InitialRegularPlan2DrinkRuntimeProvision(
            blockers=(
                Plan2NativeBlocker(
                    "plan2-drink-catalog-unavailable",
                    f"{type(error).__name__}:{error}",
                ),
            )
        )
    if not isinstance(catalog, DrinkCatalog):
        return InitialRegularPlan2DrinkRuntimeProvision(
            blockers=(
                Plan2NativeBlocker(
                    "plan2-drink-catalog-result-invalid",
                    type(catalog).__name__,
                ),
            )
        )

    instances: list[Plan2NativeDrinkInstance] = []
    for slot_index, session_ref in enumerate(refs):
        try:
            drink_id = drink_ref_resolver(session_ref)
        except Exception as error:
            _block(
                blockers,
                "plan2-drink-ref-resolver-failed",
                f"{session_ref}:{type(error).__name__}:{error}",
            )
            continue
        if not isinstance(drink_id, str) or not drink_id:
            _block(
                blockers,
                "plan2-drink-ref-resolver-result-invalid",
                session_ref,
            )
            continue
        try:
            drink = catalog.get_drink(drink_id)
        except Exception as error:
            _block(
                blockers,
                "plan2-drink-master-unavailable",
                f"{session_ref}:{drink_id}:{type(error).__name__}",
            )
            continue
        compilation = compile_plan2_native_drink_instance(
            drink,
            instance_id=(
                session_ref
                if refs.count(session_ref) == 1
                else f"drink-slot:{slot_index}:{session_ref}"
            ),
            session_ref=session_ref,
        )
        if not compilation.supported:
            blockers.extend(
                value for value in compilation.blockers if value not in blockers
            )
            continue
        assert compilation.instance is not None
        instances.append(compilation.instance)

    if blockers or len(instances) != len(refs):
        return InitialRegularPlan2DrinkRuntimeProvision(blockers=tuple(blockers))
    drink_runtime = Plan2NativeDrinkRuntime(tuple(instances))
    adapter_ids = tuple(
        dict.fromkeys(
            (*base.runtime_adapter_ids, PLAN2_MASTER_DRINK_RUNTIME_ADAPTER_ID)
        )
    )
    return InitialRegularPlan2DrinkRuntimeProvision(
        runtime=replace(
            base,
            drink_runtime=drink_runtime,
            resolved_drink_session_refs=refs,
            runtime_adapter_ids=adapter_ids,
        )
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2DrinkRuntimeProvider:
    catalog_loader: Plan2DrinkCatalogLoader = load_drink_catalog
    drink_ref_resolver: Plan2DrinkRefResolver = _identity_ref
    base_runtime_provider: Plan2BaseRuntimeProvider | None = None

    def __post_init__(self) -> None:
        if not callable(self.catalog_loader):
            raise TypeError("catalog_loader must be callable")
        if not callable(self.drink_ref_resolver):
            raise TypeError("drink_ref_resolver must be callable")
        if self.base_runtime_provider is not None and not callable(
            self.base_runtime_provider
        ):
            raise TypeError("base_runtime_provider must be callable or None")

    def provision(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularPlan2DrinkRuntimeProvision:
        return provision_initial_regular_plan2_drink_runtime(
            request,
            catalog_loader=self.catalog_loader,
            drink_ref_resolver=self.drink_ref_resolver,
            base_runtime_provider=self.base_runtime_provider,
        )

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> Plan2NativeStageRuntime:
        return self.provision(request).require_runtime()


def remaining_plan2_drink_session_refs(
    state: Plan2NativeHorizonState,
) -> tuple[str, ...]:
    """Exact outer refs remaining after the horizon's consumptions."""

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be Plan2NativeHorizonState")
    return state.drink_runtime.session_refs


def build_initial_regular_plan2_drink_runtime_provider(
    *,
    catalog_loader: Plan2DrinkCatalogLoader = load_drink_catalog,
    drink_ref_resolver: Plan2DrinkRefResolver = _identity_ref,
    base_runtime_provider: Plan2BaseRuntimeProvider | None = None,
) -> InitialRegularPlan2DrinkRuntimeProvider:
    return InitialRegularPlan2DrinkRuntimeProvider(
        catalog_loader=catalog_loader,
        drink_ref_resolver=drink_ref_resolver,
        base_runtime_provider=base_runtime_provider,
    )


__all__ = [
    "EFFECT_LESSON_DEPEND_REVIEW",
    "PLAN2_DRINK_PLAN_TYPES",
    "PLAN2_MASTER_DRINK_RUNTIME_ADAPTER_ID",
    "InitialRegularPlan2DrinkRuntimeBlocked",
    "InitialRegularPlan2DrinkRuntimeProvision",
    "InitialRegularPlan2DrinkRuntimeProvider",
    "Plan2BaseRuntimeProvider",
    "Plan2DrinkCatalogLoader",
    "Plan2DrinkRefResolver",
    "Plan2NativeDrinkCompilation",
    "build_initial_regular_plan2_drink_runtime_provider",
    "compile_plan2_native_drink_instance",
    "provision_initial_regular_plan2_drink_runtime",
    "remaining_plan2_drink_session_refs",
]
