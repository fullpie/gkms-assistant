"""BC-only indexes over sealed original-PC decision receipts.

This module reads metadata and old derived row receipts, never observed event
streams. It neither creates RL transitions nor admits data to fitting. A later
source-bound feature worker must hash each consumed native span and encode it.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import secrets
import sys

from .training_artifact_io import canonical_json_bytes
from .training_quarantine import CURRENT_INDEX, load_current_training_quarantine
from .canonical_training_labels import PLAN_BY_NATIVE_VALUE, EFFECT_BY_NATIVE_VALUE, STAGE_BY_NATIVE_VALUE, STAGE_BY_NAME

SCHEMA = "gkms.original-pc-main-bc-ref-index.v1"
CASE_SCHEMA = "gkms.original-pc-main-bc-case-ref.v1"
DECISION_SCHEMA = "gkms.original-pc-main-bc-decision-ref.v1"
MANIFEST_SCHEMA = "gkms.original-pc-main-bc-ref-index-manifest.v1"
ELIGIBILITY_SCOPE = "BC-only-ref-index-not-fit"
_ROOT = Path(__file__).resolve().parents[2]
_SECRET = secrets.token_bytes(32)
_MAX_METADATA_BYTES = 32 * 1024**2
_CASE_KEYS = (
    "original_index", "episode_id", "source_master_hash", "produce_id",
    "stage_type", "fixed_split", "source", "qualification", "event_index",
    "main_decisions", "secondary_decisions", "source_actions",
)
_CAPTURE_REQUIREMENTS = (
    "source_verified_against_original_export", "source_spine_passed",
    "required_responses_covered", "scoped_owned_capture_verified",
    "case_cleanup_passed", "parent_membership_passed", "parent_shared_cleanup_passed",
    "snapshot_hash_and_candidate_rng_verified",
)


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _same(left, right):
    return canonical_json_bytes(left) == canonical_json_bytes(right)


def _stamp(path):
    path = Path(path).resolve()
    s = path.stat()
    return (str(path), s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class _MetadataReader:
    def __init__(self):
        self.stamps = {}

    def binary(self, reference):
        if (not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str)
                or not _sha(reference.get("sha256")) or "byte_offset" in reference):
            raise ValueError("A hash-bound whole metadata file reference is required")
        path = Path(reference["path"]).resolve()
        before = _stamp(path)
        if path.name == "observed_steps.jsonl" or before[1] > _MAX_METADATA_BYTES:
            raise ValueError("Raw event streams are outside the BC metadata reader")
        if "bytes" in reference and (type(reference["bytes"]) is not int or reference["bytes"] != before[1]):
            raise ValueError("Metadata reference length differs")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference["sha256"] or _stamp(path) != before:
            raise ValueError("Metadata reference hash or file identity differs: " + str(path))
        self.stamps[str(path)] = before
        return raw

    def read(self, reference):
        return json.loads(self.binary(reference))


def _load_split_verifier():
    """Reuse the repository's existing full-corpus --verify implementation."""
    script = _ROOT / "scripts/prepare_full_corpus_player_split.py"
    spec = importlib.util.spec_from_file_location("_original_pc_bc_split_verifier", script)
    module = importlib.util.module_from_spec(spec)
    directory = str(script.parent)
    sys.path.insert(0, directory)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(directory)
    return module


def verify_fixed_player_split(manifest_reference):
    """Reproduce and compare the already frozen assignment, without writing it.

    This is the existing ``--verify`` path: it reads original public-history
    profiles and normalized replay metadata, not native observation streams.
    No seed, split, source record or assignment is changed or generated on disk.
    """
    reader = _MetadataReader()
    manifest = reader.read(manifest_reference)
    if manifest.get("schema") != "gkms.full-corpus-player-split-manifest.v1":
        raise ValueError("The existing full-corpus split manifest is required")
    for reference in manifest["code"].values():
        reader.binary(reference)
    preparer = manifest["code"]["preparer"]
    if Path(preparer["path"]).resolve() != _ROOT / "scripts/prepare_full_corpus_player_split.py":
        raise ValueError("Split verification must use the existing repository preparer")
    policy = reader.read(manifest["policy"])
    for reference in policy["inputs"].values():
        reader.binary(reference)
    stored = {key: reader.read(reference) for key, reference in manifest["artifacts"].items()}
    verifier = _load_split_verifier()
    actual = verifier.prepare(policy)
    if set(actual) != set(stored) or any(not _same(actual[key], stored[key]) for key in actual):
        raise ValueError("Fixed player assignment differs from its original policy/source evidence")
    overlap = verifier.verify_no_overlap(stored["stages"], stored["assignment"])
    if (manifest["source_records"] != len(stored["stages"])
            or manifest["counts"] != dict(Counter(x["new_split"] for x in stored["assignment"]["stages"]))):
        raise ValueError("Fixed split manifest counts differ")
    return {"manifest": deepcopy(dict(manifest_reference)), "artifacts": stored,
            "verification": overlap, "file_stamps": tuple(reader.stamps.values()),
            "new_split_created": False, "bc_fit_admitted": False}


def _indexed_closure(index_path):
    index = json.loads(Path(index_path).read_bytes())
    try:
        return index["current_phase"]["active_work_package"]["full_primary_feature_inventory"]["closure_manifest"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Current index has no sealed primary inventory closure") from exc


def _check_anchor(reference, index_path):
    expected = _indexed_closure(index_path)
    if (Path(reference["path"]).resolve() != Path(expected["path"]).resolve()
            or reference.get("sha256") != expected.get("sha256")):
        raise ValueError("BC input differs from the current indexed inventory closure")


def _signature(payload, stamps):
    return hmac.new(_SECRET, payload + canonical_json_bytes(stamps), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class OriginalPcBcDataset:
    """Process-local verified metadata; returned records are independent copies."""
    _payload: bytes
    _stamps: tuple
    _seal: str

    @property
    def summary(self):
        return deepcopy(_dataset_payload(self)["summary"])


def _check_dataset_stamps(dataset):
    if not isinstance(dataset, OriginalPcBcDataset):
        raise ValueError("Use a verified original-PC BC metadata context")
    if any(_stamp(stamp[0]) != stamp for stamp in dataset._stamps):
        raise ValueError("Sealed BC metadata changed; reopen its verified context")


def _dataset_payload(dataset):
    if (not isinstance(dataset, OriginalPcBcDataset)
            or not hmac.compare_digest(dataset._seal, _signature(dataset._payload, dataset._stamps))):
        raise ValueError("Use a verified original-PC BC metadata context")
    _check_dataset_stamps(dataset)
    payload = json.loads(dataset._payload)
    _check_anchor(payload["closure_manifest"], payload["index_path"])
    return payload


def _unique(rows, key, description):
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) or key not in row for row in rows):
        raise ValueError("Malformed " + description)
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("Duplicate " + description)
    return result


def open_original_pc_bc_dataset(closure_manifest_reference, *, index_path=None):
    """Open the complete sealed case denominator; do not read native streams.

    ``index_path`` is the current project index (also used for quarantine), not
    an output path. Its closure reference is only an anchor; all actual closure,
    selection and fixed-assignment metadata are independently hash-checked.
    """
    index_path = Path(index_path or CURRENT_INDEX).resolve()
    _check_anchor(closure_manifest_reference, index_path)
    reader = _MetadataReader()
    manifest = reader.read(closure_manifest_reference)
    if manifest.get("schema") != "gkms.shared-primary-feature-inventory-closure-manifest.v1":
        raise ValueError("A sealed primary feature inventory closure is required")
    closure = reader.read(manifest["final_report"])
    if (closure.get("schema") != "gkms.shared-primary-feature-inventory-closure.v1"
            or closure.get("status") != "file-only-final-inventory-closure-complete"
            or closure.get("inventory_incomplete_cases") != 0
            or closure.get("training_admitted") is not False):
        raise ValueError("Inventory closure is incomplete or its scope changed")
    if not _same(manifest["fixed_split_manifest"], closure["fixed_split_manifest"]):
        raise ValueError("Closure fixed split references differ")
    split = verify_fixed_player_split(closure["fixed_split_manifest"])
    reader.stamps.update({s[0]: s for s in split["file_stamps"]})
    selection = reader.read(closure["selection"])
    metadata = reader.read(closure["source_metadata"])
    if not _same(metadata["inputs"]["split_manifest"], split["manifest"]):
        raise ValueError("Selection metadata uses a different fixed split")
    work = reader.read(metadata["inputs"]["fixed_work_candidates"])
    if not _same(selection["selected"], [{**row, "event_bytes": row["whole_stream_bytes"]} for row in metadata["cases"]]):
        raise ValueError("Original selection differs from preparation metadata")
    selected = _unique(selection["selected"], "episode_id", "selected episode")
    if len({r["original_index"] for r in selected.values()}) != len(selected):
        raise ValueError("Duplicate original source index")
    work_cases = _unique(work["cases"], "episode_id", "fixed-work episode")
    distribution = reader.read(closure["artifacts"]["case_distribution.json"])
    cases = _unique(distribution, "episode_id", "closure episode")
    if set(cases) != set(selected) or set(cases) != set(work_cases):
        raise ValueError("Closure/selection/work case denominators differ")
    stages = _unique(split["artifacts"]["stages"], "stage_id", "fixed source stage")
    assigned = _unique(split["artifacts"]["assignment"]["stages"], "stage_id", "fixed assigned stage")
    groups = _unique(split["artifacts"]["assignment"]["groups"], "group_id", "fixed player group")
    for episode, case in cases.items():
        row = selected[episode]
        for key in ("source", "fixed_split", "qualification", "event_index"):
            if not _same(row[key], case[key]) or not _same(row[key], work_cases[episode][key]):
                raise ValueError("Closure case differs from its original source/selection")
        if (type(row["original_index"]) is not int or row["original_index"] < 0
                or case["index"] != row["original_index"] or case["source_master_hash"] != row["source_master_hash"]
                or case["produce_id"] != row["produce_id"] or case["stage_type"] != row["stage_type"]
                or case["inventory_complete"] is not True):
            raise ValueError("Closure source identity or inventory scope differs")
        fixed = row["fixed_split"]
        stage, assignment, group = stages[fixed["stage_id"]], assigned[fixed["stage_id"]], groups[fixed["group_id"]]
        if (any(not _same(assignment[k], v) for k, v in fixed.items())
                or stage["original_episode_id"] != episode or stage["source"] != row["source"]
                or stage["source_master_hash"] != row["source_master_hash"]
                or stage["source_export_index"] != row["original_index"]
                or stage["whole_trajectory_id"] != fixed["whole_trajectory_id"]
                or stage["player_sha256"] != fixed["player_sha256"]
                or fixed["stage_id"] not in group["stage_ids"]
                or fixed["whole_trajectory_id"] not in group["whole_trajectory_ids"]
                or group["player_sha256"] != fixed["player_sha256"] or group["new_split"] != fixed["new_split"]):
            raise ValueError("Case contradicts its original player/whole-cultivation assignment")
        case["selection"] = row
        case["original_stage"] = stage
    denominators = metadata["denominators"]
    main_count = sum(r["main_decisions"] for r in selected.values())
    if (len(cases) != closure["cases"] or len(cases) != manifest["inventory_cases"]
            or len(cases) != denominators["selected_scoped_pass_cases"]
            or main_count != closure["encoded_primary_rows"] or main_count != closure["label_bound_primary_rows"]
            or main_count != manifest["encoded_and_label_bound_rows"]
            or main_count != denominators["selected_primary_decisions"]
            or main_count + closure["excluded_primary_rows"] != denominators["all_capture_primary_decisions"]
            or len(cases) + closure["excluded_cases"] != denominators["all_capture_cases"]):
        raise ValueError("Original BC case/main-decision denominators differ")
    payload = {"closure_manifest": deepcopy(dict(closure_manifest_reference)), "index_path": str(index_path),
               "fixed_split_manifest": split["manifest"], "selection_reference": closure["selection"],
               "source_metadata_reference": closure["source_metadata"], "cases": list(cases.values()),
               "summary": {"schema": SCHEMA, "cases": len(cases), "main_decisions": main_count,
                   "candidates": closure["candidates"], "denominators": denominators,
                   "fixed_split_verification": split["verification"], "new_split_created": False,
                   "eligibility_scope": ELIGIBILITY_SCOPE, "bc_fit_admitted": False,
                   "action_after_state_observed": False, "RL_transition_qualified": False,
                   "raw_event_files_read": 0, "vectors_encoded": 0}}
    quarantine = load_current_training_quarantine(index_path)
    for case in payload["cases"]:
        _check_case_quarantine(payload, case, quarantine=quarantine)
    raw = canonical_json_bytes(payload)
    stamps = tuple(reader.stamps.values())
    return OriginalPcBcDataset(raw, stamps, _signature(raw, stamps))


def _check_case_quarantine(payload, case, *, quarantine=None):
    if quarantine is None:
        quarantine = load_current_training_quarantine(payload["index_path"])
    if quarantine is None:
        raise ValueError("Current indexed training quarantine is unavailable")
    fixed, stage = case["fixed_split"], case["original_stage"]
    if (not _sha(fixed.get("player_sha256")) or fixed.get("new_split") not in ("train", "validation", "test")
            or stage.get("player_witness_verified") is not True or stage.get("quarantine_excluded") is not False
            or (stage.get("protected_role") in ("evaluation_A", "reserved_evaluation_C") and fixed["new_split"] == "train")):
        raise ValueError("Unknown, blocked, quarantined or protected source cannot enter BC refs")
    matches = quarantine.match({"identity": {"episode_id": case["episode_id"]}}, {},
                               {"file_sha256": case["source"]["sha256"]})
    if matches:
        raise ValueError("Current quarantine excludes BC source: " + ",".join(matches))
    return quarantine.reference()


def _case_metadata(payload, case):
    quarantine = _check_case_quarantine(payload, case)
    reader = _MetadataReader()
    selected = case["selection"]
    inventory = reader.read(case["case_report"])
    if (inventory.get("schema") != "gkms.shared-primary-case-feature-inventory.v1"
            or inventory.get("inventory_complete") is not True or inventory.get("error")
            or inventory.get("fatal_category") or inventory.get("training_admitted") is not False
            or any(not _same(inventory["source"][k], selected[k]) for k in _CASE_KEYS)
            or any(not _same(inventory[k], case[k]) for k in ("counts", "rows_artifact", "feature_contract"))):
        raise ValueError("Sealed case inventory/source/row references differ")
    source = reader.read(case["source"])
    qualification = reader.read(case["qualification"])
    index = reader.read(case["event_index"])
    contract = reader.read(case["feature_contract"])
    fixed = selected["fixed_split"]
    spec = qualification["specification"]
    if (qualification.get("status") != "file-validated"
            or any(qualification.get(k) is not True for k in _CAPTURE_REQUIREMENTS)
            or qualification.get("capture_qualification_gaps") != []
            or qualification.get("source_quarantined") is not False
            or qualification["episode_id"] != case["episode_id"]
            or qualification["original_index"] != selected["original_index"]
            or qualification["source"] != selected["source"]
            or qualification["event_index"] != selected["event_index"]
            or qualification["source_actions"] != selected["source_actions"]
            or qualification["strict_before_after_equal"] != selected["source_actions"]
            or qualification["main_decisions"] != selected["main_decisions"]
            or qualification["secondary_decisions"] != selected["secondary_decisions"]):
        raise ValueError("Original capture qualification does not bind this case")
    if (source["source"]["episode_id"] != case["episode_id"]
            or source["source"]["trajectory_id"] != fixed["whole_trajectory_id"]
            or source["expected"]["master_hash"] != selected["source_master_hash"]
            or source["produce_id"] != selected["produce_id"] or source["step_type"] != selected["stage_type"]
            or spec["source"] != selected["source"] or spec["trajectory_id"] != fixed["whole_trajectory_id"]
            or spec["index"] != selected["original_index"] or spec["episode_id"] != case["episode_id"]
            or spec["master_hash"] != selected["source_master_hash"] or spec["stage"] != selected["stage_type"]
            or spec["original_actions"] != selected["source_actions"]
            or spec["player"]["player_sha256"] != fixed["player_sha256"]
            or spec["original_split"] != case["original_stage"]["original_split_sidecar"]):
        raise ValueError("Original source/player/Master/split identity differs")
    identity = case["original_stage"]["identity"]
    if (case["flow"] != "|".join((selected["produce_id"], PLAN_BY_NATIVE_VALUE[identity["planType"]],
                                     EFFECT_BY_NATIVE_VALUE[identity["mainEffectType"]]))
            or STAGE_BY_NATIVE_VALUE[identity["stepType"]] != STAGE_BY_NAME.get(selected["stage_type"])):
        raise ValueError("BC flow/stage differs from the original grouped source")
    body = {k: v for k, v in contract.items() if k != "contract_sha256"}
    if (contract.get("contract_sha256") != hashlib.sha256(canonical_json_bytes(body)).hexdigest()
            or contract.get("diagnostic_only") is not True
            or contract["observed_runtime_grow_contract"]["source_master_hash"] != selected["source_master_hash"]):
        raise ValueError("Prior diagnostic feature contract/source binding differs")
    if (index.get("schema") != "gkms.qualified-observer-decision-event-index.v1"
            or index.get("whole_event_stream_hashed") is not True
            or index["case_id"] != inventory["case_id"] or index["case_id"] != case["episode_id"] + "/public-history"
            or index["source"] != case["source"] or index["inputs_receipt"] != qualification["inputs_receipt"]
            or index["observer_validation"] != qualification["observer_validation"]):
        raise ValueError("Original decision-event index binding differs")
    event_file = index["events"]
    file_stamp = _stamp(event_file["path"])
    if list(file_stamp[1:]) != index["file_stamp"] or file_stamp[1] != event_file["bytes"]:
        raise ValueError("Original event file identity changed; no fresh stream verification claimed")
    events = index["decision_events"]
    actions = source["expected"]["actions"]
    if (len(events) != selected["source_actions"] or len(actions) != len(events)
            or any(type(e.get("source_order")) is not int for e in events)
            or [e["source_order"] for e in events] != list(range(len(actions)))
            or any(type(e.get("event_order")) is not int for e in events)
            or [e["event_order"] for e in events] != sorted({e["event_order"] for e in events})):
        raise ValueError("Original decision/source order coverage is incomplete or duplicated")
    previous_end = 0
    for event, action in zip(events, actions):
        if (type(event.get("kind")) is not int or event["kind"] not in (1, 2)
                or type(event.get("phase")) is not int or event["phase"] != 0
                or Path(event["path"]).resolve() != Path(event_file["path"]).resolve()
                or type(event.get("byte_offset")) is not int or event["byte_offset"] < previous_end
                or type(event.get("bytes")) is not int or event["bytes"] <= 0
                or event["byte_offset"] + event["bytes"] > event_file["bytes"] or not _sha(event.get("sha256"))
                or type(action.get("order")) is not int or action["order"] != event["source_order"]
                or (event["kind"] == 2) != (action["action_type"] == "effect-card-select")):
            raise ValueError("Decision span/action is outside its exact original index")
        previous_end = event["byte_offset"] + event["bytes"]
    mains = [e for e in events if e["kind"] == 1]
    if len(mains) != selected["main_decisions"]:
        raise ValueError("Original main-decision denominator differs")
    return reader, inventory, source, mains, quarantine


def iter_original_pc_bc_cases(dataset) -> Iterator[dict]:
    """Yield metadata-checked cases; ``selection`` is the exact old worker input."""
    payload = _dataset_payload(dataset)
    for case in payload["cases"]:
        _check_dataset_stamps(dataset)
        _, _, _, _, quarantine = _case_metadata(payload, case)
        yield {"schema": CASE_SCHEMA, "selection": deepcopy(case["selection"]),
               "inventory_case_report": deepcopy(case["case_report"]),
               "prior_rows_artifact": deepcopy(case["rows_artifact"]),
               "prior_feature_contract": deepcopy(case["feature_contract"]),
               "closure_manifest": deepcopy(payload["closure_manifest"]),
               "fixed_split_manifest": deepcopy(payload["fixed_split_manifest"]),
               "current_quarantine": quarantine, "eligibility_scope": ELIGIBILITY_SCOPE,
               "bc_fit_admitted": False, "raw_event_span_verified_here": False}


def iter_original_pc_bc_decision_refs(dataset, *, episode_id=None) -> Iterator[dict]:
    """Validate each complete case's stored rows before yielding any of its refs.

    The old hashes are provenance only. Native span content and new v5 features
    are verified by the consuming feature worker, never synthesized here.
    """
    payload = _dataset_payload(dataset)
    cases = [c for c in payload["cases"] if episode_id is None or c["episode_id"] == episode_id]
    if not cases:
        raise ValueError("Episode is outside this sealed BC case index")
    for case in cases:
        _check_dataset_stamps(dataset)
        reader, inventory, source, mains, quarantine = _case_metadata(payload, case)
        rows = [json.loads(line) for line in reader.binary(case["rows_artifact"]).splitlines() if line.strip()]
        if len(rows) != len(mains):
            raise ValueError("Stored BC rows do not cover the complete original main-decision case")
        result = []
        candidates_count = 0
        for row, event in zip(rows, mains):
            action = source["expected"]["actions"][event["source_order"]]
            if (row.get("encoded") is not True or row.get("training_admitted") is not False
                    or row.get("raw_state_changed") is not False
                    or not _same(row["event_reference"], event)
                    or type(row.get("source_order")) is not int or row["source_order"] != event["source_order"]
                    or type(row.get("event_order")) is not int or row["event_order"] != event["event_order"]
                    or not _same(row["source_action"], action)
                    or any(not _sha(row.get(key)) for key in ("raw_state_sha256", "feature_sha256",
                        "context_tokens_sha256", "candidate_tokens_sha256", "semantics_sha256"))):
                raise ValueError("Stored BC row/source/span/hash binding differs")
            bindings, target = row["candidate_bindings"], row["label_index"]
            if (not isinstance(bindings, list) or not bindings or type(target) is not int or not 0 <= target < len(bindings)
                    or any(not isinstance(b, Mapping) or b.get("kind") not in ("use-hand", "use-drink", "turn-end")
                        or type(b.get("slot")) is not int or b["slot"] < 0
                        or b["kind"] == "turn-end" and b["slot"] != 0 for b in bindings)
                    or len({(b["kind"], b["slot"]) for b in bindings}) != len(bindings)
                    or sum(b["kind"] == "turn-end" and b["slot"] == 0 for b in bindings) != 1
                    or len(row["candidate_token_counts"]) != len(bindings)
                    or any(type(n) is not int or n <= 0 for n in row["candidate_token_counts"])
                    or type(row.get("context_token_count")) is not int or row["context_token_count"] <= 0):
                raise ValueError("Stored candidate binding/count/target is invalid or ambiguous")
            indexes = action.get("indexes")
            matches = [i for i, b in enumerate(bindings) if b["kind"] == action["action_type"]
                       and indexes == ([] if b["kind"] == "turn-end" else [b["slot"]])]
            if (not isinstance(indexes, list) or any(type(i) is not int for i in indexes)
                    or matches != [target]):
                raise ValueError("Original source label has no unique matching stored candidate")
            candidates_count += len(bindings)
            result.append({"schema": DECISION_SCHEMA, "episode_id": case["episode_id"],
                "source_stage_id": case["fixed_split"]["stage_id"], "fixed_split": deepcopy(case["fixed_split"]),
                "source": deepcopy(case["source"]), "source_master_hash": case["source_master_hash"],
                "produce_id": case["produce_id"], "stage_type": case["stage_type"],
                "qualification": deepcopy(case["qualification"]), "event_index": deepcopy(case["event_index"]),
                "event_reference": deepcopy(event), "source_order": event["source_order"],
                "event_order": event["event_order"], "source_action": deepcopy(action), "label_index": target,
                "candidate_bindings": deepcopy(bindings), "raw_state_sha256": row["raw_state_sha256"],
                "prior_feature_evidence": {"rows_artifact": deepcopy(case["rows_artifact"]),
                    "feature_contract": deepcopy(case["feature_contract"]),
                    **{k: row[k] for k in ("feature_sha256", "context_tokens_sha256", "candidate_tokens_sha256", "semantics_sha256")}},
                "closure_manifest": deepcopy(payload["closure_manifest"]), "current_quarantine": quarantine,
                "eligibility_scope": ELIGIBILITY_SCOPE, "bc_fit_admitted": False,
                "raw_event_span_verified_here": False, "action_after_state_observed": False,
                "RL_transition_qualified": False})
        if (candidates_count != inventory["counts"]["candidates"]
                or len(result) != inventory["counts"]["encoded_rows"]
                or len(result) != inventory["counts"]["label_bound_rows"]):
            raise ValueError("Stored BC case row/candidate counts differ")
        # Do not yield a valid-looking prefix if the remaining case is corrupt.
        _check_case_quarantine(payload, case)
        _check_dataset_stamps(dataset)
        yield from result


def write_original_pc_bc_source_index(dataset, output):
    """Write a new metadata index; a manifest is emitted only after all checks.

    No runner, native read, model, new split or feature vector is created.
    Partial files remain unsealed if validation fails; existing outputs are
    never overwritten. The manifest retains the original selection reference.
    """
    payload = _dataset_payload(dataset)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = list(iter_original_pc_bc_cases(dataset))
    cases_path, rows_path = output / "cases.json", output / "decisions.jsonl"
    cases_path.write_bytes(canonical_json_bytes(cases) + b"\n")
    rows = candidates = 0
    split_counts = Counter()
    with rows_path.open("xb") as stream:
        for row in iter_original_pc_bc_decision_refs(dataset):
            stream.write(canonical_json_bytes(row) + b"\n")
            rows += 1
            candidates += len(row["candidate_bindings"])
            split_counts[row["fixed_split"]["new_split"]] += 1
    summary = payload["summary"]
    if (len(cases), rows, candidates) != (summary["cases"], summary["main_decisions"], summary["candidates"]):
        raise ValueError("Complete BC reference index denominator differs")
    _dataset_payload(dataset)
    current = load_current_training_quarantine(payload["index_path"])
    for case in payload["cases"]:
        _check_case_quarantine(payload, case, quarantine=current)

    def reference(path):
        hasher = hashlib.sha256()
        length = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                hasher.update(chunk)
                length += len(chunk)
        return {"path": str(path), "sha256": hasher.hexdigest(), "bytes": length}

    manifest = {"schema": MANIFEST_SCHEMA, "closure_manifest": payload["closure_manifest"],
        "implementation": reference(Path(__file__).resolve()),
        "fixed_split_manifest": payload["fixed_split_manifest"], "source_selection": payload["selection_reference"],
        "source_preparation_metadata": payload["source_metadata_reference"],
        "cases": reference(cases_path), "decisions": reference(rows_path),
        "counts": {"cases": len(cases), "main_decisions": rows, "candidates": candidates,
                   "fixed_split_rows": dict(split_counts)}, "denominators": summary["denominators"],
        "current_quarantine": current.reference(), "fixed_split_verification": summary["fixed_split_verification"],
        "eligibility_scope": ELIGIBILITY_SCOPE, "bc_fit_admitted": False, "new_split_created": False,
        "raw_event_files_read": 0, "raw_event_spans_read": 0, "vectors_encoded": 0,
        "action_after_state_observed": False, "RL_transition_qualified": False}
    manifest_path = output / "manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
    return reference(manifest_path)
