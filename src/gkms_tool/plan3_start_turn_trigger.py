"""Typed, fail-closed inventory for ``ExamStartTurn`` trigger shapes.

This module is deliberately independent from :mod:`plan3_engine`.  It records
the rows present in the local Master/coverage evidence and exposes the small
part of the start-turn predicate that can be proven from the current
``Plan3State`` shape and the local PC/Android evidence.

The important distinction here is between a row being present in Master and a
row being executable by this resolver.  Unknown negation, threshold/multiple,
search, interval, and status-lifecycle semantics return ``UNRESOLVED`` and
never change either supplied state.  The exact resolved rows use the current
``ExamParameter``/status context at the settled start-turn event; they do not
interpret a card's or gimmick's event delta as the field value.
"""

from __future__ import annotations

import dataclasses
import struct
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence


# ---------------------------------------------------------------------------
# Master tokens used by the start-turn rows

PHASE_EXAM_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"

LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
LESSON_DANCE = "ProduceStepLessonType_LessonDance"
LESSON_SP = "ProduceStepLessonType_LessonSp"
LESSON_VISUAL = "ProduceStepLessonType_LessonVisual"
LESSON_VOCAL = "ProduceStepLessonType_LessonVocal"

CHECK_NOT = "ProduceExamTriggerCheckType_Not"

FIELD_BLOCK_UP = "ProduceExamFieldStatusType_BlockUp"
FIELD_CARD_PLAY_AGGRESSIVE_UP = (
    "ProduceExamFieldStatusType_CardPlayAggressiveUp"
)
FIELD_CARD_SEARCH_COUNT_UP = "ProduceExamFieldStatusType_CardSearchCountUp"
FIELD_CONCENTRATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_ConcentrationChangeCountUp"
)
FIELD_CONCENTRATION_UP = "ProduceExamFieldStatusType_ConcentrationUp"
FIELD_CONDITION_THRESHOLD_MULTIPLE = (
    "ProduceExamFieldStatusType_ConditionThresholdMultiple"
)
FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN = (
    "ProduceExamFieldStatusType_ConditionThresholdMultipleDown"
)
FIELD_FULL_POWER_POINT_GET_SUM_UP = (
    "ProduceExamFieldStatusType_FullPowerPointGetSumUp"
)
FIELD_FULL_POWER_POINT_UP = "ProduceExamFieldStatusType_FullPowerPointUp"
FIELD_FULL_POWER_UP = "ProduceExamFieldStatusType_FullPowerUp"
FIELD_LESSON_BUFF_UP = "ProduceExamFieldStatusType_LessonBuffUp"
FIELD_NO_BLOCK = "ProduceExamFieldStatusType_NoBlock"
FIELD_NO_STANCE = "ProduceExamFieldStatusType_NoStance"
FIELD_PARAMETER_BUFF = "ProduceExamFieldStatusType_ParameterBuff"
FIELD_PARAMETER_BUFF_UP = "ProduceExamFieldStatusType_ParameterBuffUp"
FIELD_PRESERVATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_PreservationChangeCountUp"
)
FIELD_PRESERVATION_UP = "ProduceExamFieldStatusType_PreservationUp"
FIELD_REMAINING_TURN = "ProduceExamFieldStatusType_RemainingTurn"
FIELD_REVIEW_UP = "ProduceExamFieldStatusType_ReviewUp"
FIELD_STAMINA_CONSUMPTION_DOWN = (
    "ProduceExamFieldStatusType_StaminaConsumptionDown"
)
FIELD_STAMINA_LESS_MULTIPLE = "ProduceExamFieldStatusType_StaminaLessMultiple"
FIELD_STAMINA_UP_MULTIPLE = "ProduceExamFieldStatusType_StaminaUpMultiple"
FIELD_STANCE_CHANGE_COUNT_UP = "ProduceExamFieldStatusType_StanceChangeCountUp"
FIELD_TURN_PROGRESS_UP = "ProduceExamFieldStatusType_TurnProgressUp"


TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN = (
    "e_trigger-exam_start_turn-condition_threshold_multiple_down-1000"
)
TRIGGER_NO_BLOCK = "e_trigger-exam_start_turn-no_block"
TRIGGER_NOT_NO_STANCE = "e_trigger-exam_start_turn-not-no_stance"
TRIGGER_NOT_PRESERVATION_UP = "e_trigger-exam_start_turn-not-preservation_up"
TRIGGER_STAMINA_UP_MULTIPLE = "e_trigger-exam_start_turn-stamina_up_multiple-500"
TRIGGER_TURN_PROGRESS_UP = "e_trigger-exam_start_turn-turn_progress_up-2"


class TriggerResolution(str, Enum):
    """How the row is classified by this independent inventory."""

    CORE_EXISTING = "core-existing"
    INDEPENDENT_RESOLVED = "independent-resolved"
    UNRESOLVED = "unresolved"


class Plan3ScalarState(Protocol):
    """Scalar fields read by the exact resolved predicates."""

    block: int
    score: int
    clear_border: int
    round_number: int
    stance: str


class Plan3NativeStateLike(Protocol):
    """Marker protocol for an immutable Plan3 native state.

    No native field is read for the resolved ``NoBlock`` predicate.  Keeping
    this as a protocol makes the resolver usable with the existing
    ``Plan3NativeState`` without importing or modifying that module.
    """


@dataclass(frozen=True, slots=True)
class StaticEvidence:
    """A local evidence locator and the narrow claim it supports."""

    source: str
    locator: str
    claim: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "locator": self.locator,
            "claim": self.claim,
        }


@dataclass(frozen=True, slots=True)
class ExamStartTurnTrigger:
    """Immutable copy of one ``produce_exam_trigger`` row.

    Tuple fields intentionally retain the source array order.  The scalar
    search/count/move fields are retained even though every one of the 95
    local start-turn rows currently has the empty/zero/Unknown values.
    """

    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_check_types: tuple[str, ...]
    field_types: tuple[str, ...]
    field_values: tuple[int, ...]
    field_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str
    resolution: TriggerResolution
    unresolved_reasons: tuple[str, ...] = ()
    evidence_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("trigger id must be a non-empty string")

        sequence_names = (
            "phase_types",
            "phase_values",
            "field_check_types",
            "field_types",
            "field_values",
            "field_card_search_ids",
            "effect_types",
            "unresolved_reasons",
            "evidence_keys",
        )
        for name in sequence_names:
            value = tuple(getattr(self, name))
            object.__setattr__(self, name, value)

        object.__setattr__(
            self,
            "resolution",
            TriggerResolution(self.resolution),
        )
        if not self.phase_types:
            raise ValueError("an ExamStartTurn row needs a phase")
        for name in ("phase_values", "field_values"):
            if any(type(item) is not int for item in getattr(self, name)):
                raise TypeError(f"{name} must contain plain integers")
        for name in ("upper_search_count", "lower_search_count"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be a plain integer")

    @property
    def phase(self) -> str:
        """The single phase used by all rows in this inventory."""

        if len(self.phase_types) != 1:
            return ",".join(self.phase_types)
        return self.phase_types[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_check_types": list(self.field_check_types),
            "field_types": list(self.field_types),
            "field_values": list(self.field_values),
            "field_card_search_ids": list(self.field_card_search_ids),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "effect_types": list(self.effect_types),
            "lesson_type": self.lesson_type,
            "resolution": self.resolution.value,
            "unresolved_reasons": list(self.unresolved_reasons),
            "evidence_keys": list(self.evidence_keys),
        }


@dataclass(frozen=True, slots=True)
class AffectedCardVersion:
    """Coverage row for one card id plus one upgrade version."""

    card_id: str
    upgrade: int
    plan_type: str
    trigger_id: str
    source_kind: str
    effect_types: tuple[str, ...]
    coverage_blockers: tuple[str, ...]
    native_dependency_reasons: tuple[str, ...] = ()
    native_dependency_blocks: bool = False
    status_enchant_id: str = ""
    sole_unlock_candidate: bool = False
    safely_resolved_by_this_module: bool = False
    current_core_executable: bool = False

    def __post_init__(self) -> None:
        if type(self.upgrade) is not int or self.upgrade < 0:
            raise ValueError("upgrade must be a non-negative plain integer")
        for name in (
            "effect_types",
            "coverage_blockers",
            "native_dependency_reasons",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def version(self) -> str:
        return f"{self.card_id}+{self.upgrade}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "plan_type": self.plan_type,
            "trigger_id": self.trigger_id,
            "source_kind": self.source_kind,
            "effect_types": list(self.effect_types),
            "coverage_blockers": list(self.coverage_blockers),
            "native_dependency_reasons": list(self.native_dependency_reasons),
            "native_dependency_blocks": self.native_dependency_blocks,
            "status_enchant_id": self.status_enchant_id,
            "sole_unlock_candidate": self.sole_unlock_candidate,
            "safely_resolved_by_this_module": self.safely_resolved_by_this_module,
            "current_core_executable": self.current_core_executable,
        }


@dataclass(frozen=True, slots=True)
class TriggerFireResult:
    """Pure result of a start-turn predicate evaluation."""

    trigger_id: str
    phase: str
    resolution: TriggerResolution
    fires: bool | None
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolution", TriggerResolution(self.resolution))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "phase": self.phase,
            "resolution": self.resolution.value,
            "fires": self.fires,
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
        }


@dataclass(frozen=True, slots=True)
class TriggerConsumeResult:
    """Pure consume result; unresolved rows always return the same states."""

    trigger_id: str
    phase: str
    resolution: TriggerResolution
    fires: bool | None
    consumed: bool | None
    state_after: Any
    native_state_after: Any | None
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolution", TriggerResolution(self.resolution))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "phase": self.phase,
            "resolution": self.resolution.value,
            "fires": self.fires,
            "consumed": self.consumed,
            "state_after": _jsonable(self.state_after),
            "native_state_after": _jsonable(self.native_state_after),
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
        }


@dataclass(frozen=True, slots=True)
class StartTurnInventory:
    """Complete local row/card inventory used by this task."""

    trigger_rows: tuple[ExamStartTurnTrigger, ...]
    affected_card_versions: tuple[AffectedCardVersion, ...]
    evidence: tuple[StaticEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_rows", tuple(self.trigger_rows))
        object.__setattr__(
            self,
            "affected_card_versions",
            tuple(self.affected_card_versions),
        )
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_card_versions)

    @property
    def affected_unique_card_count(self) -> int:
        return len({item.card_id for item in self.affected_card_versions})

    @property
    def sole_unlock_versions(self) -> tuple[AffectedCardVersion, ...]:
        return tuple(
            item
            for item in self.affected_card_versions
            if item.sole_unlock_candidate
        )

    @property
    def safe_executable_versions(self) -> tuple[AffectedCardVersion, ...]:
        return tuple(
            item
            for item in self.affected_card_versions
            if item.safely_resolved_by_this_module
        )

    @property
    def current_core_executable_versions(self) -> tuple[AffectedCardVersion, ...]:
        return tuple(
            item
            for item in self.affected_card_versions
            if item.current_core_executable
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "exam_start_turn_trigger_row_count": len(self.trigger_rows),
            "affected_card_version_count": self.affected_version_count,
            "affected_unique_card_count": self.affected_unique_card_count,
            "sole_unlock_version_count": len(self.sole_unlock_versions),
            "safe_executable_version_count": len(self.safe_executable_versions),
            "current_core_executable_version_count": len(
                self.current_core_executable_versions
            ),
            "trigger_rows": [item.to_dict() for item in self.trigger_rows],
            "affected_card_versions": [
                item.to_dict() for item in self.affected_card_versions
            ],
            "evidence": [item.to_dict() for item in self.evidence],
        }


# ---------------------------------------------------------------------------
# Static evidence and raw Master row inventory

STATIC_EVIDENCE: tuple[StaticEvidence, ...] = (
    StaticEvidence(
        "coverage",
        "var/coverage/plan3_card_executable_coverage.json:next_gaps[trigger-start-turn-shape]",
        "13 affected versions; 5 directly unlocked if this gap is fixed alone; 8 Common and 5 Plan3.",
    ),
    StaticEvidence(
        "master-sqlite",
        "var/master.sqlite3:produce_exam_trigger WHERE phase_types_json contains ExamStartTurn",
        "95 real ExamStartTurn rows; phase values, direct search, upper/lower counts, move and effect filters are empty/zero/Unknown/empty in every row.",
    ),
    StaticEvidence(
        "master-diff",
        "_research/gakumasu-diff/ProduceExamTrigger.yaml:target trigger ids",
        "Target rows retain field/check/value/search arrays exactly; no prose translation is used.",
    ),
    StaticEvidence(
        "android-il2cpp",
        "_research/android/game-v3.2.3/extracted/lib/arm64-v8a/libil2cpp.so:0x68082D4..0x6808E5C",
        "The generic field evaluator reads CurrentTurn, JudgeParameter/condition threshold, and current idol-status type for the exact target enum cases.",
    ),
    StaticEvidence(
        "android-native-comparisons",
        "IsFieldStatusTriggerStatusEffect:0x6808794,0x68089E0,0x6808BBC,0x6808834",
        "ConditionThresholdMultipleDown is inclusive float32 <=, TurnProgressUp is signed currentTurn > value, and stance fields read current status type.",
    ),
    StaticEvidence(
        "android-native-not",
        "IsEffectTriggerFieldStatusValid:0x6808278..0x68082A0",
        "The caller materializes check==Not and accepts when that bit differs from the normalized base predicate, proving logical inversion outside the field evaluator.",
    ),
    StaticEvidence(
        "native-phase",
        "docs/android-v323-plan3-static-trigger-order.md and plan3_engine.start_plan3_turn",
        "ExamStartTurn listeners observe the settled post-draw current state, after Full Power and gimmick changes; no event delta is supplied to the field evaluator.",
    ),
    StaticEvidence(
        "no-block",
        "docs/android-v323-plan3-static-trigger-order.md:concrete NoBlock observation",
        "A concrete native/static example observes NoBlock from the current block state; this supports only the NoBlock scalar predicate here.",
    ),
    StaticEvidence(
        "existing-core",
        "src/gkms_tool/plan3_engine.py:Plan3State and start-turn trigger evaluator",
        "The scalar projection carries score, clear border, current turn, block, and the current stance needed by the exact rows.",
    ),
)


def _spec(
    trigger_id: str,
    field: str = "",
    value: int | None = None,
    check: str = "",
    field_search_id: str = "",
    lesson: str = LESSON_UNKNOWN,
) -> tuple[str, str, tuple[int, ...], tuple[str, ...], tuple[str, ...], str]:
    """Build one exact compact row spec without interpreting its id."""

    return (
        trigger_id,
        field,
        () if value is None else (value,),
        () if not check else (check,),
        () if not field_search_id else (field_search_id,),
        lesson,
    )


_START_TURN_SPECS: tuple[
    tuple[str, str, tuple[int, ...], tuple[str, ...], tuple[str, ...], str], ...
] = (
    _spec("e_trigger-exam_start_turn"),
    _spec("e_trigger-exam_start_turn-block_up-14", FIELD_BLOCK_UP, 14),
    _spec("e_trigger-exam_start_turn-block_up-15", FIELD_BLOCK_UP, 15),
    _spec("e_trigger-exam_start_turn-block_up-30", FIELD_BLOCK_UP, 30),
    _spec(
        "e_trigger-exam_start_turn-block_up-30-lesson_vocal",
        FIELD_BLOCK_UP,
        30,
        lesson=LESSON_VOCAL,
    ),
    _spec("e_trigger-exam_start_turn-block_up-50", FIELD_BLOCK_UP, 50),
    _spec("e_trigger-exam_start_turn-block_up-7", FIELD_BLOCK_UP, 7),
    _spec("e_trigger-exam_start_turn-block_up-80", FIELD_BLOCK_UP, 80),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-12",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        12,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-13",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        13,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-20",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        20,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-3",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        3,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-5",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        5,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-7",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        7,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-8",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        8,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_play_aggressive_up-8-lesson_visual",
        FIELD_CARD_PLAY_AGGRESSIVE_UP,
        8,
        lesson=LESSON_VISUAL,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_search_count_up-7-p_card_search-lost-lesson_visual",
        FIELD_CARD_SEARCH_COUNT_UP,
        7,
        field_search_id="p_card_search-lost",
        lesson=LESSON_VISUAL,
    ),
    _spec(
        "e_trigger-exam_start_turn-card_search_count_up-7-p_card_search-trouble-deck_grave",
        FIELD_CARD_SEARCH_COUNT_UP,
        7,
        field_search_id="p_card_search-trouble-deck_grave",
    ),
    _spec(
        "e_trigger-exam_start_turn-concentration_change_count_up-2",
        FIELD_CONCENTRATION_CHANGE_COUNT_UP,
        2,
    ),
    _spec(
        "e_trigger-exam_start_turn-concentration_change_count_up-3",
        FIELD_CONCENTRATION_CHANGE_COUNT_UP,
        3,
    ),
    _spec(
        "e_trigger-exam_start_turn-concentration_change_count_up-3-lesson_dance",
        FIELD_CONCENTRATION_CHANGE_COUNT_UP,
        3,
        lesson=LESSON_DANCE,
    ),
    _spec(
        "e_trigger-exam_start_turn-concentration_change_count_up-4",
        FIELD_CONCENTRATION_CHANGE_COUNT_UP,
        4,
    ),
    _spec("e_trigger-exam_start_turn-concentration_up", FIELD_CONCENTRATION_UP),
    _spec(
        "e_trigger-exam_start_turn-concentration_up-2",
        FIELD_CONCENTRATION_UP,
        2,
    ),
    _spec(
        TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN,
        FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN,
        1000,
    ),
    _spec(
        "e_trigger-exam_start_turn-full_power_point_get_sum_up-13",
        FIELD_FULL_POWER_POINT_GET_SUM_UP,
        13,
    ),
    _spec(
        "e_trigger-exam_start_turn-full_power_point_get_sum_up-15",
        FIELD_FULL_POWER_POINT_GET_SUM_UP,
        15,
    ),
    _spec(
        "e_trigger-exam_start_turn-full_power_point_get_sum_up-18",
        FIELD_FULL_POWER_POINT_GET_SUM_UP,
        18,
    ),
    _spec(
        "e_trigger-exam_start_turn-full_power_point_get_sum_up-5",
        FIELD_FULL_POWER_POINT_GET_SUM_UP,
        5,
    ),
    _spec(
        "e_trigger-exam_start_turn-full_power_point_get_sum_up-8",
        FIELD_FULL_POWER_POINT_GET_SUM_UP,
        8,
    ),
    _spec(
        "e_trigger-exam_start_turn-full_power_point_up-3",
        FIELD_FULL_POWER_POINT_UP,
        3,
    ),
    _spec(
        "e_trigger-exam_start_turn-full_power_point_up-5",
        FIELD_FULL_POWER_POINT_UP,
        5,
    ),
    _spec("e_trigger-exam_start_turn-full_power_up", FIELD_FULL_POWER_UP),
    _spec(
        "e_trigger-exam_start_turn-full_power_up-lesson_dance",
        FIELD_FULL_POWER_UP,
        lesson=LESSON_DANCE,
    ),
    _spec(
        "e_trigger-exam_start_turn-lesson_buff_up-1-lesson_dance",
        FIELD_LESSON_BUFF_UP,
        1,
        lesson=LESSON_DANCE,
    ),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-10", FIELD_LESSON_BUFF_UP, 10),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-13", FIELD_LESSON_BUFF_UP, 13),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-20", FIELD_LESSON_BUFF_UP, 20),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-25", FIELD_LESSON_BUFF_UP, 25),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-3", FIELD_LESSON_BUFF_UP, 3),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-5", FIELD_LESSON_BUFF_UP, 5),
    _spec(
        "e_trigger-exam_start_turn-lesson_buff_up-5-lesson_dance",
        FIELD_LESSON_BUFF_UP,
        5,
        lesson=LESSON_DANCE,
    ),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-7", FIELD_LESSON_BUFF_UP, 7),
    _spec("e_trigger-exam_start_turn-lesson_buff_up-8", FIELD_LESSON_BUFF_UP, 8),
    _spec("e_trigger-exam_start_turn-lesson_dance", lesson=LESSON_DANCE),
    _spec("e_trigger-exam_start_turn-lesson_sp", lesson=LESSON_SP),
    _spec("e_trigger-exam_start_turn-lesson_visual", lesson=LESSON_VISUAL),
    _spec("e_trigger-exam_start_turn-lesson_vocal", lesson=LESSON_VOCAL),
    _spec(TRIGGER_NO_BLOCK, FIELD_NO_BLOCK),
    _spec(
        "e_trigger-exam_start_turn-not-concentration_up",
        FIELD_CONCENTRATION_UP,
        check=CHECK_NOT,
    ),
    _spec(
        "e_trigger-exam_start_turn-not-condition_threshold_multiple-1000",
        FIELD_CONDITION_THRESHOLD_MULTIPLE,
        1000,
        check=CHECK_NOT,
    ),
    _spec(
        "e_trigger-exam_start_turn-not-full_power_up",
        FIELD_FULL_POWER_UP,
        check=CHECK_NOT,
    ),
    _spec(TRIGGER_NOT_NO_STANCE, FIELD_NO_STANCE, check=CHECK_NOT),
    _spec(
        TRIGGER_NOT_PRESERVATION_UP,
        FIELD_PRESERVATION_UP,
        check=CHECK_NOT,
    ),
    _spec(
        "e_trigger-exam_start_turn-not-stamina_consumption_down",
        FIELD_STAMINA_CONSUMPTION_DOWN,
        check=CHECK_NOT,
    ),
    _spec("e_trigger-exam_start_turn-parameter_buff", FIELD_PARAMETER_BUFF),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-10",
        FIELD_PARAMETER_BUFF_UP,
        10,
    ),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-10-lesson_dance",
        FIELD_PARAMETER_BUFF_UP,
        10,
        lesson=LESSON_DANCE,
    ),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-10-lesson_visual",
        FIELD_PARAMETER_BUFF_UP,
        10,
        lesson=LESSON_VISUAL,
    ),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-15",
        FIELD_PARAMETER_BUFF_UP,
        15,
    ),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-3",
        FIELD_PARAMETER_BUFF_UP,
        3,
    ),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-3-lesson_dance",
        FIELD_PARAMETER_BUFF_UP,
        3,
        lesson=LESSON_DANCE,
    ),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-6",
        FIELD_PARAMETER_BUFF_UP,
        6,
    ),
    _spec(
        "e_trigger-exam_start_turn-parameter_buff_up-6-lesson_visual",
        FIELD_PARAMETER_BUFF_UP,
        6,
        lesson=LESSON_VISUAL,
    ),
    _spec(
        "e_trigger-exam_start_turn-preservation_change_count_up-2",
        FIELD_PRESERVATION_CHANGE_COUNT_UP,
        2,
    ),
    _spec(
        "e_trigger-exam_start_turn-preservation_change_count_up-3",
        FIELD_PRESERVATION_CHANGE_COUNT_UP,
        3,
    ),
    _spec(
        "e_trigger-exam_start_turn-preservation_change_count_up-4",
        FIELD_PRESERVATION_CHANGE_COUNT_UP,
        4,
    ),
    _spec(
        "e_trigger-exam_start_turn-preservation_change_count_up-4-lesson_visual",
        FIELD_PRESERVATION_CHANGE_COUNT_UP,
        4,
        lesson=LESSON_VISUAL,
    ),
    _spec("e_trigger-exam_start_turn-preservation_up", FIELD_PRESERVATION_UP),
    _spec(
        "e_trigger-exam_start_turn-preservation_up-lesson_dance",
        FIELD_PRESERVATION_UP,
        lesson=LESSON_DANCE,
    ),
    _spec("e_trigger-exam_start_turn-remaining_turn-1", FIELD_REMAINING_TURN, 1),
    _spec("e_trigger-exam_start_turn-remaining_turn-2", FIELD_REMAINING_TURN, 2),
    _spec("e_trigger-exam_start_turn-remaining_turn-3", FIELD_REMAINING_TURN, 3),
    _spec("e_trigger-exam_start_turn-remaining_turn-4", FIELD_REMAINING_TURN, 4),
    _spec("e_trigger-exam_start_turn-remaining_turn-5", FIELD_REMAINING_TURN, 5),
    _spec("e_trigger-exam_start_turn-remaining_turn-6", FIELD_REMAINING_TURN, 6),
    _spec("e_trigger-exam_start_turn-review_up-1", FIELD_REVIEW_UP, 1),
    _spec("e_trigger-exam_start_turn-review_up-10", FIELD_REVIEW_UP, 10),
    _spec("e_trigger-exam_start_turn-review_up-15", FIELD_REVIEW_UP, 15),
    _spec("e_trigger-exam_start_turn-review_up-3", FIELD_REVIEW_UP, 3),
    _spec(
        "e_trigger-exam_start_turn-review_up-3-lesson_dance",
        FIELD_REVIEW_UP,
        3,
        lesson=LESSON_DANCE,
    ),
    _spec("e_trigger-exam_start_turn-review_up-5", FIELD_REVIEW_UP, 5),
    _spec("e_trigger-exam_start_turn-review_up-6", FIELD_REVIEW_UP, 6),
    _spec(
        "e_trigger-exam_start_turn-review_up-6-lesson_dance",
        FIELD_REVIEW_UP,
        6,
        lesson=LESSON_DANCE,
    ),
    _spec(
        "e_trigger-exam_start_turn-stamina_consumption_down",
        FIELD_STAMINA_CONSUMPTION_DOWN,
    ),
    _spec(
        "e_trigger-exam_start_turn-stamina_less_multiple-500",
        FIELD_STAMINA_LESS_MULTIPLE,
        500,
    ),
    _spec(
        "e_trigger-exam_start_turn-stamina_less_multiple-500-lesson_dance",
        FIELD_STAMINA_LESS_MULTIPLE,
        500,
        lesson=LESSON_DANCE,
    ),
    _spec(
        "e_trigger-exam_start_turn-stamina_less_multiple-500-lesson_visual",
        FIELD_STAMINA_LESS_MULTIPLE,
        500,
        lesson=LESSON_VISUAL,
    ),
    _spec(
        "e_trigger-exam_start_turn-stamina_less_multiple-500-lesson_vocal",
        FIELD_STAMINA_LESS_MULTIPLE,
        500,
        lesson=LESSON_VOCAL,
    ),
    _spec(TRIGGER_STAMINA_UP_MULTIPLE, FIELD_STAMINA_UP_MULTIPLE, 500),
    _spec(
        "e_trigger-exam_start_turn-stamina_up_multiple-500-lesson_dance",
        FIELD_STAMINA_UP_MULTIPLE,
        500,
        lesson=LESSON_DANCE,
    ),
    _spec(
        "e_trigger-exam_start_turn-stamina_up_multiple-800",
        FIELD_STAMINA_UP_MULTIPLE,
        800,
    ),
    _spec(
        "e_trigger-exam_start_turn-stance_change_count_up-4-lesson_vocal",
        FIELD_STANCE_CHANGE_COUNT_UP,
        4,
        lesson=LESSON_VOCAL,
    ),
    _spec(TRIGGER_TURN_PROGRESS_UP, FIELD_TURN_PROGRESS_UP, 2),
    _spec("e_trigger-exam_start_turn-turn_progress_up-3", FIELD_TURN_PROGRESS_UP, 3),
)


def _resolution_for(
    trigger_id: str,
    field: str,
    check_types: tuple[str, ...],
    field_values: tuple[int, ...],
    field_search_ids: tuple[str, ...],
    lesson: str,
) -> tuple[TriggerResolution, tuple[str, ...], tuple[str, ...]]:
    if trigger_id == TRIGGER_NO_BLOCK:
        return (
            TriggerResolution.INDEPENDENT_RESOLVED,
            (),
            ("android-no-block", "plan3-state-block"),
        )

    if not field and not check_types and not field_values and not field_search_ids:
        return TriggerResolution.CORE_EXISTING, (), ("existing-start-turn-shape",)
    if field == FIELD_FULL_POWER_UP and not check_types and not field_values:
        return TriggerResolution.CORE_EXISTING, (), ("existing-full-power-shape",)
    if (
        field == FIELD_CONCENTRATION_UP
        and check_types == (CHECK_NOT,)
        and not field_values
    ):
        return TriggerResolution.CORE_EXISTING, (), ("existing-not-concentration-shape",)

    if trigger_id == TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN:
        return (
            TriggerResolution.INDEPENDENT_RESOLVED,
            (),
            (
                "master-trigger-row",
                "android-condition-threshold-down",
                "plan3-score-clear-border",
            ),
        )
    if trigger_id == TRIGGER_STAMINA_UP_MULTIPLE:
        return (
            TriggerResolution.UNRESOLVED,
            ("multiple-formula-and-stamina-timing-unproven",),
            ("coverage-gap", "master-trigger-row", "android-field-enum-only"),
        )
    if trigger_id == TRIGGER_TURN_PROGRESS_UP:
        return (
            TriggerResolution.INDEPENDENT_RESOLVED,
            (),
            (
                "master-trigger-row",
                "android-current-turn-signed-gt",
                "plan3-round-number",
            ),
        )
    if trigger_id in (TRIGGER_NOT_NO_STANCE, TRIGGER_NOT_PRESERVATION_UP):
        return (
            TriggerResolution.INDEPENDENT_RESOLVED,
            (),
            (
                "master-trigger-row",
                "android-current-idol-status",
                "android-outer-not",
            ),
        )
    if check_types:
        return (
            TriggerResolution.UNRESOLVED,
            ("not-semantics-or-status-timing-unproven",),
            ("master-trigger-row", "android-phase-only"),
        )
    if field_search_ids:
        return (
            TriggerResolution.UNRESOLVED,
            ("search-scope-and-count-timing-unproven",),
            ("master-trigger-row", "android-search-body-not-proven"),
        )
    if field_values:
        return (
            TriggerResolution.UNRESOLVED,
            ("field-executor-or-threshold-semantics-unproven",),
            ("master-trigger-row", "android-field-enum-only"),
        )
    return (
        TriggerResolution.UNRESOLVED,
        ("unimplemented-start-turn-field-shape",),
        ("master-trigger-row", "android-field-enum-only"),
    )


def _make_trigger(spec: tuple[str, str, tuple[int, ...], tuple[str, ...], tuple[str, ...], str]) -> ExamStartTurnTrigger:
    trigger_id, field, values, checks, field_search_ids, lesson = spec
    resolution, reasons, evidence_keys = _resolution_for(
        trigger_id,
        field,
        checks,
        values,
        field_search_ids,
        lesson,
    )
    return ExamStartTurnTrigger(
        id=trigger_id,
        phase_types=(PHASE_EXAM_START_TURN,),
        phase_values=(),
        field_check_types=checks,
        field_types=() if not field else (field,),
        field_values=values,
        field_card_search_ids=field_search_ids,
        produce_card_search_id="",
        upper_search_count=0,
        lower_search_count=0,
        card_move_position_type=MOVE_UNKNOWN,
        effect_types=(),
        lesson_type=lesson,
        resolution=resolution,
        unresolved_reasons=reasons,
        evidence_keys=evidence_keys,
    )


ALL_EXAM_START_TURN_TRIGGERS: tuple[ExamStartTurnTrigger, ...] = tuple(
    _make_trigger(spec) for spec in _START_TURN_SPECS
)
if len(ALL_EXAM_START_TURN_TRIGGERS) != 95:  # pragma: no cover - static guard
    raise RuntimeError("the local ExamStartTurn row inventory must contain 95 rows")

EXAM_START_TURN_TRIGGER_BY_ID: Mapping[str, ExamStartTurnTrigger] = MappingProxyType(
    {item.id: item for item in ALL_EXAM_START_TURN_TRIGGERS}
)


# ---------------------------------------------------------------------------
# The current 13-row coverage gap and its 5 sole-unlock candidate versions

def _affected_group(
    *,
    card_id: str,
    plan_type: str,
    upgrades: Sequence[int],
    trigger_id: str,
    source_kind: str,
    effect_types: Sequence[str],
    coverage_blockers: Sequence[str],
    native_dependency_reasons: Sequence[str] = (),
    native_dependency_blocks: bool = False,
    status_enchant_id: str = "",
) -> tuple[AffectedCardVersion, ...]:
    sole_unlock = card_id in {
        "p_card-03-ido-100_047",
        "p_card-03-men-3_062",
    }
    remaining_blockers = tuple(
        blocker
        for blocker in coverage_blockers
        if not (
            blocker.startswith("trigger-start-turn-shape:")
            or blocker.startswith(
                "trigger-field:ProduceExamFieldStatusType_"
                "ConditionThresholdMultipleDown:"
            )
            or blocker.startswith(
                "trigger-field:ProduceExamFieldStatusType_TurnProgressUp:"
            )
        )
    )
    safe = (
        trigger_id
        in {
            TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN,
            TRIGGER_TURN_PROGRESS_UP,
            TRIGGER_NOT_NO_STANCE,
            TRIGGER_NOT_PRESERVATION_UP,
        }
        and not remaining_blockers
    )
    return tuple(
        AffectedCardVersion(
            card_id=card_id,
            upgrade=upgrade,
            plan_type=plan_type,
            trigger_id=trigger_id,
            source_kind=source_kind,
            effect_types=tuple(effect_types),
            coverage_blockers=tuple(coverage_blockers),
            native_dependency_reasons=tuple(native_dependency_reasons),
            native_dependency_blocks=native_dependency_blocks,
            status_enchant_id=status_enchant_id,
            sole_unlock_candidate=sole_unlock,
            safely_resolved_by_this_module=safe,
            current_core_executable=safe,
        )
        for upgrade in upgrades
    )


_AFFECTED_CARD_VERSIONS: tuple[AffectedCardVersion, ...] = (
    *_affected_group(
        card_id="p_card-00-sup-2_025",
        plan_type="ProducePlanType_Common",
        upgrades=(0, 1, 2, 3),
        trigger_id=TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN,
        source_kind="card-play-trigger",
        effect_types=(
            "ProduceExamEffectType_ExamBlock",
            "ProduceExamEffectType_ExamCardDraw",
            "ProduceExamEffectType_ExamEffectTimer",
            "ProduceExamEffectType_ExamLesson",
        ),
        coverage_blockers=(
            "trigger-field:ProduceExamFieldStatusType_ConditionThresholdMultipleDown:e_trigger-exam_start_turn-condition_threshold_multiple_down-1000",
            "trigger-start-turn-shape:e_trigger-exam_start_turn-condition_threshold_multiple_down-1000",
        ),
        native_dependency_reasons=("exam-card-draw-zone-rng",),
    ),
    *_affected_group(
        card_id="p_card-00-sup-2_028",
        plan_type="ProducePlanType_Common",
        upgrades=(0, 1, 2, 3),
        trigger_id=TRIGGER_TURN_PROGRESS_UP,
        source_kind="card-play-trigger",
        effect_types=(
            "ProduceExamEffectType_ExamCardUpgrade",
            "ProduceExamEffectType_ExamEffectTimer",
            "ProduceExamEffectType_ExamLesson",
            "ProduceExamEffectType_ExamStaminaConsumptionDown",
        ),
        coverage_blockers=(
            "trigger-field:ProduceExamFieldStatusType_TurnProgressUp:e_trigger-exam_start_turn-turn_progress_up-2",
            "trigger-start-turn-shape:e_trigger-exam_start_turn-turn_progress_up-2",
        ),
    ),
    *_affected_group(
        card_id="p_card-03-ido-100_047",
        plan_type="ProducePlanType_Plan3",
        upgrades=(0,),
        trigger_id=TRIGGER_NOT_PRESERVATION_UP,
        source_kind="status-enchant-trigger",
        effect_types=(
            "ProduceExamEffectType_ExamBlock",
            "ProduceExamEffectType_ExamEnthusiasticMultiple",
            "ProduceExamEffectType_ExamOverPreservation",
            "ProduceExamEffectType_ExamPlayableValueAdd",
            "ProduceExamEffectType_ExamStatusEnchant",
        ),
        coverage_blockers=(
            "trigger-start-turn-shape:e_trigger-exam_start_turn-not-preservation_up",
        ),
        status_enchant_id="enchant-p_card-03-ido-100_047-enc01",
    ),
    *_affected_group(
        card_id="p_card-03-men-3_062",
        plan_type="ProducePlanType_Plan3",
        upgrades=(0, 1, 2, 3),
        trigger_id=TRIGGER_NOT_NO_STANCE,
        source_kind="status-enchant-trigger",
        effect_types=(
            "ProduceExamEffectType_ExamAddGrowEffect",
            "ProduceExamEffectType_ExamStatusEnchant",
        ),
        coverage_blockers=(
            "trigger-start-turn-shape:e_trigger-exam_start_turn-not-no_stance",
        ),
        native_dependency_reasons=("exam-add-grow-guid-mutation",),
        native_dependency_blocks=False,
        status_enchant_id="enchant-p_card-03-men-3_062-enc01",
    ),
)

if len(_AFFECTED_CARD_VERSIONS) != 13:  # pragma: no cover - static guard
    raise RuntimeError("the affected card inventory must contain 13 versions")


START_TURN_INVENTORY = StartTurnInventory(
    trigger_rows=ALL_EXAM_START_TURN_TRIGGERS,
    affected_card_versions=_AFFECTED_CARD_VERSIONS,
    evidence=STATIC_EVIDENCE,
)

SOLE_UNLOCK_CARD_VERSIONS: tuple[AffectedCardVersion, ...] = tuple(
    START_TURN_INVENTORY.sole_unlock_versions
)
SAFE_EXECUTABLE_CARD_VERSIONS: tuple[AffectedCardVersion, ...] = tuple(
    START_TURN_INVENTORY.safe_executable_versions
)


# ---------------------------------------------------------------------------
# Pure resolver/runtime surface


def resolve_trigger(trigger_id: str) -> ExamStartTurnTrigger | None:
    """Return one immutable row, or ``None`` for an unknown id."""

    return EXAM_START_TURN_TRIGGER_BY_ID.get(trigger_id)


def matches_trigger_contract(
    expected: ExamStartTurnTrigger,
    actual: Any,
) -> bool:
    """Match every stored Master column for one exact catalog row."""

    if not isinstance(expected, ExamStartTurnTrigger):
        return False
    if getattr(actual, "id", None) != expected.id:
        return False
    for name in (
        "phase_types",
        "phase_values",
        "field_check_types",
        "field_types",
        "field_values",
        "field_card_search_ids",
        "effect_types",
    ):
        try:
            value = tuple(getattr(actual, name))
        except (AttributeError, TypeError):
            return False
        if value != getattr(expected, name):
            return False
    for name in (
        "produce_card_search_id",
        "upper_search_count",
        "lower_search_count",
        "card_move_position_type",
        "lesson_type",
    ):
        if getattr(actual, name, object()) != getattr(expected, name):
            return False
    return True


def _unresolved_result(
    trigger: ExamStartTurnTrigger,
    *,
    phase: str,
    reasons: Sequence[str],
) -> TriggerFireResult:
    return TriggerFireResult(
        trigger_id=trigger.id,
        phase=phase,
        resolution=TriggerResolution.UNRESOLVED,
        fires=None,
        reasons=tuple(reasons),
        state_unchanged=True,
    )


def fires(
    trigger: ExamStartTurnTrigger,
    state: Plan3ScalarState,
    *,
    event_phase: str = PHASE_EXAM_START_TURN,
    native_state: Plan3NativeStateLike | None = None,
) -> TriggerFireResult:
    """Evaluate a trigger without mutating scalar or native state.

    Only catalog rows marked ``INDEPENDENT_RESOLVED`` are executable.  Every
    predicate reads current state at the settled ``ExamStartTurn`` boundary;
    a mismatched event phase returns false rather than treating StartTurn as a
    card-trigger wildcard.
    """

    del native_state  # The proven predicates need no GUID-native mutation.
    if not isinstance(trigger, ExamStartTurnTrigger):
        return TriggerFireResult(
            trigger_id=str(getattr(trigger, "id", "<unknown>")),
            phase=event_phase,
            resolution=TriggerResolution.UNRESOLVED,
            fires=None,
            reasons=("trigger-record-type-unproven",),
        )

    if trigger.resolution is not TriggerResolution.INDEPENDENT_RESOLVED:
        return _unresolved_result(
            trigger,
            phase=event_phase,
            reasons=trigger.unresolved_reasons or ("shape-unresolved",),
        )
    if trigger.phase_types != (PHASE_EXAM_START_TURN,):
        return _unresolved_result(
            trigger,
            phase=event_phase,
            reasons=("phase-shape-unproven",),
        )
    if event_phase != PHASE_EXAM_START_TURN:
        return TriggerFireResult(
            trigger_id=trigger.id,
            phase=event_phase,
            resolution=TriggerResolution.INDEPENDENT_RESOLVED,
            fires=False,
            reasons=(),
            state_unchanged=True,
        )

    if trigger.id == TRIGGER_NO_BLOCK:
        try:
            block = getattr(state, "block")
        except (AttributeError, TypeError):
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-block-missing",),
            )
        if type(block) is not int or block < 0:
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-block-invalid",),
            )
        predicate = block == 0
    elif trigger.id == TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN:
        try:
            score = getattr(state, "score")
            clear_border = getattr(state, "clear_border")
        except (AttributeError, TypeError):
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-condition-threshold-missing",),
            )
        if (
            type(score) is not int
            or type(clear_border) is not int
            or score < 0
            or clear_border <= 0
            or score > 2**31 - 1
            or clear_border > 2**31 - 1
        ):
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-condition-threshold-invalid",),
            )
        threshold = trigger.field_values[0]
        try:
            threshold_ratio = _float32(
                _float32(threshold) / _float32(1000.0)
            )
            progress_ratio = _float32(
                _float32(score) / _float32(clear_border)
            )
        except (OverflowError, struct.error, ZeroDivisionError):
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("condition-threshold-float32-conversion-unproven",),
            )
        # Native compares threshold to progress and returns signed GE, which
        # is equivalent to progress <= threshold for finite positive inputs.
        predicate = threshold_ratio >= progress_ratio
    elif trigger.id == TRIGGER_TURN_PROGRESS_UP:
        try:
            current_turn = getattr(state, "round_number")
        except (AttributeError, TypeError):
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-current-turn-missing",),
            )
        if (
            type(current_turn) is not int
            or current_turn < 1
            or current_turn > 2**31 - 1
        ):
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-current-turn-invalid",),
            )
        # Android 0x68089E8 CMP W0,W19 -> 0x6808B68 CSET GT.
        predicate = current_turn > trigger.field_values[0]
    elif trigger.id in {
        TRIGGER_NOT_NO_STANCE,
        TRIGGER_NOT_PRESERVATION_UP,
    }:
        try:
            stance = getattr(state, "stance")
        except (AttributeError, TypeError):
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-stance-missing",),
            )
        if stance not in {
            "neutral",
            "concentration",
            "preservation",
            "full_power",
        }:
            return _unresolved_result(
                trigger,
                phase=event_phase,
                reasons=("plan3-state-stance-invalid",),
            )
        if trigger.id == TRIGGER_NOT_NO_STANCE:
            base_predicate = stance == "neutral"
        else:
            # Scalar Preservation level 3 is the projection of the native
            # type-4 OverPreservation branch, which the base field also accepts.
            base_predicate = stance == "preservation"
        predicate = not base_predicate
    else:
        return _unresolved_result(
            trigger,
            phase=event_phase,
            reasons=("independent-resolver-shape-mismatch",),
        )
    return TriggerFireResult(
        trigger_id=trigger.id,
        phase=event_phase,
        resolution=TriggerResolution.INDEPENDENT_RESOLVED,
        fires=predicate,
        reasons=(),
        state_unchanged=True,
    )


def _float32(value: int | float) -> float:
    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def consume(
    trigger: ExamStartTurnTrigger,
    state: Any,
    *,
    event_phase: str = PHASE_EXAM_START_TURN,
    native_state: Any | None = None,
) -> TriggerConsumeResult:
    """Return a pure consumption result for a start-turn trigger.

    Card-gate transactions and active-enchant use counters are both owned by
    the central engine.  This independent predicate resolver therefore keeps
    both supplied states unchanged and returns ``consumed=False``.
    """

    decision = fires(
        trigger,
        state,
        event_phase=event_phase,
        native_state=native_state,
    )
    if decision.resolution is TriggerResolution.UNRESOLVED:
        return TriggerConsumeResult(
            trigger_id=decision.trigger_id,
            phase=decision.phase,
            resolution=decision.resolution,
            fires=None,
            consumed=None,
            state_after=state,
            native_state_after=native_state,
            reasons=decision.reasons,
            state_unchanged=True,
        )
    return TriggerConsumeResult(
        trigger_id=decision.trigger_id,
        phase=decision.phase,
        resolution=decision.resolution,
        fires=decision.fires,
        consumed=False,
        state_after=state,
        native_state_after=native_state,
        reasons=("central-engine-owns-trigger-consumption",),
        state_unchanged=True,
    )


def _jsonable(value: Any) -> Any:
    """Convert result payloads, including existing frozen states, to JSON data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [_jsonable(item) for item in value]
    return repr(value)


__all__ = [
    "AffectedCardVersion",
    "ALL_EXAM_START_TURN_TRIGGERS",
    "CHECK_NOT",
    "EXAM_START_TURN_TRIGGER_BY_ID",
    "ExamStartTurnTrigger",
    "FIELD_CONDITION_THRESHOLD_MULTIPLE_DOWN",
    "FIELD_NO_BLOCK",
    "FIELD_NO_STANCE",
    "FIELD_PRESERVATION_UP",
    "FIELD_STAMINA_UP_MULTIPLE",
    "FIELD_TURN_PROGRESS_UP",
    "PHASE_EXAM_START_TURN",
    "SAFE_EXECUTABLE_CARD_VERSIONS",
    "SOLE_UNLOCK_CARD_VERSIONS",
    "START_TURN_INVENTORY",
    "StartTurnInventory",
    "StaticEvidence",
    "TriggerConsumeResult",
    "TriggerFireResult",
    "TriggerResolution",
    "TRIGGER_CONDITION_THRESHOLD_MULTIPLE_DOWN",
    "TRIGGER_NO_BLOCK",
    "TRIGGER_NOT_NO_STANCE",
    "TRIGGER_NOT_PRESERVATION_UP",
    "TRIGGER_STAMINA_UP_MULTIPLE",
    "TRIGGER_TURN_PROGRESS_UP",
    "consume",
    "fires",
    "matches_trigger_contract",
    "resolve_trigger",
]
