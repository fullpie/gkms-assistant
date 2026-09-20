"""Non-technical view-model and Tk panel for Initial Regular offline search.

The controller accepts only already-built pure rollout inputs.  It never
captures the game or claims that missing server/RNG branches can be fetched
automatically.  Search runs on a background worker; cancellation is
cooperative at external-request boundaries and stale results are discarded.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from fractions import Fraction
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, TypeAlias
import tkinter as tk
from tkinter import filedialog, ttk

from .audition_rules import FINAL, MID1, MID2
from .cultivation_contracts import STATUS_FAILED_SETTLED
from .initial_regular_offline_search import (
    InitialRegularAdapterCacheKey,
    InitialRegularFrontierCacheKey,
    InitialRegularOfflineBranchExpansion,
    InitialRegularOfflineBranchProvider,
    search_initial_regular_offline,
)
from .initial_regular_fixed_run_report import (
    InitialRegularFixedRunCardCoverageReport,
    InitialRegularFixedRunReport,
    InitialRegularFixedRunMilestoneReport,
)
from .initial_regular_fixed_run import InitialRegularFixedRunStop
from .initial_regular_nia_master_outer_scaffold import (
    NIA_MASTER_OUTER_SCAFFOLD_LABEL,
    NiaMasterOuterScaffoldReport,
)
from .nia_master_inner_acceptance import NiaMasterInnerAcceptanceResult
from .nia_full_scenario_comparison import NiaFullScenarioComparison
from .nia_full_monte_carlo import NiaMonteCarloComparison
from .initial_regular_autopilot import (
    PAGE_EXAM,
    PAGE_NIA_OUTER,
    PAGE_OVERVIEW,
    PAGE_UNKNOWN,
    PLAN1,
    PLAN2,
    PLAN3,
    STATUS_COMPLETED as LIVE_STATUS_COMPLETED,
    STATUS_HARD_STOP as LIVE_STATUS_HARD_STOP,
    STATUS_STOPPED as LIVE_STATUS_STOPPED,
    InitialRegularAutopilotResult,
    InitialRegularLiveSurfaceReader,
    InitialRegularSurface,
    run_live_initial_regular_autopilot,
)
from .exam_execution_policy import (
    EXACT_EXAM_POLICY,
    MAA_COMPLETION_EXAM_POLICY,
)
from .live_monitor_event_stream import LiveMonitorEventWatcher
from .master_db import DEFAULT_DATABASE
from .gui_dashboard_views import cultivation_action_label, cultivation_page_label, native_cultivation_mode_support
from .plan2_native_exam_save_orchestrator import (
    Plan2NativeExamSaveDecision,
    decide_plan2_native_exam_save,
)
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxLimits,
)
from .plan2_native_horizon import Plan2NativeAction, Plan2NativeHorizonState
from .plan2_deck_snapshot import (
    Plan2CardSnapshot,
    Plan2DeckSnapshot,
    build_plan2_deck_snapshot,
)
from .plan2_native_self_play_acceptance import (
    DEFAULT_MASTER_DIR as PLAN2_DEFAULT_MASTER_DIR,
    Plan2NativeSelfPlayResult,
    run_plan2_native_self_play_acceptance,
)
from .plan3_rollout_inner_adapter import Plan3InnerSearchCallable
from .produce_outer_local_save import DEFAULT_PC_GAME_ROOT
from .produce_rollout import ProduceAction, ProduceRolloutKernel, ProduceRolloutState
from .produce_rollout_expectimax import (
    OuterActionValue,
    OuterExpectimaxPause,
    OuterExpectimaxReady,
    OuterExpectimaxResult,
    OuterSearchIssue,
    OuterTerminalEvaluator,
)
from .route_calendar import CLASS, CRAM_LESSON, LESSON, OUTING, SUPPLY


class InitialRegularSimulatorPhase(StrEnum):
    BLOCKED = "blocked"
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class InitialRegularSimulatorDataKind(StrEnum):
    SP_BRANCHES = "sp_branches"
    EVENT_BRANCHES = "event_branches"
    REWARD_BRANCHES = "reward_branches"
    RNG_STATES = "rng_states"
    CARD_GUIDS = "card_guids"
    INNER_SEARCH = "inner_search"
    AUDITION_RANKS = "audition_ranks"


_DATA_KIND_LABELS = {
    InitialRegularSimulatorDataKind.SP_BRANCHES: "普通／SP 課程分支",
    InitialRegularSimulatorDataKind.EVENT_BRANCHES: "事件結果分支",
    InitialRegularSimulatorDataKind.REWARD_BRANCHES: "獎勵與替換清單",
    InitialRegularSimulatorDataKind.RNG_STATES: "課程／考試 RNG",
    InitialRegularSimulatorDataKind.CARD_GUIDS: "牌組 GUID",
    InitialRegularSimulatorDataKind.INNER_SEARCH: "完整課程／考試搜尋器",
    InitialRegularSimulatorDataKind.AUDITION_RANKS: "試鏡名次與通關邊界",
}

_DATA_KIND_BLOCKERS = {
    InitialRegularSimulatorDataKind.SP_BRANCHES: (
        "還缺每個課程的普通／SP 分支與機率。"
    ),
    InitialRegularSimulatorDataKind.EVENT_BRANCHES: (
        "還缺課堂、活動或外出事件的伺服器結果分支。"
    ),
    InitialRegularSimulatorDataKind.REWARD_BRANCHES: (
        "還缺獎勵候選或替換結果的完整清單。"
    ),
    InitialRegularSimulatorDataKind.RNG_STATES: (
        "還缺課程／考試開始時的亂數狀態（RNG）。"
    ),
    InitialRegularSimulatorDataKind.CARD_GUIDS: (
        "還缺牌組中每張牌的實例識別碼（GUID）。"
    ),
    InitialRegularSimulatorDataKind.INNER_SEARCH: (
        "還缺可執行完整課程／考試模擬的搜尋器。"
    ),
    InitialRegularSimulatorDataKind.AUDITION_RANKS: (
        "還缺試鏡名次與通關邊界的明確結果。"
    ),
}


@dataclass(frozen=True, slots=True)
class InitialRegularSimulatorDataInventory:
    available: frozenset[InitialRegularSimulatorDataKind] = frozenset()

    def __post_init__(self) -> None:
        values = frozenset(self.available)
        if any(not isinstance(value, InitialRegularSimulatorDataKind) for value in values):
            raise TypeError("available must contain simulator data kinds")
        object.__setattr__(self, "available", values)

    @classmethod
    def complete(cls) -> "InitialRegularSimulatorDataInventory":
        return cls(frozenset(InitialRegularSimulatorDataKind))

    @property
    def missing(self) -> tuple[InitialRegularSimulatorDataKind, ...]:
        return tuple(value for value in InitialRegularSimulatorDataKind if value not in self.available)

    @property
    def completeness_text(self) -> str:
        total = len(InitialRegularSimulatorDataKind)
        return f"已具備 {len(self.available)}/{total} 類必要資料"


@dataclass(frozen=True, slots=True)
class InitialRegularSimulatorRequest:
    kernel: ProduceRolloutKernel | None = None
    state: ProduceRolloutState | None = None
    branch_provider: InitialRegularOfflineBranchProvider | None = None
    search: Plan3InnerSearchCallable | None = None
    evaluator: OuterTerminalEvaluator | None = None
    inventory: InitialRegularSimulatorDataInventory = field(
        default_factory=InitialRegularSimulatorDataInventory
    )
    beam_width: int = 64
    max_transitions: int = 128
    adapter_cache_key: InitialRegularAdapterCacheKey | None = None
    frontier_cache_key: InitialRegularFrontierCacheKey | None = None

    def __post_init__(self) -> None:
        if self.kernel is not None and not isinstance(self.kernel, ProduceRolloutKernel):
            raise TypeError("kernel must be ProduceRolloutKernel or None")
        if self.state is not None and not isinstance(self.state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState or None")
        if self.branch_provider is not None and not callable(self.branch_provider):
            raise TypeError("branch_provider must be callable or None")
        if self.search is not None and not callable(self.search):
            raise TypeError("search must be callable or None")
        if self.evaluator is not None and not callable(self.evaluator):
            raise TypeError("evaluator must be callable or None")
        if not isinstance(self.inventory, InitialRegularSimulatorDataInventory):
            raise TypeError("inventory must be InitialRegularSimulatorDataInventory")
        for name in ("beam_width", "max_transitions"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("adapter_cache_key", "frontier_cache_key"):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable or None")


@dataclass(frozen=True, slots=True)
class InitialRegularSimulatorView:
    phase: InitialRegularSimulatorPhase
    state_summary: str
    completeness_text: str
    status_text: str
    best_action_text: str
    reason_text: str
    blockers: tuple[str, ...] = ()
    technical_details: tuple[str, ...] = ()
    start_enabled: bool = False
    cancel_enabled: bool = False
    details_expanded: bool = False


@dataclass(frozen=True, slots=True)
class _WorkerMessage:
    generation: int
    result: OuterExpectimaxResult | None = None
    error: Exception | None = None


SimulatorRunner: TypeAlias = Callable[..., OuterExpectimaxResult]
SimulatorSubmit: TypeAlias = Callable[[Callable[[], None]], None]


_ACTION_LABELS = {
    LESSON: "一般課程",
    CRAM_LESSON: "強化課程",
    CLASS: "上課事件",
    SUPPLY: "活動支給",
    OUTING: "外出",
    "rest": "休息",
    MID1: "期中試鏡",
    MID2: "第二次試鏡",
    FINAL: "最終試鏡",
}


def _state_summary(state: ProduceRolloutState | None) -> str:
    if state is None:
        return "尚未提供培育狀態"
    deck_count = sum(entry.count for entry in state.deck)
    return (
        f"第 {state.week}/{state.total_weeks} 週　"
        f"體力 {state.stamina}/{state.max_stamina}　"
        f"Vo {state.attributes.vocal} / Da {state.attributes.dance} / "
        f"Vi {state.attributes.visual}　牌庫 {deck_count} 張"
    )


def _input_blockers(
    request: InitialRegularSimulatorRequest,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    blockers: list[str] = []
    details: list[str] = []
    if request.kernel is None:
        blockers.append("還缺 Initial Regular 的培育規則。")
        details.append("[kernel-unresolved] kernel")
    if request.state is None:
        blockers.append("還缺目前培育狀態。")
        details.append("[state-unresolved] state")
    if request.branch_provider is None:
        blockers.append("還缺可完整列舉外部分支的資料來源。")
        details.append("[branch-provider-unresolved] branch_provider")
    if request.evaluator is None:
        blockers.append("還缺用來比較完整培育結果的評分目標。")
        details.append("[terminal-evaluator-unresolved] evaluator")
    if request.search is None and InitialRegularSimulatorDataKind.INNER_SEARCH in request.inventory.available:
        blockers.append(_DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.INNER_SEARCH])
        details.append("[inner-search-unresolved] search")
    for kind in request.inventory.missing:
        blockers.append(_DATA_KIND_BLOCKERS[kind])
        details.append(f"[data-unresolved] {kind.value}")
    return tuple(dict.fromkeys(blockers)), tuple(details)


def _action_label(action: ProduceAction | None, *, terminal: bool) -> str:
    if terminal:
        return "培育已完成"
    if action is None:
        return "目前沒有可顯示的下一步"
    return _ACTION_LABELS.get(action.choice_id, action.choice_id)


def _fraction_text(value: Fraction) -> str:
    return str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"


def _action_value_detail(value: OuterActionValue) -> str:
    label = _ACTION_LABELS.get(value.action.choice_id, value.action.choice_id)
    return f"{label}: exact EV {_fraction_text(value.value)}"


def _human_blockers(issues: tuple[OuterSearchIssue, ...]) -> tuple[str, ...]:
    messages: list[str] = []
    for issue in issues:
        text = f"{issue.code} {issue.field} {issue.detail}".lower()
        if issue.code == "unresolved-action":
            continue
        if "guid" in text:
            message = _DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.CARD_GUIDS]
        elif "rng" in text or "random-state" in text or "random-root" in text:
            message = _DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.RNG_STATES]
        elif "rank" in text or "audition-rank" in text:
            message = _DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.AUDITION_RANKS]
        elif "search" in text:
            message = _DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.INNER_SEARCH]
        elif "reward" in text or "offer" in text or "replacement" in text:
            message = _DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.REWARD_BRANCHES]
        elif "event" in text:
            message = _DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.EVENT_BRANCHES]
        elif "sp" in text:
            message = _DATA_KIND_BLOCKERS[InitialRegularSimulatorDataKind.SP_BRANCHES]
        elif "cancel" in text:
            message = "計算已取消。"
        else:
            message = "仍有一項外部分支尚未完整提供，請展開技術細節確認。"
        if message not in messages:
            messages.append(message)
    if not messages:
        messages.append("資料尚未完整，請展開技術細節確認。")
    return tuple(messages)


def _result_technical_details(result: OuterExpectimaxResult) -> tuple[str, ...]:
    if isinstance(result, OuterExpectimaxPause):
        return tuple(
            f"[{issue.code}] {issue.field}"
            + (f": {issue.detail}" if issue.detail else "")
            for issue in result.issues
        )
    details = [
        f"exact EV: {_fraction_text(result.value)}",
        f"explored nodes: {result.explored_nodes}",
    ]
    details.extend(_action_value_detail(value) for value in result.action_values)
    return tuple(details)


def format_initial_regular_simulator_view(
    request: InitialRegularSimulatorRequest,
    *,
    phase: InitialRegularSimulatorPhase | None = None,
    result: OuterExpectimaxResult | None = None,
    error: Exception | None = None,
    details_expanded: bool = False,
) -> InitialRegularSimulatorView:
    """Build one immutable, non-technical simulator view."""

    if not isinstance(request, InitialRegularSimulatorRequest):
        raise TypeError("request must be InitialRegularSimulatorRequest")
    blockers, input_details = _input_blockers(request)
    if phase is None:
        phase = (
            InitialRegularSimulatorPhase.BLOCKED
            if blockers
            else InitialRegularSimulatorPhase.IDLE
        )
    summary = _state_summary(request.state)
    completeness = request.inventory.completeness_text
    if phase == InitialRegularSimulatorPhase.RUNNING:
        return InitialRegularSimulatorView(
            phase,
            summary,
            completeness,
            "正在背景計算完整培育路線…",
            "計算中",
            "視窗仍可操作；只會使用目前已提供的明確分支。",
            start_enabled=False,
            cancel_enabled=True,
            details_expanded=details_expanded,
        )
    if phase == InitialRegularSimulatorPhase.CANCELLING:
        return InitialRegularSimulatorView(
            phase,
            summary,
            completeness,
            "正在取消計算…",
            "取消中",
            "會在下一個外部分支邊界停止；目前視窗不會被鎖住。",
            start_enabled=False,
            cancel_enabled=False,
            details_expanded=details_expanded,
        )
    if phase == InitialRegularSimulatorPhase.CANCELLED:
        return InitialRegularSimulatorView(
            phase,
            summary,
            completeness,
            "計算已取消",
            "尚未產生建議",
            "可以補齊或調整資料後重新開始。",
            blockers=("計算已取消。",),
            start_enabled=not blockers,
            details_expanded=details_expanded,
        )
    if error is not None or phase == InitialRegularSimulatorPhase.FAILED:
        detail = "" if error is None else f"{type(error).__name__}: {error}"
        return InitialRegularSimulatorView(
            InitialRegularSimulatorPhase.FAILED,
            summary,
            completeness,
            "計算未完成",
            "尚未產生建議",
            "背景計算發生錯誤，沒有改用簡化估算。",
            blockers=("計算發生錯誤，請展開技術細節確認。",),
            technical_details=(detail,) if detail else (),
            start_enabled=not blockers,
            details_expanded=details_expanded,
        )
    if isinstance(result, OuterExpectimaxPause):
        return InitialRegularSimulatorView(
            InitialRegularSimulatorPhase.BLOCKED,
            summary,
            completeness,
            "資料不足，計算已暫停",
            "尚未產生建議",
            "缺少完整分支時不會退回猜測或簡化規則。",
            blockers=_human_blockers(result.issues),
            technical_details=_result_technical_details(result),
            start_enabled=not blockers,
            details_expanded=details_expanded,
        )
    if isinstance(result, OuterExpectimaxReady):
        action = _action_label(result.best_action, terminal=result.terminal)
        reason = (
            "已完整列舉目前提供的所有分支；這個下一步的精確期望值最高，"
            f"為 {_fraction_text(result.value)}。"
        )
        if result.terminal:
            reason = f"完整培育結果已可評分，精確值為 {_fraction_text(result.value)}。"
        return InitialRegularSimulatorView(
            InitialRegularSimulatorPhase.COMPLETED,
            summary,
            completeness,
            "計算完成",
            action,
            reason,
            technical_details=_result_technical_details(result),
            start_enabled=not blockers,
            details_expanded=details_expanded,
        )
    return InitialRegularSimulatorView(
        phase,
        summary,
        completeness,
        "資料尚未完整" if blockers else "可以開始計算",
        "尚未產生建議",
        (
            "請先補齊下列資料。"
            if blockers
            else "計算只會使用明確列舉的分支，不會操作遊戲。"
        ),
        blockers=blockers,
        technical_details=input_details,
        start_enabled=not blockers,
        cancel_enabled=False,
        details_expanded=details_expanded,
    )


def _default_submit(job: Callable[[], None]) -> None:
    threading.Thread(
        target=job,
        daemon=True,
        name="gkms-initial-regular-offline-search",
    ).start()


class InitialRegularSimulatorController:
    """Headless background-search controller consumed by the Tk panel."""

    def __init__(
        self,
        request: InitialRegularSimulatorRequest = InitialRegularSimulatorRequest(),
        *,
        runner: SimulatorRunner | None = None,
        submit: SimulatorSubmit | None = None,
    ) -> None:
        if not isinstance(request, InitialRegularSimulatorRequest):
            raise TypeError("request must be InitialRegularSimulatorRequest")
        self._request = request
        self._runner = search_initial_regular_offline if runner is None else runner
        self._submit = _default_submit if submit is None else submit
        if not callable(self._runner) or not callable(self._submit):
            raise TypeError("runner and submit must be callable")
        self._queue: queue.Queue[_WorkerMessage] = queue.Queue()
        self._generation = 0
        self._cancel_event: threading.Event | None = None
        self._details_expanded = False
        self._view = format_initial_regular_simulator_view(request)

    @property
    def request(self) -> InitialRegularSimulatorRequest:
        return self._request

    @property
    def view(self) -> InitialRegularSimulatorView:
        return self._view

    def set_request(
        self, request: InitialRegularSimulatorRequest
    ) -> InitialRegularSimulatorView:
        if not isinstance(request, InitialRegularSimulatorRequest):
            raise TypeError("request must be InitialRegularSimulatorRequest")
        if self._view.phase in {
            InitialRegularSimulatorPhase.RUNNING,
            InitialRegularSimulatorPhase.CANCELLING,
        }:
            self.cancel()
        self._generation += 1
        self._request = request
        self._view = format_initial_regular_simulator_view(
            request, details_expanded=self._details_expanded
        )
        return self._view

    def set_details_expanded(self, expanded: bool) -> InitialRegularSimulatorView:
        if not isinstance(expanded, bool):
            raise TypeError("expanded must be a boolean")
        self._details_expanded = expanded
        self._view = replace(self._view, details_expanded=expanded)
        return self._view

    def start(self) -> InitialRegularSimulatorView:
        blockers, _details = _input_blockers(self._request)
        if blockers or self._view.phase in {
            InitialRegularSimulatorPhase.RUNNING,
            InitialRegularSimulatorPhase.CANCELLING,
        }:
            self._view = format_initial_regular_simulator_view(
                self._request, details_expanded=self._details_expanded
            )
            return self._view
        self._generation += 1
        generation = self._generation
        cancel_event = threading.Event()
        self._cancel_event = cancel_event
        request = self._request
        self._view = format_initial_regular_simulator_view(
            request,
            phase=InitialRegularSimulatorPhase.RUNNING,
            details_expanded=self._details_expanded,
        )

        def worker() -> None:
            try:
                assert request.kernel is not None
                assert request.state is not None
                assert request.branch_provider is not None
                assert request.search is not None
                assert request.evaluator is not None

                def cancellable_provider(state, pending, weekly_node):
                    if cancel_event.is_set():
                        return InitialRegularOfflineBranchExpansion.paused(
                            OuterSearchIssue(
                                "simulation-cancelled", "cancel", "user request"
                            )
                        )
                    return request.branch_provider(state, pending, weekly_node)

                result = self._runner(
                    request.kernel,
                    request.state,
                    branch_provider=cancellable_provider,
                    search=request.search,
                    evaluator=request.evaluator,
                    beam_width=request.beam_width,
                    max_transitions=request.max_transitions,
                    adapter_cache_key=request.adapter_cache_key,
                    frontier_cache_key=request.frontier_cache_key,
                )
            except Exception as error:
                self._queue.put(_WorkerMessage(generation, error=error))
            else:
                self._queue.put(_WorkerMessage(generation, result=result))

        self._submit(worker)
        return self._view

    def cancel(self) -> InitialRegularSimulatorView:
        if self._view.phase != InitialRegularSimulatorPhase.RUNNING:
            return self._view
        assert self._cancel_event is not None
        self._cancel_event.set()
        self._view = format_initial_regular_simulator_view(
            self._request,
            phase=InitialRegularSimulatorPhase.CANCELLING,
            details_expanded=self._details_expanded,
        )
        return self._view

    def poll(self) -> InitialRegularSimulatorView:
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            if message.generation != self._generation:
                continue
            cancelled = self._cancel_event is not None and self._cancel_event.is_set()
            if cancelled:
                self._view = format_initial_regular_simulator_view(
                    self._request,
                    phase=InitialRegularSimulatorPhase.CANCELLED,
                    details_expanded=self._details_expanded,
                )
            elif message.error is not None:
                self._view = format_initial_regular_simulator_view(
                    self._request,
                    phase=InitialRegularSimulatorPhase.FAILED,
                    error=message.error,
                    details_expanded=self._details_expanded,
                )
            else:
                self._view = format_initial_regular_simulator_view(
                    self._request,
                    result=message.result,
                    details_expanded=self._details_expanded,
                )
            self._cancel_event = None
        return self._view


class Plan2OfflineSimulatorSource(StrEnum):
    MASTER_INITIAL = "master_initial"
    EXAM_SAVE_DATA = "exam_save_data"


class Plan2OfflineSimulatorOperation(StrEnum):
    DECISION = "decision"
    SELF_PLAY = "self_play"
    FIXED_RUN = "fixed_run"


class OfflineFixedRunPlan(StrEnum):
    PLAN1 = "plan1"
    PLAN2 = "plan2"
    PLAN2_AGGRESSIVE = "plan2_aggressive"
    PLAN3 = "plan3"
    NIA_PRO_MID1 = "nia_pro_mid1"
    NIA_PRO_FULL = "nia_pro_full"
    NIA_MASTER_PREFIX = "nia_master_prefix"
    NIA_MASTER_INNER = "nia_master_inner"
    NIA_PRO_LESSON_COMPARE = "nia_pro_lesson_compare"
    NIA_PRO_WEEKLY_COMPARE = "nia_pro_weekly_compare"
    NIA_PRO_MONTE_CARLO = "nia_pro_monte_carlo"


_PLAN2_SOURCE_LABELS = {
    Plan2OfflineSimulatorSource.MASTER_INITIAL: "目前 Master 初始牌組",
    Plan2OfflineSimulatorSource.EXAM_SAVE_DATA: "ExamSaveData",
}

_OFFLINE_FIXED_RUN_PLAN_LABELS = {
    OfflineFixedRunPlan.PLAN1: "Plan1 好調／集中（FKTN）",
    OfflineFixedRunPlan.PLAN2: "Plan2 好印象（Campus mode!!／FKTN）",
    OfflineFixedRunPlan.PLAN2_AGGRESSIVE: "Plan2 元氣／幹勁（冠菊／FKTN）",
    OfflineFixedRunPlan.PLAN3: "Plan3 指針（FKTN）",
    OfflineFixedRunPlan.NIA_PRO_MID1: "NIA Pro Mid1 單場（非 27 週整場）",
    OfflineFixedRunPlan.NIA_PRO_FULL: "NIA Pro 27 週完整固定情境（FKTN）",
    OfflineFixedRunPlan.NIA_MASTER_PREFIX: (
        "NIA Master caller 回應前綴（partial／非 LocalSave）"
    ),
    OfflineFixedRunPlan.NIA_MASTER_INNER: "NIA Master 三演出核心（無 26 週外層）",
    OfflineFixedRunPlan.NIA_PRO_LESSON_COMPARE: "NIA Pro 指定週 Vo／Da／Vi 完整路線比較",
    OfflineFixedRunPlan.NIA_PRO_WEEKLY_COMPARE: "NIA Pro 外出／差入完整路線比較（需情境 JSON）",
    OfflineFixedRunPlan.NIA_PRO_MONTE_CARLO: "NIA Pro 指定週 RNG 批次比較",
}

_OFFLINE_FIXED_RUN_SCENARIO_LABELS = {
    OfflineFixedRunPlan.PLAN1: "fktn-plan1-deterministic-server-smoke",
    OfflineFixedRunPlan.PLAN2: "fktn-plan2-deterministic-server-smoke",
    OfflineFixedRunPlan.PLAN2_AGGRESSIVE: (
        "fktn-plan2-aggressive-deterministic-server-smoke"
    ),
    OfflineFixedRunPlan.PLAN3: "fktn-plan3-deterministic-server-smoke",
    OfflineFixedRunPlan.NIA_PRO_MID1: (
        "nia-pro-produce004-mid1-single-outer-section"
    ),
    OfflineFixedRunPlan.NIA_PRO_FULL: "nia-pro-fktn-plan3-w1-final-fixed",
    OfflineFixedRunPlan.NIA_MASTER_PREFIX: NIA_MASTER_OUTER_SCAFFOLD_LABEL,
    OfflineFixedRunPlan.NIA_MASTER_INNER: "nia-master-fktn-three-inner-stages",
    OfflineFixedRunPlan.NIA_PRO_LESSON_COMPARE: "nia-pro-self-lesson-attribute-full-route-comparison",
    OfflineFixedRunPlan.NIA_PRO_WEEKLY_COMPARE: "nia-pro-outing-care-package-full-route-comparison",
    OfflineFixedRunPlan.NIA_PRO_MONTE_CARLO: "nia-pro-self-lesson-common-rng-monte-carlo",
}


@dataclass(frozen=True, slots=True)
class Plan2OfflineSimulatorRequest:
    source: Plan2OfflineSimulatorSource = Plan2OfflineSimulatorSource.MASTER_INITIAL
    exam_save_path: str = ""
    idol_card_id: str = "i_card-fktn-3-007"
    random_state: int = 0x12345678
    limit_turn: int = 3
    stamina: int = 27
    max_stamina: int = 27
    draw_count: int = 3
    hand_limit: int = 5
    max_actions: int = 30
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(
        max_depth=3,
        beam_width=6,
        max_nodes=2_000,
    )
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights()
    database: Path = DEFAULT_DATABASE
    master_dir: Path = PLAN2_DEFAULT_MASTER_DIR
    fixed_run_plan: OfflineFixedRunPlan = OfflineFixedRunPlan.PLAN2
    fixed_run_scenario_path: str = ""
    nia_compare_week: int = 1
    nia_monte_carlo_samples: int = 4
    nia_monte_carlo_root_seed: int = 0xC0C0BABA
    # Optional caller-owned Android ProduceSchedule JSON.  These trailing
    # fields preserve every existing positional constructor while keeping the
    # runtime route independent from the fixed scenario path.
    nia_runtime_schedule_path: str = ""
    # Alias accepted for callers that name the projection as a route.  Both
    # fields are normalized to the same value in __post_init__.
    nia_runtime_route_path: str = ""
    # Strict typed response bundle.  It remains independent from both the v1
    # fixed route JSON and the optional ProduceSchedule JSON.
    nia_runtime_response_path: str = ""
    # Optional caller-authoritative Plan1 per-stage runtime loadout.  This is
    # deliberately trailing so existing positional constructors stay valid.
    plan1_run_runtime_loadout_path: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source, Plan2OfflineSimulatorSource):
            raise TypeError("source must be Plan2OfflineSimulatorSource")
        if not isinstance(self.fixed_run_plan, OfflineFixedRunPlan):
            raise TypeError("fixed_run_plan must be OfflineFixedRunPlan")
        if type(self.exam_save_path) is not str:
            raise TypeError("exam_save_path must be text")
        if type(self.fixed_run_scenario_path) is not str:
            raise TypeError("fixed_run_scenario_path must be text")
        if type(self.nia_runtime_schedule_path) is not str:
            raise TypeError("nia_runtime_schedule_path must be text")
        if type(self.nia_runtime_route_path) is not str:
            raise TypeError("nia_runtime_route_path must be text")
        if type(self.nia_runtime_response_path) is not str:
            raise TypeError("nia_runtime_response_path must be text")
        if type(self.plan1_run_runtime_loadout_path) is not str:
            raise TypeError("plan1_run_runtime_loadout_path must be text")
        schedule_path = self.nia_runtime_schedule_path.strip()
        route_path = self.nia_runtime_route_path.strip()
        if schedule_path and route_path and schedule_path != route_path:
            raise ValueError(
                "nia_runtime_schedule_path and nia_runtime_route_path must match"
            )
        normalized_runtime_path = schedule_path or route_path
        object.__setattr__(self, "nia_runtime_schedule_path", normalized_runtime_path)
        object.__setattr__(self, "nia_runtime_route_path", normalized_runtime_path)
        object.__setattr__(
            self,
            "nia_runtime_response_path",
            self.nia_runtime_response_path.strip(),
        )
        object.__setattr__(
            self,
            "plan1_run_runtime_loadout_path",
            self.plan1_run_runtime_loadout_path.strip(),
        )
        if type(self.nia_compare_week) is not int or not 1 <= self.nia_compare_week <= 27:
            raise ValueError("nia_compare_week must be between 1 and 27")
        if (
            type(self.nia_monte_carlo_samples) is not int
            or not 1 <= self.nia_monte_carlo_samples <= 4096
        ):
            raise ValueError("nia_monte_carlo_samples must be between 1 and 4096")
        if (
            type(self.nia_monte_carlo_root_seed) is not int
            or not 0 <= self.nia_monte_carlo_root_seed <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ValueError("nia_monte_carlo_root_seed must be UInt64")
        if type(self.idol_card_id) is not str or not self.idol_card_id:
            raise ValueError("idol_card_id must be non-empty text")
        for name in (
            "random_state",
            "limit_turn",
            "stamina",
            "max_stamina",
            "draw_count",
            "hand_limit",
            "max_actions",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.random_state > 0xFFFFFFFF:
            raise ValueError("random_state is outside UInt32")
        if self.limit_turn < 1 or self.hand_limit < 1 or self.max_actions < 1:
            raise ValueError("turn, hand, and action limits must be positive")
        if self.stamina > self.max_stamina:
            raise ValueError("stamina cannot exceed max_stamina")
        if self.draw_count > self.hand_limit:
            raise ValueError("draw_count cannot exceed hand_limit")
        if not isinstance(self.limits, Plan2NativeExpectimaxLimits):
            raise TypeError("limits must be Plan2NativeExpectimaxLimits")
        if not isinstance(self.weights, Plan2NativeEvaluationWeights):
            raise TypeError("weights must be Plan2NativeEvaluationWeights")
        object.__setattr__(self, "database", Path(self.database))
        object.__setattr__(self, "master_dir", Path(self.master_dir))

    @property
    def source_label(self) -> str:
        return _PLAN2_SOURCE_LABELS[self.source]

    @property
    def decision_ready(self) -> bool:
        return (
            self.source == Plan2OfflineSimulatorSource.MASTER_INITIAL
            or bool(self.exam_save_path.strip())
        )

    @property
    def fixed_run_plan_label(self) -> str:
        return _OFFLINE_FIXED_RUN_PLAN_LABELS[self.fixed_run_plan]


@dataclass(frozen=True, slots=True)
class Plan2OfflineSimulatorView:
    phase: InitialRegularSimulatorPhase
    source_text: str
    coverage_text: str
    state_text: str
    status_text: str
    recommended_action_text: str
    predicted_path_text: str
    blockers_text: str
    self_play_summary_text: str
    decision_enabled: bool
    self_play_enabled: bool
    technical_details: tuple[str, ...] = ()
    # The exact deck projection is optional so all existing callers that build
    # the view positionally remain compatible.  It is derived from the result
    # state already held by this view-model; no additional file read or solver
    # invocation occurs.
    deck_snapshot: Plan2DeckSnapshot | None = None
    deck_snapshot_summary: str = ""
    # A complete Initial Regular outer-run report may be injected by a
    # caller that already owns the run.  Keeping this last and optional
    # preserves all existing positional constructors and runner semantics.
    fixed_run_report: InitialRegularFixedRunReport | None = None


Plan2DecisionRunner: TypeAlias = Callable[
    [Plan2OfflineSimulatorRequest],
    Plan2NativeExamSaveDecision | Plan2NativeSelfPlayResult,
]
Plan2SelfPlayRunner: TypeAlias = Callable[
    [Plan2OfflineSimulatorRequest], Plan2NativeSelfPlayResult
]
Plan2FixedRunRunner: TypeAlias = Callable[
    [Plan2OfflineSimulatorRequest],
    InitialRegularFixedRunReport
    | NiaMasterOuterScaffoldReport
    | NiaMasterInnerAcceptanceResult
    | NiaFullScenarioComparison
    | NiaMonteCarloComparison,
]
Plan2OfflineSimulatorResult: TypeAlias = (
    Plan2NativeExamSaveDecision
    | Plan2NativeSelfPlayResult
    | InitialRegularFixedRunReport
    | NiaMasterOuterScaffoldReport
    | NiaMasterInnerAcceptanceResult
    | NiaFullScenarioComparison
    | NiaMonteCarloComparison
)


@dataclass(frozen=True, slots=True)
class _Plan2WorkerMessage:
    generation: int
    operation: Plan2OfflineSimulatorOperation
    result: Plan2OfflineSimulatorResult | None = None
    error: Exception | None = None


def _run_plan2_offline_decision(
    request: Plan2OfflineSimulatorRequest,
) -> Plan2NativeExamSaveDecision | Plan2NativeSelfPlayResult:
    if request.source == Plan2OfflineSimulatorSource.EXAM_SAVE_DATA:
        return decide_plan2_native_exam_save(
            request.exam_save_path.strip(),
            draw_count=request.draw_count,
            hand_limit=request.hand_limit,
            limits=request.limits,
            weights=request.weights,
            database=request.database,
        )
    return _run_plan2_fixed_seed_self_play(request)


def _run_plan2_fixed_seed_self_play(
    request: Plan2OfflineSimulatorRequest,
) -> Plan2NativeSelfPlayResult:
    return run_plan2_native_self_play_acceptance(
        request.idol_card_id,
        random_state=request.random_state,
        limit_turn=request.limit_turn,
        stamina=request.stamina,
        max_stamina=request.max_stamina,
        draw_count=request.draw_count,
        hand_limit=request.hand_limit,
        max_actions=request.max_actions,
        limits=request.limits,
        weights=request.weights,
        database=request.database,
        master_dir=request.master_dir,
    )


class NiaRuntimeScheduleInputError(ValueError):
    """Typed error for an unreadable caller-owned ProduceSchedule JSON file."""

    code = "nia-runtime-schedule-json-invalid"

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"{self.code}: {detail}")


class Plan1RunRuntimeLoadoutInputError(ValueError):
    """GUI-facing typed failure for a strict Plan1 run loadout bundle."""

    def __init__(self, code: str, field: str, detail: str = "") -> None:
        self.code = str(code)
        self.field = str(field)
        self.detail = str(detail)
        suffix = f"：{self.detail}" if self.detail else ""
        super().__init__(
            "Plan1 執行期 loadout 無法載入或套用："
            f"{self.code} [{self.field}]{suffix}"
        )


class NiaRuntimeResponseInputError(ValueError):
    """GUI-facing typed failure for a strict runtime response bundle."""

    def __init__(self, code: str, field: str, detail: str = "") -> None:
        self.code = code
        self.field = field
        self.detail = detail
        suffix = f"：{detail}" if detail else ""
        super().__init__(
            f"{code}：NIA 執行回應 bundle 無法載入 [{field}]{suffix}"
        )


class NiaRuntimeScenarioBindingError(ValueError):
    """Raised before the solver when a runtime route cannot be bound safely."""

    code = "nia-runtime-scenario-binding-blocked"

    def __init__(self, binding: Any) -> None:
        self.binding = binding
        issues = tuple(getattr(binding, "issues", ()))
        if issues:
            first = issues[0]
            week = "" if first.week is None else f" W{first.week}"
            detail = f"{first.code} [{first.field}]{week}: {first.detail}"
        else:
            detail = "runtime route is not executable"
        super().__init__(
            f"{self.code}: NIA runtime route 無法執行；{detail}。"
        )


def _load_plan1_run_runtime_loadout_for_gui(
    request: Plan2OfflineSimulatorRequest,
) -> Any | None:
    """Strictly load the optional Plan1 bundle once inside the worker."""

    path = request.plan1_run_runtime_loadout_path
    if not path:
        return None
    from .initial_regular_plan1_run_runtime_loadout import (
        Plan1RunRuntimeLoadoutError,
        load_plan1_run_runtime_loadout_bundle,
    )

    try:
        bundle = load_plan1_run_runtime_loadout_bundle(path)
    except Plan1RunRuntimeLoadoutError as error:
        raise Plan1RunRuntimeLoadoutInputError(
            error.code,
            error.field,
            error.detail,
        ) from error
    except (OSError, UnicodeError) as error:
        raise Plan1RunRuntimeLoadoutInputError(
            "plan1-run-loadout-file-unreadable",
            "plan1_run_runtime_loadout_path",
            f"{type(error).__name__}: {error}",
        ) from error

    expected = (("produce_id", "produce-001"), ("character_id", "fktn"))
    for field, value in expected:
        actual = getattr(bundle, field)
        if actual != value:
            raise Plan1RunRuntimeLoadoutInputError(
                f"plan1-run-loadout-{field.removesuffix('_id')}-mismatch",
                field,
                f"固定情境={value}；bundle={actual}",
            )
    return bundle


def _load_nia_runtime_schedule_records(
    path: str,
) -> tuple[Mapping[str, object], ...]:
    """Read explicit ProduceSchedule records supplied by the GUI caller."""

    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NiaRuntimeScheduleInputError(f"無法讀取 {target}: {exc}") from exc

    # Accept a bare repeated-record list and common envelope names used by
    # Android dumps.  No values are synthesized when the envelope is absent.
    if isinstance(payload, Mapping):
        candidate: object | None = None
        for key in (
            "records",
            "schedule",
            "steps",
            "produceSchedule",
            "ProduceSchedule",
        ):
            if key in payload:
                candidate = payload[key]
                break
        if candidate is None:
            raise NiaRuntimeScheduleInputError(
                "JSON 必須包含 records／schedule／steps／ProduceSchedule 陣列"
            )
        payload = candidate
    if not isinstance(payload, list):
        raise NiaRuntimeScheduleInputError("ProduceSchedule 必須是 JSON 陣列")
    records: list[Mapping[str, object]] = []
    for index, value in enumerate(payload):
        if not isinstance(value, Mapping):
            raise NiaRuntimeScheduleInputError(
                f"ProduceSchedule 第 {index + 1} 筆不是 object"
            )
        records.append(value)
    return tuple(records)


def _load_nia_runtime_response_bundle_for_gui(
    request: Plan2OfflineSimulatorRequest,
    *,
    expected_produce_id: str = "produce-004",
) -> Any | None:
    """Load the optional typed response bundle exactly once in the worker."""

    response_path = request.nia_runtime_response_path
    if not response_path:
        return None
    from .nia_runtime_response_bundle import (
        NiaRuntimeResponseBundleError,
        load_nia_runtime_response_bundle,
    )

    try:
        return load_nia_runtime_response_bundle(
            response_path,
            expected_produce_id=expected_produce_id,
        )
    except NiaRuntimeResponseBundleError as error:
        raise NiaRuntimeResponseInputError(
            error.code,
            error.field,
            error.detail,
        ) from error


def _build_nia_master_prefix_initial_state(
    request: Plan2OfflineSimulatorRequest,
    inventory: Any,
) -> ProduceRolloutState:
    """Build the explicitly synthetic W1 outer shadow used by the GUI plan.

    The response-bundle v1 schema intentionally has no global initial-state
    field.  Consequently the GUI entry point is bounded to W1 and uses only
    Master profile/deck identities plus deterministic synthetic GUIDs.  It is
    not a LocalSave projection; response post-states remain caller authority.
    """

    from .master_db import get_idol_profile
    from .nia_inner_terminal_acceptance import NIA_FKTN_UNIQUE_CARD_UPGRADE
    from .plan3_engine import load_plan3_initial_deck
    from .produce_rollout import AttributeValues, DeckEntry, RolloutPhase

    profile = get_idol_profile(inventory.idol_card_id, request.database)
    if profile is None or not profile.produce_card_id:
        raise NiaRuntimeScheduleInputError(
            "NIA Master prefix 找不到 FKTN Master 初始 profile／專屬卡。"
        )
    manifest = load_plan3_initial_deck(
        produce_id="produce-005",
        database=request.database,
    )
    refs = (
        *((value.card_id, value.upgrade) for value in manifest.cards),
        (profile.produce_card_id, NIA_FKTN_UNIQUE_CARD_UPGRADE),
    )
    deck: list[DeckEntry] = []
    for index, (card_id, upgrade) in enumerate(refs, start=1):
        guid = f"nia-master-prefix:synthetic-guid:{index:02d}"
        matching = next(
            (
                position
                for position, entry in enumerate(deck)
                if (entry.card_id, entry.upgrade) == (card_id, upgrade)
            ),
            None,
        )
        if matching is None:
            deck.append(
                DeckEntry(card_id, upgrade, instance_ids=(guid,))
            )
        else:
            entry = deck[matching]
            deck[matching] = DeckEntry(
                entry.card_id,
                entry.upgrade,
                entry.count + 1,
                (*entry.instance_ids, guid),
            )
    return ProduceRolloutState(
        mode_id="produce-005",
        character_id=profile.character_id,
        week=1,
        total_weeks=inventory.total_weeks,
        phase=RolloutPhase.READY_FOR_WEEK,
        stamina=profile.stamina,
        max_stamina=profile.stamina,
        produce_points=inventory.setting.initial_produce_point,
        attributes=AttributeValues(profile.vocal, profile.dance, profile.visual),
        deck=tuple(deck),
    )


def _run_nia_master_prefix_from_gui_inputs(
    request: Plan2OfflineSimulatorRequest,
) -> NiaMasterOuterScaffoldReport:
    """Strictly join caller schedule/bundle inputs and run the p5 prefix."""

    from .initial_regular_nia_master_customize_checkpoint import (
        NIA_MASTER_FKTN_IDOL_CARD_ID,
        normalize_nia_master_customize_schedule_week,
    )
    from .initial_regular_nia_master_outer_scaffold import (
        NiaMasterOuterScaffoldScenario,
        NiaMasterOuterWeekScenario,
        run_nia_master_schedule_outer_scaffold,
    )
    from .nia_static_inventory import build_nia_static_inventory
    from .nia_outer_action_runtime import NiaCarePackageScenario

    schedule_path = request.nia_runtime_schedule_path.strip()
    if not schedule_path:
        raise NiaRuntimeScheduleInputError(
            "NIA Master prefix 必須提供 caller-owned ProduceSchedule JSON。"
        )
    if not request.nia_runtime_response_path:
        raise NiaRuntimeResponseInputError(
            "nia-master-prefix-response-bundle-required",
            "nia_runtime_response_path",
            "NIA Master prefix 必須提供 produce-005 typed response bundle",
        )
    records = _load_nia_runtime_schedule_records(schedule_path)
    if not records:
        raise NiaRuntimeScheduleInputError(
            "NIA Master prefix ProduceSchedule 不可為空。"
        )
    bundle = _load_nia_runtime_response_bundle_for_gui(
        request,
        expected_produce_id="produce-005",
    )
    assert bundle is not None
    try:
        schedules = tuple(
            normalize_nia_master_customize_schedule_week(record)
            for record in records
        )
    except (TypeError, ValueError, KeyError) as error:
        raise NiaRuntimeScheduleInputError(
            f"NIA Master prefix ProduceSchedule 無法正規化：{error}"
        ) from error
    if schedules[0].number != 1:
        raise NiaRuntimeScheduleInputError(
            "response bundle v1 沒有全域 initial_state；GUI Master prefix 僅能從 W1 起跑。"
        )

    schedule_weeks = {value.number for value in schedules}
    response_entries: dict[int, list[tuple[str, object]]] = {}
    for action_name, scenarios in bundle.scenario_maps.items():
        for week, scenario in scenarios.items():
            if action_name == "fan_present":
                scenario = NiaCarePackageScenario(
                    scenario,
                    f"{bundle.authority.runtime_ref}:w{week}:fan-present",
                )
            response_entries.setdefault(week, []).append((action_name, scenario))
    extra_weeks = sorted(set(response_entries) - schedule_weeks)
    if extra_weeks:
        raise NiaRuntimeResponseInputError(
            "nia-master-prefix-response-extra",
            "responses",
            f"bundle 含不在 prefix schedule 的週：{extra_weeks}",
        )
    ambiguous = {
        week: tuple(name for name, _scenario in entries)
        for week, entries in response_entries.items()
        if len(entries) > 1
    }
    if ambiguous:
        raise NiaRuntimeResponseInputError(
            "nia-master-prefix-response-ambiguous",
            "responses",
            repr(ambiguous),
        )

    weeks = tuple(
        NiaMasterOuterWeekScenario(
            schedule,
            (
                None
                if schedule.number not in response_entries
                else response_entries[schedule.number][0][1]
            ),
        )
        for schedule in schedules
    )
    inventory = build_nia_static_inventory(
        NIA_MASTER_FKTN_IDOL_CARD_ID,
        produce_id="produce-005",
        database=request.database,
        master_dir=request.master_dir,
    )
    initial_state = _build_nia_master_prefix_initial_state(request, inventory)
    try:
        scenario = NiaMasterOuterScaffoldScenario(
            initial_state=initial_state,
            weeks=weeks,
            schedule_authority_ref=(
                f"caller ProduceSchedule={schedule_path}; "
                f"typed bundle={bundle.authority.runtime_ref}; "
                "GUI W1 outer shadow=PC Master identities plus synthetic GUIDs"
            ),
        )
    except (TypeError, ValueError) as error:
        raise NiaRuntimeScheduleInputError(
            f"NIA Master prefix 必須是從 W1 起連續的 schedule：{error}"
        ) from error
    return run_nia_master_schedule_outer_scaffold(
        scenario,
        inventory=inventory,
        database=request.database,
        master_dir=request.master_dir,
    )


def _bind_nia_runtime_schedule_for_gui(
    *,
    request: Plan2OfflineSimulatorRequest,
    scenario: Any,
    response_bundle: Any | None = None,
) -> Any | None:
    """Project and bind an optional runtime schedule without executing it."""

    runtime_path = request.nia_runtime_schedule_path.strip()
    if not runtime_path:
        return None
    from .nia_runtime_route import project_nia_runtime_route
    from .nia_runtime_scenario_binding import (
        bind_nia_full_route_scenario_to_runtime_route,
    )

    records = _load_nia_runtime_schedule_records(runtime_path)
    route = project_nia_runtime_route(
        records,
        produce_id="produce-004",
        master_dir=request.master_dir,
    )
    response_maps = (
        {}
        if response_bundle is None
        else {
            "self_lesson_scenarios": response_bundle.self_lesson_scenarios,
            "business_scenarios": response_bundle.business_scenarios,
            "customize_scenarios": response_bundle.customize_scenarios,
            "event_business_scenarios": (
                response_bundle.event_business_scenarios
            ),
            "interval_scenarios": response_bundle.interval_scenarios,
            "refresh_scenarios": response_bundle.refresh_scenarios,
            "fan_present_scenarios": response_bundle.fan_present_scenarios,
        }
    )
    binding = bind_nia_full_route_scenario_to_runtime_route(
        scenario,
        route,
        **response_maps,
    )
    if not binding.executable:
        raise NiaRuntimeScenarioBindingError(binding)
    return binding


def _run_plan2_fixed_whole_run(
    request: Plan2OfflineSimulatorRequest,
) -> (
    InitialRegularFixedRunReport
    | NiaMasterOuterScaffoldReport
    | NiaMasterInnerAcceptanceResult
    | NiaFullScenarioComparison
    | NiaMonteCarloComparison
):
    """Run one explicit deterministic acceptance and return its GUI report."""

    if request.fixed_run_plan is OfflineFixedRunPlan.PLAN1:
        from .initial_regular_plan1_fixed_scenario import (
            run_fktn_plan1_w1_to_final_fixed_scenario,
        )
        from .plan1_native_catalog import compile_plan1_native_catalog

        run_runtime_loadout = _load_plan1_run_runtime_loadout_for_gui(request)
        catalog = compile_plan1_native_catalog(database=request.database)
        result = run_fktn_plan1_w1_to_final_fixed_scenario(
            master_dir=request.master_dir,
            run_runtime_loadout=run_runtime_loadout,
        )
        milestones = []
        for stage, resolution, outcome in (
            ("Mid1", result.mid1_resolution, result.mid1_outcome),
            ("Final", result.final_resolution, result.final_outcome),
        ):
            if resolution is not None and outcome is not None:
                milestones.append(
                    InitialRegularFixedRunMilestoneReport(
                        stage,
                        resolution.player_score,
                        resolution.rank,
                        outcome.cleared,
                    )
                )
        return replace(
            result.fixed_run_report,
            milestones=tuple(milestones),
            card_coverage=InitialRegularFixedRunCardCoverageReport(
                scope=(
                    "Common＋Plan1；stage runtime authority=caller bundle"
                    if run_runtime_loadout is not None
                    else (
                        "Common＋Plan1；stage runtime authority="
                        "synthetic fixed smoke"
                    )
                ),
                executable_versions=catalog.executable_version_count,
                total_versions=catalog.total_version_count,
                blocked_versions=catalog.blocked_version_count,
            ),
        )

    if request.fixed_run_plan is OfflineFixedRunPlan.PLAN3:
        from .initial_regular_plan3_fixed_scenario import (
            run_fktn_plan3_deterministic_w1_final,
        )

        result = run_fktn_plan3_deterministic_w1_final()
        audition_outcomes = tuple(
            value for value in result.inner_outcomes if value.rank is not None
        )
        milestones = tuple(
            InitialRegularFixedRunMilestoneReport(
                stage,
                outcome.score or 0,
                outcome.rank,
                outcome.cleared,
            )
            for stage, outcome in zip(
                ("Mid1", "Final"), audition_outcomes, strict=True
            )
        )
        return replace(result.report, milestones=milestones)

    if request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_MID1:
        from .initial_regular_nia_fixed_scenario import (
            run_nia_produce004_mid1_fixed_scenario,
        )

        result = run_nia_produce004_mid1_fixed_scenario()
        milestone = InitialRegularFixedRunMilestoneReport(
            "NIA Pro Mid1",
            result.resolution.player_score,
            result.resolution.rank,
            result.resolution.cleared,
        )
        return replace(result.report, milestones=(milestone,))

    if request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_FULL:
        from .nia_full_fixed_scenario import (
            build_nia_produce004_fktn_default_full_scenario,
            load_nia_full_route_scenario,
            run_nia_produce004_fktn_full_fixed_scenario,
        )

        scenario_path = request.fixed_run_scenario_path.strip()
        runtime_path = request.nia_runtime_schedule_path.strip()
        response_bundle = _load_nia_runtime_response_bundle_for_gui(request)
        # A supplied runtime route must bind to a concrete scenario before
        # entering the runner.  A response bundle without a schedule still
        # binds through the full runner's custom fixed route.  With neither
        # input, preserve the existing built-in behavior exactly.
        scenario = None
        if scenario_path:
            scenario = load_nia_full_route_scenario(scenario_path)
        elif runtime_path or response_bundle is not None:
            scenario = build_nia_produce004_fktn_default_full_scenario(
                database=request.database,
                master_dir=request.master_dir,
            )
        if runtime_path:
            _bind_nia_runtime_schedule_for_gui(
                request=request,
                scenario=scenario,
                response_bundle=response_bundle,
            )
        response_maps = (
            {}
            if response_bundle is None
            else {
                "self_lesson_response_scenarios": (
                    response_bundle.self_lesson_scenarios
                ),
                "business_scenarios": response_bundle.business_scenarios,
                "customize_scenarios": response_bundle.customize_scenarios,
                "event_business_scenarios": (
                    response_bundle.event_business_scenarios
                ),
                "interval_scenarios": response_bundle.interval_scenarios,
                "refresh_scenarios": response_bundle.refresh_scenarios,
                "fan_present_scenarios": (
                    response_bundle.fan_present_scenarios
                ),
                "strict_runtime_response_bundle": True,
            }
        )
        result = run_nia_produce004_fktn_full_fixed_scenario(
            scenario=scenario,
            database=request.database,
            master_dir=request.master_dir,
            **response_maps,
        )
        stage_labels = {
            MID1: "NIA Pro Mid1",
            MID2: "NIA Pro Mid2",
            FINAL: "NIA Pro Final",
        }
        milestones = tuple(
            InitialRegularFixedRunMilestoneReport(
                stage_labels[record.catalog.step_type],
                record.resolution.player_score,
                record.resolution.rank,
                record.resolution.cleared,
            )
            for record in result.stages
        )
        return replace(result.report, milestones=milestones)

    if request.fixed_run_plan is OfflineFixedRunPlan.NIA_MASTER_PREFIX:
        return _run_nia_master_prefix_from_gui_inputs(request)

    if request.fixed_run_plan is OfflineFixedRunPlan.NIA_MASTER_INNER:
        from .nia_master_inner_acceptance import (
            run_nia_master_fktn_inner_acceptance,
        )

        return run_nia_master_fktn_inner_acceptance(
            database=request.database,
            master_dir=request.master_dir,
        )

    if request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_LESSON_COMPARE:
        from .nia_full_fixed_scenario import (
            build_nia_produce004_fktn_default_full_scenario,
            load_nia_full_route_scenario,
        )
        from .nia_full_scenario_comparison import (
            build_nia_self_lesson_attribute_candidates,
            compare_nia_full_route_scenarios,
        )

        scenario_path = request.fixed_run_scenario_path.strip()
        base = (
            build_nia_produce004_fktn_default_full_scenario(
                database=request.database,
                master_dir=request.master_dir,
            )
            if not scenario_path
            else load_nia_full_route_scenario(scenario_path)
        )
        return compare_nia_full_route_scenarios(
            build_nia_self_lesson_attribute_candidates(
                base,
                week=request.nia_compare_week,
            ),
            database=request.database,
            master_dir=request.master_dir,
        )

    if request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_WEEKLY_COMPARE:
        from .nia_full_fixed_scenario import load_nia_full_route_scenario
        from .nia_full_scenario_comparison import (
            build_nia_outing_care_package_candidates,
            compare_nia_full_route_scenarios,
        )

        scenario_path = request.fixed_run_scenario_path.strip()
        if not scenario_path:
            raise ValueError(
                "外出／差入比較需要含 exact Present response 的情境 JSON"
            )
        base = load_nia_full_route_scenario(scenario_path)
        return compare_nia_full_route_scenarios(
            build_nia_outing_care_package_candidates(
                base,
                week=request.nia_compare_week,
                master_dir=request.master_dir,
            ),
            database=request.database,
            master_dir=request.master_dir,
        )

    if request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_MONTE_CARLO:
        from .nia_full_fixed_scenario import (
            build_nia_produce004_fktn_default_full_scenario,
            load_nia_full_route_scenario,
        )
        from .nia_full_monte_carlo import sample_nia_full_route_candidates
        from .nia_full_scenario_comparison import (
            build_nia_self_lesson_attribute_candidates,
        )

        scenario_path = request.fixed_run_scenario_path.strip()
        base = (
            build_nia_produce004_fktn_default_full_scenario(
                database=request.database,
                master_dir=request.master_dir,
            )
            if not scenario_path
            else load_nia_full_route_scenario(scenario_path)
        )
        return sample_nia_full_route_candidates(
            build_nia_self_lesson_attribute_candidates(
                base,
                week=request.nia_compare_week,
            ),
            sample_count=request.nia_monte_carlo_samples,
            root_seed=request.nia_monte_carlo_root_seed,
            database=request.database,
            master_dir=request.master_dir,
        )

    if request.fixed_run_plan is OfflineFixedRunPlan.PLAN2_AGGRESSIVE:
        # Keep the aggressive/block starter, rewards, item runtime, gimmick,
        # evaluator and deterministic server branch together as one explicit
        # fixture.  It must not silently reuse the Review profile.
        from .initial_regular_plan2_fixed_scenario import (
            run_fktn_plan2_aggressive_deterministic_smoke,
        )

        result = run_fktn_plan2_aggressive_deterministic_smoke(
            limits=request.limits,
        )
        milestones = tuple(
            InitialRegularFixedRunMilestoneReport(
                (
                    value.catalog.stage_type
                    if value.catalog is not None
                    else f"Audition {index}"
                ),
                value.player_score or 0,
                None if value.resolution is None else value.resolution.rank,
                (
                    value.resolution is not None
                    and value.catalog is not None
                    and value.resolution.rank <= value.catalog.clear_border
                ),
            )
            for index, value in enumerate(result.audition_results, start=1)
        )
        return replace(result.report, milestones=milestones)

    if request.idol_card_id != "i_card-fktn-3-007":
        raise ValueError("完整培育 smoke 目前只支援 i_card-fktn-3-007")
    # Import lazily so ordinary single-stage GUI use does not load the whole
    # outer-scenario assembly or its Master catalogs.
    from .initial_regular_plan2_fixed_scenario import (
        run_fktn_plan2_deterministic_smoke,
    )

    result = run_fktn_plan2_deterministic_smoke()
    milestones = tuple(
        InitialRegularFixedRunMilestoneReport(
            (
                value.catalog.stage_type
                if value.catalog is not None
                else f"Audition {index}"
            ),
            value.player_score or 0,
            None if value.resolution is None else value.resolution.rank,
            (
                value.resolution is not None
                and value.catalog is not None
                and value.resolution.rank <= value.catalog.clear_border
            ),
        )
        for index, value in enumerate(result.audition_results, start=1)
    )
    return replace(result.report, milestones=milestones)


def _plan2_action_text(action: Plan2NativeAction | None, *, terminal: bool) -> str:
    if terminal:
        return "目前已是終局"
    if action is None:
        return "尚未產生推薦動作"
    if action.kind == "end_turn":
        return "推薦：結束回合"
    return f"推薦：出牌（GUID：{action.card_guid}）"


def _plan2_state_text(state: Plan2NativeHorizonState | None) -> str:
    if state is None:
        return "尚未載入可模擬的狀態"
    return (
        f"第 {state.scalar.current_turn}/{state.limit_turn} 回合｜"
        f"分數 {state.scalar.score}｜"
        f"體力 {state.scalar.stamina}/{state.scalar.max_stamina}"
    )


def _plan2_coverage_text(
    *,
    universe: int,
    compiled: int,
    failed: int,
    current_ready: bool,
    missing_count: int,
) -> str:
    current = "目前牌組可用" if current_ready else f"目前牌組缺少 {missing_count} 種卡牌程式"
    return f"卡牌程式：{current}｜全域 {compiled}/{universe}（未涵蓋 {failed}）"


def _plan2_blocker_lines(values) -> tuple[str, ...]:
    return tuple(
        f"[{value.code}]" + (f" {value.detail}" if value.detail else "")
        for value in values
    )


def _plan2_snapshot_for_result(
    result: Plan2NativeExamSaveDecision | Plan2NativeSelfPlayResult,
) -> Plan2DeckSnapshot | None:
    """Project only the already-returned result state for display."""

    if isinstance(result, Plan2NativeExamSaveDecision):
        if result.bootstrap is None:
            return None
        # The bootstrap envelope carries typed blockers and, when accepted,
        # the exact settled Horizon state.  The deck snapshot bridge accepts
        # that envelope directly and renders a blocked empty view when the
        # state is unavailable.
        return build_plan2_deck_snapshot(
            result.bootstrap,
            blockers=result.blockers,
        )
    if isinstance(result, Plan2NativeSelfPlayResult):
        if result.initial_state is None:
            return None
        return build_plan2_deck_snapshot(
            result.initial_state,
            blockers=result.blockers,
        )
    return None


def _plan2_snapshot_summary(snapshot: Plan2DeckSnapshot | None) -> str:
    if snapshot is None:
        return "牌庫快照：尚無資料"
    return (
        "牌庫快照："
        f"卡片 {len(snapshot.card_universe)} 張；"
        f"手牌 {len(snapshot.hand)}；抽牌堆 {len(snapshot.deck)}；"
        f"棄牌堆 {len(snapshot.grave)}；除外 {len(snapshot.lost)}；"
        f"飲料 {len(snapshot.drinks)}；P 道具 {len(snapshot.p_items)}；"
        f"被動 {len(snapshot.passives)}；"
        f"未知 {len(snapshot.unknown)}；阻擋 {len(snapshot.blockers)}"
    )


def _plan2_snapshot_rows(
    snapshot: Plan2DeckSnapshot | None,
) -> tuple[tuple[str, int, str, str, str], ...]:
    """Flatten ordered zones for the small Tk Treeview."""

    if snapshot is None:
        return ()
    rows: list[tuple[str, int, str, str, str]] = []

    def add(zone: str, cards: tuple[Plan2CardSnapshot, ...]) -> None:
        for card in cards:
            upgrade = "?" if card.effective_upgrade is None else str(card.effective_upgrade)
            rows.append((zone, card.zone_order, card.card_id, upgrade, card.guid))

    add("hand", snapshot.hand)
    add("deck", snapshot.deck)
    add("grave", snapshot.grave)
    add("lost", snapshot.lost)
    add("hold", snapshot.hold)
    add("removed", snapshot.removed)
    add("playing", snapshot.playing)
    for index, group in enumerate(snapshot.future_deck):
        add(f"future[{index}]", group)
    if snapshot.past_deck is not None:
        for index, group in enumerate(snapshot.past_deck):
            add(f"past[{index}]", group)
    return tuple(rows)


_PLAN2_SNAPSHOT_ZONE_LABELS = {
    "hand": "手牌",
    "deck": "抽牌堆",
    "grave": "棄牌堆",
    "lost": "除外",
    "hold": "保留",
    "removed": "移除",
    "playing": "結算中",
}


def _plan2_snapshot_zone_label(zone: str) -> str:
    if zone.startswith("future["):
        return "未來牌序" + zone[len("future") :]
    if zone.startswith("past["):
        return "過去牌序" + zone[len("past") :]
    return _PLAN2_SNAPSHOT_ZONE_LABELS.get(zone, zone)


def _plan2_snapshot_aux_summary(snapshot: Plan2DeckSnapshot | None) -> str:
    if snapshot is None:
        return "飲料／P 道具／被動：尚無資料"
    drink_parts = []
    for drink in snapshot.drinks:
        if isinstance(drink, Mapping):
            drink_id = drink.get("_id")
            if isinstance(drink_id, str) and drink_id:
                drink_parts.append(drink_id)
            else:
                drink_parts.append("?")
        else:
            drink_parts.append("?")
    item_parts = []
    for item in snapshot.p_items:
        item_id = item.item_id or "?"
        remaining = "?" if item.remaining_uses is None else str(item.remaining_uses)
        item_parts.append(f"{item_id}:{remaining}")
    passive_parts = []
    for passive in snapshot.passives:
        identity = passive.identity or "?"
        level = "?" if passive.level is None else str(passive.level)
        kind = "記憶" if passive.kind == "memory" else "支援"
        passive_parts.append(f"{kind}:{identity}@{level}")
    extras = (
        f"飲料 {len(snapshot.drinks)} [{', '.join(drink_parts) or '無'}]；"
        f"P 道具 [{', '.join(item_parts) or '無'}]；"
        f"被動 [{', '.join(passive_parts) or '無'}]"
    )
    if snapshot.unknown:
        extras += "；未知：" + ";".join(snapshot.unknown)
    if snapshot.blockers:
        extras += "；阻擋：" + ";".join(snapshot.blockers)
    return extras


_FIXED_RUN_STEP_KIND_LABELS = {
    "decision": "決策",
    "external": "外部結果",
}


def _fixed_run_value(value: object | None) -> str:
    """Render an optional report value without inventing a value."""

    return "未知" if value is None else str(value)


def _plan2_fixed_run_summary(
    report: InitialRegularFixedRunReport | None,
) -> str:
    """Return the non-technical summary shown above the fixed-run table."""

    if report is None:
        return "完整培育流程：尚未執行"
    nia_partial = (
        not report.completed
        and report.stop is InitialRegularFixedRunStop.POLICY
        and not report.issues
        and any(value.stage == "NIA Pro Mid1" for value in report.milestones)
    )
    if nia_partial:
        return (
            "NIA Pro Mid1 單場驗收：已完成；"
            f"停在第 {report.final_state.week} 週可繼續；"
            f"步驟：{len(report.steps)}；問題：無"
        )
    completion = "已完成" if report.completed else "未完成"
    issue_text = "無" if not report.issues else "、".join(
        issue.code for issue in report.issues
    )
    return (
        f"完整培育流程：{completion}；停止：{report.stop.value}；"
        f"步驟：{len(report.steps)}；問題：{issue_text}"
    )


def _plan2_fixed_run_rows(
    report: InitialRegularFixedRunReport | None,
) -> tuple[tuple[str, ...], ...]:
    """Flatten report steps into read-only Treeview rows.

    The helper consumes only scalar values already present in the report.  In
    particular it never asks the rollout kernel to reconstruct a state or
    rerun an external branch.
    """

    if report is None:
        return ()
    rows: list[tuple[str, ...]] = []
    for step in report.steps:
        kind = _FIXED_RUN_STEP_KIND_LABELS.get(step.kind.value, step.kind.value)
        action = (
            f"動作：{step.action}"
            if step.action is not None
            else ""
        )
        request = ""
        if step.request_kind is not None or step.request_id is not None:
            request_kind = _fixed_run_value(
                None if step.request_kind is None else step.request_kind.value
            )
            request_id = _fixed_run_value(step.request_id)
            request = f"request：{request_kind}/{request_id}"
        action_request = "；".join(value for value in (action, request) if value)
        if not action_request:
            action_request = "未知"
        before = step.before
        after = step.after
        attributes = (
            f"Vo {before.vocal}→{after.vocal} / "
            f"Da {before.dance}→{after.dance} / "
            f"Vi {before.visual}→{after.visual}"
        )
        phases = (
            f"{before.phase.value}→{after.phase.value} / "
            f"{before.lifecycle.value}→{after.lifecycle.value}"
        )
        rows.append(
            (
                str(step.week),
                kind,
                action_request,
                _fixed_run_value(step.branch),
                f"{before.stamina}→{after.stamina}",
                attributes,
                f"{before.deck_count}→{after.deck_count}",
                phases,
            )
        )
    return tuple(rows)


def _plan2_fixed_run_deck_rows(
    report: InitialRegularFixedRunReport | None,
) -> tuple[tuple[str, str, str], ...]:
    if report is None:
        return ()
    return tuple(
        (entry.card_id, str(entry.upgrade), str(entry.count))
        for entry in report.final_deck
    )


def _plan2_fixed_run_deck_summary(
    report: InitialRegularFixedRunReport | None,
) -> str:
    if report is None:
        return "最終牌庫：尚未執行"
    stack_count = len(report.final_deck)
    card_count = sum(entry.count for entry in report.final_deck)
    identity_count = sum(entry.identity_complete for entry in report.final_deck)
    identity_cards = sum(len(entry.instance_ids) for entry in report.final_deck)
    return (
        f"最終牌庫：{card_count} 張／{stack_count} 種；"
        f"識別完整 stack {identity_count}/{stack_count}、GUID {identity_cards}/{card_count}；"
        "固定 scenario 的 outer deck"
    )


def _plan2_fixed_run_milestone_summary(
    report: InitialRegularFixedRunReport | None,
) -> str:
    if report is None or not report.milestones:
        return "課程／演出結果：尚無資料"
    parts = []
    for value in report.milestones:
        rank = "" if value.rank is None else f"／第 {value.rank} 名"
        clear = "通過" if value.cleared else "未通過"
        parts.append(f"{value.stage}：{value.score} 分{rank}（{clear}）")
    return "課程／演出結果：" + "；".join(parts)


def _fixed_run_capability_summary(
    plan: OfflineFixedRunPlan,
    report: InitialRegularFixedRunReport | None,
    *,
    nia_runtime_schedule_path: str = "",
    plan1_run_runtime_loadout_path: str = "",
) -> str:
    """Describe fixed/report capability without deriving a new run result."""

    if not isinstance(plan, OfflineFixedRunPlan):
        raise TypeError("plan must be OfflineFixedRunPlan")
    report_state = (
        "尚未執行"
        if report is None
        else f"既有 report completed={'是' if report.completed else '否'}"
    )
    if plan is OfflineFixedRunPlan.PLAN1:
        coverage = None if report is None else report.card_coverage
        runtime_authority = (
            "階段 runtime 權威：caller 提供的 typed run loadout bundle"
            if plan1_run_runtime_loadout_path.strip()
            else "階段 runtime 權威：synthetic fixed smoke（未提供 bundle）"
        )
        coverage_text = (
            "card catalog coverage 尚未附於 report；"
            if coverage is None
            else (
                f"{coverage.scope} card versions "
                f"{coverage.executable_versions}/{coverage.total_versions} 可執行、"
                f"{coverage.blocked_versions} fail-closed；"
            )
        )
        return (
            f"Plan1 目前最新 coverage：{coverage_text}"
            f"{runtime_authority}；"
            "FKTN SSR 初始 Master manifest 11/11 張，含 2 張 compound card；"
            "ordered fixture，非 authentic LocalSave。"
            f"固定結果：{report_state}；GUI 僅純渲染 report。"
        )
    if plan is OfflineFixedRunPlan.PLAN2:
        return (
            "Plan2 好印象（Campus mode!!／FKTN）：Common＋Plan2 native card "
            "catalog 709/709 可執行；固定／report 可純渲染；"
            f"結果狀態：{report_state}。"
        )
    if plan is OfflineFixedRunPlan.PLAN2_AGGRESSIVE:
        return (
            "Plan2 元氣／幹勁（冠菊／FKTN）：Common＋Plan2 native card "
            "catalog 709/709 可執行；固定／report 可純渲染；"
            f"結果狀態：{report_state}。"
        )
    if plan is OfflineFixedRunPlan.PLAN3:
        return (
            "Plan3 指針（FKTN）：Common＋Plan3 card versions 582/582 "
            "單步可執行；whole-run 仍是固定 scenario；"
            "fixed/report 僅純渲染既有步驟；"
            f"完成狀態不由 GUI 推定（{report_state}）。"
        )
    if plan is OfflineFixedRunPlan.NIA_PRO_MID1:
        return (
            "NIA Pro Mid1：部分流程（非 27 週整場）；"
            "非 authentic NIA LocalSave；GUI 不補造 completed。"
        )
    if plan is OfflineFixedRunPlan.NIA_PRO_FULL:
        runtime_route_text = (
            "已提供 runtime ProduceSchedule，執行前完成 route binding 驗證。"
            if nia_runtime_schedule_path.strip()
            else "未提供 runtime ProduceSchedule；固定 scenario 尚未經 runtime route 驗證。"
        )
        return (
            "NIA Pro 27 週：固定情境／report 可純渲染；"
            "非 authentic NIA LocalSave，completed 僅沿用 report；"
            f"{runtime_route_text}"
        )
    if plan is OfflineFixedRunPlan.NIA_MASTER_PREFIX:
        return (
            "NIA Master caller 回應前綴：只執行提供的 produce-005 "
            "ProduceSchedule＋typed response prefix；W1 初態是 Master-only "
            "synthetic outer shadow，非 authentic LocalSave，亦非 26 週整場。"
        )
    if plan is OfflineFixedRunPlan.NIA_MASTER_INNER:
        return (
            "NIA Master 三演出核心：僅 inner 核心、無外層 26 週；"
            "非 authentic NIA LocalSave。"
        )
    return (
        f"{_OFFLINE_FIXED_RUN_PLAN_LABELS[plan]}：固定情境結果可純渲染；"
        "不重跑 solver、不推定未提供的 runtime。"
    )


def format_plan2_offline_simulator_view(
    request: Plan2OfflineSimulatorRequest,
    *,
    phase: InitialRegularSimulatorPhase | None = None,
    operation: Plan2OfflineSimulatorOperation | None = None,
    result: Plan2OfflineSimulatorResult | None = None,
    error: Exception | None = None,
) -> Plan2OfflineSimulatorView:
    """Build one immutable non-technical Plan2 offline view."""

    if not isinstance(request, Plan2OfflineSimulatorRequest):
        raise TypeError("request must be Plan2OfflineSimulatorRequest")
    if phase is None:
        phase = (
            InitialRegularSimulatorPhase.IDLE
            if request.decision_ready
            else InitialRegularSimulatorPhase.BLOCKED
        )
    busy = phase == InitialRegularSimulatorPhase.RUNNING
    source_text = f"資料來源：{request.source_label}"
    if busy:
        if operation is Plan2OfflineSimulatorOperation.FIXED_RUN and (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_MID1
        ):
            task = "NIA Pro Mid1 單場驗收"
        elif operation is Plan2OfflineSimulatorOperation.FIXED_RUN and (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_FULL
        ):
            task = "NIA Pro 27 週完整固定情境"
        elif operation is Plan2OfflineSimulatorOperation.FIXED_RUN and (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_MASTER_PREFIX
        ):
            task = "NIA Master caller 回應前綴（partial／非 LocalSave）"
        elif operation is Plan2OfflineSimulatorOperation.FIXED_RUN and (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_MASTER_INNER
        ):
            task = "NIA Master 三場玩家端演出驗收"
        elif operation is Plan2OfflineSimulatorOperation.FIXED_RUN and (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_LESSON_COMPARE
        ):
            task = (
                f"NIA Pro W{request.nia_compare_week} "
                "Vo／Da／Vi 完整路線比較"
            )
        elif operation is Plan2OfflineSimulatorOperation.FIXED_RUN and (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_WEEKLY_COMPARE
        ):
            task = (
                f"NIA Pro W{request.nia_compare_week} "
                "外出／差入完整路線比較"
            )
        elif operation is Plan2OfflineSimulatorOperation.FIXED_RUN and (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_MONTE_CARLO
        ):
            task = (
                f"NIA Pro W{request.nia_compare_week} "
                f"RNG 批次比較（每候選 {request.nia_monte_carlo_samples} 樣本）"
            )
        else:
            task = {
                Plan2OfflineSimulatorOperation.SELF_PLAY: "固定 seed self-play 驗收",
                Plan2OfflineSimulatorOperation.FIXED_RUN: "完整 13 週培育 smoke",
            }.get(operation, "離線建議")
        return Plan2OfflineSimulatorView(
            phase,
            source_text,
            "正在背景編譯卡牌程式與搜尋…",
            "正在讀取離線狀態…",
            f"正在計算{task}",
            "計算中",
            "預測路徑：計算中",
            "",
            "視窗仍可操作；不會連線或操作遊戲。",
            False,
            False,
        )
    if error is not None or phase == InitialRegularSimulatorPhase.FAILED:
        detail = "" if error is None else f"{type(error).__name__}: {error}"
        failure_blocker = (
            detail
            if isinstance(
                error,
                (
                    Plan1RunRuntimeLoadoutInputError,
                    NiaRuntimeScenarioBindingError,
                    NiaRuntimeScheduleInputError,
                    NiaRuntimeResponseInputError,
                ),
            )
            else "計算發生錯誤，未改用簡化估算。"
        )
        return Plan2OfflineSimulatorView(
            InitialRegularSimulatorPhase.FAILED,
            source_text,
            "卡牌程式覆蓋：未完成",
            "離線狀態未完成",
            "背景計算失敗",
            "尚未產生推薦動作",
            "預測路徑：無",
            failure_blocker,
            "固定 seed 驗收未完成",
            request.decision_ready,
            True,
            (detail,) if detail else (),
        )
    if isinstance(result, NiaMonteCarloComparison):
        attribute_labels = {"vocal": "Vo", "dance": "Da", "visual": "Vi"}
        decision_week = result.best.candidate.decision_week
        ranked = []
        for index, value in enumerate(result.choices, start=1):
            choice = attribute_labels.get(
                value.candidate.choice_id,
                value.candidate.choice_id,
            )
            ranked.append(
                f"{index}. {choice} 平均 {float(value.mean_final_score):.1f}／"
                f"範圍 {value.minimum_final_score}–{value.maximum_final_score}"
            )
        best_choice = attribute_labels.get(
            result.best.candidate.choice_id,
            result.best.candidate.choice_id,
        )
        return Plan2OfflineSimulatorView(
            phase=InitialRegularSimulatorPhase.COMPLETED,
            source_text=f"RNG 批次比較：NIA Pro W{decision_week} 自主課程屬性",
            coverage_text=(
                "每個 Vo／Da／Vi 候選使用完全相同的 schedule/exam UInt32 RNG roots，"
                "逐樣本完整重跑到 Final；只抽樣牌序／回合 RNG，"
                "其他 server 事件、獎勵與 NPC scenario 維持固定"
            ),
            state_text="；".join(ranked),
            status_text=(
                f"Monte Carlo 完成：3 個候選 × {result.sample_count} 樣本"
            ),
            recommended_action_text=(
                f"此 RNG 樣本集建議：W{decision_week} 自主課程選 {best_choice}"
            ),
            predicted_path_text=(
                "最佳候選 Final 樣本："
                + "／".join(
                    str(value.final_score) for value in result.best.samples
                )
            ),
            blockers_text=(
                "尚未抽樣 SP／事件／獎勵機率；平均值只代表明示的 UInt32 RNG root 假設"
            ),
            self_play_summary_text=(
                f"共同亂數批次完成；最佳平均 Final "
                f"{float(result.best.mean_final_score):.1f} 分"
            ),
            decision_enabled=request.decision_ready,
            self_play_enabled=True,
            technical_details=(
                "scenario: nia-pro-self-lesson-common-rng-monte-carlo",
                "plan: nia_pro_monte_carlo",
                f"decision_week: {decision_week}",
                f"samples_per_choice: {result.sample_count}",
                f"root_seed: {result.root_seed}",
                f"seed_assumption: {result.seed_assumption}",
                "backend: cpu-native-transition-replay",
                "authentic_nia_local_save: false",
            ),
        )
    if isinstance(result, NiaFullScenarioComparison):
        if request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_WEEKLY_COMPARE:
            choice_labels = {"outing": "外出", "care_package": "差入"}
            ranked = []
            for index, value in enumerate(result.evaluations, start=1):
                choice = choice_labels.get(
                    value.candidate.choice_id,
                    value.candidate.choice_id,
                )
                ranked.append(
                    f"{index}. {choice} Final {value.final_audition_score}／"
                    f"三演出合計 {value.total_audition_score}／"
                    f"終局牌庫 {sum(entry.count for entry in value.result.run.final_state.deck)}"
                )
            best_choice = choice_labels.get(
                result.best.candidate.choice_id,
                result.best.candidate.choice_id,
            )
            comparison_start_week = result.best.result.start_week
            return Plan2OfflineSimulatorView(
                phase=InitialRegularSimulatorPhase.COMPLETED,
                source_text=(
                    "固定反事實比較：NIA Pro "
                    f"W{result.decision_week} 外出／差入"
                ),
                coverage_text=(
                    "差入候選使用情境 JSON 內的 exact Present Start／Receive／End "
                    "與 response card GUID；外出使用 Master 固定恢復量。兩個候選都從"
                    f"同一 W{comparison_start_week} checkpoint 完整重跑到 Final"
                ),
                state_text="；".join(ranked),
                status_text=(
                    f"NIA Pro W{result.decision_week} 外出／差入候選皆已跑到 Final"
                ),
                recommended_action_text=(
                    f"此固定 response scenario 建議：W{result.decision_week} 選 {best_choice}"
                ),
                predicted_path_text=(
                    "推薦候選三演出分數："
                    + "／".join(str(value) for value in result.best.audition_scores)
                ),
                blockers_text=(
                    "Present 獎勵是已觀測／caller 提供的固定 response；"
                    "尚未建立未觀測差入內容的機率分布，不外推為一般最優策略"
                ),
                self_play_summary_text=(
                    f"比較完成：2 個 W{comparison_start_week}→Final 候選；"
                    f"最佳 Final {result.best.final_audition_score} 分"
                ),
                decision_enabled=request.decision_ready,
                self_play_enabled=True,
                technical_details=(
                    "scenario: nia-pro-outing-care-package-full-route-comparison",
                    "plan: nia_pro_weekly_compare",
                    f"objective: {result.objective}",
                    f"scope: continuation-w{comparison_start_week}-to-final-comparison",
                    "present_source: exact-scenario-json",
                    "probability_model: none-fixed-runtime-facts",
                    "authentic_nia_local_save: false",
                ),
            )
        attribute_labels = {"vocal": "Vo", "dance": "Da", "visual": "Vi"}
        ranked = []
        for index, value in enumerate(result.evaluations, start=1):
            attribute = value.candidate.candidate_id.rsplit(":", 1)[-1]
            ranked.append(
                f"{index}. {attribute_labels.get(attribute, attribute)} "
                f"Final {value.final_audition_score}／"
                f"三演出合計 {value.total_audition_score}"
            )
        best_attribute = result.best.candidate.candidate_id.rsplit(":", 1)[-1]
        comparison_start_week = result.best.result.start_week
        return Plan2OfflineSimulatorView(
            phase=InitialRegularSimulatorPhase.COMPLETED,
            source_text=(
                "固定反事實比較：NIA Pro "
                f"W{result.decision_week} 自主課程屬性"
            ),
            coverage_text=(
                "每個候選都以同一份 PC Master／metadata＋Android native scenario "
                f"完整重跑 W{comparison_start_week}→Final；"
                f"只替換 W{result.decision_week} 的 "
                "Vo／Da／Vi 選擇；"
                "伺服器分支與 RNG 固定，因此這是反事實比較，不是假稱已知機率的期望值"
            ),
            state_text="；".join(ranked),
            status_text=(
                f"NIA Pro W{result.decision_week} "
                "三個屬性候選皆已完整跑到 Final"
            ),
            recommended_action_text=(
                f"固定 scenario 建議：W{result.decision_week} 自主課程選 "
                f"{attribute_labels.get(best_attribute, best_attribute)}"
            ),
            predicted_path_text=(
                "推薦候選三演出分數："
                + "／".join(str(value) for value in result.best.audition_scores)
            ),
            blockers_text=(
                "尚未納入 SP 機率、未觀測 server 分支與不同獎勵分布；"
                "不外推為一般最優策略"
            ),
            self_play_summary_text=(
                f"比較完成：{len(result.evaluations)} 個 "
                f"W{comparison_start_week}→Final 候選；"
                f"最佳 Final {result.best.final_audition_score} 分"
            ),
            decision_enabled=request.decision_ready,
            self_play_enabled=True,
            technical_details=(
                "scenario: nia-pro-self-lesson-attribute-full-route-comparison",
                "plan: nia_pro_lesson_compare",
                f"objective: {result.objective}",
                f"scope: continuation-w{comparison_start_week}-to-final-comparison",
                "probability_model: none-fixed-runtime-facts",
                "authentic_nia_local_save: false",
            ),
        )
    if isinstance(result, NiaMasterOuterScaffoldReport):
        final = result.final_state
        issue_lines = tuple(
            f"{issue.code} [{issue.field}] {issue.detail}".strip()
            for issue in result.issues
        )
        trace_lines = tuple(
            f"W{trace.week} {trace.selected_step_type}→{trace.action_id}"
            for trace in result.traces
        )
        blocked = bool(result.issues)
        boundary = (
            f"typed stop：{result.issues[0].code}"
            if blocked
            else "caller schedule prefix 已用盡"
        )
        return Plan2OfflineSimulatorView(
            phase=(
                InitialRegularSimulatorPhase.BLOCKED
                if blocked
                else InitialRegularSimulatorPhase.COMPLETED
            ),
            source_text=(
                "NIA Master caller 回應前綴（partial／非 LocalSave）"
            ),
            coverage_text=(
                "produce-005 PC Master 身分／26 週 calendar＋caller-owned "
                "ProduceSchedule／typed response bundle；GUI W1 初態僅是 "
                "Master-only synthetic outer shadow。這不是 authentic LocalSave，"
                "也不是 26 週完成宣告。"
            ),
            state_text=(
                f"第 {final.week} 週；體力 {final.stamina}/{final.max_stamina}；"
                f"Vo {final.attributes.vocal}／Da {final.attributes.dance}／"
                f"Vi {final.attributes.visual}；"
                f"牌庫 {sum(entry.count for entry in final.deck)} 張"
            ),
            status_text=(
                f"已執行 {len(result.traces)} 個 exact 週轉移；{boundary}；"
                "結果範圍僅為 partial prefix"
            ),
            recommended_action_text=(
                "補齊或修正 typed runtime response 後續跑"
                if blocked
                else "已抵達 caller 提供的 prefix 邊界"
            ),
            predicted_path_text=(
                "trace：" + "；".join(trace_lines)
                if trace_lines
                else "trace：尚無完成週"
            ),
            blockers_text=(
                "沒有 typed blocker；僅停止於 caller prefix 邊界"
                if not issue_lines
                else "\n".join(issue_lines)
            ),
            self_play_summary_text=(
                f"NIA Master partial report：{len(result.traces)} 週；"
                f"stop={result.stop.value}；full_route_complete=false；"
                "authentic_nia_local_save=false"
            ),
            decision_enabled=request.decision_ready,
            self_play_enabled=True,
            technical_details=(
                f"scenario: {result.scenario_label}",
                "plan: nia_master_prefix",
                "scope: schedule-driven-executable-prefix-not-26-week-run",
                f"traces: {len(result.traces)}",
                f"stop: {result.stop.value}",
                f"partial_success: {str(result.partial_success).lower()}",
                f"blocked: {str(result.blocked).lower()}",
                "initial_state_source: pc-master-identities-plus-synthetic-guids",
                "runtime_route_source: caller ProduceSchedule JSON",
                "runtime_response_source: typed produce-005 bundle JSON",
                "authentic_nia_local_save: false",
                "full_route_complete: false",
            ),
        )
    if isinstance(result, NiaMasterInnerAcceptanceResult):
        stage_text = "；".join(
            f"W{value.week} {value.catalog.step_type}：{value.score} 分"
            for value in result.stages
        )
        return Plan2OfflineSimulatorView(
            phase=InitialRegularSimulatorPhase.COMPLETED,
            source_text=(
                "固定演出核心："
                f"{_OFFLINE_FIXED_RUN_PLAN_LABELS[OfflineFixedRunPlan.NIA_MASTER_INNER]}"
            ),
            coverage_text=(
                "規則來源：PC Master／metadata＋Android native；"
                "三場玩家端牌局已完整執行；produce-005 的每週 schedule、"
                "NPC／獎勵與外層 26 週流程仍需 runtime 輸入；"
                "不是 authentic NIA LocalSave"
            ),
            state_text=(
                f"真 Master 初始牌庫 {sum(value.count for value in result.starter_deck)} 張；"
                f"{stage_text}"
            ),
            status_text="NIA Master Mid1／Mid2／Final 玩家端演出驗收完成",
            recommended_action_text="外層週流程等待 ProduceSchedule／LocalSave scenario",
            predicted_path_text="三場為獨立 runtime scenario，未偽裝成連續 26 週培育",
            blockers_text="外層阻塞：runtime weekly schedule、NPC 終值與獎勵結果",
            self_play_summary_text=(
                "NIA Master 演出核心：3/3 終局完成；"
                f"分數 {result.scores}；外層流程尚未執行"
            ),
            decision_enabled=request.decision_ready,
            self_play_enabled=True,
            technical_details=(
                "scenario: nia-master-fktn-three-inner-stages",
                "plan: nia_master_inner",
                "scope: independent-inner-stages-no-outer-schedule",
                "authentic_nia_local_save: false",
                "outer_schedule_executable: false",
            ),
        )
    if isinstance(result, InitialRegularFixedRunReport):
        capability_summary = _fixed_run_capability_summary(
            request.fixed_run_plan,
            result,
            nia_runtime_schedule_path=request.nia_runtime_schedule_path,
            plan1_run_runtime_loadout_path=(
                request.plan1_run_runtime_loadout_path
            ),
        )
        final = result.final_state
        fixed_plan_label = request.fixed_run_plan_label
        scenario_label = _OFFLINE_FIXED_RUN_SCENARIO_LABELS[
            request.fixed_run_plan
        ]
        plan1_bundle = (
            request.fixed_run_plan is OfflineFixedRunPlan.PLAN1
            and bool(request.plan1_run_runtime_loadout_path)
        )
        issue_lines = tuple(
            (
                "Plan1 執行期 loadout 無法套用："
                if plan1_bundle
                and (
                    issue.code.startswith("plan1-run-loadout")
                    or "plan1-bootstrap" in issue.code
                )
                else ""
            )
            + f"{issue.code} [{issue.field}] {issue.detail}".strip()
            for issue in result.issues
        )
        actions = tuple(
            step.action
            for step in result.steps
            if step.action is not None
        )
        preview = " → ".join(actions[:12])
        if len(actions) > 12:
            preview += " → …"
        nia_partial = (
            request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_MID1
            and not result.completed
            and result.stop is InitialRegularFixedRunStop.POLICY
            and not result.issues
            and any(value.stage == "NIA Pro Mid1" for value in result.milestones)
        )
        nia_full = request.fixed_run_plan is OfflineFixedRunPlan.NIA_PRO_FULL
        nia_continuation = nia_full and result.initial_state.week > 1
        accepted = result.completed or nia_partial
        if nia_partial:
            source = f"固定單場：{fixed_plan_label}"
            coverage = (
                "規則來源：PC Master／metadata＋Android native；"
                "牌序、GUID、RNG、倍率、NPC 與獎勵為明確 caller-authored scenario；"
                "不是 NIA LocalSave"
            )
            status = f"NIA Pro Mid1 單場驗收完成；可從第 {final.week} 週繼續"
            recommendation = "已產生單場驗收報告"
        else:
            source = (
                f"固定續跑：{fixed_plan_label}"
                if nia_continuation
                else f"固定整場：{fixed_plan_label}"
            )
            coverage = (
                "規則來源：PC Master／metadata＋Android native；"
                "週行動、牌序、GUID、RNG、倍率、NPC 與獎勵為明確固定 scenario；"
                "不是 authentic NIA LocalSave"
                if nia_full
                else (
                    "規則來源：PC Master／metadata＋Android native；"
                    "事件、獎勵、NPC 與 RNG 為明確固定 server scenario"
                )
            )
            status = (
                (
                    "NIA Pro 27 週完整固定情境已通關"
                    if nia_full and not nia_continuation
                    else (
                        f"NIA Pro W{result.initial_state.week}→Final "
                        "固定 scenario 已通關"
                    )
                    if nia_full
                    else "完整培育流程已通關"
                )
                if result.completed
                else f"完整培育流程停止：{result.stop.value}"
            )
            recommendation = "已產生完整流程報告"
        return Plan2OfflineSimulatorView(
            phase=(
                InitialRegularSimulatorPhase.COMPLETED
                if accepted
                else InitialRegularSimulatorPhase.BLOCKED
            ),
            source_text=source,
            coverage_text=f"{coverage}\n{capability_summary}",
            state_text=(
                f"第 {final.week} 週；體力 {final.stamina}；"
                f"Vo {final.vocal}／Da {final.dance}／Vi {final.visual}；"
                f"牌庫 {final.deck_count} 張"
            ),
            status_text=status,
            recommended_action_text=recommendation,
            predicted_path_text=f"動作路徑：{preview or '無'}",
            blockers_text=(
                "沒有阻塞" if not issue_lines else "\n".join(issue_lines)
            ),
            self_play_summary_text=_plan2_fixed_run_summary(result),
            decision_enabled=request.decision_ready,
            self_play_enabled=True,
            technical_details=(
                f"scenario: {scenario_label}",
                f"plan: {request.fixed_run_plan.value}",
                (
                    "plan1_runtime_authority: caller typed run loadout bundle"
                    if request.fixed_run_plan is OfflineFixedRunPlan.PLAN1
                    and request.plan1_run_runtime_loadout_path
                    else (
                        "plan1_runtime_authority: synthetic fixed smoke"
                        if request.fixed_run_plan is OfflineFixedRunPlan.PLAN1
                        else "plan1_runtime_authority: not-applicable"
                    )
                ),
                f"steps: {len(result.steps)}",
                f"stop: {result.stop.value}",
                (
                    "scope: partial-week9-mid1-to-week10"
                    if nia_partial
                    else (
                        f"scope: continuation-w{result.initial_state.week}-to-final"
                        if nia_continuation
                        else "scope: complete-route"
                    )
                ),
                (
                    "authentic_nia_local_save: false"
                    if nia_partial or nia_full
                    else "scenario_inputs: fixed"
                ),
                (
                    "scenario_source: custom-json"
                    if nia_full and request.fixed_run_scenario_path.strip()
                    else "scenario_source: built-in"
                    if nia_full
                    else "scenario_source: fixed-run-adapter"
                ),
                (
                    "runtime_route_source: ProduceSchedule JSON validated"
                    if nia_full and request.nia_runtime_schedule_path.strip()
                    else "runtime_route_source: omitted; fixed scenario not runtime-validated"
                    if nia_full
                    else "runtime_route_source: not-applicable"
                ),
                (
                    "runtime_response_source: typed bundle JSON loaded"
                    if nia_full and request.nia_runtime_response_path
                    else "runtime_response_source: omitted; legacy fixed inputs"
                    if nia_full
                    else "runtime_response_source: not-applicable"
                ),
            ),
            fixed_run_report=result,
        )
    if isinstance(result, Plan2NativeExamSaveDecision):
        deck_snapshot = _plan2_snapshot_for_result(result)
        coverage = result.coverage
        coverage_text = (
            "卡牌程式覆蓋：未完成"
            if coverage is None
            else _plan2_coverage_text(
                universe=coverage.universe_version_count,
                compiled=coverage.compiled_version_count,
                failed=coverage.failed_version_count,
                current_ready=coverage.current_cards_ready,
                missing_count=len(coverage.current_missing_refs),
            )
        )
        root = None if result.bootstrap is None else result.bootstrap.state
        blockers = _plan2_blocker_lines(result.blockers)
        status = "離線建議完成" if result.decision_ready else "離線建議受阻"
        path = " → ".join(result.principal_path) or "無"
        return Plan2OfflineSimulatorView(
            InitialRegularSimulatorPhase.COMPLETED if result.decision_ready else InitialRegularSimulatorPhase.BLOCKED,
            source_text,
            coverage_text,
            _plan2_state_text(root),
            status,
            _plan2_action_text(result.best_action, terminal=result.terminal),
            f"預測路徑：{path}",
            "沒有阻塞" if not blockers else "\n".join(blockers),
            "固定 seed self-play 尚未執行",
            request.decision_ready,
            True,
            (
                f"predicted value: {result.predicted_value}",
                f"source: {result.source_path}",
            ),
            deck_snapshot,
            _plan2_snapshot_summary(deck_snapshot),
        )
    if isinstance(result, Plan2NativeSelfPlayResult):
        deck_snapshot = _plan2_snapshot_for_result(result)
        coverage = result.coverage
        coverage_text = (
            "卡牌程式覆蓋：未完成"
            if coverage is None
            else _plan2_coverage_text(
                universe=coverage.universe_version_count,
                compiled=coverage.compiled_version_count,
                failed=coverage.failed_version_count,
                current_ready=coverage.current_deck_ready,
                missing_count=len(coverage.current_missing_refs),
            )
        )
        first = result.steps[0] if result.steps else None
        action = None if first is None else first.action
        path = "無" if first is None else " → ".join(first.principal_path)
        blockers = _plan2_blocker_lines(result.blockers)
        final_score = None if result.final_state is None else result.final_state.scalar.score
        summary = (
            f"固定 seed 驗收完成：{len(result.steps)} 步，終局分數 {final_score}。"
            if result.acceptance_passed
            else f"固定 seed 驗收未完成：{result.stop_reason}。"
        )
        return Plan2OfflineSimulatorView(
            InitialRegularSimulatorPhase.COMPLETED if result.acceptance_passed else InitialRegularSimulatorPhase.BLOCKED,
            source_text,
            coverage_text,
            _plan2_state_text(result.initial_state),
            "離線模擬完成" if result.acceptance_passed else "離線模擬受阻",
            _plan2_action_text(action, terminal=result.initial_state is not None and result.initial_state.terminal),
            f"預測路徑：{path}",
            "沒有阻塞" if not blockers else "\n".join(blockers),
            summary,
            request.decision_ready,
            True,
            (
                f"seed: {result.random_state}",
                f"steps: {len(result.steps)}",
                f"stop reason: {result.stop_reason}",
            ),
            deck_snapshot,
            _plan2_snapshot_summary(deck_snapshot),
        )
    missing = (
        "請先選擇 ExamSaveData 檔案。"
        if request.source == Plan2OfflineSimulatorSource.EXAM_SAVE_DATA
        and not request.exam_save_path.strip()
        else ""
    )
    return Plan2OfflineSimulatorView(
        phase,
        source_text,
        "卡牌程式覆蓋：尚未計算",
        "尚未載入離線狀態",
        "可以開始離線計算" if request.decision_ready else "需要選擇資料",
        "尚未產生推薦動作",
        "預測路徑：尚未計算",
        missing,
        "固定 seed self-play 尚未執行",
        request.decision_ready,
        True,
    )


class Plan2OfflineSimulatorController:
    """Headless background controller for the Plan2-only UI block."""

    def __init__(
        self,
        request: Plan2OfflineSimulatorRequest = Plan2OfflineSimulatorRequest(),
        *,
        decision_runner: Plan2DecisionRunner = _run_plan2_offline_decision,
        self_play_runner: Plan2SelfPlayRunner = _run_plan2_fixed_seed_self_play,
        fixed_run_runner: Plan2FixedRunRunner = _run_plan2_fixed_whole_run,
        submit: SimulatorSubmit | None = None,
    ) -> None:
        if not isinstance(request, Plan2OfflineSimulatorRequest):
            raise TypeError("request must be Plan2OfflineSimulatorRequest")
        if (
            not callable(decision_runner)
            or not callable(self_play_runner)
            or not callable(fixed_run_runner)
        ):
            raise TypeError("Plan2 runners must be callable")
        self._request = request
        self._decision_runner = decision_runner
        self._self_play_runner = self_play_runner
        self._fixed_run_runner = fixed_run_runner
        self._submit = _default_submit if submit is None else submit
        if not callable(self._submit):
            raise TypeError("submit must be callable")
        self._queue: queue.Queue[_Plan2WorkerMessage] = queue.Queue()
        self._generation = 0
        self._view = format_plan2_offline_simulator_view(request)

    @property
    def request(self) -> Plan2OfflineSimulatorRequest:
        return self._request

    @property
    def view(self) -> Plan2OfflineSimulatorView:
        return self._view

    def set_request(self, request: Plan2OfflineSimulatorRequest) -> Plan2OfflineSimulatorView:
        if not isinstance(request, Plan2OfflineSimulatorRequest):
            raise TypeError("request must be Plan2OfflineSimulatorRequest")
        self._generation += 1
        self._request = request
        self._view = format_plan2_offline_simulator_view(request)
        return self._view

    def _start(self, operation: Plan2OfflineSimulatorOperation) -> Plan2OfflineSimulatorView:
        if self._view.phase == InitialRegularSimulatorPhase.RUNNING:
            return self._view
        if operation == Plan2OfflineSimulatorOperation.DECISION and not self._request.decision_ready:
            self._view = format_plan2_offline_simulator_view(self._request)
            return self._view
        self._generation += 1
        generation = self._generation
        self._last_completed_run_evidence = None
        request = self._request
        self._view = format_plan2_offline_simulator_view(
            request,
            phase=InitialRegularSimulatorPhase.RUNNING,
            operation=operation,
        )

        def worker() -> None:
            try:
                if operation == Plan2OfflineSimulatorOperation.DECISION:
                    result = self._decision_runner(request)
                elif operation == Plan2OfflineSimulatorOperation.SELF_PLAY:
                    result = self._self_play_runner(request)
                else:
                    result = self._fixed_run_runner(request)
                if operation == Plan2OfflineSimulatorOperation.SELF_PLAY and not isinstance(
                    result, Plan2NativeSelfPlayResult
                ):
                    raise TypeError("self-play runner returned an unsupported result")
                if operation == Plan2OfflineSimulatorOperation.DECISION and not isinstance(
                    result, (Plan2NativeExamSaveDecision, Plan2NativeSelfPlayResult)
                ):
                    raise TypeError("decision runner returned an unsupported result")
                if operation == Plan2OfflineSimulatorOperation.FIXED_RUN and not isinstance(
                    result,
                    (
                        InitialRegularFixedRunReport,
                        NiaMasterInnerAcceptanceResult,
                        NiaFullScenarioComparison,
                        NiaMonteCarloComparison,
                    ),
                ):
                    raise TypeError("fixed-run runner returned an unsupported result")
            except Exception as error:
                self._queue.put(_Plan2WorkerMessage(generation, operation, error=error))
            else:
                self._queue.put(_Plan2WorkerMessage(generation, operation, result=result))

        self._submit(worker)
        return self._view

    def start_decision(self) -> Plan2OfflineSimulatorView:
        return self._start(Plan2OfflineSimulatorOperation.DECISION)

    def start_self_play(self) -> Plan2OfflineSimulatorView:
        return self._start(Plan2OfflineSimulatorOperation.SELF_PLAY)

    def start_fixed_run(self) -> Plan2OfflineSimulatorView:
        return self._start(Plan2OfflineSimulatorOperation.FIXED_RUN)

    def poll(self) -> Plan2OfflineSimulatorView:
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            if message.generation != self._generation:
                continue
            self._view = format_plan2_offline_simulator_view(
                self._request,
                operation=message.operation,
                result=message.result,
                error=message.error,
                phase=(
                    InitialRegularSimulatorPhase.FAILED
                    if message.error is not None
                    else None
                ),
            )
        return self._view


@dataclass(frozen=True, slots=True)
class InitialRegularLiveRequest:
    """User-selected context for the unattended Initial Regular run."""

    idol_card_id: str = "i_card-fktn-3-007"
    plan_type: str = PLAN2
    produce_id: str = "produce-001"
    stage_number: int | None = 1
    game_root: Path = DEFAULT_PC_GAME_ROOT
    expected_run_id: str | None = None
    plan2_max_actions: int = 100
    plan2_policy_bundle_path: Path | None = None
    audition_strategy: str = "highest_available"
    exam_policy_variant: str | None = None

    def __post_init__(self) -> None:
        for name in ("idol_card_id", "plan_type", "produce_id"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"{name} must be text")
        if self.stage_number is not None and (
            type(self.stage_number) is not int or self.stage_number < 1
        ):
            raise ValueError("stage_number must be a positive integer or None")
        object.__setattr__(self, "game_root", Path(self.game_root))
        if self.expected_run_id is not None and not isinstance(
            self.expected_run_id, str
        ):
            raise TypeError("expected_run_id must be text or None")
        if type(self.plan2_max_actions) is not int or self.plan2_max_actions < 1:
            raise ValueError("plan2_max_actions must be a positive integer")
        if self.audition_strategy not in {"stable_clear", "highest_available"}:
            raise ValueError("audition_strategy must be stable_clear or highest_available")
        if self.exam_policy_variant not in (None, "baseline", "integrated"):
            raise ValueError("exam_policy_variant must be baseline or integrated")
        if self.plan2_policy_bundle_path is not None:
            object.__setattr__(
                self,
                "plan2_policy_bundle_path",
                Path(self.plan2_policy_bundle_path),
            )


@dataclass(frozen=True, slots=True)
class InitialRegularLiveReadiness:
    ready: bool
    current_page: str
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.ready) is not bool:
            raise TypeError("ready must be bool")
        if not isinstance(self.current_page, str):
            raise TypeError("current_page must be text")
        if self.ready == bool(self.blockers):
            raise ValueError("ready must be true exactly when blockers are empty")


@dataclass(frozen=True, slots=True)
class InitialRegularLiveView:
    phase: InitialRegularSimulatorPhase
    readiness_text: str
    current_page_text: str
    action_count_text: str
    recent_action_text: str
    stop_reason_text: str
    diff_summary_text: str
    blockers: tuple[str, ...] = ()
    technical_details: tuple[str, ...] = ()
    start_enabled: bool = True
    stop_enabled: bool = False
    monitor_snapshot: Mapping[str, Any] | None = None
    terminal_bookkeeping: Mapping[str, Any] | None = None


def terminal_bookkeeping_pending(value: object) -> bool:
    bookkeeping = getattr(value, "terminal_bookkeeping", None)
    return isinstance(bookkeeping, Mapping) and bookkeeping.get("state") == "pending"


@dataclass(frozen=True, slots=True)
class _LiveWorkerMessage:
    generation: int
    progress: Mapping[str, Any] | None = None
    result: InitialRegularAutopilotResult | None = None
    error: Exception | None = None
    monitor_snapshot: Mapping[str, Any] | None = None
    completed_run_evidence: Mapping[str, Any] | None = None


LiveRunner: TypeAlias = Callable[..., InitialRegularAutopilotResult]
LiveStatusReader: TypeAlias = Callable[[], Mapping[str, Any]]
LiveSurfaceProbe: TypeAlias = Callable[[InitialRegularLiveRequest], InitialRegularSurface]
LiveMonitorWatcherFactory: TypeAlias = Callable[[str], LiveMonitorEventWatcher]
LiveSubmit: TypeAlias = Callable[[Callable[[], None]], None]
LiveControllerStarter: TypeAlias = Callable[[], Mapping[str, Any]]
LiveHomePreparer: TypeAlias = Callable[[], Mapping[str, Any]]
CompletedRunEvidenceFinalizer: TypeAlias = Callable[
    [InitialRegularAutopilotResult, InitialRegularLiveRequest, str],
    Mapping[str, Any],
]
LiveProduceBootstrapper: TypeAlias = Callable[
    [InitialRegularLiveRequest], Mapping[str, Any]
]


_MIN_LIVE_CONTROLLER_VERSION = 45
_NIA_PRODUCE_NOT_STARTED = (
    "尚未開始 N.I.A. 培育；按『開始無人培育』會由 MAA 自動進入。"
)
_HOME_PAGE_UNRECOGNIZED = "目前必須停在可讀取的考試或培育總覽頁面。"


_LIVE_PLAN_LABELS = {
    PLAN1: "Plan 1",
    PLAN2: "Plan 2",
    PLAN3: "Plan 3",
}
_LIVE_MODE_LABELS = {
    "produce-001": "初",
    "produce-004": "NIA Pro",
    "produce-005": "NIA Master",
}
_LIVE_PAGE_LABELS = {
    "global-home": "遊戲首頁",
    "evidence-finalizing": "整理本場資料",
    PAGE_OVERVIEW: "培育總覽",
    PAGE_EXAM: "考試",
    "training": "課程選擇",
    "result": "結果確認",
    "reward": "卡片獎勵",
    "completed": "培育完成",
    "unknown": "尚未辨識",
}


def _default_live_runner(**kwargs: Any) -> InitialRegularAutopilotResult:
    """Dispatch to the selected input backend without cross-backend fallback."""

    if _default_input_uses_dll():
        from .runtime_outer_runner import run_runtime_cultivation

        native_kwargs = dict(kwargs)
        native_kwargs.pop("stage_number", None)
        result = run_runtime_cultivation(**native_kwargs)
        if (isinstance(result, InitialRegularAutopilotResult) and result.completed
                and getattr(result, "native_completed_run_evidence", None) is None):
            # An older native return must never enter the Maa finalizer merely
            # because its transport receipt was absent.
            result = replace(result, native_completed_run_evidence={
                "schema": "gkms.native-completed-run-evidence-unavailable.v1",
                "status": "unavailable", "evidence_complete": False,
                "run_id": native_kwargs.get("expected_run_id"),
                "reason": "native-completed-result-receipt-missing",
            })
        return result

    kwargs.pop("audition_strategy", None)
    if kwargs.pop("exam_policy_variant", None) is not None:
        raise RuntimeError("新版演出模型需要 DLL 狀態與操作接口，不能退回視覺控制。")
    return run_live_initial_regular_autopilot(
        **kwargs,
        exam_policy=(
            EXACT_EXAM_POLICY
            if kwargs.get("plan2_policy_bundle_path") is not None
            else MAA_COMPLETION_EXAM_POLICY
        ),
    )


def _default_live_status_reader() -> Mapping[str, Any]:
    if _default_input_uses_dll():
        from .runtime_command_client import RuntimeCommandClient
        from .controller_client import _serialized_controller_request

        with _serialized_controller_request(5):
            client = RuntimeCommandClient()
            status = client.read_status()
            reply = client.execute("status", timeout=3.0).require_ok()
        if reply.request.session_generation != status["session_generation"]:
            raise RuntimeError("DLL 遊戲程序已變更，請重新檢查連線。")
        return {
            "input_backend": "dll", "bridge_live": True,
            "target_pid": status["pid"], "capabilities": status["capabilities"],
            "session_generation": reply.request.session_generation,
        }
    from .controller_client import send_command

    return dict(send_command("status"))


def _default_input_uses_dll() -> bool:
    from .runtime_command_client import input_backend

    return input_backend() == "dll"


_DLL_LIVE_CAPABILITIES = frozenset({
    "read_outer_snapshot", "outer.action", "read_snapshot",
    "exam.play", "exam.drink", "exam.end_turn",
})


def _live_controller_status_is_usable(status: Mapping[str, Any]) -> bool:
    """Check actual backend capabilities, keeping Maa's existing contract."""

    if not isinstance(status, Mapping):
        return False
    if status.get("input_backend") == "dll":
        capabilities = status.get("capabilities")
        return (
            status.get("bridge_live") is True
            and type(status.get("target_pid")) is int and status["target_pid"] > 0
            and isinstance(capabilities, (list, tuple, set, frozenset))
            and all(isinstance(value, str) for value in capabilities)
            and _DLL_LIVE_CAPABILITIES <= set(capabilities)
        )
    raw_version = status.get("controller_version")
    if type(raw_version) is not int or raw_version < _MIN_LIVE_CONTROLLER_VERSION:
        return False
    return (
        bool(status.get("background_control"))
        and int(status.get("target_hwnd", 0)) > 0
        and int(status.get("target_pid", 0)) > 0
    )


def _default_live_controller_starter() -> Mapping[str, Any]:
    """Start or upgrade the supervisor-owned Maa worker, then wait for bind.

    An existing elevated supervisor restarts its own outdated worker without
    another UAC prompt.  If no supervisor exists, the same fixed launcher asks
    for one UAC approval and preserves worker ownership for later restarts.
    This never sends a game action.
    """

    if _default_input_uses_dll():
        status = _default_live_status_reader()
        if not _live_controller_status_is_usable(status):
            raise RuntimeError("DLL 尚未連線或缺少必要能力；請先開啟遊戲並載入 DLL。")
        return status

    project_root = Path(__file__).resolve().parents[2]
    script = project_root / "scripts" / "launch_elevated_supervisor.ps1"
    if not script.is_file():
        raise FileNotFoundError(f"找不到 MAA 高權限啟動腳本：{script}")
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
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
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("等待 MAA 高權限監督器就緒逾時") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            "UAC 被取消或 MAA 高權限監督器啟動失敗"
            + ("" if not detail else f"：{detail}")
        )

    deadline = time.monotonic() + 60.0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = _default_live_status_reader()
        except Exception as error:
            last_error = error
        else:
            if _live_controller_status_is_usable(status):
                return dict(status)
        time.sleep(0.1)
    suffix = "" if last_error is None else f"（最後錯誤：{last_error}）"
    raise RuntimeError(f"MAA 高權限監督器已啟動，但新版 worker 未就緒{suffix}")


def _default_live_home_preparer() -> Mapping[str, Any]:
    """Make global Home the GUI's only cultivation start boundary."""

    if _default_input_uses_dll():
        from .runtime_outer_runner import RuntimeOuterGateway
        from .controller_client import _serialized_controller_request

        with _serialized_controller_request(5):
            snapshot = RuntimeOuterGateway().read()
        return {"current_page": snapshot.raw["screen_type"], "snapshot": dict(snapshot.raw),
                "input_backend": "dll", "read_only": True}
    from .controller_client import send_command

    return dict(
        send_command(
            "wait_global_home",
            timeout=90.0,
            timeout_seconds=75.0,
        )
    )


def _default_completed_run_evidence_finalizer(
    result: InitialRegularAutopilotResult,
    request: InitialRegularLiveRequest,
    run_id: str,
) -> Mapping[str, Any]:
    """Finalize one already-completed GUI run without changing gameplay."""

    if getattr(result, "native_completed_run_evidence", None) is not None:
        from .native_completed_run_receipt import reuse_native_completed_run_evidence

        return reuse_native_completed_run_evidence(result, request, run_id)

    from .completed_run_evidence import finalize_completed_run_evidence
    from .native_stage_ingest_receipt import ingest_completed_run_native_stages
    from .nia_live_unattended import NiaLiveUnattendedRequest
    from .produce_outer_local_save import read_current_produce_outer_local_save
    from .run_identity import load_run

    run = load_run(run_id)
    try:
        final_snapshot = read_current_produce_outer_local_save(request.game_root)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        final_snapshot = None
    nia_request = NiaLiveUnattendedRequest(
        produce_id=request.produce_id,
        idol_card_id=request.idol_card_id,
        game_root=request.game_root,
        expected_run_id=run_id,
        stage_number=request.stage_number or 1,
    )
    evidence = dict(
        finalize_completed_run_evidence(
            result,
            run=run,
            request=nia_request,
            final_snapshot=final_snapshot,
        )
    )
    report_path = evidence.get("report_path")
    if isinstance(report_path, str) and report_path:
        try:
            evidence["native_stage_ingest"] = dict(
                ingest_completed_run_native_stages(report_path)
            )
        except Exception as error:
            evidence["native_stage_ingest"] = {
                "dataset_ready": False,
                "error": f"{type(error).__name__}: {error}",
                "frozen_dataset_modified": False,
                "active_policy_modified": False,
                "training_triggered": False,
            }
    return evidence


def _default_nia_produce_bootstrapper(
    request: InitialRegularLiveRequest,
) -> Mapping[str, Any]:
    """Enter one allow-listed N.I.A. run through the elevated Maa helper."""

    if _default_input_uses_dll():
        raise RuntimeError("DLL 培育入口由 native 培育流程統一處理。")
    if request.produce_id not in {"produce-004", "produce-005"}:
        raise ValueError("N.I.A. bootstrap only supports produce-004/produce-005")
    from .controller_client import send_command

    result = dict(
        send_command(
            "start_nia_produce",
            timeout=150.0,
            produce_id=request.produce_id,
            idol_card_id=request.idol_card_id,
        )
    )
    if bool(result.get("started")):
        return result
    reason = str(result.get("reason", "unknown"))
    if reason == "ap-insufficient-cancelled":
        raise RuntimeError("N.I.A. AP 不足；MAA 已取消，未使用回體力道具")
    if reason == "existing-produce-detected":
        raise RuntimeError("MAA 偵測到既有培育，但找不到可接續的 LocalSave")
    raise RuntimeError(f"MAA 未能進入 N.I.A. 培育：{reason}")


def _default_produce_bootstrapper(
    request: InitialRegularLiveRequest,
) -> Mapping[str, Any]:
    """Enter any supported Initial/N.I.A. mode through Maa's OCR picker."""

    if _default_input_uses_dll():
        raise RuntimeError("DLL 培育入口由 native 培育流程統一處理。")
    from .controller_client import send_command

    result = dict(
        send_command(
            "start_produce",
            timeout=150.0,
            produce_id=request.produce_id,
            idol_card_id=request.idol_card_id,
        )
    )
    if bool(result.get("started")):
        return result
    reason = str(result.get("reason", "unknown"))
    if reason == "ap-insufficient-cancelled":
        raise RuntimeError("Produce AP 不足；MAA 已取消，未使用回體力道具")
    if reason == "existing-produce-detected":
        raise RuntimeError("MAA 偵測到既有培育，未覆蓋目前進度")
    raise RuntimeError(f"MAA 未能進入指定培育：{reason}")


def _default_live_surface_probe(
    request: InitialRegularLiveRequest,
) -> InitialRegularSurface:
    if _default_input_uses_dll():
        from .runtime_outer_runner import RuntimeOuterGateway

        snapshot = RuntimeOuterGateway().read()
        page = (
            PAGE_EXAM if snapshot.raw["screen_type"] == "ExamScreenPresenter"
            else PAGE_OVERVIEW if snapshot.raw["surface"] == "schedule"
            else PAGE_NIA_OUTER
        )
        return InitialRegularSurface(page, {
            "source": "dll", "native_snapshot": dict(snapshot.raw),
            "session_generation": snapshot.session_generation,
        })
    reader = InitialRegularLiveSurfaceReader(
        idol_card_id=request.idol_card_id,
        produce_id=request.produce_id,
        plan_type=request.plan_type,
        game_root=request.game_root,
        expected_run_id=request.expected_run_id,
    )
    return reader(None)


def check_initial_regular_live_readiness(
    request: InitialRegularLiveRequest,
    *,
    status_reader: LiveStatusReader = _default_live_status_reader,
    surface_probe: LiveSurfaceProbe = _default_live_surface_probe,
) -> InitialRegularLiveReadiness:
    """Check native protocol/session; only the runner owns action readiness."""

    if not isinstance(request, InitialRegularLiveRequest):
        raise TypeError("request must be InitialRegularLiveRequest")
    blockers: list[str] = []
    if not request.idol_card_id.strip():
        blockers.append("請先選擇偶像卡。")
    if request.plan_type not in _LIVE_PLAN_LABELS:
        blockers.append("請先選擇支援的培育計畫。")
    if not request.produce_id.strip():
        blockers.append("請先選擇完整的舞台情境。")
    if blockers:
        return InitialRegularLiveReadiness(False, "unknown", tuple(blockers))

    try:
        status = status_reader()
        if not isinstance(status, Mapping):
            raise TypeError("controller status is not a mapping")
    except Exception as error:
        backend = "DLL" if status_reader is _default_live_status_reader and _default_input_uses_dll() else "MAA"
        return InitialRegularLiveReadiness(
            False,
            "unknown",
            (f"無法讀取 {backend} 控制狀態：{type(error).__name__}: {error}",),
        )
    if status.get("input_backend") == "dll":
        supported_mode, support_text = native_cultivation_mode_support(request.produce_id)
        if not supported_mode:
            return InitialRegularLiveReadiness(False, "unknown", (support_text,))
        if not _live_controller_status_is_usable(status):
            return InitialRegularLiveReadiness(False, "unknown", ("DLL 尚未完成活性與必要能力檢查。",))
        try:
            surface = surface_probe(request)
            if not isinstance(surface, InitialRegularSurface) or surface.payload.get("source") != "dll":
                raise TypeError("DLL readiness requires a native surface")
            native = surface.payload.get("native_snapshot")
            if not isinstance(native, Mapping):
                raise TypeError("DLL snapshot is missing")
            if surface.payload.get("session_generation") != status.get("session_generation"):
                raise ValueError("DLL process changed during readiness check")
            if (native.get("schema") != "gkms.outer-runtime-snapshot.v1"
                    or not isinstance(native.get("screen_type"), str) or not native["screen_type"]
                    or not isinstance(native.get("surface"), str) or not native["surface"]
                    or type(native.get("busy")) is not bool or type(native.get("actions_complete")) is not bool
                    or not isinstance(native.get("legal_actions"), list)):
                raise ValueError("DLL native snapshot protocol is incomplete")
            screen = native["screen_type"]
            explicit = native.get("blockers", [])
            if not isinstance(explicit, list):
                raise ValueError("DLL native blockers protocol is invalid")
            if explicit:
                return InitialRegularLiveReadiness(False, screen, tuple(str(value) for value in explicit))
            # Busy/incomplete/new surfaces are valid observations. The sole
            # native runner waits or abstains at its own exact action boundary;
            # a GUI label table must never become a second execution gate.
            return InitialRegularLiveReadiness(True, screen)
        except Exception as error:
            return InitialRegularLiveReadiness(False, "unknown", (
                f"無法讀取 DLL 遊戲場景：{type(error).__name__}: {error}",
            ))
    if not bool(status.get("background_control")):
        blockers.append("MAA 背景控制尚未就緒。")
    if request.stage_number is None:
        blockers.append("請先選擇完整的舞台情境。")
    raw_version = status.get("controller_version")
    if type(raw_version) is not int:
        blockers.append("MAA 高權限控制器未提供有效的整數版本。")
    elif raw_version < _MIN_LIVE_CONTROLLER_VERSION:
        blockers.append(
            f"MAA 高權限控制器版本過舊（{raw_version} < {_MIN_LIVE_CONTROLLER_VERSION}）。"
        )
    if int(status.get("target_hwnd", 0)) <= 0 or int(
        status.get("target_pid", 0)
    ) <= 0:
        blockers.append("MAA 尚未綁定遊戲視窗。")
    if blockers:
        return InitialRegularLiveReadiness(False, "unknown", tuple(blockers))

    try:
        surface = surface_probe(request)
        if not isinstance(surface, InitialRegularSurface):
            raise TypeError("surface probe returned an invalid value")
    except FileNotFoundError as error:
        if request.produce_id in {"produce-004", "produce-005"}:
            return InitialRegularLiveReadiness(
                False,
                "unknown",
                (_NIA_PRODUCE_NOT_STARTED,),
            )
        return InitialRegularLiveReadiness(
            False,
            "unknown",
            (f"無法讀取 ExamSaveData 或培育總覽：FileNotFoundError: {error}",),
        )
    except Exception as error:
        return InitialRegularLiveReadiness(
            False,
            "unknown",
            (f"無法讀取 ExamSaveData 或培育總覽：{type(error).__name__}: {error}",),
        )
    if surface.page not in {PAGE_EXAM, PAGE_OVERVIEW} and not (
        request.produce_id in {"produce-004", "produce-005"}
        and surface.page != PAGE_UNKNOWN
    ):
        blockers.append(_HOME_PAGE_UNRECOGNIZED)
    return InitialRegularLiveReadiness(
        not blockers,
        surface.page,
        tuple(blockers),
    )


def _readiness_requests_nia_bootstrap(
    request: InitialRegularLiveRequest,
    readiness: InitialRegularLiveReadiness,
) -> bool:
    return (
        request.produce_id in {"produce-004", "produce-005"}
        and readiness.blockers == (_NIA_PRODUCE_NOT_STARTED,)
    )


def _readiness_is_nia_home_attention_wait(
    request: InitialRegularLiveRequest,
    readiness: InitialRegularLiveReadiness,
) -> bool:
    """Keep a Home-prepared N.I.A. start idle behind an unknown overlay."""

    return (
        request.produce_id in {"produce-004", "produce-005"}
        and readiness.current_page == PAGE_UNKNOWN
        and readiness.blockers == (_HOME_PAGE_UNRECOGNIZED,)
    )


def _live_result_action_count(result: InitialRegularAutopilotResult) -> int:
    native_count = sum(
        int(step.outcome.get("actions_executed", 0))
        for step in result.steps
        if step.page == PAGE_EXAM
    )
    return native_count if result.plan_type == PLAN2 else len(result.steps)


def _live_recent_action(result: InitialRegularAutopilotResult) -> str:
    if not result.steps:
        return "尚無動作"
    step = result.steps[-1]
    if step.page == PAGE_EXAM:
        inner_steps = step.outcome.get("steps")
        if isinstance(inner_steps, list) and inner_steps:
            raw = inner_steps[-1]
            if isinstance(raw, Mapping):
                action = raw.get("action")
                if isinstance(action, Mapping):
                    action_id = action.get("action_id")
                    if isinstance(action_id, str) and action_id:
                        return action_id
    label = cultivation_action_label(step.action, step.target)
    target = "" if step.target in (None, "") or label != step.action else f"：{step.target}"
    return f"{label}{target}"


def _live_diff_summary(result: InitialRegularAutopilotResult) -> str:
    paths: list[str] = []
    for step in result.steps:
        groups = step.outcome.get("differences")
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            values = group.get("differences")
            if not isinstance(values, list):
                continue
            for value in values:
                if isinstance(value, Mapping) and isinstance(value.get("path"), str):
                    paths.append(str(value["path"]))
    if not paths:
        return "無預測差異"
    preview = "、".join(paths[:3])
    suffix = "" if len(paths) <= 3 else f"，另有 {len(paths) - 3} 項"
    return f"{len(paths)} 項：{preview}{suffix}"


def format_initial_regular_live_view(
    request: InitialRegularLiveRequest,
    *,
    phase: InitialRegularSimulatorPhase = InitialRegularSimulatorPhase.IDLE,
    readiness: InitialRegularLiveReadiness | None = None,
    progress: Mapping[str, Any] | None = None,
    result: InitialRegularAutopilotResult | None = None,
    error: Exception | None = None,
) -> InitialRegularLiveView:
    if not isinstance(request, InitialRegularLiveRequest):
        raise TypeError("request must be InitialRegularLiveRequest")
    blockers = () if readiness is None else readiness.blockers
    page = "unknown" if readiness is None else readiness.current_page
    action_count = 0
    recent = "尚無動作"
    stop_reason = "尚未停止"
    diff_summary = "無預測差異"
    technical: tuple[str, ...] = ()
    monitor_snapshot: Mapping[str, Any] | None = None
    terminal_bookkeeping: Mapping[str, Any] | None = None
    if progress is not None:
        page = str(progress.get("current_page", page))
        action_count = int(progress.get("outer_action_count", 0))
        recent_step = progress.get("recent_step")
        if isinstance(recent_step, Mapping):
            target = recent_step.get("target")
            action = str(recent_step.get("action", "動作"))
            label = cultivation_action_label(action, target)
            recent = label + ("" if target in (None, "") or label != action else f"：{target}")
        if progress.get("stop_reason"):
            stop_reason = str(progress["stop_reason"])
        raw_monitor = progress.get("monitor_snapshot")
        if isinstance(raw_monitor, Mapping):
            monitor_snapshot = dict(raw_monitor)
    if result is not None:
        if isinstance(getattr(result, "terminal_bookkeeping", None), Mapping):
            terminal_bookkeeping = deepcopy(dict(result.terminal_bookkeeping))
        action_count = _live_result_action_count(result)
        recent = _live_recent_action(result)
        stop_reason = result.stop_reason
        diff_summary = _live_diff_summary(result)
        if result.steps:
            page = result.steps[-1].page
        elif result.completed:
            page = "completed"
        if result.status == STATUS_FAILED_SETTLED:
            page = "首頁"
            stop_reason = "本次培育未通過 Final，已完成結算並返回首頁"
        phase = (
            InitialRegularSimulatorPhase.COMPLETED
            if result.status == LIVE_STATUS_COMPLETED
            else InitialRegularSimulatorPhase.FAILED
            if result.status == STATUS_FAILED_SETTLED
            else InitialRegularSimulatorPhase.CANCELLED
            if result.status == LIVE_STATUS_STOPPED
            else InitialRegularSimulatorPhase.BLOCKED
        )
        if phase == InitialRegularSimulatorPhase.COMPLETED and terminal_bookkeeping_pending(result):
            reason = str(terminal_bookkeeping.get("stop_reason") or "terminal-bookkeeping-pending")
            stop_reason = f"本場培育已完成；本機狀態尚待清理，已停止連跑：{reason}"
            technical = (reason,)
    if error is not None:
        phase = InitialRegularSimulatorPhase.FAILED
        stop_reason = f"背景執行失敗：{error}"
        technical = (f"{type(error).__name__}: {error}",)
    elif phase == InitialRegularSimulatorPhase.BLOCKED and blockers and stop_reason == "尚未停止":
        stop_reason = "；".join(blockers)
    readiness_text = (
        "必要條件已就緒"
        if readiness is not None and readiness.ready
        else "尚未檢查必要條件"
        if readiness is None
        else "尚未就緒"
    )
    if phase == InitialRegularSimulatorPhase.RUNNING:
        readiness_text = "無人培育執行中"
    elif phase == InitialRegularSimulatorPhase.CANCELLING:
        readiness_text = "正在安全停止，等待目前步驟結束"
    bookkeeping_pending = terminal_bookkeeping is not None and terminal_bookkeeping.get("state") == "pending"
    if bookkeeping_pending and phase == InitialRegularSimulatorPhase.COMPLETED:
        readiness_text = "本場培育已完成；本機狀態尚待清理"
    return InitialRegularLiveView(
        phase=phase,
        readiness_text=readiness_text,
        current_page_text=_LIVE_PAGE_LABELS.get(page, cultivation_page_label(
            page, None if monitor_snapshot is None else monitor_snapshot.get("native_screen_type"),
        )),
        action_count_text=f"動作數：{action_count}",
        recent_action_text=f"最近動作：{recent}",
        stop_reason_text=f"停止原因：{stop_reason}",
        diff_summary_text=f"差異摘要：{diff_summary}",
        blockers=blockers,
        technical_details=technical,
        start_enabled=not bookkeeping_pending and phase
        not in {
            InitialRegularSimulatorPhase.RUNNING,
            InitialRegularSimulatorPhase.CANCELLING,
        },
        stop_enabled=phase == InitialRegularSimulatorPhase.RUNNING,
        monitor_snapshot=monitor_snapshot,
        terminal_bookkeeping=terminal_bookkeeping,
    )


def _default_live_submit(job: Callable[[], None]) -> None:
    threading.Thread(
        target=job,
        daemon=True,
        name="gkms-initial-regular-unattended",
    ).start()


class InitialRegularLiveController:
    """Headless cooperative worker for the non-technical live controls."""

    def __init__(
        self,
        request: InitialRegularLiveRequest = InitialRegularLiveRequest(),
        *,
        runner: LiveRunner = _default_live_runner,
        status_reader: LiveStatusReader = _default_live_status_reader,
        surface_probe: LiveSurfaceProbe = _default_live_surface_probe,
        monitor_watcher_factory: LiveMonitorWatcherFactory | None = None,
        submit: LiveSubmit | None = None,
        controller_starter: LiveControllerStarter | None = None,
        home_preparer: LiveHomePreparer | None = None,
        completed_run_finalizer: CompletedRunEvidenceFinalizer | None = None,
        produce_bootstrapper: LiveProduceBootstrapper | None = None,
    ) -> None:
        if not isinstance(request, InitialRegularLiveRequest):
            raise TypeError("request must be InitialRegularLiveRequest")
        for value in (runner, status_reader, surface_probe):
            if not callable(value):
                raise TypeError("live dependencies must be callable")
        self._request = request
        self._runner = runner
        self._status_reader = status_reader
        self._surface_probe = surface_probe
        self._monitor_watcher_factory = (
            LiveMonitorEventWatcher
            if monitor_watcher_factory is None
            and status_reader is _default_live_status_reader
            else monitor_watcher_factory
        )
        if self._monitor_watcher_factory is not None and not callable(
            self._monitor_watcher_factory
        ):
            raise TypeError("monitor_watcher_factory must be callable or None")
        # Dependency-injected readers are test/offline seams and must not open
        # UAC unexpectedly. The real GUI receives the default one-shot starter.
        self._controller_starter = (
            _default_live_controller_starter
            if controller_starter is None
            and status_reader is _default_live_status_reader
            else controller_starter
        )
        if self._controller_starter is not None and not callable(
            self._controller_starter
        ):
            raise TypeError("controller_starter must be callable or None")
        self._home_preparer = (
            _default_live_home_preparer
            if home_preparer is None
            and status_reader is _default_live_status_reader
            else home_preparer
        )
        if self._home_preparer is not None and not callable(self._home_preparer):
            raise TypeError("home_preparer must be callable or None")
        self._completed_run_finalizer = (
            _default_completed_run_evidence_finalizer
            if completed_run_finalizer is None
            and status_reader is _default_live_status_reader
            else completed_run_finalizer
        )
        if self._completed_run_finalizer is not None and not callable(
            self._completed_run_finalizer
        ):
            raise TypeError("completed_run_finalizer must be callable or None")
        self._produce_bootstrapper = (
            _default_nia_produce_bootstrapper
            if produce_bootstrapper is None
            and status_reader is _default_live_status_reader
            else produce_bootstrapper
        )
        if self._produce_bootstrapper is not None and not callable(
            self._produce_bootstrapper
        ):
            raise TypeError("produce_bootstrapper must be callable or None")
        self._submit = _default_live_submit if submit is None else submit
        if not callable(self._submit):
            raise TypeError("submit must be callable")
        self._queue: queue.Queue[_LiveWorkerMessage] = queue.Queue()
        self._generation = 0
        self._monitor_stop_event: threading.Event | None = None
        self._stop_event: threading.Event | None = None
        self._readiness: InitialRegularLiveReadiness | None = None
        self._progress: Mapping[str, Any] | None = None
        self._last_completed_run_evidence: Mapping[str, Any] | None = None
        self._view = format_initial_regular_live_view(request)

    @property
    def request(self) -> InitialRegularLiveRequest:
        return self._request

    @property
    def view(self) -> InitialRegularLiveView:
        return self._view

    @property
    def last_completed_run_evidence(self) -> Mapping[str, Any] | None:
        return self._last_completed_run_evidence

    def set_request(self, request: InitialRegularLiveRequest) -> InitialRegularLiveView:
        if not isinstance(request, InitialRegularLiveRequest):
            raise TypeError("request must be InitialRegularLiveRequest")
        if terminal_bookkeeping_pending(self._view):
            return self._view
        if self._view.phase in {
            InitialRegularSimulatorPhase.RUNNING,
            InitialRegularSimulatorPhase.CANCELLING,
        }:
            return self._view
        self._generation += 1
        if self._monitor_stop_event is not None:
            self._monitor_stop_event.set()
            self._monitor_stop_event = None
        self._request = request
        self._readiness = None
        self._progress = None
        self._last_completed_run_evidence = None
        self._view = format_initial_regular_live_view(request)
        return self._view

    def check_readiness(self) -> InitialRegularLiveReadiness:
        if terminal_bookkeeping_pending(self._view):
            return InitialRegularLiveReadiness(False, "completed", (self._view.stop_reason_text,))
        readiness = check_initial_regular_live_readiness(
            self._request,
            status_reader=self._status_reader,
            surface_probe=self._surface_probe,
        )
        self._readiness = readiness
        self._view = format_initial_regular_live_view(
            self._request,
            phase=(
                InitialRegularSimulatorPhase.IDLE
                if readiness.ready
                else InitialRegularSimulatorPhase.BLOCKED
            ),
            readiness=readiness,
        )
        return readiness

    def start(self) -> InitialRegularLiveView:
        if terminal_bookkeeping_pending(self._view):
            return self._view
        if self._view.phase in {
            InitialRegularSimulatorPhase.RUNNING,
            InitialRegularSimulatorPhase.CANCELLING,
        }:
            return self._view
        native_start = self._status_reader is _default_live_status_reader and _default_input_uses_dll()
        if native_start:
            supported_mode, support_text = native_cultivation_mode_support(self._request.produce_id)
            if not supported_mode:
                self._readiness = InitialRegularLiveReadiness(False, "unknown", (support_text,))
                self._view = format_initial_regular_live_view(self._request,
                    phase=InitialRegularSimulatorPhase.BLOCKED, readiness=self._readiness)
                return self._view
        needs_controller_start = False
        if not native_start and self._controller_starter is not None:
            try:
                current_status = self._status_reader()
            except Exception:
                current_status = {}
            if not _live_controller_status_is_usable(current_status):
                needs_controller_start = True
        needs_nia_bootstrap = False
        force_home_start = (
            not native_start
            and
            self._home_preparer is not None
            and self._request.produce_id in {"produce-004", "produce-005"}
        )
        if native_start:
            # Queue actual DLL liveness/surface reads in the worker.  This
            # pending state is not a claim that Home or any input is ready.
            readiness = InitialRegularLiveReadiness(False, "unknown", ("正在檢查 DLL 連線與遊戲場景。",))
            self._readiness = readiness
        elif needs_controller_start or force_home_start:
            # UAC approval is user-paced. Keep Tk responsive while the fixed
            # helper/Home route starts; no Tk callback blocks on game input.
            readiness = InitialRegularLiveReadiness(True, "unknown")
            self._readiness = readiness
        else:
            readiness = self.check_readiness()
            if not readiness.ready:
                if not _readiness_requests_nia_bootstrap(self._request, readiness):
                    return self._view
                needs_nia_bootstrap = True
                readiness = InitialRegularLiveReadiness(True, "unknown")
                self._readiness = readiness
        self._generation += 1
        generation = self._generation
        request = self._request
        stop_event = threading.Event()
        self._stop_event = stop_event
        monitor_stop_event = threading.Event()
        self._monitor_stop_event = monitor_stop_event
        self._progress = {
            "current_page": readiness.current_page,
            "outer_action_count": 0,
        }
        self._view = format_initial_regular_live_view(
            request,
            phase=InitialRegularSimulatorPhase.RUNNING,
            readiness=readiness,
            progress=self._progress,
        )

        monitor_start_lock = threading.Lock()
        monitor_started_run_id: list[str] = []
        resolved_run_id: list[str | None] = [request.expected_run_id]

        def start_monitor_watcher(source_run_id: object) -> None:
            if native_start or self._monitor_watcher_factory is None:
                # Native runner callbacks already carry authoritative state.
                # Do not subscribe to the legacy Maa event stream alongside it.
                return
            if not isinstance(source_run_id, str) or not source_run_id.strip():
                return
            bound_run_id = source_run_id.strip()
            with monitor_start_lock:
                if monitor_started_run_id:
                    if monitor_started_run_id[0] != bound_run_id:
                        monitor_stop_event.set()
                    return
                monitor_started_run_id.append(bound_run_id)
            watcher_factory = self._monitor_watcher_factory

            def monitor_worker() -> None:
                watcher = None
                try:
                    watcher = watcher_factory(bound_run_id)
                    watcher.run(
                        stop_requested=lambda: (
                            monitor_stop_event.is_set()
                            or generation != self._generation
                        ),
                        publish_snapshot=lambda snapshot: self._queue.put(
                            _LiveWorkerMessage(
                                generation,
                                monitor_snapshot=dict(snapshot),
                            )
                        ),
                    )
                except Exception:
                    # Monitoring is observational and cannot stop cultivation.
                    pass
                finally:
                    if watcher is not None:
                        try:
                            watcher.close()
                        except Exception:
                            pass

            threading.Thread(
                target=monitor_worker,
                daemon=True,
                name="gkms-initial-regular-live-event-monitor",
            ).start()

        # Existing/resumed runs are already bound at Start.  Fresh runs are
        # intentionally None here and start this watcher from the first
        # run-bound progress event below, before Exam dispatch.
        start_monitor_watcher(request.expected_run_id)

        def progress_callback(progress: Mapping[str, Any]) -> None:
            source_run_id = progress.get("source_run_id")
            if isinstance(source_run_id, str) and source_run_id.strip():
                resolved_run_id[0] = source_run_id.strip()
            self._queue.put(_LiveWorkerMessage(generation, progress=dict(progress)))
            start_monitor_watcher(source_run_id)

        def worker() -> None:
            def finish_cancelled_before_runner() -> None:
                self._queue.put(
                    _LiveWorkerMessage(
                        generation,
                        result=InitialRegularAutopilotResult(
                            status=LIVE_STATUS_STOPPED,
                            stop_reason="stop-requested",
                            plan_type=request.plan_type,
                            cycles=0,
                            steps=(),
                        ),
                    )
                )

            try:
                if native_start:
                    native_readiness = check_initial_regular_live_readiness(
                        request, status_reader=self._status_reader, surface_probe=self._surface_probe,
                    )
                    if not native_readiness.ready:
                        raise RuntimeError("DLL 尚未就緒：" + "; ".join(native_readiness.blockers))
                    if stop_event.is_set():
                        finish_cancelled_before_runner()
                        return
                    progress_callback({"current_page": native_readiness.current_page, "outer_action_count": 0})
                bootstrap_required = needs_nia_bootstrap
                if needs_controller_start:
                    assert self._controller_starter is not None
                    try:
                        self._controller_starter()
                    except Exception as error:
                        raise RuntimeError(
                            f"無法啟動新版 MAA 高權限控制器：{error}"
                        ) from error
                if force_home_start:
                    assert self._home_preparer is not None
                    self._home_preparer()
                    if stop_event.is_set():
                        finish_cancelled_before_runner()
                        return
                    progress_callback(
                        {
                            "current_page": "global-home",
                            "outer_action_count": 0,
                        }
                    )
                    home_readiness = check_initial_regular_live_readiness(
                        request,
                        status_reader=self._status_reader,
                        surface_probe=self._surface_probe,
                    )
                    if _readiness_is_nia_home_attention_wait(
                        request,
                        home_readiness,
                    ):
                        progress_callback(
                            {
                                "current_page": PAGE_UNKNOWN,
                                "outer_action_count": 0,
                                "source_run_id": request.expected_run_id,
                            }
                        )
                    while _readiness_is_nia_home_attention_wait(
                        request,
                        home_readiness,
                    ):
                        # ``wait_global_home`` already established the start
                        # boundary.  An unknown frame here is external user
                        # attention for either a fresh or resumed run, not
                        # permission to click or a cultivation/model failure.
                        if stop_event.wait(0.5):
                            finish_cancelled_before_runner()
                            return
                        home_readiness = check_initial_regular_live_readiness(
                            request,
                            status_reader=self._status_reader,
                            surface_probe=self._surface_probe,
                        )
                    if _readiness_requests_nia_bootstrap(request, home_readiness):
                        bootstrap_required = True
                    elif not home_readiness.ready:
                        raise RuntimeError(
                            "回到遊戲首頁後仍無法判定培育入口："
                            + "; ".join(home_readiness.blockers)
                        )
                    else:
                        bootstrap_required = False
                        progress_callback(
                            {
                                "current_page": home_readiness.current_page,
                                "outer_action_count": 0,
                            }
                        )
                elif needs_controller_start:
                    started_readiness = check_initial_regular_live_readiness(
                        request,
                        status_reader=self._status_reader,
                        surface_probe=self._surface_probe,
                    )
                    if _readiness_requests_nia_bootstrap(request, started_readiness):
                        bootstrap_required = True
                    elif not started_readiness.ready:
                        raise RuntimeError(
                            "MAA 控制器啟動後仍未就緒："
                            + "; ".join(started_readiness.blockers)
                        )
                    else:
                        progress_callback(
                            {
                                "current_page": started_readiness.current_page,
                                "outer_action_count": 0,
                            }
                        )
                if bootstrap_required:
                    if self._produce_bootstrapper is None:
                        raise RuntimeError("N.I.A. MAA 培育入口尚未設定")
                    self._produce_bootstrapper(request)
                    deadline = time.monotonic() + 30.0
                    post_bootstrap_readiness: InitialRegularLiveReadiness | None = None
                    while time.monotonic() < deadline:
                        if stop_event.is_set():
                            raise RuntimeError(
                                "N.I.A. 啟動後在 LocalSave 出現前收到停止要求"
                            )
                        post_bootstrap_readiness = check_initial_regular_live_readiness(
                            request,
                            status_reader=self._status_reader,
                            surface_probe=self._surface_probe,
                        )
                        if post_bootstrap_readiness.ready:
                            break
                        if not _readiness_requests_nia_bootstrap(
                            request, post_bootstrap_readiness
                        ):
                            raise RuntimeError(
                                "N.I.A. 已開始，但初始頁面不可接續："
                                + "; ".join(post_bootstrap_readiness.blockers)
                            )
                        time.sleep(0.1)
                    if (
                        post_bootstrap_readiness is None
                        or not post_bootstrap_readiness.ready
                    ):
                        raise RuntimeError(
                            "N.I.A. 已開始，但 30 秒內未建立可讀 LocalSave"
                        )
                    progress_callback(
                        {
                            "current_page": post_bootstrap_readiness.current_page,
                            "outer_action_count": 0,
                        }
                    )
                assert request.stage_number is not None
                result = self._runner(
                    idol_card_id=request.idol_card_id,
                    plan_type=request.plan_type,
                    produce_id=request.produce_id,
                    game_root=request.game_root,
                    expected_run_id=request.expected_run_id,
                    stage_number=request.stage_number,
                    stop_requested=stop_event.is_set,
                    progress_callback=progress_callback,
                    plan2_max_actions=request.plan2_max_actions,
                    plan2_policy_bundle_path=request.plan2_policy_bundle_path,
                    audition_strategy=request.audition_strategy,
                    **({"exam_policy_variant": request.exam_policy_variant}
                       if request.exam_policy_variant is not None else {}),
                )
                if not isinstance(result, InitialRegularAutopilotResult):
                    raise TypeError("live runner returned an unsupported result")
                completed_run_evidence: Mapping[str, Any] | None = None
                native_receipt = getattr(result, "native_completed_run_evidence", None) is not None
                if result.completed and (native_receipt or self._completed_run_finalizer is not None):
                    progress_callback(
                        {
                            "current_page": "evidence-finalizing",
                            "outer_action_count": len(result.steps),
                            "source_run_id": resolved_run_id[0],
                        }
                    )
                    if native_receipt:
                        try:
                            from .native_completed_run_receipt import reuse_native_completed_run_evidence

                            completed_run_evidence = dict(reuse_native_completed_run_evidence(
                                result, request, resolved_run_id[0]))
                        except Exception as error:
                            completed_run_evidence = {
                                "status": "error", "complete": False, "evidence_complete": False,
                                "reason": f"native-completed-evidence-unavailable:{type(error).__name__}:{error}",
                            }
                    elif resolved_run_id[0] is None:
                        completed_run_evidence = {
                            "status": "skipped",
                            "complete": False,
                            "reason": "completed-run-id-unavailable",
                        }
                    else:
                        try:
                            completed_run_evidence = dict(
                                self._completed_run_finalizer(
                                    result,
                                    request,
                                    resolved_run_id[0],
                                )
                            )
                        except Exception as error:
                            completed_run_evidence = {
                                "status": "error",
                                "complete": False,
                                "reason": (
                                    f"{type(error).__name__}: {error}"
                                ),
                            }
            except Exception as error:
                self._queue.put(_LiveWorkerMessage(generation, error=error))
            else:
                self._queue.put(
                    _LiveWorkerMessage(
                        generation,
                        result=result,
                        completed_run_evidence=completed_run_evidence,
                    )
                )

        self._submit(worker)
        return self._view

    def stop(self) -> InitialRegularLiveView:
        if self._view.phase != InitialRegularSimulatorPhase.RUNNING:
            return self._view
        assert self._stop_event is not None
        self._stop_event.set()
        if self._monitor_stop_event is not None:
            self._monitor_stop_event.set()
        self._view = format_initial_regular_live_view(
            self._request,
            phase=InitialRegularSimulatorPhase.CANCELLING,
            readiness=self._readiness,
            progress=self._progress,
        )
        return self._view

    def poll(self) -> InitialRegularLiveView:
        while True:
            try:
                message = self._queue.get_nowait()
            except queue.Empty:
                break
            if message.generation != self._generation:
                continue
            if terminal_bookkeeping_pending(self._view):
                # A late monitor update cannot erase the preserved completion
                # and its separate cleanup warning. This is not reconciliation.
                continue
            if isinstance(message.monitor_snapshot, Mapping):
                progress = dict(self._progress or {})
                progress["current_page"] = str(
                    message.monitor_snapshot.get(
                        "page",
                        progress.get("current_page", PAGE_UNKNOWN),
                    )
                )
                progress["monitor_snapshot"] = dict(message.monitor_snapshot)
                self._progress = progress
                self._view = format_initial_regular_live_view(
                    self._request,
                    phase=self._view.phase,
                    readiness=self._readiness,
                    progress=progress,
                )
            elif message.progress is not None:
                self._progress = message.progress
                phase = (
                    InitialRegularSimulatorPhase.CANCELLING
                    if self._stop_event is not None and self._stop_event.is_set()
                    else InitialRegularSimulatorPhase.RUNNING
                )
                self._view = format_initial_regular_live_view(
                    self._request,
                    phase=phase,
                    readiness=self._readiness,
                    progress=self._progress,
                )
            elif message.error is not None:
                monitor_stop_event = self._monitor_stop_event
                if monitor_stop_event is not None:
                    monitor_stop_event.set()
                self._view = format_initial_regular_live_view(
                    self._request,
                    readiness=self._readiness,
                    progress=self._progress,
                    error=message.error,
                )
                self._stop_event = None
                self._monitor_stop_event = None
            elif message.result is not None:
                monitor_stop_event = self._monitor_stop_event
                if monitor_stop_event is not None:
                    monitor_stop_event.set()
                self._last_completed_run_evidence = (
                    None
                    if message.completed_run_evidence is None
                    else dict(message.completed_run_evidence)
                )
                self._view = format_initial_regular_live_view(
                    self._request,
                    readiness=self._readiness,
                    progress=self._progress,
                    result=message.result,
                )
                self._stop_event = None
                self._monitor_stop_event = None
        return self._view


class InitialRegularSimulatorPanel(ttk.Frame):
    """Small Tk renderer; all behavior remains in the headless controller."""

    def __init__(
        self,
        parent,
        controller: InitialRegularSimulatorController | None = None,
        *,
        plan2_controller: Plan2OfflineSimulatorController | None = None,
        live_controller: InitialRegularLiveController | None = None,
        poll_interval_ms: int = 100,
    ) -> None:
        super().__init__(parent, padding=12)
        self.controller = controller or InitialRegularSimulatorController()
        self.plan2_controller = plan2_controller or Plan2OfflineSimulatorController()
        self.live_controller = live_controller or InitialRegularLiveController()
        self.poll_interval_ms = poll_interval_ms
        self._after_id: str | None = None
        self.summary_var = tk.StringVar()
        self.completeness_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.action_var = tk.StringVar()
        self.reason_var = tk.StringVar()
        self.blockers_var = tk.StringVar()
        self.details_var = tk.StringVar()
        self.details_expanded_var = tk.BooleanVar(value=False)
        self.plan2_source_var = tk.StringVar(
            value=self.plan2_controller.request.source_label
        )
        self.plan2_path_var = tk.StringVar(
            value=self.plan2_controller.request.exam_save_path
        )
        self.plan2_fixed_run_plan_var = tk.StringVar(
            value=self.plan2_controller.request.fixed_run_plan_label
        )
        self.plan2_fixed_run_scenario_path_var = tk.StringVar(
            value=self.plan2_controller.request.fixed_run_scenario_path
        )
        self.plan2_nia_runtime_schedule_path_var = tk.StringVar(
            value=self.plan2_controller.request.nia_runtime_schedule_path
        )
        # Route terminology is retained as a view-only alias for callers that
        # use the projection module's name.
        self.plan2_nia_runtime_route_path_var = (
            self.plan2_nia_runtime_schedule_path_var
        )
        self.plan2_nia_runtime_response_path_var = tk.StringVar(
            value=self.plan2_controller.request.nia_runtime_response_path
        )
        self.plan2_plan1_run_runtime_loadout_path_var = tk.StringVar(
            value=(
                self.plan2_controller.request.plan1_run_runtime_loadout_path
            )
        )
        self.plan2_nia_compare_week_var = tk.StringVar(
            value=str(self.plan2_controller.request.nia_compare_week)
        )
        self.plan2_nia_monte_carlo_samples_var = tk.StringVar(
            value=str(self.plan2_controller.request.nia_monte_carlo_samples)
        )
        self.plan2_coverage_var = tk.StringVar()
        self.plan2_state_var = tk.StringVar()
        self.plan2_status_var = tk.StringVar()
        self.plan2_action_var = tk.StringVar()
        self.plan2_path_result_var = tk.StringVar()
        self.plan2_blockers_var = tk.StringVar()
        self.plan2_self_play_var = tk.StringVar()
        self.plan2_snapshot_summary_var = tk.StringVar()
        self.plan2_snapshot_aux_var = tk.StringVar()
        self.plan2_fixed_run_summary_var = tk.StringVar(
            value="完整培育流程：尚未執行"
        )
        self.plan2_fixed_run_deck_summary_var = tk.StringVar(
            value="最終牌庫：尚未執行"
        )
        self.plan2_fixed_run_milestone_var = tk.StringVar(
            value="課程／演出結果：尚無資料"
        )
        self.live_idol_var = tk.StringVar(
            value=self.live_controller.request.idol_card_id
        )
        self.live_plan_var = tk.StringVar(
            value=_LIVE_PLAN_LABELS.get(
                self.live_controller.request.plan_type,
                self.live_controller.request.plan_type,
            )
        )
        self.live_mode_var = tk.StringVar(
            value=_LIVE_MODE_LABELS.get(
                self.live_controller.request.produce_id,
                self.live_controller.request.produce_id,
            )
        )
        self.live_stage_var = tk.StringVar(
            value=(
                ""
                if self.live_controller.request.stage_number is None
                else str(self.live_controller.request.stage_number)
            )
        )
        self.live_readiness_var = tk.StringVar()
        self.live_page_var = tk.StringVar()
        self.live_action_count_var = tk.StringVar()
        self.live_recent_action_var = tk.StringVar()
        self.live_stop_reason_var = tk.StringVar()
        self.live_diff_var = tk.StringVar()
        self.live_blockers_var = tk.StringVar()
        self._build()
        self._render(self.controller.view)
        self._render_plan2(self.plan2_controller.view)
        self._render_live(self.live_controller.view)
        self._after_id = self.after(self.poll_interval_ms, self._poll)

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        summary = ttk.LabelFrame(self, text="培育狀態摘要", padding=10)
        summary.grid(row=0, column=0, sticky="ew")
        ttk.Label(summary, textvariable=self.summary_var).pack(anchor="w")
        ttk.Label(summary, textvariable=self.completeness_var).pack(
            anchor="w", pady=(5, 0)
        )

        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, sticky="ew", pady=10)
        self.start_button = ttk.Button(
            controls, text="開始計算", command=self._start
        )
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(
            controls, text="取消", command=self._cancel
        )
        self.cancel_button.pack(side="left", padx=(8, 0))

        result = ttk.LabelFrame(self, text="完整模擬建議", padding=10)
        result.grid(row=2, column=0, sticky="ew")
        ttk.Label(result, textvariable=self.status_var).pack(anchor="w")
        ttk.Label(
            result, textvariable=self.action_var, font=("Microsoft JhengHei UI", 12, "bold")
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(
            result, textvariable=self.reason_var, wraplength=980, justify="left"
        ).pack(anchor="w", pady=(5, 0))
        ttk.Label(
            result,
            textvariable=self.blockers_var,
            wraplength=980,
            justify="left",
            foreground="#9a3412",
        ).pack(anchor="w", pady=(8, 0))

        ttk.Checkbutton(
            self,
            text="展開技術細節",
            variable=self.details_expanded_var,
            command=self._toggle_details,
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.details_label = ttk.Label(
            self,
            textvariable=self.details_var,
            wraplength=980,
            justify="left",
        )

        plan2 = ttk.LabelFrame(
            self,
            text="離線模擬（Plan2 單場／跨流派整場）",
            padding=10,
        )
        plan2.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        plan2.columnconfigure(1, weight=1)
        ttk.Label(plan2, text="資料來源").grid(row=0, column=0, sticky="w")
        self.plan2_source_combo = ttk.Combobox(
            plan2,
            textvariable=self.plan2_source_var,
            values=tuple(_PLAN2_SOURCE_LABELS.values()),
            state="readonly",
            width=26,
        )
        self.plan2_source_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.plan2_source_combo.bind("<<ComboboxSelected>>", self._plan2_source_changed)

        ttk.Label(plan2, text="ExamSaveData").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.plan2_path_entry = ttk.Entry(
            plan2, textvariable=self.plan2_path_var
        )
        self.plan2_path_entry.grid(
            row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0)
        )
        ttk.Button(plan2, text="選擇檔案", command=self._browse_plan2_exam_save).grid(
            row=1, column=2, sticky="w", pady=(8, 0)
        )

        plan2_controls = ttk.Frame(plan2)
        plan2_controls.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.plan2_decision_button = ttk.Button(
            plan2_controls,
            text="計算離線建議",
            command=self._start_plan2_decision,
        )
        self.plan2_decision_button.pack(side="left")
        self.plan2_self_play_button = ttk.Button(
            plan2_controls,
            text="固定 seed self-play 驗收",
            command=self._start_plan2_self_play,
        )
        self.plan2_self_play_button.pack(side="left", padx=(8, 0))
        ttk.Label(plan2_controls, text="固定 scenario").pack(
            side="left", padx=(16, 4)
        )
        self.plan2_fixed_run_plan_combo = ttk.Combobox(
            plan2_controls,
            textvariable=self.plan2_fixed_run_plan_var,
            values=tuple(_OFFLINE_FIXED_RUN_PLAN_LABELS.values()),
            state="readonly",
            width=34,
        )
        self.plan2_fixed_run_plan_combo.pack(side="left")
        self.plan2_fixed_run_button = ttk.Button(
            plan2_controls,
            text="跑選擇的離線驗收",
            command=self._start_plan2_fixed_run,
        )
        self.plan2_fixed_run_button.pack(side="left", padx=(8, 0))
        ttk.Label(plan2_controls, text="NIA scenario JSON").pack(
            side="left", padx=(16, 4)
        )
        self.plan2_fixed_run_scenario_entry = ttk.Entry(
            plan2_controls,
            textvariable=self.plan2_fixed_run_scenario_path_var,
            width=24,
        )
        self.plan2_fixed_run_scenario_entry.pack(side="left")
        ttk.Button(
            plan2_controls,
            text="選擇",
            command=self._browse_plan2_fixed_run_scenario,
        ).pack(side="left", padx=(4, 0))
        ttk.Label(plan2_controls, text="NIA ProduceSchedule JSON").pack(
            side="left", padx=(12, 4)
        )
        self.plan2_nia_runtime_schedule_entry = ttk.Entry(
            plan2_controls,
            textvariable=self.plan2_nia_runtime_schedule_path_var,
            width=24,
        )
        self.plan2_nia_runtime_schedule_entry.pack(side="left")
        ttk.Button(
            plan2_controls,
            text="選擇",
            command=self._browse_plan2_nia_runtime_schedule,
        ).pack(side="left", padx=(4, 0))
        ttk.Label(plan2_controls, text="NIA Response Bundle JSON").pack(
            side="left", padx=(12, 4)
        )
        self.plan2_nia_runtime_response_entry = ttk.Entry(
            plan2_controls,
            textvariable=self.plan2_nia_runtime_response_path_var,
            width=24,
        )
        self.plan2_nia_runtime_response_entry.pack(side="left")
        ttk.Button(
            plan2_controls,
            text="瀏覽",
            command=self._browse_plan2_nia_runtime_response,
        ).pack(side="left", padx=(4, 0))
        ttk.Label(plan2_controls, text="比較週").pack(
            side="left", padx=(10, 4)
        )
        ttk.Spinbox(
            plan2_controls,
            textvariable=self.plan2_nia_compare_week_var,
            from_=1,
            to=27,
            width=3,
        ).pack(side="left")
        ttk.Label(plan2_controls, text="RNG樣本").pack(
            side="left", padx=(8, 4)
        )
        ttk.Spinbox(
            plan2_controls,
            textvariable=self.plan2_nia_monte_carlo_samples_var,
            from_=1,
            to=4096,
            width=5,
        ).pack(side="left")

        plan1_loadout_controls = ttk.Frame(plan2)
        plan1_loadout_controls.grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        plan1_loadout_controls.columnconfigure(1, weight=1)
        ttk.Label(
            plan1_loadout_controls,
            text="Plan1 執行期 Loadout JSON",
        ).grid(row=0, column=0, sticky="w")
        self.plan2_plan1_run_runtime_loadout_entry = ttk.Entry(
            plan1_loadout_controls,
            textvariable=self.plan2_plan1_run_runtime_loadout_path_var,
        )
        self.plan2_plan1_run_runtime_loadout_entry.grid(
            row=0, column=1, sticky="ew", padx=(8, 8)
        )
        ttk.Button(
            plan1_loadout_controls,
            text="瀏覽",
            command=self._browse_plan2_plan1_run_runtime_loadout,
        ).grid(row=0, column=2, sticky="w")

        ttk.Label(plan2, textvariable=self.plan2_coverage_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        ttk.Label(plan2, textvariable=self.plan2_state_var).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        ttk.Label(plan2, textvariable=self.plan2_status_var).grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            plan2,
            textvariable=self.plan2_action_var,
            font=("Microsoft JhengHei UI", 11, "bold"),
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(5, 0))
        ttk.Label(
            plan2,
            textvariable=self.plan2_path_result_var,
            wraplength=950,
            justify="left",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(
            plan2,
            textvariable=self.plan2_blockers_var,
            wraplength=950,
            justify="left",
            foreground="#9a3412",
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Label(
            plan2,
            textvariable=self.plan2_self_play_var,
            wraplength=950,
            justify="left",
        ).grid(row=10, column=0, columnspan=3, sticky="w", pady=(4, 0))

        live = ttk.LabelFrame(self, text="無人培育", padding=10)
        snapshot_frame = ttk.LabelFrame(plan2, text="精確牌庫", padding=6)
        snapshot_frame.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        snapshot_frame.columnconfigure(0, weight=1)
        self.plan2_snapshot_tree = ttk.Treeview(
            snapshot_frame,
            columns=("zone", "order", "card_id", "upgrade", "guid"),
            show="headings",
            height=7,
        )
        headings = {
            "zone": "區域",
            "order": "順序",
            "card_id": "卡牌",
            "upgrade": "強化",
            "guid": "GUID",
        }
        widths = {"zone": 130, "order": 60, "card_id": 220, "upgrade": 90, "guid": 260}
        for column, heading in headings.items():
            self.plan2_snapshot_tree.heading(column, text=heading)
            self.plan2_snapshot_tree.column(column, width=widths[column], anchor="w")
        snapshot_scroll = ttk.Scrollbar(
            snapshot_frame,
            orient="vertical",
            command=self.plan2_snapshot_tree.yview,
        )
        self.plan2_snapshot_tree.configure(yscrollcommand=snapshot_scroll.set)
        self.plan2_snapshot_tree.grid(row=0, column=0, sticky="nsew")
        snapshot_scroll.grid(row=0, column=1, sticky="ns")
        ttk.Label(
            snapshot_frame,
            textvariable=self.plan2_snapshot_summary_var,
            wraplength=950,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(
            snapshot_frame,
            textvariable=self.plan2_snapshot_aux_var,
            wraplength=950,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(3, 0))

        fixed_run_frame = ttk.LabelFrame(plan2, text="完整培育流程", padding=6)
        fixed_run_frame.grid(
            row=12, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )
        fixed_run_frame.columnconfigure(0, weight=1)
        self.plan2_fixed_run_tree = ttk.Treeview(
            fixed_run_frame,
            columns=(
                "week",
                "kind",
                "action_request",
                "branch",
                "stamina",
                "attributes",
                "deck",
                "phase_lifecycle",
            ),
            show="headings",
            height=8,
        )
        fixed_run_headings = {
            "week": "週",
            "kind": "類型",
            "action_request": "動作／request",
            "branch": "branch",
            "stamina": "體力前後",
            "attributes": "VoDaVi 前後",
            "deck": "牌數前後",
            "phase_lifecycle": "phase／lifecycle",
        }
        fixed_run_widths = {
            "week": 48,
            "kind": 78,
            "action_request": 260,
            "branch": 180,
            "stamina": 95,
            "attributes": 260,
            "deck": 90,
            "phase_lifecycle": 260,
        }
        for column, heading in fixed_run_headings.items():
            self.plan2_fixed_run_tree.heading(column, text=heading)
            self.plan2_fixed_run_tree.column(
                column, width=fixed_run_widths[column], anchor="w"
            )
        fixed_run_scroll = ttk.Scrollbar(
            fixed_run_frame,
            orient="vertical",
            command=self.plan2_fixed_run_tree.yview,
        )
        self.plan2_fixed_run_tree.configure(yscrollcommand=fixed_run_scroll.set)
        self.plan2_fixed_run_tree.grid(row=0, column=0, sticky="nsew")
        fixed_run_scroll.grid(row=0, column=1, sticky="ns")
        ttk.Label(
            fixed_run_frame,
            textvariable=self.plan2_fixed_run_summary_var,
            wraplength=950,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(
            fixed_run_frame,
            textvariable=self.plan2_fixed_run_deck_summary_var,
            wraplength=950,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(
            fixed_run_frame,
            textvariable=self.plan2_fixed_run_milestone_var,
            wraplength=950,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.plan2_fixed_run_deck_tree = ttk.Treeview(
            fixed_run_frame,
            columns=("card_id", "upgrade", "count", "identity"),
            show="headings",
            height=5,
        )
        for column, heading, width in (
            ("card_id", "最終卡牌", 360),
            ("upgrade", "強化", 90),
            ("count", "張數", 80),
            ("identity", "GUID／識別", 260),
        ):
            self.plan2_fixed_run_deck_tree.heading(column, text=heading)
            self.plan2_fixed_run_deck_tree.column(column, width=width, anchor="w")
        fixed_run_deck_scroll = ttk.Scrollbar(
            fixed_run_frame,
            orient="vertical",
            command=self.plan2_fixed_run_deck_tree.yview,
        )
        self.plan2_fixed_run_deck_tree.configure(
            yscrollcommand=fixed_run_deck_scroll.set
        )
        self.plan2_fixed_run_deck_tree.grid(row=4, column=0, sticky="nsew", pady=(3, 0))
        fixed_run_deck_scroll.grid(row=4, column=1, sticky="ns", pady=(3, 0))

        live.grid(row=6, column=0, sticky="ew", pady=(12, 0))
        live.columnconfigure(1, weight=1)
        ttk.Label(live, text="偶像卡").grid(row=0, column=0, sticky="w")
        ttk.Entry(live, textvariable=self.live_idol_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )
        ttk.Label(live, text="培育計畫").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Combobox(
            live,
            textvariable=self.live_plan_var,
            values=tuple(_LIVE_PLAN_LABELS.values()),
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        ttk.Label(live, text="舞台編號").grid(
            row=2, column=0, sticky="w", pady=(8, 0)
        )
        ttk.Spinbox(
            live,
            textvariable=self.live_stage_var,
            from_=1,
            to=9,
            width=8,
        ).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(8, 0))
        live_controls = ttk.Frame(live)
        live_controls.grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        self.live_start_button = ttk.Button(
            live_controls,
            text="開始無人培育",
            command=self._start_live,
        )
        self.live_start_button.pack(side="left")
        self.live_stop_button = ttk.Button(
            live_controls,
            text="停止",
            command=self._stop_live,
        )
        self.live_stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(live, textvariable=self.live_readiness_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )
        ttk.Label(live, textvariable=self.live_page_var).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(live, textvariable=self.live_action_count_var).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(live, textvariable=self.live_recent_action_var).grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(live, textvariable=self.live_stop_reason_var).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(live, textvariable=self.live_diff_var).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        ttk.Label(
            live,
            textvariable=self.live_blockers_var,
            wraplength=950,
            justify="left",
            foreground="#9a3412",
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(live, text="培育模式").grid(
            row=11, column=0, sticky="w", pady=(8, 0)
        )
        self.live_mode_combo = ttk.Combobox(
            live,
            textvariable=self.live_mode_var,
            values=tuple(_LIVE_MODE_LABELS.values()),
            state="readonly",
            width=18,
        )
        self.live_mode_combo.grid(
            row=11, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )
        self.live_mode_combo.bind("<<ComboboxSelected>>", self._live_mode_changed)

    def set_request(self, request: InitialRegularSimulatorRequest) -> None:
        self._render(self.controller.set_request(request))

    def _start(self) -> None:
        self._render(self.controller.start())

    def _cancel(self) -> None:
        self._render(self.controller.cancel())

    def _toggle_details(self) -> None:
        self._render(
            self.controller.set_details_expanded(self.details_expanded_var.get())
        )

    def _sync_plan2_request(self) -> Plan2OfflineSimulatorView:
        source = next(
            key
            for key, label in _PLAN2_SOURCE_LABELS.items()
            if label == self.plan2_source_var.get()
        )
        request = replace(
            self.plan2_controller.request,
            source=source,
            exam_save_path=self.plan2_path_var.get().strip(),
            fixed_run_scenario_path=(
                self.plan2_fixed_run_scenario_path_var.get().strip()
            ),
            nia_runtime_schedule_path=(
                self.plan2_nia_runtime_schedule_path_var.get().strip()
            ),
            nia_runtime_route_path=(
                self.plan2_nia_runtime_schedule_path_var.get().strip()
            ),
            nia_runtime_response_path=(
                self.plan2_nia_runtime_response_path_var.get().strip()
            ),
            plan1_run_runtime_loadout_path=(
                self.plan2_plan1_run_runtime_loadout_path_var.get().strip()
            ),
            nia_compare_week=int(self.plan2_nia_compare_week_var.get()),
            nia_monte_carlo_samples=int(
                self.plan2_nia_monte_carlo_samples_var.get()
            ),
            fixed_run_plan=next(
                key
                for key, label in _OFFLINE_FIXED_RUN_PLAN_LABELS.items()
                if label == self.plan2_fixed_run_plan_var.get()
            ),
        )
        return self.plan2_controller.set_request(request)

    def _plan2_source_changed(self, _event=None) -> None:
        self._render_plan2(self._sync_plan2_request())

    def _browse_plan2_exam_save(self) -> None:
        path = filedialog.askopenfilename(title="選擇 ExamSaveData")
        if path:
            self.plan2_path_var.set(path)
            self.plan2_source_var.set(
                _PLAN2_SOURCE_LABELS[Plan2OfflineSimulatorSource.EXAM_SAVE_DATA]
            )
            self._render_plan2(self._sync_plan2_request())

    def _browse_plan2_fixed_run_scenario(self) -> None:
        path = filedialog.askopenfilename(
            title="選擇 NIA scenario JSON",
            filetypes=(("JSON", "*.json"), ("所有檔案", "*.*")),
        )
        if path:
            self.plan2_fixed_run_scenario_path_var.set(path)
            json_modes = {
                _OFFLINE_FIXED_RUN_PLAN_LABELS[
                    OfflineFixedRunPlan.NIA_PRO_FULL
                ],
                _OFFLINE_FIXED_RUN_PLAN_LABELS[
                    OfflineFixedRunPlan.NIA_PRO_LESSON_COMPARE
                ],
                _OFFLINE_FIXED_RUN_PLAN_LABELS[
                    OfflineFixedRunPlan.NIA_PRO_WEEKLY_COMPARE
                ],
                _OFFLINE_FIXED_RUN_PLAN_LABELS[
                    OfflineFixedRunPlan.NIA_PRO_MONTE_CARLO
                ],
            }
            if self.plan2_fixed_run_plan_var.get() not in json_modes:
                self.plan2_fixed_run_plan_var.set(
                    _OFFLINE_FIXED_RUN_PLAN_LABELS[
                        OfflineFixedRunPlan.NIA_PRO_FULL
                    ]
                )
            self._render_plan2(self._sync_plan2_request())

    def _browse_plan2_nia_runtime_schedule(self) -> None:
        path = filedialog.askopenfilename(
            title="選擇 NIA ProduceSchedule JSON",
            filetypes=(("JSON", "*.json"), ("所有檔案", "*.*")),
        )
        if path:
            self.plan2_nia_runtime_schedule_path_var.set(path)
            runtime_plans = {
                _OFFLINE_FIXED_RUN_PLAN_LABELS[OfflineFixedRunPlan.NIA_PRO_FULL],
                _OFFLINE_FIXED_RUN_PLAN_LABELS[
                    OfflineFixedRunPlan.NIA_MASTER_PREFIX
                ],
            }
            if self.plan2_fixed_run_plan_var.get() not in runtime_plans:
                self.plan2_fixed_run_plan_var.set(
                    _OFFLINE_FIXED_RUN_PLAN_LABELS[
                        OfflineFixedRunPlan.NIA_PRO_FULL
                    ]
                )
            self._render_plan2(self._sync_plan2_request())

    def _browse_plan2_nia_runtime_response(self) -> None:
        path = filedialog.askopenfilename(
            title="選擇 NIA Runtime Response Bundle JSON",
            filetypes=(("JSON", "*.json"), ("所有檔案", "*.*")),
        )
        if path:
            self.plan2_nia_runtime_response_path_var.set(path)
            runtime_plans = {
                _OFFLINE_FIXED_RUN_PLAN_LABELS[OfflineFixedRunPlan.NIA_PRO_FULL],
                _OFFLINE_FIXED_RUN_PLAN_LABELS[
                    OfflineFixedRunPlan.NIA_MASTER_PREFIX
                ],
            }
            if self.plan2_fixed_run_plan_var.get() not in runtime_plans:
                self.plan2_fixed_run_plan_var.set(
                    _OFFLINE_FIXED_RUN_PLAN_LABELS[
                        OfflineFixedRunPlan.NIA_PRO_FULL
                    ]
                )
            self._render_plan2(self._sync_plan2_request())

    def _browse_plan2_plan1_run_runtime_loadout(self) -> None:
        path = filedialog.askopenfilename(
            title="選擇 Plan1 執行期 Loadout JSON",
            filetypes=(("JSON", "*.json"), ("所有檔案", "*.*")),
        )
        if path:
            self.plan2_plan1_run_runtime_loadout_path_var.set(path)
            self.plan2_fixed_run_plan_var.set(
                _OFFLINE_FIXED_RUN_PLAN_LABELS[OfflineFixedRunPlan.PLAN1]
            )
            self._render_plan2(self._sync_plan2_request())

    def _start_plan2_decision(self) -> None:
        self._sync_plan2_request()
        self._render_plan2(self.plan2_controller.start_decision())

    def _start_plan2_self_play(self) -> None:
        self._sync_plan2_request()
        self._render_plan2(self.plan2_controller.start_self_play())

    def _start_plan2_fixed_run(self) -> None:
        self._sync_plan2_request()
        self._render_plan2(self.plan2_controller.start_fixed_run())

    def _sync_live_request(self) -> InitialRegularLiveView:
        plan_type = next(
            (
                key
                for key, label in _LIVE_PLAN_LABELS.items()
                if label == self.live_plan_var.get()
            ),
            self.live_plan_var.get().strip(),
        )
        try:
            stage_number: int | None = int(self.live_stage_var.get().strip())
        except ValueError:
            stage_number = None
        produce_id = next(
            (
                key
                for key, label in _LIVE_MODE_LABELS.items()
                if label == self.live_mode_var.get()
            ),
            self.live_mode_var.get().strip(),
        )
        if produce_id in {"produce-004", "produce-005"}:
            plan_type = PLAN3
            self.live_plan_var.set(_LIVE_PLAN_LABELS[PLAN3])
        request = replace(
            self.live_controller.request,
            idol_card_id=self.live_idol_var.get().strip(),
            plan_type=plan_type,
            produce_id=produce_id,
            stage_number=stage_number,
        )
        return self.live_controller.set_request(request)

    def _live_mode_changed(self, _event=None) -> None:
        label = self.live_mode_var.get()
        produce_id = next(
            (key for key, value in _LIVE_MODE_LABELS.items() if value == label),
            label,
        )
        if produce_id in {"produce-004", "produce-005"}:
            self.live_plan_var.set(_LIVE_PLAN_LABELS[PLAN3])
            if self.live_idol_var.get().strip() == "i_card-fktn-3-007":
                self.live_idol_var.set("i_card-fktn-3-011")
        elif self.live_idol_var.get().strip() == "i_card-fktn-3-011":
            self.live_idol_var.set("i_card-fktn-3-007")
        self._render_live(self._sync_live_request())

    def _start_live(self) -> None:
        self._sync_live_request()
        self._render_live(self.live_controller.start())

    def _stop_live(self) -> None:
        self._render_live(self.live_controller.stop())

    def _poll(self) -> None:
        self._render(self.controller.poll())
        self._render_plan2(self.plan2_controller.poll())
        self._render_live(self.live_controller.poll())
        self._after_id = self.after(self.poll_interval_ms, self._poll)

    def _render(self, view: InitialRegularSimulatorView) -> None:
        self.summary_var.set(view.state_summary)
        self.completeness_var.set(view.completeness_text)
        self.status_var.set(view.status_text)
        self.action_var.set(view.best_action_text)
        self.reason_var.set(view.reason_text)
        self.blockers_var.set("\n".join(f"• {value}" for value in view.blockers))
        self.details_var.set("\n".join(view.technical_details))
        self.start_button.configure(state="normal" if view.start_enabled else "disabled")
        self.cancel_button.configure(state="normal" if view.cancel_enabled else "disabled")
        self.details_expanded_var.set(view.details_expanded)
        if view.details_expanded:
            self.details_label.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        else:
            self.details_label.grid_remove()

    def _render_plan2(self, view: Plan2OfflineSimulatorView) -> None:
        self.plan2_coverage_var.set(view.coverage_text)
        self.plan2_state_var.set(view.state_text)
        self.plan2_status_var.set(view.status_text)
        self.plan2_action_var.set(view.recommended_action_text)
        self.plan2_path_result_var.set(view.predicted_path_text)
        self.plan2_blockers_var.set(view.blockers_text)
        self.plan2_self_play_var.set(view.self_play_summary_text)
        snapshot = view.deck_snapshot
        self.plan2_snapshot_summary_var.set(
            view.deck_snapshot_summary or _plan2_snapshot_summary(snapshot)
        )
        self.plan2_snapshot_aux_var.set(_plan2_snapshot_aux_summary(snapshot))
        for item in self.plan2_snapshot_tree.get_children():
            self.plan2_snapshot_tree.delete(item)
        for row_index, row in enumerate(_plan2_snapshot_rows(snapshot)):
            zone, order, card_id, upgrade, guid = row
            self.plan2_snapshot_tree.insert(
                "",
                "end",
                iid=f"plan2-snapshot-{row_index}",
                values=(_plan2_snapshot_zone_label(zone), order, card_id, upgrade, guid),
            )
        self.plan2_fixed_run_summary_var.set(
            _plan2_fixed_run_summary(view.fixed_run_report)
        )
        self.plan2_fixed_run_deck_summary_var.set(
            _plan2_fixed_run_deck_summary(view.fixed_run_report)
        )
        self.plan2_fixed_run_milestone_var.set(
            _plan2_fixed_run_milestone_summary(view.fixed_run_report)
        )
        for item in self.plan2_fixed_run_tree.get_children():
            self.plan2_fixed_run_tree.delete(item)
        for row_index, row in enumerate(_plan2_fixed_run_rows(view.fixed_run_report)):
            self.plan2_fixed_run_tree.insert(
                "",
                "end",
                iid=f"plan2-fixed-run-{row_index}",
                values=row,
            )
        for item in self.plan2_fixed_run_deck_tree.get_children():
            self.plan2_fixed_run_deck_tree.delete(item)
        final_deck = (
            ()
            if view.fixed_run_report is None
            else view.fixed_run_report.final_deck
        )
        for row_index, row in enumerate(
            _plan2_fixed_run_deck_rows(view.fixed_run_report)
        ):
            entry = final_deck[row_index]
            identity = ", ".join(entry.instance_ids) if entry.instance_ids else "未提供"
            self.plan2_fixed_run_deck_tree.insert(
                "",
                "end",
                iid=f"plan2-fixed-run-deck-{row_index}",
                values=(*row, identity),
            )
        self.plan2_decision_button.configure(
            state="normal" if view.decision_enabled else "disabled"
        )
        self.plan2_self_play_button.configure(
            state="normal" if view.self_play_enabled else "disabled"
        )
        self.plan2_fixed_run_button.configure(
            state="normal" if view.self_play_enabled else "disabled"
        )
        self.plan2_fixed_run_plan_combo.configure(
            state="readonly" if view.self_play_enabled else "disabled"
        )
        exam_source = (
            self.plan2_controller.request.source
            == Plan2OfflineSimulatorSource.EXAM_SAVE_DATA
        )
        self.plan2_path_entry.configure(
            state="normal" if exam_source else "disabled"
        )

    def _render_live(self, view: InitialRegularLiveView) -> None:
        self.live_readiness_var.set(view.readiness_text)
        self.live_page_var.set(f"目前頁面：{view.current_page_text}")
        self.live_action_count_var.set(view.action_count_text)
        self.live_recent_action_var.set(view.recent_action_text)
        self.live_stop_reason_var.set(view.stop_reason_text)
        self.live_diff_var.set(view.diff_summary_text)
        details = (*view.blockers, *view.technical_details)
        self.live_blockers_var.set("\n".join(details))
        self.live_start_button.configure(
            state="normal" if view.start_enabled else "disabled"
        )
        self.live_stop_button.configure(
            state="normal" if view.stop_enabled else "disabled"
        )

    def destroy(self) -> None:
        self.controller.cancel()
        self.plan2_controller.set_request(self.plan2_controller.request)
        self.live_controller.stop()
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None
        super().destroy()


def mount_initial_regular_simulator_tab(
    notebook: ttk.Notebook,
    controller: InitialRegularSimulatorController | None = None,
) -> InitialRegularSimulatorPanel:
    """Public GUI entry point for embedding the first simulator tab."""

    panel = InitialRegularSimulatorPanel(notebook, controller)
    notebook.add(panel, text="Initial 離線整場模擬")
    return panel


__all__ = [
    "InitialRegularLiveController",
    "InitialRegularLiveReadiness",
    "InitialRegularLiveRequest",
    "InitialRegularLiveView",
    "InitialRegularSimulatorController",
    "InitialRegularSimulatorDataInventory",
    "InitialRegularSimulatorDataKind",
    "InitialRegularSimulatorPanel",
    "InitialRegularSimulatorPhase",
    "InitialRegularSimulatorRequest",
    "InitialRegularSimulatorView",
    "NiaRuntimeResponseInputError",
    "NiaRuntimeScenarioBindingError",
    "NiaRuntimeScheduleInputError",
    "Plan1RunRuntimeLoadoutInputError",
    "OfflineFixedRunPlan",
    "Plan2OfflineSimulatorController",
    "Plan2OfflineSimulatorOperation",
    "Plan2OfflineSimulatorRequest",
    "Plan2OfflineSimulatorSource",
    "Plan2OfflineSimulatorView",
    "check_initial_regular_live_readiness",
    "format_initial_regular_simulator_view",
    "format_initial_regular_live_view",
    "format_plan2_offline_simulator_view",
    "mount_initial_regular_simulator_tab",
]
