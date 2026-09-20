"""Standalone native planner for three exact Plan 3 move-effect cards.

The Android command stream observes a completed HandAdd/PoolAdd difference,
queues the card's move effects in Master order, and only then marks the card's
``isMoveProduceExamEffectUseInTurn`` flag.  This module keeps that boundary
explicit.  It reuses :class:`Plan3NativeState` for every zone, GUID, RNG and
runtime-growth mutation; the only sidecar is the native per-card turn flag
which the current Plan3NativeCard projection does not retain.

This is deliberately not an engine integration.  Unknown cards with move
effects, unknown effect shapes, missing Select input, and unproved Hold
overflow callback ordering stop the bounded callback queue fail-closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Mapping

from .card_search import ProduceCardSearchRule
from .logic_engine import MasterCard, load_master_card
from .master_db import DEFAULT_DATABASE
from .nia_add_grow_effect import NiaAddGrowMutation
from .plan3_engine import (
    EFFECT_ADD_GROW,
    EFFECT_CARD_DRAW,
    EFFECT_CARD_MOVE,
    FIELD_CONCENTRATION_UP,
    FIELD_STANCE_CHANGE_COUNT_UP,
    LESSON_UNKNOWN,
    MOVE_HAND,
    MOVE_HOLD,
    MOVE_UNKNOWN,
    PHASE_NONE,
    PICK_COUNT_UNKNOWN,
    PICK_RANGE_ALL,
    PICK_RANGE_SELECT,
    PICK_RANGE_UNKNOWN,
    STANCE_CONCENTRATION,
    Plan3Card,
    Plan3Effect,
    Plan3State,
    evaluate_plan3_trigger,
    load_plan3_card,
    load_plan3_effect,
)
from .plan3_force_play_card_search import (
    HOLD_ALL_EFFECT_ID,
    ForcePlayPlanResult,
    contract_for_effect as force_play_contract_for_effect,
    plan_force_play_card_search,
)
from .plan3_native_search import apply_plan3_native_add_grow_effects
from .plan3_native_state import (
    Plan3NativeState,
    Plan3NativeStateError,
    SupportInputs,
)


CARD_A = "p_card-03-act-100_013"
CARD_B = "p_card-03-act-100_018"
CARD_C = "p_card-03-men-100_015"

MOVE_TRIGGER_HAND = "ProduceCardMoveEffectTriggerType_Hand"
MOVE_TRIGGER_HOLD = "ProduceCardMoveEffectTriggerType_Hold"

EFFECT_A_DRAW = "e_effect-exam_card_draw-0001"
EFFECT_B_HOLD_GROW = (
    "e_effect-exam_add_grow_effect-p_card_search-hold-all-0_0-"
    "g_effect-lesson_count_add-1"
)
EFFECT_C_MOVE = (
    "e_effect-exam_card_move-p_card_search-deck_grave-hold-select-1_1"
)

TRIGGER_A = "e_trigger-none-concentration_up-2"
TRIGGER_C = "e_trigger-none-stance_change_count_up-4"

EFFECT_B_LESSON = "e_effect-exam_lesson_full_power_point-0050-3500-01"
EFFECT_B_STAMINA = "e_effect-exam_stamina_reduce-1000"
EFFECT_C_GROW = (
    "e_effect-exam_add_grow_effect-p_card_search-hold-all-0_0-"
    "g_effect-lesson_add-5"
)
EFFECT_C_PLAYABLE = "e_effect-exam_playable_value_add-01"
EFFECT_C_TIMER = (
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_preservation-0001"
)
EFFECT_C_TIMER_CHILD = "e_effect-exam_preservation-0001"

EFFECT_STAMINA_REDUCE = "ProduceExamEffectType_ExamStaminaReduce"
EFFECT_LESSON_FULL_POWER_POINT = (
    "ProduceExamEffectType_ExamLessonFullPowerPoint"
)
EFFECT_FORCE_PLAY = "ProduceExamEffectType_ExamForcePlayCardSearch"
EFFECT_PLAYABLE_ADD = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_TIMER = "ProduceExamEffectType_ExamEffectTimer"
EFFECT_PRESERVATION = "ProduceExamEffectType_ExamPreservation"

SEARCH_HOLD = "p_card_search-hold"
SEARCH_DECK_GRAVE = "p_card_search-deck_grave"


class LegendMoveEffectError(ValueError):
    """A strict input or Master-shape error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True, slots=True)
class LegendCardSpec:
    card_id: str
    category: str
    stamina: int
    play_trigger_id: str
    move_position_type: str
    play_effect_ids: tuple[str, ...]
    move_trigger_type: str
    move_effect_ids: tuple[str, ...]
    status_enchant_id: str = ""
    central_missing_hooks: tuple[str, ...] = ()

    @property
    def upgrade(self) -> int:
        return 0

    @property
    def whole_card_core_ready(self) -> bool:
        return not self.central_missing_hooks


CARD_SPECS: Mapping[str, LegendCardSpec] = {
    CARD_A: LegendCardSpec(
        CARD_A,
        "ProduceCardCategory_ActiveSkill",
        1,
        TRIGGER_A,
        "ProduceCardMovePositionType_Lost",
        ("e_effect-exam_lesson-0001-01",),
        MOVE_TRIGGER_HAND,
        (EFFECT_A_DRAW,),
        (
            "card_enchant-e_trigger-exam_stance_change_count_interval-2-12-"
            "g_effect-lesson_add-20-g_effect-lesson_count_add-1-"
            "g_effect-cost_add-1"
        ),
        (),
    ),
    CARD_B: LegendCardSpec(
        CARD_B,
        "ProduceCardCategory_ActiveSkill",
        0,
        "",
        "ProduceCardMovePositionType_Lost",
        (EFFECT_B_LESSON, EFFECT_B_STAMINA),
        MOVE_TRIGGER_HOLD,
        (EFFECT_B_HOLD_GROW,),
        central_missing_hooks=(),
    ),
    CARD_C: LegendCardSpec(
        CARD_C,
        "ProduceCardCategory_MentalSkill",
        3,
        TRIGGER_C,
        "ProduceCardMovePositionType_Grave",
        (EFFECT_C_GROW, HOLD_ALL_EFFECT_ID, EFFECT_C_PLAYABLE, EFFECT_C_TIMER),
        MOVE_TRIGGER_HAND,
        (EFFECT_C_MOVE,),
        central_missing_hooks=(),
    ),
}


@dataclass(frozen=True, slots=True)
class LegendCardContract:
    spec: LegendCardSpec
    master: MasterCard
    card: Plan3Card
    move_effects: tuple[Plan3Effect, ...]

    @property
    def whole_card_core_ready(self) -> bool:
        return self.spec.whole_card_core_ready

    @property
    def central_missing_hooks(self) -> tuple[str, ...]:
        return self.spec.central_missing_hooks


def _expect(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise LegendMoveEffectError(code, detail)


def _neutral_trigger_shape(card: Plan3Card, *, field: str, value: int) -> bool:
    trigger = card.play_trigger
    return bool(
        trigger is not None
        and trigger.id in {TRIGGER_A, TRIGGER_C}
        and trigger.phase_types == (PHASE_NONE,)
        and trigger.phase_values == ()
        and trigger.field_check_types == ()
        and trigger.field_types == (field,)
        and trigger.field_values == (value,)
        and trigger.field_card_search_ids == ()
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and trigger.effect_types == ()
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def _neutral_move_rule(effect: Plan3Effect) -> bool:
    rule = effect.card_move_rule
    return bool(
        rule is not None
        and not rule.target_card_id
        and rule.target_upgrade == 0
        and rule.target_effect_type == "ProduceExamEffectType_Unknown"
        and not rule.pick_reference_search_id
        and rule.pick_count_type == PICK_COUNT_UNKNOWN
        and not rule.second_search_id
        and rule.second_pick_range_type == PICK_RANGE_UNKNOWN
        and not rule.second_pick_reference_search_id
        and rule.second_pick_count_type == PICK_COUNT_UNKNOWN
        and rule.second_pick_count_min == 0
        and rule.second_pick_count_max == 0
        and not rule.chain_effect_ids
        and not rule.card_status_enchant_id
    )


@lru_cache(maxsize=12)
def load_legend_card_contract(
    card_id: str, database: Path = DEFAULT_DATABASE
) -> LegendCardContract:
    """Load and validate one of the three complete +0 Master rows."""

    spec = CARD_SPECS.get(card_id)
    if spec is None:
        raise LegendMoveEffectError("unsupported-card", card_id)
    database = Path(database)
    master = load_master_card(card_id, 0, database)
    card = load_plan3_card(card_id, 0, database)
    move_effects = tuple(
        load_plan3_effect(effect_id, database) for effect_id in master.move_effect_ids
    )
    _expect(master.upgrade == 0, "master-upgrade", card_id)
    _expect(master.plan_type == "ProducePlanType_Plan3", "master-plan", card_id)
    _expect(master.category == spec.category, "master-category", card_id)
    _expect(master.stamina_cost == spec.stamina, "master-stamina", card_id)
    _expect(master.play_trigger_id == spec.play_trigger_id, "master-play-trigger", card_id)
    _expect(master.move_position_type == spec.move_position_type, "master-play-move", card_id)
    _expect(tuple(effect.id for effect in master.effects) == spec.play_effect_ids, "master-play-effect-order", card_id)
    _expect(master.move_effect_trigger_type == spec.move_trigger_type, "master-move-trigger", card_id)
    _expect(master.move_effect_ids == spec.move_effect_ids, "master-move-effects", card_id)
    _expect(master.move_trigger_ids == (), "master-move-trigger-list", card_id)
    _expect(master.produce_card_status_enchant_id == spec.status_enchant_id, "master-card-enchant", card_id)

    if card_id == CARD_A:
        _expect(_neutral_trigger_shape(card, field=FIELD_CONCENTRATION_UP, value=2), "trigger-a-shape")
        effect = move_effects[0]
        _expect(
            effect.id == EFFECT_A_DRAW
            and effect.effect_type == EFFECT_CARD_DRAW
            and effect.value1 == 1
            and effect.value2 == effect.effect_count == effect.effect_turn == 0
            and effect.card_move_rule is None,
            "move-effect-a-shape",
        )
    elif card_id == CARD_B:
        _expect(card.play_trigger is None, "trigger-b-shape")
        effect = move_effects[0]
        rule = effect.card_move_rule
        _expect(
            effect.id == EFFECT_B_HOLD_GROW
            and effect.effect_type == EFFECT_ADD_GROW
            and _neutral_move_rule(effect)
            and rule is not None
            and rule.search_id == SEARCH_HOLD
            and rule.destination == MOVE_UNKNOWN
            and rule.pick_range_type == PICK_RANGE_ALL
            and rule.pick_count_min == rule.pick_count_max == 0
            and rule.card_grow_effect_ids == ("g_effect-lesson_count_add-1",)
            and rule.effect_group_ids == ("effect_group-visible-exam_add_grow_effect-000",),
            "move-effect-b-shape",
        )
        _validate_b_direct(card)
    else:
        _expect(_neutral_trigger_shape(card, field=FIELD_STANCE_CHANGE_COUNT_UP, value=4), "trigger-c-shape")
        effect = move_effects[0]
        rule = effect.card_move_rule
        _expect(
            effect.id == EFFECT_C_MOVE
            and effect.effect_type == EFFECT_CARD_MOVE
            and _neutral_move_rule(effect)
            and rule is not None
            and rule.search_id == SEARCH_DECK_GRAVE
            and rule.destination == MOVE_HOLD
            and rule.pick_range_type == PICK_RANGE_SELECT
            and rule.pick_count_min == rule.pick_count_max == 1
            and not rule.card_grow_effect_ids
            and not rule.effect_group_ids,
            "move-effect-c-shape",
        )
        _validate_c_direct(card)
    return LegendCardContract(spec, master, card, move_effects)


def _validate_b_direct(card: Plan3Card) -> None:
    first, second = card.effects
    _expect(
        first.id == EFFECT_B_LESSON
        and first.effect_type == EFFECT_LESSON_FULL_POWER_POINT
        and (first.value1, first.value2, first.effect_count, first.effect_turn)
        == (50, 3500, 1, 0),
        "direct-b-lesson-shape",
    )
    _expect(
        second.id == EFFECT_B_STAMINA
        and second.effect_type == EFFECT_STAMINA_REDUCE
        and (second.value1, second.value2, second.effect_count, second.effect_turn)
        == (1000, 0, 0, 0),
        "direct-b-stamina-shape",
    )


def _validate_c_direct(card: Plan3Card) -> None:
    grow, force, playable, timer = card.effects
    _expect(grow.id == EFFECT_C_GROW and grow.effect_type == EFFECT_ADD_GROW, "direct-c-grow-shape")
    _expect(force.id == HOLD_ALL_EFFECT_ID and force.effect_type == EFFECT_FORCE_PLAY, "direct-c-force-shape")
    _expect(
        playable.id == EFFECT_C_PLAYABLE
        and playable.effect_type == EFFECT_PLAYABLE_ADD
        and playable.effect_count == 1,
        "direct-c-playable-shape",
    )
    _expect(
        timer.id == EFFECT_C_TIMER
        and timer.effect_type == EFFECT_TIMER
        and (timer.value1, timer.value2, timer.effect_count, timer.effect_turn)
        == (1, 0, 1, 0)
        and timer.chain_effect_id == EFFECT_C_TIMER_CHILD
        and timer.chain_effect is not None
        and timer.chain_effect.id == EFFECT_C_TIMER_CHILD
        and timer.chain_effect.effect_type == EFFECT_PRESERVATION
        and timer.chain_effect.value1 == 1,
        "direct-c-timer-shape",
    )


@dataclass(frozen=True, slots=True)
class SpecialPlayTriggerDecision:
    card_id: str
    trigger_id: str
    supported: bool
    fires: bool
    reused_central_evaluator: bool
    reasons: tuple[str, ...] = ()


def evaluate_special_play_trigger(
    card_id: str,
    state: Plan3State,
    *,
    database: Path = DEFAULT_DATABASE,
) -> SpecialPlayTriggerDecision:
    """Evaluate A's exact missing predicate or reuse C's central predicate."""

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    contract = load_legend_card_contract(card_id, Path(database))
    if card_id == CARD_B:
        return SpecialPlayTriggerDecision(card_id, "", True, True, False)
    if card_id == CARD_A:
        return SpecialPlayTriggerDecision(
            card_id,
            TRIGGER_A,
            True,
            state.stance == STANCE_CONCENTRATION and state.stance_level >= 2,
            False,
        )
    assert contract.card.play_trigger is not None
    decision = evaluate_plan3_trigger(
        contract.card.play_trigger, state, event_phase=PHASE_NONE
    )
    return SpecialPlayTriggerDecision(
        card_id,
        TRIGGER_C,
        decision.supported,
        decision.fires,
        True,
        decision.unsupported_rules,
    )


@dataclass(frozen=True, slots=True)
class StaminaReduce1000Result:
    before: Plan3State
    after: Plan3State
    effect_id: str = EFFECT_B_STAMINA
    exact_terminal_zero_only: bool = True


def apply_exact_stamina_reduce_1000(
    state: Plan3State, *, database: Path = DEFAULT_DATABASE
) -> StaminaReduce1000Result:
    """Resolve only the exact 1000-permille row: remaining stamina becomes 0.

    Native computes the reduction from maximum stamina, so the scalar input
    must supply and satisfy that runtime invariant even though the exact 1000
    row necessarily reaches zero.
    """

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    load_legend_card_contract(CARD_B, Path(database))
    if state.max_stamina is None:
        raise LegendMoveEffectError("max-stamina-required", EFFECT_B_STAMINA)
    if not 0 <= state.stamina <= state.max_stamina:
        raise LegendMoveEffectError(
            "stamina-outside-native-range",
            f"stamina={state.stamina}:max={state.max_stamina}",
        )
    return StaminaReduce1000Result(state, replace(state, stamina=0))


@dataclass(frozen=True, slots=True)
class MoveCommitEvent:
    """One authoritative zone commit, observed before callback insertion."""

    before: Plan3NativeState
    after_commit: Plan3NativeState
    moved_guid: str

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after_commit, Plan3NativeState
        ):
            raise TypeError("before and after_commit must be Plan3NativeState")
        if not isinstance(self.moved_guid, str) or not self.moved_guid:
            raise TypeError("moved_guid must be non-empty text")


@dataclass(frozen=True, slots=True)
class MoveEffectSelection:
    triggering_guid: str
    selected_guid: str

    def __post_init__(self) -> None:
        for name in ("triggering_guid", "selected_guid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise TypeError(f"{name} must be non-empty text")


@dataclass(frozen=True, slots=True)
class MoveEffectRuntimeConfig:
    hand_limit: int
    hold_limit: int
    lesson_type: str
    is_full_power: bool = False
    support_upgrades: SupportInputs = field(default_factory=dict)
    support_card_searches: Mapping[str, ProduceCardSearchRule] = field(
        default_factory=dict
    )
    max_callbacks: int = 32

    def __post_init__(self) -> None:
        for name in ("hand_limit", "hold_limit", "max_callbacks"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TypeError(f"{name} must be a non-negative integer")
        if self.max_callbacks < 1:
            raise ValueError("max_callbacks must be positive")
        if not isinstance(self.lesson_type, str):
            raise TypeError("lesson_type must be text")
        if not isinstance(self.is_full_power, bool):
            raise TypeError("is_full_power must be bool")


@dataclass(frozen=True, slots=True)
class MoveEffectTrace:
    ordinal: int
    moved_guid: str
    card_id: str
    destination: str
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MoveEffectRuntimeResult:
    initial_event: MoveCommitEvent
    after: Plan3NativeState
    used_move_effect_guids: frozenset[str]
    traces: tuple[MoveEffectTrace, ...]
    add_grow_mutations: tuple[NiaAddGrowMutation, ...]
    drawn_guids: tuple[str, ...]
    moved_to_hold_guids: tuple[str, ...]
    resolved: bool
    reason: str = ""
    detail: str = ""
    pending_selection_for_guid: str = ""


def _zone_of(state: Plan3NativeState, guid: str) -> str:
    zones = (
        ("hand", state.hand),
        ("deck", state.deck),
        ("grave", state.grave),
        ("lost", state.lost),
        ("hold", state.hold),
    )
    found = tuple(name for name, cards in zones if any(card.guid == guid for card in cards))
    if len(found) != 1:
        raise LegendMoveEffectError("move-guid-zone-count", f"{guid}:{len(found)}")
    return found[0]


def _trigger_for_zone(zone: str) -> str:
    return {
        "hand": MOVE_TRIGGER_HAND,
        "hold": MOVE_TRIGGER_HOLD,
    }.get(zone, "")


def _selection_map(
    values: tuple[MoveEffectSelection, ...],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if not isinstance(value, MoveEffectSelection):
            raise TypeError("selections must contain MoveEffectSelection")
        if value.triggering_guid in result:
            raise LegendMoveEffectError("duplicate-selection-trigger-guid", value.triggering_guid)
        result[value.triggering_guid] = value.selected_guid
    return result


def execute_legend_move_callbacks(
    event: MoveCommitEvent,
    config: MoveEffectRuntimeConfig,
    *,
    used_move_effect_guids: frozenset[str] = frozenset(),
    selections: tuple[MoveEffectSelection, ...] = (),
    database: Path = DEFAULT_DATABASE,
) -> MoveEffectRuntimeResult:
    """Run the bounded exact A/B/C callback queue after an initial commit."""

    if not isinstance(event, MoveCommitEvent):
        raise TypeError("event must be MoveCommitEvent")
    if not isinstance(config, MoveEffectRuntimeConfig):
        raise TypeError("config must be MoveEffectRuntimeConfig")
    if not isinstance(used_move_effect_guids, frozenset) or any(
        not isinstance(value, str) or not value for value in used_move_effect_guids
    ):
        raise TypeError("used_move_effect_guids must be a frozenset of text")
    selection_by_guid = _selection_map(selections)
    database = Path(database)
    queue: list[MoveCommitEvent] = [event]
    current = event.after_commit
    used = set(used_move_effect_guids)
    traces: list[MoveEffectTrace] = []
    mutations: list[NiaAddGrowMutation] = []
    drawn: list[str] = []
    moved_to_hold: list[str] = []

    def finish(
        *, reason: str = "", detail: str = "", pending: str = ""
    ) -> MoveEffectRuntimeResult:
        return MoveEffectRuntimeResult(
            event,
            current,
            frozenset(used),
            tuple(traces),
            tuple(mutations),
            tuple(drawn),
            tuple(moved_to_hold),
            not reason,
            reason,
            detail,
            pending,
        )

    ordinal = 0
    while queue:
        if ordinal >= config.max_callbacks:
            return finish(reason="callback-budget-exhausted", detail=str(config.max_callbacks))
        callback = queue.pop(0)
        if callback.after_commit != current:
            return finish(reason="callback-state-order-mismatch", detail=callback.moved_guid)
        try:
            before_zone = _zone_of(callback.before, callback.moved_guid)
            destination = _zone_of(callback.after_commit, callback.moved_guid)
            before_card = callback.before.card_by_guid(callback.moved_guid)
            moved_card = callback.after_commit.card_by_guid(callback.moved_guid)
        except (LegendMoveEffectError, Plan3NativeStateError) as error:
            return finish(
                reason=getattr(error, "code", "move-event-invalid"),
                detail=getattr(error, "detail", str(error)),
            )
        if before_zone == destination:
            return finish(reason="move-event-did-not-change-zone", detail=callback.moved_guid)
        if (
            before_card.card_id != moved_card.card_id
            or before_card.base_upgrade != moved_card.base_upgrade
        ):
            return finish(
                reason="move-event-card-identity-changed",
                detail=callback.moved_guid,
            )

        operations = [
            f"observe-zone-commit:{before_zone}->{destination}",
            "difference-callback-before-move-effect-insertion",
        ]
        trigger_type = _trigger_for_zone(destination)
        try:
            master = load_master_card(moved_card.card_id, moved_card.effective_upgrade, database)
        except (KeyError, OSError, ValueError) as error:
            return finish(reason="callback-card-master-unresolved", detail=f"{moved_card.card_id}+{moved_card.effective_upgrade}:{error}")
        if not master.move_effect_ids:
            operations.append("card-has-no-move-effect")
            traces.append(MoveEffectTrace(ordinal, moved_card.guid, moved_card.card_id, destination, tuple(operations)))
            ordinal += 1
            continue
        if moved_card.card_id not in CARD_SPECS or moved_card.effective_upgrade != 0:
            return finish(reason="unhandled-card-move-effect", detail=f"{moved_card.card_id}+{moved_card.effective_upgrade}")
        try:
            contract = load_legend_card_contract(moved_card.card_id, database)
        except LegendMoveEffectError as error:
            return finish(reason=error.code, detail=error.detail)
        if contract.spec.move_trigger_type != trigger_type:
            operations.append("move-trigger-destination-mismatch")
            traces.append(MoveEffectTrace(ordinal, moved_card.guid, moved_card.card_id, destination, tuple(operations)))
            ordinal += 1
            continue
        if moved_card.guid in used:
            operations.append("skip-is-move-effect-use-in-turn")
            traces.append(MoveEffectTrace(ordinal, moved_card.guid, moved_card.card_id, destination, tuple(operations)))
            ordinal += 1
            continue

        operations.extend(
            f"queue-play-effect:{effect.id}" for effect in contract.move_effects
        )
        # Native sets this after at least one command has been inserted, but
        # before the queued effect (and any Select pause) is executed.
        used.add(moved_card.guid)
        operations.append("set-is-move-effect-use-in-turn")

        try:
            if moved_card.card_id == CARD_A:
                transition = current.draw_to_hand(
                    1,
                    hand_limit=config.hand_limit,
                    lesson_type=config.lesson_type,
                    support_upgrades=config.support_upgrades,
                    support_card_searches=config.support_card_searches,
                    effect_draw=True,
                )
                current = transition.after
                drawn.extend(transition.drawn_guids)
                operations.append(
                    f"execute-card-draw:{transition.actual_count}"
                )
                for guid in transition.drawn_guids:
                    queue.append(MoveCommitEvent(transition.before, transition.after, guid))
                    operations.append(f"append-hand-add-callback:{guid}")
            elif moved_card.card_id == CARD_B:
                current, added = apply_plan3_native_add_grow_effects(
                    current, contract.move_effects, database=database
                )
                mutations.extend(added)
                operations.extend(
                    f"apply-hold-grow:{row.guid}" for row in added
                )
            else:
                effect = contract.move_effects[0]
                assert effect.card_move_rule is not None
                candidates = current.card_move_candidates(
                    _load_search(effect.card_move_rule.search_id, database),
                    playing_guid=moved_card.guid,
                )
                if candidates:
                    selected_guid = selection_by_guid.get(moved_card.guid)
                    if selected_guid is None:
                        traces.append(MoveEffectTrace(ordinal, moved_card.guid, moved_card.card_id, destination, tuple((*operations, "pause-for-select"))))
                        return finish(reason="select-input-required", detail=moved_card.guid, pending=moved_card.guid)
                    selected = tuple(value for value in candidates if value.guid == selected_guid)
                    if len(selected) != 1:
                        return finish(reason="selected-guid-not-candidate", detail=selected_guid)
                    if config.is_full_power:
                        operations.append("hold-add-during-full-power-noop")
                    else:
                        before_move = current
                        current = current.move_card_targets(
                            selected,
                            MOVE_HOLD,
                            hold_limit=config.hold_limit,
                            hand_limit=config.hand_limit,
                            is_full_power=False,
                            lesson_type=config.lesson_type,
                            support_upgrades=config.support_upgrades,
                            support_card_searches=config.support_card_searches,
                        )
                        moved_to_hold.append(selected_guid)
                        operations.append(f"execute-card-move-to-hold:{selected_guid}")
                        queue.append(MoveCommitEvent(before_move, current, selected_guid))
                        operations.append(f"append-pool-add-callback:{selected_guid}")
                else:
                    operations.append("select-candidates-empty-noop")
        except Plan3NativeStateError as error:
            return finish(reason=error.code, detail=error.detail)

        traces.append(MoveEffectTrace(ordinal, moved_card.guid, moved_card.card_id, destination, tuple(operations)))
        ordinal += 1
    return finish()


def dispatch_legend_move_callback_branches(
    event: MoveCommitEvent,
    config: MoveEffectRuntimeConfig,
    *,
    database: Path = DEFAULT_DATABASE,
) -> tuple[MoveEffectRuntimeResult, ...]:
    """Dispatch one committed GUID and enumerate only a native Select pause.

    The once-per-turn input is read from the cards themselves.  A Card C
    DeckGrave Select1 callback becomes one deterministic local branch per
    eligible GUID; all other callbacks produce exactly one branch.
    """

    used = frozenset(
        card.guid
        for card in event.after_commit.all_cards
        if card.move_effect_used_in_turn
    )
    first = execute_legend_move_callbacks(
        event,
        config,
        used_move_effect_guids=used,
        database=Path(database),
    )
    if first.resolved:
        return (
            replace(
                first,
                after=first.after.with_move_effect_used_guids(
                    first.used_move_effect_guids
                ),
            ),
        )
    if first.reason != "select-input-required":
        raise LegendMoveEffectError(first.reason, first.detail)
    triggering_guid = first.pending_selection_for_guid
    contract = load_legend_card_contract(CARD_C, Path(database))
    effect = contract.move_effects[0]
    assert effect.card_move_rule is not None
    candidates = event.after_commit.card_move_candidates(
        _load_search(effect.card_move_rule.search_id, Path(database)),
        playing_guid=triggering_guid,
    )
    results: list[MoveEffectRuntimeResult] = []
    for candidate in candidates:
        result = execute_legend_move_callbacks(
            event,
            config,
            used_move_effect_guids=used,
            selections=(
                MoveEffectSelection(triggering_guid, candidate.guid),
            ),
            database=Path(database),
        )
        if not result.resolved:
            raise LegendMoveEffectError(result.reason, result.detail)
        results.append(
            replace(
                result,
                after=result.after.with_move_effect_used_guids(
                    result.used_move_effect_guids
                ),
            )
        )
    if not results:
        raise LegendMoveEffectError(
            "select-input-required-without-candidates", triggering_guid
        )
    return tuple(results)


@lru_cache(maxsize=8)
def _load_search(search_id: str, database: Path) -> ProduceCardSearchRule:
    from .card_search import load_produce_card_search

    return load_produce_card_search(search_id, database)


@dataclass(frozen=True, slots=True)
class CardCDirectReusePlan:
    before: Plan3NativeState
    after_add_grow: Plan3NativeState
    add_grow_mutations: tuple[NiaAddGrowMutation, ...]
    force_play: ForcePlayPlanResult
    playable_value_delta: int
    timer_turns: int
    timer_child_effect_id: str

    @property
    def force_play_requires_core_hook(self) -> bool:
        return self.force_play.core_hook_required or self.force_play.unresolved


def plan_card_c_direct_reuse(
    state: Plan3NativeState,
    *,
    playing_guid: str,
    database: Path = DEFAULT_DATABASE,
) -> CardCDirectReusePlan:
    """Reuse AddGrow then ForcePlay planning; retain Playable/Timer as typed facts."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    contract = load_legend_card_contract(CARD_C, Path(database))
    grown, mutations = apply_plan3_native_add_grow_effects(
        state,
        (contract.card.effects[0],),
        database=Path(database),
        playing_guid=playing_guid,
    )
    force = plan_force_play_card_search(
        grown,
        force_play_contract_for_effect(HOLD_ALL_EFFECT_ID, Path(database)),
        playing_guid=playing_guid,
        database=Path(database),
    )
    return CardCDirectReusePlan(
        state,
        grown,
        mutations,
        force,
        1,
        1,
        EFFECT_C_TIMER_CHILD,
    )


def whole_card_readiness(
    *, database: Path = DEFAULT_DATABASE
) -> tuple[dict[str, object], ...]:
    """Return an audit-friendly readiness row for each exact card."""

    rows: list[dict[str, object]] = []
    for card_id in (CARD_A, CARD_B, CARD_C):
        contract = load_legend_card_contract(card_id, Path(database))
        rows.append(
            {
                "card_id": card_id,
                "upgrade": 0,
                "standalone_move_effect_ready": True,
                "whole_card_core_ready": contract.whole_card_core_ready,
                "central_missing_hooks": list(contract.central_missing_hooks),
            }
        )
    return tuple(rows)


__all__ = [
    "CARD_A",
    "CARD_B",
    "CARD_C",
    "CARD_SPECS",
    "EFFECT_A_DRAW",
    "EFFECT_B_HOLD_GROW",
    "EFFECT_B_STAMINA",
    "EFFECT_C_MOVE",
    "EFFECT_C_TIMER",
    "LegendMoveEffectError",
    "LegendCardContract",
    "MoveCommitEvent",
    "MoveEffectRuntimeConfig",
    "MoveEffectRuntimeResult",
    "MoveEffectSelection",
    "SpecialPlayTriggerDecision",
    "StaminaReduce1000Result",
    "CardCDirectReusePlan",
    "load_legend_card_contract",
    "evaluate_special_play_trigger",
    "apply_exact_stamina_reduce_1000",
    "execute_legend_move_callbacks",
    "dispatch_legend_move_callback_branches",
    "plan_card_c_direct_reuse",
    "whole_card_readiness",
]
