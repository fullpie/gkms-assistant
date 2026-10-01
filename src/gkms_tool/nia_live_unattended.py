"""Headless, restart-safe composition for one live N.I.A. Produce run.

This module adds no recognition or input policy.  It composes the existing
allow-listed Maa controller, LocalSave reader, and production autopilot into a
CLI that can be left running without the Tk panel.  A pre-existing Produce is
resumed only when the persisted run identity matches the requested mode and
idol; a run started by this process is allowed to create that identity on its
first production surface.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Sequence

from .exam_execution_policy import (
    ExamExecutionMode,
    resolve_exam_execution_policy,
)
from .initial_regular_autopilot import (
    INNER_IMITATION_RUNTIME_ENABLED_DEFAULT,
    InnerImitationExamRunner,
    STATUS_COMPLETED,
    InitialRegularAutopilotResult,
    InitialRegularAutopilotStep,
    _incomplete_outer_observer_anchor,
    run_live_initial_regular_autopilot,
)
from .nia_idol_catalog import load_nia_idol_catalog
from .nia_date_rollover import (
    KIND as NIA_DATE_ROLLOVER_KIND,
    REASON as NIA_DATE_ROLLOVER_REASON,
    SCHEMA as NIA_DATE_ROLLOVER_SCHEMA,
    evaluate_date_rollover_resume,
    localsave_binding_digest,
)
from .nia_route_profile import nia_final_week, nia_phase_for_week, nia_stage_positions
from .nia_strategy_journal import (
    DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
    NIA_STRATEGY_JOURNAL_SCHEMA,
    NIA_STRATEGY_JOURNAL_SCHEMA_VERSION,
    NiaFinalRecord,
    NiaOuterTransactionReceipt,
    NiaStrategyState,
    append_nia_strategy_journal,
    nia_strategy_journal_path,
    read_nia_strategy_journal,
)
from .produce_outer_local_save import (
    DEFAULT_PC_GAME_ROOT,
    ProduceOuterLocalSaveSnapshot,
    read_current_produce_outer_local_save,
)


SCHEMA = "gkms.nia-live-unattended.v1"
DEFAULT_OUTPUT_PATH = (
    Path(__file__).resolve().parents[2] / "var" / "nia_live" / "latest.json"
)
DEFAULT_INTENT_PATH = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "nia_live"
    / "bootstrap_intent.json"
)
MIN_CONTROLLER_VERSION = 45
# First-time accounts can show several voice/fast-forward/performance setup
# dialogs after Maa has already selected the exact idol and submitted Produce.
# Keep the one-crash recovery ticket long enough for those one-off screens.  It
# remains bound to the same controller PID, produce ID and idol-card ID, so
# this does not authorize a different process or selection.
BOOTSTRAP_INTENT_MAX_AGE_SECONDS = 3600.0
SUPPORTED_PRODUCE_IDS = frozenset({"produce-004", "produce-005"})
DEFAULT_IDOL_CARD_ID = "i_card-fktn-3-011"
NIA_LIVE_STRATEGY_VERSION = "nia-live-runtime-v1"
NIA_LIVE_TELEMETRY_CHECKPOINT_SCHEMA = (
    "gkms.nia-live-strategy-telemetry-checkpoint.v1"
)


@dataclass(frozen=True, slots=True)
class NiaLiveUnattendedRequest:
    produce_id: str = "produce-005"
    idol_card_id: str = DEFAULT_IDOL_CARD_ID
    game_root: Path = DEFAULT_PC_GAME_ROOT
    expected_run_id: str | None = None
    stage_number: int = 1
    output_path: Path = DEFAULT_OUTPUT_PATH
    intent_path: Path = DEFAULT_INTENT_PATH
    strategy_journal_root: Path = DEFAULT_NIA_STRATEGY_JOURNAL_ROOT
    exam_execution_mode: ExamExecutionMode = (
        ExamExecutionMode.MAA_COMPLETION_BASELINE
    )
    request_uac: bool = False
    use_ap_drink: bool = False
    # Explicit opt-in for the plan-neutral inner imitation bridge.  The
    # default remains the existing Maa completion baseline; no runner or
    # prior is loaded when this is false.
    inner_imitation_enabled: bool = INNER_IMITATION_RUNTIME_ENABLED_DEFAULT
    # Optional observer-owned evidence.  A normal resume leaves this unset;
    # checkpoint recovery then remains ineligible for learning.  This is not
    # inferred from a timestamp gap.
    date_rollover_evidence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.produce_id not in SUPPORTED_PRODUCE_IDS:
            raise ValueError("produce_id must be produce-004 or produce-005")
        if not isinstance(self.idol_card_id, str) or not self.idol_card_id.strip():
            raise ValueError("idol_card_id must be non-empty text")
        if self.expected_run_id is not None and (
            not isinstance(self.expected_run_id, str)
            or not self.expected_run_id.strip()
        ):
            raise ValueError("expected_run_id must be non-empty text or None")
        if type(self.stage_number) is not int or self.stage_number < 1:
            raise ValueError("stage_number must be a positive integer")
        if type(self.request_uac) is not bool:
            raise TypeError("request_uac must be bool")
        if type(self.use_ap_drink) is not bool:
            raise TypeError("use_ap_drink must be bool")
        if type(self.inner_imitation_enabled) is not bool:
            raise TypeError("inner_imitation_enabled must be bool")
        if self.date_rollover_evidence is not None and not isinstance(
            self.date_rollover_evidence, Mapping
        ):
            raise TypeError("date_rollover_evidence must be a mapping or None")
        exam_policy = resolve_exam_execution_policy(self.exam_execution_mode)
        object.__setattr__(self, "exam_execution_mode", exam_policy.mode)
        object.__setattr__(self, "game_root", Path(self.game_root).resolve())
        object.__setattr__(self, "output_path", Path(self.output_path).resolve())
        object.__setattr__(self, "intent_path", Path(self.intent_path).resolve())
        object.__setattr__(
            self,
            "strategy_journal_root",
            Path(self.strategy_journal_root).resolve(),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["game_root"] = str(self.game_root)
        value["output_path"] = str(self.output_path)
        value["intent_path"] = str(self.intent_path)
        value["strategy_journal_root"] = str(self.strategy_journal_root)
        value["exam_execution_mode"] = self.exam_execution_mode.value
        value["date_rollover_evidence"] = (
            None
            if self.date_rollover_evidence is None
            else dict(self.date_rollover_evidence)
        )
        return value


@dataclass(frozen=True, slots=True)
class NiaLiveUnattendedReport:
    request: NiaLiveUnattendedRequest
    status: str
    started_at: str
    finished_at: str | None
    controller: Mapping[str, Any] | None
    bootstrap: Mapping[str, Any] | None
    progress: Mapping[str, Any] | None
    result: InitialRegularAutopilotResult | None
    active_run_id: str | None
    run_started_by_request: bool = False
    preflight: Mapping[str, Any] | None = None
    strategy_journal: Mapping[str, Any] | None = None
    full_cultivation_shadow: Mapping[str, Any] | None = None
    error: str | None = None
    schema: str = SCHEMA

    @property
    def completed(self) -> bool:
        return self.result is not None and self.result.status == STATUS_COMPLETED

    @property
    def early_end_after_audition_failure(self) -> bool:
        """Whether the run ended through N.I.A.'s failed-audition exit path."""

        if self.result is None:
            return False
        return any(
            step.action == "audition-failure-end"
            or step.target == "maa-nia-audition-failure-produce-end"
            for step in self.result.steps
        )

    @property
    def successful_clear(self) -> bool:
        return self.completed and not self.early_end_after_audition_failure

    def to_dict(self) -> dict[str, Any]:
        run_origin = None
        if self.active_run_id is not None:
            try:
                from .run_identity import load_run

                run_origin = load_run(self.active_run_id).to_dict()
            except (FileNotFoundError, OSError, TypeError, ValueError):
                run_origin = None
        payload = {
            "schema": self.schema,
            "request": self.request.to_dict(),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "controller": None if self.controller is None else dict(self.controller),
            "bootstrap": None if self.bootstrap is None else dict(self.bootstrap),
            "progress": None if self.progress is None else dict(self.progress),
            "result": None if self.result is None else self.result.to_dict(),
            "active_run_id": self.active_run_id,
            "run_origin": run_origin,
            "run_started_by_request": self.run_started_by_request,
            "preflight": None if self.preflight is None else dict(self.preflight),
            "strategy_journal": (
                None
                if self.strategy_journal is None
                else dict(self.strategy_journal)
            ),
            "error": self.error,
            "production_runtime": True,
            "authentic_live_evidence_observed": bool(
                self.bootstrap is not None
                or self.progress is not None
                or self.result is not None
            ),
            # ``completed`` is a runner lifecycle fact: N.I.A. also reaches a
            # normal ProduceEnd screen after a failed audition.  Keep that
            # distinct from a successful cultivation clear.
            "authentic_run_ended": self.completed,
            "early_end_after_audition_failure": (
                self.early_end_after_audition_failure
            ),
            "authentic_full_run_completed": self.successful_clear,
            "input_backend": "MaaFramework-Win32-only",
        }
        from .nia_completion_acceptance import (
            evaluate_nia_completion_payload,
        )

        acceptance = evaluate_nia_completion_payload(payload)
        payload["completion_acceptance"] = acceptance.to_dict()
        payload["leaderboard_learning_unlocked"] = acceptance.accepted
        if self.full_cultivation_shadow is None:
            # Preserve the pre-joint-report payload shape for running/failed
            # reports and older consumers.  The additive field appears only
            # after the completed run-end join has actually run.
            payload.pop("full_cultivation_shadow", None)
        else:
            payload["full_cultivation_shadow"] = dict(self.full_cultivation_shadow)
        return payload


ControllerStatusReader = Callable[[], Mapping[str, Any]]
Bootstrapper = Callable[..., Mapping[str, Any]]
SnapshotReader = Callable[[str | Path], ProduceOuterLocalSaveSnapshot]
Runner = Callable[..., InitialRegularAutopilotResult]
ActiveRunReader = Callable[[], Any]
RunIdentityBootstrapper = Callable[..., Mapping[str, Any]]
BootstrapModalAdvancer = Callable[[Mapping[str, Any]], Mapping[str, Any]]
EndOnlyAuthorityReader = Callable[[NiaLiveUnattendedRequest], Mapping[str, Any]]
EndOnlyReconciler = Callable[[Mapping[str, Any]], Mapping[str, Any]]
DateRolloverResumer = Callable[[], object]
OuterObserverCursorFactory = Callable[[int], Any]
OuterObserverBarrierCall = Callable[..., Mapping[str, Any]]
OUTER_OBSERVER_NATIVE_BARRIER_TIMEOUT_MS = 1000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_outer_observer_cursor_factory(target_pid: int) -> Any:
    from .runtime_outer_observer_watcher import RuntimeOuterObserverAnchorCursor

    return RuntimeOuterObserverAnchorCursor(target_pid)


def _default_outer_observer_barrier_call(
    *,
    expected_generation: int,
    minimum_next: int,
) -> Mapping[str, Any]:
    from .controller_client import send_command

    native_timeout_ms = OUTER_OBSERVER_NATIVE_BARRIER_TIMEOUT_MS
    return send_command(
        "flush_runtime_outer_observer_once",
        timeout=native_timeout_ms / 1000.0 + 5.0,
        timeout_ms=native_timeout_ms,
        expected_generation=expected_generation,
        minimum_next=minimum_next,
    )


def _runner_accepts_outer_observer_anchor_provider(runner: Runner) -> bool:
    """Preserve fixed-signature injected runners while extending production."""

    if runner is run_live_initial_regular_autopilot:
        return True
    try:
        parameters = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return False
    explicit = parameters.get("outer_observer_anchor_provider")
    if explicit is not None and explicit.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _local_date_from_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _controller_pid(status: Mapping[str, Any]) -> int:
    value = status.get("target_pid")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("controller target_pid is unavailable")
    return value


def _write_bootstrap_intent(
    request: NiaLiveUnattendedRequest,
    controller: Mapping[str, Any],
) -> None:
    _atomic_json(
        request.intent_path,
        {
            "schema": SCHEMA + ".bootstrap-intent",
            "created_unix": time.time(),
            "target_pid": _controller_pid(controller),
            "produce_id": request.produce_id,
            "idol_card_id": request.idol_card_id,
            "use_ap_drink": request.use_ap_drink,
        },
    )


def _consume_matching_bootstrap_intent(
    request: NiaLiveUnattendedRequest,
    controller: Mapping[str, Any],
) -> bool:
    path = request.intent_path
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            return False
        created = raw.get("created_unix")
        if isinstance(created, bool) or not isinstance(created, int | float):
            return False
        age = time.time() - float(created)
        return (
            0.0 <= age <= BOOTSTRAP_INTENT_MAX_AGE_SECONDS
            and raw.get("schema") == SCHEMA + ".bootstrap-intent"
            and raw.get("target_pid") == _controller_pid(controller)
            and raw.get("produce_id") == request.produce_id
            and raw.get("idol_card_id") == request.idol_card_id
            and raw.get("use_ap_drink", False) is request.use_ap_drink
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def _remove_bootstrap_intent(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _controller_is_usable(status: Mapping[str, Any]) -> bool:
    try:
        version = int(status.get("controller_version", 0))
        hwnd = int(status.get("target_hwnd", 0))
        pid = int(status.get("target_pid", 0))
    except (TypeError, ValueError):
        return False
    return (
        version >= MIN_CONTROLLER_VERSION
        and bool(status.get("background_control"))
        and hwnd > 0
        and pid > 0
    )


def build_nia_live_preflight(status: Mapping[str, Any]) -> Mapping[str, Any]:
    """Audit the fixed Maa/live surface once, before any gameplay input."""

    if not _controller_is_usable(status):
        raise RuntimeError("Maa controller is not usable for N.I.A. preflight")
    from .nia_live_production_coverage import (
        audit_nia_live_production_coverage,
        audit_nia_live_reachable_graph,
    )

    entry = audit_nia_live_production_coverage()
    graph = audit_nia_live_reachable_graph()
    if not entry.complete:
        raise RuntimeError(
            "N.I.A. Maa entry ownership is incomplete: "
            f"missing={entry.missing_nodes!r}, stale={entry.stale_nodes!r}"
        )
    if not graph.complete:
        raise RuntimeError(
            "N.I.A. Maa reachable graph is incomplete: "
            f"unresolved={graph.unresolved_nodes!r}, "
            f"duplicate={graph.duplicate_nodes!r}"
        )
    return {
        "complete": True,
        "controller_version": int(status["controller_version"]),
        "target_pid": _controller_pid(status),
        "target_hwnd": int(status["target_hwnd"]),
        "background_control": True,
        "input_backend": "MaaFramework-Win32-only",
        "entry_branches_owned": len(entry.ownership),
        "reachable_nodes": len(graph.reachable_nodes),
        "state_authority": "ProduceLocalSave+ExamSave",
        "card_identity_authority": "ExamSave-ordered-hand-slot",
        "generic_nia_card_art_or_text_fallback": False,
    }


def _default_status_reader() -> Mapping[str, Any]:
    from .controller_client import send_command

    return dict(send_command("status"))


def _request_controller_uac() -> None:
    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "launch_elevated_supervisor.ps1"
    if not script.is_file():
        raise FileNotFoundError(script)
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=150.0,
        check=False,
        creationflags=creationflags,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "elevated Maa controller did not start"
            + ("" if not detail else f": {detail}")
        )


def _ensure_controller(
    status_reader: ControllerStatusReader,
    *,
    request_uac: bool,
    sleep: Callable[[float], None],
) -> Mapping[str, Any]:
    try:
        status = dict(status_reader())
    except Exception:
        status = {}
    if _controller_is_usable(status):
        return status
    if not request_uac:
        raise RuntimeError(
            f"Maa controller v{MIN_CONTROLLER_VERSION} is unavailable; "
            "approve UAC or pass --request-uac"
        )
    _request_controller_uac()
    deadline = time.monotonic() + 60.0
    last: Mapping[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = dict(status_reader())
        except Exception:
            last = {}
        if _controller_is_usable(last):
            return last
        sleep(0.2)
    raise RuntimeError(
        f"Maa controller v{MIN_CONTROLLER_VERSION} did not become ready after UAC"
    )


def _active_snapshot_or_none(
    reader: SnapshotReader,
    game_root: Path,
) -> ProduceOuterLocalSaveSnapshot | None:
    try:
        snapshot = reader(game_root)
    except FileNotFoundError:
        return None
    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot_reader returned an unsupported value")
    lifecycle = snapshot.lifecycle
    return snapshot if lifecycle is not None and lifecycle.is_in_progress else None


def _active_identity_tuple(active: Any) -> tuple[str, str] | None:
    if active is None:
        return None
    produce_id = getattr(active, "produce_id", None)
    idol_card_id = getattr(active, "idol_card_id", None)
    if not isinstance(produce_id, str) or not isinstance(idol_card_id, str):
        raise TypeError("active run identity is malformed")
    return produce_id, idol_card_id


def _active_run_id(active: Any) -> str | None:
    value = None if active is None else getattr(active, "run_id", None)
    return value if isinstance(value, str) and value else None


def _default_bootstrapper(
    *,
    produce_id: str,
    idol_card_id: str,
    use_ap_drink: bool = False,
) -> Mapping[str, Any]:
    from .controller_client import send_command

    if type(use_ap_drink) is not bool:
        raise TypeError("use_ap_drink must be bool")
    payload: dict[str, Any] = {
        "produce_id": produce_id,
        "idol_card_id": idol_card_id,
    }
    if use_ap_drink:
        payload["use_ap_drink"] = True
    return dict(send_command("start_nia_produce", timeout=150.0, **payload))


def _default_date_rollover_resumer() -> Mapping[str, Any]:
    """Re-enter the active Produce route after an accepted date update."""

    from .controller_client import send_command

    return dict(
        send_command(
            "resume_active_produce",
            timeout=45.0,
            timeout_seconds=30.0,
        )
    )


def _default_bootstrap_modal_advancer(
    controller: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Advance one Maa-recognized first-use setup dialog.

    Initial and N.I.A. share the same pre-LocalSave voice/fast-forward dialogs.
    Reuse the already tested Maa-only bridge instead of adding another visual
    policy or a fixed coordinate path here.
    """

    from .initial_live_unattended import (
        _default_bootstrap_modal_advancer as advance_initial_bootstrap_modal,
    )

    return dict(advance_initial_bootstrap_modal(controller))


def _default_active_run_reader() -> Any:
    from .run_identity import load_active_run

    return load_active_run()


def _default_run_identity_bootstrapper(
    *,
    idol_card_id: str,
    produce_id: str,
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> Mapping[str, Any]:
    from .live_source import ensure_initial_regular_active_run

    return dict(
        ensure_initial_regular_active_run(
            idol_card_id=idol_card_id,
            produce_id=produce_id,
            snapshot=snapshot,
        )
    )


def _default_end_only_authority_reader(
    request: NiaLiveUnattendedRequest,
) -> Mapping[str, Any]:
    """Load only durable evidence for an already-finished expected run.

    The current output is excluded because it is the report being written by
    this invocation.  Candidate reports are restricted to the same directory,
    schema and exact run identity, newest first; the semantic Final proof is
    validated separately before any Maa reconciliation is allowed.
    """

    run_id = request.expected_run_id
    if run_id is None:
        raise ValueError("end-only authority requires expected_run_id")
    journal_path = nia_strategy_journal_path(
        run_id,
        root=request.strategy_journal_root,
    )
    checkpoint_path = journal_path.with_suffix(".telemetry.json")
    checkpoint_bytes = checkpoint_path.read_bytes()
    checkpoint = json.loads(checkpoint_bytes.decode("utf-8"))
    if not isinstance(checkpoint, Mapping):
        raise ValueError("end-only telemetry checkpoint must be an object")

    reports: list[Mapping[str, Any]] = []
    candidates = sorted(
        (
            path
            for path in request.output_path.parent.glob("*.json")
            if path.resolve() != request.output_path
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for path in candidates:
        try:
            source = path.read_bytes()
            payload = json.loads(source.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        if payload.get("schema") != SCHEMA or payload.get("active_run_id") != run_id:
            continue
        reports.append(
            {
                "path": str(path.resolve()),
                "sha256": hashlib.sha256(source).hexdigest(),
                "report": dict(payload),
            }
        )
        result = payload.get("result")
        stop_reason = (
            result.get("stop_reason") if isinstance(result, Mapping) else None
        )
        if (
            payload.get("status") == "hard-stop"
            and isinstance(stop_reason, str)
            and stop_reason.startswith("post-live-execution-failed:")
        ):
            # Stop at the first newest report that could possibly satisfy the
            # semantic proof below.  Do not parse older, potentially very
            # large exam reports merely to rediscover the same run.
            break
        # Bound malformed/non-Final same-run history as well.
        if len(reports) >= 8:
            break
    return {
        "checkpoint_path": str(checkpoint_path.resolve()),
        "checkpoint_sha256": hashlib.sha256(checkpoint_bytes).hexdigest(),
        "checkpoint": dict(checkpoint),
        "reports": reports,
    }


def _positive_integer(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _nonnegative_integer(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _prove_end_only_final_authority(
    request: NiaLiveUnattendedRequest,
    *,
    archetype: str,
    raw: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Prove a cleared Final from checkpoint plus one exact prior report.

    This proof authorizes only completion reconciliation after LocalSave has
    disappeared.  It does not authorize Produce bootstrap, card input, or a
    different run.  A terminal exam alone is insufficient: the prior report
    must also prove the accepted Final-live start and the post-live-only stop.
    """

    run_id = request.expected_run_id
    if run_id is None:
        raise ValueError("end-only authority requires expected_run_id")
    checkpoint = raw.get("checkpoint")
    reports = raw.get("reports")
    if not isinstance(checkpoint, Mapping) or not isinstance(reports, Sequence):
        raise ValueError("end-only authority bundle is malformed")
    expected_checkpoint_identity = {
        "schema": NIA_LIVE_TELEMETRY_CHECKPOINT_SCHEMA,
        "run_id": run_id,
        "mode": request.produce_id,
        "idol": request.idol_card_id,
        "archetype": archetype,
    }
    if any(
        checkpoint.get(name) != expected
        for name, expected in expected_checkpoint_identity.items()
    ):
        raise ValueError("end-only checkpoint identity mismatch")
    final_week = nia_final_week(request.produce_id)
    if checkpoint.get("week") != final_week or checkpoint.get("turns_remaining") != 0:
        raise ValueError("end-only checkpoint has no completed Final horizon")
    final_score = _positive_integer(checkpoint.get("exam_score"))
    values = checkpoint.get("values")
    sources = checkpoint.get("sources")
    if final_score is None or not isinstance(values, Mapping) or not isinstance(
        sources, Mapping
    ):
        raise ValueError("end-only checkpoint state is incomplete")
    for name in _NiaLiveStrategyTelemetry._STATE_FIELDS:
        if _nonnegative_integer(values.get(name)) is None:
            raise ValueError(f"end-only checkpoint value missing: {name}")
        if not isinstance(sources.get(name), str) or not sources[name]:
            raise ValueError(f"end-only checkpoint source missing: {name}")
    max_stamina = _positive_integer(values.get("max_stamina"))
    stamina = _nonnegative_integer(values.get("stamina"))
    if max_stamina is None or stamina is None or stamina > max_stamina:
        raise ValueError("end-only checkpoint stamina is invalid")
    auditions = checkpoint.get("auditions")
    if not isinstance(auditions, list) or not auditions:
        raise ValueError("end-only checkpoint has no audition history")
    final_audition = auditions[-1]
    if not isinstance(final_audition, Mapping) or any(
        (
            final_audition.get("week") != final_week,
            final_audition.get("phase") != 3,
            final_audition.get("terminal") is not True,
            final_audition.get("reason") != "terminal",
            final_audition.get("score") != final_score,
        )
    ):
        raise ValueError("end-only checkpoint Final audition is not authoritative")
    final_steps = final_audition.get("steps")
    if not isinstance(final_steps, list) or not final_steps:
        raise ValueError("end-only checkpoint Final audition has no decisions")
    final_step = final_steps[-1]
    if (
        not isinstance(final_step, Mapping)
        or not isinstance(final_step.get("chosen"), str)
        or not final_step["chosen"]
        or final_step.get("actual_score") != final_score
    ):
        raise ValueError("end-only checkpoint Final decision is incomplete")

    qualifying: list[tuple[Mapping[str, Any], str | None, str | None]] = []
    for candidate in reports:
        if not isinstance(candidate, Mapping):
            continue
        report = candidate.get("report", candidate)
        if not isinstance(report, Mapping):
            continue
        report_request = report.get("request")
        result = report.get("result")
        progress = report.get("progress")
        if not all(
            isinstance(value, Mapping)
            for value in (report_request, result, progress)
        ):
            continue
        assert isinstance(report_request, Mapping)
        assert isinstance(result, Mapping)
        assert isinstance(progress, Mapping)
        stop_reason = result.get("stop_reason")
        if not (
            report.get("schema") == SCHEMA
            and report.get("status") == "hard-stop"
            and report.get("error") is None
            and report.get("active_run_id") == run_id
            and report.get("early_end_after_audition_failure") is False
            and report_request.get("produce_id") == request.produce_id
            and report_request.get("idol_card_id") == request.idol_card_id
            and report_request.get("expected_run_id") == run_id
            and result.get("status") == "hard-stop"
            and result.get("plan_type") == archetype
            and isinstance(stop_reason, str)
            and stop_reason.startswith("post-live-execution-failed:")
            and progress.get("stop_reason") == stop_reason
            and progress.get("current_page")
            in {"final-live-playing", "post-live"}
        ):
            continue
        steps = result.get("steps")
        if not isinstance(steps, list):
            continue
        final_exam_indexes: list[int] = []
        live_start_indexes: list[int] = []
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            index = _nonnegative_integer(step.get("index"))
            outcome = step.get("outcome")
            if index is None or not isinstance(outcome, Mapping):
                continue
            if (
                step.get("page") == "exam"
                and step.get("action") == "dispatch"
                and step.get("target") == archetype
                and outcome.get("accepted") is True
                and outcome.get("terminal") is True
                and outcome.get("reason") == "terminal"
            ):
                final_exam_indexes.append(index)
            if (
                step.get("page") == "final-live-start"
                and step.get("action") == "start"
                and step.get("target") == "final-live"
                and outcome.get("accepted") is True
                and outcome.get("action") == "maa-click-1-start-live"
            ):
                live_start_indexes.append(index)
        if (
            len(final_exam_indexes) != 1
            or len(live_start_indexes) != 1
            or live_start_indexes[0] <= final_exam_indexes[0]
        ):
            continue
        qualifying.append(
            (
                report,
                (
                    candidate.get("path")
                    if isinstance(candidate.get("path"), str)
                    else None
                ),
                (
                    candidate.get("sha256")
                    if isinstance(candidate.get("sha256"), str)
                    else None
                ),
            )
        )
    if not qualifying:
        raise ValueError("end-only recovery has no authoritative Final report")
    # The default reader is newest-first.  Multiple stopped post-live retries
    # for the same exact run are not an identity ambiguity; use the newest one
    # that independently carries the complete Final/live proof.
    report, report_path, report_sha256 = qualifying[0]
    result = report["result"]
    assert isinstance(result, Mapping)
    return {
        "kind": "expected-run-cleared-final-end-only",
        "run_id": run_id,
        "mode": request.produce_id,
        "idol": request.idol_card_id,
        "archetype": archetype,
        "week": final_week,
        "final_score": final_score,
        "checkpoint_path": raw.get("checkpoint_path"),
        "checkpoint_sha256": raw.get("checkpoint_sha256"),
        "report_path": report_path,
        "report_sha256": report_sha256,
        "prior_stop_reason": result.get("stop_reason"),
    }


def _default_end_only_reconciler(
    _controller: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Ask the existing Maa post-live graph to advance or confirm Home."""

    from .controller_client import send_command

    return dict(send_command("advance_nia_post_live", timeout=330.0))


def _prove_end_only_reconciliation(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or value.get("completed") is not True:
        raise ValueError("end-only Maa reconciliation did not complete")
    reason = value.get("reason")
    visited = value.get("visited_nodes")
    terminals = {
        "produce-mode-selection-reached": {"ProduceChooseScenario"},
        "produce-home-reached": {"ProduceHomeFlag", "ProduceBackHome"},
    }
    expected_nodes = terminals.get(reason)
    if (
        expected_nodes is None
        or not isinstance(visited, list)
        or any(not isinstance(name, str) or not name for name in visited)
        or not expected_nodes.intersection(visited)
    ):
        raise ValueError("end-only Maa reconciliation has no known Home terminal")
    return dict(value)


class _NiaLiveStrategyTelemetry:
    """Capture only authoritative runner evidence; never influence gameplay."""

    _STATE_FIELDS = (
        "stamina",
        "max_stamina",
        "produce_points",
        "vocal",
        "dance",
        "visual",
        "vote_count",
    )

    def __init__(
        self,
        *,
        request: NiaLiveUnattendedRequest,
        run_id: str,
        archetype: str,
        initial_snapshot: ProduceOuterLocalSaveSnapshot | None,
        controller: Mapping[str, Any] | None = None,
    ) -> None:
        self.request = request
        self.run_id = run_id
        self.archetype = archetype
        self.path = nia_strategy_journal_path(
            run_id, root=request.strategy_journal_root
        )
        self.checkpoint_path = self.path.with_suffix(".telemetry.json")
        self.values: dict[str, int] = {}
        self.sources: dict[str, str] = {}
        self.week: int | None = None
        self.exam_score: int | None = None
        self.turns_remaining: int | None = None
        self.plays_remaining: int | None = None
        self.choices: list[dict[str, object]] = []
        self.auditions: list[dict[str, object]] = []
        self.seen_steps: set[tuple[object, ...]] = set()
        self.records_written = 0
        self.terminal_written = False
        self.error: str | None = None
        self.recovered_checkpoint = False
        self.controller = (
            dict(controller) if isinstance(controller, Mapping) else None
        )
        self._resume_observed_at: str | None = None
        self._checkpoint_updated_at: str | None = None
        self._checkpoint_local_save_digest: str | None = None
        self._checkpoint_local_save_week: int | None = None
        self._checkpoint_local_save_log_count: int | None = None
        self.local_save_digest: str | None = None
        self.local_save_week: int | None = None
        self.local_save_log_count: int | None = None
        self.date_rollover_resume: dict[str, Any] = {
            "schema": NIA_DATE_ROLLOVER_SCHEMA,
            "kind": NIA_DATE_ROLLOVER_KIND,
            "detected": False,
            "accepted": False,
            "reason": "not-a-resumed-run",
        }
        try:
            self._restore_checkpoint()
        except Exception as error:
            # Telemetry is deliberately non-authoritative for gameplay.  A bad
            # checkpoint must never stop or alter the live Produce run.
            self.error = f"checkpoint-load:{type(error).__name__}: {error}"
        if initial_snapshot is not None:
            self._observe_snapshot(initial_snapshot, "initial-localsave")
        if self.recovered_checkpoint:
            self._resume_observed_at = _utc_now()
            self._evaluate_date_rollover_resume()
        self._persist_checkpoint_nonfatal()

    @staticmethod
    def _optional_integer(value: object, label: str) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ValueError(f"{label} must be a non-negative integer or null")
        return value

    def _restore_checkpoint(self) -> None:
        if not self.checkpoint_path.exists():
            return
        raw = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("telemetry checkpoint must be an object")
        expected_identity = {
            "schema": NIA_LIVE_TELEMETRY_CHECKPOINT_SCHEMA,
            "run_id": self.run_id,
            "mode": self.request.produce_id,
            "idol": self.request.idol_card_id,
            "archetype": self.archetype,
        }
        for name, expected in expected_identity.items():
            if raw.get(name) != expected:
                raise ValueError(f"telemetry checkpoint {name} mismatch")

        values = raw.get("values")
        sources = raw.get("sources")
        if not isinstance(values, Mapping) or not isinstance(sources, Mapping):
            raise ValueError("telemetry checkpoint state is malformed")
        for name in self._STATE_FIELDS:
            if name not in values:
                continue
            observed = self._optional_integer(
                values[name], f"telemetry checkpoint values.{name}"
            )
            source = sources.get(name)
            if observed is None or not isinstance(source, str) or not source:
                raise ValueError(f"telemetry checkpoint source missing for {name}")
            self.values[name] = observed
            self.sources[name] = source

        week = self._optional_integer(raw.get("week"), "telemetry checkpoint week")
        final_week = nia_final_week(self.request.produce_id)
        if week is not None and not 1 <= week <= final_week:
            raise ValueError(
                f"telemetry checkpoint week is outside 1..{final_week}"
            )
        self.week = week
        for name in ("exam_score", "turns_remaining", "plays_remaining"):
            setattr(
                self,
                name,
                self._optional_integer(
                    raw.get(name), f"telemetry checkpoint {name}"
                ),
            )

        for name in ("choices", "auditions"):
            rows = raw.get(name)
            if not isinstance(rows, list) or any(
                not isinstance(row, Mapping) for row in rows
            ):
                raise ValueError(f"telemetry checkpoint {name} is malformed")
            setattr(self, name, [dict(row) for row in rows])

        updated_at = raw.get("updated_at")
        if isinstance(updated_at, str) and updated_at:
            self._checkpoint_updated_at = updated_at
        binding = raw.get("localsave_binding")
        if isinstance(binding, Mapping):
            digest = binding.get("digest")
            if isinstance(digest, str) and digest:
                self._checkpoint_local_save_digest = digest
            week_value = binding.get("week")
            if type(week_value) is int and week_value >= 0:
                self._checkpoint_local_save_week = week_value
            log_count = binding.get("log_count")
            if type(log_count) is int and log_count >= 0:
                self._checkpoint_local_save_log_count = log_count

        # Step indexes restart at one for every resumed runner process.  The
        # de-duplication set is intentionally process-local; restoring it would
        # discard a genuinely new step that happens to share page/action labels
        # with an earlier invocation.
        self.recovered_checkpoint = True

    def _checkpoint_payload(self) -> Mapping[str, Any]:
        return {
            "schema": NIA_LIVE_TELEMETRY_CHECKPOINT_SCHEMA,
            "run_id": self.run_id,
            "mode": self.request.produce_id,
            "idol": self.request.idol_card_id,
            "archetype": self.archetype,
            "values": dict(self.values),
            "sources": dict(self.sources),
            "week": self.week,
            "exam_score": self.exam_score,
            "turns_remaining": self.turns_remaining,
            "plays_remaining": self.plays_remaining,
            "choices": [dict(row) for row in self.choices],
            "auditions": [dict(row) for row in self.auditions],
            "localsave_binding": {
                "digest": self.local_save_digest,
                "week": self.local_save_week,
                "log_count": self.local_save_log_count,
            },
            "updated_at": _utc_now(),
        }

    def _persist_checkpoint_nonfatal(self) -> None:
        try:
            _atomic_json(self.checkpoint_path, self._checkpoint_payload())
        except Exception as error:
            self.error = f"checkpoint-write:{type(error).__name__}: {error}"

    @staticmethod
    def _integer(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    def _phase(self, week: int) -> int | None:
        return nia_phase_for_week(self.request.produce_id, week)

    def _observe_mapping(self, value: Mapping[str, Any], source: str) -> None:
        for name in self._STATE_FIELDS:
            observed = self._integer(value.get(name))
            if observed is not None:
                self.values[name] = observed
                self.sources[name] = source
        digest = localsave_binding_digest(value)
        if digest is not None:
            self.local_save_digest = digest
            week_value = value.get("week")
            self.local_save_week = (
                week_value if type(week_value) is int and week_value >= 0 else None
            )
            log_count = value.get("log_count")
            self.local_save_log_count = (
                log_count if type(log_count) is int and log_count >= 0 else None
            )
        week = self._integer(value.get("week"))
        if week is not None and 1 <= week <= nia_final_week(self.request.produce_id):
            self.week = week

    def _observe_snapshot(
        self, snapshot: ProduceOuterLocalSaveSnapshot, source: str
    ) -> None:
        self._observe_mapping(
            {
                **{
                    "log_count": snapshot.log_count,
                    "week": snapshot.latest_week_marker,
                    "last_completed_week": snapshot.last_completed_week,
                },
                **{
                    name: getattr(snapshot, name, None)
                    for name in self._STATE_FIELDS
                },
            },
            source,
        )

    def _evaluate_date_rollover_resume(self) -> None:
        """Record, but do not infer, a date-rollover resume decision."""

        current_date = _local_date_from_timestamp(self._resume_observed_at)
        checkpoint_date = _local_date_from_timestamp(self._checkpoint_updated_at)
        wall_date_boundary = (
            checkpoint_date is not None
            and current_date is not None
            and checkpoint_date != current_date
        )
        evidence = self.request.date_rollover_evidence
        explicit_date_update = (
            isinstance(evidence, Mapping)
            and evidence.get("interruption_reason") == NIA_DATE_ROLLOVER_REASON
            and evidence.get("explicit_date_update") is True
        )
        # The game changes its business date at the daily reset, which need
        # not cross the PC's local calendar date.  An observer-owned explicit
        # date-update event is therefore a valid detection source; the strict
        # validator below still has to prove the run, LocalSave, controller,
        # and zero-input bindings before the resume can be accepted.
        detected = wall_date_boundary or explicit_date_update
        metadata: dict[str, Any] = {
            "schema": NIA_DATE_ROLLOVER_SCHEMA,
            "kind": NIA_DATE_ROLLOVER_KIND,
            "run_id": self.run_id,
            "mode": self.request.produce_id,
            "idol_card_id": self.request.idol_card_id,
            "detected": detected,
            "accepted": False,
            "reason": (
                "date-boundary-observed-without-explicit-date-update-evidence"
                if detected
                else "date-boundary-not-observed"
            ),
            "checkpoint_updated_at": self._checkpoint_updated_at,
            "resume_observed_at": self._resume_observed_at,
            "checkpoint_date": checkpoint_date,
            "resume_date": current_date,
            "detection_source": (
                "wall-date-boundary"
                if wall_date_boundary
                else "explicit-date-update-evidence"
                if explicit_date_update
                else "none"
            ),
            "week_before": self._checkpoint_local_save_week,
            "week_after": self.local_save_week,
            "localsave_digest_before": self._checkpoint_local_save_digest,
            "localsave_digest_after": self.local_save_digest,
        }
        if detected and evidence is not None:
            pid = self.controller.get("target_pid") if self.controller else None
            hwnd = self.controller.get("target_hwnd") if self.controller else None
            metadata["controller_pid"] = pid
            metadata["controller_hwnd"] = hwnd
            metadata["evidence"] = dict(evidence)
            check = evaluate_date_rollover_resume(
                evidence,
                run_id=self.run_id,
                mode=self.request.produce_id,
                idol_card_id=self.request.idol_card_id,
                checkpoint_week=self._checkpoint_local_save_week,
                current_week=self.local_save_week,
                checkpoint_local_save_digest=self._checkpoint_local_save_digest,
                current_local_save_digest=self.local_save_digest,
                controller_pid=pid,
                controller_hwnd=hwnd,
            )
            metadata.update(check.to_dict())
            if check.accepted:
                metadata["reason"] = check.reason
        elif evidence is not None and not detected:
            metadata["reason"] = "explicit-evidence-without-observed-date-boundary"
        self.date_rollover_resume = metadata

    @staticmethod
    def _exam_state(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
        for name in (
            "actual_next_evidence",
            "evidence_after",
            "evidence_before",
        ):
            evidence = row.get(name)
            if isinstance(evidence, Mapping) and isinstance(
                evidence.get("state"), Mapping
            ):
                return evidence["state"]
        return None

    def _observe_exam(self, outcome: Mapping[str, Any]) -> None:
        compact_steps: list[dict[str, object]] = []
        rows = outcome.get("steps")
        if not isinstance(rows, list):
            policy = outcome.get("execution_policy")
            identity = outcome.get("exam_save_identity")
            final_score = self._integer(outcome.get("final_score"))
            exact_transitions = outcome.get("exact_transitions")
            if not isinstance(exact_transitions, list):
                exact_transitions = []
            exact_transitions = [
                dict(value)
                for value in exact_transitions
                if isinstance(value, Mapping)
            ]
            progress_trace = outcome.get("progress_trace")
            if not isinstance(progress_trace, list):
                progress_trace = []
            progress_trace = [
                dict(value) for value in progress_trace
                if isinstance(value, Mapping)
            ]
            if final_score is not None:
                self.exam_score = final_score
            foreground_recovery_count = outcome.get(
                "foreground_recovery_count"
            )
            if (
                type(foreground_recovery_count) is not int
                or foreground_recovery_count < 0
            ):
                foreground_recovery_count = 0
            if (
                outcome.get("terminal") is True
                and isinstance(policy, Mapping)
                and policy.get("mode")
                == ExamExecutionMode.MAA_COMPLETION_BASELINE.value
                and isinstance(identity, Mapping)
            ):
                stage_position = nia_stage_positions(self.request.produce_id).get(
                    identity.get("step_context_id")
                )
                self.auditions.append(
                    {
                        "week": self.week if stage_position is None else stage_position[0],
                        "phase": (
                            None if self.week is None else self._phase(self.week)
                        )
                        if stage_position is None
                        else stage_position[1],
                        "terminal": True,
                        "reason": str(outcome.get("reason", "")),
                        "score": final_score,
                        # Maa's completion action is one opaque task, so it
                        # has no native-loop ``steps`` list.  Preserve every
                        # exact state/action/state row in the durable ledger
                        # instead of silently reducing a complete exam to a
                        # score/progress trace.
                        "steps": exact_transitions,
                        "exact_transitions": exact_transitions,
                        "exact_transition_count": len(exact_transitions),
                        "progress_trace": progress_trace,
                        "progress_trace_truncated": (
                            outcome.get("progress_trace_truncated") is True
                        ),
                        "deck_shadow_sync": (
                            dict(outcome["deck_shadow_sync"])
                            if isinstance(outcome.get("deck_shadow_sync"), Mapping)
                            else None
                        ),
                        "execution_policy": dict(policy),
                        "exam_save_identity": dict(identity),
                        "foreground_recovery_attempted": (
                            outcome.get("foreground_recovery_attempted") is True
                        ),
                        "foreground_recovery_count": foreground_recovery_count,
                    }
                )
            return
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            decision = row.get("decision")
            state = self._exam_state(row)
            scalar = None
            if isinstance(state, Mapping):
                scalar = state.get("scalar")
                if not isinstance(scalar, Mapping):
                    scalar = state
            if isinstance(scalar, Mapping):
                for key, target in (
                    ("score", "exam_score"),
                    ("remaining_turns", "turns_remaining"),
                    ("remain_turn", "turns_remaining"),
                    ("plays_remaining", "plays_remaining"),
                ):
                    observed = self._integer(scalar.get(key))
                    if observed is not None:
                        setattr(self, target, observed)
                self._observe_mapping(scalar, "settled-examsave")
            best_action = (
                decision.get("best_action")
                if isinstance(decision, Mapping)
                else None
            )
            predicted = (
                decision.get("predicted_value")
                if isinstance(decision, Mapping)
                else None
            )
            compact_steps.append(
                {
                    "index": index,
                    "chosen": best_action if isinstance(best_action, str) else None,
                    "candidates": None,
                    "candidate_set_complete": False,
                    "predicted_value": (
                        predicted
                        if isinstance(predicted, (int, float))
                        and not isinstance(predicted, bool)
                        else None
                    ),
                    "actual_score": self.exam_score,
                }
            )
        if compact_steps:
            self.auditions.append(
                {
                    "week": self.week,
                    "phase": None if self.week is None else self._phase(self.week),
                    "terminal": bool(outcome.get("terminal")),
                    "reason": str(outcome.get("reason", "")),
                    "score": self.exam_score,
                    "steps": compact_steps,
                }
            )

    def _observe_checkpoint_shadow(self, outcome: Mapping[str, Any]) -> None:
        """Use a run-bound shadow checkpoint only for missing max stamina.

        The outer Produce log does not necessarily emit a max-stamina
        transition.  Reward confirmation checkpoints, however, carry the
        already-persisted run shadow produced from the same run-bound
        LocalSave/OCR evidence.  This is an observation of an existing
        authoritative checkpoint, not a derivation from stamina or a default
        value.  Require both the checkpoint run identity and the requested
        mode/idol identity before accepting it, and never overwrite a newer
        max-stamina observation from the runner.
        """
        if "max_stamina" in self.values:
            return
        checkpoint = outcome.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            return
        if checkpoint.get("run_id") != self.run_id:
            return
        shadow = checkpoint.get("shadow")
        if not isinstance(shadow, Mapping):
            return
        if (
            shadow.get("produce_id") != self.request.produce_id
            or shadow.get("idol_card_id") != self.request.idol_card_id
        ):
            return
        maximum = shadow.get("max_stamina")
        if type(maximum) is not int or maximum < 1:
            return
        current = shadow.get("stamina")
        if current is not None and (
            type(current) is not int or current < 0 or current > maximum
        ):
            return
        self.values["max_stamina"] = maximum
        self.sources["max_stamina"] = "recent-step.checkpoint.shadow"

    def _observe_outer_transaction_receipt(
        self,
        raw: Mapping[str, Any],
    ) -> None:
        """Append one autopilot-owned receipt through the existing journal.

        The autopilot supplies transaction facts only.  This persistence owner
        adds the already-bound request/archetype context and lets the typed
        journal validate every field.  A retained progress receipt may arrive
        repeatedly; the journal's exact-row idempotence prevents duplicates.
        """

        if raw.get("run_id") != self.run_id:
            raise ValueError("outer transaction receipt run_id mismatch")
        if raw.get("produce_id") != self.request.produce_id:
            raise ValueError("outer transaction receipt produce_id mismatch")
        receipt = NiaOuterTransactionReceipt.from_dict(
            {
                "schema": NIA_STRATEGY_JOURNAL_SCHEMA,
                "schema_version": NIA_STRATEGY_JOURNAL_SCHEMA_VERSION,
                "record_type": "outer_transaction_v2",
                "record_id": raw.get("record_id"),
                "recorded_at": raw.get("recorded_at"),
                "run_id": self.run_id,
                "mode": self.request.produce_id,
                "idol": self.request.idol_card_id,
                "archetype": self.archetype,
                "week": raw.get("week"),
                "phase": raw.get("phase"),
                "strategy_version": NIA_LIVE_STRATEGY_VERSION,
                "transaction_id": raw.get("transaction_id"),
                "selected_action": raw.get("selected_action"),
                "legal_action_ids": raw.get("legal_action_ids"),
                "candidate_set_complete": raw.get("candidate_set_complete"),
                "target_pid": raw.get("target_pid"),
                "authority": raw.get("authority"),
                "authority_complete": raw.get("authority_complete"),
                "observer_anchor": raw.get("observer_anchor"),
                "observer_anchor_complete": raw.get(
                    "observer_anchor_complete"
                ),
                "terminal_receipt_id": raw.get("terminal_receipt_id"),
                "terminal_kind": raw.get("terminal_kind"),
                "reward": raw.get("reward"),
                "bc_eligible": raw.get("bc_eligible"),
                "rl_eligible": raw.get("rl_eligible"),
                "details": raw.get("details"),
            }
        )
        if append_nia_strategy_journal(self.path, receipt):
            self.records_written += 1

    def observe_progress(self, progress: Mapping[str, Any]) -> None:
        outer_transaction_receipt = progress.get("outer_transaction_receipt")
        if isinstance(outer_transaction_receipt, Mapping):
            self._observe_outer_transaction_receipt(
                outer_transaction_receipt
            )
        recent = progress.get("recent_step")
        if not isinstance(recent, Mapping):
            return
        key = (
            recent.get("index"),
            recent.get("page"),
            recent.get("action"),
            recent.get("target"),
        )
        if key in self.seen_steps:
            return
        self.seen_steps.add(key)
        outcome = recent.get("outcome")
        if isinstance(outcome, Mapping):
            for name in ("authority", "current_authority"):
                authority = outcome.get(name)
                if isinstance(authority, Mapping):
                    self._observe_mapping(authority, f"recent-step.{name}")
            self._observe_checkpoint_shadow(outcome)
            if recent.get("page") == "exam":
                self._observe_exam(outcome)
        action = recent.get("action")
        target = recent.get("target")
        if isinstance(action, str) and isinstance(target, str):
            decision_evidence = (
                outcome.get("decision_evidence")
                if isinstance(outcome, Mapping)
                and isinstance(outcome.get("decision_evidence"), Mapping)
                else None
            )
            candidates = (
                decision_evidence.get("candidates")
                if isinstance(decision_evidence, Mapping)
                else None
            )
            candidate_set_complete = bool(
                isinstance(decision_evidence, Mapping)
                and decision_evidence.get("candidate_set_complete") is True
                and isinstance(candidates, list)
                and candidates
            )
            choice_week = (
                decision_evidence.get("route_week")
                if isinstance(decision_evidence, Mapping)
                and type(decision_evidence.get("route_week")) is int
                else self.week
            )
            choice_phase = (
                decision_evidence.get("route_phase")
                if isinstance(decision_evidence, Mapping)
                and type(decision_evidence.get("route_phase")) is int
                else None if choice_week is None else self._phase(choice_week)
            )
            source = (
                dict(decision_evidence.get("source"))
                if isinstance(decision_evidence, Mapping)
                and isinstance(decision_evidence.get("source"), Mapping)
                else None
            )
            choice = {
                "week": choice_week,
                "phase": choice_phase,
                "page": str(recent.get("page", "")),
                "decision_kind": action,
                "chosen": target,
                "candidates": candidates if candidate_set_complete else None,
                "candidate_set_complete": candidate_set_complete,
                "state": dict(self.values),
            }
            if source is not None:
                choice["source"] = source
            outer_bc_shadow = (
                decision_evidence.get("outer_bc_shadow")
                if isinstance(decision_evidence, Mapping)
                and isinstance(
                    decision_evidence.get("outer_bc_shadow"), Mapping
                )
                else None
            )
            if outer_bc_shadow is not None:
                choice["outer_bc_shadow"] = dict(outer_bc_shadow)
            self.choices.append(
                choice
            )
        self._persist_checkpoint_nonfatal()

    def status(self) -> Mapping[str, Any]:
        from .nia_completion_acceptance import DURABLE_EXAM_LEDGER_SCHEMA

        return {
            "path": str(self.path),
            "run_id": self.run_id,
            "records_written": self.records_written,
            "observed_choices": len(self.choices),
            "observed_auditions": len(self.auditions),
            "terminal_written": self.terminal_written,
            "checkpoint_path": str(self.checkpoint_path),
            "checkpoint_recovered": self.recovered_checkpoint,
            "date_rollover_resume": dict(self.date_rollover_resume),
            "audition_ledger": {
                "schema": DURABLE_EXAM_LEDGER_SCHEMA,
                "run_id": self.run_id,
                "mode": self.request.produce_id,
                "idol": self.request.idol_card_id,
                "archetype": self.archetype,
                "rows": [dict(row) for row in self.auditions],
            },
            "error": self.error,
        }

    def finalize(
        self,
        result: InitialRegularAutopilotResult,
        *,
        failed_audition: bool,
        final_snapshot: ProduceOuterLocalSaveSnapshot | None,
    ) -> None:
        if not result.completed:
            return
        if final_snapshot is not None:
            self._observe_snapshot(final_snapshot, "terminal-localsave")
        missing = [name for name in self._STATE_FIELDS if name not in self.values]
        if self.week is None or self._phase(self.week) is None or missing:
            self.error = "terminal-state-incomplete:" + ",".join(
                (["week"] if self.week is None else []) + missing
            )
            return
        try:
            existing = read_nia_strategy_journal(self.path)
            if any(isinstance(row, NiaFinalRecord) for row in existing):
                self.terminal_written = True
                return
            state = NiaStrategyState(
                **{name: self.values[name] for name in self._STATE_FIELDS},
                exam_score=self.exam_score,
                turns_remaining=self.turns_remaining,
                plays_remaining=self.plays_remaining,
                details={"field_sources": self.sources},
            )
            append_nia_strategy_journal(
                self.path,
                NiaFinalRecord(
                    record_id=f"final-{self.run_id}",
                    recorded_at=_utc_now(),
                    run_id=self.run_id,
                    mode=self.request.produce_id,
                    idol=self.request.idol_card_id,
                    archetype=self.archetype,
                    week=self.week,
                    phase=self._phase(self.week),  # type: ignore[arg-type]
                    strategy_version=NIA_LIVE_STRATEGY_VERSION,
                    state=state,
                    outcome=("audition-failed" if failed_audition else "completed"),
                    passed=not failed_audition,
                    final_score=self.exam_score,
                    details={
                        "runner_stop_reason": result.stop_reason,
                        "candidate_sets_complete": False,
                        "observed_choices": self.choices,
                        "auditions": self.auditions,
                    },
                ),
            )
            self.records_written += 1
            self.terminal_written = True
            self._persist_checkpoint_nonfatal()
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"


def finalize_nia_strategy_telemetry_from_result(
    request: NiaLiveUnattendedRequest,
    *,
    run_id: str,
    result: InitialRegularAutopilotResult,
    final_snapshot: ProduceOuterLocalSaveSnapshot | None = None,
) -> Mapping[str, Any]:
    """Rebuild and terminalize the existing strategy journal from a result.

    The GUI live controller owns the same authoritative
    :class:`InitialRegularAutopilotResult` steps as the unattended wrapper but
    does not keep a ``_NiaLiveStrategyTelemetry`` instance while it runs.  This
    public completion seam replays those already-settled steps through the
    existing telemetry implementation and appends at most one terminal journal
    row.  A repeated call detects the durable terminal row before rebuilding,
    so it cannot duplicate one completed run.
    """

    if not isinstance(request, NiaLiveUnattendedRequest):
        raise TypeError("request must be NiaLiveUnattendedRequest")
    if not isinstance(result, InitialRegularAutopilotResult):
        raise TypeError("result must be InitialRegularAutopilotResult")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be non-empty text")
    run_id = run_id.strip()
    path = nia_strategy_journal_path(
        run_id,
        root=request.strategy_journal_root,
    )

    def durable_status(
        records: Sequence[object],
        *,
        error: str | None = None,
    ) -> Mapping[str, Any]:
        finals = [value for value in records if isinstance(value, NiaFinalRecord)]
        final = finals[-1] if finals else None
        final_payload = None if final is None else final.to_dict()
        details = (
            final_payload.get("details")
            if isinstance(final_payload, Mapping)
            and isinstance(final_payload.get("details"), Mapping)
            else {}
        )
        choices = details.get("observed_choices")
        auditions = details.get("auditions")
        return {
            "path": str(path),
            "run_id": run_id,
            "records_written": len(records),
            "observed_choices": len(choices) if isinstance(choices, list) else 0,
            "observed_auditions": len(auditions) if isinstance(auditions, list) else 0,
            "terminal_written": final is not None,
            "checkpoint_path": str(path.with_suffix(".telemetry.json")),
            "checkpoint_recovered": path.with_suffix(".telemetry.json").exists(),
            "error": error,
        }

    existing = read_nia_strategy_journal(path)
    if any(isinstance(value, NiaFinalRecord) for value in existing):
        return durable_status(existing)

    telemetry = _NiaLiveStrategyTelemetry(
        request=request,
        run_id=run_id,
        archetype=result.plan_type,
        initial_snapshot=None,
    )
    # Reconstruct the whole authoritative result instead of merging a partial
    # completion checkpoint, which could duplicate choices after a retry.
    telemetry.values.clear()
    telemetry.sources.clear()
    telemetry.week = None
    telemetry.exam_score = None
    telemetry.turns_remaining = None
    telemetry.plays_remaining = None
    telemetry.choices.clear()
    telemetry.auditions.clear()
    telemetry.seen_steps.clear()
    for step in result.steps:
        telemetry.observe_progress({"recent_step": step.to_dict()})
    telemetry.finalize(
        result,
        failed_audition=any(
            step.action == "audition-failure-end"
            or step.target == "maa-nia-audition-failure-produce-end"
            for step in result.steps
        ),
        final_snapshot=final_snapshot,
    )
    records = read_nia_strategy_journal(path)
    return durable_status(records, error=telemetry.error)


def run_nia_live_unattended(
    request: NiaLiveUnattendedRequest = NiaLiveUnattendedRequest(),
    *,
    status_reader: ControllerStatusReader = _default_status_reader,
    bootstrapper: Bootstrapper = _default_bootstrapper,
    snapshot_reader: SnapshotReader = read_current_produce_outer_local_save,
    runner: Runner = run_live_initial_regular_autopilot,
    active_run_reader: ActiveRunReader = _default_active_run_reader,
    run_identity_bootstrapper: RunIdentityBootstrapper = (
        _default_run_identity_bootstrapper
    ),
    bootstrap_modal_advancer: BootstrapModalAdvancer = (
        _default_bootstrap_modal_advancer
    ),
    end_only_authority_reader: EndOnlyAuthorityReader = (
        _default_end_only_authority_reader
    ),
    end_only_reconciler: EndOnlyReconciler = _default_end_only_reconciler,
    date_rollover_resumer: DateRolloverResumer = _default_date_rollover_resumer,
    sleep: Callable[[float], None] = time.sleep,
    inner_imitation_runner: InnerImitationExamRunner | None = None,
    outer_observer_cursor_factory: OuterObserverCursorFactory = (
        _default_outer_observer_cursor_factory
    ),
    outer_observer_barrier_call: OuterObserverBarrierCall = (
        _default_outer_observer_barrier_call
    ),
) -> NiaLiveUnattendedReport:
    """Start or safely resume one N.I.A. run, persisting every progress update."""

    if not isinstance(request, NiaLiveUnattendedRequest):
        raise TypeError("request must be NiaLiveUnattendedRequest")
    for value in (
        status_reader,
        bootstrapper,
        snapshot_reader,
        runner,
        active_run_reader,
        run_identity_bootstrapper,
        bootstrap_modal_advancer,
        end_only_authority_reader,
        end_only_reconciler,
        date_rollover_resumer,
        sleep,
        outer_observer_cursor_factory,
        outer_observer_barrier_call,
    ):
        if not callable(value):
            raise TypeError("unattended dependencies must be callable")
    if inner_imitation_runner is not None and not callable(inner_imitation_runner):
        raise TypeError("inner_imitation_runner must be callable or None")

    started_at = _utc_now()
    controller: Mapping[str, Any] | None = None
    bootstrap: Mapping[str, Any] | None = None
    progress: Mapping[str, Any] | None = None
    result: InitialRegularAutopilotResult | None = None
    active_run_id: str | None = None
    preflight: Mapping[str, Any] | None = None
    telemetry: _NiaLiveStrategyTelemetry | None = None
    run_started_by_request = False
    outer_observer_anchor_provider: (
        Callable[[int, int], Mapping[str, Any]] | None
    ) = None

    def persist(status: str, *, error: str | None = None) -> NiaLiveUnattendedReport:
        report = NiaLiveUnattendedReport(
            request=request,
            status=status,
            started_at=started_at,
            finished_at=(None if status == "running" else _utc_now()),
            controller=controller,
            bootstrap=bootstrap,
            progress=progress,
            result=result,
            active_run_id=active_run_id,
            run_started_by_request=run_started_by_request,
            preflight=preflight,
            strategy_journal=None if telemetry is None else telemetry.status(),
            error=error,
        )
        payload = report.to_dict()
        if status == "completed" and active_run_id is not None:
            try:
                from .exact_exam_bc_sidecar import (
                    flush_exact_exam_bc_diagnostics,
                )
                from .outer_bc_shadow import (
                    build_full_cultivation_shadow_report,
                )

                completion = flush_exact_exam_bc_diagnostics(
                    # Native stage validation/scoring is background work and
                    # never blocks formal Maa control.  At the already-
                    # completed run boundary, allow the final stage enough
                    # time to finish its bounded JSONL span before assembling
                    # the durable shadow report.
                    active_run_id,
                    timeout_seconds=30.0,
                )
                shadow = build_full_cultivation_shadow_report(
                    payload,
                    run_id=active_run_id,
                    exact_completion=completion,
                )
            except Exception as shadow_error:
                # A report-side diagnostic must not change the completed
                # cultivation result or its acceptance decision.
                from .outer_bc_shadow import FULL_CULTIVATION_SHADOW_SCHEMA

                shadow = {
                    "schema": FULL_CULTIVATION_SHADOW_SCHEMA,
                    "run_id": active_run_id,
                    "diagnostic_only": True,
                    "applied": False,
                    "formal_result_unchanged": True,
                    "outer": {},
                    "exact": {},
                    "completeness": {
                        "outer_complete": False,
                        "exact_complete": False,
                        "complete": False,
                    },
                    "blockers": [
                        "aggregation-error:"
                        f"{type(shadow_error).__name__}: {shadow_error}"
                    ],
                }
            report = replace(report, full_cultivation_shadow=shadow)
            payload = report.to_dict()
        _atomic_json(request.output_path, payload)
        if (
            status == "completed"
            and isinstance(payload.get("completion_acceptance"), Mapping)
            and payload["completion_acceptance"].get("accepted") is True
        ):
            try:
                from .maa_baseline_inner_behavior import (
                    append_maa_baseline_inner_behavior_report,
                )

                append_maa_baseline_inner_behavior_report(
                    request.output_path
                )
                from .maa_baseline_inner_transition import (
                    append_maa_baseline_inner_transitions,
                )

                append_maa_baseline_inner_transitions(request.output_path)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                # Training export is a post-completion sidecar; the completed
                # cultivation report remains authoritative.
                pass
            if active_run_id is not None:
                try:
                    from .run_identity import clear_active_run

                    clear_active_run(expected_run_id=active_run_id)
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    # The accepted report remains authoritative.  A changed
                    # pointer is deliberately not cleared because it may own
                    # a newer run; the next homepage preflight will expose
                    # that mismatch instead of deleting another identity.
                    pass
        return report

    try:
        selected_plan = load_nia_idol_catalog().require(
            request.idol_card_id
        ).plan_type
        controller = _ensure_controller(
            status_reader,
            request_uac=request.request_uac,
            sleep=sleep,
        )
        preflight = build_nia_live_preflight(controller)
        try:
            outer_observer_cursor = outer_observer_cursor_factory(
                _controller_pid(controller)
            )
            snapshot_barrier = getattr(
                outer_observer_cursor,
                "snapshot_barrier",
                None,
            )
            if not callable(snapshot_barrier):
                raise TypeError(
                    "outer observer cursor has no callable snapshot_barrier"
                )
            barrier_session_failure: dict[str, Any] | None = None

            def live_outer_observer_anchor_provider(
                expected_generation: int,
                minimum_next: int,
            ) -> Mapping[str, Any]:
                nonlocal barrier_session_failure
                if barrier_session_failure is not None:
                    return dict(barrier_session_failure)
                try:
                    barrier_result = outer_observer_barrier_call(
                        expected_generation=expected_generation,
                        minimum_next=minimum_next,
                    )
                    if not isinstance(barrier_result, Mapping):
                        raise TypeError(
                            "outer observer barrier call must return a mapping"
                        )
                    return snapshot_barrier(barrier_result)
                except Exception as error:
                    barrier_session_failure = _incomplete_outer_observer_anchor(
                        "outer-observer-barrier-provider-error",
                        error,
                    )
                    return dict(barrier_session_failure)

            # Keep this closure for the whole run.  Its bound method retains
            # the one cursor/reader instance associated with the controller
            # PID, and each call submits exactly one non-retried barrier.
            outer_observer_anchor_provider = (
                live_outer_observer_anchor_provider
            )
        except Exception as error:
            incomplete_anchor = _incomplete_outer_observer_anchor(
                "outer-observer-cursor-factory-error",
                error,
            )

            def failed_outer_observer_anchor_provider(
                _expected_generation: int,
                _minimum_next: int,
                anchor: Mapping[str, Any] = incomplete_anchor,
            ) -> Mapping[str, Any]:
                return dict(anchor)

            outer_observer_anchor_provider = (
                failed_outer_observer_anchor_provider
            )
        snapshot = _active_snapshot_or_none(snapshot_reader, request.game_root)
        active = active_run_reader()
        active_run_id = _active_run_id(active)
        started_new = False
        if snapshot is not None:
            identity = _active_identity_tuple(active)
            requested_identity = (request.produce_id, request.idol_card_id)
            if identity != requested_identity:
                if not _consume_matching_bootstrap_intent(request, controller):
                    if identity is None:
                        raise RuntimeError(
                            "active N.I.A. LocalSave has no persisted run identity; "
                            "refusing takeover"
                        )
                    raise RuntimeError(
                        "active N.I.A. run identity differs from requested mode/idol"
                    )
                identity_result = dict(
                    run_identity_bootstrapper(
                        idol_card_id=request.idol_card_id,
                        produce_id=request.produce_id,
                        snapshot=snapshot,
                    )
                )
                raw_run_id = identity_result.get("run_id")
                if not isinstance(raw_run_id, str) or not raw_run_id:
                    raise RuntimeError("bootstrap intent produced no run identity")
                active_run_id = raw_run_id
                active = active_run_reader()
                identity = _active_identity_tuple(active)
                if identity is None:
                    identity = requested_identity
                _remove_bootstrap_intent(request.intent_path)
            if identity != requested_identity:
                raise RuntimeError(
                    "active N.I.A. run identity differs from requested mode/idol"
                )
            if (
                request.expected_run_id is not None
                and active_run_id != request.expected_run_id
            ):
                raise RuntimeError("active N.I.A. run_id differs from expected_run_id")
        else:
            if request.expected_run_id is not None:
                requested_identity = (request.produce_id, request.idol_card_id)
                if active_run_id != request.expected_run_id:
                    raise RuntimeError(
                        "active N.I.A. run_id differs from expected_run_id"
                    )
                if _active_identity_tuple(active) != requested_identity:
                    raise RuntimeError(
                        "active N.I.A. run identity differs from requested mode/idol"
                    )
                try:
                    end_only_authority = dict(
                        _prove_end_only_final_authority(
                            request,
                            archetype=selected_plan,
                            raw=dict(end_only_authority_reader(request)),
                        )
                    )
                except Exception as error:
                    raise RuntimeError(
                        "expected_run_id cannot bootstrap a new N.I.A. run; "
                        "end-only recovery unavailable: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                end_only_authority["local_save"] = "absent"
                telemetry = _NiaLiveStrategyTelemetry(
                    request=request,
                    run_id=active_run_id,
                    archetype=selected_plan,
                    initial_snapshot=None,
                    controller=controller,
                )
                if not telemetry.recovered_checkpoint or telemetry.error is not None:
                    raise RuntimeError(
                        "end-only telemetry checkpoint could not be restored: "
                        + str(telemetry.error or "checkpoint-not-recovered")
                    )
                progress = {
                    "current_page": "post-live",
                    "cycles": 0,
                    "outer_action_count": 0,
                    "recent_step": {
                        "index": 0,
                        "page": "post-live",
                        "action": "reconcile",
                        "target": "expected-run-home",
                        "outcome": {
                            "accepted": False,
                            "authority": dict(end_only_authority),
                            "reason": "awaiting-existing-maa-post-live-route",
                        },
                    },
                }
                persist("running")
                reconciled = _prove_end_only_reconciliation(
                    dict(end_only_reconciler(controller))
                )
                step = InitialRegularAutopilotStep(
                    0,
                    "post-live",
                    "reconcile",
                    str(reconciled["reason"]),
                    {
                        "accepted": True,
                        "authority": dict(end_only_authority),
                        "maa": dict(reconciled),
                    },
                )
                result = InitialRegularAutopilotResult(
                    STATUS_COMPLETED,
                    "end-only-home-reconciled",
                    selected_plan,
                    1,
                    (step,),
                )
                progress = {
                    "current_page": "completed",
                    "cycles": 1,
                    "outer_action_count": 0,
                    "recent_step": step.to_dict(),
                    "stop_reason": result.stop_reason,
                }
                telemetry.finalize(
                    result,
                    failed_audition=False,
                    final_snapshot=None,
                )
                if not telemetry.terminal_written or telemetry.error is not None:
                    raise RuntimeError(
                        "end-only terminal strategy journal was not committed: "
                        + str(telemetry.error or "terminal-not-written")
                    )
                return persist("completed")
            _write_bootstrap_intent(request, controller)
            try:
                bootstrap_kwargs: dict[str, Any] = {
                    "produce_id": request.produce_id,
                    "idol_card_id": request.idol_card_id,
                }
                # Keep the default dependency-injection call shape backward
                # compatible.  An AP drink is reachable only when the caller
                # explicitly opts in and the bootstrapper accepts the flag.
                if request.use_ap_drink:
                    bootstrap_kwargs["use_ap_drink"] = True
                bootstrap = dict(bootstrapper(**bootstrap_kwargs))
            except Exception:
                # The controller request is mutating.  A timeout/error can be
                # reported after Maa already selected the card or entered the
                # run, so retain the exact intent for LocalSave-based
                # late-success takeover.  Explicit ``started=false`` below is
                # the only safe pre-identity removal path; otherwise expiry
                # handles a genuinely abandoned intent.
                raise
            if not bool(bootstrap.get("started")):
                _remove_bootstrap_intent(request.intent_path)
                raise RuntimeError(
                    "Maa N.I.A. bootstrap stopped: "
                    + str(bootstrap.get("reason", "unknown"))
                )
            started_new = True
            run_started_by_request = True
            continuation_events: list[Mapping[str, Any]] = []
            deadline = time.monotonic() + 90.0
            next_modal_attempt = 0.0
            while time.monotonic() < deadline:
                snapshot = _active_snapshot_or_none(snapshot_reader, request.game_root)
                if snapshot is not None:
                    break
                now = time.monotonic()
                if now >= next_modal_attempt and not continuation_events:
                    continuation_events.append(
                        dict(bootstrap_modal_advancer(controller))
                    )
                    # The mutating bridge is at-most-once.  Maa's bootstrap
                    # task already handles stacked known setup dialogs before
                    # ProduceEntryFlag; repeating the post-entry click without
                    # a new typed page receipt can hit the next screen.
                    next_modal_attempt = float("inf")
                sleep(0.1)
            bootstrap = {
                **bootstrap,
                "pre_localsave_continuation": [
                    dict(value) for value in continuation_events
                ],
            }
            if snapshot is None:
                reason = (
                    "no-attempt"
                    if not continuation_events
                    else str(continuation_events[-1].get("reason", "unknown"))
                )
                raise RuntimeError(
                    "N.I.A. bootstrap produced no active LocalSave in 90s; "
                    f"last Maa continuation={reason}"
                )

        if started_new:
            identity_result = dict(
                run_identity_bootstrapper(
                    idol_card_id=request.idol_card_id,
                    produce_id=request.produce_id,
                    snapshot=snapshot,
                )
            )
            raw_run_id = identity_result.get("run_id")
            if not isinstance(raw_run_id, str) or not raw_run_id:
                raise RuntimeError("new N.I.A. LocalSave produced no run identity")
            identity_result = {
                **identity_result,
                "produce_id": request.produce_id,
                "idol_card_id": request.idol_card_id,
            }
            active_run_id = raw_run_id
            bootstrap = {**bootstrap, "run_identity": identity_result}
            _remove_bootstrap_intent(request.intent_path)

        persist("running")

        if active_run_id is None:
            raise RuntimeError("N.I.A. runner has no bound run identity")
        telemetry = _NiaLiveStrategyTelemetry(
            request=request,
            run_id=active_run_id,
            archetype=selected_plan,
            initial_snapshot=snapshot,
            controller=controller,
        )

        # A strict date-rollover proof means the game may still be parked at
        # Home after its date-change dialog.  Reuse the existing allow-listed
        # route once, before the regular runner starts reading pages.  The
        # gate is deliberately the accepted evidence recorded by telemetry:
        # normal runs, ordinary checkpoint resumes, and rejected evidence do
        # not send this command.
        if telemetry.date_rollover_resume.get("accepted") is True:
            date_rollover_resumer()

        def on_progress(value: Mapping[str, Any]) -> None:
            nonlocal progress
            progress = dict(value)
            try:
                telemetry.observe_progress(progress)
            except Exception as error:
                telemetry.error = f"{type(error).__name__}: {error}"
            persist("running")

        runner_kwargs: dict[str, Any] = {
            "idol_card_id": request.idol_card_id,
            "plan_type": selected_plan,
            "produce_id": request.produce_id,
            "game_root": request.game_root,
            # Always bind the inner live loop to the exact identity we just
            # resumed or created.  ``request.expected_run_id`` is optional for
            # a fresh run, but once LocalSave has produced a run identity it
            # must not be weakened back to ``None`` for later pages/exams.
            "expected_run_id": active_run_id,
            "stage_number": request.stage_number,
            "exam_policy": resolve_exam_execution_policy(
                request.exam_execution_mode
            ),
            "progress_callback": on_progress,
        }
        if _runner_accepts_outer_observer_anchor_provider(runner):
            runner_kwargs["outer_observer_anchor_provider"] = (
                outer_observer_anchor_provider
            )
        # Preserve the historical injected-runner call shape while the flag
        # is off.  An opt-in runner explicitly accepts the new seam.
        if request.inner_imitation_enabled:
            runner_kwargs["inner_imitation_enabled"] = True
            if inner_imitation_runner is not None:
                runner_kwargs["inner_imitation_runner"] = inner_imitation_runner
        result = runner(**runner_kwargs)
        if not isinstance(result, InitialRegularAutopilotResult):
            raise TypeError("live runner returned an unsupported result")
        active = active_run_reader()
        refreshed_run_id = _active_run_id(active)
        if refreshed_run_id is not None:
            active_run_id = refreshed_run_id
        final_snapshot = None
        if result.completed:
            try:
                observed = snapshot_reader(request.game_root)
                if (
                    isinstance(observed, ProduceOuterLocalSaveSnapshot)
                    and observed.lifecycle is not None
                    and not observed.lifecycle.is_in_progress
                ):
                    final_snapshot = observed
            except (FileNotFoundError, OSError, StopIteration, ValueError):
                pass
        telemetry.finalize(
            result,
            failed_audition=any(
                step.action == "audition-failure-end"
                or step.target == "maa-nia-audition-failure-produce-end"
                for step in result.steps
            ),
            final_snapshot=final_snapshot,
        )
        return persist("completed" if result.completed else "hard-stop")
    except Exception as error:
        return persist("failed", error=f"{type(error).__name__}: {error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one N.I.A. Produce through LocalSave + Maa only."
    )
    parser.add_argument(
        "--produce-id",
        choices=tuple(sorted(SUPPORTED_PRODUCE_IDS)),
        default="produce-005",
    )
    parser.add_argument("--idol-card-id", default=DEFAULT_IDOL_CARD_ID)
    parser.add_argument("--game-root", type=Path, default=DEFAULT_PC_GAME_ROOT)
    parser.add_argument("--expected-run-id")
    parser.add_argument("--stage-number", type=int, default=1)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--intent-path", type=Path, default=DEFAULT_INTENT_PATH)
    parser.add_argument(
        "--strategy-journal-root",
        type=Path,
        default=DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
    )
    parser.add_argument(
        "--exam-execution-mode",
        choices=tuple(mode.value for mode in ExamExecutionMode),
        default=ExamExecutionMode.MAA_COMPLETION_BASELINE.value,
        help=(
            "maa-completion-baseline completes each exam without simulator "
            "coverage; exact keeps the strict simulator path"
        ),
    )
    parser.add_argument("--request-uac", action="store_true")
    parser.add_argument(
        "--use-ap-drink",
        "--allow-ap-drink",
        dest="use_ap_drink",
        action="store_true",
        help="明確允許開場 AP 不足時使用既有回體力道具節點",
    )
    parser.add_argument(
        "--enable-inner-imitation",
        dest="inner_imitation_enabled",
        action="store_true",
        help=(
            "opt in to the injected cross-plan inner imitation bridge; "
            "abstain falls back to the existing Maa baseline"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    report = run_nia_live_unattended(
        NiaLiveUnattendedRequest(
            produce_id=args.produce_id,
            idol_card_id=args.idol_card_id,
            game_root=args.game_root,
            expected_run_id=args.expected_run_id,
            stage_number=args.stage_number,
            output_path=args.output,
            intent_path=args.intent_path,
            strategy_journal_root=args.strategy_journal_root,
            exam_execution_mode=args.exam_execution_mode,
            request_uac=args.request_uac,
            use_ap_drink=args.use_ap_drink,
            inner_imitation_enabled=args.inner_imitation_enabled,
        )
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.completed else 2 if report.status == "hard-stop" else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_INTENT_PATH",
    "NiaLiveUnattendedReport",
    "NiaLiveUnattendedRequest",
    "build_nia_live_preflight",
    "finalize_nia_strategy_telemetry_from_result",
    "run_nia_live_unattended",
]
