"""Exact standalone Plan 3 status-change -> Full Power Point listener.

The module owns the permanent listener installed by ``p_card-03-ido-3_138``.
It evaluates the trigger only after an ``ExamFullPowerPoint`` difference has
been committed, against the current (post-commit) Preservation stance.

Android v3.2.3 globally gates effect-result listeners through
``ExamEffectCalculateContext.IsEnchantTriggerActive``.  Direct card effects
are eligible.  Nested status-enchant effects are eligible only for effect
types 103 (Timer) and 190 (TimerEndTurn); the exact child here is type 49, so
its own ``+1`` difference cannot recursively re-enter this listener.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    CATEGORY_MENTAL,
    COST_STAMINA,
    EFFECT_FULL_POWER_POINT,
    EFFECT_STATUS_ENCHANT,
    FIELD_PRESERVATION_UP,
    LESSON_UNKNOWN,
    MOVE_LOST,
    MOVE_UNKNOWN,
    PLAN3,
    STANCE_PRESERVATION,
    ActivePlan3StatusEnchant,
    Plan3Effect,
    Plan3StatusEnchantRule,
    Plan3Trigger,
    load_plan3_effect,
)


CARD_ID = "p_card-03-ido-3_138"
CARD_UPGRADES = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple((CARD_ID, upgrade) for upgrade in CARD_UPGRADES)
AFFECTED_CARD_VERSION_COUNT = 4
SOLE_UNLOCK_CARD_VERSION_COUNT = 4
CO_BLOCKED_CARD_VERSION_COUNT = 0

TRIGGER_ID = (
    "e_trigger-exam_status_change-preservation_up-exam_full_power_point"
)
PHASE_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"
INSTALLER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-03-ido-3_138-enc01"
)
STATUS_ENCHANT_ID = "enchant-p_card-03-ido-3_138-enc01"
CHILD_EFFECT_ID = "e_effect-exam_full_power_point-0001"
CHILD_VALUE = 1

PRESERVATION_EFFECT_ID = "e_effect-exam_preservation-0002"
BLOCK_EFFECT_ID = "e_effect-exam_block-0003"
ORDERED_CARD_EFFECT_IDS = {
    0: (PRESERVATION_EFFECT_ID, INSTALLER_EFFECT_ID),
    1: (PRESERVATION_EFFECT_ID, INSTALLER_EFFECT_ID),
    2: (PRESERVATION_EFFECT_ID, INSTALLER_EFFECT_ID),
    3: (BLOCK_EFFECT_ID, PRESERVATION_EFFECT_ID, INSTALLER_EFFECT_ID),
}
CARD_STAMINA_BY_UPGRADE = (4, 1, 0, 0)

EFFECT_GROUP_STATUS_ENCHANT = "effect_group-visible-exam_status_enchant-000"
EFFECT_GROUP_FULL_POWER = "effect_group-visible-exam_full_power-000"

FULL_POWER_POINT_EFFECT_ENUM = 49
REENTRANT_TIMER_EFFECT_ENUMS = (103, 190)
FULL_POWER_POINT_CAP = 10

NATIVE_STATUS_CHANGE_ORDER = (
    "effect-executor-commits-status",
    "difference-entry-appended-after-minus-before",
    "iterate-difference-status-types-in-native-order",
    "evaluate-current-post-commit-field-state",
    "stable-filter-active-listeners-by-effect-type",
    "queue-listener-children-in-effect-order",
    "spend-listener-count-if-finite",
    "queue-triggered-card-status-callbacks",
    "execute-queued-children",
)


class EffectSource(str, Enum):
    DIRECT_CARD = "direct-card"
    STATUS_ENCHANT = "status-enchant"
    ITEM = "item"
    GIMMICK = "gimmick"


@dataclass(frozen=True, slots=True)
class NativeEffectContext:
    source: EffectSource
    playing_enchant_uid: int = 0
    playing_enchant_is_trigger_active: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.source, EffectSource):
            raise TypeError("source must be EffectSource")
        if (
            isinstance(self.playing_enchant_uid, bool)
            or not isinstance(self.playing_enchant_uid, int)
            or self.playing_enchant_uid < 0
        ):
            raise ValueError("playing_enchant_uid must be non-negative")
        if not isinstance(self.playing_enchant_is_trigger_active, bool):
            raise TypeError("playing_enchant_is_trigger_active must be bool")
        if self.source == EffectSource.STATUS_ENCHANT and self.playing_enchant_uid == 0:
            raise ValueError("status-enchant sources require a native UID")
        if self.source != EffectSource.STATUS_ENCHANT and (
            self.playing_enchant_uid != 0
            or self.playing_enchant_is_trigger_active
        ):
            raise ValueError("only status-enchant sources carry enchant flags")

    @property
    def status_change_listener_enabled(self) -> bool:
        if self.source in (EffectSource.ITEM, EffectSource.GIMMICK):
            return False
        if self.source == EffectSource.STATUS_ENCHANT:
            return self.playing_enchant_is_trigger_active
        return True


DIRECT_CARD_CONTEXT = NativeEffectContext(EffectSource.DIRECT_CARD)


@dataclass(frozen=True, slots=True)
class CommittedFullPowerPointDifference:
    before: int
    after: int
    post_stance: str
    source_effect_type: str
    context: NativeEffectContext = DIRECT_CARD_CONTEXT
    committed: bool = True

    def __post_init__(self) -> None:
        for name, value in (("before", self.before), ("after", self.after)):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= FULL_POWER_POINT_CAP
            ):
                raise ValueError(f"{name} must be between 0 and 10")
        if not isinstance(self.post_stance, str) or not self.post_stance:
            raise TypeError("post_stance must be non-empty text")
        if not isinstance(self.source_effect_type, str) or not self.source_effect_type:
            raise TypeError("source_effect_type must be non-empty text")
        if not isinstance(self.context, NativeEffectContext):
            raise TypeError("context must be NativeEffectContext")
        if not isinstance(self.committed, bool):
            raise TypeError("committed must be bool")

    @property
    def delta(self) -> int:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class StatusChangeFullPowerPointProgram:
    installer: Plan3Effect

    @property
    def rule(self):
        return self.installer.status_enchant

    @property
    def child(self) -> Plan3Effect:
        assert self.rule is not None
        return self.rule.effects[0]


@dataclass(frozen=True, slots=True)
class StatusChangeFullPowerPointState:
    full_power_points: int
    stance: str
    active: tuple[ActivePlan3StatusEnchant, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.full_power_points, bool)
            or not isinstance(self.full_power_points, int)
            or not 0 <= self.full_power_points <= FULL_POWER_POINT_CAP
        ):
            raise ValueError("full_power_points must be between 0 and 10")
        if not isinstance(self.stance, str) or not self.stance:
            raise TypeError("stance must be non-empty text")
        active = tuple(self.active)
        if not all(isinstance(item, ActivePlan3StatusEnchant) for item in active):
            raise TypeError("active must contain ActivePlan3StatusEnchant values")
        ids = tuple(item.instance_id for item in active)
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            raise ValueError("active listener instance IDs must be unique")
        object.__setattr__(self, "active", active)


@dataclass(frozen=True, slots=True)
class StatusChangeFullPowerPointFire:
    listener_sequence: int
    instance_id: str
    child_sequence: int
    child_effect_id: str
    child_effect_type_enum: int
    child_before: int
    child_after: int
    child_difference_created: bool
    child_reentry_enabled: bool


@dataclass(frozen=True, slots=True)
class StatusChangeFullPowerPointExecution:
    before: StatusChangeFullPowerPointState
    after: StatusChangeFullPowerPointState
    difference: CommittedFullPowerPointDifference
    fires: tuple[StatusChangeFullPowerPointFire, ...] = ()
    unresolved: tuple[str, ...] = ()
    source_gate_matched: bool = False
    trigger_matched: bool = False
    order: tuple[str, ...] = NATIVE_STATUS_CHANGE_ORDER

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def matches_status_change_full_power_point_trigger(trigger: object) -> bool:
    """Match only the audited phase-13/type-49/Preservation trigger."""

    return bool(
        isinstance(trigger, Plan3Trigger)
        and trigger.id == TRIGGER_ID
        and trigger.phase_types == (PHASE_STATUS_CHANGE,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and trigger.field_types == (FIELD_PRESERVATION_UP,)
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and trigger.effect_types == (EFFECT_FULL_POWER_POINT,)
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def matches_status_change_full_power_point_rule(rule: object) -> bool:
    """Match the exact listener identity and its one FullPowerPoint +1 child."""

    if not (
        isinstance(rule, Plan3StatusEnchantRule)
        and rule.id == STATUS_ENCHANT_ID
        and matches_status_change_full_power_point_trigger(rule.trigger)
        and len(rule.effects) == 1
    ):
        return False
    child = rule.effects[0]
    return bool(
        child.id == CHILD_EFFECT_ID
        and child.effect_type == EFFECT_FULL_POWER_POINT
        and child.value1 == CHILD_VALUE
        and child.value2 == 0
        and child.effect_count == 0
        and child.effect_turn == 0
        and not child.status_enchant_id
        and child.status_enchant is None
        and not child.chain_effect_id
        and not child.chain_effect_ids
        and child.chain_effect is None
        and child.trigger is None
        and not child.once
        and child.card_move_rule is None
    )


def _program_errors(installer: object) -> tuple[str, ...]:
    if not isinstance(installer, Plan3Effect):
        return ("typed-installer-required",)
    errors: list[str] = []
    if installer.id != INSTALLER_EFFECT_ID:
        errors.append("installer-id")
    if not (
        installer.effect_type == EFFECT_STATUS_ENCHANT
        and installer.value1 == 0
        and installer.value2 == 0
        and installer.effect_count == 0
        and installer.effect_turn == -1
        and installer.status_enchant_id == STATUS_ENCHANT_ID
        and not installer.chain_effect_id
        and not installer.chain_effect_ids
        and installer.chain_effect is None
        and installer.trigger is None
        and not installer.once
        and installer.card_move_rule is None
    ):
        errors.append("installer-shape")
    rule = installer.status_enchant
    if rule is None or rule.id != STATUS_ENCHANT_ID:
        errors.append("status-enchant")
        return tuple(errors)
    if not matches_status_change_full_power_point_trigger(rule.trigger):
        errors.append("trigger-shape")
    if len(rule.effects) != 1:
        errors.append("child-count")
        return tuple(errors)
    if not matches_status_change_full_power_point_rule(rule):
        errors.append("child-shape")
    return tuple(errors)


def load_status_change_full_power_point_program(
    database: Path = DEFAULT_DATABASE,
) -> StatusChangeFullPowerPointProgram:
    installer = load_plan3_effect(INSTALLER_EFFECT_ID, database)
    errors = _program_errors(installer)
    if errors:
        raise ValueError(f"unsupported status-change program: {errors!r}")
    return StatusChangeFullPowerPointProgram(installer)


def matches_status_change_full_power_point_installer(effect: object) -> bool:
    return isinstance(effect, Plan3Effect) and not _program_errors(effect)


def install_status_change_full_power_point_listener(
    program: StatusChangeFullPowerPointProgram,
    instance_id: str,
    *,
    native_uid: int,
) -> ActivePlan3StatusEnchant:
    if not isinstance(program, StatusChangeFullPowerPointProgram) or _program_errors(
        program.installer
    ):
        raise ValueError("exact status-change program is required")
    if not isinstance(instance_id, str) or not instance_id:
        raise TypeError("instance_id must be non-empty text")
    if isinstance(native_uid, bool) or not isinstance(native_uid, int) or native_uid <= 0:
        raise ValueError("native_uid must be positive")
    assert program.rule is not None
    return ActivePlan3StatusEnchant(
        instance_id=instance_id,
        source_id=program.installer.id,
        rule=program.rule,
        max_uses=0,
        uses=0,
        max_uses_per_turn=0,
        uses_this_turn=0,
        remaining_turns=-1,
        passing_turn_start=False,
        is_item_direct=False,
        turn_count=0,
        native_uid=native_uid,
    )


def _listener_errors(
    listener: ActivePlan3StatusEnchant,
    program: StatusChangeFullPowerPointProgram,
) -> tuple[str, ...]:
    errors: list[str] = []
    if listener.source_id != INSTALLER_EFFECT_ID or listener.rule != program.rule:
        errors.append(f"listener-program:{listener.instance_id}")
    if not (
        listener.max_uses == 0
        and listener.uses == 0
        and listener.max_uses_per_turn == 0
        and listener.uses_this_turn == 0
        and listener.remaining_turns == -1
        and listener.passing_turn_start is False
        and listener.is_item_direct is False
        and listener.turn_count == 0
        and listener.native_uid > 0
        and not listener.captured_card_guid
        and listener.is_encore_enchant is False
    ):
        errors.append(f"listener-lifecycle:{listener.instance_id}")
    return tuple(errors)


def execute_status_change_full_power_point(
    state: StatusChangeFullPowerPointState,
    difference: CommittedFullPowerPointDifference,
    program: StatusChangeFullPowerPointProgram,
) -> StatusChangeFullPowerPointExecution:
    """Evaluate one already committed Full Power Point difference.

    Listener selection is a snapshot in active collection order.  Each exact
    listener queues its one child once.  Child settlement happens afterward
    in that same order and is capped at ten; a cap no-op still represents the
    already selected listener firing, but creates no nested difference.
    """

    if not isinstance(state, StatusChangeFullPowerPointState):
        raise TypeError("state must be StatusChangeFullPowerPointState")
    if not isinstance(difference, CommittedFullPowerPointDifference):
        raise TypeError("difference must be CommittedFullPowerPointDifference")
    if not isinstance(program, StatusChangeFullPowerPointProgram):
        raise TypeError("program must be StatusChangeFullPowerPointProgram")
    errors = list(_program_errors(program.installer))
    if not difference.committed:
        errors.append("difference-not-committed")
    if state.full_power_points != difference.after:
        errors.append("state-does-not-match-committed-difference")
    if state.stance != difference.post_stance:
        errors.append("state-does-not-match-post-commit-stance")
    # An ExamFullPowerPoint executor is an increase.  Zero changes are not in
    # DifferenceStatusTypeList, and a negative delta with this effect type is
    # an impossible/unknown event rather than a predicate miss.
    if (
        difference.source_effect_type == EFFECT_FULL_POWER_POINT
        and difference.delta <= 0
    ):
        errors.append("full-power-point-effect-did-not-increase")

    status_change_listeners: list[tuple[int, ActivePlan3StatusEnchant]] = []
    for sequence, listener in enumerate(state.active):
        has_phase = PHASE_STATUS_CHANGE in listener.rule.trigger.phase_types
        if not has_phase:
            continue
        if listener.rule.trigger.phase_types != (PHASE_STATUS_CHANGE,):
            errors.append(f"unknown-status-change-phase-shape:{listener.instance_id}")
            continue
        errors.extend(_listener_errors(listener, program))
        status_change_listeners.append((sequence, listener))
    if errors:
        return StatusChangeFullPowerPointExecution(
            state,
            state,
            difference,
            unresolved=tuple(dict.fromkeys(errors)),
        )

    source_gate = difference.context.status_change_listener_enabled
    trigger_match = bool(
        source_gate
        and difference.source_effect_type == EFFECT_FULL_POWER_POINT
        and difference.delta > 0
        and difference.post_stance == STANCE_PRESERVATION
    )
    if not trigger_match:
        return StatusChangeFullPowerPointExecution(
            state,
            state,
            difference,
            source_gate_matched=source_gate,
            trigger_matched=False,
        )

    points = state.full_power_points
    fires: list[StatusChangeFullPowerPointFire] = []
    for sequence, listener in status_change_listeners:
        child_before = points
        points = min(FULL_POWER_POINT_CAP, points + CHILD_VALUE)
        child_after = points
        fires.append(
            StatusChangeFullPowerPointFire(
                listener_sequence=sequence,
                instance_id=listener.instance_id,
                child_sequence=0,
                child_effect_id=CHILD_EFFECT_ID,
                child_effect_type_enum=FULL_POWER_POINT_EFFECT_ENUM,
                child_before=child_before,
                child_after=child_after,
                child_difference_created=child_after != child_before,
                child_reentry_enabled=(
                    FULL_POWER_POINT_EFFECT_ENUM in REENTRANT_TIMER_EFFECT_ENUMS
                ),
            )
        )
    if not fires:
        return StatusChangeFullPowerPointExecution(
            state,
            state,
            difference,
            source_gate_matched=True,
            trigger_matched=True,
        )
    after = StatusChangeFullPowerPointState(points, state.stance, state.active)
    return StatusChangeFullPowerPointExecution(
        state,
        after,
        difference,
        tuple(fires),
        source_gate_matched=True,
        trigger_matched=True,
    )


def child_native_context(listener: ActivePlan3StatusEnchant) -> NativeEffectContext:
    """Return the exact non-reentrant context used by this listener's child."""

    if not isinstance(listener, ActivePlan3StatusEnchant) or listener.native_uid <= 0:
        raise ValueError("installed listener with positive native UID is required")
    return NativeEffectContext(
        EffectSource.STATUS_ENCHANT,
        playing_enchant_uid=listener.native_uid,
        playing_enchant_is_trigger_active=False,
    )


def matches_status_change_card(card: object) -> bool:
    """Return true only for one exact affected Master card version."""

    upgrade = getattr(card, "upgrade", None)
    if isinstance(upgrade, bool) or not isinstance(upgrade, int):
        return False
    return bool(
        getattr(card, "id", None) == CARD_ID
        and upgrade in CARD_UPGRADES
        and getattr(card, "plan_type", None) == PLAN3
        and getattr(card, "category", None) == CATEGORY_MENTAL
        and getattr(card, "stamina_cost", None)
        == CARD_STAMINA_BY_UPGRADE[upgrade]
        and getattr(card, "force_stamina_cost", None) == 0
        and getattr(card, "cost_type", None) == COST_STAMINA
        and getattr(card, "cost_value", None) == 0
        and getattr(card, "play_trigger", None) is None
        and getattr(card, "move_position_type", None) == MOVE_LOST
        and tuple(effect.id for effect in getattr(card, "effects", ()))
        == ORDERED_CARD_EFFECT_IDS[upgrade]
    )


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "CARD_ID",
    "CARD_STAMINA_BY_UPGRADE",
    "CARD_UPGRADES",
    "CHILD_EFFECT_ID",
    "CHILD_VALUE",
    "CO_BLOCKED_CARD_VERSION_COUNT",
    "CommittedFullPowerPointDifference",
    "DIRECT_CARD_CONTEXT",
    "EffectSource",
    "FULL_POWER_POINT_CAP",
    "FULL_POWER_POINT_EFFECT_ENUM",
    "INSTALLER_EFFECT_ID",
    "NATIVE_STATUS_CHANGE_ORDER",
    "NativeEffectContext",
    "ORDERED_CARD_EFFECT_IDS",
    "PHASE_STATUS_CHANGE",
    "REENTRANT_TIMER_EFFECT_ENUMS",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "STATUS_ENCHANT_ID",
    "StatusChangeFullPowerPointExecution",
    "StatusChangeFullPowerPointFire",
    "StatusChangeFullPowerPointProgram",
    "StatusChangeFullPowerPointState",
    "TRIGGER_ID",
    "child_native_context",
    "execute_status_change_full_power_point",
    "install_status_change_full_power_point_listener",
    "load_status_change_full_power_point_program",
    "matches_status_change_card",
    "matches_status_change_full_power_point_rule",
    "matches_status_change_full_power_point_trigger",
    "matches_status_change_full_power_point_installer",
]
