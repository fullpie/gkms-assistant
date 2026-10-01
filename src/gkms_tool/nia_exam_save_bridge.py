"""Thin N.I.A. ``ExamSaveData`` to exact static audition bridge.

Android v3.2.3 ``ExamSaveData`` persists ``stepType`` and the *current* exam
RNG state.  It does not persist the produce ID, audition difficulty number or
``ProduceStepAuditionDifficultyKey``.  Nor does a normal in-audition save prove
that its current RNG state is the state immediately before
``CalcTurnParameterType``.  Callers therefore supply those four pieces of run
context next to a decrypted/raw ExamSaveData mapping.  The higher-level
``nia_outer_exam_save_adapter`` derives the identity context from the outer
Produce play log, this ExamSaveData and Master; direct callers of this low-level
bridge may still provide it explicitly.

This module only joins that context to the local Master and replays the APK
turn-parameter algorithm.  It performs no decryption, OCR, process access,
input action, persistence or integrity checking.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .audition_rules import FINAL, MID1, MID2
from .nia_static_adapter import (
    DEFAULT_MASTER_DIR,
    NiaAuditionDefinition,
    NiaNpcScoreRange,
    NiaReplayedTurnParameterSchedule,
    load_nia_static_bundle,
    replay_nia_turn_parameter_schedule,
)


_MISSING = object()
_STEP_TYPE_BY_NUMBER = {16: MID1, 17: MID2, 18: FINAL}
_STEP_TYPE_NAMES = frozenset(_STEP_TYPE_BY_NUMBER.values())


@dataclass(frozen=True, slots=True)
class NiaExamSaveBridgeIssue:
    """One functional input preventing an exact profile or schedule join."""

    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("issue code and field must be non-empty")


@dataclass(frozen=True, slots=True)
class NiaExamSaveDifficultyKey:
    """Android's four-field ``ProduceStepAuditionDifficultyKey``."""

    row_id: str
    produce_id: str
    step_type: str
    number: int


@dataclass(frozen=True, slots=True)
class NiaExamSaveAuditionProjection:
    """Static audition profile and exact turn schedule, when resolvable."""

    produce_id: str | None
    step_type: str | None
    number: int | None
    difficulty_key: NiaExamSaveDifficultyKey | None
    pre_schedule_random_state: int | None
    profile: NiaAuditionDefinition | None
    turn_schedule: NiaReplayedTurnParameterSchedule | None
    issues: tuple[NiaExamSaveBridgeIssue, ...]

    @property
    def complete(self) -> bool:
        return (
            not self.issues
            and self.profile is not None
            and self.turn_schedule is not None
        )

    @property
    def npc_scores(self) -> tuple[NiaNpcScoreRange, ...]:
        return self.profile.npc_scores if self.profile is not None else ()

    @property
    def clear_rank(self) -> int | None:
        return self.profile.clear_rank if self.profile is not None else None


def _issue(
    issues: list[NiaExamSaveBridgeIssue],
    code: str,
    field: str,
    detail: str = "",
) -> None:
    candidate = NiaExamSaveBridgeIssue(code, field, detail)
    if candidate not in issues:
        issues.append(candidate)


def _value(mapping: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    return _MISSING


def _text(
    value: object,
    *,
    field: str,
    issues: list[NiaExamSaveBridgeIssue],
    missing_code: str,
    invalid_code: str,
) -> str | None:
    if value is _MISSING:
        _issue(issues, missing_code, field)
        return None
    if not isinstance(value, str) or not value:
        _issue(issues, invalid_code, field, "expected non-empty text")
        return None
    return value


def _positive_int(
    value: object,
    *,
    field: str,
    issues: list[NiaExamSaveBridgeIssue],
    missing_code: str,
    invalid_code: str,
) -> int | None:
    if value is _MISSING:
        _issue(issues, missing_code, field)
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _issue(issues, invalid_code, field, "expected a positive integer")
        return None
    return value


def _step_type(
    value: object,
    *,
    field: str,
    issues: list[NiaExamSaveBridgeIssue],
    missing_code: str,
    invalid_code: str,
) -> str | None:
    if value is _MISSING:
        _issue(issues, missing_code, field)
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        result = _STEP_TYPE_BY_NUMBER.get(value)
        if result is not None:
            return result
    elif isinstance(value, str) and value in _STEP_TYPE_NAMES:
        return value
    _issue(
        issues,
        invalid_code,
        field,
        "expected N.I.A. audition stepType 16, 17, 18 or its full enum name",
    )
    return None


def _random_state(
    value: object, issues: list[NiaExamSaveBridgeIssue]
) -> int | None:
    field = "randomStateBeforeTurnSchedule"
    if value is _MISSING:
        _issue(
            issues,
            "pre-schedule-random-state-missing",
            field,
            "ExamSaveData.random is the current state and cannot be assumed "
            "to precede CalcTurnParameterType",
        )
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 0xFFFFFFFF
    ):
        _issue(
            issues,
            "pre-schedule-random-state-invalid",
            field,
            "expected uint32",
        )
        return None
    return value


def _difficulty_key(
    value: object, issues: list[NiaExamSaveBridgeIssue]
) -> NiaExamSaveDifficultyKey | None:
    field = "difficultyKey"
    if value is _MISSING:
        _issue(issues, "difficulty-key-missing", field)
        return None
    if not isinstance(value, Mapping):
        _issue(
            issues,
            "difficulty-key-invalid",
            field,
            "expected an object with id/produceId/stepType/number",
        )
        return None

    row_id = _text(
        _value(value, "id", "rowId", "row_id"),
        field=f"{field}.id",
        issues=issues,
        missing_code="difficulty-key-field-missing",
        invalid_code="difficulty-key-field-invalid",
    )
    produce_id = _text(
        _value(value, "produceId", "produce_id"),
        field=f"{field}.produceId",
        issues=issues,
        missing_code="difficulty-key-field-missing",
        invalid_code="difficulty-key-field-invalid",
    )
    step_type = _step_type(
        _value(value, "stepType", "step_type"),
        field=f"{field}.stepType",
        issues=issues,
        missing_code="difficulty-key-field-missing",
        invalid_code="difficulty-key-field-invalid",
    )
    number = _positive_int(
        _value(value, "number"),
        field=f"{field}.number",
        issues=issues,
        missing_code="difficulty-key-field-missing",
        invalid_code="difficulty-key-field-invalid",
    )
    if None in (row_id, produce_id, step_type, number):
        return None
    return NiaExamSaveDifficultyKey(
        row_id=row_id,
        produce_id=produce_id,
        step_type=step_type,
        number=number,
    )


def _lookup_idol_card_id(
    payload: Mapping[str, object],
    exam_save: Mapping[str, object],
    key: NiaExamSaveDifficultyKey,
    issues: list[NiaExamSaveBridgeIssue],
) -> str | None:
    explicit = _value(payload, "idolCardId", "idol_card_id")
    if explicit is not _MISSING:
        return _text(
            explicit,
            field="idolCardId",
            issues=issues,
            missing_code="idol-card-id-missing",
            invalid_code="idol-card-id-invalid",
        )
    prefix = "p_step_audition_difficulty-"
    if key.row_id.startswith(prefix):
        suffix = key.row_id[len(prefix) :]
        if suffix:
            return suffix
    return _text(
        _value(exam_save, "characterId", "character_id"),
        field="examSaveData.characterId",
        issues=issues,
        missing_code="character-id-missing",
        invalid_code="character-id-invalid",
    )


@lru_cache(maxsize=32)
def _static_profiles(
    idol_card_id: str,
    produce_id: str,
    master_dir: Path,
) -> tuple[NiaAuditionDefinition, ...]:
    return load_nia_static_bundle(
        idol_card_id,
        produce_id=produce_id,
        active_item_ids=(),
        master_dir=master_dir,
    ).auditions


def load_runtime_audition_profiles(
    idol_card_id: str, produce_id: str, master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[NiaAuditionDefinition, ...]:
    """Share the typed rule graph for all Master-bound audition modes.

    The historical Nia* value classes hold generic BattleConfig/NPC/gimmick
    data. No NIA vote formula is applied: live scoring uses serialized native
    parameter bonuses, including the game's HIF star contribution.
    """
    from .runtime_mode_profile import load_mode_audition_options
    from .nia_static_adapter import (
        NiaAuditionDifficultyKey, _gimmicks, _npc_scores, _score_curve,
        _turn_parameter_schedule,
    )

    directory = Path(master_dir)
    options = load_mode_audition_options(produce_id, idol_card_id, master_dir=directory)
    return tuple(NiaAuditionDefinition(
        rules=item.rules,
        difficulty_key=NiaAuditionDifficultyKey(item.difficulty_row_id, produce_id,
            item.rules.step_type, item.rules.number, item.rules.battle_config_id),
        audition_type=item.audition_type,
        is_static_npc_score=item.is_static_npc_score,
        vote_count_baseline=item.vote_count_baseline, vote_count=item.required_vote_count,
        dearness_level=item.required_dearness_level,
        score_curve=_score_curve(item.rules.score_config_id, directory),
        npc_scores=_npc_scores(item.rules.npc_group_id, directory),
        gimmicks=(_gimmicks(item.rules.gimmick_group_id, directory) if item.rules.gimmick_group_id else ()),
        turn_parameter_schedule=_turn_parameter_schedule(item.rules),
    ) for item in options)


def project_nia_exam_save_audition(
    payload: Mapping[str, object],
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
    _profile_loader: Callable = _static_profiles,
) -> NiaExamSaveAuditionProjection:
    """Join a decrypted/raw N.I.A. exam mapping to one exact audition.

    Accepted input is either an enriched flat ExamSaveData mapping or a thin
    wrapper with ``examSaveData`` plus:

    - ``difficultyKey``: official id/produceId/stepType/number key
    - ``randomStateBeforeTurnSchedule``: uint32 immediately before the APK
      creates ``turnStatusParameterTypeList``

    Optional top-level ``produceId``, ``auditionNumber`` and ``idolCardId`` are
    cross-checked or used to select an exact-card Master row.
    """

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    issues: list[NiaExamSaveBridgeIssue] = []

    exam_value = _value(payload, "examSaveData", "exam_save_data")
    if exam_value is _MISSING:
        exam_save: Mapping[str, object] = payload
    elif isinstance(exam_value, Mapping):
        exam_save = exam_value
    else:
        _issue(
            issues,
            "exam-save-data-invalid",
            "examSaveData",
            "expected an object",
        )
        exam_save = {}

    key = _difficulty_key(
        _value(
            payload,
            "difficultyKey",
            "difficulty_key",
            "produceStepAuditionDifficultyKey",
        ),
        issues,
    )
    exam_step_type = _step_type(
        _value(exam_save, "stepType", "step_type"),
        field="examSaveData.stepType",
        issues=issues,
        missing_code="step-type-missing",
        invalid_code="step-type-invalid",
    )
    random_state = _random_state(
        _value(
            payload,
            "randomStateBeforeTurnSchedule",
            "random_state_before_turn_schedule",
            "preScheduleRandomState",
        ),
        issues,
    )

    produce_id = key.produce_id if key is not None else None
    step_type = key.step_type if key is not None else exam_step_type
    number = key.number if key is not None else None

    explicit_produce_id = _value(payload, "produceId", "produce_id")
    if explicit_produce_id is not _MISSING:
        parsed = _text(
            explicit_produce_id,
            field="produceId",
            issues=issues,
            missing_code="produce-id-missing",
            invalid_code="produce-id-invalid",
        )
        if produce_id is None:
            produce_id = parsed
        elif parsed is not None and parsed != produce_id:
            _issue(
                issues,
                "produce-id-mismatch",
                "produceId",
                f"{parsed} != difficultyKey.produceId {produce_id}",
            )

    explicit_number = _value(payload, "auditionNumber", "audition_number")
    if explicit_number is not _MISSING:
        parsed_number = _positive_int(
            explicit_number,
            field="auditionNumber",
            issues=issues,
            missing_code="audition-number-missing",
            invalid_code="audition-number-invalid",
        )
        if number is None:
            number = parsed_number
        elif parsed_number is not None and parsed_number != number:
            _issue(
                issues,
                "audition-number-mismatch",
                "auditionNumber",
                f"{parsed_number} != difficultyKey.number {number}",
            )

    if key is not None and exam_step_type is not None and key.step_type != exam_step_type:
        _issue(
            issues,
            "step-type-mismatch",
            "examSaveData.stepType",
            f"{exam_step_type} != difficultyKey.stepType {key.step_type}",
        )

    profile: NiaAuditionDefinition | None = None
    if key is not None and produce_id is not None and step_type is not None and number is not None:
        idol_card_id = _lookup_idol_card_id(
            payload, exam_save, key, issues
        )
        if idol_card_id is not None:
            try:
                profiles = _profile_loader(
                    idol_card_id,
                    produce_id,
                    Path(master_dir).resolve(),
                )
            except (FileNotFoundError, KeyError, ValueError) as error:
                _issue(
                    issues,
                    "static-profile-unavailable",
                    "difficultyKey",
                    str(error),
                )
            else:
                profile = next(
                    (
                        audition
                        for audition in profiles
                        if audition.rules.step_type == step_type
                        and audition.rules.number == number
                    ),
                    None,
                )
                if profile is None:
                    _issue(
                        issues,
                        "difficulty-not-found",
                        "difficultyKey",
                        f"{produce_id}/{step_type}/{number}",
                    )
                elif profile.difficulty_key.row_id != key.row_id:
                    _issue(
                        issues,
                        "difficulty-row-id-mismatch",
                        "difficultyKey.id",
                        f"{key.row_id} != Master {profile.difficulty_key.row_id}",
                    )

    if profile is not None:
        limit_turn = _value(exam_save, "limitTurn", "limit_turn")
        if limit_turn is not _MISSING:
            if isinstance(limit_turn, bool) or not isinstance(limit_turn, int):
                _issue(
                    issues,
                    "limit-turn-invalid",
                    "examSaveData.limitTurn",
                    "expected integer",
                )
            elif limit_turn != profile.rules.turns:
                _issue(
                    issues,
                    "limit-turn-mismatch",
                    "examSaveData.limitTurn",
                    f"{limit_turn} != Master {profile.rules.turns}",
                )

    turn_schedule: NiaReplayedTurnParameterSchedule | None = None
    if profile is not None and random_state is not None and not issues:
        turn_schedule = replay_nia_turn_parameter_schedule(
            profile.turn_parameter_schedule,
            random_state,
        )

    return NiaExamSaveAuditionProjection(
        produce_id=produce_id,
        step_type=step_type,
        number=number,
        difficulty_key=key,
        pre_schedule_random_state=random_state,
        profile=profile,
        turn_schedule=turn_schedule,
        issues=tuple(issues),
    )


def project_runtime_exam_save_audition(
    payload: Mapping[str, object], *, master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaExamSaveAuditionProjection:
    """Master-bound audition projection without a NIA-mode bundle gate."""
    return project_nia_exam_save_audition(payload, master_dir=master_dir,
                                         _profile_loader=load_runtime_audition_profiles)


__all__ = [
    "NiaExamSaveAuditionProjection",
    "NiaExamSaveBridgeIssue",
    "NiaExamSaveDifficultyKey",
    "project_nia_exam_save_audition",
    "load_runtime_audition_profiles",
    "project_runtime_exam_save_audition",
]
