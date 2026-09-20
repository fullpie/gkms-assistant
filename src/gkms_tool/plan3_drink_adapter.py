"""Pure Plan3 scalar/native application for ordered drink-use steps.

The adapter intentionally has no search or live bridge dependency.  A caller
first creates a :class:`Plan3DrinkUse`, then this module applies each step in
Master order while keeping the GUID-native zones and scalar zone projection
identical.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from .master_db import DEFAULT_DATABASE

from .native_exam_formula import (
    AddingParameterStatus,
    ParameterApplicationStatus,
    ProduceParameterType,
    apply_parameter_add,
    calculate_adding_parameter,
)
from .plan3_anti_debuff import (
    AntiDebuffBlockResult,
    ExamStatusEffectTargetType,
    try_block_status_addition,
)
from .plan3_drink import (
    EFFECT_BLOCK,
    EFFECT_CONCENTRATION,
    EFFECT_FULL_POWER_POINT,
    EFFECT_LESSON,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_PRESERVATION,
    EFFECT_EXTRA_TURN,
    EFFECT_STAMINA_CONSUMPTION_ADD,
    EFFECT_STAMINA_CONSUMPTION_DOWN,
    EFFECT_STAMINA_RECOVER_FIX,
    EFFECT_STAMINA_REDUCE_FIX,
    MOVE_HOLD,
    SEARCH_DECK_GRAVE,
    Plan3DrinkInventory,
    Plan3DrinkCardUpgradeStep,
    Plan3DrinkKernelStep,
    Plan3DrinkNumericEffect,
    Plan3DrinkNumericStep,
    Plan3DrinkSelectCardStep,
    Plan3DrinkUse,
    use_plan3_drink,
)
from .plan3_engine import (
    PHASE_STANCE_CHANGE_CONCENTRATION,
    PHASE_STATUS_CHANGE,
    STANCE_CONCENTRATION,
    STANCE_FULL_POWER,
    STANCE_PRESERVATION,
    Plan3ExamSettings,
    Plan3RuntimeEffectEvent,
    Plan3State,
    _add_full_power_points,
    _adding_parameter_settings,
    _add_stamina_consumption_down_status,
    _apply_runtime_phase,
    _change_stance,
    _native_idol_status,
    _plan3_adding_parameter_status,
    load_plan3_exam_settings,
)
from .plan3_native_state import (
    Plan3NativeState,
    Plan3NativeStateError,
)


class Plan3DrinkApplicationError(ValueError):
    """The ordered drink action cannot be represented by this adapter."""


STAMINA_CONSUMPTION_ADD_EFFECT_TYPE_VALUE = 69


@dataclass(frozen=True, slots=True)
class Plan3DrinkNumericApplication:
    """One Master numeric drink effect applied to scalar Plan 3 state."""

    effect: Plan3DrinkNumericEffect
    before: Plan3State
    after: Plan3State
    parameter_pre_fix_values: tuple[int, ...] = ()
    parameter_actual_values: tuple[int, ...] = ()
    enthusiasm_released: int = 0
    extra_plays: int = 0
    full_power_points_gained: int = 0
    stamina_recovered: int = 0
    fired_status_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()
    extra_turns_added: int = 0
    anti_debuff_gate: AntiDebuffBlockResult | None = None
    runtime_effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()
    enthusiastic_receipts: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3DrinkAppliedStep:
    """One completed step with scalar/native before and after state."""

    step: Plan3DrinkNumericStep | Plan3DrinkSelectCardStep | Plan3DrinkCardUpgradeStep | Plan3DrinkKernelStep
    scalar_before: Plan3State
    scalar_after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    parameter_pre_fix_values: tuple[int, ...] = ()
    parameter_actual_values: tuple[int, ...] = ()
    enthusiasm_released: int = 0
    extra_plays: int = 0
    full_power_points_gained: int = 0
    stamina_recovered: int = 0
    fired_status_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()
    extra_turns_added: int = 0
    anti_debuff_gate: AntiDebuffBlockResult | None = None
    runtime_effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()
    card_upgrade_result: object | None = None
    kernel_trace: object | None = None
    enthusiastic_receipts: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3DrinkApplication:
    """A consumed drink plus its completely applied ordered transition."""

    use: Plan3DrinkUse
    scalar_before: Plan3State
    scalar_after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    applied_steps: tuple[Plan3DrinkAppliedStep, ...]

    @property
    def inventory_before(self) -> Plan3DrinkInventory:
        return self.use.before

    @property
    def inventory_after(self) -> Plan3DrinkInventory:
        return self.use.after


def _synchronize_scalar_zones(
    state: Plan3State, native: Plan3NativeState
) -> Plan3State:
    zones = native.projection()
    return replace(
        state,
        hand=zones.hand,
        draw_pile=zones.deck,
        discard_pile=zones.grave,
        lost_pile=zones.lost,
        hold_pile=zones.hold,
    )


def _move_deck_or_grave_to_hold(
    native: Plan3NativeState, guid: str
) -> Plan3NativeState:
    matches = tuple(
        (zone_name, index, card)
        for zone_name in ("deck", "grave")
        for index, card in enumerate(getattr(native, zone_name))
        if card.guid == guid
    )
    if len(matches) != 1:
        raise Plan3NativeStateError("drink-select-guid-not-found", guid)
    zone_name, index, card = matches[0]
    source = getattr(native, zone_name)
    return replace(
        native,
        **{
            zone_name: (*source[:index], *source[index + 1 :]),
            "hold": (*native.hold, card),
        },
    )


def _validate_numeric_shape(effect: Plan3DrinkNumericEffect) -> None:
    if effect.chain_effect_id or effect.status_enchant_id:
        raise Plan3DrinkApplicationError(
            f"nested drink effect is unsupported: {effect.id}"
        )
    if effect.effect_type == EFFECT_LESSON:
        valid = (
            effect.value2 == 0
            and effect.effect_count > 0
            and effect.effect_turn == 0
        )
    elif effect.effect_type in (
        EFFECT_BLOCK,
        EFFECT_PRESERVATION,
        EFFECT_CONCENTRATION,
        EFFECT_FULL_POWER_POINT,
    ):
        valid = (
            effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
        valid = (
            effect.value1 == 0
            and effect.value2 == 0
            and effect.effect_count > 0
            and effect.effect_turn == 0
        )
    elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
        valid = (
            effect.value1 == 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn > 0
        )
    elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_ADD:
        valid = (
            effect.value1 == 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn > 0
        )
    elif effect.effect_type == EFFECT_EXTRA_TURN:
        valid = (
            effect.value1 == 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    elif effect.effect_type in (
        EFFECT_STAMINA_RECOVER_FIX,
        EFFECT_STAMINA_REDUCE_FIX,
    ):
        valid = (
            effect.value1 > 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn == 0
        )
    else:
        valid = False
    if not valid:
        raise Plan3DrinkApplicationError(
            f"unsupported numeric drink shape: {effect.id}"
        )


def _apply_lesson(
    state: Plan3State,
    effect: Plan3DrinkNumericEffect,
    settings: Plan3ExamSettings,
) -> tuple[Plan3State, tuple[int, ...], tuple[int, ...]]:
    before_fix: list[int] = []
    actual: list[int] = []
    working = state
    for _ in range(effect.effect_count):
        hit = calculate_adding_parameter(
            effect.value1,
            is_buff_active=True,
            status=_plan3_adding_parameter_status(working),
            settings=_adding_parameter_settings(settings),
        )
        application = apply_parameter_add(
            hit,
            status=ParameterApplicationStatus(
                judge_parameter=working.score,
                limit_border=working.limit_border,
                clear_border=working.clear_border,
                current_turn_total_add_parameter=(
                    working.current_turn_total_add_parameter
                ),
                slump=working.slump,
                is_battle=working.is_battle,
                current_parameter_type=ProduceParameterType(
                    working.current_parameter_type
                ),
                battle_bonus_permille_vocal=(
                    working.battle_bonus_permille_vocal
                ),
                battle_bonus_permille_dance=(
                    working.battle_bonus_permille_dance
                ),
                battle_bonus_permille_visual=(
                    working.battle_bonus_permille_visual
                ),
                judge_parameter_vocal=working.judge_parameter_vocal,
                judge_parameter_dance=working.judge_parameter_dance,
                judge_parameter_visual=working.judge_parameter_visual,
            ),
        )
        before_fix.append(application.pre_fix_parameter)
        actual.append(application.actual_parameter)
        working = replace(
            working,
            score=application.after,
            current_turn_total_add_parameter=(
                application.current_turn_total_add_parameter
            ),
            judge_parameter_vocal=application.judge_parameter_vocal,
            judge_parameter_dance=application.judge_parameter_dance,
            judge_parameter_visual=application.judge_parameter_visual,
        )
    return working, tuple(before_fix), tuple(actual)


def apply_plan3_drink_numeric_effect(
    state: Plan3State,
    effect: Plan3DrinkNumericEffect,
    *,
    settings: Plan3ExamSettings | None = None,
) -> Plan3DrinkNumericApplication:
    """Apply one validated numeric drink effect with native phase ordering.

    This is the shared scalar boundary used both by prospective drink actions
    and by completed LocalSave command-history replay.  In particular, a
    successful Concentration change dispatches ``StanceChangeConcentration``
    before the next Master-ordered drink effect is applied.
    """

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(effect, Plan3DrinkNumericEffect):
        raise TypeError("effect must be Plan3DrinkNumericEffect")
    if settings is None:
        settings = load_plan3_exam_settings()
    if not isinstance(settings, Plan3ExamSettings):
        raise TypeError("settings must be Plan3ExamSettings")
    _validate_numeric_shape(effect)

    working = state
    pre_fix: tuple[int, ...] = ()
    actual: tuple[int, ...] = ()
    enthusiasm_released = 0
    extra_plays = 0
    full_power_points_gained = 0
    stamina_recovered = 0
    fired_status_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()
    extra_turns_added = 0
    anti_debuff_gate: AntiDebuffBlockResult | None = None
    runtime_effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()
    enthusiastic_receipts = ()

    if effect.effect_type == EFFECT_BLOCK:
        working = replace(working, block=working.block + effect.value1)
    elif effect.effect_type == EFFECT_PRESERVATION:
        if working.stance != STANCE_FULL_POWER:
            stance = _change_stance(
                working,
                STANCE_PRESERVATION,
                effect.value1,
                settings,
            )
            working = stance.state
            enthusiasm_released = stance.enthusiasm_released
            enthusiastic_receipts = stance.enthusiastic_receipts
            extra_plays = stance.extra_plays
    elif effect.effect_type == EFFECT_CONCENTRATION:
        if working.stance != STANCE_FULL_POWER:
            stance = _change_stance(
                working,
                STANCE_CONCENTRATION,
                effect.value1,
                settings,
            )
            working = stance.state
            enthusiasm_released = stance.enthusiasm_released
            enthusiastic_receipts = stance.enthusiastic_receipts
            extra_plays = stance.extra_plays
            if stance.changed:
                runtime = _apply_runtime_phase(
                    working,
                    PHASE_STANCE_CHANGE_CONCENTRATION,
                    settings=settings,
                )
                if runtime.unsupported_rules:
                    raise Plan3DrinkApplicationError(
                        "unsupported concentration-change runtime: "
                        + ",".join(runtime.unsupported_rules)
                    )
                working = runtime.state
                enthusiasm_released += runtime.enthusiasm_released
                extra_plays += runtime.extra_plays
                full_power_points_gained += runtime.full_power_points_gained
                stamina_recovered += runtime.stamina_recovered
                pre_fix = runtime.parameter_pre_fix_values
                actual = runtime.parameter_actual_values
                fired_status_enchant_ids = runtime.fired_enchant_ids
                fired_effect_ids = runtime.fired_effect_ids
    elif effect.effect_type == EFFECT_STAMINA_RECOVER_FIX:
        if working.max_stamina is None:
            raise Plan3DrinkApplicationError("state:max-stamina-unknown")
        recovered = min(
            effect.value1,
            max(0, working.max_stamina - working.stamina),
        )
        working = replace(working, stamina=working.stamina + recovered)
        stamina_recovered = recovered
    elif effect.effect_type == EFFECT_STAMINA_REDUCE_FIX:
        working = replace(
            working,
            stamina=max(0, working.stamina - effect.value1),
        )
    elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN:
        working = _add_stamina_consumption_down_status(
            working,
            turns=effect.effect_turn,
        )
    elif effect.effect_type == EFFECT_STAMINA_CONSUMPTION_ADD:
        anti_debuff_gate = try_block_status_addition(
            working.anti_debuff_runtime,
            incoming_effect_type_value=(
                STAMINA_CONSUMPTION_ADD_EFFECT_TYPE_VALUE
            ),
            incoming_target_type=ExamStatusEffectTargetType.DEBUFF,
        )
        if not anti_debuff_gate.blocked:
            raise Plan3DrinkApplicationError(
                "stamina-consumption-add-runtime-unmodelled:"
                f"{effect.id}"
            )
        working = replace(
            working,
            anti_debuff_runtime=anti_debuff_gate.state_after,
        )
    elif effect.effect_type == EFFECT_EXTRA_TURN:
        working = replace(
            working,
            turns_remaining=working.turns_remaining + 1,
            extra_turn=working.extra_turn + 1,
        )
        extra_turns_added = 1
    elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
        working = replace(
            working,
            plays_remaining=working.plays_remaining + effect.effect_count,
        )
        extra_plays = effect.effect_count
    elif effect.effect_type == EFFECT_FULL_POWER_POINT:
        # Direct drinks pass the same native effect-result source gate as
        # direct cards. Dispatch only after an actual positive type-49 delta;
        # the amount is not the number of times a listener is fired.
        working, full_power_points_gained = _add_full_power_points(working, effect.value1)
        if full_power_points_gained > 0:
            runtime = _apply_runtime_phase(
                working, PHASE_STATUS_CHANGE, settings=settings,
                event_effect_type=EFFECT_FULL_POWER_POINT,
                effect_result_trigger_enabled=True,
            )
            if runtime.unsupported_rules:
                raise Plan3DrinkApplicationError(
                    "unsupported FullPowerPoint-change runtime: " + ",".join(runtime.unsupported_rules)
                )
            working = runtime.state
            pre_fix, actual = runtime.parameter_pre_fix_values, runtime.parameter_actual_values
            enthusiasm_released += runtime.enthusiasm_released
            extra_plays += runtime.extra_plays
            full_power_points_gained += runtime.full_power_points_gained
            stamina_recovered += runtime.stamina_recovered
            fired_status_enchant_ids = runtime.fired_enchant_ids
            fired_effect_ids = runtime.fired_effect_ids
            runtime_effect_events = runtime.effect_events
    elif effect.effect_type == EFFECT_LESSON:
        working, pre_fix, actual = _apply_lesson(working, effect, settings)
    else:
        raise Plan3DrinkApplicationError(
            f"unsupported numeric drink effect: {effect.id}"
        )

    return Plan3DrinkNumericApplication(
        effect=effect,
        before=state,
        after=working,
        parameter_pre_fix_values=pre_fix,
        parameter_actual_values=actual,
        enthusiasm_released=enthusiasm_released,
        extra_plays=extra_plays,
        full_power_points_gained=full_power_points_gained,
        stamina_recovered=stamina_recovered,
        fired_status_enchant_ids=fired_status_enchant_ids,
        fired_effect_ids=fired_effect_ids,
        extra_turns_added=extra_turns_added,
        anti_debuff_gate=anti_debuff_gate,
        runtime_effect_events=runtime_effect_events,
        enthusiastic_receipts=enthusiastic_receipts,
    )


def _apply_kernel_effect(state, native, step, *, settings, source_id,
                         support_upgrades, support_card_searches,
                         plan_ignore_card_ids, created_card_guids, forced_selected_card_guids):
    """Reuse exact owners without manufacturing a played card or its costs."""
    effect = step.effect.resolve_contract()
    kind = effect.effect_type
    if kind == "ProduceExamEffectType_ExamLessonValueMultiple":
        from .nia_lesson_value_multiple import install_lesson_value_multiple
        from .plan3_engine import allocate_plan3_status_uid
        result = install_lesson_value_multiple(state.lesson_parameter_multiple_state,
            permil=effect.value1, turn=effect.effect_turn)
        if not result.installed:
            raise Plan3DrinkApplicationError("drink lesson multiplier installation blocked:" + effect.id)
        layers = result.state
        if result.merged_index is None:
            state, uid = allocate_plan3_status_uid(state)
            values = list(layers.statuses); values[-1] = replace(values[-1], uid=uid)
            layers = type(layers)(tuple(values))
        return replace(state, lesson_parameter_multiple_state=layers), native, result
    if kind == "ProduceExamEffectType_ExamStatusEnchant":
        from .plan3_engine import _install_card_status_enchant
        after = _install_card_status_enchant(state, effect, source_id=source_id)
        # The shared allocator is source-neutral; keep drink provenance explicit.
        newest = after.active_status_enchants[-1]
        newest = replace(newest, instance_id=f"drink:{source_id}:{effect.id}:uid:{newest.native_uid}")
        after = replace(after, active_status_enchants=(*after.active_status_enchants[:-1], newest))
        return after, native, {"effect_id":effect.id,"source_kind":"drink","native_uid":newest.native_uid}
    if support_upgrades is None or support_card_searches is None:
        raise Plan3DrinkApplicationError("drink native effect requires authoritative HandAdd support context:" + effect.id)
    # Auditions keep lesson_type=Unknown; HandAdd checks the current battle
    # parameter just as the ordinary native draw scheduler does.
    hand_add_lesson=state.lesson_type
    if state.is_battle:
        from .plan3_engine import LESSON_VOCAL,LESSON_DANCE,LESSON_VISUAL
        hand_add_lesson={1:LESSON_VOCAL,2:LESSON_DANCE,3:LESSON_VISUAL}.get(state.current_parameter_type)
        if hand_add_lesson is None:
            raise Plan3DrinkApplicationError('drink HandAdd requires the current battle parameter')
    if kind == "ProduceExamEffectType_ExamForcePlayCardSearch":
        from .plan3_drink_force_play_random_pool import execute_plan3_random_pool_force_drink
        if created_card_guids is None or len(created_card_guids)!=1 or plan_ignore_card_ids is None:
            raise Plan3DrinkApplicationError("force drink requires exact plan-ignore IDs and one caller-owned generated GUID")
        result=execute_plan3_random_pool_force_drink(state,native,effect.id,guid_token=created_card_guids[0],
            plan_ignore_card_ids=plan_ignore_card_ids,settings=settings,support_upgrades=support_upgrades,
            support_card_searches=support_card_searches,selected_card_guids=forced_selected_card_guids,
            database=Path(step.effect.database))
        return result.scalar_after,result.native_after,result
    if kind == "ProduceExamEffectType_ExamHandGraveCountCardDraw":
        from .plan3_hand_grave_draw import execute_hand_grave_draw, load_hand_grave_draw
        from .plan3_native_search import dispatch_plan3_guid_move_commits
        result = execute_hand_grave_draw(load_hand_grave_draw(Path(step.effect.database)), native,
            hand_limit=settings.hand_limit, lesson_type=hand_add_lesson,
            support_upgrades=support_upgrades, support_card_searches=support_card_searches)
        moved = dispatch_plan3_guid_move_commits(native, result.after_hand_to_grave, result.discarded_guids,
            hand_limit=settings.hand_limit, hold_limit=settings.hold_limit, lesson_type=hand_add_lesson,
            is_full_power=state.stance == STANCE_FULL_POWER, support_upgrades=support_upgrades,
            support_card_searches=support_card_searches, database=Path(step.effect.database))
        if len(moved) != 1:
            raise Plan3DrinkApplicationError("drink discard callbacks require an explicit branch:" + effect.id)
        draw = moved[0].state.draw_to_hand(len(native.hand), hand_limit=settings.hand_limit,
            lesson_type=hand_add_lesson, support_upgrades=support_upgrades,
            support_card_searches=support_card_searches, effect_draw=True)
        return _synchronize_scalar_zones(state, draw.after), draw.after, {"discard":result,"callbacks":moved[0].traces,"draw":draw}
    if kind == "ProduceExamEffectType_ExamCardDraw":
        result = native.draw_to_hand(effect.value1, hand_limit=settings.hand_limit,
            lesson_type=hand_add_lesson, support_upgrades=support_upgrades,
            support_card_searches=support_card_searches, effect_draw=True)
        return _synchronize_scalar_zones(state, result.after), result.after, result
    if kind == "ProduceExamEffectType_ExamCardMove":
        from .plan3_startplay_idol_return import return_idol_to_hand
        from .plan3_native_search import dispatch_plan3_guid_move_commits
        moved, trace = return_idol_to_hand(native, effect, hand_limit=settings.hand_limit,
            hold_limit=settings.hold_limit, is_full_power=state.stance == STANCE_FULL_POWER,
            lesson_type=hand_add_lesson, support_upgrades=support_upgrades,
            support_card_searches=support_card_searches, database=Path(step.effect.database))
        callbacks = dispatch_plan3_guid_move_commits(native, moved, trace.target_guids,
            hand_limit=settings.hand_limit, hold_limit=settings.hold_limit, lesson_type=hand_add_lesson,
            is_full_power=state.stance == STANCE_FULL_POWER, support_upgrades=support_upgrades,
            support_card_searches=support_card_searches, database=Path(step.effect.database))
        if len(callbacks) != 1:
            raise Plan3DrinkApplicationError("drink move callbacks require an explicit branch:" + effect.id)
        after = callbacks[0].state
        return _synchronize_scalar_zones(state, after), after, {"move":trace,"callbacks":callbacks[0].traces}
    if kind == "ProduceExamEffectType_ExamCardCreateSearch":
        from .plan3_card_create_search import (load_master_card_create_search_rows,
            resolve_card_create_search_contract, apply_card_create_search, CardCreateSearchChanceInput)
        if plan_ignore_card_ids is None or created_card_guids is None:
            raise Plan3DrinkApplicationError("drink creation requires exact plan-ignore IDs and caller-owned generated GUIDs:" + effect.id)
        row = next(row for row in load_master_card_create_search_rows(Path(step.effect.database)) if row["id"] == effect.id)
        result = apply_card_create_search(native, resolve_card_create_search_contract(row),
            execution_input=CardCreateSearchChanceInput(plan_type="ProducePlanType_Plan3",
                plan_ignore_card_ids=tuple(plan_ignore_card_ids), hand_limit=settings.hand_limit,
                guid_tokens=tuple(created_card_guids)))
        if not result.resolved:
            raise Plan3DrinkApplicationError("drink creation runtime unresolved:" + str(result.branch.required_fields))
        if support_upgrades:
            # Creation has a separate HandAdd boundary; do not silently omit it.
            raise Plan3DrinkApplicationError("drink generated-card HandAdd support owner still required:" + effect.id)
        return _synchronize_scalar_zones(state, result.after), result.after, result
    raise Plan3DrinkApplicationError("unbound drink kernel owner:" + kind)


def apply_plan3_drink_use(
    state: Plan3State,
    native: Plan3NativeState,
    use: Plan3DrinkUse,
    *,
    settings: Plan3ExamSettings | None = None,
    database: Path = DEFAULT_DATABASE,
    support_upgrades=None,
    support_card_searches=None,
    plan_ignore_card_ids: tuple[str, ...] | None = None,
    created_card_guids: tuple[str, ...] | None = None,
    forced_selected_card_guids: tuple[str, ...] | None = None,
) -> Plan3DrinkApplication:
    """Apply one already-materialized drink use in exact step order."""

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(native, Plan3NativeState):
        raise TypeError("native must be Plan3NativeState")
    if not isinstance(use, Plan3DrinkUse):
        raise TypeError("use must be Plan3DrinkUse")
    if settings is None:
        settings = load_plan3_exam_settings()
    native.assert_plan3_projection(state)

    scalar_working = state
    native_working = native
    applied: list[Plan3DrinkAppliedStep] = []
    for step in use.steps:
        scalar_before = scalar_working
        native_before = native_working
        pre_fix: tuple[int, ...] = ()
        actual: tuple[int, ...] = ()
        enthusiasm_released = 0
        extra_plays = 0
        full_power_points_gained = 0
        stamina_recovered = 0
        fired_status_enchant_ids: tuple[str, ...] = ()
        fired_effect_ids: tuple[str, ...] = ()
        extra_turns_added = 0
        anti_debuff_gate: AntiDebuffBlockResult | None = None
        enthusiastic_receipts = ()
        runtime_effect_events: tuple[Plan3RuntimeEffectEvent, ...] = ()
        card_upgrade_result = None
        kernel_trace = None

        if isinstance(step, Plan3DrinkKernelStep):
            scalar_working, native_working, kernel_trace = _apply_kernel_effect(
                scalar_working, native_working, step, settings=settings, source_id=use.drink.id,
                support_upgrades=support_upgrades, support_card_searches=support_card_searches,
                plan_ignore_card_ids=plan_ignore_card_ids, created_card_guids=created_card_guids,
                forced_selected_card_guids=forced_selected_card_guids)
        elif isinstance(step, Plan3DrinkCardUpgradeStep):
            from .plan3_card_upgrade import execute_plan3_card_upgrade

            contract = step.effect.resolve_contract()
            card_upgrade_result = execute_plan3_card_upgrade(native_working, contract)
            if not card_upgrade_result.applied:
                raise Plan3DrinkApplicationError("drink Hand-All upgrade paused:" + str(card_upgrade_result.pause))
            native_working = card_upgrade_result.state_after
            scalar_working = _synchronize_scalar_zones(scalar_working, native_working)
        elif isinstance(step, Plan3DrinkSelectCardStep):
            effect = step.effect
            if (
                effect.card_search_id != SEARCH_DECK_GRAVE
                or effect.destination != MOVE_HOLD
                or effect.pick_count_min != 1
                or effect.pick_count_max != 1
            ):
                raise Plan3DrinkApplicationError(
                    f"unsupported drink selection: {effect.id}"
                )
            native_working = _move_deck_or_grave_to_hold(
                native_working, step.selected_card_guid
            )
            scalar_working = _synchronize_scalar_zones(
                scalar_working, native_working
            )
        else:
            effect = step.effect
            numeric = apply_plan3_drink_numeric_effect(
                scalar_working,
                effect,
                settings=settings,
            )
            scalar_working = numeric.after
            pre_fix = numeric.parameter_pre_fix_values
            actual = numeric.parameter_actual_values
            enthusiasm_released = numeric.enthusiasm_released
            extra_plays = numeric.extra_plays
            full_power_points_gained = numeric.full_power_points_gained
            stamina_recovered = numeric.stamina_recovered
            fired_status_enchant_ids = numeric.fired_status_enchant_ids
            fired_effect_ids = numeric.fired_effect_ids
            extra_turns_added = numeric.extra_turns_added
            anti_debuff_gate = numeric.anti_debuff_gate
            runtime_effect_events = numeric.runtime_effect_events
            enthusiastic_receipts = numeric.enthusiastic_receipts
            from .enthusiastic_runtime import replay_enthusiastic_add_receipts
            if native_working.enthusiastic_runtime != numeric.before.enthusiastic_runtime:
                raise Plan3NativeStateError("drink-enthusiastic-source-mismatch")
            replayed = replay_enthusiastic_add_receipts(native_working.enthusiastic_runtime, enthusiastic_receipts,
                cursor_before=numeric.before.next_status_uid, cursor_after=numeric.after.next_status_uid)
            if replayed != numeric.after.enthusiastic_runtime:
                raise Plan3NativeStateError("drink-enthusiastic-receipt-chain-incomplete")
            native_working = replace(native_working, enthusiastic_runtime=replayed)
            if runtime_effect_events:
                # At this numeric effect's boundary, before the next drink
                # command (e.g. an upgrade/select), apply each real GUID grow
                # in listener order. Other native mutations remain explicit.
                from .plan3_engine import EFFECT_ADD_GROW, EFFECT_CARD_DRAW, EFFECT_CARD_UPGRADE, EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST
                from .plan3_native_search import apply_plan3_native_add_grow_effects

                for event in runtime_effect_events:
                    if event.effect_type == EFFECT_ADD_GROW:
                        native_working, _ = apply_plan3_native_add_grow_effects(native_working, (event.effect,), database=Path(database))
                    elif (event.effect.card_move_rule is not None
                            or event.effect_type in {EFFECT_CARD_DRAW, EFFECT_CARD_UPGRADE, EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST}):
                        raise Plan3DrinkApplicationError("unsupported drink-triggered native effect:" + event.effect_id)
            if anti_debuff_gate is not None:
                if (
                    native_working.anti_debuff_runtime
                    != anti_debuff_gate.state_before
                ):
                    raise Plan3NativeStateError(
                        "plan3-anti-debuff-projection-mismatch"
                    )
                native_working = replace(
                    native_working,
                    anti_debuff_runtime=anti_debuff_gate.state_after,
                )

        native_working.assert_plan3_projection(scalar_working)
        applied.append(
            Plan3DrinkAppliedStep(
                step=step,
                scalar_before=scalar_before,
                scalar_after=scalar_working,
                native_before=native_before,
                native_after=native_working,
                parameter_pre_fix_values=pre_fix,
                parameter_actual_values=actual,
                enthusiasm_released=enthusiasm_released,
                extra_plays=extra_plays,
                full_power_points_gained=full_power_points_gained,
                stamina_recovered=stamina_recovered,
                fired_status_enchant_ids=fired_status_enchant_ids,
                fired_effect_ids=fired_effect_ids,
                extra_turns_added=extra_turns_added,
                anti_debuff_gate=anti_debuff_gate,
                runtime_effect_events=runtime_effect_events,
                card_upgrade_result=card_upgrade_result,
                kernel_trace=kernel_trace,
                enthusiastic_receipts=enthusiastic_receipts,
            )
        )

    return Plan3DrinkApplication(
        use=use,
        scalar_before=state,
        scalar_after=scalar_working,
        native_before=native,
        native_after=native_working,
        applied_steps=tuple(applied),
    )


def apply_plan3_drink(
    state: Plan3State,
    native: Plan3NativeState,
    inventory: Plan3DrinkInventory,
    slot_index: int,
    *,
    selected_card_guid: str | None = None,
    settings: Plan3ExamSettings | None = None,
    database: Path = DEFAULT_DATABASE,
    support_upgrades=None,
    support_card_searches=None,
    plan_ignore_card_ids: tuple[str, ...] | None = None,
    created_card_guids: tuple[str, ...] | None = None,
    forced_selected_card_guids: tuple[str, ...] | None = None,
) -> Plan3DrinkApplication:
    """Consume one inventory slot, then apply its ordered steps."""

    use = use_plan3_drink(
        inventory,
        slot_index,
        selected_card_guid=selected_card_guid,
    )
    return apply_plan3_drink_use(
        state,
        native,
        use,
        settings=settings,
        database=database,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
        plan_ignore_card_ids=plan_ignore_card_ids,
        created_card_guids=created_card_guids,
        forced_selected_card_guids=forced_selected_card_guids,
    )


__all__ = [
    "Plan3DrinkApplication",
    "Plan3DrinkApplicationError",
    "Plan3DrinkAppliedStep",
    "Plan3DrinkNumericApplication",
    "STAMINA_CONSUMPTION_ADD_EFFECT_TYPE_VALUE",
    "apply_plan3_drink",
    "apply_plan3_drink_numeric_effect",
    "apply_plan3_drink_use",
]
