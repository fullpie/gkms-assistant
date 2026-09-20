from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .elevated_controller import REQUEST_PATH, RESPONSE_PATH, SESSION_PATH


IPC_MUTEX_NAME = r"Local\gkms_tool.elevated_controller.ipc.v1"
WAIT_OBJECT_0 = 0
WAIT_ABANDONED = 0x80
WAIT_TIMEOUT = 0x102
REQUEST_CONSUMED_ACK_GRACE_SECONDS = 2.0
RESPONSE_LATE_GRACE_SECONDS = 2.0
_READ_ONLY_CAPTURE_TIMEOUT_FLOORS = {
    # The worker's screenshot job is itself bounded at 15 seconds.  Twenty
    # seconds leaves response/ack grace without recreating the old 45+45s
    # client-side stall when the IPC helper disappears.
    "capture_once": 20.0,
    "capture_screen_once": 20.0,
    "capture_native_once": 20.0,
    "recognize_audition_cards_once": 45.0,
    "recognize_nia_replay_skip_once": 45.0,
}
_READ_ONLY_COMMANDS = frozenset(
    {
        "status",
        "capture_once",
        "capture_screen_once",
        "capture_native_once",
        "recognize_audition_cards_once",
        "recognize_nia_mirror_once",
        "recognize_nia_outer_once",
        "audit_nia_replay_controls",
        "recognize_nia_replay_skip_once",
    }
)


@contextmanager
def _serialized_controller_request(timeout: float, *, mutex_name: str = IPC_MUTEX_NAME):
    """Serialize the controller's single-file IPC across Python processes."""

    if os.name != "nt":
        yield
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.ReleaseMutex.restype = ctypes.c_bool
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = kernel32.WaitForSingleObject(
            handle,
            max(1, min(int((timeout + 5.0) * 1000), 0xFFFFFFFE)),
        )
        if result == WAIT_TIMEOUT:
            raise TimeoutError("timed out waiting for elevated controller IPC")
        if result not in {WAIT_OBJECT_0, WAIT_ABANDONED}:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not kernel32.ReleaseMutex(handle):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _wait_until_request_consumed(*, deadline: float) -> None:
    """Wait until the helper has removed the single in-flight request.

    The helper writes ``response.json`` before its ``finally`` block removes
    ``request.json``.  Returning to the next caller as soon as the response is
    visible lets that caller replace ``request.json`` while the helper is
    still deleting the previous one, producing an intermittent Windows
    sharing/permission failure.  The cross-process mutex remains held while
    this function runs, so observing absence is the mailbox acknowledgement.
    """

    while REQUEST_PATH.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("elevated controller 尚未完成上一個 request")
        time.sleep(0.01)


def _send_command_unlocked(
    command: str, *, timeout: float = 5.0, **payload: Any
) -> dict[str, Any]:
    if not SESSION_PATH.is_file():
        raise ConnectionError("找不到 elevated controller session")
    session = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
    if not session.get("active"):
        raise ConnectionError("elevated controller 未執行")
    request_id = uuid.uuid4().hex
    request = {
        "request_id": request_id,
        "token": session["token"],
        "target_pid": session["target_pid"],
        "command": command,
        **payload,
    }
    deadline = time.monotonic() + timeout
    response_deadline = deadline + RESPONSE_LATE_GRACE_SECONDS
    # A caller can time out while the elevated helper is still finishing its
    # request.  Never overwrite that request on a retry.
    _wait_until_request_consumed(deadline=deadline)
    # A prior response is no longer useful once its request returned.  Removing
    # it before posting the next request also avoids a short Windows sharing
    # race when the elevated helper atomically replaces response.json while the
    # medium-integrity client has just finished reading the old file.
    try:
        RESPONSE_PATH.unlink(missing_ok=True)
    except OSError:
        # The helper will report a real write failure if the transient reader
        # lock outlives this request; do not mask it as a client-side success.
        pass
    _atomic_json(REQUEST_PATH, request)
    while time.monotonic() < response_deadline:
        if RESPONSE_PATH.is_file():
            try:
                response = json.loads(RESPONSE_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                time.sleep(0.05)
                continue
            if response.get("request_id") == request_id:
                if not response.get("ok"):
                    raise RuntimeError(response.get("error", "controller command failed"))
                result = response["result"]
                # The helper deliberately publishes the response before its
                # finally block removes request.json.  Once this exact
                # request_id has an ok response, crossing the caller's work
                # deadline during that tiny mailbox acknowledgement gap must
                # not turn an already-completed Maa input into a timeout.
                try:
                    _wait_until_request_consumed(
                        deadline=max(
                            deadline,
                            time.monotonic()
                            + REQUEST_CONSUMED_ACK_GRACE_SECONDS,
                        )
                    )
                except TimeoutError:
                    # The exact request_id already has a successful result.
                    # Keep that result authoritative; the next caller still
                    # waits for request.json to disappear before it can post
                    # another command, so returning cannot overwrite the
                    # helper's in-flight acknowledgement.
                    pass
                return result
        time.sleep(0.05)
    raise TimeoutError(f"等待 elevated controller {command} 回應逾時")


def send_command(command: str, *, timeout: float = 5.0, **payload: Any) -> dict[str, Any]:
    """Send one single-flight request through the elevated controller."""

    # Maa PrintWindow is normally sub-second, but Windows can occasionally
    # hold one read-only frame while Unity presents a new surface.  A 15-second
    # caller timeout used to hard-stop an otherwise healthy unattended run even
    # though the helper completed immediately afterwards.  Captures are
    # side-effect-free, so give only these allow-listed commands a bounded
    # worker-matched floor; mutating commands retain the caller's deadline.
    effective_timeout = max(
        float(timeout),
        _READ_ONLY_CAPTURE_TIMEOUT_FLOORS.get(command, 0.0),
    )
    with _serialized_controller_request(effective_timeout):
        attempts = 2 if command in _READ_ONLY_COMMANDS else 1
        for attempt in range(attempts):
            try:
                return _send_command_unlocked(
                    command,
                    timeout=effective_timeout,
                    **payload,
                )
            except TimeoutError:
                if attempt + 1 >= attempts:
                    raise
        raise AssertionError("controller retry loop exhausted unexpectedly")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="呼叫高權限白名單遊戲控制器。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("focus-game-once")
    subparsers.add_parser("capture-once")
    subparsers.add_parser("capture-screen-once")
    subparsers.add_parser("capture-native-once")
    audition_cards = subparsers.add_parser("recognize-audition-cards-once")
    audition_cards.add_argument("--capture-id")
    audition_cards.add_argument("--ranking-snapshot-id")
    audition_cards.add_argument("--ranking-source-sha256")
    audition_cards.add_argument("--ranking-request-sha256")
    audition_cards.add_argument("--ranking-visible-rank", type=int)
    audition_cards.add_argument("--ranking-replay-identity-sha256")
    audition_cards.add_argument("--evidence-digest")
    audition_cards.add_argument("--exam-save-evidence-digest")
    audition_cards.add_argument("--local-save-digest")
    audition_cards.add_argument("--exam-save-sha256")
    audition_cards.add_argument("--source-sha256")
    audition_cards.add_argument("--session-transition-id")
    audition_cards.add_argument("--transition-id")
    audition_cards.add_argument("--run-id")
    audition_cards.add_argument("--step-context-id")
    subparsers.add_parser("recognize-nia-mirror-once")
    nia_reco = subparsers.add_parser("recognize-nia-outer-once")
    nia_reco.add_argument(
        "--scope",
        choices=(
            "all",
            "overview",
            "subpage",
            "drink-dialog",
            "exam-action",
            "business",
            "activity",
            "outing",
            "consultation",
            "guidance",
            "lesson",
            "rest",
            "reward",
            "card-operation",
        ),
        default="all",
    )
    subparsers.add_parser("audit-nia-replay-controls")
    subparsers.add_parser("recognize-nia-replay-skip-once")
    ranking = subparsers.add_parser("open-produce-ranking")
    ranking.add_argument("--timeout-seconds", type=float, default=60.0)
    nia_replay = subparsers.add_parser("open-nia-recommended-replay")
    nia_replay.add_argument("--idol-card-id", required=True)
    nia_replay.add_argument("--timeout-seconds", type=float, default=60.0)
    nia_replay.add_argument(
        "--from-home",
        action="store_true",
        help="從新的培育首頁進入固定 NIA Master 卡片選擇流程",
    )
    nia_replay.add_argument(
        "--use-ap-drink",
        "--allow-ap-drink",
        dest="use_ap_drink",
        action="store_true",
        help="明確允許推薦回放入口 AP 不足時使用既有回復飲料節點",
    )
    nia_pro_replay = subparsers.add_parser("open-nia-pro-recommended-replay")
    nia_pro_replay.add_argument("--idol-card-id", required=True)
    nia_pro_replay.add_argument("--timeout-seconds", type=float, default=60.0)
    nia_pro_replay.add_argument(
        "--from-home",
        action="store_true",
        help="從新的培育首頁進入固定 NIA Pro 卡片選擇流程",
    )
    nia_pro_replay.add_argument(
        "--use-ap-drink",
        "--allow-ap-drink",
        dest="use_ap_drink",
        action="store_true",
        help="明確允許推薦回放入口 AP 不足時使用既有回復飲料節點",
    )
    nia_pro_replay_start = subparsers.add_parser(
        "start-first-nia-pro-recommended-replay"
    )
    nia_pro_replay_start.add_argument(
        "--timeout-seconds", type=float, default=30.0
    )
    nia_pro_replay_start.add_argument(
        "--visible-rank", type=int, default=1
    )
    home_wait = subparsers.add_parser("wait-global-home")
    home_wait.add_argument("--timeout-seconds", type=float, default=60.0)
    replay_control = subparsers.add_parser("advance-nia-replay")
    replay_control.add_argument("--target-turn", type=int)
    replay_control.add_argument("--timeout-seconds", type=float, default=30.0)
    nia_start = subparsers.add_parser("start-nia-produce")
    nia_start.add_argument(
        "--produce-id", choices=("produce-004", "produce-005"), required=True
    )
    nia_start.add_argument("--idol-card-id", required=True)
    nia_start.add_argument(
        "--use-ap-drink",
        "--allow-ap-drink",
        dest="use_ap_drink",
        action="store_true",
        help="明確允許開場 AP 不足時使用既有回體力道具節點",
    )
    produce_start = subparsers.add_parser("start-produce")
    produce_start.add_argument(
        "--produce-id",
        choices=("produce-001", "produce-002", "produce-003", "produce-004", "produce-005"),
        required=True,
    )
    produce_start.add_argument("--idol-card-id", required=True)
    produce_start.add_argument(
        "--use-ap-drink",
        "--allow-ap-drink",
        dest="use_ap_drink",
        action="store_true",
        help="明確允許開場 AP 不足時使用既有回體力道具節點",
    )
    subparsers.add_parser("run-initial-plan1-exam")
    baseline_exam = subparsers.add_parser("run-maa-baseline-exam")
    baseline_exam.add_argument(
        "--timeout-seconds",
        type=float,
        default=600.0,
    )
    subparsers.add_parser("advance-nia-post-live")
    click = subparsers.add_parser("click-once")
    click.add_argument("--x", type=int, required=True, help="遊戲外框視窗內 X")
    click.add_argument("--y", type=int, required=True, help="遊戲外框視窗內 Y")
    click.add_argument(
        "--method", choices=("send-input", "window-message"), default="send-input"
    )
    scroll = subparsers.add_parser("scroll-once")
    scroll.add_argument("--dx", type=int, default=0)
    scroll.add_argument("--dy", type=int, required=True)
    swipe = subparsers.add_parser("swipe-once")
    swipe.add_argument("--x1", type=int, required=True)
    swipe.add_argument("--y1", type=int, required=True)
    swipe.add_argument("--x2", type=int, required=True)
    swipe.add_argument("--y2", type=int, required=True)
    swipe.add_argument("--duration-ms", type=int, default=450)
    subparsers.add_parser("close-game")
    # Compatibility spelling retained for clients created before the fixed
    # close-game command was named canonically.
    subparsers.add_parser("close-game-once")
    subparsers.add_parser("stop")
    args = parser.parse_args()
    if args.command == "status":
        result = send_command("status")
    elif args.command == "focus-game-once":
        result = send_command("focus_game_once")
    elif args.command == "capture-once":
        result = send_command("capture_once", timeout=15.0)
    elif args.command == "capture-screen-once":
        result = send_command("capture_screen_once", timeout=15.0)
    elif args.command == "capture-native-once":
        result = send_command("capture_native_once", timeout=15.0)
    elif args.command == "recognize-audition-cards-once":
        capture_metadata = {
            key: value
            for key, value in {
                "capture_id": args.capture_id,
                "ranking_snapshot_id": args.ranking_snapshot_id,
                "ranking_source_sha256": args.ranking_source_sha256,
                "ranking_request_sha256": args.ranking_request_sha256,
                "ranking_visible_rank": args.ranking_visible_rank,
                "ranking_replay_identity_sha256": args.ranking_replay_identity_sha256,
                "evidence_digest": args.evidence_digest,
                "exam_save_evidence_digest": args.exam_save_evidence_digest,
                "local_save_digest": args.local_save_digest,
                "exam_save_sha256": args.exam_save_sha256,
                "source_sha256": args.source_sha256,
                "session_transition_id": args.session_transition_id,
                "transition_id": args.transition_id,
                "run_id": args.run_id,
                "step_context_id": args.step_context_id,
            }.items()
            if value is not None
        }
        result = send_command(
            "recognize_audition_cards_once",
            timeout=45.0,
            capture_metadata=capture_metadata,
        )
    elif args.command == "recognize-nia-mirror-once":
        result = send_command("recognize_nia_mirror_once", timeout=20.0)
    elif args.command == "recognize-nia-outer-once":
        result = send_command(
            "recognize_nia_outer_once", timeout=20.0, scope=args.scope
        )
    elif args.command == "audit-nia-replay-controls":
        result = send_command("audit_nia_replay_controls")
    elif args.command == "recognize-nia-replay-skip-once":
        result = send_command("recognize_nia_replay_skip_once", timeout=15.0)
    elif args.command == "open-produce-ranking":
        timeout_seconds = max(10.0, min(180.0, float(args.timeout_seconds)))
        result = send_command(
            "open_produce_ranking",
            timeout=timeout_seconds + 15.0,
            timeout_seconds=timeout_seconds,
        )
    elif args.command == "open-nia-recommended-replay":
        timeout_seconds = max(10.0, min(180.0, float(args.timeout_seconds)))
        payload = {
            "timeout_seconds": timeout_seconds,
            "idol_card_id": args.idol_card_id,
        }
        if args.from_home:
            payload["from_new_produce_home"] = True
        if args.use_ap_drink:
            payload["use_ap_drink"] = True
        result = send_command(
            "open_nia_recommended_replay",
            timeout=timeout_seconds + 15.0,
            **payload,
        )
    elif args.command == "open-nia-pro-recommended-replay":
        timeout_seconds = max(10.0, min(180.0, float(args.timeout_seconds)))
        payload = {
            "timeout_seconds": timeout_seconds,
            "idol_card_id": args.idol_card_id,
        }
        if args.from_home:
            payload["from_new_produce_home"] = True
        if args.use_ap_drink:
            payload["use_ap_drink"] = True
        result = send_command(
            "open_nia_pro_recommended_replay",
            timeout=timeout_seconds + 15.0,
            **payload,
        )
    elif args.command == "start-first-nia-pro-recommended-replay":
        timeout_seconds = max(10.0, min(180.0, float(args.timeout_seconds)))
        result = send_command(
            "start_first_nia_pro_recommended_replay",
            timeout=timeout_seconds + 15.0,
            timeout_seconds=timeout_seconds,
            visible_rank=args.visible_rank,
        )
    elif args.command == "wait-global-home":
        timeout_seconds = max(10.0, min(120.0, float(args.timeout_seconds)))
        result = send_command(
            "wait_global_home",
            timeout=timeout_seconds + 15.0,
            timeout_seconds=timeout_seconds,
        )
    elif args.command == "advance-nia-replay":
        timeout_seconds = max(10.0, min(180.0, float(args.timeout_seconds)))
        payload = {"timeout_seconds": timeout_seconds}
        if args.target_turn is not None:
            payload["target_turn"] = args.target_turn
        result = send_command(
            "advance_nia_replay",
            timeout=timeout_seconds + 15.0,
            **payload,
        )
    elif args.command == "start-nia-produce":
        payload = {
            "produce_id": args.produce_id,
            "idol_card_id": args.idol_card_id,
        }
        if args.use_ap_drink:
            payload["use_ap_drink"] = True
        result = send_command(
            "start_nia_produce",
            timeout=150.0,
            **payload,
        )
    elif args.command == "start-produce":
        payload = {
            "produce_id": args.produce_id,
            "idol_card_id": args.idol_card_id,
        }
        if args.use_ap_drink:
            payload["use_ap_drink"] = True
        result = send_command(
            "start_produce",
            timeout=150.0,
            **payload,
        )
    elif args.command == "run-initial-plan1-exam":
        result = send_command("run_initial_plan1_exam", timeout=270.0)
    elif args.command == "run-maa-baseline-exam":
        timeout_seconds = max(10.0, min(600.0, float(args.timeout_seconds)))
        result = send_command(
            "run_maa_baseline_exam",
            timeout=timeout_seconds + 30.0,
            timeout_seconds=timeout_seconds,
        )
    elif args.command == "advance-nia-post-live":
        result = send_command("advance_nia_post_live", timeout=330.0)
    elif args.command == "click-once":
        command = "send_input_click_once" if args.method == "send-input" else "click_once"
        result = send_command(command, window_x=args.x, window_y=args.y)
    elif args.command == "scroll-once":
        result = send_command(
            "send_input_scroll_once",
            delta_x=args.dx,
            delta_y=args.dy,
        )
    elif args.command == "swipe-once":
        result = send_command(
            "send_input_swipe_once",
            x1=args.x1,
            y1=args.y1,
            x2=args.x2,
            y2=args.y2,
            duration_ms=args.duration_ms,
        )
    elif args.command == "close-game":
        result = send_command("close_game", timeout=15.0)
    elif args.command == "close-game-once":
        result = send_command("close_game_once", timeout=15.0)
    else:
        result = send_command("stop")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
