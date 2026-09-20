"""Shared read-only GUI view for outer actions, lessons, and auditions.

The desktop GUI already receives overview/training results from explicit live
reader jobs and already monitors local saves for the Plan3 audition advisor.
This module joins those existing sources without capturing a window or
creating an executable click. It is Tk-free so routing stays unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .initial_regular_outer_advisor import (
    STATUS_READY,
    InitialRegularOuterAdvice,
    advise_initial_regular_outer,
)
from .nia_outer_advisor import (
    STATUS_READY as NIA_STATUS_READY,
    NiaOuterAdvice,
    advise_nia_outer,
)
from .overview_actions import (
    ACTIVITY,
    CLASS,
    DANCE_LESSON,
    REST,
    VOCAL_LESSON,
    VISUAL_LESSON,
)
from .plan3_audition_advisor_gui import (
    Plan3AuditionAdvisorView,
    advise_plan3_audition_gui_file,
    format_plan3_audition_advisor_report,
    plan3_advisor_mode,
)
from .plan3_lesson_advisor import (
    Plan3LessonAdvisorReport,
    advise_plan3_lesson_file,
)
from .produce_outer_local_save import (
    PRODUCE_LIFECYCLE_FILENAME,
    PRODUCE_PLAY_LOG_FILENAME,
    ProduceOuterLocalSaveSnapshot,
    read_produce_outer_local_save,
)
from .route_calendar import (
    BUSINESS,
    CARE_PACKAGE,
    OUTING,
    SPECIAL_GUIDANCE,
)


SCOPE_OUTER = "outer"
SCOPE_AUDITION = "audition"
SCOPE_LESSON = "lesson"
SCOPE_UNAVAILABLE = "unavailable"

STAGE_OUTER = "outer"
STAGE_AUDITION = "audition"
STAGE_LESSON = "lesson"
STAGE_BUSY = "busy"
STAGE_WAITING = "waiting"
STAGE_INACTIVE = "inactive"

_OUTER_LIVE_KINDS = frozenset({"overview", "training"})
_SCREEN_CHANGING_KINDS = frozenset(
    {
        "calibration",
        "capture",
        "deck",
        "detect",
        "exam",
        "execute",
        "lesson",
        "lesson_result",
        "reward",
        "shop",
    }
)
_ACTION_NAMES: Mapping[str, str] = {
    VOCAL_LESSON: "Vo 課程",
    DANCE_LESSON: "Da 課程",
    VISUAL_LESSON: "Vi 課程",
    REST: "休息",
    ACTIVITY: "活動",
    CLASS: "上課",
    OUTING: "外出",
    BUSINESS: "營業",
    CARE_PACKAGE: "支援物資",
    SPECIAL_GUIDANCE: "特別指導",
}

_NIA_STAGE_NAMES: Mapping[str, str] = {
    "before_mid1": "Mid1 前",
    "mid1": "Mid1 演出",
    "between_mid1_mid2": "Mid1～Mid2",
    "mid2": "Mid2 演出",
    "between_mid2_final": "Mid2～Final",
    "final": "Final 演出",
}

_NIA_AUDITION_NAMES: Mapping[str, str] = {
    "mid1": "Mid1",
    "mid2": "Mid2",
    "final": "Final",
}


@dataclass(frozen=True, slots=True)
class CurrentAdviceView:
    """Text-only view model rendered by the shared current-advice panel."""

    scope: str
    available: bool
    status_text: str
    action_text: str
    prediction_text: str
    reason_text: str
    retry: bool = False


@dataclass(frozen=True, slots=True)
class CurrentAdviceLocalSaveSignature:
    """Cheap marker that also exists while ExamSaveData is absent."""

    path: Path
    outer_size: int
    outer_modified_ns: int
    exam_size: int | None = None
    exam_modified_ns: int | None = None
    lifecycle_size: int | None = None
    lifecycle_modified_ns: int | None = None

    @property
    def outer_marker(self) -> tuple[int, int, int | None, int | None]:
        return (
            self.outer_size,
            self.outer_modified_ns,
            self.lifecycle_size,
            self.lifecycle_modified_ns,
        )


@dataclass(frozen=True, slots=True)
class CurrentAdviceRefreshSignature:
    """Inputs that make one background advisor result current."""

    local_save: CurrentAdviceLocalSaveSignature
    live_revision: int
    produce_id: str = ""

    @property
    def path(self) -> Path:
        return self.local_save.path


@dataclass(slots=True)
class CurrentAdviceRefreshTracker:
    """Run once per local-save/live-reader revision without overlapping."""

    last_submitted: CurrentAdviceRefreshSignature | None = None
    busy: bool = False

    def claim(self, signature: CurrentAdviceRefreshSignature) -> bool:
        if self.busy or signature == self.last_submitted:
            return False
        self.last_submitted = signature
        self.busy = True
        return True

    def finish(self, *, retry: bool = False) -> None:
        self.busy = False
        if retry:
            self.last_submitted = None

    def source_missing(self) -> None:
        if not self.busy:
            self.last_submitted = None


@dataclass(frozen=True, slots=True)
class CurrentAdviceLiveInputs:
    """Latest outputs already produced by the GUI's explicit live jobs."""

    overview_output: Mapping[str, object] | None = None
    training_output: Mapping[str, object] | None = None
    active_kind: str | None = None
    revision: int = 0

    def clear(self) -> "CurrentAdviceLiveInputs":
        if (
            self.overview_output is None
            and self.training_output is None
            and self.active_kind is None
        ):
            return self
        return CurrentAdviceLiveInputs(revision=self.revision + 1)


def observe_current_advice_live_update(
    current: CurrentAdviceLiveInputs,
    kind: str,
    payload: object,
) -> CurrentAdviceLiveInputs:
    """Reduce an existing GUI queue item into advisor input state.

    A training result retains the preceding overview because only the overview
    contains SP badges. Other screen observations and relevant read errors
    invalidate both outputs.
    """

    if not isinstance(current, CurrentAdviceLiveInputs):
        raise TypeError("current must be CurrentAdviceLiveInputs")
    if kind == "overview" and isinstance(payload, Mapping):
        return CurrentAdviceLiveInputs(
            overview_output=dict(payload),
            training_output=None,
            active_kind="overview",
            revision=current.revision + 1,
        )
    if kind == "training" and isinstance(payload, Mapping):
        return CurrentAdviceLiveInputs(
            overview_output=current.overview_output,
            training_output=dict(payload),
            active_kind="training",
            revision=current.revision + 1,
        )
    if kind == "error":
        source = payload[0] if isinstance(payload, tuple) and payload else None
        if source in _OUTER_LIVE_KINDS or source in _SCREEN_CHANGING_KINDS:
            return current.clear()
        return current
    if kind in _SCREEN_CHANGING_KINDS:
        return current.clear()
    return current


def read_current_advice_local_save_signature(
    exam_save_path: str | Path,
) -> CurrentAdviceLocalSaveSignature | None:
    """Read a change marker without requiring an active ExamSaveData file."""

    exam_path = Path(exam_save_path)
    outer_path = exam_path.parent / PRODUCE_PLAY_LOG_FILENAME
    try:
        outer_metadata = outer_path.stat()
    except OSError:
        return None
    if not outer_path.is_file():
        return None

    def metadata(path: Path) -> tuple[int | None, int | None]:
        try:
            value = path.stat()
        except OSError:
            return None, None
        if not path.is_file():
            return None, None
        return value.st_size, value.st_mtime_ns

    exam_size, exam_modified_ns = metadata(exam_path)
    lifecycle_size, lifecycle_modified_ns = metadata(
        exam_path.parent / PRODUCE_LIFECYCLE_FILENAME
    )
    return CurrentAdviceLocalSaveSignature(
        path=exam_path.resolve(),
        outer_size=outer_metadata.st_size,
        outer_modified_ns=outer_metadata.st_mtime_ns,
        exam_size=exam_size,
        exam_modified_ns=exam_modified_ns,
        lifecycle_size=lifecycle_size,
        lifecycle_modified_ns=lifecycle_modified_ns,
    )


def classify_current_outer_stage(
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> str:
    """Classify only stages proven by the serialized outer log."""

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if snapshot.lifecycle is not None and not snapshot.lifecycle.is_in_progress:
        return STAGE_INACTIVE

    latest_step = snapshot.completed_steps[-1] if snapshot.completed_steps else None
    current_step = bool(
        snapshot.last_log_cell_type_name == "step"
        and latest_step is not None
        and latest_step.log_index == snapshot.log_count - 1
    )
    if current_step:
        assert latest_step is not None
        if not latest_step.lines and latest_step.step_type_name.startswith(
            "audition_"
        ):
            return STAGE_AUDITION
        if not latest_step.lines and "lesson_" in latest_step.step_type_name:
            return STAGE_LESSON
        return STAGE_BUSY
    if snapshot.last_log_cell_type_name == "week":
        return STAGE_OUTER
    return STAGE_WAITING


def _unavailable_view(
    reason: str,
    *,
    scope: str = SCOPE_UNAVAILABLE,
    retry: bool = False,
) -> CurrentAdviceView:
    labels = {
        SCOPE_AUDITION: "演出",
        SCOPE_LESSON: "課程",
    }
    label = labels.get(scope, "外層行動")
    action = (
        "第一動作：—"
        if scope in {SCOPE_AUDITION, SCOPE_LESSON}
        else "行動：—"
    )
    return CurrentAdviceView(
        scope=scope,
        available=False,
        status_text=f"{label}｜暫時無法提供建議（unavailable）",
        action_text=action,
        prediction_text="預測：—",
        reason_text=f"{reason}\n僅讀取既有資料，不會自動操作遊戲。",
        retry=retry,
    )


def _number(value: int | None) -> str:
    return "—" if value is None else str(value)


def format_initial_regular_outer_advice(
    advice: InitialRegularOuterAdvice,
) -> CurrentAdviceView:
    """Present the stable outer contract in the shared panel."""

    if not isinstance(advice, InitialRegularOuterAdvice):
        raise TypeError("advice must be InitialRegularOuterAdvice")
    if advice.status != STATUS_READY:
        return _unavailable_view(advice.reason, scope=SCOPE_OUTER)

    name = _ACTION_NAMES.get(advice.action, advice.action)
    if advice.is_sp and advice.attribute is not None:
        name = f"{advice.attribute} SP 課程"
    prediction_parts = [f"HP → {_number(advice.predicted_hp)}"]
    if advice.attribute is not None:
        prediction_parts.append(
            f"{advice.attribute} {_number(advice.current_attribute)} → "
            f"{_number(advice.predicted_attribute)}"
        )
    prediction_parts.append(
        "名目 +"
        f"{_number(advice.nominal_attribute_gain)}／有效 +"
        f"{_number(advice.effective_attribute_gain)}／溢出 "
        f"{_number(advice.overflow_attribute_gain)}"
    )

    reasons: list[str] = []
    if advice.action == REST:
        reasons.append("本次課程會破壞下一堂的體力保留，因此建議先休息。")
    elif advice.attribute is not None:
        reasons.append(
            "SP 優先，並以 Initial 的 1000 上限後有效收益比較目前候選。"
        )
        if advice.overflow_attribute_gain:
            reasons.append(
                f"{advice.attribute} 的名目收益會有 "
                f"{advice.overflow_attribute_gain} 點溢出。"
            )
    reasons.append(advice.reason)
    if advice.gain_estimate_source:
        reasons.append(f"收益估計來源：{advice.gain_estimate_source}。")
    reasons.append("這是唯讀建議，不會建立或送出點擊動作。")
    return CurrentAdviceView(
        scope=SCOPE_OUTER,
        available=True,
        status_text="外層行動｜可提供建議（ready）",
        action_text=f"行動：{name}（{advice.action}）",
        prediction_text="預測：" + "｜".join(prediction_parts),
        reason_text="\n".join(reasons),
    )


def _nia_route_summary(advice: NiaOuterAdvice) -> str | None:
    if advice.difficulty is None:
        return None
    parts = [f"N.I.A. {advice.difficulty}"]
    if advice.week is not None and advice.total_steps is not None:
        parts.append(f"第 {advice.week}/{advice.total_steps} 週")
    if advice.stage is not None:
        parts.append(_NIA_STAGE_NAMES.get(advice.stage, advice.stage))
    if (
        advice.next_audition_stage is not None
        and advice.next_audition_week is not None
    ):
        next_stage = _NIA_AUDITION_NAMES.get(
            advice.next_audition_stage,
            advice.next_audition_stage,
        )
        parts.append(f"下一場 {next_stage}（第 {advice.next_audition_week} 週）")
    return "｜".join(parts)


def format_nia_outer_advice(advice: NiaOuterAdvice) -> CurrentAdviceView:
    """Present one route-qualified N.I.A. outer result without a click."""

    if not isinstance(advice, NiaOuterAdvice):
        raise TypeError("advice must be NiaOuterAdvice")
    route_summary = _nia_route_summary(advice)
    route_label = (
        "N.I.A. 外層行動"
        if advice.difficulty is None
        else f"N.I.A. {advice.difficulty} 外層行動"
    )
    if advice.status != NIA_STATUS_READY:
        details = [
            f"[{issue.code}] {issue.detail}"
            for issue in advice.issues
        ]
        if not details:
            details.append(advice.reason)
        reasons: list[str] = []
        if route_summary is not None:
            reasons.append(f"路線：{route_summary}。")
        reasons.extend(details)
        view = _unavailable_view("\n".join(reasons), scope=SCOPE_OUTER)
        return replace(
            view,
            status_text=f"{route_label}｜暫時無法提供建議（unavailable）",
        )

    name = _ACTION_NAMES.get(advice.action, advice.action)
    if advice.is_sp and advice.attribute is not None:
        name = f"{advice.attribute} SP 課程"
    prediction_parts = [f"HP → {_number(advice.predicted_hp)}"]
    if advice.attribute is not None:
        prediction_parts.append(
            f"{advice.attribute} {_number(advice.current_attribute)} → "
            f"{_number(advice.predicted_attribute)}"
        )
    if advice.attribute_cap is not None:
        prediction_parts.append(f"屬性上限 {advice.attribute_cap}")
    prediction_parts.append(
        "名目 +"
        f"{_number(advice.nominal_attribute_gain)}／有效 +"
        f"{_number(advice.effective_attribute_gain)}／溢出 "
        f"{_number(advice.overflow_attribute_gain)}"
    )

    reasons = []
    if route_summary is not None:
        reasons.append(f"路線：{route_summary}。")
    if advice.estimated_lesson_hp_cost is not None:
        reasons.append(
            f"課程體力 {advice.estimated_lesson_hp_cost}；"
            f"下一堂保留 {_number(advice.reserved_hp_for_next_lesson)}。"
        )
    reasons.append(advice.reason)
    if advice.route_source:
        reasons.append(f"路線證據：{advice.route_source}。")
    if advice.gain_estimate_source:
        reasons.append(f"收益估計來源：{advice.gain_estimate_source}。")
    reasons.append("這是唯讀建議，不會建立或送出點擊動作。")
    return CurrentAdviceView(
        scope=SCOPE_OUTER,
        available=True,
        status_text=f"{route_label}｜可提供建議（ready）",
        action_text=f"行動：{name}（{advice.action}）",
        prediction_text="預測：" + "｜".join(prediction_parts),
        reason_text="\n".join(reasons),
    )


def format_audition_advice_view(
    view: Plan3AuditionAdvisorView,
) -> CurrentAdviceView:
    """Adapt the established Initial/NIA audition presentation."""

    if not isinstance(view, Plan3AuditionAdvisorView):
        raise TypeError("view must be Plan3AuditionAdvisorView")
    return CurrentAdviceView(
        scope=SCOPE_AUDITION,
        available=view.available,
        status_text=f"演出｜{view.status_text}",
        action_text=f"第一動作：{view.action_text}",
        prediction_text=(
            view.terminal_text
            if view.terminal_text.startswith("預測")
            else f"預測：{view.terminal_text}"
        ),
        reason_text=view.reason_text,
    )


def _plan3_first_action_text(action: Mapping[str, object]) -> str:
    kind = action.get("kind")
    if kind == "drink":
        slot = action.get("drink_slot_index")
        position = (
            str(slot + 1)
            if isinstance(slot, int) and not isinstance(slot, bool)
            else "?"
        )
        name = str(action.get("drink_name") or action.get("drink_id") or "未知飲料")
        suffix = "（之後需選擇指定卡牌）" if action.get("selected_card_guid") else ""
        return f"先使用飲料槽 {position}：{name}{suffix}"
    if kind == "card":
        index = action.get("hand_index")
        position = (
            str(index + 1)
            if isinstance(index, int) and not isinstance(index, bool)
            else "?"
        )
        name = str(action.get("card_name") or action.get("card_id") or "未知卡牌")
        upgrade = action.get("card_upgrade")
        suffix = (
            "+"
            if isinstance(upgrade, int)
            and not isinstance(upgrade, bool)
            and upgrade > 0
            else ""
        )
        return f"打出手牌第 {position} 張：{name}{suffix}"
    if kind == "skip":
        return "跳過／結束本回合"
    return "目前沒有可安全顯示的動作"


_LESSON_ISSUE_MESSAGES: Mapping[str, str] = {
    "not-lesson": "目前 ExamSaveData 不是課程；不提供課程動作。",
    "local-save-unavailable": "正在等待遊戲寫入完整且穩定的課程資料。",
    "lesson-state-unavailable": "課程資料尚未完整投影，暫不提供動作。",
    "not-plan3-lesson": "目前不是已支援的 Plan3 正式課程。",
    "lesson-complete": "本堂課程已結束，等待下一個外層可選畫面。",
    "native-state-unavailable": "目前缺少完整 GUID／牌區狀態，無法安全求解。",
    "lesson-search-unavailable": "目前牌組或效果尚未完整支援課程求解。",
    "full-horizon-not-ready": "尚未取得可精確走到課程終局的完整路線。",
    "decision-binding-unavailable": "無法把課程結果可靠對應到目前手牌。",
    "no-executable-action": "目前沒有可安全顯示的出牌、飲料或跳過動作。",
}


def format_plan3_lesson_advisor_report(
    report: Plan3LessonAdvisorReport,
) -> CurrentAdviceView:
    """Format a settled Plan3 lesson report without exposing a click."""

    if not isinstance(report, Plan3LessonAdvisorReport):
        raise TypeError("report must be Plan3LessonAdvisorReport")
    if report.available:
        assert report.first_action is not None
        assert report.current_state is not None
        assert report.terminal is not None
        current = report.current_state
        terminal = report.terminal
        result = (
            "PERFECT"
            if terminal.get("perfect")
            else "CLEAR"
            if terminal.get("clear")
            else "未達標"
        )
        prediction = (
            f"預測課程終局：{terminal.get('score', '—')} 分｜"
            f"體力 {terminal.get('stamina', '—')}｜{result}"
        )
        reasons = [
            (
                f"目前第 {current.get('round', '—')} 回合，"
                f"分數 {current.get('score', '—')}，"
                f"體力 {current.get('stamina', '—')}/"
                f"{current.get('max_stamina', '—')}。"
            ),
            "已依目前手牌、完整牌區、飲料與剩餘回合模擬到課程終局。",
        ]
        candidates = report.search.get("candidate_count")
        if (
            isinstance(candidates, int)
            and not isinstance(candidates, bool)
            and candidates > 0
        ):
            reasons.append(f"本次比較了 {candidates} 條完整課程路線。")
        reasons.append("這是唯讀建議，不會建立或送出點擊動作。")
        return CurrentAdviceView(
            scope=SCOPE_LESSON,
            available=True,
            status_text="課程｜可提供建議（ready）",
            action_text=(
                "第一動作：" + _plan3_first_action_text(report.first_action)
            ),
            prediction_text=prediction,
            reason_text="\n".join(reasons),
        )

    messages: list[str] = []
    for issue in report.issues:
        message = _LESSON_ISSUE_MESSAGES.get(issue.code)
        if message is None and issue.code.startswith("search-diagnostic:"):
            message = "部分卡牌或效果仍無法完整模擬，暫不提供課程動作。"
        if message is None:
            message = "目前課程資料不足，暫不提供動作。"
        if message not in messages:
            messages.append(message)
    if not messages:
        messages.append("目前沒有可用的 Plan3 課程建議。")
    view = _unavailable_view("\n".join(messages), scope=SCOPE_LESSON)
    retry = any(issue.code == "local-save-unavailable" for issue in report.issues)
    return replace(view, retry=retry)


def advise_current_advice_gui_file(
    exam_save_path: str | Path,
    *,
    produce_id: str,
    live_inputs: CurrentAdviceLiveInputs = CurrentAdviceLiveInputs(),
) -> CurrentAdviceView:
    """Route one refresh among outer, lesson, and Initial/NIA audition advice."""

    if not isinstance(produce_id, str) or not produce_id:
        raise TypeError("produce_id must be a non-empty string")
    if not isinstance(live_inputs, CurrentAdviceLiveInputs):
        raise TypeError("live_inputs must be CurrentAdviceLiveInputs")
    exam_path = Path(exam_save_path)
    try:
        snapshot = read_produce_outer_local_save(exam_path.parent)
    except (OSError, TypeError, ValueError) as error:
        return _unavailable_view(
            "outer local save 正在更新，暫時無法判斷目前階段："
            f"{type(error).__name__}。",
            retry=True,
        )

    mode = plan3_advisor_mode(snapshot)
    stage = classify_current_outer_stage(snapshot)
    if mode is None:
        return _unavailable_view("目前 outer mode 不是已支援的 Initial／NIA。")
    if stage == STAGE_INACTIVE:
        return _unavailable_view("目前沒有進行中的培育。")

    # The serialized audition stage is authoritative. Cached outer choices
    # never overwrite the established Initial/NIA audition router.
    if stage == STAGE_AUDITION:
        report = advise_plan3_audition_gui_file(exam_path)
        view = format_audition_advice_view(
            format_plan3_audition_advisor_report(report)
        )
        retry = bool(
            not report.available
            and any(
                issue.code == "local-save-unavailable"
                for issue in report.issues
            )
        )
        return replace(view, retry=retry)

    if stage == STAGE_LESSON:
        return format_plan3_lesson_advisor_report(
            advise_plan3_lesson_file(exam_path)
        )

    stage_messages = {
        STAGE_BUSY: "目前已進入課程、休息或其他外層步驟；等待下一個可選畫面。",
        STAGE_WAITING: "outer stage 尚未形成可安全決策的完整選項。",
    }
    if stage != STAGE_OUTER:
        return _unavailable_view(
            stage_messages.get(stage, "目前資料不足，無法判斷可選行動。"),
            scope=SCOPE_OUTER,
        )

    has_overview = (
        live_inputs.active_kind == "overview"
        and live_inputs.overview_output is not None
    )
    has_training = (
        live_inputs.active_kind == "training"
        and live_inputs.training_output is not None
    )
    if not (has_overview or has_training):
        return _unavailable_view(
            "等待既有 overview／training reader 提供完整可見選項。",
            scope=SCOPE_OUTER,
        )

    if mode == "initial":
        if produce_id != "produce-001":
            return _unavailable_view(
                "Initial Regular outer save 與 active run 的 produce_id 不一致；"
                "暫不提供外層行動。",
                scope=SCOPE_OUTER,
            )
        try:
            advice = advise_initial_regular_outer(
                snapshot,
                overview_output=live_inputs.overview_output,
                training_output=live_inputs.training_output,
            )
        except (TypeError, ValueError) as error:
            return _unavailable_view(
                "overview／training 資料不足或仍在動畫中："
                f"{type(error).__name__}。",
                scope=SCOPE_OUTER,
            )
        return format_initial_regular_outer_advice(advice)

    if mode == "nia":
        try:
            advice = advise_nia_outer(
                snapshot,
                overview_output=live_inputs.overview_output,
                training_output=live_inputs.training_output,
                produce_id=produce_id,
            )
        except (TypeError, ValueError) as error:
            return _unavailable_view(
                "N.I.A. overview／training 或 route 證據不完整："
                f"{type(error).__name__}。",
                scope=SCOPE_OUTER,
            )
        return format_nia_outer_advice(advice)

    return _unavailable_view(
        "目前 outer mode 不是已支援的 Initial／N.I.A.。",
        scope=SCOPE_OUTER,
    )


__all__ = [
    "CurrentAdviceLiveInputs",
    "CurrentAdviceLocalSaveSignature",
    "CurrentAdviceRefreshSignature",
    "CurrentAdviceRefreshTracker",
    "CurrentAdviceView",
    "SCOPE_AUDITION",
    "SCOPE_LESSON",
    "SCOPE_OUTER",
    "SCOPE_UNAVAILABLE",
    "STAGE_AUDITION",
    "STAGE_BUSY",
    "STAGE_INACTIVE",
    "STAGE_LESSON",
    "STAGE_OUTER",
    "STAGE_WAITING",
    "advise_current_advice_gui_file",
    "classify_current_outer_stage",
    "format_audition_advice_view",
    "format_initial_regular_outer_advice",
    "format_nia_outer_advice",
    "format_plan3_lesson_advisor_report",
    "observe_current_advice_live_update",
    "read_current_advice_local_save_signature",
]
