"""Fail-closed bridge from verified frames to durable replay/checkpoints.

The GUI may decide what a checkpoint represents, but it must not have to
reimplement the safety invariant: a state writer is called only when the final
two phase samples independently verify the same model state.  Every attempt is
recorded, including rejected stable frames.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .live_actions import CardPlayExecutionResult, CardPlayFrameSample
from .transition_replay import (
    DEFAULT_TRANSITION_REPLAY_ROOT,
    SavedTransitionReplay,
    TransitionReplayEvidence,
    TransitionReplayIds,
    finalize_transition_replay,
    save_transition_replay,
)


@dataclass(frozen=True, slots=True)
class TransitionCheckpointWrite(Mapping[str, Any]):
    """Typed input for a verified transition's checkpoint writer.

    ``replay_record_id`` is the ID of the already-durable
    ``verified_pending_checkpoint`` replay.  Keeping it beside the terminal
    verified state lets a checkpoint (notably ExamSession v3) persist the
    exact transition ID without guessing it through a closure or directory
    scan.

    The object also implements the read-only ``Mapping`` interface over
    ``verified_state``.  Existing one-argument writers which only consume the
    state continue to work, while new writers can use the explicit typed
    attributes.
    """

    verified_state: Mapping[str, Any]
    replay_record_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.replay_record_id, str) or not self.replay_record_id:
            raise ValueError("replay_record_id must be a non-empty string")
        if not isinstance(self.verified_state, Mapping):
            raise TypeError("verified_state must be a mapping")
        object.__setattr__(
            self,
            "verified_state",
            MappingProxyType(dict(self.verified_state)),
        )

    def __getitem__(self, key: str) -> Any:
        return self.verified_state[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.verified_state)

    def __len__(self) -> int:
        return len(self.verified_state)


CheckpointWriter = Callable[[TransitionCheckpointWrite], Any]


@dataclass(frozen=True, slots=True)
class TransitionCommitResult:
    replay: SavedTransitionReplay
    committed: bool
    checkpoint_result: Any | None
    failure_reason: str | None


def _stable_verified_pair(
    samples: tuple[CardPlayFrameSample, ...],
) -> tuple[CardPlayFrameSample, CardPlayFrameSample] | None:
    """Return the terminal qualifying pair; never accept earlier stability."""

    if len(samples) < 2:
        return None
    previous, current = samples[-2:]
    before = previous.analysis
    after = current.analysis
    if (
        before.phase in {"settled", "result"}
        and before.phase == after.phase
        and before.semantic_key == after.semantic_key
        and before.expected_matches
        and before.expected_matches == after.expected_matches
        and bool(before.verified_state)
        and dict(before.verified_state) == dict(after.verified_state)
    ):
        return previous, current
    return None


def _verification_summary(
    result: CardPlayExecutionResult,
    terminal_pair: tuple[CardPlayFrameSample, CardPlayFrameSample] | None,
    *,
    verified: bool,
    failure_reason: str | None,
) -> dict[str, Any]:
    samples = result.phase_samples
    confidences = [sample.analysis.confidence for sample in samples]
    terminal_state = (
        dict(terminal_pair[1].analysis.verified_state)
        if terminal_pair is not None
        else None
    )
    result_state = dict(result.verified_state)
    return {
        "execution_reported_committed": result.committed,
        # Keep this field aligned with whether the evidence is accepted by
        # replay summaries.  ``terminal_pair_stable`` distinguishes a real
        # terminal pair from an inconsistent aggregate result.
        "stable_verified_pair": verified,
        "terminal_pair_stable": terminal_pair is not None,
        "result_verified_state_present": bool(result_state),
        "result_verified_state_matches_terminal_pair": (
            terminal_state is not None
            and bool(result_state)
            and result_state == terminal_state
        ),
        "sample_count": len(samples),
        "confidence": {
            "minimum": min(confidences) if confidences else None,
            "final": confidences[-1] if confidences else None,
        },
        "failure_reason": failure_reason,
        "outcome": "verified" if verified else "rejected",
    }


def record_and_commit_verified_transition(
    *,
    result: CardPlayExecutionResult,
    before: Any,
    predicted: Any | None,
    action: Any,
    checkpoint_writer: CheckpointWriter | None,
    card: Any | None = None,
    context: Mapping[str, Any] | None = None,
    ids: TransitionReplayIds | Mapping[str, Any] | None = None,
    checkpoint: Mapping[str, Any] | None = None,
    replay_root: Path = DEFAULT_TRANSITION_REPLAY_ROOT,
) -> TransitionCommitResult:
    """Persist an attempt, then update a checkpoint only after verification.

    ``observed_state`` is deliberately retained as the measurement in the
    replay.  The terminal pair's ``verified_state`` is passed to the writer
    only after the stable-pair check and an exact comparison with the result's
    state, so partial observations cannot overwrite a richer trusted
    checkpoint and prediction cannot be committed as an observation.  The
    writer receives a :class:`TransitionCheckpointWrite` whose replay ID
    refers to an already-durable ``verified_pending_checkpoint`` record.
    """

    pair = _stable_verified_pair(result.phase_samples)
    terminal_verified_state = (
        dict(pair[1].analysis.verified_state) if pair is not None else {}
    )
    result_verified_state = dict(result.verified_state)
    if pair is None:
        verification_failure = (
            result.failure_reason or "stable-verification-missing"
        )
    elif not result_verified_state:
        verification_failure = "result-verified-state-missing"
    elif result_verified_state != terminal_verified_state:
        verification_failure = "result-verified-state-mismatch"
    else:
        verification_failure = None
    verified = verification_failure is None
    verification = _verification_summary(
        result,
        pair,
        verified=verified,
        failure_reason=verification_failure,
    )
    replay_context = dict(context or {})
    replay_context["verification"] = verification
    resolving = tuple(
        sample.capture
        for sample in result.phase_samples
        if sample.analysis.phase in {"resolving", "failed"}
    )
    settled = tuple(
        sample.capture
        for sample in result.phase_samples
        if sample.analysis.phase in {"settled", "result"}
    )
    unknown_effect = any(
        "unknown" in issue.casefold() or "unsupported" in issue.casefold()
        for sample in result.phase_samples
        for issue in sample.analysis.issues
    )
    replay_kwargs = dict(
        before=before,
        predicted=predicted,
        observed=dict(result.observed_state) or None,
        action=action,
        card=card,
        context=replay_context,
        ids=ids,
        phase_trace=[sample.to_dict() for sample in result.phase_samples],
        checkpoint={**dict(checkpoint or {}), "verification": verification},
        evidence=TransitionReplayEvidence(
            pre=result.pre_capture,
            preview=result.preview_capture,
            resolving=resolving,
            settled=settled,
        ),
        unknown_effect=unknown_effect,
        root=replay_root,
    )
    if not verified:
        saved = save_transition_replay(
            status="rejected",
            failure_reason=verification_failure,
            **replay_kwargs,
        )
        return TransitionCommitResult(
            saved, False, None, verification_failure
        )
    if checkpoint_writer is None:
        # A verified frame without an authorized target is evidence only.
        saved = save_transition_replay(
            status="verified_uncommitted",
            failure_reason="checkpoint-writer-missing",
            **replay_kwargs,
        )
        return TransitionCommitResult(saved, False, None, "checkpoint-writer-missing")

    saved = save_transition_replay(
        status="verified_pending_checkpoint",
        failure_reason=None,
        **replay_kwargs,
    )
    checkpoint_write = TransitionCheckpointWrite(
        verified_state=terminal_verified_state,
        replay_record_id=saved.record_id,
    )
    try:
        written = checkpoint_writer(checkpoint_write)
    except Exception as error:
        reason = f"checkpoint-write-failed:{type(error).__name__}:{error}"
        saved = finalize_transition_replay(
            saved,
            status="checkpoint_failed",
            failure_reason=reason,
            checkpoint_committed=False,
        )
        return TransitionCommitResult(
            saved, False, None, reason
        )
    saved = finalize_transition_replay(
        saved,
        status="committed",
        failure_reason=None,
        checkpoint_committed=True,
    )
    return TransitionCommitResult(saved, True, written, None)
