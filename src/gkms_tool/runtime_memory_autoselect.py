"""Preserve native memory auto-selection; only explicit user locks may override.

There is no card value, replay prior, model inference or fallback filler here.
The native operation/completion receipt remains the source of the base deck.
"""
from collections.abc import Mapping


def native_memory_ids(loadout):
    rows = loadout.get("memories")
    if not isinstance(rows, list) or len(rows) != 4:
        raise ValueError("native memory auto-selection did not expose four slots")
    ordered = {}
    for row in rows:
        if (not isinstance(row, Mapping) or type(row.get("position")) is not int
                or row["position"] not in range(4) or row["position"] in ordered
                or type(row.get("is_rental")) is not bool
                or not isinstance(row.get("memory_id"), str) or not row["memory_id"]):
            raise ValueError("native memory auto-selection has an incomplete slot identity")
        ordered[row["position"]] = row["memory_id"]
    values = tuple(ordered[i] for i in range(4))
    if len(set(values)) != 4:
        raise ValueError("native memory auto-selection repeats an instance")
    return values


def memory_auto_record(snapshot):
    values = [section["memory_auto"] for section in (snapshot.get("selection"), snapshot.get("ui_state"))
        if isinstance(section, Mapping) and isinstance(section.get("memory_auto"), Mapping)]
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ValueError("memory auto-selection observations disagree")
    value = values[0]
    if value.get("schema") != "gkms.memory-auto-selection.v1":
        raise ValueError("native memory auto-selection schema differs")
    return value


def memory_auto_matches(record, target):
    return (isinstance(record, Mapping) and type(record.get("operation_serial")) is int
        and record.get("owner_bound") is True
        and record["operation_serial"] > 0 and record["operation_serial"] == target.get("operation_serial")
        and all(isinstance(target.get(key), str) and target[key] and record.get(key) == target[key]
            for key in ("account_scope", "produce_id", "idol_card_id")))


def memory_auto_terminal_failure(after, target):
    """A consumed owned task failure is final, even if its UI owner has left."""
    if target.get("action_id") not in {"loadout.memory_auto", "loadout.memory_auto_confirm"}:
        return None
    record = memory_auto_record(after)
    if (not isinstance(record, Mapping) or record.get("phase") != "failed"
            or record.get("task_consumed") is not True
            or record.get("task_status") not in {"Succeeded", "Faulted", "Canceled"}
            or type(record.get("operation_serial")) is not int or record["operation_serial"] <= 0
            or record["operation_serial"] != target.get("operation_serial")
            or any(not isinstance(target.get(k), str) or not target[k] or record.get(k) != target[k]
                   for k in ("account_scope", "produce_id", "idol_card_id"))):
        return None
    return record


def memory_auto_action_advanced(before, after, target):
    if after.get("busy") is not False:
        return False
    record = memory_auto_record(after)
    if not memory_auto_matches(record, target):
        return False
    if target.get("action_id") == "loadout.memory_auto":
        return (after.get("screen_type") == "ProduceMemoryDeckAutoSetConfirmSheetPresenter"
            and record.get("phase") == "awaiting_confirm")
    if target.get("action_id") != "loadout.memory_auto_confirm":
        return False
    if (after.get("screen_type") != "ProduceMemorySelectScreenPresenter"
            or after.get("actions_complete") is not True or record.get("phase") != "succeeded"
            or record.get("confirmed") is not True):
        return False
    try:
        native_memory_ids({"memories": record.get("resources")})
    except ValueError:
        return False
    return any(action.get("action_id") == "ui.navigation"
        and (action.get("target") or {}).get("button_id") == "produce.memory_continue"
        for action in after.get("legal_actions", ()))


def overlay_explicit_memory_locks(auto_ids, *, locked_ids=(), excluded_ids=(), eligible_owned_ids):
    """Keep native order and replace only unlocked slots with requested locks."""
    current, locked, excluded = tuple(auto_ids), tuple(locked_ids), set(excluded_ids)
    if (len(current) != 4 or len(set(current)) != 4 or len(locked) > 4 or len(set(locked)) != len(locked)
            or any(not isinstance(value, str) or not value for value in (*current, *locked))):
        raise ValueError("memory selection or explicit locks are invalid")
    if set(locked) & excluded or not set(locked) <= set(eligible_owned_ids):
        raise ValueError("locked memory is excluded, unowned or incompatible")
    result = list(current)
    missing = [value for value in locked if value not in current]
    # Prefer a prohibited slot, otherwise retain as much of the native prefix
    # as possible. This is a deterministic lock overlay, not a new ranking.
    replaceable = [i for i in range(4) if result[i] not in locked]
    replaceable.sort(key=lambda i: (result[i] not in excluded, -i))
    if len(missing) > len(replaceable):
        raise ValueError("native memory result cannot retain every explicit lock")
    overrides = []
    for index, value in zip(replaceable, missing):
        overrides.append({"position": index, "before_memory_id": result[index], "locked_memory_id": value})
        result[index] = value
    if set(result) & excluded:
        raise ValueError("game auto-selection includes an excluded memory; no custom filler policy is enabled")
    return tuple(result), tuple(overrides)
