"""Deterministic full-horizon search over exact native ordered card zones.

This is the strict counterpart to :mod:`gkms_tool.audition_horizon`'s legacy
unknown-order belief state.  It reuses the already reviewed numerical card,
status, item, and terminal-value engine, but card identity/order and every RNG
consumer are carried by ``NativeOrderedZoneState`` and the deterministic
HandAdd support evaluator.  There is no chance fan-out and no game input.

The first supported slice deliberately excludes card-instance effects that the
numerical engine cannot yet write back to the v4 runtime payload (once-only
effects, card status/growth/move state, card creation, and random movement).
Those shapes fail closed during preflight or at the exact transition boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from typing import Iterable, Mapping

from .audition_horizon import (
    AuditionBoundaryModel,
    AuditionBoundaryState,
    MOVE_GRAVE,
    MOVE_LOST,
    PLAN_COMMON,
    PLAN_LOGIC,
    HorizonActionEvaluation,
    HorizonBlocker,
    HorizonCardRef,
    HorizonDecision,
    HorizonDiagnostics,
    HorizonExpectedValue,
    HorizonSearchLimits,
    VerifiedTurnSchedule,
    _Blocked,
    _SearchContext,
    _blocker_summary,
    _direct_draw_count,
    _prepare_card_transition,
    _prepare_next_turn,
    _prepare_skip_transition,
    _schedule_blockers,
    apply_audition_terminal_recovery,
)
from .audition_local_save_state import empty_local_save_exam_card_runtime_state
from .audition_native_ordered_zones import (
    NativeEndTurnDisposition,
    NativeOrderedCardInstance,
    NativeOrderedZoneError,
    NativeOrderedZoneState,
)
from .audition_native_support import (
    NativeHandAddCard,
    NativeHandAddSupportError,
    evaluate_native_hand_add_support,
)
from .audition_rules import AuditionRules
from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import ProduceCardSearchRule
from .item_rules import EquippedItemRule
from .logic_engine import LogicExamState, LogicTransition, MasterCard


NATIVE_DETERMINISTIC_DRAW_SEMANTICS = (
    "native-deterministic-v1: exact ordered Deck prefix; on shortage move "
    "ordered Grave to Deck and replay Android xorshift32/Fisher-Yates; then "
    "replay HandAdd supports in drawn-card and loadout order"
)

_TROUBLE_CATEGORY = "ProduceCardCategory_Trouble"
_NATIVE_ACTION_PREDICTION_SEAL = object()


class _NativeBlocked(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.blocker = HorizonBlocker(code, detail)


@dataclass(frozen=True, slots=True)
class _NativeNode:
    logic_state: LogicExamState
    zones: NativeOrderedZoneState
    used_support_ids: tuple[str, ...] = ()
    boundary: AuditionBoundaryState | None = None


@dataclass(frozen=True, slots=True)
class NativeAuditionActionPrediction:
    """Exact next settled native state for one explicitly bound root action."""

    action_id: str
    hand_index: int | None
    selected_guid: str | None
    before_logic_state: LogicExamState
    before_zones: NativeOrderedZoneState
    before_used_support_ids: tuple[str, ...]
    after_logic_state: LogicExamState
    after_zones: NativeOrderedZoneState
    after_used_support_ids: tuple[str, ...]
    immediate_transition: LogicTransition
    direct_draw_count: int
    terminal: bool
    _factory_seal: object = field(repr=False, compare=False)
    boundary: AuditionBoundaryState | None = None

    def __post_init__(self) -> None:
        if self._factory_seal is not _NATIVE_ACTION_PREDICTION_SEAL:
            raise ValueError(
                "NativeAuditionActionPrediction must come from the exact simulator"
            )
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("action_id must be non-empty text")
        if self.action_id == "END_TURN":
            if self.hand_index is not None or self.selected_guid is not None:
                raise ValueError("END_TURN cannot bind a Hand index or GUID")
        else:
            if (
                not isinstance(self.hand_index, int)
                or isinstance(self.hand_index, bool)
                or self.hand_index < 0
                or not isinstance(self.selected_guid, str)
                or not self.selected_guid.strip()
            ):
                raise ValueError("card actions require a non-negative Hand index and GUID")
        for name in ("before_logic_state", "after_logic_state"):
            if not isinstance(getattr(self, name), LogicExamState):
                raise TypeError(f"{name} must be LogicExamState")
        for name in ("before_zones", "after_zones"):
            if not isinstance(getattr(self, name), NativeOrderedZoneState):
                raise TypeError(f"{name} must be NativeOrderedZoneState")
        if self.before_zones.pending_played is not None:
            raise ValueError("prediction before state must be settled")
        if self.before_zones.binding != self.after_zones.binding:
            raise ValueError("predicted descendant changed its evidence binding")
        if (
            self.before_zones.persistent_card_identity_digest()
            != self.after_zones.persistent_card_identity_digest()
        ):
            raise ValueError("predicted descendant changed persistent card identity")
        for name in ("before_used_support_ids", "after_used_support_ids"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{name} must contain non-empty text")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must not contain duplicates")
            object.__setattr__(self, name, values)
        if not isinstance(self.immediate_transition, LogicTransition):
            raise TypeError("immediate_transition must be LogicTransition")
        if self.immediate_transition.before != self.before_logic_state:
            raise ValueError("immediate transition is not bound to before_logic_state")
        if not self.immediate_transition.legal or self.immediate_transition.unsupported_rules:
            raise ValueError("prediction requires one fully supported transition")
        if self.action_id == "END_TURN":
            if self.immediate_transition.card.id != "gkms_tool-turn-skip":
                raise ValueError("END_TURN transition uses the wrong synthetic card")
        else:
            assert self.hand_index is not None and self.selected_guid is not None
            if self.hand_index >= len(self.before_zones.hand):
                raise ValueError("prediction Hand index is outside before_zones")
            selected = self.before_zones.hand[self.hand_index]
            selected_action_id = f"{selected.card_id}@{selected.effective_upgrade}"
            if (
                selected.guid != self.selected_guid
                or selected_action_id != self.action_id
                or self.immediate_transition.card.id != selected.card_id
                or self.immediate_transition.card.upgrade
                != selected.effective_upgrade
            ):
                raise ValueError("prediction action binding disagrees with before_zones")
        if (
            self.immediate_transition.after != self.after_logic_state
            and not self.immediate_transition.turn_ended
        ):
            # A normal turn-ending action advances through TurnStart before
            # reaching the next actionable state.  Same-turn and terminal
            # actions retain the immediate numerical state exactly.
            raise ValueError("prediction after state is not explained by its transition")
        if (
            not isinstance(self.direct_draw_count, int)
            or isinstance(self.direct_draw_count, bool)
            or self.direct_draw_count < 0
        ):
            raise ValueError("direct_draw_count must be a non-negative integer")
        if not isinstance(self.terminal, bool):
            raise TypeError("terminal must be boolean")
        if self.boundary is not None and not isinstance(
            self.boundary, AuditionBoundaryState
        ):
            raise TypeError("boundary must be AuditionBoundaryState or None")
        if self.terminal != (
            self.after_logic_state.turns_remaining == 0
            or (
                self.boundary is not None
                and self.boundary.exam_end_complete
            )
        ):
            raise ValueError("terminal flag disagrees with after_logic_state/boundary")
        if self.after_zones.pending_played is not None:
            raise ValueError("predicted next settled state cannot retain PlayingCard")

    def to_dict(self) -> dict[str, object]:
        transition = self.immediate_transition
        return {
            "action_id": self.action_id,
            "hand_index": self.hand_index,
            "selected_guid": self.selected_guid,
            "before_logic_state": asdict(self.before_logic_state),
            "before_zones": self.before_zones.to_dict(),
            "before_used_support_ids": list(self.before_used_support_ids),
            "after_logic_state": asdict(self.after_logic_state),
            "after_zones": self.after_zones.to_dict(),
            "after_used_support_ids": list(self.after_used_support_ids),
            # Hash every current and future LogicTransition field.  A selective
            # projection is unsafe for action-CAS because newly modeled costs,
            # raw score, or recovery fields could otherwise collide.
            "immediate_transition": asdict(transition),
            "direct_draw_count": self.direct_draw_count,
            "terminal": self.terminal,
            "boundary": (
                None if self.boundary is None else self.boundary.to_dict()
            ),
        }

    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NativeAuditionActionSimulation:
    """Read-only one-action result; it never grants live input authority."""

    prediction: NativeAuditionActionPrediction | None
    blockers: tuple[HorizonBlocker, ...] = ()

    @property
    def simulation_complete(self) -> bool:
        return self.prediction is not None and not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "simulation_complete": self.simulation_complete,
            "prediction": (
                None if self.prediction is None else self.prediction.to_dict()
            ),
            "prediction_digest": (
                None if self.prediction is None else self.prediction.digest()
            ),
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class _NativeActionAdvance:
    node: _NativeNode
    transition: LogicTransition
    terminal: bool
    boundary: AuditionBoundaryState | None = None


@dataclass(slots=True)
class _NativeContext:
    numerical: _SearchContext
    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...]
    support_card_searches: Mapping[str, ProduceCardSearchRule]
    memo: dict[_NativeNode, HorizonExpectedValue] = field(default_factory=dict)
    deterministic_draws: int = 0
    maximum_draw_fanout: int = 0
    support_rolls: int = 0
    approximation_used: bool = False
    action_branches_pruned: int = 0


def _ref(card: NativeOrderedCardInstance) -> HorizonCardRef:
    return HorizonCardRef(card.card_id, card.effective_upgrade)


def _catalog_card(
    card: NativeOrderedCardInstance,
    catalog: Mapping[HorizonCardRef, MasterCard],
) -> MasterCard:
    ref = _ref(card)
    result = catalog.get(ref)
    if result is None:
        raise _NativeBlocked("native-card-master-missing", ref.key)
    if result.id != ref.card_id or result.upgrade != ref.upgrade:
        raise _NativeBlocked("native-card-master-mismatch", ref.key)
    return result


def _runtime_blockers(
    zones: NativeOrderedZoneState,
) -> tuple[HorizonBlocker, ...]:
    """Gate the narrow v1 card-runtime executor without normalising data."""

    neutral = empty_local_save_exam_card_runtime_state()
    blockers: list[HorizonBlocker] = []
    for card in zones.card_universe:
        runtime = card.runtime_state
        detail = f"{card.card_id}:{card.guid}"
        if runtime.status_effect != neutral.status_effect:
            blockers.append(HorizonBlocker("native-card-status-unsupported", detail))
        if runtime.affect_grow_effect_id_list != neutral.affect_grow_effect_id_list:
            blockers.append(HorizonBlocker("native-card-affect-grow-unsupported", detail))
        if (
            runtime.grow_effect_exam_start_after_list
            != neutral.grow_effect_exam_start_after_list
        ):
            blockers.append(HorizonBlocker("native-card-grow-after-unsupported", detail))
        if (
            runtime.stamina_consumption_specify_effect_list
            != neutral.stamina_consumption_specify_effect_list
        ):
            blockers.append(HorizonBlocker("native-card-stamina-list-unsupported", detail))
        if runtime.is_move_produce_exam_effect_use_in_turn:
            blockers.append(HorizonBlocker("native-card-move-use-unsupported", detail))
        try:
            customize = runtime.customize_count_list.to_value()
        except (TypeError, ValueError) as error:
            blockers.append(
                HorizonBlocker("native-card-customize-invalid", f"{detail}:{error}")
            )
        else:
            if not isinstance(customize, list) or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or value != 0
                for value in customize
            ):
                blockers.append(
                    HorizonBlocker("native-card-customize-unsupported", detail)
                )
    return tuple(blockers)


def _input_blockers(
    state: LogicExamState,
    zones: NativeOrderedZoneState,
    used_support_ids: tuple[str, ...],
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    limits: HorizonSearchLimits,
    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...],
    support_card_searches: Mapping[str, ProduceCardSearchRule],
    boundary_model: AuditionBoundaryModel | None = None,
) -> tuple[HorizonBlocker, ...]:
    blockers: list[HorizonBlocker] = []
    if not isinstance(state, LogicExamState):
        return (HorizonBlocker("logic-state-invalid", "wrong type"),)
    if not isinstance(zones, NativeOrderedZoneState):
        return (HorizonBlocker("native-zones-invalid", "wrong type"),)
    if not isinstance(rules, AuditionRules):
        return (HorizonBlocker("audition-rules-invalid", "wrong type"),)
    if not isinstance(schedule, VerifiedTurnSchedule):
        return (HorizonBlocker("turn-schedule-invalid", "wrong type"),)
    if not isinstance(catalog, Mapping):
        return (HorizonBlocker("master-catalog-invalid", "wrong type"),)
    if not isinstance(item, EquippedItemRule):
        return (HorizonBlocker("equipped-item-rule-invalid", "wrong type"),)
    if not isinstance(limits, HorizonSearchLimits):
        return (HorizonBlocker("search-limits-invalid", "wrong type"),)
    if not isinstance(support_card_searches, Mapping):
        return (HorizonBlocker("support-card-search-input-invalid"),)
    try:
        state.validate()
    except (TypeError, ValueError) as error:
        blockers.append(HorizonBlocker("logic-state-invalid", str(error)))
    try:
        limits.validate()
    except (TypeError, ValueError) as error:
        blockers.append(HorizonBlocker("search-limits-invalid", str(error)))
    if boundary_model is not None and not isinstance(
        boundary_model, AuditionBoundaryModel
    ):
        blockers.append(HorizonBlocker("audition-boundary-model-invalid"))
    else:
        blockers.extend(_schedule_blockers(state, schedule, boundary_model))
    if zones.pending_played is not None:
        blockers.append(HorizonBlocker("native-pending-card-unsettled"))
    if state.created_cards:
        blockers.append(
            HorizonBlocker(
                "native-created-card-history-unsupported",
                repr(state.created_cards),
            )
        )
    if not zones.hand:
        blockers.append(HorizonBlocker("visible-hand-empty"))
    if len(zones.hand) > rules.hand_limit:
        blockers.append(
            HorizonBlocker(
                "visible-hand-exceeds-master-limit",
                f"hand={len(zones.hand)};limit={rules.hand_limit}",
            )
        )
    if state.turns_remaining > rules.turns:
        blockers.append(
            HorizonBlocker(
                "remaining-turns-exceed-master",
                f"remaining={state.turns_remaining};master={rules.turns}",
            )
        )
    if state.turn_end_stamina_recovery != rules.turn_end_stamina_recovery:
        blockers.append(
            HorizonBlocker(
                "turn-end-recovery-master-mismatch",
                f"state={state.turn_end_stamina_recovery};"
                f"master={rules.turn_end_stamina_recovery}",
            )
        )
    if rules.gimmick_group_id:
        blockers.append(
            HorizonBlocker("future-audition-gimmick-not-modeled", rules.gimmick_group_id)
        )
    if item.unsupported_rules:
        blockers.extend(
            HorizonBlocker("equipped-item-rule-unsupported", value)
            for value in item.unsupported_rules
        )
    blockers.extend(_runtime_blockers(zones))

    for instance in zones.card_universe:
        ref = _ref(instance)
        card = catalog.get(ref)
        if card is None:
            blockers.append(HorizonBlocker("native-card-master-missing", ref.key))
            continue
        if card.id != ref.card_id or card.upgrade != ref.upgrade:
            blockers.append(HorizonBlocker("native-card-master-mismatch", ref.key))
        if card.plan_type not in {PLAN_COMMON, PLAN_LOGIC}:
            blockers.append(
                HorizonBlocker("native-card-plan-unsupported", f"{ref.key}:{card.plan_type}")
            )
        if card.move_position_type not in {MOVE_GRAVE, MOVE_LOST}:
            blockers.append(
                HorizonBlocker(
                    "native-card-move-position-unsupported",
                    f"{ref.key}:{card.move_position_type}",
                )
            )
        if (
            card.move_effect_trigger_type
            not in {"", "ProduceCardMoveEffectTriggerType_Unknown"}
            or card.move_effect_ids
            or card.move_trigger_ids
        ):
            blockers.append(HorizonBlocker("native-card-move-effect-unsupported", ref.key))
        if card.produce_card_status_enchant_id:
            blockers.append(
                HorizonBlocker("native-card-status-enchant-unsupported", ref.key)
            )
        if any(effect.once for effect in card.effects):
            blockers.append(HorizonBlocker("native-card-once-effect-unsupported", ref.key))

    support_ids = {value.support_id for value in support_upgrades}
    unknown_used = set(used_support_ids) - support_ids
    if unknown_used:
        blockers.append(
            HorizonBlocker("support-used-id-unknown", sorted(unknown_used)[0])
        )
    installed = {
        support_id
        for card in zones.hand
        for support_id in card.support_upgrade_ids
    }
    if installed - set(used_support_ids):
        blockers.append(
            HorizonBlocker(
                "native-hand-support-not-used",
                ",".join(sorted(installed - set(used_support_ids))),
            )
        )
    for zone_name in ("deck", "grave", "lost"):
        for card in getattr(zones, zone_name):
            if card.support_upgrade_ids:
                blockers.append(
                    HorizonBlocker(
                        "native-non-hand-support-installed",
                        f"{zone_name}:{card.guid}:"
                        + ",".join(card.support_upgrade_ids),
                    )
                )

    if state.lost_card_count != len(zones.lost):
        blockers.append(
            HorizonBlocker(
                "native-lost-count-zone-mismatch",
                f"logic={state.lost_card_count};zones={len(zones.lost)}",
            )
        )
    trouble_count = 0
    trouble_count_complete = True
    for instance in (*zones.hand, *zones.deck, *zones.grave):
        card = catalog.get(_ref(instance))
        if card is None:
            trouble_count_complete = False
            continue
        if card.category == _TROUBLE_CATEGORY:
            trouble_count += 1
    if trouble_count_complete and state.trouble_card_count != trouble_count:
        blockers.append(
            HorizonBlocker(
                "native-trouble-count-zone-mismatch",
                f"logic={state.trouble_card_count};zones={trouble_count}",
            )
        )
    try:
        current_frame = schedule.by_round.get(state.round_number)
    except (TypeError, ValueError):
        current_frame = None
    if current_frame is not None:
        try:
            # Empty-card evaluation still validates every support/search and
            # the exact current-turn used set without consuming RNG.
            evaluate_native_hand_add_support(
                (),
                lesson_type=current_frame.lesson_type,
                random_state=zones.random_state,
                support_upgrades=support_upgrades,
                support_card_searches=support_card_searches,
                used_support_ids=used_support_ids,
            )
        except NativeHandAddSupportError as error:
            blockers.append(HorizonBlocker("native-support-input-invalid", str(error)))
    if (
        state.runtime_status_enchants
        and state.last_resolved_runtime_status_round != state.round_number
    ):
        blockers.append(
            HorizonBlocker(
                "current-turn-runtime-status-not-resolved-before-visible-hand",
                f"round={state.round_number}",
            )
        )
    return tuple(dict.fromkeys(blockers))


def _draw_with_support(
    zones: NativeOrderedZoneState,
    used_support_ids: tuple[str, ...],
    count: int,
    lesson_type: str,
    context: _NativeContext,
) -> tuple[NativeOrderedZoneState, tuple[str, ...]]:
    if count < 0:
        raise _NativeBlocked("negative-resolved-draw-count", str(count))
    if count == 0:
        return zones, used_support_ids
    if count > len(zones.deck) + len(zones.grave):
        raise _NativeBlocked(
            "native-insufficient-draw-cards",
            f"draw={count};available={len(zones.deck) + len(zones.grave)}",
        )
    if len(zones.hand) + count > context.numerical.rules.hand_limit:
        raise _NativeBlocked(
            "native-draw-exceeds-hand-limit",
            f"hand={len(zones.hand)};draw={count};"
            f"limit={context.numerical.rules.hand_limit}",
        )
    try:
        drawn = zones.draw_to_hand(count)
    except NativeOrderedZoneError as error:
        raise _NativeBlocked("native-draw-failed", str(error)) from error
    context.deterministic_draws += 1
    context.maximum_draw_fanout = 1
    descriptors = tuple(
        NativeHandAddCard(
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            effective_upgrade=card.effective_upgrade,
        )
        for card in drawn.drawn_instances
    )
    try:
        support = evaluate_native_hand_add_support(
            descriptors,
            lesson_type=lesson_type,
            random_state=drawn.state.random_state,
            support_upgrades=context.support_upgrades,
            support_card_searches=context.support_card_searches,
            used_support_ids=used_support_ids,
        )
    except NativeHandAddSupportError as error:
        raise _NativeBlocked("native-support-replay-failed", str(error)) from error
    context.support_rolls += len(support.audit_trace)
    next_zones = drawn.state
    by_guid = {card.guid: card for card in next_zones.hand}
    for result in support.cards:
        previous = by_guid[result.guid]
        try:
            updated = previous.install_support_upgrades(result.added_support_ids)
            next_zones = next_zones.replace_hand_runtime(previous, updated)
        except NativeOrderedZoneError as error:
            raise _NativeBlocked("native-support-apply-failed", str(error)) from error
        by_guid[result.guid] = updated
        if updated.effective_upgrade != result.final_effective_upgrade:
            raise _NativeBlocked(
                "native-support-result-mismatch",
                f"guid={result.guid};state={updated.effective_upgrade};"
                f"result={result.final_effective_upgrade}",
            )
    next_zones = replace(next_zones, random_state=support.final_random_state)
    return next_zones, support.used_support_ids


def _end_turn_dispositions(
    zones: NativeOrderedZoneState,
    catalog: Mapping[HorizonCardRef, MasterCard],
) -> Mapping[str, NativeEndTurnDisposition]:
    return {
        card.guid: (
            NativeEndTurnDisposition.LOST
            if _catalog_card(card, catalog).is_end_turn_lost
            else NativeEndTurnDisposition.GRAVE
        )
        for card in zones.hand
    }


def _close_turn(
    logic_state: LogicExamState,
    zones: NativeOrderedZoneState,
    context: _NativeContext,
) -> tuple[LogicExamState, NativeOrderedZoneState]:
    dispositions = _end_turn_dispositions(zones, context.numerical.catalog)
    end_turn_lost = tuple(
        card
        for card in zones.hand
        if dispositions[card.guid] is NativeEndTurnDisposition.LOST
    )
    try:
        closed = zones.close_turn(dispositions)
    except NativeOrderedZoneError as error:
        raise _NativeBlocked("native-reset-hand-failed", str(error)) from error
    if not end_turn_lost:
        return logic_state, closed
    trouble_lost = sum(
        _catalog_card(card, context.numerical.catalog).category
        == "ProduceCardCategory_Trouble"
        for card in end_turn_lost
    )
    # ResetHand moves these cards to Lost outside apply_logic_card/skip, so the
    # zone-aware executor must keep the numerical lost/trouble counters aligned.
    return (
        replace(
            logic_state,
            lost_card_count=logic_state.lost_card_count + len(end_turn_lost),
            trouble_card_count=max(0, logic_state.trouble_card_count - trouble_lost),
        ),
        closed,
    )


def _advance_action(
    node: _NativeNode,
    transition: LogicTransition,
    hand_index: int | None,
    direct_draw_count: int,
    context: _NativeContext,
) -> _NativeActionAdvance:
    if not transition.legal:
        raise _NativeBlocked("illegal-action")
    if transition.unsupported_rules:
        raise _NativeBlocked(
            "logic-transition-unsupported", ",".join(transition.unsupported_rules)
        )
    if transition.after.created_cards != node.logic_state.created_cards:
        raise _NativeBlocked("native-created-card-unsupported")

    zones = node.zones
    used = node.used_support_ids
    selected: NativeOrderedCardInstance | None = None
    selected_master: MasterCard | None = None
    if hand_index is not None:
        selected = zones.hand[hand_index]
        selected_master = _catalog_card(selected, context.numerical.catalog)
        zones, selected = zones.remove_hand(hand_index)

    expected_lost = node.logic_state.lost_card_count
    expected_trouble = node.logic_state.trouble_card_count
    if selected_master is not None:
        if selected_master.move_position_type == MOVE_LOST:
            expected_lost += 1
            if selected_master.category == _TROUBLE_CATEGORY:
                expected_trouble = max(0, expected_trouble - 1)
    if (
        transition.after.lost_card_count != expected_lost
        or transition.after.trouble_card_count != expected_trouble
    ):
        raise _NativeBlocked(
            "native-runtime-zone-count-change-unsupported",
            "logic lost/trouble changed outside the selected card's exact move: "
            f"expected=({expected_lost},{expected_trouble});"
            f"actual=({transition.after.lost_card_count},"
            f"{transition.after.trouble_card_count})",
        )

    after = transition.after
    if context.numerical.rules.force_end_score > 0:
        from .logic_engine import complete_audition_if_forced

        transition = complete_audition_if_forced(
            transition, context.numerical.rules.force_end_score
        )
        after = transition.after
    if direct_draw_count:
        frame = context.numerical.turn_frames[node.logic_state.round_number]
        zones, used = _draw_with_support(
            zones, used, direct_draw_count, frame.lesson_type, context
        )

    if selected is not None and selected_master is not None:
        pending = zones.pending_played
        if pending is None or pending.guid != selected.guid:
            raise _NativeBlocked("native-pending-card-missing", selected.guid)
        try:
            incremented = pending.increment_play_count()
            zones = zones.replace_pending_runtime(pending, incremented)
            if selected_master.move_position_type == MOVE_GRAVE:
                zones = zones.append_played_to_grave(incremented)
            elif selected_master.move_position_type == MOVE_LOST:
                zones = zones.append_played_to_lost(incremented)
            else:
                raise _NativeBlocked(
                    "native-card-move-position-unsupported",
                    selected_master.move_position_type,
                )
        except NativeOrderedZoneError as error:
            raise _NativeBlocked("native-played-card-settle-failed", str(error)) from error

    if transition.turn_ended:
        # Ordinary final turns still execute native ResetHand.  A force-end
        # reached while the card leaves plays_remaining > 0 is different: the
        # static client evidence does not prove ResetHand on that branch, so
        # it is intentionally not synthesized here.
        after, zones = _close_turn(after, zones, context)

    boundary: AuditionBoundaryState | None = None
    model = context.numerical.boundary_model
    if (
        model is not None
        and transition.turn_ended
        and after.turns_remaining > 0
    ):
        boundary = model.resolve(
            next_turn=after.round_number,
            post_boundary_recovery_units=after.turns_remaining,
        )
        if boundary.terminal:
            completed = apply_audition_terminal_recovery(
                after,
                boundary,
                recovery_stamina=(
                    context.numerical.rules.turn_end_stamina_recovery
                ),
                already_recovered=transition.end_turn_stamina_recovered,
            )
            return _NativeActionAdvance(
                _NativeNode(completed, zones, used, boundary),
                transition,
                True,
                boundary,
            )

    if after.turns_remaining == 0:
        return _NativeActionAdvance(
            _NativeNode(after, zones, used, boundary),
            transition,
            True,
            boundary,
        )

    if not transition.turn_ended:
        return _NativeActionAdvance(
            _NativeNode(after, zones, used, boundary),
            transition,
            False,
            boundary,
        )

    try:
        next_state, draw_count = _prepare_next_turn(after, context.numerical)
    except _Blocked:
        raise
    frame = context.numerical.turn_frames.get(next_state.round_number)
    if frame is None:
        raise _NativeBlocked("future-turn-frame-missing", str(next_state.round_number))
    zones, next_used = _draw_with_support(
        zones, (), draw_count, frame.lesson_type, context
    )
    return _NativeActionAdvance(
        _NativeNode(next_state, zones, next_used),
        transition,
        False,
    )


def _finish_action(
    node: _NativeNode,
    transition: LogicTransition,
    hand_index: int | None,
    direct_draw_count: int,
    context: _NativeContext,
) -> tuple[HorizonExpectedValue, LogicTransition]:
    advanced = _advance_action(
        node,
        transition,
        hand_index,
        direct_draw_count,
        context,
    )
    if advanced.terminal:
        return (
            HorizonExpectedValue.terminal(
                advanced.node.logic_state,
                force_end_score=context.numerical.rules.force_end_score,
            ),
            advanced.transition,
        )
    return _node_value(advanced.node, context), advanced.transition


def _card_action(
    node: _NativeNode,
    hand_index: int,
    context: _NativeContext,
) -> tuple[HorizonExpectedValue, LogicTransition]:
    instance = node.zones.hand[hand_index]
    ref = _ref(instance)
    card = _catalog_card(instance, context.numerical.catalog)
    prepared = _prepare_card_transition(node.logic_state, ref, context.numerical)
    if prepared.item_result.unsupported_rules:
        raise _NativeBlocked(
            "equipped-item-transition-unsupported",
            ",".join(prepared.item_result.unsupported_rules),
        )
    transition = prepared.transition
    if not transition.legal:
        raise _NativeBlocked("illegal-action", ref.key)
    item_draw_effects = prepared.item_result.post_card_effects
    if transition.turn_ended:
        item_draw_effects = (
            *item_draw_effects,
            *prepared.item_result.end_turn_effects,
        )
    try:
        direct_draw = _direct_draw_count(card, transition, item_draw_effects)
    except _Blocked:
        raise
    return _finish_action(node, transition, hand_index, direct_draw, context)


def _skip_action(
    node: _NativeNode,
    context: _NativeContext,
) -> tuple[HorizonExpectedValue, LogicTransition]:
    prepared = _prepare_skip_transition(node.logic_state, context.numerical)
    if prepared.item_result.unsupported_rules:
        raise _NativeBlocked(
            "turn-skip-item-unsupported",
            ",".join(prepared.item_result.unsupported_rules),
        )
    if prepared.item_result.post_card_effects or prepared.item_result.fired_enchantment_ids:
        raise _NativeBlocked("turn-skip-item-use-accounting-unverified")
    transition = prepared.transition
    if not transition.legal:
        raise _NativeBlocked(
            "turn-skip-illegal", ",".join(transition.unsupported_rules)
        )
    return _finish_action(node, transition, None, 0, context)


def _candidate_indices(
    node: _NativeNode,
    context: _NativeContext,
) -> tuple[int | None, ...]:
    ranked: list[tuple[tuple[int, int, int, str, str], int]] = []
    for index, instance in enumerate(node.zones.hand):
        ref = _ref(instance)
        card = _catalog_card(instance, context.numerical.catalog)
        preview = _prepare_card_transition(node.logic_state, ref, context.numerical).transition
        if not preview.legal:
            continue
        ranked.append(
            (
                (
                    card.evaluation,
                    preview.total_score_gain,
                    -card.stamina_cost,
                    ref.key,
                    instance.guid,
                ),
                index,
            )
        )
    ranked.sort(reverse=True)
    if len(ranked) > context.numerical.limits.action_beam_width:
        if not context.numerical.limits.allow_advisory_beam:
            raise _NativeBlocked(
                "action-beam-width-exceeded",
                f"actions={len(ranked)};limit={context.numerical.limits.action_beam_width}",
            )
        removed = len(ranked) - context.numerical.limits.action_beam_width
        context.approximation_used = True
        context.action_branches_pruned += removed + 1
        ranked = ranked[: context.numerical.limits.action_beam_width]
        return tuple(index for _key, index in ranked)
    return (*tuple(index for _key, index in ranked), None)


def _node_value(
    node: _NativeNode,
    context: _NativeContext,
) -> HorizonExpectedValue:
    if node.logic_state.turns_remaining == 0:
        return HorizonExpectedValue.terminal(
            node.logic_state,
            force_end_score=context.numerical.rules.force_end_score,
        )
    cached = context.memo.get(node)
    if cached is not None:
        return cached
    context.numerical.expand_node()
    values: list[HorizonExpectedValue] = []
    blockers: list[HorizonBlocker] = []
    for index in _candidate_indices(node, context):
        try:
            value, _transition = (
                _skip_action(node, context)
                if index is None
                else _card_action(node, index, context)
            )
        except (_NativeBlocked, _Blocked) as error:
            blocker = error.blocker
            if blocker.code == "search-node-limit-exceeded":
                raise
            blockers.append(blocker)
            continue
        values.append(value)
    incomplete = tuple(
        blocker
        for blocker in blockers
        if blocker.code not in {"illegal-action", "turn-skip-illegal"}
    )
    if incomplete:
        raise _NativeBlocked(
            "future-action-set-incomplete", _blocker_summary(incomplete)
        )
    if not values:
        raise _NativeBlocked("no-supported-future-action", _blocker_summary(blockers))
    best = max(values, key=lambda value: value.sort_key)
    context.memo[node] = best
    return best


def simulate_native_audition_action(
    state: LogicExamState,
    zones: NativeOrderedZoneState,
    used_support_ids: Iterable[str],
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    *,
    action_id: str,
    hand_index: int | None,
    limits: HorizonSearchLimits = HorizonSearchLimits(),
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] = {},
    boundary_model: AuditionBoundaryModel | None = None,
) -> NativeAuditionActionSimulation:
    """Predict one exact next settled state without searching future policy."""

    blockers: list[HorizonBlocker] = []
    if not isinstance(action_id, str) or not action_id.strip():
        blockers.append(HorizonBlocker("native-action-id-invalid"))
    if isinstance(used_support_ids, (str, bytes)):
        used = ()
        blockers.append(HorizonBlocker("support-used-input-invalid"))
    else:
        try:
            used = tuple(used_support_ids)
        except TypeError as error:
            used = ()
            blockers.append(
                HorizonBlocker("support-used-input-invalid", str(error))
            )
    try:
        supports = (
            tuple(support_upgrades.values())
            if isinstance(support_upgrades, Mapping)
            else tuple(support_upgrades)
        )
    except TypeError as error:
        supports = ()
        blockers.append(
            HorizonBlocker("support-runtime-input-invalid", str(error))
        )
    blockers.extend(
        _input_blockers(
            state,
            zones,
            used,
            rules,
            schedule,
            catalog,
            item,
            limits,
            supports,
            support_card_searches,
            boundary_model,
        )
    )
    if blockers:
        return NativeAuditionActionSimulation(
            None, tuple(dict.fromkeys(blockers))
        )

    numerical = _SearchContext(
        rules,
        schedule,
        catalog,
        item,
        limits,
        supports,
        support_card_searches,
        boundary_model,
    )
    context = _NativeContext(numerical, supports, support_card_searches)
    root = _NativeNode(state, zones, used)
    selected_guid: str | None = None
    direct_draw = 0
    try:
        if action_id == "END_TURN":
            if hand_index is not None:
                raise _NativeBlocked(
                    "native-action-binding-mismatch",
                    "END_TURN requires hand_index=None",
                )
            prepared = _prepare_skip_transition(state, numerical)
            if prepared.item_result.unsupported_rules:
                raise _NativeBlocked(
                    "turn-skip-item-unsupported",
                    ",".join(prepared.item_result.unsupported_rules),
                )
            if (
                prepared.item_result.post_card_effects
                or prepared.item_result.fired_enchantment_ids
            ):
                raise _NativeBlocked("turn-skip-item-use-accounting-unverified")
            transition = prepared.transition
            if not transition.legal:
                raise _NativeBlocked(
                    "turn-skip-illegal",
                    ",".join(transition.unsupported_rules),
                )
        else:
            if (
                not isinstance(hand_index, int)
                or isinstance(hand_index, bool)
                or hand_index < 0
                or hand_index >= len(zones.hand)
            ):
                raise _NativeBlocked(
                    "native-action-hand-index-invalid", repr(hand_index)
                )
            instance = zones.hand[hand_index]
            ref = _ref(instance)
            if action_id != ref.key:
                raise _NativeBlocked(
                    "native-action-binding-mismatch",
                    f"requested={action_id};hand={ref.key};index={hand_index}",
                )
            selected_guid = instance.guid
            card = _catalog_card(instance, catalog)
            prepared = _prepare_card_transition(state, ref, numerical)
            if prepared.item_result.unsupported_rules:
                raise _NativeBlocked(
                    "equipped-item-transition-unsupported",
                    ",".join(prepared.item_result.unsupported_rules),
                )
            transition = prepared.transition
            if not transition.legal:
                raise _NativeBlocked("illegal-action", ref.key)
            item_draw_effects = prepared.item_result.post_card_effects
            if transition.turn_ended:
                item_draw_effects = (
                    *item_draw_effects,
                    *prepared.item_result.end_turn_effects,
                )
            direct_draw = _direct_draw_count(
                card, transition, item_draw_effects
            )

        advanced = _advance_action(
            root,
            transition,
            hand_index,
            direct_draw,
            context,
        )
        prediction = NativeAuditionActionPrediction(
            action_id=action_id,
            hand_index=hand_index,
            selected_guid=selected_guid,
            before_logic_state=state,
            before_zones=zones,
            before_used_support_ids=used,
            after_logic_state=advanced.node.logic_state,
            after_zones=advanced.node.zones,
            after_used_support_ids=advanced.node.used_support_ids,
            immediate_transition=advanced.transition,
            direct_draw_count=direct_draw,
            terminal=advanced.terminal,
            _factory_seal=_NATIVE_ACTION_PREDICTION_SEAL,
            boundary=advanced.boundary,
        )
    except (_NativeBlocked, _Blocked) as error:
        return NativeAuditionActionSimulation(None, (error.blocker,))
    except (IndexError, KeyError, TypeError, ValueError) as error:
        return NativeAuditionActionSimulation(
            None,
            (
                HorizonBlocker(
                    "native-action-simulation-invalid",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    return NativeAuditionActionSimulation(prediction)


def simulate_native_audition_horizon(
    state: LogicExamState,
    zones: NativeOrderedZoneState,
    used_support_ids: Iterable[str],
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    *,
    limits: HorizonSearchLimits = HorizonSearchLimits(),
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] = {},
    boundary_model: AuditionBoundaryModel | None = None,
) -> HorizonDecision:
    """Evaluate the deterministic horizon without granting action authority.

    ``decision_ready`` in this low-level result means only that the simulated
    action set and terminal search are complete.  It is not evidence that the
    supplied Python objects came from one current, settled live transaction.
    Live callers must use :func:`recommend_native_audition_horizon`, which is
    deliberately fail-closed until a bound authorization artifact exists.
    """

    try:
        used = tuple(used_support_ids)
    except TypeError as error:
        used = ()
        initial_blockers = [HorizonBlocker("support-used-input-invalid", str(error))]
    else:
        initial_blockers = []
    try:
        supports = (
            tuple(support_upgrades.values())
            if isinstance(support_upgrades, Mapping)
            else tuple(support_upgrades)
        )
    except TypeError as error:
        supports = ()
        initial_blockers.append(
            HorizonBlocker("support-runtime-input-invalid", str(error))
        )
    blockers = [
        *initial_blockers,
        *_input_blockers(
            state,
            zones,
            used,
            rules,
            schedule,
            catalog,
            item,
            limits,
            supports,
            support_card_searches,
            boundary_model,
        ),
    ]
    try:
        schedule_digest = schedule.digest()
    except (TypeError, ValueError):
        schedule_digest = ""
    if blockers:
        return HorizonDecision(
            decision_ready=False,
            recommendations=(),
            blockers=tuple(dict.fromkeys(blockers)),
            diagnostics=HorizonDiagnostics(
                schedule_digest=schedule_digest,
                nodes_expanded=0,
                memo_entries=0,
                chance_outcomes_evaluated=0,
                maximum_chance_fanout=0,
                full_terminal_horizon=False,
                draw_semantics=NATIVE_DETERMINISTIC_DRAW_SEMANTICS,
            ),
        )

    numerical = _SearchContext(
        rules,
        schedule,
        catalog,
        item,
        limits,
        supports,
        support_card_searches,
        boundary_model,
    )
    context = _NativeContext(numerical, supports, support_card_searches)
    root = _NativeNode(state, zones, used)
    evaluations: list[HorizonActionEvaluation] = []
    for hand_index, instance in enumerate(zones.hand):
        card = _catalog_card(instance, catalog)
        ref = _ref(instance)
        try:
            value, transition = _card_action(root, hand_index, context)
            evaluations.append(
                HorizonActionEvaluation(
                    action_id=ref.key,
                    hand_index=hand_index,
                    card=card,
                    value=value,
                    immediate_transition=transition,
                )
            )
        except (_NativeBlocked, _Blocked) as error:
            evaluations.append(
                HorizonActionEvaluation(
                    action_id=ref.key,
                    hand_index=hand_index,
                    card=card,
                    value=None,
                    immediate_transition=None,
                    blockers=(error.blocker,),
                )
            )
    try:
        value, transition = _skip_action(root, context)
        evaluations.append(
            HorizonActionEvaluation(
                action_id="END_TURN",
                hand_index=None,
                card=None,
                value=value,
                immediate_transition=transition,
            )
        )
    except (_NativeBlocked, _Blocked) as error:
        evaluations.append(
            HorizonActionEvaluation(
                action_id="END_TURN",
                hand_index=None,
                card=None,
                value=None,
                immediate_transition=None,
                blockers=(error.blocker,),
            )
        )

    evaluations.sort(
        key=lambda value: (
            value.fully_supported,
            value.value.sort_key if value.value is not None else (-1, -1, -1),
            value.action_id,
            -(value.hand_index if value.hand_index is not None else 10_000),
        ),
        reverse=True,
    )
    supported = [value for value in evaluations if value.fully_supported]
    incomplete_root = [
        blocker
        for value in evaluations
        for blocker in value.blockers
        if blocker.code not in {"illegal-action", "turn-skip-illegal"}
    ]
    if incomplete_root:
        blockers.append(
            HorizonBlocker("root-action-set-incomplete", _blocker_summary(incomplete_root))
        )
    if not supported:
        blockers.append(HorizonBlocker("no-supported-root-action"))
        blockers.extend(
            blocker for value in evaluations for blocker in value.blockers
        )
    if context.approximation_used:
        blockers.append(
            HorizonBlocker(
                "advisory-beam-approximation-used",
                f"action_pruned={context.action_branches_pruned}",
            )
        )
    full = bool(supported) and not incomplete_root and not context.approximation_used
    return HorizonDecision(
        decision_ready=full,
        recommendations=tuple(evaluations),
        blockers=tuple(dict.fromkeys(blockers)),
        diagnostics=HorizonDiagnostics(
            schedule_digest=schedule_digest,
            nodes_expanded=numerical.nodes_expanded,
            memo_entries=len(context.memo),
            chance_outcomes_evaluated=context.deterministic_draws,
            maximum_chance_fanout=context.maximum_draw_fanout,
            full_terminal_horizon=full,
            approximation_used=context.approximation_used,
            action_branches_pruned=context.action_branches_pruned,
            draw_semantics=NATIVE_DETERMINISTIC_DRAW_SEMANTICS,
        ),
    )


def recommend_native_audition_horizon(
    state: LogicExamState,
    zones: NativeOrderedZoneState,
    used_support_ids: Iterable[str],
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    *,
    limits: HorizonSearchLimits = HorizonSearchLimits(),
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] = {},
    boundary_model: AuditionBoundaryModel | None = None,
) -> HorizonDecision:
    """Return recommendations, but never authorize input without a certificate.

    The bound certificate must cover LocalSave schema-v5 evidence, numerical
    state, exact zones/RNG, schedule, rules, support/search inputs, and equipped
    items.  That issuer is intentionally not implemented in this pure module;
    therefore this public action-ready boundary always adds a blocker today.
    Call :func:`simulate_native_audition_horizon` for read-only analysis.
    """

    result = simulate_native_audition_horizon(
        state,
        zones,
        used_support_ids,
        rules,
        schedule,
        catalog,
        item,
        limits=limits,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
        boundary_model=boundary_model,
    )
    authorization_blocker = HorizonBlocker(
        "native-action-authorization-missing",
        "simulation inputs are not bound by a verified live eligibility certificate",
    )
    return replace(
        result,
        decision_ready=False,
        blockers=tuple(dict.fromkeys((*result.blockers, authorization_blocker))),
    )


__all__ = [
    "NATIVE_DETERMINISTIC_DRAW_SEMANTICS",
    "NativeAuditionActionPrediction",
    "NativeAuditionActionSimulation",
    "recommend_native_audition_horizon",
    "simulate_native_audition_action",
    "simulate_native_audition_horizon",
]
