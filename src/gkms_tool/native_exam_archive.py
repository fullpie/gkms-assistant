"""Verify a retired native Exam transaction without changing its history."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import re

from .runtime_command_client import RuntimeCommandRequest

DEFAULT_COMMAND_ROOT = Path(__file__).resolve().parents[2] / "var/runtime_command_bridge"


def _require(value, reason):
    if not value:
        raise ValueError("Archived native Exam request: " + reason)


def _reference(path):
    raw = path.read_bytes()
    return raw, {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _pointer(value):
    _require(isinstance(value, (int, str)) and not isinstance(value, bool), "sequence identity missing")
    text = str(value).removeprefix("native-sequence:")
    return int(text, 16 if text.lower().startswith("0x") else 10)


def resolve_archived_exam_request(request_id, *, run_id, produce_id, idol_card_id,
                                  expected_generation, expected_action, expected_model_source=None,
                                  command_root=DEFAULT_COMMAND_ROOT):
    """Bind the same retired request to its recorded action, session and run.

    This resolves transaction status only. It does not qualify a transition,
    source feature representation, legal pool or training dataset.
    """
    _require(isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id),
             "unsafe request identifier")
    root = Path(command_root).resolve()
    path = root / "completed_exam" / (request_id + ".json")
    _require(path.resolve().is_relative_to(root), "archive path escaped its root")
    raw, archive = _reference(path)
    value = json.loads(raw)
    _require(isinstance(value, Mapping) and value.get("schema") == "gkms.runtime-pending-exam.v1"
             and value.get("outcome") in {"settled", "rejected"}, "retired terminal outcome required")
    request = value.get("request")
    _require(isinstance(request, Mapping) and request.get("schema") == "gkms.runtime-command.v1"
             and request.get("request_id") == request_id, "request identity differs")
    RuntimeCommandRequest(request_id, request.get("session_generation"), request.get("command"),
                          request.get("expected_revision"), request.get("target"), request.get("continuation_of"))
    _require(isinstance(expected_generation, str) and bool(expected_generation)
             and request["session_generation"] == expected_generation, "recorded session differs")
    _require(isinstance(expected_action, Mapping) and expected_action.get("kind") in {"play", "drink", "end_turn"}
             and request["command"] == "exam." + expected_action["kind"]
             and value.get("kind") == expected_action["kind"], "recorded main action differs")
    if expected_action["kind"] != "end_turn":
        key = "card_guid" if expected_action["kind"] == "play" else "drink_id"
        target = request["target"]
        _require(type(expected_action.get("slot")) is int and target.get("slot") == expected_action["slot"]
                 and type(value.get("slot")) is int and value["slot"] == target["slot"]
                 and isinstance(expected_action.get(key), str) and bool(expected_action[key])
                 and target.get(key) == value.get(key) == expected_action[key], "recorded target differs")
    else:
        _require(request.get("target") in (None, {}) and value.get("slot") == 0,
                 "end-turn target differs")
    before, context = value.get("before"), value.get("native_context")
    _require(isinstance(before, Mapping) and before.get("run_id") == run_id
             and isinstance(context, Mapping) and context.get("produce_id") == produce_id
             and context.get("idol_card_id") == idol_card_id, "run/mode/idol binding differs")
    _require(_pointer(before.get("session_transition_id")) == _pointer(value.get("sequence_id")),
             "original native sequence differs")
    before_path = Path(before.get("source_path", ""))
    before_raw, before_ref = _reference(before_path)
    _require(before_ref["sha256"] == before.get("source_sha256")
             and before_ref["bytes"] == before.get("source_size"), "original before-state source changed")
    state = json.loads(before_raw)
    _require(isinstance(state, Mapping) and state.get("produceId") == produce_id
             and state.get("idolCardId") == idol_card_id
             and type(state.get("stepType")) is int and context.get("step_type") == state["stepType"]
             and before.get("step_context_id") == "exam-step:" + str(state.get("stepType")),
             "original before-state identity differs")
    binding = value.get("model_policy_binding")
    if binding is not None:
        _require(isinstance(binding, Mapping) and binding.get("run_id") == run_id
                 and isinstance(expected_model_source, Mapping)
                 and all(key in expected_model_source and expected_model_source[key] == item for key, item in binding.items()),
                 "original model/source binding differs")
    else:
        _require(not isinstance(expected_model_source, Mapping) or expected_model_source.get("loaded") is not True,
                 "learned policy archive has no model binding")
    return {"schema": "gkms.archived-native-exam-request-resolution.v1", "request_id": request_id,
        "outcome": value["outcome"], "archive": dict(value), "archive_reference": archive, "request": dict(request),
        "raw_before": state, "before_reference": before_ref, "sequence_id": value["sequence_id"],
        "step_type_value": state["stepType"], "run_id": run_id, "produce_id": produce_id,
        "idol_card_id": idol_card_id, "model_policy_binding": binding,
        "original_request_replayed": False, "training_admitted": False}


__all__ = ["DEFAULT_COMMAND_ROOT", "resolve_archived_exam_request"]
