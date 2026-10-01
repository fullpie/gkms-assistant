"""Persist native-legal N.I.A. decisions before a full transition settles.

The exact live runner can prove a complete planner-admitted action set and a
submitted action before ``ExamSaveData`` reaches the next actionable state.
Those rows are useful for offline A/B and contextual behaviour learning, but
they are not full reinforcement-learning transitions: reward and next state
remain explicitly absent.  The existing ``nia_inner_transition_dataset``
keeps ownership of rows that later acquire an observed settled after-state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from typing import Any

from .audition_local_save_state import AuditionLocalSaveStateEvidence
from .nia_inner_transition_collector import (
    LegalCandidateProvider,
    NiaCompleteLegalCandidates,
    _normalize_binding_override,
    build_plan2_learning_decision_receipt,
    merge_plan2_learning_decision_receipt_metadata,
)
from .plan2_native_horizon import Plan2NativeAction, Plan2NativeDrinkAction
from .plan2_native_unattended_loop import Plan2NativeUnattendedStepRecord


SCHEMA = "gkms.nia-legal-decision.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "var" / "nia_training" / "legal_decisions.jsonl"
_PLAN_TYPES = {
    2: "ProducePlanType_Plan1",
    3: "ProducePlanType_Plan2",
    4: "ProducePlanType_Plan3",
}
_EFFECT_TYPES = {
    2: "ProduceExamEffectType_ExamParameterBuff",
    10: "ProduceExamEffectType_ExamLessonBuff",
    31: "ProduceExamEffectType_ExamReview",
    42: "ProduceExamEffectType_ExamCardPlayAggressive",
    45: "ProduceExamEffectType_ExamConcentration",
}
_STAGES = {
    16: "ProduceStepType_AuditionMid1",
    17: "ProduceStepType_AuditionMid2",
    18: "ProduceStepType_AuditionFinal",
}


def _action_dict(value: object) -> dict[str, object]:
    if isinstance(value, Plan2NativeDrinkAction):
        return {
            "kind": value.kind,
            "slot_index": value.slot_index,
            "instance_id": value.instance_id,
            "drink_id": value.drink_id,
            "selected_card_guid": value.selected_card_guid,
            "action_id": value.action_id,
        }
    if isinstance(value, Plan2NativeAction):
        return {
            "kind": value.kind,
            "card_guid": value.card_guid,
            "action_id": value.action_id,
        }
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"unsupported legal decision action: {type(value).__name__}")


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class NiaLegalDecision:
    boundary_digest: str
    source_id: str
    step: int
    state_before: Mapping[str, object]
    legal_candidates: tuple[Mapping[str, object], ...]
    action: Mapping[str, object]
    metadata: Mapping[str, object]
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError("unsupported legal decision schema")
        if (
            not isinstance(self.boundary_digest, str)
            or len(self.boundary_digest) != 64
            or any(value not in "0123456789abcdef" for value in self.boundary_digest)
        ):
            raise ValueError("boundary_digest must be lowercase SHA-256")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("source_id must be non-empty text")
        if isinstance(self.step, bool) or not isinstance(self.step, int) or self.step < 1:
            raise ValueError("step must be a positive integer")
        if not isinstance(self.state_before, Mapping):
            raise TypeError("state_before must be an object")
        candidates = tuple(self.legal_candidates)
        if not candidates or any(not isinstance(value, Mapping) for value in candidates):
            raise ValueError("legal_candidates must be a non-empty object tuple")
        candidate_keys = tuple(_canonical(value) for value in candidates)
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("legal_candidates must be unique")
        if not isinstance(self.action, Mapping) or _canonical(self.action) not in candidate_keys:
            raise ValueError("chosen action is outside legal_candidates")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be an object")
        if self.metadata.get("candidate_complete") is not True:
            raise ValueError("candidate_complete must be true")
        object.__setattr__(self, "legal_candidates", candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "boundary_digest": self.boundary_digest,
            "source_id": self.source_id,
            "step": self.step,
            "state_before": dict(self.state_before),
            "legal_candidates": [dict(value) for value in self.legal_candidates],
            "action": dict(self.action),
            "reward": None,
            "next_state": None,
            "contextual_bandit": True,
            "full_rl_transition": False,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "NiaLegalDecision":
        if raw.get("schema") != SCHEMA:
            raise ValueError("unsupported legal decision schema")
        if (
            raw.get("reward") is not None
            or raw.get("next_state") is not None
            or raw.get("contextual_bandit") is not True
            or raw.get("full_rl_transition") is not False
        ):
            raise ValueError("legal decision must not claim a full transition")
        candidates = raw.get("legal_candidates")
        if not isinstance(candidates, list):
            raise ValueError("legal_candidates must be an array")
        state = raw.get("state_before")
        action = raw.get("action")
        metadata = raw.get("metadata")
        if not all(isinstance(value, Mapping) for value in (state, action, metadata)):
            raise ValueError("legal decision object fields are incomplete")
        return cls(
            str(raw.get("boundary_digest", "")),
            str(raw.get("source_id", "")),
            int(raw.get("step", 0)),
            state,
            tuple(value for value in candidates if isinstance(value, Mapping)),
            action,
            metadata,
        )


def build_nia_legal_decision(
    record: Plan2NativeUnattendedStepRecord,
    legal: NiaCompleteLegalCandidates,
    *,
    submission_proof: str,
    source_id_override: str | None = None,
    run_binding_id: str | None = None,
    retained_proof: bool = False,
) -> NiaLegalDecision:
    if not isinstance(record, Plan2NativeUnattendedStepRecord):
        raise TypeError("record must be Plan2NativeUnattendedStepRecord")
    before = record.evidence_before
    if before is None or record.action is None or record.decision is None:
        raise ValueError("legal decision record is incomplete")
    if not record.decision.decision_ready or record.decision.best_action != record.action:
        raise ValueError("legal decision does not match the planner")
    if not isinstance(legal, NiaCompleteLegalCandidates) or not legal.complete:
        raise ValueError("complete legal candidates are required")
    if not isinstance(submission_proof, str) or not submission_proof:
        raise ValueError("submission_proof must be non-empty text")
    if type(retained_proof) is not bool:
        raise TypeError("retained_proof must be bool")
    binding_override = _normalize_binding_override(
        source_id_override,
        run_binding_id,
    )
    source_id = before.run_id if binding_override is None else binding_override
    candidates = tuple(_action_dict(value) for value in legal.candidates)
    action = _action_dict(record.action)
    decision_receipt = build_plan2_learning_decision_receipt(
        record,
        before,
        source_id=source_id,
        action=action,
        legal_candidates=candidates,
        retained_proof=retained_proof,
    )
    boundary_digest = str(decision_receipt["legal_boundary_digest"])
    runtime = before.state.root_runtime
    opaque = None if runtime is None else runtime.opaque_fields.to_value()
    produce_id = opaque.get("produceId") if isinstance(opaque, Mapping) else None
    plan_type = (
        _PLAN_TYPES.get(opaque.get("planType"))
        if isinstance(opaque, Mapping)
        else None
    )
    exam_effect_type = (
        _EFFECT_TYPES.get(
            opaque.get("mainEffectType", opaque.get("displayMainEffectType"))
        )
        if isinstance(opaque, Mapping)
        else None
    )
    stage = _STAGES.get(before.state.step_type_value)
    metadata = {
        "candidate_authority": legal.authority,
        "candidate_complete": True,
        "submission_proof": submission_proof,
        "run_id": before.run_id,
        "step_context_id": before.step_context_id,
        "step_context_digest": before.step_context_digest,
        "session_transition_id": before.session_transition_id,
        "source_sha256": before.source_sha256,
        "evidence_digest": before.digest(),
        "full_rl_deferred": True,
        "produce_id": produce_id,
        "plan_type": plan_type,
        "exam_effect_type": exam_effect_type,
        "stage": stage,
        "turn": before.state.current_turn,
    }
    merge_plan2_learning_decision_receipt_metadata(
        metadata,
        decision_receipt,
    )
    if binding_override is not None:
        metadata.update(
            {
                "evidence_run_id": before.run_id,
                "run_binding_id": binding_override,
            }
        )
    return NiaLegalDecision(
        boundary_digest=boundary_digest,
        source_id=(
            f"{before.run_id}:{before.source_sha256}"
            if binding_override is None
            else source_id
        ),
        step=record.step_index,
        state_before=before.state.to_dict(),
        legal_candidates=candidates,
        action=action,
        metadata=metadata,
    )


@dataclass(slots=True)
class NiaLegalDecisionCollector:
    candidate_provider: LegalCandidateProvider
    output: Path = field(default_factory=lambda: DEFAULT_OUTPUT)
    source_id_override: str | None = None
    run_binding_id: str | None = None
    _rows: list[NiaLegalDecision] = field(default_factory=list, init=False)
    _seen: set[str] = field(default_factory=set, init=False)
    _rejections: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if not callable(self.candidate_provider):
            raise TypeError("candidate_provider must be callable")
        _normalize_binding_override(self.source_id_override, self.run_binding_id)
        self.output = Path(self.output)
        if not self.output.exists():
            return
        for line_number, line in enumerate(
            self.output.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                row = NiaLegalDecision.from_dict(raw)
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                raise ValueError(
                    f"invalid legal decision line {line_number}: {error}"
                ) from error
            if row.boundary_digest in self._seen:
                raise ValueError("duplicate legal decision boundary")
            self._seen.add(row.boundary_digest)
            self._rows.append(row)

    @property
    def rows(self) -> tuple[NiaLegalDecision, ...]:
        return tuple(self._rows)

    @property
    def rejections(self) -> tuple[str, ...]:
        return tuple(self._rejections)

    def _write(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.output.name}.", suffix=".tmp", dir=str(self.output.parent)
            )
            with open(descriptor, "w", encoding="utf-8", newline="\n", closefd=True) as stream:
                for row in self._rows:
                    stream.write(_canonical(row.to_dict()))
                    stream.write("\n")
            Path(temporary).replace(self.output)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def collect(
        self,
        record: Plan2NativeUnattendedStepRecord,
        *,
        submission_proof: str,
        retained_proof: bool = False,
    ) -> NiaLegalDecision | None:
        try:
            legal = self.candidate_provider(record)
            if legal is None:
                raise ValueError("legal-candidates-missing")
            row = build_nia_legal_decision(
                record,
                legal,
                submission_proof=submission_proof,
                source_id_override=self.source_id_override,
                run_binding_id=self.run_binding_id,
                retained_proof=retained_proof,
            )
        except (TypeError, ValueError) as error:
            self._rejections.append(str(error))
            return None
        if row.boundary_digest in self._seen:
            return row
        self._rows.append(row)
        self._seen.add(row.boundary_digest)
        try:
            self._write()
        except OSError as error:
            self._rows.pop()
            self._seen.remove(row.boundary_digest)
            self._rejections.append(f"persist-failed:{type(error).__name__}:{error}")
            return None
        return row

    def __call__(self, record: Plan2NativeUnattendedStepRecord) -> None:
        execution = record.execution
        if (
            execution is None
            or not execution.accepted
            or execution.replan
            or record.action is None
        ):
            return
        self.collect(record, submission_proof="accepted-exact-execution")

    def collect_retained(
        self,
        record: Plan2NativeUnattendedStepRecord,
        retained: AuditionLocalSaveStateEvidence,
    ) -> NiaLegalDecision | None:
        action = record.action
        playing = retained.state.playing_card
        if (
            not isinstance(action, Plan2NativeAction)
            or action.kind != "play"
            or playing is None
            or playing.guid != action.card_guid
        ):
            self._rejections.append("retained-transition-action-mismatch")
            return None
        return self.collect(
            record,
            submission_proof="retained-playing-card-command-queue",
            retained_proof=True,
        )


def load_nia_legal_decision_dataset(
    path: str | Path = DEFAULT_OUTPUT,
) -> tuple[NiaLegalDecision, ...]:
    """Load a legal-decision JSONL while enforcing unique boundaries."""

    source = Path(path)
    rows: list[NiaLegalDecision] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise ValueError("row must be an object")
            row = NiaLegalDecision.from_dict(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError(
                f"invalid legal decision line {line_number}: {error}"
            ) from error
        if row.boundary_digest in seen:
            raise ValueError("duplicate legal decision boundary")
        seen.add(row.boundary_digest)
        rows.append(row)
    return tuple(rows)


__all__ = [
    "DEFAULT_OUTPUT",
    "NiaLegalDecision",
    "NiaLegalDecisionCollector",
    "SCHEMA",
    "build_nia_legal_decision",
    "load_nia_legal_decision_dataset",
]
