"""File-only membership seal for completed cases before an owned child discard.

This does not certify capture quality or shared-provider final cleanup. The
normal clean-parent path remains with its existing validator. No capture file
is repaired, re-written, or replayed here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


# Reviewed producers: their exit 4 paths preserve uncertain native ownership
# until process termination. Unknown producer semantics require a new review.
DISCARD_PRODUCERS = {
    "pc_replay_batch.py": "690f332105e6f3514d27d9042e1b46151701b2528a7aeeb9ac002c4ade71527d",
    "pc_replay_execution.py": "149a6ea28ce76fa35d7e58c6ba3fae4803ac277144c4d0ba1edb7481aad3c24f",
}
PENDING_KEYS = ("continuations", "waiting_continuations", "runner_tail", "runner_wait_count")


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf8"))
    _require(isinstance(value, dict), "object receipt required: " + str(path))
    return value


def _ref(path: Path) -> dict:
    path = path.resolve()
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "sha256": digest, "bytes": path.stat().st_size}


def _lifecycle(episode: dict, execution: dict, master_hash: str, baseline: int) -> None:
    """Reusable-owner facts only; sibling score/candidate quality stays local."""
    _require(episode.get("status") == "completed", "journal member is not completed")
    _require(episode.get("game_io") is False, "case is not offline")
    for key in ("roots_before", "roots_after_release"):
        _require(type(episode.get(key)) is int and episode[key] == baseline,
                 "case roots did not return to the provider baseline")
    static = episode["original_static_boundary"]
    fields = static.get("container_fields_null")
    _require(static.get("context_type") == 0 and static.get("is_replay") is False
             and static.get("repair_or_resolve_invoked") is False
             and static.get("all_22_container_fields_null") is True
             and isinstance(fields, dict) and len(fields) == 22
             and all(value is True for value in fields.values()), "original static boundary is not established")
    provider = episode["provider_boundary"]
    connections = provider.get("connections")
    _require(provider.get("master_hash") == master_hash and provider.get("provider_attached") is True
             and provider.get("all_connection_refcounts_zero") is True
             and isinstance(connections, list) and bool(connections)
             and all(type(row.get("reference_count")) is int and row["reference_count"] == 0
                     for row in connections), "case provider boundary is not established")
    _require(execution.get("execute_attempts") == 1 and execution.get("task_status") == "Succeeded"
             and execution.get("task_consumed") is True and execution.get("get_result_succeeded") is True
             and execution.get("native_replay_complete") is True and not execution.get("error")
             and execution.get("game_process_used") is False, "case native task boundary is not established")
    markers = execution.get("markers", {})
    _require(markers.get("read_errors") == []
             and all(markers.get(key) is False for key in ("in_progress", "command_playing", "has_error"))
             and all(markers.get(key) is True for key in ("queue_empty", "end_exam", "exam_end_complete")),
             "case terminal lifecycle markers are incomplete")
    loop = execution.get("owned_loop", {})
    pending = (loop.get("cleanup_ticks") or [loop.get("pending", [])])[-1]
    _require(loop.get("restored") is True and not loop.get("pending_at_exit") and bool(pending)
             and all(type(row.get(key)) is int and row[key] == 0 for row in pending for key in PENDING_KEYS),
             "case original loop boundary is not established")
    observer = execution.get("observer", {})
    stats = observer.get("native_stats", {})
    _require(observer.get("hooks_restored") is True and observer.get("errors") == []
             and type(stats.get("active")) is int and stats["active"] == 0
             and type(stats.get("error_latched")) is int and stats["error_latched"] == 0,
             "case observer boundary is not established")
    summary = episode["replay"]
    expected = {"actual_score": markers.get("judge_parameter"),
                "expected_score": execution.get("expected_original_score"),
                "terminal_score_matches": execution.get("terminal_score_matches"),
                "native_replay_complete": execution.get("native_replay_complete"),
                "source_actions_consumed": markers.get("log_index"),
                "expected_source_action_count": execution.get("expected_source_action_count"),
                "source_action_cursor_complete": execution.get("source_action_cursor_complete"),
                "owned_loop_restored": loop.get("restored"),
                "observer_hooks_restored": observer.get("hooks_restored"),
                "terminal_snapshot_complete": execution.get("terminal_snapshot", {}).get("state_complete")}
    _require(all(key in summary and summary[key] == value for key, value in expected.items()),
             "case terminal summary differs from bound execution")


def verify_discarded_completed_prefix(run_dir: str | Path, *, episode: dict | None = None,
                                     inputs: dict | None = None) -> dict[str, Any]:
    """Verify a finite completed prefix after a proven intentional owned exit 4.

    Optional objects must equal their on-disk receipts; they never replace
    evidence reads. A failed result grants neither membership nor cleanup.
    """
    evidence: dict[str, dict] = {}
    result: dict[str, Any] = {
        "schema": "gkms.discarded-completed-prefix-seal.v1",
        "passed": False, "errors": [], "closure_kind": "owned-process-discard",
        "file_only": True, "game_io": False, "evidence": evidence,
        "capture_quality_verified": False,
        "shared_parent_provider_cleanup_claimed": False,
        "process_discard_boundary": {"passed": False},
    }
    try:
        run = Path(run_dir).resolve()

        def read(path: Path) -> dict:
            reference = _ref(path)
            evidence[str(path.resolve())] = reference
            value = _read(path)
            _require(_ref(path) == reference, "receipt changed while reading: " + str(path))
            return value

        def checked(reference: dict, path: Path) -> dict:
            _require(isinstance(reference, dict) and Path(reference.get("path", "")).resolve() == path.resolve(),
                     "bound receipt path differs: " + str(path))
            value = read(path)
            actual = evidence[str(path.resolve())]
            _require(reference.get("sha256") == actual["sha256"]
                     and ("bytes" not in reference or reference["bytes"] == actual["bytes"]),
                     "bound receipt hash differs: " + str(path))
            return value

        own_inputs = read(run / "inputs.json")
        own_episode = read(run / "pc_replay_batch_episode.json")
        _require(inputs is None or inputs == own_inputs, "supplied inputs differ from capture")
        _require(episode is None or episode == own_episode, "supplied episode differs from capture")
        parent_value = own_inputs.get("owned_batch_parent")
        _require(isinstance(parent_value, str) and bool(parent_value), "owned parent is not declared")
        parent = Path(parent_value).resolve()
        result["parent"] = str(parent)
        _require(run.parent == parent / "cases", "case is outside the declared parent")
        process = read(parent / "process_outcome.json")
        worker = read(parent / "worker.json")
        _require(type(process.get("exit_code")) is int and process["exit_code"] == 4
                 and process.get("timeout") is False and process.get("game_process_used") is False,
                 "parent is not an observed non-timeout owned exit 4")
        _require(type(process.get("pid")) is int and process["pid"] > 0
                 and process["pid"] == worker.get("pid") and worker.get("game_process_used") is False,
                 "parent worker/process ownership differs")
        batch = read(parent / "pc_replay_batch.json")
        count, total = batch.get("completed_prefix_count"), batch.get("case_count")
        _require(batch.get("schema") == "gkms.original-pc-owned-replay-batch.v1"
                 and batch.get("batch_complete") is False and batch.get("single_managed_owner") is True
                 and batch.get("game_process_used") is False
                 and batch.get("shared_provider_rebuilt_between_cases") is False,
                 "parent owned batch contract differs")
        _require(type(count) is int and type(total) is int and 0 < count < total,
                 "discard has no bounded nonempty completed prefix")
        target_index = own_episode.get("case_index")
        _require(type(target_index) is int and 0 <= target_index < count
                 and own_episode.get("status") == "completed", "target is outside the completed prefix")
        active = batch.get("active_case", {})
        active_run = parent / "cases" / f"{count:04d}"
        _require(active.get("case_index") == count
                 and Path(active.get("receipt_path", "")).resolve() == active_run / "pc_replay_batch_episode.json",
                 "failed active case does not immediately follow the completed prefix")
        parent_inputs = read(parent / "inputs.json")
        _require(parent_inputs.get("executed_from_source_snapshot") is True
                 and parent_inputs.get("game_process_used") is False, "producer was not the frozen owned worker")
        for name, digest in DISCARD_PRODUCERS.items():
            sources = [row for row in parent_inputs.get("source_artifacts", []) if Path(row.get("path", "")).name == name]
            _require(len(sources) == 1, "discard producer identity is missing or ambiguous: " + name)
            source = sources[0]
            path = parent / "source_snapshot" / name
            _require(Path(source.get("copy", "")).resolve() == path and source.get("sha256") == digest,
                     "discard producer has not been reviewed: " + name)
            evidence[str(path)] = _ref(path)
            _require(evidence[str(path)]["sha256"] == digest, "frozen discard producer hash differs: " + name)
        prepared = checked(batch["prepared_inputs"], parent / "pc_replay_batch_inputs.json")
        plan = checked(parent_inputs["replay_batch"], parent / "prepared_batch.json")
        request = checked(plan["request"], parent / "source_snapshot/batch_request.json")
        cases = prepared.get("cases", [])
        entries = request.get("entries", [])
        _require(plan.get("schema") == "gkms.pc-prepared-replay-batch.v1"
                 and request.get("schema") == "gkms.pc-replay-batch-request.v1"
                 and plan.get("master_hash") == request.get("master_hash") == batch.get("master_hash")
                 and len(cases) == len(entries) == len(plan.get("entries", [])) == total,
                 "frozen request/prepared batch cardinality or Master differs")
        _require(len({case.get("case_id") for case in cases}) == total, "prepared case identities are duplicated")
        for index, member in enumerate(cases):
            expected_run = parent / "cases" / f"{index:04d}"
            entry, original = plan["entries"][index], entries[index]
            _require(member.get("case_index") == index and Path(member.get("output", "")).resolve() == expected_run
                     and member.get("case_id") == entry.get("case_id") == original.get("case_id")
                     and member.get("source_sha256") == entry.get("source_sha256") == original.get("source", {}).get("sha256")
                     and member.get("identity", {}).get("episode_id") == entry.get("episode_id") == original.get("episode_id")
                     and member.get("master_hash") == batch["master_hash"], "frozen ordered request membership differs")

        def bound_case(index: int) -> tuple[dict, dict]:
            member = cases[index]
            case_run = parent / "cases" / f"{index:04d}"
            receipt = read(case_run / "pc_replay_batch_episode.json")
            _require(all(receipt.get(key) == value for key, value in member.items()), "prepared case receipt differs")
            case_inputs = checked(receipt["inputs"], case_run / "inputs.json")
            _require(Path(case_inputs.get("owned_batch_parent", "")).resolve() == parent
                     and case_inputs.get("batch_case_id") == member["case_id"], "case owner identity differs")
            source = case_run / "source_snapshot/replay_source.json"
            _require(Path(member.get("source_path", "")).resolve() == source, "case source path differs")
            source_value = read(source)
            _require(evidence[str(source)]["sha256"] == member["source_sha256"] == case_inputs.get("original_replay_sha256"),
                     "case original source hash differs")
            _require(member.get("expected_score") == source_value["expected"]["terminal_score"]
                     and member.get("expected_actions") == len(source_value["expected"]["actions"]),
                     "prepared score/action expectation differs from source")
            checked(receipt["difficulty_input"], case_run / "source_snapshot/difficulty_input.json")
            return receipt, case_inputs

        failed, _ = bound_case(count)
        _require(active.get("case_id") == failed.get("case_id"), "active failed case identity differs")
        direct = False
        explicit = (batch.get("owned_process_discard_required") is True and batch.get("process_exit_code") == 4
                    and batch.get("failed_case_index") == count and batch.get("failed_case_id") == failed["case_id"]
                    and failed.get("status") == "failed-owned-process-discard-required"
                    and failed.get("original_cleanup_not_forced") is True and failed.get("process_exit_code") == 4
                    and bool(failed.get("error")) and bool(batch.get("error")))
        if any(key in batch for key in ("owned_process_discard_required", "process_exit_code", "failed_case_index", "failed_case_id")):
            _require(explicit, "explicit parent discard receipt contradicts its active failed case")
        failed_execution_path = active_run / "pc_replay_execution.json"
        if failed_execution_path.is_file():
            failed_execution = read(failed_execution_path)
            direct = (failed.get("status") == "running"
                      and failed_execution.get("schema") == "gkms.original-pc-replay-execution.v1"
                      and failed_execution.get("owned_process_exit_preserves_live_async_ownership") is True
                      and failed_execution.get("game_process_used") is False
                      and failed_execution.get("execute_attempts") == 1 and bool(failed_execution.get("error"))
                      and (failed_execution.get("quiescent_after_failure") is False
                           or bool(failed_execution.get("owned_loop", {}).get("pending_at_exit"))))
        _require(explicit or direct, "failed active case does not prove intentional ownership-preserving discard")
        journal_path = parent / "pc_replay_batch_completed.jsonl"
        _require(Path(batch.get("completed_journal_path", "")).resolve() == journal_path, "journal path differs")
        journal_reference = _ref(journal_path)
        raw = journal_path.read_text(encoding="utf8")
        _require(bool(raw) and raw.endswith("\n"), "completed journal has an incomplete tail")
        rows = [json.loads(line) for line in raw.splitlines()]
        _require(len(rows) == count and [row.get("case_index") for row in rows] == list(range(count)),
                 "completed journal is not the exact contiguous prefix")
        _require(_ref(journal_path) == journal_reference, "completed journal changed while reading")
        evidence[str(journal_path)] = journal_reference
        baseline = None
        for index, row in enumerate(rows):
            receipt, _ = bound_case(index)
            episode_path = parent / "cases" / f"{index:04d}" / "pc_replay_batch_episode.json"
            checked(row["receipt"], episode_path)
            _require(all(row.get(key) == receipt.get(key) for key in ("case_id", "case_index", "identity", "source_sha256", "replay")),
                     "journal completion differs from bound case receipt")
            execution = checked(receipt["replay_receipt"], episode_path.parent / "pc_replay_execution.json")
            initialization = checked(receipt["initialization_receipt"], episode_path.parent / "pc_exam_initialization.json")
            _require(initialization.get("exam_data_initialized") is True and not initialization.get("error"),
                     "case initialization was not completed successfully")
            if baseline is None:
                baseline = receipt.get("roots_before")
                _require(type(baseline) is int and baseline >= 0, "provider root baseline missing")
            _lifecycle(receipt, execution, batch["master_hash"], baseline)
        # Recheck all consumed receipts after the proof, including the observed
        # process exit. The returned digest is a derived seal, not a fabricated
        # producer completed_journal field.
        _require(all(_ref(Path(path)) == reference for path, reference in evidence.items()),
                 "capture evidence changed during prefix verification")
        result.update(passed=True, case_index=target_index, case_id=own_episode["case_id"],
                      episode_id=own_episode["identity"]["episode_id"], source_sha256=own_episode["source_sha256"],
                      completed_prefix_count=count,
                      journal_receipt=journal_reference,
                      process_discard_boundary={"passed": True,
                          "evidence_kind": "batch-discard-receipt" if explicit else "active-replay-discard-receipt",
                          "parent_exit_code": 4, "timeout": False, "failed_case_index": count,
                          "scope": "OS-confirmed owned child exit; shared provider/getter/pool cleanup is not claimed"})
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        result["errors"].append({"field": "discarded_completed_prefix", "reason": str(error)})
    return result
