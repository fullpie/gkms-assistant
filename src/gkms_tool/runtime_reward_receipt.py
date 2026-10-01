"""Recognize a completed drink award independently of its later presentation.

Native ActivateManualEffectRequestListAsync awaits ActivateEffect, updates
progress, then plays the effect. A blocked/animated parent is not proof that
the preceding reward request is still outstanding. No input is owned here.
"""
from collections import Counter
from collections.abc import Mapping


def drink_reward_receipt(before, after, target):
    if target.get("action_id") != "reward.receive" or after.get("surface") == "error":
        return None
    old_state, new_state = before.get("state"), after.get("state")
    old_progress, new_progress = before.get("progress"), after.get("progress")
    if not all(isinstance(x, Mapping) for x in (old_state, new_state, old_progress, new_progress)):
        return None
    if old_state.get("in_progress") is not True or new_state.get("in_progress") is not True:
        return None
    for key in ("produce_id", "week", "step_type"):
        if key not in old_state or new_state.get(key) != old_state[key]:
            return None
    if not old_progress.get("idolCardId") or new_progress.get("idolCardId") != old_progress["idolCardId"]:
        return None
    ui = before.get("ui_state") or {}
    candidates = ui.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    selected = [r for r in candidates if isinstance(r, Mapping) and r.get("selected") is True]
    if len(selected) != 1:
        return None
    chosen = selected[0]
    if (type(target.get("index")) is not int or type(chosen.get("index")) is not int
            or not isinstance(chosen.get("instance_key"), str) or not chosen["instance_key"]):
        return None
    if any(chosen.get(k) != target.get(k) for k in ("index", "instance_key", "resource_id")):
        return None
    if ui.get("selected_index") != chosen.get("index"):
        return None
    pool = []
    for r in candidates:
        if (not isinstance(r, Mapping) or type(r.get("resource_type")) is not int or r["resource_type"] != 3
                or not isinstance(r.get("resource_id"), str) or not r["resource_id"]
                or type(r.get("quantity")) is not int or r["quantity"] <= 0):
            return None
        pool.append((r["resource_id"], r["quantity"]))
    old_effects = (before.get("collections") or {}).get("effects")
    new_effects = (after.get("collections") or {}).get("effects")
    if not isinstance(old_effects, list) or not isinstance(new_effects, list):
        return None
    matches = []
    for effect in old_effects:
        if not isinstance(effect, Mapping) or effect.get("type") != "ProduceEffectType_ProduceRewardSet":
            continue
        rewards = effect.get("rewards")
        if (not isinstance(rewards, list) or len(rewards) != len(pool)
                or any(not isinstance(r, Mapping) or r.get("resourceType") != "ProduceResourceType_ProduceDrink" for r in rewards)):
            continue
        if [(r.get("resourceId"), r.get("quantity")) for r in rewards] == pool:
            matches.append(effect)
    if len(matches) != 1:
        return None
    effect = matches[0]
    identity = (effect.get("number"), effect.get("originOwnerId"), effect.get("produceEffectId"))
    if type(identity[0]) is not int or identity[0] <= 0 or not all(isinstance(v, str) and v for v in identity[1:]):
        return None
    if any(isinstance(e, Mapping) and (e.get("number"), e.get("originOwnerId"), e.get("produceEffectId")) == identity for e in new_effects):
        return None
    old_drinks = old_progress.get("produceDrinkIds")
    if old_drinks is None and (ui.get("drink_capacity") or {}).get("owned_drinks") == []:
        # The native protobuf omits an empty repeated field. Require the
        # independently projected empty inventory, not an arbitrary default.
        old_drinks = []
    new_drinks = new_progress.get("produceDrinkIds")
    if any(not isinstance(rows, list) or any(not isinstance(v, str) or not v for v in rows)
           for rows in (old_drinks, new_drinks)):
        return None
    gain = Counter(new_drinks)[chosen["resource_id"]] - Counter(old_drinks)[chosen["resource_id"]]
    if gain < chosen["quantity"]:
        return None
    return {"kind": "native-drink-transfer-and-consumed-effect", "resource_id": chosen["resource_id"],
            "quantity": chosen["quantity"], "observed_inventory_gain": gain,
            "consumed_effect_number": identity[0], "presentation_complete": False}
