"""Static lesson-gimmick rules used by the current visible lesson slice."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import yaml

from .logic_engine import (
    EFFECT_BLOCK,
    EFFECT_GOOD_IMPRESSION,
    EFFECT_MOTIVATION,
    LogicExamState,
    MasterCardEffect,
    load_master_effect,
    scale_good_impression_gain,
)
from .master_db import DEFAULT_DATABASE


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GIMMICK_SOURCE = (
    PROJECT_ROOT / "_research" / "gakumasu-diff" / "ProduceExamGimmickEffectGroup.yaml"
)

# Fingerprinted from the in-game rule panel for the current 11-turn Dance
# pursuit lesson: turn 2 block +6, then the 4/9/10 Review thresholds on turns
# 5/7/8.  The same schedule also exists under the generic after-mid alias.
LIVE_GIMMICK_GROUP_ID = "p_e_gim-exam_review-2-produce-002-da-hard-final"

FIELD_UNKNOWN = "ProduceExamFieldStatusType_Unknown"
FIELD_REVIEW = "ProduceExamFieldStatusType_ReviewUp"
FIELD_BLOCK = "ProduceExamFieldStatusType_BlockUp"
FIELD_MOTIVATION = "ProduceExamFieldStatusType_CardPlayAggressiveUp"
CHECK_UNKNOWN = "ProduceExamTriggerCheckType_Unknown"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
EFFECT_STAMINA_DAMAGE = "ProduceExamEffectType_ExamStaminaDamage"
EFFECT_CARD_CREATE_ID = "ProduceExamEffectType_ExamCardCreateId"


@dataclass(frozen=True, slots=True)
class LessonGimmickStep:
    group_id: str
    priority: int
    start_turn: int
    remaining_turn_permille: int
    field_status_type: str
    field_status_value: int
    field_status_check_type: str
    effect: MasterCardEffect


@dataclass(frozen=True, slots=True)
class LessonGimmickProfile:
    id: str
    steps: tuple[LessonGimmickStep, ...]


@dataclass(frozen=True, slots=True)
class GimmickResolution:
    before: LogicExamState
    after: LogicExamState
    fired_effect_ids: tuple[str, ...]
    unsupported_rules: tuple[str, ...]

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported_rules


@lru_cache(maxsize=8)
def load_lesson_gimmick_profile(
    group_id: str = LIVE_GIMMICK_GROUP_ID,
    source: Path = DEFAULT_GIMMICK_SOURCE,
    database: Path = DEFAULT_DATABASE,
) -> LessonGimmickProfile:
    """Load one exact repeated-ID schedule from the static Master YAML."""

    if not source.is_file():
        raise FileNotFoundError(f"找不到課程 gimmick Master：{source}")
    rows = yaml.load(source.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
    if not isinstance(rows, list):
        raise ValueError("ProduceExamGimmickEffectGroup Master 不是 list")
    selected = [row for row in rows if isinstance(row, dict) and row.get("id") == group_id]
    if not selected:
        raise KeyError(f"Master 找不到課程 gimmick group：{group_id}")
    steps = []
    for row in selected:
        effect_id = row.get("produceExamEffectId")
        if not isinstance(effect_id, str) or not effect_id:
            raise ValueError(f"gimmick row 缺少 effect id：{group_id}")
        steps.append(
            LessonGimmickStep(
                group_id=group_id,
                priority=int(row.get("priority", 0)),
                start_turn=int(row.get("startTurn", 0)),
                remaining_turn_permille=int(row.get("remainingTurnPermil", 0)),
                field_status_type=str(row.get("fieldStatusType", "")),
                field_status_value=int(row.get("fieldStatusValue", 0)),
                field_status_check_type=str(row.get("fieldStatusCheckType", "")),
                effect=load_master_effect(effect_id, database),
            )
        )
    steps.sort(key=lambda step: step.priority)
    return LessonGimmickProfile(group_id, tuple(steps))


def _condition_fires(
    step: LessonGimmickStep, state: LogicExamState
) -> tuple[bool, str | None]:
    if (
        step.field_status_type == FIELD_UNKNOWN
        and step.field_status_check_type == CHECK_UNKNOWN
    ):
        return True, None
    if step.field_status_type == FIELD_REVIEW:
        if step.field_status_check_type == CHECK_UNKNOWN:
            return state.good_impression >= step.field_status_value, None
        if step.field_status_check_type == CHECK_NOT:
            return state.good_impression < step.field_status_value, None
    if step.field_status_type == FIELD_BLOCK:
        if step.field_status_check_type == CHECK_UNKNOWN:
            return state.block >= step.field_status_value, None
        if step.field_status_check_type == CHECK_NOT:
            return state.block < step.field_status_value, None
    if step.field_status_type == FIELD_MOTIVATION:
        if step.field_status_check_type == CHECK_UNKNOWN:
            return state.motivation >= step.field_status_value, None
        if step.field_status_check_type == CHECK_NOT:
            return state.motivation < step.field_status_value, None
    return False, (
        f"gimmick-condition:{step.field_status_check_type}:"
        f"{step.field_status_type}:{step.priority}"
    )


def resolve_lesson_turn_start(
    profile: LessonGimmickProfile, state: LogicExamState
) -> GimmickResolution:
    """Apply the verified subset of scheduled turn-start effects."""

    current = state
    fired: list[str] = []
    unsupported: list[str] = []
    for step in profile.steps:
        if step.start_turn != state.round_number:
            continue
        if step.remaining_turn_permille != 0:
            unsupported.append(f"gimmick-remaining-turn:{step.priority}")
            continue
        does_fire, condition_unsupported = _condition_fires(step, current)
        if condition_unsupported:
            unsupported.append(condition_unsupported)
            continue
        if not does_fire:
            continue
        effect = step.effect
        if effect.effect_type == EFFECT_BLOCK:
            current = replace(
                current, block=current.block + effect.value1 + current.motivation
            )
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION:
            gain_bonus = sum(
                value
                for value, _turns in current.good_impression_gain_bonus_layers
            )
            current = replace(
                current,
                good_impression=(
                    current.good_impression
                    + scale_good_impression_gain(effect.value1, gain_bonus)
                ),
            )
        elif effect.effect_type == EFFECT_MOTIVATION:
            gain_bonus = sum(
                value
                for value, _turns in current.motivation_gain_bonus_layers
            )
            current = replace(
                current,
                motivation=(
                    current.motivation
                    + scale_good_impression_gain(effect.value1, gain_bonus)
                ),
            )
        elif effect.effect_type == EFFECT_STAMINA_DAMAGE:
            current = replace(
                current,
                stamina=max(0, current.stamina - effect.value1),
            )
        elif effect.effect_type == EFFECT_CARD_CREATE_ID:
            # This mutates the future draw pile but not any visible numerical
            # HUD field. Every subsequently drawn card is still identified
            # from the live hand before ranking, so the shadow state remains
            # exact for the current turn.
            pass
        else:
            unsupported.append(f"gimmick-effect:{effect.effect_type}:{effect.id}")
            continue
        fired.append(effect.id)
    return GimmickResolution(
        before=state,
        after=current,
        fired_effect_ids=tuple(fired),
        unsupported_rules=tuple(unsupported),
    )
