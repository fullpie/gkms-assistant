"""Bounded Plan2 leaf for ``p_card-02-sup-3_180`` effect-trigger up3.

This leaf owns only the trigger on ordered effect slot 1 of the four
``ふれんどめもりーず`` versions.  The slot-0 timer is represented as an
opaque handoff (parent id, delay, and child id); its executor and child
formula remain owned by ``plan2_timer_lesson_depend_review.py``.

The native distinction that matters here is:

* ``ExecuteCardCommandImpl`` walks ``PlayProduceExamEffectList`` in Master
  order and evaluates a non-null ``PlayEffectTriggerList[i]`` while building
  direct-effect candidates;
* the field predicate reads the signed current Aggressive scalar and uses an
  inclusive ``>= 3`` comparison; card-play counts, status deltas, and
  ``SpendCount`` are not inputs;
* UseHand reaches that build before cost payment, while a cost-consuming
  UsePool/forced path reaches it after payment; both paths still build before
  any direct effect executes;
* the target card uses the one-argument PlayEffect command factory, so the
  original slot trigger is not re-run by the later effect runner.

No Plan2 runtime, Plan3, native-search, GUI, controller, clock, hash, or
security surface is used here.  The module is read-only against Master and
returns handoff/audit data only.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import (
    AggressiveTriggerRow,
    CHECK_NOT,
    DIRECT_DISPATCH_PHASE,
    FIELD_CARD_PLAY_AGGRESSIVE_UP,
    LESSON_UNKNOWN,
    MOVE_UNKNOWN,
    PHASE_NONE,
    PLAN2,
    resolve_aggressive_trigger,
)

# These are evidence-only imports.  They intentionally reuse the already
# audited shared field predicate and the up9 effect-slot call-site record;
# this leaf still checks the up3 Master row and its own transaction boundary.
from .plan2_card_trigger_aggressive_up9 import (  # noqa: E402
    ANDROID_V323_PC_METADATA_EVIDENCE as UP9_CARD_NATIVE_EVIDENCE,
)
from .plan2_effect_triggers_aggressive_up9 import (  # noqa: E402
    ANDROID_NATIVE_EVIDENCE as UP9_EFFECT_NATIVE_EVIDENCE,
)


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
NATIVE_AUDIT_ARTIFACT: Final = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_effect_trigger_aggressive_up3_native_audit.json"
)

TARGET_CARD_ID: Final = "p_card-02-sup-3_180"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_CARD_LEVEL_REFS: Final = tuple(
    (TARGET_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES
)
TARGET_TRIGGER_ID: Final = "e_trigger-none-card_play_aggressive_up-3"
TARGET_THRESHOLD: Final = 3

CARD_NAME_BY_UPGRADE: Final = {
    0: "ふれんどめもりーず",
    1: "ふれんどめもりーず+",
    2: "ふれんどめもりーず++",
    3: "ふれんどめもりーず+++",
}

COST_AGGRESSIVE: Final = "ExamCostType_ExamCardPlayAggressive"
CATEGORY_ACTIVE_SKILL: Final = "ProduceCardCategory_ActiveSkill"
CARD_MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"

TIMER_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamEffectTimer"
STATUS_ENCHANT_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamStatusEnchant"
TIMER_SLOT_INDEX: Final = 0
TRIGGER_SLOT_INDEX: Final = 1
TIMER_DELAY: Final = 4
END_TURN_TRIGGER_ID: Final = "e_trigger-exam_end_turn"

TIMER_EFFECT_ID_BY_UPGRADE: Final = {
    0: "e_effect-exam_effect_timer-0004-01-e_effect-exam_lesson_depend_exam_review-1200-01",
    1: "e_effect-exam_effect_timer-0004-01-e_effect-exam_lesson_depend_exam_review-1200-01",
    2: "e_effect-exam_effect_timer-0004-01-e_effect-exam_lesson_depend_exam_review-1800-01",
    3: "e_effect-exam_effect_timer-0004-01-e_effect-exam_lesson_depend_exam_review-2100-01",
}
TIMER_CHILD_EFFECT_ID_BY_UPGRADE: Final = {
    0: "e_effect-exam_lesson_depend_exam_review-1200-01",
    1: "e_effect-exam_lesson_depend_exam_review-1200-01",
    2: "e_effect-exam_lesson_depend_exam_review-1800-01",
    3: "e_effect-exam_lesson_depend_exam_review-2100-01",
}
TIMER_VALUE_BY_UPGRADE: Final = {0: 1200, 1: 1200, 2: 1800, 3: 2100}

STATUS_WRAPPER_EFFECT_ID_BY_UPGRADE: Final = {
    0: "e_effect-exam_status_enchant-03-enchant-p_card-02-sup-3_180-enc02",
    1: "e_effect-exam_status_enchant-03-enchant-p_card-02-sup-3_180-enc01",
    2: "e_effect-exam_status_enchant-03-enchant-p_card-02-sup-3_180-enc01",
    3: "e_effect-exam_status_enchant-03-enchant-p_card-02-sup-3_180-enc01",
}
STATUS_ENCHANT_ID_BY_UPGRADE: Final = {
    0: "enchant-p_card-02-sup-3_180-enc02",
    1: "enchant-p_card-02-sup-3_180-enc01",
    2: "enchant-p_card-02-sup-3_180-enc01",
    3: "enchant-p_card-02-sup-3_180-enc01",
}
STATUS_CHILD_EFFECT_ID_BY_UPGRADE: Final = {
    0: "e_effect-exam_review-0002",
    1: "e_effect-exam_review-0003",
    2: "e_effect-exam_review-0003",
    3: "e_effect-exam_review-0003",
}

CARD_ORIGINS: Final = ("normal", "forced", "extra")
DIRECT_EFFECT_BUILD_BOUNDARY: Final = "effect-slot-candidate-build"
COMMAND_FACTORY: Final = "ExamPlayCommand.CreatePlayEffectCommand(effect)"
TWO_ARGUMENT_COMMAND_FACTORY: Final = (
    "ExamPlayCommand.CreatePlayEffectCommand(effect, trigger)"
)


class Plan2EffectTriggerAggressiveUp3ContractError(ValueError):
    """The local Master or supplied runtime shape is outside this leaf."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class EffectTriggerBoundary(str, Enum):
    """Payment position at the direct-effect candidate-build call site."""

    PRE_PAYMENT_DIRECT_EFFECT_BUILD = "pre-payment-direct-effect-build"
    POST_PAYMENT_DIRECT_EFFECT_BUILD = "post-payment-direct-effect-build"
    NO_PAYMENT_DIRECT_EFFECT_BUILD = "no-payment-direct-effect-build"


class EffectTriggerStage(str, Enum):
    """The original card-slot trigger exists only at candidate build time."""

    CANDIDATE_BUILD = "effect-slot-candidate-build"
    PLAY_EFFECT_EXECUTION = "play-effect-execution"
    POST_DIRECT_EFFECT = "post-direct-effect"


@dataclass(frozen=True, slots=True)
class _ExpectedEffect:
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]
    status_enchant_id: str = ""
    chain_effect_id: str = ""


_TIMER_GROUPS: Final = (
    "effect_group-visible-exam_lesson_depend_exam_review-000",
    "effect_group-visible-exam_lesson-000",
    "effect_group-visible-exam_effect_timer-000",
)
_STATUS_GROUPS: Final = (
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_review-000",
)

_EXPECTED_EFFECTS: Final[dict[str, _ExpectedEffect]] = {
    TIMER_EFFECT_ID_BY_UPGRADE[0]: _ExpectedEffect(
        TIMER_EFFECT_TYPE,
        TIMER_DELAY,
        0,
        1,
        0,
        _TIMER_GROUPS,
        chain_effect_id=TIMER_CHILD_EFFECT_ID_BY_UPGRADE[0],
    ),
    TIMER_EFFECT_ID_BY_UPGRADE[2]: _ExpectedEffect(
        TIMER_EFFECT_TYPE,
        TIMER_DELAY,
        0,
        1,
        0,
        _TIMER_GROUPS,
        chain_effect_id=TIMER_CHILD_EFFECT_ID_BY_UPGRADE[2],
    ),
    TIMER_EFFECT_ID_BY_UPGRADE[3]: _ExpectedEffect(
        TIMER_EFFECT_TYPE,
        TIMER_DELAY,
        0,
        1,
        0,
        _TIMER_GROUPS,
        chain_effect_id=TIMER_CHILD_EFFECT_ID_BY_UPGRADE[3],
    ),
    STATUS_WRAPPER_EFFECT_ID_BY_UPGRADE[0]: _ExpectedEffect(
        STATUS_ENCHANT_EFFECT_TYPE,
        0,
        0,
        0,
        3,
        _STATUS_GROUPS,
        status_enchant_id=STATUS_ENCHANT_ID_BY_UPGRADE[0],
    ),
    STATUS_WRAPPER_EFFECT_ID_BY_UPGRADE[1]: _ExpectedEffect(
        STATUS_ENCHANT_EFFECT_TYPE,
        0,
        0,
        0,
        3,
        _STATUS_GROUPS,
        status_enchant_id=STATUS_ENCHANT_ID_BY_UPGRADE[1],
    ),
}

_RAW_TRIGGER_KEYS: Final = frozenset(
    {
        "id",
        "phaseTypes",
        "phaseValues",
        "fieldStatusCheckTypes",
        "fieldStatusTypes",
        "fieldStatusValues",
        "fieldStatusProduceCardSearchIds",
        "produceCardSearchId",
        "upperSearchCount",
        "lowerSearchCount",
        "cardMovePositionType",
        "effectTypes",
        "lessonType",
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    }
)
_RAW_CARD_KEYS: Final = frozenset(
    {
        "assetId",
        "category",
        "costType",
        "costValue",
        "effectGroupIds",
        "evaluation",
        "forceStamina",
        "id",
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
        "moveEffectTriggerType",
        "moveProduceExamEffectIds",
        "moveProduceExamTriggerIds",
        "name",
        "noDeckDuplication",
        "order",
        "originCharacterId",
        "originIdolCardId",
        "originPrimaStellaIdolCardId",
        "originSupportCardId",
        "planType",
        "playEffects",
        "playMovePositionType",
        "playProduceExamTriggerId",
        "produceCardCustomizeIds",
        "produceCardStatusEnchantId",
        "produceDescriptions",
        "rarity",
        "rentalUnlockProducerLevel",
        "searchTag",
        "stamina",
        "unlockProducerLevel",
        "upgradeCount",
        "viewStartTime",
        "voiceAssetId",
    }
)
_RAW_EFFECT_KEYS: Final = frozenset(
    {
        "chainProduceExamEffectId",
        "chainProduceExamEffectIds",
        "customizeProduceDescriptions",
        "effectCount",
        "effectGroupIds",
        "effectTurn",
        "effectType",
        "effectValue1",
        "effectValue2",
        "id",
        "movePositionType",
        "pickCountMax",
        "pickCountMax2",
        "pickCountMin",
        "pickCountMin2",
        "pickCountReferenceProduceCardSearchId",
        "pickCountReferenceProduceCardSearchId2",
        "pickCountType",
        "pickCountType2",
        "pickRangeType",
        "pickRangeType2",
        "produceCardGrowEffectIds",
        "produceCardSearchId",
        "produceCardSearchId2",
        "produceCardStatusEnchantId",
        "produceDescriptions",
        "produceExamStatusEnchantId",
        "targetExamEffectType",
        "targetProduceCardId",
        "targetUpgradeCount",
    }
)
_RAW_STATUS_KEYS: Final = frozenset(
    {"assetId", "id", "produceDescriptions", "produceExamEffectIds", "produceExamTriggerId"}
)


def _plain_int(value: object, label: str, *, signed: bool = True) -> int:
    if type(value) is not int:
        raise Plan2EffectTriggerAggressiveUp3ContractError("invalid-integer", label)
    if signed and not INT32_MIN <= value <= INT32_MAX:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "integer-out-of-range", label
        )
    if not signed and value < 0:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "negative-integer", label
        )
    return value


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise Plan2EffectTriggerAggressiveUp3ContractError("invalid-text", label)
    return value


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-json-object", label
        ) from error
    if not isinstance(parsed, dict):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-json-object", label
        )
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-json-array", label
        ) from error
    if not isinstance(parsed, list):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-json-array", label
        )
    return parsed


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not str for item in parsed):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-string-array", label
        )
    return tuple(parsed)  # type: ignore[arg-type]


def _int_tuple(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not int for item in parsed):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-integer-array", label
        )
    return tuple(parsed)  # type: ignore[arg-type]


def _validate_exact_keys(
    raw: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if frozenset(raw) != expected:
        raise Plan2EffectTriggerAggressiveUp3ContractError("raw-shape", label)


def _validate_description_arrays(
    raw: Mapping[str, object], keys: Sequence[str], label: str
) -> None:
    for key in keys:
        value = raw[key]
        if type(value) is not list or any(type(item) is not dict for item in value):
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "description-shape", f"{label}:{key}"
            )


EXACT_TARGET_TRIGGER: Final = AggressiveTriggerRow(
    id=TARGET_TRIGGER_ID,
    phase_types=(PHASE_NONE,),
    phase_values=(),
    field_status_check_types=(),
    field_status_types=(FIELD_CARD_PLAY_AGGRESSIVE_UP,),
    field_status_values=(TARGET_THRESHOLD,),
    field_status_card_search_ids=(),
    produce_card_search_id="",
    upper_search_count=0,
    lower_search_count=0,
    card_move_position_type=MOVE_UNKNOWN,
    effect_types=(),
    lesson_type=LESSON_UNKNOWN,
)


def _validate_raw_trigger(raw: Mapping[str, object]) -> None:
    _validate_exact_keys(raw, _RAW_TRIGGER_KEYS, TARGET_TRIGGER_ID)
    for key, wanted in EXACT_TARGET_TRIGGER.to_dict().items():
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "trigger-raw-value", f"{TARGET_TRIGGER_ID}:{key}"
            )
    _validate_description_arrays(
        raw,
        ("produceDescriptions", "playProduceDescriptions", "playEffectProduceDescriptions"),
        TARGET_TRIGGER_ID,
    )


def _parse_trigger_row(row: sqlite3.Row) -> AggressiveTriggerRow:
    if _required_text(row["id"], "trigger.id") != TARGET_TRIGGER_ID:
        raise Plan2EffectTriggerAggressiveUp3ContractError("unknown-trigger")
    parsed = AggressiveTriggerRow(
        id=TARGET_TRIGGER_ID,
        phase_types=_string_tuple(row["phase_types_json"], "trigger.phaseTypes"),
        phase_values=_int_tuple(row["phase_values_json"], "trigger.phaseValues"),
        field_status_check_types=_string_tuple(
            row["field_status_check_types_json"], "trigger.fieldStatusCheckTypes"
        ),
        field_status_types=_string_tuple(
            row["field_status_types_json"], "trigger.fieldStatusTypes"
        ),
        field_status_values=_int_tuple(
            row["field_status_values_json"], "trigger.fieldStatusValues"
        ),
        field_status_card_search_ids=_string_tuple(
            row["field_status_produce_card_search_ids_json"],
            "trigger.fieldStatusProduceCardSearchIds",
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_plain_int(
            row["upper_search_count"], "trigger.upperSearchCount", signed=False
        ),
        lower_search_count=_plain_int(
            row["lower_search_count"], "trigger.lowerSearchCount", signed=False
        ),
        card_move_position_type=_required_text(
            row["card_move_position_type"], "trigger.cardMovePositionType"
        ),
        effect_types=_string_tuple(row["effect_types_json"], "trigger.effectTypes"),
        lesson_type=_required_text(row["lesson_type"], "trigger.lessonType"),
    )
    if parsed != EXACT_TARGET_TRIGGER:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "trigger-shape", TARGET_TRIGGER_ID
        )
    _validate_raw_trigger(_json_object(row["raw_json"], "trigger.raw"))
    return parsed


@dataclass(frozen=True, slots=True)
class EffectTriggerContract:
    row: AggressiveTriggerRow
    threshold: int = TARGET_THRESHOLD
    comparison: str = "signed_greater_equal"
    value_source: str = (
        "signed current ExamStatusEffectCollection.GetAggressive(true)"
    )
    counter_source: str = "global/per-turn Aggressive card-play counts are not read"
    status_delta_source: str = "Aggressive/status delta is not read"
    trigger_count_source: str = "not a TriggerEffectStatusEffect listener"
    phase: str = PHASE_NONE
    callsite: str = "ExamSequence.ExecuteCardCommandImpl.PlayEffectTriggerList[i]"
    evaluation_stage: str = DIRECT_EFFECT_BUILD_BOUNDARY
    same_card_reread: bool = False

    @property
    def check_not(self) -> bool:
        return self.row.field_status_check_types == (CHECK_NOT,)

    @property
    def result_expression(self) -> str:
        base = f"signed_aggressive_value >= {self.threshold}"
        return f"not ({base})" if self.check_not else base

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "phase": self.phase,
            "threshold": self.threshold,
            "checkNot": self.check_not,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "valueSource": self.value_source,
            "counterSource": self.counter_source,
            "statusDeltaSource": self.status_delta_source,
            "triggerCountSource": self.trigger_count_source,
            "callsite": self.callsite,
            "evaluationStage": self.evaluation_stage,
            "sameCardReread": self.same_card_reread,
        }


@dataclass(frozen=True, slots=True)
class EffectTriggerResolution:
    supported: bool
    contract: EffectTriggerContract | None
    reasons: tuple[str, ...] = ()


def resolve_effect_trigger_aggressive_up3(
    row: AggressiveTriggerRow,
) -> EffectTriggerResolution:
    """Resolve only the exact up3 row; an altered shape is unsupported."""

    if row.id != TARGET_TRIGGER_ID:
        return EffectTriggerResolution(False, None, ("unknown-target-trigger",))
    shared = resolve_aggressive_trigger(row)
    reasons = list(shared.reasons)
    if row != EXACT_TARGET_TRIGGER:
        reasons.append("trigger-row-is-not-exact-target-shape")
    if reasons:
        return EffectTriggerResolution(False, None, tuple(dict.fromkeys(reasons)))
    return EffectTriggerResolution(
        True, EffectTriggerContract(row=row, threshold=TARGET_THRESHOLD)
    )


@dataclass(frozen=True, slots=True)
class EffectTriggerEvaluationInput:
    """Native inputs plus explicit inert fields and transaction position."""

    aggressive_status_value: int
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    observed_aggressive_status_delta: int = 0
    phase: str = PHASE_NONE
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: EffectTriggerBoundary = (
        EffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD
    )
    stage: EffectTriggerStage = EffectTriggerStage.CANDIDATE_BUILD
    card_origin: str = "normal"
    is_use_playable_count: bool = True
    payment_committed_before_build: bool | None = None
    direct_effects_executed_before_build: int = 0
    later_same_card_aggressive_status_value: int | None = None

    def __post_init__(self) -> None:
        _plain_int(self.aggressive_status_value, "aggressive_status_value")
        _plain_int(
            self.global_card_play_count,
            "global_card_play_count",
            signed=False,
        )
        _plain_int(self.turn_card_play_count, "turn_card_play_count", signed=False)
        _plain_int(
            self.observed_aggressive_status_delta,
            "observed_aggressive_status_delta",
        )
        _plain_int(
            self.direct_effects_executed_before_build,
            "direct_effects_executed_before_build",
            signed=False,
        )
        if self.later_same_card_aggressive_status_value is not None:
            _plain_int(
                self.later_same_card_aggressive_status_value,
                "later_same_card_aggressive_status_value",
            )
        if self.card_origin not in CARD_ORIGINS:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "unsupported-card-origin", str(self.card_origin)
            )
        if type(self.is_use_playable_count) is not bool:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "invalid-boolean", "is_use_playable_count"
            )
        try:
            boundary = (
                self.boundary
                if isinstance(self.boundary, EffectTriggerBoundary)
                else EffectTriggerBoundary(self.boundary)
            )
            stage = (
                self.stage
                if isinstance(self.stage, EffectTriggerStage)
                else EffectTriggerStage(self.stage)
            )
        except (TypeError, ValueError) as error:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "invalid-transaction-shape", str(error)
            ) from error
        object.__setattr__(self, "boundary", boundary)
        object.__setattr__(self, "stage", stage)
        if self.payment_committed_before_build is not None and type(
            self.payment_committed_before_build
        ) is not bool:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "invalid-boolean", "payment_committed_before_build"
            )

    @property
    def payment_committed(self) -> bool:
        if self.payment_committed_before_build is not None:
            return self.payment_committed_before_build
        return self.boundary is EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD


AggressiveEffectTriggerInput = EffectTriggerEvaluationInput


@dataclass(frozen=True, slots=True)
class EffectTriggerEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    aggressive_status_value: int | None
    threshold: int | None
    check_not: bool | None
    phase: str
    dispatch_phase: str
    boundary: EffectTriggerBoundary
    stage: EffectTriggerStage
    card_origin: str
    payment_committed_before_build: bool
    direct_effects_executed_before_build: int
    read_fields: tuple[str, ...] = ("signed current Aggressive scalar",)
    ignored_fields: tuple[str, ...] = (
        "global card-play count",
        "per-turn card-play count",
        "Aggressive/status delta",
        "isUsePlayableCount",
        "later same-card Aggressive value",
        "TriggerEffectStatusEffect.SpendCount",
    )
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "supported": self.supported,
            "fires": self.fires,
            "aggressiveStatusValue": self.aggressive_status_value,
            "threshold": self.threshold,
            "checkNot": self.check_not,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "stage": self.stage.value,
            "cardOrigin": self.card_origin,
            "paymentCommittedBeforeBuild": self.payment_committed_before_build,
            "directEffectsExecutedBeforeBuild": self.direct_effects_executed_before_build,
            "readFields": list(self.read_fields),
            "ignoredFields": list(self.ignored_fields),
            "reasons": list(self.reasons),
        }


def evaluate_effect_trigger_aggressive_up3(
    inputs: EffectTriggerEvaluationInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
) -> EffectTriggerEvaluation:
    """Evaluate the slot trigger without reading or mutating runtime state."""

    resolution = resolve_effect_trigger_aggressive_up3(trigger)
    if not resolution.supported:
        return EffectTriggerEvaluation(
            trigger_id=trigger.id,
            supported=False,
            fires=None,
            aggressive_status_value=None,
            threshold=None,
            check_not=None,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            stage=inputs.stage,
            card_origin=inputs.card_origin,
            payment_committed_before_build=inputs.payment_committed,
            direct_effects_executed_before_build=inputs.direct_effects_executed_before_build,
            reasons=resolution.reasons,
        )
    contract = resolution.contract
    assert contract is not None
    reasons: list[str] = []
    if inputs.phase != PHASE_NONE:
        reasons.append("runtime-phase-is-not-none")
    if inputs.dispatch_phase != DIRECT_DISPATCH_PHASE:
        reasons.append("dispatch-phase-is-not-play-effect")
    if inputs.stage is not EffectTriggerStage.CANDIDATE_BUILD:
        reasons.append("runtime-stage-is-not-effect-candidate-build")
    if inputs.direct_effects_executed_before_build != 0:
        reasons.append("direct-effects-already-executed")
    expected_payment = (
        inputs.boundary is EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD
    )
    if inputs.payment_committed != expected_payment:
        reasons.append("payment-boundary-does-not-match-callsite")
    if reasons:
        return EffectTriggerEvaluation(
            trigger_id=trigger.id,
            supported=False,
            fires=None,
            aggressive_status_value=None,
            threshold=contract.threshold,
            check_not=contract.check_not,
            phase=inputs.phase,
            dispatch_phase=inputs.dispatch_phase,
            boundary=inputs.boundary,
            stage=inputs.stage,
            card_origin=inputs.card_origin,
            payment_committed_before_build=inputs.payment_committed,
            direct_effects_executed_before_build=inputs.direct_effects_executed_before_build,
            reasons=tuple(reasons),
        )
    base_match = inputs.aggressive_status_value >= contract.threshold
    fires = not base_match if contract.check_not else base_match
    return EffectTriggerEvaluation(
        trigger_id=trigger.id,
        supported=True,
        fires=fires,
        aggressive_status_value=inputs.aggressive_status_value,
        threshold=contract.threshold,
        check_not=contract.check_not,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        stage=inputs.stage,
        card_origin=inputs.card_origin,
        payment_committed_before_build=inputs.payment_committed,
        direct_effects_executed_before_build=inputs.direct_effects_executed_before_build,
    )


# Friendly aliases matching the existing up9 effect leaf vocabulary.
resolve_aggressive_effect_trigger = resolve_effect_trigger_aggressive_up3
evaluate_aggressive_effect_trigger = evaluate_effect_trigger_aggressive_up3


@dataclass(frozen=True, slots=True)
class MasterEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    effect_group_ids: tuple[str, ...]
    status_enchant_id: str = ""
    chain_effect_id: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "effectId": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "effectGroupIds": list(self.effect_group_ids),
            "statusEnchantId": self.status_enchant_id,
            "chainEffectId": self.chain_effect_id,
        }


@dataclass(frozen=True, slots=True)
class MasterStatusEnchant:
    status_id: str
    trigger_id: str
    child_effect_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "statusId": self.status_id,
            "triggerId": self.trigger_id,
            "childEffectIds": list(self.child_effect_ids),
        }


@dataclass(frozen=True, slots=True)
class EffectTriggerCardSlot:
    card_id: str
    upgrade: int
    slot_index: int
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    count: int
    turn: int
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool
    effect_group_ids: tuple[str, ...]
    chain_effect_id: str = ""
    status_enchant_id: str = ""
    status_trigger_id: str = ""
    status_child_effect_ids: tuple[str, ...] = ()

    @property
    def timer_delay(self) -> int | None:
        return self.value1 if self.effect_type == TIMER_EFFECT_TYPE else None

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot_index,
            "effectId": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
            "effectGroupIds": list(self.effect_group_ids),
            "chainEffectId": self.chain_effect_id,
            "statusEnchantId": self.status_enchant_id,
            "statusTriggerId": self.status_trigger_id,
            "statusChildEffectIds": list(self.status_child_effect_ids),
            "triggerRead": bool(self.trigger_id),
            "timerHandoffOnly": self.effect_type == TIMER_EFFECT_TYPE,
        }


@dataclass(frozen=True, slots=True)
class TargetCardVersion:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    effect_slots: tuple[EffectTriggerCardSlot, ...]

    @property
    def ref(self) -> tuple[str, int]:
        return (self.card_id, self.upgrade)

    @property
    def effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def trigger_ids(self) -> tuple[str, ...]:
        return tuple(slot.trigger_id for slot in self.effect_slots)

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "movePositionType": self.move_position_type,
            "effectSlots": [slot.to_dict() for slot in self.effect_slots],
        }


def _expected_slot_payload(upgrade: int) -> tuple[dict[str, object], ...]:
    return (
        {
            "produceExamTriggerId": "",
            "produceExamEffectId": TIMER_EFFECT_ID_BY_UPGRADE[upgrade],
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
        {
            "produceExamTriggerId": TARGET_TRIGGER_ID,
            "produceExamEffectId": STATUS_WRAPPER_EFFECT_ID_BY_UPGRADE[upgrade],
            "hideIcon": False,
            "isOncePlayEffect": False,
        },
    )


def _validate_raw_effect(raw: Mapping[str, object], expected: _ExpectedEffect) -> None:
    _validate_exact_keys(raw, _RAW_EFFECT_KEYS, expected.effect_type)
    exact: dict[str, object] = {
        "effectType": expected.effect_type,
        "effectValue1": expected.value1,
        "effectValue2": expected.value2,
        "effectCount": expected.count,
        "effectTurn": expected.turn,
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
        "chainProduceExamEffectId": expected.chain_effect_id,
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": expected.status_enchant_id,
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": list(expected.effect_group_ids),
    }
    for key, wanted in exact.items():
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "effect-raw-value", f"{expected.effect_type}:{key}"
            )
    _validate_description_arrays(
        raw, ("produceDescriptions", "customizeProduceDescriptions"), expected.effect_type
    )


def _parse_effect_row(row: sqlite3.Row, expected_id: str) -> MasterEffect:
    if _required_text(row["id"], "effect.id") != expected_id:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "effect-id-mismatch", f"{row['id']}:{expected_id}"
        )
    expected = _EXPECTED_EFFECTS.get(expected_id)
    if expected is None:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "unknown-effect", expected_id
        )
    actual_values = (
        _required_text(row["effect_type"], "effect.effect_type"),
        _plain_int(row["value1"], "effect.value1"),
        _plain_int(row["value2"], "effect.value2"),
        _plain_int(row["effect_count"], "effect.effect_count"),
        _plain_int(row["effect_turn"], "effect.effect_turn"),
        row["status_enchant_id"],
        row["chain_effect_id"],
    )
    wanted_values = (
        expected.effect_type,
        expected.value1,
        expected.value2,
        expected.count,
        expected.turn,
        expected.status_enchant_id,
        expected.chain_effect_id,
    )
    if actual_values != wanted_values:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "effect-value-or-link-shape", expected_id
        )
    raw = _json_object(row["raw_json"], f"{expected_id}:raw")
    if raw.get("id") != expected_id:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "effect-raw-value", f"{expected_id}:id"
        )
    _validate_raw_effect(raw, expected)
    return MasterEffect(
        effect_id=expected_id,
        effect_type=expected.effect_type,
        value1=expected.value1,
        value2=expected.value2,
        count=expected.count,
        turn=expected.turn,
        effect_group_ids=expected.effect_group_ids,
        status_enchant_id=expected.status_enchant_id,
        chain_effect_id=expected.chain_effect_id,
    )


def _validate_raw_card(
    raw: Mapping[str, object],
    *,
    card_id: str,
    upgrade: int,
    name: str,
    expected_payload: Sequence[Mapping[str, object]],
) -> None:
    _validate_exact_keys(raw, _RAW_CARD_KEYS, f"{card_id}:{upgrade}")
    exact: dict[str, object] = {
        "id": card_id,
        "upgradeCount": upgrade,
        "name": name,
        "planType": PLAN2,
        "category": CATEGORY_ACTIVE_SKILL,
        "stamina": 0,
        "costType": COST_AGGRESSIVE,
        "costValue": 2,
        "playProduceExamTriggerId": "",
        "playEffects": [dict(item) for item in expected_payload],
        "playMovePositionType": CARD_MOVE_LOST,
    }
    for key, wanted in exact.items():
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "card-raw-value", f"{card_id}:{upgrade}:{key}"
            )
    for key in (
        "produceDescriptions",
        "effectGroupIds",
        "produceCardCustomizeIds",
        "moveProduceExamEffectIds",
        "moveProduceExamTriggerIds",
    ):
        if type(raw[key]) is not list:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "card-raw-array", f"{card_id}:{upgrade}:{key}"
            )


def _parse_status_row(row: sqlite3.Row, expected_id: str, expected_child: str) -> MasterStatusEnchant:
    if _required_text(row["id"], "status.id") != expected_id:
        raise Plan2EffectTriggerAggressiveUp3ContractError("status-id-mismatch", expected_id)
    children = _string_tuple(row["produce_exam_effect_ids_json"], f"{expected_id}:children")
    if row["produce_exam_trigger_id"] != END_TURN_TRIGGER_ID or children != (expected_child,):
        raise Plan2EffectTriggerAggressiveUp3ContractError("status-link-shape", expected_id)
    raw = _json_object(row["raw_json"], f"{expected_id}:raw")
    _validate_exact_keys(raw, _RAW_STATUS_KEYS, expected_id)
    exact = {
        "id": expected_id,
        "assetId": "",
        "produceExamTriggerId": END_TURN_TRIGGER_ID,
        "produceExamEffectIds": [expected_child],
    }
    for key, wanted in exact.items():
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "status-raw-value", f"{expected_id}:{key}"
            )
    _validate_description_arrays(raw, ("produceDescriptions",), expected_id)
    return MasterStatusEnchant(expected_id, END_TURN_TRIGGER_ID, children)


def _parse_slot(
    payload: object,
    *,
    card_id: str,
    upgrade: int,
    slot_index: int,
    expected: Mapping[str, object],
    effects: Mapping[str, MasterEffect],
    statuses: Mapping[str, MasterStatusEnchant],
) -> EffectTriggerCardSlot:
    if not isinstance(payload, Mapping) or set(payload) != set(expected):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "card-slot-shape", f"{card_id}:{upgrade}:{slot_index}"
        )
    if dict(payload) != dict(expected):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "card-slot-value", f"{card_id}:{upgrade}:{slot_index}"
        )
    effect_id = str(expected["produceExamEffectId"])
    effect = effects.get(effect_id)
    if effect is None:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "unknown-slot-effect", f"{card_id}:{upgrade}:{slot_index}:{effect_id}"
        )
    status = statuses.get(effect.status_enchant_id)
    if effect.status_enchant_id and status is None:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "missing-status-enchant", effect.status_enchant_id
        )
    return EffectTriggerCardSlot(
        card_id=card_id,
        upgrade=upgrade,
        slot_index=slot_index,
        effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        count=effect.count,
        turn=effect.turn,
        trigger_id=str(expected["produceExamTriggerId"]),
        hide_icon=bool(expected["hideIcon"]),
        is_once_play_effect=expected["isOncePlayEffect"] is True,
        effect_group_ids=effect.effect_group_ids,
        chain_effect_id=effect.chain_effect_id,
        status_enchant_id=effect.status_enchant_id,
        status_trigger_id=status.trigger_id if status else "",
        status_child_effect_ids=status.child_effect_ids if status else (),
    )


def validate_target_card_version(card: TargetCardVersion) -> None:
    """Validate the normalized two-slot contract; malformed shape fails closed."""

    if card.ref not in TARGET_CARD_LEVEL_REFS:
        raise Plan2EffectTriggerAggressiveUp3ContractError("unknown-card", repr(card.ref))
    if card.name != CARD_NAME_BY_UPGRADE[card.upgrade]:
        raise Plan2EffectTriggerAggressiveUp3ContractError("card-name", repr(card.ref))
    if (
        card.plan_type != PLAN2
        or card.category != CATEGORY_ACTIVE_SKILL
        or card.stamina != 0
        or card.cost_type != COST_AGGRESSIVE
        or card.cost_value != 2
        or card.play_trigger_id != ""
        or card.move_position_type != CARD_MOVE_LOST
    ):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "card-admission-or-cost-or-move-shape", repr(card.ref)
        )
    expected_payload = _expected_slot_payload(card.upgrade)
    if len(card.effect_slots) != len(expected_payload):
        raise Plan2EffectTriggerAggressiveUp3ContractError("card-slot-count", repr(card.ref))
    for index, (slot, expected) in enumerate(zip(card.effect_slots, expected_payload)):
        effect_id = str(expected["produceExamEffectId"])
        wanted = _EXPECTED_EFFECTS[effect_id]
        if (
            slot.card_id != TARGET_CARD_ID
            or slot.upgrade != card.upgrade
            or slot.slot_index != index
            or slot.effect_id != effect_id
            or slot.trigger_id != expected["produceExamTriggerId"]
            or slot.hide_icon is not False
            or slot.is_once_play_effect is not False
            or slot.effect_type != wanted.effect_type
            or slot.value1 != wanted.value1
            or slot.value2 != wanted.value2
            or slot.count != wanted.count
            or slot.turn != wanted.turn
            or slot.effect_group_ids != wanted.effect_group_ids
            or slot.chain_effect_id != wanted.chain_effect_id
            or slot.status_enchant_id != wanted.status_enchant_id
        ):
            raise Plan2EffectTriggerAggressiveUp3ContractError(
                "card-slot-order-or-effect-shape", f"{card.ref}:{index}"
            )
        if index == TIMER_SLOT_INDEX:
            if (
                slot.trigger_id
                or slot.timer_delay != TIMER_DELAY
                or slot.status_trigger_id
                or slot.status_child_effect_ids
            ):
                raise Plan2EffectTriggerAggressiveUp3ContractError(
                    "timer-handoff-shape", repr(card.ref)
                )
        else:
            if (
                slot.trigger_id != TARGET_TRIGGER_ID
                or slot.status_trigger_id != END_TURN_TRIGGER_ID
                or slot.status_child_effect_ids
                != (STATUS_CHILD_EFFECT_ID_BY_UPGRADE[card.upgrade],)
            ):
                raise Plan2EffectTriggerAggressiveUp3ContractError(
                    "effect-trigger-handoff-shape", repr(card.ref)
                )


def _parse_card_row(
    row: sqlite3.Row,
    effects: Mapping[str, MasterEffect],
    statuses: Mapping[str, MasterStatusEnchant],
) -> TargetCardVersion:
    card_id = _required_text(row["id"], "card.id")
    upgrade = _plain_int(row["upgrade_count"], "card.upgrade_count", signed=False)
    if (card_id, upgrade) not in TARGET_CARD_LEVEL_REFS:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "unknown-card", f"{card_id}:{upgrade}"
        )
    name = _required_text(row["name"], f"{card_id}:{upgrade}:name")
    if name != CARD_NAME_BY_UPGRADE[upgrade]:
        raise Plan2EffectTriggerAggressiveUp3ContractError("card-name", f"{card_id}:{upgrade}")
    if row["plan_type"] != PLAN2 or row["category"] != CATEGORY_ACTIVE_SKILL:
        raise Plan2EffectTriggerAggressiveUp3ContractError("card-plan-or-category", f"{card_id}:{upgrade}")
    stamina = _plain_int(row["stamina"], f"{card_id}:{upgrade}:stamina", signed=False)
    cost_value = _plain_int(row["cost_value"], f"{card_id}:{upgrade}:cost_value", signed=False)
    if (
        stamina != 0
        or row["cost_type"] != COST_AGGRESSIVE
        or cost_value != 2
        or row["play_trigger_id"] != ""
        or row["move_position_type"] != CARD_MOVE_LOST
    ):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "card-admission-or-cost-or-move-shape", f"{card_id}:{upgrade}"
        )
    payload = _json_array(row["play_effects_json"], f"{card_id}:{upgrade}:playEffects")
    expected_payload = _expected_slot_payload(upgrade)
    if len(payload) != len(expected_payload):
        raise Plan2EffectTriggerAggressiveUp3ContractError("card-slot-count", f"{card_id}:{upgrade}")
    _validate_raw_card(
        _json_object(row["raw_json"], f"{card_id}:{upgrade}:raw"),
        card_id=card_id,
        upgrade=upgrade,
        name=name,
        expected_payload=expected_payload,
    )
    slots = tuple(
        _parse_slot(
            item,
            card_id=card_id,
            upgrade=upgrade,
            slot_index=index,
            expected=expected,
            effects=effects,
            statuses=statuses,
        )
        for index, (item, expected) in enumerate(zip(payload, expected_payload))
    )
    card = TargetCardVersion(
        card_id=card_id,
        upgrade=upgrade,
        name=name,
        plan_type=str(row["plan_type"]),
        category=str(row["category"]),
        stamina=stamina,
        cost_type=str(row["cost_type"]),
        cost_value=cost_value,
        play_trigger_id=str(row["play_trigger_id"]),
        move_position_type=str(row["move_position_type"]),
        effect_slots=slots,
    )
    validate_target_card_version(card)
    return card


def _read_rows_by_id(
    connection: sqlite3.Connection, table: str, ids: Sequence[str]
) -> dict[str, sqlite3.Row]:
    if not ids or len(set(ids)) != len(ids):
        raise Plan2EffectTriggerAggressiveUp3ContractError("invalid-master-id-list", table)
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"SELECT * FROM {table} WHERE id IN ({placeholders})", tuple(ids)
    ).fetchall()
    result = {str(row["id"]): row for row in rows}
    if len(rows) != len(ids) or set(result) != set(ids):
        missing = sorted(set(ids) - set(result))
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "master-catalog-missing", f"{table}:{','.join(missing)}"
        )
    return result


@dataclass(frozen=True, slots=True)
class Plan2EffectTriggerAggressiveUp3Catalog:
    database: str
    trigger: AggressiveTriggerRow
    card_versions: tuple[TargetCardVersion, ...]
    effect_rows: tuple[MasterEffect, ...]
    status_rows: tuple[MasterStatusEnchant, ...]

    @property
    def trigger_rows(self) -> tuple[AggressiveTriggerRow, ...]:
        return (self.trigger,)

    @property
    def version_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(card.ref for card in self.card_versions)

    def card(self, upgrade: int) -> TargetCardVersion:
        for card in self.card_versions:
            if card.upgrade == upgrade:
                return card
        raise KeyError(upgrade)

    def accounting(self) -> dict[str, object]:
        affected = self.version_refs
        return {
            "affected": len(affected),
            "direct": 0,
            "coBlocked": len(affected),
            "affectedRefs": [list(ref) for ref in affected],
            "directRefs": [],
            "coBlockedRefs": [list(ref) for ref in affected],
            "remainingBlocker": f"C:effect-trigger:{TARGET_TRIGGER_ID}",
            "standaloneBaseline": (
                "timer family already owns delay4 handoff; this leaf leaves all four "
                "versions co-blocked until central timer-family chaining integrates the trigger"
            ),
            "afterTimerFamilyCentralHandoff": {
                "directExpected": len(affected),
                "coBlockedExpected": 0,
                "formalCoverageNumber": "not_claimed_by_standalone_leaf",
            },
        }


def load_plan2_effect_trigger_aggressive_up3_catalog(
    database: Path = DEFAULT_DATABASE,
) -> Plan2EffectTriggerAggressiveUp3Catalog:
    """Read the exact trigger, four card versions, wrappers, and handoff rows."""

    path = Path(database).resolve()
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            trigger_rows = connection.execute(
                "SELECT * FROM produce_exam_trigger WHERE id = ?", (TARGET_TRIGGER_ID,)
            ).fetchall()
            if len(trigger_rows) != 1:
                raise Plan2EffectTriggerAggressiveUp3ContractError("trigger-catalog", repr(len(trigger_rows)))
            trigger = _parse_trigger_row(trigger_rows[0])

            effect_ids = tuple(dict.fromkeys((*TIMER_EFFECT_ID_BY_UPGRADE.values(), *STATUS_WRAPPER_EFFECT_ID_BY_UPGRADE.values())))
            raw_effects = _read_rows_by_id(connection, "effect", effect_ids)
            effects = {
                effect_id: _parse_effect_row(raw_effects[effect_id], effect_id)
                for effect_id in effect_ids
            }

            status_ids = tuple(dict.fromkeys(STATUS_ENCHANT_ID_BY_UPGRADE.values()))
            raw_statuses = _read_rows_by_id(connection, "produce_exam_status_enchant", status_ids)
            statuses = {
                status_id: _parse_status_row(
                    raw_statuses[status_id],
                    status_id,
                    STATUS_CHILD_EFFECT_ID_BY_UPGRADE[
                        next(
                            upgrade
                            for upgrade, expected_id in STATUS_ENCHANT_ID_BY_UPGRADE.items()
                            if expected_id == status_id
                        )
                    ],
                )
                for status_id in status_ids
            }

            child_ids = tuple(
                dict.fromkeys(
                    (*TIMER_CHILD_EFFECT_ID_BY_UPGRADE.values(), *STATUS_CHILD_EFFECT_ID_BY_UPGRADE.values())
                )
            )
            _read_rows_by_id(connection, "effect", child_ids)

            rows = connection.execute(
                """
                SELECT * FROM card
                 WHERE id = ? AND upgrade_count BETWEEN 0 AND 3
                 ORDER BY upgrade_count
                """,
                (TARGET_CARD_ID,),
            ).fetchall()
            actual_refs = tuple((str(row["id"]), int(row["upgrade_count"])) for row in rows)
            if actual_refs != TARGET_CARD_LEVEL_REFS:
                raise Plan2EffectTriggerAggressiveUp3ContractError("card-catalog", repr(actual_refs))
            cards = tuple(_parse_card_row(row, effects, statuses) for row in rows)
    except Plan2EffectTriggerAggressiveUp3ContractError:
        raise
    except sqlite3.Error as error:
        raise Plan2EffectTriggerAggressiveUp3ContractError("master-read-failed", str(error)) from error
    return Plan2EffectTriggerAggressiveUp3Catalog(
        database=str(path),
        trigger=trigger,
        card_versions=cards,
        effect_rows=tuple(effects[effect_id] for effect_id in effect_ids),
        status_rows=tuple(statuses[status_id] for status_id in status_ids),
    )


load_catalog = load_plan2_effect_trigger_aggressive_up3_catalog


@dataclass(frozen=True, slots=True)
class TimerEnqueueHandoff:
    slot_index: int
    parent_effect_id: str
    delay: int
    child_effect_id: str
    relative_order: str = "slot-0-before-triggered-slot-1"
    formula_or_executor_reimplemented: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot_index,
            "parentEffectId": self.parent_effect_id,
            "delay": self.delay,
            "childEffectId": self.child_effect_id,
            "relativeOrder": self.relative_order,
            "formulaOrExecutorReimplemented": self.formula_or_executor_reimplemented,
        }


@dataclass(frozen=True, slots=True)
class OrderedEffectPass:
    pass_index: int
    snapshot_aggressive_status_value: int
    slot_evaluations: tuple[tuple[int, EffectTriggerEvaluation], ...]
    candidate_slot_indexes: tuple[int, ...]
    timer_handoff: TimerEnqueueHandoff
    execution_order: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "pass": self.pass_index,
            "snapshotAggressiveStatusValue": self.snapshot_aggressive_status_value,
            "orderedSlotIndexes": [0, 1],
            "slotEvaluations": [
                {"slot": index, "evaluation": evaluation.to_dict()}
                for index, evaluation in self.slot_evaluations
            ],
            "candidateSlotIndexes": list(self.candidate_slot_indexes),
            "timerEnqueue": self.timer_handoff.to_dict(),
            "executionOrder": list(self.execution_order),
            "triggerReadSlots": [index for index, _ in self.slot_evaluations],
            "samePassRereadPolicy": (
                "one current signed Aggressive read for each non-null trigger-bearing "
                "ordered slot; slot 0 has no trigger"
            ),
        }


@dataclass(frozen=True, slots=True)
class EffectTriggerBuildProbe:
    card_ref: tuple[str, int]
    card_origin: str
    payment_committed_before_build: bool
    repetitions: int
    passes: tuple[OrderedEffectPass, ...]
    command_factory: str = COMMAND_FACTORY
    same_card_trigger_reread: bool = False

    @property
    def snapshot_aggressive_status_value(self) -> int:
        return self.passes[0].snapshot_aggressive_status_value

    @property
    def slot_evaluations(self) -> tuple[tuple[int, EffectTriggerEvaluation], ...]:
        return self.passes[0].slot_evaluations

    @property
    def included_slot_indexes(self) -> tuple[int, ...]:
        return self.passes[0].candidate_slot_indexes

    @property
    def included_effect_ids(self) -> tuple[str, ...]:
        indexes = set(self.included_slot_indexes)
        return tuple(
            effect_id
            for effect_id, index in (
                (TIMER_EFFECT_ID_BY_UPGRADE[self.card_ref[1]], 0),
                (STATUS_WRAPPER_EFFECT_ID_BY_UPGRADE[self.card_ref[1]], 1),
            )
            if index in indexes
        )

    @property
    def trigger_read_count(self) -> int:
        return sum(len(item.slot_evaluations) for item in self.passes)

    @property
    def trigger_read_slots_per_pass(self) -> tuple[int, ...]:
        return tuple(index for index, _ in self.passes[0].slot_evaluations)

    def to_dict(self) -> dict[str, object]:
        return {
            "cardRef": list(self.card_ref),
            "cardOrigin": self.card_origin,
            "paymentCommittedBeforeBuild": self.payment_committed_before_build,
            "repetitions": self.repetitions,
            "snapshotAggressiveStatusValue": self.snapshot_aggressive_status_value,
            "snapshotRule": (
                "read current signed Aggressive at ExecuteCardCommandImpl candidate "
                "build; later cost/direct execution does not rebuild this command"
            ),
            "passes": [item.to_dict() for item in self.passes],
            "triggerReadSlotsPerPass": list(self.trigger_read_slots_per_pass),
            "triggerReadCount": self.trigger_read_count,
            "includedSlotIndexes": list(self.included_slot_indexes),
            "includedEffectIds": list(self.included_effect_ids),
            "sameCardTriggerReread": self.same_card_trigger_reread,
            "commandFactory": self.command_factory,
            "twoArgumentFactoryUsed": False,
        }


def simulate_effect_trigger_build(
    card: TargetCardVersion,
    inputs: EffectTriggerEvaluationInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
    *,
    repetitions: int = 1,
) -> EffectTriggerBuildProbe:
    """Build ordered direct-effect handoffs from one supplied card-play input."""

    validate_target_card_version(card)
    if type(repetitions) is not int or repetitions < 1:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-repeat-count", str(repetitions)
        )
    if trigger.id != TARGET_TRIGGER_ID:
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "unknown-target-trigger", trigger.id
        )
    passes: list[OrderedEffectPass] = []
    slots_by_index = {slot.slot_index: slot for slot in card.effect_slots}
    for pass_index in range(repetitions):
        evaluations: list[tuple[int, EffectTriggerEvaluation]] = []
        candidates = [TIMER_SLOT_INDEX]
        for slot in card.effect_slots:
            if not slot.trigger_id:
                continue
            evaluation = evaluate_effect_trigger_aggressive_up3(inputs, trigger)
            if not evaluation.supported:
                raise Plan2EffectTriggerAggressiveUp3ContractError(
                    "effect-trigger-fail-closed",
                    f"{card.ref}:{slot.slot_index}:{evaluation.reasons}",
                )
            evaluations.append((slot.slot_index, evaluation))
            if evaluation.fires:
                candidates.append(slot.slot_index)
        candidates.sort()
        timer_slot = slots_by_index[TIMER_SLOT_INDEX]
        timer = TimerEnqueueHandoff(
            slot_index=TIMER_SLOT_INDEX,
            parent_effect_id=timer_slot.effect_id,
            delay=timer_slot.timer_delay or 0,
            child_effect_id=timer_slot.chain_effect_id,
        )
        execution_order = ["slot-0:timer-parent-enqueue"]
        if TRIGGER_SLOT_INDEX in candidates:
            execution_order.append("slot-1:triggered-status-enchant")
        passes.append(
            OrderedEffectPass(
                pass_index=pass_index,
                snapshot_aggressive_status_value=inputs.aggressive_status_value,
                slot_evaluations=tuple(evaluations),
                candidate_slot_indexes=tuple(candidates),
                timer_handoff=timer,
                execution_order=tuple(execution_order),
            )
        )
    return EffectTriggerBuildProbe(
        card_ref=card.ref,
        card_origin=inputs.card_origin,
        payment_committed_before_build=inputs.payment_committed,
        repetitions=repetitions,
        passes=tuple(passes),
    )


simulate_ordered_effect_trigger_build = simulate_effect_trigger_build


def _validate_reused_native_evidence() -> dict[str, object]:
    """Prove the up9 evidence is the same field/call-site primitive."""

    up9_field = UP9_EFFECT_NATIVE_EVIDENCE["fieldPredicate"]
    up9_effect_slot = UP9_EFFECT_NATIVE_EVIDENCE["effectSlotCallSite"]
    required_field = {
        "symbol": "ExamExtensions.IsEffectTriggerFieldValid",
        "rva": "0x68072F4",
        "getterSymbol": "ExamStatusEffectCollection.GetAggressive",
        "getterRva": "0x7E99350",
    }
    required_slot = {
        "symbol": "ExamSequence.ExecuteCardCommandImpl",
        "rva": "0x7ECE628",
        "call": "ExamCardData.get_PlayEffectTriggerList -> IsEffectTriggerFieldValid(trigger, context)",
        "boundary": "pre-payment-direct-effect-build",
    }
    if any(up9_field.get(key) != value for key, value in required_field.items()):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "reused-predicate-evidence-mismatch"
        )
    if any(up9_effect_slot.get(key) != value for key, value in required_slot.items()):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "reused-effect-callsite-evidence-mismatch"
        )
    card_field = UP9_CARD_NATIVE_EVIDENCE["android_v3_2_3"]["field_predicate"]
    return {
        "predicateReuse": {
            "up9CardModule": "plan2_card_trigger_aggressive_up9.ANDROID_V323_PC_METADATA_EVIDENCE",
            "up9EffectModule": "plan2_effect_triggers_aggressive_up9.ANDROID_NATIVE_EVIDENCE",
            "sameFieldPredicate": True,
            "sameFieldSymbol": up9_field["symbol"],
            "sameGetter": up9_field["getterSymbol"],
            "up3Threshold": TARGET_THRESHOLD,
        },
        "effectTriggerCallsite": {
            "source": up9_effect_slot["sourceLines"],
            "symbol": up9_effect_slot["symbol"],
            "rva": up9_effect_slot["rva"],
            "call": up9_effect_slot["call"],
            "phase": "ExecuteCardCommandImpl direct effect candidate build",
            "sameAsUp9EffectLeaf": True,
            "notCardAdmissionPhase": True,
        },
        "cardAdmissionContrast": {
            "up9CardEvidenceSymbol": card_field["symbol"],
            "phase": "IsPlayable/card admission; separate call site",
            "samePredicateDifferentCallsite": True,
        },
    }


UP3_REUSED_PREDICATE_EVIDENCE: Final = _validate_reused_native_evidence()

ANDROID_V323_PC_EFFECT_TRIGGER_EVIDENCE: Final[dict[str, object]] = {
    "android_v3_2_3": {
        "field_enum": {
            "source": "_research/android/game-v3.2.3/il2cppdumper/dump.cs:697025-697026",
            "symbol": "ProduceExamFieldStatusType.CardPlayAggressiveUp",
            "value": 42,
        },
        "field_predicate": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
            "symbol": "ExamExtensions.IsEffectTriggerFieldValid",
            "rva": "0x68072F4",
            "helper_symbol": "ExamExtensions.IsFieldStatusTriggerStatusEffect",
            "helper_rva": "0x68082D4",
            "getter_symbol": "ExamStatusEffectCollection.GetAggressive",
            "getter_rva": "0x7E99350",
            "fact": "reads signed current Aggressive scalar; threshold comparison is inclusive",
            "not_read": [
                "global card-play count",
                "per-turn card-play count",
                "Aggressive/status delta",
                "TriggerEffectStatusEffect.SpendCount",
            ],
        },
        "effect_trigger_callsite": {
            "source": "docs/android-v323-card-transaction-order-report.md and ExamSequence.txt",
            "symbol": "ExamSequence.ExecuteCardCommandImpl",
            "rva": "0x7ECE628",
            "list": "PlayProduceExamEffectList + PlayEffectTriggerList[i]",
            "predicate": "IsEffectTriggerFieldValid(trigger, context)",
            "phase": "direct effect candidate build",
            "normal_hand": "candidate build before ConsumeCardCost",
            "forced_extra_pool": "candidate build after ConsumeCardCost only when UsePool consumes cost",
            "direct_effects": "all direct effects execute after candidate build",
        },
        "command_factory": {
            "used": COMMAND_FACTORY,
            "used_rva": "0x7EC35E0",
            "not_used": TWO_ARGUMENT_COMMAND_FACTORY,
            "not_used_rva": "0x7EC392C",
            "original_slot_trigger_reread": False,
        },
        "transaction": {
            "use_hand": [
                "ExecuteCardCommandImpl(Hand, card, true)",
                "ConsumeCardCost(card, context)",
                "cost/difference reactives",
                "ordered direct PlayEffect slots",
                "UserCardAfterCheck",
                "MovePlayCard",
                "play-count/history updates",
            ],
            "use_pool": [
                "ValidateCardCost / IsPlayable",
                "ConsumeCardCost when IsConsumeCost",
                "cost differences",
                "ExecuteCardCommandImpl(position, card, false, context)",
                "ordered direct PlayEffect slots",
            ],
            "snapshot_rule": "each callsite build reads current Aggressive; no post-direct-effect rerun for this card's own slot",
            "repeat_rule": "UsePlayCountBuff supplies N extra repeats; each N+1 pass walks Master order and reads slot 1 again",
        },
        "timer_handoff": {
            "source": "docs/android-v323-effect-timer-chain.md",
            "slot": TIMER_SLOT_INDEX,
            "delay": TIMER_DELAY,
            "relative_order": "slot 0 timer enqueue precedes slot 1 triggered status-enchant execution",
            "executor_or_formula_owned_by": "plan2_timer_lesson_depend_review.py",
        },
    },
    "pc_structural_crosscheck": {
        "source": "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/targeted-metadata-index.json",
        "compatibility_source": "docs/android-pc-exam-metadata-compatibility.md",
        "symbols": {
            "ExamExtensions.IsEffectTriggerFieldValid": {"parameterCount": 2},
            "ExamSequence.ExecuteCardCommandImpl": {"parameterCount": 4},
            "ExamPlayCommand.CreatePlayEffectCommand": {"overloads": [1, 2]},
            "ExamStatusEffectCollection.GetAggressive": {"parameterCount": 1},
            "ExamStatusEffectCollection.GetCardPlayValidEffectList": {"parameterCount": 2},
        },
        "authority": "PC metadata is structural cross-check only; Android v3.2.3 native timing/predicate is authoritative",
    },
    "reused_up9": UP3_REUSED_PREDICATE_EVIDENCE,
}


def build_plan2_effect_trigger_aggressive_up3_native_audit(
    database: Path = DEFAULT_DATABASE,
    *,
    focused_test_count: int | None = None,
) -> dict[str, object]:
    """Return a standalone audit payload without writing central coverage."""

    if focused_test_count is not None and (
        type(focused_test_count) is not int or focused_test_count < 0
    ):
        raise Plan2EffectTriggerAggressiveUp3ContractError(
            "invalid-focused-test-count"
        )
    catalog = load_plan2_effect_trigger_aggressive_up3_catalog(database)
    probes = {
        "below3": simulate_effect_trigger_build(
            catalog.card(0),
            EffectTriggerEvaluationInput(aggressive_status_value=2),
            repetitions=1,
        ).to_dict(),
        "at3": simulate_effect_trigger_build(
            catalog.card(0),
            EffectTriggerEvaluationInput(aggressive_status_value=3),
            repetitions=1,
        ).to_dict(),
        "forcedPostPaymentAt3": simulate_effect_trigger_build(
            catalog.card(3),
            EffectTriggerEvaluationInput(
                aggressive_status_value=3,
                card_origin="forced",
                is_use_playable_count=False,
                boundary=EffectTriggerBoundary.POST_PAYMENT_DIRECT_EFFECT_BUILD,
                payment_committed_before_build=True,
            ),
            repetitions=1,
        ).to_dict(),
    }
    return {
        "schema_version": 1,
        "scope": "Plan2 standalone effect-trigger leaf: e_trigger-none-card_play_aggressive_up-3",
        "validation": {
            "internet": "not_used",
            "master_access": "read_only",
            "hash_verification": "not_performed",
            "security_verification": "not_performed",
            "clock_verification": "not_performed",
            "focused_tests": (
                f"{focused_test_count} passed"
                if focused_test_count is not None
                else "not_recorded"
            ),
            "runtime_agent_calls": False,
        },
        "target": {
            "card_id": TARGET_CARD_ID,
            "name": "ふれんどめもりーず",
            "upgrades": list(TARGET_UPGRADES),
            "version_refs": [list(ref) for ref in TARGET_CARD_LEVEL_REFS],
            "trigger_id": TARGET_TRIGGER_ID,
            "threshold": TARGET_THRESHOLD,
        },
        "master_snapshot": {
            "database": "var/master.sqlite3",
            "trigger": catalog.trigger.to_dict(),
            "ordered_versions": [card.to_dict() for card in catalog.card_versions],
            "effect_rows": [effect.to_dict() for effect in catalog.effect_rows],
            "status_rows": [status.to_dict() for status in catalog.status_rows],
        },
        "predicate": {
            "phase": PHASE_NONE,
            "signed_snapshot": "current Aggressive at ExecuteCardCommandImpl candidate build",
            "comparison": "signed_aggressive_value >= 3",
            "boundary": "inclusive; at 3 fires, at 2 does not",
            "read_fields": ["current Aggressive scalar"],
            "ignored_fields": [
                "global card-play count",
                "per-turn card-play count",
                "Aggressive/status delta",
                "TriggerEffectStatusEffect.SpendCount",
            ],
            "same_card_trigger_reread_at_runner": False,
        },
        "ordered_slots": {
            "slot_order": [0, 1],
            "trigger_slot": TRIGGER_SLOT_INDEX,
            "timer_slot": TIMER_SLOT_INDEX,
            "trigger_wraps": "status-enchant wrapper on slot 1 only",
            "read_policy": "one read per trigger-bearing ordered slot per build pass; slot 0 has no trigger",
            "timer_enqueue_relative_order": "slot 0 enqueue before slot 1 wrapper execution",
            "all_versions": [card.to_dict() for card in catalog.card_versions],
        },
        "transaction_order": {
            "normal": "build trigger candidates -> cost payment -> direct effects",
            "forced": "UsePool may cost-pay first -> build trigger candidates -> direct effects",
            "extra": "same field predicate; isUsePlayableCount/repeat mode is inert to >=3",
            "post_build": [
                "slot 0 Timer parent enqueue (delay 4 handoff)",
                "slot 1 status-enchant wrapper if trigger fires",
                "later UserCardAfterCheck/MovePlayCard/count updates",
            ],
            "direct_effects_before_trigger": False,
            "cost_is_reexecuted_by_trigger": False,
        },
        "accounting": catalog.accounting(),
        "native_evidence": ANDROID_V323_PC_EFFECT_TRIGGER_EVIDENCE,
        "scope_guard": {
            "new_module": "src/gkms_tool/plan2_effect_trigger_aggressive_up3.py",
            "new_tests": ["tests/test_plan2_effect_trigger_aggressive_up3.py"],
            "new_native_audit": str(NATIVE_AUDIT_ARTIFACT.relative_to(PROJECT_ROOT)),
            "central_formal_coverage_read_only": "var/coverage/plan2_card_executable_coverage.json",
            "central_formal_coverage_modified": False,
            "plan2_core_runtime_modified": False,
            "native_search_modified": False,
            "plan3_modified": False,
            "gui_or_controller_modified": False,
            "unknown_or_altered_shape": "fail_closed",
        },
    }


build_native_audit = build_plan2_effect_trigger_aggressive_up3_native_audit


__all__ = [
    "ANDROID_V323_PC_EFFECT_TRIGGER_EVIDENCE",
    "AggressiveEffectTriggerInput",
    "CARD_NAME_BY_UPGRADE",
    "CARD_ORIGINS",
    "COMMAND_FACTORY",
    "DIRECT_EFFECT_BUILD_BOUNDARY",
    "EffectTriggerBoundary",
    "EffectTriggerBuildProbe",
    "EffectTriggerCardSlot",
    "EffectTriggerContract",
    "EffectTriggerEvaluation",
    "EffectTriggerEvaluationInput",
    "EffectTriggerResolution",
    "EffectTriggerStage",
    "EXACT_TARGET_TRIGGER",
    "END_TURN_TRIGGER_ID",
    "NATIVE_AUDIT_ARTIFACT",
    "Plan2EffectTriggerAggressiveUp3Catalog",
    "Plan2EffectTriggerAggressiveUp3ContractError",
    "STATUS_CHILD_EFFECT_ID_BY_UPGRADE",
    "STATUS_ENCHANT_ID_BY_UPGRADE",
    "STATUS_WRAPPER_EFFECT_ID_BY_UPGRADE",
    "TARGET_CARD_ID",
    "TARGET_CARD_LEVEL_REFS",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "TIMER_CHILD_EFFECT_ID_BY_UPGRADE",
    "TIMER_DELAY",
    "TIMER_EFFECT_ID_BY_UPGRADE",
    "TIMER_SLOT_INDEX",
    "TRIGGER_SLOT_INDEX",
    "TargetCardVersion",
    "TimerEnqueueHandoff",
    "build_native_audit",
    "build_plan2_effect_trigger_aggressive_up3_native_audit",
    "evaluate_aggressive_effect_trigger",
    "evaluate_effect_trigger_aggressive_up3",
    "load_catalog",
    "load_plan2_effect_trigger_aggressive_up3_catalog",
    "resolve_aggressive_effect_trigger",
    "resolve_effect_trigger_aggressive_up3",
    "simulate_effect_trigger_build",
    "simulate_ordered_effect_trigger_build",
    "validate_target_card_version",
]
