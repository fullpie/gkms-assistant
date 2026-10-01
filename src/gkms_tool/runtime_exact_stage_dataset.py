"""Promote only fresh legal-verified native recorder stages into RL rows.

The caller owns identity binding: trajectory, capture-source, and main-action
surface evidence IDs must come from the authoritative Replay job/binding or
the durable Live run, never from an invented per-file ordinal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from .audition_local_save_state import EXAM_SAVE_DATA_ROOT_FIELDS
from .canonical_training_labels import INNER_TRANSITION_SCHEMA
from .training_artifact_io import (
    atomic_write as _atomic_write,
    canonical_json_bytes as _canonical_bytes,
    sha256_file as _sha256_file,
)


SCHEMA: Final = "gkms.runtime-exact-stage-dataset.v1"
RECORDER_SCHEMA: Final = "gkms.runtime-exam-recorder.shadow.v1"
LEGAL_PROBE_TARGET: Final = "gkms_runtime_exam_recorder_legal_verified_probe"
LEGAL_PROBE_MACRO: Final = "GKMS_RUNTIME_LEGAL_VERIFIED_PROBE"
REQUIRED_STATE_FIELDS: Final = frozenset(EXAM_SAVE_DATA_ROOT_FIELDS)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _state(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, UnicodeError, ValueError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _plain_number(value: object) -> int | float | None:
    return (
        value
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _native_boundary_valid(body: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Recognize existing recorder finalizers without changing legacy records.

    The two newer native branches require an official AddPlayLog commit and
    physical queue drain before serializing a stable Main decision. Their
    distinct pairing reasons preserve which branch produced the original row;
    the persisted Main state must independently agree with that boundary.
    """
    boundary = body.get("boundary")
    if isinstance(boundary, str) and boundary in {"ExamSequence.AddExecuteCommand(next-manual-before)", "terminal-marker"}:
        return True
    reasons = {
        "ExamSequence.set_IsCommandPlaying(false)-after": "same-action-queue-drain-stable-main",
        "ExamSequence.AddExecuteCommand(next-main-decision-before)": "prior-official-action-drained-next-main-decision",
    }
    action = body.get("action")
    return (isinstance(boundary, str) and boundary in reasons
        and (boundary != "ExamSequence.set_IsCommandPlaying(false)-after"
             or (isinstance(action, Mapping) and type(action.get("play_type")) is int
                 and action["play_type"] in (2, 3)))
        and body.get("pairing_reason") == reasons[boundary]
        and body.get("source") == "managed-runtime-hook"
        and body.get("official_action_captured") is True
        and body.get("official_action_duplicate") is False
        and body.get("official_action_authority") == "ExamParameterModel.AddPlayLog(ExamPlayLog)"
        and type(body.get("official_action_order")) is int and body["official_action_order"] >= 0
        and body.get("managed_thread_match") is True
        and body.get("state_before_captured") is True and body.get("state_after_captured") is True
        and body.get("action_state_pairing_candidate") is True
        and body.get("transition_promotion_candidate") is True
        and body.get("terminal_known") is True and body.get("terminal") is False
        and body.get("terminal_predicate") is False and body.get("terminal_completion") is False
        and type(after.get("phase")) is int and after["phase"] == 6
        and after.get("isTurnCardPlayEnd") is False
        and after.get("isExamEndComplete") is False and after.get("commandList") == [])


def _normalized_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """Add exact aliases required by the generic training contract."""

    result = dict(value)
    aliases = {
        "score": "parameter",
        "current_turn": "currentTurn",
        "remain_turn": "remainTurn",
        "turn_card_play_count": "turnCardPlayCount",
        "exam_card_play_count": "examCardPlayCount",
        "is_turn_card_play_end": "isTurnCardPlayEnd",
        "random_state": "random",
        "max_stamina": "maxStamina",
        "step_type_value": "stepType",
    }
    for target, source in aliases.items():
        result[target] = value.get(source)
    return result


def _selected_candidate(
    action: Mapping[str, Any], candidates: Sequence[object]
) -> tuple[int, Mapping[str, Any]] | None:
    action_type = action.get("action_type")
    play_index = action.get("play_index")
    matches: list[tuple[int, Mapping[str, Any]]] = []
    for index, raw_candidate in enumerate(candidates):
        if not isinstance(raw_candidate, Mapping):
            continue
        candidate_type = raw_candidate.get("action_type", raw_candidate.get("kind"))
        type_match = candidate_type == action_type
        if action_type == "turn-end":
            type_match = candidate_type in {"turn-end", "turn_end", "end_turn"}
        slot = raw_candidate.get("slot_index", raw_candidate.get("slot"))
        if not type_match or slot != play_index:
            continue
        if action_type == "use-hand":
            source_card = action.get("source_card")
            if not isinstance(source_card, Mapping):
                continue
            if (
                raw_candidate.get("card_guid", raw_candidate.get("guid"))
                != source_card.get("guid")
                or raw_candidate.get("card_id") != source_card.get("id")
            ):
                continue
        elif action_type == "use-drink":
            if raw_candidate.get("drink_id") != action.get("source_drink_id"):
                continue
        matches.append((index, raw_candidate))
    return matches[0] if len(matches) == 1 else None


def _legal_candidate_contract_valid(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("legal") is not True:
        return False
    slot = value.get("slot_index", value.get("slot"))
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
        return False
    action_type = value.get("action_type", value.get("kind"))
    if action_type == "use-hand":
        guid = value.get("card_guid", value.get("guid"))
        return (
            isinstance(value.get("card_id"), str)
            and bool(value["card_id"])
            and isinstance(guid, str)
            and bool(guid)
            and isinstance(value.get("upgrade"), int)
            and not isinstance(value.get("upgrade"), bool)
        )
    if action_type == "use-drink":
        return isinstance(value.get("drink_id"), str) and bool(value["drink_id"])
    if action_type in {"turn-end", "turn_end", "end_turn"}:
        return value.get("action_id") == "END_TURN"
    return False


def _identity(state: Mapping[str, Any]) -> dict[str, object]:
    return {
        key: state.get(key)
        for key in (
            "produceId",
            "idolCardId",
            "settingId",
            "characterId",
            "examType",
            "stepType",
            "planType",
            "mainEffectType",
            "limitTurn",
            "isReplay",
        )
    }


def _read_source(
    path: Path,
) -> tuple[list[dict[str, Any]], dict[str, object]]:
    transitions: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    legal_preflights: list[dict[str, object]] = []
    recorder_preflights: list[dict[str, object]] = []
    rows_seen = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            rows_seen += 1
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, UnicodeError, ValueError) as error:
                parse_errors.append(f"line-{line_number}:{type(error).__name__}")
                continue
            if not isinstance(row, Mapping) or row.get("schema") != RECORDER_SCHEMA:
                continue
            body = row.get("body")
            if not isinstance(body, Mapping):
                continue
            if body.get("record") == "legal_verified_probe_preflight":
                legal_preflights.append(
                    {
                        "line_number": line_number,
                        "pid": row.get("pid"),
                        "sequence": row.get("sequence"),
                        "valid": (
                            body.get("valid") is True
                            and body.get("target") == LEGAL_PROBE_TARGET
                            and body.get("macro") == LEGAL_PROBE_MACRO
                            and body.get("transition_promotion_gate")
                            == "python-stage-89-field-continuity-v1"
                        ),
                    }
                )
            elif body.get("record") == "preflight":
                recorder_preflights.append(
                    {
                        "line_number": line_number,
                        "pid": row.get("pid"),
                        "sequence": row.get("sequence"),
                        "valid": (
                            body.get("valid") is True
                            and body.get("atomic_hooks") is True
                        ),
                    }
                )
            elif body.get("record") == "transition":
                transitions.append(
                    {"line_number": line_number, "row": row, "body": body}
                )
    return transitions, {
        "rows_seen": rows_seen,
        "parse_errors": parse_errors,
        "legal_verified_preflights": legal_preflights,
        "recorder_preflights": recorder_preflights,
    }


def split_runtime_exact_stages(
    transitions: Sequence[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], bool]:
    stages: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for transition in transitions:
        current.append(transition)
        if transition["body"].get("terminal") is True:
            stages.append(current)
            current = []
    incomplete_suffix = bool(current)
    if current:
        stages.append(current)
    return stages, incomplete_suffix


def validate_runtime_exact_stage(
    stage: Sequence[dict[str, Any]],
    *,
    stage_index: int,
    source_sha256: str,
    trajectory_id: str,
    capture_source_id: str,
    expected_mode: str,
    main_action_surface_evidence: str,
    scan: Mapping[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    blockers: set[str] = set()
    prepared: list[
        tuple[
            dict[str, Any],
            Mapping[str, Any],
            Mapping[str, Any],
            Mapping[str, Any],
            list[object],
            Mapping[str, Any],
            int,
        ]
    ] = []
    identities: list[dict[str, object]] = []
    orders: list[int] = []
    envelope_sequences: list[int] = []
    stage_pids: set[int] = set()
    for item in stage:
        row = item["row"]
        body = item["body"]
        before = _state(body.get("state_before"))
        after = _state(body.get("state_after"))
        action = body.get("action")
        legal = body.get("legal_actions")
        runtime_probe = body.get("runtime_legal_action_probe")
        membership = body.get("official_chosen_membership")
        if row.get("exact") is not False:
            blockers.add("native-envelope-must-remain-candidate")
        if row.get("legal_actions_complete") is not True:
            blockers.add("envelope-legal-actions-incomplete")
        pid = row.get("pid")
        sequence = row.get("sequence")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            blockers.add("transition-pid-invalid")
        else:
            stage_pids.add(pid)
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
            blockers.add("transition-sequence-invalid")
        else:
            envelope_sequences.append(sequence)
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            blockers.add("native-state-missing")
            continue
        if set(before) != REQUIRED_STATE_FIELDS or set(after) != REQUIRED_STATE_FIELDS:
            blockers.add("native-state-root-inventory-mismatch")
        if not (
            body.get("state_before_captured") is True
            and body.get("state_after_captured") is True
            and body.get("state_before_complete") is False
            and body.get("state_after_complete") is False
            and body.get("action_state_exact") is False
            and body.get("action_state_pairing_candidate") is True
            and body.get("transition_promotion_candidate") is True
            and body.get("transition_promotion_gate")
            == "python-stage-89-field-continuity-v1"
            and body.get("managed_thread_match") is True
            and body.get("terminal_known") is True
        ):
            blockers.add("native-action-state-contract-incomplete")
        if not isinstance(action, Mapping) or action.get("known") is not True:
            blockers.add("native-action-identity-missing")
            continue
        if action.get("isManual") is not True:
            blockers.add("non-manual-action")
        if not isinstance(legal, Mapping) or not (
            legal.get("complete") is True
            and legal.get("legal_actions_complete") is True
            and legal.get("exact") is True
            and isinstance(legal.get("actions"), list)
        ):
            blockers.add("unified-legal-set-incomplete")
            continue
        if not isinstance(runtime_probe, Mapping) or not (
            runtime_probe.get("legal_actions_complete") is True
            and runtime_probe.get("exact") is True
            and runtime_probe.get("candidate_actions_authoritative") is True
            and isinstance(runtime_probe.get("promotion"), Mapping)
            and runtime_probe["promotion"].get("allowed") is True
        ):
            blockers.add("runtime-legal-probe-not-promoted")
        candidates = list(legal["actions"])
        if not candidates or any(
            not _legal_candidate_contract_valid(value) for value in candidates
        ):
            blockers.add("legal-candidate-contract-invalid")
        if len({_canonical_bytes(value) for value in candidates}) != len(candidates):
            blockers.add("duplicate-legal-candidate")
        selected = _selected_candidate(action, candidates)
        if selected is None:
            blockers.add("chosen-action-not-unique-legal-member")
            continue
        if not isinstance(membership, Mapping) or not (
            membership.get("known") is True
            and membership.get("member") is True
            and membership.get("probe_complete") is True
            and membership.get("action") == action
        ):
            blockers.add("native-chosen-membership-unverified")
        if selected[1].get("legal") is not True:
            blockers.add("selected-candidate-not-legal")
        expected_play_type = {
            "use-hand": 2,
            "use-drink": 3,
            "turn-end": 12,
        }.get(action.get("action_type"))
        if expected_play_type is None or action.get("play_type") != expected_play_type:
            blockers.add("native-action-type-code-mismatch")
        order = body.get("action_order")
        if not isinstance(order, int) or isinstance(order, bool):
            blockers.add("native-action-order-invalid")
            continue
        if not _native_boundary_valid(body, after):
            blockers.add("native-boundary-invalid")
        identities.extend((_identity(before), _identity(after)))
        if (
            _plain_number(before.get("parameter")) is None
            or _plain_number(after.get("parameter")) is None
        ):
            blockers.add("native-score-not-numeric")
        orders.append(order)
        prepared.append(
            (item, before, after, action, candidates, selected[1], selected[0])
        )

    if not stage:
        blockers.add("empty-stage")
    if len(prepared) != len(stage):
        blockers.add("stage-row-rejected")
    if len(stage_pids) != 1:
        blockers.add("stage-pid-crossing")
    if (
        len(envelope_sequences) != len(stage)
        or len(set(envelope_sequences)) != len(envelope_sequences)
        or envelope_sequences != sorted(envelope_sequences)
    ):
        blockers.add("transition-envelope-sequence-invalid")
    if stage_pids and stage:
        pid = next(iter(stage_pids))
        first_line = min(int(value["line_number"]) for value in stage)
        first_sequence = min(envelope_sequences) if envelope_sequences else None
        for key, blocker in (
            ("legal_verified_preflights", "legal-verified-preflight-unbound"),
            ("recorder_preflights", "recorder-preflight-unbound"),
        ):
            candidates = scan.get(key)
            bound = (
                isinstance(candidates, list)
                and any(
                    isinstance(value, Mapping)
                    and value.get("valid") is True
                    and value.get("pid") == pid
                    and isinstance(value.get("line_number"), int)
                    and value["line_number"] < first_line
                    and isinstance(value.get("sequence"), int)
                    and isinstance(first_sequence, int)
                    and value["sequence"] < first_sequence
                    for value in candidates
                )
            )
            if not bound:
                blockers.add(blocker)
    if orders and orders != list(range(orders[0], orders[0] + len(orders))):
        blockers.add("native-action-order-gap")
    if identities and any(value != identities[0] for value in identities[1:]):
        blockers.add("stage-identity-crossing")
    if prepared:
        first_before = prepared[0][1]
        if not (
            first_before.get("currentTurn") == 1
            and first_before.get("turnCardPlayCount") == 0
            and first_before.get("examCardPlayCount") == 0
            and first_before.get("isTurnCardPlayEnd") is False
        ):
            blockers.add("stage-does-not-start-at-authoritative-turn-one")
        terminals = [
            index
            for index, value in enumerate(prepared)
            if value[0]["body"].get("terminal") is True
        ]
        if terminals != [len(prepared) - 1]:
            blockers.add("terminal-not-once-last")
        terminal_after = prepared[-1][2]
        if terminal_after.get("isExamEndComplete") is not True:
            blockers.add("terminal-state-not-complete")
        for current, following in zip(prepared, prepared[1:], strict=False):
            if _canonical_bytes(current[2]) != _canonical_bytes(following[1]):
                blockers.add("adjacent-native-state-discontinuity")
                break

    identity = identities[0] if identities else {}
    source_mode = "replay" if identity.get("isReplay") is True else "live"
    if source_mode != expected_mode:
        blockers.add("runtime-source-mode-mismatch")
    required_identity = {
        "produceId": str,
        "idolCardId": str,
        "settingId": str,
        "characterId": str,
        "examType": int,
        "stepType": int,
        "planType": int,
        "mainEffectType": int,
        "limitTurn": int,
        "isReplay": bool,
    }
    for key, kind in required_identity.items():
        value = identity.get(key)
        if not isinstance(value, kind) or (
            kind is int and isinstance(value, bool)
        ) or (kind is str and not value):
            blockers.add(f"stage-identity-invalid:{key}")
    rows: list[dict[str, object]] = []
    if not blockers:
        for (
            item,
            before,
            after,
            raw_action,
            candidates,
            selected,
            selected_index,
        ) in prepared:
            order = int(item["body"]["action_order"])
            before_normalized = _normalized_state(before)
            after_normalized = _normalized_state(after)
            reward = after_normalized["score"] - before_normalized["score"]
            rows.append(
                {
                    "schema": INNER_TRANSITION_SCHEMA,
                    "source": f"runtime:{source_sha256}:line:{item['line_number']}",
                    "source_id": capture_source_id,
                    "trajectory_id": trajectory_id,
                    "capture_source_id": capture_source_id,
                    "step": order,
                    "state_before": before_normalized,
                    "legal_candidates": candidates,
                    "action": dict(selected),
                    "state_after": after_normalized,
                    "reward": reward,
                    "terminal": item["body"].get("terminal") is True,
                    "candidate_set_kind": "unified",
                    "behavior_only": False,
                    "full_rl_transition": True,
                    "trajectory_complete": True,
                    "stage_return_derivable": True,
                    "metadata": {
                        "produce_id": before.get("produceId"),
                        "plan_type": before.get("planType"),
                        "exam_effect_type": before.get("mainEffectType"),
                        "step_type": before.get("stepType"),
                        "candidate_set_kind": "unified",
                        "full_rl_policy_ready": True,
                        "policy_surface": "main-action-v1",
                        "main_action_surface_evidence": main_action_surface_evidence,
                        "selected_candidate_index": selected_index,
                        "native_action_order": order,
                        "native_raw_action": dict(raw_action),
                        "runtime_source_mode": source_mode,
                        "runtime_source_sha256": source_sha256,
                        "capture_source_id": capture_source_id,
                        "runtime_stage_index": stage_index,
                        "state_authority": "native-ExamSaveData-runtime",
                        "simulator_used": False,
                    },
                }
            )
    return rows, {
        "stage_index": stage_index,
        "source_mode": source_mode,
        "identity": identity,
        "trajectory_id": trajectory_id,
        "transition_count": len(stage),
        "native_action_orders": orders,
        "accepted_transition_count": len(rows),
        "blockers": sorted(blockers),
        "passed": not blockers,
    }


def build_runtime_exact_stage_dataset(
    input_path: Path,
    output_dir: Path,
    *,
    trajectory_id: str,
    capture_source_id: str,
    expected_mode: str,
    stage_index: int | None = None,
    main_action_surface_complete: bool = False,
    main_action_surface_evidence: str | None = None,
) -> dict[str, object]:
    input_path = Path(input_path).resolve()
    output_dir = Path(output_dir).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    if not isinstance(trajectory_id, str) or not trajectory_id:
        raise ValueError("trajectory_id must be non-empty")
    if not isinstance(capture_source_id, str) or not capture_source_id:
        raise ValueError("capture_source_id must be non-empty")
    if expected_mode not in {"live", "replay"}:
        raise ValueError("expected_mode must be live or replay")
    if stage_index is not None and (
        isinstance(stage_index, bool) or not isinstance(stage_index, int) or stage_index < 0
    ):
        raise ValueError("stage_index must be a non-negative integer")
    transitions, scan = _read_source(input_path)
    stages, incomplete_suffix = split_runtime_exact_stages(transitions)
    source_sha256 = _sha256_file(input_path)
    indexed_stages = list(enumerate(stages))
    global_blockers: set[str] = set()
    if stage_index is not None:
        indexed_stages = [
            value for value in indexed_stages if value[0] == stage_index
        ]
        if not indexed_stages:
            global_blockers.add("requested-stage-index-absent")
    elif expected_mode == "replay" and len(stages) != 1:
        global_blockers.add("replay-stage-index-required")
    elif expected_mode == "live":
        step_types = []
        for _index, stage in indexed_stages:
            before = _state(stage[0]["body"].get("state_before")) if stage else None
            step_types.append(before.get("stepType") if isinstance(before, Mapping) else None)
        if (
            len(set(step_types)) != len(step_types)
            or any(not isinstance(value, int) for value in step_types)
            or step_types != sorted(step_types)
        ):
            global_blockers.add("live-run-boundary-ambiguous")
    if main_action_surface_complete is not True:
        global_blockers.add("main-action-surface-not-proven-complete")
    if not isinstance(main_action_surface_evidence, str) or not (
        main_action_surface_evidence.strip()
    ):
        global_blockers.add("main-action-surface-evidence-missing")
    all_rows: list[dict[str, object]] = []
    stage_reports: list[dict[str, object]] = []
    blocker_counts: Counter[str] = Counter()
    for selected_stage_index, stage in indexed_stages:
        rows, report = validate_runtime_exact_stage(
            stage,
            stage_index=selected_stage_index,
            source_sha256=source_sha256,
            trajectory_id=trajectory_id,
            capture_source_id=capture_source_id,
            expected_mode=expected_mode,
            main_action_surface_evidence=main_action_surface_evidence or "",
            scan=scan,
        )
        if incomplete_suffix and selected_stage_index == len(stages) - 1:
            report["blockers"] = sorted(
                set(report["blockers"]) | {"unterminated-stage-suffix"}
            )
            report["passed"] = False
            rows = []
            report["accepted_transition_count"] = 0
        all_rows.extend(rows)
        stage_reports.append(report)
        blocker_counts.update(report["blockers"])
    if scan["parse_errors"]:
        global_blockers.add("source-parse-errors")
    if not transitions:
        global_blockers.add("no-transitions")
    if global_blockers:
        all_rows = []
    accepted_stages = sum(report["passed"] is True for report in stage_reports)
    dataset_ready = bool(all_rows) and not global_blockers
    transitions_path = output_dir / "inner_transitions.jsonl"
    report_path = output_dir / "report.json"
    encoded = b"".join(_canonical_bytes(row) + b"\n" for row in all_rows)
    _atomic_write(transitions_path, encoded)
    report: dict[str, object] = {
        "schema": SCHEMA,
        "dataset_ready": dataset_ready,
        "input": {
            "path": str(input_path),
            "sha256": source_sha256,
            "bytes": input_path.stat().st_size,
            "trajectory_id": trajectory_id,
            "capture_source_id": capture_source_id,
            "binding_authority_contract": (
                "trajectory_id, capture_source_id, and main_action_surface_evidence "
                "must come from the authoritative replay job/binding or Live run"
            ),
            "expected_mode": expected_mode,
            "requested_stage_index": stage_index,
        },
        "scan": scan,
        "counts": {
            "source_transition_count": len(transitions),
            "candidate_stage_count": len(stages),
            "accepted_stage_count": accepted_stages if not global_blockers else 0,
            "accepted_transition_count": len(all_rows),
            "blocker_counts": dict(sorted(blocker_counts.items())),
        },
        "global_blockers": sorted(global_blockers),
        "stages": stage_reports,
        "output": {
            "transitions_path": str(transitions_path),
            "transitions_sha256": _sha256_file(transitions_path),
        },
        "contract": {
            "old_exact_false_rows_relabelled": False,
            "simulator_used": False,
            "policy_surface": "main-action-v1",
            "main_action_surface_complete": main_action_surface_complete,
            "main_action_surface_evidence": main_action_surface_evidence,
            "trajectory_id": trajectory_id,
            "capture_source_id": capture_source_id,
            "required_native_root_field_count": len(REQUIRED_STATE_FIELDS),
            "required_candidate_scope": "unified",
            "requires_turn1_to_terminal": True,
        },
    }
    report["report_sha256"] = _digest(report)
    _atomic_write(
        report_path,
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n",
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build exact RL transition rows from a fresh verified recorder"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--trajectory-id",
        required=True,
        help="authoritative replay trajectory or durable Live run ID",
    )
    parser.add_argument(
        "--capture-source-id",
        required=True,
        help="authoritative recorder capture/session ID",
    )
    parser.add_argument("--expected-mode", choices=("live", "replay"), required=True)
    parser.add_argument("--stage-index", type=int)
    parser.add_argument("--main-action-surface-complete", action="store_true")
    parser.add_argument(
        "--main-action-surface-evidence",
        help="binding/report evidence ID proving no unrecorded secondary action",
    )
    args = parser.parse_args(argv)
    report = build_runtime_exact_stage_dataset(
        args.input,
        args.output_dir,
        trajectory_id=args.trajectory_id,
        capture_source_id=args.capture_source_id,
        expected_mode=args.expected_mode,
        stage_index=args.stage_index,
        main_action_surface_complete=args.main_action_surface_complete,
        main_action_surface_evidence=args.main_action_surface_evidence,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["dataset_ready"] is True else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "REQUIRED_STATE_FIELDS",
    "SCHEMA",
    "build_runtime_exact_stage_dataset",
    "main",
    "split_runtime_exact_stages",
    "validate_runtime_exact_stage",
]
