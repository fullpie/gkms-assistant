"""DLL Exam actions with native receipts and no screen-dependent gates.

The gateway owns observation, submission and settlement as one transaction.
It shares the existing controller mutex, so a legacy Maa request cannot run
between a DLL action and its completion. An unresolved action is persisted
and reconciled before any subsequent input, including after a GUI restart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION, EXAM_SAVE_DATA_SOURCE_TYPE,
    AuditionLocalSaveStateEvidence, parse_local_save_exam_state,
)
from .runtime_live_recorder import require_live_action_recorder
from .runtime_action_settlement_watcher import RuntimeActionSettlementWatcher
from .runtime_action_state_evidence import (
    RuntimeActionStateProvenance, adapt_runtime_action_state_evidence,
)
from .runtime_command_client import (
    RuntimeCommandClient, RuntimeCommandError, RuntimeCommandPending,
    RuntimeCommandProtocolError, RuntimeCommandRequest, RuntimeCommandResult,
)
from .training_artifact_io import atomic_write, canonical_json_bytes as json_bytes
from .gui_setup.native_observation import observe_native_return


@dataclass(frozen=True, slots=True)
class RuntimeExamOutcome:
    status: str
    detail: str
    evidence: AuditionLocalSaveStateEvidence | None = None
    provenance: RuntimeActionStateProvenance | None = None
    request_id: str | None = None
    submitted: bool = False
    raw_state: Mapping[str, Any] | None = None
    # Scene identity supplied beside ExamSaveData by the same native snapshot.
    # A recorder transition preserves its parent snapshot's identity context;
    # it does not add fields to the hashed raw ExamSaveData.
    native_context: Mapping[str, Any] | None = None
    # Same read_snapshot response, outside the original ExamSaveData hash.
    # In particular native_legal_inputs is an observation of game validators,
    # not a simulator/collector completion flag.
    native_snapshot: Mapping[str, Any] | None = None
    native_session_generation: str | None = None


def _controller_lease(timeout: float) -> AbstractContextManager:
    from .controller_client import _serialized_controller_request
    return _serialized_controller_request(timeout)


def _sequence_key(value: object) -> str:
    # Recorder and bridge format the same sequence pointer in decimal/hex.
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeCommandProtocolError("native sequence identity is absent")
    text = str(value)
    if not text:
        raise RuntimeCommandProtocolError("native sequence identity is empty")
    try:
        return str(int(text, 16 if text.lower().startswith("0x") else 10))
    except ValueError:
        return text


def _secondary_reference_read_was_unstable(snapshot: Mapping[str, Any]) -> bool:
    """Recognize a retryable read, never qualify its state for a decision."""
    dto = snapshot.get("exam_model_observation")
    if (not isinstance(dto, Mapping) or dto.get("schema") != "gkms.live-exam-model-observation.v1"
            or dto.get("decision_type") != "secondary" or dto.get("complete") is not False):
        return False
    errors = dto.get("read_errors")
    allowed = {"before-reference-presence-incomplete", "after-reference-presence-incomplete",
               "source-reference-presence-changed-during-observation"}
    if (not isinstance(errors, list) or not errors or any(not isinstance(x, str) for x in errors)
            or not set(errors) <= allowed or "source-reference-presence-changed-during-observation" not in errors):
        return False
    purity = dto.get("purity")
    if (not isinstance(purity, Mapping) or purity.get("reference_presence_stable") is not False
            or any(purity.get(key) is not True for key in
                   ("state_equal", "owner_stable", "execution_master_stable", "live_counter_stable"))
            or not purity.get("before_native_sha256")
            or purity.get("before_native_sha256") != purity.get("after_native_sha256")
            or not isinstance(dto.get("state_before"), Mapping) or dto.get("state_before") != dto.get("state_after")):
        return False
    failed = False
    for name, label in (("reference_presence", "before-reference-presence-incomplete"),
                        ("reference_presence_after", "after-reference-presence-incomplete")):
        proof = dto.get(name)
        if not isinstance(proof, Mapping):
            return False
        if proof.get("complete") is True:
            if proof.get("read_errors") != [] or proof.get("parameter_source_verified") is not True or label in errors:
                return False
        elif (proof.get("complete") is False and proof.get("parameter_source_verified") is False
              and proof.get("read_errors") == ["parameter reference pointers changed during SaveData construction/JsonUtility"]
              and label in errors):
            failed = True
        else:
            return False
        for side in ("card_phase_counters_before", "card_phase_counters_after"):
            phases = proof.get(side)
            if not isinstance(phases, Mapping) or phases.get("complete") is not True or phases.get("read_errors") != []:
                return False
    for name in ("command_presence", "command_presence_after"):
        command = dto.get(name)
        if not isinstance(command, Mapping) or command.get("complete") is not True or command.get("read_errors") != []:
            return False
    return failed


def native_step_context_digest(snapshot: Mapping[str, Any], session_generation: str) -> str:
    """One identity formula shared by native reads and pure policy bindings."""
    raw = snapshot.get("exam_save")
    if not isinstance(raw, Mapping) or not isinstance(session_generation, str) or not session_generation:
        raise RuntimeCommandProtocolError("native snapshot identity is absent")
    identity = json_bytes({"session_generation": session_generation,
        "sequence_id": _sequence_key(snapshot.get("sequence_id")), "setting_id": raw.get("settingId"),
        "character_id": raw.get("characterId"), "step_type": raw.get("stepType")})
    return hashlib.sha256(identity).hexdigest()


def _snapshot_evidence(
    result: RuntimeCommandResult, base: AuditionLocalSaveStateEvidence | None,
    *, run_id: str | None = None,
    archive_root: Path | None = None,
) -> tuple[AuditionLocalSaveStateEvidence, RuntimeActionStateProvenance]:
    snapshot = result.snapshot
    if result.status != "ok" or snapshot is None:
        raise RuntimeCommandError(str(result.raw.get("error_code", "native snapshot unavailable")))
    if snapshot.get("busy") is not False or snapshot.get("queue_empty") is not True:
        raise RuntimeCommandError("native action is still executing")
    if snapshot.get("phase") != 6 and snapshot.get("terminal") is not True:
        raise RuntimeCommandError("native phase is not Main or terminal")
    raw = snapshot.get("exam_save")
    if not isinstance(raw, Mapping):
        raise RuntimeCommandProtocolError("native snapshot has no complete ExamSaveData")
    if base is not None and base.session_transition_id.startswith("native-sequence:"):
        expected_sequence = base.session_transition_id.removeprefix("native-sequence:")
        if expected_sequence != _sequence_key(snapshot.get("sequence_id")):
            raise RuntimeCommandError("native Exam sequence changed; create a new stage context")
        if base.step_context_digest != native_step_context_digest(snapshot, result.request.session_generation):
            raise RuntimeCommandError("native session or stage identity changed; create a new stage context")
    if base is None:
        state = parse_local_save_exam_state(raw)
        canonical = json_bytes(raw)
        digest = hashlib.sha256(canonical).hexdigest()
        sequence = _sequence_key(snapshot.get("sequence_id"))
        # Native payloads have no encrypted LocalSave envelope. Preserve an
        # immutable actual JSON file and mark envelope_version=0 explicitly.
        path = (archive_root or result.path.parent.parent) / "exam_snapshots" / f"{digest}.json"
        if not path.exists():
            atomic_write(path, canonical)
        zone_digest = hashlib.sha256(json_bytes(state.zones.to_dict())).hexdigest()
        base = AuditionLocalSaveStateEvidence(
            schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
            run_id=run_id or f"native-session:{result.request.session_generation}",
            step_context_id=f"exam-step:{state.step_type_value}",
            step_context_digest=native_step_context_digest(snapshot, result.request.session_generation),
            session_transition_id=f"native-sequence:{sequence}",
            zone_checkpoint_digest=zone_digest, source_path=str(path),
            source_sha256=digest, source_size=len(canonical), source_type=EXAM_SAVE_DATA_SOURCE_TYPE,
            envelope_version=0, state=state,
        )
    evidence, provenance = adapt_runtime_action_state_evidence(
        base, raw, telemetry_path=result.path,
        telemetry_end_offset=0, capture_kind="runtime-command-snapshot",
    )
    return _archive_native_state(archive_root or result.path.parent.parent, evidence, raw), provenance


def _archive_native_state(root: Path, evidence: AuditionLocalSaveStateEvidence, raw: Mapping[str, Any]):
    path = root / "exam_snapshots" / f"{evidence.source_sha256}.json"
    if not path.exists():
        atomic_write(path, json_bytes(raw))
    return replace(evidence, source_path=str(path))


class RuntimeExamGateway:
    def __init__(
        self, client: RuntimeCommandClient | None = None, *, timeout: float = 30.0,
        cancelled: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        lease: Callable[[float], AbstractContextManager] = _controller_lease,
        watcher_factory: Callable[..., Any] = RuntimeActionSettlementWatcher,
    ) -> None:
        self.client = client or RuntimeCommandClient()
        self.timeout = timeout
        self.cancelled = cancelled
        self.monotonic = monotonic
        self.sleep = sleep
        self.lease = lease
        self.watcher_factory = watcher_factory
        self.model_policy = None

    def bind_model_policy(self, policy):
        binding = getattr(policy, 'runtime_policy_binding', None)
        if (not isinstance(binding, Mapping) or binding.get('loaded') is not True
                or binding.get('variant_id') not in ('baseline','integrated')
                or not callable(getattr(policy, 'choose_secondary', None))):
            raise RuntimeCommandError('A loaded explicit two-model policy is required')
        if self.model_policy is not None and json_bytes(self.model_policy.runtime_policy_binding) != json_bytes(binding):
            raise RuntimeCommandError('The current exam model cannot change within its gateway')
        self.model_policy = policy

    def _validate_pending_model(self, pending):
        expected = pending.get('model_policy_binding')
        actual = None if self.model_policy is None else self.model_policy.runtime_policy_binding
        if json_bytes(expected) != json_bytes(actual):
            raise RuntimeCommandError('Pending native input belongs to a different model; preserve and resume its original policy')

    @property
    def pending_path(self) -> Path:
        return self.client.root / "pending_exam.json"

    def read_current(self, base: AuditionLocalSaveStateEvidence) -> AuditionLocalSaveStateEvidence:
        result = self.read_native(base)
        assert result.evidence is not None
        return result.evidence

    @observe_native_return(require_settled=False)
    def read_native(self, base: AuditionLocalSaveStateEvidence | None = None, *, run_id: str | None = None) -> RuntimeExamOutcome:
        """Return the typed state and its original native serializer payload."""
        with self.lease(self.timeout):
            if self.pending_path.exists():
                pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
                self._validate_pending_model(pending)
                outcome = self._settle(pending)
                if outcome.status != "settled" or outcome.evidence is None:
                    raise RuntimeCommandError(outcome.detail)
                return outcome
            deadline = self.monotonic() + self.timeout
            while True:
                if self.cancelled is not None and self.cancelled():
                    raise RuntimeCommandError("cancelled while waiting for the game decision phase")
                result = self.client.execute("read_snapshot", timeout=min(5.0, max(0.0, deadline - self.monotonic())))
                result.require_ok()
                snapshot = result.snapshot
                if snapshot is None:
                    raise RuntimeCommandProtocolError("native Exam snapshot is absent")
                ready = (snapshot.get("busy") is False and snapshot.get("queue_empty") is True
                         and (snapshot.get("phase") == 6 or snapshot.get("terminal") is True))
                if ready:
                    evidence, provenance = _snapshot_evidence(result, base, run_id=run_id, archive_root=self.client.root)
                    return RuntimeExamOutcome("observed", "current native Exam state", evidence,
                                              provenance, raw_state=snapshot["exam_save"],
                                              native_context=snapshot.get("produce_context"), native_snapshot=snapshot,
                                              native_session_generation=result.request.session_generation)
                if self.monotonic() >= deadline:
                    raise RuntimeCommandError("game did not reach its native decision phase")
                # One game-owned readiness condition, not two matching frames
                # or a second LocalSave/visual completion authority.
                self.sleep(min(.1, max(0.0, deadline - self.monotonic())))

    @observe_native_return(require_settled=True)
    def execute(
        self, kind: str, before: AuditionLocalSaveStateEvidence, *,
        card_guid: str | None = None, slot: int | None = None,
        drink_id: str | None = None,
        preferred_selection_guid: str | None = None,
        secondary_policy: str | None = None,
    ) -> RuntimeExamOutcome:
        if secondary_policy not in (None, "native-card-choice-value-v1", "gui-exam-bc-v1"):
            raise RuntimeCommandError("unsupported native secondary selection policy")
        if secondary_policy == 'gui-exam-bc-v1' and self.model_policy is None:
            raise RuntimeCommandError('The selected model does not own secondary decisions')
        with self.lease(self.timeout):
            if self.cancelled is not None and self.cancelled() and not self.pending_path.exists():
                return RuntimeExamOutcome("rejected", "cancelled before native input")
            if self.pending_path.exists():
                # Resolve the old operation even if the caller has already
                # planned a different action. It must replan on its outcome.
                pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
                self._validate_pending_model(pending)
                result = self._settle(pending)
                if result.status == "settled":
                    return RuntimeExamOutcome("replan", "previous DLL action reconciled",
                                              result.evidence, result.provenance, result.request_id,
                                              raw_state=result.raw_state, native_context=result.native_context,
                                              native_snapshot=result.native_snapshot,
                                              native_session_generation=result.native_session_generation)
                return result
            try:
                snapshot_result = self.client.execute("read_snapshot")
                current, provenance = _snapshot_evidence(snapshot_result, before, archive_root=self.client.root)
            except RuntimeCommandError as exc:
                return RuntimeExamOutcome("rejected", str(exc))
            if current.state != before.state:
                return RuntimeExamOutcome("replan", "DLL state advanced before input", current, provenance,
                                          raw_state=snapshot_result.snapshot["exam_save"],
                                          native_context=snapshot_result.snapshot.get("produce_context"),
                                          native_snapshot=snapshot_result.snapshot,
                                          native_session_generation=snapshot_result.request.session_generation)
            raw_snapshot = snapshot_result.snapshot
            assert raw_snapshot is not None
            source_execution_master = None
            if secondary_policy == "gui-exam-bc-v1":
                dto = raw_snapshot.get("exam_model_observation")
                master = dto.get("execution_master") if isinstance(dto, Mapping) else None
                if (not isinstance(dto, Mapping) or dto.get("schema") != "gkms.live-exam-model-observation.v1"
                        or dto.get("decision_type") != "main" or dto.get("complete") is not True
                        or dto.get("read_errors") != [] or not isinstance(master, Mapping)):
                    return RuntimeExamOutcome("rejected", "Fresh native model observation has no complete actual Master binding")
                validator = getattr(self.model_policy, "validate_source_execution_master", None)
                if not callable(validator):
                    return RuntimeExamOutcome("rejected", "Loaded model cannot validate the actual source Master")
                try:
                    validator(master)
                except (OSError, ValueError, TypeError, KeyError) as error:
                    return RuntimeExamOutcome("rejected", "Model source Master changed before input: " + str(error))
                source_execution_master = deepcopy(dict(master))
            target: dict[str, object] = {}
            if kind == "play":
                matching = [i for i, card in enumerate(current.state.zones.hand) if card.guid == card_guid]
                if len(matching) != 1:
                    return RuntimeExamOutcome("rejected", "selected card GUID is not unique in native Hand")
                slot = matching[0]
                target = {"slot": slot, "card_guid": card_guid}
            elif kind == "drink":
                target = {"slot": slot, "drink_id": drink_id}
            elif kind != "end_turn":
                return RuntimeExamOutcome("rejected", "unsupported native action kind")
            status = self.client.read_status()
            if status["session_generation"] != snapshot_result.request.session_generation:
                return RuntimeExamOutcome("rejected", "DLL generation changed after observation")
            if secondary_policy is not None and "exam.continuation" not in status.get("capabilities", ()):
                return RuntimeExamOutcome("rejected", "native secondary selection capability is unavailable")
            try:
                recorder_path = require_live_action_recorder(status)
            except RuntimeCommandError as exc:
                return RuntimeExamOutcome("rejected", str(exc))
            request = self.client.make_request(
                f"exam.{kind}", expected_revision=raw_snapshot.get("revision"), target=target,
            )
            if request.session_generation != snapshot_result.request.session_generation:
                return RuntimeExamOutcome("rejected", "DLL generation changed before action binding")
            pending = {
                "schema": "gkms.runtime-pending-exam.v1", "request": request.to_dict(),
                "before": before.to_dict(), "sequence_id": raw_snapshot.get("sequence_id"),
                "recorder_path": str(recorder_path), "start_offset": recorder_path.stat().st_size,
                "kind": kind, "slot": slot if kind != "end_turn" else 0,
                "card_guid": card_guid, "drink_id": drink_id,
                "preferred_selection_guid": preferred_selection_guid,
                "secondary_policy": secondary_policy,
                **({'model_policy_binding': dict(self.model_policy.runtime_policy_binding)}
                   if self.model_policy is not None else {}),
                **({'source_execution_master': source_execution_master}
                   if source_execution_master is not None else {}),
                "source_card_id": current.state.zones.hand[slot].card_id if kind == "play" else None,
                "source_card_upgrade": current.state.zones.hand[slot].effective_upgrade if kind == "play" else None,
                "native_context": raw_snapshot.get("produce_context"),
            }
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.pending_path, json_bytes(pending))
            if self.cancelled is not None and self.cancelled():
                self._retire(pending, "cancelled-before-submit")
                return RuntimeExamOutcome("rejected", "cancelled before native input")
            try:
                self.client.submit(request)
            except RuntimeCommandPending:
                # The descriptor/running queue decides whether it was sent.
                # It is deliberately left in place for reconciliation.
                return RuntimeExamOutcome("pending", "DLL publication outcome unknown",
                                          request_id=request.request_id)
            except RuntimeCommandError as exc:
                self._retire(pending, "rejected")
                return RuntimeExamOutcome("rejected", str(exc), request_id=request.request_id)
            return self._settle(pending)

    def _retire(self, pending: Mapping[str, Any], outcome: str) -> None:
        request_id = pending["request"]["request_id"]
        history = self.client.root / "completed_exam" / f"{request_id}.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(history, json_bytes({**pending, "outcome": outcome}))
        raw = pending["request"]
        release = getattr(self.client, "release_action", None)
        if callable(release):
            release(RuntimeCommandRequest(raw["request_id"], raw["session_generation"], raw["command"],
                                          raw.get("expected_revision"), raw.get("target")))
        self.pending_path.unlink(missing_ok=True)

    def _settle(self, pending: Mapping[str, Any]) -> RuntimeExamOutcome:
        if pending.get("schema") != "gkms.runtime-pending-exam.v1":
            raise RuntimeCommandProtocolError("pending Exam schema is invalid")
        raw_request = pending["request"]
        request = RuntimeCommandRequest(
            raw_request["request_id"], raw_request["session_generation"], raw_request["command"],
            raw_request.get("expected_revision"), raw_request.get("target"),
        )
        if self.client.read_status()["session_generation"] != request.session_generation:
            return RuntimeExamOutcome("pending", "previous game session requires explicit reconciliation",
                                      request_id=request.request_id)
        before = AuditionLocalSaveStateEvidence.from_dict(pending["before"])
        watcher = self.watcher_factory(
            pending["recorder_path"], start_offset=pending["start_offset"],
            expected_action_kind=pending["kind"], expected_slot=pending["slot"],
            expected_card_guid=pending.get("card_guid"), expected_drink_id=pending.get("drink_id"),
        )
        deadline = self.monotonic() + self.timeout
        submitted = False
        settlement = None
        next_snapshot_at = 0.0
        next_selection_at = 0.0
        while True:
            result = self.client.poll_result(request)
            if result is not None:
                if result.status == "rejected":
                    self._retire(pending, "rejected")
                    detail = str(result.raw.get("error_message", result.raw.get("error_code", "DLL rejected action")))
                    if detail == "stale native state revision":
                        return RuntimeExamOutcome("replan", detail, request_id=request.request_id)
                    return RuntimeExamOutcome("rejected", detail,
                                              request_id=request.request_id)
                submitted = submitted or result.submitted
            receipt = watcher.poll()
            if receipt is not None:
                if receipt.runtime_sequence is None or (
                    _sequence_key(receipt.runtime_sequence) != _sequence_key(pending["sequence_id"])
                ):
                    return RuntimeExamOutcome("pending", "native receipt sequence does not match action",
                                              request_id=request.request_id, submitted=submitted)
                settlement = receipt
            if submitted and settlement is not None:
                receipt = settlement
                if receipt.state_after is not None:
                    after, provenance = adapt_runtime_action_state_evidence(
                        before, json.loads(receipt.state_after), telemetry_path=receipt.path,
                        telemetry_end_offset=receipt.end_offset,
                    )
                    after = _archive_native_state(self.client.root, after, json.loads(receipt.state_after))
                    if after.state.is_native_actionable_settled or receipt.terminal:
                        self._retire(pending, "settled")
                        return RuntimeExamOutcome("settled", "official native action transition",
                                                  after, provenance, request.request_id, True,
                                                  json.loads(receipt.state_after), pending.get("native_context"))
            now = self.monotonic()
            if submitted and settlement is None and now >= next_selection_at:
                next_selection_at = now + 0.25
                if "exam.continuation" in self.client.read_status().get("capabilities", ()):
                    continuation_error = self._continue_selection(pending, request)
                    if continuation_error is not None:
                        return RuntimeExamOutcome("pending", continuation_error,
                                                  request_id=request.request_id, submitted=True)
            if submitted and settlement is not None and now >= next_snapshot_at:
                # A queue-drain receipt proves the chosen action was accepted.
                # A fresh same-sequence snapshot supplies S' even when the
                # recorder has not published its stronger transition yet.
                next_snapshot_at = now + 0.25
                try:
                    snapshot = self.client.execute("read_snapshot", timeout=min(2.0, max(0.0, deadline - now)))
                    if (snapshot.snapshot is not None and
                            _sequence_key(snapshot.snapshot.get("sequence_id")) == _sequence_key(pending["sequence_id"])):
                        after, provenance = _snapshot_evidence(snapshot, before, archive_root=self.client.root)
                        if after.state.is_native_actionable_settled or snapshot.snapshot.get("terminal") is True:
                            self._retire(pending, "settled")
                            return RuntimeExamOutcome("settled", "official queue drain and current DLL state",
                                                      after, provenance, request.request_id, True,
                                                      snapshot.snapshot["exam_save"], snapshot.snapshot.get("produce_context"),
                                                      native_snapshot=snapshot.snapshot,
                                                      native_session_generation=snapshot.request.session_generation)
                except RuntimeCommandError:
                    pass
            if self.cancelled is not None and self.cancelled():
                return RuntimeExamOutcome("pending", "cancelled while native action is pending",
                                          request_id=request.request_id, submitted=submitted)
            if now >= deadline:
                return RuntimeExamOutcome("pending", "native action settlement is still pending",
                                          request_id=request.request_id, submitted=submitted)
            self.sleep(min(0.05, max(0.0, deadline - now)))

    def _continue_selection(self, pending, parent: RuntimeCommandRequest) -> str | None:
        """Continue one game-owned selector under the existing main action."""
        from .runtime_card_choice_policy import choose_runtime_card_ui_action

        if self.cancelled is not None and self.cancelled():
            return "cancelled before native selector continuation"

        continuations = pending.setdefault("continuations", [])
        previous = continuations[-1] if continuations else None
        if previous is not None:
            raw = previous["request"]
            child = RuntimeCommandRequest(
                raw["request_id"], raw["session_generation"], raw["command"],
                raw.get("expected_revision"), raw.get("target"), raw.get("continuation_of"),
            )
            receipt = self.client.poll_result(child)
            if receipt is None:
                return None  # Keep polling this ID; do not submit the toggle again.
            if receipt.status != "submitted":
                return f"native selector {receipt.status}: {receipt.raw.get('error_message', '')}"
        result = self.client.execute("read_outer_snapshot", timeout=5)
        if result.status != "ok" or result.snapshot is None:
            return None
        native = result.snapshot
        if native.get("exam_continuation") is not True or native.get("busy") is not False:
            return None
        if result.request.session_generation != parent.session_generation:
            return "game process changed during the pending Exam selection"
        ui = native.get("ui_state")
        ui = ui if isinstance(ui, Mapping) else {}
        if native.get("actions_complete") is False or ui.get("blocker"):
            detail = str(ui.get("blocker") or "native action observation incomplete")
            if ui.get("owner_error"):
                detail += ": " + str(ui["owner_error"])
            return "native selector observation unavailable: " + detail
        context = native.get("parent_context")
        if not isinstance(context, Mapping):
            return "native selector parent identity unavailable"
        if _sequence_key(context.get("sequence_id")) != _sequence_key(pending["sequence_id"]):
            return "native selector belongs to another Exam sequence"
        source_guid = context.get("source_card_guid")
        if pending.get("card_guid") is not None and source_guid != pending["card_guid"]:
            return "native selector does not bind the pending played card"
        if pending.get("drink_id") is not None and context.get("source_drink_id") != pending["drink_id"]:
            return "native selector does not bind the pending drink"
        if previous is not None and previous["revision"] == native.get("revision"):
            return None
        if pending.get('secondary_policy') == 'gui-exam-bc-v1':
            self._validate_pending_model(pending)
            if self.model_policy is None:
                return 'pending model secondary policy is unavailable'
            if _secondary_reference_read_was_unstable(native):
                # Read-only retry stays inside the existing settlement deadline.
                # Preserve the failed observation; never call policy on it or
                # publish another parent/selector input from this snapshot.
                payload = json_bytes(result.raw)
                digest = hashlib.sha256(payload).hexdigest()
                path = self.client.root / 'exam_observation_retries' / parent.request_id / (digest + '.json')
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    if path.read_bytes() != payload:
                        raise RuntimeCommandProtocolError('Archived selector observation differs from its content hash')
                else:
                    atomic_write(path, payload)
                retries = pending.setdefault('observation_retries', [])
                if not any(item.get('observation_request_id') == result.request.request_id for item in retries):
                    retries.append({'schema': 'gkms.pending-selector-observation-retry.v1',
                        'parent_request_id': parent.request_id, 'observation_request_id': result.request.request_id,
                        'session_generation': parent.session_generation, 'revision': native.get('revision'),
                        'reason': 'native-reference-read-unstable',
                        'source_path': str(result.path),
                        'canonical_response_archive': {'path': str(path), 'sha256': digest, 'bytes': len(payload)},
                        'read_errors': deepcopy(native['exam_model_observation']['read_errors']),
                        'parent_request_unchanged': True, 'input_submitted': False,
                        'retry_budget': 'existing-action-settlement-deadline'})
                atomic_write(self.pending_path, json_bytes(pending))
                return None
            choice = self.model_policy.choose_secondary(native,
                session_generation=parent.session_generation, pending=pending)
        elif (pending.get("preferred_selection_guid") is None
                and pending.get("secondary_policy") == "native-card-choice-value-v1"):
            from .runtime_exam_continuation_policy import choose_native_exam_continuation

            choice = choose_native_exam_continuation(native, parent_card_id=pending.get("source_card_id"),
                parent_card_upgrade=pending.get("source_card_upgrade"), parent_drink_id=pending.get("drink_id"))
        else:
            choice = choose_runtime_card_ui_action(
                native, allow_exam_continuation=True,
                preferred_card_guid=pending.get("preferred_selection_guid"),
            )
        if choice is None:
            return "game requested an unsupported native card selection"
        target, reason = choice
        if target is None:
            return None if reason.get("status") == "waiting" else "native selector policy unavailable: " + str(reason.get("reason"))
        if self.cancelled is not None and self.cancelled():
            return "cancelled after native selector decision"
        child = self.client.make_request(
            "outer.action", expected_revision=native["revision"], target=target,
            continuation_of=parent.request_id,
        )
        continuations.append({"request": child.to_dict(), "revision": native["revision"], "decision": reason})
        atomic_write(self.pending_path, json_bytes(pending))
        if self.cancelled is not None and self.cancelled():
            # Only this never-published child is cancelled. Its parent may
            # already be executing and must remain pending for reconciliation.
            continuations.pop()
            atomic_write(self.pending_path, json_bytes(pending))
            return "cancelled before native selector publication"
        try:
            self.client.submit(child)
        except RuntimeCommandPending:
            return None  # Journal owns the same child on the next poll/restart.
        except RuntimeCommandError as error:
            return str(error)
        return None


class RuntimePlan1ActionExecutor:
    """Use the same native transaction gateway for Plan1's learned runtime."""

    def __init__(self, gateway: RuntimeExamGateway) -> None:
        self.gateway = gateway

    def __call__(self, *, candidate, evidence, snapshot):
        from .plan1_learned_policy_runtime import Plan1MaaExecutionReceipt
        from .unified_legal_action_snapshot import UnifiedLegalActionSnapshot

        if not isinstance(snapshot, UnifiedLegalActionSnapshot) or not snapshot.complete:
            raise ValueError("DLL action requires a complete legal action snapshot")
        action_id = candidate.get("action_id")
        if action_id not in snapshot.action_ids or evidence.digest() != snapshot.boundary_digest:
            raise ValueError("DLL action does not bind the planned legal boundary")
        outcome = self.gateway.execute(
            candidate["kind"], evidence, card_guid=candidate.get("card_guid", candidate.get("guid")),
            slot=candidate.get("slot", candidate.get("slot_index")), drink_id=candidate.get("drink_id"),
            **({"preferred_selection_guid": candidate["selected_card_guid"]}
               if candidate.get("selected_card_guid") else {}),
            **({"secondary_policy": candidate["secondary_policy"]}
               if candidate.get("secondary_policy") else {}),
        )
        if outcome.status == "settled" and outcome.evidence is not None:
            return Plan1MaaExecutionReceipt(
                action_id=action_id, boundary_before_digest=evidence.digest(), input_submitted=True,
                boundary_after_digest=outcome.evidence.digest(), state_after=outcome.evidence.state.to_dict(),
                submission_proof={"executor": "dll", "request_id": outcome.request_id,
                                  "native_state": outcome.provenance.to_dict() if outcome.provenance else None},
            )
        return Plan1MaaExecutionReceipt(
            action_id=action_id, boundary_before_digest=evidence.digest(),
            input_submitted=outcome.submitted,
            submission_proof={"executor": "dll", "request_id": outcome.request_id} if outcome.submitted else None,
            blockers=(f"dll-{outcome.status}:{outcome.detail}",),
        )
