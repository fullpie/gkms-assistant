"""Exact Initial Regular Plan2 lesson-gimmick hooks.

The LocalSave schedule owns only ``turn/group/effect``.  This module joins
those identities back to the PC Master profile and exposes hooks only for the
small scalar executor family whose Android semantics are already recovered:
Block, Review, and direct StaminaDamage.  Unknown conditions or effects remain
fail-closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Final

from .lesson_gimmick import (
    CHECK_NOT,
    CHECK_UNKNOWN,
    DEFAULT_GIMMICK_SOURCE,
    EFFECT_STAMINA_DAMAGE,
    FIELD_REVIEW,
    FIELD_UNKNOWN,
    LessonGimmickStep,
    load_lesson_gimmick_profile,
)
from .logic_engine import EFFECT_BLOCK, EFFECT_GOOD_IMPRESSION
from .master_db import DEFAULT_DATABASE
from .plan2_exam_mode import (
    Plan2ScheduledGimmickHook,
    Plan2ScheduledGimmickHookKey,
    Plan2ScheduledGimmickHookResult,
    plan2_scheduled_gimmick_hook_key,
)
from .plan2_native_horizon import (
    Plan2NativeExternalBlockEffect,
    Plan2NativeExternalReviewEffect,
    Plan2NativeExternalStaminaDamageEffect,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    apply_plan2_native_external_block_effect,
    apply_plan2_native_external_review_effect,
    apply_plan2_native_external_stamina_damage_effect,
)


_ROW_FIELDS: Final = frozenset({"turn", "gimmickGroupId", "gimmickEffectId"})


class InitialRegularPlan2LessonGimmickError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _step_fires(step: LessonGimmickStep, state: Plan2NativeHorizonState) -> bool:
    if (
        step.field_status_type == FIELD_UNKNOWN
        and step.field_status_check_type == CHECK_UNKNOWN
        and step.field_status_value == 0
    ):
        return True
    if step.field_status_type == FIELD_REVIEW:
        if step.field_status_check_type == CHECK_UNKNOWN:
            return state.scalar.review >= step.field_status_value
        if step.field_status_check_type == CHECK_NOT:
            return state.scalar.review < step.field_status_value
    raise InitialRegularPlan2LessonGimmickError(
        "plan2-lesson-gimmick-condition-unsupported",
        f"{step.group_id}:priority={step.priority}",
    )


def _hook_for_step(step: LessonGimmickStep) -> Plan2ScheduledGimmickHook:
    effect = step.effect
    if (
        step.remaining_turn_permille != 0
        or effect.value1 <= 0
        or effect.value2 != 0
        or effect.effect_count != 0
        or effect.effect_turn != 0
        or effect.status_enchant_id
        or effect.chain_effect_id
        or effect.trigger_id
        or effect.hide_icon
        or effect.once
        or effect.status_enchant_rule is not None
    ):
        raise InitialRegularPlan2LessonGimmickError(
            "plan2-lesson-gimmick-effect-shape-unsupported", effect.id
        )
    if effect.effect_type not in {
        EFFECT_BLOCK,
        EFFECT_GOOD_IMPRESSION,
        EFFECT_STAMINA_DAMAGE,
    }:
        raise InitialRegularPlan2LessonGimmickError(
            "plan2-lesson-gimmick-effect-unsupported",
            f"{effect.id}:{effect.effect_type}",
        )

    def execute(state: object, catalog: object) -> Plan2ScheduledGimmickHookResult:
        if not isinstance(state, Plan2NativeHorizonState):
            raise TypeError("gimmick state must be Plan2NativeHorizonState")
        if not isinstance(catalog, Plan2NativeProgramCatalog):
            raise TypeError("gimmick catalog must be Plan2NativeProgramCatalog")
        if state.scalar.current_turn != step.start_turn:
            raise ValueError(
                "lesson gimmick StartTurn mismatch:"
                f"expected={step.start_turn}:actual={state.scalar.current_turn}"
            )
        fires = _step_fires(step, state)
        condition_trace = (
            "lesson-gimmick-gate:"
            f"{effect.id}:review={state.scalar.review}:"
            f"field={step.field_status_type}:value={step.field_status_value}:"
            f"check={step.field_status_check_type}:fires={str(fires).lower()}"
        )
        if not fires:
            return Plan2ScheduledGimmickHookResult(state, (condition_trace,))
        source_id = f"gimmick:{step.group_id}:priority={step.priority}"
        if effect.effect_type == EFFECT_BLOCK:
            applied = apply_plan2_native_external_block_effect(
                state,
                catalog,
                Plan2NativeExternalBlockEffect(
                    source_id, effect.id, effect.value1, step.priority
                ),
            )
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION:
            applied = apply_plan2_native_external_review_effect(
                state,
                catalog,
                Plan2NativeExternalReviewEffect(
                    source_id, effect.id, effect.value1, step.priority
                ),
            )
        else:
            applied = apply_plan2_native_external_stamina_damage_effect(
                state,
                catalog,
                Plan2NativeExternalStaminaDamageEffect(
                    source_id, effect.id, effect.value1, step.priority
                ),
            )
        return Plan2ScheduledGimmickHookResult(
            applied.state,
            (condition_trace, *applied.trace),
        )

    return Plan2ScheduledGimmickHook(
        effect_id=effect.id,
        executor_id=(
            "initial-regular.plan2.lesson-gimmick."
            f"{step.group_id}.priority-{step.priority}.{effect.id}.v1"
        ),
        executor=execute,
    )


def build_initial_regular_plan2_lesson_gimmick_hooks(
    raw_schedule: object,
    *,
    source: Path = DEFAULT_GIMMICK_SOURCE,
    database: Path = DEFAULT_DATABASE,
) -> Mapping[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook]:
    """Join one exact LocalSave schedule to Master and return typed hooks."""

    if not isinstance(raw_schedule, list):
        raise InitialRegularPlan2LessonGimmickError(
            "plan2-lesson-gimmick-schedule-invalid"
        )
    hooks: dict[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook] = {}
    profiles: dict[str, tuple[LessonGimmickStep, ...]] = {}
    for index, row in enumerate(raw_schedule):
        if not isinstance(row, Mapping) or set(row) != _ROW_FIELDS:
            raise InitialRegularPlan2LessonGimmickError(
                "plan2-lesson-gimmick-row-invalid", str(index)
            )
        turn = row["turn"]
        group_id = row["gimmickGroupId"]
        effect_id = row["gimmickEffectId"]
        if (
            isinstance(turn, bool)
            or not isinstance(turn, int)
            or turn < 1
            or not isinstance(group_id, str)
            or not group_id
            or not isinstance(effect_id, str)
            or not effect_id
        ):
            raise InitialRegularPlan2LessonGimmickError(
                "plan2-lesson-gimmick-row-invalid", str(index)
            )
        if group_id not in profiles:
            profiles[group_id] = load_lesson_gimmick_profile(
                group_id, source=Path(source), database=Path(database)
            ).steps
        matches = tuple(
            step
            for step in profiles[group_id]
            if step.start_turn == turn and step.effect.id == effect_id
        )
        if len(matches) != 1:
            raise InitialRegularPlan2LessonGimmickError(
                "plan2-lesson-gimmick-master-row-mismatch",
                f"{index}:{group_id}:turn={turn}:effect={effect_id}",
            )
        key = plan2_scheduled_gimmick_hook_key(turn, group_id, effect_id)
        if key in hooks:
            raise InitialRegularPlan2LessonGimmickError(
                "plan2-lesson-gimmick-schedule-row-ambiguous",
                f"{group_id}:turn={turn}:effect={effect_id}",
            )
        hooks[key] = _hook_for_step(matches[0])
    return MappingProxyType(hooks)


__all__ = [
    "InitialRegularPlan2LessonGimmickError",
    "build_initial_regular_plan2_lesson_gimmick_hooks",
]
