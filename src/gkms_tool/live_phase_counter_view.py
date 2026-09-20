"""Restore only witnessed missing Nullable phase fields in a feature copy.

The original JsonUtility payload is retained. Every value comes from stable
typed native storage, including the latent Int32 when hasValue is false.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import re

from .training_artifact_io import canonical_json_bytes

SCHEMA = "gkms.live-phase-counter-feature-overlay.v1"
CAPTURE_SCHEMA = "gkms.live-card-phase-counters.v1"
ZONES = ("handList", "deckList", "graveList", "lostList", "holdList")
_MISSING = object()


def _require(value, reason):
    if not value:
        raise ValueError("Live phase counters: " + reason)


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _pointer(value):
    _require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value) is not None
             and int(value, 16) > 0, "nonnull hexadecimal native owner required")
    return int(value, 16)


def _int32(value):
    return type(value) is int and -(2**31) <= value < 2**31


def _nullable(value):
    _require(isinstance(value, Mapping) and set(value) == {"hasValue", "value"}
             and type(value["hasValue"]) is bool and _int32(value["value"]),
             "forecastStart is not exact Boolean/Int32 nullable storage")


def _layout(value):
    names = {"type", "value_size", "header_size", "has_value_offset", "value_offset"}
    _require(isinstance(value, Mapping) and set(value) == names
             and value["type"] == "System.Nullable<System.Int32>"
             and all(type(value[key]) is int for key in names - {"type"}), "closed Nullable<Int32> layout required")
    size, header = value["value_size"], value["header_size"]
    has, number = value["has_value_offset"] - header, value["value_offset"] - header
    _require(0 < size <= 64 and 16 <= header <= 256 and 0 <= has < size
             and 0 <= number <= size - 4 and not number <= has < number + 4,
             "nullable member layout is outside the native storage bounds")


def _capture(value, *, scope, owner):
    _require(isinstance(value, Mapping) and value.get("schema") == CAPTURE_SCHEMA
             and value.get("scope") == scope and value.get("complete") is True
             and value.get("read_errors") == [] and isinstance(value.get("cards"), list)
             and isinstance(value.get("active_statuses"), list),
             "complete scoped native phase capture required")
    _require(_pointer(value.get("owner_id")) == _pointer(owner), "phase capture owner differs")
    return value


def _captures(evidence, scope):
    _require(isinstance(evidence, Mapping), "capture evidence is missing")
    purity = evidence.get("purity")
    _require(isinstance(purity, Mapping) and all(purity.get(key) is True for key in
             ("state_equal", "owner_stable", "execution_master_stable")), "native capture purity is not established")
    owner = evidence.get("sequence_id" if scope == "state" else "command_id")
    output = []
    for name in (("reference_presence", "reference_presence_after") if scope == "state"
                 else ("command_presence", "command_presence_after")):
        parent = evidence.get(name)
        _require(isinstance(parent, Mapping) and parent.get("complete") is True and parent.get("read_errors") == [],
                 "parent presence capture is incomplete")
        keys = ("card_phase_counters_before", "card_phase_counters_after") if scope == "state" else ("card_phase_counters",)
        for key in keys:
            output.append(_capture(parent.get(key), scope=scope, owner=owner))
    first = canonical_json_bytes(output[0])
    _require(all(canonical_json_bytes(value) == first for value in output[1:]),
             "original phase owner pointers or nullable storage changed during capture")
    return output, owner


def _phase_rows(status):
    if status is None:
        return []
    _require(isinstance(status, Mapping), "card status is not an object")
    phases = status.get("_phaseCountDictionary")
    if phases is None:
        return []
    _require(isinstance(phases, Mapping) and set(phases) == {"_list"} and isinstance(phases["_list"], list),
             "serialized phase dictionary shape differs")
    return phases["_list"]


def _identity(card, witness, *, zone, index):
    _require(isinstance(card, Mapping) and isinstance(witness, Mapping)
             and witness.get("zone") == zone and type(witness.get("index")) is int and witness["index"] == index,
             "card zone/index binding differs")
    _pointer(witness.get("card_object_id"))
    data = card.get("_cardData")
    _require("_guid" in card and (card["_guid"] is None or isinstance(card["_guid"], str))
             and "card_guid" in witness and witness["card_guid"] == card["_guid"]
             and isinstance(data, Mapping) and isinstance(data.get("_id"), str)
             and witness.get("card_id") == data["_id"] and _int32(data.get("_upgradeCount"))
             and _int32(witness.get("card_upgrade")) and witness["card_upgrade"] == data["_upgradeCount"],
             "card GUID/definition identity differs")
    _require("_statusEffect" in card, "raw card status field is missing")


def _card(raw, feature, witness, *, zone, index, path, ledger):
    _identity(raw, witness, zone=zone, index=index)
    _identity(feature, witness, zone=zone, index=index)
    entries = witness.get("entries")
    _require(isinstance(entries, list), "phase entry list is missing")
    raw_status, feature_status = raw["_statusEffect"], feature["_statusEffect"]
    raw_rows, feature_rows = _phase_rows(raw_status), _phase_rows(feature_status)
    _require("status_object_id" in witness and "status_id" in witness, "native card status presence is missing")
    if witness["status_object_id"] is None:
        _require(witness["status_id"] is None and not entries and not raw_rows
                 and (feature_status is None or feature_status == {}),
                 "null native card status conflicts with serialized phase data")
        return 0
    _pointer(witness["status_object_id"])
    _require(isinstance(raw_status, Mapping) and isinstance(raw_status.get("_id"), str)
             and witness["status_id"] == raw_status["_id"] and isinstance(feature_status, Mapping),
             "native status identity differs from the raw card")
    _require((not entries and feature_status == {}) or feature_status.get("_id") == witness["status_id"],
             "feature status identity differs")
    return _entries(raw_rows, feature_rows, entries, path=path + "/_statusEffect",
                    binding={"card_object_id": witness["card_object_id"], "status_object_id": witness["status_object_id"]},
                    ledger=ledger)


def _entries(raw_rows, feature_rows, entries, *, path, binding, ledger):
    _require(isinstance(entries, list) and len(entries) == len(raw_rows) == len(feature_rows),
             "phase entry coverage differs")
    seen = set()
    for ordinal, (entry, raw_entry, feature_entry) in enumerate(zip(entries, raw_rows, feature_rows)):
        _require(isinstance(entry, Mapping) and type(entry.get("ordinal")) is int and entry["ordinal"] == ordinal
                 and _int32(entry.get("key")) and entry["key"] not in seen and _int32(entry.get("current")),
                 "phase ordinal/key/current is invalid or repeated")
        seen.add(entry["key"])
        _pointer(entry.get("phase_object_id"))
        _nullable(entry.get("forecastStart"))
        _layout(entry.get("nullable_storage"))
        for value in (raw_entry, feature_entry):
            _require(isinstance(value, Mapping) and set(value) == {"key", "value"}
                     and _int32(value["key"]) and value["key"] == entry["key"]
                     and isinstance(value["value"], Mapping) and set(value["value"]) <= {"current", "forecastStart"}
                     and _int32(value["value"].get("current")) and value["value"]["current"] == entry["current"],
                     "phase key/current binding differs")
            existing = value["value"].get("forecastStart", _MISSING)
            if existing is not _MISSING:
                _nullable(existing)
                _require(canonical_json_bytes(existing) == canonical_json_bytes(entry["forecastStart"]),
                         "existing forecastStart conflicts with native storage")
        present = "forecastStart" in raw_entry["value"]
        changed = "forecastStart" not in feature_entry["value"]
        if changed:
            feature_entry["value"]["forecastStart"] = deepcopy(entry["forecastStart"])
        ledger.append({"path": path + f"/_phaseCountDictionary/_list/{ordinal}/value/forecastStart",
            "authority": "stable-typed-native-Nullable<Int32>-storage", "raw_field_present": present,
            "raw_value": deepcopy(raw_entry["value"].get("forecastStart")),
            "semantic_value": deepcopy(entry["forecastStart"]), "semantic_view_changed": changed,
            "phase_object_id": entry["phase_object_id"], "phase_key": entry["key"], "current": entry["current"],
            "nullable_storage": deepcopy(entry["nullable_storage"]), **deepcopy(binding)})
    return len(entries)


def _active_statuses(raw, feature, capture, evidence, ledger):
    raw_status, raw_refs = raw.get("status"), raw.get("references")
    feature_status, feature_refs = feature.get("status"), feature.get("references")
    _require(isinstance(raw_status, Mapping) and isinstance(raw_refs, Mapping)
             and isinstance(feature_status, Mapping) and isinstance(feature_refs, Mapping)
             and isinstance(raw_status.get("_effectList"), list) and isinstance(raw_refs.get("RefIds"), list)
             and isinstance(feature_refs.get("RefIds"), list), "active status reference domains required")
    links, rows = raw_status["_effectList"], raw_refs["RefIds"]
    _require(feature_status.get("_effectList") == links and len(feature_refs["RefIds"]) == len(rows),
             "feature active status membership differs")
    active = capture["active_statuses"]
    _require(len(active) == len(links), "active status phase coverage differs")
    total, seen = 0, set()
    for index, (link, witness) in enumerate(zip(links, active)):
        _require(isinstance(link, Mapping) and set(link) == {"rid"} and _int32(link["rid"])
                 and link["rid"] >= 0 and link["rid"] not in seen and isinstance(witness, Mapping),
                 "active status rid membership is invalid or repeated")
        seen.add(link["rid"])
        matches = [position for position, row in enumerate(rows) if isinstance(row, Mapping)
                   and type(row.get("rid")) is int and row["rid"] == link["rid"]]
        _require(len(matches) == 1, "active status reference is absent or ambiguous")
        position = matches[0]
        row, projected = rows[position], feature_refs["RefIds"][position]
        _require(type(witness.get("status_index")) is int and witness["status_index"] == index
                 and _int32(witness.get("owner_uid")) and isinstance(witness.get("owner_type"), Mapping)
                 and row.get("type") == witness["owner_type"] and isinstance(row.get("data"), Mapping)
                 and _int32(row["data"].get("_uid")) and row["data"]["_uid"] == witness["owner_uid"],
                 "active status type/UID/index differs from native phase owner")
        _pointer(witness.get("owner_object_id"))
        _require(isinstance(projected, Mapping) and projected.get("rid") == link["rid"]
                 and projected.get("type") == row["type"] and isinstance(projected.get("data"), Mapping)
                 and _int32(projected["data"].get("_uid")) and projected["data"]["_uid"] == row["data"]["_uid"],
                 "feature active owner identity differs")
        keys = ("status_index", "owner_object_id", "owner_uid", "owner_type")
        for parent_name in ("reference_presence", "reference_presence_after"):
            parent = evidence[parent_name]
            source = parent.get("parameter_sources_before")
            original = source.get("active_status_sources") if isinstance(source, Mapping) else None
            joined = parent.get("active_status_sources")
            _require(isinstance(original, list) and isinstance(joined, list)
                     and len(original) == len(joined) == len(active)
                     and isinstance(original[index], Mapping) and isinstance(joined[index], Mapping)
                     and all(key in original[index] and key in joined[index] for key in keys)
                     and canonical_json_bytes({key: witness[key] for key in keys})
                     == canonical_json_bytes({key: original[index][key] for key in keys})
                     == canonical_json_bytes({key: joined[index][key] for key in keys})
                     and type(joined[index].get("rid")) is int and joined[index]["rid"] == link["rid"]
                     and type(joined[index].get("reference_index")) is int and joined[index]["reference_index"] == position,
                     "phase status owner differs from the original parameter/reference join")
        declared = witness.get("has_phase_count_dictionary")
        _require(type(declared) is bool and isinstance(witness.get("entries"), list),
                 "active phase dictionary presence is not typed")
        raw_data, feature_data = row["data"], projected["data"]
        if not declared:
            _require("_phaseCountDictionary" not in raw_data and "_phaseCountDictionary" not in feature_data
                     and witness["entries"] == [], "undeclared active phase dictionary has data")
            continue
        _require("_phaseCountDictionary" in raw_data and "_phaseCountDictionary" in feature_data,
                 "declared active phase dictionary is missing from raw or feature view")
        total += _entries(_phase_rows(raw_data), _phase_rows(feature_data), witness["entries"],
            path=f"/references/RefIds/{position}/data",
            binding={"status_index": index, "rid": link["rid"], "reference_index": position,
                     "owner_object_id": witness["owner_object_id"], "owner_uid": witness["owner_uid"]}, ledger=ledger)
    return total


def _finish(raw, feature, working, ledger, additions, captures, owner, scope, entries):
    _require(isinstance(ledger, list), "mutable independent projection ledger required")
    feature.clear()
    feature.update(working)
    ledger.extend(additions)
    return {"schema": SCHEMA, "scope": scope, "capture_schema": CAPTURE_SCHEMA, "owner_id": owner,
        "raw_object_sha256": _digest(raw), "feature_object_sha256": _digest(working),
        "capture_sha256": [_digest(value) for value in captures], "captures_verified": len(captures),
        "cards_verified": len(captures[0]["cards"]), "entries_verified": entries,
        "active_statuses_verified": len(captures[0]["active_statuses"]),
        "forecast_fields_added": sum(row["semantic_view_changed"] for row in additions),
        "original_pointer_and_storage_identity_stable": True, "only_missing_forecast_fields_added": True,
        "latent_nullable_value_preserved": True, "raw_object_modified": False,
        "training_admitted": False, "game_io": False}


def overlay_state_phase_counters(raw_state, feature_state, *, evidence, ledger):
    """Validate four native captures, then fill missing fields atomically."""
    _require(isinstance(raw_state, Mapping) and type(feature_state) is dict and raw_state is not feature_state,
             "separate raw and mutable feature state objects required")
    captures, owner = _captures(evidence, "state")
    working = deepcopy(feature_state)
    expected = []
    for zone in ZONES:
        _require(isinstance(raw_state.get(zone), list) and isinstance(working.get(zone), list)
                 and len(raw_state[zone]) == len(working[zone]), "complete five-zone card coverage required")
        expected.extend((zone, index) for index in range(len(raw_state[zone])))
    rows = captures[0]["cards"]
    _require(len(rows) == len(expected), "native phase capture card denominator differs")
    additions, entries = [], 0
    for row, (zone, index) in zip(rows, expected):
        entries += _card(raw_state[zone][index], working[zone][index], row,
                         zone=zone, index=index, path=f"/{zone}/{index}", ledger=additions)
    entries += _active_statuses(raw_state, working, captures[0], evidence, additions)
    return _finish(raw_state, feature_state, working, ledger, additions, captures, owner, "state", entries)


def overlay_command_phase_counters(raw_command, feature_command, *, evidence, ledger):
    """Bind the actual current playing-card owner; never infer null from JSON."""
    _require(isinstance(raw_command, Mapping) and type(feature_command) is dict and raw_command is not feature_command
             and "_playingCard" in raw_command and "_playingCard" in feature_command,
             "separate raw and feature command objects with playing-card presence required")
    captures, owner = _captures(evidence, "command")
    _require(captures[0]["active_statuses"] == [], "command phase capture cannot contain state active statuses")
    witnesses = []
    for name in ("command_presence", "command_presence_after"):
        fields = evidence[name].get("fields")
        _require(isinstance(fields, list), "typed command source presence required")
        matches = [row for row in fields if isinstance(row, Mapping) and row.get("name") == "_playingCard"]
        _require(len(matches) == 1 and matches[0].get("declared_type") == "Campus.InGame.Card.ExamCardData"
                 and type(matches[0].get("is_null")) is bool and "object_id" in matches[0],
                 "exact playing-card source presence required")
        witnesses.append(matches[0])
    _require(canonical_json_bytes(witnesses[0]) == canonical_json_bytes(witnesses[1]),
             "playing-card source pointer changed during capture")
    source = witnesses[0]
    working, additions, entries = deepcopy(feature_command), [], 0
    cards = captures[0]["cards"]
    if source["is_null"]:
        _require(source["object_id"] is None and working["_playingCard"] is None and cards == [],
                 "null playing-card witness conflicts with feature/counter presence")
    else:
        pointer = _pointer(source["object_id"])
        _require(len(cards) == 1 and isinstance(cards[0], Mapping)
                 and _pointer(cards[0].get("card_object_id")) == pointer,
                 "command phase card does not match the actual playing-card pointer")
        entries = _card(raw_command["_playingCard"], working["_playingCard"], cards[0],
                        zone="command._playingCard", index=0, path="/command/_playingCard", ledger=additions)
    return _finish(raw_command, feature_command, working, ledger, additions, captures, owner, "command", entries)


__all__ = ["SCHEMA", "CAPTURE_SCHEMA", "overlay_state_phase_counters", "overlay_command_phase_counters"]
