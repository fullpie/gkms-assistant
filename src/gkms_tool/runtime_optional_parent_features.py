"""Inference-only optional parent-definition links over the frozen codec.

An empty source definition ID is not the same assertion as a null object.
The original object and all its structural tokens remain untouched. Only the
definition-edge visitor omits an explicitly absent optional parent-card edge.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import re
from pathlib import Path

from .native_structure_features import NativeStructureCardSemanticFeatures
from .training_artifact_io import canonical_json_bytes, sha256_file

SCHEMA = "gkms.runtime-optional-parent-definition-extension.v1"
PROOF_SCHEMA = "gkms.optional-parent-card-definition-edge-proof.v1"
_ROOT = Path(__file__).resolve().parents[2]
PROOF_REFERENCE = {
    "path": str(_ROOT / "var/research/post_update_live_20260919/live_serializer_reference_case_v1/optional_parent_card_definition_edge_proof_v1.json"),
    "sha256": "22cf3be15003847443807246c8311fb4432952e062553c42333d7efef8e66209",
}


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_reference(reference):
    if not isinstance(reference, Mapping) or set(reference) - {"path", "sha256", "bytes"}:
        raise ValueError("Optional parent source proof requires an explicit path/SHA")
    path = Path(reference["path"])
    if not path.is_absolute() or sha256_file(path) != reference["sha256"]:
        raise ValueError("Optional parent source proof bytes differ")
    return path


class RuntimeOptionalParentCardFeatures(NativeStructureCardSemanticFeatures):
    """Keep the inherited emitter, graph encoder and numeric operations intact."""

    def __init__(self, package, *, active_status_contract=None):
        super().__init__(package, active_status_contract=active_status_contract)
        proof_path = _read_reference(PROOF_REFERENCE)
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        if (proof.get("schema") != PROOF_SCHEMA
                or proof.get("empty_raw_card_id_has_no_definition_edge") is not True
                or proof.get("nonnull_object_equivalent_to_null") is not False
                or proof.get("numeric_encoding_changed") is not False
                or proof.get("source_metadata_sha256") != "9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668"
                or proof.get("current_metadata_sha256") != "9349bc965fb08a434ab1c9547a3440a8ee02a05e761723792aabe5f4a9ecb635"):
            raise ValueError("Optional parent source proof scope differs")
        references = proof.get("evidence")
        if not isinstance(references, list) or not references:
            raise ValueError("Optional parent source proof has no evidence")
        paths = [proof_path, *[_read_reference(row) for row in references], Path(__file__).resolve()]
        object.__setattr__(self, "_extension_files", tuple((path, _stamp(path)) for path in paths))
        body = {
            "schema": SCHEMA, "proof": deepcopy(PROOF_REFERENCE),
            "implementation": {"path": str(paths[-1]), "sha256": sha256_file(paths[-1])},
            "scope": "optional _triggerCard definition edge only; explicit empty string ID and Int32-compatible integer upgrade zero",
            "raw_state_modified": False, "nonnull_object_changed_to_null": False,
            "existing_emitted_tokens_preserved": True, "inherited_numeric_methods_unchanged": True,
            "real_unknown_definition_ids_rejected": True, "training_admitted": False,
        }
        import hashlib
        body["contract_sha256"] = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        object.__setattr__(self, "_extension_json", canonical_json_bytes(body))

    @property
    def optional_parent_definition_extension(self):
        return json.loads(self._extension_json)

    def validate_definition_extension(self):
        if any(_stamp(path) != stamp for path, stamp in self._extension_files):
            raise ValueError("Optional parent definition source or implementation changed during execution")

    def _context(self, state):
        # Validate the qualified owner before delegating all actual encoding.
        self.validate_definition_extension()
        status, references = state.get("status"), state.get("references")
        links = status.get("_effectList", []) if isinstance(status, Mapping) else []
        refs = references.get("RefIds", []) if isinstance(references, Mapping) else []
        refs = refs if isinstance(refs, list) else []
        for link in links if isinstance(links, list) else ():
            if not isinstance(link, Mapping):
                continue
            rows = [row for row in refs if isinstance(row, Mapping) and row.get("rid") == link.get("rid")]
            if len(rows) != 1 or not isinstance(rows[0].get("data"), Mapping):
                continue  # The unchanged parent retains the missing/ambiguous gap.
            source = rows[0]["data"].get("_triggerCard")
            data = source.get("_cardData") if isinstance(source, Mapping) else None
            if (isinstance(data, Mapping) and data.get("_id") == ""
                    and type(data.get("_upgradeCount")) is int and data["_upgradeCount"] == 0
                    and rows[0].get("type") != {"asm": "Assembly-CSharp", "ns": "Campus.InGame.Exam", "class": "TriggerEffectStatusEffect"}):
                raise ValueError("Empty optional parent definition has an unqualified native owner")
        return super()._context(state)

    def _native_links(self, value, prefix, tokens, gaps, depth=0):
        # This visitor expands only static definition edges. The inherited
        # active-state encoder already produced its existing structural tokens.
        if (depth <= 32 and re.fullmatch(r"native\.active\[\d+\]", prefix)
                and isinstance(value, Mapping) and "_triggerCard" in value):
            source = value["_triggerCard"]
            if source is not None:
                data = source.get("_cardData") if isinstance(source, Mapping) else None
                if (not isinstance(data, Mapping) or "_id" not in data or "_upgradeCount" not in data
                        or not isinstance(data["_id"], str) or type(data["_upgradeCount"]) is not int
                        or not 0 <= data["_upgradeCount"] < 2**31):
                    gaps.append(prefix + "._triggerCard.definition:optional-parent-source-identity-invalid")
                elif data["_id"] == "":
                    if data["_upgradeCount"] != 0:
                        gaps.append(prefix + "._triggerCard.definition:empty-id-with-nonzero-upgrade")
                    else:
                        # A link-only projection, not a state/object rewrite.
                        remaining_edges = {key: child for key, child in value.items() if key != "_triggerCard"}
                        return super()._native_links(remaining_edges, prefix, tokens, gaps, depth)
        return super()._native_links(value, prefix, tokens, gaps, depth)


__all__ = ["RuntimeOptionalParentCardFeatures", "SCHEMA", "PROOF_REFERENCE"]
