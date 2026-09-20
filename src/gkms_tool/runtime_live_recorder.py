"""Qualify an action-settlement recorder independently of training capture.

Only startup metadata is read. This never submits a game read/action, changes
recorder files, or weakens the old exact-dataset legal-probe requirements.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from .runtime_command_client import RuntimeCommandError


from .application_paths import native_state_root
TELEMETRY_ROOT = native_state_root() / "telemetry"
MAX_STARTUP_BYTES = 1024 * 1024
RECORDER_SCHEMA = "gkms.runtime-exam-recorder.shadow.v1"
ORIGINAL_METADATA = "9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668"
CURRENT_METADATA = "9349bc965fb08a434ab1c9547a3440a8ee02a05e761723792aabe5f4a9ecb635"
PROFILES = {
    ORIGINAL_METADATA: (0x0BE0E000, 0x6A73E78D, "pc-9a6bf153-original"),
    CURRENT_METADATA: (0x0BE54000, 0x6A996E38, "pc-9349bc96-default"),
}
HOOK_NAMES = frozenset({
    "ExamPlayCommand.CreateUseHandCommand", "ExamPlayCommand.CreateUseDrinkCommand",
    "ExamPlayCommand.CreateTurnEndCommand", "ExamSequence.AddExecuteCommand",
    "ExamParameterModel.AddPlayLog(ExamPlayLog)", "ExamSequence.set_IsCommandPlaying",
    "ExamSequence.IsEndExam", "ExamParameterModel.get_IsExamEndComplete",
    "ExamParameterModel.SetExamEndComplete", "CardCreateIdEffectExecutor.ExecuteEffect",
    "ExamCardData.get_Guid", "ExamCardData.CreateGuidIfNeed",
    "ExamCardMoveController.AddCard(single)", "ExamCardMoveController.AddCard(list)",
    "ExamEffectCalculateContext.GetRandomInt",
})


class LiveRecorderUnavailable(RuntimeCommandError):
    pass


def _fail(reason):
    raise LiveRecorderUnavailable("native action recorder is not ready: " + reason)


def _consumer(status):
    if not isinstance(status, Mapping) or type(status.get("pid")) is not int or status["pid"] <= 0:
        _fail("current bridge PID is invalid")
    version = status.get("pc_version")
    identity = version.get("engine_identity") if isinstance(version, Mapping) else None
    if (not isinstance(version, Mapping) or version.get("recognized") is not True
            or not isinstance(identity, Mapping)):
        _fail("current bridge has no recognized PC identity")
    sha = identity.get("metadata_sha256")
    if sha not in PROFILES:
        _fail("PC metadata has no admitted recorder profile")
    size, timestamp, _ = PROFILES[sha]
    if (type(identity.get("game_assembly_image_size")) is not int or identity["game_assembly_image_size"] != size
            or type(identity.get("game_assembly_timestamp")) is not int or identity["game_assembly_timestamp"] != timestamp):
        _fail("current bridge PC header/metadata identity differs")
    return dict(identity)


def _startup_rows(path, pid):
    try:
        with path.open("rb") as stream:
            before = path.stat()
            raw = stream.read(MAX_STARTUP_BYTES)
            after = path.stat()
    except OSError as error:
        raise LiveRecorderUnavailable("native action recorder is not ready: startup log unavailable") from error
    if before.st_ino != after.st_ino or after.st_size < before.st_size:
        _fail("startup log was replaced or truncated")
    complete_lines = raw.split(b"\n")[:-1]
    current = []
    for line in complete_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise LiveRecorderUnavailable("native action recorder is not ready: malformed startup row") from error
        if not isinstance(row, Mapping) or row.get("schema") != RECORDER_SCHEMA or row.get("pid") != pid:
            _fail("startup row schema/PID differs from current bridge")
        body = row.get("body")
        if not isinstance(body, Mapping):
            _fail("startup row has no typed body")
        record = body.get("record")
        if record == "preflight_begin":
            current = [dict(body)]
        elif current:
            # Normal action rows can be very large. Startup readiness is an
            # immutable preceding segment, not an invitation to parse a run.
            if record in {"captured_action", "action_settlement", "transition", "native_before_action"}:
                break
            current.append(dict(body))
    if not current:
        _fail("startup begin is absent from the bounded log prefix")
    return current


def _qualify(path, pid, identity, *, legal_probe):
    bodies = _startup_rows(path, pid)
    failures = [row for row in bodies if (row.get("record") == "preflight" and row.get("valid") is not True)
                or row.get("record") == "recorder_error"]
    if failures:
        _fail(str(failures[-1].get("reason", "recorder preflight failed"))[:256])
    finals = [row for row in bodies if row.get("record") == "preflight" and row.get("valid") is True]
    if len(finals) != 1:
        _fail("atomic hook preflight has not completed uniquely")
    final = finals[0]
    hooks = final.get("hooks")
    if (final.get("atomic_hooks") is not True or not isinstance(hooks, list) or len(hooks) != 15
            or any(not isinstance(hook, Mapping) for hook in hooks)
            or {hook.get("name") for hook in hooks} != HOOK_NAMES
            or any(hook.get("installed") is not True or hook.get("enabled") is not True
                   or type(hook.get("actual_token")) is not int or hook["actual_token"] <= 0
                   or hook.get("actual_token") != hook.get("expected_token") for hook in hooks)):
        _fail("the 15 required hooks were not atomically installed and enabled")
    module = final.get("module")
    if (not isinstance(module, Mapping) or module.get("valid") is not True or module.get("pid") != pid
            or module.get("game_assembly_size") != identity["game_assembly_image_size"]
            or module.get("pe_timestamp") != identity["game_assembly_timestamp"]):
        _fail("recorder module belongs to a different process or PC header")
    if legal_probe:
        if identity["metadata_sha256"] != ORIGINAL_METADATA:
            _fail("the historical legal probe cannot serve this PC version")
        probes = [row for row in bodies if row.get("record") == "legal_verified_probe_preflight"]
        if (final.get("legal_verified_probe") is not True or len(probes) != 1
                or probes[0].get("valid") is not True
                or probes[0].get("target") != "gkms_runtime_exam_recorder_legal_verified_probe"
                or probes[0].get("macro") != "GKMS_RUNTIME_LEGAL_VERIFIED_PROBE"
                or str(probes[0].get("metadata_sha256", "")).lower() != ORIGINAL_METADATA):
            _fail("historical legal-probe qualification is missing")
    else:
        profiles = [row for row in bodies if row.get("record") == "pc_method_profile_preflight"]
        if len(profiles) != 1 or profiles[0].get("valid") is not True:
            _fail("normal recorder has no verified current-PC method profile")
        observed = profiles[0].get("identity")
        module_identity = module.get("verified_pc_identity")
        for value in (observed, module_identity):
            if (not isinstance(value, Mapping)
                    or any(value.get(key) != identity[key] for key in (
                        "metadata_sha256", "game_assembly_image_size", "game_assembly_timestamp"))
                    or value.get("method_profile") != PROFILES[identity["metadata_sha256"]][2]):
                _fail("normal recorder method profile differs from current bridge identity")
        if final.get("legal_verified_probe") is True:
            _fail("a research probe cannot masquerade as the normal recorder")
    return path


def require_live_action_recorder(status, *, telemetry_root=None):
    """Return the verified live log path; never change an existing pending path."""
    identity = _consumer(status)
    pid = status["pid"]
    root = TELEMETRY_ROOT if telemetry_root is None else Path(telemetry_root)
    normal = root / f"runtime_exam_recorder_shadow_{pid}.jsonl"
    if normal.is_file():
        return _qualify(normal, pid, identity, legal_probe=False)
    if identity["metadata_sha256"] == ORIGINAL_METADATA:
        legacy = root / f"runtime_exam_recorder_legal_verified_probe_{pid}.jsonl"
        if legacy.is_file():
            return _qualify(legacy, pid, identity, legal_probe=True)
    _fail("normal recorder startup log is unavailable; no historical-probe fallback for this PC")
