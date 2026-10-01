"""Typed Plan2 lesson/audition mode and scheduled-gimmick boundaries.

The native ``ExamSaveData`` representation uses an empty turn-parameter
schedule for lessons.  A lesson's one fixed parameter type and its
Normal/SP/Hard kind instead come entirely from ``stepType`` 1 through 9.

Scheduled gimmicks intentionally remain an explicit hook boundary.  The
serialized grammar can identify *when* and *which* effect is queued, but it
does not contain the effect implementation.  A caller must therefore bind a
known, evidence-backed hook for every serialized schedule row; unknown rows
fail closed.  Effect IDs are not row identities: Master legally reuses one
effect at different turns with different trigger predicates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, TypeAlias

from .native_exam_formula import INT32_MAX, ProduceParameterType
from .runtime_lesson_context import LESSON_STEP_VALUES, lesson_step_rule


PLAN2_LESSON_EXAM_TYPE: Final = 0
PLAN2_AUDITION_EXAM_TYPE: Final = 1


class Plan2ExamModeError(ValueError):
    """Exam mode or scheduled-gimmick evidence is not safely executable."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class Plan2LessonDifficulty(Enum):
    NORMAL = "normal"
    SP = "sp"
    HARD = "hard"


class Plan2ExamOutcome(Enum):
    CLEAR = "clear"
    FAILURE = "failure"


class Plan2ExamTerminalReason(Enum):
    LIMIT_BORDER = "limit_border"
    TURN_EXHAUSTED = "turn_exhausted"


class Plan2ScheduledGimmickPhase(Enum):
    START_TURN = "start_turn"


Plan2ScheduledGimmickHookKey: TypeAlias = tuple[int, str, str]


def plan2_scheduled_gimmick_hook_key(
    turn: object,
    gimmick_group_id: object,
    gimmick_effect_id: object,
) -> Plan2ScheduledGimmickHookKey:
    """Return the identity serialized by one native gimmick schedule row."""

    return (
        _plain_i32(turn, "gimmick.turn", minimum=1),
        _text(gimmick_group_id, "gimmick_group_id"),
        _text(gimmick_effect_id, "gimmick_effect_id"),
    )


_LESSON_STEP_PROPERTIES: Final = {
    step: (lesson_step_rule(step).parameter_type, Plan2LessonDifficulty(lesson_step_rule(step).difficulty))
    for step in LESSON_STEP_VALUES
}


def _plain_i32(value: object, label: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > INT32_MAX
    ):
        raise Plan2ExamModeError("invalid-int32", label)
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan2ExamModeError("invalid-text", label)
    return value


@dataclass(frozen=True, slots=True)
class Plan2ExamMode:
    """Resolved native exam mode without inventing a lesson turn schedule."""

    exam_type: int
    step_type_value: int
    lesson_parameter_type: ProduceParameterType | None = None
    lesson_difficulty: Plan2LessonDifficulty | None = None

    def __post_init__(self) -> None:
        exam_type = _plain_i32(self.exam_type, "exam_type")
        step_type = _plain_i32(self.step_type_value, "step_type_value")
        if exam_type not in {PLAN2_LESSON_EXAM_TYPE, PLAN2_AUDITION_EXAM_TYPE}:
            raise Plan2ExamModeError("unsupported-exam-type", str(exam_type))
        if exam_type == PLAN2_AUDITION_EXAM_TYPE:
            if self.lesson_parameter_type is not None:
                raise Plan2ExamModeError(
                    "audition-has-lesson-parameter-type"
                )
            if self.lesson_difficulty is not None:
                raise Plan2ExamModeError("audition-has-lesson-difficulty")
            return

        expected = _LESSON_STEP_PROPERTIES.get(step_type)
        if expected is None:
            raise Plan2ExamModeError(
                "unsupported-lesson-step-type", str(step_type)
            )
        parameter_type, difficulty = expected
        try:
            resolved_parameter_type = ProduceParameterType(
                self.lesson_parameter_type
            )
        except (TypeError, ValueError) as error:
            raise Plan2ExamModeError(
                "invalid-lesson-parameter-type",
                repr(self.lesson_parameter_type),
            ) from error
        try:
            resolved_difficulty = Plan2LessonDifficulty(
                self.lesson_difficulty
            )
        except (TypeError, ValueError) as error:
            raise Plan2ExamModeError(
                "invalid-lesson-difficulty", repr(self.lesson_difficulty)
            ) from error
        if resolved_parameter_type is not parameter_type:
            raise Plan2ExamModeError(
                "lesson-step-parameter-mismatch", str(step_type)
            )
        if resolved_difficulty is not difficulty:
            raise Plan2ExamModeError(
                "lesson-step-difficulty-mismatch", str(step_type)
            )
        object.__setattr__(
            self, "lesson_parameter_type", resolved_parameter_type
        )
        object.__setattr__(self, "lesson_difficulty", resolved_difficulty)

    @property
    def is_lesson(self) -> bool:
        return self.exam_type == PLAN2_LESSON_EXAM_TYPE

    @property
    def is_battle(self) -> bool:
        return self.exam_type == PLAN2_AUDITION_EXAM_TYPE

    @property
    def is_sp(self) -> bool:
        return self.lesson_difficulty is Plan2LessonDifficulty.SP

    @property
    def is_hard(self) -> bool:
        return self.lesson_difficulty is Plan2LessonDifficulty.HARD

    @property
    def is_normal(self) -> bool:
        return self.lesson_difficulty is Plan2LessonDifficulty.NORMAL

    @property
    def parameter_type(self) -> ProduceParameterType:
        if self.lesson_parameter_type is None:
            return ProduceParameterType.UNKNOWN
        return self.lesson_parameter_type


def resolve_plan2_exam_mode(
    exam_type: object,
    step_type_value: object,
) -> Plan2ExamMode:
    """Resolve exactly the mode fields serialized by ``ExamSaveData``."""

    resolved_exam_type = _plain_i32(exam_type, "exam_type")
    resolved_step_type = _plain_i32(step_type_value, "step_type_value")
    if resolved_exam_type == PLAN2_LESSON_EXAM_TYPE:
        properties = _LESSON_STEP_PROPERTIES.get(resolved_step_type)
        if properties is None:
            raise Plan2ExamModeError(
                "unsupported-lesson-step-type", str(resolved_step_type)
            )
        parameter_type, difficulty = properties
        return Plan2ExamMode(
            exam_type=resolved_exam_type,
            step_type_value=resolved_step_type,
            lesson_parameter_type=parameter_type,
            lesson_difficulty=difficulty,
        )
    if resolved_exam_type == PLAN2_AUDITION_EXAM_TYPE:
        return Plan2ExamMode(
            exam_type=resolved_exam_type,
            step_type_value=resolved_step_type,
        )
    raise Plan2ExamModeError(
        "unsupported-exam-type", str(resolved_exam_type)
    )


DEFAULT_PLAN2_AUDITION_EXAM_MODE: Final = resolve_plan2_exam_mode(
    PLAN2_AUDITION_EXAM_TYPE,
    16,
)


def evaluate_plan2_exam_outcome(
    score: object,
    clear_border: object,
) -> Plan2ExamOutcome:
    """ClearBorder judges success; it never decides when the exam ends."""

    resolved_score = _plain_i32(score, "score")
    if (
        isinstance(clear_border, bool)
        or not isinstance(clear_border, int)
        or clear_border < -1
        or clear_border > INT32_MAX
    ):
        raise Plan2ExamModeError("invalid-clear-border")
    return (
        Plan2ExamOutcome.CLEAR
        if clear_border >= 0 and resolved_score >= clear_border
        else Plan2ExamOutcome.FAILURE
    )


@dataclass(frozen=True, slots=True)
class Plan2ScheduledGimmickHookResult:
    """Opaque state handoff returned by one registered offline hook."""

    state: object
    trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        trace = tuple(self.trace)
        if any(not isinstance(value, str) or not value for value in trace):
            raise Plan2ExamModeError("invalid-gimmick-hook-trace")
        object.__setattr__(self, "trace", trace)


Plan2ScheduledGimmickExecutor = Callable[
    [object, object], Plan2ScheduledGimmickHookResult
]


@dataclass(frozen=True, slots=True)
class Plan2ScheduledGimmickHook:
    """Known implementation bound to one serialized gimmick effect ID."""

    effect_id: str
    executor_id: str
    executor: Plan2ScheduledGimmickExecutor = field(
        repr=False,
        compare=False,
        hash=False,
    )
    phase: Plan2ScheduledGimmickPhase = (
        Plan2ScheduledGimmickPhase.START_TURN
    )

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _text(self.executor_id, "executor_id")
        try:
            phase = Plan2ScheduledGimmickPhase(self.phase)
        except (TypeError, ValueError) as error:
            raise Plan2ExamModeError(
                "unsupported-gimmick-phase", repr(self.phase)
            ) from error
        if not callable(self.executor):
            raise Plan2ExamModeError("gimmick-hook-not-callable")
        object.__setattr__(self, "phase", phase)


@dataclass(frozen=True, slots=True)
class Plan2ScheduledGimmick:
    turn: int
    gimmick_group_id: str
    gimmick_effect_id: str
    hook: Plan2ScheduledGimmickHook

    def __post_init__(self) -> None:
        _plain_i32(self.turn, "turn", minimum=1)
        _text(self.gimmick_group_id, "gimmick_group_id")
        _text(self.gimmick_effect_id, "gimmick_effect_id")
        if not isinstance(self.hook, Plan2ScheduledGimmickHook):
            raise TypeError("hook must be Plan2ScheduledGimmickHook")
        if self.hook.effect_id != self.gimmick_effect_id:
            raise Plan2ExamModeError("gimmick-hook-effect-id-mismatch")


_SCHEDULED_GIMMICK_FIELDS: Final = frozenset(
    {"turn", "gimmickGroupId", "gimmickEffectId"}
)


def compile_plan2_scheduled_gimmicks(
    raw: object,
    *,
    hooks: Mapping[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook]
    | None,
    maximum_turn: object,
    completed_through_turn: object = 0,
    observe_unmapped_hooks: bool = False,
) -> tuple[Plan2ScheduledGimmick, ...]:
    """Compile the exact known LocalSave schedule or fail closed.

    ``completed_through_turn`` removes hooks whose native StartTurn phase has
    already happened in a settled Main-phase checkpoint.
    """

    resolved_maximum = _plain_i32(maximum_turn, "maximum_turn", minimum=1)
    resolved_completed = _plain_i32(
        completed_through_turn,
        "completed_through_turn",
    )
    if resolved_completed > resolved_maximum:
        raise Plan2ExamModeError("gimmick-completed-turn-out-of-range")
    if type(observe_unmapped_hooks) is not bool:
        raise TypeError("observe_unmapped_hooks must be boolean")
    if not isinstance(raw, list):
        raise Plan2ExamModeError("invalid-gimmick-list-grammar")
    resolved_hooks = {} if hooks is None else dict(hooks)
    for key, hook in resolved_hooks.items():
        if (
            not isinstance(key, tuple)
            or len(key) != 3
            or isinstance(key[0], bool)
            or not isinstance(key[0], int)
            or key[0] < 1
            or not isinstance(key[1], str)
            or not key[1]
            or not isinstance(key[2], str)
            or not key[2]
            or not isinstance(hook, Plan2ScheduledGimmickHook)
            or hook.effect_id != key[2]
        ):
            raise Plan2ExamModeError("invalid-gimmick-hook-registry")

    compiled: list[Plan2ScheduledGimmick] = []
    previous_turn = 0
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping) or set(item) != _SCHEDULED_GIMMICK_FIELDS:
            raise Plan2ExamModeError(
                "unknown-gimmick-row-grammar", str(index)
            )
        turn = _plain_i32(item["turn"], f"gimmickList[{index}].turn", minimum=1)
        if turn > resolved_maximum:
            raise Plan2ExamModeError(
                "gimmick-turn-out-of-range", f"{index}:{turn}"
            )
        if turn < previous_turn:
            raise Plan2ExamModeError(
                "gimmick-turn-order-unmapped", f"{index}:{turn}"
            )
        previous_turn = turn
        group_id = _text(
            item["gimmickGroupId"],
            f"gimmickList[{index}].gimmickGroupId",
        )
        effect_id = _text(
            item["gimmickEffectId"],
            f"gimmickList[{index}].gimmickEffectId",
        )
        # A settled Main-phase ExamSave already contains every StartTurn
        # effect through ``completed_through_turn``.  Requiring an executor
        # for one of those historical rows cannot change a future decision;
        # it only prevents us from using the game's durable current state.
        if turn <= resolved_completed:
            continue
        hook = resolved_hooks.get(
            plan2_scheduled_gimmick_hook_key(turn, group_id, effect_id)
        )
        if hook is None:
            if observe_unmapped_hooks:
                # Live play observes this game-owned effect in the next
                # ExamSave instead of blocking the current card choice.
                continue
            raise Plan2ExamModeError(
                "scheduled-gimmick-hook-missing", effect_id
            )
        if hook.phase is not Plan2ScheduledGimmickPhase.START_TURN:
            raise Plan2ExamModeError(
                "unsupported-gimmick-phase", hook.phase.value
            )
        compiled.append(
            Plan2ScheduledGimmick(
                turn=turn,
                gimmick_group_id=group_id,
                gimmick_effect_id=effect_id,
                hook=hook,
            )
        )
    return tuple(compiled)


__all__ = [
    "DEFAULT_PLAN2_AUDITION_EXAM_MODE",
    "PLAN2_AUDITION_EXAM_TYPE",
    "PLAN2_LESSON_EXAM_TYPE",
    "Plan2ExamMode",
    "Plan2ExamModeError",
    "Plan2ExamOutcome",
    "Plan2ExamTerminalReason",
    "Plan2LessonDifficulty",
    "Plan2ScheduledGimmick",
    "Plan2ScheduledGimmickHook",
    "Plan2ScheduledGimmickHookKey",
    "Plan2ScheduledGimmickHookResult",
    "Plan2ScheduledGimmickPhase",
    "compile_plan2_scheduled_gimmicks",
    "evaluate_plan2_exam_outcome",
    "plan2_scheduled_gimmick_hook_key",
    "resolve_plan2_exam_mode",
]
