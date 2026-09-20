"""Static N.I.A. mode adapter with explicit runtime-owned gaps.

The extracted Master proves the N.I.A. mode totals, audition variants,
battle configuration, score curves, NPC score ranges, gimmick schedules and
item definitions.  It does *not* contain one canonical weekly action list.
Android v3.2.3 exposes that list as repeated ``ProduceSchedule`` protobuf
records on the run/memory payload, so callers may provide those records here
without OCR.

This module is deliberately a read-only data join.  It performs no game
control, persistence, hashing, clock checks or tamper validation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from pathlib import Path
from struct import pack, unpack
from typing import Any

import yaml

from .audition_rules import (
    FINAL,
    MID1,
    MID2,
    AuditionRules,
    list_audition_rules,
)
from .exam_native_rng import next_range


PROJECT_ROOT = Path(__file__).resolve().parents[2]
from .application_paths import master_directory
DEFAULT_MASTER_DIR = master_directory()

NIA_GROUP_ID = "produce_group-002"
NIA_PRODUCE_IDS = {"produce-004": "Pro", "produce-005": "Master"}

# OriginalName values in Android v3.2.3 ProduceStepType.  Runtime JSON may
# contain either the protobuf enum integer or its full OriginalName string.
_STEP_TYPES = {
    0: "ProduceStepType_Unknown",
    1: "ProduceStepType_LessonVocalNormal",
    2: "ProduceStepType_LessonVocalSp",
    3: "ProduceStepType_LessonVocalHard",
    4: "ProduceStepType_LessonDanceNormal",
    5: "ProduceStepType_LessonDanceSp",
    6: "ProduceStepType_LessonDanceHard",
    7: "ProduceStepType_LessonVisualNormal",
    8: "ProduceStepType_LessonVisualSp",
    9: "ProduceStepType_LessonVisualHard",
    10: "ProduceStepType_Event",
    11: "ProduceStepType_EventActivity",
    12: "ProduceStepType_EventSchool",
    13: "ProduceStepType_Shop",
    14: "ProduceStepType_Refresh",
    15: "ProduceStepType_Present",
    16: MID1,
    17: MID2,
    18: FINAL,
    19: "ProduceStepType_SelfLessonVocalNormal",
    20: "ProduceStepType_SelfLessonVocalSp",
    21: "ProduceStepType_SelfLessonDanceNormal",
    22: "ProduceStepType_SelfLessonDanceSp",
    23: "ProduceStepType_SelfLessonVisualNormal",
    24: "ProduceStepType_SelfLessonVisualSp",
    25: "ProduceStepType_Business",
    26: "ProduceStepType_EventBusiness",
    27: "ProduceStepType_FanPresent",
    28: "ProduceStepType_Customize",
    29: "ProduceStepType_LegendLessonVocalNormal",
    30: "ProduceStepType_LegendLessonVocalSp",
    31: "ProduceStepType_LegendLessonDanceNormal",
    32: "ProduceStepType_LegendLessonDanceSp",
    33: "ProduceStepType_LegendLessonVisualNormal",
    34: "ProduceStepType_LegendLessonVisualSp",
    35: "ProduceStepType_OpenLessonVocalNormal",
    36: "ProduceStepType_OpenLessonVocalSp",
    37: "ProduceStepType_OpenLessonVocalNormalStar",
    38: "ProduceStepType_OpenLessonVocalSpStar",
    39: "ProduceStepType_OpenLessonDanceNormal",
    40: "ProduceStepType_OpenLessonDanceSp",
    41: "ProduceStepType_OpenLessonDanceNormalStar",
    42: "ProduceStepType_OpenLessonDanceSpStar",
    43: "ProduceStepType_OpenLessonVisualNormal",
    44: "ProduceStepType_OpenLessonVisualSp",
    45: "ProduceStepType_OpenLessonVisualNormalStar",
    46: "ProduceStepType_OpenLessonVisualSpStar",
    47: "ProduceStepType_Interval",
    48: "ProduceStepType_EventSchoolVocal",
    49: "ProduceStepType_EventSchoolDance",
    50: "ProduceStepType_EventSchoolVisual",
}
_STEP_TYPE_NAMES = frozenset(_STEP_TYPES.values())
_PARAMETER_TYPES = {
    0: "ProduceParameterType_Unknown",
    1: "ProduceParameterType_Vocal",
    2: "ProduceParameterType_Dance",
    3: "ProduceParameterType_Visual",
}
_PARAMETER_TYPE_NAMES = frozenset(_PARAMETER_TYPES.values())


DIRECT_PLAN3_REUSE = (
    "ProduceCard/ProduceExamEffect/ProduceExamTrigger schema",
    "ExamSetting stance, hand, draw and turn-end constants",
    "GUID Hand/Deck/Grave/Lost/Hold zones and native RNG",
    "support HandAdd temporary-upgrade evaluation",
    "ProduceExamGimmickEffectGroup row scheduling",
)

NIA_SPECIFIC_SURFACES = (
    "runtime ProduceSchedule weekly action list",
    "three audition lifecycle stages with selectable difficulty",
    "three-attribute battle score curves and per-turn attribute order",
    "NPC score ranges, rank threshold and runtime NPC modifiers",
    "Business/SelfLesson/FanPresent/Interval route effects and rewards",
)


@dataclass(frozen=True, slots=True)
class NiaStaticDiagnostic:
    code: str
    detail: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NiaRuntimeScheduleStep:
    number: int
    selected_step_type: str
    step_types: tuple[str, ...]
    step_sub_parameter_types: tuple[str, ...]
    refresh_stamina: int

    @property
    def all_step_types(self) -> tuple[str, ...]:
        values = [self.selected_step_type, *self.step_types]
        return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class NiaScheduleAdaptation:
    steps: tuple[NiaRuntimeScheduleStep, ...]
    diagnostics: tuple[NiaStaticDiagnostic, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.diagnostics

    @property
    def audition_weeks(self) -> tuple[tuple[str, int], ...]:
        result: list[tuple[str, int]] = []
        for step in self.steps:
            for stage in (MID1, MID2, FINAL):
                if stage in step.all_step_types:
                    result.append((stage, step.number))
        return tuple(result)


@dataclass(frozen=True, slots=True)
class NiaProduceSetting:
    initial_produce_point: int
    refresh_stamina_recovery_permille: int
    before_audition_refresh_stamina_recovery_permille: int
    step_skip_stamina_recovery_permille: int
    customize_produce_card_count: int
    continue_count: int
    audition_trend_upper_permille: int
    audition_trend_lower_permille: int


@dataclass(frozen=True, slots=True)
class NiaModeDefinition:
    produce_id: str
    difficulty: str
    group_id: str
    group_type: str
    base_step_level: int
    total_steps: int
    max_refresh_count: int
    action_point_quantity: int
    parameter_growth_limit: int
    exam_setting_id: str
    produce_setting_id: str
    force_common_live: bool
    easy_item_ids: tuple[str, ...]
    live_types: tuple[str, ...]
    setting: NiaProduceSetting


@dataclass(frozen=True, slots=True)
class NiaScoreCurvePoint:
    parameter: int
    vocal_permille: int
    dance_permille: int
    visual_permille: int


@dataclass(frozen=True, slots=True)
class NiaNpcScoreRange:
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


@dataclass(frozen=True, slots=True)
class NiaGimmickStep:
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


@dataclass(frozen=True, slots=True)
class NiaAuditionDifficultyKey:
    """The real composite key of a repeated Master difficulty row."""

    row_id: str
    produce_id: str
    step_type: str
    number: int
    battle_config_id: str

    @property
    def stable_key(self) -> str:
        return "/".join(
            (
                self.row_id,
                self.produce_id,
                self.step_type,
                str(self.number),
                self.battle_config_id,
            )
        )


@dataclass(frozen=True, slots=True)
class NiaTurnParameterWeight:
    parameter_type: str
    parameter: int
    random_pool_turns: int


@dataclass(frozen=True, slots=True)
class NiaTurnParameterSchedule:
    """Static schedule recipe recovered from ``CalcTurnParameterType``.

    The first ``random_turns`` entries are drawn without replacement from
    ``ordered_weights``.  The last three entries are deterministic.  A live
    ``ExamParameterModel.RandomState`` is needed only to replay the random
    prefix, not to rediscover the schedule formula.
    """

    turns: int
    ordered_weights: tuple[NiaTurnParameterWeight, ...]
    fixed_tail: tuple[str, str, str]
    algorithm: str = "android-v3.2.3-exam-parameter-calc"

    @property
    def random_turns(self) -> int:
        return self.turns - len(self.fixed_tail)

    @property
    def random_pool(self) -> tuple[str, ...]:
        return tuple(
            parameter_type
            for item in self.ordered_weights
            for parameter_type in (item.parameter_type,) * item.random_pool_turns
        )


@dataclass(frozen=True, slots=True)
class NiaReplayedTurnParameterSchedule:
    parameter_types: tuple[str, ...]
    random_state_before: int
    random_state_after: int
    rng_draws: int


@dataclass(frozen=True, slots=True)
class NiaAuditionDefinition:
    rules: AuditionRules
    difficulty_key: NiaAuditionDifficultyKey
    audition_type: str
    is_static_npc_score: bool
    vote_count_baseline: int
    vote_count: int
    dearness_level: int
    score_curve: tuple[NiaScoreCurvePoint, ...]
    npc_scores: tuple[NiaNpcScoreRange, ...]
    gimmicks: tuple[NiaGimmickStep, ...]
    turn_parameter_schedule: NiaTurnParameterSchedule

    @property
    def pass_rank_threshold(self) -> int:
        return self.rules.rank_threshold

    @property
    def clear_rank(self) -> int:
        return self.rules.rank_threshold


@dataclass(frozen=True, slots=True)
class NiaSelfLessonDefinition:
    id: str
    progress_level: int
    stamina: int
    parameter: int


@dataclass(frozen=True, slots=True)
class NiaItemEffectDefinition:
    id: str
    effect_type: str
    effect_turn: int
    effect_count: int
    produce_effect_id: str
    status_enchant_id: str


@dataclass(frozen=True, slots=True)
class NiaItemDefinition:
    id: str
    name: str
    plan_type: str
    fire_limit: int
    fire_interval: int
    trigger_ids: tuple[str, ...]
    effects: tuple[NiaItemEffectDefinition, ...]
    is_exam_effect: bool
    is_easy: bool


@dataclass(frozen=True, slots=True)
class NiaStaticBundle:
    mode: NiaModeDefinition
    schedule: NiaScheduleAdaptation | None
    auditions: tuple[NiaAuditionDefinition, ...]
    self_lessons: tuple[NiaSelfLessonDefinition, ...]
    active_items: tuple[NiaItemDefinition, ...]
    score_penalty_min_permille: int
    score_penalty_max_permille: int
    diagnostics: tuple[NiaStaticDiagnostic, ...]
    direct_plan3_reuse: tuple[str, ...] = DIRECT_PLAN3_REUSE
    nia_specific_surfaces: tuple[str, ...] = NIA_SPECIFIC_SURFACES

    @property
    def static_join_ready(self) -> bool:
        return bool(self.auditions) and self.mode.total_steps > 0

    @property
    def full_simulation_ready(self) -> bool:
        return self.static_join_ready and not self.diagnostics


@lru_cache(maxsize=24)
def _rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Master file not found: {target}")
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(target.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError(f"{target.name} must contain a list")
    return tuple(row for row in payload if isinstance(row, Mapping))


@lru_cache(maxsize=12)
def _rows_by_id(
    path: Path,
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for row in _rows(path):
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            buckets.setdefault(row_id, []).append(row)
    return {key: tuple(values) for key, values in buckets.items()}


def _one(
    rows: Sequence[Mapping[str, Any]], label: str, predicate: Any
) -> Mapping[str, Any]:
    matches = [row for row in rows if predicate(row)]
    if len(matches) != 1:
        raise KeyError(f"{label} must resolve exactly once, found {len(matches)}")
    return matches[0]


def _int(row: Mapping[str, Any], key: str, *, minimum: int | None = None) -> int:
    value = row.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def _text(row: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = row.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{key} must be text")
    return value


def _strings(row: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = row.get(key, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _field(row: Mapping[str, object], camel: str, snake: str) -> object:
    if camel in row:
        return row[camel]
    return row.get(snake)


def _normalize_enum(
    value: object,
    *,
    by_number: Mapping[int, str],
    names: frozenset[str],
    label: str,
) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return by_number[value]
        except KeyError as error:
            raise ValueError(f"unknown {label} enum value: {value}") from error
    if isinstance(value, str) and value in names:
        return value
    raise ValueError(f"invalid {label}: {value!r}")


def _normalize_enum_list(
    value: object,
    *,
    by_number: Mapping[int, str],
    names: frozenset[str],
    label: str,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list")
    return tuple(
        _normalize_enum(
            item, by_number=by_number, names=names, label=label
        )
        for item in value
    )


def adapt_nia_runtime_schedule(
    records: Sequence[Mapping[str, object]],
    *,
    expected_steps: int | None = None,
) -> NiaScheduleAdaptation:
    """Normalize Android ``ProduceSchedule`` records without OCR."""

    steps: list[NiaRuntimeScheduleStep] = []
    for index, row in enumerate(records):
        if not isinstance(row, Mapping):
            raise TypeError(f"schedule[{index}] must be an object")
        number = _field(row, "number", "number")
        refresh = _field(row, "refreshStamina", "refresh_stamina")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError(f"schedule[{index}].number must be positive")
        if not isinstance(refresh, int) or isinstance(refresh, bool) or refresh < 0:
            raise ValueError(
                f"schedule[{index}].refreshStamina must be non-negative"
            )
        selected = _normalize_enum(
            _field(row, "selectedStepType", "selected_step_type"),
            by_number=_STEP_TYPES,
            names=_STEP_TYPE_NAMES,
            label="ProduceStepType",
        )
        step_types = _normalize_enum_list(
            _field(row, "stepTypes", "step_types"),
            by_number=_STEP_TYPES,
            names=_STEP_TYPE_NAMES,
            label="ProduceStepType",
        )
        parameters = _normalize_enum_list(
            _field(
                row,
                "stepSubParameterTypes",
                "step_sub_parameter_types",
            ),
            by_number=_PARAMETER_TYPES,
            names=_PARAMETER_TYPE_NAMES,
            label="ProduceParameterType",
        )
        steps.append(
            NiaRuntimeScheduleStep(
                number=number,
                selected_step_type=selected,
                step_types=step_types,
                step_sub_parameter_types=parameters,
                refresh_stamina=refresh,
            )
        )
    steps.sort(key=lambda item: item.number)
    numbers = tuple(item.number for item in steps)
    if len(numbers) != len(set(numbers)):
        raise ValueError("schedule numbers must be unique")

    diagnostics: list[NiaStaticDiagnostic] = []
    if expected_steps is not None:
        if expected_steps < 1:
            raise ValueError("expected_steps must be positive")
        expected_numbers = tuple(range(1, expected_steps + 1))
        if numbers != expected_numbers:
            diagnostics.append(
                NiaStaticDiagnostic(
                    "runtime-schedule-incomplete",
                    f"expected weeks 1..{expected_steps}, received {numbers}",
                    ("ProduceSchedule.Number",),
                )
            )
        present = {
            value
            for step in steps
            for value in step.all_step_types
            if value in {MID1, MID2, FINAL}
        }
        missing = tuple(stage for stage in (MID1, MID2, FINAL) if stage not in present)
        if missing:
            diagnostics.append(
                NiaStaticDiagnostic(
                    "runtime-audition-weeks-incomplete",
                    "missing schedule stages: " + ",".join(missing),
                    ("ProduceSchedule.SelectedStepType", "ProduceSchedule.StepTypes"),
                )
            )
    return NiaScheduleAdaptation(tuple(steps), tuple(diagnostics))


def _difficulty_ids(idol_card_id: str) -> tuple[str, ...]:
    exact = f"p_step_audition_difficulty-{idol_card_id}"
    parts = idol_card_id.split("-")
    if len(parts) >= 3 and parts[0] == "i_card" and parts[1]:
        character = f"p_step_audition_difficulty-{parts[1]}"
        if character != exact:
            return exact, character
    return (exact,)


def _difficulty_row(
    rules: AuditionRules,
    idol_card_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    # Exact-card and character fallback rows can coexist for the same stage.
    # Android's IdolCard points at one row ID, so preserve that priority rather
    # than treating both IDs as one unordered candidate set.
    for candidate_id in _difficulty_ids(idol_card_id):
        matches = tuple(
            row
            for row in rows
            if (
                row.get("id") == candidate_id
                and row.get("produceId") == rules.produce_id
                and row.get("stepType") == rules.step_type
                and row.get("number") == rules.number
                and row.get("produceExamBattleConfigId")
                == rules.battle_config_id
            )
        )
        if len(matches) > 1:
            raise KeyError(
                "N.I.A. audition difficulty must resolve exactly once, "
                f"found {len(matches)} for {candidate_id}"
            )
        if matches:
            return matches[0]
    raise KeyError("N.I.A. audition difficulty must resolve exactly once, found 0")


def _difficulty_key(
    rules: AuditionRules, row: Mapping[str, Any]
) -> NiaAuditionDifficultyKey:
    return NiaAuditionDifficultyKey(
        row_id=_text(row, "id"),
        produce_id=_text(row, "produceId"),
        step_type=_text(row, "stepType"),
        number=_int(row, "number", minimum=1),
        battle_config_id=_text(row, "produceExamBattleConfigId"),
    )


def _float32(value: float | int) -> float:
    return unpack("<f", pack("<f", float(value)))[0]


def _float32_ratio(numerator_a: int, numerator_b: int, denominator: int) -> float:
    product = _float32(_float32(numerator_a) * _float32(numerator_b))
    return _float32(product / _float32(denominator))


def _turn_parameter_schedule(rules: AuditionRules) -> NiaTurnParameterSchedule:
    """Mirror Android v3.2.3 ``CalcTurnParameterType`` pool construction."""

    if rules.turns < 3:
        raise ValueError("audition turn schedule requires at least three turns")
    ordered = sorted(
        (
            ("ProduceParameterType_Vocal", rules.vocal_parameter),
            ("ProduceParameterType_Dance", rules.dance_parameter),
            ("ProduceParameterType_Visual", rules.visual_parameter),
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    if any(parameter < 0 for _, parameter in ordered):
        raise ValueError("audition parameter weights must be non-negative")
    total_parameter = sum(parameter for _, parameter in ordered)
    if total_parameter <= 0:
        raise ValueError("audition parameter weights must contain a positive value")

    random_turns = rules.turns - 3
    first_count = ceil(
        _float32_ratio(random_turns, ordered[0][1], total_parameter)
    )
    remaining = random_turns - first_count
    lower_total = ordered[1][1] + ordered[2][1]
    if lower_total <= 0:
        second_count = remaining
    else:
        # The APK calculates this quotient as float32 and passes the promoted
        # value to System.Math.Round, whose default midpoint rule is ToEven.
        second_count = round(
            _float32_ratio(remaining, ordered[1][1], lower_total)
        )
    third_count = remaining - second_count
    counts = (first_count, second_count, third_count)
    if any(count < 0 for count in counts) or sum(counts) != random_turns:
        raise ValueError("native audition parameter pool counts are invalid")

    weights = tuple(
        NiaTurnParameterWeight(
            parameter_type=parameter_type,
            parameter=parameter,
            random_pool_turns=count,
        )
        for (parameter_type, parameter), count in zip(ordered, counts, strict=True)
    )
    return NiaTurnParameterSchedule(
        turns=rules.turns,
        ordered_weights=weights,
        fixed_tail=(ordered[2][0], ordered[1][0], ordered[0][0]),
    )


def replay_nia_turn_parameter_schedule(
    schedule: NiaTurnParameterSchedule,
    random_state: int,
) -> NiaReplayedTurnParameterSchedule:
    """Replay the APK schedule once the pre-schedule RNG state is known."""

    if not isinstance(schedule, NiaTurnParameterSchedule):
        raise TypeError("schedule must be a NiaTurnParameterSchedule")
    state_before = random_state
    state_after = random_state
    pool = list(schedule.random_pool)
    result: list[str] = []
    for _ in range(schedule.random_turns):
        index, state_after = next_range(state_after, 0, len(pool))
        result.append(pool.pop(index))
    result.extend(schedule.fixed_tail)
    return NiaReplayedTurnParameterSchedule(
        parameter_types=tuple(result),
        random_state_before=state_before,
        random_state_after=state_after,
        rng_draws=schedule.random_turns,
    )


def _score_curve(
    score_config_id: str, master_dir: Path
) -> tuple[NiaScoreCurvePoint, ...]:
    selected = _rows_by_id(
        master_dir / "ProduceExamBattleScoreConfig.yaml"
    ).get(score_config_id, ())
    if len(selected) < 2:
        raise KeyError(f"score curve is incomplete: {score_config_id}")
    result = tuple(
        NiaScoreCurvePoint(
            parameter=_int(row, "parameter", minimum=0),
            vocal_permille=_int(row, "vocalPermil", minimum=0),
            dance_permille=_int(row, "dancePermil", minimum=0),
            visual_permille=_int(row, "visualPermil", minimum=0),
        )
        for row in sorted(selected, key=lambda row: _int(row, "parameter"))
    )
    if len({point.parameter for point in result}) != len(result):
        raise ValueError(f"score curve repeats a parameter: {score_config_id}")
    return result


def _npc_scores(
    npc_group_id: str, master_dir: Path
) -> tuple[NiaNpcScoreRange, ...]:
    rows = _rows_by_id(master_dir / "ProduceExamBattleNpcGroup.yaml").get(
        npc_group_id, ()
    )
    if not rows:
        raise KeyError(f"NPC group not found: {npc_group_id}")
    mobs = _rows_by_id(master_dir / "ProduceExamBattleNpcMob.yaml")
    ordered = tuple(sorted(rows, key=lambda value: _int(value, "number")))
    result: list[NiaNpcScoreRange] = []
    for row in ordered:
        character_id = _text(row, "characterId", allow_empty=True) or None
        mob_id = (
            _text(row, "produceExamBattleNpcMobId", allow_empty=True) or None
        )
        if (character_id is None) == (mob_id is None):
            raise ValueError(
                "NPC score row must identify exactly one character or mob: "
                f"{npc_group_id}/{_int(row, 'number', minimum=1)}"
            )
        mob_name = ""
        if mob_id is not None:
            selected_mobs = mobs.get(mob_id, ())
            if len(selected_mobs) != 1:
                raise KeyError(
                    f"NPC mob identity is not unique: {mob_id} "
                    f"({len(selected_mobs)})"
                )
            mob_name = _text(selected_mobs[0], "name")
        result.append(
            NiaNpcScoreRange(
                number=_int(row, "number", minimum=1),
                character_id=character_id,
                mob_id=mob_id,
                mob_name=mob_name,
                score_min=_int(row, "scoreMin", minimum=0),
                score_max=_int(row, "scoreMax", minimum=0),
                vocal_permille=_int(row, "vocalPermil", minimum=0),
                dance_permille=_int(row, "dancePermil", minimum=0),
                visual_permille=_int(row, "visualPermil", minimum=0),
                opening_permille=_int(row, "opScorePermil", minimum=0),
                middle_permille=_int(row, "midScorePermil", minimum=0),
                ending_permille=_int(row, "edScorePermil", minimum=0),
            )
        )
    return tuple(result)


def _gimmicks(
    group_id: str, master_dir: Path
) -> tuple[NiaGimmickStep, ...]:
    rows = _rows_by_id(
        master_dir / "ProduceExamGimmickEffectGroup.yaml"
    ).get(group_id, ())
    if not rows:
        raise KeyError(f"gimmick group not found: {group_id}")
    return tuple(
        NiaGimmickStep(
            priority=_int(row, "priority"),
            start_turn=_int(row, "startTurn", minimum=0),
            remaining_turn_permille=_int(row, "remainingTurnPermil", minimum=0),
            remaining_turn=_int(row, "remainingTurn"),
            field_status_type=_text(row, "fieldStatusType"),
            field_status_value=_int(row, "fieldStatusValue"),
            field_status_check_type=_text(row, "fieldStatusCheckType"),
            field_status_card_search_id=_text(
                row, "fieldStatusProduceCardSearchId", allow_empty=True
            ),
            effect_id=_text(row, "produceExamEffectId"),
            is_positive=row.get("isPositive") is True,
        )
        for row in sorted(rows, key=lambda value: _int(value, "priority"))
    )


def load_nia_gimmick_steps(
    group_id: str,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[NiaGimmickStep, ...]:
    """Return one repeated-ID native gimmick schedule from static Master.

    This narrow public seam lets plan-specific runtime bridges evaluate the
    saved schedule's condition without reopening or duplicating the YAML
    grammar.  It does not execute an effect or infer runtime state.
    """

    if not isinstance(group_id, str) or not group_id:
        raise ValueError("gimmick group_id must be non-empty text")
    return _gimmicks(group_id, Path(master_dir))


def _auditions(
    idol_card_id: str, produce_id: str, master_dir: Path
) -> tuple[NiaAuditionDefinition, ...]:
    difficulty_rows = _rows(master_dir / "ProduceStepAuditionDifficulty.yaml")
    result: list[NiaAuditionDefinition] = []
    for rules in list_audition_rules(
        idol_card_id,
        produce_id=produce_id,
        master_dir=master_dir,
    ):
        row = _difficulty_row(rules, idol_card_id, difficulty_rows)
        result.append(
            NiaAuditionDefinition(
                rules=rules,
                difficulty_key=_difficulty_key(rules, row),
                audition_type=_text(row, "auditionType"),
                is_static_npc_score=row.get("isStaticNpcScore") is True,
                vote_count_baseline=_int(row, "voteCountBaseLine", minimum=0),
                vote_count=_int(row, "voteCount", minimum=0),
                dearness_level=_int(row, "dearnessLevel", minimum=0),
                score_curve=_score_curve(rules.score_config_id, master_dir),
                npc_scores=_npc_scores(rules.npc_group_id, master_dir),
                gimmicks=_gimmicks(rules.gimmick_group_id, master_dir),
                turn_parameter_schedule=_turn_parameter_schedule(rules),
            )
        )
    if not result:
        raise KeyError(f"no N.I.A. auditions found: {idol_card_id}/{produce_id}")
    return tuple(result)


def _self_lessons(
    produce_id: str, master_dir: Path
) -> tuple[NiaSelfLessonDefinition, ...]:
    prefix = f"self_lesson-{produce_id.replace('-', '_')}-"
    rows = [
        row
        for row in _rows(master_dir / "ProduceStepSelfLesson.yaml")
        if str(row.get("id", "")).startswith(prefix)
    ]
    return tuple(
        NiaSelfLessonDefinition(
            id=_text(row, "id"),
            progress_level=_int(row, "progressLevel", minimum=0),
            stamina=_int(row, "stamina", minimum=0),
            parameter=_int(row, "parameter", minimum=0),
        )
        for row in rows
    )


def load_nia_item_definitions(
    item_ids: Sequence[str],
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[NiaItemDefinition, ...]:
    directory = Path(master_dir)
    item_rows = _rows_by_id(directory / "ProduceItem.yaml")
    effect_rows = _rows_by_id(directory / "ProduceItemEffect.yaml")
    result: list[NiaItemDefinition] = []
    for item_id in item_ids:
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("item IDs must be non-empty text")
        matches = item_rows.get(item_id, ())
        if len(matches) != 1:
            raise KeyError(
                f"ProduceItem must resolve exactly once, found {len(matches)}"
            )
        row = matches[0]
        effect_ids = _strings(row, "produceItemEffectIds")
        effects: list[NiaItemEffectDefinition] = []
        for effect_id in effect_ids:
            effect_matches = effect_rows.get(effect_id, ())
            if len(effect_matches) != 1:
                raise KeyError(
                    "ProduceItemEffect must resolve exactly once: "
                    f"{effect_id} ({len(effect_matches)})"
                )
            effect = effect_matches[0]
            effects.append(
                NiaItemEffectDefinition(
                    id=effect_id,
                    effect_type=_text(effect, "effectType"),
                    effect_turn=_int(effect, "effectTurn"),
                    effect_count=_int(effect, "effectCount"),
                    produce_effect_id=_text(
                        effect, "produceEffectId", allow_empty=True
                    ),
                    status_enchant_id=_text(
                        effect, "produceExamStatusEnchantId", allow_empty=True
                    ),
                )
            )
        trigger_ids = [
            _text(row, "produceTriggerId", allow_empty=True),
            *_strings(row, "produceTriggerIds"),
        ]
        result.append(
            NiaItemDefinition(
                id=item_id,
                name=_text(row, "name", allow_empty=True),
                plan_type=_text(row, "planType"),
                fire_limit=_int(row, "fireLimit", minimum=0),
                fire_interval=_int(row, "fireInterval", minimum=0),
                trigger_ids=tuple(value for value in trigger_ids if value),
                effects=tuple(effects),
                is_exam_effect=row.get("isExamEffect") is True,
                is_easy=row.get("isEasy") is True,
            )
        )
    return tuple(result)


def _character_id(idol_card_id: str) -> str:
    parts = idol_card_id.split("-")
    if len(parts) >= 3 and parts[0] == "i_card" and parts[1]:
        return parts[1]
    return idol_card_id


def _mode_definition(
    produce_id: str, idol_card_id: str, master_dir: Path
) -> NiaModeDefinition:
    if produce_id not in NIA_PRODUCE_IDS:
        raise ValueError(f"not an N.I.A. produce mode: {produce_id}")
    mode = _one(
        _rows(master_dir / "Produce.yaml"),
        "N.I.A. Produce mode",
        lambda row: row.get("id") == produce_id,
    )
    group = _one(
        _rows(master_dir / "ProduceGroup.yaml"),
        "N.I.A. ProduceGroup",
        lambda row: (
            row.get("id") == NIA_GROUP_ID
            and produce_id in row.get("produceIds", [])
        ),
    )
    setting_id = _text(mode, "produceSettingId")
    setting = _one(
        _rows(master_dir / "ProduceSetting.yaml"),
        "N.I.A. ProduceSetting",
        lambda row: row.get("id") == setting_id,
    )
    character_id = _character_id(idol_card_id)
    live_types = tuple(
        _text(row, "liveType")
        for row in _rows(master_dir / "ProduceLiveEvaluation.yaml")
        if row.get("produceId") == produce_id
        and row.get("characterId") == character_id
    )
    return NiaModeDefinition(
        produce_id=produce_id,
        difficulty=NIA_PRODUCE_IDS[produce_id],
        group_id=NIA_GROUP_ID,
        group_type=_text(group, "type"),
        base_step_level=_int(mode, "baseStepLevel", minimum=1),
        total_steps=_int(mode, "steps", minimum=1),
        max_refresh_count=_int(mode, "maxRefreshCount", minimum=0),
        action_point_quantity=_int(mode, "actionPointQuantity", minimum=0),
        parameter_growth_limit=_int(
            mode, "idolCardParameterGrowthLimit", minimum=1
        ),
        exam_setting_id=_text(mode, "examSettingId"),
        produce_setting_id=setting_id,
        force_common_live=group.get("isForceLiveCommon") is True,
        easy_item_ids=_strings(mode, "easyProduceItemIds"),
        live_types=live_types,
        setting=NiaProduceSetting(
            initial_produce_point=_int(setting, "initialProducePoint", minimum=0),
            refresh_stamina_recovery_permille=_int(
                setting, "refreshStaminaRecoveryPermil", minimum=0
            ),
            before_audition_refresh_stamina_recovery_permille=_int(
                setting,
                "beforeAuditionRefreshStaminaRecoveryPermil",
                minimum=0,
            ),
            step_skip_stamina_recovery_permille=_int(
                setting, "stepSkipStaminaRecoveryPermil", minimum=0
            ),
            customize_produce_card_count=_int(
                setting, "customizeProduceCardCount", minimum=0
            ),
            continue_count=_int(setting, "continueCount", minimum=0),
            audition_trend_upper_permille=_int(
                setting, "produceAuditionTrendAssessmentPermilUpper", minimum=0
            ),
            audition_trend_lower_permille=_int(
                setting, "produceAuditionTrendAssessmentPermilLower", minimum=0
            ),
        ),
    )


def load_nia_static_bundle(
    idol_card_id: str,
    *,
    produce_id: str = "produce-004",
    runtime_schedule: Sequence[Mapping[str, object]] | None = None,
    active_item_ids: Sequence[str] | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaStaticBundle:
    """Join every static N.I.A. exam input currently proven locally."""

    if not isinstance(idol_card_id, str) or not idol_card_id:
        raise ValueError("idol_card_id must be non-empty text")
    directory = Path(master_dir)
    mode = _mode_definition(produce_id, idol_card_id, directory)
    diagnostics: list[NiaStaticDiagnostic] = []

    schedule: NiaScheduleAdaptation | None = None
    if runtime_schedule is None:
        diagnostics.append(
            NiaStaticDiagnostic(
                "runtime-schedule-required",
                "weekly choices are repeated ProduceSchedule records, not a fixed Master table",
                (
                    "ProduceSchedule.Number",
                    "ProduceSchedule.SelectedStepType",
                    "ProduceSchedule.StepTypes",
                    "ProduceSchedule.StepSubParameterTypes",
                    "ProduceSchedule.RefreshStamina",
                ),
            )
        )
    else:
        schedule = adapt_nia_runtime_schedule(
            runtime_schedule, expected_steps=mode.total_steps
        )
        diagnostics.extend(schedule.diagnostics)

    if active_item_ids is None:
        items: tuple[NiaItemDefinition, ...] = ()
        diagnostics.append(
            NiaStaticDiagnostic(
                "runtime-active-items-required",
                "the mode has no fixed active item loadout; use the current run item IDs",
                ("ProduceItem", "ProduceItemEffect"),
            )
        )
    else:
        items = load_nia_item_definitions(
            active_item_ids, master_dir=directory
        )

    settings = _one(
        _rows(directory / "Setting.yaml"),
        "global Setting",
        lambda row: True,
    )
    diagnostics.extend(
        (
            NiaStaticDiagnostic(
                "runtime-audition-rng-state-required",
                "Master weights and the APK algorithm prove the turn schedule recipe; replaying its random prefix requires the pre-schedule ExamParameterModel.RandomState",
                (
                    "ProduceExamBattleConfig.vocal/dance/visual/turn",
                    "ExamParameterModel.CalcTurnParameterType",
                    "ExamParameterModel.RandomState",
                ),
            ),
            NiaStaticDiagnostic(
                "runtime-audition-modifiers-required",
                "final multipliers and NPC scores depend on current attributes, E/V/Star and NPC enhance/weaken effects",
                (
                    "ProduceExamBattleScoreConfig",
                    "ProduceExamBattleNpcGroup.scoreMin/scoreMax",
                ),
            ),
            NiaStaticDiagnostic(
                "runtime-live-result-required",
                "ProduceLiveEvaluation lists allowed live types but contains no score-to-live threshold",
                tuple(mode.live_types),
            ),
        )
    )
    return NiaStaticBundle(
        mode=mode,
        schedule=schedule,
        auditions=_auditions(idol_card_id, produce_id, directory),
        self_lessons=_self_lessons(produce_id, directory),
        active_items=items,
        score_penalty_min_permille=_int(
            settings, "produceExamBattleScorePenaltyMinPermil", minimum=0
        ),
        score_penalty_max_permille=_int(
            settings, "produceExamBattleScorePenaltyMaxPermil", minimum=0
        ),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "DEFAULT_MASTER_DIR",
    "DIRECT_PLAN3_REUSE",
    "NIA_GROUP_ID",
    "NIA_PRODUCE_IDS",
    "NIA_SPECIFIC_SURFACES",
    "NiaAuditionDefinition",
    "NiaAuditionDifficultyKey",
    "NiaGimmickStep",
    "NiaItemDefinition",
    "NiaItemEffectDefinition",
    "NiaModeDefinition",
    "NiaNpcScoreRange",
    "NiaProduceSetting",
    "NiaRuntimeScheduleStep",
    "NiaScheduleAdaptation",
    "NiaScoreCurvePoint",
    "NiaSelfLessonDefinition",
    "NiaStaticBundle",
    "NiaStaticDiagnostic",
    "NiaReplayedTurnParameterSchedule",
    "NiaTurnParameterSchedule",
    "NiaTurnParameterWeight",
    "adapt_nia_runtime_schedule",
    "load_nia_gimmick_steps",
    "load_nia_item_definitions",
    "load_nia_static_bundle",
    "replay_nia_turn_parameter_schedule",
]
