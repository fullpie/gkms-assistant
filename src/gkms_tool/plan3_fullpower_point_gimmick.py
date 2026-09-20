"""Exact unfiltered FullPowerPoint status-change trigger used by NIA gimmicks.

This recognizes Master shape, not game-input legality. Phase 13 receives a
committed changed-status type; the shared effect-result source gate and ordered
runtime owner still decide whether to dispatch. The child effects remain under
the engine's normal strict validation and GUID mutation executor.
"""
from __future__ import annotations
from dataclasses import dataclass

from .plan3_engine import (
    EFFECT_FULL_POWER_POINT, LESSON_UNKNOWN, MOVE_UNKNOWN, PHASE_STATUS_CHANGE,
    Plan3Trigger,
)

TRIGGER_ID = "e_trigger-exam_status_change-exam_full_power_point"
GROW_EFFECT_ID = "e_effect-exam_add_grow_effect-p_card_search-deck_all-all-0_0-g_effect-lesson_add-2"


def matches_trigger_contract(trigger: object) -> bool:
    return bool(
        isinstance(trigger, Plan3Trigger)
        and trigger.id == TRIGGER_ID
        and trigger.phase_types == (PHASE_STATUS_CHANGE,)
        and not trigger.phase_values
        and not trigger.field_check_types
        and not trigger.field_types
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and trigger.effect_types == (EFFECT_FULL_POWER_POINT,)
        and trigger.lesson_type == LESSON_UNKNOWN
    )


COMMAND_SNAPSHOT_CONTRACT = "gkms.plan3-queued-effect-before-late-grow.v1"


@dataclass(frozen=True)
class FollowingLessonCommandSnapshot:
    effect_id: str
    command_index: int
    value1: int
    value2: int
    effect_count: int
    prior_growth_command_indexes: tuple[int, ...]
    contract: str = COMMAND_SNAPSHOT_CONTRACT


def following_lesson_command_snapshot(effect, command_index, events):
    """Keep the built command's payload; later grow invalidates the card cache.

    APK ExecuteCardCommandImpl builds its whole command list before enqueue.
    ExamCardData.get_PlayProduceExamEffectList makes fresh ProduceExamEffect
    objects; CreatePlayEffectCommand retains one of those objects. AddGrowEffect
    appends a grow then nulls the cache pointer, not the already queued object.
    The GUID-native grow owner must still commit the growth for the next use.
    Evidence: command_snapshot_apk_bodies.json + native Mid2 step34 regression.
    """
    relevant = tuple(event for event in events if event.phase == PHASE_STATUS_CHANGE
        and event.source_direct_effect_index is not None and event.effect_id == GROW_EFFECT_ID)
    if not relevant:
        return None
    if effect.effect_type not in {"ProduceExamEffectType_ExamLesson", "ProduceExamEffectType_ExamLessonFullPowerPoint"}:
        raise ValueError("following-Lesson-command-type-outside-snapshot-contract")
    if type(command_index) is not int or command_index < 0:
        raise ValueError("following-Lesson-command-index-unavailable")
    if any(event.effect_type != "ProduceExamEffectType_ExamAddGrowEffect"
           or type(event.source_direct_effect_index) is not int
           or not 0 <= event.source_direct_effect_index < command_index for event in relevant):
        raise ValueError("following-Lesson-growth-command-order-unbound")
    return FollowingLessonCommandSnapshot(effect.id, command_index, effect.value1, effect.value2,
        effect.effect_count, tuple(event.source_direct_effect_index for event in relevant))
