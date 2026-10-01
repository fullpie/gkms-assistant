from __future__ import annotations

import ctypes
import json
import queue
import threading
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Mapping
import tkinter as tk
from tkinter import messagebox, ttk

from .audition_rules import MID1, next_audition_stage_options
from .catalog import card_choices, get_card
from .engine import recommend_cards, recommend_run_actions
from .event_db import analyze_event_by_adv_asset
from .exam_session import ExamSession, load_exam_session, save_exam_session
from .exact_exam_bc_monitor import (
    ExactExamBCDiagnosticLedgerMonitor,
    ExactExamBCMonitorContext,
)
from .current_advice_gui import (
    CurrentAdviceLiveInputs,
    CurrentAdviceRefreshSignature,
    CurrentAdviceRefreshTracker,
    CurrentAdviceView,
    advise_current_advice_gui_file,
    observe_current_advice_live_update,
    read_current_advice_local_save_signature,
)
from .master_db import IdolProfile, get_mode_initial_deck, list_idol_profiles
from .nia_idol_catalog import NiaIdolCatalogEntry, load_nia_idol_catalog
from .initial_regular_simulator_gui import (
    InitialRegularLiveController,
    InitialRegularLiveRequest,
    InitialRegularSimulatorPhase,
    mount_initial_regular_simulator_tab,
    terminal_bookkeeping_pending,
)
from .gui_dashboard_views import (
    BatchProgressView,
    CultivationOverview,
    RecommendationRow,
    cultivation_overview,
    recommendation_rows,
    observed_grade_text,
    cultivation_page_label,
    native_cultivation_mode_support,
    cultivation_action_label,
)
from .gui_card_library import CardDataUpdatePanel, CardLibraryPanel
from .gui_model_update import ModelUpdatePanel
from .gui_preferences import (
    AUDITION_STRATEGY_LABELS, DEFAULT_GUI_PREFERENCES, GuiPreferences,
    load_gui_preferences, save_gui_preferences,
)
from .models import ExamState, RunState
from .plan3_audition_advisor_gui import select_plan3_exam_local_save_path
from .route_calendar import (
    RoutePosition,
    infer_position_from_final_countdown,
    load_route_calendar,
    milestone_summary,
    supported_produce_ids,
)
from .run_identity import (
    DEFAULT_RUN_ROOT,
    RunIdentity,
    RunPaths,
    create_run,
    load_active_run,
    paths_for,
)
from .run_shadow import load_run_shadow
from .policy_dashboard import discover_policy_dashboard_views
from .policy_bundle import PolicyBundle, activate_policy_bundle
from .cultivation_batch_controller import (
    CultivationBatchController,
    CultivationBatchView,
)
from .lesson_gimmick import LIVE_GIMMICK_GROUP_ID
from .lesson_result import calculate_pursuit_lesson_result
from .lesson_session import load_lesson_session, save_lesson_session
from .lesson_targets import load_lesson_score_targets
from .transition_commit import (
    TransitionCheckpointWrite,
    record_and_commit_verified_transition,
)
from .live_actions import (
    CardPlayExecutionResult,
    SuggestedClick,
    execute_suggested_click,
    execute_verified_card_click,
)
from .live_source import (
    LiveRewardPreviewWorkflowSession,
    LiveView,
    capture_once,
    checkpoint_live_pursuit_lesson_result,
    checkpoint_confirmed_card_reward,
    controller_status as _maa_controller_status,
    detect_live_cards,
    load_calibration_sample,
    make_live_card_frame_analyzer,
    read_live_exam_with_retry,
    read_live_lesson_with_retry,
    read_live_overview,
    read_live_overview_decision,
    read_live_reward_preview_workflow,
    read_live_shop,
    read_live_training_choice,
    sync_live_deck_panel,
)
from .reward_preview_gui import (
    format_reward_preview_workflow,
    should_reset_reward_preview_workflow,
)
from .transition_replay import TransitionReplayIds


FONT_FAMILY = "Microsoft JhengHei UI"
CONSOLE_PAGE_TITLES = (
    "培育",
    "我的卡片與編成",
    "模型與紀錄",
)
LIVE_IDOL_CARD_ID = "i_card-ssmk-3-001"
LIVE_RUN_VALUES = {
    "stamina": 30,
    "max_stamina": 30,
    "vocal": 75,
    "dance": 65,
    "visual": 75,
}


def controller_status() -> Mapping[str, object]:
    """Keep the GUI connection badge tied to the selected input backend."""
    from .runtime_command_client import input_backend

    if input_backend() != "dll":
        return _maa_controller_status()
    from .initial_regular_simulator_gui import _default_live_status_reader

    try:
        return _default_live_status_reader()
    except Exception as error:
        return {"input_backend": "dll", "bridge_live": False, "target_pid": 0,
                "reason": f"{type(error).__name__}: {error}"}


def _enable_windows_dpi_awareness() -> None:
    """Use real monitor pixels before Tk creates its first HWND."""

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _responsive_geometry(screen_width: int, screen_height: int) -> tuple[int, int, int, int]:
    if screen_width < 1 or screen_height < 1:
        raise ValueError("screen dimensions must be positive")
    width = max(1180, int(screen_width * 0.52))
    height = max(760, int(screen_height * 0.84))
    width = min(width, screen_width)
    height = min(height, screen_height)
    return (
        width,
        height,
        min(max(0, int(screen_width * 0.01)), max(0, screen_width - width)),
        max(0, (screen_height - height) // 2),
    )


def _profile_choice_maps(
    profiles: list[IdolProfile],
    localized_by_id: Mapping[str, NiaIdolCatalogEntry],
) -> tuple[tuple[str, ...], dict[str, IdolProfile], dict[str, str]]:
    """Build readable, stable selector labels without parsing display text."""

    base_labels: list[str] = []
    for profile in profiles:
        localized = localized_by_id.get(profile.id)
        song_name = (
            profile.name
            if localized is None
            else localized.translated_song_name
        )
        idol_name = (
            profile.character_id
            if localized is None
            else localized.translated_idol_name
        )
        base_labels.append(f"{song_name}｜{idol_name}")
    counts = Counter(base_labels)
    labels: list[str] = []
    by_label: dict[str, IdolProfile] = {}
    by_id: dict[str, str] = {}
    for profile, base_label in zip(profiles, base_labels, strict=True):
        label = (
            base_label
            if counts[base_label] == 1
            else f"{base_label}｜{profile.id}"
        )
        labels.append(label)
        by_label[label] = profile
        by_id[profile.id] = label
    return tuple(labels), by_label, by_id


def _confirmed_nia_available_idol_cards(
    root: Path = DEFAULT_RUN_ROOT,
) -> frozenset[tuple[str, str]]:
    """Return card/mode pairs proven usable by a real N.I.A. run shadow."""

    result: set[tuple[str, str]] = set()
    try:
        shadows = tuple(Path(root).glob("run-*/run_shadow.json"))
    except OSError:
        return frozenset()
    for path in shadows:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        idol_card_id = payload.get("idol_card_id")
        produce_id = payload.get("produce_id")
        route_week = payload.get("route_week")
        if (
            isinstance(idol_card_id, str)
            and idol_card_id
            and produce_id in {"produce-004", "produce-005"}
            and isinstance(route_week, int)
            and not isinstance(route_week, bool)
            and route_week >= 1
        ):
            result.add((idol_card_id, str(produce_id)))
    return frozenset(result)


def _active_live_learned_bundle_path() -> Path | None:
    """Return the explicit live Exam selection, or None when unavailable.

    ``PolicyBundle.load()`` resolves the active selection and verifies the
    referenced Exam artifacts. Backend selection is independent: a missing
    or invalid model never authorizes a DLL-to-Maa fallback, and native
    preflight rejects an unavailable model before cultivation starts.
    """

    try:
        bundle = PolicyBundle.load(component_roles=("exact_exam_policy",),
                                   optional_component_roles=("offline_rl_policy",))
        runtime = bundle.payload.get("runtime")
        exact = bundle.component("exact_exam_policy")
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
        return None
    if not isinstance(runtime, Mapping):
        return None
    live_apply = runtime.get("live_apply_allowed")
    exam_live = (
        live_apply is True
        or isinstance(live_apply, Mapping)
        and live_apply.get("exam") is True
    )
    if not (
        runtime.get("mode") == "learned-policy"
        and runtime.get("default_enabled") is True
        and runtime.get("fallback") == "stop-on-no-learned-decision"
        and exam_live
        and exact.get("enabled") is True
        and exact.get("live_apply_allowed") is True
    ):
        return None
    return bundle.manifest_path


def _policy_runtime_labels(views: tuple[object, ...]) -> tuple[str, str]:
    """Return the selected model and the policy which really owns input."""

    active = next(
        (value for value in views if getattr(value, "active", False)),
        views[0] if views else None,
    )
    if active is None:
        return (
            "目前模型：尚無模型包",
            "正式控制：外層規則／演出 Maa｜Offline RL：未啟用",
        )
    components = tuple(getattr(active, "components", ()) or ())
    by_role = {
        str(getattr(value, "role", "")): value
        for value in components
        if getattr(value, "role", None)
    }
    exact = by_role.get("exact_exam_policy")
    offline_rl = by_role.get("offline_rl_policy")
    has_bc = exact is not None or "outer_policy" in by_role
    learned_runtime = getattr(active, "runtime_mode", None) == "learned-policy"
    exact_live = bool(
        learned_runtime
        and exact is not None
        and getattr(exact, "live_apply_allowed", False)
    )
    rl_live = bool(
        exact_live
        and offline_rl is not None
        and getattr(offline_rl, "live_apply_allowed", False)
    )
    exact_shadow = bool(
        exact is not None and getattr(exact, "shadow_ready", False)
    )
    rl_shadow = bool(
        offline_rl is not None and getattr(offline_rl, "shadow_ready", False)
    )
    bundle_id = str(getattr(active, "bundle_id", "目前模型包"))
    if rl_live:
        model_label = f"目前模型：{bundle_id}｜演出：Offline RL → Behavior Cloning"
        owner_label = (
            "正式控制：外層規則＋排行榜 prior｜"
            "演出 Offline RL → Behavior Cloning｜Maa 僅執行點擊"
        )
    elif exact_live:
        model_label = f"目前模型：{bundle_id}｜演出：Behavior Cloning"
        owner_label = (
            "正式控制：外層規則＋排行榜 prior｜"
            "演出 Behavior Cloning｜Maa 僅執行點擊｜Offline RL：未啟用"
        )
    else:
        bc_text = "Shadow" if exact_shadow else "未提供" if not has_bc else "未接管"
        rl_text = "Shadow" if rl_shadow else "未啟用"
        model_label = f"目前模型：{bundle_id}｜Behavior Cloning：{bc_text}"
        owner_label = (
            "正式控制：外層規則＋排行榜 prior｜演出 Maa completion｜"
            f"Offline RL：{rl_text}"
        )
    return model_label, owner_label


def _policy_runtime_summary(views: tuple[object, ...]) -> str:
    """Compatibility one-line form used by existing tests and diagnostics."""

    model, owner = _policy_runtime_labels(views)
    return f"{model}｜{owner}"


def _monitor_action_label(value: object) -> str:
    if isinstance(value, str) and value:
        return cultivation_action_label(value)
    if not isinstance(value, Mapping):
        return "—"
    for key in ("label", "action_id", "action", "card_id", "drink_id", "id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return cultivation_action_label(candidate)
    attribute = value.get("attribute")
    return str(attribute) if isinstance(attribute, str) and attribute else "—"


def _monitor_recommendation_sources(
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Map authoritative autopilot evidence to the GUI's named sources."""

    sources: dict[str, object] = {
        "rules": None,
        "behavior_cloning": None,
        "offline_rl": None,
        "leaderboard_prior": None,
        "mixed": None,
    }
    recent = snapshot.get("recent_step")
    if not isinstance(recent, Mapping):
        return sources
    outcome = recent.get("outcome")
    if snapshot.get("source") == "dll" and isinstance(outcome, Mapping) and outcome.get("executor") == "dll":
        decision = outcome.get("decision")
        if isinstance(decision, Mapping):
            action = cultivation_action_label(str(recent.get("action", "")), recent.get("target"))
            reason = decision.get("reason") or decision.get("reasons")
            if isinstance(reason, (tuple, list)):
                reason = "；".join(str(value) for value in reason)
            sources["rules"] = {"action_label": action or "已採用遊戲動作",
                "reason": "最近採用的決策：" + str(reason or "依遊戲目前合法選項繼續。"),
                "status": "available", "legal": None, "executable": False}
            return sources
    evidence = (
        outcome.get("decision_evidence")
        if isinstance(outcome, Mapping)
        else None
    )
    if not isinstance(evidence, Mapping):
        return sources

    chosen = evidence.get("chosen")
    source = evidence.get("source")
    source = source if isinstance(source, Mapping) else {}
    legal = source.get("runtime_legal_actions")
    legal_ids = (
        tuple(value for value in legal if isinstance(value, str))
        if isinstance(legal, (list, tuple))
        else ()
    )
    if isinstance(chosen, str) and chosen:
        sources["rules"] = {
            "action_label": chosen,
            "reason": str(source.get("advisor_reason") or "正式培育目前採用的動作。"),
            "status": "available",
            "legal": chosen in legal_ids if legal_ids else None,
            "executable": False,
        }

    shadow = evidence.get("outer_bc_shadow")
    if isinstance(shadow, Mapping) and shadow.get("status") == "scored":
        model_action = shadow.get("model_top_action")
        if isinstance(model_action, str) and model_action:
            sources["behavior_cloning"] = {
                "action_label": model_action,
                "confidence": shadow.get("model_top_probability"),
                "reason": "Behavior Cloning shadow；只比較，不會改變正式動作。",
                "status": "available",
                "legal": model_action in legal_ids if legal_ids else None,
                "executable": False,
            }

    learned_rank = source.get("learned_prior_rank")
    if isinstance(learned_rank, (list, tuple)):
        prior_rows = [
            {
                "action_label": action,
                "reason": (
                    "排行榜 imitation prior 的排序；"
                    f"資料範圍 {source.get('learned_prior_source') or '未標示'}。"
                ),
                "status": "available",
                "legal": action in legal_ids if legal_ids else None,
                "executable": False,
            }
            for action in learned_rank
            if isinstance(action, str) and action
        ]
        if prior_rows:
            sources["leaderboard_prior"] = prior_rows
    return sources


def _exact_exam_bc_monitor_context(
    snapshot: Mapping[str, object],
    *,
    identity: object | None,
    profile: object | None,
) -> ExactExamBCMonitorContext | None:
    """Bind display evidence to one exact live run/flow/stage snapshot."""

    if snapshot.get("page") != "exam" or identity is None or profile is None:
        return None
    run_id = getattr(identity, "run_id", None)
    produce_id = getattr(identity, "produce_id", None)
    idol_card_id = getattr(identity, "idol_card_id", None)
    character_id = getattr(identity, "character_id", None)
    if (
        getattr(profile, "id", None) != idol_card_id
        or getattr(profile, "character_id", None) != character_id
    ):
        return None
    plan_type = getattr(profile, "plan_type", None)
    effect_type = getattr(profile, "exam_effect_type", None)
    state = snapshot.get("state")
    update = snapshot.get("state_update")
    if not isinstance(state, Mapping) or not isinstance(update, Mapping):
        return None
    stage = {16: "Mid1", 17: "Mid2", 18: "Final"}.get(
        state.get("step_type_value")
    )
    timestamp = update.get("timestamp")
    if not (
        all(
            isinstance(value, str) and value
            for value in (run_id, produce_id, plan_type, effect_type, stage)
        )
        and isinstance(timestamp, (int, float))
        and not isinstance(timestamp, bool)
    ):
        return None
    return ExactExamBCMonitorContext(
        run_id=run_id,
        flow=f"{produce_id}|{plan_type}|{effect_type}",
        stage=stage,
        state_timestamp=float(timestamp),
    )


def _monitor_state_summary(snapshot: Mapping[str, object]) -> str:
    state = snapshot.get("state")
    if isinstance(state, Mapping) and state.get("in_progress") is False:
        return "準備編成／等待開始"
    if not isinstance(state, Mapping) or not state:
        return "等待完整遊戲狀態"
    labels = {
        "week": "週次",
        "weeks_remaining": "剩餘週",
        "current_turn": "回合",
        "remain_turn": "剩餘回合",
        "round_number": "回合",
        "turn": "回合",
        "turns_remaining": "剩餘回合",
        "plays_remaining": "可出牌",
        "stamina": "體力",
        "max_stamina": "體力上限",
        "produce_points": "P 點",
        "vote_count": "票數",
        "vocal": "Vo",
        "dance": "Da",
        "visual": "Vi",
        "score": "分數",
        "player_score": "分數",
        "block": "元氣",
        "good_impression": "好印象",
        "motivation": "幹勁",
        "turn_card_play_count": "本回合已出牌",
        "exam_card_play_count": "累計出牌",
        "status_effect_count": "狀態",
        "removed_status_effect_count": "已移除狀態",
    }
    parts = [
        f"{label} {state[key]}"
        for key, label in labels.items()
        if key in state
    ]
    return "｜".join(parts) if parts else "等待目前狀態數值"


def _resume_live_exam_checkpoint(
    saved_exam: ExamSession | None,
    *,
    idol_card_id: str = LIVE_IDOL_CARD_ID,
    produce_id: str = "produce-001",
    run_id: str | None = None,
    character_id: str | None = None,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    """Select the exact stage identity the Live tab should read next."""

    default_identity: dict[str, object] = {
        "produce_id": produce_id,
        "step_type": MID1,
        "stage_number": 1,
    }
    if saved_exam is None or saved_exam.idol_card_id != idol_card_id:
        return None, default_identity
    if run_id is not None:
        if character_id is None or not saved_exam.matches_run(
            run_id=run_id,
            idol_card_id=idol_card_id,
            character_id=character_id,
            produce_id=produce_id,
        ):
            return None, default_identity
    if not saved_exam.stage_completed:
        return asdict(saved_exam.logic_state), {
            "produce_id": saved_exam.produce_id,
            "step_type": saved_exam.step_type,
            "stage_number": saved_exam.stage_number,
        }
    next_stages = next_audition_stage_options(
        idol_card_id,
        produce_id=saved_exam.produce_id,
        current_step_type=saved_exam.step_type,
        current_number=saved_exam.stage_number,
    )
    if len(next_stages) != 1:
        # Final completed means a future read belongs to a new run.  Multiple
        # difficulty variants require an explicit UI choice and are outside
        # this fixed 初・REGULAR prototype.
        return None, default_identity
    next_stage = next_stages[0]
    return asdict(saved_exam.logic_state), {
        "produce_id": next_stage.produce_id,
        "step_type": next_stage.step_type,
        "stage_number": next_stage.number,
    }


def _verified_card_commit_state(
    result: CardPlayExecutionResult,
) -> dict[str, object] | None:
    """Return only screenshot-verified state; never fall back to prediction."""

    if not result.committed:
        return None
    observed = dict(result.verified_state)
    required = {"turns_remaining", "stamina", "score", "block"}
    missing = sorted(required - set(observed))
    if missing:
        raise ValueError(
            "verified card result is missing committed state fields: "
            + ", ".join(missing)
        )
    return observed


def _save_verified_exam_transition(
    request: TransitionCheckpointWrite,
    *,
    previous: ExamSession,
    identity: RunIdentity,
    produce_id: str,
    step_type: str,
    stage_number: int,
    path: Path,
) -> ExamSession:
    """CAS one verified replay into the exact schema-v3 Exam checkpoint."""

    if not isinstance(request, TransitionCheckpointWrite):
        raise TypeError("request must be TransitionCheckpointWrite")
    if not isinstance(previous, ExamSession):
        raise TypeError("previous must be ExamSession")
    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be RunIdentity")
    if previous.preflight_binding is None:
        raise ValueError("previous Exam session has no preflight binding")
    if previous.bootstrap_consumed_context_id is None:
        raise ValueError("previous Exam session has no consumed context")
    if previous.transition_id is None:
        raise ValueError("previous Exam session has no transition ID")
    if not previous.matches_run(
        run_id=identity.run_id,
        idol_card_id=identity.idol_card_id,
        character_id=identity.character_id,
        produce_id=identity.produce_id,
    ):
        raise ValueError("previous Exam session belongs to a different run")
    if (
        previous.produce_id != produce_id
        or previous.step_type != step_type
        or previous.stage_number != stage_number
    ):
        raise ValueError("previous Exam session belongs to a different stage")
    stored = load_exam_session(path)
    if stored != previous:
        raise ValueError("previous Exam session is stale or was tampered")
    return save_exam_session(
        request.verified_state,
        idol_card_id=identity.idol_card_id,
        produce_id=produce_id,
        step_type=step_type,
        stage_number=stage_number,
        run_id=identity.run_id,
        character_id=identity.character_id,
        preflight_binding=previous.preflight_binding,
        bootstrap_consumed_context_id=previous.bootstrap_consumed_context_id,
        transition_id=request.replay_record_id,
        expected_previous_transition_id=previous.transition_id,
        path=path,
    )


def _live_execution_enabled(
    *,
    click_available: bool,
    busy: bool,
    safety_blockers: tuple[str, ...],
    click_count: int = 1,
) -> bool:
    """One testable gate shared by every live suggestion button refresh."""

    # Generic live actions currently verify one semantic target click only.
    # A declared double click needs its own state machine (select -> settle ->
    # confirm); never make the button look executable until that exists.
    supported_click = (
        not isinstance(click_count, bool)
        and isinstance(click_count, int)
        and click_count == 1
    )
    return bool(
        supported_click and click_available and not busy and not safety_blockers
    )


class GkmsApp(tk.Tk):
    def __init__(self, *, preferences_path: Path | None = None) -> None:
        super().__init__()
        self._preferences_path = DEFAULT_GUI_PREFERENCES if preferences_path is None else Path(preferences_path)
        preferences_error = None
        try:
            preferences = load_gui_preferences(self._preferences_path)
        except (OSError, TypeError, ValueError, KeyError) as error:
            preferences = GuiPreferences()
            preferences_error = f"偏好設定無法讀取，已使用預設值：{error}"
        self.title("GKMS 自動培育控制台")
        width, height, left, top = _responsive_geometry(
            self.winfo_screenwidth(), self.winfo_screenheight()
        )
        self.geometry(f"{width}x{height}+{left}+{top}")
        self.minsize(min(1080, width), min(720, height))
        self._configure_console_appearance()

        self.hand_ids: list[str] = [
            "p_card-00-act-0_001",
            "p_card-00-act-0_002",
            "p_card-01-men-0_007",
        ]
        self._exam_recommendations = []
        self._run_recommendations = []
        self._live_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._live_busy = False
        self._live_after_id: str | None = None
        self._plan3_advisor_queue: queue.Queue[
            tuple[str, CurrentAdviceRefreshSignature, object]
        ] = queue.Queue()
        self._plan3_advisor_tracker = CurrentAdviceRefreshTracker()
        self._plan3_advisor_source_path: Path | None = None
        self._plan3_advisor_after_id: str | None = None
        self._plan3_advisor_discovery_countdown = 0
        self._plan3_advisor_waiting_visible = False
        self._current_advice_live_inputs = CurrentAdviceLiveInputs()
        self._current_advice_outer_marker: (
            tuple[int, int, int | None, int | None] | None
        ) = None
        self._live_suggested_action: SuggestedClick | None = None
        self._live_suggestion_kind: str | None = None
        self._live_reward_workflow_session: (
            LiveRewardPreviewWorkflowSession | None
        ) = None
        self.idol_profiles = list_idol_profiles()
        self.console_idol_profiles = [
            profile
            for profile in self.idol_profiles
            if profile.exam_effect_type
            in {
                "ProduceExamEffectType_ExamLessonBuff",
                "ProduceExamEffectType_ExamParameterBuff",
                "ProduceExamEffectType_ExamReview",
                "ProduceExamEffectType_ExamCardPlayAggressive",
                "ProduceExamEffectType_ExamConcentration",
                "ProduceExamEffectType_ExamFullPower",
            }
        ]
        try:
            localized_by_id = {
                entry.idol_card_id: entry
                for entry in load_nia_idol_catalog().entries
            }
        except (OSError, TypeError, ValueError):
            localized_by_id = {}
        (
            self.profile_choice_labels,
            readable_profile_by_label,
            self.profile_label_by_id,
        ) = _profile_choice_maps(self.console_idol_profiles, localized_by_id)
        # Legacy labels remain accepted by internal callers, but are no longer
        # presented to the user as the primary identity.
        self.profile_by_label = {
            profile.label: profile for profile in self.idol_profiles
        }
        self.profile_by_label.update(readable_profile_by_label)
        self._confirmed_nia_available_cards = set(
            _confirmed_nia_available_idol_cards()
        )
        self._profile_availability_overrides: dict[tuple[str, str], bool] = {}
        self._active_run: RunIdentity | None = load_active_run()
        self._run_paths = (
            None if self._active_run is None else paths_for(self._active_run)
        )
        self._live_idol_card_id = (
            preferences.idol_card_id
            if self._active_run is None
            else self._active_run.idol_card_id
        )
        self._live_produce_id = (
            preferences.produce_id
            if self._active_run is None
            else self._active_run.produce_id
        )
        selected_profile = next(
            (
                profile
                for profile in self.console_idol_profiles
                if profile.id == preferences.idol_card_id
            ),
            self.console_idol_profiles[0] if self.console_idol_profiles else None,
        )
        self.profile_selection_var = tk.StringVar(
            value=(
                ""
                if selected_profile is None
                else self.profile_label_by_id.get(
                    selected_profile.id, selected_profile.label
                )
            )
        )
        self.console_mode_var = tk.StringVar(
            value=preferences.produce_id
        )
        self.console_mode_choice_var = tk.StringVar(
            value=(
                "N.I.A. Master"
                if self.console_mode_var.get() == "produce-005"
                else "N.I.A. Pro"
            )
        )
        self.console_source_var = tk.StringVar(value="Offline")
        self.console_stage_var = tk.StringVar(value="—")
        self.console_capture_age_var = tk.StringVar(value="尚無")
        self.home_cycle_target_var = tk.StringVar(value=str(preferences.target_cycles))
        self.audition_strategy_var = tk.StringVar(value=AUDITION_STRATEGY_LABELS[preferences.audition_strategy])
        self.exam_policy_variant_var = tk.StringVar(value=preferences.policy_variant_id)
        self._last_capture_timestamp: float | None = None
        self._last_update_label = "擷取"
        self._last_live_view_log_signature: tuple[object, ...] | None = None
        self._last_monitor_log_signature: tuple[object, ...] | None = None
        self._recommendation_sources: dict[str, object] = {}
        self._monitor_base_recommendation_sources: dict[str, object] = {}
        self._exact_exam_bc_monitor = ExactExamBCDiagnosticLedgerMonitor()
        self._exact_exam_bc_overlay_active = False
        self._batch_progress = BatchProgressView(1, 0, 0, 0, 0)
        self._native_run_requests: dict[str, InitialRegularLiveRequest] = {}
        self._console_live_controller = InitialRegularLiveController(
            InitialRegularLiveRequest(
                idol_card_id=(
                    "" if selected_profile is None else selected_profile.id
                ),
                plan_type=(
                    "ProducePlanType_Plan2"
                    if selected_profile is None
                    else selected_profile.plan_type
                ),
                produce_id=self.console_mode_var.get(),
                stage_number=1,
                audition_strategy=preferences.audition_strategy,
                exam_policy_variant=preferences.policy_variant_id,
            )
        )
        self._console_batch_controller = CultivationBatchController(
            self._console_live_controller,
            prepare_next_run=self._prepare_next_console_batch_run,
        )
        self._console_live_after_id: str | None = None
        saved_lesson = (
            None
            if self._run_paths is None
            else load_lesson_session(self._run_paths.lesson_session)
        )
        identity_issue: str | None = None
        if self._active_run is not None and saved_lesson is not None:
            if not saved_lesson.matches_run(
                run_id=self._active_run.run_id,
                idol_card_id=self._active_run.idol_card_id,
                character_id=self._active_run.character_id,
                produce_id=self._active_run.produce_id,
            ):
                identity_issue = "lesson-session-run-identity-mismatch"
                saved_lesson = None
        self._live_logic_state: dict[str, object] | None = (
            None if saved_lesson is None else asdict(saved_lesson.logic_state)
        )
        self._live_clear_target: int | None = (
            None if saved_lesson is None else saved_lesson.clear_target
        )
        self._live_lesson_type = (
            "ProduceStepLessonType_Unknown"
            if saved_lesson is None
            else saved_lesson.lesson_type
        )
        if self._live_lesson_type == "ProduceStepLessonType_Unknown" and self._run_paths:
            shadow = load_run_shadow(self._run_paths.shadow)
            if shadow is not None:
                lesson_types = {
                    "vocal": "ProduceStepLessonType_LessonVocal",
                    "dance": "ProduceStepLessonType_LessonDance",
                    "visual": "ProduceStepLessonType_LessonVisual",
                }
                for observation in reversed(shadow.observations):
                    if observation.kind != "pursuit_lesson_choice":
                        continue
                    value = observation.metadata.get("lesson_attribute")
                    if isinstance(value, str) and value in lesson_types:
                        self._live_lesson_type = lesson_types[value]
                    break
        saved_exam = (
            None
            if self._run_paths is None
            else load_exam_session(self._run_paths.exam_session)
        )
        self._live_exam_session: ExamSession | None = saved_exam
        self._live_exam_state, self._live_exam_identity = (
            _resume_live_exam_checkpoint(
                saved_exam,
                idol_card_id=self._live_idol_card_id,
                produce_id=self._live_produce_id,
                run_id=(
                    None if self._active_run is None else self._active_run.run_id
                ),
                character_id=(
                    None
                    if self._active_run is None
                    else self._active_run.character_id
                ),
            )
        )
        if (
            self._active_run is not None
            and saved_exam is not None
            and saved_exam.run_id != self._active_run.run_id
        ):
            identity_issue = "exam-session-run-identity-mismatch"
        self._live_suggested_next_state: dict[str, object] | None = None
        self._live_suggestion_context: dict[str, object] | None = None
        initial_safety_blockers = []
        if identity_issue is not None:
            initial_safety_blockers.append(identity_issue)
        if saved_exam is not None:
            initial_safety_blockers.extend(saved_exam.live_auto_click_blockers())
        self._live_safety_blockers = tuple(
            dict.fromkeys(initial_safety_blockers)
        )
        self._live_route_position: RoutePosition | None = None
        self._controller_click_available = False
        self._controller_background_control = False
        self._live_window_hidden_for_execute = False
        self.status_var = tk.StringVar(
            value="正在讀取遊戲連線、培育狀態與模型資料。"
        )

        self._build_header()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self._build_live_tab()
        self._build_run_tab()
        self._build_advanced_tab()
        ttk.Label(self, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=14, pady=(0, 10)
        )
        self._live_after_id = self.after(75, self._drain_live_queue)
        self._plan3_advisor_after_id = self.after(250, self._poll_plan3_advisor)
        self._console_live_after_id = self.after(250, self._poll_console_autopilot)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if preferences_error:
            self.status_var.set(preferences_error)

    def _configure_console_appearance(self) -> None:
        dpi = max(96.0, float(self.winfo_fpixels("1i")))
        scale = min(2.25, max(1.0, dpi / 96.0))
        background, paper = "#f4f6f8", "#ffffff"
        ink, muted, border, accent = "#243244", "#64748b", "#dbe2ea", "#4268ae"
        self.option_add("*Font", (FONT_FAMILY, 11))
        self.option_add("*Text.background", paper)
        self.option_add("*Text.foreground", ink)
        self.option_add("*Listbox.background", paper)
        self.option_add("*Listbox.foreground", ink)
        self.configure(background=background)
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=(FONT_FAMILY, 11))
        style.configure("TFrame", background=background)
        style.configure("TLabel", background=background, foreground=ink)
        style.configure("Hint.TLabel", background=background, foreground=muted, font=(FONT_FAMILY, 10))
        style.configure("Model.TLabel", background=background, foreground=accent, font=(FONT_FAMILY, 11, "bold"))
        style.configure("Availability.TLabel", background=background, foreground=muted, font=(FONT_FAMILY, 10))
        style.configure("StopReason.TLabel", background=background, foreground="#9b3737", font=(FONT_FAMILY, 11))
        for prefix in ("", "Pink.", "Mint.", "Lavender.", "Sky."):
            style.configure(prefix + "TLabelframe", background=background, bordercolor=border, relief="solid")
            style.configure(prefix + "TLabelframe.Label", background=background, foreground=ink, font=(FONT_FAMILY, 11, "bold"))
        style.configure("TNotebook", background=background, borderwidth=0)
        style.configure("TNotebook.Tab", background=background, foreground=muted, padding=(int(16 * scale), int(8 * scale)))
        style.map("TNotebook.Tab", background=[("selected", paper)], foreground=[("selected", accent)])
        style.configure("TButton", background=paper, foreground=ink, bordercolor=border, padding=(int(10 * scale), int(6 * scale)))
        style.map("TButton", background=[("active", "#e7edf6")])
        style.configure("Accent.TButton", background=accent, foreground=paper, bordercolor=accent, font=(FONT_FAMILY, 11, "bold"))
        style.map("Accent.TButton", background=[("active", "#355791"), ("disabled", "#c9d1dc")], foreground=[("disabled", "#64748b")])
        style.configure("Danger.TButton", background=paper, foreground="#a34343", bordercolor=border)
        style.configure("Treeview", background=paper, fieldbackground=paper, foreground=ink, rowheight=int(28 * scale), bordercolor=border)
        style.configure("Treeview.Heading", background="#eaf0f6", foreground=ink, font=(FONT_FAMILY, 10, "bold"))
        style.map("Treeview", background=[("selected", "#dce7f8")], foreground=[("selected", "#203b60")])

    def _build_header(self) -> None:
        frame = ttk.Frame(self, padding=(14, 12))
        frame.pack(fill="x")
        ttk.Label(
            frame,
            text="學園偶像大師 · 培育助手",
            font=("Yu Gothic UI", 18, "bold"),
        ).pack(anchor="w")

    def _build_initial_regular_simulator_tab(self) -> None:
        self.initial_regular_simulator_panel = (
            mount_initial_regular_simulator_tab(self.notebook)
        )

    def _labeled_spinbox(
        self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, upper: int
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Spinbox(parent, from_=0, to=upper, textvariable=variable, width=12).grid(
            row=row, column=1, sticky="ew", padx=(8, 0), pady=4
        )

    def _build_live_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text=CONSOLE_PAGE_TITLES[0])
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=3)
        tab.columnconfigure(2, weight=0)
        tab.rowconfigure(2, weight=1)

        self.live_controller_var = tk.StringVar(value="Controller：檢查中…")
        self.live_capture_var = tk.StringVar(value="最近更新：尚無")
        self.live_route_var = tk.StringVar(value="路線位置：等待培育總覽")
        self.live_state_var = tk.StringVar(value="目前回合狀態：尚無觀測資料")
        self.live_recommendation_var = tk.StringVar(value="推薦：等待資料")
        self.live_confidence_var = tk.StringVar(value="信心：—")
        self.live_grade_var = tk.StringVar(value="目前評等：等待完整能力資料")
        self.live_auto_var = tk.BooleanVar(value=False)
        self.live_execute_var = tk.StringVar(value="執行目前建議（等待辨識）")

        setup = ttk.LabelFrame(
            tab, text="下一場培育", padding=12
        )
        setup.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        for column, weight in enumerate((3, 1, 2, 1, 0)):
            setup.columnconfigure(column, weight=weight)
        ttk.Label(setup, text="培育偶像").grid(row=0, column=0, sticky="w")
        self.live_profile_choice = ttk.Combobox(
            setup,
            values=self.profile_choice_labels,
            textvariable=self.profile_selection_var,
            state="readonly",
            width=34,
        )
        self.live_profile_choice.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.live_profile_choice.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_console_profile_changed()
        )
        ttk.Label(setup, text="模式").grid(row=0, column=1, sticky="w")
        self.live_mode_choice = ttk.Combobox(
            setup,
            values=("N.I.A. Pro", "N.I.A. Master"),
            textvariable=self.console_mode_choice_var,
            state="readonly",
            width=14,
        )
        self.live_mode_choice.grid(row=1, column=1, sticky="ew", padx=(0, 8))
        self.live_mode_choice.bind(
            "<<ComboboxSelected>>", lambda _event: self._on_console_mode_changed()
        )
        self.live_flow_var = tk.StringVar(value="流派：—")
        ttk.Label(setup, text="演出檔位").grid(row=0, column=2, sticky="w")
        self.audition_strategy_choice = ttk.Combobox(
            setup, values=tuple(AUDITION_STRATEGY_LABELS.values()),
            textvariable=self.audition_strategy_var, state="readonly", width=22,
        )
        self.audition_strategy_choice.grid(row=1, column=2, sticky="ew", padx=(0, 8))
        self.audition_strategy_choice.bind("<<ComboboxSelected>>", lambda _event: self._on_audition_strategy_changed())
        ttk.Label(setup, text="連續培育場數").grid(row=0, column=3, sticky="w")
        self.home_cycle_spinbox = ttk.Spinbox(
            setup,
            from_=1,
            to=999,
            textvariable=self.home_cycle_target_var,
            width=7,
            command=self._save_console_preferences,
        )
        self.home_cycle_spinbox.grid(
            row=1, column=3, sticky="ew", padx=(0, 8)
        )
        self.home_cycle_spinbox.bind("<FocusOut>", lambda _event: self._save_console_preferences())
        self.live_model_status_var = tk.StringVar(
            value="目前模型：正在讀取…"
        )
        self.live_formal_policy_var = tk.StringVar(
            value="正式控制：正在讀取…"
        )
        setup_actions = ttk.Frame(setup)
        setup_actions.grid(row=0, column=4, rowspan=2, sticky="e")
        self.live_new_run_button = ttk.Button(
            setup_actions,
            text="建立新場次",
            command=self._start_selected_live_run,
        )
        self.live_start_auto_button = ttk.Button(
            setup_actions,
            text="開始自動培育",
            command=self._start_console_autopilot,
            style="Accent.TButton",
        )
        self.live_start_auto_button.pack(side="left", padx=(6, 0))
        self.live_stop_auto_button = ttk.Button(
            setup_actions,
            text="停止",
            command=self._stop_console_autopilot,
            state="disabled",
            style="Danger.TButton",
        )
        self.live_stop_auto_button.pack(side="left", padx=(6, 0))
        self.profile_availability_var = tk.StringVar(value="卡片：尚未選擇")
        ttk.Label(
            setup,
            textvariable=self.profile_availability_var,
            style="Availability.TLabel",
            wraplength=1000,
        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(8, 0))
        self.console_selection_note_var = tk.StringVar(
            value="上方選擇套用於下一場；下方顯示目前培育狀態。"
        )
        ttk.Label(
            setup,
            textvariable=self.console_selection_note_var,
        ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self.live_mode_support_var = tk.StringVar()
        ttk.Label(setup, textvariable=self.live_mode_support_var, style="Hint.TLabel", wraplength=1100).grid(
            row=4, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self._refresh_native_mode_support()

        status_strip = ttk.Frame(tab, padding=(8, 4))
        status_strip.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        for column in range(5):
            status_strip.columnconfigure(column, weight=1)
        self.live_game_status_var = tk.StringVar(value="遊戲：檢查中")
        self.live_mode_status_var = tk.StringVar(value="模式：—")
        self.live_stage_status_var = tk.StringVar(value="階段：—")
        self.live_source_status_var = tk.StringVar(value="來源：Offline")
        for column, variable in enumerate(
            (
                self.live_game_status_var,
                self.live_mode_status_var,
                self.live_stage_status_var,
                self.live_capture_var,
            )
        ):
            ttk.Label(status_strip, textvariable=variable).grid(
                row=0, column=column, sticky="w", padx=(0, 8)
            )
        self.live_stop_reason_var = tk.StringVar()
        self.live_stop_reason_label = ttk.Label(status_strip, textvariable=self.live_stop_reason_var,
                                                style="StopReason.TLabel", wraplength=1120)
        state_box = ttk.LabelFrame(
            tab, text="當前狀態", padding=12
        )
        state_box.grid(row=2, column=0, sticky="nsew", padx=(0, 5))
        state_box.columnconfigure(0, weight=1)
        state_box.rowconfigure(1, weight=1)
        self.live_state_summary_text = tk.Text(
            state_box, height=8, width=28, wrap="word", state="disabled", borderwidth=0
        )
        ttk.Label(
            state_box,
            textvariable=self.live_route_var,
            style="Hint.TLabel",
            wraplength=420,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.live_state_summary_text.grid(row=1, column=0, sticky="nsew")
        ttk.Separator(state_box).grid(row=2, column=0, sticky="ew", pady=10)
        ttk.Label(state_box, textvariable=self.live_grade_var, wraplength=290).grid(row=3, column=0, sticky="ew")

        self.live_details_notebook = ttk.Notebook(tab)
        self.live_details_notebook.grid(row=2, column=1, columnspan=2, sticky="nsew", padx=(8, 0))

        cards = ttk.Frame(
            self.live_details_notebook,
            padding=10,
        )
        self.live_details_notebook.add(cards, text="目前手牌")
        cards.columnconfigure(0, weight=1)
        cards.rowconfigure(1, weight=1)
        self.live_cards_label = tk.StringVar(value="目前手牌：尚無")
        ttk.Label(cards, textvariable=self.live_cards_label).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.live_card_tree = ttk.Treeview(cards, columns=("card", "upgrade", "origin", "legal"), show="headings", height=8)
        for key, text in zip(("card", "upgrade", "origin", "legal"), ("卡牌 ID", "強化", "資料意義", "可用性")):
            self.live_card_tree.heading(key, text=text)
        self.live_card_tree.column("card", width=135)
        self.live_card_tree.column("upgrade", width=45, anchor="center")
        self.live_card_tree.column("origin", width=115)
        self.live_card_tree.column("legal", width=52, anchor="center")
        self.live_card_tree.grid(row=1, column=0, sticky="nsew")
        self.live_drinks_var = tk.StringVar(
            value="飲料：進入演出後顯示｜合法動作：等待目前頁面"
        )
        ttk.Label(cards, textvariable=self.live_drinks_var, wraplength=500).grid(
            row=2, column=0, sticky="ew", pady=(8, 0)
        )

        recommendation = ttk.Frame(
            self.live_details_notebook,
            padding=10,
        )
        self.live_details_notebook.insert(0, recommendation, text="目前建議")
        self.live_details_notebook.select(recommendation)
        recommendation.columnconfigure(0, weight=1)
        recommendation.rowconfigure(0, weight=1)
        self.live_recommendation_tree = ttk.Treeview(
            recommendation,
            columns=("rank", "action", "source", "score", "confidence", "status"),
            displaycolumns=("action", "source"),
            show="headings",
            height=10,
        )
        for key, text in zip(
            ("rank", "action", "source", "score", "confidence", "status"),
            ("#", "動作", "來源", "策略值", "信心／機率", "狀態"),
        ):
            self.live_recommendation_tree.heading(key, text=text)
        self.live_recommendation_tree.column("rank", width=28, anchor="center")
        self.live_recommendation_tree.column("action", width=120)
        self.live_recommendation_tree.column("source", width=100)
        self.live_recommendation_tree.column("score", width=50, anchor="e")
        self.live_recommendation_tree.column("confidence", width=58, anchor="e")
        self.live_recommendation_tree.column("status", width=58, anchor="center")
        self.live_recommendation_tree.grid(row=0, column=0, sticky="nsew")
        self.live_recommendation_tree.bind(
            "<<TreeviewSelect>>", self._show_recommendation_detail
        )
        self.live_reason_text = tk.Text(
            recommendation, height=4, wrap="word", state="disabled", borderwidth=0
        )
        self.live_reason_text.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self.plan3_advisor_status_var = tk.StringVar(
            value="等待目前資料（unavailable）"
        )
        self.plan3_advisor_action_var = tk.StringVar(value="行動：—")
        self.plan3_advisor_terminal_var = tk.StringVar(value="預測：—")
        self.plan3_advisor_reason_var = tk.StringVar(
            value=(
                "等待既有 Initial／N.I.A. 外層畫面資料或演出 LocalSave；"
                "不會自動操作遊戲。"
            )
        )
        log_box = ttk.LabelFrame(tab, text="最近動作", padding=8)
        log_box.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        log_box.columnconfigure(0, weight=1)
        self.live_log_text = tk.Text(log_box, height=4, wrap="word", state="disabled", borderwidth=0)
        self.live_log_text.grid(row=0, column=0, sticky="nsew")
        progress_row = ttk.Frame(log_box)
        progress_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.batch_target_var = self.home_cycle_target_var
        self.batch_target_spinbox = self.home_cycle_spinbox
        self.batch_start_button = self.live_start_auto_button
        self.batch_stop_button = self.live_stop_auto_button
        self.batch_completed_var = tk.StringVar(value="已完成：0")
        self.batch_success_var = tk.StringVar(value="成功：0")
        self.batch_failure_var = tk.StringVar(value="失敗：0")
        self.batch_intervention_var = tk.StringVar(value="")
        self.batch_progress_var = tk.StringVar(value="0 / 1")
        for variable in (self.batch_completed_var, self.batch_success_var, self.batch_failure_var):
            ttk.Label(progress_row, textvariable=variable, style="Hint.TLabel").pack(side="left", padx=(0, 16))
        self.batch_progress = ttk.Progressbar(progress_row, maximum=100, length=140)
        self.batch_progress.pack(side="right")
        ttk.Label(progress_row, textvariable=self.batch_progress_var, style="Hint.TLabel").pack(side="right", padx=10)

        controls = ttk.Frame(tab)
        # The console is a monitor for the unattended owner.  Keep the legacy
        # button objects as compatibility seams for advanced code/tests, but
        # do not expose a second manual capture/recognition workflow here.
        self.live_sample_button = ttk.Button(
            controls, text="載入校正樣本", command=self._load_live_calibration
        )
        self.live_capture_button = ttk.Button(
            controls, text="擷取畫面", command=self._capture_live_once
        )
        self.live_state_button = ttk.Button(
            controls, text="讀取培育狀態", command=self._read_live_overview
        )
        self.live_detect_button = ttk.Button(
            controls, text="分析課程", command=self._read_live_lesson
        )
        self.live_exam_button = ttk.Button(
            controls, text="分析演出", command=self._read_live_exam
        )
        self.live_training_button = ttk.Button(
            controls, text="訓練選項", command=self._read_live_training_choice
        )
        self.live_reward_button = ttk.Button(
            controls, text="卡牌獎勵", command=self._read_live_reward
        )
        self.live_deck_button = ttk.Button(
            controls, text="同步牌組", command=self._sync_live_deck_panel
        )
        self.live_shop_button = ttk.Button(
            controls, text="讀取商店（實驗）", command=self._read_live_shop
        )
        self.live_execute_button = ttk.Button(
            controls,
            textvariable=self.live_execute_var,
            command=self._execute_live_suggestion,
            state="disabled",
        )
        self._initialize_recommendation_sources()
        self._refresh_selected_profile_detail()
        self._refresh_live_policy_status()
        self._refresh_console_identity()
        self._refresh_cultivation_overview()
        self._run_live_worker("status", controller_status)

    @staticmethod
    def _flow_label(effect_type: str | None) -> str:
        return {
            "ProduceExamEffectType_ExamLessonBuff": "集中",
            "ProduceExamEffectType_ExamParameterBuff": "好調",
            "ProduceExamEffectType_ExamReview": "好印象",
            "ProduceExamEffectType_ExamCardPlayAggressive": "幹勁",
            "ProduceExamEffectType_ExamConcentration": "強氣",
            "ProduceExamEffectType_ExamFullPower": "全力",
        }.get(str(effect_type), "—")

    @staticmethod
    def _mode_label(produce_id: str | None) -> str:
        return {
            "produce-004": "N.I.A. Pro",
            "produce-005": "N.I.A. Master",
        }.get(str(produce_id), str(produce_id or "—"))

    def _on_console_profile_changed(self) -> None:
        if hasattr(self, "profile_choice"):
            self.profile_choice.set(self.profile_selection_var.get())
        self._refresh_selected_profile_detail()
        self._refresh_console_identity()
        if hasattr(self, "run_vars"):
            self._apply_selected_profile()
        self._save_console_preferences()

    def _selected_audition_strategy(self) -> str:
        label = self.audition_strategy_var.get()
        return next(key for key, value in AUDITION_STRATEGY_LABELS.items() if value == label)

    def _save_console_preferences(self) -> bool:
        profile = self._selected_profile()
        if profile is None:
            return False
        try:
            preferences = GuiPreferences(
                idol_card_id=profile.id, produce_id=self.console_mode_var.get(),
                target_cycles=int(self.home_cycle_target_var.get()),
                audition_strategy=self._selected_audition_strategy(),
                policy_variant_id=self.exam_policy_variant_var.get(),
            )
            save_gui_preferences(preferences, self._preferences_path)
        except (OSError, TypeError, ValueError, StopIteration) as error:
            self.status_var.set(f"偏好未儲存：{error}")
            return False
        return True

    def _on_audition_strategy_changed(self) -> None:
        self._save_console_preferences()
        self._sync_console_live_request()

    def _set_exam_policy_variant(self, variant: str) -> None:
        from .gui_preferences import EXAM_POLICY_VARIANTS
        from .gui_exam_models import current_policy_selection_state
        if variant not in EXAM_POLICY_VARIANTS:
            raise ValueError("演出模型設定無效。")
        if (self._console_live_controller.view.phase in {
                InitialRegularSimulatorPhase.RUNNING, InitialRegularSimulatorPhase.CANCELLING}
                or self._console_batch_controller.view.running or self._live_busy
                or getattr(getattr(self, "card_library_panel", None), "game_input_pending", False)):
            raise RuntimeError("請先停止目前工作並等待結果，才能切換下一場的模型。")
        from .runtime_command_client import DEFAULT_BRIDGE_ROOT
        selection = current_policy_selection_state(variant,
            pending_transaction=any(DEFAULT_BRIDGE_ROOT.glob('*pending*.json')))
        if selection['locked']:
            raise RuntimeError(selection['reason'])
        previous = self.exam_policy_variant_var.get()
        self.exam_policy_variant_var.set(variant)
        if not self._save_console_preferences():
            self.exam_policy_variant_var.set(previous)
            raise RuntimeError("模型偏好儲存失敗，選擇未變更。")
        self._sync_console_live_request()

    def _refresh_selected_profile_detail(self) -> None:
        if not hasattr(self, "profile_availability_var"):
            return
        profile = self._selected_profile()
        if profile is None:
            self.profile_availability_var.set("卡片：尚未選擇")
            return
        mode_key = (profile.id, self.console_mode_var.get())
        override = self._profile_availability_overrides.get(mode_key)
        if override is False:
            availability = (
                "目前不可用（可能尚未完成「初」培育，或角色親密度不足）"
            )
        elif override is True or mode_key in self._confirmed_nia_available_cards:
            availability = "過去實機已確認可用"
        else:
            availability = "尚未確認（開始時由遊戲選卡頁確認）"
        self.profile_availability_var.set(f"遊戲選卡資格：{availability}")

    def _refresh_native_mode_support(self) -> bool:
        from .runtime_command_client import input_backend
        try:
            native = input_backend() == "dll"
        except (OSError, ValueError, RuntimeError) as error:
            self.live_mode_support_var.set(f"輸入方式設定不可讀：{error}")
            return False
        if not native:
            self.live_mode_choice.configure(values=("N.I.A. Pro", "N.I.A. Master"))
            self.live_mode_support_var.set("目前使用 MAA 相容培育。")
            self.live_start_auto_button.configure(text="開始自動培育")
            return True
        from .runtime_outer_policy import RANKING_PRODUCE_IDS
        self.live_mode_choice.configure(values=tuple(self._mode_label(value) for value in RANKING_PRODUCE_IDS))
        resume = self._native_resume_identity()
        profile = None if resume is None else next((p for p in self.idol_profiles if p.id == resume.idol_card_id), None)
        supported, note = native_cultivation_mode_support(self.console_mode_var.get() if resume is None else resume.produce_id)
        if resume is not None:
            self.live_start_auto_button.configure(text="接續目前培育")
            if profile is None:
                supported, note = False, "目前培育的偶像資料尚未就緒，無法確認流派。"
            else:
                identity_note = f"本次接續 {profile.name}／{self._mode_label(resume.produce_id)}；上方設定保留給下一場。"
                note = identity_note if supported else identity_note + note
        else:
            self.live_start_auto_button.configure(text="開始自動培育")
        self.live_mode_support_var.set(note)
        return supported

    def _native_resume_identity(self) -> RunIdentity | None:
        """Only a manifest created from actual native progress can bind resume.

        The runner revalidates it against the game. Keeping expected_run_id
        prevents a stale Resume click from silently becoming a fresh start.
        """
        from .runtime_command_client import input_backend
        if input_backend() != "dll":
            return None
        identity = load_active_run()
        if identity is None or identity.evidence.get("source") != "native-produce-progress":
            return None
        if not all(isinstance(identity.evidence.get(key), str) and identity.evidence[key]
                   for key in ("session_generation", "native_revision")):
            return None
        return identity

    def _displayed_live_profile(self):
        if self._active_run is not None:
            return next((p for p in self.idol_profiles if p.id == self._active_run.idol_card_id), None)
        return self.profile_by_label.get(self.profile_selection_var.get())

    def _refresh_live_policy_status(self) -> None:
        if not hasattr(self, "live_model_status_var"):
            return
        try:
            views = discover_policy_dashboard_views()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            views = ()
        model, owner = _policy_runtime_labels(tuple(views))
        from .runtime_command_client import input_backend
        try:
            if input_backend() == "dll":
                owner = owner.replace("Maa 僅執行點擊", "DLL 執行動作")
        except (OSError, ValueError, RuntimeError):
            pass
        self.live_model_status_var.set(model)
        self.live_formal_policy_var.set(owner)

    def _on_console_mode_changed(self) -> None:
        self.console_mode_var.set(
            "produce-005"
            if self.console_mode_choice_var.get() == "N.I.A. Master"
            else "produce-004"
        )
        self._refresh_native_mode_support()
        self._refresh_console_identity()
        profile = self._selected_profile()
        if profile is not None and hasattr(self, "run_deck_var"):
            self._set_mode_deck_summary(profile)
        self._save_console_preferences()

    def _refresh_console_identity(self) -> None:
        profile = self._displayed_live_profile()
        self._refresh_selected_profile_detail()
        self.live_flow_var.set(
            "流派：" + self._flow_label(None if profile is None else profile.exam_effect_type)
        )
        self.live_mode_status_var.set(
            "模式：" + self._mode_label(self.console_mode_var.get() if self._active_run is None else self._active_run.produce_id)
        )
        step_type = self._live_exam_identity.get("step_type")
        stage = {
            "ProduceStepType_AuditionMid1": "Mid1",
            "ProduceStepType_AuditionMid2": "Mid2",
            "ProduceStepType_AuditionFinal": "Final",
        }.get(str(step_type), str(step_type or "—"))
        if self._active_run is None:
            stage = "尚未開始"
        self.console_stage_var.set(stage)
        self.live_stage_status_var.set(
            f"階段：{stage}／{self._flow_label(None if profile is None else profile.exam_effect_type)}"
        )
        self.live_source_status_var.set(
            "來源：" + self.console_source_var.get()
        )
        if hasattr(self, "_console_live_controller"):
            try:
                self._sync_console_live_request()
            except (OSError, ValueError, RuntimeError) as error:
                self.status_var.set(str(error))

    def _sync_console_live_request(self, *, for_next_run: bool = False) -> None:
        profile = self.profile_by_label.get(self.profile_selection_var.get())
        if profile is None:
            return
        resume = None if for_next_run else self._native_resume_identity()
        if resume is not None:
            profile = next((p for p in self.idol_profiles if p.id == resume.idol_card_id), None)
            if profile is None:
                raise ValueError("目前培育的偶像 Master 資料缺失，不能以另一張卡的流派接續。")
            request = self._native_run_requests.get(resume.run_id)
            if request is None:
                current = self._console_live_controller.request
                same = current.idol_card_id == profile.id and current.produce_id == resume.produce_id
                bundle = (current.plan2_policy_bundle_path if same and current.plan2_policy_bundle_path is not None
                          else _active_live_learned_bundle_path())
                request = InitialRegularLiveRequest(idol_card_id=profile.id, plan_type=profile.plan_type,
                    produce_id=resume.produce_id, stage_number=1, expected_run_id=resume.run_id,
                    plan2_policy_bundle_path=bundle,
                    audition_strategy=str(resume.evidence.get("audition_strategy", "highest_available")),
                    exam_policy_variant=resume.evidence.get("exam_policy_variant"))
                self._native_run_requests[resume.run_id] = request
            self._console_live_controller.set_request(request)
            return
        produce_id = self.console_mode_var.get()
        learned_bundle = _active_live_learned_bundle_path()
        self._console_live_controller.set_request(
            InitialRegularLiveRequest(
                idol_card_id=profile.id,
                plan_type=profile.plan_type,
                produce_id=produce_id,
                stage_number=1,
                # The active backend owns resume/new-run identity; selecting
                # another next-run card does not override the live run.
                expected_run_id=None,
                plan2_policy_bundle_path=learned_bundle,
                audition_strategy=self._selected_audition_strategy(),
                exam_policy_variant=self.exam_policy_variant_var.get(),
            )
        )

    def _set_live_analysis_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in (
            self.live_sample_button,
            self.live_capture_button,
            self.live_state_button,
            self.live_training_button,
            self.live_reward_button,
            self.live_deck_button,
            self.live_detect_button,
            self.live_exam_button,
        ):
            button.configure(state=state)
        for button in getattr(self, "advanced_manual_buttons", ()):
            button.configure(state=state)
        if enabled:
            self._refresh_execute_button()
        else:
            self.live_execute_button.configure(state="disabled")

    def _start_console_autopilot(self) -> None:
        if not self._refresh_native_mode_support():
            self.status_var.set(self.live_mode_support_var.get())
            return
        library = getattr(self, "card_library_panel", None)
        if library is not None and library.game_input_pending:
            self.status_var.set("編成工作正在進行，完成後才能開始培育。")
            return
        if self._live_busy:
            self.status_var.set("手動擷取／分析仍在執行，不能同時啟動 autopilot。")
            return
        self._sync_console_live_request()
        try:
            target = int(self.home_cycle_target_var.get())
            if not self._save_console_preferences():
                return
            batch = self._console_batch_controller.start(target)
        except (TypeError, ValueError, RuntimeError) as error:
            messagebox.showerror(
                "自動培育無法開始", str(error), parent=self
            )
            return
        self._apply_batch_view(batch)

    def _stop_console_autopilot(self) -> None:
        batch = self._console_batch_controller.stop()
        self._apply_batch_view(batch)

    def _poll_console_autopilot(self) -> None:
        try:
            batch = self._console_batch_controller.poll()
            self._apply_batch_view(batch)
            self._refresh_capture_age()
        except Exception as error:
            self.status_var.set(
                f"批次狀態更新失敗：{type(error).__name__}: {error}"
            )
        finally:
            self._console_live_after_id = self.after(
                250, self._poll_console_autopilot
            )

    def _refresh_capture_age(self) -> None:
        if self._last_capture_timestamp is None:
            return
        age = max(0.0, time.time() - self._last_capture_timestamp)
        self.console_capture_age_var.set(f"{age:.1f} 秒前")
        self.live_capture_var.set(
            f"最近{self._last_update_label}：" + self.console_capture_age_var.get()
        )

    def _prepare_next_console_batch_run(self) -> None:
        from .runtime_command_client import input_backend
        if input_backend() == "dll":
            # Completion has been observed; the native runner creates the next
            # manifest only after the game actually starts that cultivation.
            self._refresh_cultivation_overview()
            self._sync_console_live_request(for_next_run=True)
        else:
            self._start_selected_live_run()

    def _start_console_batch(self) -> None:
        if not self._refresh_native_mode_support():
            self.status_var.set(self.live_mode_support_var.get())
            return
        library = getattr(self, "card_library_panel", None)
        if library is not None and library.game_input_pending:
            self.status_var.set("編成工作正在進行，完成後才能開始培育。")
            return
        if self._live_busy:
            self.status_var.set("手動擷取／分析仍在執行，不能同時啟動批次。")
            return
        try:
            target = int(self.batch_target_var.get())
        except ValueError:
            messagebox.showerror(
                "循環次數錯誤", "循環次數必須是整數。", parent=self
            )
            return
        self._sync_console_live_request()
        try:
            batch = self._console_batch_controller.start(target)
        except (TypeError, ValueError, RuntimeError) as error:
            messagebox.showerror("循環無法開始", str(error), parent=self)
            return
        self._apply_batch_view(batch)

    def _stop_console_batch(self) -> None:
        self._apply_batch_view(self._console_batch_controller.stop())

    def _apply_batch_view(self, batch: CultivationBatchView) -> None:
        self._batch_progress = BatchProgressView(
            target_cycles=batch.target_cycles,
            completed_cycles=batch.completed_cycles,
            success_count=batch.success_count,
            failure_count=batch.failure_count,
            intervention_count=batch.intervention_count,
            running=batch.running,
        )
        if hasattr(self, "batch_completed_var"):
            self.batch_completed_var.set(f"已完成：{batch.completed_cycles}")
            self.batch_success_var.set(f"成功：{batch.success_count}")
            self.batch_failure_var.set(f"失敗：{batch.failure_count}")
            self.batch_intervention_var.set("介入：尚未接線")
            self.batch_progress_var.set(batch.progress_text)
            self.batch_progress["value"] = (
                100.0 * batch.completed_cycles / max(1, batch.target_cycles)
            )
            self.batch_start_button.configure(
                state=(
                    "disabled"
                    if batch.running or self._live_busy or terminal_bookkeeping_pending(batch.live_view)
                    else "normal"
                )
            )
            self.batch_stop_button.configure(
                state="normal" if batch.running else "disabled"
            )
        self._apply_console_live_view(batch.live_view)
        if batch.message:
            self.status_var.set(
                f"{batch.message}｜"
                f"{getattr(batch.live_view, 'current_page_text', '—')}｜"
                f"{getattr(batch.live_view, 'action_count_text', '')}"
            )

    def _apply_console_live_view(self, view: object) -> None:
        phase = getattr(view, "phase", InitialRegularSimulatorPhase.IDLE)
        bookkeeping_pending = terminal_bookkeeping_pending(view)
        running = phase in {
            InitialRegularSimulatorPhase.RUNNING,
            InitialRegularSimulatorPhase.CANCELLING,
        }
        library = getattr(self, "card_library_panel", None)
        if library is not None:
            library.set_game_busy(running or self._batch_progress.running or self._live_busy)
        owner_busy = running or self._live_busy or self._batch_progress.running or (
            library is not None and library.game_input_pending
        )
        mode_supported = self._refresh_native_mode_support()
        self.live_start_auto_button.configure(
            state="disabled" if owner_busy or not mode_supported or bookkeeping_pending else "normal"
        )
        self.live_stop_auto_button.configure(
            state=(
                "normal"
                if phase == InitialRegularSimulatorPhase.RUNNING or self._batch_progress.running
                else "disabled"
            )
        )
        self.live_profile_choice.configure(
            state="disabled" if owner_busy else "readonly"
        )
        self.live_mode_choice.configure(
            state="disabled" if owner_busy else "readonly"
        )
        self.audition_strategy_choice.configure(state="disabled" if owner_busy else "readonly")
        self.live_new_run_button.configure(
            state="disabled" if owner_busy or bookkeeping_pending else "normal"
        )
        self.home_cycle_spinbox.configure(
            state="disabled" if owner_busy else "normal"
        )
        if hasattr(self, "batch_target_spinbox"):
            self.batch_target_spinbox.configure(
                state="disabled" if owner_busy else "normal"
            )
        if hasattr(self, "profile_choice"):
            self.profile_choice.configure(
                state="disabled" if owner_busy else "readonly"
            )
        self._set_live_analysis_controls_enabled(not owner_busy)
        self.live_game_status_var.set(
            "遊戲：自動培育執行中"
            if phase == InitialRegularSimulatorPhase.RUNNING
            else (
                "遊戲：正在停止"
                if phase == InitialRegularSimulatorPhase.CANCELLING
                else self.live_game_status_var.get()
            )
        )
        current_page = str(getattr(view, "current_page_text", "—"))
        terminal = phase in {InitialRegularSimulatorPhase.COMPLETED, InitialRegularSimulatorPhase.CANCELLED,
                            InitialRegularSimulatorPhase.FAILED, InitialRegularSimulatorPhase.BLOCKED}
        stop_reason = str(getattr(view, "stop_reason_text", ""))
        if terminal and (phase != InitialRegularSimulatorPhase.COMPLETED or bookkeeping_pending):
            if stop_reason in {"", "停止原因：尚未停止"}:
                stop_reason = str(getattr(view, "readiness_text", "培育已停止"))
            self.live_stop_reason_var.set(stop_reason)
            self.live_stop_reason_label.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 0))
        else:
            self.live_stop_reason_var.set("")
            self.live_stop_reason_label.grid_remove()
        self.console_source_var.set("Live" if running else self.console_source_var.get())
        self.live_source_status_var.set("來源：" + self.console_source_var.get())
        self.status_var.set(
            f"{getattr(view, 'readiness_text', '')}｜{current_page}｜"
            f"{getattr(view, 'action_count_text', '')}｜"
            f"{getattr(view, 'stop_reason_text', '')}"
        )
        monitor_snapshot = getattr(view, "monitor_snapshot", None)
        if isinstance(monitor_snapshot, Mapping):
            self._apply_autopilot_monitor_snapshot(monitor_snapshot)
        log_signature = (
            phase,
            current_page,
            getattr(view, "action_count_text", ""),
            getattr(view, "recent_action_text", ""),
            getattr(view, "stop_reason_text", ""),
        )
        if (
            phase != InitialRegularSimulatorPhase.IDLE
            and log_signature != self._last_live_view_log_signature
        ):
            self._last_live_view_log_signature = log_signature
            detail = "｜".join(
                value
                for value in (
                    current_page,
                    str(getattr(view, "action_count_text", "")),
                    str(getattr(view, "recent_action_text", "")),
                    stop_reason if terminal else "",
                )
                if value
            )
            self._append_live_log(detail)
        if phase in {
            InitialRegularSimulatorPhase.COMPLETED,
            InitialRegularSimulatorPhase.CANCELLED,
            InitialRegularSimulatorPhase.FAILED,
            InitialRegularSimulatorPhase.BLOCKED,
        }:
            technical = " ".join(
                str(value)
                for value in tuple(getattr(view, "technical_details", ()) or ())
            )
            terminal_detail = (
                technical
                + " "
                + str(getattr(view, "stop_reason_text", ""))
            )
            if "idol-card-not-selectable" in terminal_detail:
                selected = self._selected_profile()
                if selected is not None:
                    self._profile_availability_overrides[
                        (selected.id, self.console_mode_var.get())
                    ] = False
                    self._refresh_selected_profile_detail()
            self.live_game_status_var.set(
                "遊戲：培育已完成"
                if phase == InitialRegularSimulatorPhase.COMPLETED
                else "遊戲：已停止"
                if phase == InitialRegularSimulatorPhase.CANCELLED
                else "遊戲：自動培育已停止"
            )
            if phase == InitialRegularSimulatorPhase.COMPLETED:
                evidence = getattr(
                    self._console_live_controller,
                    "last_completed_run_evidence",
                    None,
                )
                if isinstance(evidence, Mapping):
                    self._append_live_log(
                        "本場完整資料已封裝。"
                        if evidence.get("evidence_complete") is True
                        else "本場原始資料已保存；部分整場證據仍標示不完整。"
                    )
            self._refresh_cultivation_overview()
            if isinstance(monitor_snapshot, Mapping) and monitor_snapshot.get("source") == "dll":
                # Refreshing the saved run list must not replace the last
                # native state with an older shadow when an action hard-stops.
                self._apply_autopilot_monitor_snapshot(monitor_snapshot)

    def _apply_autopilot_monitor_snapshot(
        self,
        snapshot: Mapping[str, object],
    ) -> None:
        """Render telemetry emitted by the sole unattended owner."""

        capture = snapshot.get("capture")
        if isinstance(capture, Mapping):
            timestamp = capture.get("timestamp")
            if isinstance(timestamp, (int, float)) and not isinstance(
                timestamp, bool
            ):
                self._last_capture_timestamp = float(timestamp)
                self._last_update_label = "擷取"
                captured = datetime.fromtimestamp(float(timestamp)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                detail = "×".join(
                    str(value)
                    for value in (capture.get("width"), capture.get("height"))
                    if isinstance(value, int) and not isinstance(value, bool)
                )
                method = capture.get("capture_method")
                suffix = "｜".join(
                    value
                    for value in (
                        detail,
                        str(method) if isinstance(method, str) else "",
                    )
                    if value
                )
                self.live_capture_var.set(
                    f"最近擷取：{captured}" + (f"｜{suffix}" if suffix else "")
                )
                self._refresh_capture_age()
        else:
            state_update = snapshot.get("state_update")
            if isinstance(state_update, Mapping):
                timestamp = state_update.get("timestamp")
                if isinstance(timestamp, (int, float)) and not isinstance(
                    timestamp, bool
                ):
                    self._last_capture_timestamp = float(timestamp)
                    self._last_update_label = "狀態更新"
                    self._refresh_capture_age()

        page = snapshot.get("page")
        page_text = cultivation_page_label(page, snapshot.get("native_screen_type"))
        state = snapshot.get("state")
        not_started = isinstance(state, Mapping) and state.get("in_progress") is False
        if not_started:
            state = {"in_progress": False}
            snapshot = {**dict(snapshot), "state": state, "cards": [], "drinks": [], "legal_actions": [], "recent_step": None}
        if isinstance(state, Mapping):
            self.live_grade_var.set(observed_grade_text(self._live_produce_id, state))
            step_type_value = state.get("step_type_value")
            exact_stage = {
                16: "Mid1",
                17: "Mid2",
                18: "Final",
            }.get(step_type_value)
            if exact_stage is not None and page == "exam":
                page_text = exact_stage
        profile = self._displayed_live_profile()
        route_week: int | None = None
        route_total: int | None = None
        if isinstance(state, Mapping):
            raw_week = state.get("week", state.get("route_week"))
            if (
                isinstance(raw_week, int)
                and not isinstance(raw_week, bool)
                and raw_week >= 0
            ):
                route_week = raw_week
                try:
                    route_total = load_route_calendar(
                        str(state.get("produce_id") or self._live_produce_id),
                        character_id=(
                            None if profile is None else profile.character_id
                        ),
                    ).total_weeks
                except (KeyError, OSError, TypeError, ValueError):
                    route_total = None
        route_label = (
            ""
            if route_week is None
            else "開場準備" if route_week == 0 else (
                f"第 {route_week} 週"
                if route_total is None
                else f"第 {route_week}/{route_total} 週"
            )
        )
        self.live_stage_status_var.set(
            "階段："
            + (f"{route_label}／" if route_label else "")
            + f"{page_text}／"
            f"{self._flow_label(None if profile is None else profile.exam_effect_type)}"
        )
        if route_label:
            self.live_route_var.set(f"培育進度：{route_label}｜目前：{page_text}")
        elif not_started:
            self.live_route_var.set("培育進度：等待開始")
        state_summary = _monitor_state_summary(snapshot)
        self.live_state_var.set(state_summary)
        self._set_text(
            self.live_state_summary_text,
            state_summary.replace("｜", "\n"),
        )

        state_update = snapshot.get("state_update")
        update_timestamp = (
            state_update.get("timestamp")
            if isinstance(state_update, Mapping)
            else None
        )
        if update_timestamp is None and isinstance(capture, Mapping):
            update_timestamp = capture.get("timestamp")
        recent_step = snapshot.get("recent_step")
        recent_action = ""
        if isinstance(recent_step, Mapping):
            raw_action = str(recent_step.get("action") or "")
            target = recent_step.get("target")
            recent_action = cultivation_action_label(raw_action, target)
            if target not in (None, "") and recent_action == raw_action:
                recent_action += f"：{target}"
        transition = snapshot.get("transition")
        if not recent_action and isinstance(transition, Mapping):
            recent_action = _monitor_action_label(transition.get("action"))
        monitor_signature = (
            (page_text, route_week, state_summary, recent_action)
            if snapshot.get("source") == "dll" else (
            snapshot.get("event_id"),
            update_timestamp,
            page_text,
            route_week,
            recent_action,
            )
        )
        if monitor_signature != self._last_monitor_log_signature:
            self._last_monitor_log_signature = monitor_signature
            message = f"{page_text}｜{state_summary}"
            if recent_action and recent_action != "—" and snapshot.get("source") != "dll":
                message += f"｜最近動作：{recent_action}"
            self._append_live_log(message)

        cards = snapshot.get("cards")
        self.live_card_tree.delete(*self.live_card_tree.get_children())
        if isinstance(cards, (list, tuple)):
            self.live_cards_label.set("目前手牌（自動培育既有觀測）")
            for index, raw in enumerate(cards):
                if not isinstance(raw, Mapping):
                    continue
                card_id = _monitor_action_label(raw)
                upgrade = raw.get("effective_upgrade", raw.get("upgrade"))
                label = raw.get("display_name", raw.get("name"))
                legal = raw.get("legal", raw.get("fully_supported"))
                self.live_card_tree.insert(
                    "",
                    "end",
                    iid=f"monitor-card-{index}",
                    values=(
                        card_id,
                        "—" if upgrade is None else f"+{upgrade}",
                        (
                            str(label or "自動培育觀測")
                            + (
                                f"｜使用 {raw['play_count']} 次"
                                if isinstance(raw.get("play_count"), int)
                                else ""
                            )
                        ),
                        (
                            "可用"
                            if legal is True
                            else "不可用"
                            if legal is False
                            else "未提供"
                        ),
                    ),
                )
        else:
            self.live_cards_label.set(
                "目前頁面沒有手牌；進入演出後會自動顯示"
                if str(page) != "exam"
                else "目前手牌：等待演出狀態"
            )

        drinks = snapshot.get("drinks")
        legal_actions = snapshot.get("legal_actions")
        drink_labels = (
            [_monitor_action_label(value) for value in drinks]
            if isinstance(drinks, (list, tuple))
            else []
        )
        legal_labels = (
            [_monitor_action_label(value) for value in legal_actions]
            if isinstance(legal_actions, (list, tuple))
            else []
        )
        drink_text = "、".join(value for value in drink_labels if value != "—")
        legal_text = "、".join(value for value in legal_labels if value != "—")
        in_exam = str(page) == "exam"
        if isinstance(drinks, (list, tuple)):
            drink_status = drink_text or "無可用飲料"
        else:
            drink_status = "等待演出狀態" if in_exam else "進入演出後顯示"
        if isinstance(legal_actions, (list, tuple)):
            legal_status = legal_text or "沒有已列出的候選"
        else:
            legal_status = "等待演出狀態" if in_exam else "此頁未列出"
        self.live_drinks_var.set(
            f"飲料：{drink_status}｜合法動作：{legal_status}"
        )

        self._monitor_base_recommendation_sources = (
            _monitor_recommendation_sources(snapshot)
        )
        self._recommendation_sources = dict(
            self._monitor_base_recommendation_sources
        )
        self._overlay_exact_exam_bc_evidence(snapshot)
        self._render_recommendation_rows()

    def _overlay_exact_exam_bc_evidence(
        self,
        snapshot: Mapping[str, object],
    ) -> None:
        """Add a comparison only for an actual, current diagnostic row."""

        try:
            identity = load_active_run()
        except (OSError, TypeError, ValueError):
            identity = None
        profile = next(
            (
                value
                for value in self.idol_profiles
                if identity is not None
                and value.id == getattr(identity, "idol_card_id", None)
            ),
            None,
        )
        context = _exact_exam_bc_monitor_context(
            snapshot,
            identity=identity,
            profile=profile,
        )
        evidence = self._exact_exam_bc_monitor.poll(context)
        if evidence is None:
            self._exact_exam_bc_overlay_active = False
            return
        formal = evidence.get("formal")
        model = evidence.get("model")
        if not isinstance(formal, Mapping) or not isinstance(model, Mapping):
            self._exact_exam_bc_overlay_active = False
            return
        agreement = model.get("agrees_with_formal")
        if type(agreement) is not bool:
            self._exact_exam_bc_overlay_active = False
            return
        self._recommendation_sources["rules"] = {
            "action_label": formal.get("action_id"),
            "reason": "本場正式控制器已完成的動作；ledger 僅供監看。",
            "status": "available",
            "legal": True,
            "executable": False,
        }
        self._recommendation_sources["behavior_cloning"] = {
            "action_label": model.get("top_action_id"),
            "confidence": model.get("top_probability"),
            "agreement": agreement,
            "reason": (
                "Exact Exam BC 離線 diagnostic；與正式動作"
                + ("一致。" if agreement else "不同。")
                + "只比較，不會改變動作。"
            ),
            "status": "available",
            "legal": True,
            "executable": False,
        }
        self._exact_exam_bc_overlay_active = True

    def _initialize_recommendation_sources(self) -> None:
        self._recommendation_sources = {
            "rules": None,
            "behavior_cloning": None,
            "offline_rl": None,
            "leaderboard_prior": None,
            "mixed": None,
        }
        self._monitor_base_recommendation_sources = dict(
            self._recommendation_sources
        )
        self._exact_exam_bc_overlay_active = False
        self._render_recommendation_rows()

    def _set_recommendation_source(
        self,
        source: str,
        payload: object,
    ) -> None:
        if source not in self._recommendation_sources:
            raise ValueError(f"unknown recommendation source: {source}")
        self._recommendation_sources[source] = payload
        self._render_recommendation_rows()

    @staticmethod
    def _recommendation_source_for_policy(policy: object) -> str:
        normalized = str(policy or "").casefold()
        if "imitation" in normalized or "prior" in normalized or "leaderboard" in normalized:
            return "leaderboard_prior"
        if "mixed" in normalized or "compose" in normalized or "hybrid" in normalized:
            return "mixed"
        return "rules"

    def _render_recommendation_rows(self) -> None:
        self._recommendation_rows = recommendation_rows(
            self._recommendation_sources
        )
        self.live_recommendation_tree.delete(
            *self.live_recommendation_tree.get_children()
        )
        self._visible_recommendation_rows = tuple(
            row for row in self._recommendation_rows if row.status == "available"
        )
        if not self._visible_recommendation_rows:
            self.live_recommendation_tree.insert(
                "",
                "end",
                iid="waiting",
                values=("—", "等待遊戲狀態", "系統", "—", "—", "等待中"),
            )
            self._set_text(
                self.live_reason_text,
                "開始培育後，這裡會顯示目前建議與選擇原因。",
            )
            return
        source_ranks: Counter[str] = Counter()
        for index, row in enumerate(self._visible_recommendation_rows):
            if row.status == "available":
                source_ranks[row.source] += 1
            rank = source_ranks[row.source]
            executable_now = bool(
                row.executable
                and self._live_suggested_action is not None
                and _live_execution_enabled(
                    click_available=self._controller_click_available,
                    busy=self._live_busy,
                    safety_blockers=self._live_safety_blockers,
                    click_count=self._live_suggested_action.click_count,
                )
            )
            self.live_recommendation_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    rank if row.status == "available" else "—",
                    row.action_label,
                    row.source_label,
                    row.score_text,
                    row.confidence_text,
                    (
                        "一致" if row.agreement else "不同"
                    )
                    if row.agreement is not None
                    else (
                        "可執行"
                        if executable_now
                        else ("只讀" if row.status == "available" else "尚無")
                    ),
                ),
            )
        if self._visible_recommendation_rows:
            self.live_recommendation_tree.selection_set("0")
            self._show_recommendation_detail()

    def _show_recommendation_detail(self, _event: object | None = None) -> None:
        selection = self.live_recommendation_tree.selection()
        if not selection or selection[0] == "waiting":
            return
        row = self._visible_recommendation_rows[int(selection[0])]
        legal = (
            "合法動作" if row.legal is True else "合法性未提供"
        )
        self._set_text(
            self.live_reason_text,
            f"來源：{row.source_label}\n"
            f"狀態：{row.status}／{legal}\n\n"
            f"策略值：{row.score_text}／信心・機率：{row.confidence_text}\n\n"
            f"{row.reason or '目前來源沒有補充說明。'}",
        )

    def _refresh_cultivation_overview(self) -> None:
        previous_run_id = (
            None if self._active_run is None else self._active_run.run_id
        )
        identity = load_active_run()
        shadow = None
        exam = None
        profile = None
        if identity is not None:
            self._active_run = identity
            self._run_paths = paths_for(identity)
            self._live_idol_card_id = identity.idol_card_id
            self._live_produce_id = identity.produce_id
            run_paths = paths_for(identity)
            shadow = load_run_shadow(run_paths.shadow)
            exam = load_exam_session(run_paths.exam_session)
            if shadow is not None:
                mode_key = (identity.idol_card_id, identity.produce_id)
                self._confirmed_nia_available_cards.add(mode_key)
                self._profile_availability_overrides.setdefault(mode_key, True)
                self._refresh_selected_profile_detail()
            profile = next(
                (
                    value
                    for value in self.idol_profiles
                    if value.id == identity.idol_card_id
                ),
                None,
            )
            if previous_run_id != identity.run_id:
                self._live_exam_session = exam
                self._live_exam_state = (
                    None if exam is None else asdict(exam.logic_state)
                )
                self._live_exam_identity = {
                    "produce_id": identity.produce_id,
                    "step_type": MID1 if exam is None else exam.step_type,
                    "stage_number": 1 if exam is None else exam.stage_number,
                }
                self._clear_live_suggestion(
                    "active run 已更新；舊建議與 checkpoint 顯示已清除。"
                )
        else:
            self._active_run = None
            self._run_paths = None
            if previous_run_id is not None:
                self._live_exam_session = None
                self._live_exam_state = None
                self._live_logic_state = None
                self._live_exam_identity = {
                    "produce_id": self.console_mode_var.get(),
                    "step_type": MID1,
                    "stage_number": 1,
                }
                self._clear_live_suggestion(
                    "active run 已結束；上一場狀態與建議已清除。"
                )
        view = cultivation_overview(
            identity=identity,
            shadow=shadow,
            profile=profile,
            exam_session=exam,
        )
        self._cultivation_overview = view
        self.live_grade_var.set(observed_grade_text(
            self._live_produce_id,
            {"vocal": view.vocal, "dance": view.dance, "visual": view.visual,
             "vote_count": getattr(shadow, "vote_count", None)},
        ))
        values = (
            "資料：已保存培育紀錄\n"
            f"偶像：{view.idol_name}\n"
            f"模式：{view.mode}\n"
            f"階段：{view.stage}\n"
            f"週次：{view.week if view.week is not None else '—'}\n\n"
            f"體力：{view.stamina if view.stamina is not None else '—'} / "
            f"{view.max_stamina if view.max_stamina is not None else '—'}\n"
            f"Vo / Da / Vi："
            f"{view.vocal if view.vocal is not None else '—'} / "
            f"{view.dance if view.dance is not None else '—'} / "
            f"{view.visual if view.visual is not None else '—'}\n"
            f"P 點數：{view.produce_points if view.produce_points is not None else '—'}\n"
            f"牌庫：{sum(row.count for row in view.deck) if view.deck else '尚未同步'} 張\n"
            f"持有物：{sum(count for _name, count in view.inventory) if view.inventory else 0}"
        )
        if identity is None:
            values = "尚無進行中的培育。\n\n選擇上方偶像與模式後，按開始即可進入培育。"
        self._set_text(self.live_state_summary_text, values)
        self.live_state_var.set(values.replace("\n", "｜"))
        if identity is not None:
            self.live_route_var.set(
                f"目前場次：{view.idol_name}｜{view.mode}"
            )
            selected = self._selected_profile()
            if (
                selected is not None
                and (
                    selected.id != identity.idol_card_id
                    or self.console_mode_var.get() != identity.produce_id
                )
            ):
                self.console_selection_note_var.set(
                    "上方選擇將套用於下一場；目前顯示的是另一場培育："
                    f"{view.idol_name}／{view.mode}。"
                )
            else:
                self.console_selection_note_var.set(
                    "上方選擇與目前培育一致。"
                )
        else:
            self.console_selection_note_var.set(
                "目前沒有進行中的培育；上方選擇將用於下一場。"
            )
        self._refresh_console_identity()
        if hasattr(self, "run_shadow_summary_text"):
            self._render_run_shadow_panel(view)

    def _render_run_shadow_panel(self, view: CultivationOverview) -> None:
        self._set_text(
            self.run_shadow_summary_text,
            f"偶像：{view.idol_name}\n"
            f"模式：{view.mode}\n"
            f"階段：{view.stage}\n"
            f"週次：{view.week if view.week is not None else '—'}\n"
            f"體力：{view.stamina if view.stamina is not None else '—'} / "
            f"{view.max_stamina if view.max_stamina is not None else '—'}\n"
            f"Vo / Da / Vi：{view.vocal or '—'} / {view.dance or '—'} / {view.visual or '—'}\n"
            f"P 點數：{view.produce_points if view.produce_points is not None else '—'}\n"
            f"持有物：{len(view.inventory)} 種",
        )
        self.run_deck_tree.delete(*self.run_deck_tree.get_children())
        for index, row in enumerate(view.deck):
            self.run_deck_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(row.card_id, f"+{row.upgrade}", row.count),
            )
        self.run_history_tree.delete(*self.run_history_tree.get_children())
        for index, (kind, detail) in enumerate(view.history):
            self.run_history_tree.insert(
                "", "end", iid=str(index), values=(kind, detail)
            )

    def _apply_current_advice_view(
        self, view: CurrentAdviceView
    ) -> None:
        self.plan3_advisor_status_var.set(view.status_text)
        self.plan3_advisor_action_var.set(view.action_text)
        self.plan3_advisor_terminal_var.set(view.prediction_text)
        self.plan3_advisor_reason_var.set(view.reason_text)
        action = view.action_text.removeprefix("行動：").strip()
        available = bool(action and action != "—" and "unavailable" not in view.status_text)
        self._set_recommendation_source(
            "rules",
            (
                {
                    "action_label": action,
                    "reason": f"{view.prediction_text}\n{view.reason_text}",
                    "status": "available",
                    "executable": False,
                }
                if available
                else None
            ),
        )

    def _show_plan3_advisor_waiting(self, message: str) -> None:
        if self._plan3_advisor_waiting_visible:
            return
        self._plan3_advisor_waiting_visible = True
        self.plan3_advisor_status_var.set("等待目前資料（unavailable）")
        self.plan3_advisor_action_var.set("行動：—")
        self.plan3_advisor_terminal_var.set("預測：—")
        self.plan3_advisor_reason_var.set(
            f"{message}\n僅讀取既有資料，不會自動操作遊戲。"
        )

    def _start_plan3_advisor_worker(
        self, signature: CurrentAdviceRefreshSignature
    ) -> None:
        self._plan3_advisor_waiting_visible = False
        self.plan3_advisor_status_var.set("正在計算目前建議…")
        self.plan3_advisor_action_var.set("行動：計算中")
        self.plan3_advisor_terminal_var.set("預測：計算中")
        self.plan3_advisor_reason_var.set(
            "正在依 outer stage 切換 Initial／N.I.A. 外層行動、課程或演出建議。"
        )

        live_inputs = self._current_advice_live_inputs
        produce_id = self._live_produce_id

        def worker() -> None:
            try:
                view = advise_current_advice_gui_file(
                    signature.path,
                    produce_id=produce_id,
                    live_inputs=live_inputs,
                )
                self._plan3_advisor_queue.put(("view", signature, view))
            except Exception as exc:
                self._plan3_advisor_queue.put(("error", signature, exc))

        threading.Thread(
            target=worker,
            daemon=True,
            name="gkms-plan3-advisor",
        ).start()

    def _drain_plan3_advisor_queue(self) -> None:
        try:
            while True:
                kind, signature, payload = self._plan3_advisor_queue.get_nowait()
                source = self._plan3_advisor_source_path
                current_local = (
                    None
                    if source is None
                    else read_current_advice_local_save_signature(source)
                )
                current_signature = (
                    None
                    if current_local is None
                    else CurrentAdviceRefreshSignature(
                        current_local,
                        self._current_advice_live_inputs.revision,
                        self._live_produce_id,
                    )
                )
                if signature != current_signature:
                    self._plan3_advisor_tracker.finish(retry=True)
                    continue
                if kind == "view":
                    view = payload
                    self._plan3_advisor_tracker.finish(retry=view.retry)
                    self._apply_current_advice_view(view)
                    self._plan3_advisor_waiting_visible = False
                else:
                    self._plan3_advisor_tracker.finish(retry=True)
                    self.plan3_advisor_status_var.set(
                        "計算暫時失敗（unavailable）"
                    )
                    self.plan3_advisor_action_var.set("行動：—")
                    self.plan3_advisor_terminal_var.set("預測：—")
                    self.plan3_advisor_reason_var.set(
                        "既有資料可能正在更新，稍後會自動重試；不會操作遊戲。"
                    )
        except queue.Empty:
            pass

    def _poll_plan3_advisor(self) -> None:
        from .runtime_command_client import input_backend
        try:
            native = input_backend() == "dll"
        except (OSError, ValueError, RuntimeError):
            native = True
        if native:
            # Native runner progress owns both the state and the decision.
            # Do not start a competing LocalSave planner in the GUI.
            self._plan3_advisor_after_id = self.after(1000, self._poll_plan3_advisor)
            return
        if self._plan3_advisor_discovery_countdown <= 0:
            discovered = select_plan3_exam_local_save_path()
            if discovered is not None:
                self._plan3_advisor_source_path = discovered
            self._plan3_advisor_discovery_countdown = 5
        else:
            self._plan3_advisor_discovery_countdown -= 1
        self._drain_plan3_advisor_queue()

        source = self._plan3_advisor_source_path
        local_signature = (
            None
            if source is None
            else read_current_advice_local_save_signature(source)
        )
        if local_signature is None:
            self._plan3_advisor_tracker.source_missing()
            if not self._plan3_advisor_tracker.busy:
                message = (
                    "尚未找到 outer local save；有資料後會自動開始。"
                    if source is None
                    else "目前沒有可讀取的 outer local save。"
                )
                self._show_plan3_advisor_waiting(message)
        else:
            marker = local_signature.outer_marker
            if (
                self._current_advice_outer_marker is not None
                and marker != self._current_advice_outer_marker
            ):
                self._current_advice_live_inputs = (
                    self._current_advice_live_inputs.clear()
                )
            self._current_advice_outer_marker = marker
            signature = CurrentAdviceRefreshSignature(
                local_signature,
                self._current_advice_live_inputs.revision,
                self._live_produce_id,
            )
            if self._plan3_advisor_tracker.claim(signature):
                self._start_plan3_advisor_worker(signature)

        self._plan3_advisor_after_id = self.after(
            1000, self._poll_plan3_advisor
        )

    def _run_live_worker(self, kind: str, operation: object) -> None:
        library = getattr(self, "card_library_panel", None)
        if kind != "status" and library is not None and library.game_input_pending:
            self._append_live_log("編成正在套用，請等待目前操作完成。")
            return
        if self._live_busy:
            self._append_live_log("已有 Live 工作進行中。")
            return
        self._live_busy = True
        self.live_sample_button.configure(state="disabled")
        self.live_capture_button.configure(state="disabled")
        self.live_state_button.configure(state="disabled")
        self.live_training_button.configure(state="disabled")
        self.live_reward_button.configure(state="disabled")
        self.live_deck_button.configure(state="disabled")
        self.live_shop_button.configure(state="disabled")
        self.live_detect_button.configure(state="disabled")
        self.live_exam_button.configure(state="disabled")
        self.live_execute_button.configure(state="disabled")
        self.live_start_auto_button.configure(state="disabled")
        if hasattr(self, "batch_start_button"):
            self.batch_start_button.configure(state="disabled")
        def worker() -> None:
            try:
                result = operation()  # type: ignore[operator]
                self._live_queue.put((kind, result))
            except Exception as exc:
                self._live_queue.put(("error", (kind, exc)))
        threading.Thread(target=worker, daemon=True, name=f"gkms-live-{kind}").start()

    def _load_live_calibration(self) -> None:
        self._clear_live_suggestion("校正回放不提供可執行建議。")
        self._run_live_worker("calibration", load_calibration_sample)

    def _capture_live_once(self) -> None:
        self._clear_live_suggestion("畫面已重新擷取，原建議失效。")
        self._run_live_worker("capture", capture_once)

    def _detect_live_cards(self) -> None:
        self._clear_live_suggestion("卡牌辨識會使原選項建議失效。")
        self._run_live_worker("detect", detect_live_cards)

    def _require_active_live_run(self) -> tuple[RunIdentity, RunPaths]:
        if self._active_run is None or self._run_paths is None:
            raise RuntimeError("請先在培育流程頁建立目前培育場次")
        return self._active_run, self._run_paths

    def _read_live_lesson(self) -> None:
        self._clear_live_suggestion("正在重新讀取並計算目前出牌畫面。")
        active_run, _run_paths = self._require_active_live_run()
        prior_state = self._live_logic_state
        shadow = load_run_shadow(_run_paths.shadow)
        observed_max_stamina = None if shadow is None else shadow.max_stamina
        if observed_max_stamina is not None and prior_state is not None:
            prior_state = dict(prior_state)
            prior_state["max_stamina"] = observed_max_stamina
        clear_target = self._live_clear_target
        self._run_live_worker(
            "lesson",
            lambda: read_live_lesson_with_retry(
                prior_state=prior_state,
                clear_target=clear_target,
                idol_card_id=active_run.idol_card_id,
                lesson_type=self._live_lesson_type,
                max_stamina=observed_max_stamina,
            ),
        )

    def _checkpoint_live_pursuit_result(self, capture: Mapping[str, object]) -> None:
        """Terminal lesson evidence is read-only until its shadow check passes."""
        self._clear_live_suggestion("terminal lesson result blocks ordinary card actions")
        self._run_live_worker(
            "lesson_result",
            lambda: checkpoint_live_pursuit_lesson_result(capture),
        )

    def _read_live_exam(self) -> None:
        self._clear_live_suggestion("正在重新讀取並計算目前考試牌面。")
        active_run, _run_paths = self._require_active_live_run()
        prior_state = self._live_exam_state
        identity = dict(self._live_exam_identity)
        self._run_live_worker(
            "exam",
            lambda: read_live_exam_with_retry(
                prior_state=prior_state,
                idol_card_id=active_run.idol_card_id,
                produce_id=str(identity["produce_id"]),
                step_type=str(identity["step_type"]),
                stage_number=int(identity["stage_number"]),
                identity=active_run,
            ),
        )

    def _read_live_overview(self) -> None:
        self._clear_live_suggestion("畫面已重新擷取，請辨識選項後再執行。")
        active_run, _run_paths = self._require_active_live_run()
        self._run_live_worker(
            "overview",
            lambda: read_live_overview_decision(
                idol_card_id=active_run.idol_card_id,
                produce_id=active_run.produce_id,
            ),
        )

    def _read_live_training_choice(self) -> None:
        self._clear_live_suggestion("正在重新辨識訓練選項。")
        self._run_live_worker("training", read_live_training_choice)

    def _read_live_reward(self) -> None:
        self._clear_live_suggestion("課後獎勵辨識為只讀，不提供領取動作。")
        active_run, _run_paths = self._require_active_live_run()
        session = self._live_reward_workflow_session
        self._run_live_worker(
            "reward",
            lambda: read_live_reward_preview_workflow(
                session=session,
                expected_run_id=active_run.run_id,
                expected_idol_card_id=active_run.idol_card_id,
                expected_produce_id=active_run.produce_id,
            ),
        )

    def _sync_live_deck_panel(self) -> None:
        self._clear_live_suggestion("正在驗證演出畫面並同步山札／捨札／除外。")
        identity = dict(self._live_exam_identity)
        self._run_live_worker(
            "deck",
            lambda: sync_live_deck_panel(
                step_type=str(identity["step_type"]),
                stage_number=int(identity["stage_number"]),
                source="visible-owned-card-panel",
            ),
        )

    def _read_live_shop(self) -> None:
        self._clear_live_suggestion("商店畫面不會提供自動執行建議。")
        self._run_live_worker("shop", read_live_shop)

    def _execute_live_suggestion(self) -> None:
        action = self._live_suggested_action
        if action is None:
            self._append_live_log("目前沒有可執行、且仍有效的建議。")
            return
        # A button callback can already be queued when the controller changes
        # state or another worker begins.  Recheck the render-time gate here;
        # button state alone is never authorization to submit input.
        if not _live_execution_enabled(
            click_available=self._controller_click_available,
            busy=self._live_busy,
            safety_blockers=self._live_safety_blockers,
            click_count=action.click_count,
        ):
            self._clear_live_suggestion(
                "live execution gate changed; fresh stable analysis is required"
            )
            return
        if self._live_safety_blockers:
            self.live_recommendation_var.set(
                "目前只分析：攜帶內容或規則尚未通過安全驗證。"
            )
            self._append_live_log(
                "已阻擋背景點擊：" + "；".join(self._live_safety_blockers)
            )
            self._refresh_execute_button()
            return
        if not self._controller_background_control:
            self.live_recommendation_var.set(
                "已降級為只分析：背景控制器尚未就緒"
            )
            self._clear_live_suggestion(
                "安全限制：不使用前景滑鼠／鍵盤；請先恢復 Maa 背景控制。"
            )
            return

        if self._live_suggestion_kind in {"lesson", "exam"}:
            context = self._live_suggestion_context
            predicted = self._live_suggested_next_state
            if context is None or predicted is None:
                self._clear_live_suggestion(
                    "卡牌建議缺少驗證 checkpoint，已降級為只分析。"
                )
                return
            card_play = dict(context["card_play"])  # type: ignore[arg-type]
            mode = str(card_play["mode"])
            analyzer_arguments: dict[str, object] = {
                "mode": mode,
                "expected_card_id": str(card_play["expected_card_id"]),
                "expected_upgrade": int(card_play["expected_upgrade"]),
                "expected_after": dict(predicted),
                "expected_card_x": action.canonical_x,
                "support_card_ids": tuple(card_play.get("support_card_ids", ())),
            }
            if mode == "lesson":
                analyzer_arguments.update(
                    clear_target=int(card_play["clear_target"]),
                    lesson_perfect_target=int(
                        card_play["lesson_perfect_target"]
                    ),
                    gimmick_group_id=card_play.get("gimmick_group_id"),
                )
            else:
                analyzer_arguments.update(
                    idol_card_id=str(card_play["idol_card_id"]),
                    produce_id=str(card_play["produce_id"]),
                    step_type=str(card_play["step_type"]),
                    stage_number=int(card_play["stage_number"]),
                )
            analyzer = make_live_card_frame_analyzer(**analyzer_arguments)  # type: ignore[arg-type]
            self.live_recommendation_var.set(
                "等待 SELECT 驗證、結算動畫與連續兩張穩定畫面"
            )
            self._run_live_worker(
                "execute",
                lambda: execute_verified_card_click(action, analyzer),
            )
            return

        settle = 1.0 if action.label.startswith("領取獎勵：") else 0.65
        self._run_live_worker(
            "execute",
            lambda: execute_suggested_click(action, settle_seconds=settle),
        )

    def _drain_live_queue(self) -> None:
        try:
            while True:
                kind, payload = self._live_queue.get_nowait()
                if should_reset_reward_preview_workflow(kind, payload):
                    if self._live_reward_workflow_session is not None:
                        self._append_live_log(
                            "畫面工作或辨識身分已改變；課後獎勵預覽進度已重置。"
                        )
                    self._live_reward_workflow_session = None
                    self.live_reward_button.configure(text="讀取技能卡獎勵")
                prior_advice_inputs = self._current_advice_live_inputs
                self._current_advice_live_inputs = observe_current_advice_live_update(
                    prior_advice_inputs,
                    kind,
                    payload,
                )
                if (
                    self._current_advice_live_inputs.revision
                    != prior_advice_inputs.revision
                ):
                    self._show_plan3_advisor_waiting(
                        "畫面資料已更新，正在重新計算目前建議。"
                    )
                self._live_busy = False
                self.live_sample_button.configure(state="normal")
                self.live_capture_button.configure(state="normal")
                self.live_state_button.configure(state="normal")
                self.live_training_button.configure(state="normal")
                self.live_reward_button.configure(state="normal")
                self.live_deck_button.configure(state="normal")
                self.live_shop_button.configure(state="normal")
                self.live_detect_button.configure(state="normal")
                self.live_exam_button.configure(state="normal")
                if kind == "error":
                    source, error = payload  # type: ignore[misc]
                    if source in {"training", "reward", "deck", "lesson", "lesson_result", "exam", "execute"}:
                        self._clear_live_suggestion(
                            "辨識或執行失敗；必須重新辨識目前畫面。"
                        )
                    if source == "lesson_result":
                        self.live_recommendation_var.set(
                            "追趕課程結果未通過三項 final／delta／before 驗證；未寫入 run shadow。"
                        )
                    if source == "reward":
                        self.live_state_var.set("課後獎勵｜辨識工作階段已重置")
                        self.live_recommendation_var.set(
                            "畫面、時間序列或培育場次未通過一致性檢查；請從未選取的獎勵總覽重新讀取。"
                        )
                    self.live_controller_var.set(f"Controller：{source} 失敗（{type(error).__name__}）")
                    if source == "status":
                        self.live_game_status_var.set("遊戲：未連線")
                    self._append_live_log(f"{source} 失敗：{error}")
                elif kind == "lesson_result":
                    result = payload  # type: ignore[assignment]
                    if not result.get("accepted", False):
                        evidence = result.get("capture", {}).get("png_path", "未儲存")
                        self.live_recommendation_var.set(
                            "追趕課程結果未通過三項 final／delta／before 驗證；未寫入 run shadow。"
                        )
                        self._append_live_log(
                            f"追趕課程結果被阻擋；evidence 保留：{evidence}；{result.get('reason', 'unknown')}"
                        )
                    else:
                        values = result["result"]
                        self.live_recommendation_var.set(
                            "已驗證追趕課程結果並更新 run shadow："
                            f"Vo {values['vocal']} / Da {values['dance']} / Vi {values['visual']}"
                        )
                        self._append_live_log("追趕課程結果已由 final／delta／before 自洽驗證。")
                elif kind == "status":
                    status = payload  # type: ignore[assignment]
                    native = status.get("input_backend") == "dll"
                    self._controller_click_available = bool(status.get("click_available"))
                    self._controller_background_control = bool(
                        status.get("background_control")
                    )
                    mode = (
                        "DLL 命令介面" if native else "Maa 背景控制"
                        if self._controller_background_control
                        else "前景相容模式"
                    )
                    self.live_controller_var.set(
                        f"Controller：已連線（{mode}；PID {status.get('target_pid', '—')}；"
                        f"{status.get('helper_integrity', '—')}）"
                    )
                    connected = status.get("bridge_live") is True if native else status.get("target_pid") not in {None, 0, ""}
                    self.live_game_status_var.set(
                        "遊戲：已連線" if connected else "遊戲：未連線"
                    )
                    self.console_source_var.set("Live" if connected else "Offline")
                    self.live_source_status_var.set(
                        "來源：" + self.console_source_var.get()
                    )
                    if native and not connected:
                        self.status_var.set("DLL 尚未連線；請先開啟遊戲。")
                    # Status establishes connectivity only.  The unattended
                    # worker remains the sole capture/recognition/input owner;
                    # this monitor never starts a parallel manual analyzer.
                    if connected:
                        self.live_state_var.set("等待自動培育提供目前狀態。")
                elif kind == "calibration":
                    self._apply_live_view(payload)  # type: ignore[arg-type]
                    self._append_live_log("已載入校正回放樣本；資料不是即時 OCR。")
                elif kind == "capture":
                    capture = payload  # type: ignore[assignment]
                    captured = datetime.fromtimestamp(float(capture["timestamp"])).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(f"最近擷取：{captured}｜{capture['width']}×{capture['height']}｜{capture['capture_method']}｜{capture.get('png_path') or '未儲存'}")
                    self.console_capture_age_var.set("剛剛")
                    self._last_capture_timestamp = float(capture["timestamp"])
                    self.console_source_var.set("Live")
                    self.live_source_status_var.set("來源：Live")
                    self._append_live_log("已擷取一張畫面；尚未進行 OCR 或狀態推論。")
                elif kind == "detect":
                    view = payload  # type: ignore[assignment]
                    self._apply_live_view(view)
                    capture = view.capture or {}
                    captured = datetime.fromtimestamp(float(capture["timestamp"])).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(f"最近偵測：{captured}｜{capture.get('png_path', '未儲存')}")
                    self._append_live_log("已用 Maa cards.onnx 找牌，並以本機 Octo 卡圖對應 Master 身分。")
                elif kind == "lesson":
                    result = payload  # type: ignore[assignment]
                    screen = result["screen"]
                    state = result["logic_state"]
                    capture = result["capture"]
                    self._last_capture_timestamp = float(capture["timestamp"])
                    self.console_source_var.set("Live")
                    self.live_source_status_var.set("來源：Live")
                    self.live_stage_status_var.set("階段：課程")
                    candidates = result["candidates"]
                    recommendation = result["recommendation"]
                    self._live_safety_blockers = tuple(
                        str(value)
                        for value in result.get("safety", {}).get(
                            "blockers", ()
                        )
                    )
                    self._live_logic_state = dict(state)
                    self._live_clear_target = int(result["clear_target"])
                    self._live_lesson_type = str(
                        result.get("card_play_context", {}).get(
                            "lesson_type", "ProduceStepLessonType_Unknown"
                        )
                    )
                    active_run, run_paths = self._require_active_live_run()
                    save_lesson_session(
                        self._live_logic_state,
                        self._live_clear_target,
                        idol_card_id=active_run.idol_card_id,
                        gimmick_group_id=LIVE_GIMMICK_GROUP_ID,
                        lesson_type=self._live_lesson_type,
                        run_id=active_run.run_id,
                        character_id=active_run.character_id,
                        produce_id=active_run.produce_id,
                        path=run_paths.lesson_session,
                    )
                    self._live_suggested_next_state = dict(
                        result["recommended_after"]
                    )
                    self._live_suggested_action = SuggestedClick.from_dict(
                        result["action"]
                    )
                    self._live_suggestion_kind = "lesson"
                    self._live_suggestion_context = {
                        "before": dict(state),
                        "predicted": dict(result["recommended_after"]),
                        "card": dict(recommendation),
                        "card_play": dict(result["card_play_context"]),
                        "ids": dict(result["rule_ids"]),
                        "context": {
                            "mode": "lesson",
                            "screen": dict(screen),
                            "item": dict(result["item"]),
                            "gimmick": dict(result["gimmick"]),
                            "clear_target": int(result["clear_target"]),
                        },
                    }
                    self.live_state_var.set(
                        f"第 {state['round_number']} 回合｜剩餘 {screen['turns_remaining']} 回合｜"
                        f"{screen['target_tier'].upper()} 尚差 {screen['clear_remaining']}｜體力 {screen['stamina']}｜"
                        f"元氣 {screen['block']}｜好印象 {state['good_impression']}｜"
                        f"幹勁 {state['motivation']}"
                    )
                    self.live_cards_label.set(
                        "目前手牌：卡名 OCR＋本地 Master；表格依推薦順序"
                    )
                    self.live_card_tree.delete(
                        *self.live_card_tree.get_children()
                    )
                    for rank, candidate in enumerate(candidates, 1):
                        self.live_card_tree.insert(
                            "",
                            "end",
                            iid=str(rank - 1),
                            values=(
                                candidate["card_id"],
                                f"+{candidate['upgrade']}",
                                f"#{rank} {candidate['display_name']}｜"
                                f"本回合 +{candidate['score_gain']}｜"
                                f"保守延續 +{candidate['carry_score_gain']}",
                                "可使用" if candidate["fully_supported"] else "未支援",
                            ),
                        )
                    top_executable = _live_execution_enabled(
                        click_available=self._controller_click_available,
                        busy=self._live_busy,
                        safety_blockers=self._live_safety_blockers,
                        click_count=self._live_suggested_action.click_count,
                    )
                    self._set_recommendation_source(
                        "rules",
                        [
                            {
                                "action_label": f"使用卡牌 {candidate['display_name']}",
                                "score": float(candidate["score_gain"])
                                + float(candidate["carry_score_gain"]),
                                "confidence": float(result["confidence"]),
                                "reason": (
                                    f"本回合 +{candidate['score_gain']}；"
                                    f"保守延續 +{candidate['carry_score_gain']}"
                                ),
                                "status": "available",
                                "legal": bool(candidate["fully_supported"]),
                                "executable": bool(rank == 1 and top_executable),
                                "card_id": str(candidate["card_id"]),
                                "hand_slot": int(candidate["hand_index"]),
                            }
                            for rank, candidate in enumerate(candidates, 1)
                        ],
                    )
                    self.live_recommendation_var.set(
                        f"推薦：{recommendation['display_name']}｜"
                        f"本回合 +{recommendation['score_gain']}｜"
                        f"保守延續 +{recommendation['carry_score_gain']}"
                    )
                    self.live_confidence_var.set(
                        f"畫面最低辨識信心：{float(result['confidence']):.1%}"
                    )
                    reason_lines = [
                        f"• {result['method']}",
                        f"• P 道具：{result['item']['name']}（{result['item']['id']}）",
                    ]
                    if self._live_safety_blockers:
                        reason_lines.append(
                            "⚠ 只分析／禁止點擊："
                            + "；".join(self._live_safety_blockers)
                        )
                    if result["gimmick"]["fired_effect_ids"]:
                        reason_lines.append(
                            "• 本回合課程 gimmick："
                            + ", ".join(result["gimmick"]["fired_effect_ids"])
                        )
                    reason_lines.extend(
                        f"• #{rank} {candidate['display_name']}：體力 -{candidate['stamina_paid']}，"
                        f"本回合 +{candidate['score_gain']}，保守延續 +{candidate['carry_score_gain']}"
                        for rank, candidate in enumerate(candidates, 1)
                    )
                    self._set_text(self.live_reason_text, "\n".join(reason_lines))
                    captured = datetime.fromtimestamp(
                        float(capture["timestamp"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(
                        f"最近回合分析：{captured}｜{capture['width']}×"
                        f"{capture['height']}｜{capture['capture_method']}"
                    )
                    self._append_live_log(
                        f"已驗證目前回合；待執行建議：{recommendation['display_name']}。"
                    )
                    if int(result.get("read_attempts", 1)) > 1:
                        self._append_live_log(
                            f"卡牌動畫結束後第 {result['read_attempts']} 次擷取才接受畫面。"
                        )
                elif kind == "exam":
                    result = payload  # type: ignore[assignment]
                    screen = result["screen"]
                    state = result["logic_state"]
                    rules = result["rules"]
                    capture = result["capture"]
                    self._last_capture_timestamp = float(capture["timestamp"])
                    self.console_source_var.set("Live")
                    self.live_source_status_var.set("來源：Live")
                    candidates = result["candidates"]
                    recommendation = result["recommendation"]
                    self._live_safety_blockers = tuple(
                        str(value)
                        for value in result.get("safety", {}).get(
                            "blockers", ()
                        )
                    )
                    self._live_exam_state = dict(state)
                    self._live_exam_identity = {
                        "produce_id": str(rules["produce_id"]),
                        "step_type": str(rules["step_type"]),
                        "stage_number": int(rules["number"]),
                    }
                    self._refresh_console_identity()
                    active_run, run_paths = self._require_active_live_run()
                    persisted_exam = load_exam_session(run_paths.exam_session)
                    checkpoint_blockers: list[str] = []
                    if persisted_exam is None:
                        checkpoint_blockers.append("exam-session-missing-after-read")
                    else:
                        if not persisted_exam.matches_run(
                            run_id=active_run.run_id,
                            idol_card_id=active_run.idol_card_id,
                            character_id=active_run.character_id,
                            produce_id=active_run.produce_id,
                        ):
                            checkpoint_blockers.append(
                                "exam-session-run-identity-mismatch-after-read"
                            )
                        if (
                            persisted_exam.step_type
                            != self._live_exam_identity["step_type"]
                            or persisted_exam.stage_number
                            != self._live_exam_identity["stage_number"]
                        ):
                            checkpoint_blockers.append(
                                "exam-session-stage-identity-mismatch-after-read"
                            )
                        if asdict(persisted_exam.logic_state) != self._live_exam_state:
                            checkpoint_blockers.append(
                                "exam-session-state-mismatch-after-read"
                            )
                        serialized_session = result.get("exam_session")
                        if (
                            not isinstance(serialized_session, Mapping)
                            or dict(serialized_session) != persisted_exam.to_dict()
                        ):
                            checkpoint_blockers.append(
                                "exam-session-payload-mismatch-after-read"
                            )
                    self._live_exam_session = persisted_exam
                    self._live_safety_blockers = tuple(
                        dict.fromkeys(
                            (*self._live_safety_blockers, *checkpoint_blockers)
                        )
                    )
                    self._live_suggested_next_state = dict(
                        result["recommended_after"]
                    )
                    self._live_suggested_action = SuggestedClick.from_dict(
                        result["action"]
                    )
                    self._live_suggestion_kind = "exam"
                    self._live_suggestion_context = {
                        "before": dict(state),
                        "predicted": dict(result["recommended_after"]),
                        "card": dict(recommendation),
                        "card_play": dict(result["card_play_context"]),
                        "ids": dict(result["rule_ids"]),
                        "context": {
                            "mode": "exam",
                            "screen": dict(screen),
                            "item": dict(result["item"]),
                            "rules": dict(rules),
                            "gimmick_effect_ids": list(
                                result["gimmick_effect_ids"]
                            ),
                            "force_end_policy": dict(
                                result["force_end_policy"]
                            ),
                        },
                    }
                    self.live_state_var.set(
                        f"REGULAR 期中｜第 {state['round_number']} 回合｜"
                        f"剩餘 {screen['turns_remaining']} 回合｜分數 "
                        f"{screen['player_score']}/{rules['force_end_score']}｜"
                        f"倍率 {screen['score_multiplier_permille'] / 10:.0f}%｜"
                        f"體力 {screen['stamina']}/{state['max_stamina']}｜"
                        f"元氣 {screen['block']}｜好印象 {state['good_impression']}｜"
                        f"幹勁 {state['motivation']}"
                    )
                    self.live_cards_label.set(
                        "目前考試手牌：卡名 OCR＋本機 Master；表格依策略排序"
                    )
                    self.live_card_tree.delete(
                        *self.live_card_tree.get_children()
                    )
                    for rank, candidate in enumerate(candidates, 1):
                        self.live_card_tree.insert(
                            "",
                            "end",
                            iid=str(rank - 1),
                            values=(
                                candidate["card_id"],
                                f"+{candidate['upgrade']}",
                                f"#{rank} {candidate['display_name']}｜"
                                f"本回合 +{candidate['score_gain']}｜"
                                f"策略值 {candidate['strategic_value']}｜"
                                f"官方時機 {candidate['auto_play_evaluation']}"
                                + ("｜本手結束" if candidate['completes_audition'] else ""),
                                "可使用" if candidate["fully_supported"] else "未支援",
                            ),
                        )
                    top_executable = _live_execution_enabled(
                        click_available=self._controller_click_available,
                        busy=self._live_busy,
                        safety_blockers=self._live_safety_blockers,
                        click_count=self._live_suggested_action.click_count,
                    )
                    self._set_recommendation_source(
                        "rules",
                        [
                            {
                                "action_label": f"使用卡牌 {candidate['display_name']}",
                                "score": float(candidate["strategic_value"]),
                                "confidence": float(result["confidence"]),
                                "reason": (
                                    f"本回合 +{candidate['score_gain']}；"
                                    f"官方時機 {candidate['auto_play_evaluation']}"
                                ),
                                "status": "available",
                                "legal": bool(candidate["fully_supported"]),
                                "executable": bool(rank == 1 and top_executable),
                                "card_id": str(candidate["card_id"]),
                                "hand_slot": int(candidate["hand_index"]),
                            }
                            for rank, candidate in enumerate(candidates, 1)
                        ],
                    )
                    self.live_recommendation_var.set(
                        f"推薦：{recommendation['display_name']}｜"
                        f"本回合精算 +{recommendation['score_gain']}｜"
                        f"策略值 {recommendation['strategic_value']}｜"
                        f"官方時機 {recommendation['auto_play_evaluation']}"
                        + ("｜本手達標結束" if recommendation['completes_audition'] else "")
                    )
                    self.live_confidence_var.set(
                        f"畫面最低辨識信心：{float(result['confidence']):.1%}"
                    )
                    reason_lines = [
                        f"• Master：{rules['mode_name']} / {rules['step_type']} / "
                        f"{rules['turns']} 回合 / 第 {rules['rank_threshold']} 名以上或 "
                        f"{rules['force_end_score']} 分。",
                        "• ExamSetting：提早結束時，每個剩餘回合恢復體力 "
                        f"+{rules['turn_end_stamina_recovery']}。",
                        f"• {result['method']}",
                        f"• P 道具：{result['item']['name']}（{result['item']['id']}）",
                    ]
                    if self._live_safety_blockers:
                        reason_lines.append(
                            "⚠ 只分析／禁止點擊："
                            + "；".join(self._live_safety_blockers)
                        )
                    reason_lines.extend(
                        f"• #{rank} {candidate['display_name']}："
                        f"本回合 +{candidate['score_gain']}（raw {candidate['raw_score_gain']}），"
                        f"自然延續 raw {candidate['passive_raw_carry']}，"
                        f"setup +{candidate['setup_value']}，官方時機 "
                        f"{candidate['auto_play_evaluation']}，體力後 "
                        f"{candidate['after']['stamina']}，overkill "
                        f"{candidate['overkill_score']}"
                        for rank, candidate in enumerate(candidates, 1)
                    )
                    self._set_text(
                        self.live_reason_text, "\n".join(reason_lines)
                    )
                    captured = datetime.fromtimestamp(
                        float(capture["timestamp"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(
                        f"最近考試分析：{captured}｜{capture['width']}×"
                        f"{capture['height']}｜{capture['capture_method']}"
                    )
                    self._append_live_log(
                        f"已驗證 REGULAR 期中第 {state['round_number']} 回合；"
                        f"待執行建議：{recommendation['display_name']}。"
                    )
                    if int(result.get("read_attempts", 1)) > 1:
                        self._append_live_log(
                            f"卡牌動畫結束後第 {result['read_attempts']} 次擷取才接受畫面。"
                        )
                elif kind == "overview":
                    result = payload  # type: ignore[assignment]
                    state = result["state"]
                    capture = result["capture"]
                    self._last_capture_timestamp = float(capture["timestamp"])
                    self.console_source_var.set("Live")
                    self.live_source_status_var.set("來源：Live")
                    self.live_stage_status_var.set("階段：培育外層")
                    decision = result["decision"]
                    recommended = decision["recommended"]
                    self._live_suggested_action = SuggestedClick.from_dict(
                        result["action"]
                    )
                    self._live_suggestion_kind = "overview"
                    profile = next(
                        (
                            item
                            for item in self.idol_profiles
                            if item.id == self._live_idol_card_id
                        ),
                        None,
                    )
                    route = load_route_calendar(
                        self._live_produce_id,
                        character_id=None if profile is None else profile.character_id,
                    )
                    position = infer_position_from_final_countdown(
                        route, int(state["weeks_remaining"])
                    )
                    self._live_route_position = position
                    self.live_state_var.set(
                        f"剩餘 {state['weeks_remaining']} 週｜體力 "
                        f"{state['stamina']}/{state['max_stamina']}｜P {state['produce_points']}｜"
                        f"Vo {state['vocal']} / Da {state['dance']} / Vi {state['visual']}"
                    )
                    action_source = (
                        position.route_week.display
                        if position.route_week.exact_actions
                        else "逐週選項尚未匯入"
                    )
                    self.live_route_var.set(
                        f"路線位置：推定第 {position.current_week}/{route.total_weeks} 週｜"
                        f"公開行程：{action_source}｜"
                        f"距 {position.next_milestone.label} "
                        f"{position.weeks_until_next_milestone} 週"
                    )
                    self._refresh_route_calendar()
                    self.live_confidence_var.set(
                        f"OCR 信心 {float(state['confidence']):.1%}｜"
                        f"行動模板 {float(recommended['match_score']):.1%}"
                    )
                    self.live_recommendation_var.set(
                        f"推薦：{recommended['label']}（{decision['policy']}）"
                    )
                    self._set_recommendation_source(
                        self._recommendation_source_for_policy(
                            decision.get("policy")
                        ),
                        {
                            "action_label": str(recommended["label"]),
                            "score": float(recommended["match_score"]),
                            "confidence": float(state["confidence"]),
                            "reason": "\n".join(
                                str(reason) for reason in decision["reasons"]
                            ),
                            "status": "available",
                            "legal": True,
                            "executable": _live_execution_enabled(
                                click_available=self._controller_click_available,
                                busy=self._live_busy,
                                safety_blockers=self._live_safety_blockers,
                                click_count=self._live_suggested_action.click_count,
                            ),
                        },
                    )
                    self._set_text(
                        self.live_reason_text,
                        "\n".join(f"• {reason}" for reason in decision["reasons"]),
                    )
                    captured = datetime.fromtimestamp(
                        float(capture["timestamp"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(
                        f"最近狀態擷取：{captured}｜{capture['width']}×{capture['height']}｜"
                        f"{capture['capture_method']}"
                    )
                    self._append_live_log(
                        "已從完整可見畫面讀取培育總覽；目前倒數直接定位新觀測點，"
                        f"不沿用誤點前的舊週次。待執行：{recommended['label']}。"
                    )
                    self._refresh_cultivation_overview()
                elif kind == "deck":
                    result = payload  # type: ignore[assignment]
                    layout = result["layout"]
                    identities = result["cards"]
                    capture = result["capture"]
                    headers = layout["headers"]
                    pile_summary = "、".join(
                        f"{item['raw_text']}" for item in headers
                    )
                    self.live_state_var.set(
                        f"演出牌組面板｜手牌偵測 {result['hand_detection_count']} 張｜"
                        f"{pile_summary}"
                    )
                    self.live_cards_label.set(
                        f"牌組面板：定位 {layout['visible_total']}/"
                        f"{layout['declared_total']} 張縮圖"
                    )
                    self.live_card_tree.delete(
                        *self.live_card_tree.get_children()
                    )
                    pile_labels = {
                        "draw": "山札",
                        "discard": "捨札",
                        "excluded": "除外",
                    }
                    for index, (cell, identity) in enumerate(
                        zip(layout["cells"], identities)
                    ):
                        self.live_card_tree.insert(
                            "",
                            "end",
                            iid=f"deck-{index}",
                            values=(
                                identity["card_id"] or f"未知 #{index + 1}",
                                "待判定",
                                f"{pile_labels[cell['pile']]} #{cell['pile_index'] + 1}｜"
                                f"{identity['display_name']}｜卡圖 {float(identity['art_score']):.0%}",
                                "ID 已辨識" if identity["accepted"] else "低信心",
                            ),
                        )
                    self.live_recommendation_var.set(
                        f"已辨識 {result['recognized_count']}/{layout['visible_total']} 張卡牌 ID；"
                        "下一層校準紅色 + 強化標記"
                        if layout["complete"] and not result["unresolved_identity_count"]
                        else "面板或卡牌身分仍不完整，不能寫成權威牌組快照"
                        if layout["complete"]
                        else "面板仍有未顯示卡牌，必須捲動後合併，不能當成完整牌組"
                    )
                    minimum_header_confidence = min(
                        float(item["confidence"]) for item in headers
                    )
                    authoritative = result["authoritative_deck"]
                    snapshot = authoritative["snapshot"]
                    total_cards = sum(
                        int(card["count"]) for card in snapshot["cards"]
                    )
                    self.live_recommendation_var.set(
                        f"完整實際牌庫已同步：{total_cards} 張｜"
                        f"{authoritative['page_count']} 頁；後續不再猜初始牌組"
                    )
                    self.live_confidence_var.set(
                        f"牌堆數量 OCR 最低信心 {minimum_header_confidence:.1%}｜"
                        f"ID 未解 {result['unresolved_identity_count']}｜"
                        f"強化未解 {result['unresolved_upgrade_count']}"
                    )
                    self._set_text(
                        self.live_reason_text,
                        "• 已捲動並合併右下角面板的全部頁面。\n"
                        "• 實際牌庫是後續搜尋的權威基線，不枚舉可能掉落卡。\n"
                        "• 角色專屬卡已依 Master 驗證必須存在；缺少就停止。\n"
                        "• 卡牌 ID／強化只接受達門檻的本機卡圖結果。",
                    )
                    captured = datetime.fromtimestamp(
                        float(capture["timestamp"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(
                        f"最近牌組同步：{captured}｜{capture.get('png_path', '未儲存')}"
                    )
                    self._append_live_log(
                        f"已背景點開牌組、掃描 {authoritative['page_count']} 頁並保存 "
                        f"{total_cards} 張實際牌庫；沒有讀取程序記憶體。"
                    )
                elif kind == "training":
                    result = payload  # type: ignore[assignment]
                    state = result["state"]
                    capture = result["capture"]
                    self._live_suggested_action = SuggestedClick.from_dict(
                        result["action"]
                    )
                    self._live_suggestion_kind = "training"
                    option_rows = "、".join(
                        f"{item['label']} {float(item['match_score']):.0%}"
                        f"{'（已選取）' if item.get('highlighted') else ''}"
                        for item in state["options"]
                    )
                    action = self._live_suggested_action
                    self.live_state_var.set(f"訓練選項：{option_rows}")
                    self.live_recommendation_var.set(f"推薦：{action.label}")
                    self.live_confidence_var.set(
                        "推薦色彩信心 "
                        f"{float(state['recommendation_confidence']):.0%}"
                    )
                    self._set_text(
                        self.live_reason_text,
                        "• 選項位置來自 Maa 的綠幕範本比對；本地化文字區被忽略。\n"
                        "• 推薦屬性來自畫面對話框中的屬性色彩。\n"
                        "• 目前只允許可驗證的單次點擊；需要先選取再確認的選項會保留為建議，不會送出輸入。",
                    )
                    captured = datetime.fromtimestamp(
                        float(capture["timestamp"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(
                        f"最近選項辨識：{captured}｜{capture['width']}×"
                        f"{capture['height']}｜{capture['capture_method']}"
                    )
                    self._append_live_log(
                        f"已辨識訓練選項；待執行建議：{action.label}。"
                    )
                elif kind == "reward":
                    result = payload  # type: ignore[assignment]
                    view = format_reward_preview_workflow(result)
                    workflow = result.get("workflow")
                    if not isinstance(
                        workflow, LiveRewardPreviewWorkflowSession
                    ):
                        raise ValueError("reward workflow session is missing")
                    self._clear_live_suggestion()
                    self._live_reward_workflow_session = (
                        workflow if view.stage == "needs_preview" else None
                    )
                    self.live_reward_button.configure(
                        text=(
                            "繼續獎勵預覽辨識"
                            if view.stage == "needs_preview"
                            else "讀取技能卡獎勵"
                        )
                    )
                    self.live_state_var.set(
                        f"{view.status}\n{view.state}"
                    )
                    self.live_cards_label.set(view.cards_label)
                    self.live_card_tree.delete(
                        *self.live_card_tree.get_children()
                    )
                    for row in view.rows:
                        self.live_card_tree.insert(
                            "",
                            "end",
                            iid=f"reward-{row.slot}",
                            values=(
                                row.card_id,
                                f"+{row.upgrade}",
                                f"槽位 #{row.slot}｜{row.display_name}｜{row.detail}",
                                row.status,
                            ),
                        )
                    self.live_recommendation_var.set(view.recommendation)
                    self.live_confidence_var.set(view.confidence)
                    self._set_text(
                        self.live_reason_text,
                        "\n".join(f"• {reason}" for reason in view.reasons),
                    )
                    capture = view.capture
                    captured = datetime.fromtimestamp(
                        float(capture["timestamp"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(
                        f"最近獎勵辨識：{captured}｜{capture['width']}×"
                        f"{capture['height']}｜"
                        f"{capture.get('capture_method', '外部畫面')}"
                    )
                    if view.stage == "needs_preview":
                        self._append_live_log(
                            "獎勵縮圖仍有歧義；等待外部完成指定槽位的可逆選取後，再讀取新畫面。"
                        )
                    else:
                        self._append_live_log(
                            "獎勵辨識與 Master 排名已完成；未建立確認或領取動作。"
                        )
                elif kind == "shop":
                    result = payload  # type: ignore[assignment]
                    state = result["state"]
                    capture = result["capture"]
                    offers = state["offers"]
                    summary = "、".join(
                        f"{item['display_name']} P{item['price']}" for item in offers
                    )
                    self.live_state_var.set(
                        f"諮詢商店｜持有 P {state['produce_points']}｜{summary}"
                    )
                    weakest_margin = min(
                        float(item["art_margin"]) for item in offers
                    )
                    weakest_price = min(
                        float(item["price_confidence"]) for item in offers
                    )
                    self.live_confidence_var.set(
                        f"卡圖最小領先差 {weakest_margin:.1%}｜"
                        f"價格 OCR 最低信心 {weakest_price:.1%}"
                    )
                    self.live_recommendation_var.set(
                        "商品已對應本地 Master；尚未取得完整牌組前不自動兌換"
                    )
                    self._set_text(
                        self.live_reason_text,
                        "\n".join(
                            f"• 槽位 {item['slot']}：{item['display_name']}｜"
                            f"{item['card_id']}｜P {item['price']}"
                            for item in offers
                        ),
                    )
                    captured = datetime.fromtimestamp(
                        float(capture["timestamp"])
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    self.live_capture_var.set(
                        f"最近商店擷取：{captured}｜{capture['width']}×"
                        f"{capture['height']}｜{capture['capture_method']}"
                    )
                    self._append_live_log(
                        "已用可見畫面與本機靜態卡圖辨識諮詢商品；沒有讀取程序記憶體。"
                    )
                elif kind == "execute":
                    result = payload  # type: ignore[assignment]
                    if isinstance(result, CardPlayExecutionResult):
                        self._handle_verified_card_execution(result)
                    else:
                        changed = bool(result.visual_change_detected)
                        executed_label = str(result.action_label)
                        execution_kind = self._live_suggestion_kind
                        execution_context = self._live_suggestion_context
                        self.live_capture_var.set(
                            "執行後擷取："
                            f"{result.post_capture.get('png_path', '未儲存')}"
                        )
                        self.live_recommendation_var.set(
                            f"已執行：{result.action_label}；"
                            + ("畫面已變化" if changed else "未偵測到明顯變化")
                        )
                        self._append_live_log(
                            f"已送出 {len(result.click_results)} 次背景點擊；"
                            + (
                                "畫面變化僅作一般 UI 操作診斷。"
                                if changed
                                else "沒有明顯變化，安全層不會額外重試。"
                            )
                        )
                        if executed_label.startswith("選取獎勵："):
                            # Selection highlighting can be visually small; a
                            # read-only reanalysis, not the generic frame delta,
                            # proves whether the collect button became enabled.
                            self.after(450, self._read_live_reward)
                        elif (
                            changed
                            and executed_label.startswith("領取獎勵：")
                            and execution_kind == "reward"
                            and isinstance(execution_context, dict)
                            and execution_context.get("stage") == "confirm"
                        ):
                            offer = execution_context.get("offer")
                            if isinstance(offer, dict):
                                checkpoint = checkpoint_confirmed_card_reward(
                                    card_id=str(offer["card_id"]),
                                    upgrade=int(offer["upgrade"]),
                                    display_name=str(offer["display_name"]),
                                    confidence=float(offer["art_score"]),
                                    capture=result.post_capture,
                                )
                                self._append_live_log(
                                    "已把確認領取的卡牌寫入場次紀錄："
                                    f"{checkpoint['run_id']}。"
                                )
                        self._clear_live_suggestion(
                            "一般 UI 操作後必須重新辨識目前畫面。"
                        )
                if kind == "execute" or (
                    kind == "error" and payload[0] == "execute"  # type: ignore[index]
                ):
                    self._restore_after_live_execute()
                self._refresh_execute_button()
        except queue.Empty:
            pass
        self._live_after_id = self.after(75, self._drain_live_queue)

    def _handle_verified_card_execution(
        self, result: CardPlayExecutionResult
    ) -> None:
        suggestion_kind = self._live_suggestion_kind
        context = self._live_suggestion_context
        action = self._live_suggested_action
        if suggestion_kind not in {"lesson", "exam"} or context is None or action is None:
            self._clear_live_suggestion(
                "驗證結果找不到原始 checkpoint，已降級為只分析。"
            )
            return

        predicted = context.get("predicted")
        before = context.get("before")
        samples = result.phase_samples
        active_run, run_paths = self._require_active_live_run()
        checkpoint_writer = None
        if suggestion_kind == "exam":
            current_exam_session = load_exam_session(run_paths.exam_session)
            if (
                current_exam_session is not None
                and not current_exam_session.live_auto_click_blockers()
                and current_exam_session.preflight_binding is not None
                and current_exam_session.bootstrap_consumed_context_id is not None
                and current_exam_session.transition_id is not None
            ):
                def checkpoint_writer(
                    request: TransitionCheckpointWrite,
                ) -> ExamSession:
                    return _save_verified_exam_transition(
                        request,
                        previous=current_exam_session,
                        identity=active_run,
                        produce_id=str(self._live_exam_identity["produce_id"]),
                        step_type=str(self._live_exam_identity["step_type"]),
                        stage_number=int(self._live_exam_identity["stage_number"]),
                        path=run_paths.exam_session,
                    )
        else:
            card_play = context.get("card_play", {})
            clear_target = (
                int(card_play["clear_target"])
                if isinstance(card_play, dict) and "clear_target" in card_play
                else self._live_clear_target
            )
            if clear_target is not None:
                def checkpoint_writer(request: TransitionCheckpointWrite) -> object:
                    return save_lesson_session(
                        request.verified_state, int(clear_target),
                        idol_card_id=active_run.idol_card_id,
                        gimmick_group_id=LIVE_GIMMICK_GROUP_ID,
                        lesson_type=self._live_lesson_type,
                        run_id=active_run.run_id,
                        character_id=active_run.character_id,
                        produce_id=active_run.produce_id,
                        path=run_paths.lesson_session,
                    )
        try:
            outcome = record_and_commit_verified_transition(
                result=result,
                before=before,
                predicted=predicted,
                action=action,
                checkpoint_writer=checkpoint_writer,
                card=context.get("card"),
                context=context.get("context"),
                ids=TransitionReplayIds.from_mapping(
                    context.get("ids", {})  # type: ignore[arg-type]
                ),
                checkpoint={
                    "kind": suggestion_kind,
                    "exam_identity": dict(self._live_exam_identity),
                    "clear_target": self._live_clear_target,
                    "confirmed_before": before,
                },
                replay_root=run_paths.directory / "transition_replays",
            )
        except Exception as error:
            # The game may already have changed, but without a durable replay we
            # deliberately retain the old checkpoint and require fresh analysis.
            self.live_recommendation_var.set(
                "已降級為只分析：差分／重播紀錄保存失敗"
            )
            self._append_live_log(
                f"未提交 shadow state；重播保存失敗：{type(error).__name__}: {error}"
            )
            self._clear_live_suggestion()
            return

        self.live_capture_var.set(
            f"穩定驗證擷取：{result.post_capture.get('png_path', '未儲存')}"
        )
        committed_state = dict(result.verified_state) if outcome.committed else None
        differences = outcome.replay.payload["delta"].get("observed_minus_predicted")
        difference_count = len(differences or [])
        final_confidence = (
            samples[-1].analysis.confidence if samples else 0.0
        )
        if committed_state is None:
            self.live_recommendation_var.set(
                "已降級為只分析："
                + (result.failure_reason or "穩定畫面未通過預測校驗")
            )
            self.live_confidence_var.set(
                f"未提交｜最後畫面信心 {final_confidence:.1%}｜"
                f"差分 {difference_count} 項"
            )
            issues = [
                issue
                for sample in samples
                for issue in sample.analysis.issues
            ]
            self._append_live_log(
                f"卡牌操作未提交；證據已保存：{outcome.replay.json_path}。"
                + ("原因：" + "；".join(issues[-3:]) if issues else "")
            )
            self._clear_live_suggestion()
            return

        if suggestion_kind == "exam":
            self._live_exam_state = dict(committed_state)
            if isinstance(outcome.checkpoint_result, ExamSession):
                self._live_exam_session = outcome.checkpoint_result
            else:
                self._live_exam_session = load_exam_session(
                    run_paths.exam_session
                )
        else:
            self._live_logic_state = dict(committed_state)
            self._live_clear_target = int(clear_target)  # type: ignore[arg-type]

        completed = int(committed_state["turns_remaining"]) == 0
        self.live_confidence_var.set(
            f"已由連續兩張穩定畫面提交｜信心 {final_confidence:.1%}｜"
            f"預測／實值差分 {difference_count} 項"
        )
        self.live_recommendation_var.set(
            f"已驗證：{result.action_label}｜重播 {outcome.replay.record_id}"
        )
        self._append_live_log(
            f"已在兩張相同語義畫面後提交實值；重播：{outcome.replay.json_path}。"
        )
        self._clear_live_suggestion(
            "結果畫面已確認，本場結束。"
            if completed
            else "已提交畫面實值，準備重新分析下一手。"
        )
        if completed and suggestion_kind == "lesson":
            self._show_lesson_completion(committed_state)
            # The result page is terminal evidence, never a hand.  Its parser
            # has no action payload and refuses to update the shadow unless
            # final values, deltas, and trusted pre-result stats all agree.
            self.after(
                50,
                lambda: self._checkpoint_live_pursuit_result(result.post_capture),
            )
        elif completed:
            self.live_state_var.set(
                "本場演出已由連續結果畫面確認完成；培育流程仍繼續。"
            )
        elif suggestion_kind == "exam":
            self.after(350, self._read_live_exam)
        else:
            self.after(350, self._read_live_lesson)

    def _apply_live_view(self, view: LiveView) -> None:
        state = view.state
        if state is None:
            self.live_state_var.set("目前回合狀態：尚無")
        else:
            buffs = "、".join(f"{buff.id}={buff.value}" for buff in state.buffs) or "無"
            self.live_state_var.set(f"第 {state.turn} 回合｜參數 {state.clear.current_parameter}/{state.clear.threshold}｜體力 {state.stamina}｜Buff：{buffs}")
        self.live_cards_label.set(view.cards_label)
        self.live_card_tree.delete(*self.live_card_tree.get_children())
        for index, card in enumerate(view.cards):
            upgrade = f"+{card.upgrade}" if card.upgrade is not None else "未判定"
            self.live_card_tree.insert("", "end", iid=str(index), values=(card.card_id, upgrade, card.label, card.legal))
        self.live_recommendation_var.set(f"推薦：{view.recommendation}")
        self.live_confidence_var.set(f"信心：{view.confidence}")
        self._set_text(self.live_reason_text, "\n".join(f"• {reason}" for reason in view.reasons))
        confidence = None
        try:
            normalized = str(view.confidence).strip().rstrip("%")
            parsed = float(normalized)
            confidence = parsed / 100.0 if parsed > 1.0 else parsed
        except ValueError:
            pass
        self._set_recommendation_source(
            "rules",
            {
                "action_label": str(view.recommendation or "尚無可用建議"),
                "confidence": confidence,
                "reason": "\n".join(str(reason) for reason in view.reasons),
                "status": "available",
                "legal": any(str(card.legal).casefold() in {"true", "yes", "合法"} for card in view.cards),
                "executable": self._live_suggested_action is not None,
            },
        )
        if state is not None:
            self._set_text(
                self.live_state_summary_text,
                self.live_state_var.get().replace("｜", "\n"),
            )

    def _append_live_log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.live_log_text.configure(state="normal")
        self.live_log_text.insert("end", f"[{timestamp}] {message}\n")
        self.live_log_text.see("end")
        self.live_log_text.configure(state="disabled")

    def _show_lesson_completion(
        self, logic_state: dict[str, object]
    ) -> None:
        score = int(logic_state.get("score", 0))
        targets = load_lesson_score_targets()
        result = calculate_pursuit_lesson_result(score, targets)
        self._clear_live_suggestion()
        self.live_state_var.set(
            f"課程完成｜結果 {result.capped_score}/{targets.perfect}｜"
            f"{result.tier}｜衝刺 {result.pursuit_bonus_total}"
        )
        self.live_cards_label.set("本堂追い込み課程已完成")
        self.live_card_tree.delete(*self.live_card_tree.get_children())
        self.live_recommendation_var.set(
            f"完成：Vo／Da／Vi 各 +{result.parameter_gain_each}"
        )
        self.live_confidence_var.set(
            "結果依已驗證出牌轉移＋本機 Master 門檻計算"
        )
        reason_lines = [
            f"• CLEAR {targets.clear}；PERFECT／上限 {targets.perfect}。",
            f"• 衝刺值 = min({score}, {targets.perfect}) - "
            f"{targets.clear} = {result.pursuit_bonus_total}。",
            f"• 追い込み獎勵平均分到三項能力：各 +"
            f"{result.parameter_gain_each}。",
        ]
        if not result.split_is_exact:
            reason_lines.append(
                f"• 尚有 {result.undistributed_remainder} 點整數餘數；"
                "未經實機驗證前不猜分配順序。"
            )
        self._set_text(self.live_reason_text, "\n".join(reason_lines))
        self._append_live_log(
            f"課程完成：{result.tier}，結果 {result.capped_score}/"
            f"{targets.perfect}，衝刺 {result.pursuit_bonus_total}，"
            f"三項能力各 +{result.parameter_gain_each}。"
        )

    def _clear_live_suggestion(self, reason: str | None = None) -> None:
        had_action = self._live_suggested_action is not None
        self._live_suggested_action = None
        self._live_suggestion_kind = None
        self._live_suggested_next_state = None
        self._live_suggestion_context = None
        self._live_safety_blockers = ()
        self.live_execute_var.set("執行目前建議（等待辨識）")
        if hasattr(self, "live_recommendation_tree"):
            for source in self._recommendation_sources:
                self._recommendation_sources[source] = None
            self._render_recommendation_rows()
        if had_action and reason:
            self._append_live_log(reason)
        self._refresh_execute_button()

    def _refresh_execute_button(self) -> None:
        action = self._live_suggested_action
        if action is None:
            self.live_execute_var.set("執行目前建議（等待辨識）")
            self.live_execute_button.configure(state="disabled")
            if self.live_execute_button.winfo_manager():
                self.live_execute_button.pack_forget()
            return
        if not self.live_execute_button.winfo_manager():
            self.live_execute_button.pack(side="right", padx=(12, 0))
        if action.click_count != 1:
            self.live_execute_var.set(
                f"僅提供建議：{action.label}（尚無可驗證的多次點擊流程）"
            )
            self.live_execute_button.configure(state="disabled")
            return
        suffix = (
            "（SELECT＋雙穩定畫面驗證）"
            if self._live_suggestion_kind in {"lesson", "exam"}
            else ""
        )
        self.live_execute_var.set(f"執行目前建議：{action.label}{suffix}")
        enabled = _live_execution_enabled(
            click_available=self._controller_click_available,
            busy=self._live_busy,
            safety_blockers=self._live_safety_blockers,
            click_count=action.click_count,
        )
        self.live_execute_button.configure(state="normal" if enabled else "disabled")

    def _restore_after_live_execute(self) -> None:
        if not self._live_window_hidden_for_execute:
            return
        self._live_window_hidden_for_execute = False
        self.deiconify()
        self.lift()

    def _on_close(self) -> None:
        for name in ("card_library_panel", "card_update_panel", "model_update_panel"):
            panel = getattr(self, name, None)
            if panel is not None:
                panel.shutdown()
        if self._live_after_id is not None:
            self.after_cancel(self._live_after_id)
        if self._plan3_advisor_after_id is not None:
            self.after_cancel(self._plan3_advisor_after_id)
        if self._console_live_after_id is not None:
            self.after_cancel(self._console_live_after_id)
        if self._console_live_controller.view.phase == InitialRegularSimulatorPhase.RUNNING:
            self._console_live_controller.stop()
        self.destroy()

    def _build_run_tab(self) -> None:
        self.profile_choice = self.live_profile_choice
        self.card_library_panel = CardLibraryPanel(
            self.notebook, names=self.profile_label_by_id,
            selection_scope=lambda: (
                self.console_mode_var.get(),
                "" if self._selected_profile() is None else self._selected_profile().id,
            ),
        )
        self.notebook.add(self.card_library_panel, text=CONSOLE_PAGE_TITLES[1])

    def _build_history_tab(self, parent_notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(parent_notebook, padding=12)
        parent_notebook.add(tab, text="培育紀錄")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        self.profile_choice = self.live_profile_choice
        self.run_deck_var = tk.StringVar(value="牌庫尚未同步")

        heading = ttk.Frame(tab)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            heading,
            text="目前場次的牌庫、持有物與歷史紀錄",
            font=("Yu Gothic UI", 13, "bold"),
        ).pack(side="left")
        ttk.Button(
            heading,
            text="重新整理",
            command=self._refresh_cultivation_overview,
        ).pack(side="right")

        shadow_box = ttk.LabelFrame(tab, text="培育紀錄", padding=10)
        shadow_box.grid(row=1, column=0, sticky="nsew")
        shadow_box.columnconfigure(0, weight=1)
        shadow_box.columnconfigure(1, weight=2)
        shadow_box.columnconfigure(2, weight=2)
        shadow_box.rowconfigure(0, weight=1)
        self.run_shadow_summary_text = tk.Text(
            shadow_box, width=30, height=10, wrap="word", state="disabled"
        )
        self.run_shadow_summary_text.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.run_deck_tree = ttk.Treeview(
            shadow_box,
            columns=("card", "upgrade", "count"),
            show="headings",
            height=9,
        )
        for key, label in zip(
            ("card", "upgrade", "count"),
            ("卡牌 ID", "強化", "數量"),
        ):
            self.run_deck_tree.heading(key, text=label)
        self.run_deck_tree.column("card", width=260)
        self.run_deck_tree.column("upgrade", width=60, anchor="center")
        self.run_deck_tree.column("count", width=55, anchor="center")
        self.run_deck_tree.grid(row=0, column=1, sticky="nsew", padx=6)
        self.run_history_tree = ttk.Treeview(
            shadow_box,
            columns=("kind", "detail"),
            show="headings",
            height=9,
        )
        self.run_history_tree.heading("kind", text="歷史類型")
        self.run_history_tree.heading("detail", text="內容／證據")
        self.run_history_tree.column("kind", width=130)
        self.run_history_tree.column("detail", width=300)
        self.run_history_tree.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self._refresh_cultivation_overview()

    def _selected_profile(self) -> IdolProfile | None:
        return self.profile_by_label.get(self.profile_selection_var.get())

    def _set_run_values(self, values: dict[str, int]) -> None:
        for key, value in values.items():
            self.run_vars[key].set(str(value))

    def _set_mode_deck_summary(self, profile: IdolProfile) -> None:
        deck = get_mode_initial_deck(
            self.console_mode_var.get(), profile.exam_effect_type
        )
        if deck is None:
            self.run_deck_var.set("模式初始牌組：找不到 Master 對應")
            return
        counts = Counter(card.name for card in deck.cards)
        summary = "、".join(
            f"{name}×{count}" if count > 1 else name
            for name, count in counts.items()
        )
        self.run_deck_var.set(
            f"模式初始牌組（{len(deck.cards)} 張；不含偶像專屬牌）：{summary}"
        )

    def _apply_selected_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            messagebox.showwarning(
                "沒有 Master 資料",
                "請先執行 master_import 建立本地資料庫。",
                parent=self,
            )
            return
        self._set_run_values(
            {
                "stamina": profile.stamina,
                "max_stamina": profile.stamina,
                "vocal": profile.vocal,
                "dance": profile.dance,
                "visual": profile.visual,
                "vocal_growth": profile.vocal_growth,
                "dance_growth": profile.dance_growth,
                "visual_growth": profile.visual_growth,
            }
        )
        self._set_mode_deck_summary(profile)
        self._refresh_route_calendar()
        self._analyze_run()
        self.status_var.set(
            f"已套用 {profile.name}（{profile.id}）的 Master 初始值與成長率。"
        )

    def _start_selected_live_run(self) -> None:
        """Explicitly rotate to a fresh run without overwriting the prior one."""

        if terminal_bookkeeping_pending(self._console_live_controller.view):
            self.status_var.set(self._console_live_controller.view.stop_reason_text)
            return
        profile = self._selected_profile()
        if profile is None:
            messagebox.showwarning(
                "缺少偶像卡資料",
                "請先選擇一張偶像卡。",
                parent=self,
            )
            return
        produce_id = self.console_mode_var.get()
        identity = create_run(
            idol_card_id=profile.id,
            character_id=profile.character_id,
            produce_id=produce_id,
            evidence={
                "source": "gui-explicit-selection",
                "profile_label": profile.label,
            },
        )
        self._active_run = identity
        self._run_paths = paths_for(identity)
        self._live_idol_card_id = identity.idol_card_id
        self._live_produce_id = identity.produce_id
        self._live_logic_state = None
        self._live_clear_target = None
        self._live_exam_state = None
        self._live_exam_identity = {
            "produce_id": identity.produce_id,
            "step_type": MID1,
            "stage_number": 1,
        }
        self._live_route_position = None
        self._live_safety_blockers = ()
        self._live_reward_workflow_session = None
        self.live_reward_button.configure(text="讀取技能卡獎勵")
        self._clear_live_suggestion("已切換到新的培育場次，請重新讀取畫面。")
        self.live_route_var.set(
            f"目前場次：{profile.name}｜{self._mode_label(identity.produce_id)}"
        )
        self.status_var.set(
            "已建立新的實機培育場次；舊場次紀錄仍保留在自己的資料夾。"
        )
        self._refresh_console_identity()
        self._refresh_cultivation_overview()

    def _load_live_sample(self) -> None:
        profile = next(
            (profile for profile in self.idol_profiles if profile.id == LIVE_IDOL_CARD_ID),
            None,
        )
        values = dict(LIVE_RUN_VALUES)
        if profile is not None:
            values.update(
                {
                    "vocal_growth": profile.vocal_growth,
                    "dance_growth": profile.dance_growth,
                    "visual_growth": profile.visual_growth,
                }
            )
            self.profile_choice.set(
                self.profile_label_by_id.get(profile.id, profile.label)
            )
            self._set_mode_deck_summary(profile)
            self._refresh_route_calendar()
        self._set_run_values(values)
        self._analyze_run()
        self.status_var.set(
            "已載入 2026-07-28 畫面樣本（剩餘 5 週）；格數未由畫面強行推算。"
        )

    def _build_advanced_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text=CONSOLE_PAGE_TITLES[2])
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Label(
            tab,
            text=(
                "模型選擇會在下一場生效。卡片資料更新與培育紀錄集中在這裡。"
            ),
            style="Hint.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        advanced_notebook = ttk.Notebook(tab)
        advanced_notebook.grid(row=1, column=0, sticky="nsew")
        self._build_model_evaluation_tab(advanced_notebook)
        self.card_update_panel = CardDataUpdatePanel(advanced_notebook)
        advanced_notebook.add(self.card_update_panel, text="卡片資料更新")
        self.model_update_panel = ModelUpdatePanel(advanced_notebook, on_candidate_ready=self._refresh_model_evaluation_page)
        advanced_notebook.add(self.model_update_panel, text="建立新版模型")
        self._build_history_tab(advanced_notebook)
        ttk.Button(tab, text="開啟診斷工具", command=self._open_diagnostic_tools).grid(
            row=2, column=0, sticky="e", pady=(8, 0),
        )

    def _open_diagnostic_tools(self) -> None:
        existing = getattr(self, "_diagnostic_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            return
        window = tk.Toplevel(self)
        self._diagnostic_window = window
        window.title("培育助手 · 診斷工具")
        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=12, pady=12)
        self._build_manual_tools_tab(notebook)
        def close() -> None:
            self.advanced_manual_buttons = []
            self._diagnostic_window = None
            window.destroy()
        window.protocol("WM_DELETE_WINDOW", close)
        if self._console_live_controller.view.phase in {
            InitialRegularSimulatorPhase.RUNNING, InitialRegularSimulatorPhase.CANCELLING,
        }:
            for button in self.advanced_manual_buttons:
                button.configure(state="disabled")

    def _build_manual_tools_tab(self, parent_notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(parent_notebook, padding=14)
        parent_notebook.add(tab, text="手動工具")
        ttk.Label(
            tab,
            text=(
                "這些工具只供除錯或特定畫面使用。一般自動培育不需要操作；"
                "autopilot 執行中會全部停用。"
            ),
            style="Hint.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(tab)
        buttons.pack(anchor="w")
        actions = (
            ("重新檢查連線", lambda: self._run_live_worker("status", controller_status)),
            ("載入校正樣本", self._load_live_calibration),
            ("讀取培育總覽", self._read_live_overview),
            ("分析課程畫面", self._read_live_lesson),
            ("分析演出畫面", self._read_live_exam),
            ("辨識訓練選項", self._read_live_training_choice),
            ("讀取卡牌獎勵", self._read_live_reward),
            ("同步演出牌組", self._sync_live_deck_panel),
            ("讀取商店（實驗）", self._read_live_shop),
        )
        self.advanced_manual_buttons: list[ttk.Button] = []
        for index, (label, command) in enumerate(actions):
            button = ttk.Button(buttons, text=label, command=command)
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=4, pady=4)
            self.advanced_manual_buttons.append(button)

    def _build_model_evaluation_tab(self, parent_notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(parent_notebook, padding=12)
        parent_notebook.add(tab, text="模型評估")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        controls = ttk.Frame(tab)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="模型包").pack(side="left")
        self.console_bundle_var = tk.StringVar()
        self.console_bundle_choice = ttk.Combobox(
            controls,
            textvariable=self.console_bundle_var,
            state="readonly",
            width=42,
        )
        self.console_bundle_choice.pack(side="left", padx=(8, 6))
        self.console_bundle_choice.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_model_evaluation_page()
        )
        ttk.Button(
            controls,
            text="重新整理",
            command=self._refresh_model_evaluation_page,
        ).pack(side="left")
        ttk.Button(
            controls,
            text="下一場選用此模型包",
            command=self._activate_console_bundle,
        ).pack(side="left", padx=(6, 0))
        self.console_bundle_status_var = tk.StringVar(value="")
        ttk.Label(controls, textvariable=self.console_bundle_status_var).pack(
            side="right"
        )

        content = ttk.Frame(tab)
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)
        self.console_model_tree = ttk.Treeview(
            content,
            columns=("role", "quality", "shadow", "apply", "validation", "test", "delta"),
            show="headings",
            height=12,
        )
        for key, label in zip(
            ("role", "quality", "shadow", "apply", "validation", "test", "delta"),
            ("策略", "檢查結果", "旁觀評估", "正式使用", "驗證吻合率", "測試吻合率", "相對基準"),
        ):
            self.console_model_tree.heading(key, text=label)
        self.console_model_tree.column("role", width=190)
        self.console_model_tree.column("quality", width=180)
        for key in ("shadow", "apply", "validation", "test", "delta"):
            self.console_model_tree.column(key, width=95, anchor="center")
        self.console_model_tree.grid(row=0, column=0, sticky="nsew")
        self.console_model_detail = tk.Text(
            content, height=10, wrap="word", state="disabled"
        )
        self.console_model_detail.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._refresh_model_evaluation_page()

    def _refresh_model_evaluation_page(self) -> None:
        views = discover_policy_dashboard_views()
        self._console_bundle_views = {value.bundle_id: value for value in views}
        labels = tuple(self._console_bundle_views)
        self.console_bundle_choice.configure(values=labels)
        selected = self.console_bundle_var.get()
        if selected not in self._console_bundle_views:
            selected = next(
                (value.bundle_id for value in views if value.active),
                labels[0] if labels else "",
            )
            self.console_bundle_var.set(selected)
        self.console_model_tree.delete(*self.console_model_tree.get_children())
        if not selected:
            self.console_bundle_status_var.set("尚無模型包")
            self._set_text(self.console_model_detail, "尚無模型資料。")
            return
        view = self._console_bundle_views[selected]
        role_labels = {
            "exact_exam_policy": "演出 BC", "offline_rl_policy": "演出 RL",
            "outer_policy": "培育 BC", "replay_syntax_prior": "回放先驗",
        }
        quality_labels = {
            "accepted-for-shadow": "已通過模型檢查", "accepted-for-learned-chain": "已通過策略驗證",
            "accepted": "已通過檢查", "diagnostic-only": "僅供診斷",
        }
        metric_by_role = {value.role: value for value in view.metrics}
        for index, component in enumerate(view.components):
            metric = metric_by_role.get(component.role)
            def percent(value: float | None) -> str:
                return "—" if value is None else f"{value * 100:.1f}%"
            delta = (
                "—"
                if metric is None or metric.improvement_pp is None
                else f"{metric.improvement_pp:+.2f}pp"
            )
            self.console_model_tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    role_labels.get(component.role, component.role),
                    quality_labels.get(component.quality, component.quality),
                    "是" if component.shadow_ready else "否",
                    "是" if component.live_apply_allowed else "否",
                    percent(None if metric is None else metric.validation_top1),
                    percent(None if metric is None else metric.test_top1),
                    delta,
                ),
            )
        self.console_bundle_status_var.set(
            "目前選用" if view.active else "未選用"
        )
        roles = {component.role for component in view.components if component.live_apply_allowed}
        exam_policy = "RL → BC" if "offline_rl_policy" in roles else "BC" if "exact_exam_policy" in roles else "既有策略"
        outer_policy = "BC" if "outer_policy" in roles else "既有培育策略"
        self._set_text(
            self.console_model_detail,
            f"模型：{view.bundle_id}\n"
            f"訓練資料：{view.exact_stage_count} 個完整演出／"
            f"{view.exact_transition_count} 筆動作轉移\n\n"
            f"此模型的演出策略：{exam_policy}／培育策略：{outer_policy}\n\n"
            "表格顯示模型與資料中動作的吻合率。\n"
            "目前評等的換算會在培育頁依已讀能力與票數顯示。\n"
            "模型選擇在新的培育或演出階段載入，當前動作保持原模型。",
        )

    def _activate_console_bundle(self) -> None:
        view = self._console_bundle_views.get(self.console_bundle_var.get())
        if view is None:
            return
        try:
            activate_policy_bundle(Path(view.manifest_path))
        except Exception as error:
            messagebox.showerror(
                "模型包切換失敗", f"{type(error).__name__}: {error}", parent=self
            )
            return
        self._refresh_model_evaluation_page()
        self._refresh_live_policy_status()
        self.status_var.set(
            "已選擇模型包；只會在下一場培育或下一個 Exam stage 載入。"
        )

    def _build_route_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="路線日曆")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        controls = ttk.Frame(tab)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="模式").pack(side="left")
        profile = self._selected_profile()
        character_id = None if profile is None else profile.character_id
        self.route_mode_by_label: dict[str, str] = {}
        for produce_id in supported_produce_ids():
            calendar = load_route_calendar(
                produce_id, character_id=character_id
            )
            self.route_mode_by_label[
                f"{calendar.label}（{produce_id}）"
            ] = produce_id
        self.route_mode_choice = ttk.Combobox(
            controls,
            values=list(self.route_mode_by_label),
            state="readonly",
            width=36,
        )
        self.route_mode_choice.pack(side="left", padx=(8, 0))
        self.route_mode_choice.current(0)
        self.route_mode_choice.bind(
            "<<ComboboxSelected>>", lambda _event: self._refresh_route_calendar()
        )
        ttk.Button(
            controls,
            text="依目前偶像卡重新整理",
            command=self._refresh_route_calendar,
        ).pack(side="left", padx=(8, 0))

        table_box = ttk.LabelFrame(tab, text="逐週行程", padding=10)
        table_box.grid(row=1, column=0, sticky="nsew")
        table_box.columnconfigure(0, weight=1)
        table_box.rowconfigure(0, weight=1)
        self.route_tree = ttk.Treeview(
            table_box,
            columns=("week", "actions", "stage", "certainty"),
            show="headings",
            height=18,
        )
        for key, label in zip(
            ("week", "actions", "stage", "certainty"),
            ("週次", "可選行動", "演出節點", "資料層級"),
        ):
            self.route_tree.heading(key, text=label)
        self.route_tree.column("week", width=65, anchor="center")
        self.route_tree.column("actions", width=430)
        self.route_tree.column("stage", width=180)
        self.route_tree.column("certainty", width=160)
        scrollbar = ttk.Scrollbar(
            table_box, orient="vertical", command=self.route_tree.yview
        )
        self.route_tree.configure(yscrollcommand=scrollbar.set)
        self.route_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.route_tree.tag_configure("current", background="#fff0b3")
        self.route_tree.tag_configure("audition", background="#e8ddff")

        self.route_summary_var = tk.StringVar(value="")
        ttk.Label(
            tab,
            textvariable=self.route_summary_var,
            justify="left",
            wraplength=1040,
        ).grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._refresh_route_calendar()

    def _refresh_route_calendar(self) -> None:
        if not hasattr(self, "route_tree"):
            return
        produce_id = self.route_mode_by_label.get(
            self.route_mode_choice.get(), "produce-001"
        )
        profile = self._selected_profile()
        character_id = None if profile is None else profile.character_id
        calendar = load_route_calendar(
            produce_id, character_id=character_id
        )
        self.route_tree.delete(*self.route_tree.get_children())
        current_week = (
            self._live_route_position.current_week
            if self._live_route_position is not None
            and produce_id == "produce-001"
            else None
        )
        for route_week in calendar.weeks:
            stage = (
                "" if route_week.stage_type is None else route_week.display
            )
            actions = "—" if route_week.stage_type is not None else route_week.display
            tags: tuple[str, ...] = ()
            if route_week.week == current_week:
                tags = ("current",)
            elif route_week.stage_type is not None:
                tags = ("audition",)
            self.route_tree.insert(
                "",
                "end",
                iid=str(route_week.week),
                values=(
                    route_week.week,
                    actions,
                    stage,
                    "逐週表" if route_week.exact_actions else "僅 Master 節點",
                ),
                tags=tags,
            )
        if current_week is not None:
            self.route_tree.selection_set(str(current_week))
            self.route_tree.see(str(current_week))
        self.route_summary_var.set(
            f"{calendar.label}｜共 {calendar.total_weeks} 週｜"
            f"{milestone_summary(calendar)}\n{calendar.source_note}。"
            "可見畫面永遠優先；路線表不會單獨授權自動點擊。"
        )

    def _build_exam_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="考試出牌")
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(tab, text="局面與手牌", padding=12)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        self.exam_vars = {
            "turns": tk.StringVar(value="5"),
            "stamina": tk.StringVar(value="20"),
            "block": tk.StringVar(value="0"),
            "favorable": tk.StringVar(value="0"),
            "score": tk.StringVar(value="0"),
        }
        labels = [
            ("turns", "剩餘回合", 20),
            ("stamina", "體力", 999),
            ("block", "元氣", 999),
            ("favorable", "好調回合", 99),
            ("score", "目前分數", 9999999),
        ]
        for row, (key, label, upper) in enumerate(labels):
            self._labeled_spinbox(left, row, label, self.exam_vars[key], upper)

        ttk.Label(left, text="目前手牌").grid(
            row=len(labels), column=0, columnspan=2, sticky="w", pady=(12, 4)
        )
        self.hand_list = tk.Listbox(left, height=8, width=34, exportselection=False)
        self.hand_list.grid(row=len(labels) + 1, column=0, columnspan=2, sticky="ew")
        self._refresh_hand()

        choice_values = [f"{card.name} | {card.id}" for card in card_choices()]
        self.card_choice = ttk.Combobox(left, values=choice_values, state="readonly", width=31)
        self.card_choice.current(0)
        self.card_choice.grid(row=len(labels) + 2, column=0, columnspan=2, sticky="ew", pady=(8, 4))
        buttons = ttk.Frame(left)
        buttons.grid(row=len(labels) + 3, column=0, columnspan=2, sticky="ew")
        ttk.Button(buttons, text="加入", command=self._add_card).pack(side="left", expand=True, fill="x")
        ttk.Button(buttons, text="移除", command=self._remove_card).pack(
            side="left", expand=True, fill="x", padx=(6, 0)
        )
        ttk.Button(left, text="分析出牌", command=self._analyze_exam).grid(
            row=len(labels) + 4, column=0, columnspan=2, sticky="ew", pady=(12, 0)
        )

        result = ttk.LabelFrame(tab, text="出牌建議", padding=10)
        result.grid(row=0, column=1, sticky="nsew")
        result.columnconfigure(0, weight=1)
        result.rowconfigure(0, weight=1)
        columns = ("rank", "card", "utility", "gain", "cost", "after")
        self.exam_tree = ttk.Treeview(result, columns=columns, show="headings", height=13)
        for key, text in zip(columns, ("#", "卡牌", "效用", "得分", "體力成本", "使用後體力")):
            self.exam_tree.heading(key, text=text)
        self.exam_tree.column("rank", width=38, anchor="center")
        self.exam_tree.column("card", width=190)
        for key in ("utility", "gain", "cost", "after"):
            self.exam_tree.column(key, width=82, anchor="e")
        self.exam_tree.grid(row=0, column=0, sticky="nsew")
        self.exam_tree.bind("<<TreeviewSelect>>", self._show_exam_detail)
        self.exam_detail = tk.Text(result, height=10, wrap="word", state="disabled")
        self.exam_detail.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self._analyze_exam()

    def _build_event_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="事件資料")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        query = ttk.LabelFrame(tab, text="ADV 資源反查", padding=12)
        query.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        query.columnconfigure(1, weight=1)
        ttk.Label(query, text="ADV asset ID").grid(row=0, column=0, sticky="w")
        self.event_adv_var = tk.StringVar(value="adv_cidol-kllj-3-010_03")
        ttk.Entry(query, textvariable=self.event_adv_var).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(query, text="分析事件", command=self._analyze_event).grid(
            row=0, column=2
        )

        result = ttk.LabelFrame(tab, text="Master 關聯與數值分支", padding=10)
        result.grid(row=1, column=0, sticky="nsew")
        result.columnconfigure(0, weight=1)
        result.rowconfigure(0, weight=1)
        self.event_detail = tk.Text(result, wrap="word", state="disabled")
        self.event_detail.grid(row=0, column=0, sticky="nsew")
        self._analyze_event()

    def _analyze_event(self) -> None:
        adv_asset_id = self.event_adv_var.get().strip()
        try:
            analysis = analyze_event_by_adv_asset(adv_asset_id)
        except Exception as exc:
            self._set_text(
                self.event_detail,
                f"事件資料庫尚未就緒：{exc}\n\n請重新執行 master_import --force。",
            )
            return
        if analysis is None:
            self._set_text(self.event_detail, f"找不到 ADV asset：{adv_asset_id}")
            return

        lines = [
            f"ADV：{analysis.adv_asset_id}",
            f"Story：{analysis.title}  [{analysis.story_id}]",
            f"Event detail：{analysis.detail_id or '無'}",
            f"Event type：{analysis.event_type or '無'}",
            "",
        ]
        if not analysis.has_numeric_branch:
            lines.extend(
                (
                    "判定：純劇情事件。",
                    "Master 沒有數值效果或培育選項；畫面上的對話選句不用納入最佳化。",
                )
            )
        else:
            if analysis.effects:
                lines.append("事件固定效果：")
                lines.extend(f"• {effect.describe()}  [{effect.id}]" for effect in analysis.effects)
                lines.append("")
            for index, suggestion in enumerate(analysis.suggestions, 1):
                lines.append(f"選項 {index}：{suggestion.id}")
                if suggestion.descriptions:
                    lines.extend(f"  {text}" for text in suggestion.descriptions)
                lines.append(
                    f"  P 點 {suggestion.produce_point:+d}；體力 {suggestion.stamina:+d}；"
                    f"卡牌 {suggestion.produce_card_id or '無'}"
                )
                if suggestion.always_successful:
                    lines.append("  必定成功")
                elif suggestion.success_probability_permyriad:
                    probability = suggestion.success_probability_permyriad / 100
                    lines.append(f"  成功率 {probability:.2f}%")
                for label, effects in (
                    ("效果", suggestion.effects),
                    ("成功", suggestion.success_effects),
                    ("失敗", suggestion.fail_effects),
                ):
                    lines.extend(f"  {label}：{effect.describe()}" for effect in effects)
                lines.append("")
        self._set_text(self.event_detail, "\n".join(lines))

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def _read_ints(self, variables: dict[str, tk.StringVar]) -> dict[str, int]:
        try:
            return {key: int(value.get()) for key, value in variables.items()}
        except ValueError as exc:
            raise ValueError("所有局面欄位都必須是整數。") from exc

    def _analyze_run(self) -> None:
        try:
            values = self._read_ints(self.run_vars)
            state = RunState(total_steps=13, **values)
            self._run_recommendations = recommend_run_actions(state)
        except ValueError as exc:
            messagebox.showerror("輸入錯誤", str(exc), parent=self)
            return
        self.run_tree.delete(*self.run_tree.get_children())
        for rank, item in enumerate(self._run_recommendations, 1):
            self.run_tree.insert("", "end", iid=str(rank - 1), values=(rank, item.action, f"{item.utility:.1f}"))
        if self._run_recommendations:
            self.run_tree.selection_set("0")
            self._show_run_detail()
        self.status_var.set("已更新培育方向；目前仍是可解釋 heuristic，不是最終精確公式。")

    def _show_run_detail(self, _event: object | None = None) -> None:
        selection = self.run_tree.selection()
        if not selection:
            return
        item = self._run_recommendations[int(selection[0])]
        self._set_text(
            self.run_detail,
            f"{item.action}\n\n" + "\n".join(f"• {reason}" for reason in item.reasons),
        )

    def _refresh_hand(self) -> None:
        self.hand_list.delete(0, "end")
        for card_id in self.hand_ids:
            card = get_card(card_id)
            self.hand_list.insert("end", f"{card.name}  [{card.id}]")

    def _add_card(self) -> None:
        value = self.card_choice.get()
        if not value:
            return
        self.hand_ids.append(value.rsplit(" | ", 1)[1])
        self._refresh_hand()

    def _remove_card(self) -> None:
        selection = self.hand_list.curselection()
        if not selection:
            return
        del self.hand_ids[selection[0]]
        self._refresh_hand()

    def _analyze_exam(self) -> None:
        if not self.hand_ids:
            messagebox.showwarning("沒有手牌", "請至少加入一張手牌。", parent=self)
            return
        try:
            values = self._read_ints(self.exam_vars)
            state = ExamState(
                turns_remaining=values["turns"],
                stamina=values["stamina"],
                block=values["block"],
                favorable_turns=values["favorable"],
                score=values["score"],
            )
            self._exam_recommendations = recommend_cards(
                state, [get_card(card_id) for card_id in self.hand_ids]
            )
        except ValueError as exc:
            messagebox.showerror("輸入錯誤", str(exc), parent=self)
            return
        self.exam_tree.delete(*self.exam_tree.get_children())
        for rank, item in enumerate(self._exam_recommendations, 1):
            transition = item.transition
            utility = "不可用" if not transition.legal else f"{item.utility:.1f}"
            after_stamina = "—" if not transition.legal else str(transition.after.stamina)
            self.exam_tree.insert(
                "",
                "end",
                iid=str(rank - 1),
                values=(rank, item.card.name, utility, transition.score_gain, transition.stamina_paid, after_stamina),
            )
        if self._exam_recommendations:
            self.exam_tree.selection_set("0")
            self._show_exam_detail()
        self.status_var.set("已完成單回合分析；尚未展開隨機抽牌與多回合搜尋。")

    def _show_exam_detail(self, _event: object | None = None) -> None:
        selection = self.exam_tree.selection()
        if not selection:
            return
        item = self._exam_recommendations[int(selection[0])]
        header = f"{item.card.name}\n{item.card.id}\nMaster 摘要：{item.card.source_note}\n"
        body = "\n".join(f"• {reason}" for reason in item.reasons)
        self._set_text(self.exam_detail, header + "\n" + body)


def main() -> int:
    # Keep already-installed console scripts on the new default interface.
    from .glass_gui.launcher import main as glass_main
    return glass_main()


if __name__ == "__main__":
    raise SystemExit(main())
