"""Recover economy intent from the existing settled Outer transaction journal.

No second pending-action file or controller. A proposed policy result is never
accepted as evidence. The native gateway's completed receipt is the authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path

from .runtime_economy_policy import INTENT_SCHEMA, _card_signature, _quote, _scope


_OPEN = {"shop.buy", "shop.open_product", "shop.finish",
         "interval.execute", "interval.open_product", "interval.finish"}
_CLOSE = {"shop.confirm_operation", "shop.cancel_operation", "shop.confirm_finish",
          "interval.confirm_operation", "interval.cancel_operation", "interval.confirm_finish"}


def _completed_without_confirmation(context, before, after):
    """A normal skip-confirm purchase can finish in the originating view."""
    quote = context.get("quote") or {}
    ui = after.get("ui_state") or {}
    if (after.get("surface") != context["surface"] or ui.get("phase") != "products"
            or _scope(after) != context["progress_scope"] or not any(
                row.get("target", {}).get("owner_instance_id") == context["parent_instance_id"]
                for row in after.get("legal_actions", ()))):
        return False
    old_points = (before.get("ui_state") or {}).get("produce_points", before.get("state", {}).get("produce_points"))
    points, price = ui.get("produce_points"), quote.get("price")
    if all(type(value) is int for value in (old_points, points, price)) and price > 0 and old_points - points == price:
        return True
    # Free purchases need an observed consumption or actual deck change, not
    # merely a changed selection/button in this view.
    if price == 0:
        rows = [row for row in ui.get("products", ()) if row.get("product_instance_id") == quote.get("product_instance_id")]
        if any(row.get("purchased") is True for row in rows):
            return True
        old_cards = (before.get("collections") or {}).get("cards")
        new_cards = (after.get("collections") or {}).get("cards")
        if isinstance(old_cards, list) and isinstance(new_cards, list):
            def composition(cards):
                return Counter(_card_signature(row) for row in cards if row.get("deleted") is not True)
            try:
                return composition(old_cards) != composition(new_cards)
            except (ValueError, TypeError, KeyError):
                return False
    return False


def advance_economy_context(context, receipt):
    """Consume one real settled journal row; preserve context across child views."""
    if not isinstance(receipt, Mapping):
        return context
    request, outcome, before = receipt.get("request"), receipt.get("outcome"), receipt.get("before")
    if (not isinstance(request, Mapping) or request.get("command") != "outer.action"
            or not isinstance(outcome, Mapping) or outcome.get("status") != "settled"
            or not isinstance(before, Mapping) or not isinstance(outcome.get("after"), Mapping)):
        return context
    target = request.get("target")
    if not isinstance(target, Mapping) or not request.get("request_id") or not request.get("session_generation"):
        return context
    if not any(isinstance(row, Mapping) and row.get("target") == target for row in before.get("legal_actions", ())):
        return context
    if request.get("expected_revision") != before.get("revision"):
        return context
    name = target.get("action_id")
    generation, scope = request["session_generation"], _scope(before)
    if context is not None and (context.get("session_generation") != generation or context.get("progress_scope") != scope):
        context = None
    if name in _CLOSE:
        return None
    if name in _OPEN:
        surface = name.split(".")[0]
        parent = target.get("owner_instance_id")
        if (before.get("surface") != surface or not isinstance(parent, str) or not parent
                or any(not isinstance(scope[key], str) or not scope[key] for key in ("produce_id", "idol_card_id"))
                or any(type(scope[key]) is not int for key in ("week", "step_type"))):
            return None
        quote = None
        if not name.endswith(".finish"):
            products = (before.get("ui_state") or {}).get("products", ())
            matches = []
            for row in products:
                try:
                    candidate = _quote(row, surface)
                except (TypeError, KeyError):
                    continue
                if all(target.get(key) == value for key, value in candidate.items()) and row.get("eligible") is True:
                    matches.append(candidate)
            if len(matches) != 1:
                return None
            quote = matches[0]
        opened = {"schema": INTENT_SCHEMA, "surface": surface, "session_generation": generation,
                "source_revision": before["revision"], "parent_instance_id": parent,
                "progress_scope": scope, "action_id": name, "origin_target": deepcopy(dict(target)),
                "quote": deepcopy(quote), "request_id": request["request_id"], "submission_status": "settled"}
        return None if quote is not None and _completed_without_confirmation(opened, before, outcome["after"]) else opened
    if context is not None and name == "card_choice.confirm":
        if _completed_without_confirmation(context, before, outcome["after"]):
            return None
        state = before.get("ui_state") or {}
        # This is an observed underlying presenter pointer, not the selector's
        # own pointer and not an assumed parent copied from the earlier intent.
        parent = state.get("parent_instance_id")
        rows = [row for row in state.get("candidates", ()) if row.get("selected") is True]
        kind = {997: "Upgrade", 998: "Delete"}.get((context.get("quote") or {}).get("resource_type"))
        if (kind is None or state.get("family") != "card-selector" or state.get("selection_type") != kind
                or state.get("selected_count") != 1 or len(rows) != 1
                or parent != context["parent_instance_id"]):
            return {key: deepcopy(value) for key, value in context.items() if key != "card_selection"}
        return {**context, "card_selection": {"session_generation": generation, "parent_instance_id": parent,
            "request_id": request["request_id"], "submission_status": "settled", "selection_type": kind,
            "selected_count": 1, "selected_card": deepcopy(rows[0])}}
    return context


def recover_economy_context(journal_directory: Path, *, limit=64):
    """Read a bounded recent tail on runner restart; never replay input.

    Missing/corrupt records cannot authorize a confirmation. The pure economy
    policy rechecks generation, scope, parent and the current complete quote.
    An operation older than this tail has no recovered authorization.
    """
    root = Path(journal_directory)
    try:
        paths = sorted(((path.stat().st_mtime_ns, path) for path in root.glob("*.json")),
                       key=lambda entry: (entry[0], entry[1].name))[-limit:]
    except OSError:
        return None
    context = None
    for _, path in paths:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            context = advance_economy_context(context, receipt)
        except (OSError, ValueError, TypeError, AttributeError):
            context = None
    return context


def read_economy_receipt(journal_directory, request_id, context):
    """Read only the exact archive emitted by the existing native gateway."""
    if not isinstance(request_id, str) or not request_id.isalnum():
        return context
    try:
        receipt = json.loads((Path(journal_directory) / f"{request_id}.json").read_text(encoding="utf-8"))
        if receipt.get("request", {}).get("request_id") != request_id:
            return None
        return advance_economy_context(context, receipt)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
