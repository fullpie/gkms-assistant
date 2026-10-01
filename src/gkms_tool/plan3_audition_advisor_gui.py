"""Non-technical GUI presentation and LocalSave refresh helpers.

This module deliberately contains no Tk widgets and no game-control code.  It
keeps the automatic read-only refresh policy independently testable while the
desktop GUI remains responsible only for scheduling work and showing text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .audition_local_save_state import EXAM_SAVE_DATA_SOURCE_TYPE
from .local_save_decoder import obfuscated_name
from .nia_audition_advisor import advise_nia_plan3_audition_files
from .plan3_audition_advisor import (
    STATUS_UNAVAILABLE,
    Plan3AuditionAdvisorIssue,
    Plan3AuditionAdvisorReport,
    advise_plan3_audition_file,
)
from .produce_outer_local_save import (
    DEFAULT_PC_GAME_ROOT,
    PRODUCE_LIFECYCLE_FILENAME,
    PRODUCE_PLAY_LOG_FILENAME,
    ProduceOuterLocalSaveSnapshot,
    decode_produce_play_log_file,
    discover_produce_save_directories,
    read_produce_outer_local_save,
)


EXAM_LOCAL_SAVE_FILENAME = obfuscated_name(EXAM_SAVE_DATA_SOURCE_TYPE)


@dataclass(frozen=True, slots=True)
class Plan3LocalSaveSignature:
    """Cheap change marker for the read-only advisor source set.

    ExamSaveData owns the turn state.  The sibling outer play log and
    lifecycle save own the Initial/N.I.A. identity, so changes to any of the
    three inputs must schedule a fresh calculation.  No content hashing is
    needed for this UI refresh marker.
    """

    path: Path
    size: int
    modified_ns: int
    outer_size: int | None = None
    outer_modified_ns: int | None = None
    lifecycle_size: int | None = None
    lifecycle_modified_ns: int | None = None


@dataclass(slots=True)
class Plan3AdvisorRefreshTracker:
    """Allow one calculation per observed file update.

    If the file changes while a calculation is running, the new signature is
    claimed by the next poll.  A transient read error can explicitly request a
    retry without treating ordinary ``unavailable`` advice as an error loop.
    """

    last_submitted: Plan3LocalSaveSignature | None = None
    busy: bool = False

    def claim(self, signature: Plan3LocalSaveSignature) -> bool:
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
class Plan3AuditionAdvisorView:
    available: bool
    status_text: str
    action_text: str
    terminal_text: str
    reason_text: str


def discover_plan3_exam_local_save_candidates(
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
) -> tuple[Path, ...]:
    """Return account save locations that can contain ExamSaveData."""

    root = Path(game_root)
    directories = discover_produce_save_directories(root)
    candidates = {directory / EXAM_LOCAL_SAVE_FILENAME for directory in directories}
    # A direct fallback also works while the outer Produce log is unavailable.
    if root.is_dir():
        candidates.update(root.glob(f"*/*/{EXAM_LOCAL_SAVE_FILENAME}"))
    return tuple(sorted(candidates, key=lambda value: str(value).casefold()))


def select_plan3_exam_local_save_path(
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
) -> Path | None:
    """Select the ExamSave owned by the unique active Produce directory.

    An account root may retain older ExamSaveData files.  Modification time is
    not a run identity: when one Produce lifecycle is explicitly in progress,
    its sibling path owns the exam even if an inactive account has a newer
    file.  The legacy newest/sole fallback remains only when no lifecycle can
    identify an active run.
    """

    directories = discover_produce_save_directories(game_root)
    active_directories: list[Path] = []
    for directory in directories:
        try:
            snapshot = read_produce_outer_local_save(directory)
        except (OSError, TypeError, ValueError):
            continue
        lifecycle = snapshot.lifecycle
        if lifecycle is not None and lifecycle.is_in_progress:
            active_directories.append(directory)
    if len(active_directories) == 1:
        return active_directories[0] / EXAM_LOCAL_SAVE_FILENAME
    if len(active_directories) > 1:
        return None

    candidates = discover_plan3_exam_local_save_candidates(game_root)
    existing: list[tuple[int, Path]] = []
    for path in candidates:
        try:
            existing.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    if existing:
        return max(existing, key=lambda value: (value[0], str(value[1]).casefold()))[1]
    if len(candidates) == 1:
        return candidates[0]
    return None


def read_plan3_local_save_signature(
    path: str | Path,
) -> Plan3LocalSaveSignature | None:
    target = Path(path)
    try:
        metadata = target.stat()
    except OSError:
        return None
    if not target.is_file():
        return None

    def sibling_metadata(filename: str) -> tuple[int | None, int | None]:
        try:
            sibling = (target.parent / filename).stat()
        except OSError:
            return None, None
        return sibling.st_size, sibling.st_mtime_ns

    outer_size, outer_modified_ns = sibling_metadata(PRODUCE_PLAY_LOG_FILENAME)
    lifecycle_size, lifecycle_modified_ns = sibling_metadata(
        PRODUCE_LIFECYCLE_FILENAME
    )
    return Plan3LocalSaveSignature(
        path=target.resolve(),
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        outer_size=outer_size,
        outer_modified_ns=outer_modified_ns,
        lifecycle_size=lifecycle_size,
        lifecycle_modified_ns=lifecycle_modified_ns,
    )


def _unavailable_mode_report(
    code: str,
    detail: str,
    *,
    source: str,
) -> Plan3AuditionAdvisorReport:
    """Return a fail-closed report before either mode advisor is selected."""

    return Plan3AuditionAdvisorReport(
        status=STATUS_UNAVAILABLE,
        issues=(Plan3AuditionAdvisorIssue(code, detail),),
        current_state=None,
        best_decision_steps=(),
        first_action=None,
        terminal=None,
        search={},
        diagnostics=(),
        source=source,
    )


def plan3_advisor_mode(
    outer: ProduceOuterLocalSaveSnapshot,
) -> str | None:
    """Map the serialized Produce type to the supported advisor mode."""

    if outer.produce_type == 1 or outer.produce_type_name == "first_star":
        return "initial"
    if (
        outer.produce_type == 2
        or outer.produce_type_name == "next_idol_audition"
    ):
        return "nia"
    return None


def advise_plan3_audition_gui_file(
    exam_save_path: str | Path,
) -> Plan3AuditionAdvisorReport:
    """Route one GUI refresh to Initial or N.I.A. without game input.

    The outer play log is authoritative for the Produce mode.  N.I.A. also
    consumes its optional sibling lifecycle save.  Ambiguous or unsupported
    mode evidence fails closed instead of guessing Initial.
    """

    exam_path = Path(exam_save_path)
    outer_path = exam_path.parent / PRODUCE_PLAY_LOG_FILENAME
    lifecycle_path = exam_path.parent / PRODUCE_LIFECYCLE_FILENAME
    source = f"outer={outer_path.resolve()};exam={exam_path.resolve()}"
    if not outer_path.is_file():
        return _unavailable_mode_report(
            "advisor-mode-unavailable",
            "the sibling ProducePlayLogSaveData file is missing",
            source=source,
        )
    try:
        outer = decode_produce_play_log_file(outer_path)
    except (OSError, TypeError, ValueError) as error:
        return _unavailable_mode_report(
            "advisor-mode-unavailable",
            f"{type(error).__name__}:{error}",
            source=source,
        )

    mode = plan3_advisor_mode(outer)
    if mode == "initial":
        return advise_plan3_audition_file(exam_path)
    if mode == "nia":
        return advise_nia_plan3_audition_files(
            outer_path,
            exam_path,
            lifecycle_path=lifecycle_path if lifecycle_path.is_file() else None,
        )
    return _unavailable_mode_report(
        "advisor-mode-unsupported",
        f"produce_type={outer.produce_type_name or outer.produce_type!r}",
        source=source,
    )


def _value(mapping: Mapping[str, object], key: str, fallback: object = "—") -> object:
    value = mapping.get(key)
    return fallback if value is None else value


def _first_action_text(action: Mapping[str, object]) -> str:
    kind = action.get("kind")
    if kind == "drink":
        slot = action.get("drink_slot_index")
        slot_text = (
            str(int(slot) + 1)
            if isinstance(slot, int) and not isinstance(slot, bool)
            else "?"
        )
        name = str(action.get("drink_name") or action.get("drink_id") or "未知飲料")
        suffix = "（之後需選擇指定卡牌）" if action.get("selected_card_guid") else ""
        return f"先使用飲料槽 {slot_text}：{name}{suffix}"
    if kind == "card":
        index = action.get("hand_index")
        position = (
            str(int(index) + 1)
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


_ISSUE_MESSAGES = {
    "not-audition": "目前不是演出回合；進入演出後會自動計算。",
    "local-save-unavailable": "正在等待遊戲寫入可讀取的演出資料。",
    "full-horizon-not-ready": "目前資料不足，為避免誤導，暫不提供操作建議。",
    "no-executable-action": "目前沒有可執行的出牌、飲料或跳過動作。",
    "decision-binding-unavailable": "目前無法把計算結果可靠地對應到畫面上的卡牌。",
    "audition-search-error": "目前牌組或效果尚未完整支援，暫不提供建議。",
    "advisor-mode-unavailable": "目前無法確認是初星或 NIA；正在等待遊戲更新模式資料。",
    "advisor-mode-unsupported": "目前模式尚未支援這個演出建議。",
    "nia-source-unavailable": "NIA 的演出或模式資料正在更新，暫時無法讀取。",
    "nia-context-unavailable": "目前無法確認這場 NIA 演出的階段與難度。",
    "nia-context-mismatch": "NIA 的演出資料與靜態規則不一致，為避免誤判，暫不提供建議。",
    "nia-plan-unsupported": "這張偶像卡的流派尚未支援 NIA 演出求解。",
    "nia-exam-save-unavailable": "目前無法完整讀取 NIA 的回合資料。",
}


_NIA_STAGE_NAMES = {
    "ProduceStepType_AuditionMid1": "第一次中間演出",
    "ProduceStepType_AuditionMid2": "第二次中間演出",
    "ProduceStepType_AuditionFinal": "最終演出",
}


def _nia_context_text(current_state: Mapping[str, object] | None) -> str | None:
    if current_state is None:
        return None
    raw_context = current_state.get("nia_context")
    if not isinstance(raw_context, Mapping):
        return None
    stage_value = raw_context.get("step_type")
    stage = _NIA_STAGE_NAMES.get(str(stage_value), str(stage_value or "未知階段"))
    route = str(raw_context.get("difficulty") or "未知路線")
    selected = raw_context.get("audition_number")
    selected_text = "?" if selected is None else str(selected)
    return f"NIA {route}｜階段：{stage}｜選擇難度：{selected_text}"


def format_plan3_audition_advisor_report(
    report: Plan3AuditionAdvisorReport,
) -> Plan3AuditionAdvisorView:
    """Turn the stable advisor contract into concise Traditional Chinese."""

    if report.available:
        assert report.first_action is not None
        assert report.current_state is not None
        assert report.terminal is not None
        terminal = report.terminal
        maximum = _value(terminal, "max_stamina")
        terminal_text = (
            f"預測終局：{_value(terminal, 'score')} 分｜"
            f"體力 {_value(terminal, 'stamina')}/{maximum}｜"
            f"第 {_value(terminal, 'rank')} 名"
        )
        current = report.current_state
        reasons: list[str] = []
        nia_context = _nia_context_text(current)
        if nia_context is not None:
            reasons.append(nia_context)
        reasons.extend([
            (
                f"目前第 {_value(current, 'round')}/{_value(current, 'limit_round')} 回合，"
                f"分數 {_value(current, 'score')}，體力 {_value(current, 'stamina')}。"
            ),
            "已依目前手牌、完整牌庫、飲料與剩餘回合模擬到終局。",
        ])
        candidates = report.search.get("candidate_count")
        if isinstance(candidates, int) and not isinstance(candidates, bool) and candidates > 0:
            reasons.append(f"本次比較了 {candidates} 條可完成路線。")
        if terminal.get("force_end"):
            recovered = terminal.get("force_end_stamina_recovered")
            if isinstance(recovered, int) and not isinstance(recovered, bool) and recovered > 0:
                reasons.append(f"預測達標後會提前結束，並回復 {recovered} 點體力。")
        reasons.append("僅提供建議，不會自動操作；遊戲狀態更新後會重新計算。")
        return Plan3AuditionAdvisorView(
            available=True,
            status_text="可提供建議（ready）",
            action_text=_first_action_text(report.first_action),
            terminal_text=terminal_text,
            reason_text="\n".join(reasons),
        )

    messages: list[str] = []
    nia_context = _nia_context_text(report.current_state)
    if nia_context is not None:
        messages.append(nia_context)
    for issue in report.issues:
        message = _ISSUE_MESSAGES.get(issue.code)
        if message is None and issue.code.startswith("search-diagnostic:"):
            message = "部分卡牌或效果仍無法完整模擬，暫不提供建議。"
        if message is None:
            message = "目前狀態尚無法可靠算到終局，暫不提供建議。"
        if message not in messages:
            messages.append(message)
    if not messages:
        messages.append("目前尚無可用的演出建議。")
    messages.append("僅讀取 LocalSave，不會自動操作遊戲。")
    return Plan3AuditionAdvisorView(
        available=False,
        status_text="暫時無法提供建議（unavailable）",
        action_text="—",
        terminal_text="—",
        reason_text="\n".join(messages),
    )


__all__ = [
    "EXAM_LOCAL_SAVE_FILENAME",
    "Plan3AdvisorRefreshTracker",
    "Plan3AuditionAdvisorView",
    "Plan3LocalSaveSignature",
    "advise_plan3_audition_gui_file",
    "discover_plan3_exam_local_save_candidates",
    "format_plan3_audition_advisor_report",
    "plan3_advisor_mode",
    "read_plan3_local_save_signature",
    "select_plan3_exam_local_save_path",
]
