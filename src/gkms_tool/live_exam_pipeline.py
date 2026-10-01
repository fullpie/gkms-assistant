"""Pure, fail-closed bridge from live Exam evidence to a schema-v3 session.

This module deliberately has no capture, input, GUI, or process-memory code.
Its only side effects are run-scoped, durable publication/checkpoint writes.
The caller must first obtain a stable full-screen observation and a clean
first-turn :class:`LogicExamState`; a ready result is returned only after the
preflight bundle and schema-v3 checkpoint have both been persisted and read
back successfully.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from .contextual_passive_step import (
    ContextualPassiveStepContext,
    ContextualPassiveStepSession,
    load_contextual_passive_step_session,
)
from .equipped_item_snapshot import (
    EquippedItemSnapshot,
    load_equipped_item_snapshot,
)
from .exam_session import (
    SESSION_SCHEMA_VERSION,
    ExamSession,
    ExamSessionConflictError,
    ExamSessionPreflightBinding,
    bootstrap_transition_id,
    load_exam_session,
    save_exam_session,
)
from .initial_modifier_reconciliation import (
    InitialModifierReconciliation,
    load_initial_modifier_reconciliation,
)
from .item_rules import EquippedItemRule
from .live_exam_context import LiveExamContextError, derive_live_exam_entry_context
from .live_exam_preflight import (
    LiveExamEntryStatusObservation,
    LiveExamPreflightBlockedError,
    LiveExamPreflightResolution,
    LoadoutPreflightPublication,
    apply_preflight_bootstrap,
    publish_loadout_preflight,
    resolve_live_exam_preflight,
)
from .loadout_runtime_bridge import loadout_snapshot_digest
from .loadout_snapshot import LoadoutSnapshot
from .logic_engine import LogicExamState
from .master_db import DEFAULT_DATABASE
from .passive_catalog import MasterPassiveCatalog
from .run_deck_snapshot import RunDeckSnapshot, load_run_deck_snapshot
from .run_identity import DEFAULT_RUN_ROOT, RunIdentity, paths_for


_T = TypeVar("_T")
_BOOTSTRAP_OWNED_STATE_FIELDS = frozenset(
    {
        "good_impression",
        "motivation",
        "good_impression_exists",
        "good_impression_passing_turn_start",
        "runtime_status_enchants",
        "last_resolved_turn_start_round",
        "last_resolved_runtime_status_round",
    }
)


def _deduplicate(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class LiveExamPipelineResult:
    """Typed hand-off returned to the future UI/automatic-action layer."""

    identity: RunIdentity
    context: ContextualPassiveStepContext | None
    loadout_snapshot: LoadoutSnapshot | None
    initial_reconciliation: InitialModifierReconciliation | None
    contextual_session: ContextualPassiveStepSession | None
    equipped_item_snapshot: EquippedItemSnapshot | None
    deck_snapshot: RunDeckSnapshot | None
    publication: LoadoutPreflightPublication | None
    preflight_resolution: LiveExamPreflightResolution | None
    bootstrapped_state: LogicExamState | None
    session: ExamSession | None
    merged_item_rule: EquippedItemRule | None
    blockers: tuple[str, ...]

    @property
    def ready(self) -> bool:
        """Whether a suggestion may now be computed (never whether to click)."""

        return bool(
            not self.blockers
            and self.context is not None
            and self.deck_snapshot is not None
            and self.preflight_resolution is not None
            and self.preflight_resolution.ready
            and self.bootstrapped_state is not None
            and self.session is not None
            and self.session.schema_version == SESSION_SCHEMA_VERSION
            and not self.session.live_auto_click_blockers()
            and self.merged_item_rule is not None
        )


def _result(
    *,
    identity: RunIdentity,
    context: ContextualPassiveStepContext | None = None,
    loadout_snapshot: LoadoutSnapshot | None = None,
    initial_reconciliation: InitialModifierReconciliation | None = None,
    contextual_session: ContextualPassiveStepSession | None = None,
    equipped_item_snapshot: EquippedItemSnapshot | None = None,
    deck_snapshot: RunDeckSnapshot | None = None,
    publication: LoadoutPreflightPublication | None = None,
    preflight_resolution: LiveExamPreflightResolution | None = None,
    bootstrapped_state: LogicExamState | None = None,
    session: ExamSession | None = None,
    merged_item_rule: EquippedItemRule | None = None,
    blockers: tuple[str, ...] | list[str] = (),
) -> LiveExamPipelineResult:
    return LiveExamPipelineResult(
        identity=identity,
        context=context,
        loadout_snapshot=loadout_snapshot,
        initial_reconciliation=initial_reconciliation,
        contextual_session=contextual_session,
        equipped_item_snapshot=equipped_item_snapshot,
        deck_snapshot=deck_snapshot,
        publication=publication,
        preflight_resolution=preflight_resolution,
        bootstrapped_state=bootstrapped_state,
        session=session,
        merged_item_rule=merged_item_rule,
        blockers=_deduplicate(tuple(blockers)),
    )


def _load_canonical_loadout(path: Path) -> LoadoutSnapshot | None:
    """Strict decoder for both the prerequisite and observation draft."""

    path = Path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("loadout path is not a regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("loadout root must be a JSON object")
    snapshot = LoadoutSnapshot.from_dict(payload)
    if dict(payload) != snapshot.to_dict():
        raise ValueError("loadout fields or values are not canonical")
    return snapshot


def _load_optional(
    path: Path,
    loader: Callable[[Path], _T | None],
    *,
    missing: str,
    invalid: str,
) -> tuple[_T | None, tuple[str, ...]]:
    if not Path(path).exists():
        return None, (missing,)
    try:
        value = loader(Path(path))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return None, (f"{invalid}:{type(error).__name__}:{error}",)
    if value is None:
        return None, (missing,)
    return value, ()


def _state_base_mismatches(
    clean_state: LogicExamState, persisted_state: LogicExamState
) -> tuple[str, ...]:
    mismatches: list[str] = []
    for state_field in fields(LogicExamState):
        name = state_field.name
        if name in _BOOTSTRAP_OWNED_STATE_FIELDS:
            continue
        if getattr(clean_state, name) != getattr(persisted_state, name):
            mismatches.append(f"exam-session-visible-state-mismatch:{name}")
    return tuple(mismatches)


def _load_pipeline_artifacts(
    identity: RunIdentity,
    *,
    root: Path,
    database: Path,
) -> tuple[
    LoadoutSnapshot | None,
    InitialModifierReconciliation | None,
    ContextualPassiveStepSession | None,
    EquippedItemSnapshot | None,
    RunDeckSnapshot | None,
    tuple[str, ...],
]:
    paths = paths_for(identity, root=root)
    blockers: list[str] = []

    # A malformed formal prerequisite is never bypassed with the draft.  The
    # draft is eligible only before formal publication and must itself be the
    # complete authoritative observation.
    loadout: LoadoutSnapshot | None = None
    if paths.loadout_snapshot.exists():
        try:
            loadout = _load_canonical_loadout(paths.loadout_snapshot)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            blockers.append(
                f"loadout-prerequisite-invalid:{type(error).__name__}:{error}"
            )
    elif paths.loadout_observation_draft.exists():
        try:
            loadout = _load_canonical_loadout(paths.loadout_observation_draft)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            blockers.append(
                f"loadout-observation-draft-invalid:{type(error).__name__}:{error}"
            )
        if loadout is not None:
            blockers.extend(
                f"loadout-observation-draft:{reason}"
                for reason in loadout.observation_blocking_reasons()
            )
    else:
        blockers.append("loadout-prerequisite-and-authoritative-draft-missing")

    initial, reasons = _load_optional(
        paths.initial_modifier_reconciliation,
        load_initial_modifier_reconciliation,
        missing="initial-reconciliation-missing",
        invalid="initial-reconciliation-invalid",
    )
    blockers.extend(reasons)
    contextual, reasons = _load_optional(
        paths.contextual_passive_session,
        load_contextual_passive_step_session,
        missing="contextual-session-missing",
        invalid="contextual-session-invalid",
    )
    blockers.extend(reasons)
    equipped, reasons = _load_optional(
        paths.equipped_item_snapshot,
        lambda path: load_equipped_item_snapshot(path, database=database),
        missing="equipped-item-snapshot-missing",
        invalid="equipped-item-snapshot-invalid",
    )
    blockers.extend(reasons)
    try:
        deck = load_run_deck_snapshot(identity, root=root)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        deck = None
        blockers.append(f"deck-snapshot-invalid:{type(error).__name__}:{error}")
    if deck is None:
        blockers.append("deck-snapshot-missing")

    return (
        loadout,
        initial,
        contextual,
        equipped,
        deck,
        _deduplicate(blockers),
    )


def exam_session_binding_from_preflight(
    context: ContextualPassiveStepContext,
    resolution: LiveExamPreflightResolution,
) -> ExamSessionPreflightBinding:
    """Build the exact v3 session binding for a ready preflight replay."""

    if not isinstance(context, ContextualPassiveStepContext):
        raise TypeError("context must be ContextualPassiveStepContext")
    if not isinstance(resolution, LiveExamPreflightResolution):
        raise TypeError("resolution must be LiveExamPreflightResolution")
    if not resolution.ready or resolution.bootstrap_status is None:
        raise LiveExamPreflightBlockedError(
            resolution.blockers or ("preflight-resolution-not-ready",)
        )
    required = (
        resolution.loadout_snapshot_digest,
        resolution.contextual_session_digest,
        resolution.initial_reconciliation_digest,
        resolution.equipped_item_snapshot_digest,
        resolution.deck_snapshot_digest,
    )
    if any(value is None for value in required):
        raise LiveExamPreflightBlockedError(("preflight-binding-digest-missing",))
    return ExamSessionPreflightBinding(
        loadout_snapshot_digest=resolution.loadout_snapshot_digest or "",
        step_context_id=context.context_id,
        step_context_digest=context.digest,
        contextual_session_digest=resolution.contextual_session_digest or "",
        initial_reconciliation_digest=resolution.initial_reconciliation_digest or "",
        equipped_item_snapshot_digest=(
            resolution.equipped_item_snapshot_digest or ""
        ),
        deck_snapshot_digest=resolution.deck_snapshot_digest or "",
        bootstrap_status_digest=resolution.bootstrap_status.digest,
    )


def prepare_live_exam_pipeline(
    *,
    identity: RunIdentity,
    step_type: str,
    stage_number: int,
    entry_status_observation: LiveExamEntryStatusObservation,
    clean_state: LogicExamState,
    catalog: MasterPassiveCatalog,
    root: Path = DEFAULT_RUN_ROOT,
    database: Path = DEFAULT_DATABASE,
) -> LiveExamPipelineResult:
    """Publish, replay, bootstrap, and checkpoint one exact Exam entry.

    All expected runtime/data failures become blockers.  The function never
    captures a screen and never performs an input action; callers may expose a
    recommendation only when ``result.ready`` is true.
    """

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be RunIdentity")
    if not isinstance(entry_status_observation, LiveExamEntryStatusObservation):
        raise TypeError(
            "entry_status_observation must be LiveExamEntryStatusObservation"
        )
    if not isinstance(clean_state, LogicExamState):
        raise TypeError("clean_state must be LogicExamState")
    if not isinstance(catalog, MasterPassiveCatalog):
        raise TypeError("catalog must be MasterPassiveCatalog")
    root = Path(root)
    database = Path(database)
    paths = paths_for(identity, root=root)

    try:
        identity.validate()
        clean_state.validate()
    except (TypeError, ValueError) as error:
        return _result(
            identity=identity,
            blockers=(f"pipeline-input-invalid:{type(error).__name__}:{error}",),
        )

    try:
        existing_session = load_exam_session(paths.exam_session)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return _result(
            identity=identity,
            blockers=(f"exam-session-invalid:{type(error).__name__}:{error}",),
        )
    if existing_session is not None:
        legacy_blockers = existing_session.live_auto_click_blockers()
        if legacy_blockers:
            return _result(
                identity=identity,
                session=existing_session,
                blockers=legacy_blockers,
            )
        if not existing_session.matches_run(
            run_id=identity.run_id,
            idol_card_id=identity.idol_card_id,
            character_id=identity.character_id,
            produce_id=identity.produce_id,
        ):
            return _result(
                identity=identity,
                session=existing_session,
                blockers=("exam-session-run-identity-mismatch",),
            )
        same_requested_stage = (
            existing_session.step_type == step_type
            and existing_session.stage_number == stage_number
        )
        if same_requested_stage and existing_session.stage_completed:
            return _result(
                identity=identity,
                session=existing_session,
                blockers=("exam-session-stage-already-completed",),
            )
        if not same_requested_stage and not existing_session.stage_completed:
            return _result(
                identity=identity,
                session=existing_session,
                blockers=("exam-session-previous-stage-not-completed",),
            )

    (
        loadout,
        initial,
        contextual,
        equipped,
        deck,
        blockers,
    ) = _load_pipeline_artifacts(identity, root=root, database=database)
    partial = dict(
        identity=identity,
        loadout_snapshot=loadout,
        initial_reconciliation=initial,
        contextual_session=contextual,
        equipped_item_snapshot=equipped,
        deck_snapshot=deck,
        session=existing_session,
    )
    if blockers:
        return _result(**partial, blockers=blockers)
    assert (
        loadout is not None
        and initial is not None
        and contextual is not None
        and equipped is not None
        and deck is not None
    )

    try:
        context = derive_live_exam_entry_context(
            identity,
            snapshot_digest=loadout_snapshot_digest(loadout),
            step_type=step_type,
            stage_number=stage_number,
        )
    except (LiveExamContextError, OSError, TypeError, ValueError) as error:
        return _result(
            **partial,
            blockers=(f"exam-context-derivation-failed:{type(error).__name__}:{error}",),
        )
    partial["context"] = context

    if existing_session is not None and (
        existing_session.step_type != step_type
        or existing_session.stage_number != stage_number
    ):
        if (
            not context.passed_audition_stages
            or context.passed_audition_stages[-1] != existing_session.step_type
        ):
            return _result(
                **partial,
                blockers=("exam-session-stage-transition-not-next",),
            )

    matching_records = tuple(
        record for record in contextual.records if record.context_id == context.context_id
    )
    if matching_records and any(
        record.context_digest != context.digest for record in matching_records
    ):
        return _result(
            **partial,
            blockers=("contextual-record-context-digest-mismatch",),
        )

    try:
        publication = publish_loadout_preflight(
            identity=identity,
            loadout_snapshot=loadout,
            initial_reconciliation=initial,
            contextual_session=contextual,
            equipped_item_snapshot=equipped,
            context=context,
            entry_status_observation=entry_status_observation,
            stage_number=stage_number,
            catalog=catalog,
            root=root,
            database=database,
            deck_snapshot=deck,
        )
    except LiveExamPreflightBlockedError as error:
        return _result(
            **partial,
            blockers=tuple(f"preflight-publish:{reason}" for reason in error.reasons),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return _result(
            **partial,
            blockers=(f"preflight-publish-failed:{type(error).__name__}:{error}",),
        )
    partial["publication"] = publication
    # Publication may append the current contextual record; expose the exact
    # session that was committed, not the stale pre-publication input.
    partial["contextual_session"] = publication.contextual_session

    try:
        resolution = resolve_live_exam_preflight(
            identity=identity,
            context=context,
            stage_number=stage_number,
            catalog=catalog,
            root=root,
            database=database,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return _result(
            **partial,
            blockers=(f"preflight-resolve-failed:{type(error).__name__}:{error}",),
        )
    partial["preflight_resolution"] = resolution
    partial["merged_item_rule"] = resolution.merged_item_rule
    if not resolution.ready or resolution.bootstrap_status is None:
        return _result(
            **partial,
            blockers=resolution.blockers or ("preflight-resolution-not-ready",),
        )

    try:
        binding = exam_session_binding_from_preflight(context, resolution)
        expected_state, applied_digest = apply_preflight_bootstrap(
            clean_state,
            resolution.bootstrap_status,
            context_id=context.context_id,
            context_digest=context.digest,
        )
    except LiveExamPreflightBlockedError as error:
        return _result(
            **partial,
            blockers=tuple(f"preflight-bootstrap:{reason}" for reason in error.reasons),
        )
    except (TypeError, ValueError) as error:
        return _result(
            **partial,
            blockers=(f"preflight-bootstrap-failed:{type(error).__name__}:{error}",),
        )
    if applied_digest != binding.bootstrap_status_digest:
        return _result(
            **partial,
            blockers=("preflight-bootstrap-persistence-token-mismatch",),
        )
    partial["bootstrapped_state"] = expected_state

    if existing_session is not None:
        if existing_session.matches_preflight_binding(binding):
            if (
                existing_session.step_type != step_type
                or existing_session.stage_number != stage_number
            ):
                return _result(
                    **partial,
                    blockers=("exam-session-stage-binding-mismatch",),
                )
            resume_blockers = list(
                _state_base_mismatches(clean_state, existing_session.logic_state)
            )
            if existing_session.logic_state != expected_state:
                resume_blockers.append("exam-session-bootstrap-state-mismatch")
            if existing_session.transition_id != bootstrap_transition_id(binding):
                resume_blockers.append("exam-session-not-at-bootstrap-transition")
            if resume_blockers:
                return _result(**partial, blockers=resume_blockers)
            partial["session"] = existing_session
            return _result(**partial)

        if existing_session.bootstrap_consumed_context_id == context.context_id:
            return _result(
                **partial,
                blockers=("exam-session-context-already-consumed",),
            )
        if (
            existing_session.step_type == step_type
            and existing_session.stage_number == stage_number
        ):
            return _result(
                **partial,
                blockers=("exam-session-current-stage-binding-mismatch",),
            )
        expected_previous_transition_id = existing_session.transition_id
    else:
        expected_previous_transition_id = None

    transition_id = bootstrap_transition_id(binding)
    try:
        saved = save_exam_session(
            expected_state,
            idol_card_id=identity.idol_card_id,
            produce_id=identity.produce_id,
            step_type=step_type,
            stage_number=stage_number,
            run_id=identity.run_id,
            character_id=identity.character_id,
            preflight_binding=binding,
            bootstrap_consumed_context_id=context.context_id,
            transition_id=transition_id,
            expected_previous_transition_id=expected_previous_transition_id,
            path=paths.exam_session,
        )
    except ExamSessionConflictError as error:
        return _result(
            **partial,
            blockers=(
                "exam-session-cas-conflict:"
                f"expected={error.expected_previous_transition_id!r}:"
                f"actual={error.actual_previous_transition_id!r}",
            ),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return _result(
            **partial,
            blockers=(f"exam-session-save-failed:{type(error).__name__}:{error}",),
        )

    try:
        persisted = load_exam_session(paths.exam_session)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        return _result(
            **partial,
            blockers=(f"exam-session-round-trip-failed:{type(error).__name__}:{error}",),
        )
    if persisted != saved or persisted is None:
        return _result(
            **partial,
            blockers=("exam-session-round-trip-mismatch",),
        )
    if not persisted.matches_preflight_binding(binding):
        return _result(
            **partial,
            blockers=("exam-session-preflight-binding-round-trip-mismatch",),
        )
    if persisted.transition_id != transition_id:
        return _result(
            **partial,
            blockers=("exam-session-transition-id-round-trip-mismatch",),
        )
    partial["session"] = persisted
    return _result(**partial)


__all__ = [
    "LiveExamPipelineResult",
    "exam_session_binding_from_preflight",
    "prepare_live_exam_pipeline",
]
