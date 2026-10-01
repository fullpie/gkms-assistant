"""Exact Plan2 runtime for the MentalSkill/Playing ``ExamCardPlay`` listener.

Android v3.2.3 builds this transaction in two distinct stages.  It sets the
playing-card pointer while the GUID is still in Hand, captures and spends
eligible listener counts before payment/direct effects, then executes the
captured children after payment and before the card's direct effects.  The
physical card move is later still.  This module exposes that capture boundary
so a listener installed by the current card cannot accidentally self-trigger.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .card_search import (
    ProduceCardSearchRule,
    exact_playing_card_search_mismatches,
    load_produce_card_search,
    match_exact_playing_card_search,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_state import (
    PERMANENT_TURN,
    Plan2CardPlayEffect,
    Plan2CardPlayListener,
    Plan2State,
)
from .playing_card_identity import resolve_playing_card_identity


STATUS_ENCHANT_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"
CARD_PLAY_PHASE_TYPE = "ProduceExamPhaseType_ExamCardPlay"
CARD_PLAY_TRIGGER_ID = (
    "e_trigger-exam_card_play-p_card_search-mental_skill-playing-0_1"
)
MENTAL_SKILL_PLAYING_SEARCH_ID = "p_card_search-mental_skill-playing"
MENTAL_SKILL_CATEGORY = "ProduceCardCategory_MentalSkill"
REVIEW_EFFECT_TYPE = "ProduceExamEffectType_ExamReview"
AGGRESSIVE_EFFECT_TYPE = "ProduceExamEffectType_ExamCardPlayAggressive"

AGGRESSIVE_WRAPPER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-men-2_004-enc01"
)
REVIEW_WRAPPER_EFFECT_ID = (
    "e_effect-exam_status_enchant-inf-enchant-p_card-02-men-2_058-enc01"
)
SUPPORTED_WRAPPER_EFFECT_IDS = frozenset(
    {AGGRESSIVE_WRAPPER_EFFECT_ID, REVIEW_WRAPPER_EFFECT_ID}
)

ANDROID_CARD_PLAY_TRIGGER_EVIDENCE = {
    "set_playing": {
        "symbol": "ExamSequence.ExecuteCardCommandImpl / CardController.SetPlayingCard",
        "vas": ["0x7ECE628", "0x7ECE85C"],
        "fact": "PlayingCard is set while the selected GUID remains in Hand",
    },
    "capture": {
        "symbol": "ExamStatusEffectCollection.GetCardPlayValidEffectList",
        "va": "0x7EA2318",
        "predicate_va": "0x7EA8C20",
        "validator_va": "0x7E68154",
        "fact": "active listeners are stably filtered against the selected card",
    },
    "count": {
        "symbol": "TriggerEffectStatusEffect.SpendCount",
        "va": "0x7EAE70C",
        "fact": "count is spent while the reactive command is captured",
    },
    "transaction_order": {
        "symbol": "ExamSequence.UseHandCard / ExecuteCardCommandImpl",
        "fact": (
            "capture is pre-payment/pre-direct; captured children execute "
            "post-payment/pre-direct; physical MovePlayCard is later"
        ),
    },
    "install": {
        "symbol": "StatusEnchantEffectExecutor.ExecuteEffect",
        "va": "0x7E8FA08",
        "fact": "child IDs preserve Master order and every normal card install is distinct",
    },
}


class Plan2CardPlayContractError(ValueError):
    """Master or runtime evidence is outside this exact proved family."""


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2CardPlayContractError(f"{label} must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2CardPlayContractError(f"{label} is outside Int32")
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(value: object, row_id: str) -> dict[str, object]:
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2CardPlayContractError(f"{row_id}: invalid raw_json") from error
    if not isinstance(raw, dict):
        raise Plan2CardPlayContractError(f"{row_id}: raw_json must be an object")
    return raw


def _json_array(value: object, label: str) -> list[object]:
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2CardPlayContractError(f"{label}: invalid JSON array") from error
    if not isinstance(raw, list):
        raise Plan2CardPlayContractError(f"{label} must be an array")
    return raw


def _require_equal(
    raw: dict[str, object], key: str, expected: object, row_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2CardPlayContractError(
            f"{row_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


def _ordered_groups(raw: dict[str, object], row_id: str) -> tuple[str, ...]:
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or any(
        not isinstance(group_id, str) or not group_id for group_id in groups
    ):
        raise Plan2CardPlayContractError(
            f"{row_id}: effectGroupIds must be an ordered string array"
        )
    return tuple(groups)


def _validate_effect_controls(
    row: sqlite3.Row,
    raw: dict[str, object],
    *,
    status_id: str,
) -> None:
    effect_id = str(row["id"])
    expected = (
        ("id", effect_id),
        ("effectType", str(row["effect_type"])),
        ("effectValue1", int(row["value1"])),
        ("effectValue2", int(row["value2"])),
        ("effectCount", int(row["effect_count"])),
        ("effectTurn", int(row["effect_turn"])),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        ("produceCardSearchId", ""),
        ("movePositionType", "ProduceCardMovePositionType_Unknown"),
        ("pickRangeType", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId", ""),
        ("pickCountType", "ProducePickCountType_Unknown"),
        ("pickCountMin", 0),
        ("pickCountMax", 0),
        ("produceCardSearchId2", ""),
        ("pickRangeType2", "ProducePickRangeType_Unknown"),
        ("pickCountReferenceProduceCardSearchId2", ""),
        ("pickCountType2", "ProducePickCountType_Unknown"),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceExamStatusEnchantId", status_id),
        ("produceCardStatusEnchantId", ""),
        ("produceCardGrowEffectIds", []),
    )
    for key, value in expected:
        _require_equal(raw, key, value, effect_id)


@dataclass(frozen=True, slots=True)
class Plan2PlayingCard:
    id: str
    upgrade: int
    category: str
    effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("id must be non-empty")
        upgrade = _plain_i32(self.upgrade, "upgrade")
        if upgrade < 0:
            raise ValueError("upgrade must be non-negative")
        if not isinstance(self.category, str) or not self.category:
            raise ValueError("category must be non-empty")
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(group_id, str) or not group_id for group_id in groups):
            raise ValueError("effect_group_ids entries must be non-empty strings")
        object.__setattr__(self, "effect_group_ids", groups)


@dataclass(frozen=True, slots=True)
class Plan2CardPlayProgram:
    wrapper_effect_id: str
    status_enchant_id: str
    trigger_id: str
    search: ProduceCardSearchRule
    effects: tuple[Plan2CardPlayEffect, ...]
    wrapper_effect_group_ids: tuple[str, ...]
    turn: int
    effect_count: int
    effect_value1: int

    def __post_init__(self) -> None:
        if self.wrapper_effect_id not in SUPPORTED_WRAPPER_EFFECT_IDS:
            raise Plan2CardPlayContractError("unsupported wrapper_effect_id")
        if self.trigger_id != CARD_PLAY_TRIGGER_ID:
            raise Plan2CardPlayContractError("unsupported trigger_id")
        if (
            not isinstance(self.search, ProduceCardSearchRule)
            or self.search.id != MENTAL_SKILL_PLAYING_SEARCH_ID
        ):
            raise Plan2CardPlayContractError("unsupported playing search")
        mismatches = exact_playing_card_search_mismatches(
            self.search,
            expected_categories=(MENTAL_SKILL_CATEGORY,),
            expected_effect_group_ids=(),
        )
        if mismatches:
            raise Plan2CardPlayContractError(
                f"unsupported playing search field {mismatches[0]}"
            )
        effects = tuple(self.effects)
        if len(effects) != 1 or not isinstance(effects[0], Plan2CardPlayEffect):
            raise Plan2CardPlayContractError("expected one typed child effect")
        object.__setattr__(self, "effects", effects)
        child = effects[0]
        expected_child_groups = {
            AGGRESSIVE_EFFECT_TYPE: (
                "effect_group-visible-exam_card_play_aggressive-000",
            ),
            REVIEW_EFFECT_TYPE: ("effect_group-visible-exam_review-000",),
        }.get(child.effect_type)
        if (
            expected_child_groups is None
            or child.value1 != 1
            or child.value2 != 0
            or child.count != 0
            or child.turn != 0
            or child.effect_group_ids != expected_child_groups
        ):
            raise Plan2CardPlayContractError("unsupported child effect shape")
        expected_groups = (
            "effect_group-visible-exam_status_enchant-000",
            expected_child_groups[0],
        )
        if tuple(self.wrapper_effect_group_ids) != expected_groups:
            raise Plan2CardPlayContractError("unsupported wrapper effect-group order")
        object.__setattr__(
            self, "wrapper_effect_group_ids", tuple(self.wrapper_effect_group_ids)
        )
        if (
            _plain_i32(self.turn, "turn") != PERMANENT_TURN
            or _plain_i32(self.effect_count, "effect_count") != 0
            or _plain_i32(self.effect_value1, "effect_value1") != 0
        ):
            raise Plan2CardPlayContractError(
                "this exact family is permanent and count-unlimited"
            )

    @property
    def native_limit_count(self) -> int:
        return self.effect_count if self.effect_count > 0 else -1

    @property
    def native_limit_count_in_turn(self) -> int:
        return self.effect_value1 if self.effect_value1 > 0 else -1

    @property
    def phase_values(self) -> tuple[int, ...]:
        return ()

    @property
    def interval(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Plan2CardPlayInstallTransition:
    before: Plan2State
    after: Plan2State
    program: Plan2CardPlayProgram
    created_status_uid: int


@dataclass(frozen=True, slots=True)
class Plan2CardPlayCapturedActivation:
    listener_before: Plan2CardPlayListener
    listener_after_count_spend: Plan2CardPlayListener | None


@dataclass(frozen=True, slots=True)
class Plan2CardPlayCapture:
    before: Plan2State
    after_count_spend: Plan2State
    playing_guid: str
    playing_card: Plan2PlayingCard
    candidates: tuple[Plan2CardPlayCapturedActivation, ...]
    removed_status_uids: tuple[int, ...]
    event_trace: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardPlayEffectOccurrence:
    listener_status_uid: int
    effect_index: int
    effect_id: str
    effect_type: str
    review_before: int
    review_after: int
    aggressive_before: int
    aggressive_after: int


@dataclass(frozen=True, slots=True)
class Plan2CardPlayActivation:
    captured: Plan2CardPlayCapturedActivation
    effects: tuple[Plan2CardPlayEffectOccurrence, ...]


@dataclass(frozen=True, slots=True)
class Plan2CardPlayTransition:
    before: Plan2State
    after_capture: Plan2State
    after_cost: Plan2State
    after_listeners: Plan2State
    after: Plan2State
    capture: Plan2CardPlayCapture
    activations: tuple[Plan2CardPlayActivation, ...]
    event_trace: tuple[str, ...]


Plan2CardPlayStage = Callable[[Plan2State], Plan2State]


def _load_child(row: sqlite3.Row) -> Plan2CardPlayEffect:
    effect_id = str(row["id"])
    raw = _json_object(row["raw_json"], effect_id)
    _validate_effect_controls(row, raw, status_id="")
    effect_type = str(row["effect_type"])
    value1 = _plain_i32(int(row["value1"]), f"{effect_id}.value1")
    value2 = _plain_i32(int(row["value2"]), f"{effect_id}.value2")
    count = _plain_i32(int(row["effect_count"]), f"{effect_id}.count")
    turn = _plain_i32(int(row["effect_turn"]), f"{effect_id}.turn")
    expected_group = {
        AGGRESSIVE_EFFECT_TYPE: ("effect_group-visible-exam_card_play_aggressive-000",),
        REVIEW_EFFECT_TYPE: ("effect_group-visible-exam_review-000",),
    }.get(effect_type)
    if (
        expected_group is None
        or value1 != 1
        or value2 != 0
        or count != 0
        or turn != 0
        or _ordered_groups(raw, effect_id) != expected_group
    ):
        raise Plan2CardPlayContractError(
            f"{effect_id}: unsupported CardPlay child shape"
        )
    return Plan2CardPlayEffect(
        effect_id,
        effect_type,
        value1,
        value2,
        count,
        turn,
        expected_group,
    )


def _validate_trigger(row: sqlite3.Row) -> None:
    trigger_id = str(row["id"])
    expected_columns = (
        ("phase_types_json", [CARD_PLAY_PHASE_TYPE]),
        ("phase_values_json", []),
        ("field_status_check_types_json", []),
        ("field_status_types_json", []),
        ("field_status_values_json", []),
        ("field_status_produce_card_search_ids_json", []),
        ("effect_types_json", []),
    )
    for column, expected in expected_columns:
        if _json_array(row[column], f"{trigger_id}.{column}") != expected:
            raise Plan2CardPlayContractError(
                f"{trigger_id}: unsupported trigger field {column}"
            )
    if (
        trigger_id != CARD_PLAY_TRIGGER_ID
        or str(row["produce_card_search_id"]) != MENTAL_SKILL_PLAYING_SEARCH_ID
        or int(row["upper_search_count"]) != 0
        or int(row["lower_search_count"]) != 1
        or str(row["card_move_position_type"])
        != "ProduceCardMovePositionType_Unknown"
        or str(row["lesson_type"]) != "ProduceStepLessonType_Unknown"
    ):
        raise Plan2CardPlayContractError(f"{trigger_id}: unsupported trigger shape")
    raw = _json_object(row["raw_json"], trigger_id)
    for key, expected in (
        ("id", CARD_PLAY_TRIGGER_ID),
        ("phaseTypes", [CARD_PLAY_PHASE_TYPE]),
        ("phaseValues", []),
        ("fieldStatusCheckTypes", []),
        ("fieldStatusTypes", []),
        ("fieldStatusValues", []),
        ("fieldStatusProduceCardSearchIds", []),
        ("produceCardSearchId", MENTAL_SKILL_PLAYING_SEARCH_ID),
        ("upperSearchCount", 0),
        ("lowerSearchCount", 1),
        ("cardMovePositionType", "ProduceCardMovePositionType_Unknown"),
        ("effectTypes", []),
        ("lessonType", "ProduceStepLessonType_Unknown"),
    ):
        _require_equal(raw, key, expected, trigger_id)


def load_plan2_card_play_program(
    wrapper_effect_id: str, database: Path = DEFAULT_DATABASE
) -> Plan2CardPlayProgram:
    """Load only the two current Plan2 normal-card wrapper programs."""

    if wrapper_effect_id not in SUPPORTED_WRAPPER_EFFECT_IDS:
        raise Plan2CardPlayContractError(
            f"{wrapper_effect_id}: wrapper is outside the exact Plan2 family"
        )
    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        wrapper = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (wrapper_effect_id,)
        ).fetchone()
        if wrapper is None:
            raise KeyError(f"unknown Master effect: {wrapper_effect_id}")
        if str(wrapper["effect_type"]) != STATUS_ENCHANT_EFFECT_TYPE:
            raise Plan2CardPlayContractError(
                f"{wrapper_effect_id}: expected StatusEnchant"
            )
        status_id = str(wrapper["status_enchant_id"])
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (status_id,)
        ).fetchone()
        if status is None:
            raise Plan2CardPlayContractError(
                f"{wrapper_effect_id}: missing status {status_id}"
            )
        trigger_id = str(status["produce_exam_trigger_id"])
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (trigger_id,)
        ).fetchone()
        if trigger is None:
            raise Plan2CardPlayContractError(
                f"{status_id}: missing trigger {trigger_id}"
            )
        child_ids = tuple(
            str(value)
            for value in _json_array(
                status["produce_exam_effect_ids_json"], status_id
            )
        )
        if len(child_ids) != 1 or not child_ids[0]:
            raise Plan2CardPlayContractError(
                f"{status_id}: expected one ordered child"
            )
        child_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (child_ids[0],)
        ).fetchone()
        if child_row is None:
            raise Plan2CardPlayContractError(
                f"{status_id}: missing child {child_ids[0]}"
            )
        child = _load_child(child_row)

    wrapper_raw = _json_object(wrapper["raw_json"], wrapper_effect_id)
    _validate_effect_controls(wrapper, wrapper_raw, status_id=status_id)
    if (
        int(wrapper["value1"]) != 0
        or int(wrapper["value2"]) != 0
        or int(wrapper["effect_count"]) != 0
        or int(wrapper["effect_turn"]) != PERMANENT_TURN
        or str(wrapper["chain_effect_id"])
    ):
        raise Plan2CardPlayContractError(
            f"{wrapper_effect_id}: unsupported wrapper count/lifetime shape"
        )
    expected_wrapper_groups = (
        "effect_group-visible-exam_status_enchant-000",
        child.effect_group_ids[0],
    )
    if _ordered_groups(wrapper_raw, wrapper_effect_id) != expected_wrapper_groups:
        raise Plan2CardPlayContractError(
            f"{wrapper_effect_id}: wrapper effect-group order changed"
        )
    status_raw = _json_object(status["raw_json"], status_id)
    for key, expected in (
        ("id", status_id),
        ("assetId", str(status["asset_id"])),
        ("produceExamTriggerId", trigger_id),
        ("produceExamEffectIds", list(child_ids)),
    ):
        _require_equal(status_raw, key, expected, status_id)
    _validate_trigger(trigger)
    search = load_produce_card_search(MENTAL_SKILL_PLAYING_SEARCH_ID, database)
    mismatches = exact_playing_card_search_mismatches(
        search,
        expected_categories=(MENTAL_SKILL_CATEGORY,),
        expected_effect_group_ids=(),
    )
    if mismatches:
        raise Plan2CardPlayContractError(
            f"{search.id}: unsupported playing search field {mismatches[0]}"
        )
    return Plan2CardPlayProgram(
        wrapper_effect_id=wrapper_effect_id,
        status_enchant_id=status_id,
        trigger_id=trigger_id,
        search=search,
        effects=(child,),
        wrapper_effect_group_ids=expected_wrapper_groups,
        turn=PERMANENT_TURN,
        effect_count=0,
        effect_value1=0,
    )


def simulate_install_plan2_card_play_listener(
    state: Plan2State, program: Plan2CardPlayProgram
) -> Plan2CardPlayInstallTransition:
    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(program, Plan2CardPlayProgram):
        raise TypeError("program must be Plan2CardPlayProgram")
    uid = state.next_status_uid
    listener = Plan2CardPlayListener(
        status_uid=uid,
        wrapper_effect_id=program.wrapper_effect_id,
        status_enchant_id=program.status_enchant_id,
        trigger_id=program.trigger_id,
        search_id=program.search.id,
        effects=program.effects,
        wrapper_effect_group_ids=program.wrapper_effect_group_ids,
        turn=program.turn,
        limit_count=program.native_limit_count,
        limit_count_in_turn=program.native_limit_count_in_turn,
        limit_count_in_turn_remaining=program.native_limit_count_in_turn,
    )
    after = replace(
        state,
        card_play_listeners=state.card_play_listeners + (listener,),
        next_status_uid=uid + 1,
    )
    return Plan2CardPlayInstallTransition(state, after, program, uid)


def _replace_or_remove_listener(
    state: Plan2State,
    uid: int,
    replacement: Plan2CardPlayListener | None,
) -> Plan2State:
    found = False
    listeners: list[Plan2CardPlayListener] = []
    for listener in state.card_play_listeners:
        if listener.status_uid != uid:
            listeners.append(listener)
            continue
        found = True
        if replacement is not None:
            listeners.append(replacement)
    if not found:
        raise RuntimeError(f"active CardPlay listener disappeared: {uid}")
    return replace(state, card_play_listeners=tuple(listeners))


def capture_plan2_exam_card_play(
    state: Plan2State,
    native_state: object,
    playing_guid: str,
    playing_card: Plan2PlayingCard,
    search: ProduceCardSearchRule,
) -> Plan2CardPlayCapture:
    """Capture matching listeners and spend their counts before payment."""

    if not isinstance(state, Plan2State):
        raise TypeError("state must be Plan2State")
    if not isinstance(playing_card, Plan2PlayingCard):
        raise TypeError("playing_card must be Plan2PlayingCard")
    identity = resolve_playing_card_identity(
        native_state,
        playing_guid,
        playing_card,
        reason_context=CARD_PLAY_TRIGGER_ID,
    )
    if identity.issues:
        issue = identity.issues[0]
        raise Plan2CardPlayContractError(f"{issue.field}: {issue.reason}")
    if search.id != MENTAL_SKILL_PLAYING_SEARCH_ID:
        raise Plan2CardPlayContractError(
            f"unsupported playing search id: {search.id}"
        )
    matched, reason = match_exact_playing_card_search(
        search,
        card_category=playing_card.category,
        card_effect_group_ids=playing_card.effect_group_ids,
        expected_categories=(MENTAL_SKILL_CATEGORY,),
        expected_effect_group_ids=(),
    )
    if reason is not None:
        raise Plan2CardPlayContractError(reason)
    after = state
    candidates: list[Plan2CardPlayCapturedActivation] = []
    removed: list[int] = []
    trace = [
        f"set-playing-card:{playing_guid}:guid-still-in-hand",
        f"capture:{CARD_PLAY_PHASE_TYPE}:pre-payment:pre-direct",
    ]
    for snapshot in state.card_play_listeners:
        if (
            snapshot.trigger_id != CARD_PLAY_TRIGGER_ID
            or snapshot.search_id != search.id
            or len(snapshot.effects) != 1
            or snapshot.effects[0].effect_type
            not in {AGGRESSIVE_EFFECT_TYPE, REVIEW_EFFECT_TYPE}
        ):
            raise Plan2CardPlayContractError(
                f"listener:{snapshot.status_uid}: unsupported trigger/search/effects"
            )
    if matched:
        for snapshot in state.card_play_listeners:
            current = next(
                (
                    item
                    for item in after.card_play_listeners
                    if item.status_uid == snapshot.status_uid
                ),
                None,
            )
            if current is None or not current.can_trigger:
                continue
            total = current.limit_count
            if total >= 0:
                total = _i32(total - 1)
            remaining = current.limit_count_in_turn_remaining
            if current.limit_count_in_turn >= 0:
                remaining = _i32(remaining - 1)
            listener_after = replace(
                current,
                limit_count=total,
                limit_count_in_turn_remaining=remaining,
            )
            if current.limit_count >= 0 and total <= 0:
                listener_after = None
                removed.append(current.status_uid)
            after = _replace_or_remove_listener(
                after, current.status_uid, listener_after
            )
            candidates.append(
                Plan2CardPlayCapturedActivation(current, listener_after)
            )
            trace.append(f"spend-count:{current.status_uid}:queue-time")
    return Plan2CardPlayCapture(
        before=state,
        after_count_spend=after,
        playing_guid=playing_guid,
        playing_card=playing_card,
        candidates=tuple(candidates),
        removed_status_uids=tuple(removed),
        event_trace=tuple(trace),
    )


def _identity_stage(state: Plan2State) -> Plan2State:
    return state


def execute_plan2_exam_card_play(
    capture: Plan2CardPlayCapture,
    *,
    pay_cost: Plan2CardPlayStage = _identity_stage,
    apply_direct_effects: Plan2CardPlayStage = _identity_stage,
) -> Plan2CardPlayTransition:
    """Execute a captured batch after cost and before direct effects."""

    if not isinstance(capture, Plan2CardPlayCapture):
        raise TypeError("capture must be Plan2CardPlayCapture")
    trace = list(capture.event_trace)
    after_cost = pay_cost(capture.after_count_spend)
    if not isinstance(after_cost, Plan2State):
        raise TypeError("pay_cost must return Plan2State")
    trace.append("pay-cost")
    after = after_cost
    activations: list[Plan2CardPlayActivation] = []
    for candidate in capture.candidates:
        listener = candidate.listener_before
        occurrences: list[Plan2CardPlayEffectOccurrence] = []
        for effect_index, effect in enumerate(listener.effects):
            review_before = after.review
            aggressive_before = after.card_play_aggressive
            if effect.effect_type == REVIEW_EFFECT_TYPE:
                next_value = _i32(after.review + effect.value1)
                if next_value < 0:
                    raise OverflowError("native Review addition wrapped negative")
                after = replace(after, review=next_value)
            elif effect.effect_type == AGGRESSIVE_EFFECT_TYPE:
                next_value = _i32(after.card_play_aggressive + effect.value1)
                if next_value < 0:
                    raise OverflowError("native Aggressive addition wrapped negative")
                after = replace(after, card_play_aggressive=next_value)
            else:  # pragma: no cover - state/loader boundary
                raise Plan2CardPlayContractError(
                    f"unsupported runtime child type: {effect.effect_type}"
                )
            occurrences.append(
                Plan2CardPlayEffectOccurrence(
                    listener.status_uid,
                    effect_index,
                    effect.effect_id,
                    effect.effect_type,
                    review_before,
                    after.review,
                    aggressive_before,
                    after.card_play_aggressive,
                )
            )
            trace.append(
                f"listener:{listener.status_uid}:effect:{effect_index}:{effect.effect_id}"
            )
        activations.append(Plan2CardPlayActivation(candidate, tuple(occurrences)))
    after_listeners = after
    after = apply_direct_effects(after_listeners)
    if not isinstance(after, Plan2State):
        raise TypeError("apply_direct_effects must return Plan2State")
    trace.extend(("direct-effects", "physical-move:later"))
    return Plan2CardPlayTransition(
        before=capture.before,
        after_capture=capture.after_count_spend,
        after_cost=after_cost,
        after_listeners=after_listeners,
        after=after,
        capture=capture,
        activations=tuple(activations),
        event_trace=tuple(trace),
    )


def simulate_plan2_exam_card_play(
    state: Plan2State,
    native_state: object,
    playing_guid: str,
    playing_card: Plan2PlayingCard,
    search: ProduceCardSearchRule,
    *,
    pay_cost: Plan2CardPlayStage = _identity_stage,
    apply_direct_effects: Plan2CardPlayStage = _identity_stage,
) -> Plan2CardPlayTransition:
    capture = capture_plan2_exam_card_play(
        state, native_state, playing_guid, playing_card, search
    )
    return execute_plan2_exam_card_play(
        capture,
        pay_cost=pay_cost,
        apply_direct_effects=apply_direct_effects,
    )


__all__ = [
    "AGGRESSIVE_EFFECT_TYPE",
    "AGGRESSIVE_WRAPPER_EFFECT_ID",
    "ANDROID_CARD_PLAY_TRIGGER_EVIDENCE",
    "CARD_PLAY_PHASE_TYPE",
    "CARD_PLAY_TRIGGER_ID",
    "MENTAL_SKILL_CATEGORY",
    "MENTAL_SKILL_PLAYING_SEARCH_ID",
    "Plan2CardPlayActivation",
    "Plan2CardPlayCapture",
    "Plan2CardPlayCapturedActivation",
    "Plan2CardPlayContractError",
    "Plan2CardPlayEffectOccurrence",
    "Plan2CardPlayInstallTransition",
    "Plan2CardPlayProgram",
    "Plan2CardPlayStage",
    "Plan2CardPlayTransition",
    "Plan2PlayingCard",
    "REVIEW_EFFECT_TYPE",
    "REVIEW_WRAPPER_EFFECT_ID",
    "SUPPORTED_WRAPPER_EFFECT_IDS",
    "capture_plan2_exam_card_play",
    "execute_plan2_exam_card_play",
    "load_plan2_card_play_program",
    "simulate_install_plan2_card_play_listener",
    "simulate_plan2_exam_card_play",
]
