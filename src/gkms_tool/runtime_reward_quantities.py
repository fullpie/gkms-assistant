"""Recover quantities omitted by the native Present reward view model.

GetRewardList constructs Card/Item/Drink ProduceRewardData with quantity=0.
The same snapshot's original present still carries the real quantity. Resolve
one complete matching group; panel indexes are not raw present-list indexes.
"""
from collections.abc import Mapping

_TYPES = {1: "ProduceResourceType_ProduceCard", 2: "ProduceResourceType_ProduceItem",
          3: "ProduceResourceType_ProduceDrink"}


def _kind(value):
    return _TYPES.get(value, value)


def resolve_present_quantities(raw, rows):
    rows = tuple(rows)
    omitted = [row for row in rows if type(row.get("quantity")) is int and row["quantity"] == 0
               and _kind(row.get("resource_type")) in _TYPES.values()]
    if not omitted:
        return rows
    group = (raw.get("ui_state") or {}).get("reward_group")
    presents = (raw.get("collections") or {}).get("present")
    if not isinstance(group, Mapping) or not isinstance(presents, list):
        raise ValueError("omitted reward quantity requires its native panel and present collection")
    indexes = [row.get("index") for row in rows]
    if any(type(index) is not int for index in indexes) or sorted(indexes) != list(range(len(rows))):
        raise ValueError("present quantity requires the complete indexed candidate group")
    ordered = sorted(rows, key=lambda row: row["index"])
    matches = []
    for present in presents:
        if not isinstance(present, Mapping) or present.get("received", False) is not False:
            continue
        rewards = present.get("rewards")
        if not isinstance(rewards, list) or len(rewards) != len(ordered):
            continue
        matches_group = True
        for row, reward in zip(ordered, rewards):
            if not isinstance(reward, Mapping):
                matches_group = False
                break
            kind = _kind(row.get("resource_type"))
            card = kind == _TYPES[1]
            identity = row.get("card_id") if card else row.get("resource_id")
            if (not isinstance(identity, str) or not identity or kind != _kind(reward.get("resourceType"))
                    or identity != reward.get("resourceId")
                    or card and row.get("upgrade", 0) != reward.get("resourceLevel", 0)):
                matches_group = False
                break
        if matches_group:
            matches.append(present)
    if len(matches) != 1:
        raise ValueError("omitted reward quantity does not bind one complete native present group")
    present = matches[0]
    resolved = []
    for row in rows:
        if row not in omitted:
            resolved.append(row)
            continue
        quantity = present["rewards"][row["index"]].get("quantity", 0)
        if type(quantity) is not int or quantity < 1:
            raise ValueError("matched native present has no positive reward quantity")
        resolved.append({**row, "ui_quantity": row["quantity"], "quantity": quantity,
                         "quantity_source": {"source": "native collections.present.rewards",
                                             "position_number": present.get("positionNumber"),
                                             "reward_index": row["index"]}})
    return tuple(resolved)
