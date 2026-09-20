"""Use the existing Plan1 ordered-zone owners for direct runtime effects."""
from dataclasses import replace
from pathlib import Path
import sqlite3

from .plan1_native_core import (Plan1Blocker, Plan1CardDrawResolution, Plan1CardMoveResolution,
    Plan1EffectExecution, Plan1TraceEntry, Plan1TraceStage, execute_plan1_effects)
from .plan1_native_stage import (
    _resolve_plan1_stage_card_upgrade, _resolve_plan1_stage_card_draw,
    _resolve_plan1_stage_hand_grave_draw, _resolve_plan1_hand_add_support,
    _plan3_placement_state, _lift_card_create_result,
)

CREATE_SEARCH = "ProduceExamEffectType_ExamCardCreateSearch"
FORCE_SEARCH = "ProduceExamEffectType_ExamForcePlayCardSearch"


def compile_runtime_drink_zone_kernel(effect, database):
    """Admit a complete shared contract only when this owner executes it."""
    if effect.effect_type == FORCE_SEARCH:
        from .plan2_drink_force_play_random_pool import load_plan2_drink_force_play_random_pool_program
        program = load_plan2_drink_force_play_random_pool_program(Path(database))
        if effect.effect_id != program.effect_id or any((effect.value1, effect.value2, effect.effect_count, effect.effect_turn)):
            raise ValueError("drink ForcePlay requires its complete original RandomPool Master contract")
        return replace(effect, blockers=())
    if effect.effect_type != CREATE_SEARCH:
        return effect
    from .plan3_card_create_search import resolve_card_create_search_contract
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM effect WHERE id=?", (effect.effect_id,)).fetchone()
    contract = resolve_card_create_search_contract(row)
    if contract.destination != "hand" or (contract.pick_count_min, contract.pick_count_max) != (1, 1):
        raise ValueError("drink CardCreateSearch requires its proven single Hand placement")
    # resolve_card_create_search_contract checks the entire original Master
    # payload. Only this drink-owned effect is delegated; ordinary card core
    # compilation and its admission blockers remain unchanged.
    return replace(effect, blockers=())


class Plan1RuntimeZoneEffects:
    def __init__(self, state, *, settings, hand_add_support_resolver=None, used_support_ids=None,
                 database=None, selected_card_guids=None, plan_ignore_card_ids=None, simulation_guid_prefix=None):
        self.zones = state.zones
        self.used_support_ids = state.hand_add_used_support_ids if used_support_ids is None else used_support_ids
        self.settings = settings
        self.hand_add_support_resolver = hand_add_support_resolver
        self.database = database
        self.selected_card_guids = selected_card_guids
        self.plan_ignore_card_ids = plan_ignore_card_ids
        self.simulation_guid_prefix = simulation_guid_prefix
        self.created_guids = []
        self.kernel_trace = []

    def _force_search(self, scalar, effect, index):
        from .plan2_drink_force_play_random_pool import load_plan2_drink_force_play_random_pool_program
        from .plan3_card_create_search import apply_card_create_search, CardCreateSearchChanceInput
        if self.plan_ignore_card_ids is None:
            raise ValueError("ForcePlay RandomPool requires the captured plan-ignore list")
        program = load_plan2_drink_force_play_random_pool_program(Path(self.database))
        native = _plan3_placement_state(self.zones)
        result = apply_card_create_search(native, program.selector, execution_input=CardCreateSearchChanceInput(
            plan_type="ProducePlanType_Plan1", plan_ignore_card_ids=self.plan_ignore_card_ids,
            hand_limit=self.settings.hand_limit, guid_tokens=(f"{self.simulation_guid_prefix}:detached",)))
        if not result.resolved:
            raise ValueError("ForcePlay RandomPool selector has unresolved runtime inputs")
        if result.trace.selected_card_ids:
            # Current authoritative Rush pool contains only Plan2/Plan3 cards.
            # A nonempty cross-plan whitelist needs that selected card's full
            # detached effect owner; do not run it as an ordinary Plan1 play.
            raise ValueError("nonempty-cross-plan-ForcePlay-detached-card-owner-required:" + ",".join(result.trace.selected_card_ids))
        self.kernel_trace.append({"effect_id": effect.effect_id, "owner": "shared-native-RandomPool-selector",
            "plan_type": "ProducePlanType_Plan1", "plan_ignore_card_ids": list(self.plan_ignore_card_ids),
            "selected_card_ids": [], "authoritative_plan1_card_count": sum(
                card.plan_type == "ProducePlanType_Plan1" for card in program.selector.pool.authoritative_candidates),
            "random_before": native.random_state, "random_after": result.trace.random_state_after,
            "rng_call_count": result.trace.rng_call_count, "detached_play_count": 0})
        self.zones = replace(self.zones, random_state=result.trace.random_state_after)
        return Plan1EffectExecution(scalar, scalar, (Plan1TraceEntry(0, Plan1TraceStage.EFFECT,
            effect.effect_id, scalar, scalar, effect.effect_type, index, None, 1, 0),))

    def _create_search(self, scalar, effect, index):
        from .plan3_card_create_search import apply_card_create_search, CardCreateSearchChanceInput, resolve_card_create_search_contract
        if not self.simulation_guid_prefix or self.plan_ignore_card_ids is None:
            raise ValueError("drink creation needs explicit local identity namespace and captured plan-ignore list")
        with sqlite3.connect(Path(self.database).resolve().as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM effect WHERE id=?", (effect.effect_id,)).fetchone()
        contract = resolve_card_create_search_contract(row)
        guid = f"{self.simulation_guid_prefix}:{len(self.created_guids)}"
        result = apply_card_create_search(_plan3_placement_state(self.zones), contract,
            execution_input=CardCreateSearchChanceInput(plan_type="ProducePlanType_Plan1",
                plan_ignore_card_ids=self.plan_ignore_card_ids, hand_limit=self.settings.hand_limit, guid_tokens=(guid,)))
        if not result.resolved:
            raise ValueError("shared CardCreateSearch has unresolved runtime inputs")
        after = _lift_card_create_result(self.zones, result.after, (guid,))
        entered = tuple(card.guid for card in after.hand if card.guid == guid)
        supported, used, _trace, error = _resolve_plan1_hand_add_support(after,
            effect_id=effect.effect_id, effect_index=index, drawn_guids=entered,
            used_support_ids=self.used_support_ids, resolver=self.hand_add_support_resolver)
        if error is not None:
            return Plan1EffectExecution(scalar, scalar, (), (error,))
        self.zones, self.used_support_ids = supported, used
        self.created_guids.append(guid)
        return Plan1EffectExecution(scalar, scalar, (Plan1TraceEntry(0, Plan1TraceStage.EFFECT,
            effect.effect_id, scalar, scalar, effect.effect_type, index, None, 1, 1),))

    def _move(self, scalar, effect, index):
        from .plan3_native_state import Plan3NativeCardMoveTarget
        from .plan3_engine import load_plan3_effect
        from .plan3_startplay_idol_return import return_idol_to_hand
        contract = effect.card_move_contract
        try:
            lesson_type = {1: "ProduceStepLessonType_LessonVocal", 2: "ProduceStepLessonType_LessonDance",
                           3: "ProduceStepLessonType_LessonVisual"}.get(getattr(scalar.current_parameter_type, "value", scalar.current_parameter_type))
            if lesson_type is None:
                raise ValueError("runtime drink move requires the current native lesson axis")
            if contract is None or contract.destination != "ProduceCardMovePositionType_Hand":
                raise ValueError("runtime drink move requires proven Hand destination")
            native = _plan3_placement_state(self.zones)
            if contract.pick_range_type == "ProducePickRangeType_All":
                after, trace = return_idol_to_hand(native, load_plan3_effect(effect.effect_id, self.database),
                    hand_limit=self.settings.hand_limit, hold_limit=0, is_full_power=False,
                    lesson_type=lesson_type, database=self.database)
                selected = trace.target_guids
                candidate_count = len(selected)
            else:
                if effect.card_move_search.id != "p_card_search-deck_grave":
                    raise ValueError("runtime drink selected move requires complete DeckGrave search")
                candidates = tuple(Plan3NativeCardMoveTarget(card.guid, card, source, order)
                    for source, zone in (("draw", native.deck), ("discard", native.grave)) for order, card in enumerate(zone))
                selected = self.selected_card_guids
                if selected is None or len(selected) != 1 or sum(card.guid == selected[0] for card in candidates) != 1:
                    raise ValueError("runtime drink selected move requires one explicit current target")
                targets = tuple(card for card in candidates if card.guid in selected)
                after = native.move_card_targets(targets, contract.destination, hand_limit=self.settings.hand_limit,
                    hold_limit=0, is_full_power=False, lesson_type=lesson_type, support_upgrades=(), support_card_searches={})
                candidate_count = len(candidates)
            moved = _lift_card_create_result(self.zones, after, ())
            entered = tuple(card.guid for card in moved.hand if card.guid in selected)
            supported, used, _trace, error = _resolve_plan1_hand_add_support(moved,
                effect_id=effect.effect_id, effect_index=index, drawn_guids=entered,
                used_support_ids=self.used_support_ids, resolver=self.hand_add_support_resolver)
            if error is not None:
                return Plan1CardMoveResolution(scalar, scalar, candidate_count, len(selected), (error,))
            self.zones, self.used_support_ids = supported, used
            return Plan1CardMoveResolution(scalar, scalar, candidate_count, len(selected))
        except (ValueError, TypeError, KeyError, OSError) as error:
            return Plan1CardMoveResolution(scalar, scalar, 0, 0,
                (Plan1Blocker("plan1-runtime-drink-card-move-unavailable", effect.effect_id, str(error)),))

    def _upgrade(self, scalar, effect, index):
        result, after = _resolve_plan1_stage_card_upgrade(scalar, self.zones, effect)
        if after is not None:
            self.zones = after
        return result

    def _draw(self, scalar, effect, index):
        result, draw = _resolve_plan1_stage_card_draw(scalar, self.zones, effect, index, self.settings)
        if draw is None:
            return result
        after, used, _trace, error = _resolve_plan1_hand_add_support(draw.after_zones,
            effect_id=effect.effect_id, effect_index=index, drawn_guids=draw.drawn_guids,
            used_support_ids=self.used_support_ids, resolver=self.hand_add_support_resolver)
        if error is not None:
            return Plan1CardDrawResolution(scalar, scalar, effect.value1, draw.actual_count, (error,))
        self.zones, self.used_support_ids = after, used
        return result

    def _redraw(self, scalar, effect, index):
        result, trace = _resolve_plan1_stage_hand_grave_draw(scalar, self.zones, effect, index,
            self.settings, self.hand_add_support_resolver, self.used_support_ids)
        if trace is not None:
            self.zones, self.used_support_ids = trace.hand_add.after_zones, trace.hand_add.result.used_support_ids
        return result

    def apply(self, scalar, effects):
        before = self.zones, self.used_support_ids
        created_before = len(self.created_guids)
        working, trace = scalar, []
        for index, effect in enumerate(effects):
            try:
                result = (self._force_search(working, effect, index) if effect.effect_type == FORCE_SEARCH else
                    self._create_search(working, effect, index) if effect.effect_type == CREATE_SEARCH else
                    execute_plan1_effects(working, (effect,), settings=self.settings,
                        card_upgrade_resolver=self._upgrade, card_draw_resolver=self._draw,
                        hand_grave_draw_resolver=self._redraw, card_move_resolver=self._move))
            except (ValueError, TypeError, KeyError, OSError) as error:
                result = Plan1EffectExecution(scalar, scalar, (),
                    (Plan1Blocker("plan1-runtime-zone-kernel-unavailable", effect.effect_id, str(error)),))
            if result.blockers:
                self.zones, self.used_support_ids = before
                del self.created_guids[created_before:]
                return Plan1EffectExecution(scalar, scalar, (), result.blockers)
            offset = len(trace)
            trace.extend(replace(entry, ordinal=offset+position, effect_index=index)
                         for position, entry in enumerate(result.trace))
            working = result.after
        return Plan1EffectExecution(scalar, working, tuple(trace))

    def commit(self, state, scalar):
        return replace(state, scalar=scalar, zones=self.zones, hand_add_used_support_ids=self.used_support_ids)
