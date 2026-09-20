"""Persistent retained-queue replay journal for Plan2 unattended lessons.

The game can keep ``ExamSaveData`` at a pre-effect command queue while the UI
already accepts another card.  In memory, :mod:`plan2_card_history` carries the
logical post-queue horizon.  This module persists only the evidence/action/HUD
facts needed to rebuild that same chain after a process restart; it never
guesses a stamina/block split from the latest retained save.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
from typing import Final, Mapping

from .audition_local_save_state import AuditionLocalSaveStateEvidence
from .audition_native_ordered_zones import NativeOrderedCardInstance
from .master_db import DEFAULT_DATABASE
from .plan2_card_history import (
    PLAN2_HUD_TURNS_RAW_REMAIN_V2,
    Plan2CardHistoryIssue,
    Plan2CardHistoryObservation,
    Plan2CardHistoryObservationAuthority,
    Plan2CardHistoryReplayResult,
    Plan2CompletedCardReplay,
    Plan2CompletedDrinkReplay,
    Plan2CompletedLogicalReplay,
    Plan2DrinkHistoryReplayResult,
    recover_retained_plan2_drink_history,
    replay_completed_plan2_card_history,
    replay_next_completed_plan2_card_history,
)
from .plan2_native_exam_save_orchestrator import (
    Plan2NativeExamSaveDependencies,
    decide_plan2_native_exam_save,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
)
from .plan2_native_program_catalog import compile_plan2_native_program_catalog


PLAN2_REPLAY_JOURNAL_SCHEMA_VERSION: Final = 1
PLAN2_PENDING_DRINK_RECEIPT_SCHEMA_VERSION: Final = 2
DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT: Final = Path("var/plan2_replay_journal")


class Plan2ReplayJournalError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class PendingReplayDisposition(StrEnum):
    """One control-safe interpretation of an exact pending PLAY replay."""

    REPLAYED = "replayed"
    WAIT_EXTERNAL_SETTLEMENT = "wait-external-settlement"
    REJECTED = "rejected"
    RESOLVER_UNAVAILABLE = "resolver-unavailable"


@dataclass(frozen=True, slots=True)
class PendingReplayResult:
    """Preserve replay issues while deciding whether physical waiting is valid."""

    disposition: PendingReplayDisposition
    replay: Plan2CompletedLogicalReplay | None = None
    issues: tuple[Plan2CardHistoryIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, PendingReplayDisposition):
            raise TypeError("pending replay disposition must be typed")
        issues = tuple(self.issues)
        if any(not isinstance(value, Plan2CardHistoryIssue) for value in issues):
            raise TypeError("pending replay issues must be typed")
        object.__setattr__(self, "issues", issues)
        if self.disposition is PendingReplayDisposition.REPLAYED:
            if not isinstance(
                self.replay,
                (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay),
            ) or issues:
                raise ValueError("replayed pending result must contain only a replay")
        elif self.replay is not None or not issues:
            raise ValueError("unreplayed pending result must contain only issues")


_PENDING_REPLAY_EXTERNAL_SETTLEMENT_ISSUES: Final = frozenset(
    {
        "plan2-card-history-item-runtime-unresolved",
        "plan2-card-history-item-pre-effect-runtime-mismatch",
    }
)


def classify_pending_replay_result(
    result: Plan2CardHistoryReplayResult | Plan2DrinkHistoryReplayResult | None,
) -> PendingReplayResult:
    """Classify replay once; only proven external-item gaps may physically wait."""

    if result is None:
        return PendingReplayResult(
            PendingReplayDisposition.RESOLVER_UNAVAILABLE,
            issues=(
                Plan2CardHistoryIssue(
                    "plan2-pending-play-replay-resolver-unavailable"
                ),
            ),
        )
    if not isinstance(
        result,
        (Plan2CardHistoryReplayResult, Plan2DrinkHistoryReplayResult),
    ):
        raise TypeError("pending replay classifier requires a typed replay result")
    if result.replay is not None:
        return PendingReplayResult(
            PendingReplayDisposition.REPLAYED,
            replay=result.replay,
        )
    disposition = (
        PendingReplayDisposition.WAIT_EXTERNAL_SETTLEMENT
        if result.issues
        and all(
            value.code in _PENDING_REPLAY_EXTERNAL_SETTLEMENT_ISSUES
            for value in result.issues
        )
        else PendingReplayDisposition.REJECTED
    )
    return PendingReplayResult(disposition, issues=result.issues)


def _is_successful_production_maa_click(value: object) -> bool:
    """Recognize the exact successful Maa click receipt returned in production."""

    if not isinstance(value, Mapping):
        return False
    if value.get("method") != "MaaFramework.SendMessage":
        return False
    if value.get("submitted") is False:
        return False
    return all(
        type(value.get(field)) is int
        for field in ("maa_x", "maa_y", "window_x", "window_y")
    )


@dataclass(frozen=True, slots=True)
class Plan2PendingDrinkDispatchWitness:
    """Minimal Maa evidence that one drink transaction was truly submitted.

    A normal drink has slot+Use clicks and a Maa batch proving no pending
    Cancel dialog.  An effective mixed drink may additionally have an exact
    Maa Confirm click.  The captures are identity/timing evidence only; no
    OCR, colour, or score revalidation is added here.
    """

    source_capture: Mapping[str, object]
    opened_capture: Mapping[str, object]
    post_use_capture: Mapping[str, object]

    def __post_init__(self) -> None:
        normalized = []
        for name in ("source_capture", "opened_capture", "post_use_capture"):
            raw = getattr(self, name)
            if not isinstance(raw, Mapping):
                raise TypeError(f"{name} must be a mapping")
            expected = {"png_path", "timestamp", "hwnd", "pid"}
            if set(raw) != expected:
                raise ValueError(f"{name} shape is invalid")
            path = raw["png_path"]
            timestamp = raw["timestamp"]
            hwnd = raw["hwnd"]
            pid = raw["pid"]
            if (
                not isinstance(path, str)
                or not path
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or isinstance(hwnd, bool)
                or not isinstance(hwnd, int)
                or hwnd <= 0
                or isinstance(pid, bool)
                or not isinstance(pid, int)
                or pid <= 0
            ):
                raise ValueError(f"{name} identity is invalid")
            normalized.append(
                {
                    "png_path": path,
                    "timestamp": float(timestamp),
                    "hwnd": hwnd,
                    "pid": pid,
                }
            )
        source, opened, post_use = normalized
        if not (
            source["hwnd"] == opened["hwnd"] == post_use["hwnd"]
            and source["pid"] == opened["pid"] == post_use["pid"]
            and source["timestamp"] < opened["timestamp"] < post_use["timestamp"]
            and opened["png_path"] != post_use["png_path"]
        ):
            raise ValueError("drink dispatch captures do not form one ordered window")
        object.__setattr__(self, "source_capture", source)
        object.__setattr__(self, "opened_capture", opened)
        object.__setattr__(self, "post_use_capture", post_use)

    def to_dict(self) -> dict[str, object]:
        return {
            "source_capture": dict(self.source_capture),
            "opened_capture": dict(self.opened_capture),
            "post_use_capture": dict(self.post_use_capture),
        }

    @classmethod
    def from_dispatch(
        cls,
        action: Plan2NativeDrinkAction,
        dispatch: Mapping[str, object],
    ) -> "Plan2PendingDrinkDispatchWitness":
        if not isinstance(dispatch, Mapping):
            raise Plan2ReplayJournalError("pending-drink-dispatch-witness-invalid")
        audit = dispatch.get("audit")
        if (
            dispatch.get("submitted") is not True
            or not isinstance(audit, Mapping)
            or audit.get("mode") != "maa-examsave-drink"
            or audit.get("slot_index") != action.slot_index
            or audit.get("instance_id") != action.instance_id
            or audit.get("drink_id") != action.drink_id
        ):
            raise Plan2ReplayJournalError("pending-drink-dispatch-witness-invalid")
        confirmation = audit.get("confirmation")
        observed_nodes = audit.get("observed_nodes")
        opened_nodes = audit.get("opened_nodes")
        open_attempt_count = audit.get("open_attempt_count")
        use_attempt_count = audit.get("use_attempt_count")
        event_driven_witness = any(
            value is not None
            for value in (
                opened_nodes,
                open_attempt_count,
                use_attempt_count,
            )
        )
        if event_driven_witness:
            if (
                not isinstance(opened_nodes, list)
                or "drink-use" not in opened_nodes
                or any(not isinstance(value, str) for value in opened_nodes)
                or type(open_attempt_count) is not int
                or not 1 <= open_attempt_count <= 8
                or type(use_attempt_count) is not int
                or not 1 <= use_attempt_count <= 2
            ):
                raise Plan2ReplayJournalError(
                    "pending-drink-dispatch-witness-invalid"
                )
            expected_click_count = (
                open_attempt_count
                + use_attempt_count
                + int(confirmation == "confirmed-effective")
            )
        else:
            # Schema-v2 legacy receipts predate the event-driven panel gate.
            # Preserve their exact historical 2/3-click grammar for restart;
            # all newly submitted dispatches carry the stronger fields above.
            expected_click_count = {
                "not-present": 2,
                "confirmed-effective": 3,
            }.get(confirmation)
        clicks = audit.get("click_results")
        if (
            expected_click_count is None
            or not isinstance(clicks, list)
            or len(clicks) != expected_click_count
            or any(not _is_successful_production_maa_click(value) for value in clicks)
            or not isinstance(observed_nodes, list)
            or any(not isinstance(value, str) for value in observed_nodes)
        ):
            raise Plan2ReplayJournalError("pending-drink-dispatch-witness-invalid")

        def capture(name: str) -> Mapping[str, object]:
            raw = audit.get(name)
            if not isinstance(raw, Mapping):
                raise Plan2ReplayJournalError(
                    "pending-drink-dispatch-witness-invalid"
                )
            return {
                key: raw.get(key)
                for key in ("png_path", "timestamp", "hwnd", "pid")
            }

        try:
            source_capture = capture("source_capture")
            opened_capture = capture("opened_capture")
            post_use_capture = capture("post_use_capture")
            if confirmation == "not-present":
                if "common-cancel" in observed_nodes or (
                    event_driven_witness and "drink-use" in observed_nodes
                ):
                    raise ValueError("unresolved drink confirmation was observed")
            else:
                confirmation_node = audit.get("confirmation_node")
                confirmation_capture = capture("confirmation_capture")
                if (
                    confirmation_node not in {"common-decide", "decide"}
                    or "common-cancel" not in observed_nodes
                    or confirmation_node not in observed_nodes
                    or confirmation_capture["hwnd"] != source_capture["hwnd"]
                    or confirmation_capture["pid"] != source_capture["pid"]
                    or not (
                        float(opened_capture["timestamp"])
                        < float(confirmation_capture["timestamp"])
                        < float(post_use_capture["timestamp"])
                    )
                ):
                    raise ValueError("confirmed drink dialog witness is invalid")
            return cls(
                source_capture,
                opened_capture,
                post_use_capture,
            )
        except (TypeError, ValueError) as error:
            raise Plan2ReplayJournalError(
                "pending-drink-dispatch-witness-invalid",
                f"{type(error).__name__}:{error}",
            ) from error


@dataclass(frozen=True, slots=True)
class Plan2PendingDrinkReceipt:
    """Durable authority written immediately after Maa submits one DRINK."""

    persisted_before: AuditionLocalSaveStateEvidence
    action: Plan2NativeDrinkAction
    prior: "Plan2PendingDrinkReceipt | None" = None
    dispatch_witness: Plan2PendingDrinkDispatchWitness | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.persisted_before, AuditionLocalSaveStateEvidence):
            raise TypeError("persisted_before must be typed evidence")
        if not isinstance(self.action, Plan2NativeDrinkAction):
            raise TypeError("action must be a typed drink action")
        if self.prior is not None:
            if not isinstance(self.prior, Plan2PendingDrinkReceipt):
                raise TypeError("prior must be a typed pending drink receipt")
            if self.prior.prior is not None:
                raise ValueError("pending drink receipt supports one barrier predecessor")
        if self.dispatch_witness is not None and not isinstance(
            self.dispatch_witness, Plan2PendingDrinkDispatchWitness
        ):
            raise TypeError("dispatch_witness must be typed or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN2_PENDING_DRINK_RECEIPT_SCHEMA_VERSION,
            "persisted_before": self.persisted_before.to_dict(),
            "action": {
                "kind": "drink",
                "slot_index": self.action.slot_index,
                "instance_id": self.action.instance_id,
                "drink_id": self.action.drink_id,
                "selected_card_guid": self.action.selected_card_guid,
            },
            "prior": None if self.prior is None else self.prior.to_dict(),
            "dispatch_witness": (
                None
                if self.dispatch_witness is None
                else self.dispatch_witness.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "Plan2PendingDrinkReceipt":
        if not isinstance(raw, Mapping):
            raise Plan2ReplayJournalError("pending-drink-row-shape-invalid")
        schema = raw.get("schema_version")
        expected_fields = {
            "schema_version",
            "persisted_before",
            "action",
        }
        if schema == PLAN2_PENDING_DRINK_RECEIPT_SCHEMA_VERSION:
            expected_fields.update(("prior", "dispatch_witness"))
        elif schema != 1:
            raise Plan2ReplayJournalError("pending-drink-schema-unsupported")
        if set(raw) != expected_fields:
            raise Plan2ReplayJournalError("pending-drink-row-shape-invalid")
        before_raw = raw["persisted_before"]
        action_raw = raw["action"]
        if not isinstance(before_raw, Mapping) or not isinstance(
            action_raw, Mapping
        ):
            raise Plan2ReplayJournalError("pending-drink-row-shape-invalid")
        expected_action_fields = {
            "kind",
            "slot_index",
            "instance_id",
            "drink_id",
            "selected_card_guid",
        }
        if set(action_raw) != expected_action_fields or action_raw["kind"] != "drink":
            raise Plan2ReplayJournalError("pending-drink-action-invalid")
        selected_card_guid = action_raw["selected_card_guid"]
        if selected_card_guid != "":
            raise Plan2ReplayJournalError("pending-drink-action-invalid")
        prior = None
        witness = None
        if schema == PLAN2_PENDING_DRINK_RECEIPT_SCHEMA_VERSION:
            prior_raw = raw["prior"]
            witness_raw = raw["dispatch_witness"]
            if prior_raw is not None:
                if not isinstance(prior_raw, Mapping):
                    raise Plan2ReplayJournalError(
                        "pending-drink-row-shape-invalid"
                    )
                prior = cls.from_dict(prior_raw)
            if witness_raw is not None:
                if not isinstance(witness_raw, Mapping):
                    raise Plan2ReplayJournalError(
                        "pending-drink-row-shape-invalid"
                    )
                try:
                    witness = Plan2PendingDrinkDispatchWitness(
                        witness_raw.get("source_capture"),
                        witness_raw.get("opened_capture"),
                        witness_raw.get("post_use_capture"),
                    )
                except (TypeError, ValueError) as error:
                    raise Plan2ReplayJournalError(
                        "pending-drink-dispatch-witness-invalid",
                        f"{type(error).__name__}:{error}",
                    ) from error
        return cls(
            AuditionLocalSaveStateEvidence.from_dict(before_raw),
            Plan2NativeDrinkAction(
                _integer(action_raw["slot_index"], "action.slot_index"),
                _text(action_raw["instance_id"], "action.instance_id"),
                _text(action_raw["drink_id"], "action.drink_id"),
                selected_card_guid,
            ),
            prior,
            witness,
        )


PLAN2_PENDING_PLAY_RECEIPT_SCHEMA_VERSION = 2
PENDING_PLAY_BEFORE_NATIVE_SETTLED: Final = "native-settled"
PENDING_PLAY_BEFORE_LOGICAL_CONTINUATION: Final = "logical-continuation"


def _pending_play_dispatch_identity(
    action: Plan2NativeAction,
    hand_slot: int,
    dispatch: Mapping[str, object],
) -> dict[str, object]:
    audit = dispatch.get("audit") if isinstance(dispatch, Mapping) else None
    if dispatch.get("submitted") is not True or not isinstance(audit, Mapping):
        raise Plan2ReplayJournalError("pending-play-dispatch-witness-invalid")
    mode = audit.get("mode")
    capture = audit.get("source_capture")
    if (
        mode not in {
            "maa-examsave-minimal",
            "maa-examsave-minimal-single-recovery",
        }
        or type(hand_slot) is not int
        or hand_slot < 0
        or not isinstance(capture, Mapping)
        or type(capture.get("pid")) is not int
        or capture.get("pid", 0) <= 0
        or type(capture.get("hwnd")) is not int
        or capture.get("hwnd", 0) <= 0
        or (
            mode == "maa-examsave-minimal-single-recovery"
            and audit.get("expected_slot") != hand_slot
        )
    ):
        raise Plan2ReplayJournalError("pending-play-dispatch-witness-invalid")
    return {
        "mode": mode,
        "action_guid": action.card_guid,
        "hand_slot": hand_slot,
        "pid": capture["pid"],
        "hwnd": capture["hwnd"],
    }


@dataclass(frozen=True, slots=True)
class Plan2PendingPlayReceipt:
    """One durable PLAY owner with an optional cleanup-only settled marker.

    ``state_after`` is not a training transition.  It is written only after
    the nested journal has reconciled to native settled truth, and exists so a
    restart can finish artifact cleanup without reconstructing removed
    predecessor files.
    """

    before_authority: str
    persisted_before: AuditionLocalSaveStateEvidence
    prior_replay_identity: Mapping[str, object] | None
    action: Plan2NativeAction
    submitted_evidence: AuditionLocalSaveStateEvidence
    dispatch_witness: Mapping[str, object]
    state_after: AuditionLocalSaveStateEvidence | None = None

    def __post_init__(self) -> None:
        if self.before_authority not in {
            PENDING_PLAY_BEFORE_NATIVE_SETTLED,
            PENDING_PLAY_BEFORE_LOGICAL_CONTINUATION,
        }:
            raise ValueError("pending PLAY before authority is invalid")
        if not isinstance(self.persisted_before, AuditionLocalSaveStateEvidence):
            raise TypeError("pending PLAY before must be typed")
        if not isinstance(self.action, Plan2NativeAction) or self.action.kind != "play":
            raise TypeError("pending PLAY action must be typed")
        if not isinstance(self.submitted_evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("pending PLAY submitted evidence must be typed")
        dispatch = dict(self.dispatch_witness)
        if (
            set(dispatch) != {"mode", "action_guid", "hand_slot", "pid", "hwnd"}
            or dispatch.get("action_guid") != self.action.card_guid
            or dispatch.get("mode") not in {
                "maa-examsave-minimal",
                "maa-examsave-minimal-single-recovery",
            }
            or type(dispatch.get("hand_slot")) is not int
            or dispatch.get("hand_slot", -1) < 0
            or type(dispatch.get("pid")) is not int
            or dispatch.get("pid", 0) <= 0
            or type(dispatch.get("hwnd")) is not int
            or dispatch.get("hwnd", 0) <= 0
        ):
            raise ValueError("pending PLAY action and dispatch differ")
        prior = (
            None
            if self.prior_replay_identity is None
            else dict(self.prior_replay_identity)
        )
        if self.before_authority == PENDING_PLAY_BEFORE_NATIVE_SETTLED:
            if prior is not None:
                raise ValueError("native-settled pending PLAY has logical authority")
        elif prior is None:
            raise ValueError("logical-continuation pending PLAY authority is incomplete")
        object.__setattr__(self, "prior_replay_identity", prior)
        object.__setattr__(self, "dispatch_witness", dispatch)
        if self.state_after is not None:
            if not isinstance(self.state_after, AuditionLocalSaveStateEvidence):
                raise TypeError("pending PLAY cleanup marker must be typed")
            runtime = self.state_after.state.root_runtime
            if not (
                self.state_after.state.is_native_actionable_settled
                or (runtime is not None and runtime.is_exam_end_complete)
            ):
                raise ValueError("pending PLAY cleanup marker must be settled")
            identity_fields = (
                "run_id",
                "step_context_id",
                "step_context_digest",
                "session_transition_id",
                "source_path",
                "source_type",
            )
            if any(
                getattr(self.persisted_before, name)
                != getattr(self.state_after, name)
                for name in identity_fields
            ):
                raise ValueError("pending PLAY cleanup marker identity differs")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PLAN2_PENDING_PLAY_RECEIPT_SCHEMA_VERSION,
            "before_authority": self.before_authority,
            "persisted_before": self.persisted_before.to_dict(),
            "prior_replay_identity": self.prior_replay_identity,
            "action": {"kind": "play", "card_guid": self.action.card_guid},
            "submitted_evidence": self.submitted_evidence.to_dict(),
            "dispatch_witness": dict(self.dispatch_witness),
            "state_after": (
                None if self.state_after is None else self.state_after.to_dict()
            ),
            "reward": None,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "Plan2PendingPlayReceipt":
        if raw.get("schema_version") != PLAN2_PENDING_PLAY_RECEIPT_SCHEMA_VERSION:
            raise Plan2ReplayJournalError("pending-play-schema-unsupported")
        expected = {
            "schema_version", "before_authority", "persisted_before",
            "prior_replay_identity", "action",
            "submitted_evidence", "dispatch_witness", "state_after", "reward",
        }
        if set(raw) != expected or raw["reward"] is not None:
            raise Plan2ReplayJournalError("pending-play-row-shape-invalid")
        action = raw["action"]
        if not isinstance(action, Mapping) or set(action) != {"kind", "card_guid"} or action.get("kind") != "play":
            raise Plan2ReplayJournalError("pending-play-action-invalid")
        before = raw["persisted_before"]
        submitted = raw["submitted_evidence"]
        witness = raw["dispatch_witness"]
        prior = raw["prior_replay_identity"]
        state_after = raw["state_after"]
        if (
            not isinstance(before, Mapping)
            or not isinstance(submitted, Mapping)
            or not isinstance(witness, Mapping)
            or (prior is not None and not isinstance(prior, Mapping))
            or (state_after is not None and not isinstance(state_after, Mapping))
        ):
            raise Plan2ReplayJournalError("pending-play-row-shape-invalid")
        return cls(
            str(raw["before_authority"]),
            AuditionLocalSaveStateEvidence.from_dict(before),
            prior,
            Plan2NativeAction("play", _text(action.get("card_guid"), "action.card_guid")),
            AuditionLocalSaveStateEvidence.from_dict(submitted),
            witness,
            (
                None
                if state_after is None
                else AuditionLocalSaveStateEvidence.from_dict(state_after)
            ),
        )


def _pending_play_prior_replay_identity(
    replay: Plan2CompletedLogicalReplay,
) -> dict[str, object]:
    return {
        "kind": (
            "card" if isinstance(replay, Plan2CompletedCardReplay) else "drink"
        ),
        "persisted_before_digest": replay.persisted_before.digest(),
        "persisted_transition_digest": replay.persisted_transition.digest(),
        "action_id": replay.action.action_id,
    }


def _pending_play_stage_compatible(
    before: AuditionLocalSaveStateEvidence,
    candidate: AuditionLocalSaveStateEvidence,
    *,
    logical_before: Plan2NativeHorizonState | None = None,
) -> bool:
    """Match one submitted PLAY to its physical or logical stage owner."""

    immutable_fields = (
        "character_id",
        "setting_id",
        "exam_type",
        "step_type_value",
        "limit_turn",
        "max_stamina",
    )
    if any(
        getattr(before.state, name) != getattr(candidate.state, name)
        for name in immutable_fields
    ):
        return False

    if logical_before is None:
        progression_fields = (
            "current_turn",
            "remain_turn",
            "extra_turn",
            "turn_parameter_types",
        )
        return not any(
            getattr(before.state, name) != getattr(candidate.state, name)
            for name in progression_fields
        )

    if (
        candidate.state.current_turn != logical_before.scalar.current_turn
        or candidate.state.remain_turn != logical_before.remaining_turns
        or candidate.state.extra_turn != logical_before.extra_turn
    ):
        return False
    added_turns = candidate.state.extra_turn - before.state.extra_turn
    before_schedule = before.state.turn_parameter_types
    candidate_schedule = candidate.state.turn_parameter_types
    return bool(
        added_turns >= 0
        and len(candidate_schedule) == len(before_schedule) + added_turns
        and candidate_schedule[: len(before_schedule)] == before_schedule
    )


def prove_plan2_submitted_play_acceptance(
    before: AuditionLocalSaveStateEvidence,
    action: Plan2NativeAction,
    submitted: AuditionLocalSaveStateEvidence,
    *,
    logical_before: Plan2NativeHorizonState | None = None,
    prior_replay: Plan2CompletedLogicalReplay | None = None,
) -> tuple[str, ...]:
    """Prove accepted native PLAY ownership without claiming settlement."""

    logical_continuation = logical_before is not None or prior_replay is not None
    if action.kind != "play" or (
        logical_continuation
        and (
            logical_before is None
            or prior_replay is None
            or prior_replay.persisted_transition != before
            or prior_replay.logical_after != logical_before
        )
    ):
        raise Plan2ReplayJournalError("pending-play-before-invalid")
    if not logical_continuation and not before.state.is_native_actionable_settled:
        raise Plan2ReplayJournalError("pending-play-before-invalid")
    identity_fields = (
        "run_id", "step_context_id", "step_context_digest",
        "session_transition_id", "source_path", "source_type",
    )
    if any(getattr(before, name) != getattr(submitted, name) for name in identity_fields):
        raise Plan2ReplayJournalError("pending-play-run-identity-mismatch")
    if not _pending_play_stage_compatible(
        before,
        submitted,
        logical_before=logical_before,
    ):
        raise Plan2ReplayJournalError("pending-play-stage-mismatch")
    runtime = submitted.state.root_runtime
    playing = submitted.state.playing_card
    if (
        submitted == before
        or submitted.state.is_native_actionable_settled
        or playing is None
        or playing.guid != action.card_guid
        or runtime is None
        or runtime.command_list_is_empty
        or runtime.is_exam_end_complete
    ):
        raise Plan2ReplayJournalError("pending-play-native-witness-invalid")
    source_hand = (
        before.state.zones.hand
        if logical_before is None
        else logical_before.zones.hand
    )
    selected = tuple(card for card in source_hand if card.guid == action.card_guid)
    if len(selected) != 1:
        raise Plan2ReplayJournalError("pending-play-before-hand-mismatch")
    try:
        selected_native = (
            NativeOrderedCardInstance.from_local_save(selected[0])
            if logical_before is None
            else selected[0]
        )
        playing_native = NativeOrderedCardInstance.from_local_save(playing)
    except (TypeError, ValueError) as error:
        raise Plan2ReplayJournalError(
            "pending-play-card-runtime-mismatch", str(error)
        ) from error
    if (
        playing_native.guid != selected_native.guid
        or playing_native.card_id != selected_native.card_id
        or playing_native.base_upgrade != selected_native.base_upgrade
        or playing_native.fixed_deck_order != selected_native.fixed_deck_order
        or playing_native.runtime_state.play_count
        <= selected_native.runtime_state.play_count
    ):
        raise Plan2ReplayJournalError("pending-play-card-runtime-mismatch")
    return (
        "pending-play:matching-playing-guid",
        "pending-play:logical-hand-identity",
        "pending-play:play-count-forward",
        "pending-play:typed-command-queue",
    )


def recover_materialized_plan2_pending_drink_chain(
    current: AuditionLocalSaveStateEvidence,
    receipt: Plan2PendingDrinkReceipt,
    *,
    observation: Plan2CardHistoryObservation,
    catalog: Plan2NativeProgramCatalog | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2DrinkHistoryReplayResult:
    """Recover an exact receipt-owned drink queue once it reached LocalSave.

    With one receipt, ``persisted_before`` is the settled planning root and
    ``current`` is its retained native drink queue.  With a chained receipt,
    the prior queue first supplies the logical root for the current queue.
    The native queue plus its ordered inventory removal is itself the commit
    witness.  A Maa dispatch witness is required only while LocalSave still
    shows the predecessor queue, so an interrupted receipt write can be
    repaired without inventing capture metadata after the new queue appears.
    """

    if not isinstance(current, AuditionLocalSaveStateEvidence):
        raise TypeError("current must be typed evidence")
    if not isinstance(receipt, Plan2PendingDrinkReceipt):
        raise TypeError("receipt must be typed")
    if not isinstance(observation, Plan2CardHistoryObservation):
        raise TypeError("observation must be typed")
    if current == receipt.persisted_before:
        raise Plan2ReplayJournalError(
            "pending-drink-materialized-chain-authority-invalid"
        )
    if catalog is None:
        catalog = compile_plan2_native_program_catalog(database=database).catalog
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be typed")

    if receipt.prior is None:
        decision = decide_plan2_native_exam_save(
            receipt.persisted_before,
            database=database,
        )
        before_horizon = _journal_replay_root(decision)
        if before_horizon is None:
            raise Plan2ReplayJournalError(
                "pending-drink-materialized-before-bootstrap-failed",
                ",".join(value.code for value in decision.blockers),
            )
    else:
        prior = recover_retained_plan2_drink_history(
            receipt.persisted_before,
            observation=observation,
            catalog=catalog,
            database=database,
            expected_action=receipt.prior.action,
            persisted_before=receipt.prior.persisted_before,
        )
        if not prior.supported or prior.replay is None:
            detail = ",".join(
                value.code + (":" + value.detail if value.detail else "")
                for value in prior.issues
            )
            raise Plan2ReplayJournalError(
                "pending-drink-materialized-prior-recovery-failed",
                detail,
            )
        before_horizon = prior.replay.logical_after
    return recover_retained_plan2_drink_history(
        current,
        observation=observation,
        catalog=catalog,
        database=database,
        expected_action=receipt.action,
        persisted_before=receipt.persisted_before,
        before_horizon=before_horizon,
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Plan2ReplayJournalError("journal-field-invalid", label)
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Plan2ReplayJournalError("journal-field-invalid", label)
    return value


@dataclass(frozen=True, slots=True)
class Plan2ReplayJournalEntry:
    persisted_before: AuditionLocalSaveStateEvidence
    persisted_transition: AuditionLocalSaveStateEvidence
    action: Plan2NativeAction
    observation: Plan2CardHistoryObservation

    def __post_init__(self) -> None:
        if not isinstance(self.persisted_before, AuditionLocalSaveStateEvidence):
            raise TypeError("persisted_before must be typed evidence")
        if not isinstance(
            self.persisted_transition, AuditionLocalSaveStateEvidence
        ):
            raise TypeError("persisted_transition must be typed evidence")
        if not isinstance(self.action, Plan2NativeAction) or self.action.kind != "play":
            raise TypeError("journal action must be a Plan2 play action")
        if not isinstance(self.observation, Plan2CardHistoryObservation):
            raise TypeError("observation must be typed")

    @property
    def transition_digest(self) -> str:
        return self.persisted_transition.digest()

    def to_dict(self) -> dict[str, object]:
        observation = self.observation
        return {
            "schema_version": PLAN2_REPLAY_JOURNAL_SCHEMA_VERSION,
            "persisted_before": self.persisted_before.to_dict(),
            "persisted_transition": self.persisted_transition.to_dict(),
            "action": {
                "kind": self.action.kind,
                "card_guid": self.action.card_guid,
            },
            "observation": {
                "turns_remaining": observation.turns_remaining,
                "score": observation.score,
                "stamina": observation.stamina,
                "block": observation.block,
                "review": observation.review,
                "aggressive": observation.aggressive,
                "plays_remaining": observation.plays_remaining,
                "source": observation.source,
                "score_authoritative": observation.score_authoritative,
                "authority": observation.authority.value,
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "Plan2ReplayJournalEntry":
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version",
            "persisted_before",
            "persisted_transition",
            "action",
            "observation",
        }:
            raise Plan2ReplayJournalError("journal-row-shape-invalid")
        if raw["schema_version"] != PLAN2_REPLAY_JOURNAL_SCHEMA_VERSION:
            raise Plan2ReplayJournalError("journal-schema-unsupported")
        before_raw = raw["persisted_before"]
        transition_raw = raw["persisted_transition"]
        action_raw = raw["action"]
        observation_raw = raw["observation"]
        if not all(
            isinstance(value, Mapping)
            for value in (before_raw, transition_raw, action_raw, observation_raw)
        ):
            raise Plan2ReplayJournalError("journal-row-shape-invalid")
        assert isinstance(before_raw, Mapping)
        assert isinstance(transition_raw, Mapping)
        assert isinstance(action_raw, Mapping)
        assert isinstance(observation_raw, Mapping)
        if set(action_raw) != {"kind", "card_guid"} or action_raw["kind"] != "play":
            raise Plan2ReplayJournalError("journal-action-invalid")
        expected_observation = {
            "turns_remaining",
            "score",
            "stamina",
            "block",
            "review",
            "aggressive",
            "plays_remaining",
            "source",
            "score_authoritative",
        }
        if frozenset(observation_raw) not in {
            frozenset(expected_observation),
            frozenset((*expected_observation, "authority")),
        }:
            raise Plan2ReplayJournalError("journal-observation-shape-invalid")
        optional_values: dict[str, int | None] = {}
        for name in ("review", "aggressive", "plays_remaining"):
            value = observation_raw[name]
            optional_values[name] = (
                None if value is None else _integer(value, f"observation.{name}")
            )
        score_authoritative = observation_raw["score_authoritative"]
        if not isinstance(score_authoritative, bool):
            raise Plan2ReplayJournalError(
                "journal-field-invalid", "observation.score_authoritative"
            )
        try:
            authority = Plan2CardHistoryObservationAuthority(
                observation_raw.get("authority", "diagnostic")
            )
        except (TypeError, ValueError) as error:
            raise Plan2ReplayJournalError(
                "journal-field-invalid", "observation.authority"
            ) from error
        return cls(
            AuditionLocalSaveStateEvidence.from_dict(before_raw),
            AuditionLocalSaveStateEvidence.from_dict(transition_raw),
            Plan2NativeAction("play", _text(action_raw["card_guid"], "card_guid")),
            Plan2CardHistoryObservation(
                turns_remaining=_integer(
                    observation_raw["turns_remaining"],
                    "observation.turns_remaining",
                ),
                score=_integer(observation_raw["score"], "observation.score"),
                stamina=_integer(
                    observation_raw["stamina"], "observation.stamina"
                ),
                block=_integer(observation_raw["block"], "observation.block"),
                review=optional_values["review"],
                aggressive=optional_values["aggressive"],
                plays_remaining=optional_values["plays_remaining"],
                source=_text(observation_raw["source"], "observation.source"),
                score_authoritative=score_authoritative,
                authority=authority,
            ),
        )


@dataclass(frozen=True, slots=True)
class Plan2SettledCardJournalProof:
    """Exact proof that one retained PLAY drained into a fresh settled save.

    This proof is intentionally separate from ``Plan2CompletedCardReplay``.
    A retained journal observation can be captured while passive native
    listeners are still running, so its scalar values need not describe the
    later TurnStart save.  The durable playing-card queue proves the submitted
    input; this object records the independent structural reconciliation that
    permits the caller to retire that journal and bootstrap the settled save.
    """

    action: Plan2NativeAction
    persisted_before: AuditionLocalSaveStateEvidence
    persisted_transition: AuditionLocalSaveStateEvidence
    settled_current: AuditionLocalSaveStateEvidence
    fresh_root: Plan2NativeHorizonState
    trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.action.kind != "play":
            raise ValueError("settled journal proof action must be PLAY")
        for name in (
            "persisted_before",
            "persisted_transition",
            "settled_current",
        ):
            if not isinstance(
                getattr(self, name), AuditionLocalSaveStateEvidence
            ):
                raise TypeError(f"{name} must be typed evidence")
        if not isinstance(self.fresh_root, Plan2NativeHorizonState):
            raise TypeError("fresh_root must be a typed horizon")
        if not self.trace or any(
            not isinstance(value, str) or not value for value in self.trace
        ):
            raise ValueError("settled journal proof trace is invalid")


@dataclass(frozen=True, slots=True)
class Plan2SettledJournalChainProof:
    """Native proof that two or more journalled PLAYs fully settled.

    Restart idempotency uses the journalled GUIDs, native ``userPlayLogList``,
    and a fresh settled bootstrap.  It does not require the local simulator to
    reproduce every intermediate RNG, support-hand-add, item, or listener
    mutation before deciding that an already-submitted action must not repeat.
    """

    actions: tuple[Plan2NativeAction, ...]
    journal_entries: tuple[Plan2ReplayJournalEntry, ...]
    settled_current: AuditionLocalSaveStateEvidence
    fresh_root: Plan2NativeHorizonState
    trace: tuple[str, ...]

    def __post_init__(self) -> None:
        actions = tuple(self.actions)
        entries = tuple(self.journal_entries)
        if len(actions) < 2 or len(actions) != len(entries):
            raise ValueError("settled journal chain proof requires aligned PLAYs")
        if any(action.kind != "play" for action in actions):
            raise ValueError("settled journal chain proof actions must be PLAY")
        if any(
            not isinstance(entry, Plan2ReplayJournalEntry) for entry in entries
        ):
            raise TypeError("journal_entries must be typed")
        if not isinstance(
            self.settled_current, AuditionLocalSaveStateEvidence
        ):
            raise TypeError("settled_current must be typed evidence")
        if not isinstance(self.fresh_root, Plan2NativeHorizonState):
            raise TypeError("fresh_root must be a typed horizon")
        if not self.trace or any(
            not isinstance(value, str) or not value for value in self.trace
        ):
            raise ValueError("settled journal chain proof trace is invalid")
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "journal_entries", entries)


@dataclass(frozen=True, slots=True)
class Plan2SettledUnjournaledPlayProof:
    """Native proof for one PLAY completed after the last journal append.

    Maa may submit the next card while the previous retained row is already
    being reconstructed, then the UI/HUD reader can fail before that final
    card is appended.  The native ``userPlayLogList`` is the durable action
    ledger in that case.  This proof deliberately contains no screenshot or
    OCR observation: the completed journal prefix, exactly one additional
    native play-log row, card GUID/runtime progression, and a fresh settled
    bootstrap are the whole authority.
    """

    action: Plan2NativeAction
    journal_tail: Plan2CompletedCardReplay
    settled_current: AuditionLocalSaveStateEvidence
    fresh_root: Plan2NativeHorizonState
    trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.action.kind != "play":
            raise ValueError("unjournaled settled proof action must be PLAY")
        if not isinstance(self.journal_tail, Plan2CompletedCardReplay):
            raise TypeError("journal_tail must be a completed card replay")
        if not isinstance(
            self.settled_current, AuditionLocalSaveStateEvidence
        ):
            raise TypeError("settled_current must be typed evidence")
        if not isinstance(self.fresh_root, Plan2NativeHorizonState):
            raise TypeError("fresh_root must be a typed horizon")
        if not self.trace or any(
            not isinstance(value, str) or not value for value in self.trace
        ):
            raise ValueError("unjournaled settled proof trace is invalid")


def plan2_replay_journal_path(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
) -> Path:
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be typed")
    identity = "\n".join(
        (
            evidence.run_id,
            evidence.step_context_id,
            evidence.source_path,
            evidence.source_type,
            evidence.state.character_id,
            evidence.state.setting_id,
            str(evidence.state.step_type_value),
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Path(root) / f"{digest}.jsonl"


def plan2_pending_drink_receipt_path(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
) -> Path:
    journal = plan2_replay_journal_path(evidence, root=root)
    return journal.with_suffix(".pending-drink.json")


def plan2_pending_play_receipt_path(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
) -> Path:
    return plan2_replay_journal_path(evidence, root=root).with_suffix(
        ".pending-play.json"
    )


def save_plan2_pending_play_receipt(
    path: str | Path,
    receipt: Plan2PendingPlayReceipt,
) -> None:
    if not isinstance(receipt, Plan2PendingPlayReceipt):
        raise TypeError("receipt must be typed")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(receipt.to_dict(), stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)


def load_plan2_pending_play_receipt(
    path: str | Path,
) -> Plan2PendingPlayReceipt | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise Plan2ReplayJournalError("pending-play-row-shape-invalid")
        return Plan2PendingPlayReceipt.from_dict(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, Plan2ReplayJournalError):
            raise
        raise Plan2ReplayJournalError(
            "pending-play-row-invalid", f"{type(error).__name__}:{error}"
        ) from error


def clear_plan2_pending_play_receipt(
    path: str | Path,
    *,
    expected: Plan2PendingPlayReceipt | None = None,
) -> None:
    target = Path(path)
    if expected is not None:
        actual = load_plan2_pending_play_receipt(target)
        if actual is None:
            return
        if actual != expected:
            raise Plan2ReplayJournalError("pending-play-clear-owner-mismatch")
    target.unlink(missing_ok=True)


def pending_play_transitional_owner_matches(
    receipt: Plan2PendingPlayReceipt,
    current: AuditionLocalSaveStateEvidence,
) -> bool:
    """Return true only for another native snapshot of the same submitted PLAY."""

    if not isinstance(receipt, Plan2PendingPlayReceipt) or not isinstance(
        current, AuditionLocalSaveStateEvidence
    ):
        raise TypeError("pending PLAY owner match requires typed inputs")
    base = receipt.submitted_evidence
    identity_fields = (
        "run_id", "step_context_id", "step_context_digest",
        "session_transition_id", "source_path", "source_type",
    )
    if any(getattr(current, name) != getattr(base, name) for name in identity_fields):
        return False
    if not _pending_play_stage_compatible(base, current):
        return False
    runtime = current.state.root_runtime
    playing = current.state.playing_card
    if (
        current.state.is_native_actionable_settled
        or runtime is None
        or runtime.command_list_is_empty
        or runtime.is_exam_end_complete
        or playing is None
        or playing.guid != receipt.action.card_guid
    ):
        return False
    return True


def save_plan2_pending_drink_receipt(
    path: str | Path,
    receipt: Plan2PendingDrinkReceipt,
) -> None:
    """Atomically persist the exact pre-input evidence and submitted slot."""

    if not isinstance(receipt, Plan2PendingDrinkReceipt):
        raise TypeError("receipt must be typed")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(
        receipt.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(payload)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)


def load_plan2_pending_drink_receipt(
    path: str | Path,
) -> Plan2PendingDrinkReceipt | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise Plan2ReplayJournalError("pending-drink-row-shape-invalid")
        return Plan2PendingDrinkReceipt.from_dict(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, Plan2ReplayJournalError):
            raise
        raise Plan2ReplayJournalError(
            "pending-drink-row-invalid",
            f"{type(error).__name__}:{error}",
        ) from error


def clear_plan2_pending_drink_receipt(
    path: str | Path,
    *,
    expected: Plan2PendingDrinkReceipt | None = None,
) -> None:
    target = Path(path)
    if not target.exists():
        return
    if expected is not None and load_plan2_pending_drink_receipt(target) != expected:
        raise Plan2ReplayJournalError("pending-drink-clear-owner-mismatch")
    target.unlink()


def rollback_plan2_pending_drink_barrier_to_prior(
    path: str | Path,
    *,
    expected_current_sha256: str,
    expected_current_action_id: str,
    expected_prior_action_id: str,
) -> Plan2PendingDrinkReceipt:
    """Atomically discard one unconfirmed barrier while retaining its prior."""

    receipt = load_plan2_pending_drink_receipt(path)
    if receipt is None or receipt.prior is None:
        raise Plan2ReplayJournalError("pending-drink-rollback-prior-missing")
    if (
        receipt.persisted_before.source_sha256 != expected_current_sha256
        or receipt.action.action_id != expected_current_action_id
        or receipt.prior.action.action_id != expected_prior_action_id
    ):
        raise Plan2ReplayJournalError("pending-drink-rollback-authority-mismatch")
    prior = receipt.prior
    save_plan2_pending_drink_receipt(path, prior)
    return prior


def rollback_uncommitted_plan2_pending_drink_receipt(
    path: str | Path,
    current: AuditionLocalSaveStateEvidence,
    *,
    expected_before_sha256: str,
    expected_current_sha256: str,
    expected_action_id: str,
    expected_capture_sha256: Mapping[str, str],
) -> Path:
    """Archive one explicitly audited false-submission receipt.

    This is a one-shot artifact migration, never an automatic startup rule.
    Both LocalSave byte identities, the exact action, all three persisted Maa
    captures, and an unchanged settled gameplay boundary must match.  The
    receipt is atomically moved aside so restart performs zero duplicate input;
    the backup remains recoverable for audit.
    """

    if not isinstance(current, AuditionLocalSaveStateEvidence):
        raise TypeError("current must be typed evidence")
    target = Path(path)
    receipt = load_plan2_pending_drink_receipt(target)
    if (
        receipt is None
        or receipt.prior is not None
        or receipt.dispatch_witness is None
        or receipt.persisted_before.source_sha256 != expected_before_sha256
        or current.source_sha256 != expected_current_sha256
        or receipt.action.action_id != expected_action_id
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-uncommitted-rollback-authority-mismatch"
        )
    if set(expected_capture_sha256) != {
        "source_capture",
        "opened_capture",
        "post_use_capture",
    } or any(
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in value)
        for value in expected_capture_sha256.values()
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-uncommitted-rollback-capture-authority-invalid"
        )
    for name in (
        "source_capture",
        "opened_capture",
        "post_use_capture",
    ):
        raw = getattr(receipt.dispatch_witness, name)
        capture_path = Path(str(raw["png_path"]))
        try:
            actual_digest = hashlib.sha256(capture_path.read_bytes()).hexdigest()
        except OSError as error:
            raise Plan2ReplayJournalError(
                "pending-drink-uncommitted-rollback-capture-unavailable",
                f"{name}:{type(error).__name__}:{error}",
            ) from error
        if actual_digest.lower() != expected_capture_sha256[name].lower():
            raise Plan2ReplayJournalError(
                "pending-drink-uncommitted-rollback-capture-mismatch",
                name,
            )

    before = receipt.persisted_before
    left = before.state
    right = current.state
    left_runtime = left.root_runtime
    right_runtime = right.root_runtime

    def ordered_drinks(evidence: AuditionLocalSaveStateEvidence) -> tuple[str, ...]:
        runtime = evidence.state.root_runtime
        opaque = None if runtime is None else runtime.opaque_fields.to_value()
        rows = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
        if not isinstance(rows, list):
            raise Plan2ReplayJournalError(
                "pending-drink-uncommitted-rollback-boundary-mismatch"
            )
        result = tuple(
            row.get("_id") if isinstance(row, Mapping) else None for row in rows
        )
        if any(not isinstance(value, str) or not value for value in result):
            raise Plan2ReplayJournalError(
                "pending-drink-uncommitted-rollback-boundary-mismatch"
            )
        return result  # type: ignore[return-value]

    def stable_key(evidence: AuditionLocalSaveStateEvidence) -> tuple[object, ...]:
        state = evidence.state
        runtime = state.root_runtime
        return (
            evidence.run_id,
            evidence.step_context_id,
            evidence.source_path,
            evidence.source_type,
            state.character_id,
            state.setting_id,
            state.exam_type,
            state.step_type_value,
            state.phase,
            state.current_turn,
            state.limit_turn,
            state.remain_turn,
            state.extra_turn,
            state.score,
            state.stamina,
            state.max_stamina,
            state.block,
            state.vocal_bonus_permille,
            state.dance_bonus_permille,
            state.visual_bonus_permille,
            state.turn_parameter_types,
            state.turn_card_play_count,
            state.exam_card_play_count,
            state.is_turn_card_play_end,
            state.random_state,
            state.turn_use_support_ids,
            state.zones,
            state.playing_card,
            state.removed_cards,
            state.future_deck,
            state.past_deck,
            ordered_drinks(evidence),
            None if runtime is None else runtime.draw_card_guid_list,
            None if runtime is None else runtime.is_turn_card_grave,
            None if runtime is None else runtime.is_turn_card_lost,
            None if runtime is None else runtime.is_exam_end_complete,
        )

    before_drinks = ordered_drinks(before)
    if (
        left_runtime is None
        or right_runtime is None
        or not left.is_native_actionable_settled
        or not right.is_native_actionable_settled
        or not left_runtime.command_list_is_empty
        or not right_runtime.command_list_is_empty
        or stable_key(before) != stable_key(current)
        or not 0 <= receipt.action.slot_index < len(before_drinks)
        or before_drinks[receipt.action.slot_index] != receipt.action.drink_id
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-uncommitted-rollback-boundary-mismatch"
        )

    backup = target.with_suffix(
        target.suffix
        + ".uncommitted-"
        + expected_current_sha256[:12].lower()
        + ".backup"
    )
    if backup.exists():
        raise Plan2ReplayJournalError(
            "pending-drink-uncommitted-rollback-backup-exists"
        )
    try:
        target.replace(backup)
    except OSError as error:
        raise Plan2ReplayJournalError(
            "pending-drink-uncommitted-rollback-write-failed",
            f"{type(error).__name__}:{error}",
        ) from error
    return backup


def migrate_plan2_pending_drink_receipt_from_report(
    report_path: str | Path,
    current: AuditionLocalSaveStateEvidence,
    *,
    root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2PendingDrinkReceipt:
    """One-shot migration of an already-written submitted-action report.

    Production does not scan prior reports.  This helper accepts one explicit
    artifact only when its final typed action record says Maa submitted and
    then timed out waiting for settlement, and the supplied current retained
    queue exactly replays that receipt.
    """

    if not isinstance(current, AuditionLocalSaveStateEvidence):
        raise TypeError("current must be typed evidence")
    try:
        raw = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise Plan2ReplayJournalError(
            "pending-drink-migration-report-invalid",
            f"{type(error).__name__}:{error}",
        ) from error
    if not isinstance(raw, Mapping):
        raise Plan2ReplayJournalError("pending-drink-migration-report-invalid")
    progress = raw.get("progress")
    recent = progress.get("recent_step") if isinstance(progress, Mapping) else None
    outcome = recent.get("outcome") if isinstance(recent, Mapping) else None
    steps = outcome.get("steps") if isinstance(outcome, Mapping) else None
    if not isinstance(steps, list) or not steps or not isinstance(steps[-1], Mapping):
        raise Plan2ReplayJournalError("pending-drink-migration-record-missing")
    final = steps[-1]
    before_raw = final.get("evidence_before")
    action_raw = final.get("action")
    execution_raw = final.get("execution")
    if not all(
        isinstance(value, Mapping)
        for value in (before_raw, action_raw, execution_raw)
    ):
        raise Plan2ReplayJournalError("pending-drink-migration-record-invalid")
    assert isinstance(before_raw, Mapping)
    assert isinstance(action_raw, Mapping)
    assert isinstance(execution_raw, Mapping)
    expected_action_fields = {
        "kind",
        "slot_index",
        "instance_id",
        "drink_id",
        "selected_card_guid",
        "action_id",
    }
    detail = execution_raw.get("detail")
    if (
        set(action_raw) != expected_action_fields
        or action_raw.get("kind") != "drink"
        or action_raw.get("selected_card_guid") != ""
        or set(execution_raw) != {"accepted", "detail", "replan"}
        or execution_raw.get("accepted") is not False
        or execution_raw.get("replan") is not False
        or not isinstance(detail, str)
        or not detail.startswith("next-settled-evidence-failed:")
    ):
        raise Plan2ReplayJournalError("pending-drink-migration-record-invalid")
    action = Plan2NativeDrinkAction(
        _integer(action_raw.get("slot_index"), "action.slot_index"),
        _text(action_raw.get("instance_id"), "action.instance_id"),
        _text(action_raw.get("drink_id"), "action.drink_id"),
        "",
    )
    if action_raw.get("action_id") != action.action_id:
        raise Plan2ReplayJournalError("pending-drink-migration-action-mismatch")
    before = AuditionLocalSaveStateEvidence.from_dict(before_raw)
    result = recover_retained_plan2_drink_history(
        current,
        observation=Plan2CardHistoryObservation(
            turns_remaining=current.state.remain_turn,
            score=current.state.score,
            stamina=current.state.stamina,
            block=current.state.block,
            source=f"pending-drink-migration:{Path(report_path).name}",
        ),
        database=database,
        expected_action=action,
        persisted_before=before,
    )
    if not result.supported or result.replay is None:
        detail = ",".join(
            value.code + (":" + value.detail if value.detail else "")
            for value in result.issues
        )
        raise Plan2ReplayJournalError(
            "pending-drink-migration-replay-rejected",
            detail,
        )
    receipt = Plan2PendingDrinkReceipt(before, action)
    save_plan2_pending_drink_receipt(
        plan2_pending_drink_receipt_path(current, root=root),
        receipt,
    )
    return receipt


def migrate_plan2_pending_drink_barrier_from_report(
    report_path: str | Path,
    current: AuditionLocalSaveStateEvidence,
    *,
    expected_prior_before_sha256: str,
    expected_current_sha256: str,
    expected_prior_action_id: str,
    expected_action_id: str,
    source_capture: Mapping[str, object],
    opened_capture: Mapping[str, object],
    post_use_capture: Mapping[str, object],
    root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2PendingDrinkReceipt:
    """Reject legacy two-click reports as drink-submission authority.

    A slot click followed by the detail-page Use click may only open the
    game's ineffective-effect confirmation.  Historical screenshots and a
    timeout therefore cannot prove that Confirm was clicked.  Production
    receipts are now written only by the live dispatcher after Maa proves
    that no confirmation exists or submits its exact Confirm node.
    """

    raise Plan2ReplayJournalError(
        "pending-drink-barrier-report-confirmation-unproven"
    )

    if not isinstance(current, AuditionLocalSaveStateEvidence):
        raise TypeError("current must be typed evidence")
    for name, value in (
        ("expected_prior_before_sha256", expected_prior_before_sha256),
        ("expected_current_sha256", expected_current_sha256),
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"{name} must be a lowercase SHA-256")
    for name, value in (
        ("expected_prior_action_id", expected_prior_action_id),
        ("expected_action_id", expected_action_id),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be non-empty text")
    if current.source_sha256 != expected_current_sha256:
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-current-sha-mismatch"
        )

    try:
        raw = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-report-invalid",
            f"{type(error).__name__}:{error}",
        ) from error
    progress = raw.get("progress") if isinstance(raw, Mapping) else None
    recent = progress.get("recent_step") if isinstance(progress, Mapping) else None
    outcome = recent.get("outcome") if isinstance(recent, Mapping) else None
    steps = outcome.get("steps") if isinstance(outcome, Mapping) else None
    if (
        not isinstance(steps, list)
        or len(steps) < 2
        or not isinstance(steps[-2], Mapping)
        or not isinstance(steps[-1], Mapping)
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-records-missing"
        )
    prior_row = steps[-2]
    current_row = steps[-1]

    def action_from(row: Mapping[str, object]) -> Plan2NativeDrinkAction:
        action_raw = row.get("action")
        if not isinstance(action_raw, Mapping) or set(action_raw) != {
            "kind",
            "slot_index",
            "instance_id",
            "drink_id",
            "selected_card_guid",
            "action_id",
        }:
            raise Plan2ReplayJournalError(
                "pending-drink-barrier-action-invalid"
            )
        action = Plan2NativeDrinkAction(
            _integer(action_raw.get("slot_index"), "action.slot_index"),
            _text(action_raw.get("instance_id"), "action.instance_id"),
            _text(action_raw.get("drink_id"), "action.drink_id"),
            "",
        )
        if (
            action_raw.get("kind") != "drink"
            or action_raw.get("selected_card_guid") != ""
            or action_raw.get("action_id") != action.action_id
        ):
            raise Plan2ReplayJournalError(
                "pending-drink-barrier-action-invalid"
            )
        return action

    prior_action = action_from(prior_row)
    action = action_from(current_row)
    if (
        prior_action.action_id != expected_prior_action_id
        or action.action_id != expected_action_id
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-action-mismatch"
        )

    prior_before_raw = prior_row.get("evidence_before")
    prior_after_raw = prior_row.get("actual_next_evidence")
    prior_logical_raw = prior_row.get("actual_next")
    current_before_raw = current_row.get("evidence_before")
    prior_execution = prior_row.get("execution")
    current_execution = current_row.get("execution")
    if not all(
        isinstance(value, Mapping)
        for value in (
            prior_before_raw,
            prior_after_raw,
            prior_logical_raw,
            current_before_raw,
            prior_execution,
            current_execution,
        )
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-record-invalid"
        )
    assert isinstance(prior_before_raw, Mapping)
    assert isinstance(prior_after_raw, Mapping)
    assert isinstance(prior_logical_raw, Mapping)
    assert isinstance(current_before_raw, Mapping)
    assert isinstance(prior_execution, Mapping)
    assert isinstance(current_execution, Mapping)
    prior_before = AuditionLocalSaveStateEvidence.from_dict(prior_before_raw)
    prior_after = AuditionLocalSaveStateEvidence.from_dict(prior_after_raw)
    current_before = AuditionLocalSaveStateEvidence.from_dict(current_before_raw)
    current_detail = current_execution.get("detail")
    if (
        prior_before.source_sha256 != expected_prior_before_sha256
        or prior_after != current
        or current_before != current
        or set(prior_execution) != {"accepted", "detail", "replan"}
        or prior_execution.get("accepted") is not True
        or prior_execution.get("replan") is not False
        or set(current_execution) != {"accepted", "detail", "replan"}
        or current_execution.get("accepted") is not False
        or current_execution.get("replan") is not False
        or not isinstance(current_detail, str)
        or not current_detail.startswith("next-settled-evidence-failed:")
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-record-mismatch"
        )

    scalar_raw = prior_logical_raw.get("scalar")
    if not isinstance(scalar_raw, Mapping):
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-logical-after-invalid"
        )
    try:
        observation = Plan2CardHistoryObservation(
            turns_remaining=_integer(
                prior_logical_raw.get("remaining_turns"),
                "actual_next.remaining_turns",
            ),
            score=_integer(scalar_raw.get("score"), "actual_next.scalar.score"),
            stamina=_integer(
                scalar_raw.get("stamina"),
                "actual_next.scalar.stamina",
            ),
            block=_integer(scalar_raw.get("block"), "actual_next.scalar.block"),
            review=_integer(
                scalar_raw.get("review"),
                "actual_next.scalar.review",
            ),
            aggressive=_integer(
                scalar_raw.get("card_play_aggressive"),
                "actual_next.scalar.card_play_aggressive",
            ),
            plays_remaining=_integer(
                prior_logical_raw.get("plays_remaining"),
                "actual_next.plays_remaining",
            ),
            source=f"pending-drink-barrier-migration:{Path(report_path).name}",
            authority=Plan2CardHistoryObservationAuthority.LOGICAL_REPLAY,
        )
    except (TypeError, ValueError) as error:
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-logical-after-invalid",
            f"{type(error).__name__}:{error}",
        ) from error
    recovered = recover_retained_plan2_drink_history(
        current,
        observation=observation,
        database=database,
        expected_action=prior_action,
        persisted_before=prior_before,
    )
    if not recovered.supported or recovered.replay is None:
        detail = ",".join(
            value.code + (":" + value.detail if value.detail else "")
            for value in recovered.issues
        )
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-prior-replay-rejected",
            detail,
        )

    pending_path = plan2_pending_drink_receipt_path(current, root=root)
    existing = load_plan2_pending_drink_receipt(pending_path)
    if (
        existing is None
        or existing.persisted_before != current
        or existing.action != action
        or existing.prior is not None
        or existing.dispatch_witness is not None
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-legacy-receipt-mismatch"
        )
    try:
        witness = Plan2PendingDrinkDispatchWitness(
            source_capture,
            opened_capture,
            post_use_capture,
        )
    except (TypeError, ValueError) as error:
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-capture-witness-invalid",
            f"{type(error).__name__}:{error}",
        ) from error
    for capture in (
        witness.source_capture,
        witness.opened_capture,
        witness.post_use_capture,
    ):
        if not Path(str(capture["png_path"])).is_file():
            raise Plan2ReplayJournalError(
                "pending-drink-barrier-capture-missing",
                str(capture["png_path"]),
            )
    controller = raw.get("controller") if isinstance(raw, Mapping) else None
    if (
        not isinstance(controller, Mapping)
        or controller.get("target_hwnd") != witness.source_capture["hwnd"]
        or controller.get("target_pid") != witness.source_capture["pid"]
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-barrier-controller-identity-mismatch"
        )

    migrated = Plan2PendingDrinkReceipt(
        current,
        action,
        prior=Plan2PendingDrinkReceipt(prior_before, prior_action),
        dispatch_witness=witness,
    )
    # Prove now—not on the next live startup—that the logical survivor can be
    # bound to the exact submitted instance retained by this receipt.
    bind_plan2_pending_drink_action_identity(
        recovered.replay.logical_after,
        migrated,
    )
    save_plan2_pending_drink_receipt(pending_path, migrated)
    return migrated


def bind_plan2_pending_drink_action_identity(
    state: Plan2NativeHorizonState,
    receipt: Plan2PendingDrinkReceipt,
) -> Plan2NativeHorizonState:
    """Bind a compacted restart runtime to its submitted action identity.

    LocalSave stores only ordered drink IDs.  Rebootstrapping the post-remove
    inventory therefore renumbers the survivors from zero, while the durable
    action receipt retains the planner's stable session instance (for example
    original slot ``2`` now visible at UI slot ``0``).  Rebind exactly that
    selected survivor only when the v2 predecessor+witness receipt, complete
    raw inventory order, and both ``localsave-drink`` identities agree.
    """

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be a typed horizon")
    if not isinstance(receipt, Plan2PendingDrinkReceipt):
        raise TypeError("receipt must be typed")
    if receipt.prior is None or receipt.dispatch_witness is None:
        raise Plan2ReplayJournalError(
            "pending-drink-identity-rebind-authority-missing"
        )
    action = receipt.action
    runtime = receipt.persisted_before.state.root_runtime
    opaque = None if runtime is None else runtime.opaque_fields.to_value()
    rows = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
    if not isinstance(rows, list):
        raise Plan2ReplayJournalError(
            "pending-drink-identity-rebind-inventory-invalid"
        )
    raw_ids = tuple(
        row.get("_id") if isinstance(row, Mapping) else None for row in rows
    )
    logical_ids = tuple(
        value.drink_id for value in state.drink_runtime.inventory
    )
    if (
        any(not isinstance(value, str) or not value for value in raw_ids)
        or raw_ids != logical_ids
        or not 0 <= action.slot_index < len(logical_ids)
        or logical_ids[action.slot_index] != action.drink_id
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-identity-rebind-inventory-mismatch"
        )
    selected = state.drink_runtime.inventory[action.slot_index]
    if selected.instance_id == action.instance_id:
        return state

    def parse(instance_id: str) -> tuple[int, str] | None:
        parts = instance_id.split(":", 2)
        if (
            len(parts) != 3
            or parts[0] != "localsave-drink"
            or not parts[1].isdigit()
            or not parts[2]
        ):
            return None
        return int(parts[1]), parts[2]

    current_identity = parse(selected.instance_id)
    receipt_identity = parse(action.instance_id)
    if (
        current_identity is None
        or receipt_identity is None
        or current_identity[1] != action.drink_id
        or receipt_identity[1] != action.drink_id
        or any(
            value.instance_id == action.instance_id
            for index, value in enumerate(state.drink_runtime.inventory)
            if index != action.slot_index
        )
    ):
        raise Plan2ReplayJournalError(
            "pending-drink-identity-rebind-instance-mismatch"
        )
    inventory = list(state.drink_runtime.inventory)
    inventory[action.slot_index] = replace(
        selected,
        instance_id=action.instance_id,
    )
    return replace(
        state,
        drink_runtime=replace(
            state.drink_runtime,
            inventory=tuple(inventory),
        ),
    )


def observation_from_horizon(
    state: Plan2NativeHorizonState,
    *,
    source: str = "maa-replay-journal",
) -> Plan2CardHistoryObservation:
    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("state must be a typed horizon")
    return Plan2CardHistoryObservation(
        turns_remaining=state.remaining_turns,
        score=state.scalar.score,
        stamina=state.scalar.stamina,
        block=state.scalar.block,
        review=state.scalar.review,
        aggressive=state.scalar.card_play_aggressive,
        plays_remaining=state.plays_remaining,
        source=source,
        authority=Plan2CardHistoryObservationAuthority.LOGICAL_REPLAY,
    )


def append_plan2_replay_journal(
    path: str | Path,
    entry: Plan2ReplayJournalEntry,
) -> None:
    if not isinstance(entry, Plan2ReplayJournalEntry):
        raise TypeError("entry must be typed")
    target = Path(path)
    existing = load_plan2_replay_journal(target)
    if existing and existing[-1].transition_digest == entry.transition_digest:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(
            json.dumps(
                entry.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_plan2_replay_journal(
    path: str | Path,
) -> tuple[Plan2ReplayJournalEntry, ...]:
    target = Path(path)
    if not target.exists():
        return ()
    entries: list[Plan2ReplayJournalEntry] = []
    for line_number, line in enumerate(
        target.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            if not isinstance(raw, Mapping):
                raise Plan2ReplayJournalError("journal-row-shape-invalid")
            entries.append(Plan2ReplayJournalEntry.from_dict(raw))
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise Plan2ReplayJournalError(
                "journal-row-invalid",
                f"line={line_number}:{type(error).__name__}:{error}",
            ) from error
    return tuple(entries)


def clear_plan2_replay_journal(path: str | Path) -> None:
    target = Path(path)
    if target.exists():
        target.unlink()


class Plan2ReplayJournalSink:
    """Persist replay-proven retained transitions emitted by the live loop."""

    def __init__(
        self,
        evidence: AuditionLocalSaveStateEvidence,
        *,
        root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
    ) -> None:
        self.root = Path(root)
        self.path = plan2_replay_journal_path(evidence, root=root)
        self.pending_drink_path = plan2_pending_drink_receipt_path(
            evidence,
            root=root,
        )
        self.pending_play_path = plan2_pending_play_receipt_path(
            evidence,
            root=root,
        )

    def append_pending_play_receipt(
        self,
        before: AuditionLocalSaveStateEvidence,
        action: Plan2NativeAction,
        submitted: AuditionLocalSaveStateEvidence,
        dispatch: Mapping[str, object],
        hand_slot: int,
        logical_before: Plan2NativeHorizonState | None = None,
        prior_replay: Plan2CompletedLogicalReplay | None = None,
    ) -> Plan2PendingPlayReceipt:
        prove_plan2_submitted_play_acceptance(
            before,
            action,
            submitted,
            logical_before=logical_before,
            prior_replay=prior_replay,
        )
        witness = _pending_play_dispatch_identity(
            action,
            hand_slot,
            dispatch,
        )
        authority = PENDING_PLAY_BEFORE_NATIVE_SETTLED
        prior_identity = None
        if logical_before is not None:
            if prior_replay is None:
                raise Plan2ReplayJournalError(
                    "pending-play-logical-prior-missing"
                )
            entries = load_plan2_replay_journal(self.path)
            if not entries:
                raise Plan2ReplayJournalError(
                    "pending-play-logical-journal-tail-missing"
                )
            tail = entries[-1]
            if isinstance(prior_replay, Plan2CompletedCardReplay):
                valid_prior = bool(
                    tail.persisted_before == prior_replay.persisted_before
                    and tail.persisted_transition
                    == prior_replay.persisted_transition
                    and tail.action == prior_replay.action
                )
            else:
                pending_drink = load_plan2_pending_drink_receipt(
                    self.pending_drink_path
                )
                valid_prior = bool(
                    pending_drink is not None
                    and pending_drink.action == prior_replay.action
                    and pending_drink.persisted_before
                    == prior_replay.persisted_before
                    and tail.persisted_transition
                    == prior_replay.persisted_before
                )
            if not (
                valid_prior
                and prior_replay.persisted_transition == before
                and prior_replay.logical_after == logical_before
            ):
                raise Plan2ReplayJournalError(
                    "pending-play-logical-journal-tail-mismatch"
                )
            authority = PENDING_PLAY_BEFORE_LOGICAL_CONTINUATION
            prior_identity = _pending_play_prior_replay_identity(prior_replay)
        receipt = Plan2PendingPlayReceipt(
            authority,
            before,
            prior_identity,
            action,
            submitted,
            witness,
        )
        existing = load_plan2_pending_play_receipt(self.pending_play_path)
        if existing is not None:
            if existing == receipt:
                return existing
            raise Plan2ReplayJournalError("pending-play-receipt-overlap")
        save_plan2_pending_play_receipt(self.pending_play_path, receipt)
        return receipt

    def pending_play_context(
        self,
        receipt: Plan2PendingPlayReceipt,
    ) -> tuple[Plan2NativeHorizonState | None, Plan2CompletedLogicalReplay | None]:
        if receipt.before_authority == PENDING_PLAY_BEFORE_NATIVE_SETTLED:
            return None, None
        entries = load_plan2_replay_journal(self.path)
        if not entries:
            raise Plan2ReplayJournalError(
                "pending-play-logical-journal-tail-missing"
            )
        tail = entries[-1]
        prior = receipt.prior_replay_identity
        assert prior is not None
        prior_kind = prior.get("kind")
        if prior_kind == "card":
            if (
                tail.persisted_before.digest()
                != prior.get("persisted_before_digest")
                or tail.persisted_transition.digest()
                != prior.get("persisted_transition_digest")
                or tail.action.action_id != prior.get("action_id")
                or tail.persisted_transition != receipt.persisted_before
            ):
                raise Plan2ReplayJournalError(
                    "pending-play-logical-journal-tail-mismatch"
                )
            replay = recover_plan2_replay_journal(
                receipt.persisted_before,
                root=self.root,
            )
        elif prior_kind == "drink":
            pending_drink = load_plan2_pending_drink_receipt(
                self.pending_drink_path
            )
            if (
                pending_drink is None
                or pending_drink.persisted_before.digest()
                != prior.get("persisted_before_digest")
                or pending_drink.action.action_id != prior.get("action_id")
                or tail.persisted_transition != pending_drink.persisted_before
                or receipt.persisted_before.digest()
                != prior.get("persisted_transition_digest")
            ):
                raise Plan2ReplayJournalError(
                    "pending-play-logical-journal-tail-mismatch"
                )
            predecessor = recover_plan2_replay_journal(
                pending_drink.persisted_before,
                root=self.root,
            )
            if predecessor is None:
                raise Plan2ReplayJournalError(
                    "pending-play-logical-prior-replay-unavailable"
                )
            observation = observation_from_horizon(
                predecessor.logical_after,
                source="pending-play-prior-drink",
            )
            recovered = recover_retained_plan2_drink_history(
                receipt.persisted_before,
                observation=observation,
                expected_action=pending_drink.action,
                persisted_before=pending_drink.persisted_before,
                before_horizon=predecessor.logical_after,
            )
            replay = recovered.replay if recovered.supported else None
        else:
            raise Plan2ReplayJournalError(
                "pending-play-logical-journal-tail-mismatch"
            )
        if not isinstance(
            replay,
            (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay),
        ):
            raise Plan2ReplayJournalError(
                "pending-play-logical-prior-replay-unavailable"
            )
        if _pending_play_prior_replay_identity(replay) != dict(prior):
            raise Plan2ReplayJournalError(
                "pending-play-logical-journal-tail-mismatch"
            )
        return replay.logical_after, replay

    def _matching_pending_play(
        self,
        before: AuditionLocalSaveStateEvidence,
        action: Plan2NativeAction,
        submitted: AuditionLocalSaveStateEvidence,
    ) -> Plan2PendingPlayReceipt | None:
        pending = load_plan2_pending_play_receipt(self.pending_play_path)
        if pending is None:
            return None
        if (
            pending.persisted_before != before
            or pending.action != action
            or pending.submitted_evidence != submitted
        ):
            raise Plan2ReplayJournalError("pending-play-completion-owner-mismatch")
        return pending

    def complete_pending_play(
        self,
        receipt: Plan2PendingPlayReceipt,
        observation: Plan2CardHistoryObservation,
    ) -> None:
        if not isinstance(receipt, Plan2PendingPlayReceipt):
            raise TypeError("pending PLAY receipt must be typed")
        if not isinstance(observation, Plan2CardHistoryObservation):
            raise TypeError("pending PLAY observation must be typed")
        current = load_plan2_pending_play_receipt(self.pending_play_path)
        if current != receipt:
            raise Plan2ReplayJournalError("pending-play-completion-owner-mismatch")
        existing = load_plan2_replay_journal(self.path)
        if existing:
            tail = existing[-1]
            if (
                tail.persisted_before == receipt.persisted_before
                and tail.persisted_transition == receipt.submitted_evidence
                and tail.action == receipt.action
            ):
                clear_plan2_pending_play_receipt(
                    self.pending_play_path,
                    expected=receipt,
                )
                return
            if tail.persisted_before == receipt.persisted_before:
                raise Plan2ReplayJournalError(
                    "pending-play-journal-owner-overlap"
                )
        append_plan2_replay_journal(
            self.path,
            Plan2ReplayJournalEntry(
                receipt.persisted_before,
                receipt.submitted_evidence,
                receipt.action,
                observation,
            ),
        )
        written = load_plan2_replay_journal(self.path)
        if not written or not (
            written[-1].persisted_before == receipt.persisted_before
            and written[-1].persisted_transition == receipt.submitted_evidence
            and written[-1].action == receipt.action
        ):
            raise Plan2ReplayJournalError("pending-play-journal-durable-append-failed")
        clear_plan2_pending_play_receipt(
            self.pending_play_path,
            expected=receipt,
        )

    def complete_pending_play_chain(
        self,
        receipt: Plan2PendingPlayReceipt,
        observation: Plan2CardHistoryObservation,
        settled: AuditionLocalSaveStateEvidence,
    ) -> None:
        """Idempotently retire a completed DRINK -> PLAY nested owner chain."""

        if not isinstance(settled, AuditionLocalSaveStateEvidence):
            raise TypeError("pending PLAY settled evidence must be typed")
        if receipt.state_after is not None:
            self.cleanup_pending_play_tombstone(receipt, settled)
            return
        prior = receipt.prior_replay_identity
        is_drink_chain = bool(
            isinstance(prior, Mapping) and prior.get("kind") == "drink"
        )
        pending_play = load_plan2_pending_play_receipt(self.pending_play_path)
        if pending_play != receipt:
            raise Plan2ReplayJournalError("pending-play-completion-owner-mismatch")
        entries = load_plan2_replay_journal(self.path)
        play_tail_matches = bool(
            entries
            and entries[-1].persisted_before == receipt.persisted_before
            and entries[-1].persisted_transition == receipt.submitted_evidence
            and entries[-1].action == receipt.action
        )
        if not is_drink_chain:
            self.complete_pending_play(receipt, observation)
            return
        assert prior is not None
        pending_drink = load_plan2_pending_drink_receipt(self.pending_drink_path)
        if pending_drink is None:
            if not entries:
                clear_plan2_pending_play_receipt(
                    self.pending_play_path,
                    expected=receipt,
                )
                return
            raise Plan2ReplayJournalError(
                "pending-play-prior-drink-owner-missing"
            )
        if (
            pending_drink.action.action_id != prior.get("action_id")
            or pending_drink.persisted_before.digest()
            != prior.get("persisted_before_digest")
            or receipt.persisted_before.digest()
            != prior.get("persisted_transition_digest")
        ):
            raise Plan2ReplayJournalError(
                "pending-play-prior-drink-owner-mismatch"
            )
        if not play_tail_matches:
            if entries and entries[-1].persisted_before == receipt.persisted_before:
                raise Plan2ReplayJournalError(
                    "pending-play-journal-owner-overlap"
                )
            append_plan2_replay_journal(
                self.path,
                Plan2ReplayJournalEntry(
                    receipt.persisted_before,
                    receipt.submitted_evidence,
                    receipt.action,
                    observation,
                ),
            )
            entries = load_plan2_replay_journal(self.path)
            play_tail_matches = bool(
                entries
                and entries[-1].persisted_before == receipt.persisted_before
                and entries[-1].persisted_transition
                == receipt.submitted_evidence
                and entries[-1].action == receipt.action
            )
            if not play_tail_matches:
                raise Plan2ReplayJournalError(
                    "pending-play-journal-durable-append-failed"
                )
        if entries:
            reconcile_settled_plan2_replay_journal(
                settled,
                root=self.root,
            )
            marker = replace(receipt, state_after=settled)
            save_plan2_pending_play_receipt(self.pending_play_path, marker)
            if load_plan2_pending_play_receipt(self.pending_play_path) != marker:
                raise Plan2ReplayJournalError(
                    "pending-play-cleanup-marker-durable-write-failed"
                )
            # Clear the journal before its drink bridge.  A crash after this
            # point leaves only a matching drink receipt, which the same
            # native-settled completion call can safely retire on restart.
            clear_plan2_replay_journal(self.path)
        else:
            raise Plan2ReplayJournalError(
                "pending-play-journal-owner-missing"
            )
        self.cleanup_pending_play_tombstone(marker, settled)

    def cleanup_pending_play_tombstone(
        self,
        receipt: Plan2PendingPlayReceipt,
        current: AuditionLocalSaveStateEvidence,
    ) -> None:
        """Resume artifact cleanup from the final durable settled marker."""

        if receipt.state_after is None or current != receipt.state_after:
            raise Plan2ReplayJournalError(
                "pending-play-cleanup-marker-current-mismatch"
            )
        persisted = load_plan2_pending_play_receipt(self.pending_play_path)
        if persisted != receipt:
            raise Plan2ReplayJournalError(
                "pending-play-completion-owner-mismatch"
            )
        # Marker creation happens only after full journal reconciliation.  If
        # a crash left the proven journal behind, its owner is this exact PLAY.
        entries = load_plan2_replay_journal(self.path)
        if entries:
            tail = entries[-1]
            if not (
                tail.persisted_before == receipt.persisted_before
                and tail.persisted_transition == receipt.submitted_evidence
                and tail.action == receipt.action
            ):
                raise Plan2ReplayJournalError(
                    "pending-play-journal-owner-overlap"
                )
            clear_plan2_replay_journal(self.path)
        prior = receipt.prior_replay_identity
        if isinstance(prior, Mapping) and prior.get("kind") == "drink":
            pending_drink = load_plan2_pending_drink_receipt(
                self.pending_drink_path
            )
            if pending_drink is not None:
                if (
                    pending_drink.action.action_id != prior.get("action_id")
                    or pending_drink.persisted_before.digest()
                    != prior.get("persisted_before_digest")
                ):
                    raise Plan2ReplayJournalError(
                        "pending-play-prior-drink-owner-mismatch"
                    )
                clear_plan2_pending_drink_receipt(
                    self.pending_drink_path,
                    expected=pending_drink,
                )
        clear_plan2_pending_play_receipt(
            self.pending_play_path,
            expected=receipt,
        )

    def append_pending_drink_receipt(
        self,
        before: AuditionLocalSaveStateEvidence,
        action: Plan2NativeDrinkAction,
        dispatch: Mapping[str, object],
        prior_replay: Plan2CompletedDrinkReplay | None,
    ) -> None:
        """Persist only after Maa returned an exact submitted receipt."""

        witness = Plan2PendingDrinkDispatchWitness.from_dispatch(
            action,
            dispatch,
        )
        prior = None
        existing = load_plan2_pending_drink_receipt(self.pending_drink_path)
        if prior_replay is not None:
            if (
                existing is None
                or existing.action != prior_replay.action
                or existing.persisted_before != prior_replay.persisted_before
                or prior_replay.persisted_transition != before
            ):
                raise Plan2ReplayJournalError(
                    "pending-drink-prior-receipt-mismatch"
                )
            # Only the immediately preceding retained drink is required to
            # rebuild the one-action PLAY barrier.  Drop an older predecessor
            # rather than allowing an unbounded speculative chain.
            prior = replace(existing, prior=None)
        save_plan2_pending_drink_receipt(
            self.pending_drink_path,
            Plan2PendingDrinkReceipt(
                before,
                action,
                prior=prior,
                dispatch_witness=witness,
            ),
        )

    def clear_pending_drink_receipt(
        self,
        settled: AuditionLocalSaveStateEvidence,
    ) -> None:
        """Retire the receipt only at a durable action boundary.

        A replay-proven PLAY may leave ``ExamSaveData`` on a retained command
        queue.  That transitional save is valid journal input, but the pending
        drink receipt is still required to rebuild the same logical chain after
        restart.  Callers needing an explicit administrative deletion can use
        :func:`clear_plan2_pending_drink_receipt` directly.
        """

        if not isinstance(settled, AuditionLocalSaveStateEvidence):
            raise TypeError("settled must be typed evidence")
        runtime = settled.state.root_runtime
        terminal = bool(runtime is not None and runtime.is_exam_end_complete)
        if not settled.state.is_native_actionable_settled and not terminal:
            return
        clear_plan2_pending_drink_receipt(self.pending_drink_path)

    def __call__(self, record: object) -> None:
        next_evidence = getattr(record, "actual_next_evidence", None)
        logical_after = getattr(record, "actual_next", None)
        before = getattr(record, "evidence_before", None)
        action = getattr(record, "action", None)
        if next_evidence is None:
            return
        if (
            next_evidence.state.is_native_actionable_settled
            or (
                next_evidence.state.root_runtime is not None
                and next_evidence.state.root_runtime.is_exam_end_complete
            )
        ):
            clear_plan2_replay_journal(self.path)
            return
        if not all(
            (
                isinstance(before, AuditionLocalSaveStateEvidence),
                isinstance(next_evidence, AuditionLocalSaveStateEvidence),
                isinstance(logical_after, Plan2NativeHorizonState),
                isinstance(action, Plan2NativeAction),
                action.kind == "play" if isinstance(action, Plan2NativeAction) else False,
            )
        ):
            return
        observation = observation_from_horizon(logical_after)
        pending = self._matching_pending_play(before, action, next_evidence)
        append_plan2_replay_journal(
            self.path,
            Plan2ReplayJournalEntry(
                before,
                next_evidence,
                action,
                observation,
            ),
        )
        if pending is not None:
            self.complete_pending_play(pending, observation)

    def append_retained_transition(
        self,
        record: object,
        next_evidence: AuditionLocalSaveStateEvidence,
        *,
        observation: Plan2CardHistoryObservation | None = None,
    ) -> None:
        """Persist a submitted PLAY whose waiter stopped on a command queue.

        The unattended loop records this case as action-rejected because no
        settled save arrived.  The outer adapter subsequently proves that the
        selected GUID is durably present in ``playingCard``.  Persist the
        already-computed native horizon here so the next process can rebuild
        the chain without another HUD/OCR inference.
        """

        before = getattr(record, "evidence_before", None)
        action = getattr(record, "action", None)
        if not isinstance(next_evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("next_evidence must be typed evidence")
        if not isinstance(before, AuditionLocalSaveStateEvidence):
            raise Plan2ReplayJournalError("journal-retained-before-missing")
        if not isinstance(action, Plan2NativeAction) or action.kind != "play":
            raise Plan2ReplayJournalError("journal-retained-action-invalid")
        if observation is None:
            # A retained playingCard + native command queue already proves the
            # submitted input. Persist the loop's native horizon rather than
            # taking a post-submit HUD screenshot: animation can cover the HUD
            # and must never leave a half journal after a successful click.
            logical_after = getattr(record, "actual_next", None)
            if not isinstance(logical_after, Plan2NativeHorizonState):
                logical_after = getattr(record, "predicted_after", None)
            if not isinstance(logical_after, Plan2NativeHorizonState):
                raise Plan2ReplayJournalError(
                    "journal-retained-native-horizon-missing"
                )
            observation = observation_from_horizon(
                logical_after,
                source="native-retained-horizon",
            )
        elif not isinstance(observation, Plan2CardHistoryObservation):
            raise TypeError("observation must be typed")
        append_plan2_replay_journal(
            self.path,
            Plan2ReplayJournalEntry(
                before,
                next_evidence,
                action,
                observation,
            ),
        )

    def append_accepted_pending_play(
        self,
        before: AuditionLocalSaveStateEvidence,
        action: Plan2NativeAction,
        retained: AuditionLocalSaveStateEvidence,
        replay: Plan2CompletedCardReplay,
    ) -> None:
        """Durably own one exact retained PLAY before the live waiter returns."""

        if replay.persisted_before != before or replay.persisted_transition != retained:
            raise Plan2ReplayJournalError("journal-pending-play-replay-mismatch")
        if replay.action != action or action.kind != "play":
            raise Plan2ReplayJournalError("journal-pending-play-action-mismatch")
        pending = self._matching_pending_play(before, action, retained)
        observation = observation_from_horizon(
            replay.logical_after,
            source="native-accepted-pending-settlement",
        )
        if pending is not None:
            self.complete_pending_play(pending, observation)
            return
        existing = load_plan2_replay_journal(self.path) if self.path.exists() else ()
        if existing:
            tail = existing[-1]
            if (
                tail.persisted_before == before
                and tail.persisted_transition == retained
                and tail.action == action
            ):
                return
        append_plan2_replay_journal(
            self.path,
            Plan2ReplayJournalEntry(
                before,
                retained,
                action,
                observation,
            ),
        )


def _journal_replay_root(
    decision: object,
) -> Plan2NativeHorizonState | None:
    """Return the exact predictive root used to plan a journalled action.

    LocalSave bootstrap deliberately contains no hypothetical generated-card
    identities.  The ExamSave orchestrator attaches deterministic, search-only
    allocator inputs before invoking expectimax, and ``search.root`` retains
    that authority.  Restart replay must use the same root: replaying a card
    with an exact CardCreateId effect from the bare bootstrap otherwise fails
    at the generated-card boundary even though the submitted GUID and retained
    command queue are already proven.

    Keep this boundary fail-closed.  A planner result may contribute only the
    generated allocator inputs; every other horizon field must still equal the
    LocalSave-bound bootstrap.  This does not accept or manufacture a game
    GUID—the deterministic symbolic identity remains confined to prediction
    until a later LocalSave publishes the native generated-card GUID.
    """

    logical_root = getattr(decision, "logical_root", None)
    bootstrap = getattr(decision, "bootstrap", None)
    root = (
        logical_root
        if isinstance(logical_root, Plan2NativeHorizonState)
        else getattr(bootstrap, "state", None)
    )
    if not isinstance(root, Plan2NativeHorizonState):
        return None
    search = getattr(decision, "search", None)
    predictive_root = getattr(search, "root", None)
    if not isinstance(predictive_root, Plan2NativeHorizonState):
        return root
    if replace(
        predictive_root,
        generated_inputs=root.generated_inputs,
    ) != root:
        raise Plan2ReplayJournalError("journal-predictive-root-mismatch")
    return predictive_root


def _legacy_committed_extra_turn_observation(
    entry: Plan2ReplayJournalEntry,
) -> Plan2CardHistoryObservation:
    """Correct one precisely identified pre-v2 HUD turn observation.

    Before ``PLAN2_HUD_TURNS_RAW_REMAIN_V2``, the HUD reader added both the
    already-materialized LocalSave ``extraTurn`` and any still-pending native
    ExtraTurn command to the large rendered remaining-turn counter.  The
    former is already included in both the rendered counter and ``remainTurn``.
    Existing journals are immutable action receipts, so recover the exact
    legacy arithmetic in memory instead of weakening card-history turn
    equality or re-reading/re-sending the action.
    """

    observation = entry.observation
    marker = f":{PLAN2_HUD_TURNS_RAW_REMAIN_V2}:"
    if (
        marker in observation.source
        or not observation.source.startswith("maa-retained-hud:")
    ):
        return observation
    before = entry.persisted_before.state
    transition = entry.persisted_transition.state
    committed_extra_turns = transition.extra_turn
    if (
        committed_extra_turns <= 0
        or before.extra_turn != committed_extra_turns
        or before.remain_turn != transition.remain_turn
    ):
        return observation
    runtime = transition.root_runtime
    command_rows = [] if runtime is None else runtime.command_list.to_value()
    if not isinstance(command_rows, list):
        return observation
    pending_extra_turns = sum(
        1
        for row in command_rows
        if isinstance(row, Mapping)
        and isinstance(row.get("_playEffect"), Mapping)
        and row["_playEffect"].get("_id") == "e_effect-exam_extra_turn"
    )
    legacy_total = (
        transition.remain_turn
        + committed_extra_turns
        + pending_extra_turns
    )
    if observation.turns_remaining != legacy_total:
        return observation
    return replace(
        observation,
        turns_remaining=transition.remain_turn + pending_extra_turns,
        source=(
            f"{observation.source}:legacy-committed-extra-turn-normalized"
        ),
    )


def recover_plan2_replay_journal(
    current: AuditionLocalSaveStateEvidence,
    *,
    observation: Plan2CardHistoryObservation | None = None,
    database: str | Path = DEFAULT_DATABASE,
    root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
) -> Plan2CompletedCardReplay | None:
    """Rebuild a retained replay chain and optionally append ``current``."""

    path = plan2_replay_journal_path(current, root=root)
    entries = load_plan2_replay_journal(path)
    if not entries:
        return None
    pending_drink_receipt = load_plan2_pending_drink_receipt(
        plan2_pending_drink_receipt_path(current, root=root)
    )
    compilation = compile_plan2_native_program_catalog(database=database)
    catalog = compilation.catalog
    first = entries[0]
    first_observation = _legacy_committed_extra_turn_observation(first)
    pending_drink_receipt_consumed = False
    if first.persisted_before.state.is_native_actionable_settled:
        decision = decide_plan2_native_exam_save(
            first.persisted_before, database=database
        )
        root = _journal_replay_root(decision)
        if root is None:
            raise Plan2ReplayJournalError(
                "journal-before-bootstrap-failed",
                ",".join(value.code for value in decision.blockers),
            )
        result = replay_completed_plan2_card_history(
            first.persisted_before,
            first.persisted_transition,
            first.action,
            before_horizon=root,
            catalog=catalog,
            database=database,
            observation=first_observation,
        )
    else:
        # A drink can publish its exact pre-effect command queue and then the
        # next PLAY before the process has emitted a settled save.  Recover
        # that typed drink queue first; the first journalled card is then an
        # ordinary chained replay.  This keeps restart recovery aligned with
        # the same in-memory path instead of trying to bootstrap a knowingly
        # transitional ExamSave.
        materialized_receipt = bool(
            pending_drink_receipt is not None
            and first.persisted_before
            != pending_drink_receipt.persisted_before
        )
        if materialized_receipt:
            assert pending_drink_receipt is not None
            try:
                drink = recover_materialized_plan2_pending_drink_chain(
                    first.persisted_before,
                    pending_drink_receipt,
                    observation=first_observation,
                    catalog=catalog,
                    database=database,
                )
            except Plan2ReplayJournalError as error:
                raise Plan2ReplayJournalError(
                    "journal-prior-drink-receipt-recovery-failed",
                    str(error),
                ) from error
            pending_drink_receipt_consumed = True
        else:
            drink = recover_retained_plan2_drink_history(
                first.persisted_before,
                observation=first_observation,
                catalog=catalog,
                database=database,
            )
        if not drink.supported or drink.replay is None:
            raise Plan2ReplayJournalError(
                "journal-prior-drink-replay-failed",
                ",".join(value.code for value in drink.issues),
            )
        prior_drink = drink.replay
        result = replay_next_completed_plan2_card_history(
            prior_drink.persisted_transition,
            prior_drink.logical_after,
            first.persisted_transition,
            first.action,
            prior_replay=prior_drink,
            catalog=catalog,
            database=database,
            observation=first_observation,
        )
    if not result.supported or result.replay is None:
        raise Plan2ReplayJournalError(
            "journal-first-replay-failed",
            ",".join(value.code for value in result.issues),
        )
    replay = result.replay
    for entry in entries[1:]:
        prior_replay: Plan2CompletedCardReplay | Plan2CompletedDrinkReplay = replay
        if entry.persisted_before != replay.persisted_transition:
            # A drink is intentionally stored in its durable typed receipt,
            # not as a card-journal row.  It may therefore be the one exact
            # transaction joining two adjacent PLAY rows.  Permit only the
            # loaded receipt whose before evidence is the preceding PLAY
            # transition and whose materialized retained queue is byte-for-
            # byte the next PLAY's before evidence.  This is recovery
            # authority only; no Maa input is dispatched here.
            if (
                pending_drink_receipt is None
                or pending_drink_receipt_consumed
                or pending_drink_receipt.persisted_before
                != replay.persisted_transition
            ):
                raise Plan2ReplayJournalError("journal-chain-discontinuous")
            drink_observation = observation_from_horizon(
                replay.logical_after,
                source="journal-interstitial-pending-drink",
            )
            drink = recover_retained_plan2_drink_history(
                entry.persisted_before,
                observation=drink_observation,
                catalog=catalog,
                database=database,
                expected_action=pending_drink_receipt.action,
                persisted_before=pending_drink_receipt.persisted_before,
                before_horizon=replay.logical_after,
            )
            if not drink.supported or drink.replay is None:
                raise Plan2ReplayJournalError(
                    "journal-interstitial-drink-replay-failed",
                    ",".join(
                        value.code
                        + (":" + value.detail if value.detail else "")
                        for value in drink.issues
                    ),
                )
            if (
                drink.replay.persisted_before != replay.persisted_transition
                or drink.replay.persisted_transition != entry.persisted_before
            ):
                raise Plan2ReplayJournalError(
                    "journal-interstitial-drink-authority-mismatch"
                )
            prior_replay = drink.replay
            pending_drink_receipt_consumed = True
        chained = replay_next_completed_plan2_card_history(
            prior_replay.persisted_transition,
            prior_replay.logical_after,
            entry.persisted_transition,
            entry.action,
            prior_replay=prior_replay,
            catalog=catalog,
            database=database,
            observation=_legacy_committed_extra_turn_observation(entry),
        )
        if not chained.supported or chained.replay is None:
            raise Plan2ReplayJournalError(
                "journal-chain-replay-failed",
                ",".join(value.code for value in chained.issues),
            )
        replay = chained.replay
    if current == replay.persisted_transition:
        return replay
    playing = current.state.playing_card
    if playing is None or current.state.root_runtime is None:
        raise Plan2ReplayJournalError("journal-current-transition-invalid")
    if observation is None:
        raise Plan2ReplayJournalError("journal-current-observation-required")
    chained = replay_next_completed_plan2_card_history(
        replay.persisted_transition,
        replay.logical_after,
        current,
        Plan2NativeAction("play", playing.guid),
        prior_replay=replay,
        catalog=catalog,
        database=database,
        observation=observation,
    )
    if not chained.supported or chained.replay is None:
        raise Plan2ReplayJournalError(
            "journal-current-replay-failed",
            ",".join(
                value.code + (":" + value.detail if value.detail else "")
                for value in chained.issues
            ),
        )
    replay = chained.replay
    append_plan2_replay_journal(
        path,
        Plan2ReplayJournalEntry(
            replay.persisted_before,
            replay.persisted_transition,
            replay.action,
            observation,
        ),
    )
    return replay


def _settled_card_identity(card: object) -> tuple[object, ...]:
    return (
        getattr(card, "guid", None),
        getattr(card, "card_id", None),
        getattr(card, "base_upgrade", None),
        getattr(card, "fixed_deck_order", None),
    )


def _settled_active_cards(state: object) -> tuple[object, ...]:
    zones = getattr(state, "zones", None)
    if zones is None:
        return ()
    return tuple(
        card
        for zone_name in ("hand", "deck", "grave", "lost", "hold")
        for card in getattr(zones, zone_name)
    )


def _settled_turn_schedule_extends_exactly(
    retained: object,
    settled: object,
) -> bool:
    """Accept only the native schedule suffix created by materialized ExtraTurn."""

    retained_extra = getattr(retained, "extra_turn", None)
    settled_extra = getattr(settled, "extra_turn", None)
    retained_types = getattr(retained, "turn_parameter_types", None)
    settled_types = getattr(settled, "turn_parameter_types", None)
    if (
        type(retained_extra) is not int
        or type(settled_extra) is not int
        or not isinstance(retained_types, tuple)
        or not isinstance(settled_types, tuple)
    ):
        return False
    added = settled_extra - retained_extra
    return bool(
        added >= 0
        and len(settled_types) == len(retained_types) + added
        and settled_types[: len(retained_types)] == retained_types
    )


def _prove_single_retained_play_settled(
    current: AuditionLocalSaveStateEvidence,
    entry: Plan2ReplayJournalEntry,
    *,
    database: str | Path,
) -> Plan2SettledCardJournalProof:
    """Reconcile one exact retained PLAY without trusting partial HUD scalars."""

    before_evidence = entry.persisted_before
    retained_evidence = entry.persisted_transition
    before = before_evidence.state
    retained = retained_evidence.state
    settled = current.state

    identity_fields = (
        "run_id",
        "step_context_id",
        "step_context_digest",
        "session_transition_id",
    )
    if any(
        getattr(before_evidence, name) != getattr(retained_evidence, name)
        or getattr(before_evidence, name) != getattr(current, name)
        for name in identity_fields
    ):
        raise Plan2ReplayJournalError("journal-settled-identity-mismatch")
    if any(
        getattr(before, name) != getattr(retained, name)
        or getattr(before, name) != getattr(settled, name)
        for name in (
            "character_id",
            "setting_id",
            "exam_type",
            "step_type_value",
            "limit_turn",
        )
    ) or (
        before.turn_parameter_types != retained.turn_parameter_types
        or not _settled_turn_schedule_extends_exactly(retained, settled)
    ):
        raise Plan2ReplayJournalError("journal-settled-stage-mismatch")
    if (
        not before.is_native_actionable_settled
        or retained.is_native_actionable_settled
        or not settled.is_native_actionable_settled
        or retained.phase != 6
        or retained.playing_card is None
        or retained.root_runtime is None
        or retained.root_runtime.command_list_is_empty
        or retained.root_runtime.draw_card_guid_list
        or retained.root_runtime.is_exam_end_complete
        or before.zones.hold
        or retained.zones.hold
        or settled.zones.hold
    ):
        raise Plan2ReplayJournalError("journal-settled-boundary-invalid")

    selected_matches = tuple(
        card for card in before.zones.hand if card.guid == entry.action.card_guid
    )
    if len(selected_matches) != 1:
        raise Plan2ReplayJournalError(
            "journal-settled-requested-card-before-mismatch"
        )
    selected = selected_matches[0]
    playing = retained.playing_card
    if _settled_card_identity(selected) != _settled_card_identity(playing):
        raise Plan2ReplayJournalError(
            "journal-settled-playing-card-identity-mismatch"
        )
    if selected.runtime_state is None or playing.runtime_state is None:
        raise Plan2ReplayJournalError(
            "journal-settled-playing-card-runtime-missing"
        )
    if (
        playing.runtime_state
        != replace(
            selected.runtime_state,
            play_count=selected.runtime_state.play_count + 1,
        )
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-playing-card-runtime-mismatch"
        )

    expected_hand = tuple(
        replace(card, zone_order=index)
        for index, card in enumerate(
            card
            for card in before.zones.hand
            if card.guid != entry.action.card_guid
        )
    )
    if retained.zones.hand != expected_hand or any(
        getattr(retained.zones, zone_name)
        != getattr(before.zones, zone_name)
        for zone_name in ("deck", "grave", "lost", "hold")
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-retained-zone-mismatch"
        )
    if retained.removed_cards != before.removed_cards:
        raise Plan2ReplayJournalError(
            "journal-settled-retained-tombstone-mismatch"
        )

    commands = retained.root_runtime.command_list.to_value()
    if (
        not isinstance(commands, list)
        or len(commands) < 5
        or not all(isinstance(row, Mapping) for row in commands)
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-command-grammar-mismatch"
        )
    action_commands = []
    for row in commands:
        assert isinstance(row, Mapping)
        raw_card = row.get("_playingCard")
        raw_drink = row.get("_playingDrink")
        if not isinstance(raw_card, Mapping) or not isinstance(raw_drink, Mapping):
            raise Plan2ReplayJournalError(
                "journal-settled-command-grammar-mismatch"
            )
        raw_guid = raw_card.get("_guid")
        if raw_guid:
            card_data = raw_card.get("_cardData")
            if (
                raw_guid != entry.action.card_guid
                or not isinstance(card_data, Mapping)
                or card_data.get("_id") != selected.card_id
            ):
                raise Plan2ReplayJournalError(
                    "journal-settled-command-card-mismatch"
                )
            action_commands.append(row)
        if raw_drink.get("_id") not in {None, ""}:
            raise Plan2ReplayJournalError(
                "journal-settled-command-drink-mismatch"
            )
    first = commands[0]
    last = commands[-1]
    if (
        len(action_commands) < 4
        or first.get("_playType") != 9
        or first.get("_isSeparateStart") is not True
        or last.get("_playType") != 9
        or last.get("_isSeparateStart") is not False
        or not any(
            row.get("_playType") == 6
            and isinstance(row.get("_playingCard"), Mapping)
            and row["_playingCard"].get("_guid") == entry.action.card_guid
            for row in commands
        )
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-command-grammar-mismatch"
        )

    if (
        settled.exam_card_play_count != before.exam_card_play_count + 1
        or settled.remain_turn != entry.observation.turns_remaining
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-turn-counter-mismatch"
        )
    same_turn = (
        settled.current_turn == before.current_turn
        and settled.remain_turn == before.remain_turn
        and settled.turn_card_play_count == before.turn_card_play_count + 1
    )
    next_turn = (
        settled.current_turn == before.current_turn + 1
        and settled.remain_turn == before.remain_turn - 1
        and settled.turn_card_play_count == 0
    )
    if not (same_turn or next_turn):
        raise Plan2ReplayJournalError(
            "journal-settled-turn-boundary-mismatch"
        )

    before_cards = _settled_active_cards(before)
    current_cards = _settled_active_cards(settled)
    before_identity = tuple(_settled_card_identity(card) for card in before_cards)
    current_identity = tuple(_settled_card_identity(card) for card in current_cards)
    if (
        len({value[0] for value in before_identity}) != len(before_identity)
        or len({value[0] for value in current_identity}) != len(current_identity)
        or sorted(before_identity) != sorted(current_identity)
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-card-universe-mismatch"
        )
    requested = tuple(
        card for card in current_cards if card.guid == entry.action.card_guid
    )
    if (
        len(requested) != 1
        or _settled_card_identity(requested[0])
        != _settled_card_identity(selected)
        or requested[0].runtime_state is None
        or requested[0].runtime_state.play_count
        < playing.runtime_state.play_count
        or replace(requested[0].runtime_state, play_count=0)
        != replace(playing.runtime_state, play_count=0)
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-requested-card-mismatch"
        )

    compilation = compile_plan2_native_program_catalog(database=database)
    bootstrap = Plan2NativeExamSaveDependencies().bootstrapper(
        current,
        compilation.catalog,
        None,
        None,
    )
    if not bootstrap.simulation_ready or bootstrap.state is None:
        raise Plan2ReplayJournalError(
            "journal-settled-current-bootstrap-unavailable",
            ",".join(value.code for value in bootstrap.blockers),
        )
    fresh_root = bootstrap.state
    if (
        fresh_root.scalar.current_turn != settled.current_turn
        or fresh_root.remaining_turns != settled.remain_turn
        or fresh_root.scalar.exam_card_play_count != settled.exam_card_play_count
        or fresh_root.scalar.turn_card_play_count != settled.turn_card_play_count
        or (
            entry.observation.plays_remaining is not None
            and fresh_root.plays_remaining != entry.observation.plays_remaining
        )
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-current-bootstrap-mismatch"
        )
    for zone_name in ("hand", "deck", "grave", "lost"):
        if tuple(
            _settled_card_identity(card)
            for card in getattr(fresh_root.zones, zone_name)
        ) != tuple(
            _settled_card_identity(card)
            for card in getattr(settled.zones, zone_name)
        ):
            raise Plan2ReplayJournalError(
                "journal-settled-current-bootstrap-zone-mismatch",
                zone_name,
            )

    return Plan2SettledCardJournalProof(
        entry.action,
        before_evidence,
        retained_evidence,
        current,
        fresh_root,
        (
            "journal:retained-playing-queue-exact",
            "journal:settled-native-turn-advance-exact",
            "journal:settled-card-universe-exact",
            "journal:settled-fresh-bootstrap-ready",
        ),
    )


def _native_user_play_action_sequence(
    evidence: AuditionLocalSaveStateEvidence,
) -> tuple[tuple[int, str, int], ...]:
    """Return the exact ordered native card-play ledger without HUD input."""

    runtime = evidence.state.root_runtime
    if runtime is None:
        raise Plan2ReplayJournalError("journal-native-play-log-missing")
    opaque = runtime.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):
        raise Plan2ReplayJournalError("journal-native-play-log-shape-invalid")
    logs = opaque.get("userPlayLogList")
    if not isinstance(logs, list):
        raise Plan2ReplayJournalError("journal-native-play-log-shape-invalid")
    actions: list[tuple[int, str, int]] = []
    for row in logs:
        if not isinstance(row, Mapping):
            raise Plan2ReplayJournalError(
                "journal-native-play-log-shape-invalid"
            )
        if row.get("_cellType") != 1:
            continue
        turn = row.get("_currentTurn")
        card_ids = row.get("_playCardList")
        upgrades = row.get("_playCardUpgradeList")
        if (
            type(turn) is not int
            or turn <= 0
            or not isinstance(card_ids, list)
            or not isinstance(upgrades, list)
            or len(card_ids) != len(upgrades)
            or any(not isinstance(value, str) or not value for value in card_ids)
            or any(type(value) is not int or value < 0 for value in upgrades)
        ):
            raise Plan2ReplayJournalError(
                "journal-native-play-log-shape-invalid"
            )
        actions.extend(
            (turn, card_id, upgrades[index])
            for index, card_id in enumerate(card_ids)
        )
    return tuple(actions)


def _prove_native_logged_journal_chain_settled(
    current: AuditionLocalSaveStateEvidence,
    entries: tuple[Plan2ReplayJournalEntry, ...],
    *,
    database: str | Path,
    root: str | Path,
) -> Plan2SettledJournalChainProof:
    """Rebase a fully settled multi-PLAY journal from native truth only."""

    if len(entries) < 2:
        raise Plan2ReplayJournalError("journal-native-chain-too-short")
    if load_plan2_pending_drink_receipt(
        plan2_pending_drink_receipt_path(current, root=root)
    ) is not None:
        raise Plan2ReplayJournalError("journal-native-chain-pending-drink")
    first = entries[0]
    first_before = first.persisted_before
    first_state = first_before.state
    settled = current.state
    if not first_state.is_native_actionable_settled:
        raise Plan2ReplayJournalError("journal-native-chain-root-not-settled")
    if not settled.is_native_actionable_settled:
        raise Plan2ReplayJournalError("journal-native-chain-current-not-settled")

    identity_fields = (
        "run_id",
        "step_context_id",
        "step_context_digest",
        "session_transition_id",
    )
    stage_fields = (
        "character_id",
        "setting_id",
        "exam_type",
        "step_type_value",
        "limit_turn",
    )
    committed: list[tuple[int, str, int]] = []
    previous_transition: AuditionLocalSaveStateEvidence | None = None
    latest_playing_by_guid: dict[str, object] = {}
    for entry in entries:
        before = entry.persisted_before
        transition = entry.persisted_transition
        if previous_transition is not None and before != previous_transition:
            raise Plan2ReplayJournalError("journal-native-chain-discontinuous")
        previous_transition = transition
        if any(
            getattr(before, name) != getattr(first_before, name)
            or getattr(transition, name) != getattr(first_before, name)
            or getattr(current, name) != getattr(first_before, name)
            for name in identity_fields
        ):
            raise Plan2ReplayJournalError("journal-native-chain-identity-mismatch")
        if any(
            getattr(before.state, name) != getattr(first_state, name)
            or getattr(transition.state, name) != getattr(first_state, name)
            or getattr(settled, name) != getattr(first_state, name)
            for name in stage_fields
        ):
            raise Plan2ReplayJournalError("journal-native-chain-stage-mismatch")
        playing = transition.state.playing_card
        runtime = transition.state.root_runtime
        if (
            entry.action.kind != "play"
            or playing is None
            or playing.guid != entry.action.card_guid
            or playing.runtime_state is None
            or runtime is None
            or runtime.command_list_is_empty
            or runtime.is_exam_end_complete
        ):
            raise Plan2ReplayJournalError("journal-native-chain-row-invalid")
        committed.append(
            (
                before.state.current_turn,
                playing.card_id,
                playing.effective_upgrade,
            )
        )
        latest_playing_by_guid[playing.guid] = playing

    assert previous_transition is not None
    if not _settled_turn_schedule_extends_exactly(
        previous_transition.state,
        settled,
    ):
        raise Plan2ReplayJournalError("journal-native-chain-schedule-mismatch")
    base_actions = _native_user_play_action_sequence(first_before)
    current_actions = _native_user_play_action_sequence(current)
    expected_actions = (*base_actions, *committed)
    if current_actions != expected_actions:
        raise Plan2ReplayJournalError("journal-native-play-log-chain-mismatch")
    if (
        settled.exam_card_play_count
        != first_state.exam_card_play_count + len(entries)
        or settled.current_turn < max(value[0] for value in committed)
    ):
        raise Plan2ReplayJournalError("journal-native-chain-counter-mismatch")

    current_cards = _settled_active_cards(settled)
    for guid, playing in latest_playing_by_guid.items():
        matches = tuple(card for card in current_cards if card.guid == guid)
        playing_runtime = getattr(playing, "runtime_state", None)
        if (
            len(matches) != 1
            or _settled_card_identity(matches[0])
            != _settled_card_identity(playing)
            or getattr(matches[0], "runtime_state", None) is None
            or playing_runtime is None
            or matches[0].runtime_state.play_count
            < playing_runtime.play_count
        ):
            raise Plan2ReplayJournalError(
                "journal-native-chain-card-runtime-mismatch",
                guid,
            )

    compilation = compile_plan2_native_program_catalog(database=database)
    bootstrap = Plan2NativeExamSaveDependencies().bootstrapper(
        current,
        compilation.catalog,
        None,
        None,
    )
    if not bootstrap.simulation_ready or bootstrap.state is None:
        raise Plan2ReplayJournalError(
            "journal-settled-current-bootstrap-unavailable",
            ",".join(value.code for value in bootstrap.blockers),
        )
    fresh_root = bootstrap.state
    if (
        fresh_root.scalar.current_turn != settled.current_turn
        or fresh_root.remaining_turns != settled.remain_turn
        or fresh_root.scalar.exam_card_play_count
        != settled.exam_card_play_count
        or fresh_root.scalar.turn_card_play_count
        != settled.turn_card_play_count
        or fresh_root.scalar.score != settled.score
        or fresh_root.scalar.stamina != settled.stamina
        or fresh_root.scalar.max_stamina != settled.max_stamina
        or fresh_root.scalar.block != settled.block
        or fresh_root.zones.random_state != settled.random_state
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-current-bootstrap-mismatch"
        )
    for zone_name in ("hand", "deck", "grave", "lost"):
        native_current = tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in getattr(settled.zones, zone_name)
        )
        if getattr(fresh_root.zones, zone_name) != native_current:
            raise Plan2ReplayJournalError(
                "journal-settled-current-bootstrap-zone-mismatch",
                zone_name,
            )

    return Plan2SettledJournalChainProof(
        tuple(entry.action for entry in entries),
        entries,
        current,
        fresh_root,
        (
            "journal:native-user-play-log-chain-exact",
            "journal:journal-guid-order-exact",
            "journal:card-runtime-progress-exact",
            "journal:settled-fresh-bootstrap-ready",
        ),
    )


def _prove_one_unjournaled_native_play_settled(
    current: AuditionLocalSaveStateEvidence,
    entries: tuple[Plan2ReplayJournalEntry, ...],
    replay: Plan2CompletedCardReplay,
    *,
    database: str | Path,
) -> Plan2SettledUnjournaledPlayProof:
    """Retire a journal followed by exactly one native-logged PLAY.

    This is the structural replacement for post-submit HUD validation.  It is
    intentionally narrow: every journal action must appear in order after the
    first journal root, and the current save may add exactly one more card-play
    row.  Any second unjournaled action, identity ambiguity, counter drift, or
    non-settled save remains a hard failure.
    """

    if not entries:
        raise Plan2ReplayJournalError("journal-settled-reconcile-empty")
    state = current.state
    logical = replay.logical_after
    if not state.is_native_actionable_settled:
        raise Plan2ReplayJournalError("journal-settled-current-not-actionable")

    base_actions = _native_user_play_action_sequence(entries[0].persisted_before)
    retained_actions = _native_user_play_action_sequence(
        entries[-1].persisted_transition
    )
    current_actions = _native_user_play_action_sequence(current)
    journal_actions: list[tuple[int, str, int]] = []
    for entry in entries:
        before = entry.persisted_before.state
        playing = entry.persisted_transition.state.playing_card
        if (
            entry.action.kind != "play"
            or playing is None
            or playing.guid != entry.action.card_guid
            or type(before.current_turn) is not int
        ):
            raise Plan2ReplayJournalError(
                "journal-native-play-log-journal-action-mismatch"
            )
        journal_actions.append(
            (before.current_turn, playing.card_id, playing.effective_upgrade)
        )
    committed = tuple(journal_actions)
    if (
        retained_actions[: len(base_actions)] != base_actions
        or retained_actions[len(base_actions) :]
        != committed[: len(retained_actions) - len(base_actions)]
        or len(retained_actions) > len(base_actions) + len(committed)
    ):
        raise Plan2ReplayJournalError(
            "journal-native-play-log-retained-prefix-mismatch"
        )
    expected_committed = (*base_actions, *committed)
    if (
        len(current_actions) != len(expected_committed) + 1
        or current_actions[: len(expected_committed)] != expected_committed
    ):
        raise Plan2ReplayJournalError(
            "journal-native-play-log-tail-count-mismatch"
        )
    missing_turn, missing_card_id, missing_upgrade = current_actions[-1]
    if missing_turn != logical.scalar.current_turn:
        raise Plan2ReplayJournalError(
            "journal-native-play-log-tail-turn-mismatch"
        )

    logical_candidates = tuple(
        card
        for card in logical.zones.hand
        if card.card_id == missing_card_id
        and card.effective_upgrade == missing_upgrade
    )
    if len(logical_candidates) != 1:
        raise Plan2ReplayJournalError(
            "journal-native-play-log-tail-guid-ambiguous"
        )
    selected = logical_candidates[0]
    current_cards = _settled_active_cards(state)
    current_selected = tuple(
        card for card in current_cards if card.guid == selected.guid
    )
    if (
        len(current_selected) != 1
        or _settled_card_identity(current_selected[0])
        != _settled_card_identity(selected)
        or current_selected[0].runtime_state is None
        or current_selected[0].runtime_state.play_count
        <= selected.runtime_state.play_count
    ):
        raise Plan2ReplayJournalError(
            "journal-native-play-log-tail-runtime-mismatch"
        )

    same_turn = (
        state.current_turn == logical.scalar.current_turn
        and state.remain_turn == logical.remaining_turns
        and state.turn_card_play_count
        == logical.scalar.turn_card_play_count + 1
    )
    next_turn = (
        state.current_turn == logical.scalar.current_turn + 1
        and state.remain_turn == logical.remaining_turns - 1
        and state.turn_card_play_count == 0
    )
    if (
        state.exam_card_play_count
        != logical.scalar.exam_card_play_count + 1
        or not (same_turn or next_turn)
    ):
        raise Plan2ReplayJournalError(
            "journal-native-play-log-tail-counter-mismatch"
        )

    compilation = compile_plan2_native_program_catalog(database=database)
    bootstrap = Plan2NativeExamSaveDependencies().bootstrapper(
        current,
        compilation.catalog,
        None,
        None,
    )
    if not bootstrap.simulation_ready or bootstrap.state is None:
        raise Plan2ReplayJournalError(
            "journal-settled-current-bootstrap-unavailable",
            ",".join(value.code for value in bootstrap.blockers),
        )
    fresh_root = bootstrap.state
    if (
        fresh_root.scalar.current_turn != state.current_turn
        or fresh_root.remaining_turns != state.remain_turn
        or fresh_root.scalar.exam_card_play_count != state.exam_card_play_count
        or fresh_root.scalar.turn_card_play_count != state.turn_card_play_count
        or fresh_root.scalar.score != state.score
        or fresh_root.scalar.stamina != state.stamina
        or fresh_root.scalar.block != state.block
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-current-bootstrap-mismatch"
        )
    for zone_name in ("hand", "deck", "grave", "lost"):
        if tuple(
            _settled_card_identity(card)
            for card in getattr(fresh_root.zones, zone_name)
        ) != tuple(
            _settled_card_identity(card)
            for card in getattr(state.zones, zone_name)
        ):
            raise Plan2ReplayJournalError(
                "journal-settled-current-bootstrap-zone-mismatch",
                zone_name,
            )

    return Plan2SettledUnjournaledPlayProof(
        Plan2NativeAction("play", selected.guid),
        replay,
        current,
        fresh_root,
        (
            "journal:native-user-play-log-prefix-exact",
            "journal:native-user-play-log-one-tail-action",
            "journal:tail-card-guid-runtime-progressed",
            "journal:settled-fresh-bootstrap-ready",
        ),
    )


def reconcile_settled_plan2_replay_journal(
    current: AuditionLocalSaveStateEvidence,
    *,
    database: str | Path = DEFAULT_DATABASE,
    root: str | Path = DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
) -> (
    Plan2CompletedCardReplay
    | Plan2SettledCardJournalProof
    | Plan2SettledJournalChainProof
    | Plan2SettledUnjournaledPlayProof
):
    """Prove that a retained PLAY journal has physically settled.

    A retained native queue is already exact action-commit authority.  On a
    later startup the queue may have drained through EndTurn/TurnStart, so the
    latest save no longer contains ``playingCard``.  Clear that journal only
    when the replayed lifecycle and current save agree on the exact stage,
    turn boundary, total play counters, and ordered persistent card identity.
    Scalar/status changes at TurnStart remain owned by the settled LocalSave;
    they are not used to infer or resend the old input.
    """

    path = plan2_replay_journal_path(current, root=root)
    entries = load_plan2_replay_journal(path)
    if not entries:
        raise Plan2ReplayJournalError("journal-settled-reconcile-empty")
    retained = entries[-1].persisted_transition
    try:
        replay = recover_plan2_replay_journal(
            retained,
            database=database,
            root=root,
        )
    except Plan2ReplayJournalError as error:
        if len(entries) >= 2:
            try:
                return _prove_native_logged_journal_chain_settled(
                    current,
                    entries,
                    database=database,
                    root=root,
                )
            except Plan2ReplayJournalError:
                pass
        external_item_gap = bool(
            error.detail
            and classify_pending_replay_result(
                Plan2CardHistoryReplayResult(
                    None,
                    (Plan2CardHistoryIssue(error.detail),),
                )
            ).disposition
            is PendingReplayDisposition.WAIT_EXTERNAL_SETTLEMENT
        )
        # A quick retained transition may persist a partial in-animation HUD
        # scalar, or an exact game-owned item listener that the local horizon
        # intentionally observes only after settlement.  Do not weaken
        # ordinary retained replay: only these exact signatures enter the
        # independent settled-save proof, and only a single journal row can be
        # retired this way.
        if (
            len(entries) != 1
            or error.code != "journal-first-replay-failed"
            or not (
                error.detail == "plan2-card-history-observation-score-underflow"
                or external_item_gap
            )
        ):
            raise
        return _prove_single_retained_play_settled(
            current,
            entries[0],
            database=database,
        )
    if replay is None:
        raise Plan2ReplayJournalError("journal-settled-replay-empty")
    state = current.state
    if not state.is_native_actionable_settled:
        raise Plan2ReplayJournalError("journal-settled-current-not-actionable")
    if (
        current.run_id != retained.run_id
        or current.step_context_id != retained.step_context_id
        or current.step_context_digest != retained.step_context_digest
        or current.session_transition_id != retained.session_transition_id
        or state.character_id != retained.state.character_id
        or state.setting_id != retained.state.setting_id
        or state.exam_type != retained.state.exam_type
        or state.step_type_value != retained.state.step_type_value
        or state.limit_turn != retained.state.limit_turn
        or not _settled_turn_schedule_extends_exactly(retained.state, state)
    ):
        raise Plan2ReplayJournalError("journal-settled-identity-mismatch")
    logical = replay.logical_after
    if (
        state.current_turn != logical.scalar.current_turn
        or state.remain_turn != logical.remaining_turns
        or state.exam_card_play_count != logical.scalar.exam_card_play_count
        or state.turn_card_play_count != logical.scalar.turn_card_play_count
    ):
        return _prove_one_unjournaled_native_play_settled(
            current,
            entries,
            replay,
            database=database,
        )

    def persistent(card: object) -> tuple[object, ...]:
        return (
            getattr(card, "guid", None),
            getattr(card, "card_id", None),
            getattr(card, "base_upgrade", None),
            getattr(card, "fixed_deck_order", None),
        )

    for zone_name, logical_cards, current_cards in (
        ("hand", logical.zones.hand, state.zones.hand),
        ("deck", logical.zones.deck, state.zones.deck),
        ("grave", logical.zones.grave, state.zones.grave),
        ("lost", logical.zones.lost, state.zones.lost),
    ):
        if tuple(map(persistent, logical_cards)) != tuple(
            map(persistent, current_cards)
        ):
            raise Plan2ReplayJournalError(
                "journal-settled-zone-identity-mismatch",
                zone_name,
            )
    requested = tuple(
        card
        for cards in (
            state.zones.hand,
            state.zones.deck,
            state.zones.grave,
            state.zones.lost,
        )
        for card in cards
        if card.guid == replay.action.card_guid
    )
    logical_requested = tuple(
        card
        for cards in (
            logical.zones.hand,
            logical.zones.deck,
            logical.zones.grave,
            logical.zones.lost,
        )
        for card in cards
        if card.guid == replay.action.card_guid
    )
    requested_runtime = (
        None if len(requested) != 1 else requested[0].runtime_state
    )
    logical_runtime = (
        None if len(logical_requested) != 1 else logical_requested[0].runtime_state
    )
    if (
        len(requested) != 1
        or len(logical_requested) != 1
        or requested[0].card_id != replay.card_id
        or requested_runtime is None
        or logical_runtime is None
        # LocalSave's object-lifecycle counter can advance farther than the
        # solver semantic count while a retained queue drains.  It may not be
        # behind the replay-proven action count; that catches a reset/tampered
        # requested card without demanding equality between two authorities.
        or requested_runtime.play_count < logical_runtime.play_count
    ):
        raise Plan2ReplayJournalError(
            "journal-settled-requested-card-mismatch"
        )
    if len(entries) == 1 and (
        state.score != logical.scalar.score
        or state.stamina != logical.scalar.stamina
        or state.max_stamina != logical.scalar.max_stamina
        or state.block != logical.scalar.block
    ):
        # The action/zone/runtime receipt is exact, but a passive or outer
        # effect made the settled scalar differ from the predicted suffix.
        # Re-bootstrap from the settled LocalSave instead of returning the
        # simulator descendant as the next shadow authority.
        return _prove_single_retained_play_settled(
            current,
            entries[0],
            database=database,
        )
    return replay


__all__ = [
    "PendingReplayDisposition",
    "PendingReplayResult",
    "Plan2PendingDrinkDispatchWitness",
    "Plan2PendingDrinkReceipt",
    "Plan2PendingPlayReceipt",
    "Plan2ReplayJournalEntry",
    "Plan2ReplayJournalError",
    "Plan2ReplayJournalSink",
    "Plan2SettledCardJournalProof",
    "Plan2SettledJournalChainProof",
    "Plan2SettledUnjournaledPlayProof",
    "append_plan2_replay_journal",
    "bind_plan2_pending_drink_action_identity",
    "classify_pending_replay_result",
    "clear_plan2_pending_drink_receipt",
    "clear_plan2_pending_play_receipt",
    "clear_plan2_replay_journal",
    "load_plan2_pending_drink_receipt",
    "load_plan2_pending_play_receipt",
    "load_plan2_replay_journal",
    "migrate_plan2_pending_drink_barrier_from_report",
    "migrate_plan2_pending_drink_receipt_from_report",
    "observation_from_horizon",
    "plan2_pending_drink_receipt_path",
    "plan2_pending_play_receipt_path",
    "plan2_replay_journal_path",
    "recover_materialized_plan2_pending_drink_chain",
    "recover_plan2_replay_journal",
    "reconcile_settled_plan2_replay_journal",
    "rollback_plan2_pending_drink_barrier_to_prior",
    "rollback_uncommitted_plan2_pending_drink_receipt",
    "save_plan2_pending_drink_receipt",
    "save_plan2_pending_play_receipt",
    "prove_plan2_submitted_play_acceptance",
]
