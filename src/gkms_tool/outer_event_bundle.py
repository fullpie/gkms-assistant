"""Pure whole-option estimates for cultivation events.

One optional ranking model; never a native legality source or game-score
forecast. Costs are valued once, all known card effects share the adopted deck
plan, and unrevealed reward distributions remain explicitly unknown.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
import math

from .deck_value import evaluate_deck_delta, normalize_deck
from .outer_resource_value import effective_stamina_cost, recovery_value
from .outer_pt_value import produce_point_reward_value

SCHEMA = "gkms.outer-event-bundle-value.v1"
_AXES = {"ProduceEffectType_VocalAddition": "vocal", "ProduceEffectType_DanceAddition": "dance",
         "ProduceEffectType_VisualAddition": "visual"}


@dataclass(frozen=True)
class EventBundleValue:
    value: float
    upper_value: float
    unscored_effects: tuple[str, ...]
    components: tuple[Mapping, ...]
    plan_progress: Mapping | None = None
    future_choice: EventBundleValue | None = None
    modeled_point_balance_min: float | None = None
    modeled_point_balance_max: float | None = None
    modeled_stamina_min: float | None = None
    reward_pick_min: Mapping[str, int] | None = None

    @property
    def ranking_key(self):
        progress = self.plan_progress or {}
        delta = (float(progress["value"]) if self.value > 0 and not self.unscored_effects
                 and progress.get("status") == "compared" else 0.)
        return self.value > 0, delta, self.value

    @property
    def future_choice_ranking_key(self):
        return self.future_choice.ranking_key if self.future_choice is not None else self.ranking_key

    def to_dict(self):
        return {"schema": SCHEMA, "value": self.value, "known_outcome_upper_value": self.upper_value,
                "unscored_effects": list(self.unscored_effects), "components": list(self.components),
                "plan_progress": dict(self.plan_progress) if self.plan_progress else None,
                "ranking_key": list(self.ranking_key),
                "modeled_point_balance_min": self.modeled_point_balance_min,
                "modeled_point_balance_max": self.modeled_point_balance_max,
                "modeled_stamina_min": self.modeled_stamina_min,
                "reward_pick_min": dict(self.reward_pick_min or {}),
                "future_choice_assessment": self.future_choice.to_dict() if self.future_choice is not None else None,
                "value_kind": "event-ranking-utility", "predicts_exam_score": False,
                "unknown_rewards_included_in_bounds": False, "native_state_modified": False}


def _progress_bound(progresses, *, upper=False):
    if (not progresses or any(not p or p.get("status") != "compared" or not p.get("plan_id") for p in progresses)
            or len({p["plan_id"] for p in progresses}) != 1):
        return None
    values = [float(p.get("known_outcome_upper_value", p["value"]) if upper else p["value"]) for p in progresses]
    return max(values) if upper else min(values)


def combine_exclusive_branches(branches, probabilities=None):
    """Aggregate mutually exclusive modeled outcomes, without inventing odds."""
    if not branches:
        raise ValueError("event branch set is empty")
    if probabilities is not None:
        if (len(probabilities) != len(branches) or any(type(p) not in (int, float) or not math.isfinite(p)
                or not 0 <= p <= 1 for p in probabilities) or not math.isclose(sum(probabilities), 1.)):
            raise ValueError("event branch probabilities must sum to one")
        pairs = [(p, b) for p, b in zip(probabilities, branches) if p > 0]
        aggregate = lambda values: sum(p * value for (p, _), value in zip(pairs, values))
        upper_aggregate = aggregate
    else:
        pairs = [(None, b) for b in branches]
        aggregate, upper_aggregate = min, max
    used = [b for _, b in pairs]
    progresses = [b.plan_progress for b in used]
    progress = None
    if _progress_bound(progresses) is not None:
        progress = {"status": "compared", "plan_id": progresses[0]["plan_id"],
            "value": aggregate([p["value"] for p in progresses]),
            "known_outcome_upper_value": upper_aggregate([p.get("known_outcome_upper_value", p["value"]) for p in progresses]),
            "predicts_exam_score": False, "unknown_rewards_included_in_bounds": False}
    return EventBundleValue(aggregate([b.value for b in used]), upper_aggregate([b.upper_value for b in used]),
        tuple(sorted({gap for b in used for gap in b.unscored_effects})),
        ({"aggregation": "observed-probabilities" if probabilities is not None else "bounds-no-assumed-probability",
          "branches": [{"probability": p, "assessment": b.to_dict()} for p, b in pairs]},), progress,
        modeled_point_balance_min=(min(b.modeled_point_balance_min for b in used)
            if all(b.modeled_point_balance_min is not None for b in used) else None),
        modeled_point_balance_max=(max(b.modeled_point_balance_max for b in used)
            if all(b.modeled_point_balance_max is not None for b in used) else None),
        modeled_stamina_min=(min(b.modeled_stamina_min for b in used)
            if all(b.modeled_stamina_min is not None for b in used) else None),
        reward_pick_min={kind: min((b.reward_pick_min or {}).get(kind, 0) for b in used)
                         for kind in {kind for b in used for kind in (b.reward_pick_min or {})}})


def _card_pool(connection, effect, deck):
    row = connection.execute("SELECT raw_json FROM produce_card_search WHERE id=?", (effect.get("produceCardSearchId"),)).fetchone()
    if row is None:
        raise ValueError("missing-search")
    search = json.loads(row[0])
    neutral = {"cardRarities": [], "produceCardIds": [], "upgradeCounts": [],
        "planType": "ProducePlanType_Unknown", "cardCategories": [],
        "cardStatusType": "ProduceCardSearchStatusType_Unknown", "orderType": "ProduceCardOrderType_Unknown",
        "cardPositionType": "ProduceCardPositionType_DeckAll", "produceCardRandomPoolId": "", "limitCount": 0,
        "staminaMinMaxType": "ConditionMinMaxType_Unknown", "staminaMin": 0, "staminaMax": 0,
        "examEffectType": "ProduceExamEffectType_Unknown", "effectGroupIds": [], "isSelf": False,
        "produceCardPoolId": "", "costType": "ExamCostType_Unknown", "isCustomized": False}
    if any(search.get(key) != value for key, value in neutral.items()):
        raise ValueError("unmodeled-search-predicate")
    tag = search.get("cardSearchTag")
    if tag not in ("", "starter"):
        raise ValueError("unmodeled-search-tag")
    pool = []
    upgrade = effect["produceEffectType"] == "ProduceEffectType_ProduceCardUpgrade"
    for index, card in enumerate(deck):
        row = connection.execute("SELECT raw_json FROM card WHERE id=? AND upgrade_count=?",
                                 (card["card_id"], card["upgrade"])).fetchone()
        if row is None:
            raise ValueError("missing-current-card")
        raw = json.loads(row[0])
        if tag and raw.get("searchTag") != tag:
            continue
        # Ordinary permanent +1. Temporary/in-Exam upgrades are not an Outer
        # eligibility signal, and special upgrades need their own contract.
        if upgrade and (card["upgrade"] != 0 or connection.execute(
                "SELECT 1 FROM card WHERE id=? AND upgrade_count=1", (card["card_id"],)).fetchone() is None):
            continue
        pool.append(index)
    return pool


def evaluate_event_bundle(connection, choice, context, *, database, card_value=None,
                          prefix_effect_ids=(), entry_point_cost=0, entry_stamina_cost=0):
    """Compare actual effect bundles in common heuristic utility units.

    Resource units are deliberately explicit: 0.1/attribute, 0.3/effective HP,
    0.001/vote; deck utility and PT opportunity cost share deck_value. They are
    design weights, not learned values or calibrated score conversions.
    Random card picks use min/max across reachable alternatives, never a made-up
    uniform distribution. Hidden reward pools add a gap, not a fabricated card.
    """
    if (type(entry_point_cost) is not int or entry_point_cost < 0
            or type(entry_stamina_cost) is not int or entry_stamina_cost < 0):
        raise ValueError("entry costs must be actual nonnegative integers")
    if entry_point_cost > context["produce_points"] or entry_stamina_cost > context["stamina"]:
        return None
    before = normalize_deck(context["deck"])
    initial = deepcopy(dict(context))
    initial["deck"] = list(before)
    initial["stamina"] -= entry_stamina_cost
    initial["produce_points"] -= entry_point_cost
    variants, gaps, details = [initial], set(), []
    callback = card_value or (lambda old, new, ctx: evaluate_deck_delta(old, new, ctx, database=database))

    def deck_delta(old, new, state):
        result = callback(old, new, {**state, "produce_point_cost": 0, "stamina_cost": 0})
        return result, float(result.value if hasattr(result, "value") else result)

    def effects(ids, current):
        for identity in ids:
            record = connection.execute("SELECT raw_json FROM produce_effect WHERE id=?", (identity,)).fetchone()
            if record is None:
                raise ValueError("Master event effect is missing: " + identity)
            effect = json.loads(record[0]); kind = effect["produceEffectType"]
            low, high = effect.get("effectValueMin", 0), effect.get("effectValueMax", 0)
            if type(low) is not int or type(high) is not int or high < low:
                raise ValueError("invalid Master effect value range")
            if kind in _AXES or kind in {"ProduceEffectType_StaminaRecoverFix", "ProduceEffectType_StaminaRecoverMultiple",
                    "ProduceEffectType_ProducePointAddition", "ProduceEffectType_ProducePointAdditionDisableTrigger",
                    "ProduceEffectType_VoteCountAddition"}:
                updated = []
                for state in current:
                    for amount in sorted({low, high}):
                        after = deepcopy(state)
                        if kind in _AXES:
                            axis = _AXES[kind]
                            gain = amount * (1000 + state["growth"][axis]) // 1000
                            after[axis] = min(state["attribute_cap"], state[axis] + gain)
                        elif kind.startswith("ProduceEffectType_StaminaRecover"):
                            if kind.endswith("Multiple"):
                                # Ranking baseline, supported by four actual
                                # NIA36-HP/200-permil observations (+7). Other
                                # values extrapolate this baseline, not a claim
                                # about unobserved server modifiers/rounding.
                                amount = state["max_stamina"] * amount // 1000
                            after["stamina"] = min(state["max_stamina"], state["stamina"] + amount)
                        elif kind.startswith("ProduceEffectType_ProducePointAddition"):
                            after["produce_points"] += amount
                        else:
                            after["vote_count"] = state.get("vote_count", 0) + amount
                        updated.append(after)
                current = updated
                details.append({"effect_id": identity, "modeled": True, "kind": kind,
                    "value_range": [low, high], "evaluation_only": True,
                    **({"formula": "floor(max_stamina * permil / 1000)", "calibration": "NIA maxHP36/200 -> +7",
                        "server_formula_verified": False} if kind.endswith("StaminaRecoverMultiple") else {})})
            elif kind in {"ProduceEffectType_ProduceCardUpgrade", "ProduceEffectType_ProduceCardDelete"}:
                if effect.get("pickCountMin") != 1 or effect.get("pickCountMax") != 1 or (
                        kind.endswith("Upgrade") and (low != 1 or high != 1)):
                    gaps.add("unmodeled-card-operation-count:" + identity); continue
                pick = effect.get("pickRangeType")
                if pick not in {"ProducePickRangeType_Select", "ProducePickRangeType_Random"}:
                    gaps.add("unmodeled-card-pick:" + identity); continue
                expanded = []
                for state in current:
                    try:
                        pool = _card_pool(connection, effect, state["deck"])
                    except ValueError as error:
                        gaps.add(f"{identity}:{error}"); expanded.append(state); continue
                    options = []
                    for index in pool:
                        after = deepcopy(state)
                        if kind.endswith("Upgrade"):
                            after["deck"][index]["upgrade"] = after["deck"][index]["upgradeCount"] = 1
                        elif len(after["deck"]) > 1:
                            after["deck"].pop(index)
                        else:
                            continue
                        delta, utility = deck_delta(state["deck"], after["deck"], state)
                        gaps.update(getattr(delta, "unscored_effects", ()))
                        key = getattr(delta, "ranking_key", (utility > 0, 0., utility))
                        options.append((key, -index, after))
                    if not options:
                        expanded.append(state)
                    elif pick.endswith("Select"):
                        expanded.append(max(options, key=lambda row: row[:2])[2])
                    else:
                        expanded.extend(option[2] for option in options)
                    details.append({"effect_id": identity, "kind": kind, "eligible_count": len(pool),
                        "pick": pick, "method": "same-plan-best-known-mutation" if pick.endswith("Select") else "all-outcomes-bounds-no-assumed-probability"})
                if len(expanded) > 256:
                    gaps.add("event-branch-budget:" + identity)
                else:
                    current = expanded
            else:
                gaps.add(("unrevealed-reward:" if kind == "ProduceEffectType_ProduceRewardSet" else "unmodeled-effect:") + identity)
                if kind == "ProduceEffectType_ProduceRewardSet":
                    resource, count = effect.get("produceResourceType"), effect.get("pickCountMin")
                    if (resource in {"ProduceResourceType_ProduceDrink", "ProduceResourceType_ProduceCard"}
                            and type(count) is int and count > 0):
                        for state in current:
                            counts = state.setdefault("__gkms_reward_pick_counts", {})
                            counts[resource] = counts.get(resource, 0) + count
                        details.append({"effect_id": identity, "kind": kind, "resource_type": resource,
                            "minimum_pick_count": count, "modeled": False, "reward_quality_unknown": True})
        return current

    # Business entry is paid before its common rewards. Its later event option
    # pays its own price only after those rewards; this may change affordability
    # and HP capping. No common effect is independently credited a second time.
    variants = effects(prefix_effect_ids, variants)
    if any(choice.produce_point_cost > state["produce_points"] for state in variants):
        return None
    choice_origins = deepcopy(variants)
    for index, state in enumerate(variants):
        state["__gkms_event_choice_origin"] = index
        state["stamina"] = max(0, state["stamina"] - choice.stamina_cost)
        state["produce_points"] -= choice.produce_point_cost
    variants = effects(choice.effect_ids, variants)
    if choice.direct_card_id:
        for state in variants:
            state["deck"].append({"card_id": choice.direct_card_id, "upgrade": choice.direct_card_upgrade,
                                  "instance_id": "proposed:event-direct-card"})
    # Common effects precede success/failure branch evaluation, so a second
    # recovery cannot receive credit for HP already restored by the first.
    probability = 1. if choice.always_successful else choice.success_probability_permyriad / 10000.
    branch_sets = []
    if probability > 0:
        branch_sets.append((probability, effects(choice.success_effect_ids, deepcopy(variants))))
    if probability < 1:
        branch_sets.append((1 - probability, effects(choice.fail_effect_ids, deepcopy(variants))))
    total_point_cost = entry_point_cost + choice.produce_point_cost
    cost = evaluate_deck_delta(before, before, {**context, "produce_point_cost": total_point_cost,
                                               "stamina_cost": 0}, database=database).value
    def summarize(origin_for_state, point_cost, components):
        totals, upper_totals, progress_totals, progress_upper_totals = [], [], [], []
        plan_id, complete_progress, local_gaps, inputs = None, True, set(gaps), {}
        for probability, states in branch_sets:
            values, progresses = [], []
            for state in states:
                origin = origin_for_state(state)
                if id(origin) not in inputs:
                    original_deck = normalize_deck(origin["deck"])
                    fee = evaluate_deck_delta(original_deck, original_deck, {**origin,
                        "produce_point_cost": point_cost, "stamina_cost": 0}, database=database).value
                    inputs[id(origin)] = original_deck, fee
                original_deck, fee = inputs[id(origin)]
                delta, deck_utility = deck_delta(original_deck, state["deck"], origin)
                local_gaps.update(getattr(delta, "unscored_effects", ()))
                progresses.append(getattr(delta, "plan_progress", None))
                hp_difference = state["stamina"] - origin["stamina"]
                hp_utility = .3 * (recovery_value(origin, hp_difference) if hp_difference >= 0
                                   else -effective_stamina_cost(origin, -hp_difference))
                attributes = sum(.1 * (state.get(axis, 0) - origin.get(axis, 0)) for axis in ("vocal", "dance", "visual"))
                gained_pt = state["produce_points"] - (origin["produce_points"] - point_cost)
                reserve = origin.get("produce_point_reserve")
                gained_pt_value = produce_point_reward_value(gained_pt, origin["produce_points"],
                    40 if reserve is None else reserve, origin.get("produce_point_spending_context"))
                votes = .001 * (state.get("vote_count", 0) - origin.get("vote_count", 0))
                values.append(deck_utility + hp_utility + attributes + gained_pt_value + votes + fee)
            totals.append(probability * min(values)); upper_totals.append(probability * max(values))
            low, high = _progress_bound(progresses), _progress_bound(progresses, upper=True)
            if low is None or (plan_id is not None and plan_id != progresses[0]["plan_id"]):
                complete_progress = False
            else:
                plan_id = progresses[0]["plan_id"]
                progress_totals.append(probability * low); progress_upper_totals.append(probability * high)
        progress = ({"status": "compared", "plan_id": plan_id, "value": sum(progress_totals),
            "known_outcome_upper_value": sum(progress_upper_totals), "predicts_exam_score": False,
            "unknown_rewards_included_in_bounds": False,
            "aggregation": "known-probability-weighted-outcome-bounds"} if complete_progress and plan_id else None)
        # Only reachable modeled outcomes enter the funding comparison. An
        # expected balance could hide the branch that cannot fund instruction.
        balances = [state["produce_points"] for probability, states in branch_sets
                    if probability > 0 for state in states]
        outcomes = [state for probability, states in branch_sets if probability > 0 for state in states]
        resources = {kind for state in outcomes for kind in state.get("__gkms_reward_pick_counts", {})}
        rewards = {kind: min(state.get("__gkms_reward_pick_counts", {}).get(kind, 0)
            - origin_for_state(state).get("__gkms_reward_pick_counts", {}).get(kind, 0) for state in outcomes)
            for kind in resources}
        return EventBundleValue(sum(totals), sum(upper_totals), tuple(sorted(local_gaps)), tuple(components), progress,
            modeled_point_balance_min=min(balances), modeled_point_balance_max=max(balances),
            modeled_stamina_min=min(state["stamina"] for state in outcomes), reward_pick_min=rewards)
    details.append({"produce_point_cost": total_point_cost, "pt_opportunity_utility": cost,
                    "stamina_cost": entry_stamina_cost + choice.stamina_cost, "costs_charged_once": True,
                    "entry_point_cost": entry_point_cost, "choice_point_cost": choice.produce_point_cost,
                    "resource_weights": {"attribute": .1, "effective_stamina": .3, "vote": .001}})
    whole = summarize(lambda state: context, total_point_cost, details)
    if not (prefix_effect_ids or entry_point_cost or entry_stamina_cost):
        return whole
    # At the later selector the entry is already paid and common rewards are
    # already owned. Predict that policy from its actual decision boundary,
    # while the business comparison still prices the entire selected bundle.
    future = summarize(lambda state: choice_origins[state["__gkms_event_choice_origin"]], choice.produce_point_cost,
        ({"comparison_origin": "after-entry-and-common-effects", "entry_cost_is_sunk": True},))
    return EventBundleValue(whole.value, whole.upper_value, whole.unscored_effects, whole.components, whole.plan_progress, future,
        whole.modeled_point_balance_min, whole.modeled_point_balance_max, whole.modeled_stamina_min, whole.reward_pick_min)
