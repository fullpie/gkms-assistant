"""Pure view models for cultivation, account cards and model records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final


RECOMMENDATION_SOURCES: Final = (
    "rules",
    "behavior_cloning",
    "offline_rl",
    "leaderboard_prior",
    "mixed",
)
RECOMMENDATION_SOURCE_LABELS: Final = {
    "rules": "規則求解器",
    "behavior_cloning": "Behavior Cloning",
    "offline_rl": "Offline RL",
    "leaderboard_prior": "排行榜 imitation prior",
    "mixed": "混合策略",
}
MODE_LABELS: Final = {
    "produce-001": "初 Regular",
    "produce-002": "初 Pro",
    "produce-003": "初 Master",
    "produce-004": "N.I.A. Pro",
    "produce-005": "N.I.A. Master",
}


def native_cultivation_mode_support(produce_id: str) -> tuple[bool, str]:
    """Show the same verified rule coverage enforced before native start."""
    from .runtime_outer_policy import RANKING_PRODUCE_IDS
    available = "、".join(MODE_LABELS.get(value, value) for value in RANKING_PRODUCE_IDS)
    if produce_id in RANKING_PRODUCE_IDS:
        return True, f"目前可試跑：{available}。其他模式規則尚未就緒。"
    return False, f"{MODE_LABELS.get(produce_id, produce_id)} 的自動培育規則尚未就緒；目前可試跑 {available}。"


def cultivation_action_label(action: str, target: object = None) -> str:
    if action == "schedule.choose":
        return {"19": "Vo 自主課程", "20": "Vo SP 自主課程", "21": "Da 自主課程", "22": "Da SP 自主課程",
                "23": "Vi 自主課程", "24": "Vi SP 自主課程", "14": "休息", "25": "營業"}.get(str(target), "選擇培育行程")
    if action == "ui.navigation":
        return {"schedule.refresh": "休息", "produce.start": "開始本場培育", "home.produce": "進入培育",
                "produce.support_continue": "前往回憶編成", "produce.memory_continue": "前往編成確認",
                "lesson.result_continue": "確認自主課程結果", "audition.result_continue": "確認演出結果",
                "produce.result_finish": "完成本場結算"}.get(str(target), "前往下一頁")
    return {"effect.advance": "繼續處理獎勵與效果", "event.advance": "繼續培育事件",
            "effect.confirm_card_change": "確認卡片強化與變更",
            "result.confirm_create_memory": "確認生成回憶", "result.confirm_selection": "確認回憶照片",
            "result.reveal_photo": "查看回憶照片", "result.select_photo": "選擇回憶照片",
            "result.start_memory_animation": "生成本場回憶", "result.continue_memory": "確認本場回憶",
            "result.continue_selection_memory": "確認繼承回憶", "result.continue_rewards": "確認培育獎勵",
            "result.continue_achievements": "確認培育成就",
            "event.skip_story": "略過本段劇情", "event.choose": "選擇事件選項",
            "schedule.choose": "選擇培育行程", "business.choose": "選擇營業", "business.start": "開始營業",
            "audition.choose": "選擇演出難度", "audition.enter": "確認開始演出",
            "reward.choose": "選擇獎勵", "card.choose": "選擇卡片",
            "card.confirm": "確認卡片選擇", "card.customize": "客製化卡片",
            "produce.resume": "繼續本場培育", "produce.confirm_settings": "確認培育設定",
            "produce.prepare": "準備本場培育", "loadout.apply": "套用目前頁面的編成",
            "dll-exam": "完成本次演出", "dll-loadout": "套用本場編成"}.get(action, action)
STAGE_LABELS: Final = {
    "ProduceStepType_AuditionMid1": "Mid1",
    "ProduceStepType_AuditionMid2": "Mid2",
    "ProduceStepType_AuditionFinal": "Final",
}


def cultivation_page_label(page: object, native_screen_type: object = None) -> str:
    detailed = {"effect_resolution": "處理本次獎勵與效果", "audition_select": "選擇演出難度",
                "card_reward": "選擇獎勵卡片", "card_choice": "調整牌庫", "card_customize": "卡片客製化",
                "card_effect_result": "確認卡片變更", "produce_result": "本場培育結算"}
    if str(page) in detailed:
        return detailed[str(page)]
    screen = str(native_screen_type or "").rsplit(".", 1)[-1]
    screens = {
        "HomeTopScreenPresenter": "遊戲首頁", "ProduceTopScreenPresenter": "選擇培育模式",
        "ProduceIdolSelectScreenPresenter": "選擇偶像", "ProduceSupportCardSelectScreenPresenter": "準備支援卡",
        "ProduceMemorySelectScreenPresenter": "準備回憶", "ProduceStartScreenPresenter": "確認編成",
        "ScheduleScreenPresenter": "培育總覽", "ScheduleEventScreenPresenter": "培育事件",
        "ScheduleBusinessScreenPresenter": "營業", "ExamScreenPresenter": "演出", "ExamResultScreenPresenter": "演出結果",
        "ProduceAuditionSelectScreenPresenter": "選擇演出難度",
        "ScheduleSelfLessonScreenPresenter": "自主課程結果",
        "AuditionBattleStartScreenPresenter": "準備演出",
        "AuditionBattleResultScreenPresenter": "演出結果",
        "ProduceResultLastNiaScreenPresenter": "本場培育結果",
        "TitlePresenter": "遊戲登入畫面",
    }
    if screen in screens:
        return screens[screen]
    if str(page) in screens:
        return screens[str(page)]
    return {"navigation": "準備編成", "layer": "確認選項", "schedule": "培育總覽",
            "event": "培育事件", "business": "營業", "overview": "培育總覽", "training": "課程選擇",
            "exam": "演出", "result": "結果確認", "reward": "卡牌獎勵", "nia-outer-subpage": "培育事件",
            "completed": "培育完成", "global-home": "遊戲首頁", "unknown": "等待遊戲狀態",
            "unsupported": "等待可用頁面"}.get(str(page), "等待遊戲狀態")


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


@dataclass(frozen=True, slots=True)
class RecommendationRow:
    source: str
    source_label: str
    action_label: str
    score: float | None = None
    confidence: float | None = None
    reason: str = ""
    status: str = "available"
    legal: bool | None = None
    executable: bool = False
    agreement: bool | None = None
    card_id: str | None = None
    guid: str | None = None
    hand_slot: int | None = None

    def __post_init__(self) -> None:
        if self.source not in RECOMMENDATION_SOURCE_LABELS:
            raise ValueError(f"unknown recommendation source: {self.source}")
        if self.source_label != RECOMMENDATION_SOURCE_LABELS[self.source]:
            raise ValueError("recommendation source label mismatch")
        if not self.action_label:
            raise ValueError("recommendation action label is empty")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("recommendation confidence must be 0..1")

    @property
    def score_text(self) -> str:
        return "—" if self.score is None else f"{self.score:.2f}"

    @property
    def confidence_text(self) -> str:
        return "—" if self.confidence is None else f"{self.confidence * 100:.1f}%"


def recommendation_rows(
    values: Mapping[str, Sequence[Mapping[str, Any]] | Mapping[str, Any] | None],
) -> tuple[RecommendationRow, ...]:
    """Normalize each source independently; unavailable sources stay visible."""

    rows: list[RecommendationRow] = []
    for source in RECOMMENDATION_SOURCES:
        raw = values.get(source)
        if raw is None:
            candidates: Sequence[Mapping[str, Any]] = ()
        elif isinstance(raw, Mapping):
            candidates = (raw,)
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            candidates = tuple(value for value in raw if isinstance(value, Mapping))
        else:
            raise TypeError(f"{source} recommendation input is invalid")
        if not candidates:
            rows.append(
                RecommendationRow(
                    source=source,
                    source_label=RECOMMENDATION_SOURCE_LABELS[source],
                    action_label="尚無可用建議",
                    reason="目前資料或模型尚未提供這個來源的結果。",
                    status="unavailable",
                )
            )
            continue
        source_rows: list[RecommendationRow] = []
        for candidate in candidates:
            slot = candidate.get("hand_slot", candidate.get("slot_index"))
            source_rows.append(
                RecommendationRow(
                    source=source,
                    source_label=RECOMMENDATION_SOURCE_LABELS[source],
                    action_label=str(candidate.get("action_label") or "未命名動作"),
                    score=_optional_number(candidate.get("score")),
                    confidence=_optional_number(candidate.get("confidence")),
                    reason=str(candidate.get("reason") or ""),
                    status=str(candidate.get("status") or "available"),
                    legal=(
                        candidate.get("legal")
                        if type(candidate.get("legal")) is bool
                        else None
                    ),
                    executable=candidate.get("executable") is True,
                    agreement=(
                        candidate.get("agreement")
                        if type(candidate.get("agreement")) is bool
                        else None
                    ),
                    card_id=_optional_text(candidate.get("card_id")),
                    guid=_optional_text(candidate.get("guid")),
                    hand_slot=(
                        int(slot)
                        if isinstance(slot, int) and not isinstance(slot, bool)
                        else None
                    ),
                )
            )
        source_rows.sort(
            key=lambda value: (
                -(value.score if value.score is not None else float("-inf")),
                value.action_label,
            )
        )
        rows.extend(source_rows)
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class DeckRow:
    card_id: str
    upgrade: int
    count: int


def deck_rows(deck: Mapping[str, int] | None) -> tuple[DeckRow, ...]:
    result: list[DeckRow] = []
    for key, count in sorted((deck or {}).items()):
        card_id, separator, upgrade_text = key.rpartition("@")
        if not separator:
            card_id, upgrade_text = key, "0"
        try:
            upgrade = int(upgrade_text)
        except ValueError:
            upgrade = 0
        result.append(DeckRow(card_id, upgrade, int(count)))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CultivationOverview:
    run_id: str | None
    idol_card_id: str | None
    idol_name: str
    mode: str
    stage: str
    week: int | None
    stamina: int | None
    max_stamina: int | None
    produce_points: int | None
    vocal: int | None
    dance: int | None
    visual: int | None
    deck: tuple[DeckRow, ...]
    inventory: tuple[tuple[str, int], ...]
    history: tuple[tuple[str, str], ...]


def cultivation_overview(
    *,
    identity: object | None,
    shadow: object | None,
    profile: object | None,
    exam_session: object | None,
) -> CultivationOverview:
    produce_id = getattr(identity, "produce_id", None)
    logic_state = getattr(exam_session, "logic_state", None)
    step_type = getattr(exam_session, "step_type", None)
    observations = tuple(getattr(shadow, "observations", ()) or ())
    history = tuple(
        (
            str(getattr(value, "kind", "observation")),
            str(getattr(value, "metadata", {}).get("name") or getattr(value, "evidence_path", "")),
        )
        for value in observations[-12:]
    )
    return CultivationOverview(
        run_id=_optional_text(getattr(identity, "run_id", None)),
        idol_card_id=_optional_text(getattr(identity, "idol_card_id", None)),
        idol_name=str(getattr(profile, "name", None) or "尚未選擇"),
        mode=MODE_LABELS.get(str(produce_id), str(produce_id or "尚無模式")),
        stage=(
            STAGE_LABELS.get(str(step_type), str(step_type))
            if step_type
            else "尚無演出階段"
        ),
        week=getattr(shadow, "route_week", None),
        stamina=(
            getattr(logic_state, "stamina", None)
            if logic_state is not None
            else getattr(shadow, "stamina", None)
        ),
        max_stamina=getattr(shadow, "max_stamina", None),
        produce_points=getattr(shadow, "produce_points", None),
        vocal=getattr(shadow, "vocal", None),
        dance=getattr(shadow, "dance", None),
        visual=getattr(shadow, "visual", None),
        deck=deck_rows(getattr(shadow, "deck", None)),
        inventory=tuple(sorted((getattr(shadow, "inventory", None) or {}).items())),
        history=history,
    )


@dataclass(frozen=True, slots=True)
class BatchProgressView:
    target_cycles: int
    completed_cycles: int
    success_count: int
    failure_count: int
    intervention_count: int
    running: bool = False

    def __post_init__(self) -> None:
        for name in (
            "target_cycles",
            "completed_cycles",
            "success_count",
            "failure_count",
            "intervention_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.target_cycles < 1:
            raise ValueError("target_cycles must be at least one")

    @property
    def progress_text(self) -> str:
        return f"{self.completed_cycles} / {self.target_cycles}"


def observed_grade_text(produce_id: str, state: Mapping[str, object]) -> str:
    """Convert a single observed state; never use model action probabilities.

    Initial requires explicit pre-final attributes and observed placement.
    N.I.A. can show the rating equivalent of the current attributes and votes.
    Missing values remain missing instead of becoming zero or a forecast.
    """
    if state.get("in_progress") is False:
        return "目前評等：開始培育後顯示"
    fields = ("vocal", "dance", "visual")
    if any(type(state.get(key)) is not int or state[key] < 0 for key in fields):
        return "目前評等：等待完整能力資料"
    try:
        if produce_id in {"produce-004", "produce-005"}:
            if type(state.get("vote_count")) is not int:
                return "目前評等：等待票數資料"
            from .nia_grade_estimator import estimate_nia_grade
            from .produce_grade_targets import load_produce_grade_targets

            targets = load_produce_grade_targets(produce_id)
            highest = next(index for index, row in enumerate(targets.thresholds) if row.grade == targets.maximum_grade)
            thresholds = targets.thresholds[:highest + 1]
            estimate = estimate_nia_grade(
                produce_id=produce_id, **{key: state[key] for key in fields},
                vote_count=state["vote_count"], target_grade=thresholds[0].grade,
            )
            next_grade = next((row for row in thresholds if row.rating_points > estimate.total_rating), None)
            achieved = next((row.grade for row in reversed(thresholds) if row.rating_points <= estimate.total_rating), thresholds[0].grade)
            remaining = (f"距 {next_grade.grade} 尚需 {next_grade.rating_points - estimate.total_rating:,} 點"
                         if next_grade is not None else "已達本模式最高已知評等")
            return (
                f"目前換算 {achieved} · {estimate.total_rating:,} 評價點\n"
                f"{remaining}\n"
                "依已讀能力與票數／社群公式・Master 門檻"
            )
        if produce_id in {"produce-001", "produce-002", "produce-003"}:
            if (
                state.get("parameters_are_pre_final") is not True
                or type(state.get("final_exam_score")) is not int
                or type(state.get("final_placement")) is not int
            ):
                return "目前評等：等待最終演出與結算名次"
            from .initial_grade_estimator import estimate_initial_grade

            estimate = estimate_initial_grade(
                produce_id, **{key: state[key] for key in fields},
                final_exam_score=state["final_exam_score"],
                final_placement=state["final_placement"], target_grade="S",
            )
            return (
                f"目前換算 {estimate.achieved_grade} · {estimate.total_rating_points:,} 評價點\n"
                "依已讀最終演出資料／社群公式・Master 門檻"
            )
    except (KeyError, OSError, TypeError, ValueError, StopIteration):
        return "目前評等：這份資料尚無可用換算"
    return "目前評等：此模式尚無換算"


__all__ = [
    "BatchProgressView",
    "CultivationOverview",
    "DeckRow",
    "MODE_LABELS",
    "native_cultivation_mode_support",
    "cultivation_action_label",
    "RECOMMENDATION_SOURCE_LABELS",
    "RECOMMENDATION_SOURCES",
    "RecommendationRow",
    "STAGE_LABELS",
    "cultivation_overview",
    "deck_rows",
    "recommendation_rows",
    "observed_grade_text",
    "cultivation_page_label",
]
