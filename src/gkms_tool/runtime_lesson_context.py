"""Native lesson identity and score rules shared by every Plan.

Lessons remain examType=0 with an empty audition schedule. A fixed-axis rule
is a derived view, never a rewritten serializer or an invented Mid1 stage.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from .native_exam_formula import ProduceParameterType


SCHEMA = "gkms.native-lesson-context.v1"
_AXES = ("vocal", "dance", "visual")
_LEGEND_EQUIVALENT = {29: 1, 30: 2, 31: 4, 32: 5, 33: 7, 34: 8}
LESSON_STEP_VALUES = frozenset((*range(1, 10), *_LEGEND_EQUIVALENT))


@dataclass(frozen=True, slots=True)
class LessonStepRule:
    step_type: int
    family: str
    parameter_type: ProduceParameterType
    axis: str
    difficulty: str
    predicate_equivalent_step: int
    lesson_type: str


def lesson_step_rule(step_type: int) -> LessonStepRule:
    if type(step_type) is not int or step_type not in LESSON_STEP_VALUES:
        raise ValueError(f"unsupported native lesson step: {step_type!r}")
    equivalent = _LEGEND_EQUIVALENT.get(step_type, step_type)
    axis_index, kind_index = divmod(equivalent - 1, 3)
    return LessonStepRule(step_type, "legend_lesson" if step_type >= 29 else "lesson",
        ProduceParameterType(axis_index + 1), _AXES[axis_index], ("normal", "sp", "hard")[kind_index],
        equivalent, "ProduceStepLessonType_Lesson" + _AXES[axis_index].title())


def _integer(value, field: str, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**31 - 1:
        raise ValueError(f"native lesson {field} must be Int32 >= {minimum}")
    return value


def _identity(raw, native, native_key, raw_key):
    first, second = raw.get(raw_key), native.get(native_key)
    if first and second and first != second:
        raise ValueError(f"native lesson {raw_key} disagrees with same-snapshot context")
    result = first or second
    if not isinstance(result, str) or not result:
        raise ValueError(f"native lesson {raw_key} is missing")
    return result


@dataclass(frozen=True, slots=True)
class NativeLessonContext:
    produce_id: str
    idol_card_id: str
    plan_type: str
    setting_id: str
    step: LessonStepRule
    current_turn: int
    limit_turn: int
    remaining_turns: int
    extra_turn: int
    score: int
    clear_border: int
    origin_clear_border: int
    limit_border: int
    lesson_limit_up_score: int
    native_complete: bool
    rule_digest: str
    stage: str = "Lesson"
    schema: str = SCHEMA

    def to_dict(self):
        result = asdict(self)
        result["step"]["parameter_type"] = int(self.step.parameter_type)
        return result


def parse_native_lesson_context(raw: Mapping[str, object], native_context=None, *,
                                expected_produce_id=None, expected_idol_card_id=None,
                                master_dir: Path | None = None) -> NativeLessonContext:
    from .runtime_mode_profile import DEFAULT_MASTER_DIR, _table, load_runtime_mode_profile
    directory = DEFAULT_MASTER_DIR if master_dir is None else Path(master_dir)
    if not isinstance(raw, Mapping) or raw.get("examType") != 0:
        raise ValueError("native lesson requires examType=0")
    native = {} if native_context is None else native_context
    if not isinstance(native, Mapping):
        raise ValueError("same-snapshot native lesson context must be an object")
    rule = lesson_step_rule(raw.get("stepType"))
    if native.get("step_type") is not None and native["step_type"] != rule.step_type:
        raise ValueError("native lesson step disagrees with same-snapshot context")
    if raw.get("turnStatusParameterTypeList") != []:
        raise ValueError("native lesson must preserve its empty audition turn schedule")
    if raw.get("npcDataList") != []:
        raise ValueError("native lesson cannot carry audition NPC tracks")
    produce_id = _identity(raw, native, "produce_id", "produceId")
    idol_id = _identity(raw, native, "idol_card_id", "idolCardId")
    profile = load_runtime_mode_profile(produce_id, master_dir=directory)
    if not any(family.family == rule.family and family.resolves_in_exam for family in profile.lesson_families):
        raise ValueError("native lesson family does not belong to the observed mode")
    idols, _digest = _table(directory, "IdolCard")
    matches = [row for row in idols if row.get("id") == idol_id]
    if len(matches) != 1:
        raise ValueError("native lesson idol does not resolve once in Master")
    idol = matches[0]
    plan = idol["planType"]
    if {"ProducePlanType_Plan1": 2, "ProducePlanType_Plan2": 3, "ProducePlanType_Plan3": 4}.get(plan) != raw.get("planType"):
        raise ValueError("native lesson Plan disagrees with observed idol")
    if raw.get("characterId") != idol.get("characterId"):
        raise ValueError("native lesson character disagrees with observed idol")
    if raw.get("settingId") != profile.exam_setting_id:
        raise ValueError("native lesson setting disagrees with mode Master")
    if expected_produce_id is not None and produce_id != expected_produce_id:
        raise ValueError("native lesson differs from expected mode")
    if expected_idol_card_id is not None and idol_id != expected_idol_card_id:
        raise ValueError("native lesson differs from expected idol")
    current = _integer(raw.get("currentTurn"), "currentTurn", minimum=1)
    limit = _integer(raw.get("limitTurn"), "limitTurn", minimum=1)
    extra = _integer(raw.get("extraTurn"), "extraTurn")
    remaining = _integer(raw.get("remainTurn"), "remainTurn")
    complete = raw.get("isExamEndComplete")
    if type(complete) is not bool:
        raise ValueError("native lesson isExamEndComplete must be explicit boolean")
    if current > limit + extra + int(complete) or remaining > limit + extra:
        raise ValueError("native lesson clock exceeds actual total turns")
    return NativeLessonContext(produce_id, idol_id, plan, profile.exam_setting_id, rule, current, limit, remaining, extra,
        _integer(raw.get("parameter"), "parameter"), _integer(raw.get("clearBorder"), "clearBorder", minimum=-1),
        _integer(raw.get("originClearBorder"), "originClearBorder", minimum=-1),
        _integer(raw.get("limitBorder"), "limitBorder", minimum=-1),
        _integer(raw.get("lessonLimitUpScore"), "lessonLimitUpScore"), complete, profile.rule_digest)


@dataclass(frozen=True, slots=True)
class NativeLessonEvaluation:
    score: int
    cleared: bool
    perfect: bool
    terminal: bool
    terminal_reason: str | None
    native_complete: bool
    clear_shortfall: int | None
    perfect_shortfall: int | None
    remaining_turns: int
    result_tier: str | None
    # Server-returned attribute growth is separate from lesson score/tier.
    attribute_growth_authority: str = "native outer lesson result"


def evaluate_native_lesson(context: NativeLessonContext, *, score: int | None = None,
                           remaining_turns: int | None = None, native_complete: bool | None = None) -> NativeLessonEvaluation:
    current = context.score if score is None else _integer(score, "evaluated score")
    remaining = context.remaining_turns if remaining_turns is None else _integer(remaining_turns, "evaluated remaining turns")
    actual_complete = context.native_complete if native_complete is None else native_complete
    if type(actual_complete) is not bool:
        raise ValueError("native_complete must be boolean")
    cleared = context.clear_border >= 0 and current >= context.clear_border
    perfect = context.limit_border >= 0 and current >= context.limit_border
    reason = "native-complete" if actual_complete else "limit-border" if perfect else "turn-exhausted" if remaining == 0 else None
    return NativeLessonEvaluation(current, cleared, perfect, reason is not None, reason, actual_complete,
        None if context.clear_border < 0 else max(0, context.clear_border - current),
        None if context.limit_border < 0 else max(0, context.limit_border - current),
        remaining, ("PERFECT" if perfect else "CLEAR" if cleared else "FAIL") if reason else None)
