"""Thin multi-run coordinator over the existing single-run live controller."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .initial_regular_simulator_gui import InitialRegularSimulatorPhase, terminal_bookkeeping_pending


class LiveController(Protocol):
    @property
    def view(self) -> object: ...

    def start(self) -> object: ...

    def stop(self) -> object: ...

    def poll(self) -> object: ...


@dataclass(frozen=True, slots=True)
class CultivationBatchView:
    target_cycles: int
    started_cycles: int
    completed_cycles: int
    success_count: int
    failure_count: int
    intervention_count: int
    running: bool
    stop_requested: bool
    message: str
    live_view: object

    @property
    def progress_text(self) -> str:
        return f"{self.completed_cycles} / {self.target_cycles}"


class CultivationBatchController:
    """Start the next run only after the prior live controller reaches terminal."""

    def __init__(
        self,
        live_controller: LiveController,
        *,
        prepare_next_run: Callable[[], None] | None = None,
    ) -> None:
        for name in ("start", "stop", "poll"):
            if not callable(getattr(live_controller, name, None)):
                raise TypeError("live_controller does not implement the live API")
        if prepare_next_run is not None and not callable(prepare_next_run):
            raise TypeError("prepare_next_run must be callable or None")
        self.live_controller = live_controller
        self.prepare_next_run = prepare_next_run
        self._target = 1
        self._started = 0
        self._completed = 0
        self._success = 0
        self._failure = 0
        self._interventions = 0
        self._running = False
        self._stop_requested = False
        self._last_phase: object | None = None
        self._message = "尚未開始"

    @staticmethod
    def _phase(view: object) -> object:
        return getattr(view, "phase", None)

    def _view(self, live_view: object | None = None) -> CultivationBatchView:
        current = self.live_controller.view if live_view is None else live_view
        return CultivationBatchView(
            target_cycles=self._target,
            started_cycles=self._started,
            completed_cycles=self._completed,
            success_count=self._success,
            failure_count=self._failure,
            intervention_count=self._interventions,
            running=self._running,
            stop_requested=self._stop_requested,
            message=self._message,
            live_view=current,
        )

    @property
    def view(self) -> CultivationBatchView:
        return self._view()

    def start(self, target_cycles: int = 1) -> CultivationBatchView:
        if isinstance(target_cycles, bool) or not isinstance(target_cycles, int):
            raise TypeError("target_cycles must be an integer")
        if target_cycles < 1:
            raise ValueError("target_cycles must be at least one")
        if terminal_bookkeeping_pending(self.live_controller.view):
            self._running = False
            self._message = str(getattr(self.live_controller.view, "stop_reason_text", "本場培育已完成；本機狀態尚待清理，已停止連跑"))
            return self._view()
        if self._running:
            return self._view()
        self._target = target_cycles
        self._started = 1
        self._completed = 0
        self._success = 0
        self._failure = 0
        self._interventions = 0
        self._running = True
        self._stop_requested = False
        try:
            live_view = self.live_controller.start()
        except Exception:
            self._running = False
            self._failure = 1
            self._message = "第一場啟動失敗；批次已停止"
            raise
        self._last_phase = self._phase(live_view)
        self._message = "第 1 場執行中"
        if self._last_phase in {
            InitialRegularSimulatorPhase.BLOCKED,
            InitialRegularSimulatorPhase.FAILED,
        }:
            self._running = False
            self._failure = 1
            self._completed = 1
            self._message = "第一場未能開始；批次已停止"
        return self._view(live_view)

    def stop(self) -> CultivationBatchView:
        self._stop_requested = True
        self._message = "停止要求已送出；不再排新場"
        live_view = self.live_controller.stop()
        if self._phase(live_view) not in {
            InitialRegularSimulatorPhase.RUNNING,
            InitialRegularSimulatorPhase.CANCELLING,
        }:
            self._running = False
        self._last_phase = self._phase(live_view)
        return self._view(live_view)

    def poll(self) -> CultivationBatchView:
        live_view = self.live_controller.poll()
        phase = self._phase(live_view)
        terminal_transition = phase != self._last_phase
        self._last_phase = phase
        if not self._running:
            return self._view(live_view)
        if phase == InitialRegularSimulatorPhase.COMPLETED and terminal_transition:
            self._completed += 1
            self._success += 1
            if terminal_bookkeeping_pending(live_view):
                self._running = False
                self._message = str(getattr(live_view, "stop_reason_text", "本場培育已完成；本機狀態尚待清理，已停止連跑"))
                return self._view(live_view)
            if self._stop_requested or self._completed >= self._target:
                self._running = False
                self._message = "批次已完成"
                return self._view(live_view)
            try:
                if self.prepare_next_run is not None:
                    self.prepare_next_run()
                next_view = self.live_controller.start()
            except Exception as error:
                self._failure += 1
                self._running = False
                self._message = (
                    "下一場準備失敗；批次已停止："
                    f"{type(error).__name__}: {error}"
                )
                return self._view(live_view)
            self._started += 1
            self._last_phase = self._phase(next_view)
            self._message = f"第 {self._started} 場執行中"
            if self._last_phase in {
                InitialRegularSimulatorPhase.BLOCKED,
                InitialRegularSimulatorPhase.FAILED,
            }:
                self._failure += 1
                self._completed += 1
                self._running = False
                self._message = "下一場未能開始；批次已停止"
            return self._view(next_view)
        if phase in {
            InitialRegularSimulatorPhase.FAILED,
            InitialRegularSimulatorPhase.BLOCKED,
        } and terminal_transition:
            self._failure += 1
            self._completed += 1
            self._running = False
            self._message = "本場失敗；批次已停止，未自動重試"
        elif phase == InitialRegularSimulatorPhase.CANCELLED and terminal_transition:
            self._running = False
            self._message = "批次已停止"
        return self._view(live_view)


__all__ = ["CultivationBatchController", "CultivationBatchView"]
