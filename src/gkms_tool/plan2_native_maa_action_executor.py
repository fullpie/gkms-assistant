"""MAA-backed action adapter for the native Plan2 unattended loop.

The adapter accepts exactly one typed Plan2 action plus the settled
``ExamSaveData`` evidence on which that action was planned.  It binds the
ordered LocalSave Hand to fresh, reconciled PrintWindow frames before input,
submits one background action, and accepts the action only after two identical
reads of a changed settled LocalSave state.

No controller is created at import time.  Every capture, analyzer, click, and
LocalSave waiter is dependency injected; the small runtime helpers merely wire
the existing MAA/live-action primitives when a caller explicitly chooses to do
so.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
import time
import unicodedata
from typing import Any, Final

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
    LocalSaveExamCard,
    LocalSaveExamState,
    verified_turn_schedule_from_local_save,
)
from .audition_zone_reconciliation_live import (
    RECONCILIATION_SCREEN_SEMANTIC_SCHEMA_VERSION,
    ReconciliationScreenHandCard,
    ReconciliationScreenSemantic,
    screen_semantic_from_analysis,
    validate_screen_semantic_against_local_save,
    validate_stable_screen_pair_against_local_save,
)
from .audition_native_ordered_zones import NativeOrderedCardInstance
from .audition_horizon import VerifiedTurnFrame, VerifiedTurnSchedule
from .live_actions import (
    CardPlayExecutionResult,
    CardPlayFrameAnalysis,
    ClickExecutionResult,
    SuggestedClick,
    canonical_to_outer_window,
    execute_suggested_click,
    execute_verified_card_click,
)
from .native_runtime_stage_capture import default_native_runtime_recorder_path
from .runtime_action_state_evidence import RuntimeActionStateProvenance
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonError,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
)
from .plan2_card_history import (
    Plan2CompletedCardReplay,
    Plan2CompletedDrinkReplay,
    Plan2CompletedLogicalReplay,
)
from .plan2_native_evidence_semantics import (
    validate_plan2_native_drink_commit,
)
from .plan2_native_unattended_loop import (
    DecisionOrchestrator,
    ExamBoundaryDisposition,
    Plan2NativeActionExecution,
    Plan2NativeLoopIssue,
    Plan2NativeUnattendedDependencies,
    RecordSink,
    SettledEvidenceReader,
)


PLAN2_NATIVE_MAA_ACTION_EXECUTOR_SCHEMA_VERSION: Final = 1
_LEGACY_NIA_OUTER_SCOPE_ERROR: Final = (
    "N.I.A. recognition scope must be all, overview, or subpage"
)
CanonicalBox = tuple[int, int, int, int]
CommandSender = Callable[..., Mapping[str, Any]]
FrameAnalyzer = Callable[[Path], CardPlayFrameAnalysis]
ScreenAnalyzerFactory = Callable[[AuditionLocalSaveStateEvidence], FrameAnalyzer]
LogicalScreenAnalyzerFactory = Callable[
    [AuditionLocalSaveStateEvidence, Plan2NativeHorizonState], FrameAnalyzer
]
HandBoxDetector = Callable[[Mapping[str, object]], tuple[CanonicalBox, ...]]
ScreenBinder = Callable[
    [AuditionLocalSaveStateEvidence], "Plan2MaaScreenBinding"
]
PlayAnalyzerFactory = Callable[
    [
        Plan2NativeAction,
        AuditionLocalSaveStateEvidence,
        "Plan2MaaScreenBinding",
    ],
    FrameAnalyzer,
]
PlayDispatcher = Callable[
    [SuggestedClick, FrameAnalyzer], "Plan2MaaInputDispatch"
]
EndTurnTargetDetector = Callable[
    ["Plan2MaaScreenBinding"], SuggestedClick
]
EndTurnDispatcher = Callable[[SuggestedClick], "Plan2MaaInputDispatch"]
NextSettledEvidenceWaiter = Callable[
    [AuditionLocalSaveStateEvidence, Plan2NativeOfflineAction],
    "Plan2StableSettledEvidence",
]
LogicalScreenBinder = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeHorizonState,
        Plan2CompletedLogicalReplay,
    ],
    "Plan2MaaScreenBinding",
]
LogicalPlayAnalyzerFactory = Callable[
    [
        Plan2NativeAction,
        AuditionLocalSaveStateEvidence,
        "Plan2MaaScreenBinding",
        Plan2NativeHorizonState,
    ],
    FrameAnalyzer,
]
LogicalNextSettledEvidenceWaiter = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeOfflineAction,
        Plan2NativeHorizonState,
        Plan2CompletedLogicalReplay,
    ],
    "Plan2StableSettledEvidence",
]
RetainedSettlementWaiter = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2CompletedLogicalReplay,
    ],
    "Plan2StableSettledEvidence",
]


class Plan2InputStateAdvanced(RuntimeError):
    """The durable save advanced while Maa was deciding whether to retry input.

    This is not proof of success by itself.  It only forbids another click;
    the ordinary next-evidence validator must still prove the requested action.
    """

    def __init__(self, message: str, *, input_submitted: bool = False) -> None:
        super().__init__(message)
        if type(input_submitted) is not bool:
            raise TypeError("input_submitted must be bool")
        self.input_submitted = input_submitted


class Plan2SubmittedPhysicalSettlementError(RuntimeError):
    """A submitted input reached native evidence but failed settlement proof.

    Once a matching playing-card/command queue has been observed, another Maa
    click can only duplicate the already-owned transaction.  This error tells
    the executor to stop without entering its pre-commit recovery-click path.
    """


class Plan2SubmittedPhysicalSettlementTimeout(
    Plan2SubmittedPhysicalSettlementError
):
    """The exact submitted transaction stayed transitional until timeout."""


def _timeout_disposition(error: Exception) -> ExamBoundaryDisposition | None:
    return (
        ExamBoundaryDisposition.SUBMITTED_PENDING
        if isinstance(error, Plan2SubmittedPhysicalSettlementTimeout)
        else None
    )


DrinkDispatcher = Callable[
    [Plan2NativeDrinkAction, SuggestedClick],
    "Plan2MaaInputDispatch",
]
DrinkConfirmationEffectPolicy = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeDrinkAction,
        Plan2NativeHorizonState | None,
    ],
    bool,
]
PreInputAuthorizer = Callable[[Mapping[str, Any]], None]
PreDispatchSurfaceGate = Callable[
    [AuditionLocalSaveStateEvidence, PreInputAuthorizer | None],
    Mapping[str, object],
]
PlayInputAuthorizerFactory = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeHorizonState | None,
        Plan2CompletedLogicalReplay | None,
        RuntimeActionStateProvenance | None,
    ],
    PreInputAuthorizer,
]
DrinkInputSubmittedSink = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeDrinkAction,
        Mapping[str, object],
        Plan2CompletedDrinkReplay | None,
    ],
    None,
]
SettledEvidenceSink = Callable[[AuditionLocalSaveStateEvidence], None]
SubmittedDrinkReplayResolver = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeDrinkAction,
        Plan2NativeHorizonState,
        Plan2CompletedDrinkReplay,
        "Plan2MaaInputDispatch",
    ],
    Plan2CompletedDrinkReplay,
]
PostInputObservationSink = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeOfflineAction,
        Plan2NativeHorizonState | None,
        Plan2CompletedLogicalReplay | None,
        "Plan2MaaInputDispatch",
    ],
    Mapping[str, object] | None,
]


def _logical_turn_schedule_from_retained_evidence(
    evidence: AuditionLocalSaveStateEvidence,
) -> VerifiedTurnSchedule:
    state = evidence.state
    names = {
        1: "ProduceStepLessonType_LessonVocal",
        2: "ProduceStepLessonType_LessonDance",
        3: "ProduceStepLessonType_LessonVisual",
    }
    if state.exam_type == 0 and state.step_type_value in range(1, 10):
        if state.turn_parameter_types:
            raise ValueError("Plan2 lesson retained evidence has a schedule")
        parameter = ((state.step_type_value - 1) // 3) + 1
        frames = tuple(
            VerifiedTurnFrame(turn, names[parameter], 1000)
            for turn in range(
                state.current_turn,
                state.limit_turn + state.extra_turn + 1,
            )
        )
    elif state.exam_type == 1 and state.step_type_value in {16, 17, 18}:
        if len(state.turn_parameter_types) not in {
            state.limit_turn,
            state.limit_turn + state.extra_turn,
        }:
            raise ValueError("Plan2 audition retained schedule length is invalid")
        multipliers = {
            1: state.vocal_bonus_permille,
            2: state.dance_bonus_permille,
            3: state.visual_bonus_permille,
        }
        frames = tuple(
            VerifiedTurnFrame(
                turn,
                names[state.turn_parameter_types[turn - 1]],
                multipliers[state.turn_parameter_types[turn - 1]],
            )
            for turn in range(
                state.current_turn,
                state.limit_turn + state.extra_turn + 1,
            )
        )
    else:
        raise ValueError("retained evidence has an unsupported Plan2 stage")
    schedule = VerifiedTurnSchedule(
        source=(
            "exam-save-data-local-save-v1:retained-logical:"
            f"{evidence.run_id}:{evidence.source_sha256}"
        ),
        evidence_sha256=evidence.digest(),
        frames=frames,
    )
    schedule.validate()
    return schedule


def _box(value: object, label: str) -> CanonicalBox:
    if not isinstance(value, tuple) or len(value) != 4:
        raise TypeError(f"{label} must be an exact four-integer tuple")
    if any(not isinstance(item, int) or isinstance(item, bool) for item in value):
        raise TypeError(f"{label} must contain integers")
    left, top, right, bottom = value
    if not (0 <= left < right <= 720 and 0 <= top < bottom <= 1280):
        raise ValueError(f"{label} is outside the canonical client")
    return value


def _capture_path(capture: Mapping[str, object]) -> Path:
    value = capture.get("png_path")
    if not isinstance(value, str) or not value:
        raise ValueError("MAA capture has no PNG path")
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _suggestion_for_box(
    capture: Mapping[str, object],
    *,
    label: str,
    box: CanonicalBox,
) -> SuggestedClick:
    left, top, right, bottom = _box(box, "suggestion box")
    return SuggestedClick(
        label=label,
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=str(_capture_path(capture)),
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        click_count=1,
    )


def _validate_suggestion_binding(
    suggestion: SuggestedClick,
    capture: Mapping[str, object],
) -> None:
    if not isinstance(suggestion, SuggestedClick):
        raise TypeError("target detector returned an invalid SuggestedClick")
    expected = (
        str(capture["png_path"]),
        float(capture["timestamp"]),
        int(capture["hwnd"]),
        int(capture["pid"]),
    )
    actual = (
        suggestion.source_png_path,
        suggestion.source_timestamp,
        suggestion.source_hwnd,
        suggestion.source_pid,
    )
    if actual != expected:
        raise ValueError("SuggestedClick is not bound to the reconciled capture")
    if suggestion.click_count != 1:
        raise ValueError("action target must begin with exactly one click")


def _resolve_command_sender(sender: CommandSender | None) -> CommandSender:
    if sender is not None:
        return sender
    from .controller_client import send_command

    return send_command


@dataclass(frozen=True, slots=True)
class Plan2MaaActionIssue:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("issue code must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("issue detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Plan2MaaScreenBinding:
    """One fresh screen/LocalSave binding and its ordered visible Hand boxes."""

    capture: Mapping[str, object]
    semantic: ReconciliationScreenSemantic
    hand_boxes: tuple[CanonicalBox, ...]
    selected_slot: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capture, Mapping):
            raise TypeError("capture must be a mapping")
        for field in ("png_path", "timestamp", "hwnd", "pid"):
            if field not in self.capture:
                raise ValueError(f"capture is missing {field}")
        if not isinstance(self.semantic, ReconciliationScreenSemantic):
            raise TypeError("semantic must be ReconciliationScreenSemantic")
        boxes = tuple(self.hand_boxes)
        if len(boxes) != len(self.semantic.hand):
            raise ValueError("visible Hand boxes differ from reconciled Hand count")
        object.__setattr__(
            self,
            "hand_boxes",
            tuple(_box(value, f"hand_boxes[{index}]") for index, value in enumerate(boxes)),
        )
        if self.selected_slot is not None and (
            type(self.selected_slot) is not int
            or not 0 <= self.selected_slot < len(boxes)
        ):
            raise ValueError("selected_slot must identify one visible Hand box")


@dataclass(frozen=True, slots=True)
class Plan2StableSettledEvidence:
    """Two independent reads of one settled or proven transitional state."""

    first: AuditionLocalSaveStateEvidence
    second: AuditionLocalSaveStateEvidence
    completed_card_replay: Plan2CompletedLogicalReplay | None = None
    retained_action_replay: Plan2CompletedLogicalReplay | None = None
    runtime_provenance: RuntimeActionStateProvenance | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.first, AuditionLocalSaveStateEvidence):
            raise TypeError("first must be typed ExamSaveData evidence")
        if not isinstance(self.second, AuditionLocalSaveStateEvidence):
            raise TypeError("second must be typed ExamSaveData evidence")
        if self.completed_card_replay is not None and not isinstance(
            self.completed_card_replay,
            (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay),
        ):
            raise TypeError("completed replay must be typed or None")
        if self.retained_action_replay is not None and not isinstance(
            self.retained_action_replay,
            (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay),
        ):
            raise TypeError("retained action replay must be typed or None")
        if (
            self.completed_card_replay is not None
            and self.retained_action_replay is not None
        ):
            raise ValueError("stable evidence cannot carry two replay proofs")
        provenance = self.runtime_provenance
        if provenance is not None:
            if not isinstance(provenance, RuntimeActionStateProvenance):
                raise TypeError("runtime_provenance must be typed or None")
            runtime = self.second.state.root_runtime
            if self.first != self.second or not (
                self.second.state.is_native_actionable_settled
                or (runtime is not None and runtime.is_exam_end_complete)
            ):
                raise ValueError(
                    "runtime provenance requires one stable actionable or terminal state"
                )
            if (
                provenance.canonical_state_sha256 != self.second.source_sha256
                or provenance.canonical_state_size != self.second.source_size
                or provenance.zones_digest != self.second.zone_checkpoint_digest
            ):
                raise ValueError("runtime provenance does not bind stable evidence")


@dataclass(frozen=True, slots=True)
class Plan2MaaInputDispatch:
    """Whether a bounded background-input workflow was submitted."""

    submitted: bool
    detail: str = ""
    audit: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if type(self.submitted) is not bool:
            raise TypeError("submitted must be bool")
        if not isinstance(self.detail, str):
            raise TypeError("detail must be text")
        if self.audit is not None and not isinstance(self.audit, Mapping):
            raise TypeError("audit must be a mapping or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "submitted": self.submitted,
            "detail": self.detail,
            "audit": None if self.audit is None else dict(self.audit),
        }


@dataclass(frozen=True, slots=True)
class Plan2MaaActionExecutionResult:
    """Evidence-rich result; ``accepted`` always implies ``next_evidence``."""

    schema_version: int
    accepted: bool
    action: Plan2NativeOfflineAction
    before_evidence: AuditionLocalSaveStateEvidence
    next_evidence: AuditionLocalSaveStateEvidence | None
    shadow_before: AuditionLocalSaveStateEvidence | None
    shadow_after: AuditionLocalSaveStateEvidence | None
    hand_slot: int | None
    suggestion: SuggestedClick | None
    dispatch: Plan2MaaInputDispatch | None
    issues: tuple[Plan2MaaActionIssue, ...]
    trace: tuple[str, ...]
    completed_card_replay: Plan2CompletedLogicalReplay | None = None
    replan_evidence: AuditionLocalSaveStateEvidence | None = None
    boundary_disposition: ExamBoundaryDisposition | None = None
    runtime_provenance: RuntimeActionStateProvenance | None = None

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_MAA_ACTION_EXECUTOR_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 MAA action-executor schema")
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be bool")
        issues = tuple(self.issues)
        if any(not isinstance(value, Plan2MaaActionIssue) for value in issues):
            raise TypeError("issues must contain Plan2MaaActionIssue values")
        object.__setattr__(self, "issues", issues)
        if not isinstance(
            self.action, (Plan2NativeAction, Plan2NativeDrinkAction)
        ):
            raise TypeError("action must be a typed Plan2 offline action")
        if not isinstance(self.before_evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("before_evidence must be typed ExamSaveData evidence")
        if self.accepted != (self.next_evidence is not None and not self.issues):
            raise ValueError("accepted requires next evidence and no issues")
        if self.accepted and self.replan_evidence is not None:
            raise ValueError("accepted result cannot request a replan")
        if self.replan_evidence is not None and self.next_evidence is not None:
            raise ValueError("replan result cannot accept next evidence")
        if self.accepted and self.shadow_after != self.next_evidence:
            raise ValueError("accepted result must advance shadow to next evidence")
        expected_failed_shadow = (
            self.shadow_before
            if self.replan_evidence is None
            else self.replan_evidence
        )
        if not self.accepted and self.shadow_after != expected_failed_shadow:
            raise ValueError("failed result must preserve shadow evidence")
        if self.completed_card_replay is not None:
            replay = self.completed_card_replay
            if not self.accepted or replay.persisted_transition != self.next_evidence:
                raise ValueError("completed replay must bind the accepted next evidence")
            if replay.persisted_before != self.before_evidence:
                raise ValueError("completed replay must bind the before evidence")
            if replay.action != self.action:
                raise ValueError("completed replay must bind the submitted action")
        provenance = self.runtime_provenance
        if provenance is not None:
            if not isinstance(provenance, RuntimeActionStateProvenance):
                raise TypeError("runtime_provenance must be typed or None")
            if not self.accepted or self.next_evidence is None:
                raise ValueError("runtime provenance requires accepted next evidence")
            if (
                provenance.canonical_state_sha256
                != self.next_evidence.source_sha256
                or provenance.canonical_state_size != self.next_evidence.source_size
                or provenance.zones_digest
                != self.next_evidence.zone_checkpoint_digest
            ):
                raise ValueError("runtime provenance does not bind action evidence")
        disposition = self.boundary_disposition
        if disposition is None:
            next_state = None if self.next_evidence is None else self.next_evidence.state
            runtime = None if next_state is None else next_state.root_runtime
            disposition = (
                ExamBoundaryDisposition.TERMINAL
                if self.accepted and runtime is not None and runtime.is_exam_end_complete
                else ExamBoundaryDisposition.SUBMITTED_PENDING
                if self.accepted and (
                    self.completed_card_replay is not None
                    and self.next_evidence == self.before_evidence
                    or not next_state.is_native_actionable_settled
                )
                else ExamBoundaryDisposition.ACTIONABLE
                if self.accepted
                else ExamBoundaryDisposition.RETRYABLE_MOVING
                if self.replan_evidence is not None
                else ExamBoundaryDisposition.TRANSITIONAL
            )
        if not isinstance(disposition, ExamBoundaryDisposition):
            raise TypeError("boundary_disposition must be typed or None")
        if disposition is ExamBoundaryDisposition.TERMINAL and not self.accepted:
            raise ValueError("terminal disposition requires accepted execution")
        if disposition is ExamBoundaryDisposition.SUBMITTED_PENDING and (
            self.replan_evidence is not None
        ):
            raise ValueError("submitted pending result cannot request a replan")
        object.__setattr__(self, "boundary_disposition", disposition)

    @property
    def logical_after(self):
        return (
            None
            if self.completed_card_replay is None
            else self.completed_card_replay.logical_after
        )

    @property
    def detail(self) -> str:
        if self.issues:
            return "; ".join(
                value.code + (f":{value.detail}" if value.detail else "")
                for value in self.issues
            )
        return "accepted changed stable settled ExamSaveData evidence"

    def to_unattended_execution(self) -> Plan2NativeActionExecution:
        observation = None
        if self.dispatch is not None and isinstance(self.dispatch.audit, Mapping):
            raw = self.dispatch.audit.get("post_input_observation")
            if isinstance(raw, Mapping):
                observation = dict(raw)
        metadata = (
            {}
            if observation is not None or self.runtime_provenance is not None
            else None
        )
        if metadata is not None:
            if observation is not None:
                metadata["post_input_observation"] = observation
            if self.runtime_provenance is not None:
                metadata["runtime_provenance"] = self.runtime_provenance.to_dict()
        return Plan2NativeActionExecution(
            self.accepted,
            self.detail,
            replan=self.replan_evidence is not None,
            metadata=metadata,
            boundary_disposition=self.boundary_disposition,
            issues=tuple(
                Plan2NativeLoopIssue(value.code, value.detail)
                for value in self.issues
            ),
        )

    def to_dict(self) -> dict[str, object]:
        action = (
            {
                "kind": self.action.kind,
                "slot_index": self.action.slot_index,
                "instance_id": self.action.instance_id,
                "drink_id": self.action.drink_id,
                "selected_card_guid": self.action.selected_card_guid,
                "action_id": self.action.action_id,
            }
            if isinstance(self.action, Plan2NativeDrinkAction)
            else {
                "kind": self.action.kind,
                "card_guid": self.action.card_guid,
                "action_id": self.action.action_id,
            }
        )
        return {
            "schema_version": self.schema_version,
            "accepted": self.accepted,
            "boundary_disposition": self.boundary_disposition.value,
            "action": action,
            "before_evidence": self.before_evidence.to_dict(),
            "next_evidence": (
                None if self.next_evidence is None else self.next_evidence.to_dict()
            ),
            "shadow_before": (
                None if self.shadow_before is None else self.shadow_before.to_dict()
            ),
            "shadow_after": (
                None if self.shadow_after is None else self.shadow_after.to_dict()
            ),
            "hand_slot": self.hand_slot,
            "suggestion": (
                None if self.suggestion is None else self.suggestion.to_dict()
            ),
            "dispatch": None if self.dispatch is None else self.dispatch.to_dict(),
            "issues": [value.to_dict() for value in self.issues],
            "trace": list(self.trace),
            "completed_card_replay": (
                None
                if self.completed_card_replay is None
                else ({
                    "card_guid": self.completed_card_replay.card_guid,
                    "card_id": self.completed_card_replay.card_id,
                    "command_effect_ids": list(
                        self.completed_card_replay.command_effect_ids
                    ),
                    "remaining_plays": self.completed_card_replay.remaining_plays,
                    "rng_after": self.completed_card_replay.rng_after,
                } if isinstance(self.completed_card_replay, Plan2CompletedCardReplay)
                else {
                    "drink_id": self.completed_card_replay.action.drink_id,
                    "command_effect_ids": list(
                        self.completed_card_replay.command_effect_ids
                    ),
                    "materialized_from_local_save": (
                        self.completed_card_replay.materialized_from_local_save
                    ),
                    "commit_proof": (
                        None
                        if self.completed_card_replay.commit_proof is None
                        else self.completed_card_replay.commit_proof.to_dict()
                    ),
                })
            ),
            "replan_evidence": (
                None
                if self.replan_evidence is None
                else self.replan_evidence.to_dict()
            ),
            "runtime_provenance": (
                None
                if self.runtime_provenance is None
                else self.runtime_provenance.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class Plan2MaaBackgroundDependencies:
    screen_binder: ScreenBinder
    play_analyzer_factory: PlayAnalyzerFactory
    play_dispatcher: PlayDispatcher
    end_turn_target_detector: EndTurnTargetDetector
    end_turn_dispatcher: EndTurnDispatcher
    next_settled_evidence_waiter: NextSettledEvidenceWaiter
    drink_dispatcher: DrinkDispatcher | None = None
    logical_screen_binder: LogicalScreenBinder | None = None
    logical_play_analyzer_factory: LogicalPlayAnalyzerFactory | None = None
    logical_next_settled_evidence_waiter: (
        LogicalNextSettledEvidenceWaiter | None
    ) = None
    retained_settlement_waiter: RetainedSettlementWaiter | None = None
    pre_dispatch_surface_gate: PreDispatchSurfaceGate | None = None
    play_input_authorizer_factory: PlayInputAuthorizerFactory | None = None
    drink_confirmation_effect_policy: DrinkConfirmationEffectPolicy | None = None
    drink_input_submitted_sink: DrinkInputSubmittedSink | None = None
    settled_evidence_sink: SettledEvidenceSink | None = None
    submitted_drink_replay_resolver: SubmittedDrinkReplayResolver | None = None
    post_input_observation_sink: PostInputObservationSink | None = None

    def __post_init__(self) -> None:
        for name in (
            "screen_binder",
            "play_analyzer_factory",
            "play_dispatcher",
            "end_turn_target_detector",
            "end_turn_dispatcher",
            "next_settled_evidence_waiter",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        if self.drink_dispatcher is not None and not callable(
            self.drink_dispatcher
        ):
            raise TypeError("drink_dispatcher must be callable or None")
        for name in (
            "logical_screen_binder",
            "logical_play_analyzer_factory",
            "logical_next_settled_evidence_waiter",
            "retained_settlement_waiter",
            "pre_dispatch_surface_gate",
            "play_input_authorizer_factory",
            "drink_confirmation_effect_policy",
            "drink_input_submitted_sink",
            "settled_evidence_sink",
            "submitted_drink_replay_resolver",
            "post_input_observation_sink",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable or None")


def validate_screen_semantic_against_logical_horizon(
    semantic: ReconciliationScreenSemantic,
    evidence: AuditionLocalSaveStateEvidence,
    logical: Plan2NativeHorizonState,
    prior_replay: Plan2CompletedLogicalReplay,
) -> None:
    """Reconcile visible state without treating a retained queue as settled.

    The transitional save owns stage and the exact retained command identity.
    The replay-proven horizon owns HUD scalars, ordered visible Hand, and
    remaining plays because native drawing can materialize on screen while
    the save still exposes the pre-draw Hand.  This permits
    the visible zero-play/end-turn boundary without weakening
    ``LogicExamState``'s ordinary actionable-state invariant.
    """

    if not isinstance(semantic, ReconciliationScreenSemantic):
        raise TypeError("semantic must be ReconciliationScreenSemantic")
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be typed ExamSaveData evidence")
    if not isinstance(logical, Plan2NativeHorizonState):
        raise TypeError("logical must be Plan2NativeHorizonState")
    if not isinstance(
        prior_replay, (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay)
    ):
        raise TypeError("prior_replay must be a typed completed replay")
    state = evidence.state
    runtime = state.root_runtime
    card_replay_mismatch = isinstance(prior_replay, Plan2CompletedCardReplay) and (
        state.playing_card is None
        or state.playing_card.guid != prior_replay.card_guid
    )
    drink_replay_mismatch = isinstance(prior_replay, Plan2CompletedDrinkReplay) and (
        state.playing_card is not None
    )
    if (
        evidence != prior_replay.persisted_transition
        or logical != prior_replay.logical_after
        or runtime is None
        or runtime.command_list_is_empty
        or card_replay_mismatch
        or drink_replay_mismatch
        or logical.command_queue
        or logical.zones.pending_played is not None
    ):
        raise ValueError("logical screen authority does not bind the retained queue")
    persisted_hand = tuple(
        NativeOrderedCardInstance.from_local_save(card) for card in state.zones.hand
    )
    if isinstance(prior_replay, Plan2CompletedDrinkReplay):
        if tuple(card.guid for card in persisted_hand) != tuple(
            card.guid for card in logical.zones.hand
        ):
            raise ValueError("logical Hand GUIDs differ from retained drink queue")
    else:
        assert state.playing_card is not None
        prior_playing = NativeOrderedCardInstance.from_local_save(state.playing_card)
        settled_prior = tuple(
            card
            for card in (*logical.zones.grave, *logical.zones.lost)
            if card.guid == prior_playing.guid
        )
        if settled_prior != (prior_playing,):
            raise ValueError("prior playingCard is not settled exactly once logically")

    if state.exam_type == 0 and state.step_type_value in range(1, 10):
        multiplier = 1000
    elif state.exam_type == 1 and state.step_type_value in {16, 17, 18}:
        parameter = state.turn_parameter_types[logical.scalar.current_turn - 1]
        multiplier = {
            1: state.vocal_bonus_permille,
            2: state.dance_bonus_permille,
            3: state.visual_bonus_permille,
        }[parameter]
    else:
        raise ValueError("retained ExamSaveData has an unsupported stage identity")
    actual_hud = (
        semantic.turns_remaining,
        semantic.score,
        semantic.stamina,
        semantic.block,
        semantic.score_multiplier_permille,
    )
    expected_hud = (
        logical.remaining_turns,
        logical.scalar.score,
        logical.scalar.stamina,
        logical.scalar.block,
        multiplier,
    )
    if actual_hud != expected_hud:
        raise ValueError(f"Maa HUD differs from logical horizon: {actual_hud!r} != {expected_hud!r}")
    actual_hand = tuple(
        (card.card_id, card.effective_upgrade, card.support_marker_id)
        for card in semantic.hand
    )
    expected_hand = tuple(
        (
            card.card_id,
            card.effective_upgrade,
            card.support_upgrade_ids[0] if card.support_upgrade_ids else None,
        )
        for card in logical.zones.hand
    )
    expected_cards = logical.zones.hand
    if any(len(card.support_upgrade_ids) > 1 for card in expected_cards):
        raise ValueError("multiple support upgrades require richer screen proof")
    if actual_hand != expected_hand:
        raise ValueError("Maa Hand differs from replay-proven logical horizon")
    expected_schedule = tuple(
        (frame.round_number, frame.lesson_type, frame.score_multiplier_permille)
        for frame in _logical_turn_schedule_from_retained_evidence(evidence).frames
    )
    if semantic.remaining_schedule != expected_schedule:
        raise ValueError("Maa ring schedule differs from retained ExamSaveData")


def _state_is_settled_or_terminal(state: LocalSaveExamState) -> bool:
    runtime = state.root_runtime
    if runtime is None:
        return False
    # ``isExamEndComplete`` is the durable native terminal authority.  The
    # client may still retain presentation commands while it animates the
    # ranking transition; those commands cannot make another card action
    # legal and must not turn a completed exam into an action rejection.
    if runtime.is_exam_end_complete:
        return True
    # Keep one canonical actionable-state definition.  Duplicating its
    # playing/command/removed-card predicates here previously caused valid
    # positioned tombstones to be rejected by only the Maa acceptance layer.
    return state.is_native_actionable_settled


def _identity_continues(
    before: AuditionLocalSaveStateEvidence,
    after: AuditionLocalSaveStateEvidence,
) -> bool:
    before_state = before.state
    after_state = after.state
    added_turns = after_state.extra_turn - before_state.extra_turn
    schedule_continues = bool(
        added_turns >= 0
        and len(after_state.turn_parameter_types)
        == len(before_state.turn_parameter_types) + added_turns
        and after_state.turn_parameter_types[
            : len(before_state.turn_parameter_types)
        ]
        == before_state.turn_parameter_types
    )
    return (
        before.run_id == after.run_id
        and before.source_path == after.source_path
        and before.source_type == after.source_type
        and before_state.character_id == after_state.character_id
        and before_state.setting_id == after_state.setting_id
        and before_state.exam_type == after_state.exam_type
        and before_state.step_type_value == after_state.step_type_value
        and before_state.limit_turn == after_state.limit_turn
        and before_state.max_stamina == after_state.max_stamina
        and before_state.vocal_bonus_permille == after_state.vocal_bonus_permille
        and before_state.dance_bonus_permille == after_state.dance_bonus_permille
        and before_state.visual_bonus_permille == after_state.visual_bonus_permille
        and schedule_continues
    )


def _local_save_drink_ids(
    evidence: AuditionLocalSaveStateEvidence,
) -> tuple[str, ...]:
    runtime = evidence.state.root_runtime
    opaque = None if runtime is None else runtime.opaque_fields.to_value()
    rows = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("ExamSaveData has no ordered drinkList authority")
    result: list[str] = []
    for index, row in enumerate(rows):
        drink_id = row.get("_id") if isinstance(row, Mapping) else None
        if not isinstance(drink_id, str) or not drink_id:
            raise ValueError(f"ExamSaveData drinkList[{index}] has no identity")
        result.append(drink_id)
    return tuple(result)


def _state_progress_key(state: LocalSaveExamState) -> tuple[object, ...]:
    runtime = state.root_runtime
    opaque = None if runtime is None else runtime.opaque_fields.to_value()
    drink_rows = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
    drink_ids = tuple(
        row.get("_id") if isinstance(row, Mapping) else None
        for row in drink_rows
    ) if isinstance(drink_rows, list) else ()
    return (
        state.phase,
        state.current_turn,
        state.remain_turn,
        state.extra_turn,
        state.score,
        state.stamina,
        state.block,
        state.turn_card_play_count,
        state.exam_card_play_count,
        None if state.playing_card is None else state.playing_card.guid,
        tuple(card.guid for card in state.zones.hand),
        tuple(card.guid for card in state.zones.deck),
        tuple(card.guid for card in state.zones.grave),
        tuple(card.guid for card in state.zones.lost),
        tuple(card.guid for card in state.zones.hold),
        drink_ids,
        False if runtime is None else runtime.is_exam_end_complete,
    )


def _validate_stale_replan_evidence(
    before: AuditionLocalSaveStateEvidence,
    stable: Plan2StableSettledEvidence,
) -> AuditionLocalSaveStateEvidence:
    """Accept a newer stable state as a replan source, never as action proof."""

    if stable.first != stable.second:
        raise ValueError("replan ExamSaveData reads are not identical")
    after = stable.second
    if not _identity_continues(before, after):
        raise ValueError("replan ExamSaveData belongs to a different exam identity")
    if (
        before.state.step_type_value != after.state.step_type_value
        or before.state.limit_turn != after.state.limit_turn
        or before.state.turn_parameter_types != after.state.turn_parameter_types
    ):
        raise ValueError("replan ExamSaveData belongs to a different exam stage")
    if not _state_is_settled_or_terminal(after.state):
        raise ValueError("replan ExamSaveData is not stable settled or terminal")
    if _state_progress_key(before.state) == _state_progress_key(after.state):
        raise ValueError("replan ExamSaveData has no semantic progress")
    return after


def _exact_play_destination_proves_requested_guid(
    before_cards: Sequence[LocalSaveExamCard | NativeOrderedCardInstance],
    after: LocalSaveExamState,
    requested_guid: str,
    *,
    require_runtime_increment: bool,
    before_current_turn: int,
    before_exam_card_play_count: int,
) -> bool:
    before_matches = tuple(
        card for card in before_cards if card.guid == requested_guid
    )
    if len(before_matches) != 1:
        return False
    before_card = before_matches[0]
    destinations = tuple(
        (zone_name, card)
        for zone_name, cards in (
            ("hand", after.zones.hand),
            ("deck", after.zones.deck),
            ("grave", after.zones.grave),
            ("lost", after.zones.lost),
        )
        for card in cards
    )
    after_matches = tuple(
        (zone_name, card)
        for zone_name, card in destinations
        if card.guid == requested_guid
    )
    if len(after_matches) != 1:
        return False
    destination, after_card = after_matches[0]
    before_runtime = before_card.runtime_state
    after_runtime = after_card.runtime_state
    persistent_identity_matches = bool(
        before_card.card_id == after_card.card_id
        and before_card.base_upgrade == after_card.base_upgrade
        and before_card.fixed_deck_order == after_card.fixed_deck_order
    )
    if not persistent_identity_matches:
        return False
    runtime_incremented = bool(
        before_runtime is not None
        and after_runtime is not None
        and after_runtime.play_count > before_runtime.play_count
    )
    # A card can leave Hand merely because the turn naturally ended while a
    # previous card's animation was still resolving.  Destination, turn and
    # aggregate play counters therefore never prove this requested action.
    # Cards whose native runtime deliberately keeps play_count at zero (for
    # example support cards) are accepted only through an exact retained
    # playing-card command replay carried by ``retained_action_replay``.
    return runtime_incremented


def _validate_next_evidence(
    before: AuditionLocalSaveStateEvidence,
    action: Plan2NativeOfflineAction,
    stable: Plan2StableSettledEvidence,
    *,
    logical_before: Plan2NativeHorizonState | None = None,
    prior_replay: Plan2CompletedLogicalReplay | None = None,
    require_duplicate_play_proof: bool = False,
) -> tuple[AuditionLocalSaveStateEvidence, Plan2CompletedLogicalReplay | None]:
    if stable.first != stable.second:
        raise ValueError("next ExamSaveData reads are not identical")
    after = stable.second
    if not _identity_continues(before, after):
        raise ValueError("next ExamSaveData belongs to a different exam identity")
    replay = stable.completed_card_replay
    retained_action_replay = stable.retained_action_replay
    if replay is None:
        if not _state_is_settled_or_terminal(after.state):
            raise ValueError("next ExamSaveData is not stable settled or terminal")
    elif (
        replay.persisted_before != before
        or replay.persisted_transition != after
        or replay.action != action
    ):
        raise ValueError("completed card replay does not bind waiter inputs")
    if retained_action_replay is not None:
        if (
            retained_action_replay.persisted_before != before
            or retained_action_replay.action != action
            or retained_action_replay.persisted_transition == before
            or not _identity_continues(
                retained_action_replay.persisted_transition,
                after,
            )
        ):
            raise ValueError("retained action replay does not bind settlement")
        if not _state_is_settled_or_terminal(after.state):
            raise ValueError("retained action replay did not reach settlement")
    if isinstance(action, Plan2NativeDrinkAction):
        if replay is not None and not isinstance(replay, Plan2CompletedDrinkReplay):
            raise ValueError("drink use carried a non-drink completed replay")
        if (
            isinstance(replay, Plan2CompletedDrinkReplay)
            and not replay.provisional_from_submitted_receipt
        ):
            if replay.commit_proof is None:
                raise ValueError("completed DRINK has no typed semantic proof")
            validate_plan2_native_drink_commit(
                replay.commit_proof,
                before,
                after,
                action,
            )
            return after, replay
    if _state_progress_key(before.state) == _state_progress_key(after.state):
        raise ValueError("next ExamSaveData has no semantic progress")

    terminal = bool(
        after.state.root_runtime is not None
        and after.state.root_runtime.is_exam_end_complete
    )
    if isinstance(action, Plan2NativeDrinkAction):
        before_drinks = _local_save_drink_ids(before)
        after_drinks = _local_save_drink_ids(after)
        if not 0 <= action.slot_index < len(before_drinks):
            raise ValueError("drink slot was absent from before ExamSaveData")
        if before_drinks[action.slot_index] != action.drink_id:
            raise ValueError("drink ID differs from before ExamSaveData slot")
        expected = (
            before_drinks[: action.slot_index]
            + before_drinks[action.slot_index + 1 :]
        )
        if after_drinks != expected:
            raise ValueError("drink inventory did not remove the exact selected slot")
    elif action.kind == "play":
        if (
            isinstance(prior_replay, Plan2CompletedDrinkReplay)
            and prior_replay.provisional_from_submitted_receipt
        ):
            prior_action = prior_replay.action
            before_drinks = _local_save_drink_ids(before)
            after_drinks = _local_save_drink_ids(after)
            if (
                not 0 <= prior_action.slot_index < len(before_drinks)
                or before_drinks[prior_action.slot_index]
                != prior_action.drink_id
            ):
                raise ValueError(
                    "provisional drink slot differs from before ExamSaveData"
                )
            expected_drinks = (
                before_drinks[: prior_action.slot_index]
                + before_drinks[prior_action.slot_index + 1 :]
            )
            if after_drinks != expected_drinks:
                raise ValueError(
                    "provisional drink inventory did not materialize exact "
                    "ordered removal with PLAY"
                )
        # A retained HandGrave/draw queue can leave the durable ExamSave on
        # the old two-card Hand while its replay-proven logical horizon already
        # exposes newly drawn cards on screen.  For that continuation the
        # logical horizon—not the frozen pre-effect Save—is the action source.
        # A completed chained replay carries the same authority in ``before``.
        action_before = (
            replay.before
            if replay is not None
            else logical_before
        )
        before_cards = (
            before.state.zones.hand
            if action_before is None
            else action_before.zones.hand
        )
        before_current_turn = (
            before.state.current_turn
            if action_before is None
            else action_before.scalar.current_turn
        )
        before_exam_card_play_count = (
            before.state.exam_card_play_count
            if action_before is None
            else action_before.scalar.exam_card_play_count
        )
        before_hand = tuple(card.guid for card in before_cards)
        if action.card_guid not in before_hand:
            raise ValueError("played GUID was absent from the before Hand")
        play_progress = bool(
            retained_action_replay is not None
            or
            replay is not None
            or _exact_play_destination_proves_requested_guid(
                before_cards,
                after.state,
                action.card_guid,
                require_runtime_increment=require_duplicate_play_proof,
                before_current_turn=before_current_turn,
                before_exam_card_play_count=before_exam_card_play_count,
            )
        )
        if not play_progress:
            raise ValueError(
                "PLAY has no exact requested-GUID destination/runtime evidence"
            )
    else:
        turn_progress = bool(
            terminal
            or after.state.current_turn > before.state.current_turn
            or after.state.remain_turn < before.state.remain_turn
        )
        if not turn_progress:
            raise ValueError("END_TURN has no turn transition evidence")
    return after, replay


class Plan2MaaBackgroundActionExecutor:
    """Execute one action and advance shadow only after verified settlement."""

    def __init__(
        self,
        dependencies: Plan2MaaBackgroundDependencies,
        *,
        initial_shadow: AuditionLocalSaveStateEvidence | None = None,
    ) -> None:
        if not isinstance(dependencies, Plan2MaaBackgroundDependencies):
            raise TypeError("dependencies must be Plan2MaaBackgroundDependencies")
        if initial_shadow is not None and not isinstance(
            initial_shadow, AuditionLocalSaveStateEvidence
        ):
            raise TypeError("initial_shadow must be typed ExamSaveData evidence")
        self.dependencies = dependencies
        self._shadow_evidence = initial_shadow

    @property
    def shadow_evidence(self) -> AuditionLocalSaveStateEvidence | None:
        return self._shadow_evidence

    def _failed(
        self,
        *,
        action: Plan2NativeOfflineAction,
        before: AuditionLocalSaveStateEvidence,
        shadow_before: AuditionLocalSaveStateEvidence | None,
        code: str,
        detail: str,
        trace: list[str],
        hand_slot: int | None = None,
        suggestion: SuggestedClick | None = None,
        dispatch: Plan2MaaInputDispatch | None = None,
        boundary_disposition: ExamBoundaryDisposition | None = None,
    ) -> Plan2MaaActionExecutionResult:
        trace.append(f"failed:{code}")
        return Plan2MaaActionExecutionResult(
            schema_version=PLAN2_NATIVE_MAA_ACTION_EXECUTOR_SCHEMA_VERSION,
            accepted=False,
            action=action,
            before_evidence=before,
            next_evidence=None,
            shadow_before=shadow_before,
            shadow_after=shadow_before,
            hand_slot=hand_slot,
            suggestion=suggestion,
            dispatch=dispatch,
            issues=(Plan2MaaActionIssue(code, detail),),
            trace=tuple(trace),
            boundary_disposition=boundary_disposition,
        )

    def _replan(
        self,
        *,
        action: Plan2NativeOfflineAction,
        before: AuditionLocalSaveStateEvidence,
        shadow_before: AuditionLocalSaveStateEvidence | None,
        fresh: AuditionLocalSaveStateEvidence,
        detail: str,
        trace: list[str],
        hand_slot: int | None = None,
        suggestion: SuggestedClick | None = None,
        dispatch: Plan2MaaInputDispatch | None = None,
    ) -> Plan2MaaActionExecutionResult:
        trace.append("replan:input-state-advanced")
        self._shadow_evidence = fresh
        return Plan2MaaActionExecutionResult(
            schema_version=PLAN2_NATIVE_MAA_ACTION_EXECUTOR_SCHEMA_VERSION,
            accepted=False,
            action=action,
            before_evidence=before,
            next_evidence=None,
            shadow_before=shadow_before,
            shadow_after=fresh,
            hand_slot=hand_slot,
            suggestion=suggestion,
            dispatch=dispatch,
            issues=(
                Plan2MaaActionIssue("input-state-advanced-replan", detail),
            ),
            trace=tuple(trace),
            replan_evidence=fresh,
        )

    def execute(
        self,
        action: Plan2NativeOfflineAction,
        evidence: AuditionLocalSaveStateEvidence,
        *,
        logical_before: Plan2NativeHorizonState | None = None,
        prior_replay: Plan2CompletedLogicalReplay | None = None,
        runtime_before_provenance: RuntimeActionStateProvenance | None = None,
    ) -> Plan2MaaActionExecutionResult:
        if not isinstance(action, (Plan2NativeAction, Plan2NativeDrinkAction)):
            raise TypeError("action must be a typed Plan2 offline action")
        if not isinstance(evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("evidence must be typed ExamSaveData evidence")
        if logical_before is not None and not isinstance(
            logical_before, Plan2NativeHorizonState
        ):
            raise TypeError("logical_before must be typed or None")
        if prior_replay is not None and not isinstance(
            prior_replay, (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay)
        ):
            raise TypeError("prior_replay must be typed or None")
        if runtime_before_provenance is not None:
            if not isinstance(
                runtime_before_provenance,
                RuntimeActionStateProvenance,
            ):
                raise TypeError("runtime_before_provenance must be typed or None")
            if (
                runtime_before_provenance.canonical_state_sha256
                != evidence.source_sha256
                or runtime_before_provenance.canonical_state_size
                != evidence.source_size
                or runtime_before_provenance.zones_digest
                != evidence.zone_checkpoint_digest
            ):
                raise ValueError(
                    "runtime_before_provenance does not bind current evidence"
                )
        logical_continuation = logical_before is not None or prior_replay is not None
        if logical_continuation and (
            logical_before is None
            or prior_replay is None
            or prior_replay.persisted_transition != evidence
            or prior_replay.logical_after != logical_before
        ):
            raise ValueError("logical continuation authority is incomplete or mismatched")

        deps = self.dependencies
        shadow_before = self._shadow_evidence
        trace = ["input:typed-action-and-exam-save"]
        if (
            isinstance(prior_replay, Plan2CompletedDrinkReplay)
            and prior_replay.provisional_from_submitted_receipt
            and not (
                isinstance(action, Plan2NativeAction)
                and action.kind == "play"
            )
        ):
            return self._failed(
                action=action,
                before=evidence,
                shadow_before=shadow_before,
                code="provisional-drink-play-barrier-required",
                detail=(
                    "a submitted drink without its own LocalSave queue permits "
                    "exactly one PLAY reconciliation action"
                ),
                trace=trace,
            )
        if shadow_before is not None and evidence != shadow_before:
            return self._failed(
                action=action,
                before=evidence,
                shadow_before=shadow_before,
                code="stale-shadow-evidence",
                detail="supplied before evidence differs from executor shadow",
                trace=trace,
            )
        retained_surface_gate_owns_readiness = bool(
            logical_continuation
            and not _state_is_settled_or_terminal(evidence.state)
            and isinstance(
                deps.pre_dispatch_surface_gate,
                MaaExamActionSurfaceGate,
            )
        )
        if retained_surface_gate_owns_readiness:
            # Native ExamSave can keep the completed card command queue as a
            # durable retained transition after the visible round surface is
            # already actionable.  The exact replay owns the logical Hand;
            # the production Maa SkipRound gate below owns physical input
            # readiness and repeats the same CAS before any click.  Without
            # that concrete gate we keep the stricter settled-save barrier.
            trace.append("retained:maa-action-surface-readiness-required")
        elif logical_continuation and not _state_is_settled_or_terminal(
            evidence.state
        ):
            if deps.retained_settlement_waiter is None:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="retained-physical-settlement-required",
                    detail=(
                        "a replay-proven command queue must physically settle "
                        "before another PLAY or DRINK can be submitted"
                    ),
                    trace=trace,
                )
            assert prior_replay is not None
            try:
                settled = deps.retained_settlement_waiter(
                    evidence,
                    prior_replay,
                )
                if settled.first != settled.second:
                    raise ValueError(
                        "retained settlement reads are not identical"
                    )
                fresh = settled.second
                if not _identity_continues(evidence, fresh):
                    raise ValueError(
                        "retained settlement belongs to a different exam identity"
                    )
                if (
                    evidence.state.step_type_value != fresh.state.step_type_value
                    or evidence.state.limit_turn != fresh.state.limit_turn
                    or evidence.state.turn_parameter_types
                    != fresh.state.turn_parameter_types
                ):
                    raise ValueError(
                        "retained settlement belongs to a different exam stage"
                    )
                if not _state_is_settled_or_terminal(fresh.state):
                    raise ValueError(
                        "retained command queue did not physically settle"
                    )
                if _state_progress_key(evidence.state) == _state_progress_key(
                    fresh.state
                ):
                    raise ValueError(
                        "retained command queue produced no semantic progress"
                    )
            except Exception as error:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="retained-physical-settlement-failed",
                    detail=f"{type(error).__name__}:{error}",
                    trace=trace,
                )
            return self._replan(
                action=action,
                before=evidence,
                shadow_before=shadow_before,
                fresh=fresh,
                detail=(
                    "retained command queue physically settled; requested "
                    "next action was not submitted and must be replanned"
                ),
                trace=trace,
            )
        if not evidence.state.is_native_actionable_settled and not logical_continuation:
            return self._failed(
                action=action,
                before=evidence,
                shadow_before=shadow_before,
                code="before-not-actionable-settled",
                detail="ExamSaveData is not an idle native Main state",
                trace=trace,
            )

        hand_authority = (
            logical_before.zones.hand
            if logical_continuation and logical_before is not None
            else evidence.state.zones.hand
        )
        expected_play_slot = None
        if isinstance(action, Plan2NativeDrinkAction):
            drink_authority_error: str | None = None
            if logical_continuation:
                assert logical_before is not None
                try:
                    raw_drink_ids = _local_save_drink_ids(evidence)
                except ValueError as error:
                    return self._failed(
                        action=action,
                        before=evidence,
                        shadow_before=shadow_before,
                        code="drink-input-authority-unavailable",
                        detail=str(error),
                        trace=trace,
                    )
                try:
                    logical_before.drink_runtime.resolve(
                        action.slot_index,
                        instance_id=action.instance_id,
                        drink_id=action.drink_id,
                    )
                except Plan2NativeHorizonError as error:
                    drink_authority_error = str(error)
                drink_ids = tuple(
                    value.drink_id
                    for value in logical_before.drink_runtime.inventory
                )
                if raw_drink_ids != drink_ids:
                    drink_authority_error = (
                        "retained LocalSave inventory has not materialized "
                        f"the logical drink removal: raw={raw_drink_ids!r}"
                    )
            else:
                try:
                    drink_ids = _local_save_drink_ids(evidence)
                except ValueError as error:
                    return self._failed(
                        action=action,
                        before=evidence,
                        shadow_before=shadow_before,
                        code="drink-input-authority-unavailable",
                        detail=str(error),
                        trace=trace,
                    )
                expected_instance_id = (
                    f"localsave-drink:{action.slot_index}:{action.drink_id}"
                )
                if (
                    not 0 <= action.slot_index < len(drink_ids)
                    or drink_ids[action.slot_index] != action.drink_id
                    or action.instance_id != expected_instance_id
                ):
                    drink_authority_error = "LocalSave drink slot mismatch"
            if drink_authority_error is not None or action.selected_card_guid:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="drink-input-authority-mismatch",
                    detail=(
                        f"slot={action.slot_index};id={action.drink_id};"
                        f"instance={action.instance_id};inventory={drink_ids!r};"
                        f"authority={drink_authority_error or 'selected-card-mismatch'}"
                    ),
                    trace=trace,
                )
        elif action.kind == "play":
            expected_play_slot = next(
                (
                    slot
                    for slot, card in enumerate(hand_authority)
                    if card.guid == action.card_guid
                ),
                None,
            )
            if expected_play_slot is None:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="input-dispatch-failed",
                    detail="PLAY GUID is absent from the ordered Hand",
                    trace=trace,
                )

        settled_waiter = (
            deps.logical_next_settled_evidence_waiter
            if logical_continuation
            else deps.next_settled_evidence_waiter
        )
        waiter_owner = getattr(settled_waiter, "__self__", None)
        bind_runtime_before = getattr(
            waiter_owner,
            "bind_runtime_before_provenance",
            None,
        )
        if callable(bind_runtime_before):
            bind_runtime_before(runtime_before_provenance)
        begin_runtime_receipt = getattr(
            waiter_owner,
            "begin_runtime_action_receipt",
            None,
        )
        if callable(begin_runtime_receipt):
            begin_runtime_receipt(action, expected_play_slot)

        def read_next_stable() -> Plan2StableSettledEvidence:
            if logical_continuation:
                if settled_waiter is None:
                    raise ValueError("logical settled-evidence waiter is not configured")
                assert logical_before is not None and prior_replay is not None
                observed = settled_waiter(
                    evidence, action, logical_before, prior_replay
                )
            else:
                assert settled_waiter is not None
                observed = settled_waiter(evidence, action)
            if not isinstance(observed, Plan2StableSettledEvidence):
                raise TypeError("settled waiter returned an invalid evidence pair")
            return observed

        observed_runtime_provenance: RuntimeActionStateProvenance | None = None

        def validate_observed(
            observed: Plan2StableSettledEvidence,
            *,
            require_duplicate_play_proof: bool = False,
        ) -> tuple[
            AuditionLocalSaveStateEvidence,
            Plan2CompletedLogicalReplay | None,
        ]:
            nonlocal observed_runtime_provenance
            validated = _validate_next_evidence(
                evidence,
                action,
                observed,
                logical_before=(logical_before if logical_continuation else None),
                prior_replay=(prior_replay if logical_continuation else None),
                require_duplicate_play_proof=require_duplicate_play_proof,
            )
            observed_runtime_provenance = observed.runtime_provenance
            return validated

        def wait_for_next() -> tuple[
            AuditionLocalSaveStateEvidence,
            Plan2CompletedLogicalReplay | None,
        ]:
            return validate_observed(read_next_stable())

        def resolve_advanced(
            advanced: Plan2InputStateAdvanced,
        ) -> tuple[
            tuple[
                AuditionLocalSaveStateEvidence,
                Plan2CompletedLogicalReplay | None,
            ]
            | None,
            AuditionLocalSaveStateEvidence | None,
            str,
        ]:
            observed = read_next_stable()
            try:
                return (
                    validate_observed(
                        observed,
                        require_duplicate_play_proof=True,
                    ),
                    None,
                    "",
                )
            except Exception as action_mismatch:
                if not advanced.input_submitted:
                    fresh = _validate_stale_replan_evidence(evidence, observed)
                    return (
                        None,
                        fresh,
                        f"{type(advanced).__name__}:{advanced}; "
                        "requested action was never submitted and is unproven: "
                        f"{type(action_mismatch).__name__}:{action_mismatch}",
                    )
                # A dispatcher may already have submitted SELECT/click.  A
                # different progressed state is not an ordinary replan
                # boundary: prove this requested action or stop before any
                # further input.  The pre-dispatch surface gate retains its
                # separate, zero-input replan path.
                _validate_stale_replan_evidence(evidence, observed)
                raise ValueError(
                    "requested-action-proof-mismatch:"
                    f"{type(advanced).__name__}:{advanced};"
                    f"{type(action_mismatch).__name__}:{action_mismatch}"
                )

        hand_slot: int | None = None
        suggestion: SuggestedClick | None = None
        authorizer = (
            None
            if deps.play_input_authorizer_factory is None
            else deps.play_input_authorizer_factory(
                evidence,
                logical_before,
                prior_replay,
                runtime_before_provenance,
            )
        )
        native_action_settlement_cursor: Mapping[str, object] | None = None

        def snapshot_native_cursor(capture: Mapping[str, object] | None) -> None:
            nonlocal native_action_settlement_cursor
            if native_action_settlement_cursor is not None or capture is None:
                return
            pid = capture.get("pid")
            if type(pid) is int and pid > 0:
                native_action_settlement_cursor = (
                    _snapshot_native_action_settlement_cursor(pid)
                )

        def attach_native_cursor(
            receipt: Plan2MaaInputDispatch,
        ) -> Plan2MaaInputDispatch:
            if native_action_settlement_cursor is None:
                return receipt
            audit = {} if receipt.audit is None else dict(receipt.audit)
            if not isinstance(audit.get("native_action_settlement"), Mapping):
                audit["native_action_settlement"] = dict(
                    native_action_settlement_cursor
                )
                return replace(receipt, audit=audit)
            return receipt

        def bind_submitted_action(receipt: Plan2MaaInputDispatch) -> None:
            bind_dispatch = getattr(
                waiter_owner,
                "bind_submitted_action_dispatch_witness",
                None,
            )
            if callable(bind_dispatch):
                bind_dispatch(
                    receipt.to_dict(),
                    action,
                    expected_play_slot,
                    logical_before if logical_continuation else None,
                    prior_replay if logical_continuation else None,
                )
                return
            if action.kind == "play":
                legacy_bind = getattr(
                    waiter_owner,
                    "bind_submitted_play_dispatch_witness",
                    None,
                )
                if callable(legacy_bind):
                    assert expected_play_slot is not None
                    legacy_bind(
                        receipt.to_dict(),
                        expected_play_slot,
                        logical_before if logical_continuation else None,
                        prior_replay if logical_continuation else None,
                    )

        gate_capture: Mapping[str, object] | None = None
        if deps.pre_dispatch_surface_gate is not None:
            try:
                gate_capture = deps.pre_dispatch_surface_gate(
                    evidence,
                    authorizer,
                )
                if not isinstance(gate_capture, Mapping):
                    raise TypeError("pre-dispatch surface gate returned invalid capture")
                snapshot_native_cursor(gate_capture)
                trace.append("screen:maa-actionable-round-gate")
            except Plan2InputStateAdvanced as advanced:
                try:
                    observed = read_next_stable()
                    fresh = _validate_stale_replan_evidence(evidence, observed)
                except Exception as error:
                    return self._failed(
                        action=action,
                        before=evidence,
                        shadow_before=shadow_before,
                        code="surface-gate-state-advanced-evidence-failed",
                        detail=f"{type(error).__name__}:{error}",
                        trace=trace,
                    )
                return self._replan(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    fresh=fresh,
                    detail=(
                        f"{type(advanced).__name__}:{advanced}; requested "
                        "action was not submitted"
                    ),
                    trace=trace,
                )
            except Exception as error:
                trace.append("screen:maa-actionable-round-not-ready")
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="pre-dispatch-surface-not-ready",
                    detail=f"{type(error).__name__}:{error}",
                    trace=trace,
                )

        # END_TURN does not depend on card artwork or visible Hand boxes.
        # ExamSave proves the action boundary and the CAS authorizer owns the
        # input identity.  Let the production dispatcher inspect the current
        # Maa frame itself so a confirmation modal left by an earlier process
        # can be resumed without first trying to see cards behind it.
        if action.kind == "end_turn" and isinstance(
            deps.end_turn_dispatcher,
            MaaExistingEndTurnDispatcher,
        ):
            prevalidated_next: tuple[
                AuditionLocalSaveStateEvidence,
                Plan2CompletedLogicalReplay | None,
            ] | None = None
            try:
                dispatch = deps.end_turn_dispatcher.dispatch_current(
                    pre_input_authorizer=authorizer,
                )
                trace.append("dispatch:end-turn-current-screen")
            except Plan2InputStateAdvanced as advanced:
                try:
                    prevalidated_next, fresh, mismatch = resolve_advanced(advanced)
                except Exception as error:
                    return self._failed(
                        action=action,
                        before=evidence,
                        shadow_before=shadow_before,
                        code="state-advanced-evidence-failed",
                        detail=f"{type(error).__name__}:{error}",
                        trace=trace,
                    )
                if fresh is not None:
                    return self._replan(
                        action=action,
                        before=evidence,
                        shadow_before=shadow_before,
                        fresh=fresh,
                        detail=mismatch,
                        trace=trace,
                        dispatch=Plan2MaaInputDispatch(
                            False,
                            "ExamSave advanced before END_TURN; action unproven",
                            {
                                "mode": "maa-examsave-advanced-replan",
                                "detail": str(advanced),
                            },
                        ),
                    )
                assert prevalidated_next is not None
                dispatch = Plan2MaaInputDispatch(
                    True,
                    "ExamSave exactly proves END_TURN before duplicate input",
                    {
                        "mode": "maa-examsave-advanced-during-end-turn-input",
                        "detail": str(advanced),
                    },
                )
                trace.append("dispatch:end-turn-state-advanced-no-retry")
            except Exception as error:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="input-dispatch-failed",
                    detail=f"{type(error).__name__}:{error}",
                    trace=trace,
                )
            if not dispatch.submitted:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="input-not-submitted",
                    detail=dispatch.detail,
                    trace=trace,
                    dispatch=dispatch,
                )
            if prevalidated_next is None:
                dispatch = attach_native_cursor(dispatch)
                bind_submitted_action(dispatch)
            if (
                isinstance(deps.pre_dispatch_surface_gate, MaaExamActionSurfaceGate)
                and not str(
                    dispatch.audit.get("mode", "")
                    if isinstance(dispatch.audit, Mapping)
                    else ""
                ).startswith(
                    "maa-examsave-advanced"
                )
            ):
                deps.pre_dispatch_surface_gate.mark_action_submitted()
            try:
                after, completed_replay = (
                    wait_for_next()
                    if prevalidated_next is None
                    else prevalidated_next
                )
            except Exception as error:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="next-settled-evidence-failed",
                    detail=f"{type(error).__name__}:{error}",
                    trace=trace,
                    dispatch=dispatch,
                )
            trace.append("accepted:changed-stable-settled-exam-save")
            self._shadow_evidence = after
            if deps.settled_evidence_sink is not None:
                try:
                    deps.settled_evidence_sink(after)
                    trace.append("receipt:pending-drink-cleared")
                except Exception as error:
                    trace.append(
                        "receipt:pending-drink-clear-failed:"
                        f"{type(error).__name__}:{error}"
                    )
            return Plan2MaaActionExecutionResult(
                schema_version=PLAN2_NATIVE_MAA_ACTION_EXECUTOR_SCHEMA_VERSION,
                accepted=True,
                action=action,
                before_evidence=evidence,
                next_evidence=after,
                shadow_before=shadow_before,
                shadow_after=after,
                hand_slot=None,
                suggestion=None,
                dispatch=dispatch,
                issues=(),
                trace=tuple(trace),
                completed_card_replay=completed_replay,
                runtime_provenance=observed_runtime_provenance,
            )

        binding: Plan2MaaScreenBinding | None = None
        action_capture: Mapping[str, object] | None = None
        production_drink = bool(
            isinstance(action, Plan2NativeDrinkAction)
            and isinstance(deps.drink_dispatcher, MaaExistingDrinkDispatcher)
        )
        try:
            if production_drink:
                if gate_capture is None:
                    assert isinstance(
                        deps.drink_dispatcher, MaaExistingDrinkDispatcher
                    )
                    action_capture = dict(
                        _resolve_command_sender(
                            deps.drink_dispatcher.command_sender
                        )("capture_screen_once", timeout=15.0)
                    )
                    _capture_path(action_capture)
                    trace.append("screen:maa-drink-fresh-capture")
                else:
                    action_capture = gate_capture
                    _capture_path(action_capture)
                    trace.append("screen:maa-drink-actionable-capture")
            elif logical_continuation:
                if deps.logical_screen_binder is None:
                    raise ValueError("logical screen binder is not configured")
                assert logical_before is not None and prior_replay is not None
                if isinstance(
                    deps.logical_screen_binder, MaaExamSaveSlotScreenBinder
                ):
                    binding = deps.logical_screen_binder.bind_capture(
                        evidence,
                        (
                            gate_capture
                            if gate_capture is not None
                            else dict(
                                _resolve_command_sender(
                                    deps.logical_screen_binder.command_sender
                                )("capture_screen_once", timeout=15.0)
                            )
                        ),
                        logical=logical_before,
                        prior_replay=prior_replay,
                        expected_slot=expected_play_slot,
                    )
                else:
                    binding = deps.logical_screen_binder(
                        evidence, logical_before, prior_replay
                    )
                if not isinstance(
                    deps.logical_screen_binder, MaaExamSaveSlotScreenBinder
                ):
                    validate_screen_semantic_against_logical_horizon(
                        binding.semantic,
                        evidence,
                        logical_before,
                        prior_replay,
                    )
            else:
                if isinstance(deps.screen_binder, MaaExamSaveSlotScreenBinder):
                    binding = deps.screen_binder.bind_capture(
                        evidence,
                        (
                            gate_capture
                            if gate_capture is not None
                            else dict(
                                _resolve_command_sender(
                                    deps.screen_binder.command_sender
                                )("capture_screen_once", timeout=15.0)
                            )
                        ),
                        expected_slot=expected_play_slot,
                    )
                else:
                    binding = deps.screen_binder(evidence)
                if not isinstance(deps.screen_binder, MaaExamSaveSlotScreenBinder):
                    validate_screen_semantic_against_local_save(
                        binding.semantic, evidence
                    )
        except Exception as error:
            return self._failed(
                action=action,
                before=evidence,
                shadow_before=shadow_before,
                code="screen-reconciliation-failed",
                detail=f"{type(error).__name__}:{error}",
                trace=trace,
            )
        if not production_drink:
            trace.append(
                "screen:maa-card-slots-bound"
                if isinstance(binding, Plan2MaaScreenBinding)
                and isinstance(deps.screen_binder, MaaExamSaveSlotScreenBinder)
                else "screen:stable-reconciled-printwindow"
            )

        prevalidated_next = None
        try:
            if isinstance(action, Plan2NativeDrinkAction):
                if deps.drink_dispatcher is None:
                    raise ValueError("Plan2 drink dispatcher is not configured")
                from .plan3_audition_executor import DRINK_SLOT_BOXES

                if production_drink:
                    if action_capture is None:
                        raise ValueError("Plan2 drink capture is unavailable")
                    capture = action_capture
                else:
                    if binding is None:
                        raise ValueError("Plan2 drink screen binding is unavailable")
                    capture = binding.capture
                suggestion = _suggestion_for_box(
                    capture,
                    label=f"Plan2 native DRINK slot {action.slot_index}",
                    box=DRINK_SLOT_BOXES[action.slot_index],
                )
                snapshot_native_cursor(capture)
                confirmation_has_effect = None
                if deps.drink_confirmation_effect_policy is not None:
                    confirmation_has_effect = deps.drink_confirmation_effect_policy(
                        evidence,
                        action,
                        logical_before,
                    )
                    if type(confirmation_has_effect) is not bool:
                        raise TypeError(
                            "drink confirmation effect policy must return bool"
                        )
                if isinstance(deps.drink_dispatcher, MaaExistingDrinkDispatcher):
                    dispatch = deps.drink_dispatcher(
                        action,
                        suggestion,
                        pre_input_authorizer=authorizer,
                        confirmation_has_effect=confirmation_has_effect,
                    )
                else:
                    dispatch = deps.drink_dispatcher(action, suggestion)
                trace.append(f"dispatch:drink-slot-{action.slot_index}")
            elif action.kind == "play":
                if binding is None:
                    raise ValueError("Plan2 PLAY screen binding is unavailable")
                assert expected_play_slot is not None
                hand_slot = expected_play_slot
                suggestion = _suggestion_for_box(
                    binding.capture,
                    label=f"Plan2 native PLAY slot {hand_slot}",
                    box=binding.hand_boxes[hand_slot],
                )
                if isinstance(deps.play_dispatcher, MaaExamSaveCardPlayDispatcher):
                    # The production Plan2 dispatcher deliberately does not
                    # consume screenshot OCR or a second solver pass.  The
                    # fixed slot came from the fresh MAA capture; ExamSave CAS
                    # below owns GUID/slot identity.
                    analyzer = lambda _path: CardPlayFrameAnalysis(
                        phase="failed",
                        issues=("unused-by-maa-examsave-minimal",),
                    )
                elif logical_continuation:
                    if deps.logical_play_analyzer_factory is None:
                        raise ValueError("logical PLAY analyzer is not configured")
                    assert logical_before is not None
                    analyzer = deps.logical_play_analyzer_factory(
                        action, evidence, binding, logical_before
                    )
                else:
                    analyzer = deps.play_analyzer_factory(action, evidence, binding)
                if authorizer is None:
                    dispatch = deps.play_dispatcher(suggestion, analyzer)
                elif (
                    isinstance(deps.play_dispatcher, MaaExamSaveCardPlayDispatcher)
                    and binding.selected_slot == hand_slot
                ):
                    dispatch = deps.play_dispatcher.submit_single_recovery_click(
                        suggestion,
                        expected_slot=hand_slot,
                        expected_hand_count=len(hand_authority),
                        pre_input_authorizer=authorizer,
                    )
                    trace.append("dispatch:resume-selected-card-confirmation")
                elif isinstance(deps.play_dispatcher, MaaVerifiedCardPlayDispatcher):
                    dispatch = deps.play_dispatcher(
                        suggestion,
                        analyzer,
                        pre_input_authorizer=authorizer,
                    )
                else:
                    raise TypeError(
                        "configured PLAY input authorizer requires the verified "
                        "MAA dispatcher"
                    )
                trace.append(f"dispatch:play-slot-{hand_slot}")
            else:
                suggestion = deps.end_turn_target_detector(binding)
                _validate_suggestion_binding(suggestion, binding.capture)
                if isinstance(
                    deps.end_turn_dispatcher,
                    MaaExistingEndTurnDispatcher,
                ):
                    dispatch = deps.end_turn_dispatcher(
                        suggestion,
                        pre_input_authorizer=authorizer,
                    )
                else:
                    dispatch = deps.end_turn_dispatcher(suggestion)
                trace.append("dispatch:end-turn-detected-button")
            if not isinstance(dispatch, Plan2MaaInputDispatch):
                raise TypeError("dispatcher returned an invalid receipt")
        except Plan2InputStateAdvanced as advanced:
            try:
                prevalidated_next, fresh, mismatch = resolve_advanced(advanced)
            except Exception as error:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="state-advanced-evidence-failed",
                    detail=f"{type(error).__name__}:{error}",
                    trace=trace,
                    hand_slot=hand_slot,
                    suggestion=suggestion,
                )
            if fresh is not None:
                return self._replan(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    fresh=fresh,
                    detail=mismatch,
                    trace=trace,
                    hand_slot=hand_slot,
                    suggestion=suggestion,
                    dispatch=Plan2MaaInputDispatch(
                        False,
                        "ExamSave advanced before requested action; action unproven",
                        {
                            "mode": "maa-examsave-advanced-replan",
                            "detail": str(advanced),
                        },
                    ),
                )
            assert prevalidated_next is not None
            dispatch = Plan2MaaInputDispatch(
                True,
                "ExamSave exactly proves requested action; duplicate click suppressed",
                {
                    "mode": "maa-examsave-advanced-during-input",
                    "detail": str(advanced),
                },
            )
            trace.append("dispatch:state-advanced-no-retry")
        except Exception as error:
            return self._failed(
                action=action,
                before=evidence,
                shadow_before=shadow_before,
                code="input-dispatch-failed",
                detail=f"{type(error).__name__}:{error}",
                trace=trace,
                hand_slot=hand_slot,
                suggestion=suggestion,
            )
        if not dispatch.submitted:
            audit = dispatch.audit
            if (
                isinstance(action, Plan2NativeDrinkAction)
                and isinstance(audit, Mapping)
                and audit.get("mode")
                in {
                    "maa-examsave-drink-noop-cancelled",
                    "maa-examsave-drink-dialog-cancelled",
                }
            ):
                return self._replan(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    fresh=evidence,
                    detail=dispatch.detail,
                    trace=trace,
                    hand_slot=hand_slot,
                    suggestion=suggestion,
                    dispatch=dispatch,
                )
            return self._failed(
                action=action,
                before=evidence,
                shadow_before=shadow_before,
                code="input-not-submitted",
                detail=dispatch.detail,
                trace=trace,
                hand_slot=hand_slot,
                suggestion=suggestion,
                dispatch=dispatch,
            )
        if prevalidated_next is None:
            dispatch = attach_native_cursor(dispatch)
            bind_submitted_action(dispatch)
        if (
            isinstance(deps.pre_dispatch_surface_gate, MaaExamActionSurfaceGate)
            and not str(
                dispatch.audit.get("mode", "")
                if isinstance(dispatch.audit, Mapping)
                else ""
            ).startswith(
                "maa-examsave-advanced"
            )
        ):
            deps.pre_dispatch_surface_gate.mark_action_submitted()
        if (
            isinstance(action, Plan2NativeDrinkAction)
            and prevalidated_next is None
            and deps.drink_input_submitted_sink is not None
        ):
            try:
                deps.drink_input_submitted_sink(
                    evidence,
                    action,
                    dispatch.to_dict(),
                    (
                        prior_replay
                        if isinstance(prior_replay, Plan2CompletedDrinkReplay)
                        else None
                    ),
                )
            except Exception as error:
                # Maa has already submitted the irreversible input.  Stop
                # without waiting or retrying when its durable receipt cannot
                # be written; a later process must never infer completion from
                # an ambiguous duplicate inventory alone.
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="input-receipt-persist-failed",
                    detail=f"{type(error).__name__}:{error}",
                    trace=trace,
                    hand_slot=hand_slot,
                    suggestion=suggestion,
                    dispatch=dispatch,
                )
            trace.append("receipt:pending-drink-persisted")

        if deps.post_input_observation_sink is not None:
            try:
                observation = deps.post_input_observation_sink(
                    evidence,
                    action,
                    logical_before,
                    prior_replay,
                    dispatch,
                )
                if observation is not None:
                    audit = (
                        {}
                        if dispatch.audit is None
                        else dict(dispatch.audit)
                    )
                    audit["post_input_observation"] = dict(observation)
                    dispatch = replace(dispatch, audit=audit)
                    trace.append("observation:post-input-captured")
            except Exception as error:
                # The irreversible Maa input is already submitted.  Animation
                # frames are useful early identity evidence, never a second
                # action gate; preserve the diagnostic and continue to the
                # authoritative ExamSave settlement proof.
                audit = {} if dispatch.audit is None else dict(dispatch.audit)
                audit["post_input_observation"] = {
                    "status": "unavailable",
                    "reason": f"{type(error).__name__}:{error}",
                }
                dispatch = replace(dispatch, audit=audit)
                trace.append("observation:post-input-unavailable")

        try:
            after, completed_replay = (
                wait_for_next()
                if prevalidated_next is None
                else prevalidated_next
            )
        except Exception as error:
            recoveries: list[Mapping[str, object]] = []
            retry_error: Exception = error
            modal_confirmation = None
            modal_confirmation_attempts: list[Mapping[str, object]] = []
            if (
                not isinstance(
                    retry_error,
                    Plan2SubmittedPhysicalSettlementError,
                )
                and action.kind == "play"
                and isinstance(deps.play_dispatcher, MaaExamSaveCardPlayDispatcher)
                and authorizer is not None
            ):
                # SELECT, CONFIRM, and the optional card-use modal are one
                # ExamSave-authorized transaction.  Background input can be
                # swallowed at any of those boundaries.  Re-read the current
                # Maa surface before every bounded recovery: an existing
                # modal owns the click; otherwise the exact GUID slot does.
                # The CAS authorizer stops immediately when ExamSave changes,
                # and the slot detector stops on any different Hand/selection.
                for recovery_index in range(6):
                    if any(
                        value.get("submitted") is True
                        for value in modal_confirmation_attempts
                    ):
                        trace.append(
                            "dispatch:submitted-confirmation-timeout-no-retry"
                        )
                        break
                    try:
                        modal_confirmation = (
                            deps.play_dispatcher.submit_card_use_confirmation_if_present(
                                expected_card_guid=action.card_guid,
                                pre_input_authorizer=authorizer,
                            )
                        )
                        if modal_confirmation is not None:
                            if (
                                modal_confirmation.submitted
                                and isinstance(
                                    deps.pre_dispatch_surface_gate,
                                    MaaExamActionSurfaceGate,
                                )
                            ):
                                # The confirmation click is the last real Maa
                                # input in this PLAY transaction.  Restart the
                                # shared quiet window here; measuring from the
                                # earlier card-select click can expose the next
                                # action while the confirmation animation is
                                # still covering the round surface.
                                deps.pre_dispatch_surface_gate.mark_action_submitted()
                                trace.append(
                                    "surface-gate:card-confirmation-submitted"
                                )
                            modal_confirmation_attempts.append(
                                modal_confirmation.to_dict()
                            )
                            trace.append(
                                "dispatch:card-use-confirmation"
                                if len(modal_confirmation_attempts) == 1
                                else "dispatch:card-use-confirmation-retry"
                            )
                        else:
                            # The outer dispatch already returned submitted.
                            # An unchanged/late ExamSave cannot prove rejection,
                            # so another slot click could duplicate the owned
                            # transaction. Only the typed confirmation path
                            # above may continue an input that has not yet sent
                            # its final irreversible confirm.
                            trace.append("dispatch:submitted-timeout-no-recovery")
                            break
                        after, completed_replay = wait_for_next()
                        retry_error = None  # type: ignore[assignment]
                        break
                    except Plan2InputStateAdvanced as advanced:
                        # The original click may finish while the bounded
                        # recovery is inspecting the unchanged frame.  CAS
                        # advancement forbids any retry input; read once more
                        # and let the ordinary transition validator prove the
                        # action instead of reporting a rejected click.
                        trace.append("dispatch:recovery-state-advanced-no-retry")
                        try:
                            after, completed_replay = wait_for_next()
                            retry_error = None  # type: ignore[assignment]
                        except Exception as post_advance_error:
                            retry_error = post_advance_error
                        break
                    except Exception as recovery_error:
                        retry_error = recovery_error
                        if (
                            "next stable settled ExamSaveData was not observed"
                            not in str(recovery_error)
                        ):
                            break
            if (
                retry_error is not None
                and isinstance(action, Plan2NativeDrinkAction)
                and logical_before is not None
                and isinstance(prior_replay, Plan2CompletedDrinkReplay)
                and not prior_replay.provisional_from_submitted_receipt
                and deps.submitted_drink_replay_resolver is not None
                and "next stable settled ExamSaveData was not observed"
                in str(retry_error)
            ):
                try:
                    completed_replay = deps.submitted_drink_replay_resolver(
                        evidence,
                        action,
                        logical_before,
                        prior_replay,
                        dispatch,
                    )
                    if (
                        not isinstance(completed_replay, Plan2CompletedDrinkReplay)
                        or not completed_replay.provisional_from_submitted_receipt
                        or completed_replay.persisted_transition != evidence
                        or completed_replay.persisted_before != evidence
                        or completed_replay.before != logical_before
                        or completed_replay.action != action
                    ):
                        raise ValueError(
                            "submitted drink resolver returned invalid barrier replay"
                        )
                    after = evidence
                    retry_error = None  # type: ignore[assignment]
                    trace.append(
                        "accepted:provisional-drink-receipt-logical-replay"
                    )
                except Exception as provisional_error:
                    retry_error = provisional_error
            if retry_error is not None:
                return self._failed(
                    action=action,
                    before=evidence,
                    shadow_before=shadow_before,
                    code="next-settled-evidence-failed",
                    detail=f"{type(retry_error).__name__}:{retry_error}",
                    trace=trace,
                    hand_slot=hand_slot,
                    suggestion=suggestion,
                    dispatch=dispatch,
                    boundary_disposition=_timeout_disposition(retry_error),
                )
            if recoveries:
                dispatch = Plan2MaaInputDispatch(
                    True,
                    "MAA card input recovered after unchanged ExamSave timeout",
                    {
                        "initial": dispatch.to_dict(),
                        "single_click_recoveries": recoveries,
                    },
                )
            elif modal_confirmation_attempts:
                dispatch = Plan2MaaInputDispatch(
                    True,
                    "MAA card input and skill-card confirmation submitted",
                    {
                        "initial": dispatch.to_dict(),
                        "card_use_confirmation": modal_confirmation_attempts[-1],
                        "card_use_confirmation_attempts": (
                            modal_confirmation_attempts
                        ),
                    },
                )

        trace.append(
            "accepted:completed-card-logical-replay"
            if completed_replay is not None
            else "accepted:changed-stable-settled-exam-save"
        )
        self._shadow_evidence = after
        receipt_can_retire = bool(
            deps.settled_evidence_sink is not None
            and not isinstance(completed_replay, Plan2CompletedDrinkReplay)
            and _state_is_settled_or_terminal(after.state)
        )
        if receipt_can_retire:
            try:
                assert deps.settled_evidence_sink is not None
                deps.settled_evidence_sink(after)
                trace.append("receipt:pending-drink-cleared")
            except Exception as error:
                trace.append(
                    "receipt:pending-drink-clear-failed:"
                    f"{type(error).__name__}:{error}"
                )
        elif (
            deps.settled_evidence_sink is not None
            and not isinstance(completed_replay, Plan2CompletedDrinkReplay)
        ):
            # An exact completed PLAY replay can legitimately bind a retained
            # native command queue.  That proves the PLAY input, but it is not
            # a receipt-retirement boundary: the pending drink receipt remains
            # the restart authority until a later native-actionable or terminal
            # LocalSave is observed.
            trace.append("receipt:pending-drink-retained-transitional")
        return Plan2MaaActionExecutionResult(
            schema_version=PLAN2_NATIVE_MAA_ACTION_EXECUTOR_SCHEMA_VERSION,
            accepted=True,
            action=action,
            before_evidence=evidence,
            next_evidence=after,
            shadow_before=shadow_before,
            shadow_after=after,
            hand_slot=hand_slot,
            suggestion=suggestion,
            dispatch=dispatch,
            issues=(),
            trace=tuple(trace),
            completed_card_replay=completed_replay,
            runtime_provenance=observed_runtime_provenance,
        )


class MaaExamActionSurfaceGate:
    """Bounded read-only Maa gate for a semantically stable exam frame.

    LocalSave can publish a completed logical transition while the previous
    card/support animation still covers the hand.  Maa's upstream
    ``ProduceRecognitionSkipRound`` templates are the existing native signal
    that the main round surface can accept another input.  Character idle
    animation makes whole-frame pixel stability meaningless, so readiness is
    exactly two consecutive identical actionable Maa node tuples, sampled
    three seconds apart.  This gate never clicks; every sample is bound to the
    same HWND/PID and re-runs the exact ExamSave CAS authorizer before it may
    be consumed by the card binder.
    """

    def __init__(
        self,
        *,
        command_sender: CommandSender | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        retry_seconds: float = 3.0,
        timeout_seconds: float = 12.0,
        attempt_limit: int = 4,
    ) -> None:
        if not callable(monotonic) or not callable(sleep):
            raise TypeError("surface-gate clock and sleep must be callable")
        if not (
            0.0 < retry_seconds <= timeout_seconds <= 12.0
        ):
            raise ValueError("surface-gate timing is outside the bounded contract")
        if type(attempt_limit) is not int or not 2 <= attempt_limit <= 4:
            raise ValueError("surface-gate attempt limit is outside 2..4")
        self.command_sender = command_sender
        self.monotonic = monotonic
        self.sleep = sleep
        self.retry_seconds = float(retry_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.attempt_limit = attempt_limit
        self._last_action_submitted_at: float | None = None

    def mark_action_submitted(self) -> None:
        """Record the latest real submission for gate audit/telemetry."""

        self._last_action_submitted_at = self.monotonic()

    def __call__(
        self,
        _evidence: AuditionLocalSaveStateEvidence,
        pre_input_authorizer: PreInputAuthorizer | None,
    ) -> Mapping[str, object]:
        sender = _resolve_command_sender(self.command_sender)
        started = self.monotonic()
        window_identity: tuple[int, int] | None = None
        last_timestamp: float | None = None
        prior_actionable_nodes: tuple[tuple[object, ...], ...] | None = None
        for attempt in range(self.attempt_limit):
            if attempt:
                if self.monotonic() - started >= self.timeout_seconds:
                    break
                self.sleep(self.retry_seconds)
            result = dict(
                sender(
                    "recognize_nia_outer_once",
                    timeout=20.0,
                    scope="exam-action",
                )
            )
            capture = result.get("capture")
            nodes = result.get("nodes")
            if not isinstance(capture, Mapping):
                raise ValueError("Maa exam-action recognition has no capture")
            capture = dict(capture)
            _capture_path(capture)
            try:
                identity = (int(capture["hwnd"]), int(capture["pid"]))
                timestamp = float(capture["timestamp"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    "Maa exam-action capture identity is invalid"
                ) from error
            if window_identity is None:
                window_identity = identity
            elif identity != window_identity:
                raise ValueError("Maa game window changed during surface wait")
            if last_timestamp is not None and timestamp <= last_timestamp:
                raise ValueError("Maa exam-action capture is not fresh")
            last_timestamp = timestamp
            if pre_input_authorizer is not None:
                pre_input_authorizer(capture)
            if not isinstance(nodes, list) or not all(
                isinstance(node, Mapping)
                and set(node).issuperset({"node", "box"})
                for node in nodes
            ):
                raise ValueError("Maa exam-action recognition nodes are invalid")
            actionable_nodes: list[tuple[object, ...]] = []
            for node in nodes:
                label = str(node["node"])
                if label not in {
                    "exam-playable-0",
                    "exam-playable-1",
                    "exam-playable-2",
                }:
                    continue
                box = node["box"]
                score = node.get("score")
                if (
                    not isinstance(box, (list, tuple))
                    or len(box) != 4
                    or not all(type(value) is int for value in box)
                    or isinstance(score, bool)
                    or not isinstance(score, (int, float))
                    or not 0.0 <= float(score) <= 1.0
                ):
                    raise ValueError(
                        "Maa actionable exam node identity is invalid"
                    )
                # Maa already applies the native task threshold before it
                # publishes a hit.  Its floating confidence can drift by a
                # few millionths across identical frames, so validate the
                # score on every sample but keep it out of cross-frame
                # identity.  Label + exact integer ROI are stable semantics.
                actionable_nodes.append((label, tuple(box)))
            signature = tuple(actionable_nodes)
            if signature and signature == prior_actionable_nodes:
                return capture
            prior_actionable_nodes = signature
        raise RuntimeError(
            "Maa actionable exam surface did not reach two-sample semantic "
            "stability"
        )


class MaaPostClickHudStabilityGate:
    """Non-blocking post-PLAY stability probe over parsed HUD semantics.

    Raw pixels are intentionally excluded: transparent HUD backgrounds still
    contain the animated idol.  The gate schedules Maa's existing read-only
    capture command, then reuses the established audition/lesson numeric HUD
    readers.  Two consecutive successful semantic signatures must match.
    """

    def __init__(
        self,
        *,
        command_sender: CommandSender | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        initial_delay_seconds: float = 1.0,
        sample_interval_seconds: float = 1.0,
        semantic_reader: Callable[
            [Path, AuditionLocalSaveStateEvidence], tuple[object, ...]
        ]
        | None = None,
    ) -> None:
        if not callable(monotonic):
            raise TypeError("post-click HUD clock must be callable")
        if initial_delay_seconds != 1.0 or sample_interval_seconds != 1.0:
            raise ValueError("post-click HUD cadence must remain one second")
        if semantic_reader is not None and not callable(semantic_reader):
            raise TypeError("post-click semantic reader must be callable")
        self.command_sender = command_sender
        self.monotonic = monotonic
        self.initial_delay_seconds = float(initial_delay_seconds)
        self.sample_interval_seconds = float(sample_interval_seconds)
        self.semantic_reader = semantic_reader
        self._active = False
        self._stable = False
        self._next_sample_at = 0.0
        self._previous_signature: tuple[object, ...] | None = None
        self._window_identity: tuple[int, int] | None = None
        self._last_timestamp: float | None = None
        self._evidence: AuditionLocalSaveStateEvidence | None = None
        self._recognizer: object | None = None

    def begin(
        self,
        action: Plan2NativeOfflineAction,
        evidence: AuditionLocalSaveStateEvidence | None = None,
    ) -> None:
        self._active = action.kind == "play"
        if self._active and not isinstance(
            evidence, AuditionLocalSaveStateEvidence
        ):
            raise TypeError("post-click PLAY gate requires typed evidence")
        self._stable = not self._active
        self._next_sample_at = self.monotonic() + self.initial_delay_seconds
        self._previous_signature = None
        self._window_identity = None
        self._last_timestamp = None
        self._evidence = evidence

    def _semantic_signature(self, path: Path) -> tuple[object, ...]:
        evidence = self._evidence
        if evidence is None:
            raise ValueError("post-click HUD evidence is unavailable")
        if self.semantic_reader is not None:
            signature = self.semantic_reader(path, evidence)
            if not isinstance(signature, tuple) or not signature:
                raise ValueError("post-click semantic signature is invalid")
            return signature
        if self._recognizer is None:
            from .live_source import _live_text_recognizer

            self._recognizer = _live_text_recognizer()
        recognizer = self._recognizer
        if evidence.state.exam_type == 1:
            from .exam_screen import read_exam_screen_path

            value = read_exam_screen_path(path, recognizer)
            return (
                "audition",
                value.turns_remaining,
                value.player_score,
                value.stamina,
                value.block,
                value.score_multiplier_permille,
                value.logic_status_values,
                value.good_impression,
                value.motivation,
            )
        if evidence.state.exam_type == 0:
            from .lesson_screen import read_lesson_screen_path

            value = read_lesson_screen_path(path, recognizer)
            return (
                "lesson",
                value.turns_remaining,
                value.clear_remaining,
                value.stamina,
                value.block,
                value.lesson_parameter,
                value.item_cooldown,
                value.good_impression,
                value.motivation,
                value.target_tier,
            )
        raise ValueError("post-click HUD exam type is unsupported")

    def sample(self) -> bool:
        if self._stable or not self._active:
            return True
        now = self.monotonic()
        if now < self._next_sample_at:
            return False
        self._next_sample_at = now + self.sample_interval_seconds
        capture = dict(
            _resolve_command_sender(self.command_sender)(
                "capture_screen_once",
                timeout=15.0,
            )
        )
        path = _capture_path(capture)
        try:
            identity = (int(capture["hwnd"]), int(capture["pid"]))
            timestamp = float(capture["timestamp"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("post-click HUD capture identity is invalid") from error
        if self._window_identity is None:
            self._window_identity = identity
        elif identity != self._window_identity:
            raise ValueError("Maa game window changed during post-click wait")
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            raise ValueError("post-click HUD capture is not fresh")
        self._last_timestamp = timestamp
        signature = self._semantic_signature(path)
        previous = self._previous_signature
        self._previous_signature = signature
        if previous is None:
            return False
        self._stable = signature == previous
        return self._stable


class MaaPrintWindowScreenBinder:
    """Capture two MAA PrintWindow frames and reuse strict reconciliation."""

    def __init__(
        self,
        analyzer_factory: ScreenAnalyzerFactory,
        *,
        hand_box_detector: HandBoxDetector,
        command_sender: CommandSender | None = None,
        logical_analyzer_factory: LogicalScreenAnalyzerFactory | None = None,
    ) -> None:
        if not callable(analyzer_factory) or not callable(hand_box_detector):
            raise TypeError("analyzer_factory and hand_box_detector must be callable")
        self.analyzer_factory = analyzer_factory
        self.hand_box_detector = hand_box_detector
        self.command_sender = command_sender
        if logical_analyzer_factory is not None and not callable(
            logical_analyzer_factory
        ):
            raise TypeError("logical_analyzer_factory must be callable or None")
        self.logical_analyzer_factory = logical_analyzer_factory

    def _capture(self) -> Mapping[str, object]:
        sender = _resolve_command_sender(self.command_sender)
        capture = dict(sender("capture_screen_once", timeout=15.0))
        _capture_path(capture)
        for field in ("timestamp", "hwnd", "pid"):
            if field not in capture:
                raise ValueError(f"MAA capture is missing {field}")
        return capture

    def __call__(
        self,
        evidence: AuditionLocalSaveStateEvidence,
        logical: Plan2NativeHorizonState | None = None,
        prior_replay: Plan2CompletedLogicalReplay | None = None,
    ) -> Plan2MaaScreenBinding:
        if logical is None:
            analyzer = self.analyzer_factory(evidence)
        else:
            if self.logical_analyzer_factory is None:
                raise ValueError("logical screen analyzer factory is not configured")
            analyzer = self.logical_analyzer_factory(evidence, logical)
        if not callable(analyzer):
            raise TypeError("screen analyzer factory returned a non-callable")
        first_capture = self._capture()
        second_capture = self._capture()
        if (
            int(first_capture["hwnd"]),
            int(first_capture["pid"]),
        ) != (
            int(second_capture["hwnd"]),
            int(second_capture["pid"]),
        ):
            raise ValueError("MAA game window changed between reconciliation captures")
        first = analyzer(_capture_path(first_capture))
        second = analyzer(_capture_path(second_capture))
        schedule = (
            verified_turn_schedule_from_local_save(evidence)
            if logical is None
            else _logical_turn_schedule_from_retained_evidence(evidence)
        )

        def reconcile_pair(
            left_analysis: CardPlayFrameAnalysis,
            right_analysis: CardPlayFrameAnalysis,
        ) -> ReconciliationScreenSemantic:
            if logical is None:
                return validate_stable_screen_pair_against_local_save(
                    left_analysis,
                    right_analysis,
                    schedule=schedule,
                    evidence=evidence,
                )
            if prior_replay is None:
                raise ValueError("logical screen binding requires prior replay")
            if left_analysis.semantic_key != right_analysis.semantic_key:
                raise ValueError("fresh Maa frames do not have identical semantics")
            left_semantic = screen_semantic_from_analysis(left_analysis, schedule)
            right_semantic = screen_semantic_from_analysis(right_analysis, schedule)
            if (
                left_semantic != right_semantic
                or left_semantic.digest() != right_semantic.digest()
            ):
                raise ValueError("fresh Maa frames do not have identical semantics")
            validate_screen_semantic_against_logical_horizon(
                left_semantic, evidence, logical, prior_replay
            )
            return left_semantic

        semantic = reconcile_pair(first, second)
        boxes = self.hand_box_detector(second_capture)
        return Plan2MaaScreenBinding(second_capture, semantic, boxes)


class MaaExamSaveSlotScreenBinder:
    """Bind one fresh MAA capture to ExamSave-owned ordered card slots.

    Production Plan2 does not need card-art OCR, HUD prediction matching, or a
    second identical screenshot.  ExamSave owns card identity and slot order;
    the one MAA capture proves only that the expected number of fixed card
    targets is currently visible in the game window.
    """

    def __init__(
        self,
        *,
        hand_box_detector: HandBoxDetector | None = None,
        command_sender: CommandSender | None = None,
    ) -> None:
        if hand_box_detector is not None and not callable(hand_box_detector):
            raise TypeError("hand_box_detector must be callable or None")
        self.hand_box_detector = hand_box_detector
        self.command_sender = command_sender

    def __call__(
        self,
        evidence: AuditionLocalSaveStateEvidence,
        logical: Plan2NativeHorizonState | None = None,
        prior_replay: Plan2CompletedLogicalReplay | None = None,
        expected_slot: int | None = None,
    ) -> Plan2MaaScreenBinding:
        sender = _resolve_command_sender(self.command_sender)
        capture = dict(sender("capture_screen_once", timeout=15.0))
        return self.bind_capture(
            evidence,
            capture,
            logical=logical,
            prior_replay=prior_replay,
            expected_slot=expected_slot,
        )

    def bind_capture(
        self,
        evidence: AuditionLocalSaveStateEvidence,
        capture: Mapping[str, object],
        *,
        logical: Plan2NativeHorizonState | None = None,
        prior_replay: Plan2CompletedLogicalReplay | None = None,
        expected_slot: int | None = None,
    ) -> Plan2MaaScreenBinding:
        """Bind the exact fresh frame already proven by the action gate."""

        capture = dict(capture)
        _capture_path(capture)
        for field in ("timestamp", "hwnd", "pid"):
            if field not in capture:
                raise ValueError(f"MAA capture is missing {field}")
        cards = evidence.state.zones.hand if logical is None else logical.zones.hand
        selected_slot = None
        if self.hand_box_detector is None:
            # The production ExamSave path already has the complete ordered
            # Hand and therefore the exact slot count.  Reuse the same pinned
            # 1..5 equal-size layouts as the Plan3 Maa executor; card art,
            # localized text, support '+' pixels and object detection are not
            # independent authorities and must not veto an otherwise valid
            # GUID->slot binding.
            from .plan3_audition_executor import FIXED_HAND_SLOT_BOXES

            boxes = FIXED_HAND_SLOT_BOXES.get(len(cards), ())
        else:
            try:
                boxes = self.hand_box_detector(capture)
            except ValueError as error:
                if (
                    expected_slot is None
                    or "card row is above the stable bottom-hand band" not in str(error)
                ):
                    raise
                boxes, selected_slot = detect_existing_maa_recovery_hand_boxes(
                    capture,
                    expected_slot=expected_slot,
                    expected_hand_count=len(cards),
                )
        if not cards or len(boxes) != len(cards):
            raise ValueError(
                "MAA visible card-slot count differs from ordered Hand authority"
            )
        schedule = (
            verified_turn_schedule_from_local_save(evidence)
            if logical is None
            else _logical_turn_schedule_from_retained_evidence(evidence)
        )
        state = evidence.state
        if logical is None:
            turns, score, stamina, block = (
                state.remain_turn,
                state.score,
                state.stamina,
                state.block,
            )
        else:
            turns, score, stamina, block = (
                logical.remaining_turns,
                logical.scalar.score,
                logical.scalar.stamina,
                logical.scalar.block,
            )
        if state.exam_type == 0:
            multiplier = 1000
        else:
            parameter = state.turn_parameter_types[
                (state.current_turn if logical is None else logical.scalar.current_turn)
                - 1
            ]
            multiplier = {
                1: state.vocal_bonus_permille,
                2: state.dance_bonus_permille,
                3: state.visual_bonus_permille,
            }[parameter]
        semantic = ReconciliationScreenSemantic(
            RECONCILIATION_SCREEN_SEMANTIC_SCHEMA_VERSION,
            turns,
            score,
            stamina,
            block,
            multiplier,
            tuple(
                ReconciliationScreenHandCard(
                    slot,
                    card.card_id,
                    card.effective_upgrade,
                    0,
                    "exam-save-fixed-slot",
                    card.support_upgrade_ids[0]
                    if len(card.support_upgrade_ids) == 1
                    else None,
                )
                for slot, card in enumerate(cards)
            ),
            tuple(
                (frame.round_number, frame.lesson_type, frame.score_multiplier_permille)
                for frame in schedule.frames
            ),
        )
        return Plan2MaaScreenBinding(
            capture,
            semantic,
            boxes,
            selected_slot=selected_slot,
        )


def detect_existing_maa_hand_boxes(
    capture: Mapping[str, object],
) -> tuple[CanonicalBox, ...]:
    """Use the existing live card detector and left-to-right Hand ordering."""

    from .live_source import (
        _canonical_detection_box,
        _card_detector,
        _deduplicate_card_detections,
        _lesson_card_layout_issue,
    )

    report = _card_detector().detect_path(_capture_path(capture))
    detections = tuple(
        sorted(
            _deduplicate_card_detections(
                tuple(
                    item
                    for item in report.detections
                    if item.label in {"cards", "recommend", "useless"}
                    and item.y >= report.image_height * 0.62
                )
            ),
            key=lambda item: item.x,
        )
    )
    issue = _lesson_card_layout_issue(report, detections)
    if issue is not None:
        raise ValueError(issue)
    return tuple(
        _canonical_detection_box(report, detection) for detection in detections
    )


def detect_existing_maa_recovery_hand_boxes(
    capture: Mapping[str, object],
    *,
    expected_slot: int,
    expected_hand_count: int,
) -> tuple[tuple[CanonicalBox, ...], int | None]:
    """Bind a stable hand or one selected-card preview by slot geometry.

    Recovery must not require artwork, card text, or support ``+`` colour.
    Maa supplies same-sized card boxes and ExamSave/logical state supplies the
    ordered GUIDs.  A SELECT preview raises exactly one card by a small amount;
    only the already-authorized expected slot may be that raised card.
    """

    from .live_source import (
        _canonical_detection_box,
        _card_detector,
        _deduplicate_card_detections,
    )

    if expected_hand_count < 1 or not 0 <= expected_slot < expected_hand_count:
        raise ValueError("recovery Hand authority is invalid")
    report = _card_detector().detect_path(_capture_path(capture))
    detections = tuple(
        sorted(
            _deduplicate_card_detections(
                tuple(
                    item
                    for item in report.detections
                    if item.label in {"cards", "recommend", "useless"}
                    and item.y >= report.image_height * 0.62
                )
            ),
            key=lambda item: item.x,
        )
    )
    if len(detections) != expected_hand_count:
        raise ValueError("recovery visible card count differs from Hand authority")
    if min(item.confidence for item in detections) < 0.80:
        raise ValueError("recovery card confidence is below 80%")
    minimum_width = report.image_width * 0.18
    minimum_height = report.image_height * 0.16
    if any(
        item.width < minimum_width or item.height < minimum_height
        for item in detections
    ):
        raise ValueError("recovery card box is too small")
    baseline = max(item.y for item in detections)
    raised = tuple(
        index
        for index, item in enumerate(detections)
        if baseline - item.y >= report.image_height * 0.012
    )
    if raised and raised != (expected_slot,):
        raise ValueError("a different card slot is selected")
    if max(item.y for item in detections) - min(item.y for item in detections) > (
        report.image_height * 0.08
    ):
        raise ValueError("recovery card row is still moving")
    centers = tuple(item.x + item.width / 2 for item in detections)
    if any(right <= left for left, right in zip(centers, centers[1:])):
        raise ValueError("recovery card slots are not left-to-right")
    return (
        tuple(_canonical_detection_box(report, item) for item in detections),
        expected_slot if raised else None,
    )


def detect_existing_maa_end_turn_action(
    binding: Plan2MaaScreenBinding,
) -> SuggestedClick:
    """Bind the existing, exported Turn End button region to this capture."""

    from .plan3_audition_executor import SKIP_BOX

    return _suggestion_for_box(
        binding.capture,
        label="Plan2 native END_TURN",
        box=SKIP_BOX,
    )


def _snapshot_native_action_settlement_cursor(
    pid: int,
) -> dict[str, object] | None:
    """Snapshot the append-only recorder end without reading prior rows."""

    try:
        path = default_native_runtime_recorder_path(pid)
        start_offset = path.stat().st_size
    except Exception:  # optional telemetry must never block Maa input
        return None
    return {"path": str(path), "start_offset": start_offset}


class MaaVerifiedCardPlayDispatcher:
    """Reuse SELECT preview verification and conditional same-slot confirm."""

    def __init__(self, *, command_sender: CommandSender | None = None) -> None:
        self.command_sender = command_sender

    def __call__(
        self,
        action: SuggestedClick,
        analyzer: FrameAnalyzer,
        *,
        pre_input_authorizer: PreInputAuthorizer | None = None,
    ) -> Plan2MaaInputDispatch:
        click_kwargs: dict[str, Any] = {
            "command_sender": _resolve_command_sender(self.command_sender),
        }
        if pre_input_authorizer is not None:
            click_kwargs["pre_input_authorizer"] = pre_input_authorizer
        result: CardPlayExecutionResult = execute_verified_card_click(
            action,
            analyzer,
            **click_kwargs,
        )
        audit = result.to_dict()
        click_results = audit.get("click_results")
        # In this SELECT workflow the first click is reversible selection and
        # the second click is the irreversible confirmation.  Once the second
        # controller command has returned successfully, post-input OCR can no
        # longer turn the receipt back into "not submitted".  The stricter
        # LocalSave waiter still decides whether the gameplay transition is
        # accepted; this flag only prevents a dangerous duplicate retry.
        confirmation_submitted = bool(
            isinstance(click_results, list) and len(click_results) >= 2
        )
        submitted = bool(result.committed or confirmation_submitted)
        return Plan2MaaInputDispatch(
            submitted=submitted,
            detail=(
                "verified card select/conditional confirm submitted"
                if result.committed
                else "card confirmation submitted; awaiting durable transition evidence"
                if confirmation_submitted
                else str(result.failure_reason or "card play was not committed")
            ),
            audit=audit,
        )


class MaaExamSaveCardPlayDispatcher(MaaVerifiedCardPlayDispatcher):
    """Submit one fixed-slot SELECT/CONFIRM pair with per-click ExamSave CAS.

    MAA owns background input and window geometry.  The caller's authorizer
    checks the exact ExamSave GUID/slot immediately before input.  Post-action
    success is intentionally left to the ExamSave waiter; screenshot OCR does
    not re-judge native simulation values.
    """

    def __init__(
        self,
        *,
        command_sender: CommandSender | None = None,
        sleep: Callable[[float], None] = time.sleep,
        preview_seconds: float = 0.55,
    ) -> None:
        super().__init__(command_sender=command_sender)
        if preview_seconds < 0:
            raise ValueError("preview_seconds must be non-negative")
        self.sleep = sleep
        self.preview_seconds = preview_seconds

    def __call__(
        self,
        action: SuggestedClick,
        analyzer: FrameAnalyzer,
        *,
        pre_input_authorizer: PreInputAuthorizer | None = None,
    ) -> Plan2MaaInputDispatch:
        del analyzer  # ExamSave, not post-click OCR, is the durable authority.
        sender, window_x, window_y = self._prepare_fixed_slot_input(
            action,
            pre_input_authorizer=pre_input_authorizer,
        )
        native_action_settlement = self._native_action_settlement_cursor(
            action.source_pid
        )
        source_capture = {
            "png_path": action.source_png_path,
            "timestamp": action.source_timestamp,
            "hwnd": action.source_hwnd,
            "pid": action.source_pid,
        }
        first_click, first_failures = self._send_fixed_slot_click(
            sender,
            window_x=window_x,
            window_y=window_y,
            source_capture=source_capture,
            pre_input_authorizer=pre_input_authorizer,
        )
        clicks = [first_click]
        transient_failures = list(first_failures)
        self.sleep(self.preview_seconds)
        if pre_input_authorizer is not None:
            # SELECT can itself advance ExamSave on a surface that does not
            # retain a separate confirmation modal.  Re-authorize the exact
            # GUID/slot before CONFIRM so a successful first click never
            # receives a duplicate second click.
            try:
                pre_input_authorizer(dict(source_capture))
            except Plan2InputStateAdvanced as advanced:
                raise Plan2InputStateAdvanced(
                    str(advanced),
                    input_submitted=True,
                ) from advanced
        second_click, second_failures = self._send_fixed_slot_click(
            sender,
            window_x=window_x,
            window_y=window_y,
            source_capture=source_capture,
            pre_input_authorizer=pre_input_authorizer,
        )
        clicks.append(second_click)
        transient_failures.extend(second_failures)
        return Plan2MaaInputDispatch(
            submitted=True,
            detail="MAA fixed-slot select/confirm submitted; ExamSave owns result",
            audit={
                "mode": "maa-examsave-minimal",
                "source_capture": source_capture,
                "window_x": window_x,
                "window_y": window_y,
                "click_results": clicks,
                "transient_retries": len(transient_failures),
                "transient_failures": transient_failures,
                "native_action_settlement": native_action_settlement,
            },
        )

    @staticmethod
    def _native_action_settlement_cursor(
        pid: int,
    ) -> dict[str, object] | None:
        return _snapshot_native_action_settlement_cursor(pid)

    def _send_fixed_slot_click(
        self,
        sender: CommandSender,
        *,
        window_x: int,
        window_y: int,
        source_capture: Mapping[str, Any],
        pre_input_authorizer: PreInputAuthorizer | None,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Retry one transient Maa job failure under the same ExamSave CAS.

        Maa may report a failed background-input job even though no durable
        game transition occurred.  Retry that individual click once only when
        the caller can re-authorize the original action against current
        ExamSave evidence.  If the first attempt actually changed the save,
        the authorizer raises and the duplicate click is never submitted.
        """

        failures: list[str] = []
        for attempt in range(2):
            try:
                result = sender(
                    "send_input_click_once",
                    window_x=window_x,
                    window_y=window_y,
                )
                return dict(result), tuple(failures)
            except RuntimeError as exc:
                failures.append(str(exc))
                if attempt >= 1 or pre_input_authorizer is None:
                    raise
                try:
                    pre_input_authorizer(dict(source_capture))
                except Plan2InputStateAdvanced as advanced:
                    raise Plan2InputStateAdvanced(
                        str(advanced),
                        input_submitted=True,
                    ) from advanced
                self.sleep(0.15)
        raise AssertionError("unreachable fixed-slot click retry state")

    def submit_single_recovery_click(
        self,
        action: SuggestedClick,
        *,
        expected_slot: int,
        expected_hand_count: int,
        pre_input_authorizer: PreInputAuthorizer,
    ) -> Plan2MaaInputDispatch:
        """Retry one fixed slot only while ExamSave CAS is unchanged.

        A normal card input is SELECT then CONFIRM.  If character animation
        swallows either background click, the durable save does not change.
        Recovery sends one click at a time and waits for ExamSave after each:
        it therefore confirms an already-selected card or selects an idle one
        without ever blindly submitting another pair.
        """

        # Check durable state before even interpreting the recovery frame.
        # A changed ExamSave means the prior input already landed; card-count
        # differences are then expected and must never trigger another click.
        pre_input_authorizer(
            {
                "png_path": action.source_png_path,
                "timestamp": action.source_timestamp,
                "hwnd": action.source_hwnd,
                "pid": action.source_pid,
            }
        )
        sender = _resolve_command_sender(self.command_sender)
        capture = dict(sender("capture_screen_once", timeout=15.0))
        boxes, selected_slot = detect_existing_maa_recovery_hand_boxes(
            capture,
            expected_slot=expected_slot,
            expected_hand_count=expected_hand_count,
        )
        rebound = _suggestion_for_box(
            capture,
            label="Plan2 native PLAY single recovery",
            box=boxes[expected_slot],
        )
        sender, window_x, window_y = self._prepare_fixed_slot_input(
            rebound,
            pre_input_authorizer=pre_input_authorizer,
        )
        click = dict(
            sender(
                "send_input_click_once",
                window_x=window_x,
                window_y=window_y,
            )
        )
        return Plan2MaaInputDispatch(
            submitted=True,
            detail="MAA single fixed-slot recovery click submitted",
            audit={
                "mode": "maa-examsave-minimal-single-recovery",
                "selected_slot": selected_slot,
                "expected_slot": expected_slot,
                "source_capture": dict(capture),
                "window_x": window_x,
                "window_y": window_y,
                "click_result": click,
            },
        )

    def submit_card_use_confirmation_if_present(
        self,
        *,
        expected_card_guid: str | None,
        pre_input_authorizer: PreInputAuthorizer,
    ) -> Plan2MaaInputDispatch | None:
        """Confirm Maa's existing optional skill-card modal once, if visible.

        Some cards are already selected and confirmed when the game opens its
        separate ``skill card use confirmation`` modal (for example when one
        ordered child has no target).  While that modal is open there is no
        meaningful Hand row to recount.  Reuse Maa's existing localized modal
        recognizer, keep ExamSave CAS as the sole action authority, and submit
        exactly one affirmative click.  Absence of the modal performs no
        input and is not an error.
        """

        if not isinstance(expected_card_guid, str) or not expected_card_guid:
            raise ValueError("card-use confirmation requires the selected card GUID")
        from .plan3_audition_executor import (
            CARD_USE_CONFIRM_BUTTON_BOX,
            _card_use_confirmation_evidence,
        )

        sender = _resolve_command_sender(self.command_sender)
        capture = dict(sender("capture_screen_once", timeout=15.0))
        evidence = _card_use_confirmation_evidence(_capture_path(capture))
        if not evidence.detected:
            return None
        action = _suggestion_for_box(
            capture,
            label="Plan2 native confirm skill card use",
            box=CARD_USE_CONFIRM_BUTTON_BOX,
        )

        def modal_authorizer(bound_capture: Mapping[str, Any]) -> None:
            # The modal is created only after SELECT/CONFIRM, so ExamSave can
            # already contain the selected card's retained command queue.
            # Tell the production CAS exactly which already-authorized GUID
            # may own that transition; ordinary card clicks keep requiring
            # byte-identical before evidence.
            pre_input_authorizer(
                {
                    **dict(bound_capture),
                    "plan2_confirmation_playing_card_guid": expected_card_guid,
                }
            )

        sender, window_x, window_y = self._prepare_fixed_slot_input(
            action,
            pre_input_authorizer=modal_authorizer,
        )
        click = dict(
            sender(
                "send_input_click_once",
                window_x=window_x,
                window_y=window_y,
            )
        )
        return Plan2MaaInputDispatch(
            True,
            "MAA skill-card use confirmation submitted",
            {
                "mode": "maa-examsave-card-use-confirmation",
                "source_capture": dict(capture),
                "title_text": evidence.title_text,
                "confirm_text": evidence.confirm_text,
                "window_x": window_x,
                "window_y": window_y,
                "click_result": click,
            },
        )

    def _prepare_fixed_slot_input(
        self,
        action: SuggestedClick,
        *,
        pre_input_authorizer: PreInputAuthorizer | None,
    ) -> tuple[CommandSender, int, int]:
        sender = _resolve_command_sender(self.command_sender)
        status = dict(sender("status"))
        if not bool(status.get("background_control")):
            raise RuntimeError("Plan2 card play requires MAA background control")
        if (
            int(status.get("target_hwnd", 0)) != action.source_hwnd
            or int(status.get("target_pid", 0)) != action.source_pid
        ):
            raise ValueError("MAA target window differs from the bound capture")
        geometry = status.get("geometry")
        if not isinstance(geometry, Mapping):
            raise RuntimeError("MAA controller status has no window geometry")
        if pre_input_authorizer is not None:
            pre_input_authorizer(
                {
                    "png_path": action.source_png_path,
                    "timestamp": action.source_timestamp,
                    "hwnd": action.source_hwnd,
                    "pid": action.source_pid,
                }
            )
        window_x, window_y = canonical_to_outer_window(
            action.canonical_x,
            action.canonical_y,
            geometry,
        )
        return sender, window_x, window_y


def _normalize_ui_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).upper()
    return "".join(character for character in normalized if character.isalnum())


class MaaExistingDrinkDispatcher:
    """Use one exact LocalSave drink slot through Maa background input.

    ExamSave owns ordered drink identity and the pre-input CAS.  Maa owns the
    pinned inventory slot and its existing ``ProduceUseDrinkConfirm`` node
    owns the actual Use-button location.  After ``Use``, Maa's existing
    ``ProduceCancel``/``ProduceNIAButton`` template nodes own the exceptional
    ineffective-effect confirmation.  No OCR, colour, or new template is
    introduced here.
    """

    def __init__(
        self,
        *,
        command_sender: CommandSender | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        open_settle_seconds: float = 0.45,
        open_attempt_limit: int = 8,
        open_retry_timeout_seconds: float = 12.0,
        post_use_settle_seconds: float = 0.45,
        confirmation_settle_seconds: float = 0.45,
    ) -> None:
        if (
            open_settle_seconds < 0
            or post_use_settle_seconds < 0
            or confirmation_settle_seconds < 0
        ):
            raise ValueError("drink settle seconds must be non-negative")
        if type(open_attempt_limit) is not int or open_attempt_limit < 1:
            raise ValueError("drink open attempt limit must be a positive integer")
        if (
            isinstance(open_retry_timeout_seconds, bool)
            or not isinstance(open_retry_timeout_seconds, (int, float))
            or open_retry_timeout_seconds <= 0
        ):
            raise ValueError("drink open retry timeout must be positive")
        self.command_sender = command_sender
        self.sleep = sleep
        self.monotonic = monotonic
        self.open_settle_seconds = open_settle_seconds
        self.open_attempt_limit = open_attempt_limit
        self.open_retry_timeout_seconds = float(open_retry_timeout_seconds)
        self.post_use_settle_seconds = post_use_settle_seconds
        self.confirmation_settle_seconds = confirmation_settle_seconds

    def __call__(
        self,
        action: Plan2NativeDrinkAction,
        suggestion: SuggestedClick,
        *,
        pre_input_authorizer: PreInputAuthorizer | None = None,
        confirmation_has_effect: bool | None = None,
    ) -> Plan2MaaInputDispatch:
        if not isinstance(action, Plan2NativeDrinkAction):
            raise TypeError("action must be Plan2NativeDrinkAction")
        from .plan3_audition_executor import DRINK_SLOT_BOXES

        if not 0 <= action.slot_index < len(DRINK_SLOT_BOXES):
            raise ValueError("drink slot is outside Maa's visible inventory")
        if suggestion.verification_box != DRINK_SLOT_BOXES[action.slot_index]:
            raise ValueError("drink suggestion is not bound to the selected slot")

        sender = _resolve_command_sender(self.command_sender)
        status = dict(sender("status"))
        if not bool(status.get("background_control")):
            raise RuntimeError("Plan2 drink use requires MAA background control")
        if (
            int(status.get("target_hwnd", 0)) != suggestion.source_hwnd
            or int(status.get("target_pid", 0)) != suggestion.source_pid
        ):
            raise ValueError("MAA target differs from the bound drink capture")
        geometry = status.get("geometry")
        if not isinstance(geometry, Mapping):
            raise RuntimeError("MAA controller status has no window geometry")

        source = {
            "png_path": suggestion.source_png_path,
            "timestamp": suggestion.source_timestamp,
            "hwnd": suggestion.source_hwnd,
            "pid": suggestion.source_pid,
        }
        open_x, open_y = canonical_to_outer_window(
            suggestion.canonical_x,
            suggestion.canonical_y,
            geometry,
        )
        allowed_dialog_nodes = frozenset(
            {"common-cancel", "common-decide", "decide", "drink-use"}
        )

        def recognize_dialog(
            *,
            after_timestamp: float,
        ) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
            try:
                raw_recognition = sender(
                    "recognize_nia_outer_once",
                    timeout=20.0,
                    scope="drink-dialog",
                )
            except (RuntimeError, ValueError) as error:
                # A controller launched before the bounded drink-dialog scope
                # reports the original three-scope ValueError.  Only that
                # exact signal may broaden this read-only batch.
                direct_legacy_error = (
                    type(error) is ValueError
                    and str(error) == _LEGACY_NIA_OUTER_SCOPE_ERROR
                )
                wrapped_legacy_error = (
                    type(error) is RuntimeError
                    and str(error)
                    == f"ValueError: {_LEGACY_NIA_OUTER_SCOPE_ERROR}"
                )
                if not (direct_legacy_error or wrapped_legacy_error):
                    raise
                raw_recognition = sender(
                    "recognize_nia_outer_once",
                    timeout=20.0,
                    scope="subpage",
                )
            recognition = dict(raw_recognition)
            raw_capture = recognition.get("capture")
            raw_nodes = recognition.get("nodes")
            if not isinstance(raw_capture, Mapping) or not isinstance(raw_nodes, list):
                raise ValueError("Maa drink recognition is malformed")
            capture = dict(raw_capture)
            _capture_path(capture)
            if (
                int(capture.get("hwnd", 0)) != suggestion.source_hwnd
                or int(capture.get("pid", 0)) != suggestion.source_pid
                or float(capture.get("timestamp", 0.0)) <= after_timestamp
            ):
                raise ValueError("MAA drink recognition capture did not advance")
            nodes = {
                str(value.get("node")): value
                for value in raw_nodes
                if (
                    isinstance(value, Mapping)
                    and isinstance(value.get("node"), str)
                    and value.get("node") in allowed_dialog_nodes
                )
            }
            return capture, nodes

        def node_center(
            nodes: Mapping[str, Mapping[str, Any]],
            name: str,
        ) -> tuple[int, int] | None:
            value = nodes.get(name)
            if value is None:
                return None
            box = value.get("box")
            if (
                not isinstance(box, list)
                or len(box) != 4
                or any(type(part) is not int for part in box)
                or box[2] <= 0
                or box[3] <= 0
            ):
                raise ValueError(f"Maa drink dialog node has invalid box: {name}")
            return canonical_to_outer_window(
                box[0] + box[2] // 2,
                box[1] + box[3] // 2,
                geometry,
            )

        opened_clicks: list[dict[str, Any]] = []
        opened_captures: list[dict[str, Any]] = []
        opened_nodes: dict[str, Mapping[str, Any]] = {}
        open_authority = source
        open_started_at = self.monotonic()
        open_exhaustion = "attempt-limit"
        for open_attempt in range(self.open_attempt_limit):
            if (
                open_attempt
                and self.monotonic() - open_started_at
                >= self.open_retry_timeout_seconds
            ):
                open_exhaustion = "wall-timeout"
                break
            if pre_input_authorizer is not None:
                pre_input_authorizer(open_authority)
            opened_clicks.append(
                dict(
                    sender(
                        "send_input_click_once",
                        window_x=open_x,
                        window_y=open_y,
                    )
                )
            )
            self.sleep(self.open_settle_seconds)
            opened, opened_nodes = recognize_dialog(
                after_timestamp=float(open_authority["timestamp"]),
            )
            opened_captures.append(opened)
            if "drink-use" in opened_nodes:
                break
            # A recognized confirmation/decision modal is not the inventory
            # panel and must never receive a slot retry behind it.
            if opened_nodes:
                return Plan2MaaInputDispatch(
                    submitted=False,
                    detail=(
                        "Maa drink panel did not open; no Use input submitted"
                    ),
                    audit={
                        "mode": "maa-examsave-drink-panel-not-open",
                        "slot_index": action.slot_index,
                        "instance_id": action.instance_id,
                        "drink_id": action.drink_id,
                        "source_capture": source,
                        "opened_captures": opened_captures,
                        "observed_open_nodes": sorted(opened_nodes),
                        "open_exhaustion": "unexpected-dialog",
                        "open_attempt_limit": self.open_attempt_limit,
                        "click_results": opened_clicks,
                    },
                )
            open_authority = opened
            if (
                open_attempt + 1 >= self.open_attempt_limit
                or self.monotonic() - open_started_at
                >= self.open_retry_timeout_seconds
            ):
                open_exhaustion = (
                    "attempt-limit"
                    if open_attempt + 1 >= self.open_attempt_limit
                    else "wall-timeout"
                )
                break
        else:  # pragma: no cover - loop always breaks or exhausts above
            raise AssertionError("bounded drink-open loop escaped")

        if "drink-use" not in opened_nodes:
            return Plan2MaaInputDispatch(
                submitted=False,
                detail="Maa drink panel did not open; no Use input submitted",
                audit={
                    "mode": "maa-examsave-drink-panel-not-open",
                    "slot_index": action.slot_index,
                    "instance_id": action.instance_id,
                    "drink_id": action.drink_id,
                    "source_capture": source,
                    "opened_captures": opened_captures,
                    "observed_open_nodes": [],
                    "open_exhaustion": open_exhaustion,
                    "open_attempt_limit": self.open_attempt_limit,
                    "click_results": opened_clicks,
                },
            )

        use_target = node_center(opened_nodes, "drink-use")
        assert use_target is not None
        used_clicks: list[dict[str, Any]] = []
        post_use_captures: list[dict[str, Any]] = []
        use_authority = opened
        nodes: dict[str, Mapping[str, Any]] = {}
        dialog_state = "detail"
        if pre_input_authorizer is not None:
            pre_input_authorizer(use_authority)
        used_clicks.append(
            dict(
                sender(
                    "send_input_click_once",
                    window_x=use_target[0],
                    window_y=use_target[1],
                )
            )
        )
        for _poll_attempt in range(2):
            self.sleep(self.post_use_settle_seconds)
            if pre_input_authorizer is not None:
                try:
                    pre_input_authorizer(use_authority)
                except Plan2InputStateAdvanced as advanced:
                    raise Plan2InputStateAdvanced(
                        str(advanced),
                        input_submitted=True,
                    ) from advanced
            post_use, nodes = recognize_dialog(
                after_timestamp=float(use_authority["timestamp"]),
            )
            post_use_captures.append(post_use)
            confirm_name = next(
                (
                    name
                    for name in ("common-decide", "decide")
                    if name in nodes
                ),
                None,
            )
            if "common-cancel" in nodes and confirm_name is not None:
                dialog_state = "confirmation"
            elif "drink-use" in nodes:
                dialog_state = "detail"
            else:
                dialog_state = "submitted"
            if dialog_state != "detail":
                break
            use_authority = post_use

        if dialog_state == "confirmation":
            cancel_target = node_center(nodes, "common-cancel")
            assert cancel_target is not None
            confirm_name = next(
                (
                    name
                    for name in ("common-decide", "decide")
                    if name in nodes
                ),
                None,
            )
            assert confirm_name is not None
            confirm_target = node_center(nodes, confirm_name)
            # A missing exact effect policy is not permission to consume an
            # item.  Closing the dialog is reversible and returns the caller
            # to the already-authorized battle state.
            if confirmation_has_effect is not True or confirm_target is None:
                exact_noop = confirmation_has_effect is False
                if pre_input_authorizer is not None:
                    try:
                        pre_input_authorizer(post_use)
                    except Plan2InputStateAdvanced as advanced:
                        raise Plan2InputStateAdvanced(
                            str(advanced),
                            input_submitted=True,
                        ) from advanced
                cancel_click = dict(
                    sender(
                        "send_input_click_once",
                        window_x=cancel_target[0],
                        window_y=cancel_target[1],
                    )
                )
                self.sleep(self.confirmation_settle_seconds)
                cancelled = dict(sender("capture_screen_once", timeout=15.0))
                _capture_path(cancelled)
                if (
                    int(cancelled.get("hwnd", 0)) != suggestion.source_hwnd
                    or int(cancelled.get("pid", 0)) != suggestion.source_pid
                    or float(cancelled.get("timestamp", 0.0))
                    <= float(post_use.get("timestamp", 0.0))
                ):
                    raise ValueError(
                        "MAA game window/capture did not advance after drink cancel"
                    )
                return Plan2MaaInputDispatch(
                    submitted=False,
                    detail=(
                        "Maa ineffective-drink confirmation cancelled; "
                        "drink was not submitted"
                        if exact_noop
                        else "Maa unproven drink dialog cancelled; drink was not submitted"
                    ),
                    audit={
                        "mode": (
                            "maa-examsave-drink-noop-cancelled"
                            if exact_noop
                            else "maa-examsave-drink-dialog-cancelled"
                        ),
                        "slot_index": action.slot_index,
                        "instance_id": action.instance_id,
                        "drink_id": action.drink_id,
                        "source_capture": source,
                        "opened_capture": opened,
                        "opened_nodes": sorted(opened_nodes),
                        "confirmation_capture": post_use,
                        "post_cancel_capture": cancelled,
                        "observed_nodes": sorted(nodes),
                        "open_attempt_count": len(opened_clicks),
                        "use_attempt_count": len(used_clicks),
                        "click_results": [
                            *opened_clicks,
                            *used_clicks,
                            cancel_click,
                        ],
                    },
                )
            assert confirm_target is not None and confirm_name is not None
            if pre_input_authorizer is not None:
                try:
                    pre_input_authorizer(post_use)
                except Plan2InputStateAdvanced as advanced:
                    raise Plan2InputStateAdvanced(
                        str(advanced),
                        input_submitted=True,
                    ) from advanced
            confirm_click = dict(
                sender(
                    "send_input_click_once",
                    window_x=confirm_target[0],
                    window_y=confirm_target[1],
                )
            )
            self.sleep(self.confirmation_settle_seconds)
            confirmed = dict(sender("capture_screen_once", timeout=15.0))
            _capture_path(confirmed)
            if (
                int(confirmed.get("hwnd", 0)) != suggestion.source_hwnd
                or int(confirmed.get("pid", 0)) != suggestion.source_pid
                or float(confirmed.get("timestamp", 0.0))
                <= float(post_use.get("timestamp", 0.0))
            ):
                raise ValueError(
                    "MAA game window/capture did not advance after drink confirm"
                )
            return Plan2MaaInputDispatch(
                submitted=True,
                detail="MAA exact drink slot, use, and effect confirmation submitted",
                audit={
                    "mode": "maa-examsave-drink",
                    "slot_index": action.slot_index,
                    "instance_id": action.instance_id,
                    "drink_id": action.drink_id,
                    "source_capture": source,
                    "opened_capture": opened,
                    "opened_nodes": sorted(opened_nodes),
                    "confirmation_capture": post_use,
                    "post_use_capture": confirmed,
                    "confirmation": "confirmed-effective",
                    "confirmation_node": confirm_name,
                    "observed_nodes": sorted(nodes),
                    "open_window_x": open_x,
                    "open_window_y": open_y,
                    "use_window_x": use_target[0],
                    "use_window_y": use_target[1],
                    "open_attempt_count": len(opened_clicks),
                    "use_attempt_count": len(used_clicks),
                    "click_results": [
                        *opened_clicks,
                        *used_clicks,
                        confirm_click,
                    ],
                },
            )
        return Plan2MaaInputDispatch(
            submitted=True,
            detail=(
                "MAA drink Use submitted; detail panel remains pending settlement"
                if dialog_state == "detail"
                else "MAA exact drink slot and use button submitted"
            ),
            audit={
                "mode": "maa-examsave-drink",
                "slot_index": action.slot_index,
                "instance_id": action.instance_id,
                "drink_id": action.drink_id,
                "source_capture": source,
                "opened_capture": opened,
                "opened_nodes": sorted(opened_nodes),
                "post_use_capture": post_use,
                "confirmation": (
                    "detail-pending"
                    if dialog_state == "detail"
                    else "not-present"
                ),
                "observed_nodes": sorted(nodes),
                "open_window_x": open_x,
                "open_window_y": open_y,
                "use_window_x": use_target[0],
                "use_window_y": use_target[1],
                "open_attempt_count": len(opened_clicks),
                "use_attempt_count": len(used_clicks),
                "click_results": [*opened_clicks, *used_clicks],
            },
        )


class MaaExistingEndTurnDispatcher:
    """Reuse background click plus the existing Turn End confirmation OCR."""

    def __init__(
        self,
        *,
        command_sender: CommandSender | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 0.25,
        max_samples: int = 16,
        stable_samples: int = 2,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if not 1 <= stable_samples <= max_samples:
            raise ValueError("stable_samples must be within max_samples")
        self.command_sender = command_sender
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.max_samples = max_samples
        self.stable_samples = stable_samples

    def dispatch_current(
        self,
        *,
        pre_input_authorizer: PreInputAuthorizer | None = None,
    ) -> Plan2MaaInputDispatch:
        """Resume an existing modal or begin a new END_TURN transaction."""

        from .plan3_audition_executor import (
            SKIP_BOX,
            SKIP_CONFIRM_YES_BOX,
            _skip_confirmation_evidence,
        )

        sender = _resolve_command_sender(self.command_sender)
        capture = dict(sender("capture_screen_once", timeout=15.0))
        evidence = _skip_confirmation_evidence(_capture_path(capture))
        if evidence.detected:
            if pre_input_authorizer is not None:
                pre_input_authorizer(capture)
            confirm = _suggestion_for_box(
                capture,
                label="Plan2 native resume confirm END_TURN",
                box=SKIP_CONFIRM_YES_BOX,
            )
            clicked = execute_suggested_click(
                confirm,
                command_sender=sender,
                # This button has a fixed Maa coordinate and the modal was
                # just recognized above.  Character animation must not turn
                # an otherwise valid ExamSave-CAS-bound click into a stale
                # pixel-diff rejection.
                semantic_stale_verifier=lambda _old, _new, _action: True,
            )
            return Plan2MaaInputDispatch(
                submitted=True,
                detail="existing END_TURN confirmation submitted",
                audit={
                    "resumed_existing_confirmation": True,
                    "confirmation": clicked.to_dict(),
                },
            )
        action = _suggestion_for_box(
            capture,
            label="Plan2 native END_TURN",
            box=SKIP_BOX,
        )
        return self(
            action,
            pre_input_authorizer=pre_input_authorizer,
        )

    def __call__(
        self,
        action: SuggestedClick,
        *,
        pre_input_authorizer: PreInputAuthorizer | None = None,
    ) -> Plan2MaaInputDispatch:
        from .plan3_audition_executor import (
            SKIP_BOX,
            SKIP_CONFIRM_YES_BOX,
            _skip_confirmation_evidence,
        )

        sender = _resolve_command_sender(self.command_sender)
        attempts: list[ClickExecutionResult] = []
        captures: list[Mapping[str, object]] = []
        confirmed: ClickExecutionResult | None = None
        state_changed_before_retry = False

        def submit(current: SuggestedClick) -> None:
            if pre_input_authorizer is not None:
                pre_input_authorizer(
                    {
                        "png_path": current.source_png_path,
                        "timestamp": current.source_timestamp,
                        "hwnd": current.source_hwnd,
                        "pid": current.source_pid,
                    }
                )
            clicked = execute_suggested_click(
                current,
                command_sender=sender,
                # END_TURN is a fixed Maa target.  ExamSave CAS immediately
                # above owns the turn identity; raw animation pixels are not
                # a second authority for whether the button may be clicked.
                semantic_stale_verifier=lambda _old, _new, _action: True,
            )
            attempts.append(clicked)
            captures.append(dict(clicked.post_capture))

        def poll_confirmation() -> Mapping[str, object] | None:
            stable_key: tuple[str, str] | None = None
            stable_count = 0
            for index in range(self.max_samples):
                if index:
                    self.sleep(self.poll_seconds)
                    captures.append(
                        dict(sender("capture_screen_once", timeout=15.0))
                    )
                capture = captures[-1]
                evidence = _skip_confirmation_evidence(_capture_path(capture))
                if not evidence.detected:
                    stable_key = None
                    stable_count = 0
                    continue
                key = (
                    _normalize_ui_text(evidence.title_text),
                    _normalize_ui_text(evidence.yes_text),
                )
                if key == stable_key:
                    stable_count += 1
                else:
                    stable_key = key
                    stable_count = 1
                if stable_count >= self.stable_samples:
                    return capture
            return None

        submit(action)
        confirmation_capture = poll_confirmation()
        if confirmation_capture is None and pre_input_authorizer is not None:
            # Maa's background click can occasionally land during a Unity
            # character-animation frame without opening the modal.  Retry at
            # most once, but only while ExamSave CAS proves the exact same
            # turn/hand is still current.  A changed save means the first
            # input already progressed and a second click is forbidden.
            latest = captures[-1]
            try:
                pre_input_authorizer(latest)
            except Exception:
                state_changed_before_retry = True
            else:
                retry = _suggestion_for_box(
                    latest,
                    label="Plan2 native END_TURN retry",
                    box=SKIP_BOX,
                )
                submit(retry)
                confirmation_capture = poll_confirmation()
        if confirmation_capture is not None:
            if pre_input_authorizer is not None:
                pre_input_authorizer(confirmation_capture)
            confirm = _suggestion_for_box(
                confirmation_capture,
                label="Plan2 native confirm END_TURN",
                box=SKIP_CONFIRM_YES_BOX,
            )
            confirmed = execute_suggested_click(
                confirm,
                command_sender=sender,
                semantic_stale_verifier=lambda _old, _new, _action: True,
            )
        return Plan2MaaInputDispatch(
            submitted=True,
            detail=(
                "END_TURN and stable detected confirmation submitted"
                if confirmed is not None
                else "END_TURN progressed before retry; ExamSave waiter owns result"
                if state_changed_before_retry
                else "END_TURN submitted; no stable confirmation was detected"
            ),
            audit={
                "attempts": [value.to_dict() for value in attempts],
                "confirmation_samples": len(captures),
                "confirmation": None if confirmed is None else confirmed.to_dict(),
                "state_changed_before_retry": state_changed_before_retry,
            },
        )


def build_plan2_maa_background_dependencies(
    *,
    screen_analyzer_factory: ScreenAnalyzerFactory,
    play_analyzer_factory: PlayAnalyzerFactory,
    next_settled_evidence_waiter: NextSettledEvidenceWaiter,
    command_sender: CommandSender | None = None,
    hand_box_detector: HandBoxDetector = detect_existing_maa_hand_boxes,
    end_turn_target_detector: EndTurnTargetDetector = detect_existing_maa_end_turn_action,
    logical_screen_analyzer_factory: LogicalScreenAnalyzerFactory | None = None,
    logical_play_analyzer_factory: LogicalPlayAnalyzerFactory | None = None,
    logical_next_settled_evidence_waiter: (
        LogicalNextSettledEvidenceWaiter | None
    ) = None,
    retained_settlement_waiter: RetainedSettlementWaiter | None = None,
    play_input_authorizer_factory: PlayInputAuthorizerFactory | None = None,
    drink_confirmation_effect_policy: DrinkConfirmationEffectPolicy | None = None,
    submitted_drink_replay_resolver: SubmittedDrinkReplayResolver | None = None,
    post_input_observation_sink: PostInputObservationSink | None = None,
    exam_save_minimal_verification: bool = False,
) -> Plan2MaaBackgroundDependencies:
    """Wire existing MAA primitives without capturing or clicking yet."""

    binder = (
        MaaExamSaveSlotScreenBinder(
            command_sender=command_sender,
        )
        if exam_save_minimal_verification
        else MaaPrintWindowScreenBinder(
            screen_analyzer_factory,
            hand_box_detector=hand_box_detector,
            command_sender=command_sender,
            logical_analyzer_factory=logical_screen_analyzer_factory,
        )
    )
    return Plan2MaaBackgroundDependencies(
        screen_binder=binder,
        play_analyzer_factory=play_analyzer_factory,
        play_dispatcher=(
            MaaExamSaveCardPlayDispatcher(command_sender=command_sender)
            if exam_save_minimal_verification
            else MaaVerifiedCardPlayDispatcher(command_sender=command_sender)
        ),
        end_turn_target_detector=end_turn_target_detector,
        end_turn_dispatcher=MaaExistingEndTurnDispatcher(
            command_sender=command_sender,
        ),
        next_settled_evidence_waiter=next_settled_evidence_waiter,
        drink_dispatcher=MaaExistingDrinkDispatcher(
            command_sender=command_sender,
        ),
        logical_screen_binder=(
            binder if logical_screen_analyzer_factory is not None else None
        ),
        logical_play_analyzer_factory=logical_play_analyzer_factory,
        logical_next_settled_evidence_waiter=logical_next_settled_evidence_waiter,
        retained_settlement_waiter=retained_settlement_waiter,
        pre_dispatch_surface_gate=MaaExamActionSurfaceGate(
            command_sender=command_sender,
        ),
        play_input_authorizer_factory=play_input_authorizer_factory,
        drink_confirmation_effect_policy=drink_confirmation_effect_policy,
        submitted_drink_replay_resolver=submitted_drink_replay_resolver,
        post_input_observation_sink=post_input_observation_sink,
    )


class Plan2MaaUnattendedLoopBridge:
    """Expose the adapter through the existing loop's split reader/executor API.

    The adapter itself waits for the next stable state.  This bridge caches that
    state so the loop's subsequent reader call consumes it instead of polling a
    second time.
    """

    def __init__(
        self,
        executor: Plan2MaaBackgroundActionExecutor,
        initial_evidence_reader: SettledEvidenceReader,
        *,
        initial_evidence: AuditionLocalSaveStateEvidence | None = None,
        initial_replay: Plan2CompletedLogicalReplay | None = None,
    ) -> None:
        if not isinstance(executor, Plan2MaaBackgroundActionExecutor):
            raise TypeError("executor must be Plan2MaaBackgroundActionExecutor")
        if not callable(initial_evidence_reader):
            raise TypeError("initial_evidence_reader must be callable")
        if initial_evidence is not None and not isinstance(
            initial_evidence, AuditionLocalSaveStateEvidence
        ):
            raise TypeError("initial_evidence must be typed or None")
        if initial_replay is not None and not isinstance(
            initial_replay, (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay)
        ):
            raise TypeError("initial_replay must be typed or None")
        if initial_replay is not None and (
            initial_evidence is None
            or initial_replay.persisted_transition != initial_evidence
        ):
            raise ValueError("initial replay must bind initial evidence")
        self.executor = executor
        self.initial_evidence_reader = initial_evidence_reader
        self._current: AuditionLocalSaveStateEvidence | None = initial_evidence
        self._pending: AuditionLocalSaveStateEvidence | None = None
        self._current_logical_after: Plan2NativeHorizonState | None = (
            None if initial_replay is None else initial_replay.logical_after
        )
        self._pending_logical_after: Plan2NativeHorizonState | None = None
        self._current_replay: Plan2CompletedLogicalReplay | None = initial_replay
        self._pending_replay: Plan2CompletedLogicalReplay | None = None
        self._current_runtime_provenance: RuntimeActionStateProvenance | None = None
        self._pending_runtime_provenance: RuntimeActionStateProvenance | None = None

    def settled_evidence_reader(
        self, previous: AuditionLocalSaveStateEvidence | None
    ) -> AuditionLocalSaveStateEvidence:
        if previous is None and self._current is not None:
            return self._current
        if previous is None and self._current is None:
            current = self.initial_evidence_reader(None)
            if not isinstance(current, AuditionLocalSaveStateEvidence):
                raise TypeError("initial reader returned invalid evidence")
            self._current = current
            return current
        if (
            previous is not None
            and previous == self._current
            and self._pending is not None
        ):
            self._current = self._pending
            self._pending = None
            self._current_logical_after = self._pending_logical_after
            self._pending_logical_after = None
            self._current_replay = self._pending_replay
            self._pending_replay = None
            self._current_runtime_provenance = self._pending_runtime_provenance
            self._pending_runtime_provenance = None
            return self._current
        raise RuntimeError("no adapter-verified next evidence is pending")

    def logical_state_reader(
        self, evidence: AuditionLocalSaveStateEvidence
    ) -> Plan2NativeHorizonState | None:
        """Return a replay-proven horizon only for the current persisted save."""

        if not isinstance(evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("evidence must be typed ExamSaveData evidence")
        if evidence != self._current:
            raise ValueError("logical state request does not match current evidence")
        return self._current_logical_after

    def action_executor(
        self, action: Plan2NativeOfflineAction
    ) -> Plan2NativeActionExecution:
        if self._current is None:
            raise RuntimeError("settled evidence must be read before action dispatch")
        result = self.executor.execute(
            action,
            self._current,
            logical_before=self._current_logical_after,
            prior_replay=self._current_replay,
            runtime_before_provenance=self._current_runtime_provenance,
        )
        if result.accepted:
            assert result.next_evidence is not None
            self._pending = result.next_evidence
            # A completed replay is authoritative while the physical save is
            # still the same receipt boundary, or while ExamSave retains a
            # native command queue that cannot yet be bootstrapped as an idle
            # state.  Once the game publishes a *new actionable settled*
            # LocalSave, that save owns the next decision.  Carrying the
            # simulator descendant across this boundary makes one passive or
            # item mismatch accumulate through the rest of the exam.
            retain_logical_replay = bool(
                result.next_evidence == self._current
                or not result.next_evidence.state.is_native_actionable_settled
            )
            self._pending_logical_after = (
                result.logical_after if retain_logical_replay else None
            )
            self._pending_replay = (
                result.completed_card_replay if retain_logical_replay else None
            )
            self._pending_runtime_provenance = result.runtime_provenance
        elif result.replan_evidence is not None:
            self._pending = result.replan_evidence
            if result.replan_evidence == self._current:
                # A reversible dialog cancellation changed no LocalSave
                # transaction.  Preserve any replay-proven logical horizon;
                # the next planner pass can omit the now-proven no-op drink.
                self._pending_logical_after = self._current_logical_after
                self._pending_replay = self._current_replay
                self._pending_runtime_provenance = self._current_runtime_provenance
            else:
                self._pending_logical_after = None
                self._pending_replay = None
                self._pending_runtime_provenance = None
        return result.to_unattended_execution()

    def dependencies(
        self,
        decision_orchestrator: DecisionOrchestrator,
        *,
        record_sink: RecordSink | None = None,
    ) -> Plan2NativeUnattendedDependencies:
        logical_orchestrator = getattr(
            decision_orchestrator, "logical_horizon_orchestrator", None
        )
        return Plan2NativeUnattendedDependencies(
            settled_evidence_reader=self.settled_evidence_reader,
            decision_orchestrator=decision_orchestrator,
            action_executor=self.action_executor,
            record_sink=record_sink,
            logical_state_reader=self.logical_state_reader,
            logical_decision_orchestrator=logical_orchestrator,
        )


__all__ = [
    "ExamBoundaryDisposition",
    "MaaExamActionSurfaceGate",
    "MaaPostClickHudStabilityGate",
    "MaaExistingDrinkDispatcher",
    "MaaExistingEndTurnDispatcher",
    "MaaPrintWindowScreenBinder",
    "MaaExamSaveCardPlayDispatcher",
    "MaaExamSaveSlotScreenBinder",
    "MaaVerifiedCardPlayDispatcher",
    "PLAN2_NATIVE_MAA_ACTION_EXECUTOR_SCHEMA_VERSION",
    "Plan2InputStateAdvanced",
    "Plan2SubmittedPhysicalSettlementError",
    "Plan2SubmittedPhysicalSettlementTimeout",
    "Plan2MaaActionExecutionResult",
    "Plan2MaaActionIssue",
    "Plan2MaaBackgroundActionExecutor",
    "Plan2MaaBackgroundDependencies",
    "Plan2MaaInputDispatch",
    "Plan2MaaScreenBinding",
    "Plan2MaaUnattendedLoopBridge",
    "Plan2StableSettledEvidence",
    "build_plan2_maa_background_dependencies",
    "detect_existing_maa_end_turn_action",
    "detect_existing_maa_hand_boxes",
    "validate_screen_semantic_against_logical_horizon",
]
