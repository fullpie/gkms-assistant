"""Exact overlay for the 87 previously blocked Plan2 effect-trigger links.

The overlay owns only direct card-effect predicates.  It preserves the base
catalog's card/version/slot order, evaluates one immutable candidate-build
snapshot, and never executes the linked effect or a whole card.

Android v3.2.3 is authoritative for the field predicate and transaction
boundary.  The examined PC metadata confirms the same method and data shape,
but is not treated as instruction-body parity.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Final, Mapping

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_native_catalog_effect_triggers import (
    EffectTriggerOrigin,
    PHASE_EXAM_CARD_PLAY,
    PHASE_NONE,
    Plan2NativeEffectTriggerContractError,
    Plan2NativeEffectTriggerHandoff,
    load_plan2_native_catalog_effect_triggers,
)


PLAN2_NATIVE_CATALOG_EFFECT_TRIGGERS_REMAINING_SCHEMA_VERSION: Final = 1
REMAINING_EFFECT_TRIGGER_ADAPTER_ID: Final = (
    "plan2.native.catalog.effect_triggers.remaining"
)

EXPECTED_OCCURRENCE_COUNT: Final = 87
EXPECTED_UNIQUE_EFFECT_COUNT: Final = 45
EXPECTED_UNIQUE_VERSION_COUNT: Final = 79
EXPECTED_CARD_ID_COUNT: Final = 20
EXPECTED_TRIGGER_ID_COUNT: Final = 9
EXPECTED_EXECUTABLE_OCCURRENCE_COUNT: Final = 87

FIELD_AGGRESSIVE: Final = "ProduceExamFieldStatusType_CardPlayAggressiveUp"
FIELD_REVIEW: Final = "ProduceExamFieldStatusType_ReviewUp"
FIELD_SEARCH_COUNT: Final = "ProduceExamFieldStatusType_CardSearchCountUp"
FIELD_BLOCK: Final = "ProduceExamFieldStatusType_BlockUp"
SEARCH_TROUBLE_NOT_LOST: Final = "p_card_search-trouble-not_lost"
CATEGORY_TROUBLE: Final = "ProduceCardCategory_Trouble"
POSITION_NOT_LOST: Final = "ProduceCardPositionType_NotLost"

TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP3: Final = (
    "e_trigger-exam_card_play-card_play_aggressive_up-3"
)
TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP6: Final = (
    "e_trigger-exam_card_play-card_play_aggressive_up-6"
)
TRIGGER_NONE_REVIEW_UP10: Final = "e_trigger-none-review_up-10"
TRIGGER_NONE_REVIEW_UP15: Final = "e_trigger-none-review_up-15"
TRIGGER_EXAM_CARD_PLAY_REVIEW_UP1: Final = (
    "e_trigger-exam_card_play-review_up-1"
)
TRIGGER_EXAM_CARD_PLAY_REVIEW_UP3: Final = (
    "e_trigger-exam_card_play-review_up-3"
)
TRIGGER_EXAM_CARD_PLAY_REVIEW_UP10: Final = (
    "e_trigger-exam_card_play-review_up-10"
)
TRIGGER_NONE_CARD_SEARCH_COUNT_UP2: Final = (
    "e_trigger-none-card_search_count_up-2-"
    "p_card_search-trouble-not_lost"
)
TRIGGER_NONE_BLOCK_UP30: Final = "e_trigger-none-block_up-30"


class RemainingEffectTriggerFamily(str, Enum):
    EXAM_CARD_PLAY_AGGRESSIVE = "exam-card-play-aggressive"
    NONE_REVIEW = "none-review"
    EXAM_CARD_PLAY_REVIEW = "exam-card-play-review"
    NONE_CARD_SEARCH_COUNT = "none-card-search-count"
    NONE_BLOCK = "none-block"


class RemainingEffectTriggerBoundary(str, Enum):
    PRE_PAYMENT_DIRECT_EFFECT_BUILD = "pre-payment-direct-effect-build"
    EXAM_CARD_PLAY_PRE_PAYMENT_DIRECT_EFFECT_BUILD = (
        "exam-card-play-pre-payment-direct-effect-build"
    )
    POST_PAYMENT_DIRECT_EFFECT_BUILD = "post-payment-direct-effect-build"
    POST_DIRECT_EFFECT_EXECUTION = "post-direct-effect-execution"


@dataclass(frozen=True, slots=True)
class _Definition:
    family: RemainingEffectTriggerFamily
    phase: str
    field: str
    threshold: int
    boundary: RemainingEffectTriggerBoundary
    search_id: str = ""


_DEFINITIONS: Final = {
    TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP3: _Definition(
        RemainingEffectTriggerFamily.EXAM_CARD_PLAY_AGGRESSIVE,
        PHASE_EXAM_CARD_PLAY,
        FIELD_AGGRESSIVE,
        3,
        RemainingEffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
    TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP6: _Definition(
        RemainingEffectTriggerFamily.EXAM_CARD_PLAY_AGGRESSIVE,
        PHASE_EXAM_CARD_PLAY,
        FIELD_AGGRESSIVE,
        6,
        RemainingEffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
    TRIGGER_NONE_REVIEW_UP10: _Definition(
        RemainingEffectTriggerFamily.NONE_REVIEW,
        PHASE_NONE,
        FIELD_REVIEW,
        10,
        RemainingEffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
    TRIGGER_NONE_REVIEW_UP15: _Definition(
        RemainingEffectTriggerFamily.NONE_REVIEW,
        PHASE_NONE,
        FIELD_REVIEW,
        15,
        RemainingEffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
    TRIGGER_EXAM_CARD_PLAY_REVIEW_UP1: _Definition(
        RemainingEffectTriggerFamily.EXAM_CARD_PLAY_REVIEW,
        PHASE_EXAM_CARD_PLAY,
        FIELD_REVIEW,
        1,
        RemainingEffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
    TRIGGER_EXAM_CARD_PLAY_REVIEW_UP3: _Definition(
        RemainingEffectTriggerFamily.EXAM_CARD_PLAY_REVIEW,
        PHASE_EXAM_CARD_PLAY,
        FIELD_REVIEW,
        3,
        RemainingEffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
    TRIGGER_EXAM_CARD_PLAY_REVIEW_UP10: _Definition(
        RemainingEffectTriggerFamily.EXAM_CARD_PLAY_REVIEW,
        PHASE_EXAM_CARD_PLAY,
        FIELD_REVIEW,
        10,
        RemainingEffectTriggerBoundary.EXAM_CARD_PLAY_PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
    TRIGGER_NONE_CARD_SEARCH_COUNT_UP2: _Definition(
        RemainingEffectTriggerFamily.NONE_CARD_SEARCH_COUNT,
        PHASE_NONE,
        FIELD_SEARCH_COUNT,
        2,
        RemainingEffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
        SEARCH_TROUBLE_NOT_LOST,
    ),
    TRIGGER_NONE_BLOCK_UP30: _Definition(
        RemainingEffectTriggerFamily.NONE_BLOCK,
        PHASE_NONE,
        FIELD_BLOCK,
        30,
        RemainingEffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
    ),
}

TARGET_TRIGGER_IDS: Final = tuple(_DEFINITIONS)
EXPECTED_TRIGGER_OCCURRENCE_COUNTS: Final = (
    (TRIGGER_NONE_REVIEW_UP10, 20),
    (TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP3, 15),
    (TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP6, 13),
    (TRIGGER_NONE_CARD_SEARCH_COUNT_UP2, 12),
    (TRIGGER_EXAM_CARD_PLAY_REVIEW_UP3, 10),
    (TRIGGER_NONE_REVIEW_UP15, 8),
    (TRIGGER_NONE_BLOCK_UP30, 4),
    (TRIGGER_EXAM_CARD_PLAY_REVIEW_UP10, 3),
    (TRIGGER_EXAM_CARD_PLAY_REVIEW_UP1, 2),
)
EXPECTED_FAMILY_OCCURRENCE_COUNTS: Final = (
    (RemainingEffectTriggerFamily.EXAM_CARD_PLAY_AGGRESSIVE.value, 28),
    (RemainingEffectTriggerFamily.NONE_REVIEW.value, 28),
    (RemainingEffectTriggerFamily.EXAM_CARD_PLAY_REVIEW.value, 15),
    (RemainingEffectTriggerFamily.NONE_CARD_SEARCH_COUNT.value, 12),
    (RemainingEffectTriggerFamily.NONE_BLOCK.value, 4),
)

ALL_ORIGINS: Final = tuple(EffectTriggerOrigin)
NOT_LOST_SOURCE_ZONE_ORDER: Final = ("hand", "deck", "grave", "hold")

ANDROID_NATIVE_EVIDENCE: Final[Mapping[str, object]] = {
    "version": "Android v3.2.3",
    "predicate": (
        "ExamExtensions.IsEffectTriggerFieldValid -> "
        "IsFieldStatusTriggerStatusEffect"
    ),
    "comparison": "signed current field value >= signed Int32 threshold",
    "searchCount": (
        "GetSearchCardList(rule, context, predicate) result count >= threshold"
    ),
    "directBoundary": (
        "effect-slot candidates are selected before payment and before queued "
        "direct effects; the original trigger is not rerun at dispatch"
    ),
    "origins": (
        "normal, forced, and extra use the same field predicate; origin changes "
        "batch/move handling, not the comparison"
    ),
}
PC_METADATA_EVIDENCE: Final[Mapping[str, object]] = {
    "role": "structural cross-check only",
    "confirmedShapes": (
        "IsEffectTriggerFieldValid",
        "ExecuteCardCommandImpl",
        "GetSearchCardList",
    ),
    "nativeBodyAuthority": "Android v3.2.3",
}


class Plan2RemainingEffectTriggerContractError(ValueError):
    """Master or runtime input is outside this exact overlay."""


@dataclass(frozen=True, slots=True)
class TroubleSearchMasterRow:
    search_id: str
    card_categories: tuple[str, ...]
    card_position_type: str
    limit_count: int


@dataclass(frozen=True, slots=True)
class Plan2RemainingEffectTriggerHandoff:
    base: Plan2NativeEffectTriggerHandoff
    family: RemainingEffectTriggerFamily
    field: str
    threshold: int
    supported_boundaries: tuple[RemainingEffectTriggerBoundary, ...]
    allowed_origins: tuple[EffectTriggerOrigin, ...] = ALL_ORIGINS
    target_predicate_executable: bool = True
    companion_effect_executable_here: bool = False
    whole_card_executable: bool = False
    listener_kind: str = "card-owned-triggered-direct-effect-slot"
    count_scope: str = "none"
    lifetime_scope: str = "one-command-candidate-build"
    comparison: str = "signed-greater-than-or-equal"
    same_card_reread: bool = False

    @property
    def card_id(self) -> str:
        return self.base.card_id

    @property
    def upgrade(self) -> int:
        return self.base.upgrade

    @property
    def slot_index(self) -> int:
        return self.base.slot_index

    @property
    def ref(self) -> tuple[str, int]:
        return self.base.ref

    @property
    def occurrence_key(self) -> tuple[str, int, int]:
        return self.base.occurrence_key

    @property
    def trigger_id(self) -> str:
        return self.base.trigger.trigger_id

    @property
    def trigger_effect_ids(self) -> tuple[str, ...]:
        return self.base.trigger_effect_ids

    @property
    def effect_id(self) -> str:
        return self.base.effect_id

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return self.base.ordered_effect_ids

    @property
    def effects_before(self) -> tuple[str, ...]:
        return self.base.effects_before

    @property
    def effects_after(self) -> tuple[str, ...]:
        return self.base.effects_after


@dataclass(frozen=True, slots=True)
class Plan2RemainingEffectTriggerAccounting:
    occurrence_count: int
    unique_effect_count: int
    unique_version_count: int
    card_id_count: int
    trigger_id_count: int
    executable_occurrence_count: int
    blocked_occurrence_count: int
    companion_handoff_occurrence_count: int
    whole_card_executable_version_count: int
    trigger_occurrences: tuple[tuple[str, int], ...]
    family_occurrences: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Plan2RemainingEffectTriggerCatalog:
    schema_version: int
    adapter_id: str
    search: TroubleSearchMasterRow
    handoffs: tuple[Plan2RemainingEffectTriggerHandoff, ...]
    accounting: Plan2RemainingEffectTriggerAccounting

    def occurrence(
        self, card_id: str, upgrade: int, slot_index: int
    ) -> Plan2RemainingEffectTriggerHandoff:
        for handoff in self.handoffs:
            if handoff.occurrence_key == (card_id, upgrade, slot_index):
                return handoff
        raise KeyError((card_id, upgrade, slot_index))

    def for_version(
        self, card_id: str, upgrade: int
    ) -> tuple[Plan2RemainingEffectTriggerHandoff, ...]:
        return tuple(row for row in self.handoffs if row.ref == (card_id, upgrade))


def _strings(value: object, label: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2RemainingEffectTriggerContractError(
            f"{label}-invalid-json"
        ) from error
    if not isinstance(parsed, list) or any(type(item) is not str for item in parsed):
        raise Plan2RemainingEffectTriggerContractError(f"{label}-invalid-array")
    return tuple(parsed)


def _ints(value: object, label: str) -> tuple[int, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2RemainingEffectTriggerContractError(
            f"{label}-invalid-json"
        ) from error
    if not isinstance(parsed, list) or any(
        type(item) is not int or not INT32_MIN <= item <= INT32_MAX
        for item in parsed
    ):
        raise Plan2RemainingEffectTriggerContractError(f"{label}-invalid-array")
    return tuple(parsed)


def _plain_i32(value: object, label: str) -> int:
    if type(value) is not int or not INT32_MIN <= value <= INT32_MAX:
        raise Plan2RemainingEffectTriggerContractError(f"{label}-invalid-int32")
    return value


def _load_search(database: Path) -> TroubleSearchMasterRow:
    path = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM produce_card_search WHERE id = ?",
                (SEARCH_TROUBLE_NOT_LOST,),
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan2RemainingEffectTriggerContractError(
            "search-master-read-failed"
        ) from error
    if row is None:
        raise Plan2RemainingEffectTriggerContractError("search-row-missing")
    search = TroubleSearchMasterRow(
        str(row["id"]),
        _strings(row["card_categories_json"], "search-card-categories"),
        str(row["card_position_type"]),
        _plain_i32(row["limit_count"], "search-limit-count"),
    )
    if search != TroubleSearchMasterRow(
        SEARCH_TROUBLE_NOT_LOST,
        (CATEGORY_TROUBLE,),
        POSITION_NOT_LOST,
        0,
    ):
        raise Plan2RemainingEffectTriggerContractError("search-shape-changed")
    operational = {
        "card_rarities": _strings(
            row["card_rarities_json"], "search-card-rarities"
        ),
        "produce_card_ids": _strings(
            row["produce_card_ids_json"], "search-produce-card-ids"
        ),
        "upgrade_counts": _ints(
            row["upgrade_counts_json"], "search-upgrade-counts"
        ),
        "plan_type": str(row["plan_type"]),
        "card_status_type": str(row["card_status_type"]),
        "order_type": str(row["order_type"]),
        "card_search_tag": str(row["card_search_tag"]),
        "produce_card_random_pool_id": str(row["produce_card_random_pool_id"]),
        "stamina_min_max_type": str(row["stamina_min_max_type"]),
        "stamina_min": _plain_i32(row["stamina_min"], "search-stamina-min"),
        "stamina_max": _plain_i32(row["stamina_max"], "search-stamina-max"),
        "exam_effect_type": str(row["exam_effect_type"]),
        "effect_group_ids": _strings(
            row["effect_group_ids_json"], "search-effect-group-ids"
        ),
        "is_self": row["is_self"],
        "produce_card_pool_id": str(row["produce_card_pool_id"]),
        "cost_type": str(row["cost_type"]),
        "is_customized": row["is_customized"],
    }
    expected_operational = {
        "card_rarities": (),
        "produce_card_ids": (),
        "upgrade_counts": (),
        "plan_type": "ProducePlanType_Unknown",
        "card_status_type": "ProduceCardSearchStatusType_Unknown",
        "order_type": "ProduceCardOrderType_Unknown",
        "card_search_tag": "",
        "produce_card_random_pool_id": "",
        "stamina_min_max_type": "ConditionMinMaxType_Unknown",
        "stamina_min": 0,
        "stamina_max": 0,
        "exam_effect_type": "ProduceExamEffectType_Unknown",
        "effect_group_ids": (),
        "is_self": 0,
        "produce_card_pool_id": "",
        "cost_type": "ExamCostType_Unknown",
        "is_customized": 0,
    }
    if operational != expected_operational:
        raise Plan2RemainingEffectTriggerContractError(
            "search-operational-shape-changed"
        )
    try:
        raw = json.loads(str(row["raw_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2RemainingEffectTriggerContractError(
            "search-raw-invalid"
        ) from error
    raw_expected = (
        ("id", SEARCH_TROUBLE_NOT_LOST),
        ("cardRarities", []),
        ("produceCardIds", []),
        ("upgradeCounts", []),
        ("planType", "ProducePlanType_Unknown"),
        ("cardCategories", [CATEGORY_TROUBLE]),
        ("cardStatusType", "ProduceCardSearchStatusType_Unknown"),
        ("orderType", "ProduceCardOrderType_Unknown"),
        ("cardPositionType", POSITION_NOT_LOST),
        ("cardSearchTag", ""),
        ("produceCardRandomPoolId", ""),
        ("limitCount", 0),
        ("staminaMinMaxType", "ConditionMinMaxType_Unknown"),
        ("staminaMin", 0),
        ("staminaMax", 0),
        ("examEffectType", "ProduceExamEffectType_Unknown"),
        ("effectGroupIds", []),
        ("isSelf", False),
        ("produceCardPoolId", ""),
        ("costType", "ExamCostType_Unknown"),
        ("isCustomized", False),
    )
    if not isinstance(raw, dict) or any(
        raw.get(key) != expected
        for key, expected in raw_expected
    ):
        raise Plan2RemainingEffectTriggerContractError("search-raw-shape-changed")
    return search


def _assert_handoff_shape(
    handoff: Plan2NativeEffectTriggerHandoff, definition: _Definition
) -> None:
    trigger = handoff.trigger
    actual = (
        trigger.phase_types,
        trigger.phase_values,
        trigger.field_status_check_types,
        trigger.field_status_types,
        trigger.field_status_values,
        trigger.field_status_card_search_ids,
        trigger.produce_card_search_id,
        trigger.upper_search_count,
        trigger.lower_search_count,
        trigger.effect_types,
    )
    expected = (
        (definition.phase,),
        (),
        (),
        (definition.field,),
        (definition.threshold,),
        (definition.search_id,) if definition.search_id else (),
        "",
        0,
        0,
        (),
    )
    if actual != expected:
        raise Plan2RemainingEffectTriggerContractError(
            f"trigger-shape-changed:{trigger.trigger_id}"
        )
    if handoff.target_predicate_executable:
        raise Plan2RemainingEffectTriggerContractError(
            f"base-owner-overlap:{handoff.occurrence_key!r}"
        )
    if handoff.companion_effect_executable_here or handoff.whole_card_executable:
        raise Plan2RemainingEffectTriggerContractError(
            f"base-scope-changed:{handoff.occurrence_key!r}"
        )


def load_plan2_native_catalog_effect_triggers_remaining(
    database: Path = DEFAULT_DATABASE,
) -> Plan2RemainingEffectTriggerCatalog:
    """Load and certify all 87 exact remaining direct-effect predicates."""

    try:
        base = load_plan2_native_catalog_effect_triggers(database)
    except Plan2NativeEffectTriggerContractError as error:
        raise Plan2RemainingEffectTriggerContractError(
            "base-effect-trigger-catalog-changed"
        ) from error
    search = _load_search(database)
    selected = tuple(
        row for row in base.handoffs if row.trigger.trigger_id in _DEFINITIONS
    )
    handoffs: list[Plan2RemainingEffectTriggerHandoff] = []
    for row in selected:
        definition = _DEFINITIONS[row.trigger.trigger_id]
        _assert_handoff_shape(row, definition)
        handoffs.append(
            Plan2RemainingEffectTriggerHandoff(
                base=row,
                family=definition.family,
                field=definition.field,
                threshold=definition.threshold,
                supported_boundaries=(definition.boundary,),
            )
        )

    trigger_counts = Counter(row.trigger_id for row in handoffs)
    family_counts = Counter(row.family.value for row in handoffs)
    accounting = Plan2RemainingEffectTriggerAccounting(
        occurrence_count=len(handoffs),
        unique_effect_count=len({row.effect_id for row in handoffs}),
        unique_version_count=len({row.ref for row in handoffs}),
        card_id_count=len({row.card_id for row in handoffs}),
        trigger_id_count=len(trigger_counts),
        executable_occurrence_count=sum(
            row.target_predicate_executable for row in handoffs
        ),
        blocked_occurrence_count=sum(
            not row.target_predicate_executable for row in handoffs
        ),
        companion_handoff_occurrence_count=sum(
            not row.companion_effect_executable_here for row in handoffs
        ),
        whole_card_executable_version_count=len(
            {row.ref for row in handoffs if row.whole_card_executable}
        ),
        trigger_occurrences=tuple(
            (trigger_id, trigger_counts[trigger_id])
            for trigger_id, _ in EXPECTED_TRIGGER_OCCURRENCE_COUNTS
        ),
        family_occurrences=tuple(
            (family, family_counts[family])
            for family, _ in EXPECTED_FAMILY_OCCURRENCE_COUNTS
        ),
    )
    scalars = (
        accounting.occurrence_count,
        accounting.unique_effect_count,
        accounting.unique_version_count,
        accounting.card_id_count,
        accounting.trigger_id_count,
        accounting.executable_occurrence_count,
        accounting.blocked_occurrence_count,
        accounting.companion_handoff_occurrence_count,
        accounting.whole_card_executable_version_count,
    )
    expected_scalars = (
        EXPECTED_OCCURRENCE_COUNT,
        EXPECTED_UNIQUE_EFFECT_COUNT,
        EXPECTED_UNIQUE_VERSION_COUNT,
        EXPECTED_CARD_ID_COUNT,
        EXPECTED_TRIGGER_ID_COUNT,
        EXPECTED_EXECUTABLE_OCCURRENCE_COUNT,
        0,
        EXPECTED_OCCURRENCE_COUNT,
        0,
    )
    if (
        scalars != expected_scalars
        or accounting.trigger_occurrences != EXPECTED_TRIGGER_OCCURRENCE_COUNTS
        or accounting.family_occurrences != EXPECTED_FAMILY_OCCURRENCE_COUNTS
    ):
        raise Plan2RemainingEffectTriggerContractError(
            f"remaining-effect-trigger-inventory-changed:{scalars!r}"
        )
    return Plan2RemainingEffectTriggerCatalog(
        PLAN2_NATIVE_CATALOG_EFFECT_TRIGGERS_REMAINING_SCHEMA_VERSION,
        REMAINING_EFFECT_TRIGGER_ADAPTER_ID,
        search,
        tuple(handoffs),
        accounting,
    )


compile_plan2_native_catalog_effect_triggers_remaining = (
    load_plan2_native_catalog_effect_triggers_remaining
)


@dataclass(frozen=True, slots=True)
class Plan2RemainingSearchCardSnapshot:
    guid: object
    category: object


@dataclass(frozen=True, slots=True)
class Plan2RemainingSearchZoneSnapshot:
    hand: object = ()
    deck: object = ()
    grave: object = ()
    lost: object = ()
    hold: object = ()
    playing: object | None = None
    future_decks: object = ()
    past_decks: object = ()
    complete: object = True


@dataclass(frozen=True, slots=True)
class Plan2RemainingEffectTriggerSnapshot:
    phase: object
    boundary: object
    origin: object = EffectTriggerOrigin.NORMAL
    aggressive: object | None = None
    review: object | None = None
    block: object | None = None
    search_zones: object | None = None
    payment_committed_before_build: object = False
    direct_effects_executed_before_build: object = 0
    listener_spend_count: object = 0
    listener_remaining_turn: object | None = None
    later_same_card_field_value: object | None = None
    later_same_card_search_zones: object | None = None


@dataclass(frozen=True, slots=True)
class Plan2RemainingExactPredicateEvaluation:
    supported: bool
    fires: bool | None
    observed_value: int | None
    threshold: int
    comparison: str
    source_zones: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None


@dataclass(frozen=True, slots=True)
class Plan2RemainingEffectTriggerEvaluation:
    occurrence_key: tuple[str, int, int] | None
    trigger_id: str
    family: str
    supported: bool
    fires: bool | None
    observed_value: int | None
    threshold: int | None
    phase: object
    boundary: object
    origin: object
    before: Plan2RemainingEffectTriggerSnapshot
    after: Plan2RemainingEffectTriggerSnapshot
    target_predicate_executable: bool
    companion_effect_executed: bool
    whole_card_executable: bool
    same_card_reread: bool
    search_source_zones: tuple[str, ...]
    reasons: tuple[str, ...]

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def _runtime_i32(value: object, label: str, reasons: list[str]) -> int | None:
    if type(value) is not int:
        reasons.append(f"{label}-is-not-plain-int32")
        return None
    if not INT32_MIN <= value <= INT32_MAX:
        reasons.append(f"{label}-is-outside-int32")
        return None
    return value


def evaluate_plan2_remaining_signed_field_predicate(
    current_value: object, threshold: object
) -> Plan2RemainingExactPredicateEvaluation:
    """Evaluate the exact signed native field branch, independently of a card."""

    reasons: list[str] = []
    current = _runtime_i32(current_value, "current-field-value", reasons)
    limit = _runtime_i32(threshold, "field-threshold", reasons)
    if reasons or current is None or limit is None:
        return Plan2RemainingExactPredicateEvaluation(
            False,
            None,
            current,
            0 if limit is None else limit,
            "signed-greater-than-or-equal",
            reasons=tuple(reasons),
        )
    return Plan2RemainingExactPredicateEvaluation(
        True,
        current >= limit,
        current,
        limit,
        "signed-greater-than-or-equal",
    )


def _card_tuple(
    value: object, label: str, reasons: list[str]
) -> tuple[Plan2RemainingSearchCardSnapshot, ...] | None:
    if type(value) is not tuple or any(
        not isinstance(card, Plan2RemainingSearchCardSnapshot) for card in value
    ):
        reasons.append(f"{label}-is-not-exact-card-tuple")
        return None
    cards = value
    for card in cards:
        if type(card.guid) is not str or not card.guid:
            reasons.append(f"{label}-card-guid-invalid")
        if type(card.category) is not str or not card.category:
            reasons.append(f"{label}-card-category-invalid")
    return cards


def _timeline_tuple(
    value: object, label: str, reasons: list[str]
) -> tuple[tuple[Plan2RemainingSearchCardSnapshot, ...], ...] | None:
    if type(value) is not tuple:
        reasons.append(f"{label}-is-not-exact-deck-tuple")
        return None
    result: list[tuple[Plan2RemainingSearchCardSnapshot, ...]] = []
    for index, deck in enumerate(value):
        parsed = _card_tuple(deck, f"{label}-{index}", reasons)
        if parsed is not None:
            result.append(parsed)
    return tuple(result)


def evaluate_plan2_remaining_trouble_not_lost_count_predicate(
    snapshot: object, threshold: object = 2
) -> Plan2RemainingExactPredicateEvaluation:
    """Count Trouble cards from the exact Hand/Deck/Grave/Hold snapshot."""

    reasons: list[str] = []
    limit = _runtime_i32(threshold, "search-count-threshold", reasons)
    if not isinstance(snapshot, Plan2RemainingSearchZoneSnapshot):
        reasons.append("search-zone-snapshot-missing-or-unknown")
        return Plan2RemainingExactPredicateEvaluation(
            False,
            None,
            None,
            0 if limit is None else limit,
            "count-greater-than-or-equal",
            NOT_LOST_SOURCE_ZONE_ORDER,
            tuple(reasons),
        )
    if snapshot.complete is not True:
        reasons.append("search-zone-snapshot-incomplete")
    ordinary: dict[str, tuple[Plan2RemainingSearchCardSnapshot, ...]] = {}
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        parsed = _card_tuple(getattr(snapshot, zone), zone, reasons)
        if parsed is not None:
            ordinary[zone] = parsed
    playing: tuple[Plan2RemainingSearchCardSnapshot, ...]
    if snapshot.playing is None:
        playing = ()
    elif isinstance(snapshot.playing, Plan2RemainingSearchCardSnapshot):
        playing = (snapshot.playing,)
        _card_tuple(playing, "playing", reasons)
    else:
        playing = ()
        reasons.append("playing-card-invalid")
    future = _timeline_tuple(snapshot.future_decks, "future-decks", reasons)
    past = _timeline_tuple(snapshot.past_decks, "past-decks", reasons)
    every_card = tuple(card for cards in ordinary.values() for card in cards)
    every_card += playing
    if future is not None:
        every_card += tuple(card for deck in future for card in deck)
    if past is not None:
        every_card += tuple(card for deck in past for card in deck)
    guids = tuple(card.guid for card in every_card if type(card.guid) is str)
    if len(set(guids)) != len(guids):
        reasons.append("duplicate-card-guid-across-zone-snapshot")
    if reasons or limit is None:
        return Plan2RemainingExactPredicateEvaluation(
            False,
            None,
            None,
            0 if limit is None else limit,
            "count-greater-than-or-equal",
            NOT_LOST_SOURCE_ZONE_ORDER,
            tuple(dict.fromkeys(reasons)),
        )
    candidates = tuple(
        card
        for zone in NOT_LOST_SOURCE_ZONE_ORDER
        for card in ordinary[zone]
        if card.category == CATEGORY_TROUBLE
    )
    count = len(candidates)
    if count > INT32_MAX:
        return Plan2RemainingExactPredicateEvaluation(
            False,
            None,
            None,
            limit,
            "count-greater-than-or-equal",
            NOT_LOST_SOURCE_ZONE_ORDER,
            ("search-result-count-is-outside-int32",),
        )
    return Plan2RemainingExactPredicateEvaluation(
        True,
        count >= limit,
        count,
        limit,
        "count-greater-than-or-equal",
        NOT_LOST_SOURCE_ZONE_ORDER,
    )


def _unsupported(
    handoff: Plan2RemainingEffectTriggerHandoff | None,
    snapshot: Plan2RemainingEffectTriggerSnapshot,
    reasons: tuple[str, ...] | list[str],
) -> Plan2RemainingEffectTriggerEvaluation:
    return Plan2RemainingEffectTriggerEvaluation(
        None if handoff is None else handoff.occurrence_key,
        "unknown" if handoff is None else handoff.trigger_id,
        "" if handoff is None else handoff.family.value,
        False,
        None,
        None,
        None if handoff is None else handoff.threshold,
        snapshot.phase,
        snapshot.boundary,
        snapshot.origin,
        snapshot,
        snapshot,
        False if handoff is None else handoff.target_predicate_executable,
        False,
        False,
        False,
        (),
        tuple(dict.fromkeys(reasons)),
    )


def evaluate_plan2_native_catalog_effect_trigger_remaining(
    source: Plan2RemainingEffectTriggerHandoff | object,
    snapshot: Plan2RemainingEffectTriggerSnapshot,
) -> Plan2RemainingEffectTriggerEvaluation:
    """Evaluate one exact overlay predicate and leave all state untouched."""

    if not isinstance(snapshot, Plan2RemainingEffectTriggerSnapshot):
        raise TypeError("snapshot must be Plan2RemainingEffectTriggerSnapshot")
    if not isinstance(source, Plan2RemainingEffectTriggerHandoff):
        return _unsupported(None, snapshot, ("unknown-occurrence-failed-closed",))
    handoff = source
    definition = _DEFINITIONS.get(handoff.trigger_id)
    if (
        definition is None
        or handoff.family is not definition.family
        or handoff.field != definition.field
        or handoff.threshold != definition.threshold
        or handoff.supported_boundaries != (definition.boundary,)
        or not handoff.target_predicate_executable
        or handoff.companion_effect_executable_here
        or handoff.whole_card_executable
    ):
        return _unsupported(handoff, snapshot, ("handoff-contract-changed",))

    reasons: list[str] = []
    try:
        origin = EffectTriggerOrigin(snapshot.origin)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        origin = None
        reasons.append("card-origin-unknown")
    try:
        boundary = RemainingEffectTriggerBoundary(snapshot.boundary)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        boundary = None
        reasons.append("effect-trigger-boundary-unknown")
    if snapshot.phase != definition.phase:
        reasons.append("effect-trigger-phase-mismatch")
    if boundary not in handoff.supported_boundaries:
        reasons.append("effect-trigger-boundary-unproven")
    if origin is not None and origin not in handoff.allowed_origins:
        reasons.append("card-origin-unproven-for-occurrence")
    if type(snapshot.payment_committed_before_build) is not bool:
        reasons.append("payment-state-is-not-boolean")
    elif snapshot.payment_committed_before_build:
        reasons.append("effect-trigger-snapshot-is-post-payment")
    direct_count = _runtime_i32(
        snapshot.direct_effects_executed_before_build,
        "direct-effects-executed-before-build",
        reasons,
    )
    listener_count = _runtime_i32(
        snapshot.listener_spend_count,
        "listener-spend-count",
        reasons,
    )
    if direct_count is not None and direct_count != 0:
        reasons.append("direct-effects-already-executed")
    if listener_count is not None and listener_count != 0:
        reasons.append("direct-effect-trigger-is-not-listener-counted")
    if snapshot.listener_remaining_turn is not None:
        reasons.append("direct-effect-trigger-has-no-listener-lifetime")
    if reasons:
        return _unsupported(handoff, snapshot, reasons)

    exact: Plan2RemainingExactPredicateEvaluation
    if definition.field == FIELD_AGGRESSIVE:
        exact = evaluate_plan2_remaining_signed_field_predicate(
            snapshot.aggressive, definition.threshold
        )
    elif definition.field == FIELD_REVIEW:
        exact = evaluate_plan2_remaining_signed_field_predicate(
            snapshot.review, definition.threshold
        )
    elif definition.field == FIELD_BLOCK:
        exact = evaluate_plan2_remaining_signed_field_predicate(
            snapshot.block, definition.threshold
        )
    elif definition.field == FIELD_SEARCH_COUNT:
        exact = evaluate_plan2_remaining_trouble_not_lost_count_predicate(
            snapshot.search_zones, definition.threshold
        )
    else:
        return _unsupported(handoff, snapshot, ("field-family-unproven",))
    if not exact.supported or type(exact.fires) is not bool:
        return _unsupported(
            handoff,
            snapshot,
            exact.reasons or ("exact-predicate-failed-closed",),
        )
    return Plan2RemainingEffectTriggerEvaluation(
        handoff.occurrence_key,
        handoff.trigger_id,
        handoff.family.value,
        True,
        exact.fires,
        exact.observed_value,
        exact.threshold,
        snapshot.phase,
        snapshot.boundary,
        snapshot.origin,
        snapshot,
        snapshot,
        True,
        False,
        False,
        False,
        exact.source_zones,
        (),
    )


evaluate_plan2_remaining_effect_trigger = (
    evaluate_plan2_native_catalog_effect_trigger_remaining
)
evaluate_plan2_native_effect_trigger_remaining = (
    evaluate_plan2_native_catalog_effect_trigger_remaining
)


__all__ = [
    "ALL_ORIGINS",
    "ANDROID_NATIVE_EVIDENCE",
    "CATEGORY_TROUBLE",
    "EXPECTED_CARD_ID_COUNT",
    "EXPECTED_EXECUTABLE_OCCURRENCE_COUNT",
    "EXPECTED_FAMILY_OCCURRENCE_COUNTS",
    "EXPECTED_OCCURRENCE_COUNT",
    "EXPECTED_TRIGGER_ID_COUNT",
    "EXPECTED_TRIGGER_OCCURRENCE_COUNTS",
    "EXPECTED_UNIQUE_EFFECT_COUNT",
    "EXPECTED_UNIQUE_VERSION_COUNT",
    "NOT_LOST_SOURCE_ZONE_ORDER",
    "PC_METADATA_EVIDENCE",
    "PLAN2_NATIVE_CATALOG_EFFECT_TRIGGERS_REMAINING_SCHEMA_VERSION",
    "POSITION_NOT_LOST",
    "Plan2RemainingEffectTriggerAccounting",
    "Plan2RemainingEffectTriggerCatalog",
    "Plan2RemainingEffectTriggerContractError",
    "Plan2RemainingEffectTriggerEvaluation",
    "Plan2RemainingEffectTriggerHandoff",
    "Plan2RemainingEffectTriggerSnapshot",
    "Plan2RemainingExactPredicateEvaluation",
    "Plan2RemainingSearchCardSnapshot",
    "Plan2RemainingSearchZoneSnapshot",
    "REMAINING_EFFECT_TRIGGER_ADAPTER_ID",
    "RemainingEffectTriggerBoundary",
    "RemainingEffectTriggerFamily",
    "SEARCH_TROUBLE_NOT_LOST",
    "TARGET_TRIGGER_IDS",
    "TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP3",
    "TRIGGER_EXAM_CARD_PLAY_AGGRESSIVE_UP6",
    "TRIGGER_EXAM_CARD_PLAY_REVIEW_UP1",
    "TRIGGER_EXAM_CARD_PLAY_REVIEW_UP3",
    "TRIGGER_EXAM_CARD_PLAY_REVIEW_UP10",
    "TRIGGER_NONE_BLOCK_UP30",
    "TRIGGER_NONE_CARD_SEARCH_COUNT_UP2",
    "TRIGGER_NONE_REVIEW_UP10",
    "TRIGGER_NONE_REVIEW_UP15",
    "TroubleSearchMasterRow",
    "compile_plan2_native_catalog_effect_triggers_remaining",
    "evaluate_plan2_native_catalog_effect_trigger_remaining",
    "evaluate_plan2_native_effect_trigger_remaining",
    "evaluate_plan2_remaining_effect_trigger",
    "evaluate_plan2_remaining_signed_field_predicate",
    "evaluate_plan2_remaining_trouble_not_lost_count_predicate",
    "load_plan2_native_catalog_effect_triggers_remaining",
]
