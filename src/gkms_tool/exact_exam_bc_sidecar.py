"""Diagnostic-only Exact Exam BC evidence for completed formal actions.

The formal Exam loop remains the sole action owner.  This module consumes
already-recorded exact transitions after the game has settled, scores the
unchanged ordered legal set, and writes a comparison ledger.  It never returns
an action to an executor and every load/score/write failure is represented as
diagnostic evidence instead of escaping into cultivation control.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Queue
from threading import Condition, Lock, Thread
import time
from typing import Any, Final, Protocol

from .canonical_training_labels import (
    EFFECT_BY_NATIVE_VALUE,
    KNOWN_READINESS_V5_FLOWS,
    PLAN_BY_NATIVE_VALUE,
    STAGE_BY_NATIVE_VALUE,
)
from .exact_policy_contract import strict_exact_candidate_index
from .exact_exam_state_projection import (
    normalize_exact_exam_legal_candidates,
    project_exact_exam_native_state,
)
from .policy_bundle import PolicyBundle
from .training_artifact_io import canonical_json_bytes


EVIDENCE_SCHEMA: Final = "gkms.exact-exam-bc-diagnostic-evidence.v1"
BATCH_SCHEMA: Final = "gkms.exact-exam-bc-diagnostic-batch.v1"
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_LEDGER_PATH: Final = (
    PROJECT_ROOT / "var" / "telemetry" / "exact_exam_bc_diagnostic.jsonl"
)
_LEDGER_APPEND_LOCK = Lock()
_SUBMIT_QUEUE: Queue[tuple[str, object, str | None]] = Queue()
_SUBMIT_WORKER_LOCK = Lock()
_SUBMIT_WORKER: Thread | None = None
_RUN_COMPLETION = Condition(Lock())
_RUN_SUBMITTED_BATCHES: dict[str, int] = {}
_RUN_COMPLETED_BATCHES: dict[str, int] = {}
_RUN_WORKER_ERRORS: dict[str, int] = {}
_RUN_EXPECTED_TRANSITIONS: dict[str, int] = {}
_RUN_DIAGNOSTIC_SOURCE: dict[str, str] = {}
_RUN_NATIVE_STAGE_REPORTS: dict[str, list[dict[str, object]]] = {}


class ExactExamScoreBundle(Protocol):
    @property
    def bundle_id(self) -> str: ...

    def component(self, role: str) -> Mapping[str, Any]: ...

    def score_exam(
        self,
        *,
        state_before: Mapping[str, Any],
        flow: str,
        stage: str,
        legal_candidates: Sequence[object],
        allow_diagnostic: bool = False,
    ) -> tuple[float, ...]: ...


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


# Compatibility alias for old diagnostic fixtures; production callers use the
# public shared projection directly.
_native_state_projection = project_exact_exam_native_state


def _kind(value: Mapping[str, Any]) -> str:
    raw = str(value.get("kind", value.get("action_type", "")))
    return {
        "play": "use-hand",
        "card": "use-hand",
        "use_hand": "use-hand",
        "drink": "use-drink",
        "use_drink": "use-drink",
        "end_turn": "turn-end",
        "turn_end": "turn-end",
    }.get(raw, raw)


_normalise_legal_candidates = normalize_exact_exam_legal_candidates


def _normalise_formal_action(
    action: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, object]:
    value = dict(action)
    kind = _kind(value)
    value["kind"] = kind
    value["action_type"] = kind
    value["legal"] = True
    if kind == "use-hand":
        guid = value.get("card_guid", value.get("guid"))
        matches = [
            candidate
            for candidate in candidates
            if candidate.get("kind") == kind
            and candidate.get("card_guid") == guid
        ]
    elif kind == "use-drink":
        slot = value.get("slot_index", value.get("slot"))
        drink_id = value.get("drink_id")
        matches = [
            candidate
            for candidate in candidates
            if candidate.get("kind") == kind
            and candidate.get("slot_index") == slot
            and candidate.get("drink_id") == drink_id
        ]
    else:
        matches = [candidate for candidate in candidates if candidate.get("kind") == kind]
    if len(matches) != 1:
        raise ValueError("formal action has no unique member in the legal set")
    raw_slot = value.get("slot_index", value.get("slot"))
    if raw_slot is not None and raw_slot != matches[0].get("slot_index"):
        raise ValueError("formal action slot disagrees with the legal candidate")
    # Bind the scorer to the exact candidate identity without changing the
    # formal action stored by the recorder or its executor-facing object.
    return dict(matches[0])


def _scope(
    state_before: Mapping[str, Any], metadata: Mapping[str, Any]
) -> tuple[str, str, str]:
    produce_id = state_before.get("produceId")
    raw_plan = state_before.get("planType")
    raw_effect = state_before.get("mainEffectType")
    raw_stage = state_before.get("stepType")
    plan = (
        PLAN_BY_NATIVE_VALUE.get(raw_plan)
        if isinstance(raw_plan, int) and not isinstance(raw_plan, bool)
        else raw_plan
    )
    effect = (
        EFFECT_BY_NATIVE_VALUE.get(raw_effect)
        if isinstance(raw_effect, int) and not isinstance(raw_effect, bool)
        else raw_effect
    )
    stage = (
        STAGE_BY_NATIVE_VALUE.get(raw_stage)
        if isinstance(raw_stage, int) and not isinstance(raw_stage, bool)
        else raw_stage
    )
    if stage not in {"Mid1", "Mid2", "Final"}:
        step_context = metadata.get("step_context_id")
        if isinstance(step_context, str) and step_context.startswith("exam-step:"):
            try:
                stage = STAGE_BY_NATIVE_VALUE.get(int(step_context.rsplit(":", 1)[1]))
            except ValueError:
                stage = None
    if not all(isinstance(value, str) and value for value in (produce_id, plan, effect)):
        raise ValueError("Exact BC transition has incomplete flow identity")
    if stage not in {"Mid1", "Mid2", "Final"}:
        raise ValueError("Exact BC transition has no canonical stage")
    flow = f"{produce_id}|{plan}|{effect}"
    if flow not in KNOWN_READINESS_V5_FLOWS:
        raise ValueError(f"flow is outside the frozen Exact BC cohort: {flow}")
    return flow, str(stage), str(plan)


def _candidate_identity(value: Mapping[str, Any]) -> str:
    kind = _kind(value)
    if kind == "use-hand":
        return f"PLAY:{value.get('card_guid', value.get('guid'))}"
    if kind == "use-drink":
        return f"DRINK:{value.get('slot_index')}:{value.get('drink_id')}"
    return "END_TURN"


@dataclass(slots=True)
class ExactExamBCDiagnosticSidecar:
    """Score completed exact rows without becoming a decision provider."""

    bundle: ExactExamScoreBundle
    ledger_path: Path | None = DEFAULT_LEDGER_PATH

    @classmethod
    def load(
        cls,
        *,
        ledger_path: Path | None = DEFAULT_LEDGER_PATH,
    ) -> "ExactExamBCDiagnosticSidecar":
        return cls(PolicyBundle.load(), ledger_path)

    def score(
        self,
        transition: Mapping[str, Any],
        *,
        source_run_id: str | None = None,
    ) -> dict[str, object]:
        if source_run_id is not None and (
            not isinstance(source_run_id, str) or not source_run_id.strip()
        ):
            raise ValueError("source_run_id must be non-empty text or None")
        metadata = _mapping(transition.get("metadata"))
        if not (
            metadata.get("candidate_set_kind") == "unified"
            and metadata.get("full_rl_policy_ready") is True
        ):
            raise ValueError("transition has no complete unified legal set")
        raw_state = transition.get("state_before")
        raw_action = transition.get("action")
        raw_candidates = transition.get("legal_candidates")
        if not (
            isinstance(raw_state, Mapping)
            and isinstance(raw_action, Mapping)
            and isinstance(raw_candidates, (list, tuple))
            and raw_candidates
        ):
            raise ValueError("transition is missing state/action/legal candidates")

        state = _native_state_projection(raw_state)
        candidates = _normalise_legal_candidates(state, raw_candidates)
        formal = _normalise_formal_action(raw_action, candidates)
        formal_index = strict_exact_candidate_index(state, formal, candidates)
        flow, stage, plan = _scope(state, metadata)
        probabilities = self.bundle.score_exam(
            state_before=state,
            flow=flow,
            stage=stage,
            legal_candidates=candidates,
            allow_diagnostic=True,
        )
        if len(probabilities) != len(candidates):
            raise ValueError("Exact BC scorer changed the candidate count")
        ranked = sorted(
            range(len(candidates)),
            key=lambda index: (-probabilities[index], index),
        )
        top_index = ranked[0]
        component = self.bundle.component("exact_exam_policy")
        evidence_before_digest = metadata.get("evidence_before_digest")
        record_identity = {
            "bundle_id": self.bundle.bundle_id,
            "run_id": source_run_id,
            "flow": flow,
            "stage": stage,
            "before": evidence_before_digest,
            "formal_index": formal_index,
        }
        result: dict[str, object] = {
            "schema": EVIDENCE_SCHEMA,
            "record_id": _sha256(record_identity),
            "recorded_at": _utc_now(),
            "status": "scored",
            "record_mode": "offline-diagnostic",
            "diagnostic_only": True,
            "applied": False,
            "live_apply_allowed": False,
            "formal_control_unchanged": True,
            "candidate_order_unchanged": True,
            "executor_unchanged": True,
            "fallback_unchanged": True,
            "bundle_id": self.bundle.bundle_id,
            "component_quality": component.get("quality"),
            "component_declared_shadow_ready": component.get("shadow_ready") is True,
            "model_sha256": component.get("model_sha256"),
            "report_sha256": component.get("report_sha256"),
            "scope": {"flow": flow, "stage": stage, "plan_type": plan},
            "source": {
                "run_id": source_run_id,
                "evidence_before_digest": evidence_before_digest,
                "evidence_after_digest": metadata.get("evidence_after_digest"),
                "source_before_sha256": metadata.get("source_before_sha256"),
                "source_after_sha256": metadata.get("source_after_sha256"),
                "step_context_id": metadata.get("step_context_id"),
                "session_transition_id": metadata.get("session_transition_id"),
                "candidate_authority": metadata.get("candidate_authority"),
                "state_projection": (
                    "native-runtime-state-v1"
                    if "handList" in raw_state
                    else "typed-localsave-to-exact-bc-input-v1"
                ),
            },
            "formal": {
                "candidate_index": formal_index,
                "action_id": _candidate_identity(formal),
                "probability": probabilities[formal_index],
            },
            "model": {
                "top_candidate_index": top_index,
                "top_action_id": _candidate_identity(candidates[top_index]),
                "top_probability": probabilities[top_index],
                "agrees_with_formal": top_index == formal_index,
            },
            "candidate_probabilities": [
                {
                    "candidate_index": index,
                    "action_id": _candidate_identity(candidate),
                    "probability": probabilities[index],
                }
                for index, candidate in enumerate(candidates)
            ],
        }
        return result

    def persist(self, evidence: Mapping[str, Any]) -> None:
        if self.ledger_path is None:
            return
        path = Path(self.ledger_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        with _LEDGER_APPEND_LOCK:
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)


def _blocked_evidence(
    transition: object,
    *,
    transition_index: int,
    source_run_id: str | None,
    reason: str,
    sidecar: ExactExamBCDiagnosticSidecar | None,
) -> dict[str, object]:
    metadata = (
        _mapping(transition.get("metadata"))
        if isinstance(transition, Mapping)
        else {}
    )
    scope: dict[str, object] = {}
    if isinstance(transition, Mapping):
        state = transition.get("state_before")
        if isinstance(state, Mapping):
            try:
                flow, stage, plan = _scope(_native_state_projection(state), metadata)
                scope = {"flow": flow, "stage": stage, "plan_type": plan}
            except Exception:
                pass
    bundle_id: str | None = None
    component: Mapping[str, Any] = {}
    if sidecar is not None:
        try:
            bundle_id = sidecar.bundle.bundle_id
            component = sidecar.bundle.component("exact_exam_policy")
        except Exception:
            pass
    identity = {
        "run_id": source_run_id,
        "transition_index": transition_index,
        "before": metadata.get("evidence_before_digest"),
        "step_context_id": metadata.get("step_context_id"),
        "reason": reason,
    }
    return {
        "schema": EVIDENCE_SCHEMA,
        "record_id": _sha256(identity),
        "recorded_at": _utc_now(),
        "status": "blocked",
        "record_mode": "offline-diagnostic",
        "diagnostic_only": True,
        "applied": False,
        "live_apply_allowed": False,
        "formal_control_unchanged": True,
        "candidate_order_unchanged": True,
        "executor_unchanged": True,
        "fallback_unchanged": True,
        "bundle_id": bundle_id,
        "component_quality": component.get("quality"),
        "component_declared_shadow_ready": component.get("shadow_ready") is True,
        "model_sha256": component.get("model_sha256"),
        "report_sha256": component.get("report_sha256"),
        "scope": scope,
        "source": {
            "run_id": source_run_id,
            "evidence_before_digest": metadata.get("evidence_before_digest"),
            "evidence_after_digest": metadata.get("evidence_after_digest"),
            "source_before_sha256": metadata.get("source_before_sha256"),
            "source_after_sha256": metadata.get("source_after_sha256"),
            "step_context_id": metadata.get("step_context_id"),
            "session_transition_id": metadata.get("session_transition_id"),
            "candidate_authority": metadata.get("candidate_authority"),
        },
        "transition_index": transition_index,
        "reason": reason,
    }


def _persist_blocked_evidence(
    evidence: Mapping[str, Any],
    *,
    sidecar: ExactExamBCDiagnosticSidecar | None,
    ledger_path: Path | None,
) -> None:
    if sidecar is not None:
        sidecar.persist(evidence)
        return
    if ledger_path is None:
        return
    # Persistence does not consult the scorer.  Keep bundle-load failures
    # durable without constructing a fake decision provider.
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    with _LEDGER_APPEND_LOCK:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)


def evaluate_exact_exam_bc_diagnostics(
    transitions: Sequence[object],
    *,
    sidecar: ExactExamBCDiagnosticSidecar | None = None,
    ledger_path: Path | None = DEFAULT_LEDGER_PATH,
    source_run_id: str | None = None,
) -> dict[str, object]:
    """Score a completed transition batch; never raise into formal control."""

    rows = tuple(transitions)
    records: list[dict[str, object]] = []
    load_error: str | None = None
    if sidecar is None:
        try:
            sidecar = ExactExamBCDiagnosticSidecar.load(ledger_path=ledger_path)
        except Exception as error:
            load_error = f"{type(error).__name__}: {error}"

    for index, raw in enumerate(rows):
        if sidecar is None:
            evidence = _blocked_evidence(
                raw,
                transition_index=index,
                source_run_id=source_run_id,
                reason=f"bundle-unavailable:{load_error}",
                sidecar=None,
            )
            try:
                _persist_blocked_evidence(
                    evidence, sidecar=None, ledger_path=ledger_path
                )
            except Exception as error:
                evidence["ledger_write_error"] = f"{type(error).__name__}: {error}"
            records.append(evidence)
            continue
        try:
            if not isinstance(raw, Mapping):
                raise TypeError("transition is not an object")
            evidence = sidecar.score(raw, source_run_id=source_run_id)
            evidence["transition_index"] = index
            try:
                sidecar.persist(evidence)
            except Exception as error:
                evidence["ledger_write_error"] = f"{type(error).__name__}: {error}"
            records.append(evidence)
        except Exception as error:
            evidence = _blocked_evidence(
                raw,
                transition_index=index,
                source_run_id=source_run_id,
                reason=f"{type(error).__name__}: {error}",
                sidecar=sidecar,
            )
            try:
                _persist_blocked_evidence(
                    evidence, sidecar=sidecar, ledger_path=ledger_path
                )
            except Exception as persist_error:
                evidence["ledger_write_error"] = (
                    f"{type(persist_error).__name__}: {persist_error}"
                )
            records.append(evidence)

    scored = sum(value.get("status") == "scored" for value in records)
    blocked = len(records) - scored
    return {
        "schema": BATCH_SCHEMA,
        "status": "scored" if scored else "blocked" if records else "empty",
        "record_mode": "offline-diagnostic",
        "diagnostic_only": True,
        "applied": False,
        "live_apply_allowed": False,
        "formal_control_unchanged": True,
        "candidate_order_unchanged": True,
        "executor_unchanged": True,
        "fallback_unchanged": True,
        "transition_count": len(rows),
        "scored_count": scored,
        "blocked_count": blocked,
        "bundle_load_error": load_error,
        "ledger_path": (
            None if ledger_path is None else str(Path(ledger_path).resolve())
        ),
        "records": records,
    }


def _diagnostic_submit_worker() -> None:
    while True:
        job_kind, payload, source_run_id = _SUBMIT_QUEUE.get()
        worker_error = False
        expected_transitions = 0
        native_stage_report: dict[str, object] | None = None
        try:
            if job_kind == "native-runtime-stage":
                from .native_runtime_stage_capture import (
                    build_native_runtime_stage_rows,
                )

                if source_run_id is None:
                    raise ValueError("native runtime stage requires source_run_id")
                transitions, native_stage_report = build_native_runtime_stage_rows(
                    payload,  # type: ignore[arg-type]
                    source_run_id=source_run_id,
                )
                expected_transitions = len(transitions)
            elif job_kind == "transition-rows":
                transitions = tuple(payload)  # type: ignore[arg-type]
            else:
                raise ValueError(f"unknown Exact BC diagnostic job: {job_kind}")
            evaluate_exact_exam_bc_diagnostics(
                transitions,
                source_run_id=source_run_id,
            )
        except Exception:
            # This worker is observational only. Its failures must never
            # escape into cultivation or terminate the one background spine.
            worker_error = True
        finally:
            if source_run_id is not None:
                with _RUN_COMPLETION:
                    if expected_transitions:
                        _RUN_EXPECTED_TRANSITIONS[source_run_id] = (
                            _RUN_EXPECTED_TRANSITIONS.get(source_run_id, 0)
                            + expected_transitions
                        )
                    if native_stage_report is not None:
                        _RUN_NATIVE_STAGE_REPORTS.setdefault(source_run_id, []).append(
                            dict(native_stage_report)
                        )
                    _RUN_COMPLETED_BATCHES[source_run_id] = (
                        _RUN_COMPLETED_BATCHES.get(source_run_id, 0) + 1
                    )
                    if worker_error:
                        _RUN_WORKER_ERRORS[source_run_id] = (
                            _RUN_WORKER_ERRORS.get(source_run_id, 0) + 1
                        )
                    _RUN_COMPLETION.notify_all()
            _SUBMIT_QUEUE.task_done()


def submit_exact_exam_bc_diagnostics(
    transitions: Sequence[object],
    *,
    source_run_id: str | None = None,
) -> bool:
    """Queue completed exact rows without loading/scoring on the caller.

    One daemon thread owns every diagnostic batch. The caller performs only a
    shallow immutable hand-off and can return the formal Maa result
    immediately; bundle loading, scoring, and ledger writes occur in the
    background and can never become cultivation control.
    """

    if source_run_id is not None and (
        not isinstance(source_run_id, str) or not source_run_id.strip()
    ):
        return False
    rows = tuple(transitions)
    if not rows:
        return False
    queued_run_id = (
        source_run_id.strip() if isinstance(source_run_id, str) else None
    )
    global _SUBMIT_WORKER
    with _SUBMIT_WORKER_LOCK:
        if _SUBMIT_WORKER is None or not _SUBMIT_WORKER.is_alive():
            _SUBMIT_WORKER = Thread(
                target=_diagnostic_submit_worker,
                name="exact-exam-bc-sidecar",
                daemon=True,
            )
            _SUBMIT_WORKER.start()
        if queued_run_id is not None:
            run_id = queued_run_id
            with _RUN_COMPLETION:
                existing_source = _RUN_DIAGNOSTIC_SOURCE.get(run_id)
                if existing_source not in (None, "maa-exact-transition"):
                    return False
                _RUN_DIAGNOSTIC_SOURCE[run_id] = "maa-exact-transition"
                _RUN_SUBMITTED_BATCHES[run_id] = (
                    _RUN_SUBMITTED_BATCHES.get(run_id, 0) + 1
                )
        _SUBMIT_QUEUE.put(("transition-rows", rows, queued_run_id))
    return True


def submit_native_runtime_exact_exam_bc_diagnostics(
    capture: Mapping[str, object],
    *,
    source_run_id: str,
) -> bool:
    """Queue one frozen-DLL Live stage after Maa has formally completed it.

    Parsing, native continuity validation, scoring, and ledger I/O all remain
    on the existing daemon.  A malformed/partial capture therefore blocks only
    shadow evidence and cannot delay or alter the completed Maa receipt.
    """

    if not isinstance(source_run_id, str) or not source_run_id.strip():
        return False
    try:
        from .native_runtime_stage_capture import NativeRuntimeStageCapture

        value = NativeRuntimeStageCapture.from_mapping(capture)
    except (OSError, TypeError, ValueError):
        return False
    if not value.complete:
        return False
    run_id = source_run_id.strip()
    global _SUBMIT_WORKER
    with _SUBMIT_WORKER_LOCK:
        if _SUBMIT_WORKER is None or not _SUBMIT_WORKER.is_alive():
            _SUBMIT_WORKER = Thread(
                target=_diagnostic_submit_worker,
                name="exact-exam-bc-sidecar",
                daemon=True,
            )
            _SUBMIT_WORKER.start()
        with _RUN_COMPLETION:
            existing_source = _RUN_DIAGNOSTIC_SOURCE.get(run_id)
            if existing_source not in (None, "native-runtime-stage"):
                return False
            _RUN_DIAGNOSTIC_SOURCE[run_id] = "native-runtime-stage"
            _RUN_SUBMITTED_BATCHES[run_id] = (
                _RUN_SUBMITTED_BATCHES.get(run_id, 0) + 1
            )
        _SUBMIT_QUEUE.put(("native-runtime-stage", value.to_dict(), run_id))
    return True


def flush_exact_exam_bc_diagnostics(
    source_run_id: str,
    *,
    timeout_seconds: float = 2.0,
) -> dict[str, object]:
    """Wait only for batches already submitted for one formal run.

    This is a run-end evidence fence, never an action-path barrier.  A timeout
    or worker failure is returned as diagnostic state and is never raised into
    cultivation control.
    """

    if not isinstance(source_run_id, str) or not source_run_id.strip():
        return {
            "status": "invalid-run-id",
            "complete": False,
            "run_id": source_run_id,
            "submitted_batch_count": 0,
            "completed_batch_count": 0,
            "pending_batch_count": 0,
            "worker_error_count": 0,
        }
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, (int, float)
    ) or timeout_seconds < 0:
        return {
            "status": "invalid-timeout",
            "complete": False,
            "run_id": source_run_id,
            "submitted_batch_count": 0,
            "completed_batch_count": 0,
            "pending_batch_count": 0,
            "worker_error_count": 0,
        }
    run_id = source_run_id.strip()
    deadline = time.monotonic() + float(timeout_seconds)
    with _RUN_COMPLETION:
        target = _RUN_SUBMITTED_BATCHES.get(run_id, 0)
        while _RUN_COMPLETED_BATCHES.get(run_id, 0) < target:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _RUN_COMPLETION.wait(remaining)
        completed = min(_RUN_COMPLETED_BATCHES.get(run_id, 0), target)
        pending = max(0, target - completed)
        errors = _RUN_WORKER_ERRORS.get(run_id, 0)
    return {
        "status": (
            "no-submissions" if target == 0 else "complete" if pending == 0 else "timeout"
        ),
        "complete": pending == 0,
        "run_id": run_id,
        "submitted_batch_count": target,
        "completed_batch_count": completed,
        "pending_batch_count": pending,
        "worker_error_count": errors,
        "diagnostic_source": _RUN_DIAGNOSTIC_SOURCE.get(run_id),
        "expected_transition_count": _RUN_EXPECTED_TRANSITIONS.get(run_id),
        "native_stage_reports": [
            dict(value) for value in _RUN_NATIVE_STAGE_REPORTS.get(run_id, ())
        ],
    }


__all__ = [
    "BATCH_SCHEMA",
    "DEFAULT_LEDGER_PATH",
    "EVIDENCE_SCHEMA",
    "ExactExamBCDiagnosticSidecar",
    "evaluate_exact_exam_bc_diagnostics",
    "flush_exact_exam_bc_diagnostics",
    "submit_native_runtime_exact_exam_bc_diagnostics",
    "submit_exact_exam_bc_diagnostics",
]
