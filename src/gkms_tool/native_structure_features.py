"""Source-bound structural card features; the original PC owns arithmetic.

This opt-in wrapper keeps the old catalog/codec intact.  It describes base
graphs, selected PT rules and observed materialized rows separately; it never
replays lineage, computes effective effects, prices an action or creates an
after-state.  A verified package is loaded once per source Master, not per row.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import struct
from types import MappingProxyType

from .card_semantic_features import CardSemanticFeatures, FEATURE_SCHEMA, _action_window, number_tokens
from .training_artifact_io import canonical_json_bytes, sha256_file

SCHEMA = "gkms.native-structure-card-representation.v1"
_INVENTORY_SHA = "24f164de28bea7f20efdb2647e07fc51a9333bbd2cf3d9a306165fbc518b5264"
_PROOF_SHA = "b8b8d626b94cadfcab847c30326fc6e960f8755eb88f7a25fca9cd3a6caf00b8"
_ENUM_SHA = "600422facdc51bbaef69fb8e238431104358fabc9f93d65a12134416dcae259f"
_SEAL = object()
ZONES = ("handList", "deckList", "graveList", "lostList", "holdList")
GROW_FIELDS = ("_id", "_effectType", "_value", "_playProduceExamTriggerId",
    "_playEffectProduceExamTriggerId", "_targetPlayEffectProduceExamTriggerIdList",
    "_playProduceExamEffectId", "_targetPlayProduceExamEffectIdList",
    "_produceCardStatusEnchantId", "_playMovePositionType")
_RUNTIME_FIELDS = ("_fixedDeckOrder", "_baseUpgradeCount", "_tmpUpgradeCount",
    "_supportUpgradeIdList", "_statusEffect", "_growEffectExamStartAfterList",
    "_affectGrowEffectIdList", "_playCount", "_isMoveProduceExamEffectUseInTurn",
    "_staminaConsumptionSpecifyEffectList")
_DISPLAY = frozenset({"produceDescriptions", "customizeProduceDescriptions", "name",
    "assetId", "voiceAssetId", "_descriptionList", "_descriptionReactiveDataTextList", "_descriptionReactiveDataTypeList"})
_LOCAL_IDS = frozenset({"_guid", "_uid", "rid"})
# Only declared definition references are expanded. effectGroupIds and target
# filters are symbolic selectors, not stand-alone Master records.
_REFS = {
    "produceExamEffectId": "effect", "playProduceExamEffectId": "effect",
    "chainProduceExamEffectId": "effect", "chainProduceExamEffectIds": "effect",
    "produceExamEffectIds": "effect", "moveProduceExamEffectIds": "effect",
    "produceExamTriggerId": "produce_exam_trigger", "playProduceExamTriggerId": "produce_exam_trigger",
    "playEffectProduceExamTriggerId": "produce_exam_trigger", "moveProduceExamTriggerIds": "produce_exam_trigger",
    "produceCardSearchId": "produce_card_search", "produceCardSearchId2": "produce_card_search",
    "pickCountReferenceProduceCardSearchId": "produce_card_search", "pickCountReferenceProduceCardSearchId2": "produce_card_search",
    "fieldStatusProduceCardSearchIds": "produce_card_search",
    "produceExamStatusEnchantId": "produce_exam_status_enchant",
    "produceCardStatusEnchantId": "produce_card_status_enchant",
    "produceCardGrowEffectIds": "produce_card_grow_effect",
    "produceDrinkEffectIds": "produce_drink_effect", "produceEffectId": "produce_effect",
    "produceItemEffectIds": "produce_item_effect",
}
_RUNTIME_REFS = {"_playProduceExamTriggerId": "produce_exam_trigger",
    "_playEffectProduceExamTriggerId": "produce_exam_trigger", "_playProduceExamEffectId": "effect",
    "_produceCardStatusEnchantId": "produce_card_status_enchant"}
_ACTIVE_REFS = {"_searchId": "produce_card_search", "_cardSearchId": "produce_card_search",
    "_cardSearchId2": "produce_card_search", "_pickCountReferenceProduceCardSearchId": "produce_card_search",
    "_pickCountReferenceProduceCardSearchId2": "produce_card_search", "_chainEffectId": "effect",
    "_chainEffectIdList": "effect", "_effectId": "effect", "_statusEnchantId": "produce_exam_status_enchant",
    "_cardStatusEnchantId": "produce_card_status_enchant", "_cardGrowEffectIdList": "produce_card_grow_effect"}


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_plain(v) for v in value]
    return value


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(_plain(value))).hexdigest()


def _reference(reference, *, expected=None, source_resolver=None):
    if not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str):
        raise ValueError("Native structure source requires a path/SHA reference")
    path = Path(reference["path"]).resolve()
    logical_path = path
    sha = reference.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64 or (expected and sha != expected):
        raise ValueError("Native structure evidence authority SHA differs")
    if source_resolver is not None:
        resolved = source_resolver.resolve(reference, expected_sha256=expected)
        if resolved.logical_reference != dict(reference):
            raise ValueError("Source resolver changed the original logical reference")
        path = resolved.physical_path
    if sha256_file(path) != sha:
        raise ValueError("Native structure source file SHA differs")
    return path, {"path": str(logical_path), "sha256": sha}


@lru_cache(maxsize=2)
def _native_schema(proof_path: str, proof_sha: str, metadata_stamp: tuple, *, source_resolver=None):
    """Cache only immutable schema evidence, never caller-provided raw states."""
    path, proof_ref = _reference({"path": proof_path, "sha256": proof_sha}, expected=_PROOF_SHA, source_resolver=source_resolver)
    proof = json.loads(path.read_text(encoding="utf-8"))
    if tuple(proof["raw_runtime_field_names"]) != GROW_FIELDS:
        raise ValueError("Native grow field contract differs")
    metadata_path, _ = _reference(proof["source_metadata"], source_resolver=source_resolver)
    _reference(proof["source_image"], source_resolver=source_resolver)
    enum_path, enum_ref = _reference({"path": str(path.parent / proof["evidence"]["grow_enum"]), "sha256": _ENUM_SHA}, source_resolver=source_resolver)
    frozen_enum = json.loads(enum_path.read_text(encoding="utf-8"))
    from .il2cpp_metadata import Il2CppMetadataV31
    metadata = Il2CppMetadataV31.load(metadata_path)
    defaults = {v[0]: v for _, v in metadata._records("field_default_values", struct.Struct("<iii"))}
    blob = metadata._section_bytes("field_and_parameter_default_value_data")
    numeric, symbolic = {}, {}
    for owner in metadata.types:
        if owner.namespace != "Campus.Common.Proto.Client.Enums":
            continue
        fields = metadata.fields[owner.first_field_index:owner.first_field_index + owner.field_count]
        symbolic[owner.name] = frozenset(owner.name + "_" + f.name for f in fields if f.name != "value__")
        if owner.name not in ("ProduceCardGrowEffectType", "ProduceCardMovePositionType"):
            continue
        members = {}
        for field in fields:
            if field.name == "value__":
                continue
            record = defaults.get(field.index)
            if record is None or record[2] < 0:
                raise ValueError("Native enum default missing")
            encoded = blob[record[2]]
            if encoded & 0x81:
                raise ValueError("Native enum is outside the verified nonnegative one-byte encoding")
            members[encoded >> 1] = field.name
        numeric[owner.name] = members
    if numeric["ProduceCardGrowEffectType"] != {x["value"]: x["name"] for x in frozen_enum["enum"]}:
        raise ValueError("Native metadata and frozen enum proof differ")
    return _freeze({"numeric": numeric, "symbolic": symbolic, "proof": proof_ref,
        "grow_enum": enum_ref, "source_image": proof["source_image"], "source_metadata": proof["source_metadata"]})


@dataclass(frozen=True, slots=True, init=False)
class NativeStructurePackage:
    payload: Mapping
    snapshot: Mapping
    native_schema: Mapping
    supplemental_tables: Mapping
    contract: Mapping

    def __init__(self, payload, snapshot, native_schema, contract, *, supplemental_tables=None, _authority=None):
        if _authority is not _SEAL:
            raise ValueError("Use load_native_structure_package for source verification")
        object.__setattr__(self, "payload", _freeze(payload))
        object.__setattr__(self, "snapshot", _freeze(snapshot))
        object.__setattr__(self, "native_schema", native_schema)
        object.__setattr__(self, "supplemental_tables", _freeze(supplemental_tables or {}))
        object.__setattr__(self, "contract", _freeze(contract))


def load_native_structure_package(*, source_master_hash, source_inventory_reference,
        native_schema_reference, catalog_reference=None, snapshot_reference=None, source_resolver=None) -> NativeStructurePackage:
    """Verify the retained four-Master inventory and exact original PC schema.

    The fixed evidence authorities are deliberately explicit. New inventories
    or native versions need a reviewed authority update, not a self-asserted
    caller report. Relocated identical files may be supplied as references.
    """
    inventory_path, inventory_ref = _reference(source_inventory_reference, expected=_INVENTORY_SHA, source_resolver=source_resolver)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    matches = [row for row in inventory["versions"] if row["master_hash"] == source_master_hash]
    if len(matches) != 1:
        raise ValueError("Source Master is outside the verified structural inventory")
    row = matches[0]
    selected = []
    for supplied, key in ((catalog_reference, "feature_payload"), (snapshot_reference, "snapshot")):
        if supplied is not None and supplied.get("sha256") != row[key]["sha256"]:
            raise ValueError("Source Master catalog/snapshot binding differs")
        selected.append(_reference(row[key] if supplied is None else supplied, source_resolver=source_resolver))
    (catalog_path, catalog_ref), (snapshot_path, snapshot_ref) = selected
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if (payload["schema"] != FEATURE_SCHEMA or payload["snapshot_fingerprint"] != row["snapshot_fingerprint"]
            or snapshot["fingerprint"] != row["snapshot_fingerprint"]):
        raise ValueError("Source structural catalog fingerprint differs")
    # This retained source table was not part of the legacy snapshot. Add it
    # as an independently hash-bound package component, never rewrite snapshot.
    manifest_path, manifest_ref = _reference(row["source_manifest"], source_resolver=source_resolver)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["master_hash"] != source_master_hash:
        raise ValueError("Source archive manifest Master differs")
    import yaml
    name = "ProduceCardStatusEnchant.yaml"
    supplement_path, supplement_ref = _reference({"path": str(manifest_path.parent / "yaml" / name),
        "sha256": manifest["yaml_sha256"][name]}, source_resolver=source_resolver)
    supplement_rows = yaml.safe_load(supplement_path.read_text(encoding="utf-8"))
    if not isinstance(supplement_rows, list) or any(not isinstance(x, dict) or not isinstance(x.get("id"), str) for x in supplement_rows):
        raise ValueError("Source card enchant table shape differs")
    supplement = {x["id"]: x for x in supplement_rows}
    if len(supplement) != len(supplement_rows):
        raise ValueError("Source card enchant identities are ambiguous")
    proof_path, proof_ref = _reference(native_schema_reference, expected=_PROOF_SHA, source_resolver=source_resolver)
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    metadata_path, _ = _reference(proof["source_metadata"], source_resolver=source_resolver)
    stat = metadata_path.stat()
    schema = _native_schema(str(proof_path), proof_ref["sha256"], (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns), source_resolver=source_resolver)
    body = {"schema": SCHEMA, "source_master_hash": source_master_hash,
        "card_payload_sha256": catalog_ref["sha256"], "snapshot_sha256": snapshot_ref["sha256"],
        "snapshot_fingerprint": row["snapshot_fingerprint"],
        "native_core_sha256": schema["source_image"]["sha256"],
        "native_metadata_sha256": schema["source_metadata"]["sha256"],
        "native_schema_proof_sha256": proof_ref["sha256"],
        "source_inventory_reference": inventory_ref, "native_schema_reference": proof_ref,
        "catalog_reference": catalog_ref, "snapshot_reference": snapshot_ref,
        "source_archive_manifest_reference": manifest_ref,
        "supplemental_table_references": {"produce_card_status_enchant": supplement_ref},
        "grow_enum_reference": _plain(schema["grow_enum"]),
        "numeric_enums_sha256": _digest(schema["numeric"]),
        "runtime_grow_fields": list(GROW_FIELDS), "zones": list(ZONES),
        "source_version_scope": "retained four-Master inventory; each identity remains distinct",
        "native_arithmetic": "original-PC-authority; not executed by this representation",
        "lineage_role": "ordered-source-witness-not-a-second-effect-layer",
        "numeric_encoding": "typed exact int/float-hex plus existing finite sign/coarse/log2 magnitude tokens",
        "static_definition_reference_fields": dict(_REFS),
        "runtime_definition_reference_fields": {**_RUNTIME_REFS, **_ACTIVE_REFS},
        "target_filter_and_group_ids": "ordered symbolic selectors retained, not static definition lookups",
        "excluded_display_fields": sorted(_DISPLAY), "excluded_local_identity_fields": sorted(_LOCAL_IDS),
        "scope": "primary-decision-before-BC-structure", "diagnostic_only": True,
        "action_after_state_observed": False, "training_admitted": False}
    return NativeStructurePackage(payload, snapshot, schema, {**body, "contract_sha256": _digest(body)},
        supplemental_tables={"produce_card_status_enchant": supplement}, _authority=_SEAL)


def load_native_structure_inference_package(source_receipt_reference, *, source_resolver=None) -> NativeStructurePackage:
    """Load a separate, verified current source without promoting training data."""
    from .runtime_master_source import load_runtime_master_source
    source = load_runtime_master_source(source_receipt_reference)
    materials, receipt = source._package_materials()
    proof_path, proof_ref = _reference(source.native_schema_reference, expected=_PROOF_SHA, source_resolver=source_resolver)
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    metadata_path, _ = _reference(proof["source_metadata"], source_resolver=source_resolver)
    stat = metadata_path.stat()
    schema = _native_schema(str(proof_path), proof_ref["sha256"], (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns), source_resolver=source_resolver)
    body = source.native_contract_template
    body.pop("contract_sha256")
    if (body["schema"] != SCHEMA or body["native_core_sha256"] != schema["source_image"]["sha256"]
            or body["native_metadata_sha256"] != schema["source_metadata"]["sha256"]
            or body["numeric_enums_sha256"] != _digest(schema["numeric"])):
        raise ValueError("Current inference package changes the frozen numerical/schema authority")
    body.update(source_master_hash=source.master_hash, card_payload_sha256=receipt["catalog"]["sha256"],
        snapshot_sha256=receipt["snapshot"]["sha256"], snapshot_fingerprint=materials["snapshot"]["fingerprint"],
        source_inventory_reference=source.reference, catalog_reference=receipt["catalog"], snapshot_reference=receipt["snapshot"],
        source_archive_manifest_reference=receipt["archive"],
        supplemental_table_references={"produce_card_status_enchant": receipt["supplemental_table"]},
        source_version_scope="explicit-current-runtime-Master; not a historical training route",
        inference_source_receipt=source.reference, training_admitted=False)
    package = NativeStructurePackage(materials["payload"], materials["snapshot"], schema,
        {**body, "contract_sha256": _digest(body)},
        supplemental_tables={"produce_card_status_enchant": materials["supplement"]}, _authority=_SEAL)
    source.validate_unchanged()
    return package


def native_structure_contract(package) -> dict:
    if not isinstance(package, NativeStructurePackage):
        raise ValueError("A verified native structure package is required")
    return _plain(package.contract)


def _emit(prefix, value, tokens, gaps, *, depth=0):
    """Typed, exact leaves and explicit presence; preserve sequence order."""
    if depth > 32:
        gaps.append(prefix + ":shape-depth-exceeded")
    elif value is None:
        tokens.append(prefix + ":observed-null")
    elif type(value) is bool:
        tokens.append(f"{prefix}:bool:{int(value)}")
    elif type(value) is int:
        tokens.append(f"{prefix}:int:{value}")
        if -(2**63) <= value < 2**63:
            tokens.extend("native-magnitude:" + x for x in number_tokens(prefix, value) if ":exact:" not in x)
    elif type(value) is float:
        if not math.isfinite(value):
            gaps.append(prefix + ":nonfinite-number")
        else:
            tokens.append(f"{prefix}:float:{value.hex()}")
            tokens.extend("native-magnitude:" + x for x in number_tokens(prefix, value) if ":exact:" not in x)
    elif isinstance(value, str):
        tokens.append(prefix + ":str:" + value)
    elif isinstance(value, Mapping):
        tokens.append(prefix + ":object")
        for key, child in sorted(value.items()):
            if key not in _DISPLAY and key not in _LOCAL_IDS:
                _emit(prefix + "." + key, child, tokens, gaps, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        tokens.append(f"{prefix}:list:{len(value)}")
        for index, child in enumerate(value):
            _emit(f"{prefix}[{index}]", child, tokens, gaps, depth=depth + 1)
    else:
        gaps.append(prefix + ":shape-invalid")


def _int32(value):
    return type(value) is int and -(2**31) <= value < 2**31


@dataclass(frozen=True, slots=True, init=False)
class NativeStructureCardSemanticFeatures(CardSemanticFeatures):
    package: NativeStructurePackage
    _active_status_contract: Mapping | None

    def __init__(self, package, *, active_status_contract=None):
        native_structure_contract(package)
        if active_status_contract is not None:
            from .active_state_features import validate_active_status_extension
            validate_active_status_extension(active_status_contract)
        object.__setattr__(self, "payload", package.payload)
        object.__setattr__(self, "package", package)
        object.__setattr__(self, "_active_status_contract", _freeze(active_status_contract))

    @property
    def active_status_contract(self):
        # Legacy validators serialize plain JSON and compare list shapes.
        # Hand out a fresh copy without exposing immutable stored evidence.
        return _plain(self._active_status_contract)

    def _table(self, name):
        return self.package.snapshot["tables"].get(name, self.package.snapshot["semantic_tables"].get(name,
            self.package.supplemental_tables.get(name, {})))

    def _graph(self, table, identity, prefix, tokens, gaps, visited=()):
        if not isinstance(identity, str) or not identity:
            gaps.append(prefix + ":reference-shape-invalid")
            return
        row = self._table(table).get(identity)
        if not isinstance(row, Mapping):
            gaps.append(f"{prefix}:reference-missing:{table}:{identity}")
            return
        key = (table, identity)
        if key in visited:
            tokens.append(prefix + ":reference-cycle:" + table + ":" + identity)
            return
        if len(visited) >= 32:
            gaps.append(prefix + ":reference-depth-exceeded")
            return
        raw = row.get("raw_json", row)
        _emit(prefix, raw, tokens, gaps)
        self._links(raw, prefix, tokens, gaps, (*visited, key))

    def _links(self, value, prefix, tokens, gaps, visited=()):
        if not isinstance(value, Mapping):
            return
        for key, child in value.items():
            if key in _DISPLAY:
                continue
            if isinstance(child, str) and "_" in child:
                enum_name = child.split("_", 1)[0]
                members = self.package.native_schema["symbolic"].get(enum_name)
                if members is not None and child not in members:
                    gaps.append(f"{prefix}.{key}:enum-unknown:{child}")
            if key in _REFS and child not in (None, "", (), []):
                children = child if isinstance(child, (tuple, list)) else (child,)
                for index, identity in enumerate(children):
                    self._graph(_REFS[key], identity, f"{prefix}.{key}.definition[{index}]", tokens, gaps, visited)
            elif isinstance(child, Mapping):
                self._links(child, prefix + "." + key, tokens, gaps, visited)
            elif isinstance(child, (list, tuple)):
                for index, part in enumerate(child):
                    if isinstance(part, Mapping):
                        self._links(part, f"{prefix}.{key}[{index}]", tokens, gaps, visited)

    def _grow(self, grow, prefix, tokens, gaps):
        _emit(prefix, grow, tokens, gaps)
        if not isinstance(grow, Mapping):
            gaps.append(prefix + ":grow-shape-invalid")
            return
        for key in GROW_FIELDS:
            if key not in grow:
                gaps.append(prefix + ":field-missing:" + key)
        for key in set(grow) - set(GROW_FIELDS):
            gaps.append(prefix + ":field-unclassified:" + key)
        if not isinstance(grow.get("_id"), str) or not grow.get("_id"):
            gaps.append(prefix + ":grow-identity-invalid")
        if not _int32(grow.get("_value")):
            gaps.append(prefix + ":value-not-int32")
        for key, owner in (("_effectType", "ProduceCardGrowEffectType"), ("_playMovePositionType", "ProduceCardMovePositionType")):
            number = grow.get(key)
            if type(number) is not int or number not in self.package.native_schema["numeric"][owner]:
                gaps.append(prefix + ":enum-unknown:" + key)
            else:
                tokens.append(prefix + "." + key + ":enum:" + self.package.native_schema["numeric"][owner][number])
        for key in ("_targetPlayEffectProduceExamTriggerIdList", "_targetPlayProduceExamEffectIdList"):
            values = grow.get(key)
            if values is not None and (not isinstance(values, list) or any(not isinstance(x, str) for x in values)):
                gaps.append(prefix + ":identifier-list-shape-invalid:" + key)
        for key, table in _RUNTIME_REFS.items():
            value = grow.get(key)
            if value is not None and not isinstance(value, str):
                gaps.append(prefix + ":identifier-shape-invalid:" + key)
            elif value:
                self._graph(table, value, prefix + "." + key + ".definition", tokens, gaps)

    def _status(self, status, tokens, gaps):
        if status is None or status == {}:
            return  # Successful null and the proof-backed view are distinct tokens.
        if not isinstance(status, Mapping):
            gaps.append("runtime-card-status-shape-invalid")
            return
        fields = {"_id", "_produceExamTriggerId", "_produceCardGrowEffectIdList", "_effectGroupIdList",
            "_descriptionList", "_triggerCount", "_phaseCountDictionary", "_spendTurn", "_spendCount"}
        for key in fields - {"_descriptionList"}:
            if key not in status:
                gaps.append("runtime-card-status-field-missing:" + key)
        for key in set(status) - fields:
            gaps.append("runtime-card-status-field-unclassified:" + key)
        for key in ("_id", "_produceExamTriggerId"):
            if not isinstance(status.get(key), str):
                gaps.append("runtime-card-status-identifier-invalid:" + key)
        for key in ("_triggerCount", "_spendTurn", "_spendCount"):
            if not _int32(status.get(key)):
                gaps.append("runtime-card-status-int32-invalid:" + key)
        for key in ("_produceCardGrowEffectIdList", "_effectGroupIdList"):
            values = status.get(key)
            if not isinstance(values, list) or any(not isinstance(x, str) or not x for x in values):
                gaps.append("runtime-card-status-list-invalid:" + key)
            elif key == "_produceCardGrowEffectIdList":
                for index, identity in enumerate(values):
                    self._graph("produce_card_grow_effect", identity, f"native.status.grow[{index}]", tokens, gaps)
        trigger = status.get("_produceExamTriggerId")
        if isinstance(trigger, str) and trigger:
            self._graph("produce_exam_trigger", trigger, "native.status.trigger", tokens, gaps)
        phases = status.get("_phaseCountDictionary")
        rows = phases.get("_list") if isinstance(phases, Mapping) else None
        if not isinstance(rows, list) or set(phases) != {"_list"}:
            gaps.append("runtime-card-status-phase-dictionary-invalid")
        else:
            seen = set()
            for entry in rows:
                value = entry.get("value") if isinstance(entry, Mapping) else None
                forecast = value.get("forecastStart") if isinstance(value, Mapping) else None
                valid = (isinstance(entry, Mapping) and set(entry) == {"key", "value"}
                    and _int32(entry["key"]) and entry["key"] not in seen
                    and isinstance(value, Mapping) and set(value) == {"current", "forecastStart"}
                    and _int32(value["current"]) and isinstance(forecast, Mapping)
                    and set(forecast) == {"hasValue", "value"} and type(forecast["hasValue"]) is bool
                    and _int32(forecast["value"]))
                if not valid:
                    gaps.append("runtime-card-status-phase-entry-invalid")
                elif _int32(entry["key"]):
                    seen.add(entry["key"])

    def _native_links(self, value, prefix, tokens, gaps, depth=0):
        """Expand declared current native references, not their predicates.

        A materialized effect's _id and origin/lineage IDs remain witnesses;
        condition/search/chain/enchant IDs are definition references. Copied
        owner instances only supply a source definition, not a second state.
        """
        if depth > 32:
            gaps.append(prefix + ":native-reference-depth-exceeded")
            return
        if isinstance(value, list):
            for index, child in enumerate(value):
                self._native_links(child, f"{prefix}[{index}]", tokens, gaps, depth + 1)
            return
        if not isinstance(value, Mapping):
            return
        if "rid" in value:
            gaps.append(prefix + ":native-nested-reference-unresolved")
        for key, child in value.items():
            if key in _DISPLAY:
                continue
            path = prefix + "." + key
            if key in _ACTIVE_REFS:
                if child is None or child == "":
                    continue
                children = child if isinstance(child, list) else [child]
                if key.endswith("List") and not isinstance(child, list):
                    gaps.append(path + ":reference-list-shape-invalid")
                    continue
                for index, identity in enumerate(children):
                    self._graph(_ACTIVE_REFS[key], identity, f"{path}.definition[{index}]", tokens, gaps)
            elif key in ("_trigger", "_gimmickTrigger"):
                if not isinstance(child, Mapping):
                    if child is not None:
                        gaps.append(path + ":trigger-shape-invalid")
                elif "_id" not in child or (child["_id"] is not None and not isinstance(child["_id"], str)):
                    gaps.append(path + ":trigger-identity-invalid")
                elif child["_id"]:
                    self._graph("produce_exam_trigger", child["_id"], path + ".definition", tokens, gaps)
            elif key in ("_triggerCard", "_triggerItem", "_triggerDrink"):
                if isinstance(child, Mapping):
                    if key == "_triggerCard":
                        data = child.get("_cardData", {})
                        if isinstance(data, Mapping) and isinstance(data.get("_id"), str) and type(data.get("_upgradeCount")) is int:
                            self._graph("card", f"{data['_id']}@{data['_upgradeCount']}", path + ".definition", tokens, gaps)
                    elif child.get("_id"):
                        self._graph("produce_item" if key == "_triggerItem" else "produce_drink",
                            child["_id"], path + ".definition", tokens, gaps)
            else:
                self._native_links(child, path, tokens, gaps, depth + 1)

    def _card(self, card_id, upgrade, runtime):
        tokens, gaps = [], []
        ref = f"{card_id}@{upgrade}"
        row = self.payload["cards"].get(ref)
        if type(upgrade) is not int or upgrade < 0 or not isinstance(row, Mapping):
            return (), ("card-reference-missing:" + ref,)
        tokens.extend("semantic:" + x for x in row["tokens"])
        self._graph("card", ref, "native.base", tokens, gaps)
        if not isinstance(runtime, Mapping) or not isinstance(runtime.get("_cardData"), Mapping):
            return tuple(tokens), ("runtime-card-data-missing",)
        data = runtime["_cardData"]
        for key in set(data) - {"_id", "_upgradeCount", "_customizeCountList", "_produceCardSkinId", "_produceCardSkinAssetId"}:
            _emit("native.cardData." + key, data[key], tokens, gaps)
            gaps.append("runtime-card-data-field-unclassified:" + key)
        if data.get("_id") != card_id or type(data.get("_upgradeCount")) is not int or data.get("_upgradeCount") != upgrade:
            gaps.append("runtime-card-source-identity-mismatch")
        counts = data.get("_customizeCountList")
        if "_customizeCountList" not in data:
            gaps.append("runtime-customization-field-missing")
        elif not isinstance(counts, list) or any(type(x) is not int or x < 0 for x in counts):
            gaps.append("runtime-customization-shape-invalid")
        else:
            _emit("native.pt.counts", counts, tokens, gaps)
            for index, count in enumerate(counts):
                if index >= len(row["customize_ids"]):
                    gaps.append(f"runtime-customization-index-unbound:{index}")
                    continue
                identity = row["customize_ids"][index]
                tokens.append(f"native.pt.option[{index}]:id:{identity}")
                if count:
                    selected = row["customization_rules"].get(identity, {}).get(str(count))
                    if not isinstance(selected, Mapping):
                        gaps.append(f"runtime-customization-rule-missing:{identity}:{count}")
                    else:
                        _emit(f"native.pt.selected[{index}]", selected, tokens, gaps)
                        self._links(selected, f"native.pt.selected[{index}]", tokens, gaps)
                        self._graph("produce_card_customize", f"{identity}@{count}", f"native.pt.source[{index}]", tokens, gaps)
        for key in _RUNTIME_FIELDS:
            if key not in runtime:
                gaps.append("runtime-field-missing:" + key)
                continue
            _emit("native.runtime." + key, runtime[key], tokens, gaps)
        for key in ("_fixedDeckOrder", "_baseUpgradeCount", "_tmpUpgradeCount", "_playCount"):
            if key in runtime and not _int32(runtime[key]):
                gaps.append("runtime-int32-invalid:" + key)
        if "_isMoveProduceExamEffectUseInTurn" in runtime and type(runtime["_isMoveProduceExamEffectUseInTurn"]) is not bool:
            gaps.append("runtime-bool-invalid:_isMoveProduceExamEffectUseInTurn")
        support = runtime.get("_supportUpgradeIdList")
        if not isinstance(support, list) or any(not isinstance(x, str) for x in support):
            gaps.append("runtime-support-upgrades-shape-invalid")
        for key in set(runtime) - set(_RUNTIME_FIELDS) - {"_cardData", "_guid"}:
            _emit("native.runtime." + key, runtime[key], tokens, gaps)
            gaps.append("runtime-field-unclassified:" + key)
        grows = runtime.get("_growEffectExamStartAfterList")
        if not isinstance(grows, list):
            gaps.append("runtime-materialized-grow-list-invalid")
        else:
            for index, grow in enumerate(grows):
                self._grow(grow, f"native.materialized[{index}]", tokens, gaps)
        lineage = runtime.get("_affectGrowEffectIdList")
        if not isinstance(lineage, list) or any(not isinstance(x, str) or not x for x in lineage):
            gaps.append("runtime-lineage-shape-invalid")
        # Lineage is a witness. Generated IDs and the retained representative ID
        # do not override materialized values or create static lookup blockers.
        self._status(runtime.get("_statusEffect"), tokens, gaps)
        specified = runtime.get("_staminaConsumptionSpecifyEffectList")
        if not isinstance(specified, list):
            gaps.append("runtime-specified-cost-list-invalid")
        elif specified:
            # Original owner is List<ExamCardValueStatusEffect>. This version
            # has no independent element-shape/consumer admission; preserve
            # every value and keep nonempty entries explicitly unresolved.
            gaps.append("runtime-specified-cost-element-contract-unqualified")
        # No claim that zero/missing cost summaries mean zero actual payment.
        return tuple(tokens), tuple(sorted(set(gaps)))

    def card_tokens(self, card_id, upgrade, runtime):
        tokens, gaps = self._card(card_id, upgrade, runtime)
        return (*tokens, *("semantic:gap:native-structure:" + x for x in gaps))

    def drink_tokens(self, drink_id):
        tokens, gaps = [], []
        row = self.payload["drinks"].get(drink_id)
        if not isinstance(row, Mapping):
            gaps.append("drink-reference-missing:" + str(drink_id))
        else:
            tokens.extend("semantic:" + x for x in row["tokens"])
            self._graph("produce_drink", drink_id, "native.drink", tokens, gaps)
        return (*tokens, *("semantic:gap:native-structure:" + x for x in gaps))

    def _context(self, state):
        from .active_state_features import extract_active_state_features
        observed = extract_active_state_features(state, qualified_status_extension=self.active_status_contract)
        optional = ["derived-effective-card-arithmetic:not-requested", "derived-lineage-replay:not-requested"]
        mandatory = []
        # Unknown class summary is optional only if its raw linked object was
        # retained; missing refs/shape/number/depth remain mandatory.
        tokens = [x for x in observed.tokens if not x.startswith("active-v2:gap:")]
        for gap in observed.gaps:
            text = gap.code + ":" + gap.path
            if gap.code == "unclassified-status":
                optional.append(text)
            else:
                mandatory.append("active:" + text)
        status, references = state.get("status"), state.get("references")
        links = status.get("_effectList") if isinstance(status, Mapping) else None
        refs = references.get("RefIds") if isinstance(references, Mapping) else None
        if isinstance(links, list) and isinstance(refs, list):
            for index, link in enumerate(links):
                rid = link.get("rid") if isinstance(link, Mapping) else None
                rows = [x for x in refs if isinstance(x, Mapping) and type(x.get("rid")) is int and x["rid"] == rid]
                if type(rid) is int and len(rows) == 1 and isinstance(rows[0].get("data"), Mapping):
                    self._native_links(rows[0]["data"], f"native.active[{index}]", tokens, mandatory)
        for zone in ZONES:
            rows = state.get(zone)
            if not isinstance(rows, list):
                mandatory.append("zone-missing-or-invalid:" + zone)
                continue
            tokens.append(f"native.zone.{zone}:list:{len(rows)}")
            for index, runtime in enumerate(rows):
                data = runtime.get("_cardData", {}) if isinstance(runtime, Mapping) else {}
                card, gaps = self._card(data.get("_id"), data.get("_upgradeCount"), runtime)
                tokens.extend(f"native.zone.{zone}[{index}]:" + x for x in card)
                mandatory.extend(f"{zone}[{index}]:" + x for x in gaps)
        drinks = state.get("drinkList")
        if isinstance(drinks, list):
            for index, row in enumerate(drinks):
                if not isinstance(row, Mapping) or not isinstance(row.get("_id"), str):
                    mandatory.append(f"drinkList[{index}]:shape-invalid")
                    continue
                _emit(f"native.inventory.drink[{index}]", row, tokens, mandatory)
                self._graph("produce_drink", row["_id"], f"native.inventory.drink[{index}].definition", tokens, mandatory)
        gimmicks = state.get("gimmickList")
        if not isinstance(gimmicks, list):
            mandatory.append("gimmickList:missing-or-shape-invalid")
        for index, row in enumerate(gimmicks if isinstance(gimmicks, list) else ()):
            if not isinstance(row, Mapping):
                mandatory.append(f"gimmick[{index}]:shape-invalid")
                continue
            matches = [x for x in self.payload["gimmicks"].get(row.get("gimmickGroupId"), ())
                if x["effect_id"] == row.get("gimmickEffectId") and x["turn"] == row.get("turn")]
            _emit(f"native.gimmick[{index}]", row, tokens, mandatory)
            if len(matches) != 1:
                mandatory.append(f"gimmick[{index}]:schedule-reference-unbound")
            else:
                tokens.extend(f"native.gimmick[{index}]:" + x for x in matches[0]["tokens"])
                self._graph("effect", row["gimmickEffectId"], f"native.gimmick[{index}].effect", tokens, mandatory)
        _emit("native.action-window", _action_window(self.payload, state), tokens, mandatory)
        return tuple(tokens), tuple(sorted(set(mandatory))), tuple(sorted(set(optional)))

    def context_tokens(self, state):
        tokens, gaps, _ = self._context(state)
        return (*tokens, *("semantic-context:gap:native-structure:" + x for x in gaps))

    def action_context_tokens(self, kind, state, *, drink_id=""):
        # Observed action kind/window only; no hand-written payoff arithmetic.
        tokens, gaps = [], []
        _emit("native.action.kind", kind, tokens, gaps)
        _emit("native.action.window", _action_window(self.payload, state), tokens, gaps)
        return tuple(tokens)

    def representation_coverage(self, state, legal_candidates=None):
        _, gaps, optional = self._context(state)
        mandatory = list(gaps)
        if legal_candidates is not None:
            if not isinstance(legal_candidates, (list, tuple)) or not legal_candidates:
                mandatory.append("native-candidates-shape-invalid")
            else:
                for row in legal_candidates:
                    if not isinstance(row, Mapping):
                        mandatory.append("native-candidate-shape-invalid")
                    elif row.get("kind") == "use-drink":
                        mandatory.extend(x for x in self.drink_tokens(row.get("drink_id")) if x.startswith("semantic:gap:"))
        mandatory = sorted(set(mandatory))
        return {"schema": SCHEMA, "mandatory_gaps": mandatory,
            "optional_derived_diagnostics": list(optional), "graph_complete": not mandatory,
            "representation_complete": not mandatory, "derived_arithmetic_required": False,
            "source_package_contract_sha256": self.package.contract["contract_sha256"],
            "action_after_state_observed": False, "RL_transition_qualified": False,
            "training_admitted": False}


__all__ = ["SCHEMA", "NativeStructurePackage", "NativeStructureCardSemanticFeatures",
    "load_native_structure_package", "load_native_structure_inference_package", "native_structure_contract"]
