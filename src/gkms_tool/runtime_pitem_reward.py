"""Pure P-item reward ranking from existing Master/route/item knowledge.

Opportunity counts are an explicit ranking heuristic, not promised activations
or a predicted exam score. Passive items never enter the skill-card draw pool.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .deck_value import _fingerprint, _master, normalize_deck
from .master_db import DEFAULT_DATABASE
from .nia_outer_exam_save_adapter import _master_rows
from .passive_catalog import DEFAULT_MASTER_DIR
from .route_calendar import load_route_calendar, SPECIAL_GUIDANCE
from .produce_effect_prior import rank_produce_effect_categories


def rank_native_pitem_reward(item_id, deck, owned_items, context, *, database=DEFAULT_DATABASE):
    database = Path(database)
    cards = normalize_deck(deck)
    with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM produce_item WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise ValueError(f"P-item Master row missing: {item_id}")
        raw = json.loads(row["raw_json"])
        effects, unscored = [], []
        for identity in json.loads(row["produce_item_effect_ids_json"]):
            link = connection.execute("SELECT effect_type,produce_effect_id,produce_exam_status_enchant_id FROM produce_item_effect WHERE id=?", (identity,)).fetchone()
            if link is None:
                raise ValueError(f"P-item Master effect link missing: {identity}")
            if link["effect_type"] != "ProduceItemEffectType_ProduceEffect":
                unscored.append(f"P-item passive not evaluated by outer resource proxy:{identity}")
                continue
            effect = connection.execute("SELECT effect_type,effect_value_min,effect_value_max FROM produce_effect WHERE id=?", (link["produce_effect_id"],)).fetchone()
            if effect is None:
                raise ValueError(f"P-item produce effect missing: {link['produce_effect_id']}")
            effects.append(dict(effect))
    name = str(raw.get("name", item_id))
    coarse, coarse_reason = rank_produce_effect_categories(effect["effect_type"] for effect in effects) or (100, "unscored-effect")
    directory = Path(context.get("master_dir", DEFAULT_MASTER_DIR))
    triggers = {value["id"]: value for value in _master_rows(directory / "ProduceTrigger.yaml")}
    trigger_id = row["produce_trigger_id"]
    trigger = triggers.get(trigger_id, {})
    phase = trigger.get("phaseType")
    week = int(context["week"])
    calendar = load_route_calendar(str(context["produce_id"]), master_dir=directory)
    future = [value for value in calendar.weeks if value.week > week]
    next_exam = context.get("next_exam", {})
    next_week = week + int(next_exam.get("weeks_until", context.get("weeks_remaining", 0)))
    def horizon_weight(route_week):
        return 1.0 if route_week.week <= next_week else .5
    used = sum(int(value.get("fireCount", 0)) for value in owned_items if value.get("produceItemId") == item_id)
    limit = max(0, int(row["fire_limit"]) - used)
    notes = ["existing Master item-effect category prior: " + coarse_reason,
             f"actual deck remains {len(cards)} cards; P-item is passive and adds no draw entry",
             "remaining route opportunities are heuristic, with later-than-next-exam benefits discounted"]
    already_owned = any(value.get("produceItemId") == item_id for value in owned_items)
    if already_owned:
        unscored.append("duplicate owned P-item acquisition semantics unavailable; no new independent fire budget assumed")
        limit = 0
    affinity = None
    if phase == "ProducePhaseType_StartCustomize":
        opportunities = sum(horizon_weight(value) * (1.0 if value.actions == (SPECIAL_GUIDANCE,) else .5)
                            for value in future if SPECIAL_GUIDANCE in value.actions)
        notes.append("Master trigger: entering special guidance; remaining mandatory/optional route opportunities")
    elif phase == "ProducePhaseType_GetProduceCard":
        # Master's structured description declares the required effect family;
        # no localized text or digits in the item ID are parsed as mechanics.
        required = {value.get("examEffectType") for value in raw.get("produceDescriptions", ())
                    if value.get("produceDescriptionType") == "ProduceDescriptionType_ProduceExamEffectType"
                    and value.get("examEffectType") not in (None, "ProduceExamEffectType_Unknown")}
        def effect_types(card):
            master = _master(card["card_id"], card["upgrade"], database, _fingerprint(database))
            result = set()
            def visit(effect, depth=0):
                result.add(effect.effect_type)
                if effect.status_enchant_rule is not None and depth < 6:
                    for child in effect.status_enchant_rule.effects:
                        visit(child, depth + 1)
            for effect in master.effects:
                visit(effect)
            return result
        if required:
            matched = sum(bool(effect_types(card) & required) for card in cards)
            affinity = matched / max(1, len(cards))
            # Actual deck direction is an observed preference proxy, not proof
            # that an unrevealed future offer will satisfy the item predicate.
            potential = {"self_lesson", "lesson", "cram_lesson", "care_package", "supply", "consultation"}
            opportunities = affinity * sum(horizon_weight(value) * (1.0 if len(value.actions) == 1 else .5)
                                             for value in future if potential.intersection(value.actions))
            notes.append(f"structured effect-family affinity in actual deck: {matched}/{len(cards)}; {','.join(sorted(required))}")
            unscored.append("future acquired-card eligibility and exact trigger predicate are not fully forecast; deck affinity is a proxy")
        else:
            opportunities = 0.0
            unscored.append("GetProduceCard trigger has no structured effect-family evidence")
    else:
        opportunities = 0.0
        unscored.append(f"outer P-item trigger opportunity not modeled:{phase or trigger_id}")
    fires = 0.0 if already_owned else min(limit, opportunities) if int(row["fire_limit"]) > 0 else opportunities
    resource_value = 0.0
    for effect in effects:
        kind = effect["effect_type"]
        amount = float(effect["effect_value_min"])
        if effect["effect_value_min"] != effect["effect_value_max"]:
            unscored.append(f"random resource magnitude:{kind}; lower bound used")
        # Same outer resource utility scales already used by runtime_outer_policy.
        if kind == "ProduceEffectType_VoteCountAddition":
            resource_value += amount * .1
        elif kind == "ProduceEffectType_StaminaRecoverFix":
            missing = max(0, int(context["max_stamina"]) - int(context["stamina"]))
            resource_value += min(amount, missing) * 30
            notes.append("future recovery valued against current observed stamina deficit, not assumed future damage")
        elif kind in {"ProduceEffectType_ProducePointAddition", "ProduceEffectType_ProducePointAdditionDisableTrigger"}:
            resource_value += amount * 5
        else:
            unscored.append(f"outer P-item effect utility not modeled:{kind}")
    score = coarse * .01 + resource_value * fires
    return score, {"value_method": "existing-item-prior+native-deck-route-opportunity-v1",
        "ranking_score": score, "item_id": item_id, "item_name": name,
        "trigger_phase": phase, "remaining_fire_limit": limit,
        "activation_opportunity_proxy": fires, "deck_effect_affinity": affinity,
        "value_reasons": notes, "unscored_effects": unscored,
        "scope": "ranking utility only; no predicted exam score, guaranteed activation count, or leaderboard prior claimed"}
