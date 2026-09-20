"""Choose an owned drink slot before claiming a reward at full capacity.

The game owns the footer, discard confirmation and reward receipt. This pure
policy only chooses among those native callbacks; it never forces Receive.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .deck_value import context_from_native, evaluate_drink_inventory_delta, normalize_deck
from .master_db import DEFAULT_DATABASE


class RewardDrinkCapacityError(ValueError):
    pass


_IDENTITY = ("parent_instance_id", "reward_view_instance_id", "selected_reward_index",
             "selected_reward_id", "selected_reward_quantity", "limit_count",
             "inventory_fingerprint", "footer_instance_id", "reward_pool_fingerprint")
_PANEL_IDENTITY = ("parent_screen_type", "panel_instance_id", "reward_group_index",
                   "reward_group_position_number", "footer_callback_name")
_CAPACITY_ACTIONS = {"reward.drink_capacity_open", "reward.drink_capacity_trash",
                     "reward.drink_capacity_confirm", "reward.drink_capacity_cancel"}


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise RewardDrinkCapacityError(f"invalid native {label}")
    return value


def _pointer(value):
    if not isinstance(value, str) or not value:
        raise RewardDrinkCapacityError("native reward owner pointer is missing")
    try:
        parsed = int(value, 16 if value.startswith("0x") else 10)
    except ValueError as error:
        raise RewardDrinkCapacityError("native reward owner pointer is invalid") from error
    if not 0 < parsed < 2 ** 64:
        raise RewardDrinkCapacityError("native reward owner pointer is out of range")
    return parsed


def _owner_identity(raw, state, child):
    """Legacy selector frames remain valid; Panel ownership is explicit."""
    kind = state.get("parent_screen_type", "ProduceRewardSelectorDialogPresenter")
    if "parent_screen_type" not in state:
        return kind, _IDENTITY
    if any(key not in state for key in _PANEL_IDENTITY):
        raise RewardDrinkCapacityError("native capacity parent identity is incomplete")
    if kind == "ProduceRewardSelectorDialogPresenter":
        if (state["panel_instance_id"] is not None or state["reward_group_index"] != -1
                or state["reward_group_position_number"] != -1 or state["footer_callback_name"] != "OnChangeDrink"):
            raise RewardDrinkCapacityError("native selector capacity owner differs")
    elif kind == "ScheduleFanPresentScreenPresenter":
        _pointer(state["panel_instance_id"])
        _integer(state["reward_group_index"], "reward group index")
        _integer(state["reward_group_position_number"], "reward group position")
        if state["footer_callback_name"] != "OnAfterDrinkRemove":
            raise RewardDrinkCapacityError("native panel footer callback owner differs")
        if child:
            if raw.get("underlying_screen_type") != kind:
                raise RewardDrinkCapacityError("capacity child belongs to another underlying panel screen")
        else:
            group = (raw.get("ui_state") or {}).get("reward_group")
            if (not isinstance(group, Mapping) or group.get("owner_type") != kind
                    or _pointer(group.get("owner_instance_id")) != _pointer(state["parent_instance_id"])
                    or _pointer(group.get("panel_instance_id")) != _pointer(state["panel_instance_id"])
                    or group.get("group_index") != state["reward_group_index"]
                    or group.get("is_completed") is not False or group.get("is_receiving") is not False
                    or group.get("is_selecting") is not True):
                raise RewardDrinkCapacityError("native capacity does not bind the current selecting reward panel")
    else:
        raise RewardDrinkCapacityError("native capacity parent screen is unsupported")
    return kind, _IDENTITY + _PANEL_IDENTITY


def choose_runtime_reward_drink_capacity_action(native, *, deck_context, database: Path = DEFAULT_DATABASE):
    raw = native.raw if hasattr(native, "raw") else native
    if not isinstance(raw, Mapping):
        raise RewardDrinkCapacityError("native capacity snapshot is not an object")
    ui = raw.get("ui_state", {})
    if not isinstance(ui, Mapping):
        raise RewardDrinkCapacityError("native capacity UI state is not an object")
    child = raw.get("surface") == "reward_drink_capacity"
    state = ui if child else ui.get("drink_capacity")
    if not isinstance(state, Mapping):
        return None
    if raw.get("busy") is not False or raw.get("actions_complete") is not True:
        raise RewardDrinkCapacityError("native reward drink capacity is not ready")
    phase = state.get("phase")
    owner_kind, identity_keys = _owner_identity(raw, state, child)
    if phase == "confirm_skip":
        if (not child or raw.get("screen_type") != "ProducePresentSkipConfirmSheetPresenter"
                or state.get("select_status") != 2):
            raise RewardDrinkCapacityError("native skip confirmation does not belong to the selected reward skip")
        actions = raw.get("legal_actions")
        if not isinstance(actions, list):
            raise RewardDrinkCapacityError("native skip confirmation actions are unavailable")
        matches = []
        for action in actions:
            target = action.get("target") if isinstance(action, Mapping) else None
            if not isinstance(target, Mapping) or action.get("action_id") not in {
                    "reward.drink_capacity_confirm_skip", "reward.drink_capacity_cancel_skip"}:
                raise RewardDrinkCapacityError("unexpected native skip confirmation action")
            if target.get("action_id") != action["action_id"] or any(k not in state or k not in target
                    or type(target[k]) is not type(state[k]) or target[k] != state[k] for k in identity_keys):
                raise RewardDrinkCapacityError("native skip confirmation belongs to another reward")
            if action["action_id"] == "reward.drink_capacity_confirm_skip":
                matches.append(target)
        if len(matches) > 1:
            raise RewardDrinkCapacityError("native skip confirmation is ambiguous")
        detail = {"source": "native-reward-drink-capacity", "phase": phase, "status": "ready",
                  "reason": "confirm the native reward skip; retain all owned drinks"}
        return (dict(matches[0]), detail) if matches else (None, {**detail, "status": "waiting"})
    if not child and state.get("is_drink_max") is not True:
        return None  # Normal reward receipt does not need capacity preparation.
    expected = {"reward_full": owner_kind,
                "drink_detail": "ProduceDrinkConfirmSheetPresenter",
                "confirm_discard": "SimpleSheetPresenter"}
    if phase not in expected or raw.get("screen_type") != expected[phase] or child != (phase != "reward_full"):
        raise RewardDrinkCapacityError("native capacity phase does not match its screen")
    for key in identity_keys:
        if key not in state:
            raise RewardDrinkCapacityError("native capacity identity missing: " + key)
    for key in ("parent_instance_id", "reward_view_instance_id", "selected_reward_id", "inventory_fingerprint", "footer_instance_id", "reward_pool_fingerprint"):
        if not isinstance(state[key], str) or not state[key]:
            raise RewardDrinkCapacityError("native capacity identity is empty: " + key)
    selected = _integer(state["selected_reward_index"], "selected reward index")
    limit = _integer(state["limit_count"], "drink capacity", 1)
    if type(state["selected_reward_quantity"]) is not int or state["selected_reward_quantity"] != 1:
        raise RewardDrinkCapacityError("capacity preparation currently requires one actual reward drink")
    owned = state.get("owned_drinks")
    if not isinstance(owned, list) or len(owned) != limit:
        raise RewardDrinkCapacityError("capacity preparation requires the actual full owned inventory")
    ids = []
    for index, row in enumerate(owned):
        if not isinstance(row, Mapping) or _integer(row.get("index"), "owned index") != index:
            raise RewardDrinkCapacityError("native owned drink slots are not complete and ordered")
        identity = row.get("drink_id")
        if not isinstance(identity, str) or not identity:
            raise RewardDrinkCapacityError("native owned drink ID is missing")
        ids.append(identity)
    if (raw.get("progress") or {}).get("produceDrinkIds", []) != ids:
        raise RewardDrinkCapacityError("capacity inventory differs from current native progress")
    if not child:
        if _pointer(state["parent_instance_id"]) != _pointer(raw.get("screen_instance_id")):
            raise RewardDrinkCapacityError("capacity preparation belongs to a different reward presenter")
        rows = ui.get("candidates")
        if not isinstance(rows, list):
            raise RewardDrinkCapacityError("native reward candidates are unavailable")
        if owner_kind == "ScheduleFanPresentScreenPresenter":
            from .runtime_reward_quantities import resolve_present_quantities

            # Native supplied the actual panel PositionNumber. Restrict the
            # existing full-pool resolver to that identity, not group_index.
            presents = (raw.get("collections") or {}).get("present")
            if not isinstance(presents, list):
                raise RewardDrinkCapacityError("native panel present collection is unavailable")
            bound = [p for p in presents if isinstance(p, Mapping)
                     and p.get("positionNumber", 0) == state["reward_group_position_number"]]
            if len(bound) != 1 or bound[0].get("received", False) is not False:
                raise RewardDrinkCapacityError("native panel position does not bind one unreceived present")
            try:
                rows = resolve_present_quantities({**raw, "collections": {**raw.get("collections", {}), "present": bound}}, rows)
            except ValueError as error:
                raise RewardDrinkCapacityError("native panel quantity binding: " + str(error)) from error
        chosen = [row for row in rows if row.get("selected") is True]
        if (len(chosen) != 1 or chosen[0].get("index") != selected or ui.get("selected_index") != selected
                or ui.get("select_status") != 1 or ui.get("receive_enabled") is not False
                or chosen[0].get("resource_type") not in (3, "ProduceResourceType_ProduceDrink")
                or chosen[0].get("resource_id") != state["selected_reward_id"] or chosen[0].get("quantity") != 1):
            raise RewardDrinkCapacityError("capacity preparation does not bind the selected blocked drink reward")
    opened = None
    if child:
        opened = _integer(state.get("opened_owned_index"), "opened drink slot")
        if opened >= limit or state.get("opened_drink_id") != ids[opened]:
            raise RewardDrinkCapacityError("native opened drink does not match its owned slot")
    actions = raw.get("legal_actions")
    if not isinstance(actions, list) or any(not isinstance(a, Mapping) or not isinstance(a.get("target"), Mapping) for a in actions):
        raise RewardDrinkCapacityError("native capacity actions are unavailable")
    for action in actions:
        name, target = action.get("action_id"), action.get("target")
        if name not in _CAPACITY_ACTIONS and not (owner_kind == "ScheduleFanPresentScreenPresenter" and name == "reward.skip"):
            continue
        if not isinstance(target, Mapping) or target.get("action_id") != name or any(
                type(target.get(k)) is not type(state[k]) or target.get(k) != state[k] for k in identity_keys):
            raise RewardDrinkCapacityError("native capacity action belongs to another reward or inventory")
        if name == "reward.skip":
            continue
        slot = _integer(target.get("owned_index"), "action owned slot")
        if slot >= limit or target.get("drink_id") != ids[slot] or child and slot != opened:
            raise RewardDrinkCapacityError("native capacity action targets a different owned drink")
    context = context_from_native(raw)
    supplied = deck_context() if callable(deck_context) else deck_context
    if not isinstance(supplied, Mapping):
        raise RewardDrinkCapacityError("complete deck context is required for capacity selection")
    actual_state = raw.get("state") or {}
    authority = {key: actual_state[key] for key in ("produce_id", "week", "stamina", "max_stamina",
        "produce_points", "vocal", "dance", "visual", "vote_count") if key in actual_state}
    if (raw.get("progress") or {}).get("idolCardId"):
        authority["idol_card_id"] = raw["progress"]["idolCardId"]
    authority["drink_capacity"] = limit
    for key, value in authority.items():
        if key in supplied and (type(supplied[key]) is not type(value) or supplied[key] != value):
            raise RewardDrinkCapacityError("deck context differs from native " + key)
    context.update(supplied)
    context.update(authority)
    context["drink_capacity"] = limit
    cards = (raw.get("collections") or {}).get("cards")
    delta = evaluate_drink_inventory_delta(normalize_deck(cards), ids, [state["selected_reward_id"]], context, database=database)
    retained = delta.retained_indexes
    if (not isinstance(retained, tuple) or len(retained) != limit or tuple(sorted(set(retained))) != retained
            or any(type(i) is not int or not 0 <= i <= limit for i in retained)):
        raise RewardDrinkCapacityError("portfolio must return exactly the actual drink capacity")
    replace = limit in retained and delta.value > 0
    discarded = next((i for i in range(limit) if i not in retained), None) if replace else None
    metadata = {"source": "native-reward-drink-capacity", "status": "ready", "phase": phase,
                "retained_indexes": list(retained), "ranking_score": delta.value, "value_method": delta.method,
                "unscored_effects": list(delta.unscored_effects), "selected_reward_id": state["selected_reward_id"],
                "discard_owned_index": discarded, "inventory_fingerprint": state["inventory_fingerprint"]}
    if not child:
        name = "reward.drink_capacity_open" if replace else "reward.skip"
        required = {"owned_index": discarded} if replace else {}
    elif discarded != opened:
        name, required = "reward.drink_capacity_cancel", {}
    else:
        name = "reward.drink_capacity_trash" if phase == "drink_detail" else "reward.drink_capacity_confirm"
        required = {"owned_index": opened}
    matches = [a for a in actions if a.get("action_id") == name and
               all(a["target"].get(k) == value for k, value in required.items())]
    if len(matches) > 1:
        raise RewardDrinkCapacityError("native capacity callback is ambiguous")
    if not matches:
        return None, {**metadata, "status": "waiting", "reason": "waiting for the bound native capacity control", "waiting_for": name}
    return dict(matches[0]["target"]), {**metadata, "reason": "keep the better drink portfolio using normal native capacity controls"}
