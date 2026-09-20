"""Plan 1 runtime score/settings projection.

``Plan1NativeSettings`` is the static ``ExamSetting`` arithmetic.  An
audition's score hit also consumes dynamic fields from the *same* native
``state_before``: the scheduled Vocal/Dance/Visual type and the three battle
bonus permils.  This module joins those two sources into one small, public
projection.  It intentionally does not inspect a card, replay action, or
``state_after``; callers may apply the projection to any already compiled
Plan 1 effect list.

The native order is:

``CalculateAddingParameter -> AddParameter -> CalcCurrentTurnBattleBonus``

for every lesson hit.  The core's ``Plan1ScalarState`` stores the projected
score context, so all supported Plan 1 lesson-effect families share this
per-hit boundary rather than growing card- or turn-specific branches.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from .audition_local_save_state import LocalSaveExamState, parse_local_save_exam_state
from .native_exam_formula import (
    INT32_MAX,
    IdolStatusType,
    ParameterApplicationStatus,
    ProduceParameterType,
)
from .plan1_native_core import (
    DEFAULT_MASTER_DIR,
    PLAN1,
    Plan1LessonBuffAdditiveStatus,
    Plan1NativeSettings,
    Plan1ScalarState,
    load_plan1_native_settings,
)
from .nia_lesson_value_multiple import LessonParameterMultipleStatus


PLAN1_RUNTIME_SCORING_SCHEMA_VERSION: Final = 1
PLAN1_RUNTIME_SCORING_ADAPTER_ID: Final = "plan1.runtime.scoring"
PLAN1_AUDITION_EXAM_TYPE: Final = 1
PLAN1_LESSON_EXAM_TYPE: Final = 0


class Plan1RuntimeScoringError(ValueError):
    """The supplied Plan 1 state-before does not prove score context."""

    def __init__(self, code: str, detail: str = "") -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("scoring error code must be non-empty text")
        if not isinstance(detail, str):
            raise TypeError("scoring error detail must be text")
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _i32(value: object, label: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > INT32_MAX
    ):
        raise Plan1RuntimeScoringError("invalid-int32", label)
    return value


def _border(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan1RuntimeScoringError("invalid-border", label)
    if value < -1 or value > INT32_MAX:
        raise Plan1RuntimeScoringError("invalid-border", label)
    return value


def _type(value: object, label: str) -> ProduceParameterType:
    if isinstance(value, bool):
        raise Plan1RuntimeScoringError("invalid-parameter-type", label)
    try:
        result = ProduceParameterType(value)
    except (TypeError, ValueError) as error:
        raise Plan1RuntimeScoringError(
            "invalid-parameter-type", f"{label}={value!r}"
        ) from error
    if result not in {
        ProduceParameterType.VOCAL,
        ProduceParameterType.DANCE,
        ProduceParameterType.VISUAL,
    }:
        raise Plan1RuntimeScoringError("invalid-parameter-type", label)
    return result


def _root(state: LocalSaveExamState) -> Mapping[str, object]:
    runtime = state.root_runtime
    if runtime is None:
        raise Plan1RuntimeScoringError("root-runtime-missing")
    raw = runtime.opaque_fields.to_value()
    if not isinstance(raw, Mapping):  # guarded by LocalSave's source type
        raise Plan1RuntimeScoringError("root-runtime-invalid")
    return raw


def _root_i32(
    raw: Mapping[str, object],
    field: str,
    *,
    minimum: int = 0,
    default: int | None = None,
) -> int:
    if field not in raw:
        if default is None:
            raise Plan1RuntimeScoringError("root-runtime-field-missing", field)
        value: object = default
    else:
        value = raw[field]
    return _i32(value, f"ExamSaveData.{field}", minimum=minimum)


def _root_border(
    raw: Mapping[str, object], field: str, *, default: int | None = None
) -> int:
    if field not in raw:
        if default is None:
            raise Plan1RuntimeScoringError("root-runtime-field-missing", field)
        value: object = default
    else:
        value = raw[field]
    return _border(value, f"ExamSaveData.{field}")


@dataclass(frozen=True, slots=True)
class Plan1RuntimeStatusProjection:
    """Score-relevant status values read from native status references.

    The first three values are the statuses consumed by the official FKTN
    Plan 1 slice.  The status object also retains idol stance and the two
    native boolean predicates needed by ``CalculateAddingParameter``; unknown
    status classes remain outside this projection instead of being inferred
    from IDs.
    """

    parameter_buff_turns: int = 0
    parameter_buff_multiple_per_turn_turns: int = 0
    lesson_buff: int = 0
    lesson_buff_multiple_permille: int = 1000
    lesson_buff_gain_multiple_permille: int = 1000
    lesson_parameter_multiple_statuses: tuple[
        LessonParameterMultipleStatus, ...
    ] = ()
    idol_status_type: IdolStatusType = IdolStatusType.UNKNOWN
    idol_status_step: int = 1
    slump: bool = False
    parameter_debuff: bool = False
    # Kept as an explicit field for callers that carry a separately proven
    # lifecycle bit.  The serialized ``_isPassingTurnStart`` flag is not
    # treated as that bit here: the official runtime keeps it true across
    # ordinary turn boundaries, so inferring freshness would shorten or
    # extend the status incorrectly.
    parameter_buff_fresh: bool = False
    lesson_buff_additive_statuses: tuple[
        Plan1LessonBuffAdditiveStatus, ...
    ] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        for name in (
            "parameter_buff_turns",
            "parameter_buff_multiple_per_turn_turns",
            "lesson_buff",
            "lesson_buff_multiple_permille",
            "lesson_buff_gain_multiple_permille",
        ):
            _i32(getattr(self, name), name)
        if not isinstance(self.idol_status_type, IdolStatusType):
            try:
                object.__setattr__(
                    self,
                    "idol_status_type",
                    IdolStatusType(self.idol_status_type),
                )
            except (TypeError, ValueError) as error:
                raise Plan1RuntimeScoringError(
                    "invalid-idol-status-type"
                ) from error
        _i32(self.idol_status_step, "idol_status_step", minimum=0)
        if not isinstance(self.slump, bool):
            raise TypeError("slump must be bool")
        if not isinstance(self.parameter_debuff, bool):
            raise TypeError("parameter_debuff must be bool")
        if not isinstance(self.parameter_buff_fresh, bool):
            raise TypeError("parameter_buff_fresh must be bool")
        statuses = tuple(self.lesson_buff_additive_statuses)
        if any(
            not isinstance(value, Plan1LessonBuffAdditiveStatus)
            for value in statuses
        ):
            raise TypeError(
                "lesson_buff_additive_statuses must contain typed statuses"
            )
        object.__setattr__(
            self,
            "lesson_buff_additive_statuses",
            statuses,
        )
        parameter_statuses = tuple(self.lesson_parameter_multiple_statuses)
        if any(
            not isinstance(value, LessonParameterMultipleStatus)
            for value in parameter_statuses
        ):
            raise TypeError(
                "lesson_parameter_multiple_statuses must contain typed statuses"
            )
        object.__setattr__(
            self,
            "lesson_parameter_multiple_statuses",
            parameter_statuses,
        )
        _i32(self.next_status_uid, "next_status_uid", minimum=1)


def _status_projection(
    raw: Mapping[str, object],
) -> Plan1RuntimeStatusProjection:
    """Resolve score statuses through serialized type/reference identity.

    ``status._effectList`` contains ``rid`` references; the class and data
    for each status live in ``references.RefIds``.  Trigger statuses are
    intentionally ignored here: they are event listeners, not active score
    modifiers, and their child effects belong to a separate lifecycle step.
    """

    status = raw.get("status")
    references = raw.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        return Plan1RuntimeStatusProjection()
    effect_list = status.get("_effectList")
    ref_list = references.get("RefIds")
    if not isinstance(effect_list, list) or not isinstance(ref_list, list):
        return Plan1RuntimeStatusProjection()

    by_rid: dict[int, Mapping[str, object]] = {}
    for row in ref_list:
        if not isinstance(row, Mapping) or type(row.get("rid")) is not int:
            continue
        by_rid[int(row["rid"])] = row

    values: dict[str, object] = {
        "parameter_buff_turns": 0,
        "parameter_buff_multiple_per_turn_turns": 0,
        "lesson_buff": 0,
        "lesson_buff_multiple_permille": 1000,
        "lesson_buff_gain_multiple_permille": 1000,
        "idol_status_type": IdolStatusType.UNKNOWN,
        "idol_status_step": 1,
        "slump": False,
        "parameter_debuff": False,
        "parameter_buff_fresh": False,
        "lesson_buff_additive_statuses": (),
        "next_status_uid": 1,
    }
    lesson_buff_additive_permille = 0
    additive_statuses: list[Plan1LessonBuffAdditiveStatus] = []
    lesson_parameter_statuses: list[LessonParameterMultipleStatus] = []

    def read_int(data: Mapping[str, object], keys: tuple[str, ...], label: str) -> int | None:
        for key in keys:
            if key not in data:
                continue
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise Plan1RuntimeScoringError(
                    "status-value-invalid", f"{label}:{key}"
                )
            return value
        return None

    for link in effect_list:
        if not isinstance(link, Mapping) or type(link.get("rid")) is not int:
            continue
        row = by_rid.get(int(link["rid"]))
        if row is None:
            continue
        kind = row.get("type")
        data = row.get("data")
        if not isinstance(kind, Mapping) or not isinstance(data, Mapping):
            continue
        name = kind.get("class")
        if name == "ParameterBuffStatusEffect":
            turn = read_int(data, ("_turn",), str(name))
            if turn is not None:
                values["parameter_buff_turns"] = max(
                    int(values["parameter_buff_turns"]), max(turn, 0)
                )
        elif name == "ParameterBuffMultiplePerTurnStatusEffect":
            turn = read_int(data, ("_turn",), str(name))
            if turn is not None:
                values["parameter_buff_multiple_per_turn_turns"] = max(
                    int(values["parameter_buff_multiple_per_turn_turns"]),
                    max(turn, 0),
                )
        elif name == "LessonBuffStatusEffect":
            amount = read_int(data, ("_value",), str(name))
            if amount is not None:
                values["lesson_buff"] = max(
                    int(values["lesson_buff"]), max(amount, 0)
                )
        elif name == "LessonBuffMultipleStatusEffect":
            amount = read_int(data, ("_value",), str(name))
            if amount is not None:
                values["lesson_buff_multiple_permille"] = max(
                    int(values["lesson_buff_multiple_permille"]), max(amount, 0)
                )
        elif name == "LessonParameterMultipleStatusEffect":
            amount = read_int(data, ("_value",), str(name))
            uid = read_int(data, ("_uid",), str(name))
            turns = read_int(data, ("_turn",), str(name))
            passing = data.get("_isPassingTurnStart")
            limited = data.get("_isTurnLimited")
            if (
                amount is None
                or uid is None
                or uid < 1
                or turns is None
                or (turns != -1 and turns < 1)
                or type(passing) is not bool
                or type(limited) is not bool
                or limited is not (turns != -1)
            ):
                raise Plan1RuntimeScoringError(
                    "status-value-invalid",
                    str(name),
                )
            lesson_parameter_statuses.append(
                LessonParameterMultipleStatus(
                    permil=amount,
                    turn=turns,
                    passing_turn_start=passing,
                    is_turn_limited=limited,
                    uid=uid,
                )
            )
        elif name == "LessonBuffAdditiveStatusEffect":
            amount = read_int(data, ("_value",), str(name))
            uid = read_int(data, ("_uid",), str(name))
            turns = read_int(data, ("_turn",), str(name))
            passing = data.get("_isPassingTurnStart")
            limited = data.get("_isTurnLimited")
            if (
                amount is None
                or amount <= 0
                or uid is None
                or uid < 1
                or turns is None
                or (turns != -1 and turns < 1)
                or type(passing) is not bool
                or limited is not (turns != -1)
            ):
                raise Plan1RuntimeScoringError(
                    "status-value-invalid",
                    str(name),
                )
            additive_statuses.append(
                Plan1LessonBuffAdditiveStatus(
                    uid=uid,
                    value_permille=amount,
                    turns=turns,
                    passing_turn_start=passing,
                )
            )
            lesson_buff_additive_permille += amount
            if lesson_buff_additive_permille > INT32_MAX:
                raise Plan1RuntimeScoringError(
                    "status-value-overflow",
                    str(name),
                )
        elif name == "ParameterDebuffStatusEffect":
            values["parameter_debuff"] = True
        elif name == "SlumpStatusEffect":
            values["slump"] = True

    idol = status.get("_idolStatusEffect")
    if isinstance(idol, Mapping):
        raw_type = idol.get("_currentType", 0)
        raw_step = idol.get("_currentStep", 0)
        if isinstance(raw_type, bool) or not isinstance(raw_type, int):
            raise Plan1RuntimeScoringError(
                "status-value-invalid", "ExamIdolStatusEffect:_currentType"
            )
        if isinstance(raw_step, bool) or not isinstance(raw_step, int):
            raise Plan1RuntimeScoringError(
                "status-value-invalid", "ExamIdolStatusEffect:_currentStep"
            )
        try:
            values["idol_status_type"] = IdolStatusType(raw_type)
        except ValueError as error:
            raise Plan1RuntimeScoringError(
                "invalid-idol-status-type", str(raw_type)
            ) from error
        values["idol_status_step"] = max(raw_step, 0)

    multiple = int(values["lesson_buff_gain_multiple_permille"])
    if multiple > INT32_MAX - lesson_buff_additive_permille:
        raise Plan1RuntimeScoringError(
            "status-value-overflow",
            "lesson-buff-multiple",
        )
    values["lesson_buff_gain_multiple_permille"] = (
        multiple + lesson_buff_additive_permille
    )
    values["lesson_buff_additive_statuses"] = tuple(additive_statuses)
    values["lesson_parameter_multiple_statuses"] = tuple(
        lesson_parameter_statuses
    )
    effect_create_count = status.get("_effectCreateCount", 0)
    if (
        isinstance(effect_create_count, bool)
        or not isinstance(effect_create_count, int)
        or not 0 <= effect_create_count < INT32_MAX
    ):
        raise Plan1RuntimeScoringError(
            "status-value-invalid",
            "status._effectCreateCount",
        )
    values["next_status_uid"] = effect_create_count + 1
    return Plan1RuntimeStatusProjection(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Plan1RuntimeScoringProjection:
    """Complete dynamic score context projected from one state-before."""

    schema_version: int
    settings: Plan1NativeSettings
    exam_type: int
    plan_type: object | None
    current_turn: int
    limit_turn: int
    extra_turn: int
    turn_parameter_types: tuple[ProduceParameterType, ...]
    current_parameter_type: ProduceParameterType
    vocal_bonus_permille: int
    dance_bonus_permille: int
    visual_bonus_permille: int
    score: int
    clear_border: int
    limit_border: int
    current_turn_total_add_parameter: int
    judge_parameter_vocal: int
    judge_parameter_dance: int
    judge_parameter_visual: int
    main_effect_type: int
    display_main_effect_type: int
    status: Plan1RuntimeStatusProjection
    step_type_value: int = 0

    def __post_init__(self) -> None:
        if self.schema_version != PLAN1_RUNTIME_SCORING_SCHEMA_VERSION:
            raise Plan1RuntimeScoringError(
                "schema-version-mismatch", str(self.schema_version)
            )
        if not isinstance(self.settings, Plan1NativeSettings):
            raise TypeError("settings must be Plan1NativeSettings")
        _i32(self.exam_type, "exam_type")
        _i32(self.current_turn, "current_turn", minimum=1)
        _i32(self.limit_turn, "limit_turn", minimum=1)
        _i32(self.extra_turn, "extra_turn")
        schedule = tuple(
            _type(value, f"turn_parameter_types[{index}]")
            for index, value in enumerate(self.turn_parameter_types)
        )
        object.__setattr__(self, "turn_parameter_types", schedule)
        current_type = _type(
            self.current_parameter_type, "current_parameter_type"
        )
        object.__setattr__(self, "current_parameter_type", current_type)
        if self.exam_type == PLAN1_LESSON_EXAM_TYPE:
            from .runtime_lesson_context import lesson_step_rule
            if schedule:
                raise Plan1RuntimeScoringError("lesson-audition-schedule-present")
            if self.current_turn > self.limit_turn + self.extra_turn:
                raise Plan1RuntimeScoringError("lesson-current-turn-out-of-range")
            expected_type = lesson_step_rule(self.step_type_value).parameter_type
        elif self.exam_type == PLAN1_AUDITION_EXAM_TYPE:
            if len(schedule) not in {self.limit_turn, self.limit_turn + self.extra_turn}:
                raise Plan1RuntimeScoringError("schedule-length-mismatch",
                    f"limit_turn={self.limit_turn};extra_turn={self.extra_turn};schedule={len(schedule)}")
            if self.current_turn > len(schedule):
                raise Plan1RuntimeScoringError("schedule-current-turn-unmapped", str(self.current_turn))
            expected_type = schedule[self.current_turn - 1]
        else:
            raise Plan1RuntimeScoringError("unsupported-exam-type", str(self.exam_type))
        if current_type is not expected_type:
            raise Plan1RuntimeScoringError(
                "current-parameter-type-mismatch",
                f"expected={expected_type.value};"
                f"actual={self.current_parameter_type!r}",
            )
        for name in (
            "vocal_bonus_permille",
            "dance_bonus_permille",
            "visual_bonus_permille",
            "score",
            "current_turn_total_add_parameter",
            "judge_parameter_vocal",
            "judge_parameter_dance",
            "judge_parameter_visual",
        ):
            _i32(getattr(self, name), name)
        _border(self.clear_border, "clear_border")
        _border(self.limit_border, "limit_border")
        _i32(self.main_effect_type, "main_effect_type")
        _i32(self.display_main_effect_type, "display_main_effect_type")
        if not isinstance(self.status, Plan1RuntimeStatusProjection):
            raise TypeError("status must be Plan1RuntimeStatusProjection")

    @property
    def is_battle(self) -> bool:
        return self.exam_type == PLAN1_AUDITION_EXAM_TYPE

    @property
    def current_bonus_permille(self) -> int:
        if not self.is_battle:
            return 1000
        if self.current_parameter_type is ProduceParameterType.VOCAL:
            return self.vocal_bonus_permille
        if self.current_parameter_type is ProduceParameterType.DANCE:
            return self.dance_bonus_permille
        return self.visual_bonus_permille

    def apply_to_scalar(
        self,
        state: Plan1ScalarState,
        *,
        require_score_match: bool = True,
    ) -> Plan1ScalarState:
        """Attach this state-before score context to a Plan 1 scalar.

        ``state`` remains the caller's card/zone checkpoint.  This method only
        replaces score/settings/status fields; it never consumes an action or
        reads a post-state.  A score mismatch is rejected by default so a
        caller cannot accidentally apply one checkpoint's battle context to a
        different transition.
        """

        if not isinstance(state, Plan1ScalarState):
            raise TypeError("state must be Plan1ScalarState")
        if require_score_match and state.score != self.score:
            raise Plan1RuntimeScoringError(
                "scalar-score-mismatch",
                f"scalar={state.score};projection={self.score}",
            )
        return replace(
            state,
            parameter_buff_turns=self.status.parameter_buff_turns,
            parameter_buff_multiple_per_turn_turns=(
                self.status.parameter_buff_multiple_per_turn_turns
            ),
            lesson_buff=self.status.lesson_buff,
            lesson_buff_multiple_permille=self.status.lesson_buff_multiple_permille,
            lesson_buff_gain_multiple_permille=(
                self.status.lesson_buff_gain_multiple_permille
            ),
            lesson_parameter_multiple_statuses=(
                self.status.lesson_parameter_multiple_statuses
            ),
            lesson_buff_additive_statuses=(
                self.status.lesson_buff_additive_statuses
            ),
            next_status_uid=self.status.next_status_uid,
            parameter_buff_fresh=self.status.parameter_buff_fresh,
            is_battle=self.is_battle,
            current_parameter_type=self.current_parameter_type,
            battle_bonus_permille_vocal=self.vocal_bonus_permille,
            battle_bonus_permille_dance=self.dance_bonus_permille,
            battle_bonus_permille_visual=self.visual_bonus_permille,
            clear_border=self.clear_border,
            limit_border=self.limit_border,
            current_turn_total_add_parameter=self.current_turn_total_add_parameter,
            judge_parameter_vocal=self.judge_parameter_vocal,
            judge_parameter_dance=self.judge_parameter_dance,
            judge_parameter_visual=self.judge_parameter_visual,
        )

    def application_status(
        self,
        *,
        judge_parameter: int | None = None,
        current_turn_total_add_parameter: int | None = None,
        slump: bool | None = None,
    ) -> ParameterApplicationStatus:
        """Build the native ``AddParameter`` status for one score hit.

        This is useful to callers that already own a scalar/effect loop and
        only need the projected dynamic scoring fields.  Defaults use the
        state-before totals; no later state is consulted.
        """

        score = self.score if judge_parameter is None else judge_parameter
        total = (
            self.current_turn_total_add_parameter
            if current_turn_total_add_parameter is None
            else current_turn_total_add_parameter
        )
        return ParameterApplicationStatus(
            judge_parameter=score,
            limit_border=self.limit_border,
            clear_border=self.clear_border,
            current_turn_total_add_parameter=total,
            slump=False if slump is None else slump,
            is_battle=self.is_battle,
            current_parameter_type=self.current_parameter_type,
            battle_bonus_permille_vocal=self.vocal_bonus_permille,
            battle_bonus_permille_dance=self.dance_bonus_permille,
            battle_bonus_permille_visual=self.visual_bonus_permille,
            judge_parameter_vocal=self.judge_parameter_vocal,
            judge_parameter_dance=self.judge_parameter_dance,
            judge_parameter_visual=self.judge_parameter_visual,
        )

    # Descriptive alias for callers that use ``project`` terminology.
    project_scalar = apply_to_scalar


def project_plan1_runtime_scoring(
    payload: Mapping[str, object] | LocalSaveExamState,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
    settings: Plan1NativeSettings | None = None,
) -> Plan1RuntimeScoringProjection:
    """Project Plan 1 scoring/settings using only a native ``state_before``.

    A raw mapping must be the recorder's exact ``state_before`` object.  A
    parsed ``LocalSaveExamState`` is accepted for callers that already passed
    through the strict local-save parser.  In either form ``state_after`` is
    neither accepted nor consulted.
    """

    if isinstance(payload, LocalSaveExamState):
        state = payload
    elif isinstance(payload, Mapping):
        try:
            state = parse_local_save_exam_state(payload)
        except (TypeError, ValueError) as error:
            raise Plan1RuntimeScoringError(
                "state-before-parse-failed", f"{type(error).__name__}:{error}"
            ) from error
    else:
        raise TypeError("payload must be a mapping or LocalSaveExamState")

    if state.exam_type not in {PLAN1_LESSON_EXAM_TYPE, PLAN1_AUDITION_EXAM_TYPE}:
        raise Plan1RuntimeScoringError(
            "unsupported-exam-type", str(state.exam_type)
        )
    raw = _root(state)
    raw_plan_type = raw.get("planType")
    if raw_plan_type is not None and not (
        raw_plan_type == 2 or raw_plan_type == PLAN1
    ):
        raise Plan1RuntimeScoringError(
            "unsupported-plan-type", repr(raw_plan_type)
        )
    # Main/display are retained as evidence, but never used as a card/turn
    # switch.  They are required state-before fields, like the score limits
    # and per-attribute totals; the battle decision is serialized examType.
    main_effect_type = _root_i32(raw, "mainEffectType")
    display_main_effect_type = _root_i32(raw, "displayMainEffectType")
    schedule = tuple(
        _type(value, f"turn_parameter_types[{index}]")
        for index, value in enumerate(state.turn_parameter_types)
    )
    if state.exam_type == PLAN1_LESSON_EXAM_TYPE:
        from .runtime_lesson_context import lesson_step_rule
        if schedule:
            raise Plan1RuntimeScoringError("lesson-audition-schedule-present")
        current_type = lesson_step_rule(state.step_type_value).parameter_type
    else:
        if len(schedule) not in {state.limit_turn, state.limit_turn + state.extra_turn}:
            raise Plan1RuntimeScoringError("schedule-length-mismatch",
                f"limit_turn={state.limit_turn};extra_turn={state.extra_turn};schedule={len(schedule)}")
        if state.current_turn > len(schedule):
            raise Plan1RuntimeScoringError("schedule-current-turn-unmapped", str(state.current_turn))
        current_type = schedule[state.current_turn - 1]
    effective_settings = (
        load_plan1_native_settings(state.setting_id, master_dir=Path(master_dir))
        if settings is None
        else settings
    )
    return Plan1RuntimeScoringProjection(
        schema_version=PLAN1_RUNTIME_SCORING_SCHEMA_VERSION,
        settings=effective_settings,
        exam_type=state.exam_type,
        plan_type=raw_plan_type,
        current_turn=state.current_turn,
        limit_turn=state.limit_turn,
        extra_turn=state.extra_turn,
        turn_parameter_types=schedule,
        current_parameter_type=current_type,
        vocal_bonus_permille=state.vocal_bonus_permille,
        dance_bonus_permille=state.dance_bonus_permille,
        visual_bonus_permille=state.visual_bonus_permille,
        score=state.score,
        clear_border=_root_border(raw, "clearBorder"),
        limit_border=_root_border(raw, "limitBorder"),
        current_turn_total_add_parameter=_root_i32(
            raw, "currentTurnTotalAddParameter"
        ),
        judge_parameter_vocal=_root_i32(raw, "parameterVocal"),
        judge_parameter_dance=_root_i32(raw, "parameterDance"),
        judge_parameter_visual=_root_i32(raw, "parameterVisual"),
        main_effect_type=main_effect_type,
        display_main_effect_type=display_main_effect_type,
        status=_status_projection(raw),
        step_type_value=state.step_type_value,
    )


# API aliases matching the existing Plan 2 naming and the runtime bridge's
# ``project_*`` vocabulary.  They all retain the same state-before-only rule.
plan1_runtime_scoring_projection = project_plan1_runtime_scoring
plan1_runtime_scoring_context_from_state_before = project_plan1_runtime_scoring
plan1_battle_scoring_context_from_exam_save = project_plan1_runtime_scoring
project_plan1_runtime_settings = project_plan1_runtime_scoring
plan1_runtime_settings_from_state_before = project_plan1_runtime_scoring
Plan1RuntimeSettingsProjection = Plan1RuntimeScoringProjection


__all__ = [
    "PLAN1_AUDITION_EXAM_TYPE",
    "PLAN1_RUNTIME_SCORING_ADAPTER_ID",
    "PLAN1_RUNTIME_SCORING_SCHEMA_VERSION",
    "Plan1RuntimeScoringError",
    "Plan1RuntimeScoringProjection",
    "Plan1RuntimeSettingsProjection",
    "Plan1RuntimeStatusProjection",
    "plan1_battle_scoring_context_from_exam_save",
    "plan1_runtime_scoring_context_from_state_before",
    "plan1_runtime_settings_from_state_before",
    "plan1_runtime_scoring_projection",
    "project_plan1_runtime_settings",
    "project_plan1_runtime_scoring",
]
