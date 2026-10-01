"""Read-only, append-only qualification of one completed reload's late bridge.

The failed maintenance response is never changed. This receipt only authorizes
the normal same-run login/recovery gate; restored progress must still be proven.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path

from .runtime_command_client import DEFAULT_BRIDGE_ROOT, RuntimeCommandClient, _claim_lock
from .run_identity import load_active_run
from .runtime_session_continuity import (
    _HASH, _JOB_ID, _digest, _fail, _manifest_digest, _native, _no_pending, _read, _time,
)
from .training_artifact_io import canonical_json_bytes

SCHEMA = "gkms.native-maintenance-late-verification.v1"
# Deliberately narrow: unrelated executor failures cannot become authorizations.
ELIGIBLE_ERROR = ("RuntimeError: game launch returned but native bridge was not verified: "
                  "DLL heartbeat expired; enter the running game before retrying")
_SOURCE_NAMES = ("response", "request", "preflight", "operation", "executor", "launch")


def _job_paths(response_path, bridge_root):
    response = Path(response_path).resolve()
    sessions = Path(bridge_root).resolve().parent / "native_maintenance" / "sessions"
    if (response.parent.name != "responses" or not response.is_relative_to(sessions)
            or not _JOB_ID.fullmatch(response.stem) or not _JOB_ID.fullmatch(response.parent.parent.name)
            or response.parent.parent.parent != sessions):
        _fail("late verification response is outside its exact maintenance session")
    job = response.parent.parent / "jobs" / response.stem
    return job, dict(zip(_SOURCE_NAMES, (response, job / "request.json", job / "preflight.json",
        job / "operation.json", job / "executor_result.json", job / "launch.json")))


def _sources(response_path, bridge_root, expected=None):
    job, paths = _job_paths(response_path, bridge_root)
    values, refs = {}, {}
    if expected is not None and set(expected) != set(paths):
        _fail("late verification source set changed")
    for name, path in paths.items():
        ref = None if expected is None else expected[name]
        if ref is not None and (not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
                or not isinstance(ref.get("sha256"), str) or not _HASH.fullmatch(ref["sha256"])
                or Path(ref.get("path", "")).resolve() != path):
            _fail("late verification source path changed")
        values[name], refs[name] = _read(path, expected=None if ref is None else ref.get("sha256"))
    response, request, executor = (values[key] for key in ("response", "request", "executor"))
    finished = response.get("finished_at")
    if (response.get("schema") != "gkms.native-maintenance.v1" or response.get("status") != "failed"
            or response.get("error") != ELIGIBLE_ERROR or "result" in response
            or response.get("request_id") != job.name or response.get("session_id") != job.parent.parent.name
            or type(finished) not in (int, float) or not math.isfinite(finished)
            or set(request) != {"command", "payload"} or request.get("command") != "reload-dll"
            or executor.get("schema") != "gkms.native-maintenance-executor.v1"
            or executor.get("operation") != "reload-dll" or executor.get("game_started") is not True
            or executor.get("inspection_only") is not False or executor.get("uac_requested") is not False
            or _time({"captured_at": executor.get("completed_at")}) > finished):
        _fail("job is not a completed reload with the eligible native verification timeout")
    return job, values, refs


def _status(status, generation, pid, observed_at):
    if (status.get("schema") != "gkms.runtime-command-status.v1" or status.get("protocol_version") != 1
            or status.get("ready") is not True or status.get("session_generation") != generation
            or type(status.get("pid")) is not int or status["pid"] != pid
            or "read_outer_snapshot" not in status.get("capabilities", ())
            or (status.get("pc_version") or {}).get("input_qualified") is not True
            or not -1 <= observed_at - _time({"captured_at": status.get("updated_at")}) <= 10):
        _fail("late native status is stale, restricted or belongs to another process")


def _binary_ref(path):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "sha256": digest}


def _validate_body(body, response_path, run, bridge_root):
    from .runtime_maintenance_independent_verification import SCHEMA as INDEPENDENT_SCHEMA, validate_body
    if body.get("schema") == INDEPENDENT_SCHEMA:
        return validate_body(body, response_path, run, bridge_root)
    job, values, refs = _sources(response_path, bridge_root, body.get("maintenance_sources", {}))
    payload = values["request"].get("payload") or {}
    result = body.get("result") or {}
    started, completed = body.get("started_at"), body.get("verified_at")
    generation, pid = result.get("native_generation"), result.get("game_pid")
    installed = Path(bridge_root).resolve().parent / "native/runtime_command_bridge/gkms_runtime_command_bridge.dll"
    if (body.get("schema") != SCHEMA or body.get("session_id") != job.parent.parent.name
            or body.get("request_id") != job.name or body.get("run_manifest_digest") != _manifest_digest(run)
            or body.get("run_id") != run.run_id or payload.get("expected_run_id") != run.run_id
            or body.get("from_generation") != payload.get("expected_generation")
            or type(pid) is not int or pid <= 0 or pid == payload.get("expected_game_pid")
            or not isinstance(generation, str) or not _HASH.fullmatch(generation)
            or generation == payload.get("expected_generation")
            or result.get("expected_run_id") != run.run_id
            or result.get("installed_sha256") != payload.get("candidate_sha256")
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in (started, completed))
            or not values["response"]["finished_at"] <= started <= completed
            or body.get("no_pending_before") is not True or body.get("no_pending_after") is not True
            or body.get("active_run_unchanged") is not True
            or any(body.get(key) is not False for key in ("original_response_rewritten", "game_input_submitted", "gameplay_continuity_verified"))
            or result.get("gameplay_resumed") is not False or result.get("uac_requested_by_job") is not False
            or body.get("installed_binary") != {"path": str(installed), "sha256": payload.get("candidate_sha256")}
            or body.get("staged_binary") != _binary_ref(job / "candidate.dll")):
        _fail("late verification does not bind this unchanged run and exact reload candidate")
    if body["staged_binary"]["sha256"] != payload.get("candidate_sha256"):
        _fail("late verification staged candidate changed")
    for name, observed in (("status_before", started), ("status_after", completed)):
        _status(body.get(name) or {}, generation, pid, observed)
    if body["status_before"].get("pc_version") != body["status_after"].get("pc_version"):
        _fail("late native PC identity changed during observation")
    from .native_maintenance import expected_game_executable
    expected_game_path = expected_game_executable()
    identity = body.get("process_identity") or {}
    normalize = lambda value: ntpath.normcase(ntpath.normpath(value))
    if (identity.get("pid") != pid or identity.get("actual_process_image_verified") is not True
            or normalize(str(identity.get("game_path", ""))) != normalize(expected_game_path)
            or identity.get("expected_game_path") != expected_game_path):
        _fail("late native PID has no fixed game executable identity")
    native_ref, request_ref = body.get("native_source") or {}, body.get("native_request") or {}
    if any(not isinstance(ref, dict) or set(ref) != {"path", "sha256"}
            or not isinstance(ref.get("sha256"), str) or not _HASH.fullmatch(ref["sha256"])
            for ref in (native_ref, request_ref)):
        _fail("late native sources require exact content hashes")
    raw, actual_ref = _native(native_ref.get("path", ""), generation, bridge_root, expected=native_ref.get("sha256"))
    request, actual_request_ref = _read(request_ref.get("path", ""), within=Path(bridge_root) / "requests",
                                     expected=request_ref.get("sha256"))
    request_id = request.get("request_id")
    response, _ = _read(actual_ref["path"])
    if (actual_ref != native_ref or actual_request_ref != request_ref
            or set(request) != {"schema", "request_id", "session_generation", "command"}
            or request.get("schema") != "gkms.runtime-command.v1" or request.get("command") != "read_outer_snapshot"
            or request.get("session_generation") != generation or not isinstance(request_id, str)
            or not _JOB_ID.fullmatch(request_id) or response.get("request_id") != request_id
            or Path(actual_ref["path"]).stem != request_id or Path(actual_request_ref["path"]).stem != request_id
            or result.get("snapshot_path") != actual_ref["path"] or result.get("screen_type") != raw.get("screen_type")
            or raw.get("busy") is not False or raw.get("actions_complete") is not True
            or not started - 1 <= _time(raw) <= completed + 1):
        _fail("late verification lacks its fresh correlated read-only snapshot")
    return result


def load_late_verification(response_path, run, bridge_root, *, expected_ref=None):
    """Validate immutable evidence only; no live reads or input on continuation."""
    job, _ = _job_paths(response_path, bridge_root)
    directory = job / "late_native_verifications"
    candidates = list(directory.glob("*.json"))
    if not candidates:
        if expected_ref is not None:
            _fail("bound late verification receipt is missing")
        return None, None
    if len(candidates) != 1:
        _fail("late verification has multiple authorizations for the same job")
    path = candidates[0].resolve()
    if expected_ref is not None and Path(expected_ref.get("path", "")).resolve() != path:
        _fail("late verification reference changed")
    body, ref = _read(path, within=directory, expected=None if expected_ref is None else expected_ref["sha256"])
    if path.stem != _digest(body):
        _fail("late verification content address changed")
    return _validate_body(body, response_path, run, bridge_root), ref


def qualify_late_native_verification(response_path, *, expected_run_id, expected_game_pid,
                                     expected_generation, bridge_root=DEFAULT_BRIDGE_ROOT,
                                     client=None, process_path_reader=None, clock=None):
    """One explicit fresh read. Never starts/restarts, sends input, or retries it."""
    from .native_maintenance import verify_inspection_process_path
    from .runtime_session_continuity import _authorization, verified_run_generation
    root = Path(bridge_root).resolve()
    if (type(expected_game_pid) is not int or expected_game_pid <= 0
            or not isinstance(expected_generation, str) or not _HASH.fullmatch(expected_generation)):
        _fail("late verification requires an explicit valid new PID and generation")
    clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
    client = client or RuntimeCommandClient(root)
    if Path(client.root).resolve() != root:
        _fail("late verification client belongs to another bridge")
    with _claim_lock():
        _no_pending(root)
        run = load_active_run(root=root.parent / "runs")
        if run is None or run.run_id != expected_run_id:
            _fail("late verification active run differs")
        job, sources, refs = _sources(response_path, root)
        payload = sources["request"].get("payload") or {}
        before = verified_run_generation(root, run)
        if (before != payload.get("expected_generation") or payload.get("expected_run_id") != run.run_id
                or expected_game_pid == payload.get("expected_game_pid") or expected_generation == before):
            _fail("late verification expected process/run transition differs")
        existing, ref = load_late_verification(response_path, run, root)
        if existing is not None:
            if existing["game_pid"] != expected_game_pid or existing["native_generation"] != expected_generation:
                _fail("existing late verification belongs to another process")
            _authorization(response_path, run, before, expected_generation, root)
            return {"receipt": ref, "result": existing, "reused": True}
        started = clock()
        status_before = dict(client.read_status())
        _status(status_before, expected_generation, expected_game_pid, started)
        installed = _binary_ref(root.parent / "native/runtime_command_bridge/gkms_runtime_command_bridge.dll")
        staged = _binary_ref(job / "candidate.dll")
        if installed["sha256"] != payload.get("candidate_sha256") or staged["sha256"] != installed["sha256"]:
            _fail("installed/staged DLL differs from the authorized reload candidate")
        try:
            identity = verify_inspection_process_path({"game_pid": expected_game_pid}, process_path_reader=process_path_reader)
        except ValueError as error:
            _fail(str(error))
        # RuntimeCommandClient retains a timed-out descriptor. Do not resend.
        native = client.execute("read_outer_snapshot", timeout=15).require_ok()
        _, native_ref = _read(native.path, within=root / "results")
        _, request_ref = _read(root / "requests" / (native.request.request_id + ".json"))
        status_after = dict(client.read_status())
        completed = clock()
        active_after = load_active_run(root=root.parent / "runs")
        _no_pending(root)
        if active_after != run or _binary_ref(installed["path"]) != installed:
            _fail("active run or installed DLL changed during late verification")
        result = {"game_pid": expected_game_pid, "native_generation": expected_generation,
            "expected_run_id": run.run_id, "installed_sha256": installed["sha256"],
            "snapshot_path": native_ref["path"], "screen_type": (native.snapshot or {}).get("screen_type"),
            "gameplay_resumed": False, "uac_requested_by_job": False}
        body = {"schema": SCHEMA, "session_id": job.parent.parent.name, "request_id": job.name,
            "run_id": run.run_id, "run_manifest_digest": _manifest_digest(run), "from_generation": before,
            "maintenance_sources": refs, "native_source": native_ref, "native_request": request_ref,
            "status_before": status_before, "status_after": status_after, "started_at": started,
            "verified_at": completed, "process_identity": identity, "installed_binary": installed,
            "staged_binary": staged, "result": result, "no_pending_before": True, "no_pending_after": True,
            "active_run_unchanged": True, "original_response_rewritten": False,
            "game_input_submitted": False, "gameplay_continuity_verified": False}
        _validate_body(body, response_path, run, root)
        # Reuse all existing preflight/process/checkpoint checks before publishing.
        _authorization(response_path, run, before, expected_generation, root, _late_candidate=body)
        directory = job / "late_native_verifications"
        directory.mkdir(exist_ok=True)
        path = directory / (_digest(body) + ".json")
        with path.open("xb") as stream:
            stream.write(canonical_json_bytes(body)); stream.flush(); os.fsync(stream.fileno())
        return {"receipt": {"path": str(path), "sha256": _digest(body)}, "result": result, "reused": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", type=Path, required=True)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-game-pid", required=True, type=int)
    parser.add_argument("--expected-generation", required=True)
    args = parser.parse_args()
    print(json.dumps(qualify_late_native_verification(args.response, expected_run_id=args.expected_run_id,
        expected_game_pid=args.expected_game_pid, expected_generation=args.expected_generation), ensure_ascii=False))


if __name__ == "__main__":
    main()
