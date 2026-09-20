"""Android v3.2.3 GUID-native ``ExamAddGrowEffect`` core.

NIA and the Plan 3 search boundary share the same ordered GUID rematch and
grow-row mutation loop.  NIA additionally supplies the native ``DeckAll``
pools that the ordinary five-zone Plan 3 state does not own: PlayingCard and
future/past auxiliary decks.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterable, Mapping

import yaml

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeGrowEffect,
    Plan3NativeState,
    Plan3NativeStateError,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamAddGrowEffect"
GROW_TYPE_LESSON_ADD = "ProduceCardGrowEffectType_LessonAdd"
GROW_TYPE_LESSON_COUNT_ADD = "ProduceCardGrowEffectType_LessonCountAdd"
GROW_TYPE_BLOCK_ADD = "ProduceCardGrowEffectType_BlockAdd"
GROW_TYPE_COST_REDUCE = "ProduceCardGrowEffectType_CostReduce"
GROW_TYPE_COST_ADD = "ProduceCardGrowEffectType_CostAdd"
GROW_TYPE_COST_PENETRATE_ADD = (
    "ProduceCardGrowEffectType_CostPenetrateAdd"
)
SEARCH_ID_DECK_ALL = "p_card_search-deck_all"
SEARCH_ID_HAND = "p_card_search-hand"
SEARCH_ID_ACTIVE_SKILL_DECK_ALL = "p_card_search-active_skill-deck_all"
POSITION_DECK_ALL = "ProduceCardPositionType_DeckAll"
POSITION_HAND = "ProduceCardPositionType_Hand"
POSITION_HOLD = "ProduceCardPositionType_Hold"
CATEGORY_ACTIVE_SKILL = "ProduceCardCategory_ActiveSkill"
CATEGORY_MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
PICK_RANGE_ALL = "ProducePickRangeType_All"

EFFECT_LESSON = "ProduceExamEffectType_ExamLesson"
EFFECT_LESSON_FULL_POWER_POINT = (
    "ProduceExamEffectType_ExamLessonFullPowerPoint"
)
EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON = (
    "ProduceExamEffectType_ExamMultipleEnthusiasticLesson"
)
EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
COST_STAMINA = "ExamCostType_Unknown"

_SUPPORTED_PLAN3_GROW_TYPES = frozenset(
    {
        GROW_TYPE_LESSON_ADD,
        GROW_TYPE_LESSON_COUNT_ADD,
        GROW_TYPE_BLOCK_ADD,
        GROW_TYPE_COST_REDUCE,
        GROW_TYPE_COST_ADD,
        GROW_TYPE_COST_PENETRATE_ADD,
    }
)
_SUPPORTED_EFFECT_GROUP_SEARCH_IDS = frozenset(
    {
        "effect_group-visible-exam_concentration-000",
        "effect_group-visible-exam_full_power-000",
    }
)

DEFAULT_MASTER_DIR = Path(__file__).resolve().parents[2] / "_research/gakumasu-diff"

_UNKNOWN_EFFECT = "ProduceExamEffectType_Unknown"
_UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"
_UNKNOWN_PICK_RANGE = "ProducePickRangeType_Unknown"
_UNKNOWN_PICK_COUNT = "ProducePickCountType_Unknown"
_UNKNOWN_PLAN = "ProducePlanType_Unknown"
_UNKNOWN_CARD_STATUS = "ProduceCardSearchStatusType_Unknown"
_UNKNOWN_CARD_ORDER = "ProduceCardOrderType_Unknown"
_UNKNOWN_MIN_MAX = "ConditionMinMaxType_Unknown"
_UNKNOWN_COST = "ExamCostType_Unknown"

_NEUTRAL_GROW_FIELDS = {
    "costType": "ExamCostType_Unknown",
    "playProduceExamTriggerId": "",
    "playEffectProduceExamTriggerId": "",
    "targetPlayEffectProduceExamTriggerIds": [],
    "playProduceExamEffectId": "",
    "targetPlayProduceExamEffectIds": [],
    "produceCardStatusEnchantId": "",
    "playMovePositionType": _UNKNOWN_MOVE,
    "effectGroupIds": [],
}

_DECK_ALL_SEARCH_FIELDS = {
    "cardRarities": [],
    "produceCardIds": [],
    "upgradeCounts": [],
    "planType": "ProducePlanType_Unknown",
    "cardCategories": [],
    "cardStatusType": "ProduceCardSearchStatusType_Unknown",
    "orderType": "ProduceCardOrderType_Unknown",
    "cardPositionType": POSITION_DECK_ALL,
    "cardSearchTag": "",
    "produceCardRandomPoolId": "",
    "limitCount": 0,
    "staminaMinMaxType": "ConditionMinMaxType_Unknown",
    "staminaMin": 0,
    "staminaMax": 0,
    "examEffectType": _UNKNOWN_EFFECT,
    "effectGroupIds": [],
    "isSelf": False,
    "produceCardPoolId": "",
    "costType": "ExamCostType_Unknown",
    "isCustomized": False,
}


class NiaAddGrowContractError(ValueError):
    """A Master/runtime input is outside the proven NIA v3.2.3 profile."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{label} must be non-empty text")
    return value


def _int32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not -(2**31) <= value <= (2**31) - 1:
        raise NiaAddGrowContractError(f"{label} is outside Int32: {value}")
    return value


@dataclass(frozen=True, slots=True)
class NiaAddGrowMasterEffect:
    """Execution-relevant projection of one ProduceExamEffect Master row."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    target_card_id: str
    target_upgrade: int
    target_effect_type: str
    search_id: str
    destination: str
    pick_range_type: str
    pick_reference_search_id: str
    pick_count_type: str
    pick_count_min: int
    pick_count_max: int
    second_search_id: str
    second_pick_range_type: str
    second_pick_reference_search_id: str
    second_pick_count_type: str
    second_pick_count_min: int
    second_pick_count_max: int
    chain_effect_id: str
    chain_effect_ids: tuple[str, ...]
    exam_status_enchant_id: str
    card_status_enchant_id: str
    grow_effect_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _text(self.effect_type, "effect_type")
        for label in (
            "value1",
            "value2",
            "effect_count",
            "effect_turn",
            "target_upgrade",
            "pick_count_min",
            "pick_count_max",
            "second_pick_count_min",
            "second_pick_count_max",
        ):
            _int32(getattr(self, label), label)
        object.__setattr__(self, "chain_effect_ids", tuple(self.chain_effect_ids))
        grow_ids = tuple(self.grow_effect_ids)
        if any(not isinstance(value, str) or not value for value in grow_ids):
            raise TypeError("grow_effect_ids must contain non-empty text")
        object.__setattr__(self, "grow_effect_ids", grow_ids)

    @classmethod
    def from_raw(cls, raw: Mapping[str, object]) -> "NiaAddGrowMasterEffect":
        if not isinstance(raw, Mapping):
            raise TypeError("raw must be a mapping")
        return cls(
            effect_id=raw.get("id"),
            effect_type=raw.get("effectType"),
            value1=raw.get("effectValue1"),
            value2=raw.get("effectValue2"),
            effect_count=raw.get("effectCount"),
            effect_turn=raw.get("effectTurn"),
            target_card_id=raw.get("targetProduceCardId"),
            target_upgrade=raw.get("targetUpgradeCount"),
            target_effect_type=raw.get("targetExamEffectType"),
            search_id=raw.get("produceCardSearchId"),
            destination=raw.get("movePositionType"),
            pick_range_type=raw.get("pickRangeType"),
            pick_reference_search_id=raw.get(
                "pickCountReferenceProduceCardSearchId"
            ),
            pick_count_type=raw.get("pickCountType"),
            pick_count_min=raw.get("pickCountMin"),
            pick_count_max=raw.get("pickCountMax"),
            second_search_id=raw.get("produceCardSearchId2"),
            second_pick_range_type=raw.get("pickRangeType2"),
            second_pick_reference_search_id=raw.get(
                "pickCountReferenceProduceCardSearchId2"
            ),
            second_pick_count_type=raw.get("pickCountType2"),
            second_pick_count_min=raw.get("pickCountMin2"),
            second_pick_count_max=raw.get("pickCountMax2"),
            chain_effect_id=raw.get("chainProduceExamEffectId"),
            chain_effect_ids=tuple(raw.get("chainProduceExamEffectIds", ())),
            exam_status_enchant_id=raw.get("produceExamStatusEnchantId"),
            card_status_enchant_id=raw.get("produceCardStatusEnchantId"),
            grow_effect_ids=tuple(raw.get("produceCardGrowEffectIds", ())),
        )

    def assert_exact_deck_all_shape(self) -> None:
        self.assert_shared_lesson_add_shape()
        if self.search_id != SEARCH_ID_DECK_ALL:
            raise NiaAddGrowContractError(
                f"unsupported ExamAddGrowEffect shape: {self.effect_id}"
            )

    def assert_shared_add_grow_shape(self) -> None:
        """Validate the common neutral AddGrow envelope proven in v3.2.3."""

        actual = (
            self.effect_type,
            self.value1,
            self.value2,
            self.effect_count,
            self.effect_turn,
            self.target_card_id,
            self.target_upgrade,
            self.target_effect_type,
            self.destination,
            self.pick_range_type,
            self.pick_reference_search_id,
            self.pick_count_type,
            self.pick_count_min,
            self.pick_count_max,
            self.second_search_id,
            self.second_pick_range_type,
            self.second_pick_reference_search_id,
            self.second_pick_count_type,
            self.second_pick_count_min,
            self.second_pick_count_max,
            self.chain_effect_id,
            self.chain_effect_ids,
            self.exam_status_enchant_id,
            self.card_status_enchant_id,
        )
        expected = (
            EFFECT_TYPE,
            0,
            0,
            0,
            0,
            "",
            0,
            _UNKNOWN_EFFECT,
            _UNKNOWN_MOVE,
            PICK_RANGE_ALL,
            "",
            _UNKNOWN_PICK_COUNT,
            0,
            0,
            "",
            _UNKNOWN_PICK_RANGE,
            "",
            _UNKNOWN_PICK_COUNT,
            0,
            0,
            "",
            (),
            "",
            "",
        )
        if (
            actual != expected
            or not self.search_id
            or not self.grow_effect_ids
        ):
            raise NiaAddGrowContractError(
                f"unsupported ExamAddGrowEffect shape: {self.effect_id}"
            )

    # Kept for compatibility with the first shared LessonAdd slice.  The
    # envelope is common to every statically proven Plan3 AddGrow row; grow
    # types are admitted independently by ``Plan3AddGrowProgram`` below.
    def assert_shared_lesson_add_shape(self) -> None:
        self.assert_shared_add_grow_shape()


@dataclass(frozen=True, slots=True)
class NiaAddGrowProgram:
    """A Master-resolved AddGrow effect with ordered grow rows."""

    effect: NiaAddGrowMasterEffect
    grow_effects: tuple[Plan3NativeGrowEffect, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.effect, NiaAddGrowMasterEffect):
            raise TypeError("effect must be NiaAddGrowMasterEffect")
        self.effect.assert_exact_deck_all_shape()
        effects = tuple(self.grow_effects)
        if not all(isinstance(value, Plan3NativeGrowEffect) for value in effects):
            raise TypeError("grow_effects must contain Plan3NativeGrowEffect values")
        if tuple(value.id for value in effects) != self.effect.grow_effect_ids:
            raise NiaAddGrowContractError(
                f"grow row order/lineage mismatch: {self.effect.effect_id}"
            )
        for value in effects:
            if value.effect_type != GROW_TYPE_LESSON_ADD:
                raise NiaAddGrowContractError(
                    f"unsupported NIA grow type: {value.id}"
                )
            if isinstance(value.value, bool) or not isinstance(value.value, int):
                raise TypeError("grow effect value must be an integer")
            if value.value <= 0:
                raise NiaAddGrowContractError(
                    f"NIA LessonAdd must be positive: {value.id}"
                )
        object.__setattr__(self, "grow_effects", effects)


@dataclass(frozen=True, slots=True)
class Plan3AddGrowProgram:
    """Plan3/NIA shared AddGrow program with its exact search Master row."""

    effect: NiaAddGrowMasterEffect
    search: ProduceCardSearchRule
    grow_effects: tuple[Plan3NativeGrowEffect, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.effect, NiaAddGrowMasterEffect):
            raise TypeError("effect must be NiaAddGrowMasterEffect")
        if not isinstance(self.search, ProduceCardSearchRule):
            raise TypeError("search must be ProduceCardSearchRule")
        self.effect.assert_shared_add_grow_shape()
        if self.search.id != self.effect.search_id:
            raise NiaAddGrowContractError(
                f"search lineage mismatch: {self.effect.effect_id}"
            )
        _assert_supported_search_rule(self.search)
        effects = tuple(self.grow_effects)
        if not all(isinstance(value, Plan3NativeGrowEffect) for value in effects):
            raise TypeError("grow_effects must contain Plan3NativeGrowEffect values")
        if tuple(value.id for value in effects) != self.effect.grow_effect_ids:
            raise NiaAddGrowContractError(
                f"grow row order/lineage mismatch: {self.effect.effect_id}"
            )
        for value in effects:
            if value.effect_type not in _SUPPORTED_PLAN3_GROW_TYPES:
                raise NiaAddGrowContractError(
                    f"unsupported shared grow type: {value.id}"
                )
            if isinstance(value.value, bool) or not isinstance(value.value, int):
                raise TypeError("grow effect value must be an integer")
            if value.value <= 0:
                raise NiaAddGrowContractError(
                    f"shared grow value must be positive: {value.id}"
                )
        object.__setattr__(self, "grow_effects", effects)


@dataclass(frozen=True, slots=True)
class Plan3AddGrowCardProfile:
    """Static card facts consumed by Android's AddGrow validity checks."""

    category: str
    effect_group_ids: tuple[str, ...]
    cost_type: str
    is_stamina_cost_penetrate: bool
    effect_types: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.category, "category")
        _text(self.cost_type, "cost_type")
        groups = tuple(self.effect_group_ids)
        effects = tuple(self.effect_types)
        if any(not isinstance(value, str) or not value for value in groups):
            raise TypeError("effect_group_ids must contain non-empty text")
        if any(not isinstance(value, str) or not value for value in effects):
            raise TypeError("effect_types must contain non-empty text")
        if not isinstance(self.is_stamina_cost_penetrate, bool):
            raise TypeError("is_stamina_cost_penetrate must be bool")
        object.__setattr__(self, "effect_group_ids", groups)
        object.__setattr__(self, "effect_types", effects)


@dataclass(frozen=True, slots=True)
class NiaDeckAllCardLocation:
    """One card in native GetCardList/CreateCardPositionDataList order."""

    zone: str
    card_index: int
    deck_list_index: int | None
    card: Plan3NativeCard


@dataclass(frozen=True, slots=True)
class NiaDeckAllState:
    """Complete native DeckAll projection around a Plan3 five-zone state.

    Native order is Hand, Deck, Grave, Lost, Hold, non-empty PlayingCard,
    future deck services in list order, then past deck services in list order.
    """

    ordinary: Plan3NativeState
    playing: Plan3NativeCard | None = None
    future_decks: tuple[tuple[Plan3NativeCard, ...], ...] = ()
    past_decks: tuple[tuple[Plan3NativeCard, ...], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.ordinary, Plan3NativeState):
            raise TypeError("ordinary must be Plan3NativeState")
        if self.playing is not None and not isinstance(self.playing, Plan3NativeCard):
            raise TypeError("playing must be Plan3NativeCard or None")

        def normalize(
            value: Iterable[Iterable[Plan3NativeCard]], label: str
        ) -> tuple[tuple[Plan3NativeCard, ...], ...]:
            if isinstance(value, (str, bytes)):
                raise TypeError(f"{label} must be nested card iterables")
            result: list[tuple[Plan3NativeCard, ...]] = []
            for deck in value:
                if isinstance(deck, (str, bytes)):
                    raise TypeError(f"{label} must be nested card iterables")
                row = tuple(deck)
                if not all(isinstance(card, Plan3NativeCard) for card in row):
                    raise TypeError(f"{label} must contain Plan3NativeCard values")
                result.append(row)
            return tuple(result)

        future = normalize(self.future_decks, "future_decks")
        past = normalize(self.past_decks, "past_decks")
        object.__setattr__(self, "future_decks", future)
        object.__setattr__(self, "past_decks", past)
        guids = [location.card.guid for location in self.native_locations]
        if len(guids) != len(set(guids)):
            raise Plan3NativeStateError("duplicate-guid")

    @property
    def native_locations(self) -> tuple[NiaDeckAllCardLocation, ...]:
        rows: list[NiaDeckAllCardLocation] = []
        for zone in ("hand", "deck", "grave", "lost", "hold"):
            rows.extend(
                NiaDeckAllCardLocation(zone, index, None, card)
                for index, card in enumerate(getattr(self.ordinary, zone))
            )
        if self.playing is not None:
            rows.append(NiaDeckAllCardLocation("playing", 0, None, self.playing))
        for zone, deck_lists in (
            ("future_deck", self.future_decks),
            ("past_deck", self.past_decks),
        ):
            for deck_index, deck in enumerate(deck_lists):
                rows.extend(
                    NiaDeckAllCardLocation(zone, card_index, deck_index, card)
                    for card_index, card in enumerate(deck)
                )
        return tuple(rows)


@dataclass(frozen=True, slots=True)
class NiaAddGrowMutation:
    effect_id: str
    sequence: int
    zone: str
    card_index: int
    deck_list_index: int | None
    guid: str
    grow_effect_ids: tuple[str, ...]
    lesson_add_before: int
    lesson_add_after: int
    lesson_count_add_before: int
    lesson_count_add_after: int
    block_add_before: int
    block_add_after: int
    cost_add_before: int
    cost_add_after: int
    cost_reduce_before: int
    cost_reduce_after: int
    cost_penetrate_add_before: int
    cost_penetrate_add_after: int


@dataclass(frozen=True, slots=True)
class NiaAddGrowExecution:
    state: NiaDeckAllState
    mutations: tuple[NiaAddGrowMutation, ...]


@lru_cache(maxsize=8)
def _yaml_index(path: Path) -> Mapping[str, Mapping[str, object]]:
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise NiaAddGrowContractError(f"Master is not a row list: {path}")
    index: dict[str, Mapping[str, object]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
            raise NiaAddGrowContractError(f"invalid Master row: {path}")
        index[raw["id"]] = raw
    return index


def _assert_deck_all_search_master(master_dir: Path) -> None:
    rows = _yaml_index(master_dir / "ProduceCardSearch.yaml")
    raw = rows.get(SEARCH_ID_DECK_ALL)
    if raw is None:
        raise NiaAddGrowContractError("DeckAll card-search Master row is missing")
    mismatches = {
        key: (raw.get(key), expected)
        for key, expected in _DECK_ALL_SEARCH_FIELDS.items()
        if raw.get(key) != expected
    }
    if mismatches:
        raise NiaAddGrowContractError(
            f"DeckAll search Master is no longer neutral: {mismatches!r}"
        )


def _assert_supported_search_rule(rule: ProduceCardSearchRule) -> None:
    common = (
        not rule.card_rarities
        and not rule.upgrade_counts
        and rule.plan_type == _UNKNOWN_PLAN
        and rule.card_status_type == _UNKNOWN_CARD_STATUS
        and rule.order_type == _UNKNOWN_CARD_ORDER
        and not rule.card_search_tag
        and not rule.produce_card_random_pool_id
        and rule.limit_count == 0
        and rule.stamina_min_max_type == _UNKNOWN_MIN_MAX
        and rule.stamina_min == 0
        and rule.stamina_max == 0
        and rule.exam_effect_type == _UNKNOWN_EFFECT
        and not rule.is_self
        and not rule.produce_card_pool_id
        and rule.cost_type == _UNKNOWN_COST
        and not rule.is_customized
    )
    fields = (
        rule.card_position_type,
        rule.card_categories,
        rule.produce_card_ids,
        rule.effect_group_ids,
    )
    profiles = {
        (POSITION_DECK_ALL, (), (), ()),
        (POSITION_DECK_ALL, (CATEGORY_ACTIVE_SKILL,), (), ()),
        (POSITION_DECK_ALL, (CATEGORY_MENTAL_SKILL,), (), ()),
        *(
            (POSITION_DECK_ALL, (), (), (effect_group_id,))
            for effect_group_id in _SUPPORTED_EFFECT_GROUP_SEARCH_IDS
        ),
        (
            POSITION_DECK_ALL,
            (CATEGORY_ACTIVE_SKILL,),
            (),
            ("effect_group-visible-exam_full_power-000",),
        ),
        (POSITION_HAND, (), (), ()),
        (POSITION_HOLD, (), (), ()),
    }
    target_card_profile = (
        rule.card_position_type == POSITION_DECK_ALL
        and not rule.card_categories
        and len(rule.produce_card_ids) == 1
        and not rule.effect_group_ids
    )
    if not common or (fields not in profiles and not target_card_profile):
        raise NiaAddGrowContractError(
            f"AddGrow card search is no longer exact: {rule.id}"
        )


def _matches_add_grow_search(
    rule: ProduceCardSearchRule,
    card: Plan3NativeCard,
    profile: Plan3AddGrowCardProfile,
) -> bool:
    if rule.produce_card_ids and card.card_id not in rule.produce_card_ids:
        return False
    if rule.card_categories and profile.category not in rule.card_categories:
        return False
    if rule.effect_group_ids and not set(rule.effect_group_ids).issubset(
        profile.effect_group_ids
    ):
        return False
    return True


def _valid_card_grow_effects(
    profile: Plan3AddGrowCardProfile,
    effects: tuple[Plan3NativeGrowEffect, ...],
) -> tuple[Plan3NativeGrowEffect, ...]:
    """Mirror the proven card/cost branches of IsValidCardGrowEffect."""

    effect_types = frozenset(profile.effect_types)
    has_lesson = bool(
        effect_types
        & {
            EFFECT_LESSON,
            EFFECT_LESSON_FULL_POWER_POINT,
            EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON,
        }
    )
    has_block = EFFECT_BLOCK in effect_types
    valid: list[Plan3NativeGrowEffect] = []
    for effect in effects:
        if effect.effect_type in {
            GROW_TYPE_LESSON_ADD,
            GROW_TYPE_LESSON_COUNT_ADD,
        }:
            applies = has_lesson
        elif effect.effect_type == GROW_TYPE_BLOCK_ADD:
            applies = has_block
        elif effect.effect_type in {GROW_TYPE_COST_ADD, GROW_TYPE_COST_REDUCE}:
            applies = (
                profile.cost_type == COST_STAMINA
                and not profile.is_stamina_cost_penetrate
            )
        elif effect.effect_type == GROW_TYPE_COST_PENETRATE_ADD:
            applies = (
                profile.cost_type == COST_STAMINA
                and profile.is_stamina_cost_penetrate
            )
        else:  # Plan3AddGrowProgram already rejects this branch.
            raise NiaAddGrowContractError(
                f"unsupported shared grow type: {effect.id}"
            )
        if applies:
            valid.append(effect)
    return tuple(valid)


def _load_grow_effect(
    grow_id: str, *, master_dir: Path
) -> Plan3NativeGrowEffect:
    raw = _yaml_index(master_dir / "ProduceCardGrowEffect.yaml").get(grow_id)
    if raw is None:
        raise NiaAddGrowContractError(f"grow Master row is missing: {grow_id}")
    mismatches = {
        key: (raw.get(key), expected)
        for key, expected in _NEUTRAL_GROW_FIELDS.items()
        if raw.get(key) != expected
    }
    if mismatches:
        raise NiaAddGrowContractError(
            f"grow Master row is conditional: {grow_id}: {mismatches!r}"
        )
    return Plan3NativeGrowEffect(
        id=_text(raw.get("id"), "grow effect id"),
        effect_type=_text(raw.get("effectType"), "grow effect type"),
        value=_int32(raw.get("value"), "grow effect value"),
    )


def _load_add_grow_master_effect(
    effect_id: str, *, database: Path
) -> NiaAddGrowMasterEffect:
    with sqlite3.connect(Path(database)) as connection:
        row = connection.execute(
            "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
    if row is None:
        raise NiaAddGrowContractError(f"effect Master row is missing: {effect_id}")
    raw = json.loads(row[0])
    effect = NiaAddGrowMasterEffect.from_raw(raw)
    if effect.effect_id != effect_id:
        raise NiaAddGrowContractError(f"effect ID mismatch: {effect_id}")
    return effect


def load_nia_add_grow_program(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaAddGrowProgram:
    """Resolve and fail-closed validate one AddGrow effect from static Master."""

    effect_id = _text(effect_id, "effect_id")
    from .version_bound_master import resolve_imported_master_directory
    master_dir = resolve_imported_master_directory(Path(database), Path(master_dir), default_master_dir=DEFAULT_MASTER_DIR)
    _assert_deck_all_search_master(master_dir)
    effect = _load_add_grow_master_effect(effect_id, database=Path(database))
    effect.assert_exact_deck_all_shape()
    grows = tuple(
        _load_grow_effect(grow_id, master_dir=master_dir)
        for grow_id in effect.grow_effect_ids
    )
    return NiaAddGrowProgram(effect, grows)


@lru_cache(maxsize=256)
def load_plan3_add_grow_program(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3AddGrowProgram:
    """Resolve one effect in the proven Plan3 AddGrow semantic closure."""

    effect_id = _text(effect_id, "effect_id")
    from .version_bound_master import resolve_imported_master_directory
    master_dir = resolve_imported_master_directory(Path(database), Path(master_dir), default_master_dir=DEFAULT_MASTER_DIR)
    effect = _load_add_grow_master_effect(effect_id, database=Path(database))
    effect.assert_shared_add_grow_shape()
    try:
        search = load_produce_card_search(effect.search_id, Path(database))
    except KeyError as error:
        raise NiaAddGrowContractError(
            f"card-search Master row is missing: {effect.search_id}"
        ) from error
    grows = tuple(
        _load_grow_effect(grow_id, master_dir=master_dir)
        for grow_id in effect.grow_effect_ids
    )
    return Plan3AddGrowProgram(effect, search, grows)


def _execute_add_grow_locations(
    state: NiaDeckAllState,
    *,
    effect_id: str,
    grow_effects: tuple[Plan3NativeGrowEffect, ...],
    locations: tuple[NiaDeckAllCardLocation, ...],
    grow_effects_by_card: Callable[
        [Plan3NativeCard], tuple[Plan3NativeGrowEffect, ...]
    ] | None = None,
) -> NiaAddGrowExecution:
    """Apply the native GUID rematch/mutation loop for both Plan3 and NIA."""

    replacements: dict[str, Plan3NativeCard] = {}
    mutations: list[NiaAddGrowMutation] = []
    sequence = 0
    for location in locations:
        card = location.card
        card_grow_effects = (
            grow_effects
            if grow_effects_by_card is None
            else grow_effects_by_card(card)
        )
        if not card_grow_effects:
            continue
        grow_ids = tuple(effect.id for effect in card_grow_effects)
        updated = card.apply_runtime_grow_effects(card_grow_effects)
        replacements[card.guid] = updated
        mutations.append(
            NiaAddGrowMutation(
                effect_id=effect_id,
                sequence=sequence,
                zone=location.zone,
                card_index=location.card_index,
                deck_list_index=location.deck_list_index,
                guid=card.guid,
                grow_effect_ids=grow_ids,
                lesson_add_before=card.runtime_lesson_add,
                lesson_add_after=updated.runtime_lesson_add,
                lesson_count_add_before=card.runtime_lesson_count_add,
                lesson_count_add_after=updated.runtime_lesson_count_add,
                block_add_before=card.runtime_block_add,
                block_add_after=updated.runtime_block_add,
                cost_add_before=card.runtime_cost_add,
                cost_add_after=updated.runtime_cost_add,
                cost_reduce_before=card.runtime_cost_reduce,
                cost_reduce_after=updated.runtime_cost_reduce,
                cost_penetrate_add_before=(
                    card.runtime_cost_penetrate_add
                ),
                cost_penetrate_add_after=(
                    updated.runtime_cost_penetrate_add
                ),
            )
        )
        sequence += 1

    def replace_cards(cards: Iterable[Plan3NativeCard]) -> tuple[Plan3NativeCard, ...]:
        return tuple(replacements.get(card.guid, card) for card in cards)

    ordinary = replace(
        state.ordinary,
        hand=replace_cards(state.ordinary.hand),
        deck=replace_cards(state.ordinary.deck),
        grave=replace_cards(state.ordinary.grave),
        lost=replace_cards(state.ordinary.lost),
        hold=replace_cards(state.ordinary.hold),
    )
    after = NiaDeckAllState(
        ordinary=ordinary,
        playing=(
            None
            if state.playing is None
            else replacements.get(state.playing.guid, state.playing)
        ),
        future_decks=tuple(replace_cards(deck) for deck in state.future_decks),
        past_decks=tuple(replace_cards(deck) for deck in state.past_decks),
    )
    return NiaAddGrowExecution(after, tuple(mutations))


def execute_nia_add_grow_program(
    state: NiaDeckAllState,
    program: NiaAddGrowProgram,
) -> NiaAddGrowExecution:
    """Apply one effect in native search-card then grow-row order."""

    if not isinstance(state, NiaDeckAllState):
        raise TypeError("state must be NiaDeckAllState")
    if not isinstance(program, NiaAddGrowProgram):
        raise TypeError("program must be NiaAddGrowProgram")

    return _execute_add_grow_locations(
        state,
        effect_id=program.effect.effect_id,
        grow_effects=program.grow_effects,
        locations=state.native_locations,
    )


def execute_plan3_add_grow_program(
    state: NiaDeckAllState,
    program: Plan3AddGrowProgram,
    *,
    card_profile_by_card: Callable[
        [Plan3NativeCard], Plan3AddGrowCardProfile
    ] | None = None,
) -> NiaAddGrowExecution:
    """Search, GUID-rematch, and mutate one shared AddGrow program."""

    if not isinstance(state, NiaDeckAllState):
        raise TypeError("state must be NiaDeckAllState")
    if not isinstance(program, Plan3AddGrowProgram):
        raise TypeError("program must be Plan3AddGrowProgram")
    if card_profile_by_card is None or not callable(card_profile_by_card):
        raise NiaAddGrowContractError(
            f"card profile resolver is required: {program.search.id}"
        )

    if program.search.card_position_type == POSITION_HAND:
        locations = tuple(
            location
            for location in state.native_locations
            if location.zone == "hand"
        )
    elif program.search.card_position_type == POSITION_HOLD:
        locations = tuple(
            location
            for location in state.native_locations
            if location.zone == "hold"
        )
    else:
        locations = state.native_locations
    profiles: dict[str, Plan3AddGrowCardProfile] = {}
    matched: list[NiaDeckAllCardLocation] = []
    for location in locations:
        profile = card_profile_by_card(location.card)
        if not isinstance(profile, Plan3AddGrowCardProfile):
            raise TypeError(
                "card_profile_by_card must return Plan3AddGrowCardProfile"
            )
        profiles[location.card.guid] = profile
        if _matches_add_grow_search(program.search, location.card, profile):
            matched.append(location)
    return _execute_add_grow_locations(
        state,
        effect_id=program.effect.effect_id,
        grow_effects=program.grow_effects,
        locations=tuple(matched),
        grow_effects_by_card=(
            lambda card: _valid_card_grow_effects(
                profiles[card.guid], program.grow_effects
            )
        ),
    )


def execute_nia_add_grow_programs(
    state: NiaDeckAllState,
    programs: Iterable[NiaAddGrowProgram],
) -> NiaAddGrowExecution:
    """Apply caller-ordered gimmick effects, preserving every native trace."""

    if isinstance(programs, (str, bytes)):
        raise TypeError("programs must be an iterable of NiaAddGrowProgram")
    current = state
    all_mutations: list[NiaAddGrowMutation] = []
    for program in tuple(programs):
        result = execute_nia_add_grow_program(current, program)
        current = result.state
        all_mutations.extend(result.mutations)
    return NiaAddGrowExecution(current, tuple(all_mutations))
