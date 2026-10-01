"""Bounded Plan2 proof for ``p_card-02-ido-3_105``.

This file is deliberately card-local.  It reads the exact Master rows in
read-only mode, models the StartTurn status-listener predicate, and reuses
the already audited ``ExamStaminaRecoverMultiple``/``StaminaRecoverFix``
primitive from :mod:`plan2_start_turn_review_stamina_recover`.

The card's top-level play effects are retained as ``PlayableValueAdd`` then
``StatusEnchant``.  The status enchant's Master child order is retained as
``ExamLessonDependBlock`` then ``ExamStaminaRecoverMultiple``.  This module
does not alter Plan2 core runtime, central coverage, Plan3, GUI, or a
controller, and it never invokes an agent or mutates runtime state.
"""

from __future__ import annotations

import json
import math
import sqlite3
import struct
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Final

from .master_db import DEFAULT_DATABASE
from .plan2_start_turn_review_stamina_recover import (
    MultipleEffectContract,
    StaminaRecoverMultipleEvaluation,
    StaminaRecoverMultipleRuntime,
    evaluate_stamina_recover_multiple as _evaluate_reused_multiple,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_start_turn_stamina_less_recover_native_audit.json"
)

PHASE_EXAM_START_TURN: Final = "ProduceExamPhaseType_ExamStartTurn"
FIELD_STAMINA_LESS_MULTIPLE: Final = (
    "ProduceExamFieldStatusType_StaminaLessMultiple"
)
MULTIPLE_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamStaminaRecoverMultiple"
LESSON_DEPEND_BLOCK_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonDependBlock"
)
STATUS_ENCHANT_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamStatusEnchant"
PLAYABLE_VALUE_ADD_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamPlayableValueAdd"
)
PLAN2: Final = "ProducePlanType_Plan2"
ACTIVE_SKILL: Final = "ProduceCardCategory_ActiveSkill"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"
MOVE_EFFECT_UNKNOWN: Final = "ProduceCardMoveEffectTriggerType_Unknown"

TARGET_CARD_ID: Final = "p_card-02-ido-3_105"
# Keep the label in escapes so PowerShell/code-page rendering cannot alter
# the Master comparison.  Both the user label and the normalized Master name
# are U+30A8 U+30A6 U+30EC U+30AB U+FF01 (エウレカ！).
TARGET_DISPLAY_LABEL: Final = "\u30a8\u30a6\u30ec\u30ab\uff01"
TARGET_CARD_NAME: Final = TARGET_DISPLAY_LABEL
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_TRIGGER_ID: Final = "e_trigger-exam_start_turn-stamina_less_multiple-500"
TARGET_THRESHOLD: Final = 500

TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE: Final = {
    0: "enchant-p_card-02-ido-3_105-enc01",
    1: "enchant-p_card-02-ido-3_105-enc02",
    2: "enchant-p_card-02-ido-3_105-enc02",
    3: "enchant-p_card-02-ido-3_105-enc02",
}
TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE: Final = {
    0: "e_effect-exam_stamina_recover_multiple-0050",
    1: "e_effect-exam_stamina_recover_multiple-0100",
    2: "e_effect-exam_stamina_recover_multiple-0100",
    3: "e_effect-exam_stamina_recover_multiple-0100",
}
TARGET_LESSON_EFFECT_IDS_BY_UPGRADE: Final = {
    0: "e_effect-exam_lesson_depend_block-0300-01",
    1: "e_effect-exam_lesson_depend_block-0400-01",
    2: "e_effect-exam_lesson_depend_block-0400-01",
    3: "e_effect-exam_lesson_depend_block-0400-01",
}
TARGET_STATUS_WRAPPER_IDS_BY_UPGRADE: Final = {
    upgrade: (
        "e_effect-exam_status_enchant-inf-enchant-p_card-02-ido-3_105-"
        f"enc{'01' if upgrade == 0 else '02'}"
    )
    for upgrade in TARGET_UPGRADES
}
TARGET_TOP_LEVEL_EFFECT_IDS_BY_UPGRADE: Final = {
    upgrade: (
        "e_effect-exam_playable_value_add-01",
        TARGET_STATUS_WRAPPER_IDS_BY_UPGRADE[upgrade],
    )
    for upgrade in TARGET_UPGRADES
}
TARGET_CHILD_EFFECT_IDS_BY_UPGRADE: Final = {
    upgrade: (
        TARGET_LESSON_EFFECT_IDS_BY_UPGRADE[upgrade],
        TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE[upgrade],
    )
    for upgrade in TARGET_UPGRADES
}
TARGET_EFFECT_IDS_BY_UPGRADE: Final = {
    upgrade: TARGET_TOP_LEVEL_EFFECT_IDS_BY_UPGRADE[upgrade]
    + TARGET_CHILD_EFFECT_IDS_BY_UPGRADE[upgrade]
    for upgrade in TARGET_UPGRADES
}
TARGET_CARD_STAMINA_BY_UPGRADE: Final = {0: 12, 1: 12, 2: 10, 3: 9}
TARGET_CARD_NAMES_BY_UPGRADE: Final = {
    0: TARGET_CARD_NAME,
    1: f"{TARGET_CARD_NAME}+",
    2: f"{TARGET_CARD_NAME}++",
    3: f"{TARGET_CARD_NAME}+++",
}

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1
F32_THOUSAND: Final = 1000.0

ANDROID_V323_NATIVE_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/lib/arm64-v8a/"
    "libil2cpp.so"
)
ANDROID_V323_METADATA_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/global-metadata.decrypted.dat"
)
ANDROID_V323_TARGET_METADATA_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/target-metadata.json"
)
ANDROID_V323_EXECUTOR_MAPPING_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json"
)
ANDROID_V323_ISIL_EXTENSIONS_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/ExamExtensions.txt"
)
ANDROID_V323_ISIL_SEQUENCE_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/Exam/"
    "ExamSequence_NestedType__ExecuteCommandImplAsync_d__111.txt"
)
ANDROID_V323_STATUS_ORDER_SOURCE: Final = (
    "docs/android-v323-plan3-status-enchant-executor.md"
)
ANDROID_V323_TURN_ORDER_SOURCE: Final = (
    "docs/android-v323-plan3-static-trigger-order.md"
)
PC_METADATA_SOURCE: Final = (
    "_research/il2cpp/"
    "B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
    "targeted-metadata-index.json"
)
PC_NATIVE_SCAN_SOURCE: Final = (
    "_research/il2cpp/"
    "B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/"
    "native-method-table-scan.json"
)
PC_COMPATIBILITY_SOURCE: Final = "docs/android-pc-exam-metadata-compatibility.md"


class StaminaLessRecoverContractError(ValueError):
    """A Master/native row is outside this exact standalone contract."""


class StartTurnSnapshot(str, Enum):
    """Only the post-draw settled StartTurn snapshot is executable."""

    SETTLED_POST_DRAW = "settled-start-turn-post-draw"
    PRE_DRAW = "pre-draw"
    PRE_SETTLEMENT = "pre-settlement"


class EvaluationBoundary(str, Enum):
    """Status-listener queue boundary, independent of card payment."""

    STATUS_COMMAND_BUILD = "status-command-build"
    POST_STATUS_CHILD_EFFECTS = "post-status-child-effects"
    PRE_DRAW = "pre-draw"


class EvaluationStage(str, Enum):
    STATUS_TRIGGER_COMMAND_BUILD = "status-trigger-command-build"
    STATUS_CHILD_EFFECTS = "status-child-effects"


class ExecutionMode(str, Enum):
    """Turn context; it is not an operand of this phase-driven predicate."""

    ORDINARY = "ordinary"
    NORMAL = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"
    EXTRA_TURN = "extra"

    @classmethod
    def _missing_(cls, value: object) -> "ExecutionMode | None":
        # Accept the user-facing spelling while retaining the sibling Plan2
        # standalone convention ``ExecutionMode.EXTRA.value == "extra"``.
        if value == "extra-turn":
            return cls.EXTRA
        return None


class SourceKind(str, Enum):
    STATUS_LISTENER = "status-listener"
    STATUS_ENCHANT_LISTENER = "status-listener"


# Readable aliases used by sibling standalone probes.
StartTurnSnapshotBoundary = StartTurnSnapshot
CardExecutionKind = ExecutionMode
TurnKind = ExecutionMode


def _i32(value: object, label: str) -> int:
    if type(value) is not int:
        raise StaminaLessRecoverContractError(f"{label} must be a signed Int32")
    if not INT32_MIN <= value <= INT32_MAX:
        raise StaminaLessRecoverContractError(f"{label} is outside signed Int32")
    return value


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        adjective = "text" if empty else "non-empty text"
        raise StaminaLessRecoverContractError(f"{label} must be {adjective}")
    return value


def _json_value(value: object, label: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StaminaLessRecoverContractError(f"{label} is invalid JSON") from exc
    return value


def _json_array(value: object, label: str) -> tuple[object, ...]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise StaminaLessRecoverContractError(f"{label} must be a JSON array")
    return tuple(parsed)


def _json_object(value: object, label: str) -> dict[str, object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, dict):
        raise StaminaLessRecoverContractError(f"{label} must be a JSON object")
    return parsed


def _strict_equal(actual: object, expected: object, label: str) -> None:
    """Compare JSON values without bool/int coercion or unordered arrays."""

    if type(actual) is not type(expected):
        raise StaminaLessRecoverContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )
    if isinstance(expected, dict):
        if set(actual) != set(expected):  # type: ignore[arg-type]
            raise StaminaLessRecoverContractError(
                f"{label} keys are not exact: {sorted(actual)!r}"  # type: ignore[arg-type]
            )
        for key, wanted in expected.items():
            _strict_equal(actual[key], wanted, f"{label}.{key}")  # type: ignore[index]
        return
    if isinstance(expected, (list, tuple)):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise StaminaLessRecoverContractError(
                f"{label} length is not exact: {actual!r}"
            )
        for index, (item, wanted) in enumerate(
            zip(actual, expected, strict=True)  # type: ignore[arg-type]
        ):
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if actual != expected:
        raise StaminaLessRecoverContractError(
            f"{label} must be {expected!r}, got {actual!r}"
        )


def _strict_subset(
    actual: object,
    expected: Mapping[str, object],
    label: str,
    *,
    allowed_extra_keys: Sequence[str] = (),
) -> None:
    if not isinstance(actual, dict):
        raise StaminaLessRecoverContractError(f"{label} must be an object")
    allowed = set(expected) | set(allowed_extra_keys)
    unknown = set(actual) - allowed
    if unknown:
        raise StaminaLessRecoverContractError(
            f"{label} has unknown keys: {sorted(unknown)!r}"
        )
    for key, wanted in expected.items():
        if key not in actual:
            raise StaminaLessRecoverContractError(f"{label} missing {key}")
        _strict_equal(actual[key], wanted, f"{label}.{key}")


def _f32(value: int | float, label: str) -> float:
    """Convert and round once to finite IEEE-754 binary32."""

    try:
        result = struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except (OverflowError, struct.error, ValueError) as exc:
        raise StaminaLessRecoverContractError(
            f"{label} is outside finite float32"
        ) from exc
    if not math.isfinite(result):
        raise StaminaLessRecoverContractError(
            f"{label} is non-finite after float32 conversion"
        )
    return result


def _f32_div(left: int | float, right: int | float, label: str) -> float:
    denominator = _f32(right, f"{label}.denominator")
    if denominator == 0.0:
        raise StaminaLessRecoverContractError(f"{label} divides by zero")
    return _f32(
        _f32(left, f"{label}.left") / denominator,
        label,
    )


@dataclass(frozen=True, slots=True)
class StaminaLessMultipleTrigger:
    """The complete normalized target ``produce_exam_trigger`` row."""

    id: str = TARGET_TRIGGER_ID
    phase_types: tuple[str, ...] = (PHASE_EXAM_START_TURN,)
    phase_values: tuple[int, ...] = ()
    field_status_check_types: tuple[str, ...] = ()
    field_status_types: tuple[str, ...] = (FIELD_STAMINA_LESS_MULTIPLE,)
    field_status_values: tuple[int, ...] = (TARGET_THRESHOLD,)
    field_status_card_search_ids: tuple[str, ...] = ()
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = MOVE_UNKNOWN
    effect_types: tuple[str, ...] = ()
    lesson_type: str = LESSON_UNKNOWN

    def __post_init__(self) -> None:
        expected = (
            ("id", TARGET_TRIGGER_ID),
            ("phase_types", (PHASE_EXAM_START_TURN,)),
            ("phase_values", ()),
            ("field_status_check_types", ()),
            ("field_status_types", (FIELD_STAMINA_LESS_MULTIPLE,)),
            ("field_status_values", (TARGET_THRESHOLD,)),
            ("field_status_card_search_ids", ()),
            ("produce_card_search_id", ""),
            ("upper_search_count", 0),
            ("lower_search_count", 0),
            ("card_move_position_type", MOVE_UNKNOWN),
            ("effect_types", ()),
            ("lesson_type", LESSON_UNKNOWN),
        )
        for label, wanted in expected:
            if getattr(self, label) != wanted:
                raise StaminaLessRecoverContractError(
                    f"{label} has unknown target shape: {getattr(self, label)!r}"
                )
        for label in (
            "phase_values",
            "field_status_values",
            "upper_search_count",
            "lower_search_count",
        ):
            values = getattr(self, label)
            if label.endswith("values"):
                for value in values:
                    _i32(value, f"trigger.{label}")
            else:
                _i32(values, f"trigger.{label}")

    @property
    def trigger_id(self) -> str:
        return self.id

    def master_contract(self) -> dict[str, object]:
        return {
            "id": self.id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_check_types": list(self.field_status_check_types),
            "field_types": list(self.field_status_types),
            "field_values": list(self.field_status_values),
            "field_card_search_ids": list(self.field_status_card_search_ids),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "effect_types": list(self.effect_types),
            "lesson_type": self.lesson_type,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "phaseTypes": list(self.phase_types),
            "phaseValues": list(self.phase_values),
            "fieldStatusCheckTypes": list(self.field_status_check_types),
            "fieldStatusTypes": list(self.field_status_types),
            "fieldStatusValues": list(self.field_status_values),
            "fieldStatusProduceCardSearchIds": list(
                self.field_status_card_search_ids
            ),
            "produceCardSearchId": self.produce_card_search_id,
            "upperSearchCount": self.upper_search_count,
            "lowerSearchCount": self.lower_search_count,
            "cardMovePositionType": self.card_move_position_type,
            "effectTypes": list(self.effect_types),
            "lessonType": self.lesson_type,
        }


EXACT_TARGET_TRIGGER: Final = StaminaLessMultipleTrigger()
STAMINA_LESS_MULTIPLE_TRIGGER: Final = EXACT_TARGET_TRIGGER


def _trigger_raw_shape(trigger: StaminaLessMultipleTrigger) -> dict[str, object]:
    return {
        "id": trigger.id,
        "phaseTypes": list(trigger.phase_types),
        "phaseValues": list(trigger.phase_values),
        "fieldStatusCheckTypes": list(trigger.field_status_check_types),
        "fieldStatusTypes": list(trigger.field_status_types),
        "fieldStatusValues": list(trigger.field_status_values),
        "fieldStatusProduceCardSearchIds": list(
            trigger.field_status_card_search_ids
        ),
        "produceCardSearchId": trigger.produce_card_search_id,
        "upperSearchCount": trigger.upper_search_count,
        "lowerSearchCount": trigger.lower_search_count,
        "cardMovePositionType": trigger.card_move_position_type,
        "effectTypes": list(trigger.effect_types),
        "lessonType": trigger.lesson_type,
    }


def _trigger_from_row(row: sqlite3.Row) -> StaminaLessMultipleTrigger:
    trigger_id = _text(row["id"], "trigger.id")
    if trigger_id != TARGET_TRIGGER_ID:
        raise StaminaLessRecoverContractError("unexpected target trigger id")
    candidate = StaminaLessMultipleTrigger(
        id=trigger_id,
        phase_types=tuple(
            _text(item, "trigger.phaseTypes")
            for item in _json_array(row["phase_types_json"], "trigger.phaseTypes")
        ),
        phase_values=tuple(
            _i32(item, "trigger.phaseValues")
            for item in _json_array(row["phase_values_json"], "trigger.phaseValues")
        ),
        field_status_check_types=tuple(
            _text(item, "trigger.fieldStatusCheckTypes")
            for item in _json_array(
                row["field_status_check_types_json"],
                "trigger.fieldStatusCheckTypes",
            )
        ),
        field_status_types=tuple(
            _text(item, "trigger.fieldStatusTypes")
            for item in _json_array(
                row["field_status_types_json"], "trigger.fieldStatusTypes"
            )
        ),
        field_status_values=tuple(
            _i32(item, "trigger.fieldStatusValues")
            for item in _json_array(
                row["field_status_values_json"], "trigger.fieldStatusValues"
            )
        ),
        field_status_card_search_ids=tuple(
            _text(item, "trigger.fieldStatusProduceCardSearchIds", empty=True)
            for item in _json_array(
                row["field_status_produce_card_search_ids_json"],
                "trigger.fieldStatusProduceCardSearchIds",
            )
        ),
        produce_card_search_id=_text(
            row["produce_card_search_id"],
            "trigger.produceCardSearchId",
            empty=True,
        ),
        upper_search_count=_i32(
            row["upper_search_count"], "trigger.upperSearchCount"
        ),
        lower_search_count=_i32(
            row["lower_search_count"], "trigger.lowerSearchCount"
        ),
        card_move_position_type=_text(
            row["card_move_position_type"],
            "trigger.cardMovePositionType",
            empty=True,
        ),
        effect_types=tuple(
            _text(item, "trigger.effectTypes")
            for item in _json_array(row["effect_types_json"], "trigger.effectTypes")
        ),
        lesson_type=_text(row["lesson_type"], "trigger.lessonType", empty=True),
    )
    raw = _json_object(row["raw_json"], TARGET_TRIGGER_ID)
    _strict_subset(
        raw,
        _trigger_raw_shape(EXACT_TARGET_TRIGGER),
        "trigger.raw",
        allowed_extra_keys=(
            "playEffectProduceDescriptions",
            "playProduceDescriptions",
            "produceDescriptions",
        ),
    )
    return candidate


@dataclass(frozen=True, slots=True)
class ContractResolution:
    supported: bool
    contract: StaminaLessMultipleTrigger | None
    reasons: tuple[str, ...] = ()


def resolve_stamina_less_trigger(source: object) -> ContractResolution:
    """Resolve only the exact trigger; unknown/altered rows stay unresolved."""

    if isinstance(source, str):
        if source == TARGET_TRIGGER_ID:
            return ContractResolution(True, EXACT_TARGET_TRIGGER)
        return ContractResolution(False, None, ("unknown-trigger-id",))
    if not isinstance(source, StaminaLessMultipleTrigger):
        return ContractResolution(False, None, ("trigger-record-type-is-unknown",))
    if source != EXACT_TARGET_TRIGGER:
        return ContractResolution(False, None, ("trigger-shape-is-not-exact",))
    return ContractResolution(True, source)


@dataclass(frozen=True, slots=True)
class StaminaLessEvaluationInput:
    """Values visible to the settled post-draw status trigger command.

    ``current_stamina`` and ``max_stamina`` are signed Int32 values read from
    ``ExamParameterModel``.  Every turn-context field is explicit so a
    pre-draw or post-child-effects snapshot cannot be silently reused.
    Forced and extra-turn modes are accepted because this is a phase-driven
    status listener, not a card-payment gate; their accounting fields remain
    inert and are not used in the predicate.
    """

    current_stamina: int
    max_stamina: int
    phase: str = PHASE_EXAM_START_TURN
    dispatch_phase: str = "StartTurnStatusEffectCommand"
    boundary: EvaluationBoundary = EvaluationBoundary.STATUS_COMMAND_BUILD
    stage: EvaluationStage = EvaluationStage.STATUS_TRIGGER_COMMAND_BUILD
    snapshot: StartTurnSnapshot = StartTurnSnapshot.SETTLED_POST_DRAW
    execution_mode: ExecutionMode = ExecutionMode.ORDINARY
    source_kind: SourceKind = SourceKind.STATUS_LISTENER
    current_turn: int = 0
    extra_turn_remaining: int = 0
    start_turn_settled: bool = True
    full_power_settled: bool = True
    gimmick_settled: bool = True
    draw_settled: bool = True
    recovery_settled: bool = True
    consumption_settled: bool = True
    status_turn_spend_settled: bool = True
    ordered_start_turn_effects_settled: bool = True
    card_cost_paid: bool = False
    direct_effects_applied: bool = False
    timer_installed: bool = False
    card_move_applied: bool = False
    play_count_incremented: bool = False

    def __post_init__(self) -> None:
        _i32(self.current_stamina, "current_stamina")
        _i32(self.max_stamina, "max_stamina")
        _i32(self.current_turn, "current_turn")
        _i32(self.extra_turn_remaining, "extra_turn_remaining")
        if self.current_turn < 0 or self.extra_turn_remaining < 0:
            raise StaminaLessRecoverContractError(
                "turn accounting counters must be non-negative"
            )
        for name, enum_type in (
            ("boundary", EvaluationBoundary),
            ("stage", EvaluationStage),
            ("snapshot", StartTurnSnapshot),
            ("execution_mode", ExecutionMode),
            ("source_kind", SourceKind),
        ):
            try:
                value = getattr(self, name)
                if not isinstance(value, enum_type):
                    object.__setattr__(self, name, enum_type(value))
            except (TypeError, ValueError) as exc:
                raise StaminaLessRecoverContractError(
                    f"{name} is outside the standalone enum contract"
                ) from exc
        for name in (
            "start_turn_settled",
            "full_power_settled",
            "gimmick_settled",
            "draw_settled",
            "recovery_settled",
            "consumption_settled",
            "status_turn_spend_settled",
            "ordered_start_turn_effects_settled",
            "card_cost_paid",
            "direct_effects_applied",
            "timer_installed",
            "card_move_applied",
            "play_count_incremented",
        ):
            if type(getattr(self, name)) is not bool:
                raise StaminaLessRecoverContractError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class StaminaLessEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    current_stamina: int
    max_stamina: int
    threshold: int | None
    comparison: str | None
    current_ratio_float32: float | None
    threshold_ratio_float32: float | None
    phase: str
    dispatch_phase: str
    boundary: EvaluationBoundary
    stage: EvaluationStage
    snapshot: StartTurnSnapshot
    execution_mode: ExecutionMode
    source_kind: SourceKind
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def allows_child_effects(self) -> bool:
        return self.supported and self.fires is True

    @property
    def result_expression(self) -> str | None:
        if self.threshold is None:
            return None
        return (
            "float32(float32(current_stamina)/float32(max_stamina)) "
            "<= float32(500)/float32(1000.0f)"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "allowsChildEffects": self.allows_child_effects,
            "currentStamina": self.current_stamina,
            "maxStamina": self.max_stamina,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "currentRatioFloat32": self.current_ratio_float32,
            "thresholdRatioFloat32": self.threshold_ratio_float32,
            "resultExpression": self.result_expression,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "stage": self.stage.value,
            "snapshot": self.snapshot.value,
            "executionMode": self.execution_mode.value,
            "sourceKind": self.source_kind.value,
            "reasons": list(self.reasons),
        }


def _unsupported_trigger(
    trigger_id: str,
    inputs: StaminaLessEvaluationInput,
    reasons: Sequence[str],
) -> StaminaLessEvaluation:
    return StaminaLessEvaluation(
        trigger_id=trigger_id,
        supported=False,
        fires=None,
        current_stamina=inputs.current_stamina,
        max_stamina=inputs.max_stamina,
        threshold=TARGET_THRESHOLD,
        comparison="float32_ratio_less_equal",
        current_ratio_float32=None,
        threshold_ratio_float32=None,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_mode=inputs.execution_mode,
        source_kind=inputs.source_kind,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def evaluate_stamina_less_trigger(
    inputs: StaminaLessEvaluationInput,
    trigger: object = EXACT_TARGET_TRIGGER,
) -> StaminaLessEvaluation:
    """Evaluate the exact inclusive ``current/max <= 500/1000`` predicate."""

    if not isinstance(inputs, StaminaLessEvaluationInput):
        raise TypeError("inputs must be StaminaLessEvaluationInput")
    resolution = resolve_stamina_less_trigger(trigger)
    trigger_id = str(getattr(trigger, "id", trigger))
    if not resolution.supported:
        return _unsupported_trigger(trigger_id, inputs, resolution.reasons)

    reasons: list[str] = []
    if inputs.phase != PHASE_EXAM_START_TURN:
        reasons.append("runtime-phase-is-not-exam-start-turn")
    if inputs.dispatch_phase != "StartTurnStatusEffectCommand":
        reasons.append("dispatch-phase-is-not-start-turn-status-command")
    if inputs.source_kind is not SourceKind.STATUS_LISTENER:
        reasons.append("source-is-not-status-listener")
    if inputs.boundary is not EvaluationBoundary.STATUS_COMMAND_BUILD:
        reasons.append("status-trigger-is-not-command-build-boundary")
    if inputs.stage is not EvaluationStage.STATUS_TRIGGER_COMMAND_BUILD:
        reasons.append("status-trigger-is-not-command-build-stage")
    if inputs.snapshot is not StartTurnSnapshot.SETTLED_POST_DRAW:
        reasons.append("start-turn-snapshot-is-not-settled-post-draw")
    for name, reason in (
        ("start_turn_settled", "start-turn-settlement-not-proven"),
        ("full_power_settled", "full-power-settlement-not-proven"),
        ("gimmick_settled", "gimmick-settlement-not-proven"),
        ("draw_settled", "draw-settlement-not-proven"),
        ("recovery_settled", "start-turn-recovery-not-settled"),
        ("consumption_settled", "stamina-consumption-not-settled"),
        ("status_turn_spend_settled", "status-turn-spend-not-settled"),
        (
            "ordered_start_turn_effects_settled",
            "ordered-start-turn-effects-not-settled",
        ),
    ):
        if not getattr(inputs, name):
            reasons.append(reason)
    if any(
        (
            inputs.card_cost_paid,
            inputs.direct_effects_applied,
            inputs.timer_installed,
            inputs.card_move_applied,
            inputs.play_count_incremented,
        )
    ):
        reasons.append("snapshot-is-after-card-payment-effect-move-or-count-step")
    if inputs.max_stamina <= 0:
        reasons.append("max-stamina-is-zero-or-negative")
    if inputs.current_stamina < 0:
        reasons.append("current-stamina-is-negative")
    if inputs.current_stamina > inputs.max_stamina:
        reasons.append("current-stamina-exceeds-max-stamina")
    if reasons:
        return _unsupported_trigger(TARGET_TRIGGER_ID, inputs, reasons)

    try:
        current_ratio = _f32_div(
            _f32(inputs.current_stamina, "current_stamina"),
            _f32(inputs.max_stamina, "max_stamina"),
            "stamina-ratio",
        )
        threshold_ratio = _f32_div(
            _f32(TARGET_THRESHOLD, "threshold"),
            _f32(F32_THOUSAND, "denominator"),
            "threshold-ratio",
        )
    except StaminaLessRecoverContractError as exc:
        return _unsupported_trigger(
            TARGET_TRIGGER_ID,
            inputs,
            (f"non-finite-trigger-arithmetic:{exc}",),
        )
    if not math.isfinite(current_ratio) or not math.isfinite(threshold_ratio):
        return _unsupported_trigger(
            TARGET_TRIGGER_ID,
            inputs,
            ("non-finite-trigger-arithmetic",),
        )
    return StaminaLessEvaluation(
        trigger_id=TARGET_TRIGGER_ID,
        supported=True,
        fires=current_ratio <= threshold_ratio,
        current_stamina=inputs.current_stamina,
        max_stamina=inputs.max_stamina,
        threshold=TARGET_THRESHOLD,
        comparison="float32_ratio_less_equal",
        current_ratio_float32=current_ratio,
        threshold_ratio_float32=threshold_ratio,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        snapshot=inputs.snapshot,
        execution_mode=inputs.execution_mode,
        source_kind=inputs.source_kind,
    )


evaluate_stamina_less_multiple = evaluate_stamina_less_trigger
evaluate_plan2_start_turn_stamina_less_multiple = evaluate_stamina_less_trigger


_EFFECT_GROUPS_BY_TYPE: Final = {
    PLAYABLE_VALUE_ADD_EFFECT_TYPE: (
        "effect_group-visible-exam_playable_value_add-000",
    ),
    STATUS_ENCHANT_EFFECT_TYPE: (
        "effect_group-visible-exam_lesson_depend_block-000",
        "effect_group-visible-exam_lesson-000",
        "effect_group-visible-exam_status_enchant-000",
        "effect_group-visible-stamina_recover_fix-000",
    ),
    LESSON_DEPEND_BLOCK_EFFECT_TYPE: (
        "effect_group-visible-exam_lesson_depend_block-000",
        "effect_group-visible-exam_lesson-000",
    ),
    MULTIPLE_EFFECT_TYPE: ("effect_group-visible-stamina_recover_fix-000",),
}
_EXPECTED_EFFECT_SCALARS: Final = {
    "e_effect-exam_playable_value_add-01": (
        PLAYABLE_VALUE_ADD_EFFECT_TYPE,
        0,
        0,
        1,
        0,
        "",
    ),
    TARGET_STATUS_WRAPPER_IDS_BY_UPGRADE[0]: (
        STATUS_ENCHANT_EFFECT_TYPE,
        0,
        0,
        0,
        -1,
        TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE[0],
    ),
    TARGET_STATUS_WRAPPER_IDS_BY_UPGRADE[1]: (
        STATUS_ENCHANT_EFFECT_TYPE,
        0,
        0,
        0,
        -1,
        TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE[1],
    ),
    "e_effect-exam_lesson_depend_block-0300-01": (
        LESSON_DEPEND_BLOCK_EFFECT_TYPE,
        300,
        0,
        1,
        0,
        "",
    ),
    "e_effect-exam_lesson_depend_block-0400-01": (
        LESSON_DEPEND_BLOCK_EFFECT_TYPE,
        400,
        0,
        1,
        0,
        "",
    ),
    "e_effect-exam_stamina_recover_multiple-0050": (
        MULTIPLE_EFFECT_TYPE,
        50,
        0,
        0,
        0,
        "",
    ),
    "e_effect-exam_stamina_recover_multiple-0100": (
        MULTIPLE_EFFECT_TYPE,
        100,
        0,
        0,
        0,
        "",
    ),
}


def _neutral_effect_raw(
    effect_id: str,
    effect_type: str,
    value1: int,
    value2: int,
    count: int,
    turn: int,
    status_enchant_id: str,
) -> dict[str, object]:
    return {
        "id": effect_id,
        "effectType": effect_type,
        "effectValue1": value1,
        "effectValue2": value2,
        "effectCount": count,
        "effectTurn": turn,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": "",
        "movePositionType": MOVE_UNKNOWN,
        "pickRangeType": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": "ProducePickRangeType_Unknown",
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": "ProducePickCountType_Unknown",
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": status_enchant_id,
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": list(_EFFECT_GROUPS_BY_TYPE[effect_type]),
    }


@dataclass(frozen=True, slots=True)
class EffectShape:
    """Exact normalized scalar and raw execution-link shape for one effect."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.effect_id,
            "effect_type": self.effect_type,
            "effect_value1": self.value1,
            "effect_value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
        }

    def as_multiple_contract(self) -> MultipleEffectContract:
        if self.effect_type != MULTIPLE_EFFECT_TYPE:
            raise StaminaLessRecoverContractError(
                "effect is not ExamStaminaRecoverMultiple"
            )
        return MultipleEffectContract(
            effect_id=self.effect_id,
            effect_value1=self.value1,
            effect_value2=self.value2,
            effect_count=self.effect_count,
            effect_turn=self.effect_turn,
            effect_type=self.effect_type,
            status_enchant_id=self.status_enchant_id,
            chain_effect_id=self.chain_effect_id,
            effect_group_ids=self.effect_group_ids,
        )


def _effect_from_row(
    row: sqlite3.Row,
    expected_id: str,
    issues: list[str],
) -> EffectShape | None:
    label = f"effect:{expected_id}"
    try:
        expected = _EXPECTED_EFFECT_SCALARS[expected_id]
        effect_type, value1, value2, count, turn, status_id = expected
        effect_id = _text(row["id"], f"{label}.id")
        if effect_id != expected_id:
            raise StaminaLessRecoverContractError("effect id mismatch")
        normalized = (
            effect_type,
            _i32(row["value1"], f"{label}.value1"),
            _i32(row["value2"], f"{label}.value2"),
            _i32(row["effect_count"], f"{label}.effect_count"),
            _i32(row["effect_turn"], f"{label}.effect_turn"),
            _text(row["status_enchant_id"], f"{label}.status_enchant_id", empty=True),
        )
        if normalized != expected:
            raise StaminaLessRecoverContractError(
                f"scalar mismatch: expected {expected!r}, got {normalized!r}"
            )
        chain_id = _text(row["chain_effect_id"], f"{label}.chain_effect_id", empty=True)
        if chain_id:
            raise StaminaLessRecoverContractError("chain effect is not empty")
        raw = _json_object(row["raw_json"], label)
        _strict_subset(
            raw,
            _neutral_effect_raw(
                expected_id,
                effect_type,
                value1,
                value2,
                count,
                turn,
                status_id,
            ),
            f"{label}.raw",
            allowed_extra_keys=("customizeProduceDescriptions", "produceDescriptions"),
        )
        return EffectShape(
            effect_id=effect_id,
            effect_type=effect_type,
            value1=value1,
            value2=value2,
            effect_count=count,
            effect_turn=turn,
            status_enchant_id=status_id,
            chain_effect_id=chain_id,
            effect_group_ids=_EFFECT_GROUPS_BY_TYPE[effect_type],
        )
    except (KeyError, TypeError, ValueError, StaminaLessRecoverContractError) as exc:
        issues.append(f"{label}:{exc}")
        return None


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class StatusEnchantRow:
    id: str
    asset_id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "asset_id": self.asset_id,
            "trigger_id": self.trigger_id,
            "child_effect_ids": list(self.child_effect_ids),
        }


@dataclass(frozen=True, slots=True)
class TargetCardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    effect_slots: tuple[CardEffectSlot, ...]
    status_enchant_id: str
    ordered_child_effect_ids: tuple[str, ...]

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def complete_ordered_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids + self.ordered_child_effect_ids

    @property
    def multiple_effect_id(self) -> str:
        return self.ordered_child_effect_ids[1]

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "name": self.name,
            "plan_type": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "cost_type": self.cost_type,
            "cost_value": self.cost_value,
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "effect_slots": [slot.to_dict() for slot in self.effect_slots],
            "status_enchant_id": self.status_enchant_id,
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "ordered_child_effect_ids": list(self.ordered_child_effect_ids),
            "complete_ordered_effect_ids": list(self.complete_ordered_effect_ids),
        }


_CARD_RAW_EXTRA_KEYS: Final = (
    "assetId",
    "evaluation",
    "isCharacterAsset",
    "isConversion",
    "isEndTurnLost",
    "isInitial",
    "isInitialDeckProduceCard",
    "isLimited",
    "isRestrict",
    "isReward",
    "libraryHidden",
    "maxCustomizeCount",
    "noDeckDuplication",
    "order",
    "originCharacterId",
    "originIdolCardId",
    "originPrimaStellaIdolCardId",
    "originSupportCardId",
    "produceDescriptions",
    "rarity",
    "rentalUnlockProducerLevel",
    "searchTag",
    "unlockProducerLevel",
    "viewStartTime",
    "voiceAssetId",
)


def _card_from_row(row: sqlite3.Row, issues: list[str]) -> TargetCardVersion | None:
    label = f"card:{row['id']}:{row['upgrade_count']}"
    try:
        card_id = _text(row["id"], f"{label}.id")
        upgrade = _i32(row["upgrade_count"], f"{label}.upgrade_count")
        if card_id != TARGET_CARD_ID or upgrade not in TARGET_UPGRADES:
            raise StaminaLessRecoverContractError("unexpected target card key")
        expected_top = TARGET_TOP_LEVEL_EFFECT_IDS_BY_UPGRADE[upgrade]
        effects = _json_array(row["play_effects_json"], f"{label}.playEffects")
        expected_effects = [
            {
                "produceExamTriggerId": "",
                "produceExamEffectId": effect_id,
                "hideIcon": False,
                "isOncePlayEffect": False,
            }
            for effect_id in expected_top
        ]
        _strict_equal(list(effects), expected_effects, f"{label}.playEffects")
        slots = tuple(
            CardEffectSlot(
                slot_index=index,
                effect_id=item["produceExamEffectId"],
                trigger_id=item["produceExamTriggerId"],
                hide_icon=item["hideIcon"],
                is_once_play_effect=item["isOncePlayEffect"],
            )
            for index, item in enumerate(effects)
            if isinstance(item, dict)
        )
        if len(slots) != len(effects):
            raise StaminaLessRecoverContractError("play effect slot is not an object")
        name = _text(row["name"], f"{label}.name")
        normalized_expected = {
            "id": TARGET_CARD_ID,
            "upgradeCount": upgrade,
            "name": TARGET_CARD_NAMES_BY_UPGRADE[upgrade],
            "planType": PLAN2,
            "category": ACTIVE_SKILL,
            "stamina": TARGET_CARD_STAMINA_BY_UPGRADE[upgrade],
            "forceStamina": 0,
            "costType": "ExamCostType_Unknown",
            "costValue": 0,
            "playProduceExamTriggerId": "",
            "playEffects": expected_effects,
            "moveProduceExamTriggerIds": [],
            "playMovePositionType": MOVE_LOST,
            "moveEffectTriggerType": MOVE_EFFECT_UNKNOWN,
            "moveProduceExamEffectIds": [],
            "produceCardCustomizeIds": [],
            "produceCardStatusEnchantId": "",
            "effectGroupIds": [
                "effect_group-visible-exam_lesson_depend_block-000",
                "effect_group-visible-exam_lesson-000",
                "effect_group-visible-exam_status_enchant-000",
                "effect_group-visible-exam_playable_value_add-000",
                "effect_group-visible-stamina_recover_fix-000",
            ],
        }
        raw = _json_object(row["raw_json"], label)
        _strict_subset(
            raw,
            normalized_expected,
            f"{label}.raw",
            allowed_extra_keys=_CARD_RAW_EXTRA_KEYS,
        )
        if (
            name != normalized_expected["name"]
            or row["plan_type"] != PLAN2
            or row["category"] != ACTIVE_SKILL
            or row["stamina"] != TARGET_CARD_STAMINA_BY_UPGRADE[upgrade]
            or row["cost_type"] != "ExamCostType_Unknown"
            or row["cost_value"] != 0
            or row["play_trigger_id"] != ""
            or row["move_position_type"] != MOVE_LOST
        ):
            raise StaminaLessRecoverContractError("normalized card scalar mismatch")
        status_id = ""
        wrapper = slots[1]
        if wrapper.effect_id != TARGET_STATUS_WRAPPER_IDS_BY_UPGRADE[upgrade]:
            raise StaminaLessRecoverContractError("status wrapper slot mismatch")
        status_id = TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE[upgrade]
        return TargetCardVersion(
            card_id=card_id,
            upgrade_count=upgrade,
            name=name,
            plan_type=row["plan_type"],
            category=row["category"],
            stamina=_i32(row["stamina"], f"{label}.stamina"),
            cost_type=_text(row["cost_type"], f"{label}.cost_type", empty=True),
            cost_value=_i32(row["cost_value"], f"{label}.cost_value"),
            play_trigger_id=_text(
                row["play_trigger_id"], f"{label}.play_trigger_id", empty=True
            ),
            move_position_type=_text(
                row["move_position_type"], f"{label}.move_position_type", empty=True
            ),
            effect_slots=slots,
            status_enchant_id=status_id,
            ordered_child_effect_ids=TARGET_CHILD_EFFECT_IDS_BY_UPGRADE[upgrade],
        )
    except (KeyError, TypeError, ValueError, StaminaLessRecoverContractError) as exc:
        issues.append(f"{label}:{exc}")
        return None


def _status_from_row(
    row: sqlite3.Row,
    expected_id: str,
    issues: list[str],
) -> StatusEnchantRow | None:
    label = f"status-enchant:{expected_id}"
    try:
        child = tuple(
            _text(item, f"{label}.produceExamEffectIds")
            for item in _json_array(
                row["produce_exam_effect_ids_json"],
                f"{label}.produceExamEffectIds",
            )
        )
        expected_child = (
            TARGET_CHILD_EFFECT_IDS_BY_UPGRADE[0]
            if expected_id == TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE[0]
            else TARGET_CHILD_EFFECT_IDS_BY_UPGRADE[1]
        )
        expected_raw = {
            "id": expected_id,
            "assetId": "",
            "produceExamTriggerId": TARGET_TRIGGER_ID,
            "produceExamEffectIds": list(expected_child),
        }
        raw = _json_object(row["raw_json"], label)
        _strict_subset(
            raw,
            expected_raw,
            f"{label}.raw",
            allowed_extra_keys=("produceDescriptions",),
        )
        if (
            row["id"] != expected_id
            or row["asset_id"] != ""
            or row["produce_exam_trigger_id"] != TARGET_TRIGGER_ID
            or child != expected_child
        ):
            raise StaminaLessRecoverContractError("normalized status-enchant mismatch")
        return StatusEnchantRow(
            id=expected_id,
            asset_id=row["asset_id"],
            trigger_id=row["produce_exam_trigger_id"],
            child_effect_ids=child,
        )
    except (KeyError, TypeError, ValueError, StaminaLessRecoverContractError) as exc:
        issues.append(f"{label}:{exc}")
        return None


@dataclass(frozen=True, slots=True)
class StaminaLessRecoverCatalog:
    database: str
    trigger: StaminaLessMultipleTrigger | None
    card_versions: tuple[TargetCardVersion, ...]
    status_enchants: tuple[StatusEnchantRow, ...]
    effect_shapes: tuple[EffectShape, ...]
    shape_issues: tuple[str, ...] = ()

    @property
    def affected_card_versions(self) -> tuple[TargetCardVersion, ...]:
        return self.card_versions

    @property
    def exact_shape_supported(self) -> bool:
        return (
            not self.shape_issues
            and self.trigger == EXACT_TARGET_TRIGGER
            and tuple(row.upgrade_count for row in self.card_versions)
            == TARGET_UPGRADES
            and len(self.status_enchants) == 2
            and len(self.effect_shapes) == len(_EXPECTED_EFFECT_SCALARS)
            and all(
                row.status_enchant_id == TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE[row.upgrade_count]
                and row.ordered_child_effect_ids
                == TARGET_CHILD_EFFECT_IDS_BY_UPGRADE[row.upgrade_count]
                for row in self.card_versions
            )
        )

    def card_version(self, upgrade: int) -> TargetCardVersion:
        for row in self.card_versions:
            if row.upgrade_count == upgrade:
                return row
        raise StaminaLessRecoverContractError(
            f"unknown target upgrade: {upgrade!r}"
        )

    def effect_shape(self, effect_id: str) -> EffectShape:
        for row in self.effect_shapes:
            if row.effect_id == effect_id:
                return row
        raise StaminaLessRecoverContractError(f"unknown target effect: {effect_id}")

    def multiple_effect(self, upgrade: int) -> MultipleEffectContract:
        return self.effect_shape(
            TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE[upgrade]
        ).as_multiple_contract()

    def _family_accounting(
        self,
        family_id: str,
        *,
        direct: int,
        exact: bool,
    ) -> dict[str, object]:
        affected = len(self.affected_card_versions)
        co = affected - direct
        return {
            "id": family_id,
            "affected": affected,
            "direct": direct,
            "co": co,
            "affected_card_versions": affected,
            "direct_card_versions": direct,
            "co_blocked_card_versions": co,
            "proof": exact,
        }

    def family_accounting(self) -> dict[str, dict[str, object]]:
        exact = self.exact_shape_supported
        affected = len(self.affected_card_versions)
        return {
            "status_trigger": self._family_accounting(
                TARGET_TRIGGER_ID, direct=0, exact=exact
            ),
            "stamina_recover_multiple": self._family_accounting(
                MULTIPLE_EFFECT_TYPE, direct=0, exact=exact
            ),
            "combined": self._family_accounting(
                f"{TARGET_TRIGGER_ID}+{MULTIPLE_EFFECT_TYPE}",
                direct=affected if exact else 0,
                exact=exact,
            ),
        }

    def summary(self) -> dict[str, object]:
        combined = self.family_accounting()["combined"]
        return {
            "affected_card_versions": len(self.affected_card_versions),
            "direct_card_versions": combined["direct"],
            "co_blocked_card_versions": combined["co"],
            "directly_unlocked_if_fixed_alone": 0,
            "directly_unlocked_if_both_blockers_fixed": combined["direct"],
            "families": self.family_accounting(),
            "exact_shape_supported": self.exact_shape_supported,
            "shape_issues": list(self.shape_issues),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "trigger": None if self.trigger is None else self.trigger.to_dict(),
            "card_versions": [row.to_dict() for row in self.card_versions],
            "status_enchants": [row.to_dict() for row in self.status_enchants],
            "effect_shapes": [row.to_dict() for row in self.effect_shapes],
            "summary": self.summary(),
        }


def load_catalog(database: Path | str = DEFAULT_DATABASE) -> StaminaLessRecoverCatalog:
    """Read the exact target slice from SQLite without opening write mode."""

    path = Path(database)
    if not path.is_file():
        raise StaminaLessRecoverContractError(f"Master database not found: {path}")
    issues: list[str] = []
    trigger: StaminaLessMultipleTrigger | None = None
    cards: list[TargetCardVersion] = []
    statuses: list[StatusEnchantRow] = []
    effects: list[EffectShape] = []
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        if trigger_row is None:
            issues.append("missing-target-trigger")
        else:
            try:
                trigger = _trigger_from_row(trigger_row)
            except (KeyError, TypeError, ValueError, StaminaLessRecoverContractError) as exc:
                issues.append(f"trigger:{exc}")

        card_rows = connection.execute(
            "SELECT id, upgrade_count, name, plan_type, category, stamina, "
            "cost_type, cost_value, play_trigger_id, move_position_type, "
            "play_effects_json, raw_json FROM card WHERE id = ? "
            "ORDER BY upgrade_count",
            (TARGET_CARD_ID,),
        ).fetchall()
        for row in card_rows:
            parsed = _card_from_row(row, issues)
            if parsed is not None:
                cards.append(parsed)
        if tuple(row.upgrade_count for row in cards) != TARGET_UPGRADES:
            issues.append("target-upgrades-are-not-exact-0-1-2-3")
        if len(cards) != 4:
            issues.append(f"target-card-version-count:{len(cards)}")

        for status_id in TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE.values():
            row = connection.execute(
                "SELECT id, asset_id, produce_exam_trigger_id, "
                "produce_exam_effect_ids_json, raw_json "
                "FROM produce_exam_status_enchant WHERE id = ?",
                (status_id,),
            ).fetchone()
            if row is None:
                issues.append(f"missing-target-status-enchant:{status_id}")
                continue
            parsed = _status_from_row(row, status_id, issues)
            if parsed is not None and parsed not in statuses:
                statuses.append(parsed)

        for effect_id in _EXPECTED_EFFECT_SCALARS:
            row = connection.execute(
                "SELECT id, effect_type, value1, value2, effect_count, effect_turn, "
                "status_enchant_id, chain_effect_id, raw_json FROM effect WHERE id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                issues.append(f"missing-target-effect:{effect_id}")
                continue
            parsed = _effect_from_row(row, effect_id, issues)
            if parsed is not None:
                effects.append(parsed)

    statuses.sort(key=lambda row: row.id)
    return StaminaLessRecoverCatalog(
        database=str(path),
        trigger=trigger,
        card_versions=tuple(cards),
        status_enchants=tuple(statuses),
        effect_shapes=tuple(effects),
        shape_issues=tuple(issues),
    )


load_plan2_start_turn_stamina_less_recover_catalog = load_catalog


def _unresolved_multiple_result(
    runtime: StaminaRecoverMultipleRuntime,
    *,
    simulate: bool,
    reason: str,
) -> StaminaRecoverMultipleEvaluation:
    # The existing bounded evaluator owns the exact result dataclass and
    # returns this same identity-preserving unresolved shape for unknown
    # effect mappings.  Keeping the call here avoids duplicating Fix logic.
    return _evaluate_reused_multiple(
        {"mysteryBehavior": True}, runtime, simulate=simulate
    )


def evaluate_stamina_recover_multiple(
    effect: EffectShape | MultipleEffectContract | Mapping[str, object],
    runtime: StaminaRecoverMultipleRuntime,
    *,
    simulate: bool = False,
) -> StaminaRecoverMultipleEvaluation:
    """Evaluate Multiple through the prior audited Fix primitive.

    The adapter adds two boundaries around that reusable implementation:
    target ``EffectShape`` rows are converted only when exact, and a native
    float32 infinity/indefinite path is rejected instead of being treated as
    an executable recovery.  The ordinary finite Int32 path remains exactly
    ``ceil_f32(f32(f32(Max)*f32(value1))/1000.0f)``.
    """

    if isinstance(effect, EffectShape):
        try:
            effect = effect.as_multiple_contract()
        except (TypeError, ValueError, StaminaLessRecoverContractError):
            return _unresolved_multiple_result(
                runtime, simulate=simulate, reason="effect-shape-is-not-exact"
            )
    result = _evaluate_reused_multiple(effect, runtime, simulate=simulate)
    scaled = result.scaled_float32
    if scaled is not None and not math.isfinite(scaled):
        return replace(
            result,
            runtime_after=result.runtime_before,
            executable=False,
            reason="non-finite-Multiple-float32-path-fail-closed",
            adjusted_recovery_value=None,
            calculated_recovery=None,
            predicted_stamina=None,
            difference=None,
            trace=result.trace + ("multiple:non-finite-fail-closed",),
        )
    return result


evaluate_multiple = evaluate_stamina_recover_multiple


def evaluate_target_card_blockers(
    catalog: StaminaLessRecoverCatalog,
    upgrade: int,
    inputs: StaminaLessEvaluationInput,
    runtime: StaminaRecoverMultipleRuntime,
    *,
    simulate_effect: bool = False,
) -> tuple[StaminaLessEvaluation, StaminaRecoverMultipleEvaluation]:
    """Evaluate both missing families for one exact card version."""

    if not catalog.exact_shape_supported:
        trigger = _unsupported_trigger(
            TARGET_TRIGGER_ID,
            inputs,
            ("target-card-catalog-shape-unresolved",),
        )
        effect = _unresolved_multiple_result(
            runtime,
            simulate=simulate_effect,
            reason="target-card-catalog-shape-unresolved",
        )
        return trigger, replace(effect, reason="target-card-catalog-shape-unresolved")
    try:
        card = catalog.card_version(upgrade)
        trigger = evaluate_stamina_less_trigger(inputs, catalog.trigger)
        effect = evaluate_stamina_recover_multiple(
            catalog.multiple_effect(card.upgrade_count),
            runtime,
            simulate=simulate_effect,
        )
        return trigger, effect
    except (KeyError, TypeError, ValueError, StaminaLessRecoverContractError) as exc:
        trigger = _unsupported_trigger(
            TARGET_TRIGGER_ID,
            inputs,
            (f"target-card-resolution-failed:{exc}",),
        )
        effect = _unresolved_multiple_result(
            runtime,
            simulate=simulate_effect,
            reason="target-card-resolution-failed",
        )
        return trigger, replace(effect, reason="target-card-resolution-failed")


NATIVE_EVIDENCE: Final[dict[str, object]] = {
    "stamina_less_multiple": {
        "android_method": "Campus.InGame.ExamExtensions.IsFieldStatusTriggerStatusEffect",
        "android_method_va": "0x68082D4",
        "android_branch_va": "0x6808A6C",
        "android_compare_va": "0x6808AA4",
        "field_enum_value": 5,
        "field_status_type": FIELD_STAMINA_LESS_MULTIPLE,
        "current_getter": {
            "method": "ExamParameterModel.get_Stamina",
            "metadata_va": "0x7EB7ABC",
            "value": "signed current stamina Int32",
        },
        "max_getter": {
            "method": "ExamParameterModel.get_MaxStamina",
            "metadata_va": "0x7EB7BB0",
            "value": "signed max stamina Int32",
        },
        "context": "ExamEffectCalculateContext +0x10 -> ExamParameterModel",
        "threshold": 500,
        "denominator": "float32(1000.0f)",
        "formula": (
            "float32(float32(current_stamina)/float32(max_stamina)) "
            "<= float32(500)/float32(1000.0f)"
        ),
        "arithmetic": [
            "SCVTF signed current Int32 to binary32",
            "SCVTF signed max Int32 to binary32",
            "binary32 current/max division",
            "SCVTF threshold 500 to binary32",
            "binary32 threshold/1000.0f division",
            "FCMP followed by ordered CSET LS; inclusive <=",
            "no integer truncation or ceil in the predicate",
        ],
        "boundary": {
            "current_0_max_1": True,
            "current_500_max_1000": True,
            "current_501_max_1000": False,
        },
        "invalid_runtime": {
            "max_zero": "fail-closed; division-by-zero domain is not executable",
            "max_negative": "fail-closed",
            "current_negative": "fail-closed",
            "current_over_max": "fail-closed",
            "non_finite": "fail-closed",
        },
    },
    "stamina_recover_multiple": {
        "executor": "Campus.InGame.Exam.StaminaRecoverMultipleEffectExecutor",
        "constructor_va": "0x7E8D78C",
        "execute_va": "0x7E5D844",
        "calculate_va": "0x7E5EC2C",
        "add_fix_va": "0x7E5EDE0",
        "base": "MaxStamina",
        "not_base": ["current stamina", "missing amount"],
        "unit": "permille",
        "effect_values_by_upgrade": {
            "0": 50,
            "1": 100,
            "2": 100,
            "3": 100,
        },
        "formula": "ceil_f32(f32(f32(MaxStamina)*f32(effectValue1))/f32(1000.0f))",
        "rounding": [
            "binary32 MaxStamina/effectValue1 conversion",
            "binary32 multiply",
            "binary32 divide by 1000.0f",
            "FRINTP/FCVTPS ceiling; no truncation",
            "no -0.0001f epsilon in Multiple; that epsilon belongs to additive status adjustment",
            "native infinity/out-of-range conversion is an Int32 indefinite path; this adapter fails it closed",
        ],
        "post_formula_order": [
            "CalculateStaminaRecover",
            "active StaminaRecoverRestriction returns 0",
            "active StaminaRecoverAdd adjusts by binary32 ceil ratio term",
            "first cap: min(MaxStamina, int32(current+adjusted))-current",
            "AddStaminaFix",
            "second cap: max(0, min(int32(current+calculated), MaxStamina))",
            "difference create/set/append",
            "ExamParameterModel.SetStamina",
            "EffectDifferenceExecuted callback",
        ],
        "bounds": {
            "zero_max_zero_current": "supported no-op: request/recovery/final are 0",
            "negative_max": "fail-closed",
            "negative_current": "fail-closed",
            "over_cap": "fail-closed",
            "effect_value_le_zero": "native value<1 path produces 0 and never decreases stamina",
            "non_finite": "fail-closed",
        },
    },
    "start_turn": {
        "snapshot": "settled-start-turn-post-draw",
        "order": [
            "status-turn spend settles",
            "full-power resolution",
            "gimmick resolution",
            "ordinary draw settles",
            "ExamStartTurn status-listener command build",
            "read signed current/max stamina",
            "nested child[0] ExamLessonDependBlock",
            "nested child[1] ExamStaminaRecoverMultiple",
            "recovery calculate/add/cap/difference/set/callback",
        ],
        "ordinary": "supported",
        "forced": "supported as the same phase-driven status listener",
        "extra_turn": "supported as the same phase-driven status listener",
        "forced_extra_policy": (
            "mode is not a predicate operand; ExtraTurn accounting changes remaining/extra-turn state, "
            "not the already settled CurrentTurn or stamina ratio. No separate card-payment gate is claimed."
        ),
    },
}


def build_native_audit(
    catalog: StaminaLessRecoverCatalog | None = None,
) -> dict[str, object]:
    """Build deterministic evidence/accounting JSON without central edits."""

    catalog = catalog or load_catalog()
    return {
        "audit_id": "plan2_start_turn_stamina_less_recover_native_audit",
        "scope": {
            "plan": "Plan2",
            "target_card": TARGET_CARD_ID,
            "target_card_display_label": TARGET_DISPLAY_LABEL,
            "target_card_master_name": TARGET_CARD_NAME,
            "target_upgrades": list(TARGET_UPGRADES),
            "bounded_standalone_whole_card": True,
            "central_coverage_rebuilt": False,
            "central_coverage_integrated": False,
            "central_core_modified": False,
            "native_search_modified": False,
            "plan3_modified": False,
            "gui_modified": False,
            "controller_modified": False,
            "formal_coverage_artifact_modified": False,
            "security_hash_clock_tamper_checks_run": False,
            "runtime_agent_calls": False,
            "excluded": [
                "plan2_core_runtime",
                "native_search",
                "formal coverage central/JSON",
                "Plan3",
                "GUI",
                "controller",
                "security/hash/clock/tamper work",
            ],
        },
        "summary": catalog.summary(),
        "master": {
            "database": "var/master.sqlite3",
            "trigger": None if catalog.trigger is None else catalog.trigger.to_dict(),
            "card_versions": [row.to_dict() for row in catalog.card_versions],
            "status_enchants": [row.to_dict() for row in catalog.status_enchants],
            "effect_shapes": [row.to_dict() for row in catalog.effect_shapes],
            "ordered_effect_claim": {
                str(upgrade): {
                    "top_level": list(TARGET_TOP_LEVEL_EFFECT_IDS_BY_UPGRADE[upgrade]),
                    "status_children": list(TARGET_CHILD_EFFECT_IDS_BY_UPGRADE[upgrade]),
                }
                for upgrade in TARGET_UPGRADES
            },
        },
        "native_semantics": NATIVE_EVIDENCE,
        "evidence": {
            "android_native": [ANDROID_V323_NATIVE_SOURCE],
            "android_il2cpp_metadata": [
                ANDROID_V323_METADATA_SOURCE,
                ANDROID_V323_TARGET_METADATA_SOURCE,
                ANDROID_V323_EXECUTOR_MAPPING_SOURCE,
            ],
            "android_instruction_order": [
                f"{ANDROID_V323_ISIL_SEQUENCE_SOURCE}:2713-2763",
                ANDROID_V323_STATUS_ORDER_SOURCE,
                ANDROID_V323_TURN_ORDER_SOURCE,
            ],
            "pc_il2cpp_metadata": [PC_METADATA_SOURCE, PC_COMPATIBILITY_SOURCE],
            "pc_native_limit": {
                "source": PC_NATIVE_SCAN_SOURCE,
                "authoritative": False,
                "candidates": [],
                "claim": "PC metadata/shape compatibility only; no PC native body parity is asserted",
            },
            "reused_plan2_proof": [
                "src/gkms_tool/plan2_start_turn_review_stamina_recover.py",
                "src/gkms_tool/plan2_stamina_recover_fix.py",
            ],
        },
        "execution_scope": {
            "this_module_models": [
                "StaminaLessMultiple StartTurn predicate",
                "ExamStaminaRecoverMultiple pure prediction",
                "four-version ordered card/status shape and accounting",
            ],
            "existing_executable_children": [
                "ExamLessonDependBlock",
                "ExamPlayableValueAdd",
            ],
            "this_module_does_not_execute": [
                "card payment",
                "card move",
                "central Plan2 state mutation",
                "agent/controller calls",
            ],
        },
        "verification": {
            "new_test_file": "tests/test_plan2_start_turn_stamina_less_recover.py",
            "central_coverage_updated": False,
            "full_pytest_run": False,
            "security_hash_clock_tamper_run": False,
        },
    }


def write_native_audit(
    path: Path | str = DEFAULT_AUDIT_PATH,
    catalog: StaminaLessRecoverCatalog | None = None,
) -> Path:
    """Write only this standalone audit artifact."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            build_native_audit(catalog),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


__all__ = [
    "ACTIVE_SKILL",
    "CardEffectSlot",
    "CardExecutionKind",
    "ContractResolution",
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_DATABASE",
    "EffectShape",
    "EvaluationBoundary",
    "EvaluationStage",
    "EXACT_TARGET_TRIGGER",
    "ExecutionMode",
    "FIELD_STAMINA_LESS_MULTIPLE",
    "LESSON_DEPEND_BLOCK_EFFECT_TYPE",
    "MULTIPLE_EFFECT_TYPE",
    "MultipleEffectContract",
    "NATIVE_EVIDENCE",
    "PHASE_EXAM_START_TURN",
    "SourceKind",
    "StaminaLessEvaluation",
    "StaminaLessEvaluationInput",
    "StaminaLessMultipleTrigger",
    "StaminaLessRecoverCatalog",
    "StaminaLessRecoverContractError",
    "StaminaRecoverMultipleEvaluation",
    "StaminaRecoverMultipleRuntime",
    "StartTurnSnapshot",
    "StartTurnSnapshotBoundary",
    "StatusEnchantRow",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_CARD_NAMES_BY_UPGRADE",
    "TARGET_CARD_STAMINA_BY_UPGRADE",
    "TARGET_CHILD_EFFECT_IDS_BY_UPGRADE",
    "TARGET_DISPLAY_LABEL",
    "TARGET_EFFECT_IDS_BY_UPGRADE",
    "TARGET_LESSON_EFFECT_IDS_BY_UPGRADE",
    "TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE",
    "TARGET_STATUS_ENCHANT_IDS_BY_UPGRADE",
    "TARGET_STATUS_WRAPPER_IDS_BY_UPGRADE",
    "TARGET_THRESHOLD",
    "TARGET_TOP_LEVEL_EFFECT_IDS_BY_UPGRADE",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "TargetCardVersion",
    "TurnKind",
    "build_native_audit",
    "evaluate_multiple",
    "evaluate_plan2_start_turn_stamina_less_multiple",
    "evaluate_stamina_less_multiple",
    "evaluate_stamina_less_trigger",
    "evaluate_stamina_recover_multiple",
    "evaluate_target_card_blockers",
    "load_catalog",
    "load_plan2_start_turn_stamina_less_recover_catalog",
    "resolve_stamina_less_trigger",
    "write_native_audit",
]
