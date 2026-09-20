"""Source-bound recovery of an observed secondary command from retained PC logs.

Only the sealed pointer-pair inventory is accepted. This authorizes a command
projection, not training, a native effect-context payload, or an RL transition.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import weakref

from .training_artifact_io import canonical_json_bytes
from .training_quarantine import load_current_training_quarantine

ROOT = Path(__file__).resolve().parents[2]
BINDING_MANIFEST = ROOT / "var/research/pc_core_20260914/secondary_current_command_binding_v1/manifest.json"
BINDING_MANIFEST_SHA256 = "52cadb29925fe79bc57ab952e9a95309fa0d3a57beee01f19bea931b53494ed8"
STORED_CONTEXT_SCHEMA = "gkms.original-pc-paired-log-selector-context.v1"
CONTEXT_SOURCE = "paired-original-AddPlayLog-command"
_CONTEXTS = weakref.WeakKeyDictionary()
_BINDINGS = weakref.WeakKeyDictionary()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _same(left, right):
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _stamp(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def _read_reference(reference):
    if (not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str)
            or not isinstance(reference.get("sha256"), str)):
        raise ValueError("Hashed original binding reference required")
    path = Path(reference["path"])
    before = _stamp(path)
    data = path.read_bytes()
    if (_sha(data) != reference["sha256"] or "bytes" in reference and reference["bytes"] != len(data)
            or _stamp(path) != before):
        raise ValueError("Original binding reference changed: " + str(path))
    return data


def _entry_key(reference):
    if not isinstance(reference, Mapping):
        raise ValueError("Exact indexed original secondary event reference required")
    keys = ("event_order", "source_order", "kind", "phase", "line_number", "byte_offset", "bytes")
    if (any(type(reference.get(k)) is not int or reference[k] < 0 for k in keys)
            or reference["kind"] != 2 or reference["phase"] != 0
            or not isinstance(reference.get("path"), str) or not isinstance(reference.get("sha256"), str)):
        raise ValueError("Exact indexed original secondary event reference required")
    return (str(Path(reference["path"]).resolve()), *(reference[k] for k in keys), reference["sha256"])


class VerifiedSecondaryCommandBindings:
    __slots__ = ("__weakref__",)

    def __init__(self):
        raise ValueError("Use load_verified_secondary_command_bindings")

    @property
    def reference(self):
        return deepcopy(_context_payload(self)["reference"])

    @property
    def summary(self):
        payload = _context_payload(self)
        return {"rows": len(payload["rows"]), "cases": len(payload["cases"]),
                "context_source": CONTEXT_SOURCE, "secondary_training_admitted": False,
                "effect_context_payload_observed": False}

    def bind_event(self, raw_event: bytes, event_reference: Mapping):
        """Verify already-read complete source bytes; do not reread the full span."""
        payload = _context_payload(self)
        key = _entry_key(event_reference)
        row = payload["rows"].get(key)
        if row is None or not _same(event_reference, row["event_reference"]):
            raise ValueError("Secondary event is outside the sealed original command-pair scope")
        if (type(raw_event) is not bytes or len(raw_event) != event_reference["bytes"]
                or _sha(raw_event) != event_reference["sha256"]):
            raise ValueError("Complete consumed secondary event bytes differ from the sealed span")
        case = payload["cases"][row["original_index"]]
        _verify_case(case, row)
        event = json.loads(raw_event)
        prefix_reference = row["entry_identity_prefix_reference"]
        if _sha(raw_event[:prefix_reference["bytes"]]) != prefix_reference["sha256"]:
            raise ValueError("Secondary callback identity prefix differs")
        identities = {key: event.get(key) for key in row["entry_identity_prefix"]}
        if not _same(identities, row["entry_identity_prefix"]):
            raise ValueError("Secondary callback identity/cursor differs from its original pair")
        _verify_window(row["previous_event_reference"], row["previous_event"])
        _verify_pair(row)
        if _stamp(case["events"]["path"]) != case["file_stamp"]:
            raise ValueError("Original event stream changed during command binding")
        context, gaps = _project_stored_command(event, row, case, payload["reference"])
        binding = object.__new__(VerifiedStoredSecondaryCommandBinding)
        _BINDINGS[binding] = {"context": context, "gaps": gaps,
                              "event_digest": _sha(canonical_json_bytes(event)),
                              "case": case, "row": row}
        return binding


class VerifiedStoredSecondaryCommandBinding:
    __slots__ = ("__weakref__",)

    def __init__(self):
        raise ValueError("Use VerifiedSecondaryCommandBindings.bind_event")

    @property
    def context(self):
        return deepcopy(_binding_payload(self)["context"])


def _context_payload(context):
    if type(context) is not VerifiedSecondaryCommandBindings or context not in _CONTEXTS:
        raise ValueError("Source-verified secondary binding context required")
    return _CONTEXTS[context]


def _binding_payload(binding):
    if type(binding) is not VerifiedStoredSecondaryCommandBinding or binding not in _BINDINGS:
        raise ValueError("Source-verified stored secondary command required")
    return _BINDINGS[binding]


def _verify_window(reference, expected):
    path = Path(reference["path"])
    with path.open("rb") as stream:
        stream.seek(reference["byte_offset"])
        raw = stream.read(reference["bytes"])
    if _sha(raw) != reference["sha256"] or not _same(json.loads(raw), expected):
        raise ValueError("Original AddPlayLog pointer window changed")


def _verify_pair(row):
    previous, entry = row["previous_event"], row["entry_identity_prefix"]
    accepted = previous.get("accepted_log", {})
    if (previous.get("kind") != 3 or previous.get("phase") != 1
            or previous.get("event_order") != entry.get("event_order", -1) - 1
            or accepted.get("is_select") is not False or accepted.get("play_type") != "PlayEffect"
            or not entry.get("command_identity") or accepted.get("command_identity") != entry["command_identity"]
            or previous.get("log_index") != entry.get("log_index")
            or entry.get("log_index") != row["event_reference"]["source_order"]
            or any(not entry.get(k) or previous.get(k) != entry[k]
                   for k in ("simulator_identity", "sequence_identity", "parameter_identity"))):
        raise ValueError("Original AddPlayLog does not bind this selector command")


def _verify_case(case, row):
    index = json.loads(_read_reference(case["event_index"]))
    if (not _same(index["events"], case["events"]) or not _same(index["file_stamp"], case["file_stamp"])
            or row["event_reference"] not in index["decision_events"]
            or not _same(index["source"], row["source"]) or not _same(row["source"], case["source"])
            or not _same(row["fixed_split"], case["fixed_split"])
            or _stamp(case["events"]["path"]) != case["file_stamp"]):
        raise ValueError("Original event-index/source/fixed-group binding changed")
    _read_reference(case["source"])
    quarantine = load_current_training_quarantine()
    if quarantine is None:
        raise ValueError("Current indexed source quarantine is unavailable")
    if quarantine.match({"identity": {"episode_id": case["episode_id"]}}, {},
                        {"file_sha256": case["source"]["sha256"]}):
        raise ValueError("Current quarantine excludes stored secondary source")


def _project_stored_command(event, row, case, manifest_reference):
    # Reuse the pure snapshot validator without changing the primary decoder.
    from .native_secondary_input import _snapshot
    state = _snapshot(event.get("snapshot"))
    _snapshot(event.get("after_candidates"))
    if event["snapshot"]["state_sha256"] != event["after_candidates"]["state_sha256"]:
        raise ValueError("Original secondary candidate query changed the state")
    logs = state.get("playLogList")
    if not isinstance(logs, list) or not logs or not isinstance(logs[-1], Mapping):
        raise ValueError("Paired original log projection is unavailable")
    log = logs[-1]
    command = log.get("_command")
    if (log.get("_isSelectLog") is not False or log.get("_selectIndex") is not None
            or not isinstance(command, Mapping) or type(command.get("_playType")) is not int
            or command["_playType"] != 5):
        raise ValueError("Paired final original log is not the native PlayEffect command projection")
    required = ("_isCardSelect", "_isCardSelect2", "_playEffect", "_cardSelectSearchId",
                "_cardSelectSearchId2", "_playingCard", "_playingDrink", "_playingItem")
    gaps = []
    if (any(key not in command for key in required)
            or any(type(command.get(k)) is not bool for k in ("_isCardSelect", "_isCardSelect2"))
            or not isinstance(command.get("_playEffect"), Mapping)
            or any(command.get(k) is not None and not isinstance(command.get(k), str)
                   for k in ("_cardSelectSearchId", "_cardSelectSearchId2"))
            or any(command.get(k) is not None and not isinstance(command.get(k), Mapping)
                   for k in ("_playingCard", "_playingDrink", "_playingItem"))):
        gaps.append(("stored-current-command-payload-incomplete", "playLogList[-1]._command"))
    if command.get("_isCardSelect") is not True or command.get("_isCardSelect2") is not False:
        gaps.append(("stored-second-selection-context-unqualified", "playLogList[-1]._command"))
    context = {"schema": STORED_CONTEXT_SCHEMA, "context_source": CONTEXT_SOURCE,
        "command_source": CONTEXT_SOURCE, "effect_field_path": "_playEffect",
        "command": deepcopy(command), "effect_context": None, "effect_context_payload_observed": False,
        "effect_context_identity": event.get("effect_context_identity"),
        "references": deepcopy(state.get("references")), "reference_domain": "snapshot.state.references",
        "pointer_binding": {"authority": CONTEXT_SOURCE, "command": event["command_identity"],
            "effect_context": event.get("effect_context_identity"), "parameter": event["parameter_identity"],
            "play_log": row["previous_event"]["play_log_identity"],
            "previous_event_order": row["previous_event"]["event_order"], "event_order": event["event_order"]},
        "source": deepcopy(case["source"]), "source_master_hash": case["source_master_hash"],
        "fixed_split": deepcopy(case["fixed_split"]), "binding_manifest": deepcopy(manifest_reference),
        "event_reference": deepcopy(row["event_reference"]),
        "previous_event_reference": deepcopy(row["previous_event_reference"]),
        "command_projection_path": f"snapshot.state.playLogList[{len(logs) - 1}]._command",
        "teacher_response_used": False, "preceding_main_action_used_as_parent": False,
        "command_pointer_pair_verified": True, "complete": not gaps,
        "secondary_training_admitted": False, "action_after_state_observed": False,
        "RL_transition_qualified": False, "raw_state_changed": False}
    return context, gaps


def verified_stored_secondary_context(binding, event):
    """Internal decoder handoff; mappings and bindings for another event fail closed."""
    payload = _binding_payload(binding)
    _validate_current_binding(payload)
    if _sha(canonical_json_bytes(event)) != payload["event_digest"]:
        raise ValueError("Stored command binding belongs to different original event bytes")
    return deepcopy(payload["context"]), tuple(payload["gaps"])


def _validate_current_binding(payload):
    _verify_case(payload["case"], payload["row"])
    _verify_window(payload["row"]["previous_event_reference"], payload["row"]["previous_event"])
    _verify_pair(payload["row"])
    if _stamp(payload["case"]["events"]["path"]) != payload["case"]["file_stamp"]:
        raise ValueError("Original event stream changed during command binding")


def _register_prepared_secondary_input(binding, prepared):
    from .native_secondary_input import _plain
    payload = _binding_payload(binding)
    payload["prepared"] = prepared
    payload["prepared_digest"] = _sha(canonical_json_bytes(_plain(prepared._record)))
    payload["prepared_gaps"] = prepared.gaps


def validate_bound_secondary_input(binding, prepared):
    """Validate the exact decoder result and reread current source/quarantine.

    The returned reference describes command/input binding, never fit admission.
    Call this at a source-qualified encoder boundary, including prefix reuse.
    """
    from .native_secondary_input import NativeSecondaryInput, _plain
    payload = _binding_payload(binding)
    if (not isinstance(prepared, NativeSecondaryInput) or payload.get("prepared") is not prepared
            or payload.get("prepared_gaps") != prepared.gaps
            or payload.get("prepared_digest") != _sha(canonical_json_bytes(_plain(prepared._record)))):
        raise ValueError("Exact source-bound prepared secondary input required")
    _validate_current_binding(payload)
    return {"binding_manifest": deepcopy(payload["context"]["binding_manifest"]),
            "event_reference": deepcopy(payload["row"]["event_reference"]),
            "source": deepcopy(payload["case"]["source"]),
            "fixed_split": deepcopy(payload["case"]["fixed_split"]),
            "context_source": CONTEXT_SOURCE, "secondary_training_admitted": False}


def load_verified_secondary_command_bindings(manifest_reference=None) -> VerifiedSecondaryCommandBindings:
    reference = {"path": str(BINDING_MANIFEST), "sha256": BINDING_MANIFEST_SHA256} if manifest_reference is None else manifest_reference
    if not isinstance(reference, Mapping) or reference.get("sha256") != BINDING_MANIFEST_SHA256:
        raise ValueError("Only the independently sealed original command-binding manifest is supported")
    raw = _read_reference(reference)
    manifest = json.loads(raw)
    if (manifest.get("schema") != "gkms.original-pc-secondary-stored-command-binding-manifest.v1"
            or manifest.get("rows") != 4917 or manifest.get("cases") != 2177
            or manifest.get("secondary_training_admitted") is not False):
        raise ValueError("Original command-binding scope differs")
    for source in manifest["files"]:
        _read_reference(source)
    contract = json.loads(_read_reference(manifest["contract"]))
    if (contract.get("schema") != "gkms.original-pc-secondary-stored-command-contract.v1"
            or contract.get("context_source") != CONTEXT_SOURCE or contract.get("all_4917_pointer_pairs_verified") is not True
            or contract.get("effect_context_payload_observed") is not False):
        raise ValueError("Original command-binding contract differs")
    cases = {}
    for line in _read_reference(manifest["case_sources"]).splitlines():
        case = json.loads(line)
        if case["original_index"] in cases:
            raise ValueError("Duplicated original command-binding case")
        cases[case["original_index"]] = case
    rows = {}
    for line in _read_reference(manifest["pointer_windows"]).splitlines():
        row = json.loads(line)
        key = _entry_key(row["event_reference"])
        if key in rows or row["original_index"] not in cases:
            raise ValueError("Duplicated or unknown original command-binding event")
        _verify_pair(row)
        rows[key] = row
    if len(rows) != 4917 or len(cases) != 2177:
        raise ValueError("Original command-binding denominator changed")
    context = object.__new__(VerifiedSecondaryCommandBindings)
    _CONTEXTS[context] = {"reference": {"path": str(Path(reference["path"]).resolve()),
        "bytes": len(raw), "sha256": _sha(raw)}, "rows": rows, "cases": cases}
    return context


__all__ = ["BINDING_MANIFEST", "BINDING_MANIFEST_SHA256", "STORED_CONTEXT_SCHEMA", "CONTEXT_SOURCE",
           "VerifiedSecondaryCommandBindings", "VerifiedStoredSecondaryCommandBinding",
           "load_verified_secondary_command_bindings", "validate_bound_secondary_input"]
