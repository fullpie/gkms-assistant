"""Completion evidence for opening the game's current reward package."""
from collections.abc import Mapping


def reward_group_advanced(before, after, target):
    if after.get("screen_type") != before.get("screen_type"):
        return True
    first = (before.get("ui_state") or {}).get("reward_group")
    current = (after.get("ui_state") or {}).get("reward_group")
    if not isinstance(first, Mapping) or not isinstance(current, Mapping):
        return False
    for name in ("owner_type", "owner_instance_id", "panel_instance_id", "button_instance_id", "group_index"):
        if name not in first or target.get(name) != first[name]:
            return False
    if any(current.get(name) != first[name] for name in ("owner_type", "owner_instance_id", "panel_instance_id")):
        return False
    # The real OnClicked handler clears this flag before its own OpenBoxAsync.
    # Button animation/readiness/receiving flags alone do not prove it ran.
    return (first.get("is_waiting_open") is True and current.get("is_waiting_open") is False
            or type(current.get("group_index")) is int and type(first["group_index"]) is int
                and current["group_index"] > first["group_index"]
            or first.get("is_completed") is False and current.get("is_completed") is True)
