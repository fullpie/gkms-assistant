"""Read-only outer Produce state reconstructed from the game's local saves.

The PC client does not serialize one complete outer-Produce model.  It does,
however, keep two useful files:

* ``ProducePlayLogSaveData`` records week markers, completed steps, exact
  before/after values, acquired rewards, and passive-effect triggers.
* ``ProduceLocalSaveData`` records lifecycle flags such as whether Produce is
  in progress and which auditions have started.

This adapter combines those direct observations.  It deliberately leaves
``available_actions`` and ``offered_rewards`` as ``None`` because neither is
present in these files; callers may join a static route calendar or a screen
observation without confusing that inference with local-save evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .local_save_decoder import (
    LocalSaveDecodeError,
    decode_local_save_bytes,
    decode_local_save_file,
    obfuscated_name,
)


PRODUCE_PLAY_LOG_SOURCE_TYPE = "Campus.Common.ProduceLog.ProducePlayLogSaveData"
PRODUCE_LIFECYCLE_SOURCE_TYPE = "Campus.InGame.Produce.ProduceLocalSaveData"
PRODUCE_PLAY_LOG_FILENAME = obfuscated_name(PRODUCE_PLAY_LOG_SOURCE_TYPE)
PRODUCE_LIFECYCLE_FILENAME = obfuscated_name(PRODUCE_LIFECYCLE_SOURCE_TYPE)

DEFAULT_PC_GAME_ROOT = (
    Path.home()
    / "AppData"
    / "LocalLow"
    / "BANDAI NAMCO Entertainment Inc_"
    / "gakumas"
)


CELL_TYPE_NAMES = {
    0: "week",
    1: "drink",
    2: "item",
    3: "ability",
    4: "step",
    5: "insert_event",
}

LINE_TYPE_NAMES = {
    0: "lesson_clear",
    1: "lesson_failure",
    2: "audition_clear",
    3: "audition_failure",
    4: "audition_result",
    5: "stamina",
    6: "vocal",
    7: "dance",
    8: "visual",
    9: "card_upgrade",
    10: "card_remove",
    11: "card_duplicate",
    12: "card_duplicate_upgrade",
    13: "card_change",
    14: "card_change_upgrade",
    15: "card_add",
    16: "drink_add",
    17: "item_add",
    18: "ability_add",
    19: "achievement_add",
    20: "max_stamina",
    21: "produce_points",
    22: "message",
    23: "shop_price_discount_multiple",
    24: "lesson_present_produce_point_up",
    25: "lesson_present_produce_point_down",
    26: "lesson_present_card_reward_count_up",
    27: "lesson_present_card_reward_count_down",
    28: "produce_point_addition_value_up",
    29: "produce_point_addition_value_down",
    30: "stamina_recover_value_up",
    31: "stamina_recover_value_down",
    32: "adv",
    33: "audition_npc_enhance",
    34: "vote_count",
    35: "audition_select",
    36: "vote_count_bonus_trigger",
    37: "card_customize",
    38: "business_success",
    39: "business_success_excellent",
    40: "high_score_gold",
    41: "exam_permanent_audition_status_enchant",
    42: "exam_permanent_lesson_status_enchant",
    43: "hif_star",
    44: "custom_item_add",
    45: "custom_item_customize",
}

STEP_TYPE_NAMES = {
    0: "unknown",
    1: "lesson_vocal_normal",
    2: "lesson_vocal_sp",
    3: "lesson_vocal_hard",
    4: "lesson_dance_normal",
    5: "lesson_dance_sp",
    6: "lesson_dance_hard",
    7: "lesson_visual_normal",
    8: "lesson_visual_sp",
    9: "lesson_visual_hard",
    10: "event",
    11: "event_activity",
    12: "event_school",
    13: "shop",
    14: "refresh",
    15: "present",
    16: "audition_mid1",
    17: "audition_mid2",
    18: "audition_final",
    19: "self_lesson_vocal_normal",
    20: "self_lesson_vocal_sp",
    21: "self_lesson_dance_normal",
    22: "self_lesson_dance_sp",
    23: "self_lesson_visual_normal",
    24: "self_lesson_visual_sp",
    25: "business",
    26: "event_business",
    27: "fan_present",
    28: "customize",
    29: "legend_lesson_vocal_normal",
    30: "legend_lesson_vocal_sp",
    31: "legend_lesson_dance_normal",
    32: "legend_lesson_dance_sp",
    33: "legend_lesson_visual_normal",
    34: "legend_lesson_visual_sp",
    35: "open_lesson_vocal_normal",
    36: "open_lesson_vocal_sp",
    37: "open_lesson_vocal_normal_star",
    38: "open_lesson_vocal_sp_star",
    39: "open_lesson_dance_normal",
    40: "open_lesson_dance_sp",
    41: "open_lesson_dance_normal_star",
    42: "open_lesson_dance_sp_star",
    43: "open_lesson_visual_normal",
    44: "open_lesson_visual_sp",
    45: "open_lesson_visual_normal_star",
    46: "open_lesson_visual_sp_star",
    47: "interval",
    48: "event_school_vocal",
    49: "event_school_dance",
    50: "event_school_visual",
}

PRODUCE_TYPE_NAMES = {
    0: "unknown",
    1: "first_star",
    2: "next_idol_audition",
    3: "hatsuboshi_idol_festival",
}

SPLIT_TYPE_NAMES = {0: "unknown", 1: "selection", 2: "final"}
PARAMETER_TYPE_NAMES = {0: "unknown", 1: "vocal", 2: "dance", 3: "visual"}

_STATUS_LINE_FIELDS = {
    5: "stamina",
    6: "vocal",
    7: "dance",
    8: "visual",
    20: "max_stamina",
    21: "produce_points",
    34: "vote_count",
}
_INVENTORY_LINE_TYPES = frozenset(
    {9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 37, 44, 45}
)


class ProduceOuterLocalSaveError(ValueError):
    """Raised when an outer Produce local-save payload is structurally invalid."""


@dataclass(frozen=True, slots=True)
class ProduceLogLine:
    log_index: int
    detail_index: int
    line_index: int
    cell_type: int
    cell_type_name: str
    line_type: int
    line_type_name: str
    before: float
    after: float
    target_id: str
    target_subscription_number: int
    target_id2: str
    target_subscription_number2: int
    is_triggered: bool
    trigger_id: str
    trigger_subscription_number: int
    trigger_owner_id: str
    trigger_owner_type: int

    @property
    def delta(self) -> float:
        return self.after - self.before


@dataclass(frozen=True, slots=True)
class ProduceCompletedStep:
    log_index: int
    week_marker: int | None
    step_type: int
    step_type_name: str
    parameter_type: int
    parameter_type_name: str
    produce_type: int
    produce_type_name: str
    split_type: int
    split_type_name: str
    lines: tuple[ProduceLogLine, ...]


@dataclass(frozen=True, slots=True)
class ProduceObservedValue:
    field: str
    value: int
    before: int
    log_index: int
    detail_index: int
    line_index: int


@dataclass(frozen=True, slots=True)
class ProduceLifecycleState:
    is_in_progress: bool
    is_started_audition_mid1: bool
    is_started_audition_mid2: bool
    is_started_audition_final: bool
    is_end_live: bool
    schedule_environment_drawing_number: int
    schedule_environment_type: int


@dataclass(frozen=True, slots=True)
class ProduceOuterLocalSaveSnapshot:
    save_data_version: int
    log_count: int
    latest_week_marker: int | None
    last_completed_week: int | None
    last_log_cell_type: int | None
    last_log_cell_type_name: str | None
    produce_type: int | None
    produce_type_name: str | None
    split_type: int | None
    split_type_name: str | None
    stamina: int | None
    max_stamina: int | None
    produce_points: int | None
    vocal: int | None
    dance: int | None
    visual: int | None
    observed_values: tuple[ProduceObservedValue, ...]
    completed_steps: tuple[ProduceCompletedStep, ...]
    inventory_events: tuple[ProduceLogLine, ...]
    audition_select_events: tuple[ProduceLogLine, ...]
    triggered_effect_lines: tuple[ProduceLogLine, ...]
    lifecycle: ProduceLifecycleState | None
    available_actions: None = None
    offered_rewards: None = None
    diagnostics: tuple[str, ...] = ()
    vote_count: int | None = None
    # Optional exact inventory count supplied by a stronger run-scoped reader.
    # ProducePlayLog itself does not serialize the current drink list, so the
    # parser leaves this unset while live callers may attach a proven count.
    drink_count: int | None = None


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProduceOuterLocalSaveError(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProduceOuterLocalSaveError(f"{label} must be a list")
    return value


def _int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProduceOuterLocalSaveError(f"{label} must be an integer")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProduceOuterLocalSaveError(f"{label} must be a boolean")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ProduceOuterLocalSaveError(f"{label} must be a string")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProduceOuterLocalSaveError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ProduceOuterLocalSaveError(f"{label} must be finite")
    return result


def _enum_name(values: Mapping[int, str], value: int, prefix: str) -> str:
    return values.get(value, f"{prefix}_{value}")


def parse_produce_lifecycle(data: Any) -> ProduceLifecycleState:
    """Parse only the outer-run lifecycle fields serialized by the client."""

    root = _mapping(data, "ProduceLocalSaveData root")
    return ProduceLifecycleState(
        is_in_progress=_bool(root.get("_isInProgress"), "_isInProgress"),
        is_started_audition_mid1=_bool(
            root.get("_isStartedAuditionMid1"), "_isStartedAuditionMid1"
        ),
        is_started_audition_mid2=_bool(
            root.get("_isStartedAuditionMid2"), "_isStartedAuditionMid2"
        ),
        is_started_audition_final=_bool(
            root.get("_isStartedAuditionFinal"), "_isStartedAuditionFinal"
        ),
        is_end_live=_bool(root.get("_isEndLive"), "_isEndLive"),
        schedule_environment_drawing_number=_int(
            root.get("_scheduleEnvironmentDrawingNumber"),
            "_scheduleEnvironmentDrawingNumber",
        ),
        schedule_environment_type=_int(
            root.get("_scheduleEnvironmentType"), "_scheduleEnvironmentType"
        ),
    )


def _integral_status_value(
    line: ProduceLogLine, diagnostics: list[str]
) -> ProduceObservedValue | None:
    field = _STATUS_LINE_FIELDS.get(line.line_type)
    if field is None:
        return None
    if line.before < 0 or line.after < 0:
        diagnostics.append(
            f"{field} at log {line.log_index} contains a negative value"
        )
        return None
    if not line.before.is_integer() or not line.after.is_integer():
        diagnostics.append(
            f"{field} at log {line.log_index} is fractional and was not coerced"
        )
        return None
    return ProduceObservedValue(
        field=field,
        value=int(line.after),
        before=int(line.before),
        log_index=line.log_index,
        detail_index=line.detail_index,
        line_index=line.line_index,
    )


def parse_produce_play_log(
    data: Any,
    *,
    save_data_version: int = 0,
    lifecycle: ProduceLifecycleState | None = None,
) -> ProduceOuterLocalSaveSnapshot:
    """Parse a decrypted ``ProducePlayLogSaveData`` JSON value.

    The newest observed ``after`` value for each status field becomes the
    snapshot value.  This is exact log replay, not OCR correction.  A step is
    associated with the most recent preceding Week record, while a future
    week is never guessed.
    """

    root = _mapping(data, "ProducePlayLogSaveData root")
    raw_logs = _list(root.get("_logList"), "_logList")
    diagnostics: list[str] = [
        "available action choices are not serialized by ProducePlayLogSaveData",
        "offered reward choices are not serialized; only accepted inventory events are present",
    ]
    all_lines: list[ProduceLogLine] = []
    inventory_events: list[ProduceLogLine] = []
    audition_select_events: list[ProduceLogLine] = []
    triggered_effect_lines: list[ProduceLogLine] = []
    completed_steps: list[ProduceCompletedStep] = []
    observed: dict[str, ProduceObservedValue] = {}
    latest_week: int | None = None
    last_cell_type: int | None = None
    produce_type: int | None = None
    split_type: int | None = None

    for log_index, raw_log in enumerate(raw_logs):
        log = _mapping(raw_log, f"_logList[{log_index}]")
        cell_type = _int(log.get("_cellType"), f"_logList[{log_index}]._cellType")
        cell_name = _enum_name(CELL_TYPE_NAMES, cell_type, "unknown_cell")
        last_cell_type = cell_type
        current_turn = _int(
            log.get("_currentTurn"), f"_logList[{log_index}]._currentTurn"
        )
        if cell_type == 0:
            if current_turn < 1:
                diagnostics.append(f"week marker at log {log_index} is below 1")
            else:
                if latest_week is not None and current_turn < latest_week:
                    diagnostics.append(
                        f"week marker regressed from {latest_week} to {current_turn}"
                    )
                latest_week = current_turn

        step_type = _int(log.get("_stepType"), f"_logList[{log_index}]._stepType")
        parameter_type = _int(
            log.get("_parameterType"), f"_logList[{log_index}]._parameterType"
        )
        row_produce_type = _int(
            log.get("_produceType"), f"_logList[{log_index}]._produceType"
        )
        row_split_type = _int(
            log.get("_splitType"), f"_logList[{log_index}]._splitType"
        )
        if row_produce_type:
            produce_type = row_produce_type
        if row_split_type:
            split_type = row_split_type

        log_lines: list[ProduceLogLine] = []
        raw_details = _list(
            log.get("_detailList"), f"_logList[{log_index}]._detailList"
        )
        for detail_index, raw_detail in enumerate(raw_details):
            detail = _mapping(
                raw_detail, f"_logList[{log_index}]._detailList[{detail_index}]"
            )
            detail_type = _int(
                detail.get("_detailType"),
                f"_logList[{log_index}]._detailList[{detail_index}]._detailType",
            )
            trigger_id = _string(
                detail.get("_triggerId"),
                f"_logList[{log_index}]._detailList[{detail_index}]._triggerId",
            )
            trigger_subscription = _int(
                detail.get("_triggerSubscriptionNumber"),
                f"_logList[{log_index}]._detailList[{detail_index}]._triggerSubscriptionNumber",
            )
            trigger_owner_id = _string(
                detail.get("_triggerOwnerId"),
                f"_logList[{log_index}]._detailList[{detail_index}]._triggerOwnerId",
            )
            trigger_owner_type = _int(
                detail.get("_triggerOwnerType"),
                f"_logList[{log_index}]._detailList[{detail_index}]._triggerOwnerType",
            )
            raw_lines = _list(
                detail.get("_detailLineList"),
                f"_logList[{log_index}]._detailList[{detail_index}]._detailLineList",
            )
            for line_index, raw_line in enumerate(raw_lines):
                line_data = _mapping(
                    raw_line,
                    f"_logList[{log_index}]._detailList[{detail_index}]"
                    f"._detailLineList[{line_index}]",
                )
                line_type = _int(line_data.get("_detailLineType"), "_detailLineType")
                parsed_line = ProduceLogLine(
                    log_index=log_index,
                    detail_index=detail_index,
                    line_index=line_index,
                    cell_type=cell_type,
                    cell_type_name=cell_name,
                    line_type=line_type,
                    line_type_name=_enum_name(
                        LINE_TYPE_NAMES, line_type, "unknown_line"
                    ),
                    before=_number(line_data.get("_before"), "_before"),
                    after=_number(line_data.get("_after"), "_after"),
                    target_id=_string(line_data.get("_targetId"), "_targetId"),
                    target_subscription_number=_int(
                        line_data.get("_targetSubscriptionNumber"),
                        "_targetSubscriptionNumber",
                    ),
                    target_id2=_string(line_data.get("_targetId2"), "_targetId2"),
                    target_subscription_number2=_int(
                        line_data.get("_targetSubscriptionNumber2"),
                        "_targetSubscriptionNumber2",
                    ),
                    is_triggered=detail_type == 1,
                    trigger_id=trigger_id,
                    trigger_subscription_number=trigger_subscription,
                    trigger_owner_id=trigger_owner_id,
                    trigger_owner_type=trigger_owner_type,
                )
                all_lines.append(parsed_line)
                log_lines.append(parsed_line)
                if parsed_line.line_type in _INVENTORY_LINE_TYPES:
                    inventory_events.append(parsed_line)
                if parsed_line.line_type == 35:
                    audition_select_events.append(parsed_line)
                if parsed_line.is_triggered:
                    triggered_effect_lines.append(parsed_line)
                value = _integral_status_value(parsed_line, diagnostics)
                if value is not None:
                    observed[value.field] = value

        if cell_type == 4:
            completed_steps.append(
                ProduceCompletedStep(
                    log_index=log_index,
                    week_marker=latest_week,
                    step_type=step_type,
                    step_type_name=_enum_name(
                        STEP_TYPE_NAMES, step_type, "unknown_step"
                    ),
                    parameter_type=parameter_type,
                    parameter_type_name=_enum_name(
                        PARAMETER_TYPE_NAMES, parameter_type, "unknown_parameter"
                    ),
                    produce_type=row_produce_type,
                    produce_type_name=_enum_name(
                        PRODUCE_TYPE_NAMES, row_produce_type, "unknown_produce"
                    ),
                    split_type=row_split_type,
                    split_type_name=_enum_name(
                        SPLIT_TYPE_NAMES, row_split_type, "unknown_split"
                    ),
                    lines=tuple(log_lines),
                )
            )

    for field in ("stamina", "vocal", "dance", "visual", "produce_points"):
        if field not in observed:
            diagnostics.append(f"no {field} transition has been logged yet")

    last_step = completed_steps[-1] if completed_steps else None
    return ProduceOuterLocalSaveSnapshot(
        save_data_version=save_data_version,
        log_count=len(raw_logs),
        latest_week_marker=latest_week,
        last_completed_week=last_step.week_marker if last_step else None,
        last_log_cell_type=last_cell_type,
        last_log_cell_type_name=(
            _enum_name(CELL_TYPE_NAMES, last_cell_type, "unknown_cell")
            if last_cell_type is not None
            else None
        ),
        produce_type=produce_type,
        produce_type_name=(
            _enum_name(PRODUCE_TYPE_NAMES, produce_type, "unknown_produce")
            if produce_type is not None
            else None
        ),
        split_type=split_type,
        split_type_name=(
            _enum_name(SPLIT_TYPE_NAMES, split_type, "unknown_split")
            if split_type is not None
            else None
        ),
        stamina=observed.get("stamina").value if "stamina" in observed else None,
        max_stamina=(
            observed.get("max_stamina").value
            if "max_stamina" in observed
            else None
        ),
        produce_points=(
            observed.get("produce_points").value
            if "produce_points" in observed
            else None
        ),
        vocal=observed.get("vocal").value if "vocal" in observed else None,
        dance=observed.get("dance").value if "dance" in observed else None,
        visual=observed.get("visual").value if "visual" in observed else None,
        observed_values=tuple(observed.values()),
        completed_steps=tuple(completed_steps),
        inventory_events=tuple(inventory_events),
        audition_select_events=tuple(audition_select_events),
        triggered_effect_lines=tuple(triggered_effect_lines),
        lifecycle=lifecycle,
        diagnostics=tuple(diagnostics),
        vote_count=(
            observed.get("vote_count").value
            if "vote_count" in observed
            else None
        ),
    )


def decode_produce_play_log_bytes(data: bytes) -> ProduceOuterLocalSaveSnapshot:
    """Decode and parse one encrypted Produce play-log file."""

    envelope = decode_local_save_bytes(data, PRODUCE_PLAY_LOG_SOURCE_TYPE)
    try:
        payload = json.loads(envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalSaveDecodeError(
            "decrypted ProducePlayLogSaveData body is not valid UTF-8 JSON"
        ) from exc
    return parse_produce_play_log(
        payload, save_data_version=envelope.save_data_version
    )


def decode_produce_play_log_file(path: str | Path) -> ProduceOuterLocalSaveSnapshot:
    """Read an encrypted Produce play-log file without write access."""

    return decode_produce_play_log_bytes(Path(path).read_bytes())


def decode_produce_lifecycle_file(path: str | Path) -> ProduceLifecycleState:
    envelope = decode_local_save_file(path, PRODUCE_LIFECYCLE_SOURCE_TYPE)
    try:
        payload = json.loads(envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalSaveDecodeError(
            "decrypted ProduceLocalSaveData body is not valid UTF-8 JSON"
        ) from exc
    return parse_produce_lifecycle(payload)


def read_produce_outer_local_save(
    save_directory: str | Path,
) -> ProduceOuterLocalSaveSnapshot:
    """Read the play log and its optional sibling lifecycle file."""

    directory = Path(save_directory)
    play_log_path = directory / PRODUCE_PLAY_LOG_FILENAME
    if not play_log_path.is_file():
        raise FileNotFoundError(play_log_path)
    snapshot = decode_produce_play_log_file(play_log_path)
    lifecycle_path = directory / PRODUCE_LIFECYCLE_FILENAME
    if not lifecycle_path.is_file():
        return snapshot
    return replace(snapshot, lifecycle=decode_produce_lifecycle_file(lifecycle_path))


def discover_produce_save_directories(
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
) -> tuple[Path, ...]:
    """Find directories containing the exact Produce play-log filename."""

    root = Path(game_root)
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            {path.parent for path in root.rglob(PRODUCE_PLAY_LOG_FILENAME)},
            key=lambda path: str(path).casefold(),
        )
    )


def read_current_produce_outer_local_save(
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
) -> ProduceOuterLocalSaveSnapshot:
    """Read the unique active Produce save below the PC game root.

    Selection uses the serialized ``_isInProgress`` flag, not timestamps.  If
    no unique active directory exists, the caller must select a directory.
    """

    directories = discover_produce_save_directories(game_root)
    if not directories:
        raise FileNotFoundError(
            f"no {PRODUCE_PLAY_LOG_FILENAME} below {Path(game_root)}"
        )
    active: list[tuple[Path, ProduceOuterLocalSaveSnapshot]] = []
    decoded: list[tuple[Path, ProduceOuterLocalSaveSnapshot]] = []
    for directory in directories:
        snapshot = read_produce_outer_local_save(directory)
        decoded.append((directory, snapshot))
        if snapshot.lifecycle is not None and snapshot.lifecycle.is_in_progress:
            active.append((directory, snapshot))
    if len(active) == 1:
        return active[0][1]
    if not active and len(decoded) == 1:
        return decoded[0][1]
    raise ProduceOuterLocalSaveError(
        "could not select a unique active Produce local-save directory"
    )


__all__ = [
    "DEFAULT_PC_GAME_ROOT",
    "PRODUCE_LIFECYCLE_FILENAME",
    "PRODUCE_LIFECYCLE_SOURCE_TYPE",
    "PRODUCE_PLAY_LOG_FILENAME",
    "PRODUCE_PLAY_LOG_SOURCE_TYPE",
    "ProduceCompletedStep",
    "ProduceLifecycleState",
    "ProduceLogLine",
    "ProduceObservedValue",
    "ProduceOuterLocalSaveError",
    "ProduceOuterLocalSaveSnapshot",
    "decode_produce_lifecycle_file",
    "decode_produce_play_log_bytes",
    "decode_produce_play_log_file",
    "discover_produce_save_directories",
    "parse_produce_lifecycle",
    "parse_produce_play_log",
    "read_current_produce_outer_local_save",
    "read_produce_outer_local_save",
]
