"""Typed, read-only inventory of the Master-owned N.I.A. surfaces.

The existing :mod:`nia_static_adapter` performs the authoritative YAML join
for N.I.A. Pro (``produce-004``) and Master (``produce-005``).  This module
projects that join into a compact contract suitable for a GUI or an audit
panel.  It intentionally does not replay a random pool, choose reward rolls,
read a LocalSave, run a search, or control a device.

Runtime-owned gaps are represented as typed blockers.  In particular, a
missing ``ProduceSchedule`` is not replaced with a guessed weekly route, and
the static turn-parameter recipe is never presented as an observed roll.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .audition_rules import FINAL, MID1, MID2
from .master_db import DEFAULT_DATABASE, get_idol_profile
from .nia_outer_advisor import load_nia_outer_mode_rules
from .nia_static_adapter import (
    DEFAULT_MASTER_DIR,
    NIA_PRODUCE_IDS,
    NiaAuditionDefinition,
    NiaGimmickStep,
    NiaItemDefinition,
    NiaItemEffectDefinition,
    NiaNpcScoreRange,
    NiaProduceSetting,
    NiaScoreCurvePoint,
    NiaStaticBundle,
    NiaStaticDiagnostic,
    NiaTurnParameterSchedule,
    load_nia_static_bundle,
)
from .route_calendar import load_route_calendar


SCHEMA_NAME = "gkms_tool.nia_static_inventory"
SCHEMA_VERSION = 1
NIA_PLAN_TYPE = "ProducePlanType_Plan3"


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{label} must be text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be bool")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _object_list(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{label} must be a list of objects")
    return tuple(value)  # type: ignore[return-value]


def _text_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _enum_text(value: object, allowed: set[str] | frozenset[str], label: str) -> str:
    text = _text(value, label)
    if text not in allowed:
        raise ValueError(f"invalid {label}: {text!r}")
    return text


@dataclass(frozen=True, slots=True)
class NiaStaticInventoryBlocker:
    """One Master/runtime boundary which static data cannot resolve."""

    code: str
    detail: str
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("inventory blocker code is required")
        if not isinstance(self.detail, str):
            raise TypeError("inventory blocker detail must be text")
        if any(not value for value in self.evidence_ids):
            raise ValueError("inventory blocker evidence IDs must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "detail": self.detail,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticInventoryBlocker":
        return cls(
            code=_text(payload.get("code"), "blocker.code"),
            detail=_text(payload.get("detail"), "blocker.detail", allow_empty=True),
            evidence_ids=_text_list(payload.get("evidence_ids", []), "blocker.evidence_ids"),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticMilestone:
    week: int
    step_type: str

    def __post_init__(self) -> None:
        if self.week < 1:
            raise ValueError("milestone week must be positive")
        _enum_text(self.step_type, {MID1, MID2, FINAL}, "milestone.step_type")

    @property
    def stage_id(self) -> str:
        return f"{self.step_type}:{self.week}"

    def to_dict(self) -> dict[str, object]:
        return {"week": self.week, "step_type": self.step_type, "stage_id": self.stage_id}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticMilestone":
        return cls(
            week=_integer(payload.get("week"), "milestone.week", minimum=1),
            step_type=_text(payload.get("step_type"), "milestone.step_type"),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticLessonStage:
    id: str
    progress_level: int
    stamina: int
    parameter: int

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("lesson stage id is required")
        if min(self.progress_level, self.stamina, self.parameter) < 0:
            raise ValueError("lesson stage values cannot be negative")

    @property
    def stage_id(self) -> str:
        return self.id

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "progress_level": self.progress_level,
            "stamina": self.stamina,
            "parameter": self.parameter,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticLessonStage":
        return cls(
            id=_text(payload.get("id"), "lesson.id"),
            progress_level=_integer(payload.get("progress_level"), "lesson.progress_level"),
            stamina=_integer(payload.get("stamina"), "lesson.stamina"),
            parameter=_integer(payload.get("parameter"), "lesson.parameter"),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticScoreCurvePoint:
    parameter: int
    vocal_permille: int
    dance_permille: int
    visual_permille: int

    def __post_init__(self) -> None:
        if min(
            self.parameter,
            self.vocal_permille,
            self.dance_permille,
            self.visual_permille,
        ) < 0:
            raise ValueError("score curve values cannot be negative")

    def to_dict(self) -> dict[str, int]:
        return {
            "parameter": self.parameter,
            "vocal_permille": self.vocal_permille,
            "dance_permille": self.dance_permille,
            "visual_permille": self.visual_permille,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticScoreCurvePoint":
        return cls(
            parameter=_integer(payload.get("parameter"), "score.parameter"),
            vocal_permille=_integer(payload.get("vocal_permille"), "score.vocal_permille"),
            dance_permille=_integer(payload.get("dance_permille"), "score.dance_permille"),
            visual_permille=_integer(payload.get("visual_permille"), "score.visual_permille"),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticNpcScoreRange:
    number: int
    character_id: str | None
    mob_id: str | None
    mob_name: str
    score_min: int
    score_max: int
    vocal_permille: int
    dance_permille: int
    visual_permille: int
    opening_permille: int
    middle_permille: int
    ending_permille: int

    def __post_init__(self) -> None:
        if self.number < 1 or self.score_min < 0 or self.score_max < self.score_min:
            raise ValueError("NPC score range is invalid")
        if (self.character_id is None) == (self.mob_id is None):
            raise ValueError("NPC score range requires exactly one character or mob")
        if self.character_id is not None:
            _text(self.character_id, "npc.character_id")
        if self.mob_id is not None:
            _text(self.mob_id, "npc.mob_id")
        _text(self.mob_name, "npc.mob_name", allow_empty=True)
        if min(
            self.vocal_permille,
            self.dance_permille,
            self.visual_permille,
            self.opening_permille,
            self.middle_permille,
            self.ending_permille,
        ) < 0:
            raise ValueError("NPC multipliers cannot be negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "number": self.number,
            "character_id": self.character_id,
            "mob_id": self.mob_id,
            "mob_name": self.mob_name,
            "score_min": self.score_min,
            "score_max": self.score_max,
            "vocal_permille": self.vocal_permille,
            "dance_permille": self.dance_permille,
            "visual_permille": self.visual_permille,
            "opening_permille": self.opening_permille,
            "middle_permille": self.middle_permille,
            "ending_permille": self.ending_permille,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticNpcScoreRange":
        return cls(
            number=_integer(payload.get("number"), "npc.number", minimum=1),
            character_id=_optional_text(
                payload.get("character_id"), "npc.character_id"
            ),
            mob_id=_optional_text(payload.get("mob_id"), "npc.mob_id"),
            mob_name=_text(payload.get("mob_name"), "npc.mob_name", allow_empty=True),
            score_min=_integer(payload.get("score_min"), "npc.score_min"),
            score_max=_integer(payload.get("score_max"), "npc.score_max"),
            vocal_permille=_integer(payload.get("vocal_permille"), "npc.vocal_permille"),
            dance_permille=_integer(payload.get("dance_permille"), "npc.dance_permille"),
            visual_permille=_integer(payload.get("visual_permille"), "npc.visual_permille"),
            opening_permille=_integer(payload.get("opening_permille"), "npc.opening_permille"),
            middle_permille=_integer(payload.get("middle_permille"), "npc.middle_permille"),
            ending_permille=_integer(payload.get("ending_permille"), "npc.ending_permille"),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticGimmickStep:
    priority: int
    start_turn: int
    remaining_turn_permille: int
    remaining_turn: int
    field_status_type: str
    field_status_value: int
    field_status_check_type: str
    field_status_card_search_id: str
    effect_id: str
    is_positive: bool

    def __post_init__(self) -> None:
        if min(
            self.priority,
            self.start_turn,
            self.remaining_turn_permille,
            self.remaining_turn,
            self.field_status_value,
        ) < 0:
            raise ValueError("gimmick values cannot be negative")
        if not self.effect_id:
            raise ValueError("gimmick effect ID is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "priority": self.priority,
            "start_turn": self.start_turn,
            "remaining_turn_permille": self.remaining_turn_permille,
            "remaining_turn": self.remaining_turn,
            "field_status_type": self.field_status_type,
            "field_status_value": self.field_status_value,
            "field_status_check_type": self.field_status_check_type,
            "field_status_card_search_id": self.field_status_card_search_id,
            "effect_id": self.effect_id,
            "is_positive": self.is_positive,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticGimmickStep":
        return cls(
            priority=_integer(payload.get("priority"), "gimmick.priority"),
            start_turn=_integer(payload.get("start_turn"), "gimmick.start_turn"),
            remaining_turn_permille=_integer(
                payload.get("remaining_turn_permille"), "gimmick.remaining_turn_permille"
            ),
            remaining_turn=_integer(payload.get("remaining_turn"), "gimmick.remaining_turn"),
            field_status_type=_text(
                payload.get("field_status_type"), "gimmick.field_status_type"
            ),
            field_status_value=_integer(
                payload.get("field_status_value"), "gimmick.field_status_value"
            ),
            field_status_check_type=_text(
                payload.get("field_status_check_type"), "gimmick.field_status_check_type"
            ),
            field_status_card_search_id=_text(
                payload.get("field_status_card_search_id"),
                "gimmick.field_status_card_search_id",
                allow_empty=True,
            ),
            effect_id=_text(payload.get("effect_id"), "gimmick.effect_id"),
            is_positive=_boolean(payload.get("is_positive"), "gimmick.is_positive"),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticAuditionStage:
    stage_id: str
    difficulty_row_id: str
    step_type: str
    number: int
    audition_type: str
    battle_config_id: str
    score_config_id: str
    npc_group_id: str
    gimmick_group_id: str
    turns: int
    clear_border: int
    limit_border: int
    parameter_base_line: int
    base_score: int
    force_end_score: int
    is_static_npc_score: bool
    vote_count_baseline: int
    vote_count: int
    dearness_level: int
    vocal_parameter: int
    dance_parameter: int
    visual_parameter: int
    exam_setting_id: str
    turn_end_stamina_recovery: int
    hand_limit: int
    turn_start_distribute: int
    score_curve: tuple[NiaStaticScoreCurvePoint, ...]
    npc_scores: tuple[NiaStaticNpcScoreRange, ...]
    gimmicks: tuple[NiaStaticGimmickStep, ...]

    def __post_init__(self) -> None:
        if not self.stage_id or not self.difficulty_row_id:
            raise ValueError("audition stage identity is required")
        _enum_text(self.step_type, {MID1, MID2, FINAL}, "audition.step_type")
        if self.number < 1 or self.turns < 1:
            raise ValueError("audition number/turns are invalid")
        if self.clear_border < 1 or self.limit_border < 0:
            raise ValueError("audition borders are invalid")
        for value in (
            self.parameter_base_line,
            self.base_score,
            self.force_end_score,
            self.vote_count_baseline,
            self.vote_count,
            self.dearness_level,
            self.vocal_parameter,
            self.dance_parameter,
            self.visual_parameter,
            self.turn_end_stamina_recovery,
            self.hand_limit,
            self.turn_start_distribute,
        ):
            if value < 0:
                raise ValueError("audition values cannot be negative")

    @property
    def rank_threshold(self) -> int:
        return self.clear_border

    @property
    def npc_count(self) -> int:
        return len(self.npc_scores)

    @property
    def gimmick_count(self) -> int:
        return len(self.gimmicks)

    def to_dict(self) -> dict[str, object]:
        return {
            "stage_id": self.stage_id,
            "difficulty_row_id": self.difficulty_row_id,
            "step_type": self.step_type,
            "number": self.number,
            "audition_type": self.audition_type,
            "battle_config_id": self.battle_config_id,
            "score_config_id": self.score_config_id,
            "npc_group_id": self.npc_group_id,
            "gimmick_group_id": self.gimmick_group_id,
            "turns": self.turns,
            "clear_border": self.clear_border,
            "limit_border": self.limit_border,
            "parameter_base_line": self.parameter_base_line,
            "base_score": self.base_score,
            "force_end_score": self.force_end_score,
            "is_static_npc_score": self.is_static_npc_score,
            "vote_count_baseline": self.vote_count_baseline,
            "vote_count": self.vote_count,
            "dearness_level": self.dearness_level,
            "vocal_parameter": self.vocal_parameter,
            "dance_parameter": self.dance_parameter,
            "visual_parameter": self.visual_parameter,
            "exam_setting_id": self.exam_setting_id,
            "turn_end_stamina_recovery": self.turn_end_stamina_recovery,
            "hand_limit": self.hand_limit,
            "turn_start_distribute": self.turn_start_distribute,
            "score_curve": [point.to_dict() for point in self.score_curve],
            "npc_scores": [npc.to_dict() for npc in self.npc_scores],
            "gimmicks": [gimmick.to_dict() for gimmick in self.gimmicks],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticAuditionStage":
        score_curve = _object_list(payload.get("score_curve"), "audition.score_curve")
        npc_scores = _object_list(payload.get("npc_scores"), "audition.npc_scores")
        gimmicks = _object_list(payload.get("gimmicks"), "audition.gimmicks")
        return cls(
            stage_id=_text(payload.get("stage_id"), "audition.stage_id"),
            difficulty_row_id=_text(
                payload.get("difficulty_row_id"), "audition.difficulty_row_id"
            ),
            step_type=_text(payload.get("step_type"), "audition.step_type"),
            number=_integer(payload.get("number"), "audition.number", minimum=1),
            audition_type=_text(payload.get("audition_type"), "audition.audition_type"),
            battle_config_id=_text(payload.get("battle_config_id"), "audition.battle_config_id"),
            score_config_id=_text(payload.get("score_config_id"), "audition.score_config_id"),
            npc_group_id=_text(payload.get("npc_group_id"), "audition.npc_group_id"),
            gimmick_group_id=_text(
                payload.get("gimmick_group_id"), "audition.gimmick_group_id"
            ),
            turns=_integer(payload.get("turns"), "audition.turns", minimum=1),
            clear_border=_integer(
                payload.get("clear_border"), "audition.clear_border", minimum=1
            ),
            limit_border=_integer(payload.get("limit_border"), "audition.limit_border"),
            parameter_base_line=_integer(
                payload.get("parameter_base_line"), "audition.parameter_base_line"
            ),
            base_score=_integer(payload.get("base_score"), "audition.base_score"),
            force_end_score=_integer(
                payload.get("force_end_score"), "audition.force_end_score"
            ),
            is_static_npc_score=_boolean(
                payload.get("is_static_npc_score"), "audition.is_static_npc_score"
            ),
            vote_count_baseline=_integer(
                payload.get("vote_count_baseline"), "audition.vote_count_baseline"
            ),
            vote_count=_integer(payload.get("vote_count"), "audition.vote_count"),
            dearness_level=_integer(
                payload.get("dearness_level"), "audition.dearness_level"
            ),
            vocal_parameter=_integer(
                payload.get("vocal_parameter"), "audition.vocal_parameter"
            ),
            dance_parameter=_integer(
                payload.get("dance_parameter"), "audition.dance_parameter"
            ),
            visual_parameter=_integer(
                payload.get("visual_parameter"), "audition.visual_parameter"
            ),
            exam_setting_id=_text(payload.get("exam_setting_id"), "audition.exam_setting_id"),
            turn_end_stamina_recovery=_integer(
                payload.get("turn_end_stamina_recovery"),
                "audition.turn_end_stamina_recovery",
            ),
            hand_limit=_integer(payload.get("hand_limit"), "audition.hand_limit"),
            turn_start_distribute=_integer(
                payload.get("turn_start_distribute"),
                "audition.turn_start_distribute",
            ),
            score_curve=tuple(
                NiaStaticScoreCurvePoint.from_dict(value) for value in score_curve
            ),
            npc_scores=tuple(
                NiaStaticNpcScoreRange.from_dict(value) for value in npc_scores
            ),
            gimmicks=tuple(
                NiaStaticGimmickStep.from_dict(value) for value in gimmicks
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticProduceSetting:
    initial_produce_point: int
    refresh_stamina_recovery_permille: int
    before_audition_refresh_stamina_recovery_permille: int
    step_skip_stamina_recovery_permille: int
    customize_produce_card_count: int
    continue_count: int
    audition_trend_upper_permille: int
    audition_trend_lower_permille: int

    def to_dict(self) -> dict[str, int]:
        return {
            "initial_produce_point": self.initial_produce_point,
            "refresh_stamina_recovery_permille": self.refresh_stamina_recovery_permille,
            "before_audition_refresh_stamina_recovery_permille": self.before_audition_refresh_stamina_recovery_permille,
            "step_skip_stamina_recovery_permille": self.step_skip_stamina_recovery_permille,
            "customize_produce_card_count": self.customize_produce_card_count,
            "continue_count": self.continue_count,
            "audition_trend_upper_permille": self.audition_trend_upper_permille,
            "audition_trend_lower_permille": self.audition_trend_lower_permille,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticProduceSetting":
        return cls(
            initial_produce_point=_integer(
                payload.get("initial_produce_point"), "setting.initial_produce_point"
            ),
            refresh_stamina_recovery_permille=_integer(
                payload.get("refresh_stamina_recovery_permille"),
                "setting.refresh_stamina_recovery_permille",
            ),
            before_audition_refresh_stamina_recovery_permille=_integer(
                payload.get("before_audition_refresh_stamina_recovery_permille"),
                "setting.before_audition_refresh_stamina_recovery_permille",
            ),
            step_skip_stamina_recovery_permille=_integer(
                payload.get("step_skip_stamina_recovery_permille"),
                "setting.step_skip_stamina_recovery_permille",
            ),
            customize_produce_card_count=_integer(
                payload.get("customize_produce_card_count"),
                "setting.customize_produce_card_count",
            ),
            continue_count=_integer(payload.get("continue_count"), "setting.continue_count"),
            audition_trend_upper_permille=_integer(
                payload.get("audition_trend_upper_permille"),
                "setting.audition_trend_upper_permille",
            ),
            audition_trend_lower_permille=_integer(
                payload.get("audition_trend_lower_permille"),
                "setting.audition_trend_lower_permille",
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticCoverage:
    milestones: int
    lesson_stages: int
    audition_stages: int
    score_curves: int
    npc_groups: int
    gimmick_groups: int
    blockers: int

    def __post_init__(self) -> None:
        if min(
            self.milestones,
            self.lesson_stages,
            self.audition_stages,
            self.score_curves,
            self.npc_groups,
            self.gimmick_groups,
            self.blockers,
        ) < 0:
            raise ValueError("coverage counts cannot be negative")

    @property
    def total_stages(self) -> int:
        return self.lesson_stages + self.audition_stages

    def to_dict(self) -> dict[str, int]:
        return {
            "milestones": self.milestones,
            "lesson_stages": self.lesson_stages,
            "audition_stages": self.audition_stages,
            "score_curves": self.score_curves,
            "npc_groups": self.npc_groups,
            "gimmick_groups": self.gimmick_groups,
            "blockers": self.blockers,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticCoverage":
        return cls(
            milestones=_integer(payload.get("milestones"), "coverage.milestones"),
            lesson_stages=_integer(payload.get("lesson_stages"), "coverage.lesson_stages"),
            audition_stages=_integer(
                payload.get("audition_stages"), "coverage.audition_stages"
            ),
            score_curves=_integer(payload.get("score_curves"), "coverage.score_curves"),
            npc_groups=_integer(payload.get("npc_groups"), "coverage.npc_groups"),
            gimmick_groups=_integer(
                payload.get("gimmick_groups"), "coverage.gimmick_groups"
            ),
            blockers=_integer(payload.get("blockers"), "coverage.blockers"),
        )


@dataclass(frozen=True, slots=True)
class NiaStaticInventory:
    idol_card_id: str
    produce_id: str
    difficulty: str
    plan_type: str | None
    group_id: str
    group_type: str
    total_weeks: int
    milestones: tuple[NiaStaticMilestone, ...]
    lesson_stage_ids: tuple[str, ...]
    audition_stage_ids: tuple[str, ...]
    lessons: tuple[NiaStaticLessonStage, ...]
    auditions: tuple[NiaStaticAuditionStage, ...]
    base_step_level: int
    max_refresh_count: int
    action_point_quantity: int
    parameter_growth_limit: int
    exam_setting_id: str
    produce_setting_id: str
    force_common_live: bool
    easy_item_ids: tuple[str, ...]
    live_types: tuple[str, ...]
    setting: NiaStaticProduceSetting
    score_penalty_min_permille: int
    score_penalty_max_permille: int
    blockers: tuple[NiaStaticInventoryBlocker, ...]

    def __post_init__(self) -> None:
        if not self.idol_card_id or self.produce_id not in NIA_PRODUCE_IDS:
            raise ValueError("N.I.A. inventory identity is invalid")
        if self.difficulty != NIA_PRODUCE_IDS[self.produce_id]:
            raise ValueError("N.I.A. inventory difficulty does not match produce_id")
        if self.plan_type is not None and not self.plan_type:
            raise ValueError("plan_type cannot be empty")
        if self.total_weeks < 1:
            raise ValueError("total_weeks must be positive")
        if len(self.milestones) != 3:
            raise ValueError("N.I.A. inventory requires three milestones")
        if tuple(stage.stage_id for stage in self.lessons) != self.lesson_stage_ids:
            raise ValueError("lesson_stage_ids do not match lessons")
        if tuple(stage.stage_id for stage in self.auditions) != self.audition_stage_ids:
            raise ValueError("audition_stage_ids do not match auditions")
        if any(not isinstance(blocker, NiaStaticInventoryBlocker) for blocker in self.blockers):
            raise TypeError("blockers must contain NiaStaticInventoryBlocker values")

    @property
    def coverage(self) -> NiaStaticCoverage:
        return NiaStaticCoverage(
            milestones=len(self.milestones),
            lesson_stages=len(self.lessons),
            audition_stages=len(self.auditions),
            score_curves=sum(bool(stage.score_curve) for stage in self.auditions),
            npc_groups=len({stage.npc_group_id for stage in self.auditions}),
            gimmick_groups=len({stage.gimmick_group_id for stage in self.auditions}),
            blockers=len(self.blockers),
        )

    @property
    def coverage_counts(self) -> dict[str, int]:
        return self.coverage.to_dict()

    @property
    def summary(self) -> str:
        blocker_text = "none" if not self.blockers else ",".join(
            blocker.code for blocker in self.blockers
        )
        return (
            f"N.I.A. {self.difficulty} ({self.produce_id}): {self.total_weeks} weeks; "
            f"milestones={len(self.milestones)}; lessons={len(self.lessons)}; "
            f"auditions={len(self.auditions)}; blockers={blocker_text}; "
            "static Master inventory only (no rolled rewards or authentic LocalSave)"
        )

    @property
    def summary_text(self) -> str:
        return self.summary

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "idol_card_id": self.idol_card_id,
            "produce_id": self.produce_id,
            "difficulty": self.difficulty,
            "plan_type": self.plan_type,
            "group_id": self.group_id,
            "group_type": self.group_type,
            "total_weeks": self.total_weeks,
            "milestones": [milestone.to_dict() for milestone in self.milestones],
            "lesson_stage_ids": list(self.lesson_stage_ids),
            "audition_stage_ids": list(self.audition_stage_ids),
            "lessons": [lesson.to_dict() for lesson in self.lessons],
            "auditions": [audition.to_dict() for audition in self.auditions],
            "base_step_level": self.base_step_level,
            "max_refresh_count": self.max_refresh_count,
            "action_point_quantity": self.action_point_quantity,
            "parameter_growth_limit": self.parameter_growth_limit,
            "exam_setting_id": self.exam_setting_id,
            "produce_setting_id": self.produce_setting_id,
            "force_common_live": self.force_common_live,
            "easy_item_ids": list(self.easy_item_ids),
            "live_types": list(self.live_types),
            "setting": self.setting.to_dict(),
            "score_penalty_min_permille": self.score_penalty_min_permille,
            "score_penalty_max_permille": self.score_penalty_max_permille,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "coverage": self.coverage.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaStaticInventory":
        if payload.get("schema") != SCHEMA_NAME or payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported N.I.A. static inventory schema")
        milestones = _object_list(payload.get("milestones"), "milestones")
        lessons = _object_list(payload.get("lessons"), "lessons")
        auditions = _object_list(payload.get("auditions"), "auditions")
        blockers = _object_list(payload.get("blockers"), "blockers")
        return cls(
            idol_card_id=_text(payload.get("idol_card_id"), "idol_card_id"),
            produce_id=_text(payload.get("produce_id"), "produce_id"),
            difficulty=_text(payload.get("difficulty"), "difficulty"),
            plan_type=_optional_text(payload.get("plan_type"), "plan_type"),
            group_id=_text(payload.get("group_id"), "group_id"),
            group_type=_text(payload.get("group_type"), "group_type"),
            total_weeks=_integer(payload.get("total_weeks"), "total_weeks", minimum=1),
            milestones=tuple(NiaStaticMilestone.from_dict(value) for value in milestones),
            lesson_stage_ids=_text_list(payload.get("lesson_stage_ids"), "lesson_stage_ids"),
            audition_stage_ids=_text_list(
                payload.get("audition_stage_ids"), "audition_stage_ids"
            ),
            lessons=tuple(NiaStaticLessonStage.from_dict(value) for value in lessons),
            auditions=tuple(
                NiaStaticAuditionStage.from_dict(value) for value in auditions
            ),
            base_step_level=_integer(payload.get("base_step_level"), "base_step_level"),
            max_refresh_count=_integer(
                payload.get("max_refresh_count"), "max_refresh_count"
            ),
            action_point_quantity=_integer(
                payload.get("action_point_quantity"), "action_point_quantity"
            ),
            parameter_growth_limit=_integer(
                payload.get("parameter_growth_limit"), "parameter_growth_limit"
            ),
            exam_setting_id=_text(payload.get("exam_setting_id"), "exam_setting_id"),
            produce_setting_id=_text(
                payload.get("produce_setting_id"), "produce_setting_id"
            ),
            force_common_live=_boolean(
                payload.get("force_common_live"), "force_common_live"
            ),
            easy_item_ids=_text_list(payload.get("easy_item_ids"), "easy_item_ids"),
            live_types=_text_list(payload.get("live_types"), "live_types"),
            setting=NiaStaticProduceSetting.from_dict(
                _mapping(payload.get("setting"), "setting")
            ),
            score_penalty_min_permille=_integer(
                payload.get("score_penalty_min_permille"),
                "score_penalty_min_permille",
            ),
            score_penalty_max_permille=_integer(
                payload.get("score_penalty_max_permille"),
                "score_penalty_max_permille",
            ),
            blockers=tuple(
                NiaStaticInventoryBlocker.from_dict(value) for value in blockers
            ),
        )


def _blockers(bundle: NiaStaticBundle, plan_type: str | None) -> tuple[NiaStaticInventoryBlocker, ...]:
    values = [
        NiaStaticInventoryBlocker(
            diagnostic.code, diagnostic.detail, diagnostic.evidence_ids
        )
        for diagnostic in bundle.diagnostics
    ]
    if plan_type is None:
        values.append(
            NiaStaticInventoryBlocker(
                "idol-profile-unavailable",
                "SQLite IdolProfile is unavailable; plan type is unknown.",
                ("master.sqlite3:idol_card.plan_type",),
            )
        )
    elif plan_type != NIA_PLAN_TYPE:
        values.append(
            NiaStaticInventoryBlocker(
                "idol-plan-type-mismatch",
                f"N.I.A. inventory expected {NIA_PLAN_TYPE}, received {plan_type}.",
                ("master.sqlite3:idol_card.plan_type",),
            )
        )
    return tuple(dict.fromkeys(values))


def _setting(value: NiaProduceSetting) -> NiaStaticProduceSetting:
    return NiaStaticProduceSetting(
        initial_produce_point=value.initial_produce_point,
        refresh_stamina_recovery_permille=value.refresh_stamina_recovery_permille,
        before_audition_refresh_stamina_recovery_permille=(
            value.before_audition_refresh_stamina_recovery_permille
        ),
        step_skip_stamina_recovery_permille=value.step_skip_stamina_recovery_permille,
        customize_produce_card_count=value.customize_produce_card_count,
        continue_count=value.continue_count,
        audition_trend_upper_permille=value.audition_trend_upper_permille,
        audition_trend_lower_permille=value.audition_trend_lower_permille,
    )


def _score_curve(values: Sequence[NiaScoreCurvePoint]) -> tuple[NiaStaticScoreCurvePoint, ...]:
    return tuple(
        NiaStaticScoreCurvePoint(
            point.parameter,
            point.vocal_permille,
            point.dance_permille,
            point.visual_permille,
        )
        for point in values
    )


def _npc_scores(values: Sequence[NiaNpcScoreRange]) -> tuple[NiaStaticNpcScoreRange, ...]:
    return tuple(
        NiaStaticNpcScoreRange(
            number=value.number,
            character_id=value.character_id,
            mob_id=value.mob_id,
            mob_name=value.mob_name,
            score_min=value.score_min,
            score_max=value.score_max,
            vocal_permille=value.vocal_permille,
            dance_permille=value.dance_permille,
            visual_permille=value.visual_permille,
            opening_permille=value.opening_permille,
            middle_permille=value.middle_permille,
            ending_permille=value.ending_permille,
        )
        for value in values
    )


def _gimmicks(values: Sequence[NiaGimmickStep]) -> tuple[NiaStaticGimmickStep, ...]:
    return tuple(
        NiaStaticGimmickStep(
            priority=value.priority,
            start_turn=value.start_turn,
            remaining_turn_permille=value.remaining_turn_permille,
            remaining_turn=value.remaining_turn,
            field_status_type=value.field_status_type,
            field_status_value=value.field_status_value,
            field_status_check_type=value.field_status_check_type,
            field_status_card_search_id=value.field_status_card_search_id,
            effect_id=value.effect_id,
            is_positive=value.is_positive,
        )
        for value in values
    )


def _audition(value: NiaAuditionDefinition) -> NiaStaticAuditionStage:
    rules = value.rules
    return NiaStaticAuditionStage(
        stage_id=f"{rules.step_type}:{rules.number}",
        difficulty_row_id=value.difficulty_key.row_id,
        step_type=rules.step_type,
        number=rules.number,
        audition_type=value.audition_type,
        battle_config_id=rules.battle_config_id,
        score_config_id=rules.score_config_id,
        npc_group_id=rules.npc_group_id,
        gimmick_group_id=rules.gimmick_group_id,
        turns=rules.turns,
        clear_border=value.clear_rank,
        limit_border=rules.force_end_score,
        parameter_base_line=rules.parameter_base_line,
        base_score=rules.base_score,
        force_end_score=rules.force_end_score,
        is_static_npc_score=value.is_static_npc_score,
        vote_count_baseline=value.vote_count_baseline,
        vote_count=value.vote_count,
        dearness_level=value.dearness_level,
        vocal_parameter=rules.vocal_parameter,
        dance_parameter=rules.dance_parameter,
        visual_parameter=rules.visual_parameter,
        exam_setting_id=rules.exam_setting_id,
        turn_end_stamina_recovery=rules.turn_end_stamina_recovery,
        hand_limit=rules.hand_limit,
        turn_start_distribute=rules.turn_start_distribute,
        score_curve=_score_curve(value.score_curve),
        npc_scores=_npc_scores(value.npc_scores),
        gimmicks=_gimmicks(value.gimmicks),
    )


def _profile_plan_type(idol_card_id: str, database: Path) -> str | None:
    profile = get_idol_profile(idol_card_id, database)
    return None if profile is None else profile.plan_type


def build_nia_static_inventory(
    idol_card_id: str,
    *,
    produce_id: str = "produce-004",
    runtime_schedule: Sequence[Mapping[str, object]] | None = None,
    active_item_ids: Sequence[str] | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
    database: Path = DEFAULT_DATABASE,
) -> NiaStaticInventory:
    """Build an N.I.A. Master inventory without running a route or exam."""

    if not isinstance(idol_card_id, str) or not idol_card_id:
        raise ValueError("idol_card_id must be non-empty text")
    if produce_id not in NIA_PRODUCE_IDS:
        raise ValueError(f"unsupported N.I.A. produce_id: {produce_id!r}")
    directory = Path(master_dir).resolve()
    database_path = Path(database).resolve()
    bundle = load_nia_static_bundle(
        idol_card_id,
        produce_id=produce_id,
        runtime_schedule=runtime_schedule,
        active_item_ids=active_item_ids,
        master_dir=directory,
    )
    rules = load_nia_outer_mode_rules(produce_id, master_dir=directory)
    calendar = load_route_calendar(produce_id, master_dir=directory)
    if rules.total_steps != bundle.mode.total_steps or rules.total_steps != calendar.total_weeks:
        raise ValueError("N.I.A. Master mode totals disagree")
    plan_type = _profile_plan_type(idol_card_id, database_path)
    milestones = tuple(
        NiaStaticMilestone(milestone.week, milestone.step_type)
        for milestone in calendar.milestones
    )
    lessons = tuple(
        NiaStaticLessonStage(
            lesson.id,
            lesson.progress_level,
            lesson.stamina,
            lesson.parameter,
        )
        for lesson in bundle.self_lessons
    )
    auditions = tuple(_audition(audition) for audition in bundle.auditions)
    return NiaStaticInventory(
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        difficulty=bundle.mode.difficulty,
        plan_type=plan_type,
        group_id=bundle.mode.group_id,
        group_type=bundle.mode.group_type,
        total_weeks=bundle.mode.total_steps,
        milestones=milestones,
        lesson_stage_ids=tuple(lesson.stage_id for lesson in lessons),
        audition_stage_ids=tuple(audition.stage_id for audition in auditions),
        lessons=lessons,
        auditions=auditions,
        base_step_level=bundle.mode.base_step_level,
        max_refresh_count=bundle.mode.max_refresh_count,
        action_point_quantity=bundle.mode.action_point_quantity,
        parameter_growth_limit=bundle.mode.parameter_growth_limit,
        exam_setting_id=bundle.mode.exam_setting_id,
        produce_setting_id=bundle.mode.produce_setting_id,
        force_common_live=bundle.mode.force_common_live,
        easy_item_ids=bundle.mode.easy_item_ids,
        live_types=bundle.mode.live_types,
        setting=_setting(bundle.mode.setting),
        score_penalty_min_permille=bundle.score_penalty_min_permille,
        score_penalty_max_permille=bundle.score_penalty_max_permille,
        blockers=_blockers(bundle, plan_type),
    )


__all__ = [
    "NIA_PLAN_TYPE",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "NiaStaticAuditionStage",
    "NiaStaticCoverage",
    "NiaStaticGimmickStep",
    "NiaStaticInventory",
    "NiaStaticInventoryBlocker",
    "NiaStaticLessonStage",
    "NiaStaticMilestone",
    "NiaStaticNpcScoreRange",
    "NiaStaticProduceSetting",
    "NiaStaticScoreCurvePoint",
    "build_nia_static_inventory",
]
