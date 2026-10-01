"""Headless Maa + LocalSave runner for Initial Pro/Master cultivation.

The bootstrap uses Maa's OCR card picker.  Once Produce starts, weekly state is
read from Produce LocalSave and card play from ExamSave; this module adds no
fixed-click navigation or duplicate screen validation policy.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

from .initial_regular_autopilot import (
    PLAN1,
    PLAN2,
    PLAN3,
    STATUS_COMPLETED,
    InitialRegularAutopilotResult,
    run_live_initial_regular_autopilot,
)
from .master_db import get_idol_profile
from .nia_live_unattended import (
    MIN_CONTROLLER_VERSION,
    _active_snapshot_or_none,
    _ensure_controller,
)
from .produce_outer_local_save import (
    DEFAULT_PC_GAME_ROOT,
    ProduceOuterLocalSaveSnapshot,
    read_current_produce_outer_local_save,
)


SCHEMA = "gkms.initial-live-unattended.v1"
SUPPORTED_PRODUCE_IDS = frozenset({"produce-001", "produce-002", "produce-003"})
SUPPORTED_PLAN_TYPES = frozenset({PLAN1, PLAN2, PLAN3})
DEFAULT_OUTPUT_PATH = (
    Path(__file__).resolve().parents[2] / "var" / "initial_live" / "latest.json"
)


@dataclass(frozen=True, slots=True)
class InitialLiveUnattendedRequest:
    produce_id: str = "produce-003"
    idol_card_id: str = "i_card-fktn-3-000"
    game_root: Path = DEFAULT_PC_GAME_ROOT
    expected_run_id: str | None = None
    stage_number: int = 1
    output_path: Path = DEFAULT_OUTPUT_PATH
    request_uac: bool = False

    def __post_init__(self) -> None:
        if self.produce_id not in SUPPORTED_PRODUCE_IDS:
            raise ValueError("produce_id must be produce-001, produce-002, or produce-003")
        if not isinstance(self.idol_card_id, str) or not self.idol_card_id.strip():
            raise ValueError("idol_card_id must be non-empty text")
        if self.expected_run_id is not None and (
            not isinstance(self.expected_run_id, str) or not self.expected_run_id.strip()
        ):
            raise ValueError("expected_run_id must be non-empty text or None")
        if type(self.stage_number) is not int or self.stage_number < 1:
            raise ValueError("stage_number must be a positive integer")
        if type(self.request_uac) is not bool:
            raise TypeError("request_uac must be bool")
        object.__setattr__(self, "game_root", Path(self.game_root).resolve())
        object.__setattr__(self, "output_path", Path(self.output_path).resolve())

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["game_root"] = str(self.game_root)
        value["output_path"] = str(self.output_path)
        return value


@dataclass(frozen=True, slots=True)
class InitialLiveUnattendedReport:
    request: InitialLiveUnattendedRequest
    status: str
    started_at: str
    finished_at: str | None
    controller: Mapping[str, Any] | None
    bootstrap: Mapping[str, Any] | None
    bootstrap_confirmed: bool
    progress: Mapping[str, Any] | None
    result: InitialRegularAutopilotResult | None
    active_run_id: str | None
    error: str | None = None
    schema: str = SCHEMA

    @property
    def completed(self) -> bool:
        return self.result is not None and self.result.status == STATUS_COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request": self.request.to_dict(),
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "controller": None if self.controller is None else dict(self.controller),
            "bootstrap": None if self.bootstrap is None else dict(self.bootstrap),
            "bootstrap_confirmed": self.bootstrap_confirmed,
            "progress": None if self.progress is None else dict(self.progress),
            "result": None if self.result is None else self.result.to_dict(),
            "active_run_id": self.active_run_id,
            "error": self.error,
            "production_runtime": True,
            "authentic_full_run_completed": self.completed,
            "input_backend": "MaaFramework-Win32-only",
            "state_authority": "ProduceLocalSave+ExamSave",
        }


ControllerStatusReader = Callable[[], Mapping[str, Any]]
Bootstrapper = Callable[..., Mapping[str, Any]]
SnapshotReader = Callable[[str | Path], ProduceOuterLocalSaveSnapshot]
Runner = Callable[..., InitialRegularAutopilotResult]
ActiveRunReader = Callable[[], Any]
RunIdentityBootstrapper = Callable[..., Mapping[str, Any]]
BootstrapModalAdvancer = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _default_status_reader() -> Mapping[str, Any]:
    from .controller_client import send_command

    return dict(send_command("status"))


def _default_bootstrapper(*, produce_id: str, idol_card_id: str) -> Mapping[str, Any]:
    from .controller_client import send_command

    return dict(
        send_command(
            "start_produce",
            timeout=150.0,
            produce_id=produce_id,
            idol_card_id=idol_card_id,
        )
    )


def _default_bootstrap_modal_advancer(
    controller: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Submit one Maa-recognized pre-LocalSave setup dialog action.

    Maa's Produce preparation task intentionally stops at ``ProduceEntryFlag``.
    A card's first use can then show voice/fast-forward setup dialogs before
    the game creates Produce LocalSave.  Reuse Maa's existing button batch and
    submit only one non-cancel action; the caller checks LocalSave again before
    requesting another action.  No OCR text, colour, fixed week or card-slot
    assumption participates in this bridge.
    """

    from .controller_client import send_command
    from .live_actions import canonical_to_outer_window

    batch = dict(
        send_command("recognize_nia_outer_once", timeout=20.0, scope="subpage")
    )
    raw_capture = batch.get("capture")
    raw_nodes = batch.get("nodes")
    if not isinstance(raw_capture, Mapping) or not isinstance(raw_nodes, list):
        raise ValueError("Maa bootstrap modal batch is malformed")
    if (
        raw_capture.get("hwnd") != controller.get("target_hwnd")
        or raw_capture.get("pid") != controller.get("target_pid")
    ):
        raise ValueError("Maa bootstrap modal capture belongs to another window")
    # Dedicated Maa nodes come first.  Cancel/reject/back buttons are
    # intentionally absent: a bootstrap bridge may advance setup, never undo
    # the user's already-confirmed Produce selection.
    priority = (
        "decide",
        "common-decide",
        "common-next",
        "common-ok",
        "common-ok-alt",
        "common-yes",
        "common-start",
        "common-skip-confirm",
        "common-continue",
        "common-close",
    )
    by_name = {
        str(node.get("node")): node
        for node in raw_nodes
        if isinstance(node, Mapping) and isinstance(node.get("node"), str)
    }
    selected_name = next((name for name in priority if name in by_name), None)
    if selected_name is None:
        return {
            "submitted": False,
            "reason": "no-maa-bootstrap-modal-action",
            "observed_nodes": sorted(by_name),
            "capture": dict(raw_capture),
        }
    selected = by_name[selected_name]
    box = selected.get("box")
    if (
        not isinstance(box, list)
        or len(box) != 4
        or any(type(value) is not int for value in box)
        or box[2] <= 0
        or box[3] <= 0
    ):
        raise ValueError("Maa bootstrap modal action has an invalid box")
    geometry = controller.get("geometry")
    if not isinstance(geometry, Mapping):
        raise ValueError("Maa controller has no window geometry")
    canonical_x = box[0] + box[2] // 2
    canonical_y = box[1] + box[3] // 2
    window_x, window_y = canonical_to_outer_window(
        canonical_x,
        canonical_y,
        geometry,
    )
    clicked = dict(
        send_command(
            "send_input_click_once",
            window_x=window_x,
            window_y=window_y,
        )
    )
    return {
        "submitted": True,
        "reason": "maa-bootstrap-modal-action",
        "node": selected_name,
        "box": list(box),
        "score": selected.get("score"),
        "capture": dict(raw_capture),
        "click": clicked,
    }


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
            selected_idol_confirmed=True,
        )
    )


def _active_run_id(active: Any) -> str | None:
    value = None if active is None else getattr(active, "run_id", None)
    return value if isinstance(value, str) and value else None


def _active_identity(active: Any) -> tuple[str, str] | None:
    if active is None:
        return None
    produce_id = getattr(active, "produce_id", None)
    idol_card_id = getattr(active, "idol_card_id", None)
    if not isinstance(produce_id, str) or not isinstance(idol_card_id, str):
        raise TypeError("active run identity is malformed")
    return produce_id, idol_card_id


def run_initial_live_unattended(
    request: InitialLiveUnattendedRequest = InitialLiveUnattendedRequest(),
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
    sleep: Callable[[float], None] = time.sleep,
) -> InitialLiveUnattendedReport:
    """Start or resume one supported Initial run and persist progress."""

    if not isinstance(request, InitialLiveUnattendedRequest):
        raise TypeError("request must be InitialLiveUnattendedRequest")
    for value in (
        status_reader,
        bootstrapper,
        snapshot_reader,
        runner,
        active_run_reader,
        run_identity_bootstrapper,
        bootstrap_modal_advancer,
        sleep,
    ):
        if not callable(value):
            raise TypeError("unattended dependencies must be callable")

    profile = get_idol_profile(request.idol_card_id)
    if profile is None:
        raise ValueError(f"idol_card_id is not present in Master: {request.idol_card_id}")
    selected_plan = profile.plan_type
    if selected_plan not in SUPPORTED_PLAN_TYPES:
        raise ValueError(
            "live Initial backend currently supports Plan1/Plan2/Plan3; "
            f"{request.idol_card_id} has {selected_plan}"
        )

    started_at = _utc_now()
    controller: Mapping[str, Any] | None = None
    bootstrap: Mapping[str, Any] | None = None
    progress: Mapping[str, Any] | None = None
    result: InitialRegularAutopilotResult | None = None
    active_run_id: str | None = None
    bootstrap_confirmed = False

    def persist(status: str, *, error: str | None = None) -> InitialLiveUnattendedReport:
        report = InitialLiveUnattendedReport(
            request=request,
            status=status,
            started_at=started_at,
            finished_at=None if status == "running" else _utc_now(),
            controller=controller,
            bootstrap=bootstrap,
            bootstrap_confirmed=bootstrap_confirmed,
            progress=progress,
            result=result,
            active_run_id=active_run_id,
            error=error,
        )
        _atomic_json(request.output_path, report.to_dict())
        return report

    try:
        controller = _ensure_controller(
            status_reader, request_uac=request.request_uac, sleep=sleep
        )
        if int(controller.get("controller_version", 0)) < MIN_CONTROLLER_VERSION:
            raise RuntimeError(
                f"Maa controller v{MIN_CONTROLLER_VERSION} is required for "
                "the filtered OCR bootstrap"
            )
        snapshot = _active_snapshot_or_none(snapshot_reader, request.game_root)
        active = active_run_reader()
        active_run_id = _active_run_id(active)
        requested_identity = (request.produce_id, request.idol_card_id)
        started_new = False
        if snapshot is not None:
            if _active_identity(active) != requested_identity:
                raise RuntimeError("active Initial run identity differs from requested mode/idol")
            if request.expected_run_id is not None and active_run_id != request.expected_run_id:
                raise RuntimeError("active Initial run_id differs from expected_run_id")
            if request.expected_run_id is None:
                # An active pointer identifies the last known run, not the
                # current Produce session.  Reconcile it against the current
                # outer LocalSave so a same-mode/same-idol new run can rotate
                # its identity instead of inheriting a completed shadow.
                identity = dict(
                    run_identity_bootstrapper(
                        idol_card_id=request.idol_card_id,
                        produce_id=request.produce_id,
                        snapshot=snapshot,
                    )
                )
                raw_run_id = identity.get("run_id")
                if not isinstance(raw_run_id, str) or not raw_run_id:
                    raise RuntimeError(
                        "active Initial LocalSave produced no reconciled run identity"
                    )
                active_run_id = raw_run_id
                bootstrap = {
                    "started": False,
                    "reason": "active-localsave-identity-reconciled",
                    "run_identity": identity,
                }
        else:
            if request.expected_run_id is not None:
                raise RuntimeError("expected_run_id cannot bootstrap a new Initial run")
            bootstrap = dict(
                bootstrapper(
                    produce_id=request.produce_id,
                    idol_card_id=request.idol_card_id,
                )
            )
            if not bool(bootstrap.get("started")):
                raise RuntimeError(
                    "Maa Initial bootstrap stopped: "
                    + str(bootstrap.get("reason", "unknown"))
                )
            bootstrap_confirmed = True
            started_new = True
            continuation_events: list[Mapping[str, Any]] = []
            deadline = time.monotonic() + 90.0
            next_modal_attempt = 0.0
            while time.monotonic() < deadline:
                snapshot = _active_snapshot_or_none(snapshot_reader, request.game_root)
                if snapshot is not None:
                    break
                now = time.monotonic()
                if now >= next_modal_attempt:
                    continuation_events.append(
                        dict(bootstrap_modal_advancer(controller))
                    )
                    next_modal_attempt = now + 1.0
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
                    "Initial bootstrap produced no active LocalSave in 90s; "
                    f"last Maa continuation={reason}"
                )
            identity = dict(
                run_identity_bootstrapper(
                    idol_card_id=request.idol_card_id,
                    produce_id=request.produce_id,
                    snapshot=snapshot,
                )
            )
            raw_run_id = identity.get("run_id")
            if not isinstance(raw_run_id, str) or not raw_run_id:
                raise RuntimeError("new Initial LocalSave produced no run identity")
            active_run_id = raw_run_id
            bootstrap = {**bootstrap, "run_identity": identity}

        persist("running")

        def on_progress(value: Mapping[str, Any]) -> None:
            nonlocal progress, active_run_id
            progress = dict(value)
            refreshed = active_run_reader()
            refreshed_id = _active_run_id(refreshed)
            if refreshed_id is not None:
                active_run_id = refreshed_id
            persist("running")

        result = runner(
            idol_card_id=request.idol_card_id,
            plan_type=selected_plan,
            produce_id=request.produce_id,
            game_root=request.game_root,
            expected_run_id=active_run_id,
            stage_number=request.stage_number,
            selected_idol_confirmed=bootstrap_confirmed,
            progress_callback=on_progress,
        )
        if not isinstance(result, InitialRegularAutopilotResult):
            raise TypeError("live runner returned an unsupported result")
        active_run_id = _active_run_id(active_run_reader()) or active_run_id
        return persist("completed" if result.completed else "hard-stop")
    except Exception as error:
        return persist("failed", error=f"{type(error).__name__}: {error}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one Initial cultivation through LocalSave + Maa only."
    )
    parser.add_argument(
        "--produce-id",
        choices=tuple(sorted(SUPPORTED_PRODUCE_IDS)),
        default="produce-003",
    )
    parser.add_argument("--idol-card-id", default="i_card-fktn-3-000")
    parser.add_argument("--game-root", type=Path, default=DEFAULT_PC_GAME_ROOT)
    parser.add_argument("--expected-run-id")
    parser.add_argument("--stage-number", type=int, default=1)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--request-uac", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = run_initial_live_unattended(
        InitialLiveUnattendedRequest(
            produce_id=args.produce_id,
            idol_card_id=args.idol_card_id,
            game_root=args.game_root,
            expected_run_id=args.expected_run_id,
            stage_number=args.stage_number,
            output_path=args.output,
            request_uac=args.request_uac,
        )
    )
    # The authoritative report file is UTF-8.  Keep console output ASCII-safe
    # because a redirected Windows console can still expose the legacy cp950
    # codec and otherwise turn an already-written report into a false process
    # failure.
    print(json.dumps(report.to_dict(), ensure_ascii=True, indent=2))
    return 0 if report.completed else 2 if report.status == "hard-stop" else 3


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_OUTPUT_PATH",
    "InitialLiveUnattendedReport",
    "InitialLiveUnattendedRequest",
    "run_initial_live_unattended",
]
