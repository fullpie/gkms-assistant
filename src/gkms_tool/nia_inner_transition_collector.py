"""Fail-closed side-car collection of real Maa N.I.A. inner transitions.

The native unattended loop already emits one
``Plan2NativeUnattendedStepRecord`` after an action has been submitted and a
settled ``ExamSaveData`` boundary has been read.  This module is a record sink
plus a separate pending-boundary store for actions whose submitted input is
proven but whose next save is still transitional.  It does not choose an
action, retry input, read a controller, or use simulator state as a substitute
for the game.

It is *not* wired to ``MaaWin32Session.run_maa_baseline_exam``.  That API
submits one opaque CustomAction task and currently exposes only a heartbeat and
the final task result; it has no reliable per-action identity/candidate hook.
The explicit blocker is exported below so callers cannot mistake this sink for
baseline-action telemetry.

The caller must provide the *complete* legal candidate set for the same
pre-action boundary and explicitly mark that set complete.  A partial set is
never promoted to the inner-transition dataset.  Pending rows contain no
``state_after`` or reward and are promoted only when a later caller supplies
an exact proof plus observed settled evidence.  This keeps the existing
behaviour-only telemetry and leaderboard reports out of the full-RL gate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any

from .audition_local_save_state import AuditionLocalSaveStateEvidence
from .nia_inner_transition_dataset import (
    DEFAULT_OUTPUT,
    NiaInnerTransition,
    NiaInnerTransitionCollection,
    NiaInnerTransitionRejection,
    SOURCE_UNKNOWN,
    try_build_nia_inner_transition,
    write_nia_inner_transition_export,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeDrinkAction,
    Plan2NativeOfflineAction,
)
from .plan2_native_unattended_loop import Plan2NativeUnattendedStepRecord


SOURCE_NATIVE_UNATTENDED_RECORD_SINK = "native_unattended_record_sink"
COLLECTOR_SCHEMA = "gkms.nia-inner-transition-collector.v2"
PLAN2_NATIVE_ENUMERATOR_AUTHORITY = "plan2-native-enumerator"
PENDING_SCHEMA = "gkms.nia-inner-transition-pending.v1"
DECISION_RECEIPT_SCHEMA = "gkms.plan2-learning-decision-receipt.v1"
MAA_BASELINE_RUNTIME_BLOCKER = (
    "maa-completion-baseline-custom-action-has-no-reliable-per-action-"
    "settled-examsave-boundary"
)


def _action_to_dict(action: object) -> object:
    """Serialize only the two typed native actions accepted by the Maa loop."""

    if isinstance(action, Plan2NativeDrinkAction):
        return {
            "kind": action.kind,
            "slot_index": action.slot_index,
            "instance_id": action.instance_id,
            "drink_id": action.drink_id,
            "selected_card_guid": action.selected_card_guid,
            "action_id": action.action_id,
        }
    if isinstance(action, Plan2NativeAction):
        return {
            "kind": action.kind,
            "card_guid": action.card_guid,
            "action_id": action.action_id,
        }
    if isinstance(action, Mapping):
        # Candidate providers may already expose a JSON candidate.  The
        # schema validator below still owns the shape/identity checks.
        return dict(action)
    raise TypeError(f"unsupported Plan2 action candidate: {type(action).__name__}")


@dataclass(frozen=True, slots=True)
class NiaCompleteLegalCandidates:
    """A candidate list whose completeness is an explicit caller claim.

    The claim is intentionally not inferred from the chosen action or from a
    planner's principal path.  A future Maa reader may construct this from a
    settled native hand/drink surface; a simulator-only partial list must use
    ``complete=False`` and will be rejected.
    """

    candidates: tuple[object, ...]
    authority: str
    complete: bool = True

    def __post_init__(self) -> None:
        values = tuple(self.candidates)
        if not values:
            raise ValueError("complete legal candidates must not be empty")
        if not isinstance(self.authority, str) or not self.authority.strip():
            raise ValueError("candidate authority must be non-empty text")
        if type(self.complete) is not bool:
            raise TypeError("candidate completeness must be boolean")
        object.__setattr__(self, "candidates", values)

    @classmethod
    def from_actions(
        cls,
        actions: Sequence[Plan2NativeOfflineAction],
        *,
        authority: str,
        complete: bool = True,
    ) -> "NiaCompleteLegalCandidates":
        return cls(tuple(actions), authority=authority, complete=complete)


LegalCandidateProvider = Callable[
    [Plan2NativeUnattendedStepRecord], NiaCompleteLegalCandidates | None
]


def _proof_payload(value: object, label: str) -> dict[str, object]:
    """Detach one caller-owned proof without accepting an empty claim."""

    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        to_dict = getattr(value, "to_dict", None)
        if not callable(to_dict):
            action = getattr(value, "action", None)
            before = getattr(value, "persisted_before", None)
            if before is None:
                journal_tail = getattr(value, "journal_tail", None)
                before = getattr(journal_tail, "persisted_before", None)
            after = getattr(value, "settled_current", None)
            payload = {
                "kind": type(value).__name__,
                "action_id": (
                    None if action is None else getattr(action, "action_id", None)
                ),
                "before_digest": (
                    None if before is None else before.digest()
                ),
                "after_digest": None if after is None else after.digest(),
            }
        else:
            raw = to_dict()
            payload = dict(raw) if isinstance(raw, Mapping) else {}
    if not payload:
        raise ValueError(f"{label} must be a non-empty proof object")
    # Round-trip through JSON to keep the pending file detached from mutable
    # caller objects and to reject captures/values that cannot be persisted.
    try:
        detached = json.loads(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not JSON-serializable") from error
    if not isinstance(detached, dict) or not detached:
        raise ValueError(f"{label} must be a non-empty proof object")
    return detached


def _action_id(value: object) -> str:
    if isinstance(value, Plan2NativeDrinkAction):
        return value.action_id
    if isinstance(value, Plan2NativeAction):
        return value.action_id
    raise TypeError(f"unsupported Plan2 action: {type(value).__name__}")


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_plan2_learning_decision_receipt(
    record: Plan2NativeUnattendedStepRecord,
    before: AuditionLocalSaveStateEvidence,
    *,
    source_id: str,
    action: Mapping[str, object],
    legal_candidates: Sequence[Mapping[str, object]],
    retained_proof: bool = False,
) -> dict[str, object]:
    """Project policy provenance once from the shared unattended record."""

    decision = record.decision
    record_action = record.action
    if (
        decision is None
        or getattr(decision, "decision_ready", None) is not True
        or record_action is None
        or getattr(decision, "best_action", None) != record_action
    ):
        raise ValueError("learning decision receipt has no ready matching decision")
    execution_object = record.execution
    if not retained_proof and (
        execution_object is None
        or execution_object.accepted is not True
        or execution_object.replan is True
    ):
        raise ValueError("learning decision receipt has no accepted execution")
    raw_source = getattr(decision, "policy_source", None)
    if isinstance(raw_source, str) and raw_source:
        policy_source = raw_source
        prefix, separator, suffix = raw_source.partition(":")
        owner = {
            "offline_rl": "offline_rl",
            "behavior_cloning": "exact_exam_bc",
            "learned-chain": "legacy_learned_chain",
        }.get(prefix, "other_policy")
        policy_id = suffix if separator and suffix else None
    else:
        source_kind = getattr(decision, "source_kind", None)
        source_kind = (
            source_kind
            if isinstance(source_kind, str) and source_kind
            else "native_expectimax"
        )
        owner = "formal_planner"
        policy_source = None
        policy_id = None
    probability = getattr(decision, "policy_probability", None)
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        probability = None
    raw_legal_action_ids = getattr(decision, "policy_legal_action_ids", ())
    policy_legal_action_ids = (
        [value for value in raw_legal_action_ids if isinstance(value, str) and value]
        if isinstance(raw_legal_action_ids, Sequence)
        and not isinstance(raw_legal_action_ids, (str, bytes, bytearray))
        else []
    )
    execution_payload = (
        execution_object.to_dict()
        if execution_object is not None
        and callable(getattr(execution_object, "to_dict", None))
        else None
    )
    action_id = action.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("learning decision action_id is unavailable")
    boundary_identity = {
        "source_id": source_id,
        "run_id": before.run_id,
        "step_context_id": before.step_context_id,
        "step_context_digest": before.step_context_digest,
        "session_transition_id": before.session_transition_id,
        "source_sha256": before.source_sha256,
        "evidence_before_digest": before.digest(),
        "action": dict(action),
        "legal_candidates": [dict(value) for value in legal_candidates],
    }
    legal_boundary_digest = _canonical_digest(boundary_identity)
    receipt_id = _canonical_digest(
        {
            "schema": DECISION_RECEIPT_SCHEMA,
            "legal_boundary_digest": legal_boundary_digest,
            "action_id": action_id,
            "owner": owner,
            "policy_id": policy_id,
        }
    )
    return {
        "schema": DECISION_RECEIPT_SCHEMA,
        "decision_receipt_id": receipt_id,
        "legal_boundary_digest": legal_boundary_digest,
        "owner": owner,
        "policy_source": policy_source,
        "policy_id": policy_id,
        "formal_source": source_kind if owner == "formal_planner" else None,
        "probability": probability,
        "action_id": action_id,
        "legal_action_ids": policy_legal_action_ids,
        "execution": execution_payload,
    }


def merge_plan2_learning_decision_receipt_metadata(
    metadata: dict[str, object],
    receipt: Mapping[str, object],
) -> None:
    metadata.update(
        {
            "decision_receipt_id": receipt["decision_receipt_id"],
            "legal_boundary_digest": receipt["legal_boundary_digest"],
            "policy_owner": receipt["owner"],
            "policy_source": receipt["policy_source"],
            "policy_id": receipt["policy_id"],
            "policy_probability": receipt["probability"],
            "policy_action_id": receipt["action_id"],
            "policy_legal_action_ids": list(receipt["legal_action_ids"]),
            "policy_execution": receipt["execution"],
            "decision_receipt": dict(receipt),
        }
    )


def _native_user_action_log(
    evidence: AuditionLocalSaveStateEvidence,
) -> tuple[tuple[str, ...], tuple[str, ...], bool] | None:
    """Return cumulative native card/drink actions when the save exposes them.

    ``userPlayLogList`` is stronger evidence than turn counters: a single
    delayed settled save can span several submitted inputs while still looking
    superficially plausible.  The third return value says whether at least one
    native log row exists; empty synthetic fixtures deliberately fall back to
    the scalar progression checks below.
    """

    runtime = evidence.state.root_runtime
    if runtime is None:
        return None
    opaque = runtime.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):
        return None
    rows = opaque.get("userPlayLogList")
    if not isinstance(rows, list):
        return None

    cards: list[str] = []
    drinks: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        raw_cards = row.get("_playCardList", [])
        if not isinstance(raw_cards, list) or any(
            not isinstance(value, str) or not value for value in raw_cards
        ):
            return None
        cards.extend(raw_cards)
        trigger_id = row.get("_triggerId", "")
        if not isinstance(trigger_id, str):
            return None
        if trigger_id.startswith("pdrink_"):
            drinks.append(trigger_id)
    return tuple(cards), tuple(drinks), bool(rows)


def _sequence_delta(
    before: Sequence[str],
    after: Sequence[str],
) -> tuple[str, ...] | None:
    """Return an append-only suffix, or ``None`` for a rewritten log."""

    left = tuple(before)
    right = tuple(after)
    if len(right) < len(left) or right[: len(left)] != left:
        return None
    return right[len(left) :]


def _card_id_for_guid(
    evidence: AuditionLocalSaveStateEvidence,
    guid: str,
) -> str | None:
    for card in evidence.state.zones.all_cards:
        if card.guid == guid:
            return card.card_id
    return None


def _immediate_action_boundary(
    before: AuditionLocalSaveStateEvidence,
    after: AuditionLocalSaveStateEvidence,
    action: Plan2NativeOfflineAction,
) -> tuple[bool, dict[str, object]]:
    """Prove that ``after`` is the first settled state after ``action``.

    Native action logs are used whenever present.  Scalar counters remain a
    compatibility fallback for old fixtures and saves without those logs.
    """

    before_state = before.state
    after_state = after.state
    turn_delta = after_state.current_turn - before_state.current_turn
    card_play_delta = (
        after_state.exam_card_play_count - before_state.exam_card_play_count
    )
    audit: dict[str, object] = {
        "turn_delta": turn_delta,
        "exam_card_play_count_delta": card_play_delta,
        "native_user_action_log_used": False,
    }

    if isinstance(action, Plan2NativeDrinkAction):
        scalar_valid = turn_delta == 0 and card_play_delta == 0
    elif isinstance(action, Plan2NativeAction) and action.kind == "end_turn":
        # The final skip can close the exam without incrementing
        # ``current_turn`` (the terminal save is written at the last turn),
        # while ordinary skips advance exactly one turn.  Keep the explicit
        # terminal flag and remaining-turn boundary; a same-turn changed save
        # without those proofs remains rejected.
        terminal_after = bool(
            after_state.root_runtime is not None
            and after_state.root_runtime.is_exam_end_complete
        )
        scalar_valid = (
            card_play_delta == 0
            and (
                turn_delta == 1
                or (
                    terminal_after
                    and turn_delta == 0
                    and after_state.remain_turn == 0
                )
            )
        )
    else:
        scalar_valid = turn_delta in {0, 1} and card_play_delta in {0, 1}
    if not scalar_valid:
        return False, audit

    before_log = _native_user_action_log(before)
    after_log = _native_user_action_log(after)
    if before_log is None or after_log is None or not (before_log[2] or after_log[2]):
        return True, audit

    play_delta = _sequence_delta(before_log[0], after_log[0])
    drink_delta = _sequence_delta(before_log[1], after_log[1])
    audit.update(
        {
            "native_user_action_log_used": True,
            "native_play_card_delta": (
                None if play_delta is None else list(play_delta)
            ),
            "native_drink_delta": (
                None if drink_delta is None else list(drink_delta)
            ),
        }
    )
    if play_delta is None or drink_delta is None:
        return False, audit

    if isinstance(action, Plan2NativeDrinkAction):
        return (
            play_delta == () and drink_delta == (action.drink_id,),
            audit,
        )
    if isinstance(action, Plan2NativeAction) and action.kind == "end_turn":
        return play_delta == () and drink_delta == (), audit

    assert isinstance(action, Plan2NativeAction)
    selected_card_id = _card_id_for_guid(before, action.card_guid)
    audit["submitted_card_id"] = selected_card_id
    return (
        selected_card_id is not None
        and play_delta == (selected_card_id,)
        and drink_delta == (),
        audit,
    )


def _keyless_exact_action_progress(
    after: AuditionLocalSaveStateEvidence,
    action: Plan2NativeOfflineAction,
    audit: Mapping[str, object],
) -> bool:
    """Require exact progress before automatically owning an unkeyed row."""

    if audit.get("native_user_action_log_used") is True:
        return True
    if isinstance(action, Plan2NativeAction) and action.kind == "end_turn":
        runtime = after.state.root_runtime
        return bool(
            audit.get("turn_delta") == 1
            or (
                runtime is not None
                and runtime.is_exam_end_complete
                and after.state.remain_turn == 0
            )
        )
    # PLAY without its native log and DRINK without an exact inventory/action
    # delta remain retryable pending. Scalar score/stamina changes alone can
    # be caused by support effects and are not an ownership proof.
    return False


def _normalize_binding_override(
    source_id_override: str | None,
    run_binding_id: str | None,
) -> str | None:
    """Normalize the two public names for an optional run binding.

    ``run_binding_id`` is the descriptive name used by live composition;
    ``source_id_override`` remains available to side-car callers.  Supplying
    both is allowed only when they agree.  ``None`` preserves the historical
    source-id/digest/key behavior byte-for-byte.
    """

    for label, value in (
        ("source_id_override", source_id_override),
        ("run_binding_id", run_binding_id),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{label} must be non-empty text when provided")
    if (
        source_id_override is not None
        and run_binding_id is not None
        and source_id_override != run_binding_id
    ):
        raise ValueError("source_id_override and run_binding_id must agree")
    return source_id_override if source_id_override is not None else run_binding_id


def _effective_source_id(
    evidence: AuditionLocalSaveStateEvidence,
    source_id_override: str | None = None,
    run_binding_id: str | None = None,
) -> str:
    override = _normalize_binding_override(source_id_override, run_binding_id)
    return evidence.run_id if override is None else override


def _action_from_dict(value: object) -> Plan2NativeOfflineAction:
    if not isinstance(value, Mapping):
        raise ValueError("pending action must be an object")
    kind = value.get("kind")
    if kind == "drink":
        selected = value.get("selected_card_guid", "")
        if not isinstance(selected, str):
            raise ValueError("pending drink selected_card_guid is invalid")
        return Plan2NativeDrinkAction(
            value.get("slot_index"),
            value.get("instance_id"),
            value.get("drink_id"),
            selected,
        )
    if kind in {"play", "end_turn"}:
        return Plan2NativeAction(kind, value.get("card_guid", ""))
    raise ValueError("pending action kind is unsupported")


def _same_stage_identity(
    before: AuditionLocalSaveStateEvidence,
    after: AuditionLocalSaveStateEvidence,
) -> bool:
    """Match the persisted ExamSave stage without requiring equal bytes."""

    if not isinstance(before, AuditionLocalSaveStateEvidence) or not isinstance(
        after, AuditionLocalSaveStateEvidence
    ):
        return False
    if any(
        getattr(before, name) != getattr(after, name)
        for name in (
            "run_id",
            "step_context_id",
            "step_context_digest",
            "session_transition_id",
            "source_path",
            "source_type",
        )
    ):
        return False
    left = before.state
    right = after.state
    if any(
        getattr(left, name) != getattr(right, name)
        for name in (
            "character_id",
            "setting_id",
            "exam_type",
            "step_type_value",
            "limit_turn",
            "max_stamina",
            "vocal_bonus_permille",
            "dance_bonus_permille",
            "visual_bonus_permille",
        )
    ):
        return False
    added_turns = right.extra_turn - left.extra_turn
    return bool(
        added_turns >= 0
        and len(right.turn_parameter_types)
        == len(left.turn_parameter_types) + added_turns
        and right.turn_parameter_types[: len(left.turn_parameter_types)]
        == left.turn_parameter_types
    )


def _settled_or_terminal(evidence: AuditionLocalSaveStateEvidence) -> bool:
    runtime = evidence.state.root_runtime
    return bool(
        runtime is not None
        and (
            evidence.state.is_native_actionable_settled
            or runtime.is_exam_end_complete
        )
    )


def _pending_key(
    evidence: AuditionLocalSaveStateEvidence,
    action: object,
    *,
    source_id: str | None = None,
) -> str:
    return "|".join(
        (
            evidence.run_id if source_id is None else source_id,
            evidence.step_context_id,
            evidence.source_path,
            evidence.source_sha256,
            _action_id(action),
        )
    )


def _boundary_key(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    source_id: str | None = None,
) -> tuple[str, str, str]:
    return (
        evidence.run_id if source_id is None else source_id,
        evidence.step_context_id,
        evidence.source_path,
    )


def _next_transition_step(
    transitions: Sequence[NiaInnerTransition],
    pending: Sequence["NiaPendingInnerTransition"],
    source_id: str,
) -> int:
    used = [
        value.step
        for value in transitions
        if value.source_id == source_id
    ]
    used.extend(
        value.transition_step
        for value in pending
        if value.source_id == source_id
    )
    return max(used, default=0) + 1


@dataclass(frozen=True, slots=True)
class NiaPendingInnerTransition:
    """Incomplete before/action boundary held outside the RL dataset."""

    key: str
    source_id: str
    transition_step: int
    kind: str
    before: AuditionLocalSaveStateEvidence
    legal_candidates: tuple[object, ...]
    action: object
    stage_proof: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("pending key must be non-empty text")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("pending source_id must be non-empty text")
        if type(self.transition_step) is not int or self.transition_step < 1:
            raise ValueError("pending transition_step must be positive")
        if self.kind not in {"submitted-unsettled", "play-retained"}:
            raise ValueError("unsupported pending transition kind")
        if not isinstance(self.before, AuditionLocalSaveStateEvidence):
            raise TypeError("pending before must be typed evidence")
        values = tuple(self.legal_candidates)
        if not values:
            raise ValueError("pending legal_candidates must be non-empty")
        if not isinstance(self.stage_proof, Mapping) or not self.stage_proof:
            raise ValueError("pending stage_proof must be non-empty")
        _action_id(self.action)
        object.__setattr__(self, "legal_candidates", values)
        object.__setattr__(self, "stage_proof", dict(self.stage_proof))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": PENDING_SCHEMA,
            "key": self.key,
            "source_id": self.source_id,
            "transition_step": self.transition_step,
            "kind": self.kind,
            "before": self.before.to_dict(),
            "legal_candidates": [
                _action_to_dict(value) for value in self.legal_candidates
            ],
            "action": _action_to_dict(self.action),
            "stage_proof": dict(self.stage_proof),
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaPendingInnerTransition":
        if not isinstance(value, Mapping):
            raise ValueError("pending row must be an object")
        if value.get("schema") != PENDING_SCHEMA:
            raise ValueError("pending schema is unsupported")
        raw_before = value.get("before")
        raw_candidates = value.get("legal_candidates")
        raw_proof = value.get("stage_proof")
        if not isinstance(raw_before, Mapping) or not isinstance(raw_candidates, Sequence) or isinstance(
            raw_candidates, (str, bytes, bytearray)
        ) or not isinstance(raw_proof, Mapping):
            raise ValueError("pending row shape is invalid")
        return cls(
            key=value.get("key"),
            source_id=value.get("source_id"),
            transition_step=value.get("transition_step"),
            kind=value.get("kind"),
            before=AuditionLocalSaveStateEvidence.from_dict(raw_before),
            legal_candidates=tuple(_action_from_dict(item) for item in raw_candidates),
            action=_action_from_dict(value.get("action")),
            stage_proof=dict(raw_proof),
        )


def build_plan2_native_enumerator_candidate_provider(
    orchestrator: object,
) -> LegalCandidateProvider:
    """Adapt a bound Plan2 planner candidate side-channel for the collector.

    The bound orchestrator owns the catalog cache and exposes
    ``plan2_native_candidate_provider``.  This adapter passes the exact record
    decision and its pre-action ExamSave to that callable, then publishes the
    returned typed actions as an explicit ``NiaCompleteLegalCandidates``
    authority.  A ``None``/empty result remains an abstention; it is never
    converted into a complete claim.
    """

    provider = getattr(orchestrator, "plan2_native_candidate_provider", None)
    if not callable(provider):
        raise TypeError(
            "orchestrator must expose callable plan2_native_candidate_provider"
        )

    def collect(record: Plan2NativeUnattendedStepRecord) -> NiaCompleteLegalCandidates | None:
        if not isinstance(record, Plan2NativeUnattendedStepRecord):
            raise TypeError("record must be Plan2NativeUnattendedStepRecord")
        if record.decision is None or record.evidence_before is None:
            return None
        actions = provider(record.decision, record.evidence_before)
        if actions is None:
            return None
        values = tuple(actions)
        if not values:
            return None
        return NiaCompleteLegalCandidates.from_actions(
            values,
            authority=PLAN2_NATIVE_ENUMERATOR_AUTHORITY,
            complete=True,
        )

    return collect


def _is_settled_or_terminal(evidence: AuditionLocalSaveStateEvidence) -> bool:
    state = evidence.state
    runtime = state.root_runtime
    return bool(
        runtime is not None
        and (
            state.is_native_actionable_settled
            or runtime.is_exam_end_complete
        )
    )


def _same_step_context(
    before: AuditionLocalSaveStateEvidence,
    after: AuditionLocalSaveStateEvidence,
) -> bool:
    """Require one physical run and one native ExamSave stage boundary."""

    before_state = before.state
    after_state = after.state
    return (
        before.run_id == after.run_id
        and before.step_context_id == after.step_context_id
        and before.step_context_digest == after.step_context_digest
        and before.session_transition_id == after.session_transition_id
        and before.source_path == after.source_path
        and before.source_type == after.source_type
        and before_state.character_id == after_state.character_id
        and before_state.setting_id == after_state.setting_id
        and before_state.exam_type == after_state.exam_type
        and before_state.step_type_value == after_state.step_type_value
        and before_state.limit_turn == after_state.limit_turn
    )


def _rejection(
    *,
    source_id: str,
    step: int,
    reasons: Sequence[str],
    missing_fields: Sequence[str] = (),
) -> NiaInnerTransitionRejection:
    return NiaInnerTransitionRejection(
        source=SOURCE_NATIVE_UNATTENDED_RECORD_SINK,
        source_id=source_id or SOURCE_UNKNOWN,
        step=step if isinstance(step, int) and not isinstance(step, bool) else 0,
        reasons=tuple(reasons),
        missing_fields=tuple(missing_fields),
    )


def try_collect_nia_inner_transition(
    record: Plan2NativeUnattendedStepRecord,
    legal: NiaCompleteLegalCandidates | None,
    *,
    source_id_override: str | None = None,
    run_binding_id: str | None = None,
) -> tuple[NiaInnerTransition | None, NiaInnerTransitionRejection | None]:
    """Build one transition from a completed real-Maa loop record.

    Every failure returns a structured rejection and performs no filesystem
    write.  In particular, a same-save logical replay (used only to continue
    a drink animation) is not a real settled post-action ExamSave and is
    rejected here.
    """

    if not isinstance(record, Plan2NativeUnattendedStepRecord):
        raise TypeError("record must be Plan2NativeUnattendedStepRecord")
    before = record.evidence_before
    after = record.actual_next_evidence
    source_id = (
        before.run_id
        if isinstance(before, AuditionLocalSaveStateEvidence)
        else SOURCE_UNKNOWN
    )
    binding_override = _normalize_binding_override(
        source_id_override,
        run_binding_id,
    )
    if isinstance(before, AuditionLocalSaveStateEvidence):
        source_id = _effective_source_id(
            before,
            source_id_override=binding_override,
        )
    step = record.step_index

    if before is None:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("state-before-missing",),
            missing_fields=("state_before",),
        )
    if after is None:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("state-after-missing",),
            missing_fields=("state_after",),
        )
    if record.action is None:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("action-missing",),
            missing_fields=("action",),
        )
    if record.execution is None or not record.execution.accepted:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("action-not-accepted",),
        )
    if record.execution.replan:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("action-was-replan",),
        )
    if record.decision is None or not record.decision.decision_ready:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("decision-boundary-not-ready",),
        )
    if record.decision.best_action != record.action:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("actual-action-does-not-match-decision",),
        )
    if not _is_settled_or_terminal(before):
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("state-before-not-settled",),
        )
    if not _is_settled_or_terminal(after):
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("state-after-not-settled",),
        )
    if not _same_step_context(before, after):
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("run-stage-step-context-mismatch",),
        )
    # A same-evidence logical replay can be useful for continuing the live
    # runner, but it does not provide an observed post-action ExamSave state.
    if before.digest() == after.digest():
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("post-action-examsave-did-not-change",),
        )
    immediate, action_boundary_audit = _immediate_action_boundary(
        before,
        after,
        record.action,
    )
    if not immediate:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("intervening-action-detected",),
        )
    if legal is None:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("legal-candidates-missing",),
            missing_fields=("legal_candidates",),
        )
    if not isinstance(legal, NiaCompleteLegalCandidates):
        raise TypeError("legal candidate provider must return NiaCompleteLegalCandidates or None")
    if not legal.complete:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=("legal-candidates-not-complete",),
            missing_fields=("legal_candidates",),
        )

    try:
        candidates = tuple(_action_to_dict(value) for value in legal.candidates)
        action = _action_to_dict(record.action)
    except (TypeError, ValueError) as error:
        return None, _rejection(
            source_id=source_id,
            step=step,
            reasons=(f"action-serialization-failed:{type(error).__name__}",),
        )

    score_delta = after.state.score - before.state.score
    metadata: dict[str, object] = {
        "collector_schema": COLLECTOR_SCHEMA,
        "candidate_authority": legal.authority,
        "run_id": before.run_id,
        "step_context_id": before.step_context_id,
        "step_context_digest": before.step_context_digest,
        "session_transition_id": before.session_transition_id,
        "exam_type": before.state.exam_type,
        "exam_step_type": before.state.step_type_value,
        "source_before_sha256": before.source_sha256,
        "source_after_sha256": after.source_sha256,
        "evidence_before_digest": before.digest(),
        "evidence_after_digest": after.digest(),
        "score_before": before.state.score,
        "score_after": after.state.score,
        "score_delta": score_delta,
        "prediction_matches_actual": record.prediction_matches_actual,
        "action_boundary_audit": action_boundary_audit,
    }
    decision_receipt = build_plan2_learning_decision_receipt(
        record,
        before,
        source_id=source_id,
        action=action,
        legal_candidates=candidates,
    )
    merge_plan2_learning_decision_receipt_metadata(metadata, decision_receipt)
    if binding_override is not None:
        metadata.update(
            {
                "evidence_run_id": before.run_id,
                "run_binding_id": binding_override,
            }
        )
    row: dict[str, object] = {
        "state_before": before.state.to_dict(),
        "legal_candidates": candidates,
        "action": action,
        "state_after": after.state.to_dict(),
        "source": SOURCE_NATIVE_UNATTENDED_RECORD_SINK,
        "source_id": source_id,
        "step": record.step_index,
        "reward": score_delta,
        "terminal": bool(
            after.state.root_runtime is not None
            and after.state.root_runtime.is_exam_end_complete
        ),
        "metadata": metadata,
    }
    transition, rejection = try_build_nia_inner_transition(
        row,
        source=SOURCE_NATIVE_UNATTENDED_RECORD_SINK,
        source_id=source_id,
        step=record.step_index,
    )
    return transition, rejection


@dataclass(slots=True)
class NiaInnerTransitionCollector:
    """Append accepted real-Maa transitions as a non-decision record sink."""

    candidate_provider: LegalCandidateProvider
    output: Path = field(default_factory=lambda: DEFAULT_OUTPUT)
    pending_output: Path | None = None
    source_id_override: str | None = None
    run_binding_id: str | None = None
    _transitions: list[NiaInnerTransition] = field(default_factory=list, init=False)
    _rejections: list[NiaInnerTransitionRejection] = field(default_factory=list, init=False)
    _seen: set[tuple[str, str, int]] = field(default_factory=set, init=False)
    _pending: dict[str, NiaPendingInnerTransition] = field(
        default_factory=dict, init=False
    )

    def __post_init__(self) -> None:
        if not callable(self.candidate_provider):
            raise TypeError("candidate_provider must be callable")
        _normalize_binding_override(self.source_id_override, self.run_binding_id)
        self.output = Path(self.output)
        self.pending_output = (
            self.output.with_suffix(".pending.json")
            if self.pending_output is None
            else Path(self.pending_output)
        )
        self._load_existing()
        self._load_pending()

    def _binding_override(self) -> str | None:
        return _normalize_binding_override(
            self.source_id_override,
            self.run_binding_id,
        )

    def _source_id_for(
        self,
        evidence: AuditionLocalSaveStateEvidence | None,
    ) -> str:
        override = self._binding_override()
        if isinstance(evidence, AuditionLocalSaveStateEvidence):
            return _effective_source_id(evidence, source_id_override=override)
        return SOURCE_UNKNOWN if override is None else override

    def _load_existing(self) -> None:
        if not self.output.exists():
            return
        try:
            for line_number, line in enumerate(
                self.output.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if not line.strip():
                    continue
                value = NiaInnerTransition.from_dict(json.loads(line))
                key = (value.source, value.source_id, value.step)
                if key in self._seen:
                    raise ValueError("duplicate existing transition boundary")
                self._seen.add(key)
                self._transitions.append(value)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError(
                f"existing inner transition dataset is invalid at {self.output}: {error}"
            ) from error

    def _load_pending(self) -> None:
        assert self.pending_output is not None
        if not self.pending_output.exists():
            return
        try:
            raw = json.loads(self.pending_output.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or raw.get("schema") != PENDING_SCHEMA:
                raise ValueError("pending artifact schema is unsupported")
            rows = raw.get("pending")
            if not isinstance(rows, list):
                raise ValueError("pending artifact rows must be an array")
            for item in rows:
                pending = NiaPendingInnerTransition.from_dict(item)
                if pending.key in self._pending:
                    raise ValueError("duplicate pending boundary")
                self._pending[pending.key] = pending
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError(
                f"existing pending artifact is invalid at {self.pending_output}: {error}"
            ) from error

    def _write_pending(self) -> None:
        assert self.pending_output is not None
        if not self._pending:
            if self.pending_output.exists():
                self.pending_output.unlink()
            return
        self.pending_output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": PENDING_SCHEMA,
            "pending": [
                self._pending[key].to_dict() for key in sorted(self._pending)
            ],
        }
        temporary = self.pending_output.with_suffix(
            self.pending_output.suffix + ".tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.pending_output)

    @property
    def transitions(self) -> tuple[NiaInnerTransition, ...]:
        return tuple(self._transitions)

    @property
    def rejections(self) -> tuple[NiaInnerTransitionRejection, ...]:
        return tuple(self._rejections)

    def collection(self) -> NiaInnerTransitionCollection:
        return NiaInnerTransitionCollection(
            tuple(self._transitions), tuple(self._rejections)
        )

    @property
    def pending(self) -> tuple[NiaPendingInnerTransition, ...]:
        return tuple(self._pending[key] for key in sorted(self._pending))

    @property
    def pending_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._pending))

    def _candidate_for_record(
        self,
        record: Plan2NativeUnattendedStepRecord,
    ) -> NiaCompleteLegalCandidates | None:
        legal = self.candidate_provider(record)
        if legal is None:
            return None
        if not isinstance(legal, NiaCompleteLegalCandidates):
            raise TypeError(
                "candidate provider must return NiaCompleteLegalCandidates or None"
            )
        if not legal.complete or not legal.candidates:
            return None
        if record.action is None:
            return None
        action_ids = {_action_id(value) for value in legal.candidates}
        if _action_id(record.action) not in action_ids:
            return None
        return legal

    def _record_pending_rejection(
        self,
        before: AuditionLocalSaveStateEvidence | None,
        step: int,
        reason: str,
    ) -> None:
        source_id = self._source_id_for(before)
        self._rejections.append(
            _rejection(
                source_id=source_id,
                step=step if isinstance(step, int) else 0,
                reasons=(reason,),
            )
        )

    def _stage_common(
        self,
        record: Plan2NativeUnattendedStepRecord,
        *,
        proof: object,
        kind: str,
        retained: AuditionLocalSaveStateEvidence | None = None,
    ) -> str | None:
        if not isinstance(record, Plan2NativeUnattendedStepRecord):
            raise TypeError("record must be Plan2NativeUnattendedStepRecord")
        before = record.evidence_before
        action = record.action
        if (
            not isinstance(before, AuditionLocalSaveStateEvidence)
            or not before.state.is_native_actionable_settled
        ):
            self._record_pending_rejection(
                before, getattr(record, "step_index", 0), "pending-before-not-settled"
            )
            return None
        if action is None or record.decision is None:
            self._record_pending_rejection(
                before, record.step_index, "pending-action-missing"
            )
            return None
        if record.decision.best_action != action:
            self._record_pending_rejection(
                before, record.step_index, "pending-action-not-decision"
            )
            return None
        if kind == "submitted-unsettled":
            if (
                record.execution is None
                or not record.execution.accepted
                or record.execution.replan
            ):
                self._record_pending_rejection(
                    before, record.step_index, "pending-action-not-submitted"
                )
                return None
            actual = record.actual_next_evidence
            if actual is not None and _settled_or_terminal(actual):
                self._record_pending_rejection(
                    before, record.step_index, "pending-after-already-settled"
                )
                return None
        elif kind == "play-retained":
            if not isinstance(action, Plan2NativeAction) or action.kind != "play":
                self._record_pending_rejection(
                    before, record.step_index, "pending-retained-action-invalid"
                )
                return None
            if not isinstance(retained, AuditionLocalSaveStateEvidence):
                raise TypeError("retained evidence must be typed")
            runtime = retained.state.root_runtime
            playing = retained.state.playing_card
            if (
                not _same_stage_identity(before, retained)
                or retained.state.is_native_actionable_settled
                or runtime is None
                or runtime.is_exam_end_complete
                or runtime.command_list_is_empty
                or playing is None
                or playing.guid != action.card_guid
            ):
                self._record_pending_rejection(
                    before, record.step_index, "pending-retained-proof-invalid"
                )
                return None
        else:
            raise ValueError("unsupported pending transition kind")

        legal = self._candidate_for_record(record)
        if legal is None:
            self._record_pending_rejection(
                before, record.step_index, "pending-candidates-incomplete"
            )
            return None
        try:
            stage_proof = _proof_payload(proof, "stage proof")
        except (TypeError, ValueError) as error:
            self._record_pending_rejection(
                before,
                record.step_index,
                f"pending-proof-invalid:{type(error).__name__}",
            )
            return None
        stage_proof.setdefault("candidate_authority", legal.authority)

        source_id = self._source_id_for(before)
        try:
            decision_receipt = build_plan2_learning_decision_receipt(
                record,
                before,
                source_id=source_id,
                action=_action_to_dict(action),
                legal_candidates=tuple(
                    _action_to_dict(value) for value in legal.candidates
                ),
                retained_proof=kind == "play-retained",
            )
        except (TypeError, ValueError) as error:
            self._record_pending_rejection(
                before,
                record.step_index,
                f"pending-decision-receipt-invalid:{type(error).__name__}",
            )
            return None
        stage_proof["decision_receipt"] = decision_receipt
        key = _pending_key(before, action, source_id=source_id)
        boundary = _boundary_key(before, source_id=source_id)
        if key in self._pending:
            self._record_pending_rejection(
                before, record.step_index, "pending-action-duplicate"
            )
            return None
        if any(
            _boundary_key(value.before, source_id=value.source_id) == boundary
            for value in self._pending.values()
        ):
            self._record_pending_rejection(
                before, record.step_index, "pending-action-overlap"
            )
            return None

        transition_step = _next_transition_step(
            self._transitions,
            tuple(self._pending.values()),
            source_id,
        )
        pending = NiaPendingInnerTransition(
            key=key,
            source_id=source_id,
            transition_step=transition_step,
            kind=kind,
            before=before,
            legal_candidates=tuple(legal.candidates),
            action=action,
            stage_proof=stage_proof,
        )
        self._pending[key] = pending
        try:
            self._write_pending()
        except (OSError, TypeError, ValueError) as error:
            self._pending.pop(key, None)
            self._record_pending_rejection(
                before,
                record.step_index,
                f"pending-persist-failed:{type(error).__name__}",
            )
            return None
        return key

    def stage_pending(
        self,
        record: Plan2NativeUnattendedStepRecord,
        proof: object,
    ) -> str | None:
        """Persist one submitted action whose next save is not settled."""

        return self._stage_common(
            record,
            proof=proof,
            kind="submitted-unsettled",
        )

    def stage_retained(
        self,
        record: Plan2NativeUnattendedStepRecord,
        retained: AuditionLocalSaveStateEvidence,
    ) -> str | None:
        """Persist one PLAY proven by a retained ``playingCard`` queue."""

        proof = {
            "kind": "play-retained-playing-card",
            "retained_evidence_digest": retained.digest(),
            "playing_card_guid": (
                None
                if retained.state.playing_card is None
                else retained.state.playing_card.guid
            ),
        }
        return self._stage_common(
            record,
            proof=proof,
            kind="play-retained",
            retained=retained,
        )

    def resolve_pending(
        self,
        observed_settled_after: AuditionLocalSaveStateEvidence,
        proof: object,
    ) -> bool:
        """Promote one pending boundary using only observed settled state."""

        if not isinstance(observed_settled_after, AuditionLocalSaveStateEvidence):
            raise TypeError("observed_settled_after must be typed evidence")
        try:
            payload = _proof_payload(proof, "resolution proof")
        except (TypeError, ValueError) as error:
            self._record_pending_rejection(
                observed_settled_after,
                0,
                f"pending-resolution-proof-invalid:{type(error).__name__}",
            )
            return False
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            self._record_pending_rejection(
                observed_settled_after,
                0,
                "pending-resolution-proof-unbound",
            )
            return False

        requested_key = payload.get("pending_key")
        if requested_key is not None and not isinstance(requested_key, str):
            self._record_pending_rejection(
                observed_settled_after,
                0,
                "pending-resolution-key-invalid",
            )
            return False
        if requested_key is None:
            action_id = payload.get("action_id")
            before_digest = payload.get(
                "before_digest", payload.get("evidence_before_digest")
            )
            after_digest = payload.get(
                "after_digest", payload.get("evidence_after_digest")
            )
            expected_source_id = self._source_id_for(observed_settled_after)
            candidates: list[NiaPendingInnerTransition] = []
            for candidate in self._pending.values():
                if candidate.source_id != expected_source_id:
                    continue
                if (
                    action_id is not None
                    and action_id != _action_id(candidate.action)
                ):
                    continue
                if (
                    before_digest is not None
                    and before_digest != candidate.before.digest()
                ):
                    continue
                if (
                    after_digest is not None
                    and after_digest != observed_settled_after.digest()
                ):
                    continue
                if (
                    candidate.before.digest() == observed_settled_after.digest()
                    or not _same_stage_identity(
                        candidate.before, observed_settled_after
                    )
                ):
                    continue
                immediate, _audit = _immediate_action_boundary(
                    candidate.before,
                    observed_settled_after,
                    candidate.action,
                )
                if immediate and _keyless_exact_action_progress(
                    observed_settled_after,
                    candidate.action,
                    _audit,
                ):
                    candidates.append(candidate)
            if len(candidates) != 1:
                self._record_pending_rejection(
                    observed_settled_after,
                    0,
                    "pending-resolution-key-ambiguous",
                )
                return False
            requested_key = candidates[0].key
        pending = self._pending.get(requested_key)
        if pending is None:
            self._record_pending_rejection(
                observed_settled_after,
                0,
                "pending-resolution-key-missing",
            )
            return False
        if pending.source_id != self._source_id_for(observed_settled_after):
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-source-mismatch",
            )
            return False

        action_id = payload.get("action_id")
        before_digest = payload.get("before_digest", payload.get("evidence_before_digest"))
        after_digest = payload.get("after_digest", payload.get("evidence_after_digest"))
        binding_fields = (requested_key, action_id, before_digest, after_digest)
        if not any(value is not None for value in binding_fields):
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-proof-unbound",
            )
            return False
        if requested_key != pending.key:
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-key-mismatch",
            )
            return False
        if action_id is not None and action_id != _action_id(pending.action):
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-action-mismatch",
            )
            return False
        if before_digest is not None and before_digest != pending.before.digest():
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-before-mismatch",
            )
            return False
        if after_digest is not None and after_digest != observed_settled_after.digest():
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-after-mismatch",
            )
            return False

        if (
            not _settled_or_terminal(observed_settled_after)
            or pending.before.digest() == observed_settled_after.digest()
            or not _same_stage_identity(pending.before, observed_settled_after)
        ):
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-after-not-observed-settled",
            )
            return False

        immediate, action_boundary_audit = _immediate_action_boundary(
            pending.before,
            observed_settled_after,
            pending.action,
        )
        if not immediate:
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-intervening-action-detected",
            )
            return False

        row = {
            "state_before": pending.before.state.to_dict(),
            "legal_candidates": [
                _action_to_dict(value) for value in pending.legal_candidates
            ],
            "action": _action_to_dict(pending.action),
            "state_after": observed_settled_after.state.to_dict(),
            "source": SOURCE_NATIVE_UNATTENDED_RECORD_SINK,
            "source_id": pending.source_id,
            "step": pending.transition_step,
            "reward": (
                observed_settled_after.state.score - pending.before.state.score
            ),
            "terminal": bool(
                observed_settled_after.state.root_runtime is not None
                and observed_settled_after.state.root_runtime.is_exam_end_complete
            ),
            "metadata": {
                "collector_schema": COLLECTOR_SCHEMA,
                "pending_schema": PENDING_SCHEMA,
                "pending_key": pending.key,
                "pending_kind": pending.kind,
                "candidate_authority": pending.stage_proof.get(
                    "candidate_authority", PLAN2_NATIVE_ENUMERATOR_AUTHORITY
                ),
                "run_id": pending.before.run_id,
                "step_context_id": pending.before.step_context_id,
                "step_context_digest": pending.before.step_context_digest,
                "session_transition_id": pending.before.session_transition_id,
                "source_before_sha256": pending.before.source_sha256,
                "source_after_sha256": observed_settled_after.source_sha256,
                "evidence_before_digest": pending.before.digest(),
                "evidence_after_digest": observed_settled_after.digest(),
                "score_before": pending.before.state.score,
                "score_after": observed_settled_after.state.score,
                "score_delta": (
                    observed_settled_after.state.score
                    - pending.before.state.score
                ),
                "observed_state_after": True,
                "predicted_state_used": False,
                "resolution_proof": payload,
                "action_boundary_audit": action_boundary_audit,
                "stage_proof": dict(pending.stage_proof),
            },
        }
        pending_receipt = pending.stage_proof.get("decision_receipt")
        if isinstance(pending_receipt, Mapping):
            merge_plan2_learning_decision_receipt_metadata(
                row["metadata"],
                pending_receipt,
            )
        if pending.source_id != pending.before.run_id:
            row["metadata"].update(
                {
                    "evidence_run_id": pending.before.run_id,
                    "run_binding_id": pending.source_id,
                }
            )
        transition, rejection = try_build_nia_inner_transition(
            row,
            source=SOURCE_NATIVE_UNATTENDED_RECORD_SINK,
            source_id=pending.source_id,
            step=pending.transition_step,
        )
        if transition is None:
            assert rejection is not None
            self._rejections.append(rejection)
            return False
        key = (transition.source, transition.source_id, transition.step)
        existing = next(
            (value for value in self._transitions if (value.source, value.source_id, value.step) == key),
            None,
        )
        if existing is not None:
            if existing == transition:
                self._pending.pop(pending.key, None)
                self._write_pending()
                return True
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                "pending-resolution-final-duplicate-conflict",
            )
            return False

        self._transitions.append(transition)
        self._seen.add(key)
        try:
            write_nia_inner_transition_export(
                self.output,
                self.collection(),
                allow_empty=False,
            )
        except (OSError, TypeError, ValueError) as error:
            self._transitions.pop()
            self._seen.remove(key)
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                f"pending-resolution-persist-failed:{type(error).__name__}",
            )
            return False
        self._pending.pop(pending.key, None)
        try:
            self._write_pending()
        except (OSError, TypeError, ValueError) as error:
            # Keep the final row and retain a retryable pending artifact if
            # cleanup itself failed; the next identical proof is idempotent.
            self._pending[pending.key] = pending
            self._record_pending_rejection(
                pending.before,
                pending.transition_step,
                f"pending-clear-failed:{type(error).__name__}",
            )
            return False
        return True

    def __call__(self, record: Plan2NativeUnattendedStepRecord) -> None:
        """Observe one loop record; incomplete records are never persisted."""

        if isinstance(record, Plan2NativeUnattendedStepRecord):
            before = record.evidence_before
            if (
                isinstance(before, AuditionLocalSaveStateEvidence)
                and self._pending
                and _settled_or_terminal(before)
            ):
                self.resolve_pending(
                    before,
                    {
                        "kind": "next-record-settled-boundary",
                        "after_digest": before.digest(),
                    },
                )
        # Terminal/failure records are part of the same loop journal but do
        # not represent an accepted physical action.  Do not invoke a
        # potentially expensive Maa candidate reader for those records.
        if (
            not isinstance(record, Plan2NativeUnattendedStepRecord)
            or record.action is None
            or record.execution is None
            or not record.execution.accepted
            or record.execution.replan
        ):
            transition, rejection = try_collect_nia_inner_transition(
                record,
                None,
                source_id_override=self.source_id_override,
                run_binding_id=self.run_binding_id,
            )
            if transition is None:
                assert rejection is not None
                self._rejections.append(rejection)
            return

        try:
            legal = self.candidate_provider(record)
        except Exception as error:
            before = record.evidence_before if isinstance(record, Plan2NativeUnattendedStepRecord) else None
            source_id = (
                before.run_id
                if isinstance(before, AuditionLocalSaveStateEvidence)
                else SOURCE_UNKNOWN
            )
            rejection = _rejection(
                source_id=source_id,
                step=getattr(record, "step_index", 0),
                reasons=(f"candidate-provider-failed:{type(error).__name__}",),
            )
            self._rejections.append(rejection)
            return

        transition, rejection = try_collect_nia_inner_transition(
            record,
            legal,
            source_id_override=self.source_id_override,
            run_binding_id=self.run_binding_id,
        )
        if transition is None:
            assert rejection is not None
            self._rejections.append(rejection)
            return
        key = (transition.source, transition.source_id, transition.step)
        if key in self._seen:
            self._rejections.append(
                _rejection(
                    source_id=transition.source_id,
                    step=transition.step,
                    reasons=("duplicate-transition-boundary",),
                )
            )
            return
        # Persist only after every strict check has passed.  A filesystem
        # failure remains a telemetry rejection and never changes live control.
        self._transitions.append(transition)
        self._seen.add(key)
        try:
            write_nia_inner_transition_export(
                self.output,
                self.collection(),
                allow_empty=False,
            )
        except (OSError, TypeError, ValueError) as error:
            self._transitions.pop()
            self._seen.remove(key)
            self._rejections.append(
                _rejection(
                    source_id=transition.source_id,
                    step=transition.step,
                    reasons=(f"transition-persist-failed:{type(error).__name__}",),
                )
            )


__all__ = [
    "COLLECTOR_SCHEMA",
    "DECISION_RECEIPT_SCHEMA",
    "LegalCandidateProvider",
    "MAA_BASELINE_RUNTIME_BLOCKER",
    "NiaCompleteLegalCandidates",
    "NiaInnerTransitionCollector",
    "NiaPendingInnerTransition",
    "PENDING_SCHEMA",
    "PLAN2_NATIVE_ENUMERATOR_AUTHORITY",
    "SOURCE_NATIVE_UNATTENDED_RECORD_SINK",
    "build_plan2_learning_decision_receipt",
    "build_plan2_native_enumerator_candidate_provider",
    "merge_plan2_learning_decision_receipt_metadata",
    "try_collect_nia_inner_transition",
]
