"""Read-only replay gate for every live audition hand after bootstrap."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contextual_passive_step import ContextualPassiveStepContext
from .exam_session import ExamSession, ExamSessionPreflightBinding, load_exam_session
from .item_rules import EquippedItemRule
from .live_exam_context import LiveExamContextError, derive_live_exam_entry_context
from .live_exam_pipeline import exam_session_binding_from_preflight
from .live_exam_preflight import LiveExamPreflightResolution, resolve_live_exam_preflight
from .logic_engine import LogicExamState
from .master_db import DEFAULT_DATABASE
from .passive_catalog import MasterPassiveCatalog
from .run_identity import DEFAULT_RUN_ROOT, RunIdentity, paths_for


def _deduplicate(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class LiveExamRuntimeGate:
    identity: RunIdentity
    session: ExamSession | None
    context: ContextualPassiveStepContext | None
    resolution: LiveExamPreflightResolution | None
    binding: ExamSessionPreflightBinding | None
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return bool(
            not self.blockers
            and self.session is not None
            and self.context is not None
            and self.resolution is not None
            and self.resolution.ready
            and self.binding is not None
            and self.session.matches_preflight_binding(self.binding)
            and self.resolution.merged_item_rule is not None
        )

    @property
    def merged_item_rule(self) -> EquippedItemRule | None:
        return None if self.resolution is None else self.resolution.merged_item_rule


def resolve_live_exam_runtime_gate(
    *,
    identity: RunIdentity,
    step_type: str,
    stage_number: int,
    observed_checkpoint_state: Mapping[str, Any] | LogicExamState | None = None,
    catalog: MasterPassiveCatalog | None = None,
    root: Path = DEFAULT_RUN_ROOT,
    database: Path = DEFAULT_DATABASE,
) -> LiveExamRuntimeGate:
    """Replay the durable safety bundle before solving any visible hand."""

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be RunIdentity")
    identity.validate()
    if not isinstance(step_type, str) or not step_type:
        raise ValueError("step_type must be non-empty text")
    if (
        not isinstance(stage_number, int)
        or isinstance(stage_number, bool)
        or stage_number < 1
    ):
        raise ValueError("stage_number must be an integer >= 1")
    root = Path(root)
    database = Path(database)
    paths = paths_for(identity, root=root)
    blockers: list[str] = []
    try:
        session = load_exam_session(paths.exam_session)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return LiveExamRuntimeGate(
            identity,
            None,
            None,
            None,
            None,
            (f"exam-session-invalid:{type(error).__name__}:{error}",),
        )
    if session is None:
        return LiveExamRuntimeGate(
            identity,
            None,
            None,
            None,
            None,
            ("exam-session-missing",),
        )
    blockers.extend(session.live_auto_click_blockers())
    if not session.matches_run(
        run_id=identity.run_id,
        idol_card_id=identity.idol_card_id,
        character_id=identity.character_id,
        produce_id=identity.produce_id,
    ):
        blockers.append("exam-session-run-identity-mismatch")
    if session.step_type != step_type or session.stage_number != stage_number:
        blockers.append("exam-session-stage-identity-mismatch")
    if session.preflight_binding is None:
        blockers.append("exam-session-preflight-binding-missing")
        return LiveExamRuntimeGate(
            identity,
            session,
            None,
            None,
            None,
            _deduplicate(blockers),
        )

    try:
        context = derive_live_exam_entry_context(
            identity,
            snapshot_digest=session.preflight_binding.loadout_snapshot_digest,
            step_type=step_type,
            stage_number=stage_number,
            expected_context_digest=session.preflight_binding.step_context_digest,
        )
    except (LiveExamContextError, OSError, TypeError, ValueError) as error:
        return LiveExamRuntimeGate(
            identity,
            session,
            None,
            None,
            session.preflight_binding,
            _deduplicate(
                blockers
                + [f"exam-context-replay-failed:{type(error).__name__}:{error}"]
            ),
        )
    if context.context_id != session.preflight_binding.step_context_id:
        blockers.append("exam-session-context-id-mismatch")

    try:
        resolution = resolve_live_exam_preflight(
            identity=identity,
            context=context,
            stage_number=stage_number,
            catalog=catalog or MasterPassiveCatalog.load(),
            root=root,
            database=database,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return LiveExamRuntimeGate(
            identity,
            session,
            context,
            None,
            session.preflight_binding,
            _deduplicate(
                blockers
                + [f"preflight-replay-failed:{type(error).__name__}:{error}"]
            ),
        )
    blockers.extend(resolution.blockers)
    binding: ExamSessionPreflightBinding | None = None
    if resolution.ready:
        try:
            binding = exam_session_binding_from_preflight(context, resolution)
        except (TypeError, ValueError) as error:
            blockers.append(
                f"preflight-binding-invalid:{type(error).__name__}:{error}"
            )
        else:
            if not session.matches_preflight_binding(binding):
                blockers.append("exam-session-preflight-binding-mismatch")

    if observed_checkpoint_state is not None:
        try:
            observed = (
                observed_checkpoint_state
                if isinstance(observed_checkpoint_state, LogicExamState)
                else LogicExamState(**dict(observed_checkpoint_state))
            )
            observed.validate(allow_completed=True)
        except (TypeError, ValueError) as error:
            blockers.append(
                f"observed-checkpoint-state-invalid:{type(error).__name__}:{error}"
            )
        else:
            if observed != session.logic_state:
                blockers.append("observed-checkpoint-state-session-mismatch")

    return LiveExamRuntimeGate(
        identity,
        session,
        context,
        resolution,
        binding,
        _deduplicate(blockers),
    )


__all__ = ["LiveExamRuntimeGate", "resolve_live_exam_runtime_gate"]
