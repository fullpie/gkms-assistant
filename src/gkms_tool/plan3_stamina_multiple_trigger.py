"""Android-v3.2.3 evidence model for Plan3 stamina-multiple triggers.

This module is intentionally separate from the engine and from the existing
StartTurn/CardPlay resolvers.  It owns only the two native field-status cases
whose bodies are present in the local Android native dump:

``StaminaUpMultiple``
    ``float32(stamina) / float32(max_stamina) >=
    float32(value) / float32(1000)``

``StaminaLessMultiple``
    ``float32(stamina) / float32(max_stamina) <=
    float32(value) / float32(1000)``

The native method does not guard a zero maximum, clamp an invalid state, or
round to an integer.  The pure evaluator therefore refuses invalid projected
game states instead of inventing a native fallback.  Master rows with a
lesson gate or a card-search/effect-group wrapper retain their exact row shape
and exact formula, but their complete trigger decision is ``None`` until the
missing wrapper/order evidence is supplied elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import struct
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Exact local Master tokens

PHASE_EXAM_START_TURN = "ProduceExamPhaseType_ExamStartTurn"
PHASE_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"

LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"

FIELD_STAMINA_UP_MULTIPLE = "ProduceExamFieldStatusType_StaminaUpMultiple"
FIELD_STAMINA_LESS_MULTIPLE = "ProduceExamFieldStatusType_StaminaLessMultiple"


class StaminaField(str, Enum):
    """The two field-status enum values decoded from the native switch."""

    STAMINA_UP_MULTIPLE = FIELD_STAMINA_UP_MULTIPLE
    STAMINA_LESS_MULTIPLE = FIELD_STAMINA_LESS_MULTIPLE


class Comparison(str, Enum):
    """The ordered comparison reached by each native switch case."""

    GREATER_EQUAL = ">="
    LESS_EQUAL = "<="


class TriggerResolution(str, Enum):
    """Whether the complete trigger wrapper is safe to evaluate."""

    PREDICATE_RESOLVED = "predicate-resolved"
    UNRESOLVED = "unresolved"


class EvaluationTiming(str, Enum):
    """The event boundary at which the native context is read."""

    START_TURN_CURRENT_CONTEXT = (
        "ExamStartTurn-current-ExamParameter-at-predicate-call"
    )
    CARD_PLAY_PRE_PAYMENT_CONTEXT = (
        "ExamCardPlay-pre-payment-pre-direct-effect-candidate-context"
    )


class ArithmeticSemantics(str, Enum):
    """No integer percentage conversion was observed in the native body."""

    FLOAT32_DIVISION_NO_INTEGER_ROUNDING = (
        "float32-scvtf-and-fdiv; no-integer-rounding"
    )


class StaminaBasis(str, Enum):
    CURRENT_OVER_MAX = "current_stamina_over_max_stamina"


@dataclass(frozen=True, slots=True)
class StaticEvidence:
    """A local file locator and the narrow claim it supports."""

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
class NativeBranchEvidence:
    """Instruction-level facts for one field enum case."""

    field_type: StaminaField
    switch_value: int
    branch_va: str
    current_getter_va: str
    current_getter_method: str
    max_getter_va: str
    max_getter_method: str
    denominator_bits: str
    denominator: float
    comparison: Comparison
    compare_va: str
    return_condition_va: str
    ordered_inputs_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_type", StaminaField(self.field_type))
        object.__setattr__(self, "comparison", Comparison(self.comparison))

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_type": self.field_type.value,
            "switch_value": self.switch_value,
            "branch_va": self.branch_va,
            "current_getter_va": self.current_getter_va,
            "current_getter_method": self.current_getter_method,
            "max_getter_va": self.max_getter_va,
            "max_getter_method": self.max_getter_method,
            "denominator_bits": self.denominator_bits,
            "denominator": self.denominator,
            "comparison": self.comparison.value,
            "compare_va": self.compare_va,
            "return_condition_va": self.return_condition_va,
            "ordered_inputs_required": self.ordered_inputs_required,
        }


NATIVE_BRANCHES: Mapping[StaminaField, NativeBranchEvidence] = {
    StaminaField.STAMINA_UP_MULTIPLE: NativeBranchEvidence(
        field_type=StaminaField.STAMINA_UP_MULTIPLE,
        switch_value=4,
        branch_va="0x6808C64",
        current_getter_va="0x7EB7ABC",
        current_getter_method="Campus.InGame.Exam.ExamParameterModel$$get_Stamina",
        max_getter_va="0x7EB7BB0",
        max_getter_method=(
            "Campus.InGame.Exam.ExamParameterModel$$get_MaxStamina"
        ),
        denominator_bits="0x447A0000",
        denominator=1000.0,
        comparison=Comparison.GREATER_EQUAL,
        compare_va="0x6808C9C (FCMP S0,S1)",
        return_condition_va="0x6808748 (CSET W0,GE)",
    ),
    StaminaField.STAMINA_LESS_MULTIPLE: NativeBranchEvidence(
        field_type=StaminaField.STAMINA_LESS_MULTIPLE,
        switch_value=5,
        branch_va="0x6808A6C",
        current_getter_va="0x7EB7ABC",
        current_getter_method="Campus.InGame.Exam.ExamParameterModel$$get_Stamina",
        max_getter_va="0x7EB7BB0",
        max_getter_method=(
            "Campus.InGame.Exam.ExamParameterModel$$get_MaxStamina"
        ),
        denominator_bits="0x447A0000",
        denominator=1000.0,
        comparison=Comparison.LESS_EQUAL,
        compare_va="0x6808AA4 (FCMP S0,S1)",
        return_condition_va="0x6808AA8 (CSET W0,LS; ordered inputs == <=)",
    ),
}


NATIVE_FORMULA_EVIDENCE: Mapping[str, Any] = {
    "platform": "Android",
    "game_version": "3.2.3",
    "method": "Campus.InGame.ExamExtensions.IsFieldStatusTriggerStatusEffect",
    "method_va": "0x68082D4",
    "method_signature": (
        "(ProduceExamFieldStatusType fieldStatusType, int value, "
        "IProduceCardSearch cardSearch, ExamEffectCalculateContext context)"
    ),
    "context_exam_parameter_field": "ExamEffectCalculateContext +0x10",
    "context_reads": (
        "ExamParameterModel.get_Stamina()",
        "ExamParameterModel.get_MaxStamina()",
    ),
    "operations": (
        "SCVTF int32->float32",
        "FDIV current/max",
        "SCVTF int32->float32",
        "FDIV value/1000.0f",
        "FCMP",
    ),
    "denominator_bits": "0x447A0000",
    "denominator": 1000.0,
    "integer_rounding": "none",
    "branches": {
        field.value: branch.to_dict() for field, branch in NATIVE_BRANCHES.items()
    },
}


STATIC_EVIDENCE: tuple[StaticEvidence, ...] = (
    StaticEvidence(
        "android-native",
        "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt:14735",
        "The method signature and body contain the StaminaUpMultiple (enum 4) and StaminaLessMultiple (enum 5) switch cases.",
    ),
    StaticEvidence(
        "android-native",
        "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt:StaminaUpMultiple branch 0x6808C64",
        "Getter calls, SCVTF/FDIV/FCMP, and the shared CSET GE return prove current/max >= value/1000.0f.",
    ),
    StaticEvidence(
        "android-native",
        "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt:StaminaLessMultiple branch 0x6808A6C",
        "Getter calls, SCVTF/FDIV/FCMP, and CSET LS prove current/max <= value/1000.0f for ordered finite inputs.",
    ),
    StaticEvidence(
        "android-metadata",
        "_research/android/game-v3.2.3/cpp2il-isil/script.json:0x7EB7ABC,0x7EB7BB0",
        "The two indirect calls resolve to get_Stamina and get_MaxStamina.",
    ),
    StaticEvidence(
        "android-metadata",
        "_research/android/game-v3.2.3/il2cppdumper/dump.cs:ExamEffectCalculateContext",
        "ExamParameter is the context field at +0x10 used by the native body.",
    ),
    StaticEvidence(
        "master-sqlite",
        "var/master.sqlite3:produce_exam_trigger scoped to exact ExamStartTurn/ExamCardPlay phases",
        "The catalog contains all 7 StartTurn and all 8 CardPlay rows whose field type is a stamina multiple.",
    ),
    StaticEvidence(
        "event-order",
        "docs/android-v323-plan3-status-enchant-executor.md:Event ordering",
        "StartTurn and CardPlay context boundaries are documented; CardPlay candidate capture is before payment/direct effects.",
    ),
    StaticEvidence(
        "event-order",
        "docs/android-v323-card-transaction-order-report.md:ExamCardPlay candidate and trigger validation",
        "Search-bearing wrappers require a card-search/effect-group order proof not owned by this module.",
    ),
    StaticEvidence(
        "pc-compatibility",
        "docs/android-pc-exam-metadata-compatibility.md:Result",
        "PC ordered metadata shape matches Android for critical exam surfaces, but this is not native instruction parity.",
    ),
    StaticEvidence(
        "pc-native-gap",
        "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/native-method-table-scan.json",
        "PC native scan is authoritative=false with no candidates, so no PC body-level formula claim is made.",
    ),
)


@dataclass(frozen=True, slots=True)
class StaminaMultipleTrigger:
    """One exact Master row plus its native formula classification."""

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
    comparison: Comparison
    denominator: int = 1000
    basis: StaminaBasis = StaminaBasis.CURRENT_OVER_MAX
    arithmetic: ArithmeticSemantics = (
        ArithmeticSemantics.FLOAT32_DIVISION_NO_INTEGER_ROUNDING
    )
    evaluation_timing: EvaluationTiming = (
        EvaluationTiming.START_TURN_CURRENT_CONTEXT
    )
    resolution: TriggerResolution = TriggerResolution.UNRESOLVED
    unresolved_reasons: tuple[str, ...] = ()
    evidence_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("trigger id must be a non-empty string")
        for name in (
            "phase_types",
            "phase_values",
            "field_check_types",
            "field_types",
            "field_values",
            "field_card_search_ids",
            "effect_types",
            "unresolved_reasons",
            "evidence_keys",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "comparison", Comparison(self.comparison))
        object.__setattr__(self, "basis", StaminaBasis(self.basis))
        object.__setattr__(self, "arithmetic", ArithmeticSemantics(self.arithmetic))
        object.__setattr__(
            self, "evaluation_timing", EvaluationTiming(self.evaluation_timing)
        )
        object.__setattr__(self, "resolution", TriggerResolution(self.resolution))

        if self.phase_types not in (
            (PHASE_EXAM_START_TURN,),
            (PHASE_CARD_PLAY,),
        ):
            raise ValueError("catalog rows must be one exact supported phase")
        if len(self.phase_types) != 1:
            raise ValueError("a row must have one phase")
        if self.field_types not in (
            (FIELD_STAMINA_UP_MULTIPLE,),
            (FIELD_STAMINA_LESS_MULTIPLE,),
        ):
            raise ValueError("row must have exactly one stamina multiple field")
        if len(self.field_values) != 1:
            raise ValueError("row must have one threshold value")
        if type(self.field_values[0]) is not int:
            raise TypeError("threshold must be a plain integer")
        if self.denominator != 1000:
            raise ValueError("native denominator is fixed at 1000")
        if self.phase == PHASE_EXAM_START_TURN:
            expected_timing = EvaluationTiming.START_TURN_CURRENT_CONTEXT
        else:
            expected_timing = EvaluationTiming.CARD_PLAY_PRE_PAYMENT_CONTEXT
        if self.evaluation_timing is not expected_timing:
            raise ValueError("phase/timing mismatch")
        if self.resolution is TriggerResolution.PREDICATE_RESOLVED and (
            self.lesson_type != LESSON_UNKNOWN or self.produce_card_search_id
        ):
            raise ValueError("wrapped rows cannot be marked predicate-resolved")
        if self.resolution is TriggerResolution.UNRESOLVED and not self.unresolved_reasons:
            raise ValueError("unresolved rows need an explicit reason")
        if self.resolution is TriggerResolution.PREDICATE_RESOLVED and self.unresolved_reasons:
            raise ValueError("resolved rows cannot carry unresolved reasons")

    @property
    def phase(self) -> str:
        return self.phase_types[0]

    @property
    def field(self) -> StaminaField:
        return StaminaField(self.field_types[0])

    @property
    def threshold(self) -> int:
        return self.field_values[0]

    @property
    def predicate_executable(self) -> bool:
        return self.resolution is TriggerResolution.PREDICATE_RESOLVED

    def master_contract(self) -> dict[str, Any]:
        """Return exactly the trigger columns checked against local Master."""

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
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.master_contract()
        result.update(
            {
                "comparison": self.comparison.value,
                "denominator": self.denominator,
                "basis": self.basis.value,
                "arithmetic": self.arithmetic.value,
                "evaluation_timing": self.evaluation_timing.value,
                "resolution": self.resolution.value,
                "predicate_executable": self.predicate_executable,
                "unresolved_reasons": list(self.unresolved_reasons),
                "evidence_keys": list(self.evidence_keys),
            }
        )
        return result


@dataclass(frozen=True, slots=True)
class AdjacentMultipleTrigger:
    """Exact non-stamina ``*Multiple`` row kept unresolved by design.

    These rows share the naming/native dispatch neighborhood but do not prove
    that their source parameter is stamina.  Keeping them in a separate typed
    inventory prevents a card-name or enum-name translation from becoming a
    guessed formula.
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
    unresolved_reason: str = "non-stamina-multiple-semantics-unproven"

    def __post_init__(self) -> None:
        for name in (
            "phase_types",
            "phase_values",
            "field_check_types",
            "field_types",
            "field_values",
            "field_card_search_ids",
            "effect_types",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if len(self.phase_types) != 1 or len(self.field_types) != 1:
            raise ValueError("adjacent row must have one phase and one field")
        if type(self.field_values[0]) is not int:
            raise TypeError("adjacent threshold must be a plain integer")

    def master_contract(self) -> dict[str, Any]:
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
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.master_contract()
        result["resolution"] = TriggerResolution.UNRESOLVED.value
        result["unresolved_reason"] = self.unresolved_reason
        return result


def _adjacent_row(
    trigger_id: str,
    phase: str,
    field_type: str,
    value: int,
    *,
    field_check_types: tuple[str, ...] = (),
    produce_card_search_id: str = "",
    lower_search_count: int = 0,
) -> AdjacentMultipleTrigger:
    return AdjacentMultipleTrigger(
        id=trigger_id,
        phase_types=(phase,),
        phase_values=(),
        field_check_types=field_check_types,
        field_types=(field_type,),
        field_values=(value,),
        field_card_search_ids=(),
        produce_card_search_id=produce_card_search_id,
        upper_search_count=0,
        lower_search_count=lower_search_count,
        card_move_position_type=MOVE_UNKNOWN,
        effect_types=(),
        lesson_type=LESSON_UNKNOWN,
    )


ADJACENT_UNRESOLVED_MULTIPLE_TRIGGERS: tuple[
    AdjacentMultipleTrigger, ...
] = (
    _adjacent_row(
        "e_trigger-exam_start_turn-condition_threshold_multiple_down-1000",
        PHASE_EXAM_START_TURN,
        "ProduceExamFieldStatusType_ConditionThresholdMultipleDown",
        1000,
    ),
    _adjacent_row(
        "e_trigger-exam_start_turn-not-condition_threshold_multiple-1000",
        PHASE_EXAM_START_TURN,
        "ProduceExamFieldStatusType_ConditionThresholdMultiple",
        1000,
        field_check_types=("ProduceExamTriggerCheckType_Not",),
    ),
    _adjacent_row(
        "e_trigger-exam_card_play-condition_threshold_multiple_down-250",
        PHASE_CARD_PLAY,
        "ProduceExamFieldStatusType_ConditionThresholdMultipleDown",
        250,
    ),
    _adjacent_row(
        "e_trigger-exam_card_play-condition_threshold_multiple_down-500",
        PHASE_CARD_PLAY,
        "ProduceExamFieldStatusType_ConditionThresholdMultipleDown",
        500,
    ),
    _adjacent_row(
        "e_trigger-exam_card_play-condition_threshold_multiple-500",
        PHASE_CARD_PLAY,
        "ProduceExamFieldStatusType_ConditionThresholdMultiple",
        500,
    ),
    _adjacent_row(
        "e_trigger-exam_card_play-parameter_buff_multiple_per_turn_up-1-p_card_search-playing-idol-unique-0_1",
        PHASE_CARD_PLAY,
        "ProduceExamFieldStatusType_ParameterBuffMultiplePerTurnUp",
        1,
        produce_card_search_id="p_card_search-playing-idol-unique",
        lower_search_count=1,
    ),
)


ADJACENT_UNRESOLVED_MULTIPLE_TRIGGER_BY_ID: Mapping[
    str, AdjacentMultipleTrigger
] = {row.id: row for row in ADJACENT_UNRESOLVED_MULTIPLE_TRIGGERS}


def _row(
    trigger_id: str,
    phase: str,
    field: StaminaField,
    threshold: int,
    *,
    lesson_type: str = LESSON_UNKNOWN,
    produce_card_search_id: str = "",
    lower_search_count: int = 0,
) -> StaminaMultipleTrigger:
    if field is StaminaField.STAMINA_UP_MULTIPLE:
        comparison = Comparison.GREATER_EQUAL
    else:
        comparison = Comparison.LESS_EQUAL

    reasons: tuple[str, ...]
    evidence_keys = (
        "android-native-formula",
        "master-trigger-row",
    )
    if lesson_type != LESSON_UNKNOWN:
        reasons = ("lesson-gate-and-trigger-order-unproven",)
        evidence_keys += ("lesson-wrapper-not-owned",)
    elif produce_card_search_id:
        reasons = ("card-search-effect-group-and-trigger-order-unproven",)
        evidence_keys += ("card-search-wrapper-not-owned",)
    else:
        reasons = ()

    resolution = (
        TriggerResolution.PREDICATE_RESOLVED
        if not reasons
        else TriggerResolution.UNRESOLVED
    )
    timing = (
        EvaluationTiming.START_TURN_CURRENT_CONTEXT
        if phase == PHASE_EXAM_START_TURN
        else EvaluationTiming.CARD_PLAY_PRE_PAYMENT_CONTEXT
    )
    return StaminaMultipleTrigger(
        id=trigger_id,
        phase_types=(phase,),
        phase_values=(),
        field_check_types=(),
        field_types=(field.value,),
        field_values=(threshold,),
        field_card_search_ids=(),
        produce_card_search_id=produce_card_search_id,
        upper_search_count=0,
        lower_search_count=lower_search_count,
        card_move_position_type=MOVE_UNKNOWN,
        effect_types=(),
        lesson_type=lesson_type,
        comparison=comparison,
        evaluation_timing=timing,
        resolution=resolution,
        unresolved_reasons=reasons,
        evidence_keys=evidence_keys,
    )


# Every row below is copied from the local Master row contract.  Do not infer
# rows from the id spelling: the test compares every stored column to SQLite.
ALL_STAMINA_MULTIPLE_TRIGGERS: tuple[StaminaMultipleTrigger, ...] = (
    _row(
        "e_trigger-exam_start_turn-stamina_less_multiple-500",
        PHASE_EXAM_START_TURN,
        StaminaField.STAMINA_LESS_MULTIPLE,
        500,
    ),
    _row(
        "e_trigger-exam_start_turn-stamina_less_multiple-500-lesson_dance",
        PHASE_EXAM_START_TURN,
        StaminaField.STAMINA_LESS_MULTIPLE,
        500,
        lesson_type="ProduceStepLessonType_LessonDance",
    ),
    _row(
        "e_trigger-exam_start_turn-stamina_less_multiple-500-lesson_visual",
        PHASE_EXAM_START_TURN,
        StaminaField.STAMINA_LESS_MULTIPLE,
        500,
        lesson_type="ProduceStepLessonType_LessonVisual",
    ),
    _row(
        "e_trigger-exam_start_turn-stamina_less_multiple-500-lesson_vocal",
        PHASE_EXAM_START_TURN,
        StaminaField.STAMINA_LESS_MULTIPLE,
        500,
        lesson_type="ProduceStepLessonType_LessonVocal",
    ),
    _row(
        "e_trigger-exam_start_turn-stamina_up_multiple-500",
        PHASE_EXAM_START_TURN,
        StaminaField.STAMINA_UP_MULTIPLE,
        500,
    ),
    _row(
        "e_trigger-exam_start_turn-stamina_up_multiple-500-lesson_dance",
        PHASE_EXAM_START_TURN,
        StaminaField.STAMINA_UP_MULTIPLE,
        500,
        lesson_type="ProduceStepLessonType_LessonDance",
    ),
    _row(
        "e_trigger-exam_start_turn-stamina_up_multiple-800",
        PHASE_EXAM_START_TURN,
        StaminaField.STAMINA_UP_MULTIPLE,
        800,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_less_multiple-300-p_card_search-active_skill-playing-0_1",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_LESS_MULTIPLE,
        300,
        produce_card_search_id="p_card_search-active_skill-playing",
        lower_search_count=1,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_less_multiple-500",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_LESS_MULTIPLE,
        500,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_less_multiple-500-p_card_search-active_skill-playing-0_1",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_LESS_MULTIPLE,
        500,
        produce_card_search_id="p_card_search-active_skill-playing",
        lower_search_count=1,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_less_multiple-500-p_card_search-playing-effect_group-visible-exam_concentration-000-0_1",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_LESS_MULTIPLE,
        500,
        produce_card_search_id=(
            "p_card_search-playing-effect_group-visible-exam_concentration-000"
        ),
        lower_search_count=1,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_up_multiple-500",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_UP_MULTIPLE,
        500,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_up_multiple-500-p_card_search-active_skill-playing-0_1",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_UP_MULTIPLE,
        500,
        produce_card_search_id="p_card_search-active_skill-playing",
        lower_search_count=1,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_up_multiple-500-p_card_search-playing-1-p_card-02-men-1_031-0_1",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_UP_MULTIPLE,
        500,
        produce_card_search_id="p_card_search-playing-1-p_card-02-men-1_031",
        lower_search_count=1,
    ),
    _row(
        "e_trigger-exam_card_play-stamina_up_multiple-800",
        PHASE_CARD_PLAY,
        StaminaField.STAMINA_UP_MULTIPLE,
        800,
    ),
)

STAMINA_MULTIPLE_TRIGGER_BY_ID: Mapping[str, StaminaMultipleTrigger] = {
    row.id: row for row in ALL_STAMINA_MULTIPLE_TRIGGERS
}


@dataclass(frozen=True, slots=True)
class StaminaMultipleTriggerCatalog:
    """Immutable catalog with explicit formula and wrapper counts."""

    rows: tuple[StaminaMultipleTrigger, ...]
    evidence: tuple[StaticEvidence, ...] = STATIC_EVIDENCE

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        ids = [row.id for row in self.rows]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate stamina trigger id")

    def get(self, trigger_id: str) -> StaminaMultipleTrigger | None:
        return next((row for row in self.rows if row.id == trigger_id), None)

    @property
    def formula_resolved_rows(self) -> tuple[StaminaMultipleTrigger, ...]:
        """Rows whose native field formula is proven (all scoped rows)."""

        return self.rows

    @property
    def predicate_executable_rows(self) -> tuple[StaminaMultipleTrigger, ...]:
        return tuple(row for row in self.rows if row.predicate_executable)

    @property
    def unresolved_rows(self) -> tuple[StaminaMultipleTrigger, ...]:
        return tuple(row for row in self.rows if not row.predicate_executable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_count": len(self.rows),
            "formula_resolved_row_count": len(self.formula_resolved_rows),
            "predicate_executable_row_count": len(self.predicate_executable_rows),
            "unresolved_row_count": len(self.unresolved_rows),
            "rows": [row.to_dict() for row in self.rows],
            "evidence": [item.to_dict() for item in self.evidence],
        }


STAMINA_MULTIPLE_CATALOG = StaminaMultipleTriggerCatalog(
    rows=ALL_STAMINA_MULTIPLE_TRIGGERS
)


@dataclass(frozen=True, slots=True)
class StaminaSnapshot:
    """The only scalar state read by the native formula."""

    current_stamina: int
    max_stamina: int

    def __post_init__(self) -> None:
        if type(self.current_stamina) is not int:
            raise TypeError("current_stamina must be a plain integer")
        if type(self.max_stamina) is not int:
            raise TypeError("max_stamina must be a plain integer")


@dataclass(frozen=True, slots=True)
class StaminaMultipleEvaluation:
    """Pure field-formula output; ``fires=None`` means fail closed."""

    field_type: StaminaField | str
    threshold: int
    fires: bool | None
    comparison: Comparison | None = None
    current_ratio: float | None = None
    threshold_ratio: float | None = None
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "field_type", StaminaField(self.field_type))
        except ValueError:
            # Keep an unknown raw token visible in JSON while retaining a
            # typed result for all known native cases.
            pass
        if self.comparison is not None:
            object.__setattr__(self, "comparison", Comparison(self.comparison))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def to_dict(self) -> dict[str, Any]:
        field = (
            self.field_type.value
            if isinstance(self.field_type, StaminaField)
            else self.field_type
        )
        return {
            "field_type": field,
            "threshold": self.threshold,
            "fires": self.fires,
            "comparison": (
                self.comparison.value
                if isinstance(self.comparison, Comparison)
                else None
            ),
            "current_ratio": self.current_ratio,
            "threshold_ratio": self.threshold_ratio,
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
        }


@dataclass(frozen=True, slots=True)
class StaminaTriggerEvaluation:
    """Complete row decision; formula can be known while wrapper is not."""

    trigger_id: str
    phase: str | None
    formula: StaminaMultipleEvaluation | None
    fires: bool | None
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "phase": self.phase,
            "formula": None if self.formula is None else self.formula.to_dict(),
            "fires": self.fires,
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
        }


def _float32(value: int | float) -> float:
    """Round one operation to the IEEE-754 binary32 value used by ``S`` regs."""

    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


def _known_field(field_type: StaminaField | str) -> StaminaField | None:
    try:
        return StaminaField(field_type)
    except (TypeError, ValueError):
        return None


def _invalid_formula(
    field_type: StaminaField | str,
    threshold: int,
    reason: str,
) -> StaminaMultipleEvaluation:
    field = _known_field(field_type)
    comparison = (
        None
        if field is None
        else NATIVE_BRANCHES[field].comparison
    )
    return StaminaMultipleEvaluation(
        field_type=field_type,
        threshold=threshold,
        fires=None,
        comparison=comparison,
        reasons=(reason,),
    )


def evaluate_stamina_multiple(
    field_type: StaminaField | str,
    threshold: int,
    snapshot: StaminaSnapshot,
) -> StaminaMultipleEvaluation:
    """Evaluate only the proven native field-status formula.

    The arithmetic mirrors the native instruction shape: both integer getter
    results are converted to binary32, each quotient is computed in binary32,
    and only then are the two quotients compared.  Invalid projected state is
    returned as ``fires=None`` rather than being clamped or divided by zero.
    """

    field = _known_field(field_type)
    if field is None:
        return _invalid_formula(field_type, threshold, "unknown-stamina-field-type")
    if type(threshold) is not int:
        return _invalid_formula(field, threshold, "threshold-is-not-int32")
    if threshold < 0:
        return _invalid_formula(field, threshold, "negative-threshold-unproven")
    if not isinstance(snapshot, StaminaSnapshot):
        return _invalid_formula(field, threshold, "stamina-snapshot-unavailable")
    if snapshot.max_stamina <= 0:
        return _invalid_formula(field, threshold, "max-stamina-nonpositive")
    if snapshot.current_stamina < 0:
        return _invalid_formula(field, threshold, "current-stamina-negative")
    if snapshot.current_stamina > snapshot.max_stamina:
        return _invalid_formula(field, threshold, "current-stamina-exceeds-max")
    if not -(2**31) <= threshold <= 2**31 - 1:
        return _invalid_formula(field, threshold, "threshold-outside-int32")

    try:
        current = _float32(snapshot.current_stamina)
        maximum = _float32(snapshot.max_stamina)
        current_ratio = _float32(current / maximum)
        threshold_f32 = _float32(threshold)
        threshold_ratio = _float32(threshold_f32 / _float32(1000.0))
    except (OverflowError, struct.error, ZeroDivisionError):
        return _invalid_formula(field, threshold, "float32-conversion-unproven")

    comparison = NATIVE_BRANCHES[field].comparison
    if comparison is Comparison.GREATER_EQUAL:
        fires_value = current_ratio >= threshold_ratio
    else:
        fires_value = current_ratio <= threshold_ratio
    return StaminaMultipleEvaluation(
        field_type=field,
        threshold=threshold,
        fires=fires_value,
        comparison=comparison,
        current_ratio=current_ratio,
        threshold_ratio=threshold_ratio,
    )


def evaluate_trigger(
    trigger_or_id: StaminaMultipleTrigger | str,
    snapshot: StaminaSnapshot,
    *,
    event_phase: str | None,
) -> StaminaTriggerEvaluation:
    """Evaluate a catalog row, keeping unsupported wrappers fail-closed.

    ``event_phase`` is required as a keyword so a caller cannot silently use a
    StartTurn snapshot for CardPlay or vice versa.  Lesson-gated and
    search-bearing rows return ``fires=None`` even though their native stamina
    formula is included in the returned ``formula`` field.
    """

    row = (
        trigger_or_id
        if isinstance(trigger_or_id, StaminaMultipleTrigger)
        else STAMINA_MULTIPLE_CATALOG.get(trigger_or_id)
    )
    if row is None:
        return StaminaTriggerEvaluation(
            trigger_id=(
                trigger_or_id.id
                if isinstance(trigger_or_id, StaminaMultipleTrigger)
                else str(trigger_or_id)
            ),
            phase=event_phase,
            formula=None,
            fires=None,
            reasons=("unknown-stamina-trigger-id",),
        )

    formula = evaluate_stamina_multiple(row.field, row.threshold, snapshot)
    reasons = list(formula.reasons)
    if event_phase != row.phase:
        reasons.append("event-phase-mismatch-or-unproven")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("lesson-gate-and-trigger-order-unproven")
    if row.produce_card_search_id:
        reasons.append("card-search-effect-group-and-trigger-order-unproven")
    reasons.extend(row.unresolved_reasons)
    unique_reasons = tuple(dict.fromkeys(reasons))
    fires_value = formula.fires if not unique_reasons else None
    return StaminaTriggerEvaluation(
        trigger_id=row.id,
        phase=event_phase,
        formula=formula,
        fires=fires_value,
        reasons=unique_reasons,
    )


def _native_float32_formula(
    field_type: StaminaField,
    threshold: int,
    snapshot: StaminaSnapshot,
) -> StaminaMultipleEvaluation:
    """Private alias used by tests/audit readers without engine dependencies."""

    return evaluate_stamina_multiple(field_type, threshold, snapshot)


__all__ = [
    "ADJACENT_UNRESOLVED_MULTIPLE_TRIGGER_BY_ID",
    "ADJACENT_UNRESOLVED_MULTIPLE_TRIGGERS",
    "ALL_STAMINA_MULTIPLE_TRIGGERS",
    "AdjacentMultipleTrigger",
    "ArithmeticSemantics",
    "Comparison",
    "EvaluationTiming",
    "FIELD_STAMINA_LESS_MULTIPLE",
    "FIELD_STAMINA_UP_MULTIPLE",
    "LESSON_UNKNOWN",
    "MOVE_UNKNOWN",
    "NATIVE_BRANCHES",
    "NATIVE_FORMULA_EVIDENCE",
    "PHASE_CARD_PLAY",
    "PHASE_CARD_PLAY_AFTER",
    "PHASE_EXAM_START_TURN",
    "STATIC_EVIDENCE",
    "STAMINA_MULTIPLE_CATALOG",
    "STAMINA_MULTIPLE_TRIGGER_BY_ID",
    "StaminaBasis",
    "StaminaField",
    "StaminaMultipleEvaluation",
    "StaminaMultipleTrigger",
    "StaminaMultipleTriggerCatalog",
    "StaminaSnapshot",
    "StaminaTriggerEvaluation",
    "TriggerResolution",
    "evaluate_stamina_multiple",
    "evaluate_trigger",
]
