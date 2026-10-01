"""Machine-checkable gate before leaderboard learning is allowed.

Legacy reports used ``completed`` as a lifecycle label.  That is useful for
operations, but it is not strong enough to prove a clean unattended run.  This
module deliberately requires a newly started run, the Maa completion exam
policy, unique settled ExamSave transactions, a Home terminal, and a terminal
strategy journal before the next learning phase can be unlocked.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Sequence

from .exam_execution_policy import ExamExecutionMode
from .nia_date_rollover import KIND as NIA_DATE_ROLLOVER_KIND
from .nia_date_rollover import SCHEMA as NIA_DATE_ROLLOVER_SCHEMA
from .nia_route_profile import nia_stage_positions


SCHEMA = "gkms.nia-completion-acceptance.v1"
PROFILE = "clean-maa-completion-baseline-v1"
REPORT_SCHEMA = "gkms.nia-live-unattended.v1"
_HOME_REASONS = frozenset({"produce-home-reached"})
_HOME_NODES = frozenset({"ProduceHomeFlag", "ProduceBackHome"})
_SUPPORTED_PRODUCE_IDS = frozenset({"produce-004", "produce-005"})
_COMPLETION_POLICY_ID = "maa-game-recommendation-completion-v1"
_COMPLETION_REASON = "maa-baseline-exam-finished"
DURABLE_EXAM_LEDGER_SCHEMA = "gkms.nia-durable-exam-ledger.v1"
_REQUIRED_NIA_EXAMS = 3
_REQUIRED_NIA_STAGE_IDS = frozenset(
    {"exam-step:16", "exam-step:17", "exam-step:18"}
)


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _rows(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


def _integer_at_least(value: object, minimum: int) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= minimum
    )


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


@lru_cache(maxsize=1)
def _master_plan_types() -> Mapping[str, str]:
    try:
        from .nia_idol_catalog import load_nia_idol_catalog

        catalog = load_nia_idol_catalog()
        return {entry.idol_card_id: entry.plan_type for entry in catalog.entries}
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}


def _master_plan_type(idol_card_id: object) -> str | None:
    if not isinstance(idol_card_id, str) or not idol_card_id:
        return None
    return _master_plan_types().get(idol_card_id)


def _valid_date_rollover_resume(
    value: Mapping[str, Any],
    *,
    active_run_id: object,
    request: Mapping[str, Any],
    controller: Mapping[str, Any],
) -> bool:
    """Validate the serialized strict resume envelope before learning."""

    if (
        value.get("schema") != NIA_DATE_ROLLOVER_SCHEMA
        or value.get("kind") != NIA_DATE_ROLLOVER_KIND
        or value.get("accepted") is not True
        or value.get("reason") != "date-rollover-evidence-accepted"
        or value.get("run_id") != active_run_id
        or value.get("mode") != request.get("produce_id")
        or value.get("idol_card_id") != request.get("idol_card_id")
        or value.get("detected") is not True
    ):
        return False
    if (
        value.get("week_before") is None
        or value.get("week_before") != value.get("week_after")
        or not _sha256(value.get("localsave_digest_before"))
        or value.get("localsave_digest_before")
        != value.get("localsave_digest_after")
    ):
        return False
    pid = controller.get("target_pid")
    hwnd = controller.get("target_hwnd")
    if value.get("controller_pid") != pid or value.get("controller_hwnd") != hwnd:
        return False
    evidence = _mapping(value.get("evidence")) or {}
    audit = _mapping(evidence.get("input_audit")) or {}
    return (
        evidence.get("schema") == NIA_DATE_ROLLOVER_SCHEMA
        and evidence.get("kind") == NIA_DATE_ROLLOVER_KIND
        and evidence.get("interruption_reason") == "date-update"
        and evidence.get("explicit_date_update") is True
        and audit.get("source") == "maa-input-ledger"
        and audit.get("controller_input_delta") == 0
        and audit.get("game_input_delta") == 0
        and evidence.get("week_before") == value.get("week_before")
        and evidence.get("week_after") == value.get("week_after")
        and evidence.get("localsave_digest_before")
        == value.get("localsave_digest_before")
        and evidence.get("localsave_digest_after")
        == value.get("localsave_digest_after")
        and evidence.get("controller_pid_before") == pid
        and evidence.get("controller_pid_after") == pid
        and evidence.get("controller_hwnd_before") == hwnd
        and evidence.get("controller_hwnd_after") == hwnd
    )


@dataclass(frozen=True, slots=True)
class NiaCompletionCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class NiaCompletionAcceptance:
    checks: tuple[NiaCompletionCheck, ...]
    exam_transactions: int
    unique_exam_transactions: int
    schema: str = SCHEMA
    profile: str = PROFILE

    @property
    def accepted(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "profile": self.profile,
            "accepted": self.accepted,
            "failed_checks": list(self.failed_checks),
            "exam_transactions": self.exam_transactions,
            "unique_exam_transactions": self.unique_exam_transactions,
            "checks": [check.to_dict() for check in self.checks],
        }


def evaluate_nia_completion_payload(
    payload: Mapping[str, Any],
) -> NiaCompletionAcceptance:
    """Evaluate one serialized unattended report without changing runtime."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    request = _mapping(payload.get("request")) or {}
    try:
        required_stage_positions = dict(
            nia_stage_positions(str(request.get("produce_id", "")))
        )
    except ValueError:
        required_stage_positions = {}
    controller = _mapping(payload.get("controller")) or {}
    preflight = _mapping(payload.get("preflight")) or {}
    result = _mapping(payload.get("result")) or {}
    journal = _mapping(payload.get("strategy_journal")) or {}
    bootstrap = _mapping(payload.get("bootstrap")) or {}
    bootstrap_identity = _mapping(bootstrap.get("run_identity")) or {}
    run_origin = _mapping(payload.get("run_origin")) or {}
    run_origin_evidence = _mapping(run_origin.get("evidence")) or {}
    raw_steps = result.get("steps")
    steps_well_formed = (
        isinstance(raw_steps, Sequence)
        and not isinstance(raw_steps, str | bytes)
        and all(isinstance(row, Mapping) for row in raw_steps)
    )
    steps = _rows(raw_steps)

    checks: list[NiaCompletionCheck] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(NiaCompletionCheck(name, bool(passed), detail))

    check(
        "report-contract",
        payload.get("schema") == REPORT_SCHEMA
        and request.get("produce_id") in _SUPPORTED_PRODUCE_IDS
        and isinstance(request.get("idol_card_id"), str)
        and bool(request.get("idol_card_id"))
        and "expected_run_id" in request,
        f"schema={payload.get('schema')!r}, mode={request.get('produce_id')!r}",
    )
    check(
        "well-formed-step-ledger",
        steps_well_formed,
        "every serialized result step must be a mapping",
    )

    stop_reason = result.get("stop_reason")
    check(
        "completed-lifecycle",
        payload.get("status") == "completed"
        and result.get("status") == "completed"
        and stop_reason == "final-performance-ended",
        f"report={payload.get('status')!r}, result={result.get('status')!r}, "
        f"reason={stop_reason!r}",
    )
    master_plan_type = _master_plan_type(request.get("idol_card_id"))
    check(
        "master-plan-identity",
        isinstance(master_plan_type, str)
        and result.get("plan_type") == master_plan_type,
        f"master={master_plan_type!r}, result={result.get('plan_type')!r}",
    )
    check(
        "authentic-production-runtime",
        payload.get("production_runtime") is True
        and payload.get("authentic_live_evidence_observed") is True
        and payload.get("authentic_full_run_completed") is True,
        "all authentic production runtime markers must be explicit",
    )
    active_run_id = payload.get("active_run_id")
    same_request_origin = (
        payload.get("run_started_by_request") is True
        and request.get("expected_run_id") is None
        and bootstrap.get("started") is True
    )
    durable_origin = (
        isinstance(active_run_id, str)
        and bool(active_run_id)
        and run_origin.get("run_id") == active_run_id
        and run_origin.get("produce_id") == request.get("produce_id")
        and run_origin.get("idol_card_id") == request.get("idol_card_id")
        and isinstance(run_origin.get("created_at"), str)
        and bool(run_origin.get("created_at"))
        and run_origin_evidence.get("source")
        == "initial-regular-production-bootstrap"
        and run_origin_evidence.get("route_week") == 1
        and _integer_at_least(run_origin_evidence.get("outer_log_count"), 1)
    )
    check(
        "clean-run-origin",
        same_request_origin or durable_origin,
        "same-request bootstrap or durable run manifest must prove a clean entry",
    )
    binding_identity = (
        bootstrap_identity
        if bootstrap_identity.get("run_id") == active_run_id
        else run_origin
    )
    check(
        "bound-run-identity",
        isinstance(active_run_id, str)
        and bool(active_run_id)
        and journal.get("run_id") == active_run_id
        and binding_identity.get("run_id") == active_run_id
        and binding_identity.get("produce_id") == request.get("produce_id")
        and binding_identity.get("idol_card_id")
        == request.get("idol_card_id"),
        f"active={active_run_id!r}, journal={journal.get('run_id')!r}, "
        f"binding={binding_identity.get('run_id')!r}",
    )
    check(
        "maa-background-controller",
        _integer_at_least(controller.get("controller_version"), 24)
        and controller.get("background_control") is True
        and _integer_at_least(controller.get("target_pid"), 1)
        and _integer_at_least(controller.get("target_hwnd"), 1)
        and controller.get("foreground_required") is False
        and controller.get("target_title") == "gakumas"
        and controller.get("target_class") == "UnityWndClass"
        and payload.get("input_backend") == "MaaFramework-Win32-only",
        "controller v24+, bound HWND/PID, and Maa-only input are required",
    )
    check(
        "production-preflight",
        preflight.get("complete") is True
        and _integer_at_least(preflight.get("entry_branches_owned"), 15)
        and _integer_at_least(preflight.get("reachable_nodes"), 90)
        and preflight.get("state_authority") == "ProduceLocalSave+ExamSave"
        and preflight.get("card_identity_authority")
        == "ExamSave-ordered-hand-slot"
        and preflight.get("generic_nia_card_art_or_text_fallback") is False,
        "Maa graph and LocalSave/ExamSave authority must be complete",
    )
    check(
        "no-runtime-error",
        "error" in payload
        and payload.get("error") is None
        and "error" in journal
        and journal.get("error") is None,
        f"report={payload.get('error')!r}, journal={journal.get('error')!r}",
    )
    check(
        "successful-auditions",
        payload.get("early_end_after_audition_failure") is False,
        "failed-audition ProduceEnd is not a successful cultivation",
    )

    last_surface = _mapping(result.get("last_surface")) or {}
    check(
        "localsave-ended",
        last_surface.get("page") == "completed",
        f"last_surface={last_surface.get('page')!r}",
    )

    home_proofs = []
    for step in steps:
        if step.get("page") != "post-live":
            continue
        outcome = _mapping(step.get("outcome")) or {}
        maa = _mapping(outcome.get("maa")) or {}
        visited = tuple(
            name
            for name in maa.get("visited_nodes", ())
            if isinstance(name, str)
        ) if isinstance(maa.get("visited_nodes"), Sequence) else ()
        if (
            outcome.get("accepted") is True
            and maa.get("completed") is True
            and maa.get("reason") in _HOME_REASONS
            and bool(_HOME_NODES.intersection(visited))
        ):
            home_proofs.append(step)
    check(
        "produce-home-terminal",
        len(home_proofs) == 1,
        f"home_terminal_proofs={len(home_proofs)}",
    )

    final_live_starts = []
    for step in steps:
        if (
            step.get("page") == "final-live-start"
            and step.get("action") == "start"
            and step.get("target") == "final-live"
        ):
            outcome = _mapping(step.get("outcome")) or {}
            if (
                outcome.get("accepted") is True
                and outcome.get("action") == "maa-click-1-start-live"
                and _integer_at_least(step.get("index"), 0)
            ):
                final_live_starts.append(step)
    final_live_before_home = bool(
        len(final_live_starts) == 1
        and len(home_proofs) == 1
        and _integer_at_least(home_proofs[0].get("index"), 0)
        and int(final_live_starts[0]["index"])
        < int(home_proofs[0]["index"])
    )
    check(
        "final-live-started-once",
        final_live_before_home,
        f"starts={len(final_live_starts)}, homes={len(home_proofs)}",
    )

    check(
        "terminal-strategy-journal",
        journal.get("terminal_written") is True
        and _integer_at_least(journal.get("records_written"), 1),
        f"terminal={journal.get('terminal_written')!r}, "
        f"records={journal.get('records_written')!r}",
    )
    checkpoint_recovered = journal.get("checkpoint_recovered") is True
    date_rollover_resume = _mapping(journal.get("date_rollover_resume")) or {}
    date_rollover_proof = (
        not checkpoint_recovered
        or _valid_date_rollover_resume(
            date_rollover_resume,
            active_run_id=active_run_id,
            request=request,
            controller=controller,
        )
    )
    check(
        "date-rollover-resume-proof",
        date_rollover_proof,
        (
            "not a checkpoint resume"
            if not checkpoint_recovered
            else "explicit date-update, unchanged LocalSave, and zero-input proof required"
        ),
    )
    check(
        "uninterrupted-strategy-journal",
        not checkpoint_recovered or date_rollover_proof,
        "completion-learning requires one uninterrupted journal or a strict "
        "date-rollover resume; "
        f"checkpoint_recovered={journal.get('checkpoint_recovered')!r}, "
        f"date_rollover={date_rollover_proof}",
    )
    check(
        "complete-outer-route-evidence",
        _integer_at_least(journal.get("observed_choices"), 20)
        and journal.get("observed_auditions") == _REQUIRED_NIA_EXAMS,
        f"choices={journal.get('observed_choices')!r}, "
        f"auditions={journal.get('observed_auditions')!r}",
    )

    exam_proofs: list[tuple[tuple[str, str, str], str, str]] = []
    result_exam_identities: list[tuple[str, str, str]] = []
    ledger_exam_identities: list[tuple[str, str, str]] = []
    exam_step_indices: list[int] = []
    ledger_stage_ids: list[str] = []
    invalid_exam_rows = 0
    result_plan_type = result.get("plan_type")

    def completion_policy_is_valid(policy: Mapping[str, Any]) -> bool:
        return (
            policy.get("mode")
            == ExamExecutionMode.MAA_COMPLETION_BASELINE.value
            and policy.get("policy_id") == _COMPLETION_POLICY_ID
            and policy.get("simulator_required") is False
            and policy.get("game_recommendation_allowed") is True
            and policy.get("mid_input_fallback_allowed") is False
        )

    def exam_identity(
        identity: Mapping[str, Any],
    ) -> tuple[tuple[str, str, str], str, str] | None:
        semantic_values = (
            identity.get("run_binding_id"),
            identity.get("step_context_id"),
            identity.get("session_transition_id"),
        )
        exam_source_run_id = identity.get("exam_source_run_id")
        source_sha256 = identity.get("source_sha256")
        if not (
            all(
                isinstance(value, str) and bool(value)
                for value in semantic_values
            )
            and semantic_values[0] == active_run_id
            and isinstance(exam_source_run_id, str)
            and bool(exam_source_run_id)
            and _sha256(source_sha256)
        ):
            return None
        return (
            semantic_values,  # type: ignore[arg-type]
            exam_source_run_id,
            source_sha256,  # type: ignore[arg-type]
        )

    for step in steps:
        if step.get("page") != "exam" or step.get("action") != "dispatch":
            continue
        outcome = _mapping(step.get("outcome")) or {}
        policy = _mapping(outcome.get("execution_policy")) or {}
        identity = _mapping(outcome.get("exam_save_identity")) or {}
        step_index = step.get("index")
        proof = exam_identity(identity)
        valid_row = (
            isinstance(result_plan_type, str)
            and bool(result_plan_type)
            and step.get("target") == result_plan_type
            and _integer_at_least(step_index, 0)
            and outcome.get("accepted") is True
            and outcome.get("terminal") is True
            and outcome.get("reason") == _COMPLETION_REASON
            and outcome.get("simulator_required") is False
            and outcome.get("input_submitted") is True
            and outcome.get("policy")
            == "game-recommendation-or-available-card"
            and outcome.get("exam_save_authority")
            == "settled-native-exam-save"
            and completion_policy_is_valid(policy)
            and proof is not None
        )
        if not valid_row:
            invalid_exam_rows += 1
            continue
        assert proof is not None
        exam_proofs.append(proof)
        result_exam_identities.append(proof[0])
        exam_step_indices.append(step_index)  # type: ignore[arg-type]

    # A runner process can be safely restarted between auditions.  The current
    # result then contains only its own exam dispatches, while the run-bound
    # telemetry checkpoint retains all earlier settled transactions.  Accept
    # that durable ledger only when its full run/mode/idol/plan envelope and
    # every stage's Master-backed identity agree with this completed report.
    durable_ledger_present = "audition_ledger" in journal
    durable_ledger = _mapping(journal.get("audition_ledger")) or {}
    raw_ledger_rows = durable_ledger.get("rows")
    ledger_rows_well_formed = (
        isinstance(raw_ledger_rows, Sequence)
        and not isinstance(raw_ledger_rows, str | bytes)
        and all(isinstance(row, Mapping) for row in raw_ledger_rows)
    )
    ledger_envelope_valid = (
        durable_ledger_present
        and durable_ledger.get("schema") == DURABLE_EXAM_LEDGER_SCHEMA
        and durable_ledger.get("run_id") == active_run_id
        and durable_ledger.get("mode") == request.get("produce_id")
        and durable_ledger.get("idol") == request.get("idol_card_id")
        and durable_ledger.get("archetype") == result_plan_type
        and ledger_rows_well_formed
    )
    if durable_ledger_present and not ledger_envelope_valid:
        invalid_exam_rows += 1
    elif ledger_envelope_valid:
        for row in _rows(raw_ledger_rows):
            policy = _mapping(row.get("execution_policy")) or {}
            identity = _mapping(row.get("exam_save_identity")) or {}
            proof = exam_identity(identity)
            stage_id = None if proof is None else proof[0][1]
            expected_position = required_stage_positions.get(stage_id or "")
            score = row.get("score")
            compact_steps = row.get("steps")
            valid_row = (
                proof is not None
                and expected_position is not None
                and row.get("week") == expected_position[0]
                and row.get("phase") == expected_position[1]
                and row.get("terminal") is True
                and row.get("reason") == _COMPLETION_REASON
                and (score is None or _integer_at_least(score, 0))
                and isinstance(compact_steps, Sequence)
                and not isinstance(compact_steps, str | bytes)
                and all(isinstance(value, Mapping) for value in compact_steps)
                and completion_policy_is_valid(policy)
            )
            if not valid_row:
                invalid_exam_rows += 1
                continue
            assert proof is not None and stage_id is not None
            exam_proofs.append(proof)
            ledger_exam_identities.append(proof[0])
            ledger_stage_ids.append(stage_id)

    request_uses_baseline = (
        request.get("exam_execution_mode")
        == ExamExecutionMode.MAA_COMPLETION_BASELINE.value
    )
    # The same transaction legitimately appears once in the resumed result and
    # once in the durable ledger.  Collapse only byte-identical proof tuples;
    # conflicting hashes for the same semantic identity remain duplicates.
    unique_proofs = set(exam_proofs)
    unique_identities = {proof[0] for proof in unique_proofs}
    observed_stage_ids = {identity[1] for identity in unique_identities}
    exam_source_run_ids = {proof[1] for proof in unique_proofs}
    source_hashes = {proof[2] for proof in unique_proofs}
    proof_variants_by_identity: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
    for identity, source_run_id, source_hash in unique_proofs:
        proof_variants_by_identity.setdefault(identity, set()).add(
            (source_run_id, source_hash)
        )
    conflicting_cross_source_proof = any(
        len(values) != 1 for values in proof_variants_by_identity.values()
    )
    duplicate_result_identity = len(result_exam_identities) != len(
        set(result_exam_identities)
    )
    duplicate_ledger_identity = len(ledger_exam_identities) != len(
        set(ledger_exam_identities)
    )
    ledger_order_valid = (
        not ledger_envelope_valid
        or ledger_stage_ids
        == sorted(
            ledger_stage_ids,
            key=lambda value: required_stage_positions[value],
        )
    )
    exam_policy_rows = len(unique_proofs)
    result_step_order_valid = (
        len(set(exam_step_indices)) == len(exam_step_indices)
        and exam_step_indices == sorted(exam_step_indices)
        and (ledger_envelope_valid or len(exam_step_indices) == exam_policy_rows)
    )
    check(
        "maa-completion-exams",
        request_uses_baseline
        and exam_policy_rows == _REQUIRED_NIA_EXAMS
        and invalid_exam_rows == 0
        and observed_stage_ids == _REQUIRED_NIA_STAGE_IDS
        and len(exam_source_run_ids) == 1
        and all(
            source.startswith("initial-regular-plan2:")
            for source in exam_source_run_ids
        ),
        f"baseline={request_uses_baseline}, valid={exam_policy_rows}, "
        f"required={_REQUIRED_NIA_EXAMS}, invalid={invalid_exam_rows}, "
        f"stages={sorted(observed_stage_ids)}, "
        f"source_runs={len(exam_source_run_ids)}",
    )
    check(
        "no-duplicate-terminal-exam-input",
        exam_policy_rows == _REQUIRED_NIA_EXAMS
        and len(unique_identities) == exam_policy_rows
        and len(source_hashes) == exam_policy_rows
        and not conflicting_cross_source_proof
        and not duplicate_result_identity
        and not duplicate_ledger_identity
        and result_step_order_valid
        and ledger_order_valid,
        f"transactions={exam_policy_rows}, unique={len(unique_identities)}",
    )

    return NiaCompletionAcceptance(
        checks=tuple(checks),
        exam_transactions=exam_policy_rows,
        unique_exam_transactions=len(unique_identities),
    )


__all__ = [
    "DURABLE_EXAM_LEDGER_SCHEMA",
    "NiaCompletionAcceptance",
    "NiaCompletionCheck",
    "evaluate_nia_completion_payload",
]
