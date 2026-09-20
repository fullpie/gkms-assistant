"""Exact standalone Plan2 timer chains for Block/LessonDependBlock children.

This leaf owns sixteen card versions and deliberately stays outside the
central Plan2 dispatcher.  It validates every source-card play slot and the
exact timer/child rows, then reuses the proven relative-StartTurn timer
scheduler and Block/parameter formula primitives.

The native boundary is relative, not an absolute turn number: a newly
installed delay-1 timer fires at the first *later* StartTurn and delay 4 at
the fourth.  Due timers are captured and executed in active installation
order, each child gets the state left by the preceding child, and every due
one-shot listener is removed only after the ordered due batch.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Literal, TypeAlias

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    AddBlockSettings,
    AddBlockStatus,
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_add_block,
    calculate_adding_parameter,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan2_block_depend_block_consumption_sum import (
    BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE,
    Plan2BlockDependConsumptionSumRuntime,
    Plan2BlockDependDifference,
    Plan2SetBlockTransition,
    _i32 as _block_i32,
    apply_plan2_set_block,
)
from .plan2_end_turn_trigger import _lesson_depend_block_raw_score
from .plan2_exam_effect_timer import (
    Plan2ExamEffectTimerContract,
    Plan2ExamEffectTimerContractError,
)
from .plan2_timer_lesson_depend_review import (
    Plan2TimerLessonDependReviewContractError,
    _plain_i32 as _review_timer_plain_i32,
)
from .plan3_engine import (
    CATEGORY_ACTIVE,
    CATEGORY_MENTAL,
    COST_STAMINA,
    EFFECT_STATUS_ENCHANT,
    EFFECT_TIMER,
    LESSON_GENERAL,
    MOVE_LOST,
    MOVE_UNKNOWN,
    PHASE_END_TURN,
    PHASE_START_TURN,
    PHASE_TURN_TIMER,
    Plan3Card,
    Plan3Effect,
    Plan3State,
    Plan3Trigger,
    _advance_status_enchants_for_turn_start,
    _install_effect_timer,
    _remove_triggered_turn_timer_statuses,
    end_plan3_turn,
    load_plan3_card,
)


PLAN2 = "ProducePlanType_Plan2"
EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"
EFFECT_LESSON_DEPEND_BLOCK = "ProduceExamEffectType_ExamLessonDependBlock"
EFFECT_AGGRESSIVE = "ProduceExamEffectType_ExamCardPlayAggressive"
EFFECT_AGGRESSIVE_MULTIPLE = (
    "ProduceExamEffectType_ExamAggressiveValueMultiple"
)
EFFECT_DEBUFF_RECOVER = "ProduceExamEffectType_ExamDebuffRecover"

PHASE_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"
FIELD_REMAINING_TURN = "ProduceExamFieldStatusType_RemainingTurn"
EFFECT_TYPE_UNKNOWN = "ProduceExamEffectType_Unknown"
LESSON_TYPE_UNKNOWN = "ProduceStepLessonType_Unknown"

CARD_ACT186 = "p_card-02-act-3_186"
CARD_IDO103 = "p_card-02-ido-3_103"
CARD_IDO127 = "p_card-02-ido-3_127"
CARD_IDO130 = "p_card-02-ido-3_130"
TARGET_CARD_IDS = (CARD_ACT186, CARD_IDO103, CARD_IDO127, CARD_IDO130)
TARGET_UPGRADES = (0, 1, 2, 3)

BLOCK_GROUP = "effect_group-visible-exam_block-000"
TIMER_GROUP = "effect_group-visible-exam_effect_timer-000"
LESSON_GROUP = "effect_group-visible-exam_lesson-000"
LESSON_DEPEND_BLOCK_GROUP = (
    "effect_group-visible-exam_lesson_depend_block-000"
)
STATUS_ENCHANT_GROUP = "effect_group-visible-exam_status_enchant-000"
AGGRESSIVE_GROUP = "effect_group-visible-exam_card_play_aggressive-000"

BLOCK_TIMER_GROUPS = (BLOCK_GROUP, TIMER_GROUP)
BLOCK_CHILD_GROUPS = (BLOCK_GROUP,)
LESSON_TIMER_GROUPS = (
    LESSON_DEPEND_BLOCK_GROUP,
    LESSON_GROUP,
    TIMER_GROUP,
)
LESSON_CHILD_GROUPS = (LESSON_DEPEND_BLOCK_GROUP, LESSON_GROUP)

GAP_TIMER = "C:effect:ProduceExamEffectType_ExamEffectTimer"
GAP_AGGRESSIVE_MULTIPLE = (
    "C:effect:ProduceExamEffectType_ExamAggressiveValueMultiple"
)
GAP_DEBUFF_RECOVER = "C:effect:ProduceExamEffectType_ExamDebuffRecover"
GAP_STATUS_CHANGE_30_BLOCK = (
    "C:status-trigger:e_trigger-exam_status_change-30-exam_block"
)

PLAY_ORIGINS = ("ordinary", "forced", "extra")
NATIVE_PHASE_ORDER = (
    "ExamEndTurn",
    "ordinary-draw",
    "ExamStartTurn",
    "StartPlay",
    "ExamTurnTimer-capture-active-order",
    "ordered-due-child-commands",
    "deferred-difference-callback-after-each-child",
    "RemoveTurnTimerTriggeredStatus",
)


class Plan2TimerBlockChainsContractError(Plan2ExamEffectTimerContractError):
    """Stable fail-closed error for this sixteen-version standalone leaf."""


def _plain_i32(value: object, label: str) -> int:
    """Reuse the prior Plan2 timer adapter's signed-Int32 admission."""

    try:
        return _review_timer_plain_i32(value, label)  # type: ignore[arg-type]
    except Plan2TimerLessonDependReviewContractError as error:
        raise Plan2TimerBlockChainsContractError(
            "signed-int32-out-of-range", label
        ) from error


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2TimerBlockChainsContractError(
            "invalid-master-json", label
        ) from error
    if not isinstance(parsed, dict):
        raise Plan2TimerBlockChainsContractError(
            "invalid-master-json-object", label
        )
    return parsed


def _require(raw: Mapping[str, object], key: str, expected: object, label: str) -> None:
    if raw.get(key) != expected:
        raise Plan2TimerBlockChainsContractError(
            "master-shape-drift", f"{label}:{key}"
        )


def _effect_groups(
    connection: sqlite3.Connection, effect_id: str
) -> tuple[str, ...]:
    row = connection.execute(
        "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
    ).fetchone()
    if row is None:
        raise Plan2TimerBlockChainsContractError(
            "master-effect-missing", effect_id
        )
    raw = _json_object(row[0], effect_id)
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or any(
        not isinstance(value, str) or not value for value in groups
    ):
        raise Plan2TimerBlockChainsContractError(
            "master-effect-groups-invalid", effect_id
        )
    return tuple(groups)


@dataclass(frozen=True, slots=True)
class _NestedStatusSpec:
    status_id: str
    trigger: Plan3Trigger
    child_effect_id: str
    child_effect_type: str
    child_value1: int
    child_count: int


@dataclass(frozen=True, slots=True)
class _HandoffSpec:
    slot_index: int
    effect_id: str
    effect_type: str
    value1: int
    value2: int = 0
    effect_count: int = 0
    effect_turn: int = 0
    effect_groups: tuple[str, ...] = ()
    nested_status: _NestedStatusSpec | None = None
    pick_count_min: int = 0
    pick_count_max: int = 0


@dataclass(frozen=True, slots=True)
class _TimerSpec:
    slot_index: int
    delay: int
    child_effect_id: str
    child_effect_type: str
    child_value1: int
    child_count: int

    @property
    def effect_id(self) -> str:
        return (
            f"e_effect-exam_effect_timer-{self.delay:04d}-01-"
            f"{self.child_effect_id}"
        )

    @property
    def timer_groups(self) -> tuple[str, ...]:
        return (
            LESSON_TIMER_GROUPS
            if self.child_effect_type == EFFECT_LESSON_DEPEND_BLOCK
            else BLOCK_TIMER_GROUPS
        )

    @property
    def child_groups(self) -> tuple[str, ...]:
        return (
            LESSON_CHILD_GROUPS
            if self.child_effect_type == EFFECT_LESSON_DEPEND_BLOCK
            else BLOCK_CHILD_GROUPS
        )


SlotSpec: TypeAlias = _HandoffSpec | _TimerSpec


@dataclass(frozen=True, slots=True)
class _VersionSpec:
    card_id: str
    base_name: str
    upgrade: int
    category: str
    stamina: int
    force_stamina: int
    is_conversion: bool
    effect_groups: tuple[str, ...]
    slots: tuple[SlotSpec, ...]
    remaining_blockers: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.card_id}+{self.upgrade}"

    @property
    def name(self) -> str:
        return f"{self.base_name}{'+' * self.upgrade}"

    @property
    def customize_ids(self) -> tuple[str, ...]:
        if self.card_id == CARD_ACT186 and self.upgrade > 0:
            return (
                "p_card_custom-wrapper-p_card-02-act-3_186-a-01",
                "p_card_custom-wrapper-p_card-02-act-3_186-b-01",
            )
        return ()

    @property
    def max_customize_count(self) -> int:
        return 1 if self.customize_ids else 0


def _plain_trigger(
    trigger_id: str,
    phase: str,
    *,
    phase_values: tuple[int, ...] = (),
    field_types: tuple[str, ...] = (),
    field_values: tuple[int, ...] = (),
    effect_types: tuple[str, ...] = (),
) -> Plan3Trigger:
    return Plan3Trigger(
        id=trigger_id,
        phase_types=(phase,),
        phase_values=phase_values,
        field_types=field_types,
        field_values=field_values,
        effect_types=effect_types,
    )


_START_TURN_TRIGGER = _plain_trigger(
    "e_trigger-exam_start_turn", PHASE_START_TURN
)
_END_REMAINING_ONE_TRIGGER = _plain_trigger(
    "e_trigger-exam_end_turn-remaining_turn-1",
    PHASE_END_TURN,
    field_types=(FIELD_REMAINING_TURN,),
    field_values=(1,),
)
_STATUS_CHANGE_30_BLOCK_TRIGGER = _plain_trigger(
    "e_trigger-exam_status_change-30-exam_block",
    PHASE_STATUS_CHANGE,
    phase_values=(30,),
    effect_types=(EFFECT_BLOCK,),
)


def _plain_handoff(
    slot: int,
    effect_id: str,
    effect_type: str,
    value1: int,
    groups: tuple[str, ...],
) -> _HandoffSpec:
    return _HandoffSpec(slot, effect_id, effect_type, value1, effect_groups=groups)


_ACT186_STATUS = _HandoffSpec(
    0,
    "e_effect-exam_status_enchant-03-enchant-p_card-02-act-3_186-enc01",
    EFFECT_STATUS_ENCHANT,
    0,
    effect_turn=3,
    effect_groups=(BLOCK_GROUP, STATUS_ENCHANT_GROUP),
    nested_status=_NestedStatusSpec(
        "enchant-p_card-02-act-3_186-enc01",
        _START_TURN_TRIGGER,
        "e_effect-exam_block-0002",
        EFFECT_BLOCK,
        2,
        0,
    ),
)


def _ido103_status(slot: int, suffix: str, value: int) -> _HandoffSpec:
    status_id = f"enchant-p_card-02-ido-3_103-{suffix}"
    return _HandoffSpec(
        slot,
        f"e_effect-exam_status_enchant-inf-{status_id}",
        EFFECT_STATUS_ENCHANT,
        0,
        effect_turn=-1,
        effect_groups=(
            LESSON_DEPEND_BLOCK_GROUP,
            LESSON_GROUP,
            STATUS_ENCHANT_GROUP,
        ),
        nested_status=_NestedStatusSpec(
            status_id,
            _END_REMAINING_ONE_TRIGGER,
            f"e_effect-exam_lesson_depend_block-{value:04d}-01",
            EFFECT_LESSON_DEPEND_BLOCK,
            value,
            1,
        ),
        pick_count_min=1,
        pick_count_max=1,
    )


def _ido130_status(slot: int, suffix: str, value: int) -> _HandoffSpec:
    status_id = f"enchant-p_card-02-ido-3_130-{suffix}"
    return _HandoffSpec(
        slot,
        f"e_effect-exam_status_enchant-inf-{status_id}",
        EFFECT_STATUS_ENCHANT,
        0,
        effect_turn=-1,
        effect_groups=(
            LESSON_DEPEND_BLOCK_GROUP,
            LESSON_GROUP,
            STATUS_ENCHANT_GROUP,
        ),
        nested_status=_NestedStatusSpec(
            status_id,
            _STATUS_CHANGE_30_BLOCK_TRIGGER,
            f"e_effect-exam_lesson_depend_block-{value:04d}-01",
            EFFECT_LESSON_DEPEND_BLOCK,
            value,
            1,
        ),
    )


_ACT186_GROUPS = (
    LESSON_DEPEND_BLOCK_GROUP,
    LESSON_GROUP,
    BLOCK_GROUP,
    STATUS_ENCHANT_GROUP,
    TIMER_GROUP,
)
_IDO103_130_GROUPS = (*_ACT186_GROUPS, AGGRESSIVE_GROUP)
_IDO127_GROUPS = (BLOCK_GROUP, TIMER_GROUP, AGGRESSIVE_GROUP)

_ACT186_VALUES = (1800, 2800, 3200, 3200)
_IDO103_AGGRESSIVE = (4, 6, 7, 8)
_IDO103_BLOCK = (5, 8, 8, 9)
_IDO103_STATUS = (("enc01", 1300), ("enc01", 1300), ("enc05", 1700), ("enc06", 1800))
_IDO127_BLOCK = (4, 8, 10, 10)
_IDO130_AGGRESSIVE = (2, 3, 5, 5)
_IDO130_BLOCK = (3, 3, 3, 6)
_IDO130_STATUS = (("enc02", 200), ("enc01", 300), ("enc01", 300), ("enc01", 300))


def _lesson_timer(slot: int, value: int) -> _TimerSpec:
    return _TimerSpec(
        slot,
        4,
        f"e_effect-exam_lesson_depend_block-{value:04d}-01",
        EFFECT_LESSON_DEPEND_BLOCK,
        value,
        1,
    )


def _block_timer(slot: int, value: int) -> _TimerSpec:
    return _TimerSpec(
        slot,
        1,
        f"e_effect-exam_block-{value:04d}",
        EFFECT_BLOCK,
        value,
        0,
    )


_VERSION_SPECS = tuple(
    _VersionSpec(
        CARD_ACT186,
        "せのびの魔法",
        upgrade,
        CATEGORY_ACTIVE,
        0,
        2 if upgrade == 3 else 3,
        True,
        _ACT186_GROUPS,
        (_ACT186_STATUS, _lesson_timer(1, value)),
    )
    for upgrade, value in enumerate(_ACT186_VALUES)
) + tuple(
    _VersionSpec(
        CARD_IDO103,
        "広がり続ける世界",
        upgrade,
        CATEGORY_ACTIVE,
        0,
        3,
        False,
        _IDO103_130_GROUPS,
        (
            _plain_handoff(
                0,
                f"e_effect-exam_card_play_aggressive-{aggressive:04d}",
                EFFECT_AGGRESSIVE,
                aggressive,
                (AGGRESSIVE_GROUP,),
            ),
            _block_timer(1, block),
            _ido103_status(2, suffix, status_value),
        ),
    )
    for upgrade, (aggressive, block, (suffix, status_value)) in enumerate(
        zip(_IDO103_AGGRESSIVE, _IDO103_BLOCK, _IDO103_STATUS, strict=True)
    )
) + tuple(
    _VersionSpec(
        CARD_IDO127,
        "わたしらしい色",
        upgrade,
        CATEGORY_MENTAL,
        1 if upgrade == 3 else 2,
        0,
        False,
        _IDO127_GROUPS,
        (
            _plain_handoff(
                0,
                "e_effect-exam_aggressive_value_multiple-0500",
                EFFECT_AGGRESSIVE_MULTIPLE,
                500,
                (AGGRESSIVE_GROUP,),
            ),
            _plain_handoff(
                1,
                f"e_effect-exam_debuff_recover-{1 if upgrade == 0 else 2:04d}",
                EFFECT_DEBUFF_RECOVER,
                1 if upgrade == 0 else 2,
                (),
            ),
            _block_timer(2, block),
        ),
        (GAP_AGGRESSIVE_MULTIPLE, GAP_DEBUFF_RECOVER),
    )
    for upgrade, block in enumerate(_IDO127_BLOCK)
) + tuple(
    _VersionSpec(
        CARD_IDO130,
        "憧れのアイドル",
        upgrade,
        CATEGORY_ACTIVE,
        0,
        2,
        False,
        _IDO103_130_GROUPS,
        (
            _plain_handoff(
                0,
                f"e_effect-exam_card_play_aggressive-{aggressive:04d}",
                EFFECT_AGGRESSIVE,
                aggressive,
                (AGGRESSIVE_GROUP,),
            ),
            _block_timer(1, block),
            _ido130_status(2, suffix, status_value),
        ),
        (GAP_STATUS_CHANGE_30_BLOCK,),
    )
    for upgrade, (aggressive, block, (suffix, status_value)) in enumerate(
        zip(_IDO130_AGGRESSIVE, _IDO130_BLOCK, _IDO130_STATUS, strict=True)
    )
)

_SPEC_BY_REF = {(spec.card_id, spec.upgrade): spec for spec in _VERSION_SPECS}
_TIMER_SPEC_BY_ID = {
    slot.effect_id: slot
    for spec in _VERSION_SPECS
    for slot in spec.slots
    if isinstance(slot, _TimerSpec)
}

TARGET_VERSION_REFS = tuple(spec.ref for spec in _VERSION_SPECS)
DIRECT_VERSION_REFS = tuple(
    spec.ref for spec in _VERSION_SPECS if not spec.remaining_blockers
)
CO_BLOCKED_VERSION_REFS = tuple(
    spec.ref for spec in _VERSION_SPECS if spec.remaining_blockers
)
TARGET_TIMER_EFFECT_IDS = frozenset(_TIMER_SPEC_BY_ID)


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockBatchAccounting:
    affected: int = 16
    direct: int = 8
    co_blocked: int = 8
    expected_delta: int = 8

    def __post_init__(self) -> None:
        if self.affected != self.direct + self.co_blocked:
            raise ValueError("batch accounting does not balance")


BATCH_ACCOUNTING = Plan2TimerBlockBatchAccounting()


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockChildContract:
    effect: Plan3Effect
    child_kind: Literal["Block", "LessonDependBlock"]
    value1: int
    count: int
    effect_group_ids: tuple[str, ...]

    @property
    def effect_id(self) -> str:
        return self.effect.id


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockTimerContract:
    base: Plan2ExamEffectTimerContract
    slot_index: int
    child: Plan2TimerBlockChildContract
    timer_effect_group_ids: tuple[str, ...]

    @property
    def effect(self) -> Plan3Effect:
        return self.base.effect

    @property
    def effect_id(self) -> str:
        return self.base.effect_id

    @property
    def delay(self) -> int:
        return self.base.delay


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockHandoffSlot:
    slot_index: int
    effect: Plan3Effect
    remaining_blockers: tuple[str, ...] = ()

    @property
    def effect_id(self) -> str:
        return self.effect.id


Plan2TimerBlockSlot: TypeAlias = (
    Plan2TimerBlockHandoffSlot | Plan2TimerBlockTimerContract
)


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockCardVersion:
    card: Plan3Card
    ordered_slots: tuple[Plan2TimerBlockSlot, ...]
    timer: Plan2TimerBlockTimerContract
    remaining_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.card, Plan3Card):
            raise TypeError("card must be Plan3Card")
        spec = _SPEC_BY_REF.get((self.card.id, self.card.upgrade))
        slots = tuple(self.ordered_slots)
        if spec is None or not (
            self.card.name == spec.name
            and self.card.plan_type == PLAN2
            and self.card.category == spec.category
            and self.card.stamina_cost == spec.stamina
            and self.card.force_stamina_cost == spec.force_stamina
            and self.card.cost_type == COST_STAMINA
            and self.card.cost_value == 0
            and self.card.play_trigger is None
            and self.card.move_position_type == MOVE_LOST
            and self.card.effect_group_ids == spec.effect_groups
            and tuple(slot.effect_id for slot in slots)
            == tuple(slot.effect_id for slot in spec.slots)
            and tuple(slot.slot_index for slot in slots)
            == tuple(slot.slot_index for slot in spec.slots)
            and self.remaining_blockers == spec.remaining_blockers
            and sum(
                isinstance(slot, Plan2TimerBlockTimerContract)
                for slot in slots
            )
            == 1
            and self.timer in slots
        ):
            raise Plan2TimerBlockChainsContractError(
                "runtime-card-contract-shape-drift",
                f"{self.card.id}+{self.card.upgrade}",
            )
        object.__setattr__(self, "ordered_slots", slots)

    @property
    def ref(self) -> str:
        return f"{self.card.id}+{self.card.upgrade}"

    @property
    def coverage(self) -> str:
        return "direct" if not self.remaining_blockers else "co-blocked"

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.ordered_slots)


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockCatalog:
    versions: tuple[Plan2TimerBlockCardVersion, ...]

    def version(self, card_id: str, upgrade: int) -> Plan2TimerBlockCardVersion:
        for version in self.versions:
            if version.card.id == card_id and version.card.upgrade == upgrade:
                return version
        raise Plan2TimerBlockChainsContractError(
            "unintegrated-card-version", f"{card_id}+{upgrade}"
        )

    @property
    def direct_refs(self) -> tuple[str, ...]:
        return tuple(row.ref for row in self.versions if row.coverage == "direct")

    @property
    def co_blocked_refs(self) -> tuple[str, ...]:
        return tuple(
            row.ref for row in self.versions if row.coverage == "co-blocked"
        )


def _validate_raw_effect(
    connection: sqlite3.Connection,
    effect: Plan3Effect,
    *,
    effect_groups: tuple[str, ...],
    pick_count_min: int = 0,
    pick_count_max: int = 0,
) -> None:
    row = connection.execute(
        "SELECT raw_json FROM effect WHERE id = ?", (effect.id,)
    ).fetchone()
    if row is None:
        raise Plan2TimerBlockChainsContractError(
            "master-effect-missing", effect.id
        )
    raw = _json_object(row[0], effect.id)
    for key, expected in (
        ("id", effect.id),
        ("effectType", effect.effect_type),
        ("effectValue1", effect.value1),
        ("effectValue2", effect.value2),
        ("effectCount", effect.effect_count),
        ("effectTurn", effect.effect_turn),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", EFFECT_TYPE_UNKNOWN),
        ("produceCardSearchId", ""),
        ("movePositionType", MOVE_UNKNOWN),
        ("pickRangeType", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", "ProducePickCountType_Unknown"),
        ("pickCountMin", pick_count_min),
        ("pickCountMax", pick_count_max),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", "ProducePickCountType_Unknown"),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", effect.chain_effect_id),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", effect.status_enchant_id),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
        ("effectGroupIds", list(effect_groups)),
    ):
        _require(raw, key, expected, effect.id)


def _plain_effect_matches(effect: Plan3Effect, spec: _HandoffSpec) -> bool:
    return (
        effect.id == spec.effect_id
        and effect.effect_type == spec.effect_type
        and effect.value1 == spec.value1
        and effect.value2 == spec.value2
        and effect.effect_count == spec.effect_count
        and effect.effect_turn == spec.effect_turn
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
        and effect.trigger is None
        and not effect.once
        and effect.card_move_rule is None
    )


def _validate_handoff(
    effect: Plan3Effect,
    spec: _HandoffSpec,
    connection: sqlite3.Connection,
    blockers: tuple[str, ...],
) -> Plan2TimerBlockHandoffSlot:
    if not _plain_effect_matches(effect, spec):
        raise Plan2TimerBlockChainsContractError(
            "handoff-effect-shape-drift", spec.effect_id
        )
    nested = spec.nested_status
    if nested is None:
        if effect.status_enchant_id or effect.status_enchant is not None:
            raise Plan2TimerBlockChainsContractError(
                "handoff-unexpected-status", spec.effect_id
            )
    else:
        status = effect.status_enchant
        child = None if status is None or len(status.effects) != 1 else status.effects[0]
        if not (
            effect.status_enchant_id == nested.status_id
            and status is not None
            and status.id == nested.status_id
            and status.trigger == nested.trigger
            and child is not None
            and child.id == nested.child_effect_id
            and child.effect_type == nested.child_effect_type
            and child.value1 == nested.child_value1
            and child.value2 == 0
            and child.effect_count == nested.child_count
            and child.effect_turn == 0
            and not child.status_enchant_id
            and child.status_enchant is None
            and not child.chain_effect_id
            and not child.chain_effect_ids
            and child.chain_effect is None
            and child.trigger is None
            and not child.once
            and child.card_move_rule is None
        ):
            raise Plan2TimerBlockChainsContractError(
                "handoff-status-shape-drift", spec.effect_id
            )
        child_groups = (
            BLOCK_CHILD_GROUPS
            if nested.child_effect_type == EFFECT_BLOCK
            else LESSON_CHILD_GROUPS
        )
        _validate_raw_effect(connection, child, effect_groups=child_groups)
    _validate_raw_effect(
        connection,
        effect,
        effect_groups=spec.effect_groups,
        pick_count_min=spec.pick_count_min,
        pick_count_max=spec.pick_count_max,
    )
    slot_blockers = tuple(
        gap
        for gap in blockers
        if (
            (spec.effect_type == EFFECT_AGGRESSIVE_MULTIPLE and gap == GAP_AGGRESSIVE_MULTIPLE)
            or (spec.effect_type == EFFECT_DEBUFF_RECOVER and gap == GAP_DEBUFF_RECOVER)
            or (
                nested is not None
                and nested.trigger.id
                == "e_trigger-exam_status_change-30-exam_block"
                and gap == GAP_STATUS_CHANGE_30_BLOCK
            )
        )
    )
    return Plan2TimerBlockHandoffSlot(spec.slot_index, effect, slot_blockers)


def _validate_timer(
    effect: Plan3Effect,
    spec: _TimerSpec,
    connection: sqlite3.Connection,
) -> Plan2TimerBlockTimerContract:
    child = effect.chain_effect
    if not (
        effect.id == spec.effect_id
        and effect.effect_type == EFFECT_TIMER
        and effect.value1 == spec.delay
        and effect.value2 == 0
        and effect.effect_count == 1
        and effect.effect_turn == 0
        and not effect.status_enchant_id
        and effect.status_enchant is None
        and effect.chain_effect_id == spec.child_effect_id
        and not effect.chain_effect_ids
        and child is not None
        and child.id == spec.child_effect_id
        and child.effect_type == spec.child_effect_type
        and child.value1 == spec.child_value1
        and child.value2 == 0
        and child.effect_count == spec.child_count
        and child.effect_turn == 0
        and not child.status_enchant_id
        and child.status_enchant is None
        and not child.chain_effect_id
        and not child.chain_effect_ids
        and child.chain_effect is None
        and child.trigger is None
        and not child.once
        and child.card_move_rule is None
        and effect.trigger is None
        and not effect.once
        and effect.card_move_rule is None
    ):
        raise Plan2TimerBlockChainsContractError(
            "timer-child-shape-drift", spec.effect_id
        )
    _validate_raw_effect(connection, effect, effect_groups=spec.timer_groups)
    _validate_raw_effect(connection, child, effect_groups=spec.child_groups)
    kind: Literal["Block", "LessonDependBlock"] = (
        "LessonDependBlock"
        if spec.child_effect_type == EFFECT_LESSON_DEPEND_BLOCK
        else "Block"
    )
    child_contract = Plan2TimerBlockChildContract(
        child, kind, spec.child_value1, spec.child_count, spec.child_groups
    )
    base = Plan2ExamEffectTimerContract(
        effect,
        spec.delay,
        spec.child_effect_id,
        spec.child_effect_type,
        spec.child_value1,
    )
    return Plan2TimerBlockTimerContract(
        base, spec.slot_index, child_contract, spec.timer_groups
    )


def _validate_card_raw(
    connection: sqlite3.Connection, card: Plan3Card, spec: _VersionSpec
) -> None:
    row = connection.execute(
        "SELECT raw_json, play_effects_json FROM card "
        "WHERE id = ? AND upgrade_count = ?",
        (card.id, card.upgrade),
    ).fetchone()
    if row is None:
        raise Plan2TimerBlockChainsContractError(
            "master-card-missing", spec.ref
        )
    raw = _json_object(row[0], spec.ref)
    for key, expected in (
        ("id", spec.card_id),
        ("upgradeCount", spec.upgrade),
        ("name", spec.name),
        ("planType", PLAN2),
        ("category", spec.category),
        ("stamina", spec.stamina),
        ("forceStamina", spec.force_stamina),
        ("costType", COST_STAMINA),
        ("costValue", 0),
        ("playProduceExamTriggerId", ""),
        ("playMovePositionType", MOVE_LOST),
        ("moveEffectTriggerType", "ProduceCardMoveEffectTriggerType_Unknown"),
        ("moveProduceExamEffectIds", []),
        ("moveProduceExamTriggerIds", []),
        ("produceCardStatusEnchantId", ""),
        ("produceCardCustomizeIds", list(spec.customize_ids)),
        ("maxCustomizeCount", spec.max_customize_count),
        ("isInitial", False),
        ("isRestrict", False),
        ("isEndTurnLost", False),
        ("isConversion", spec.is_conversion),
        ("isInitialDeckProduceCard", False),
        ("isLimited", False),
        ("isReward", False),
        ("libraryHidden", False),
        ("noDeckDuplication", True),
        ("effectGroupIds", list(spec.effect_groups)),
    ):
        _require(raw, key, expected, spec.ref)
    try:
        refs = json.loads(str(row[1]))
    except json.JSONDecodeError as error:
        raise Plan2TimerBlockChainsContractError(
            "invalid-play-effects-json", spec.ref
        ) from error
    if not isinstance(refs, list) or len(refs) != len(spec.slots):
        raise Plan2TimerBlockChainsContractError(
            "ordered-play-slot-count-drift", spec.ref
        )
    for index, (ref, slot) in enumerate(zip(refs, spec.slots, strict=True)):
        if not isinstance(ref, dict) or ref != {
            "produceExamTriggerId": "",
            "produceExamEffectId": slot.effect_id,
            "hideIcon": False,
            "isOncePlayEffect": False,
        }:
            raise Plan2TimerBlockChainsContractError(
                "ordered-play-slot-drift", f"{spec.ref}:slot-{index}"
            )


def validate_plan2_timer_block_card(
    card: Plan3Card,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2TimerBlockCardVersion:
    if not isinstance(card, Plan3Card):
        raise TypeError("card must be Plan3Card")
    spec = _SPEC_BY_REF.get((card.id, card.upgrade))
    if spec is None:
        raise Plan2TimerBlockChainsContractError(
            "unintegrated-card-version", f"{card.id}+{card.upgrade}"
        )
    if not (
        card.name == spec.name
        and card.plan_type == PLAN2
        and card.category == spec.category
        and card.stamina_cost == spec.stamina
        and card.force_stamina_cost == spec.force_stamina
        and card.cost_type == COST_STAMINA
        and card.cost_value == 0
        and card.play_trigger is None
        and card.move_position_type == MOVE_LOST
        and card.effect_group_ids == spec.effect_groups
        and tuple(effect.id for effect in card.effects)
        == tuple(slot.effect_id for slot in spec.slots)
    ):
        raise Plan2TimerBlockChainsContractError(
            "card-shape-drift", spec.ref
        )
    with closing(sqlite3.connect(database)) as connection:
        _validate_card_raw(connection, card, spec)
        slots: list[Plan2TimerBlockSlot] = []
        timers: list[Plan2TimerBlockTimerContract] = []
        for effect, slot_spec in zip(card.effects, spec.slots, strict=True):
            if isinstance(slot_spec, _TimerSpec):
                timer = _validate_timer(effect, slot_spec, connection)
                slots.append(timer)
                timers.append(timer)
            else:
                slots.append(
                    _validate_handoff(
                        effect,
                        slot_spec,
                        connection,
                        spec.remaining_blockers,
                    )
                )
    if len(timers) != 1:
        raise Plan2TimerBlockChainsContractError(
            "card-must-have-one-target-timer", spec.ref
        )
    return Plan2TimerBlockCardVersion(
        card, tuple(slots), timers[0], spec.remaining_blockers
    )


def load_plan2_timer_block_catalog(
    *, database: Path = DEFAULT_DATABASE
) -> Plan2TimerBlockCatalog:
    versions = tuple(
        validate_plan2_timer_block_card(
            load_plan3_card(spec.card_id, spec.upgrade, database),
            database=database,
        )
        for spec in _VERSION_SPECS
    )
    catalog = Plan2TimerBlockCatalog(versions)
    if (
        tuple(row.ref for row in versions) != TARGET_VERSION_REFS
        or catalog.direct_refs != DIRECT_VERSION_REFS
        or catalog.co_blocked_refs != CO_BLOCKED_VERSION_REFS
        or (len(versions), len(catalog.direct_refs), len(catalog.co_blocked_refs))
        != (BATCH_ACCOUNTING.affected, BATCH_ACCOUNTING.direct, BATCH_ACCOUNTING.co_blocked)
    ):
        raise Plan2TimerBlockChainsContractError("coverage-accounting-drift")
    return catalog


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockQueuedTimer:
    instance_id: str
    source_guid: str
    card_ref: str
    timer: Plan2TimerBlockTimerContract

    def __post_init__(self) -> None:
        for label, value in (
            ("instance_id", self.instance_id),
            ("source_guid", self.source_guid),
            ("card_ref", self.card_ref),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be non-empty text")
        if not isinstance(self.timer, Plan2TimerBlockTimerContract):
            raise TypeError("timer must be Plan2TimerBlockTimerContract")
        spec = _TIMER_SPEC_BY_ID.get(self.timer.effect_id)
        version_spec = next(
            (row for row in _VERSION_SPECS if row.ref == self.card_ref), None
        )
        if spec is None or not (
            version_spec is not None
            and any(
                isinstance(slot, _TimerSpec)
                and slot.effect_id == self.timer.effect_id
                for slot in version_spec.slots
            )
            and
            self.timer.slot_index == spec.slot_index
            and self.timer.delay == spec.delay
            and self.timer.child.effect_id == spec.child_effect_id
            and self.timer.child.value1 == spec.child_value1
            and self.timer.child.count == spec.child_count
        ):
            raise Plan2TimerBlockChainsContractError(
                "queued-timer-outside-exact-catalog", self.timer.effect_id
            )


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockRuntime:
    scheduler: Plan3State
    block_runtime: Plan2BlockDependConsumptionSumRuntime
    application_status: ParameterApplicationStatus
    queue: tuple[Plan2TimerBlockQueuedTimer, ...] = ()
    completed_start_boundaries: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scheduler, Plan3State):
            raise TypeError("scheduler must be Plan3State")
        if not isinstance(
            self.block_runtime, Plan2BlockDependConsumptionSumRuntime
        ):
            raise TypeError(
                "block_runtime must be Plan2BlockDependConsumptionSumRuntime"
            )
        if not isinstance(self.application_status, ParameterApplicationStatus):
            raise TypeError("application_status must be ParameterApplicationStatus")
        if self.scheduler.score != self.application_status.judge_parameter:
            raise Plan2TimerBlockChainsContractError("score-projections-differ")
        if (
            isinstance(self.completed_start_boundaries, bool)
            or not isinstance(self.completed_start_boundaries, int)
            or self.completed_start_boundaries < 0
        ):
            raise ValueError(
                "completed_start_boundaries must be a non-negative integer"
            )
        if self.scheduler.awaiting_turn_start:
            raise Plan2TimerBlockChainsContractError(
                "standalone-node-cannot-pause-mid-boundary"
            )
        if self.scheduler.plays_remaining != 0:
            raise Plan2TimerBlockChainsContractError(
                "standalone-node-must-be-post-play"
            )
        queue = tuple(self.queue)
        if any(not isinstance(item, Plan2TimerBlockQueuedTimer) for item in queue):
            raise TypeError("queue must contain Plan2TimerBlockQueuedTimer")
        active_ids = tuple(
            enchant.instance_id for enchant in self.scheduler.active_status_enchants
        )
        if active_ids != tuple(item.instance_id for item in queue):
            raise Plan2TimerBlockChainsContractError(
                "queue-identity-projections-differ"
            )
        if len(active_ids) != len(set(active_ids)):
            raise Plan2TimerBlockChainsContractError(
                "duplicate-queue-instance-identity"
            )
        for item, enchant in zip(
            queue, self.scheduler.active_status_enchants, strict=True
        ):
            if not (
                enchant.source_id == item.timer.effect_id
                and enchant.rule.id == f"effect-timer:{item.timer.effect_id}"
                and enchant.rule.trigger.phase_types == (PHASE_TURN_TIMER,)
                and enchant.rule.trigger.phase_values == (item.timer.delay,)
                and enchant.rule.effects == (item.timer.child.effect,)
                and enchant.max_uses == 1
                and enchant.uses == 0
                and enchant.max_uses_per_turn == 0
                and enchant.uses_this_turn == 0
                and enchant.remaining_turns == -1
                and not enchant.is_item_direct
                and 0 <= enchant.turn_count <= INT32_MAX
            ):
                raise Plan2TimerBlockChainsContractError(
                    "queue-listener-shape-drift", item.instance_id
                )
        object.__setattr__(self, "queue", queue)

    @property
    def terminal(self) -> bool:
        return self.scheduler.turns_remaining == 0

    @property
    def queue_instance_ids(self) -> tuple[str, ...]:
        return tuple(item.instance_id for item in self.queue)


def create_plan2_timer_block_runtime(
    *,
    turns_remaining: int,
    block_runtime: Plan2BlockDependConsumptionSumRuntime | None = None,
    application_status: ParameterApplicationStatus | None = None,
) -> Plan2TimerBlockRuntime:
    turns = _plain_i32(turns_remaining, "turns_remaining")
    if turns <= 0:
        raise ValueError("turns_remaining must be positive")
    block = (
        Plan2BlockDependConsumptionSumRuntime()
        if block_runtime is None
        else block_runtime
    )
    application = (
        ParameterApplicationStatus(judge_parameter=0)
        if application_status is None
        else application_status
    )
    if not isinstance(block, Plan2BlockDependConsumptionSumRuntime):
        raise TypeError("block_runtime has the wrong type")
    if not isinstance(application, ParameterApplicationStatus):
        raise TypeError("application_status has the wrong type")
    scheduler = Plan3State(
        turns_remaining=turns,
        stamina=0,
        max_stamina=0,
        score=application.judge_parameter,
        plays_remaining=0,
        lesson_type=LESSON_GENERAL,
    )
    return Plan2TimerBlockRuntime(scheduler, block, application)


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockEnqueue:
    before: Plan2TimerBlockRuntime
    after: Plan2TimerBlockRuntime
    version: Plan2TimerBlockCardVersion
    play_origin: str
    source_guid: str
    queued: Plan2TimerBlockQueuedTimer
    ordered_handoff_effect_ids: tuple[str, ...]
    remaining_blockers: tuple[str, ...]
    event_trace: tuple[str, ...]


def enqueue_plan2_timer_block_card(
    runtime: Plan2TimerBlockRuntime,
    version: Plan2TimerBlockCardVersion,
    *,
    source_guid: str,
    play_origin: str = "ordinary",
) -> Plan2TimerBlockEnqueue:
    """Install the one exact timer while preserving every card-slot handoff."""

    if not isinstance(runtime, Plan2TimerBlockRuntime):
        raise TypeError("runtime must be Plan2TimerBlockRuntime")
    if not isinstance(version, Plan2TimerBlockCardVersion):
        raise TypeError("version must be Plan2TimerBlockCardVersion")
    if not isinstance(source_guid, str) or not source_guid:
        raise ValueError("source_guid must be non-empty text")
    if play_origin not in PLAY_ORIGINS:
        raise Plan2TimerBlockChainsContractError(
            "unsupported-play-origin", str(play_origin)
        )
    if runtime.terminal:
        raise Plan2TimerBlockChainsContractError(
            "timer-enqueue-outside-active-turn"
        )
    working = runtime.scheduler
    queue = list(runtime.queue)
    handoffs: list[str] = []
    trace = [f"accepted-{play_origin}-play:{version.ref}:{source_guid}"]
    queued: Plan2TimerBlockQueuedTimer | None = None
    for slot in version.ordered_slots:
        if isinstance(slot, Plan2TimerBlockHandoffSlot):
            handoffs.append(slot.effect_id)
            trace.append(
                f"card-slot:{slot.slot_index}:handoff:{slot.effect_id}"
            )
            trace.extend(
                f"card-slot:{slot.slot_index}:co-blocked:{gap}"
                for gap in slot.remaining_blockers
            )
            continue
        trace.append(f"card-slot:{slot.slot_index}:timer:{slot.effect_id}")
        working = _install_effect_timer(
            working,
            slot.effect,
            source_id=f"{source_guid}:slot-{slot.slot_index}",
        )
        installed = working.active_status_enchants[-1]
        queued = Plan2TimerBlockQueuedTimer(
            installed.instance_id, source_guid, version.ref, slot
        )
        queue.append(queued)
        trace.append(
            f"timer-enqueued:{installed.instance_id}:delay={slot.delay}"
        )
    if queued is None:
        raise Plan2TimerBlockChainsContractError(
            "card-has-no-target-timer", version.ref
        )
    after = Plan2TimerBlockRuntime(
        working,
        runtime.block_runtime,
        runtime.application_status,
        tuple(queue),
        runtime.completed_start_boundaries,
    )
    return Plan2TimerBlockEnqueue(
        runtime,
        after,
        version,
        play_origin,
        source_guid,
        queued,
        tuple(handoffs),
        version.remaining_blockers,
        tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class Plan2TimerPlainBlockExecution:
    queue_instance_id: str
    timer_effect_id: str
    child_effect_id: str
    requested_value: int
    calculated_value: int
    block_fix_value: int
    set_block: Plan2SetBlockTransition
    differences: tuple[Plan2BlockDependDifference, ...]
    event_trace: tuple[str, ...]

    @property
    def block_before(self) -> int:
        return self.set_block.before.block

    @property
    def block_after(self) -> int:
        return self.set_block.after.block


@dataclass(frozen=True, slots=True)
class Plan2TimerLessonDependBlockHit:
    hit_index: int
    block_snapshot: int
    raw_value: int
    calculated_value: int
    application: ParameterApplication


@dataclass(frozen=True, slots=True)
class Plan2TimerLessonDependBlockExecution:
    queue_instance_id: str
    timer_effect_id: str
    child_effect_id: str
    value_permille: int
    value2: int
    hits: tuple[Plan2TimerLessonDependBlockHit, ...]
    value2_target_block: int
    set_block: Plan2SetBlockTransition | None
    block_differences: tuple[Plan2BlockDependDifference, ...]
    event_trace: tuple[str, ...]

    @property
    def block_snapshot(self) -> int:
        return self.hits[0].block_snapshot if self.hits else 0


Plan2TimerBlockChildExecution: TypeAlias = (
    Plan2TimerPlainBlockExecution | Plan2TimerLessonDependBlockExecution
)


def _execute_plain_block_child(
    queued: Plan2TimerBlockQueuedTimer,
    block_runtime: Plan2BlockDependConsumptionSumRuntime,
) -> tuple[Plan2TimerPlainBlockExecution, Plan2BlockDependConsumptionSumRuntime]:
    child = queued.timer.child
    if child.child_kind != "Block" or not (
        child.effect.effect_type == EFFECT_BLOCK
        and child.effect.value1 == child.value1
        and child.effect.value2 == 0
        and child.effect.effect_count == 0
        and child.effect.effect_turn == 0
    ):
        raise Plan2TimerBlockChainsContractError(
            "runtime-block-child-shape-drift", child.effect_id
        )
    requested = _plain_i32(child.value1, "Block child value")
    calculated = calculate_add_block(
        requested,
        is_buff_active=True,
        status=block_runtime.add_block_status,
        settings=block_runtime.add_block_settings,
        additional_multiple_aggressive_rate=1.0,
    )
    block_fix = max(_block_i32(-block_runtime.block), calculated)
    block_after = _block_i32(block_runtime.block + block_fix)
    set_block = apply_plan2_set_block(
        block_runtime, block_after, is_consumption=False
    )
    block_difference = Plan2BlockDependDifference(
        kind="block",
        preview=block_runtime.block,
        current=set_block.after.block,
        block_consumption_sum_count=block_runtime.block_consumption_sum_count,
        status_effect_type=None,
    )
    effect_difference = Plan2BlockDependDifference(
        kind="effect",
        preview=block_runtime.block,
        current=set_block.after.block,
        block_consumption_sum_count=None,
        status_effect_type=BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE,
    )
    execution = Plan2TimerPlainBlockExecution(
        queued.instance_id,
        queued.timer.effect_id,
        child.effect_id,
        requested,
        calculated,
        block_fix,
        set_block,
        (block_difference, effect_difference),
        (
            "BlockExecutor:snapshot-signed-Block",
            "CalculateAddBlock(isBuffActive=true,rate=1.0)",
            "AddBlockFix:append-block-difference",
            "AddBlockFix:SetBlock(isConsumption=false)",
            "BlockExecutor:append-effect-difference(statusEffectType=3)",
            "ExamSequence:EffectDifferenceExecutedAsync(deferred)",
        ),
    )
    return execution, set_block.after


def _updated_application_status(
    status: ParameterApplicationStatus, application: ParameterApplication
) -> ParameterApplicationStatus:
    return replace(
        status,
        judge_parameter=application.after,
        current_turn_total_add_parameter=(
            application.current_turn_total_add_parameter
        ),
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )


def _execute_lesson_depend_block_child(
    queued: Plan2TimerBlockQueuedTimer,
    block_runtime: Plan2BlockDependConsumptionSumRuntime,
    application_status: ParameterApplicationStatus,
    turn_input: "Plan2TimerBlockTurnInput",
) -> tuple[
    Plan2TimerLessonDependBlockExecution,
    ParameterApplicationStatus,
    Plan2BlockDependConsumptionSumRuntime,
]:
    child = queued.timer.child
    effect = child.effect
    if child.child_kind != "LessonDependBlock" or not (
        effect.effect_type == EFFECT_LESSON_DEPEND_BLOCK
        and effect.value1 == child.value1
        and effect.value2 == 0
        and effect.effect_count == child.count == 1
        and effect.effect_turn == 0
    ):
        raise Plan2TimerBlockChainsContractError(
            "runtime-lesson-child-shape-drift", child.effect_id
        )
    block_snapshot = _plain_i32(block_runtime.block, "Block snapshot")
    raw = _lesson_depend_block_raw_score(block_snapshot, child.value1)
    current = application_status
    hits: list[Plan2TimerLessonDependBlockHit] = []
    trace = [
        f"LessonDependBlock:snapshot-signed-Block:{block_snapshot}",
        "LessonDependBlock:f32(value1/1000)*f32(Block)+negative-epsilon:ceil",
    ]
    for index in range(child.count):
        calculated = calculate_adding_parameter(
            raw,
            is_buff_active=turn_input.lesson_is_buff_active,
            status=turn_input.adding_status,
            settings=turn_input.adding_settings,
            additional=turn_input.additional,
        )
        application = apply_parameter_add(calculated, status=current)
        current = _updated_application_status(current, application)
        hits.append(
            Plan2TimerLessonDependBlockHit(
                index, block_snapshot, raw, calculated, application
            )
        )
        trace.append(
            f"LessonDependBlock:hit:{index}:raw={raw}:"
            f"calculated={calculated}:actual={application.actual_parameter}"
        )
    # Native always evaluates the value2 retention path after all score hits.
    # Even value2=0 can change an Int32 outside binary32's exact integer range.
    # The comparison is original signed Block versus ceil_f32((1-ratio)*Block).
    retention = f32(f32(1.0) - permille_to_f32(effect.value2))
    target_block = ceil_f32_to_i32(
        f32(retention * f32(block_runtime.block))
    )
    set_block: Plan2SetBlockTransition | None = None
    block_differences: tuple[Plan2BlockDependDifference, ...] = ()
    after_block = block_runtime
    trace.append(
        f"LessonDependBlock:value2-retention:{effect.value2}:"
        f"target-Block={target_block}"
    )
    if target_block != block_runtime.block:
        set_block = apply_plan2_set_block(
            block_runtime, target_block, is_consumption=True
        )
        block_differences = (
            Plan2BlockDependDifference(
                kind="block",
                preview=block_runtime.block,
                current=target_block,
                block_consumption_sum_count=(
                    block_runtime.block_consumption_sum_count
                ),
                status_effect_type=None,
                is_consumption=True,
            ),
        )
        after_block = set_block.after
        trace.extend(
            (
                "LessonDependBlock:append-block-difference",
                "LessonDependBlock:SetBlock(isConsumption=true)",
            )
        )
    else:
        trace.append("LessonDependBlock:value2-retention:no-block-change")
    trace.append("ExamSequence:EffectDifferenceExecutedAsync(deferred)")
    return (
        Plan2TimerLessonDependBlockExecution(
            queued.instance_id,
            queued.timer.effect_id,
            child.effect_id,
            child.value1,
            effect.value2,
            tuple(hits),
            target_block,
            set_block,
            block_differences,
            tuple(trace),
        ),
        current,
        after_block,
    )


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockTurnInput:
    adding_status: AddingParameterStatus = AddingParameterStatus()
    adding_settings: AddingParameterSettings = AddingParameterSettings()
    additional: AddingParameterAdditionalData | None = None
    lesson_is_buff_active: bool = True
    extra_turns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.adding_status, AddingParameterStatus):
            raise TypeError("adding_status must be AddingParameterStatus")
        if not isinstance(self.adding_settings, AddingParameterSettings):
            raise TypeError("adding_settings must be AddingParameterSettings")
        if self.additional is not None and not isinstance(
            self.additional, AddingParameterAdditionalData
        ):
            raise TypeError("additional must be AddingParameterAdditionalData")
        if type(self.lesson_is_buff_active) is not bool:
            raise TypeError("lesson_is_buff_active must be boolean")
        extra = _plain_i32(self.extra_turns, "extra_turns")
        if extra < 0:
            raise ValueError("extra_turns must be non-negative")


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockTurnTransition:
    before: Plan2TimerBlockRuntime
    after: Plan2TimerBlockRuntime
    turn_input: Plan2TimerBlockTurnInput
    due_instance_ids: tuple[str, ...]
    executions: tuple[Plan2TimerBlockChildExecution, ...]
    terminal: bool
    terminal_stranded_instance_ids: tuple[str, ...]
    phase_trace: tuple[str, ...]


def advance_plan2_timer_block_turn(
    runtime: Plan2TimerBlockRuntime,
    turn_input: Plan2TimerBlockTurnInput = Plan2TimerBlockTurnInput(),
) -> Plan2TimerBlockTurnTransition:
    """Close one turn and resolve the next legal relative-timer boundary."""

    if not isinstance(runtime, Plan2TimerBlockRuntime):
        raise TypeError("runtime must be Plan2TimerBlockRuntime")
    if not isinstance(turn_input, Plan2TimerBlockTurnInput):
        raise TypeError("turn_input must be Plan2TimerBlockTurnInput")
    if runtime.terminal:
        raise Plan2TimerBlockChainsContractError("exam-already-terminal")
    scheduler = runtime.scheduler
    if turn_input.extra_turns:
        if (
            scheduler.turns_remaining + turn_input.extra_turns > INT32_MAX
            or scheduler.extra_turn + turn_input.extra_turns > INT32_MAX
        ):
            raise Plan2TimerBlockChainsContractError(
                "signed-int32-overflow", "extra-turn"
            )
        scheduler = replace(
            scheduler,
            turns_remaining=scheduler.turns_remaining + turn_input.extra_turns,
            extra_turn=scheduler.extra_turn + turn_input.extra_turns,
        )
    # Reuse the same Plan3 relative timer representation as the earlier
    # Plan2 timer adapter.  Detach only these Plan2-only child types while the
    # common EndTurn boundary is spent, then restore the identical listeners.
    timer_enchants = scheduler.active_status_enchants
    ended = end_plan3_turn(replace(scheduler, active_status_enchants=()))
    ended = replace(ended, active_status_enchants=timer_enchants)
    reset_application = replace(
        runtime.application_status, current_turn_total_add_parameter=0
    )
    trace = ["phase:ExamEndTurn"]
    if ended.turns_remaining == 0:
        after = Plan2TimerBlockRuntime(
            replace(ended, score=reset_application.judge_parameter),
            runtime.block_runtime,
            reset_application,
            runtime.queue,
            runtime.completed_start_boundaries,
        )
        trace.append("terminal:no-draw:no-StartTurn:no-TurnTimer")
        return Plan2TimerBlockTurnTransition(
            runtime,
            after,
            turn_input,
            (),
            (),
            True,
            after.queue_instance_ids,
            tuple(trace),
        )
    if any(
        enchant.turn_count == INT32_MAX
        for enchant in ended.active_status_enchants
    ):
        raise Plan2TimerBlockChainsContractError(
            "signed-int32-overflow", "timer-turn-count"
        )
    advanced = _advance_status_enchants_for_turn_start(ended)
    active_by_id = {
        enchant.instance_id: enchant
        for enchant in advanced.active_status_enchants
    }
    due_ids = tuple(
        item.instance_id
        for item in runtime.queue
        if (
            active_by_id[item.instance_id].rule.trigger.phase_types
            == (PHASE_TURN_TIMER,)
            and active_by_id[item.instance_id].turn_count
            in active_by_id[item.instance_id].rule.trigger.phase_values
        )
    )
    trace.extend(
        (
            "phase:ordinary-draw",
            "phase:ExamStartTurn",
            "phase:StartPlay",
            "phase:ExamTurnTimer:capture-active-order",
        )
    )
    block = runtime.block_runtime
    application = reset_application
    executions: list[Plan2TimerBlockChildExecution] = []
    due_set = frozenset(due_ids)
    for item in runtime.queue:
        if item.instance_id not in due_set:
            continue
        if item.timer.child.child_kind == "Block":
            execution, block = _execute_plain_block_child(item, block)
        else:
            execution, application, block = _execute_lesson_depend_block_child(
                item, block, application, turn_input
            )
        executions.append(execution)
        trace.append(
            f"timer-fired:{item.instance_id}:child={item.timer.child.effect_id}"
        )
        trace.extend(execution.event_trace)
    after_removal = _remove_triggered_turn_timer_statuses(advanced)
    trace.append("phase:RemoveTurnTimerTriggeredStatus")
    after_scheduler = replace(
        after_removal,
        score=application.judge_parameter,
        current_turn_total_add_parameter=(
            application.current_turn_total_add_parameter
        ),
        plays_remaining=0,
        awaiting_turn_start=False,
        active_status_enchants=tuple(
            replace(enchant, passing_turn_start=True)
            for enchant in after_removal.active_status_enchants
        ),
    )
    after_queue = tuple(
        item for item in runtime.queue if item.instance_id not in due_set
    )
    after = Plan2TimerBlockRuntime(
        after_scheduler,
        block,
        application,
        after_queue,
        runtime.completed_start_boundaries + 1,
    )
    return Plan2TimerBlockTurnTransition(
        runtime,
        after,
        turn_input,
        due_ids,
        tuple(executions),
        False,
        (),
        tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class Plan2TimerBlockSearchResult:
    initial: Plan2TimerBlockRuntime
    transitions: tuple[Plan2TimerBlockTurnTransition, ...]
    states: tuple[Plan2TimerBlockRuntime, ...]
    stopped_reason: str


def search_plan2_timer_block_native_horizon(
    initial: Plan2TimerBlockRuntime,
    turn_inputs: Sequence[Plan2TimerBlockTurnInput],
) -> Plan2TimerBlockSearchResult:
    """Carry exact timer identities and counters across immutable nodes."""

    if not isinstance(initial, Plan2TimerBlockRuntime):
        raise TypeError("initial must be Plan2TimerBlockRuntime")
    if not isinstance(turn_inputs, Sequence):
        raise TypeError("turn_inputs must be a sequence")
    current = initial
    transitions: list[Plan2TimerBlockTurnTransition] = []
    states: list[Plan2TimerBlockRuntime] = []
    reason = "input-exhausted"
    for item in turn_inputs:
        if not isinstance(item, Plan2TimerBlockTurnInput):
            raise TypeError(
                "turn_inputs must contain Plan2TimerBlockTurnInput"
            )
        if current.terminal:
            reason = "terminal"
            break
        transition = advance_plan2_timer_block_turn(current, item)
        transitions.append(transition)
        current = transition.after
        states.append(current)
        if transition.terminal:
            reason = "terminal"
            break
    return Plan2TimerBlockSearchResult(
        initial, tuple(transitions), tuple(states), reason
    )


__all__ = [
    "BATCH_ACCOUNTING",
    "CARD_ACT186",
    "CARD_IDO103",
    "CARD_IDO127",
    "CARD_IDO130",
    "CO_BLOCKED_VERSION_REFS",
    "DIRECT_VERSION_REFS",
    "NATIVE_PHASE_ORDER",
    "PLAY_ORIGINS",
    "Plan2TimerBlockBatchAccounting",
    "Plan2TimerBlockCardVersion",
    "Plan2TimerBlockCatalog",
    "Plan2TimerBlockChainsContractError",
    "Plan2TimerBlockChildContract",
    "Plan2TimerBlockChildExecution",
    "Plan2TimerBlockEnqueue",
    "Plan2TimerBlockHandoffSlot",
    "Plan2TimerBlockQueuedTimer",
    "Plan2TimerBlockRuntime",
    "Plan2TimerBlockSearchResult",
    "Plan2TimerBlockTimerContract",
    "Plan2TimerBlockTurnInput",
    "Plan2TimerBlockTurnTransition",
    "Plan2TimerLessonDependBlockExecution",
    "Plan2TimerLessonDependBlockHit",
    "Plan2TimerPlainBlockExecution",
    "TARGET_TIMER_EFFECT_IDS",
    "TARGET_VERSION_REFS",
    "advance_plan2_timer_block_turn",
    "create_plan2_timer_block_runtime",
    "enqueue_plan2_timer_block_card",
    "load_plan2_timer_block_catalog",
    "search_plan2_timer_block_native_horizon",
    "validate_plan2_timer_block_card",
]
