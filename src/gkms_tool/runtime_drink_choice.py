"""Pure reconciliation of the game's kept-drink slots with a portfolio choice."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .deck_value import context_from_native, evaluate_drink_inventory_delta, normalize_deck
from .master_db import DEFAULT_DATABASE


class RuntimeDrinkChoiceError(ValueError):
    pass


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise RuntimeDrinkChoiceError(f"{label} must be an integer >= {minimum}")
    return value


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise RuntimeDrinkChoiceError(f"native {label} must be a complete array")
    return value


def _slot(row):
    if type(row.get("is_new")) is not bool:
        raise RuntimeDrinkChoiceError("native drink slot needs a Boolean new/owned identity")
    return row["is_new"], _integer(row.get("index"), "native drink slot index")


def _nonempty(value, label):
    if not isinstance(value, str) or not value:
        raise RuntimeDrinkChoiceError(f"native {label} is absent")
    return value


def choose_runtime_drink_inventory_action(
    native, *, deck_context: Callable[[], Mapping[str, Any]] | Mapping[str, Any],
    database: Path = DEFAULT_DATABASE,
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any]]:
    """Return one unchanged native target; never click or mutate the snapshot.

    The model's selected tuples identify drinks to KEEP. Portfolio indexes use
    owned slots followed by new slots, so duplicate IDs remain distinct.
    """
    raw = native.raw if hasattr(native, "raw") else native
    if not isinstance(raw, Mapping):
        raise RuntimeDrinkChoiceError("native drink snapshot is not an object")
    state = raw.get("ui_state")
    if not isinstance(state, Mapping) or state.get("family") != "drink_inventory":
        return None, {"source": "native-drink-portfolio", "status": "not-applicable"}
    if raw.get("busy") is not False or raw.get("actions_complete") is not True or state.get("data_ready") is not True:
        raise RuntimeDrinkChoiceError("native drink inventory is not ready")
    phase = state.get("phase")
    expected_screen = {"select_kept_drinks": "ProduceDrinkMaxSheetPresenter",
                       "confirm_no_new_drinks": "ProducePresentSkipConfirmSheetPresenter"}.get(phase)
    if expected_screen is None or raw.get("screen_type") != expected_screen or raw.get("surface") != "drink_inventory":
        raise RuntimeDrinkChoiceError("native drink phase does not match its current screen")
    limit = _integer(state.get("limit_count"), "native drink capacity", 1)
    owned, added = _rows(state.get("owned_drinks"), "owned drinks"), _rows(state.get("new_drinks"), "new drinks")
    pool, by_slot = [], {}
    for is_new, values in ((False, owned), (True, added)):
        for index, row in enumerate(values):
            slot = _slot(row)
            if slot != (is_new, index) or row.get("source") != ("new" if is_new else "owned"):
                raise RuntimeDrinkChoiceError("native owned/new pool order or slot identity differs")
            _nonempty(row.get("drink_id"), "drink ID")
            pool.append(row)
            by_slot[slot] = row
    if len(owned) > limit or len(pool) < limit or not added:
        raise RuntimeDrinkChoiceError("native full-inventory pool/capacity is inconsistent")
    selected_rows = _rows(state.get("selected"), "kept drink slots")
    selected = set()
    for row in selected_rows:
        slot = _slot(row)
        if slot in selected or slot not in by_slot or row.get("drink_id") != by_slot[slot]["drink_id"]:
            raise RuntimeDrinkChoiceError("native kept slot repeats or differs from its drink pool")
        selected.add(slot)
    if _integer(state.get("selected_count"), "native kept count") != len(selected):
        raise RuntimeDrinkChoiceError("native kept count differs from actual selected slots")
    if state.get("keeps_new_drink") is not any(slot[0] for slot in selected):
        raise RuntimeDrinkChoiceError("native new-drink retention flag differs from selected slots")
    encoded = json.dumps([owned, added, selected_rows, limit], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if state.get("selection_fingerprint") != fingerprint:
        raise RuntimeDrinkChoiceError("native drink selection fingerprint differs from the full pool/kept set")
    items, list_indexes = {}, []
    for row in _rows(state.get("items"), "mixed-list drink items"):
        slot = _slot(row)
        index = _integer(row.get("list_index"), "native mixed-list index")
        if slot in items or slot not in by_slot or row.get("drink_id") != by_slot[slot]["drink_id"] or row.get("source") != by_slot[slot]["source"]:
            raise RuntimeDrinkChoiceError("native mixed-list item repeats or differs from its drink slot")
        if row.get("selected") is not (slot in selected):
            raise RuntimeDrinkChoiceError("native mixed-list selected identity differs from kept slots")
        if row.get("toggle_allowed") is not (slot in selected or len(selected) < limit):
            raise RuntimeDrinkChoiceError("native drink toggle capacity contract differs")
        items[slot] = row
        list_indexes.append(index)
    if set(items) != set(by_slot) or list_indexes != sorted(set(list_indexes)):
        raise RuntimeDrinkChoiceError("native mixed-list pool is incomplete or out of order")

    actions = _rows(raw.get("legal_actions"), "drink actions")
    allowed = {"drink_choice.toggle", "drink_choice.confirm"} if phase == "select_kept_drinks" else {
        "drink_choice.confirm_skip_new", "drink_choice.cancel_skip"}
    parent_ids, signatures = set(), set()
    for action in actions:
        name, target = action.get("action_id"), action.get("target")
        if name not in allowed or not isinstance(target, Mapping) or target.get("action_id") != name:
            raise RuntimeDrinkChoiceError("unexpected action in native drink inventory")
        if target.get("selection_fingerprint") != fingerprint or target.get("kept_drinks") != selected_rows:
            raise RuntimeDrinkChoiceError("native drink action does not bind the actual kept set/fingerprint")
        parent_ids.add(_nonempty(target.get("parent_instance_id"), "drink selector parent"))
        _nonempty(target.get("callback_instance_id"), "drink action callback")
        if name == "drink_choice.toggle":
            slot = _slot(target)
            row = items.get(slot)
            if row is None or any(target.get(key) != row[key] for key in ("list_index", "drink_id", "source")) or target.get("selected") is not (slot in selected) or not row["toggle_allowed"]:
                raise RuntimeDrinkChoiceError("native toggle target no longer binds its current drink slot")
            for key in ("item_instance_id", "list_instance_id"):
                _nonempty(target.get(key), key)
            signature = name, slot
        else:
            for key in ("sheet_instance_id", "button_instance_id"):
                _nonempty(target.get(key), key)
            if name != "drink_choice.cancel_skip" and len(selected) != limit:
                raise RuntimeDrinkChoiceError("native drink confirmation does not keep exact capacity")
            if name == "drink_choice.confirm_skip_new" and any(slot[0] for slot in selected):
                raise RuntimeDrinkChoiceError("native skip-new confirmation retains a new drink")
            signature = name, None
        if signature in signatures:
            raise RuntimeDrinkChoiceError("native drink action is ambiguous")
        signatures.add(signature)
    if len(parent_ids) > 1:
        raise RuntimeDrinkChoiceError("native drink actions refer to different parent selectors")

    collections = raw.get("collections")
    if not isinstance(collections, Mapping) or "cards" not in collections:
        raise RuntimeDrinkChoiceError("complete native collections.cards required for drink portfolio")
    context = context_from_native(raw)
    supplied = deck_context() if callable(deck_context) else deck_context
    if not isinstance(supplied, Mapping):
        raise RuntimeDrinkChoiceError("drink deck context resolver must return a mapping")
    context.update(supplied)
    context["drink_capacity"] = limit  # The live selector, not a saved preference.
    delta = evaluate_drink_inventory_delta(normalize_deck(collections["cards"]),
        [row["drink_id"] for row in owned], [row["drink_id"] for row in added], context, database=database)
    retained = delta.retained_indexes
    if not isinstance(retained, tuple) or len(retained) != limit or any(type(index) is not int or index < 0 or index >= len(pool) for index in retained) or tuple(sorted(set(retained))) != retained:
        raise RuntimeDrinkChoiceError("drink portfolio did not return exact distinct native retained indexes")
    desired = {_slot(pool[index]) for index in retained}
    metadata = {"source": "native-drink-portfolio", "status": "ready", "inventory_kind": "drink",
        "retained_indexes": list(retained), "desired_slots": [dict(pool[index]) for index in retained],
        "selection_fingerprint": fingerprint, "ranking_score": delta.value,
        "before_deck_value": delta.before_value, "after_deck_value": delta.after_value,
        "value_method": delta.method, "context_digest": delta.context_digest,
        "unscored_effects": list(delta.unscored_effects), "value_reasons": list(delta.reasons)}
    metadata["effect_valuation"] = getattr(delta, "valuation", None)

    def choose(name, reason, slot=None):
        matches = [action for action in actions if action["action_id"] == name and
                   (slot is None or _slot(action["target"]) == slot)]
        if not matches:
            return None, {**metadata, "status": "waiting", "reason": "等待所選飲料操作就緒"}
        return dict(matches[0]["target"]), {**metadata, "reason": reason}

    if phase == "confirm_no_new_drinks":
        if desired != selected or any(slot[0] for slot in desired):
            return choose("drink_choice.cancel_skip", "返回飲料選擇，套用目前牌庫的最佳保留組合")
        return choose("drink_choice.confirm_skip_new", "最佳保留組合為現有飲料，確認放棄本次新飲料")
    removals = selected - desired
    if removals:
        slot = next(_slot(row) for row in pool if _slot(row) in removals)
        return choose("drink_choice.toggle", "先取消不在最佳組合中的飲料，騰出保留名額", slot)
    additions = desired - selected
    if additions:
        slot = next(_slot(row) for row in pool if _slot(row) in additions)
        return choose("drink_choice.toggle", "加入最佳組合中的飲料", slot)
    return choose("drink_choice.confirm", "已核對完整最佳保留組合與容量，確認飲料選擇")
