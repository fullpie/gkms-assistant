"""Verify a settled Final outcome independently of training-capture admission."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path

from .audition_local_save_state import AuditionLocalSaveStateEvidence
from .native_exam_archive import resolve_archived_exam_request
from .runtime_action_state_evidence import adapt_runtime_action_state_evidence
from .training_artifact_io import canonical_json_bytes

SCHEMA = "gkms.native-workflow-Final-terminal-proof.v1"
_MAX_RECORD_BYTES = 32 * 1024 * 1024


def _require(value, message):
    if not value:
        raise ValueError("Final workflow terminal: " + message)


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _pointer(value):
    _require(isinstance(value, (str, int)) and not isinstance(value, bool), "native sequence is missing")
    text = str(value).removeprefix("native-sequence:")
    number = int(text, 16 if text.lower().startswith("0x") else 10)
    _require(number > 0, "native sequence is null")
    return number


def _final_identity(state, produce_id, idol_card_id, *, terminal):
    _require(isinstance(state, Mapping) and type(state.get("stepType")) is int and state["stepType"] == 18
             and state.get("produceId") == produce_id and state.get("idolCardId") == idol_card_id
             and state.get("isReplay") is False and state.get("isExamEndComplete") is terminal,
             "state is not this live run's matching Final boundary")
    _require(type(state.get("parameter")) is int and state["parameter"] >= 0, "Final score is not a nonnegative integer")


def _ending_record(path, start, end):
    _require(type(start) is int and type(end) is int and 0 <= start < end
             and path.stat().st_size >= end, "recorder cursor is invalid or truncated")
    begin = max(start, end - _MAX_RECORD_BYTES)
    with path.open("rb") as stream:
        if start:
            stream.seek(start - 1)
            _require(stream.read(1) == b"\n", "archived action cursor is not a complete record boundary")
        stream.seek(begin)
        payload = stream.read(end - begin)
    _require(len(payload) == end - begin and payload.endswith(b"\n"), "terminal recorder row is incomplete")
    previous = payload.rfind(b"\n", 0, len(payload) - 1)
    _require(previous >= 0 or begin == start, "terminal recorder row exceeds bounded read")
    line = payload[previous + 1:]
    row_start = begin + previous + 1
    return json.loads(line), {"path": str(path), "start_offset": row_start, "end_offset": end,
        "bytes": len(line), "sha256": hashlib.sha256(line).hexdigest()}


def _recorder_after(provenance, resolved):
    archive = resolved["archive"]
    path = Path(provenance["telemetry_path"]).resolve()
    _require(path == Path(archive["recorder_path"]).resolve(), "recorder differs from the retired request")
    row, reference = _ending_record(path, archive["start_offset"], provenance.get("telemetry_end_offset"))
    body = row.get("body") if isinstance(row, Mapping) else None
    _require(isinstance(row, Mapping) and row.get("schema") == "gkms.runtime-exam-recorder.shadow.v1"
             and type(row.get("pid")) is int and row["pid"] > 0
             and type(row.get("sequence")) is int and row["sequence"] > 0
             and isinstance(body, Mapping) and body.get("record") == "transition"
             and body.get("official_action_captured") is True and body.get("state_after_captured") is True
             and body.get("terminal") is True and body.get("terminal_known") is True
             and body.get("managed_thread_match") is True, "normal recorder has no captured terminal transition")
    _require(all(type(body.get(key)) is int and body[key] >= 0 for key in ("action_order", "official_action_order")),
             "terminal recorder action-order fields are invalid")
    _require(_pointer(body.get("runtime_sequence")) == _pointer(resolved["sequence_id"]),
             "terminal recorder belongs to another native sequence")
    action = body.get("action")
    kind = {"play": "use-hand", "drink": "use-drink", "end_turn": "turn-end"}[archive["kind"]]
    _require(isinstance(action, Mapping) and action.get("known") is True and action.get("isManual") is True
             and action.get("action_type") == kind and type(action.get("play_index")) is int
             and action["play_index"] == archive["slot"], "terminal recorder action differs from the retired request")
    if archive["kind"] == "play":
        _require(isinstance(action.get("source_card"), Mapping)
                 and action["source_card"].get("guid") == archive.get("card_guid"), "terminal card identity differs")
    elif archive["kind"] == "drink":
        _require(action.get("source_drink_id") == archive.get("drink_id"), "terminal drink identity differs")
    _require(isinstance(body.get("state_before"), Mapping)
             and canonical_json_bytes(body["state_before"]) == canonical_json_bytes(resolved["raw_before"]),
             "terminal transition before-state differs from the bound request")
    return body.get("state_after"), reference, {"pid": row["pid"], "envelope_sequence": row.get("sequence"),
        "native_sequence": str(body["runtime_sequence"]), "action_order": body.get("action_order"),
        "official_action_order": body.get("official_action_order"),
        "original_qualification_flags": {key: body.get(key) for key in
            ("state_before_complete", "state_after_complete", "action_state_exact", "transition_promotion_candidate")}}


def _rpc_after(provenance, resolved, command_root):
    path = Path(provenance["telemetry_path"]).resolve()
    root = Path(command_root).resolve()
    _require(path.parent == root / "results" and provenance.get("telemetry_end_offset") == 0,
             "snapshot provenance is not an explicit retained DLL result")
    with path.open("rb") as stream:
        payload = stream.read(_MAX_RECORD_BYTES + 1)
    _require(len(payload) <= _MAX_RECORD_BYTES, "terminal RPC exceeds bounded read")
    result = json.loads(payload)
    _require(isinstance(result, Mapping) and result.get("schema") == "gkms.runtime-command-result.v1"
             and result.get("status") == "ok" and result.get("request_id") == path.stem
             and result.get("session_generation") == resolved["request"]["session_generation"],
             "terminal RPC response identity/session differs")
    snapshot = result.get("snapshot")
    _require(isinstance(snapshot, Mapping) and snapshot.get("busy") is False and snapshot.get("queue_empty") is True
             and snapshot.get("terminal") is True and snapshot.get("is_replay") is False
             and _pointer(snapshot.get("sequence_id")) == _pointer(resolved["sequence_id"]),
             "DLL Final snapshot is not settled in the original sequence")
    return snapshot.get("exam_save"), {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload)}, {"snapshot_request_id": result["request_id"],
        "native_sequence": str(snapshot["sequence_id"]), "queue_empty_observed": True, "revision": snapshot.get("revision")}


def _attempt_tier(step, archive):
    context = step.get("native_context_before")
    if not isinstance(context, Mapping):
        return None, []
    schedule = context.get("current_schedule")
    values = [("native_context_before.audition_number", context.get("audition_number")),
              ("native_context_before.current_schedule.step_select_number",
               schedule.get("step_select_number") if isinstance(schedule, Mapping) else None)]
    present = [(name, value) for name, value in values if value is not None]
    _require(all(type(value) is int and value > 0 for _, value in present)
             and len({value for _, value in present}) <= 1, "actual audition tier fields contradict")
    tier = present[0][1] if present else None
    archived = archive.get("native_context", {})
    if archived.get("audition_number") is not None and tier is not None:
        _require(archived["audition_number"] == tier, "audition tier differs from the retired request context")
    return tier, [name for name, _ in present]


def verify_final_terminal_from_outcome(outcome, *, run_id, produce_id, idol_card_id, command_root):
    """Verify the last settled action's actual terminal source; never promote it."""
    _require(isinstance(outcome, Mapping) and outcome.get("accepted") is True and outcome.get("terminal") is True
             and outcome.get("source_run_id") == run_id and isinstance(outcome.get("steps"), list)
             and bool(outcome["steps"]), "accepted same-run terminal outcome with action history required")
    step = outcome["steps"][-1]
    _require(isinstance(step, Mapping) and step.get("status") == "settled" and step.get("input_submitted") is True,
             "last main action is not settled; earlier terminal markers cannot replace it")
    provenance = step.get("native_provenance")
    _require(isinstance(provenance, Mapping), "last action lacks native state provenance")
    resolved = resolve_archived_exam_request(step.get("request_id"), run_id=run_id, produce_id=produce_id,
        idol_card_id=idol_card_id, command_root=command_root,
        expected_generation=step.get("native_legal_session_generation"), expected_action=step.get("action"),
        expected_model_source=step.get("policy_source"))
    _require(resolved["outcome"] == "settled" and resolved["step_type_value"] == 18
             and provenance.get("base_source_sha256") == resolved["before_reference"]["sha256"],
             "settled Final archive/base-state binding differs")
    _final_identity(resolved["raw_before"], produce_id, idol_card_id, terminal=False)
    kind = provenance.get("digest_kind")
    if kind == "canonical-recorder-transition-state-after-json":
        after, source, detail = _recorder_after(provenance, resolved)
        capture_kind = "recorder-transition"
    elif kind == "canonical-runtime-command-exam-save-json":
        after, source, detail = _rpc_after(provenance, resolved, command_root)
        capture_kind = "runtime-command-snapshot"
    else:
        raise ValueError("Final workflow terminal: unsupported native provenance kind")
    _final_identity(after, produce_id, idol_card_id, terminal=True)
    canonical = canonical_json_bytes(after)
    _require(type(provenance.get("canonical_state_size")) is int
             and len(canonical) == provenance["canonical_state_size"]
             and hashlib.sha256(canonical).hexdigest() == provenance.get("canonical_state_sha256"),
             "terminal state bytes differ from the immutable outcome")
    before = AuditionLocalSaveStateEvidence.from_dict(resolved["archive"]["before"])
    _, checked = adapt_runtime_action_state_evidence(before, after, telemetry_path=source["path"],
        telemetry_end_offset=provenance["telemetry_end_offset"], capture_kind=capture_kind)
    _require(checked.to_dict() == dict(provenance), "parsed terminal state/provenance differs")
    tier, tier_sources = _attempt_tier(step, resolved["archive"])
    npcs = after.get("npcDataList")
    npc_scores = ([{"id": row.get("_id"), "number": row.get("_number"), "score": row["_currentScore"]} for row in npcs]
                  if isinstance(npcs, list) and npcs and all(isinstance(row, Mapping)
                      and type(row.get("_currentScore")) is int and row["_currentScore"] >= 0 for row in npcs) else None)
    return {"schema": SCHEMA, "run_id": run_id, "produce_id": produce_id, "idol_card_id": idol_card_id,
        "step_type_value": 18, "request_id": resolved["request_id"],
        "session_generation": resolved["request"]["session_generation"], "native_sequence": str(resolved["sequence_id"]),
        "final_score": after["parameter"], "terminal": True, "workflow_terminal_verified": True,
        "audition_number": tier, "audition_number_sources": tier_sources, "npc_scores_observed": npc_scores,
        "score_above_all_observed_npcs": (all(after["parameter"] > row["score"] for row in npc_scores) if npc_scores else None),
        "official_win": None, "terminal_is_not_by_itself_a_win": True,
        "capture_kind": capture_kind, "terminal_source": source, "source_detail": detail,
        "archive_reference": resolved["archive_reference"], "before_reference": resolved["before_reference"],
        "canonical_state_sha256": provenance["canonical_state_sha256"], "canonical_state_size": len(canonical),
        "policy_model_sha256": (step.get("policy_source") or {}).get("model_sha256"),
        "training_admitted": False, "existing_training_capture_flags_changed": False,
        "policy_win_or_score_improvement_proven": False, "game_io": False}


__all__ = ["SCHEMA", "verify_final_terminal_from_outcome"]
