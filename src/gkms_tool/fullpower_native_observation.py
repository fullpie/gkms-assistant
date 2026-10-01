"""Decode current FullPower model inputs without asserting a sim can execute.

Five current zones and the observed status graph are authoritative. Retired
card snapshots stay provenance. No raw ExamSave fields are filled or rewritten.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .audition_local_save_state import parse_local_save_exam_state
from .fullpower_decision_observation import FullPowerObservationUnavailable
from .plan3_local_save_bridge import project_plan3_local_save, _restore_effect_timer_listener
from .plan3_native_state import Plan3NativeCard, Plan3NativeState
from .plan3_search_stamina_change import (Plan3SearchStaminaRuntime, Plan3SearchStaminaStatus,
                                        resolve_plan3_search_stamina_change)
from .runtime_canonical_json import canonical_sha256


@dataclass(frozen=True)
class NativeCurrentObservation:
    parsed: object
    state: object
    native_state: Plan3NativeState
    captured_digest: str
    source_details: Mapping


def _fail(message):
    raise FullPowerObservationUnavailable(("native-current-observation:" + message,))


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        _fail("invalid-" + name)
    return value


def _active_references(raw):
    status = raw.get("status")
    references = raw.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        _fail("status-graph-missing")
    rows, active = references.get("RefIds"), status.get("_effectList")
    if not isinstance(rows, list) or not isinstance(active, list):
        _fail("status-reference-list-missing")
    by_rid = {}
    for row in rows:
        if not isinstance(row, Mapping) or row.get("rid") in by_rid:
            _fail("status-reference-identity-invalid")
        by_rid[row["rid"]] = row
    ids = [row.get("rid") for row in active if isinstance(row, Mapping)]
    if len(ids) != len(active) or len(ids) != len(set(ids)) or any(rid not in by_rid for rid in ids):
        _fail("active-status-binding-invalid")
    return [by_rid[rid] for rid in ids]


def validate_native_candidate_pool(observation, candidates):
    """Bind supplied candidates to this observation; never enumerate legality."""
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)) or not candidates:
        _fail("native-candidate-pool-missing")
    hand = observation.payload["cards"]["hand"]
    drinks = observation.payload["inventory"]["drink_ids"]
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or ("legal" in candidate and candidate["legal"] is not True):
            _fail("candidate-not-observed-legal")
        kind = candidate.get("kind", candidate.get("action_type"))
        kind = {"play": "use-hand", "card": "use-hand", "drink": "use-drink", "end_turn": "turn-end"}.get(kind, kind)
        if kind == "turn-end":
            key = (kind,)
        elif kind in {"use-hand", "use-drink"}:
            slot = candidate.get("slot_index", candidate.get("slot", candidate.get("hand_slot")))
            if type(slot) is not int or slot < 0:
                _fail("candidate-current-slot-missing")
            if any(key in candidate and (type(candidate[key]) is not int or candidate[key] != slot)
                   for key in ("slot_index", "slot", "hand_slot", "card_index")):
                _fail("candidate-slot-alias-mismatch")
            if kind == "use-hand":
                if slot >= len(hand):
                    _fail("candidate-slot-outside-hand")
                card = hand[slot]
                identities = [candidate[key] for key in ("sim_card_instance_id", "card_guid", "guid") if key in candidate]
                if (not identities or any(guid != card["guid"] for guid in identities)
                        or candidate.get("card_id", card["card_id"]) != card["card_id"]
                        or any(key in candidate and (type(candidate[key]) is not int
                            or candidate[key] != card["effective_upgrade"]) for key in ("effective_upgrade", "upgrade"))
                        or ("error" in candidate and (type(candidate["error"]) is not int or candidate["error"] != 0))):
                    _fail("candidate-current-card-identity-or-legality-mismatch")
                key = (kind, card["guid"])
            else:
                if slot >= len(drinks) or candidate.get("drink_id") != drinks[slot]:
                    _fail("candidate-current-drink-identity-mismatch")
                key = (kind, slot, drinks[slot])
        else:
            _fail("candidate-family-unavailable")
        if key in seen:
            _fail("candidate-identities-duplicated")
        seen.add(key)


def project_native_current_observation(raw, *, database: Path, master_dir: Path):
    """Return a model carrier; exact transition and input authority stay separate."""
    digest = canonical_sha256(raw)
    parsed = parse_local_save_exam_state(raw)
    if not parsed.is_native_actionable_settled:
        _fail("native-main-boundary-unavailable")
    zones = {name: tuple(getattr(parsed.zones, name)) for name in ("hand", "deck", "grave", "lost", "hold")}
    current = {card.guid: (zone, card) for zone, cards in zones.items() for card in cards}
    if len(current) != sum(len(cards) for cards in zones.values()):
        _fail("current-card-guid-duplicated")
    # The shared parser proves every removed snapshot has the same GUID/card ID
    # in exactly one current zone; upgrade/play-count history may differ.
    tombstones = [{"snapshot": card.to_dict(), "current_zone": current[card.guid][0],
                   "current_upgrade": current[card.guid][1].effective_upgrade} for card in parsed.removed_cards]
    base = project_plan3_local_save(parsed, database=database, master_dir=master_dir, native_card_play_counts=True)
    state = base.state
    active = _active_references(raw)
    handled, observations, restored_timers, search_statuses = set(), [], [], []
    fullpower_values = []
    for ref in active:
        rid, data = ref["rid"], ref.get("data")
        name = (ref.get("type") or {}).get("class")
        if not isinstance(data, Mapping):
            _fail("active-status-data-missing")
        field = f"root_runtime.references[{rid}]"
        if name == "FullPowerPointStatusEffect":
            if (set(data) != {"_uid", "_value", "_isPassingTurnStart", "_isTurnLimited", "_turn"}
                    or type(data.get("_isPassingTurnStart")) is not bool
                    or data.get("_isTurnLimited") is not False or data.get("_turn") != -1):
                _fail("fullpower-current-status-shape-unavailable")
            _integer(data.get("_uid"), "fullpower-uid", 1)
            fullpower_values.append(_integer(data.get("_value"), "fullpower-value"))
            # PassingTurnStart controls lifecycle; it does not erase the
            # explicitly observed current value of this unlimited status.
            handled.add(("active-status-lifecycle-unsupported", field))
            observations.append({"field": field, "kind": "current-fullpower-value", "native_data": dict(data)})
        elif name == "SearchPlayCardStaminaConsumptionChangeStatusEffect":
            required = {"_uid", "_value", "_count", "_fromExamEffectId", "_searchId",
                        "_isPassingTurnStart", "_isTurnLimited", "_turn"}
            if (set(data) != required or type(data.get("_isPassingTurnStart")) is not bool
                    or data.get("_isTurnLimited") is not False or data.get("_turn") != -1):
                _fail("search-stamina-current-status-shape-unavailable")
            resolution = resolve_plan3_search_stamina_change(data["_fromExamEffectId"], database)
            contract = resolution.contract
            if (contract is None or data["_value"] != contract.stamina_cost
                    or data["_searchId"] != contract.target.search_id):
                _fail("search-stamina-current-source-unavailable")
            search_statuses.append(Plan3SearchStaminaStatus(
                status_uid=_integer(data["_uid"], "search-stamina-uid", 1),
                stamina_cost=_integer(data["_value"], "search-stamina-value"),
                remaining_uses=_integer(data["_count"], "search-stamina-count", 1),
                duration_turns=contract.duration_turns, target=contract.target, from_effect_id=contract.effect_id))
            handled.add(("active-status-class-unsupported", field))
            observations.append({"field": field, "kind": "current-search-stamina", "native_data": dict(data)})
        elif name == "TriggerEffectStatusEffect" and data.get("_overrideType") == 30:
            source = data.get("_triggerCard") or {}
            source_card = source.get("_cardData") or {}
            positioned = current.get(source.get("_guid"))
            # Restore the timer's immutable captured graph, not the live card's
            # possibly expired temporary upgrade. Same GUID/card ID is required.
            if positioned and (positioned[1].card_id, positioned[1].effective_upgrade) != (
                    source_card.get("_id"), source_card.get("_upgradeCount")):
                if positioned[1].card_id != source_card.get("_id"):
                    _fail("timer-source-card-id-mismatch")
                issues = []
                timer = _restore_effect_timer_listener(data, rid=rid, database=database, issues=issues, source_cards=None)
                if timer is None or issues:
                    _fail("captured-timer-graph-unavailable")
                restored_timers.append(timer)
                handled.add(("effect-timer-source-card-mismatch", field + "._triggerCard"))
                observations.append({"field": field, "kind": "captured-timer-source", "native_data": dict(data),
                    "current_zone": positioned[0], "current_upgrade": positioned[1].effective_upgrade})
    if len(fullpower_values) > 1:
        _fail("fullpower-current-status-duplicated")
    if fullpower_values:
        state = replace(state, full_power_points=fullpower_values[0])
    if restored_timers:
        order = {ref["data"].get("_uid"): index for index, ref in enumerate(active)}
        enchants = (*state.active_status_enchants, *restored_timers)
        if any(enchant.native_uid not in order for enchant in enchants):
            _fail("timer-current-order-unavailable")
        state = replace(state, active_status_enchants=tuple(sorted(enchants, key=lambda e: order[e.native_uid])))
    unresolved = [issue for issue in base.issues if issue.code != "not-actionable-settled"
                  and (issue.code, issue.field) not in handled]
    if unresolved:
        raise FullPowerObservationUnavailable(tuple(
            f"native-current-observation:{issue.code}:{issue.field}:{issue.detail}" for issue in unresolved))
    # Construct only the observed current carrier; the strict simulator factory
    # still rejects unsupported removed/history/transition states unchanged.
    native = Plan3NativeState(**{zone: tuple(Plan3NativeCard.from_local_save(card,
        database=database, master_dir=master_dir) for card in cards) for zone, cards in zones.items()},
        random_state=parsed.random_state, turn_used_support_ids=parsed.turn_use_support_ids,
        total_effect_draw_card_count=_integer(raw.get("totalDrawCardCount"), "total-draw-count"),
        anti_debuff_runtime=state.anti_debuff_runtime, enthusiastic_runtime=state.enthusiastic_runtime,
        search_stamina_runtime=(Plan3SearchStaminaRuntime(tuple(search_statuses),
            _integer(raw["status"].get("_effectCreateCount"), "status-create-count") + 1)
            if search_statuses else Plan3SearchStaminaRuntime()))
    native.assert_plan3_projection(state)
    if canonical_sha256(raw) != digest:
        _fail("native-source-mutated")
    return NativeCurrentObservation(parsed, state, native, digest, {
        "contract": "current-model-observation", "transition_executable": False,
        "current_zone_source": "ordered handList/deckList/graveList/lostList/holdList",
        "status_source": "same-snapshot status._effectList and references.RefIds",
        "retired_card_snapshots": tombstones, "explicit_status_decodes": observations,
        "separate_simulator_issues": [{"code": i.code, "field": i.field, "detail": i.detail} for i in base.issues]})
