"""Master-backed Plan2 scheduled gimmicks for Initial Regular stages.

The historical FKTN Final group remains pinned to its exact acceptance
program.  Other groups are admitted only when every schedule row belongs to a
compiled field/effect family (including the Final BlockUp/LessonValueMultiple
combination); display descriptions are retained as Master metadata but never
used to infer runtime behavior.

Android v3.2.3 establishes the execution boundary used here:

* ``ExamExtensions.IsFieldStatusTriggerStatusEffect`` (VA ``0x68082D4``)
  reads ``ExamStatusEffectCollection.GetReview`` for ReviewUp and applies the
  inclusive signed threshold predicate already audited by the Plan2 runtime.
* ``ExamSequence.<ExamLoopTaskAsync>d__94`` instructions 4073--4173 obtain
  the current-turn gimmick, evaluate ``IsEffectTriggerFieldValid``, and queue
  ``CreatePlayEffectCommand``.  Instructions 4211--4218 then advance from
  TurnStart (2) to TurnStartAfter (3); TurnStartDraw is phase 4 and queues
  ``CreateTurnDrawCommand`` before MainStart (5).

The hook therefore evaluates the settled TurnStart Review snapshot, then
uses the horizon's public external Review effect entry point.  That entry
point commits through the ordinary status-change listener/child pipeline.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import Final

import yaml

from .audition_rules import FINAL
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageKind,
    InitialRegularInnerStageRequest,
)
from .initial_regular_plan2_inner_adapter import (
    InitialRegularPlan2GimmickProvision,
)
from .plan2_exam_mode import (
    Plan2ScheduledGimmickHook,
    Plan2ScheduledGimmickHookKey,
    Plan2ScheduledGimmickHookResult,
    plan2_scheduled_gimmick_hook_key,
)
from .plan2_native_catalog_status_enchant import (
    Plan2NativeStatusEnchantError,
    Plan2StatusEnchantInstallProgram,
    install_plan2_native_status_enchant,
    load_plan2_native_status_enchant_installer_effect,
)
from .plan2_native_catalog_review_dynamic import (
    OP_INSTALL_LESSON_MULTIPLE,
    Plan2NativeReviewDynamicInstallInput,
    Plan2NativeReviewDynamicProgram,
    install_plan2_native_review_dynamic,
)
from .plan2_native_horizon import (
    _next_horizon_status_uid,
    Plan2NativeBlocker,
    Plan2NativeExternalAggressiveEffect,
    Plan2NativeExternalBlockEffect,
    Plan2NativeExternalReviewEffect,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    apply_plan2_native_external_aggressive_effect,
    apply_plan2_native_external_block_effect,
    apply_plan2_native_external_review_effect,
)
from .native_exam_formula import ceil_f32_to_i32, f32, permille_to_f32
from .plan2_native_stage_bootstrap import Plan2NativeStageGimmickFact


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GIMMICK_MASTER = (
    PROJECT_ROOT
    / "_research"
    / "gakumasu-diff"
    / "ProduceExamGimmickEffectGroup.yaml"
)
DEFAULT_MASTER_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"

FKTN_FINAL_GIMMICK_GROUP_ID: Final = (
    "p_exam_gimmick-produce-001-exam_review_01-audition-after_mid"
)
FKTN_AGGRESSIVE_FINAL_GIMMICK_GROUP_ID: Final = (
    "p_exam_gimmick-produce-001-"
    "exam_card_play_aggressive_03-audition-after_mid"
)
INITIAL_MASTER_REVIEW_FINAL_GIMMICK_GROUP_ID: Final = (
    "p_exam_gimmick-produce-003-exam_review_01-audition-after_mid"
)
NIA_FINAL_REVIEW_GIMMICK_GROUP_ID: Final = (
    "p_exam_gimmick-produce_004-3-1-exam_review_01"
)
EXAM_REVIEW_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamReview"
EXAM_BLOCK_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlock"
EXAM_REVIEW_VALUE_MULTIPLE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamReviewValueMultiple"
)
EXAM_AGGRESSIVE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamCardPlayAggressive"
)
EXAM_LESSON_VALUE_MULTIPLE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
EXAM_STATUS_ENCHANT_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamStatusEnchant"
)
FIELD_REVIEW_UP: Final = "ProduceExamFieldStatusType_ReviewUp"
FIELD_BLOCK_UP: Final = "ProduceExamFieldStatusType_BlockUp"
FIELD_AGGRESSIVE_UP: Final = (
    "ProduceExamFieldStatusType_CardPlayAggressiveUp"
)
FIELD_STATUS_UNKNOWN: Final = "ProduceExamFieldStatusType_Unknown"
FIELD_CHECK_NORMAL: Final = "ProduceExamTriggerCheckType_Unknown"

ANDROID_V323_EXAM_EXTENSIONS_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/ExamExtensions.txt"
)
ANDROID_V323_EXAM_SEQUENCE_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/Exam/"
    "ExamSequence_NestedType__ExamLoopTaskAsync_d__94.txt"
)
ANDROID_V323_DUMP_SOURCE: Final = (
    "_research/android/game-v3.2.3/il2cppdumper/dump.cs"
)

_EXPECTED_STEPS: Final = (
    (1, 4, 1, "e_effect-exam_review-0005", 5),
    (2, 8, 9, "e_effect-exam_review-0009", 9),
)
_EXPECTED_INITIAL_MASTER_REVIEW_STEPS: Final = (
    (
        1,
        3,
        0,
        "e_effect-exam_block-0003",
        3,
        EXAM_BLOCK_EFFECT_TYPE,
        FIELD_STATUS_UNKNOWN,
        "effect_group-visible-exam_block-000",
    ),
    (
        2,
        5,
        5,
        "e_effect-exam_review-0008",
        8,
        EXAM_REVIEW_EFFECT_TYPE,
        FIELD_REVIEW_UP,
        "effect_group-visible-exam_review-000",
    ),
    (
        3,
        8,
        16,
        "e_effect-exam_review-0011",
        11,
        EXAM_REVIEW_EFFECT_TYPE,
        FIELD_REVIEW_UP,
        "effect_group-visible-exam_review-000",
    ),
    (
        4,
        10,
        31,
        "e_effect-exam_review_value_multiple-0500",
        500,
        EXAM_REVIEW_VALUE_MULTIPLE_EFFECT_TYPE,
        FIELD_REVIEW_UP,
        "effect_group-visible-exam_review-000",
    ),
)
_EXPECTED_NIA_FINAL_REVIEW_STEPS: Final = (
    (
        1,
        1,
        0,
        "e_effect-exam_status_enchant-inf-enchant-"
        "p_exam_gimmick-produce_004-2-3-exam_review_01-enc01",
        0,
        EXAM_STATUS_ENCHANT_EFFECT_TYPE,
        FIELD_STATUS_UNKNOWN,
        "",
    ),
    (
        4,
        4,
        11,
        "e_effect-exam_review-0003",
        3,
        EXAM_REVIEW_EFFECT_TYPE,
        FIELD_REVIEW_UP,
        "effect_group-visible-exam_review-000",
    ),
    (
        7,
        7,
        22,
        "e_effect-exam_review_value_multiple-0300",
        300,
        EXAM_REVIEW_VALUE_MULTIPLE_EFFECT_TYPE,
        FIELD_REVIEW_UP,
        "effect_group-visible-exam_review-000",
    ),
)
_EXPECTED_AGGRESSIVE_STEPS: Final = (
    (1, 2, 0, "e_effect-exam_card_play_aggressive-0003", 3),
    (2, 5, 10, "e_effect-exam_card_play_aggressive-0009", 9),
)
_GROUP_FIELDS: Final = frozenset(
    {
        "id",
        "priority",
        "remainingTurnPermil",
        "startTurn",
        "remainingTurn",
        "fieldStatusType",
        "fieldStatusValue",
        "fieldStatusCheckType",
        "produceExamEffectId",
        "fieldStatusProduceCardSearchId",
        "isPositive",
        "produceDescriptions",
    }
)
_SUPPORTED_GIMMICK_FIELD_TYPES: Final = frozenset(
    {
        FIELD_STATUS_UNKNOWN,
        FIELD_REVIEW_UP,
        FIELD_BLOCK_UP,
        FIELD_AGGRESSIVE_UP,
    }
)
_EFFECT_FIELDS: Final = frozenset(
    {
        "id",
        "effectType",
        "effectValue1",
        "effectValue2",
        "effectCount",
        "effectTurn",
        "targetProduceCardId",
        "targetUpgradeCount",
        "targetExamEffectType",
        "produceCardSearchId",
        "movePositionType",
        "pickRangeType",
        "pickCountReferenceProduceCardSearchId",
        "pickCountType",
        "pickCountMin",
        "pickCountMax",
        "produceCardSearchId2",
        "pickRangeType2",
        "pickCountReferenceProduceCardSearchId2",
        "pickCountType2",
        "pickCountMin2",
        "pickCountMax2",
        "chainProduceExamEffectId",
        "chainProduceExamEffectIds",
        "produceExamStatusEnchantId",
        "produceCardStatusEnchantId",
        "produceCardGrowEffectIds",
        "effectGroupIds",
        "produceDescriptions",
        "customizeProduceDescriptions",
    }
)
_UNKNOWN_EFFECT_TYPE: Final = "ProduceExamEffectType_Unknown"
_UNKNOWN_MOVE_POSITION: Final = "ProduceCardMovePositionType_Unknown"
_UNKNOWN_PICK_RANGE: Final = "ProducePickRangeType_Unknown"
_UNKNOWN_PICK_COUNT: Final = "ProducePickCountType_Unknown"


def _block(code: str, detail: str = "") -> Plan2NativeBlocker:
    return Plan2NativeBlocker(code, detail)


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "text" if empty else "non-empty text"
        raise TypeError(f"{label} must be {qualifier}")
    return value


class InitialRegularPlan2GimmickRuntimeBlocked(RuntimeError):
    """Adapter boundary error retaining every typed gimmick blocker."""

    def __init__(self, blockers: Sequence[Plan2NativeBlocker]) -> None:
        resolved = tuple(blockers)
        if not resolved or any(
            not isinstance(value, Plan2NativeBlocker) for value in resolved
        ):
            raise TypeError("blockers must contain Plan2NativeBlocker values")
        self.blockers = resolved
        super().__init__(
            ";".join(
                value.code
                if not value.detail
                else f"{value.code}:{value.detail}"
                for value in resolved
            )
        )


@dataclass(frozen=True, slots=True)
class Plan2MasterGimmickEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    status_enchant_program: Plan2StatusEnchantInstallProgram | None = None
    review_dynamic_program: Plan2NativeReviewDynamicProgram | None = None

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _text(self.effect_type, "effect_type")
        for name in ("value1", "value2", "count"):
            _plain_int(getattr(self, name), name)
        _plain_int(self.turn, "turn", minimum=-1)
        program = self.status_enchant_program
        if self.effect_type == EXAM_STATUS_ENCHANT_EFFECT_TYPE:
            if not isinstance(program, Plan2StatusEnchantInstallProgram):
                raise TypeError(
                    "ExamStatusEnchant gimmick effect requires its exact program"
                )
            if (
                program.installer_effect_id != self.effect_id
                or program.turn != self.turn
            ):
                raise ValueError("status enchant program/effect mismatch")
        elif program is not None:
            raise ValueError("direct gimmick effect cannot carry a status program")
        dynamic = self.review_dynamic_program
        if self.effect_type == EXAM_LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
            if not isinstance(dynamic, Plan2NativeReviewDynamicProgram):
                raise TypeError(
                    "ExamLessonValueMultiple gimmick effect requires its exact program"
                )
            if (
                dynamic.effect_id != self.effect_id
                or dynamic.effect_type != self.effect_type
                or dynamic.value1 != self.value1
                or dynamic.value2 != self.value2
                or dynamic.effect_count != self.count
                or dynamic.effect_turn != self.turn
                or dynamic.operation_kind != OP_INSTALL_LESSON_MULTIPLE
            ):
                raise ValueError("lesson value multiple program/effect mismatch")
        elif dynamic is not None:
            raise ValueError(
                "direct gimmick effect cannot carry a review-dynamic program"
            )


@dataclass(frozen=True, slots=True)
class Plan2MasterGimmickStep:
    group_id: str
    priority: int
    start_turn: int
    review_threshold: int
    effect: Plan2MasterGimmickEffect
    field_status_type: str = FIELD_STATUS_UNKNOWN
    field_status_check_type: str = FIELD_CHECK_NORMAL

    def __post_init__(self) -> None:
        _text(self.group_id, "group_id")
        _plain_int(self.priority, "priority", minimum=1)
        _plain_int(self.start_turn, "start_turn", minimum=1)
        _plain_int(self.review_threshold, "review_threshold")
        if not isinstance(self.effect, Plan2MasterGimmickEffect):
            raise TypeError("effect must be Plan2MasterGimmickEffect")
        _text(self.field_status_type, "field_status_type")
        _text(self.field_status_check_type, "field_status_check_type")

    @property
    def field_status_value(self) -> int:
        """Serialized field predicate value (kept beside the legacy alias)."""

        return self.review_threshold


@dataclass(frozen=True, slots=True)
class Plan2MasterGimmickGroup:
    group_id: str
    steps: tuple[Plan2MasterGimmickStep, ...]

    def __post_init__(self) -> None:
        _text(self.group_id, "group_id")
        steps = tuple(self.steps)
        if not steps or any(
            not isinstance(value, Plan2MasterGimmickStep) for value in steps
        ):
            raise TypeError("steps must contain typed gimmick steps")
        if any(value.group_id != self.group_id for value in steps):
            raise ValueError("gimmick step group mismatch")
        order = tuple((value.priority, value.start_turn) for value in steps)
        if order != tuple(sorted(order)):
            raise ValueError("gimmick steps must preserve priority/native order")
        turns = tuple(value.start_turn for value in steps)
        if turns != tuple(sorted(turns)):
            raise ValueError("gimmick steps must preserve StartTurn order")
        if len({value.priority for value in steps}) != len(steps):
            raise ValueError("gimmick priorities must be unique")
        object.__setattr__(self, "steps", steps)


@dataclass(frozen=True, slots=True)
class Plan2MasterAggressiveGimmickStep:
    group_id: str
    priority: int
    start_turn: int
    aggressive_threshold: int
    effect: Plan2MasterGimmickEffect

    def __post_init__(self) -> None:
        _text(self.group_id, "group_id")
        _plain_int(self.priority, "priority", minimum=1)
        _plain_int(self.start_turn, "start_turn", minimum=1)
        _plain_int(self.aggressive_threshold, "aggressive_threshold")
        if not isinstance(self.effect, Plan2MasterGimmickEffect):
            raise TypeError("effect must be Plan2MasterGimmickEffect")


@dataclass(frozen=True, slots=True)
class Plan2MasterAggressiveGimmickGroup:
    group_id: str
    steps: tuple[Plan2MasterAggressiveGimmickStep, ...]

    def __post_init__(self) -> None:
        _text(self.group_id, "group_id")
        steps = tuple(self.steps)
        if not steps or any(
            not isinstance(value, Plan2MasterAggressiveGimmickStep)
            for value in steps
        ):
            raise TypeError("steps must contain typed aggressive gimmick steps")
        if any(value.group_id != self.group_id for value in steps):
            raise ValueError("aggressive gimmick step group mismatch")
        order = tuple((value.priority, value.start_turn) for value in steps)
        if order != tuple(sorted(order)):
            raise ValueError("aggressive gimmicks must preserve native order")
        if len({value.priority for value in steps}) != len(steps):
            raise ValueError("aggressive gimmick priorities must be unique")
        object.__setattr__(self, "steps", steps)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2GimmickRuntimeProvision:
    gimmicks: InitialRegularPlan2GimmickProvision | None = None
    blockers: tuple[Plan2NativeBlocker, ...] = ()

    def __post_init__(self) -> None:
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2NativeBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan2NativeBlocker values")
        if self.gimmicks is not None and not isinstance(
            self.gimmicks, InitialRegularPlan2GimmickProvision
        ):
            raise TypeError("gimmicks must be a typed provision or None")
        if (self.gimmicks is None) == (not blockers):
            raise ValueError("provision must contain gimmicks xor blockers")
        object.__setattr__(self, "blockers", blockers)

    @property
    def supported(self) -> bool:
        return self.gimmicks is not None and not self.blockers

    def require_gimmicks(self) -> InitialRegularPlan2GimmickProvision:
        if not self.supported:
            raise InitialRegularPlan2GimmickRuntimeBlocked(self.blockers)
        assert self.gimmicks is not None
        return self.gimmicks


@lru_cache(maxsize=4)
def _load_group_document(path_text: str) -> tuple[Mapping[str, object], ...]:
    with Path(path_text).open("r", encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, list) or any(
        not isinstance(value, Mapping) for value in raw
    ):
        raise ValueError("ProduceExamGimmickEffectGroup root must be a row list")
    return tuple(raw)


def _effect_raw_shape(
    raw: Mapping[str, object],
    *,
    effect_id: str,
    value1: int,
    value2: int = 0,
    effect_count: int = 0,
    effect_turn: int = 0,
    effect_type: str = EXAM_REVIEW_EFFECT_TYPE,
    visible_effect_group_id: str = "effect_group-visible-exam_review-000",
) -> bool:
    if set(raw) != _EFFECT_FIELDS:
        return False
    integer_fields = (
        "effectValue1",
        "effectValue2",
        "effectCount",
        "effectTurn",
        "targetUpgradeCount",
        "pickCountMin",
        "pickCountMax",
        "pickCountMin2",
        "pickCountMax2",
    )
    if any(
        isinstance(raw.get(key), bool) or not isinstance(raw.get(key), int)
        for key in integer_fields
    ):
        return False
    expected = {
        "id": effect_id,
        "effectType": effect_type,
        "effectValue1": value1,
        "effectValue2": value2,
        "effectCount": effect_count,
        "effectTurn": effect_turn,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": _UNKNOWN_EFFECT_TYPE,
        "produceCardSearchId": "",
        "movePositionType": _UNKNOWN_MOVE_POSITION,
        "pickRangeType": _UNKNOWN_PICK_RANGE,
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": _UNKNOWN_PICK_COUNT,
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": _UNKNOWN_PICK_RANGE,
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": _UNKNOWN_PICK_COUNT,
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": [visible_effect_group_id],
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        return False
    return isinstance(raw.get("produceDescriptions"), list) and isinstance(
        raw.get("customizeProduceDescriptions"), list
    )


def _load_effect(
    connection: sqlite3.Connection,
    *,
    effect_id: str,
    expected_value: int,
    expected_value2: int = 0,
    expected_effect_count: int = 0,
    expected_effect_turn: int = 0,
    expected_effect_type: str = EXAM_REVIEW_EFFECT_TYPE,
    visible_effect_group_id: str = "effect_group-visible-exam_review-000",
    dynamic_source_id: str = "gimmick-effect",
) -> Plan2MasterGimmickEffect:
    row = connection.execute(
        """
        SELECT id, effect_type, value1, value2, effect_count, effect_turn,
               status_enchant_id, chain_effect_id, raw_json
          FROM effect
         WHERE id = ?
        """,
        (effect_id,),
    ).fetchone()
    if row is None:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-effect-row-missing", effect_id),)
        )
    try:
        raw = json.loads(row[8])
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-effect-shape-unbound",
                    f"{effect_id}:raw-json:{type(error).__name__}",
                ),
            )
        ) from error
    normalized = (
        row[0],
        row[1],
        row[2],
        row[3],
        row[4],
        row[5],
        row[6],
        row[7],
    )
    expected = (
        effect_id,
        expected_effect_type,
        expected_value,
        expected_value2,
        expected_effect_count,
        expected_effect_turn,
        "",
        "",
    )
    if (
        normalized != expected
        or not isinstance(raw, Mapping)
        or not _effect_raw_shape(
            raw,
            effect_id=effect_id,
            value1=expected_value,
            value2=expected_value2,
            effect_count=expected_effect_count,
            effect_turn=expected_effect_turn,
            effect_type=expected_effect_type,
            visible_effect_group_id=visible_effect_group_id,
        )
    ):
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-effect-shape-unbound", effect_id),)
        )
    dynamic_program = None
    if expected_effect_type == EXAM_LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
        # The standalone Plan2 lesson-multiple module owns two card rows and
        # intentionally keeps that card catalog narrow.  A scheduled gimmick
        # has no card row, so retain the same native operation in the horizon
        # program while taking the effect ID/value/turn from this Master row.
        dynamic_program = Plan2NativeReviewDynamicProgram(
            card_id=dynamic_source_id,
            upgrade=0,
            slot_index=0,
            ordered_effect_ids=(effect_id,),
            effect_id=effect_id,
            effect_type=expected_effect_type,
            operation_kind=OP_INSTALL_LESSON_MULTIPLE,
            value1=expected_value,
            value2=expected_value2,
            effect_count=expected_effect_count,
            effect_turn=expected_effect_turn,
        )
    return Plan2MasterGimmickEffect(
        effect_id=effect_id,
        effect_type=expected_effect_type,
        value1=expected_value,
        value2=expected_value2,
        count=expected_effect_count,
        turn=expected_effect_turn,
        review_dynamic_program=dynamic_program,
    )


def _load_status_enchant_effect(
    connection: sqlite3.Connection,
    *,
    group_id: str,
    effect_id: str,
    expected_status_enchant_id: str,
    database: Path,
) -> Plan2MasterGimmickEffect:
    """Compile one gimmick-owned installer through the exact native catalog."""

    try:
        program = load_plan2_native_status_enchant_installer_effect(
            effect_id,
            source_id=group_id,
            database=database,
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-status-enchant-shape-unbound",
                    f"{effect_id}:{type(error).__name__}:{error}",
                ),
            )
        ) from error
    row = connection.execute(
        """
        SELECT effect_type, value1, value2, effect_count, effect_turn,
               status_enchant_id, chain_effect_id
          FROM effect
         WHERE id = ?
        """,
        (effect_id,),
    ).fetchone()
    expected = (
        EXAM_STATUS_ENCHANT_EFFECT_TYPE,
        0,
        0,
        0,
        -1,
        expected_status_enchant_id,
        "",
    )
    if row is None or tuple(row) != expected or (
        program.status_enchant_id != expected_status_enchant_id
        or program.turn != -1
        or program.total_limit != -1
        or program.per_turn_limit != -1
    ):
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-status-enchant-shape-unbound", effect_id),)
        )
    return Plan2MasterGimmickEffect(
        effect_id=effect_id,
        effect_type=EXAM_STATUS_ENCHANT_EFFECT_TYPE,
        value1=0,
        value2=0,
        count=0,
        turn=-1,
        status_enchant_program=program,
    )


def load_plan2_master_gimmick_group(
    group_id: str,
    *,
    master_path: Path = DEFAULT_GIMMICK_MASTER,
    database: Path = DEFAULT_MASTER_DATABASE,
) -> Plan2MasterGimmickGroup:
    """Load and strictly compile a native-proven Review-plan gimmick group."""

    _text(group_id, "group_id")
    if group_id == FKTN_FINAL_GIMMICK_GROUP_ID:
        expected_steps = tuple(
            (
                priority,
                turn,
                threshold,
                effect_id,
                effect_value,
                EXAM_REVIEW_EFFECT_TYPE,
                FIELD_REVIEW_UP,
                "effect_group-visible-exam_review-000",
            )
            for priority, turn, threshold, effect_id, effect_value in _EXPECTED_STEPS
        )
    elif group_id == INITIAL_MASTER_REVIEW_FINAL_GIMMICK_GROUP_ID:
        expected_steps = _EXPECTED_INITIAL_MASTER_REVIEW_STEPS
    elif group_id == NIA_FINAL_REVIEW_GIMMICK_GROUP_ID:
        expected_steps = _EXPECTED_NIA_FINAL_REVIEW_STEPS
    else:
        # Keep the historical pinned groups on their exact acceptance path,
        # but admit any other Master group whose rows are made entirely from
        # the generic field/effect families compiled below.  This is the
        # common path for newly observed Final gimmicks; group IDs are
        # evidence labels, not an implementation allowlist.
        try:
            return load_plan2_master_review_gimmick_group(
                group_id,
                master_path=Path(master_path),
                database=Path(database),
            )
        except InitialRegularPlan2GimmickRuntimeBlocked as error:
            if error.blockers and all(
                blocker.code == "plan2-gimmick-group-row-missing"
                for blocker in error.blockers
            ):
                raise InitialRegularPlan2GimmickRuntimeBlocked(
                    (_block("plan2-gimmick-group-unsupported", group_id),)
                ) from error
            raise
    master_path = Path(master_path)
    database = Path(database)
    if not master_path.is_file():
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-master-source-unavailable",
                    str(master_path),
                ),
            )
        )
    try:
        document = _load_group_document(str(master_path.resolve()))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-master-source-unavailable",
                    f"{master_path}:{type(error).__name__}:{error}",
                ),
            )
        ) from error
    rows = tuple(value for value in document if value.get("id") == group_id)
    if len(rows) != len(expected_steps):
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-group-row-cardinality-drift",
                    f"{group_id}:expected={len(expected_steps)}:actual={len(rows)}",
                ),
            )
        )

    row_shapes: list[tuple[int, int, int, str]] = []
    blockers: list[Plan2NativeBlocker] = []
    for index, (row, expected_step) in enumerate(
        zip(rows, expected_steps, strict=True)
    ):
        detail = f"{group_id}:row={index}"
        if set(row) != _GROUP_FIELDS:
            blockers.append(
                _block("plan2-gimmick-group-field-shape-unbound", detail)
            )
            continue
        try:
            priority = _plain_int(row["priority"], "priority", minimum=1)
            start_turn = _plain_int(row["startTurn"], "startTurn", minimum=1)
            remaining_turn_permille = _plain_int(
                row["remainingTurnPermil"],
                "remainingTurnPermil",
            )
            remaining_turn = _plain_int(
                row["remainingTurn"],
                "remainingTurn",
            )
            threshold = _plain_int(
                row["fieldStatusValue"],
                "fieldStatusValue",
            )
            effect_id = _text(row["produceExamEffectId"], "produceExamEffectId")
        except (TypeError, ValueError) as error:
            blockers.append(
                _block(
                    "plan2-gimmick-group-field-shape-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
            )
            continue
        expected_field = expected_step[6]
        neutral = (
            remaining_turn_permille == 0
            and remaining_turn == 0
            and row["fieldStatusType"] == expected_field
            and row["fieldStatusCheckType"] == FIELD_CHECK_NORMAL
            and row["fieldStatusProduceCardSearchId"] == ""
            and row["isPositive"] is True
            and isinstance(row["produceDescriptions"], list)
        )
        if not neutral:
            blockers.append(
                _block("plan2-gimmick-group-field-shape-unbound", detail)
            )
            continue
        row_shapes.append((priority, start_turn, threshold, effect_id))
    expected_row_shapes = tuple(value[:4] for value in expected_steps)
    if tuple(row_shapes) != expected_row_shapes:
        blockers.append(
            _block(
                "plan2-gimmick-group-native-order-or-shape-drift",
                group_id,
            )
        )
    if blockers:
        raise InitialRegularPlan2GimmickRuntimeBlocked(tuple(blockers))

    if not database.is_file():
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-effect-source-unavailable",
                    str(database),
                ),
            )
        )
    try:
        connection = sqlite3.connect(str(database))
        try:
            loaded_effects: list[Plan2MasterGimmickEffect] = []
            for (
                _priority,
                _turn,
                _threshold,
                effect_id,
                effect_value,
                effect_type,
                _field_type,
                visible_effect_group_id,
            ) in expected_steps:
                if effect_type == EXAM_STATUS_ENCHANT_EFFECT_TYPE:
                    loaded_effects.append(
                        _load_status_enchant_effect(
                            connection,
                            group_id=group_id,
                            effect_id=effect_id,
                            expected_status_enchant_id=(
                                "enchant-p_exam_gimmick-"
                                "produce_004-2-3-exam_review_01-enc01"
                            ),
                            database=database,
                        )
                    )
                else:
                    loaded_effects.append(
                        _load_effect(
                            connection,
                            effect_id=effect_id,
                            expected_value=effect_value,
                            expected_effect_type=effect_type,
                            visible_effect_group_id=visible_effect_group_id,
                        )
                    )
            effects = tuple(loaded_effects)
        finally:
            connection.close()
    except InitialRegularPlan2GimmickRuntimeBlocked:
        raise
    except (OSError, sqlite3.Error) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-effect-source-unavailable",
                    f"{database}:{type(error).__name__}:{error}",
                ),
            )
        ) from error

    return Plan2MasterGimmickGroup(
        group_id,
        tuple(
            Plan2MasterGimmickStep(
                group_id=group_id,
                priority=priority,
                start_turn=start_turn,
                review_threshold=threshold,
                effect=effect,
                field_status_type=_field_type,
                field_status_check_type=FIELD_CHECK_NORMAL,
            )
            for (
                priority,
                start_turn,
                threshold,
                _effect_id,
                _effect_value,
                _effect_type,
                _field_type,
                _visible_effect_group_id,
            ), effect in zip(expected_steps, effects, strict=True)
        ),
    )


def load_plan2_master_gimmick_status_enchant_step(
    group_id: str,
    priority: int,
    start_turn: int,
    effect_id: str,
    *,
    master_path: Path = DEFAULT_GIMMICK_MASTER,
    database: Path = DEFAULT_MASTER_DATABASE,
) -> Plan2StatusEnchantInstallProgram:
    """Compile one native gimmick ``ExamStatusEnchant`` row by identity.

    A gimmick group can contain direct effects that are outside the current
    simulator slice.  Requiring the whole group to compile therefore blocks
    an otherwise exact active status-enchant listener.  This entry point
    validates only the captured ``group_id``/priority/start-turn/effect ID
    row, then reuses the status/trigger/child graph compiler.  Sibling rows
    are intentionally not inspected or treated as evidence for this row.
    """

    _text(group_id, "group_id")
    _plain_int(priority, "priority", minimum=1)
    _plain_int(start_turn, "start_turn", minimum=1)
    _text(effect_id, "effect_id")
    master_path = Path(master_path)
    database = Path(database)
    if not master_path.is_file() or not database.is_file():
        missing = master_path if not master_path.is_file() else database
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-master-source-unavailable", str(missing)),)
        )
    try:
        document = _load_group_document(str(master_path.resolve()))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-master-source-unavailable",
                    f"{master_path}:{type(error).__name__}:{error}",
                ),
            )
        ) from error

    rows = tuple(value for value in document if value.get("id") == group_id)
    if not rows:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-group-row-missing", group_id),)
        )
    matches = tuple(
        row
        for row in rows
        if (
            row.get("priority") == priority
            and row.get("startTurn") == start_turn
            and row.get("produceExamEffectId") == effect_id
        )
    )
    if len(matches) != 1:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-status-step-not-unique",
                    f"{group_id}:priority={priority}:start={start_turn}:"
                    f"effect={effect_id}:count={len(matches)}",
                ),
            )
        )
    row = matches[0]
    detail = f"{group_id}:priority={priority}:start={start_turn}:effect={effect_id}"
    if set(row) != _GROUP_FIELDS:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-group-field-shape-unbound", detail),)
        )
    try:
        row_priority = _plain_int(row["priority"], "priority", minimum=1)
        row_start_turn = _plain_int(row["startTurn"], "startTurn", minimum=1)
        row_field_value = _plain_int(row["fieldStatusValue"], "fieldStatusValue")
    except (TypeError, ValueError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-group-field-shape-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                ),
            )
        ) from error
    # The group row is an installer identity envelope, not the status
    # enchant's runtime trigger.  Keep its serialized field predicate intact
    # without assuming Review: Master currently contains aggressive,
    # stamina, lesson-buff, and other field families alongside Review.
    valid_field_type = isinstance(row["fieldStatusType"], str) and bool(
        row["fieldStatusType"]
    )
    valid_check_type = row["fieldStatusCheckType"] in {
        FIELD_CHECK_NORMAL,
        "ProduceExamTriggerCheckType_Not",
    }
    valid_shape = (
        row_priority == priority
        and row_start_turn == start_turn
        and row["produceExamEffectId"] == effect_id
        and row["remainingTurnPermil"] == 0
        and row["remainingTurn"] == 0
        and valid_field_type
        and valid_check_type
        and row["fieldStatusProduceCardSearchId"] == ""
        and row["isPositive"] is True
        and isinstance(row["produceDescriptions"], list)
    )
    if not valid_shape:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-group-field-shape-unbound", detail),)
        )
    try:
        return load_plan2_native_status_enchant_installer_effect(
            effect_id,
            source_id=group_id,
            database=database,
        )
    except (
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        Plan2NativeStatusEnchantError,
    ) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-status-enchant-shape-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                ),
            )
        ) from error


def load_plan2_master_review_gimmick_group(
    group_id: str,
    *,
    master_path: Path = DEFAULT_GIMMICK_MASTER,
    database: Path = DEFAULT_MASTER_DATABASE,
) -> Plan2MasterGimmickGroup:
    """Compile any Master-backed Review-gated Plan2 gimmick group.

    Unlike the historical acceptance loader above, this path has no group-ID
    allowlist.  It admits only the already-owned direct field/effect families
    and validates every Master row before producing hooks.  In particular,
    ``BlockUp`` predicates and plain ``LessonValueMultiple`` installers share
    this path with the original Review/Block rows.
    """

    _text(group_id, "group_id")
    master_path = Path(master_path)
    database = Path(database)
    if not master_path.is_file() or not database.is_file():
        missing = master_path if not master_path.is_file() else database
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-master-source-unavailable", str(missing)),)
        )
    try:
        document = _load_group_document(str(master_path.resolve()))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-master-source-unavailable",
                    f"{master_path}:{type(error).__name__}:{error}",
                ),
            )
        ) from error
    rows = tuple(value for value in document if value.get("id") == group_id)
    if not rows:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-group-row-missing", group_id),)
        )
    try:
        rows = tuple(
            sorted(
                rows,
                key=lambda value: _plain_int(
                    value.get("priority"),
                    "priority",
                    minimum=1,
                ),
            )
        )
    except (TypeError, ValueError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-group-field-shape-unbound",
                    f"{group_id}:priority:{type(error).__name__}:{error}",
                ),
            )
        ) from error

    normalized: list[tuple[int, int, int, str, str, str]] = []
    blockers: list[Plan2NativeBlocker] = []
    for index, row in enumerate(rows):
        detail = f"{group_id}:row={index}"
        if set(row) != _GROUP_FIELDS:
            blockers.append(
                _block("plan2-gimmick-group-field-shape-unbound", detail)
            )
            continue
        try:
            priority = _plain_int(row["priority"], "priority", minimum=1)
            start_turn = _plain_int(row["startTurn"], "startTurn", minimum=1)
            remaining_turn_permille = _plain_int(
                row["remainingTurnPermil"],
                "remainingTurnPermil",
            )
            remaining_turn = _plain_int(
                row["remainingTurn"],
                "remainingTurn",
            )
            threshold = _plain_int(row["fieldStatusValue"], "fieldStatusValue")
            effect_id = _text(row["produceExamEffectId"], "produceExamEffectId")
        except (TypeError, ValueError) as error:
            blockers.append(
                _block(
                    "plan2-gimmick-group-field-shape-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
            )
            continue
        field_type = row["fieldStatusType"]
        field_check_type = row["fieldStatusCheckType"]
        neutral = bool(
            remaining_turn_permille == 0
            and remaining_turn == 0
            and isinstance(field_type, str)
            and field_type in _SUPPORTED_GIMMICK_FIELD_TYPES
            and (field_type != FIELD_STATUS_UNKNOWN or threshold == 0)
            and field_check_type == FIELD_CHECK_NORMAL
            and row["fieldStatusProduceCardSearchId"] == ""
            and row["isPositive"] is True
            and isinstance(row["produceDescriptions"], list)
        )
        if not neutral:
            blockers.append(
                _block("plan2-gimmick-group-field-shape-unbound", detail)
            )
            continue
        normalized.append(
            (
                priority,
                start_turn,
                threshold,
                effect_id,
                field_type,
                field_check_type,
            )
        )
    if blockers:
        raise InitialRegularPlan2GimmickRuntimeBlocked(tuple(blockers))
    priorities = tuple(value[0] for value in normalized)
    start_turns = tuple(value[1] for value in normalized)
    if (
        len(priorities) != len(set(priorities))
        or start_turns != tuple(sorted(start_turns))
    ):
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-group-native-order-or-shape-drift",
                    group_id,
                ),
            )
        )

    effects: list[Plan2MasterGimmickEffect] = []
    try:
        connection = sqlite3.connect(str(database))
        try:
            for (
                _priority,
                _turn,
                _threshold,
                effect_id,
                _field_type,
                _field_check_type,
            ) in normalized:
                row = connection.execute(
                    """
                    SELECT effect_type, value1, value2, effect_count,
                           effect_turn, status_enchant_id, chain_effect_id,
                           raw_json
                      FROM effect
                     WHERE id = ?
                    """,
                    (effect_id,),
                ).fetchone()
                if row is None:
                    raise InitialRegularPlan2GimmickRuntimeBlocked(
                        (_block("plan2-gimmick-effect-row-missing", effect_id),)
                    )
                effect_type = str(row[0])
                value1 = int(row[1])
                if effect_type == EXAM_STATUS_ENCHANT_EFFECT_TYPE:
                    status_id = str(row[5])
                    effects.append(
                        _load_status_enchant_effect(
                            connection,
                            group_id=group_id,
                            effect_id=effect_id,
                            expected_status_enchant_id=status_id,
                            database=database,
                        )
                    )
                    continue
                if effect_type not in {
                    EXAM_REVIEW_EFFECT_TYPE,
                    EXAM_BLOCK_EFFECT_TYPE,
                    EXAM_AGGRESSIVE_EFFECT_TYPE,
                    EXAM_REVIEW_VALUE_MULTIPLE_EFFECT_TYPE,
                    EXAM_LESSON_VALUE_MULTIPLE_EFFECT_TYPE,
                }:
                    raise InitialRegularPlan2GimmickRuntimeBlocked(
                        (
                            _block(
                                "plan2-gimmick-effect-type-unbound",
                                f"{effect_id}:{effect_type}",
                            ),
                        )
                    )
                try:
                    raw = json.loads(str(row[7]))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise InitialRegularPlan2GimmickRuntimeBlocked(
                        (_block("plan2-gimmick-effect-shape-unbound", effect_id),)
                    ) from error
                effect_turn = row[4]
                if effect_type == EXAM_LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
                    if (
                        isinstance(effect_turn, bool)
                        or not isinstance(effect_turn, int)
                        or (effect_turn != -1 and effect_turn <= 0)
                    ):
                        raise InitialRegularPlan2GimmickRuntimeBlocked(
                            (_block("plan2-gimmick-effect-shape-unbound", effect_id),)
                        )
                else:
                    effect_turn = 0
                groups = raw.get("effectGroupIds") if isinstance(raw, Mapping) else None
                if (
                    not isinstance(groups, list)
                    or len(groups) != 1
                    or not isinstance(groups[0], str)
                    or not groups[0]
                ):
                    raise InitialRegularPlan2GimmickRuntimeBlocked(
                        (_block("plan2-gimmick-effect-shape-unbound", effect_id),)
                    )
                effects.append(
                    _load_effect(
                        connection,
                        effect_id=effect_id,
                        expected_value=value1,
                        expected_effect_turn=effect_turn,
                        expected_effect_type=effect_type,
                        visible_effect_group_id=groups[0],
                        dynamic_source_id=group_id,
                    )
                )
        finally:
            connection.close()
    except InitialRegularPlan2GimmickRuntimeBlocked:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-effect-source-unavailable",
                    f"{database}:{type(error).__name__}:{error}",
                ),
            )
        ) from error

    try:
        return Plan2MasterGimmickGroup(
            group_id,
            tuple(
                Plan2MasterGimmickStep(
                    group_id,
                    priority,
                    start_turn,
                    threshold,
                    effect,
                    field_status_type=field_type,
                    field_status_check_type=field_check_type,
                )
                for (
                    priority,
                    start_turn,
                    threshold,
                    _effect_id,
                    field_type,
                    field_check_type,
                ), effect in zip(
                    normalized,
                    effects,
                    strict=True,
                )
            ),
        )
    except (TypeError, ValueError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-group-native-order-or-shape-drift",
                    f"{group_id}:{type(error).__name__}:{error}",
                ),
            )
        ) from error


def build_plan2_master_review_gimmick_hooks(
    group: Plan2MasterGimmickGroup,
) -> Mapping[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook]:
    if not isinstance(group, Plan2MasterGimmickGroup):
        raise TypeError("group must be Plan2MasterGimmickGroup")
    return {
        plan2_scheduled_gimmick_hook_key(
            step.start_turn,
            step.group_id,
            step.effect.effect_id,
        ): _hook_for_step(step)
        for step in group.steps
    }


def load_plan2_master_aggressive_gimmick_group(
    group_id: str,
    *,
    master_path: Path = DEFAULT_GIMMICK_MASTER,
    database: Path = DEFAULT_MASTER_DATABASE,
) -> Plan2MasterAggressiveGimmickGroup:
    """Load the exact FKTN Final Aggressive start-turn gimmicks."""

    _text(group_id, "group_id")
    if group_id != FKTN_AGGRESSIVE_FINAL_GIMMICK_GROUP_ID:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-group-unsupported", group_id),)
        )
    master_path = Path(master_path)
    database = Path(database)
    if not master_path.is_file():
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-master-source-unavailable",
                    str(master_path),
                ),
            )
        )
    try:
        document = _load_group_document(str(master_path.resolve()))
    except (OSError, TypeError, ValueError, yaml.YAMLError) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-master-source-unavailable",
                    f"{master_path}:{type(error).__name__}:{error}",
                ),
            )
        ) from error
    rows = tuple(value for value in document if value.get("id") == group_id)
    if len(rows) != len(_EXPECTED_AGGRESSIVE_STEPS):
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-group-row-cardinality-drift",
                    f"{group_id}:expected=2:actual={len(rows)}",
                ),
            )
        )

    row_shapes: list[tuple[int, int, int, str]] = []
    blockers: list[Plan2NativeBlocker] = []
    for index, row in enumerate(rows):
        detail = f"{group_id}:row={index}"
        if set(row) != _GROUP_FIELDS:
            blockers.append(
                _block("plan2-gimmick-group-field-shape-unbound", detail)
            )
            continue
        try:
            priority = _plain_int(row["priority"], "priority", minimum=1)
            start_turn = _plain_int(row["startTurn"], "startTurn", minimum=1)
            remaining_turn_permille = _plain_int(
                row["remainingTurnPermil"],
                "remainingTurnPermil",
            )
            remaining_turn = _plain_int(row["remainingTurn"], "remainingTurn")
            threshold = _plain_int(
                row["fieldStatusValue"],
                "fieldStatusValue",
            )
            effect_id = _text(row["produceExamEffectId"], "produceExamEffectId")
        except (TypeError, ValueError) as error:
            blockers.append(
                _block(
                    "plan2-gimmick-group-field-shape-unbound",
                    f"{detail}:{type(error).__name__}:{error}",
                )
            )
            continue
        expected_field = (
            FIELD_STATUS_UNKNOWN if threshold == 0 else FIELD_AGGRESSIVE_UP
        )
        neutral = (
            remaining_turn_permille == 0
            and remaining_turn == 0
            and row["fieldStatusType"] == expected_field
            and row["fieldStatusCheckType"] == FIELD_CHECK_NORMAL
            and row["fieldStatusProduceCardSearchId"] == ""
            and row["isPositive"] is True
            and isinstance(row["produceDescriptions"], list)
        )
        if not neutral:
            blockers.append(
                _block("plan2-gimmick-group-field-shape-unbound", detail)
            )
            continue
        row_shapes.append((priority, start_turn, threshold, effect_id))
    expected_shapes = tuple(value[:4] for value in _EXPECTED_AGGRESSIVE_STEPS)
    if tuple(row_shapes) != expected_shapes:
        blockers.append(
            _block(
                "plan2-gimmick-group-native-order-or-shape-drift",
                group_id,
            )
        )
    if blockers:
        raise InitialRegularPlan2GimmickRuntimeBlocked(tuple(blockers))

    if not database.is_file():
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-effect-source-unavailable", str(database)),)
        )
    try:
        connection = sqlite3.connect(str(database))
        try:
            effects = tuple(
                _load_effect(
                    connection,
                    effect_id=effect_id,
                    expected_value=effect_value,
                    expected_effect_type=EXAM_AGGRESSIVE_EFFECT_TYPE,
                    visible_effect_group_id=(
                        "effect_group-visible-exam_card_play_aggressive-000"
                    ),
                )
                for _priority, _turn, _threshold, effect_id, effect_value
                in _EXPECTED_AGGRESSIVE_STEPS
            )
        finally:
            connection.close()
    except InitialRegularPlan2GimmickRuntimeBlocked:
        raise
    except (OSError, sqlite3.Error) as error:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-effect-source-unavailable",
                    f"{database}:{type(error).__name__}:{error}",
                ),
            )
        ) from error

    return Plan2MasterAggressiveGimmickGroup(
        group_id,
        tuple(
            Plan2MasterAggressiveGimmickStep(
                group_id=group_id,
                priority=priority,
                start_turn=start_turn,
                aggressive_threshold=threshold,
                effect=effect,
            )
            for (
                priority,
                start_turn,
                threshold,
                _effect_id,
                _effect_value,
            ), effect in zip(_EXPECTED_AGGRESSIVE_STEPS, effects, strict=True)
        ),
    )


def _hook_for_step(step: Plan2MasterGimmickStep) -> Plan2ScheduledGimmickHook:
    def execute(
        state: object,
        catalog: object,
    ) -> Plan2ScheduledGimmickHookResult:
        if not isinstance(state, Plan2NativeHorizonState):
            raise TypeError("gimmick state must be Plan2NativeHorizonState")
        if not isinstance(catalog, Plan2NativeProgramCatalog):
            raise TypeError("gimmick catalog must be Plan2NativeProgramCatalog")
        if state.scalar.current_turn != step.start_turn:
            raise ValueError(
                "gimmick StartTurn mismatch:"
                f"expected={step.start_turn}:actual={state.scalar.current_turn}"
            )
        field_type = step.field_status_type
        threshold = step.field_status_value
        if step.field_status_check_type != FIELD_CHECK_NORMAL:
            raise ValueError(
                "unsupported gimmick field-status check:"
                f"{step.field_status_check_type}"
            )
        if field_type == FIELD_STATUS_UNKNOWN:
            field_value = 0
            fires = threshold == 0
            trace_name = "gimmick-review-gate"
            trace_field = "review"
        elif field_type == FIELD_REVIEW_UP:
            field_value = state.scalar.review
            fires = field_value >= threshold
            trace_name = "gimmick-review-gate"
            trace_field = "review"
        elif field_type == FIELD_BLOCK_UP:
            field_value = state.scalar.block
            fires = field_value >= threshold
            trace_name = "gimmick-block-gate"
            trace_field = "block"
        elif field_type == FIELD_AGGRESSIVE_UP:
            field_value = state.scalar.card_play_aggressive
            fires = field_value >= threshold
            trace_name = "gimmick-aggressive-gate"
            trace_field = "aggressive"
        else:
            raise ValueError(f"unsupported gimmick field status:{field_type}")
        condition_trace = (
            f"{trace_name}:{step.effect.effect_id}:"
            f"{trace_field}={field_value}:"
            f"threshold={threshold}:fires={str(fires).lower()}"
        )
        if not fires:
            return Plan2ScheduledGimmickHookResult(state, (condition_trace,))
        source_id = f"gimmick:{step.group_id}:priority={step.priority}"
        same_group_status_uids = tuple(
            listener.status_uid
            for listener in state.status_enchant.listeners
            if listener.source_card_id == step.group_id
        )
        if step.effect.effect_type == EXAM_REVIEW_EFFECT_TYPE:
            applied = apply_plan2_native_external_review_effect(
                state,
                catalog,
                Plan2NativeExternalReviewEffect(
                    source_id=source_id,
                    effect_id=step.effect.effect_id,
                    value=step.effect.value1,
                    sequence=step.priority,
                    suppressed_status_uids=same_group_status_uids,
                ),
            )
        elif step.effect.effect_type == EXAM_BLOCK_EFFECT_TYPE:
            applied = apply_plan2_native_external_block_effect(
                state,
                catalog,
                Plan2NativeExternalBlockEffect(
                    source_id=source_id,
                    effect_id=step.effect.effect_id,
                    value=step.effect.value1,
                    sequence=step.priority,
                ),
            )
        elif step.effect.effect_type == EXAM_AGGRESSIVE_EFFECT_TYPE:
            applied = apply_plan2_native_external_aggressive_effect(
                state,
                catalog,
                Plan2NativeExternalAggressiveEffect(
                    source_id=source_id,
                    effect_id=step.effect.effect_id,
                    value=step.effect.value1,
                    sequence=step.priority,
                ),
            )
        elif step.effect.effect_type == EXAM_REVIEW_VALUE_MULTIPLE_EFFECT_TYPE:
            delta = ceil_f32_to_i32(
                f32(
                    f32(state.scalar.review)
                    * permille_to_f32(step.effect.value1)
                )
            )
            if delta == 0:
                return Plan2ScheduledGimmickHookResult(
                    state,
                    (condition_trace, "gimmick-review-value-multiple:zero-review"),
                )
            applied = apply_plan2_native_external_review_effect(
                state,
                catalog,
                Plan2NativeExternalReviewEffect(
                    source_id=source_id,
                    effect_id=step.effect.effect_id,
                    value=delta,
                    sequence=step.priority,
                    apply_review_additive=False,
                    suppressed_status_uids=same_group_status_uids,
                ),
            )
        elif step.effect.effect_type == EXAM_LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
            program = step.effect.review_dynamic_program
            assert isinstance(program, Plan2NativeReviewDynamicProgram)
            next_uid = _next_horizon_status_uid(state)
            install_runtime = replace(
                state.review_dynamic,
                next_status_uid=next_uid,
            )
            installed = install_plan2_native_review_dynamic(
                install_runtime,
                program,
                Plan2NativeReviewDynamicInstallInput(
                    source_guid=source_id,
                    completed_effect_ids=(),
                ),
            )
            if not installed.executable or not installed.installed:
                detail = ",".join(installed.unresolved) or "not-installed"
                raise ValueError(
                    "gimmick-lesson-value-multiple-install-failed-closed:"
                    f"{step.effect.effect_id}:{detail}"
                )
            after = replace(
                state,
                scalar=replace(
                    state.scalar,
                    next_status_uid=max(
                        state.scalar.next_status_uid,
                        installed.after.next_status_uid,
                    ),
                ),
                review_dynamic=installed.after,
                triggered_review_status_runtime=replace(
                    state.triggered_review_status_runtime,
                    review_runtime=installed.after,
                ),
            )
            return Plan2ScheduledGimmickHookResult(
                after,
                (
                    condition_trace,
                    "gimmick-lesson-value-multiple-installed:"
                    f"{step.effect.effect_id}",
                    *installed.trace,
                ),
            )
        elif step.effect.effect_type == EXAM_STATUS_ENCHANT_EFFECT_TYPE:
            program = step.effect.status_enchant_program
            assert isinstance(program, Plan2StatusEnchantInstallProgram)
            next_uid = _next_horizon_status_uid(state)
            install_runtime = replace(
                state.status_enchant,
                next_status_uid=next_uid,
            )
            installed = install_plan2_native_status_enchant(
                install_runtime,
                program,
                source_guid=source_id,
                completed_effect_ids=(),
            )
            if not installed.executable or not installed.installed:
                detail = ",".join(installed.unresolved) or "not-installed"
                raise ValueError(
                    "gimmick-status-enchant-install-failed-closed:"
                    f"{step.effect.effect_id}:{detail}"
                )
            assert installed.listener is not None
            installed_runtime = replace(
                installed.after,
                listeners=tuple(
                    replace(value, passing_turn_start=True)
                    if value.status_uid == installed.listener.status_uid
                    else value
                    for value in installed.after.listeners
                ),
            )
            after = replace(
                state,
                scalar=replace(
                    state.scalar,
                    next_status_uid=max(
                        state.scalar.next_status_uid,
                        installed_runtime.next_status_uid,
                    ),
                ),
                status_enchant=installed_runtime,
            )
            return Plan2ScheduledGimmickHookResult(
                after,
                (
                    condition_trace,
                    "gimmick-status-enchant-installed:"
                    f"uid={installed.listener.status_uid}:"
                    f"{step.effect.effect_id}",
                    *installed.trace,
                ),
            )
        else:
            raise ValueError(
                f"unsupported compiled gimmick effect:{step.effect.effect_type}"
            )
        return Plan2ScheduledGimmickHookResult(
            applied.state,
            (condition_trace, *applied.trace),
        )

    return Plan2ScheduledGimmickHook(
        effect_id=step.effect.effect_id,
        executor_id=(
            "initial-regular.plan2.final-gimmick."
            f"priority-{step.priority}.{step.effect.effect_id}.v1"
        ),
        executor=execute,
    )


def _hook_for_aggressive_step(
    step: Plan2MasterAggressiveGimmickStep,
) -> Plan2ScheduledGimmickHook:
    def execute(
        state: object,
        catalog: object,
    ) -> Plan2ScheduledGimmickHookResult:
        if not isinstance(state, Plan2NativeHorizonState):
            raise TypeError("gimmick state must be Plan2NativeHorizonState")
        if not isinstance(catalog, Plan2NativeProgramCatalog):
            raise TypeError("gimmick catalog must be Plan2NativeProgramCatalog")
        if state.scalar.current_turn != step.start_turn:
            raise ValueError(
                "gimmick StartTurn mismatch:"
                f"expected={step.start_turn}:actual={state.scalar.current_turn}"
            )
        aggressive = state.scalar.card_play_aggressive
        fires = (
            step.aggressive_threshold == 0
            or aggressive >= step.aggressive_threshold
        )
        condition_trace = (
            "gimmick-aggressive-gate:"
            f"{step.effect.effect_id}:aggressive={aggressive}:"
            f"threshold={step.aggressive_threshold}:"
            f"fires={str(fires).lower()}"
        )
        if not fires:
            return Plan2ScheduledGimmickHookResult(state, (condition_trace,))
        applied = apply_plan2_native_external_aggressive_effect(
            state,
            catalog,
            Plan2NativeExternalAggressiveEffect(
                source_id=(
                    f"gimmick:{step.group_id}:priority={step.priority}"
                ),
                effect_id=step.effect.effect_id,
                value=step.effect.value1,
                sequence=step.priority,
            ),
        )
        return Plan2ScheduledGimmickHookResult(
            applied.state,
            (condition_trace, *applied.trace),
        )

    return Plan2ScheduledGimmickHook(
        effect_id=step.effect.effect_id,
        executor_id=(
            "initial-regular.plan2.final-aggressive-gimmick."
            f"priority-{step.priority}.{step.effect.effect_id}.v1"
        ),
        executor=execute,
    )


def build_plan2_audition_gimmick_hooks(
    raw_schedule: object,
    *,
    master_path: Path = DEFAULT_GIMMICK_MASTER,
    database: Path = DEFAULT_MASTER_DATABASE,
) -> Mapping[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook]:
    """Bind any audition's native schedule to exact Master-family hooks.

    Production ExamSaveData already carries the authoritative turn, group and
    effect IDs.  This bridge validates the complete group through the pinned
    direct/status-enchant families and never infers a hook from an effect ID
    alone.
    """

    if not isinstance(raw_schedule, list):
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-schedule-invalid", "not-list"),)
        )
    if not raw_schedule:
        return {}
    rows: list[tuple[int, str, str]] = []
    for index, raw in enumerate(raw_schedule):
        if not isinstance(raw, Mapping) or set(raw) != {
            "turn",
            "gimmickGroupId",
            "gimmickEffectId",
        }:
            raise InitialRegularPlan2GimmickRuntimeBlocked(
                (_block("plan2-gimmick-schedule-row-invalid", str(index)),)
            )
        turn = raw["turn"]
        group_id = raw["gimmickGroupId"]
        effect_id = raw["gimmickEffectId"]
        if (
            isinstance(turn, bool)
            or not isinstance(turn, int)
            or turn < 1
            or not isinstance(group_id, str)
            or not group_id
            or not isinstance(effect_id, str)
            or not effect_id
        ):
            raise InitialRegularPlan2GimmickRuntimeBlocked(
                (_block("plan2-gimmick-schedule-row-invalid", str(index)),)
            )
        rows.append((turn, group_id, effect_id))
    group_ids = {value[1] for value in rows}
    if len(group_ids) != 1:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (_block("plan2-gimmick-schedule-group-ambiguous", repr(sorted(group_ids))),)
        )
    group_id = next(iter(group_ids), "")
    group = load_plan2_master_gimmick_group(
        group_id,
        master_path=Path(master_path),
        database=Path(database),
    )
    expected = tuple(
        (value.start_turn, value.group_id, value.effect.effect_id)
        for value in group.steps
    )
    if tuple(rows) != expected:
        raise InitialRegularPlan2GimmickRuntimeBlocked(
            (
                _block(
                    "plan2-gimmick-schedule-master-mismatch",
                    f"observed={rows!r};expected={expected!r}",
                ),
            )
        )
    return {
        plan2_scheduled_gimmick_hook_key(
            value.start_turn,
            value.group_id,
            value.effect.effect_id,
        ): _hook_for_step(value)
        for value in group.steps
    }


def build_initial_regular_plan2_final_gimmick_hooks(
    raw_schedule: object,
    *,
    master_path: Path = DEFAULT_GIMMICK_MASTER,
    database: Path = DEFAULT_MASTER_DATABASE,
) -> Mapping[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook]:
    """Compatibility name for callers bound before all audition stages shared it."""
    return build_plan2_audition_gimmick_hooks(
        raw_schedule, master_path=master_path, database=database,
    )


def provision_initial_regular_plan2_gimmick(
    request: InitialRegularInnerStageRequest,
    *,
    master_path: Path = DEFAULT_GIMMICK_MASTER,
    database: Path = DEFAULT_MASTER_DATABASE,
) -> InitialRegularPlan2GimmickRuntimeProvision:
    """Build facts and hooks directly consumable by the common adapter."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    group_id = request.gimmick_group_id
    if group_id is None:
        return InitialRegularPlan2GimmickRuntimeProvision(
            blockers=(
                _block("plan2-gimmick-group-id-unresolved", "request"),
            )
        )
    if group_id == "":
        return InitialRegularPlan2GimmickRuntimeProvision(
            gimmicks=InitialRegularPlan2GimmickProvision("")
        )
    if (
        request.stage_kind is not InitialRegularInnerStageKind.AUDITION
        or request.stage_type != FINAL
        or request.exam_type != 1
        or request.step_type_value != 18
    ):
        return InitialRegularPlan2GimmickRuntimeProvision(
            blockers=(
                _block(
                    "plan2-gimmick-stage-shape-unbound",
                    f"{group_id}:stage={request.stage_type}:"
                    f"exam={request.exam_type}:step={request.step_type_value}",
                ),
            )
        )
    try:
        if group_id == FKTN_AGGRESSIVE_FINAL_GIMMICK_GROUP_ID:
            group = load_plan2_master_aggressive_gimmick_group(
                group_id,
                master_path=master_path,
                database=database,
            )
            hooks = tuple(
                (
                    plan2_scheduled_gimmick_hook_key(
                        value.start_turn,
                        value.group_id,
                        value.effect.effect_id,
                    ),
                    _hook_for_aggressive_step(value),
                )
                for value in group.steps
            )
        else:
            group = load_plan2_master_gimmick_group(
                group_id,
                master_path=master_path,
                database=database,
            )
            hooks = tuple(
                (
                    plan2_scheduled_gimmick_hook_key(
                        value.start_turn,
                        value.group_id,
                        value.effect.effect_id,
                    ),
                    _hook_for_step(value),
                )
                for value in group.steps
            )
    except InitialRegularPlan2GimmickRuntimeBlocked as error:
        return InitialRegularPlan2GimmickRuntimeProvision(
            blockers=error.blockers
        )
    maximum_turn = (
        None
        if request.limit_turn is None or request.extra_turn is None
        else request.limit_turn + request.extra_turn
    )
    if maximum_turn is None or any(
        value.start_turn > maximum_turn for value in group.steps
    ):
        return InitialRegularPlan2GimmickRuntimeProvision(
            blockers=(
                _block(
                    "plan2-gimmick-turn-out-of-stage-range",
                    f"{group_id}:maximum={maximum_turn}",
                ),
            )
        )
    facts = tuple(
        Plan2NativeStageGimmickFact(
            turn=value.start_turn,
            gimmick_group_id=group.group_id,
            gimmick_effect_id=value.effect.effect_id,
        )
        for value in group.steps
    )
    return InitialRegularPlan2GimmickRuntimeProvision(
        gimmicks=InitialRegularPlan2GimmickProvision(
            group.group_id,
            facts=facts,
            hooks=hooks,
        )
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2MasterGimmickProvider:
    """Callable adapter provider over the pinned PC Master sources."""

    master_path: Path = DEFAULT_GIMMICK_MASTER
    database: Path = DEFAULT_MASTER_DATABASE

    def provision(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularPlan2GimmickRuntimeProvision:
        return provision_initial_regular_plan2_gimmick(
            request,
            master_path=self.master_path,
            database=self.database,
        )

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularPlan2GimmickProvision:
        return self.provision(request).require_gimmicks()


__all__ = [
    "ANDROID_V323_DUMP_SOURCE",
    "ANDROID_V323_EXAM_EXTENSIONS_SOURCE",
    "ANDROID_V323_EXAM_SEQUENCE_SOURCE",
    "DEFAULT_GIMMICK_MASTER",
    "DEFAULT_MASTER_DATABASE",
    "EXAM_AGGRESSIVE_EFFECT_TYPE",
    "EXAM_BLOCK_EFFECT_TYPE",
    "EXAM_LESSON_VALUE_MULTIPLE_EFFECT_TYPE",
    "EXAM_REVIEW_EFFECT_TYPE",
    "EXAM_REVIEW_VALUE_MULTIPLE_EFFECT_TYPE",
    "EXAM_STATUS_ENCHANT_EFFECT_TYPE",
    "FIELD_AGGRESSIVE_UP",
    "FIELD_BLOCK_UP",
    "FIELD_CHECK_NORMAL",
    "FIELD_REVIEW_UP",
    "FIELD_STATUS_UNKNOWN",
    "FKTN_AGGRESSIVE_FINAL_GIMMICK_GROUP_ID",
    "FKTN_FINAL_GIMMICK_GROUP_ID",
    "INITIAL_MASTER_REVIEW_FINAL_GIMMICK_GROUP_ID",
    "NIA_FINAL_REVIEW_GIMMICK_GROUP_ID",
    "InitialRegularPlan2GimmickRuntimeBlocked",
    "InitialRegularPlan2GimmickRuntimeProvision",
    "InitialRegularPlan2MasterGimmickProvider",
    "Plan2MasterAggressiveGimmickGroup",
    "Plan2MasterAggressiveGimmickStep",
    "Plan2MasterGimmickEffect",
    "Plan2MasterGimmickGroup",
    "Plan2MasterGimmickStep",
    "build_initial_regular_plan2_final_gimmick_hooks",
    "build_plan2_audition_gimmick_hooks",
    "build_plan2_master_review_gimmick_hooks",
    "load_plan2_master_aggressive_gimmick_group",
    "load_plan2_master_gimmick_group",
    "load_plan2_master_gimmick_status_enchant_step",
    "load_plan2_master_review_gimmick_group",
    "provision_initial_regular_plan2_gimmick",
]
