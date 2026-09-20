"""Native Outer snapshots and action transactions, independent of vision.

Policies rank the actual candidates produced by the game's current presenter.
Only this gateway submits the chosen callback and waits for native progress.
An unresolved callback stays pending across runner restarts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any

from .runtime_command_client import (
    RuntimeCommandClient, RuntimeCommandError, RuntimeCommandPending,
    RuntimeCommandProtocolError, RuntimeCommandRequest,
)
from .runtime_exam_executor import _controller_lease
from .runtime_outer_behavior_prior import DEFAULT_WEEKLY_BC_PROVIDER
from .training_artifact_io import atomic_write, canonical_json_bytes


def _record_terminal_bookkeeping_pending(client_root, run_id, payload, error):
    """Record a local cleanup failure without re-finalizing gameplay evidence.

    The already-written terminal payload is the authority. Neither a sharing
    violation nor a secondary evidence-write failure authorizes another action.
    """
    import hashlib
    import uuid
    from .run_identity import ACTIVE_POINTER_NAME, DEFAULT_RUN_ROOT

    terminal_path = Path(client_root) / "last_cultivation.json"
    terminal_bytes = canonical_json_bytes(payload)
    warning = {
        "schema": "gkms.terminal-bookkeeping.v1", "state": "pending",
        "run_id": run_id, "operation": "clear-active-run",
        "stop_reason": "terminal-bookkeeping-pending:active-run-clear-failed",
        "terminal_status": payload["status"], "terminal_stop_reason": payload["stop_reason"],
        "terminal_payload": {"path": str(terminal_path),
            "sha256": hashlib.sha256(terminal_bytes).hexdigest(), "bytes": len(terminal_bytes)},
        "completed_run_evidence": deepcopy(payload.get("completed_run_evidence")),
        "native_segment": deepcopy(payload.get("native_segment")),
        "active_pointer": {"path": str(DEFAULT_RUN_ROOT / ACTIVE_POINTER_NAME),
            "expected_run_id": run_id, "cleared": False},
        "error": {"type": type(error).__name__, "message": str(error),
            "winerror": getattr(error, "winerror", None), "errno": getattr(error, "errno", None)},
        "automatic_restart_allowed": False, "gameplay_result_changed": False,
        "game_action_submitted": False, "dataset_admission_changed": False,
    }
    journal_path = Path(client_root) / "terminal_bookkeeping" / (uuid.uuid4().hex + ".json")
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve the exact pre-warning terminal bytes in this immutable entry;
        # last_cultivation is a mutable diagnostic and may later carry the warning.
        journal = {"bookkeeping": warning, "terminal_payload": payload}
        journal_bytes = canonical_json_bytes(journal)
        atomic_write(journal_path, journal_bytes)
        warning = {**warning, "durable": True, "journal": {"path": str(journal_path),
            "sha256": hashlib.sha256(journal_bytes).hexdigest(), "bytes": len(journal_bytes)}}
    except Exception as storage_error:
        warning = {**warning, "durable": False,
            "persistence_error": f"{type(storage_error).__name__}: {storage_error}"}
    try:
        atomic_write(terminal_path, canonical_json_bytes({**payload, "terminal_bookkeeping": warning}))
    except Exception as storage_error:
        # The original completed payload remains intact if this atomic update
        # fails. Keep the warning visible to the caller even if storage is full.
        warning = {**warning,
            "latest_update_error": f"{type(storage_error).__name__}: {storage_error}"}
    return warning


from .runtime_outer_snapshot import RuntimeOuterSnapshot, RuntimeOuterReader


@dataclass(frozen=True, slots=True)
class RuntimeOuterOutcome:
    status: str
    detail: str
    snapshot: RuntimeOuterSnapshot | None = None
    request_id: str | None = None


def _new_produce_effect_wait(before: RuntimeOuterSnapshot, after: RuntimeOuterSnapshot) -> bool:
    """Hand ownership to a new real local effect acknowledgement, not a RPC.

    Customize End can resolve rewards on the same page before it can close.
    Waiting for a page change there would prevent the required acknowledgement.
    A precise new native waiter is an input boundary, not a server-ACK claim.
    """
    ui = after.raw.get("ui_state") or {}
    wait = ui.get("effect_confirmation") or {}
    previous = (before.raw.get("ui_state") or {}).get("effect_confirmation") or {}
    actions = after.actions
    if (before.raw.get("screen_type") != "ScheduleCustomizeScreenPresenter"
            or after.raw.get("screen_type") != "ScheduleCustomizeScreenPresenter"
            or ui.get("phase") != "await_effect_acknowledgement" or wait.get("ready") is not True
            or any(wait.get(key) is not True for key in ("active", "enabled", "has_callback"))
            or wait.get("disabled") is not False
            or len(actions) != 1 or actions[0].get("action_id") != "effect.advance"):
        return False
    target = actions[0]["target"]
    callback = wait.get("wait_callback_instance_id")
    if (not isinstance(callback, str) or callback in {"", "0", "0x0"}
            or callback == previous.get("wait_callback_instance_id")):
        return False
    state = after.raw.get("state") or {}
    old_state = before.raw.get("state") or {}
    return (target.get("button_source") == "produce-screen-touch"
            and after.raw.get("screen_instance_id") == before.raw.get("screen_instance_id")
            and target.get("presenter_type") == after.raw.get("screen_type")
            and target.get("screen_instance_id") == after.raw.get("screen_instance_id")
            and isinstance(wait.get("button_instance_id"), str)
            and wait["button_instance_id"] not in {"", "0", "0x0"}
            and target.get("button_instance_id") == wait["button_instance_id"]
            and target.get("wait_callback_instance_id") == callback
            and all(key in state and target.get(key) == state[key] == old_state.get(key)
                    for key in ("produce_id", "week", "step_type")))


def _memory_creation_advanced(after: RuntimeOuterSnapshot, target: Mapping[str, object]) -> bool:
    """The dialog closing/UUID allocation precede the game's async creation.

    Only the same result presenter reaching its normal post-await stage releases
    the operation owner. The next animation button must remain free to execute.
    """
    raw, state = after.raw, after.raw.get("ui_state") or {}
    if raw.get("screen_type") != "ProduceResultScreenPresenter" or raw.get("surface") != "produce_result":
        return False
    try:
        parent, screen = target.get("parent_instance_id"), raw.get("screen_instance_id")
        if not isinstance(parent, str) or not isinstance(screen, str):
            return False
        identity = int(parent, 16 if parent.startswith("0x") else 10)
        if not 0 < identity < 2 ** 64 or identity != int(screen, 16 if screen.startswith("0x") else 10):
            return False
    except ValueError:
        return False
    assigned = state.get("created_memory_id")
    if not isinstance(assigned, str) or not assigned:
        return False
    stages = state.get("stages") or {}
    return (any(stages.get(key) is True for key in ("memory_creation", "memory_detail", "selection_memory_detail"))
            or any(a.get("action_id") in {"result.start_memory_animation", "result.continue_memory",
                                          "result.continue_selection_memory"} for a in after.actions))


class RuntimeOuterGateway(RuntimeOuterReader):
    def __init__(self, client: RuntimeCommandClient | None = None, *, timeout=30.0,
                 cancelled: Callable[[], bool] | None = None,
                 monotonic=time.monotonic, sleep=time.sleep, lease=_controller_lease):
        self.client = client or RuntimeCommandClient()
        self.timeout, self.cancelled = timeout, cancelled
        self.monotonic, self.sleep, self.lease = monotonic, sleep, lease

    def read(self) -> RuntimeOuterSnapshot:
        # This ordinary ADV component can briefly remain shown while the
        # game changes screens. Retry only its rejected observation; actual
        # persistent choices still stop with the original error at the deadline.
        deadline = self.monotonic() + self.timeout
        last_rejection = None
        while True:
            if self.cancelled is not None and self.cancelled():
                raise RuntimeCommandError("cancelled while reading native Outer state")
            remaining = deadline - self.monotonic()
            if last_rejection is not None and remaining <= 0:
                raise last_rejection
            if self.timeout > 0 and remaining <= 0:
                raise RuntimeCommandError("native Outer read deadline expired before another request")
            reader = RuntimeOuterReader(self.client, timeout=max(0.0, remaining),
                cancelled=self.cancelled, monotonic=self.monotonic, sleep=self.sleep)
            try:
                return reader.read()
            except RuntimeCommandError as error:
                if (type(error) is not RuntimeCommandError or str(error) !=
                        "command-rejected: visible native ADV choices require their own choice contract"
                        or self.timeout <= 0 or self.monotonic() >= deadline):
                    raise
                last_rejection = error
                if self.cancelled is not None and self.cancelled():
                    raise RuntimeCommandError("cancelled while reading native Outer state") from error
                self.sleep(min(0.25, max(0.0, deadline - self.monotonic())))

    @property
    def pending_path(self):
        return self.client.root / "pending_outer.json"

    def execute(self, before: RuntimeOuterSnapshot, target: Mapping[str, object]) -> RuntimeOuterOutcome:
        with self.lease(self.timeout):
            if self.cancelled is not None and self.cancelled() and not self.pending_path.exists():
                return RuntimeOuterOutcome("rejected", "cancelled before Outer input")
            if self.pending_path.exists():
                return self.resume()
            if not any(dict(action["target"]) == dict(target) for action in before.actions):
                return RuntimeOuterOutcome("rejected", "chosen target is outside the native legal set")
            if target.get("action_id") == "result.confirm_create_memory" and (
                    before.raw.get("screen_type") != "MemoryCreateConfirmSheetPresenter"
                    or not isinstance(target.get("parent_instance_id"), str) or not target["parent_instance_id"]
                    or (before.raw.get("ui_state") or {}).get("parent_instance_id") != target["parent_instance_id"]):
                return RuntimeOuterOutcome("rejected", "memory creation requires its bound native result presenter")
            latest = self.read()
            if latest.session_generation != before.session_generation:
                return RuntimeOuterOutcome("rejected", "game process changed before Outer input")
            if latest.revision != before.revision:
                return RuntimeOuterOutcome("replan", "native Outer state advanced", latest)
            request = self.client.make_request("outer.action", expected_revision=before.revision, target=target)
            if request.session_generation != before.session_generation:
                return RuntimeOuterOutcome("rejected", "game process changed before request binding")
            pending = {"schema": "gkms.runtime-pending-outer.v1", "request": request.to_dict(),
                       "before": dict(before.raw), "source_path": str(before.source_path)}
            atomic_write(self.pending_path, canonical_json_bytes(pending))
            if self.cancelled is not None and self.cancelled():
                self._retire(pending, "cancelled-before-submit")
                return RuntimeOuterOutcome("rejected", "cancelled before Outer input")
            try:
                self.client.submit(request)
            except RuntimeCommandPending:
                return RuntimeOuterOutcome("pending", "Outer publication outcome is unknown", request_id=request.request_id)
            except RuntimeCommandError as exc:
                self._retire(pending, "rejected")
                return RuntimeOuterOutcome("rejected", str(exc), request_id=request.request_id)
            return self._settle(pending)

    def _retire(self, pending, outcome):
        path = self.client.root / "completed_outer" / f"{pending['request']['request_id']}.json"
        atomic_write(path, canonical_json_bytes({**pending, "outcome": outcome}))
        raw = pending["request"]
        release = getattr(self.client, "release_action", None)
        if callable(release):
            release(RuntimeCommandRequest(raw["request_id"], raw["session_generation"], raw["command"],
                                          raw.get("expected_revision"), raw.get("target")))
        self.pending_path.unlink(missing_ok=True)

    def resume(self) -> RuntimeOuterOutcome:
        with self.lease(self.timeout):
            if not self.pending_path.exists():
                return RuntimeOuterOutcome("replan", "no pending native Outer operation", self.read())
            pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
            outcome = self._settle(pending)
            if outcome.status == "settled":
                return RuntimeOuterOutcome("replan", "previous native Outer operation reconciled",
                                           outcome.snapshot, outcome.request_id)
            return outcome

    def _settle(self, pending) -> RuntimeOuterOutcome:
        if pending.get("schema") != "gkms.runtime-pending-outer.v1":
            raise RuntimeCommandProtocolError("native Outer journal schema differs")
        raw = pending["request"]
        request = RuntimeCommandRequest(raw["request_id"], raw["session_generation"], raw["command"],
                                        raw.get("expected_revision"), raw.get("target"))
        before = RuntimeOuterSnapshot(request.session_generation, pending["before"], Path(pending["source_path"]))
        deadline = self.monotonic() + self.timeout
        next_read = 0.0
        submitted = False
        while True:
            if self.client.read_status()["session_generation"] != request.session_generation:
                return RuntimeOuterOutcome("pending", "prior game session needs reconciliation", request_id=request.request_id)
            receipt = self.client.poll_result(request)
            if receipt is not None:
                if receipt.status == "rejected":
                    self._retire(pending, "rejected")
                    if receipt.raw.get("error_message") == "stale native outer revision":
                        return RuntimeOuterOutcome("replan", "native state moved before input; observe again",
                                                   request_id=request.request_id)
                    return RuntimeOuterOutcome("rejected", str(receipt.raw.get("error_message", receipt.raw.get("error_code", "rejected"))), request_id=request.request_id)
                submitted = submitted or receipt.status == "submitted"
            now = self.monotonic()
            if submitted and now >= next_read:
                next_read = now + 0.25
                try:
                    after = self.read()
                    if after.session_generation != before.session_generation:
                        return RuntimeOuterOutcome("pending", "game process changed during Outer action", request_id=request.request_id)
                    if after.raw.get("surface") == "error":
                        error = (after.raw.get("ui_state") or {}).get("error") or {}
                        return RuntimeOuterOutcome("pending", "native game error during operation: " +
                            str(error.get("description", "unconfirmed transaction")), after, request.request_id)
                    # Busy/animation flags alone are not a completed action.
                    changed = any(after.raw.get(key) != before.raw.get(key) for key in
                                  ("screen_type", "screen_instance_id", "progress", "collections", "ui_state", "selection"))
                    target = request.target or {}
                    reward_receipt = None
                    replay_receipt = None
                    live_story_receipt = None
                    loading_receipt = None
                    is_direct_replay = target.get("action_id") in {
                        "replay.direct.open", "replay.direct.start", "replay.direct.exit", "replay.direct.home"}
                    if is_direct_replay:
                        from .runtime_direct_replay_receipt import direct_replay_receipt
                        replay_receipt = direct_replay_receipt(before.raw, after.raw, target)
                        changed = replay_receipt is not None
                    if target.get("action_id") == "reward.receive":
                        from .runtime_reward_receipt import drink_reward_receipt
                        reward_receipt = drink_reward_receipt(before.raw, after.raw, target)
                    if target.get("action_id") == "produce.choose_idol":
                        changed = (after.raw.get("selection") or {}).get("idol_card_id") == target.get("idol_card_id")
                    elif target.get("action_id") == "result.confirm_create_memory":
                        changed = _memory_creation_advanced(after, target)
                    elif target.get("action_id") == "ui.navigation" and target.get("button_id") == "login.continue":
                        from .runtime_login_touch import login_touch_advanced
                        changed = login_touch_advanced(before.raw, after.raw, target)
                    elif target.get("action_id") == "notice.dismiss_startup_image":
                        from .runtime_startup_notice import startup_notice_advanced
                        changed = startup_notice_advanced(before.raw, after.raw, target)
                    elif before.raw.get("screen_type") == "LiveScenePresenter" and target.get("button_source") == "player-skip-only":
                        from .runtime_presentation import native_live_story_receipt
                        live_story_receipt = native_live_story_receipt(before.raw, after.raw, target)
                        changed = live_story_receipt is not None
                    elif target.get('action_id') == 'live.loading_continue':
                        from .runtime_live_loading import loading_press_receipt
                        loading_receipt = loading_press_receipt(before.raw, after.raw, target)
                        if loading_receipt is not None and loading_receipt['status'] == 'failed':
                            self._retire(pending, {'status': 'failed', 'after': dict(after.raw),
                                                  'completion_evidence': loading_receipt})
                            return RuntimeOuterOutcome('failed', loading_receipt['completion'], after, request.request_id)
                        changed = loading_receipt is not None
                    elif target.get("action_id") in {"loadout.memory_auto", "loadout.memory_auto_confirm"}:
                        from .runtime_memory_autoselect import memory_auto_action_advanced, memory_auto_terminal_failure
                        failed = memory_auto_terminal_failure(after.raw, target)
                        if failed is not None:
                            self._retire(pending, {"status": "failed", "after": dict(after.raw),
                                "native_memory_auto": dict(failed), "task_consumed": True,
                                "gameplay_success_claimed": False})
                            return RuntimeOuterOutcome("failed", "native memory auto-selection task failed: " +
                                str(failed.get("failure", "native task unsuccessful")), after, request.request_id)
                        changed = memory_auto_action_advanced(before.raw, after.raw, target)
                    elif target.get("action_id") == "reward.open_group":
                        from .runtime_reward_group import reward_group_advanced
                        changed = reward_group_advanced(before.raw, after.raw, target)
                    elif target.get("action_id") in {"schedule.choose", "customize.finish"}:
                        # Selecting a schedule changes Decided before its async
                        # entry completes. Customize also issues a separate
                        # Start request while its screen is opening. Likewise,
                        # the End response can update progress before the old
                        # Customize screen has finished closing. Neither local
                        # flags nor that same-screen update release this owner.
                        # Native input readiness must still guard the new page;
                        # a page transition alone is not a server-ACK claim.
                        changed = after.raw.get("screen_type") != before.raw.get("screen_type")
                        if target.get("action_id") == "customize.finish":
                            changed = changed or _new_produce_effect_wait(before, after)
                    elif target.get("action_id") == "shop.confirm_operation":
                        # Closing the confirmation only starts its async buy.
                        # Keep this operation pending until the observed wallet,
                        # owned cards or native shop ledger actually changes.
                        old_points = (before.raw.get("state") or {}).get("produce_points")
                        new_points = (after.raw.get("state") or {}).get("produce_points")
                        changed = type(old_points) is int and type(new_points) is int and old_points != new_points
                        for field in ("cards", "shop"):
                            old = (before.raw.get("collections") or {}).get(field)
                            new = (after.raw.get("collections") or {}).get(field)
                            changed = changed or isinstance(old, list) and isinstance(new, list) and old != new
                    if (not after.raw["busy"] and changed) or reward_receipt is not None or replay_receipt is not None or live_story_receipt is not None or loading_receipt is not None:
                        completed = {"status": "settled", "after": dict(after.raw)}
                        if reward_receipt is not None:
                            completed["completion_evidence"] = reward_receipt
                        if replay_receipt is not None:
                            completed["completion_evidence"] = replay_receipt
                        if live_story_receipt is not None:
                            completed["completion_evidence"] = live_story_receipt
                        if loading_receipt is not None:
                            completed['completion_evidence'] = loading_receipt
                        self._retire(pending, completed)
                        detail = ("native drink receipt confirmed; presentation readiness remains separate"
                                  if reward_receipt is not None else "normal game callback and native state changed")
                        if replay_receipt is not None:
                            detail = replay_receipt["completion"]
                        return RuntimeOuterOutcome("settled", detail, after, request.request_id)
                except RuntimeCommandError as error:
                    # read() already handles ordinary scene transitions.
                    # Preserve the pending command, but expose a persistent
                    # reader failure instead of hiding it behind a timeout.
                    return RuntimeOuterOutcome("pending", f"native Outer completion read failed: {error}",
                                               request_id=request.request_id)
            if self.cancelled is not None and self.cancelled():
                return RuntimeOuterOutcome("pending", "cancelled while Outer action is pending", request_id=request.request_id)
            if now >= deadline:
                return RuntimeOuterOutcome("pending", "waiting for native Outer completion", request_id=request.request_id)
            self.sleep(min(0.05, max(0.0, deadline - now)))


def choose_runtime_outer_action(
    native: RuntimeOuterSnapshot, *, produce_id: str, idol_card_id: str,
    audition_strategy: str = "highest_available",
    deck_plan_owner=None,
    operation_context=None,
    weekly_bc_provider_path=None,
) -> tuple[Mapping[str, object] | None, Mapping[str, object]]:
    """Choose from native candidates using current state and mode rules."""
    from .runtime_presentation import native_presentation_wait
    presentation = native_presentation_wait(native, produce_id=produce_id, idol_card_id=idol_card_id)
    if presentation is not None:
        return None, presentation
    if native.raw.get("surface") == "presentation":
        from .runtime_live_loading import loading_target
        loading = loading_target(native.raw)
        if loading is not None:
            return loading, {'source': 'native-LiveLoading-WaitPress',
                             'reason': '按遊戲目前等待中的演出開始按鈕'}
        # The shared validator admitted the currently owned skip-only callback.
        return native.actions[0]["target"], {"source": "native-owned-live-story",
            "reason": "使用目前 Live 前後劇情的遊戲原生略過按鈕"}
    actions = native.actions
    if native.raw["screen_type"] == "ProduceMemoryDeckAutoSetConfirmSheetPresenter":
        choices = [a for a in actions if a["action_id"] == "loadout.memory_auto_confirm"]
        if len(choices) > 1:
            raise RuntimeCommandError("native memory auto-selection confirmation is ambiguous")
        if not choices:
            return None, {"source": "native-memory-auto-selection", "status": "waiting",
                "reason": "等待遊戲自動編成確認按鈕就緒"}
        target = choices[0]["target"]
        if target.get("produce_id") != produce_id or target.get("idol_card_id") != idol_card_id:
            raise RuntimeCommandError("native memory auto-selection belongs to a different preparation")
        return target, {"source": "native-memory-auto-selection", "reason": "確認遊戲內自動推薦；保留遊戲目前的租借選项"}
    if native.raw["screen_type"] == "StartupImageOverlayPresenter":
        from .runtime_startup_notice import DISMISS_ACTION, startup_notice_target
        notices = [a for a in actions if a["action_id"] == DISMISS_ACTION]
        if len(notices) > 1 or any(not startup_notice_target(native.raw, a["target"]) for a in notices):
            raise RuntimeCommandError("startup advertisement close has an ambiguous or changed native owner")
        if not notices:
            return None, {"source": "native-startup-advertisement", "status": "waiting",
                "reason": "等待廣告／公告頁的原生關閉按鈕就緒"}
        return notices[0]["target"], {"source": "native-startup-advertisement",
            "reason": "關閉目前廣告／公告頁", "changes_settings": False}
    if native.raw["screen_type"] == "DearnessLevelUpReceiveOverlayPresenter":
        notices = [a for a in actions if a["action_id"] == "notice.dearness_advance"]
        if len(notices) > 1:
            raise RuntimeCommandError("dearness notice has ambiguous native confirmation targets")
        if not notices:
            return None, {"source": "native-dearness-notice", "status": "waiting",
                          "reason": "等待親密度提升通知的原生確認按鈕"}
        return notices[0]["target"], {"source": "native-dearness-notice",
                                     "reason": "確認親密度提升通知"}
    if native.raw["screen_type"] == "ProduceRefreshConfirmSheetPresenter":
        # ScheduleScreenPresenter.RefreshRequestAsync opens this exact sheet
        # with the actual recovery/limit preview. Confirm its normal button;
        # no week/archetype-specific navigation or resource mutation is needed.
        ui = native.raw.get("ui_state") or {}
        if (native.raw.get("underlying_screen_type") != "ScheduleScreenPresenter"
                or (native.raw.get("state") or {}).get("in_progress") is not True):
            raise RuntimeCommandError("refresh confirmation does not belong to an active schedule")
        matches = [a for a in actions if a["action_id"] == "ui.sheet_confirm"]
        if len(matches) > 1:
            raise RuntimeCommandError("refresh confirmation has ambiguous normal buttons")
        if (not matches or native.raw.get("busy") is not False or native.raw.get("actions_complete") is not True
                or ui.get("closing") is not False or ui.get("disabled") is not False):
            return None, {"source": "native-refresh-confirmation", "status": "waiting",
                          "reason": "等待原生休息確認按鈕就緒"}
        return matches[0]["target"], {"source": "native-refresh-confirmation", "reason": "確認已選擇的休息行動"}
    from .runtime_login_touch import waiting_for_login_touch
    if waiting_for_login_touch(native.raw):
        return None, {"source": "native-login-touch", "status": "waiting",
                      "reason": "等待登入獎勵頁的下一個原生點擊階段"}
    if native.raw["screen_type"] in {"AuditionBattleResultScreenPresenter", "AuditionBattleResultHifScreenPresenter"}:
        state = native.raw.get("ui_state") or {}
        if native.raw["surface"] != "audition_result" or type(state.get("is_win")) is not bool:
            raise RuntimeCommandError("native audition result outcome is unavailable; no automatic continuation")
        if state["is_win"]:
            name = "audition.continue_after_win"
        else:
            remaining = state.get("remaining_count")
            number = state.get("selected_number")
            if type(remaining) is not int or remaining <= 0:
                raise RuntimeCommandError("native audition failed: no retries remain; preserve failure screen")
            if type(number) is not int or number <= 1:
                raise RuntimeCommandError("native audition failed: no lower difficulty; preserve failure screen")
            if state.get("can_retry") is not True:
                raise RuntimeCommandError("native audition failed: retry unavailable; preserve failure screen")
            name = "audition.open_retry_result"
        # The normal GoNext may already have completed this result, while
        # ProduceScene is waiting for its effect touch acknowledgement. The
        # native adapter deliberately replaces GoNext with that exact callback.
        # Preserve loss/retry guards above, then service the actual foreground
        # owner instead of waiting for a second GoNext that will never appear.
        if state.get("phase") == "await_effect_acknowledgement":
            acknowledgements = [action for action in actions if action["action_id"] == "effect.advance"
                and action["target"].get("button_source") == "produce-screen-touch"]
            if len(acknowledgements) > 1:
                raise RuntimeCommandError("native audition result exposes ambiguous effect acknowledgements")
            ready = (state.get("effect_confirmation") or {}).get("ready") is True
            if not acknowledgements or not ready:
                return None, {"source": "native-audition-result-effect", "status": "waiting",
                    "reason": "waiting for the current result effect acknowledgement"}
            return acknowledgements[0]["target"], {"source": "native-audition-result-effect",
                "is_win": state["is_win"], "is_complete": state.get("is_complete"),
                "reason": "acknowledge the current normal result effect wait"}
        candidates = [action for action in actions if action["action_id"] == name]
        if len(candidates) > 1:
            raise RuntimeCommandError("native audition result action is ambiguous")
        detail = {"source": "native-audition-result", "is_win": state["is_win"],
                  "remaining_count": state.get("remaining_count"), "selected_number": state.get("selected_number")}
        if not candidates:
            return None, {**detail, "status": "waiting", "reason": "waiting for the normal result control"}
        return candidates[0]["target"], detail
    if native.raw["surface"] == "error":
        returns = [action for action in actions if action["action_id"] == "error.return_title"]
        if len(returns) != 1:
            raise RuntimeCommandError("native error has no unique verified return-to-title action")
        return returns[0]["target"], {"source": "native-error-return-title",
            "reason": "依目前錯誤視窗的正常按鈕返回標題；不重送原操作"}
    if native.raw["surface"] == "audition_retry":
        # Retry binds the observed free quota or an owned one-ticket quote.
        # Never fall through to a generic sheet confirmation or purchase.
        from .runtime_outer_policy import choose

        retry = choose(native, produce_id=produce_id, idol_card_id=idol_card_id, audition_strategy=audition_strategy)
        if retry.target is None:
            if retry.metadata.get("status") == "waiting":
                return None, retry.metadata
            raise RuntimeCommandError("native retry policy gap: " + str(retry.metadata.get("reason", "no bounded retry")))
        if not any(dict(action["target"]) == dict(retry.target) for action in actions):
            raise RuntimeCommandError("native retry policy returned a target outside the current legal set")
        return retry.target, retry.metadata
    if not actions:
        raise RuntimeCommandError("current native screen has no complete legal candidates")
    foreground = [a for a in actions if a["action_id"] in {"effect.advance", "effect.confirm_card_change"}]
    if len(foreground) == 1:
        return foreground[0]["target"], {"source": "native-foreground-effect"}
    if len(foreground) > 1:
        raise RuntimeCommandError("native effect owner exposes ambiguous confirmation targets")
    from .runtime_card_choice_policy import choose_runtime_card_ui_action
    from .runtime_outer_policy import build_runtime_outer_context, choose

    def bind_deck_plan(context):
        if deck_plan_owner is not None:
            context = deck_plan_owner.bind(context, (native.raw.get("collections") or {}).get("cards"),
                boundary={"revision": native.revision, "surface": native.raw["surface"],
                          "schedule_number": (native.raw.get("state") or {}).get("schedule_number")})
        return context

    def shared_deck_context():
        return bind_deck_plan(build_runtime_outer_context(native, produce_id=produce_id,
            idol_card_id=idol_card_id, audition_strategy=audition_strategy))

    if native.raw["surface"] in {"shop", "interval"} or (
            native.raw["screen_type"] == "ScheduleShopConfirmSheetPresenter"
            and native.raw.get("underlying_screen_type") == "ScheduleIntervalScreenPresenter"):
        from .runtime_economy_policy import choose_runtime_economy_action
        from .outer_training_budget import build_training_budget

        def economy_deck_context():
            context = shared_deck_context()
            return {**context, "training_budget": build_training_budget(native.raw, context)}

        target, detail = choose_runtime_economy_action(native, deck_context=economy_deck_context,
                                                     operation_context=operation_context)
        if target is None and detail.get("status") not in {"waiting"}:
            raise RuntimeCommandError("native economy policy gap: " + str(detail.get("reason")))
        return target, detail

    if native.raw["surface"] == "drink_inventory":
        from .runtime_drink_choice import choose_runtime_drink_inventory_action

        return choose_runtime_drink_inventory_action(native,
            deck_context=shared_deck_context)

    if native.raw["surface"] == "reward_drink_capacity":
        from .runtime_reward_drink_capacity import choose_runtime_reward_drink_capacity_action
        capacity = choose_runtime_reward_drink_capacity_action(native, deck_context=shared_deck_context)
        if capacity is None:
            raise RuntimeCommandError("native reward drink capacity adapter is unavailable")
        return capacity

    if native.raw["surface"] == "customize_entry_confirmation":
        if native.raw["screen_type"] != "ProduceCustomizeConfirmSheetPresenter":
            raise RuntimeCommandError("native customize entry confirmation owner changed")
        confirm = [a for a in actions if a["action_id"] == "customize_entry.confirm"]
        if len(confirm) > 1:
            raise RuntimeCommandError("native customize entry confirmation is ambiguous")
        if not confirm:
            return None, {"source": "native-customize-entry", "status": "waiting", "reason": "等待自訂前提示的正常確認按鈕"}
        return confirm[0]["target"], {"source": "native-customize-entry",
            "reason": "確認進入已選擇的自訂週次，保留目前提示設定"}

    card_choice = choose_runtime_card_ui_action(
        native,
        deck_context=shared_deck_context,
    )
    if card_choice is not None:
        return card_choice
    selection = native.raw.get("selection")
    selection = selection if isinstance(selection, Mapping) else {}
    if native.raw["screen_type"] == "StartExchangeItemExpireSheetPresenter":
        close = [a for a in actions if a["action_id"] == "ui.sheet_cancel"
                 and a["target"].get("button_source") == "expiry-notice"]
        if len(close) == 1:
            return close[0]["target"], {"source": "close-expiry-information"}
    if native.raw["screen_type"] == "ProduceMemorySelectRentalEnableConfirmSheetPresenter":
        proceed = [a for a in actions if a["action_id"] == "loadout.memory_continue_without_rental"]
        if len(proceed) == 1:
            return proceed[0]["target"], {"source": "normal-game-continue-with-owned-memories"}
    resumes = [a for a in actions if a["action_id"] == "produce.resume"]
    if resumes:
        if len(resumes) != 1 or any(resumes[0]["target"].get(key) != value for key, value in
                                    (("produce_id", produce_id), ("idol_card_id", idol_card_id))):
            raise RuntimeCommandError("native resume target differs from the requested cultivation")
        return resumes[0]["target"], {"source": "resume-current-native-cultivation"}
    # Preserve real event choices. Story controls only advance text/effect
    # acknowledgements when the game is not offering a decision to the policy.
    if not any(a["action_id"] == "event.choose" for a in actions):
        for action_id in ("customize.confirm_execute", "customize.confirm_finish", "effect.confirm_card_change", "effect.advance", "story.confirm_skip", "story.skip", "story.advance",
                          "event.skip_story", "event.advance", "event.confirm_choice", "produce.confirm_settings"):
            controls = [a for a in actions if a["action_id"] == action_id]
            if len(controls) == 1:
                return controls[0]["target"], {"source": "normal-game-lifecycle-control"}
            if len(controls) > 1:
                raise RuntimeCommandError("native lifecycle control is ambiguous: " + action_id)
    if native.raw["surface"] == "produce_result":
        cancel = [a for a in actions if a["action_id"] == "result.cancel_create_memory"]
        if cancel:
            if len(cancel) != 1 or native.raw.get("screen_type") != "MemoryCreateConfirmSheetPresenter" or (
                    native.raw.get("ui_state") or {}).get("stage") != "memory_already_created":
                raise RuntimeCommandError("native memory re-entry cancellation is ambiguous")
            return cancel[0]["target"], {"source": "normal-result-reentry-cancellation",
                "reason": "回憶建立已開始，關閉重複確認並等待原本流程完成"}
        completion = [a for a in actions if a["action_id"] in {
            "result.confirm_selection", "result.confirm_create_memory", "result.start_memory_animation",
            "result.continue_memory", "result.continue_selection_memory", "result.continue_rewards",
            "result.continue_achievements", "result.acknowledge_new_record",
            "result.acknowledge_high_score_ranking", "result.acknowledge_high_score_reward",
            "result.keep_easy_mode_setting",
        }]
        if len(completion) == 1:
            reason = ("保留目前簡單模式設定並關閉詢問" if completion[0]["action_id"] == "result.keep_easy_mode_setting"
                      else "保留目前選擇並完成培育結果")
            return completion[0]["target"], {"source": "normal-result-completion", "reason": reason}
        if len(completion) > 1:
            raise RuntimeCommandError("native result stage exposes ambiguous completion controls")
        photos = [a for a in actions if a["action_id"] in {"result.select_photo", "result.reveal_photo"}]
        if photos:
            current = native.raw.get("ui_state", {}).get("current_photo_guid")
            preferred = [a for a in photos if a["target"].get("photo_guid") == current]
            chosen = min(preferred or photos, key=lambda a: a["target"]["index"])
            return chosen["target"], {"source": "normal-result-photo-selection", "reason": "使用目前照片或第一個實際可用的照片"}
    mode = [a for a in actions if a["action_id"] == "produce.choose_mode"
            and a["target"].get("produce_id") == produce_id]
    if mode:
        if len(mode) != 1:
            raise RuntimeCommandError("requested produce mode is ambiguous")
        return mode[0]["target"], {"source": "requested-mode", "produce_id": produce_id}
    if native.raw["screen_type"] == "ProduceIdolSelectScreenPresenter" and selection.get("idol_card_id") != idol_card_id:
        idols = [a for a in actions if a["action_id"] == "produce.choose_idol"
                 and a["target"].get("idol_card_id") == idol_card_id]
        if len(idols) != 1:
            raise RuntimeCommandError("requested owned idol is not in the native candidate set")
        return idols[0]["target"], {"source": "requested-idol", "idol_card_id": idol_card_id}
    if native.raw["surface"] in {"event", "schedule", "business", "audition_select"}:
        decision = choose(native, produce_id=produce_id, idol_card_id=idol_card_id,
                          audition_strategy=audition_strategy, context_transform=bind_deck_plan,
                          weekly_bc_provider_path=weekly_bc_provider_path)
        if decision.target is None:
            if decision.metadata.get("status") == "waiting":
                return None, decision.metadata
            raise RuntimeCommandError("native policy gap: " + str(decision.metadata.get("reason", "no exact action")))
        if not any(dict(action["target"]) == dict(decision.target) for action in actions):
            raise RuntimeCommandError("native policy returned a target outside the current legal set")
        return decision.target, decision.metadata
    # These are the existing fixed navigation steps; selector arrows and
    # difficulty toggles are intentionally excluded in favor of exact IDs.
    navigation = [a for a in actions if a["action_id"] == "ui.navigation" and a["target"].get("button_id") in {
        "title.start", "login.continue", "home.produce", "produce.idol_continue", "produce.support_continue",
        "produce.memory_continue", "produce.start", "exam.result_continue",
        "lesson.result_continue", "audition.start", "audition.result_continue", "produce.result_finish",
        "produce.evaluation_continue", "reward.continue", "audition.unlock_continue",
    }]
    if len(navigation) == 1:
        target = navigation[0]["target"]
        if target.get("button_id") in {"produce.idol_continue", "produce.support_continue", "produce.memory_continue", "produce.start"}:
            if selection.get("produce_id") != produce_id or selection.get("idol_card_id") != idol_card_id:
                raise RuntimeCommandError("native selection differs from requested mode/idol")
        return target, {"source": "normal-game-navigation"}
    raise RuntimeCommandError("native policy adapter missing for " + native.raw["screen_type"])


def _finalize_native_completed_evidence(result, *, run_id, produce_id, idol_card_id, game_root):
    """Use the canonical writer without invoking obsolete live/sidecar readers."""
    from .completed_run_evidence import finalize_completed_run_evidence
    from .exam_execution_policy import ExamExecutionMode
    from .nia_live_unattended import NiaLiveUnattendedRequest
    from .outer_bc_shadow import FULL_CULTIVATION_SHADOW_SCHEMA
    from .run_identity import load_run

    return finalize_completed_run_evidence(result, run=load_run(run_id),
        request=NiaLiveUnattendedRequest(produce_id=produce_id, idol_card_id=idol_card_id,
            game_root=game_root, expected_run_id=run_id, exam_execution_mode=ExamExecutionMode.EXACT),
        strategy_finalizer=lambda *args, **kwargs: {"status": "not-used-native", "terminal_written": False},
        exact_completion_flusher=lambda *args, **kwargs: {"status": "not-used-native", "complete": False},
        shadow_builder=lambda *args, **kwargs: {"schema": FULL_CULTIVATION_SHADOW_SCHEMA,
            "run_id": run_id, "diagnostic_only": True, "applied": False, "formal_result_unchanged": True,
            "completeness": {"outer_complete": False, "exact_complete": False, "complete": False},
            "blockers": ["native-stage-validation-pending"]})


def _fullpower_trial_model_route(policy, required_stages):
    """Verify declared trial coverage; this is not production-stage validation."""
    aliases = {"Mid1": "Mid1", "Mid2": "Mid2", "Final": "Final",
        "ProduceStepType_AuditionMid1": "Mid1", "ProduceStepType_AuditionMid2": "Mid2",
        "ProduceStepType_AuditionFinal": "Final"}
    try:
        required = tuple(aliases[stage] for stage in required_stages)
        declared = tuple(aliases[stage] for stage in policy.stages)
    except (KeyError, TypeError, AttributeError) as error:
        raise RuntimeCommandError("FullPower trial stage scope is invalid") from error
    if not required or not set(required).issubset(declared):
        raise RuntimeCommandError("FullPower trial does not cover this mode's required audition stages")
    sources = policy.artifact_sources
    if (not isinstance(sources, Mapping) or sources.get("role") != "fullpower_current_state_trial"
            or sources.get("experimental") is not True or sources.get("production_validated") is not False):
        raise RuntimeCommandError("FullPower trial has no verified experimental artifact identity")
    return {**dict(policy.model_route), "experimental": True, "production_validated": False,
        "all_stages_validated": False, "stage_scope_complete": True,
        "required_stages": list(required), "trial_stages": list(declared), "artifact_sources": dict(sources)}


def _preflight_start_recorder(client, session_generation):
    """Require this process's actual action recorder before new AP spending."""
    from .runtime_live_recorder import require_live_action_recorder
    native_status = client.read_status()
    if native_status.get('session_generation') != session_generation:
        raise RuntimeCommandError('Native generation changed before action-recorder preflight')
    require_live_action_recorder(native_status)
    return native_status


def _preflight_native_model_mode(produce_id, idol_card_id, plan_type, bundle_path=None, *, native_capabilities=None,
                                 exam_policy_variant=None, model_context=None):
    """Check the selected model before the normal Start button can spend AP."""
    from .master_db import get_idol_profile
    from .policy_bundle import PolicyBundle
    from .policy_mode_coverage import resolve_exam_policy_mode_route
    from .runtime_mode_profile import load_runtime_mode_profile

    idol = get_idol_profile(idol_card_id)
    if idol is None or idol.plan_type != plan_type:
        raise RuntimeCommandError("selected idol/Plan does not match current Master")
    if exam_policy_variant is not None:
        from .gui_exam_models import verify_gui_exam_model_artifacts
        descriptor = verify_gui_exam_model_artifacts(exam_policy_variant)
        from .local_flow_trial import resolve_live_flow_scope
        try:
            resolve_live_flow_scope(variant_id=exam_policy_variant, produce_id=produce_id,
                idol_card_id=idol_card_id, plan_type=plan_type, exam_effect_type=idol.exam_effect_type)
        except ValueError as error:
            raise RuntimeCommandError(str(error)) from error
        if 'exam.model_observation.v1' not in (native_capabilities or ()):
            raise RuntimeCommandError('Selected BC model requires exam.model_observation.v1 before AP use')
        if not descriptor.get('available'):
            raise RuntimeCommandError(descriptor.get('reason') or 'Selected model artifact unavailable')
        if not isinstance(model_context, Mapping) or model_context.get('schema')!='gkms.live-exam-model-preflight.v1':
            raise RuntimeCommandError('Current Master qualification pending; read_model_context required before AP use')
        from .live_pc_consumer_contract import validate_live_pc_consumer_identity
        engine = model_context.get('engine_identity') or {}
        try:
            validate_live_pc_consumer_identity(engine)
        except ValueError as error:
            raise RuntimeCommandError('Current native model consumer differs from the verified PC version: ' + str(error)) from error
        if 'exam.continuation' not in (native_capabilities or ()):
            raise RuntimeCommandError('Selected model requires native secondary continuation before AP use')
        from .runtime_gui_exam_policy import build_live_exam_policy
        # This temporary owner performs no input and is not a cultivation run.
        # The real run captures its actual identity after native start succeeds.
        policy = build_live_exam_policy(exam_policy_variant,produce_id=produce_id,idol_card_id=idol_card_id,
            run_id='preflight:'+produce_id+':'+idol_card_id)
        route = policy.preflight_model_context(model_context)
        return {**route,'scope':'before-AP-artifact-and-current-Master-check','cultivation_started':False}
    if (native_capabilities is not None
            and (produce_id == "produce-004" or plan_type in {"ProducePlanType_Plan1", "ProducePlanType_Plan2"})
            and "exam.native_legal_inputs" not in native_capabilities):
        raise RuntimeCommandError("native primary policy requires exam.native_legal_inputs before AP use")
    if native_capabilities is not None and "exam.continuation" not in native_capabilities:
        raise RuntimeCommandError("native primary policy requires exam.continuation before AP use")
    mode = load_runtime_mode_profile(produce_id)
    if (produce_id == "produce-004" and plan_type == "ProducePlanType_Plan3"
            and idol.exam_effect_type == "ProduceExamEffectType_ExamFullPower"):
        if "exam.native_legal_inputs" not in (native_capabilities or ()):
            raise RuntimeCommandError("FullPower policy requires exam.native_legal_inputs before AP use")
        if bundle_path is None:
            raise RuntimeCommandError("FullPower requires an explicitly selected trained FullPower bundle before AP use")
        selected = json.loads(Path(bundle_path).read_text(encoding="utf-8-sig"))
        if isinstance(selected, Mapping) and "fullpower_current_state_trial" in selected:
            from .runtime_fullpower_policy import build_runtime_fullpower_policy
            policy = build_runtime_fullpower_policy(produce_id=produce_id, idol_card_id=idol_card_id,
                                                   policy_bundle_path=bundle_path)
            route = _fullpower_trial_model_route(policy, mode.audition_stages)
            return {**route, "produce_id": produce_id, "plan_type": plan_type,
                    "main_effect_type": idol.exam_effect_type, "native_legal_inputs_required": True}
        from .runtime_fullpower_policy import resolve_fullpower_native_primary_route
        bundle = PolicyBundle.load(bundle_path, component_roles=("exact_exam_policy",))
        route = resolve_fullpower_native_primary_route(bundle, required_stages=mode.audition_stages)
        if not route["available"]:
            raise RuntimeCommandError("selected FullPower model is not ready before AP use: " + "; ".join(route["blockers"]))
        return {**route, "produce_id": produce_id, "plan_type": plan_type,
                "main_effect_type": idol.exam_effect_type, "native_legal_inputs_required": True}
    bundle = PolicyBundle.load(bundle_path, component_roles=("exact_exam_policy",),
                               optional_component_roles=("offline_rl_policy",))
    route = resolve_exam_policy_mode_route(bundle, produce_id=produce_id,
        plan_type=idol.plan_type, main_effect_type=idol.exam_effect_type,
        required_stages=mode.audition_stages)
    if not route.available:
        raise RuntimeCommandError("selected model does not cover this mode/Plan before cultivation start: "
                                  + "; ".join(route.blockers))
    return route.to_dict()


def run_runtime_cultivation(
    *, idol_card_id: str, plan_type: str, produce_id: str,
    game_root=None, expected_run_id: str | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    plan2_max_actions: int = 100, plan2_policy_bundle_path: str | Path | None = None,
    weekly_bc_provider_path: str | Path | None = DEFAULT_WEEKLY_BC_PROVIDER,
    audition_strategy: str = "highest_available",
    exam_policy_variant: str | None = None,
    gateway: RuntimeOuterGateway | None = None, max_cycles: int = 10000,
    settlement_only: bool = False,
):
    """Drive the complete native scene lifecycle; no visual reader is built."""
    from .cultivation_contracts import (
        InitialRegularAutopilotResult, InitialRegularAutopilotStep,
        InitialRegularPlan2ExamContext,
        STATUS_COMPLETED, STATUS_STOPPED, STATUS_HARD_STOP, STATUS_FAILED_SETTLED,
    )
    from .produce_outer_local_save import DEFAULT_PC_GAME_ROOT
    from .run_identity import create_run, load_active_run
    from .runtime_failed_settlement import FailedSettlement
    from .runtime_session_continuity import (observe_run_session, proof_directory,
                                            choose_session_recovery_action)
    from .account_inventory import AccountInventorySnapshot, save_account_inventory
    from .account_loadout import (
        DEFAULT_CONSTRAINTS_PATH, load_loadout_constraints, LoadoutConstraints,
        read_available_loadout, recommend_initial_supports, apply_account_loadout, apply_native_memory_locks,
    )

    root = Path(game_root) if game_root is not None else DEFAULT_PC_GAME_ROOT
    gateway = gateway or RuntimeOuterGateway(cancelled=stop_requested)
    client = gateway.client
    steps = []
    source_run_id = expected_run_id
    entered = expected_run_id is not None
    completion_evidence = None
    failure_settlement = None
    session_run = None
    session_continuity = None
    prepared_loadout = False
    prepared_sections: set[str] = set()
    loadout_inventory = None
    loadout_proposal = None
    loadout_constraints = None
    snapshot = None
    deadline_without_progress = time.monotonic() + 45.0
    last_revision = None
    deck_plan_owner = None
    from .runtime_economy_context import read_economy_receipt, recover_economy_context

    economy_context = recover_economy_context(client.root / "completed_outer")

    def finish(status, reason, cycle):
        result = InitialRegularAutopilotResult(status, reason, plan_type, cycle, tuple(steps),
            last_surface=None if snapshot is None else dict(snapshot.raw),
            failure_settlement=None if failure_settlement is None else dict(failure_settlement.payload))
        payload = result.to_dict()
        payload["audition_strategy"] = audition_strategy
        payload['exam_policy_variant'] = exam_policy_variant
        payload["weekly_bc_provider_path"] = str(weekly_bc_provider_path) if weekly_bc_provider_path is not None else None
        if completion_evidence is not None:
            payload["native_completion"] = completion_evidence
        if session_continuity is not None:
            payload["session_continuity"] = session_continuity
        if source_run_id is not None and snapshot is not None:
            try:
                from .native_cultivation_evidence import write_native_cultivation_segment

                payload["native_segment"] = write_native_cultivation_segment(
                    source_run_id, payload, produce_id=produce_id, idol_card_id=idol_card_id,
                    session_generation=snapshot.session_generation,
                )
            except Exception as error:
                # Evidence storage reports its own failure; it never changes
                # an already-observed gameplay result or authorizes more input.
                payload["native_segment"] = {"recorded": False, "error": f"{type(error).__name__}: {error}"}
        if status == STATUS_COMPLETED and source_run_id is not None:
            try:
                payload["completed_run_evidence"] = dict(_finalize_native_completed_evidence(result,
                    run_id=source_run_id, produce_id=produce_id, idol_card_id=idol_card_id, game_root=root))
            except Exception as error:
                payload["completed_run_evidence"] = {"evidence_complete": False,
                    "dataset_ready": False, "error": f"{type(error).__name__}: {error}"}
        atomic_write(client.root / "last_cultivation.json", canonical_json_bytes(payload))
        if status == STATUS_COMPLETED:
            from dataclasses import replace
            result = replace(result, native_completed_run_evidence=deepcopy(payload.get("completed_run_evidence", {
                "evidence_complete": False, "error": "native-completed-run-receipt-unavailable"})))
        if (status in {STATUS_COMPLETED, STATUS_FAILED_SETTLED} and source_run_id is not None):
            from .run_identity import clear_active_run
            try:
                clear_active_run(expected_run_id=source_run_id)
            except Exception as error:
                from dataclasses import replace
                warning = _record_terminal_bookkeeping_pending(client.root, source_run_id, payload, error)
                result = replace(result, terminal_bookkeeping=warning)
        return result

    from .runtime_outer_policy import AUDITION_STRATEGIES, RANKING_PRODUCE_IDS
    if produce_id not in RANKING_PRODUCE_IDS:
        return finish(STATUS_HARD_STOP, f"native-mode-rules-not-implemented:{produce_id}", 0)
    if exam_policy_variant not in (None,'baseline','integrated'):
        return finish(STATUS_HARD_STOP, 'Unknown explicit exam policy variant', 0)
    if audition_strategy not in AUDITION_STRATEGIES:
        return finish(STATUS_HARD_STOP, f"native-audition-strategy-not-implemented:{audition_strategy}", 0)
    if settlement_only and expected_run_id is None:
        return finish(STATUS_HARD_STOP, "failure settlement requires an existing run ID", 0)

    for cycle in range(1, max_cycles + 1):
        if stop_requested():
            return finish(STATUS_STOPPED, "user-stop-requested", cycle)
        try:
            resumed = None
            if gateway.pending_path.exists():
                resumed = gateway.resume()
                if resumed.status == "pending":
                    return finish(STATUS_HARD_STOP, resumed.detail, cycle)
                if resumed.request_id is not None:
                    economy_context = read_economy_receipt(client.root / "completed_outer", resumed.request_id, economy_context)
            snapshot = gateway.read()
            state = snapshot.raw.get("state") or {}
            progress = snapshot.raw.get("progress") or {}
            if snapshot.raw.get("surface") == "error":
                error = (snapshot.raw.get("ui_state") or {}).get("error") or {}
                description = error.get("description", "native game error")
                return finish(STATUS_HARD_STOP, "native-game-error: " + str(description), cycle)
            active_session_run = load_active_run()
            if (active_session_run is not None and source_run_id in (None, active_session_run.run_id)
                    and active_session_run.produce_id == produce_id and active_session_run.idol_card_id == idol_card_id
                    and active_session_run.evidence.get('exam_policy_variant') != exam_policy_variant):
                raise RuntimeCommandError('Resume must retain the original cultivation model selection')
            if session_run is not None:
                if (active_session_run is None or active_session_run.to_dict() != session_run.to_dict()):
                    raise RuntimeCommandError("active run changed during session continuity verification")
            elif (active_session_run is not None
                  and source_run_id in (None, active_session_run.run_id)
                  and active_session_run.produce_id == produce_id and active_session_run.idol_card_id == idol_card_id):
                initial = getattr(active_session_run, "evidence", {}).get("session_generation")
                if initial and (initial != snapshot.session_generation or proof_directory(client.root, active_session_run.run_id).exists()):
                    session_run = active_session_run
                    source_run_id, entered = session_run.run_id, True
            if session_run is not None:
                session_continuity = observe_run_session(client.root, session_run, snapshot, previous=session_continuity)
            ui = snapshot.raw.get("ui_state") or {}
            exhausted = (snapshot.raw.get("surface") == "audition_result" and ui.get("is_win") is False
                         and ui.get("can_retry") is False and type(ui.get("remaining_count")) is int
                         and ui["remaining_count"] == 0)
            journal_exists = (source_run_id is not None
                and FailedSettlement.journal_path(client.root, source_run_id).exists())
            if failure_settlement is None and (exhausted or journal_exists):
                active = load_active_run()
                if (active is None or (source_run_id is not None and active.run_id != source_run_id)
                        or active.produce_id != produce_id or active.idol_card_id != idol_card_id):
                    raise RuntimeCommandError("exhausted failure does not bind the requested active run")
                if active.evidence.get('exam_policy_variant') != exam_policy_variant:
                    raise RuntimeCommandError('Resume must retain the original cultivation model selection')
                source_run_id, entered = active.run_id, True
                failure_settlement = FailedSettlement.observe(client.root, active, snapshot)
            if failure_settlement is not None:
                failure_settlement.check(snapshot, load_active_run())
                if resumed is not None:
                    failure_settlement.reconcile(client.root, resumed)
            elif settlement_only:
                raise RuntimeCommandError("settlement must begin at the verified exhausted audition failure")
            if (failure_settlement is None and isinstance(progress, Mapping)
                    and progress.get("isFailedProduce") is True and state.get("in_progress") is True):
                return finish(STATUS_HARD_STOP, "native-cultivation-failed: automatic result continuation paused", cycle)
            if progress_callback is not None:
                try:
                    recent = None if not steps else steps[-1].to_dict()
                    progress_callback({
                        "source_run_id": source_run_id, "current_page": snapshot.raw["surface"],
                        "audition_strategy": audition_strategy,
                        "cycles": cycle, "outer_action_count": len(steps), "recent_step": recent,
                        "monitor_snapshot": {"page": snapshot.raw["surface"],
                                             "state": state if state.get("in_progress") is not False else {"in_progress": False},
                                             "native_screen_type": snapshot.raw["screen_type"],
                                             "presentation": snapshot.raw.get("ui_state") if snapshot.raw["surface"] == "presentation" else None,
                                             "source": "dll", "recent_step": recent,
                                             "state_update": {"source": "dll", "timestamp": time.time()}},
                    })
                except Exception:
                    pass  # UI display cannot own gameplay or its transaction.
            if snapshot.revision != last_revision:
                last_revision = snapshot.revision
                deadline_without_progress = time.monotonic() + 45.0
            from .runtime_presentation import native_presentation_wait
            presentation = native_presentation_wait(snapshot, produce_id=produce_id, idol_card_id=idol_card_id)
            if presentation is not None:
                if time.monotonic() >= deadline_without_progress:
                    return finish(STATUS_HARD_STOP, "native Live presentation stopped advancing: " + presentation["phase"], cycle)
                gateway.sleep(0.5)
                continue
            if snapshot.raw["busy"]:
                if time.monotonic() >= deadline_without_progress:
                    return finish(STATUS_HARD_STOP, "native scene remained busy", cycle)
                gateway.sleep(0.25)
                continue
            in_progress = state.get("in_progress") is True
            if session_continuity is not None and session_continuity["status"] == "awaiting-progress":
                target = choose_session_recovery_action(snapshot, session_run, session_continuity)
                if target is None:
                    if time.monotonic() >= deadline_without_progress:
                        raise RuntimeCommandError("saved cultivation did not become available after maintenance")
                    gateway.sleep(0.25)
                    continue
                outcome = gateway.execute(snapshot, target)
                if outcome.status == "replan":
                    continue
                steps.append(InitialRegularAutopilotStep(cycle, snapshot.raw["surface"], target["action_id"],
                    str(target.get("button_id", "")), {"executor": "dll", "status": outcome.status,
                    "request_id": outcome.request_id, "decision": {"source": "maintenance-login-resume-only",
                    "gameplay_continuity_verified": False, "maintenance_job_id": session_continuity["maintenance_job_id"]}}))
                if outcome.status != "settled":
                    return finish(STATUS_HARD_STOP, outcome.detail, cycle)
                gateway.sleep(0.25)
                continue
            if failure_settlement is not None:
                if snapshot.raw["screen_type"] == "HomeTopScreenPresenter":
                    failure_settlement.complete(snapshot)
                    return finish(STATUS_FAILED_SETTLED, "native-failed-cultivation-returned-home", cycle)
                target, decision = failure_settlement.choose(snapshot, lambda: choose_runtime_outer_action(
                    snapshot, produce_id=produce_id, idol_card_id=idol_card_id,
                    audition_strategy=audition_strategy, weekly_bc_provider_path=weekly_bc_provider_path))
                if target is None:
                    if time.monotonic() >= deadline_without_progress:
                        raise RuntimeCommandError("normal failure settlement stopped advancing")
                    gateway.sleep(0.25)
                    continue
                outcome = gateway.execute(snapshot, target)
                failure_settlement.record(snapshot, target, outcome)
                if outcome.status == "replan":
                    continue
                steps.append(InitialRegularAutopilotStep(cycle, snapshot.raw["surface"], target["action_id"],
                    str(target.get("button_id", "")), {"executor": "dll", "status": outcome.status,
                    "request_id": outcome.request_id, "decision": decision}))
                if outcome.status != "settled":
                    return finish(STATUS_HARD_STOP, outcome.detail, cycle)
                gateway.sleep(0.25)
                continue
            if (source_run_id is None and not in_progress and snapshot.raw["surface"] in {"produce_result", "navigation"}):
                existing = load_active_run()
                if existing is not None and existing.evidence.get("source") == "native-produce-progress":
                    if existing.produce_id != produce_id or existing.idol_card_id != idol_card_id:
                        raise RuntimeCommandError("active native run belongs to another cultivation")
                    if existing.evidence.get('exam_policy_variant') != exam_policy_variant:
                        raise RuntimeCommandError('Resume must retain the original cultivation model selection')
                    source_run_id, entered = existing.run_id, True
            if (snapshot.raw["screen_type"] == "HomeTopScreenPresenter" and entered
                    and state.get("in_progress") is False):
                if progress.get("isFailedProduce") is True:
                    return finish(STATUS_HARD_STOP, "failed Home has no matched settlement proof", cycle)
                from .native_cultivation_evidence import inspect_native_completion

                completion_evidence = inspect_native_completion(source_run_id, produce_id=produce_id,
                    idol_card_id=idol_card_id, home_snapshot=snapshot.raw, current_steps=steps)
                if completion_evidence.get("gameplay_complete") is not True:
                    return finish(STATUS_HARD_STOP, "native run ended without verified Final/result completion: "
                        + ",".join(completion_evidence.get("blockers", ())), cycle)
                return finish(STATUS_COMPLETED, "native-home-and-produce-completed", cycle)
            selection = snapshot.raw.get("selection")
            selection = selection if isinstance(selection, Mapping) else {}
            if in_progress and source_run_id is None:
                progress = snapshot.raw.get("progress") or {}
                if progress.get("produceId") != produce_id or progress.get("idolCardId") != idol_card_id:
                    raise RuntimeCommandError("actual native cultivation differs from requested mode/idol")
                existing = load_active_run()
                if existing is not None:
                    if existing.produce_id != produce_id or existing.idol_card_id != idol_card_id:
                        raise RuntimeCommandError("active run manifest belongs to another cultivation")
                    if existing.evidence.get('exam_policy_variant') != exam_policy_variant:
                        raise RuntimeCommandError('Resume must retain the original cultivation model selection')
                    source_run_id = existing.run_id
                else:
                    identity = create_run(idol_card_id=idol_card_id, character_id=progress["characterId"],
                        produce_id=produce_id, evidence={"source": "native-produce-progress",
                            "audition_strategy": audition_strategy,
                            "exam_policy_variant": exam_policy_variant,
                            "weekly_bc_provider_path": str(weekly_bc_provider_path) if weekly_bc_provider_path is not None else None,
                            "session_generation": snapshot.session_generation,
                            "native_revision": snapshot.revision, "snapshot_path": str(snapshot.source_path)})
                    source_run_id = identity.run_id
                entered = True
            if in_progress and source_run_id is not None:
                progress = snapshot.raw.get("progress") or {}
                if progress.get("produceId") != produce_id or progress.get("idolCardId") != idol_card_id:
                    raise RuntimeCommandError("native cultivation identity changed during execution")
            if snapshot.raw["screen_type"] == "ExamScreenPresenter":
                from .runtime_exam_dispatch import dispatch_runtime_exam

                native_exam = client.execute("read_snapshot").require_ok()
                raw_exam = (native_exam.snapshot or {}).get("exam_save", {})
                step_type = raw_exam.get("stepType")
                result = dispatch_runtime_exam(
                    # Native evidence provenance; DLL executors read live state from their gateway.
                    plan_type, native_exam.path, produce_id=produce_id, idol_card_id=idol_card_id,
                    run_id=source_run_id, exam_type=raw_exam.get("examType"),
                    policy_bundle_path=plan2_policy_bundle_path, max_actions=plan2_max_actions,
                    **({'exam_policy_variant':exam_policy_variant} if exam_policy_variant is not None else {}),
                    stop_requested=stop_requested, progress_callback=progress_callback,
                    plan2_context=InitialRegularPlan2ExamContext(
                        idol_card_id=idol_card_id, produce_id=produce_id, max_actions=plan2_max_actions,
                        stage_number={16: 1, 17: 2, 18: 3}.get(step_type, 1),
                        learned_policy_bundle_path=None if plan2_policy_bundle_path is None else Path(plan2_policy_bundle_path),
                    ),
                )
                steps.append(InitialRegularAutopilotStep(cycle, "exam", "dll-exam", None, result))
                if not result.get("accepted") or not result.get("terminal"):
                    return finish(STATUS_HARD_STOP, str(result.get("reason", "native exam did not complete")), cycle)
                deadline_without_progress = time.monotonic() + 45.0
                gateway.sleep(0.25)
                continue
            section = {"ProduceSupportCardSelectScreenPresenter": "support", "ProduceMemorySelectScreenPresenter": "memory"}.get(snapshot.raw["screen_type"])
            if section is not None and section not in prepared_sections:
                rental_loaders = [a for a in snapshot.actions if a["action_id"] == "loadout.refresh_rentals"]
                if rental_loaders:
                    if len(rental_loaders) != 1:
                        raise RuntimeCommandError("native rental-loader action is ambiguous")
                    loaded = gateway.execute(snapshot, rental_loaders[0]["target"])
                    if loaded.status not in {"settled", "replan"}:
                        raise RuntimeCommandError(loaded.detail)
                    continue
                if loadout_inventory is None:
                    result = client.execute("read_inventory")
                    result.require_ok()
                    loadout_inventory = AccountInventorySnapshot.from_dict(result.raw["inventory"])
                    save_account_inventory(client.root.parent / "account_inventory/current.json", loadout_inventory)
                    loadout_constraints = load_loadout_constraints(DEFAULT_CONSTRAINTS_PATH, account_scope=loadout_inventory.account_scope) if DEFAULT_CONSTRAINTS_PATH.is_file() else LoadoutConstraints()
                    if loadout_constraints.excluded_memory_ids:
                        raise RuntimeCommandError("native-memory-exclusions-not-supported: 已保存回憶排除設定；本版遊戲自動編成無對應限制，請先在選卡偏好明確解除回憶排除。設定未刪除，未消耗 AP。")
                if section == "memory":
                    from .runtime_memory_autoselect import memory_auto_record, native_memory_ids, overlay_explicit_memory_locks
                    automatic = memory_auto_record(snapshot.raw)
                    if automatic and automatic.get("owner_bound") is True and automatic.get("phase") == "failed":
                        raise RuntimeCommandError("native memory auto-selection failed: " + str(automatic.get("failure", "unknown")))
                    if automatic and automatic.get("owner_bound") is True and automatic.get("phase") == "succeeded":
                        available = read_available_loadout(client)
                        current = available.get("memory_auto") or {}
                        if (current.get("operation_serial") != automatic.get("operation_serial")
                                or current.get("confirmed") is not True or current.get("phase") != "succeeded"
                                or current.get("owner_bound") is not True
                                or any(current.get(k) != value for k, value in (
                                    ("account_scope", loadout_inventory.account_scope), ("produce_id", produce_id), ("idol_card_id", idol_card_id)))):
                            raise RuntimeCommandError("native memory auto-selection completion or scope changed")
                        if (available.get("game_validation") or {}).get("enter_enabled") is not True:
                            if time.monotonic() >= deadline_without_progress:
                                raise RuntimeCommandError("native memory auto-selection completed but the game has not enabled continue")
                            gateway.sleep(.25)
                            continue
                        chosen = native_memory_ids(available)
                        eligible = {row.memory_id for row in loadout_inventory.memories
                            if row.plan_type in (plan_type, "ProducePlanType_Common")}
                        wanted, overrides = overlay_explicit_memory_locks(chosen,
                            locked_ids=loadout_constraints.locked_memory_ids,
                            excluded_ids=loadout_constraints.excluded_memory_ids, eligible_owned_ids=eligible)
                        if overrides:
                            applied = apply_native_memory_locks(loadout_inventory, available, overrides, client)
                            if (applied.status != "submitted" or applied.raw.get("applied") is not True
                                    or native_memory_ids(applied.raw.get("loadout") or {}) != wanted):
                                raise RuntimeCommandError("explicit memory locks are not confirmed by native readback")
                        prepared_sections.add("memory")
                        prepared_loadout = {"support", "memory"} <= prepared_sections
                        steps.append(InitialRegularAutopilotStep(cycle, "loadout", "native-memory-auto-ready", idol_card_id,
                            {"source": "game-native-memory-auto-selection", "operation_serial": current["operation_serial"],
                             "memory_ids": list(wanted), "explicit_lock_overrides": list(overrides),
                             "memory_model_used": False, "memory_heuristic_used": False}))
                        continue
                    choices = [a for a in snapshot.actions if a["action_id"] == "loadout.memory_auto"]
                    if len(choices) > 1:
                        raise RuntimeCommandError("native memory auto-selection action is ambiguous")
                    if not choices:
                        if time.monotonic() >= deadline_without_progress:
                            raise RuntimeCommandError("native memory auto-selection did not expose its next operation")
                        gateway.sleep(.25)
                        continue
                    target = choices[0]["target"]
                    if any(target.get(k) != value for k, value in (("account_scope", loadout_inventory.account_scope),
                            ("produce_id", produce_id), ("idol_card_id", idol_card_id))):
                        raise RuntimeCommandError("native memory auto-selection target scope differs")
                    selected = gateway.execute(snapshot, target)
                    if selected.status not in {"settled", "replan"}:
                        raise RuntimeCommandError(selected.detail)
                    if selected.status == "settled":
                        steps.append(InitialRegularAutopilotStep(cycle, "loadout", "loadout.memory_auto", idol_card_id,
                            {"source": "game-native-memory-auto-selection", "request_id": selected.request_id,
                             "status": selected.status, "operation_serial": target["operation_serial"]}))
                    continue
                if loadout_proposal is None:
                    available = read_available_loadout(client)
                    proposals = recommend_initial_supports(loadout_inventory, available, constraints=loadout_constraints,
                                                          idol_card_id=idol_card_id, produce_id=produce_id, limit=1)
                    if not proposals:
                        raise RuntimeCommandError("no available loadout satisfies locked cards")
                    loadout_proposal = proposals[0]
                applied = apply_account_loadout(loadout_inventory, loadout_proposal.selection, client, section="support")
                if applied.status != "submitted" or applied.raw.get("applied") is not True:
                    raise RuntimeCommandError("native loadout apply is not confirmed")
                applied_sections = applied.raw.get("applied_sections", [section])
                if applied_sections != ["support"]:
                    raise RuntimeCommandError("support-only preparation must not report memory selection")
                prepared_sections.add("support")
                prepared_loadout = {"support", "memory"} <= prepared_sections
                steps.append(InitialRegularAutopilotStep(cycle, "loadout", "dll-loadout", idol_card_id,
                                                        {"applied": True, "sections": list(applied_sections),
                                                         "full_loadout_applied": prepared_loadout,
                                                         "reasons": list(loadout_proposal.reasons)}))
                continue
            if not snapshot.actions and not (snapshot.raw["surface"] == "audition_retry" and snapshot.raw["actions_complete"]):
                if snapshot.raw.get("blockers"):
                    raise RuntimeCommandError("native screen contract unavailable: " + snapshot.raw["screen_type"]
                                              + "; blockers=" + str(snapshot.raw.get("blockers", [])))
                if time.monotonic() >= deadline_without_progress:
                    raise RuntimeCommandError("native screen did not expose its next action: " + snapshot.raw["screen_type"])
                # A temporary screen with no known action can finish on its
                # own. Observe the next boundary under the same deadline;
                # never invent a click or claim support for that screen.
                gateway.sleep(0.25)
                continue
            if deck_plan_owner is None and source_run_id is not None:
                from .runtime_deck_plan import RuntimeDeckPlan
                from .run_identity import DEFAULT_RUN_ROOT

                deck_plan_owner = RuntimeDeckPlan(DEFAULT_RUN_ROOT / source_run_id / "deck_plan.json", run_id=source_run_id)
            target, decision = choose_runtime_outer_action(snapshot, produce_id=produce_id, idol_card_id=idol_card_id,
                audition_strategy=audition_strategy, deck_plan_owner=deck_plan_owner, operation_context=economy_context,
                weekly_bc_provider_path=weekly_bc_provider_path)
            if target is None:
                if time.monotonic() >= deadline_without_progress:
                    raise RuntimeCommandError("native chosen action did not become ready: " + str(decision.get("reason", "")))
                gateway.sleep(0.25)
                continue
            if target.get("button_id") == "produce.start" and not prepared_loadout:
                raise RuntimeCommandError("start requires an applied native loadout")
            if target.get("button_id") == "produce.start":
                from .portable_outer_assets import preflight_outer_assets
                decision = {**decision, 'outer_asset_preflight':preflight_outer_assets(
                    produce_id=produce_id,idol_card_id=idol_card_id,weekly_provider_path=weekly_bc_provider_path)}
                native_status = _preflight_start_recorder(client, snapshot.session_generation)
                model_context = None
                if exam_policy_variant is not None:
                    model_read = client.execute('read_model_context').require_ok()
                    if model_read.request.session_generation != snapshot.session_generation:
                        raise RuntimeCommandError('Native generation changed during model preflight')
                    model_context = model_read.raw.get('model_context')
                decision = {**decision, "model_mode_route": _preflight_native_model_mode(
                    produce_id, idol_card_id, plan_type, plan2_policy_bundle_path,
                    native_capabilities=native_status.get("capabilities", ()),
                    **({'exam_policy_variant':exam_policy_variant,'model_context':model_context}
                       if exam_policy_variant is not None else {}))}
            result = gateway.execute(snapshot, target)
            if result.status == "replan":
                continue
            steps.append(InitialRegularAutopilotStep(cycle, snapshot.raw["surface"], str(target["action_id"]),
                                                    str(target.get("button_id", target.get("step_type", ""))),
                                                    {"executor": "dll", "status": result.status, "request_id": result.request_id, "decision": decision}))
            if result.status != "settled":
                return finish(STATUS_HARD_STOP, result.detail, cycle)
            economy_context = read_economy_receipt(client.root / "completed_outer", result.request_id, economy_context)
            if decision.get("clear_operation_context") is True:
                economy_context = None
            if decision.get("stop_after_action") is True:
                return finish(STATUS_HARD_STOP, "native economy cancelled: " + str(decision.get("reason")), cycle)
        except Exception as error:
            return finish(STATUS_HARD_STOP, f"native-cultivation:{type(error).__name__}:{error}", cycle)
        gateway.sleep(0.25)
    return finish(STATUS_HARD_STOP, "native-cycle-budget-reached", max_cycles)
