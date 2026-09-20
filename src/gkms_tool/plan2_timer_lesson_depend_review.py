"""Standalone Plan 2 timers whose child scales Lesson from signed Review.

This module deliberately owns only eight exact card versions.  It reuses the
proven Plan 3 relative-turn listener representation and the native float32
formula helpers, while keeping the still-unproven aggressive-up-3 branch on
``p_card-02-sup-3_180`` explicit and co-blocked.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_adding_parameter,
    get_ratio_effect_int_value,
)
from .plan3_engine import (
    CATEGORY_ACTIVE,
    COST_STAMINA,
    EFFECT_STATUS_ENCHANT,
    EFFECT_TIMER,
    LESSON_GENERAL,
    LESSON_UNKNOWN,
    MOVE_LOST,
    MOVE_UNKNOWN,
    PHASE_END_TURN,
    PHASE_NONE,
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
COST_AGGRESSIVE = "ExamCostType_ExamCardPlayAggressive"
EFFECT_REVIEW = "ProduceExamEffectType_ExamReview"
EFFECT_LESSON_DEPEND_REVIEW = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
FIELD_REVIEW_UP = "ProduceExamFieldStatusType_ReviewUp"
FIELD_AGGRESSIVE_UP = "ProduceExamFieldStatusType_CardPlayAggressiveUp"

IDO_CARD_ID = "p_card-02-ido-3_236"
SUPPORT_CARD_ID = "p_card-02-sup-3_180"
DIRECT_REVIEW_EFFECT_ID = "e_effect-exam_review-0002"
AGGRESSIVE_TRIGGER_ID = "e_trigger-none-card_play_aggressive_up-3"

TIMER_EFFECT_GROUPS = (
    "effect_group-visible-exam_lesson_depend_exam_review-000",
    "effect_group-visible-exam_lesson-000",
    "effect_group-visible-exam_effect_timer-000",
)
CHILD_EFFECT_GROUPS = (
    "effect_group-visible-exam_lesson_depend_exam_review-000",
    "effect_group-visible-exam_lesson-000",
)
REVIEW_EFFECT_GROUPS = ("effect_group-visible-exam_review-000",)
SUPPORT_WRAPPER_EFFECT_GROUPS = (
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_review-000",
)
IDO_CARD_EFFECT_GROUPS = (*TIMER_EFFECT_GROUPS, "effect_group-visible-exam_review-000")
SUPPORT_CARD_EFFECT_GROUPS = (
    "effect_group-visible-exam_lesson_depend_exam_review-000",
    "effect_group-visible-exam_lesson-000",
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_effect_timer-000",
    "effect_group-visible-exam_review-000",
)


def _child_id(value_permille: int) -> str:
    return f"e_effect-exam_lesson_depend_exam_review-{value_permille:04d}-01"


def _timer_id(delay: int, value_permille: int) -> str:
    return (
        f"e_effect-exam_effect_timer-{delay:04d}-01-"
        f"{_child_id(value_permille)}"
    )


@dataclass(frozen=True, slots=True)
class _TimerSpec:
    slot_index: int
    delay: int
    value_permille: int
    trigger_id: str = ""
    trigger_threshold: int | None = None

    @property
    def effect_id(self) -> str:
        return _timer_id(self.delay, self.value_permille)

    @property
    def child_effect_id(self) -> str:
        return _child_id(self.value_permille)


@dataclass(frozen=True, slots=True)
class _VersionSpec:
    card_id: str
    upgrade: int
    stamina: int
    cost_type: str
    cost_value: int
    effect_group_ids: tuple[str, ...]
    timers: tuple[_TimerSpec, ...]
    wrapper_effect_id: str = ""
    wrapper_enchant_id: str = ""
    wrapper_review_value: int = 0
    remaining_blockers: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        return f"{self.card_id}+{self.upgrade}"


_IDO_VALUES = ((3000, 5000), (4400, 5500), (4600, 5800), (4900, 6100))
_SUP_VALUES = (1200, 1200, 1800, 2100)

_VERSION_SPECS = tuple(
    _VersionSpec(
        IDO_CARD_ID,
        upgrade,
        11 if upgrade == 0 else 10,
        COST_STAMINA,
        0,
        IDO_CARD_EFFECT_GROUPS,
        (
            _TimerSpec(
                1,
                1,
                values[0],
                "e_trigger-none-review_up-10",
                10,
            ),
            _TimerSpec(
                2,
                2,
                values[1],
                "e_trigger-none-review_up-15",
                15,
            ),
        ),
    )
    for upgrade, values in enumerate(_IDO_VALUES)
) + tuple(
    _VersionSpec(
        SUPPORT_CARD_ID,
        upgrade,
        0,
        COST_AGGRESSIVE,
        2,
        SUPPORT_CARD_EFFECT_GROUPS,
        (_TimerSpec(0, 4, value),),
        (
            "e_effect-exam_status_enchant-03-"
            f"enchant-p_card-02-sup-3_180-enc{'02' if upgrade == 0 else '01'}"
        ),
        f"enchant-p_card-02-sup-3_180-enc{'02' if upgrade == 0 else '01'}",
        2 if upgrade == 0 else 3,
        (f"effect-trigger:{AGGRESSIVE_TRIGGER_ID}",),
    )
    for upgrade, value in enumerate(_SUP_VALUES)
)
_SPEC_BY_REF = {(spec.card_id, spec.upgrade): spec for spec in _VERSION_SPECS}
_TIMER_SPEC_BY_ID = {
    timer.effect_id: timer for spec in _VERSION_SPECS for timer in spec.timers
}

TARGET_VERSION_REFS = tuple(spec.ref for spec in _VERSION_SPECS)
DIRECT_VERSION_REFS = tuple(
    spec.ref for spec in _VERSION_SPECS if spec.card_id == IDO_CARD_ID
)
CO_BLOCKED_VERSION_REFS = tuple(
    spec.ref for spec in _VERSION_SPECS if spec.card_id == SUPPORT_CARD_ID
)
TARGET_TIMER_EFFECT_IDS = frozenset(_TIMER_SPEC_BY_ID)


class Plan2TimerLessonDependReviewContractError(ValueError):
    """Stable fail-closed error for a row outside this exact batch."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _plain_i32(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{label} must be an integer")
    if value < INT32_MIN or value > INT32_MAX:
        raise Plan2TimerLessonDependReviewContractError(
            "signed-int32-out-of-range", label
        )
    return value


def _review_after_direct(value: int, gain: int) -> int:
    result = value + gain
    if result < INT32_MIN or result > INT32_MAX:
        raise Plan2TimerLessonDependReviewContractError(
            "signed-int32-overflow", "direct-review"
        )
    return result


def _is_plain_effect(
    effect: Plan3Effect,
    *,
    effect_type: str,
    value1: int,
    value2: int,
    effect_count: int,
    effect_turn: int,
) -> bool:
    return (
        effect.effect_type == effect_type
        and effect.value1 == value1
        and effect.value2 == value2
        and effect.effect_count == effect_count
        and effect.effect_turn == effect_turn
        and not effect.status_enchant_id
        and effect.status_enchant is None
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
        and effect.trigger is None
        and not effect.once
        and effect.card_move_rule is None
    )


def _is_neutral_trigger(trigger: Plan3Trigger, *, trigger_id: str) -> bool:
    return (
        trigger.id == trigger_id
        and trigger.phase_types == (PHASE_NONE,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def _is_phase_only_trigger(
    trigger: Plan3Trigger, *, trigger_id: str, phase: str
) -> bool:
    return (
        trigger.id == trigger_id
        and trigger.phase_types == (phase,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and not trigger.field_types
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and not trigger.effect_types
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def _effect_group_ids(
    connection: sqlite3.Connection, effect_id: str
) -> tuple[str, ...]:
    row = connection.execute(
        "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
    ).fetchone()
    if row is None:
        raise Plan2TimerLessonDependReviewContractError(
            "master-effect-missing", effect_id
        )
    raw = json.loads(str(row[0]))
    groups = raw.get("effectGroupIds") if isinstance(raw, dict) else None
    if not isinstance(groups, list) or not all(
        isinstance(value, str) and value for value in groups
    ):
        raise Plan2TimerLessonDependReviewContractError(
            "master-effect-groups-invalid", effect_id
        )
    return tuple(groups)


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewChildContract:
    effect: Plan3Effect
    value_permille: int

    @property
    def effect_id(self) -> str:
        return self.effect.id


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewTimerContract:
    effect: Plan3Effect
    child: Plan2TimerReviewChildContract
    slot_index: int
    delay: int
    trigger_id: str
    trigger_threshold: int | None

    @property
    def effect_id(self) -> str:
        return self.effect.id


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewCardVersion:
    card: Plan3Card
    timers: tuple[Plan2TimerReviewTimerContract, ...]
    direct_review_gain: int
    remaining_blockers: tuple[str, ...]

    @property
    def ref(self) -> str:
        return f"{self.card.id}+{self.card.upgrade}"

    @property
    def coverage(self) -> str:
        return "direct" if not self.remaining_blockers else "co-blocked"


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewCatalog:
    versions: tuple[Plan2TimerReviewCardVersion, ...]

    def version(self, card_id: str, upgrade: int) -> Plan2TimerReviewCardVersion:
        for version in self.versions:
            if version.card.id == card_id and version.card.upgrade == upgrade:
                return version
        raise Plan2TimerLessonDependReviewContractError(
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


def _validate_timer(
    effect: Plan3Effect,
    spec: _TimerSpec,
    connection: sqlite3.Connection,
) -> Plan2TimerReviewTimerContract:
    child = effect.chain_effect
    valid_trigger = effect.trigger is None
    if spec.trigger_threshold is not None:
        trigger = effect.trigger
        valid_trigger = (
            trigger is not None
            and _is_neutral_trigger(trigger, trigger_id=spec.trigger_id)
            and trigger.field_types == (FIELD_REVIEW_UP,)
            and trigger.field_values == (spec.trigger_threshold,)
        )
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
        and _is_plain_effect(
            child,
            effect_type=EFFECT_LESSON_DEPEND_REVIEW,
            value1=spec.value_permille,
            value2=0,
            effect_count=1,
            effect_turn=0,
        )
        and valid_trigger
        and not effect.once
        and effect.card_move_rule is None
        and _effect_group_ids(connection, effect.id) == TIMER_EFFECT_GROUPS
        and _effect_group_ids(connection, child.id) == CHILD_EFFECT_GROUPS
    ):
        raise Plan2TimerLessonDependReviewContractError(
            "unintegrated-timer-child-shape", spec.effect_id
        )
    child_contract = Plan2TimerReviewChildContract(child, spec.value_permille)
    return Plan2TimerReviewTimerContract(
        effect,
        child_contract,
        spec.slot_index,
        spec.delay,
        spec.trigger_id,
        spec.trigger_threshold,
    )


def _validate_support_wrapper(
    effect: Plan3Effect,
    spec: _VersionSpec,
    connection: sqlite3.Connection,
) -> None:
    trigger = effect.trigger
    enchant = effect.status_enchant
    end_trigger = None if enchant is None else enchant.trigger
    child = None if enchant is None or len(enchant.effects) != 1 else enchant.effects[0]
    if not (
        effect.id == spec.wrapper_effect_id
        and effect.effect_type == EFFECT_STATUS_ENCHANT
        and effect.value1 == 0
        and effect.value2 == 0
        and effect.effect_count == 0
        and effect.effect_turn == 3
        and effect.status_enchant_id == spec.wrapper_enchant_id
        and enchant is not None
        and enchant.id == spec.wrapper_enchant_id
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
        and trigger is not None
        and _is_neutral_trigger(trigger, trigger_id=AGGRESSIVE_TRIGGER_ID)
        and trigger.field_types == (FIELD_AGGRESSIVE_UP,)
        and trigger.field_values == (3,)
        and not effect.once
        and effect.card_move_rule is None
        and end_trigger is not None
        and _is_phase_only_trigger(
            end_trigger,
            trigger_id="e_trigger-exam_end_turn",
            phase=PHASE_END_TURN,
        )
        and child is not None
        and _is_plain_effect(
            child,
            effect_type=EFFECT_REVIEW,
            value1=spec.wrapper_review_value,
            value2=0,
            effect_count=0,
            effect_turn=0,
        )
        and _effect_group_ids(connection, effect.id)
        == SUPPORT_WRAPPER_EFFECT_GROUPS
        and _effect_group_ids(connection, child.id) == REVIEW_EFFECT_GROUPS
    ):
        raise Plan2TimerLessonDependReviewContractError(
            "co-blocked-wrapper-shape-changed", spec.ref
        )


def validate_plan2_timer_review_card(
    card: Plan3Card,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2TimerReviewCardVersion:
    """Validate one supplied card against the exact eight-version contract."""

    if not isinstance(card, Plan3Card):
        raise TypeError("card must be Plan3Card")
    spec = _SPEC_BY_REF.get((card.id, card.upgrade))
    if spec is None:
        raise Plan2TimerLessonDependReviewContractError(
            "unintegrated-card-version", f"{card.id}+{card.upgrade}"
        )
    expected_ids = (
        ((DIRECT_REVIEW_EFFECT_ID,) if card.id == IDO_CARD_ID else ())
        + tuple(timer.effect_id for timer in spec.timers)
        + ((spec.wrapper_effect_id,) if spec.wrapper_effect_id else ())
    )
    if not (
        card.plan_type == PLAN2
        and card.category == CATEGORY_ACTIVE
        and card.stamina_cost == spec.stamina
        and card.force_stamina_cost == 0
        and card.cost_type == spec.cost_type
        and card.cost_value == spec.cost_value
        and card.play_trigger is None
        and card.move_position_type == MOVE_LOST
        and card.effect_group_ids == spec.effect_group_ids
        and tuple(effect.id for effect in card.effects) == expected_ids
    ):
        raise Plan2TimerLessonDependReviewContractError(
            "unintegrated-card-shape", spec.ref
        )
    with closing(sqlite3.connect(database)) as connection:
        offset = 0
        direct_gain = 0
        if card.id == IDO_CARD_ID:
            direct = card.effects[0]
            if not _is_plain_effect(
                direct,
                effect_type=EFFECT_REVIEW,
                value1=2,
                value2=0,
                effect_count=0,
                effect_turn=0,
            ) or _effect_group_ids(
                connection, direct.id
            ) != REVIEW_EFFECT_GROUPS:
                raise Plan2TimerLessonDependReviewContractError(
                    "direct-review-shape-changed", spec.ref
                )
            direct_gain = 2
            offset = 1
        timers = tuple(
            _validate_timer(card.effects[offset + index], timer, connection)
            for index, timer in enumerate(spec.timers)
        )
        if spec.wrapper_effect_id:
            _validate_support_wrapper(card.effects[-1], spec, connection)
    return Plan2TimerReviewCardVersion(
        card, timers, direct_gain, spec.remaining_blockers
    )


def load_plan2_timer_review_catalog(
    *, database: Path = DEFAULT_DATABASE
) -> Plan2TimerReviewCatalog:
    versions = tuple(
        validate_plan2_timer_review_card(
            load_plan3_card(spec.card_id, spec.upgrade, database),
            database=database,
        )
        for spec in _VERSION_SPECS
    )
    catalog = Plan2TimerReviewCatalog(versions)
    if (
        tuple(row.ref for row in versions) != TARGET_VERSION_REFS
        or catalog.direct_refs != DIRECT_VERSION_REFS
        or catalog.co_blocked_refs != CO_BLOCKED_VERSION_REFS
    ):
        raise Plan2TimerLessonDependReviewContractError(
            "coverage-accounting-changed"
        )
    return catalog


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewAdmission:
    timer_effect_id: str
    review_snapshot: int
    threshold: int | None
    admitted: bool
    stage: str = "pre-direct-effect-sequence"


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewQueuedTimer:
    instance_id: str
    source_guid: str
    card_ref: str
    timer: Plan2TimerReviewTimerContract

    def __post_init__(self) -> None:
        for label, value in (
            ("instance_id", self.instance_id),
            ("source_guid", self.source_guid),
            ("card_ref", self.card_ref),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} must be non-empty text")
        if not isinstance(self.timer, Plan2TimerReviewTimerContract):
            raise TypeError("timer must be Plan2TimerReviewTimerContract")
        spec = _TIMER_SPEC_BY_ID.get(self.timer.effect_id)
        if spec is None or not (
            self.timer.slot_index == spec.slot_index
            and self.timer.delay == spec.delay
            and self.timer.child.value_permille == spec.value_permille
            and self.timer.trigger_id == spec.trigger_id
            and self.timer.trigger_threshold == spec.trigger_threshold
        ):
            raise Plan2TimerLessonDependReviewContractError(
                "queued-timer-outside-exact-catalog", self.timer.effect_id
            )


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewRuntime:
    scheduler: Plan3State
    application_status: ParameterApplicationStatus
    queue: tuple[Plan2TimerReviewQueuedTimer, ...] = ()
    completed_start_boundaries: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scheduler, Plan3State):
            raise TypeError("scheduler must be Plan3State")
        if not isinstance(self.application_status, ParameterApplicationStatus):
            raise TypeError("application_status must be ParameterApplicationStatus")
        if self.scheduler.score != self.application_status.judge_parameter:
            raise Plan2TimerLessonDependReviewContractError(
                "score-projections-differ"
            )
        if (
            not isinstance(self.completed_start_boundaries, int)
            or isinstance(self.completed_start_boundaries, bool)
            or self.completed_start_boundaries < 0
        ):
            raise ValueError(
                "completed_start_boundaries must be a non-negative integer"
            )
        if self.scheduler.awaiting_turn_start:
            raise Plan2TimerLessonDependReviewContractError(
                "standalone-node-cannot-pause-mid-boundary"
            )
        if any(
            not isinstance(queued, Plan2TimerReviewQueuedTimer)
            for queued in self.queue
        ):
            raise TypeError("queue must contain Plan2TimerReviewQueuedTimer")
        active_ids = tuple(
            enchant.instance_id for enchant in self.scheduler.active_status_enchants
        )
        if active_ids != self.queue_instance_ids:
            raise Plan2TimerLessonDependReviewContractError(
                "queue-identity-projections-differ"
            )
        if len(set(active_ids)) != len(active_ids):
            raise Plan2TimerLessonDependReviewContractError(
                "duplicate-queue-instance-identity"
            )
        for queued, enchant in zip(
            self.queue, self.scheduler.active_status_enchants, strict=True
        ):
            if not (
                enchant.source_id == queued.timer.effect_id
                and enchant.rule.id == f"effect-timer:{queued.timer.effect_id}"
                and enchant.rule.trigger.phase_types == (PHASE_TURN_TIMER,)
                and enchant.rule.trigger.phase_values == (queued.timer.delay,)
                and enchant.rule.effects == (queued.timer.child.effect,)
                and enchant.max_uses == 1
                and enchant.uses == 0
                and enchant.max_uses_per_turn == 0
                and enchant.uses_this_turn == 0
                and enchant.remaining_turns == -1
                and not enchant.is_item_direct
                and 0 <= enchant.turn_count <= INT32_MAX
            ):
                raise Plan2TimerLessonDependReviewContractError(
                    "queue-listener-shape-changed", queued.instance_id
                )
        if self.scheduler.plays_remaining != 0:
            raise Plan2TimerLessonDependReviewContractError(
                "standalone-node-must-be-post-play"
            )

    @property
    def queue_instance_ids(self) -> tuple[str, ...]:
        return tuple(timer.instance_id for timer in self.queue)

    @property
    def terminal(self) -> bool:
        return self.scheduler.turns_remaining == 0


def create_plan2_timer_review_runtime(
    *,
    turns_remaining: int,
    application_status: ParameterApplicationStatus | None = None,
) -> Plan2TimerReviewRuntime:
    turns_remaining = _plain_i32(turns_remaining, "turns_remaining")
    if turns_remaining <= 0:
        raise ValueError("turns_remaining must be positive")
    application = (
        ParameterApplicationStatus(judge_parameter=0)
        if application_status is None
        else application_status
    )
    if not isinstance(application, ParameterApplicationStatus):
        raise TypeError("application_status must be ParameterApplicationStatus")
    scheduler = Plan3State(
        turns_remaining=turns_remaining,
        stamina=0,
        max_stamina=0,
        score=application.judge_parameter,
        plays_remaining=0,
        lesson_type=LESSON_GENERAL,
    )
    return Plan2TimerReviewRuntime(scheduler, application)


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewEnqueue:
    before: Plan2TimerReviewRuntime
    after: Plan2TimerReviewRuntime
    version: Plan2TimerReviewCardVersion
    review_snapshot: int
    direct_review_gain: int
    review_after_direct: int
    admissions: tuple[Plan2TimerReviewAdmission, ...]
    queued: tuple[Plan2TimerReviewQueuedTimer, ...]
    remaining_blockers: tuple[str, ...]
    event_trace: tuple[str, ...]


def enqueue_plan2_timer_review_card(
    runtime: Plan2TimerReviewRuntime,
    version: Plan2TimerReviewCardVersion,
    *,
    source_guid: str,
    review_snapshot: int,
) -> Plan2TimerReviewEnqueue:
    """Snapshot card gates, then install admitted timers in card-slot order."""

    if not isinstance(runtime, Plan2TimerReviewRuntime):
        raise TypeError("runtime must be Plan2TimerReviewRuntime")
    if not isinstance(version, Plan2TimerReviewCardVersion):
        raise TypeError("version must be Plan2TimerReviewCardVersion")
    if not isinstance(source_guid, str) or not source_guid:
        raise ValueError("source_guid must be non-empty text")
    review_snapshot = _plain_i32(review_snapshot, "review_snapshot")
    if runtime.terminal or runtime.scheduler.awaiting_turn_start:
        raise Plan2TimerLessonDependReviewContractError(
            "timer-enqueue-outside-active-turn"
        )
    working = runtime.scheduler
    queue = list(runtime.queue)
    admissions: list[Plan2TimerReviewAdmission] = []
    queued: list[Plan2TimerReviewQueuedTimer] = []
    trace = [f"card-gate:signed-review-snapshot:{review_snapshot}"]
    for timer in version.timers:
        admitted = (
            timer.trigger_threshold is None
            or review_snapshot >= timer.trigger_threshold
        )
        admissions.append(
            Plan2TimerReviewAdmission(
                timer.effect_id,
                review_snapshot,
                timer.trigger_threshold,
                admitted,
            )
        )
        trace.append(
            f"card-gate:slot-{timer.slot_index}:{timer.trigger_id or 'none'}:"
            f"admitted={str(admitted).lower()}"
        )
    direct_after = _review_after_direct(
        review_snapshot, version.direct_review_gain
    )
    if version.direct_review_gain:
        trace.append(
            f"card-slot:0:direct-review:+{version.direct_review_gain}:"
            f"after={direct_after}"
        )
    for timer, admission in zip(version.timers, admissions, strict=True):
        trace.append(
            f"card-slot:{timer.slot_index}:timer:{timer.effect_id}:"
            f"admitted={str(admission.admitted).lower()}"
        )
        if not admission.admitted:
            continue
        working = _install_effect_timer(
            working,
            timer.effect,
            source_id=f"{source_guid}:slot-{timer.slot_index}",
        )
        installed = working.active_status_enchants[-1]
        item = Plan2TimerReviewQueuedTimer(
            installed.instance_id, source_guid, version.ref, timer
        )
        queue.append(item)
        queued.append(item)
        trace.append(f"timer-enqueued:{installed.instance_id}:delay={timer.delay}")
    after = Plan2TimerReviewRuntime(
        working,
        runtime.application_status,
        tuple(queue),
        runtime.completed_start_boundaries,
    )
    if version.remaining_blockers:
        trace.extend(f"co-blocked:{value}" for value in version.remaining_blockers)
    return Plan2TimerReviewEnqueue(
        runtime,
        after,
        version,
        review_snapshot,
        version.direct_review_gain,
        direct_after,
        tuple(admissions),
        tuple(queued),
        version.remaining_blockers,
        tuple(trace),
    )


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewChildExecution:
    queue_instance_id: str
    source_guid: str
    card_ref: str
    timer_effect_id: str
    child_effect_id: str
    review_snapshot: int
    ratio_value: int
    calculated_value: int
    application: ParameterApplication


def execute_plan2_timer_review_child(
    queued: Plan2TimerReviewQueuedTimer,
    *,
    review_snapshot: int,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    application_status: ParameterApplicationStatus,
    additional: AddingParameterAdditionalData | None = None,
    is_buff_active: bool = True,
) -> tuple[Plan2TimerReviewChildExecution, ParameterApplicationStatus]:
    """Execute one native child hit from the due-phase Review snapshot."""

    if not isinstance(queued, Plan2TimerReviewQueuedTimer):
        raise TypeError("queued must be Plan2TimerReviewQueuedTimer")
    review_snapshot = _plain_i32(review_snapshot, "review_snapshot")
    if not isinstance(adding_status, AddingParameterStatus):
        raise TypeError("adding_status must be AddingParameterStatus")
    if not isinstance(settings, AddingParameterSettings):
        raise TypeError("settings must be AddingParameterSettings")
    if not isinstance(application_status, ParameterApplicationStatus):
        raise TypeError("application_status must be ParameterApplicationStatus")
    ratio_value = get_ratio_effect_int_value(
        review_snapshot, queued.timer.child.value_permille, is_ceil=True
    )
    calculated = calculate_adding_parameter(
        ratio_value,
        is_buff_active=is_buff_active,
        status=replace(adding_status, review=review_snapshot),
        settings=settings,
        additional=additional,
    )
    application = apply_parameter_add(calculated, status=application_status)
    after_status = replace(
        application_status,
        judge_parameter=application.after,
        current_turn_total_add_parameter=(
            application.current_turn_total_add_parameter
        ),
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )
    return (
        Plan2TimerReviewChildExecution(
            queued.instance_id,
            queued.source_guid,
            queued.card_ref,
            queued.timer.effect_id,
            queued.timer.child.effect_id,
            review_snapshot,
            ratio_value,
            calculated,
            application,
        ),
        after_status,
    )


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewTurnInput:
    review_snapshot: int
    adding_status: AddingParameterStatus = AddingParameterStatus()
    settings: AddingParameterSettings = AddingParameterSettings()
    additional: AddingParameterAdditionalData | None = None
    is_buff_active: bool = True
    extra_turns: int = 0

    def __post_init__(self) -> None:
        _plain_i32(self.review_snapshot, "review_snapshot")
        if (
            not isinstance(self.extra_turns, int)
            or isinstance(self.extra_turns, bool)
            or self.extra_turns < 0
        ):
            raise ValueError("extra_turns must be a non-negative integer")
        _plain_i32(self.extra_turns, "extra_turns")


@dataclass(frozen=True, slots=True)
class Plan2TimerReviewTurnTransition:
    before: Plan2TimerReviewRuntime
    after: Plan2TimerReviewRuntime
    turn_input: Plan2TimerReviewTurnInput
    due_instance_ids: tuple[str, ...]
    executions: tuple[Plan2TimerReviewChildExecution, ...]
    terminal: bool
    terminal_stranded_instance_ids: tuple[str, ...]
    phase_trace: tuple[str, ...]


def advance_plan2_timer_review_turn(
    runtime: Plan2TimerReviewRuntime,
    turn_input: Plan2TimerReviewTurnInput,
) -> Plan2TimerReviewTurnTransition:
    """Close one turn and resolve the next legal timer phase, if it exists."""

    if not isinstance(runtime, Plan2TimerReviewRuntime):
        raise TypeError("runtime must be Plan2TimerReviewRuntime")
    if not isinstance(turn_input, Plan2TimerReviewTurnInput):
        raise TypeError("turn_input must be Plan2TimerReviewTurnInput")
    if runtime.terminal:
        raise Plan2TimerLessonDependReviewContractError("exam-already-terminal")
    scheduler = runtime.scheduler
    if turn_input.extra_turns:
        if (
            scheduler.turns_remaining + turn_input.extra_turns > INT32_MAX
            or scheduler.extra_turn + turn_input.extra_turns > INT32_MAX
        ):
            raise Plan2TimerLessonDependReviewContractError(
                "signed-int32-overflow", "extra-turn"
            )
        scheduler = replace(
            scheduler,
            turns_remaining=scheduler.turns_remaining + turn_input.extra_turns,
            extra_turn=scheduler.extra_turn + turn_input.extra_turns,
        )
    # The generic Plan 3 validator intentionally rejects this Plan 2-only
    # child type before considering the current phase.  Timer listeners have
    # no EndTurn behavior, so detach only this standalone queue while the
    # already-proven common boundary is spent, then restore the same objects
    # (and therefore their native/search identity) unchanged.
    timer_enchants = scheduler.active_status_enchants
    ended = end_plan3_turn(replace(scheduler, active_status_enchants=()))
    ended = replace(ended, active_status_enchants=timer_enchants)
    reset_application = replace(
        runtime.application_status, current_turn_total_add_parameter=0
    )
    trace = ["phase:ExamEndTurn"]
    if ended.turns_remaining == 0:
        after = Plan2TimerReviewRuntime(
            replace(ended, score=reset_application.judge_parameter),
            reset_application,
            runtime.queue,
            runtime.completed_start_boundaries,
        )
        stranded = after.queue_instance_ids
        trace.append("terminal:no-StartTurn:no-TurnTimer")
        return Plan2TimerReviewTurnTransition(
            runtime,
            after,
            turn_input,
            (),
            (),
            True,
            stranded,
            tuple(trace),
        )

    if any(
        enchant.turn_count == INT32_MAX
        for enchant in ended.active_status_enchants
    ):
        raise Plan2TimerLessonDependReviewContractError(
            "signed-int32-overflow", "timer-turn-count"
        )
    advanced = _advance_status_enchants_for_turn_start(ended)
    active_by_id = {
        enchant.instance_id: enchant
        for enchant in advanced.active_status_enchants
    }
    due_ids = tuple(
        queued.instance_id
        for queued in runtime.queue
        if (
            active_by_id[queued.instance_id].rule.trigger.phase_types
            == (PHASE_TURN_TIMER,)
            and active_by_id[queued.instance_id].turn_count
            in active_by_id[queued.instance_id].rule.trigger.phase_values
        )
    )
    trace.extend(("phase:ExamStartTurn", "phase:StartPlay", "phase:ExamTurnTimer"))
    application = reset_application
    executions: list[Plan2TimerReviewChildExecution] = []
    for queued in runtime.queue:
        if queued.instance_id not in due_ids:
            continue
        execution, application = execute_plan2_timer_review_child(
            queued,
            review_snapshot=turn_input.review_snapshot,
            adding_status=turn_input.adding_status,
            settings=turn_input.settings,
            application_status=application,
            additional=turn_input.additional,
            is_buff_active=turn_input.is_buff_active,
        )
        executions.append(execution)
        trace.append(
            f"timer-fired:{queued.instance_id}:child={execution.child_effect_id}:"
            f"review={turn_input.review_snapshot}:actual="
            f"{execution.application.actual_parameter}"
        )
    after_removal = _remove_triggered_turn_timer_statuses(advanced)
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
    due_set = frozenset(due_ids)
    after_queue = tuple(
        queued for queued in runtime.queue if queued.instance_id not in due_set
    )
    after = Plan2TimerReviewRuntime(
        after_scheduler,
        application,
        after_queue,
        runtime.completed_start_boundaries + 1,
    )
    return Plan2TimerReviewTurnTransition(
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
class Plan2TimerReviewSearchResult:
    initial: Plan2TimerReviewRuntime
    transitions: tuple[Plan2TimerReviewTurnTransition, ...]
    states: tuple[Plan2TimerReviewRuntime, ...]
    stopped_reason: str


def search_plan2_timer_review_native_horizon(
    initial: Plan2TimerReviewRuntime,
    turn_inputs: Sequence[Plan2TimerReviewTurnInput],
) -> Plan2TimerReviewSearchResult:
    """Persist exact timer objects across an immutable native-search horizon."""

    if not isinstance(initial, Plan2TimerReviewRuntime):
        raise TypeError("initial must be Plan2TimerReviewRuntime")
    if not isinstance(turn_inputs, Sequence):
        raise TypeError("turn_inputs must be a sequence")
    current = initial
    transitions: list[Plan2TimerReviewTurnTransition] = []
    states: list[Plan2TimerReviewRuntime] = []
    reason = "input-exhausted"
    for item in turn_inputs:
        if not isinstance(item, Plan2TimerReviewTurnInput):
            raise TypeError("turn_inputs must contain Plan2TimerReviewTurnInput")
        if current.terminal:
            reason = "terminal"
            break
        transition = advance_plan2_timer_review_turn(current, item)
        transitions.append(transition)
        current = transition.after
        states.append(current)
        if transition.terminal:
            reason = "terminal"
            break
    return Plan2TimerReviewSearchResult(
        initial, tuple(transitions), tuple(states), reason
    )


__all__ = [
    "AGGRESSIVE_TRIGGER_ID",
    "CO_BLOCKED_VERSION_REFS",
    "DIRECT_VERSION_REFS",
    "IDO_CARD_ID",
    "Plan2TimerLessonDependReviewContractError",
    "Plan2TimerReviewAdmission",
    "Plan2TimerReviewCardVersion",
    "Plan2TimerReviewCatalog",
    "Plan2TimerReviewChildContract",
    "Plan2TimerReviewChildExecution",
    "Plan2TimerReviewEnqueue",
    "Plan2TimerReviewQueuedTimer",
    "Plan2TimerReviewRuntime",
    "Plan2TimerReviewSearchResult",
    "Plan2TimerReviewTimerContract",
    "Plan2TimerReviewTurnInput",
    "Plan2TimerReviewTurnTransition",
    "SUPPORT_CARD_ID",
    "TARGET_TIMER_EFFECT_IDS",
    "TARGET_VERSION_REFS",
    "advance_plan2_timer_review_turn",
    "create_plan2_timer_review_runtime",
    "enqueue_plan2_timer_review_card",
    "execute_plan2_timer_review_child",
    "load_plan2_timer_review_catalog",
    "search_plan2_timer_review_native_horizon",
    "validate_plan2_timer_review_card",
]
