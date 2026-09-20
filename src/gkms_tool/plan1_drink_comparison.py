"""Pure Plan1 adapter for the shared bounded DRINK -> same GUID comparison.

The existing bridge owns effects, card compilation, payment and automatic turn
lifecycle. Its exact/promotion flags are deliberately unchanged. A small local
audit rejects source graphs or drink prefixes that the bridge cannot continue
without refreshing opaque listener references/counters.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path

from .card_search import exact_playing_card_search_mismatches
from .exam_drink_decision import DrinkStateView, DrinkTransition, compare_drink_before_play
from .exam_status_runtime import load_exam_status_trigger, load_runtime_status_enchant
from .leaderboard_replay import LeaderboardReplayAction
from .master_db import DEFAULT_DATABASE
from .nia_plan1_native_sidecar import Plan1NativeLegalCandidate
from .plan1_native_core import compile_plan1_effect
from .plan1_pitem_runtime import SERIALIZED_PHASE_ENUM
from .plan1_runtime_simulator_bridge import (
    Plan1RuntimeStateProjection, _compile_plan1_drink, _drink_inventory, _opaque_runtime,
    _promote_transition_local_effect_installers, apply_plan1_runtime_action,
)


ADAPTER_ID = "gkms.plan1-drink-comparison.v1"
_STEP = {"Mid1": 16, "Mid2": 17, "Final": 18}
_PREFIX = "ProduceExamEffectType_"
_SCALAR_EFFECT_ROLES = {
    "ExamLesson": "direct-score", "ExamBlock": "resource", "ExamBlockFix": "resource",
    "ExamLessonBuff": "score-buff", "ExamParameterBuff": "score-buff",
    "ExamParameterBuffMultiplePerTurn": "score-buff", "ExamLessonValueMultiple": "score-buff",
    "ExamLessonBuffAdditive": "score-buff", "ExamStaminaRecoverFix": "resource",
    "ExamStaminaRecoverMultiple": "resource", "ExamStaminaConsumptionDown": "resource",
    "ExamStaminaConsumptionDownFix": "resource",
    "ExamMultipleLessonBuffLesson": "direct-score", "ExamLessonDependParameterBuff": "direct-score",
    "ExamLessonAddMultipleParameterBuff": "direct-score",
    "ExamCardUpgrade": "hand-upgrade", "ExamPlayableValueAdd": "extra-play",
    "ExamHandGraveCountCardDraw": "redraw", "ExamCardDraw": "draw",
    "ExamStaminaReduceFix": "immediate-cost",
    "ExamStaminaConsumptionAdd": "future-cost",
    "ExamCardMove": "retrieve",
    "ExamCardCreateSearch": "create-card",
    "ExamSearchPlayCardStaminaConsumptionChange": "resource",
    "ExamForcePlayCardSearch": "forced-play",
}
_SCALAR_STATUSES = {
    "ParameterBuffStatusEffect", "LessonBuffStatusEffect", "ParameterBuffMultiplePerTurnStatusEffect",
    "PlayableValueAddStatusEffect", "StaminaConsumptionDownStatusEffect", "StaminaConsumptionAddStatusEffect",
    "StaminaConsumptionDownFixStatusEffect", "AntiDebuffStatusEffect", "LessonParameterMultipleStatusEffect",
    "LessonBuffAdditiveStatusEffect", "LessonBuffMultipleStatusEffect",
}
_STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"
_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
_END_TURN = "ProduceExamPhaseType_ExamEndTurn"
_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
_START_PLAY = "ProduceExamPhaseType_StartPlay"
_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"


def _kind(candidate):
    return {"use-hand": "play", "use-drink": "drink"}.get(candidate.get("kind"), candidate.get("kind"))


def _listener_shape_supported(trigger, *, item_direct):
    """Admission audit only; actual condition/effect execution stays bridge-owned.

    Admit the structurally closed event forms already interpreted by the
    bridge. Unknown condition families are not silently treated as false.
    """
    phases = tuple(trigger.phase_types)
    if len(phases) != 1 or phases[0] not in {_STATUS_CHANGE, _CARD_PLAY, _INTERVAL, _END_TURN, _START_TURN, _START_PLAY, _CARD_PLAY_AFTER}:
        return False
    if item_direct:
        if phases[0] == _CARD_PLAY_AFTER:
            return (not trigger.phase_values and not trigger.field_status_check_types and not trigger.field_status_types
                and not trigger.field_status_values and not trigger.field_status_card_search_ids and not trigger.effect_types
                and trigger.card_search_rule is not None and trigger.upper_search_count == 0 and trigger.lower_search_count == 1
                and trigger.card_search_rule.card_position_type == "ProduceCardPositionType_Target"
                and exact_playing_card_search_mismatches(trigger.card_search_rule,
                    expected_categories=("ProduceCardCategory_ActiveSkill",), expected_effect_group_ids=()) == ("card_position_type",))
        # The existing P-item parser restores its typed source, limit and phase
        # counters in these three callbacks. StatusChange is not that route.
        return phases[0] in {_CARD_PLAY, _INTERVAL, _START_TURN}
    if (trigger.field_status_check_types or trigger.field_status_card_search_ids
            or trigger.card_move_position_type != "ProduceCardMovePositionType_Unknown"
            or trigger.lesson_type != "ProduceStepLessonType_Unknown"):
        return False
    if phases[0] == _STATUS_CHANGE:
        if not set(trigger.effect_types) <= {_PREFIX + "ExamLessonBuff", _PREFIX + "ExamParameterBuff"}:
            return False
        if trigger.produce_card_search_id or trigger.upper_search_count or trigger.lower_search_count:
            return False
        if trigger.field_status_types or trigger.field_status_values:
            return (not trigger.phase_values and len(trigger.field_status_types) == len(trigger.field_status_values) == 1
                    and trigger.field_status_types[0] in {"ProduceExamFieldStatusType_LessonBuffUp", "ProduceExamFieldStatusType_ParameterBuffUp"})
        return (not trigger.phase_values or
                len(trigger.phase_values) == 1 and tuple(trigger.effect_types) == (_PREFIX + "ExamLessonBuff",))
    if phases[0] == _INTERVAL:
        return (not trigger.field_status_types and not trigger.field_status_values and not trigger.effect_types
                and len(trigger.phase_values) == 1 and trigger.phase_values[0] > 0
                and not trigger.upper_search_count and not trigger.lower_search_count
                and (not trigger.produce_card_search_id or trigger.card_search_rule is not None))
    if phases[0] == _CARD_PLAY:
        return (not trigger.phase_values and not trigger.field_status_types and not trigger.field_status_values
                and not trigger.effect_types and trigger.upper_search_count == 0 and trigger.lower_search_count == 1
                and trigger.card_search_rule is not None and not exact_playing_card_search_mismatches(
                    trigger.card_search_rule, expected_categories=("ProduceCardCategory_ActiveSkill",), expected_effect_group_ids=()))
    if phases[0] in {_END_TURN, _START_PLAY}:
        return (not trigger.phase_values and not trigger.effect_types and not trigger.produce_card_search_id
                and not trigger.upper_search_count and not trigger.lower_search_count
                and len(trigger.field_status_types) == len(trigger.field_status_values)
                and set(trigger.field_status_types) <= {"ProduceExamFieldStatusType_LessonBuffUp", "ProduceExamFieldStatusType_ParameterBuffUp"})
    return False


def audit_plan1_drink_comparison_input(projection, *, database=DEFAULT_DATABASE):
    """Independently inventory the local branch's captured status dependencies."""
    blockers, listeners = [], []
    if not isinstance(projection, Plan1RuntimeStateProjection) or projection.state is None or projection.parsed is None:
        return {"complete": False, "blockers": ["plan1-comparison-projection-unavailable"], "listeners": []}
    blockers.extend(f"{value.code}:{value.detail}" for value in projection.blockers)
    if not projection.state.scalar.is_battle:
        blockers.append("plan1-comparison-native-battle-scoring-not-bound")
    root = _opaque_runtime(projection.parsed)
    status, refs = root.get("status"), root.get("references")
    links = status.get("_effectList") if isinstance(status, Mapping) else None
    rows = refs.get("RefIds") if isinstance(refs, Mapping) else None
    if not isinstance(links, list) or not isinstance(rows, list) or not isinstance(root.get("drinkList"), list):
        return {"complete": False, "blockers": [*blockers, "plan1-comparison-explicit-status-and-drink-graph-required"], "listeners": []}
    by_rid = {}
    for row in rows:
        if not isinstance(row, Mapping) or type(row.get("rid")) is not int or row["rid"] in by_rid:
            blockers.append("plan1-comparison-invalid-or-duplicate-reference")
            continue
        by_rid[row["rid"]] = row
    active_classes = []
    for link in links:
        row = by_rid.get(link.get("rid")) if isinstance(link, Mapping) else None
        if not isinstance(row, Mapping) or not isinstance(row.get("type"), Mapping) or not isinstance(row.get("data"), Mapping):
            blockers.append("plan1-comparison-unresolved-active-reference")
            continue
        kind, data = row["type"].get("class"), row["data"]
        active_classes.append(kind)
        if kind == "SearchPlayCardStaminaConsumptionChangeStatusEffect":
            try:
                from .plan2_search_play_card_stamina_consumption_change import restore_search_play_card_stamina_status
                restored = restore_search_play_card_stamina_status(data, database=Path(database))
                if restored not in projection.state.scalar.search_play_card_stamina_runtime.statuses:
                    raise ValueError("native search-stamina status is absent from the scalar runtime")
            except (ValueError, TypeError, KeyError, OSError) as error:
                blockers.append(f"plan1-comparison-search-stamina-status-unavailable:{error}")
            continue
        if kind in _SCALAR_STATUSES:
            turn = data.get("_turn")
            if (type(turn) is not int or turn < -1 or type(data.get("_uid")) is not int or data["_uid"] < 1
                    or type(data.get("_isPassingTurnStart")) is not bool
                    or type(data.get("_isTurnLimited")) is not bool or data["_isTurnLimited"] != (turn != -1)):
                blockers.append(f"plan1-comparison-scalar-status-lifetime-incomplete:{kind}")
            if kind in {"LessonBuffStatusEffect", "LessonBuffMultipleStatusEffect", "PlayableValueAddStatusEffect",
                        "LessonParameterMultipleStatusEffect", "LessonBuffAdditiveStatusEffect"} and (
                            type(data.get("_value")) is not int or data["_value"] < 0):
                blockers.append(f"plan1-comparison-scalar-status-value-incomplete:{kind}")
            if kind == "LessonBuffMultipleStatusEffect" and data.get("_turn") != -1:
                blockers.append("plan1-comparison-finite-lesson-buff-multiple-lifetime-unbound")
            continue
        if kind != "TriggerEffectStatusEffect":
            blockers.append(f"plan1-comparison-unrepresented-active-status:{kind}")
            continue
        try:
            trigger_data = data.get("_trigger", {})
            trigger = load_exam_status_trigger(trigger_data.get("_id"), Path(database))
            enchant = load_runtime_status_enchant(data.get("_statusEnchantId"), Path(database))
            observed_ids = tuple(value.get("_id") for value in data.get("_effectList", []) if isinstance(value, Mapping))
            if enchant.trigger.id != trigger.id or observed_ids != tuple(value.id for value in enchant.effects):
                raise ValueError("listener-trigger-or-effects-disagree-with-master")
            if tuple(trigger_data.get("_phaseTypeList", [])) != tuple(SERIALIZED_PHASE_ENUM[value] for value in trigger.phase_types):
                raise ValueError("listener-native-phase-disagrees-with-master")
            if tuple(trigger_data.get("_phaseValueList", [])) != tuple(trigger.phase_values):
                raise ValueError("listener-native-phase-values-disagree-with-master")
            item = data.get("_isItemDirectEnchant") is True
            if not _listener_shape_supported(trigger, item_direct=item):
                raise ValueError("listener-condition-shape-outside-existing-bridge")
            # Non-item scalar callbacks do not carry a consumed finite-use
            # counter back into the reference graph. Such rows are not admitted.
            if not item:
                finite_phase = (trigger.phase_types in {(_END_TURN,), (_START_PLAY,)}
                    and type(data.get("_turn")) is int and data["_turn"] > 0
                    and data.get("_isTurnLimited") is True and type(data.get("_isPassingTurnStart")) is bool)
                if data.get("_limitCount") != -1 or data.get("_turn") != -1 and not finite_phase:
                    raise ValueError("nonitem-listener-finite-counter-continuation-unbound")
            for identity in observed_ids:
                if item:
                    # The existing P-item callbacks own the event-conditioned
                    # child compiler (including interval Additive). Both
                    # compared PLAY transitions must pass those callbacks.
                    continue
                compiled = _promote_transition_local_effect_installers((compile_plan1_effect(identity, database=Path(database)),))[0]
                if compiled.blockers:
                    raise ValueError("listener-child-compiler-blocked")
            listeners.append({"status_id": enchant.id, "phase_types": list(trigger.phase_types),
                              "effect_types": list(trigger.effect_types), "item_direct": item,
                              "source_rid": link["rid"]})
        except (KeyError, TypeError, ValueError, OSError) as error:
            blockers.append(f"plan1-comparison-listener-scope:{type(error).__name__}:{error}")
    return {"complete": not blockers, "blockers": list(dict.fromkeys(blockers)), "listeners": listeners,
            "active_status_classes": active_classes, "captured_digest": projection.captured_digest,
            "proof_scope": "local-comparison-input-dependencies; not-native-or-training-promotion"}


@dataclass(frozen=True)
class Plan1DrinkComparisonState:
    projection: Plan1RuntimeStateProjection
    drinks: tuple
    turns_remaining: int
    terminal: bool = False
    drink_prefix_used: bool = False
    play_completed: bool = False
    drink_effects: tuple = ()
    extra_play_budget: int = 0
    additional_plays_used: int = 0
    selection_variants: tuple[object, ...] = ()


class Plan1DrinkComparisonAdapter:
    def __init__(self, projection, *, database=DEFAULT_DATABASE):
        self.database = Path(database)
        self.audit = audit_plan1_drink_comparison_input(projection, database=self.database)
        self._compiled = {}
        self.prefix_blockers = {}

    def view(self, state):
        scalar = state.projection.state.scalar
        return DrinkStateView(scalar.score, scalar.stamina, scalar.max_stamina, scalar.turn, state.turns_remaining,
            scalar.plays_remaining, scalar.play_count, state.terminal,
            tuple((key, getattr(scalar, key)) for key in ("block", "lesson_buff", "parameter_buff_turns", "parameter_buff_multiple_per_turn_turns")),
            tuple(value.instance_id for value in state.drinks),
            hashlib.sha256(repr(state.projection.state.zones).encode("utf-8")).hexdigest())

    def _drink(self, state, candidate):
        slot = candidate.get("slot_index", candidate.get("drink_slot_index"))
        if type(slot) is not int or not 0 <= slot < len(state.drinks):
            raise ValueError("drink-slot-outside-observed-inventory")
        observed = state.drinks[slot]
        expected = Plan1NativeLegalCandidate.drink(observed)
        if (candidate.get("action_id") != expected.action_id or candidate.get("drink_id") != observed.drink_id
                or candidate.get("instance_id", observed.instance_id) != observed.instance_id
                or candidate.get("selected_card_guid")):
            raise ValueError("drink-candidate-native-identity-mismatch")
        return slot, observed

    def drink_roles(self, state, candidate):
        try:
            _slot, observed = self._drink(state, candidate)
            if observed.drink_id not in self._compiled:
                self._compiled[observed.drink_id] = _compile_plan1_drink(observed.drink_id, database=self.database)
            _drink, effects, blockers = self._compiled[observed.drink_id]
            if blockers:
                raise ValueError(";".join(f"{value.code}:{value.detail}" for value in blockers))
            # The formal bridge now commits each direct effect before its
            # unlimited scalar StatusChange children. Finite counters and
            # unfamiliar listeners are rejected by the graph audit above.
            roles = tuple(dict.fromkeys(_SCALAR_EFFECT_ROLES.get(effect.effect_type.removeprefix(_PREFIX), "unsupported") for effect in effects))
            if any(effect.effect_type == _PREFIX + "ExamStatusEnchant" for effect in effects):
                roles = []
                for effect in effects:
                    if effect.effect_type != _PREFIX + "ExamStatusEnchant":
                        roles.append(_SCALAR_EFFECT_ROLES.get(effect.effect_type.removeprefix(_PREFIX), "unsupported"))
                        continue
                    enchant = load_runtime_status_enchant(effect.status_enchant_id, self.database)
                    if (enchant.trigger.phase_types not in {(_END_TURN,), (_START_PLAY,)}
                            or not _listener_shape_supported(enchant.trigger, item_direct=False)):
                        raise ValueError("installed-drink-listener-phase-contract-unavailable")
                    child_roles = []
                    for child in enchant.effects:
                        compiled = compile_plan1_effect(child.id, database=self.database)
                        if compiled.blockers:
                            raise ValueError("installed-drink-listener-child-core-unavailable")
                        child_roles.append(_SCALAR_EFFECT_ROLES.get(compiled.effect_type.removeprefix(_PREFIX), "unsupported"))
                    if "unsupported" in child_roles:
                        raise ValueError("installed-drink-listener-child-role-unavailable")
                    roles.append("persistent-focus" if enchant.trigger.phase_types == (_END_TURN,) and
                                 all(child.effect_type == _PREFIX + "ExamLessonBuff" for child in enchant.effects) else "start-play-draw")
                roles = tuple(dict.fromkeys(roles))
            if not roles or "unsupported" in roles:
                raise ValueError("drink-effect-outside-implemented-prefix-scope")
            return roles
        except (KeyError, TypeError, ValueError, OSError) as error:
            self.prefix_blockers[candidate.get("action_id", "")] = str(error)
            return ("unsupported",)

    def transition(self, state, candidate, *, _selected_guids=None):
        if not self.audit["complete"]:
            return DrinkTransition(blockers=tuple(self.audit["blockers"]))
        if state.terminal or state.play_completed and (not state.drink_prefix_used or state.additional_plays_used >= state.extra_play_budget):
            return DrinkTransition(blockers=("plan1-comparison-one-play-horizon-exhausted",))
        try:
            if _kind(candidate) == "drink":
                if state.drink_prefix_used:
                    raise ValueError("plan1-comparison-one-bottle-prefix-only")
                slot, _observed = self._drink(state, candidate)
                if "unsupported" in self.drink_roles(state, candidate):
                    raise ValueError(self.prefix_blockers.get(candidate.get("action_id"), "unsupported-drink-prefix"))
                effects = self._compiled[_observed.drink_id][1]
                selected_moves = [effect for effect in effects if effect.card_move_contract is not None
                                  and effect.card_move_contract.pick_range_type == "ProducePickRangeType_Select"]
                if selected_moves and _selected_guids is None:
                    if len(selected_moves) != 1 or selected_moves[0].card_move_search.id != "p_card_search-deck_grave":
                        raise ValueError("drink-selection-complete-branch-contract-unavailable")
                    targets = (*state.projection.state.zones.deck, *state.projection.state.zones.grave)
                    branches = [self.transition(state, candidate, _selected_guids=(card.guid,)) for card in targets]
                    if not branches or any(not branch.supported for branch in branches):
                        return DrinkTransition(blockers=("drink-selection-not-all-branches-supported", *tuple(dict.fromkeys(
                            reason for branch in branches for reason in branch.blockers))))
                    return DrinkTransition(replace(branches[0].after, selection_variants=tuple(branch.after for branch in branches)))
                action = LeaderboardReplayAction(0, "use-drink", (slot,))
            elif _kind(candidate) == "play":
                if state.play_completed:
                    counter_blockers = self._extra_play_counter_blockers(state)
                    if counter_blockers:
                        return DrinkTransition(blockers=counter_blockers)
                guid = candidate.get("card_guid")
                matches = [i for i, card in enumerate(state.projection.state.zones.hand) if card.guid == guid]
                if len(matches) != 1 or candidate.get("action_id") != f"PLAY:{guid}":
                    raise ValueError("play-guid-not-unique-in-current-hand")
                slot = matches[0]
                observed_slot = candidate.get("hand_slot", candidate.get("hand_index", candidate.get("slot_index")))
                if observed_slot is not None and observed_slot != slot:
                    raise ValueError("play-guid-and-observed-slot-mismatch")
                card = state.projection.state.zones.hand[slot]
                if candidate.get("card_id", card.card_id) != card.card_id:
                    raise ValueError("play-guid-and-card-id-mismatch")
                action = LeaderboardReplayAction(int(state.drink_prefix_used) + int(state.play_completed), "use-hand", (slot,))
            else:
                raise ValueError("plan1-comparison-requires-play-or-drink")
            applied = apply_plan1_runtime_action(state.projection, action, database=self.database,
                **({"drink_selected_card_guids": _selected_guids} if _selected_guids is not None else {}))
            blockers = tuple(f"{value.code}:{value.detail}" for value in applied.blockers)
            if _kind(candidate) == "play" and applied.program is not None and any(
                effect.effect_type == _PREFIX + "ExamStatusEnchant" for effect in applied.program.effects
            ):
                # The bridge intentionally treats a newly installed listener
                # as scalar-neutral until the next captured observation. That
                # admission is not evidence for its same-input end-turn effects.
                blockers += ("plan1-comparison-new-card-listener-continuation-unbound",)
            if blockers or not applied.supported or not applied.legal or applied.next_state is None:
                return DrinkTransition(blockers=blockers or ("plan1-bridge-local-transition-unavailable",))
            if _kind(candidate) == "drink":
                if applied.drink_application is None or tuple(applied.drink_application.inventory_before) != tuple(value.drink_id for value in state.drinks):
                    raise ValueError("plan1-drink-application-inventory-mismatch")
                # The bridge owns scalar and ordered-zone effects. Its typed
                # branch remains simulated; the native source graph is intact.
                extra = applied.next_state.scalar.plays_remaining - state.projection.state.scalar.plays_remaining
                after = replace(state, projection=replace(applied.projection, state=applied.next_state),
                                drinks=state.drinks[:slot] + state.drinks[slot + 1:], drink_prefix_used=True,
                                drink_effects=applied.drink_application.compiled_effects, extra_play_budget=extra)
                installs = tuple((effect, state.projection.state.scalar.turn) for effect in after.drink_effects
                                 if effect.effect_type == _PREFIX + "ExamStatusEnchant")
                if installs:
                    after = replace(after, projection=replace(after.projection,
                        local_status_installs=(*state.projection.local_status_installs, *installs)))
            else:
                terminal = applied.turn_end is not None and applied.turn_end.terminal
                elapsed = applied.next_state.scalar.turn - state.projection.state.scalar.turn
                if elapsed < 0:
                    raise ValueError("plan1-comparison-turn-regressed")
                after = replace(state, projection=replace(applied.projection, state=applied.next_state),
                                terminal=terminal, turns_remaining=0 if terminal else max(0, state.turns_remaining - elapsed),
                                play_completed=True, additional_plays_used=state.additional_plays_used + int(state.play_completed))
            return DrinkTransition(after)
        except (KeyError, TypeError, ValueError, OSError, IndexError) as error:
            return DrinkTransition(blockers=(f"plan1-drink-comparison:{type(error).__name__}:{error}",))

    def verify_drink_opportunity(self, before, after, _drink, baseline):
        """Accept only the structural changes the actual core just executed."""
        a, b = before.projection.state, after.projection.state
        expected_extra = sum(effect.effect_count for effect in after.drink_effects if effect.effect_type == _PREFIX + "ExamPlayableValueAdd")
        blockers = []
        if b.scalar.plays_remaining - a.scalar.plays_remaining != expected_extra or not 0 <= expected_extra <= 1:
            blockers.append("extra-play-effect-and-opportunity-count-disagree")
        zone_effects = {effect.effect_type.removeprefix(_PREFIX) for effect in after.drink_effects}
        redraw = "ExamHandGraveCountCardDraw" in zone_effects
        drawing = redraw or "ExamCardDraw" in zone_effects
        creating = "ExamCardCreateSearch" in zone_effects
        forced = "ExamForcePlayCardSearch" in zone_effects
        zone_continuation = drawing or "ExamCardMove" in zone_effects or creating
        if not zone_continuation and not any(card.guid == baseline["card_guid"] for card in b.zones.hand):
            blockers.append("baseline-guid-no-longer-in-local-hand")
        upgrades = []
        if zone_continuation or forced:
            old_guids, new_guids = {card.guid for card in a.zones.card_universe}, {card.guid for card in b.zones.card_universe}
            valid_creation = (creating and old_guids < new_guids and len(new_guids-old_guids) == 1
                              and all(guid.startswith("simulated-drink:") for guid in new_guids-old_guids))
            if (a.zones.binding != b.zones.binding or a.zones.pending_played != b.zones.pending_played
                    or old_guids != new_guids and not valid_creation):
                blockers.append("draw-prefix-changed-card-universe-or-pending-play")
            if b.scalar.total_effect_draw_card_count < a.scalar.total_effect_draw_card_count:
                blockers.append("draw-prefix-counter-regressed")
            if forced and not zone_continuation and replace(a.zones, random_state=b.zones.random_state) != b.zones:
                blockers.append("empty-force-selection-changed-non-RNG-zones")
        elif a.zones != b.zones:
            if not any(effect.effect_type == _PREFIX + "ExamCardUpgrade" for effect in after.drink_effects):
                blockers.append("zone-change-without-implemented-upgrade-effect")
            if (a.zones.random_state != b.zones.random_state or a.zones.binding != b.zones.binding
                    or any(getattr(a.zones, name) != getattr(b.zones, name) for name in ("deck", "grave", "lost", "pending_played"))
                    or [(c.guid, c.card_id) for c in a.zones.hand] != [(c.guid, c.card_id) for c in b.zones.hand]):
                blockers.append("hand-upgrade-changed-order-identity-or-other-zones")
            else:
                for old, new in zip(a.zones.hand, b.zones.hand):
                    if new.effective_upgrade < old.effective_upgrade:
                        blockers.append("hand-upgrade-regressed-an-instance")
                    if old.effective_upgrade != new.effective_upgrade:
                        upgrades.append({"guid": old.guid, "before": old.effective_upgrade, "after": new.effective_upgrade})
        for variant in after.selection_variants:
            proof = self.verify_drink_opportunity(before, variant, _drink, baseline)
            blockers.extend(proof["blockers"])
        return {"supported": not blockers, "extra_plays": expected_extra, "upgraded_cards": upgrades,
                "continuation_rebind_required": zone_continuation, "redraw": redraw,
                "blockers": blockers, "source": "existing-Plan1-core-typed-drink-effects", "native_state_after_claimed": False}

    def compare_rebound_continuation(self, before_drink, after_drink, baseline_action, baseline_state):
        """Re-evaluate the actual simulated hand after a proven zone effect.

        A redraw replaces the complete hand. An ordinary draw compares only
        newly available cards and the original baseline, so a different card
        already in Hand cannot be presented as a benefit caused by drinking.
        None of these advisory identities is submitted to the game.
        """
        if after_drink.selection_variants:
            branches = [self.compare_rebound_continuation(before_drink, variant, baseline_action, baseline_state)
                        for variant in after_drink.selection_variants]
            if any(not branch.supported for branch in branches):
                return DrinkTransition(blockers=("not-all-selection-continuations-proven",))
            worst = min(branches, key=lambda branch: (self.view(branch.after).score, self.view(branch.after).stamina))
            return DrinkTransition(worst.after, comparison={**dict(worst.comparison or {}),
                "selection_outcomes_compared": len(branches), "selection_value": "minimum-across-complete-local-target-set",
                "actual_secondary_owner_unchanged": True})
        before_guids = {card.guid for card in before_drink.projection.state.zones.hand}
        redraw = any(effect.effect_type == _PREFIX + "ExamHandGraveCountCardDraw" for effect in after_drink.drink_effects)
        baseline = self.view(baseline_state)
        options, evaluated = [], []
        for slot, card in enumerate(after_drink.projection.state.zones.hand):
            if not redraw and card.guid in before_guids and card.guid != baseline_action["card_guid"]:
                continue
            candidate = {"kind": "play", "action_id": f"PLAY:{card.guid}", "card_guid": card.guid,
                         "card_id": card.card_id, "hand_slot": slot}
            result = self.transition(after_drink, candidate)
            row = {"action_id": candidate["action_id"], "blockers": list(result.blockers)}
            evaluated.append(row)
            if not result.supported:
                continue
            after = self.view(result.after)
            aligned = ((after.current_turn, after.turns_remaining, after.plays_remaining, after.terminal, after.exam_card_play_count)
                == (baseline.current_turn, baseline.turns_remaining, baseline.plays_remaining, baseline.terminal, baseline.exam_card_play_count))
            row.update(score=after.score, stamina=after.stamina, boundary_aligned=aligned)
            if aligned:
                options.append((after.score, after.stamina, -slot, result.after, card.guid))
        if not options:
            return DrinkTransition(blockers=("no-supported-rebound-play-at-aligned-boundary",), comparison={"candidates": evaluated})
        best = max(options, key=lambda row: row[:3])
        return DrinkTransition(best[3], comparison={"card_guid": best[4], "candidates": evaluated,
            "authority": "local-Plan1-core-simulation", "automatic_execution_allowed": False})

    def _extra_play_counter_blockers(self, state):
        if state.projection.state.scalar.turn != state.projection.parsed.current_turn:
            return ("extra-play-prefix-crossed-captured-turn",)
        root = _opaque_runtime(state.projection.parsed)
        active = {row["rid"] for row in root["status"]["_effectList"]}
        for row in root["references"]["RefIds"]:
            data = row.get("data", {})
            if row.get("rid") not in active or data.get("_isItemDirectEnchant") is not True or data.get("_limitCount") == 0:
                continue
            if SERIALIZED_PHASE_ENUM[_CARD_PLAY] in data.get("_trigger", {}).get("_phaseTypeList", ()) and (
                    data.get("_limitCount", -1) > 0 or data.get("_limitCountInTurnRemain", -1) > 0):
                return ("extra-play-finite-item-counter-continuation-unbound",)
        return ()

    def align_extra_play(self, state, baseline_state):
        """At most one extra simulated PLAY, joined to the same end boundary.

        The only dispatched recommendation remains the original drink. Every
        extra-card candidate is local core simulation and requires a new game
        observation before any later actual PLAY can be chosen.
        """
        baseline = self.view(baseline_state)
        def aligned(view, extra):
            return ((view.current_turn, view.turns_remaining, view.plays_remaining, view.terminal)
                == (baseline.current_turn, baseline.turns_remaining, baseline.plays_remaining, baseline.terminal)
                and view.exam_card_play_count == baseline.exam_card_play_count + extra)
        if aligned(self.view(state), 0):
            return DrinkTransition(state, comparison={"extra_play_count": 0, "reason": "extra-play-opportunity-not-consumed"})
        options, evaluated = [], []
        for slot, card in enumerate(state.projection.state.zones.hand):
            candidate = {"kind": "play", "action_id": f"PLAY:{card.guid}", "card_guid": card.guid,
                         "card_id": card.card_id, "hand_slot": slot}
            result = self.transition(state, candidate)
            audit = {"action_id": candidate["action_id"], "blockers": list(result.blockers)}
            evaluated.append(audit)
            if not result.supported:
                continue
            after = self.view(result.after)
            audit.update(score=after.score, stamina=after.stamina, boundary_aligned=aligned(after, 1))
            if not aligned(after, 1):
                continue
            regress = after.stamina < baseline.stamina or any(dict(after.resources)[key] < value for key, value in baseline.resources)
            paid_score = after.score > baseline.score and (
                baseline.terminal and after.terminal and after.stamina >= 0
                or after.stamina >= max(1, after.max_stamina // 4))
            if after.score < baseline.score or regress and not paid_score:
                audit["blockers"] = ["additional-play-resource-or-score-regression"]
                continue
            options.append((after.score, after.stamina, -slot, result.after, candidate["action_id"]))
        if not options:
            reasons = tuple(dict.fromkeys(reason for row in evaluated for reason in row.get("blockers", ())))
            return DrinkTransition(blockers=reasons or ("no-supported-extra-play-at-aligned-boundary",), comparison={"candidates": evaluated})
        best = max(options, key=lambda row: row[:3])
        return DrinkTransition(best[3], comparison={"extra_play_count": 1, "extra_action_id": best[4],
            "candidate_authority": "local-Plan1-core-simulation", "automatic_execution_allowed": False, "candidates": evaluated})


def recommend_plan1_drink_before_play(projection, *, legal_candidates, baseline_action_id, context,
                                     database=DEFAULT_DATABASE):
    """Use one live caller's projection and legal snapshot, without input ownership."""
    adapter = Plan1DrinkComparisonAdapter(projection, database=database)
    blockers = [*context.coverage_blockers, *adapter.audit["blockers"]]
    parsed = getattr(projection, "parsed", None)
    if (context.plan_type != "ProducePlanType_Plan1" or context.main_effect_type not in {
            _PREFIX + "ExamLessonBuff", _PREFIX + "ExamParameterBuff"}):
        blockers.append("plan1-drink-main-effect-unavailable")
    if parsed is None or parsed.exam_type != 1 or parsed.step_type_value != _STEP.get(context.stage):
        blockers.append("plan1-drink-native-stage-mismatch")
    context = replace(context, coverage_blockers=tuple(dict.fromkeys(blockers)))
    if parsed is None or projection.state is None:
        return {"schema": ADAPTER_ID, "candidate": None, "blockers": list(context.coverage_blockers), "mechanics_audit": adapter.audit}
    try:
        drinks = _drink_inventory(parsed)
    except (TypeError, ValueError) as error:
        return {"schema": ADAPTER_ID, "candidate": None, "blockers": [f"plan1-comparison-drink-inventory:{error}"], "mechanics_audit": adapter.audit}
    candidates = []
    for value in legal_candidates:
        candidate = dict(value.to_dict() if hasattr(value, "to_dict") else value)
        slot = candidate.get("slot_index", candidate.get("drink_slot_index"))
        if _kind(candidate) == "drink" and "instance_id" not in candidate and type(slot) is int and 0 <= slot < len(drinks):
            candidate["instance_id"] = drinks[slot].instance_id
        candidates.append(candidate)
    root = Plan1DrinkComparisonState(projection, drinks, parsed.remain_turn)
    result = compare_drink_before_play(state=root, legal_candidates=candidates, baseline_action_id=baseline_action_id,
                                      adapter=adapter, context=context)
    result.update(adapter=ADAPTER_ID, mechanics_audit=adapter.audit, prefix_blockers=adapter.prefix_blockers,
                  native_promotion_allowed=False, bridge_exact_flag_unchanged=projection.exact)
    return result
