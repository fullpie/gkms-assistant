"""Pure Shop/Interval choices over native products and one caller-owned intent.

API: choose_runtime_economy_action(native, deck_context=..., operation_context=None).
No input, IPC or persistent state is created here. Before submitting buy/open/
execute/finish, metadata contains an ``operation_intent`` with the actual native
target, quote, parent and generation. The caller retains it only after a real
receipt, adding ``request_id`` and ``submission_status`` (submitted/settled).

A special card confirmation additionally needs ``card_selection`` with the same
generation/parent, its real request_id/submission_status, selection_type
(Upgrade/Delete), selected_count=1 and selected_card from the native selector.
Neither the old Shop SelectIndex nor a policy recommendation is proof of that
selection. Missing/changed context cancels an available confirmation normally.
Apply clear_operation_context/stop_after_action only after the returned cancel
actually settles; a policy return is not proof that gameplay was performed.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from .deck_value import (DeckValueError, _value, card_customizations, context_from_native,
                         evaluate_deck_delta, evaluate_drink_inventory_delta, normalize_deck)
from .master_db import DEFAULT_DATABASE
from .outer_training_budget import budget_priority
from .passive_catalog import DEFAULT_MASTER_DIR

INTENT_SCHEMA = "gkms.native-economy-intent.v1"
_QUOTE_KEYS = ("index", "position_number", "resource_type", "resource_id", "price", "product_instance_id")
_CARD_OPERATIONS = {997: "Upgrade", 998: "Delete"}


class RuntimeEconomyError(ValueError):
    pass


def _int(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise RuntimeEconomyError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value:
        raise RuntimeEconomyError(f"{label} is absent")
    return value


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise RuntimeEconomyError(f"native {label} must be a complete array")
    return value


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _quote(row, surface):
    result = {key: row[key] for key in _QUOTE_KEYS}
    for key in (("upgrade",) if surface == "shop" else ("group",)):
        result[key] = row[key]
    return result


def _scope(raw):
    state, progress = raw.get("state", {}), raw.get("progress", {})
    return {"produce_id": state.get("produce_id"), "week": state.get("week"),
            "step_type": state.get("step_type"), "idol_card_id": progress.get("idolCardId")}


def _card_signature(row):
    normalized = normalize_deck([row])[0]
    return normalized["card_id"], normalized["upgrade"], tuple(sorted(card_customizations(normalized)))


def _changed_deck(deck, selected, kind):
    after = [dict(card) for card in deck]
    if kind == "Delete":
        after.pop(selected)
    else:
        after[selected]["upgrade"] += 1
        after[selected]["upgradeCount"] = after[selected]["upgrade"]
    return after


def choose_runtime_economy_action(
    native, *, deck_context: Callable[[], Mapping[str, Any]] | Mapping[str, Any],
    operation_context: Mapping[str, Any] | None = None, database: Path = DEFAULT_DATABASE,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any]]:
    """Rank the full offer pool, then bind its real preview/execute callback."""
    raw = native.raw if hasattr(native, "raw") else native
    if not isinstance(raw, Mapping):
        raise RuntimeEconomyError("native economy snapshot is not an object")
    surface, state = raw.get("surface"), raw.get("ui_state", {})
    source = {"source": "native-economy-deck-value"}
    # Current Interval inner confirmation does not yet project its full quote.
    # Its normal cancel is useful; a generic affirmative is not proof of price.
    if (raw.get("screen_type") == "ScheduleShopConfirmSheetPresenter"
            and raw.get("underlying_screen_type") == "ScheduleIntervalScreenPresenter" and surface != "interval"):
        if raw.get("busy") is not False or raw.get("actions_complete") is not True:
            return None, {**source, "status": "waiting", "reason": "Interval confirmation is not actionable"}
        actions = _rows(raw.get("legal_actions"), "economy confirmation actions")
        if any(not isinstance(a.get("target"), Mapping) or a["target"].get("action_id") != a.get("action_id") for a in actions):
            raise RuntimeEconomyError("Interval confirmation action identity differs from target")
        cancel = [a for a in actions if a.get("action_id") == "ui.sheet_cancel" and isinstance(a.get("target"), Mapping)]
        if len(cancel) > 1:
            raise RuntimeEconomyError("ambiguous Interval confirmation cancel")
        return (dict(cancel[0]["target"]) if cancel else None), {**source,
            "status": "ready" if cancel else "abstained", "reason": "Interval confirmation has no current native price/parent quote",
            "operation_context_rejected": True, "clear_operation_context": bool(cancel),
            "stop_after_action": bool(cancel),
            "unscored_effects": ["unprojected-interval-purchase-confirmation"]}
    if surface not in {"shop", "interval"}:
        return None, {**source, "status": "not-applicable"}
    if not isinstance(state, Mapping) or raw.get("busy") is not False or raw.get("actions_complete") is not True:
        raise RuntimeEconomyError("native economy boundary is not ready")
    generation = _text(getattr(native, "session_generation", None) or raw.get("session_generation"), "native session generation")
    phase = state.get("phase")
    expected_screen = ({"shop": "ScheduleShopScreenPresenter", "interval": "ScheduleIntervalScreenPresenter"}.get(surface)
                       if phase == "products" else "ProduceShopEndWarningSheetPresenter" if phase == "confirm_finish"
                       else "ScheduleShopConfirmSheetPresenter" if phase == "confirm_purchase" else None)
    if expected_screen is not None and raw.get("screen_type") != expected_screen:
        raise RuntimeEconomyError("economy phase does not match its actual native presenter")
    actions = _rows(raw.get("legal_actions"), "economy actions")
    for action in actions:
        target = action.get("target")
        if not isinstance(target, Mapping) or target.get("action_id") != action.get("action_id"):
            raise RuntimeEconomyError("native economy action identity differs from target")

    def matching(name, quote=None):
        found = [a for a in actions if a["action_id"] == name and
                 (quote is None or all(key in a["target"] and _bytes(a["target"][key]) == _bytes(value) for key, value in quote.items()))]
        if len(found) > 1:
            raise RuntimeEconomyError(f"ambiguous native economy action: {name}")
        return found[0] if found else None

    def choose(action, reason, **detail):
        if action is None:
            return None, {**source, **detail, "status": "waiting", "reason": "waiting for the chosen native economy control"}
        return dict(action["target"]), {**source, **detail, "status": "ready", "reason": reason}

    def context_valid(origin_names, parent):
        ctx = operation_context
        if not isinstance(ctx, Mapping) or ctx.get("schema") != INTENT_SCHEMA or ctx.get("surface") != surface:
            return False
        if ctx.get("session_generation") != generation or ctx.get("parent_instance_id") != parent or ctx.get("progress_scope") != _scope(raw):
            return False
        if ctx.get("submission_status") not in {"submitted", "settled"} or not isinstance(ctx.get("request_id"), str) or not ctx["request_id"]:
            return False
        origin = ctx.get("origin_target")
        return (isinstance(origin, Mapping) and ctx.get("action_id") in origin_names
                and origin.get("action_id") == ctx["action_id"] and origin.get("owner_instance_id") == parent)

    if phase == "confirm_finish":
        confirm = matching(f"{surface}.confirm_finish")
        parent = None if confirm is None else confirm["target"].get("parent_instance_id")
        if not context_valid({f"{surface}.finish"}, parent):
            return None, {**source, "status": "abstained", "reason": "finish confirmation has no submitted caller-bound finish intent",
                          "operation_context_rejected": True}
        return choose(confirm, "complete the caller's verified decision to leave", clear_operation_context=True)
    if phase not in {"products", "confirm_purchase"} or state.get("data_ready") is not True:
        return None, {**source, "status": "abstained", "reason": "unsupported native economy phase"}

    points = _int(state.get("produce_points"), "current native points")
    products = _rows(state.get("products"), "economy products")
    keys, identities = set(), set()
    for offset, row in enumerate(products):
        index = _int(row.get("index"), "product index")
        _int(row.get("position_number"), "product position")
        kind = _int(row.get("resource_type"), "product resource type")
        _int(row.get("price"), "actual product price")
        if not isinstance(row.get("resource_id"), str):
            raise RuntimeEconomyError("product resource ID must preserve an observed string")
        _text(row.get("product_instance_id"), "native product instance")
        if surface == "shop":
            _int(row.get("upgrade"), "product upgrade")
            if index != offset:
                raise RuntimeEconomyError("native Shop product order differs from its indexes")
            group = None
        else:
            group = _text(row.get("group"), "Interval product group")
        if (group, index) in keys or (kind, row["position_number"]) in identities:
            raise RuntimeEconomyError("native economy product identity is repeated")
        keys.add((group, index)); identities.add((kind, row["position_number"]))
        for key in ("selected", "direct_open", "eligible", "can_buy", "purchased"):
            if type(row.get(key)) is not bool:
                raise RuntimeEconomyError(f"native product {key} flag is unavailable")
        if row["eligible"] and (not row["can_buy"] or row["purchased"] or row["price"] > points
                                or row.get("locked") is True or row.get("drink_capacity_blocked") is True
                                or (row.get("remaining_count") is not None and _int(row["remaining_count"], "remaining product count") == 0)):
            raise RuntimeEconomyError("native eligible product violates its actual budget/availability")
    if surface == "shop":
        selected_index = _int(state.get("selected_index"), "native selected product index", -1)
        if any(row["selected"] is not (row["index"] == selected_index) for row in products):
            raise RuntimeEconomyError("native Shop selection differs from product rows")
    product_actions = {f"{surface}.select", f"{surface}.open_product", "shop.buy", "interval.execute"}
    for action in actions:
        if action["action_id"] not in product_actions:
            continue
        matches = [row for row in products if all(key in action["target"] and _bytes(action["target"][key]) == _bytes(value)
                                                  for key, value in _quote(row, surface).items())]
        if len(matches) != 1 or not matches[0]["eligible"]:
            raise RuntimeEconomyError("native economy target no longer binds one available product quote")
        row = matches[0]
        expected = f"{surface}.open_product" if row["direct_open"] else ("shop.buy" if surface == "shop" else "interval.execute") if row["selected"] else f"{surface}.select"
        if action["action_id"] != expected:
            raise RuntimeEconomyError("native product action differs from its actual selection/direct callback")

    cached_context = None
    def valuation_context():
        nonlocal cached_context
        if cached_context is None:
            supplied = deck_context() if callable(deck_context) else deck_context
            if not isinstance(supplied, Mapping):
                raise RuntimeEconomyError("economy deck context must resolve to a mapping")
            cached_context = {**context_from_native(raw), **supplied, "produce_points": points}
            for key in ("stamina", "max_stamina"):
                if key in raw.get("state", {}):
                    cached_context[key] = raw["state"][key]
        return cached_context

    collections = raw.get("collections")
    if not isinstance(collections, Mapping) or "cards" not in collections:
        raise RuntimeEconomyError("complete native deck required for economy decisions")
    deck = normalize_deck(collections["cards"])
    cost_cache = {}
    def cost(price):
        if price not in cost_cache:
            cost_cache[price] = evaluate_deck_delta(deck, deck, {**valuation_context(), "produce_point_cost": price}, database=database).value
        return cost_cache[price]

    def training_budget_assessment(price):
        # Shop/Interval products here are not the planned Customize purchase.
        # Compare the real quoted debit with leaving this menu unchanged;
        # never count a hoped-for reward or the displayed base_price as cash.
        budget = valuation_context().get("training_budget")
        before_priority = budget_priority(budget, points)
        after_points = points - price
        after_priority = budget_priority(budget, after_points)
        return {"points_before": points, "points_after": after_points,
                "leave_budget_priority": before_priority, "purchase_budget_priority": after_priority,
                "preserves_training_budget": after_priority >= before_priority,
                "training_budget_target_week": budget.get("target_week") if isinstance(budget, Mapping) else None,
                "training_budget_target_cost": budget.get("target_cost") if isinstance(budget, Mapping) else None}

    def score(row, actual_card=None):
        kind, price = row["resource_type"], row["price"]
        context = {**valuation_context(), "produce_point_cost": price}
        detail = {"quote": _quote(row, surface), "resource_type": kind, "unscored_effects": []}
        if kind == 1:
            offered = dict(row.get("card") or {"card_id": row["resource_id"], "upgrade": row.get("upgrade", row.get("data", {}).get("upgradeCount", 0))})
            if offered.get("card_id") != row["resource_id"]:
                raise RuntimeEconomyError("native product card differs from resource ID")
            offered["instance_id"] = f"proposed:economy:{row['position_number']}"
            delta = evaluate_deck_delta(deck, [*deck, offered], context, database=database)
        elif kind in _CARD_OPERATIONS:
            if kind == 998 and len(deck) <= 1:
                return None, {**detail, "unscored_effects": ["cannot-delete-last-card"]}
            variants, skipped = [], []
            for index, card in enumerate(deck):
                if actual_card is not None:
                    if _card_signature(card) != _card_signature(actual_card):
                        continue
                    if actual_card.get("deck_number", actual_card.get("number")) is not None and card.get("deck_number") != actual_card.get("deck_number", actual_card.get("number")):
                        continue
                try:
                    candidate = evaluate_deck_delta(deck, _changed_deck(deck, index, _CARD_OPERATIONS[kind]), context, database=database)
                    variants.append((candidate.value, index, candidate))
                except DeckValueError as error:
                    skipped.append(str(error))
            if not variants:
                return None, {**detail, "unscored_effects": ["no-valued-native-card-mutation", *skipped]}
            _, index, delta = max(variants, key=lambda x: (*x[2].ranking_key, -x[1]))
            detail.update(recommended_card=dict(deck[index]), selection_type=_CARD_OPERATIONS[kind],
                          recommendation_scope="actual deck value; subsequent native selector must prove its chosen card")
        elif kind == 3:
            progress = raw.get("progress")
            if not isinstance(progress, Mapping):
                raise RuntimeEconomyError("native current drink inventory is unavailable")
            owned = progress.get("produceDrinkIds", [])
            delta = evaluate_drink_inventory_delta(deck, owned, [row["resource_id"]], context, database=database)
            return delta.value + cost(price), {**detail, "value_method": delta.method,
                "unscored_effects": list(delta.unscored_effects), "value_reasons": list(delta.reasons),
                "context_digest": delta.context_digest, "retained_indexes": list(delta.retained_indexes),
                "pt_opportunity_utility": cost(price)}
        elif kind == 5:
            amount = _int(row.get("stamina_recover_value", row.get("data", {}).get("staminaRecoverValue")), "native recovery amount")
            current, maximum = _int(context.get("stamina"), "native stamina"), _int(context.get("max_stamina"), "native maximum stamina", 1)
            if current > maximum:
                raise RuntimeEconomyError("native stamina exceeds maximum")
            resource_context = dict(context); resource_context.pop("exam_outlooks", None)
            owned = raw.get("progress", {}).get("produceDrinkIds", [])
            directory = Path(context.get("master_dir", DEFAULT_MASTER_DIR))
            # Same existing deck/consumable proxy, changed scalar state only;
            # a one-use recovery is not counted once for every future audition.
            before_value, _, before_gaps, _ = _value(deck, resource_context, Path(database), directory, owned)
            after_value, _, after_gaps, _ = _value(deck, {**resource_context, "stamina": min(maximum, current + amount)}, Path(database), directory, owned)
            return after_value - before_value + cost(price), {**detail,
                "value_method": "shared-deck-resource-context-delta", "stamina_recovered": min(maximum-current, amount),
                "unscored_effects": sorted(before_gaps | after_gaps), "pt_opportunity_utility": cost(price),
                "followup_requirement": "actual recovery amount/total-price confirmation remains owned by the caller"}
        else:
            return None, {**detail, "unscored_effects": [f"economy-resource-not-valued:{kind}; no invented effect or replacement card"]}
        return delta.value, {**detail, "value_method": delta.method, "context_digest": delta.context_digest,
            "unscored_effects": list(delta.unscored_effects), "value_reasons": list(delta.reasons),
            "deck_plan_progress": delta.plan_progress, "ranking_order": list(delta.ranking_key)}

    if phase == "confirm_purchase":
        cancel_detail = {}
        base = dict(state); base.pop("products_fingerprint", None); base.pop("confirmation_binding", None)
        # The bridge appends this cross-screen operation receipt after hashing
        # shop_ui/interval_ui. It is not part of the native product contract.
        base.pop("memory_auto", None)
        fingerprint = hashlib.sha256(_bytes(base)).hexdigest()
        if state.get("products_fingerprint") != fingerprint:
            raise RuntimeEconomyError("native pending-operation product fingerprint differs")
        confirm, cancel = matching(f"{surface}.confirm_operation"), matching(f"{surface}.cancel_operation")
        for action in (confirm, cancel):
            if action and action["target"].get("products_fingerprint") != fingerprint:
                raise RuntimeEconomyError("native operation confirmation does not bind the current product pool")
        parent = (confirm or cancel or {}).get("target", {}).get("parent_instance_id")
        reason = "missing or stale caller-bound operation context"
        execute_name = "shop.buy" if surface == "shop" else "interval.execute"
        if context_valid({execute_name, f"{surface}.open_product"}, parent):
            quote = operation_context.get("quote")
            rows = [row for row in products if isinstance(quote, Mapping) and _bytes(_quote(row, surface)) == _bytes(quote)]
            origin = operation_context["origin_target"]
            if len(rows) == 1 and all(key in origin and _bytes(origin[key]) == _bytes(value) for key, value in quote.items()) and rows[0]["eligible"]:
                row, actual = rows[0], None
                valid = operation_context["action_id"] == (f"{surface}.open_product" if row["direct_open"] else execute_name)
                if row["resource_type"] in _CARD_OPERATIONS:
                    selected = operation_context.get("card_selection")
                    valid = valid and (isinstance(selected, Mapping) and selected.get("session_generation") == generation
                        and selected.get("parent_instance_id") == parent and selected.get("submission_status") in {"submitted", "settled"}
                        and isinstance(selected.get("request_id"), str) and bool(selected["request_id"])
                        and selected.get("selection_type") == _CARD_OPERATIONS[row["resource_type"]]
                        and type(selected.get("selected_count")) is int and selected["selected_count"] == 1
                        and isinstance(selected.get("selected_card"), Mapping)
                        and selected["selected_card"].get("selected") is True)
                    if valid:
                        actual = selected["selected_card"]
                    else:
                        reason = "special card operation lacks actual single-card selector receipt; no price/card inference"
                if valid:
                    try:
                        value, detail = score(row, actual)
                    except (DeckValueError, sqlite3.Error, OSError):
                        value, detail = None, {}
                    if value is not None and math.isfinite(value) and value > 0:
                        budget_assessment = training_budget_assessment(row["price"])
                        detail = {**detail, **budget_assessment}
                        if budget_assessment["preserves_training_budget"]:
                            return choose(confirm, "confirm the unchanged native quote, positive value and preserved training budget",
                                          ranking_score=value, evaluation=detail, clear_operation_context=True)
                        reason = "purchase would increase the next training budget shortfall; cancel normally"
                        cancel_detail = {"training_budget_rejected": True, "ranking_score": value, "evaluation": detail}
                    else:
                        reason = "submitted operation no longer has a supported positive value"
            else:
                reason = "submitted product quote/price is absent, changed or unavailable"
        return choose(cancel, reason, operation_context_rejected=True, clear_operation_context=cancel is not None,
                      stop_after_action=cancel is not None, **cancel_detail)

    evaluations = []
    for row in products:
        if not row["eligible"]:
            evaluations.append({"quote": _quote(row, surface), "eligible": False, "value": None})
            continue
        try:
            value, detail = score(row)
        except (DeckValueError, sqlite3.Error, OSError) as error:
            value, detail = None, {"quote": _quote(row, surface), "unscored_effects": [f"valuation-unavailable:{error}"]}
        if value is not None and not math.isfinite(value):
            raise RuntimeEconomyError("economy value is not finite")
        evaluations.append({**detail, **training_budget_assessment(row["price"]), "eligible": True, "value": value})
    positive = [(value["value"], index, value) for index, value in enumerate(evaluations)
                if value["eligible"] and value["value"] is not None and value["value"] > 0
                and value["preserves_training_budget"]]
    if positive:
        _, index, evaluation = max(positive, key=lambda x: (
            *x[2].get("ranking_order", (True, 0.0, x[0])), products[x[1]]["selected"], -x[1]))
        row = products[index]
        name = f"{surface}.open_product" if row["direct_open"] else ("shop.buy" if surface == "shop" else "interval.execute") if row["selected"] else f"{surface}.select"
        action = matching(name, _quote(row, surface))
        target, metadata = choose(action, "best supported complete-deck/resource gain after the actual PT cost", evaluations=evaluations,
                                  chosen_quote=_quote(row, surface), ranking_score=evaluation["value"])
        quote = _quote(row, surface)
    else:
        name = f"{surface}.finish"
        target, metadata = choose(matching(name), "no positive purchase preserves the training budget; leave without buying", evaluations=evaluations)
        quote = None
    if target is not None and name not in {"shop.select", "interval.select"}:
        metadata["operation_intent"] = {"schema": INTENT_SCHEMA, "surface": surface,
            "session_generation": generation, "source_revision": _text(raw.get("revision"), "native source revision"),
            "parent_instance_id": _text(target.get("owner_instance_id"), "native economy parent"),
            "progress_scope": _scope(raw), "action_id": name, "origin_target": dict(target), "quote": quote,
            "submission_status": "proposed", "caller_instruction": "retain only after actual native submission; attach receipt before confirming"}
    return target, metadata
