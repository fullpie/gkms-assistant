"""Standalone scheduler for ``ExamStanceChangeFromFullPower``.

The Android v3.2.3 turn-start path expires Full Power with
``ExamEffectUtility.UnsetFullPower``.  That method commits ``StanceReset``
before it appends a ``FullPower -> Unknown`` stance difference.  The later
difference scheduler selects phase 47 from the *old* stance, while the
ordinary field predicates read the already-reset calculation context.

This module is deliberately independent of ``plan3_engine``.  It catalogs
all five local Master trigger rows and the twelve affected Plan 3 card
versions, evaluates the exact current-state field shapes, and models native
listener count spending and ordered child-command creation.  Unknown row,
state, difference, or listener shapes fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence


PHASE = "ProduceExamPhaseType_ExamStanceChangeFromFullPower"
PHASE_RESET = "ProduceExamPhaseType_ExamStanceReset"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
FIELD_FULL_POWER_UP = "ProduceExamFieldStatusType_FullPowerUp"
FIELD_FULL_POWER_POINT_UP = "ProduceExamFieldStatusType_FullPowerPointUp"
FIELD_CARD_SEARCH_COUNT_UP = "ProduceExamFieldStatusType_CardSearchCountUp"
SEARCH_LOST_IDOL_CARD = "p_card_search-lost-p_card-03-ido-3_144"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
PLAN3 = "ProducePlanType_Plan3"
UNLIMITED = -1


class NativeStance(IntEnum):
    UNKNOWN = 0
    CONCENTRATION = 1
    PRESERVATION = 2
    FULL_POWER = 3
    OVER_PRESERVATION = 4


class Resolution(str, Enum):
    EXECUTABLE = "executable"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class TriggerRow:
    """One exact ``produce_exam_trigger`` row."""

    id: str
    phase_types: tuple[str, ...] = (PHASE,)
    phase_values: tuple[int, ...] = ()
    field_status_check_types: tuple[str, ...] = ()
    field_status_types: tuple[str, ...] = ()
    field_status_values: tuple[int, ...] = ()
    field_status_produce_card_search_ids: tuple[str, ...] = ()
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = MOVE_UNKNOWN
    effect_types: tuple[str, ...] = ()
    lesson_type: str = LESSON_UNKNOWN
    resolution: Resolution = field(init=False)
    unresolved_reasons: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("trigger id must be non-empty")
        sequence_fields = (
            "phase_types",
            "phase_values",
            "field_status_check_types",
            "field_status_types",
            "field_status_values",
            "field_status_produce_card_search_ids",
            "effect_types",
        )
        for name in sequence_fields:
            value = getattr(self, name)
            if isinstance(value, (str, bytes)):
                raise TypeError(f"{name} must be a sequence")
            object.__setattr__(self, name, tuple(value))
        reasons = _row_shape_errors(self)
        object.__setattr__(self, "unresolved_reasons", reasons)
        object.__setattr__(
            self,
            "resolution",
            Resolution.UNRESOLVED if reasons else Resolution.EXECUTABLE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_status_check_types": list(self.field_status_check_types),
            "field_status_types": list(self.field_status_types),
            "field_status_values": list(self.field_status_values),
            "field_status_produce_card_search_ids": list(
                self.field_status_produce_card_search_ids
            ),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "effect_types": list(self.effect_types),
            "lesson_type": self.lesson_type,
            "resolution": self.resolution.value,
            "unresolved_reasons": list(self.unresolved_reasons),
        }


def _row_shape_errors(row: TriggerRow) -> tuple[str, ...]:
    reasons: list[str] = []
    if row.phase_types != (PHASE,):
        reasons.append("phase-shape")
    if row.phase_values:
        reasons.append("phase-values")
    if row.field_status_check_types not in ((), (CHECK_NOT,)):
        reasons.append("field-check-shape")
    if len(row.field_status_types) > 1:
        reasons.append("multiple-field-types")
    if row.produce_card_search_id or row.upper_search_count or row.lower_search_count:
        reasons.append("top-level-card-search")
    if row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("card-move-filter")
    if row.effect_types:
        reasons.append("effect-type-filter")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-filter")

    field_type = row.field_status_types[0] if row.field_status_types else None
    if field_type is None:
        if (
            row.field_status_values
            or row.field_status_produce_card_search_ids
            or row.field_status_check_types
        ):
            reasons.append("payload-without-field")
    elif field_type == FIELD_FULL_POWER_UP:
        if row.field_status_values or row.field_status_produce_card_search_ids:
            reasons.append("full-power-up-payload")
    elif field_type == FIELD_FULL_POWER_POINT_UP:
        if row.field_status_values != (10,) or row.field_status_produce_card_search_ids:
            reasons.append("full-power-point-payload")
    elif field_type == FIELD_CARD_SEARCH_COUNT_UP:
        if (
            row.field_status_values != (1,)
            or row.field_status_produce_card_search_ids != (SEARCH_LOST_IDOL_CARD,)
            or row.field_status_check_types
        ):
            reasons.append("card-search-count-payload")
    else:
        reasons.append("field-type")
    return tuple(dict.fromkeys(reasons))


TRIGGER_BASE = "e_trigger-exam_stance_change_from_full_power"
TRIGGER_CARD_SEARCH = (
    "e_trigger-exam_stance_change_from_full_power-card_search_count_up-1-"
    "p_card_search-lost-p_card-03-ido-3_144"
)
TRIGGER_FULL_POWER_UP = (
    "e_trigger-exam_stance_change_from_full_power-full_power_up"
)
TRIGGER_NOT_FULL_POWER_POINT_10 = (
    "e_trigger-exam_stance_change_from_full_power-not-full_power_point_up-10"
)
TRIGGER_NOT_FULL_POWER_UP = (
    "e_trigger-exam_stance_change_from_full_power-not-full_power_up"
)

ALL_TRIGGER_ROWS: tuple[TriggerRow, ...] = (
    TriggerRow(TRIGGER_BASE),
    TriggerRow(
        TRIGGER_CARD_SEARCH,
        field_status_types=(FIELD_CARD_SEARCH_COUNT_UP,),
        field_status_values=(1,),
        field_status_produce_card_search_ids=(SEARCH_LOST_IDOL_CARD,),
    ),
    TriggerRow(TRIGGER_FULL_POWER_UP, field_status_types=(FIELD_FULL_POWER_UP,)),
    TriggerRow(
        TRIGGER_NOT_FULL_POWER_POINT_10,
        field_status_check_types=(CHECK_NOT,),
        field_status_types=(FIELD_FULL_POWER_POINT_UP,),
        field_status_values=(10,),
    ),
    TriggerRow(
        TRIGGER_NOT_FULL_POWER_UP,
        field_status_check_types=(CHECK_NOT,),
        field_status_types=(FIELD_FULL_POWER_UP,),
    ),
)
TRIGGER_BY_ID: Mapping[str, TriggerRow] = MappingProxyType(
    {row.id: row for row in ALL_TRIGGER_ROWS}
)
EXECUTABLE_TRIGGER_ROWS = ALL_TRIGGER_ROWS


def matches_trigger_contract(candidate: object) -> bool:
    """Return whether ``candidate`` is one of the five exact Master rows.

    The core engine uses shorter ``field_*`` attribute names than this
    standalone catalog, so both spellings are read explicitly.  An ID match
    alone is never sufficient: every imported trigger column must match.
    """

    trigger_id = getattr(candidate, "id", None)
    row = TRIGGER_BY_ID.get(trigger_id) if isinstance(trigger_id, str) else None
    if row is None:
        return False

    def sequence(*names: str) -> tuple[object, ...] | None:
        for name in names:
            if hasattr(candidate, name):
                value = getattr(candidate, name)
                if isinstance(value, (str, bytes)):
                    return None
                try:
                    return tuple(value)
                except TypeError:
                    return None
        return None

    return (
        sequence("phase_types") == row.phase_types
        and sequence("phase_values") == row.phase_values
        and sequence("field_status_check_types", "field_check_types")
        == row.field_status_check_types
        and sequence("field_status_types", "field_types")
        == row.field_status_types
        and sequence("field_status_values", "field_values")
        == row.field_status_values
        and sequence(
            "field_status_produce_card_search_ids", "field_card_search_ids"
        )
        == row.field_status_produce_card_search_ids
        and getattr(candidate, "produce_card_search_id", None)
        == row.produce_card_search_id
        and getattr(candidate, "upper_search_count", None)
        == row.upper_search_count
        and getattr(candidate, "lower_search_count", None)
        == row.lower_search_count
        and getattr(candidate, "card_move_position_type", None)
        == row.card_move_position_type
        and sequence("effect_types") == row.effect_types
        and getattr(candidate, "lesson_type", None) == row.lesson_type
    )


@dataclass(frozen=True, slots=True)
class PostCommitState:
    """The current context read by ``IsFieldStatusTriggerStatusEffect``."""

    stance: NativeStance
    full_power_points: int
    card_search_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "stance", NativeStance(self.stance))
        if type(self.full_power_points) is not int or self.full_power_points < 0:
            raise ValueError("full_power_points must be a non-negative integer")
        counts = dict(self.card_search_counts)
        if any(
            not isinstance(key, str)
            or type(value) is not int
            or value < 0
            for key, value in counts.items()
        ):
            raise ValueError("card search counts must map strings to non-negative integers")
        object.__setattr__(self, "card_search_counts", MappingProxyType(counts))


@dataclass(frozen=True, slots=True)
class StanceDifference:
    """The completed difference consumed by the native scheduler."""

    before_stance: NativeStance
    after_stance: NativeStance
    before_step: int
    after_step: int
    stance_change_count_before: int
    stance_change_count_after: int
    full_power_points: int
    stance_committed: bool = True
    difference_appended: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "before_stance", NativeStance(self.before_stance))
        object.__setattr__(self, "after_stance", NativeStance(self.after_stance))
        for name in (
            "before_step",
            "after_step",
            "stance_change_count_before",
            "stance_change_count_after",
            "full_power_points",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.stance_committed, bool) or not isinstance(
            self.difference_appended, bool
        ):
            raise TypeError("difference flags must be boolean")

    @classmethod
    def full_power_expiry(
        cls, *, before_step: int, stance_change_count: int, full_power_points: int
    ) -> "StanceDifference":
        return cls(
            NativeStance.FULL_POWER,
            NativeStance.UNKNOWN,
            before_step,
            0,
            stance_change_count,
            stance_change_count,
            full_power_points,
        )

    @property
    def is_native_expiry_shape(self) -> bool:
        return (
            self.before_stance is NativeStance.FULL_POWER
            and self.after_stance is NativeStance.UNKNOWN
            and self.after_step == 0
            and self.stance_change_count_before == self.stance_change_count_after
            and self.stance_committed
            and self.difference_appended
        )


@dataclass(frozen=True, slots=True)
class TriggerEvaluation:
    trigger_id: str
    resolution: Resolution
    fires: bool | None
    reasons: tuple[str, ...] = ()
    base_predicate: bool | None = None
    not_applied: bool = False


def evaluate_trigger(
    trigger: TriggerRow | object,
    state: PostCommitState | object,
    difference: StanceDifference | object,
    *,
    event_phase: str = PHASE,
) -> TriggerEvaluation:
    """Evaluate phase 47 using old stance plus the post-commit context."""

    trigger_id = str(getattr(trigger, "id", "<unknown>"))
    if not isinstance(trigger, TriggerRow):
        return TriggerEvaluation(trigger_id, Resolution.UNRESOLVED, None, ("trigger-shape",))
    if trigger.resolution is Resolution.UNRESOLVED:
        return TriggerEvaluation(
            trigger.id, trigger.resolution, None, trigger.unresolved_reasons
        )
    if not isinstance(state, PostCommitState):
        return TriggerEvaluation(trigger.id, Resolution.UNRESOLVED, None, ("state-shape",))
    if not isinstance(difference, StanceDifference):
        return TriggerEvaluation(trigger.id, Resolution.UNRESOLVED, None, ("difference-shape",))
    if not isinstance(event_phase, str):
        return TriggerEvaluation(trigger.id, Resolution.UNRESOLVED, None, ("event-phase-shape",))
    if event_phase != PHASE:
        return TriggerEvaluation(trigger.id, Resolution.EXECUTABLE, False, ("phase-mismatch",))
    if not difference.stance_committed or not difference.difference_appended:
        return TriggerEvaluation(
            trigger.id, Resolution.UNRESOLVED, None, ("scheduler-before-commit-or-append",)
        )
    if difference.after_stance is not state.stance:
        return TriggerEvaluation(
            trigger.id, Resolution.UNRESOLVED, None, ("post-state-difference-mismatch",)
        )
    if difference.before_stance is not NativeStance.FULL_POWER:
        return TriggerEvaluation(
            trigger.id, Resolution.EXECUTABLE, False, ("old-stance-is-not-full-power",)
        )

    field_type = trigger.field_status_types[0] if trigger.field_status_types else None
    if field_type is None:
        base = True
    elif field_type == FIELD_FULL_POWER_UP:
        base = state.stance is NativeStance.FULL_POWER
    elif field_type == FIELD_FULL_POWER_POINT_UP:
        base = state.full_power_points >= trigger.field_status_values[0]
    elif field_type == FIELD_CARD_SEARCH_COUNT_UP:
        search_id = trigger.field_status_produce_card_search_ids[0]
        if search_id not in state.card_search_counts:
            return TriggerEvaluation(
                trigger.id,
                Resolution.UNRESOLVED,
                None,
                (f"card-search-count-unavailable:{search_id}",),
            )
        base = state.card_search_counts[search_id] >= trigger.field_status_values[0]
    else:  # protected by TriggerRow resolution; retain fail-closed defense.
        return TriggerEvaluation(trigger.id, Resolution.UNRESOLVED, None, ("field-type",))

    not_applied = trigger.field_status_check_types == (CHECK_NOT,)
    return TriggerEvaluation(
        trigger.id,
        Resolution.EXECUTABLE,
        not base if not_applied else base,
        base_predicate=base,
        not_applied=not_applied,
    )


@dataclass(frozen=True, slots=True)
class ListenerRow:
    id: str
    installer_effect_id: str
    trigger_id: str
    ordered_effect_ids: tuple[str, ...]
    effect_count: int
    effect_turn: int
    effect_value1: int
    effect_value2: int
    effect_group_ids: tuple[str, ...]
    origin: str = "card-direct"

    def __post_init__(self) -> None:
        if self.trigger_id not in TRIGGER_BY_ID:
            raise ValueError("listener trigger is not in the exact catalog")
        if type(self.effect_count) is not int or self.effect_count < 0:
            raise ValueError("effect_count must be non-negative")
        if type(self.effect_turn) is not int:
            raise TypeError("effect_turn must be an integer")
        object.__setattr__(self, "ordered_effect_ids", tuple(self.ordered_effect_ids))
        object.__setattr__(self, "effect_group_ids", tuple(self.effect_group_ids))

    @property
    def total_limit(self) -> int:
        return self.effect_count if self.effect_count > 0 else UNLIMITED

    @property
    def per_turn_limit(self) -> int:
        return self.effect_value1 if self.effect_value1 > 0 else UNLIMITED

    @property
    def remaining_turns(self) -> int:
        return self.effect_turn


GROUP_STATUS_ENCHANT = "effect_group-visible-exam_status_enchant-000"
GROUP_PRESERVATION = "effect_group-visible-exam_preservation-000"
GROUP_FULL_POWER = "effect_group-visible-exam_full_power-000"

CARD_LISTENER_ROWS: tuple[ListenerRow, ...] = (
    ListenerRow(
        "enchant-p_card-03-act-2_071-enc01",
        "e_effect-exam_status_enchant-01-inf-enchant-p_card-03-act-2_071-enc01",
        TRIGGER_NOT_FULL_POWER_UP,
        ("e_effect-exam_preservation-0002",),
        1,
        -1,
        0,
        0,
        (GROUP_STATUS_ENCHANT, GROUP_PRESERVATION),
    ),
    ListenerRow(
        "enchant-p_card-03-ido-3_235-enc01",
        "e_effect-exam_status_enchant-02-inf-enchant-p_card-03-ido-3_235-enc01",
        TRIGGER_NOT_FULL_POWER_UP,
        ("e_effect-exam_full_power_point-0010",),
        2,
        -1,
        0,
        0,
        (GROUP_STATUS_ENCHANT, GROUP_FULL_POWER),
    ),
    ListenerRow(
        "enchant-p_card-03-ido-3_235-enc02",
        "e_effect-exam_status_enchant-02-inf-enchant-p_card-03-ido-3_235-enc02",
        TRIGGER_NOT_FULL_POWER_UP,
        ("e_effect-exam_full_power_point-0009",),
        2,
        -1,
        0,
        0,
        (GROUP_STATUS_ENCHANT, GROUP_FULL_POWER),
    ),
    ListenerRow(
        "enchant-p_card-03-men-2_078-enc01",
        "e_effect-exam_status_enchant-03-inf-enchant-p_card-03-men-2_078-enc01",
        TRIGGER_NOT_FULL_POWER_UP,
        ("e_effect-exam_preservation-0001",),
        3,
        -1,
        0,
        0,
        (GROUP_STATUS_ENCHANT, GROUP_PRESERVATION),
    ),
)
LISTENER_BY_ID: Mapping[str, ListenerRow] = MappingProxyType(
    {row.id: row for row in CARD_LISTENER_ROWS}
)

# These exact Master aliases are item-owned consumers of the same phase
# family.  They are cataloged to make the family inventory complete, but are
# intentionally outside the twelve card-version coverage rows.
ITEM_STATUS_ENCHANT_IDS: tuple[str, ...] = (
    "enchant-p_item_effect_03-3-168-0-enc01",
    "enchant-p_item_effect_03-3-168-1-enc01",
    "enchant-pitem_03-3-168-0-enc01",
    "enchant-pitem_03-3-168-1-enc01",
)
ITEM_CARD_MOVE_EFFECT = (
    "e_effect-exam_card_move-p_card_search-deck_all-p_card-03-ido-3_144-"
    "hold-all-0_0"
)
ITEM_GROW_EFFECT_0 = (
    "e_effect-exam_add_grow_effect-p_card_search-deck_all-p_card-03-ido-3_144-"
    "all-0_0-g_effect-full_power_point_add-2-g_effect-lesson_add-5-"
    "g_effect-cost_penetrate_add-2"
)
ITEM_GROW_EFFECT_1 = (
    "e_effect-exam_add_grow_effect-p_card_search-deck_all-p_card-03-ido-3_144-"
    "all-0_0-g_effect-full_power_point_add-2-g_effect-lesson_add-12-"
    "g_effect-cost_penetrate_add-2"
)
ITEM_STATUS_ENCHANT_EFFECTS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        ITEM_STATUS_ENCHANT_IDS[0]: (ITEM_CARD_MOVE_EFFECT, ITEM_GROW_EFFECT_0),
        ITEM_STATUS_ENCHANT_IDS[1]: (ITEM_CARD_MOVE_EFFECT, ITEM_GROW_EFFECT_1),
        ITEM_STATUS_ENCHANT_IDS[2]: (ITEM_CARD_MOVE_EFFECT, ITEM_GROW_EFFECT_0),
        ITEM_STATUS_ENCHANT_IDS[3]: (ITEM_CARD_MOVE_EFFECT, ITEM_GROW_EFFECT_1),
    }
)


@dataclass(frozen=True, slots=True)
class CardVersion:
    card_id: str
    upgrade: int
    category: str
    stamina: int
    move_position_type: str
    ordered_effect_ids: tuple[str, ...]
    listener_id: str

    @property
    def version(self) -> str:
        return f"{self.card_id}+{self.upgrade}"

    @property
    def listener_slot(self) -> int:
        installer = LISTENER_BY_ID[self.listener_id].installer_effect_id
        return self.ordered_effect_ids.index(installer)


def _card(
    card_id: str,
    upgrade: int,
    category: str,
    stamina: int,
    effects: Sequence[str],
    listener_id: str,
) -> CardVersion:
    return CardVersion(
        card_id,
        upgrade,
        category,
        stamina,
        "ProduceCardMovePositionType_Lost",
        tuple(effects),
        listener_id,
    )


_ACT_LISTENER = CARD_LISTENER_ROWS[0]
_IDO_LISTENER_1 = CARD_LISTENER_ROWS[1]
_IDO_LISTENER_0 = CARD_LISTENER_ROWS[2]
_MEN_LISTENER = CARD_LISTENER_ROWS[3]
ACTIVE = "ProduceCardCategory_ActiveSkill"
MENTAL = "ProduceCardCategory_MentalSkill"

AFFECTED_CARD_VERSIONS: tuple[CardVersion, ...] = (
    _card("p_card-03-act-2_071", 0, ACTIVE, 5, ("e_effect-exam_full_power_point-0003", "e_effect-exam_lesson_full_power_point-0028-1000-01", _ACT_LISTENER.installer_effect_id), _ACT_LISTENER.id),
    _card("p_card-03-act-2_071", 1, ACTIVE, 5, ("e_effect-exam_full_power_point-0004", "e_effect-exam_lesson_full_power_point-0028-2000-01", _ACT_LISTENER.installer_effect_id), _ACT_LISTENER.id),
    _card("p_card-03-act-2_071", 2, ACTIVE, 5, ("e_effect-exam_full_power_point-0005", "e_effect-exam_lesson_full_power_point-0028-2000-01", _ACT_LISTENER.installer_effect_id), _ACT_LISTENER.id),
    _card("p_card-03-act-2_071", 3, ACTIVE, 4, ("e_effect-exam_full_power_point-0005", "e_effect-exam_lesson_full_power_point-0028-2000-01", _ACT_LISTENER.installer_effect_id), _ACT_LISTENER.id),
    _card("p_card-03-ido-3_235", 0, MENTAL, 8, ("e_effect-exam_playable_value_add-01", _IDO_LISTENER_0.installer_effect_id), _IDO_LISTENER_0.id),
    _card("p_card-03-ido-3_235", 1, MENTAL, 6, ("e_effect-exam_playable_value_add-01", _IDO_LISTENER_1.installer_effect_id), _IDO_LISTENER_1.id),
    _card("p_card-03-ido-3_235", 2, MENTAL, 5, ("e_effect-exam_block-0002", "e_effect-exam_playable_value_add-01", _IDO_LISTENER_1.installer_effect_id), _IDO_LISTENER_1.id),
    _card("p_card-03-ido-3_235", 3, MENTAL, 4, ("e_effect-exam_block-0002", "e_effect-exam_playable_value_add-01", _IDO_LISTENER_1.installer_effect_id), _IDO_LISTENER_1.id),
    *tuple(
        _card("p_card-03-men-2_078", upgrade, MENTAL, stamina, ("e_effect-exam_preservation-0001", "e_effect-exam_playable_value_add-01", _MEN_LISTENER.installer_effect_id), _MEN_LISTENER.id)
        for upgrade, stamina in enumerate((6, 3, 2, 1))
    ),
)


@dataclass(frozen=True, slots=True)
class ActiveListener:
    listener_id: str
    total_remaining: int
    per_turn_remaining: int = UNLIMITED
    remaining_turns: int = UNLIMITED
    origin: str = "card-direct"

    def __post_init__(self) -> None:
        for name in ("total_remaining", "per_turn_remaining", "remaining_turns"):
            value = getattr(self, name)
            if type(value) is not int or value < UNLIMITED:
                raise ValueError(f"{name} must be -1 or non-negative")

    @classmethod
    def install(cls, listener: ListenerRow) -> "ActiveListener":
        return cls(
            listener.id,
            listener.total_limit,
            listener.per_turn_limit,
            listener.remaining_turns,
            listener.origin,
        )

    @property
    def can_fire(self) -> bool:
        return self.total_remaining != 0 and self.per_turn_remaining != 0


@dataclass(frozen=True, slots=True)
class QueuedListenerEffects:
    listener_id: str
    ordered_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]
    total_before: int
    total_after: int
    count_spent_at_queue_time: bool = True


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    resolution: Resolution
    before: tuple[ActiveListener, ...]
    after: tuple[ActiveListener, ...]
    evaluations: tuple[TriggerEvaluation, ...]
    queued: tuple[QueuedListenerEffects, ...]
    removed_listener_ids: tuple[str, ...]
    operations: tuple[str, ...]
    reasons: tuple[str, ...] = ()


FULL_POWER_EXPIRY_ORDER: tuple[str, ...] = (
    "clear_prior_difference_data",
    "capture_full_power_before_step_gauge_and_stance_change_count",
    "stance_reset_commit",
    "create_and_set_full_power_to_unknown_difference",
    "append_difference",
    "select_phase47_listeners_from_old_full_power_and_post_commit_context",
    "spend_listener_count_while_building_commands",
    "queue_listener_child_effects_in_master_order",
    "queue_phase47_card_status_callbacks",
    "queue_phase40_stance_reset_listeners",
    "queue_phase40_card_status_callbacks",
    "only_then_consume_gauge_and_schedule_optional_full_power_reentry",
)


def schedule_full_power_exit(
    listeners: Sequence[ActiveListener] | object,
    state: PostCommitState | object,
    difference: StanceDifference | object,
    *,
    event_phase: str = PHASE,
) -> ScheduleResult:
    """Build one phase-47 listener batch without executing child effects."""

    if isinstance(listeners, (str, bytes)):
        listeners = ()
        input_error = "listener-sequence-shape"
    else:
        try:
            listeners = tuple(listeners)  # type: ignore[arg-type]
            input_error = ""
        except TypeError:
            listeners = ()
            input_error = "listener-sequence-shape"
    if input_error or any(not isinstance(item, ActiveListener) for item in listeners):
        return ScheduleResult(
            Resolution.UNRESOLVED,
            tuple(item for item in listeners if isinstance(item, ActiveListener)),
            tuple(item for item in listeners if isinstance(item, ActiveListener)),
            (),
            (),
            (),
            (),
            (input_error or "listener-shape",),
        )

    evaluations: list[TriggerEvaluation] = []
    reasons: list[str] = []
    for active in listeners:
        row = LISTENER_BY_ID.get(active.listener_id)
        if row is None or active.origin != "card-direct":
            reasons.append(f"listener-unresolved:{active.listener_id}")
            continue
        evaluation = evaluate_trigger(
            TRIGGER_BY_ID[row.trigger_id], state, difference, event_phase=event_phase
        )
        evaluations.append(evaluation)
        if evaluation.fires is None:
            reasons.extend(evaluation.reasons)
    if reasons:
        return ScheduleResult(
            Resolution.UNRESOLVED,
            listeners,
            listeners,
            tuple(evaluations),
            (),
            (),
            (),
            tuple(dict.fromkeys(reasons)),
        )

    after: list[ActiveListener] = []
    queued: list[QueuedListenerEffects] = []
    removed: list[str] = []
    eval_iter = iter(evaluations)
    for active in listeners:
        row = LISTENER_BY_ID[active.listener_id]
        evaluation = next(eval_iter)
        if not evaluation.fires or not active.can_fire:
            after.append(active)
            continue
        total = active.total_remaining - 1 if active.total_remaining > 0 else UNLIMITED
        per_turn = (
            active.per_turn_remaining - 1
            if active.per_turn_remaining > 0
            else UNLIMITED
        )
        spent = replace(active, total_remaining=total, per_turn_remaining=per_turn)
        queued.append(
            QueuedListenerEffects(
                row.id,
                row.ordered_effect_ids,
                row.effect_group_ids,
                active.total_remaining,
                total,
            )
        )
        if total == 0:
            removed.append(active.listener_id)
        else:
            after.append(spent)
    return ScheduleResult(
        Resolution.EXECUTABLE,
        listeners,
        tuple(after),
        tuple(evaluations),
        tuple(queued),
        tuple(removed),
        FULL_POWER_EXPIRY_ORDER,
    )


def catalog_to_dict() -> dict[str, Any]:
    """Return a JSON-friendly summary without importing the core engine."""

    return {
        "phase": PHASE,
        "trigger_rows": [row.to_dict() for row in ALL_TRIGGER_ROWS],
        "executable_trigger_row_count": len(EXECUTABLE_TRIGGER_ROWS),
        "card_listener_ids": [row.id for row in CARD_LISTENER_ROWS],
        "item_status_enchants": {
            listener_id: list(effect_ids)
            for listener_id, effect_ids in ITEM_STATUS_ENCHANT_EFFECTS.items()
        },
        "affected_card_versions": [
            {
                "card_id": card.card_id,
                "upgrade": card.upgrade,
                "category": card.category,
                "stamina": card.stamina,
                "move_position_type": card.move_position_type,
                "ordered_effect_ids": list(card.ordered_effect_ids),
                "listener_id": card.listener_id,
                "listener_slot": card.listener_slot,
            }
            for card in AFFECTED_CARD_VERSIONS
        ],
        "affected_card_version_count": len(AFFECTED_CARD_VERSIONS),
        "affected_unique_card_count": len({card.card_id for card in AFFECTED_CARD_VERSIONS}),
        "sole_unlock_card_version_count": 0,
        "whole_card_direct_delta": 12,
        "core_integrated": True,
        "native_order": list(FULL_POWER_EXPIRY_ORDER),
    }


__all__ = [
    "ACTIVE",
    "AFFECTED_CARD_VERSIONS",
    "ALL_TRIGGER_ROWS",
    "ActiveListener",
    "CARD_LISTENER_ROWS",
    "CHECK_NOT",
    "EXECUTABLE_TRIGGER_ROWS",
    "FIELD_CARD_SEARCH_COUNT_UP",
    "FIELD_FULL_POWER_POINT_UP",
    "FIELD_FULL_POWER_UP",
    "FULL_POWER_EXPIRY_ORDER",
    "ITEM_STATUS_ENCHANT_IDS",
    "ITEM_STATUS_ENCHANT_EFFECTS",
    "LISTENER_BY_ID",
    "MENTAL",
    "NativeStance",
    "PHASE",
    "PHASE_RESET",
    "PostCommitState",
    "Resolution",
    "SEARCH_LOST_IDOL_CARD",
    "StanceDifference",
    "TRIGGER_BY_ID",
    "TRIGGER_CARD_SEARCH",
    "TRIGGER_FULL_POWER_UP",
    "TRIGGER_NOT_FULL_POWER_POINT_10",
    "TRIGGER_NOT_FULL_POWER_UP",
    "TriggerRow",
    "catalog_to_dict",
    "evaluate_trigger",
    "matches_trigger_contract",
    "schedule_full_power_exit",
]
