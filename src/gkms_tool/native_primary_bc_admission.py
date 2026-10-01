"""Explicit BC admission for complete, source-bound native v5 packed datasets.

Original representation/storage receipts stay diagnostic. This gate verifies
the entire fixed source scope and returns a process-local sealed capability for
the new BC trainer only. It never reads native event streams or grants RL,
runtime-Master compatibility, model activation, or policy-quality acceptance.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from . import packed_primary_features as packed
from .canonical_training_labels import STAGE_BY_NAME
from .native_structure_contract_set import validate_native_structure_contract_set, resolve_native_structure_contract
from .original_pc_bc_dataset import open_original_pc_bc_dataset, iter_original_pc_bc_cases
from .training_artifact_io import canonical_json_bytes
from .training_quarantine import CURRENT_INDEX, load_current_training_quarantine

SCHEMA = "gkms.native-primary-bc-admission.v1"
_SOURCE_INDEX_SHA = "bc5a2c01292670f10672dcc71320cb4bdc3306f9839e8e03a05b1584986aaee4"
_EXPECTED_COUNTS = (2810, 70888, 394544)
_EXPECTED_ROUTE_COUNTS = (4, 7)
_SECRET = secrets.token_bytes(32)
_AUTHORITY = object()
_MODE = "native-structure-v5"
_ROOT = Path(__file__).resolve().parents[2]


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _same(a, b):
    return canonical_json_bytes(a) == canonical_json_bytes(b)


def _stamp(path):
    path = Path(path).resolve()
    s = path.stat()
    return (str(path), s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class _Checks:
    def __init__(self):
        self.files = {}
        self.stamps = {}

    def pin(self, reference):
        if (not isinstance(reference, Mapping) or not isinstance(reference.get("path"), str)
                or not _sha(reference.get("sha256"))):
            raise ValueError("Hash-bound admission evidence reference required")
        path = Path(reference["path"]).resolve()
        if path.name == "observed_steps.jsonl":
            raise ValueError("Admission never reads original native event streams")
        stamp = _stamp(path)
        key = str(path)
        if key in self.files:
            actual = self.files[key]
            if self.stamps[key] != stamp:
                raise ValueError("Verified admission evidence changed: " + key)
        else:
            h = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    h.update(chunk)
            actual = {"path": key, "sha256": h.hexdigest(), "bytes": stamp[1]}
            if _stamp(path) != stamp:
                raise ValueError("Admission evidence changed while hashing")
            self.files[key], self.stamps[key] = actual, stamp
        if (reference["sha256"] != actual["sha256"]
                or "bytes" in reference and (type(reference["bytes"]) is not int or reference["bytes"] != actual["bytes"])):
            raise ValueError("Admission evidence SHA/length differs: " + key)
        return deepcopy(actual)

    def path(self, path):
        path = Path(path).resolve()
        stamp = _stamp(path)
        if path.name == "observed_steps.jsonl":
            raise ValueError("Admission never reads original native event streams")
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024**2), b""):
                h.update(chunk)
        if _stamp(path) != stamp:
            raise ValueError("Admission evidence changed while hashing")
        reference = {"path": str(path), "sha256": h.hexdigest(), "bytes": stamp[1]}
        self.files[str(path)], self.stamps[str(path)] = reference, stamp
        return deepcopy(reference)

    def read(self, reference):
        pin = self.pin(reference)
        if pin["bytes"] > 64 * 1024**2:
            raise ValueError("Use streaming rows for large admission metadata")
        return json.loads(Path(pin["path"]).read_bytes())

    def read_path(self, path):
        reference = self.path(path)
        return self.read(reference), reference

    def same_file_bytes(self, a, b):
        left, right = self.pin(a), self.pin(b)
        if (left["sha256"], left["bytes"]) != (right["sha256"], right["bytes"]):
            raise ValueError("Frozen evidence copies differ")

    def check_stamps(self):
        if any(_stamp(path) != stamp for path, stamp in self.stamps.items()):
            raise ValueError("Admission evidence changed before verification completed")


class NativePrimaryBcAdmissionBlocked(ValueError):
    def __init__(self, message, *, report_reference=None):
        super().__init__(message)
        self.report_reference = deepcopy(report_reference)
        self.report_ref = deepcopy(report_reference)


def _quarantine(index_path, cases):
    current = load_current_training_quarantine(index_path=index_path)
    if current is None:
        raise ValueError("Current indexed quarantine is unavailable")
    for case in cases:
        fixed = case["fixed_split"]
        if not _sha(fixed.get("player_sha256")) or fixed.get("new_split") not in ("train", "validation", "test"):
            raise ValueError("Unknown or blocked player/split cannot enter BC admission")
        matches = current.match({"identity": {"episode_id": case["episode_id"]}}, {},
                                {"file_sha256": case["source"]["sha256"]})
        if matches:
            raise ValueError("Current quarantine excludes BC source: " + ",".join(matches))
    return current.reference()


def _verify_packages(contract_set, checks):
    from .native_structure_features import load_native_structure_package, NativeStructureCardSemanticFeatures
    from .shared_bc_features import validate_shared_primary_contract
    validate_native_structure_contract_set(contract_set)
    masters = {r["source_master_hash"] for r in contract_set["routes"]}
    if (len(masters), len(contract_set["routes"])) != _EXPECTED_ROUTE_COUNTS:
        raise ValueError("The complete four-Master/seven-route representation set is required")
    checks.pin({"path": str(Path(__file__).with_name("native_structure_contract_set.py")),
                "sha256": contract_set["router_source_sha256"]})
    for name, sha in contract_set["common_representation"]["shared"]["encoder_source_hashes"].items():
        checks.pin({"path": str(Path(__file__).with_name(name)), "sha256": sha})
    for contract in contract_set["contracts"].values():
        native = contract["native_structure_contract"]
        package = load_native_structure_package(source_master_hash=native["source_master_hash"],
            source_inventory_reference=native["source_inventory_reference"],
            native_schema_reference=native["native_schema_reference"],
            catalog_reference=native["catalog_reference"], snapshot_reference=native["snapshot_reference"])
        features = NativeStructureCardSemanticFeatures(package,
            active_status_contract=contract.get("observed_active_status_contract"))
        validate_shared_primary_contract(contract, features)
        for key, value in native.items():
            if key.endswith("_reference") and isinstance(value, Mapping):
                checks.pin(value)
        for reference in native["supplemental_table_references"].values():
            checks.pin(reference)
        proof = checks.read(native["native_schema_reference"])
        checks.pin(proof["source_image"])
        checks.pin(proof["source_metadata"])
    return contract_set


def _verify_execution(run, config, state, checks):
    implementation, implementation_ref = checks.read_path(run / "implementation_manifest.json")
    execution = checks.read(state["execution_pins"])
    for row in implementation["files"]:
        checks.pin(row["frozen"])
    for reference in execution["pins"]:
        checks.pin(reference)
    for key in ("program", "scheduler", "explicit_worker_entry"):
        if isinstance(execution.get(key), Mapping):
            checks.pin(execution[key])
    checks.same_file_bytes(config["worker_entry"], execution["program"])
    implementation_files = {str(Path(x["frozen"]["path"]).resolve()): x["frozen"]["sha256"] for x in implementation["files"]}
    for reference in (config["worker_entry"], checks.path(run / "config.json")):
        if implementation_files.get(str(Path(reference["path"]).resolve())) != reference["sha256"]:
            raise ValueError("Run config/worker is outside its frozen implementation")
    contract_set = checks.read(config["representation"]["contract_set"])
    hashes = dict(contract_set["common_representation"]["shared"]["encoder_source_hashes"])
    hashes["native_structure_contract_set.py"] = contract_set["router_source_sha256"]
    hashes["packed_primary_features.py"] = checks.path(Path(packed.__file__).resolve())["sha256"]
    for name, sha in hashes.items():
        path = (run / "frozen/src/gkms_tool" / name).resolve()
        if implementation_files.get(str(path)) != sha:
            raise ValueError("Executed frozen encoder/packed code differs from its representation contract: " + name)
        checks.pin({"path": str(path), "sha256": sha})
    return execution, implementation_ref


def _verify_adoption(result, feature_run, config, checks):
    adopted = result.get("adopted_from")
    if adopted is None:
        if "adoption_reference" in result:
            raise ValueError("Incomplete adopted-result provenance")
        return
    old = checks.read(adopted["state_reference"])
    if (old.get("active_jobs") or old.get("status") not in ("pilot-complete", "completed", "stopped")
            or adopted["case_id"] != result["case_id"]
            or not _same({k: v for k, v in result.items() if k not in ("adopted_from", "adoption_reference")},
                         old["completed"][result["case_id"]])
            or adopted["execution_pins_reference"] != old["execution_pins"]
            or adopted["report_reference"] != result["report"]
            or adopted["process_outcome_reference"] != result["process_outcome"]):
        raise ValueError("Adopted result differs from its exact original completed receipt")
    old_run = Path(adopted["state_reference"]["path"]).resolve().parent
    old_config, _ = checks.read_path(old_run / "config.json")
    _, implementation = _verify_execution(old_run, old_config, old, checks)
    if implementation != adopted["implementation_manifest_reference"]:
        raise ValueError("Adopted result implementation reference differs")
    checks.same_file_bytes(old_config["representation"]["contract_set"], config["representation"]["contract_set"])
    checks.same_file_bytes(old_config["selection"], config["selection"])
    adoption = checks.read(result["adoption_reference"])
    if adoption.get("encoder_changed") is not False or adoption.get("training_admitted") is not False:
        raise ValueError("Resource-only adoption changed encoder or admission scope")
    for key, value in adoption.items():
        if key.endswith("_reference") and isinstance(value, Mapping):
            checks.pin(value)
    expected_refs = {"old_state_reference": adopted["state_reference"],
        "old_execution_pins_reference": adopted["execution_pins_reference"],
        "old_implementation_manifest_reference": adopted["implementation_manifest_reference"],
        "new_config_reference": checks.path(feature_run / "config.json"),
        "new_implementation_manifest_reference": checks.path(feature_run / "implementation_manifest.json"),
        "new_selection_reference": config["selection"], "new_source_metadata_reference": config["source_metadata"],
        "new_contract_set_reference": config["representation"]["contract_set"]}
    for key, reference in expected_refs.items():
        checks.same_file_bytes(adoption[key], reference)
    matches = [r for r in adoption.get("cases", []) if r.get("case_id") == result["case_id"]]
    if len(matches) != 1:
        raise ValueError("Adoption ledger does not uniquely contain the original case")
    row = matches[0]
    original = old["completed"][result["case_id"]]
    if (row["old_result_canonical_sha256"] != hashlib.sha256(canonical_json_bytes(original)).hexdigest()
            or row["old_report_reference"] != result["report"]
            or row["process_outcome_reference"] != result["process_outcome"]
            or row["counts"] != result["counts"] or row["fixed_split"] != result["fixed_split"]):
        raise ValueError("Adoption ledger source/result/packed witnesses differ")
    checks.same_file_bytes(row["packed_features"], result["packed_features"])
    report = checks.read(result["report"])
    if row["source"] != report["source"]["source"] or row["feature_contract"] != report["feature_contract"]:
        raise ValueError("Adoption source/feature contract differs")
    pairs = adoption.get("freeze_file_pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("Resource adoption lacks explicit frozen encoder file pairs")
    for pair in pairs:
        checks.same_file_bytes(pair["source"], pair["frozen"])


def _packed_rows(reader, checks):
    """Verify each derived shard once, without allocating global padded arrays."""
    manifest = reader.manifest
    for shard in manifest["shards"]:
        directory = reader.path.parent / shard["directory"]
        for reference in shard["files"].values():
            checks.pin({"path": str(directory / reference["name"]),
                        "sha256": reference["sha256"], "bytes": reference["bytes"]})
        arrays = packed._load_shard(reader.path.parent, shard, manifest)
        try:
            with (directory / "metadata.jsonl").open("rb") as stream:
                for local in range(shard["row_count"]):
                    global_index = shard["row_start"] + local
                    begin, end = (int(x) for x in arrays["context_offsets"][local:local + 2])
                    first, last = (int(x) for x in arrays["row_candidate_offsets"][local:local + 2])
                    context = arrays["context_values"][begin:end]
                    candidates = []
                    for candidate in range(first, last):
                        left, right = (int(x) for x in arrays["candidate_token_offsets"][candidate:candidate + 2])
                        candidates.append(arrays["candidate_values"][left:right])
                    metadata = packed._row_metadata(stream, arrays["metadata_offsets"], local, global_index,
                        len(candidates), manifest["metadata_contract"]["feature_contract_sha256"])
                    yield global_index, context, candidates, metadata
        finally:
            packed._close_arrays(arrays)


def _check_row(row, expected, packed_metadata, context, candidates, contract, selected, closure_reference, expected_flow):
    if not _same({k: v for k, v in row.items() if k != "packed_row"}, packed_metadata):
        raise ValueError("Packed metadata differs from the same-order feature row receipt")
    for key in ("event_reference", "source_order", "event_order", "source_action", "label_index",
                "candidate_bindings", "raw_state_sha256"):
        if not _same(row[key], expected[key]):
            raise ValueError("New packed row differs from its original BC source/label: " + key)
    if (row.get("encoded") is not True or row.get("training_admitted") is not False
            or row.get("raw_state_changed") is not False or row.get("primary_decision_only") is not True
            or row.get("action_after_state_observed") is not False or row.get("RL_transition_qualified") is not False
            or row.get("feature_mode") != _MODE or row.get("feature_contract_sha256") != contract["contract_sha256"]
            or row.get("current_graph_complete") is not True
            or row.get("representation_complete") is not True
            or any(row.get(key) != [] for key in ("mandatory_gaps", "native_structure_gaps", "shared_contract_gaps", "gap_ids"))):
        raise ValueError("Mandatory native representation gaps or incomplete BC row")
    scope = row.get("source_verification", {})
    if (scope.get("scope") != "prior-whole-stream-qualified+current-indexed-main-span-sha"
            or scope.get("all_consumed_spans_hash_required") is not True
            or scope.get("primary_BC_only") is not True or scope.get("current_whole_stream_rehashed") is not False
            or scope.get("closure_reference") != closure_reference
            or scope.get("episode_id") != selected["episode_id"]
            or scope.get("original_index") != selected["original_index"]
            or scope.get("event_index_reference") != selected["event_index"]):
        raise ValueError("Packed row lacks its original selected-span verification witness")
    if (row["candidate_count"] != len(candidates) or len(row["candidate_bindings"]) != len(candidates)
            or row["context_token_count"] != len(context)
            or row["stage"] != STAGE_BY_NAME[selected["stage_type"]] or row["flow"] != expected_flow
            or type(row["label_index"]) is not int or not 0 <= row["label_index"] < len(candidates)):
        raise ValueError("Packed row candidate/context/stage dimensions differ")
    actual_context = hashlib.sha256(json.dumps(context.tolist(), separators=(",", ":")).encode()).hexdigest()
    actual_candidates = hashlib.sha256(json.dumps([x.tolist() for x in candidates], separators=(",", ":")).encode()).hexdigest()
    if (row.get("pointer_context_indices_sha256") != actual_context
            or row.get("pointer_candidate_indices_sha256") != actual_candidates):
        raise ValueError("Packed pointer byte values differ from the encoder row witnesses")
    if len({tuple(x.tolist()) for x in candidates}) != len(candidates):
        raise ValueError("Packed candidate hash collision")


def _verify_case(result, selected, expected_rows, config, contract_set, feature_run, checks):
    if (result.get("passed") is not True or result.get("status") != "inventory-complete"
            or result.get("process_exit_code") != 0 or result.get("fatal_category")
            or result.get("quarantine_skipped") is True or result.get("feature_mode") != _MODE):
        raise ValueError("Feature case failed, was quarantined, or lacks a successful v5 exit")
    _verify_adoption(result, feature_run, config, checks)
    checks.pin(selected["qualification"])
    report = checks.read(result["report"])
    if (not _same(report["source"], selected) or report.get("inventory_complete") is not True
            or report.get("feature_mode") != _MODE or report.get("error") or report.get("fatal_category")
            or report.get("training_admitted") is not False or report.get("quarantine_skipped") is True
            or report.get("model_fit") is not False or report.get("game_io") is not False
            or report["counts"] != result["counts"]
            or report["counts"].get("row_errors", 0) != 0
            or any(report["counts"].get(key) != selected["main_decisions"] for key in
                   ("encoded_rows", "label_bound_rows", "packed_rows", "representation_complete_rows"))):
        raise ValueError("Complete required case rows/features are not qualified")
    outcome = checks.read(result["process_outcome"])
    request = checks.read(outcome["request"])
    original_result = checks.read(outcome["result"])
    if (outcome.get("exit_code") != 0 or outcome.get("pid") != result["worker_pid"]
            or outcome.get("confirmed_process_exit_before_replacement") is not True
            or not _same(request["selected"], selected) or original_result["report"] != result["report"]
            or original_result["worker_pid"] != result["worker_pid"]
            or original_result["case_id"] != result["case_id"]
            or original_result.get("passed") is not True):
        raise ValueError("Feature worker source/result/exit ownership differs")
    if Path(request["output"]).resolve() != feature_run and "adopted_from" not in result:
        raise ValueError("Cross-run case reuse requires explicit verified adoption")
    checks.pin(request["program"])
    origin_config, _ = checks.read_path(Path(request["output"]) / "config.json")
    checks.same_file_bytes(request["program"], origin_config["worker_entry"])
    native = contract_set["common_representation"]["native"]
    contract = resolve_native_structure_contract(contract_set, source_master_hash=selected["source_master_hash"],
        produce_id=selected["produce_id"], native_core_sha256=native["native_core_sha256"],
        native_metadata_sha256=native["native_metadata_sha256"])
    if not _same(checks.read(report["feature_contract"]), contract):
        raise ValueError("Case used a different source-package feature contract")
    receipt = report["packed_features"]
    if not _same(receipt, result["packed_features"]):
        raise ValueError("Worker/result packed references differ")
    checks.pin(receipt)
    reader = packed.PackedPrimaryReader(receipt["path"], expected_sha256=receipt["sha256"])
    manifest = reader.manifest
    header = manifest["metadata_contract"]
    checks.same_file_bytes(header["contract_set"], config["representation"]["contract_set"])
    if (reader.row_count != len(expected_rows) or manifest.get("representation_complete") is not True
            or header["feature_contract_sha256"] != contract["contract_sha256"]
            or header["feature_contract"] != report["feature_contract"]
            or any(not _same(header[k], selected[k]) for k in
                   ("original_index", "episode_id", "source_master_hash", "fixed_split", "source", "qualification", "event_index"))
            or any(manifest[k] != contract_set["pointer_layout"][k] for k in ("context_buckets", "candidate_buckets"))):
        raise ValueError("Packed manifest differs from its required source/representation scope")
    index = checks.read(selected["event_index"])
    actual_stamp = _stamp(index["events"]["path"])
    if list(actual_stamp[1:]) != index["file_stamp"]:
        raise ValueError("Original native file identity changed since qualification")
    checks.stamps[actual_stamp[0]] = actual_stamp  # stat only; never read this stream.
    rows_ref = checks.pin(report["rows_artifact"])
    with Path(rows_ref["path"]).open("rb") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if len(rows) != len(expected_rows):
        raise ValueError("New feature rows do not cover every original main decision")
    source = checks.read(selected["source"])
    source_stage = source["contest_situation"]["stages"][source["source"]["stage_index"]]
    player = source_stage["selfSections"][source["source"]["section_index"]]["player"]
    expected_flow = "|".join((selected["produce_id"], source_stage["planType"], player["examEffectType"]))
    admitted = []
    iterator = _packed_rows(reader, checks)
    try:
        for local, context, candidates, metadata in iterator:
            row, expected = rows[local], expected_rows[local]
            _check_row(row, expected, metadata, context, candidates, contract, selected,
                       config["representation"]["closure_reference"], expected_flow)
            if row["packed_row"]["row_index"] != local or Path(row["packed_row"]["manifest_path"]).resolve() != reader.path:
                raise ValueError("Feature row packed location differs")
            admitted.append({"packed_manifest": deepcopy(receipt), "row_index": local,
                "label_index": row["label_index"], "flow": row["flow"], "stage": row["stage"],
                "action_kind": row["source_action"]["action_type"], "split": selected["fixed_split"]["new_split"],
                "whole_trajectory_id": selected["fixed_split"]["whole_trajectory_id"],
                "player_sha256": selected["fixed_split"]["player_sha256"], "episode_id": selected["episode_id"],
                "feature_contract_sha256": contract["contract_sha256"], "raw_state_sha256": row["raw_state_sha256"],
                "candidate_count": len(candidates), "source": deepcopy(selected["source"])})
    finally:
        iterator.close()
    if len(admitted) != selected["main_decisions"] or sum(x["candidate_count"] for x in admitted) != report["counts"]["candidates"]:
        raise ValueError("Packed required case totals differ")
    return admitted


def _verify(feature_run, contract_set_reference, source_index_reference, index_path):
    checks = _Checks()
    checks.path(Path(__file__).resolve())
    feature_run = Path(feature_run).resolve()
    config, config_ref = checks.read_path(feature_run / "config.json")
    state, state_ref = checks.read_path(feature_run / "state.json")
    if (config.get("feature_mode") != _MODE or state.get("status") != "completed"
            or state.get("active_jobs") != [] or state.get("fatal_errors") != []
            or state.get("total") != _EXPECTED_COUNTS[0]):
        completed = len(state.get("completed", {}))
        raise ValueError("The complete 2,810-case feature run is required; pilots/partial runs cannot admit BC; "
                         f"completed={completed}, remaining={max(0, _EXPECTED_COUNTS[0] - completed)}")
    report, report_ref = checks.read_path(feature_run / "report.json")
    if (report.get("status") != "completed"
            or report.get("remaining") != 0 or report.get("active_workers") != 0
            or report.get("processed") != _EXPECTED_COUNTS[0]):
        raise ValueError("The complete 2,810-case feature run is required; pilots/partial runs cannot admit BC")
    if source_index_reference.get("sha256") != _SOURCE_INDEX_SHA:
        raise ValueError("The sealed complete original BC source index is required")
    if Path(config["current_project_index_path"]).resolve() != Path(index_path).resolve():
        raise ValueError("Admission must reread the feature run's configured current project quarantine index")
    source_index = checks.read(source_index_reference)
    dimensions = tuple(source_index["counts"][k] for k in ("cases", "main_decisions", "candidates"))
    if source_index.get("bc_fit_admitted") is not False or dimensions != _EXPECTED_COUNTS:
        raise ValueError("Original BC source denominator/admission flags differ")
    representation = config["representation"]
    if (representation.get("diagnostic_only") is not True or representation.get("training_admitted") is not False
            or config.get("training_admitted") is not False or config.get("model_fit") is not False):
        raise ValueError("Original diagnostic run flags must remain unchanged")
    checks.same_file_bytes(representation["contract_set"], contract_set_reference)
    checks.same_file_bytes(representation["bc_source_index"], source_index_reference)
    checks.same_file_bytes(representation["closure_reference"], source_index["closure_manifest"])
    contract_set = _verify_packages(checks.read(contract_set_reference), checks)
    _verify_execution(feature_run, config, state, checks)
    selection = checks.read(config["selection"])["selected"]
    checks.same_file_bytes(config["selection"], source_index["source_selection"])
    metadata = checks.read(config["source_metadata"])
    if not _same(selection, [{**r, "event_bytes": r["whole_stream_bytes"]} for r in metadata["cases"]]):
        raise ValueError("Feature metadata changed original cases/source/splits")
    original = open_original_pc_bc_dataset(source_index["closure_manifest"], index_path=index_path)
    for stamp in original._stamps:
        if _stamp(stamp[0]) != stamp:
            raise ValueError("Original closure/group verification inputs changed")
        checks.path(stamp[0])
    source_cases = list(iter_original_pc_bc_cases(original))
    indexed = checks.read(source_index["cases"])
    if len(source_cases) != _EXPECTED_COUNTS[0] or len(indexed) != len(source_cases):
        raise ValueError("BC source cases do not cover the full fixed scope")
    for actual, saved, selected in zip(source_cases, indexed, selection):
        for key in ("selection", "inventory_case_report", "prior_rows_artifact", "prior_feature_contract",
                    "closure_manifest", "fixed_split_manifest"):
            if not _same(actual[key], saved[key]):
                raise ValueError("BC source index differs from verified original closure/group evidence")
        if not _same(actual["selection"], selected):
            raise ValueError("Feature selection is not the exact original full source scope")
    if len(state["completed"]) != len(selection) or len(report["case_receipts"]) != len(selection):
        raise ValueError("Feature completion ledger is incomplete")
    results = {r["case_id"]: r for r in report["case_receipts"]}
    if not _same(results, state["completed"]) or len(results) != len(selection):
        raise ValueError("Feature report/state completion ledgers differ")
    source_rows = checks.pin(source_index["decisions"])
    rows, errors, case_sources = [], [], []
    with Path(source_rows["path"]).open("rb") as stream:
        originals = (json.loads(line) for line in stream if line.strip())
        for selected in selection:
            expected = []
            for _ in range(selected["main_decisions"]):
                record = next(originals, None)
                if record is None or record["episode_id"] != selected["episode_id"] or not _same(record["fixed_split"], selected["fixed_split"]):
                    raise ValueError("Original source-index main row order/denominator differs")
                expected.append(record)
            case_sources.append({k: deepcopy(selected[k]) for k in ("episode_id", "source", "fixed_split")})
            try:
                _quarantine(index_path, [case_sources[-1]])
                result = results[selected["episode_id"] + "/public-history"]
                rows.extend(_verify_case(result, selected, expected, config, contract_set, feature_run, checks))
            except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
                errors.append({"episode_id": selected["episode_id"], "original_index": selected["original_index"],
                    "error": type(exc).__name__ + ": " + str(exc), "case_report": results.get(selected["episode_id"] + "/public-history", {}).get("report")})
        if next(originals, None) is not None:
            raise ValueError("Extra original BC source rows are outside the fixed scope")
    if errors:
        failure = ValueError("Required full-scope feature/source qualification failed")
        failure.case_errors = errors
        raise failure
    counts = (len(selection), len(rows), sum(x["candidate_count"] for x in rows))
    if counts != _EXPECTED_COUNTS:
        raise ValueError("Qualified vectors do not cover the complete planned source denominator")
    for key in ("encoded_rows", "label_bound_rows", "packed_rows", "representation_complete_rows"):
        if report["counts"].get(key) != counts[1]:
            raise ValueError("Run rollup differs from verified required rows: " + key)
    if report["counts"].get("candidates") != counts[2] or report["counts"].get("row_errors", 0) != 0:
        raise ValueError("Run candidate/error rollup differs")
    splits = dict(Counter(row["split"] for row in rows))
    if splits != source_index["counts"]["fixed_split_rows"] or any(splits.get(s, 0) <= 0 for s in ("train", "validation", "test")):
        raise ValueError("Fixed train/validation/test scope is incomplete or changed")
    current = _quarantine(index_path, case_sources)
    checks.check_stamps()
    summary = {"bc_fit_admitted": True, "cases": counts[0], "main_decisions": counts[1], "candidates": counts[2],
        "fixed_split_rows": splits, "mandatory_gap_rows": 0, "row_errors": 0,
        "pointer_layout": deepcopy(contract_set["pointer_layout"]), "contract_set_reference": deepcopy(contract_set_reference),
        "fixed_split_reference": deepcopy(source_index["fixed_split_manifest"]), "current_quarantine": current,
        "action_after_state_observed": False, "RL_transition_qualified": False,
        "current_runtime_master_compatibility_qualified": False, "automatic_model_activation_allowed": False,
        "qualification_scope": "full-original-source-primary-BC-only; separate from representation diagnostic flags",
        "native_event_files_read_by_gate": 0, "native_event_spans_read_by_gate": 0}
    return {"inputs": {"feature_run": str(feature_run), "config": config_ref, "state": state_ref, "report": report_ref,
                "contract_set": deepcopy(contract_set_reference), "source_index": deepcopy(source_index_reference), "index_path": str(Path(index_path).resolve())},
            "summary": summary, "contract_set": contract_set, "rows": rows, "cases": case_sources,
            "verified_files": list(checks.files.values()), "file_stamps": tuple(checks.stamps.values())}


def _sign(payload, stamps):
    return hmac.new(_SECRET, payload + canonical_json_bytes(stamps), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedNativePrimaryBcAdmission:
    _payload: bytes
    _stamps: tuple
    _seal: str

    def __init__(self, payload, stamps, *, _authority=None):
        if _authority is not _AUTHORITY:
            raise ValueError("Use the complete native BC admission verifier")
        raw = canonical_json_bytes(payload)
        object.__setattr__(self, "_payload", raw)
        object.__setattr__(self, "_stamps", stamps)
        object.__setattr__(self, "_seal", _sign(raw, stamps))

    @property
    def summary(self):
        return deepcopy(_context_payload(self)["summary"])

    @property
    def contract_set(self):
        return deepcopy(_context_payload(self)["contract_set"])

    @property
    def receipt_reference(self):
        return deepcopy(_context_payload(self)["receipt_reference"])


def _context_payload(context):
    try:
        verified = isinstance(context, VerifiedNativePrimaryBcAdmission) and hmac.compare_digest(
            context._seal, _sign(context._payload, context._stamps))
    except (AttributeError, TypeError):
        verified = False
    if not verified:
        raise ValueError("A verified native BC admission context is required; report flags are not admission")
    return json.loads(context._payload)


def validate_native_primary_bc_admission(context):
    payload = _context_payload(context)
    if any(_stamp(stamp[0]) != stamp for stamp in context._stamps):
        raise ValueError("Admitted source/vector evidence changed; rerun the complete gate")
    _quarantine(payload["inputs"]["index_path"], payload["cases"])
    if payload["summary"].get("bc_fit_admitted") is not True:
        raise ValueError("Native BC context has no fitting admission")
    return deepcopy(payload["summary"])


def iter_native_primary_bc_rows(context):
    validate_native_primary_bc_admission(context)
    for row in _context_payload(context)["rows"]:
        yield deepcopy(row)


def prepare_native_primary_bc_admission(feature_run, *, contract_set_reference,
        source_index_reference, output, index_path=None):
    """Save qualified/blocked evidence; return a capability only on full success."""
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    index_path = Path(index_path or CURRENT_INDEX).resolve()
    try:
        verified = _verify(feature_run, contract_set_reference, source_index_reference, index_path)
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        report = {"schema": SCHEMA, "status": "blocked", "bc_fit_admitted": False,
            "feature_run": str(Path(feature_run).resolve()), "contract_set_reference": deepcopy(contract_set_reference),
            "source_index_reference": deepcopy(source_index_reference), "error": type(exc).__name__ + ": " + str(exc),
            "case_errors": getattr(exc, "case_errors", []), "model_fit": False,
            "action_after_state_observed": False, "RL_transition_qualified": False}
        path = output / "report.json"
        path.write_bytes(canonical_json_bytes(report) + b"\n")
        reference = _Checks().path(path)
        raise NativePrimaryBcAdmissionBlocked(report["error"], report_reference=reference) from exc
    report = {"schema": SCHEMA, "status": "qualified-for-explicit-native-primary-BC", "bc_fit_admitted": True,
              "inputs": verified["inputs"], "summary": verified["summary"], "verified_files": verified["verified_files"],
              "original_diagnostic_flags_modified": False, "model_fit": False}
    path = output / "report.json"
    path.write_bytes(canonical_json_bytes(report) + b"\n")
    reference = _Checks().path(path)
    verified["receipt_reference"] = reference
    stamps = (*verified.pop("file_stamps"), _stamp(path))
    return VerifiedNativePrimaryBcAdmission(verified, stamps, _authority=_AUTHORITY)


def load_verified_native_primary_bc_admission(receipt_reference, *, index_path=None):
    """Reverify persisted inputs; a JSON qualified flag never creates a capability."""
    if isinstance(receipt_reference, VerifiedNativePrimaryBcAdmission):
        validate_native_primary_bc_admission(receipt_reference)
        return receipt_reference
    checks = _Checks()
    report = checks.read(receipt_reference)
    if report.get("schema") != SCHEMA or report.get("status") != "qualified-for-explicit-native-primary-BC":
        raise ValueError("A complete native BC qualification receipt is required")
    inputs = report["inputs"]
    verified = _verify(inputs["feature_run"], inputs["contract_set"], inputs["source_index"], index_path or inputs["index_path"])
    if (report.get("bc_fit_admitted") is not True or not _same(report["inputs"], verified["inputs"])
            or not _same(report["summary"], verified["summary"]) or not _same(report["verified_files"], verified["verified_files"])):
        raise ValueError("Persisted BC receipt differs from freshly verified complete evidence")
    verified["receipt_reference"] = deepcopy(dict(receipt_reference))
    stamps = (*verified.pop("file_stamps"), *checks.stamps.values())
    return VerifiedNativePrimaryBcAdmission(verified, stamps, _authority=_AUTHORITY)


validate_admitted_dataset = validate_native_primary_bc_admission
