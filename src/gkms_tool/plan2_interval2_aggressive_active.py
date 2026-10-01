"""Exact standalone Plan2 adapter for the interval-2 ActiveSkill trigger.

The adapter owns one Master status-trigger family only:
``e_trigger-exam_play_count_interval-2-card_play_aggressive_up-6-``
``p_card_search-active_skill-target``.  It does not register a central hook,
reimplement the existing direct ``CardPlayAggressive`` effects, or extend the
Plan2 core state.

The three native conditions are evaluated from one pre-payment/pre-direct
snapshot after the accepted event has become ``Playing``:

* the listener-local phase-23 count has just reached modulo two;
* the signed current Aggressive value is inclusive ``>= 6``;
* the bound Playing card matches the exact Target/ActiveSkill search.

The counter record is the same Plan2 phase-23 record used by the interval-five
adapter.  Child effects are dispatched in active/ Master order and the
unlimited listener ``SpendCount`` is represented after that child batch.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_exact_target_card_search,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import (
    AggressiveTriggerRow,
    FIELD_CARD_PLAY_AGGRESSIVE_UP,
    LESSON_UNKNOWN,
    MOVE_UNKNOWN,
)
from .plan2_card_play_playing_search_trigger import PlaySource
from .plan2_end_turn_card_play_aggressive_trigger import (
    Plan2EndTurnCardPlayAggressiveEvaluationInput,
    TARGET_THRESHOLD as SHARED_AGGRESSIVE_THRESHOLD,
    evaluate_plan2_end_turn_card_play_aggressive_trigger,
)
from .plan2_end_turn_trigger import (
    Plan2EndTurnContractError,
    _validate_neutral_effect_fields,
)
from .plan2_play_count_interval5 import (
    NATIVE_CARD_TRANSACTION_ORDER as INTERVAL5_TRANSACTION_ORDER,
    Plan2PlayCountPhaseCounter,
)
from .plan2_state import (
    PERMANENT_TURN,
    Plan2EndTurnEffect,
    Plan2EndTurnListener,
    Plan2State,
    simulate_plan2_turn_start,
)


CARD_ID = "p_card-02-ido-3_143"
CARD_UPGRADES = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple((CARD_ID, upgrade) for upgrade in CARD_UPGRADES)
AFFECTED_CARD_VERSION_COUNT = 4
DIRECT_CARD_VERSION_COUNT = 4
SOLE_UNLOCK_CARD_VERSION_COUNT = 4
CO_BLOCKED_CARD_VERSION_COUNT = 0

TRIGGER_ID = (
    "e_trigger-exam_play_count_interval-2-card_play_aggressive_up-6-"
    "p_card_search-active_skill-target"
)
PHASE_PLAY_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
PHASE_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
INTERVAL = 2
TARGET_THRESHOLD = SHARED_AGGRESSIVE_THRESHOLD
SEARCH_ID = "p_card_search-active_skill-target"
EVENT_CARD_CATEGORY = "ProduceCardCategory_ActiveSkill"
SOURCE_CARD_CATEGORY = "ProduceCardCategory_MentalSkill"
CARD_PLAN_TYPE = "ProducePlanType_Plan2"
CARD_MOVE_POSITION = "ProduceCardMovePositionType_Lost"
UNKNOWN_COST = "ExamCostType_Unknown"

STATUS_ENCHANT_IDS = (
    "enchant-p_card-02-ido-3_143-enc01",
    "enchant-p_card-02-ido-3_143-enc02",
    "enchant-p_card-02-ido-3_143-enc03",
)
STATUS_ENCHANT_ID_BY_UPGRADE = (
    STATUS_ENCHANT_IDS[0],
    STATUS_ENCHANT_IDS[1],
    STATUS_ENCHANT_IDS[2],
    STATUS_ENCHANT_IDS[2],
)
TARGET_STATUS_ENCHANT_IDS = STATUS_ENCHANT_ID_BY_UPGRADE

WRAPPER_EFFECT_IDS = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_143-enc01",
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_143-enc02",
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_143-enc03",
)
WRAPPER_EFFECT_ID_BY_UPGRADE = (
    WRAPPER_EFFECT_IDS[0],
    WRAPPER_EFFECT_IDS[1],
    WRAPPER_EFFECT_IDS[2],
    WRAPPER_EFFECT_IDS[2],
)
TARGET_WRAPPER_EFFECT_IDS = WRAPPER_EFFECT_ID_BY_UPGRADE

CHILD_EFFECT_IDS = (
    "e_effect-exam_block-0007",
    "e_effect-exam_block-0008",
    "e_effect-exam_block-0009",
)
CHILD_BLOCK_VALUES = (7, 8, 9)
CHILD_EFFECT_VALUE_BY_UPGRADE = (7, 8, 9, 9)
CHILD_EFFECT_ID_BY_UPGRADE = (
    CHILD_EFFECT_IDS[0],
    CHILD_EFFECT_IDS[1],
    CHILD_EFFECT_IDS[2],
    CHILD_EFFECT_IDS[2],
)
CHILD_BLOCK_VALUE_BY_UPGRADE = (7, 8, 9, 9)
TARGET_CHILD_EFFECT_IDS = CHILD_EFFECT_ID_BY_UPGRADE

CHILD_EFFECT_TYPE = "ProduceExamEffectType_ExamBlock"
CHILD_EFFECT_GROUP_IDS = ("effect_group-visible-exam_block-000",)
WRAPPER_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_block-000",
    "effect_group-visible-exam_status_enchant-000",
)
TARGET_CARD_ID = CARD_ID
TARGET_CARD_UPGRADES = CARD_UPGRADES
TARGET_TRIGGER_ID = TRIGGER_ID
TARGET_STATUS_ENCHANT_ID_BY_UPGRADE = STATUS_ENCHANT_ID_BY_UPGRADE
TARGET_WRAPPER_EFFECT_ID_BY_UPGRADE = WRAPPER_EFFECT_ID_BY_UPGRADE
TARGET_CHILD_EFFECT_ID_BY_UPGRADE = CHILD_EFFECT_ID_BY_UPGRADE
TARGET_CHILD_EFFECT_GROUP_IDS = CHILD_EFFECT_GROUP_IDS
TARGET_CHILD_EFFECT_TYPE = CHILD_EFFECT_TYPE
TARGET_WRAPPER_EFFECT_GROUP_IDS = WRAPPER_EFFECT_GROUP_IDS
TARGET_SEARCH_ID = SEARCH_ID
TARGET_CHILD_EFFECT_VALUES = CHILD_BLOCK_VALUE_BY_UPGRADE
CARD_STAMINA_BY_UPGRADE = (5, 5, 5, 4)

# These direct effects are already handled by the existing Plan2 card-play
# adapter.  They are catalog evidence only; this leaf never executes them.
DIRECT_CARD_PLAY_AGGRESSIVE_EFFECT_IDS_BY_UPGRADE = (
    (),
    ("e_effect-exam_card_play_aggressive-0003",),
    ("e_effect-exam_card_play_aggressive-0004",),
    ("e_effect-exam_card_play_aggressive-0004",),
)
ORDERED_CARD_EFFECT_IDS_BY_UPGRADE = (
    (WRAPPER_EFFECT_IDS[0],),
    (DIRECT_CARD_PLAY_AGGRESSIVE_EFFECT_IDS_BY_UPGRADE[1][0], WRAPPER_EFFECT_IDS[1]),
    (DIRECT_CARD_PLAY_AGGRESSIVE_EFFECT_IDS_BY_UPGRADE[2][0], WRAPPER_EFFECT_IDS[2]),
    (DIRECT_CARD_PLAY_AGGRESSIVE_EFFECT_IDS_BY_UPGRADE[3][0], WRAPPER_EFFECT_IDS[2]),
)
CARD_EFFECT_GROUP_IDS_BY_UPGRADE = (
    (
        "effect_group-visible-exam_block-000",
        "effect_group-visible-exam_status_enchant-000",
    ),
    (
        "effect_group-visible-exam_block-000",
        "effect_group-visible-exam_status_enchant-000",
        "effect_group-visible-exam_card_play_aggressive-000",
    ),
    (
        "effect_group-visible-exam_block-000",
        "effect_group-visible-exam_status_enchant-000",
        "effect_group-visible-exam_card_play_aggressive-000",
    ),
    (
        "effect_group-visible-exam_block-000",
        "effect_group-visible-exam_status_enchant-000",
        "effect_group-visible-exam_card_play_aggressive-000",
    ),
)

TRIGGER_PHASE_BOUNDARY = "phase23-after-set-playing-before-payment-before-direct-effects"
SETTLEMENT_BOUNDARY = "after-final-zone-move"
NATIVE_CARD_TRANSACTION_ORDER = INTERVAL5_TRANSACTION_ORDER


class Plan2Interval2AggressiveActiveContractError(ValueError):
    """A Master row, listener, or snapshot is outside the exact contract."""


def _strict_equal(actual: object, expected: object, label: str) -> None:
    """Compare JSON values without Python's bool/int coercion."""

    if type(actual) is not type(expected):
        raise Plan2Interval2AggressiveActiveContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )
    if isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2Interval2AggressiveActiveContractError(
                f"{label} must be {expected!r}, got {actual!r}"
            )
        for index, (item, wanted) in enumerate(zip(actual, expected)):
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if isinstance(expected, dict):
        if actual != expected:
            raise Plan2Interval2AggressiveActiveContractError(
                f"{label} must be {expected!r}, got {actual!r}"
            )
        return
    if actual != expected:
        raise Plan2Interval2AggressiveActiveContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2Interval2AggressiveActiveContractError(
            f"{label}: invalid JSON object"
        ) from error
    if not isinstance(parsed, dict):
        raise Plan2Interval2AggressiveActiveContractError(
            f"{label}: JSON value is not an object"
        )
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2Interval2AggressiveActiveContractError(
            f"{label}: invalid JSON array"
        ) from error
    if not isinstance(parsed, list):
        raise Plan2Interval2AggressiveActiveContractError(
            f"{label}: JSON value is not an array"
        )
    return parsed


def _strict_array(value: object, expected: list[object], label: str) -> tuple[object, ...]:
    actual = _json_array(value, label)
    _strict_equal(actual, expected, label)
    return tuple(actual)


def _strict_int(value: object, label: str) -> int:
    if type(value) is not int or not INT32_MIN <= value <= INT32_MAX:
        raise Plan2Interval2AggressiveActiveContractError(
            f"{label} must be an Int32 integer"
        )
    return value


def _strict_raw_fields(
    raw: dict[str, object], expected: Mapping[str, object], row_id: str
) -> None:
    for key, wanted in expected.items():
        if key not in raw:
            raise Plan2Interval2AggressiveActiveContractError(
                f"{row_id}: missing raw field {key}"
            )
        _strict_equal(raw[key], wanted, f"{row_id}.{key}")


def _target_trigger() -> AggressiveTriggerRow:
    return AggressiveTriggerRow(
        id=TRIGGER_ID,
        phase_types=(PHASE_PLAY_COUNT_INTERVAL,),
        phase_values=(INTERVAL,),
        field_status_check_types=(),
        field_status_types=(FIELD_CARD_PLAY_AGGRESSIVE_UP,),
        field_status_values=(TARGET_THRESHOLD,),
        field_status_card_search_ids=(),
        produce_card_search_id=SEARCH_ID,
        upper_search_count=0,
        lower_search_count=0,
        card_move_position_type=MOVE_UNKNOWN,
        effect_types=(),
        lesson_type=LESSON_UNKNOWN,
    )


EXACT_TARGET_TRIGGER = _target_trigger()


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveTarget:
    """One source-card upgrade and its exact status-wrapper slot."""

    card_id: str
    upgrade: int
    status_enchant_id: str
    wrapper_effect_id: str
    child_effect_id: str
    child_block_value: int
    ordered_play_effect_ids: tuple[str, ...]
    direct_card_play_aggressive_effect_ids: tuple[str, ...]
    card_effect_group_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.card_id != CARD_ID or self.upgrade not in CARD_UPGRADES:
            raise Plan2Interval2AggressiveActiveContractError(
                "target card is outside p_card-02-ido-3_143 upgrade0..3"
            )
        if self.status_enchant_id != STATUS_ENCHANT_ID_BY_UPGRADE[self.upgrade]:
            raise Plan2Interval2AggressiveActiveContractError(
                "target status mapping is not exact"
            )
        if self.wrapper_effect_id != WRAPPER_EFFECT_ID_BY_UPGRADE[self.upgrade]:
            raise Plan2Interval2AggressiveActiveContractError(
                "target wrapper mapping is not exact"
            )
        if self.child_effect_id != CHILD_EFFECT_ID_BY_UPGRADE[self.upgrade]:
            raise Plan2Interval2AggressiveActiveContractError(
                "target child mapping is not exact"
            )
        if self.child_block_value != CHILD_BLOCK_VALUE_BY_UPGRADE[self.upgrade]:
            raise Plan2Interval2AggressiveActiveContractError(
                "target Block value mapping is not exact"
            )
        if tuple(self.ordered_play_effect_ids) != ORDERED_CARD_EFFECT_IDS_BY_UPGRADE[
            self.upgrade
        ]:
            raise Plan2Interval2AggressiveActiveContractError(
                "target direct effect order is not exact"
            )
        if tuple(self.direct_card_play_aggressive_effect_ids) != DIRECT_CARD_PLAY_AGGRESSIVE_EFFECT_IDS_BY_UPGRADE[
            self.upgrade
        ]:
            raise Plan2Interval2AggressiveActiveContractError(
                "existing direct CardPlayAggressive slot drifted"
            )
        if tuple(self.card_effect_group_ids) != CARD_EFFECT_GROUP_IDS_BY_UPGRADE[
            self.upgrade
        ]:
            raise Plan2Interval2AggressiveActiveContractError(
                "target card effect-group order is not exact"
            )


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveProgram:
    """One exact permanent/unlimited status listener program."""

    source_upgrades: tuple[int, ...]
    wrapper_effect_id: str
    status_enchant_id: str
    trigger: AggressiveTriggerRow
    search: ProduceCardSearchRule
    child: Plan2EndTurnEffect
    wrapper_effect_group_ids: tuple[str, ...]
    effect_count: int = 0
    effect_value1: int = 0
    turn: int = PERMANENT_TURN

    def __post_init__(self) -> None:
        if tuple(self.source_upgrades) not in ((0,), (1,), (2, 3)):
            raise Plan2Interval2AggressiveActiveContractError(
                "source upgrade grouping must be (0), (1), or (2,3)"
            )
        if self.wrapper_effect_id not in WRAPPER_EFFECT_IDS:
            raise Plan2Interval2AggressiveActiveContractError(
                "unknown target wrapper"
            )
        index = WRAPPER_EFFECT_IDS.index(self.wrapper_effect_id)
        if self.status_enchant_id != STATUS_ENCHANT_IDS[index]:
            raise Plan2Interval2AggressiveActiveContractError(
                "wrapper/status identity drifted"
            )
        if self.trigger != EXACT_TARGET_TRIGGER:
            raise Plan2Interval2AggressiveActiveContractError(
                "program trigger is not exact"
            )
        if not matches_exact_target_active_skill_search(self.search)[0]:
            raise Plan2Interval2AggressiveActiveContractError(
                "program search is not exact Target/ActiveSkill"
            )
        expected_child = Plan2EndTurnEffect(
            effect_id=CHILD_EFFECT_IDS[index],
            effect_type=CHILD_EFFECT_TYPE,
            value1=CHILD_BLOCK_VALUES[index],
            value2=0,
            count=0,
            turn=0,
            effect_group_ids=CHILD_EFFECT_GROUP_IDS,
        )
        if self.child != expected_child:
            raise Plan2Interval2AggressiveActiveContractError(
                "program child is not exact Block value/order"
            )
        if tuple(self.wrapper_effect_group_ids) != WRAPPER_EFFECT_GROUP_IDS:
            raise Plan2Interval2AggressiveActiveContractError(
                "program wrapper group order is not exact"
            )
        if self.effect_count != 0 or self.effect_value1 != 0:
            raise Plan2Interval2AggressiveActiveContractError(
                "program listener is not unlimited"
            )
        if self.turn != PERMANENT_TURN:
            raise Plan2Interval2AggressiveActiveContractError(
                "program listener is not permanent"
            )

    @property
    def effects(self) -> tuple[Plan2EndTurnEffect, ...]:
        return (self.child,)

    @property
    def native_total_limit(self) -> int:
        return -1

    @property
    def native_per_turn_limit(self) -> int:
        return -1


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveContract:
    """Resolved trigger/search/program family and four affected versions."""

    trigger: AggressiveTriggerRow
    search: ProduceCardSearchRule
    programs: tuple[Plan2Interval2AggressiveActiveProgram, ...]
    targets: tuple[Plan2Interval2AggressiveActiveTarget, ...]

    def __post_init__(self) -> None:
        if self.trigger != EXACT_TARGET_TRIGGER:
            raise Plan2Interval2AggressiveActiveContractError(
                "contract trigger is not exact"
            )
        if not matches_exact_target_active_skill_search(self.search)[0]:
            raise Plan2Interval2AggressiveActiveContractError(
                "contract search is not exact Target/ActiveSkill"
            )
        programs = tuple(self.programs)
        targets = tuple(self.targets)
        if tuple(program.wrapper_effect_id for program in programs) != WRAPPER_EFFECT_IDS:
            raise Plan2Interval2AggressiveActiveContractError(
                "program order is not enc01, enc02, enc03"
            )
        if tuple(target.upgrade for target in targets) != CARD_UPGRADES:
            raise Plan2Interval2AggressiveActiveContractError(
                "targets are not exactly upgrade0..3"
            )
        if tuple(program.search for program in programs) != (self.search,) * 3:
            raise Plan2Interval2AggressiveActiveContractError(
                "programs do not share the exact search snapshot"
            )
        object.__setattr__(self, "programs", programs)
        object.__setattr__(self, "targets", targets)

    @property
    def affected_version_count(self) -> int:
        return len(self.targets)

    @property
    def direct_version_count(self) -> int:
        return DIRECT_CARD_VERSION_COUNT

    @property
    def co_blocked_version_count(self) -> int:
        return self.affected_version_count - self.direct_version_count

    @property
    def affected_direct(self) -> tuple[int, int]:
        return self.affected_version_count, self.direct_version_count

    def program_for_status(self, status_enchant_id: str) -> Plan2Interval2AggressiveActiveProgram:
        for program in self.programs:
            if program.status_enchant_id == status_enchant_id:
                return program
        raise Plan2Interval2AggressiveActiveContractError(
            f"unsupported target status: {status_enchant_id}"
        )

    def program_for_upgrade(self, upgrade: int) -> Plan2Interval2AggressiveActiveProgram:
        if upgrade not in CARD_UPGRADES or isinstance(upgrade, bool):
            raise Plan2Interval2AggressiveActiveContractError(
                f"unsupported target upgrade: {upgrade!r}"
            )
        return self.program_for_status(STATUS_ENCHANT_ID_BY_UPGRADE[upgrade])

    def target_for_upgrade(self, upgrade: int) -> Plan2Interval2AggressiveActiveTarget:
        for target in self.targets:
            if target.upgrade == upgrade:
                return target
        raise Plan2Interval2AggressiveActiveContractError(
            f"unsupported target upgrade: {upgrade!r}"
        )


@dataclass(frozen=True, slots=True)
class PlayingCardSnapshot:
    """Minimal SetPlaying snapshot consumed by the exact search predicate."""

    category: str
    card_id: str = ""
    upgrade: int = 0
    position: str = "ProduceCardPositionType_Playing"

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or not self.category:
            raise TypeError("category must be non-empty text")
        if not isinstance(self.card_id, str):
            raise TypeError("card_id must be text")
        if type(self.upgrade) is not int or self.upgrade < 0:
            raise ValueError("upgrade must be a non-negative integer")
        if self.position != "ProduceCardPositionType_Playing":
            raise Plan2Interval2AggressiveActiveContractError(
                "PlayingCard snapshot position is not exact"
            )


@dataclass(frozen=True, slots=True)
class AcceptedPlay:
    """One accepted ordinary/forced/extra event at the phase-23 boundary."""

    source: PlaySource
    aggressive_status_value: int | None = None
    card_category: str | None = None
    accepted: bool = True
    playing_card_present: bool = True
    set_playing: bool = True
    is_use_playable_count: bool | None = None
    card: object | None = None
    phase: str = PHASE_PLAY_COUNT_INTERVAL
    evaluation_boundary: str = TRIGGER_PHASE_BOUNDARY
    payment_started: bool = False
    direct_effects_started: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, PlaySource):
            raise TypeError("source must be Plan2 PlaySource")
        if self.aggressive_status_value is not None and (
            type(self.aggressive_status_value) is not int
            or not INT32_MIN <= self.aggressive_status_value <= INT32_MAX
        ):
            raise ValueError("aggressive_status_value must be a signed Int32 or None")
        for label in ("accepted", "playing_card_present", "set_playing", "payment_started", "direct_effects_started"):
            if type(getattr(self, label)) is not bool:
                raise TypeError(f"{label} must be a boolean")
        if self.is_use_playable_count is not None and type(self.is_use_playable_count) is not bool:
            raise TypeError("is_use_playable_count must be bool or None")
        if self.card_category is not None and (
            not isinstance(self.card_category, str) or not self.card_category
        ):
            raise TypeError("card_category must be non-empty text or None")
        if not isinstance(self.phase, str) or not self.phase:
            raise TypeError("phase must be non-empty text")
        if not isinstance(self.evaluation_boundary, str) or not self.evaluation_boundary:
            raise TypeError("evaluation_boundary must be non-empty text")


Plan2AcceptedPlay = AcceptedPlay
Plan2Interval2AggressiveActiveListener = Plan2EndTurnListener
Plan2Interval2AggressiveActivePhaseCounter = Plan2PlayCountPhaseCounter


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveState:
    """Plan2 base state plus only this family's listener-local counters."""

    active: tuple[Plan2EndTurnListener, ...]
    phase_counts: tuple[Plan2PlayCountPhaseCounter, ...] = ()
    plan2_state: Plan2State = Plan2State()

    def __post_init__(self) -> None:
        if not isinstance(self.plan2_state, Plan2State):
            raise TypeError("plan2_state must be Plan2State")
        active = tuple(self.active)
        if not all(isinstance(item, Plan2EndTurnListener) for item in active):
            raise TypeError("active must contain Plan2 listener records")
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
        if not all(isinstance(item, Plan2PlayCountPhaseCounter) for item in counters):
            raise TypeError("phase_counts must contain Plan2 interval counters")
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


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveInstallTransition:
    before: Plan2Interval2AggressiveActiveState
    after: Plan2Interval2AggressiveActiveState
    program: Plan2Interval2AggressiveActiveProgram
    created_status_uid: int

    @property
    def listener(self) -> Plan2EndTurnListener:
        return self.after.active[-1]


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveFire:
    listener_sequence: int
    status_uid: int
    status_enchant_id: str
    count_before: int
    count_after: int
    child_sequence: int
    child_effect_id: str
    child_effect_type: str
    child_effect_value1: int
    child_effect_count: int
    child_execution_count: int
    block_before: int
    block_after: int
    aggressive_status_value: int
    spend_count_called: bool = True
    spend_count_delta: int = 0

    @property
    def child_value1(self) -> int:
        return self.child_effect_value1

    @property
    def delta(self) -> int:
        return self.block_after - self.block_before

    @property
    def source_effect_id(self) -> str:
        index = STATUS_ENCHANT_IDS.index(self.status_enchant_id)
        return WRAPPER_EFFECT_IDS[index]


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveExecution:
    before: Plan2Interval2AggressiveActiveState
    after: Plan2Interval2AggressiveActiveState
    event: AcceptedPlay
    fires: tuple[Plan2Interval2AggressiveActiveFire, ...] = ()
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = NATIVE_CARD_TRANSACTION_ORDER
    event_trace: tuple[str, ...] = ()
    search_match: bool | None = None
    aggressive_gate: bool | None = None

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after

    @property
    def block_delta(self) -> int:
        return self.after.plan2_state.block - self.before.plan2_state.block

    @property
    def trigger_boundary(self) -> str:
        return TRIGGER_PHASE_BOUNDARY

    @property
    def settlement_boundary(self) -> str:
        return SETTLEMENT_BOUNDARY


@dataclass(frozen=True, slots=True)
class Plan2Interval2AggressiveActiveTurnStartTransition:
    before: Plan2Interval2AggressiveActiveState
    after: Plan2Interval2AggressiveActiveState
    spent_status_uids: tuple[int, ...]
    expired_status_uids: tuple[int, ...]
    fresh_status_uids: tuple[int, ...]
    permanent_status_uids: tuple[int, ...]


def matches_plan2_interval2_aggressive_active_trigger(trigger: object) -> bool:
    return isinstance(trigger, AggressiveTriggerRow) and trigger == EXACT_TARGET_TRIGGER


def _target_search_shape_reason(rule: object) -> str | None:
    if not isinstance(rule, ProduceCardSearchRule):
        return "target-active-skill-search-type"
    if rule.card_categories != (EVENT_CARD_CATEGORY,):
        return f"card-search-category:{rule.id}"
    if rule.limit_count != 0:
        return f"card-search-limit:{rule.id}"
    if rule.produce_card_ids or rule.upgrade_counts:
        return f"card-search-target-filters:{rule.id}"
    # The shared exact Target predicate owns all neutral Target fields.  The
    # one category field is checked above because this target row intentionally
    # is not category-neutral.  Its generic target matcher requires at least
    # one ID, so use a synthetic any-target key only for that shape check; the
    # real row remains empty-ID/empty-upgrade, as native category targeting is.
    neutral = replace(
        rule,
        card_categories=(),
        produce_card_ids=("__any_target__",),
    )
    matched, reason = match_exact_target_card_search(neutral, "__any_target__", 0)
    if reason is not None:
        return reason
    if not matched:
        return f"card-search-target-shape:{rule.id}"
    return None


def matches_exact_target_active_skill_search(
    rule: object,
    *,
    card_category: str | None = None,
) -> tuple[bool, str | None]:
    """Reuse the exact Target predicate with the proved ActiveSkill field."""

    reason = _target_search_shape_reason(rule)
    if reason is not None:
        return False, reason
    assert isinstance(rule, ProduceCardSearchRule)
    if card_category is not None:
        if not isinstance(card_category, str) or not card_category:
            return False, f"playing-card-search-input-category:{rule.id}"
        if card_category != EVENT_CARD_CATEGORY:
            return False, None
    return True, None


matches_target_active_skill_search = matches_exact_target_active_skill_search
matches_plan2_interval2_aggressive_active_search = matches_exact_target_active_skill_search


def _load_target_trigger(connection: sqlite3.Connection) -> AggressiveTriggerRow:
    row = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
    ).fetchone()
    if row is None:
        raise Plan2Interval2AggressiveActiveContractError(
            f"missing exact trigger: {TRIGGER_ID}"
        )
    trigger_id = str(row["id"])
    _strict_array(row["phase_types_json"], [PHASE_PLAY_COUNT_INTERVAL], trigger_id)
    _strict_array(row["phase_values_json"], [INTERVAL], trigger_id)
    _strict_array(row["field_status_check_types_json"], [], trigger_id)
    _strict_array(row["field_status_types_json"], [FIELD_CARD_PLAY_AGGRESSIVE_UP], trigger_id)
    _strict_array(row["field_status_values_json"], [TARGET_THRESHOLD], trigger_id)
    _strict_array(row["field_status_produce_card_search_ids_json"], [], trigger_id)
    _strict_array(row["effect_types_json"], [], trigger_id)
    for column, expected in (
        ("produce_card_search_id", SEARCH_ID),
        ("upper_search_count", 0),
        ("lower_search_count", 0),
        ("card_move_position_type", MOVE_UNKNOWN),
        ("lesson_type", LESSON_UNKNOWN),
    ):
        if row[column] != expected:
            raise Plan2Interval2AggressiveActiveContractError(
                f"{trigger_id}: {column} drifted"
            )
    raw = _json_object(row["raw_json"], trigger_id)
    _strict_raw_fields(
        raw,
        {
            "id": TRIGGER_ID,
            "phaseTypes": [PHASE_PLAY_COUNT_INTERVAL],
            "phaseValues": [INTERVAL],
            "fieldStatusCheckTypes": [],
            "fieldStatusTypes": [FIELD_CARD_PLAY_AGGRESSIVE_UP],
            "fieldStatusValues": [TARGET_THRESHOLD],
            "fieldStatusProduceCardSearchIds": [],
            "produceCardSearchId": SEARCH_ID,
            "upperSearchCount": 0,
            "lowerSearchCount": 0,
            "cardMovePositionType": MOVE_UNKNOWN,
            "effectTypes": [],
            "lessonType": LESSON_UNKNOWN,
        },
        trigger_id,
    )
    candidate = _target_trigger()
    if candidate != EXACT_TARGET_TRIGGER:
        raise Plan2Interval2AggressiveActiveContractError(
            "typed target trigger construction drifted"
        )
    return candidate


def _validate_effect_row(
    row: sqlite3.Row,
    *,
    effect_id: str,
    effect_type: str,
    value1: int,
    value2: int,
    effect_count: int,
    effect_turn: int,
    status_enchant_id: str,
    groups: tuple[str, ...],
) -> Plan2EndTurnEffect:
    if str(row["id"]) != effect_id:
        raise Plan2Interval2AggressiveActiveContractError(
            f"expected exact effect {effect_id}"
        )
    raw = _json_object(row["raw_json"], effect_id)
    try:
        _validate_neutral_effect_fields(row, raw)
    except Plan2EndTurnContractError as error:
        raise Plan2Interval2AggressiveActiveContractError(str(error)) from error
    _strict_raw_fields(
        raw,
        {
            "id": effect_id,
            "effectType": effect_type,
            "effectValue1": value1,
            "effectValue2": value2,
            "effectCount": effect_count,
            "effectTurn": effect_turn,
            "produceExamStatusEnchantId": status_enchant_id,
            "chainProduceExamEffectId": "",
            "chainProduceExamEffectIds": [],
            "effectGroupIds": list(groups),
        },
        effect_id,
    )
    if (
        str(row["effect_type"]) != effect_type
        or _strict_int(row["value1"], f"{effect_id}.value1") != value1
        or _strict_int(row["value2"], f"{effect_id}.value2") != value2
        or _strict_int(row["effect_count"], f"{effect_id}.effect_count") != effect_count
        or _strict_int(row["effect_turn"], f"{effect_id}.effect_turn") != effect_turn
        or str(row["status_enchant_id"]) != status_enchant_id
        or str(row["chain_effect_id"]) != ""
    ):
        raise Plan2Interval2AggressiveActiveContractError(
            f"{effect_id}: scalar or chain shape drifted"
        )
    return Plan2EndTurnEffect(
        effect_id=effect_id,
        effect_type=effect_type,
        value1=value1,
        value2=value2,
        count=effect_count,
        turn=effect_turn,
        effect_group_ids=groups,
    )


def _load_program(
    connection: sqlite3.Connection,
    *,
    source_upgrades: tuple[int, ...],
    wrapper_effect_id: str,
    status_enchant_id: str,
    child_effect_id: str,
    child_value: int,
    trigger: AggressiveTriggerRow,
    search: ProduceCardSearchRule,
) -> Plan2Interval2AggressiveActiveProgram:
    status = connection.execute(
        "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
        (status_enchant_id,),
    ).fetchone()
    if status is None:
        raise Plan2Interval2AggressiveActiveContractError(
            f"missing status: {status_enchant_id}"
        )
    if str(status["produce_exam_trigger_id"]) != TRIGGER_ID:
        raise Plan2Interval2AggressiveActiveContractError(
            f"{status_enchant_id}: trigger drifted"
        )
    child_ids = _json_array(status["produce_exam_effect_ids_json"], status_enchant_id)
    _strict_equal(child_ids, [child_effect_id], f"{status_enchant_id}.produceExamEffectIds")
    status_raw = _json_object(status["raw_json"], status_enchant_id)
    _strict_raw_fields(
        status_raw,
        {
            "id": status_enchant_id,
            "assetId": str(status["asset_id"]),
            "produceExamTriggerId": TRIGGER_ID,
            "produceExamEffectIds": [child_effect_id],
        },
        status_enchant_id,
    )

    wrapper = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (wrapper_effect_id,)
    ).fetchone()
    if wrapper is None:
        raise Plan2Interval2AggressiveActiveContractError(
            f"missing wrapper: {wrapper_effect_id}"
        )
    wrapper_from_row = _validate_effect_row(
        wrapper,
        effect_id=wrapper_effect_id,
        effect_type="ProduceExamEffectType_ExamStatusEnchant",
        value1=0,
        value2=0,
        effect_count=0,
        effect_turn=PERMANENT_TURN,
        status_enchant_id=status_enchant_id,
        groups=WRAPPER_EFFECT_GROUP_IDS,
    )
    wrapper_raw = _json_object(wrapper["raw_json"], wrapper_effect_id)
    _strict_raw_fields(
        wrapper_raw,
        {
            "produceExamStatusEnchantId": status_enchant_id,
            "chainProduceExamEffectId": "",
            "chainProduceExamEffectIds": [],
        },
        wrapper_effect_id,
    )

    child_row = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (child_effect_id,)
    ).fetchone()
    if child_row is None:
        raise Plan2Interval2AggressiveActiveContractError(
            f"missing child: {child_effect_id}"
        )
    child = _validate_effect_row(
        child_row,
        effect_id=child_effect_id,
        effect_type=CHILD_EFFECT_TYPE,
        value1=child_value,
        value2=0,
        effect_count=0,
        effect_turn=0,
        status_enchant_id="",
        groups=CHILD_EFFECT_GROUP_IDS,
    )
    program = Plan2Interval2AggressiveActiveProgram(
        source_upgrades=source_upgrades,
        wrapper_effect_id=wrapper_effect_id,
        status_enchant_id=status_enchant_id,
        trigger=trigger,
        search=search,
        child=child,
        wrapper_effect_group_ids=WRAPPER_EFFECT_GROUP_IDS,
    )
    if wrapper_from_row.effect_type != "ProduceExamEffectType_ExamStatusEnchant":
        raise Plan2Interval2AggressiveActiveContractError(
            f"{wrapper_effect_id}: wrapper type drifted"
        )
    return program


def _load_card_target(row: sqlite3.Row) -> Plan2Interval2AggressiveActiveTarget:
    card_id = str(row["id"])
    upgrade = _strict_int(row["upgrade_count"], f"{card_id}.upgrade_count")
    if card_id != CARD_ID or upgrade not in CARD_UPGRADES:
        raise Plan2Interval2AggressiveActiveContractError(
            f"unexpected target card version {card_id}/{upgrade}"
        )
    expected_effects = ORDERED_CARD_EFFECT_IDS_BY_UPGRADE[upgrade]
    effect_rows = _json_array(row["play_effects_json"], f"{card_id}:{upgrade}.playEffects")
    actual_effects: list[str] = []
    for index, entry in enumerate(effect_rows):
        if not isinstance(entry, dict):
            raise Plan2Interval2AggressiveActiveContractError(
                f"{card_id}:{upgrade}: play effect {index} is not an object"
            )
        _strict_raw_fields(
            entry,
            {
                "produceExamTriggerId": "",
                "hideIcon": False,
                "isOncePlayEffect": False,
            },
            f"{card_id}:{upgrade}.playEffects[{index}]",
        )
        effect_id = entry.get("produceExamEffectId")
        if not isinstance(effect_id, str) or not effect_id:
            raise Plan2Interval2AggressiveActiveContractError(
                f"{card_id}:{upgrade}: play effect id is invalid"
            )
        actual_effects.append(effect_id)
    if tuple(actual_effects) != expected_effects:
        raise Plan2Interval2AggressiveActiveContractError(
            f"{card_id}:{upgrade}: ordered direct effect shape drifted"
        )
    raw = _json_object(row["raw_json"], f"{card_id}:{upgrade}")
    _strict_raw_fields(
        raw,
        {
            "id": CARD_ID,
            "upgradeCount": upgrade,
            "planType": CARD_PLAN_TYPE,
            "category": SOURCE_CARD_CATEGORY,
            "stamina": (5, 5, 5, 4)[upgrade],
            "forceStamina": 0,
            "costType": UNKNOWN_COST,
            "costValue": 0,
            "playProduceExamTriggerId": "",
            "playMovePositionType": CARD_MOVE_POSITION,
            "effectGroupIds": list(CARD_EFFECT_GROUP_IDS_BY_UPGRADE[upgrade]),
        },
        f"{card_id}:{upgrade}",
    )
    _strict_equal(
        _json_array(row["play_effects_json"], f"{card_id}:{upgrade}.playEffects"),
        raw.get("playEffects"),
        f"{card_id}:{upgrade}.playEffects",
    )
    direct_ids = DIRECT_CARD_PLAY_AGGRESSIVE_EFFECT_IDS_BY_UPGRADE[upgrade]
    return Plan2Interval2AggressiveActiveTarget(
        card_id=CARD_ID,
        upgrade=upgrade,
        status_enchant_id=STATUS_ENCHANT_ID_BY_UPGRADE[upgrade],
        wrapper_effect_id=WRAPPER_EFFECT_ID_BY_UPGRADE[upgrade],
        child_effect_id=CHILD_EFFECT_ID_BY_UPGRADE[upgrade],
        child_block_value=CHILD_BLOCK_VALUE_BY_UPGRADE[upgrade],
        ordered_play_effect_ids=tuple(actual_effects),
        direct_card_play_aggressive_effect_ids=direct_ids,
        card_effect_group_ids=CARD_EFFECT_GROUP_IDS_BY_UPGRADE[upgrade],
    )


def load_plan2_interval2_aggressive_active_contract(
    database: Path = DEFAULT_DATABASE,
) -> Plan2Interval2AggressiveActiveContract:
    """Read only the named Master rows for this exact trigger family."""

    database = Path(database).resolve()
    search = load_produce_card_search(SEARCH_ID, database)
    search_reason = _target_search_shape_reason(search)
    if search_reason is not None:
        raise Plan2Interval2AggressiveActiveContractError(search_reason)
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        trigger = _load_target_trigger(connection)
        programs = (
            _load_program(
                connection,
                source_upgrades=(0,),
                wrapper_effect_id=WRAPPER_EFFECT_IDS[0],
                status_enchant_id=STATUS_ENCHANT_IDS[0],
                child_effect_id=CHILD_EFFECT_IDS[0],
                child_value=CHILD_BLOCK_VALUES[0],
                trigger=trigger,
                search=search,
            ),
            _load_program(
                connection,
                source_upgrades=(1,),
                wrapper_effect_id=WRAPPER_EFFECT_IDS[1],
                status_enchant_id=STATUS_ENCHANT_IDS[1],
                child_effect_id=CHILD_EFFECT_IDS[1],
                child_value=CHILD_BLOCK_VALUES[1],
                trigger=trigger,
                search=search,
            ),
            _load_program(
                connection,
                source_upgrades=(2, 3),
                wrapper_effect_id=WRAPPER_EFFECT_IDS[2],
                status_enchant_id=STATUS_ENCHANT_IDS[2],
                child_effect_id=CHILD_EFFECT_IDS[2],
                child_value=CHILD_BLOCK_VALUES[2],
                trigger=trigger,
                search=search,
            ),
        )
        rows = connection.execute(
            "SELECT * FROM card WHERE id = ? ORDER BY upgrade_count", (CARD_ID,)
        ).fetchall()
        targets = tuple(_load_card_target(row) for row in rows)
    if tuple(target.upgrade for target in targets) != CARD_UPGRADES:
        raise Plan2Interval2AggressiveActiveContractError(
            "target card does not contain exactly upgrade0..3"
        )
    return Plan2Interval2AggressiveActiveContract(
        trigger=trigger,
        search=search,
        programs=programs,
        targets=targets,
    )


def load_plan2_interval2_aggressive_active_program(
    database: Path = DEFAULT_DATABASE,
    upgrade: int | None = None,
) -> Plan2Interval2AggressiveActiveContract | Plan2Interval2AggressiveActiveProgram:
    contract = load_plan2_interval2_aggressive_active_contract(database)
    return contract if upgrade is None else contract.program_for_upgrade(upgrade)


def load_plan2_play_count_interval2_aggressive_active_program(
    database: Path = DEFAULT_DATABASE,
    upgrade: int | None = None,
) -> Plan2Interval2AggressiveActiveContract | Plan2Interval2AggressiveActiveProgram:
    return load_plan2_interval2_aggressive_active_program(database, upgrade)


def load_plan2_interval2_aggressive_active_programs(
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan2Interval2AggressiveActiveProgram, ...]:
    return load_plan2_interval2_aggressive_active_contract(database).programs


def _expected_listener(
    status_uid: int, program: Plan2Interval2AggressiveActiveProgram
) -> Plan2EndTurnListener:
    return Plan2EndTurnListener(
        status_uid=status_uid,
        wrapper_effect_id=program.wrapper_effect_id,
        status_enchant_id=program.status_enchant_id,
        trigger_id=TRIGGER_ID,
        effects=program.effects,
        wrapper_effect_group_ids=WRAPPER_EFFECT_GROUP_IDS,
        turn=PERMANENT_TURN,
        limit_count=-1,
        limit_count_in_turn=-1,
        limit_count_in_turn_remaining=-1,
        turn_count=0,
        is_passing_turn_start=False,
    )


def _listener_errors(
    listener: Plan2EndTurnListener,
    contract: Plan2Interval2AggressiveActiveContract,
) -> tuple[str, ...]:
    try:
        program = contract.program_for_status(listener.status_enchant_id)
    except Plan2Interval2AggressiveActiveContractError:
        return (f"listener-status:{listener.status_uid}",)
    expected = _expected_listener(listener.status_uid, program)
    errors: list[str] = []
    for field_name in (
        "wrapper_effect_id",
        "status_enchant_id",
        "trigger_id",
        "effects",
        "wrapper_effect_group_ids",
    ):
        if getattr(listener, field_name) != getattr(expected, field_name):
            errors.append(f"listener-{field_name}:{listener.status_uid}")
    if not (
        listener.turn == PERMANENT_TURN
        and listener.limit_count == -1
        and listener.limit_count_in_turn == -1
        and listener.limit_count_in_turn_remaining == -1
        and 0 <= listener.turn_count <= INT32_MAX
        and type(listener.is_passing_turn_start) is bool
    ):
        errors.append(f"listener-lifecycle:{listener.status_uid}")
    return tuple(errors)


def install_plan2_interval2_aggressive_active_listener(
    state: Plan2Interval2AggressiveActiveState,
    program: Plan2Interval2AggressiveActiveProgram,
) -> Plan2Interval2AggressiveActiveInstallTransition:
    if not isinstance(state, Plan2Interval2AggressiveActiveState):
        raise TypeError("state must be Plan2Interval2AggressiveActiveState")
    if not isinstance(program, Plan2Interval2AggressiveActiveProgram):
        raise TypeError("program must be Plan2Interval2AggressiveActiveProgram")
    uid = state.plan2_state.next_status_uid
    listener = _expected_listener(uid, program)
    after = replace(
        state,
        active=state.active + (listener,),
        plan2_state=replace(state.plan2_state, next_status_uid=uid + 1),
    )
    return Plan2Interval2AggressiveActiveInstallTransition(
        before=state,
        after=after,
        program=program,
        created_status_uid=uid,
    )


def install_plan2_interval2_aggressive_active_listener_on_state(
    state: Plan2State,
    program: Plan2Interval2AggressiveActiveProgram,
) -> Plan2Interval2AggressiveActiveInstallTransition:
    return install_plan2_interval2_aggressive_active_listener(
        Plan2Interval2AggressiveActiveState(active=(), plan2_state=state), program
    )


def install_plan2_interval2_aggressive_active_listener_for_upgrade(
    state: Plan2Interval2AggressiveActiveState,
    upgrade: int,
    contract: Plan2Interval2AggressiveActiveContract | None = None,
) -> Plan2Interval2AggressiveActiveInstallTransition:
    resolved = contract or load_plan2_interval2_aggressive_active_contract()
    return install_plan2_interval2_aggressive_active_listener(
        state, resolved.program_for_upgrade(upgrade)
    )


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _event_card_category(event: AcceptedPlay) -> tuple[str | None, str | None]:
    explicit = event.card_category
    observed: object = None
    if event.card is not None:
        if isinstance(event.card, Mapping):
            observed = event.card.get("category")
        else:
            observed = getattr(event.card, "category", None)
    if explicit is None:
        if isinstance(observed, str) and observed:
            return observed, None
        return None, "playing-card-category-unavailable"
    if observed is not None and observed != explicit:
        return None, "playing-card-category-conflict"
    return explicit, None


def _shared_signed_aggressive_gate(value: int) -> tuple[bool | None, str | None]:
    """Reuse the existing signed current-value >=6 evaluator primitive."""

    try:
        evaluation = evaluate_plan2_end_turn_card_play_aggressive_trigger(
            Plan2EndTurnCardPlayAggressiveEvaluationInput(
                aggressive_status_value=value
            )
        )
    except (TypeError, ValueError) as error:
        return None, f"aggressive-input:{error}"
    if (
        not evaluation.supported
        or evaluation.fires is None
        or evaluation.comparison != "signed_greater_equal"
        or evaluation.check_not is not False
        or evaluation.threshold != TARGET_THRESHOLD
    ):
        return None, "shared-signed-aggressive-primitive-drifted"
    return evaluation.fires, None


def execute_plan2_interval2_aggressive_active_card_start(
    state: Plan2Interval2AggressiveActiveState,
    event: AcceptedPlay,
    contract: Plan2Interval2AggressiveActiveContract,
) -> Plan2Interval2AggressiveActiveExecution:
    """Resolve one accepted SetPlaying event before payment/direct effects."""

    if not isinstance(state, Plan2Interval2AggressiveActiveState):
        raise TypeError("state must be Plan2Interval2AggressiveActiveState")
    if not isinstance(event, AcceptedPlay):
        raise TypeError("event must be AcceptedPlay")
    if not isinstance(contract, Plan2Interval2AggressiveActiveContract):
        raise TypeError("contract must be Plan2Interval2AggressiveActiveContract")
    errors: list[str] = []
    if contract.trigger != EXACT_TARGET_TRIGGER:
        errors.append("unsupported-trigger-shape")
    search_reason = _target_search_shape_reason(contract.search)
    if search_reason is not None:
        errors.append(search_reason)
    if not event.accepted:
        return Plan2Interval2AggressiveActiveExecution(
            before=state,
            after=state,
            event=event,
            unresolved=tuple(_unique(errors)),
            event_trace=("rejected-before-exam-card-play",),
        )
    if event.phase != PHASE_PLAY_COUNT_INTERVAL:
        errors.append("runtime-phase-is-not-exam-play-count-interval")
    if event.evaluation_boundary != TRIGGER_PHASE_BOUNDARY:
        errors.append("evaluation-is-not-pre-payment-pre-direct")
    if event.payment_started:
        errors.append("payment-already-started")
    if event.direct_effects_started:
        errors.append("direct-effects-already-started")
    if not event.playing_card_present or not event.set_playing:
        errors.append("accepted-play-has-no-set-playing-card")
    category, category_error = _event_card_category(event)
    if category_error is not None:
        errors.append(category_error)
    search_match: bool | None = None
    if category is not None and not errors:
        search_match, search_reason = matches_exact_target_active_skill_search(
            contract.search, card_category=category
        )
        if search_reason is not None:
            errors.append(search_reason)
    for listener in state.active:
        errors.extend(_listener_errors(listener, contract))
    known_uids = {listener.status_uid for listener in state.active}
    for counter in state.phase_counts:
        if counter.status_uid not in known_uids:
            errors.append(f"counter-for-unknown-listener:{counter.status_uid}")
    if errors:
        return Plan2Interval2AggressiveActiveExecution(
            before=state,
            after=state,
            event=event,
            unresolved=_unique(errors),
            search_match=search_match,
            event_trace=("fail-closed-before-phase23-counter",),
        )
    assert search_match is not None
    trace: list[str] = ["set-playing-card"]
    if not search_match:
        trace.extend(
            (
                "search-non-match:target-active-skill",
                "ordered-direct-card-effects",
                "final-zone-move-settled",
                "card-play-after-and-interval-after-listeners",
            )
        )
        return Plan2Interval2AggressiveActiveExecution(
            before=state,
            after=state,
            event=event,
            event_trace=tuple(trace),
            search_match=False,
        )

    current_aggressive = event.aggressive_status_value
    if current_aggressive is None:
        current_aggressive = state.plan2_state.card_play_aggressive
    aggressive_gate, aggressive_reason = _shared_signed_aggressive_gate(
        current_aggressive
    )
    if aggressive_reason is not None:
        return Plan2Interval2AggressiveActiveExecution(
            before=state,
            after=state,
            event=event,
            unresolved=(aggressive_reason,),
            search_match=True,
            event_trace=("fail-closed-before-phase23-counter",),
        )
    assert aggressive_gate is not None
    old_counts = {item.status_uid: item.count for item in state.phase_counts}
    counters: list[Plan2PlayCountPhaseCounter] = []
    eligible: list[tuple[int, Plan2EndTurnListener, Plan2Interval2AggressiveActiveProgram, int, int]] = []
    trace.extend(
        (
            "increment-listener-phase-counts:23",
            "snapshot:aggressive-before-payment-before-direct-effects",
            f"search-match:target-active-skill:{category}",
            f"aggressive-current:{current_aggressive}>=6:{aggressive_gate}",
        )
    )
    for sequence, listener in enumerate(state.active):
        count_before = old_counts.get(listener.status_uid, 0)
        if count_before == INT32_MAX:
            return Plan2Interval2AggressiveActiveExecution(
                before=state,
                after=state,
                event=event,
                unresolved=(f"phase-count-overflow:{listener.status_uid}",),
                search_match=True,
                aggressive_gate=aggressive_gate,
            )
        count_after = count_before + 1
        counters.append(Plan2PlayCountPhaseCounter(listener.status_uid, count_after))
        trace.append(
            f"listener:{listener.status_uid}:phase23-count:{count_before}->{count_after}"
        )
        if count_after % INTERVAL or not aggressive_gate:
            continue
        program = contract.program_for_status(listener.status_enchant_id)
        eligible.append((sequence, listener, program, count_before, count_after))

    total_delta = sum(item[2].child.value1 for item in eligible)
    if state.plan2_state.block > INT32_MAX - total_delta:
        return Plan2Interval2AggressiveActiveExecution(
            before=state,
            after=state,
            event=event,
            unresolved=("block-parameter-overflow",),
            search_match=True,
            aggressive_gate=aggressive_gate,
        )
    block = state.plan2_state.block
    fires: list[Plan2Interval2AggressiveActiveFire] = []
    for sequence, listener, program, count_before, count_after in eligible:
        block_before = block
        block += program.child.value1
        trace.append(
            f"listener:{listener.status_uid}:child:0:{program.child.effect_id}:"
            "phase23:pre-payment:pre-direct"
        )
        fires.append(
            Plan2Interval2AggressiveActiveFire(
                listener_sequence=sequence,
                status_uid=listener.status_uid,
                status_enchant_id=program.status_enchant_id,
                count_before=count_before,
                count_after=count_after,
                child_sequence=0,
                child_effect_id=program.child.effect_id,
                child_effect_type=program.child.effect_type,
                child_effect_value1=program.child.value1,
                child_effect_count=program.child.count,
                child_execution_count=1,
                block_before=block_before,
                block_after=block,
                aggressive_status_value=current_aggressive,
            )
        )
    for fire in fires:
        trace.append(f"spend-count:{fire.status_uid}:unlimited:no-delta")
    trace.extend(
        (
            "ordered-direct-card-effects",
            "final-zone-move-settled",
            "card-play-after-and-interval-after-listeners",
        )
    )
    after = replace(
        state,
        phase_counts=tuple(counters),
        plan2_state=replace(state.plan2_state, block=block),
    )
    return Plan2Interval2AggressiveActiveExecution(
        before=state,
        after=after,
        event=event,
        fires=tuple(fires),
        search_match=True,
        aggressive_gate=aggressive_gate,
        event_trace=tuple(trace),
    )


execute_plan2_play_count_interval2_aggressive_active_card_start = execute_plan2_interval2_aggressive_active_card_start


def simulate_plan2_interval2_aggressive_active_turn_start(
    state: Plan2Interval2AggressiveActiveState,
    contract: Plan2Interval2AggressiveActiveContract | None = None,
) -> Plan2Interval2AggressiveActiveTurnStartTransition:
    if not isinstance(state, Plan2Interval2AggressiveActiveState):
        raise TypeError("state must be Plan2Interval2AggressiveActiveState")
    resolved = contract or load_plan2_interval2_aggressive_active_contract()
    errors = [
        error
        for listener in state.active
        for error in _listener_errors(listener, resolved)
    ]
    if errors:
        raise Plan2Interval2AggressiveActiveContractError(str(_unique(errors)))
    base_transition = simulate_plan2_turn_start(state.plan2_state)
    counters = {item.status_uid: item for item in state.phase_counts}
    survivors: list[Plan2EndTurnListener] = []
    spent = list(base_transition.spent_status_uids)
    expired = list(base_transition.expired_status_uids)
    fresh = list(base_transition.fresh_status_uids)
    permanent = list(base_transition.permanent_status_uids)
    for listener in state.active:
        if listener.turn_count == INT32_MAX:
            raise Plan2Interval2AggressiveActiveContractError(
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
    after = Plan2Interval2AggressiveActiveState(
        active=tuple(survivors),
        phase_counts=tuple(
            counters[listener.status_uid]
            for listener in survivors
            if listener.status_uid in counters
        ),
        plan2_state=base_transition.after,
    )
    return Plan2Interval2AggressiveActiveTurnStartTransition(
        before=state,
        after=after,
        spent_status_uids=tuple(spent),
        expired_status_uids=tuple(expired),
        fresh_status_uids=tuple(fresh),
        permanent_status_uids=tuple(permanent),
    )


simulate_plan2_play_count_interval2_aggressive_active_turn_start = simulate_plan2_interval2_aggressive_active_turn_start


__all__ = [
    "AcceptedPlay",
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "CARD_STAMINA_BY_UPGRADE",
    "CARD_EFFECT_GROUP_IDS_BY_UPGRADE",
    "CARD_ID",
    "CARD_MOVE_POSITION",
    "CARD_PLAN_TYPE",
    "CARD_UPGRADES",
    "CHILD_BLOCK_VALUES",
    "CHILD_BLOCK_VALUE_BY_UPGRADE",
    "CHILD_EFFECT_VALUE_BY_UPGRADE",
    "CHILD_EFFECT_GROUP_IDS",
    "CHILD_EFFECT_ID_BY_UPGRADE",
    "CHILD_EFFECT_IDS",
    "CHILD_EFFECT_TYPE",
    "CO_BLOCKED_CARD_VERSION_COUNT",
    "DIRECT_CARD_PLAY_AGGRESSIVE_EFFECT_IDS_BY_UPGRADE",
    "DIRECT_CARD_VERSION_COUNT",
    "EVENT_CARD_CATEGORY",
    "EXACT_TARGET_TRIGGER",
    "INTERVAL",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "ORDERED_CARD_EFFECT_IDS_BY_UPGRADE",
    "PHASE_CARD_PLAY",
    "PHASE_CARD_PLAY_AFTER",
    "PHASE_PLAY_COUNT_INTERVAL",
    "PERMANENT_TURN",
    "Plan2AcceptedPlay",
    "Plan2Interval2AggressiveActiveContract",
    "Plan2Interval2AggressiveActiveContractError",
    "Plan2Interval2AggressiveActiveExecution",
    "Plan2Interval2AggressiveActiveFire",
    "Plan2Interval2AggressiveActiveInstallTransition",
    "Plan2Interval2AggressiveActiveListener",
    "Plan2Interval2AggressiveActivePhaseCounter",
    "Plan2Interval2AggressiveActiveProgram",
    "Plan2Interval2AggressiveActiveState",
    "Plan2Interval2AggressiveActiveTarget",
    "Plan2Interval2AggressiveActiveTurnStartTransition",
    "Plan2PlayCountPhaseCounter",
    "PlaySource",
    "SEARCH_ID",
    "SETTLEMENT_BOUNDARY",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "STATUS_ENCHANT_IDS",
    "STATUS_ENCHANT_ID_BY_UPGRADE",
    "TARGET_SEARCH_ID",
    "TARGET_CARD_ID",
    "TARGET_CARD_UPGRADES",
    "TARGET_CHILD_EFFECT_GROUP_IDS",
    "TARGET_CHILD_EFFECT_IDS",
    "TARGET_CHILD_EFFECT_ID_BY_UPGRADE",
    "TARGET_CHILD_EFFECT_TYPE",
    "TARGET_STATUS_ENCHANT_ID_BY_UPGRADE",
    "TARGET_STATUS_ENCHANT_IDS",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "TARGET_WRAPPER_EFFECT_GROUP_IDS",
    "TARGET_WRAPPER_EFFECT_ID_BY_UPGRADE",
    "TARGET_WRAPPER_EFFECT_IDS",
    "TRIGGER_ID",
    "TRIGGER_PHASE_BOUNDARY",
    "WRAPPER_EFFECT_GROUP_IDS",
    "WRAPPER_EFFECT_IDS",
    "PlayingCardSnapshot",
    "execute_plan2_interval2_aggressive_active_card_start",
    "execute_plan2_play_count_interval2_aggressive_active_card_start",
    "install_plan2_interval2_aggressive_active_listener",
    "install_plan2_interval2_aggressive_active_listener_for_upgrade",
    "install_plan2_interval2_aggressive_active_listener_on_state",
    "load_plan2_interval2_aggressive_active_contract",
    "load_plan2_interval2_aggressive_active_program",
    "load_plan2_interval2_aggressive_active_programs",
    "load_plan2_play_count_interval2_aggressive_active_program",
    "matches_exact_target_active_skill_search",
    "matches_plan2_interval2_aggressive_active_search",
    "matches_plan2_interval2_aggressive_active_trigger",
    "matches_target_active_skill_search",
    "simulate_plan2_interval2_aggressive_active_turn_start",
    "simulate_plan2_play_count_interval2_aggressive_active_turn_start",
]
