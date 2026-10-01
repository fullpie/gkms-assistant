"""Same-produce Live observation and its owned normal skip-only ADV control."""
from __future__ import annotations

from collections.abc import Mapping
import math

from .runtime_command_client import RuntimeCommandError


def _owned_story_target(raw):
    actions = raw.get("legal_actions")
    if actions == [] and raw.get("actions_complete") is False:
        return None
    if not isinstance(actions, list) or len(actions) != 1 or raw.get("actions_complete") is not True:
        raise RuntimeCommandError("native Live story action coverage is ambiguous")
    ui = raw.get("ui_state") or {}
    story = ui.get("story") or {}
    action = actions[0]
    target = action.get("target") or {}
    button = story.get("skip_only_button") or {}
    players = ui.get("live_story_players")
    if (action.get("action_id") != "story.skip" or target.get("action_id") != "story.skip"
            or target.get("button_source") != "player-skip-only" or target.get("owner_type") != "LiveScenePresenter"
            or target.get("live_story_field") not in {"_beforeLiveStoryPlayer", "_afterLiveStoryPlayer"}
            or raw.get("busy") is not False or (raw.get("pointer_blocking") or {}).get("input_ready") is not True
            or any(story.get(key) is not True for key in ("data_ready", "engine_active", "story_run_active", "skip_only_ready", "skip_only_mode"))
            or story.get("skip_permission_callback_instance_id") != "0x0"
            or any(button.get(key) is not True for key in ("active", "enabled", "has_callback"))
            or button.get("disabled") is not False or not isinstance(players, list)):
        raise RuntimeCommandError("native Live skip-only story control is unqualified")
    for key in ("owner_instance_id", "adv_instance_id", "live_story_field", "live_story_player_instance_id"):
        if not isinstance(target.get(key), str) or target[key] in ("", "0x0") or target[key] != story.get(key):
            raise RuntimeCommandError("native Live story owner differs")
    for key in ("button_instance_id", "wait_callback_instance_id"):
        if not isinstance(target.get(key), str) or target[key] in ("", "0x0") or target[key] != button.get(key):
            raise RuntimeCommandError("native Live story callback differs")
    rows = [row for row in players if isinstance(row, Mapping) and row.get("field") == target["live_story_field"]]
    if (len(rows) != 1 or rows[0].get("player_instance_id") != target["live_story_player_instance_id"]
            or rows[0].get("adv_instance_id") != target["adv_instance_id"]
            or rows[0].get("active") is not True or rows[0].get("story_run_active") is not True
            or rows[0].get("player_is_end") is not False):
        raise RuntimeCommandError("native Live story player is not the current owned player")
    return target


def native_presentation_wait(native, *, produce_id, idol_card_id):
    raw = native.raw if hasattr(native, "raw") else native
    if not isinstance(raw, Mapping) or raw.get("surface") != "presentation":
        return None
    state, progress, ui = (raw.get(key) for key in ("state", "progress", "ui_state"))
    if not all(isinstance(value, Mapping) for value in (state, progress, ui)):
        raise RuntimeCommandError("native Live identity/state is incomplete")
    if (raw.get("screen_type") != "LiveScenePresenter" or raw.get("window_root_available") is not False
            or raw.get("collections_available") is not False or raw.get("blockers") != []
            or ui.get("family") != "live_presentation" or ui.get("identity_bound") is not True
            or type(ui.get("live_from_type")) is not int or ui["live_from_type"] != 1
            or state.get("in_progress") is not True or state.get("produce_id") != produce_id
            or progress.get("produceId") != produce_id or progress.get("idolCardId") != idol_card_id
            or ui.get("idol_card_id") != idol_card_id or not ui.get("character_id")
            or ui["character_id"] != progress.get("characterId")):
        raise RuntimeCommandError("native Live does not bind the current cultivation")
    keys = ("is_live_started", "is_live_ended", "is_playing", "is_pause")
    if any(type(ui.get(key)) is not bool for key in keys):
        raise RuntimeCommandError("native Live playback flags are incomplete")
    started, ended, playing, paused = (ui[key] for key in keys)
    if playing is not (started and not ended) or raw.get("busy") is not (playing and not paused):
        raise RuntimeCommandError("native Live playback flags disagree")
    phase = "paused" if paused else "after_live" if ended else "playing" if playing else "before_live"
    if ui.get("phase") != phase:
        raise RuntimeCommandError("native Live phase disagrees with playback flags")
    elapsed = ui.get("current_time")
    if elapsed is not None and (type(elapsed) not in (int, float) or not math.isfinite(elapsed)):
        raise RuntimeCommandError("native Live timeline is invalid")
    from .runtime_live_loading import loading_target
    if loading_target(raw) is not None or _owned_story_target(raw) is not None:
        return None
    return {"source": "native-live-presentation", "status": "waiting", "phase": phase,
            "current_time": elapsed, "reason": "等待原生培育演出與場景切換完成",
            "cultivation_complete": False, "input_sent": False}


def native_live_story_receipt(before, after, target):
    """A changing timeline is not completion of the owned story's callback."""
    if before.get("screen_type") != "LiveScenePresenter" or target.get("button_source") != "player-skip-only":
        return None
    expected = _owned_story_target(before)
    if expected != target:
        raise RuntimeCommandError("pending Live story differs from its original native target")
    old = before.get("progress") or {}
    progress = after.get("progress") or {}
    if any(not old.get(key) or progress.get(key) != old[key] for key in ("produceId", "idolCardId", "characterId")):
        return None
    if after.get("surface") == "error":
        return None
    completion = None
    if after.get("screen_type") != "LiveScenePresenter":
        if after.get("busy") is False and (after.get("state") or {}).get("in_progress") is True:
            completion = "same-produce-native-scene-left-Live"
    elif after.get("screen_instance_id") == before.get("screen_instance_id"):
        native_presentation_wait(after, produce_id=old["produceId"], idol_card_id=old["idolCardId"])
        rows = [row for row in (after.get("ui_state") or {}).get("live_story_players", [])
                if isinstance(row, Mapping) and row.get("field") == target["live_story_field"]]
        if len(rows) == 1:
            row = rows[0]
            if (row.get("player_instance_id") == target["live_story_player_instance_id"]
                    and row.get("adv_instance_id") in (target["adv_instance_id"], "0x0")
                    and row.get("story_run_active") is False and row.get("player_is_end") is True):
                completion = "same-owned-native-LiveStoryPlayer-ended"
            elif row.get("player_instance_id") == "0x0" and row.get("adv_instance_id") == "0x0":
                completion = "same-Live-owner-released-story-player"
    if completion is None:
        return None
    return {"source": "native-owned-live-story", "completion": completion,
            "live_owner_instance_id": before["screen_instance_id"], "target": dict(target),
            "cultivation_complete": False, "server_transaction_claimed": False}
