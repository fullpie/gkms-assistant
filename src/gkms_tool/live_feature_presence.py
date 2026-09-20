"""Bound live reference presence and counters for the existing feature encoder.

JsonUtility's zero-valued objects are not evidence of null references. This
adapter changes a copied model view only when the same native capture supplies
the exact field-presence witness. It neither changes raw observations nor
grants training qualification or game-input authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re

from .observed_empty_collections import JSON_SOURCE, _semantic_view
from .training_artifact_io import canonical_json_bytes
from .live_phase_counter_view import overlay_state_phase_counters, overlay_command_phase_counters

SCHEMA = "gkms.live-exam-feature-presence-view.v3"
REFERENCE_SCHEMA = "gkms.live-exam-reference-presence.v1"
COMMAND_SCHEMA = "gkms.live-exam-command-presence.v1"
PARAMETER_SCHEMA = "gkms.parameter-status-reference-sources.v1"
_PARAMETER_AUTHORITY = "actual parameter references before and after original SaveData/JsonUtility; list/type/UID joined to that exact serialized save"
_SEAL = object()
_STATUS_TYPES = {
    "_triggerCard": "Campus.InGame.Card.ExamCardData",
    "_triggerItem": "Campus.InGame.Item.ProduceItemData",
    "_triggerDrink": "Campus.InGame.Drink.ProduceDrinkData",
    "_triggerGimmickGroup": "Campus.InGame.ProduceGimmickEffectGroupData",
}
_COMMAND_TYPES = {
    "_playingCard": _STATUS_TYPES["_triggerCard"],
    "_playingItem": _STATUS_TYPES["_triggerItem"],
    "_playingDrink": _STATUS_TYPES["_triggerDrink"],
    "_playingGimmick": _STATUS_TYPES["_triggerGimmickGroup"],
}
_COUNTER_SOURCE = "ExamParameterModel.TotalEffectDrawCardCount"


def _require(value, reason):
    if not value:
        raise ValueError("Live feature presence: " + reason)


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _pointer(value):
    _require(isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value) is not None,
             "a hexadecimal native object identity is required")
    result = int(value, 16)
    _require(result > 0, "native owner identity is null")
    return result


def _int32(value):
    return type(value) is int and -(2**31) <= value < 2**31


@dataclass(frozen=True, slots=True, init=False)
class LiveFeatureView:
    """Immutable result whose JSON properties return independent plain copies."""

    _payload_json: bytes

    def __init__(self, payload, *, _authority=None):
        _require(_authority is _SEAL, "use prepare_live_feature_view")
        object.__setattr__(self, "_payload_json", canonical_json_bytes(payload))

    @property
    def state(self):
        return json.loads(self._payload_json)["state"]

    @property
    def current_context(self):
        return json.loads(self._payload_json)["current_context"]

    @property
    def ledger(self):
        return json.loads(self._payload_json)["ledger"]

    @property
    def provenance(self):
        return json.loads(self._payload_json)["provenance"]

    @property
    def raw_state_sha256(self):
        return self.provenance["raw_state_sha256"]

    @property
    def raw_current_context_sha256(self):
        return self.provenance["raw_current_context_sha256"]


def _reference_envelope(proof, evidence, save_key, native_hash):
    _require(isinstance(proof, Mapping) and proof.get("schema") == REFERENCE_SCHEMA
             and proof.get("complete") is True and proof.get("read_errors") == [],
             "complete current native reference presence is required")
    for key in ("sequence_id", "parameter_id"):
        _require(_pointer(proof.get(key)) == _pointer(evidence.get(key)), "stale reference " + key)
    _require(_pointer(proof.get("save_object_id")) == _pointer(evidence.get(save_key)),
             "stale reference " + save_key)
    _require(proof.get("state_native_sha256") == native_hash, "reference proof binds another native state")
    _parameter_sources(proof)
    return proof


def _parameter_sources(proof):
    """Prove the field witnesses belong to original parameter objects."""
    _require(proof.get("parameter_source_verified") is True and proof.get("authority") == _PARAMETER_AUTHORITY,
             "actual parameter reference authority is required; SaveData-only presence is insufficient")
    _pointer(proof.get("status_collection_id"))
    _pointer(proof.get("save_status_collection_id"))
    values = []
    for key in ("parameter_sources_before", "parameter_sources_after"):
        source = proof.get(key)
        _require(isinstance(source, Mapping) and source.get("schema") == PARAMETER_SCHEMA
                 and source.get("complete") is True and source.get("read_errors") == [],
                 "complete original parameter source capture is required")
        for identity in ("sequence_id", "parameter_id", "status_collection_id"):
            _require(_pointer(source.get(identity)) == _pointer(proof.get(identity)),
                     "original parameter source owner differs: " + identity)
        rows = source.get("active_status_sources")
        joined = proof.get("active_status_sources")
        _require(isinstance(rows, list) and isinstance(joined, list) and len(rows) == len(joined),
                 "original parameter source membership coverage differs")
        for original, row in zip(rows, joined):
            keys = ("status_index", "owner_object_id", "owner_uid", "owner_type", "fields")
            _require(isinstance(original, Mapping) and isinstance(row, Mapping)
                     and set(original) == set(keys) and all(name in row for name in keys)
                     and canonical_json_bytes(original) == canonical_json_bytes({name: row[name] for name in keys}),
                     "original parameter fields differ from the SaveData membership join")
        values.append(source)
    _require(canonical_json_bytes(values[0]) == canonical_json_bytes(values[1]),
             "original parameter references changed during SaveData/JsonUtility capture")


def _envelope(evidence):
    _require(isinstance(evidence, Mapping), "current capture evidence is missing")
    purity = evidence.get("purity")
    _require(isinstance(purity, Mapping) and all(purity.get(key) is True for key in
             ("state_equal", "owner_stable", "execution_master_stable", "reference_presence_stable",
              "live_counter_stable")), "capture purity is not established")
    native_hash = purity.get("before_native_sha256")
    _require(_sha(native_hash) and native_hash == purity.get("after_native_sha256")
             and purity.get("hash_encoding") == "nlohmann-json-dump", "native state purity hashes differ")
    proof = _reference_envelope(evidence.get("reference_presence"), evidence, "save_object_id", native_hash)
    after = _reference_envelope(evidence.get("reference_presence_after"), evidence,
                                "save_object_id_after", purity["after_native_sha256"])
    _require(canonical_json_bytes(proof["parameter_sources_before"])
             == canonical_json_bytes(after["parameter_sources_before"]),
             "original parameter reference identities changed across the pure observation")
    return proof, after, native_hash


def _fields(owner, rows, expected, *, path, binding, ledger, require_all=False):
    _require(isinstance(owner, Mapping) and isinstance(rows, list), "source fields are missing")
    names = set(owner) & set(expected)
    if require_all:
        _require(names == set(expected), "serialized source fields are incomplete")
    _require(all(isinstance(row, Mapping) and isinstance(row.get("name"), str) for row in rows),
             "source field witness shape differs")
    supplied = [row["name"] for row in rows]
    _require(len(supplied) == len(set(supplied)) and set(supplied) == names,
             "source field witness coverage differs")
    for row in rows:
        name = row["name"]
        _require(row.get("declared_type") == expected[name], "source field declared type differs: " + name)
        is_null = row.get("is_null")
        _require(type(is_null) is bool and "object_id" in row, "source field presence is not typed")
        if is_null:
            _require(row["object_id"] is None, "null source field has a nonnull object identity")
        else:
            _pointer(row["object_id"])
        original = owner[name]
        _require(isinstance(original, Mapping) or (original is None and is_null),
                 "serialized reference shape contradicts native presence: " + name)
        if is_null:
            owner[name] = None
        ledger.append({"path": path + "/" + name, "declared_type": row["declared_type"],
            "native_is_null": is_null, "native_object_id": row["object_id"],
            "raw_value": deepcopy(original), "semantic_view_changed": is_null and original is not None,
            "authority": "bound-native-reference-presence", **deepcopy(binding)})


def _status_view(state, proof, ledger):
    status, refs = state.get("status"), state.get("references")
    links = status.get("_effectList") if isinstance(status, Mapping) else None
    rows = refs.get("RefIds") if isinstance(refs, Mapping) else None
    witnesses = proof.get("active_status_sources")
    _require(isinstance(links, list) and isinstance(rows, list) and isinstance(witnesses, list)
             and len(witnesses) == len(links), "active status proof coverage differs")
    by_rid = {}
    for index, row in enumerate(rows):
        if isinstance(row, Mapping) and type(row.get("rid")) is int:
            by_rid.setdefault(row["rid"], []).append(index)
    seen_rids, seen_owners, seen_save_owners = set(), set(), set()
    for index, (link, witness) in enumerate(zip(links, witnesses)):
        _require(isinstance(link, Mapping) and set(link) == {"rid"} and type(link["rid"]) is int
                 and link["rid"] >= 0 and link["rid"] not in seen_rids, "active status membership is ambiguous")
        rid = link["rid"]
        seen_rids.add(rid)
        matches = by_rid.get(rid, ())
        _require(len(matches) == 1 and isinstance(witness, Mapping), "active status rid is missing or ambiguous")
        ref_index = matches[0]
        _require(type(witness.get("status_index")) is int and witness["status_index"] == index
                 and type(witness.get("rid")) is int and witness["rid"] == rid
                 and type(witness.get("reference_index")) is int and witness["reference_index"] == ref_index,
                 "active status index/rid/reference binding differs")
        owner_id = _pointer(witness.get("owner_object_id"))
        _require(owner_id not in seen_owners, "active status owner is repeated")
        seen_owners.add(owner_id)
        save_owner_id = _pointer(witness.get("save_owner_object_id"))
        _require(save_owner_id not in seen_save_owners, "cloned SaveData status owner is repeated")
        seen_save_owners.add(save_owner_id)
        row = rows[ref_index]
        kind, data = row.get("type"), row.get("data")
        _require(isinstance(kind, Mapping) and set(kind) == {"asm", "ns", "class"}
                 and kind.get("asm") == "Assembly-CSharp" and kind.get("ns") == "Campus.InGame.Exam"
                 and isinstance(kind.get("class"), str) and bool(kind["class"])
                 and witness.get("owner_type") == kind, "native status owner type differs")
        _require(isinstance(data, Mapping) and _int32(data.get("_uid"))
                 and _int32(witness.get("owner_uid")) and data["_uid"] == witness["owner_uid"],
                 "native status owner UID differs")
        _fields(deepcopy(data), witness.get("save_fields"), _STATUS_TYPES,
                path=f"/references/RefIds/{ref_index}/data", binding={}, ledger=[],
                require_all=kind["class"] == "TriggerEffectStatusEffect")
        _require(isinstance(witness.get("fields"), list) and all(isinstance(field, Mapping) for field in witness["fields"])
                 and [(field.get("name"), field.get("declared_type")) for field in witness["fields"]]
                 == [(field["name"], field["declared_type"]) for field in witness["save_fields"]],
                 "parameter and SaveData source field declarations differ")
        ledger_start = len(ledger)
        _fields(data, witness.get("fields"), _STATUS_TYPES, path=f"/references/RefIds/{ref_index}/data",
                binding={"status_index": index, "rid": rid, "reference_index": ref_index,
                         "owner_object_id": witness["owner_object_id"], "owner_uid": witness["owner_uid"],
                         "save_owner_object_id": witness["save_owner_object_id"],
                         "reference_source_authority": "actual-parameter-StatusEffectCollection"},
                ledger=ledger, require_all=kind["class"] == "TriggerEffectStatusEffect")
        for entry, saved in zip(ledger[ledger_start:], witness["save_fields"]):
            entry["save_native_is_null"] = saved["is_null"]
            entry["save_native_object_id"] = saved["object_id"]


def _counter_view(state, evidence, ledger):
    counters = evidence.get("live_counters")
    count = counters.get("totalDrawCardCount") if isinstance(counters, Mapping) else None
    _require(isinstance(count, Mapping) and count.get("source") == _COUNTER_SOURCE,
             "actual TotalEffectDrawCardCount witness is required")
    _require(_pointer(count.get("parameter_id")) == _pointer(evidence.get("parameter_id")),
             "live draw counter parameter owner differs")
    values = [count.get(key) for key in ("value", "before", "after")]
    _require(all(_int32(value) and value >= 0 for value in values) and len(set(values)) == 1,
             "live draw counter is invalid or changed during capture")
    _require("totalDrawCardCount" in state and _int32(state["totalDrawCardCount"])
             and state["totalDrawCardCount"] >= 0, "serialized draw counter shape differs")
    original = state["totalDrawCardCount"]
    state["totalDrawCardCount"] = values[0]
    ledger.append({"path": "/totalDrawCardCount", "raw_value": original, "semantic_value": values[0],
        "semantic_view_changed": original != values[0], "authority": _COUNTER_SOURCE,
        "parameter_id": evidence["parameter_id"], "native_before": count["before"], "native_after": count["after"]})


def _field_semantics(fields):
    return sorted(({key: field[key] for key in ("name", "declared_type", "is_null")}
                   for field in fields), key=lambda field: field["name"])


def _presence_semantics(proof):
    """Allocation addresses may change when each save object is constructed."""
    return [{**{key: row[key] for key in ("status_index", "rid", "reference_index", "owner_type", "owner_uid")},
             "fields": _field_semantics(row["fields"]), "save_fields": _field_semantics(row["save_fields"])}
            for row in proof["active_status_sources"]]


def _command_view(context, evidence, empty_contract, ledger):
    _require(isinstance(context, Mapping) and isinstance(context.get("command"), Mapping),
             "current command reference domain is missing")
    command_hash = evidence.get("command_native_sha256")
    after_hash = evidence.get("command_native_sha256_after")
    _require(_sha(command_hash) and _sha(after_hash) and command_hash == after_hash,
             "command native hashes are absent or changed during capture")
    proofs = []
    for key, expected_hash in (("command_presence", command_hash), ("command_presence_after", after_hash)):
        proof = evidence.get(key)
        _require(isinstance(proof, Mapping) and proof.get("schema") == COMMAND_SCHEMA
                 and proof.get("complete") is True and proof.get("read_errors") == [],
                 "complete current command presence is required: " + key)
        _require(_pointer(proof.get("command_id")) == _pointer(evidence.get("command_id")),
                 "command owner differs from its presence proof")
        _require(proof.get("command_native_sha256") == expected_hash,
                 "command presence native hash differs")
        proofs.append(proof)
    command = context["command"]
    raw_command = deepcopy(command)
    _fields(deepcopy(command), proofs[1].get("fields"), _COMMAND_TYPES, path="/command",
            binding={"command_id": evidence["command_id"], "command_native_sha256": command_hash},
            ledger=[], require_all=True)
    _fields(command, proofs[0].get("fields"), _COMMAND_TYPES, path="/command",
            binding={"command_id": evidence["command_id"], "command_native_sha256": command_hash},
            ledger=ledger, require_all=True)
    _require(_field_semantics(proofs[0]["fields"]) == _field_semantics(proofs[1]["fields"]),
             "command reference presence changed across the pure observation")
    if isinstance(command["_playingCard"], Mapping):
        # Normalize only this actual parent object. The carrier is never a
        # state or a candidate pool and never crosses the command RID domain.
        carrier, card_ledger = _semantic_view({"handList": [command["_playingCard"]]}, JSON_SOURCE,
            inactive_status_contract=empty_contract.get("inactive_card_status_contract"),
            optional_identifiers_contract=empty_contract.get("optional_effect_identifiers_contract"))
        command["_playingCard"] = carrier["handList"][0]
        for entry in card_ledger:
            entry = deepcopy(entry)
            _require(entry["path"].startswith("/handList/0/"), "command parent view has an unexpected owner")
            entry["path"] = "/command/_playingCard" + entry["path"][len("/handList/0"):]
            ledger.append(entry)
    return overlay_command_phase_counters(raw_command, command, evidence=evidence, ledger=ledger)


def prepare_live_feature_view(raw_state, *, evidence, current_context=None, empty_contract):
    """Validate same-capture witnesses, then return a separate immutable view."""
    _require(isinstance(raw_state, Mapping) and isinstance(empty_contract, Mapping),
             "raw state and explicit empty-collection contract are required")
    proof, after_proof, native_hash = _envelope(evidence)
    state, ledger = _semantic_view(deepcopy(raw_state), JSON_SOURCE,
        inactive_status_contract=empty_contract.get("inactive_card_status_contract"),
        optional_identifiers_contract=empty_contract.get("optional_effect_identifiers_contract"))
    ledger = list(ledger)
    _status_view(state, proof, ledger)
    _status_view(deepcopy(raw_state), after_proof, [])
    _require(_presence_semantics(proof) == _presence_semantics(after_proof),
             "reference presence changed across the pure observation")
    _counter_view(state, evidence, ledger)
    phase_provenance = overlay_state_phase_counters(raw_state, state, evidence=evidence, ledger=ledger)
    context = deepcopy(current_context)
    command_phase_provenance = None
    if context is not None:
        command_phase_provenance = _command_view(context, evidence, empty_contract, ledger)
    else:
        _require(evidence.get("command_presence") is None and evidence.get("command_presence_after") is None,
                 "command proof has no current command domain")
    provenance = {"schema": SCHEMA, "raw_state_sha256": _digest(raw_state),
        "raw_current_context_sha256": None if current_context is None else _digest(current_context),
        "semantic_state_sha256": _digest(state),
        "semantic_current_context_sha256": None if context is None else _digest(context),
        "reference_presence_sha256": _digest(proof), "reference_presence_after_sha256": _digest(after_proof),
        "reference_presence_stable": True, "evidence_sha256": _digest(evidence),
        "parameter_source_verified": True, "parameter_reference_identities_stable": True,
        "parameter_source_authority": _PARAMETER_AUTHORITY,
        "phase_counter_view": phase_provenance,
        "command_phase_counter_view": command_phase_provenance,
        "parameter_sources_sha256": _digest(proof["parameter_sources_before"]),
        "empty_contract_sha256": _digest(empty_contract), "ledger_sha256": _digest(ledger),
        "sequence_id": evidence["sequence_id"], "parameter_id": evidence["parameter_id"],
        "save_object_id": evidence["save_object_id"], "save_object_id_after": evidence["save_object_id_after"],
        "state_native_sha256": native_hash,
        "command_id": None if context is None else evidence["command_id"],
        "command_native_sha256": None if context is None else evidence["command_native_sha256"],
        "command_presence_stable": None if context is None else True,
        "command_presence_sha256": None if context is None else _digest(evidence["command_presence"]),
        "command_presence_after_sha256": None if context is None else _digest(evidence["command_presence_after"]),
        "raw_observation_modified": False, "training_admitted": False, "game_io": False,
        "native_input_authority_granted": False, "new_native_evidence_created": False}
    return LiveFeatureView({"state": state, "current_context": context, "ledger": ledger,
                            "provenance": provenance}, _authority=_SEAL)


__all__ = ["SCHEMA", "LiveFeatureView", "prepare_live_feature_view"]
