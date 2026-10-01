"""Immutable native run segments and conservative same-run aggregation.

These are evidence indexes, not a second gameplay state owner or a training
promotion gate. Original partial/unknown outcomes remain visible. Only actual
recorder byte ranges are indexed; native state JSON is never forged into one.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from .run_identity import DEFAULT_RUN_ROOT, load_run
from .training_artifact_io import canonical_json_bytes
from .native_exam_archive import DEFAULT_COMMAND_ROOT, resolve_archived_exam_request


SCHEMA = "gkms.native-cultivation-segment.v1"
MERGED_SCHEMA = "gkms.native-cultivation-segments.v1"


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _run_directory(run_id, run_root):
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id or any(c in run_id for c in "/\\"):
        raise ValueError("native segment run_id must be one path component")
    root = Path(run_root).resolve()
    directory = (root / run_id).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("native segment path escapes the run root")
    return directory


def extract_native_stage_captures(result):
    """Read both current protocol and old capture alias at documented roots."""
    if not isinstance(result, Mapping):
        raise ValueError("native result must be an object")
    found, seen = [], set()
    def one(value, label):
        if not isinstance(value, Mapping):
            raise ValueError("native runtime stage capture must be an object")
        key = _digest(value)
        if key not in seen:
            seen.add(key)
            found.append({"location": label, "capture": deepcopy(dict(value))})
    def node(value, label):
        if not isinstance(value, Mapping):
            return
        for name in ("native_runtime_stage_capture", "native_capture"):
            if value.get(name) is not None:
                one(value[name], label + "." + name)
        values = value.get("native_runtime_stage_captures")
        if values is not None:
            if not isinstance(values, list):
                raise ValueError("native runtime stage captures must be an array")
            for index, capture in enumerate(values):
                one(capture, f"{label}.native_runtime_stage_captures[{index}]")
        if isinstance(value.get("orchestration"), Mapping):
            node(value["orchestration"], label + ".orchestration")
    node(result, "result")
    steps = result.get("steps", [])
    if not isinstance(steps, (list, tuple)):
        raise ValueError("native result steps must be an array")
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise ValueError("native result step must be an object")
        node(step.get("outcome"), f"result.steps[{index}].outcome")
    return tuple(found)


def _request_outcomes(value):
    result = {}
    def visit(node):
        if isinstance(node, Mapping):
            identity, status = node.get("request_id"), node.get("status", node.get("native_action_status"))
            if isinstance(identity, str) and identity and status in {"pending", "unknown", "submitted", "settled", "rejected"}:
                result.setdefault(identity, set()).add(status)
            for child in node.values():
                visit(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
    visit(value)
    return result


def write_native_cultivation_segment(run_id, result, *, produce_id, idol_card_id, session_generation,
                                      run_root=DEFAULT_RUN_ROOT):
    directory = _run_directory(run_id, run_root)
    run = load_run(run_id, root=Path(run_root))
    if run.produce_id != produce_id or run.idol_card_id != idol_card_id:
        raise ValueError("native segment belongs to another run mode/idol")
    if not isinstance(session_generation, str) or not session_generation:
        raise ValueError("native segment needs actual session_generation")
    value = result.to_dict() if callable(getattr(result, "to_dict", None)) else result
    if not isinstance(value, Mapping) or not isinstance(value.get("steps"), (list, tuple)):
        raise ValueError("native segment requires a complete result report")
    value = deepcopy(dict(value))
    captures = extract_native_stage_captures(value)
    for item in captures:
        binding = item["capture"].get("stage_binding")
        if isinstance(binding, Mapping) and binding.get("source_run_id") not in (None, run_id):
            raise ValueError("capture belongs to another source run")
    body = {"schema": SCHEMA, "run_id": run_id, "produce_id": produce_id,
            "idol_card_id": idol_card_id, "run_manifest_digest": _digest(run.to_dict()),
            "session_generation": session_generation, "result": value}
    identity = _digest(body)
    output = directory / "native_segments"
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"{identity}.json"
    if not path.exists():
        payload = canonical_json_bytes({**body, "segment_id": identity, "recorded_at_ns": time.time_ns()}) + b"\n"
        descriptor, temporary = tempfile.mkstemp(prefix=".native-segment-", suffix=".tmp", dir=output)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # Atomic create without replacing an immutable report.
            except FileExistsError:
                pass
        finally:
            Path(temporary).unlink(missing_ok=True)
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved_body = {key: val for key, val in saved.items() if key not in {"segment_id", "recorded_at_ns"}}
    if saved.get("segment_id") != identity or _digest(saved_body) != identity:
        raise ValueError("immutable native segment content conflict")
    outcomes = _request_outcomes(value)
    return {"schema": SCHEMA, "run_id": run_id, "segment_id": identity, "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "status": value.get("status"),
            "partial": value.get("status") != "completed", "capture_count": len(captures),
            "unresolved_request_ids": sorted(key for key, states in outcomes.items()
                if states & {"pending", "unknown", "submitted"} and "settled" not in states),
            "dataset_ready": False}


def _verified_native_segments(run_id, run_root):
    directory = _run_directory(run_id, run_root)
    run = load_run(run_id, root=Path(run_root))
    segments = []
    for path in (directory / "native_segments").glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        body = {key: val for key, val in value.items() if key not in {"segment_id", "recorded_at_ns"}}
        if (value.get("schema") != SCHEMA or value.get("run_id") != run_id
                or _digest(body) != value.get("segment_id") or path.stem != value.get("segment_id")):
            raise ValueError("native segment identity/hash mismatch")
        if value.get("run_manifest_digest") != _digest(run.to_dict()):
            raise ValueError("native segment was written for another run manifest")
        if value.get("produce_id") != run.produce_id or value.get("idol_card_id") != run.idol_card_id:
            raise ValueError("native segment mode/idol differs from its run manifest")
        segments.append(value)
    segments.sort(key=lambda value: (value["recorded_at_ns"], value["segment_id"]))
    return run, segments


def _request_records(value):
    if isinstance(value, Mapping):
        if isinstance(value.get("request_id"), str) and isinstance(value.get("action"), Mapping):
            yield value
        for child in value.values():
            yield from _request_records(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _request_records(child)


def merge_native_cultivation_segments(run_id, *, run_root=DEFAULT_RUN_ROOT, command_root=DEFAULT_COMMAND_ROOT):
    """Aggregate immutable ledgers; byte-gap/state validation is never bypassed."""
    run, segments = _verified_native_segments(run_id, run_root)
    steps, captures, seen_captures, requests, seen_steps = [], [], set(), {}, {}
    blockers, request_contexts = [], {}
    for segment in segments:
        for identity, states in _request_outcomes(segment["result"]).items():
            requests.setdefault(identity, set()).update(states)
        for record in _request_records(segment["result"]):
            request_contexts.setdefault(record["request_id"], []).append((segment, record))
        for index, step in enumerate(segment["result"]["steps"]):
            value = deepcopy(dict(step))
            outcome = value.get("outcome", {})
            request = outcome.get("request_id") if isinstance(outcome, Mapping) else None
            core = {key: val for key, val in value.items() if key != "index"}
            if isinstance(request, str) and request:
                if request in seen_steps:
                    previous_digest, previous_status = seen_steps[request]
                    if previous_digest == _digest(core):
                        continue
                    status = outcome.get("status")
                    pending = {"pending", "unknown", "submitted"}
                    # Keep evolving receipts; an unknown -> settled update is
                    # neither a duplicate action nor a contradictory terminal.
                    if previous_status not in pending and status not in pending:
                        blockers.append(f"conflicting-step-receipt:{request}")
                seen_steps[request] = (_digest(core), outcome.get("status"))
            value["segment_source"] = {"segment_id": segment["segment_id"], "original_index": value.get("index", index)}
            value["index"] = len(steps) + 1
            steps.append(value)
        for item in extract_native_stage_captures(segment["result"]):
            capture = item["capture"]
            key = _digest((segment["session_generation"], capture))
            if key in seen_captures:
                continue
            seen_captures.add(key)
            captures.append({"capture": capture, "session_generation": segment["session_generation"],
                             "source_segments": [segment["segment_id"]]})
    # Only matching stage bindings can join resume ranges. Unbound legacy
    # captures remain individual intervals for the original strict validator.
    grouped, unbound = {}, []
    for entry in captures:
        capture = entry["capture"]
        binding = capture.get("stage_binding")
        if (not isinstance(binding, Mapping) or binding.get("source_run_id") != run_id
                or not binding.get("session_transition_id") or not binding.get("step_context_digest")
                or type(capture.get("start_offset")) is not int or type(capture.get("end_offset")) is not int):
            unbound.append(entry)
            continue
        key = _digest((entry["session_generation"], capture.get("pid"), capture.get("path"), binding))
        grouped.setdefault(key, []).append(entry)
    coalesced = list(unbound)
    for entries in grouped.values():
        entries.sort(key=lambda entry: entry["capture"]["start_offset"])
        current = deepcopy(entries[0])
        for following in entries[1:]:
            left, right = current["capture"], following["capture"]
            if right["start_offset"] <= left["end_offset"]:
                left["end_offset"] = max(left["end_offset"], right["end_offset"])
                left["complete"] = left["end_offset"] > left["start_offset"]
                current["source_segments"].extend(following["source_segments"])
            else:
                blockers.append("recorder-span-gap:" + str(left.get("path")))
                coalesced.append(current)
                current = deepcopy(following)
        coalesced.append(current)
    original_unresolved = sorted(identity for identity, states in requests.items()
                        if states & {"pending", "unknown", "submitted"}
                        and not states & {"settled", "rejected"})
    resolutions, resolution_errors = [], []
    for identity in original_unresolved:
        contexts = request_contexts.get(identity, [])
        if not contexts:
            continue  # No action/session binding: preserve the original unknown.
        candidates = []
        try:
            for segment, record in contexts:
                proof = resolve_archived_exam_request(identity, run_id=run_id, produce_id=run.produce_id,
                    idol_card_id=run.idol_card_id, command_root=command_root,
                    expected_generation=record.get("native_legal_session_generation", segment["session_generation"]),
                    expected_action=record["action"], expected_model_source=record.get("policy_source"))
                candidates.append(proof)
            if len({_digest(value["archive_reference"]) for value in candidates}) != 1:
                raise ValueError("same request has conflicting archive sources")
            proof = candidates[0]
            requests[identity].add(proof["outcome"])
            resolutions.append({key: value for key, value in proof.items() if key not in {"archive", "raw_before"}})
        except FileNotFoundError:
            pass  # A missing archive does not resolve a pending request.
        except (OSError, KeyError, TypeError, ValueError) as error:
            resolution_errors.append({"request_id": identity, "reason": str(error)})
    unresolved = sorted(identity for identity, states in requests.items()
                        if states & {"pending", "unknown", "submitted"}
                        and not states & {"settled", "rejected"})
    blockers.extend("conflicting-native-request-outcome:" + identity for identity, states in requests.items()
                    if "rejected" in states and states & {"settled", "submitted"})
    blockers.extend("unresolved-native-request:" + identity for identity in unresolved)
    completed = bool(segments) and segments[-1]["result"].get("status") == "completed"
    if not completed:
        blockers.append("native-run-not-completed")
    if not coalesced:
        blockers.append("native-run-has-no-recorder-captures")
    return {"schema": MERGED_SCHEMA, "run_id": run_id, "segments": [
        {"segment_id": value["segment_id"], "status": value["result"].get("status"),
         "partial": value["result"].get("status") != "completed", "session_generation": value["session_generation"]}
        for value in segments], "steps": steps,
        "native_runtime_stage_captures": [value["capture"] for value in coalesced],
        "capture_sources": coalesced, "unresolved_request_ids": unresolved,
        "original_segment_unresolved_request_ids": original_unresolved,
        "archive_resolution_schema": "gkms.native-run-terminal-archive-resolution.v1",
        "archived_request_resolutions": resolutions, "archive_resolution_errors": resolution_errors,
        "run_completed": completed, "blockers": sorted(set(blockers)), "dataset_ready": False,
        "validation_required": "each actual recorder stage must pass native turn-one-to-terminal validation"}


def _pointer_number(value):
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        raise ValueError("native sequence identity is missing")
    text = str(value).removeprefix("native-sequence:")
    return int(text, 16 if text.lower().startswith("0x") else 10)


def _final_terminal_from_capture(capture, run_id, produce_id, idol_card_id):
    """Read at most 8 MiB inside the actual capture, without stage promotion."""
    binding = capture.get("stage_binding", {})
    if (not isinstance(binding, Mapping) or binding.get("source_run_id") != run_id
            or binding.get("step_type_value") != 18):
        raise ValueError("Final capture binding does not identify this run's step 18")
    start, end, pid = capture.get("start_offset"), capture.get("end_offset"), capture.get("pid")
    if any(type(value) is not int for value in (start, end, pid)) or start < 0 or end <= start or pid <= 0:
        raise ValueError("Final recorder byte range/PID is invalid")
    path = Path(capture["path"]).resolve()
    if path.stat().st_size < end:
        raise ValueError("Final recorder was truncated")
    begin = max(start, end - 8 * 1024 * 1024)
    with path.open("rb") as stream:
        stream.seek(begin)
        payload = stream.read(end - begin)
    for line in reversed(payload.splitlines()):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue  # The bounded tail may start inside an earlier long row.
        body = row.get("body") if isinstance(row, Mapping) else None
        if not isinstance(body, Mapping) or body.get("record") != "transition" or body.get("terminal") is not True:
            continue
        after = body.get("state_after")
        if isinstance(after, str):
            after = json.loads(after)
        if (row.get("schema") != "gkms.runtime-exam-recorder.shadow.v1" or row.get("pid") != pid
                or body.get("terminal_known") is not True or not isinstance(after, Mapping)
                or after.get("isExamEndComplete") is not True or after.get("produceId") != produce_id
                or after.get("idolCardId") != idol_card_id or after.get("stepType") != 18
                or _pointer_number(body.get("runtime_sequence")) != _pointer_number(binding.get("session_transition_id"))):
            raise ValueError("Final terminal row identity differs from this run/capture")
        return {"path": str(path), "start_offset": start, "end_offset": end,
                "record_sha256": hashlib.sha256(line).hexdigest(), "pid": pid,
                "native_sequence": str(body["runtime_sequence"]), "envelope_sequence": row.get("sequence"),
                "final_score": after.get("parameter"), "validation": "observed Final terminal; not a training promotion"}
    raise ValueError("no complete matching Final terminal row in bounded recorder tail")


def _created_memory(snapshot, produce_id, idol_card_id):
    if not isinstance(snapshot, Mapping):
        return None
    progress = snapshot.get("progress")
    if not isinstance(progress, Mapping) or progress.get("produceId") != produce_id or progress.get("idolCardId") != idol_card_id:
        return None
    memory = progress.get("resultMemory")
    if not isinstance(memory, Mapping) or memory.get("idolCardId", idol_card_id) != idol_card_id:
        return None
    identity = memory.get("userMemoryId")
    return identity if isinstance(identity, str) and identity else None


def inspect_native_completion(run_id, *, produce_id, idol_card_id, home_snapshot: Mapping,
                              current_steps: Sequence = (), run_root=DEFAULT_RUN_ROOT,
                              command_root=DEFAULT_COMMAND_ROOT):
    """Inspect gameplay completion independently of frozen-stage readiness."""
    result = {"gameplay_complete": False, "final_terminal_observed": False,
              "workflow_final_terminal_observed": False,
              "workflow_final_attempts": [],
              "created_memory_id": None, "sources": {}, "blockers": [], "dataset_ready": False}
    try:
        run, segments = _verified_native_segments(run_id, run_root)
        if (run.produce_id, run.idol_card_id) != (produce_id, idol_card_id):
            raise ValueError("completion request differs from the immutable run identity")
        if not isinstance(current_steps, Sequence) or isinstance(current_steps, (str, bytes)):
            raise ValueError("current completion steps must be an array")
        fresh = [step.to_dict() if callable(getattr(step, "to_dict", None)) else step for step in current_steps]
        if any(not isinstance(step, Mapping) for step in fresh):
            raise ValueError("current completion step is invalid")
    except (OSError, TypeError, ValueError) as error:
        result["blockers"].append(f"native-completion-identity:{error}")
        return result
    ledgers = [(segment["result"].get("steps", []), segment["segment_id"], segment["result"].get("last_surface"))
               for segment in segments]
    ledgers.append((fresh, "current_steps", None))
    result_steps, persisted_ids, terminal_errors, workflow_requests = [], set(), [], set()
    for steps, source, last_surface in ledgers:
        for step in steps:
            outcome = step.get("outcome", {})
            if not isinstance(outcome, Mapping):
                continue
            if outcome.get("accepted") is True and outcome.get("terminal") is True:
                inner_steps = outcome.get("steps", [])
                last_context = (inner_steps[-1].get("native_context_before", {})
                    if isinstance(inner_steps, (list, tuple)) and inner_steps and isinstance(inner_steps[-1], Mapping) else {})
                if isinstance(last_context, Mapping) and last_context.get("step_type") == 18:
                    from .native_workflow_terminal import verify_final_terminal_from_outcome
                    try:
                        proof = verify_final_terminal_from_outcome(outcome, run_id=run_id, produce_id=produce_id,
                            idol_card_id=idol_card_id, command_root=command_root)
                        result["workflow_final_terminal_observed"] = True
                        if proof["request_id"] not in workflow_requests:
                            workflow_requests.add(proof["request_id"])
                            attempt = {**proof, "segment_id": source, "outer_step_index": step.get("index")}
                            result["workflow_final_attempts"].append(attempt)
                            # Immutable segment time, then within-segment step
                            # order, preserves failed attempts before retries.
                            result["sources"]["workflow_final_terminal"] = attempt
                    except (OSError, KeyError, TypeError, ValueError) as error:
                        result["sources"].setdefault("workflow_terminal_diagnostics", []).append(str(error))
                for entry in extract_native_stage_captures(outcome):
                    capture = entry["capture"]
                    if capture.get("stage_binding", {}).get("step_type_value") != 18:
                        continue
                    try:
                        proof = _final_terminal_from_capture(capture, run_id, produce_id, idol_card_id)
                        result["final_terminal_observed"] = True
                        result["sources"]["final_terminal"] = {**proof, "segment_id": source}
                    except (OSError, KeyError, TypeError, ValueError) as error:
                        terminal_errors.append(str(error))
            if (step.get("action") in {"result.confirm_create_memory", "result.continue_memory"}
                    and outcome.get("status") == "settled" and outcome.get("executor") == "dll"
                    and isinstance(outcome.get("request_id"), str) and outcome["request_id"]):
                result_steps.append({"action": step["action"], "request_id": outcome["request_id"], "segment_id": source})
        memory = _created_memory(last_surface, produce_id, idol_card_id)
        if memory is not None:
            persisted_ids.add(memory)
            result["sources"].setdefault("persisted_memory", []).append({"segment_id": source,
                "user_memory_id": memory, "field": "last_surface.progress.resultMemory.userMemoryId"})
    if not result["final_terminal_observed"]:
        # Recorder retention and dataset coverage do not own gameplay
        # completion. The same-run result action, created UID and actual
        # completed Home below are the independent completion proof.
        result["sources"]["terminal_diagnostics"] = ["same-run-Final-terminal-not-observed", *terminal_errors]
        result["sources"]["terminal_diagnostics_scope"] = "legacy capture path only; independent workflow evidence is separate"
    if not result_steps:
        result["blockers"].append("same-run-settled-memory-result-action-missing")
    result["sources"]["result_actions"] = result_steps
    state = home_snapshot.get("state") if isinstance(home_snapshot, Mapping) else None
    valid_home = (isinstance(home_snapshot, Mapping) and home_snapshot.get("screen_type") == "HomeTopScreenPresenter"
                  and isinstance(state, Mapping) and state.get("in_progress") is False
                  and state.get("progress_status") == 99 and state.get("step_type") == 18
                  and state.get("produce_id") == produce_id)
    home_memory = _created_memory(home_snapshot, produce_id, idol_card_id)
    if not valid_home or home_memory is None:
        result["blockers"].append("current-Home-Final-completion-state-or-created-memory-missing")
    if len(persisted_ids) > 1:
        result["blockers"].append("conflicting-persisted-created-memory-IDs")
    elif persisted_ids:
        result["created_memory_id"] = next(iter(persisted_ids))
        if home_memory != result["created_memory_id"]:
            result["blockers"].append("Home-created-memory-differs-from-persisted-run")
    elif home_memory and any(step["segment_id"] == "current_steps" for step in result_steps):
        result["created_memory_id"] = home_memory
        result["sources"]["created_memory_binding"] = "current settled result action + actual Home progress.resultMemory.userMemoryId"
    else:
        result["blockers"].append("persisted-created-memory-ID-missing")
    result["sources"]["home"] = {"revision": home_snapshot.get("revision") if isinstance(home_snapshot, Mapping) else None,
                                   "user_memory_id": home_memory, "valid_completion_state": valid_home}
    if result["workflow_final_attempts"]:
        latest = result["workflow_final_attempts"][-1]
        progress = home_snapshot.get("progress", {})
        auditions = progress.get("auditions", []) if isinstance(progress, Mapping) else []
        finals = [row for row in auditions if isinstance(row, Mapping)
                  and row.get("auditionStepType") == "ProduceStepType_AuditionFinal"] if isinstance(auditions, list) else []
        if len(finals) == 1:
            final = finals[0]
            result["sources"]["Home_final_audition"] = {"record": deepcopy(dict(final)),
                "matches_latest_terminal_score": final.get("score") == latest["final_score"],
                "matches_latest_terminal_tier": final.get("selectNumber") == latest.get("audition_number"),
                "source": "observed Home progress.auditions Final record; not entry_number"}
    result["gameplay_complete"] = not result["blockers"]
    return result
